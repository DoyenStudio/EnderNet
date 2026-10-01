# coding=utf-8
"""本界面各槽位在屏幕上的**全局矩形**，给飞行动画算起终点。

控件的路径集中在这里，`terminal_panel.py` 从本模块取（`SCROLL_PATH` 等），
免得同一条路径在两个文件里各写一份——那种重复的代价不是多打几个字，而是
改了 JSON 只改到一处，另一处静默指向一个不存在的控件。

**矩形是「现在」的。** 每次调用都现问引擎，不缓存：滚动一下、窗口一缩放、
背包行数一变，同一个槽位的坐标就变了。缓存下来的结果是动画飞到一个
已经没人站的位置，而那种错要盯着屏幕才看得出来。

**取不到就是 `None`，调用方据此不飞。** 本模块**不抛**：路径少一层、
格子还没生成、控件类型对不上，都在这里收成 `None`。理由是这些失败全都
只该让「这一次动画不做」，不该让搬运本身失败——搬运的成败由命令回执说，
不归这里管。

**这里给的是「控件」的矩形，不是「图标」的矩形。** 槽位控件是 18×18，
而槽里那张图标是 16×16、在槽位里居中，两者差 `(18 - 16) / 2 = 1` 像素。
这个换算**不在这里做**：本模块的职责是「屏幕上这个控件在哪儿」，而「图标
该落在哪一点」是 `flying_item` 的事（它按 `ICON_SIZE` 把图标摆到矩形正中，
一份规则同时管住槽位、整块区域与退化成一个点的点击处）。在这里也缩一次，
就会缩两遍——而两遍差 1 像素，只在屏幕上看得见。
"""

#: 内容面板（`terminal@common_terminal_panel_ui.content`）的路径。它同时是
#: 飞行动画的坐标系原点——`flying_item` 是它的子控件。
TERMINAL_ROOT = "/terminal"

#: 滚动区面板。**注意它自己是 `common.scrolling_panel`（一个 panel），不是
#: `scroll_view`**，真正的滚动控件在它下面一层，见 `SCROLL_VIEW_PATHS`。
SCROLL_PATH = TERMINAL_ROOT + "/terminal_items_scroll"

#: 背包那一半的整块面板（上 9x3 + 下 9x1）。
INVENTORY_PATH = TERMINAL_ROOT + "/inventory"

#: 两段格子。**与 `common_terminal_panel.json` 里的两个 collection 一一对应**，
#: 顺序是界面序：0..26 在背包网格、27..35 在快捷栏。
BACKPACK_GRID_PATH = INVENTORY_PATH + "/backpack"
HOTBAR_GRID_PATH = INVENTORY_PATH + "/hotbar"

#: 飞行动画用的那个图标控件。
FLYING_ITEM_PATH = TERMINAL_ROOT + "/flying_item"

try:
    INTEGER_TYPES = (int, long)
except NameError:
    INTEGER_TYPES = (int,)

#: 每行几格。两段格子与网络列表**都是 9 列**，因此一处定义。
COLUMNS = 9

#: 背包网格里有几格（9x3）。界面槽号到段的换算靠它。
BACKPACK_CELLS = 27

#: 滚动控件相对**滚动面板**的三条候选后缀。`terminal_items_scroll` 自己是
#: 面板，`scroll_view` 在它下面，而**那一层的名字随输入方式分叉**：鼠标端
#: `scroll_mouse`、触控端 `scroll_touch`（原版模板 `common.scrolling_panel`
#: 里两边靠 `ignored` 互斥）。写死一条就会在另一种设备上取不到控件——**那不
#: 报错**，只表现为「动画不动」，而动画不动看起来和「没触发」一模一样。
#:
#: 空串是既有代码的假设（把滚动面板直接当 `scroll_view`），留着它是因为它
#: 到底成不成立**还没在游戏里验过**：成功了就不必再往下试。
#:
#: **后缀而不是整条路径**：合成终端还有一个同样是 `common.scrolling_panel`
#: 的候选配方窗口（`client/ui/crafting_panel.py`），它的分叉方式一模一样而
#: 前缀不同。把三条候选写成后缀，两边共用一套，免得新界面上又抄一遍、
#: 抄漏一条。
SCROLL_VIEW_SUFFIXES = (
    "",
    "/scroll_mouse/scroll_view",
    "/scroll_touch/scroll_view",
)

SCROLL_VIEW_PATHS = tuple(SCROLL_PATH + suffix for suffix in SCROLL_VIEW_SUFFIXES)


def _control(screen, path):
    # type: (object, str) -> object
    """按路径取控件；取不到（或引擎抛）返回 `None`。"""
    try:
        return screen.GetBaseUIControl(path)
    except Exception:
        return None


def _rect(control):
    # type: (object) -> object
    """控件的全局矩形 `(x, y, 宽, 高)`；取不到返回 `None`。

    用 `GetGlobalPosition()` 而不是 `GetPosition()`：前者是屏幕坐标，起终点
    与坐标原点都在同一个坐标系里，减一下就是相对位置；后者是相对各自父节点
    的，两个槽位的父节点不同（一个在网格里、一个在滚动内容里），直接相减
    没有意义。
    """
    if control is None:
        return None
    try:
        x, y = control.GetGlobalPosition()
        width, height = control.GetSize()
        return (float(x), float(y), float(width), float(height))
    except Exception:
        return None


def scroll_view_at(screen, panel_path):
    # type: (object, str) -> object
    """**任意**滚动面板下面的滚动控件（`ScrollViewUIControl`）；取不到返回 `None`。

    `panel_path` 是这个滚动面板的完整路径（如 `/terminal/terminal_items_scroll`）。
    **不给默认值**：默认值会让调用方以为「不传就是本界面的那一个」，
    而本模块里唯一知道自己是哪个面板的地方是调用点。
    """
    for suffix in SCROLL_VIEW_SUFFIXES:
        control = _control(screen, panel_path + suffix)
        if control is None:
            continue
        try:
            view = control.asScrollView()
        except Exception:
            continue
        if view is not None:
            return view
    return None


def scroll_view(screen):
    # type: (object) -> object
    """本终端列表的滚动控件。"""
    return scroll_view_at(screen, SCROLL_PATH)


def scroll_content_at(screen, panel_path):
    # type: (object, str) -> object
    """**任意**滚动面板里那一层内容控件；取不到返回 `None`。"""
    return _content_of(scroll_view_at(screen, panel_path))


def scroll_content(screen):
    # type: (object) -> object
    """滚动区里的**内容控件**，也就是那张 `terminal_items_grid`。

    **内容路径不写死**：内容在 `scroll_view` 之下，而那一段的名字随输入方式
    分叉（见 `SCROLL_VIEW_PATHS`）。引擎自己知道内容是谁——原版模板里的
    `scroll_content` 属性就指名了它——问它要才是跨端一致的
    （`ScrollViewUIControl.GetScrollViewContentControl()`；tooldelta 的
    `UScrollView.GetContent()` 转发的是同一个接口）。

    两个调用口都试：运行版本里百分比接口要从 `.base` 上拿（既有代码就是
    这么调的），而文档把内容接口写在控件本身，两者对不上，因此都试一次。
    """
    return _content_of(scroll_view(screen))


def _content_of(view):
    # type: (object) -> object
    """从一个滚动控件上问出它的内容控件；取不到返回 `None`。"""
    if view is None:
        return None
    base = getattr(view, "base", None)
    for holder in (view, base):
        getter = getattr(holder, "GetScrollViewContentControl", None)
        if getter is None:
            continue
        try:
            control = getter()
        except Exception:
            continue
        if control is not None:
            return control
    return None


def _grid_cell_rect(screen, path, index):
    # type: (object, str, int) -> object
    """某个网格里第 `index` 格（按行优先、每行 `COLUMNS` 格）的矩形。"""
    control = _control(screen, path)
    if control is None:
        return None
    try:
        grid = control.asGrid()
    except Exception:
        return None
    if grid is None:
        return None
    try:
        cell = grid.GetGridItem(index % COLUMNS, index // COLUMNS)
    except Exception:
        return None
    return _rect(cell)


def backpack_slot_rect(screen, ui_index):
    # type: (object, object) -> object
    """**界面槽号**（0..35）→ 全局矩形；取不到返回 `None`。

    收的是界面槽号，不是引擎槽号——两套槽序方向相反（`backpack_model.
    engine_slot_of` 是它们之间唯一的换算），而点击路径上拿到的一律是界面
    槽号。这里不做那次换算：本模块只回答「屏幕上这一格在哪儿」，引擎槽号
    是另一件事（哪一格的数据），混进来就会在某一处把方向写反。
    """
    # `bool` 要单独挡：它是 `int` 的子类，而 `True` 会被算成第 1 格。
    if isinstance(ui_index, bool) or not isinstance(ui_index, INTEGER_TYPES):
        return None
    if not 0 <= ui_index < BACKPACK_CELLS + COLUMNS:
        return None
    if ui_index < BACKPACK_CELLS:
        return _grid_cell_rect(screen, BACKPACK_GRID_PATH, ui_index)
    return _grid_cell_rect(screen, HOTBAR_GRID_PATH, ui_index - BACKPACK_CELLS)


def backpack_area_rect(screen):
    # type: (object) -> object
    """背包那一半**整块**的矩形（上 9x3 + 下 9x1）。

    用在「网络 → 背包」这一向上：服务端把东西放进**第一个装得下的格子**，
    客户端看不到是哪一格，因此只能落到整块区域，而不假装知道是哪一格。
    """
    return _rect(_control(screen, INVENTORY_PATH))


def network_row_rect(screen, row):
    # type: (object, object) -> object
    """网络列表第 `row` 行 → 全局矩形；行取不到返回 `None`。

    行坐标**不是缓存下来的**：滚动之后同一个下标就是另一行了，所以每次
    现问内容控件（它的全局位置本来就带着滚动偏移）。
    """
    content = scroll_content(screen)
    if content is None:
        return None
    if isinstance(row, bool) or not isinstance(row, INTEGER_TYPES) or row < 0:
        return None
    try:
        grid = content.asGrid()
    except Exception:
        return None
    if grid is None:
        return None
    try:
        cell = grid.GetGridItem(row % COLUMNS, row // COLUMNS)
    except Exception:
        return None
    return _rect(cell)


def network_area_rect(screen):
    # type: (object) -> object
    """网络列表**整块**的矩形。

    用在「物品在网络里还没有行」的场合——它要飞进去的是那一份库存，而
    屏幕上还没有它落脚的地方；落到列表区的中心，比随便挑一行准。
    """
    return _rect(_control(screen, SCROLL_PATH))


#: 引擎附在按钮回调参数里的**点击位置**，键名取自
#: `SetButtonTouchDownCallback` 的 args 表（`TouchPosX` / `TouchPosY`，
#: 文档写明是「按钮被点击时屏幕上的 **UI 坐标**」）。PC 端是鼠标点下的
#: 位置，触屏端是手指按下的位置——同一套键，因此不必分端。
CLICK_X_KEY = "TouchPosX"
CLICK_Y_KEY = "TouchPosY"


def click_rect(params):
    # type: (object) -> object
    """这一次点击落在屏幕的哪里，包成一个**退化矩形** `(x, y, 0, 0)`。

    收的是**绑定回调收到的那个 params**，不是现问鼠标：要的是「玩家点的那
    一下在哪儿」，而不是「动画开始时鼠标在哪儿」——后者在那两下点击之后也
    可能已经被挪走了。

    坐标系与 `GetGlobalPosition()` **同一个**（都是 UI 坐标），所以可以直接
    当飞行动画的落点，不需要任何缩放换算。

    取不到就返回 `None`（键盘触发、参数形状变了、老引擎不带这两个键），
    调用方据此退回原来的落点——**不飞向 `(0, 0)`**，那会是一道横穿屏幕的
    假动画。
    """
    if not isinstance(params, dict):
        return None
    raw_x = params.get(CLICK_X_KEY)
    raw_y = params.get(CLICK_Y_KEY)
    # `bool` 单独挡：它是 `int` 的子类，`True` 会被 `float()` 收成 1.0。
    # 类型元组用模块级那份，别在这里写 `long`——离线跑的是 py3，那是个 NameError。
    for value in (raw_x, raw_y):
        if isinstance(value, bool):
            return None
        if not isinstance(value, INTEGER_TYPES) and not isinstance(value, float):
            return None
    try:
        return (float(raw_x), float(raw_y), 0.0, 0.0)
    except (TypeError, ValueError):
        return None
