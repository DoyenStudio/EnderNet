# coding=utf-8
"""物品名单编辑器：就地编辑一份物品标识列表（11-07 起）。

## 它不是自己绑定的：绑定在**宿主屏的类体**上

本对象原先用运行时注册（`DynaBinding`）自己声明 `#EnderNetItemList.*`。
那条路在**当时**的引擎上不生效（引擎按回调名字到 `_Base` 上 `getattr`，
而运行时注册的包装函数叫 `reader`），症状是每渲染一帧报一次
`no attribute 'reader'`、格子全空。真机证据与改法的由来见
`plans/tasks/11-交付说明.md` 第十二节。

因此本对象退化成**模型 + 控制器**：

- 绑定名与回调落在宿主的 `widgets/list_window_bindings.py` 上
  （`ItemListWindowBindingsMixin`），宿主屏继承它即可；
- 这里只留查询（`get_count` / `has_item` / `get_item_id_aux` /
  `is_selected` / `can_add` / `can_delete` / `select`），它们本来就是纯的；
- 控件级回调（加／删／关、每格的 `interact_btn`）走 `SetCallback`，
  **不经 `Binder` 注册表**，因此不受那个缺陷影响，原样保留。

`close()` 相应地不再「摘绑定」——绑定是静态的，本来也摘不掉；
它只关掉自己开的选择器、移除根控件、清掉 `_vars` 里的打开标记。
"""

from ....tooldelta.define.item import Item
from .select_item_window import (
    WINDOW_ATTRIBUTE as SELECT_WINDOW_ATTRIBUTE,
    show_select_item_window,
)
from .widgets.window_identity import (
    ITEM_LIST_BINDING_PREFIX as BINDING_PREFIX,
    ITEM_LIST_COLLECTION as COLLECTION,
    ITEM_LIST_WINDOW_ATTRIBUTE as WINDOW_ATTRIBUTE,
)

if 0 > 1:
    from ....tooldelta.ui import ToolDeltaScreen

try:
    INTEGER_TYPES = (int, long)
except NameError:
    INTEGER_TYPES = (int,)


def show_item_list_window(screen, item_ids, max_items, title="物品列表", on_selected=None):
    # type: (ToolDeltaScreen, list[str], int, str, object) -> bool
    """Edit a list, or close and call on_selected after picking one item."""
    if WINDOW_ATTRIBUTE in screen._vars:
        return False
    if not isinstance(item_ids, list):
        raise TypeError("item_ids must be a list")
    if isinstance(max_items, bool) or not isinstance(max_items, INTEGER_TYPES) or max_items < 0:
        raise ValueError("max_items must be a non-negative integer")
    if len(item_ids) > max_items:
        raise ValueError("item_ids exceeds max_items")
    ItemListWindow(screen, item_ids, max_items, title, on_selected)
    return True


class ItemListWindow(object):
    WINDOW_ATTRIBUTE = WINDOW_ATTRIBUTE
    SELECT_WINDOW_ATTRIBUTE = SELECT_WINDOW_ATTRIBUTE
    COLLECTION = COLLECTION
    BINDING_PREFIX = BINDING_PREFIX
    CONTROL_DEFINITION = "endernet_windows.item_list_window"
    CONTROL_NAME = "endernet_item_list_window"

    def __init__(self, screen, item_ids, max_items, title, on_selected=None):
        self.screen = screen
        self.item_ids = item_ids
        self.max_items = max_items
        self.title = title
        self.on_selected = on_selected
        self.selected_index = None
        self.closed = False
        self.root = None
        self.picker = None
        self._icons = {}
        screen._vars[self.WINDOW_ATTRIBUTE] = self
        try:
            self.root = screen.AddElement(
                self.CONTROL_DEFINITION, self.CONTROL_NAME
            )
            # Leave room for the scroll panel's own layers below the picker (50).
            self.root.SetLayer(30)
            self.root.GetElement("add_btn").asButton().SetCallback(self.add)
            self.root.GetElement("del_btn").asButton().SetCallback(self.delete_selected)
            self.root.GetElement("close").asButton().SetCallback(self.close)
        except Exception:
            self.close()
            raise

    def get_title(self):
        return self.title

    def get_count(self):
        return 0 if self.closed else len(self.item_ids)

    def has_item(self, index):
        return (not self.closed and not isinstance(index, bool)
                and isinstance(index, INTEGER_TYPES) and 0 <= index < len(self.item_ids))

    def get_item_id_aux(self, index):
        if not self.has_item(index):
            return 0
        item_id = self.item_ids[index]
        if item_id not in self._icons:
            self._icons[item_id] = Item(item_id).GetBasicInfo().id_aux
        return self._icons[item_id]

    def is_selected(self, index):
        return self.has_item(index) and index == self.selected_index

    def _picker_open(self):
        return self.SELECT_WINDOW_ATTRIBUTE in self.screen._vars

    def can_add(self):
        return not self.closed and not self._picker_open() and len(self.item_ids) < self.max_items

    def can_delete(self):
        return not self._picker_open() and self.has_item(self.selected_index)

    def select(self, params):
        index = params.get("#collection_index")
        if self._picker_open() or not self.has_item(index):
            return
        self.selected_index = index

    def add(self, params=None):
        if not self.can_add():
            return
        # This API has no network snapshot argument; the picker still offers
        # its loaded catalogue and the local backpack. Its network source is empty.
        if self._show_picker():
            self.picker = self.screen._vars[self.SELECT_WINDOW_ATTRIBUTE]

    def _show_picker(self):
        return show_select_item_window(self.screen, [], self._on_item_selected)

    def _on_item_selected(self, item_id):
        self.picker = None
        if self.closed:
            return
        if self.on_selected is not None:
            callback = self.on_selected
            self.close()
            callback(item_id)
            return
        if item_id in self.item_ids:
            self.selected_index = self.item_ids.index(item_id)
        elif len(self.item_ids) < self.max_items:
            self.item_ids.append(item_id)
            self.selected_index = len(self.item_ids) - 1

    def delete_selected(self, params=None):
        if not self.can_delete():
            return
        del self.item_ids[self.selected_index]
        self.selected_index = None

    def close(self, params=None):
        """Keep edits, close an owned picker, and drop this window's open marker.

        **不摘绑定**：绑定写在宿主类体上（见模块 docstring），是静态的，
        本来也摘不掉。摘掉 `_vars` 里的打开标记就够——宿主那几条回调读到
        「没有窗口」时会返回安全值，界面照常渲染，只是空着。
        """
        if self.closed:
            return
        self.closed = True
        if self.picker is not None:
            self.picker.close()
            self.picker = None
        if self.root is not None and self.screen._activated:
            self.root.Remove()
        if self.screen._vars.get(self.WINDOW_ATTRIBUTE) is self:
            del self.screen._vars[self.WINDOW_ATTRIBUTE]
        self.root = None
        self.selected_index = None
        self._icons.clear()
