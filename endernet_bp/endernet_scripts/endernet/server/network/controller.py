# coding=utf-8
"""主控制器：唯一准入出口、网络运行状态与只读设备状态聚合。

对应任务 03-02 至 03-05 与 03-07。参照 AE2 的 `me/Grid.java`：网格自己不做
物流，它只决定「谁现在可以动手」并把请求交给对应的服务。本模块是同一件事
在 EnderNet 里的落点，也是全局约束 2（输入、输出、终端和订单请求均由所属
主控制器准入）唯一的执行者。

**身份绑定的是实例，不是编号。** 主控认的是「哪个控制器方块实例」
（`DeviceRef` = 位置 + 代次），不是 02 分配的分量编号。分量编号在成员增减时
会尽量沿用，但**位置被顶替**（拆掉再在原位放一个新的）时，新的控制器方块
绝不能自动继承旧主控的身份与归属（03 文档明确要求）。
`instance_id` 由 ref 推导，同一次进程内稳定；进程重启后会重新分配
（与 02 的分量编号同样的口径），持久化身份由任务 12 另行绑定。

**准入是一条有序的链，不是一个布尔判断。** 顺序见 `MasterController.admit()`。
其中第 5 条（主控实例已加载）与第 7 条（`topology.validate`）**必须都在**：
02 的 `validate()` 在控制器区块卸载而终端区块仍加载时仍然放行——那是有意的
（不把「暂时访问不到」当成「已拆除」），但业务不能据此开工。这是
`plans/tasks/01-交付说明.md` 第八节点名要 03 明确的第一项契约，
`test_controller.py` 里有一条专门的用例钉住它。

**只读路径不设门禁。** `device_status()` 在无主控、断电、超载、分量不存在时
都要能答（03-07 与 11-13 的非控制器设备状态栏）。这不是「降级」，而是诊断
本来就该在任何状态下可读——否则设备离线时界面反而看不到原因。

本模块不 import `runtime.py`：主控持有的是运行时的**鸭子类型**引用，
依赖方向只有「运行时 → 主控」一条。
"""

from ...common.identity import DeviceRef, NodeKey  # noqa: F401（供 # type: 注释）
from ...common.protocol import (
    BlockReason,
    CommandKind,
    CommandResult,
    DeviceCommand,  # noqa: F401
    Reason,
    STATUS_BLOCKED,
    STATUS_IN_FLIGHT,
    STATUS_STALE,
    reason_code,
)
from .admission import BandwidthState
from .services import AssemblyProfile, ServiceId, is_read_only, route_of


# -- 运行状态 --------------------------------------------------------------


class RunState(object):
    """一个网络的运行状态。**网络级**，不含设备级带宽（那是 `bandwidth_ok`）。

    界面拿它做总体在线/离线；具体原因看 `blockers()`。两者分开是有意的：
    状态是粗粒度的桶，原因是可定位的清单，界面不该从状态字符串反推原因。
    """

    #: 分量里没有控制器（已确认）。**不等于 `MASTER_ABSENT`**：
    #: 那个是「有控制器，但没人接管它」。
    NO_CONTROLLER = "no_controller"
    #: 有控制器实例，但没有主控运行时接管它。
    MASTER_ABSENT = "master_absent"
    #: 主控实例的区块未加载（临时不可访问，不是拆除）。
    MASTER_UNLOADED = "master_unloaded"
    #: 主控恢复流程尚未完成。
    MASTER_RECOVERING = "master_recovering"
    #: 分量正在重建，结论待确认。
    REBUILDING = "rebuilding"
    #: 分量里有多个控制器实例，非法连接。
    CONTROLLER_CONFLICT = "controller_conflict"
    #: 边界未确认（还有未加载成员），控制器数量未知。
    TOPOLOGY_UNKNOWN = "topology_unknown"
    #: 能源不可用或待确认。
    NO_POWER = "no_power"
    #: 必需的网络服务缺失或尚未实现。
    SERVICE_ABSENT = "service_absent"
    #: 一切条件满足，可以执行业务命令。
    RUNNABLE = "runnable"

    ALL = frozenset(
        [
            NO_CONTROLLER,
            MASTER_ABSENT,
            MASTER_UNLOADED,
            MASTER_RECOVERING,
            REBUILDING,
            CONTROLLER_CONFLICT,
            TOPOLOGY_UNKNOWN,
            NO_POWER,
            SERVICE_ABSENT,
            RUNNABLE,
        ]
    )

    TEXT = {
        NO_CONTROLLER: "无控制器",
        MASTER_ABSENT: "主控未接管",
        MASTER_UNLOADED: "主控未加载",
        MASTER_RECOVERING: "主控恢复中",
        REBUILDING: "网络重建中",
        CONTROLLER_CONFLICT: "控制器冲突",
        TOPOLOGY_UNKNOWN: "网络状态未知",
        NO_POWER: "能源不可用",
        SERVICE_ABSENT: "服务未就绪",
        RUNNABLE: "运行中",
    }

    @staticmethod
    def text_of(state):
        # type: (str) -> str
        return RunState.TEXT.get(state, state)


#: 原因的**优先级**顺序：前面的先决定 `state`。
#: 顺序不是随意的——「还在重建」比「没有控制器」更根本，因为重建期间的
#: 控制器计数本来就作数不了，先报后者会把「待确认」说成「确认没有」。
_BLOCKER_STATES = (
    (Reason.REBUILDING, RunState.REBUILDING),
    (Reason.CONTROLLER_CONFLICT, RunState.CONTROLLER_CONFLICT),
    (Reason.TOPOLOGY_UNKNOWN, RunState.TOPOLOGY_UNKNOWN),
    (Reason.NO_CONTROLLER, RunState.NO_CONTROLLER),
    (Reason.MASTER_ABSENT, RunState.MASTER_ABSENT),
    (Reason.MASTER_UNLOADED, RunState.MASTER_UNLOADED),
    (Reason.MASTER_RECOVERING, RunState.MASTER_RECOVERING),
    (Reason.BROWNOUT, RunState.NO_POWER),
    (Reason.ENERGY_UNAVAILABLE, RunState.NO_POWER),
    (Reason.SERVICE_NOT_READY, RunState.SERVICE_ABSENT),
    (Reason.SERVICE_ABSENT, RunState.SERVICE_ABSENT),
    # 正式档下「准入策略未定案」意味着数值不可信，与「服务不可用」同样
    # 不能开工。精确原因由 `blockers()` 给出，状态桶只区分能不能跑。
    (Reason.ASSEMBLY_UNRESOLVED, RunState.SERVICE_ABSENT),
)

#: 只有这一个状态允许执行业务命令。写成显式集合而不是取反，
#: 是为了将来新增状态时**默认拒绝**。
RUNNABLE_STATES = frozenset([RunState.RUNNABLE])

#: 每条业务命令都需要的服务：计时与供能。缺了它们整个网络都动不了，
#: 因此放在网络级判定；库存与工厂是**按命令**需要的，见 `route_blocker`。
NETWORK_CRITICAL_SERVICES = (ServiceId.SCHEDULER, ServiceId.ENERGY)

#: 「结论还没确认」的原因码。设备状态栏据此把 `validity` 标成 `pending`。
#: 界面**不出现第三种文本**：待确认与确认离线都显示离线，只是详情里说明。
PENDING_CODES = frozenset(
    [
        Reason.REBUILDING,
        Reason.TOPOLOGY_UNKNOWN,
        Reason.MASTER_ABSENT,
        Reason.MASTER_RECOVERING,
        Reason.BANDWIDTH_UNKNOWN,
        Reason.BANDWIDTH_UNCONFIGURED,
        Reason.DEFINITION_UNCONFIGURED,
        Reason.ENERGY_UNAVAILABLE,
        Reason.SERVICE_ABSENT,
        Reason.SERVICE_NOT_READY,
        Reason.ASSEMBLY_UNRESOLVED,
    ]
)


def run_state_of(blockers):
    # type: (tuple) -> str
    """由阻塞原因清单推出网络状态。空清单即 `RUNNABLE`。"""
    codes = set(reason_code(reason) for reason in blockers)
    for code, state in _BLOCKER_STATES:
        if code in codes:
            return state
    return RunState.RUNNABLE


def component_blockers_by_id(topology, component_id):
    # type: (object, str) -> list
    """按分量编号取分量层面的阻塞原因。

    与 `component_blockers` 分开的必要性：网络没有主控时，运行时手上只有
    编号，没有一个「代表性成员」可传——挑一个成员来问分量状态，会在该成员
    恰好还待在 `pending_keys` 里时得到与分量本身不同的答案。
    """
    component = topology.snapshot().components.get(component_id)
    if component is None:
        return [BlockReason(Reason.REBUILDING, "该网络尚未归属或已消失")]
    return list(component.reason_list())


def component_blockers(topology, node_key):
    # type: (object, object) -> list
    """分量层面的阻塞原因（重建中 / 无控制器 / 冲突 / 未知）。

    **不判断「能否运行」**——`ComponentInfo.is_runnable()` 只答拓扑条件，
    这里也一样：能源、带宽、服务都在这条清单之外，由调用方追加。
    """
    snapshot = topology.snapshot()
    component = snapshot.component_of(node_key)
    if component is None:
        return [BlockReason(Reason.REBUILDING, "该位置尚未归属到任何网络")]
    out = list(component.reason_list())
    if node_key in snapshot.pending_keys and not component.pending:
        out.append(BlockReason(Reason.REBUILDING, "该位置正在重新归属"))
    return out


def power_blocker(services, node_key):
    # type: (object, object) -> object
    """能源阻塞原因；没有则为 None。

    两种 `SERVICE_ABSENT` 都保留着，但各有各的适用面：**服务未装配**是
    结构性问题；**服务是替身**只可能出现在调用方自己塞了替身的装配里
    （默认表里能源已经是正式实现）。把它们说成断电会让人去查供电，
    而真正的问题是那项服务根本拿不到。

    三态落在这里：`False` 才是「确认没电」（`BROWNOUT`），`None` 是
    「还没法确认」（`ENERGY_UNAVAILABLE`）——**待确认不等于有电**，
    与带宽的三态同一条纪律。区分这两者是有意的：把「算不出需求」说成
    「没电」会让人去修供电，而真正要做的是把定义表或输入补齐。
    """
    if services is None:
        return BlockReason(Reason.SERVICE_ABSENT, "网络服务未装配")
    service = services.get(ServiceId.ENERGY)
    if service is None:
        return BlockReason(Reason.SERVICE_ABSENT, "能源服务未装配")
    if service.is_stand_in:
        return BlockReason(
            Reason.SERVICE_ABSENT,
            "能源服务尚未实现（任务 %s）" % (service.pending_task,),
        )
    ok = service.power_ok(node_key)
    if ok is True:
        return None
    if ok is False:
        return BlockReason(Reason.BROWNOUT, "能源不足")
    # None = 待确认。**未确认不等于有电**，与带宽的三态同一条纪律。
    return BlockReason(Reason.ENERGY_UNAVAILABLE, "能源判定尚未确认")


def power_state_of(services, node_key):
    # type: (object, object) -> object
    """本设备当前是否有能源。True / False / None（待确认或服务未实现）。"""
    if services is None:
        return None
    service = services.get(ServiceId.ENERGY)
    if service is None or service.is_stand_in:
        return None
    return service.power_ok(node_key)


def service_blockers(services, profile=AssemblyProfile.DEV, policy=None):
    # type: (object, str, object) -> tuple
    """网络级服务阻塞原因：计时、供能，以及正式档下的未定规则。"""
    if services is None:
        return (BlockReason(Reason.SERVICE_ABSENT, "网络服务未装配"),)
    out = []
    for service_id in NETWORK_CRITICAL_SERVICES:
        service = services.get(service_id)
        if service is None:
            out.append(BlockReason(Reason.SERVICE_ABSENT, "服务 %s 未装配" % (service_id,)))
        elif service.is_stand_in:
            out.append(
                BlockReason(
                    Reason.SERVICE_ABSENT,
                    "服务 %s 尚未实现（任务 %s）" % (service_id, service.pending_task),
                )
            )
        elif not service.is_ready():
            out.append(BlockReason(Reason.SERVICE_NOT_READY, "服务 %s 尚未就绪" % (service_id,)))
    if profile == AssemblyProfile.PRODUCTION and policy is not None:
        unresolved = policy.unresolved_report()
        if unresolved:
            out.append(
                BlockReason(
                    Reason.ASSEMBLY_UNRESOLVED,
                    "正式装配档下准入策略仍有 %d 项未定案：%s"
                    % (
                        len(unresolved),
                        "、".join(str(item.get("field")) for item in unresolved),
                    ),
                )
            )
    return tuple(out)


def route_blocker(services, kind):
    # type: (object, str) -> object
    """本命令所需服务是否可取。返回 `BlockReason` 或 None。

    与 `service_blockers` 分开：那一条管「整个网络动不了」，这一条管
    「这条命令要的那项服务没有」。两条都会给出 `SERVICE_ABSENT`，
    但说明不同——诊断要能分辨「全网缺调度」与「这个功能还没写」。
    """
    route = route_of(kind)
    if route is None:
        return BlockReason(Reason.BAD_PARAMETER, "未知命令类型: %r" % (kind,))
    service_id = route[0]
    if service_id is None:
        return None
    if services is None:
        return BlockReason(Reason.SERVICE_ABSENT, "网络服务未装配")
    service = services.get(service_id)
    if service is None:
        return BlockReason(Reason.SERVICE_ABSENT, "服务 %s 未装配" % (service_id,))
    if service.is_stand_in:
        return BlockReason(
            Reason.SERVICE_ABSENT,
            "服务 %s 尚未实现（任务 %s）" % (service_id, service.pending_task),
        )
    if not service.is_ready():
        return BlockReason(Reason.SERVICE_NOT_READY, "服务 %s 尚未就绪" % (service_id,))
    return None


# -- 请求去重账本 ----------------------------------------------------------

#: 账本存档版本。任务 12 用它判断能否读入。
LEDGER_SCHEMA_VERSION = 1


class LedgerPolicy(object):
    """去重账本的保留策略。

    **范围** = 本主控实例（不是全网，也不是进程级）：主控被顶替或分量
    重组时账本随实例走，因为「这条请求处理过没有」是相对某个主控说的。
    **期限** = 按 tick 计。引擎有两个不同频率的时钟，因此这里存的是
    调用方给的 tick 计数，**不写死秒数换算**。
    """

    def __init__(self, retention_ticks=900, max_entries=4096):
        # type: (int, int) -> None
        self.retention_ticks = int(retention_ticks)
        self.max_entries = int(max_entries)

    def describe(self):
        # type: () -> dict
        return {
            "retention_ticks": self.retention_ticks,
            "max_entries": self.max_entries,
            "scope": "master_instance",
            "persisted": False,
        }


class _Entry(object):
    """账本里的一条：在途或已完结。"""

    __slots__ = ("key", "tick", "result")

    def __init__(self, key, tick):
        self.key = key
        self.tick = int(tick)
        self.result = None

    def is_open(self):
        return self.result is None


class RequestLedger(object):
    """同一条请求只执行一次。

    键是 `(request_id, 来源设备实例)`：`request_id` 由发起方生成，
    来源设备实例由服务端从命令里取，因此**客户端无法通过换 request_id
    绕过去重，也无法冒用别的设备已用过的 request_id**。

    **只记真正产生了效果的结果。** 被阻塞、版本过期、参数被拒的请求
    不留记录：它们没有副作用，而且绝大多数是暂时状态；把「暂时不通」
    记成终局，会让设备拿同一个 request_id 永远重试不成功。账本防的是
    「重复执行」，不是「缓存答案」。

    本实例**不持久化**。`export_state` / `import_state` 是留给任务 12 的钩子，
    本次不接线（03 文档 03-05：保留期与范围先定下来，落盘由 12 做）。
    """

    #: `begin()` 的三态。
    BEGIN_STARTED = "started"
    BEGIN_IN_FLIGHT = "in_flight"
    BEGIN_REPLAYED = "replayed"

    def __init__(self, policy=None):
        # type: (LedgerPolicy) -> None
        self.policy = policy if policy is not None else LedgerPolicy()
        self._entries = {}
        #: 插入顺序。py2.7 的字典无序，而按最旧淘汰需要确定的顺序。
        self._order = []
        self.stats = {
            "begun": 0,
            "finished": 0,
            "replayed": 0,
            "aborted": 0,
            "pruned": 0,
            "evicted": 0,
        }

    def begin(self, key, tick):
        # type: (tuple, int) -> str
        entry = self._entries.get(key)
        if entry is not None:
            if entry.is_open():
                return RequestLedger.BEGIN_IN_FLIGHT
            return RequestLedger.BEGIN_REPLAYED
        while len(self._order) >= self.policy.max_entries:
            self._evict_oldest()
        self._entries[key] = _Entry(key, tick)
        self._order.append(key)
        self.stats["begun"] += 1
        return RequestLedger.BEGIN_STARTED

    def finish(self, key, result, tick=None):
        # type: (tuple, object, object) -> bool
        """记下结果。返回是否真的入账。"""
        if result is None or not result.is_applied():
            self.abort(key)
            return False
        entry = self._entries.get(key)
        if entry is None:
            entry = _Entry(key, tick if tick is not None else 0)
            self._entries[key] = entry
            self._order.append(key)
        entry.result = result
        if tick is not None:
            entry.tick = int(tick)
        self.stats["finished"] += 1
        return True

    def abort(self, key):
        # type: (tuple) -> bool
        """清掉在途标记。业务抛错、请求被拒、判定为阻塞时都要走它。"""
        entry = self._entries.pop(key, None)
        if entry is None:
            return False
        try:
            self._order.remove(key)
        except ValueError:
            pass
        self.stats["aborted"] += 1
        return True

    def replay(self, key):
        # type: (tuple) -> object
        entry = self._entries.get(key)
        if entry is None or entry.result is None:
            return None
        return entry.result

    def prune(self, tick):
        # type: (int) -> int
        """丢掉超过保留期的条目。返回清掉的条数。"""
        limit = int(tick) - self.policy.retention_ticks
        dropped = 0
        for key in list(self._order):
            entry = self._entries.get(key)
            if entry is None:
                self._order.remove(key)
                continue
            if entry.tick >= limit:
                continue
            del self._entries[key]
            self._order.remove(key)
            dropped += 1
        self.stats["pruned"] += dropped
        return dropped

    def _evict_oldest(self):
        # type: () -> None
        if not self._order:
            return
        key = self._order[0]
        del self._entries[key]
        self._order.pop(0)
        self.stats["evicted"] += 1

    def __len__(self):
        return len(self._order)

    def keys(self):
        # type: () -> list
        return list(self._order)

    # -- 存档钩子（留给任务 12） --------------------------------------

    def export_state(self):
        # type: () -> dict
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "retention_ticks": self.policy.retention_ticks,
            "max_entries": self.policy.max_entries,
            "entries": [
                {
                    "request_id": key[0],
                    "source": key[1],
                    "tick": self._entries[key].tick,
                    "result": (
                        self._entries[key].result.to_dict()
                        if self._entries[key].result is not None
                        else None
                    ),
                }
                for key in self._order
            ],
        }

    def import_state(self, data):
        # type: (dict) -> int
        """从存档恢复。

        版本不认就报错而不是尽量读——按项目纪律，未知的**更新**版本要原样
        保留并标记只读，不能读成空值再覆盖回去。
        """
        if not isinstance(data, dict):
            raise ValueError("账本存档必须是字典")
        version = data.get("schema_version")
        if version != LEDGER_SCHEMA_VERSION:
            raise ValueError("不支持的账本存档版本: %r" % (version,))
        self._entries = {}
        self._order = []
        for item in data.get("entries") or ():
            key = (item.get("request_id"), item.get("source") or "")
            if key[0] is None or key in self._entries:
                continue
            entry = _Entry(key, item.get("tick") or 0)
            raw = item.get("result")
            if raw is not None:
                entry.result = result_from_dict(raw)
            self._entries[key] = entry
            self._order.append(key)
        return len(self._order)

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report["policy"] = self.policy.describe()
        report["size"] = len(self._order)
        report["open"] = sum(
            1 for key in self._order if self._entries[key].is_open()
        )
        return report


def result_from_dict(data):
    # type: (dict) -> CommandResult
    """`CommandResult.to_dict()` 的逆。供账本存档恢复使用。"""
    reasons = tuple(
        item
        if isinstance(item, BlockReason)
        else BlockReason(
            (item or {}).get("code", ""),
            (item or {}).get("detail", ""),
        )
        for item in (data.get("reasons") or ())
    )
    return CommandResult(
        data.get("request_id") or "",
        data.get("status"),
        data.get("payload"),
        reasons,
        data.get("topology_version"),
    )


def ledger_key(command):
    # type: (DeviceCommand) -> tuple
    """账本键 = `(request_id, 来源设备实例)`。

    来源取 `source.device_ref`（玩家操作的终端）；设备自身的到期任务没有
    玩家来源时退回命令的目标设备，这样同一个设备发出的两条同号请求
    仍然只执行一次。
    """
    ref = None
    if command.source is not None and command.source.device_ref is not None:
        ref = command.source.device_ref
    elif command.device_ref is not None:
        ref = command.device_ref
    return (command.request_id, ref.as_key() if ref is not None else "")


# -- 许可 ------------------------------------------------------------------


class AccessGrant(object):
    """主控签发的一次性许可。运行时校验通过后才交出服务。

    这是「设备不得绕过主控制器直接改库存」的**结构性**保证：服务不公开
    取用，只有拿着当前主控签发、版本未过期、尚未消费的许可才拿得到。

    `topology_version` 记的是**签发时**的图版本，不是命令带来的那个：
    命令可以不带版本（只读查询常这样），但许可必须能证明自己是本帧的。
    """

    __slots__ = (
        "request_id",
        "controller_instance_id",
        "topology_version",
        "issued_tick",
        "service_id",
        "_consumed",
    )

    def __init__(
        self,
        request_id,
        controller_instance_id,
        topology_version,
        issued_tick,
        service_id=None,
    ):
        # type: (str, str, int, int, str) -> None
        self.request_id = request_id
        self.controller_instance_id = controller_instance_id
        self.topology_version = topology_version
        self.issued_tick = issued_tick
        #: 本次许可允许取用的服务。只读命令没有对应服务，为 None。
        self.service_id = service_id
        self._consumed = False

    @property
    def consumed(self):
        return self._consumed

    def consume(self):
        # type: () -> bool
        """标记已使用。返回是否**本次**才被消费（重复使用返回 False）。"""
        if self._consumed:
            return False
        self._consumed = True
        return True

    def to_dict(self):
        # type: () -> dict
        return {
            "request_id": self.request_id,
            "controller_instance_id": self.controller_instance_id,
            "topology_version": self.topology_version,
            "issued_tick": self.issued_tick,
            "service_id": self.service_id,
            "consumed": self._consumed,
        }

    def __repr__(self):
        return "AccessGrant(%s, %s)" % (self.request_id, self.service_id)


# -- 设备总体状态（03-07 / 11-13 的字段来源） ------------------------------


class DeviceNetworkStatus(object):
    """一台接网设备的**总体**在线/离线与原因。

    界面（blocks.md 第 9 条）只要一个布尔值与一段原因：非控制器设备用
    `stat_bar` 显示绿色在线或红色离线，点 `interact_btn` 弹出详情。
    因此这里**只有两种文本**——待确认也显示离线，`validity` 与 `reasons`
    负责说明「为什么还没确认」。

    在线 = 已确认接入唯一有效主控 **且** 型号被该主控支持 **且** 网络允许
    工作 **且** 带宽与能源都满足。任一不满足**或未确认**即离线。
    """

    __slots__ = (
        "node_key",
        "generation",
        "device_id",
        "is_online",
        "validity",
        "master_instance_id",
        "run_state",
        "bandwidth_ok",
        "power_ok",
        "bandwidth_domain_id",
        "domain_ids",
        "ui_applicable",
        "version",
        "reasons",
        "notes",
    )

    def __init__(
        self,
        node_key,
        generation,
        device_id,
        is_online,
        validity,
        master_instance_id,
        run_state,
        bandwidth_ok,
        power_ok,
        bandwidth_domain_id=None,
        domain_ids=(),
        ui_applicable=True,
        version=0,
        reasons=(),
        notes=(),
    ):
        self.node_key = node_key
        self.generation = generation
        self.device_id = device_id
        self.is_online = bool(is_online)
        #: `ok` = 结论已确认；`pending` = 结论还没确认（仍然显示离线）。
        self.validity = validity
        self.master_instance_id = master_instance_id
        self.run_state = run_state
        #: True / False / None（待确认）。**None 不是「可以」。**
        self.bandwidth_ok = bandwidth_ok
        self.power_ok = power_ok
        self.bandwidth_domain_id = bandwidth_domain_id
        self.domain_ids = tuple(domain_ids)
        #: 控制器本身不使用这套设备状态栏（blocks.md 第 5 条）。
        self.ui_applicable = bool(ui_applicable)
        #: 计算时的图版本。界面据此判断自己拿到的是不是过期快照。
        self.version = version
        self.reasons = tuple(reasons)
        self.notes = tuple(notes)

    def state_text(self):
        # type: () -> str
        return "在线" if self.is_online else "离线"

    def reason_codes(self):
        # type: () -> tuple
        return tuple(reason_code(reason) for reason in self.reasons)

    def to_dict(self):
        # type: () -> dict
        return {
            "node_key": self.node_key.to_dict() if self.node_key is not None else None,
            "generation": self.generation,
            "device_id": self.device_id,
            "online": self.is_online,
            "state_text": self.state_text(),
            "validity": self.validity,
            "master_instance_id": self.master_instance_id,
            "run_state": self.run_state,
            "run_state_text": RunState.text_of(self.run_state),
            "bandwidth_ok": self.bandwidth_ok,
            "power_ok": self.power_ok,
            "bandwidth_domain_id": self.bandwidth_domain_id,
            "domain_ids": list(self.domain_ids),
            "ui_applicable": self.ui_applicable,
            "version": self.version,
            "reasons": [
                reason.to_dict() if isinstance(reason, BlockReason) else reason
                for reason in self.reasons
            ],
            "notes": list(self.notes),
        }

    def __repr__(self):
        return "DeviceNetworkStatus(%s, %s)" % (self.device_id, self.state_text())


def compose_device_status(
    ref,
    definitions,
    registry,
    catalog,
    topology,
    admission,
    services=None,
    profile=AssemblyProfile.DEV,
    policy=None,
    master=None,
):
    # type: (...) -> DeviceNetworkStatus
    """聚合一台设备的只读总体状态。

    **不设门禁**：没有主控、没有分量、没有服务时同样给出一份结论，
    因为设备离线时界面恰恰最需要原因。

    写成模块级函数而不是主控的方法，是为了让「分量不存在」也有答案：
    那时没有运行时，也就没有主控，但注册表、拓扑与带宽判定依然可读。
    """
    snapshot = topology.snapshot()
    record = registry.get_by_ref(ref)
    version = topology.version

    if record is None:
        return DeviceNetworkStatus(
            ref.node_key,
            ref.generation if ref is not None else None,
            None,
            False,
            "ok",
            None,
            RunState.MASTER_ABSENT,
            None,
            None,
            version=version,
            reasons=[BlockReason(Reason.DEVICE_GONE, "实例已不存在或代次过期")],
        )

    reasons = []
    notes = []
    node_key = ref.node_key

    if master is not None:
        blockers = master.blockers()
        run_state = master.state()
        master_instance_id = master.instance_id
        master_device_id = master.device_id
    else:
        blockers = component_blockers(topology, node_key)
        if snapshot.component_of(node_key) is not None:
            blockers.append(BlockReason(Reason.MASTER_ABSENT, "该网络没有主控接管"))
        run_state = run_state_of(tuple(blockers))
        master_instance_id = None
        master_device_id = None
    reasons.extend(blockers)

    bandwidth = admission.status_of(ref)
    if bandwidth.ok is not True:
        reasons.extend(bandwidth.reasons)
        if bandwidth.state == BandwidthState.UNKNOWN and not bandwidth.reasons:
            reasons.append(BlockReason(Reason.BANDWIDTH_UNKNOWN, "带宽判定尚未确认"))
    notes.extend(bandwidth.notes)

    power_ok = power_state_of(services, node_key)
    served = True
    if master_device_id is None:
        served = False
    else:
        verdict = catalog.controller_serves(master_device_id, record.device_id)
        if not verdict.allowed:
            served = False
            reasons.append(
                BlockReason(
                    Reason.CONTROLLER_DOES_NOT_SERVE,
                    "%s 不由 %s 控制：%s"
                    % (record.device_id, master_device_id, verdict.detail),
                )
            )

    is_online = (
        run_state == RunState.RUNNABLE
        and bandwidth.ok is True
        and power_ok is True
        and served
    )
    codes = set(reason_code(reason) for reason in reasons)
    # 待确认与确认离线对界面是同一个结论，但诊断里必须能分辨。
    validity = "pending" if (codes & PENDING_CODES) or bandwidth.ok is None else "ok"

    return DeviceNetworkStatus(
        node_key,
        ref.generation,
        record.device_id,
        is_online,
        validity,
        master_instance_id,
        run_state,
        bandwidth.ok,
        power_ok,
        bandwidth.bandwidth_domain_id,
        snapshot.domain_ids_of(node_key),
        ui_applicable=not catalog.is_controller(record.device_id),
        version=version,
        reasons=reasons,
        notes=notes,
    )


# -- 主控制器 --------------------------------------------------------------


class MasterController(object):
    """一个网络的主控制器。**唯一**的业务命令准入出口。

    `runtime` 是所属的 `NetworkRuntime`（鸭子类型，本模块不 import 它）：
    服务只能经由它取用，因此主控本身不持有服务表。
    """

    def __init__(
        self,
        ref,
        runtime,
        registry,
        catalog,
        topology,
        admission,
        profile=AssemblyProfile.DEV,
        policy=None,
        ledger=None,
        session_check=None,
    ):
        # type: (DeviceRef, object, object, object, object, object, str, object, RequestLedger, object) -> None
        self.ref = ref
        self.instance_id = "%s#%d" % (ref.as_key(), ref.generation)
        self.runtime = runtime
        self.registry = registry
        self.catalog = catalog
        self.topology = topology
        self.admission = admission
        self.profile = AssemblyProfile.validate(profile)
        self.policy = policy
        self.ledger = ledger if ledger is not None else RequestLedger()
        #: 会话校验钩子：`callable(source) -> bool`。
        #: 03 还没有会话层（那是 11 的），因此 None 表示「只检查
        #: `distance_ok`」。注入真实的校验器即可启用 `SESSION_INVALID`——
        #: 比在这里假装有一张会话表要诚实。
        self.session_check = session_check
        self._recovering = False
        self.tick_count = 0
        self.stats = {"submitted": 0, "admitted": 0, "rejected": 0, "blocked": 0, "stale": 0}

    # -- 身份 ----------------------------------------------------------

    @property
    def component_id(self):
        return self.runtime.component_id

    @property
    def device_id(self):
        # type: () -> str
        record = self.registry.get_by_ref(self.ref)
        return record.device_id if record is not None else None

    def is_loaded(self):
        # type: () -> bool
        record = self.registry.get_by_ref(self.ref)
        return bool(record is not None and record.loaded)

    # -- 恢复 ----------------------------------------------------------

    def begin_recovery(self):
        # type: () -> None
        """进入恢复中：业务暂停，但状态查询照常。

        任务 12 落地后由存档恢复驱动。03 内只有「挂载服务 → 完成恢复」
        这一小段，因为恢复数据目前还没有来源；这里不假装有时间轴。
        """
        self._recovering = True

    def complete_recovery(self):
        # type: () -> None
        self._recovering = False

    def is_recovering(self):
        # type: () -> bool
        return self._recovering

    # -- 状态 ----------------------------------------------------------

    def blockers(self):
        # type: () -> tuple
        """全部阻塞原因，**不截断成第一条**。

        一个网络可能同时「没有服务」又「断电」，界面要能一起说清楚。
        只有同一维度内的互斥项（重建中 / 无控制器 / 冲突 / 未知）会互相
        取代，那由 `ComponentInfo.reason_list()` 保证。
        """
        out = component_blockers(self.topology, self.ref.node_key)
        record = self.registry.get_by_ref(self.ref)
        if record is None:
            out.append(BlockReason(Reason.MASTER_ABSENT, "主控实例已不存在或代次过期"))
        elif not record.loaded:
            out.append(BlockReason(Reason.MASTER_UNLOADED, "主控实例所在区块未加载"))
        if self._recovering:
            out.append(BlockReason(Reason.MASTER_RECOVERING, "主控恢复流程尚未完成"))
        blocker = power_blocker(self.runtime.services, self.ref.node_key)
        if blocker is not None:
            out.append(blocker)
        out.extend(
            service_blockers(self.runtime.services, self.profile, self.policy)
        )
        return tuple(out)

    def state(self):
        # type: () -> str
        return self.runtime.state()

    def is_runnable(self):
        # type: () -> bool
        return self.state() in RUNNABLE_STATES

    def device_status(self, ref):
        # type: (DeviceRef) -> DeviceNetworkStatus
        """只读。任何状态下都能答（03-07）。"""
        return self.runtime.device_status(ref)

    def status_view(self):
        # type: () -> dict
        """只读快照，供 `CommandKind.STATUS` 与诊断。"""
        blockers = self.blockers()
        state = self.state()
        return {
            "instance_id": self.instance_id,
            "component_id": self.component_id,
            "device_id": self.device_id,
            "ref": self.ref.to_dict(),
            "run_state": state,
            "run_state_text": RunState.text_of(state),
            "runnable": state in RUNNABLE_STATES,
            "recovering": self._recovering,
            "topology_version": self.topology.version,
            "admission_version": self.admission.version,
            "blockers": [reason.to_dict() for reason in blockers],
            "services": self.runtime.service_view(),
        }

    # -- 准入 ----------------------------------------------------------

    def admit(self, command):
        # type: (DeviceCommand) -> tuple
        """准入门链。返回 `(grant, None)` 或 `(None, CommandResult)`。

        顺序是有意的，每一步的存在理由见下面逐条注释。**不要调整顺序**：
        第 5、7 两条是 01 点名要 03 补的洞，第 10 条在带宽之后是因为
        「这条命令要的服务没有」比「这台设备没带宽」更接近根因。
        """
        # 1 命令形状。未知类型不兜底推断：猜一个服务出来就等于让设备绕过准入。
        failure = self._check_shape(command)
        if failure is not None:
            return None, failure

        read_only = is_read_only(command.kind)
        key = ledger_key(command)
        if not read_only:
            # 2 去重。只读命令不进账本：重复读取没有副作用，而把快照
            #   缓存到保留期结束只会让终端显示过期数据。
            outcome = self.ledger.begin(key, self.tick_count)
            if outcome == RequestLedger.BEGIN_IN_FLIGHT:
                return None, CommandResult.in_flight(
                    command.request_id, self.topology.version
                )
            if outcome == RequestLedger.BEGIN_REPLAYED:
                return None, self._replayed(command, key)

        grant, failure = self._admit_after_ledger(command, read_only)
        if failure is not None and not read_only:
            # 没产生效果就不留在途标记：同一个 request_id 下次要能重新判定。
            self.ledger.abort(key)
        return grant, failure

    def _check_shape(self, command):
        # type: (DeviceCommand) -> object
        if route_of(command.kind) is None:
            return CommandResult.rejected(
                command.request_id,
                [BlockReason(Reason.BAD_PARAMETER, "未知命令类型: %r" % (command.kind,))],
                self.topology.version,
            )
        if command.source is None:
            return CommandResult.rejected(
                command.request_id,
                [BlockReason(Reason.BAD_PARAMETER, "命令缺少来源")],
                self.topology.version,
            )
        if command.device_ref is None:
            return CommandResult.rejected(
                command.request_id,
                [BlockReason(Reason.BAD_PARAMETER, "命令没有目标设备")],
                self.topology.version,
            )
        return None

    def _admit_after_ledger(self, command, read_only):
        # type: (DeviceCommand, bool) -> tuple
        version = self.topology.version
        request_id = command.request_id
        ref = command.device_ref
        source = command.source

        # 3 来源。距离由适配器给出，这里只信它；会话则要看有没有校验器。
        if not source.distance_ok:
            return None, CommandResult.rejected(
                request_id, [BlockReason(Reason.OUT_OF_RANGE, "操作距离过远")], version
            )
        if self.session_check is not None and not self.session_check(source):
            return None, CommandResult.rejected(
                request_id,
                [BlockReason(Reason.SESSION_INVALID, "会话已失效，请重新打开界面")],
                version,
            )

        # 4 目标设备属于本网络。尚未归属（component 为 None）不在这里判——
        #   那是「还不知道」，交给第 7 条的待确认处理，不是「走错门」。
        component_id = self.topology.snapshot().component_id_of(ref.node_key)
        if component_id is not None and component_id != self.component_id:
            return None, CommandResult.rejected(
                request_id,
                [
                    BlockReason(
                        Reason.WRONG_NETWORK,
                        "目标设备属于 %s，本主控管的是 %s"
                        % (component_id, self.component_id),
                    )
                ],
                version,
            )

        # 5 **主控实例存在且已加载**。01 契约 #1 的落点之一：
        #   02 的 validate() 只看目标设备自己加不加载，控制器区块卸载时
        #   它照样放行，业务不能据此开工。
        record = self.registry.get_by_ref(self.ref)
        if record is None:
            return None, CommandResult.blocked(
                request_id,
                [BlockReason(Reason.MASTER_ABSENT, "主控实例已不存在或代次过期")],
                version,
            )
        if not record.loaded:
            return None, CommandResult.blocked(
                request_id,
                [
                    BlockReason(
                        Reason.MASTER_UNLOADED,
                        "主控实例所在区块未加载；这不代表它已被拆除",
                    )
                ],
                version,
            )
        if self._recovering and not read_only:
            # 恢复未完成只挡业务，不挡诊断：03 验收明确要求「主控恢复之前
            # 仍允许状态视图与必要的恢复管理」。若在这里一并挡掉，
            # `RECOVERY` 这个入口在真正需要它的时候恰好是死的。
            # 这不是隐瞒状态——`status_view()` 里带着 `recovering: True`。
            # 而上一条的 `MASTER_UNLOADED` 不同：那是**够不着**主控，
            # 连快照都取不回来，所以它照旧一律拦下。
            return None, CommandResult.blocked(
                request_id,
                [BlockReason(Reason.MASTER_RECOVERING, "主控恢复流程尚未完成")],
                version,
            )

        # 6 命令依据的图版本。命令可以不带（只读查询常这样），带了就必须对得上。
        if command.topology_version is not None and command.topology_version != version:
            return None, CommandResult.stale(
                request_id,
                [
                    BlockReason(
                        Reason.STALE_VERSION,
                        "命令依据的图版本 %r，当前 %r" % (command.topology_version, version),
                    )
                ],
                version,
            )

        # 7 拓扑判定。原样透传它的 blocked / stale，不在这里改判。
        verdict = self.topology.validate(ref, command.topology_version)
        if not verdict.allowed:
            result = (
                CommandResult.stale(request_id, verdict.reasons, version)
                if verdict.status == STATUS_STALE
                else CommandResult.blocked(request_id, verdict.reasons, version)
            )
            return None, result

        # 8 网络运行状态。只读命令到这里就够：诊断要在网络不能工作时也能看。
        blockers = self.blockers()
        state = run_state_of(blockers)
        if state not in RUNNABLE_STATES:
            if read_only:
                return self._grant(command, None), None
            return None, CommandResult.blocked(request_id, blockers, version)
        if read_only:
            return self._grant(command, None), None

        # 9 型号支持。「拥有主控」不等于「支持全部功能」：
        #   DOS 控制器不认总线，那台设备连得上但用不了。
        target = self.registry.get_by_ref(ref)
        if target is None:
            return None, CommandResult.stale(
                request_id,
                [BlockReason(Reason.DEVICE_GONE, "实例已不存在或代次过期")],
                version,
            )
        served = self.catalog.controller_serves(record.device_id, target.device_id)
        if not served.allowed:
            return None, CommandResult.rejected(
                request_id,
                [
                    BlockReason(
                        Reason.CONTROLLER_DOES_NOT_SERVE,
                        "%s 不由 %s 控制：%s"
                        % (target.device_id, record.device_id, served.detail),
                    )
                ],
                version,
            )

        # 10 装配档位。放在带宽之前是有意的：正式档下带宽数值本身来自
        #    未定案的策略，先报「没带宽」等于把替身数值当成了正式结果。
        if self.profile == AssemblyProfile.PRODUCTION and self.policy is not None:
            unresolved = self.policy.unresolved_report()
            if unresolved:
                return None, CommandResult.blocked(
                    request_id,
                    [
                        BlockReason(
                            Reason.ASSEMBLY_UNRESOLVED,
                            "正式装配档下准入策略仍有 %d 项未定案，数值不可信"
                            % (len(unresolved),),
                        )
                    ],
                    version,
                )

        # 11 设备带宽。三态：未获额度、待确认都**不放行**。
        bandwidth = self.admission.status_of(ref)
        if bandwidth.ok is not True:
            reasons = list(bandwidth.reasons)
            if bandwidth.state == BandwidthState.UNKNOWN:
                reasons.append(
                    BlockReason(
                        Reason.BANDWIDTH_UNKNOWN,
                        "带宽判定尚未确认；未确认不等于可以",
                    )
                )
            elif not reasons:
                reasons.append(
                    BlockReason(
                        Reason.BANDWIDTH_OVERLOADED,
                        "带宽未放行（恰好满载时的口径见 R13，当前策略不予放行）",
                    )
                )
            return None, CommandResult.blocked(request_id, reasons, version)

        # 12 本命令所需的服务。替身在开发档下就是「尚未实现」，
        #    如实回报而不是让它返回一份看起来正常的空库存。
        blocker = route_blocker(self.runtime.services, command.kind)
        if blocker is not None:
            return None, CommandResult.blocked(request_id, [blocker], version)
        return self._grant(command, route_of(command.kind)[0]), None

    def _grant(self, command, service_id):
        # type: (DeviceCommand, str) -> AccessGrant
        return AccessGrant(
            command.request_id,
            self.instance_id,
            self.topology.version,
            self.tick_count,
            service_id,
        )

    def _replayed(self, command, key):
        # type: (DeviceCommand, tuple) -> CommandResult
        stored = self.ledger.replay(key)
        if stored is None:
            # 单线程下不该发生；真发生了也不能凭空造一个结果出来。
            self.ledger.abort(key)
            return CommandResult.rejected(
                command.request_id,
                [BlockReason(Reason.STALE_INSTANCE, "去重记录已失效，请重新发起")],
                self.topology.version,
            )
        self.ledger.stats["replayed"] += 1
        payload = dict(stored.payload)
        payload["replayed"] = True
        reasons = tuple(stored.reasons) + (
            BlockReason(Reason.DUPLICATE_REQUEST, "同一条请求已处理过，此处为重放结果"),
        )
        return CommandResult(
            command.request_id, stored.status, payload, reasons, self.topology.version
        )

    # -- 执行 ----------------------------------------------------------

    def submit(self, command):
        # type: (DeviceCommand) -> CommandResult
        """准入 → 取服务 → 执行 → 记账。通知由服务在提交后自行发出。

        业务抛错时清掉在途标记并**向上抛**：那是实现缺陷，不该被伪装成
        一条业务失败。需要「不中断调用方」的入口（引擎事件路径）由
        `ApplicationService.submit` 负责包住。
        """
        self.stats["submitted"] += 1
        grant, failure = self.admit(command)
        if failure is not None:
            self._note_failure(failure)
            return failure

        if command.kind == CommandKind.STATUS:
            self.stats["admitted"] += 1
            return CommandResult.ok(command.request_id, self.status_view(), self.topology.version)
        if command.kind == CommandKind.RECOVERY:
            self.stats["admitted"] += 1
            return self._recovery(command)

        key = ledger_key(command)
        service = self.runtime.service(grant.service_id, grant)
        try:
            result = service.handle(grant, command)
        except Exception:
            self.ledger.abort(key)
            raise
        if not isinstance(result, CommandResult):
            self.ledger.abort(key)
            raise TypeError(
                "服务 %s 的 handle 必须返回 CommandResult，得到 %r"
                % (grant.service_id, result)
            )
        self.ledger.finish(key, result, self.tick_count)
        self.stats["admitted"] += 1
        return result

    def _recovery(self, command):
        # type: (DeviceCommand) -> CommandResult
        """必要的恢复管理（03 验收末条）。

        **只读**：当前只支持 `{"op": "report"}`，用于在主控尚未恢复时
        取出诊断。任何会改库存或推进订单的动作都不在这里——那会变成
        「用调试入口绕过运行条件」。
        """
        op = command.payload.get("op")
        if op == "report":
            return CommandResult.ok(command.request_id, self.diagnostics(), self.topology.version)
        return CommandResult.rejected(
            command.request_id,
            [
                BlockReason(
                    Reason.UNSUPPORTED,
                    "恢复管理当前只支持 op=report（只读诊断），得到 %r" % (op,),
                )
            ],
            self.topology.version,
        )

    def _note_failure(self, result):
        # type: (CommandResult) -> None
        if result.status == STATUS_BLOCKED:
            self.stats["blocked"] += 1
        elif result.status == STATUS_STALE:
            self.stats["stale"] += 1
        elif result.status != STATUS_IN_FLIGHT:
            self.stats["rejected"] += 1

    # -- 生命周期 ------------------------------------------------------

    def advance_tick(self):
        # type: () -> int
        """推进本主控的时钟并清理过期去重记录。由运行时每帧调用。"""
        self.tick_count += 1
        return self.ledger.prune(self.tick_count)

    def save_node_data(self):
        # type: () -> dict
        """主控的持久数据：身份 + 去重账本。

        账本要不要落盘由任务 12 决定（掉落期间重发是否算重复，属于
        12-03 的范围）；这里先把结构定下来，**本次不接线**。
        """
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "instance_id": self.instance_id,
            "master": self.ref.to_dict(),
            "device_id": self.device_id,
            "topology_version": self.topology.version,
            "ledger": self.ledger.export_state(),
        }

    def diagnostics(self):
        # type: () -> dict
        blockers = self.blockers()
        state = self.state()
        return {
            "instance_id": self.instance_id,
            "component_id": self.component_id,
            "device_id": self.device_id,
            "ref": self.ref.to_dict(),
            "run_state": state,
            "runnable": state in RUNNABLE_STATES,
            "recovering": self._recovering,
            "topology_version": self.topology.version,
            "blockers": [reason.to_dict() for reason in blockers],
            "ledger": self.ledger.diagnostics(),
            "stats": dict(self.stats),
        }

    def __repr__(self):
        return "MasterController(%s, %s)" % (self.instance_id, self.state())
