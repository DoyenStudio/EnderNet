# coding=utf-8
"""Offline selector interactions and catalogue request/response lifecycle."""

import importlib.util
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from endernet_bp.endernet_scripts.endernet.client.item_catalog import ItemCatalogClient  # noqa: E402
from endernet_bp.endernet_scripts.endernet.server.adapters.item_catalog import (  # noqa: E402
    ItemCatalogService, filter_item_ids,
)


class ItemStub(object):
    def __init__(self, item_id, count=1):
        self.newItemName = item_id
        self.count = count

    def GetBasicInfo(self):
        return types.SimpleNamespace(itemName="石头" if self.newItemName == "minecraft:stone"
                                     else self.newItemName, itemCategory=self.newItemName)


class Control(object):
    def __init__(self):
        self.children = {}
        self.visible = True
        self.removed = False

    def GetElement(self, path):
        return self.children.setdefault(path, Control())

    def asButton(self):
        return self

    asImage = asGrid = asItemRenderer = asButton

    def SetCallback(self, callback):
        self.callback = callback

    def SetSprite(self, sprite):
        self.sprite = sprite

    def SetUiItem(self, item):
        self.item = item.newItemName

    def SetVisible(self, visible):
        self.visible = visible

    def GetGridItem(self, x, y):
        return self.GetElement((x, y))

    def SetLayer(self, layer):
        self.layer = layer

    def Remove(self):
        self.removed = True


class Screen(object):
    def __init__(self):
        self.bindings = {}
        self._vars = {}
        self._activated = True

    def AddElement(self, definition, name):
        self.definition = definition
        self.root = Control()
        return self.root

    def DynaBinding(self, flag, name, callback):
        self.bindings[(flag, name)] = callback

    def DynaUnbinding(self, flag, name):
        self.bindings.pop((flag, name), None)


class Event(object):
    sent = []

    def send(self, player_id=None):
        self.sent.append((self.name, player_id, self.marshal()))


EVENTS = types.SimpleNamespace(CustomC2SEvent=Event, CustomS2CEvent=Event)


class Bus(object):
    def __init__(self):
        self.handles = []

    def ListenEvent(self, event, priority, static=False):
        def bind(callback):
            self.handles.append((event, callback, priority))
        return bind

    def _dyn_unlisten(self, event, callback, priority):
        self.handles.remove((event, callback, priority))


class SelectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        prefix = "endernet_bp.endernet_scripts."
        modules = {}
        for suffix, attrs in (
            ("tooldelta.api.client.player", {"GetLocalPlayerHotbarAndInvItems": lambda: []}),
            ("tooldelta.define.item", {"Item": ItemStub}),
            ("tooldelta.ui", {"Binder": types.SimpleNamespace(BF_EditChanged=1)}),
        ):
            module = types.ModuleType(prefix + suffix)
            module.__dict__.update(attrs)
            modules[module.__name__] = module
        path = os.path.join(ROOT, "endernet_bp", "endernet_scripts", "endernet",
                            "client", "ui", "select_item_window.py")
        spec = importlib.util.spec_from_file_location(
            prefix + "endernet.client.ui.select_item_window", path)
        cls.module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(cls.module)

    def setUp(self):
        self.catalog = ItemCatalogClient(Bus(), EVENTS, Event)
        self.catalog.item_ids = ["test:item_%02d" % i for i in range(55)] + ["minecraft:stone"]
        self.screen = Screen()
        self.selected = []
        self.patcher = patch.object(self.module, "get_catalog", return_value=self.catalog)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.assertIs(self.module.show_select_item_window(
            self.screen, ["minecraft:stone", "minecraft:stone"], self.selected.append), True)
        self.window = self.screen._vars[self.module.WINDOW_ATTRIBUTE]
        self.addCleanup(self.window.close)

    def search(self, text):
        self.screen.bindings[(1, self.module.SEARCH_BINDING)]({"Text": text})

    def test_default_and_search_entire_catalog_before_limit(self):
        self.assertEqual(self.screen.root.layer, 50)
        self.assertEqual(len(self.window.visible_ids), 40)
        self.search(" \t ")
        self.assertEqual(len(self.window.visible_ids), 40)
        self.search("  石头  ".encode("utf-8"))
        self.assertEqual(self.window.visible_ids, ["minecraft:stone"])
        self.search("ITEM_54")
        self.assertEqual(self.window.visible_ids, ["test:item_54"])
        self.search("absent")
        self.assertEqual(self.window.visible_ids, [])
        self.assertFalse(any(cell.visible for cell in self.window.cells))
        self.window._select(0)
        self.assertFalse(self.window.closed)

    def test_sources_dedup_and_exclusive_pressed_texture(self):
        self.window.root.GetElement("select_from_net_btn").callback({})
        self.assertEqual(self.window.visible_ids, ["minecraft:stone"])
        with patch.object(self.module, "GetLocalPlayerHotbarAndInvItems", return_value=[
            None, ItemStub("minecraft:air"), ItemStub("minecraft:dirt", 0),
            ItemStub("minecraft:stone", 3), ItemStub("minecraft:stone", 5),
        ]):
            self.window.root.GetElement("select_from_backpack_btn").callback({})
        self.assertEqual(self.window.visible_ids, ["minecraft:stone"])
        for source, path in self.module.SOURCE_BUTTONS:
            expected = (self.module.PRESSED_TEXTURE if source == "backpack"
                        else self.module.DEFAULT_TEXTURE)
            self.assertEqual(self.window.root.GetElement(path).GetElement("default").sprite, expected)

    def test_close_before_callback_exactly_once_and_reopen(self):
        old_root = self.window.root
        click = self.window.cells[0].GetElement("interact_btn").callback

        def callback(item_id):
            self.assertTrue(old_root.removed)
            self.assertFalse(self.screen.bindings)
            self.assertNotIn(self.module.WINDOW_ATTRIBUTE, self.screen._vars)
            self.selected.append(item_id)
            self.assertIs(self.module.show_select_item_window(self.screen, [], lambda _: None), True)
            self.reopened = self.screen._vars[self.module.WINDOW_ATTRIBUTE]

        self.window.select_callback = callback
        click({})
        click({})
        self.assertEqual(self.selected, ["test:item_00"])
        self.assertIs(self.screen._vars[self.module.WINDOW_ATTRIBUTE], self.reopened)
        self.reopened.root.GetElement("close").callback({})
        self.assertNotIn(self.module.WINDOW_ATTRIBUTE, self.screen._vars)
        self.assertFalse(self.screen.bindings)
        self.assertEqual(len(self.catalog.windows), 0)

    def test_duplicate_open_preserves_window_and_callback(self):
        old_root = self.window.root
        old_callback = self.window.select_callback
        self.search("ITEM_54")
        with patch.object(self.screen, "AddElement") as create:
            self.assertIs(self.module.show_select_item_window(self.screen, [], lambda _: None), False)
            create.assert_not_called()
        self.assertIs(self.screen._vars[self.module.WINDOW_ATTRIBUTE], self.window)
        self.assertIs(self.window.select_callback, old_callback)
        self.assertFalse(old_root.removed)
        self.assertEqual(self.window.visible_ids, ["test:item_54"])
        self.window._select(0)
        self.assertEqual(self.selected, ["test:item_54"])

    def test_creation_marks_before_controls_and_failure_allows_retry(self):
        self.window.close()

        def fail_creation(*args):
            self.assertIn(self.module.WINDOW_ATTRIBUTE, self.screen._vars)
            self.assertIs(self.module.show_select_item_window(self.screen, [], self.selected.append), False)
            raise RuntimeError("creation failed")

        with patch.object(self.screen, "AddElement", side_effect=fail_creation):
            with self.assertRaises(RuntimeError):
                self.module.show_select_item_window(self.screen, [], self.selected.append)
        self.assertNotIn(self.module.WINDOW_ATTRIBUTE, self.screen._vars)
        self.assertIs(self.module.show_select_item_window(self.screen, [], self.selected.append), True)
        self.screen._vars[self.module.WINDOW_ATTRIBUTE].close()

    def test_closed_screen_cleanup_removes_only_picker_marker(self):
        self.screen._vars["unrelated"] = True
        self.screen._activated = False
        self.window.close()
        self.window.close()
        self.assertEqual(self.screen._vars, {"unrelated": True})
        self.assertFalse(self.screen.bindings)

    def test_reopen_and_late_catalog_response(self):
        self.window.close()
        self.catalog.item_ids = []
        self.assertIs(self.module.show_select_item_window(self.screen, [], self.selected.append), True)
        replacement = self.screen._vars[self.module.WINDOW_ATTRIBUTE]
        self.addCleanup(replacement.close)
        self.assertTrue(self.window.closed)
        self.assertEqual(replacement.visible_ids, [])
        self.catalog.receive(self.catalog.response(["minecraft:stone"]))
        self.assertEqual(replacement.visible_ids, ["minecraft:stone"])
        replacement.close()
        self.catalog.receive(self.catalog.response(["minecraft:dirt"]))
        self.assertEqual(self.selected, [])

    def test_json_contract(self):
        with open(os.path.join(ROOT, "endernet_rp", "ui", "endernet_windows.json"),
                  encoding="utf-8") as handle:
            data = json.load(handle)
        controls = {key.split("@")[0]: value for entry in data["select_item_window"]["controls"]
                    for key, value in entry.items()}
        grid = controls["items"]["controls"][0]["grid"]
        self.assertEqual(grid["grid_dimensions"], [8, 5])
        self.assertEqual(grid["maximum_grid_items"], 40)
        self.assertEqual(controls["select_from_net_btn"]["$label_text"], "网络")
        self.assertEqual(controls["item_search_editor"]["$text_box_name"], self.module.SEARCH_BINDING)


class CatalogTests(unittest.TestCase):
    def test_categories_roundtrip_once_and_cleanup(self):
        Event.sent = []
        client_bus, server_bus = Bus(), Bus()
        loads = []

        def load():
            loads.append(True)
            return {"item_ids": filter_item_ids(
                ["", "command", "none", "items", "items", "construction"], ItemStub),
                "fluid_ids": ["minecraft:water"],
                "bucket_fluid_ids": {"minecraft:water_bucket": "minecraft:water"}}

        client = ItemCatalogClient(client_bus, EVENTS, Event)
        server = ItemCatalogService(server_bus, EVENTS, load)
        client.start()
        server.start()
        client.on_ready(None)
        client.on_ready(None)
        self.assertEqual(len(Event.sent), 1)
        request_payload = dict(Event.sent[0][2], __id__="player1", player_id="spoof")
        request = server.request.unmarshal(request_payload)
        server.receive(request)
        self.assertEqual(Event.sent[-1][1], "player1")
        self.assertEqual(Event.sent[-1][0], client.response.name)
        client.receive(client.response.unmarshal(Event.sent[-1][2]))
        self.assertEqual(client.item_ids, ["construction", "items"])
        self.assertEqual(client.fluid_ids, ["minecraft:water"])
        self.assertEqual(client.bucket_fluid_ids, {"minecraft:water_bucket": "minecraft:water"})
        server.receive(request)
        self.assertEqual(loads, [True])
        client.stop()
        server.stop()
        self.assertEqual(client_bus.handles + server_bus.handles, [])
        self.assertEqual(client.item_ids, [])
        self.assertEqual(client.fluid_ids, [])
        self.assertEqual(client.bucket_fluid_ids, {})


if __name__ == "__main__":
    unittest.main()
