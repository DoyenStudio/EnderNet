# coding=utf-8
"""Fluid naming rules, catalogue transport and picker interactions offline."""

import importlib.util
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

from test_select_item_window import (
    ROOT,
    BinderStub,
    Bus,
    EVENTS,
    Event,
    Screen,
)
from endernet_bp.endernet_scripts.endernet.common.fluid_catalog import build_fluid_catalog
from endernet_bp.endernet_scripts.endernet.client.item_catalog import ItemCatalogClient
from endernet_bp.endernet_scripts.endernet.server.adapters.item_catalog import load_catalog


class FluidItem(object):
    def __init__(self, item_id, count=1):
        self.newItemName = item_id
        #: 真 `Item` 有的第二个字段（渲染要用，见 `widgets/control_api.py`）。
        self.newAuxValue = 0
        self.count = count

    def GetBasicInfo(self):
        return types.SimpleNamespace(
            itemName="水" if self.newItemName == "minecraft:water" else self.newItemName,
            itemCategory="none" if "water" in self.newItemName else "items", id_aux=7654)


class FluidRulesTests(unittest.TestCase):
    def test_prefix_and_suffix_only_affect_namespaced_item_name(self):
        registered = [
            "minecraft:flowing_water", "minecraft:flowing_water", "mod:flowing_oil",
            "mod:flowing_missing", "mod:not_flowing_water", "flowing_mod:water",
            "mod:flowing_", "flowing_water", "minecraft:water_bucket", "mod:milk_bucket",
            "mod:missing_bucket", "mod:bucket", "mod:water_bucket_extra", "mod:_bucket",
            "_bucket:stone", "minecraft:stone",
        ]
        exists = {"minecraft:water", "mod:oil", "mod:milk", "flowing_mod:water",
                  "mod:not_water", "mod:water", "mod:bucket", "_bucket:stone"}
        fluids, buckets = build_fluid_catalog(registered, exists.__contains__)
        self.assertEqual(fluids, ["minecraft:water", "mod:oil"])
        self.assertEqual(buckets, {"minecraft:water_bucket": "minecraft:water",
                                   "mod:milk_bucket": "mod:milk"})
        # Candidate existence is authoritative even when it is absent from registered.
        self.assertNotIn("mod:oil", registered)

    def test_remove_only_one_prefix_or_suffix(self):
        fluids, buckets = build_fluid_catalog(
            ["mod:flowing_flowing_oil", "mod:oil_bucket_bucket"],
            {"mod:flowing_oil", "mod:oil_bucket"}.__contains__)
        self.assertEqual(fluids, ["mod:flowing_oil"])
        self.assertEqual(buckets, {"mod:oil_bucket_bucket": "mod:oil_bucket"})

    def test_server_scans_raw_registry_before_creative_category_filter(self):
        prefix = "endernet_bp.endernet_scripts."
        engine = types.ModuleType("mod.server.extraServerApi")
        calls = []

        def registered(all_items):
            calls.append(all_items)
            return ["minecraft:flowing_water", "minecraft:water_bucket", "minecraft:stone"]

        engine.GetEngineCompFactory = lambda: types.SimpleNamespace(
            CreateItem=lambda level: types.SimpleNamespace(GetLoadItems=registered))
        engine.GetLevelId = lambda: "level"
        item_api = types.ModuleType(prefix + "tooldelta.api.server.item")
        checked = []

        def exists(item_id):
            checked.append(item_id)
            return item_id == "minecraft:water"

        item_api.ItemExists = exists
        item_module = types.ModuleType(prefix + "tooldelta.define.item")
        item_module.Item = FluidItem
        with patch.dict(sys.modules, {engine.__name__: engine, item_api.__name__: item_api,
                                      item_module.__name__: item_module}):
            catalog = load_catalog()
        self.assertEqual(calls, [True])
        self.assertEqual(catalog["item_ids"], ["minecraft:stone"])
        self.assertEqual(catalog["fluid_ids"], ["minecraft:water"])
        self.assertEqual(catalog["bucket_fluid_ids"], {"minecraft:water_bucket": "minecraft:water"})
        self.assertEqual(checked, ["minecraft:water", "minecraft:water"])


class FluidWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        prefix = "endernet_bp.endernet_scripts."
        modules = {}
        for suffix, attrs in (
            ("tooldelta.api.client.player", {"GetLocalPlayerHotbarAndInvItems": lambda: []}),
            ("tooldelta.define.item", {"Item": FluidItem}),
            ("tooldelta.ui", {"Binder": BinderStub}),
        ):
            module = types.ModuleType(prefix + suffix)
            module.__dict__.update(attrs)
            modules[module.__name__] = module

        def load(name):
            fullname = prefix + "endernet.client.ui." + name.replace("/", ".")
            path = os.path.join(ROOT, "endernet_bp", "endernet_scripts", "endernet", "client",
                                "ui", name + ".py")
            spec = importlib.util.spec_from_file_location(fullname, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        with patch.dict(sys.modules, modules):
            cls.items = load("select_item_window")
            with patch.dict(sys.modules, {cls.items.__name__: cls.items}):
                cls.fluids = load("select_fluid_window")
            cls.bindings_module = load("widgets/list_window_bindings")
        # 同一块屏上会同时开物品选择器与流体选择器，因此宿主两条混入都要有——
        # 这正是「两条绑定各认各的 `_vars` 槽」那条判据的现场。
        cls.Host = type(
            "HostScreen",
            (cls.bindings_module.SelectItemWindowBindingsMixin,
             cls.bindings_module.SelectFluidWindowBindingsMixin,
             Screen),
            {},
        )

    def setUp(self):
        self.screen = type(self).Host()
        self.catalog = ItemCatalogClient(Bus(), EVENTS, Event)
        self.catalog.item_ids = ["minecraft:stone"]
        self.catalog.fluid_ids = ["minecraft:water", "mod:oil"]
        self.catalog.bucket_fluid_ids = {"minecraft:water_bucket": "minecraft:water",
                                        "mod:milk_bucket": "mod:milk"}
        self.received = []
        patcher = patch.object(self.items, "get_catalog", return_value=self.catalog)
        patcher.start()
        self.addCleanup(patcher.stop)

    def open(self, network_ids=None):
        self.assertIs(self.fluids.show_select_fluid_window(
            self.screen, network_ids or [], self.received.append), True)
        window = self.screen._vars[self.fluids.WINDOW_ATTRIBUTE]
        self.addCleanup(window.close)
        return window

    def test_all_network_and_backpack_use_canonical_fluid_ids(self):
        window = self.open(["mod:oil", "mod:oil", "minecraft:water"])
        self.assertEqual(self.screen.definition, "endernet_windows.select_fluid_window")
        self.assertEqual(window.root.layer, 50)
        self.assertEqual(window.COLLECTION, "endernet_select_fluids")
        self.assertEqual(window.visible_ids, ["minecraft:water", "mod:oil"])
        self.assertEqual(window.get_item_id_aux(0), 7654)
        window._set_source("network")
        self.assertEqual(window.visible_ids, ["mod:oil", "minecraft:water"])
        inventory = [None, FluidItem("minecraft:stone"), FluidItem("minecraft:water_bucket", 0),
                     FluidItem("mod:milk_bucket"), FluidItem("minecraft:water_bucket", 3),
                     FluidItem("minecraft:water_bucket"), FluidItem("mod:missing_bucket")]
        with patch.object(self.fluids, "GetLocalPlayerHotbarAndInvItems", return_value=inventory):
            window._set_source("backpack")
        self.assertEqual(window.visible_ids, ["mod:milk", "minecraft:water"])
        for source, path in self.items.SOURCE_BUTTONS:
            texture = self.items.PRESSED_TEXTURE if source == "backpack" else self.items.DEFAULT_TEXTURE
            self.assertEqual(window.root.GetElement(path).GetElement("default").sprite, texture)
        window._select(0)
        self.assertEqual(self.received, ["mod:milk"])

    def test_search_and_limit_apply_after_fluid_identification(self):
        self.catalog.fluid_ids = ["mod:fluid_%02d" % i for i in range(51)] + ["minecraft:water"]
        window = self.open()
        search = self.screen.callback(self.fluids.SEARCH_BINDING)
        search({"Text": " \t "})
        self.assertEqual(len(window.visible_ids), 40)
        search({"Text": " FLUID_50 "})
        self.assertEqual(window.visible_ids, ["mod:fluid_50"])
        search({"Text": " 水 ".encode("utf-8")})
        self.assertEqual(window.visible_ids, ["minecraft:water"])
        search({"Text": "missing"})
        self.assertFalse(any(window.has_item(i) for i in range(40)))
        window._select(0)
        self.assertEqual(self.received, [])

    def test_duplicate_marker_close_before_callback_and_binding_isolation(self):
        self.items.show_select_item_window(self.screen, [], lambda _: None)
        item_window = self.screen._vars[self.items.WINDOW_ATTRIBUTE]
        self.addCleanup(item_window.close)
        window = self.open()
        self.assertIs(self.fluids.show_select_fluid_window(self.screen, [], lambda _: None), False)
        root = window.root
        engine = self.screen._screen_instance
        self.assertEqual(len(engine.collections), 4)
        fluid_click = self.screen.callback(window.BINDING_PREFIX + "select")

        def selected(fluid_id):
            self.assertTrue(root.removed)
            self.assertNotIn(self.fluids.WINDOW_ATTRIBUTE, self.screen._vars)
            # **两条绑定同时活在宿主类体上，各认各的 `_vars` 槽**（11-07）。
            # 关掉流体窗既不摘掉自己那条（静态绑定摘不掉），也不碰物品窗
            # 那条：前者已关所以忽略编辑，后者照常收到。
            self.screen.callback(self.fluids.SEARCH_BINDING)({"Text": "missing"})
            self.assertEqual(window.query, "")
            self.screen.callback(self.items.SEARCH_BINDING)({"Text": "minecraft:stone"})
            self.assertEqual(item_window.query, "minecraft:stone")
            self.received.append(fluid_id)

        window.select_callback = selected
        fluid_click({"#collection_index": 0})
        fluid_click({"#collection_index": 0})
        self.assertEqual(self.received, ["minecraft:water"])
        self.assertIs(self.screen._vars[self.items.WINDOW_ATTRIBUTE], item_window)
        self.assertEqual(len(engine.collections), 2)
        self.assertTrue(all(key[1] == item_window.COLLECTION for key in engine.collections))
        self.assertTrue(callable(self.screen.callback(item_window.BINDING_PREFIX + "select")))
        self.open().close()

    def test_late_catalog_refreshes_backpack_and_stop_cleans_everything(self):
        self.catalog.start()
        self.catalog.fluid_ids = []
        self.catalog.bucket_fluid_ids = {}
        window = self.open()
        self.assertEqual(window.visible_ids, [])
        with patch.object(self.fluids, "GetLocalPlayerHotbarAndInvItems",
                          return_value=[FluidItem("minecraft:water_bucket")]):
            window._set_source("backpack")
            payload = self.catalog.response([], ["minecraft:water"],
                                            {"minecraft:water_bucket": "minecraft:water"}).marshal()
            self.catalog.receive(self.catalog.response.unmarshal(payload))
        self.assertEqual(window.visible_ids, ["minecraft:water"])
        self.catalog.stop()
        self.assertTrue(window.closed)
        self.assertEqual(self.screen._vars, {})
        self.assertEqual(self.catalog.fluid_ids, [])
        self.assertEqual(self.catalog.bucket_fluid_ids, {})
        self.assertEqual(self.catalog.bus.handles, [])

    def test_create_failure_removes_fluid_marker(self):
        with patch.object(self.screen, "AddElement", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                self.fluids.show_select_fluid_window(self.screen, [], self.received.append)
        self.assertEqual(self.screen._vars, {})
        self.open()

    def test_json_fluid_bindings_are_separate_from_item_picker(self):
        with open(os.path.join(ROOT, "endernet_rp", "ui", "endernet_windows.json"),
                  encoding="utf-8") as handle:
            data = json.load(handle)
        controls = {key.split("@")[0]: value for entry in data["select_fluid_window"]["controls"]
                    for key, value in entry.items()}
        self.assertEqual(controls["fluid_search_editor"]["$text_box_name"], self.fluids.SEARCH_BINDING)
        grid = controls["fluids"]["controls"][0]["grid"]
        self.assertEqual(grid["collection_name"], "endernet_select_fluids")
        self.assertEqual(grid["grid_item_template"], "endernet_windows.selected_fluid")
        template = data["selected_fluid@selected_item"]
        self.assertEqual(template["$selector_collection"], "endernet_select_fluids")
        self.assertEqual(template["$selector_select"], "#EnderNetSelectFluid.select")
        self.assertEqual(grid["grid_dimensions"], [8, 5])
        self.assertEqual(grid["maximum_grid_items"], 40)


if __name__ == "__main__":
    unittest.main()
