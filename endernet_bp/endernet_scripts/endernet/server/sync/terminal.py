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
    ItemDelta,
    Reason,
    ResyncRequired,
    SessionReady,
    SnapshotBegin,
    SnapshotChunk,
    SnapshotEnd,
    SyncEnvelope,
    SyncError,
    MSG_ACK,
    MSG_SESSION_CLOSE,
    MSG_SESSION_OPEN,
    MSG_SNAPSHOT_REQUEST,
    VIEW_ITEM,
)
from ...common.resource_sync import ResourceResyncRequired
from ..network.ticking import RotationCursor
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

    三个注入的依赖，都是为了让本模块与引擎、拓扑、主控解耦：

    - `storage_of(network_id)` → `StorageService` 或 None。**无门槛只读
      路径**，先例见 `application.py` 的 `debug_query`（它同样走
      `runtime.services.get(...)` 而不订阅服务、不碰 `ServiceId.ALL`）。
    - `context_of(player_id, device_ref)` → `SessionContext` 或 None。
    - `port`：`send / listen / source_player_id / on_player_leave`。
    """

    def __init__(
        self,
        registry=None,
        policy=None,
        storage_of=None,
        context_of=None,
        port=None,
    ):
        self.registry = registry if registry is not None else SessionRegistry()
        self.policy = policy if policy is not None else SyncPolicy()
        self._storage_of = storage_of if storage_of is not None else _no_storage
        self._context_of = context_of if context_of is not None else _no_context
        self.port = port
        self._subs = {}
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
            "networks_dropped": 0,
            # 「服务在但只读口不全」与「版本链被别人推进过」都不是错误，
            # 但从现象上看都是「什么都没发出去」。计数留着，否则查不出来。
            "storage_unreadable": 0,
            "version_chain_breaks": 0,
        }

    # -- 装配 ----------------------------------------------------------

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
                envelope.session_id,
                request_id,
                Reason.SESSION_INVALID,
                "会话不存在或已关闭",
            )
        if session.player_id != player_id:
            # 别人的会话：**不告诉他这条会话存在**，与「不存在」同一个回答。
            self.stats["sessions_refused"] += 1
            return self._error(
                envelope.session_id,
                request_id,
                Reason.SESSION_INVALID,
                "会话不存在或已关闭",
            )
        session.touch(frame)
        if kind == MSG_SESSION_CLOSE:
            self.close_session(session.session_id, "client_closed")
            return None
        if kind == MSG_SNAPSHOT_REQUEST:
            return self._on_snapshot_request(session, envelope.message, frame)
        if kind == MSG_ACK:
            return self._on_ack(session, envelope.message, frame)
        return self._error(
            session.session_id,
            getattr(envelope.message, "request_id", ""),
            Reason.UNSUPPORTED,
            "该消息不由物品视图处理：%r" % (kind,),
        )

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

    # -- 推进 ----------------------------------------------------------

    def tick(self, context=None, budget=None):
        # type: (dict, int) -> int
        """推进一步：时效回收 + 传输推进 + 发布窗口。返回本帧发布的脏键数。

        **必须紧跟 `runtimes.tick()`**：提交发生在那里，发布放其后，
        窗口才看得见本帧提交。
        """
        self._frame += 1
        frame = self._frame
        self.stats["frames"] += 1

        expired = self.registry.evict_expired(frame)
        for session in expired:
            self._forget(session)

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
                self._advance_transfer(session, subs, frame)
        return self._publish_network(subs, frame)

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

    def close_session(self, session_id, reason="closed"):
        # type: (str, str) -> object
        session = self.registry.close(session_id, reason)
        if session is not None:
            self._forget(session)
        return session

    def close_for_player(self, player_id, reason="player_left"):
        # type: (str, str) -> list
        return self._forget_all(self.registry.close_for_player(player_id, reason))

    def close_for_device(self, ref, reason="device_replaced"):
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
        subs = self._subs.get(session.network_id)
        if subs is None:
            return
        subs.discard(session.session_id)
        if subs.drop_if_empty():
            del self._subs[session.network_id]
            self.stats["networks_dropped"] += 1

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
            # 适配器约定抛 `SyncTransportError`，但引擎边界什么都可能抛；
            # 这里一律当作「这一条没发出去」，不让它穿出 tick()。
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

        这种情况下 `network_id` / `epoch` 只能留空，而 `SyncEnvelope` 对
        S2C 消息要求它们非空——`error` 是那条规则唯一的例外（见
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


def _no_context(player_id, device_ref):
    # type: (str, object) -> None
    return None
