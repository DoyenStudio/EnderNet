# coding=utf-8
"""物品选择器：宿主静态搜索绑定，网格按窗口生命周期动态绑定。

网格通过 DynaBindingCollection 提供可见性与 id_aux，点击通过集合下标选取。
窗口打开时注册、关闭时解除绑定，不再查找网格格子或渲染器控件。
"""

from functools import partial

from ....tooldelta.api.client.player import GetLocalPlayerHotbarAndInvItems
from ....tooldelta.define.item import Item
from ....tooldelta.ui import Binder
from ..item_catalog import get_catalog
from .widgets.window_identity import (
    SELECT_ITEM_SEARCH_BINDING as SEARCH_BINDING,
    SELECT_ITEM_WINDOW_ATTRIBUTE as WINDOW_ATTRIBUTE,
)

if 0 > 1:
    import typing

    from ....tooldelta.ui import ToolDeltaScreen

COLLECTION = "endernet_select_items"
BINDING_PREFIX = "#EnderNetSelectItem."
MAX_ITEMS = 40
DEFAULT_TEXTURE = "textures/ui/endernet/background_panel_opaque"
PRESSED_TEXTURE = "textures/ui/endernet/button_pressed"
HOVER_TEXTURE = "textures/ui/endernet/button_hovered"
SOURCE_BUTTONS = (
    ("backpack", "select_from_backpack_btn"),
    ("network", "select_from_net_btn"),
    ("all", "select_from_all_btn"),
)

try:
    TEXT_TYPE = unicode
except NameError:
    TEXT_TYPE = str


def _text(value):
    if isinstance(value, TEXT_TYPE):
        return value
    return value.decode("utf-8")


def _unique_ids(item_ids):
    seen = set()
    result = []
    for item_id in item_ids:
        if item_id and item_id != "minecraft:air" and item_id not in seen:
            seen.add(item_id)
            result.append(item_id)
    return result


def show_select_item_window(screen, network_item_ids, select_callback):
    # type: (ToolDeltaScreen, list[str], typing.Callable[[str], None]) -> bool
    """Return False if a picker is already open, True after creating one."""
    if WINDOW_ATTRIBUTE in screen._vars:
        return False
    SelectItemWindow(screen, network_item_ids, select_callback)
    return True


class SelectItemWindow(object):
    WINDOW_ATTRIBUTE = WINDOW_ATTRIBUTE
    SEARCH_BINDING = SEARCH_BINDING
    COLLECTION = COLLECTION
    BINDING_PREFIX = BINDING_PREFIX
    CONTROL_DEFINITION = "endernet_windows.select_item_window"
    CONTROL_NAME = "endernet_select_item_window"

    def __init__(self, screen, network_item_ids, select_callback):
        self.screen = screen
        self.select_callback = select_callback
        self.network_item_ids = _unique_ids(list(network_item_ids))
        self.catalog = get_catalog()
        self.source = "all"
        self.query = ""
        self.visible_ids = []
        self.search_names = {}
        self.closed = False
        self.root = None
        self.dynamic_bindings = []
        # The instance is both the open marker and the optional cleanup handle.
        screen._vars[self.WINDOW_ATTRIBUTE] = self
        try:
            self._create_controls()
        except Exception:
            self.close()
            raise

    def _create_controls(self):
        screen = self.screen
        self._bind_grid()
        self.root = screen.AddElement(
            self.CONTROL_DEFINITION, self.CONTROL_NAME
        )
        self.root.SetLayer(50)
        for source, path in SOURCE_BUTTONS:
            self.root.GetElement(path).asButton().SetCallback(partial(self._set_source, source))
        self.root.GetElement("close").asButton().SetCallback(self.close)
        if self.catalog is not None:
            self.catalog.windows.add(self)
        self._set_source("all")

    def _bind_grid(self):
        self._bind_dynamic(Binder.BF_BindBool, "has_item", self.has_item, self.COLLECTION)
        self._bind_dynamic(Binder.BF_BindInt, "item_id_aux", self.get_item_id_aux, self.COLLECTION)
        self._bind_dynamic(Binder.BF_ButtonClickUp, "select", self.on_select)

    def _bind_dynamic(self, flag, suffix, callback, collection=None):
        # type: (int, str, object, object) -> None
        binding_name = self.BINDING_PREFIX + suffix
        # 先记录，注册中途失败时 close 也能撤销。
        self.dynamic_bindings.append((flag, collection, binding_name))
        if collection is None:
            self.screen.DynaBinding(flag, binding_name, callback)
        else:
            self.screen.DynaBindingCollection(flag, collection, binding_name, callback)

    def has_item(self, index):
        # type: (int) -> bool
        return not self.closed and 0 <= index < len(self.visible_ids)

    def get_item_id_aux(self, index):
        # type: (int) -> int
        if not self.has_item(index):
            return 131072
        return Item(self.visible_ids[index]).GetBasicInfo().id_aux

    def on_select(self, params):
        # type: (dict) -> None
        index = params.get("#collection_index")
        if isinstance(index, int) and not isinstance(index, bool):
            self._select(index)

    def _set_source(self, source, params=None):
        if self.closed:
            return
        self.source = source
        for button_source, path in SOURCE_BUTTONS:
            button = self.root.GetElement(path)
            selected = button_source == source
            button.GetElement("default").asImage().SetSprite(
                PRESSED_TEXTURE if selected else DEFAULT_TEXTURE
            )
            button.GetElement("hover").asImage().SetSprite(
                PRESSED_TEXTURE if selected else HOVER_TEXTURE
            )
        self.refresh()

    def on_search(self, params):
        # type: (dict) -> None
        """搜索框变了（由宿主屏的类体绑定转发，见模块 docstring）。"""
        if self.closed:
            return
        self.query = _text(params.get("Text") or "").strip().lower()
        self.refresh()

    def _source_ids(self):
        if self.source == "backpack":
            return _unique_ids([
                item.newItemName for item in GetLocalPlayerHotbarAndInvItems()
                if item is not None and item.count > 0
            ])
        if self.source == "network":
            return self.network_item_ids
        return self.catalog.item_ids if self.catalog is not None else []

    def refresh(self):
        """Filter the full source before taking its first 40 matches."""
        if self.closed or not self.screen._activated:
            return
        visible_ids = []
        for item_id in self._source_ids():
            if self.query:
                if item_id not in self.search_names:
                    self.search_names[item_id] = _text(Item(item_id).GetBasicInfo().itemName).lower()
                if (self.query not in self.search_names[item_id]
                        and self.query not in _text(item_id).lower()):
                    continue
            visible_ids.append(item_id)
            if len(visible_ids) == MAX_ITEMS:
                break
        self.visible_ids = visible_ids

    def _select(self, index, params=None):
        if self.closed or not 0 <= index < len(self.visible_ids):
            return
        item_id = self.visible_ids[index]
        callback = self.select_callback
        self.close()
        callback(item_id)

    def close(self, params=None):
        """Cancel without selecting; also safe to call from the host's OnDestroy."""
        if self.closed:
            return
        self.closed = True
        if self.catalog is not None:
            self.catalog.windows.discard(self)
        for flag, collection, binding_name in reversed(self.dynamic_bindings):
            if collection is None:
                self.screen.DynaUnbinding(flag, binding_name)
            else:
                self.screen.DynaUnbindingCollection(flag, collection)
        self.dynamic_bindings = []
        if self.screen._activated and self.root is not None:
            self.root.Remove()
        if self.screen._vars.get(self.WINDOW_ATTRIBUTE) is self:
            del self.screen._vars[self.WINDOW_ATTRIBUTE]
        self.select_callback = None
        self.visible_ids = []
        self.search_names.clear()
        self.root = None
