# coding=utf-8
"""终端同步中枢：订阅、发布窗口、快照与增量（11-02）。

对应 `terminal_item_index.md` 第 5、6 节。本模块**不 import 引擎**：
仓储读取走注入的 `storage_of`，玩家与设备的既有事实走注入的 `context_of`，
发送走注入的 `port`。于是整条发布路径可以离线驱动——喂几次 `tick()`、
看发出了什么包，就够了。

## 发布窗口为什么由 `tick()` 驱动，而不是调度器

`SchedulerService.schedule(ref, kind, delay, period, handler, owner)` 有三处
不合用：

1. `owner` 必须是已装配的 service_id，而 lane 是**每设备**的——任选一台
   成员设备做宿主，它一被拆就整网停摆；
2. `RuntimeManager.reconcile` 的 `cancel_all(ref)` 会静默掐掉窗口；
3. `period` 的单位是逻辑帧，等于把「100～200ms 仅是首轮测试配置」这个
   **待测量**固化进另一个模块。

所以窗口挂在 `ServerBootstrap.tick()` 里、**紧跟 `runtimes.tick()`**：提交
发生在 `runtimes.tick` 内，发布放其后，窗口的 `take_dirty_keys()` 才看得见
本帧提交。

## 空闲零成本

只遍历**有订阅者的**网络。因此 `StorageService.tick()` 那句「无人订阅时
`take_dirty_keys()` 不会被调用」继续成立：没有会话的网络连碰都不碰，
`publish_version` 不动，也不产生任何包。

## 主线程边界就在 `take_dirty_keys()` 这一句

`terminal_item_index.md` §6.1 要求「先发布现有待合并变化，使视图版本与当前
索引一致，再固定快照内容，中间不允许插入其他提交」。在这个单线程 tick 里，
`take_dirty_keys()` **本身就是**那个原子点——排序、清空、推进到 V 在同一条
语句序列里完成，中间没有 yield，插不进任何提交。之后到达的提交落到下一个
窗口，其 `base_version` 就是这里推到的 V，不丢也不重。

## 共享的是冻结的记录与分片边界，不是线上编码

§5.4 说「相同网络/代次/视图且基准版本一致的订阅者共享不可变批次与编码
结果」。本条实现里共享的是 **`FrozenSnapshot.records` / `chunks` 与批次
的记录本身**；**线上编码无法共享**，因为资源序号是按会话发的
（`sessions.py` 有说明：一张共享的序号表会让中途加入的会话拿到它没见过
描述、因而无法解码的序号）。同版本的多名订阅者省掉的是复制与切分，包体
构造仍各做各的——字节数不会因为「共享」而消失。
"""

from ...common.protocol import (
    BlockReason,
    CommandResult,
    CommandResultMessage,
    CommandSource,
    CommandKind,
    CraftStateMessage,
    DeviceCommand,
    ItemDelta,
    InventorySnapshot,
    Reason,
    ResyncRequired,
    SessionReady,
    SnapshotBegin,
    SnapshotChunk,
    SnapshotEnd,
    SyncEnvelope,
    SyncError,
    MSG_ACK,
    MSG_COMMAND,
    MSG_SESSION_CLOSE,
    MSG_SESSION_OPEN,
    MSG_SNAPSHOT_REQUEST,
    VIEW_ITEM,
)
from ...common.resource_sync import ResourceResyncRequired
from ..network.runtime import RuntimeChange
from ..network.ticking import RotationCursor
from .device_status import (
    DeviceStatusSubscriptions,
    build_status_message,
    read_status,
)
from .sessions import SESSION_ACTIVE, SESSION_SNAPSHOTTING, SessionRegistry
from .transfer import DeltaBatch, FrozenSnapshot, build_delta_packets  # noqa: F401

# -- 重取快照的原因码 ------------------------------------------------------
#
# 客户端收到 `resync_required` 一律丢弃当前缓存，因此每个码都要能指向
# 一个具体的、服务端确实观察到的情形；「不知道为什么会这样」不该是一个码。

#: 客户端报的版本与我们保留的历史接不上（缺口太老）。
RESYNC_VERSION_GAP = "version_gap"
#: 快照在途时到达的增量超出了每会话暂存上限。
RESYNC_PENDING_OVERFLOW = "pending_overflow"
#: 已发出未确认的数据超出每会话积压上限（慢客户端）。
RESYNC_BACKLOG_OVERFLOW = "backlog_overflow"
#: 确认里提到的传输我们已经不认识了（历史被回收或传输早已终止）。
RESYNC_UNKNOWN_TRANSFER = "unknown_transfer"
#: 本会话的资源序号表满了——它引用了太多不同的键。
RESYNC_RESOURCE_TABLE_FULL = "resource_table_full"
#: 该网络当前没有可读的存储服务。
RESYNC_NO_STORAGE = "no_storage"

# -- 运行时变化导致的注销原因 ----------------------------------------------
#
# 三种变化都让**整网**的会话作废，但作废的理由不同，而 `Session.close_reason`
# 是实机上唯一能回答「这个界面为什么自己关了」的东西。合成一个字符串的代价
# 是查的时候只能看到「网络变了」——而「主控被拆」与「网络在重建」要查的
# 东西完全不一样（一个查方块还在不在，一个查重建为什么没结束）。

#: 分量消失（合并、拆分后退役、整网被拆光）。
CLOSE_NETWORK_RETIRED = "network_retired"
#: 网络停止接受业务命令（重建中、恢复中）。
CLOSE_NETWORK_CLOSED = "network_closed"
#: 主控被移除或卸载。
CLOSE_MASTER_LOST = "master_lost"
#: 界面指着的方块已经被拆掉或换了一台（同坐标、代次变大）。**不属于上面那
#: 三种**：那三种关的是整网，这一条只关掉这一台设备上的会话。
CLOSE_DEVICE_REPLACED = "device_replaced"

#: `RuntimeChange.kind` → 注销原因。**只列会注销的那三种**，其余变化
#: 在这个映射里查不到，因此它们不会顺手关掉任何会话。
_RUNTIME_CLOSE_REASONS = {
    RuntimeChange.RETIRED: CLOSE_NETWORK_RETIRED,
    RuntimeChange.CLOSED: CLOSE_NETWORK_CLOSED,
    RuntimeChange.MASTER_LOST: CLOSE_MASTER_LOST,
}

#: 注销原因 → 推给客户端的那句话。**与 `close_reason` 分开**：那个是给服务端
#: 诊断看的短标识（`master_lost`），这一句会原样落到玩家眼前，所以写成完整的
#: 中文。文本的唯一真源在服务端（`zh_CN.lang` 只管标识符 → 显示名），与
#: `device_status` 的原因文本同一条口径。
_GONE_DETAILS = {
    CLOSE_NETWORK_RETIRED: "所属网络已退役，请重新打开界面",
    CLOSE_NETWORK_CLOSED: "网络已关闭，请重新打开界面",
    CLOSE_MASTER_LOST: "网络主控已失去，请重新打开界面",
    #: `_drop_stale_devices` 专用。它不走 `_RUNTIME_CLOSE_REASONS`（那条路
    #: 关的是**整网**），所以单独列在这里。
    CLOSE_DEVICE_REPLACED: "终端已被替换，请重新打开界面",
}


class SyncPolicy(object):
    """同步的传输与缓存边界。**逐项待定**，来源见各字段注释。

    全部字段都不是「已验收指标」：`11. 终端与客户端同步.md` 页尾写明
    「请求大小、会话过期和分页上限由目标环境测量后确定」，而 §5 的
    「100～200ms 仅作为首轮合并窗口测试配置」正是这里没有任何时间字段的
    原因——窗口由调用方每帧推一次，本模块不持有周期。
    """

    __slots__ = (
        "publish_budget_keys",
        "snapshot_chunk_items",
        "snapshot_chunk_bytes",
        "snapshot_chunks_per_frame",
        "delta_items_per_packet",
        "delta_bytes_per_packet",
        "max_pending_delta_batches",
        "max_pending_delta_bytes",
        "max_backlog_items",
        "max_backlog_bytes",
        "history_batches",
        "history_bytes",
        "history_frames",
        "max_failures",
        "backoff_frames",
        "require_admission",
    )

    def __init__(
        self,
        publish_budget_keys=256,
        snapshot_chunk_items=64,
        snapshot_chunk_bytes=4096,
        snapshot_chunks_per_frame=4,
        delta_items_per_packet=64,
        delta_bytes_per_packet=4096,
        max_pending_delta_batches=64,
        max_pending_delta_bytes=65536,
        max_backlog_items=8192,
        max_backlog_bytes=524288,
        history_batches=32,
        history_bytes=262144,
        history_frames=600,
        max_failures=3,
        backoff_frames=20,
        require_admission=False,
    ):
        # 待定：每帧取多少脏键（跨网络轮转的预算）。
        self.publish_budget_keys = int(publish_budget_keys)
        # 待定：快照分片上限（条目数与字节数，任一触顶换片）。
        self.snapshot_chunk_items = int(snapshot_chunk_items)
        self.snapshot_chunk_bytes = int(snapshot_chunk_bytes)
        # 待定：每帧每个会话最多推进几片快照——把「一次发完整份」拆成
        # 若干帧，否则一台大网络首次打开终端就是一帧几百个包。
        self.snapshot_chunks_per_frame = int(snapshot_chunks_per_frame)
        # 待定：增量分片上限。
        self.delta_items_per_packet = int(delta_items_per_packet)
        self.delta_bytes_per_packet = int(delta_bytes_per_packet)
        # 待定：快照在途时每会话暂存多少批增量。溢出**不扩容**，
        # 而是终止传输、重取快照（`terminal_item_index.md` §5.6）。
        self.max_pending_delta_batches = int(max_pending_delta_batches)
        self.max_pending_delta_bytes = int(max_pending_delta_bytes)
        # 待定：每会话「已发未确认」的上限。响应正常的客户端每片都会确认，
        # 积压保持在很小的水平；只有跟不上的客户端才会堆到这里。
        self.max_backlog_items = int(max_backlog_items)
        self.max_backlog_bytes = int(max_backlog_bytes)
        # 待定：每网络保留的增量历史（条数/字节/帧数三重上限，任一触顶淘汰）。
        self.history_batches = int(history_batches)
        self.history_bytes = int(history_bytes)
        self.history_frames = int(history_frames)
        # 待定：连续失败几次就退避；退避多少帧。
        self.max_failures = int(max_failures)
        self.backoff_frames = int(backoff_frames)
        #: R13：只读物品视图**不重复判定准入**。终端只是看，恰好满载不该
        #: 让界面打不开——满载的影响留在详情解释里，不在这里另立一套。
        self.require_admission = bool(require_admission)

    def unresolved_report(self):
        # type: () -> list
        """仍带未定标签的字段。与 `BandwidthPolicy` / `MediumPolicy` 同形。

        **这些不是「还没调好的常数」，是「还没定案的规则」**：11.md 页尾
        写明请求大小、会话过期与分页上限由目标环境测量后确定，而 §5 的
        「100～200ms 仅作为首轮合并窗口测试配置」说的正是这里没有时间字段
        的原因（窗口由调用方每帧推一次）。逐项报出来，正式验收时才不会有人
        把当前值当成已定案的数值。
        """
        measured = "11-14 实测后确定；当前值是首轮测试配置，不是验收指标"
        return [
            {
                "field": "sync.publish_budget_keys",
                "rule": "11-14",
                "value": self.publish_budget_keys,
                "note": "每帧跨网络轮转取走的脏键上限；" + measured,
            },
            {
                "field": "sync.snapshot_chunk_items",
                "rule": "11-14",
                "value": self.snapshot_chunk_items,
                "note": "快照分片的条目上限；" + measured,
            },
            {
                "field": "sync.snapshot_chunk_bytes",
                "rule": "11-14",
                "value": self.snapshot_chunk_bytes,
                "note": "快照分片的字节上限（按 UTF-8 JSON 量）；" + measured,
            },
            {
                "field": "sync.snapshot_chunks_per_frame",
                "rule": "11-14",
                "value": self.snapshot_chunks_per_frame,
                "note": "每会话每帧推进的分片数；" + measured,
            },
            {
                "field": "sync.delta_items_per_packet",
                "rule": "11-14",
                "value": self.delta_items_per_packet,
                "note": "增量包的条目上限；" + measured,
            },
            {
                "field": "sync.delta_bytes_per_packet",
                "rule": "11-14",
                "value": self.delta_bytes_per_packet,
                "note": "增量包的字节上限；" + measured,
            },
            {
                "field": "sync.max_pending_delta_batches",
                "rule": "11-14",
                "value": self.max_pending_delta_batches,
                "note": "快照在途时每会话暂存的批数；溢出即重取快照，" + measured,
            },
            {
                "field": "sync.max_pending_delta_bytes",
                "rule": "11-14",
                "value": self.max_pending_delta_bytes,
                "note": "同上，按字节计的暂存上限；" + measured,
            },
            {
                "field": "sync.max_backlog_items",
                "rule": "11-14",
                "value": self.max_backlog_items,
                "note": "每会话已发未确认的条目上限（慢客户端）；" + measured,
            },
            {
                "field": "sync.max_backlog_bytes",
                "rule": "11-14",
                "value": self.max_backlog_bytes,
                "note": "同上，按字节计；" + measured,
            },
            {
                "field": "sync.history_batches",
                "rule": "11-14",
                "value": self.history_batches,
                "note": "每网络保留的增量历史条数；越短越容易触发断档重取，"
                        + measured,
            },
            {
                "field": "sync.history_bytes",
                "rule": "11-14",
                "value": self.history_bytes,
                "note": "同上，按字节计；" + measured,
            },
            {
                "field": "sync.history_frames",
                "rule": "11-14",
                "value": self.history_frames,
                "note": "同上，按帧数计（三重上限任一触顶即淘汰）；" + measured,
            },
            {
                "field": "sync.max_failures",
                "rule": "11-14",
                "value": self.max_failures,
                "note": "连续几次发送失败开始退避；" + measured,
            },
            {
                "field": "sync.backoff_frames",
                "rule": "11-14",
                "value": self.backoff_frames,
                "note": "退避多少帧；" + measured,
            },
            {
                "field": "sync.require_admission",
                "rule": "R13",
                "value": self.require_admission,
                "note": "只读物品视图不重复判定准入；替身取不判。R13 定案后"
                        "要连带核对「恰好满载是否影响查看」这一条",
            },
        ]


class NetworkSubscriptions(object):
    """一个网络的订阅者、冻结快照与增量历史。

    **最后一个订阅者离开就整个丢掉**（`drop_if_empty`）：留着它等于让
    一份没人看的记录随时间长大——泄漏不会报错，只是内存慢慢涨上去，
    而「什么时候该回收」在别处再判一次就容易与这里判得不一样。
    """

    __slots__ = (
        "network_id",
        "sessions",
        "snapshots",
        "snapshot_of_version",
        "history",
        "history_bytes",
        "publish_version",
        "last_publish_frame",
        "stats",
    )

    def __init__(self, network_id):
        self.network_id = network_id
        #: session_id -> Session。迭代一律经 `ordered_sessions()`，
        #: 不直接遍历这个字典（py2.7 的字典序不确定，会让包序随机）。
        self.sessions = {}
        #: snapshot_id -> FrozenSnapshot，以及 version -> snapshot_id 的共享表。
        self.snapshots = {}
        self.snapshot_of_version = {}
        #: 已发布的增量批次，按 `new_version` 递增排列。
        self.history = []
        self.history_bytes = 0
        #: 本网络已经发布出去的物品视图版本。**由本模块独占推进**
        #: （只有它会调 `take_dirty_keys()`），因此它可以当作「客户端
        #: 手里应该是哪个版本」的基准。
        self.publish_version = 0
        self.last_publish_frame = 0
        self.stats = {"batches": 0, "keys": 0, "packets": 0, "snapshots": 0}

    # -- 订阅者 --------------------------------------------------------

    def add(self, session):
        # type: (object) -> None
        self.sessions[session.session_id] = session

    def discard(self, session_id):
        # type: (str) -> bool
        return self.sessions.pop(session_id, None) is not None

    def is_empty(self):
        # type: () -> bool
        return not self.sessions

    def ordered_sessions(self):
        # type: () -> list
        """按会话序号排序的订阅者。

        包按这个顺序发出，因此同样的输入永远产生同样的发送顺序——
        「这次是谁先收到」不会因为字典序在两次运行间变化而变。
        """
        return sorted(self.sessions.values(), key=lambda item: item.serial)

    def __repr__(self):
        return "NetworkSubscriptions(%s, %d 个会话)" % (
            self.network_id,
            len(self.sessions),
        )

    # -- 快照 ----------------------------------------------------------

    def share_snapshot(self, frozen):
        # type: (FrozenSnapshot) -> FrozenSnapshot
        """登记一份冻结快照。**同版本直接复用已有那份**，不重复复制。"""
        existing = self.snapshot_of_version.get(frozen.version)
        if existing is not None:
            return self.snapshots[existing]
        self.snapshots[frozen.snapshot_id] = frozen
        self.snapshot_of_version[frozen.version] = frozen.snapshot_id
        self.stats["snapshots"] += 1
        return frozen

    def snapshot_for(self, version):
        # type: (int) -> FrozenSnapshot
        snapshot_id = self.snapshot_of_version.get(version)
        if snapshot_id is None:
            return None
        return self.snapshots.get(snapshot_id)

    def snapshot_by_id(self, snapshot_id):
        # type: (str) -> FrozenSnapshot
        return self.snapshots.get(snapshot_id)

    def referenced_snapshots(self):
        # type: () -> set
        return set(
            session.snapshot_id
            for session in self.sessions.values()
            if session.snapshot_id
        )

    def drop_unused_snapshots(self):
        # type: () -> int
        """丢掉没人引用的冻结快照。

        **当前版本那一份要留着**：同一版本上随时可能有第二个终端打开，
        它要共享这份而不是再复制一遍（§6 末段）。留着的那一份在下一次
        版本推进后自然变成「非当前」，那时再没人引用就会被清掉。
        """
        keep = self.referenced_snapshots()
        doomed = [
            snapshot_id
            for snapshot_id, snapshot in self.snapshots.items()
            if snapshot_id not in keep and snapshot.version != self.publish_version
        ]
        for snapshot_id in doomed:
            snapshot = self.snapshots.pop(snapshot_id)
            if self.snapshot_of_version.get(snapshot.version) == snapshot_id:
                del self.snapshot_of_version[snapshot.version]
        return len(doomed)

    # -- 增量历史 ------------------------------------------------------

    def remember(self, batch, policy, frame):
        # type: (DeltaBatch, SyncPolicy, int) -> None
        self.history.append(batch)
        self.history_bytes += batch.bytes()
        self.publish_version = batch.new_version
        self.last_publish_frame = frame
        self.stats["batches"] += 1
        self.stats["keys"] += batch.key_count()
        self.stats["packets"] += len(batch.packets)
        self.trim_history(policy, frame)

    def trim_history(self, policy, frame):
        # type: (SyncPolicy, int) -> int
        """三重上限（条数/字节/帧数）任一触顶就从头淘汰。返回淘汰条数。

        三条都要：只限条数挡不住「每条都很大」，只限字节挡不住「每帧一条
        小包攒了一小时」，只限时间挡不住「一秒内来了一万条」。
        """
        dropped = 0
        while self.history:
            too_many = policy.history_batches > 0 and len(self.history) > policy.history_batches
            too_big = policy.history_bytes > 0 and self.history_bytes > policy.history_bytes
            too_old = (
                policy.history_frames > 0
                and frame - self.history[0].frame > policy.history_frames
            )
            if not (too_many or too_big or too_old):
                break
            if len(self.history) == 1:
                # 只留一条也超限时仍留它：把最后一条也扔掉会让「刚好落在
                # 边界上的客户端」永远补不上，而它本来只差一个批次。
                break
            oldest = self.history.pop(0)
            self.history_bytes -= oldest.bytes()
            dropped += 1
        return dropped

    def oldest_base(self):
        # type: () -> int
        """历史仍能覆盖到的最早基准版本。历史为空时返回当前版本。"""
        if not self.history:
            return self.publish_version
        return self.history[0].base_version

    def history_since(self, from_version):
        # type: (int) -> list
        """`from_version` 之后的全部批次；接不上则返回 None。

        **返回 None 与返回空列表是两件事**：前者是「我不认识这个版本」，
        后者是「你已经是最新的」。合并成一种，服务端就会在客户端其实
        落后时什么都不发，而客户端会一直以为自己是最新的。
        """
        if from_version == self.publish_version:
            return []
        if from_version < self.oldest_base() or from_version > self.publish_version:
            return None
        out = []
        expected = from_version
        for batch in self.history:
            if batch.new_version <= from_version:
                continue
            if batch.base_version != expected:
                return None
            out.append(batch)
            expected = batch.new_version
        if expected != self.publish_version:
            return None
        return out

    def drop_if_empty(self):
        # type: () -> bool
        """没有订阅者了就把缓存整个丢掉。**是真的清**，不是标记。"""
        if self.sessions:
            return False
        self.snapshots = {}
        self.snapshot_of_version = {}
        self.history = []
        self.history_bytes = 0
        return True


class TerminalSyncHub(object):
    """会话 + 发布窗口 + 传输推进。

    四个注入的依赖，都是为了让本模块与引擎、拓扑、主控解耦：

    - `storage_of(network_id)` → `StorageService` 或 None。**无门槛只读
      路径**，先例见 `application.py` 的 `debug_query`（它同样走
      `runtime.services.get(...)` 而不订阅服务、不碰 `ServiceId.ALL`）。
    - `context_of(player_id, device_ref)` → `SessionContext` 或 None。
    - `port`：`send / listen / source_player_id / on_player_leave`。
    - `submit(command)` → `CommandResult`。**手动存取命令的出口**（11-03）。

    `submit` 走注入而不是持有 `ApplicationService`，理由与另外三个不同：
    那是**分层禁令**要求的，不只是解耦偏好。`server/sync/` 在
    `test_import_boundaries.py` 的 `DOMAIN_PREFIXES` 里，不得 import
    `tooldelta` / `mod.*`；而命令真正要经过的主控在 `server/network/`，
    注入一个回调就让本模块连 `controller.py` 都不必认识。副作用是它的
    可测性：喂一个假 `submit` 就能断言「命令是怎么拼出来的」。
    """

    def __init__(
        self,
        registry=None,
        policy=None,
        storage_of=None,
        context_of=None,
        port=None,
        submit=None,
        status_of=None,
        craft_of=None,
    ):
        self.registry = registry if registry is not None else SessionRegistry()
        self.policy = policy if policy is not None else SyncPolicy()
        self._storage_of = storage_of if storage_of is not None else _no_storage
        self._context_of = context_of if context_of is not None else _no_context
        self._submit = submit if submit is not None else _no_submit
        self._status_of = status_of if status_of is not None else _no_status
        #: 合成终端的工作台（11-04）。**只有合成终端才会有值**——
        #: 装配层按方块型号决定，其它终端一律拿到 `None`，
        #: 因此这里不需要再判一次「这是不是合成终端」。
        self._craft_of = craft_of if craft_of is not None else _no_craft
        self.port = port
        self._subs = {}
        self._inventory_sent = {}
        #: 每会话上一次发出去的工作台状态，用来做变化去重。与
        #: `_inventory_sent` 一样在 `_forget` 里清。
        self._craft_sent = {}
        #: 设备总体状态（11-08/11-13）。订阅表就是 `registry`，这里只记
        #: 「每会话上一次发出去的是什么」，用来做变化去重。
        self.status = DeviceStatusSubscriptions()
        self._rotation = RotationCursor()
        self._next_batch = 1
        self._next_snapshot = 1
        #: 发布窗口的帧号。**本模块自己数**：引擎帧号是调用方给的、可能
        #: 是个字符串令牌（`runtimes.tick` 就用 `"manager#N"`），拿它做
        #: TTL 与退避的算术会静默出错。
        self._frame = 0
        self.stats = {
            "frames": 0,
            "windows": 0,
            "published_keys": 0,
            "packets": 0,
            "snapshots": 0,
            "resyncs": 0,
            "send_failures": 0,
            "transport_errors": 0,
            "sessions_opened": 0,
            "sessions_refused": 0,
            #: 空闲超时回收时**实际告诉客户端的条数**，以及**因为「这个玩家
            #: 还有别的活会话」而没发的条数**（见 `_notify_abandoned`）。
            #: 两个数分开，才答得了「客户端为什么没被通知」；回收总数在
            #: `registry.stats["expired"]`，三个数的关系是
            #: `expired == expired_notices + expired_notices_skipped + 发失败的`。
            "expired_notices": 0,
            "expired_notices_skipped": 0,
            #: 除空闲超时之外那几条**服务端单方面注销**的通知
            #: （设备被替换、网络退役/关闭/换主控），见 `_notify_gone`。
            #: 与 `expired_notices` 分开计：这两类在实机上要查的东西不同
            #: （「这个人走开了」vs「这台方块/这条网络没了」）。条数分母是
            #: `registry.stats["closed"]`，但那个数还含玩家主动关闭与停机。
            "gone_notices": 0,
            "gone_notices_skipped": 0,
            #: 其中有多少条是**开着之后**复核不过而关掉的（走开、传走、
            #: 设备被换、网络不可用）。与开会话被拒分开计：前者是「这个人
            #: 现在离得远」，后者是「这条请求本来就开不了」——查的东西不同。
            "revalidate_refused": 0,
            "networks_dropped": 0,
            # 「服务在但只读口不全」与「版本链被别人推进过」都不是错误，
            # 但从现象上看都是「什么都没发出去」。计数留着，否则查不出来。
            "storage_unreadable": 0,
            "version_chain_breaks": 0,
            # 解析不出来的包。**与「有没有包发过来」分开计**：那个数在适配器
            # 的 `AdapterSyncPort.stats["requests"]` 上。「一个包都没收到」与
            # 「收到的都读不懂」是两件不同的事，前者查总线，后者查版本。
            "bad_packets": 0,
            # 手动存取命令（11-03）。`commands` 只数**走到执行**的那些，
            # 回绝在 `command_rejected`——两者相加才是「玩家按了多少次」，
            # 而分开计才能回答「按了没反应」到底是没收到还是被拒了。
            "commands": 0,
            "command_rejected": 0,
            #: 执行路径抛异常的次数。**必须回一条结果**：客户端没有时钟也
            #: 没有超时，不回则它在途记录永不释放（见 `_on_command`）。
            "command_errors": 0,
            #: 推出去的工作台状态条数（11-04）。**只数真的发出去的**：
            #: 去重命中不算，因此这个数涨就说明格子或预览真的变了。
            "craft_states": 0,
        }

    # -- 装配 ----------------------------------------------------------

    @staticmethod
    def network_id_of(component_id):
        # type: (object) -> str
        """分量编号的**线上形态**。全项目只有这一处做这个转换。

        分量编号是个整数（`TopologyService._allocate_component_id`），而
        `SyncEnvelope` 对 S2C 消息要求 `network_id` 是**非空文本**。不转的话
        每一条 `session_ready` 都会在编码时被判非法，症状是「界面一开就报错」
        而错误信息说的是「network_id 必须是文本」——与「编号是整数」这件
        事隔着好几层。

        写成**一处**而不是在各调用点各转一次：`self._subs` 的键、
        `Session.network_id`、`ResourceDescriptions(network_id, ...)` 里的
        那个字符串必须是同一个值。转两处就有转出两种写法的余地，而它们
        不相等时表现为「订阅表里有两个网络、其中一个永远是空的」。
        """
        return "%s" % (component_id,)

    def attach(self, port):
        # type: (object) -> None
        self.port = port

    def accepts(self, source):
        # type: (object) -> bool
        """交给 `MasterController.session_check` 的那个探针。"""
        return self.registry.accepts(source)

    def context_of(self, player_id, device_ref):
        # type: (str, object) -> object
        return self._context_of(player_id, device_ref)

    # -- 请求入口 ------------------------------------------------------

    def receive(self, player_id, data):
        # type: (str, object) -> object
        """C2S 的**线上入口**：原始字典进，编码好的回包（或 `None`）出。

        **解析与编码都在这里，不在适配器里。** 编码要用该会话的资源描述表
        （序号是逐会话发的），而那张表只有 hub 拿得到；让适配器去
        `registry.get(...)` 取表，等于把会话表的内部结构摊给引擎边界。

        坏包**不抛也不回错**：这是引擎事件路径，抛出去打挂的是这一帧；
        而连信封都解析不出来时，「回给谁、按哪个请求号回」都无从谈起——
        回一条无名无姓的 `error` 只会让界面显示「出错了」而说不出是哪次。
        计数留在 `stats["bad_packets"]`：它非零就是**版本不匹配或对面发了
        脏数据**，而不是「什么都没发生」。
        """
        try:
            envelope = SyncEnvelope.from_dict(data)
        except (ValueError, TypeError, KeyError, AttributeError):
            self.stats["bad_packets"] += 1
            return None
        reply = self.handle(envelope, player_id)
        if reply is None:
            return None
        session = self.registry.get(reply.session_id)
        descriptions = session.descriptions if session is not None else None
        return reply.to_dict(descriptions)

    def handle(self, envelope, player_id, frame=None):
        # type: (object, str, int) -> object
        """处理一条 C2S 同步消息，返回要回给该玩家的 `SyncEnvelope` 或 None。

        `player_id` **由调用方从引擎事件上取**（`__id__`），不从信封里读；
        本方法签名里根本没有「信封里的玩家」这一项，因此没有取错的机会。
        """
        frame = self._frame if frame is None else frame
        kind = envelope.message.kind
        if kind == MSG_SESSION_OPEN:
            return self._open(envelope, player_id, frame)
        # 请求号要带上：这种情况下信封里没有可填的会话与网络，客户端就只剩
        # `request_id` 这一个抓手；丢了它，`error` 就成了一条无名无姓的通知，
        # 界面只能显示「出错了」而说不出是哪一次请求出的错。
        request_id = getattr(envelope.message, "request_id", "")
        session = self.registry.get(envelope.session_id)
        if session is None:
            return self._error(
                "",
                request_id,
                Reason.SESSION_INVALID,
                "会话不存在或已关闭",
            )
        if session.player_id != player_id:
            # 别人的会话：**不告诉他这条会话存在**，与「不存在」同一个回答。
            self.stats["sessions_refused"] += 1
            return self._error(
                "",
                request_id,
                Reason.SESSION_INVALID,
                "会话不存在或已关闭",
            )
        if kind == MSG_SESSION_CLOSE:
            # 关会话**不做复核**：客户端说要走，那就让它走。复核失败也只会
            # 走到同一个 `close_session`，中间平白多回一条错误。
            session.touch(frame)
            self.close_session(session.session_id, "client_closed")
            return None
        verdict = self._revalidate(session, player_id)
        if not verdict.ok:
            # 判据不过就**关掉**，不是只拒这一条：距离/维度/设备哪一条不成立，
            # 会话都不该继续活着——留着它，发布窗口会继续把库存组包发给一个
            # 已经站在远处（或换了维度）的客户端，而那在实机上看起来只是
            # 「界面还开着」，直到 TTL 到点。
            self.stats["sessions_refused"] += 1
            self.stats["revalidate_refused"] += 1
            self.close_session(
                session.session_id, "revalidate:%s" % (verdict.reason,)
            )
            return self._error("", request_id, verdict.reason, verdict.detail)
        session.touch(frame)
        if kind == MSG_SNAPSHOT_REQUEST:
            return self._on_snapshot_request(session, envelope.message, frame)
        if kind == MSG_ACK:
            return self._on_ack(session, envelope.message, frame)
        if kind == MSG_COMMAND:
            return self._on_command(session, envelope.message, frame)
        return self._error(
            session.session_id,
            getattr(envelope.message, "request_id", ""),
            Reason.UNSUPPORTED,
            "该消息不由物品视图处理：%r" % (kind,),
        )

    def _revalidate(self, session, player_id):
        # type: (object, str) -> object
        """每请求复核一个已存在的会话，返回 `SessionCheckResult`。

        **每条非 `session_open` 的消息都要过这里**（确认、取快照、关闭以外的
        全部）。不查的代价不是报错，是服务端持续把库存内容发给一个已经走出
        交互距离、或已经换了维度的客户端——它看起来只是「界面还开着」。

        代价是每条消息一次上下文解析，其中两次是引擎查询（维度、脚部坐标）。
        确认包的频率由发布窗口决定（每批一条），不是每帧每键一条，因此这个
        开销有界。**不为此加一个「隔几帧查一次」的旋钮**：那是一个没有测量
        依据的新数值，而它挡住的正是这条校验本身——漏查的那几帧里，包照发。
        """
        context = self._context_of(player_id, session.device_ref)
        return self.registry.revalidate(session, context)

    def _open(self, envelope, player_id, frame):
        # type: (object, str, int) -> object
        request = envelope.message
        context = self._context_of(player_id, request.device_ref)
        if context is None:
            self.stats["sessions_refused"] += 1
            return self._error(
                "", request.request_id, Reason.WRONG_NETWORK, "该终端当前不属于任何网络"
            )
        session, result = self.registry.open(envelope, context, frame)
        if session is None:
            self.stats["sessions_refused"] += 1
            return self._error("", request.request_id, result.reason, result.detail)
        self.stats["sessions_opened"] += 1
        storage = self._storage_of(session.network_id)
        index_version = 0
        view_version = 0
        if storage is not None:
            index_version = getattr(storage, "index_version", 0)
            view_version = getattr(storage, "publish_version", 0)
        subs = self._subs.get(session.network_id)
        if subs is None:
            subs = NetworkSubscriptions(session.network_id)
            # **新建的订阅表要把版本基准对齐到存储的当前版本。** 最后一个
            # 会话离开时整张表被丢掉（连同 `publish_version`），下一个会话
            # 会重建它——不在这里对齐的话，重建出来的基准是 0 而存储早就
            # 走到 7 了，第一个批次于是声称 `0 -> 8`：一个纯粹编造的版本链，
            # 客户端会发现缺口并永远重取快照。
            subs.publish_version = view_version
            self._subs[session.network_id] = subs
        subs.add(session)
        # **开局就让客户端对齐到当前版本，而不是从 0 开始追**：会话刚建立，
        # 客户端手里什么都没有，此刻的视图版本就是它将要拿到的那份快照版本。
        session.item_view_version = view_version
        # 新会话从一份新快照开始（§5.5：首次订阅从新快照开始）。
        session.must_resync = True
        return SyncEnvelope(
            SessionReady(index_version, view_version),
            session_id=session.session_id,
            network_id=session.network_id,
            epoch=session.epoch,
            view_kind=session.view_kind,
        )

    def _on_snapshot_request(self, session, request, frame):
        # type: (object, object, int) -> object
        subs = self._subs.get(session.network_id)
        if subs is None:
            return None
        batches = subs.history_since(request.from_version)
        if batches is not None:
            # 接得上：只补增量，省掉一次整份复制与传输。
            #
            # **以客户端自报的位置为准，而不是以 `session.item_view_version`。**
            # 后者是服务端「我发到哪儿了」的记账，前者才是「你手里有什么」的
            # 事实——客户端发现自己缺包来补的时候，这两者本来就该不同，而要求
            # 它们相等等于把「补发」这条路整个堵死（缺口只能靠重取快照补）。
            # `from_version` 不是随便就能报的：它要落在历史区间内、且能拼出一条
            # 不断裂的链，否则 `history_since` 返回 None，走下面重取快照那条。
            #
            # 采纳自报位置之后必须**回写**会话的记账，否则 `_deliver_batch` 会
            # 拿批次的 `base_version`（= 自报位置）与它比较，判成版本缺口而中止
            # ——补发在第一步就自我否决了。
            session.must_resync = False
            session.item_view_version = request.from_version
            for batch in batches:
                self._deliver_batch(session, batch, frame)
            return None
        session.must_resync = True
        session.backoff_until_frame = 0
        return None

    def _on_ack(self, session, ack, frame):
        # type: (object, object, int) -> object
        entry = session.outstanding.get(ack.transfer_id)
        if entry is None:
            self._abort(
                session,
                RESYNC_UNKNOWN_TRANSFER,
                "确认提到了一次我们不认识的传输：%s" % (ack.transfer_id,),
            )
            return None
        session.touch(frame)
        session.fail_count = 0
        packets = entry["packets"]
        last = len(packets) - 1
        if ack.sequence > last:
            # 序号越界说明对面在按另一个传输的进度的说话，不能按它扣账。
            self._abort(
                session,
                RESYNC_UNKNOWN_TRANSFER,
                "确认序号 %d 超出传输 %s 的范围（0..%d）"
                % (ack.sequence, ack.transfer_id, last),
            )
            return None
        for index in range(entry["acked"] + 1, ack.sequence + 1):
            items, size = packets[index]
            session.backlog_items -= items
            session.backlog_bytes -= size
        entry["acked"] = ack.sequence
        if entry["acked"] >= last:
            del session.outstanding[ack.transfer_id]
        return None

    # -- 手动存取（11-03）----------------------------------------------

    def _on_command(self, session, message, frame):
        # type: (object, object, int) -> object
        """受理一条手动存取命令，把结果作为 `command_result` 发回。

        **走到这里时会话已经被复核过了**：`handle` 对每条非 `session_open`
        的消息都先跑 `_revalidate`（距离、维度、设备代次），不过则关会话并
        回 `error`。因此本方法不再重复校验目标——`session.device_ref` 就是
        刚刚验过的那一台。

        ## 来源取会话，不取载荷

        `CommandSource` 的 `player_id` / `device_ref` / `dimension` 全部来自
        `session`。载荷里根本没有对应字段（`TerminalCommand` 的文档列了
        理由），所以「客户端自报身份」在这一层不是「被忽略」，而是
        **不可能表达**——想塞进来会被 `_require_fields` 判成未知字段而
        拒收整条消息。

        ## `topology_version=None` 是有意的

        主控能拒绝「基于旧图」的命令，那需要调用方给出它依据的图版本。
        本模块手里**没有**权威的图版本源，而 `TerminalCommand` 里那个
        版本是 `expected_index_version`（交易版本，属存储服务）。把后者
        填进 `topology_version` 就是把两个版本混成一个——`protocol.py`
        本节开头明禁的那种做法，且它不会报错，只会让主控按错误的依据
        放行或拒绝。`MasterController` 只在非 None 时比对版本，None 合法。

        ## 业务失败也是「答案」

        `blocked` / `rejected` / `stale` / `partial` 一律走 `command_result`
        正常回给客户端——它们是**结论**，界面据此提示玩家。`error` 仍然
        只留给会话与信道级的拒收（会话不存在、复核不过）。

        执行路径抛异常时**也必须回一条**：客户端没有时钟也没有超时，
        不回则它的在途记录永不释放（`NetworkClient._in_flight`），
        表现为「按了一次之后这个界面再也发不出命令」。
        """
        self.stats["commands"] += 1
        command = DeviceCommand(
            message.request_id,
            CommandKind.STORAGE,
            CommandSource(
                session.player_id,
                session.device_ref,
                session.dimension,
                distance_ok=True,
                session_id=session.session_id,
                received_tick=frame or 0,
            ),
            payload=message.payload(),
            topology_version=None,
        )
        try:
            result = self._submit(command)
        except Exception as error:
            self.stats["command_errors"] += 1
            return self._error(
                session.session_id,
                message.request_id,
                Reason.INTERNAL,
                "命令执行异常：%s" % (error,),
            )
        if not isinstance(result, CommandResult):
            # 注入接错时的兜底。静默当成成功会让界面显示一次没发生的存取，
            # 而那是**账实不符**里最难查的一种：库存没动，界面说动了。
            self.stats["command_errors"] += 1
            return self._error(
                session.session_id,
                message.request_id,
                Reason.INTERNAL,
                "命令执行方返回了非 CommandResult：%r" % (type(result),),
            )
        if not result.is_applied():
            self.stats["command_rejected"] += 1
        self._send(
            session, CommandResultMessage(message.request_id, result.to_dict())
        )
        return None

    # -- 推进 ----------------------------------------------------------

    def tick(self, context=None, budget=None):
        # type: (dict, int) -> int
        """推进一步：时效回收 + 传输推进 + 发布窗口。返回本帧发布的脏键数。

        **必须紧跟 `runtimes.tick()`**：提交发生在那里，发布放其后，
        窗口才看得见本帧提交。

        时效回收那一步**顺带通知客户端**（`_notify_abandoned`）：光把会话
        从注册表里抹掉，客户端不会知道——见该方法的说明。
        """
        self._frame += 1
        frame = self._frame
        self.stats["frames"] += 1

        expired = self.registry.evict_expired(frame)
        for session in expired:
            self._forget(session)
        if expired:
            self._notify_abandoned(expired)

        active = [
            network_id
            for network_id in sorted(self._subs)
            if not self._subs[network_id].is_empty()
        ]
        if not active:
            return 0

        limit = self.policy.publish_budget_keys if budget is None else int(budget)
        ring, share = RotationCursor.plan_frame(
            active, limit, self._rotation.cursor(), self._rotation.min_share_floor
        )
        allocations = []
        spent = 0
        remaining = limit
        for index in ring:
            if remaining <= 0:
                break
            network_id = active[index]
            grant = min(share, remaining)
            cost = self._service_network(network_id, frame)
            allocations.append((network_id, grant, cost))
            spent += cost
            remaining -= cost
        self._rotation.note_frame(allocations)
        self._rotation.advance()
        return spent

    def _service_network(self, network_id, frame):
        # type: (str, int) -> int
        """推进一个网络的传输，然后开一次发布窗口。返回窗口取走的脏键数。

        **两件事共用一次访问**：传输推进（快照分片、暂存增量落盘、退避
        到期）与发布窗口都要遍历同一批会话，分成两个循环会在两者之间
        插入「刚发完的包又被同一个 tick 重算一遍」的机会。
        """
        subs = self._subs.get(network_id)
        if subs is None:
            return 0
        for session in subs.ordered_sessions():
            if session.is_open():
                self._publish_inventory(session)
                self._publish_device_status(session)
                self._publish_craft_state(session)
                self._advance_transfer(session, subs, frame)
        return self._publish_network(subs, frame)

    def _publish_inventory(self, session):
        # type: (object) -> None
        storage = self._storage_of(session.network_id)
        provider = getattr(storage, "inventories", None)
        if provider is None:
            return
        try:
            endpoint = provider.endpoint_of(session.player_id)
            if endpoint is None or not endpoint.can_read():
                return
            view = endpoint.inventory_view()
            rows = [dict(slot=row["slot"], canonical=row["canonical"],
                         count=row["count"], slot_version=row["version"])
                    for row in view["slots"]]
            message = InventorySnapshot(view["revision"], rows)
        except Exception:
            # 读取失败不伪造空背包；客户端旧版本仍由命令入口复核。
            return
        payload = message.to_dict()
        if self._inventory_sent.get(session.session_id) == payload:
            return
        if self._send(session, message):
            self._inventory_sent[session.session_id] = payload

    def _publish_craft_state(self, session):
        # type: (object) -> None
        """把**这台设备自己的九宫格**推给客户端（11-04）。

        与 `_publish_inventory` 同一形状：内容没变就不发（按整个载荷去重）。
        区别在数据来源——背包是**玩家**的，九宫格是**这台设备**的，
        因此问的是 `craft_of(session.device_ref)` 而不是存储服务的
        `inventories`。装配层对非合成终端返回 `None`，这里直接返回。

        预览（`preview`）由装配层算好一并给出：它要查引擎配方表，
        而本模块不许 import 引擎。
        """
        try:
            state = self._craft_of(session.device_ref)
        except Exception:
            # 读取失败不伪造一个空工作台——空工作台会被客户端读成
            # 「我的原料没了」。宁可不发。
            return
        if state is None:
            return
        try:
            message = CraftStateMessage(
                state["revision"], state["slots"],
                state.get("recipe") or "", state.get("preview"),
            )
        except (KeyError, TypeError, ValueError):
            return
        payload = message.to_dict()
        if self._craft_sent.get(session.session_id) == payload:
            return
        if self._send(session, message):
            self._craft_sent[session.session_id] = payload
            self.stats["craft_states"] += 1

    def _publish_device_status(self, session):
        # type: (object) -> None
        """把**这台会话自己的设备**的总体状态推给客户端（11-08/11-13）。

        与 `_publish_inventory` 共用同一次逐会话访问、同一个变化去重手法，
        差别只在对象：库存是网络的，状态是**设备自己**的。搭在既有会话上
        而不是另开一条订阅，是因为客户端只持有一个会话（见 `network_client`
        的 `_absorb`：`session_id`/`epoch`/描述表都是单值），第二条会话会
        把物品列表打乱。于是「打开即快照、持续更新、关闭即清理」三条都由
        既有的会话生命周期保证，`_forget` 是唯一的清理点。

        读不到就**什么都不发**（不是发一份「离线」）：读取失败与确认离线
        是两件事，混起来会让界面在读取路径出问题时显示一个方向错误的结论。
        真正的「离线但还没确认」由领域层的 `validity="pending"` 表达。
        """
        status = read_status(session.device_ref, self._status_of)
        if status is None:
            return
        message = build_status_message(status)
        payload = message.to_dict()
        if self.status.unchanged(session.session_id, payload):
            return
        if self._send(session, message):
            self.status.remember(session.session_id, payload)

    def _advance_transfer(self, session, subs, frame):
        # type: (object, object, int) -> None
        if session.backoff_until_frame > frame:
            return
        if session.must_resync:
            self._start_snapshot(session, subs, frame)
            return
        if session.snapshot_id:
            self._send_snapshot_chunks(session, subs, frame)
            return
        if session.pending_deltas:
            self._flush_pending(session, frame)

    def _publish_network(self, subs, frame):
        # type: (object, int) -> int
        """一个网络的发布窗口。**没有订阅者就不会被调到这里。**

        返回取走的脏键数；无脏项时返回 0 且**版本不动**。
        """
        if subs.is_empty():
            return 0
        storage = self._storage_of(subs.network_id)
        if storage is None:
            return 0
        if not _publishable(storage):
            # 存储服务在但缺少只读口。**跳过而不是报错**：这一帧少发一次
            # 增量，比让一次 AttributeError 打挂整帧（后续所有网络全废）
            # 轻得多；计数留着，免得「一直没发」查不出来。
            self.stats["storage_unreadable"] += 1
            return 0
        if storage.publish_version != subs.publish_version:
            # 版本链断了——不是本模块推进的（否则两者恒等）。**必须让客户端
            # 重取**：继续按 `base=V` 发增量，客户端会发现缺口并反复重取，
            # 而每次都因为同一个原因失败，表现成「界面永远转圈」。
            self.stats["version_chain_breaks"] += 1
            self.resync_network(
                subs.network_id,
                RESYNC_VERSION_GAP,
                "发布版本与本模块记录的不一致（%d / %d）"
                % (storage.publish_version, subs.publish_version),
            )
            subs.publish_version = storage.publish_version
            return 0
        if not storage.has_dirty_items():
            return 0
        keys = storage.take_dirty_keys()
        if not keys:
            return 0
        batch = self._build_batch(subs, storage, keys, frame)
        subs.remember(batch, self.policy, frame)
        self.stats["windows"] += 1
        self.stats["published_keys"] += batch.key_count()
        for session in subs.ordered_sessions():
            if session.is_open():
                self._deliver_batch(session, batch, frame)
        return len(keys)

    def _build_batch(self, subs, storage, keys, frame):
        # type: (object, object, tuple, int) -> DeltaBatch
        present = dict(
            (record[0], record) for record in storage.read_items(keys)
        )
        upserts = []
        removes = []
        for key in keys:
            record = present.get(key)
            if record is None:
                # `take_dirty_keys()` 拿到的键可能已经不存在了（被取空、
                # 介质卸下）。**这是 remove，不是 upsert 0**：一条 0 的
                # upsert 会在客户端留下一个空条目。
                removes.append(key)
            else:
                upserts.append(record)
        batch_id = "b%d" % (self._next_batch,)
        self._next_batch += 1
        return build_delta_packets(
            batch_id,
            subs.publish_version,
            getattr(storage, "publish_version", subs.publish_version + 1),
            upserts,
            removes,
            self.policy.delta_items_per_packet,
            self.policy.delta_bytes_per_packet,
            frame,
        )

    # -- 快照 ----------------------------------------------------------

    def _start_snapshot(self, session, subs, frame):
        # type: (object, object, int) -> bool
        """冻结一份快照并发出 `snapshot_begin`。失败时安排退避重试。"""
        storage = self._storage_of(subs.network_id)
        if storage is None:
            self._defer(session, RESYNC_NO_STORAGE, "该网络当前没有可读的存储服务", frame)
            return False
        # §6.1：**先发布待合并变化**，使视图版本与当前索引一致，再固定快照。
        # 不这么做的话，快照会取到「比它的版本号更新的值」，之后的增量
        # 再叠上来虽然幂等，但「版本 V 的快照」这句话就不成立了。
        self._publish_network(subs, frame)
        version = getattr(storage, "publish_version", 0)
        frozen = subs.snapshot_for(version)
        if frozen is None:
            snapshot_id = "s%d" % (self._next_snapshot,)
            self._next_snapshot += 1
            frozen = subs.share_snapshot(
                FrozenSnapshot(
                    snapshot_id, version, storage.iter_item_records(), self.policy
                )
            )
        session.must_resync = False
        session.snapshot_id = frozen.snapshot_id
        session.transfer_cursor = -1
        session.pending_deltas = []
        session.pending_delta_bytes = 0
        session.outstanding = {}
        session.backlog_items = 0
        session.backlog_bytes = 0
        session.state = SESSION_SNAPSHOTTING
        self.stats["snapshots"] += 1
        if not self._send(
            session,
            SnapshotBegin(
                frozen.snapshot_id, frozen.version, frozen.item_count()
            ),
        ):
            return False
        return True

    def _send_snapshot_chunks(self, session, subs, frame):
        # type: (object, object, int) -> None
        frozen = subs.snapshot_by_id(session.snapshot_id)
        if frozen is None:
            # 快照对象已经被回收，而会话还指着它——只能重来一份。
            self._abort(
                session,
                RESYNC_UNKNOWN_TRANSFER,
                "在途快照 %s 已不在缓存里" % (session.snapshot_id,),
            )
            return
        budget = self.policy.snapshot_chunks_per_frame
        while budget > 0:
            index = session.transfer_cursor + 1
            if index >= frozen.chunk_count():
                break
            chunk = frozen.chunks[index]
            if not self._send(
                session, SnapshotChunk(frozen.snapshot_id, index, chunk)
            ):
                return
            session.transfer_cursor = index
            budget -= 1
        if session.transfer_cursor + 1 < frozen.chunk_count():
            return
        # 全部分片发出：发结束时**先让位再改状态**——`_send` 失败时
        # 状态必须留在「快照在途」，否则客户端会以为已经收全了。
        if not self._send(session, SnapshotEnd(frozen.snapshot_id, frozen.chunk_count())):
            return
        session.snapshot_id = ""
        session.transfer_cursor = -1
        session.state = SESSION_ACTIVE
        session.record_published(frozen.version)
        subs.drop_unused_snapshots()
        self._flush_pending(session, frame)

    def _flush_pending(self, session, frame):
        # type: (object, int) -> None
        """把快照期间暂存的增量按序补发。**顺序不能变**：版本链断一节，
        后面每一节都成了「接不上的增量」。"""
        pending = session.pending_deltas
        session.pending_deltas = []
        session.pending_delta_bytes = 0
        for batch in pending:
            self._deliver_batch(session, batch, frame)
            if session.must_resync:
                # 补发途中又断了（版本对不上或被判超限），剩下的丢掉：
                # 客户端已经要重取快照，再补也只是浪费带宽。
                session.pending_deltas = []
                session.pending_delta_bytes = 0
                return

    # -- 增量投递 ------------------------------------------------------

    def _deliver_batch(self, session, batch, frame):
        # type: (object, object, int) -> None
        if session.must_resync:
            return
        if session.state == SESSION_SNAPSHOTTING or session.snapshot_id:
            self._buffer_batch(session, batch, frame)
            return
        if session.item_view_version != batch.base_version:
            self._abort(
                session,
                RESYNC_VERSION_GAP,
                "客户端在版本 %d，这批的基准是 %d"
                % (session.item_view_version, batch.base_version),
            )
            return
        for packet in batch.packets:
            message = ItemDelta(
                batch.batch_id,
                packet.packet_index,
                packet.packet_count,
                batch.base_version,
                batch.new_version,
                packet.upserts,
                packet.removes,
            )
            if not self._send(session, message, transfer_id=batch.batch_id,
                              items=packet.item_count(), size=packet.bytes):
                return
        session.record_published(batch.new_version)

    def _buffer_batch(self, session, batch, frame):
        # type: (object, object, int) -> None
        session.pending_deltas.append(batch)
        session.pending_delta_bytes += batch.bytes()
        over_batches = (
            self.policy.max_pending_delta_batches > 0
            and len(session.pending_deltas) > self.policy.max_pending_delta_batches
        )
        over_bytes = (
            self.policy.max_pending_delta_bytes > 0
            and session.pending_delta_bytes > self.policy.max_pending_delta_bytes
        )
        if over_batches or over_bytes:
            self._abort(
                session,
                RESYNC_PENDING_OVERFLOW,
                "快照在途期间积压了 %d 批 / %d 字节"
                % (len(session.pending_deltas), session.pending_delta_bytes),
            )

    # -- 会话注销 ------------------------------------------------------
    #
    # **这一组方法只负责关，不负责说。** 「服务端单方面注销」要推一条通知
    # （`_notify_gone`），而「玩家自己关了、玩家走了、服务停机」**不该推**——
    # 因此通知挂在**具体调用点**上，不放进 `_forget_all`，否则那三种情形会
    # 跟着发，而它们的包没有任何收件人。新增调用点前先想清楚属于哪一类。

    def close_session(self, session_id, reason="closed"):
        # type: (str, str) -> object
        session = self.registry.close(session_id, reason)
        if session is not None:
            self._forget(session)
        return session

    def close_for_player(self, player_id, reason="player_left"):
        # type: (str, str) -> list
        return self._forget_all(self.registry.close_for_player(player_id, reason))

    def close_for_device(self, ref, reason=CLOSE_DEVICE_REPLACED):
        # type: (object, str) -> list
        return self._forget_all(self.registry.close_for_device(ref, reason))

    def close_for_network(self, network_id, reason="network_changed"):
        # type: (str, str) -> list
        return self._forget_all(self.registry.close_for_network(network_id, reason))

    def close_for_epoch(self, network_id, epoch, reason="epoch_rotated"):
        # type: (str, str, str) -> list
        return self._forget_all(
            self.registry.close_for_epoch(network_id, epoch, reason)
        )

    def on_runtime_change(self, change):
        # type: (object) -> int
        """跟随一次运行时状态变化注销会话。返回受影响的会话数。

        组合根把它挂到 `RuntimeManager.add_listener` 上。**运行时可以自己
        关掉业务，但会话表不在运行时里**——不在这里跟一遍，症状是「网络已经
        在重建了，终端还在按旧成员收增量」，而那种界面看起来只是数据不动。

        三种变化关掉整网（理由见 `_RUNTIME_CLOSE_REASONS`），成员增减则
        只关掉那些**终端已被替换**的会话（见 `_drop_stale_devices`）。
        其余变化（建立、绑主控、开放）是空操作：它们不使任何既有会话失效。

        **两条路都要在关掉之后告诉客户端**（`_notify_gone`）。会话没了却
        不吭声，客户端会拿着一个死会话继续显示库存，直到下一次点击被拒
        ——与空闲超时那条一模一样（`_notify_abandoned` 的说明、欠账台账
        **D-08**）。这里不能用 `_forget_all` 顺手带上：`close_for_player`
        （玩家离开）与 `shutdown`（停机）走的也是它，而那两个时候**不该
        发**——收件人已经不在了，或者整台服务正要停。
        """
        network_id = self.network_id_of(change.component_id)
        reason = _RUNTIME_CLOSE_REASONS.get(change.kind)
        if reason is not None:
            closed = self.close_for_network(network_id, reason)
            self._notify_gone(closed, _GONE_DETAILS[reason])
            return len(closed)
        if change.kind == RuntimeChange.MEMBERS_CHANGED:
            doomed = self._drop_stale_devices(network_id)
            self._notify_gone(doomed, _GONE_DETAILS[CLOSE_DEVICE_REPLACED])
            return len(doomed)
        return 0

    def _drop_stale_devices(self, network_id):
        # type: (str) -> list
        """关掉那些「界面指着的方块已经不是原来那一台」的会话。

        **必须重新查一遍，而不是从变化里推。** `RuntimeChange` 的 `__slots__`
        里根本没有成员键——它说的是「这个网络的成员集合变了」。要判某台
        终端还在不在，只能拿会话里存的 `device_ref` 回到注册表问一次。

        判据与开会话时**同一句**（`device_ref.matches`）：位置相同、代次
        也相同才算同一台。拆掉又在原地放一个会让代次变大，于是这里关掉
        旧会话——而那正是必须的：界面上的库存属于已经被拆掉的那台方块。

        先收集再关：`close_session` 会改动订阅表，边遍历边关就会漏掉后面
        那些。查不到上下文的（网络已经不在了）一并算作失效——那是
        fail closed 的那一侧。
        """
        subs = self._subs.get(network_id)
        if subs is None:
            return []
        doomed = []
        for session in subs.ordered_sessions():
            context = self._context_of(session.player_id, session.device_ref)
            if context is None or context.record_ref is None:
                doomed.append(session)
                continue
            if not session.device_ref.matches(context.record_ref):
                doomed.append(session)
        for session in doomed:
            self.close_session(session.session_id, CLOSE_DEVICE_REPLACED)
        return doomed

    def shutdown(self, reason="shutdown"):
        # type: (str) -> int
        """关掉全部会话并丢掉全部缓存，返回关掉的会话数。`stop()` 用。

        **不只是关会话**：最后一个会话离开时订阅表会自己消失
        （`_forget` 里的 `drop_if_empty`），因此这份调用之后 `_subs` 必然是
        空的——不需要另外再清一次。会话 ID 序号、批次号、轮转游标都**不重置**：
        它们是进程级计数器，重置会让停机前发出去的 `batch_id` 被下一轮复用，
        而客户端手里可能还留着那个号。
        """
        return len(self._forget_all(self.registry.close_all(reason)))

    def resync_network(self, network_id, code=RESYNC_VERSION_GAP, detail=""):
        # type: (str, str, str) -> int
        """让一个网络的**全部**会话重取快照。返回受影响的会话数。

        调用点在「服务端知道客户端缓存一定不对了」的地方：整份索引重建
        （恢复/修复路径），或运行时换代次。那种时候索引里哪些键变了并没有
        记在任何脏集合里，靠增量补不上——只能重取。
        """
        subs = self._subs.get(network_id)
        if subs is None:
            return 0
        affected = subs.ordered_sessions()
        for session in affected:
            self._abort(session, code, detail)
        return len(affected)

    def _forget_all(self, sessions):
        # type: (list) -> list
        for session in sessions:
            self._forget(session)
        return sessions

    def _forget(self, session):
        # type: (object) -> None
        self._inventory_sent.pop(session.session_id, None)
        self._craft_sent.pop(session.session_id, None)
        self.status.drop(session.session_id)
        subs = self._subs.get(session.network_id)
        if subs is None:
            return
        subs.discard(session.session_id)
        if subs.drop_if_empty():
            del self._subs[session.network_id]
            self.stats["networks_dropped"] += 1

    # -- 服务端单方面注销的通知 ----------------------------------------

    def _notify_abandoned(self, sessions):
        # type: (list) -> int
        """空闲超时回收时**告诉客户端一声**，返回实际发出的条数。

        只是 `_notify_gone` 的一个具名调用点：文本与**计数桶**都与那几条
        「服务端单方面注销」的路径分开（实机上要查的东西不同——「这个人走开
        了」vs「这台方块/这条网络没了」）。
        """
        return self._notify_gone(
            sessions,
            "会话空闲超时，请重新打开界面",
            "expired_notices",
            "expired_notices_skipped",
        )

    def _notify_gone(self, sessions, detail, sent_key="gone_notices",
                     skipped_key="gone_notices_skipped"):
        # type: (list, str, str, str) -> int
        """**服务端单方面注销**一批会话之后，告诉客户端一声。

        **不发这一条的后果是「静默的尸体」。** `evict_expired` 只清服务端
        自己的账（`_forget` 一条包都不发），客户端于是继续以为会话还在：
        界面照常显示**过期库存**，直到下一次点击被回 `session_invalid`
        才丢会话、亮遮罩（`client/network_client.py` 的 `_absorb_error`）。
        2026-10-01 真机实测：服务端 `expired` 涨到 3，而客户端
        `sessions_dropped` 始终是 0——玩家看到的是「点什么都没反应」，
        而中间那段时间界面看起来一切正常。

        补上这一条之后，界面在会话死掉的那一刻就起遮罩。**会话照样会被
        回收**（超时那条是故意的，它是客户端崩溃或断线时的兜底），变的
        是它不再无声无息。客户端**零改动**：空会话号的 `error` 它早就会
        处理，因此**四条路径共用这一段**——`evict_expired`（超时）、
        `_drop_stale_devices`（设备被替换）、以及 `on_runtime_change` 的
        整网注销（退役/关闭/丢主控）。

        **一个玩家最多一条。** 同一玩家可以并存多个会话
        （`max_sessions_per_player` 是 4），一次回收可能同时丢掉其中几条；
        逐条发等于让同一个客户端连收几条同样的通知。

        **只有这个玩家一条活会话都不剩时才发。** 客户端一次只持有一个
        会话，手里那个一定是**最后开的**那个；旧的之所以还在，正是因为
        关界面时那条 `session_close` 没送到（引擎销毁界面不走 `OnDestroy`
        时就是这样）。此时若替旧会话发一条「你的会话没了」，客户端会照
        字面**把它手里那个新会话一并丢掉**——比不发更糟。判据因此是
        「清完之后这个玩家还有没有活会话」，与 `_drop_stale_devices` 的
        「必须重新查一遍，不要从变化里推」是同一条理由。

        **这条守卫是保守的，代价写在这里**：若客户端手里那个（最后开的）
        会话被注销、而同一玩家在**别处**还留着一个更早的活会话（另一个
        网络上的旧终端），这里会判成「他还有活会话」而不发——那一次仍要
        等到下一次点击被拒。要修得靠「被注销的是不是他最新那个会话」，
        而不是「他还有没有会话」，属于另一次决定，本轮**没有替它定案**。

        **何时不该调它**：`close_for_player`（玩家离开）与 `shutdown`
        （停机）也走 `_forget_all`，但那两处**不该发**——收件人已经不在，
        或整台服务正要停。因此通知挂在具体的调用点上，不放进 `_forget_all`。
        """
        notified = 0
        told = set()
        for session in sessions:
            player_id = session.player_id
            if player_id in told:
                continue
            told.add(player_id)
            if self.registry.sessions_for_player(player_id):
                self.stats[skipped_key] += 1
                continue
            if self._send_gone(session, detail, sent_key):
                notified += 1
        return notified

    def _send_gone(self, session, detail, sent_key="gone_notices"):
        # type: (object, str, str) -> bool
        """给一个**已经不在注册表里**的会话发一条「会话已失效」。

        **不走 `_send`。** 那条路会把 `session.session_id` 盖在信封上，而
        客户端正是靠「信封里没有会话号」认出「这不是某一次请求失败，是
        这个会话本身没了」（见 `SyncError` 的说明）。带着号发出去，客户端
        只会当成一次普通的请求失败，界面照旧亮着——发的这一条就等于没发。

        同理，`request_id` 留空是**如实**：这一条不回答任何请求（客户端
        并没有在等什么，它只是界面上的那个会话没了），客户端的空会话号
        那条分支也根本不读它。

        编码与发送的异常都在这里接住：这是每帧都会走到的路径，一个异常
        打挂的是整帧，不只是这一个客户端——与 `_send` 同一条纪律。会话
        已经注销，因此这里**不做** `_note_failure` 那一套连续失败与退避：
        没有下一次要退避的重试了。
        """
        envelope = self._error("", "", Reason.SESSION_INVALID, detail)
        if self.port is None:
            self.stats["send_failures"] += 1
            return False
        try:
            payload = envelope.to_dict()
        except (TypeError, ValueError):
            self.stats["transport_errors"] += 1
            return False
        try:
            ok = self.port.send(session.player_id, payload)
        except Exception:
            self.stats["transport_errors"] += 1
            return False
        if not ok:
            self.stats["send_failures"] += 1
            return False
        self.stats[sent_key] += 1
        return True

    # -- 发送 ----------------------------------------------------------

    def _send(self, session, message, transfer_id="", items=0, size=0):
        # type: (object, object, str, int, int) -> bool
        """编码并发送一条消息。**所有发送都经这里**，失败与记账只有一处。

        返回 False 表示这一条没发出去，调用方应当停下（不要继续发后面的
        包——那会让客户端收到一堆接不上的分片）。
        """
        envelope = SyncEnvelope(
            message,
            session_id=session.session_id,
            network_id=session.network_id,
            epoch=session.epoch,
            view_kind=session.view_kind,
        )
        try:
            payload = envelope.to_dict(session.descriptions)
        except ResourceResyncRequired as exc:
            # **绝不外抛**：这是每帧都会被调到的路径，一个异常打挂的
            # 是整帧，不只是这一个会话。
            self._abort(session, RESYNC_RESOURCE_TABLE_FULL, str(exc))
            return False
        except (TypeError, ValueError) as exc:
            self.stats["transport_errors"] += 1
            self._abort(session, RESYNC_UNKNOWN_TRANSFER, "编码失败：%s" % (exc,))
            return False
        if self.port is None:
            self.stats["send_failures"] += 1
            return False
        try:
            ok = self.port.send(session.player_id, payload)
        except Exception:
            # 端口的契约是**返回 `False`**（收件人不存在这类可预判的情形），
            # 异常留给引擎真的出错那一类；两种都要接住、且分开计数——
            # 前者是「没人在收」，后者是「引擎坏了」，查法完全不同。
            self.stats["transport_errors"] += 1
            ok = False
        if not ok:
            self._note_failure(session)
            return False
        # **成功即清零。** `fail_count` 的语义是「连续」失败次数，一次成功的
        # 发送就是那个「连续」的断点。只在 `_on_ack` 里清零不够：快照分片
        # 不带传输号、客户端也不会为它确认，于是只收快照的会话永远等不到
        # 清零——一旦曾经连败到阈值，此后**任何一次**偶发失败都会立刻触发
        # 一次中止与退避，而它本该只是重发一条。
        session.fail_count = 0
        if transfer_id:
            entry = session.outstanding.get(transfer_id)
            if entry is None:
                entry = {"packets": [], "acked": -1}
                session.outstanding[transfer_id] = entry
            entry["packets"].append((items, size))
            session.backlog_items += items
            session.backlog_bytes += size
            self.stats["packets"] += 1
            if self._over_backlog(session):
                self._abort(
                    session,
                    RESYNC_BACKLOG_OVERFLOW,
                    "已发未确认达到 %d 条 / %d 字节"
                    % (session.backlog_items, session.backlog_bytes),
                )
                return False
        return True

    def _over_backlog(self, session):
        # type: (object) -> bool
        if (
            self.policy.max_backlog_items > 0
            and session.backlog_items > self.policy.max_backlog_items
        ):
            return True
        return (
            self.policy.max_backlog_bytes > 0
            and session.backlog_bytes > self.policy.max_backlog_bytes
        )

    def _note_failure(self, session):
        # type: (object) -> None
        self.stats["send_failures"] += 1
        session.fail_count += 1
        if session.fail_count >= max(1, self.policy.max_failures):
            self._abort(
                session,
                RESYNC_UNKNOWN_TRANSFER,
                "连续 %d 次发送失败" % (session.fail_count,),
            )

    def _error(self, session_id, request_id, code, detail):
        # type: (str, str, str, str) -> object
        """给客户端的拒绝。**会话不存在时也回**，否则客户端只会看到界面不响应。

        **`session_id` 传空表示「这个会话已经不在了」**（会话不存在／被别人
        抢／距离、维度或设备复核不过），传非空表示「会话还活着，是这一条请求
        自身失败了」（执行命令时抛了异常之类）。客户端据此决定要不要就地丢掉
        会话。

        **判据做成结构性的，而不是让客户端列一份「哪些原因码算会话级」的表。**
        那份表一旦与服务端漂移，表现是「界面看起来还正常、点什么都没反应」
        ——正是最难查的一类（同 `client/network_client.py` 里拒绝在校验动作名
        时给出的理由：漏改的那一份只会造成看不见的失败）。

        这种情况 `network_id` / `epoch` 只能留空，而 `SyncEnvelope` 对 S2C
        消息要求它们非空——`error` 是那条规则唯一的例外（见
        `SyncEnvelope._validate`）。客户端按 `request_id` 把错误挂回对应的
        那次请求，因此它即使没有会话也送得到、认得出。
        """
        return SyncEnvelope(
            SyncError(request_id, code, detail),
            session_id=session_id,
            view_kind=VIEW_ITEM,
        )

    def _abort(self, session, code, detail):
        # type: (object, str, str) -> None
        """终止在途传输，要求客户端重取快照。**幂等**，可重复调用。

        退避只在**连续失败**到阈值时才安排：一次性的失效（版本对不上、
        确认里提到陌生传输）本来就该立刻重试，给它加退避只会让一个
        偶发缺口拖慢好几帧。而「一直发不出去」那种才需要退避，否则就是
        每帧重冻一份快照的重试风暴。
        """
        if session.must_resync and not session.snapshot_id:
            return
        session.must_resync = True
        session.snapshot_id = ""
        session.transfer_cursor = -1
        session.pending_deltas = []
        session.pending_delta_bytes = 0
        session.outstanding = {}
        session.backlog_items = 0
        session.backlog_bytes = 0
        session.state = SESSION_ACTIVE
        if session.fail_count >= max(1, self.policy.max_failures):
            session.backoff_until_frame = self._frame + self.policy.backoff_frames
        self.stats["resyncs"] += 1
        self._send(session, ResyncRequired(code, detail))

    def _defer(self, session, code, detail, frame):
        # type: (object, str, str, int) -> None
        """开不了快照（例如没有存储服务）：告诉客户端，并退避后重试。"""
        session.must_resync = True
        session.backoff_until_frame = frame + max(1, self.policy.backoff_frames)
        self.stats["resyncs"] += 1
        self._send(session, ResyncRequired(code, detail))

    # -- 诊断 ----------------------------------------------------------

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report["frame"] = self._frame
        report["networks"] = {}
        for network_id in sorted(self._subs):
            subs = self._subs[network_id]
            report["networks"][network_id] = {
                "sessions": len(subs.sessions),
                "publish_version": subs.publish_version,
                "history": len(subs.history),
                "history_bytes": subs.history_bytes,
                "snapshots": len(subs.snapshots),
                "batches": subs.stats["batches"],
            }
        report["rotation"] = self._rotation.describe()
        report["device_status"] = self.status.diagnostics()
        return report


#: 发布窗口需要存储服务提供的只读口。缺一不可：少了 `take_dirty_keys`
#: 就取不到变化，少了 `publish_version` 就无从构造 `base_version`。
_STORAGE_READ_PORT = (
    "has_dirty_items",
    "take_dirty_keys",
    "read_items",
    "iter_item_records",
)


def _publishable(storage):
    # type: (object) -> bool
    """存储服务是否提供了发布窗口需要的**全部**只读口。

    用能力检查而不是 `isinstance(StorageService)`：与
    `application.py` 里 `debug_query("plan")` 的做法一致——那一处同样
    按 `callable(getattr(...))` 判断，好在服务被替换或被包装时立刻降级，
    而不是等到某次调用抛 AttributeError。
    """
    for name in _STORAGE_READ_PORT:
        if not callable(getattr(storage, name, None)):
            return False
    return hasattr(storage, "publish_version")


def _no_storage(network_id):
    # type: (str) -> None
    return None


def _no_status(device_ref):
    # type: (object) -> None
    """没有接入运行时（客户端半边单独跑、离线夹具）时的兜底：不发状态。"""
    return None


def _no_craft(device_ref):
    # type: (object) -> None
    """没有接工作台时的兜底：不发。

    **不是「发一个空工作台」**：空工作台在客户端读出来是「你的原料没了」，
    而真相是「这个功能没接」。与 `_no_status` 同一条纪律。
    """
    return None


def _no_context(player_id, device_ref):
    # type: (str, object) -> None
    return None


def _no_submit(command):
    # type: (object) -> object
    """没有接入执行方时的兜底：**明确拒绝，绝不假装成功**。

    返回 `blocked` 而不是抛异常，是因为「没接执行方」与「执行方坏了」是
    两件事：前者是装配遗漏（离线测试、客户端半边单独跑），后者是内部
    错误。前者该让界面显示「暂时不可用」，后者该让日志里有堆栈。
    """
    return CommandResult.blocked(
        command.request_id,
        [
            BlockReason(
                Reason.SERVICE_NOT_READY,
                "同步中枢没有接入命令执行方（submit）",
            )
        ],
    )
