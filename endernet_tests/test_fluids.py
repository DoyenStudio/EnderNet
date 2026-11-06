# coding=utf-8
"""Public radium protocol regression tests, without importing another addon.

The transaction fake deliberately keeps speculative writes separate from real
storage. These tests verify EnderNet's integer boundary, not radium internals.
"""

from copy import deepcopy
import math
import unittest

import testsupport  # noqa: F401

from endernet.common.identity import NodeKey
from endernet.common.protocol import TransferStatus
from endernet.common.resource_key import ResourceKey, canonical_tree
from endernet.server.adapters.fluids import RadiumFluidProvider


WATER = ResourceKey.fluid("minecraft:water")
LAVA = ResourceKey.fluid("minecraft:lava")


class FakeTransactionError(Exception):
    pass


class FakeTransaction(object):
    def __init__(self, api, parent=None):
        self.api = api
        self.parent = parent
        self.states = {}

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        self.api.current = self.parent

    def state(self, handler):
        if handler not in self.states:
            previous = self.parent.state(handler) if self.parent else handler.contents
            self.states[handler] = dict(previous)
        return self.states[handler]

    def commit(self):
        if self.api.fail_commit:
            raise FakeTransactionError("Machine unloaded before commit")
        if self.parent is not None:
            self.parent.states.update(self.states)
        else:
            for handler, state in self.states.items():
                handler.contents = dict(state)
            self.api.commits += 1
            if self.api.fail_after_commit:
                raise RuntimeError("Unexpected notification failure after application")


class FakeFluidHandler(object):
    def __init__(self, api, capacities=None):
        self.api = api
        self.capacities = capacities or {0: 1000.0, 3: 1000.0}
        self.contents = dict((slot, (None, 0.0)) for slot in self.capacities)
        self.inputs = set(self.capacities)
        self.outputs = set(self.capacities)
        self.accepts = lambda slot, resource: True
        self.calls = []
        self.responses = []
        self.reported_results = []
        self.stale = False

    def get_slots(self):
        self.check()
        return sorted(self.capacities)

    def check(self):
        if self.stale:
            raise FakeTransactionError("Stale query proxy")

    def get_resource(self, slot, tx=None):
        self.check()
        return (tx.state(self) if tx else self.contents)[slot][0]

    def get_amount(self, slot, tx=None):
        self.check()
        return (tx.state(self) if tx else self.contents)[slot][1]

    def get_capacity(self, slot, fluid_id=None):
        self.check()
        if fluid_id is not None and not self.is_valid(slot, fluid_id):
            return 0.0
        return self.capacities[slot]

    def is_valid(self, slot, fluid_id):
        self.check()
        return slot in self.inputs and self.accepts(slot, fluid_id)

    def insert(self, fluid_id, amount, tx, slot=None):
        return self.move(fluid_id, amount, tx, slot, True)

    def extract(self, fluid_id, amount, tx, slot=None):
        return self.move(fluid_id, amount, tx, slot, False)

    def move(self, resource, amount, tx, slot, insert):
        self.check()
        self.calls.append((insert, resource, amount, slot))
        if self.reported_results:
            return self.reported_results.pop(0)
        if self.responses:
            limit = self.responses.pop(0)
            if not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit < 0:
                return limit
            amount = min(amount, limit)
        state = tx.state(self)
        moved = 0.0
        for current in sorted(self.capacities) if slot is None else [slot]:
            before_resource, before = state[current]
            infinite = math.isinf(self.capacities[current])
            if insert:
                if not self.is_valid(current, resource) or before_resource not in (None, resource):
                    continue
                if infinite and before_resource is not None:
                    continue
                actual = min(amount - moved, self.capacities[current] - before)
                if actual > 0:
                    state[current] = (resource, float("inf") if infinite else before + actual)
            else:
                if current not in self.outputs or before_resource != resource:
                    continue
                actual = min(amount - moved, before)
                if actual > 0 and not infinite:
                    state[current] = (resource if before > actual else None, before - actual)
            moved += actual
            if moved >= amount:
                break
        return moved


class FakeRadium(object):
    API_VERSION = (1, 1, 0)
    FLUID = "radium:fluid"
    TransactionError = FakeTransactionError

    def __init__(self):
        self.handler = FakeFluidHandler(self)
        self.current = None
        self.commits = 0
        self.fail_commit = False
        self.fail_after_commit = False
        self.queries = []
        self.faces = set(range(6))
        self.properties = {}

    def query(self, capability, dimension, pos, face=None):
        self.queries.append((capability, dimension, pos, face))
        return self.handler if face in self.faces else None

    def open_transaction(self, parent=None):
        if self.current is not parent:
            raise AssertionError("A parent transaction must be explicit")
        self.current = FakeTransaction(self, parent)
        return self.current

    def fluid_resource(self, fluid_id, properties=None):
        if not properties:
            return fluid_id
        resource = (fluid_id, canonical_tree(properties))
        self.properties[resource] = deepcopy(properties)
        return resource

    def get_fluid_id(self, resource):
        return resource[0] if isinstance(resource, tuple) else resource

    def get_fluid_properties(self, resource):
        return deepcopy(self.properties.get(resource, {}))


class TestRadiumFluidAdapter(unittest.TestCase):
    def setUp(self):
        self.api = FakeRadium()
        self.available = self.api
        self.provider = RadiumFluidProvider(lambda: self.available)
        self.position = NodeKey(2, -7, 65, 11)
        self.adapter = self.provider.container_at(self.position, 4)

    def test_provider_uses_dimension_position_and_target_absolute_face(self):
        self.assertEqual(self.api.queries[-1], (self.api.FLUID, 2, (-7, 65, 11), 4))
        self.api.faces.remove(5)
        self.assertIsNone(self.provider.container_at(self.position, 5))

    def test_missing_component_is_not_cached_forever(self):
        self.available = None
        self.assertIsNone(self.provider.container_at(self.position, 4))
        self.available = self.api
        self.assertIsNotNone(self.provider.container_at(self.position, 4))

    def test_incompatible_api_versions_are_not_used(self):
        for version in ((1, 0, 0), (0, 9, 0), (2, 0, 0)):
            self.api.API_VERSION = version
            self.assertIsNone(self.provider.container_at(self.position, 4))
        self.api.API_VERSION = (1, 2, 0)
        self.assertIsNotNone(self.provider.container_at(self.position, 4))

    def test_each_operation_queries_again_after_machine_replacement(self):
        old = self.api.handler
        self.adapter.insert("first", WATER, 40)
        replacement = FakeFluidHandler(self.api)
        self.api.handler = replacement
        self.adapter.insert("second", WATER, 20)
        self.assertEqual(old.contents[0][1], 40)
        self.assertEqual(replacement.contents[0][1], 20)

    def test_sparse_slots_preserve_indices_and_drop_fractional_remainders_from_view(self):
        self.api.handler.contents = {0: (WATER.name, 14.75), 3: (LAVA.name, 9.25)}
        entries = self.adapter.entries()
        self.assertEqual([(slot, entry.resource_key, entry.count) for slot, entry in entries],
                         [(0, WATER, 14), (3, LAVA, 9)])
        self.assertEqual(self.adapter.count_of(WATER), 14)

    def test_simulated_insertion_does_not_change_storage_or_commit(self):
        self.api.handler.capacities = {0: 10.5, 3: 0.0}
        self.assertEqual(self.adapter.can_accept(WATER, 20), 10)
        self.assertEqual(self.api.handler.contents[0], (None, 0.0))
        self.assertEqual(self.api.commits, 0)

    def test_fractional_capacity_commits_only_whole_millibuckets(self):
        self.api.handler.capacities = {0: 10.5, 3: 0.0}
        receipt = self.adapter.insert("insert", WATER, 20)
        self.assertEqual(receipt.status, TransferStatus.PARTIAL)
        self.assertEqual(receipt.moved, 10)
        self.assertEqual(receipt.remainder, 10)
        self.assertEqual(self.api.handler.contents[0], (WATER.name, 10.0))

    def test_fractional_extraction_leaves_remainder_in_machine(self):
        self.api.handler.contents[3] = (WATER.name, 25.75)
        receipt = self.adapter.extract("extract", WATER, 40)
        self.assertEqual(receipt.moved, 25)
        self.assertEqual(self.api.handler.contents[3], (WATER.name, 0.75))

    def test_second_fractional_result_rolls_back_without_guessing(self):
        self.api.handler.responses = [4.5, 3.5]
        receipt = self.adapter.insert("changing-limit", WATER, 10)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(self.api.handler.contents[0], (None, 0.0))
        self.assertEqual(self.api.commits, 0)

    def test_less_than_one_millibucket_is_never_committed(self):
        self.api.handler.contents[0] = (WATER.name, 0.75)
        receipt = self.adapter.extract("tiny", WATER, 1)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(self.api.handler.contents[0], (WATER.name, 0.75))
        self.assertEqual(self.api.commits, 0)

    def test_properties_round_trip_without_merging_same_base_id(self):
        properties = {"grade": 2, "additives": [True, None, {"density": 0.5}]}
        coolant = ResourceKey.fluid("test:coolant", user_data=properties)
        self.assertEqual(self.adapter.insert("insert", coolant, 50).moved, 50)
        self.assertEqual(self.adapter.entries()[0][1].resource_key, coolant)
        self.assertEqual(self.adapter.count_of(ResourceKey.fluid("test:coolant")), 0)
        self.assertEqual(self.adapter.extract("wrong", ResourceKey.fluid("test:coolant"), 50).moved, 0)
        self.assertEqual(self.adapter.extract("correct", coolant, 50).moved, 50)

    def test_typed_properties_stay_distinct(self):
        first = ResourceKey.fluid("test:coolant", user_data={"grade": 1})
        second = ResourceKey.fluid("test:coolant", user_data={"grade": True})
        self.adapter.insert("first", first, 10)
        self.adapter.insert("second", second, 20)
        self.assertEqual(self.adapter.count_of(first), 10)
        self.assertEqual(self.adapter.count_of(second), 20)

    def test_port_input_output_restrictions_are_respected(self):
        self.api.handler.inputs = {3}
        self.api.handler.outputs = {0}
        self.assertEqual(self.adapter.insert("insert", WATER, 10).moved, 10)
        self.assertEqual(self.api.handler.contents[3], (WATER.name, 10.0))
        self.assertEqual(self.adapter.extract("extract", WATER, 10).moved, 0)

    def test_visible_input_tank_does_not_hide_a_later_extractable_resource(self):
        self.api.handler.contents = {0: (WATER.name, 100.0), 3: (LAVA.name, 20.0)}
        self.api.handler.outputs = {3}
        entries = self.adapter.entries()
        self.assertEqual([(slot, entry.resource_key, entry.count) for slot, entry in entries],
                         [(3, LAVA, 20)])
        self.assertEqual(self.adapter.count_of(WATER), 0)
        self.assertEqual(self.api.handler.contents[3], (LAVA.name, 20.0))
        self.assertEqual(self.api.commits, 0)

    def test_resource_filter_refusal_does_not_consume_fluid(self):
        self.api.handler.accepts = lambda slot, resource: resource == LAVA.name
        receipt = self.adapter.insert("insert", WATER, 10)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)

    def test_infinite_source_uses_committed_receipt_not_inventory_delta(self):
        self.api.handler.capacities[3] = float("inf")
        self.api.handler.contents[3] = (WATER.name, float("inf"))
        self.assertEqual(self.adapter.entries()[0][1].count, 2 ** 53 - 1)
        receipt = self.adapter.extract("creative", WATER, 250)
        self.assertEqual(receipt.status, TransferStatus.COMPLETE)
        self.assertEqual(receipt.moved, 250)
        self.assertEqual(self.api.handler.contents[3][1], float("inf"))

    def test_commit_invalidation_rolls_back_and_reports_zero(self):
        self.api.fail_commit = True
        receipt = self.adapter.insert("stale-at-commit", WATER, 10)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(self.api.handler.contents[0], (None, 0.0))
        self.assertEqual(self.api.commits, 0)

    def test_unexpected_commit_error_is_uncertain_and_not_replayed(self):
        self.api.fail_after_commit = True
        receipt = self.adapter.insert("unknown-commit-result", WATER, 10)
        self.assertEqual(receipt.status, TransferStatus.UNCERTAIN)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(self.api.handler.contents[0], (WATER.name, 10.0))
        self.assertEqual(self.api.commits, 1)
        self.assertEqual(len(self.api.handler.calls), 1)

    def test_invalid_public_transfer_results_never_commit(self):
        for invalid in (True, float("nan"), float("inf"), -1, 11, "10", None):
            self.api.handler.reported_results = [invalid]
            receipt = self.adapter.insert("bad-public-result", WATER, 10)
            self.assertEqual(receipt.moved, 0)
            self.assertEqual(self.api.commits, 0)

    def test_empty_property_record_is_not_silently_merged_with_no_properties(self):
        empty_properties = ResourceKey.fluid(WATER.name, user_data={})
        self.assertNotEqual(empty_properties, WATER)
        self.assertEqual(self.adapter.insert("empty-properties", empty_properties, 10).moved, 0)
        self.assertEqual(self.api.commits, 0)

    def test_non_integer_request_is_rejected_before_contacting_machine(self):
        for invalid in (True, 0.5, -1):
            with self.assertRaises((TypeError, ValueError)):
                self.adapter.insert("bad-request", WATER, invalid)
        self.assertEqual(self.api.handler.calls, [])

    def test_missing_component_after_discovery_refuses_without_old_handler_write(self):
        self.available = None
        self.assertEqual(self.adapter.insert("component-gone", WATER, 10).moved, 0)
        self.assertFalse(self.adapter.is_valid())
        self.assertEqual(self.api.handler.calls, [])

    def test_stale_operation_reports_zero_without_committing(self):
        self.api.handler.stale = True
        receipt = self.adapter.extract("stale", WATER, 10)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(self.api.commits, 0)

    def test_provider_invalidation_invalidates_existing_adapters(self):
        self.provider.invalidate(self.position)
        self.assertFalse(self.adapter.is_valid())
        self.assertEqual(self.adapter.insert("invalid", WATER, 10).moved, 0)
        fresh = self.provider.container_at(self.position, 4)
        self.assertTrue(fresh.is_valid())
        self.provider.invalidate_all()
        self.assertFalse(fresh.is_valid())

    def test_item_keys_cannot_be_passed_to_fluid_endpoint(self):
        item = ResourceKey.item("minecraft:water_bucket")
        self.assertEqual(self.adapter.insert("wrong-kind", item, 1).moved, 0)
        self.assertEqual(self.adapter.extract("wrong-kind", item, 1).moved, 0)
        self.assertEqual(self.api.commits, 0)


if __name__ == "__main__":
    unittest.main()
