# coding=utf-8
"""终端同步通道的引擎适配器（任务 11）。本模块是**引擎边界**。

它只做四件翻译工作，一件业务判断都没有：

1. **收**：把 C2S 事件上的原始字典交给 `TerminalSyncHub.receive()`，
   再把回包经 S2C 事件发回给同一个玩家；
2. **身份**：`__id__`——发消息的人是谁，**只有引擎知道**。协议里连
   「消息体里写玩家」这个字段都没定义（见 `common/protocol.py` 的
   `SessionOpen`），因此没有一处代码会去读一个可以被伪造的身份；
3. **位置**：玩家所在维度与脚部坐标——会话校验的输入，而校验**在服务端
   按引擎给的位置自己重算**，不信客户端自报的坐标；
4. **注销**：玩家离线时关掉他的全部会话。

**事件类必须在函数体里建。** 模块顶层的 `class X(CustomC2SEvent)` 需要
模块顶层取到 `tooldelta.events`，而两条路都断：绝对写
`from tooldelta import events` 在真机上必 `ImportError`（脚本目录被整体当
包挂载，`tooldelta` 就在包内），相对写 `from ....tooldelta import events`
在离线模块名下是**越界**的层级（离线名少两层）。本机实测还有一条：
`import tooldelta` 本身就是失败的——`tooldelta/__init__` → `events` →
`events/client/control.py` 上那条模块级的
`from mod.client.extraClientApi import ...` 撞上了工具链装在
site-packages 里那份 `mod` 桩自身的循环导入。也就是说这个 vendored 包
**本来就不能在没游戏的环境里整体导入**；函数体导入不是保守起见，是必需
的，否则本模块（以及 import 它的 `bootstrap`）脱离游戏后无法导入，整条
离线链路随之失效（`test_import_boundaries.py` 的
`test_no_module_level_engine_import_in_adapter` 逐文件钉这一条）。
代价是「事件类的名字对不对」在真机上才看得出来，因此留了一个
`build_event_classes()` 的口子让离线检查能跑同一份类定义——
`endernet_tests/test_sync_channels.py` 用的就是它。

**不注册第二条总线。** 事件类挂在 MC 总线上（经
`ServerEventBinder.register` → `GetMCServerEventBus`）：`EventBus` 注册时
用的是 `event.GetNamespace() or self.namespace`，而自定义事件自带本模组的
命名空间，因此挂 MC 总线注册到的位置与 Mod 总线完全一样
（`GetNamespace` 见 `tooldelta/events/basic.py`）。`CustomC2SEvent.Listen()`
那一支走的是 `GetModServerEventBus()`，本项目禁用——同一条纪律由
`test_project_uses_only_the_mc_event_bus` 守着。
"""

from ...common.protocol import SYNC_CHANNEL_C2S, SYNC_CHANNEL_S2C
from .events import engine_player_dimension

#: 文本类型。**py2 下 `str` 与 `unicode` 是两类**（先例见
#: `client/ui/inventory_model.py` 的 `INTEGER_TYPES`）：引擎给的玩家 id
#: 在哪一类上，两边都出现过。只认 `str` 的话，py2 里一条 `unicode` 形式的
#: id 会被判成「取不到身份」，而那会让**每一次开界面都失败**——
#: 失败方向是安全的，但整个功能没了。
try:
    TEXT_TYPES = (str, unicode)
except NameError:
    TEXT_TYPES = (str,)


#: 引擎在 C2S 原始字典上**额外附加**的字段。它们不属于协议，解析前一律摘掉。
#:
#: `__id__` 是引擎给的发送者身份，它与协议字段**平级地混在同一个字典里**。
#: 而 `SyncEnvelope.from_dict` 对未知字段是拒收整条的（那是要防「对面发了
#: 脏数据」，不能放宽），于是不摘掉它的话，真机上每一条请求都会在解析那一步
#: 就被判成坏包——症状是「界面什么都不显示」，而服务端计数说的是
#: `bad_packets`，离原因隔着好几层。
#:
#: 摘掉它这件事只能由适配器做：它是唯一知道引擎约定的那一层，协议层不该
#: 认识某个引擎的私有字段名。
ENGINE_ADDED_FIELDS = ("__id__",)


def raw_data(value):
    # type: (object) -> object
    """从「事件实例」或「原始字典」里取出那个字典，取不到返回 None。

    两种形态都要认：组合根拿到的是引擎派发的事件实例（原始字典在 `.data` 上），
    而离线测试与 `unmarshal` 之后的重放可能直接给字典。只认一种的话，另一种
    会静默退化成「取不到身份」——那表现为「每一台终端都打不开」，
    与「总线挂错了」同一个症状，查法却完全不同。
    """
    if isinstance(value, dict):
        return value
    return getattr(value, "data", None)


def engine_events():
    # type: () -> object
    """引擎的 `tooldelta.events` 模块。**导入留在函数体内**（见模块 docstring）。

    它是本模块唯一的引擎依赖入口：两个事件类以它的基类为起点构造，
    玩家离线事件也从它取。因此它同时是**离线测试唯一的注入点**——
    传一个假模块进来，`listen` / `send` / `on_player_leave` 三条路径都能
    在游戏外跑通（见 `AdapterSyncPort.events`）。
    """
    from ....tooldelta import events as events_module

    return events_module


def source_player_id(data):
    # type: (object) -> str
    """从 C2S 事件的原始字典里取发消息的玩家 id。

    **`__id__` 是身份的唯一起点。** 引擎在把消息交给服务端时附上它，
    客户端既写不了也改不了；消息体里若也带一个玩家字段，那个字段是
    **对面说的**，只能用来对账、不能用来判定（协议层因此连字段都没定义）。

    取不到时返回空串。**不猜、不回退到别的字段**：一个猜出来的身份会让
    「这条请求是谁发的」变成猜测，而它正是所有会话校验的输入。
    """
    if not isinstance(data, dict):
        return ""
    value = data.get("__id__")
    return value if isinstance(value, TEXT_TYPES) else ""


def engine_player_position(player_id):
    # type: (str) -> object
    """玩家脚部坐标的**引擎实现**。导入留在函数体内，理由同 `events.py`。

    取**脚部**而不是实体中心：会话校验比的是「玩家到方块的距离」，而方块
    坐标是它最小角的整数坐标——用脚部位置时「站在方块上」垂直差为 0，
    用中心位置则会稳定多出半个身位。这条差别**没有在游戏里量过**
    （11-14 才做），因此这里**没有**据此加减任何经验常数：距离上限本身
    仍是待定值（`SessionPolicy.max_interaction_distance`）。
    """
    from ....tooldelta.api.server import GetFootPos

    return tuple(GetFootPos(player_id))


def build_event_classes(events_module):
    # type: (object) -> tuple
    """以 `events_module` 为基类来源，构造 `(C2S, S2C)` 两个事件类。

    **公开是为了可离线检验。** `tooldelta` 脱离游戏导不进来，于是
    「事件类的 `name` 是否等于 `common/protocol.py` 那两个频道常量」这条
    唯一的跨边界防漂移检查，只能靠传一个假模块进来跑。检查的是**同一份**
    类定义，假模块只换掉基类。

    每个类只实现**它真正要走的那个方向**：服务端只收 C2S、只发 S2C，
    所以 C2S 有 `unmarshal` 没有 `marshal`，S2C 反之。另一半留给基类抛
    `NotImplementedError`——真有人反着发时，那比「发出去一个空包」好查。
    """

    class TerminalRequestEvent(events_module.CustomC2SEvent):
        """客户端 → 服务端的一条终端请求。"""

        name = SYNC_CHANNEL_C2S

        def __init__(self, data=None):
            #: 引擎给的原始字典，**含 `__id__`**。原样带着，不在这一层解析：
            #: 解析规则在协议层，两侧共用同一份（`SyncEnvelope.from_dict`）。
            self.data = data if isinstance(data, dict) else {}

        def source_player_id(self):
            # type: () -> str
            """发这条消息的玩家。见模块级 `source_player_id`。"""
            return source_player_id(self.data)

        @classmethod
        def unmarshal(cls, data):
            return cls(data)

    class TerminalSyncEvent(events_module.CustomS2CEvent):
        """服务端 → 客户端的一条终端同步消息。"""

        name = SYNC_CHANNEL_S2C

        def __init__(self, payload=None):
            #: 已经编码好的线上字典（`SyncEnvelope.to_dict()` 的结果）。
            self.payload = payload if isinstance(payload, dict) else {}

        def marshal(self):
            # type: () -> dict
            return self.payload

    return TerminalRequestEvent, TerminalSyncEvent


class AdapterSyncPort(object):
    """同步通道的引擎侧实现。**这是 `server/sync/` 唯一的引擎出口。**

    对外只有四件事（与 `TerminalSyncHub` 的 docstring 对齐）：`send` /
    `listen` / `source_player_id` / `on_player_leave`，另加两条位置查询
    （会话校验的输入，由组合根拿去做 `context_of`）。

    **可以脱离引擎构造**：`__init__` 不 import 任何东西，引擎 import 全在
    方法体内。离线测试因此能构造它，验证身份提取、注册与发送这三条
    路径——只有真的进游戏才会碰到引擎接口。
    """

    def __init__(self, binder, events=None, player_dimension=None,
                 player_position=None):
        # type: (object, object, object, object) -> None
        #: 注册账本。**复用 `ServerEventBinder` 而不是自己留一份**：它已经
        #: 负责「记下句柄、`unbind()` 时逐一注销」，另起一份的结果是
        #: `stop()` 漏掉这两个监听器（热重载后回调翻倍），而那种症状极难定位。
        self.binder = binder
        #: 引擎 `events` 模块的解析器，默认 `engine_events`。**唯一的引擎
        #: 注入点**：换掉它，`listen` / `send` / `on_player_leave` 三条路径
        #: 就都能在游戏外跑通。
        self.events = events if events is not None else engine_events
        #: 玩家维度与脚部坐标。默认是引擎实现，注入替身即可离线驱动
        #: 「玩家在哪个维度、离终端多远」这两条会话校验的输入。
        self.player_dimension = (
            player_dimension
            if player_dimension is not None
            else engine_player_dimension
        )
        self.player_position = (
            player_position
            if player_position is not None
            else engine_player_position
        )
        #: 两个事件类，**每个端口只建一次**（见 `classes()`）。惰性构造：
        #: `__init__` 里建就等于在这里 import 引擎，离线构造立刻失败。
        self._event_classes = None
        self.stats = {
            "requests": 0,
            "leaves": 0,
            "sent": 0,
            "refused": 0,
            "handler_errors": 0,
        }

    def classes(self):
        # type: () -> tuple
        """本端口用的 `(C2S, S2C)` 两个事件类，**只建一次**。

        `EventBus` 用事件类对象本身当字典键（`event_listeners[priority][event]`），
        同一个端口两次拿到不同的类对象就会注册到两张表里，而注销只摘得掉
        其中一张——症状是热重载后同一个包被处理两次。
        """
        if self._event_classes is None:
            self._event_classes = build_event_classes(self.events())
        return self._event_classes

    # -- 身份与位置 ----------------------------------------------------

    def source_player_id(self, value):
        # type: (object) -> str
        """见模块级 `source_player_id`。本方法多认一种形态：**事件实例**（见
        `raw_data`）。取不到就是空串——不猜、不回退到别的字段。
        """
        return source_player_id(raw_data(value))

    def payload_of(self, value):
        # type: (object) -> object
        """事件里**属于协议的那部分**：摘掉引擎附的字段，其余原样。

        **它是 C2S 通向 `SyncEnvelope.from_dict` 的必经之路**：那个解析器对
        未知字段是拒收整条的，而引擎的身份字段就在同一个字典里（见
        `ENGINE_ADDED_FIELDS`）。漏摘的表现不是报错，是每一条请求都变成
        「坏包」——界面什么都不显示。

        返回 `None` 表示「这不是可以解析的东西」，由 hub 按坏包计数：
        与「字典里少字段」走同一条路（连信封都解不出来时不回话）。
        """
        data = raw_data(value)
        if not isinstance(data, dict):
            return None
        clean = dict(data)
        for field in ENGINE_ADDED_FIELDS:
            clean.pop(field, None)
        return clean

    def dimension_of(self, player_id):
        # type: (str) -> object
        """玩家所在维度；查不到返回 `None`。

        **不猜一个维度**：猜错会去读另一个维度同坐标的方块，而那可能是
        别人的设备。拿不到就交给会话校验去按「查不到坐标」处理——默认口径
        是拒绝（`SessionPolicy.require_position`，fail closed）。
        """
        return self._ask(self.player_dimension, player_id)

    def position_of(self, player_id):
        # type: (str) -> object
        """玩家脚部坐标；查不到返回 `None`。理由同 `dimension_of`。"""
        return self._ask(self.player_position, player_id)

    def _ask(self, asker, player_id):
        # type: (object, str) -> object
        """调一次引擎查询，失败归为「这次拿不到」。

        **吞掉异常是有意的**：玩家可能刚好离线、实体可能刚被卸载，
        这两种都不是缺陷。吞掉的只是「拿不到」这个结论本身——判定归
        会话校验，而它在拿不到时的默认答案是**拒绝**，不是放行。
        """
        if not player_id:
            return None
        try:
            return asker(player_id)
        except Exception:
            return None

    # -- 注册 ----------------------------------------------------------

    def listen(self, handler):
        # type: (object) -> int
        """注册 C2S 接收侧，返回注册的监听器个数（恒为 1）。

        `handler` 收到的是 `TerminalRequestEvent` 实例（已 `unmarshal`）。
        **包装一层**：引擎有没有真的把消息送进来，是实机上最难回答的问题
        之一，所以在这里计数——`stats["requests"]` 恒为 0 就直接排除了
        「总线挂错了」以外的所有猜测。
        """
        c2s, _ = self.classes()

        def dispatch(event):
            self.stats["requests"] += 1
            try:
                return handler(event)
            except Exception:
                # 回调抛出去会一路回到引擎。**记下并继续**：一条坏包不该
                # 让后续所有包都不再被处理（`drive_once()` 同一条纪律）。
                self.stats["handler_errors"] += 1
                return None

        self.binder.register(c2s, dispatch)
        return 1

    def on_player_leave(self, handler):
        # type: (object) -> int
        """注册玩家离线回调，返回注册的监听器个数（恒为 1）。

        **不注册它的后果不是报错，是会话泄漏**：界面关了、人走了，订阅表
        里还留着那个会话，发布窗口从此每帧都以为有人在看。
        """
        leave_event = self.events().DelServerPlayerEvent

        def dispatch(event):
            self.stats["leaves"] += 1
            try:
                return handler(getattr(event, "id", ""))
            except Exception:
                self.stats["handler_errors"] += 1
                return None

        self.binder.register(leave_event, dispatch)
        return 1

    # -- 发送 ----------------------------------------------------------

    def send(self, player_id, payload):
        # type: (str, dict) -> bool
        """把一条**已经编码好**的 S2C 消息发给某个玩家。

        **不编解码**：`payload` 由 hub 编好——只有它拿得到会话的资源描述表
        （序号是逐会话发的）。适配器碰了那份表，等于把会话表的内部结构
        摊到了引擎边界上。

        返回值只回答一件可预判的事：**有没有收件人**。
        **引擎抛的异常一律不在这里吞**——让它冒到 hub 的 `transport_errors`
        上去，而不是被这里转成一个与「收件人不存在」看起来一样的 `False`。
        两种情况要分开计数，否则「发不出去」查不出是引擎的问题还是没人的问题。
        """
        if not player_id:
            self.stats["refused"] += 1
            return False
        _, s2c = self.classes()
        s2c(payload).send(player_id)
        self.stats["sent"] += 1
        return True

    def diagnostics(self):
        # type: () -> dict
        return {
            "stats": dict(self.stats),
            "channels": [SYNC_CHANNEL_C2S, SYNC_CHANNEL_S2C],
        }
