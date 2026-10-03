# coding=utf-8
"""终端显示缓存（11-03）：把快照分片与增量批组装成一份列表。

这个模块是本仓库里**唯一**把「收包」接成「一条可见列表」的地方，而它的
正确性没法靠界面看：一份缺了一块分片的库存显示出来与完整的一模一样——
行都在、数量都是真的——只是少了几种物品。因此下面每一条「绝不应用半份」
的用例都对应一个**看不出来**的故障。

它也是 `NetworkClient` 与 `ItemListModel` 的第一个消费者。两个模块在此之前
各自有完整用例，但**没有人把它们接起来过**——「接起来的缝在哪」正是这个
文件要回答的问题。
"""

import unittest

import testsupport
from endernet.client import network_client
from endernet.client.ui.terminal_cache import TerminalCache
from endernet.common.protocol import (
    MSG_ACK,
    MSG_SNAPSHOT_REQUEST,
    ItemDelta,
    ResyncRequired,
    SessionReady,
    SnapshotBegin,
    SnapshotChunk,
    SnapshotEnd,
    SyncEnvelope,
)
from endernet.common.resource_key import ResourceKey

STONE = ResourceKey.item("minecraft:stone")
DIRT = ResourceKey.item("minecraft:dirt")
SAND = ResourceKey.item("minecraft:sand")
APPLE = ResourceKey.item("minecraft:apple")

#: 版本号取两个明显不同的值：把「物品视图版本」与「会话索引版本」弄混的
#: 任何一处改动都会让下面某条断言红。两者分工不同（见 `protocol.py` 开头）。
SESSION_INDEX_VERSION = 7
SNAPSHOT_VERSION = 10


class RecordingPort(object):
    """只记下发出的包。客户端的端口契约只有 `send(payload)` + `diagnostics()`。"""

    def __init__(self):
        self.sent = []

    def send(self, payload):
        # type: (dict) -> bool
        self.sent.append(payload)
        return True

    def diagnostics(self):
        # type: () -> dict
        return {"stats": {}, "channels": [], "listening": False}


class CacheFixture(unittest.TestCase):
    def setUp(self):
        self.port = RecordingPort()
        self.client = network_client.NetworkClient(port=self.port)
        self.cache = TerminalCache(client=self.client)
        self.feed(SessionReady(SESSION_INDEX_VERSION, 9))

    def feed(self, message, session_id="s1", network_id="n1", epoch="e1"):
        """按线上格式喂一条 S2C 消息，返回解析结果。"""
        return self.client.receive(
            SyncEnvelope(message, session_id, network_id, epoch).to_dict()
        )

    def tick(self):
        """走一帧，取回本帧对界面的影响。**setUp 里那一批快照也要走一帧才
        离开**——返回值是「本帧的影响」，攒着不取的话下一条用例会拿到上一条
        留下的东西。"""
        return self.cache.tick()

    def absorb(self):
        """走一帧，把本帧的影响丢掉。

        **`feed` 只是入队，组装发生在 `tick()` 里**（`take_events` 取走即
        出队）。因此「断言的是组装状态、不是帧结果」的用例必须显式走一帧，
        否则断言看的是一个还没被处理的队列——症状是所有计数都是 0。
        断言帧结果的用例则用 `tick()` 取返回值。
        """
        self.cache.tick()

    def visible(self):
        # type: () -> tuple
        """当前可见的资源键，按显示顺序。"""
        return tuple(self.cache.model.query()[1])

    def stored_of(self, key):
        # type: (object) -> int
        generation = self.cache.model.query()[0]
        for entry in self.cache.model.query_page(generation, 0, 99):
            if entry.key == key:
                return entry.stored
        return None

    def names(self):
        return [key.name for key in self.visible()]

    # -- 线上包 --------------------------------------------------------

    def kinds(self):
        return [item["message"]["kind"] for item in self.port.sent]

    def requests(self):
        return [
            item for item in self.port.sent
            if item["message"]["kind"] == MSG_SNAPSHOT_REQUEST
        ]

    def acks(self):
        return [item for item in self.port.sent if item["message"]["kind"] == MSG_ACK]

    def send_snapshot(self, records, version=SNAPSHOT_VERSION, snapshot_id="snap-1",
                      chunk_size=2):
        """发一份**完整**快照。缺块/重复那几条用例不走这里，它们自己逐包发。"""
        chunks = [
            tuple(records[index:index + chunk_size])
            for index in range(0, len(records), chunk_size)
        ]
        if not chunks:
            chunks = [()]
        self.feed(SnapshotBegin(snapshot_id, version, len(records)))
        for index, items in enumerate(chunks):
            self.feed(SnapshotChunk(snapshot_id, index, items))
        self.feed(SnapshotEnd(snapshot_id, len(chunks)))

    def delta(self, index, count, upserts=(), removes=(),
              base=SNAPSHOT_VERSION, new=SNAPSHOT_VERSION + 1, batch_id="b1"):
        return ItemDelta(
            batch_id, index, count, base, new, tuple(upserts), tuple(removes)
        )


class TestSnapshotAssembly(CacheFixture):
    """快照 = 分片重组。**缺一块就整份丢掉**是这个文件的核心断言。"""

    def test_a_complete_snapshot_replaces_the_list(self):
        self.send_snapshot([(STONE, 40, 0, False), (DIRT, 12, 4, False), (SAND, 3, 0, False)])
        self.absorb()
        self.assertEqual(self.cache.model.count(), 3)
        self.assertEqual(self.cache.model.version(), SNAPSHOT_VERSION)
        self.assertEqual(self.cache.stats["snapshots_applied"], 1)

    def test_chunks_arriving_out_of_order_assemble_the_same(self):
        """分片按流到达，顺序不保证。按下标存，不按到达顺序拼接。"""
        records = [(STONE, 1, 0, False), (DIRT, 2, 0, False), (SAND, 3, 0, False), (APPLE, 4, 0, False)]
        self.feed(SnapshotBegin("snap-1", SNAPSHOT_VERSION, 4))
        for index in (2, 0, 3, 1):
            self.feed(SnapshotChunk(
                "snap-1", index, tuple(records[index:index + 1])
            ))
        self.feed(SnapshotEnd("snap-1", 4))
        self.absorb()
        self.assertEqual(self.cache.model.count(), 4)
        self.assertEqual(self.cache.stats["snapshots_applied"], 1)

    def test_a_duplicate_chunk_index_keeps_the_first(self):
        """重复下标保留第一份：两份内容不同时说不上哪一份对，而先到的那份
        至少与其余分片来自同一次发送。覆盖会让一份快照里混进另一个时间点的
        数据，且没有任何迹象。"""
        self.feed(SnapshotBegin("snap-1", SNAPSHOT_VERSION, 2))
        self.feed(SnapshotChunk("snap-1", 0, ((STONE, 1, 0, False),)))
        self.feed(SnapshotChunk("snap-1", 0, ((DIRT, 2, 0, False),)))
        self.feed(SnapshotChunk("snap-1", 1, ((SAND, 3, 0, False),)))
        self.feed(SnapshotEnd("snap-1", 2))
        self.absorb()
        self.assertEqual(self.cache.stats["duplicate_chunks"], 1)
        self.assertEqual(sorted(self.names()), ["minecraft:sand", "minecraft:stone"])

    def test_a_truncated_snapshot_is_never_applied(self):
        """**缺一块就整份丢掉。** 这是本文件最重要的断言。

        应用半份的后果是「行都在、数量都是真的，只是少了几种」——玩家没有
        任何办法从界面上看出少了什么，而接下来每一条命令都会按一份错的
        库存去发。
        """
        self.feed(SnapshotBegin("snap-1", SNAPSHOT_VERSION, 2))
        self.feed(SnapshotChunk("snap-1", 0, ((STONE, 1, 0, False),)))
        # 只发了一块，`end` 却说总共两块。
        self.feed(SnapshotEnd("snap-1", 2))
        self.absorb()
        self.assertEqual(self.cache.model.count(), 0)
        self.assertEqual(self.cache.stats["truncated_snapshots"], 1)
        self.assertEqual(self.cache.stats["snapshots_applied"], 0)

    def test_a_truncated_snapshot_replaces_nothing_it_had_before(self):
        """丢掉半成品不等于丢掉**已有的**列表。上一份快照还是好的，界面
        应当继续显示它，而不是因为这一次丢包变成空白。"""
        self.send_snapshot([(STONE, 1, 0, False), (DIRT, 2, 0, False)])
        self.tick()
        self.feed(SnapshotBegin("snap-2", SNAPSHOT_VERSION + 5, 2))
        self.feed(SnapshotChunk("snap-2", 0, ((SAND, 9, 0, False),)))
        self.feed(SnapshotEnd("snap-2", 2))
        self.absorb()
        self.assertEqual(self.cache.stats["truncated_snapshots"], 1)
        self.assertEqual(sorted(self.names()), ["minecraft:dirt", "minecraft:stone"])
        self.assertEqual(self.cache.model.version(), SNAPSHOT_VERSION)

    def test_a_truncated_snapshot_is_re_requested_once_per_frame(self):
        """补请求**每帧至多一次**：一帧里三条孤儿 `snapshot_end` 就发三条
        请求的话，服务端会连着推三份快照，而客户端只会用最后一份。"""
        for _ in range(3):
            self.feed(SnapshotEnd("snap-gone", 2))
        self.tick()
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(self.cache.stats["requests_sent"], 1)
        self.assertEqual(self.cache.stats["orphan_ends"], 3)

    def test_the_next_frame_may_request_again(self):
        """「每帧一次」是节流，不是「只此一次」：下一帧真的还没来数据就得
        能再要一次，否则一次丢包就让界面永远停在空白。

        **第一次失败不受退避影响**——退避是从第二次连续失败才开始的。
        """
        self.feed(SnapshotEnd("snap-gone", 2))
        self.tick()
        self.feed(SnapshotEnd("snap-gone", 2))
        self.tick()
        self.assertEqual(len(self.requests()), 2)

    def test_repeated_failures_back_off_instead_of_hammering(self):
        """连续失败要**退避**，不能每帧一次地无限补下去。

        2026-10-01 真机实测撞出来的：一份解不出来的快照会被整份丢掉再要
        一份，下一帧接着来，就这样滚成了 **3210 次补取请求 / 17345 个回包**
        （根因见 `NetworkClient._is_for_this_session`）。「每帧至多一次」是
        个很紧的上限，但它**没有终点**——只要根因还在，服务端就一直被自己
        推出去的快照按住摩擦。

        这条钉两件事：连续失败之后请求数**确实不再跟着帧数涨**，以及
        `requests_backed_off` 把「先别发」如实记下来（与「不必发」分开）。
        """
        # 十四帧，每帧都失败一次。没有退避的话这里就是 14 条请求。
        for _ in range(14):
            self.feed(SnapshotEnd("snap-gone", 2))
            self.tick()
        self.assertLessEqual(self.cache.stats["requests_sent"], 5)
        self.assertGreater(self.cache.stats["requests_backed_off"], 5)
        self.assertEqual(
            self.cache.stats["requests_sent"]
            + self.cache.stats["requests_backed_off"],
            14,
            "每一帧的失败要么发了请求、要么被退避挡下，不该有第三种下场",
        )

    def test_a_complete_snapshot_clears_the_backoff(self):
        """一份完整的快照把退避清掉——它量的就是「一直在失败」这件事。"""
        for _ in range(4):
            self.feed(SnapshotEnd("snap-gone", 2))
            self.tick()
        self.send_snapshot([(STONE, 1, 0, False)])
        self.tick()
        self.assertEqual(self.cache.stats["snapshots_applied"], 1)
        # 退避已清：下一次失败又能立刻补一份。
        before = self.cache.stats["requests_sent"]
        self.feed(SnapshotEnd("snap-gone", 2))
        self.tick()
        self.assertEqual(self.cache.stats["requests_sent"], before + 1)

    def test_the_request_asks_from_the_version_we_actually_applied(self):
        """`from_version` 取**模型已应用到的**版本，不是客户端记的那个
        「服务端最近告知的」。增量收了一半时两者不等，用错会补出一份缺了
        那半批的增量。"""
        self.send_snapshot([(STONE, 1, 0, False)])
        self.tick()
        self.feed(SnapshotEnd("snap-gone", 1))
        self.tick()
        self.assertEqual(
            self.requests()[0]["message"]["from_version"], SNAPSHOT_VERSION
        )
        # 会话索引版本是另一个数，绝不能出现在这里（两个版本各有其名）。
        self.assertNotEqual(SESSION_INDEX_VERSION, SNAPSHOT_VERSION)

    def test_chunks_without_a_begin_are_counted_not_applied(self):
        self.feed(SnapshotChunk("snap-gone", 0, ((STONE, 1, 0, False),)))
        self.absorb()
        self.assertEqual(self.cache.stats["orphan_chunks"], 1)
        self.assertEqual(self.cache.model.count(), 0)

    def test_a_new_snapshot_id_discards_the_half_built_one(self):
        """新 id 顶掉半成品：两份不同快照的分片拼在一起会得到一张
        「哪一半都不是」的表。"""
        self.feed(SnapshotBegin("snap-1", SNAPSHOT_VERSION, 2))
        self.feed(SnapshotChunk("snap-1", 0, ((STONE, 1, 0, False),)))
        self.feed(SnapshotBegin("snap-2", SNAPSHOT_VERSION + 1, 1))
        self.feed(SnapshotChunk("snap-2", 0, ((DIRT, 5, 0, False),)))
        self.feed(SnapshotEnd("snap-2", 1))
        self.absorb()
        self.assertEqual(self.names(), ["minecraft:dirt"])
        self.assertEqual(self.cache.model.version(), SNAPSHOT_VERSION + 1)

    def test_the_same_snapshot_id_keeps_collecting(self):
        """同 id 同版本再来一次 `begin` 是允许的（服务端重发），已收的分片
        要接着用，不能白丢重来。"""
        self.feed(SnapshotBegin("snap-1", SNAPSHOT_VERSION, 2))
        self.feed(SnapshotChunk("snap-1", 0, ((STONE, 1, 0, False),)))
        self.feed(SnapshotBegin("snap-1", SNAPSHOT_VERSION, 2))
        self.feed(SnapshotChunk("snap-1", 1, ((DIRT, 2, 0, False),)))
        self.feed(SnapshotEnd("snap-1", 2))
        self.absorb()
        self.assertEqual(self.cache.model.count(), 2)
        self.assertEqual(self.cache.stats["snapshots_started"], 1)

    def test_the_same_snapshot_id_with_a_new_version_restarts(self):
        """同 id 不同版本：这一份换了内容，已收的分片**必须作废**。"""
        self.feed(SnapshotBegin("snap-1", SNAPSHOT_VERSION, 2))
        self.feed(SnapshotChunk("snap-1", 0, ((STONE, 1, 0, False),)))
        self.feed(SnapshotBegin("snap-1", SNAPSHOT_VERSION + 1, 1))
        self.feed(SnapshotChunk("snap-1", 0, ((DIRT, 5, 0, False),)))
        self.feed(SnapshotEnd("snap-1", 1))
        self.absorb()
        self.assertEqual(self.cache.model.count(), 1)
        self.assertEqual(self.cache.model.version(), SNAPSHOT_VERSION + 1)

    def test_a_total_items_mismatch_is_counted_but_still_applied(self):
        """总数对不上**照常应用**：为了服务端自己报的一个数字拒绝一份完整
        数据，会陷入「永远重取」的死循环——每次重取的结论都一样。"""
        self.feed(SnapshotBegin("snap-1", SNAPSHOT_VERSION, 99))
        self.feed(SnapshotChunk("snap-1", 0, ((STONE, 1, 0, False), (DIRT, 2, 0, False))))
        self.feed(SnapshotEnd("snap-1", 1))
        self.absorb()
        self.assertEqual(self.cache.stats["total_items_mismatch"], 1)
        self.assertEqual(self.cache.model.count(), 2)

    def test_a_snapshot_never_acks_anything(self):
        """**快照分片永远不被 ack。** 服务端发分片时不带 `transfer_id`，硬凑
        一个发过去，`_on_ack` 在 `session.outstanding` 里查不到就会判
        `RESYNC_UNKNOWN_TRANSFER` **把会话关掉**——ack 错了不是「没效果」，
        是「把玩家的界面打掉」。"""
        self.send_snapshot([(STONE, 1, 0, False), (DIRT, 2, 0, False)], chunk_size=1)
        self.tick()
        self.assertEqual(self.acks(), [])
        self.assertEqual(self.cache.stats["acks_sent"], 0)


class TestDeltaBatches(CacheFixture):
    """增量批：**收齐 `packet_count` 个才应用**，缺一个就是断档。"""

    def setUp(self):
        CacheFixture.setUp(self)
        self.send_snapshot([(STONE, 10, 0, False), (DIRT, 5, 0, False)])
        self.tick()

    def test_a_single_packet_batch_applies(self):
        self.feed(self.delta(0, 1, upserts=[(STONE, 7, 0, False)]))
        result = self.tick()
        self.assertEqual(result.version, SNAPSHOT_VERSION + 1)
        self.assertEqual(self.cache.model.version(), SNAPSHOT_VERSION + 1)

    def test_a_multi_packet_batch_applies_only_once_complete(self):
        self.feed(self.delta(0, 2, upserts=[(STONE, 7, 0, False)]))
        self.assertIsNone(self.tick())
        self.assertEqual(self.cache.model.version(), SNAPSHOT_VERSION)
        self.feed(self.delta(1, 2, removes=[DIRT]))
        self.assertIsNotNone(self.tick())
        self.assertEqual(self.cache.model.version(), SNAPSHOT_VERSION + 1)

    def test_the_batch_is_applied_in_packet_order(self):
        """按 `packet_index` 写，不按到达顺序：同一批次里两包可能都写同一个
        键，反过来就是「先到的那份说了算」。"""
        self.feed(self.delta(1, 2, upserts=[(STONE, 99, 0, False)]))
        self.feed(self.delta(0, 2, upserts=[(STONE, 1, 0, False)]))
        self.tick()
        self.assertEqual(self.stored_of(STONE), 99)

    def test_incomplete_batches_are_visible(self):
        """没收齐的批次要**报得出来**：真机上「stats 在涨、列表不动」时，
        第一个要看的就是「有几批缺包」。缺包多久算超时是未定值，因此这里
        只暴露现状，不编一个毫秒数去重发。"""
        self.feed(self.delta(0, 3))
        self.absorb()
        self.assertEqual(self.cache.incomplete_batches(), {"b1": (1, 3)})

    def test_a_duplicate_packet_is_kept_once(self):
        self.feed(self.delta(0, 2, upserts=[(STONE, 7, 0, False)]))
        self.feed(self.delta(0, 2, upserts=[(STONE, 7, 0, False)]))
        self.absorb()
        self.assertEqual(self.cache.stats["duplicate_packets"], 1)
        self.feed(self.delta(1, 2))
        self.assertIsNotNone(self.tick())
        self.assertEqual(self.cache.model.version(), SNAPSHOT_VERSION + 1)

    def test_an_inconsistent_batch_is_restarted(self):
        """同一个 `batch_id` 的包说着不同的基准版本 → **整个重来**。留着旧的
        那几个包会把两个批次的条目拼成一个，而它们各自的基准不同。"""
        self.feed(self.delta(0, 2, upserts=[(STONE, 7, 0, False)]))
        self.feed(self.delta(0, 2, upserts=[(DIRT, 3, 0, False)], base=99, new=100))
        self.absorb()
        self.assertEqual(self.cache.stats["inconsistent_batches"], 1)
        self.assertEqual(self.cache.incomplete_batches(), {"b1": (1, 2)})

    def test_a_republished_batch_is_not_applied_twice(self):
        """**重复投递认得出。** 本仓库实测过客户端事件类被重复投递
        （`shared_event_classes`），同一批增量会到达两次；第二次的基准已经
        对不上，模型会抛 `ListResyncRequired`——于是一次重复投递换来一次
        白重取快照，每开一次界面都来一遍。"""
        self.feed(self.delta(0, 1, upserts=[(STONE, 7, 0, False)]))
        self.tick()
        self.feed(self.delta(0, 1, upserts=[(STONE, 7, 0, False)]))
        self.assertIsNone(self.tick())
        self.assertEqual(self.cache.stats["late_packets"], 1)
        self.assertEqual(self.cache.stats["resync_on_batch"], 0)
        self.assertEqual(self.requests(), [])

    def test_an_ack_carries_the_batch_id_and_the_last_index(self):
        """`transfer_id` 取 `batch_id`（服务端对增量批传的正是它），
        `sequence` 取最大连续下标；**只有应用之后才发**。"""
        self.feed(self.delta(0, 2, upserts=[(STONE, 7, 0, False)]))
        self.absorb()
        self.assertEqual(self.acks(), [])
        self.feed(self.delta(1, 2, removes=[DIRT]))
        self.tick()
        ack = self.acks()[0]["message"]
        self.assertEqual(ack["transfer_id"], "b1")
        self.assertEqual(ack["sequence"], 1)

    def test_a_version_gap_asks_for_a_snapshot_without_clearing_the_model(self):
        """`ListResyncRequired` → **不清模型**。模型的版本仍然有效，直接拿它
        去补一份就行；清了它，接下来每条增量都会再对不上。"""
        self.feed(self.delta(0, 1, upserts=[(STONE, 7, 0, False)], base=99, new=100))
        self.tick()
        self.assertEqual(self.cache.stats["resync_on_batch"], 1)
        self.assertEqual(self.cache.model.version(), SNAPSHOT_VERSION)
        self.assertEqual(self.cache.model.count(), 2)
        self.assertEqual(
            self.requests()[0]["message"]["from_version"], SNAPSHOT_VERSION
        )

    def test_a_version_gap_does_not_ack_the_batch(self):
        """没写进去就不能 ack：服务端会把它从积压里扣掉，而客户端的表里永远
        缺这一批——之后每一批的基准都对不上，表现成「界面再也不更新了」。"""
        self.feed(self.delta(0, 1, base=99, new=100))
        self.tick()
        self.assertEqual(self.acks(), [])

    def test_a_batch_for_an_empty_model_still_applies(self):
        """没有快照不代表不能应用：服务端可能只推增量（本地版本本来就是 0）。"""
        fresh = TerminalCache(client=self.client)
        self.feed(self.delta(0, 1, upserts=[(STONE, 7, 0, False)], base=0, new=1))
        self.assertIsNotNone(fresh.tick())
        self.assertEqual(fresh.model.version(), 1)
        self.assertEqual(fresh.model.count(), 1)


class TestServerResync(CacheFixture):
    """服务端推的 `resync_required` 与模型抛的 `ListResyncRequired` 处置
    **不一样**，这是本文件第二组核心断言。"""

    def setUp(self):
        CacheFixture.setUp(self)
        self.send_snapshot([(STONE, 10, 0, False), (DIRT, 5, 0, False)])
        self.tick()

    def test_resync_clears_the_model_and_does_not_ask(self):
        """清表、丢半成品、然后**等**。服务端在那条路径上已经推了新快照；再
        `request_snapshot` 一次是多余的，而且 `model.clear()` 刻意不重置
        `_version`，拿一个旧版本去请求，回来的增量会应用不到空模型上。"""
        self.feed(ResyncRequired("version_gap", "断了"))
        self.absorb()
        self.assertEqual(self.cache.stats["resync_from_server"], 1)
        self.assertEqual(self.cache.model.count(), 0)
        self.assertTrue(self.cache.awaiting_snapshot())
        self.assertEqual(self.requests(), [])

    def test_resync_drops_half_built_snapshots_and_batches(self):
        self.feed(SnapshotBegin("snap-9", SNAPSHOT_VERSION + 1, 2))
        self.feed(SnapshotChunk("snap-9", 0, ((SAND, 1, 0, False),)))
        self.absorb()
        # 收尾之前先确认现场非空：否则「丢了半成品」这句话没有对象。
        self.assertTrue(self.cache.diagnostics()["snapshot_in_flight"])
        self.feed(ResyncRequired("version_gap", "断了"))
        # 半成品已丢：它的 `end` 现在是一条孤儿，认不出来了。
        self.feed(SnapshotEnd("snap-9", 2))
        self.absorb()
        self.assertEqual(self.cache.stats["orphan_ends"], 1)
        self.assertEqual(self.cache.model.count(), 0)

    def test_deltas_are_dropped_while_waiting_for_the_new_snapshot(self):
        """清表之后到新快照之前，**增量一律丢弃并计数**：那时表是空的，而
        增量的基准还对着清掉之前那一份，写进去就是「用半张表冒充整张表」。"""
        self.feed(ResyncRequired("version_gap", "断了"))
        self.absorb()
        self.feed(self.delta(0, 1, upserts=[(STONE, 7, 0, False)]))
        self.assertIsNone(self.tick())
        self.assertEqual(self.cache.stats["deltas_dropped_while_awaiting"], 1)
        self.assertEqual(self.cache.model.count(), 0)

    def test_the_new_snapshot_ends_the_waiting_window(self):
        self.feed(ResyncRequired("version_gap", "断了"))
        self.absorb()
        self.send_snapshot([(APPLE, 4, 0, False)], version=20, chunk_size=1)
        self.absorb()
        self.assertFalse(self.cache.awaiting_snapshot())
        self.assertEqual(self.cache.model.count(), 1)
        self.assertEqual(self.cache.model.version(), 20)

    def test_no_snapshot_request_is_sent_while_awaiting_one(self):
        """「已经有一份在路上」时不再补请求，也不因为一条孤儿 `snapshot_end`
        而破例。"""
        self.feed(ResyncRequired("version_gap", "断了"))
        self.absorb()
        self.feed(SnapshotEnd("snap-gone", 1))
        self.tick()
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.cache.stats["requests_skipped"], 1)


class TestFrameResult(CacheFixture):
    """`tick()` 的返回值：界面靠它决定要不要重画、往哪儿滚。"""

    def setUp(self):
        CacheFixture.setUp(self)
        self.send_snapshot([(STONE, 10, 0, False), (DIRT, 5, 0, False)])
        self.tick()

    def test_a_quiet_frame_reports_nothing(self):
        self.assertIsNone(self.tick())

    def test_the_result_carries_the_changed_keys_and_the_version(self):
        self.feed(self.delta(0, 1, upserts=[(STONE, 7, 0, False)]))
        result = self.tick()
        self.assertIn(STONE, result.changed_keys)
        self.assertEqual(result.version, SNAPSHOT_VERSION + 1)

    def test_two_batches_in_one_frame_are_merged(self):
        """**合并的是界面需要知道的东西**：只报最后一批的话，前一批动过的行
        界面就不知道要重画。"""
        self.feed(self.delta(0, 1, upserts=[(STONE, 7, 0, False)], new=11, batch_id="b1"))
        self.feed(self.delta(0, 1, upserts=[(DIRT, 2, 0, False)], base=11, new=12,
                             batch_id="b2"))
        result = self.tick()
        self.assertIn(STONE, result.changed_keys)
        self.assertIn(DIRT, result.changed_keys)
        self.assertEqual(result.version, 12)

    def test_a_removal_shows_up_in_removed_keys(self):
        self.feed(self.delta(0, 1, removes=[DIRT]))
        result = self.tick()
        self.assertIn(DIRT, result.removed_keys)
        self.assertEqual(result.version, SNAPSHOT_VERSION + 1)

    def test_a_reorder_in_any_batch_marks_the_whole_frame(self):
        """`reordered` **取或**：这一帧里只要重排过一次，界面就得重排一遍。

        第一批只改量（默认按名称排序，改量不重排），第二批插入一个新键
        （任何排序下顺序都变）。只看最后一批的话，这条会得出 False。
        """
        self.feed(self.delta(0, 1, upserts=[(STONE, 7, 0, False)], new=11, batch_id="b1"))
        self.feed(self.delta(0, 1, upserts=[(APPLE, 1, 0, False)], base=11, new=12,
                             batch_id="b2"))
        result = self.tick()
        self.assertTrue(result.reordered)
        self.assertEqual(result.version, 12)


class TestLifecycle(CacheFixture):
    def test_close_says_goodbye_and_clears_everything(self):
        self.send_snapshot([(STONE, 1, 0, False)], chunk_size=1)
        self.feed(self.delta(0, 2, upserts=[(STONE, 2, 0, False)]))
        self.absorb()
        # 收尾之前先确认现场非空：否则「清干净了」这句话没有任何内容。
        self.assertEqual(self.cache.model.count(), 1)
        self.assertEqual(self.cache.incomplete_batches(), {"b1": (1, 2)})
        self.cache.close()
        self.assertIn("session_close", self.kinds())
        self.assertEqual(self.cache.model.count(), 0)
        self.assertEqual(self.cache.incomplete_batches(), {})
        self.assertIsNone(self.tick())
        self.assertFalse(self.client.has_session())

    def test_close_can_skip_the_goodbye_packet(self):
        """服务端已经判死这个会话时再发一条只会换来又一条错误，而错误码恰恰
        是这一刻没人看的东西。

        反面（`notify=True` 确实发了 `session_close`）在同类的上一条用例里，
        两条合起来才说明「发的与不发的确实是同一件事的两个选项」。
        """
        self.cache.close(notify=False)
        self.assertEqual(self.kinds(), [])
        self.assertEqual(self.cache.model.count(), 0)
        self.assertFalse(self.client.has_session())

    def test_diagnostics_reports_the_assembly_state(self):
        report = self.cache.diagnostics()
        self.assertFalse(report["snapshot_in_flight"])
        self.assertFalse(report["awaiting_snapshot"])
        self.assertIn("truncated_snapshots", report["stats"])
        self.assertIn("client", report)


if __name__ == "__main__":
    unittest.main()
