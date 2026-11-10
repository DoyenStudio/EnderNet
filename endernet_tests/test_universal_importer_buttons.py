# coding=utf-8
"""采集器按钮的三态文字与延迟挂载行为。"""

import ast
import os
import types
import unittest
from unittest.mock import Mock

import testsupport

from endernet_bp.endernet_scripts.endernet.client.ui.shared.button_utils import (
    button_content_paths,
)


def importer_class():
    """直接执行生产类，省去离线不可用的引擎事件总线注册。"""
    path = os.path.join(testsupport.ENDERNET_ROOT, "client", "ui",
                        "universal_importer", "screen.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    original = next(node for node in tree.body
                    if isinstance(node, ast.ClassDef)
                    and node.name == "UniversalImporterScreen")
    original.bases = [ast.Name(id="object", ctx=ast.Load())]
    original.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[original], type_ignores=[]))
    namespace = {"button_content_paths": button_content_paths}
    exec(compile(module, path, "exec"), namespace)
    return namespace[original.name]


class Control(object):
    def __init__(self, nodes, path):
        self.nodes = nodes
        self.path = path
        self.text = None
        self.callback = None

    def GetElement(self, path):
        return self.nodes[self.path + "/" + path]

    def asButton(self):
        return self

    asLabel = asButton

    def SetText(self, text):
        self.text = text

    def SetCallback(self, callback):
        self.callback = callback

    def SetLayer(self, layer):
        self.layer = layer


class TestImporterButtonLabels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Importer = importer_class()

    def setUp(self):
        self.nodes = {}
        paths = ["", "/control_board"]
        for name in ("sample_btn", "mode_btn", "save_btn", "exit_btn"):
            paths.append("/control_board/" + name)
        for name in ("sample_btn", "mode_btn"):
            for suffix in button_content_paths("button_label"):
                paths.append("/control_board/%s/%s" % (name, suffix))
        for path in paths:
            full = "/overlay" + path
            self.nodes[full] = Control(self.nodes, full)
        self.view = object.__new__(self.Importer)
        self.view._view_root = self.nodes["/overlay"]
        self.view._view_path = "/overlay"
        self.view._view_host = types.SimpleNamespace(base=types.SimpleNamespace(
            GetBaseUIControl=self.nodes.get))
        self.view._view_bound = False
        self.view._board_position = None
        self.view.selecting = False
        self.view.mode = "inputs"
        self.view.closed = False
        self.view._scan_once = Mock()
        self.view._clear_sections = Mock()

    def labels(self, name):
        return [self.nodes["/overlay/control_board/%s/%s" % (name, suffix)]
                for suffix in button_content_paths("button_label")]

    def test_mount_and_clicks_keep_three_state_labels_in_sync(self):
        self.assertTrue(self.view._finish_mount())
        self.assertTrue(self.view._view_bound)
        self.assertEqual([label.text for label in self.labels("sample_btn")],
                         ["开始选择"] * 3)
        self.assertEqual([label.text for label in self.labels("mode_btn")],
                         ["输入模式"] * 3)
        self.nodes["/overlay/control_board/sample_btn"].callback({})
        self.assertEqual([label.text for label in self.labels("sample_btn")],
                         ["关闭选择"] * 3)
        self.view._scan_once.assert_called_once_with()
        self.nodes["/overlay/control_board/mode_btn"].callback({})
        self.assertEqual([label.text for label in self.labels("mode_btn")],
                         ["输出模式"] * 3)
        self.nodes["/overlay/control_board/sample_btn"].callback({})
        self.assertEqual([label.text for label in self.labels("sample_btn")],
                         ["开始选择"] * 3)
        self.view._clear_sections.assert_called_once_with()

    def test_mount_waits_for_pressed_label_then_initializes_current_state(self):
        full = "/overlay/control_board/mode_btn/" + button_content_paths("button_label")[-1]
        late = self.nodes.pop(full)
        self.assertFalse(self.view._finish_mount())
        self.assertFalse(self.view._view_bound)
        self.assertIsNone(self.nodes["/overlay/control_board/sample_btn"].callback)
        self.view.selecting = True
        self.view.mode = "outputs"
        self.nodes[full] = late
        self.assertTrue(self.view._finish_mount())
        self.assertEqual([label.text for label in self.labels("sample_btn")],
                         ["关闭选择"] * 3)
        self.assertEqual([label.text for label in self.labels("mode_btn")],
                         ["输出模式"] * 3)

    def test_missing_label_prevents_partial_update_and_later_update_recovers(self):
        self.assertTrue(self.view._finish_mount())
        full = "/overlay/control_board/sample_btn/" + button_content_paths("button_label")[1]
        late = self.nodes.pop(full)
        self.view.mode = "outputs"
        self.view._update_labels()
        self.assertEqual([label.text for label in self.labels("mode_btn")],
                         ["输入模式"] * 3)
        self.nodes[full] = late
        self.view._update_labels()
        self.assertEqual([label.text for label in self.labels("mode_btn")],
                         ["输出模式"] * 3)
