# coding=utf-8
"""Batch protocol and authoritative placements, including interrupted writes."""
import copy
import unittest
from unittest.mock import patch

from test_crafting_handlers import Fixture, PLANKS, STICK
from test_template_handlers import Fixture as TemplateFixture
from test_terminal_cursor import CursorPrimitives
from endernet.common.resources.item_codec import item_dict_from_key
from endernet.common.protocol import TerminalCommand
from endernet.server.adapters.containers.engine import EngineInventoryProvider, build_stack_limit


class BatchMixin:
    def install_inventory(self):
        self.engine = CursorPrimitives()
        self.provider = EngineInventoryProvider(self.engine, build_stack_limit(self.engine))
        self.service.inventories = self.provider
        self.hand = self.provider.cursor_of("p1")
        self.bag = self.provider.endpoint_of("p1")
        self.hand.write_slot(0, item_dict_from_key(PLANKS, 7))

    def batch(self, *placements):
        entries = []
        for area, cell, count in placements:
            entry = dict(target=area, cell=cell, count=count)
            if area == "bag":
                view = self.bag.slot_view(cell)
                entry.update(slot_version=view["version"], fingerprint=view["canonical"])
            else:
                entry["slot_version"] = (self.grid if area == "grid" else self.state).revision
            entries.append(entry)
        return {"action": "slot_batch", "config": {
            "revision": self.hand.snapshot()["revision"], "placements": entries}}


class TestPhysicalBatch(BatchMixin, Fixture):
    def setUp(self):
        super().setUp()
        self.install_inventory()

    def test_bag_and_grid_share_one_cursor_and_advance_device_revision(self):
        payload = self.batch(("bag", 9, 2), ("grid", 0, 2), ("grid", 3, 2))
        original = copy.deepcopy(payload)
        result = self.send(payload)
        self.assertEqual(result.status, "ok", result.to_dict())
        self.assertEqual(result.payload["cursor"]["count"], 1)
        self.assertEqual([x["count"] for x in result.payload["placements"]], [2, 2, 2])
        self.assertEqual(self.bag.adapter.count_of(PLANKS), 2)
        self.assertEqual((self.grid.count_at(0), self.grid.count_at(3)), (2, 2))
        self.assertEqual(result.payload["grid_revision"], self.grid.revision)
        self.assertEqual(payload, original)
        self.assertFalse(self.send(payload).is_applied())
        self.assertEqual(self.hand.snapshot()["count"], 1)

    def test_stale_last_slot_rejects_before_the_first_write(self):
        for area in ("bag", "grid"):
            with self.subTest(area=area):
                payload = self.batch(("bag", 1, 2), (area, 2, 2))
                payload["config"]["placements"][-1]["slot_version"] += 1
                self.assertFalse(self.send(payload).is_applied())
                self.assertEqual(self.hand.snapshot()["count"], 7)
                self.assertIsNone(self.engine.slots[1])

    def test_partial_capacity_stops_before_the_next_slot(self):
        self.engine.slots[2] = item_dict_from_key(PLANKS, 63)
        result = self.send(self.batch(("bag", 1, 2), ("bag", 2, 2), ("bag", 3, 2)))
        self.assertEqual(result.status, "partial")
        self.assertEqual([x["count"] for x in result.payload["placements"]], [2, 1])
        self.assertEqual(self.hand.snapshot()["count"], 4)
        self.assertIsNone(self.engine.slots[3])

    def test_occupied_slot_reports_committed_prefix_and_stops(self):
        self.engine.slots[2] = item_dict_from_key(STICK, 1)
        result = self.send(self.batch(("bag", 1, 2), ("bag", 2, 2), ("bag", 3, 2)))
        self.assertEqual(result.status, "partial")
        self.assertEqual([x["count"] for x in result.payload["placements"]], [2, 0])
        self.assertEqual(self.hand.snapshot()["count"], 5)
        self.assertIsNone(self.engine.slots[3])

    def test_uncertain_write_keeps_journal_and_does_not_continue_or_replay(self):
        for raises in (False, True):
            with self.subTest(raises=raises):
                self.install_inventory()
                original = self.engine.set_player_item
                attempts = []
                def write(owner, slot, data):
                    attempts.append(slot)
                    if slot == 2:
                        if raises:
                            raise IOError("lost acknowledgement")
                        return True
                    return original(owner, slot, data)
                self.engine.set_player_item = write
                payload = self.batch(("bag", 1, 2), ("bag", 2, 2), ("bag", 3, 2))
                result = self.send(payload)
                self.assertEqual(result.status, "partial")
                self.assertEqual(attempts, [1, 2])
                self.assertEqual(self.engine.record["pending"]["slot"], 2)
                self.assertFalse(self.send(payload).is_applied())
                self.assertEqual(attempts, [1, 2])

    def test_malformed_tail_is_rejected_before_writing_anything(self):
        payload = self.batch(("bag", 1, 2), ("bag", 2, 2))
        payload["config"]["placements"][-1]["count"] = True
        self.assertFalse(self.send(payload).is_applied())
        self.assertEqual(self.hand.snapshot()["count"], 7)
        self.assertIsNone(self.engine.slots[1])


class TestTemplateBatch(BatchMixin, TemplateFixture):
    def setUp(self):
        super().setUp()
        self.install_inventory()

    def test_virtual_cells_copy_original_hand_after_real_stack_is_consumed(self):
        result = self.send(self.batch(("bag", 1, 7), ("source", 0, 7), ("source", 1, 7)))
        self.assertEqual(result.status, "ok", result.to_dict())
        self.assertEqual(self.hand.snapshot()["count"], 0)
        self.assertEqual(self.bag.adapter.count_of(PLANKS), 7)
        self.assertEqual(result.payload["template_revision"], self.state.revision)
        for cell in (0, 1):
            self.assertEqual(self.config.cell("source", cell)[1], 7)

    def test_locked_output_stops_without_creating_physical_items(self):
        result = self.send(self.batch(("source", 0, 2), ("outputs", 0, 2), ("bag", 1, 2)))
        self.assertEqual(result.status, "partial", result.to_dict())
        self.assertEqual(self.hand.snapshot()["count"], 7)
        self.assertEqual(self.bag.adapter.count_of(PLANKS), 0)


class TestBatchWire(unittest.TestCase):
    def config(self):
        return {"revision": 3, "placements": [dict(target="bag", cell=9, count=2,
                                                  slot_version=1, fingerprint="")]}

    def test_round_trip_uses_one_normal_command_envelope(self):
        command = TerminalCommand("drag-1", "slot_batch", config=self.config())
        decoded = TerminalCommand.from_dict(command.to_dict())
        self.assertEqual(decoded.payload(), command.payload())

    def test_invalid_batch_is_rejected_even_without_debug(self):
        from endernet.common import settings
        wire = TerminalCommand("drag-1", "slot_batch", config=self.config()).to_dict()
        invalid = [None, {}, {"revision": True, "placements": []}]
        for field, value in (("target", "network"), ("count", 0), ("count", True),
                             ("cell", 36), ("slot_version", -1), ("player_id", "other")):
            config = self.config()
            config["placements"][0][field] = value
            invalid.append(config)
        for size in (0, 2, 65):
            config = self.config()
            config["placements"] *= size
            invalid.append(config)
        with patch.object(settings, "DEBUG", False):
            for config in invalid:
                with self.subTest(config=config), self.assertRaises((ValueError, TypeError)):
                    TerminalCommand.from_dict(dict(wire, config=config))
