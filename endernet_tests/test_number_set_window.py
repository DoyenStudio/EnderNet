# coding=utf-8
"""Offline quantity dialog interactions and resource binding contract."""

import importlib.util
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

from test_select_item_window import BinderStub, ROOT, Control, Screen


class NumberControl(Control):
    def __init__(self):
        Control.__init__(self)
        self.text = ""

    def GetElement(self, path):
        return self.children.setdefault(path, NumberControl())

    def asTextEditBox(self):
        return self

    def asLabel(self):
        return self

    def SetText(self, text):
        self.text = text

    def GetText(self):
        return self.text


class NumberScreen(Screen):
    def AddElement(self, definition, name):
        self.definition = definition
        self.root = NumberControl()
        return self.root


class NumberSetWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        prefix = "endernet_bp.endernet_scripts."
        ui = types.ModuleType(prefix + "tooldelta.ui")
        ui.Binder = BinderStub
        modules = {ui.__name__: ui}

        def load(name):
            path = os.path.join(ROOT, "endernet_bp", "endernet_scripts", "endernet",
                                "client", "ui", name.replace("/", os.sep) + ".py")
            spec = importlib.util.spec_from_file_location(
                prefix + "endernet.client.ui." + name.replace("/", "."), path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        with patch.dict(sys.modules, modules):
            cls.module = load("number_set_window")
            # 编辑框的绑定写在**宿主的类体**上（11-07），测试因此也要有宿主。
            cls.bindings_module = load("widgets/list_window_bindings")
        cls.Host = type(
            "HostScreen",
            (cls.bindings_module.NumberSetWindowBindingsMixin, NumberScreen),
            {},
        )

    def setUp(self):
        self.screen = type(self).Host()
        self.confirmed = []

    def open(self, number=64, minimum=1, maximum=None, callback=None, title=None):
        args = (self.screen, number, callback or self.confirmed.append, minimum, maximum)
        if title is None:
            opened = self.module.show_number_set_window(*args)
        else:
            opened = self.module.show_number_set_window(*args, title=title)
        self.assertTrue(opened)
        self.window = self.screen._vars[self.module.WINDOW_ATTRIBUTE]
        self.root = self.screen.root
        self.editor = self.root.GetElement("num_editor")

    def click(self, path):
        self.root.GetElement(path).callback({})

    def edit(self, text):
        self.editor.SetText(text)
        # 按**绑定名**取回调——引擎就是按名字取的（11-07 起绑定在宿主类体上）。
        self.screen.callback(self.module.EDIT_BINDING)({"Text": text})

    def assert_closed_window_ignores_edits(self, window):
        """已关的窗口不再接受编辑。

        **绑定不会被摘掉**（它在宿主类体上是静态的）：这条判据因此从
        「运行时注册被摘除」换成「回调仍在、但窗口自己提前返回」。
        """
        before = window.number
        self.screen.callback(self.module.EDIT_BINDING)({"Text": "999"})
        self.assertEqual(window.number, before)

    def test_open_initial_quantity_duplicate_and_set_number(self):
        self.open()
        self.assertEqual(self.screen.definition, "endernet_windows.number_set_window")
        self.assertEqual(self.root.layer, 50)
        self.assertEqual(self.editor.GetText(), "64")
        self.assertFalse(self.module.show_number_set_window(self.screen, 1, self.confirmed.append))
        self.window.set_number(128)
        self.assertEqual(self.editor.GetText(), "128")

    def test_title_defaults_and_can_be_customized(self):
        self.open()
        self.assertEqual(self.root.GetElement("title/title_tip").GetText(),
                         self.module.DEFAULT_TITLE)
        self.window.close()
        self.open(title="设置流体数量")
        self.assertEqual(self.root.GetElement("title/title_tip").GetText(), "设置流体数量")

    def test_all_six_steps_and_bounds(self):
        self.open(200, maximum=1000)
        for path, expected in (("add_1x_btn", 201), ("add_10x_btn", 211),
                               ("add_100x_btn", 311), ("sub_1x_btn", 310),
                               ("sub_10x_btn", 300), ("sub_100x_btn", 200)):
            self.click(path)
            self.assertEqual(self.editor.GetText(), str(expected))
        self.window.set_number(1)
        self.click("sub_100x_btn")
        self.assertEqual(self.editor.GetText(), "1")
        self.window.set_number(999)
        self.click("add_100x_btn")
        self.assertEqual(self.editor.GetText(), "1000")

    def test_confirm_reads_latest_text_closes_before_callback_and_fires_once(self):
        def confirmed(number):
            self.assertNotIn(self.module.WINDOW_ATTRIBUTE, self.screen._vars)
            self.assertTrue(self.root.removed)
            self.assert_closed_window_ignores_edits(self.window)
            self.confirmed.append(number)

        self.open(callback=confirmed)
        self.editor.SetText(" 12345678901234567890 ")
        self.click("confirm_btn")
        self.click("confirm_btn")
        self.assertEqual(self.confirmed, [12345678901234567890])

    def test_invalid_input_cannot_confirm_and_step_recovers_last_valid_number(self):
        self.open(maximum=1000)
        self.edit("120")
        for text in ("", "abc", "1.5", "-1", "0", "1001", "1e2", "1_0", "+2"):
            self.edit(text)
            self.click("confirm_btn")
            self.assertFalse(self.confirmed)
            self.assertFalse(self.window.closed)
        self.click("add_10x_btn")
        self.assertEqual(self.editor.GetText(), "130")
        self.click("confirm_btn")
        self.assertEqual(self.confirmed, [130])

    def test_steps_read_text_even_without_edit_event(self):
        self.open()
        self.editor.SetText("90")
        self.click("add_10x_btn")
        self.assertEqual(self.editor.GetText(), "100")

    def test_zero_allowed_when_requested_and_initial_clamped(self):
        self.open(-10, minimum=0, maximum=100)
        self.assertEqual(self.editor.GetText(), "0")
        self.window.set_number(200)
        self.assertEqual(self.editor.GetText(), "100")
        self.edit("0")
        self.click("confirm_btn")
        self.assertEqual(self.confirmed, [0])

    def test_cancel_cleanup_reopen_and_stale_callbacks(self):
        self.open()
        old = self.window
        self.click("close")
        self.click("confirm_btn")
        self.click("add_1x_btn")
        old.close()
        self.assertFalse(self.confirmed)
        self.assert_closed_window_ignores_edits(old)
        self.open(5)
        old.on_edited({"Text": "100"})
        old.close()
        self.assertIs(self.screen._vars[self.module.WINDOW_ATTRIBUTE], self.window)
        self.assertEqual(self.editor.GetText(), "5")

    def test_inactive_host_cleanup_does_not_touch_removed_control(self):
        self.open()
        self.screen._activated = False
        self.click("confirm_btn")
        self.click("add_1x_btn")
        self.window.close()
        self.assertFalse(self.root.removed)
        self.assertFalse(self.confirmed)
        self.assert_closed_window_ignores_edits(self.window)
        self.assertNotIn(self.module.WINDOW_ATTRIBUTE, self.screen._vars)

    def test_creation_failure_cleans_marker(self):
        with patch.object(NumberControl, "SetText", side_effect=RuntimeError("engine failure")):
            with self.assertRaises(RuntimeError):
                self.open()
        self.assertNotIn(self.module.WINDOW_ATTRIBUTE, self.screen._vars)
        self.assertTrue(self.screen.root.removed)
        self.open()

    def test_bad_arguments_do_not_create_controls(self):
        for number, minimum, maximum in ((True, 1, None), (None, 1, None),
                                         (1.5, 1, None), (1, -1, None),
                                         (1, 10, 9), (1, 1, False)):
            with self.assertRaises(ValueError):
                self.module.show_number_set_window(
                    self.screen, number, self.confirmed.append, minimum, maximum)
        with self.assertRaises(TypeError):
            self.module.show_number_set_window(self.screen, 1, None)
        for title in (None, 5, ("a",)):
            with self.assertRaises(TypeError):
                self.module.show_number_set_window(
                    self.screen, 1, self.confirmed.append, 1, None, title)
        self.assertFalse(self.screen._vars)

    def test_json_control_paths_and_binding_do_not_collide_with_item_search(self):
        with open(os.path.join(ROOT, "endernet_rp", "ui", "endernet_windows.json"),
                  encoding="utf-8") as handle:
            data = json.load(handle)
        controls = {key.split("@")[0]: value
                    for entry in data["number_set_window"]["controls"]
                    for key, value in entry.items()}
        for path, _ in self.module.STEP_BUTTONS:
            self.assertIn(path, controls)
        self.assertIn("confirm_btn", controls)
        self.assertIn("close", controls)
        self.assertEqual(controls["num_editor"]["$text_box_name"], self.module.EDIT_BINDING)
        self.assertEqual(controls["num_editor"]["$text_edit_box_content_binding_name"],
                         self.module.EDIT_BINDING + "_content")
        self.assertNotEqual(self.module.EDIT_BINDING, "#EnderNetSelectItem.search")
        title_tip = controls["title"]["controls"][0]["title_tip"]
        self.assertEqual(title_tip["type"], "label")
        self.assertEqual(title_tip["text"], self.module.DEFAULT_TITLE)
        self.assertEqual(self.module.TITLE_PATH, "title/title_tip")


if __name__ == "__main__":
    unittest.main()
