# coding=utf-8
"""同步通道的引擎边界（任务 11-01）。本模块只钉一件事：**两侧的频道名与
身份来源不会悄悄漂移。**

它们漂移的代价与别处不同——不报错、不刷屏，只是**什么都收不到**。
`common/protocol.py` 里两个常量是唯一事实来源，而服务端与客户端各自
定义自己的事件类（`client/` 不能 import `server/`），于是同一个字符串
在两侧各写一次。改了一边、漏了另一边，症状是模组安静地不工作。

三条互补的检查：

1. **类定义对着常量**：用真的 `build_event_classes()` 造出事件类，
   断言 `name` 等于常量。基类来源换成一个假 `tooldelta.events`——
   被测的是那两个字符串，换基类不影响它们。
2. **常量以外没有第二份抄写**：扫业务代码里那两个字符串的字面量，
   只允许出现在 `common/protocol.py` 里。
3. **端口本身能脱机跑**：注册、身份、位置、发送四条路径都用替身驱动。

第 2 条是**跨边界**的：`client/network_client.py` 还没写（11-01 之外
的部分），但它一旦把频道名写成字面量，这条现在就会红——这正是它该有
的作用；而在它写出来之前，这里**不做**它的防漂移检查（不假装测过）。

`server/adapters/messages.py` **不需要假引擎就能导入**，本模块因此直接
`from endernet.server.adapters import messages`——这本身就是一条断言：
它证明引擎导入确实都留在函数体内（`test_import_boundaries.py` 的
`test_no_module_level_engine_import_in_adapter` 静态钉同一件事）。

代价是 `messages.py` 里 `engine_events()` 那个函数体里的 `....tooldelta`
在离线模块名下是**越界**的相对层级（离线名少两层），调用它必 ImportError。
本模块从不调用它：端口的两条引擎口子都由 `events=` 注入，而那正是它存在
的理由。
"""

import io
import os
import types
import unittest

import testsupport
from endernet.common.protocol import SYNC_CHANNEL_C2S, SYNC_CHANNEL_S2C
from endernet.server.adapters import messages

MESSAGES_PATH = os.path.join(
    testsupport.ENDERNET_ROOT, "server", "adapters", "messages.py"
)


class FakeEngineEvent(object):
    """`tooldelta.events` 里事件基类的替身。

    **照着真基类的契约来**，不是随手造一个空类：`ServerEvent` 的
    `marshal`/`unmarshal` 都抛 `NotImplementedError`（`basic.py:15-26`），
    替身照搬这一条，下面「反着发必须炸」那条用例才是真在测我们的类，
    而不是在测替身恰好没实现某个方法。
    """

    name = "fake"

    def marshal(self):
        raise NotImplementedError

    @classmethod
    def unmarshal(cls, data):
        raise NotImplementedError("Not server or client event")

    def send(self, *args):
        #: 替身不该真的发消息。真发成功反而是坏消息——它说明测试打到了
        #: 引擎上，而不是打在被测代码上。
        raise AssertionError("替身不该真的发消息：%r" % (args,))


class FakeEventsModule(types.ModuleType):
    """假的 `tooldelta.events`。

    **不是空模块**：`build_event_classes` 会从它取两个自定义事件基类，
    取不到就在建类时 `AttributeError`——那会被误读成「messages.py 写错了」，
    其实是假模块给少了。玩家离线事件也在这里。
    """

    def __init__(self):
        types.ModuleType.__init__(self, "endernet_tests.fake_tooldelta.events")
        self.CustomC2SEvent = type("FakeCustomC2SEvent", (FakeEngineEvent,), {})
        self.CustomS2CEvent = type("FakeCustomS2CEvent", (FakeEngineEvent,), {})
        self.DelServerPlayerEvent = type(
            "FakeDelServerPlayerEvent", (FakeEngineEvent,), {}
        )


class FakeBinder(object):
    """`ServerEventBinder.register` 的最小替身：只记账。"""

    def __init__(self):
        self.registrations = []

    def register(self, event_cls, func, priority=0):
        self.registrations.append((event_cls, func, priority))
        return func

    def classes(self):
        return [item[0] for item in self.registrations]


def fake_events_module():
    # type: () -> FakeEventsModule
    """一个新的假 `tooldelta.events`。**每次新建**：用例会往它上面挂记录用的
    替身类，共用一份的话，前一个用例的替身会漏到后一个用例里。"""
    return FakeEventsModule()


def build_pair(events_module):
    # type: (object) -> tuple
    """用真的 `build_event_classes()` 造一对事件类。"""
    return messages.build_event_classes(events_module)


class TestEventClassNamesMatchTheProtocol(unittest.TestCase):
    """事件类的 `name` 必须来自 `common/protocol.py` 的常量。"""

    def setUp(self):
        self.events_module = fake_events_module()
        self.c2s, self.s2c = build_pair(self.events_module)

    def test_the_two_channels_match_the_constants(self):
        self.assertEqual(self.c2s.name, SYNC_CHANNEL_C2S)
        self.assertEqual(self.s2c.name, SYNC_CHANNEL_S2C)

    def test_each_class_sits_on_the_right_base(self):
        """方向不能反：C2S 挂在客户端→服务端的基类上，S2C 挂在反方向的基类上。

        挂反了不会报错——它会安静地注册到一条没人发的通道上。
        """
        self.assertTrue(issubclass(self.c2s, self.events_module.CustomC2SEvent))
        self.assertTrue(issubclass(self.s2c, self.events_module.CustomS2CEvent))

    def test_building_twice_yields_distinct_classes(self):
        """`build_event_classes` 每次新建，**缓存的责任在端口实例上**。

        这条是给 `AdapterSyncPort.classes()` 立前提的：如果这里返回同一对类，
        那边就不需要缓存；而 `EventBus` 恰恰是拿**类对象本身**当字典键的
        （`event_listeners[priority][event]`），两次拿到不同的类对象会注册到
        两张表里，注销只摘得掉一张——症状是热重载后同一个包被处理两次。
        """
        again = build_pair(self.events_module)
        self.assertIsNot(again[0], self.c2s)
        self.assertIsNot(again[1], self.s2c)


class TestTheChannelNamesAreWrittenOnce(unittest.TestCase):
    """两个频道名只有一份字面量——`common/protocol.py`。"""

    def test_no_other_file_repeats_the_channel_strings(self):
        offenders = []
        for path in testsupport.source_files():
            if os.path.basename(path) == "protocol.py":
                continue
            with io.open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
            for constant in (SYNC_CHANNEL_C2S, SYNC_CHANNEL_S2C):
                if constant in source:
                    offenders.append(
                        "%s 抄了 %r" % (os.path.basename(path), constant)
                    )
        self.assertEqual(
            offenders,
            [],
            "频道名出现第二份抄写。改一边漏一边不会报错，只会让模组安静地"
            "收不到消息：\n  " + "\n  ".join(offenders),
        )

    def test_the_constants_are_meaningful_and_distinct(self):
        """守卫的前提：两个常量本身得是有意义的、彼此不同的名字。"""
        self.assertTrue(SYNC_CHANNEL_C2S)
        self.assertTrue(SYNC_CHANNEL_S2C)
        self.assertNotEqual(SYNC_CHANNEL_C2S, SYNC_CHANNEL_S2C)

    def test_the_adapter_uses_the_constants(self):
        """常量得真的被引用，否则「唯一事实来源」是句空话。"""
        with io.open(MESSAGES_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("SYNC_CHANNEL_C2S", source)
        self.assertIn("SYNC_CHANNEL_S2C", source)


class TestIdentityComesFromTheEngine(unittest.TestCase):
    """身份只有一个来源：引擎附在原始字典上的 `__id__`。"""

    def setUp(self):
        self.events_module = fake_events_module()

    def port(self):
        return messages.AdapterSyncPort(
            FakeBinder(), events=lambda: self.events_module
        )

    def test_the_engine_id_is_used(self):
        self.assertEqual(self.port().source_player_id({"__id__": "p1"}), "p1")

    def test_a_self_declared_player_is_ignored(self):
        """消息体里自称的玩家一律不认。

        这是**唯一**能把伪造身份挡在外面的地方：读了一个对面写的字段，
        会话就绑到了请求方指定的玩家身上，而距离校验是对着这个身份做的，
        等于白做。
        """
        port = self.port()
        self.assertEqual(port.source_player_id({"player_id": "p2"}), "")
        self.assertEqual(
            port.source_player_id({"player_id": "p2", "__id__": "p1"}), "p1"
        )

    def test_missing_or_malformed_data_yields_empty(self):
        port = self.port()
        self.assertEqual(port.source_player_id({}), "")
        self.assertEqual(port.source_player_id(None), "")
        self.assertEqual(port.source_player_id("p1"), "")
        self.assertEqual(port.source_player_id({"__id__": 7}), "")

    def test_the_event_instance_reads_the_same_field(self):
        c2s, _ = build_pair(self.events_module)
        self.assertEqual(c2s({"__id__": "p1"}).source_player_id(), "p1")
        self.assertEqual(c2s({}).source_player_id(), "")
        self.assertEqual(c2s(None).data, {})

    def test_the_event_keeps_the_raw_dictionary(self):
        """原始字典原样带着——解析规则在协议层，适配器不提前解释它。"""
        c2s, _ = build_pair(self.events_module)
        raw = {"__id__": "p1", "kind": "session_open"}
        self.assertIs(c2s(raw).data, raw)

    def test_each_side_only_implements_its_own_direction(self):
        """服务端只收 C2S、只发 S2C。反着来时必须炸，而不是发一个空包。

        `marshal` 没被 C2S 覆写，于是走到基类的 `NotImplementedError`
        （`tooldelta/events/basic.py:15`，替身照搬了这条契约）。
        """
        c2s, s2c = build_pair(self.events_module)
        with self.assertRaises(NotImplementedError):
            c2s({"__id__": "p1"}).marshal()
        self.assertEqual(s2c({"kind": "x"}).marshal(), {"kind": "x"})


class TestPortRegistration(unittest.TestCase):
    """注册：两个监听器都要挂上，且挂在正确的类上。"""

    def setUp(self):
        self.events_module = fake_events_module()
        self.binder = FakeBinder()
        self.port = messages.AdapterSyncPort(
            self.binder, events=lambda: self.events_module
        )

    def test_listen_registers_the_request_class(self):
        self.assertEqual(self.port.listen(lambda event: None), 1)
        self.assertEqual(len(self.binder.registrations), 1)
        c2s, _ = self.port.classes()
        self.assertIs(self.binder.classes()[0], c2s)

    def test_the_port_builds_its_classes_only_once(self):
        """同一个端口两次 `classes()` 必须是**同一个**类对象。

        见 `test_building_twice_yields_distinct_classes`：`EventBus` 拿类
        对象当键，两次建类就是两张表，注销只摘得掉一张。
        """
        first = self.port.classes()
        self.assertIs(self.port.classes(), first)
        self.assertEqual(first, self.port.classes())

    def test_player_leave_registers_the_leave_event(self):
        self.assertEqual(self.port.on_player_leave(lambda player_id: None), 1)
        self.assertIs(
            self.binder.classes()[0], self.events_module.DelServerPlayerEvent
        )

    def test_the_leave_handler_receives_the_player_id(self):
        seen = []
        self.port.on_player_leave(seen.append)
        _, dispatch, _ = self.binder.registrations[0]
        dispatch(types.SimpleNamespace(id="p1"))
        self.assertEqual(seen, ["p1"])
        self.assertEqual(self.port.stats["leaves"], 1)

    def test_the_request_count_is_kept(self):
        """「引擎到底有没有把消息送进来」在实机上最难回答，所以有个计数器。

        `stats["requests"]` 恒为 0 就直接排除了「总线挂错了」以外的全部猜测。
        """
        seen = []
        self.port.listen(seen.append)
        _, dispatch, _ = self.binder.registrations[0]
        dispatch("packet-1")
        dispatch("packet-2")
        self.assertEqual(seen, ["packet-1", "packet-2"])
        self.assertEqual(self.port.stats["requests"], 2)

    def test_a_failing_handler_does_not_kill_the_listener(self):
        """一条坏包不该让后续所有包都不再被处理。

        回调把异常抛回引擎，后果不止是这一条失败——引擎会记一条错，而下一个
        包是否还会被送来取决于引擎的实现细节。因此这里必须自己接住
        （`drive_once()` 同一条纪律）。
        """
        calls = []

        def handler(event):
            calls.append(event)
            if event == "bad":
                raise ValueError("boom")

        self.port.listen(handler)
        _, dispatch, _ = self.binder.registrations[0]
        dispatch("bad")
        dispatch("good")
        self.assertEqual(calls, ["bad", "good"])
        self.assertEqual(self.port.stats["handler_errors"], 1)
        self.assertEqual(self.port.stats["requests"], 2)

    def test_a_failing_leave_handler_does_not_kill_the_listener(self):
        def handler(player_id):
            raise ValueError("boom")

        self.port.on_player_leave(handler)
        _, dispatch, _ = self.binder.registrations[0]
        dispatch(types.SimpleNamespace(id="p1"))
        self.assertEqual(self.port.stats["handler_errors"], 1)


class TestPortSend(unittest.TestCase):
    """发送：只回答「有没有收件人」，引擎抛的异常留给调用方计数。"""

    def setUp(self):
        self.events_module = fake_events_module()
        self.binder = FakeBinder()
        self.sent = []

        events_module = self.events_module
        sent = self.sent

        class RecordingS2C(events_module.CustomS2CEvent):
            """记下每一次发送。`sent` 是从 `setUp` 闭包进来的。"""

            name = "recording"

            def __init__(self, payload):
                self.payload = payload

            def send(self, client_id):
                sent.append((client_id, self.payload))

        events_module.CustomS2CEvent = RecordingS2C

    def port(self, events_module=None):
        return messages.AdapterSyncPort(
            self.binder,
            events=lambda: events_module or self.events_module,
        )

    def test_a_payload_reaches_the_named_player(self):
        port = self.port()
        self.assertTrue(port.send("p1", {"kind": "session_ready"}))
        self.assertEqual(self.sent, [("p1", {"kind": "session_ready"})])
        self.assertEqual(port.stats["sent"], 1)

    def test_the_port_counts_what_it_sent(self):
        port = self.port()
        port.send("p1", {"kind": "a"})
        port.send("p1", {"kind": "b"})
        self.assertEqual(
            self.sent, [("p1", {"kind": "a"}), ("p1", {"kind": "b"})]
        )
        self.assertEqual(port.stats["sent"], 2)

    def test_no_recipient_is_refused_without_touching_the_engine(self):
        """空 id 是**可预判的**「没人在收」，不问引擎，直接答否并计数。"""
        port = self.port()
        self.assertFalse(port.send("", {"kind": "error"}))
        self.assertEqual(self.sent, [])
        self.assertEqual(port.stats["refused"], 1)
        self.assertEqual(port.stats["sent"], 0)

    def test_transport_errors_are_not_swallowed_here(self):
        """引擎抛的异常**必须往上冒**：hub 的 `transport_errors` 靠它计数。

        在这里吞掉的话，「没人在收」与「引擎坏了」会变成同一个 `False`，
        而这两种情况的查法完全不同——一个查订阅表，一个查引擎日志。
        """
        events_module = fake_events_module()

        class ExplodingS2C(events_module.CustomS2CEvent):
            name = "exploding"

            def __init__(self, payload):
                self.payload = payload

            def send(self, client_id):
                raise RuntimeError("engine is gone")

        events_module.CustomS2CEvent = ExplodingS2C
        with self.assertRaises(RuntimeError):
            self.port(events_module).send("p1", {"kind": "x"})


class TestPortPositions(unittest.TestCase):
    """位置查询：查不到就是 `None`，由会话校验去决定怎么处理。"""

    def setUp(self):
        self.events_module = fake_events_module()

    def port(self, dimension=None, position=None):
        return messages.AdapterSyncPort(
            FakeBinder(),
            events=lambda: self.events_module,
            player_dimension=dimension,
            player_position=position,
        )

    def test_values_pass_through(self):
        port = self.port(
            dimension=lambda player_id: 0,
            position=lambda player_id: (1.5, 64.0, -2.5),
        )
        self.assertEqual(port.dimension_of("p1"), 0)
        self.assertEqual(port.position_of("p1"), (1.5, 64.0, -2.5))

    def test_a_failing_query_is_none_not_zero(self):
        """**`None` 与 `0` 是两回事**：维度 0 是主世界，而「查不到」必须走到
        会话校验的 fail-closed 那一支去，不能被读成「玩家在主世界」——
        后者会去读另一个维度同坐标的方块。
        """

        def boom(player_id):
            raise RuntimeError("player is gone")

        port = self.port(dimension=boom, position=boom)
        self.assertIsNone(port.dimension_of("p1"))
        self.assertIsNone(port.position_of("p1"))

    def test_an_empty_player_id_is_not_queried(self):
        """没有身份就不要去问引擎——问了只会拿到一条查不到玩家的错误日志。"""
        calls = []

        def asker(player_id):
            calls.append(player_id)
            return 0

        port = self.port(dimension=asker)
        self.assertIsNone(port.dimension_of(""))
        self.assertEqual(calls, [])


class TestDiagnostics(unittest.TestCase):
    def test_the_channels_are_reported(self):
        port = messages.AdapterSyncPort(
            FakeBinder(), events=fake_events_module
        )
        report = port.diagnostics()
        self.assertEqual(report["channels"], [SYNC_CHANNEL_C2S, SYNC_CHANNEL_S2C])
        #: 计数器的键是**契约**的一部分：实机上排障时要按名字去查，
        #: 少一个键不会报错，只会让人以为「没有这件事发生」。
        self.assertEqual(
            sorted(report["stats"]),
            ["handler_errors", "leaves", "refused", "requests", "sent"],
        )


if __name__ == "__main__":
    unittest.main()
