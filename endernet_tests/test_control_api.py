# coding=utf-8
"""`widgets/control_api.py` 的两个形状（都是 2026-10-03 真机撞出来的）。

这个文件守的是**控件 API 的形状**——而形状错了在真机上的表现是
「那一格空着」或「点了没反应」：不报错、不刷屏、日志干净，因此只能靠
**把替身照真机写**来拦住它。两条各一：

1. `SetUiItem` 收 `(name, aux)`，**不是 `Item` 对象**；
2. 引擎的 `ButtonUIControl` **没有 `SetCallback`**——而 `slot_layout` 那条路
   （取主面板上的控件用的）拿到的正是引擎控件，`.asButton()` 也就给引擎按钮。

替身按真机上 `dir()` 读到的形状写（`AddTouchEventParams`、
`SetButtonTouchUpCallback` 有，`SetCallback` 没有），不是照本仓的包装类写。
"""

import unittest

import testsupport  # noqa: F401  (放进 sys.path)

from endernet.client.ui.widgets.control_api import on_click, show_item


class EngineButtonStub(object):
    """**引擎的** `ButtonUIControl`：有那两个方法、**没有 `SetCallback`**。

    真机 `dir()` 实测（2026-10-03）：`AddTouchEventParams`、
    `SetButtonTouchUpCallback`、`SetButtonTouchDownCallback` 一应俱全，
    而 `SetCallback` **不存在**——那是 tooldelta 的 `UButton` 才有的。
    """

    def __init__(self):
        self.touch_params = []
        self.touch_up = None

    def AddTouchEventParams(self, params):
        self.touch_params.append(params)

    def SetButtonTouchUpCallback(self, callback):
        self.touch_up = callback


class WrapperButtonStub(object):
    """tooldelta 的 `UButton`：只有 `SetCallback`（`AddElement` 那条路给的）。"""

    def __init__(self):
        self.callback = None

    def SetCallback(self, callback):
        self.callback = callback


class ControlStub(object):
    def __init__(self, button):
        self._button = button

    def asButton(self):
        return self._button


class RendererStub(object):
    def __init__(self):
        self.calls = []

    def SetUiItem(self, *args):
        self.calls.append(args)


class RendererControlStub(object):
    def __init__(self, renderer):
        self._renderer = renderer

    def asItemRenderer(self):
        return self._renderer


class TestShowItem(unittest.TestCase):
    def test_it_passes_the_name_and_the_aux_value(self):
        """**两个参数**，不是 `Item` 对象。

        真机上 `SetUiItem()` 至少收两个参数，传一个对象会抛
        `TypeError: SetUiItem() takes at least 3 arguments (2 given)`——
        而界面上只表现为那一格空着。这条把「两个」钉死：替身如果哪天被
        改成收对象，这里会变红。
        """
        renderer = RendererStub()
        show_item(RendererControlStub(renderer), "minecraft:stone", 3)
        self.assertEqual(renderer.calls, [("minecraft:stone", 3)])

    def test_the_aux_value_defaults_to_zero(self):
        """只给名字时补 0——不是省掉第二个参数（那样真机会抛）。"""
        renderer = RendererStub()
        show_item(RendererControlStub(renderer), "minecraft:barrier")
        self.assertEqual(renderer.calls, [("minecraft:barrier", 0)])


class TestOnClick(unittest.TestCase):
    def test_the_engine_button_gets_the_two_engine_calls(self):
        """没有 `SetCallback` 时走引擎那一对，并**吞掉这次触摸**。

        `isSwallow` 是照抄 tooldelta `UButton.SetCallback` 的取值：不吞的话，
        点在容器屏幕的按钮上会同时落到它底下的槽位上。
        """
        button = EngineButtonStub()
        callback = object()
        on_click(ControlStub(button), callback)
        self.assertEqual(button.touch_params, [{"isSwallow": True}])
        self.assertIs(button.touch_up, callback)

    def test_the_tooldelta_wrapper_is_used_when_it_has_set_callback(self):
        """`AddElement` / `GetElement` 那条路给的是包装类，直接用它的 `SetCallback`。"""
        button = WrapperButtonStub()
        callback = object()
        on_click(ControlStub(button), callback)
        self.assertIs(button.callback, callback)

    def test_a_control_without_a_button_api_is_not_silently_ignored(self):
        """取到的控件不是按钮时**抛**——调用方各自决定要不要容忍。

        静默返回会让「这个控件接不上」与「接上了但控件不对」长得一样。
        """

        class NotAButton(object):
            def asButton(self):
                return object()

        with self.assertRaises(AttributeError):
            on_click(NotAButton(), object())


if __name__ == "__main__":
    unittest.main()
