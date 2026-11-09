# coding=utf-8
"""触屏长按拆堆的数量、进度条的位置与拖动判定（`touch_input`）。"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.resources.key import ResourceKey
from endernet.client.ui.shared import slot_layout
from endernet.client.ui.terminal import touch_input
from endernet.client.ui.terminal.touch_input import TouchGesture, slide_count

THRESHOLD = 0.4
SLOT = (100.0, 200.0, 18.0, 18.0)


class TestSlideCount(unittest.TestCase):
    def test_half_rounded_up_and_the_bar_width_spans_the_whole_stack(self):
        self.assertEqual(slide_count(31, 0), 16)
        self.assertEqual(slide_count(30, touch_input.SPLIT_SLIDE_PIXELS / 2), 30)
        self.assertEqual(slide_count(30, -touch_input.SPLIT_SLIDE_PIXELS / 3), 5)
        self.assertEqual(slide_count(30, -1000), 1)
        self.assertEqual(slide_count(30, 1000), 30)


class FakeScreen(object):
    """任意路径都给一个 Mock；手指 = 跟随光标的宿主，根面板在 (0, 0)。"""

    def __init__(self, entry):
        self.now = 0.0
        self.controls = {}
        self.finger = (100.0, 100.0)
        self.backpack = Mock()
        self.backpack.entry.return_value = entry
        self.backpack.selected = None
        self.backpack.selected_count = None
        self.list_view = None

    def _clock(self):
        return self.now

    def top_modal(self):
        return None

    def GetBaseUIControl(self, path):
        if path not in self.controls:
            control = Mock()
            if path == "/window" + touch_input.HOST_PATH:
                control.GetGlobalPosition.side_effect = lambda: self.finger
            elif path in ("", "/window"):
                control.GetGlobalPosition.return_value = (0.0, 0.0)
            self.controls[path] = control
        return self.controls[path]


class TestTouchGesture(unittest.TestCase):
    def setUp(self):
        stack = SimpleNamespace(key=ResourceKey.item("minecraft:stone"), count=lambda: 30)
        self.screen = FakeScreen(stack)
        self.slot = SLOT
        patcher = patch.object(slot_layout, "backpack_slot_rect", side_effect=lambda screen, index: self.slot)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.touch = TouchGesture(self.screen, THRESHOLD)
        self.touch.press("bag", 0, {"TouchPosX": 10.0, "TouchPosY": 10.0})
        self.touch.tick()

    def control(self, path):
        return self.screen.controls[path]

    def overlay(self, name, child=None):
        path = "%s/%s" % (touch_input.OVERLAY_PATH, name)
        return self.control(path + ("/" + child if child else ""))

    def test_the_big_bar_sits_on_the_slot_starts_at_half_and_follows_the_slide(self):
        self.screen.now = THRESHOLD + 0.1
        self.touch.tick()
        self.control(touch_input.OVERLAY_PATH).SetPosition.assert_called_with((100.0, 200.0))
        self.overlay("split_up").SetVisible.assert_called_with(True)
        self.overlay("split_down").SetVisible.assert_called_with(False)
        self.overlay("split_up", "split_count").asLabel().SetText.assert_called_with("15")
        self.overlay("split_up", "split_fill").asImage().SetSpriteClipRatio.assert_called_with(0.5)
        self.screen.finger = (100.0 + touch_input.SPLIT_SLIDE_PIXELS / 2, 140.0)
        self.touch.tick()
        self.overlay("split_up", "split_count").asLabel().SetText.assert_called_with("30")
        self.assertTrue(self.touch.release({"TouchPosX": 300.0, "TouchPosY": 300.0}),
                        "拆堆时在哪松手都只是选中")
        self.screen.backpack.select_partial.assert_called_once_with(0, 30)
        self.control(touch_input.OVERLAY_PATH).SetVisible.assert_called_with(False)

    def test_near_the_top_the_bar_goes_below_the_slot(self):
        self.slot = (100.0, 10.0, 18.0, 18.0)
        self.screen.now = THRESHOLD + 0.1
        self.touch.tick()
        self.overlay("split_down").SetVisible.assert_called_with(True)

    def test_moving_before_the_threshold_drags_the_whole_stack(self):
        self.screen.finger = (160.0, 100.0)
        self.screen.now = 0.1
        self.touch.tick()
        self.control("/window" + touch_input.COUNT_PATH).asLabel().SetText.assert_called_with("30")
        self.screen.now = THRESHOLD + 1
        self.touch.tick()
        self.assertIsNone(self.touch.source["split"], "已经在拖了，不再进入拆堆")

    def test_a_release_without_coordinates_after_a_drag_cancels(self):
        self.screen.finger = (160.0, 100.0)
        self.touch.tick()
        self.assertTrue(self.touch.release({}))
        self.screen.backpack.select_partial.assert_not_called()
        self.assertIsNone(self.touch.source)

    def test_the_small_bar_marks_the_split_share_until_the_selection_goes(self):
        self.touch.release({"TouchPosX": 10.0, "TouchPosY": 10.0})
        self.screen.backpack.selected, self.screen.backpack.selected_count = 0, 10
        self.touch.tick()
        bar = self.control("/window" + touch_input.BAR_PATH)
        bar.SetPosition.assert_called_with((101.5, 200.5))
        self.assertEqual(self.touch.selection_bar(), (10, 30), "显示与数值走面板的 split_bar 绑定")
        self.screen.backpack.selected = None
        self.touch.tick()
        self.assertIsNone(self.touch.selection_bar())


if __name__ == "__main__":
    unittest.main()
