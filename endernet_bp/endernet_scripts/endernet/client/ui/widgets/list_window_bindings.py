# coding=utf-8
"""三个辅助窗口的**类体绑定**：物品名单／流体名单／物品选择器（11-07）。

这三个窗口是**挂在任意宿主屏上的辅助对象**（用 `AddElement` 建出来的），
它们原先靠自己声明的运行时注册（`DynaBinding` / `DynaBindingCollection`）。
那条路在**当时**不生效——引擎在渲染期按回调自己的名字到 `_Base` 上
`getattr`，而运行时注册的包装函数叫 `reader`，于是每渲染一帧报一次
`no attribute 'reader'`、格子全空，而**建界面时不报错**。真机证据见
`plans/tasks/11-交付说明.md` 第十二节；那一节同时点名了这三个文件
「要用之前必须先照本节改」。本模块就是那一次改。

改法是**让宿主屏在类体里声明转发的绑定**，与 `widgets/device_status_bindings.py`
同形：窗口对象退化成「模型 + 控制器」（它自己一声不响地留着 `get_count` /
`has_item` / `is_selected` 这些纯查询），绑定名落在宿主类的类体上，
每条回调从 `screen._vars` 取窗口对象，取不到就给安全值。

## 三条纪律

1. **必须写在类体里。** 混入者只管继承，不要在任何地方再调
   `DynaBinding`。这是本模块既有静态绑定的约定；ToolDelta 动态绑定修复后，
   物品选择器的网格在窗口对象中使用独立的动态绑定（2026-10-03）。
2. **每条回调都要容忍窗口还没建起来、或已经关掉。** 绑定跟着界面走，
   而窗口是按需建的；安全值是「空」（`""` / `0` / `False`），
   不是「假装有一行」——报一个「有 N 行」出去就会画出幽灵格子。
3. **一个宿主屏同时只开一个同种窗口。** 绑定名在宿主上是**全局**的，
   两条同种窗口共存会互相盖。`show_item_list_window` 一族已经因此拒绝
   第二次打开（`WINDOW_ATTRIBUTE in screen._vars` 就返回 `False`），
   这里不重复判断。

## `*_content_binding_name` 那一项**有意不接**

`select_item_window` 的搜索框声明了
`$text_edit_box_content_binding_name = "#EnderNetSelectItem.search_content"`，
而**全仓没有任何 Python 注册过它**——终端的搜索框
（`#EnderNetTerminal.search_content`）同样如此，而终端的搜索**已在真机上
验过可用**（2026-10-01）。那是编辑器给 `text_edit_box` 的默认参数，
与 image 上那对静态 `clip_ratio: 0.0` 属同一类噪声（见记忆与交付说明的
相关条目）。硬接它反而有害：搜索文本在脚本里被 strip + 小写，
把它读回去覆盖输入框，玩家打「Iron」会被当场改成「iron」。

因此本模块只接 `$text_box_name` 那一条——**已验证可用的那一半**。
"""

from .....tooldelta.ui import Binder

from .window_identity import (
    FLUID_LIST_BINDING_PREFIX,
    FLUID_LIST_COLLECTION,
    FLUID_LIST_WINDOW_ATTRIBUTE,
    ITEM_LIST_BINDING_PREFIX,
    ITEM_LIST_COLLECTION,
    ITEM_LIST_WINDOW_ATTRIBUTE,
    NUMBER_SET_EDIT_BINDING,
    NUMBER_SET_WINDOW_ATTRIBUTE,
    SELECT_FLUID_SEARCH_BINDING,
    SELECT_FLUID_WINDOW_ATTRIBUTE,
    SELECT_ITEM_SEARCH_BINDING,
    SELECT_ITEM_WINDOW_ATTRIBUTE,
)

# **不从窗口模块 import 这些常量**：窗口模块会 import 引擎，而本模块被
# 终端屏 import——那会把整条 tooldelta 初始化链拖进「导入终端屏」这一步。
# 常量的家是 `window_identity.py`，那里一个引擎 import 都没有。


def _window_of(screen, attribute):
    # type: (object, str) -> object
    """从宿主屏的 `_vars` 取那个窗口对象；没有就返回 `None`。

    `_vars` 是 `ToolDeltaScreen.__init__` 建的（因此绑定解析时它一定在），
    但测试替身未必有——`getattr` 兜底比让一条绑定把整个界面炸掉划算。
    """
    screen_vars = getattr(screen, "_vars", None)
    if not isinstance(screen_vars, dict):
        return None
    return screen_vars.get(attribute)


class ItemListWindowBindingsMixin(object):
    """给一块屏接上 `endernet_windows.item_list_window`（物品名单编辑器）。

    混入者只需继承，什么都不用提供：窗口实例按 `WINDOW_ATTRIBUTE` 从自己的
    `_vars` 里找。宿主**必须**已经让 `show_item_list_window` 建过窗口，
    否则每条回调返回安全值（界面照常渲染，只是空着）。
    """

    def _item_list_window(self):
        # type: () -> object
        return _window_of(self, ITEM_LIST_WINDOW_ATTRIBUTE)

    # -- 窗口级（global）-----------------------------------------------

    @Binder.binding(Binder.BF_BindString, ITEM_LIST_BINDING_PREFIX + "title")
    def item_list_title(self):
        # type: () -> str
        window = self._item_list_window()
        if window is None:
            return ""
        return window.get_title()

    @Binder.binding(Binder.BF_BindInt, ITEM_LIST_BINDING_PREFIX + "count")
    def item_list_count(self):
        # type: () -> int
        window = self._item_list_window()
        if window is None:
            return 0
        return window.get_count()

    @Binder.binding(Binder.BF_BindBool, ITEM_LIST_BINDING_PREFIX + "can_add")
    def item_list_can_add(self):
        # type: () -> bool
        window = self._item_list_window()
        if window is None:
            return False
        return window.can_add()

    @Binder.binding(Binder.BF_BindBool, ITEM_LIST_BINDING_PREFIX + "can_delete")
    def item_list_can_delete(self):
        # type: () -> bool
        window = self._item_list_window()
        if window is None:
            return False
        return window.can_delete()

    @Binder.binding(Binder.BF_ButtonClickUp, ITEM_LIST_BINDING_PREFIX + "select")
    def item_list_select(self, params=None):
        # type: (object) -> None
        window = self._item_list_window()
        if window is not None:
            window.select(params)

    # -- 每行（collection）---------------------------------------------

    @Binder.binding_collection(
        Binder.BF_BindBool, ITEM_LIST_COLLECTION,
        ITEM_LIST_BINDING_PREFIX + "has_item")
    def item_list_has_item(self, index):
        # type: (object) -> bool
        window = self._item_list_window()
        if window is None:
            return False
        return window.has_item(index)

    @Binder.binding_collection(
        Binder.BF_BindInt, ITEM_LIST_COLLECTION,
        ITEM_LIST_BINDING_PREFIX + "item_id_aux")
    def item_list_item_id_aux(self, index):
        # type: (object) -> int
        window = self._item_list_window()
        if window is None:
            return 0
        return window.get_item_id_aux(index)

    @Binder.binding_collection(
        Binder.BF_BindBool, ITEM_LIST_COLLECTION,
        ITEM_LIST_BINDING_PREFIX + "selected")
    def item_list_selected(self, index):
        # type: (object) -> bool
        window = self._item_list_window()
        if window is None:
            return False
        return window.is_selected(index)

    @Binder.binding_collection(
        Binder.BF_BindString, ITEM_LIST_COLLECTION,
        ITEM_LIST_BINDING_PREFIX + "item_count")
    def item_list_item_count(self, index):
        # type: (object) -> str
        """行上的数量文字。**这个窗口列的是物品标识，没有数量。**

        行模板 `item_can_be_selected` 声明了它（与终端列表共用同一份模板），
        因此这里必须有个落点：不注册的话那一条绑定解析不到，而引擎对这种
        残缺不报错——只是安静地给空。显式返回空串，让「JSON 每个绑定名都有
        落点」成为可断言的不变式。
        """
        return ""

    # -- 给测试用 ------------------------------------------------------

    @classmethod
    def item_list_binding_names(cls):
        # type: () -> tuple
        """本 mixin 认下的绑定名。**由 JSON 契约用例对账用**，不参与运行时。"""
        return (
            ITEM_LIST_BINDING_PREFIX + "title",
            ITEM_LIST_BINDING_PREFIX + "count",
            ITEM_LIST_BINDING_PREFIX + "can_add",
            ITEM_LIST_BINDING_PREFIX + "can_delete",
            ITEM_LIST_BINDING_PREFIX + "select",
            ITEM_LIST_BINDING_PREFIX + "has_item",
            ITEM_LIST_BINDING_PREFIX + "item_id_aux",
            ITEM_LIST_BINDING_PREFIX + "selected",
            ITEM_LIST_BINDING_PREFIX + "item_count",
        )


class FluidListWindowBindingsMixin(object):
    """给一块屏接上 `endernet_windows.fluid_list_window`（流体名单编辑器）。

    与物品那一份逐条同形：两个窗口本就用同一份行模板的做法，
    只是绑定名与 collection 各用自己的一set（`fluid_can_be_selected`
    里的名字是写死的，不像物品那份走 `$` 参数）。
    """

    def _fluid_list_window(self):
        # type: () -> object
        return _window_of(self, FLUID_LIST_WINDOW_ATTRIBUTE)

    @Binder.binding(Binder.BF_BindString, FLUID_LIST_BINDING_PREFIX + "title")
    def fluid_list_title(self):
        # type: () -> str
        window = self._fluid_list_window()
        if window is None:
            return ""
        return window.get_title()

    @Binder.binding(Binder.BF_BindInt, FLUID_LIST_BINDING_PREFIX + "count")
    def fluid_list_count(self):
        # type: () -> int
        window = self._fluid_list_window()
        if window is None:
            return 0
        return window.get_count()

    @Binder.binding(Binder.BF_BindBool, FLUID_LIST_BINDING_PREFIX + "can_add")
    def fluid_list_can_add(self):
        # type: () -> bool
        window = self._fluid_list_window()
        if window is None:
            return False
        return window.can_add()

    @Binder.binding(Binder.BF_BindBool, FLUID_LIST_BINDING_PREFIX + "can_delete")
    def fluid_list_can_delete(self):
        # type: () -> bool
        window = self._fluid_list_window()
        if window is None:
            return False
        return window.can_delete()

    @Binder.binding(Binder.BF_ButtonClickUp, FLUID_LIST_BINDING_PREFIX + "select")
    def fluid_list_select(self, params=None):
        # type: (object) -> None
        window = self._fluid_list_window()
        if window is not None:
            window.select(params)

    @Binder.binding_collection(
        Binder.BF_BindBool, FLUID_LIST_COLLECTION,
        FLUID_LIST_BINDING_PREFIX + "has_item")
    def fluid_list_has_item(self, index):
        # type: (object) -> bool
        window = self._fluid_list_window()
        if window is None:
            return False
        return window.has_item(index)

    @Binder.binding_collection(
        Binder.BF_BindInt, FLUID_LIST_COLLECTION,
        FLUID_LIST_BINDING_PREFIX + "item_id_aux")
    def fluid_list_item_id_aux(self, index):
        # type: (object) -> int
        window = self._fluid_list_window()
        if window is None:
            return 0
        return window.get_item_id_aux(index)

    @Binder.binding_collection(
        Binder.BF_BindBool, FLUID_LIST_COLLECTION,
        FLUID_LIST_BINDING_PREFIX + "selected")
    def fluid_list_selected(self, index):
        # type: (object) -> bool
        window = self._fluid_list_window()
        if window is None:
            return False
        return window.is_selected(index)

    @classmethod
    def fluid_list_binding_names(cls):
        # type: () -> tuple
        return (
            FLUID_LIST_BINDING_PREFIX + "title",
            FLUID_LIST_BINDING_PREFIX + "count",
            FLUID_LIST_BINDING_PREFIX + "can_add",
            FLUID_LIST_BINDING_PREFIX + "can_delete",
            FLUID_LIST_BINDING_PREFIX + "select",
            FLUID_LIST_BINDING_PREFIX + "has_item",
            FLUID_LIST_BINDING_PREFIX + "item_id_aux",
            FLUID_LIST_BINDING_PREFIX + "selected",
        )


class SelectItemWindowBindingsMixin(object):
    """给一块屏接上 `endernet_windows.select_item_window`（物品选择器）。

    **只有一条绑定**：搜索框的 `BF_EditChanged`。其余控件（三个来源按钮、
    关闭）走控件级 `SetCallback`。网格可见性、物品图标与集合点击由窗口
    在打开时动态注册，关闭时解除；这里仅转发静态搜索绑定。
    """

    def _select_item_window(self):
        # type: () -> object
        return _window_of(self, SELECT_ITEM_WINDOW_ATTRIBUTE)

    @Binder.binding(Binder.BF_EditChanged, SELECT_ITEM_SEARCH_BINDING)
    def select_item_search(self, params):
        # type: (object) -> None
        window = self._select_item_window()
        if window is not None:
            window.on_search(params)

    @classmethod
    def select_item_binding_names(cls):
        # type: () -> tuple
        return (SELECT_ITEM_SEARCH_BINDING,)


class SelectFluidWindowBindingsMixin(object):
    """给一块屏接上 `endernet_windows.select_fluid_window`（流体选择器）。

    与物品选择器逐条同形，**只有搜索框那一条绑定名不同**：两份 JSON 各用
    各的名字，因此两个混入者不能互相顶替（物品那份挂在 `#EnderNetSelectItem`
    上，这份挂在 `#EnderNetSelectFluid` 上）。
    """

    def _select_fluid_window(self):
        # type: () -> object
        return _window_of(self, SELECT_FLUID_WINDOW_ATTRIBUTE)

    @Binder.binding(Binder.BF_EditChanged, SELECT_FLUID_SEARCH_BINDING)
    def select_fluid_search(self, params):
        # type: (object) -> None
        window = self._select_fluid_window()
        if window is not None:
            window.on_search(params)

    @classmethod
    def select_fluid_binding_names(cls):
        # type: () -> tuple
        return (SELECT_FLUID_SEARCH_BINDING,)


class NumberSetWindowBindingsMixin(object):
    """给一块屏接上 `endernet_windows.number_set_window`（数量窗）。

    与两个选择器同一处境：数量窗也是**挂在宿主屏上的辅助对象**，原先用
    运行时注册声明输入框那一条 `BF_EditChanged`。它今天有两个**生产**调用点
    （长按网络列表开下单预览、样板终端长按改数量），因此这条不是「将来会
    用到」——是已经在用的路上，而那条路一旦被真手指走到就会踩到那个缺陷。

    其余控件（六步进键、关闭、确认）走控件级 `SetCallback`，原样保留。
    """

    def _number_set_window(self):
        # type: () -> object
        return _window_of(self, NUMBER_SET_WINDOW_ATTRIBUTE)

    @Binder.binding(Binder.BF_EditChanged, NUMBER_SET_EDIT_BINDING)
    def number_set_edit(self, params):
        # type: (object) -> None
        window = self._number_set_window()
        if window is not None:
            window.on_edited(params)

    @classmethod
    def number_set_binding_names(cls):
        # type: () -> tuple
        return (NUMBER_SET_EDIT_BINDING,)
