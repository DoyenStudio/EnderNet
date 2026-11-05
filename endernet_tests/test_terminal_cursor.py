# coding=utf-8
"""Real storage/transfer handlers, with only the engine primitives replaced."""
import copy

from test_crafting_handlers import Fixture, PLANKS, STICK, STICK_RECIPE
from test_containers_engine import FakePlayerPrimitives
from endernet.common.item_codec import item_dict_from_key, item_key_from_dict
from endernet.common.resource_key import ResourceKey
from endernet.server.adapters.containers_engine import EngineInventoryProvider


class CursorPrimitives(FakePlayerPrimitives):
    def __init__(self):
        super().__init__()
        self.record = {}
        self.drops = []

    def cursor_record(self, owner):
        return copy.deepcopy(self.record)

    def save_cursor(self, owner, record):
        self.record = copy.deepcopy(record)
        return True

    def drop_cursor(self, owner, data):
        self.drops.append(copy.deepcopy(data))
        return True


class TestTerminalCursor(Fixture):
    def setUp(self):
        super().setUp()
        self.engine = CursorPrimitives()
        self.provider = EngineInventoryProvider(self.engine)
        self.service.inventories = self.provider
        self.hand = self.provider.cursor_of("p1")
        self.bag = self.provider.endpoint_of("p1")
        self.sequence = 0
        self.recipes.recipes_for_input = lambda key: [STICK_RECIPE] if key == PLANKS else []

    def click(self, region, index, gesture, key=None, revision=None):
        self.sequence += 1
        options = {"gesture": gesture, "revision": self.hand.snapshot()["revision"]
                   if revision is None else revision}
        command = {"action": "slot_click", "target": region, "cell": index, "config": options}
        if key:
            command["key"] = key.to_dict()
        if region == "bag":
            view = self.bag.slot_view(index)
            command.update(slot=index, slot_version=view["version"])
            options["fingerprint"] = view["canonical"]
        elif region == "grid":
            command["slot_version"] = self.grid.revision
        return self.send(command, "click-%d" % self.sequence)

    def ok(self, *args, **kwargs):
        result = self.click(*args, **kwargs)
        self.assertTrue(result.is_applied(), result.to_dict())
        return result.payload

    def test_right_half_single_swap_and_close_preserve_identity(self):
        item = ResourceKey.item("minecraft:stone", custom_tips="unique")
        self.engine.slots[1] = item_dict_from_key(item, 5)
        self.engine.slots[2] = item_dict_from_key(STICK, 1)
        self.ok("bag", 1, "right")
        self.assertEqual(self.hand.snapshot()["count"], 3)
        self.ok("bag", 0, "right")
        self.assertEqual(self.hand.snapshot()["count"], 2)
        self.ok("bag", 2, "left")
        self.assertEqual(self.hand.snapshot()["key"], STICK.to_dict())
        self.assertEqual(item_key_from_dict(self.engine.slots[2]), item)
        self.assertTrue(self.hand.return_to_player(self.bag.adapter))
        self.assertEqual(self.hand.snapshot()["count"], 0)
        self.assertEqual(self.bag.adapter.count_of(item), 5)
        self.assertEqual(self.bag.adapter.count_of(STICK), 1)

    def test_network_extraction_and_single_deposit_reject_stale_cursor(self):
        before = self.service.query(PLANKS)["stored"]
        available = self.service.simulate_extract(PLANKS, 64)["accepted"]
        first = self.ok("network", 0, "right", PLANKS)
        half = (available + 1) // 2
        self.assertEqual(first["receipt"]["requested"], half)
        moved = first["receipt"]["moved"]
        self.assertGreater(moved, 0)
        self.assertEqual(first["cursor"]["count"], moved)
        self.assertFalse(self.click("network", 0, "left", PLANKS, 0).is_applied())
        self.ok("network", 0, "one_in")
        self.assertEqual(self.service.query(PLANKS)["stored"], before - moved + 1)

    def test_hand_placed_grid_matches_and_crafts_to_cursor(self):
        self.engine.slots[9] = item_dict_from_key(PLANKS, 2)
        self.ok("bag", 9, "left")
        self.ok("grid", 0, "right")
        self.ok("grid", 3, "right")
        self.assertEqual(self.grid.recipe, STICK_RECIPE.digest())
        result = self.send({"action": "craft_take", "target": "cursor",
                            "slot_version": self.grid.revision,
                            "config": {"revision": self.hand.snapshot()["revision"]}}, "craft")
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual(self.hand.snapshot()["key"], STICK.to_dict())

    def test_full_bag_drops_once_even_if_closed_again(self):
        self.hand.write_slot(0, item_dict_from_key(STICK, 7))
        self.engine.slots[:] = [item_dict_from_key(PLANKS, 64) for _ in range(36)]
        self.assertTrue(self.hand.return_to_player(self.bag.adapter))
        self.assertTrue(self.hand.return_to_player(self.bag.adapter))
        self.assertEqual([7], [x["count"] for x in self.engine.drops])

    def test_recovery_finishes_either_side_of_an_interrupted_write(self):
        for written in (False, True):
            before = item_dict_from_key(PLANKS, 4)
            after = item_dict_from_key(PLANKS, 2)
            self.engine.record = {"revision": 2, "item": None, "pending": {
                "area": "bag", "identity": "p1", "slot": 3, "before": before,
                "after": after, "hand_after": after}}
            self.engine.slots[3] = after if written else before
            self.assertTrue(self.hand.return_to_player(self.bag.adapter))
            self.assertEqual(self.bag.adapter.count_of(PLANKS), 4)
            self.assertEqual(self.hand.snapshot()["count"], 0)

    def test_quick_move_targets_player_slots_and_leaves_hand_available(self):
        payload = self.ok("network", 0, "quick", PLANKS)
        self.assertEqual(payload["bag_slots"], [0])
        self.assertGreater(self.engine.slots[0]["count"], 0)
        self.assertEqual(self.hand.snapshot()["count"], 0)

    def test_pocket_move_uses_grid_revision_and_original_bag_slot(self):
        self.engine.slots[9] = item_dict_from_key(PLANKS, 3)
        before = self.bag.slot_view(9)
        fields = {"action": "grid_move", "target": "bag", "cell": 8,
                  "slot": 9, "slot_version": before["version"], "key": PLANKS.to_dict(),
                  "config": {"placing": True, "grid_revision": self.grid.revision,
                             "fingerprint": before["canonical"]}}
        self.assertTrue(self.send(fields).is_applied())
        self.assertIsNone(self.engine.slots[9])
        self.assertEqual(self.grid.count_at(8), 3)
        self.assertFalse(self.send(fields, "stale").is_applied())
        self.assertEqual(self.hand.snapshot()["count"], 0)

    def test_interrupted_drop_is_not_replayed_as_a_second_entity(self):
        self.hand.write_slot(0, item_dict_from_key(STICK, 3))
        self.engine.slots[:] = [item_dict_from_key(PLANKS, 64) for _ in range(36)]
        def interrupted(owner, data):
            self.engine.drops.append(data)
            raise IOError("lost acknowledgement")
        self.engine.drop_cursor = interrupted
        with self.assertRaises(IOError):
            self.hand.return_to_player(self.bag.adapter)
        self.assertFalse(self.hand.return_to_player(self.bag.adapter))
        self.assertEqual(len(self.engine.drops), 1)
        self.assertEqual(self.engine.record["pending"]["area"], "drop")
