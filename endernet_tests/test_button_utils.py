# coding=utf-8
"""Ore UI 按钮按压反馈的离线回归测试。"""

import unittest

from endernet_bp.endernet_scripts.endernet.client.ui.button_utils import (
    SetOreUiButtonCallback,
)


class Child(object):
    def __init__(self, position):
        self.position = position

    def GetPosition(self):
        return self.position

    def SetPosition(self, position):
        self.position = position


class Button(object):
    def __init__(self, children):
        self.children = children

    def asButton(self):
        return self

    def GetChildByPath(self, path):
        return self.children.get(path)

    def AddTouchEventParams(self, params):
        self.params = params

    def SetButtonTouchDownCallback(self, callback):
        self.down = callback

    def SetButtonTouchUpCallback(self, callback):
        self.up = callback

    def SetButtonTouchCancelCallback(self, callback):
        self.cancel = callback


class WrappedButton(object):
    def __init__(self, base):
        self.base = base


class OreUiButtonTests(unittest.TestCase):
    def test_optional_children_and_repeated_events(self):
        for paths in ((), ("/button_label",), ("/icon",),
                      ("/button_label", "/icon")):
            children = dict((path, Child((4, 8))) for path in paths)
            button = Button(children)
            calls = []
            self.assertIs(SetOreUiButtonCallback(button, calls.append), button)
            self.assertEqual(button.params, {"isSwallow": True})
            button.up({"before_down": True})
            for unused in range(3):
                button.down({})
                button.down({})
                for child in children.values():
                    self.assertEqual(child.position, (4, 9))
                button.up({"released": True})
                for child in children.values():
                    self.assertEqual(child.position, (4, 8))
            self.assertEqual(len(calls), 4)

    def test_wrapped_control_restores_before_callback(self):
        child = Child((2, 3))
        button = Button({"/icon": child})
        wrapped = WrappedButton(button)
        args = {"ButtonPath": "/button"}

        def callback(event):
            self.assertIs(event, args)
            self.assertEqual(child.position, (2, 3))
            return "clicked"

        self.assertIs(SetOreUiButtonCallback(wrapped, callback), wrapped)
        button.down({})
        self.assertEqual(button.up(args), "clicked")

    def test_cancel_restores_without_click_or_drift(self):
        child = Child((0, 0))
        button = Button({"/button_label": child})
        calls = []
        SetOreUiButtonCallback(button, calls.append)
        button.down({})
        button.cancel({})
        button.cancel({})
        self.assertEqual(child.position, (0, 0))
        self.assertEqual(calls, [])
        button.down({})
        button.up({})
        self.assertEqual(child.position, (0, 0))
        self.assertEqual(calls, [{}])

    def test_callback_error_occurs_after_restore(self):
        child = Child((1, 2))
        button = Button({"/icon": child})

        def fail(args):
            raise ValueError("callback failed")

        SetOreUiButtonCallback(button, fail)
        button.down({})
        with self.assertRaises(ValueError):
            button.up({})
        self.assertEqual(child.position, (1, 2))

    def test_invalid_callback(self):
        with self.assertRaises(TypeError):
            SetOreUiButtonCallback(Button({}), None)
