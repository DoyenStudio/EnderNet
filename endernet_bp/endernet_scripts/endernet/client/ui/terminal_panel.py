# coding=utf-8
"""Reusable terminal screen and common-terminal registration (simulation only)."""

from ....tooldelta.define.item import Item
from ....tooldelta.ui import Binder, RegistToolDeltaScreen, ToolDeltaScreen
from .inventory_model import (
    BACKPACK_COUNT, HOTBAR_COUNT, HOTBAR_START, INTEGER_TYPES,
    InventoryModel, SLOT_COUNT, item_key,
)


class CustomSlotView(object):
    """Cache child controls and update only changed icon/count/selection state."""

    def __init__(self, control):
        self.icon = control.GetElement("item_renderer").asItemRenderer()
        self.count = control.GetElement("stack_count_label").asLabel()
        self.selected = control.GetElement("item_selected_image")
        self.bar = control.GetElement("durability_bar")
        self.fill = control.GetElement("durability_bar/fill").asImage()
        control.GetElement("item_lock").SetVisible(False)
        self._key = None
        self._text = None
        self._selected = None
        self.icon.SetVisible(False)
        self.bar.SetVisible(False)

    def refresh(self, item, selected):
        key = item_key(item) if item is not None else None
        if key != self._key:
            self.icon.SetVisible(False)
            self.bar.SetVisible(False)
            if item is not None:
                self.icon.SetVisible(bool(self.icon.SetUiItem(item)))
                maximum = item.GetBasicInfo().maxDurability
                if maximum and item.durability is not None:
                    ratio = max(0.0, min(1.0, float(item.durability) / float(maximum)))
                    self.fill.SetSpriteClipRatio("fromRightToLeft", 1.0 - ratio)
                    self.fill.SetSpriteColor((min(1.0, 2.0 * (1.0 - ratio)),
                                              min(1.0, 2.0 * ratio), 0.0))
                    self.bar.SetVisible(ratio < 1.0)
            self._key = key
        text = str(item.count) if item is not None and item.count > 1 else ""
        if text != self._text:
            self.count.SetText(text)
            self._text = text
        if selected != self._selected:
            self.selected.SetVisible(selected)
            self._selected = selected


class TerminalPanelBase(ToolDeltaScreen):
    """Other terminal screens may inherit this class and reuse terminal_inventory.

    Slots 0..26 are a 9x3 backpack, 27..35 a 9x1 hotbar. These are
    simulated UI IDs, NOT engine inventory indices. Click twice to transfer.
    """

    INVENTORY_PATH = "/terminal/inventory"

    def __init__(self, screen_name, screen_instance, params=None):
        ToolDeltaScreen.__init__(self, screen_name, screen_instance, params)
        self.inventory = InventoryModel(self._init_params.get("items"))
        self._views = {}

    def OnCreate(self):
        for grid_name, start, count, columns in (
            ("backpack", 0, BACKPACK_COUNT, 9),
            ("hotbar", HOTBAR_START, HOTBAR_COUNT, 9),
        ):
            grid = self.GetElement(self.INVENTORY_PATH + "/" + grid_name).asGrid()
            for local_index in range(count):
                cell = grid.GetGridItem(local_index % columns, local_index // columns)
                self._views[start + local_index] = CustomSlotView(cell.GetElement("visual"))
        self._refresh(range(SLOT_COUNT))

    def _refresh(self, indices):
        for index in indices:
            view = self._views.get(index)
            if view is not None:
                view.refresh(self.inventory.get_item(index), self.inventory.selected == index)

    def SetInventoryItems(self, items):
        """Replace all 36 simulated items atomically; safe before/after Create."""
        self.inventory.replace(items)
        self._refresh(range(SLOT_COUNT))

    def GetInventoryItems(self):
        return self.inventory.snapshot()

    def OnInventoryChanged(self, indices):
        """Optional subclass hook. Never use this local state as server authority."""
        pass

    def _click(self, params, start, count):
        # Do not reinterpret an absent or out-of-range index as slot zero.
        index = params.get("#collection_index")
        if (isinstance(index, bool) or not isinstance(index, INTEGER_TYPES)
                or not 0 <= index < count):
            return
        changed = self.inventory.click(start + index)
        self._refresh(changed)
        if len(changed) == 2:
            self.OnInventoryChanged(changed)

    @Binder.binding(Binder.BF_ButtonClickUp, "#EnderNetTerminal.backpack_click")
    def OnBackpackClick(self, params):
        self._click(params, 0, BACKPACK_COUNT)

    @Binder.binding(Binder.BF_ButtonClickUp, "#EnderNetTerminal.hotbar_click")
    def OnHotbarClick(self, params):
        self._click(params, HOTBAR_START, HOTBAR_COUNT)

    @Binder.binding(Binder.BF_ButtonClickUp, "#EnderNetTerminal.close")
    def OnCloseClick(self, params):
        self.inventory.cancel()
        self.RemoveUI()

    def OnDeactive(self):
        self._refresh(self.inventory.cancel())

    def OnDestroy(self):
        self.inventory.cancel()
        self._views.clear()


@RegistToolDeltaScreen("common_terminal_panel_ui.main")
class CommonTerminalPanel(TerminalPanelBase):
    """Common terminal. Network inventory is intentionally not connected yet."""

    @classmethod
    def OpenDemo(cls):
        """Client-side manual check, after UiInitFinishedEvent has registered UI."""
        items = [None] * SLOT_COUNT
        items[0] = Item("minecraft:stone", count=40)
        items[1] = Item("minecraft:stone", count=50)
        items[2] = Item("minecraft:dirt", count=16)
        items[26] = Item("minecraft:apple", count=3)
        items[27] = Item("minecraft:stone", count=10)
        items[35] = Item("minecraft:stick", count=8)
        return cls.CreateUI({"items": items})
