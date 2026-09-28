# coding=utf-8
"""Fluid picker using the item picker's UI lifecycle and search behavior."""

from ....tooldelta.api.client.player import GetLocalPlayerHotbarAndInvItems
from .select_item_window import SelectItemWindow, _unique_ids

if 0 > 1:
    import typing

    from ....tooldelta.ui import ToolDeltaScreen

WINDOW_ATTRIBUTE = "_endernet_select_fluid_window"
SEARCH_BINDING = "#EnderNetSelectFluid.search"


def show_select_fluid_window(screen, network_fluid_ids, select_callback):
    # type: (ToolDeltaScreen, list[str], typing.Callable[[str], None]) -> bool
    """Open a fluid picker; return False if already open, otherwise True."""
    if WINDOW_ATTRIBUTE in screen._vars:
        return False
    SelectFluidWindow(screen, network_fluid_ids, select_callback)
    return True


class SelectFluidWindow(SelectItemWindow):
    WINDOW_ATTRIBUTE = WINDOW_ATTRIBUTE
    SEARCH_BINDING = SEARCH_BINDING
    CONTROL_DEFINITION = "endernet_windows.select_fluid_window"
    CONTROL_NAME = "endernet_select_fluid_window"
    GRID_PATH = "fluids/grid"

    def _source_ids(self):
        if self.source == "network":
            # Supplied IDs are already network fluid identifiers, not bucket IDs.
            return self.network_item_ids
        if self.catalog is None:
            return []
        if self.source == "backpack":
            buckets = self.catalog.bucket_fluid_ids
            return _unique_ids([
                buckets[item.newItemName]
                for item in GetLocalPlayerHotbarAndInvItems()
                if item is not None and item.count > 0 and item.newItemName in buckets
            ])
        return self.catalog.fluid_ids
