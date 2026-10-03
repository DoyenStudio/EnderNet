# coding=utf-8
"""Offline selector interactions and catalogue request/response lifecycle."""

import ast
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
        #: 真 `Item` 有这个字段，而**渲染要用它**（`SetUiItem(name, aux)`）。
        #: 替身原先没有，于是「画图标」这条路在离线从来跑不完。
        self.newAuxValue = 0
        self.count = count

    def GetBasicInfo(self):
        if self.newItemName == "minecraft:air":
            # 照真 `Item.GetBasicInfo`（`tooldelta/define/item.py:127-128`）：引擎
            # 的 `GetItemBasicInfo` 取不到时它抛 ValueError。**必须复现**——真机上
            # `GetLoadItems(True)` 正是会把 `minecraft:air` 报上来，替身若给它编一份
            # 分类，这个「整张目录永远为空」的缺陷在离线就永远测不出来。
            raise ValueError("Can't get basic info of " + self.newItemName)
        return types.SimpleNamespace(itemName="石头" if self.newItemName == "minecraft:stone"
                                     else self.newItemName, itemCategory=self.newItemName,
                                     id_aux=1234 if self.newItemName == "minecraft:stone" else 5678)


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

    def SetUiItem(self, name, aux):
        """`(name, aux)` 两个必填——真引擎的签名（2026-10-03 实测）。

        替身原先收一个 `Item` 对象，于是「传对象」那条错路在离线是绿的，
        真机上却每次抛 TypeError（格子全空、不报错）。
        """
        self.item = name

    def SetVisible(self, visible):
        self.visible = visible

    def GetGridItem(self, x, y):
        return self.GetElement((x, y))

    def ExecuteAfterUpdate(self, callback):
        """真 `UGrid` 有它：**网格建好之后**回调一次。

        这些用例里网格「已经建好」，因此立刻回调；「格子晚到」那一种由
        `LateGridControl` 专门复现（真机就是那一支，见它的 docstring）。
        """
        callback()

    def SetLayer(self, layer):
        self.layer = layer

    def Remove(self):
        self.removed = True


class BinderStub(object):
    """照真 `Binder` 的样子：两个装饰器往函数上贴标记并原样返回。

    11-07 起三个辅助窗口的绑定写在**宿主屏的类体**上，因此这里的替身必须
    把 `binding_flags` / `binding_name` 贴上——测试要像引擎那样按名字把
    回调取出来（见 `Screen` 的 docstring）。
    """

    BF_BindString = 1
    BF_BindInt = 2
    BF_BindBool = 3
    BF_ButtonClickUp = 4
    BF_EditChanged = 5

    @staticmethod
    def binding(flag, name):
        def decorate(func):
            func.binding_flags = flag
            func.binding_name = name
            return func
        return decorate

    @staticmethod
    def binding_collection(flag, collection, name):
        def decorate(func):
            func.binding_flags = flag
            func.collection_name = collection
            func.binding_collection = collection
            func.binding_name = name
            return func
        return decorate


class DynamicEngine(object):
    """登记名字，取值时才按名字找回调，复现引擎渲染阶段的解析。"""

    screen_name = "test"

    def __init__(self):
        self.bindings = {}
        self.collections = {}

    def _process_default(self, func, screen_name):
        self.bindings[(func.binding_flags, func.binding_name)] = func.__name__

    def _process_collection(self, func, screen_name):
        self.collections[(func.binding_flags, func.collection_name, func.binding_name)] = func.__name__

    def _process_default_unregister(self, func, screen_name):
        self.bindings.pop((func.binding_flags, func.binding_name), None)

    def _process_collection_unregister(self, func, screen_name):
        self.collections.pop((func.binding_flags, func.collection_name, func.binding_name), None)


def load_dynamic_screen():
    # 直接执行当前上游动态绑定方法，避免替身掩盖包装函数的签名/名字问题。
    path = os.path.join(ROOT, "endernet_bp", "endernet_scripts", "tooldelta",
                "ui", "general_screen.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    screen_cls = next(node for node in tree.body
              if isinstance(node, ast.ClassDef) and node.name == "ToolDeltaScreen")
    dynamic = next(node for node in screen_cls.body
               if isinstance(node, ast.If) and isinstance(node.test, ast.Name)
               and node.test.id == "SUPPORT_DYNA_BINDING")
    methods = ast.ClassDef(name="DynamicScreen", bases=[], keywords=[],
                   body=dynamic.body, decorator_list=[])
    extracted = ast.fix_missing_locations(ast.Module(body=[methods], type_ignores=[]))
    namespace = {"Binder": BinderStub}
    exec(compile(extracted, path, "exec"), namespace)
    return namespace["DynamicScreen"]


class Screen(load_dynamic_screen()):
    def __init__(self):
        self._vars = {}
        self._activated = True
        self._dyna_bindings = {}
        self._screen_instance = self._screen_node = DynamicEngine()

    def AddElement(self, definition, name):
        self.definition = definition
        self.root = Control()
        return self.root

    def callback(self, name):
        # type: (str) -> object
        """按**绑定名**取回调，与引擎同一条路（存名字、按名字 getattr）。"""
        engine = self._screen_instance
        for (flag, binding_name), callback_name in engine.bindings.items():
            if binding_name == name:
                return getattr(engine, callback_name)
        for attr in dir(type(self)):
            func = getattr(type(self), attr, None)
            if getattr(func, "binding_name", None) == name:
                return getattr(self, attr)
        raise AssertionError("宿主屏上没有 %r 这条绑定" % (name,))


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
            # `static` 照真总线（`event_bus.py:37-41`）复现：它会拿这个标记去给
            # listener 挂 `_td_static`，而**绑定方法挂不住属性**。替身以前只收下
            # 不作声，于是 `ItemCatalogClient.start()` / `ItemCatalogService.start()`
            # 里 `static=True` + `self.receive` 这个组合在离线一路绿灯，真机上
            # 却抛 AttributeError，把整个模组断在 `OnServerInited`。
            if static:
                if hasattr(callback, "_td_static"):
                    return callback
                callback._td_static = True
            self.handles.append((event, callback, priority))
            return callback
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
            ("tooldelta.ui", {"Binder": BinderStub}),
        ):
            module = types.ModuleType(prefix + suffix)
            module.__dict__.update(attrs)
            modules[module.__name__] = module

        def load(name):
            """按文件名加载一个界面模块（合成包名，避免碰真包）。"""
            path = os.path.join(ROOT, "endernet_bp", "endernet_scripts", "endernet",
                                "client", "ui", name.replace("/", os.sep) + ".py")
            spec = importlib.util.spec_from_file_location(
                prefix + "endernet.client.ui." + name.replace("/", "."), path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        with patch.dict(sys.modules, modules):
            cls.module = load("select_item_window")
            # 绑定在**宿主的类体**上（11-07），因此测试也要有一个宿主。
            # 真引擎那一步是 `ToolDeltaScreen._get_tdscreen_bindings` 生成
            # `_Base`；这里只要求「按名字取得到」，机制的取证在
            # `test_terminal_panel.TestEngineBindingResolution`。
            cls.bindings_module = load("widgets/list_window_bindings")
        cls.Host = type(
            "HostScreen",
            (cls.bindings_module.SelectItemWindowBindingsMixin, Screen),
            {},
        )

    def setUp(self):
        self.catalog = ItemCatalogClient(Bus(), EVENTS, Event)
        self.catalog.item_ids = ["test:item_%02d" % i for i in range(55)] + ["minecraft:stone"]
        self.screen = type(self).Host()
        self.selected = []
        self.patcher = patch.object(self.module, "get_catalog", return_value=self.catalog)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.assertIs(self.module.show_select_item_window(
            self.screen, ["minecraft:stone", "minecraft:stone"], self.selected.append), True)
        self.window = self.screen._vars[self.module.WINDOW_ATTRIBUTE]
        self.addCleanup(self.window.close)

    def search(self, text):
        # 按**绑定名**取回调——引擎就是这么找的，因此这条也在验名字对得上。
        self.screen.callback(self.module.SEARCH_BINDING)({"Text": text})

    def bound(self, suffix, index):
        for key, name in self.screen._screen_instance.collections.items():
            if key[2] == self.module.BINDING_PREFIX + suffix:
                # gui resolves callback names on the registration owner; for a
                # proxy this is not its underlying ScreenNode.
                return getattr(self.screen._screen_instance, name)(index)
        raise AssertionError("missing binding: " + suffix)

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
        self.assertFalse(any(self.bound("has_item", i) for i in range(40)))
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
        click = self.screen.callback(self.module.BINDING_PREFIX + "select")

        def callback(item_id):
            self.assertTrue(old_root.removed)
            # **绑定不随窗口摘除**：它在宿主类体上，是静态的（11-07 改法）。
            # 关窗后回调照样解析得到，只是窗口自己提前返回——这一条正是
            # 类体绑定与「运行时注册、关窗时摘掉」那条路的分水岭。
            self.assertTrue(self.window.closed)
            self.screen.callback(self.module.SEARCH_BINDING)({"Text": "minecraft:stone"})
            self.assertNotIn(self.module.WINDOW_ATTRIBUTE, self.screen._vars)
            self.selected.append(item_id)
            self.assertIs(self.module.show_select_item_window(self.screen, [], lambda _: None), True)
            self.reopened = self.screen._vars[self.module.WINDOW_ATTRIBUTE]

        self.window.select_callback = callback
        click({"#collection_index": 0})
        click({"#collection_index": 0})
        self.assertEqual(self.selected, ["test:item_00"])
        self.assertIs(self.screen._vars[self.module.WINDOW_ATTRIBUTE], self.reopened)
        self.reopened.root.GetElement("close").callback({})
        self.assertNotIn(self.module.WINDOW_ATTRIBUTE, self.screen._vars)
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
        # 静态搜索绑定保留，动态网格绑定清理。
        self.assertIsNotNone(self.screen.callback(self.module.SEARCH_BINDING))

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
        template = data["selected_item"]
        visible = template["bindings"][0]
        self.assertEqual(visible["binding_name"], "$selector_visible")
        self.assertEqual(template["$selector_visible|default"], self.module.BINDING_PREFIX + "has_item")
        self.assertEqual(visible["binding_collection_name"], "$selector_collection")
        self.assertEqual(template["$selector_collection|default"], self.module.COLLECTION)
        children = {key.split("@")[0]: value for entry in template["controls"]
                    for key, value in entry.items()}
        renderer = children["item_renderer"]["bindings"][0]
        self.assertEqual(renderer["binding_name"], "$selector_item_id_aux")
        self.assertEqual(template["$selector_item_id_aux|default"], self.module.BINDING_PREFIX + "item_id_aux")
        self.assertEqual(renderer["binding_name_override"], "#item_id_aux")
        self.assertEqual(renderer["binding_collection_name"], "$selector_collection")
        button = children["interact_btn"]
        self.assertEqual(button["$pressed_button_name"], "$selector_select")
        self.assertEqual(template["$selector_select|default"], self.module.BINDING_PREFIX + "select")
        self.assertEqual(button["bindings"][0]["binding_collection_name"], "$selector_collection")
        self.assertEqual(button["button_mappings"][0]["to_button_id"], "$pressed_button_name")


class CatalogTests(unittest.TestCase):
    def test_items_without_basic_info_are_dropped_not_fatal(self):
        """`minecraft:air` 这类没有物品定义的条目要跳过，不能让整张目录失败。

        真机上 `GetLoadItems(True)` 会把它报上来，`GetBasicInfo()` 抛 ValueError；
        这个异常一旦穿出 `load_catalog()`，后果不是「少一条」而是**客户端永远
        拿到空目录**——`receive()` 在发包之前就断了。
        """
        ids = filter_item_ids(
            ["minecraft:air", "", "command", "none", "items", "construction"],
            ItemStub,
        )
        self.assertEqual(ids, ["construction", "items"])

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



class DynamicGridTests(SelectorTests):
    def test_grid_never_reads_cells_or_renderer_paths(self):
        self.window.close()
        with patch.object(Control, "GetGridItem", side_effect=AssertionError("cell lookup")), \
                patch.object(Control, "SetUiItem", side_effect=AssertionError("renderer lookup")):
            self.module.show_select_item_window(self.screen, [], self.selected.append)
            self.window = self.screen._vars[self.module.WINDOW_ATTRIBUTE]
            self.search("stone")
            self.assertTrue(self.bound("has_item", 0))
            self.assertFalse(self.bound("has_item", 1))
            self.assertEqual(self.bound("item_id_aux", 0), 1234)
            self.assertEqual(self.bound("item_id_aux", -1), 131072)
            self.assertEqual(self.bound("item_id_aux", 40), 131072)

    def test_close_cleans_dynamic_bindings_and_name_attributes(self):
        engine = self.screen._screen_instance
        callbacks = list(engine.collections.values()) + list(engine.bindings.values())
        self.assertEqual(len(engine.collections), 2)
        self.window.close()
        self.assertEqual(engine.collections, {})
        self.assertEqual(engine.bindings, {})
        self.assertEqual(self.screen._dyna_bindings, {})
        for name in callbacks:
            self.assertFalse(hasattr(engine, name))

    def test_proxy_resolves_callbacks_on_registered_instance(self):
        self.window.close()
        self.screen._screen_node = DynamicEngine()
        self.module.show_select_item_window(self.screen, [], self.selected.append)
        self.window = self.screen._vars[self.module.WINDOW_ATTRIBUTE]
        for name in self.screen._screen_instance.collections.values():
            self.assertTrue(callable(getattr(self.screen._screen_instance, name)))
        self.assertEqual(self.bound("item_id_aux", 0), 5678)

    def test_partial_registration_failure_is_cleaned(self):
        self.window.close()
        with patch.object(self.screen._screen_instance, "_process_collection",
                          side_effect=RuntimeError("registration failed")):
            with self.assertRaises(RuntimeError):
                self.module.show_select_item_window(self.screen, [], self.selected.append)
        self.assertNotIn(self.module.WINDOW_ATTRIBUTE, self.screen._vars)
        self.assertEqual(self.screen._dyna_bindings, {})
        self.assertFalse(any(name.startswith("_td_dyna_binding_")
                             for name in vars(self.screen._screen_instance)))

if __name__ == "__main__":
    unittest.main()
