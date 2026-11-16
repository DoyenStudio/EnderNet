# coding=utf-8
"""Production screens keep native slots and reject stale closed-screen updates."""
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

import testsupport  # noqa: F401
from endernet.client.channels.production import ProductionCache
from endernet.client.ui.shared import production_gauges
from endernet.client.ui.shared.production_gauges import (
    ProductionGauges, energy_display, format_energy, fraction, set_hover_text,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def snapshot(session="one", opened=True, pos=None):
    return {"session": session, "opened": opened, "device_id": "endernet:resonance_inscriber",
            "dimension": 0, "pos": pos or [1, 2, 3], "energy": 100, "progress": 0}


class ProductionCacheTests(unittest.TestCase):
    def test_open_is_claimed_once_and_poll_updates_existing_screen(self):
        cache = ProductionCache()
        self.assertTrue(cache.offer(snapshot()))
        self.assertEqual(cache.take("endernet:resonance_inscriber"), "one")
        self.assertIsNone(cache.take("endernet:resonance_inscriber"))
        newer = snapshot(opened=False)
        newer["progress"] = 9
        self.assertTrue(cache.offer(newer))
        self.assertEqual(cache.current["one"]["progress"], 9)
        self.assertIsNone(cache.take("endernet:resonance_inscriber"))

    def test_delayed_update_cannot_reopen_closed_screen(self):
        cache = ProductionCache()
        cache.offer(snapshot())
        cache.close("one")
        self.assertFalse(cache.offer(snapshot(opened=False)))
        self.assertFalse(cache.current)
        self.assertFalse(cache.pending)

    def test_new_open_before_claim_replaces_old_identity(self):
        cache = ProductionCache()
        cache.offer(snapshot())
        cache.offer(snapshot(session="two", pos=[7, 8, 9]))
        self.assertNotIn("one", cache.current)
        self.assertEqual(cache.take("endernet:resonance_inscriber"), "two")
        self.assertEqual(cache.current["two"]["pos"], [7, 8, 9])

    def test_unrelated_device_and_malformed_positions_rejected(self):
        cache = ProductionCache()
        wrong = snapshot()
        wrong["device_id"] = "endernet:dos_network_endpoint"
        self.assertFalse(cache.offer(wrong))
        wrong = snapshot()
        wrong["pos"] = [1, 2]
        self.assertFalse(cache.offer(wrong))


def all_controls(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            for result in all_controls(child):
                yield result
    elif isinstance(value, list):
        for child in value:
            for result in all_controls(child):
                yield result


class ProductionScreenTests(unittest.TestCase):
    def test_machine_slots_use_exact_native_container_indices(self):
        for kind, count in (("charger", 2), ("inscriber", 4), ("generator", 1)):
            relative = "ui/endernet/production/resonance_%s.json" % kind
            with open(os.path.join(ROOT, "endernet_rp", relative), encoding="utf-8") as source:
                data = json.load(source)
            nodes = list(all_controls(data))
            indices = [node["$container_slot_index"] for node in nodes if "$container_slot_index" in node]
            self.assertEqual(sorted(indices), list(range(count)))
            bindings = {node.get("binding_name") for node in nodes}
            self.assertNotIn("#EnderNetProduction.energy", bindings)
            self.assertNotIn("#EnderNetProduction.progress", bindings)
            self.assertNotIn("#EnderNetProduction.mode", bindings)
            if kind == "inscriber":
                self.assertIn("#EnderNetProduction.status", bindings)
            else:
                self.assertNotIn("#EnderNetProduction.energy", bindings)
                self.assertNotIn("#EnderNetProduction.status", bindings)
            for node in nodes:
                if any(binding.get("binding_name", "").startswith("#EnderNetProduction.")
                       and binding.get("binding_name_override") == "#text"
                       for binding in node.get("bindings", [])):
                    self.assertEqual(node.get("text"), "#text")
            self.assertNotIn("#EnderNetDeviceStatus.popup_visible", bindings)
            if kind == "inscriber":
                buttons = {node.get("$pressed_button_name") for node in nodes}
                for mode in ("grind", "press", "assemble"):
                    self.assertIn("#EnderNetProduction." + mode, buttons)


class ProductionGaugeTests(unittest.TestCase):
    def test_horizontal_arrow_updates_and_retains_paused_progress(self):
        class Control(object):
            def __init__(self):
                self._vars = {}
                self.clips = []

            def __getitem__(self, key):
                return self

            def asImage(self):
                return self

            def GetSize(self):
                return (10, 52)

            def SetFullSize(self, *args):
                pass

            def SetSprite(self, *args):
                pass

            def SetSpriteColor(self, *args):
                pass

            def SetPropertyBag(self, *args):
                pass

            def SetSpriteClipRatio(self, direction, ratio):
                self.clips.append((direction, ratio))

        class Screen(object):
            def GetElement(self, path):
                return Control()

        define = types.ModuleType("endernet_scripts.tooldelta.define")
        define.UICtrlPosData = lambda *args, **kwargs: kwargs
        with patch.object(production_gauges, "__package__", "endernet_scripts.endernet.client.ui.shared"), \
                patch.dict(sys.modules, {define.__name__: define}):
            for direction in ("fromRightToLeft", "fromTopToBottom"):
                gauge = ProductionGauges(Screen(), "/bg", progress_direction=direction)
                data = {"energy": 16000, "capacity": 32000, "duration": 80, "progress": 0}
                gauge.update(data)
                data["progress"] = 20
                gauge.update(data)
                data.update(energy=0, status="no_power")
                gauge.update(data)
                self.assertEqual(gauge.previous_progress, 0.25)
                data["progress"] = 80
                gauge.update(data)
                data["progress"] = 0
                gauge.update(data)
                self.assertEqual(gauge.indicator_fill.clips,
                                 [(direction, value) for value in (1.0, 0.75, 0.0, 1.0)])

    def test_tiled_energy_fill_preserves_segments_and_color_thresholds(self):
        self.assertEqual(energy_display(0, 32000, 52), (0.0, 2))
        self.assertEqual(energy_display(1, 32000, 52), (3.0 / 52, 2))
        self.assertEqual(energy_display(32000, 32000, 52), (51.0 / 52, 0))
        self.assertEqual(energy_display(64000, 32000, 52), (51.0 / 52, 0))
        self.assertEqual(energy_display(1600, 32000, 52)[1], 1)
        self.assertEqual(energy_display(4800, 32000, 52)[1], 0)
        self.assertEqual(energy_display(1, 32000, 52, generator=True)[1], 0)
        self.assertEqual(energy_display(32, 0, 52)[0], 0.0)

    def test_progress_clamps_and_keeps_pause_ratio(self):
        self.assertEqual(fraction(800, 1600), 0.5)
        self.assertEqual(fraction(20, 80), 0.25)
        self.assertEqual(fraction(2, 0), 0.0)
        self.assertEqual(fraction(-1, 80), 0.0)
        self.assertEqual(fraction(81, 80), 1.0)
        self.assertEqual(format_energy(32), "32 FE")
        self.assertEqual(format_energy(32000), "32.00 kFE")
        self.assertEqual(format_energy(64, "EN"), "64 EN")
        self.assertEqual(format_energy(32000, "EN"), "32.00 kEN")

    def test_gauges_open_without_removed_title_led_or_static_slot_frames(self):
        class Control(object):
            def __getitem__(self, key):
                return self

            def asImage(self):
                return self

        class Screen(object):
            def __init__(self):
                self.requested = []

            def GetElement(self, path):
                self.requested.append(path)
                return Control()

        for generator, indicator in ((True, "flame"), (False, "progress")):
            screen = Screen()
            ProductionGauges(screen, "/bg", generator)
            self.assertEqual(screen.requested, ["/bg/main/power_bar", "/bg/main/" + indicator])

    def test_hover_text_updates_mouse_and_touch_and_skips_unchanged_text(self):
        class Control(object):
            def __init__(self):
                self._vars = {}
                self.children = {}
                self.writes = []

            def __getitem__(self, key):
                if key not in self.children:
                    self.children[key] = Control()
                return self.children[key]

            def SetPropertyBag(self, value):
                self.writes.append(value)

        control = Control()
        set_hover_text(control, "32 FE")
        set_hover_text(control, "32 FE")
        self.assertEqual(set(control.children), {"info/hover/hover_text", "info/pressed/hover_text"})
        for child in control.children.values():
            self.assertEqual(child.writes, [{"#hover_text": "32 FE"}])
        set_hover_text(control, "64 FE")
        for child in control.children.values():
            self.assertEqual(len(child.writes), 2)


if __name__ == "__main__":
    unittest.main()
