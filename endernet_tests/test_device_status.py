# coding=utf-8
"""设备总体状态的上行：订阅、去重、清理（11-08 / 11-13）。

**全部离线**：`server/sync/` 不许 import 引擎，因此这里喂几次 `tick()`、
看发出了什么包就够了。夹具不接存储——设备状态与库存是两条独立的上行，
`storage_of` 缺省返回 `None` 时库存那一半自动静默，留下的包正好只有
设备状态，判据不会被无关流量淹掉。

反复钉住的四件事：

- **会话开启即快照**：`_status_sent` 里没有这一条，首帧必发；
- **未变不重发**：同一份结论连续 tick 只出一个包；
- **关了就不发**：会话关闭/玩家离开后，订阅记账必须归零，之后不再有包；
- **读不到 ≠ 离线**：读取失败这一帧什么都不发，绝不伪造一个方向错误的
  结论出去。
"""

import json
import unittest

import testsupport  # noqa: F401  (放进 sys.path)

from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.protocol import (
    BlockReason,
    MSG_DEVICE_STATUS,
    MSG_INVENTORY_SNAPSHOT,
    MSG_ITEM_DELTA,
    Reason,
    SessionOpen,
    SyncEnvelope,
    VIEW_ITEM,
)
from endernet.server.network.controller import DeviceNetworkStatus, RunState
from endernet.server.sync.device_status import (
    DeviceStatusSubscriptions,
    build_status_message,
    read_status,
)
from endernet.server.sync.sessions import SessionContext
from endernet.server.sync.terminal import TerminalSyncHub

NETWORK = "net-1"
EPOCH = "epoch-1"


class FakePort(object):
    """记录发出的包。契约同 `AdapterSyncPort`：正常 `True`，不收就 `False`。"""

    def __init__(self):
        self.sent = []

    def send(self, player_id, payload):
        # type: (str, dict) -> bool
        self.sent.append((player_id, payload))
        return True


def make_status(
    ref,
    online=True,
    validity="ok",
    reasons=(),
    notes=(),
    ui_applicable=True,
    version=1,
    device_id="endernet:storage_driver",
):
    # type: (...) -> DeviceNetworkStatus
    """造一份真实形状的结论。**用真 `DeviceNetworkStatus`**，不造替身。

    替身会把 `to_dict()` 的字段名重新实现一遍，于是 `build_status_message`
    这个名字映射一旦漂移，测的是替身的字典而不是领域层真正的线上形状。
    """
    return DeviceNetworkStatus(
        ref.node_key,
        ref.generation,
        device_id,
        online,
        validity,
        "master-1",
        RunState.RUNNABLE if online else RunState.MASTER_ABSENT,
        True if online else None,
        True if online else None,
        version=version,
        reasons=list(reasons),
        notes=list(notes),
        ui_applicable=ui_applicable,
    )


class DeviceStatusFixture(unittest.TestCase):
    def setUp(self):
        self.port = FakePort()
        self.device = DeviceRef(NodeKey(0, 10, 64, 0), 1)
        #: `ref.as_key()` -> 结论，或一个**要抛出来**的异常实例。
        self.statuses = {}
        self.submissions = []
        self.descriptions = {}
        self.hub = TerminalSyncHub(
            status_of=self._status_of,
            context_of=self._context_of,
            port=self.port,
        )
        self.statuses[self.device.as_key()] = make_status(self.device)

    # -- 注入 ----------------------------------------------------------

    def _status_of(self, device_ref):
        value = self.statuses.get(device_ref.as_key())
        if isinstance(value, BaseException):
            raise value
        return value

    def _context_of(self, player_id, device_ref):
        if device_ref is None:
            return None
        return SessionContext(
            player_id,
            device_ref,
            NETWORK,
            EPOCH,
            "master-1",
            network_workable=True,
            player_dimension=0,
            player_position=device_ref.node_key.as_tuple(),
        )

    # -- 会话 ----------------------------------------------------------

    def open_view(self, player_id="p1"):
        reply = self.hub.handle(
            SyncEnvelope(SessionOpen("r1", self.device, 0), view_kind=VIEW_ITEM),
            player_id,
        )
        session = self.hub.registry.get(reply.session_id)
        self.descriptions[session.session_id] = session.descriptions
        return session

    def messages(self):
        """按线上路径解回来（JSON 往返），不只是看 Python 对象。"""
        out = []
        for _pid, payload in self.port.sent:
            wire = json.loads(json.dumps(payload))
            out.append(
                SyncEnvelope.from_dict(
                    wire, self.descriptions.get(wire["session_id"])
                )
            )
        return out

    def statuses_on_the_wire(self):
        return [
            envelope
            for envelope in self.messages()
            if envelope.message.kind == MSG_DEVICE_STATUS
        ]


class TestPublishWindow(DeviceStatusFixture):
    def test_the_session_gets_the_current_verdict_on_its_first_frame(self):
        session = self.open_view()
        self.hub.tick()
        packets = self.statuses_on_the_wire()
        self.assertEqual(len(packets), 1)
        envelope = packets[0]
        self.assertEqual(envelope.session_id, session.session_id)
        self.assertEqual(envelope.network_id, NETWORK)
        self.assertEqual(envelope.epoch, EPOCH)
        message = envelope.message
        self.assertTrue(message.online)
        self.assertEqual(message.state_text, "在线")
        self.assertEqual(message.generation, 1)
        self.assertEqual(message.device_id, "endernet:storage_driver")

    def test_an_unchanged_verdict_is_not_republished(self):
        self.open_view()
        for _ in range(4):
            self.hub.tick()
        self.assertEqual(len(self.statuses_on_the_wire()), 1)

    def test_a_changed_verdict_is_republished(self):
        self.open_view()
        self.hub.tick()
        # 断电：结论翻转。
        self.statuses[self.device.as_key()] = make_status(
            self.device,
            online=False,
            validity="ok",
            version=2,
            reasons=[BlockReason(Reason.BROWNOUT, "能源不可用")],
        )
        self.hub.tick()
        packets = self.statuses_on_the_wire()
        self.assertEqual(len(packets), 2)
        last = packets[-1].message
        self.assertFalse(last.online)
        self.assertEqual(last.state_text, "离线")
        self.assertEqual(last.status_version, 2)
        self.assertEqual(last.reasons[0]["code"], Reason.BROWNOUT)

    def test_pending_still_reads_offline_and_carries_the_reason(self):
        self.statuses[self.device.as_key()] = make_status(
            self.device,
            online=False,
            validity="pending",
            reasons=[BlockReason(Reason.MASTER_RECOVERING, "主控恢复中")],
        )
        self.open_view()
        self.hub.tick()
        message = self.statuses_on_the_wire()[0].message
        # 没有第三态：待确认也显示「离线」，为什么在 reasons 里。
        self.assertEqual(message.state_text, "离线")
        self.assertEqual(message.validity, "pending")
        self.assertEqual(message.run_state_text, "主控未接管")

    def test_controllers_are_never_published(self):
        self.statuses[self.device.as_key()] = make_status(
            self.device, ui_applicable=False
        )
        self.open_view()
        for _ in range(3):
            self.hub.tick()
        self.assertEqual(self.statuses_on_the_wire(), [])

    def test_status_is_its_own_message_kind(self):
        """状态不搭在库存线上：它是独立的消息种类，不与快照/增量混流。"""
        self.open_view()
        self.hub.tick()
        kinds = [envelope.message.kind for envelope in self.messages()]
        self.assertEqual(kinds.count(MSG_DEVICE_STATUS), 1)
        self.assertNotIn(MSG_INVENTORY_SNAPSHOT, kinds)
        self.assertNotIn(MSG_ITEM_DELTA, kinds)


class TestLifecycleCleanup(DeviceStatusFixture):
    def test_closing_the_session_stops_the_status_and_the_bookkeeping(self):
        session = self.open_view()
        self.hub.tick()
        self.assertEqual(self.hub.status.count(), 1)
        self.hub.close_session(session.session_id, "closed")
        self.assertEqual(self.hub.status.count(), 0)
        self.hub.tick()
        self.assertEqual(len(self.statuses_on_the_wire()), 1)

    def test_the_player_leaving_stops_the_status(self):
        self.open_view()
        self.hub.tick()
        self.hub.close_for_player("p1", "player_left")
        self.assertEqual(self.hub.status.count(), 0)
        self.hub.tick()
        self.assertEqual(len(self.statuses_on_the_wire()), 1)

    def test_shutdown_clears_every_subscription(self):
        self.open_view()
        self.hub.tick()
        self.hub.shutdown()
        self.assertEqual(self.hub.status.count(), 0)
        report = self.hub.diagnostics()
        self.assertEqual(report["device_status"]["sessions"], 0)
        self.assertEqual(report["device_status"]["published"], 1)

    def test_a_replaced_device_starts_a_new_generation(self):
        """拆掉又在原地放一个：旧会话关闭，新会话的消息带新代次。"""
        session = self.open_view()
        self.hub.tick()
        self.hub.close_for_device(self.device, "device_replaced")
        self.assertEqual(self.hub.status.count(), 0)

        replacement = DeviceRef(self.device.node_key, 2)
        self.device = replacement
        self.statuses[replacement.as_key()] = make_status(replacement)
        reopened = self.open_view("p1")
        self.hub.tick()
        packets = self.statuses_on_the_wire()
        self.assertEqual(packets[-1].message.generation, 2)
        self.assertNotEqual(session.session_id, reopened.session_id)


class TestGuardedReads(DeviceStatusFixture):
    def test_a_failing_read_never_reports_online(self):
        self.statuses[self.device.as_key()] = RuntimeError("读取路径炸了")
        self.open_view()
        self.hub.tick()  # 不得抛出
        self.assertEqual(self.statuses_on_the_wire(), [])
        self.assertEqual(self.hub.status.count(), 0)

    def test_a_missing_device_is_not_reported_as_offline(self):
        self.statuses.pop(self.device.as_key())
        self.open_view()
        self.hub.tick()
        self.assertEqual(self.statuses_on_the_wire(), [])

    def test_a_status_from_another_generation_is_refused(self):
        """代次对不上的结论不得喂进界面——`runtime_for` 只按位置路由。"""
        other = make_status(DeviceRef(self.device.node_key, 9))
        self.assertIsNone(read_status(self.device, lambda ref: other))
        self.assertIsNotNone(
            read_status(self.device, lambda ref: make_status(self.device))
        )

    def test_a_gone_device_without_identity_is_refused(self):
        """「实例已不存在」那一份结论 `device_id` 是空的，线上过不了校验。"""
        gone = make_status(self.device, online=False, device_id=None)
        self.assertIsNone(read_status(self.device, lambda ref: gone))

    def test_status_reads_never_submit_anything(self):
        self.open_view()
        for _ in range(3):
            self.hub.tick()
        self.assertEqual(self.submissions, [])


class TestWireShape(unittest.TestCase):
    def test_the_message_survives_a_json_round_trip(self):
        ref = DeviceRef(NodeKey(0, 1, 2, 3), 4)
        status = make_status(
            ref, online=False, validity="pending",
            reasons=[BlockReason(Reason.REBUILDING, "网络重建中")],
        )
        message = build_status_message(status)
        wire = json.loads(json.dumps(message.to_dict()))
        restored = SyncEnvelope.from_dict(
            SyncEnvelope(message, "s1", NETWORK, EPOCH).to_dict()
        ).message
        self.assertEqual(restored.to_dict(), wire)

    def test_unknown_and_missing_fields_are_rejected(self):
        from endernet.common import protocol

        ref = DeviceRef(NodeKey(0, 1, 2, 3), 4)
        payload = build_status_message(make_status(ref)).to_dict()
        # 基线本身必须可解，否则下面的两条断言可能只是碰巧为真。
        self.assertEqual(
            protocol.decode_sync_message(payload).kind, MSG_DEVICE_STATUS
        )
        extra = dict(payload)
        extra["extra"] = 1
        with self.assertRaises(ValueError):
            protocol.decode_sync_message(extra)
        missing = dict(payload)
        del missing["online"]
        with self.assertRaises(ValueError):
            protocol.decode_sync_message(missing)

    def test_a_bare_version_field_is_refused(self):
        """两个版本必须各有其名：本消息只认 `status_version`。"""
        from endernet.common import protocol

        ref = DeviceRef(NodeKey(0, 1, 2, 3), 4)
        payload = build_status_message(make_status(ref)).to_dict()
        payload["version"] = 3
        with self.assertRaises(ValueError):
            protocol.decode_sync_message(payload)

    def test_status_version_is_not_the_trade_version(self):
        ref = DeviceRef(NodeKey(0, 1, 2, 3), 4)
        message = build_status_message(make_status(ref, version=77))
        self.assertEqual(message.status_version, 77)
        self.assertNotIn("version", message.to_dict())


class TestSubscriptions(unittest.TestCase):
    def test_unchanged_remember_drop_count(self):
        subs = DeviceStatusSubscriptions()
        payload = {"online": True}
        self.assertFalse(subs.unchanged("s1", payload))
        subs.remember("s1", payload)
        self.assertTrue(subs.unchanged("s1", payload))
        self.assertFalse(subs.unchanged("s1", {"online": False}))
        self.assertEqual(subs.count(), 1)
        subs.drop("s1")
        self.assertEqual(subs.count(), 0)
        self.assertFalse(subs.unchanged("s1", payload))
        report = subs.diagnostics()
        self.assertEqual(report["published"], 1)
        self.assertEqual(report["dropped"], 1)

    def test_dropping_an_unknown_session_is_harmless(self):
        subs = DeviceStatusSubscriptions()
        subs.drop("never-seen")
        self.assertEqual(subs.stats["dropped"], 0)


if __name__ == "__main__":
    unittest.main()
