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
3. **事件类进程内各建一次**（`ClientEventPort.classes()` →
   `shared_event_classes`）。`EventBus` 拿类对象本身当字典键，两次建类就是
   两张表，注销只摘得掉一张。**这条纪律是实测出来的，不是保守起见**：本机
   实测（2026-09-29）每新建一个端口就多一次投递，第 4 个端口收到 4 份，
   而且只有最后注册的那个端口收得到、先前仍在监听中的端口一条都收不到。
   机理与症状见 `shared_event_classes` 的注释。

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

**命令通道已在 11-03 落地**（`send_command` + `command_result` 回执）。原先
这里写的是「没有 `send_command`，因为命令形状未定」——那个理由现在不成立了：
`common/protocol.py` 的 `TerminalCommand` / `CommandResultMessage` 把线格式
定了下来，服务端 `TerminalSyncHub._on_command` 也接了执行方。**仍未定的只剩
R12 的手势映射**（拖拽、长按、鼠标键位与组合键、光标持有物的退出规则），
因此这里只做「一次命令 = 一次点击」，不发明任何手势。

**在途记录只记不发。** `send_command` 把整条编好的信封留在 `self._in_flight`
里，但**本轮不重发**：重发的判据是「多久没回算超时」，而那个毫秒数是未定值
（全局约束 7）。留着它是为了让「重复点击与超时重试不重复扣库存」这条验收项
将来**有实现路径**——服务端 `controller.admit` 的去重账本按 `request_id` 记账，
重发**同一条**信封才命得中它，新编一条信封就绕过去了。不重发不等于可以随手
丢掉：`drop_session` 与 `_forget_session` 都清它，否则下一个会话的在途表会
攒着上一个会话的 id。
"""

from ..common.protocol import (
    MSG_COMMAND_RESULT,
    MSG_ERROR,
    MSG_SESSION_READY,
    MSG_INVENTORY_SNAPSHOT,
    MSG_SNAPSHOT_BEGIN,
    SYNC_CHANNEL_C2S,
    SYNC_CHANNEL_S2C,
    VIEW_ITEM,
    STATUS_REJECTED,
    Ack,
    SessionClose,
    SessionOpen,
    SnapshotRequest,
    SyncEnvelope,
    TerminalCommand,
)
from ..common.resource_sync import ResourceDescriptions

#: 收件队列的默认上限。**待定值**：任务 11 页尾写明「请求大小、会话过期和
#: 分页上限由目标环境测量后确定」，这一条属于同类。丢弃旧包不会静默出错：
#: 增量批带 `base_version`，缺了一批之后下一批的基准就对不上，模型会抛
#: `ListResyncRequired` 去重取快照（见 `client/ui/item_list_model.py`）。
#: 也就是说这个上限**只影响要重取多少，不影响正确性**——正因为这样，
#: 现在给一个未经测量的数字是可以接受的，而它仍然标着「待定」。
DEFAULT_QUEUE_LIMIT = 256


class _CommandOutcome(object):
    """`take_command_results()` 里一个元素的最小形状：只要一个 `.result`。

    正常回执是协议里的 `CommandResultMessage`。**服务端的拒绝（`error`）
    不是回执消息，但它是这一次请求的结局**——把它翻译成同一个形状，界面
    那边就只写一条收尾路径（`TerminalListView._settle_action`），不必为
    「错误」再分一支。分两支的代价是迟早有一支忘了清选中，而表现是
    「选中框赖着不走」，玩家下一次点击会拿一件他没选过的东西去操作。

    **它只给界面看，不送回协议层**：这里没有任何 `from_dict`，协议那边
    `SyncError` 的表达力也比这个字典强。
    """

    __slots__ = ("result",)

    def __init__(self, result):
        # type: (dict) -> None
        self.result = result


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


#: 进程内共享的事件类：`引擎 events 模块 -> (C2S, S2C)`。
#:
#: **必须进程内只建一份，不能「每个端口建一份」。** 本机实测（2026-09-29，
#: 4 个端口依次 `listen`）：同一条 S2C 消息被投递的次数等于**这个进程里
#: 建过的端口数**，第 4 个端口收到 4 份；并且只有**最后注册的那个端口**收得到，
#: 先前那些仍在监听中的端口一条都收不到。服务端一条只发一次（同一条消息的
#: 各份 `session_id` 完全相同），重复全部出在客户端这一侧。
#:
#: 机理在 vendored 包的两处（`tooldelta/events/event_bus/event_bus.py`）：
#:
#: - `_dyn_listen` 以**事件类对象**为键判重（`event not in
#:   self.event_listeners[priority]`），所以每新建一个类就是一次新的
#:   `_add_sys_event_listener` —— 引擎侧多一个监听器；
#: - `_add_sys_event_listener` 把处理器 `setattr` 到 system 对象上，名字是
#:   `tdevent_handler_<模块>.<类名><优先级>`。三个端口建出的三个类同模块、
#:   同类名、同优先级，于是**三个处理器同名**，`setattr` 只留得下最后那份；
#:   引擎按这个名字回调，就回调了最后那一份、且回调了注册次数那么多次。
#:
#: 注销救不了：`RemoveEventListener` 走 `_dyn_unlisten`，它只把回调从表里
#: 摘掉，**从不调 `UnListenForEvent`**（同文件里 `_del_sys_event_listener`
#: 无人调用），引擎侧的注册只增不减。也就是说「打开终端 → 关闭 → 再打开」
#: 每来一轮，重复投递就加一。
#:
#: 后果不是「多收一份、白算一遍」：同一批增量被应用两次，第二次的
#: `base_version` 已经对不上，模型会判成断档并重取快照——每开一次界面，
#: 每次刷新都白重取一份快照。
#:
#: 判据因此落在**类对象是不是同一份**上：类相同，`_dyn_listen` 的判重命中，
#: 引擎侧的注册不再增加，`listen` 只是往那张既有的表里追加回调（注销就把它
#: 摘掉），于是每个端口恰好收到一份。
#:
#: 键取**引擎 `events` 模块对象**而不是模块名：离线测试注入的是各自造的假
#: 模块，按名字缓存会让两个测试互相串台。用真模块当键不会泄漏——它是进程级
#: 单例；假模块由测试自己持有。
_EVENT_CLASSES = {}


def shared_event_classes(events_module):
    # type: (object) -> tuple
    """进程内共享的 `(C2S, S2C)` 事件类；同一个 `events_module` 只建一份。

    给 `ClientEventPort.classes()` 用。理由与实测见 `_EVENT_CLASSES` 的注释。
    这里**不缓存到 `build_event_classes` 里**：那个函数是离线测试直接调的
    （`test_sync_channels.py` 用它核对两侧的类定义），保持它是纯函数，
    测试才不必先清缓存。
    """
    cached = _EVENT_CLASSES.get(events_module)
    if cached is None:
        cached = build_event_classes(events_module)
        _EVENT_CLASSES[events_module] = cached
    return cached


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
        #: 两个事件类，**进程内只建一份**（理由见模块 docstring 第 3 条与
        #: `shared_event_classes`）。惰性构造：`__init__` 里建就等于在这里
        #: import 引擎。
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
        """本端口用的 `(C2S, S2C)` 两个事件类。**与其余端口共用同一份。**

        共用是硬要求而不是省内存：每个端口建一份的话，每建一个端口引擎侧就多
        一个监听器，同一条消息会被投递多次，且只有最后注册的端口收得到
        （机理与实测见 `shared_event_classes`）。
        """
        if self._event_classes is None:
            self._event_classes = shared_event_classes(self.events())
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


#: 请求 id 的**进程级**序号。
#:
#: **它不能放进 `NetworkClient` 实例。** 界面每打开一次就新建一个客户端，
#: 实例级计数器会让每个会话都从 `c1` 重新开始；而服务端的去重账本
#: （`network/controller.RequestLedger`）按 `(request_id, 来源设备实例)`
#: 记账、并保留 `LedgerPolicy.retention_ticks` 那么久。于是**下一个会话里
#: 序号相同的请求会命中上一个会话留下的那一条**，服务端回的是**重放结果**
#: （多带一条 `duplicate_request`），状态还是当初那个 `ok`——客户端照单
#: 全收，而东西一件没搬。
#:
#: 真机症状（2026-10-01，User 回报）：关掉终端再打开，同样两个槽位再交换
#: 一次，界面按本地虚拟空间画出了交换结果，**真实背包却纹丝不动**。两个
#: 会话的请求序列一样长时必然撞号，因此它是稳定的、不是偶发。
_REQUEST_SERIAL = 0


def next_request_id():
    # type: () -> str
    """请求 id。**单调递增且带前缀**：服务端按它去重（`DeviceCommand`
    也要求请求 id），而重复的 id 会让「重发」与「新请求」分不开。
    """
    global _REQUEST_SERIAL
    _REQUEST_SERIAL += 1
    return "c%d" % (_REQUEST_SERIAL,)


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
        self.inventory_slots = {}
        self.inventory_revision = -1
        self.network_id = ""
        self.epoch = ""
        #: 存储索引版本——**唯一允许写进命令的版本**。
        self.index_version = 0
        #: 服务端最近一次告知的物品视图版本。**它不是本地已应用到的版本**，
        #: 因此 `request_snapshot` 不会拿它当缺省值（见那个方法的 docstring）。
        self.item_view_version = 0
        self._queue = []
        self._handlers = []
        #: 本会话的资源描述表。**客户端必须自己持有一份**：服务端带描述表
        #: 编码时，一个序号只在**首次**发送时附带描述，之后只发
        #: `resource_ref = {epoch, serial}`（`resource_identity.md` §3）。
        #: 没有这张表，客户端解不出快照分片与增量里的任何条目——症状是
        #: 每一条都进 `bad_packets`，而 `session_ready`/`snapshot_begin`/
        #: `snapshot_end` 这类不带引用的消息照常通过，看起来像「只有内容丢了」。
        #: table 属于哪个会话记在 `_descriptions_of`，会话或 epoch 一变就换新的。
        self._descriptions = None
        self._descriptions_of = None
        #: 本会话绑定的终端实例（`DeviceRef`），由 `open_session` 记下。
        self._device_ref = None
        #: 已发出、还没拿到回执的命令：`request_id -> SyncEnvelope`。
        #: **存整条信封而不是几个字段**：将来要重发时得原样再发一遍，服务端的
        #: 去重账本按 `request_id` 记账，重编一条就绕过去了（见模块 docstring）。
        #: 只增不减会漏，因此 `_forget_session` 清它。
        self._in_flight = {}
        #: 已取回、还没被界面取走的命令回执。与 `_queue` 同理：取走即出队。
        self._results = []
        self.stats = {
            "requests": 0,
            "received": 0,
            "bad_packets": 0,
            "dropped": 0,
            "handler_errors": 0,
            "sessions_opened": 0,
            "sessions_closed": 0,
            "describe_tables": 0,
            #: 不属于本会话的 S2C 包——**并存会话的直接证据**。它涨说明同一
            #: 玩家还有另一个活着的会话在推（关掉再打开终端就会造出一个），
            #: 而每一条都会试图换掉资源描述表。见 `_is_for_this_session`。
            "foreign_packets": 0,
            "commands": 0,
            #: 回执的 `request_id` 不在在途表里——通常是**上一个会话**的回执
            #: 迟到了。与 `unknown_results` 分开计数：这两个数一起看能分清
            #: 「服务端在回上一个会话的包」和「服务端回了一个我们没发过的 id」。
            "stale_results": 0,
            "unknown_results": 0,
            #: 服务端的拒绝（`error`）。与回执分开数：回执说的是「你那条命令
            #: 怎么了」，错误说的是「这条请求根本没被当成命令处理」。
            "errors": 0,
            #: 其中**因为服务端说会话已经不在了**而就地丢掉会话的次数。
            #: 它与 `errors` 的差就是「服务端自己出了错但会话还活着」那些。
            "sessions_dropped": 0,
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
        self.inventory_slots = {}
        self.inventory_revision = -1
        self.session_id = ""
        self.network_id = ""
        self.epoch = ""
        self._device_ref = None
        # 描述表跟着会话一起丢：留着它，下一个会话的前几条引用会拿旧序号
        # 去解，而服务端那边早就是一张新表了。
        self._descriptions = None
        self._descriptions_of = None
        # 在途与待取回执同样按会话算：`request_serial` 不重置，因此留着的
        # `request_id` 永远不会被新命令复用，它们只会变成**永远取不走**的
        # 垃圾——而界面的「有没有命令没回来」会一直说有。
        self._in_flight = {}
        self._results = []

    def drop_session(self):
        # type: () -> None
        """就地丢掉本会话的**全部**本地状态，**不发关闭包**。

        给「服务端已经判死这个会话」的场景用：此时再发 `session_close`
        只会换来又一条错误，而错误码恰恰是这一刻没人看的东西。与
        `close_session` 的区别只有「发不发包」一条，收尾动作完全一样
        （两条路都走 `_forget_session`），因此界面不必分两种关法。
        """
        self._forget_session()

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

    def send_command(
        self,
        action,
        expected_index_version=None,
        key=None,
        count=None,
        slot=None,
        slot_version=None,
        into_slot=None,
        into_slot_version=None,
        target=None,
    ):
        # type: (str, object, object, object, object, object, object, object, object) -> str
        """发一条手动存取命令，返回 `request_id`。

        `action` 是 `StorageService.handle` 认识的那几个之一（`inv_take` /
        `inv_put` / `inv_split` / `inv_merge` / `inv_swap` / `inv_quick_move`，
        以服务端那一侧为准）。**这里不校验动作名**：动作表在服务端，客户端
        再列一份就有两个真相源，而漏改的那一份只会让某条命令被服务端回一句
        「不认识」——那是能看见的失败，比本地拒发更好查。

        **`key` 收 `ResourceKey`，在这里转成线上形状。** `TerminalCommand`
        的 `key` 字段与线上同形（字典）：它的 `from_dict` 原样转交、`payload()`
        原样转给 `StorageService`，而后者的 `_key_from` 只认 `dict`。把领域
        对象留在里面，`to_dict()` 就会把它**原样写进消息**，引擎那边 msgpack
        编不了自定义对象——报的是
        `TypeError: can not serialize 'ResourceKey' object`，而**那是发包那
        一刻才炸的**，界面上什么都看不到（2026-09-30 真机撞过）。

        参数与 `TerminalCommand` 一一对应，**不在这里做字段名映射**：映射只
        在 `TerminalCommand.payload()` 里有一处。

        **`expected_index_version` 一定会带上，但本轮服务端不校验它**
        （理由见 `TerminalCommand` 的文档：客户端拿不到它的新值，校验会把它
        自己锁死）。默认 `None` 是给「调用方明确不想带」用的，界面正常路径
        应当传 `self.index_version`。

        **身份与目标不在参数里**：玩家由引擎附、终端由会话决定，两者都在
        服务端一侧有权威来源（见 `TerminalCommand` 的「为什么没有这些字段」）。

        **在途记录只记不发**：整条信封存进 `_in_flight`，本轮没有超时重发
        （超时值是未定值，见模块 docstring）。回执到了之后按 `request_id`
        弹出，因此这个表的大小就是「还没回来的命令有几条」。
        """
        self._require_session("发命令")
        request_id = self._next_request_id()
        envelope = SyncEnvelope(
            TerminalCommand(
                request_id,
                action,
                expected_index_version=expected_index_version,
                key=key.to_dict() if key is not None else None,
                count=count,
                slot=slot,
                slot_version=slot_version,
                into_slot=into_slot,
                into_slot_version=into_slot_version,
                target=target,
            ),
            session_id=self.session_id,
            network_id=self.network_id,
            epoch=self.epoch,
            view_kind=self.view_kind,
        )
        self._in_flight[request_id] = envelope
        self.stats["commands"] += 1
        self._send(envelope)
        return request_id

    def pending_commands(self):
        # type: () -> int
        """还没收到回执的命令条数。"""
        return len(self._in_flight)

    def _next_request_id(self):
        # type: () -> str
        """本实例发出的下一条请求 id。**序号取自进程级的 `next_request_id`**，
        理由见那里：序号放进实例会让每个会话都从 `c1` 重新开始，而服务端
        是按号去重的。
        """
        self.stats["requests"] += 1
        return next_request_id()

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
        # **第一道闸：号对不上就别让它碰描述表。** 判据只取「有一个非空且与
        # 自己不同的号」——畸形信封（`{}`、少字段）没有号，仍然走下面的解析
        # 并计进 `bad_packets`。两者必须分得开：混在一起，排障时分不清
        # 「协议对不上」与「并存会话」。
        raw_session = data.get("session_id") or ""
        if raw_session and self.session_id and raw_session != self.session_id:
            self.stats["foreign_packets"] += 1
            return None
        try:
            envelope = SyncEnvelope.from_dict(data, self._descriptions_for(data))
        except Exception:
            self.stats["bad_packets"] += 1
            return None
        # **不属于本会话的包在这里丢掉，且必须在 `_absorb` 之前。** 见
        # `_is_for_this_session`：它动的状态比「入队」深——`_absorb` 会把
        # 信封里的 `session_id`/`epoch` 写进本地，等于让别人的会话把我们的
        # 身份顶掉。描述表那一半在 `_descriptions_for` 里挡（两处都要挡：
        # 那里挡的是「换表」，这里挡的是「改会话状态与入队」）。
        #
        # 注意判据取**解析之后**的 `kind`：畸形信封在上一行就已经进了
        # `bad_packets`，不该被算成「别人的包」——两者混在一起会让排障时
        # 分不清「协议对不上」与「并存会话」。
        if not self._is_for_this_session(envelope, data):
            self.stats["foreign_packets"] += 1
            return None
        self.stats["received"] += 1
        if envelope.message.kind == MSG_INVENTORY_SNAPSHOT:
            if (envelope.session_id != self.session_id or
                    envelope.network_id != self.network_id or
                    envelope.epoch != self.epoch):
                return None
            message = envelope.message
            if message.revision < self.inventory_revision:
                return None
            self.inventory_slots = {row["slot"]: dict(row) for row in message.slots}
            self.inventory_revision = message.revision
            return envelope
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

    def _is_for_this_session(self, envelope, data):
        # type: (object, dict) -> bool
        """这条 S2C 包是不是**本会话**的。不是就丢，一个字节都不往本地写。

        **它防的不是伪造，是并存会话。** 引擎按玩家派发，而同一玩家可以不
        止一个会话（`max_sessions_per_player` 是 4）：关掉终端再打开，旧的
        那个在 `session_ttl_frames`（出厂 600 帧 ≈ 20 秒）到期前**还活着、
        还在推**。两个会话的包交替到达，而 `_descriptions_for` 的判据里带
        `session_id` —— 每来一条别人的包，整张资源描述表就被换成空的，本
        会话自己的快照分片与增量随即全解不出来。

        2026-10-01 真机实测到的正是这一条：同一玩家并存 `s1`/`s4` 两个会话，
        客户端收到的包在两者之间来回翻，`describe_tables` 一路涨、`bad_packets`
        涨到 9637、快照被判截断后又重取，**滚成 3210 次重取请求 / 17345 个
        回包**的无限循环。表现是「存进去的东西列表里永远不出现」，而服务端
        的账其实是对的。**手动关掉 `s1` 之后翻腾立刻停止。**

        规则：
        - 已经有自己的会话号 ⇒ 只收**号对得上**的；空号只有 `error` 允许
          （服务端用它表示「那个会话已经没了」，见 `_absorb_error`）。
        - 还没有自己的会话号（`session_ready` 未到）⇒ 只收**能建立会话**的
          那两种。放行其它种类等于给旧会话留了一个窗口：它的一条分片就会把
          表换掉，而那时我们连自己的号都还不知道。
        - **换了号不会自动跟随**：本客户端一次只开一个会话（`open_session`
          只发一次），别人的号意味着那是**另一台终端**的流。要换会话就先把
          旧的关掉（`session_id` 清空后回到上面第二支），这才是真实的路径。
        """
        incoming = envelope.session_id or ""
        kind = envelope.message.kind
        if not self.session_id:
            return kind in (MSG_SESSION_READY, MSG_ERROR)
        if not incoming:
            return kind == MSG_ERROR
        return incoming == self.session_id

    def _descriptions_for(self, data):
        # type: (dict) -> object
        """取出本会话的资源描述表；服务端换过表就新建一张，交给解码用。

        **为什么要有这张表**：带描述表编码时，一个序号只在**首次**发送时
        附带描述，之后只发 `resource_ref = {epoch, serial}`
        （`resource_identity.md` §3）。没有它，`_decode_item` 对每一条引用
        都只能抛「收到资源引用，但当前没有会话描述表」，而快照分片与增量
        两个包里**每一条都带引用**——症状是内容全丢、`bad_packets` 却只
        缓慢上涨，而 `session_ready` / `snapshot_begin` / `snapshot_end`
        这些不带引用的消息照常通过，看起来像「只有内容丢了」。
        `resource_sync.py` 开头那句「供服务端发送与客户端解码分别持有」
        说的就是这份表。

        **建表的依据是「服务端换过一次表」**，所以判据取会话与 epoch 两样：
        服务端每开一个会话就新建一张 `ResourceDescriptions(network_id, epoch)`
        （`server/sync/sessions.py`），epoch 则是网络代次、重开或重同步换新值。
        只比 epoch，会在「同一网络重开会话」时沿用旧表——那是最坏的一种：
        服务端的序号从 1 重新分配，客户端的 1 号却还指着上一会话的键，两边
        都以为自己对，直到 `decode` 的序号重绑检查把它判成坏包。只比会话，
        则会在 epoch 轮换时沿用旧表。

        **epoch 为空就不建表**：`error` 是唯一允许空会话字段的 S2C 消息，
        而 `ResourceDescriptions` 要求非空 epoch（拿到空值它自己会炸，那会
        把一个坏包变成一次抛出，违背 `receive` 的「只计数不抛出」）。
        """
        epoch = data.get("epoch") or ""
        session_id = data.get("session_id") or ""
        if not epoch:
            return self._descriptions
        # **别人的会话不换表。** 这一句与 `_is_for_this_session` 是同一件事
        # 的两半，两半都不能少：那时挡的是「改会话状态并入队」，这里挡的是
        # 「把表换掉」。少了这一句，别人的一条分片就能让本会话的引用全部
        # 解不出来——`receive` 是先建表再解析，坏包在解析那一步才产生，
        # 而那时表已经被换走了。
        if self.session_id and session_id and session_id != self.session_id:
            return self._descriptions
        identity = (data.get("network_id") or "", epoch, session_id)
        if self._descriptions is None or identity != self._descriptions_of:
            self._descriptions = ResourceDescriptions(identity[0], epoch)
            self._descriptions_of = identity
            self.stats["describe_tables"] += 1
        return self._descriptions

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
            self.inventory_slots = {}
            self.inventory_revision = -1
            self.index_version = message.index_version
            self.item_view_version = message.item_view_version
            self.stats["sessions_opened"] += 1
        elif message.kind == MSG_SNAPSHOT_BEGIN:
            self.item_view_version = message.item_view_version
        elif message.kind == MSG_COMMAND_RESULT:
            self._absorb_command_result(message)
        elif message.kind == MSG_ERROR:
            self._absorb_error(envelope, message)
        # **`command_result` 与 `item_delta` 一样不推进 `item_view_version`**：
        # 回执说的是「这条命令成没成」，库存动了的话，新版本会由随之而来的
        # 增量或快照告知。拿回执去推一个视图版本，等于凭「命令成功」推断
        # 「本地已经是最新的」——而回执完全可能先于增量到达。
        # **`item_delta` 刻意不在这里推进 `item_view_version`**：一个窗口可能
        # 拆成多包，收到最后一包也不代表已经应用（还差模型那边合并）。真正
        # 应用与否只有列表模型判得了（`begin_batch` 的基准比对），因此这里
        # 记的始终是**服务端告知**的进度，`request_snapshot` 不拿它当依据
        # （那个方法的 `from_version` 是必填的）。

    def _absorb_error(self, envelope, message):
        # type: (object, object) -> None
        """服务端的拒绝。**按信封里有没有会话 id 分两类**（见服务端
        `_error` 的说明）：

        - **不带会话 id** ⇒ 「那个会话已经不在了」（会话不存在／被别人抢／
          距离、维度或设备复核不过／**服务端空闲超时回收**——最后一条是
          服务端主动推的，`SyncTerminalHub._notify_abandoned`）。
          此时**就地丢掉本会话的本地状态**。
          留着它，界面会一直看起来正常：遮罩不出、库存快照还是旧的、命令
          继续往一个死会话上发，而每一条都只会再换回一条错误。
        - **带会话 id** ⇒ 会话还活着，是这一条请求自身失败了（服务端执行
          命令时抛了异常之类）。丢会话会让「服务端一次内部错误」变成
          「玩家界面掉线」，那是更糟的处置。

        **两条路都要把这一次请求收尾**，否则界面上那条「在途」永远挂着：

        - 会话判死那条走 `drop_session()`，界面按「会话还在不在」收尾
          （`TerminalListView._settle_lost_session`）。那对**所有**丢会话的
          原因都成立，不依赖错误这一条路径。
        - 会话还活着那条没有回执会来，所以在这里给它补一个结局，塞进
          `_results`（形状见 `_CommandOutcome`）。**只补在途表里有的**：
          与 `_absorb_command_result` 同一套判据，凭空冒出来的 id 不进队列。
        """
        self.stats["errors"] += 1
        if not envelope.session_id:
            self.stats["sessions_dropped"] += 1
            self.drop_session()
            return
        if message.request_id not in self._in_flight:
            return
        self._results.append(_CommandOutcome({
            "request_id": message.request_id,
            "status": STATUS_REJECTED,
            "payload": None,
            "reasons": [{"code": message.code, "detail": message.detail}],
            "topology_version": None,
        }))

    def _absorb_command_result(self, message):
        # type: (object) -> None
        """按 `request_id` 收下一条命令回执。

        **在途表里没有，也要分两种。** 只说「对不上」的话，两个完全不同的
        故障会长得一模一样：

        - **这个客户端发过命令**（`stats["commands"] > 0`）→ 计
          `stale_results`。最常见是**迟到或重复的回执**：那条命令要么已经
          答过，要么它是某个已被清掉的会话里发出去的（账本随会话清）。
        - **一条都没发过** → 计 `unknown_results`。服务端在回一个我们**从来
          没发过**的 id，那才是真异常。

        判据取的是**累计**发过多少条，不是「本会话发过多少条」：账本和会话
        是同一个边界上一起清的，所以「本会话发过没有」在账本被清之后一律
        为假——那样一来每个跨会话的迟到回执都会被记成 `unknown_results`，
        两个计数器也就没有区别了。

        **两种都不回错误、不关会话**：一条对不上的回执不构成会话级故障，
        为它关掉玩家的界面只会更糟；计数器是留给诊断看的。

        `CommandResultMessage` 的 `request_id` 已经在协议层与 `result` 里的
        那个核对过（`_validate_result`），所以这里只按消息层的 id 记账是
        安全的——两个 id 必然一致，对不上时那条包根本进不来。
        """
        if self._in_flight.pop(message.request_id, None) is None:
            if self.stats["commands"]:
                self.stats["stale_results"] += 1
            else:
                self.stats["unknown_results"] += 1
        self._results.append(message)

    def take_command_results(self, limit=None):
        # type: (object) -> list
        """取走最多 `limit` 条命令回执（不传则全取），返回 `CommandResultMessage` 列表。

        与 `take_events` 同一条「取走即出队」纪律，但**两条队列分开**：
        回执是「一次性事件」（这条命令成没成），而 `take_events` 那一条
        是「要按帧合并的状态流」（增量批）。混在一起的话，界面为了处理
        一条回执就得把这一帧的增量也一起遍历，而在「增量拆成十几包」时
        那个合并逻辑没法做。
        """
        if limit is None or limit >= len(self._results):
            taken = self._results
            self._results = []
            return taken
        taken = self._results[:limit]
        self._results = self._results[limit:]
        return taken

    def pending_results(self):
        # type: () -> int
        """已取回、还没被界面取走的回执条数。"""
        return len(self._results)

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
            # 命令通道的两条队列**分开报**：`in_flight` 是「还没回来的」，
            # `results` 是「回来了但界面还没取走的」。两者同时很大 = 界面
            # 没在 tick；`in_flight` 只涨不落 = 回执根本没送到（本轮没有
            # 超时重发，见模块 docstring），这是首先要看的一个数。
            "in_flight": len(self._in_flight),
            "results": self.pending_results(),
            "handlers": len(self._handlers),
            # 描述表的规模：它只增不减地跟着会话走，因此这个数等于「客户端
            # 认得的资源有几种」。为 0 而列表却是空的，第一个要看的就是它。
            "descriptions": (
                None if self._descriptions is None
                else self._descriptions.size()
            ),
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
