# coding=utf-8
"""Offline conservation and edge-case checks for the simulated terminal slots."""

import json
import importlib.util
import os
import random
import sys
import unittest
import types
from collections import Counter
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from endernet_bp.endernet_scripts.endernet.client.ui.inventory_model import (  # noqa: E402
    InventoryModel, SLOT_COUNT, item_key,
)


class ItemStub(object):
    def __init__(self, name="stone", count=1, limit=64, **metadata):
        self.newItemName = "minecraft:" + name
        self.newAuxValue = 0
        self.count = count
        self.limit = limit
        for field in ("enchantData", "modEnchantData", "customTips", "extraId",
                      "userData", "durability"):
            setattr(self, field, metadata.get(field))

    def GetBasicInfo(self):
        return type("BasicInfo", (), {"maxStackSize": self.limit, "maxDurability": 0})()


def model_with(*entries):
    slots = [None] * SLOT_COUNT
    for index, item in entries:
        slots[index] = item
    return InventoryModel(slots)


def totals(model):
    result = Counter()
    for item in model.snapshot():
        if item is not None:
            result[item_key(item).canonical()] += item.count
    return result


class InventoryTests(unittest.TestCase):
    def test_move_between_boundary_slots_and_cancel(self):
        model = model_with((26, ItemStub(count=40)))
        model.click(26)
        self.assertEqual(model.get_item(26).count, 40)
        model.click(26)
        self.assertIsNone(model.selected)
        model.click(26)
        model.click(35)
        self.assertIsNone(model.get_item(26))
        self.assertEqual(model.get_item(35).count, 40)

    def test_partial_merge_retains_remainder_then_empty_target(self):
        model = model_with((0, ItemStub(count=40)), (27, ItemStub(count=50)))
        before = totals(model)
        model.click(0)
        model.click(27)
        self.assertEqual(model.get_item(27).count, 64)
        self.assertEqual(model.get_item(0).count, 26)
        self.assertEqual(model.selected, 0)
        self.assertEqual(model.click(27), ())
        model.click(35)
        self.assertEqual(model.get_item(35).count, 26)
        self.assertEqual(totals(model), before)

    def test_complete_merge_and_nonstandard_stack_limits(self):
        model = model_with((0, ItemStub("egg", 2, 16)), (1, ItemStub("egg", 14, 16)))
        model.click(0)
        model.click(1)
        self.assertIsNone(model.get_item(0))
        self.assertEqual(model.get_item(1).count, 16)
        self.assertIsNone(model.selected)

    def test_swap_and_special_metadata_never_merge(self):
        for metadata in ({"customTips": "named"}, {"extraId": "unique"},
                         {"userData": {"nested": [1, 2]}}, {"durability": 5},
                         {"enchantData": [(1, 2)]}):
            model = model_with((0, ItemStub(count=3, **metadata)), (27, ItemStub(count=5)))
            before = totals(model)
            model.click(0)
            model.click(27)
            self.assertEqual(model.get_item(0).count, 5)
            self.assertEqual(model.get_item(27).count, 3)
            self.assertEqual(totals(model), before)
        model = model_with((0, ItemStub("stone", 4)), (1, ItemStub("dirt", 6)))
        model.click(0)
        model.click(1)
        self.assertEqual(model.get_item(0).newItemName, "minecraft:dirt")

    def test_snapshot_ownership_and_atomic_replace(self):
        original = ItemStub(count=3, userData={"nested": [1]})
        model = model_with((0, original))
        original.userData["nested"].append(2)
        model.get_item(0).userData["nested"].append(3)
        self.assertEqual(model.get_item(0).userData, {"nested": [1]})
        before = totals(model)
        with self.assertRaises(ValueError):
            model.replace([ItemStub(count=65)] + [None] * 35)
        self.assertEqual(totals(model), before)
        with self.assertRaises(ValueError):
            model.replace([None] * 35)
        for index in (-1, 36, True, "0"):
            with self.assertRaises(ValueError):
                model.click(index)

    def test_random_moves_preserve_counts_and_limits(self):
        rng = random.Random(20260924)
        items = [ItemStub(rng.choice(("stone", "dirt", "apple")), rng.randint(1, 64))
                 if index % 3 else None for index in range(SLOT_COUNT)]
        model = InventoryModel(items)
        before = totals(model)
        for unused in range(1000):
            model.click(rng.randrange(SLOT_COUNT))
            self.assertEqual(totals(model), before)
            for item in model.snapshot():
                if item:
                    self.assertTrue(0 < item.count <= item.limit)
        model.cancel()
        self.assertEqual(totals(model), before)

    def test_json_grids_and_registration(self):
        ui = os.path.join(ROOT, "endernet_rp", "ui")
        with open(os.path.join(ui, "endernet_terminal_inventory.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        controls = dict(entry for control in data["inventory"]["controls"] for entry in control.items())
        for name, count, dimensions in (("backpack", 27, [9, 3]), ("hotbar", 9, [9, 1])):
            self.assertEqual(controls[name]["maximum_grid_items"], count)
            self.assertEqual(controls[name]["grid_dimensions"], dimensions)
            self.assertEqual(data[name + "_slot"]["bindings"][0]["binding_collection_name"],
                             controls[name]["collection_name"])
        with open(os.path.join(ui, "_ui_defs.json"), encoding="utf-8") as handle:
            self.assertIn("ui/endernet_terminal_inventory.json", json.load(handle)["ui_defs"])


class ControlStub(object):
    def __init__(self):
        self.children = {}
        self.visible = True
        self.text = "stale"
        self.icon_updates = 0

    def GetElement(self, path):
        return self.children.setdefault(path, ControlStub())

    def asItemRenderer(self):
        return self

    asLabel = asImage = asItemRenderer

    def SetVisible(self, visible):
        self.visible = visible

    def SetText(self, text):
        self.text = text

    def SetUiItem(self, item):
        self.icon_updates += 1
        return True


class ScreenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Load production screen with only its engine boundary replaced.
        # This validates Python routing/render calls, not native engine events.
        prefix = "endernet_bp.endernet_scripts."
        ui = types.ModuleType(prefix + "tooldelta.ui")
        item = types.ModuleType(prefix + "tooldelta.define.item")
        item.Item = ItemStub

        class ScreenStub(object):
            def __init__(self, name, instance, params):
                self._init_params = params or {}

        class BinderStub(object):
            BF_ButtonClickUp = 1

            @staticmethod
            def binding(flag, name):
                def decorate(func):
                    func.binding_name = name
                    return func
                return decorate

        ui.ToolDeltaScreen = ScreenStub
        ui.Binder = BinderStub
        ui.RegistToolDeltaScreen = lambda name: lambda screen: screen
        module_name = prefix + "endernet.client.ui.terminal_panel"
        path = os.path.join(ROOT, "endernet_bp", "endernet_scripts", "endernet", "client", "ui", "terminal_panel.py")
        spec = importlib.util.spec_from_file_location(module_name, path)
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {ui.__name__: ui, item.__name__: item}):
            spec.loader.exec_module(cls.module)

    def test_count_only_change_does_not_reset_icon_and_empty_clears(self):
        control = ControlStub()
        view = self.module.CustomSlotView(control)
        view.refresh(None, False)
        self.assertFalse(view.icon.visible)
        self.assertEqual(view.count.text, "")
        item = ItemStub(count=40)
        view.refresh(item, True)
        item.count = 26
        view.refresh(item, False)
        self.assertEqual(view.icon.icon_updates, 1)
        self.assertEqual(view.count.text, "26")
        self.assertFalse(view.selected.visible)
        view.refresh(None, False)
        self.assertFalse(view.icon.visible)
        self.assertEqual(view.count.text, "")

    def test_collection_mapping_inheritance_and_destroy_cancel(self):
        items = [None] * SLOT_COUNT
        items[26] = ItemStub(count=2)
        screen = self.module.CommonTerminalPanel("test", None, {"items": items})
        changes = []
        screen.OnInventoryChanged = changes.append
        screen.OnBackpackClick({"#collection_index": 26})
        screen.OnHotbarClick({"#collection_index": 9})  # Must not address slot 36.
        screen.OnHotbarClick({})
        self.assertEqual(screen.inventory.selected, 26)
        screen.OnHotbarClick({"#collection_index": 8})
        self.assertEqual(changes, [(26, 35)])
        self.assertEqual(screen.GetInventoryItems()[35].count, 2)
        screen.OnHotbarClick({"#collection_index": 8})
        screen.OnDestroy()
        self.assertIsNone(screen.inventory.selected)
        self.assertEqual(screen.GetInventoryItems()[35].count, 2)


if __name__ == "__main__":
    unittest.main()
