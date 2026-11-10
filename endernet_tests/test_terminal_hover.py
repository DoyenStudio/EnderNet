# coding=utf-8
"""Hover text served per cell through the slot button's hover state."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import testsupport  # noqa: F401
from endernet.common.resource_key import ResourceKey
from endernet.client.ui import stack_size, terminal_hover, terminal_input


class TestTerminalHover(unittest.TestCase):
    def setUp(self):
        self.key = ResourceKey.item("minecraft:stone", 1, user_data={"display": {"Name": "named"}})
        self.row = SimpleNamespace(key=self.key, stored=64, name="Stone", has_pattern=False)
        self.view = SimpleNamespace(rows=[self.row], has_item=lambda i: i == 0)
        self.modal = None
        self.screen = SimpleNamespace(list_view=self.view, top_modal=lambda: self.modal,
                                      _clock=lambda: 1, backpack=Mock())
        self.router = terminal_input.TerminalInput(self.screen)
        self.router.desktop = True
        self.hover = self.router.hover
        self.format_patch = patch.object(terminal_hover, "formatted_hover", return_value=b"Native hover")
        self.formatter = self.format_patch.start()
        self.addCleanup(self.format_patch.stop)

    def test_count_is_omitted_and_identity_is_cached(self):
        self.assertEqual(self.hover.text(("network", 0)), b"Native hover")
        self.row.stored = 32
        self.assertEqual(self.hover.text(("network", 0)), b"Native hover")
        self.formatter.assert_called_once_with(self.key)
        self.row.key = ResourceKey.item("minecraft:stone", 2)
        self.hover.text(("network", 0))
        self.assertEqual(self.formatter.call_count, 2)
        self.assertEqual(self.hover.text(("network", -1)), b"")

    def test_pattern_marker_follows_the_row_without_reformatting(self):
        self.row.stored = 0
        self.row.has_pattern = True
        self.assertEqual(self.hover.text(("network", 0)), "Native hover\n§e[可制造]".encode("utf-8"))
        self.row.has_pattern = False
        self.assertEqual(self.hover.text(("network", 0)), b"Native hover")
        self.formatter.assert_called_once_with(self.key)

    def test_bag_cells_never_show_the_marker(self):
        self.screen.backpack.entry.return_value = SimpleNamespace(key=self.key, count=lambda: 3)
        self.assertEqual(terminal_hover.hovered_stack(self.screen, ("bag", 27)), (self.key, False))
        self.assertEqual(self.hover.text(("bag", 27)), b"Native hover")

    def test_device_cells_use_aux(self):
        self.screen.craft = SimpleNamespace(cell=lambda i: {"name": "minecraft:wool", "aux": 14, "count": 2})
        key, craftable = terminal_hover.hovered_stack(self.screen, ("grid", 0))
        self.assertEqual((key.aux, craftable), (14, False))

    def test_touch_drag_and_modal_hide_the_tooltip(self):
        self.router.desktop = False
        self.assertEqual(self.hover.text(("network", 0)), b"")
        self.router.desktop = True
        self.router.drag = {"preview": {}}
        self.assertEqual(self.hover.text(("network", 0)), b"")
        self.router.drag = None
        self.router.settling_drag = {"preview": {}}
        self.assertEqual(self.hover.text(("network", 0)), b"")
        self.router.settling_drag = None
        self.modal = object()
        self.assertEqual(self.hover.text(("network", 0)), b"")
        self.formatter.assert_not_called()

    def test_custom_tips_are_appended_once(self):
        key = ResourceKey.item("minecraft:stone", 0, custom_tips="Extra line")
        self.row.key = key
        self.assertEqual(self.hover.text(("network", 0)), b"Native hover\nExtra line")

    def test_touch_selection_shows_the_hover_text_in_the_bottom_bubble(self):
        controls = {path: Mock() for path in (terminal_hover.DETAILS_PATH, terminal_hover.DETAILS_IMAGE_PATH,
                                              terminal_hover.DETAILS_LABEL_PATH)}
        details = self.router.details
        self.screen.now = 10.0
        self.screen._clock = lambda: self.screen.now
        with patch.object(terminal_hover.slot_layout, "screen_control",
                          side_effect=lambda screen, path: controls[path]):
            details.tick(("network", 0), ("network", self.key))
            controls[terminal_hover.DETAILS_LABEL_PATH].asLabel().SetText.assert_called_with(b"Native hover")
            controls[terminal_hover.DETAILS_PATH].SetVisible.assert_called_with(True)
            self.screen.now += terminal_hover.DETAILS_FADE_SECONDS / 2
            details.tick(None, None)
            alpha = controls[terminal_hover.DETAILS_IMAGE_PATH].SetAlpha.call_args.args[0]
            self.assertAlmostEqual(alpha, 1 - 0.5 ** 5, msg="原版 in_quint 淡出，取消选中不打断")
            self.screen.now += terminal_hover.DETAILS_FADE_SECONDS
            details.tick(None, None)
            controls[terminal_hover.DETAILS_PATH].SetVisible.assert_called_with(False)
            details.notice({"reasons": [{"detail": u"背包槽已变化"}]})
            controls[terminal_hover.DETAILS_LABEL_PATH].asLabel().SetText.assert_called_with(
                u"背包槽已变化".encode("utf-8"))
        self.assertEqual(self.formatter.call_count, 1)


class TestDurability(unittest.TestCase):
    """耐久条（原版 `common.durability_bar`）的三项数据。"""

    def setUp(self):
        self.addCleanup(stack_size.reset)
        stack_size.reset()
        stack_size._CACHE[("minecraft:iron_pickaxe", 0)] = (1, 1, 250)
        stack_size._CACHE[("minecraft:stone", 0)] = (2, 64, 0)

    def test_non_tools_and_full_tools_hide_the_bar(self):
        self.assertEqual(stack_size.durability(None), (False, 0, 0))
        self.assertEqual(stack_size.durability(ResourceKey.item("minecraft:stone")), (False, 0, 0))
        self.assertEqual(stack_size.durability(ResourceKey.item("minecraft:iron_pickaxe")), (False, 250, 250))

    def test_damage_comes_from_aux_unless_identity_carries_durability(self):
        self.assertEqual(stack_size.durability(ResourceKey.item("minecraft:iron_pickaxe", 40)), (True, 250, 210))
        worn = ResourceKey.item("minecraft:iron_pickaxe", 40, durability=100)
        self.assertEqual(stack_size.durability(worn), (True, 250, 100))

    def test_held_item_bar_follows_the_cursor_item(self):
        screen = SimpleNamespace(list_view=None, top_modal=lambda: None, _clock=lambda: 1, backpack=Mock())
        router = terminal_input.TerminalInput(screen)
        router.cursor_icon, router.cursor_count = Mock(), Mock()
        router.show_cursor(ResourceKey.item("minecraft:iron_pickaxe", 40), 1)
        self.assertEqual(router.durability_bar(), (210, 250))
        router.show_cursor(ResourceKey.item("minecraft:stone"), 3)
        self.assertIsNone(router.durability_bar())
        router.show_cursor(None, 0)
        self.assertIsNone(router.durability_bar())


if __name__ == "__main__":
    unittest.main()
