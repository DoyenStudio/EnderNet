# coding=utf-8
"""终端同步通道的客户端侧（任务 11-01 / 11-02 的客户端一半）。

与 `server/adapters/messages.py` 是**同一条协议的两端**，因此那三条纪律在
这里逐条对称：

1. **事件类在函数体里建**（`build_event_classes`）。模块顶层
   `class X(CustomS2CEvent)` 得先拿到 `tooldelta.events`，而取它只有两条
   路，两条都断：绝对写 `from tooldelta import events` 在真机上必
   `ImportError`（脚本目录被整体当包挂载，`tooldelta` 就在包内，见
   `test_import_boundaries.py`），相对写 `from ...tooldelta import events`
   在离线模块名下又是**越界**的层级（离线名少两层）。于是只剩函数体。
   本机实测还多一条：`import tooldelta` 在这里同样是失败的，失败点是
   `tooldelta/events/client/control.py` 上那条模块级的
   `from mod.client.extraClientApi import ...`，撞上工具链装在
   site-packages 里那份 `mod` 桩自身的循环导入——**这个 vendored 包本来
   就不能在没游戏的环境里整体导入**，函数体导入不是保守起见，是必需的。
   代价是「事件类名字对不对」只有真机看得见，因此留了同一个口子给
   `endernet_tests/test_sync_channels.py` 跑**同一份**类定义。
2. **只走 MC 客户端总线**（`GetMCClientEventBus`）。`CustomS2CEvent.Listen`
   那一支走 `GetModClientEventBus()`，本项目禁用；同一条纪律由
   `test_project_uses_only_the_mc_event_bus` 守着。
3. **事件类各建一次**（`ClientEventPort.classes()`）。`EventBus` 拿类对象
   本身当字典键，两次建类就是两张表，注销只摘得掉一张。

**客户端为什么必须自己定义一份事件类**：`client/` 不允许 import `server/`
（分层由 `test_import_boundaries.py` 静态强制），而两个事件类的 `name` 必须
与 `common/protocol.py` 的两个常量**逐字相同**——不同就是「什么都收不到」，
不报错、不刷屏。防漂移只有一条离线可跑的路：两边都用常量，再由
`test_sync_channels.py` 断言**两侧的类定义**都等于常量。

**身份方向的不对称，是有依据的。** `__id__`（发送者是谁）是引擎在
**C2S 方向**附的：服务端拿到的是「原始字典 + `__id__`」，因此那边必须
在解析前把它摘掉（`ENGINE_ADDED_FIELDS`）。S2C 方向引擎没有发送者身份
可附，客户端收到的就是服务端 `to_dict()` 出去的那一份，所以这里**不摘
任何字段**。若真机上 S2C 也带上附加字段，症状是**每一条都判成坏包**
（`stats["bad_packets"]` 与 `SyncEnvelope.from_dict` 的未知字段报错），
第一件事是比对服务端发出去的字典与客户端收到的字典差在哪几个键上。

**这里没有 `send_command`。** 命令通道的消息形状尚未确定：`common/protocol.py`
的 `CLIENT_TO_SERVER_KINDS` 只有四条会话/快照消息，而任务文档要求命令
「含请求 ID、动作和期望版本」——那个信封属于 11-11（数量命令）与 R12
（手势映射，尚未定稿）的范围。在这里编一个没有对端的频道，等于把未定
玩法悄悄定案，也正是 `plans/tasks/11` 第 21 行禁止的「用占位绕过前置
条件」。交付说明里记为**未写**，不记为「已实现待接线」。
"""

from ..common.protocol import (
    MSG_SESSION_READY,
    MSG_SNAPSHOT_BEGIN,
    SYNC_CHANNEL_C2S,
    SYNC_CHANNEL_S2C,
    VIEW_ITEM,
    Ack,
    SessionClose,
    SessionOpen,
    SnapshotRequest,
    SyncEnvelope,
)

#: 收件队列的默认上限。**待定值**：任务 11 页尾写明「请求大小、会话过期和
#: 分页上限由目标环境测量后确定」，这一条属于同类。丢弃旧包不会静默出错：
#: 增量批带 `base_version`，缺了一批之后下一批的基准就对不上，模型会抛
#: `ListResyncRequired` 去重取快照（见 `client/ui/item_list_model.py`）。
#: 也就是说这个上限**只影响要重取多少，不影响正确性**——正因为这样，
#: 现在给一个未经测量的数字是可以接受的，而它仍然标着「待定」。
DEFAULT_QUEUE_LIMIT = 256


def engine_events():
    # type: () -> object
    """引擎的 `tooldelta.events` 模块。**导入留在函数体内**（见模块 docstring）。

    它是本模块唯一的引擎依赖入口，因此也是**离线测试唯一的注入点**。
    """
    from ...tooldelta import events as events_module

    return events_module


def engine_client_bus():
    # type: () -> object
    """MC 客户端事件总线。导入同样留在函数体内。

    **不复用 `CustomS2CEvent.Listen`**：那一支取的是模组客户端总线
    （`GetModClientEventBus`），本项目统一走 MC 总线（理由见
    `server/adapters/events.py` 的 `ToolDeltaEventApi`）。
    """
    from ...tooldelta.events.event_bus import GetMCClientEventBus

    return GetMCClientEventBus()


def build_event_classes(events_module):
    # type: (object) -> tuple
    """以 `events_module` 为基类来源，构造 `(C2S, S2C)` 两个事件类。

    与 `server/adapters/messages.py` 的 `build_event_classes` **方向相反**，
    同一份类定义供 `test_sync_channels.py` 离线核对：

    - `ClientRequestEvent`（C2S）：客户端**发**。实现 `marshal()`——
      `CustomC2SEvent.send()` 走 `NotifyToServer(name, marshal())`；
    - `ClientSyncEvent`（S2C）：客户端**收**。实现 `unmarshal(data)`——
      总线把引擎送来的字典交给它。

    每个类只实现自己真正要走的那一半，另一半留给基类抛
    `NotImplementedError`：真有人反着用时，那比「发出去一个空包」好查。
    """

    class ClientRequestEvent(events_module.CustomC2SEvent):
        """客户端 → 服务端的一条终端请求。"""

        name = SYNC_CHANNEL_C2S

        def __init__(self, payload=None):
            #: 已经编码好的线上字典（`SyncEnvelope.to_dict()` 的结果）。
            #: **不在这里解析**：解析规则在协议层，两侧共用同一份。
            self.payload = payload if isinstance(payload, dict) else {}

        def marshal(self):
            # type: () -> dict
            return self.payload

    class ClientSyncEvent(events_module.CustomS2CEvent):
        """服务端 → 客户端的一条终端同步消息。"""

        name = SYNC_CHANNEL_S2C

        def __init__(self, payload=None):
            #: 引擎送来的原始字典，**原样带着**：解析在 `NetworkClient.receive`，
            #: 那里才有「坏包怎么计数」的上下文。
            self.payload = payload if isinstance(payload, dict) else {}

        @classmethod
        def unmarshal(cls, data):
            return cls(data)

    return ClientRequestEvent, ClientSyncEvent


class ClientEventPort(object):
    """客户端同步通道的引擎侧实现。**这是 `NetworkClient` 唯一的引擎出口。**

    对外只有四件事：`listen` / `unlisten` / `send` / `classes`，另加
    `bus`（惰性取总线，供离线注入替身）。

    **可以脱离引擎构造**：`__init__` 不 import 任何东西，引擎 import 全在
    方法体内——于是 `client/bootstrap.py` 在不进游戏时也能把它建出来，
    而离线测试能把注册、注销、发送三条路径都跑通。
    """

    def __init__(self, events=None, bus=None):
        # type: (object, object) -> None
        #: 引擎 `events` 模块的解析器，默认 `engine_events`。
        self.events = events if events is not None else engine_events
        #: 事件总线。默认 `engine_client_bus`；注入替身即可离线驱动注册/注销。
        self._bus = bus
        #: 两个事件类，**每个端口只建一次**（理由见模块 docstring 第 3 条）。
        #: 惰性构造：`__init__` 里建就等于在这里 import 引擎。
        self._event_classes = None
        #: 已注册的 `(事件类, 派发函数, 优先级)`，供 `unlisten` 逐一注销。
        self._handles = []
        self.stats = {
            "received": 0,
            "sent": 0,
            "handler_errors": 0,
            "duplicate_listen": 0,
        }

    def classes(self):
        # type: () -> tuple
        """本端口用的 `(C2S, S2C)` 两个事件类，**只建一次**。"""
        if self._event_classes is None:
            self._event_classes = build_event_classes(self.events())
        return self._event_classes

    def bus(self):
        # type: () -> object
        """事件总线。默认取引擎实现，注入替身即可离线驱动。"""
        if self._bus is None:
            self._bus = engine_client_bus()
        return self._bus

    # -- 注册 ----------------------------------------------------------

    def listen(self, handler, priority=0):
        # type: (object, int) -> int
        """注册 S2C 接收侧，返回这次注册的监听器个数。

        **重复注册是被拒的，不是被允许的**：注册两次会让同一个包被处理
        两次（两套会话状态各改一遍），而症状是「有时多一次」。这里拒绝并
        计数（`stats["duplicate_listen"]`），因为它在组合根上就是一个错误，
        只是不值得让模组启动失败。
        """
        if self._handles:
            self.stats["duplicate_listen"] += 1
            return 0
        _, s2c = self.classes()

        def dispatch(event):
            self.stats["received"] += 1
            try:
                return handler(event)
            except Exception:
                # 回调抛出去会一路回到引擎。**记下并继续**：一个坏包不该让
                # 后续所有包都不再被处理（`ServerBootstrap.tick()` 同一条纪律）。
                self.stats["handler_errors"] += 1
                return None

        # 不带 `static=True`：`dispatch` 是每次调用新建的闭包，那个去重标记
        # 不会命中；重复注册本来就由上面的 `self._handles` 挡掉。
        self.bus().ListenEvent(s2c, priority)(dispatch)
        self._handles.append((s2c, dispatch, priority))
        return 1

    def unlisten(self):
        # type: () -> int
        """注销全部监听器，返回注销掉的个数。

        **必须真的注销**：只清掉本地引用不会让回调消失，监听器注册在
        进程级的表里。注意上游的 `EventBus._dyn_unlisten` **只从回调表里
        摘掉 listener**，没有再调 `UnListenForEvent`（`_del_sys_event_listener`
        无人调用）——因此引擎那一侧的包装处理器还在，但它遍历的列表已经空了，
        不会再有回调发生。这个结论是读上游源码得出的，**未在真机上验证**；
        若将来发现关掉界面后仍在收包，这里是第一个要看的地方。
        """
        count = 0
        for event_cls, dispatch, priority in self._handles:
            self.bus().RemoveEventListener(event_cls, dispatch, priority)
            count += 1
        self._handles = []
        return count

    def listening(self):
        # type: () -> bool
        return bool(self._handles)

    # -- 发送 ----------------------------------------------------------

    def send(self, payload):
        # type: (dict) -> bool
        """把一条**已经编码好**的 C2S 消息发往服务端。

        **没有收件人参数**：客户端的对端恒为服务端，引擎自己知道是谁在发
        （它正是 `__id__` 的来源）。因此这里没有服务端 `send(player_id, ...)`
        那个「有没有收件人」的判断，返回值只表示发过了。

        **引擎抛的异常不在这里吞**：让它冒到 `NetworkClient` 的调用点上。
        在这里吞掉的话，「引擎发不出去」会静默变成「发了」，而调用方会
        继续等一个永远不会来的回包。
        """
        c2s, _ = self.classes()
        c2s(payload).send()
        self.stats["sent"] += 1
        return True

    def diagnostics(self):
        # type: () -> dict
        return {
            "stats": dict(self.stats),
            "channels": [SYNC_CHANNEL_C2S, SYNC_CHANNEL_S2C],
            "listening": self.listening(),
        }


class NetworkClient(object):
    """客户端侧的会话状态、请求编码与回包分发。

    **不持有任何权威状态**（架构规划 6.6）：这里记的版本、会话 id 与
    网络 id 都是**服务端告诉它的**，它不判断库存、订单或能量。列表内容
    归 `client/ui/item_list_model.py`，这里只把线上来的东西解出来交给它。
    """

    def __init__(self, port=None, view_kind=VIEW_ITEM, queue_limit=None):
        # type: (object, str, object) -> None
        #: 引擎侧端口。不给就现建一个（离线测试注入替身）。
        self.port = port if port is not None else ClientEventPort()
        self.view_kind = view_kind
        self.queue_limit = (
            DEFAULT_QUEUE_LIMIT if queue_limit is None else queue_limit
        )
        #: 会话身份。**全部来自服务端的回包**，客户端不自己编：
        #: `session_open` 发出时还没有会话，`session_ready` 才给出 id。
        self.session_id = ""
        self.network_id = ""
        self.epoch = ""
        #: 存储索引版本——**唯一允许写进命令的版本**。
        self.index_version = 0
        #: 服务端最近一次告知的物品视图版本。**它不是本地已应用到的版本**，
        #: 因此 `request_snapshot` 不会拿它当缺省值（见那个方法的 docstring）。
        self.item_view_version = 0
        self._request_serial = 0
        self._queue = []
        self._handlers = []
        #: 本会话绑定的终端实例（`DeviceRef`），由 `open_session` 记下。
        self._device_ref = None
        self.stats = {
            "requests": 0,
            "received": 0,
            "bad_packets": 0,
            "dropped": 0,
            "handler_errors": 0,
            "sessions_opened": 0,
            "sessions_closed": 0,
        }

    # -- 会话 ----------------------------------------------------------

    def device_ref(self):
        # type: () -> object
        """本会话绑定的终端实例；没有会话时返回 None。"""
        return self._device_ref

    def open_session(self, device_ref, dimension):
        # type: (object, int) -> str
        """请求打开一个会话，返回本次请求的 `request_id`。

        `device_ref` 必须是 `DeviceRef`（`common/identity.py`，即 `NodeKey` +
        实例代次），`dimension` 是玩家当前维度。**两者都由界面层从它已经
        拿到的上下文里填**——这里不做任何查询，因为客户端查不到权威答案，
        而一个猜出来的维度只会让服务端拒掉这次请求（失败方向安全，但查起来
        毫无线索）。传错类型会立刻炸在 `device_ref.to_dict()` 上，不是静默
        发一个空位置出去。

        **客户端报的实例代次不被服务端信任**：服务端拿它去比对自己注册表里的
        当前代次（`DeviceRef.matches`），对不上就拒。所以界面拿着一个过期的
        代次来开会话，症状是**这次请求被拒**，而不是连到别人的终端上。

        玩家身份**不在消息里**：引擎会在发出去时附上发送者，服务端只认它
        （协议层因此连字段都没定义，见 `common/protocol.py` 的 `SessionOpen`）。
        """
        request_id = self._next_request_id()
        self._device_ref = device_ref
        self._send(
            SyncEnvelope(
                SessionOpen(request_id, device_ref, dimension),
                view_kind=self.view_kind,
            )
        )
        return request_id

    def close_session(self):
        # type: () -> object
        """主动关闭会话，返回 `request_id`；没有会话时返回 None 且不发包。

        **没有会话就不发**：服务端对「关一个不存在的会话」只会回一条错误，
        而它的触发场景（界面关闭时网络本来就断了）恰恰是错误码会被忽略的
        时候。本地状态**无条件清掉**——界面已经关了，留着 session_id 只会
        让下一次会话复用到一个已经作废的 id。
        """
        request_id = None
        if self.session_id:
            request_id = self._next_request_id()
            self._send(
                SyncEnvelope(
                    SessionClose(request_id),
                    session_id=self.session_id,
                    network_id=self.network_id,
                    epoch=self.epoch,
                    view_kind=self.view_kind,
                )
            )
            self.stats["sessions_closed"] += 1
        self._forget_session()
        return request_id

    def _require_session(self, what):
        # type: (str) -> None
        """会话范围内的请求必须先有会话，否则**当场报错**。

        `close_session` 是唯一不适用的一条：界面关闭时网络可能本来就断了，
        「没得关」是个正常状态，那里不发包也不报错。而「取快照 / 确认传输」
        在没有会话时不是正常状态——界面不该做得到，做了就是调用点的错误。
        不自己判的话，症状取决于 `settings.DEBUG`：开着是协议层的
        「session_id 必须是非空文本」（离原因隔一层），关着就是发一条
        没有会话的请求出去，等一个永远不会来的回包。
        """
        if not self.session_id:
            raise ValueError("还没有会话，不能%s" % (what,))

    def _forget_session(self):
        # type: () -> None
        self.session_id = ""
        self.network_id = ""
        self.epoch = ""
        self._device_ref = None

    def has_session(self):
        # type: () -> bool
        return bool(self.session_id)

    # -- 请求 ----------------------------------------------------------

    def request_snapshot(self, from_version):
        # type: (int) -> str
        """请求（重新）取一份快照，返回 `request_id`。

        **`from_version` 是必填的**，缺省值刻意不给：这里记的
        `item_view_version` 是「服务端最近一次告知的版本」，而服务端要的是
        「本地**已应用**到的版本」。两者在增量批收了一半时并不相等，而用错的
        后果是服务端按一个客户端并没有的版本去补增量——补出来的表看着正常，
        内容却缺了那半批。因此调用方必须显式写出来源，通常是
        `model.version()`（`client/ui/item_list_model.py`）。
        """
        self._require_session("取快照")
        request_id = self._next_request_id()
        self._send(
            SyncEnvelope(
                SnapshotRequest(request_id, from_version),
                session_id=self.session_id,
                network_id=self.network_id,
                epoch=self.epoch,
                view_kind=self.view_kind,
            )
        )
        return request_id

    def ack(self, transfer_id, sequence):
        # type: (str, int) -> str
        """确认某次传输里连续到达的最大包序号，返回 `request_id`。

        确认是**推进服务端发送窗口的唯一手段**：不发它，服务端会一直等在
        上一次的游标上（直到退避与重取的兜底生效）。因此这个方法在
        「收下且已应用」之后就应当调用，而不是等到整批齐了才一起发——
        慢客户端本来就靠它把窗口一点点推上去。
        """
        self._require_session("确认传输")
        request_id = self._next_request_id()
        self._send(
            SyncEnvelope(
                Ack(request_id, transfer_id, sequence),
                session_id=self.session_id,
                network_id=self.network_id,
                epoch=self.epoch,
                view_kind=self.view_kind,
            )
        )
        return request_id

    def _next_request_id(self):
        # type: () -> str
        """请求 id。**单调递增且带前缀**：服务端按它去重（`DeviceCommand`
        也要求请求 id），而重复的 id 会让「重发」与「新请求」分不开。
        """
        self._request_serial += 1
        self.stats["requests"] += 1
        return "c%d" % (self._request_serial,)

    def _send(self, envelope):
        # type: (SyncEnvelope) -> None
        self.port.send(envelope.to_dict())

    # -- 接收 ----------------------------------------------------------

    def on_message(self, handler):
        # type: (object) -> int
        """登记一个**即时**回调：`handler(envelope, message)`。

        与 `take_events` 并存，因为两者的时机要求不同：`session_ready` /
        `error` 决定「这个界面能不能开」，等一帧没有意义；而列表内容的
        刷新本来就该按帧合并（一批增量拆成十几个包，逐包重画等于画十几遍）。
        """
        self._handlers.append(handler)
        return len(self._handlers)

    def clear_handlers(self):
        # type: () -> int
        """注销全部即时回调，返回个数。界面关闭时必须调它——
        否则下一个界面会被上一个界面的回调一起处理。"""
        count = len(self._handlers)
        self._handlers = []
        return count

    def receive(self, value):
        # type: (object) -> object
        """收一条 S2C 数据（事件实例或原始字典），返回解析出的 `SyncEnvelope`。

        **坏包只计数、不抛出**：`value` 来自网络，任何一条都可能坏，而这里
        是它唯一的入口。抛出去只会让回调在引擎里报错（`ClientEventPort` 的
        `handler_errors`），而那条路查不出「坏在哪」——这里返回 None，并在
        `stats["bad_packets"]` 上留下计数。

        **状态只在解析成功之后才改**：一次失败不能留下半份会话状态，
        否则界面会拿着一个半新半旧的会话去发请求。
        """
        data = _payload_of(value)
        if data is None:
            self.stats["bad_packets"] += 1
            return None
        try:
            envelope = SyncEnvelope.from_dict(data)
        except Exception:
            self.stats["bad_packets"] += 1
            return None
        self.stats["received"] += 1
        self._absorb(envelope)
        self._enqueue(envelope)
        # **逐个回调各自接住异常。** 一个回调炸了不能让后面的回调收不到这
        # 一条——包已经解析完、状态已经更新、也已经入队了，此时中断只让
        # 「一部分订阅者知道、另一部分不知道」，而那种不一致查起来毫无线索。
        # 注意这里**不往上抛**，因此端口的 `handler_errors` 看不到它：两个
        # 计数器量的不是一件事（端口那个数的是「派发本身炸了」，这个数的是
        # 「某一条消息的回调炸了」）。
        for handler in list(self._handlers):
            try:
                handler(envelope, envelope.message)
            except Exception:
                self.stats["handler_errors"] += 1
        return envelope

    def _absorb(self, envelope):
        # type: (SyncEnvelope) -> None
        """把回包里的路由字段与版本记下来。

        **空字段不覆盖已有值**：`error` 是唯一允许空 `session_id`/`epoch` 的
        S2C 消息（会话根本不存在时服务端没有可填的），用它去覆盖会让一个
        本来好好的会话在收到一条错误之后自己失忆。
        """
        if envelope.session_id:
            self.session_id = envelope.session_id
        if envelope.network_id:
            self.network_id = envelope.network_id
        if envelope.epoch:
            self.epoch = envelope.epoch
        message = envelope.message
        if message.kind == MSG_SESSION_READY:
            self.index_version = message.index_version
            self.item_view_version = message.item_view_version
            self.stats["sessions_opened"] += 1
        elif message.kind == MSG_SNAPSHOT_BEGIN:
            self.item_view_version = message.item_view_version
        # **`item_delta` 刻意不在这里推进 `item_view_version`**：一个窗口可能
        # 拆成多包，收到最后一包也不代表已经应用（还差模型那边合并）。真正
        # 应用与否只有列表模型判得了（`begin_batch` 的基准比对），因此这里
        # 记的始终是**服务端告知**的进度，`request_snapshot` 不拿它当依据
        # （那个方法的 `from_version` 是必填的）。

    def _enqueue(self, envelope):
        # type: (SyncEnvelope) -> None
        """入队，超出上限时丢**最旧**的一条并计数。

        丢最旧而不是丢最新：最新的那条决定「界面现在该显示什么」，丢掉它
        等于把界面停在过去。丢掉的后果是版本链断掉，而那是**可发现**的
        （模型抛 `ListResyncRequired` 去重取快照），不是静默错误。
        """
        self._queue.append(envelope)
        while len(self._queue) > self.queue_limit:
            self._queue.pop(0)
            self.stats["dropped"] += 1

    def take_events(self, limit=None):
        # type: (object) -> list
        """取走最多 `limit` 条待处理的回包（不传则全取），返回 `SyncEnvelope` 列表。

        **取走即出队**：留着它们会让下一帧把同一批增量再应用一遍，而模型
        的基准版本比对会把重复应用判成断档（多报一次 resync，功能还能用，
        但每次刷新都白重取一份快照）。
        """
        if limit is None or limit >= len(self._queue):
            taken = self._queue
            self._queue = []
            return taken
        taken = self._queue[:limit]
        self._queue = self._queue[limit:]
        return taken

    def pending_count(self):
        # type: () -> int
        return len(self._queue)

    def diagnostics(self):
        # type: () -> dict
        """诊断。**不报任何一个版本的「差值」**：两个版本偶尔相等（没有
        提交发生过），据此推断「它们是不是同一个东西」会得出错误结论——
        它们各自具名，只是因为分工不同（见 `common/protocol.py` 本节开头）。
        """
        return {
            "session_id": self.session_id,
            "network_id": self.network_id,
            "epoch": self.epoch,
            "index_version": self.index_version,
            "item_view_version": self.item_view_version,
            "pending": self.pending_count(),
            "handlers": len(self._handlers),
            "stats": dict(self.stats),
            "port": self.port.diagnostics(),
        }


def _payload_of(value):
    # type: (object) -> object
    """从「事件实例」或「原始字典」里取出那个字典，取不到返回 None。

    两种形态都要认：组合根拿到的是引擎派发的事件实例（原始字典在
    `.payload` 上），而离线测试直接给字典。只认一种的话，另一种会静默
    退化成「每一条都是坏包」——界面什么都不显示，而计数说的却是
    `bad_packets`，离原因隔着好几层。
    """
    if isinstance(value, dict):
        return value
    return getattr(value, "payload", None)


def message_kind_of(envelope):
    # type: (object) -> str
    """便捷读取：包里的消息种类；不是包就返回空串。

    给界面层做分支用。**这里不重复「哪些种类属于哪个方向」**：那是
    `common/protocol.py` 的 `CLIENT_TO_SERVER_KINDS` / `SERVER_TO_CLIENT_KINDS`
    两份 frozenset 的事，在这里再列一份，将来新增一个消息种类就得记得改两处，
    而漏改的那一处不会报错、只会让新消息被判成反方向。
    """
    message = getattr(envelope, "message", None)
    if message is None:
        return ""
    return getattr(message, "kind", "")
