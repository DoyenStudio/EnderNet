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
    BlockReason,
    CommandKind,
    CommandResult,
    MSG_COMMAND_RESULT,
    MSG_ERROR,
    MSG_ITEM_DELTA,
    MSG_RESYNC_REQUIRED,
    MSG_SNAPSHOT_BEGIN,
    MSG_SNAPSHOT_CHUNK,
    MSG_SNAPSHOT_END,
    MSG_SESSION_READY,
    MSG_TEMPLATE_STATE,
    Reason,
    SessionClose,
    SessionOpen,
    SnapshotRequest,
    SyncEnvelope,
    SYNC_SCHEMA_VERSION,
    TerminalCommand,
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
        #: 每请求复核要读的外部事实。测试改这几个字典就能模拟「玩家走开了」
        #: 「换了维度」「网络不可用」，而不用另造一份上下文——复核走的是
        #: 与开会话**同一个** `context_of`，两者必须看到同一批事实。
        self.positions = {}
        self.dimensions = {}
        self.workable = {}
        #: 手动存取命令的**执行方替身**。真的执行方是 `MasterController`
        #: （经 `ServerBootstrap.submit_command`），而它属于 `server/network/`，
        #: 不在本模块的可见范围内——注入回调正是为了这里能只测「命令是怎么
        #: 拼出来的」，不去驱动整条主控链路（那条链路的用例在
        #: `test_sync_commands` 之外的各服务测试里）。
        self.submissions = []
        self.submit_result = None
        self.submit_error = None
        self.hub = TerminalSyncHub(
            policy=self.policy,
            storage_of=self._storage_of,
            context_of=self._context_of,
            port=self.port,
            submit=self._submit,
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

    def _submit(self, command):
        # type: (object) -> object
        """记录命令并按 `submit_result` / `submit_error` 作答。"""
        self.submissions.append(command)
        if self.submit_error is not None:
            raise self.submit_error
        if self.submit_result is not None:
            return self.submit_result
        return CommandResult.ok(command.request_id, {"action": "stand-in"})

    def _context_of(self, player_id, device_ref):
        """按**坐标**查，返回注册表里**当前**那一台——与生产同形。

        **不能按 `matches` 查**：生产里是「拿坐标去注册表问现在是谁」
        （`runtime_of(node_key)` + `registry.get(node_key)`），于是拆掉又在原地
        放一个时，问到的是一台代次更大的新设备。按 `matches` 查的话，那种
        情形会退化成「查不到上下文」——结论碰巧一样（都是关掉会话），
        但 `_drop_stale_devices` 里「代次变了」与「设备没了」两条分支就再也
        测不出来了，而这正是复核与注销要分开计数的理由。
        """
        if device_ref is None:
            return None
        for network_id, device in self.devices.items():
            if device.node_key.as_key() == device_ref.node_key.as_key():
                return SessionContext(
                    player_id,
                    device,
                    network_id,
                    self.epochs[network_id],
                    "master-1",
                    network_workable=self.workable.get(network_id, True),
                    player_dimension=self.dimensions.get(player_id, 0),
                    player_position=self.positions.get(
                        player_id, device.node_key.as_tuple()
                    ),
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

    def send_as(self, session, message, player_id):
        # type: (object, object, str) -> object
        """以**另一个玩家**的身份发一条消息，用来测「别人的会话」。

        `player_id` 是引擎在事件上附的那个值（`__id__`），因此伪造它不需要
        动信封——信封里本来就没有玩家字段，这正是那条设计要保证的。
        """
        return self.hub.handle(
            SyncEnvelope(
                message,
                session_id=session.session_id,
                network_id=session.network_id,
                epoch=session.epoch,
                view_kind=session.view_kind,
            ),
            player_id,
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

    def error_payloads(self, player_id="p1"):
        # type: (str) -> list
        """端口上发给该玩家的 `error` **原始载荷**（还没解码的那一层）。

        **不解码是有意的**：服务端单方面注销那几条通知带的是**空**会话号，
        而 `SyncEnvelope.from_dict` 会拿 `session_id` 去查会话描述表——
        查不到虽然不炸，但这里要断言的正是那几个字段本身（尤其是「空」），
        走一遍解码只会把要测的东西绕过去。
        """
        return [
            payload
            for pid, payload in self.port.sent
            if pid == player_id and payload["message"]["kind"] == MSG_ERROR
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


class InventoryPrimitives(object):
    def __init__(self):
        self.items = {}

    def player_slot_count(self, player_id):
        return 36

    def player_item(self, player_id, slot):
        return self.items.get((player_id, slot))


class TestInventoryVersions(Fixture):
    def setUp(self):
        Fixture.setUp(self)
        from endernet.server.adapters.containers_engine import EngineInventoryProvider
        from endernet.client.network_client import NetworkClient
        self.primitives = InventoryPrimitives()
        self.provider = EngineInventoryProvider(self.primitives)
        self.service.inventories = self.provider
        self.client = NetworkClient()
        self.session, reply = self.open_view()
        self.client.receive(reply.to_dict())

    def set_slot(self, name):
        self.primitives.items[("p1", 0)] = testsupport.engine_item(name, 4)

    def latest(self):
        return self.of_kind("inventory_snapshot", "p1")[-1]

    def test_sync_and_command_share_versions_and_reject_observed_aba(self):
        from endernet.server.storage.transfer import SlotAnchor
        self.set_slot("minecraft:stone")
        self.hub.tick()
        first = self.latest()
        self.client.receive(first.to_dict())
        old = self.client.inventory_slots[0]["slot_version"]
        self.set_slot("minecraft:dirt")
        self.hub.tick()
        self.set_slot("minecraft:stone")
        self.hub.tick()
        endpoint = self.provider.endpoint_of("p1")
        verdict = endpoint.anchor_verdict(SlotAnchor(0, STONE, old))
        self.assertEqual(verdict[1], Reason.STALE_VERSION)
        self.client.receive(self.latest().to_dict())
        fresh = self.client.inventory_slots[0]["slot_version"]
        self.assertGreater(fresh, old)
        self.assertTrue(endpoint.anchor_verdict(SlotAnchor(0, STONE, fresh))[0])
        # 迟到的旧快照不能覆盖当前版本。
        self.assertIsNone(self.client.receive(first.to_dict()))
        self.assertEqual(self.client.inventory_slots[0]["slot_version"], fresh)

    def test_unchanged_snapshot_is_not_resent_and_send_failure_retries(self):
        self.set_slot("minecraft:stone")
        self.port.reachable = False
        self.hub.tick()
        self.assertEqual(self.of_kind("inventory_snapshot"), [])
        self.port.reachable = True
        self.hub.tick()
        self.hub.tick()
        self.assertEqual(len(self.of_kind("inventory_snapshot")), 1)

    def test_player_and_session_isolation_and_close_cleanup(self):
        other, reply = self.open_view(player_id="p2", request_id="r2")
        self.set_slot("minecraft:stone")
        self.hub.tick()
        first = self.latest()
        self.client.receive(first.to_dict())
        second = self.of_kind("inventory_snapshot", "p2")[-1]
        self.assertEqual(second.message.slots[0]["canonical"], "")
        self.assertIsNone(self.client.receive(second.to_dict()))
        self.assertEqual(self.client.session_id, self.session.session_id)
        self.close_view(self.session)
        self.assertNotIn(self.session.session_id, self.hub._inventory_sent)
        self.client.drop_session()
        self.assertEqual(self.client.inventory_slots, {})
        self.assertIsNone(self.client.receive(first.to_dict()))

    def test_read_failure_does_not_send_empty_snapshot(self):
        self.set_slot("minecraft:stone")
        self.hub.tick()
        self.provider.endpoint_of("p1").adapter.invalidate()
        self.hub.tick()
        self.assertEqual(len(self.of_kind("inventory_snapshot")), 1)


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
# 每请求复核
# ---------------------------------------------------------------------------


class TestPerRequestRevalidation(Fixture):
    """会话开着不等于一直成立：每条请求都要重新问一遍「它还成立吗」。

    **不查的代价不是报错**，是服务端持续把库存组包发给一个已经走出交互
    距离、或已经换了维度的客户端——实机上看起来只是「界面还开着」，直到
    TTL 到点。因此这里的断言分两层：拒绝**并且**把会话关掉（只拒不放，
    订阅表会留着一个永远不该再收包的人）。
    """

    def walk_away(self, player_id="p1", x=999.0):
        """把玩家挪到远处。**改的是上下文解析的那份事实**，不是客户端说的
        话——客户端根本没有「我在哪」这个字段，位置由服务端自己查。"""
        self.positions[player_id] = (x, 64.0, 0.0)

    def test_a_valid_session_keeps_being_served(self):
        """复核通过 = **什么都不发生**：没有错误回包，会话照旧活着。

        `SnapshotRequest` 走通时不回包（快照从这里开始，客户端从后续的
        `snapshot_*` 拿结果），因此这里断言的是「回包为 None、也没有 error」，
        而不是某个正常回包的形状。
        """
        session, _ = self.open_view()
        self.assertIsNone(self.send(session, SnapshotRequest("q1", 0)))
        self.assertEqual(self.of_kind(MSG_ERROR), [])
        self.assertIsNotNone(self.hub.registry.get(session.session_id))
        self.assertEqual(self.hub.stats["revalidate_refused"], 0)

    def test_walking_out_of_range_refuses_and_closes(self):
        session, _ = self.open_view()
        self.walk_away()

        reply = self.send(session, SnapshotRequest("q1", 0))

        self.assertEqual(reply.message.kind, MSG_ERROR)
        self.assertEqual(reply.message.code, Reason.OUT_OF_RANGE)
        self.assertIsNone(self.hub.registry.get(session.session_id), "必须一并关掉")
        self.assertEqual(self.hub.stats["revalidate_refused"], 1)
        self.assertEqual(
            session.close_reason, "revalidate:" + Reason.OUT_OF_RANGE
        )

    def test_a_refused_revalidation_leaves_no_subscription(self):
        """拒绝之后**不能**留下订阅表：留着它，发布窗口每帧都以为还有人在看，
        于是继续对着一批早已离线的玩家组包——那正是 11-02 要防的空转。"""
        session, _ = self.open_view()
        self.walk_away()

        self.send(session, SnapshotRequest("q1", 0))

        self.assertEqual(self.hub.diagnostics()["networks"], {})
        self.assertEqual(self.hub.registry.count(), 0)

    def test_an_ack_is_revalidated_too(self):
        """**确认包是这条校验最关键的一条**：快照与增量的实际投递挂在它上面。

        只复核「取快照」而不复核「确认」，等于人走开了之后服务端还在按他的
        确认继续推分片——而分片里装的是库存内容。
        """
        session, _ = self.open_view()
        self.settle(session)
        self.port.sent = []
        self.stand.seed(DIRT, 3)
        self.hub.tick()
        batch_id = self.of_kind(MSG_ITEM_DELTA)[0].message.batch_id
        self.assertEqual(session.backlog_items, 1, "先确认这条增量真的在途")

        self.walk_away()
        self.ack(session, batch_id, 0)

        self.assertIsNone(self.hub.registry.get(session.session_id))
        self.assertEqual(self.hub.stats["revalidate_refused"], 1)
        self.assertEqual(session.backlog_items, 1, "被拒的确认不该改动积压账")

    def test_the_boundary_distance_is_still_served(self):
        """边界含端点，复核这一侧也是。写死一个数字就没这条了。"""
        session, _ = self.open_view()
        device = self.devices[NETWORK]
        limit = self.hub.registry.policy.max_interaction_distance
        self.positions["p1"] = (device.node_key.x + limit, 64.0, 0.0)

        self.send(session, SnapshotRequest("q1", 0))

        self.assertEqual(self.of_kind(MSG_ERROR), [])
        self.assertIsNotNone(self.hub.registry.get(session.session_id))
        self.assertEqual(self.hub.stats["revalidate_refused"], 0)

    def test_changing_dimension_closes_the_session(self):
        session, _ = self.open_view()
        self.dimensions["p1"] = 1

        reply = self.send(session, SnapshotRequest("q1", 0))

        self.assertEqual(reply.message.code, Reason.SESSION_INVALID)
        self.assertIsNone(self.hub.registry.get(session.session_id))

    def test_a_network_that_stops_working_closes_the_session(self):
        session, _ = self.open_view()
        self.workable[NETWORK] = False

        reply = self.send(session, SnapshotRequest("q1", 0))

        self.assertEqual(reply.message.code, Reason.WRONG_NETWORK)
        self.assertIsNone(self.hub.registry.get(session.session_id))

    def test_a_replaced_device_closes_the_session(self):
        """拆掉又在原地放一个：复核走的是与开会话同一句 `matches`。"""
        session, _ = self.open_view()
        self.devices[NETWORK] = DeviceRef(self.device.node_key, 2)

        reply = self.send(session, SnapshotRequest("q1", 0))

        self.assertEqual(reply.message.code, Reason.STALE_INSTANCE)
        self.assertIsNone(self.hub.registry.get(session.session_id))

    def test_a_device_that_left_its_network_closes_the_session(self):
        """设备不属于任何网络了（上下文解析不出来）：与开会话同向——拒绝。"""
        session, _ = self.open_view()
        self.devices.pop(NETWORK)

        reply = self.send(session, SnapshotRequest("q1", 0))

        self.assertEqual(reply.message.code, Reason.WRONG_NETWORK)
        self.assertIsNone(self.hub.registry.get(session.session_id))

    def test_closing_never_needs_revalidation(self):
        """客户端说要走就让它走：复核不过也只会走到同一个 `close_session`，
        中间平白多回一条错误，而界面那边会把它当异常显示。

        顺带钉住「关会话这条路不碰复核计数」——否则
        `revalidate_refused` 就不再等于「有多少会话因为不成立而被关掉」。
        """
        session, _ = self.open_view()
        self.walk_away()

        self.close_view(session)

        self.assertIsNone(self.hub.registry.get(session.session_id))
        self.assertEqual(session.close_reason, "client_closed")
        self.assertEqual(self.hub.stats["revalidate_refused"], 0)

    def test_only_the_walking_player_is_affected(self):
        mine, _ = self.open_view(player_id="p1")
        theirs, _ = self.open_view(player_id="p2", request_id="r2")

        self.walk_away("p1")
        self.send(mine, SnapshotRequest("q1", 0))

        self.assertIsNone(self.hub.registry.get(mine.session_id))
        self.assertIsNotNone(self.hub.registry.get(theirs.session_id))

    def test_the_refusal_carries_the_distance_limit(self):
        """拒绝的说明里要有**生效的**上限：实机上「为什么开不了」全靠它。

        对着 policy 字段断言，因此改策略时这条跟着变。
        """
        session, _ = self.open_view()
        self.walk_away()

        reply = self.send(session, SnapshotRequest("q1", 0))

        limit = self.hub.registry.policy.max_interaction_distance
        self.assertIn("%g" % (limit,), reply.message.detail)


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
# 跟随运行时变化注销
# ---------------------------------------------------------------------------


def RUNTIME(kind):
    # type: (str) -> str
    """按名字取 `RuntimeChange` 的种类常量（`RUNTIME("retired")`）。

    **不在这边抄一份字面量**：抄一份的话，「退役」写成 `"retire"` 就只是
    「那条变化不是退役」，用例照样绿；`getattr` 会当场抛 `AttributeError`。
    """
    from endernet.server.network.runtime import RuntimeChange

    return getattr(RuntimeChange, kind.upper())


class TestRuntimeChanges(Fixture):
    """`RuntimeChange` → 注销会话（bootstrap 把它挂在 `RuntimeManager` 上）。

    这里测的是**接线那一侧的一半**：给定一条变化，哪些会话该关。另一半
    （变化真的会从 `RuntimeManager` 流到这里）由 `test_bootstrap.py` 的
    `TestSyncWiring` 经真实装配验证——两半分开是因为这一半要能单独驱动
    任何一种变化，而真装配只走得出它自己会发的那几种。
    """

    def change(self, kind, network_id=NETWORK):
        # type: (str, str) -> object
        """造一条变化。**编号用线上形态**：运行时的编号是整数，而
        `network_id_of` 是唯一的转换点（另有专门用例钉它）。"""
        from endernet.server.network.runtime import RuntimeChange

        return RuntimeChange(network_id, kind)

    def test_a_retired_network_closes_its_sessions(self):
        session, _ = self.open_view()
        self.assertEqual(self.hub.on_runtime_change(self.change(RUNTIME("retired"))), 1)
        self.assertIsNone(self.hub.registry.get(session.session_id))
        self.assertEqual(self.hub.diagnostics()["networks"], {})

    def test_a_closed_network_closes_its_sessions(self):
        """网络被关掉（重建中、无主控）时界面上的库存不该继续看下去。"""
        session, _ = self.open_view()
        self.assertEqual(self.hub.on_runtime_change(self.change(RUNTIME("closed"))), 1)
        self.assertIsNone(self.hub.registry.get(session.session_id))

    def test_a_lost_master_closes_its_sessions(self):
        session, _ = self.open_view()
        self.assertEqual(self.hub.on_runtime_change(self.change(RUNTIME("master_lost"))), 1)
        self.assertIsNone(self.hub.registry.get(session.session_id))

    def test_other_changes_are_no_ops(self):
        """建立、绑主控、开放都不使任何既有会话失效。

        把它们一并关掉会表现成「网络一重建，终端就自己关了」——而那些
        变化在一次拆装里会出现好几回。
        """
        session, _ = self.open_view()
        for kind in ("created", "master_bound", "opened"):
            self.assertEqual(self.hub.on_runtime_change(self.change(RUNTIME(kind))), 0, kind)
        self.assertIsNotNone(self.hub.registry.get(session.session_id))

    def test_a_replaced_device_closes_only_its_own_sessions(self):
        """拆掉又在原地放一个 → 代次变大 → 旧会话作废（判据与开会话同一句）。"""
        session, _ = self.open_view()
        # 同一坐标、新代次：注册表里那台已经不是界面对着的那一台了。
        self.devices[NETWORK] = DeviceRef(session.device_ref.node_key, 2)

        self.assertEqual(
            self.hub.on_runtime_change(self.change(RUNTIME("members_changed"))), 1
        )
        self.assertIsNone(self.hub.registry.get(session.session_id))

    def test_an_unchanged_device_keeps_its_session(self):
        """成员增减里没动到的那台终端不受影响——否则每次插拔都踢人。"""
        session, _ = self.open_view()
        self.assertEqual(
            self.hub.on_runtime_change(self.change(RUNTIME("members_changed"))), 0
        )
        self.assertIsNotNone(self.hub.registry.get(session.session_id))

    def test_a_missing_context_counts_as_replaced(self):
        """查不到上下文（网络已不在、设备已消失）按失效处理：fail closed。"""
        session, _ = self.open_view()
        self.devices.pop(NETWORK)

        self.assertEqual(
            self.hub.on_runtime_change(self.change(RUNTIME("members_changed"))), 1
        )
        self.assertIsNone(self.hub.registry.get(session.session_id))

    def test_a_change_only_touches_its_own_network(self):
        self.add_network(SECOND_NETWORK, x=40)
        first_session, _ = self.open_view()
        second_session, _ = self.open_view(player_id="p2", network_id=SECOND_NETWORK)

        self.assertEqual(
            self.hub.on_runtime_change(
                self.change(RUNTIME("retired"), network_id=SECOND_NETWORK)
            ),
            1,
        )
        self.assertIsNotNone(self.hub.registry.get(first_session.session_id))
        self.assertIsNone(self.hub.registry.get(second_session.session_id))

    def test_a_change_for_an_unknown_network_is_harmless(self):
        self.open_view()
        self.assertEqual(
            self.hub.on_runtime_change(
                self.change(RUNTIME("retired"), network_id="nope")
            ),
            0,
        )

    def test_shutdown_closes_every_session_on_every_network(self):
        """停机走 `shutdown()` 而不是让调用方自己遍历（见 `close_all` 的说明）。"""
        self.add_network(SECOND_NETWORK, x=40)
        first, _ = self.open_view()
        second, _ = self.open_view(player_id="p2", network_id=SECOND_NETWORK)
        self.settle(first)
        self.settle(second)

        self.assertEqual(self.hub.shutdown(), 2)
        self.assertEqual(self.hub.registry.count(), 0)
        self.assertEqual(self.hub.diagnostics()["networks"], {})
        self.assertEqual(self.hub.stats["networks_dropped"], 2)

    def test_shutdown_does_not_reuse_session_ids(self):
        """序号不重置：停机前发出去的号，客户端手里可能还留着。"""
        session, _ = self.open_view()
        self.hub.shutdown()
        second, _ = self.open_view(player_id="p2")
        self.assertNotEqual(session.session_id, second.session_id)

    def test_shutdown_with_nothing_open_is_harmless(self):
        self.assertEqual(self.hub.shutdown(), 0)

    def test_network_id_of_is_the_only_conversion(self):
        """分量编号是整数，线上要的是文本；全项目只在这里转。"""
        self.assertEqual(sync_terminal.TerminalSyncHub.network_id_of(7), "7")
        self.assertEqual(sync_terminal.TerminalSyncHub.network_id_of("7"), "7")
        self.assertEqual(sync_terminal.TerminalSyncHub.network_id_of(0), "0")


# ---------------------------------------------------------------------------
# 空闲超时回收的通知
# ---------------------------------------------------------------------------


class TestExpiryNotice(Fixture):
    """会话因空闲超时被回收时，服务端要**主动**告诉客户端。

    2026-10-01 真机实测到的缺陷：`evict_expired` 只清服务端自己的账，一条
    包都不发；客户端于是继续以为会话还在——界面照常显示**过期库存**，直到
    下一次点击被拒才丢会话。本节钉的是「那一条通知真的发出去、发得对、且
    客户端那边真的认」。

    **TTL 本身没有被改动**：会话照样会被回收（它是客户端崩溃或断线时的
    兜底），变的只是它不再无声无息。
    """

    def shorten_ttl(self, frames=3):
        # type: (int) -> None
        """把 TTL 调短，让用例只推几帧就到期。

        改的是**策略值**，判据一个字没动——按 `SessionPolicy` 的说法，
        用例该对着字段断言，而不是硬编 600 这个数去推 601 帧。
        """
        self.hub.registry.policy.session_ttl_frames = frames

    def expire(self, frames=5):
        # type: (int) -> None
        for _ in range(frames):
            self.hub.tick()

    def test_expiry_sends_a_sessionless_error(self):
        """空会话号 + 发得出去（线上格式合法）+ 只发一条。"""
        self.shorten_ttl()
        session, _ = self.open_view()
        self.assertEqual(self.error_payloads(), [])
        self.expire()
        self.assertIsNone(
            self.hub.registry.get(session.session_id), "会话本来就没被回收"
        )
        self.assertEqual(len(self.error_payloads()), 1)
        payload = self.error_payloads()[0]
        # **空的是关键**：客户端正是靠「信封里没有会话号」认出「这不是某一次
        # 请求失败，是这个会话本身没了」。带上号就等于没发。
        self.assertEqual(payload["session_id"], "")
        self.assertEqual(payload["network_id"], "")
        self.assertEqual(payload["epoch"], "")
        self.assertEqual(payload["message"]["code"], Reason.SESSION_INVALID)
        self.assertEqual(self.hub.stats["expired_notices"], 1)
        self.assertEqual(self.hub.stats["expired_notices_skipped"], 0)

    def test_the_client_recognises_it_as_session_death(self):
        """服务端「发了」与客户端「认了」是两件事，这里把两头接起来。

        只断言服务端发出了一个包，测不到客户端认不认——而认不出来的症状
        （界面照常显示过期库存）与根本不发**一模一样**。所以把真发出去的
        那份载荷喂给一个真客户端。
        """
        from endernet.client.network_client import NetworkClient

        self.shorten_ttl()
        _, reply = self.open_view()
        client = NetworkClient()
        client.receive(reply.to_dict())
        self.assertTrue(client.has_session())

        self.expire()
        payloads = self.error_payloads()
        self.assertEqual(len(payloads), 1)
        client.receive(json.loads(json.dumps(payloads[0])))
        self.assertFalse(client.has_session(), "客户端没认出来，界面会继续显示过期库存")
        self.assertEqual(client.stats["sessions_dropped"], 1)

    def test_no_notice_while_this_player_still_has_a_live_session(self):
        """玩家还有别的活会话时**不发**——那多半正是客户端手里那个。

        客户端一次只持有一个会话，而它手里那个一定是**最后开的**那个；
        旧的之所以还在，是因为关界面时那条 `session_close` 没送到。替旧
        会话发一条「你的会话没了」，客户端会把它手里那个新会话一起丢掉。
        """
        self.shorten_ttl()
        old, _ = self.open_view(request_id="r1")
        self.expire(2)
        fresh, _ = self.open_view(request_id="r2")
        self.assertNotEqual(old.session_id, fresh.session_id)

        self.expire(2)   # 第 4 帧：old（第 0 帧）到期，fresh（第 2 帧）没有
        self.assertIsNone(self.hub.registry.get(old.session_id))
        self.assertIsNotNone(self.hub.registry.get(fresh.session_id), "连新的也回收了")
        self.assertEqual(self.error_payloads(), [], "把客户端手里那个会话也顶掉了")
        self.assertEqual(self.hub.stats["expired_notices"], 0)
        self.assertEqual(self.hub.stats["expired_notices_skipped"], 1)

    def test_one_notice_per_player_even_when_several_expire_together(self):
        """同一玩家的几条会话同帧到期，只发一条。"""
        self.shorten_ttl()
        self.open_view(request_id="r1")
        self.open_view(request_id="r2")
        self.expire()
        self.assertEqual(len(self.error_payloads()), 1)
        self.assertEqual(self.hub.stats["expired_notices"], 1)

    def test_every_player_gets_their_own_notice(self):
        """别的玩家的会话到期，不能连累也不该漏掉。"""
        self.shorten_ttl()
        self.open_view(player_id="p1", request_id="r1")
        self.open_view(player_id="p2", request_id="r2")
        self.expire()
        self.assertEqual(len(self.error_payloads("p1")), 1)
        self.assertEqual(len(self.error_payloads("p2")), 1)
        self.assertEqual(self.hub.stats["expired_notices"], 2)

    def test_an_unreachable_port_does_not_break_the_tick(self):
        """端口不通只记一笔，不能把整帧打挂（这一条每帧都会走到）。

        **不能走 `_note_failure` 那一套**：会话已经注销，没有下一次要退避的
        重试，而那个分支会去动一个已经不在表里的会话。
        """
        session, _ = self.open_view()
        # 先把快照发完：不然这几帧里的失败是**传输**的，与这一条通知混在
        # 同一个计数里，测的就不是它了。
        self.settle(session)
        self.port.sent = []
        self.shorten_ttl(1)
        self.port.reachable = False
        before = self.hub.stats["send_failures"]
        self.expire(3)
        self.assertEqual(self.hub.stats["expired_notices"], 0)
        self.assertEqual(self.hub.stats["send_failures"], before + 1)

    def test_a_disabled_ttl_neither_evicts_nor_notifies(self):
        """`session_ttl_frames <= 0` 是「不按时间回收」，那时没有人可通知。"""
        self.hub.registry.policy.session_ttl_frames = 0
        session, _ = self.open_view()
        self.expire(50)
        self.assertIsNotNone(self.hub.registry.get(session.session_id))
        self.assertEqual(self.error_payloads(), [])
        self.assertEqual(self.hub.stats["expired_notices"], 0)


# ---------------------------------------------------------------------------
# 其余三条服务端单方面注销的通知（D-08 的后两条）
# ---------------------------------------------------------------------------


class TestGoneNotice(Fixture):
    """设备被替换、网络退役/关闭/丢主控——同样是服务端单方面注销。

    **与超时那条是同一个缺陷的另外两面**：`_forget` 一条包都不发，客户端
    就继续拿着一个死会话显示库存，直到下一次点击被拒（欠账台账 **D-08**）。
    这里钉的是「那一条通知在另外三条路径上也真的发出去、发的是各自那句话、
    且客户端那边真的认」。

    与 `TestExpiryNotice` 共用 `_notify_gone` 的核心（一个玩家一条、还有
    活会话时不发），因此这里只补**路径本身**的差异：什么时候被调用、
    发的是哪句话、以及**哪些路径不该发**。
    """

    def change(self, kind, network_id=NETWORK):
        # type: (str, str) -> object
        from endernet.server.network.runtime import RuntimeChange

        return RuntimeChange(network_id, kind)

    def test_a_retired_network_tells_the_client(self):
        """整网退役：空会话号 + 发得出去 + 只发一条。"""
        session, _ = self.open_view()
        self.settle(session)
        self.port.sent = []

        self.assertEqual(self.hub.on_runtime_change(self.change(RUNTIME("retired"))), 1)
        self.assertIsNone(self.hub.registry.get(session.session_id))
        payloads = self.error_payloads()
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        # 与超时那条同一套判据：客户端靠**空**会话号认出「这个会话没了」。
        self.assertEqual(payload["session_id"], "")
        self.assertEqual(payload["network_id"], "")
        self.assertEqual(payload["epoch"], "")
        self.assertEqual(payload["message"]["code"], Reason.SESSION_INVALID)
        self.assertEqual(self.hub.stats["gone_notices"], 1)
        # 超时那一桶不该被动到——两个数分开，才答得了「客户端为什么被通知」。
        self.assertEqual(self.hub.stats["expired_notices"], 0)

    def test_the_client_recognises_a_network_close_as_session_death(self):
        """服务端「发了」与客户端「认了」是两件事，这里把两头接起来。"""
        from endernet.client.network_client import NetworkClient

        _, reply = self.open_view()
        client = NetworkClient()
        client.receive(reply.to_dict())
        self.assertTrue(client.has_session())

        self.hub.on_runtime_change(self.change(RUNTIME("master_lost")))
        payloads = self.error_payloads()
        self.assertEqual(len(payloads), 1)
        client.receive(json.loads(json.dumps(payloads[0])))
        self.assertFalse(client.has_session(), "客户端没认出来，界面会继续显示过期库存")
        self.assertEqual(client.stats["sessions_dropped"], 1)

    def test_a_replaced_device_tells_the_client(self):
        """拆掉又在原地放一个：界面指着的方块已经没了。"""
        session, _ = self.open_view()
        self.settle(session)
        self.port.sent = []
        self.devices[NETWORK] = DeviceRef(session.device_ref.node_key, 2)

        self.assertEqual(
            self.hub.on_runtime_change(self.change(RUNTIME("members_changed"))), 1
        )
        self.assertIsNone(self.hub.registry.get(session.session_id))
        self.assertEqual(len(self.error_payloads()), 1)
        self.assertEqual(self.hub.stats["gone_notices"], 1)

    def test_an_unchanged_device_says_nothing(self):
        """成员增减里没动到的那台不受影响——没关会话就不该有通知。"""
        session, _ = self.open_view()
        self.settle(session)
        self.port.sent = []

        self.assertEqual(
            self.hub.on_runtime_change(self.change(RUNTIME("members_changed"))), 0
        )
        self.assertEqual(self.error_payloads(), [])
        self.assertEqual(self.hub.stats["gone_notices"], 0)

    def test_each_reason_carries_its_own_text(self):
        """四种注销各有各的说法——玩家看到的那一句话就是这里定的。

        「主控被拆」与「网络在重建」要查的东西完全不一样（一个查方块还在
        不在，一个查重建为什么没结束），所以那句话不能合成一句「网络故障」。
        """
        texts = {}
        for kind, reason in (
            ("retired", sync_terminal.CLOSE_NETWORK_RETIRED),
            ("closed", sync_terminal.CLOSE_NETWORK_CLOSED),
            ("master_lost", sync_terminal.CLOSE_MASTER_LOST),
        ):
            self.setUp()
            self.open_view()
            self.hub.on_runtime_change(self.change(RUNTIME(kind)))
            payloads = self.error_payloads()
            self.assertEqual(len(payloads), 1, kind)
            texts[kind] = payloads[0]["message"]["detail"]
            self.assertEqual(
                texts[kind], sync_terminal._GONE_DETAILS[reason], kind
            )
        self.assertEqual(len(set(texts.values())), 3, "四种注销至少有三种不同说法")

    def test_the_replacement_text_is_its_own(self):
        """设备被替换那一句与整网注销那三种都不同——它查的是另一件事。"""
        session, _ = self.open_view()
        self.devices[NETWORK] = DeviceRef(session.device_ref.node_key, 2)
        self.hub.on_runtime_change(self.change(RUNTIME("members_changed")))
        payloads = self.error_payloads()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(
            payloads[0]["message"]["detail"],
            sync_terminal._GONE_DETAILS[sync_terminal.CLOSE_DEVICE_REPLACED],
        )

    def test_no_notice_while_this_player_still_has_a_live_session(self):
        """玩家还有别的活会话时不发——那多半正是客户端手里那个。

        与超时那条**同一条守卫、同一条理由**：替旧会话发一条「你的会话
        没了」，客户端会把它手里那个新会话一并丢掉，比不发更糟。
        """
        self.add_network(SECOND_NETWORK, x=40)
        first, _ = self.open_view(request_id="r1")
        other, _ = self.open_view(network_id=SECOND_NETWORK, request_id="r2")
        self.settle(other)
        self.port.sent = []

        self.assertEqual(self.hub.on_runtime_change(self.change(RUNTIME("retired"))), 1)
        self.assertIsNone(self.hub.registry.get(first.session_id))
        self.assertIsNotNone(self.hub.registry.get(other.session_id), "把另一个网络的也关了")
        self.assertEqual(self.error_payloads(), [], "把客户端手里那个会话也顶掉了")
        self.assertEqual(self.hub.stats["gone_notices"], 0)
        self.assertEqual(self.hub.stats["gone_notices_skipped"], 1)

    def test_one_notice_per_player_across_the_whole_network(self):
        """同一玩家在同一网络上开了两个会话，整网注销时只发一条。"""
        self.open_view(request_id="r1")
        self.open_view(request_id="r2")
        self.assertEqual(self.hub.on_runtime_change(self.change(RUNTIME("retired"))), 2)
        self.assertEqual(len(self.error_payloads()), 1)
        self.assertEqual(self.hub.stats["gone_notices"], 1)

    def test_every_player_gets_their_own_notice(self):
        """别的玩家被牵连注销，不能漏掉他。"""
        self.open_view(player_id="p1", request_id="r1")
        self.open_view(player_id="p2", request_id="r2")
        self.hub.on_runtime_change(self.change(RUNTIME("retired")))
        self.assertEqual(len(self.error_payloads("p1")), 1)
        self.assertEqual(len(self.error_payloads("p2")), 1)
        self.assertEqual(self.hub.stats["gone_notices"], 2)

    def test_a_player_leaving_says_nothing(self):
        """玩家离开**不发**——收件人已经不在了。

        这条钉的是「通知挂在具体调用点上，不放进 `_forget_all`」：放进去的
        话 `close_for_player` 与 `shutdown` 会跟着发，而那两个时候发出去的
        包没有任何意义，只会污染「客户端为什么被通知」的计数。
        """
        session, _ = self.open_view()
        self.settle(session)
        self.port.sent = []

        self.assertEqual(self.hub.close_for_player("p1", "player_left"), [session])
        self.assertEqual(self.error_payloads(), [])
        self.assertEqual(self.hub.stats["gone_notices"], 0)

    def test_shutdown_says_nothing(self):
        """停机也不发——整台服务正要停。"""
        session, _ = self.open_view()
        self.settle(session)
        self.port.sent = []

        self.assertEqual(self.hub.shutdown(), 1)
        self.assertEqual(self.error_payloads(), [])
        self.assertEqual(self.hub.stats["gone_notices"], 0)
        self.assertEqual(self.hub.stats["expired_notices"], 0)

    def test_an_unreachable_port_does_not_break_the_change(self):
        """端口不通只记一笔，注销本身照常完成（调用方在看返回的条数）。"""
        session, _ = self.open_view()
        self.settle(session)
        self.port.sent = []
        self.port.reachable = False
        before = self.hub.stats["send_failures"]

        self.assertEqual(self.hub.on_runtime_change(self.change(RUNTIME("retired"))), 1)
        self.assertIsNone(self.hub.registry.get(session.session_id))
        self.assertEqual(self.hub.stats["gone_notices"], 0)
        self.assertEqual(self.hub.stats["send_failures"], before + 1)
        self.assertEqual(self.port.sent, [])


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
        """`error` 是「S2C 必须带会话」唯一的例外，否则只能硬编一个假会话。

        **2026-09-30 起会话字段一律留空**：会话不存在时服务端没有可填的
        session_id，而空与不空正是客户端据以区分「这个会话已经没了」与
        「会话还活着、只是这条请求失败了」的**唯一**判据（见服务端
        `_error` 的说明）。原先是把客户端自称的那个 id 回显回去——它听起来
        更「有礼貌」，但那样两类错误在线上完全同形，客户端只能去列一份
        「哪些原因码算会话级」的表，而那份表会漂移。
        """
        reply = self.hub.handle(
            SyncEnvelope(SnapshotRequest("q1", 0), session_id="s404"), "p1"
        )
        self.assertEqual(reply.session_id, "")
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


# ---------------------------------------------------------------------------
# 手动存取命令（11-03）
# ---------------------------------------------------------------------------


class TestTerminalCommands(Fixture):
    """`command` → `submit` → `command_result` 这条回路。

    这里测的是**中枢**那一半：命令是怎么拼出来的、结果怎么回去、出错时
    回不回。命令**执行**的那一半（`StorageService` 的五个背包动作）在
    `test_terminal_inventory.py`，两边不重复。
    """

    def command(self, session, action="inv_take", request_id="c1", **kwargs):
        # type: (object, str, str, dict) -> tuple
        """发一条命令，返回 `(handle 的返回值, 端口上收到的 command_result 列表)`。

        **两个出口要分开取。** 业务结果走 `_send`（进端口），而会话级的
        拒收走 `handle` 的返回值（`_error` 构造回包交给调用方）。只看端口
        会漏掉后者，只看返回值会漏掉前者。
        """
        reply = self.send(session, TerminalCommand(request_id, action, **kwargs))
        return reply, self.of_kind(MSG_COMMAND_RESULT, session.player_id)

    def test_a_command_reaches_the_executor_and_the_result_comes_back(self):
        session, _ = self.open_view()
        _, replies = self.command(session, count=32, slot=3)
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].message.result["status"], "ok")
        self.assertEqual(replies[0].session_id, session.session_id)
        self.assertEqual(replies[0].message.request_id, "c1")

    def test_the_source_is_built_from_the_session_not_the_payload(self):
        """来源三件套全部取**会话**。

        载荷里根本没有这些字段（`TerminalCommand` 的文档列了理由），所以
        「客户端自报身份」在这一层不是「被忽略」而是**不可能表达**——
        这正是这条用例要钉住的：命令上的 `source` 与发消息的那个玩家
        一致，而不是与消息内容一致。
        """
        session, _ = self.open_view(player_id="p1")
        self.command(session, count=1)
        command = self.submissions[0]
        self.assertEqual(command.source.player_id, "p1")
        self.assertEqual(command.source.device_ref, session.device_ref)
        self.assertEqual(command.source.dimension, session.dimension)
        self.assertEqual(command.source.session_id, session.session_id)
        self.assertTrue(command.source.distance_ok)
        self.assertEqual(command.kind, CommandKind.STORAGE)
        self.assertEqual(command.request_id, "c1")

    def test_topology_version_is_left_unset_on_purpose(self):
        """**不拿交易版本冒充图版本。**

        中枢手里没有权威的图版本源；把 `expected_index_version` 填进
        `topology_version` 会让主控按错误的依据放行或拒绝，而且不会报错。
        交易版本自己也**不进动作载荷**：它是消息层的字段（`payload()` 的
        文档写着），而服务端那五个背包动作没有一个读它——目前没有任何
        消费者校验它（理由见 `TerminalCommand` 的类文档）。
        """
        session, _ = self.open_view()
        message = TerminalCommand(
            "c1", "inv_merge", 999, {"k": "v"}, 4, 3, 9, into_slot=5
        )
        self.send(session, message)
        command = self.submissions[0]
        self.assertIsNone(command.topology_version)
        self.assertNotIn("expected_index_version", command.payload)
        # 字段名映射**只有一处**（`TerminalCommand.payload()`），中枢原样转交：
        # 两边各拼一次就是两条会各自漂移的路径。
        self.assertEqual(command.payload, message.payload())

    def test_a_business_refusal_is_an_answer_not_an_error(self):
        """`blocked` / `rejected` 走 `command_result`，不走 `error`。

        它们是**结论**（界面据此提示玩家），而 `error` 留给会话与信道级
        的拒收。混在一起会让「没空间了」看起来像「协议坏了」。
        """
        session, _ = self.open_view()
        self.submit_result = CommandResult.rejected(
            "c1", [BlockReason(Reason.BAD_PARAMETER, "参数不合法")]
        )
        reply, replies = self.command(session, count=1)
        self.assertIsNone(reply, "业务失败不是协议错误，不该另回一条 error")
        self.assertEqual(replies[0].message.result["status"], "rejected")
        self.assertEqual(
            replies[0].message.result["reasons"][0]["code"], Reason.BAD_PARAMETER
        )
        self.assertEqual(self.hub.stats["command_rejected"], 1)

    def test_an_executor_that_raises_still_gets_an_answer(self):
        """执行方抛异常时**也回一条**。

        客户端没有时钟也没有超时（`NetworkClient._in_flight` 只进不出），
        不回则那条在途记录永不释放，表现为「按了一次之后这个界面再也发不
        出命令」——而日志里什么都没有。
        """
        session, _ = self.open_view()
        self.submit_error = RuntimeError("boom")
        reply, replies = self.command(session, count=1)
        self.assertEqual(replies, [], "异常不该被当成一条正常结果发出去")
        self.assertEqual(reply.message.kind, MSG_ERROR)
        self.assertEqual(reply.message.code, Reason.INTERNAL)
        self.assertEqual(reply.message.request_id, "c1")
        self.assertEqual(self.hub.stats["command_errors"], 1)

    def test_an_executor_returning_the_wrong_type_is_never_a_success(self):
        """注入接错时**绝不假装成功**：静默当成 ok 会让界面显示一次
        没发生的存取，而库存没动——账实不符里最难查的一种。"""
        session, _ = self.open_view()
        self.submit_result = {"status": "ok"}
        reply, replies = self.command(session, count=1)
        self.assertEqual(replies, [])
        self.assertEqual(reply.message.code, Reason.INTERNAL)
        self.assertEqual(self.hub.stats["command_errors"], 1)

    def test_a_command_is_revalidated_like_every_other_message(self):
        """走开之后发命令：会话被关，命令**没有**到达执行方。

        命令是能改库存的那一类，因此这条复核比增量更该过——放行等于让一个
        已经站在远处的客户端继续动这台设备的网络。
        """
        session, _ = self.open_view()
        self.positions["p1"] = (999.0, 64.0, 0.0)
        reply, _ = self.command(session, count=1)
        self.assertEqual(self.submissions, [])
        self.assertEqual(reply.message.code, Reason.OUT_OF_RANGE)
        self.assertIsNone(self.hub.registry.get(session.session_id))

    def test_a_command_on_someone_elses_session_is_refused(self):
        """别人的会话：**不告诉他这条会话存在**，与「不存在」同一个回答。"""
        session, _ = self.open_view(player_id="p1")
        reply = self.send_as(
            session, TerminalCommand("c1", "inv_take", count=1), "p2"
        )
        self.assertEqual(self.submissions, [])
        self.assertEqual(reply.message.code, Reason.SESSION_INVALID)

    def test_without_an_executor_the_command_is_blocked_not_dropped(self):
        """没接执行方 = 装配遗漏，回 `blocked` 而不是静默丢弃。

        与「执行方坏了」分开：前者该让界面说「暂时不可用」，后者该在日志里
        留堆栈。
        """
        hub = TerminalSyncHub(
            policy=self.policy,
            storage_of=self._storage_of,
            context_of=self._context_of,
            port=self.port,
        )
        # 会话开着**这个**中枢上：会话号是逐注册表发的，拿另一个中枢的号
        # 会先撞上 `SESSION_INVALID`，那样测到的是会话校验而不是缺执行方。
        opened = hub.handle(
            SyncEnvelope(SessionOpen("r-open", self.device, 0), view_kind=VIEW_ITEM),
            "p1",
        )
        self.port.sent = []
        hub.handle(
            SyncEnvelope(
                TerminalCommand("c1", "inv_take", count=1),
                session_id=opened.session_id,
                network_id=opened.network_id,
                epoch=opened.epoch,
                view_kind=VIEW_ITEM,
            ),
            "p1",
        )
        results = [
            SyncEnvelope.from_dict(json.loads(json.dumps(payload)))
            for _, payload in self.port.sent
            if payload["message"]["kind"] == MSG_COMMAND_RESULT
        ]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].message.result["status"], "blocked")
        self.assertEqual(
            results[0].message.result["reasons"][0]["code"], Reason.SERVICE_NOT_READY
        )
        self.assertEqual(
            results[0].message.result["request_id"], "c1",
            "客户端靠它把结果挂回那次请求；丢了它界面就只剩「出错了」",
        )

    def test_command_counters_separate_refusals_from_executions(self):
        """`commands` 与 `command_rejected` 分开计。

        「按了没反应」到底是没收到还是被拒了，只有分开计才答得出来。
        """
        session, _ = self.open_view()
        self.command(session, count=1, request_id="c1")
        self.submit_result = CommandResult.stale(
            "c2", [BlockReason(Reason.STALE_VERSION, "槽已变")]
        )
        self.command(session, count=1, request_id="c2")
        self.assertEqual(self.hub.stats["commands"], 2)
        self.assertEqual(self.hub.stats["command_rejected"], 1)
        self.assertEqual(self.hub.stats["command_errors"], 0)


# -- 11-05 样板终端的状态推送 ----------------------------------------------

def template_rows(count, locked=False):
    return [{"slot": index, "canonical": "", "count": 0, "name": "", "aux": 0,
             "locked": locked}
            for index in range(count)]


def template_state(revision=1, mode="crafting"):
    """与 `bootstrap._sync_template_of` 同形的一份状态。"""
    return {
        "revision": revision,
        "mode": mode,
        "source": template_rows(9),
        "outputs": template_rows(3, locked=mode == "crafting"),
        "input": template_rows(1),
        "result": template_rows(1),
        "limits": {"max_count": 65535, "default_count": 1},
    }


class TestTemplateStatePublish(Fixture):
    """11-05 的样板状态推送。判据与 11-04 的工作台那条逐条对应：
    按整个载荷去重、没接上就不发、读炸了也宁可不发。"""

    def setUp(self):
        Fixture.setUp(self)
        self.template = None
        self.template_calls = 0
        self.template_raises = False
        #: `Fixture` 建中枢时没有传 `template_of`，而这里要测的正是接上之后
        #: 的行为，因此换掉那个注入点（与 `_inventory_sent` 同一种用法）。
        self.hub._template_of = self._template_of

    def _template_of(self, device_ref):
        self.template_calls += 1
        if self.template_raises:
            raise RuntimeError("engine query exploded")
        return self.template

    def publish(self, session):
        self.hub.tick()
        return self.of_kind(MSG_TEMPLATE_STATE)

    def test_a_device_without_template_slots_is_not_pushed_to(self):
        session, _ = self.open_view()
        self.settle(session)
        self.port.sent = []
        self.template = None
        self.assertEqual([], self.publish(session))

    def test_the_state_is_pushed_once_and_only_when_it_changes(self):
        """`settle` 自己会 tick，因此第一帧就已经推过一条了。"""
        self.template = template_state()
        session, _ = self.open_view()
        self.settle(session)
        self.assertIn(session.session_id, self.hub._template_sent, "第一帧发过了")
        self.port.sent = []
        self.assertEqual([], self.publish(session), "内容没变就不发")
        self.template = template_state(revision=2)
        self.assertEqual(1, len(self.publish(session)), "改了就要再发")

    def test_a_switch_to_factory_mode_unlocks_the_output_rows(self):
        self.template = template_state(mode="factory")
        session, _ = self.open_view()
        self.settle(session)
        message = self.of_kind(MSG_TEMPLATE_STATE)[-1].message
        self.assertEqual("factory", message.mode)
        self.assertEqual([False] * 3, [row["locked"] for row in message.outputs])
        self.assertEqual([False] * 9, [row["locked"] for row in message.source])

    def test_a_failing_read_never_fakes_an_empty_state(self):
        """**不发**与**发一份空的**是两回事：后者在客户端读出来是
        「你配的东西没了」。"""
        self.template_raises = True
        session, _ = self.open_view()
        self.settle(session)
        self.assertEqual([], self.of_kind(MSG_TEMPLATE_STATE))
        self.assertEqual(0, self.hub.stats["template_states"])

    def test_the_pushed_payload_survives_the_wire(self):
        self.template = template_state(revision=7)
        session, _ = self.open_view()
        self.settle(session)
        message = self.of_kind(MSG_TEMPLATE_STATE)[-1].message
        self.assertEqual(7, message.revision)
        self.assertEqual(9, len(message.source))
        self.assertEqual(65535, message.limits["max_count"])
        self.assertEqual(1, self.hub.stats["template_states"])

    def test_closing_the_session_drops_the_dedup_record(self):
        self.template = template_state()
        session, _ = self.open_view()
        self.settle(session)
        self.assertIn(session.session_id, self.hub._template_sent)
        self.close_view(session)
        self.assertNotIn(session.session_id, self.hub._template_sent)


if __name__ == "__main__":
    unittest.main()
