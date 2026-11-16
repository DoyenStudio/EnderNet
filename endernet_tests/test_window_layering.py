# coding=utf-8
"""Nested pickers must cover the list's slots and receive their pointer input."""

import unittest

from ui_terminal_templates import UiControls


# MCDK 3.10 runtime: common.scrolling_panel's scrolling_view_port adds 1;
# its other intermediate containers add 0.
SCROLL_VIEWPORT_LAYER = 1


class WindowLayeringTests(unittest.TestCase):
    def test_list_slots_stay_below_picker_backdrop_in_every_host(self):
        ui = UiControls()
        covered = set()
        for reference in ui.definitions:
            if not reference.endswith(".content"):
                continue
            children = ui.children(ui.template(reference))
            for kind in ("item", "fluid"):
                list_name = "endernet_%s_list_window" % kind
                picker_name = "endernet_select_%s_window" % kind
                if list_name not in children:
                    continue
                covered.add((reference, kind))
                with self.subTest(host=reference, kind=kind):
                    listing = children[list_name]
                    picker = children[picker_name]
                    scroll = ui.child(listing, "scroll_view")
                    grid = ui.template(ui.substitute(
                        scroll[0]["$scrolling_content"], scroll[1]))
                    slot = ui.template(ui.substitute(
                        grid[0]["grid_item_template"], grid[1]))
                    slot_children = ui.children(slot)
                    slot_layer = (listing[0]["layer"] + scroll[0]["layer"]
                                  + SCROLL_VIEWPORT_LAYER + grid[0].get("layer", 0)
                                  + slot[0].get("layer", 0))
                    backdrop = ui.child(picker, "modal_backdrop")
                    backdrop_layer = picker[0]["layer"] + backdrop[0]["layer"]
                    for name in ("bg", "item_renderer", "selected_frame",
                                 "durability", "item_count", "interact_btn"):
                        if name in slot_children:
                            self.assertLess(
                                slot_layer + slot_children[name][0].get("layer", 0),
                                backdrop_layer,
                                "%s/%s must stay below the picker" % (list_name, name))
                    self.assertGreater(scroll[0]["layer"], ui.child(listing, "bg")[0]["layer"])
        self.assertEqual(covered, {
            ("endernet_bus_item_input_io.content", "item"),
            ("endernet_bus_item_output_io.content", "item"),
            ("endernet_template_terminal_panel.content", "item"),
            ("endernet_bus_fluid_input_io.content", "fluid"),
            ("endernet_bus_fluid_output_io.content", "fluid"),
        })


if __name__ == "__main__":
    unittest.main()
