# coding=utf-8
"""Execute the real ToolDelta class with SDK-shaped, name-resolving screens.

The SDK's gui.py forwards func.__name__ (not the callable) to native code.
These stubs retain only that name and resolve it again when dispatching.
"""
import ast
import os
import unittest

import py2check


SOURCE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                      "endernet_bp", "endernet_scripts", "tooldelta", "ui",
                      "general_screen.py")


class Binder(object):
    BF_BindInt = 6 | 16777216
    BF_BindString = 8 | 16777216
    BF_ButtonClick = 2 | 268435456
    BF_EditChanged = 11 | 1048576
    BF_ToggleChanged = 13 | 65536
    BF_SliderChanged = 1 | 4096

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
            Binder.binding(flag, name)(func)
            func.collection_name = collection
            return func
        return decorate


class EngineBindings(object):
    def _init_bindings(self):
        self.registry = {}
        self.fail_register = False
        self.fail_unregister = False

    def _process_default(self, func, screen_name):
        if self.fail_register:
            raise RuntimeError("registration failed")
        self.registry[(screen_name, None, func.binding_name)] = func.__name__

    def _process_collection(self, func, screen_name):
        if self.fail_register:
            raise RuntimeError("registration failed")
        self.registry[(screen_name, func.collection_name, func.binding_name)] = func.__name__

    def _unregister(self, func, screen_name, collection):
        if self.fail_unregister:
            raise RuntimeError("unregistration failed")
        self.registry.pop((screen_name, collection, func.binding_name), None)

    def _process_default_unregister(self, func, screen_name):
        self._unregister(func, screen_name, None)

    def _process_collection_unregister(self, func, screen_name):
        self._unregister(func, screen_name, func.collection_name)

    def dispatch(self, screen_name, collection, binding_name, *args):
        name = self.registry[(screen_name, collection, binding_name)]
        return getattr(self, name)(*args)


class ScreenNode(EngineBindings):
    def __init__(self, namespace, name, param=None):
        self._init_bindings()
        self.screen_name = ""
        self.full_name = namespace + "." + name


class ScreenProxy(EngineBindings):
    def __init__(self, screen_name, screen_node):
        self._init_bindings()
        self.screenName = screen_name
        self.screenNode = screen_node

    def GetScreenName(self):
        return self.screenName


class EventService(object):
    def __init__(self, bus):
        pass


def load_screen_class():
    with open(SOURCE, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), SOURCE)
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == "ToolDeltaScreen")
    extracted = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    namespace = {"EventListenerService": EventService, "GetMCClientEventBus": lambda: None,
                 "ScreenNode": ScreenNode, "ScreenProxy": ScreenProxy,
                 "Binder": Binder, "SUPPORT_DYNA_BINDING": True, "__name__": __name__}
    exec(compile(extracted, SOURCE, "exec"), namespace)
    return namespace["ToolDeltaScreen"]


class DynaBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Screen = load_screen_class()

    def setUp(self):
        self.node = ScreenNode("demo", "main")
        self.screen = self.Screen("main", self.node)

    def read(self, name="#value", collection=None, *args):
        return self.screen._screen_instance.dispatch(
            self.screen._get_dyna_screen_name(), collection, name, *args)

    def test_reader_is_resolved_on_engine_instance(self):
        def reader(index):
            return index + 131072
        self.screen.DynaBindingCollection(Binder.BF_BindInt, "items", "#value", reader)
        self.assertEqual(self.read("#value", "items", 2), 131074)
        self.assertFalse(hasattr(self.node, "reader"))

    def test_bound_methods_and_events_keep_argument_shape(self):
        class Receiver(object):
            def receive(self, params):
                return params["value"]
        for flag in (Binder.BF_ButtonClick, Binder.BF_EditChanged,
                     Binder.BF_ToggleChanged, Binder.BF_SliderChanged):
            with self.subTest(flag=flag):
                name = "#event_%s" % flag
                self.screen.DynaBinding(flag, name, Receiver().receive)
                self.assertEqual(self.read(name, None, {"value": 7}), 7)

    def test_same_reader_and_static_method_do_not_collide(self):
        self.node.reader = lambda: "static"
        def reader(index):
            return index
        for name, collection in (("#a", "items"), ("#b", "items"), ("#a", "other")):
            self.screen.DynaBindingCollection(Binder.BF_BindInt, collection, name, reader)
            self.assertEqual(self.read(name, collection, 3), 3)
        self.assertEqual(len(set(self.node.registry.values())), 3)
        self.assertEqual(self.node.reader(), "static")

    def test_replacement_removes_old_wrapper(self):
        self.screen.DynaBinding(Binder.BF_BindString, "#value", lambda: "old")
        old_name = next(iter(self.node.registry.values()))
        self.screen.DynaBinding(Binder.BF_BindString, "#value", lambda: "new")
        self.assertEqual(self.read(), "new")
        self.assertFalse(hasattr(self.node, old_name))
        self.assertEqual(len(self.screen._dyna_bindings), 1)

    def test_unbinding_is_idempotent_and_removes_wrapper(self):
        self.screen.DynaBinding(Binder.BF_BindInt, "#value", lambda: 5)
        name = next(iter(self.node.registry.values()))
        self.screen.DynaUnbinding(Binder.BF_BindInt, "#value")
        self.screen.DynaUnbinding(Binder.BF_BindInt, "#value")
        self.assertFalse(hasattr(self.node, name))
        self.assertEqual(self.node.registry, {})
        self.assertEqual(self.screen._dyna_bindings, {})

    def test_collection_unbinding_keeps_other_collections(self):
        for collection in ("items", "other"):
            for name in ("#a", "#b"):
                self.screen.DynaBindingCollection(Binder.BF_BindInt, collection, name, lambda i: i)
        self.screen.DynaUnbindingCollection(Binder.BF_BindInt, "items")
        self.assertEqual(len(self.screen._dyna_bindings), 2)
        self.assertEqual(self.read("#a", "other", 9), 9)

    def test_destroy_clears_all_wrappers(self):
        self.screen.DynaBinding(Binder.BF_BindInt, "#value", lambda: 1)
        self.screen.DynaBindingCollection(Binder.BF_BindInt, "items", "#value", lambda i: i)
        names = list(self.node.registry.values())
        self.screen._do_deactive = lambda **kwargs: None
        self.screen._on_destroy()
        self.assertEqual(self.screen._dyna_bindings, {})
        self.assertEqual(self.node.registry, {})
        self.assertTrue(all(not hasattr(self.node, name) for name in names))

    def test_instances_do_not_share_callbacks(self):
        other_node = ScreenNode("demo", "main")
        other = self.Screen("main", other_node)
        self.screen.DynaBinding(Binder.BF_BindInt, "#value", lambda: 1)
        other.DynaBinding(Binder.BF_BindInt, "#value", lambda: 2)
        self.assertEqual(self.read(), 1)
        self.assertEqual(other_node.dispatch("demo.main", None, "#value"), 2)
        self.screen._clear_dyna_bindings()
        self.assertEqual(other_node.dispatch("demo.main", None, "#value"), 2)

    def test_screen_name_prefers_runtime_name(self):
        self.node.screen_name = "runtime.main"
        self.screen.DynaBinding(Binder.BF_BindInt, "#value", lambda: 1)
        self.assertEqual(self.node.dispatch("runtime.main", None, "#value"), 1)

    def test_generated_proxy_uses_proxy_as_binding_owner(self):
        base = self.Screen._register_as_proxy("native.main")
        proxy = base("native.runtime", self.node)
        self.screen = proxy._super_screen_ins
        self.assertIs(self.screen.base, self.node)
        self.assertIs(self.screen._screen_instance, proxy)
        self.screen.DynaBinding(Binder.BF_BindString, "#value", lambda: "proxy")
        self.screen.DynaBindingCollection(Binder.BF_BindInt, "items", "#value", lambda i: i)
        self.assertEqual(self.read(), "proxy")
        self.assertEqual(self.read("#value", "items", 4), 4)
        self.assertEqual(self.node.registry, {})
        self.screen._clear_dyna_bindings()
        self.assertEqual(proxy.registry, {})

    def test_registration_failure_does_not_leave_callback(self):
        self.node.fail_register = True
        for collection in (None, "items"):
            with self.assertRaises(RuntimeError):
                if collection is None:
                    self.screen.DynaBinding(Binder.BF_BindInt, "#value", lambda: 1)
                else:
                    self.screen.DynaBindingCollection(Binder.BF_BindInt, collection, "#value", lambda i: i)
            self.assertEqual(self.screen._dyna_bindings, {})
            self.assertFalse(any(name.startswith("_td_dyna_binding_") for name in vars(self.node)))

    def test_unregistration_failure_retains_callback_for_retry(self):
        self.screen.DynaBinding(Binder.BF_BindInt, "#value", lambda: 1)
        self.node.fail_unregister = True
        with self.assertRaises(RuntimeError):
            self.screen.DynaUnbinding(Binder.BF_BindInt, "#value")
        self.assertEqual(self.read(), 1)
        self.node.fail_unregister = False
        self.screen.DynaUnbinding(Binder.BF_BindInt, "#value")
        self.assertEqual(self.screen._dyna_bindings, {})

    def test_framework_file_passes_python2_syntax_gate(self):
        self.assertEqual(py2check.check_paths([SOURCE]), [])
