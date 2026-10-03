# coding=utf-8
"""终端界面的**显示缓存**：把线上的快照分片与增量批组装成一份列表。

11-03 之前 `client/network_client.py` 与 `client/ui/item_list_model.py` 都写
好了却**零消费者**（交付说明 §五 4.1 把这件事划给 11-03）。这个模块就是那个
消费者，也是**唯一**把「收包」接成「一条可见列表」的地方。

三层各管一件事，不要互相替代：

- `NetworkClient`：会话、请求编码、回包分发。它**只解析**，不判断收到了什么；
- `ItemListModel`：条目、顺序、筛选、批次合并。它**只认绝对量**，不知道网络；
- 本模块：**组装与去重**。分片齐了没有、一批包到齐没有、缺了该找谁要。

## 快照绝不应用半份

`snapshot_end` 到的时候按 `chunk_count` 逐一核对 `chunk_index` 是否都在。
**缺一块就整份丢掉**（`truncated_snapshots`），宁可空着重取：一份截断的
库存看起来完全正常——行都在、数量都是真的——只是少了几种物品，而玩家
没有任何办法从界面上看出少了什么。这是本模块最不能让步的一条。

`total_items` 对不上则**照常应用**并计数。它是服务端自己报的条目总数，
与分片里的实际条数不符只说明它那侧的口径不一致；为了这个数字拒绝一份
完整数据，会陷入「永远在重取」的死循环——每一次重取的结论都一样。

## 快照分片**永远不被 ack**

服务端 `_send_snapshot_chunks` 发分片时**不带 transfer_id**（增量批才带
`batch_id`）。协议上就没有可确认的东西，而硬凑一个 id 发过去，服务端
`_on_ack` 会在 `session.outstanding` 里查不到它，直接判
`RESYNC_UNKNOWN_TRANSFER` **把会话关掉**。也就是说：ack 错了不是「没效果」，
是「把玩家的界面打掉」。

## 两个 resync 的处置**不一样**，这是有意的

- `ListResyncRequired`（模型抛的：增量基准对不上本地版本）——**不清模型**。
  模型的 `_version` 仍然有效，直接拿它去 `request_snapshot` 补一份增量
  或快照即可。
- `ResyncRequired`（服务端推的：它判定客户端追不上了）——**清模型**，然后
  **等**。服务端在它那条路径上已经主动推出了新快照，再 `request_snapshot`
  一次是多余的；更糟的是 `model.clear()` 刻意不重置 `_version`（见那个方法），
  拿一个旧版本去请求，回来的增量的 `base_version` 会与本地对不上。

清了模型之后到新快照到达之前，**增量一律丢弃并计数**：那时表是空的，
而增量的基准还对着清掉之前那一份，写进去就是「用半张表冒充整张表」。
`_awaiting_snapshot` 就是这个窗口。
"""

from ..network_client import NetworkClient
from ..network_client import message_kind_of
from ...common.protocol import (
    MSG_ITEM_DELTA,
    MSG_RESYNC_REQUIRED,
    MSG_SNAPSHOT_BEGIN,
    MSG_SNAPSHOT_CHUNK,
    MSG_SNAPSHOT_END,
)
from .item_list_model import ApplyResult, ItemListModel, ListResyncRequired

#: 记多少条「已经应用过的批次」。重复的增量包再到达时要认出来。
#:
#: **这不是过度设计**：本仓库实测过客户端事件类被重复投递（见
#: `client/network_client.py` 的 `shared_event_classes`），同一批增量会到达
#: 两次。第二次的 `base_version` 已经对不上（第一次应用时推进过了），模型会
#: 抛 `ListResyncRequired`——于是一次重复投递换来一次白重取快照，每开一次
#: 界面都来一遍。
#:
#: 32 是**待定值**（同 `network_client.DEFAULT_QUEUE_LIMIT`）：服务端在同一
#: 会话里同时最多挂着几个批次没有公开上限，这里只求覆盖「刚刚应用过的那几批」。
#: 记不下时的后果是退化成上面那种白重取，**不影响正确性**。
APPLIED_BATCH_MEMORY = 32

#: 连续补取失败之后等多少帧再补。索引是「连续第几次失败」。
#:
#: **第一次是 0：一包丢了就该立刻再要一份。** 偶发丢包是常态，为它等一截
#: 只会把一次看不见的补包变成肉眼可见的卡顿。之后就指数增长、封顶——
#: 连续失败说明**不是偶发**（对端在推一份我们解不出来的东西），此时继续
#: 每帧一次只是拿服务端当靶子打。
#:
#: 帧数是**相对节拍**，不折算成秒：界面渲染帧率与脚本 tick、模拟 tick 都
#: 不是一回事（`CLAUDE.md` 记了这条）。封顶值本身也是首轮配置（11-14 实测
#: 后确定），与 `SyncPolicy` 里那一堆同性质。
SNAPSHOT_BACKOFF_MAX_FRAMES = 120


def _snapshot_backoff(failures):
    # type: (int) -> int
    """连续失败 `failures` 次之后，下一次补取要等几帧。"""
    if failures <= 1:
        return 0
    return min(1 << (failures - 1), SNAPSHOT_BACKOFF_MAX_FRAMES)


class TerminalCache(object):
    """一份终端列表的显示缓存。**引擎无关**，可以离线整条跑。"""

    def __init__(self, client=None, model=None, name_resolver=None):
        # type: (object, object, object) -> None
        #: 会话与收包。不给就现建一个。
        self.client = client if client is not None else NetworkClient()
        #: 条目与顺序。**写入路径只在本模块内**（`_absorb_*` 那几个方法）；
        #: 界面只读它——`query` / `query_page` / `index_of` / `version`。
        #: 唯一由界面驱动的写入是 `set_sort` / `set_filter_text`（玩家的操作，
        #: 不是数据流），它们只改条件、不改条目。
        self.model = (
            model if model is not None
            else ItemListModel(name_resolver=name_resolver)
        )
        #: 在途快照：`{snapshot_id, item_view_version, total_items, chunks}`。
        #: `chunks` 是 `chunk_index -> 条目元组`，因此乱序到达不影响结果。
        self._snapshot = None
        #: 在途增量批：`batch_id -> {packet_count, base_version, new_version,
        #: packets}`，同样按 `packet_index` 存，乱序不影响。
        self._batches = {}
        #: 服务端要求重同步之后的「等快照」窗口（见模块 docstring）。
        self._awaiting_snapshot = False
        #: 已应用过的批次 id（有界）。用来认出重复投递的增量包。
        self._applied = []
        self._applied_set = set()
        #: 本帧合并出来的 `ApplyResult`；没有变化时是 None。
        self._frame = None
        #: 本帧已经为哪些 snapshot_id 补发过请求。**每帧每个 id 最多一次**：
        #: 一帧里收到三条孤儿 `snapshot_end` 就发三条请求，服务端会连着推
        #: 三份快照，而客户端只会用最后一份。
        self._requested = set()
        #: 本对象见过的渲染帧数（`tick()` 每调一次加一）。退避按**帧**算而不是
        #: 按秒——界面渲染帧率与脚本 tick、模拟 tick 都不是一回事，写死换算
        #: 会随负载漂（`CLAUDE.md` 记了这条）。它只是一个相对节拍。
        self._frames = 0
        #: 连续补取失败的次数。**一份快照成功应用就归零**——它量的正是
        #: 「一直在失败」这件事，偶发丢一包不该被算成同一种状态。
        self._failed_snapshots = 0
        #: 下一次允许补取的最早帧号。见 `_snapshot_backoff`。
        self._next_request_frame = 0
        self.stats = {
            "snapshots_started": 0,
            "snapshots_applied": 0,
            #: 缺块而整份丢掉的快照。**每涨一次都应该能追出一台机器在丢包**。
            "truncated_snapshots": 0,
            #: `snapshot_id` 我们压根没有（多半是上一份的尾巴）。
            "orphan_chunks": 0,
            "orphan_ends": 0,
            #: 同 `snapshot_id` + 同 `chunk_index` 又到了一次。
            "duplicate_chunks": 0,
            "delta_batches": 0,
            "delta_packets": 0,
            "duplicate_packets": 0,
            #: 同一 `batch_id` 的关键字段（批大小、基准版本）前后不一致。
            "inconsistent_batches": 0,
            #: 已经应用过的批次又来了包（重复投递，见 `APPLIED_BATCH_MEMORY`）。
            "late_packets": 0,
            #: 增量的基准版本对不上本地 → 模型要求重取。
            "resync_on_batch": 0,
            #: 服务端推 `resync_required` → 清表等快照。
            "resync_from_server": 0,
            #: `resync_required` 之后、新快照到达之前被丢弃的增量。
            "deltas_dropped_while_awaiting": 0,
            #: 服务端报的 `total_items` 与分片实际条数不符（应用了，只记一笔）。
            "total_items_mismatch": 0,
            #: 因为「已经有一份在等」而没发出去的补请求。
            "requests_skipped": 0,
            #: 因为**退避窗口还没过**而没发出去的补请求。与 `requests_skipped`
            #: 分开：那一个是「不必发」，这一个是「先别发」，两者混在一起
            #: 就看不出客户端是不是正在被服务端按在地上摩擦。
            "requests_backed_off": 0,
            "requests_sent": 0,
            "acks_sent": 0,
            "acks_skipped": 0,
        }

    # -- 每帧 ----------------------------------------------------------

    def tick(self):
        # type: () -> object
        """推进一帧，返回本帧对界面的影响（`ApplyResult`），没有变化时 None。

        **返回值是「合并后」的结果**，不是最后一条：一帧里可能到达好几批
        增量，只报最后一批的话，前几批动过的行界面就不知道要重画。
        合并规则与 `ApplyResult` 的字段一一对应——`reordered` 取或、
        `changed_keys` / `removed_keys` 取并、锚点与版本取最新。

        界面拿它做两件事：与**正在显示的那几行**求交集决定要不要重画，
        以及用 `anchor_key` / `anchor_offset` 把玩家盯着的那一行留在原处。
        缓存层不知道视口在哪，所以交集是界面的事。

        **命令回执不在这里取**（`take_command_results`）：回执是「一次性
        事件」，而这个返回值是「要按帧合并的状态」。混在一起会让界面为了
        处理一条回执就把这一帧的增量也遍历一遍。
        """
        self._frames += 1
        self._frame = None
        self._requested = set()
        for envelope in self.client.take_events():
            self._absorb(envelope)
        frame = self._frame
        self._frame = None
        return frame

    def _absorb(self, envelope):
        # type: (object) -> None
        """一条 S2C 回包进入组装流程。不认识的消息**静默跳过**。

        不认识不是错误：`session_ready` / `error` / `command_result` 都会
        经过同一条队列，而它们归别的层处理。这里按种类分派而不是按方向，
        因此新增一种消息不会让这个方法误吞或误判。
        """
        kind = message_kind_of(envelope)
        if kind == MSG_SNAPSHOT_BEGIN:
            self._on_snapshot_begin(envelope.message)
        elif kind == MSG_SNAPSHOT_CHUNK:
            self._on_snapshot_chunk(envelope.message)
        elif kind == MSG_SNAPSHOT_END:
            self._on_snapshot_end(envelope.message)
        elif kind == MSG_ITEM_DELTA:
            self._on_delta(envelope.message)
        elif kind == MSG_RESYNC_REQUIRED:
            self._on_resync_required(envelope.message)

    # -- 快照 ----------------------------------------------------------

    def _on_snapshot_begin(self, message):
        # type: (object) -> None
        """开一份新快照。

        新 `snapshot_id` 顶掉半成品；同一个 id 再来一次时——同版本就接着
        收（服务端重发 `begin` 是允许的），不同版本说明这一份换了内容，
        已收的分片**必须作废**，否则会把两个版本的条目拼进同一张表。
        """
        current = self._snapshot
        if current is not None and current["snapshot_id"] == message.snapshot_id:
            if current["item_view_version"] == message.item_view_version:
                current["total_items"] = message.total_items
                return
        self._snapshot = {
            "snapshot_id": message.snapshot_id,
            "item_view_version": message.item_view_version,
            "total_items": message.total_items,
            "chunks": {},
        }
        # 新快照一到，「等快照」的窗口就结束了。
        self._awaiting_snapshot = False
        self.stats["snapshots_started"] += 1

    def _on_snapshot_chunk(self, message):
        # type: (object) -> None
        """收一块分片。**重复的下标保留第一份**。

        保留第一份而不是覆盖：两份内容不同的话，说不上哪一份是对的，而
        「先到的那份」至少与其余分片来自同一次发送。覆盖会让一份快照里
        混进另一个时间点的数据，且没有任何迹象。
        """
        snap = self._snapshot
        if snap is None or snap["snapshot_id"] != message.snapshot_id:
            self.stats["orphan_chunks"] += 1
            return
        if message.chunk_index in snap["chunks"]:
            self.stats["duplicate_chunks"] += 1
            return
        snap["chunks"][message.chunk_index] = message.items

    def _on_snapshot_end(self, message):
        # type: (object) -> None
        """收尾：齐了就应用，缺一块就整份丢掉并补请求（见模块 docstring）。"""
        snap = self._snapshot
        if snap is None or snap["snapshot_id"] != message.snapshot_id:
            self.stats["orphan_ends"] += 1
            self._failed_snapshots += 1
            self._request_snapshot_once("orphan_end")
            return
        # 先摘掉在途状态：下面无论走哪条路，这一份都不该再被续收。
        self._snapshot = None
        chunks = snap["chunks"]
        complete = all(
            index in chunks for index in range(message.chunk_count)
        )
        if not complete:
            self.stats["truncated_snapshots"] += 1
            self._failed_snapshots += 1
            self._request_snapshot_once("truncated")
            return

        records = []
        for index in range(message.chunk_count):
            records.extend(chunks[index])
        if len(records) != snap["total_items"]:
            # **计数但照常应用**：为了一个对不上的总数拒绝一份完整数据，
            # 会陷入「永远重取」的死循环（理由见模块 docstring）。
            self.stats["total_items_mismatch"] += 1
        self.stats["snapshots_applied"] += 1
        # **一份完整的快照就把退避清掉**：它量的正是「一直在失败」这件事，
        # 一次成功说明路又通了。窗口也要一并放开——只清计数不清窗口的话，
        # 上一次发送时算出来的 `_next_request_frame` 还压着，下一次偶发丢包
        # 会莫名其妙地等上几十帧。
        self._failed_snapshots = 0
        self._next_request_frame = 0
        self._merge(self.model.replace_snapshot(snap["item_view_version"], records))

    # -- 增量 ----------------------------------------------------------

    def _on_delta(self, message):
        # type: (object) -> None
        """收一个增量包。**收齐 `packet_count` 个才应用**（见 `ItemDelta`）。"""
        if self._awaiting_snapshot:
            self.stats["deltas_dropped_while_awaiting"] += 1
            return
        if message.batch_id in self._applied_set:
            self.stats["late_packets"] += 1
            return
        self.stats["delta_packets"] += 1
        entry = self._batches.get(message.batch_id)
        if entry is None:
            entry = {
                "packet_count": message.packet_count,
                "base_version": message.base_version,
                "new_version": message.new_version,
                "packets": {},
            }
            self._batches[message.batch_id] = entry
        elif (entry["packet_count"] != message.packet_count
                or entry["base_version"] != message.base_version
                or entry["new_version"] != message.new_version):
            # 同一个 batch_id 的包说着不同的批次。**整个重来**：留着旧的那
            # 几个包会把两个批次的条目拼成一个，而它们各自的基准不同。
            self.stats["inconsistent_batches"] += 1
            entry = {
                "packet_count": message.packet_count,
                "base_version": message.base_version,
                "new_version": message.new_version,
                "packets": {},
            }
            self._batches[message.batch_id] = entry
        if message.packet_index in entry["packets"]:
            self.stats["duplicate_packets"] += 1
            return
        entry["packets"][message.packet_index] = message
        if len(entry["packets"]) != entry["packet_count"]:
            return
        del self._batches[message.batch_id]
        self._apply_batch(message.batch_id, entry)

    def _apply_batch(self, batch_id, entry):
        # type: (str, dict) -> None
        """一批到齐：写进模型、报给界面、确认传输。

        **三件事的顺序不能换**：先看模型收不收（基准版本对不对），成了才写、
        才 ack。没写进去却 ack 了，服务端会把这一批从积压里扣掉，而客户端
        的表里永远缺这一批——之后每一批的基准都对不上，表现成「界面再也
        不更新了」。
        """
        packets = entry["packets"]
        order = [packets[index] for index in range(entry["packet_count"])]
        try:
            token = self.model.begin_batch(
                entry["base_version"], entry["new_version"]
            )
        except ListResyncRequired:
            # **不清模型**：模型的版本仍然有效，拿它去补一份就行。
            self.stats["resync_on_batch"] += 1
            self._request_snapshot_once("version_gap")
            return
        for packet in order:
            for key, stored, reserved, has_pattern in packet.upserts:
                self.model.upsert(key, stored, reserved, has_pattern)
            for key in packet.removes:
                self.model.remove(key)
        self._merge(self.model.end_batch(token))
        self.stats["delta_batches"] += 1
        self._remember_applied(batch_id)
        self._ack(batch_id, entry["packet_count"] - 1)

    def _on_resync_required(self, message):
        # type: (object) -> None
        """服务端要求重同步：清表、丢半成品、然后**等**（见模块 docstring）。"""
        self.stats["resync_from_server"] += 1
        self.model.clear()
        self._snapshot = None
        self._batches = {}
        self._awaiting_snapshot = True
        # **不 `request_snapshot`**：服务端在那条路径上已经推了新快照，
        # 而且 `model.clear()` 刻意不重置 `_version`，拿它去请求会要回一批
        # 应用不到空模型上的增量。

    # -- 请求与确认 ----------------------------------------------------

    def _request_snapshot_once(self, cause):
        # type: (str) -> None
        """补一份快照；**每帧至多一次，且连续失败要退避**。

        一帧里收到三条孤儿 `snapshot_end` 就发三条请求，服务端会连着推三份
        快照，而客户端只会用最后一份——前面几份的带宽与冻结开销纯属白费。

        **退避是 2026-10-01 真机实测后补上的。** 「每帧一次」这个上限单看
        很紧，但它没有终点：一份快照解不出来就整份丢掉再要一份，下一帧
        接着来。实测撞到的那次滚成了 **3210 次补取请求 / 17345 个回包**
        （原因见 `NetworkClient._is_for_this_session` 那段），而且**只要根因
        还在就一直滚下去**——服务端被自己推出去的快照按住摩擦。

        `cause` 只进诊断，不参与判重：这一帧已经发过就够了。
        """
        if self._awaiting_snapshot:
            # 已经有一份在路上了，再要一份只是让服务端多冻结一次。
            self.stats["requests_skipped"] += 1
            return
        if self._requested:
            self.stats["requests_skipped"] += 1
            return
        if not self.client.has_session():
            self.stats["requests_skipped"] += 1
            return
        if self._frames < self._next_request_frame:
            self.stats["requests_backed_off"] += 1
            return
        self._requested.add(cause)
        # `from_version` 取**模型已应用到的**版本，不是 `client` 记的那个
        # 「服务端最近告知的」——增量收了一半时两者不等（见 `request_snapshot`）。
        self.client.request_snapshot(self.model.version())
        self.stats["requests_sent"] += 1
        self._next_request_frame = (
            self._frames + _snapshot_backoff(self._failed_snapshots)
        )

    def _ack(self, batch_id, sequence):
        # type: (str, int) -> None
        """确认一个已经应用的增量批。

        `transfer_id` 取 `batch_id`：服务端 `_send(..., transfer_id=...)`
        对增量批传的正是它（快照分片那一侧根本不传，因此快照永远不 ack，
        见模块 docstring）。没有会话时**跳过而不是报错**：断线本来就可能
        发生在这两者之间。
        """
        if not self.client.has_session():
            self.stats["acks_skipped"] += 1
            return
        self.client.ack(batch_id, sequence)
        self.stats["acks_sent"] += 1

    # -- 帧内合并 ------------------------------------------------------

    def _merge(self, result):
        # type: (object) -> None
        """把一条 `ApplyResult` 并进本帧的结果。

        合并的是**界面需要知道的东西**：哪些行变了、要不要重排、锚点在哪。
        直接丢掉前一条的话，一帧里到达三批增量时界面只会重画第三批动过的行。
        """
        if result is None:
            return
        current = self._frame
        if current is None:
            self._frame = ApplyResult(
                result.reordered,
                tuple(result.changed_keys),
                tuple(result.removed_keys),
                result.anchor_key,
                result.anchor_offset,
                result.selection_cleared,
                result.version,
            )
            return
        changed = list(current.changed_keys)
        seen = set(changed)
        for key in result.changed_keys:
            if key not in seen:
                seen.add(key)
                changed.append(key)
        removed = list(current.removed_keys)
        removed_seen = set(removed)
        for key in result.removed_keys:
            if key not in removed_seen:
                removed_seen.add(key)
                removed.append(key)
        self._frame = ApplyResult(
            # **取或**：这一帧里只要重排过一次，界面就得重排一遍。
            current.reordered or result.reordered,
            tuple(changed),
            tuple(removed),
            result.anchor_key,
            result.anchor_offset,
            current.selection_cleared or result.selection_cleared,
            result.version,
        )

    def _remember_applied(self, batch_id):
        # type: (str) -> None
        """记下已经应用过的批次，超出上限时丢最旧的。"""
        self._applied.append(batch_id)
        self._applied_set.add(batch_id)
        while len(self._applied) > APPLIED_BATCH_MEMORY:
            self._applied_set.discard(self._applied.pop(0))

    # -- 只读视图与生命周期 --------------------------------------------

    def incomplete_batches(self):
        # type: () -> dict
        """还没收齐的批次：`batch_id -> (已有包数, 应有包数)`。

        **给界面显示与诊断用，不是给重试用的**：缺包多久算超时是一个**未定
        值**（全局约束 7），因此本轮不在这里编一个毫秒数去重发。能回答的
        只有「现在有几批没收齐」——真机上 `stats` 涨而列表不动时，第一个
        要看的数就是它。
        """
        return dict(
            (batch_id, (len(entry["packets"]), entry["packet_count"]))
            for batch_id, entry in self._batches.items()
        )

    def awaiting_snapshot(self):
        # type: () -> bool
        """是否处在「服务端要求重同步、正在等新快照」的窗口里。"""
        return self._awaiting_snapshot

    def close(self, notify=True):
        # type: (bool) -> None
        """收尾：注销回调、清空模型、丢弃半成品与在途批。

        `notify=True`（默认）会先发一条 `session_close` 告诉服务端；
        `notify=False` 只做本地收尾。后者给「服务端已经判死这个会话」用——
        那时再发一条只会换来又一条错误，而错误码恰恰是这一刻没人看的东西。

        **两条路都要清在途记录**：`close_session` 与 `drop_session` 都走
        `_forget_session`，因此这里不必区分。
        """
        if notify:
            self.client.close_session()
        else:
            self.client.drop_session()
        self.client.clear_handlers()
        self.model.clear()
        self._snapshot = None
        self._batches = {}
        self._applied = []
        self._applied_set = set()
        self._awaiting_snapshot = False
        self._frame = None
        self._requested = set()

    def diagnostics(self):
        # type: () -> dict
        return {
            "entries": self.model.count(),
            "version": self.model.version(),
            "generation": self.model.query_generation(),
            "rebuilds": self.model.rebuilds(),
            "snapshot_in_flight": self._snapshot is not None,
            "awaiting_snapshot": self._awaiting_snapshot,
            "incomplete_batches": self.incomplete_batches(),
            "stats": dict(self.stats),
            "client": self.client.diagnostics(),
        }
