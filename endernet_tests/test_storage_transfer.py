# coding=utf-8
"""转储器真实槽、守恒、限速、滚动能耗与故障恢复。"""
import unittest
from unittest.mock import patch

from test_storage import Fixture, TAPE, STONE, DIRT, WATER, key
from endernet.common.definitions import STORAGE_TRANSFER_ID
from endernet.common.resources.key import MICRO_UNITS_PER_UNIT
from endernet.server.adapters.containers.contract import FakeContainer
from endernet.server.devices.storage_transfer import StorageTransferService
from endernet.server.network.node import NetworkNode
from endernet.server.persistence.repository import MemoryRepository
from endernet.server.devices.storage_driver import MEDIUM_RECORD_KIND


class Energy(object):
    def set_dynamic_demand(self, ref, demand):
        self.demand = demand


class StorageTransferTests(Fixture):
    def setUp(self):
        Fixture.setUp(self)
        self.target = self.new_medium()
        self.containers.by_position[key(1).as_key()] = FakeContainer(slots=4, max_stack=1)
        self.transfer = self.place(1, STORAGE_TRANSFER_ID)
        self.bay = self.store.get_by_ref(self.transfer.ref)
        self.runner = StorageTransferService(self.store)
        self.energy = Energy()

    def source(self, slot=0, entries=None):
        medium, _, _ = self.store.create_medium(TAPE)
        for resource, count in (entries or {STONE: 40}).items():
            medium._put(resource, count)
        self.assertTrue(self.store.place_medium(self.bay, slot, medium.medium_id)[0])
        return medium

    def run_tick(self, tick, online=True):
        self.runner.prepare(tick, lambda ref: self.energy)
        return self.runner.execute(tick, lambda ref: online, lambda ref: self.service)

    def test_transfer_is_conservative_and_source_is_not_network_stock(self):
        source = self.source()
        self.service.add_node(NetworkNode.from_record(self.definitions, self.transfer))
        self.assertEqual(self.service.query(STONE)["stored"], 0)
        self.run_tick(0)
        self.assertEqual(self.run_tick(1), 4 * MICRO_UNITS_PER_UNIT)
        self.assertEqual((source.count_of(STONE), self.target.count_of(STONE)), (36, 4))
        self.assertEqual(self.service.query(STONE)["stored"], 4)
        self.assertTrue(self.service.audit()["ok"])

    def test_rolling_energy_uses_commits_and_expires_while_offline(self):
        self.source()
        self.run_tick(0)
        self.assertEqual(self.energy.demand, 0)
        self.run_tick(1)
        self.assertEqual(self.energy.demand, 0)
        self.run_tick(2, False)
        self.assertEqual(self.energy.demand, 1)
        self.run_tick(3, False)
        self.assertEqual(self.energy.demand, 0)
        self.assertEqual(self.run_tick(100), 0)
        self.assertEqual(self.run_tick(101), 4 * MICRO_UNITS_PER_UNIT)

    def test_manual_or_logistics_removal_stops_old_task(self):
        source = self.source()
        self.run_tick(0)
        self.containers.container_at(self.transfer.node_key).write_slot(0, None)
        self.assertEqual(self.run_tick(1), 0)
        self.assertEqual(source.count_of(STONE), 40)

    def test_destination_removed_is_not_written_through_stale_mount(self):
        source = self.source()
        self.run_tick(0)
        self.unplug()
        self.assertEqual(self.run_tick(1), 0)
        self.assertEqual(source.count_of(STONE), 40)

    def test_four_slots_and_multiple_resources_make_progress(self):
        sources = [self.source(slot, {STONE: 40, DIRT: 40}) for slot in range(4)]
        for tick in range(20):
            self.run_tick(tick)
        for source in sources:
            self.assertLess(source.count_of(STONE), 40)
            self.assertLess(source.count_of(DIRT), 40)
        self.assertEqual(sum(s.count_of(STONE) for s in sources) + self.target.count_of(STONE), 160)
        self.assertEqual(sum(s.count_of(DIRT) for s in sources) + self.target.count_of(DIRT), 160)

    def test_fluid_uses_unit_conversion_and_empty_medium_remains(self):
        source = self.source(entries={WATER: 50})
        for tick in range(4):
            self.run_tick(tick)
        self.assertEqual(source.count_of(WATER), 0)
        self.assertEqual(self.target.count_of(WATER), 50)
        self.assertEqual(self.bay.assignment(0).medium_id, source.medium_id)
        data = self.containers.container_at(self.transfer.node_key).read_slot(0)
        self.assertFalse(self.store.accepts_put(STORAGE_TRANSFER_ID, TAPE, data))

    def test_ingress_requires_nonempty_real_identity_and_single_item(self):
        source = self.source()
        data = self.containers.container_at(self.transfer.node_key).read_slot(0)
        self.assertTrue(self.store.accepts_put(STORAGE_TRANSFER_ID, TAPE, data))
        self.assertFalse(self.store.accepts_put(STORAGE_TRANSFER_ID, TAPE, {"count": 1}))
        self.assertFalse(self.store.accepts_put(STORAGE_TRANSFER_ID, "minecraft:stone", data))
        data["count"] = 2
        self.assertFalse(self.store.accepts_put(STORAGE_TRANSFER_ID, TAPE, data))
        source.entries.clear()
        source.recompute_usage()
        data["count"] = 1
        self.assertFalse(self.store.accepts_put(STORAGE_TRANSFER_ID, TAPE, data))

    def test_failed_target_commit_rolls_back_source_and_budgets(self):
        source = self.source()
        self.run_tick(0)
        from endernet.server.storage.medium import Medium
        original = Medium.commit
        def fail(medium, *args):
            if medium is self.target:
                raise RuntimeError("injected")
            return original(medium, *args)
        with patch.object(Medium, "commit", fail):
            self.assertEqual(self.run_tick(1), 0)
            self.assertEqual(source.count_of(STONE), 40)
        self.assertEqual(self.run_tick(2), 4 * MICRO_UNITS_PER_UNIT)

    def test_group_failure_blocks_save_until_full_retry_and_restore_conserves(self):
        source = self.source()
        self.run_tick(0)
        self.run_tick(1)
        repository = MemoryRepository()
        repository.inject_fault("write", MEDIUM_RECORD_KIND, source.medium_id)
        with self.assertRaises(Exception):
            self.store.save_to_repository(repository, budget=1)
        with self.assertRaises(IOError):
            repository.commit()
        self.store.save_to_repository(repository, budget=1)
        repository.commit()
        records = [repository.load(MEDIUM_RECORD_KIND, medium.medium_id) for medium in (source, self.target)]
        from endernet.server.storage.medium import Medium
        restored = [Medium.from_dict(record.payload, self.definitions, self.medium_policy) for record in records]
        self.assertEqual(sum(medium.count_of(STONE) for medium in restored), 40)

    def test_dos_rejects_and_tiny_accepts(self):
        self.assertFalse(self.definitions.get("endernet:dos_network_endpoint").allows(STORAGE_TRANSFER_ID))
        self.assertTrue(self.definitions.get("endernet:tiny_network_endpoint").allows(STORAGE_TRANSFER_ID))

    def test_full_target_is_skipped_and_other_resource_can_progress(self):
        source = self.source(entries={STONE: 8, DIRT: 8})
        # 类型满但已有泥土仍可追加；石头不能阻塞泥土。
        self.target._put(DIRT, 1)
        for index in range(self.target.max_types() - 1):
            from endernet.common.resources.key import ResourceKey
            self.target._put(ResourceKey.item("test:kind_%d" % index), 1)
        self.service._reindex_keys((DIRT,))
        for tick in range(8):
            self.run_tick(tick)
        self.assertEqual(source.count_of(STONE), 8)
        self.assertLess(source.count_of(DIRT), 8)

    def test_multiple_targets_are_revisited_independently_for_each_slot(self):
        self.target._put(STONE, int(self.target.capacity_micro() // MICRO_UNITS_PER_UNIT))
        self.service._reindex_keys((STONE,))
        others = [self.new_medium(slot=slot) for slot in range(1, 4)]
        sources = [self.source(slot, {DIRT: 16}) for slot in range(4)]
        for tick in range(16):
            self.run_tick(tick)
        self.assertTrue(all(source.count_of(DIRT) < 16 for source in sources))
        self.assertEqual(sum(source.count_of(DIRT) for source in sources)
                         + sum(target.count_of(DIRT) for target in others), 64)

    def test_twenty_tick_window_sums_slots_before_rounding(self):
        self.medium_policy.frames_per_second = 20
        sources = [self.source(slot, {STONE: 1}) for slot in range(4)]
        self.run_tick(0)
        for tick in range(1, 22):
            self.run_tick(tick)
        self.assertEqual(self.energy.demand, 1)
        self.assertEqual(sum(source.count_of(STONE) for source in sources), 0)
        self.run_tick(41)
        self.assertEqual(self.energy.demand, 0)

    def test_fractional_fluid_rate_below_four_is_free(self):
        self.source(entries={WATER: 25})
        self.run_tick(0)
        self.run_tick(1)
        self.run_tick(2)
        self.assertEqual(self.energy.demand, 0)
        self.assertEqual(self.runner.view(self.transfer.ref)["rate_micro"], 25)

    def test_repeated_execution_in_one_tick_does_not_duplicate(self):
        source = self.source()
        self.run_tick(0)
        self.run_tick(1)
        before = source.count_of(STONE)
        self.assertEqual(self.run_tick(1), 0)
        self.assertEqual(source.count_of(STONE), before)

    def test_real_hopper_typed_nbt_is_normalized_before_ingress_check(self):
        from endernet.server.adapters.events import ServerEventBinder
        source = self.source()
        binder = ServerEventBinder.__new__(ServerEventBinder)
        binder.registry, binder.drivers = self.registry, self.store
        binder.patterns = binder.memory_slots = binder.ports = None
        binder.counts = {}
        data = {"newItemName": TAPE, "count": 1, "newAuxValue": 0,
                "userData": {"endernet:medium": {
                    "id": {"__type__": 8, "__value__": source.medium_id},
                    "v": {"__type__": 3, "__value__": 1}}}}
        class Event(object):
            collectionIndex = 0
            cancelled = False
            def cancel(self):
                self.cancelled = True
        event = Event()
        self.assertFalse(binder._veto_or_mark(self.transfer.node_key, data, event))
        self.assertFalse(event.cancelled)
        source.entries.clear()
        source.recompute_usage()
        self.assertTrue(binder._veto_or_mark(self.transfer.node_key, data, event))
        self.assertTrue(event.cancelled)

    def test_two_devices_share_target_without_starvation_or_duplication(self):
        first = self.source()
        self.containers.by_position[key(3).as_key()] = FakeContainer(slots=4, max_stack=1)
        record = self.place(3, STORAGE_TRANSFER_ID)
        other = self.store.get_by_ref(record.ref)
        second, _, _ = self.store.create_medium(TAPE)
        second._put(STONE, 40)
        self.assertTrue(self.store.place_medium(other, 0, second.medium_id)[0])
        for tick in range(10):
            self.run_tick(tick)
        self.assertLess(first.count_of(STONE), 40)
        self.assertLess(second.count_of(STONE), 40)
        self.assertEqual(first.count_of(STONE) + second.count_of(STONE)
                         + self.target.count_of(STONE), 80)

    def test_destroyed_device_and_duplicate_identity_cannot_continue(self):
        source = self.source()
        self.run_tick(0)
        # 强行复制物品进另一容器，现有唯一归属裁决后最多有一个可写来源。
        data = self.containers.container_at(self.transfer.node_key).read_slot(0)
        self.containers.container_at(self.record.node_key).write_slot(1, data)
        self.store.refresh(self.record.ref)
        self.service.add_node(self.node)
        self.run_tick(1)
        self.assertEqual(source.count_of(STONE) + self.target.count_of(STONE), 40)
        self.store.drop(self.transfer.ref)
        self.registry.unregister(self.transfer.node_key)
        self.assertEqual(self.run_tick(2), 0)
        self.assertIsNone(self.runner.view(self.transfer.ref))


if __name__ == "__main__":
    unittest.main()
