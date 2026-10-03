# coding=utf-8
"""接口配置的上行：视图、订阅、去重、清理（11-07）。

**全部离线**：`server/sync/` 不许 import 引擎，因此这里喂几次 `tick()`、
看发出了什么包就够了。夹具不接存储——`storage_of` 缺省返回 `None` 时库存
那一半自动静默，留下的包正好只有配置，判据不会被无关流量淹掉。

形状与 `test_device_status.py` 逐条对齐，反复钉住同一批事：

- **会话开启即快照**：首帧必发；
- **未变不重发**：同一份配置连续 tick 只出一个包；
- **关了就不发**：会话关闭/玩家离开后记账归零，之后不再有包；
- **读不到 ≠ 空配置**：读不到这一帧什么都不发，绝不伪造一份「这台设备
  什么都没配」出去。

多出来的一条是**非接口设备一条都不发**——它对应 `read_config` 的第二道门，
而这条门正是「这条消息按设备发，但只有接口设备有配置」的实现方式。
"""

import json
import unittest

import testsupport  # noqa: F401  (放进 sys.path)

from endernet.common import protocol
from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.protocol import (
    MSG_DEVICE_STATUS,
    MSG_ITEM_DELTA,
    MSG_PORT_CONFIG,
    PORT_CONFIG_FIELDS,
    PORT_FILTER_MODES,
    SessionOpen,
    SyncEnvelope,
    VIEW_ITEM,
)
from endernet.server.devices.io import (
    FILTER_MODES,
    INTAKE,
    OUTPUT,
    PortConfig,
    PortPolicy,
    PortState,
    config_view,
)
from endernet.server.sync.device_config import (
    PortConfigSubscriptions,
    build_config_message,
    read_config,
)
from endernet.server.storage.transfer import TransferBuffer
from endernet.server.sync.sessions import SessionContext
from endernet.server.sync.terminal import TerminalSyncHub

NETWORK = "net-1"
EPOCH = "epoch-1"
PORT_ID = "endernet:bus_item_output_io"


class FakePort(object):
    """记录发出的包。契约同 `AdapterSyncPort`：正常 `True`，不收就 `False`。"""

    def __init__(self):
        self.sent = []

    def send(self, player_id, payload):
        # type: (str, dict) -> bool
        self.sent.append((player_id, payload))
        return True


def make_state(ref, device_id=PORT_ID, role=OUTPUT, config=None, buffer=None):
    # type: (DeviceRef, str, str, PortConfig, object) -> PortState
    """造一份**真** `PortState`，不造替身。

    替身会把 `to_dict()` 的字段名与 `config_view` 的挑拣各实现一遍，于是
    「视图里少了哪一项」这件事测的是替身而不是领域层。这条与
    `test_device_status.make_status` 的理由逐字相同。
    """
    return PortState(ref, device_id, role, config, buffer)


class PortConfigFixture(unittest.TestCase):
    def setUp(self):
        self.port = FakePort()
        self.device = DeviceRef(NodeKey(0, 10, 64, 0), 1)
        #: `ref.as_key()` -> `PortState`（或一个**要抛出来**的异常实例）。
        self.states = {}
        self.policy = PortPolicy()
        self.submissions = []
        self.descriptions = {}
        self.hub = TerminalSyncHub(
            port_of=self._port_of,
            context_of=self._context_of,
            port=self.port,
            submit=self._submit,
        )
        self.states[self.device.as_key()] = make_state(self.device)

    # -- 注入 ----------------------------------------------------------

    def _port_of(self, device_ref):
        value = self.states.get(device_ref.as_key())
        if isinstance(value, BaseException):
            raise value
        # 字典 = 「直接喂一份视图」，给「读到了却编不出去」那条用例用；
        # `None` = 非接口设备。
        if value is None or isinstance(value, dict):
            return value
        return config_view(value, self.policy)

    def _submit(self, command):
        """真记一笔：`test_*_never_submit_*` 那两条因此不是恒真的断言。"""
        self.submissions.append(command)
        return None

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

    def configs_on_the_wire(self):
        return [
            envelope
            for envelope in self.messages()
            if envelope.message.kind == MSG_PORT_CONFIG
        ]


class TestPortConfigView(unittest.TestCase):
    """`config_view` 是下发的唯一形状来源。"""

    def setUp(self):
        self.ref = DeviceRef(NodeKey(0, 1, 2, 3), 7)
        self.policy = PortPolicy()

    def test_the_view_carries_the_configuration_as_the_domain_serialises_it(self):
        config = PortConfig(
            enabled=True,
            filter=(),
            filter_mode="whitelist",
            limit=4,
            cache_enabled=False,
        )
        state = make_state(self.ref, config=config)
        view = config_view(state, self.policy)
        # **逐字来自 `PortConfig.to_dict()`**，不在这里另拼一份。
        self.assertEqual(view["config"], config.to_dict())
        self.assertEqual(view["device_id"], PORT_ID)
        self.assertEqual(view["generation"], 7)
        self.assertEqual(view["role"], OUTPUT)

    def test_the_limit_and_the_modes_come_from_the_policy_not_from_the_screen(self):
        """上限与允许的模式由策略回答——界面自己抄一个数就是把替身定案。"""
        output = config_view(make_state(self.ref), self.policy)
        self.assertEqual(
            output["filter_limit"], self.policy.filter_limit_for(OUTPUT)
        )
        self.assertEqual(output["filter_modes"], ["whitelist"])

        intake = config_view(
            make_state(self.ref, role=INTAKE, device_id="endernet:bus_item_input_io"),
            self.policy,
        )
        self.assertEqual(
            intake["filter_limit"], self.policy.filter_limit_for(INTAKE)
        )
        # 存货口两种模式都收——与取货口只收白名单是**两条不同的规则**。
        self.assertEqual(intake["filter_modes"], ["blacklist", "whitelist"])

    def test_the_view_names_no_version_at_all(self):
        """配置版本今天不存在，而另外两个版本词汇各有其名。

        顺手把这条钉住：往视图里加一个 `version` 就是在发明一套版本口径，
        而它必须由玩法文档先定（见 11-07 的「期望配置版本」缺项）。
        """
        view = config_view(make_state(self.ref), self.policy)
        for forbidden in ("version", "config_version", "schema_version"):
            self.assertNotIn(forbidden, view)

    def test_reading_a_view_does_not_touch_the_state(self):
        state = make_state(self.ref)
        before = state.to_dict()
        stats_before = dict(state.stats)
        config_view(state, self.policy)
        config_view(state, self.policy)
        self.assertEqual(state.to_dict(), before)
        self.assertEqual(state.stats, stats_before)

    def test_a_port_with_a_real_buffer_still_encodes(self):
        """**真机上撞到的那条**：`buffer_summary` 原先发的是 `describe()` 的字典。

        `TransferBuffer.describe()` 给的是 `{capacity, total, free, lots,
        stats, ...}` 一份**字典**，而 `PortConfigMessage` 的校验要求这一项
        是文本。两者对不上的后果不是「少一个字段」，而是
        `build_config_message` 抛 `TypeError` → 被 `_publish_device_config`
        吞掉并计进 `config_errors` → **配置推送一条都到不了客户端**，而
        界面上只是「什么都不显示」。

        **为什么离线夹具没照到**：本文件所有夹具的 `buffer` 都是 `None`，
        于是 `buffer_summary` 恒为 `None`，那条分支从来没被走过。这条用例
        为此专门造一个**有缓冲**的接口——真机与离线之间的差别就藏在这里。
        """
        buffer = TransferBuffer(576, "port@test")
        state = make_state(self.ref, buffer=buffer)
        view = config_view(state, self.policy)
        self.assertIsInstance(view["buffer_summary"], str)
        message = build_config_message(view)   # 编不出来会在这里抛
        self.assertEqual(message.buffer_summary, view["buffer_summary"])
        # 线上往返也得过（形状不对时在这一步才炸）。
        self.assertEqual(
            protocol.decode_sync_message(message.to_dict()).buffer_summary,
            view["buffer_summary"],
        )

    def test_the_buffer_summary_ignores_the_volatile_counters(self):
        """摘要只报**占用**，不报那些每帧都在动的计数。

        发布去重按整个载荷比相等（`PortConfigSubscriptions.unchanged`）：
        把 `describe()` 的 `stats` 发出去，等于**每条接口每帧一个包**，
        而载荷里那点变化对界面毫无意义。
        """
        buffer = TransferBuffer(576, "port@test")
        state = make_state(self.ref, buffer=buffer)
        before = config_view(state, self.policy)["buffer_summary"]
        buffer.stats["refused"] += 1
        buffer.stats["withdrawals"] += 1
        self.assertEqual(
            config_view(state, self.policy)["buffer_summary"], before)

    def test_the_wire_field_lists_are_mirrors_of_the_domain(self):
        """镜像常量与真身逐项相等——**这是它们唯一的存在理由**。

        `common/` 不能 import `server/`，所以字段清单只能抄一份过来；
        漂移的表现是「配置被静默丢掉一项」，那时界面少一条过滤而玩家
        看不出是丢包还是本来就没配。顺序不参与比较：py2 的字典没有顺序，
        而 `_require_fields` 比的是集合。
        """
        self.assertEqual(set(PORT_CONFIG_FIELDS), set(PortConfig().to_dict()))
        self.assertEqual(len(PORT_CONFIG_FIELDS), len(PortConfig().to_dict()))
        self.assertEqual(
            sorted(PORT_FILTER_MODES), sorted(FILTER_MODES)
        )


class TestPublishWindow(PortConfigFixture):
    def test_the_session_gets_the_current_configuration_on_its_first_frame(self):
        session = self.open_view()
        self.hub.tick()
        packets = self.configs_on_the_wire()
        self.assertEqual(len(packets), 1)
        envelope = packets[0]
        self.assertEqual(envelope.session_id, session.session_id)
        self.assertEqual(envelope.network_id, NETWORK)
        self.assertEqual(envelope.epoch, EPOCH)
        message = envelope.message
        self.assertEqual(message.device_id, PORT_ID)
        self.assertEqual(message.generation, 1)
        self.assertEqual(message.role, OUTPUT)
        self.assertEqual(
            message.config,
            self.states[self.device.as_key()].config.to_dict(),
        )
        self.assertEqual(message.filter_limit, self.policy.filter_limit_for(OUTPUT))
        # 解回来是元组（构造器统一转的），线上是列表——两处都只表顺序。
        self.assertEqual(list(message.filter_modes), ["whitelist"])

    def test_the_default_configuration_is_published_as_it_really_is(self):
        """新设备默认：启用、白名单、**缓存开着**、空名单。

        `cache_enabled` 的默认值是 `True` 且**不许被界面的默认值顶掉**
        （`PortConfig.from_dict` 的 `False` 必须读得回来）——这条从
        「推出去的这一份」这一侧钉住同一个事实。
        """
        self.open_view()
        self.hub.tick()
        config = self.configs_on_the_wire()[0].message.config
        self.assertTrue(config["cache_enabled"])
        self.assertTrue(config["enabled"])
        self.assertEqual(config["filter_mode"], "whitelist")
        self.assertEqual(config["filter"], [])

    def test_an_unchanged_configuration_is_not_republished(self):
        self.open_view()
        for _ in range(4):
            self.hub.tick()
        self.assertEqual(len(self.configs_on_the_wire()), 1)

    def test_a_changed_configuration_is_republished(self):
        self.open_view()
        self.hub.tick()
        # 玩家在界面上关掉了缓存开关。
        self.states[self.device.as_key()].config = PortConfig(cache_enabled=False)
        self.hub.tick()
        packets = self.configs_on_the_wire()
        self.assertEqual(len(packets), 2)
        self.assertFalse(packets[-1].message.config["cache_enabled"])

    def test_a_device_that_is_not_a_port_publishes_nothing(self):
        """非接口设备收不到这条消息，而不是收到一份空配置。

        一份空配置在界面上读出来就是「这台设备什么都没配」——那是个会让
        玩家去改一台根本没有过滤名单的设备的结论。
        """
        self.states[self.device.as_key()] = None
        self.open_view()
        for _ in range(3):
            self.hub.tick()
        self.assertEqual(self.configs_on_the_wire(), [])

    def test_configuration_is_its_own_message_kind(self):
        """配置不搭在库存线上，也不与设备状态混流：三条各自独立。"""
        self.open_view()
        self.hub.tick()
        kinds = [envelope.message.kind for envelope in self.messages()]
        self.assertEqual(kinds.count(MSG_PORT_CONFIG), 1)
        # 夹具没接运行时，因此设备状态那一条本来就不发；这里要断言的是
        # **配置没有冒充它**。
        self.assertNotIn(MSG_DEVICE_STATUS, kinds)
        self.assertNotIn(MSG_ITEM_DELTA, kinds)


class TestLifecycleCleanup(PortConfigFixture):
    def test_closing_the_session_stops_the_config_and_the_bookkeeping(self):
        session = self.open_view()
        self.hub.tick()
        self.assertEqual(self.hub.config.count(), 1)
        self.hub.close_session(session.session_id, "closed")
        self.assertEqual(self.hub.config.count(), 0)
        self.hub.tick()
        self.assertEqual(len(self.configs_on_the_wire()), 1)

    def test_the_player_leaving_stops_the_config(self):
        self.open_view()
        self.hub.tick()
        self.hub.close_for_player("p1", "player_left")
        self.assertEqual(self.hub.config.count(), 0)
        self.hub.tick()
        self.assertEqual(len(self.configs_on_the_wire()), 1)

    def test_shutdown_clears_every_subscription(self):
        self.open_view()
        self.hub.tick()
        self.hub.shutdown()
        self.assertEqual(self.hub.config.count(), 0)
        report = self.hub.diagnostics()
        self.assertEqual(report["port_config"]["sessions"], 0)
        self.assertEqual(report["port_config"]["published"], 1)

    def test_a_replaced_device_starts_a_new_generation(self):
        """拆掉又在原地放一个：旧会话关闭，新会话的消息带新代次。"""
        self.open_view()
        self.hub.tick()
        self.hub.close_for_device(self.device, "device_replaced")
        self.assertEqual(self.hub.config.count(), 0)

        replacement = DeviceRef(self.device.node_key, 2)
        self.device = replacement
        self.states[replacement.as_key()] = make_state(replacement)
        self.open_view("p1")
        self.hub.tick()
        self.assertEqual(self.configs_on_the_wire()[-1].message.generation, 2)


class TestGuardedReads(PortConfigFixture):
    def test_a_failing_read_publishes_nothing(self):
        self.states[self.device.as_key()] = RuntimeError("读取路径炸了")
        self.open_view()
        self.hub.tick()  # 不得抛出
        self.assertEqual(self.configs_on_the_wire(), [])
        self.assertEqual(self.hub.config.count(), 0)
        # **异常在 `read_config` 的门里就被吞掉并转成 `None`**，因此它不计进
        # `config_errors`——那一桶数的是「读到了却编不出去」，见下一条。
        self.assertEqual(self.hub.stats["config_errors"], 0)

    def test_a_view_that_cannot_be_encoded_is_counted_not_raised(self):
        """读到了、却编不成一条合法消息：计数并跳过，不让整帧作废。"""
        view = config_view(make_state(self.device), self.policy)
        view["filter_limit"] = "九个"  # 不是非负整数，线上校验会拒
        self.states[self.device.as_key()] = view
        self.open_view()
        self.hub.tick()  # 不得抛出
        self.assertEqual(self.configs_on_the_wire(), [])
        self.assertEqual(self.hub.stats["config_errors"], 1)

    def test_a_missing_device_publishes_nothing(self):
        self.states.pop(self.device.as_key())
        self.open_view()
        self.hub.tick()
        self.assertEqual(self.configs_on_the_wire(), [])

    def test_a_non_dict_view_is_refused(self):
        """注入接错时的兜底：宁可一条不发，也不发一条读不懂的。"""
        self.assertIsNone(read_config(self.device, lambda ref: "不是字典"))

    def test_a_configuration_from_another_generation_is_refused(self):
        """代次对不上的配置不得喂进界面——`get_by_ref` 的键含代次，这里再核一遍。"""
        other = config_view(make_state(DeviceRef(self.device.node_key, 9)), self.policy)
        self.assertIsNone(read_config(self.device, lambda ref: other))
        mine = config_view(make_state(self.device), self.policy)
        self.assertIsNotNone(read_config(self.device, lambda ref: mine))

    def test_a_view_without_identity_is_refused(self):
        """身份缺失的消息线上过不了校验，这里先挡掉，免得它在编码期炸。"""
        view = config_view(make_state(self.device), self.policy)
        view["device_id"] = None
        self.assertIsNone(read_config(self.device, lambda ref: view))

    def test_reading_configuration_never_submits_anything(self):
        """11-07 的只读要求：看配置不触发任何业务写入。"""
        self.open_view()
        for _ in range(3):
            self.hub.tick()
        self.assertEqual(self.submissions, [])


class TestWireShape(unittest.TestCase):
    def test_the_message_survives_a_json_round_trip(self):
        ref = DeviceRef(NodeKey(0, 1, 2, 3), 4)
        state = make_state(
            ref,
            role=INTAKE,
            device_id="endernet:bus_item_input_io",
            config=PortConfig(
                filter_mode="blacklist",
                filter=(),
                cache_enabled=True,
            ),
        )
        message = build_config_message(config_view(state, PortPolicy()))
        wire = json.loads(json.dumps(message.to_dict()))
        restored = SyncEnvelope.from_dict(
            SyncEnvelope(message, "s1", NETWORK, EPOCH).to_dict()
        ).message
        self.assertEqual(restored.to_dict(), wire)
        self.assertEqual(restored.config["filter_mode"], "blacklist")

    def test_unknown_and_missing_fields_are_rejected(self):
        ref = DeviceRef(NodeKey(0, 1, 2, 3), 4)
        payload = build_config_message(
            config_view(make_state(ref), PortPolicy())
        ).to_dict()
        # 基线本身必须可解，否则下面的两条断言可能只是碰巧为真。
        self.assertEqual(
            protocol.decode_sync_message(payload).kind, MSG_PORT_CONFIG
        )
        extra = dict(payload)
        extra["extra"] = 1
        with self.assertRaises(ValueError):
            protocol.decode_sync_message(extra)
        missing = dict(payload)
        del missing["filter_limit"]
        with self.assertRaises(ValueError):
            protocol.decode_sync_message(missing)

    def test_a_configuration_missing_a_field_is_refused(self):
        """`config` 那一项按镜像清单逐字段验——少一项就拒收整条。"""
        ref = DeviceRef(NodeKey(0, 1, 2, 3), 4)
        payload = build_config_message(
            config_view(make_state(ref), PortPolicy())
        ).to_dict()
        broken = dict(payload)
        broken["config"] = {
            key: value
            for key, value in payload["config"].items()
            if key != "cache_enabled"
        }
        with self.assertRaises(ValueError):
            protocol.decode_sync_message(broken)

    def test_an_unknown_filter_mode_is_refused(self):
        ref = DeviceRef(NodeKey(0, 1, 2, 3), 4)
        payload = build_config_message(
            config_view(make_state(ref), PortPolicy())
        ).to_dict()
        broken = dict(payload)
        broken["config"] = dict(payload["config"], filter_mode="either")
        with self.assertRaises(ValueError):
            protocol.decode_sync_message(broken)
        broken_modes = dict(payload)
        broken_modes["filter_modes"] = ["either"]
        with self.assertRaises(ValueError):
            protocol.decode_sync_message(broken_modes)


class TestSubscriptions(unittest.TestCase):
    def test_dropping_a_session_that_was_never_seen_is_harmless(self):
        subs = PortConfigSubscriptions()
        subs.drop("s-never")
        self.assertEqual(subs.count(), 0)
        self.assertEqual(subs.stats["dropped"], 0)

    def test_remembering_the_same_payload_twice_still_counts_once(self):
        subs = PortConfigSubscriptions()
        subs.remember("s1", {"a": 1})
        self.assertTrue(subs.unchanged("s1", {"a": 1}))
        self.assertFalse(subs.unchanged("s1", {"a": 2}))
        subs.drop("s1")
        self.assertEqual(subs.count(), 0)
        self.assertEqual(subs.stats["dropped"], 1)


if __name__ == "__main__":
    unittest.main()
