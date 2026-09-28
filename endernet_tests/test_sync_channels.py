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

第 2 条是**跨边界**的：`client/network_client.py` 里那两个字符串同样
只允许以常量的形式出现。它在 11-01 那一步还不存在（先写服务端半边），
那时这条检查只覆盖 `server/`；现在它写出来了，**两侧各查一遍**——
`client/` 不允许 import `server/`，因此同一个字符串注定要在两个文件里
各写一次，而漏改一边不会报错、只会让模组安静地收不到消息。

`server/adapters/messages.py` 与 `client/network_client.py` **都不需要假
引擎就能导入**，本模块因此直接 import 两者——这本身就是一条断言：它证明
引擎导入确实都留在函数体内（`test_import_boundaries.py` 的
`test_no_module_level_engine_import_in_adapter` 静态钉同一件事）。

代价是两侧 `engine_events()` 函数体里的 `....tooldelta` / `...tooldelta`
在离线模块名下是**越界**的相对层级（离线名少两层），调用它必 ImportError。
本模块从不调用它：端口的两条引擎口子都由 `events=` 注入，而那正是它存在
的理由。
"""

import io
import os
import types
import unittest

import testsupport
from endernet.client import network_client
from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.protocol import (
    MSG_SESSION_CLOSE,
    MSG_SESSION_OPEN,
    MSG_SESSION_READY,
    MSG_SNAPSHOT_REQUEST,
    SYNC_CHANNEL_C2S,
    SYNC_CHANNEL_S2C,
    SessionReady,
    SyncEnvelope,
    SyncError,
)
from endernet.server.adapters import messages

MESSAGES_PATH = os.path.join(
    testsupport.ENDERNET_ROOT, "server", "adapters", "messages.py"
)
CLIENT_PATH = os.path.join(
    testsupport.ENDERNET_ROOT, "client", "network_client.py"
)

#: 一个终端实例的引用：位置 + 代次。两侧的用例共用一份，**不复用同一个
#: 可变对象**的风险在这里不存在（`DeviceRef` 是只读值对象）。
DEVICE_REF = DeviceRef(NodeKey(0, 10, 64, 20), 3)


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


# --- 客户端半边（`client/network_client.py`）--------------------------------


class FakeBus(object):
    """MC 事件总线的最小替身，**照真总线的行为回调**。

    真总线（`event_bus.py:71-96`）把引擎送来的原始字典交给
    `event.unmarshal(*args)`，再遍历**当前**那张回调表逐个调用。因此：

    - 已注销的监听器**收不到**（这正是 `unlisten` 那条用例要证明的）；
    - 注销只摘掉回调、不摘掉那张表本身（`_dyn_unlisten` 的已知行为），
      所以替身保留空列表而不是删掉键——否则「注销后再注册」会走出一条
      真机上不存在的路径。
    """

    def __init__(self):
        self.registrations = []
        self.removals = []
        self._handlers = {}

    def ListenEvent(self, event_cls, priority, static=False):
        def wrapper(func):
            # `static` 必须**照真总线（`event_bus.py:37-41`）复现**，不能只收下
            # 不作声：真总线会拿它给 listener 挂 `_td_static`，而绑定方法挂不住
            # 属性，真机上直接抛 AttributeError。替身放行过一次，模组就带着
            # 「`item_catalog` 起不来」的缺陷通过了整个离线门禁。
            if static:
                if hasattr(func, "_td_static"):
                    return func
                func._td_static = True
            self.registrations.append((event_cls, func, priority, static))
            self._handlers.setdefault(event_cls, []).append(func)
            return func

        return wrapper

    def RemoveEventListener(self, event_cls, func, priority=0):
        self.removals.append((event_cls, func, priority))
        handlers = self._handlers.get(event_cls, [])
        if func in handlers:
            handlers.remove(func)

    def deliver(self, event_cls, raw):
        # type: (object, dict) -> int
        """把一条原始字典按真总线的方式送到监听器上，返回回调次数。"""
        event = event_cls.unmarshal(raw)
        count = 0
        for func in list(self._handlers.get(event_cls, [])):
            func(event)
            count += 1
        return count


class ClientPortFixture(unittest.TestCase):
    """公用装配：假引擎模块 + 假总线 + 记录发送的 C2S 基类。

    `RecordingC2S` 覆写的是 `send()`——**客户端没有收件人参数**，这一条与
    服务端的 `send(client_id)` 不同，因此替身也必须不带参数，否则
    「客户端到底有没有传收件人」就测不出来了。
    """

    def setUp(self):
        self.events_module = fake_events_module()
        self.bus = FakeBus()
        self.sent = []

        events_module = self.events_module
        sent = self.sent

        class RecordingC2S(events_module.CustomC2SEvent):
            name = "recording"

            def __init__(self, payload):
                self.payload = payload

            def send(self):
                sent.append(self.payload)

        events_module.CustomC2SEvent = RecordingC2S

    def port(self, events_module=None):
        return network_client.ClientEventPort(
            events=lambda: events_module or self.events_module,
            bus=self.bus,
        )


class TestTheClientUsesTheSameConstants(unittest.TestCase):
    """客户端的事件类必须与协议常量逐字相同。**这条离线可跑，是唯一的。**"""

    def setUp(self):
        self.events_module = fake_events_module()
        self.c2s, self.s2c = network_client.build_event_classes(
            self.events_module
        )

    def test_the_two_channels_match_the_constants(self):
        self.assertEqual(self.c2s.name, SYNC_CHANNEL_C2S)
        self.assertEqual(self.s2c.name, SYNC_CHANNEL_S2C)

    def test_each_class_sits_on_the_right_base(self):
        """方向不能反。客户端的 C2S 是**发**的那一半，挂反了不会报错。"""
        self.assertTrue(issubclass(self.c2s, self.events_module.CustomC2SEvent))
        self.assertTrue(issubclass(self.s2c, self.events_module.CustomS2CEvent))

    def test_each_side_only_implements_its_own_direction(self):
        """客户端只发 C2S、只收 S2C；反着来必须炸，而不是发一个空包。"""
        c2s, s2c = network_client.build_event_classes(self.events_module)
        self.assertEqual(c2s({"kind": "x"}).marshal(), {"kind": "x"})
        with self.assertRaises(NotImplementedError):
            c2s({}).unmarshal({})
        self.assertEqual(s2c({"kind": "x"}).payload, {"kind": "x"})
        with self.assertRaises(NotImplementedError):
            s2c({"kind": "x"}).marshal()

    def test_the_client_file_references_the_constants(self):
        """常量得真的被引用，否则「唯一事实来源」是句空话。"""
        with io.open(CLIENT_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("SYNC_CHANNEL_C2S", source)
        self.assertIn("SYNC_CHANNEL_S2C", source)


class TestClientPortRegistration(ClientPortFixture):
    def test_listen_does_not_ask_the_bus_to_dedup_statically(self):
        """**不传 `static=True`**。

        真总线会拿它给 listener 挂 `_td_static`，而绑定方法挂不住属性——真机上
        正是这个 AttributeError 让模组断在 `OnServerInited`，`item_catalog`
        整条服务起不来。而且那个标记从来挡不住重复注册：回调是每次新建的闭包，
        `hasattr` 永远为假。去重由 `self._handles` 负责（见下一条用例）。
        """
        port = self.port()
        self.assertEqual(port.listen(lambda event: None), 1)
        event_cls, _, priority, static = self.bus.registrations[0]
        _, s2c = port.classes()
        self.assertIs(event_cls, s2c)
        self.assertEqual(priority, 0)
        self.assertFalse(static)

    def test_the_port_builds_its_classes_only_once(self):
        port = self.port()
        first = port.classes()
        self.assertIs(port.classes(), first)

    def test_a_second_listen_is_refused_and_counted(self):
        """注册两次会让同一个包被处理两遍，而症状只是「有时多一次」。"""
        port = self.port()
        first = []
        second = []
        self.assertEqual(port.listen(first.append), 1)
        self.assertEqual(port.listen(second.append), 0)
        _, s2c = port.classes()
        self.bus.deliver(s2c, {"kind": MSG_SESSION_READY})
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(self.bus.registrations), 1)
        self.assertEqual(port.stats["duplicate_listen"], 1)

    def test_unlisten_really_removes_from_the_bus(self):
        """注销必须真的调用引擎的 `RemoveEventListener`。

        只清本地引用不会让回调消失：监听器注册在进程级的表里，而
        `EventBus._dyn_unlisten` 只从回调表里摘 listener（它**不再调**
        `UnListenForEvent`，`_del_sys_event_listener` 无人调用）。因此
        「摘掉了」的判据是**回调不再发生**，这里按真总线的遍历方式断言它。
        """
        port = self.port()
        seen = []
        port.listen(seen.append)
        _, s2c = port.classes()
        self.assertEqual(self.bus.deliver(s2c, {"kind": MSG_SESSION_READY}), 1)
        self.assertEqual(port.unlisten(), 1)
        self.assertEqual(len(self.bus.removals), 1)
        self.assertFalse(port.listening())
        self.assertEqual(self.bus.deliver(s2c, {"kind": MSG_SESSION_READY}), 0)
        self.assertEqual(len(seen), 1)

    def test_a_failing_handler_does_not_kill_the_listener(self):
        calls = []

        def handler(event):
            calls.append(event)
            raise ValueError("boom")

        port = self.port()
        port.listen(handler)
        _, s2c = port.classes()
        self.bus.deliver(s2c, {"kind": "a"})
        self.bus.deliver(s2c, {"kind": "b"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(port.stats["handler_errors"], 2)
        self.assertEqual(port.stats["received"], 2)

    def test_the_counters_are_reported(self):
        port = self.port()
        report = port.diagnostics()
        self.assertEqual(
            report["channels"], [SYNC_CHANNEL_C2S, SYNC_CHANNEL_S2C]
        )
        self.assertEqual(
            sorted(report["stats"]),
            ["duplicate_listen", "handler_errors", "received", "sent"],
        )


class TestClientPortSend(ClientPortFixture):
    def test_a_payload_reaches_the_engine_without_a_recipient(self):
        """客户端没有收件人参数：对端恒为服务端，引擎自己知道是谁在发。"""
        port = self.port()
        self.assertTrue(port.send({"kind": MSG_SESSION_OPEN}))
        self.assertEqual(self.sent, [{"kind": MSG_SESSION_OPEN}])
        self.assertEqual(port.stats["sent"], 1)

    def test_transport_errors_are_not_swallowed_here(self):
        """引擎抛的异常必须往上冒到调用点。

        在这里吞掉的话，「发不出去」会静默变成「发了」，而调用方会继续
        等一个永远不会来的回包。
        """
        events_module = fake_events_module()

        class ExplodingC2S(events_module.CustomC2SEvent):
            name = "exploding"

            def __init__(self, payload):
                self.payload = payload

            def send(self):
                raise RuntimeError("engine is gone")

        events_module.CustomC2SEvent = ExplodingC2S
        with self.assertRaises(RuntimeError):
            self.port(events_module).send({"kind": "x"})


class NetworkClientFixture(ClientPortFixture):
    def setUp(self):
        ClientPortFixture.setUp(self)
        self.client = network_client.NetworkClient(port=self.port())

    def envelope(self, message, session_id="s1", network_id="n1", epoch="e1"):
        return SyncEnvelope(
            message,
            session_id=session_id,
            network_id=network_id,
            epoch=epoch,
        )

    def feed(self, message, **kwargs):
        """把一条 S2C 消息按线上格式喂给客户端，返回解析结果。"""
        return self.client.receive(self.envelope(message, **kwargs).to_dict())

    def open_a_session(self, index_version=7, item_view_version=9):
        self.feed(SessionReady(index_version, item_view_version))


class TestNetworkClientRequests(NetworkClientFixture):
    def body(self, position=0):
        return self.sent[position]["message"]

    def test_open_session_sends_the_expected_body(self):
        request_id = self.client.open_session(DEVICE_REF, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.body()["kind"], MSG_SESSION_OPEN)
        self.assertEqual(self.body()["request_id"], request_id)
        self.assertEqual(self.body()["dimension"], 0)
        self.assertEqual(self.body()["device_ref"], DEVICE_REF.to_dict())

    def test_the_request_carries_no_player_field(self):
        """身份**只能**来自引擎附在 C2S 原始字典上的 `__id__`。

        在消息体里也写一份，等于给「读了对面的自称」留了机会；协议层用
        「未知字段即拒收」把这条路堵死（`_require_fields`），这里钉住
        我们确实没有往消息里塞。
        """
        self.client.open_session(DEVICE_REF, 0)
        body = self.body()
        self.assertNotIn("player_id", body)
        self.assertNotIn("__id__", body)

    def test_no_session_open_session_has_no_session_id_yet(self):
        """`session_open` 是唯一一条在会话存在之前发出的消息。"""
        self.client.open_session(DEVICE_REF, 0)
        self.assertEqual(self.sent[0]["session_id"], "")

    def test_close_session_without_a_session_sends_nothing(self):
        self.assertIsNone(self.client.close_session())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.client.stats["sessions_closed"], 0)

    def test_close_session_sends_and_forgets(self):
        self.open_a_session()
        self.assertTrue(self.client.has_session())
        request_id = self.client.close_session()
        self.assertEqual(self.body(0)["kind"], MSG_SESSION_CLOSE)
        self.assertEqual(self.body(0)["request_id"], request_id)
        self.assertEqual(self.sent[0]["session_id"], "s1")
        #: 本地状态**无条件清掉**：界面已经关了，留着 session_id 只会让
        #: 下一次会话复用一个已经作废的 id。
        self.assertFalse(self.client.has_session())
        self.assertIsNone(self.client.device_ref())
        self.assertEqual(self.client.epoch, "")

    def test_request_snapshot_carries_the_version_it_was_given(self):
        self.open_a_session()
        self.client.request_snapshot(3)
        self.assertEqual(self.body(0)["kind"], MSG_SNAPSHOT_REQUEST)
        self.assertEqual(self.body(0)["from_version"], 3)

    def test_request_snapshot_has_no_default_version(self):
        """`from_version` 必填，**这是设计**：客户端记的是「服务端告知的
        版本」，服务端要的是「本地已应用到的版本」，增量收了一半时两者不等。
        给个缺省值就等于让调用方在不知不觉中按错误的基准补增量。
        """
        self.open_a_session()
        with self.assertRaises(TypeError):
            self.client.request_snapshot()

    def test_session_scoped_requests_refuse_to_go_out_without_a_session(self):
        """没有会话就当场报错，而不是发一条无会话的请求出去。

        不发出去却也不报错更糟：调用方会等一个永远不会来的回包。
        `close_session` 是唯一例外（没得关是正常状态），由它自己的用例钉住。
        """
        with self.assertRaises(ValueError):
            self.client.request_snapshot(0)
        with self.assertRaises(ValueError):
            self.client.ack("t1", 0)
        self.assertEqual(self.sent, [])

    def test_every_request_gets_its_own_id(self):
        self.open_a_session()
        ids = [
            self.client.open_session(DEVICE_REF, 0),
            self.client.request_snapshot(0),
            self.client.ack("t1", 0),
        ]
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(self.client.stats["requests"], 3)

    def test_ack_carries_the_transfer_and_sequence(self):
        self.open_a_session()
        self.client.ack("t1", 4)
        self.assertEqual(self.body(0)["transfer_id"], "t1")
        self.assertEqual(self.body(0)["sequence"], 4)


class TestNetworkClientReceive(NetworkClientFixture):
    def test_session_ready_records_both_versions_separately(self):
        """两个版本各自具名，**不合并成一个 `version`**。

        它们分工不同：`index_version` 是存储提交/交易期望，`item_view_version`
        是物品视图/界面发布。这里给两个**不同**的值，任何一处把两者弄混
        都会让这条红。
        """
        self.open_a_session(index_version=7, item_view_version=9)
        self.assertEqual(self.client.index_version, 7)
        self.assertEqual(self.client.item_view_version, 9)
        self.assertEqual(self.client.session_id, "s1")
        self.assertEqual(self.client.network_id, "n1")
        self.assertEqual(self.client.epoch, "e1")
        self.assertEqual(self.client.stats["sessions_opened"], 1)

    def test_a_bad_packet_is_counted_not_raised(self):
        """收包口**绝不抛**：`value` 来自网络，任何一条都可能坏。

        抛出去只会让回调在引擎里报错（端口的 `handler_errors`），而那条路
        查不出「坏在哪」。
        """
        for bad in (None, "nope", 42, {}, {"kind": MSG_SESSION_READY}):
            self.assertIsNone(self.client.receive(bad))
        self.assertEqual(self.client.stats["bad_packets"], 5)
        self.assertEqual(self.client.stats["received"], 0)

    def test_an_unknown_message_kind_is_a_bad_packet(self):
        data = self.envelope(SessionReady(1, 1)).to_dict()
        data["message"]["kind"] = "invented"
        self.assertIsNone(self.client.receive(data))
        self.assertEqual(self.client.stats["bad_packets"], 1)

    def test_a_negative_version_is_a_bad_packet(self):
        """数量边界在协议层（`require_count`），这里只需要「确实拦住了」，
        不必再判一次——判两次迟早会分叉。"""
        data = self.envelope(SessionReady(1, 1)).to_dict()
        data["message"]["item_view_version"] = -1
        self.assertIsNone(self.client.receive(data))
        self.assertEqual(self.client.stats["bad_packets"], 1)
        self.assertEqual(self.client.item_view_version, 0)

    def test_an_extra_field_is_rejected_rather_than_stripped(self):
        """两个方向**不对称，且这是有依据的**。

        `__id__`（发送者是谁）是引擎在 C2S 方向附的，所以服务端必须在解析前
        把它摘掉。S2C 方向引擎没有发送者身份可附，客户端收到的就是服务端
        `to_dict()` 出去的那一份——**这里不摘任何字段**。若真机上这一侧也
        带了附加字段，症状是每一条都判成坏包：这正是 `stats["bad_packets"]`
        要回答的问题，所以这里钉住「拒收并计数」而不是悄悄容忍。
        """
        data = self.envelope(SessionReady(1, 1)).to_dict()
        data["__id__"] = "p1"
        self.assertIsNone(self.client.receive(data))
        self.assertEqual(self.client.stats["bad_packets"], 1)

    def test_an_error_envelope_does_not_wipe_the_session(self):
        """`error` 是唯一允许空会话字段的 S2C 消息（会话根本不存在时服务端
        没有可填的）。拿它去覆盖，会让一个本来好好的会话在收到一条错误之后
        自己失忆——之后发的每条请求都会被服务端当成无会话而拒掉。
        """
        self.open_a_session()
        payload = SyncEnvelope(SyncError("req-1", "stale_version")).to_dict()
        envelope = self.client.receive(payload)
        self.assertIsNotNone(envelope)
        self.assertEqual(self.client.session_id, "s1")
        self.assertEqual(self.client.epoch, "e1")
        self.assertEqual(self.client.stats["bad_packets"], 0)

    def test_taking_events_drains_them(self):
        self.open_a_session()
        taken = self.client.take_events()
        self.assertEqual(len(taken), 1)
        self.assertEqual(taken[0].message.kind, MSG_SESSION_READY)
        #: 取走即出队：留着它们会让下一帧把同一批增量再应用一遍。
        self.assertEqual(self.client.take_events(), [])
        self.assertEqual(self.client.pending_count(), 0)

    def test_taking_a_bounded_number_leaves_the_rest(self):
        self.feed(SessionReady(1, 1))
        self.feed(SessionReady(2, 2))
        taken = self.client.take_events(1)
        self.assertEqual(len(taken), 1)
        self.assertEqual(self.client.pending_count(), 1)

    def test_the_queue_is_bounded_and_drops_the_oldest(self):
        """丢最旧：最新的那条决定界面现在该显示什么。

        丢包的后果**是可发现的**（版本链断掉 → 模型抛 `ListResyncRequired`
        去重取快照），不是静默错误，因此这里可以接受一个有界的队列。
        """
        client = network_client.NetworkClient(
            port=self.port(), queue_limit=2
        )
        for index in (1, 2, 3):
            client.receive(
                self.envelope(SessionReady(index, index)).to_dict()
            )
        self.assertEqual(client.pending_count(), 2)
        self.assertEqual(client.stats["dropped"], 1)
        self.assertEqual(
            [item.message.index_version for item in client.take_events()],
            [2, 3],
        )

    def test_handlers_are_called_immediately(self):
        seen = []
        self.client.on_message(lambda envelope, message: seen.append(message))
        self.open_a_session()
        self.assertEqual([message.kind for message in seen], [MSG_SESSION_READY])

    def test_a_failing_handler_does_not_stop_the_others(self):
        """一个回调抛异常不能让后面的回调收不到，也不能让包停在中途。"""
        seen = []

        def boom(envelope, message):
            raise ValueError("boom")

        self.client.on_message(boom)
        self.client.on_message(lambda envelope, message: seen.append(message))
        envelope = self.feed(SessionReady(1, 1))
        self.assertIsNotNone(envelope)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.client.stats["handler_errors"], 1)
        #: 状态与队列在派发**之前**就已经更新，因此炸掉一个回调不会丢包。
        self.assertEqual(self.client.session_id, "s1")
        self.assertEqual(self.client.pending_count(), 1)

    def test_clear_handlers_stops_them(self):
        seen = []
        self.client.on_message(lambda envelope, message: seen.append(message))
        self.assertEqual(self.client.clear_handlers(), 1)
        self.feed(SessionReady(1, 1))
        self.assertEqual(seen, [])

    def test_a_packet_that_arrives_through_the_bus_is_decoded(self):
        """整条链路：总线 → 事件类 `unmarshal` → `receive` → 状态。

        这条是**唯一**把两端接起来跑的用例——上面所有 `receive` 都是直接
        喂字典，而真机上那个字典是先经过 `ClientSyncEvent.unmarshal` 的。
        """
        port = self.client.port
        self.assertEqual(port.listen(self.client.receive), 1)
        _, s2c = port.classes()
        raw = self.envelope(SessionReady(7, 9)).to_dict()
        self.assertEqual(self.bus.deliver(s2c, raw), 1)
        self.assertEqual(self.client.session_id, "s1")
        self.assertEqual(self.client.item_view_version, 9)

    def test_diagnostics_keeps_the_two_versions_apart(self):
        self.open_a_session(index_version=7, item_view_version=9)
        report = self.client.diagnostics()
        self.assertEqual(report["index_version"], 7)
        self.assertEqual(report["item_view_version"], 9)
        self.assertEqual(report["session_id"], "s1")
        self.assertEqual(report["port"]["channels"], [SYNC_CHANNEL_C2S, SYNC_CHANNEL_S2C])
        #: 计数器的键同样是契约：实机上排障要按名字去查，少一个键不会报错，
        #: 只会让人以为「没有这件事发生」。
        self.assertEqual(
            sorted(report["stats"]),
            [
                "bad_packets",
                "dropped",
                "handler_errors",
                "received",
                "requests",
                "sessions_closed",
                "sessions_opened",
            ],
        )


if __name__ == "__main__":
    unittest.main()
