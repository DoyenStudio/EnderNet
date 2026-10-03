# coding=utf-8
"""Exercise the list's Binder callbacks and its actual nested selector offline."""

import importlib.util
import json
import os
import sys
import types
import unittest
from unittest.mock import patch
from weakref import WeakSet

from test_select_item_window import BinderStub, ROOT, Screen


class ItemStub(object):
    def __init__(self, item_id):
        self.item_id = item_id
        self.newItemName = item_id
        #: 真 `Item` 有的第二个字段（渲染要用，见 `widgets/control_api.py`）。
        self.newAuxValue = 0

    def GetBasicInfo(self):
        return types.SimpleNamespace(itemName=self.item_id, id_aux=12345)


class FluidListWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        prefix = "endernet_bp.endernet_scripts."
        modules = {}
        for suffix, attrs in (
            ("tooldelta.ui", {"Binder": BinderStub}),
            ("tooldelta.define.item", {"Item": ItemStub}),
            ("tooldelta.api.client.player", {"GetLocalPlayerHotbarAndInvItems": lambda: []}),
        ):
            module = types.ModuleType(prefix + suffix)
            module.__dict__.update(attrs)
            modules[module.__name__] = module

        def load(name):
            full_name = prefix + "endernet.client.ui." + name.replace("/", ".")
            path = os.path.join(ROOT, "endernet_bp", "endernet_scripts", "endernet",
                                "client", "ui", name + ".py")
            spec = importlib.util.spec_from_file_location(full_name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        with patch.dict(sys.modules, modules):
            cls.item_selector = load("select_item_window")
            with patch.dict(sys.modules, {cls.item_selector.__name__: cls.item_selector}):
                cls.item_list = load("item_list_window")
                cls.selector = load("select_fluid_window")
                with patch.dict(sys.modules, {
                    cls.item_list.__name__: cls.item_list,
                    cls.selector.__name__: cls.selector,
                }):
                    cls.module = load("fluid_list_window")
            cls.bindings_module = load("widgets/list_window_bindings")
        # 有一组用例在**同一块屏**上同时开物品名单与流体名单（两份混入都要有，
        # 否则那组用例里的物品那一半在真机上根本解析不到绑定）。
        cls.Host = type(
            "HostScreen",
            (cls.bindings_module.ItemListWindowBindingsMixin,
             cls.bindings_module.FluidListWindowBindingsMixin,
             Screen),
            {},
        )

    def setUp(self):
        self.screen = type(self).Host()
        self.items = ["minecraft:water", "minecraft:lava"]
        self.catalog = types.SimpleNamespace(
            item_ids=["minecraft:stone"], fluid_ids=["minecraft:water", "mod:oil"],
            bucket_fluid_ids={"minecraft:water_bucket": "minecraft:water"}, windows=WeakSet())
        self.patcher = patch.object(self.item_selector, "get_catalog", return_value=self.catalog)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def open(self, items=None, maximum=4, title=None):
        items = self.items if items is None else items
        args = [self.screen, items, maximum]
        if title is not None:
            args.append(title)
        self.assertIs(self.module.show_fluid_list_window(*args), True)
        window = self.screen._vars[self.module.WINDOW_ATTRIBUTE]
        self.addCleanup(window.close)
        return window

    def resolve(self, flag, name):
        """按**绑定名**取回调，并核对旗标——引擎就是按名字取的。"""
        for attr in dir(type(self.screen)):
            func = getattr(type(self.screen), attr, None)
            if getattr(func, "binding_name", None) == name:
                self.assertEqual(
                    getattr(func, "binding_flags", None), flag,
                    "%s 的旗标不是 %r" % (name, flag))
                return getattr(self.screen, attr)
        raise AssertionError("宿主屏上没有 %r 这条绑定" % (name,))

    def declared(self):
        """宿主类体上声明的绑定：`(旗标, collection 或 None, 名字)`。"""
        out = set()
        for attr in dir(type(self.screen)):
            func = getattr(type(self.screen), attr, None)
            name = getattr(func, "binding_name", None)
            if name is None:
                continue
            out.add((getattr(func, "binding_flags", None),
                     getattr(func, "binding_collection", None), name))
        return out

    def bind(self, flag, name, *args):
        return self.resolve(flag, self.module.BINDING_PREFIX + name)(*args)

    def cell_bind(self, flag, name, index):
        return self.resolve(flag, self.module.BINDING_PREFIX + name)(index)

    def test_title_count_icons_and_selection_use_bindings(self):
        window = self.open(title="输入白名单")
        self.assertEqual(window.root.layer, 30)
        self.assertEqual(self.bind(BinderStub.BF_BindString, "title"), "输入白名单")
        self.assertEqual(self.bind(BinderStub.BF_BindInt, "count"), 2)
        self.assertEqual(self.cell_bind(BinderStub.BF_BindInt, "item_id_aux", 1), 12345)
        self.assertFalse(self.cell_bind(BinderStub.BF_BindBool, "selected", 0))
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 1})
        self.assertTrue(self.cell_bind(BinderStub.BF_BindBool, "selected", 1))
        self.assertFalse(self.cell_bind(BinderStub.BF_BindBool, "selected", 0))
        for invalid in (-1, 2, None, True, "0"):
            self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": invalid})
            self.assertEqual(window.selected_index, 1)
            self.assertFalse(self.cell_bind(BinderStub.BF_BindBool, "has_item", invalid))
            self.assertEqual(self.cell_bind(BinderStub.BF_BindInt, "item_id_aux", invalid), 0)

    def test_add_opens_real_selector_and_updates_original_list(self):
        window = self.open()
        window.root.GetElement("add_btn").callback({})
        picker = self.screen._vars[self.selector.WINDOW_ATTRIBUTE]
        self.assertGreater(picker.root.layer, window.root.layer)
        self.assertFalse(window.can_add())
        window.select({"#collection_index": 0})
        self.assertIsNone(window.selected_index)
        picker._select(1)
        self.assertTrue(picker.closed)
        self.assertEqual(self.items, ["minecraft:water", "minecraft:lava", "mod:oil"])
        self.assertEqual(self.bind(BinderStub.BF_BindInt, "count"), 3)
        self.assertTrue(self.cell_bind(BinderStub.BF_BindBool, "selected", 2))

    def test_duplicate_selects_existing_without_append_and_cancel_keeps_list(self):
        window = self.open()
        window.add()
        window.picker._select(0)
        self.assertEqual(self.items, ["minecraft:water", "minecraft:lava"])
        self.assertEqual(window.selected_index, 0)
        window.add()
        window.picker.close()
        self.assertEqual(self.items, ["minecraft:water", "minecraft:lava"])
        self.assertEqual(window.selected_index, 0)
        self.assertTrue(window.can_add())

    def test_delete_selected_updates_original_and_clears_highlight(self):
        window = self.open()
        window.delete_selected()
        self.assertEqual(len(self.items), 2)
        window.select({"#collection_index": 0})
        window.root.GetElement("del_btn").callback({})
        self.assertEqual(self.items, ["minecraft:lava"])
        self.assertFalse(self.cell_bind(BinderStub.BF_BindBool, "selected", 0))
        self.assertFalse(self.bind(BinderStub.BF_BindBool, "can_delete"))
        window.select({"#collection_index": 0})
        window.delete_selected()
        self.assertEqual(self.items, [])
        self.assertEqual(self.bind(BinderStub.BF_BindInt, "count"), 0)

    def test_limit_checked_on_open_and_again_when_picker_returns(self):
        window = self.open(maximum=2)
        self.assertFalse(window.can_add())
        window.add()
        self.assertNotIn(self.selector.WINDOW_ATTRIBUTE, self.screen._vars)
        window._on_item_selected("mod:oil")
        self.assertEqual(len(self.items), 2)
        window.select({"#collection_index": 0})
        window.delete_selected()
        self.assertTrue(window.can_add())

    def test_empty_zero_capacity_and_large_scroll_list(self):
        window = self.open([], 0)
        self.assertEqual(window.get_title(), "流体列表")
        self.assertFalse(window.can_add())
        self.assertFalse(window.can_delete())
        window.close()
        many = ["test:item_%d" % i for i in range(81)]
        window = self.open(many, 100)
        self.assertEqual(self.bind(BinderStub.BF_BindInt, "count"), 81)
        window.select({"#collection_index": 80})
        self.assertTrue(self.cell_bind(BinderStub.BF_BindBool, "selected", 80))
        window.delete_selected()
        self.assertEqual(len(many), 80)

    def test_duplicate_window_marker_and_invalid_inputs(self):
        for items, maximum in (([], -1), ([], True), ([], 1.5), (self.items, 1)):
            with self.assertRaises(ValueError):
                self.module.show_fluid_list_window(self.screen, items, maximum)
            self.assertNotIn(self.module.WINDOW_ATTRIBUTE, self.screen._vars)
        with self.assertRaises(TypeError):
            self.module.show_fluid_list_window(self.screen, (), 0)
        window = self.open()
        self.assertIs(self.module.show_fluid_list_window(self.screen, [], 4), False)
        self.assertIs(self.screen._vars[self.module.WINDOW_ATTRIBUTE], window)

    def test_close_cleans_owned_picker_and_marker_but_keeps_the_bindings(self):
        """关窗只清「打开标记」与它自己开的选择器；**绑定不摘**（11-07）。"""
        before = self.declared()
        window = self.open()
        window.add()
        picker = window.picker
        window.close()
        window.close()
        self.assertTrue(picker.closed)
        self.assertEqual(self.screen._vars, {})
        self.assertEqual(self.declared(), before)
        window._on_item_selected("mod:oil")
        self.assertEqual(len(self.items), 2)

    def test_unrelated_picker_is_not_replaced_or_closed(self):
        self.selector.show_select_fluid_window(self.screen, [], lambda _: None)
        picker = self.screen._vars[self.selector.WINDOW_ATTRIBUTE]
        self.addCleanup(picker.close)
        window = self.open()
        window.add()
        window.close()
        self.assertIs(self.screen._vars[self.selector.WINDOW_ATTRIBUTE], picker)
        self.assertFalse(picker.closed)

    def test_creation_failure_clears_the_marker_and_allows_retry(self):
        with patch.object(self.screen, "AddElement", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                self.module.show_fluid_list_window(self.screen, self.items, 4)
        self.assertEqual(self.screen._vars, {})
        self.open()

    def test_json_contract_matches_registered_binders(self):
        self.open()
        with open(os.path.join(ROOT, "endernet_rp", "ui", "endernet_windows.json"),
                  encoding="utf-8") as handle:
            data = json.load(handle)

        def controls(node):
            return {key.split("@")[0]: value for entry in node["controls"] for key, value in entry.items()}

        parts = controls(data["fluid_can_be_selected"])
        for control, name, target, flag in (
            (data["fluid_can_be_selected"], "has_item", "#visible", BinderStub.BF_BindBool),
            (parts["item_renderer"], "item_id_aux", "#item_id_aux", BinderStub.BF_BindInt),
            (parts["selected_frame"], "selected", "#visible", BinderStub.BF_BindBool),
        ):
            binding = control["bindings"][0]
            self.assertEqual(binding["binding_name_override"], target)
            key = (flag, binding["binding_collection_name"], binding["binding_name"])
            self.assertIn(key, self.declared())
            self.assertEqual(binding["binding_name"], self.module.BINDING_PREFIX + name)
        button = parts["interact_btn"]
        self.assertIn(
            (BinderStub.BF_ButtonClickUp, None, button["$pressed_button_name"]),
            self.declared())
        self.assertEqual(button["bindings"][0]["binding_collection_name"], self.module.COLLECTION)
        self.assertTrue(button["button_mappings"])
        grid = data["fluid_list_grid"]
        self.assertEqual(grid["collection_name"], self.module.COLLECTION)
        self.assertEqual(grid["bindings"][0]["binding_name_override"], "#maximum_grid_items")
        self.assertIn(
            (BinderStub.BF_BindInt, None, grid["bindings"][0]["binding_name"]),
            self.declared())
        window = controls(data["fluid_list_window"])
        self.assertEqual(window["scroll_view"]["$scrolling_content"], "endernet_windows.fluid_list_grid")
        for control, flag, target in (
            (controls(window["title"])["title_tip"], BinderStub.BF_BindString, "#text"),
            (window["add_btn"], BinderStub.BF_BindBool, "#enabled"),
            (window["del_btn"], BinderStub.BF_BindBool, "#enabled"),
        ):
            binding = control["bindings"][0]
            self.assertEqual(binding["binding_name_override"], target)
            self.assertIn((flag, None, binding["binding_name"]), self.declared())

    def test_fluid_and_item_lists_have_independent_bindings_and_lifetimes(self):
        self.item_list.show_item_list_window(self.screen, ["minecraft:stone"], 4)
        items = self.screen._vars[self.item_list.WINDOW_ATTRIBUTE]
        self.addCleanup(items.close)
        before = self.declared()
        fluids = self.open()
        fluids.add()
        self.assertEqual(fluids.picker.visible_ids, ["minecraft:water", "mod:oil"])
        fluids.close()
        self.assertEqual(self.declared(), before)
        self.assertIs(self.screen._vars[self.item_list.WINDOW_ATTRIBUTE], items)
        self.assertEqual(items.get_count(), 1)

    def test_backpack_bucket_selection_adds_fluid_identifier(self):
        window = self.open([])
        bucket = types.SimpleNamespace(newItemName="minecraft:water_bucket", count=1)
        with patch.object(self.selector, "GetLocalPlayerHotbarAndInvItems", return_value=[bucket]):
            window.add()
            picker = window.picker
            picker.source = "backpack"
            picker.refresh()
            picker._select(0)
        self.assertEqual(window.item_ids, ["minecraft:water"])


if __name__ == "__main__":
    unittest.main()
