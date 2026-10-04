# coding=utf-8
"""样板终端界面（11-05）：通用终端的一切 + 一份样板配置区。

上半屏的网络列表、下半屏的玩家背包、搜索、排序、遮罩、关闭全部沿用
`TerminalPanelBase` 与 `TerminalListView`，**绑定名仍是 `#EnderNetTerminal.*`**
——样板终端不是另一个终端，它是同一个终端的另一种面板。本文件加的是：

1. **四个格子区**（`TemplateView`）：虚拟输入九宫格、虚拟输出格、真实输入槽、
   真实输出槽。四者的显示全部来自服务端每帧推的 `template_state`
   （`TemplateStateMessage`），**客户端不自己算配置**。
2. **九条编辑命令**：切模式、虚格放入／清空／改数量、制作、真实槽的取放。

## 双向模型（交接第 4、5 节）

- **背包或网络列表有选中** → 点虚格 = 把那件物品**复制**成配置（源槽不消耗、不移动）；
- **选中虚格** → 点外界（**背包槽或网络列表行**）= **仅清空那一格的配置**，
  外界不增加、不替换、不合并物品，那一下**也不取物**；
- 点已选中的同一格 = 取消；点另一格 = 选中挪过去。

第二条是本界面唯一会**改共享基类行为**的地方，因此走的是一个显式的协作口子
（`TerminalPanelBase.config`），而不是覆盖绑定方法——绑定按方法名从 `dir(cls)`
收集，同名覆盖会让两个版本都注册进去，引擎取哪个是未定义的。

## 长按改数量

长按虚格弹 `number_set_window`（User 2026-10-02），范围 1～65535。上限取自
**服务端推来的 `limits`**，客户端不写死——那个数是服务端的策略值，只有一处。

## 修订号：本地号就是「最后一次收到的那个」

每条配置编辑都带上 `slot_version = self.revision`（最后一次推送里的号）。
**客户端不做乐观推进**：乐观推进要在「这一条其实被拒了」时回退，而回退本身
又是另一场比赛。这样做的代价只有一条：**同一帧里连点两次编辑，后一条可能被判
过期**。它会在下一次推送到达后自愈（推送带来新号，界面也刷新成真实内容），
而玩家看到的是「第二格没上」——再点一下即可。

## 已知缺项（如实记，不补假实现）

- **本界面自己的动作不驱动 `flying_item`**：虚格里的东西是配置、不是实物；
  真实槽那三条命令（放入／取回／取出结果）也没画动画——起终点要现算控件
  矩形，而那三个格子的运行时路径**刚刚才量出来**
  （`/template_panel/template_input_slot` 等，2026-10-02）。**继承来的**那条
  交互（背包↔网络列表、整槽存入）照常飞：它走的是 `TerminalListView`，
  共用 `slot_layout` 的路子，2026-10-02 那条「一切路径都取不到」的缺陷已经
  修好（见 `slot_layout._candidates`）。
- **失败原因没有显示位置**：与通用／合成终端同一处境，原因码只进
  `last_error` 与 `diagnostics()`，界面上没有控件显示它。
- **滚动重锚缺席**：本界面的滚动控件路径同样没有运行时结论，因此
  `TerminalListView` 在这里不接管滚动条（`scroll_path=None`）。
"""

from functools import partial

from ....tooldelta.ui import Binder, RegistToolDeltaScreen
from . import stack_size
from .number_set_window import WINDOW_ATTRIBUTE as NUMBER_WINDOW_ATTRIBUTE
from .number_set_window import show_number_set_window
from .terminal_cache import TerminalCache
from .terminal_panel import (
    LONG_PRESS_SECONDS,
    TerminalListView,
    TerminalPanelBase,
)
from .widgets import device_status
from ..network_client import NetworkClient
from ...common.identity import DeviceRef
from ...common.protocol import MSG_TEMPLATE_STATE, STATUS_OK, Reason

try:
    INTEGER_TYPES = (int, long)
except NameError:
    INTEGER_TYPES = (int,)

try:
    TEXT_TYPE = unicode
except NameError:
    TEXT_TYPE = str

#: 新增控件的绑定前缀。**通用终端已有的那几条一律沿用 `#EnderNetTerminal.*`**
#: （搜索、排序、网络列表、背包、快捷栏、遮罩、关闭），不另立一套。
BINDING_PREFIX = "#EnderNetTemplate."

#: 四个格子区。**每个区一个集合**：集合绑定的回调只拿得到集合内下标、
#: 拿不到集合名（`terminal_panel.SEGMENTS` 那条），共用一套的话后面的区会按
#: 前面的区的下标去读。集合名必须与 JSON 里 `collection_name` 逐字相同。
SURFACE_SOURCE = "source"
SURFACE_OUTPUTS = "outputs"
SURFACE_INPUT = "input"
SURFACE_RESULT = "result"
SURFACES = (
    (SURFACE_SOURCE, "endernet_template_source"),
    (SURFACE_OUTPUTS, "endernet_template_outputs"),
    (SURFACE_INPUT, "endernet_template_input"),
    (SURFACE_RESULT, "endernet_template_result"),
)
COLLECTION_OF = dict(SURFACES)
#: 每个区有几格。**与 `template_terminal` 的同名常量是同一个数**；
#: 这里再写一份是因为客户端要按它给绑定答下标范围，不能跟着对面变。
CELLS_OF = {
    SURFACE_SOURCE: 9,
    SURFACE_OUTPUTS: 3,
    SURFACE_INPUT: 1,
    SURFACE_RESULT: 1,
}
#: 哪几个区是**虚拟**的（可编辑配置）。真实槽那两个不做虚格编辑。
VIRTUAL_SURFACES = (SURFACE_SOURCE, SURFACE_OUTPUTS)

#: 合成模式下**唯一会显示**的输出格：中间那一格，服务端把检测到的产物放在
#: 这里（上下两格 User 2026-10-02 定：不显示）。
CRAFT_PREVIEW_CELL = 1
#: 长按那条手势只挂在虚格上——真实槽的点击是取放，没有「改数量」这回事。
LONG_PRESS_SURFACES = VIRTUAL_SURFACES

MODE_CRAFTING = "crafting"
MODE_FACTORY = "factory"
DEFAULT_MODE = MODE_CRAFTING
MODE_LABELS = ((MODE_CRAFTING, u"合成样板"), (MODE_FACTORY, u"工厂样板"))

#: 标题文本。**只在这里拼一次**：JSON 里那个 label 是静态的，模式一换就要
#: 由脚本改（交接第 3 节「切换后应同步模式文字」）。
#:
#: 措辞由 User 2026-10-02 定：`样板配置 - 合成样板` / `样板配置 - 工厂样板`。
#: 分隔符是**半角连字符加空格**，与 JSON 里那个静态初值（`（模式： …）`）
#: 不是同一套写法——静态那个只是资源初值，真机上看到的一律以这里为准。
TITLE_FORMAT = u"样板配置 - %s"

#: 标题 label **相对屏幕基底**的路径。
#:
#: 基底取 tooldelta 的 `SCREEN_BASE_PATH`
#: （`/variables_button_mappings_and_controls/safezone_screen_matrix/inner_matrix/`
#: `safezone_screen_panel/root_screen_panel`），**不自己抄一份**：那串是引擎的
#: 结构，抄一份只会在它变化时静默取不到控件，而「取不到」在这里只表现为
#: 「标题不写」——界面上看不出与「没接」的区别。
#:
#: 本界面的内容是通过 `$screen_content` **内联**进 `root_screen_panel` 的，
#: 没有通用终端那层 `/terminal`（那台是手写的 `controls`，所以它有）；
#: 内联之后「从基底怎么走到它」就是这一条。
TITLE_PATH = "/template_panel/title"


def title_control_path():
    # type: () -> object
    """`<屏幕基底>/template_panel/title`；拿不到基底时返回 `None`。

    **导入放在函数里**：`tooldelta/ui/utils.py` 在**模块级**就
    `import mod.client.extraClientApi`，离线（没有引擎）时整个包导不进来
    ——与 `OnKeyPressInGame`、`_listen_key_event` 那条是同一个理由：
    界面代码必须能离线导入，否则 `endernet_tests` 会一起报废。
    """
    try:
        from ....tooldelta.ui import SCREEN_BASE_PATH
    except ImportError:
        return None
    return SCREEN_BASE_PATH + TITLE_PATH

#: 六个动作名，**与 `StorageService.handle` 的动作表逐字相同**。
ACTION_MODE = "template_mode"
ACTION_CELL_PUT = "template_cell_put"
ACTION_CELL_CLEAR = "template_cell_clear"
ACTION_CELL_COUNT = "template_cell_count"
ACTION_ENCODE = "template_encode"
ACTION_INPUT_PUT = "template_input_put"
ACTION_INPUT_TAKE = "template_input_take"
ACTION_OUTPUT_TAKE = "template_output_take"

#: **要带配置修订号的那几条。** 其余三条（真实槽的取放）的 `slot_version`
#: 是**背包槽锚**，意思完全不同——一张表说清哪个字段在哪条命令里什么意思。
CONFIG_ACTIONS = frozenset([
    ACTION_MODE, ACTION_CELL_PUT, ACTION_CELL_CLEAR, ACTION_CELL_COUNT,
    ACTION_ENCODE,
])

#: 面给玩家的失败原因。**只列界面必须点出来的那几条**，其余退化成空串。
REASON_TEXTS = {
    Reason.TEMPLATE_CELL_LOCKED: u"这一格在当前模式下不能改",
    Reason.TEMPLATE_CONFIG_INVALID: u"当前配置还构不成一块样板",
    Reason.SLOT_OCCUPIED: u"那一格已经有别的东西了",
    Reason.NO_CAPACITY: u"背包放不下",
    Reason.OUT_OF_RANGE: u"数量或格子不对",
    Reason.STALE_VERSION: u"配置刚被别人改过，等界面刷新后再试",
    Reason.PATTERN_KIND_MISMATCH: u"输入槽只收工厂样板",
    Reason.PATTERN_NOT_A_TEMPLATE: u"那不是本模组的样板",
    Reason.INSUFFICIENT_STOCK: u"背包那一格里没有这块样板",
}

#: 留多少条回执供诊断。与 `terminal_panel.RECEIPT_MEMORY` 同一口径与同一理由。
RECEIPT_MEMORY = 8


def _text(value):
    # type: (object) -> object
    """把过线来的字节串转成文本，其余原样。

    **必须收 `str`（py2 的字节串）**，不能只认 `unicode`：服务端那一侧
    `ResourceKey.name` 是 unicode，但**过一趟线（msgpack / JSON 往返）回来
    就是 `str`**。只认 unicode 的写法会把一个好好的名字换成空串，症状是
    「格子里有配置、画面上没有图标」——`id_aux` 恒为 0，而界面上看不出
    是没数据还是没渲染。`terminal_panel._text` 是同一份东西，这里再写一遍
    的理由与本文件别处的 `INTEGER_TYPES` 相同：客户端每个界面模块各留一份
    常量，不跨模块 import 私有名。
    """
    if isinstance(value, TEXT_TYPE):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


class TemplateView(object):
    """四个格子区的**客户端显示模型 + 交互状态机**。引擎无关，可离线整条跑。

    **它不持有任何权威状态**：格子里有什么、模式是什么、哪一格能不能改，
    全部照抄服务端推来的 `template_state`。本地能决定的只有「选中了哪一格」
    「在等哪几条回执」。
    """

    def __init__(self, screen, client=None):
        # type: (object, object) -> None
        self.screen = screen
        self.client = client if client is not None else NetworkClient()
        self.closed = False
        #: 最后一次收到的配置修订号。-1 = 还没收到过，此时任何一条都接受。
        self.revision = -1
        self.mode = DEFAULT_MODE
        #: 区名 -> 行元组。行是服务端给的那个形状（含 `locked`）。
        self.rows = dict((surface, ()) for surface, _c in SURFACES)
        self.limits = {"max_count": 1, "default_count": 1}
        #: 选中的虚格 `(区名, 下标)` 或 `None`。**只有一份**，因此「点另一格
        #: 就把选中挪过去」与「跨区也只能选一个」都是自然结果。
        self.selection = None
        #: 每个虚格的按下记录 `区名 -> (下标, 时刻, 是否已触发长按)`。
        #: **每区各一份**：两区的下标在不同的空间里，共用一份会把 A 区的长按
        #: 算到 B 区头上。
        self.presses = {}
        self.receipts = []
        self.last_error = None
        self.errors = 0
        self._requests = {}

    # -- 收包 ----------------------------------------------------------

    def apply(self, message):
        # type: (object) -> bool
        """收一份 `template_state`。**旧版本一律丢掉**，返回是否采纳。

        「相等也采纳」是有意的：同一条推送重发一次是幂等的，而丢掉它会让
        界面停在一个更旧的画面上（`revision` 只增不减，等号那一支才是常态）。
        """
        revision = getattr(message, "revision", None)
        if (isinstance(revision, bool) or not isinstance(revision, INTEGER_TYPES)
                or revision < self.revision):
            return False
        self.revision = revision
        mode = getattr(message, "mode", None)
        if mode in (MODE_CRAFTING, MODE_FACTORY):
            self.mode = mode
        for surface, _collection in SURFACES:
            rows = getattr(message, surface, None) or ()
            self.rows[surface] = tuple(rows)
        limits = getattr(message, "limits", None)
        if isinstance(limits, dict):
            self.limits = dict(limits)
        # 服务端的真相一进来，过期的选中就不该留着：那一格可能已经空了。
        if self.selection is not None:
            surface, index = self.selection
            if self.cell(surface, index) is None:
                self.selection = None
        return True

    # -- 格子（绑定读的就是这几个） ------------------------------------

    def raw_row(self, surface, index):
        # type: (str, object) -> object
        """一格的服务端行（含 `locked`），越界或没有返回 `None`。

        `bool` 与越界一律挡掉：集合回调的下标是外部输入，`isinstance(True, int)`
        为真，而 `rows[-1]` 不报错、取的是最后一格。
        """
        if isinstance(index, bool) or not isinstance(index, INTEGER_TYPES):
            return None
        rows = self.rows.get(surface) or ()
        if not 0 <= index < len(rows):
            return None
        row = rows[index]
        return row if isinstance(row, dict) else None

    def cell(self, surface, index):
        # type: (str, object) -> object
        """一格的**内容**；空格或下标不合法返回 `None`。

        **空位的判据是「canonical 为空」**（`TemplateStateMessage` 的文档：
        空位四条一起空）。名字为空但 canonical 非空的行是**消息的 bug**，
        协议层已经拒了；这里当空处理只是不画它。
        """
        row = self.raw_row(surface, index)
        if row is None:
            return None
        canonical = row.get("canonical")
        count = row.get("count")
        if not canonical or isinstance(count, bool) or not isinstance(
                count, INTEGER_TYPES) or count <= 0:
            return None
        # **先过一遍 `_text`**：过线回来的名字是字节串，直接拿 `unicode` 去判
        # 会把它当非文本、换成空串——2026-10-02 真机上就是这个症状。
        name = _text(row.get("name"))
        if not isinstance(name, TEXT_TYPE):
            name = u""
        aux = row.get("aux")
        return {
            "canonical": canonical,
            "count": int(count),
            "name": name,
            "aux": aux if isinstance(aux, INTEGER_TYPES)
            and not isinstance(aux, bool) else 0,
            "locked": bool(row.get("locked")),
        }

    def locked(self, surface, index):
        # type: (str, object) -> bool
        """这一格能不能改。**空格也要看它**：合成模式的中间格没有配方时是
        空的，但一样不许玩家往里放东西。"""
        row = self.raw_row(surface, index)
        return bool(row.get("locked")) if row is not None else True

    def id_aux(self, surface, index):
        # type: (str, object) -> int
        cell = self.cell(surface, index)
        if cell is None or not cell["name"]:
            return 0
        return stack_size.id_aux(cell["name"])

    def count_text(self, surface, index):
        # type: (str, object) -> str
        """数量标签。**1 留空**（同终端列表与背包）：满屏的「1」只是噪声。"""
        cell = self.cell(surface, index)
        if cell is None or cell["count"] <= 1:
            return ""
        return str(cell["count"])

    def is_selected(self, surface, index):
        # type: (str, object) -> bool
        return self.selection == (surface, index)

    def visible(self, surface, index):
        # type: (str, object) -> bool
        """这一格画不画。**只有合成模式的输出格有例外**（User 2026-10-02）。

        - **工厂模式**：三格都画。产物是玩家自己配的，空格也要有槽底；
        - **合成模式**：**只画中间那一格**（上下两格整个不画）。

        **「没有产物」不在这里表达**：那由 `id_aux` 回 0 完成——中间那格
        没检测到配方时它就是空的，槽底还在、里面没图标。这与下半屏的背包槽
        是**同一套做法**（`terminal_panel._slot_visible` 恒真、`_slot_item_id_aux`
        空格回 0），那边已经跑通了。服务端也照这条来：检测不到配方时就发
        **空行**，不再发 `minecraft:barrier` 占位（User 2026-10-02 改）。

        > 一条记号：`#item_id_aux = 0` 能清掉图标，是**集合绑定这条路**的性质。
        > 另一条路（`SetUiItem(Item("minecraft:air"))`，搬运动画在用）会保留
        > 上一个渲染的物品——两件事长得像，别混。

        其余三个区同样恒真：空格子也要画槽底，少了槽底玩家会以为界面坏了。
        """
        if surface != SURFACE_OUTPUTS:
            return True
        if self.mode != MODE_CRAFTING:
            return True
        return index == CRAFT_PREVIEW_CELL

    def title(self):
        # type: () -> object
        for mode, label in MODE_LABELS:
            if mode == self.mode:
                return TITLE_FORMAT % (label,)
        return TITLE_FORMAT % (MODE_LABELS[0][1],)

    def mode_selected(self, mode):
        # type: (str) -> bool
        return self.mode == mode

    def is_unavailable(self):
        # type: () -> bool
        """遮罩：没有会话就挡住。判据与通用终端那一条相同。"""
        return self.closed or not self.client.has_session()

    # -- 交互 ----------------------------------------------------------

    def on_cell_click(self, surface, index):
        # type: (str, int) -> None
        """点一格虚格。见模块文档的双向模型。"""
        if self.closed or not self.client.has_session():
            return
        if self.locked(surface, index):
            return
        take = self._pending_backpack_item()
        if take is not None:
            # **复制**：源槽的实物不消耗、不移动，发出去的只是一条配置。
            _slot, key, _count = take
            self._send(ACTION_CELL_PUT, target=surface, cell=index, key=key)
            return
        view = getattr(self.screen, "list_view", None)
        if view is not None:
            key = view.cache.model.selected_key()
            if key is not None:
                # 网络行同样只提供配置的资源键，不按库存余额取出实物。
                self._send(ACTION_CELL_PUT, target=surface, cell=index, key=key)
                return
        if self.selection == (surface, index):
            self.selection = None
            return
        self.selection = (surface, index)

    def consumes_slot_click(self, ui_index):
        # type: (int) -> bool
        """`TerminalPanelBase._click` 的口子：这一下归虚格吗？

        选中了虚格时**归**——点背包槽（含空格）都是「清空被选中的那一格」，
        而不是选中／搬运背包里的东西。**没有选中时返回假**，于是通用终端与
        合成终端的行为一字不改。
        """
        return self._consume_selection()

    def consumes_row_click(self, row_index):
        # type: (int) -> bool
        """`TerminalListView.on_select` 的口子：点网络列表行归虚格吗？

        与背包那一侧**共用同一个实现**：交接说的「外界槽位」两类都算，
        两处各写一遍迟早会长出两套「什么算外界」的说法。
        """
        return self._consume_selection()

    def _consume_selection(self):
        # type: () -> bool
        if self.selection is None:
            return False
        surface, index = self.selection
        self.selection = None
        # **空格子不发命令**：服务端把「清空一个空格」当成功，但那是一次
        # 没有意义的往返。本地清掉选中就够了。
        if self.cell(surface, index) is not None:
            self._send(ACTION_CELL_CLEAR, target=surface, cell=index)
        return True

    def on_cell_press(self, surface, params, start, count):
        # type: (str, object, int, int) -> None
        """按下虚格：**只记格子与时刻**（长按判定要按下与抬起两个相位）。"""
        index = self._index_of(params, start, count)
        if index is None:
            return
        self.presses[surface] = (index, self.screen._clock(), False)

    def on_cell_release(self, surface, params, start, count):
        # type: (str, object, int, int) -> None
        """抬起虚格：**长按已经触发过就什么都不做**，否则算一次普通点击。

        **无论长短都要把按下记录清掉**：留着的话，下一次短按会拿上一次的时间
        戳去比，一次偶然的长按会让之后**每一下短按**都变成改数量。

        够久但没被 `_settle_long_press` 处理过（帧率极低、按下与抬起之间没有
        跑过一帧）时在这里补一次：判定只此一处，两个入口共用 `open_quantity`。
        """
        index = self._index_of(params, start, count)
        press = self.presses.pop(surface, None)
        if index is None:
            return
        if press is not None and press[0] == index:
            if press[2]:
                return
            if self.screen._clock() - press[1] >= LONG_PRESS_SECONDS:
                self.open_quantity(surface, index)
                return
        self.on_cell_click(surface, index)

    def _settle_long_press(self):
        # type: () -> None
        """按住够久就**当场**弹数量窗，不等松手（User 2026-10-02）。

        挂在这里而不是抬起那一侧：抬起才触发时，玩家按住了什么都不发生，
        只能靠松手去猜「这次算不算长按」——而这正是「看得到但用不了」的
        那种手感。每渲染帧检查一次，代价是遍历一张最多两个元素的表。

        **判定只此一处**：`on_cell_release` 那一支是帧率极低时的兜底，
        调的是同一个 `open_quantity`。
        """
        if not self.presses:
            return
        now = self.screen._clock()
        for surface, (index, at, fired) in list(self.presses.items()):
            if fired or now - at < LONG_PRESS_SECONDS:
                continue
            self.presses[surface] = (index, at, True)
            self.open_quantity(surface, index)

    def open_quantity(self, surface, index):
        # type: (str, int) -> bool
        """长按虚格：弹出数量窗。**空格子与锁着的格子都不弹。**

        上限取自服务端推来的 `limits`，客户端不写死——那个数是服务端的策略值。
        """
        if self.closed or not self.client.has_session():
            return False
        if self.locked(surface, index):
            return False
        cell = self.cell(surface, index)
        if cell is None:
            return False
        max_count = self.limits.get("max_count")
        if (isinstance(max_count, bool) or not isinstance(max_count, INTEGER_TYPES)
                or max_count < 1):
            max_count = 1
        return show_number_set_window(
            self.screen, cell["count"],
            partial(self._on_quantity, surface, index),
            min_number=1, max_number=max_count,
        )

    def _on_quantity(self, surface, index, number):
        # type: (str, int, int) -> object
        self._send(ACTION_CELL_COUNT, target=surface, cell=index, count=number)

    def on_real_slot_click(self, surface):
        # type: (str) -> None
        """点真实槽（`input` / `result`）。

        真实槽**只能当目标，永远不当来源**：规则里没有「选中真实槽」这个状态，
        来源只有背包（与网络列表的选中）。因此点它时按「有没有选中虚格」
        「背包有没有选中」「槽里有没有东西」三档走。
        """
        if self.closed or not self.client.has_session():
            return
        if self._consume_selection():
            return
        take = self._pending_backpack_item()
        if take is not None:
            slot, key, _count = take
            view = self.client.inventory_slots.get(slot)
            # 拿不到那一槽的服务端快照就**等待同步**：槽版本是锚上的一关。
            if view is None:
                return
            action = (ACTION_INPUT_PUT if surface == SURFACE_INPUT
                      else ACTION_OUTPUT_TAKE)
            self._send(action, key=key, slot=slot,
                       slot_version=view["slot_version"])
            return
        if self.cell(surface, 0) is None:
            return
        if surface == SURFACE_INPUT:
            self._send(ACTION_INPUT_TAKE)
        else:
            self._send(ACTION_OUTPUT_TAKE)

    # `key` 那一条的约定（**别"修"**）：选中的背包槽是 `slot` 指的那一格，
    # `key` 是**客户端看到的那一格现在装着什么**——放与取两个方向都这样报，
    # 它同时充当那一格的指纹与版本锚（06 的槽锚纪律）。
    # 放进去／取出来的**那一件东西**不在这里给：
    #   放入输入槽 → 它就是背包那一格里的东西，与 key 相同；
    #   取回产物槽 → 它只能来自**设备**，服务端自己知道是哪一件
    #                （`_template_slot_take` 换锚，见那里的说明）。
    # 曾有一版把「取回」也照 key 插入（那会把目标格里那件无关物品复制一份、
    # 同时把设备里的样板扣掉）；服务端已挡住，客户端因此**不需要**改这里。

    def on_mode(self, mode):
        # type: (str) -> object
        """切模式。**重复点当前模式什么都不发**：交接明写不把再次点击已选
        模式解释为取消选择，而发一条什么都不改的命令只会白换一条回执。"""
        if self.closed or not self.client.has_session():
            return None
        if mode not in (MODE_CRAFTING, MODE_FACTORY) or mode == self.mode:
            return None
        return self._send(ACTION_MODE, target=mode)

    def on_make(self):
        # type: () -> object
        """点「OK」：编码并输出。**客户端不自行产出物品**（交接第 5 节）。"""
        if self.closed or not self.client.has_session():
            return None
        return self._send(ACTION_ENCODE)

    def _pending_backpack_item(self):
        # type: () -> object
        """背包里选中的那一格 `(引擎槽号, key, 数量)`；没有返回 `None`。

        走 `pending_take` 而不是自己读模型：它**发命令前重读引擎**、并核对
        服务端快照的指纹，因此「选中之后东西被别处拿走了」在这一步就发现，
        而不是发一条注定失败的配置出去。
        """
        backpack = getattr(self.screen, "backpack", None)
        if backpack is None or backpack.selected is None:
            return None
        return backpack.pending_take()

    @staticmethod
    def _index_of(params, start, count):
        # type: (object, int, int) -> object
        """集合回调的 `#collection_index` → 本区下标；不合法返回 `None`。"""
        index = params.get("#collection_index")
        if (isinstance(index, bool) or not isinstance(index, INTEGER_TYPES)
                or not 0 <= index < count):
            return None
        return start + index

    # -- 发命令 --------------------------------------------------------

    def _send(self, action, target=None, cell=None, key=None, count=None,
              slot=None, slot_version=None):
        # type: (str, object, object, object, object, object, object) -> str
        """发一条命令，返回 `request_id`（没发出去时 `None`）。

        **`slot_version` 一条命令一个意思**：配置编辑那几条是**配置修订号**
        （最后收到的那一个，不做乐观推进，见模块文档），真实槽的取放是
        **背包槽锚的版本**。两种都由调用方给，这里不猜。
        """
        if self.closed or not self.client.has_session():
            return None
        if action in CONFIG_ACTIONS:
            slot_version = self.revision
        request_id = self.client.send_command(
            action,
            expected_index_version=self.client.index_version,
            target=target,
            cell=cell,
            key=key,
            count=count,
            slot=slot,
            slot_version=slot_version,
        )
        self._requests[request_id] = action
        return request_id

    # -- 回执 ----------------------------------------------------------

    def on_results(self, taken):
        # type: (list) -> int
        """处理本帧被取走的命令回执，返回处理了几条。

        **回执不在这里取**（`take_command_results`）：那是一条队列，列表视图
        已经在取，两处各取一次会互相吃掉对方的回执。因此由列表视图取，
        取完转发到这里（`TerminalListView(receipt_sink=...)`）。
        """
        count = 0
        for message in taken:
            result = getattr(message, "result", None)
            if not isinstance(result, dict):
                continue
            self.receipts.append(result)
            self._settle(result)
            count += 1
        del self.receipts[:-RECEIPT_MEMORY]
        return count

    def _settle(self, result):
        # type: (dict) -> None
        """一条回执的收尾。**只认自己发出去的那几条**（按 `request_id`）。"""
        action = self._requests.pop(result.get("request_id"), None)
        if action is None:
            return
        if result.get("status") != STATUS_OK:
            self.last_error = self._error_text(result)
            return
        self.last_error = None
        if action == ACTION_CELL_PUT:
            # **一次放入 = 一次来源选择用完**（User 2026-10-02）：放完就把
            # 背包槽与网络行的选中一起放掉，下一次点虚格是「选中那一格」而
            # 不是「再复制一件」。
            #
            # 清在**回执成功之后**而不是发送那一刻：被拒的那一下不该把玩家
            # 的来源选择吃掉——那是他刚刚做过的、有意义的一次选择。
            self._clear_sources()

    def _clear_sources(self):
        # type: () -> None
        """放掉两侧的选中：背包槽与网络列表行。

        两侧都清是因为它们**本来就是互斥的一对**（`TerminalPanelBase._click`
        在选中背包槽时会先清网络那一边），只清一侧会留下另一半亮着。
        """
        backpack = getattr(self.screen, "backpack", None)
        if backpack is not None:
            backpack.clear_selection()
        view = getattr(self.screen, "list_view", None)
        if view is not None and getattr(view, "cache", None) is not None:
            view.cache.model.clear_selection()

    @staticmethod
    def _error_text(result):
        # type: (dict) -> object
        """把回执里的原因码翻成人话。**取第一条**（服务端按重要性排序）。

        认不出的原因返回空串而不是原样回显代码：`no_capacity` 这种词对玩家
        什么也没说，而把它塞进一个将来会出现的提示控件里，只会让人以为界面
        出了别的毛病。
        """
        reasons = result.get("reasons")
        if not isinstance(reasons, list) or not reasons:
            return u""
        first = reasons[0]
        if not isinstance(first, dict):
            return u""
        return REASON_TEXTS.get(first.get("code"), u"")

    # -- 生命周期 ------------------------------------------------------

    def tick(self):
        # type: () -> None
        if self.closed:
            return
        self._settle_lost_session()
        self._settle_long_press()

    def _settle_lost_session(self):
        # type: () -> None
        """会话没了就把在途与选中一起收尾，并且关上数量窗。

        判据取「会话在不在」，不取「收到了哪条错误」——那样对所有丢会话的
        原因都成立（服务端判死、玩家走远、换维度、设备被换掉）。没有这一条，
        界面会停在「假活」：选中框赖着不走、在途表里挂着永远不回来的请求。
        """
        if self.client.has_session():
            return
        if not self._requests and self.selection is None:
            return
        self._requests = {}
        self.selection = None
        self.close_number_window()

    def close_number_window(self):
        # type: () -> None
        """关掉数量窗（如果开着）。销毁时必须调——组件自己的文档写明
        宿主的 `OnDestroy` 负责这一步，而它挂在屏幕的 `_vars` 上。"""
        window = self.screen._vars.get(NUMBER_WINDOW_ATTRIBUTE)
        if window is not None:
            window.close()

    def close(self):
        # type: () -> None
        """收尾。可以重复调用。"""
        if self.closed:
            return
        self.closed = True
        self.close_number_window()
        self._requests = {}
        self.selection = None
        self.presses = {}
        self.rows = dict((surface, ()) for surface, _c in SURFACES)

    def diagnostics(self):
        # type: () -> dict
        return {
            "closed": self.closed,
            "revision": self.revision,
            "mode": self.mode,
            "occupied": dict(
                (surface, sum(1 for index in range(CELLS_OF[surface])
                              if self.cell(surface, index) is not None))
                for surface, _c in SURFACES
            ),
            "selected": self.selection,
            "limits": dict(self.limits),
            "in_flight": len(self._requests),
            "errors": self.errors,
            "last_error": self.last_error,
            "receipts": list(self.receipts),
        }


@RegistToolDeltaScreen("template_terminal_panel_ui.main")
class TemplatePanel(TerminalPanelBase):
    """样板终端：通用终端的一切 + 四个格子区。

    接线与 `CraftingPanel.OnCreate` 逐条对应，多出来的只有 `template` 这一个
    模型、它那条 `template_state` 收包回调，以及挂给基类的那一个协作口子。
    **没有权威身份就整块暂停**（同 11.md 的 UI 前置约定）。
    """

    #: 与 `list_view` 同一个地位：绑定比 `OnCreate` 先生效，而它只在拿到
    #: 权威身份时才建，因此这里是类属性兜着。没有它时四区都是空的
    #: ——**不是假的**，就是没接到数据。
    template = None

    #: 标题控件的路径与上一次写进去的文本。**类属性兜底**：它们由
    #: `OnTicking` 里的 `_sync_title` 写，而 `OnTicking` 可能比 `OnCreate`
    #: 早一拍（与 `list_view` 同一条理由）。
    _title_path = None
    _title_text = None

    # -- 四个格子区的绑定（类体注册） ----------------------------------
    #
    # 每条都先看 `self.template` 在不在：没有权威身份的会话（`OpenDemo`）永远
    # 不建它，而绑定照样会被引擎问。缺席时给**安全值**——报一个「有 N 格」
    # 出去就会画出幽灵格子。
    #
    # **每个区一套方法**：集合绑定的回调只拿得到集合内下标、拿不到集合名，
    # 共用一套的话后面的区会按前面的区的下标去读。

    @Binder.binding_collection(
        Binder.BF_BindBool, "endernet_template_source",
        BINDING_PREFIX + "source_visible")
    def source_visible(self, index):
        # type: (object) -> bool
        return True if self.template is None else self.template.visible(
            SURFACE_SOURCE, index)

    @Binder.binding_collection(
        Binder.BF_BindInt, "endernet_template_source",
        BINDING_PREFIX + "source_item_id_aux")
    def source_item_id_aux(self, index):
        # type: (object) -> int
        if self.template is None:
            return 0
        return self.template.id_aux(SURFACE_SOURCE, index)

    @Binder.binding_collection(
        Binder.BF_BindString, "endernet_template_source",
        BINDING_PREFIX + "source_item_count")
    def source_item_count(self, index):
        # type: (object) -> str
        if self.template is None:
            return ""
        return self.template.count_text(SURFACE_SOURCE, index)

    @Binder.binding_collection(
        Binder.BF_BindBool, "endernet_template_source",
        BINDING_PREFIX + "source_selected")
    def source_selected(self, index):
        # type: (object) -> bool
        if self.template is None:
            return False
        return self.template.is_selected(SURFACE_SOURCE, index)

    @Binder.binding_collection(
        Binder.BF_BindBool, "endernet_template_outputs",
        BINDING_PREFIX + "outputs_visible")
    def outputs_visible(self, index):
        # type: (object) -> bool
        return True if self.template is None else self.template.visible(
            SURFACE_OUTPUTS, index)

    @Binder.binding_collection(
        Binder.BF_BindInt, "endernet_template_outputs",
        BINDING_PREFIX + "outputs_item_id_aux")
    def outputs_item_id_aux(self, index):
        # type: (object) -> int
        if self.template is None:
            return 0
        return self.template.id_aux(SURFACE_OUTPUTS, index)

    @Binder.binding_collection(
        Binder.BF_BindString, "endernet_template_outputs",
        BINDING_PREFIX + "outputs_item_count")
    def outputs_item_count(self, index):
        # type: (object) -> str
        if self.template is None:
            return ""
        return self.template.count_text(SURFACE_OUTPUTS, index)

    @Binder.binding_collection(
        Binder.BF_BindBool, "endernet_template_outputs",
        BINDING_PREFIX + "outputs_selected")
    def outputs_selected(self, index):
        # type: (object) -> bool
        if self.template is None:
            return False
        return self.template.is_selected(SURFACE_OUTPUTS, index)

    @Binder.binding_collection(
        Binder.BF_BindBool, "endernet_template_input",
        BINDING_PREFIX + "input_visible")
    def input_visible(self, index):
        # type: (object) -> bool
        return True if self.template is None else self.template.visible(
            SURFACE_INPUT, index)

    @Binder.binding_collection(
        Binder.BF_BindInt, "endernet_template_input",
        BINDING_PREFIX + "input_item_id_aux")
    def input_item_id_aux(self, index):
        # type: (object) -> int
        if self.template is None:
            return 0
        return self.template.id_aux(SURFACE_INPUT, index)

    @Binder.binding_collection(
        Binder.BF_BindString, "endernet_template_input",
        BINDING_PREFIX + "input_item_count")
    def input_item_count(self, index):
        # type: (object) -> str
        if self.template is None:
            return ""
        return self.template.count_text(SURFACE_INPUT, index)

    @Binder.binding_collection(
        Binder.BF_BindBool, "endernet_template_input",
        BINDING_PREFIX + "input_selected")
    def input_selected(self, index):
        # type: (object) -> bool
        return False

    @Binder.binding_collection(
        Binder.BF_BindBool, "endernet_template_result",
        BINDING_PREFIX + "result_visible")
    def result_visible(self, index):
        # type: (object) -> bool
        return True if self.template is None else self.template.visible(
            SURFACE_RESULT, index)

    @Binder.binding_collection(
        Binder.BF_BindInt, "endernet_template_result",
        BINDING_PREFIX + "result_item_id_aux")
    def result_item_id_aux(self, index):
        # type: (object) -> int
        if self.template is None:
            return 0
        return self.template.id_aux(SURFACE_RESULT, index)

    @Binder.binding_collection(
        Binder.BF_BindString, "endernet_template_result",
        BINDING_PREFIX + "result_item_count")
    def result_item_count(self, index):
        # type: (object) -> str
        if self.template is None:
            return ""
        return self.template.count_text(SURFACE_RESULT, index)

    @Binder.binding_collection(
        Binder.BF_BindBool, "endernet_template_result",
        BINDING_PREFIX + "result_selected")
    def result_selected(self, index):
        # type: (object) -> bool
        return False

    # -- 控件绑定 ------------------------------------------------------

    def _title_label(self):
        # type: () -> object
        """`<配置区>/title` 那个 label；取不到返回 `None`。

        **取到一次就把路径记下来**（`_title_path`），之后不再重算——
        路径是引擎的结构，运行期不会变。

        `_title_path` 为**空串**表示「试过了、取不到」，不再重试：取不到的
        代价是标题不写，而每帧重试会每帧抛一次异常（引擎把那个算在渲染里）。
        """
        path = self._title_path
        if path is None:
            candidate = title_control_path()
            if candidate is None:
                return None
            try:
                label = self.GetBaseUIControl(candidate).asLabel()
            except Exception:
                self._title_path = ""
                return None
            self._title_path = candidate
            return label
        if not path:
            return None
        try:
            return self.GetBaseUIControl(path).asLabel()
        except Exception:
            self._title_path = ""
            return None

    def _sync_title(self):
        # type: () -> None
        """把当前模式写进标题。**只在文本真的变了时写。**

        为什么这个控件走 `SetText` 而不是绑定（User 2026-10-02 定）：它上面
        那条全局绑定在真机上没能把文字画出来，而**同一个面板里 `stat_bar`
        用的是同一种写法却是好的**——也就是说问题不在绑定机制，在这个控件。
        直接写文本绕开了那一层。

        写之前比一遍：`SetText` 每帧调一次会让引擎每帧重新排版这个 label，
        与排序图标那条「只在状态真变了时写 UV」同一个理由。
        """
        if self.template is None:
            return
        text = self.template.title()
        if text == self._title_text:
            return
        label = self._title_label()
        if label is None:
            return
        try:
            label.SetText(text)
        except Exception:
            self._title_path = ""
            return
        self._title_text = text

    @Binder.binding(Binder.BF_BindBool, BINDING_PREFIX + "mode_crafting_selected")
    def mode_crafting_selected(self):
        # type: () -> bool
        """选中框的显隐**一律来自推送**，不做本地乐观切换：本地先亮、
        服务端拒了再灭回去，看起来就是「点了闪一下」。
        没有会话时两边都假——一个框都不亮，比亮错一个要好。"""
        if self.template is None or not self.template.client.has_session():
            return False
        return self.template.mode_selected(MODE_CRAFTING)

    @Binder.binding(Binder.BF_BindBool, BINDING_PREFIX + "mode_factory_selected")
    def mode_factory_selected(self):
        # type: () -> bool
        if self.template is None or not self.template.client.has_session():
            return False
        return self.template.mode_selected(MODE_FACTORY)

    @Binder.binding(Binder.BF_ButtonClickUp, BINDING_PREFIX + "mode_crafting")
    def OnModeCrafting(self, params=None):
        # type: (object) -> None
        if self.template is not None:
            self.template.on_mode(MODE_CRAFTING)

    @Binder.binding(Binder.BF_ButtonClickUp, BINDING_PREFIX + "mode_factory")
    def OnModeFactory(self, params=None):
        # type: (object) -> None
        if self.template is not None:
            self.template.on_mode(MODE_FACTORY)

    @Binder.binding(Binder.BF_ButtonClickUp, BINDING_PREFIX + "make")
    def OnMakeTemplate(self, params=None):
        # type: (object) -> None
        if self.template is not None:
            self.template.on_make()

    # -- 虚格的按住／抬起（长按 = 改数量） ------------------------------
    #
    # 与背包那两条**同一形状**：同一个绑定名挂两个旗标，引擎按「标志 + 名字」
    # 找回调，JSON 侧一个字都不用改。

    @Binder.binding(Binder.BF_ButtonClickDown, BINDING_PREFIX + "source_click")
    def OnSourcePress(self, params=None):
        # type: (object) -> None
        if self.template is not None:
            self.template.on_cell_press(SURFACE_SOURCE, params, 0, CELLS_OF[
                SURFACE_SOURCE])

    @Binder.binding(Binder.BF_ButtonClickUp, BINDING_PREFIX + "source_click")
    def OnSourceRelease(self, params=None):
        # type: (object) -> None
        if self.template is not None:
            self.template.on_cell_release(SURFACE_SOURCE, params, 0, CELLS_OF[
                SURFACE_SOURCE])

    @Binder.binding(Binder.BF_ButtonClickDown, BINDING_PREFIX + "outputs_click")
    def OnOutputsPress(self, params=None):
        # type: (object) -> None
        if self.template is not None:
            self.template.on_cell_press(SURFACE_OUTPUTS, params, 0, CELLS_OF[
                SURFACE_OUTPUTS])

    @Binder.binding(Binder.BF_ButtonClickUp, BINDING_PREFIX + "outputs_click")
    def OnOutputsRelease(self, params=None):
        # type: (object) -> None
        if self.template is not None:
            self.template.on_cell_release(SURFACE_OUTPUTS, params, 0, CELLS_OF[
                SURFACE_OUTPUTS])

    @Binder.binding(Binder.BF_ButtonClickUp, BINDING_PREFIX + "input_click")
    def OnInputSlotClick(self, params=None):
        # type: (object) -> None
        if self.template is not None:
            self.template.on_real_slot_click(SURFACE_INPUT)

    @Binder.binding(Binder.BF_ButtonClickUp, BINDING_PREFIX + "result_click")
    def OnResultSlotClick(self, params=None):
        # type: (object) -> None
        if self.template is not None:
            self.template.on_real_slot_click(SURFACE_RESULT)

    # -- 生命周期 ------------------------------------------------------

    def OnCreate(self):
        TerminalPanelBase.OnCreate(self)
        params = self._init_params
        ref = params.get("device_ref")
        if ref is None:
            return
        try:
            device_ref = DeviceRef.from_dict(ref) if isinstance(ref, dict) else ref
        except (KeyError, TypeError, ValueError):
            # 身份破了就按「没有权威身份」处理：拿半个位置去开会话只会换来
            # 一条被拒的请求，症状与「终端没反应」完全一样。
            return
        if not isinstance(device_ref, DeviceRef):
            return
        dimension = params.get("dimension", 0)
        client = NetworkClient()
        # 先挂收包再开会话：`session_ready` 与第一份 `template_state` 都可能比
        # 这个函数返回还早（同 `CommonTerminalPanel` 那条注释）。
        client.port.listen(client.receive)
        self.status = device_status.DeviceStatusModel(device_ref)
        client.on_message(self._make_status_handler(client))
        self.template = TemplateView(self, client=client)
        client.on_message(self._make_template_handler(client))
        # **把协作口子挂上去**：两条共享手势（点背包槽、点网络行）在这台
        # 终端上多一层含义，见 `TerminalPanelBase.config` 的文档。
        self.config = self.template
        self.list_view = TerminalListView(
            self,
            cache=TerminalCache(client=client),
            device_ref=device_ref,
            dimension=dimension,
            backpack=self.backpack,
            # **本界面的滚动控件路径没有运行时结论**，所以不接管滚动条：
            # 见模块文档的已知缺项。列表内容、搜索、排序都不受它影响。
            scroll_path=None,
            # 回执只能有一处取走（队列语义），由列表视图取、取完转给本视图。
            receipt_sink=self._on_receipts,
        )
        client.open_session(device_ref, dimension)

    def _make_template_handler(self, client):
        # type: (object) -> object
        """收 `template_state` 的回调。

        两道过滤与状态那条完全一样，缺一不可：会话号对不上就丢（旧会话的包
        可能在关掉之后才到）；`TemplateView.apply` 自己再挡一次旧版本。回调
        **不抛**：它在 `receive()` 的派发循环里跑，抛出去会连累同一批里的
        其它消息。
        """
        def on_template_message(envelope, message):
            # type: (object, object) -> None
            if message is None or message.kind != MSG_TEMPLATE_STATE:
                return
            if envelope.session_id != client.session_id:
                return
            if self.template is not None:
                self.template.apply(message)

        return on_template_message

    def _on_receipts(self, taken):
        # type: (list) -> None
        """列表视图取走回执之后转给本视图（见 `TemplateView.on_results`）。"""
        if self.template is not None:
            self.template.on_results(taken)

    def OnTicking(self):
        # **基类的背包轮询要显式调**：本方法没有 `super()`（同
        # `CommonTerminalPanel.OnTicking`），基类加一个同名方法会被覆盖而
        # **永不执行且不报错**，症状只是「背包一直不更新」。
        self.fly.tick()
        self._poll_backpack()
        if self.list_view is not None:
            self.list_view.tick()
        if self.template is not None:
            self.template.tick()
        # 标题走 SetText（见 `_sync_title`），因此每帧要对一次；文本没变时
        # 它第一步就返回，代价是一次字符串比较。
        self._sync_title()

    def OnDestroy(self):
        if self.list_view is not None:
            self.list_view.close()
            self.list_view = None
        if self.template is not None:
            self.template.close()
            self.template = None
        # **协作口子要一起摘掉**：留着的话，下一次打开时基类会拿一个已经
        # `close()` 的视图去判「这一下归谁」，而它的答案全是「不归」。
        self.config = None
        self.status = None
        TerminalPanelBase.OnDestroy(self)

    @classmethod
    def OpenDemo(cls):
        """客户端手工自查用：在 `UiInitFinishedEvent` 注册过界面之后调。

        **没有权威身份，因此整块暂停**（同 `CommonTerminalPanel.OpenDemo`）：
        四个格子区全空着、列表也空着。要走通整条链路就得由服务端
        `PushUIRequest` 推过来（右键样板终端）。
        """
        return cls.PushUI({})
