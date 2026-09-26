# coding=utf-8
"""终端同步中枢：订阅、发布窗口、快照与增量（11-02）。

对应 `terminal_item_index.md` 第 5、6 节。**全部离线**：`server/sync/` 不许
import 引擎，因此这里喂几次 `tick()`、看发出了什么包就够了。

三条贯穿全篇的判据（不是每条用例各写一遍的口径）：

1. **版本只由发布窗口推进**，而窗口只在「有订阅者 + 有脏物品键」时才开。
   因此 `publish_version` 与「客户端手里应该是哪个版本」是同一件事，
   每一次断言都对着它；
2. **物视图只含物品**（流体在索引里但不在此视图），快照与增量都不该出现
   流体键；
3. **收包按会话描述解码**，而不是拿服务端对象直接比对——`resource_ref`
   是会话内的序号，绕过编解码就等于把「线上到底发了什么」这一层跳过去了。

夹具用**真实的** `StorageService`（`testsupport.StorageStand`）而不是存储
替身：替身会把「哪些键算脏」「取走后版本怎么走」重新实现一遍，于是测的是
替身的语义。真仓储才让「窗口取走的正是提交过的那些键」这句话有内容。
"""

import json
import unittest

import testsupport

from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.protocol import (
    Ack,
    MSG_ERROR,
    MSG_ITEM_DELTA,
    MSG_RESYNC_REQUIRED,
    MSG_SNAPSHOT_BEGIN,
    MSG_SNAPSHOT_CHUNK,
    MSG_SNAPSHOT_END,
    MSG_SESSION_READY,
    Reason,
    SessionClose,
    SessionOpen,
    SnapshotRequest,
    SyncEnvelope,
    SYNC_SCHEMA_VERSION,
    VIEW_ITEM,
    VIEW_ORDER,
)
from endernet.common.resource_key import ResourceKey
from endernet.server.sync.sessions import SESSION_ACTIVE, SessionContext
from endernet.server.sync import terminal as sync_terminal
from endernet.server.sync.terminal import SyncPolicy, TerminalSyncHub

NETWORK = "net-1"
SECOND_NETWORK = "net-2"
EPOCH = "epoch-1"

STONE = ResourceKey.item("minecraft:stone")
DIRT = ResourceKey.item("minecraft:dirt")
SAND = ResourceKey.item("minecraft:sand")
GRAVEL = ResourceKey.item("minecraft:gravel")
COBBLE = ResourceKey.item("minecraft:cobblestone")
LOG = ResourceKey.item("minecraft:oak_log")
WATER = ResourceKey.fluid("minecraft:water")


class FakePort(object):
    """记录发出的包；可整体「打不通」，用来测失败与退避。

    **端口契约只有两条**（与 `AdapterSyncPort` 对齐）：正常返回 `True`，
    「没有收件人」这类可预判的情形返回 `False`；**引擎真的出错时才抛**。
    这里走返回值这一路，`reachable = False` 模拟的正是「没人在收」。
    """

    def __init__(self):
        self.sent = []
        self.attempts = 0
        self.reachable = True

    def send(self, player_id, payload):
        # type: (str, dict) -> bool
        self.attempts += 1
        if not self.reachable:
            return False
        self.sent.append((player_id, payload))
        return True


class Fixture(unittest.TestCase):
    """一个（或两个）网络的真实存储 + 中枢 + 记录型端口。"""

    def setUp(self):
        self.policy = SyncPolicy()
        self.port = FakePort()
        #: session_id -> 该会话的资源描述表，用来把发出的包解回来。
        self.descriptions = {}
        self.stands = {}
        self.devices = {}
        self.epochs = {}
        self.hub = TerminalSyncHub(
            policy=self.policy,
            storage_of=self._storage_of,
            context_of=self._context_of,
            port=self.port,
        )
        self.add_network(NETWORK, x=10)
        self.stand = self.stands[NETWORK]
        self.service = self.stand.service
        self.device = self.devices[NETWORK]

    # -- 注入 ----------------------------------------------------------

    def add_network(self, network_id, x, epoch=EPOCH):
        # type: (str, int, str) -> DeviceRef
        """再加一个网络：自己的驱动器、介质与存储服务。"""
        stand = testsupport.StorageStand()
        stand.place(0)
        stand.new_medium()
        stand.attach()
        device = DeviceRef(NodeKey(0, x, 64, 0), 1)
        self.stands[network_id] = stand
        self.devices[network_id] = device
        self.epochs[network_id] = epoch
        return device

    def _storage_of(self, network_id):
        stand = self.stands.get(network_id)
        return None if stand is None else stand.service

    def _context_of(self, player_id, device_ref):
        if device_ref is None:
            return None
        for network_id, device in self.devices.items():
            if device_ref.matches(device):
                return SessionContext(
                    player_id,
                    device,
                    network_id,
                    self.epochs[network_id],
                    "master-1",
                    network_workable=True,
                    player_dimension=0,
                    player_position=device.node_key.as_tuple(),
                )
        return None

    # -- 会话 ----------------------------------------------------------

    def open_view(self, player_id="p1", network_id=NETWORK, request_id="r1"):
        # type: (str, str, str) -> tuple
        """开一个物品视图会话，返回 `(session, 回信)`。"""
        device = self.devices[network_id]
        reply = self.hub.handle(
            SyncEnvelope(SessionOpen(request_id, device, 0), view_kind=VIEW_ITEM),
            player_id,
        )
        session = self.hub.registry.get(reply.session_id)
        # 会话描述表按会话发号，因此解码必须用**同一个**实例；留着它等价于
        # 客户端手里那张表。
        self.descriptions[session.session_id] = session.descriptions
        return session, reply

    def close_view(self, session):
        # type: (object) -> None
        """走客户端的关闭路径（C2S `session_close`），不是直接调 hub。"""
        self.hub.handle(
            SyncEnvelope(
                SessionClose("close-1"),
                session_id=session.session_id,
                network_id=session.network_id,
                epoch=session.epoch,
            ),
            session.player_id,
        )

    def send(self, session, message, request_id="q1"):
        # type: (object, object, str) -> object
        return self.hub.handle(
            SyncEnvelope(
                message,
                session_id=session.session_id,
                network_id=session.network_id,
                epoch=session.epoch,
                view_kind=session.view_kind,
            ),
            session.player_id,
        )

    def ack(self, session, transfer_id, sequence):
        # type: (object, str, int) -> None
        self.send(session, Ack("ack-1", transfer_id, sequence))

    def request_snapshot(self, session, from_version):
        # type: (object, int) -> None
        self.send(session, SnapshotRequest("q1", from_version))

    # -- 收包 ----------------------------------------------------------

    def messages(self, player_id=None):
        # type: (str) -> list
        """把已发出的包解回 `SyncEnvelope`。

        刻意走一遍 JSON 往返：线上就是这么走的，能顺带钉住「这些字段
        真的可序列化」——只断言 Python 对象的话，一个 `tuple` 字段要到
        真机上才暴露。
        """
        out = []
        for pid, payload in self.port.sent:
            if player_id is not None and pid != player_id:
                continue
            wire = json.loads(json.dumps(payload))
            out.append(
                SyncEnvelope.from_dict(
                    wire, self.descriptions.get(wire["session_id"])
                )
            )
        return out

    def of_kind(self, kind, player_id=None):
        # type: (str, str) -> list
        return [
            envelope
            for envelope in self.messages(player_id)
            if envelope.message.kind == kind
        ]

    def kinds(self, player_id=None):
        # type: (str) -> list
        return [envelope.message.kind for envelope in self.messages(player_id)]

    def resync_codes(self, player_id=None):
        # type: (str) -> list
        return [
            envelope.message.code
            for envelope in self.of_kind(MSG_RESYNC_REQUIRED, player_id)
        ]

    # -- 断言用的便捷入口 ----------------------------------------------

    def settle(self, session, ticks=4):
        # type: (object, int) -> None
        """推进几帧，直到会话进入「可收增量」的状态。

        **不假定几帧能走完**：一次 `tick()` 只推进一个阶段（这一帧开传，
        下一帧才发分片），而分片预算是策略字段——改了策略不该让所有用例
        一起红。上限仍然有：卡住要报「卡住了」，不是默默转满帧数。
        """
        for _ in range(ticks):
            self.hub.tick()
            if session.state == SESSION_ACTIVE and not session.snapshot_id:
                return
        self.fail("会话没能在 %d 帧内进入可收增量的状态" % (ticks,))


# ---------------------------------------------------------------------------
# 发布窗口
# ---------------------------------------------------------------------------


class TestPublishWindow(Fixture):
    """取脏、推进版本、下发绝对量。"""

    def test_nobody_subscribed_means_no_take_and_no_version_bump(self):
        """没有订阅者时连碰都不碰：不取脏、不推进、不发包。"""
        self.stand.seed(STONE, 3)
        before = self.service.publish_version
        for _ in range(3):
            self.assertEqual(self.hub.tick(), 0, "没人看就不该有发布窗口")
        self.assertTrue(self.service.has_dirty_items(), "脏键要原样留着")
        self.assertEqual(self.service.publish_version, before)
        self.assertEqual(self.port.sent, [])

    def test_a_snapshot_is_frozen_after_publishing_the_pending_changes(self):
        """§6.1 的顺序：先发布待合并变化，再固定快照，随后才开传输。

        所以第一份快照的版本**不是** 0，而是那个窗口推到的版本；而窗口
        取走的键只存在于快照里，不会再作为增量重发一次。
        """
        self.stand.seed(STONE, 3)
        self.assertEqual(self.service.publish_version, 0)
        session, reply = self.open_view()
        self.assertEqual(reply.message.item_view_version, 0, "回信报的是当时的版本")

        self.hub.tick()
        self.assertEqual(self.service.publish_version, 1, "窗口推到了 1")
        self.assertEqual(self.kinds(), [MSG_SNAPSHOT_BEGIN], "这一帧只开传")
        self.settle(session)
        self.assertEqual(self.service.publish_version, 1, "发分片不再推进版本")
        self.assertEqual(
            self.kinds(),
            [MSG_SNAPSHOT_BEGIN, MSG_SNAPSHOT_CHUNK, MSG_SNAPSHOT_END],
            "开传、分片、结束，一条不多",
        )
        begin = self.of_kind(MSG_SNAPSHOT_BEGIN)[0].message
        self.assertEqual(begin.item_view_version, 1, "快照冻在窗口版本处")
        self.assertEqual(begin.total_items, 1)
        records = [
            item
            for envelope in self.of_kind(MSG_SNAPSHOT_CHUNK)
            for item in envelope.message.items
        ]
        self.assertEqual(records, [(STONE, 3, 0)])
        self.assertEqual(session.item_view_version, 1)

    def test_a_new_session_does_not_also_receive_that_first_window(self):
        """窗口里那批变化由快照覆盖，**不能再作为增量发一遍**。

        重发一次是幂等的，因此这个错不会表现成坏数据——它只表现成每次
        打开界面都多收一批已经收到过的键。幂等的错更容易被放过。
        """
        self.stand.seed(STONE, 3)
        session, _ = self.open_view()
        self.settle(session)
        self.assertEqual(self.of_kind(MSG_ITEM_DELTA), [])

    def test_each_dirty_key_is_sent_once_and_as_an_absolute_value(self):
        session, _ = self.open_view()
        self.settle(session)
        self.port.sent = []

        self.stand.seed(DIRT, 5)
        self.hub.tick()
        deltas = self.of_kind(MSG_ITEM_DELTA)
        self.assertEqual(len(deltas), 1)
        message = deltas[0].message
        self.assertEqual(message.upserts, ((DIRT, 5, 0),))
        self.assertEqual(message.removes, ())
        self.assertEqual(message.packet_count, 1)

        # 再推一帧：没有新提交，就不该有第二个包。
        self.hub.tick()
        self.assertEqual(len(self.of_kind(MSG_ITEM_DELTA)), 1, "同一个键不重复发")

        # 数量变化仍然走同一个键，报的是**绝对量**而不是增量 2。
        self.port.sent = []
        self.stand.seed(DIRT, 2)
        self.hub.tick()
        message = self.of_kind(MSG_ITEM_DELTA)[0].message
        self.assertEqual(message.upserts, ((DIRT, 7, 0),))

    def test_a_later_commit_uses_the_published_version_as_its_base(self):
        """一次窗口 = 一次版本推进。第二批的基准就是第一批推到的那一版。"""
        session, _ = self.open_view()
        self.settle(session)
        self.port.sent = []

        self.stand.seed(DIRT, 1)
        self.hub.tick()
        first = self.of_kind(MSG_ITEM_DELTA)[0].message
        self.port.sent = []
        self.stand.seed(SAND, 1)
        self.hub.tick()
        second = self.of_kind(MSG_ITEM_DELTA)[0].message

        self.assertEqual(first.new_version, second.base_version)
        self.assertEqual(second.new_version, first.new_version + 1)
        self.assertEqual(session.item_view_version, second.new_version)

    def test_two_sessions_at_the_same_version_share_one_frozen_snapshot(self):
        """同一版本上先后打开的两个终端共享同一份冻结记录（§6 末段）。"""
        self.stand.seed(STONE, 2)
        self.open_view(player_id="p1", request_id="r1")
        self.open_view(player_id="p2", request_id="r2")
        self.hub.tick()

        begins = self.of_kind(MSG_SNAPSHOT_BEGIN)
        self.assertEqual(len(begins), 2, "两个会话各自开传")
        self.assertEqual(
            begins[0].message.snapshot_id,
            begins[1].message.snapshot_id,
            "同版本的快照不该复制第二份",
        )
        self.assertEqual(
            self.hub.diagnostics()["networks"][NETWORK]["snapshots"],
            1,
            "只冻结了一份",
        )

    def test_one_window_splits_into_packets_that_share_the_base_and_new_version(self):
        """一批多包：共享 base/new 与 batch_id，靠 packet_index 拼回整批。"""
        self.policy.delta_items_per_packet = 1
        session, _ = self.open_view()
        self.settle(session)
        self.port.sent = []

        for resource in (STONE, DIRT, SAND):
            self.stand.seed(resource, 1)
        self.hub.tick()

        packets = [
            envelope.message for envelope in self.of_kind(MSG_ITEM_DELTA)
        ]
        self.assertEqual(len(packets), 3)
        self.assertEqual([p.packet_index for p in packets], [0, 1, 2])
        self.assertEqual(set(p.packet_count for p in packets), set([3]))
        self.assertEqual(set(p.batch_id for p in packets), set([packets[0].batch_id]))
        self.assertEqual(set(p.base_version for p in packets), set([packets[0].base_version]))
        self.assertEqual(set(p.new_version for p in packets), set([packets[0].new_version]))
        sent = set()
        for packet in packets:
            self.assertTrue(packet.upserts, "分片不该出现空包")
            for key, _stored, _reserved in packet.upserts:
                sent.add(key)
        self.assertEqual(sent, set([STONE, DIRT, SAND]))
        self.assertEqual(session.item_view_version, packets[-1].new_version)

    def test_the_snapshot_and_the_delta_both_leave_fluids_out(self):
        """流体在索引里，但物品视图不含它——快照与增量都不该出现流体键。"""
        self.stand.seed(WATER, 3)
        self.stand.seed(STONE, 2)
        session, _ = self.open_view()
        self.settle(session)

        records = [
            item
            for envelope in self.of_kind(MSG_SNAPSHOT_CHUNK)
            for item in envelope.message.items
        ]
        self.assertEqual(records, [(STONE, 2, 0)], "快照只含物品")
        self.assertIn(WATER, self.service.keys(), "流体确实在索引里")

        self.port.sent = []
        self.stand.seed(WATER, 4)
        self.hub.tick()
        self.assertEqual(
            self.of_kind(MSG_ITEM_DELTA), [], "纯流体提交不产生物品增量"
        )

    def test_an_ack_clears_the_backlog_of_that_transfer(self):
        """确认按传输记账：确认到位后积压归零，而不是一路堆到上限。"""
        session, _ = self.open_view()
        self.settle(session)
        self.port.sent = []

        self.stand.seed(DIRT, 3)
        self.hub.tick()
        batch_id = self.of_kind(MSG_ITEM_DELTA)[0].message.batch_id
        # 积压按**条目**计（一个资源键一条），不是按 stored 里的件数：
        # 限的是包的规模，而包的大小由条目数决定。
        self.assertEqual(session.backlog_items, 1)

        self.ack(session, batch_id, 0)
        self.assertEqual(session.backlog_items, 0)
        self.assertEqual(session.backlog_bytes, 0)
        self.assertEqual(session.outstanding, {})

    def test_an_ack_for_an_unknown_transfer_asks_for_a_resync(self):
        session, _ = self.open_view()
        self.settle(session)
        self.port.sent = []

        self.ack(session, "b999", 0)
        self.assertEqual(self.resync_codes(), [sync_terminal.RESYNC_UNKNOWN_TRANSFER])
        self.assertTrue(session.must_resync)


# ---------------------------------------------------------------------------
# 传输上限：积压、暂存、失败
# ---------------------------------------------------------------------------


class TestTransferLimits(Fixture):
    """有界缓存与失败处理。**溢出不扩容**，一律终止 + 重冻。"""

    def test_backlog_overflow_aborts_the_transfer_and_refreezes(self):
        """跟不上的客户端：已发未确认堆到上限就重取快照，不再往后堆。"""
        self.policy.max_backlog_items = 2
        self.policy.delta_items_per_packet = 1
        session, _ = self.open_view()
        self.settle(session)
        first_snapshot = self.of_kind(MSG_SNAPSHOT_BEGIN)[0].message.snapshot_id
        self.port.sent = []

        # 三个键拆成三包：前两包把积压顶到上限，第三包越界。
        for resource in (DIRT, SAND, GRAVEL):
            self.stand.seed(resource, 1)
        self.hub.tick()
        self.assertEqual(self.resync_codes(), [sync_terminal.RESYNC_BACKLOG_OVERFLOW])
        self.assertEqual(session.backlog_items, 0, "中止时积压账要清掉")

        self.port.sent = []
        self.hub.tick()
        begins = self.of_kind(MSG_SNAPSHOT_BEGIN)
        self.assertEqual(len(begins), 1, "重冻一份，而不是接着发后面的包")
        self.assertNotEqual(begins[0].message.snapshot_id, first_snapshot)

    def test_pending_overflow_during_a_snapshot_aborts_and_refreezes(self):
        """快照在途时的暂存也有上限：积压两批（上限 1）就该重来。"""
        self.policy.snapshot_chunk_items = 1
        self.policy.snapshot_chunks_per_frame = 1
        self.policy.max_pending_delta_batches = 1
        for resource in (STONE, DIRT, SAND, GRAVEL, COBBLE):
            self.stand.seed(resource, 1)
        session, _ = self.open_view()

        self.hub.tick()  # 冻结 + 开传
        self.assertEqual(len(self.of_kind(MSG_SNAPSHOT_CHUNK)), 0)
        self.hub.tick()  # 首片
        self.assertEqual(len(self.of_kind(MSG_SNAPSHOT_CHUNK)), 1)
        self.stand.seed(LOG, 1)
        self.hub.tick()  # 第二片 + 第一批暂存（到上限，还不算溢出）
        self.assertEqual(len(self.of_kind(MSG_SNAPSHOT_CHUNK)), 2)
        self.assertEqual(self.resync_codes(), [])
        self.stand.seed(COBBLE, 1)  # **物品**：纯流体提交不产生物品增量
        self.hub.tick()  # 第三片 + 第二批暂存 -> 溢出

        self.assertEqual(
            self.resync_codes(), [sync_terminal.RESYNC_PENDING_OVERFLOW]
        )
        self.assertTrue(session.must_resync)

    def test_consecutive_send_failures_back_off_then_recover(self):
        """一直发不出去：连败到阈值后退避，而不是每帧重冻一份快照。"""
        self.policy.max_failures = 3
        self.policy.backoff_frames = 3
        self.stand.seed(STONE, 1)
        session, _ = self.open_view()
        self.port.reachable = False

        for _ in range(3):
            self.hub.tick()
        self.assertEqual(self.port.sent, [])
        self.assertTrue(session.fail_count >= 3)
        self.assertTrue(session.must_resync)
        attempts = self.port.attempts

        # 退避期内一次都不再尝试。
        self.hub.tick()
        self.hub.tick()
        self.assertEqual(self.port.attempts, attempts, "退避期不该继续撞墙")

        self.port.reachable = True
        self.hub.tick()
        self.assertEqual(len(self.of_kind(MSG_SNAPSHOT_BEGIN)), 1, "退避过后恢复")
        self.assertEqual(session.fail_count, 0, "恢复即清零")

    def test_one_successful_send_clears_the_failure_streak(self):
        """`fail_count` 是**连续**失败数：一次成功就是那个「连续」的断点。

        只在确认里清零不够——快照分片不带传输号、客户端也不会为它确认，
        于是只收快照的会话永远等不到清零，此后任何一次偶发失败都会立刻
        触发中止，而它本该只是重发一条。
        """
        self.policy.max_failures = 3
        self.stand.seed(STONE, 1)
        session, _ = self.open_view()
        self.port.reachable = False

        self.hub.tick()
        self.assertEqual(session.fail_count, 1)

        self.port.reachable = True
        self.hub.tick()
        self.assertEqual(session.fail_count, 0)
        self.settle(session)

    def test_a_resource_table_that_runs_out_becomes_a_resync_not_an_exception(self):
        """资源描述表满 → 转成 `resync_required`，**绝不外抛**。

        这是每帧都会走到的路径：一个异常打挂的是整帧，不只是这个会话。
        """
        session, _ = self.open_view()
        self.settle(session)
        # 把表容量压到 1，第二个新键就会在 `encode` 里撞上限。改的是
        # 会话自己的公开字段，不是内部结构。
        session.descriptions.max_entries = 1
        self.port.sent = []

        self.stand.seed(DIRT, 1)
        self.stand.seed(SAND, 1)
        self.hub.tick()  # 必须**正常返回**，不得抛异常

        self.assertEqual(
            self.resync_codes(), [sync_terminal.RESYNC_RESOURCE_TABLE_FULL]
        )


# ---------------------------------------------------------------------------
# 订阅清理与版本链
# ---------------------------------------------------------------------------


class TestSubscriptions(Fixture):
    """最后一个订阅者离开就整个回收；版本链不许凭空续上。"""

    def test_the_last_subscriber_leaving_reclaims_the_history_and_the_cache(self):
        session, _ = self.open_view()
        self.settle(session)
        self.stand.seed(DIRT, 1)
        self.hub.tick()
        self.assertEqual(
            self.hub.diagnostics()["networks"][NETWORK]["history"], 1
        )

        self.close_view(session)
        self.assertEqual(self.hub.diagnostics()["networks"], {})
        self.assertEqual(self.hub.stats["networks_dropped"], 1)

    def test_closing_sessions_does_not_touch_the_storage_service(self):
        """同步层只读业务状态：关掉界面不该让库存或版本动一下。"""
        session, _ = self.open_view()
        self.settle(session)
        self.stand.seed(DIRT, 1)
        self.hub.tick()
        keys_before = self.service.keys()
        version_before = self.service.publish_version
        loads_before = list(self.stand.loads)

        self.close_view(session)
        self.hub.tick()

        self.assertEqual(self.service.keys(), keys_before)
        self.assertEqual(self.service.publish_version, version_before)
        self.assertEqual(self.stand.loads, loads_before)

    def test_reopening_after_everyone_left_keeps_the_version_chain(self):
        """重建的订阅表必须对齐存储的当前版本，而不是从 0 重新数。

        订阅表随最后一个会话一起被丢掉，里面记着 `publish_version`。
        重建时若默认从 0 起，第一个批次就会声称 `0 -> N`——一个纯属编造
        的版本链，客户端会判定缺口并永远重取快照。
        """
        session, _ = self.open_view()
        self.settle(session)
        self.stand.seed(DIRT, 1)
        self.hub.tick()
        version = self.service.publish_version
        self.close_view(session)
        self.assertTrue(self.service.has_dirty_items() is False)

        self.port.sent = []
        second, reply = self.open_view(player_id="p2")
        self.assertEqual(reply.message.item_view_version, version, "回信对齐当前版本")
        self.settle(second)
        begin = self.of_kind(MSG_SNAPSHOT_BEGIN)[0].message
        self.assertEqual(begin.item_view_version, version)

        self.port.sent = []
        self.stand.seed(SAND, 1)
        self.hub.tick()
        delta = self.of_kind(MSG_ITEM_DELTA)[0].message
        self.assertEqual(delta.base_version, version, "基准接得上，不是 0")

    def test_a_publish_version_moved_behind_our_back_triggers_a_resync(self):
        """版本被别处推进过 → 让客户端重取，而不是按自己记的基准发增量。

        继续发的话客户端每条都会判缺口、反复重取，而每次都因为同一个
        原因失败——表现成「界面永远转圈」。
        """
        session, _ = self.open_view()
        self.settle(session)
        self.port.sent = []

        # 绕过本模块取走脏键：等价于「别处也发布了同一个视图」。
        self.stand.seed(DIRT, 1)
        self.service.take_dirty_keys()
        self.hub.tick()

        self.assertEqual(self.resync_codes(), [sync_terminal.RESYNC_VERSION_GAP])
        self.assertEqual(self.hub.stats["version_chain_breaks"], 1)
        self.assertEqual(
            self.hub.diagnostics()["networks"][NETWORK]["publish_version"],
            self.service.publish_version,
            "断链之后基准要跟上去",
        )


# ---------------------------------------------------------------------------
# 历史与重取
# ---------------------------------------------------------------------------


class TestHistory(Fixture):
    """增量历史的三重上限与「接不上就重取」。"""

    def setUp(self):
        Fixture.setUp(self)
        self.policy.history_batches = 2
        self.session, _ = self.open_view()
        self.settle(self.session)
        self.port.sent = []
        # 三个窗口，让历史超出一条：开界面时没有待发布的改动，因此三个
        # 窗口就是 v0->v1、v1->v2、v2->v3。
        for resource in (DIRT, SAND, GRAVEL):
            self.stand.seed(resource, 1)
            self.hub.tick()
        self.assertEqual(self.service.publish_version, 3)

    def test_history_is_trimmed_by_its_batch_limit(self):
        self.assertEqual(
            self.hub.diagnostics()["networks"][NETWORK]["history"], 2
        )

    def test_a_client_that_is_current_gets_nothing(self):
        """已经是最新：回空列表，**不发**任何东西。"""
        self.port.sent = []
        self.request_snapshot(self.session, self.session.item_view_version)
        self.hub.tick()
        self.assertEqual(self.port.sent, [])

    def test_a_client_behind_the_oldest_history_is_told_to_resync(self):
        """缺口早于历史起点：只能重发快照，补增量已经补不上了。"""
        self.port.sent = []
        self.request_snapshot(self.session, 0)
        self.hub.tick()
        begins = self.of_kind(MSG_SNAPSHOT_BEGIN)
        self.assertEqual(len(begins), 1)
        self.assertEqual(begins[0].message.item_view_version, 3)

    def test_a_client_the_history_still_covers_gets_only_the_deltas(self):
        """缺口还在历史里：只补增量，省掉一次整份复制与传输。"""
        self.port.sent = []
        self.request_snapshot(self.session, 2)
        self.hub.tick()
        self.assertEqual(self.of_kind(MSG_SNAPSHOT_BEGIN), [])
        deltas = self.of_kind(MSG_ITEM_DELTA)
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0].message.base_version, 2)
        self.assertEqual(deltas[0].message.new_version, 3)


# ---------------------------------------------------------------------------
# 跨网络预算
# ---------------------------------------------------------------------------


class TestCrossNetworkBudget(Fixture):
    """预算按网络轮转，不是一个网络吃满、后面的永久饿死。"""

    def setUp(self):
        Fixture.setUp(self)
        self.second_device = self.add_network(SECOND_NETWORK, x=40)
        self.second_service = self.stands[SECOND_NETWORK].service
        self.policy.publish_budget_keys = 1

    def test_the_budget_serves_one_network_a_frame_and_rotates(self):
        """预算 1 时一帧只够一个网络，但**下一帧轮到另一个**。

        刻意不断言「先轮到谁」：环序起点由 `RotationCursor` 逐帧前移，
        而它已经走过几帧取决于此前的会话建立过程。断言的应是轮转性质
        （收敛 + 不偏向），不是某一帧的具体排列——后者会在别处多推一帧
        之后莫名其妙地红。
        """
        first, _ = self.open_view(player_id="p1", network_id=NETWORK)
        second, _ = self.open_view(
            player_id="p2", network_id=SECOND_NETWORK, request_id="r2"
        )
        self.settle(first)
        self.settle(second)

        self.stand.seed(STONE, 1)
        self.stands[SECOND_NETWORK].seed(DIRT, 1)

        frames = []
        for _ in range(2):
            self.port.sent = []
            self.hub.tick()
            frames.append([pid for pid, _payload in self.port.sent])

        self.assertEqual([len(frame) for frame in frames], [1, 1], "一帧一个网络")
        self.assertNotEqual(frames[0], frames[1], "下一帧轮到另一个")
        self.assertEqual(self.service.publish_version, 1)
        self.assertEqual(self.second_service.publish_version, 1)


# ---------------------------------------------------------------------------
# 边界：拒绝与死路
# ---------------------------------------------------------------------------


class TestRefusals(Fixture):
    """会话不存在时的回答。**不留悬念**——不回话等于界面不响应。"""

    def test_a_request_for_an_unknown_session_gets_an_error_not_silence(self):
        reply = self.hub.handle(
            SyncEnvelope(
                Ack("ack-1", "b1", 0),
                session_id="s404",
                network_id=NETWORK,
                epoch=EPOCH,
            ),
            "p1",
        )
        self.assertIsNotNone(reply)
        self.assertEqual(reply.message.kind, MSG_ERROR)
        self.assertEqual(reply.message.code, Reason.SESSION_INVALID)
        self.assertEqual(self.port.sent, [], "错误是回给调用方的，不是广播的")

    def test_a_session_cannot_be_used_by_another_player(self):
        session, _ = self.open_view(player_id="p1")
        reply = self.hub.handle(
            SyncEnvelope(
                Ack("ack-1", "b1", 0),
                session_id=session.session_id,
                network_id=NETWORK,
                epoch=EPOCH,
            ),
            "p2",
        )
        # **不告诉他这条会话存在**：与「不存在」是同一个回答。
        self.assertEqual(reply.message.code, Reason.SESSION_INVALID)

    def test_an_order_view_session_is_refused_rather_than_opened(self):
        """`order` 是**已知**种类但还没有生产者：拒绝的理由要和「发错了」分开。

        混淆这两种的理由会让「订单终端还没做」表现成「你的客户端发错了
        消息」——排查方向从一开始就是错的。
        """
        device = self.devices[NETWORK]
        reply = self.hub.handle(
            SyncEnvelope(
                SessionOpen("r1", device, 0), view_kind=VIEW_ORDER
            ),
            "p1",
        )
        self.assertEqual(reply.message.kind, MSG_ERROR)
        self.assertEqual(reply.message.code, Reason.UNSUPPORTED)
        self.assertEqual(reply.message.request_id, "r1")
        self.assertEqual(self.hub.registry.count(), 0, "不能留下半个会话")
        self.assertEqual(self.port.sent, [])

    def test_the_error_envelope_needs_no_session(self):
        """`error` 是「S2C 必须带会话」唯一的例外，否则只能硬编一个假会话。"""
        reply = self.hub.handle(
            SyncEnvelope(SnapshotRequest("q1", 0), session_id="s404"), "p1"
        )
        self.assertEqual(reply.session_id, "s404")
        self.assertEqual(reply.network_id, "")
        # 解回来仍然认得出是关于哪次请求的。
        wire = json.loads(json.dumps(reply.to_dict()))
        decoded = SyncEnvelope.from_dict(wire)
        self.assertEqual(decoded.message.request_id, "q1")


class TestWireEntry(Fixture):
    """`receive()`：C2S 的**线上入口**——原始字典进，编码好的回包出。

    它与 `handle()` 只差两件事：**解析**在入口做，**编码**也在这里做。
    编码要用该会话的资源描述表，而那张表是**逐会话发号**的，只有 hub
    拿得到；让引擎适配器自己去 `registry.get(...)` 取表，等于把会话表的
    内部结构摊到引擎边界上。这就是这一步放在 hub 而不是适配器的理由。

    因此这里的用例都走一遍真的 JSON 往返——与线上完全一致。
    """

    def wire(self, message, **kwargs):
        # type: (object, object) -> dict
        """按客户端会发出来的样子造一个原始字典。"""
        return json.loads(json.dumps(SyncEnvelope(message, **kwargs).to_dict()))

    def decode(self, payload):
        # type: (dict) -> object
        """把回包按客户端的方式解回来：**用该会话自己的描述表**。"""
        session = self.hub.registry.get(payload["session_id"])
        descriptions = None if session is None else session.descriptions
        return SyncEnvelope.from_dict(payload, descriptions)

    def test_a_well_formed_open_is_answered_with_a_decodable_payload(self):
        """回包必须**当场解得开**。

        快照与增量的资源是**序号**，解号要那张逐会话的表。一旦回包用的表
        与客户端手里的不是同一张，症状是客户端显示一片空白，而服务端这边
        每一处都「成功」——所以这条只能在往返里测。
        """
        data = self.wire(SessionOpen("r1", self.devices[NETWORK], 0))
        payload = self.hub.receive("p1", data)
        self.assertIsNotNone(payload)
        envelope = self.decode(payload)
        self.assertEqual(envelope.message.kind, MSG_SESSION_READY)
        self.assertEqual(envelope.epoch, EPOCH)
        self.assertEqual(envelope.network_id, NETWORK)

    def test_the_session_belongs_to_the_caller_not_to_the_payload(self):
        """身份由**调用方**给（引擎的 `__id__`），消息体里没有这个字段。

        协议层连字段都没定义，因此这里能测的正是「hub 用的是哪一个」。
        """
        data = self.wire(SessionOpen("r1", self.devices[NETWORK], 0))
        payload = self.hub.receive("p2", data)
        session = self.hub.registry.get(payload["session_id"])
        self.assertEqual(session.player_id, "p2")

    def test_an_error_reply_carries_the_request_id(self):
        """会话不存在时回 `error`，带原请求号——界面才挂得回那次操作。

        `error` 也是 S2C 里唯一允许不带会话的一条（`SyncEnvelope._validate`），
        因为此时服务端根本没有可填的会话。
        """
        data = self.wire(
            Ack("ack-1", "b1", 0),
            session_id="s404",
            network_id=NETWORK,
            epoch=EPOCH,
        )
        payload = self.hub.receive("p1", data)
        self.assertIsNotNone(payload)
        envelope = SyncEnvelope.from_dict(payload)
        self.assertEqual(envelope.message.kind, MSG_ERROR)
        self.assertEqual(envelope.message.code, Reason.SESSION_INVALID)
        self.assertEqual(envelope.message.request_id, "ack-1")

    def test_garbage_is_not_answered_but_is_counted(self):
        """连信封都解不出来时**不回话**。

        回一条无名无姓的 `error` 只会让界面显示「出错了」而说不出是哪次；
        而抛出去打挂的是这一帧（引擎每秒调用数十次）。计数是唯一诚实的
        出口：`bad_packets` 非零就是版本不匹配或对面发了脏数据。
        """
        before = self.hub.stats["bad_packets"]
        self.assertIsNone(self.hub.receive("p1", {"nonsense": True}))
        self.assertIsNone(self.hub.receive("p1", "not a dict"))
        self.assertIsNone(self.hub.receive("p1", None))
        self.assertIsNone(self.hub.receive("p1", []))
        self.assertEqual(self.hub.stats["bad_packets"], before + 4)
        self.assertEqual(self.port.sent, [])

    def test_an_unknown_message_kind_is_not_answered(self):
        """未知种类是**协议错误**，不是「不认识就跳过」。

        静默跳过会让「客户端版本比服务端新」表现成「这个功能没反应」。
        """
        data = self.wire(SessionOpen("r1", self.devices[NETWORK], 0))
        data["message"] = {"kind": "no_such_kind"}
        self.assertIsNone(self.hub.receive("p1", data))
        self.assertEqual(self.hub.stats["bad_packets"], 1)

    def test_a_future_schema_version_is_not_answered(self):
        """版本先于一切：拿旧解析器读新格式，最危险的是「能解析但意思不对」。"""
        data = self.wire(SessionOpen("r1", self.devices[NETWORK], 0))
        data["schema_version"] = SYNC_SCHEMA_VERSION + 1
        self.assertIsNone(self.hub.receive("p1", data))
        self.assertEqual(self.hub.stats["bad_packets"], 1)

    def test_a_message_that_needs_no_answer_returns_none(self):
        """`handle` 返回 `None` 时**不发空包**，且入口照样如实返回 `None`。

        「不回」本身是有含义的：这条消息不需要回答。补一个空包会让客户端
        不断收到无法归类的东西；而在这里编一条回包，等于把「关闭成功」
        表现成一次多余的往返。
        """
        session, _ = self.open_view()
        data = self.wire(
            SessionClose("close-1"),
            session_id=session.session_id,
            network_id=session.network_id,
            epoch=session.epoch,
        )
        self.assertIsNone(self.hub.receive("p1", data))
        self.assertIsNone(self.hub.registry.get(session.session_id))
        self.assertEqual(self.port.sent, [])


if __name__ == "__main__":
    unittest.main()
