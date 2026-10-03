# coding=utf-8
"""Fluid identifier list editor sharing the item list's editing lifecycle."""

from .item_list_window import INTEGER_TYPES, ItemListWindow
from .select_fluid_window import (
    WINDOW_ATTRIBUTE as SELECT_WINDOW_ATTRIBUTE,
    show_select_fluid_window,
)
from .widgets.window_identity import (
    FLUID_LIST_BINDING_PREFIX as BINDING_PREFIX,
    FLUID_LIST_COLLECTION as COLLECTION,
    FLUID_LIST_WINDOW_ATTRIBUTE as WINDOW_ATTRIBUTE,
)

if 0 > 1:
    from ....tooldelta.ui import ToolDeltaScreen


def show_fluid_list_window(screen, fluid_ids, max_fluids, title="流体列表"):
    # type: (ToolDeltaScreen, list[str], int, str) -> bool
    """Edit fluid_ids in place; refuse a second fluid list on the same screen."""
    if WINDOW_ATTRIBUTE in screen._vars:
        return False
    if not isinstance(fluid_ids, list):
        raise TypeError("fluid_ids must be a list")
    if isinstance(max_fluids, bool) or not isinstance(max_fluids, INTEGER_TYPES) or max_fluids < 0:
        raise ValueError("max_fluids must be a non-negative integer")
    if len(fluid_ids) > max_fluids:
        raise ValueError("fluid_ids exceeds max_fluids")
    FluidListWindow(screen, fluid_ids, max_fluids, title)
    return True


class FluidListWindow(ItemListWindow):
    WINDOW_ATTRIBUTE = WINDOW_ATTRIBUTE
    SELECT_WINDOW_ATTRIBUTE = SELECT_WINDOW_ATTRIBUTE
    COLLECTION = COLLECTION
    BINDING_PREFIX = BINDING_PREFIX
    CONTROL_DEFINITION = "endernet_windows.fluid_list_window"
    CONTROL_NAME = "endernet_fluid_list_window"

    def _show_picker(self):
        # The picker returns fluid identifiers, including fluids mapped from buckets.
        return show_select_fluid_window(self.screen, [], self._on_item_selected)
