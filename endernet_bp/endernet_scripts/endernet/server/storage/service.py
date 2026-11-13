# coding=utf-8
"""每网络一份的存储服务：介质挂载、库存汇总、预留与受控读写。

对应任务 05-04 至 05-07 的网络侧，以及 05-09（供 11 消费的版本化查询面）。
参考 AE2 的 `me/service/StorageService.java` 与 `me/storage/NetworkStorage.java`。

**权威与派生的分工（架构 §3）。** 介质的库存归介质对象，玩家背包里的物品
归容器适配器，这里维护的 `item_totals` / `item_sources` 是**派生索引**：
卸载只解除该介质的贡献，不清空介质；`rebuild_index()` 随时可以从介质与
有效预留台账把它整份重建出来。索引不是第二份权威库存。

**候选顺序是稳定的。** 选盘一律按介质稳定 ID 排序，不用 set 的遍历顺序——
`set` 的顺序随哈希变化，同一份库存两次查询可能落到不同的盘上，
而那种不确定性在离线测试里几乎不会自己暴露。

数据结构逐条对应 `plans/technical_notes/terminal_item_index.md` §2 的契约，
名字不改：`item_totals[key] = [stored, reserved]`、`item_sources[key]`、
`mounted[medium_id]`、`dirty_items`。

**索引是全量的，物品视图是它的投影——两者不要混为一谈。** 介质里同时装物品
与流体，`item_totals` / `item_sources` / 额度与预留都建立在**全量**之上，
`rebuild_index()` 与 `audit()` 也按全量校验，因此它们含流体键。而
`dirty_items` 与 `take_dirty_keys()`/`publish_version` 服务的**物品视图**
（终端首版不显示流体，`11. 终端与客户端同步.md`）——凡改动索引的地方
一律经 `_mark_dirty()` 落标记，它会把流体挡在外面。详见 `_mark_dirty`。
"""

from ...common.protocol import (
    BlockReason,
    CommandResult,
    Reason,
    TransferReceipt,
    TransferStatus,
)
from ...common.resources import ResourceKey, item_key_from_dict
from ..crafting.terminals import TerminalActions
from ..devices.bus_io import (
    FILTER_MODES,
    MODE_WHITELIST,
    ROLE_FILTER_MODES,
    PASSIVE,
    TEMPLATE,
    PortConfig,
    PortDispatcher,
    PortGateway,
    PortRunner,
    is_port_device,
    normalize_item_config,
    port_blocker,
)
from ..devices import DRIVER_DEVICE_ID
from ..network import NetworkService, RequestLedger, ServiceId
from .medium import (
    DIRECTION_READ,
    DIRECTION_WRITE,
    MediumRecovery,
    TransferBudget,
)
from .transfer import (
    NetworkEndpoint,
    TransferCoordinator,
    TransferPolicy,
    receipt_result,
)


try:
    INTEGER_TYPES = (int, long)
except NameError:
    INTEGER_TYPES = (int,)

try:  # pragma: no cover - 仅在 Python 2 下成立
    _TEXT_TYPES = (str, unicode)
except NameError:  # pragma: no cover - 仅在 Python 3 下成立
    _TEXT_TYPES = (str,)


#: `NetworkNode.has_capability` 用的能力名。与 `devices/capabilities.py` 一致。
STORAGE_PROVIDER_CAPABILITY = "storage_provider"

#: 网络级台账的存档结构版本（12-04）。**与节点记录分开**：预留与去重账本
#: 是网络级的，写进某个节点的记录会在多驱动器网络里存成互相不一致的多份。
NETWORK_STATE_SCHEMA_VERSION = 1

#: 网络级台账的存档记录类型。主键是主控的**位置文本**（`node_key_text`）。
#: 位置稳定而主控实例代次不持久，因此按位置找得回「这个位置曾经有一条网络
#: 台账」；而同位置重放的新主控**不会继承旧身份**——它读到的是一份待复核
#: 记录，不是可以续跑的状态（12 验收）。
NETWORK_RECORD_KIND = "endernet.network_state"


class MountRecord(object):
    """一块介质当前挂在哪。挂载代次用来识别「旧回调」。"""

    __slots__ = (
        "medium_id",
        "node_key",
        "device_ref",
        "slot",
        "slot_version",
        "mount_generation",
        "mount_tick",
    )

    def __init__(self, medium_id, node_key, device_ref, slot, slot_version, mount_tick=0):
        # type: (str, object, object, int, int, int) -> None
        self.medium_id = medium_id
        self.node_key = node_key
        self.device_ref = device_ref
        self.slot = int(slot)
        self.slot_version = int(slot_version)
        #: 挂载代次 = 设备实例代次。同位置换方块后旧代次失效。
        self.mount_generation = device_ref.generation
        self.mount_tick = int(mount_tick)

    def to_dict(self):
        # type: () -> dict
        return {
            "medium_id": self.medium_id,
            "node_key": self.node_key.to_dict(),
            "generation": self.mount_generation,
            "slot": self.slot,
            "slot_version": self.slot_version,
            "mount_tick": self.mount_tick,
        }

    def __repr__(self):
        return "MountRecord(%s @ %r 槽%d)" % (self.medium_id, self.node_key, self.slot)


class Reservation(object):
    """一份预留凭据。

    `parts` 记录「这份预留压在哪几块盘上」，因此某块盘离线时能精确知道
    要撤销哪些预留，而不必把所有预留都推倒重来。
    """

    __slots__ = (
        "reservation_id",
        "owner_id",
        "key",
        "count",
        "parts",
        "tick",
        "active",
    )

    def __init__(self, reservation_id, owner_id, key, count, parts, tick=0):
        # type: (str, str, ResourceKey, int, tuple, int) -> None
        self.reservation_id = reservation_id
        self.owner_id = owner_id
        self.key = key
        self.count = int(count)
        self.parts = tuple(parts)
        self.tick = int(tick)
        self.active = True

    def mediums(self):
        # type: () -> tuple
        return tuple(medium_id for medium_id, _ in self.parts)

    def to_dict(self):
        # type: () -> dict
        return {
            "reservation_id": self.reservation_id,
            "owner_id": self.owner_id,
            "key": self.key.to_dict(),
            "count": self.count,
            "parts": [
                {"medium_id": medium_id, "count": count} for medium_id, count in self.parts
            ],
            "tick": self.tick,
        }

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> Reservation
        """`to_dict` 的逆。活预留仅允许绑定到已恢复的订单。

        「复活一份没有主的预留」会让库存被一条已经跑完（或已经作废）的订单
        永久锁住（见 `StorageService.import_network_state`）。
        """
        parts = tuple(
            (item["medium_id"], int(item["count"]))
            for item in data.get("parts", ())
        )
        return cls(
            data["reservation_id"],
            data.get("owner_id"),
            ResourceKey.from_dict(data["key"]),
            data["count"],
            parts,
            data.get("tick", 0),
        )

    def __repr__(self):
        return "Reservation(%s, %s x%d)" % (self.reservation_id, self.owner_id, self.count)


class StorageService(NetworkService):
    """存储服务。每个网络运行时各有一份实例。"""

    service_id = ServiceId.STORAGE
    #: 本轮不声明对调度服务的依赖：发布窗口（11）落地后才会真正用到它，
    #: 而现在声明一个用不到的依赖会悄悄改变所有注册表的装配顺序。
    requires = ()
    is_stand_in = False
    pending_task = ""

    def __init__(
        self,
        runtime=None,
        drivers=None,
        policy=None,
        ledger_policy=None,
        ports=None,
        transfer_policy=None,
        reflow_sink=None,
        inventories=None,
        workbench=None,
        recipes=None,
        crafting_policy=None,
        grid_stack_limit=None,
        templates=None,
        template_policy=None,
    ):
        # type: (object, object, object, object, object, object, object, object,
        #        object, object, object, object, object, object) -> None
        #: 驱动器状态集合与介质权威表，由启动层注入（架构 §3）。
        #: **裸构造是合法的**（`default_stand_ins()` 会用 `None` 构造每个槽位），
        #: 此时 `is_ready()` 为假，一切业务给 `SERVICE_NOT_READY`——
        #: 那是「装了但还不能用」，不是「没有需求」。
        self.drivers = drivers
        self.policy = policy
        self.runtime = runtime
        #: 接口状态集合（06），同样由启动层注入。**与 `drivers` 同一地位**：
        #: 接口缓冲里的东西不能因为网络断开或运行时重建而消失。
        self.ports = ports
        #: 转移的未定案旋钮（R04/R05/R12）。
        self.transfer_policy = (
            transfer_policy if transfer_policy is not None else TransferPolicy()
        )
        #: 合格回流入口（任务 10）。未接线时为 None，诊断里会写明
        #: 「外部输入通知无处可去」——那是「还没接」，不是「不用发」。
        self.reflow_sink = reflow_sink
        #: 每网络一份的转移协调器。**惰性构造**：接口跑起来之前它不该存在，
        #: 否则每个网络都会白拿一份账本与待核对台账。
        self._transfer = None
        #: 背包提供者（06-09）。生产实现是 `adapters/containers/engine.py`
        #: 的 `EngineInventoryProvider`，离线测试注入替身。
        self.inventories = inventories
        #: 终端交互用例（背包、槽位、光标、合成格、样板终端）与它们的状态。
        #: 命令仍从本服务的 `handle` 进来，按动作表分派过去。
        self.terminal = TerminalActions(
            self,
            workbench=workbench,
            recipes=recipes,
            crafting_policy=crafting_policy,
            grid_stack_limit=grid_stack_limit,
            templates=templates,
            template_policy=template_policy,
        )
        #: 分派表（`_handler_table`），第一次 `handle` 时建。
        self._handlers = None
        #: 接口的轮询驱动。同样惰性构造（需要运行时里的调度服务）。
        self._ports_runner = None
        #: 接口的命令处理器。每网络一份，与协调器同源。
        self._port_dispatcher = None
        # 轮询注册表**只有一份**：`PortRunner._registered`。
        # 服务这边再存一份的代价是两张表要靠调用顺序保持一致，
        # 而任何一处绕过服务直接调 runner 都会让它们分叉。

        # -- 派生索引（权威在介质与预留台账）--
        #: `ResourceKey -> [stored, reserved]`
        self.item_totals = {}
        #: `ResourceKey -> set(medium_id)`
        self.item_sources = {}
        #: 待发布的**物品**键（流体不入表）。同一窗口内去重。
        #: 只经 `_mark_dirty()` 写入，别处不要直接 add。
        self.dirty_items = set()
        #: `medium_id -> MountRecord`
        self.mounted = {}
        #: 存储提交版本：每次实际改动的提交都推进。服务于交易验证。
        self.index_version = 0
        #: 物品视图发布版本：只在真的取走一批脏项时推进。服务于 11 的同步。
        #: **两者不能互相替代**（`terminal_item_index.md` §6）。
        self.publish_version = 0

        self._reservations = {}
        #: `medium_id -> {ResourceKey: count}`，由 `parts` 派生。
        self._medium_reserved = {}
        self._reservation_seq = 0
        #: 存档里带回来、但**没有复活**的预留（12-04）。它们不属于任何在跑的
        #: 订单，复活等于把库存锁给一条永远不会再动的订单。
        self._archived_reservations = []
        self._clock_tick = 0
        self._ledger = RequestLedger(ledger_policy)
        self.stats = {
            "mounts": 0,
            "unmounts": 0,
            "rebuilds": 0,
            "inserts": 0,
            "extracts": 0,
            "reserves": 0,
            "released": 0,
            "revoked": 0,
            "requests": 0,
            "refused": 0,
        }
        #: 最近一次参数错误或配方来源异常的类型与消息。玩家只看到通用说明，
        #: 排查看这里。
        self.last_error = ""

    # -- NetworkService 入口 -------------------------------------------

    def attach(self, runtime):
        # type: (object) -> None
        self.runtime = runtime

    def detach(self):
        # type: () -> None
        self.runtime = None

    def is_ready(self):
        # type: () -> bool
        return self.drivers is not None

    # -- 转移与接口 ----------------------------------------------------

    def transfer(self):
        # type: () -> TransferCoordinator
        """本网络的转移协调器。**惰性构造**，每网络一份。

        它属于存储服务而不是独立服务槽位：AE2 那边也是「网络存取入口」
        （`IStorageMonitorableAccessor`）由存储暴露，而不是另开一个服务。
        少一个槽位意味着装配表、替身清单与 `assert_profile_ok` 都不用动。
        """
        if self._transfer is None:
            self._transfer = TransferCoordinator(
                policy=self.transfer_policy,
                sink=self.reflow_sink,
                network_id=(
                    self.runtime.component_id if self.runtime is not None else ""
                ),
            )
        return self._transfer

    def ports_runner(self):
        # type: () -> PortRunner
        """接口的轮询驱动。惰性构造——需要运行时里已经装好的调度服务。"""
        if self._ports_runner is None:
            gateway = PortGateway(self.runtime)
            self._ports_runner = PortRunner(
                self.ports, gateway, coordinator=self.transfer()
            )
        return self._ports_runner

    def ports_runner_view(self):
        # type: () -> dict
        """接口驱动的只读诊断。**公开访问器**：启动层的诊断走它，
        不伸手读 `_ports_runner`（那会让任何没有这个私有名的实现把整块
        诊断打崩，而诊断正是网络坏掉时最该能答的东西）。

        `wired=False` 与 `registered=0` 是两件事：前者说「驱动还没建起来」，
        后者说「建起来了但没有接口注册」。混在一起就查不出
        「接口为什么不动」。
        """
        if self.ports is None:
            return {"wired": False, "reason": "本网络没有接入接口状态集合"}
        if self._ports_runner is None:
            return {"wired": False, "registered": 0,
                    "reason": "尚未构造：本网络的接口还没被登记过"}
        view = self._ports_runner.diagnostics()
        view["wired"] = True
        return view

    def _scheduler(self):
        # type: () -> object
        """本网络的调度服务。没有装配时返回 None（**不报错**）：
        接口只是不会被轮询，注册表与状态查询照常。"""
        if self.runtime is None:
            return None
        return self.runtime.services.get(ServiceId.SCHEDULER)

    def _attach_port(self, node):
        # type: (object) -> bool
        """登记一台接口：建立专属状态、注册轮询。**幂等**。

        `add_node` 对每个成员反复调用，因此这里必须自己保证「只注册一次」——
        `SchedulerService.set_poll_handler` 每调用一次就把退避打回初值，
        在里面直接注册会让退避永远失效（表现为「每帧都在轮询」）。
        """
        if self.ports is None or not is_port_device(node.device_id):
            return False
        # 朝向以 `NetworkNode` 上的读数为准：它来自注册表，而注册表在
        # 放置与补查两条路径上都会解析朝向（`lifecycle._resolve_facing`）。
        state = self.ports.ensure(node.ref, node.device_id, node.facing)
        if state is None:
            return False
        scheduler = self._scheduler()
        if scheduler is None:
            return False
        return self.ports_runner().attach(node, scheduler)

    def _detach_port(self, node):
        # type: (object) -> bool
        """注销一台接口的轮询。**缓冲与配置保留**（06 验收）。"""
        if self._ports_runner is None:
            return False
        scheduler = self._scheduler()
        if scheduler is None:
            return False
        return self._ports_runner.detach(node, scheduler)

    def add_node(self, node, saved_data=None):
        # type: (object, object) -> bool
        """拓扑发布时登记成员；驱动器的槽位读数变化时也用此入口更新挂载。

        两种成员：存储驱动器（挂载介质）与接口设备（登记轮询）。
        驱动器的挂载集合来自它自己的权威槽位，因此这里不解析 `saved_data`
        决定装了什么——那会让「存档说什么」盖过「设备上真的有什么」。
        """
        if is_port_device(node.device_id):
            return self._attach_port(node)
        if self.drivers is None or node.device_id != DRIVER_DEVICE_ID:
            return False
        if not node.has_capability(STORAGE_PROVIDER_CAPABILITY):
            return False

        # `saved_data` 刻意**不用**来决定装了什么：挂载集合的唯一依据是
        # 驱动器自己的权威槽位。让存档盖过设备上真实有什么，正是
        # 「缓存变成第二份权威库存」的经典形态。恢复载荷只用于校验。
        state = self.drivers.ensure(node.ref)
        if state is None:
            return False
        before = self._mount_signature()
        # **先卸掉权威槽位里已经没有的**：这就是「对最终挂载集合求差」。
        # 没有这一步的话，任何一处直接调 `provider.unmount()` 而不经过本服务
        # 的路径都会让索引与设备悄悄不一致——而那种不一致只会表现为
        # 「库存比设备上多」，查起来毫无线索。
        admitted = self._driver_admitted(node.ref)
        assignments = state.occupied() if admitted else ()
        authoritative = set(item.medium_id for item in assignments)
        for medium_id in sorted(self.mounted):
            record = self.mounted[medium_id]
            if record.node_key == node.node_key and medium_id not in authoritative:
                reason = "权威槽位里已没有它" if admitted else "驱动器失去带宽准入"
                self.unmount_medium(medium_id, reason=reason)

        for assignment in assignments:
            existing = self.mounted.get(assignment.medium_id)
            if (
                existing is not None
                and existing.node_key == node.node_key
                and (
                    existing.slot != assignment.slot
                    or existing.mount_generation != node.ref.generation
                )
            ):
                # 同一台驱动器上的槽位移动（或同位置换了实例）：先卸后挂。
                # **只处理本节点内的移动**——跨节点的「同一块盘」是异常，
                # 交给 mount_medium 明确拒绝。
                self.unmount_medium(assignment.medium_id, reason="槽位或实例变化")
            self.mount_medium(
                assignment.medium_id,
                node.node_key,
                assignment.slot,
                device_ref=node.ref,
                slot_version=assignment.slot_version,
                tick=self._clock_tick,
            )
        # 幂等重挂不计为成员变更。
        return self._mount_signature() != before

    def _mount_signature(self):
        # type: () -> tuple
        """挂载集合的指纹，用来判断一次同步有没有真的改动什么。"""
        return tuple(
            sorted(
                (record.medium_id, record.node_key.as_key(), record.slot, record.mount_generation)
                for record in self.mounted.values()
            )
        )

    def remove_node(self, node):
        # type: (object) -> bool
        """节点离开网络：解除它贡献的挂载。**不等于世界里的方块被拆了。**

        接口设备走另一条：注销轮询、撤销任务，**保留缓冲与配置**——
        06 验收要求「外部物流已送入缓冲的资源仍保留」，而拆线、失去主控、
        运行时重建都不该让那些东西消失。
        """
        if is_port_device(node.device_id):
            return self._detach_port(node)
        affected = [
            medium_id
            for medium_id, record in self.mounted.items()
            if record.node_key == node.node_key
        ]
        changed = False
        for medium_id in sorted(affected):
            ok, _, _ = self.unmount_medium(medium_id, reason="节点离开网络")
            if ok:
                changed = True
        return changed

    def note_domain_change(self, node, from_domain_ids, to_domain_ids):
        # type: (object, tuple, tuple) -> bool
        """**有意不处理。**

        介质额度按介质记账，与带宽线路无关；驱动器负载变化由
        `note_device_load_changed` 只重算所在线路，线路归属变化本身由
        `AdmissionService` 在同一次拓扑发布里处理。因此这里恒为 False。
        （这与 `NetworkService.note_domain_change` 文档里「05 的存储会覆盖」
        那句括注不一致，是有意的，理由记在交付说明里。）
        """
        return False

    def on_wake(self, node_key):
        # type: (object) -> bool
        """方块实体 tick 到达。介质没有被动推进，因此只转发给接口。

        **接口这边也只记一笔**：退避的归属是调度服务，搬运的发起是轮询。
        唤醒在这里不产生任何业务副作用（与 `NetworkService.on_wake` 的契约一致）。
        """
        if self._ports_runner is None or self.ports is None:
            return False
        return self._ports_runner.note_wake(node_key)

    def tick(self, context, budget):
        # type: (dict, int) -> int
        """推进时钟、账本清理与接口时基。**不在这里做任何库存全量扫描。**

        发布窗口属任务 11：本轮只维护准确的索引与脏集合，无人订阅时
        `take_dirty_keys()` 不会被调用，因此这里的工作量恒为常数。

        接口的搬运**不在这里发起**：它挂在调度服务的轮询上，而轮询只在
        运行时开放时才会被推进。这里只推一个时基给批次时间戳用。
        """
        context = context or {}
        tick = context.get("tick")
        if tick is not None:
            self.advance_clock(int(tick))
        self._ledger.prune(self._clock_tick)
        if self.ports is not None:
            self.ports.advance_clock(self._clock_tick)
        if self._transfer is not None:
            self._transfer.tick(self._clock_tick)
        return 0

    def save_node_data(self, node):
        # type: (object) -> object
        """**本服务在该节点上没有独立的持久数据**，因此返回 None。

        - 介质与槽位的权威在 `StorageDriverStore`，它有自己的记录；
        - 汇总索引是派生量，恢复时由 `rebuild_index()` 重建；
        - 预留台账是**网络级**的，而本入口是按节点的（写进节点记录里），
          按节点存会在多驱动器网络里存成互相不一致的多份。

        任务 12 需要为网络级数据（预留、去重账本）另开一条记录；
        在此之前预留不跨重启保留——它们本来就属于在途订单。
        这里**不返回一份看起来像存档的空字典**：那会让恢复流程以为有东西要读。
        """
        return None

    # -- 网络级台账（12-04）--------------------------------------------

    def export_network_state(self):
        # type: () -> dict
        """导出**网络级**的存档载荷：预留、待核对转移、预留发号水位。

        与 `save_node_data` 是两条路：那个是「节点私有数据」，按节点存；
        这个是「整张网络共一份的数据」，按网络存。混在一起会让预留台账
        在多驱动器网络里存成互相不一致的多份——那正是这里单开一条的理由。

        **去重账本不在这里**：它是「本进程内这次请求有没有做过」的短期记忆
        （保留期 900 帧），跨重启复活一份旧账本只会让新请求被当成重放。
        """
        return {
            "schema_version": NETWORK_STATE_SCHEMA_VERSION,
            "network_id": self._network_id(),
            "reservation_seq": self._reservation_seq,
            "reservations": [
                item.to_dict() for item in self._ordered_reservations() if item.active
            ],
            "pending_transfers": (
                self._transfer.export_pending() if self._transfer is not None else []
            ),
        }

    def import_network_state(self, payload, restored_owners=()):
        # type: (dict) -> dict
        """读回网络级台账。返回处置统计。

        只有 `restored_owners` 中已恢复的订单才能恢复活预留，其余预留
        **记进诊断等人工处置**，避免无主预留永久锁住库存。并把
        `reservation_seq` 水位抬到已见过的最大号之上，避免新预留撞号。

        **待核对转移照常恢复**，但**只恢复成「待核对」，绝不重放**——
        那正是 12-07 的要求：可能成功但未能确认的记录必须停住等人查。
        """
        stats = {
            "reservations_archived": 0,
            "reservations_restored": 0,
            "pending_restored": 0,
            "reservation_seq": self._reservation_seq,
        }
        if not isinstance(payload, dict):
            return stats
        for raw in payload.get("reservations", ()) or ():
            try:
                reservation = Reservation.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                continue
            if reservation.owner_id in restored_owners:
                if reservation.count <= 0 or any(count <= 0 for _, count in reservation.parts):
                    raise ValueError("invalid restored reservation count")
                if sum(count for _, count in reservation.parts) != reservation.count:
                    raise ValueError("restored reservation parts mismatch")
                existing = self._reservations.get(reservation.reservation_id)
                if existing is not None and existing.to_dict() != reservation.to_dict():
                    raise ValueError("restored reservation identity conflict")
                if existing is None:
                    self._reservations[reservation.reservation_id] = reservation
                    self._note_reservation_parts(reservation)
                    self._set_reserved(reservation.key, self._reserved_of(reservation.key))
                    stats["reservations_restored"] += 1
            else:
                self._archived_reservations.append(reservation.to_dict())
                stats["reservations_archived"] += 1
            # 水位抬高：不复活但也不撞号。号里的序号是可以复用的边界，
            # 撞号会让「同一 owner 的两份预留」共用 ID。
            sequence = self._reservation_sequence(reservation.reservation_id)
            if sequence > self._reservation_seq:
                self._reservation_seq = sequence
        if self._transfer is not None:
            stats["pending_restored"] = self._transfer.import_pending(
                payload.get("pending_transfers", ())
            )
        stats["reservation_seq"] = self._reservation_seq
        return stats

    def _reservation_sequence(self, reservation_id):
        # type: (str) -> int
        """从 `"<owner>:<n>"` 里取序号。读不出返回 0。

        **必须同时认 `str` 与 `unicode`**：存档里的字符串是
        `json.dumps(ensure_ascii=True)` 写出去的，`json.loads` 在 py2 里
        读回来的是 `unicode`——只判 `str` 的实现会在这里**静默返回 0**，
        于是水位永远抬不起来，重进世界后的新预留与旧预留撞号。
        （真机实测撞到过：`archived_reservations` 有记录，而水位是 0。）
        """
        if not isinstance(reservation_id, _TEXT_TYPES):
            return 0
        _, _, tail = reservation_id.rpartition(":")
        try:
            return int(tail)
        except (TypeError, ValueError):
            return 0

    def archived_reservations(self):
        # type: () -> list
        """存档里带回来、但没有复活的预留（12-04 的待复核记录）。"""
        return [dict(item) for item in self._archived_reservations]

    def status(self):
        # type: () -> dict
        return {
            "service_id": self.service_id,
            "available": self.is_ready(),
            "stand_in": False,
            "ready": self.is_ready(),
            "mediums": len(self.mounted),
            "keys": len(self.item_totals),
            "index_version": self.index_version,
            "publish_version": self.publish_version,
        }

    def diagnostics(self):
        # type: () -> dict
        report = NetworkService.diagnostics(self)
        report.update(
            {
                "mounted": sorted(self.mounted),
                "keys": len(self.item_totals),
                "dirty": len(self.dirty_items),
                "reservations": len(self._reservations),
                #: 存档带回来、没有复活的预留（12-04）。**非空不等于故障**：
                #: 它说明上一次退出时还有在途订单压着这些资源，等人工处置。
                "archived_reservations": len(self._archived_reservations),
                "index_version": self.index_version,
                "publish_version": self.publish_version,
                "clock_tick": self._clock_tick,
                "ledger": self._ledger.stats,
                "last_error": self.last_error,
                # 接口那半边的内容在 `bootstrap.io_diagnostics()` 里，这里不抄
                # 一份——抄一份的代价是两处迟早对不上（同 `energy_diagnostics`
                # 把逐设备明细留在 `status_view()` 的理由）。
                "ports": (
                    len(self._ports_runner._registered)
                    if self._ports_runner is not None else 0
                ),
                "transfer": (
                    self._transfer.diagnostics() if self._transfer is not None
                    else {"wired": False,
                          "note": "本网络还没有发起过转移，协调器尚未构造"}
                ),
                "scope_note": (
                    "介质挂载、汇总索引、预留与受控读写已实现；"
                    "**真实物品交接（06）已接入**——接口设备与转移协调器都挂在"
                    "本服务下，端点预检、受控提交、中转缓冲与实际数量回执均已落地。"
                    "但**接触真实引擎的那一段只有部分游戏内验证**"
                    "（详见 06-交付说明第五节）；存档后端（12）与界面同步（11）"
                    "已接入。"
                ),
            }
        )
        return report

    # -- 时钟 ----------------------------------------------------------

    def advance_clock(self, tick):
        # type: (int) -> int
        """推进额度时基。**这是唯一的时基来源**：不用方块 tick，不用墙上时间。"""
        self._clock_tick = int(tick)
        return self._clock_tick

    def _now(self):
        # type: () -> int
        return self._clock_tick

    # -- 挂载 ----------------------------------------------------------

    def mount_medium(
        self, medium_id, node_key, slot, device_ref, slot_version=0, tick=None
    ):
        # type: (str, object, int, object, int, int) -> tuple
        """把一块介质挂到本网络。返回 `(ok, reason, detail)`。

        幂等：**同一位置、同一实例代次**重复调用是空操作（`sync_members`
        每帧都会对每个成员调 `add_node`）。同一块盘已经挂在**别处**时
        **明确拒绝**而不是抢占——抢占会让「同 ID 双挂载」这条异常悄无声息地
        被修好，库存也说不清归谁。同位置的槽位移动由 `add_node` 先卸后挂处理。

        `device_ref` 是必填的：挂载代次就是设备实例代次，没有它就无法分辨
        「同一块盘还挂在原地」与「新实例占了这个位置」。
        """
        if self.drivers is None:
            return False, Reason.SERVICE_NOT_READY, "存储服务未接入驱动器"
        if device_ref is None:
            raise ValueError("mount_medium 需要设备实例引用（挂载代次由它决定）")
        if not self._driver_admitted(device_ref):
            status = self.runtime.admission.status_of(device_ref)
            reason = Reason.BANDWIDTH_UNKNOWN if status.ok is None else Reason.BANDWIDTH_DENIED
            return False, reason, "驱动器尚未获得带宽准入"

        existing = self.mounted.get(medium_id)
        if existing is not None:
            if (
                existing.node_key == node_key
                and existing.slot == slot
                and existing.mount_generation == device_ref.generation
            ):
                return True, "", "already mounted"
            return (
                False,
                Reason.MEDIUM_DUPLICATE_MOUNT,
                "介质 %s 已挂在 %r 槽%d" % (medium_id, existing.node_key, existing.slot),
            )

        medium = self.drivers.medium(medium_id)
        if medium is None:
            return False, Reason.MEDIUM_NOT_MOUNTED, "没有介质 %s 的记录" % (medium_id,)

        record = MountRecord(
            medium_id,
            node_key,
            device_ref,
            slot,
            slot_version,
            tick if tick is not None else self._clock_tick,
        )
        self.mounted[medium_id] = record
        self._apply_contribution(medium, 1)
        self.stats["mounts"] += 1
        self.index_version += 1
        return True, "", ""

    def unmount_medium(self, medium_id, reason=""):
        # type: (str, str) -> tuple
        """解除一块介质的贡献。返回 `(ok, reason, detail)`。

        顺序是**定死的**：先拒绝该挂载代次的新提交（本方法的第一步就是
        把它从 `mounted` 里摘掉），再撤销受影响预留，最后扣贡献。
        反过来的话，扣完贡献会短暂出现 `reserved > stored`。
        """
        record = self.mounted.pop(medium_id, None)
        if record is None:
            return False, Reason.MEDIUM_NOT_MOUNTED, "介质 %s 未挂载" % (medium_id,)

        revoked = self.revoke_reservations_of_medium(medium_id, reason or "介质已卸载")

        medium = self.drivers.medium(medium_id) if self.drivers is not None else None
        if medium is not None:
            self._apply_contribution(medium, -1)
        self.stats["unmounts"] += 1
        self.index_version += 1
        return True, "", {"revoked_owners": revoked}

    def medium_ids(self):
        # type: () -> tuple
        return tuple(sorted(self.mounted))

    def mount_record(self, medium_id):
        # type: (str) -> dict
        record = self.mounted.get(medium_id)
        return None if record is None else record.to_dict()

    def medium_view(self, medium_id):
        # type: (str) -> dict
        """介质的只读视图（挂载信息 + 库存概览）。"""
        record = self.mounted.get(medium_id)
        medium = self.drivers.medium(medium_id) if self.drivers is not None else None
        if medium is None:
            return {"medium_id": medium_id, "mounted": False}
        view = medium.mount_view()
        view["mounted"] = record is not None
        view["recovery_text"] = RECOVERY_TEXT.get(medium.recovery, "未知状态")
        if record is not None:
            view["node_key"] = record.node_key.as_key()
            view["slot"] = record.slot
            view["generation"] = record.mount_generation
        return view

    def refresh_mounts(self):
        # type: () -> int
        """按驱动器权威槽位重算挂载集合：新增的挂上、消失的卸下。

        只处理**已经在本网络的**驱动器节点，不遍历无关驱动器。
        """
        if self.drivers is None or self.runtime is None:
            return 0
        wanted = {}
        for node in self.runtime.members():
            if node.device_id != DRIVER_DEVICE_ID or not self._driver_admitted(node.ref):
                continue
            state = self.drivers.get_by_ref(node.ref)
            if state is None:
                continue
            for assignment in state.occupied():
                wanted[assignment.medium_id] = (node, assignment)
        before = self._mount_signature()
        for medium_id in sorted(set(self.mounted) - set(wanted)):
            self.unmount_medium(medium_id, reason="权威槽位里已没有它")
        for medium_id in sorted(wanted):
            node, assignment = wanted[medium_id]
            self.mount_medium(
                medium_id,
                node.node_key,
                assignment.slot,
                device_ref=node.ref,
                slot_version=assignment.slot_version,
            )
        # 返回**指纹变动的条目数**（对称差）。一次槽位移动算两条（旧的没了、
        # 新的来了）——它是诊断读数，不是「发生了几个逻辑动作」。
        return len(set(self._mount_signature()) ^ set(before))

    # -- 汇总索引 ------------------------------------------------------

    def _apply_contribution(self, medium, sign):
        # type: (object, int) -> None
        """按介质贡献增减汇总。**只遍历这一块盘的条目**，与它存了多少件无关。

        来源集合只在「从 0 变正」或「从正变 0」时改动——数量在正数区间内
        变化不改来源集合（`terminal_item_index.md` §3 要求 4）。
        """
        for key, count in medium.stored_view().items():
            entry = self.item_totals.get(key)
            before = entry[0] if entry is not None else 0
            after = before + sign * count
            if after < 0:
                raise AssertionError(
                    "介质 %s 的贡献被重复扣减：%s 由 %d 变成 %d"
                    % (medium.medium_id, key.canonical(), before, after)
                )
            if sign > 0 and count > 0:
                self.item_sources.setdefault(key, set()).add(medium.medium_id)
            elif sign < 0 and count > 0:
                bucket = self.item_sources.get(key)
                if bucket is not None:
                    bucket.discard(medium.medium_id)
                    if not bucket:
                        del self.item_sources[key]
            reserved = entry[1] if entry is not None else 0
            self._set_stored(key, after, reserved)
            self._mark_dirty(key)

    def _mark_dirty(self, key):
        # type: (ResourceKey) -> None
        """脏标记的**唯一入口**：只有物品进物品视图。

        介质里同时装物品与流体（`Medium.stored_view()` 返回全部条目），
        但终端首版不显示流体，而 `take_dirty_keys()` 一旦非空就会推进
        `publish_version`——若不在这里挡一道，**一次纯流体提交也会推进
        物品视图版本**，客户端于是收到一批它根本不显示的键。

        流体将来会有自己的 `view_kind`，到那时按视图分别维护脏集合，
        而不是把流体塞进物品的这一个。
        """
        if key.is_item():
            self.dirty_items.add(key)

    def _set_stored(self, key, stored, reserved):
        # type: (ResourceKey, int, int) -> None
        """写 `item_totals`，并维护「stored 为 0 且无人预留时才移除条目」。"""
        if stored <= 0 and reserved <= 0:
            self.item_totals.pop(key, None)
            return
        if reserved > stored:
            raise AssertionError(
                "%s 的可用量为负：stored=%d reserved=%d" % (key.canonical(), stored, reserved)
            )
        self.item_totals[key] = [stored, reserved]

    def _set_reserved(self, key, reserved):
        # type: (ResourceKey, int) -> None
        entry = self.item_totals.get(key)
        stored = entry[0] if entry is not None else 0
        if reserved > stored:
            raise AssertionError(
                "%s 的预留超过库存：stored=%d reserved=%d"
                % (key.canonical(), stored, reserved)
            )
        if stored <= 0 and reserved <= 0:
            self.item_totals.pop(key, None)
            return
        self.item_totals[key] = [stored, reserved]
        self._mark_dirty(key)

    def rebuild_index(self):
        # type: () -> int
        """从介质与有效预留台账整份重建派生索引。返回重建的条目数。

        **恢复路径与索引修复都走它**，因此不存在「增量更新与重建给出不同结论」
        的可能。构建期间不对外发布，重建完成后才推进版本。
        """
        self.item_totals = {}
        self.item_sources = {}
        self.dirty_items = set()
        for medium_id in sorted(self.mounted):
            medium = self.drivers.medium(medium_id)
            if medium is None:
                continue
            self._apply_contribution(medium, 1)
        self._medium_reserved = {}
        for reservation in self._ordered_reservations():
            self._note_reservation_parts(reservation)
            self._set_reserved(reservation.key, self._reserved_of(reservation.key))
        self.stats["rebuilds"] += 1
        self.index_version += 1
        return len(self.item_totals)

    def keys(self):
        # type: () -> tuple
        """稳定排序的物品键。"""
        return tuple(sorted(self.item_totals, key=lambda item: item.canonical()))

    def query(self, key):
        # type: (ResourceKey) -> dict
        entry = self.item_totals.get(key)
        if entry is None:
            return {
                "key": key.to_dict(),
                "stored": 0,
                "reserved": 0,
                "available": 0,
                "version": self.index_version,
            }
        return {
            "key": key.to_dict(),
            "stored": entry[0],
            "reserved": entry[1],
            "available": entry[0] - entry[1],
            "version": self.index_version,
        }

    def snapshot(self):
        # type: () -> dict
        """不可变快照。**纯数据**：改动它不影响索引，也不含任何内部对象。"""
        return {
            "version": self.index_version,
            "publish_version": self.publish_version,
            "keys": self.keys(),
            "entries": [self.query(key) for key in self.keys()],
            "mediums": [self.medium_view(medium_id) for medium_id in self.medium_ids()],
        }

    def take_dirty_keys(self):
        # type: () -> tuple
        """取出并清空物品视图的脏键。**非空时才推进发布版本**（§6）。

        这一个调用就是发布窗口的原子点：排序、清空、推进 `publish_version`
        在同一条语句序列里完成，主线程 tick 内不插入其他提交，因此窗口
        之后到达的改动必然落到下一个窗口（`base_version` 就是这里推到的值）。
        """
        if not self.dirty_items:
            return ()
        keys = tuple(sorted(self.dirty_items, key=lambda item: item.canonical()))
        self.dirty_items = set()
        self.publish_version += 1
        return keys

    # -- 物品视图的窄读口（供 11 的同步层，勿绕过去直接戳 item_totals）--

    def has_dirty_items(self):
        # type: () -> bool
        """物品视图是否有待发布变化。**不推进版本、不清空集合。**"""
        return bool(self.dirty_items)

    def read_items(self, keys):
        # type: (object) -> tuple
        """按 `keys` 的顺序返回其中**当前仍然存在**的 `(key, stored, reserved)`。

        发布窗口拿到脏键后要取各键的最新值，走这里而不是 `query()`：
        `query()` 对不存在的键返回一份 0/0 的记录，与「存在但当前可用量为
        0」在返回值上分不出来——而这两个结论在线上是 upsert 与 remove 的
        区别。这里**返回的元组里没有的键就是已经消失的**，调用方据此构造
        remove，不必再猜。

        刻意不排序：调用方（`take_dirty_keys()`）给的已经是 canonical 序，
        再排一次是白花时间；顺序即输入顺序。
        """
        out = []
        for key in keys:
            entry = self.item_totals.get(key)
            if entry is None:
                continue
            out.append((key, entry[0], entry[1]))
        return tuple(out)

    def iter_item_records(self):
        # type: () -> tuple
        """`((key, stored, reserved), ...)`，canonical 序，**只含物品键**。

        与 `keys()` 的相对顺序一致（canonical 把 kind 摆在最前，流体因此
        整体排在物品之前或之后，这里只是把流体滤掉，不重排）。返回
        `tuple`，供发布窗口一次性冻结快照用——调用方可以放心持有它，
        不必担心后续提交把它改掉。
        """
        out = []
        for key in self.keys():
            if not key.is_item():
                continue
            entry = self.item_totals[key]
            out.append((key, entry[0], entry[1]))
        return tuple(out)

    def audit(self):
        # type: () -> dict
        """独立全量求和校验。**只给测试与诊断，不在生产 tick 里跑。**"""
        expected_totals = {}
        expected_sources = {}
        for medium_id in sorted(self.mounted):
            medium = self.drivers.medium(medium_id)
            if medium is None:
                continue
            for key, count in medium.stored_view().items():
                expected_totals[key] = expected_totals.get(key, 0) + count
                expected_sources.setdefault(key, set()).add(medium_id)
        problems = []
        for key in set(expected_totals) | set(self.item_totals):
            want = expected_totals.get(key, 0)
            got = self.item_totals.get(key, [0, 0])[0]
            if want != got:
                problems.append(
                    "%s：汇总 %d，独立求和 %d" % (key.canonical(), got, want)
                )
        for key in set(expected_sources) | set(self.item_sources):
            want = expected_sources.get(key, set())
            got = set(self.item_sources.get(key, set()))
            if want != got:
                problems.append(
                    "%s：来源 %r，独立求和 %r" % (key.canonical(), sorted(got), sorted(want))
                )
        for key, entry in self.item_totals.items():
            if entry[1] > entry[0]:
                problems.append("%s：预留 %d 超过库存 %d" % (key.canonical(), entry[1], entry[0]))
        return {
            "ok": not problems,
            "problems": problems,
            "keys": len(expected_totals),
            "mounted": len(self.mounted),
        }

    # -- 预留 ----------------------------------------------------------

    def reserve(self, owner_id, key, count, tick=0):
        # type: (str, ResourceKey, int, int) -> object
        """为指定拥有者预留资源。**要么全留、要么不留**，不拆一半。

        「两个订单不能预留同一份剩余材料」是验收项，因此可用量不足时
        直接返回 None，而不是给一份部分预留——部分预留会让两个订单
        各拿到半份材料，谁也做不成。
        """
        if count <= 0:
            raise ValueError("预留数量必须为正")
        entry = self.item_totals.get(key)
        available = (entry[0] - entry[1]) if entry is not None else 0
        if available < count:
            return None

        parts = self._plan_parts(key, count)
        if sum(item[1] for item in parts) < count:
            return None

        self._reservation_seq += 1
        reservation = Reservation(
            "%s:%d" % (owner_id or "anon", self._reservation_seq),
            owner_id,
            key,
            count,
            parts,
            tick,
        )
        self._reservations[reservation.reservation_id] = reservation
        self._note_reservation_parts(reservation)
        self._set_reserved(key, self._reserved_of(key))
        self.stats["reserves"] += 1
        return reservation

    def release(self, reservation_id, count=None):
        # type: (str, int) -> int
        """释放预留（全部或一部分）。返回实际释放的数量。"""
        reservation = self._reservations.get(reservation_id)
        if reservation is None or not reservation.active:
            return 0
        released = reservation.count if count is None else min(int(count), reservation.count)
        if released <= 0:
            return 0

        reservation.count -= released
        self._rebalance_parts(reservation)
        if reservation.count <= 0:
            self._forget_reservation(reservation)
        self._set_reserved(reservation.key, self._reserved_of(reservation.key))
        self.stats["released"] += released
        return released

    def release_owned_by(self, owner_id):
        # type: (str) -> int
        """释放某个拥有者的全部预留。返回释放的份数。"""
        total = 0
        for reservation in list(self._ordered_reservations()):
            if reservation.owner_id == owner_id:
                total += self.release(reservation.reservation_id)
        return total

    def reservations_of(self, owner_id):
        # type: (str) -> tuple
        return tuple(
            item
            for item in self._ordered_reservations()
            if item.owner_id == owner_id and item.active
        )

    def revoke_reservations_of_medium(self, medium_id, reason=""):
        # type: (str, str) -> tuple
        """撤销所有压在该介质上的预留，返回受影响的拥有者。

        调用方据此通知订单重验——**本服务不实现订单调度**（05 明令）。
        """
        affected = set()
        for reservation in list(self._ordered_reservations()):
            if medium_id not in reservation.mediums():
                continue
            affected.add(reservation.owner_id)
            self._forget_reservation(reservation)
            self._set_reserved(reservation.key, self._reserved_of(reservation.key))
            self.stats["revoked"] += 1
        return tuple(sorted(item for item in affected if item))

    def _ordered_reservations(self):
        # type: () -> list
        """按预留 ID 稳定排序。遍历顺序不随字典哈希变化。"""
        return [self._reservations[key] for key in sorted(self._reservations)]

    def _plan_parts(self, key, count):
        # type: (ResourceKey, int) -> tuple
        """挑出承担这份预留的介质。**按稳定 ID 排序**，不用 set 顺序。"""
        remaining = count
        parts = []
        for medium_id in sorted(self.item_sources.get(key, ())):
            if remaining <= 0:
                break
            if self._provider_for(medium_id) is None:
                continue
            free_on_medium = self._available_on_medium(medium_id, key)
            if free_on_medium <= 0:
                continue
            take = min(remaining, free_on_medium)
            parts.append((medium_id, take))
            remaining -= take
        return tuple(parts)

    def _available_on_medium(self, medium_id, key):
        # type: (str, ResourceKey) -> int
        reserved = self._medium_reserved.get(medium_id, {}).get(key, 0)
        return max(0, self.drivers.medium(medium_id).count_of(key) - reserved)

    def _rebalance_parts(self, reservation):
        # type: (Reservation) -> None
        """预留数量变化后重算它在各盘上的分摊。"""
        self._drop_parts(reservation)
        reservation.parts = self._plan_parts(reservation.key, reservation.count)
        self._note_reservation_parts(reservation)

    def _note_reservation_parts(self, reservation):
        # type: (Reservation) -> None
        for medium_id, count in reservation.parts:
            bucket = self._medium_reserved.setdefault(medium_id, {})
            bucket[reservation.key] = bucket.get(reservation.key, 0) + count

    def _drop_parts(self, reservation):
        # type: (Reservation) -> None
        for medium_id, count in reservation.parts:
            bucket = self._medium_reserved.get(medium_id)
            if bucket is None:
                continue
            left = bucket.get(reservation.key, 0) - count
            if left > 0:
                bucket[reservation.key] = left
            else:
                bucket.pop(reservation.key, None)
            if not bucket:
                del self._medium_reserved[medium_id]

    def _forget_reservation(self, reservation):
        # type: (Reservation) -> None
        self._drop_parts(reservation)
        reservation.active = False
        self._reservations.pop(reservation.reservation_id, None)

    def _reserved_of(self, key):
        # type: (ResourceKey) -> int
        total = 0
        for reservation in self._ordered_reservations():
            if reservation.key == key:
                total += reservation.count
        return total

    # -- 转移 ----------------------------------------------------------

    def simulate_insert(self, key, count, now_tick=None):
        # type: (ResourceKey, int, int) -> dict
        """预演存入。**不改库存也不消耗额度。**"""
        now = self._now() if now_tick is None else int(now_tick)
        remaining = count
        reason = Reason.NO_CAPACITY
        for medium_id in sorted(self.mounted):
            if remaining <= 0:
                break
            provider = self._provider_for(medium_id)
            if provider is None:
                continue
            plan = provider.plan_transfer(medium_id, key, remaining, DIRECTION_WRITE, now)
            if plan is None:
                continue
            if plan.ok:
                remaining -= plan.accepted
            else:
                reason = plan.reason
        accepted = count - remaining
        return {
            "direction": DIRECTION_WRITE,
            "requested": count,
            "accepted": accepted,
            "reason": "" if accepted else reason,
        }

    def simulate_extract(self, key, count, now_tick=None):
        # type: (ResourceKey, int, int) -> dict
        """预演取出。可用量是 `stored - reserved`，**别人的预留看不到**。"""
        now = self._now() if now_tick is None else int(now_tick)
        entry = self.item_totals.get(key)
        available = (entry[0] - entry[1]) if entry is not None else 0
        accepted = min(count, available)
        reason = ""
        if accepted <= 0:
            reason = Reason.INSUFFICIENT_STOCK
        elif accepted < count:
            reason = Reason.INSUFFICIENT_STOCK
        # 额度是按盘算的，因此这里按盘累加，而不是拿汇总去估。
        quota = self._extract_quota(key, accepted, now) if accepted else 0
        if quota < accepted:
            accepted = quota
            reason = Reason.RATE_LIMITED
        return {
            "direction": DIRECTION_READ,
            "requested": count,
            "accepted": accepted,
            "reason": reason,
        }

    def commit_insert(self, op_id, key, count, owner_id=None, now_tick=None):
        # type: (str, ResourceKey, int, str, int) -> TransferReceipt
        """存入。带 `op_id` 去重：重发返回**原回执**，不二次移动。"""
        return self._commit(op_id, key, count, DIRECTION_WRITE, owner_id, now_tick)

    def commit_extract(self, op_id, key, count, owner_id=None, now_tick=None):
        # type: (str, ResourceKey, int, str, int) -> TransferReceipt
        """取出。带 `op_id` 去重。"""
        return self._commit(op_id, key, count, DIRECTION_READ, owner_id, now_tick)

    def _commit(self, op_id, key, count, direction, owner_id, now_tick):
        # type: (str, ResourceKey, int, str, str, int) -> TransferReceipt
        now = self._now() if now_tick is None else int(now_tick)
        op_key = (op_id or "", direction)
        state = self._ledger.begin(op_key, now)
        if state == RequestLedger.BEGIN_REPLAYED:
            stored = self._ledger.replay(op_key)
            if stored is not None:
                return stored
            return TransferReceipt(op_id, count, 0, TransferStatus.UNCERTAIN, "", key)
        if state == RequestLedger.BEGIN_IN_FLIGHT:
            # **不确定就不重放**（05-03 步骤 6）。
            return TransferReceipt(
                op_id, count, 0, TransferStatus.UNCERTAIN, "", key
            )

        receipt = self._do_transfer(op_id, key, count, direction, owner_id, now)
        self._ledger.finish(op_key, receipt, now)
        return receipt

    def _do_transfer(self, op_id, key, count, direction, owner_id, now):
        # type: (str, ResourceKey, int, str, str, int) -> TransferReceipt
        if direction == DIRECTION_WRITE:
            return self._do_insert(op_id, key, count, now)
        return self._do_extract(op_id, key, count, owner_id, now)

    def _do_insert(self, op_id, key, count, now):
        # type: (str, ResourceKey, int, int) -> TransferReceipt
        """跨盘存入。

        按**稳定介质 ID 顺序**逐盘走提供者的 `write`，凑够就停。
        走提供者而不是直接操作介质对象，是为了让「谁能读写一块盘」
        只有一条路径——设备的能力契约就该是唯一的门。
        """
        moved = 0
        reason = Reason.NO_CAPACITY
        budget = TransferBudget(now, op_id)
        for medium_id in sorted(self.mounted):
            if moved >= count:
                break
            provider = self._provider_for(medium_id)
            if provider is None:
                continue
            receipt = provider.write(medium_id, key, count - moved, budget)
            if receipt.moved > 0:
                moved += receipt.moved
            elif receipt.reason:
                reason = receipt.reason
        if moved:
            self._reindex_keys((key,))
            self.stats["inserts"] += 1
        else:
            self.stats["refused"] += 1
        return self._receipt(
            op_id, key, count, moved, direction=DIRECTION_WRITE, reason=reason
        )

    def _do_extract(self, op_id, key, count, owner_id, now):
        # type: (str, ResourceKey, int, str, int) -> TransferReceipt
        entry = self.item_totals.get(key)
        # 别人预留的部分**不能取**（05 验收：普通取货看不到他人预留物品）。
        available = (entry[0] - entry[1]) if entry is not None else 0
        if available <= 0:
            self.stats["refused"] += 1
            return self._receipt(
                op_id,
                key,
                count,
                0,
                direction=DIRECTION_READ,
                reason=Reason.INSUFFICIENT_STOCK,
            )
        target = min(count, available)

        moved = 0
        reason = Reason.INSUFFICIENT_STOCK
        budget = TransferBudget(now, op_id)
        for medium_id in sorted(self.item_sources.get(key, ())):
            if moved >= target:
                break
            provider = self._provider_for(medium_id)
            if provider is None:
                continue
            wanted = min(target - moved, self._available_on_medium(medium_id, key))
            if not wanted:
                continue
            receipt = provider.read(medium_id, key, wanted, budget)
            if receipt.moved > 0:
                moved += receipt.moved
            elif receipt.reason:
                reason = receipt.reason
        if moved:
            self._reindex_keys((key,))
            self.stats["extracts"] += 1
        else:
            self.stats["refused"] += 1
        return self._receipt(
            op_id, key, count, moved, direction=DIRECTION_READ, reason=reason
        )

    def _extract_quota(self, key, count, now):
        # type: (ResourceKey, int, int) -> int
        """取出方向当前能动的量（跨盘的额度合计）。纯预演，不消耗额度。"""
        total = 0
        for medium_id in sorted(self.item_sources.get(key, ())):
            if total >= count:
                break
            provider = self._provider_for(medium_id)
            if provider is None:
                continue
            wanted = min(count - total, self._available_on_medium(medium_id, key))
            if not wanted:
                continue
            plan = provider.plan_transfer(medium_id, key, wanted, DIRECTION_READ, now)
            if plan is not None and plan.ok:
                total += plan.accepted
        return total

    def _receipt(self, op_id, key, count, moved, direction, reason):
        # type: (str, ResourceKey, int, int, str, str) -> TransferReceipt
        if moved == count and count > 0:
            status = TransferStatus.COMPLETE
            reason = ""
        elif moved > 0:
            status = TransferStatus.PARTIAL
            reason = Reason.CONTAINER_PARTIAL
        else:
            status = TransferStatus.REFUSED
            reason = reason or Reason.INSUFFICIENT_STOCK
        return TransferReceipt(op_id, count, moved, status, reason, key)

    def _driver_admitted(self, ref):
        # type: (object) -> bool
        return self.runtime is None or self.runtime.admission.status_of(ref).ok is True

    def import_medium(self, source_ref, slot, key, count, now_tick, budget=8, cursor=0):
        """从转储器真实槽导入，源不挂到公共库存；返回搬运量、原因、工作量。"""
        from ...common.definitions import STORAGE_TRANSFER_ID
        from .medium import transfer_between

        if count <= 0 or budget <= 0:
            return 0, "等待介质", 0
        if self.runtime is not None:
            node = self.runtime.member(source_ref.node_key)
            energy = self.runtime.services.get(ServiceId.ENERGY)
            if (node is None or not node.ref.matches(source_ref) or not self.runtime.is_open()
                    or not self._driver_admitted(source_ref) or energy is None
                    or energy.power_ok(source_ref.node_key) is not True):
                return 0, "网络离线，暂停转储", 1
        state = self.drivers.get_by_ref(source_ref) if self.drivers is not None else None
        if (state is None or state.device_id != STORAGE_TRANSFER_ID or not state.loaded
                or not self.drivers.refresh(source_ref)):
            return 0, "介质变化，等待确认", 1
        assignment = state.assignment(slot)
        if assignment is None:
            return 0, "介质变化，等待确认", 1
        source = self.drivers.medium(assignment.medium_id)
        owner = self.drivers.medium_slot_owner(assignment.medium_id)
        if (source is None or not source.is_tradeable() or source.model_id not in self.drivers.definitions.mediums
                or owner is None or owner[0] is not state or owner[1].slot != slot
                or source.medium_id in self.mounted):
            return 0, "介质不可用", 1
        targets = sorted(self.mounted)
        if targets:
            offset = cursor % len(targets)
            targets = targets[offset:] + targets[:offset]
        moved, spent = 0, 0
        reason = "网络空间不足"
        for medium_id in targets:
            if spent >= budget or moved >= count:
                break
            spent += 1
            provider = self._provider_for(medium_id)
            if provider is None or medium_id == source.medium_id:
                continue
            target_ref = self.mounted[medium_id].device_ref
            if not self.drivers.refresh(target_ref) or self._provider_for(medium_id) is None:
                continue
            target = self.drivers.medium(medium_id)
            target_owner = self.drivers.medium_slot_owner(medium_id)
            if target_owner is None or not target_owner[0].ref.matches(target_ref):
                continue
            # 空间存在而计划无法提交时，是读写额度不足，不将其误报为满盘。
            if (target.free_micro() >= key.micro_units(1)
                    and (target.count_of(key) or target.remaining_types() > 0)):
                reason = "等待读写额度"
            amount = transfer_between(source, target, key, count - moved, now_tick,
                                      (source.revision, target.revision))
            if amount:
                self.drivers.note_transfer_changed(source.medium_id, medium_id)
                moved += amount
        if moved:
            self._reindex_keys((key,))
            self.stats["inserts"] += 1
        return moved, "转储中" if moved else reason, max(1, spent)

    def _provider_for(self, medium_id):
        # type: (str) -> object
        """取该介质的**提供者**。不能交易时返回 None。

        介质必须挂在本网络，所属驱动器必须仍是已加载并获准的实例，
        介质自身也须允许交易。
        """
        record = self.mounted.get(medium_id)
        if record is None or self.drivers is None:
            return None
        state = self.drivers.get_by_ref(record.device_ref)
        if state is None or not state.loaded or not self._driver_admitted(record.device_ref):
            return None
        medium = self.drivers.medium(medium_id)
        if medium is None or not medium.is_tradeable():
            return None
        return state.provider

    def _reindex_keys(self, keys):
        # type: (tuple) -> None
        """提交之后只更新受影响的键：不重扫全部介质。"""
        for key in keys:
            stored = 0
            sources = set()
            for medium_id in sorted(self.mounted):
                medium = self.drivers.medium(medium_id)
                if medium is None:
                    continue
                count = medium.count_of(key)
                if count > 0:
                    stored += count
                    sources.add(medium_id)
            entry = self.item_totals.get(key)
            reserved = entry[1] if entry is not None else 0
            self._set_stored(key, stored, reserved)
            if sources:
                self.item_sources[key] = sources
            else:
                self.item_sources.pop(key, None)
            self._mark_dirty(key)
        self.index_version += 1

    # -- 命令 ----------------------------------------------------------

    def handle(self, grant, command):
        # type: (object, object) -> CommandResult
        """受理一条已获准入的命令。

        **未知动作明确拒绝，绝不返回假成功**（架构 §7）。
        """
        self.stats["requests"] += 1
        if self.drivers is None:
            self.stats["refused"] += 1
            return CommandResult.blocked(
                command.request_id,
                [BlockReason(Reason.SERVICE_NOT_READY, "存储服务未接入驱动器")],
            )
        payload = command.payload or {}
        action = payload.get("action") or ""
        if not action:
            return self._reject(command, Reason.BAD_PARAMETER, "命令缺少 action")

        handlers = self._handler_table()
        handler = handlers.get(action)
        if handler is None:
            return self._reject(
                command,
                Reason.UNSUPPORTED,
                "存储服务没有动作 %r（可用：%s）" % (action, "、".join(sorted(handlers))),
            )
        try:
            return handler(command, payload)
        except (KeyError, TypeError, ValueError) as error:
            self.last_error = "%s: %s" % (type(error).__name__, error)
            return self._reject(
                command, Reason.BAD_PARAMETER, "操作失败，请重试"
            )

    def _handler_table(self):
        # type: () -> dict
        """`动作名 -> 处理器`。**只建一次**：装配推进与接口轮询每帧都会进来。

        终端交互（背包、槽位、光标、合成格、样板终端）由 `self.terminal`
        处理，这里只并入它报的动作表。
        """
        if self._handlers is not None:
            return self._handlers
        handlers = {
            "mount": self._cmd_mount,
            "unmount": self._cmd_unmount,
            "driver_status": self._cmd_driver_status,
            "refresh_mounts": self._cmd_refresh_mounts,
            "query": self._cmd_query,
            "snapshot": self._cmd_snapshot,
            "medium": self._cmd_medium,
            "insert": self._cmd_insert,
            "extract": self._cmd_extract,
            "reserve": self._cmd_reserve,
            "release": self._cmd_release,
            # 接口（06）
            "port_status": self._cmd_port_status,
            "port_configure": self._cmd_port_configure,
            "port_dispatch": self._cmd_port_dispatch,
            "port_stage": self._cmd_port_stage,
            "port_clear": self._cmd_port_clear,
        }
        handlers.update(self.terminal.handlers())
        self._handlers = handlers
        return handlers

    def _reject(self, command, code, detail):
        # type: (object, str, str) -> CommandResult
        self.stats["refused"] += 1
        return CommandResult.rejected(
            command.request_id, [BlockReason(code, detail)]
        )

    def _driver_state_of(self, command):
        # type: (object) -> tuple
        ref = command.device_ref
        if ref is None:
            return None, self._reject(command, Reason.BAD_PARAMETER, "命令没有目标设备")
        state = self.drivers.get_by_ref(ref)
        if state is None:
            return None, self._reject(
                command, Reason.DEVICE_GONE, "目标位置上没有驱动器实例"
            )
        if not state.loaded:
            return None, CommandResult.blocked(
                command.request_id,
                [
                    BlockReason(
                        Reason.DRIVER_UNLOADED,
                        "驱动器所在区块未加载；这不代表它已被拆除",
                    )
                ],
            )
        return state, None

    def _cmd_mount(self, command, payload):
        # type: (object, dict) -> CommandResult
        """把一块介质装进某个槽。

        **「装上了」的含义是「容器里那一格确实是它」**（2026-10-04）：命令层
        往方块容器写那件物品，驱动器随后重读、把槽位读数对上。因此这里不再
        直接改内存里的槽位表——那样写进去的槽位会被下一次重读静默回退。

        `model_id` 表示「造一块新的空白介质」：发号 + 把载体写进那件物品。
        """
        state, refusal = self._driver_state_of(command)
        if refusal is not None:
            return refusal
        slot = payload.get("slot")
        if isinstance(slot, bool) or not isinstance(slot, int):
            return self._reject(command, Reason.BAD_PARAMETER, "slot 必须是整数")

        medium_id = payload.get("medium_id")
        if not medium_id:
            model_id = payload.get("model_id")
            if not model_id:
                return self._reject(
                    command,
                    Reason.BAD_PARAMETER,
                    "需要 medium_id（已有介质）或 model_id（新建介质）",
                )
            medium, reason, detail = self.drivers.create_medium(model_id, self._now())
            if medium is None:
                return self._reject(command, reason, detail)
            medium_id = medium.medium_id

        ok, reason, detail = self.drivers.place_medium(
            state, slot, medium_id, payload.get("expected_slot_version")
        )
        if not ok:
            return self._refuse_mount(command, reason, detail)

        mounted, mount_reason, mount_detail = self.mount_medium(
            medium_id,
            state.node_key,
            slot,
            device_ref=state.ref,
            slot_version=state.slot_view(slot)["slot_version"],
        )
        if not mounted:
            # 物品已经在容器里了，只是网络侧这一次挂不上。**不回滚那件物品**：
            # 它就是权威读数，下一帧 `add_node` 会按它收敛；强行取回来反而要
            # 给它找一个新的去向，而这里没有可靠的去向。
            return self._refuse_mount(command, mount_reason, mount_detail)
        return CommandResult.ok(
            command.request_id,
            {
                "action": "mount",
                "medium_id": medium_id,
                "slot": slot,
                "session_slots": state.slot_views(),
                "config_version": state.config_version,
                "disk_count": state.disk_count(self.drivers.medium),
            },
        )

    def _cmd_unmount(self, command, payload):
        # type: (object, dict) -> CommandResult
        """把某个槽里的介质物品取出来，交给玩家背包。

        **取出来必须有去向。** 本命令目前只支持 `into="player"`：先探一次
        背包放不放得下，**放不下就不取**——绝不静默把那件物品销毁掉。
        （玩家直接用手把介质从驱动器里拿出来是另一条路，不经过这里。）
        """
        state, refusal = self._driver_state_of(command)
        if refusal is not None:
            return refusal
        slot = payload.get("slot")
        if isinstance(slot, bool) or not isinstance(slot, int):
            return self._reject(command, Reason.BAD_PARAMETER, "slot 必须是整数")
        if payload.get("into", "player") != "player":
            return self._reject(
                command,
                Reason.BAD_PARAMETER,
                "取出来的物品必须有去向：本命令目前只支持 into='player'",
            )
        assignment = state.assignment(slot)
        if assignment is None:
            return self._reject(
                command, Reason.BAD_PARAMETER, "槽位 %d 本来就是空的" % (slot,)
            )
        data = self.drivers.inspect_slot(state, slot)
        if data is None:
            return self._refuse_mount(
                command, Reason.CONTAINER_UNCERTAIN, "读不出 %d 号槽里那件物品" % (slot,)
            )
        try:
            key = item_key_from_dict(data)
        except (TypeError, ValueError):
            return self._reject(command, Reason.SLOT_CHANGED, "槽里的物品字典读不动")
        if key is None:
            return self._reject(
                command, Reason.BAD_PARAMETER, "槽位 %d 是空的" % (slot,)
            )
        endpoint, refusal = self.inventory_endpoint_for(command, payload)
        if refusal is not None:
            return refusal
        count = 1
        space = endpoint.probe_space(key, count)
        if space is not None and space < count:
            return self._reject(
                command, Reason.NO_CAPACITY, "背包放不下这一件；先腾出空间再来取"
            )
        ok, reason, taken = self.drivers.take_medium(
            state, slot, payload.get("expected_slot_version")
        )
        if not ok:
            return self._refuse_mount(command, reason, taken)
        receipt = endpoint.commit_insert(command.scoped_request_id(), key, count)
        if receipt.moved < count:
            # 探过之后仍然没全收下：**如实报不确定**，不当作成功，
            # 也不能把那件物品再塞回容器（读数已经变了，塞回去是第二次写）。
            return CommandResult.rejected(
                command.request_id,
                [
                    BlockReason(
                        Reason.CONTAINER_UNCERTAIN,
                        "介质已从驱动器取出，但背包只收下 %d 件；请核对背包"
                        % (receipt.moved,),
                    )
                ],
            )
        self.unmount_medium(assignment.medium_id, reason="取出介质")
        return CommandResult.ok(
            command.request_id,
            {
                "action": "unmount",
                "slot": slot,
                "medium_id": assignment.medium_id,
                "into": "player",
                "session_slots": state.slot_views(),
                "config_version": state.config_version,
                "disk_count": state.disk_count(self.drivers.medium),
            },
        )

    def _cmd_driver_status(self, command, payload):
        # type: (object, dict) -> CommandResult
        state, refusal = self._driver_state_of(command)
        if refusal is not None:
            return refusal
        return CommandResult.ok(
            command.request_id,
            {
                "action": "driver_status",
                "node_key": state.node_key.as_key(),
                "slots": state.slot_views(),
                "config_version": state.config_version,
                "disk_count": state.disk_count(self.drivers.medium),
                "recovery": state.recovery,
                "ready": self.is_ready(),
            },
        )

    def _cmd_refresh_mounts(self, command, payload):
        # type: (object, dict) -> CommandResult
        return CommandResult.ok(
            command.request_id,
            {"action": "refresh_mounts", "changed": self.refresh_mounts()},
        )

    def _cmd_medium(self, command, payload):
        # type: (object, dict) -> CommandResult
        medium_id = payload.get("medium_id")
        if not medium_id:
            return self._reject(command, Reason.BAD_PARAMETER, "缺少 medium_id")
        return CommandResult.ok(
            command.request_id, {"action": "medium", "medium": self.medium_view(medium_id)}
        )

    def _cmd_query(self, command, payload):
        # type: (object, dict) -> CommandResult
        raw = payload.get("key")
        if payload.get("all"):
            return CommandResult.ok(
                command.request_id, {"action": "query", "snapshot": self.snapshot()}
            )
        if not isinstance(raw, dict):
            return self._reject(command, Reason.BAD_PARAMETER, "key 必须是资源键字典")
        return CommandResult.ok(
            command.request_id,
            {"action": "query", "entry": self.query(ResourceKey.from_dict(raw))},
        )

    def _cmd_snapshot(self, command, payload):
        # type: (object, dict) -> CommandResult
        return CommandResult.ok(
            command.request_id, {"action": "snapshot", "snapshot": self.snapshot()}
        )

    def _cmd_insert(self, command, payload):
        # type: (object, dict) -> CommandResult
        return self._transfer_command(command, payload, DIRECTION_WRITE)

    def _cmd_extract(self, command, payload):
        # type: (object, dict) -> CommandResult
        return self._transfer_command(command, payload, DIRECTION_READ)

    def _transfer_command(self, command, payload, direction):
        # type: (object, dict, str) -> CommandResult
        raw = payload.get("key")
        count = payload.get("count")
        if not isinstance(raw, dict) or isinstance(count, bool) or not isinstance(count, int):
            return self._reject(command, Reason.BAD_PARAMETER, "需要 key 与整数 count")
        if count <= 0:
            return self._reject(command, Reason.OUT_OF_RANGE, "count 必须为正")
        key = ResourceKey.from_dict(raw)
        op_id = payload.get("op_id") or command.scoped_request_id()
        source = command.source
        owner_id = source.player_id if source is not None else None
        if direction == DIRECTION_WRITE:
            receipt = self.commit_insert(op_id, key, count, owner_id)
        else:
            receipt = self.commit_extract(op_id, key, count, owner_id)
        return self._receipt_result(command, receipt)

    def _cmd_reserve(self, command, payload):
        # type: (object, dict) -> CommandResult
        raw = payload.get("key")
        count = payload.get("count")
        if not isinstance(raw, dict) or isinstance(count, bool) or not isinstance(count, int):
            return self._reject(command, Reason.BAD_PARAMETER, "需要 key 与整数 count")
        owner_id = payload.get("owner_id")
        if not owner_id:
            source = command.source
            owner_id = source.player_id if source is not None else ""
        reservation = self.reserve(owner_id, ResourceKey.from_dict(raw), count, self._now())
        if reservation is None:
            return CommandResult.blocked(
                command.request_id,
                [BlockReason(Reason.INSUFFICIENT_STOCK, "可用量不足，预留未成立")],
            )
        return CommandResult.ok(
            command.request_id,
            {"action": "reserve", "reservation": reservation.to_dict()},
        )

    def _cmd_release(self, command, payload):
        # type: (object, dict) -> CommandResult
        reservation_id = payload.get("reservation_id")
        count = payload.get("count")
        if reservation_id:
            released = self.release(reservation_id, count)
        else:
            owner_id = payload.get("owner_id")
            if not owner_id:
                return self._reject(
                    command, Reason.BAD_PARAMETER, "需要 reservation_id 或 owner_id"
                )
            released = self.release_owned_by(owner_id)
        return CommandResult.ok(
            command.request_id, {"action": "release", "released": released}
        )

    # -- 接口命令（06）-------------------------------------------------

    def _dispatcher(self):
        # type: () -> object
        """接口的命令处理器。与转移协调器一样**每网络一份**。"""
        if self._port_dispatcher is None:
            self._port_dispatcher = PortDispatcher(
                self.ports,
                self.transfer(),
                self.network_endpoint(),
            )
        return self._port_dispatcher

    def network_endpoint(self):
        # type: () -> NetworkEndpoint
        """本网络这一端的转移端点（终端交互与接口派发共用）。"""
        return NetworkEndpoint(self, self._network_id())

    def _network_id(self):
        # type: () -> str
        return self.runtime.component_id if self.runtime is not None else ""

    def _port_state_of(self, command):
        # type: (object) -> tuple
        """取命令目标的那台接口。三种失败分开报。"""
        ref = command.device_ref
        if ref is None:
            return None, self._reject(command, Reason.BAD_PARAMETER, "命令没有目标设备")
        if self.ports is None:
            return None, CommandResult.blocked(
                command.request_id,
                [
                    BlockReason(
                        Reason.SERVICE_NOT_READY, "存储服务未接入接口状态集合"
                    )
                ],
            )
        state = self.ports.get_by_ref(ref)
        if state is None:
            return None, self._reject(
                command, Reason.DEVICE_GONE, "目标位置上没有接口实例"
            )
        if not state.loaded:
            return None, CommandResult.blocked(
                command.request_id,
                [
                    BlockReason(
                        Reason.DRIVER_UNLOADED,
                        "接口所在区块未加载；这不代表它已被拆除",
                    )
                ],
            )
        return state, None

    def _cmd_port_status(self, command, payload):
        # type: (object, dict) -> CommandResult
        state, refusal = self._port_state_of(command)
        if refusal is not None:
            return refusal
        view = state.to_dict()
        blocker = port_blocker(state)
        view["action"] = "port_status"
        view["blocker"] = blocker.to_dict() if blocker is not None else None
        # **这条命令走的是 `CommandKind.STORAGE`，因此要过主控准入**——
        # 网络没有主控时它压根到不了这里（那是有意的：设备不得绕过主控）。
        # 想在离线/无主控时查看接口，走只读入口 `debug_query("port")`，
        # 那条路径不经过主控，也不碰任何业务状态。
        return CommandResult.ok(command.request_id, view)

    def _cmd_port_configure(self, command, payload):
        # type: (object, dict) -> CommandResult
        """写入接口配置。**载荷只表达意图**：过滤内容、方向、单次上限。

        不接受客户端直接提交的「缓冲里现在有几个」之类的计数——那是权威状态
        （架构 §4 的同一条纪律，`disk_count` 那边照此办理）。
        """
        state, refusal = self._port_state_of(command)
        if refusal is not None:
            return refusal
        raw = payload.get("config")
        if not isinstance(raw, dict):
            return self._reject(command, Reason.BAD_PARAMETER, "config 必须是字典")
        # **合并而不是整体替换。** 界面往往只改其中一项（比如只改过滤），
        # 整体替换会把没提到的字段打回默认值——玩家会发现「我只改了过滤，
        # 方向怎么回去了」。合并的语义是「改我说的那几项」，与命令载荷
        # 「只表达意图」的定位一致。
        merged = state.config.to_dict()
        if "slot_items" in raw and (state.config.slot_items is not None or raw["slot_items"] is not None):
            return self._reject(command, Reason.BAD_PARAMETER, "请按槽提交配置")
        if "slot_item" in raw:
            edit = raw["slot_item"]
            if (state.config.slot_items is None or len(raw) != 1 or not isinstance(edit, dict)
                    or set(edit) != set(("slot", "item_id"))):
                return self._reject(command, Reason.BAD_PARAMETER, "逐槽配置格式错误")
            slot = edit["slot"]
            items = merged["slot_items"]
            if isinstance(slot, bool) or not isinstance(slot, INTEGER_TYPES) or not 0 <= slot < len(items):
                return self._reject(command, Reason.SUPPLY_SLOTS_INVALID, "供货位越界")
            items[slot] = edit["item_id"]
            raw = {}
        elif "supply_slots" in raw or "filter" in raw:
            # Older clients submit a shared filter and supply-slot indices.
            merged["slot_items"] = None
        if state.device_id == "endernet:bus_item_input_io" and "filter_mode" in raw:
            if raw["filter_mode"] not in tuple(FILTER_MODES):
                return self._reject(command, Reason.BAD_PARAMETER, "未知的过滤模式")
            merged["filter"] = (merged.get("item_filters") or {}).get(raw["filter_mode"], [])
        for field, value in raw.items():
            if field in merged:
                merged[field] = value
            else:
                return self._reject(
                    command, Reason.BAD_PARAMETER,
                    "配置里没有 %r 这一项（可用：%s）" % (field, "、".join(sorted(merged))),
                )
        try:
            config = PortConfig.from_dict(merged, self.ports.policy)
            if state.device_id != "endernet:bus_item_io":
                normalize_item_config(config, state.device_id, validate=False)
        except (KeyError, TypeError, ValueError) as error:
            return self._reject(
                command, Reason.BAD_PARAMETER, "配置内容不合法：%s" % (error,)
            )
        refusal = self._validate_port_config(
            state, config, command.request_id
        )
        if refusal is not None:
            return refusal
        try:
            self.ports.configure(state.ref, config)
        except ValueError as error:
            return self._reject(command, Reason.BAD_PARAMETER, str(error))
        # **配置变更是「立即尝试」的触发点**：`SchedulerService.wake` 把该设备的
        # 轮询提前到本帧。槽位变化没有通知通道（容器 API 不提供），那条路只能
        # 靠方块实体 tick 打回退避——04 明确警告过不能拿心跳当变化信号。
        self._wake_port(state.ref)
        view = state.to_dict()
        view["action"] = "port_configure"
        return CommandResult.ok(command.request_id, view)

    def _validate_port_config(self, state, config, prefix):
        # type: (object, PortConfig, str) -> object
        """按**角色**校验配置。返回拒绝结果，通过时返回 None。

        三条校验都放在服务端（客户端只提意图）——11-07 的验收明确要求
        「非法第 10 种取货配置被服务端拒绝」，因此不能只在界面上拦住。
        """
        allowed = ROLE_FILTER_MODES.get(state.role, frozenset([MODE_WHITELIST]))
        if config.filter_mode not in allowed:
            return CommandResult.rejected(
                prefix,
                [
                    BlockReason(
                        Reason.FILTER_MODE_MISMATCH,
                        "%s 角色不支持 %s 过滤（可用：%s）"
                        % (state.role, config.filter_mode, "、".join(sorted(allowed))),
                    )
                ],
            )
        limit = self.ports.policy.filter_limit_for(state.role)
        count = len(config.filter)
        if state.device_id == "endernet:bus_item_input_io":
            limit = 16
            count = max([count] + [len(entries) for entries in config.item_filters.values()])
        if count > limit:
            return CommandResult.rejected(
                prefix,
                [
                    BlockReason(
                        Reason.FILTER_LIMIT_REACHED,
                        "%s 最多配置 %d 种资源，收到 %d 种"
                        % (state.role, limit, count),
                    )
                ],
            )
        if config.supply_slots:
            if state.role != PASSIVE:
                return CommandResult.rejected(
                    prefix,
                    [
                        BlockReason(
                            Reason.SUPPLY_SLOTS_INVALID,
                            "只有被动接口有存取共用的槽位，%s 没有供货位"
                            % (state.role,),
                        )
                    ],
                )
            slot_count = self.ports.supply_slot_count(state)
            for slot in config.supply_slots:
                if slot < 0 or slot >= slot_count:
                    return CommandResult.rejected(
                        prefix,
                        [
                            BlockReason(
                                Reason.SUPPLY_SLOTS_INVALID,
                                "供货位 %r 越界（这台接口有 %d 格）"
                                % (slot, slot_count),
                            )
                        ],
                    )
        return None

    def _wake_port(self, ref):
        # type: (object) -> bool
        """把该接口的轮询提前到本帧。没有调度服务时静默返回 False——
        「不能立刻试」不等于「配置没生效」，下一次轮询照样会用新配置。"""
        scheduler = self._scheduler()
        if scheduler is None or self._ports_runner is None:
            return False
        return self._ports_runner.wake(ref, scheduler)

    def _cmd_port_dispatch(self, command, payload):
        # type: (object, dict) -> CommandResult
        """接口自报的一次搬运尝试。**由接口自己的轮询发起。**

        意图必须与设备的角色一致：设备不得要求一台取货口去做存货口的事
        （全局约束 2：设备不得绕过主控改库存，也不得越权扮演别的角色）。
        """
        state, refusal = self._port_state_of(command)
        if refusal is not None:
            return refusal
        intent = payload.get("intent")
        if intent != state.role:
            return self._reject(
                command,
                Reason.PORT_DIRECTION_MISMATCH,
                "设备 %s 的角色是 %r，却要求执行 %r"
                % (state.device_id, state.role, intent),
            )
        # **一份门**（`port_blocker`）：轮询、这里、只读诊断三处共用。
        # 各写一份的代价是加新门时漏掉一处，而那处的症状是
        # 「`port_status` 说正常、命令却不动」——最难查的一类。
        blocker = port_blocker(state)
        if blocker is not None:
            return CommandResult.blocked(command.request_id, [blocker])
        result = self._dispatcher().dispatch(
            state, command.request_id, command.topology_version
        )
        # 轮询和订单主动投料共用这里。在下一条命令复用批次槽之前记账。
        if (state.role == TEMPLATE and (result.payload or {}).get("batch")
                and state.batch.is_complete()):
            factory = self.runtime.services.get(ServiceId.FACTORY)
            executor = getattr(factory, "executor", None)
            if executor is not None:
                executor.confirm_dispatched_batch(state, self.runtime.tick_count)
        return result

    def _cmd_port_stage(self, command, payload):
        # type: (object, dict) -> CommandResult
        """成批备料入口（06-07）。**由任务 09 的工厂服务调用。**

        09 还未实现，因此这条命令目前只有测试与诊断会用到；它在这里的意义是
        **把契约定下来**：工厂给出 `op_id`、资源与数量，网络把料备进接口的
        中转位，接口再一台一台投给外部机器。工厂不需要知道投料怎么实现。
        """
        state, refusal = self._port_state_of(command)
        if refusal is not None:
            return refusal
        if state.role != TEMPLATE:
            return self._reject(
                command,
                Reason.UNSUPPORTED,
                "只有样板接口支持成批备料（设备 %s 的角色是 %r）"
                % (state.device_id, state.role),
            )
        raw = payload.get("key")
        count = payload.get("count")
        op_id = payload.get("op_id") or command.scoped_request_id()
        if not isinstance(raw, dict) or isinstance(count, bool) or not isinstance(count, int):
            return self._reject(command, Reason.BAD_PARAMETER, "需要 key 与整数 count")
        if count <= 0:
            return self._reject(command, Reason.OUT_OF_RANGE, "count 必须为正")
        return self._dispatcher().stage_batch(
            state,
            ResourceKey.from_dict(raw),
            count,
            op_id,
            command.request_id,
            command.topology_version,
        )

    def _cmd_port_clear(self, command, payload):
        # type: (object, dict) -> CommandResult
        """人工解除待核对标记。**它不做任何搬运**，只是允许这条路重新尝试。

        需要这个入口的理由：一次 `UNCERTAIN` 会让该接口停用（06-02 不许
        盲目重试），而自动重试不能替代核对。没有它，一条不确定就永久停用。
        """
        state, refusal = self._port_state_of(command)
        if refusal is not None:
            return refusal
        # 走访问器而不是读私有字段：`clear_hold` 只碰状态，不碰调度器，
        # 因此没装调度服务时也该能解除（那是台不能轮询但能销账的接口）。
        runner = self.ports_runner()
        ok, detail = runner.clear_hold(state.ref)
        view = state.to_dict()
        view["action"] = "port_clear"
        view["cleared"] = ok
        view["detail"] = detail
        # 没有待核对标记**不是错误**：重复点一次「解除」应当得到同一条回答，
        # 而不是一个看起来像失败的原因码。
        return CommandResult.ok(command.request_id, view)

    # -- 自定义背包槽（06-09，服务端半边）------------------------------

    def inventory_endpoint_for(self, command, payload):
        # type: (object, dict) -> tuple
        """取请求所指向的背包端点。返回 `(endpoint, refusal)`。

        玩家身份取**服务端可信来源**（`command.source.player_id`），
        不接受载荷里自报的玩家——那等于让客户端指定去动谁的背包。
        """
        if self.inventories is None:
            return None, CommandResult.blocked(
                command.request_id,
                [BlockReason(Reason.SERVICE_NOT_READY, "存储服务未接入背包提供者")],
            )
        source = command.source
        player_id = source.player_id if source is not None else None
        if not player_id:
            return None, self._reject(
                command, Reason.BAD_PARAMETER, "命令没有可信的玩家身份"
            )
        endpoint = self.inventories.endpoint_of(player_id)
        if endpoint is None:
            return None, self._reject(
                command, Reason.DEVICE_GONE, "找不到玩家 %s 的背包" % (player_id,)
            )
        if not endpoint.can_read():
            return None, CommandResult.blocked(
                command.request_id,
                [BlockReason(Reason.CONTAINER_UNCERTAIN, "背包当前不可读")],
            )
        return endpoint, None


    def _refuse_mount(self, command, reason, detail):
        # type: (object, str, str) -> CommandResult
        """装载类失败的三种归宿，必须分开，否则界面给不出可操作的提示。

        - `stale`：请求依据的槽位版本已过期，**重新取快照再发**就有机会成功；
        - `rejected`：请求本身不合法（越界、槽被占、物品不对），重发无用；
        - `blocked`：暂时不通（服务未就绪、盘读不懂），稍后可能可以。
        """
        self.stats["refused"] += 1
        block_reason = BlockReason(reason, detail)
        if reason == Reason.STALE_VERSION:
            return CommandResult.stale(command.request_id, [block_reason])
        if reason in (
            Reason.OUT_OF_RANGE,
            Reason.BAD_PARAMETER,
            Reason.SLOT_OCCUPIED,
            Reason.NOT_A_MEDIUM,
            Reason.MEDIUM_DUPLICATE_MOUNT,
        ):
            return CommandResult.rejected(command.request_id, [block_reason])
        return CommandResult.blocked(command.request_id, [block_reason])

    def _receipt_result(self, command, receipt, action="transfer"):
        # type: (object, TransferReceipt, str) -> CommandResult
        """回执 -> 命令结果。

        分流口径与接口那边**共用一份实现**（`transfer.receipt_result`）：
        `stale`（取新快照再发）与 `rejected`（重发无用）与 `blocked`
        （暂时不通）对界面意味着三种不同的下一步提示，两处各写一份迟早分叉。
        """
        return receipt_result(command.request_id, receipt, action)

#: 介质恢复状态 -> 人可读的说明。诊断与界面共用，避免两处各写一套措辞。
RECOVERY_TEXT = {
    MediumRecovery.OK: "正常",
    MediumRecovery.UNKNOWN_SCHEMA: "存档版本无法识别，内容原样保留但不可交易",
    MediumRecovery.MISSING_MODEL: "介质型号未注册，内容原样保留但不可交易",
    MediumRecovery.CORRUPT: "记录损坏，内容原样保留但不可交易",
}
