# coding=utf-8
"""转录器：守恒、限速、能源、介质生命周期及共享存档。"""
import json
import os
import unittest
from unittest import mock

import testsupport
from endernet.common.definitions import DefinitionSet, TRANSCRIBER_ID, UNCONFIGURED
from endernet.common.identity import NodeKey, DeviceRef
from endernet.common.resources.key import ResourceKey
from endernet.common.resources import medium_item
from endernet.server.adapters.containers.contract import FakeContainer
from endernet.server.devices.registry import DeviceRegistry
from endernet.server.devices.storage_driver import StorageDriverStore
from endernet.server.devices.transcriber import TranscriberService
from endernet.server.storage.medium import Medium, MediumPolicy, transfer_between
from endernet.server.network.energy import EnergyService, EnergyPolicy
from endernet.server.network.node import NetworkNode
from endernet.server.persistence.repository import MemoryRepository

TAPE = "endernet:singleside_tape"
FLOPPY = "endernet:softdisk_1k"
STONE = ResourceKey.item("minecraft:stone", 0)
DIRT = ResourceKey.item("minecraft:dirt", 0)
WATER = ResourceKey.fluid("minecraft:water")


class Boxes(object):
    def __init__(self):
        self.boxes = {}

    def container_at(self, key):
        return self.boxes.setdefault(key.as_key(), FakeContainer(slots=8, max_stack=1))


class Runtime(object):
    def __init__(self, definitions):
        self.definitions = definitions


class TestTranscriber(unittest.TestCase):
    def setUp(self):
        self.defs = DefinitionSet()
        self.registry = DeviceRegistry(self.defs)
        self.boxes = Boxes()
        self.store = StorageDriverStore(self.defs, self.registry, containers=self.boxes,
                                        medium_policy=MediumPolicy(frames_per_second=20))
        self.ref = self.registry.register(NodeKey(0, 0, 64, 0), TRANSCRIBER_ID).record.ref
        self.state = self.store.ensure(self.ref)
        self.service = TranscriberService(self.store)
        self.energy = EnergyService(Runtime(self.defs), policy=EnergyPolicy.dev_stand_in())
        self.energy.add_node(NetworkNode.from_record(self.defs, self.registry.get_by_ref(self.ref)))

    def insert(self, slot, model=TAPE, entries=None):
        box = self.boxes.container_at(self.ref.node_key)
        box.write_slot(slot, testsupport.item_dict_without_carrier(model, 1))
        self.assertTrue(self.store.refresh(self.ref))
        medium = self.store.medium(self.state.assignment(slot).medium_id)
        for key, count in (entries or {}).items():
            medium._put(key, count)
        return medium

    def pulse(self, tick, online=True):
        self.service.prepare(tick, lambda ref: self.energy)
        return self.service.execute(tick, lambda ref: online)

    def test_combined_four_slots_share_each_medium_budget(self):
        sources = [self.insert(i, entries={STONE: 100}) for i in range(4)]
        targets = [self.insert(i) for i in range(4, 8)]
        self.assertEqual(self.pulse(20), 16 * 50)
        self.assertEqual(sum(m.count_of(STONE) for m in targets), 16)
        self.assertEqual(sum(m.count_of(STONE) for m in sources), 384)
        self.assertEqual(self.service.view(self.ref)["demand"], 4)
        self.assertEqual(self.pulse(20), 0)
        self.assertEqual(self.pulse(21), 0)

    def test_read_and_write_bottlenecks(self):
        for left, right in ((FLOPPY, TAPE), (TAPE, FLOPPY), (FLOPPY, FLOPPY)):
            with self.subTest(left=left, right=right):
                self.setUp()
                self.insert(0, left, {STONE: 100})
                target = self.insert(4, right)
                expected = 16 if left == right == FLOPPY else 4
                self.assertEqual(self.pulse(20), expected * 50)
                self.assertEqual(target.count_of(STONE), expected)
                self.assertEqual(self.service.view(self.ref)["demand"], expected // 4)

    def test_forecast_does_not_mutate_content_or_tokens(self):
        source = self.insert(0, entries={STONE: 100})
        target = self.insert(4)
        before = (source.to_dict(), target.to_dict())
        self.service.prepare(20, lambda ref: self.energy)
        self.assertEqual((source.to_dict(), target.to_dict()), before)
        self.assertEqual(self.service.view(self.ref)["rate_micro"], 200)

    def test_partial_capacity_and_zero_floor(self):
        source = self.insert(0, entries={STONE: 100})
        target = self.insert(4, entries={STONE: 1726})
        self.assertEqual(self.pulse(20), 100)
        self.assertEqual(self.service.view(self.ref)["demand"], 0)
        self.assertEqual((source.count_of(STONE), target.count_of(STONE)), (98, 1728))
        self.pulse(40)
        self.assertEqual(self.service.view(self.ref)["rate_micro"], 0)

    def test_full_type_target_does_not_block_other_target(self):
        self.insert(0, entries={STONE: 50})
        full_types = dict((ResourceKey.item("test:item_%d" % i, 0), 1) for i in range(12))
        blocked = self.insert(4, entries=full_types)
        target = self.insert(5)
        self.pulse(20)
        self.assertEqual(blocked.count_of(STONE), 0)
        self.assertEqual(target.count_of(STONE), 4)

    def test_mixed_resources_and_rotation(self):
        source = self.insert(0, entries={STONE: 100, DIRT: 100, WATER: 5000})
        target = self.insert(4)
        for tick in range(1, 201):
            self.pulse(tick)
        for key in (STONE, DIRT, WATER):
            self.assertGreater(target.count_of(key), 0)
            self.assertEqual(source.count_of(key) + target.count_of(key),
                             5000 if key == WATER else 100)
        self.assertLessEqual(target.used_micro, 40 * 50)

    def test_low_speed_accumulates_fractional_items(self):
        self.insert(0, entries={STONE: 50})
        target = self.insert(4)
        for tick in range(1, 21):
            self.pulse(tick)
        self.assertEqual(target.count_of(STONE), 4)

    def test_brownout_keeps_demand_and_resumes(self):
        source = self.insert(0, entries={STONE: 100})
        self.insert(4)
        for tick in range(1, 41):
            self.assertEqual(self.pulse(tick, online=False), 0)
            self.assertEqual(self.service.view(self.ref)["demand"], 1)
        self.assertEqual(source.count_of(STONE), 100)
        self.assertEqual(self.pulse(41), 200)

    def test_target_removed_between_quote_and_commit(self):
        source = self.insert(0, entries={STONE: 100})
        target = self.insert(4)
        self.service.prepare(20, lambda ref: self.energy)
        self.boxes.container_at(self.ref.node_key).write_slot(4, None)
        self.assertEqual(self.service.execute(20, lambda ref: True), 0)
        self.assertEqual((source.count_of(STONE), target.count_of(STONE)), (100, 0))

    def test_unloaded_and_unreadable_pause(self):
        self.insert(0, entries={STONE: 100})
        self.insert(4)
        self.store.mark_loaded(self.ref.node_key, False)
        self.assertEqual(self.pulse(20), 0)
        self.store.mark_loaded(self.ref.node_key, True)
        with mock.patch.object(self.boxes, "container_at", side_effect=IOError("unloaded")):
            self.assertEqual(self.pulse(21), 0)
        self.assertEqual(self.pulse(40), 200)

    def test_duplicates_never_transfer_to_self(self):
        source = self.insert(0, entries={STONE: 100})
        box = self.boxes.container_at(self.ref.node_key)
        box.write_slot(4, box.read_slot(0))
        self.assertEqual(self.pulse(20), 0)
        self.assertEqual(source.count_of(STONE), 100)
        self.assertIn(4, self.state.invalid)

    def test_move_to_driver_retains_identity_and_tokens(self):
        source = self.insert(0, entries={STONE: 100})
        self.insert(4)
        self.pulse(20)
        box = self.boxes.container_at(self.ref.node_key)
        item = box.read_slot(0)
        box.write_slot(0, None)
        self.store.refresh(self.ref)
        other = self.registry.register(NodeKey(0, 2, 64, 0), "endernet:storage_driver").record.ref
        self.boxes.container_at(other.node_key).write_slot(0, item)
        state = self.store.ensure(other)
        self.assertIs(self.store.medium(state.assignment(0).medium_id), source)
        self.assertEqual(source.count_of(STONE), 96)
        self.assertEqual(source.read_bucket.balance_micro, 0)
        self.assertIsNone(self.state.provider)
        self.assertIsNotNone(state.provider)

    def test_pair_save_exceeds_budget_as_one_group_and_restores(self):
        source = self.insert(0, entries={STONE: 100})
        target = self.insert(4)
        self.pulse(20)
        repo = MemoryRepository()
        self.assertEqual(self.store.save_to_repository(repo, budget=1), 2)
        restored = StorageDriverStore(self.defs, self.registry, containers=self.boxes)
        restored.restore_from_repository(repo)
        self.assertEqual(restored.medium(source.medium_id).count_of(STONE), 96)
        self.assertEqual(restored.medium(target.medium_id).count_of(STONE), 4)

    def test_pair_save_failure_keeps_entire_group_dirty(self):
        source = self.insert(0, entries={STONE: 100})
        target = self.insert(4)
        self.pulse(20)
        repo = MemoryRepository()
        original = repo.write
        calls = []
        def fail_second(record):
            calls.append(record)
            if len(calls) == 2:
                raise IOError("save failure")
            return original(record)
        with mock.patch.object(repo, "write", side_effect=fail_second):
            with self.assertRaises(IOError):
                self.store.save_to_repository(repo, budget=1)
        self.assertTrue({source.medium_id, target.medium_id}.issubset(self.store._dirty_mediums))
        self.assertEqual(self.store.save_to_repository(repo, budget=1), 2)

    def test_commit_failure_restores_both_budgets_and_contents(self):
        source = self.insert(0, entries={STONE: 100})
        target = self.insert(4)
        before = (source.to_dict(), target.to_dict())
        original = Medium.commit
        def refuse_target(medium, plan, tick):
            if medium is target:
                original(medium, plan, tick)
                raise RuntimeError("failed after target write")
            return original(medium, plan, tick)
        with mock.patch.object(Medium, "commit", refuse_target):
            self.assertEqual(transfer_between(source, target, STONE, 4, 20), 0)
        self.assertEqual((source.to_dict(), target.to_dict()), before)
        self.assertEqual(transfer_between(source, target, STONE, 4, 20, (9, 9)), 0)

    def test_corrupt_media_is_preserved(self):
        source = self.insert(0, entries={STONE: 100})
        self.insert(4)
        source.recovery = "corrupt"
        self.assertEqual(self.pulse(20), 0)
        self.assertEqual(source.count_of(STONE), 100)

    def test_dynamic_demand_generation_and_remount(self):
        self.assertIs(self.energy._members[self.ref.node_key].demand, UNCONFIGURED)
        self.assertTrue(self.energy.set_dynamic_demand(self.ref, 4))
        stale = DeviceRef(self.ref.node_key, self.ref.generation + 1)
        self.assertFalse(self.energy.set_dynamic_demand(stale, 99))
        self.energy.add_node(NetworkNode.from_record(self.defs, self.registry.get_by_ref(self.ref)))
        self.assertEqual(self.energy._members[self.ref.node_key].demand, 4)
        self.energy.set_dynamic_demand(self.ref, None)
        self.assertIs(self.energy._members[self.ref.node_key].demand, UNCONFIGURED)

    def test_media_only_and_ui_slot_mapping(self):
        self.assertTrue(self.store.accepts_put(TRANSCRIBER_ID, TAPE))
        self.assertFalse(self.store.accepts_put(TRANSCRIBER_ID, "minecraft:stone"))
        path = os.path.join(os.path.dirname(__file__), "..", "endernet_rp", "ui", "endernet", "storage", "transcriber.json")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        found = []
        def walk(value):
            if isinstance(value, dict):
                if "collection_index" in value:
                    found.append(value["collection_index"])
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)
        walk(data)
        self.assertEqual(sorted(found), list(range(8)))

    def test_handoff_lore_matches_latest_contents(self):
        source = self.insert(0, entries={STONE: 100})
        self.insert(4)
        self.pulse(20)
        self.store.advance_display(budget=8, node_key=self.ref.node_key, force=True)
        data = self.boxes.container_at(self.ref.node_key).read_slot(0)
        self.assertEqual(data["userData"]["display"]["Lore"], list(medium_item.medium_lore_lines(source)))


    def test_duplicate_across_hosts_has_one_owner(self):
        source = self.insert(0, entries={STONE: 100})
        item = self.boxes.container_at(self.ref.node_key).read_slot(0)
        other = self.registry.register(NodeKey(0, 2, 64, 0), "endernet:storage_driver").record.ref
        self.boxes.container_at(other.node_key).write_slot(0, item)
        self.store.ensure(other)
        holders = [state for state in self.store.states() if source.medium_id in state.medium_ids()]
        self.assertEqual(len(holders), 1)
        self.assertEqual(self.store.medium_slot_owner(source.medium_id)[0], holders[0])

    def test_replaced_device_invalidates_prepared_transfer(self):
        source = self.insert(0, entries={STONE: 100})
        self.insert(4)
        self.service.prepare(20, lambda ref: self.energy)
        self.store.drop(self.ref)
        self.assertEqual(self.service.execute(20, lambda ref: True), 0)
        self.assertEqual(source.count_of(STONE), 100)

    def test_model_mismatch_never_reinterprets_contents(self):
        source = self.insert(0, entries={STONE: 100})
        box = self.boxes.container_at(self.ref.node_key)
        item = box.read_slot(0)
        item["newItemName"] = FLOPPY
        box.write_slot(0, item)
        self.insert(4)
        self.assertEqual(self.pulse(20), 0)
        self.assertEqual(source.count_of(STONE), 100)
        self.assertEqual(self.state.invalid[0]["code"], "medium_model_mismatch")

    def test_static_energy_updates_cannot_erase_dynamic_demand(self):
        self.energy.set_dynamic_demand(self.ref, 4)
        self.assertFalse(self.energy.set_device_state(self.ref, state="idle", unit_count=0))
        self.assertEqual(self.energy._members[self.ref.node_key].demand, 4)

    def test_continuous_transfer_does_not_starve_lore_refresh(self):
        source = self.insert(0, "endernet:database_compressed_tape", {STONE: 1000})
        self.insert(4, "endernet:database_compressed_tape")
        for tick in range(1, 11):
            self.pulse(tick)
            self.store.advance_display()
        self.assertGreater(self.store.stats["display_writes"], 2)
        self.assertGreater(source.count_of(STONE), 0)


class TestTranscriberNetwork(unittest.TestCase):
    def test_both_controllers_run_without_exposing_inventory(self):
        from test_bootstrap import FakeWorld
        from endernet.server.bootstrap import ServerBootstrap
        from endernet.server.runtime.events import NormalizedEvent
        from endernet.server.network.services import ServiceId
        for controller in ("endernet:dos_network_endpoint", "endernet:tiny_network_endpoint"):
            with self.subTest(controller=controller):
                boxes = Boxes()
                server = ServerBootstrap(world=FakeWorld(), container_provider=boxes,
                                         energy_policy=EnergyPolicy.dev_stand_in())
                server.start(bind_engine=False)
                self.addCleanup(server.stop)
                transcriber = NodeKey(0, 1, 64, 0)
                for key, device in ((NodeKey(0, 0, 64, 0), "endernet:network_baseband_8"),
                                    (NodeKey(0, -1, 64, 0), controller),
                                    (transcriber, TRANSCRIBER_ID),
                                    (NodeKey(0, 0, 65, 0), "endernet:creative_energy_source")):
                    server.push(NormalizedEvent.place(key, device))
                server.tick()
                state = server.drivers.get(transcriber)
                box = boxes.container_at(transcriber)
                for slot in (0, 4):
                    box.write_slot(slot, testsupport.item_dict_without_carrier(TAPE, 1))
                server.drivers.refresh(state.ref)
                source = server.drivers.medium(state.assignment(0).medium_id)
                target = server.drivers.medium(state.assignment(4).medium_id)
                source._put(STONE, 100)
                server.clock.advance(20)
                server.tick()
                self.assertTrue(server.runtimes.device_status(state.ref).is_online)
                self.assertEqual(target.count_of(STONE), 4)
                self.assertEqual(source.count_of(STONE), 96)
                runtime = server.runtimes.runtime_of(transcriber)
                self.assertEqual(runtime.services.get(ServiceId.STORAGE).snapshot()["entries"], [])
                self.assertEqual(server.admission.status_of(state.ref).demand_quarters, 0)
