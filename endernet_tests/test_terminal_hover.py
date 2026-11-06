# coding=utf-8
"""Hover identity, lifecycle and stationary-pointer regression coverage."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import testsupport  # noqa: F401
from endernet.common.resource_key import ResourceKey
from endernet.client.ui import terminal_hover, terminal_input


class TestTerminalHover(unittest.TestCase):
    def setUp(self):
        self.key = ResourceKey.item("minecraft:stone", 1, user_data={"display": {"Name": "named"}})
        self.row = SimpleNamespace(key=self.key, stored=64, name="Stone", has_pattern=False)
        self.view = SimpleNamespace(rows=[self.row], has_item=lambda i: i == 0)
        self.screen = SimpleNamespace(list_view=self.view, top_modal=lambda: None,
                                      _clock=lambda: 1, backpack=Mock())
        self.router = terminal_input.TerminalInput(self.screen)
        self.screen.slot_input = self.router
        self.router.desktop = True
        self.control = Mock()
        self.control_patch = patch.object(terminal_hover.slot_layout, "control", return_value=self.control)
        self.format_patch = patch.object(terminal_hover, "formatted_hover", return_value=b"Native hover")
        self.control_patch.start()
        self.formatter = self.format_patch.start()
        self.addCleanup(self.control_patch.stop)
        self.addCleanup(self.format_patch.stop)

    def test_count_is_omitted_and_identity_replacement_refreshes(self):
        hover = self.router.hover
        hover.update(("network", 0))
        self.row.stored = 32
        hover.update(("network", 0))
        self.control.SetPropertyBag.assert_called_once_with({"#hover_text": b"Native hover"})
        self.formatter.assert_called_once_with(self.key)
        self.row.key = ResourceKey.item("minecraft:stone", 2)
        hover.update(("network", 0))
        self.assertEqual(self.formatter.call_count, 2)
        hover.update(("network", -1))
        self.control.SetVisible.assert_called_with(False)

    def test_stationary_pointer_rehits_after_scroll_and_notice_does_not_block_hover(self):
        api = SimpleNamespace(GetFocusPos=lambda: (50, 50))
        self.router.notice_until = 10
        with patch.object(terminal_input.slot_layout, "slot_at", side_effect=[("network", 0), None]) as hit:
            self.router.update_info(api, self.view)
            self.screen._clock = lambda: 2
            self.router.update_info(api, self.view)
        self.assertEqual(hit.call_count, 2)
        self.control.SetVisible.assert_called_with(False)

    def test_modal_hides_before_poll_deadline_and_reset_hides(self):
        self.router.hover.update(("network", 0))
        self.router.info_time = 99
        self.screen.top_modal = lambda: object()
        self.router.update_info(Mock(), self.view)
        self.assertIsNone(self.router.hover.shown)
        self.router.hover.update(("network", 0))
        self.router.reset()
        self.assertIsNone(self.router.hover.shown)

    def test_bag_identity_and_zero_stock_pattern_are_preserved(self):
        self.screen.backpack.entry.return_value = SimpleNamespace(key=self.key, count=lambda: 3)
        self.assertEqual(terminal_hover.hovered_stack(self.screen, ("bag", 27)), (self.key, False))
        self.row.stored = 0
        self.row.has_pattern = True
        self.router.hover.update(("network", 0))
        self.control.SetVisible.assert_called_with(True)
        self.assertEqual(self.router.hover.shown, "Native hover\n§e[可制造]".encode("utf-8"))
        self.router.hover.update(("bag", 27))
        self.assertEqual(self.router.hover.shown, b"Native hover")

    def test_pattern_changes_refresh_marker_without_reformatting_item(self):
        hover = self.router.hover
        hover.update(("network", 0))
        self.row.has_pattern = True
        hover.update(("network", 0))
        self.assertEqual(hover.shown, "Native hover\n§e[可制造]".encode("utf-8"))
        self.row.has_pattern = False
        hover.update(("network", 0))
        self.assertEqual(hover.shown, b"Native hover")
        self.formatter.assert_called_once_with(self.key)

    def test_device_cells_use_aux_and_drag_hides_tooltip(self):
        self.screen.craft = SimpleNamespace(cell=lambda i: {"name": "minecraft:wool", "aux": 14, "count": 2})
        key, craftable = terminal_hover.hovered_stack(self.screen, ("grid", 0))
        self.assertEqual((key.aux, craftable), (14, False))
        self.router.hover.update(("network", 0))
        self.router.drag = {"preview": {}}
        self.router.info_time = 99
        self.router.update_info(Mock(), self.view)
        self.assertIsNone(self.router.hover.shown)

    def test_touch_selection_does_not_show_mouse_tooltip(self):
        self.router.desktop = False
        self.screen.backpack.selected = 0
        self.screen.backpack.entry.return_value = None
        self.router.update_info(Mock(), self.view)
        self.formatter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
