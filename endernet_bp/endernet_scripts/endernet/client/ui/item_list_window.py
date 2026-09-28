# coding=utf-8
"""Binder-driven editor for an existing list of item identifiers."""

from ....tooldelta.define.item import Item
from ....tooldelta.ui import Binder
from .select_item_window import (
    WINDOW_ATTRIBUTE as SELECT_WINDOW_ATTRIBUTE,
    show_select_item_window,
)

if 0 > 1:
    from ....tooldelta.ui import ToolDeltaScreen

WINDOW_ATTRIBUTE = "_endernet_item_list_window"
COLLECTION = "endernet_item_list"
BINDING_PREFIX = "#EnderNetItemList."

try:
    INTEGER_TYPES = (int, long)
except NameError:
    INTEGER_TYPES = (int,)


def show_item_list_window(screen, item_ids, max_items, title="物品列表"):
    # type: (ToolDeltaScreen, list[str], int, str) -> bool
    """Edit item_ids in place; refuse a second window on the same screen."""
    if WINDOW_ATTRIBUTE in screen._vars:
        return False
    if not isinstance(item_ids, list):
        raise TypeError("item_ids must be a list")
    if isinstance(max_items, bool) or not isinstance(max_items, INTEGER_TYPES) or max_items < 0:
        raise ValueError("max_items must be a non-negative integer")
    if len(item_ids) > max_items:
        raise ValueError("item_ids exceeds max_items")
    ItemListWindow(screen, item_ids, max_items, title)
    return True


class ItemListWindow(object):
    WINDOW_ATTRIBUTE = WINDOW_ATTRIBUTE
    SELECT_WINDOW_ATTRIBUTE = SELECT_WINDOW_ATTRIBUTE
    COLLECTION = COLLECTION
    BINDING_PREFIX = BINDING_PREFIX
    CONTROL_DEFINITION = "endernet_windows.item_list_window"
    CONTROL_NAME = "endernet_item_list_window"

    def __init__(self, screen, item_ids, max_items, title):
        self.screen = screen
        self.item_ids = item_ids
        self.max_items = max_items
        self.title = title
        self.selected_index = None
        self.closed = False
        self.root = None
        self.picker = None
        self._icons = {}
        self._bindings = []
        self._collection_flags = set()
        screen._vars[self.WINDOW_ATTRIBUTE] = self
        try:
            self._bind()
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

    def _bind(self):
        # Runtime bindings allow reuse on any registered ToolDeltaScreen.
        for flag, name, callback in (
            (Binder.BF_BindString, "title", self.get_title),
            (Binder.BF_BindInt, "count", self.get_count),
            (Binder.BF_BindBool, "can_add", self.can_add),
            (Binder.BF_BindBool, "can_delete", self.can_delete),
            (Binder.BF_ButtonClickUp, "select", self.select),
        ):
            name = self.BINDING_PREFIX + name
            self.screen.DynaBinding(flag, name, callback)
            self._bindings.append((flag, name))
        for flag, name, callback in (
            (Binder.BF_BindInt, "item_id_aux", self.get_item_id_aux),
            (Binder.BF_BindBool, "has_item", self.has_item),
            (Binder.BF_BindBool, "selected", self.is_selected),
        ):
            self.screen.DynaBindingCollection(flag, self.COLLECTION, self.BINDING_PREFIX + name, callback)
            self._collection_flags.add(flag)

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
        """Keep edits, close an owned picker, and release this window's bindings."""
        if self.closed:
            return
        self.closed = True
        if self.picker is not None:
            self.picker.close()
            self.picker = None
        for flag, name in self._bindings:
            self.screen.DynaUnbinding(flag, name)
        for flag in self._collection_flags:
            self.screen.DynaUnbindingCollection(flag, self.COLLECTION)
        if self.root is not None and self.screen._activated:
            self.root.Remove()
        if self.screen._vars.get(self.WINDOW_ATTRIBUTE) is self:
            del self.screen._vars[self.WINDOW_ATTRIBUTE]
        self.root = None
        self.selected_index = None
        self._icons.clear()
        self._bindings = []
        self._collection_flags.clear()
