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

from test_select_item_window import ROOT, Screen


class BinderStub(object):
    BF_BindString = 1
    BF_BindInt = 2
    BF_BindBool = 3
    BF_ButtonClickUp = 4
    BF_EditChanged = 5


class ItemStub(object):
    def __init__(self, item_id):
        self.item_id = item_id
        self.newItemName = item_id
        self.newItemName = item_id

    def GetBasicInfo(self):
        return types.SimpleNamespace(itemName=self.item_id, id_aux=12345)


class BoundScreen(Screen):
    def __init__(self):
        Screen.__init__(self)
        self.collection_bindings = {}

    def DynaBindingCollection(self, flag, collection, name, callback):
        self.collection_bindings[(flag, collection, name)] = callback

    def DynaUnbindingCollection(self, flag, collection):
        for key in list(self.collection_bindings):
            if key[:2] == (flag, collection):
                del self.collection_bindings[key]


class ItemListWindowTests(unittest.TestCase):
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
            full_name = prefix + "endernet.client.ui." + name
            path = os.path.join(ROOT, "endernet_bp", "endernet_scripts", "endernet",
                                "client", "ui", name + ".py")
            spec = importlib.util.spec_from_file_location(full_name, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        with patch.dict(sys.modules, modules):
            cls.selector = load("select_item_window")
            with patch.dict(sys.modules, {cls.selector.__name__: cls.selector}):
                cls.module = load("item_list_window")

    def setUp(self):
        self.screen = BoundScreen()
        self.items = ["minecraft:stone", "minecraft:dirt"]
        self.catalog = types.SimpleNamespace(
            item_ids=["minecraft:stone", "minecraft:apple"], windows=WeakSet())
        self.patcher = patch.object(self.selector, "get_catalog", return_value=self.catalog)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def open(self, items=None, maximum=4, title=None):
        items = self.items if items is None else items
        args = [self.screen, items, maximum]
        if title is not None:
            args.append(title)
        self.assertIs(self.module.show_item_list_window(*args), True)
        window = self.screen._vars[self.module.WINDOW_ATTRIBUTE]
        self.addCleanup(window.close)
        return window

    def bind(self, flag, name, *args):
        return self.screen.bindings[(flag, self.module.BINDING_PREFIX + name)](*args)

    def cell_bind(self, flag, name, index):
        return self.screen.collection_bindings[
            (flag, self.module.COLLECTION, self.module.BINDING_PREFIX + name)](index)

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
        self.assertEqual(self.items, ["minecraft:stone", "minecraft:dirt", "minecraft:apple"])
        self.assertEqual(self.bind(BinderStub.BF_BindInt, "count"), 3)
        self.assertTrue(self.cell_bind(BinderStub.BF_BindBool, "selected", 2))

    def test_duplicate_selects_existing_without_append_and_cancel_keeps_list(self):
        window = self.open()
        window.add()
        window.picker._select(0)
        self.assertEqual(self.items, ["minecraft:stone", "minecraft:dirt"])
        self.assertEqual(window.selected_index, 0)
        window.add()
        window.picker.close()
        self.assertEqual(self.items, ["minecraft:stone", "minecraft:dirt"])
        self.assertEqual(window.selected_index, 0)
        self.assertTrue(window.can_add())

    def test_delete_selected_updates_original_and_clears_highlight(self):
        window = self.open()
        window.delete_selected()
        self.assertEqual(len(self.items), 2)
        window.select({"#collection_index": 0})
        window.root.GetElement("del_btn").callback({})
        self.assertEqual(self.items, ["minecraft:dirt"])
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
        window._on_item_selected("minecraft:apple")
        self.assertEqual(len(self.items), 2)
        window.select({"#collection_index": 0})
        window.delete_selected()
        self.assertTrue(window.can_add())

    def test_empty_zero_capacity_and_large_scroll_list(self):
        window = self.open([], 0)
        self.assertEqual(window.get_title(), "物品列表")
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
                self.module.show_item_list_window(self.screen, items, maximum)
            self.assertNotIn(self.module.WINDOW_ATTRIBUTE, self.screen._vars)
        with self.assertRaises(TypeError):
            self.module.show_item_list_window(self.screen, (), 0)
        window = self.open()
        self.assertIs(self.module.show_item_list_window(self.screen, [], 4), False)
        self.assertIs(self.screen._vars[self.module.WINDOW_ATTRIBUTE], window)

    def test_close_cleans_owned_picker_and_bindings_but_keeps_other_bindings(self):
        self.screen.bindings[(99, "#Other.binding")] = lambda: None
        self.screen.collection_bindings[(99, "other", "#Other.binding")] = lambda: None
        window = self.open()
        window.add()
        picker = window.picker
        window.close()
        window.close()
        self.assertTrue(picker.closed)
        self.assertEqual(self.screen._vars, {})
        self.assertEqual(list(self.screen.bindings), [(99, "#Other.binding")])
        self.assertEqual(list(self.screen.collection_bindings), [(99, "other", "#Other.binding")])
        window._on_item_selected("minecraft:apple")
        self.assertEqual(len(self.items), 2)

    def test_unrelated_picker_is_not_replaced_or_closed(self):
        self.selector.show_select_item_window(self.screen, [], lambda _: None)
        picker = self.screen._vars[self.selector.WINDOW_ATTRIBUTE]
        self.addCleanup(picker.close)
        window = self.open()
        window.add()
        window.close()
        self.assertIs(self.screen._vars[self.selector.WINDOW_ATTRIBUTE], picker)
        self.assertFalse(picker.closed)

    def test_creation_failure_unbinds_and_allows_retry(self):
        with patch.object(self.screen, "AddElement", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                self.module.show_item_list_window(self.screen, self.items, 4)
        self.assertEqual(self.screen._vars, {})
        self.assertEqual(self.screen.bindings, {})
        self.assertEqual(self.screen.collection_bindings, {})
        self.open()

    def test_json_contract_matches_registered_binders(self):
        self.open()
        with open(os.path.join(ROOT, "endernet_rp", "ui", "endernet_windows.json"),
                  encoding="utf-8") as handle:
            data = json.load(handle)

        def controls(node):
            return {key.split("@")[0]: value for entry in node["controls"] for key, value in entry.items()}

        template = data["item_can_be_selected"]
        parts = controls(template)
        # 模板的集合名与绑定名是 `$` 变量（11-03 起与终端共用一份），因此先按
        # 它自己声明的 `|default` 把它们填回去——那些默认值就是本用例要守的
        # 11-09 口径。**不写死成字面量**：写死了这个用例就只是把 JSON 抄了一
        # 遍，默认值改了它照样绿，而 11-09 的窗口会当场瞎掉。
        variables = {}
        for key, value in template.items():
            if key.startswith("$"):
                variables["$" + key[1:].split("|")[0]] = value

        def filled(value):
            # type: (object) -> object
            return variables.get(value, value) if isinstance(value, str) else value

        self.assertEqual(filled("$item_collection_name"), self.module.COLLECTION)
        for control, name, target, flag in (
            (template, "has_item", "#visible", BinderStub.BF_BindBool),
            (parts["item_renderer"], "item_id_aux", "#item_id_aux", BinderStub.BF_BindInt),
            (parts["selected_frame"], "selected", "#visible", BinderStub.BF_BindBool),
        ):
            binding = control["bindings"][0]
            self.assertEqual(binding["binding_name_override"], target)
            key = (flag, filled(binding["binding_collection_name"]), filled(binding["binding_name"]))
            self.assertIn(key, self.screen.collection_bindings)
            self.assertEqual(
                filled(binding["binding_name"]), self.module.BINDING_PREFIX + name)
        button = parts["interact_btn"]
        self.assertIn(
            (BinderStub.BF_ButtonClickUp, filled(button["$pressed_button_name"])),
            self.screen.bindings)
        # 按键映射指向的是**控件自己的那个属性**，属性里装的才是绑定名，因此
        # 要按 `$pressed_button_name` 取一次值、再把这个值当变量填一次。
        self.assertEqual(
            filled(button[button["button_mappings"][0]["to_button_id"]]),
            self.module.BINDING_PREFIX + "select")
        self.assertEqual(
            filled(button["bindings"][0]["binding_collection_name"]), self.module.COLLECTION)
        grid = data["item_list_grid"]
        self.assertEqual(grid["collection_name"], self.module.COLLECTION)
        self.assertEqual(grid["bindings"][0]["binding_name_override"], "#maximum_grid_items")
        self.assertIn((BinderStub.BF_BindInt, grid["bindings"][0]["binding_name"]), self.screen.bindings)
        window = controls(data["item_list_window"])
        self.assertEqual(window["scroll_view"]["$scrolling_content"], "endernet_windows.item_list_grid")
        for control, flag, target in (
            (controls(window["title"])["title_tip"], BinderStub.BF_BindString, "#text"),
            (window["add_btn"], BinderStub.BF_BindBool, "#enabled"),
            (window["del_btn"], BinderStub.BF_BindBool, "#enabled"),
        ):
            binding = control["bindings"][0]
            self.assertEqual(binding["binding_name_override"], target)
            self.assertIn((flag, binding["binding_name"]), self.screen.bindings)


if __name__ == "__main__":
    unittest.main()
