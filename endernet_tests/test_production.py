# coding=utf-8
"""Production rules: conserved material, resumed progress and shared FE budget."""

import copy
import json
import unittest

import testsupport  # noqa: F401
from endernet.common.materials import MACHINE_RECIPES
from endernet.server.production.machine import (
    ProductionMachine, CHARGER, INSCRIBER, GENERATOR, STATE_FIELD, FUEL_TICKS,
    plain_item, item_count, same_item,
)


class Saved(dict):
    def __getitem__(self, key):
        return copy.deepcopy(self.get(key))


class Slots(object):
    def __init__(self, size):
        self.items = [None] * size
        self.fail_slot = None

    def read(self, slot):
        return copy.deepcopy(self.items[slot])

    def write(self, slot, data):
        if slot == self.fail_slot:
            return False
        self.items[slot] = copy.deepcopy(data) if item_count(data) else None
        return True


class Tx(object):
    def __init__(self):
        self.writes = {}
        self.guards = []
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def guard(self, function):
        self.guards.append(function)

    def commit(self):
        if not all(f() for f in self.guards):
            raise ValueError("invalidated")
        for store, amount in self.writes.items():
            store.amount = amount
        for store in self.writes:
            if store.persist:
                store.persist()
        self.committed = True


class Energy(object):
    def __init__(self, capacity, amount=0, persist=None, owner=None):
        self.capacity = capacity
        self.amount = amount
        self.persist = persist
        self.manager = None
        self.storage_ids = frozenset([self])

    def get_amount(self, tx=None):
        return tx.writes.get(self, self.amount) if tx else self.amount

    def get_capacity(self):
        return self.capacity

    def insert(self, count, tx):
        current = self.get_amount(tx)
        moved = min(count, self.capacity - current)
        tx.writes[self] = current + moved
        return moved

    def extract(self, count, tx):
        current = self.get_amount(tx)
        moved = min(count, current)
        tx.writes[self] = current - moved
        return moved


class FakeApi(object):
    TransactionError = ValueError

    def create_energy_storage(self, capacity, amount=0, **kwargs):
        return Energy(capacity, amount, **kwargs)

    def energy_view(self, store, **kwargs):
        return store

    def open_transaction(self):
        return Tx()

    def move_energy(self, source, target, maximum, tx):
        count = min(maximum, source.get_amount(tx), target.get_capacity() - target.get_amount(tx))
        moved = source.extract(count, tx)
        self_count = target.insert(moved, tx)
        if self_count != moved:
            raise ValueError("test transfer mismatch")
        return moved


def make_machine(device_id, items=None, amount=32000, api=None, entity=None):
    size = {CHARGER: 2, INSCRIBER: 4, GENERATOR: 1}[device_id]
    slots = Slots(size)
    if items:
        for index, item in enumerate(items):
            slots.items[index] = item
    saved = entity if entity is not None else Saved({STATE_FIELD: {"energy": amount}})
    machine = ProductionMachine(device_id, saved, slots, api or FakeApi(), MACHINE_RECIPES)
    return machine, slots, saved


class TestProduction(unittest.TestCase):
    def test_native_default_fields_do_not_poison_completed_journal(self):
        class NativeDefaults(Slots):
            def read(self, slot):
                item = super(NativeDefaults, self).read(slot)
                if not item_count(item):
                    return item
                item.update({"canPlaceOn": [], "canDestroy": [], "enchantData": [],
                             "isDiggerItem": False, "durability": 0, "itemId": -1,
                             "customTips": "", "extraId": "", "userData": {},
                             "showInHand": True, "modEnchantData": []})
                return item
        slots = NativeDefaults(2)
        slots.items[0] = plain_item("endernet:resonance_crystal", 1)
        saved = Saved({STATE_FIELD: {"energy": 32000}})
        machine = ProductionMachine(CHARGER, saved, slots, FakeApi(), MACHINE_RECIPES)
        for _ in range(80):
            machine.tick()
        self.assertEqual(machine.status, "complete")
        self.assertEqual(machine.progress, 0)
        self.assertIsNone(machine.journal)
        altered = plain_item("endernet:charged_resonance_crystal", 1)
        altered["userData"] = {"custom": "must-survive"}
        self.assertFalse(same_item(altered, slots.read(1)))

    def test_all_eight_recipes_consume_once_after_eighty_ticks(self):
        for recipe in MACHINE_RECIPES:
            with self.subTest(recipe=recipe.id):
                items = [plain_item(name, count * 2) for name, count in recipe.inputs]
                machine, slots, _ = make_machine(recipe.machine, items)
                machine.mode = recipe.mode
                for _ in range(79):
                    machine.tick()
                self.assertFalse(item_count(slots.items[-1]))
                self.assertEqual(machine.progress, 79)
                machine.tick()
                self.assertEqual(slots.items[-1]["newItemName"], recipe.output)
                self.assertEqual(slots.items[-1]["count"], recipe.count)
                self.assertEqual(machine.energy.get_amount(), 32000 - 2560)
                for index, (_, count) in enumerate(recipe.inputs):
                    self.assertEqual(slots.items[index]["count"], count)

    def test_power_failure_preserves_progress_and_inputs(self):
        machine, slots, _ = make_machine(CHARGER, [plain_item("endernet:resonance_crystal", 1)], 32)
        machine.tick()
        for _ in range(4):
            machine.tick()
        self.assertEqual(machine.progress, 1)
        self.assertEqual(slots.items[0]["count"], 1)
        self.assertEqual(machine.status, "no_power")

    def test_full_output_does_not_spend_power(self):
        machine, slots, _ = make_machine(CHARGER, [plain_item("endernet:resonance_crystal", 1),
                                                  plain_item("endernet:charged_resonance_crystal", 64)])
        machine.tick()
        self.assertEqual(machine.progress, 0)
        self.assertEqual(machine.energy.get_amount(), 32000)
        slots.items[1] = plain_item("minecraft:stone", 1)
        machine.tick()
        self.assertEqual(machine.status, "output_full")

    def test_save_reload_resumes_without_double_consumption(self):
        machine, slots, saved = make_machine(CHARGER, [plain_item("endernet:resonance_crystal", 1)])
        for _ in range(40):
            machine.tick()
        restored = ProductionMachine(CHARGER, saved, slots, FakeApi(), MACHINE_RECIPES)
        for _ in range(40):
            restored.tick()
        self.assertIsNone(slots.items[0])
        self.assertEqual(slots.items[1]["count"], 1)
        self.assertEqual(restored.energy.get_amount(), 32000 - 2560)

    def test_interrupted_slot_commit_recovers_before_accepting_inputs(self):
        machine, slots, saved = make_machine(CHARGER, [plain_item("endernet:resonance_crystal", 1)])
        slots.fail_slot = 1
        for _ in range(80):
            machine.tick()
        self.assertIsNone(slots.items[0])
        self.assertIsNotNone(machine.journal)
        self.assertFalse(machine.accepts(0, plain_item("endernet:resonance_crystal", 1)))
        restored = ProductionMachine(CHARGER, saved, slots, FakeApi(), MACHINE_RECIPES)
        slots.fail_slot = None
        restored.tick()
        self.assertEqual(slots.items[1]["count"], 1)
        self.assertEqual(restored.energy.get_amount(), 32000 - 2560)
        self.assertIsNone(restored.journal)

    def test_foreign_journal_mutation_is_not_overwritten(self):
        machine, slots, _ = make_machine(CHARGER, [plain_item("endernet:resonance_crystal", 1)])
        slots.fail_slot = 1
        for _ in range(80):
            machine.tick()
        slots.items[0] = plain_item("minecraft:diamond", 4)
        slots.fail_slot = None
        machine.tick()
        self.assertEqual(machine.status, "recovery_error")
        self.assertEqual(slots.items[0]["count"], 4)
        self.assertIsNone(slots.items[1])

    def test_uncancellable_manual_removal_fences_failed_commit(self):
        machine, slots, saved = make_machine(CHARGER, [plain_item("endernet:resonance_crystal", 1)])
        slots.fail_slot = 1
        for _ in range(80):
            machine.tick()
        machine.fence_manual_extraction()
        slots.fail_slot = None
        restored = ProductionMachine(CHARGER, saved, slots, FakeApi(), MACHINE_RECIPES)
        restored.tick()
        self.assertEqual(restored.status, "recovery_error")
        self.assertIsNone(slots.items[1])

    def test_failed_journal_save_must_persist_before_retry_writes_slots(self):
        class RefusingSave(Saved):
            refusing = False

            def __setitem__(self, key, value):
                if not self.refusing:
                    super(RefusingSave, self).__setitem__(key, value)

        saved = RefusingSave({STATE_FIELD: {"energy": 32000}})
        machine, slots, _ = make_machine(
            CHARGER, [plain_item("endernet:resonance_crystal", 1)], entity=saved)
        before = machine._items()
        after = [None, plain_item("endernet:charged_resonance_crystal", 1)]
        saved.refusing = True
        with self.assertRaises(IOError):
            machine._commit_slots(before, after, {"progress": 0, "recipe": ""})
        with self.assertRaises(IOError):
            machine.recover()
        self.assertEqual(slots.items, before)
        saved.refusing = False
        self.assertTrue(machine.recover())
        self.assertEqual(slots.items, after)

    def test_journal_persists_when_native_store_rejects_mixed_slot_lists(self):
        class NativeSave(Saved):
            def __setitem__(self, key, value):
                # Observed engine failure: a journal with dict/None slot lists
                # rejects the entire field update and keeps the previous state.
                if isinstance(value.get("journal"), dict):
                    return
                super(NativeSave, self).__setitem__(key, value)

        saved = NativeSave({STATE_FIELD: {"energy": 32000}})
        source = plain_item("endernet:resonance_crystal", 1)
        source["userData"] = {"custom_name": "保留元数据", "number": 7}
        machine, slots, _ = make_machine(CHARGER, [source], entity=saved)
        slots.fail_slot = 1
        for _ in range(80):
            machine.tick()
        record = saved[STATE_FIELD]
        self.assertEqual(record["version"], 2)
        self.assertIsInstance(record["journal"], str)
        decoded = json.loads(record["journal"])
        self.assertEqual(decoded["before"][0]["userData"], source["userData"])
        self.assertIsNone(decoded["after"][0])
        restored = ProductionMachine(CHARGER, saved, slots, FakeApi(), MACHINE_RECIPES)
        slots.fail_slot = None
        restored.tick()
        self.assertEqual(slots.items[1]["count"], 1)
        self.assertIsNone(saved[STATE_FIELD]["journal"])

    def test_version_one_dictionary_journal_migrates_without_replaying_output(self):
        source = plain_item("endernet:resonance_crystal", 1)
        output = plain_item("endernet:charged_resonance_crystal", 1)
        saved = Saved({STATE_FIELD: {"version": 1, "energy": 0, "progress": 80,
                      "journal": {"before": [source, None], "after": [None, output],
                                  "post": {"progress": 0, "recipe": ""}}}})
        machine, slots, _ = make_machine(CHARGER, [None, output], entity=saved)
        self.assertIsInstance(saved[STATE_FIELD]["journal"], str)
        machine.tick()
        self.assertIsNone(machine.journal)
        self.assertEqual(slots.items[1]["count"], 1)

    def test_successful_tick_clears_prior_error_message(self):
        machine, _, _ = make_machine(CHARGER, [plain_item("endernet:resonance_crystal", 1)])
        machine.status = "error"
        machine.message = "production state did not persist"
        machine.tick()
        self.assertEqual(machine.status, "working")
        self.assertEqual(machine.message, "")

    def test_filters_modes_and_manual_input_cancellation(self):
        machine, slots, _ = make_machine(INSCRIBER, [plain_item("minecraft:quartz", 1)])
        self.assertFalse(machine.accepts(3, plain_item("minecraft:quartz", 1)))
        self.assertFalse(machine.removable(0, automatic=True))
        self.assertTrue(machine.removable(3, automatic=True))
        machine.tick()
        self.assertFalse(machine.set_mode("press"))
        slots.items[0] = None
        machine.tick()
        self.assertTrue(machine.set_mode("press"))
        self.assertTrue(machine.accepts(1, plain_item("minecraft:amethyst_shard", 1)))
        self.assertFalse(machine.accepts(0, plain_item("minecraft:amethyst_shard", 1)))

    def test_one_coal_generates_exactly_102400_fe(self):
        machine, slots, _ = make_machine(GENERATOR, [plain_item("minecraft:coal", 1)], amount=0)
        target = Energy(200000)
        for _ in range(1600):
            machine.tick()
            machine.push_energy([target, None, None, None, None, None])
        self.assertEqual(target.amount, 102400)
        self.assertEqual(machine.burn, 0)
        self.assertIsNone(slots.items[0])
        machine.tick()
        self.assertEqual(machine.status, "waiting_fuel")

    def test_full_generator_pauses_fuel_and_does_not_burn_another_coal(self):
        machine, slots, _ = make_machine(GENERATOR, [plain_item("minecraft:coal", 2)], amount=32000)
        machine.tick()
        self.assertEqual(machine.burn, 0)
        self.assertEqual(slots.items[0]["count"], 2)
        self.assertEqual(machine.status, "buffer_full")

    def test_six_faces_and_external_pulls_share_64_fe_budget(self):
        machine, _, _ = make_machine(GENERATOR, amount=1000)
        machine.tick()
        api = machine.api
        with api.open_transaction() as tx:
            self.assertEqual(machine.energy_output.extract(30, tx), 30)
            tx.commit()
        targets = [Energy(1000) for _ in range(6)]
        self.assertEqual(machine.push_energy(targets), 34)
        self.assertEqual(sum(t.amount for t in targets), 34)
        self.assertEqual(machine.push_energy(targets), 0)
        self.assertGreater(sum(t.amount > 0 for t in targets), 1)

    def test_aborted_output_transaction_preserves_budget_and_energy(self):
        machine, _, _ = make_machine(GENERATOR, amount=1000)
        machine.tick()
        with machine.api.open_transaction() as tx:
            self.assertEqual(machine.energy_output.extract(64, tx), 64)
        self.assertEqual(machine.energy.get_amount(), 1000)
        self.assertEqual(machine.budget.get_amount(), 64)

    def test_generator_reload_retains_partially_burned_coal(self):
        machine, slots, saved = make_machine(GENERATOR, [plain_item("minecraft:coal", 2)], amount=0)
        for _ in range(7):
            machine.tick()
        restored = ProductionMachine(GENERATOR, saved, slots, FakeApi(), MACHINE_RECIPES)
        self.assertEqual(restored.burn, 1593)
        restored.tick()
        self.assertEqual(restored.burn, 1592)
        self.assertEqual(slots.items[0]["count"], 1)
        self.assertEqual(restored.energy.get_amount(), 512)

    def test_snapshot_burn_ratio_survives_pause_and_reload(self):
        machine, slots, saved = make_machine(GENERATOR, amount=0)
        empty = machine.snapshot()
        self.assertEqual(empty["burn"], 0)
        self.assertEqual(empty["burn_duration"], FUEL_TICKS)
        slots.items[0] = plain_item("minecraft:coal", 1)
        machine.tick()
        burning = machine.snapshot()
        self.assertEqual(burning["burn"], FUEL_TICKS - 1)
        self.assertEqual(float(burning["burn"]) / burning["burn_duration"], 1599.0 / 1600)
        with machine.api.open_transaction() as tx:
            machine.energy.insert(32000, tx)
            tx.commit()
        machine.tick()
        paused = machine.snapshot()
        self.assertEqual(paused["status"], "buffer_full")
        self.assertEqual(paused["burn"], burning["burn"])
        self.assertEqual(paused["burn_duration"], burning["burn_duration"])
        restored = ProductionMachine(GENERATOR, saved, slots, FakeApi(), MACHINE_RECIPES)
        self.assertEqual(restored.snapshot()["burn"], paused["burn"])
        self.assertEqual(restored.snapshot()["burn_duration"], paused["burn_duration"])

    def test_snapshot_keeps_existing_fields_and_is_json_serializable(self):
        expected = set(("device_id", "energy", "capacity", "progress", "duration",
                        "mode", "status", "burn", "message"))
        for device_id in (CHARGER, INSCRIBER, GENERATOR):
            machine, _, _ = make_machine(device_id)
            data = machine.snapshot()
            self.assertEqual(set(data), expected | set(("burn_duration",)))
            self.assertEqual(json.loads(json.dumps(data)), data)

    def test_static_callback_can_be_marked_by_tooldelta(self):
        from endernet.server.adapters.production import static_listener
        calls = []
        listener = static_listener(calls.append)
        listener._td_static = True
        listener("event")
        self.assertEqual(calls, ["event"])


if __name__ == "__main__":
    unittest.main()
