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
    ORIGIN_EXTERNAL,
    ORIGIN_INTERNAL,
    Reason,
    TransferReceipt,
    TransferRequest,
    TransferStatus,
)
from ...common.resource_key import ResourceKey  # noqa: F401 (仅用于 # type: 注释)
from ..devices.io import (
    FILTER_MODES,
    INTAKE,
    MODE_WHITELIST,
    PASSIVE_PORT_SLOT_COUNT,
    ROLE_FILTER_MODES,
    OUTPUT,
    PASSIVE,
    TEMPLATE,
    PortConfig,
    PortDispatcher,
    PortGateway,
    PortRunner,
    is_port_device,
    port_blocker,
    role_of,
)
from ..devices.storage_driver import DRIVER_DEVICE_ID
from ..network.controller import RequestLedger
from ..network.services import NetworkService, ServiceId
from .medium import (
    DIRECTION_READ,
    DIRECTION_WRITE,
    MediumRecovery,
    TransferBudget,
)
from .transfer import (
    CURSOR_IGNORE,
    NetworkEndpoint,
    SlotAnchor,
    TransferCoordinator,
    TransferPolicy,
    anchored_extract_endpoint,
    anchored_insert_endpoint,
    receipt_result,
)


#: `NetworkNode.has_capability` 用的能力名。与 `devices/capabilities.py` 一致。
STORAGE_PROVIDER_CAPABILITY = "storage_provider"


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
    ):
        # type: (object, object, object, object, object, object, object, object) -> None
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
        #: 背包提供者（06-09）。生产实现是 `adapters/containers_engine.py`
        #: 的 `EngineInventoryProvider`，离线测试注入替身。
        self.inventories = inventories
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
        """登记一个成员。**对每个成员每帧都会被调用**，必须幂等。

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
        authoritative = set(item.medium_id for item in state.occupied())
        for medium_id in sorted(self.mounted):
            record = self.mounted[medium_id]
            if record.node_key == node.node_key and medium_id not in authoritative:
                self.unmount_medium(medium_id, reason="权威槽位里已没有它")

        for assignment in state.occupied():
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
        # 幂等重挂**不算变化**：`sync_members` 每帧对每个成员调本方法，
        # 若把「本来就挂着」报成变化，运行时每帧都会发一次成员变更通知。
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

        介质额度按介质记账，与带宽负载域无关；驱动器负载变化由
        `note_device_load_changed` 只重算所在域，域归属变化本身由
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
                "index_version": self.index_version,
                "publish_version": self.publish_version,
                "clock_tick": self._clock_tick,
                "ledger": self._ledger.stats,
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
                    "仍未接入。"
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
            if node.device_id != DRIVER_DEVICE_ID:
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
            if before == 0 and after > 0:
                self.item_sources.setdefault(key, set()).add(medium.medium_id)
            elif before > 0 and after == 0:
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
            medium = self.drivers.medium(medium_id)
            if medium is None or not medium.is_tradeable():
                continue
            already = self._medium_reserved.get(medium_id, {}).get(key, 0)
            free_on_medium = medium.count_of(key) - already
            if free_on_medium <= 0:
                continue
            take = min(remaining, free_on_medium)
            parts.append((medium_id, take))
            remaining -= take
        return tuple(parts)

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
        if accepted and quota <= 0:
            accepted = 0
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
            receipt = provider.read(medium_id, key, target - moved, budget)
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
            plan = provider.plan_transfer(medium_id, key, count - total, DIRECTION_READ, now)
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

    def _provider_for(self, medium_id):
        # type: (str) -> object
        """取该介质的**提供者**。不能交易时返回 None。

        三个条件缺一不可：介质挂在本网络、驱动器实例仍在（代次对得上）、
        介质本身可交易。缓存还在不等于能交易（架构 §5.2：断电/失去准入时
        「保留权威内容」与「允许访问」是两件事）。
        """
        record = self.mounted.get(medium_id)
        if record is None or self.drivers is None:
            return None
        state = self.drivers.get_by_ref(record.device_ref)
        if state is None:
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
            # 自定义背包槽（06-09，服务端半边）
            "inv_take": self._cmd_inv_take,
            "inv_put": self._cmd_inv_put,
            "inv_split": self._cmd_inv_split,
            "inv_merge": self._cmd_inv_merge,
            "inv_quick_move": self._cmd_inv_quick_move,
        }
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
            return self._reject(
                command, Reason.BAD_PARAMETER, "动作 %s 的参数不合法：%s" % (action, error)
            )

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

        provider = state.provider
        ok, reason, detail = provider.mount(
            slot, medium_id, payload.get("expected_slot_version"), self._now()
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
            # 驱动器已经装上了但网络侧挂不上去：**回滚槽位**，不留半完成状态。
            provider.unmount(slot)
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
        state, refusal = self._driver_state_of(command)
        if refusal is not None:
            return refusal
        slot = payload.get("slot")
        if isinstance(slot, bool) or not isinstance(slot, int):
            return self._reject(command, Reason.BAD_PARAMETER, "slot 必须是整数")

        assignment = state.assignment(slot)
        ok, reason, detail = state.provider.unmount(
            slot, payload.get("expected_slot_version")
        )
        if not ok:
            return self._refuse_mount(command, reason, detail)
        if assignment is not None:
            self.unmount_medium(assignment.medium_id, reason="取出介质")
        return CommandResult.ok(
            command.request_id,
            {
                "action": "unmount",
                "slot": slot,
                "medium_id": detail,
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
        op_id = payload.get("op_id") or command.request_id
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
                NetworkEndpoint(self, self._network_id()),
            )
        return self._port_dispatcher

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
        except (KeyError, TypeError, ValueError) as error:
            return self._reject(
                command, Reason.BAD_PARAMETER, "配置内容不合法：%s" % (error,)
            )
        refusal = self._validate_port_config(
            state, config, command.request_id
        )
        if refusal is not None:
            return refusal
        self.ports.configure(state.ref, config)
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
        if len(config.filter) > limit:
            return CommandResult.rejected(
                prefix,
                [
                    BlockReason(
                        Reason.FILTER_LIMIT_REACHED,
                        "%s 最多配置 %d 种资源，收到 %d 种"
                        % (state.role, limit, len(config.filter)),
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
        return self._dispatcher().dispatch(
            state, command.request_id, command.topology_version
        )

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
        op_id = payload.get("op_id") or command.request_id
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

    def _inventory_endpoint_for(self, command, payload):
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

    @staticmethod
    def _anchor_of(payload):
        # type: (dict) -> object
        """从载荷里读槽锚。缺 `slot` 时返回 None（表示「不按槽定位」）。"""
        slot = payload.get("slot")
        if slot is None:
            return None
        raw = payload.get("key")
        key = ResourceKey.from_dict(raw) if isinstance(raw, dict) else None
        return SlotAnchor(slot, key, payload.get("slot_version"))

    def _cursor_refusal(self, command, payload):
        # type: (object, dict) -> object
        """R12 的显式拦截：客户端说手里还拿着东西时怎么办**尚未定案**。

        替身口径是「拒绝接管」。没有这个拦截，一份没有规则的东西会被
        「看起来能用」的实现悄悄定下来（全局约束 7）。
        """
        if not payload.get("cursor"):
            return None
        if self.transfer_policy.cursor_disposition == CURSOR_IGNORE:
            return None
        return CommandResult.rejected(
            command.request_id,
            [
                BlockReason(
                    Reason.CURSOR_UNRESOLVED,
                    "R12 未定案：本模组目前不接管关闭界面时手里的物品",
                )
            ],
        )

    def _transfer_payload(self, command, payload, direction):
        # type: (object, dict, str) -> CommandResult
        """`inv_take` / `inv_put` 的公共实现。

        **带槽锚时只动那个槽，但走的是同一条转移路径。** 锚被做成一个
        「只能动这个槽」的端点视图（`AnchoredInventoryView`），于是预检、
        去重、取出、放入、余量回退全都与普通路径共用一段代码。

        早先锚定路径是一条**旁路**：核对锚、调 `anchored_extract`、完事。
        那条路会同时漏掉三样东西——网络那一侧根本没人动（`inv_take` 于是把
        背包里的东西凭空删掉、`inv_put` 凭空造出来）、去重账本不参与
        （重发就再动一次）、外部输入通知不发（手动存入不算回流）。
        """
        cursor = self._cursor_refusal(command, payload)
        if cursor is not None:
            return cursor
        endpoint, refusal = self._inventory_endpoint_for(command, payload)
        if refusal is not None:
            return refusal
        raw = payload.get("key")
        count = payload.get("count")
        if not isinstance(raw, dict) or isinstance(count, bool) or not isinstance(count, int):
            return self._reject(command, Reason.BAD_PARAMETER, "需要 key 与整数 count")
        if count <= 0:
            return self._reject(command, Reason.OUT_OF_RANGE, "count 必须为正")
        key = ResourceKey.from_dict(raw)
        try:
            anchor = self._anchor_of(payload)
        except (TypeError, ValueError) as error:
            return self._reject(command, Reason.BAD_PARAMETER, "槽锚不合法：%s" % (error,))

        op_id = payload.get("op_id") or command.request_id
        network = NetworkEndpoint(self, self._network_id())
        if direction == DIRECTION_WRITE:
            source, target = network, endpoint
        else:
            source, target = endpoint, network

        if anchor is not None:
            if anchor.expected_key is None:
                return self._reject(
                    command, Reason.BAD_PARAMETER,
                    "按槽操作必须给出期望物品指纹（key）",
                )
            if direction == DIRECTION_READ:
                source = anchored_extract_endpoint(endpoint, anchor)
            else:
                target = anchored_insert_endpoint(endpoint, anchor)
            # **锚先过一道**，为的是给出准确的原因码：预检只会说
            # 「来源没有可动的 / 目标放不下」，而玩家要做的是**刷新界面**。
            view = source if direction == DIRECTION_READ else target
            ok, reason, detail = view.anchor_verdict()
            if not ok:
                return self._receipt_result(
                    command,
                    TransferReceipt(
                        op_id, count, 0, TransferStatus.REFUSED, reason, key,
                        endpoint.holder_id(),
                    ),
                    "inv",
                )

        request = TransferRequest(
            op_id,
            source.endpoint_id(),
            target.endpoint_id(),
            key,
            count,
            command.topology_version,
            owner_id=(command.source.player_id if command.source is not None else ""),
            origin=ORIGIN_EXTERNAL if direction == DIRECTION_READ else ORIGIN_INTERNAL,
        )
        receipt = self.transfer().commit(request, source, target)
        return self._receipt_result(command, receipt, "inv")

    def _cmd_inv_take(self, command, payload):
        # type: (object, dict) -> CommandResult
        """从背包取出，存入网络。"""
        return self._transfer_payload(command, payload, DIRECTION_READ)

    def _cmd_inv_put(self, command, payload):
        # type: (object, dict) -> CommandResult
        """从网络取出，放进背包。**按 maxStackSize 拆分，不生成超堆叠物品。**
        """
        return self._transfer_payload(command, payload, DIRECTION_WRITE)

    def _cmd_inv_split(self, command, payload):
        # type: (object, dict) -> CommandResult
        """拆分：从某个槽取走**一部分**存进网络，其余留在原槽。

        实现上就是带槽锚的 `inv_take`——分开命名是因为它在界面上是另一个
        手势，而「同一次点击重发只生效一次」这条要求对两者都一样。
        """
        if payload.get("slot") is None:
            return self._reject(command, Reason.BAD_PARAMETER, "拆分必须给出 slot")
        return self._transfer_payload(command, payload, DIRECTION_READ)

    def _cmd_inv_merge(self, command, payload):
        # type: (object, dict) -> CommandResult
        """合并：把 `slot` 的内容并进 `into_slot`。**背包内部移动，不经过网络。**

        仍然要服务端做，因为客户端看到的两个槽可能已经过期：两个槽锚各自
        核对一遍，任一不过就整体拒绝，而不是「并一半」。
        """
        cursor = self._cursor_refusal(command, payload)
        if cursor is not None:
            return cursor
        endpoint, refusal = self._inventory_endpoint_for(command, payload)
        if refusal is not None:
            return refusal
        from_slot = payload.get("slot")
        into_slot = payload.get("into_slot")
        if from_slot is None or into_slot is None:
            return self._reject(
                command, Reason.BAD_PARAMETER, "合并需要 slot 与 into_slot"
            )
        if from_slot == into_slot:
            return self._reject(command, Reason.BAD_PARAMETER, "两个槽不能是同一个")
        try:
            source_anchor = SlotAnchor(
                from_slot, self._key_from(payload), payload.get("slot_version")
            )
            target_view = endpoint.slot_view(into_slot)
            target_key = (
                ResourceKey.from_dict(target_view["key"])
                if target_view["key"] is not None
                else source_anchor.expected_key
            )
            target_anchor = SlotAnchor(
                into_slot, target_key, payload.get("into_slot_version")
            )
        except (TypeError, ValueError, KeyError) as error:
            return self._reject(command, Reason.BAD_PARAMETER, "槽锚不合法：%s" % (error,))

        op_id = payload.get("op_id") or command.request_id
        view = endpoint.slot_view(source_anchor.slot)
        count = min(view["count"], int(payload.get("count") or view["count"]))
        if count <= 0:
            return self._reject(command, Reason.INSUFFICIENT_STOCK, "源槽是空的")

        ok, reason, detail = endpoint.anchor_verdict(source_anchor)
        if not ok:
            return CommandResult.rejected(
                command.request_id, [BlockReason(reason, detail)]
            )
        ok, reason, detail = endpoint.anchor_verdict(target_anchor)
        if not ok and target_view["key"] is not None:
            return CommandResult.rejected(
                command.request_id, [BlockReason(reason, detail)]
            )
        # 先取后放，两次都锚定。取出的那部分若放不进去，**退回原槽**——
        # 这比留在「手里」强，因为手里不属于任何一处权威状态。
        taken = endpoint.anchored_extract(op_id + ":out", source_anchor, count)
        if taken.moved <= 0:
            return self._receipt_result(command, taken)
        placed = endpoint.anchored_insert(op_id + ":in", target_anchor, taken.moved)
        if placed.moved < taken.moved:
            undo = endpoint.anchored_insert(
                op_id + ":undo", source_anchor, taken.moved - placed.moved
            )
            if undo.moved < taken.moved - placed.moved:
                return CommandResult.blocked(
                    command.request_id,
                    [
                        BlockReason(
                            Reason.CONTAINER_UNCERTAIN,
                            "合并只完成了一部分，且有 %d 个物品没能退回原槽"
                            % (taken.moved - placed.moved - undo.moved,),
                        )
                    ],
                )
        return self._receipt_result(
            command, placed if placed.moved >= taken.moved else
            TransferReceipt(
                op_id, count, placed.moved, TransferStatus.PARTIAL,
                Reason.CONTAINER_PARTIAL, taken.resource_key, endpoint.endpoint_id(),
            )
        )

    @staticmethod
    def _key_from(payload):
        # type: (dict) -> object
        raw = payload.get("key")
        return ResourceKey.from_dict(raw) if isinstance(raw, dict) else None

    def _cmd_inv_quick_move(self, command, payload):
        # type: (object, dict) -> CommandResult
        """快捷转移：把某个槽整槽搬走，方向由 `target` 决定。

        `target == "network"` 走 `inv_take`（进网络），
        `target == "backpack"` 走 `inv_put`（从网络回背包）。
        载荷里的 `count` 缺省为整槽——快捷转移的语义就是「整槽」。
        """
        target = payload.get("target")
        if target not in ("network", "backpack"):
            return self._reject(
                command, Reason.BAD_PARAMETER, "target 必须是 network 或 backpack"
            )
        if not payload.get("count"):
            endpoint, refusal = self._inventory_endpoint_for(command, payload)
            if refusal is not None:
                return refusal
            slot = payload.get("slot")
            if slot is None:
                return self._reject(command, Reason.BAD_PARAMETER, "快捷转移必须给出 slot")
            view = endpoint.slot_view(slot)
            if view["count"] <= 0:
                return self._reject(command, Reason.INSUFFICIENT_STOCK, "该槽是空的")
            payload = dict(payload)
            payload["count"] = view["count"]
            payload["key"] = payload.get("key") or view["key"]
        if target == "network":
            return self._transfer_payload(command, payload, DIRECTION_READ)
        return self._transfer_payload(command, payload, DIRECTION_WRITE)

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
