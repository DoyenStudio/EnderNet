# coding=utf-8
"""合成终端界面（11-04）：通用终端的一切 + 九宫格工作台。

上半屏的网络列表、下半屏的玩家背包、搜索、排序、遮罩、关闭全部沿用
`TerminalPanelBase` 与 `TerminalListView`，**绑定名仍是 `#EnderNetTerminal.*`**
——合成终端不是另一个终端，它是同一个终端的另一种面板。本文件只加两样东西：

1. 九宫格与产物预览（`CraftingView`）。数据全部来自服务端每帧推的
   `craft_state`（`CraftStateMessage`），**客户端不自己算配方**：摆在格子里
   的是真实物品，能不能合成只有服务端说了算（`plans/ui/crafting_terminal_panel.md`
   第 5 节）。
2. 三个操作：导入（`craft_import`）、点输出槽（`craft_take`）、清空
   （`craft_clear`），各一条 `TerminalCommand`。

## 为什么九宫格换了模板

User 交接的 `crafting_grid` 引用 `endernet_container_common.custom_slot`，那是
一个**展示用**模板：它没有集合绑定，里面的渲染控件也没有按钮
（`plans/technical_notes/custom_slot.md` 明写「所有显示状态由客户端 Python
主动更新」）。而按控件路径逐格 SetUiItem 这条路在本仓库**已被否决过一次**
——见 `terminal_panel.py` 的类文档：grid 的格子要到渲染时才生成，滚动／换出
之后的控件引用就是过期的，那条路当初的症状是 `OnCreate` 第一步就抛异常、
整块界面建不起来。

因此九宫格与输出槽都换成 `endernet_windows.item_can_be_selected` 的**薄包装**
（`plans/tasks/11-交付说明.md` 第十一节第 3 小节给下半屏做过同一个动作）。
模板两侧都只能覆盖 `$` 变量，控件名与几何一个字没动。

集合绑定的回调**只拿得到集合内下标、拿不到集合名**，所以九宫格与输出槽必须
各用一套绑定名：共用一套的话输出槽会按九宫格的下标去读，界面照常渲染、
不报任何错（同 `terminal_panel.SEGMENTS` 那条）。

## 输出槽为什么是 1×1 的 grid

`item_can_be_selected` 的绑定是**集合绑定**，只有在集合上下文里才解析得到。
单放一个实例在面板上，引擎查不到集合，那一格永远空白。所以输出槽改成
`grid_dimensions: [1, 1]` 的 grid（集合 `endernet_craft_output`）、模板仍是那个
薄包装——渲染出来还是 18×18 的一格，只是它现在有集合身份了。

## 已知缺项（如实记，不补假实现）

- **合成没有专属飞行动画。** `flying_item` 控件在面板上，本类也照常推进
  `self.fly`，但**本轮不发起任何一次飞行**：起终点要现算控件矩形，而本界面
  的控件根路径还没在运行时核实过（`plans/ui/crafting_terminal_panel.md` 交接
  时也记着「合成专属动画未定义」）。发一个飞向 `(0, 0)` 的动画比不飞更糟。
- **失败原因没有显示位置。** 与通用终端的命令回执同一处境：原因码留在
  `CraftingView.last_error` 与 `diagnostics()` 里，界面上**没有控件显示它**。
  本轮不新增控件（按 11.md 第 25 行，新增控件由 User 负责）。
- **滚动重锚缺席。** 网络列表那一半的锚点要读滚动条的百分比，而本界面的
  滚动控件路径同样没有运行时结论，因此 `TerminalListView` 在这里不接管滚动条
  （`scroll_path=None`）：列表内容、搜索、排序照常，只有「重排后把玩家盯着的
  那一行拉回视野」这一条不做。
"""

from functools import partial

from ....tooldelta.define.item import Item
from ....tooldelta.ui import Binder, RegistToolDeltaScreen
from . import slot_layout, stack_size
from .select_item_window import (
    WINDOW_ATTRIBUTE as PICKER_WINDOW_ATTRIBUTE,
    show_select_item_window,
)
from .terminal_cache import TerminalCache
from .terminal_panel import TerminalListView, TerminalPanelBase
from .widgets import device_status
from ..network_client import NetworkClient
from ...common.identity import DeviceRef
from ...common.protocol import (
    CRAFT_SLOT_COUNT,
    MSG_CRAFT_STATE,
    MSG_DEVICE_STATUS,
    STATUS_OK,
    Reason,
)
from ...common.resource_key import ResourceKey

try:
    INTEGER_TYPES = (int, long)
except NameError:
    INTEGER_TYPES = (int,)

try:
    TEXT_TYPE = unicode
except NameError:
    TEXT_TYPE = str

#: 新增控件的绑定前缀。**通用终端已有的那几条一律沿用 `#EnderNetTerminal.*`**
#: （搜索、排序、网络列表、背包、快捷栏、遮罩、关闭），不另立一套：同一个
#: 界面上同名同义的控件用两个名字，只会让「这条绑定到底谁在答」变成猜谜。
BINDING_PREFIX = "#EnderNetCraft."

#: 九宫格的集合名。**必须与 JSON 里 `crafting_grid` 的 `collection_name` 逐字
#: 相同**——对不上时引擎查不到集合，九格全空而界面照常渲染。
GRID_COLLECTION = "crafting_grid"
#: 输出槽自己的集合。见模块文档「输出槽为什么是 1×1 的 grid」。
OUTPUT_COLLECTION = "endernet_craft_output"

GRID_VISIBLE_BINDING = BINDING_PREFIX + "grid_visible"
GRID_ITEM_ID_AUX_BINDING = BINDING_PREFIX + "grid_item_id_aux"
GRID_ITEM_COUNT_BINDING = BINDING_PREFIX + "grid_item_count"
GRID_SELECTED_BINDING = BINDING_PREFIX + "grid_selected"
#: 点九宫格的一格。**R12 没定案之前它什么都不做**（放入／取出／拖拽都属未定），
#: 但名字要注册：JSON 侧把它填进 `$item_select_button`，而半张表上的名字查不到
#: 时会安静地什么都不发生——那时「点了没反应」与「本来就没实现」分不开。
GRID_CLICK_BINDING = BINDING_PREFIX + "grid_click"

OUTPUT_VISIBLE_BINDING = BINDING_PREFIX + "output_visible"
OUTPUT_ITEM_ID_AUX_BINDING = BINDING_PREFIX + "output_item_id_aux"
OUTPUT_ITEM_COUNT_BINDING = BINDING_PREFIX + "output_item_count"
OUTPUT_SELECTED_BINDING = BINDING_PREFIX + "output_selected"
#: 点输出槽 = 执行一次合成。
OUTPUT_TAKE_BINDING = BINDING_PREFIX + "take"

IMPORT_BINDING = BINDING_PREFIX + "import"
CLEAR_BINDING = BINDING_PREFIX + "clear"

#: 三个动作名，**与 `StorageService.handle` 的动作表逐字相同**。这里不另立
#: 一套词汇再映射一次：映射写错了的表现是命令被服务端回一句「不认识」，
#: 而那只有真机上看得见。
ACTION_IMPORT = "craft_import"
ACTION_TAKE = "craft_take"
ACTION_CLEAR = "craft_clear"

#: 面给玩家的失败原因。**只列界面必须点出来的那几条**（11-04 的交接要求），
#: 其余原因退化成空串——不是「没有原因」，而是这一轮没有能显示它的控件
#: （见模块文档的已知缺项）。文案按 `plans/ui/crafting_terminal_panel.md`
#: 第 5 节确认过的口径写。
REASON_TEXTS = {
    Reason.CRAFT_GRID_OCCUPIED: "九宫格里还有物品，先点「清空」再导入",
    Reason.CRAFT_NO_RECIPE: "这个产物查不到工作台配方",
    Reason.NO_CAPACITY: "背包放不下",
    Reason.INSUFFICIENT_STOCK: "九宫格的原料不满足这条配方",
}

#: 留多少条回执供诊断。与 `terminal_panel.RECEIPT_MEMORY` 同一口径与同一理由：
#: 回执必须被取走（不取 `NetworkClient._results` 只涨不落），而界面上没有
#: 显示位置，取走后只留最近几条。
RECEIPT_MEMORY = 8

#: 候选配方窗口。**用本界面自己的模板**，不改 JSON、不新造控件：窗口是
#: 运行动态创建的（与 `select_item_window` 同一手法），候选逐条铺 `recipe_section`。
RECIPE_WINDOW_ATTRIBUTE = "_endernet_recipe_choice_window"
RECIPE_WINDOW_DEFINITION = "crafting_terminal_panel_ui.load_recipe_window"
RECIPE_WINDOW_NAME = "endernet_recipe_choice_window"
RECIPE_SECTION_DEFINITION = "crafting_terminal_panel_ui.recipe_section"
#: 窗口里那个滚动区的名字（`main` 之下的一层）。内容控件由引擎回答
#: （`GetScrollViewContentControl`），不猜路径。
RECIPE_SCROLL_PATH = "/" + RECIPE_WINDOW_NAME + "/scroll_view"
#: 候选逐条往上摆：`recipe_section` 高 62，留 2 像素缝。
RECIPE_ROW_HEIGHT = 64.0
#: 一屏最多铺几条。**超过它就不再铺**（不是假装能滚动）：滚动区能不能跟着
#: 内容长大是没实测过的，铺出屏幕外只会让玩家以为「只有这几条」。
RECIPE_MAX_ROWS = 3


def _cell_of(row):
    # type: (object) -> object
    """一行的线上形状 → 内部形状；空位返回 `None`。

    **空位的判据是「canonical 为空串且 count 为 0」**（`CraftStateMessage`
    的文档：空位四条一起空）。两条都看：只看 canonical 会把一个「有东西但
    名字为空」的畸形行当成空位，只看 count 则会把 `count: 0` 的实物抹掉。

    `name` / `aux` **由服务端随这一行带来**，不是本地反解指纹得来的：
    画一格要的是 `#item_id_aux`，也就是 `Item(资源名).GetBasicInfo().id_aux`，
    而 `canonical()` 是给存档与去重用的指纹、**没承诺可解析**——从它里面
    抠出资源名就是拿线上格式当接口，编码一变就静默画不出图标。
    """
    if not isinstance(row, dict):
        return None
    canonical = row.get("canonical")
    count = row.get("count")
    if not canonical or not isinstance(count, INTEGER_TYPES) or count <= 0:
        return None
    name = row.get("name")
    aux = row.get("aux")
    return {
        "canonical": canonical,
        "count": int(count),
        "name": name if isinstance(name, TEXT_TYPE) else u"",
        "aux": aux if isinstance(aux, INTEGER_TYPES) and not isinstance(aux, bool)
        else 0,
    }


class CraftingView(object):
    """九宫格、产物预览与三条操作的**客户端状态**。引擎无关，可离线整条跑。

    **它不持有任何权威状态**（架构规划 6.6）：格子里有什么、能不能合成、
    产物是多少，全部照抄服务端推来的 `craft_state` 与命令回执。本地能决定的
    只有「窗口开着没有」「在等哪几条回执」。

    ## 身份从哪来：**服务端把名字一起发过来**

    九宫格里每一行的线上形状是 `{slot, canonical, count, name, aux}`，
    画图标直接用 `name`（`stack_size.id_aux(name)`）。

    **不要在客户端建 `canonical -> 资源名` 的索引。** 这里曾经是那么做的
    （从网络列表与玩家背包两处灌索引），因为它当时确实只有指纹可用；
    后来在**消息那一侧**修掉了：`CraftStateMessage` 现在随行带 `name` / `aux`，
    空的四条一起空。理由记在那个类的文档里，这里只说客户端这一半：

    - `canonical()` 是给存档与去重用的**指纹**，没承诺可解析——从它里面抠
      资源名就是拿线上格式当接口，编码一变就静默画不出图标；
    - 索引必然**有漏**：九宫格里的东西是服务端从背包与网络借来的，客户端能
      看见的只是它当时恰好显示的那一份。漏了就是「只有数量、没有图标」，
      而那看起来像引擎没渲染，查起来毫无线索。**九宫格是服务端权威的**
      （`InventorySnapshot` 只用 `canonical` 是因为玩家背包客户端本地读得到，
      两者不是同一种处境），所以名字只能由服务端给。
    """

    def __init__(self, screen, client=None):
        # type: (object, object) -> None
        self.screen = screen
        self.client = client if client is not None else NetworkClient()
        self.closed = False
        #: 服务端推来的九宫格版本。**只接受不比它旧的**，乱序到达时不会把
        #: 旧画面盖回来。
        self.revision = -1
        self.cells = (None,) * CRAFT_SLOT_COUNT
        #: 保留的配方标识（`PresetRecipe.digest()`）。**只是备忘**：能不能合成
        #: 看的是格子里实际有什么（服务端那侧的口径），这里只用来在界面上
        #: 认出「上次导入的是哪一条」。
        self.recipe = ""
        self.preview = None
        #: 等玩家在候选里挑一条时的状态：`{output, candidates}` 或 `None`。
        self.choice = None
        self.receipts = []
        #: 最近一次失败的原因码与文案。没有显示位置（见模块文档）。
        self.last_error = None
        self.errors = 0
        self.window = None
        self._requests = {}

    # -- 收包 ----------------------------------------------------------

    def apply(self, message):
        # type: (object) -> bool
        """收一份 `craft_state`。**旧版本一律丢掉**，返回是否采纳。"""
        revision = getattr(message, "revision", None)
        if (isinstance(revision, bool) or not isinstance(revision, INTEGER_TYPES)
                or revision < self.revision):
            return False
        rows = getattr(message, "slots", ()) or ()
        cells = []
        for index in range(CRAFT_SLOT_COUNT):
            cells.append(_cell_of(rows[index]) if index < len(rows) else None)
        self.revision = revision
        self.cells = tuple(cells)
        recipe = getattr(message, "recipe", "") or ""
        self.recipe = recipe
        preview = getattr(message, "preview", None)
        self.preview = preview if isinstance(preview, dict) else None
        #: 摆位一换，等着的候选就不作数了——服务端把候选交回来之后玩家又动了
        #: 格子，那时按旧候选重发只会被拒。**这里不替玩家清空选择**，只是把
        #: 那个窗口收起来：格子的内容变了，窗口里画的那份预览已经不成立了。
        if self.choice is not None:
            self.close_window()
            self.choice = None
        return True

    # -- 九宫格与预览（绑定读的就是这几个） ----------------------------

    def cell(self, index):
        # type: (object) -> object
        """第 `index` 格；空位或下标不合法返回 `None`。

        越界与 `bool` 一律挡掉：集合回调的下标是外部输入，
        `isinstance(True, int)` 为真，而 `self.cells[-1]` 不报错、取的是最后一格。
        """
        if isinstance(index, bool) or not isinstance(index, INTEGER_TYPES):
            return None
        if not 0 <= index < CRAFT_SLOT_COUNT:
            return None
        return self.cells[index]

    def has_cell(self, index):
        # type: (object) -> bool
        return self.cell(index) is not None

    def cell_id_aux(self, index):
        # type: (object) -> int
        cell = self.cell(index)
        if cell is None or not cell.get("name"):
            return 0
        return stack_size.id_aux(cell["name"])

    def cell_count(self, index):
        # type: (object) -> str
        """数量标签。**1 留空**（同终端列表与背包）：满屏的「1」只是噪声。"""
        cell = self.cell(index)
        if cell is None or cell["count"] <= 1:
            return ""
        return str(cell["count"])

    def output_visible(self):
        # type: () -> bool
        """产物槽的可见性：**没有可合成的产物就不画它**。

        与九宫格那条（恒真：空格子也要画槽底）取向不同，这是有意的——
        九宫格是设备的固定结构，少了槽底玩家会以为界面坏了；而输出槽是
        「现在能合成什么」，没有产物还画一个空槽，看起来像是「有产物但没画出来」。
        """
        return not self.closed and self.preview is not None

    def output_id_aux(self):
        # type: () -> int
        if self.preview is None:
            return 0
        name = self.preview.get("name")
        if not name:
            return 0
        return stack_size.id_aux(name)

    def output_count(self):
        # type: () -> str
        if self.preview is None:
            return ""
        count = self.preview.get("count")
        if isinstance(count, bool) or not isinstance(count, INTEGER_TYPES):
            return ""
        return str(count) if count > 1 else ""

    def is_unavailable(self):
        # type: () -> bool
        """遮罩：没有会话就挡住。判据与通用终端那一条相同。"""
        return self.closed or not self.client.has_session()

    # -- 三个操作 ------------------------------------------------------

    def on_import(self, params=None):
        # type: (object) -> bool
        """点「导入」：弹产物选择窗。

        **产物由玩家选，不由客户端猜。** 界面上唯一的凭据是九宫格里的原料与
        产物预览，而「想合成什么」与「现在能合成什么」是两件事——按预览直接
        发命令就是把玩家没做过的一次选择当成他做过了。
        选择窗用的仍是既有组件（`select_item_window`），本次不改造它。
        """
        if self.closed or not self.client.has_session():
            return False
        return show_select_item_window(
            self.screen, self._network_item_ids(), self.on_product_chosen)

    def _network_item_ids(self):
        # type: () -> list
        """产物候选：**网络列表里当前可见的那些物品**。

        取可见行而不是整份库存：搜索框里的字就是玩家的筛选意图，把筛掉的东西
        再端上来会让「搜了还是没有」变成假象。重复名去掉（同一件东西的行只有
        一条，但列表可能还没刷新）。
        """
        view = getattr(self.screen, "list_view", None)
        rows = getattr(view, "rows", ()) or ()
        ids = []
        seen = set()
        for row in rows:
            name = row.key.name
            if name not in seen:
                seen.add(name)
                ids.append(name)
        return ids

    def on_product_chosen(self, item_id):
        # type: (str) -> object
        """选择窗回调：把这个产物交给服务端去找配方。返回 `request_id`。

        一条配方直接摆料、多条配方回一份 `needs_choice` 由 `_settle` 接住
        ——**两条路都由服务端决定**，客户端不预先判「这个产物有几条配方」。
        """
        if self.closed or not self.client.has_session() or not item_id:
            return None
        return self._send(ACTION_IMPORT, key=ResourceKey.item(item_id))

    def on_take(self, params=None):
        # type: (object) -> object
        """点输出槽：执行**一次**合成。一次就是一次，不连做。"""
        if self.closed or not self.client.has_session():
            return None
        return self._send(ACTION_TAKE)

    def on_clear(self, params=None):
        # type: (object) -> object
        """点「清空」：九宫格里的实物全部返还网络（服务端口径）。"""
        if self.closed or not self.client.has_session():
            return None
        return self._send(ACTION_CLEAR)

    def on_grid_click(self, params=None):
        # type: (object) -> None
        """点九宫格的一格。**R12 未定案，本轮什么都不做。**

        放入／取出／拖拽、长按、左右键与光标持有物的退出规则都还没有结论
        （UI 文档第 8 节），自己发明一套就是把未定玩法悄悄定案。这里显式留一个
        **不做任何事**的方法，只是为了让 JSON 里那个名字有主——查不到绑定时
        引擎安静地什么都不发生，而「安静地什么都不发生」正是要避开的那种错。
        """
        return None

    def _send(self, action, key=None, recipe=None):
        # type: (str, object, object) -> str
        request_id = self.client.send_command(
            action,
            expected_index_version=self.client.index_version,
            key=key,
            recipe=recipe,
        )
        self._requests[request_id] = action
        return request_id

    # -- 多配方选择 ----------------------------------------------------

    def on_choice(self, digest):
        # type: (str) -> object
        """在候选窗口里点某一条：带它的 digest 重发一次导入。

        **digest 先在本地的候选表里比一遍**：不比对的话，一个来路不明的
        digest 会换回一条 `craft_no_recipe` 回执，而玩家只是点了一下界面。
        服务端仍会拿它的配方表重挑一遍（`PresetRecipe.digest()` 的文档说明它
        不是安全边界），这里只是别把明显不成立的请求发出去。
        """
        if self.closed or not self.client.has_session() or self.choice is None:
            return None
        candidates = self.choice["candidates"]
        found = False
        for candidate in candidates:
            if candidate.get("digest") == digest:
                found = True
                break
        if not found:
            return None
        output = self.choice.get("output") or {}
        name = output.get("name")
        self.close_window()
        self.choice = None
        if not name:
            return None
        aux = output.get("aux")
        if isinstance(aux, bool) or not isinstance(aux, INTEGER_TYPES):
            aux = 0
        return self._send(ACTION_IMPORT, key=ResourceKey.item(name, aux),
                          recipe=digest)

    def _open_choice(self, payload):
        # type: (dict) -> bool
        """服务端说「有多条配方，你挑一条」：把候选窗口开出来。

        窗口本身是**运行动态创建的**（用本界面自己的 `load_recipe_window`
        模板），失败时只计数、不改状态：玩家再点一次「导入」会重新拿到候选。
        抛出会把一次界面故障变成整个面板的故障，而那两条路的风险完全不对等。
        """
        output = payload.get("output")
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return False
        self.choice = {
            "output": dict(output) if isinstance(output, dict) else {},
            "candidates": [dict(item) for item in candidates
                           if isinstance(item, dict)],
        }
        return self.open_window()

    def open_window(self):
        # type: () -> bool
        """把候选窗口开出来。**没有会话就不开**：开了也只是一扇点不动的窗——
        点哪一条都会走到 `on_choice` 的会话判据上，而玩家看到的是一扇正常的
        窗口怎么点都没反应。"""
        if self.closed or self.choice is None:
            return False
        if not self.client.has_session():
            return False
        try:
            self.window = RecipeChoiceWindow(
                self.screen, self.choice["output"], self.choice["candidates"],
                self.on_choice, self.on_choice_cancelled)
        except Exception:
            self.errors += 1
            self.window = None
            return False
        return True

    def on_choice_cancelled(self):
        # type: () -> None
        """关掉候选窗口 = 取消这次选择，**不取料、不发命令**。"""
        self.choice = None
        self.window = None

    def close_window(self):
        # type: () -> None
        if self.window is not None:
            self.window.close()
            self.window = None

    def close_picker(self):
        # type: () -> None
        """关掉产物选择窗（如果开着）。销毁时必须调——它挂在屏幕的 `_vars` 上，
        留着的话下一次打开本面板会被当成「已经有一个开着」。"""
        picker = self.screen._vars.get(PICKER_WINDOW_ATTRIBUTE)
        if picker is not None:
            picker.close()

    # -- 回执 ----------------------------------------------------------

    def on_results(self, taken):
        # type: (list) -> int
        """处理本帧被取走的命令回执，返回处理了几条。

        **回执不在这里取**（`take_command_results`）：那是一条队列，面板上的
        列表视图已经在取，两处各取一次会互相吃掉对方的回执——而列表那边吃掉
        的是「清掉选中」的信号，症状是选中框赖着不走。因此由列表视图取，
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
        payload = result.get("payload")
        if not isinstance(payload, dict):
            return
        if action == ACTION_IMPORT and payload.get("needs_choice"):
            self._open_choice(payload)

    @staticmethod
    def _error_text(result):
        # type: (dict) -> str
        """把回执里的原因码翻成人话。**取第一条**：服务端按重要性排序。

        认不出的原因返回空串而不是原样回显代码——`no_capacity` 这种词对玩家
        什么也没说，而把它塞进一个将来会出现的提示控件里，只会让人以为界面
        出了别的毛病。
        """
        reasons = result.get("reasons")
        if not isinstance(reasons, list) or not reasons:
            return ""
        first = reasons[0]
        if not isinstance(first, dict):
            return ""
        return REASON_TEXTS.get(first.get("code"), "")

    # -- 生命周期 ------------------------------------------------------

    def tick(self):
        # type: () -> None
        """每帧：会话没了就把在途与窗口一起收尾。"""
        if self.closed:
            return
        self._settle_lost_session()

    def _settle_lost_session(self):
        # type: () -> None
        """会话没了就收尾。判据取「会话在不在」，不取「收到了哪条错误」——
        与 `TerminalListView._settle_lost_session` 同一条：那样对所有丢会话的
        原因都成立（服务端判死、玩家走远、换维度、设备被换掉）。

        没有这一条，界面会停在「假活」：候选窗口开着、在途表里挂着一条永远
        不会回来的请求，玩家点哪儿都只是再发一条没有下文的命令。
        """
        if self.client.has_session():
            return
        if not self._requests and self.choice is None:
            return
        self._requests = {}
        self.choice = None
        self.close_window()

    def close(self):
        # type: () -> None
        """收尾。可以重复调用。"""
        if self.closed:
            return
        self.closed = True
        self.close_window()
        self.close_picker()
        self._requests = {}
        self.choice = None
        self.cells = (None,) * CRAFT_SLOT_COUNT
        self.preview = None
        self.recipe = ""

    def diagnostics(self):
        # type: () -> dict
        return {
            "closed": self.closed,
            "revision": self.revision,
            "cells": sum(1 for cell in self.cells if cell is not None),
            "recipe": self.recipe,
            "preview": None if self.preview is None else dict(self.preview),
            "choosing": None if self.choice is None else len(self.choice["candidates"]),
            "in_flight": len(self._requests),
            "errors": self.errors,
            "last_error": self.last_error,
            "receipts": list(self.receipts),
        }


def _item_of(entry):
    # type: (object) -> object
    """一个 `{name, aux, count}` → `Item`；形状不对返回 `None`。

    配方投影里的 `name` 是**普通物品 ID 字符串**（`"minecraft:stick"`），
    不是 `ResourceKey`——`PresetRecipe.to_dict()` 的文档写明了这一点。
    """
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    if not name:
        return None
    aux = entry.get("aux")
    if isinstance(aux, bool) or not isinstance(aux, INTEGER_TYPES):
        aux = 0
    count = entry.get("count")
    if isinstance(count, bool) or not isinstance(count, INTEGER_TYPES) or count < 1:
        count = 1
    return Item(name, aux, count)


class RecipeChoiceWindow(object):
    """多配方候选：一个产物有好几条配方时，让玩家挑一条再导入。

    **运行动态创建，不改 JSON。** 用的是本界面自己的 `load_recipe_window`
    与 `recipe_section` 两个模板——它们本来就是给这件事画的，只是交接时是
    静态的一个占位实例（UI 文档第 4 节：「当前 `load_recipe_window` 滚动内容
    仅引用一个 `recipe_section`，未声明多项 collection 或动态工厂。接入时须让
    全部候选配方各有一个预览项及"导入"按钮」）。本轮按这句话逐条铺实例。

    **为什么不走集合绑定**（像九宫格那样）：一条配方要同时画 9 格原料 + 1 个
    产物 + 1 个按钮，而集合绑定一格里只能放一个值，`recipe_section` 的内层
    槽位没有嵌套集合可用。这条路在 JSON UI 里表达不出来，只能脚本铺。

    代价如实记：铺出来的候选**不参与滚动**（见 `RECIPE_MAX_ROWS`），且每一格
    是脚本按控件路径写进去的——这正是 11-03 从网络列表上撤下来的那条路。
    区别在**格子是它自己刚建的、当帧就填**（不是去摸一个由渲染器延迟生成、
    之后还会被回收换出的格子），因此不存在「拿到过期引用」那个已知故障。
    """

    WINDOW_ATTRIBUTE = RECIPE_WINDOW_ATTRIBUTE

    def __init__(self, screen, output, candidates, choose, cancel=None):
        # type: (object, dict, list, object, object) -> None
        self.screen = screen
        self.output = dict(output or {})
        self.candidates = list(candidates)
        self.choose = choose
        self.cancel = cancel
        self.closed = False
        self.root = None
        self.shown = 0
        screen._vars[self.WINDOW_ATTRIBUTE] = self
        try:
            self._create_controls()
        except Exception:
            self.close()
            raise

    def _create_controls(self):
        # type: () -> None
        screen = self.screen
        self.root = screen.AddElement(
            RECIPE_WINDOW_DEFINITION, RECIPE_WINDOW_NAME)
        # 压在主面板之上：主面板的遮罩是 layer 20、接近 30 的控件也有，
        # 这里取 50（与 `select_item_window` 同一个层）。
        self.root.SetLayer(50)
        content = self._content()
        for index in range(min(len(self.candidates), RECIPE_MAX_ROWS)):
            candidate = self.candidates[index]
            if not isinstance(candidate, dict):
                continue
            section = content.AddElement(
                RECIPE_SECTION_DEFINITION, "candidate_%d" % (index,))
            section.SetAnchorFrom("top_left")
            section.SetAnchorTo("top_left")
            # 窗口根不是 3×3 的网格，逐条往下排：`recipe_section` 高 62，
            # 留 2 像素缝。
            section.SetPos((6.0, index * RECIPE_ROW_HEIGHT))
            self._fill(section, candidate)
            section.GetElement("confirm_btn").asButton().SetCallback(
                partial(self._on_confirm, candidate))
            self.shown += 1
        self.root.GetElement("close").asButton().SetCallback(self.close)

    def _content(self):
        # type: () -> object
        """候选铺进去的那一层：窗口里滚动区的内容控件。

        **路径不写死**：内容在 `scroll_view` 之下，而那一层的名字随输入方式
        分叉（鼠标端 `scroll_mouse`、触控端 `scroll_touch`），引擎自己知道
        内容是谁。取不到就退回窗口根——铺上去仍然看得见（会盖住标题，属于
        「窗口没按预期搭起来」的可见故障，比静默什么都不显示好查）。
        """
        content = slot_layout.scroll_content_at(self.screen, RECIPE_SCROLL_PATH)
        if content is not None:
            return content
        return self.root

    def _fill(self, section, candidate):
        # type: (object, dict) -> None
        """把一条配方投影画进这个 section。不持有任何实物。"""
        slots = candidate.get("slots")
        if not isinstance(slots, list):
            slots = []
        for index in range(CRAFT_SLOT_COUNT):
            cell = section.GetElement("input_slots/slot%d" % (index,))
            entry = slots[index] if index < len(slots) else None
            self._paint(cell, entry)
        outputs = candidate.get("outputs")
        if not isinstance(outputs, list):
            outputs = []
        self._paint(section.GetElement("output_slot"),
                    outputs[0] if outputs else None)

    @staticmethod
    def _paint(cell, entry):
        # type: (object, object) -> None
        """一格原料／产物：有东西就画，没有就**藏起来**。

        藏而不是留空底图：配方本来没有原料的位置保持为空（UI 文档第 4 节），
        一格空的槽底会让玩家以为「这里要放东西但预览没显示出来」。
        """
        item = _item_of(entry)
        if item is None:
            cell.SetVisible(False)
            return
        cell.SetVisible(True)
        cell.GetElement("item_renderer").asItemRenderer().SetUiItem(item)
        count = entry.get("count") if isinstance(entry, dict) else None
        label = cell.GetElement("item_count").asLabel()
        if (isinstance(count, INTEGER_TYPES) and not isinstance(count, bool)
                and count > 1):
            label.SetText(str(count))
        else:
            label.SetText("")

    def _on_confirm(self, candidate, params=None):
        # type: (dict, object) -> None
        digest = candidate.get("digest") if isinstance(candidate, dict) else None
        callback = self.choose
        self.close()
        if callback is not None and digest:
            callback(digest)

    def close(self, params=None):
        # type: (object) -> None
        """关窗。**取消不取料**：这里除了界面什么都没动。"""
        if self.closed:
            return
        self.closed = True
        if self.screen._activated and self.root is not None:
            self.root.Remove()
        if self.screen._vars.get(self.WINDOW_ATTRIBUTE) is self:
            del self.screen._vars[self.WINDOW_ATTRIBUTE]
        cancel = self.cancel
        self.cancel = None
        self.choose = None
        self.root = None
        self.shown = 0
        if cancel is not None:
            cancel()


@RegistToolDeltaScreen("crafting_terminal_panel_ui.main")
class CraftingPanel(TerminalPanelBase):
    """合成终端：通用终端的一切 + 九宫格工作台。

    接线与 `CommonTerminalPanel.OnCreate` 逐条对应，多出来的只有 `craft`
    这一个模型、以及它那条 `craft_state` 收包回调。**没有权威身份就整块暂停**
    （同 11.md 的 UI 前置约定）：缺交付时保持暂停，而不是先写一段假设。
    """

    #: 与 `list_view` 同一个地位：绑定比 `OnCreate` 先生效，而它只在拿到
    #: 权威身份时才建，因此这里是类属性兜着。没有它时九宫格与产物都是空的
    #: ——**不是假的**，就是没接到数据。
    craft = None

    # -- 九宫格与产物槽的绑定（类体注册） ------------------------------
    #
    # 每条都先看 `self.craft` 在不在：没有权威身份的会话（`OpenDemo`）永远不建
    # 它，而绑定照样会被引擎问。缺席时给**安全值**——报一个「有 N 格」出去
    # 就会画出幽灵格子。
    #
    # **九宫格与输出槽各一套**：集合绑定的回调只拿得到集合内下标、拿不到集合名
    # （同 `terminal_panel.SEGMENTS` 那条），共用一套的话输出槽会按九宫格的下标
    # 去读。

    @Binder.binding_collection(
        Binder.BF_BindBool, GRID_COLLECTION, GRID_VISIBLE_BINDING)
    def craft_grid_visible(self, index):
        # type: (object) -> bool
        """**恒真，空格子也要画**：空是用 `#item_id_aux == 0` 表达的，
        不是靠隐藏（同下半屏那八条）。"""
        return True

    @Binder.binding_collection(
        Binder.BF_BindInt, GRID_COLLECTION, GRID_ITEM_ID_AUX_BINDING)
    def craft_grid_item_id_aux(self, index):
        # type: (object) -> int
        if self.craft is None:
            return 0
        return self.craft.cell_id_aux(index)

    @Binder.binding_collection(
        Binder.BF_BindString, GRID_COLLECTION, GRID_ITEM_COUNT_BINDING)
    def craft_grid_item_count(self, index):
        # type: (object) -> str
        if self.craft is None:
            return ""
        return self.craft.cell_count(index)

    @Binder.binding_collection(
        Binder.BF_BindBool, GRID_COLLECTION, GRID_SELECTED_BINDING)
    def craft_grid_selected(self, index):
        # type: (object) -> bool
        """九宫格**没有选中态**。模板声明了这个变量，因此必须有人答它——
        答假值是「这里没有选中这回事」，而漏答是引擎拿模板默认值（指向上半屏
        的集合），那是另一回事。"""
        return False

    @Binder.binding_collection(
        Binder.BF_BindBool, OUTPUT_COLLECTION, OUTPUT_VISIBLE_BINDING)
    def craft_output_visible(self, index):
        # type: (object) -> bool
        if self.craft is None:
            return False
        return self.craft.output_visible()

    @Binder.binding_collection(
        Binder.BF_BindInt, OUTPUT_COLLECTION, OUTPUT_ITEM_ID_AUX_BINDING)
    def craft_output_item_id_aux(self, index):
        # type: (object) -> int
        if self.craft is None:
            return 0
        return self.craft.output_id_aux()

    @Binder.binding_collection(
        Binder.BF_BindString, OUTPUT_COLLECTION, OUTPUT_ITEM_COUNT_BINDING)
    def craft_output_item_count(self, index):
        # type: (object) -> str
        if self.craft is None:
            return ""
        return self.craft.output_count()

    @Binder.binding_collection(
        Binder.BF_BindBool, OUTPUT_COLLECTION, OUTPUT_SELECTED_BINDING)
    def craft_output_selected(self, index):
        # type: (object) -> bool
        return False

    #: 四条按钮绑定。**都不看 `params`**，但签名里必须收它——引擎按位置传。
    #: 转发到工作台而不是就地处理：会话与在途的账都在那边。

    @Binder.binding(Binder.BF_ButtonClickUp, GRID_CLICK_BINDING)
    def OnCraftGridClick(self, params=None):
        # type: (object) -> None
        if self.craft is not None:
            self.craft.on_grid_click(params)

    @Binder.binding(Binder.BF_ButtonClickUp, OUTPUT_TAKE_BINDING)
    def OnCraftTake(self, params=None):
        # type: (object) -> None
        if self.craft is not None:
            self.craft.on_take(params)

    @Binder.binding(Binder.BF_ButtonClickUp, IMPORT_BINDING)
    def OnCraftImport(self, params=None):
        # type: (object) -> None
        """点「导入」。**幂等**：选择窗已经开着时 `show_select_item_window`
        返回假，不会再开一个。"""
        if self.craft is not None:
            self.craft.on_import(params)

    @Binder.binding(Binder.BF_ButtonClickUp, CLEAR_BINDING)
    def OnCraftClear(self, params=None):
        # type: (object) -> None
        if self.craft is not None:
            self.craft.on_clear(params)

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
        # 先挂收包再开会话：`session_ready` 与第一份 `craft_state` 都可能比
        # 这个函数返回还早（同 `CommonTerminalPanel` 那条注释）。
        client.port.listen(client.receive)
        self.status = device_status.DeviceStatusModel(device_ref)
        client.on_message(self._make_status_handler(client))
        self.craft = CraftingView(self, client=client)
        client.on_message(self._make_craft_handler(client))
        self.list_view = TerminalListView(
            self,
            cache=TerminalCache(client=client),
            device_ref=device_ref,
            dimension=dimension,
            backpack=self.backpack,
            # **本界面的滚动控件路径没有运行时结论**，所以不接管滚动条：
            # 见模块文档的已知缺项。列表内容、搜索、排序都不受它影响。
            scroll_path=None,
            # 回执只能有一处取走（队列语义），由列表视图取、取完转发给工作台。
            receipt_sink=self._on_receipts,
        )
        client.open_session(device_ref, dimension)

    def _make_status_handler(self, client):
        # type: (object) -> object
        """收设备状态的回调。与 `CommonTerminalPanel` 那一份逐条相同。"""
        def on_status_message(envelope, message):
            # type: (object, object) -> None
            if message is None or message.kind != MSG_DEVICE_STATUS:
                return
            if envelope.session_id != client.session_id:
                return
            if self.status is not None:
                self.status.apply(message)

        return on_status_message

    def _make_craft_handler(self, client):
        # type: (object) -> object
        """收 `craft_state` 的回调。

        两道过滤与状态那条完全一样，缺一不可：会话号对不上就丢（旧会话的包
        可能在关掉之后才到）；`CraftingView.apply` 自己再挡一次旧版本。回调
        **不抛**：它在 `receive()` 的派发循环里跑，抛出去会连累同一批里的
        其它消息。
        """
        def on_craft_message(envelope, message):
            # type: (object, object) -> None
            if message is None or message.kind != MSG_CRAFT_STATE:
                return
            if envelope.session_id != client.session_id:
                return
            if self.craft is not None:
                self.craft.apply(message)

        return on_craft_message

    def _on_receipts(self, taken):
        # type: (list) -> None
        """列表视图取走回执之后转给工作台（见 `CraftingView.on_results`）。"""
        if self.craft is not None:
            self.craft.on_results(taken)

    def OnTicking(self):
        # **基类的背包轮询要显式调**：本方法没有 `super()`（同
        # `CommonTerminalPanel.OnTicking`），基类加一个同名方法会被覆盖而
        # **永不执行且不报错**，症状只是「背包一直不更新」。
        self.fly.tick()
        self._poll_backpack()
        if self.list_view is not None:
            self.list_view.tick()
        if self.craft is not None:
            self.craft.tick()

    def OnDestroy(self):
        if self.list_view is not None:
            self.list_view.close()
            self.list_view = None
        if self.craft is not None:
            self.craft.close()
            self.craft = None
        self.status = None
        TerminalPanelBase.OnDestroy(self)

    @classmethod
    def OpenDemo(cls):
        """客户端手工自查用：在 `UiInitFinishedEvent` 注册过界面之后调。

        **没有权威身份，因此整块暂停**（同 `CommonTerminalPanel.OpenDemo`）：
        九宫格空着、产物空着、列表也空着。要走通整条链路就得由服务端
        `PushUIRequest` 推过来（右键合成终端）。
        """
        return cls.PushUI({})
