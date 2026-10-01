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
    MSG_COMMAND,
    MSG_SESSION_CLOSE,
    MSG_SESSION_OPEN,
    MSG_SESSION_READY,
    MSG_SNAPSHOT_REQUEST,
    STATUS_OK,
    SYNC_CHANNEL_C2S,
    SYNC_CHANNEL_S2C,
    CommandResult,
    CommandResultMessage,
    SessionReady,
    SnapshotChunk,
    SyncEnvelope,
    SyncError,
    TerminalCommand,
)
from endernet.common.resource_key import ResourceKey
from endernet.common.resource_sync import ResourceDescriptions
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
        """`build_event_classes` 每次新建，**缓存的责任在 `classes()` 上**。

        这条是给 `AdapterSyncPort.classes()` / `ClientEventPort.classes()` 立
        前提的：如果这里返回同一对类，那两边就不需要缓存；而 `EventBus` 恰恰
        是拿**类对象本身**当字典键的（`event_listeners[priority][event]`），
        两次拿到不同的类对象就多登记一次，而注销只摘得掉其中一张表。
        真机上多登记这一次的代价是**每一条 S2C 消息多投递一份**，见
        `TestEventClassesAreSharedPerProcess`。
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

    def test_a_second_port_shares_the_same_classes(self):
        """**两个端口也必须是同一对类**，不只是同一个端口两次。

        客户端那一侧实测过这条的代价（`TestEventClassesAreSharedPerProcess`）：
        各建一份 → 引擎侧多登记一次 → 每建一个端口，同一条消息就多投递一份。
        服务端这一侧本轮**没有实测到重复**（进程里只有一个装配实例），这条
        用例按同一个构造、同一个理由立前提，**不记为服务端已实测**。
        """
        second = messages.AdapterSyncPort(
            self.binder, events=lambda: self.events_module
        )
        self.assertIs(second.classes()[0], self.port.classes()[0])
        self.assertIs(second.classes()[1], self.port.classes()[1])

    def test_a_different_events_module_gets_its_own_classes(self):
        """缓存按模块对象分，且**两侧不共用**：客户端的 `build_event_classes`
        造出的方向相反，共用一张缓存会让 S2C 的类变成「只发不收」的那一半。"""
        other = fake_events_module()
        self.assertIsNot(
            messages.shared_event_classes(self.events_module)[0],
            messages.shared_event_classes(other)[0],
        )
        self.assertIsNot(
            messages.shared_event_classes(self.events_module)[0],
            network_client.shared_event_classes(self.events_module)[0],
        )

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
      真机上不存在的路径；
    - **登记按事件类去重**（`_dyn_listen` 的 `event not in
      self.event_listeners[priority]`），同一个类注册第二个回调**不会**再往
      引擎登记一次。替身必须照做：这一条正是「每个端口建一份事件类」那个
      缺陷在离线看不出来的原因（真机实测第 4 个端口收到 4 份，见
      `network_client.shared_event_classes`）。
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
            if event_cls not in self._handlers:
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


class TestEventClassesAreSharedPerProcess(ClientPortFixture):
    """事件类**进程内只有一份**，不是「每个端口一份」。

    真机实测（2026-09-29）：连开 4 个端口，第 4 个把同一条 S2C 消息收到了
    **4 份**，而同一条消息的 `session_id` 四份完全相同——服务端只发了一次，
    重复全在客户端。第 N 个端口收到 N 份的原因是**引擎侧的登记只增不减**：

    - `EventBus._dyn_listen` 以**事件类对象**为键判重，新建一个类就多一次
      `_add_sys_event_listener`；
    - `_add_sys_event_listener` 用 `tdevent_handler_<模块>.<类名><优先级>` 这个
      名字把处理器 `setattr` 到 system 上，几个端口造出的类同模块同类名，
      于是处理器同名、只留得下最后一份——实测里先注册的端口**一条都收不到**；
    - 注销走 `_dyn_unlisten`，它从不调 `UnListenForEvent`，所以关掉界面
      （`unlisten`）也不会把登记还回去。

    后果不止「多算一遍」：同一批增量被应用两次时第二次的 `base_version` 已经
    对不上，模型判成断档并重取快照——**每开一次界面，之后每次刷新都白重取
    一份快照**。

    `FakeBus` 按真总线的去重规则记账（`registrations` 每个事件类只记一条），
    所以「登记了几条」在这里是可断言的。引擎那一侧的命名冲突没法在离线复现，
    它由 `shared_event_classes` 从源头上避免（类相同 → 判重命中 → 根本不登记）。
    """

    def test_two_ports_share_one_pair_of_classes(self):
        first = self.port()
        second = self.port()
        self.assertIs(first.classes()[0], second.classes()[0])
        self.assertIs(first.classes()[1], second.classes()[1])
        self.assertIsNot(first, second)

    def test_a_different_events_module_gets_its_own_classes(self):
        """缓存按**模块对象**分，不按全局。两个模块共用一份会让离线测试互相
        串台——而真机上只有一份 `tooldelta.events`，不构成问题。"""
        other = fake_events_module()
        self.assertIsNot(
            network_client.shared_event_classes(self.events_module)[1],
            network_client.shared_event_classes(other)[1],
        )

    def test_the_helper_is_idempotent(self):
        first = network_client.shared_event_classes(self.events_module)
        self.assertIs(network_client.shared_event_classes(self.events_module), first)

    def test_opening_and_closing_three_times_registers_the_bus_once(self):
        """**界面开关三轮，引擎侧只应登记一条。**

        这一条是那个真机缺陷的回归门：按「每个端口建一份」的写法，这里会是
        三条登记，实机上就是第三条起每条消息投递三份。
        """
        seen = []
        s2c = None
        for _ in range(3):
            port = self.port()
            port.listen(seen.append)
            _, s2c = port.classes()
            port.unlisten()
        self.assertEqual(len(self.bus.registrations), 1)
        # 最后一个端口已经注销，此时一条都到不了回调上——「登记还在」不等于
        # 「还收得到」，这正是 `unlisten` 必须真的调 `RemoveEventListener`。
        self.assertEqual(self.bus.deliver(s2c, {"kind": MSG_SESSION_READY}), 0)
        self.assertEqual(seen, [])
        # 再开一轮（第四个端口）：仍然只有那一条登记，且它收得到、只收一份。
        again = self.port()
        again.listen(seen.append)
        self.assertIs(again.classes()[1], s2c)
        self.assertEqual(len(self.bus.registrations), 1)
        self.assertEqual(self.bus.deliver(s2c, {"kind": MSG_SESSION_READY}), 1)
        self.assertEqual(len(seen), 1)

    def test_each_live_port_receives_the_message_exactly_once(self):
        """两个端口同时听着（同一份类）：一条消息**各收一份**，不是各收两份。

        「同时两个端口」在生产里不会出现（一个界面一个），但它是这条不变量
        最直接的量法：登记只有一条，回调表里有几个回调就调几次。
        """
        ports = [self.port(), self.port()]
        seen = [[], []]
        for index, port in enumerate(ports):
            port.listen(seen[index].append)
        _, s2c = ports[0].classes()
        self.assertEqual(len(self.bus.registrations), 1)
        self.bus.deliver(s2c, {"kind": MSG_SESSION_READY})
        self.assertEqual([len(calls) for calls in seen], [1, 1])


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

    def test_request_ids_are_unique_across_clients(self):
        """**序号是进程级的，不是实例级的。**

        界面每打开一次就新建一个 `NetworkClient`。序号若随实例从 0 重新
        开始，第二个会话里序列相同的请求会与上一个会话**撞号**，而服务端的
        去重账本按 `(request_id, 来源设备实例)` 记账、保留约 900 tick——
        那条命令拿回来的是一条**重放结果**（状态仍是当初的 `ok`），
        **东西一件没搬**。2026-10-01 真机症状：关掉终端再打开、同样两个
        槽位再交换一次，界面按本地虚拟空间画出了交换，真实背包纹丝不动。
        """
        other = network_client.NetworkClient(port=self.port())
        first = self.client.open_session(DEVICE_REF, 0)
        second = other.open_session(DEVICE_REF, 0)
        self.assertNotEqual(first, second)
        # 同一个实例内部仍然逐条递增（`_in_flight` 按 id 挂回执）。
        self.open_a_session()
        self.assertNotEqual(first, self.client.request_snapshot(0))

    def test_ack_carries_the_transfer_and_sequence(self):
        self.open_a_session()
        self.client.ack("t1", 4)
        self.assertEqual(self.body(0)["transfer_id"], "t1")
        self.assertEqual(self.body(0)["sequence"], 4)


class TestNetworkClientCommands(NetworkClientFixture):
    """客户端的命令通道（11-03）。

    与 `TestTerminalCommands`（`test_sync_publish.py`）是**同一条协议的两端**：
    那边钉服务端怎么读一条命令、回的又是什么；这边钉客户端发出去的那份
    线上字典长什么样、回执怎么挂回请求上。
    """

    def command(self, action="inv_take", **kwargs):
        """发一条命令，返回 `(request_id, 线上字典)`。"""
        request_id = self.client.send_command(action, **kwargs)
        return request_id, self.sent[-1]

    def result_of(self, request_id, status=STATUS_OK, **extra):
        """造一条形状合法的回执字典（`CommandResult.to_dict()` 的那一份）。"""
        result = CommandResult.ok(request_id, {"action": "inv_take"}).to_dict()
        result.update(extra)
        return CommandResultMessage(request_id, result)

    def test_send_command_sends_the_expected_body(self):
        """这条钉的是**字段被原样带过去**，因此 `slot` 之类仍用占位值。

        但 **`key` 不能用占位值**：它的契约是「收 `ResourceKey`、在
        `send_command` 里转成线上字典」，塞别的东西进去会当场炸（那里直接
        调 `.to_dict()`）。这正是一条形状用例该有的样子——它顺手把「`key`
        到底是什么」也钉住了。
        """
        self.open_a_session()
        key = ResourceKey.item("minecraft:stone")
        request_id, data = self.command(
            "inv_take", expected_index_version=7, key=key,
            count=32, slot="slot_0", slot_version=3,
        )
        self.assertEqual(data["session_id"], "s1")
        self.assertEqual(data["network_id"], "n1")
        self.assertEqual(data["epoch"], "e1")
        body = data["message"]
        self.assertEqual(body["kind"], MSG_COMMAND)
        self.assertEqual(body["request_id"], request_id)
        self.assertEqual(body["action"], "inv_take")
        self.assertEqual(body["expected_index_version"], 7)
        self.assertEqual(body["key"], key.to_dict(), "线上形状是字典")
        self.assertEqual(body["count"], 32)
        self.assertEqual(body["slot"], "slot_0")
        self.assertEqual(body["slot_version"], 3)

    def test_the_whole_body_is_msgpack_encodable(self):
        """**真机撞过（2026-09-30）。** C2S 的信封是引擎用 msgpack 编的，
        而它编不了自定义对象。

        `key` 曾经是 `ResourceKey` 实例、被 `to_dict()` 原样写进消息，于是
        报名 `can not serialize 'ResourceKey' object`——**而那是发包那一刻
        才炸的**，界面上什么都看不到，只有引擎日志里一行。协议层每一处形状
        检查都拦不住它：它们看的是「键在不在」「值是不是文本/整数」，而
        `key` 这两样都不查。

        这条直接拿编码器过一遍**整条信封**，因此将来任何一种「塞了个对象
        进去」都能在这里被拦下，而不必逐个字段去加检查。
        """
        try:
            import msgpack
        except ImportError:      # 引擎自带；离线环境没装就跳过
            self.skipTest("没有 msgpack，跳过编码检查")
        self.open_a_session()
        for action, kwargs in (
            ("inv_take", dict(expected_index_version=7, count=5, slot=9,
                              slot_version=7)),
            ("inv_swap", dict(expected_index_version=7, count=None, slot=9,
                              slot_version=7, into_slot=10,
                              into_slot_version=7)),
            ("inv_put", dict(expected_index_version=7, count=5)),
        ):
            _, data = self.command(
                action, key=ResourceKey.item("minecraft:stone"), **kwargs
            )
            msgpack.packb(data, use_bin_type=True)

    def test_command_absent_optionals_are_still_written_as_none(self):
        """可选字段**必须写出来**，不能省略。

        `_require_fields` 既拒缺项也拒多项，所以「不写＝没这个意思」在这里
        不成立。这条同时是「客户端多塞一个 `cursor` 会当场变成协议错误」的
        依据——真正收窄字段集的地方是 `TerminalCommand.FIELDS`，不是这里
        少写几个键。
        """
        self.open_a_session()
        _, data = self.command("inv_take")
        body = data["message"]
        for field in TerminalCommand.FIELDS:
            self.assertIn(field, body, "缺少字段 %s" % (field,))
        self.assertIsNone(body["key"])
        self.assertIsNone(body["count"])
        self.assertIsNone(body["slot"])
        self.assertIsNone(body["slot_version"])
        self.assertIsNone(body["into_slot"])
        self.assertIsNone(body["into_slot_version"])
        self.assertIsNone(body["target"])

    def test_command_carries_no_identity_or_cursor(self):
        """身份、目标与光标持有物**都不在命令里**。

        身份由引擎附在 C2S 原始字典上（服务端从 `session.player_id` 取）、
        目标就是 `session.device_ref`（`_check_target` 每请求已核一次）、
        `cursor` 一旦出现服务端的 `_cursor_refusal` 就会拦——R12 尚未定案，
        不发是「没有这只手」，不是「声称手上没东西」。
        `TerminalCommand` 的类文档逐条记了理由。
        """
        self.open_a_session()
        _, data = self.command("inv_take")
        body = data["message"]
        for absent in ("player_id", "player", "device_ref", "cursor", "op_id", "__id__"):
            self.assertNotIn(absent, body)

    def test_a_command_needs_a_session(self):
        """没有会话就当场报错，而不是发一条无会话的命令出去等回包。"""
        with self.assertRaises(ValueError):
            self.client.send_command("inv_take")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.client.pending_commands(), 0)

    def test_the_command_stays_in_flight_until_its_result_arrives(self):
        self.open_a_session()
        request_id, _ = self.command("inv_take")
        self.assertEqual(self.client.pending_commands(), 1)
        self.feed(self.result_of(request_id))
        self.assertEqual(self.client.pending_commands(), 0)
        self.assertEqual(self.client.pending_results(), 1)

    def test_taking_results_drains_them(self):
        self.open_a_session()
        request_id, _ = self.command("inv_take")
        self.feed(self.result_of(request_id))
        taken = self.client.take_command_results()
        self.assertEqual(len(taken), 1)
        self.assertEqual(taken[0].request_id, request_id)
        #: 取走即出队：留着会让界面把同一条回执报两遍。
        self.assertEqual(self.client.take_command_results(), [])
        self.assertEqual(self.client.pending_results(), 0)

    def test_taking_a_bounded_number_of_results_leaves_the_rest(self):
        self.open_a_session()
        first, _ = self.command("inv_take")
        second, _ = self.command("inv_put")
        self.feed(self.result_of(first))
        self.feed(self.result_of(second))
        self.assertEqual(len(self.client.take_command_results(1)), 1)
        self.assertEqual(self.client.pending_results(), 1)

    def test_a_result_for_a_command_we_never_sent_is_counted(self):
        """回执对不上**不关会话、不回错误**：一条对不上的回执不构成会话级
        故障，为它关掉玩家的界面只会更糟。计数是留给诊断的。"""
        self.open_a_session()
        self.feed(self.result_of("c99"))
        self.assertEqual(self.client.stats["unknown_results"], 1)
        self.assertEqual(self.client.stats["stale_results"], 0)
        self.assertEqual(self.client.session_id, "s1")
        #: 仍然取得到：界面拿它至少能说「有条命令的结果对不上」。
        self.assertEqual(self.client.pending_results(), 1)

    def test_a_result_for_a_wiped_ledger_is_counted_as_stale(self):
        """发过命令、账本却被会话清掉 → 迟到或重复的回执。

        与「我们压根没发过」是两种故障，两个计数器分开就是为了让它们
        在诊断里长得不一样（见 `_absorb_command_result`）。
        """
        self.open_a_session()
        self.command("inv_take")
        self.client.drop_session()
        self.assertEqual(self.client.pending_commands(), 0)
        self.open_a_session()
        self.feed(self.result_of("c1"))
        self.assertEqual(self.client.stats["stale_results"], 1)
        self.assertEqual(self.client.stats["unknown_results"], 0)

    def test_a_command_result_does_not_advance_the_item_view_version(self):
        """回执说的是「这条命令成没成」，**不是**「本地已经是最新的」。

        拿它去推视图版本，等于凭「命令成功」推断库存视图已经追上——而回执
        完全可能先于增量到达。`item_delta` 同样不推（同一条纪律）。
        """
        self.open_a_session(index_version=7, item_view_version=9)
        request_id, _ = self.command("inv_take")
        self.feed(self.result_of(request_id))
        self.assertEqual(self.client.item_view_version, 9)

    def test_drop_session_clears_the_ledgers_without_sending(self):
        """`drop_session` 给「服务端已判死这个会话」用：再发 `session_close`
        只会换来又一条没人看的错误。"""
        self.open_a_session()
        self.command("inv_take")
        sent_before = len(self.sent)
        self.client.drop_session()
        self.assertEqual(len(self.sent), sent_before)
        self.assertEqual(self.client.pending_commands(), 0)
        self.assertEqual(self.client.pending_results(), 0)
        self.assertFalse(self.client.has_session())
        self.assertIsNone(self.client.device_ref())

    def test_close_session_also_clears_the_command_ledgers(self):
        """在途表按会话算：留着它，下个会话的「有没有命令没回来」会一直说有。"""
        self.open_a_session()
        self.command("inv_take")
        self.client.close_session()
        self.assertEqual(self.client.pending_commands(), 0)
        self.assertEqual(self.client.pending_results(), 0)

    def test_a_bad_result_shape_is_a_bad_packet_and_keeps_the_command_in_flight(self):
        """形状不对的回执**整条拒收**，在途记录因此留在表里。

        这是有意的方向：把一条读不懂的回执当成「这条命令答过了」，会让
        界面以为命令已经结束，而实际发生什么完全不知道。
        """
        self.open_a_session()
        request_id, _ = self.command("inv_take")
        data = self.envelope(self.result_of(request_id)).to_dict()
        data["message"]["result"]["status"] = "no_such_status"
        self.assertIsNone(self.client.receive(data))
        self.assertEqual(self.client.stats["bad_packets"], 1)
        self.assertEqual(self.client.pending_commands(), 1)
        self.assertEqual(self.client.pending_results(), 0)

    def test_diagnostics_reports_both_command_ledgers(self):
        self.open_a_session()
        request_id, _ = self.command("inv_take")
        self.feed(self.result_of(request_id))
        report = self.client.diagnostics()
        self.assertEqual(report["in_flight"], 0)
        self.assertEqual(report["results"], 1)


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

    def test_an_error_without_a_session_means_the_session_is_gone(self):
        """**空会话字段的 `error` = 「那个会话已经没了」，客户端据此丢掉它。**

        2026-09-30 起这是**结构性判据**（见服务端 `_error` 的说明）。丢掉
        之后遮罩会出（`is_unavailable` 判的就是「有没有会话」）、库存快照
        作废、在途命令表清空——原先这几样一样都不会发生：`error` 到了客户端
        **没有任何人看它**，于是会话一直「活着」，界面看起来正常却什么都做
        不了，连选中框都赖着不走。

        **丢掉 ≠ 失忆。** 这里原先钉的是反过来的口径（「空字段不覆盖已有
        值」）。两者的差别只在「空会话字段意味着什么」：现在服务端**只在
        会话真的没了的时候**才留空，所以留着那份本地状态才是失忆——它记的
        是一个服务端已经不认的会话。
        """
        self.open_a_session()
        payload = SyncEnvelope(SyncError("req-1", "session_invalid")).to_dict()
        envelope = self.client.receive(payload)
        self.assertIsNotNone(envelope)
        self.assertEqual(self.client.session_id, "")
        self.assertEqual(self.client.epoch, "")
        self.assertEqual(self.client.stats["bad_packets"], 0)
        self.assertEqual(self.client.stats["errors"], 1)
        self.assertEqual(self.client.stats["sessions_dropped"], 1)

    def test_an_error_naming_a_live_session_keeps_it(self):
        """**带会话 id 的 `error` = 「这条请求自己失败了」，不是会话级故障。**

        服务端执行命令时抛异常走的就是这一支（`_error(session.session_id,
        ...)`，`server/sync/terminal.py`）。把它当成会话判死，等于「服务端
        一次内部错误 = 玩家界面掉线」——比它要修的那个毛病更糟。
        """
        self.open_a_session()
        payload = SyncEnvelope(
            SyncError("req-1", "internal"),
            session_id="s1", network_id="n1", epoch="e1",
        ).to_dict()
        envelope = self.client.receive(payload)
        self.assertIsNotNone(envelope)
        self.assertEqual(self.client.session_id, "s1")
        self.assertEqual(self.client.epoch, "e1")
        self.assertEqual(self.client.stats["errors"], 1)
        self.assertEqual(self.client.stats["sessions_dropped"], 0)

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

    def test_diagnostics_reports_whether_there_is_a_description_table(self):
        """没有表与有一张空的表**是两种故障**，`bad_packets` 分不出来。"""
        self.assertIsNone(self.client.diagnostics()["descriptions"])
        self.open_a_session()
        self.assertEqual(self.client.diagnostics()["descriptions"], 0)

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
                "commands",
                "describe_tables",
                "dropped",
                "errors",
                "foreign_packets",
                "handler_errors",
                "received",
                "requests",
                "sessions_closed",
                "sessions_dropped",
                "sessions_opened",
                "stale_results",
                "unknown_results",
            ],
        )


class TestNetworkClientDescriptions(NetworkClientFixture):
    """客户端自己那张资源描述表（`resource_identity.md` §3）。

    **这一条是真机上先炸出来的。** 服务端带描述表编码时，一个序号只在
    首次发送时附带描述，之后只发 `{epoch, serial}`。客户端不持表的话，
    快照分片与增量**两个包里每一条都解不出来**——而 `session_ready` /
    `snapshot_begin` / `snapshot_end` 不带引用，照常通过。真机上的症状是
    「会话开得起来、快照开始也收得到、内容一条没有」，`bad_packets` 缓慢
    上涨。离线用例原先全用 `to_dict()`（不带描述表）编码，走的正是
    `resource_key` 那条不需要表的路径，因此整条引用路径没有用例覆盖。
    """

    def wire(self, message, server, **kwargs):
        """按**服务端真实的编码方式**（带描述表）产生线上字典。"""
        return self.envelope(message, **kwargs).to_dict(server)

    def server_table(self, epoch="e1"):
        return ResourceDescriptions("n1", epoch)

    def test_a_chunk_carrying_references_is_decoded_not_counted_as_bad(self):
        server = self.server_table()
        stone = ResourceKey.item("minecraft:stone")
        iron = ResourceKey.item("minecraft:iron_ingot")
        # 先把序号发出去（真机上就是这一条 `session_ready`），客户端才有表。
        self.open_a_session()
        payload = self.wire(
            SnapshotChunk("s1", 0, ((stone, 4, 0), (iron, 3, 0))), server
        )
        envelope = self.client.receive(payload)
        self.assertIsNotNone(envelope, "带引用的分片被判成了坏包")
        self.assertEqual(self.client.stats["bad_packets"], 0)
        self.assertEqual(
            [(key.name, stored) for key, stored, _ in envelope.message.items],
            [("minecraft:stone", 4), ("minecraft:iron_ingot", 3)],
        )
        self.assertEqual(self.client.diagnostics()["descriptions"], 2)

    def test_only_the_first_send_of_a_serial_carries_the_description(self):
        """第二次发同一个序号只有 `{epoch, serial}`——所以表必须留着。

        这条钉的是**缺陷的成因**：若表没留（或每条都重新建），第二个分片
        会因为「没有描述可读」被判坏包，而第一个好好的。顺序不能反：
        `encode` 本身就会登记序号，先探一次它的返回值就等于把「首次」
        用掉了，再发的第一个分片已经不带描述了。
        """
        server = self.server_table()
        stone = ResourceKey.item("minecraft:stone")
        self.open_a_session()
        self.assertIsNotNone(
            self.client.receive(self.wire(SnapshotChunk("s1", 0, ((stone, 4, 0),)), server))
        )
        # 序号已经发出去了，服务端这边再发同一个键就只剩引用。
        self.assertNotIn("description", server.encode(stone))
        self.assertIsNotNone(
            self.client.receive(self.wire(SnapshotChunk("s1", 1, ((stone, 4, 0),)), server))
        )
        self.assertEqual(self.client.stats["bad_packets"], 0)
        self.assertEqual(self.client.stats["describe_tables"], 1)

    def test_a_new_session_gets_a_new_table(self):
        """重开会话时服务端会换一张表（序号从 1 重新分配），客户端必须跟上。

        跟不上就是最坏的一种：两边的 1 号指着不同的资源，谁都不报错。
        客户端认得的仍然是上一会话的键，直到序号重绑检查把这一条判成坏包。

        **换会话的真实路径是先关掉旧的**（面板每次打开都是一份新的
        `NetworkClient`），所以这里先 `close_session()` 把号清掉再收新会话。
        持着旧号直接收另一个号的 `session_ready` 是**另一台终端**的流，
        现在会被丢掉——见 `test_a_foreign_session_does_not_steal_our_state`。
        """
        first = self.server_table()
        stone = ResourceKey.item("minecraft:stone")
        self.open_a_session()
        self.assertIsNotNone(
            self.client.receive(self.wire(SnapshotChunk("s1", 0, ((stone, 4, 0),)), first))
        )
        self.client.close_session()
        self.assertFalse(self.client.has_session())
        # 新会话：同一网络、同一 epoch，但服务端的表是新的。
        self.feed(SessionReady(4, 1), session_id="s2")
        second = self.server_table()
        gold = ResourceKey.item("minecraft:gold_ingot")
        envelope = self.client.receive(
            self.wire(SnapshotChunk("s2", 0, ((gold, 2, 0),)), second, session_id="s2")
        )
        self.assertIsNotNone(envelope, "新会话沿用了旧表，序号被解成了别的资源")
        self.assertEqual(envelope.message.items[0][0].name, "minecraft:gold_ingot")
        self.assertEqual(self.client.stats["describe_tables"], 2)

    def test_a_foreign_session_does_not_steal_our_state(self):
        """**并存会话**：别人会话的包不进描述表、不改本地状态、不入队。

        2026-10-01 真机实测撞出来的那条。关掉终端再打开，旧会话在
        `session_ttl_frames` 到期前还活着还在推，而引擎按玩家派发，两条流
        交替到达。旧实现里每来一条别人的包就把整张描述表换成空的，于是
        **本会话自己的分片与增量跟着全解不出来**——快照判截断 → 重取 →
        再被打断，滚成 3210 次请求的循环，界面上「存进去的东西永远不出现」。

        这里钉三件事：别人的包不进表（`describe_tables` 不涨）、
        `bad_packets` 不动（它是**别人的包**，不是坏包——两者混在一起就
        分不清「协议对不上」与「并存会话」）、以及本会话的包照样解得出来。
        """
        ours = self.server_table()
        stone = ResourceKey.item("minecraft:stone")
        self.open_a_session()
        self.assertEqual(self.client.session_id, "s1")

        # 另一个会话（同一玩家、同一网络）的分片：它有自己的表。
        theirs = self.server_table()
        gold = ResourceKey.item("minecraft:gold_ingot")
        queued = self.client.pending_count()   # 本会话那条 `session_ready`
        self.assertIsNone(
            self.client.receive(
                self.wire(SnapshotChunk("s9", 0, ((gold, 2, 0),)), theirs,
                          session_id="s9")
            )
        )
        self.assertEqual(self.client.stats["foreign_packets"], 1)
        self.assertEqual(self.client.stats["describe_tables"], 1)
        self.assertEqual(self.client.stats["bad_packets"], 0)
        self.assertEqual(self.client.session_id, "s1", "别人的包把会话号顶掉了")
        self.assertEqual(self.client.pending_count(), queued, "别人的包进了队列")

        # 本会话的包**没有**被牵连。
        envelope = self.client.receive(
            self.wire(SnapshotChunk("s1", 0, ((stone, 4, 0),)), ours)
        )
        self.assertIsNotNone(envelope)
        self.assertEqual(envelope.message.items[0][0].name, "minecraft:stone")
        self.assertEqual(self.client.stats["describe_tables"], 1)

    def test_a_rotated_epoch_gets_a_new_table(self):
        """epoch 是网络代次，轮换即「服务端那张表作废」。"""
        self.feed(SessionReady(1, 1))
        self.assertEqual(self.client.stats["describe_tables"], 1)
        self.feed(SessionReady(1, 1), epoch="e2")
        self.assertEqual(self.client.stats["describe_tables"], 2)

    def test_an_error_envelope_does_not_build_a_table(self):
        """`error` 的会话字段是空的，拿它建表只会让后续每条引用都判旧 epoch。"""
        self.open_a_session()
        self.client.receive(SyncEnvelope(SyncError("r1", "no_session")).to_dict())
        self.assertEqual(self.client.stats["describe_tables"], 1)
        self.assertEqual(self.client.stats["bad_packets"], 0)

    def test_closing_the_session_drops_the_table(self):
        self.open_a_session()
        self.assertEqual(self.client.diagnostics()["descriptions"], 0)
        self.client.close_session()
        self.assertIsNone(self.client.diagnostics()["descriptions"])


if __name__ == "__main__":
    unittest.main()
