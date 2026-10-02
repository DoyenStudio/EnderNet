# coding=utf-8
"""Reusable terminal screen and the common terminal's network list (11-03).

上半屏是网络物品列表，下半屏是玩家背包。列表这一半由 `TerminalCache` 驱动，
是本文件里唯一持有**线上状态**的东西；背包那一半由 `BackpackModel` 驱动，
读的是**真实玩家背包**（客户端本地读引擎，见 `plans/tasks/11. 终端与客户端
同步.md` 的默认口径段）。

交互是**对称的两步**（2026-10-01 User 改定，取代原先「点一行即取一组」那条
默认）：**选中一侧，再点另一侧才搬。**

- 点终端行 ⇒ 只**选中**那一行；点背包槽 ⇒ 只**选中**那一格。两侧互斥。
- 背包选了一格 ⇒ 再点终端行 = **存进网络**（`inv_take`）。
- 网络选了一行 ⇒ 再点背包槽 = 把那一行**放进这一格**（`inv_put` 带槽锚）：
  空格放得进、同种合堆、**异种拒绝**。
- 选中背包格再点列表空白处 = 整槽存进网络（`inv_quick_move`）。
- 选中背包格再点另一个背包格 = 背包内合并／交换。

**本地不做任何乐观写入**——屏幕上看到的背包就是引擎说的那一份，搬运全部发
命令给服务端，以回执为准。

下半屏 36 格**一格控件都不持有**：全部走引擎的集合绑定（`#EnderNetTerminal.
backpack_*` / `.hotbar_*`），由引擎逐格回调本类取值。理由是实测撞出来的——
grid 的格子要到**渲染**时才生成，在 `OnCreate` 里按控件路径去摸只会摸到空
（ModSDK 的网格文档也明说格子会在滚动中被回收换出、不要按路径操作），
而且按路径拿到的控件引用在换出之后就是过期的。

**上下两半的绑定都写在类体里**（`@Binder.binding` / `@Binder.binding_collection`），
一条都不用运行时注册（`DynaBinding` / `DynaBindingCollection`）。这不是风格
问题，是**运行时注册那条路在这个引擎上根本不生效**：见 `TerminalPanelBase`
的类文档。
"""

import time

from ....tooldelta.ui import Binder, RegistToolDeltaScreen, ToolDeltaScreen

try:
    from ....tooldelta.events.client.control import OnKeyPressInGame
except ImportError:
    # 离线（没有引擎）时拿不到这个事件类：`tooldelta/events/client/control.py`
    # 在**模块级** import `mod.client.extraClientApi`。**退化成「不注册这条
    # 监听」，而不是让整个模块导入失败**——界面代码必须能离线导入，否则
    # `endernet_tests` 里所有引用本模块的用例会一起报废。这与
    # `device_status_bindings` 取 `BF_BindColor`（拿不到就不挂颜色绑定）
    # 是同一手法。
    OnKeyPressInGame = None
from . import flying_item, slot_layout, stack_size
from .widgets import device_status
from .backpack_model import (
    BACKPACK_COUNT, HOTBAR_COUNT, HOTBAR_START, INTEGER_TYPES, BackpackModel,
    engine_slot_of,
)
from .item_list_model import SORT_COUNT, SORT_NAME
from .terminal_cache import TerminalCache
from .widgets import device_status_bindings
from ..network_client import NetworkClient
from ...common.identity import DeviceRef
from ...common.protocol import STATUS_OK

#: 颜色绑定的旗标。**定义已搬到** `ui/widgets/device_status_bindings.py`
#: （11-07 让设备代理屏与终端屏共用同一份 `stat_bar` 模板，没有理由留两份），
#: 这里只把旧名重新导出：`test_terminal_panel.py:2570` 直接读它，
#: 改名会让那条用例断在导入期。
_COLOR_BINDING_FLAG = device_status_bindings.COLOR_BINDING_FLAG

#: 背包轮询分频：每这么多帧重读一次引擎背包。
#:
#: **这是帧数，不是秒。** UI 的渲染帧率与脚本 tick、模拟 tick 都不是一回事，
#: 在代码里写死频率换算会随负载漂（CLAUDE.md 记了这条）。一次读是 36 份带
#: userData 的字典序列化加 36 次身份归一化，逐帧读会重演本文件里那条「别每帧
#: 调引擎」的教训；容器那边的既定做法也是退避轮询。
BACKPACK_POLL_FRAMES = 12

#: 长按判定的门槛（秒）。**这是手感值，只由观感决定**，与 `flying_item` 的
#: `FLY_SECONDS` 同一类，不进任何判定——改它只会改变「按多久算长按」。
#: 手势本身由 User 2026-10-01 定为**长按背包槽 = 拆分一半**（`inv_split`）。
#: **按秒不按帧**：界面的渲染帧率与脚本 tick、模拟 tick 都不是一回事，
#: 按帧数计时会让同一段时长在低帧率下拖长一截。
LONG_PRESS_SECONDS = 0.4

#: 终端列表的绑定前缀与集合名，与 `common_terminal_panel.json` 里
#: `terminal_slot` 模板覆盖的那几个 `$` 变量一一对应。
BINDING_PREFIX = "#EnderNetTerminal."
COLLECTION = "terminal_slots"

SEARCH_BINDING = BINDING_PREFIX + "search"
SORT_METHOD_BINDING = BINDING_PREFIX + "sort_method"
SORT_ORDER_BINDING = BINDING_PREFIX + "sort_order"
SELECT_BINDING = BINDING_PREFIX + "select"
COUNT_BINDING = BINDING_PREFIX + "count"
UNAVAILABLE_BINDING = BINDING_PREFIX + "unavailable"
#: 网络列表**空白背景**上那一下。名字要与 `common_terminal_panel.json` 里
#: `terminal_scroll_background` 的 `$pressed_button_name` **逐字相同**——
#: 对不上时引擎只是查不到绑定、安静地什么都不做（同其余几条绑定）。
QUICK_STORE_BINDING = BINDING_PREFIX + "quick_store"

#: 下半屏的两段格子。绑定是**按集合**注册的，而集合绑定的回调只拿得到「集合内
#: 下标」、拿不到集合名，所以两段必须各用一套绑定名：共用一套的话，快捷栏那 9
#: 格会按背包的下标去读 `BackpackModel`——读到的全是别的格子，界面照常渲染、
#: 不报任何错。
BACKPACK_COLLECTION = "endernet_terminal_backpack"
HOTBAR_COLLECTION = "endernet_terminal_hotbar"

#: `(集合名, 绑定名前缀, 首槽界面编号, 槽数)`。段内下标 + 首槽编号 = 面板槽位
#: 编号（0..35，见 `BackpackModel.engine_slot_of` 的换算）。两段各有一个具名
#: 常量，因为八个绑定方法要**各自**在类体里指明自己属于哪一段——类体里没法
#: 循环。
BACKPACK_SEGMENT = (BACKPACK_COLLECTION, "backpack", 0, BACKPACK_COUNT)
HOTBAR_SEGMENT = (HOTBAR_COLLECTION, "hotbar", HOTBAR_START, HOTBAR_COUNT)
SEGMENTS = (BACKPACK_SEGMENT, HOTBAR_SEGMENT)

#: 一格要报给引擎的四个量。名字既拼绑定名，也对应 `endernet_windows.
#: item_can_be_selected` 模板里的四个 `$item_*_binding`——绑定名对不上时引擎
#: 只是查不到绑定、安静地给假值，因此两边各有一个用例钉住：注册侧是
#: `test_terminal_panel.TestBackpackCellBindings`，JSON 侧是
#: `test_terminal_inventory.ScreenTests`。
SLOT_FIELDS = ("visible", "item_id_aux", "selected", "item_count")


def slot_binding(prefix, suffix):
    # type: (str, str) -> str
    """`("backpack", "selected")` → `"#EnderNetTerminal.backpack_selected"`。"""
    return BINDING_PREFIX + prefix + "_" + suffix


#: 控件路径。`main` 下只有一个 `terminal@common_terminal_panel_ui.content`，
#: 因此 `/terminal/...` 就是内容面板的根。滚动区那条**定义在 `slot_layout`
#: 里**（飞行动画的坐标也走它），这里重导出一次，免得同一条路径两个出处。
SCROLL_PATH = slot_layout.SCROLL_PATH
SORT_METHOD_ICON_PATH = "/sort_method_btn/icon"
SORT_ORDER_ICON_PATH = "/sort_order_btn/icon"

#: 排序方式图标是横排两格：左格按标识符、右格按数量
#: （`plans/ui/common_terminal_panel.md` 的 UV 表）。
SORT_METHOD_UV = {SORT_NAME: (0.0, 0.0), SORT_COUNT: (16.0, 0.0)}
#: 方向图标同样两格：左格升序、右格降序。默认降序（决策 D6）。
SORT_ORDER_UV = {False: (0.0, 0.0), True: (16.0, 0.0)}
ICON_UV_SIZE = (16.0, 16.0)

#: 排序方式按钮只在两种之间切换。`SORT_KIND` 没有对应图标，不参与循环。
SORT_METHOD_CYCLE = (SORT_NAME, SORT_COUNT)

#: 留多少条命令回执供诊断。回执**没有显示位置**（见 `plans/ui/
#: common_terminal_panel.md` 的待确认段），但队列必须被取走——不取的话
#: `NetworkClient._results` 会一直涨。这里取走后只留最近几条。
RECEIPT_MEMORY = 8

try:
    TEXT_TYPE = unicode
except NameError:
    TEXT_TYPE = str


#: 保活间隔：每这么多帧发一条 `keepalive`。**帧数，不是秒**。
#:
#: 终端面板**不需要**它的时候看起来一切正常，需要它的时候症状是「开着不动
#: 20 秒，界面还在、点哪儿都没反应」——服务端按空闲回收会话
#: （`SessionPolicy.session_ttl_frames`，出厂 600 个脚本 tick ≈20 秒），
#: 而 `expired` 那条路只推一条空会话号的 `error`（D-08）。
#:
#: 取值与推导**与 `proxy_panel.KEEPALIVE_FRAMES` 完全相同**（那里写得最细：
#: 两个计数器不同源，真正要比的是 `KEEPALIVE_FRAMES / 渲染帧率 <
#: 600 / 脚本 tick 频率`，而且**渲染帧率越慢余量越小**）。
#: 两边各留一份是因为两个模块互不依赖，`test_terminal_panel` 有一条用例
#: 把它们钉成同一个数——它们没有理由分家。
KEEPALIVE_FRAMES = 100


def _escape_key():
    # type: () -> object
    """ESC 的键码。**从引擎枚举里取**，不写死数字。

    那个枚举的取值不是本仓库的事实来源，抄一份只会在换版本时静默接不到键
    ——而「接了但永远不触发」与「压根没接」在界面上长得一模一样。
    常见写法试一遍；一个都取不到时返回 `None`，监听器随之永不触发
    （那是安全的退化：最多少一个关屏快捷键，而不是乱关别人的界面）。
    """
    board = getattr(OnKeyPressInGame, "KeyBoardType", None)
    if board is None:
        return None
    for name in ("Escape", "ESCAPE", "Esc", "KEY_ESCAPE"):
        value = getattr(board, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


ESCAPE_KEY = _escape_key()


def _listen_key_event(func):
    # type: (object) -> object
    """把方法挂成「**本屏在前台时才收到**」的引擎事件监听。

    `EventListenerService` 的监听器由 `general_screen.py` 的 `_do_active` /
    `_do_deactive` 成对开关，注册期恰好就是本屏在前台的那一段，屏一收起来
    就注销了。因此处理函数里**不需要再判 `screenName`**。

    离线时（`OnKeyPressInGame` 拿不到）原样返回，方法上不会留下标记，
    `_process_bind_listeners` 也就不会收它——不是「注册了一个永远不来的
    回调」，是压根没注册。
    """
    if OnKeyPressInGame is None:
        return func
    return ToolDeltaScreen.Listen(OnKeyPressInGame, 0)(func)


def _text(value):
    # type: (object) -> object
    """统一成 unicode。py2 下输入框回传的是 utf-8 字节串，直接 `.lower()`
    或与 `u""` 比较都会抛 `UnicodeDecodeError`（同 `select_item_window.py`）。"""
    if isinstance(value, TEXT_TYPE):
        return value
    return value.decode("utf-8")


class TerminalPanelBase(device_status_bindings.DeviceStatusBindingsMixin, ToolDeltaScreen):
    """Other terminal screens may inherit this class and reuse terminal_inventory.

    槽位是**界面编号**，不是引擎槽号：上方 9x3 是 0..26（背包），下方 9x1 是
    27..35（快捷栏）。换算成引擎槽号的只有 `BackpackModel.engine_slot_of`
    一处——引擎的顺序是快捷栏在前。

    点击任一槽**只选中**，不搬运；搬运发生在点**对侧**时（列表那一半发
    `inv_take` / `inv_put`）。因此本类**没有任何本地写入路径**：选中态不持有
    物品，丢弃它不欠任何东西。两侧的选中态互斥，见 `_click` 的三选一优先级。

    36 格全部由**集合绑定**驱动，本类不持有任何格子控件。这条不只是「更
    合规」：选中态归本类、而交接回执由列表视图处理，绑定每渲染帧重取一次，
    两边就不必互相认识——早先那套「每帧对账画高亮」的写法连同
    `CustomSlotView` 一起删掉了。

    **绑定一律写在类体里，不用运行时注册。** 引擎登记一条绑定之后，是拿
    **回调自己的名字**去生成的 `_Base` 类上 `getattr` 取回调的（`_Base` 由
    `ToolDeltaScreen._get_tdscreen_bindings` 在注册界面时造出来）。类体里的
    绑定会被那张表把**方法本身**挂成 `_Base` 的属性，名字就是方法名，因此
    查得到；而运行时注册那条路传下去的是一个包装函数，它的名字（如
    `reader`）在 `_Base` 上根本不存在——于是引擎每渲染一帧抛一次
    `AttributeError`，格子全空，界面看起来只是「背包里没东西」。这条是
    游戏内实测撞出来的，不是推断。

    因此绑定**不随会话生灭**：界面一建起来它们就生效，比 `OnCreate` 里建
    列表视图还早。每条回调都必须容忍自己依赖的那个对象还没建好，并给**安全
    值**（空列表、不遮罩、点了不做事），不能抛。
    """

    #: 列表视图。**只有拿到权威身份的会话才建**（见 `CommonTerminalPanel.
    #: OnCreate`），而绑定比它早生效，所以这里得有个类属性兜着。
    list_view = None

    #: 设备总体状态（11-13）。与 `list_view` 同一个地位：绑定比 `OnCreate`
    #: 早生效，而它只在拿到权威身份时才建，所以也要类属性兜着。没有它时
    #: 状态栏**不显示**（而不是显示一个假的「离线」）。
    status = None

    #: **虚格编辑器的协作对象**（11-05 的样板终端）。与 `list_view` / `status`
    #: 同一个地位，为 `None` 时**不存在**，此时背包点击与网络行点击的行为
    #: 与 11-03 定下的一字不差。
    #:
    #: 存在的理由是那两条手势在样板终端上多了一层含义（交接第 4 节）：
    #: 「选中虚格 → 点外界（背包槽或网络行）= 仅清空被选虚格」，而那一格
    #: **不取物、不清选中**。做成子类覆盖绑定方法是不行的——绑定按方法名
    #: 从 `dir(cls)` 收集，同名覆盖会让两个版本都注册进去，引擎怎么取是
    #: 未定义的。因此这里留一个显式的口子，默认 `None` 保证通用／合成终端
    #: 逐字不变。
    config = None

    def __init__(self, screen_name, screen_instance, params=None):
        ToolDeltaScreen.__init__(self, screen_name, screen_instance, params)
        self.backpack = BackpackModel()
        self._poll_frames = 0
        #: 长按判定（`inv_split` 的手势）。**按下记时、抬起判时**：引擎只有
        #: 「按下」与「抬起」两个相位，没有「长按」这一档绑定，时长只能在
        #: 脚本侧算（见 `_press` 与 `_click`）。
        self._press_index = None
        self._press_at = 0.0
        #: 时钟**可注入**：按秒不按帧的理由与 `flying_item` 完全一样，而
        #: 注入之后离线能精确推进——用例不必真的 sleep 0.4 秒。
        self._clock = time.time
        #: 搬运动画。控件解析是懒的（`flying_item` 不在本界面上时永久退化成
        #: 不做任何事），因此这里不等 `OnCreate`，也不会因为没有它而失败。
        #: 两条路径都是**内容区相对**的，由 `slot_layout.control` 按本界面的
        #: 屏幕挂法解析（两种挂法见 `slot_layout._candidates`）。
        self.fly = flying_item.FlyingItemAnimator(
            self, slot_layout.FLYING_ITEM_PATH, slot_layout.CONTENT_PATH)

    def OnCreate(self):
        # 先读一次。读失败时 `replace_from_engine` 返回空表，界面停在「全空」
        # ——**不能在这里抛**，抛出去整个界面建不起来。这不是假设：早先这里
        # 按控件路径逐格摸 `grid.GetGridItem`，格子要到渲染时才生成，第一步
        # 就 `ValueError: Can't initialize UBaseCtrl: comp is None`。
        self.backpack.replace_from_engine()

    # -- 列表绑定（类体注册） ------------------------------------------
    #
    # 每条都先看列表视图在不在：没有权威身份的会话（如 `OpenDemo`）永远不建
    # 它，而绑定照样会被引擎问。缺席时的返回值是**安全值**，不是假数据——
    # 报一个「有 N 行」出去就会画出幽灵格子。

    @Binder.binding(Binder.BF_BindInt, COUNT_BINDING)
    def terminal_count(self):
        # type: () -> int
        """可见行数，接 `#maximum_grid_items`。没有会话时是 0。"""
        if self.list_view is None:
            return 0
        return self.list_view.get_count()

    @Binder.binding(Binder.BF_BindBool, UNAVAILABLE_BINDING)
    def terminal_unavailable(self):
        # type: () -> bool
        """遮罩的 `#visible`：没有会话时**不遮**（同 `is_unavailable` 的
        取向——绑定还没生效时界面应当可用，而不是先糊上一层）。"""
        if self.list_view is None:
            return False
        return self.list_view.is_unavailable()

    @Binder.binding(Binder.BF_EditChanged, SEARCH_BINDING)
    def terminal_search(self, params):
        # type: (object) -> None
        if self.list_view is not None:
            self.list_view.on_search_changed(params)

    @Binder.binding(Binder.BF_ButtonClickUp, SORT_METHOD_BINDING)
    def terminal_sort_method(self, params=None):
        # type: (object) -> None
        # `params` 默认 `None` 是**照抄列表视图那一侧的签名**（两个排序回调
        # 都不看它），保持转发前后一致，别让同一条绑定在两处有不同形状。
        if self.list_view is not None:
            self.list_view.on_sort_method(params)

    @Binder.binding(Binder.BF_ButtonClickUp, SORT_ORDER_BINDING)
    def terminal_sort_order(self, params=None):
        # type: (object) -> None
        if self.list_view is not None:
            self.list_view.on_sort_order(params)

    @Binder.binding(Binder.BF_ButtonClickUp, SELECT_BINDING)
    def terminal_select(self, params):
        # type: (object) -> None
        if self.list_view is not None:
            self.list_view.on_select(params)

    @Binder.binding_collection(
        Binder.BF_BindBool, COLLECTION, BINDING_PREFIX + "has_item")
    def terminal_has_item(self, index):
        # type: (object) -> bool
        if self.list_view is None:
            return False
        return self.list_view.has_item(index)

    @Binder.binding_collection(
        Binder.BF_BindInt, COLLECTION, BINDING_PREFIX + "item_id_aux")
    def terminal_item_id_aux(self, index):
        # type: (object) -> int
        if self.list_view is None:
            return 0
        return self.list_view.get_item_id_aux(index)

    @Binder.binding_collection(
        Binder.BF_BindBool, COLLECTION, BINDING_PREFIX + "selected")
    def terminal_selected(self, index):
        # type: (object) -> bool
        if self.list_view is None:
            return False
        return self.list_view.is_selected(index)

    @Binder.binding_collection(
        Binder.BF_BindString, COLLECTION, BINDING_PREFIX + "item_count")
    def terminal_item_count(self, index):
        # type: (object) -> str
        if self.list_view is None:
            return ""
        return self.list_view.get_item_count(index)

    # -- 格子绑定（类体注册） ------------------------------------------
    #
    # 每格四个量 × 两段 = 八条。名字由 `slot_binding` 拼出，与界面 JSON 里
    # `item_can_be_selected` 模板覆盖的四个 `$item_*_binding` 一一对应。
    # **两段必须各写一套**：集合绑定的回调只拿得到集合内下标、拿不到集合名，
    # 共用一套的话快捷栏那 9 格会按背包的下标去读，界面照常渲染、不报任何错。

    @Binder.binding_collection(
        Binder.BF_BindBool, BACKPACK_COLLECTION, slot_binding("backpack", "visible"))
    def backpack_visible(self, index):
        # type: (object) -> bool
        return self._slot_visible(BACKPACK_SEGMENT, index)

    @Binder.binding_collection(
        Binder.BF_BindInt, BACKPACK_COLLECTION, slot_binding("backpack", "item_id_aux"))
    def backpack_item_id_aux(self, index):
        # type: (object) -> int
        return self._slot_item_id_aux(BACKPACK_SEGMENT, index)

    @Binder.binding_collection(
        Binder.BF_BindBool, BACKPACK_COLLECTION, slot_binding("backpack", "selected"))
    def backpack_selected(self, index):
        # type: (object) -> bool
        return self._slot_selected(BACKPACK_SEGMENT, index)

    @Binder.binding_collection(
        Binder.BF_BindString, BACKPACK_COLLECTION, slot_binding("backpack", "item_count"))
    def backpack_item_count(self, index):
        # type: (object) -> str
        return self._slot_item_count(BACKPACK_SEGMENT, index)

    @Binder.binding_collection(
        Binder.BF_BindBool, HOTBAR_COLLECTION, slot_binding("hotbar", "visible"))
    def hotbar_visible(self, index):
        # type: (object) -> bool
        return self._slot_visible(HOTBAR_SEGMENT, index)

    @Binder.binding_collection(
        Binder.BF_BindInt, HOTBAR_COLLECTION, slot_binding("hotbar", "item_id_aux"))
    def hotbar_item_id_aux(self, index):
        # type: (object) -> int
        return self._slot_item_id_aux(HOTBAR_SEGMENT, index)

    @Binder.binding_collection(
        Binder.BF_BindBool, HOTBAR_COLLECTION, slot_binding("hotbar", "selected"))
    def hotbar_selected(self, index):
        # type: (object) -> bool
        return self._slot_selected(HOTBAR_SEGMENT, index)

    @Binder.binding_collection(
        Binder.BF_BindString, HOTBAR_COLLECTION, slot_binding("hotbar", "item_count"))
    def hotbar_item_count(self, index):
        # type: (object) -> str
        return self._slot_item_count(HOTBAR_SEGMENT, index)

    # 设备总体状态的十一条绑定**不在这个类体里**——它们已搬到
    # `ui/widgets/device_status_bindings.py` 的 `DeviceStatusBindingsMixin`
    # （11-07：设备代理屏与终端屏共用同一份 `stat_bar` 模板与同一组绑定名
    # `#EnderNetDeviceStatus.*`）。本类继承它，因此 `dir(cls)` 照样看得见、
    # 引擎照样按名取得着，只是取值逻辑只有一份。

    # -- 格子回调（每渲染帧，逐格） ------------------------------------

    @staticmethod
    def _slot_index(segment, index):
        # type: (tuple, object) -> object
        """集合内下标 → 面板槽位编号（0..35）；不在范围内时返回 `None`。

        **校验是必须的**：两段共用同一个槽位空间（背包 0..26、快捷栏 27..35），
        越界的下标会让快捷栏的格子读成背包的格子，而界面照常渲染。
        """
        if (isinstance(index, bool) or not isinstance(index, INTEGER_TYPES)
                or not 0 <= index < segment[3]):
            return None
        return segment[2] + index

    def _slot_entry(self, segment, index):
        # type: (tuple, object) -> object
        slot = self._slot_index(segment, index)
        if slot is None:
            return None
        return self.backpack.entry(slot)

    def _slot_visible(self, segment, index):
        # type: (tuple, object) -> bool
        """格子的 `#visible`：**恒真，空格子也要画**。

        模板把这个值当成那一格可见性的**唯一**来源，而背包里的空格子仍然要
        画出槽底，所以不能用「有没有东西」来回答可见性——没有东西是用
        `#item_id_aux == 0` 表达的。
        """
        return True

    def _slot_item_id_aux(self, segment, index):
        # type: (tuple, object) -> int
        entry = self._slot_entry(segment, index)
        if entry is None:
            return 0
        return stack_size.id_aux(entry.key.name)

    def _slot_selected(self, segment, index):
        # type: (tuple, object) -> bool
        slot = self._slot_index(segment, index)
        return slot is not None and slot == self.backpack.selected

    def _slot_item_count(self, segment, index):
        # type: (tuple, object) -> str
        """堆叠数标签。**1 与 0 都留空**（同上半屏的 `get_item_count`）：
        满屏的「1」只是噪声。"""
        entry = self._slot_entry(segment, index)
        count = entry.count() if entry is not None else 0
        return str(count) if count > 1 else ""

    # -- 每帧与点击 ----------------------------------------------------

    def _poll_backpack(self):
        # type: () -> None
        """背包那一半的每帧推进：按分频重读引擎。

        重读之后**不需要再通知界面**：36 格的数据都挂在集合绑定上，引擎每
        渲染帧自己来取一次。

        **由子类显式调用，不在本类的 `OnTicking` 里**：`CommonTerminalPanel`
        的 `OnTicking` 没有 `super()`，基类加一个同名方法会被覆盖而**永不执行
        且不报错**——那种错在游戏里只表现为「背包一直不更新」。
        """
        self._poll_frames += 1
        if self._poll_frames >= BACKPACK_POLL_FRAMES:
            self._poll_frames = 0
            self.backpack.replace_from_engine()

    def GetBackpackItems(self):
        # type: () -> list
        """诊断快照：每槽一个 `(资源名, 数量)` 或 `None`。

        **只读**。这里没有对应的写入口——面板的背包由引擎说了算，留一个
        「本地灌数据」的写入口比删掉更贵：它迟早会被用来绕开服务端。
        """
        return self.backpack.snapshot()

    def _click(self, params, start, count):
        # Do not reinterpret an absent or out-of-range index as slot zero.
        index = params.get("#collection_index")
        if (isinstance(index, bool) or not isinstance(index, INTEGER_TYPES)
                or not 0 <= index < count):
            return
        # **点之前先读一次**：玩家看到的可能是上一帧的画面，拿过期内容去选中
        # 会选出「显示的东西」与「那一格实际的东西」对不上的一格。点击是低频
        # 事件，这一次读与每帧轮询不是一回事。
        self.backpack.replace_from_engine()
        # **不挡连点。** 背包内的操作支持多轮：玩家在等服务端回执的那段时间
        # 里可以接着点，每一轮都作用在虚拟空间上、并记进 `_pending`。真值
        # 沿队列从头兑现（`BackpackModel._retire`）。
        ui_index = start + index
        # **长按是另一条手势，先判它。** `inv_split`（把这一槽的一半存进网络）
        # 由 User 2026-10-01 定为长按；引擎没有「长按」这一档绑定，所以时长
        # 在这里按「按下 - 抬起」算（`_press` 记的）。
        #
        # **判完无论长短都要把按下记录清掉**：留着的话，下一下短按会拿上一次
        # 的时间戳去比，一次偶然的长按会让之后**每一下短按**都变成拆分。
        hold = None
        if self._press_index == ui_index:
            hold = self._clock() - self._press_at
        self._press_index = None
        if hold is not None and hold >= LONG_PRESS_SECONDS:
            if self.list_view is not None:
                self.list_view.on_backpack_split(ui_index, params)
            return
        # **三选一，顺序就是优先级。**
        #
        # 1. **网络里选中了一行** ⇒ 把那一行放进这一格（`on_backpack_put`）。
        #    这是 2026-10-01 加的那半边手势：点终端行只选中，点背包格才搬。
        # 2. 背包里选中了别的格 ⇒ 背包内部的动作（合并／交换／移动）。
        # 3. 否则 ⇒ 选中这一格。
        #
        # 判断全部发生在 `select()` **之前**：`select()` 一旦先跑，选中就挪到
        # 目标格上去了，再拿它当源槽就是搬错了格子。
        #
        # 没有会话时不发命令、也不动选中：这些动作必须由服务端执行，本地没有
        # 任何可写的东西（`BackpackModel` 的第三条纪律）。
        # **虚格选中优先于背包动作**（11-05，见 `config` 的文档）。放在长按
        # 之后、三选一之前：长按是「改这一格的数量」之外的另一种手势，
        # 而虚格选中要抢的是「再点一下」那一下。`config` 为 `None` 时这两行
        # 是空操作，通用／合成终端的行为因此逐字不变。
        if (self.config is not None
                and self.config.consumes_slot_click(ui_index)):
            return
        if self.list_view is not None and self._row_selected():
            self.list_view.on_backpack_put(ui_index)
            return
        if self.backpack.intends_exchange(ui_index):
            if self.list_view is not None:
                self.list_view.on_backpack_exchange(ui_index)
            return
        if self.list_view is not None:
            # **互斥：两侧不同时选中。** 选中背包这一格就把网络那一边的选中
            # 放掉，否则下一次点背包格会按「对侧有选中」去放东西，而玩家心里
            # 想的是背包内合并——两个都选中时谁优先是没有答案的，不让它发生
            # 最省事。
            self.list_view.cache.model.clear_selection()
        self.backpack.select(ui_index)

    def _row_selected(self):
        # type: () -> bool
        """网络列表里此刻有没有选中的行。没有列表视图（无权威身份）时为假。"""
        if self.list_view is None:
            return False
        return self.list_view.cache.model.selected_key() is not None

    def _press(self, params, start, count):
        # type: (object, int, int) -> None
        """按下（`BF_ButtonClickDown`）：**只记格子与时刻**，什么都不做。

        与抬起那条绑定**挂同一个名字**（`slot_binding(prefix, "click")`）：
        引擎按「标志 + 名字」找回调，两个相位于是各挂各的，而 **JSON 侧一个
        字都不用改**——这是不碰 User 界面的那条路（`item_can_be_selected`
        是共享模板，上半屏也在用）。

        取不到合法的格子下标就把记录清掉：那一下按下的是一格**算不出身份**
        的位置，抬起时不该拿它去比对。
        """
        index = params.get("#collection_index")
        if (isinstance(index, bool) or not isinstance(index, INTEGER_TYPES)
                or not 0 <= index < count):
            self._press_index = None
            return
        self._press_index = start + index
        self._press_at = self._clock()

    @Binder.binding(Binder.BF_ButtonClickDown, slot_binding("backpack", "click"))
    def OnBackpackPress(self, params):
        self._press(params, 0, BACKPACK_COUNT)

    @Binder.binding(Binder.BF_ButtonClickDown, slot_binding("hotbar", "click"))
    def OnHotbarPress(self, params):
        self._press(params, HOTBAR_START, HOTBAR_COUNT)

    @Binder.binding(Binder.BF_ButtonClickUp, slot_binding("backpack", "click"))
    def OnBackpackClick(self, params):
        self._click(params, 0, BACKPACK_COUNT)

    @Binder.binding(Binder.BF_ButtonClickUp, slot_binding("hotbar", "click"))
    def OnHotbarClick(self, params):
        self._click(params, HOTBAR_START, HOTBAR_COUNT)

    @Binder.binding(Binder.BF_ButtonClickUp, QUICK_STORE_BINDING)
    def OnQuickStoreClick(self, params=None):
        # type: (object) -> None
        """点上方网络列表的**空白背景**：把选中的背包槽整槽存进网络。

        **这一块是隐形按钮，不是装饰。** `common.scrolling_panel` 的
        `background` 覆盖着整个列表区，但格子（`terminal_items_grid`）画在
        它上层，而空格子的格子是不可见的——于是**格子上的一下归格子、
        空白处的一下归这里**，两种点击在同一条路径上分开。

        `params` 不看：这条手势与集合无关，也没有 `#collection_index`。
        默认 `None` 是照抄列表视图那一侧的签名，保持转发前后一致。

        转发到列表视图而不是就地处理，与其余几条一样：**背包与命令的账
        都在列表视图那边**（它才认识会话与在途请求），面板只做转发。
        没有会话（`OpenDemo`）时列表视图缺席，这一下什么也不做。
        """
        if self.list_view is not None:
            self.list_view.on_quick_store(params)

    @_listen_key_event
    def OnEscapeKey(self, event):
        # type: (object) -> None
        """按 ESC 关屏（User 2026-10-02）。

        **不走 JSON 的 `button_mappings`。** 引擎给的那条路（`menu_cancel` →
        某个按钮名）在真机上会触发脚本层奇怪的报错，因此改成在这里接
        `OnKeyPressInGame`。

        三条判据：**只认按下**（`isDown == 1`——抬起时再来一次会把刚开的屏
        又关掉）、键码是 ESC、本屏仍是活动的那个（监听器本身已经保证了这一点，
        这里再挡一次是因为 `_do_deactive` 与事件派发之间可能隔着一帧）。

        `RemoveUI()` 与点 ✕ 走同一条路（`OnCloseClick` 也调它），因此收尾
        ——清选中、收动画、注销端口——完全一致。
        """
        if not self._activated:
            return
        if getattr(event, "isDown", 0) != 1:
            return
        if ESCAPE_KEY is None or getattr(event, "key", None) != ESCAPE_KEY:
            return
        self.RemoveUI()

    @Binder.binding(Binder.BF_ButtonClickUp, "#EnderNetTerminal.close")
    def OnCloseClick(self, params):
        # type: (object) -> None
        """点 ✕ 关屏。

        **这条装饰器一度挂错了地方**（2026-10-02 加 ESC 那条时）：它被留在了
        新方法 `OnEscapeKey` 头上，于是点 ✕ 触发的是那个只在键盘事件上成立的
        处理函数——它拿到的是按钮的 `params` 字典，`getattr(event, "isDown", 0)`
        取不到就当场返回。**表现是 ✕ 点了完全没反应**，而 `OnCloseClick` 成了
        一个没人绑的死方法。加方法时贴着别人的装饰器插，就是这个下场：
        装饰器与它修饰的函数之间没有任何语法约束，错了不报错。
        """
        self.backpack.clear_selection()
        # 半空中的那一个要收起来：界面都没了，`OnTicking` 不再推进它，
        # 留着下一次打开时还挂在半路。
        self.fly.clear()
        self.RemoveUI()

    def OnDeactive(self):
        # 界面隐藏后 `OnTicking` 不再跑，选中态留着只会在下次打开时冒出来
        # 指向一个早就过期的东西。控件不用管：绑定每渲染帧重取一次。
        # 动画同理——它也是「没人推进就永远停在那一帧」的东西。
        self.backpack.clear_selection()
        self.fly.clear()
        # 详情弹窗也收起来：界面隐藏后没人再点它，留着的话下次打开时它
        # 会**先显示一帧**，而那一帧里的原因是上一次的连接状态。
        if self.status is not None:
            self.status.close_popup()

    def OnDestroy(self):
        self.backpack.clear_selection()
        self.fly.clear()


class TerminalListView(object):
    """把 `TerminalCache` 接成 `common_terminal_panel_ui` 上的一份可见列表。

    **本类一条绑定都不注册。** 引擎要的十条绑定写在 `TerminalPanelBase` 的
    类体里（`terminal_count` / `terminal_has_item` / …），由那边转发到这里；
    绑定不能随会话生灭（原因见 `TerminalPanelBase` 的类文档），而会话会。
    因此本类只回答「现在是什么」，不碰登记的账。

    列表**整表交给引擎的集合绑定**（决策 D2）：`#maximum_grid_items` 报
    可见行数，引擎按它生成格子，再逐格回调 `has_item` / `get_item_id_aux`
    / `is_selected` / `get_item_count`。因此这里**不持有控件引用、不逐格写
    值**——11-10 的虚拟槽位控件池是把这个策略换掉，不是在这里加一层。

    一行点击 = 一条 `inv_put`（**从网络取进背包**，方向以网络为参照，见
    `server/storage/service.py` 的动作表），数量是**一组**：`min(可用数,
    maxStackSize)`。界面上没有任何输入数量的控件，而「取一个」与「取一组」
    都需要先定一个约定；这里取**一组**，并把这条约定记为待确认——R12 定稿
    后若加上按住取半、右键取一个，改的是这一处。
    """

    def __init__(self, screen, cache=None, device_ref=None, dimension=0,
                 backpack=None, scroll_path=SCROLL_PATH, receipt_sink=None):
        self.screen = screen
        #: 本界面滚动面板的路径。**默认是本终端列表那一处**，`None` 表示
        #: 「这个界面的滚动路径没有运行时结论」——那时整块重锚不做，列表
        #: 内容与搜索、排序都不受影响（合成终端就是这么接的，见
        #: `crafting_panel.py` 的已知缺项）。传一个错的路径比不传更糟：
        #: 取到的可能是另一个控件，重锚会去动一个与列表无关的滚动条。
        self.scroll_path = scroll_path
        #: 回执的**第二位消费者**（可选）。回执队列是「取走即出队」，
        #: 两处各取一次会互相吃掉对方的条目，因此由本视图取，取完把整批
        #: 转发给它（合成终端的工作台靠它收自己的三条命令的回执）。
        self.receipt_sink = receipt_sink
        #: 下半屏那个背包模型，**同一个对象**（面板持有的那个）。传进来而不是
        #: 回头摸 `screen.backpack`：这里要的只是「读选中、清选中」两件事，而
        #: 显式传参把这条依赖摆在构造函数的签名上。
        self.backpack = backpack
        self.cache = cache if cache is not None else TerminalCache()
        self.device_ref = device_ref
        self.closed = False
        #: 保活计数（帧）。见 `_keepalive`：终端面板一条命令都不发时，
        #: 服务端会在 20 秒后把会话回收，而界面上只表现为「点哪儿都没反应」。
        self._keepalive_frames = 0
        #: 排序状态。初值是 `ItemListModel` 的默认（决策 D6：名称降序），
        #: 但**不是抄来的**——`__init__` 末尾会把它写回模型（见那里）。
        self.sort_field = SORT_NAME
        self.sort_descending = True
        self.search_text = ""
        #: 当前可见行的 `ItemEntry`。集合绑定按 index 读它，因此它必须与
        #: 引擎正在渲染的那一帧一致——`refresh()` 是唯一的写入点。
        self.rows = ()
        self.receipts = []
        self._shown = set()
        #: `request_id -> 发那条命令时的选中修订号`（`BackpackModel.
        #: selection_revision()`）。**回来时要收尾**的命令都记在这里
        #: （`inv_take`，或背包内部的 `inv_merge` / `inv_swap`）。见
        #: `_settle_action`。
        #:
        #: **是表不是单值**：背包内的操作支持多轮（玩家在等服务端回执的那段
        #: 时间里可以继续点），因此同时有多条在途是正常状态。单值会让后一条
        #: 顶掉前一条，于是前一条的回执认不出来、那条本地操作也就永远等不到
        #: 撤下它的信号。
        #:
        #: **修订号是用来判断「这条命令发出去之后，玩家还动过选中没有」的。**
        #: 回执只该清**发命令时**的那个选中；拿「收到回执那一刻的选中」去清
        #: 会把玩家新建立的选中一并抹掉——之后点终端行就落到 `inv_put`，
        #: **方向与玩家的意图相反**。比槽号是不够的：玩家「取消选中 → 又选中
        #: 同一格」之后槽号与发命令时一模一样，而那是两次不同的意图。
        #: **不改点击抑制**：防抖与连击合并属 R12。
        self._action_requests = {}
        self._scroll = None
        self._sort_uv = {SORT_METHOD_ICON_PATH: None, SORT_ORDER_ICON_PATH: None}
        if self.scroll_path is not None:
            try:
                # **拿到的就是引擎那个 `ScrollViewUIControl` 本身**，两个百分比
                # 方法长在它身上；`.base` 是 tooldelta `UBaseCtrl` 才有的东西。
                # 2026-10-01 真机实测：写成 `self._scroll.base.X` 会 AttributeError，
                # 被下面两处 `except Exception: return` 吞掉，于是**重锚整条静默
                # 失效**——表现只是「滚动位置偶尔会跳」，离线用例也照不到（当时的
                # 替身照 tooldelta 包装层的形状写的）。见 `_remember_anchor`。
                self._scroll = slot_layout.scroll_view_at(screen, self.scroll_path)
            except Exception:
                # 取不到滚动条不影响列表本身：锚点退化成「不重锚」。
                self._scroll = None
        # **把排序状态写回模型**，而不是假定它自己也是这两个值。方向与方式
        # 是本类呈现给玩家的东西（两个图标），图标说的和列表做的必须是同一
        # 件事；而模型没有公开的读取接口，靠「两边默认值碰巧一样」来对齐，
        # 改一处默认就会让图标与顺序悄悄分家。这一次 `set_sort` 只是让首次
        # 建表按本类的初值来，多出的那一次失效发生在第一帧渲染之前。
        self._apply_sort()

    # -- 引擎回调（每渲染帧，逐格） ------------------------------------

    def get_count(self):
        # type: () -> int
        """可见行数，接 `#maximum_grid_items`。"""
        if self.closed:
            return 0
        return self.cache.model.count()

    def is_unavailable(self):
        # type: () -> bool
        """遮罩的 `#visible`：没有会话，或正等着重同步的快照。

        **反着报是刻意的**：绑定名读不到值时引擎给的是假值，遮罩默认不
        显示——界面在「绑定还没生效」时应当是可用的，而不是先糊上一层。
        """
        return (self.closed or not self.cache.client.has_session()
                or self.cache.awaiting_snapshot())

    def has_item(self, index):
        # type: (object) -> bool
        return (not self.closed and not isinstance(index, bool)
                and isinstance(index, INTEGER_TYPES)
                and 0 <= index < len(self.rows))

    def _basic_info(self, item_id):
        # type: (str) -> tuple
        """`(id_aux, maxStackSize)`。**委托给 `stack_size`**，不在这里缓存。

        「一组是多少」只能有一份定义：上半屏发 `inv_put`、下半屏发
        `inv_take`，两处各写一份的代价不是重复代码，而是**两个方向的天花板
        会悄悄分家**——同一个键在「取」的时候按 64 算、在「存」的时候按 1 算，
        界面上没有任何东西看得出来。因此缓存也挂在 `stack_size` 的模块上，
        不跟视图实例走。
        """
        return stack_size.basic_info(item_id)

    def _stack_limit(self, item_id):
        # type: (str) -> int
        """一组是多少。退化的方向见 `stack_size.stack_limit`。"""
        return stack_size.stack_limit(item_id)

    def get_item_id_aux(self, index):
        # type: (object) -> int
        if not self.has_item(index):
            return 0
        return self._basic_info(self.rows[index].key.name)[0]

    def is_selected(self, index):
        # type: (object) -> bool
        if not self.has_item(index):
            return False
        return self.cache.model.selected_key() == self.rows[index].key

    def get_item_count(self, index):
        # type: (object) -> str
        """数量标签显示**可用数**（决策 D4），不是总存量。

        `stored - reserved` 为 1 时留空：物品种类多的时候满屏的「1」只是
        噪声（同下半屏 `_slot_item_count` 的处理）。**排序仍按总存量**
        （决策 D7），因此这一列显示的数与排序依据并不是同一个量——那是
        已知差异，记在 `plans/ui/common_terminal_panel.md` 的待确认段。
        """
        if not self.has_item(index):
            return ""
        entry = self.rows[index]
        available = entry.stored - entry.reserved
        if available <= 1:
            return ""
        return str(available)

    # -- 排序 ----------------------------------------------------------

    def on_sort_method(self, params=None):
        # type: (object) -> None
        """切换排序方式：标识符 ↔ 数量。

        **没有会话时什么都不做。** 判据与 `on_select` 的那一条完全相同，
        理由也一样——只是那里早就有了、这里原先只挡了 `closed`。
        **不靠遮罩拦**：2026-10-01 真机实测，`unavailable_mask` 提到
        `layer: 39` 之后两个排序按钮**仍然点得到**（遮罩能拦内容区，
        拦不住这两个）。所以拦截落在回调这一层，遮罩只管表现。
        """
        if self.closed or not self.cache.client.has_session():
            return
        try:
            position = SORT_METHOD_CYCLE.index(self.sort_field)
        except ValueError:
            position = -1
        self.sort_field = SORT_METHOD_CYCLE[(position + 1) % len(SORT_METHOD_CYCLE)]
        self._apply_sort()

    def on_sort_order(self, params=None):
        # type: (object) -> None
        """切换方向。方式与方向是**两个独立状态**（`plans/ui/
        common_terminal_panel.md` 第 76-79 行）。

        会话判据与 `on_sort_method` 同一条：遮罩拦不住这个按钮。
        """
        if self.closed or not self.cache.client.has_session():
            return
        self.sort_descending = not self.sort_descending
        self._apply_sort()

    def _apply_sort(self):
        # type: () -> None
        """条件改完再查一次，并同步图标。

        `set_sort` 只改条件并推进代次，真正的重排发生在下一次取值时
        （`ItemListModel._invalidate`），因此同一帧里连点两下也只重排一次。
        """
        self.cache.model.set_sort(self.sort_field, self.sort_descending)
        self._sync_sort()
        self.refresh()

    def _sync_sort(self):
        # type: () -> None
        """把两个图标的 UV 写到位，**只在真的变了时写**。

        `SetUV` 每帧调用会让引擎每帧重新切图；排序是低频操作，没有理由
        让它跟着渲染帧走。
        """
        self._set_icon(SORT_METHOD_ICON_PATH, SORT_METHOD_UV[self.sort_field])
        self._set_icon(SORT_ORDER_ICON_PATH, SORT_ORDER_UV[self.sort_descending])

    def _set_icon(self, path, uv):
        # type: (str, tuple) -> None
        if self._sort_uv.get(path) == uv:
            return
        # **路径要先解析**：`path` 是内容区相对的，四台面板的屏幕挂法不同
        # （见 `slot_layout._candidates`），直接交给 `GetElement` 会在这三台
        # 上一条都取不到——而取不到的代价只是两个排序图标永远不换 UV。
        full = slot_layout.path_of(self.screen, path)
        if full is None:
            return
        try:
            self.screen.GetElement(full).asImage().SetUV(uv, ICON_UV_SIZE)
        except Exception:
            # 图标缺失或引擎拒绝都不该让排序本身失效：UV 只是表现，
            # 排序状态与列表内容都已经改完了。
            return
        self._sort_uv[path] = uv

    # -- 搜索 ----------------------------------------------------------

    def on_search_changed(self, params):
        # type: (object) -> None
        """输入框内容变了。**同一帧里多次输入只会重排一次**：`set_filter_text`
        只改条件，重排推迟到下一次取值（见 `_apply_sort` 的同一句说明）。

        会话判据与两条排序回调同一条：遮罩拦不住面板上的控件。
        """
        if self.closed or not self.cache.client.has_session():
            return
        text = _text(params.get("Text") or "").strip()
        if text == self.search_text:
            return
        self.search_text = text
        self.cache.model.set_filter_text(text)
        self.refresh()

    # -- 点击 ----------------------------------------------------------

    def on_select(self, params):
        # type: (object) -> None
        """点一行。**有选中就存进去，没选中就取出来。**

        - **下半屏选中了某个背包槽** → `inv_take`（背包 → 网络），数量是**一组**
          = `min(该槽数量, maxStackSize)`。这是「先点背包、再点终端行」那条路。
        - **没选中** → `inv_put`（网络 → 背包），数量 = `min(可用数,
          maxStackSize)`，并选中这一行。可用数不足一组时能拿多少拿多少——
          「取一组」的意思是**不超过一组**，不是「不够就一个都不给」。

        「存一组」与「取一组」互为镜像，两者都是**本轮定的默认，不是已定案
        的玩法**：R12 的手势映射定稿后（按住取半、右键取一个之类）改的是这里
        与 `BackpackModel.pending_take`。

        可用数为 0 时**不发命令**，只选中——`inv_put` 的 `count` 必须为正，
        发一个 0 只会换来一条 `OUT_OF_RANGE` 回执，而玩家什么也没做错。

        **两条路都不发 `cursor`**：R12 的持有物归属尚未定案，缺席**不等于**
        客户端声称「手里没拿东西」（`TerminalCommand` 的文档说得更细）。第一
        次点击不搬走任何东西，所以关闭界面时也没有悬空义务。

        **不做点击抑制**：连点两次就是两条命令。抑制（防抖、连击合并）属
        R12 的手势映射，本轮不发明；服务端的去重账本按 `request_id` 生效，
        管的是同一条请求被重发，不是两次独立点击。
        """
        index = params.get("#collection_index")
        if self.closed or isinstance(index, bool) or not isinstance(index, INTEGER_TYPES):
            return
        # **没有会话就什么都不做。** 遮罩虽然会出（`is_unavailable`），但它
        # 拦不拦得住输入**没有实测过**（`plans/ui/common_terminal_panel.md`
        # 第 3 节把「遮罩能否拦住面板外的排序按钮」列为待验）。真放进来时，
        # 下面那条 `inv_put` 会走到 `send_command` → `_require_session` 抛
        # `ValueError`——而这是 UI 回调，异常会冒到引擎的派发里，每次点击
        # 报一条。**判据取「会话在不在」，与 `_settle_lost_session` 同一套。**
        #
        # `_send_take` 那一支本来就不会炸（`inventory_slots` 随会话一起清空，
        # 拿不到快照就等待同步），但拦在这里比让两条路各靠一个偶然成立的条件
        # 安全——那种「碰巧不会炸」迟早会随别人的改动失效。
        if not self.cache.client.has_session():
            return
        # **样板终端的虚格选中优先**（11-05）：选中虚格之后点网络行是
        # 「清空那一格」，**不取物**。判据与背包那一侧共用同一个方法，
        # 因此两条路径不会各有一套「什么算外界」的说法。
        #
        # 放在**下标范围与会话检查之前**：这两条说的是「这一行有没有东西
        # 可搬」，而虚格选中要做的动作与那一行无关（那一行可能是空的、
        # 甚至列表还没收到快照）。`config`（`getattr` 而不是直取：本类不
        # import 屏幕类，与 `_fly` 取 `screen.fly` 同一手法）为 `None` 时
        # 这一行是空操作，通用／合成终端的行为因此一字不改。
        config = getattr(self.screen, "config", None)
        if config is not None and config.consumes_row_click(index):
            return
        if not 0 <= index < len(self.rows):
            return
        entry = self.rows[index]
        model = self.cache.model
        # **点终端行分两种含义，按「背包里有没有选中」分流。**
        #
        # - 背包选了一格 ⇒ 把它**存进网络**（`inv_take`，见 `_send_take`）。
        # - 否则 ⇒ **只选中这一行**，东西不动。
        #
        # 2026-10-01 User 改定：原先那条「点一行 = 直接取一组进背包」的默认
        # 撤掉了。现在两侧**对称**——选中一侧、点另一侧才搬。点中一条行只是
        # 把它选中，再点背包某一格才把这一行的东西放进那里（放进空格 / 合堆，
        # 异种由服务端拒，见 `on_backpack_put`）。
        if self.backpack is not None and self.backpack.selected is not None:
            model.select(entry.key)
            self._send_take(params)
            # **无论发没发出去都要返回。** 选中了背包槽而那一格已经空了时
            # `pending_take` 会给出 `None` 并清掉选中，此时**什么都不做**——
            # 绝不能落到下面那条「选中这一行」上：玩家选的是背包，把他的点击
            # 改判成「选中网络行」不是他要的那个动作。
            return
        # 同一条再点 = 取消选中（与背包侧「同格再点取消」对称）。
        if model.selected_key() == entry.key:
            model.clear_selection()
        else:
            model.select(entry.key)

    def on_backpack_put(self, ui_index):
        # type: (int) -> None
        """**把选中的网络行放进这个背包槽。** 手势的另一半（对 `on_select`）。

        服务端那边就是一条**带槽锚的 `inv_put`**：锚的期望指纹取**这一行**的
        键，于是三种结果一次说清，**不需要新命令、也不需要改协议**：

        - 目标槽是**空的** ⇒ `anchor_verdict(allow_empty=True)` 放行（只核版本）；
        - 目标槽**同种**   ⇒ 指纹相符，`probe_space` 把数量截到那一格装得下的
          量，也就是**合堆**；
        - 目标槽**异种**   ⇒ 指纹不符，`SLOT_CHANGED` 拒绝——**异种不交换**。
          这是 2026-10-01 定的：跨侧交换没有对应命令（`inv_swap` 是背包内两格），
          拿两条命令拼又不是原子的，中间失败会半途而废。
        """
        if self.closed:
            return
        client = self.cache.client
        if not client.has_session():
            return
        key = self.cache.model.selected_key()
        if key is None:
            return
        entry = None
        for row in self.rows:
            if row.key == key:
                entry = row
                break
        if entry is None:
            return
        slot = engine_slot_of(ui_index)
        view = client.inventory_slots.get(slot)
        # 拿不到那一槽的服务端快照就**等待同步**：槽版本是锚上的一关，用本地
        # 读数或空值绕过去，换来的只是服务端一条 `STALE_VERSION`。
        if view is None:
            return
        available = entry.stored - entry.reserved
        limit = self._stack_limit(key.name)
        count = available if available < limit else limit
        if count <= 0:
            # 可用数为 0（全被别人预留）时不发命令：`inv_put` 的 `count` 必须
            # 为正，发一个 0 只会换来一条越界回执，而玩家什么也没做错。
            return
        request_id = client.send_command(
            "inv_put",
            expected_index_version=client.index_version,
            key=key,
            count=count,
            slot=slot,
            slot_version=view["slot_version"],
        )
        # 记的是**这一行**的选中，回执只该清它——与 `_send_take` 记背包选中
        # 是同一个道理。两者共用 `_action_requests`，按 `kind` 分开收尾。
        self._action_requests[request_id] = ("row", key)
        # 落点就是**那一格**：这一次客户端真的知道是哪一格（是玩家点的）。
        self._fly(key, self._network_rect(key),
                  slot_layout.backpack_slot_rect(self.screen, ui_index))

    def _send_take(self, params=None):
        # type: (object) -> None
        """把选中的背包槽存进网络，一条 `inv_take`。

        **发之前重读那一格**（`pending_take` 里做），用的不是选中那一刻的
        快照：过期窗口因此从「一帧」压到「一次命令」，而且本地就能发现「选中
        之后东西被别处拿走了」——那时不发命令、只清选中，而不是发一条注定被
        拒的请求出去换一张 `SLOT_CHANGED` 回执。

        槽版本取本会话收到的服务端背包快照。快照尚未到达，或指纹与本地
        读数不符时不发命令，等待同步；不得用本地计数或空值绕过版本校验。
        服务端再次读取并验证该版本，观察过的 A→B→A 会拒绝旧请求。

        数量是**一组**（`pending_take` 的默认口径）；整槽那条路走
        `_send_quick_move`，是本方法的姊妹。
        """
        take = self.backpack.pending_take()
        if take is None:
            return
        slot, key, count = take
        ui_index = self.backpack.selected
        client = self.cache.client
        view = client.inventory_slots.get(slot)
        if view is None or view["canonical"] != key.canonical():
            return
        request_id = client.send_command(
            "inv_take",
            expected_index_version=client.index_version,
            key=key,
            count=count,
            slot=slot,
            slot_version=view["slot_version"],
        )
        # 记下**发命令时**的选中修订号：回执只该清这一个选中。
        self._action_requests[request_id] = (
            "bag", self.backpack.selection_revision())
        self._fly(key, slot_layout.backpack_slot_rect(self.screen, ui_index),
                  self._entry_target(params, key))

    def _send_quick_move(self, params=None):
        # type: (object) -> None
        """把选中的背包槽**整槽**存进网络，一条 `inv_quick_move`。

        与 `_send_take` 是姊妹：**同一套核账**（发之前重读那一格、对服务端
        快照核指纹与槽版本、核对不过是等待同步而不是换个法子发出去），
        差别只有两处——数量按**整槽**取（`pending_take(whole=True)`），
        动作名换成 `inv_quick_move` 并带上 `target="network"`。

        `inv_quick_move` 在服务端那侧的定义就是「整槽搬」（`target` 决定
        方向），与「点终端行 = 一组」是两条不同的命令；这里不自己造一个
        「整槽的 inv_take」，那样两边迟早对不上。

        **「尽可能多」由服务端裁**：网络装不下的部分回 `PARTIAL`，本地不
        预先截断——截断要读网络余量，而那是客户端看不到的东西。
        """
        take = self.backpack.pending_take(whole=True)
        if take is None:
            return
        slot, key, count = take
        ui_index = self.backpack.selected
        client = self.cache.client
        view = client.inventory_slots.get(slot)
        if view is None or view["canonical"] != key.canonical():
            return
        request_id = client.send_command(
            "inv_quick_move",
            expected_index_version=client.index_version,
            key=key,
            count=count,
            slot=slot,
            slot_version=view["slot_version"],
            target="network",
        )
        # 与 `inv_take` 同一条收尾：东西已经进网络了，那个指向空槽的选中
        # 没有意义。回执按同一张表清（`_settle_action` 不按动作分家）。
        self._action_requests[request_id] = (
            "bag", self.backpack.selection_revision())
        self._fly(key, slot_layout.backpack_slot_rect(self.screen, ui_index),
                  self._entry_target(params, key))

    def on_backpack_split(self, ui_index, params=None):
        # type: (int, object) -> None
        """**长按背包格：把这一槽的一半存进网络**（`inv_split`）。

        手势由 User 2026-10-01 定：**长按**（PC 与触控同一颗按钮，不需要
        组合键，也不需要区分左右键）。服务端那侧 `inv_split` 就是「带槽锚的
        `inv_take`，只取一部分」，因此**核账与 `_send_take` 完全相同**——
        发之前重读那一格、对服务端快照核指纹与槽版本、核对不过是等待同步。
        这里不另写一套：分开的代价是迟早有一份忘了重读。

        **先选中再取**：`pending_take` 读的是选中那一格。选中之后若那一格
        已经空了，`pending_take` 会返回 `None` 并清掉选中，于是这一下什么都
        不发——与「点了一个空格」同一种结局。

        **「一半」由 `pending_take(half=True)` 定义**（`该槽数量 // 2`），
        堆叠是 1 时它给出 `None`：一件东西拆不出两半，那一下就该什么都不做，
        而不是发一条 `count=0` 去换一张越界回执。

        回执走**同一条**收尾（`_settle_action` 不按动作分家）：成与不成都清
        选中，因为这半槽已经进网络了／那一次意图已经被服务端答复过了。
        """
        if self.closed or self.backpack is None:
            return
        client = self.cache.client
        if not client.has_session():
            return
        self.backpack.select(ui_index)
        take = self.backpack.pending_take(half=True)
        if take is None:
            return
        slot, key, count = take
        view = client.inventory_slots.get(slot)
        if view is None or view["canonical"] != key.canonical():
            return
        request_id = client.send_command(
            "inv_split",
            expected_index_version=client.index_version,
            key=key,
            count=count,
            slot=slot,
            slot_version=view["slot_version"],
        )
        self._action_requests[request_id] = (
            "bag", self.backpack.selection_revision())
        self._fly(key, slot_layout.backpack_slot_rect(self.screen, ui_index),
                  self._entry_target(params, key))

    def on_quick_store(self, params=None):
        # type: (object) -> None
        """点网络列表空白背景：把选中的背包槽整槽存进网络。

        **手势由 User 定（2026-10-01）**：先点背包槽选中，再点上方列表的
        空白处。**没有选中时什么也不做**——不发命令、也不改选中：那一下
        既不是「存什么」也不是「取什么」，没有可以借用的默认目标。

        判据与 `on_select` 那两条一样取「会话在不在」：没有会话时下面那条
        命令会走到 `send_command` → `_require_session` 抛 `ValueError`，
        而这是 UI 回调，异常会冒到引擎的派发里、每次点击报一条。
        """
        if self.closed or self.backpack is None:
            return
        if not self.cache.client.has_session():
            return
        self._send_quick_move(params)

    def on_backpack_exchange(self, ui_index):
        # type: (int) -> None
        """在**背包内部**动一下：合并或交换，一条 `inv_merge` / `inv_swap`。

        与 `_send_take` 是反面：那条把东西送进网络，这条只在背包里挪，**不改变
        网络库存**。两条共用一个选中态，所以互斥是天然的——选中背包槽之后点
        终端行仍然走 `inv_take`，点另一个背包槽走这里。

        **发之前重读两格**（`pending_exchange` 里做），并用服务端快照核对源槽
        的指纹与版本，与 `_send_take` 同一套：核对不过是**等待同步**，不是换一
        个法子把命令发出去。

        **目标槽只带版本、不带指纹**，与 `inv_merge` 的既有契约一致：服务端
        会重新读目标槽，把读到的东西当作期望值——因此那一关的指纹核对是空的，
        真正起作用的是版本。这不影响交换的正确性（交换是对称的，换到什么就是
        什么），但**换到的东西可能不是界面上刚显示的那一件**：槽版本只按「指纹
        变化」递增，观察间隔里的 A→B→A 识别不出来（S-15 记的边界）。
        合并受影响更大，所以 `pending_exchange` 先在本地比过同类；真到了服务端
        发现目标已经不是同类时，`slot_insert` 会以 `SLOT_OCCUPIED` 拒绝。
        """
        if self.closed or self.backpack is None:
            return
        # **会话没了就别再往下走。** 下面那几道核对（快照指纹、槽版本）
        # 在没有会话时是**碰巧**会拦下的（`inventory_slots` 随会话一起清空），
        # 与 `on_select` 当初补这条判据的理由一样：靠偶然成立的条件迟早会
        # 随别人的改动失效。而且这条是**动作**，不是表现。
        if not self.cache.client.has_session():
            return
        decision = self.backpack.pending_exchange(ui_index)
        if decision is None:
            return
        action, source_index, count = decision
        entry = self.backpack.entry(source_index)
        if entry is None:
            return
        source_slot = engine_slot_of(source_index)
        target_slot = engine_slot_of(ui_index)
        client = self.cache.client
        source_view = client.inventory_slots.get(source_slot)
        target_view = client.inventory_slots.get(target_slot)
        if source_view is None or target_view is None:
            return
        # **本地改过的槽与快照对不上，是应该的。** 那几格相对服务端的状态
        # 就是虚拟空间里的预测值——而快照是服务端**还没走到那一步**时的样子。
        # 逐条命令在服务端是**按序**执行的，轮到这一条时它那两格正好长成
        # 虚拟空间现在这样，所以发虚拟空间里的那个 key 才是对的。
        #
        # 只有**没改过**的槽才拿快照核对：那几格两边本来就该一致，不一致
        # 说明本地读数过期了，该等同步而不是发一条注定被拒的命令。
        pending = self.backpack.pending_slots()
        if (source_index not in pending
                and source_view["canonical"] != entry.key.canonical()):
            return
        # **本地改过的槽不发版本。** 那几格在服务端那边的版本客户端算不出来
        # ——服务端什么时候读到、读了几次取决于它自己的时序，而且交换与合并
        # 不一样（交换只复核目标槽，合并两格各复核一次）。发一个猜的版本
        # 只会换来一次 `stale_version` 拒绝，而玩家什么都没做错。
        # `None` 是 `SlotAnchor` 有意留的「不校验版本」。
        request_id = client.send_command(
            action,
            expected_index_version=client.index_version,
            key=entry.key,
            count=count,
            slot=source_slot,
            slot_version=(
                None if source_index in pending else source_view["slot_version"]
            ),
            into_slot=target_slot,
            into_slot_version=(
                None if ui_index in pending else target_view["slot_version"]
            ),
        )
        # **命令真的发出去了，才动画面。** 上面每一处 `return` 都是「等待
        # 同步」的意思，那几次点击不该改变任何东西。
        #
        # 装上之后**立刻清掉选中**：动作在画面上已经完成了，再留一个指向
        # 源槽的高亮会让玩家以为还能接着点。（没装上时不在这里清，那条路
        # 仍由回执去清——退化成加虚拟空间之前的行为。）
        if self.backpack.apply_exchange(
                request_id, action, source_index, ui_index, count):
            self.backpack.clear_selection()
        # 修订号取**上面那次清选中之后**的：这条命令对选中态该做的事已经
        # 在发送时做完了，回执回来时只要玩家没再动过选中，就什么都不用做。
        self._action_requests[request_id] = (
            "bag", self.backpack.selection_revision())
        # **同一份虚拟空间里的两个槽位**，两端都精确：这一条是唯一两个落点
        # 都拿得出真实坐标的搬运。目标槽是玩家刚点的那一格，所以**用
        # `ui_index` 算坐标，而不是用源槽**——写反了就是物品从目标飞回源槽。
        self._fly(entry.key,
                  slot_layout.backpack_slot_rect(self.screen, source_index),
                  slot_layout.backpack_slot_rect(self.screen, ui_index))

    # -- 动画 ----------------------------------------------------------

    def _fly(self, key, source, target):
        # type: (object, object, object) -> None
        """发起一次搬运动画。**只在命令真的发出去之后调。**

        两端是**全局矩形** `(x, y, 宽, 高)`，取不到就传 `None`；换算成控件
        坐标、缓动与收尾都在 `flying_item.FlyingItemAnimator` 里。这里只
        做一件事：**保证表现层的问题不会冒到搬运路径上**——控件缺席、坐标
        拿不到、引擎拒绝，都只是「这一次不飞」。

        走 `getattr` 取动画器：`TerminalListView` 是被面板装上去的，而测试
        与别的复用方装的可能是不带动画的宿主；那种场合少一个飞行动画，
        不该让搬运失败。
        """
        animator = getattr(self.screen, "fly", None)
        if animator is None:
            return
        animator.fly(key, source, target)

    def _entry_target(self, params, key):
        # type: (object, object) -> object
        """**「存进网络」这一向的落点：玩家点的那一下在哪儿，就飞到哪儿。**

        2026-10-01 按 User 的口径改的。此前落到 `_network_rect(key)`，也就是
        「这一件在网络列表上的行，没有行就落到列表区中心」——而那一行本来就
        不是玩家点的地方（整槽存入那一条点的是列表的**空白**处，那里没有行），
        于是动画朝着一个与手势无关的位置飞。飞向点击处就与玩家的动作对上了。

        坐标来自绑定回调的 `params`（`TouchPosX/TouchPosY`，**UI 坐标**，与
        `GetGlobalPosition()` 同一套），**不是现问鼠标**：要的是「点的那一
        下在哪儿」，动画开始时鼠标可能已经挪走了。

        取不到就退回 `_network_rect(key)`——键盘/手柄触发、老引擎不带这两个
        键时，落到「该去的那一行」仍然比不飞好。`_network_rect` 因此没有被
        取代，它还是这条路线的兜底，也是「从网络取出来」那一向的落点。
        """
        point = slot_layout.click_rect(params)
        if point is not None:
            return point
        return self._network_rect(key)

    def _network_rect(self, key):
        # type: (object) -> object
        """这一件在网络列表上的落点。

        **它已经有行就落到那一行，没有就落到列表区中心**——后者是真的：
        网络里没有这一件时，屏幕上也没有它落脚的地方，落点只能是「那一片
        区域」。落到别处（比如玩家刚点的那一行）是在画一件客户端并不知道
        的事。

        `index_of` 给的是**当前可见顺序**里的下标，而滚动位置会变，所以
        坐标现问 `slot_layout`，不缓存。
        """
        index = self.cache.model.index_of(key)
        if index >= 0:
            rect = slot_layout.network_row_rect(self.screen, index)
            if rect is not None:
                return rect
        return slot_layout.network_area_rect(self.screen)

    def _keepalive(self):
        # type: () -> None
        """每 `KEEPALIVE_FRAMES` 帧告诉服务端「这个会话还开着」。

        **放在列表视图里而不是各个面板的 `OnTicking`**：三个终端面板
        （通用／合成／样板）都建了这个视图、都每帧调 `tick()`，因此这里是
        唯一的收口处——放进面板的话要写三遍，而漏掉一个的症状是「那个界面
        开着不动 20 秒就死」，看起来像别处的问题。

        没有会话时**只把计数清零**，不发：`keepalive` 需要会话号，而这不是
        错误（界面刚打开、会话还在路上）。`_settle_lost_session` 已经在
        这一步之前把丢会话的情形收过尾了。

        `keepalive` 抛异常时咽掉并清零：它是保活，不是业务，一次网络抖动
        不该让 `OnTicking` 每帧报错（而引擎把那个异常算在渲染里）。
        """
        client = getattr(self.cache, "client", None)
        if client is None or not client.has_session():
            self._keepalive_frames = 0
            return
        self._keepalive_frames += 1
        if self._keepalive_frames < KEEPALIVE_FRAMES:
            return
        self._keepalive_frames = 0
        try:
            client.keepalive()
        except Exception:
            pass

    # -- 每帧 ----------------------------------------------------------

    def tick(self):
        # type: () -> None
        """推进一帧：先记锚点，再收包，最后按需重画。"""
        if self.closed:
            return
        self._settle_lost_session()
        self._keepalive()
        self._drain_receipts()
        # **顺序不能反**：重排发生在 `cache.tick()` 里面，等它跑完再读滚动
        # 位置，读到的是「换过之后」的顶行——那时玩家正看着的那一行已经
        # 换了人，锚点就锚到了别人身上。
        self._remember_anchor()
        result = self.cache.tick()
        if result is None:
            return
        if not result.reordered and not (set(result.changed_keys) & self._shown):
            return
        self.refresh()
        # **不是只在 `reordered` 时重锚。** 删掉一行不置「重排」位——其余行的
        # 相对顺序确实没变——但锚点行上面少了几行之后，它的**下标**变了，而
        # 滚动条记的是百分比。只在重排时重锚的话，玩家会看着列表莫名往上跳。
        # 写回的值与当前滚动位置相同时不会来回拉锯：这个值就是从当前滚动位置
        # 反推出来的那一行算回去的。
        self._restore_anchor(result)

    def refresh(self):
        # type: () -> None
        """把模型的当前可见行抓成一份快照，供集合绑定按 index 读取。

        `rows` 与引擎这一帧渲染的格子必须同源，因此这里一次取全（决策 D2
        的整表渲染）。筛选后行数变少时，多出来的格子由 `has_item` 报假而
        不可见。
        """
        if self.closed:
            return
        model = self.cache.model
        generation, keys = model.query()
        self.rows = model.query_page(generation, 0, len(keys))
        self._shown = set(keys)

    def _remember_anchor(self):
        # type: () -> None
        """把「玩家正盯着哪一行」告诉模型。

        顶行由滚动条的百分比折算——`ScrollViewUIControl` 只给百分比，
        没有「第几行」这种概念。折算必然有误差（一格的高度未必整除），
        因此它只用来找**一个近似的落点**，真正的身份是那个资源键。
        """
        if self._scroll is None:
            return
        rows = self.rows
        if not rows:
            self.cache.model.set_anchor(None, 0)
            return
        try:
            percent = self._scroll.GetScrollViewPercentValue()
        except Exception:
            return
        # 百分比按文档是整数，但**不假设**：引擎真回一个浮点时，整数检查
        # 会让重锚整条静默失效，而表现只是「滚动位置偶尔会跳」——那种 bug
        # 进游戏也未必看得出。取整一次，两种都收。
        if isinstance(percent, bool) or not isinstance(percent, INTEGER_TYPES + (float,)):
            return
        index = int(percent) * len(rows) // 100
        if index >= len(rows):
            index = len(rows) - 1
        self.cache.model.set_anchor(rows[index].key, index)

    def _restore_anchor(self, result):
        # type: (object) -> None
        """重排之后把锚点那一行拉回视野。

        锚点没了（那一行被删掉或被筛掉）时退到 `anchor_offset`——「它原来
        占着第几行」，与 `ItemListModel._reanchor` 的口径一致。
        """
        if self._scroll is None or result.anchor_key is None:
            return
        model = self.cache.model
        index = model.index_of(result.anchor_key)
        if index < 0:
            index = result.anchor_offset
        rows = model.count()
        if rows <= 0 or index >= rows:
            return
        percent = index * 100 // rows
        try:
            self._scroll.SetScrollViewPercentValue(percent)
        except Exception:
            return

    def _settle_lost_session(self):
        # type: () -> None
        """会话没了就把在途动作与背包选中一起收尾。

        **没有这一条，界面会卡在一个「假活」的状态上。** 服务端判死会话时
        只回一条 `error`（不是回执），回执那一关永远等不到——于是
        `_action_requests` 与选中一起悬着：选中框赖着不走，点别的背包槽也
        只会再发一条没有下文的命令（`_click` 见有选中就走交换，根本不改
        选中），点终端行又因为选中还在而发 `inv_take`。玩家看到的是「点什么
        都没反应」。

        **判据取「会话还在不在」，不取「收到了哪条错误」。** 那样对所有丢
        会话的原因都成立：服务端判死、玩家走远、换维度、设备被换掉、以及
        将来任何新的原因，都不必在这里再列一遍。

        每帧调用无代价：没有东西要收的时候第一次判断就返回了。
        """
        if self.cache.client.has_session():
            return
        # 状态快照与会话一起失效；即使没有在途动作，也不能残留在线与弹窗。
        self.screen.status = None
        if self.backpack is None:
            self._action_requests = {}
            return
        if (not self._action_requests and self.backpack.selected is None
                and not self.backpack.has_pending()):
            return
        self._action_requests = {}
        # **本地操作也要撤。** 它们是一份「服务端答应过但还没兑现」的画面，
        # 而会话都没了，那份答应已经作废。留着它，界面会一直显示一个不存在
        # 的交换结果，玩家点哪儿也都不对。
        self.backpack.clear_pending()
        # **选中态不持有物品**（`BackpackModel` 的第三条纪律），所以丢掉它
        # 不欠任何东西。
        self.backpack.clear_selection()

    def _drain_receipts(self):
        # type: () -> None
        """取走命令回执。**必须取**：不取 `NetworkClient._results` 只涨不落。

        回执目前**没有显示位置**（`plans/ui/common_terminal_panel.md` 的
        待确认段记了这一缺项），因此取走之后只留最近几条供诊断——不是丢掉
        判断，而是这一轮没有能放它的控件。

        例外是**我们自己发出的那条要清选中的命令**（`inv_take`，或背包内部的
        `inv_merge` / `inv_swap`）：它的回执要用来收尾选中态（见
        `_settle_action`），因此不能只当展示数据。

        **取走的这一批要转给 `receipt_sink`**（如果装了）：它就是为此存在的
        ——回执队列是「取走即出队」，同一个界面上的第二个消费者（合成终端的
        工作台）若自己再取一次，两处会互相吃掉对方的条目，而列表这边被吃掉的
        是「清掉选中」的信号，症状是选中框赖着不走。
        """
        taken = self.cache.client.take_command_results()
        if not taken:
            return
        for message in taken:
            self.receipts.append(message.result)
            self._settle_action(message.result)
        del self.receipts[:-RECEIPT_MEMORY]
        if self.receipt_sink is not None:
            self.receipt_sink(taken)

    def _settle_action(self, result):
        # type: (dict) -> None
        """背包动作的回执到了：**无论成败都清掉选中**。

        「动作」是三种之一：`inv_take`（存进网络）、`inv_merge`（背包内合并）、
        `inv_swap`（背包内交换）。三者对选中态的处理完全相同，因此共用这一处
        ——**别按动作分家**：两条路各写一份，迟早有一条忘了清，而表现是「选中
        赖着不走」，玩家下一次点击会拿一件他没选过的东西去操作。

        - **成功**：东西已经搬完了，界面上那两格很快会变（下一轮轮询），留着
          一个指向它的选中没有意义。
        - **被拒**（多半是 `SLOT_CHANGED`：选中之后那一格被别处改了）：**更
          要清**，否则玩家会在同一条死路上反复点。清完立刻重读一次，让他看到
          引擎的真实状态，而不是继续看着一个已经不存在的东西。

        「发送即清」是不行的——被拒时会把玩家的选中白白吞掉，而他什么都没做
        错。回执就是用来区分这两种结局的，清了它就没法区分了。

        只认 `request_id` 对得上的那一条：队列里还有别的命令的回执，按顺序猜
        会把别人的结果算到自己头上。
        """
        request_id = result.get("request_id")
        if request_id not in self._action_requests:
            return
        kind, token = self._action_requests.pop(request_id)
        if kind == "row":
            # **这一条动的是网络行那一侧的选中。** 两侧的选中是互斥的
            # （见 `TerminalPanelBase._click`），所以收尾时只需要管自己这
            # 一半；`_settle_action` 的其余部分（撤本地操作、重读背包）
            # 都不是给这一条用的，直接返回。
            if self.cache.model.selected_key() == token:
                self.cache.model.clear_selection()
            return
        if self.backpack is None:
            return
        revision = token
        # **只在「这条命令发出去之后玩家没再动过选中」时才清。** 回执清的是
        # **发命令时**的那个选中，不是「收到回执那一刻的选中」——两者之间玩家
        # 完全可能取消又重选。清了新选中的后果很具体：下一次点终端行会落到
        # `inv_take` 之外的地方，**方向与玩家的意图相反**。
        if self.backpack.selection_revision() == revision:
            self.backpack.clear_selection()
        if result.get("status") != STATUS_OK:
            # 失败了：把这一条本地操作撤下来，画面上那两格立刻回到它所依据
            # 的样子。**只撤这一条**——后面几轮玩家做的操作还站得住。
            self.backpack.drop_pending(request_id)
        # **成不成都要重读真实背包。** 回执是「可以去看真值了」的信号，
        # 而真值才是唯一权威：`replace_from_engine` 会把这次读数灌进
        # `_base`，再按序重放还没兑现的那些操作。
        #
        # 注意成功的这一条**不在这里退场**——它要等真值真的变成它的结果
        # 才退（`_retire`）。按回执退会让画面先跳回操作前的样子、再跳过来。
        self.backpack.replace_from_engine()
        # 重画不用管：选中态挂在面板的集合绑定上，引擎下一渲染帧自己来取。
        # 这正是上下两半能各管各的、又不必互相认识的原因。

    def diagnostics(self):
        # type: () -> dict
        animator = getattr(self.screen, "fly", None)
        return {
            "closed": self.closed,
            "rows": len(self.rows),
            "sort_field": self.sort_field,
            "sort_descending": self.sort_descending,
            "search_text": self.search_text,
            "anchored": self.cache.model.anchor()[0] is not None,
            "receipts": list(self.receipts),
            # **`errors` 是这里唯一会回答「动画为什么不动」的数**：控件缺席
            # 与引擎拒绝都收在里面（见 `FlyingItemAnimator`），而它们的表现
            # 一样——屏幕上什么都没发生。
            "animation": (None if animator is None else animator.diagnostics()),
            "cache": self.cache.diagnostics(),
        }

    def close(self):
        # type: () -> None
        """注销收包、发关闭包并清表。可以重复调用。

        **没有「摘绑定」这一步**：绑定在类体里、跟着界面走，不跟会话走。
        收尾靠的是 `closed`——引擎照样回调，但那十条一律先看列表视图在不在，
        而面板已经把 `list_view` 置空了。
        """
        if self.closed:
            return
        self.closed = True
        self.cache.client.port.unlisten()
        # 收包回调也要摘：设备状态那条挂在**面板**上而不是本视图上（它不
        # 随会话生灭），不摘的话界面关了回调还在跑。`port.unlisten` 只摘
        # 引擎事件，管不到 `on_message` 那张表。
        self.cache.client.clear_handlers()
        self.cache.close()
        self.rows = ()
        self._shown = set()
        # 在途的命令与本地操作都不再有人收尾：视图都拆了，回执回来也没有
        # 界面可清。**不需要替玩家把东西放回去**——那些命令要么已经由服务端
        # 执行完，要么从来没动过；虚拟空间只是画面上那一份，丢掉它不欠任何
        # 东西（这正是「不写引擎背包、不写网络」换来的）。
        self._action_requests = {}
        self.backpack = None
        self._scroll = None


@RegistToolDeltaScreen("common_terminal_panel_ui.main")
class CommonTerminalPanel(TerminalPanelBase):
    """通用终端：上列表、下背包。

    列表**只在这个界面拿到了权威的终端身份时才接**（`_init_params` 里的
    `device_ref`，由 `PushUIRequest` 从服务端带来）。没有它就只显示空列表，
    不占位、不伪造一个设备——按 11.md 的 UI 前置约定，缺交付时相关逻辑保持
    暂停，而不是先写一段假设。`OpenDemo()` 走的正是这条路。

    `list_view` 初值是基类的类属性 `None`（绑定比本类的 `OnCreate` 更早生效，
    所以它不能靠实例属性），这里只在真的拿到身份时才赋值。
    """

    def OnCreate(self):
        TerminalPanelBase.OnCreate(self)
        params = self._init_params
        # `PushUIRequest` 的 params 过了引擎与消息层两道，`device_ref` 到这里
        # 是个**字典**（`DeviceRef.to_dict()` 的产物），不是 `DeviceRef`。少了
        # 这一步还原，`open_session` 会炸在 `device_ref.to_dict()` 上；而换成
        # 自己拼一个形状像的东西，后果更糟——位置错了的会话会被服务端拒掉，
        # 症状与「终端没反应」完全一样。
        ref = params.get("device_ref")
        if ref is None:
            return
        try:
            device_ref = DeviceRef.from_dict(ref) if isinstance(ref, dict) else ref
        except (KeyError, TypeError, ValueError):
            # 身份破了就按「没有权威身份」处理：照 11.md 的 UI 前置约定暂停，
            # 而不是拿半个位置去开会话。
            return
        if not isinstance(device_ref, DeviceRef):
            return
        dimension = params.get("dimension", 0)
        client = NetworkClient()
        # 先挂收包再开会话：`session_ready` 可能比这个函数返回还早。
        client.port.listen(client.receive)
        #: 设备总体状态（11-13）。会话一开，服务端下一帧就会推一份快照，
        #: 因此**收包回调必须早于 `open_session`**——晚一步那一份就永远
        #: 补不上了（这正是 `session_ready` 那条注释的同一个理由）。
        self.status = device_status.DeviceStatusModel(device_ref)
        client.on_message(self._make_status_handler(client))
        self.list_view = TerminalListView(
            self,
            cache=TerminalCache(client=client),
            device_ref=device_ref,
            dimension=dimension,
            # 上下两半共用**同一个**背包模型：列表视图要在交接回执回来后清掉
            # 选中，而选中态归面板持有。
            backpack=self.backpack,
        )
        client.open_session(device_ref, dimension)

    def OnTicking(self):
        # **基类的背包轮询要显式调**：本方法没有 `super()`（下面 `tick()` 的
        # 时序有讲究），所以基类不能靠方法名自动接上。飞行动画同理——它挂在
        # 基类上，而推进它的只有这一处。
        self.fly.tick()
        self._poll_backpack()
        if self.list_view is not None:
            self.list_view.tick()

    def OnDestroy(self):
        if self.list_view is not None:
            self.list_view.close()
            self.list_view = None
        # 绑定不随会话生灭（类体里那十几条），因此**置空**就是全部的清理：
        # 它们照样会被引擎问，而每条都先看 `self.status` 在不在。
        self.status = None
        TerminalPanelBase.OnDestroy(self)

    @classmethod
    def OpenDemo(cls):
        """客户端手工自查用：在 `UiInitFinishedEvent` 注册过界面之后调。

        **不再灌演示物品。** 早先它往 36 格里塞假的 `Item` 以预览「合堆溢出、
        异类交换、空格移动」——那几条都是**本地模拟背包**的语义，面板换成真
        背包之后已经不存在了；而 `plans/technical_notes/
        netease_container_screens.md` 记着那些演示物品在游戏里**根本没画出来
        过**。现在下半屏显示的就是玩家自己的背包，要看渲染就往背包里放几件
        东西，比一份假数据更可信。

        列表那一半照旧是空的：本入口没有权威身份，按 `OnCreate` 的前置约定
        整块暂停，不占位、不伪造。

        **走 `PushUI`，不是 `CreateUI`。** 本屏是 `is_proxy=False` 的脚本屏，
        打开方式由真实入口定死：服务端 `PushUIRequest` →
        `tooldelta/ui/event_handler.py:22` 调的就是 `PushUI`，手工自查这条没有
        理由拐到另一条路上去。真机上 `CreateUI` 这一条**失败过**——
        `clientApi.CreateUI` 返回 `None`，上游判 `isinstance` 抛
        `CreateUI failed`（`plans/technical_notes/netease_container_screens.md`
        §五 2；ModSDK 文档给 `CreateUI` 记的限制是 `uiKey` 重名即创建失败）。
        """
        return cls.PushUI({})
