# coding=utf-8
"""主动物品口定案：真实库存、ID名单、轮转与权限，离线回归。"""
import json
import unittest

import testsupport  # noqa: F401
import test_io as fixtures
from endernet.common.identity import DeviceRef, node_key_text
from endernet.common.protocol import Reason
from endernet.common.resources.key import ResourceKey
from endernet.server.adapters.containers.contract import ContainerEntry, FakeContainer
from endernet.server.devices.bus_io import (
    PortConfig, PortStore, PortRunner, PortGateway, normalize_item_config,
    MODE_BLACKLIST, MODE_WHITELIST, PORT_RECORD_KIND, port_blocker,
)
from endernet.server.persistence.repository import MemoryRepository, Record
from endernet.client.ui.devices.port_config_model import PortConfigModel
from endernet.server.adapters.world import ToolDeltaWorldAdapter


class FacingStates(unittest.TestCase):
    def test_six_way_state_takes_precedence_and_legacy_horizontal_still_works(self):
        world = ToolDeltaWorldAdapter()
        for facing in ('up', 'down', 'north', 'south', 'west', 'east'):
            states = {'minecraft:facing_direction': facing,
                      'minecraft:cardinal_direction': 'north'}
            world.block_states_at = lambda key: states
            self.assertEqual(world.facing_at(fixtures.key(1)), facing)
        states = {'minecraft:cardinal_direction': 'east'}
        self.assertEqual(world.facing_at(fixtures.key(1)), 'east')
        states = {}
        self.assertIsNone(world.facing_at(fixtures.key(1)))


class VerticalRoutes(fixtures.PortFixture):
    def test_legacy_facing_migrates_once_and_new_archive_uses_world_direction(self):
        calls = []
        self.ports.migrate_facing = lambda key, facing: calls.append(facing) or True
        raw = self.state.archive_payload()
        raw['schema_version'] = 2
        raw['facing'] = 'east'
        self.state.facing = 'south'
        self.assertTrue(self.ports._apply_archive(self.state, raw))
        self.assertEqual(self.state.facing, 'east')
        self.assertEqual(self.registry.get_by_ref(self.ref).facing, 'east')
        self.assertEqual(calls, ['east'])
        self.assertGreater(self.ports.pending_archive_writes(), 0)
        raw = self.state.archive_payload()
        self.state.facing = 'up'
        self.assertTrue(self.ports._apply_archive(self.state, raw))
        self.assertEqual(self.state.facing, 'up')
        self.assertEqual(calls, ['east'])

    def test_failed_legacy_facing_migration_freezes_without_overwriting_archive(self):
        self.ports.migrate_facing = lambda key, facing: False
        raw = self.state.archive_payload()
        raw['schema_version'] = 2
        self.assertFalse(self.ports._apply_archive(self.state, raw))
        self.assertEqual(self.state.hold, Reason.BAD_PARAMETER)
        self.assertIn(node_key_text(self.ref.node_key), self.ports.frozen_ports())
        self.assertEqual(raw['schema_version'], 2)

    def test_output_connects_only_facing_container_in_all_six_directions(self):
        self.configure(cache_enabled=False)
        for index, facing in enumerate(('up', 'down', 'north', 'south', 'west', 'east')):
            self.state.facing = facing
            target = self.state.target_key()
            box = FakeContainer(slots=4)
            self.containers.boxes[target.as_key()] = box
            self.result('direction-%d' % index)
            self.assertGreater(box.count_of(fixtures.STONE), 0)
            self.network_stub.stored[fixtures.STONE] = 40
            del self.containers.boxes[target.as_key()]
            # Previously installed neighbors remain irrelevant, including the
            # fixture's original east container when facing vertically.
            self.assertIn(Reason.NO_CONTAINER, self.result('missing-%d' % index).reason_codes())

    def test_vertical_intake_still_uses_real_buffer_then_network(self):
        for facing in ('up', 'down'):
            self.state.device_id = fixtures.INPUT
            self.state.role = fixtures.INTAKE
            self.state.facing = facing
            self.configure()
            cache = FakeContainer(slots=5)
            box = FakeContainer(slots=4)
            box.set_slot(0, ContainerEntry(fixtures.STONE, 3))
            self.containers.boxes[self.ref.node_key.as_key()] = cache
            self.containers.boxes[self.state.target_key().as_key()] = box
            before = self.network_stub.stored[fixtures.STONE]
            self.result('intake-' + facing)
            self.assertEqual(cache.count_of(fixtures.STONE), 3)
            self.assertEqual(self.network_stub.stored[fixtures.STONE], before)
            self.result('drain-' + facing)
            self.assertEqual(cache.count_of(fixtures.STONE), 0)
            self.assertEqual(self.network_stub.stored[fixtures.STONE], before + 3)


class IntakeRules(fixtures.PortFixture):
    device_id = fixtures.INPUT

    def setUp(self):
        fixtures.PortFixture.setUp(self)
        self.cache = FakeContainer(slots=5)
        self.containers.boxes[self.ref.node_key.as_key()] = self.cache

    def test_new_device_blacklist_is_empty_and_accepts_all(self):
        self.assertEqual(self.state.config.filter_mode, MODE_BLACKLIST)
        self.box.set_slot(0, ContainerEntry(fixtures.DIRT, 7))
        self.result('stage')
        self.assertEqual(self.cache.count_of(fixtures.DIRT), 7)
        self.assertNotIn(fixtures.DIRT, self.network_stub.stored)
        self.result('drain')
        self.assertEqual(self.network_stub.stored[fixtures.DIRT], 7)

    def test_empty_whitelist_does_not_prevent_manual_stock_draining(self):
        self.configure(filter=())
        self.cache.set_slot(0, ContainerEntry(fixtures.DIRT, 7))
        self.containers.boxes.pop(self.target_ref.node_key.as_key())
        self.assertIsNone(port_blocker(self.state))
        self.result()
        self.assertEqual(self.network_stub.stored[fixtures.DIRT], 7)

    def test_poll_without_facing_container_can_drain(self):
        self.cache.set_slot(0, ContainerEntry(fixtures.DIRT, 7))
        self.containers.boxes.pop(self.target_ref.node_key.as_key())
        master = fixtures._Master()
        master.submit = lambda cmd: self.dispatcher.dispatch(self.state, cmd.request_id)
        runner = PortRunner(self.ports, PortGateway(fixtures._FakeRuntime(master=master)))
        self.assertTrue(runner._poll(fixtures._Task(self.ref)).changed)
        self.assertEqual(self.network_stub.stored[fixtures.DIRT], 7)

    def test_full_network_does_not_prefetch(self):
        self.box.set_slot(0, ContainerEntry(fixtures.DIRT, 7))
        self.network_stub.simulate_insert = lambda key, count: {'accepted': 0}
        self.result()
        self.assertEqual(self.box.count_of(fixtures.DIRT), 7)
        self.assertEqual(self.cache.entries(), [])

    def test_partial_cache_returns_to_external_source(self):
        self.cache = fixtures._HalfFillingBox(accept=3, slots=5)
        self.containers.boxes[self.ref.node_key.as_key()] = self.cache
        self.box.set_slot(0, ContainerEntry(fixtures.DIRT, 7))
        self.result()
        self.assertEqual(self.cache.count_of(fixtures.DIRT), 3)
        self.assertEqual(self.box.count_of(fixtures.DIRT), 4)

    def test_partial_network_returns_to_real_cache(self):
        self.cache.set_slot(0, ContainerEntry(fixtures.DIRT, 7))
        original = self.network_stub.commit_insert
        self.network_stub.commit_insert = lambda op, key, count, owner='': original(op, key, min(count, 3), owner)
        self.result()
        self.assertEqual(self.cache.count_of(fixtures.DIRT), 4)
        self.assertEqual(self.network_stub.stored[fixtures.DIRT], 3)

    def test_missing_or_wrong_cache_never_falls_back_to_direct(self):
        self.box.set_slot(0, ContainerEntry(fixtures.DIRT, 7))
        for cache in (None, FakeContainer(slots=4)):
            if cache is None:
                self.containers.boxes.pop(self.ref.node_key.as_key())
            else:
                self.containers.boxes[self.ref.node_key.as_key()] = cache
            self.assertIn(Reason.NO_ADAPTER, self.result(str(cache)).reason_codes())
            self.assertEqual(self.box.count_of(fixtures.DIRT), 7)

    def test_rotates_ids_and_variants_without_losing_metadata(self):
        special = ResourceKey.item('minecraft:stone', aux=2, user_data={'tag': 'x'})
        self.box.set_slot(0, ContainerEntry(fixtures.STONE, 12))
        self.box.set_slot(1, ContainerEntry(fixtures.DIRT, 12))
        self.box.set_slot(2, ContainerEntry(special, 12))
        self.policy.max_batch = 1
        for index in range(8):
            self.result('step%d' % index)
        self.assertEqual(self.network_stub.stored[fixtures.DIRT], 2)
        self.assertEqual(self.network_stub.stored[fixtures.STONE], 41)
        self.assertEqual(self.network_stub.stored[special], 1)

    def test_filter_change_only_affects_future_suction(self):
        self.box.set_slot(0, ContainerEntry(fixtures.DIRT, 7))
        self.result('stage')
        self.configure(filter=(fixtures.DIRT,), filter_mode=MODE_BLACKLIST)
        self.result('drain')
        self.assertEqual(self.network_stub.stored[fixtures.DIRT], 7)

    def test_round_trip_keeps_cursor_and_does_not_recreate_items(self):
        self.box.set_slot(0, ContainerEntry(fixtures.DIRT, 7))
        self.result('stage')
        raw = json.loads(json.dumps(self.ports.save_all()))
        other = PortStore(None, self.registry, containers=self.containers)
        other.restore(raw)
        restored = other.get(self.ref.node_key)
        self.assertEqual(restored.cursor, self.state.cursor)
        self.assertEqual(self.cache.count_of(fixtures.DIRT), 7)
        self.assertEqual(restored.buffer.total(), 0)

    def test_invalid_config_and_future_schema_freeze_without_writing(self):
        for field, value in (('schema_version', 999), ('config', {'filter_mode': 'invalid'})):
            raw = self.state.archive_payload()
            raw[field] = value
            self.assertFalse(self.ports._apply_archive(self.state, raw))
            self.assertTrue(self.state.hold)
            self.assertIn(node_key_text(self.state.node_key), self.ports.frozen_ports())

    def test_connection_view_tracks_container_changes(self):
        from endernet.server.devices.bus_io import config_view
        self.assertEqual(config_view(self.state, self.policy, self.ports)['facing_status'], '外部容器：已接入')
        self.containers.boxes.pop(self.target_ref.node_key.as_key())
        self.assertEqual(config_view(self.state, self.policy, self.ports)['facing_status'], '外部容器：未接入')

    def test_disabled_port_does_not_drain(self):
        self.configure(enabled=False)
        self.assertIsNotNone(port_blocker(self.state))


class OutputRules(fixtures.PortFixture):
    def setUp(self):
        fixtures.PortFixture.setUp(self)
        self.cache = FakeContainer(slots=5)
        self.containers.boxes[self.ref.node_key.as_key()] = self.cache
        self.configure(filter=(fixtures.STONE, fixtures.DIRT), limit=1)
        self.network_stub.stored[fixtures.DIRT] = 50

    def test_fair_delivery_advances_only_after_delivery(self):
        self.result('stage')
        self.assertEqual(self.state.cursor, {})
        for index in range(1, 8):
            self.result('step%d' % index)
        self.assertEqual(self.box.count_of(fixtures.DIRT), 2)
        self.assertEqual(self.box.count_of(fixtures.STONE), 2)

    def test_blocked_candidate_does_not_starve_other_item(self):
        self.box = FakeContainer(slots=1, max_stack=64)
        self.box.set_slot(0, ContainerEntry(fixtures.STONE, 1))
        self.containers.boxes[self.target_ref.node_key.as_key()] = self.box
        self.result('stage')
        self.result('deliver')
        self.assertEqual(self.box.count_of(fixtures.STONE), 2)

    def test_disabled_cache_is_take_only_and_reenable_allows_put(self):
        self.configure(cache_enabled=False)
        self.assertFalse(self.ports.accepts_container_put(self.ref.node_key))
        self.assertTrue(self.ports.accepts_container_put(self.target_ref.node_key))
        self.configure(cache_enabled=True)
        self.assertTrue(self.ports.accepts_container_put(self.ref.node_key))

    def test_direct_mode_rotates_actual_variants(self):
        special = ResourceKey.item('minecraft:stone', aux=4, durability=5)
        self.network_stub.stored[special] = 50
        self.configure(filter=(fixtures.STONE,), cache_enabled=False, limit=1)
        self.result('a')
        self.result('b')
        self.assertEqual(self.box.count_of(fixtures.STONE), 1)
        self.assertEqual(self.box.count_of(special), 1)


class ConfigCommands(fixtures.BootstrapFixture):
    def test_mode_switch_restores_separate_lists(self):
        self.build_network(port_device=fixtures.INPUT)
        ref = self.ref_at(fixtures.key(3))
        def configure(request, **config):
            result = self.submit(request, ref, 'port_configure', config=config)
            self.assertTrue(result.is_applied(), result.reason_codes())
            return self.server.ports.get_by_ref(ref).config
        first = configure('black', filter=[fixtures.STONE.to_dict()])
        self.assertEqual(first.filter_mode, MODE_BLACKLIST)
        white = configure('switch', filter_mode=MODE_WHITELIST)
        self.assertEqual(white.filter, ())
        configure('white', filter=[fixtures.DIRT.to_dict()])
        black = configure('back', filter_mode=MODE_BLACKLIST)
        self.assertEqual(black.filter, (fixtures.STONE,))
        self.assertEqual(black.item_filters[MODE_WHITELIST], [fixtures.DIRT.to_dict()])

    def test_inactive_list_limit_is_validated(self):
        self.build_network(port_device=fixtures.INPUT)
        ref = self.ref_at(fixtures.key(3))
        lists = {'blacklist': [], 'whitelist': [ResourceKey.item('test:item%d' % i).to_dict() for i in range(17)]}
        result = self.submit('invalid', ref, 'port_configure', config={'item_filters': lists})
        self.assertIn(Reason.FILTER_LIMIT_REACHED, result.reason_codes())
        self.assertEqual(self.server.ports.get_by_ref(ref).config.item_filters['whitelist'], [])

    def test_malformed_mode_is_rejected_instead_of_raising(self):
        self.build_network(port_device=fixtures.INPUT)
        ref = self.ref_at(fixtures.key(3))
        result = self.submit('invalid-mode', ref, 'port_configure', config={'filter_mode': []})
        self.assertIn(Reason.BAD_PARAMETER, result.reason_codes())

    def test_intake_master_replay_does_not_advance_to_next_leg(self):
        self.build_network(port_device=fixtures.INPUT)
        self.mount_a_disk()
        self.accrue_quota()
        ref = self.ref_at(fixtures.key(3))
        cache = self.containers.boxes[fixtures.key(3).as_key()]
        self.driver_box.set_slot(0, ContainerEntry(fixtures.STONE, 5))
        first = self.submit('stage-input', ref, 'port_dispatch', intent='intake')
        self.assertEqual(first.payload['route'], 'source_to_cache')
        self.assertEqual(cache.count_of(fixtures.STONE), 5)
        replay = self.submit('stage-input', ref, 'port_dispatch', intent='intake')
        self.assertEqual(replay.payload['route'], 'source_to_cache')
        self.assertEqual(cache.count_of(fixtures.STONE), 5)
        self.assertEqual(self.driver_box.count_of(fixtures.STONE), 0)
        drain = self.submit('drain-input', ref, 'port_dispatch', intent='intake')
        self.assertEqual(drain.payload['route'], 'cache_to_network')
        self.assertEqual(cache.count_of(fixtures.STONE), 0)


class ConfigMigration(unittest.TestCase):
    def test_old_mode_migrates_to_only_its_list_and_ids_deduplicate(self):
        config = PortConfig.from_dict({'filter': [fixtures.STONE.to_dict(), ResourceKey.item('minecraft:stone', aux=1).to_dict()]})
        normalize_item_config(config, fixtures.INPUT)
        self.assertEqual(config.filter_mode, MODE_WHITELIST)
        self.assertEqual(config.item_filters[MODE_BLACKLIST], [])
        self.assertEqual(config.filter, (fixtures.STONE,))
        self.assertEqual(config.to_dict(), PortConfig.from_dict(config.to_dict()).to_dict())

    def test_oversize_archive_is_not_truncated(self):
        config = PortConfig(filter=[ResourceKey.item('test:item%d' % i) for i in range(17)])
        with self.assertRaises(ValueError):
            normalize_item_config(config, fixtures.INPUT)
        self.assertEqual(len(config.filter), 17)

    def test_client_mode_preview_and_failed_edit_rollback(self):
        model = PortConfigModel(None)
        config = PortConfig(filter=(fixtures.STONE,), filter_mode=MODE_BLACKLIST)
        normalize_item_config(config, fixtures.INPUT)
        model.device_id = fixtures.INPUT
        model.applied = config.to_dict()
        model.desired = config.to_dict()
        model.expect('switch', {'filter_mode': MODE_WHITELIST})
        self.assertEqual(model.filter_names(), [])
        self.assertIn('不主动吸取', model.status_hint())
        model.settle('switch', config=model.desired)
        model.expect('edit', model.intent_filter(['minecraft:dirt']))
        self.assertEqual(model.filter_names(), ['minecraft:dirt'])
        model.settle('edit', detail='拒绝测试')
        self.assertEqual(model.filter_names(), [])
        self.assertEqual(model.status_hint(), '拒绝测试')


class FrozenPortReplacement(fixtures.PortFixture):
    def assert_frozen_record_is_preserved(self, ports, state, repository, raw):
        record_id = node_key_text(state.node_key)
        self.assertEqual(port_blocker(state).code, Reason.BAD_PARAMETER)
        with self.assertRaises(ValueError):
            ports.configure(state.ref, PortConfig())
        ports.note_changed(state)
        self.assertEqual(ports.save_to_repository(repository), 0)
        self.assertEqual(repository.load(PORT_RECORD_KIND, record_id).payload, raw)

    def test_frozen_payload_blocks_same_device_after_removal_and_replacement(self):
        repository = MemoryRepository()
        raw = self.state.archive_payload()
        raw['schema_version'] = 999
        record_id = node_key_text(self.state.node_key)
        repository.write(Record(PORT_RECORD_KIND, record_id, raw))
        ports = PortStore(None, self.registry, containers=self.containers)
        ports.restore_from_repository(repository)
        frozen = ports.ensure(self.ref, fixtures.OUTPUT_DEVICE, 'east')
        self.assertTrue(frozen.is_held())
        ports.drop(self.ref)
        replacement_ref = DeviceRef(self.ref.node_key, self.ref.generation + 1)
        self.registry.add(replacement_ref, fixtures.OUTPUT_DEVICE, 'east')
        replacement = ports.ensure(replacement_ref, fixtures.OUTPUT_DEVICE, 'east')
        self.assert_frozen_record_is_preserved(ports, replacement, repository, raw)

    def test_frozen_envelope_blocks_different_model_without_applying_old_config(self):
        repository = MemoryRepository()
        raw = self.state.archive_payload()
        raw['config']['cache_enabled'] = False
        record_id = node_key_text(self.state.node_key)
        repository.write(Record(PORT_RECORD_KIND, record_id, raw))
        repository._store[PORT_RECORD_KIND][record_id].schema_version = 99
        ports = PortStore(None, self.registry, containers=self.containers)
        ports.restore_from_repository(repository)
        self.registry.add(self.ref, fixtures.INPUT, 'east')
        replacement = ports.ensure(self.ref, fixtures.INPUT, 'east')
        self.assertTrue(replacement.config.cache_enabled)
        self.assertEqual(replacement.config.filter_mode, MODE_BLACKLIST)
        self.assert_frozen_record_is_preserved(ports, replacement, repository, raw)
        self.assertEqual(repository.load(PORT_RECORD_KIND, record_id).schema_version, 99)


class ContainerGuards(unittest.TestCase):
    def setUp(self):
        import test_bootstrap as bootstrap
        self.fixture = bootstrap.TestPatternContainerEvents()
        self.fixture.setUp()
        self.bootstrap = bootstrap
        self.ref = self.fixture.place(fixtures.OUTPUT_DEVICE, 7)
        self.fixture.server.ports.configure(self.ref, PortConfig(cache_enabled=False))

    def test_player_and_hopper_cannot_insert_even_when_item_is_unreadable(self):
        for name in ('PlayerTryPutCustomContainerItemServerEvent', 'ItemPushInCustomContainerServerEvent'):
            for item in ('minecraft:stone', ''):
                event = self.bootstrap._PutEvent(item, 7, 64, 0)
                event.dimension = 0
                self.fixture.api.dispatch(self.fixture.api.event(name), event)
                self.assertTrue(event.cancelled)

    def test_removal_is_not_blocked(self):
        for name in ('PlayerTryRemoveCustomContainerItemServerEvent', 'ItemPullOutCustomContainerServerEvent'):
            event = self.bootstrap._PutEvent('minecraft:stone', 7, 64, 0)
            event.dimension = 0
            self.fixture.api.dispatch(self.fixture.api.event(name), event)
            self.assertFalse(event.cancelled)

    def test_reenable_and_other_devices_accept_input(self):
        self.fixture.server.ports.configure(self.ref, PortConfig(cache_enabled=True))
        event = self.bootstrap._PutEvent('minecraft:stone', 7, 64, 0)
        self.fixture.api.dispatch(self.fixture.api.event('PlayerTryPutCustomContainerItemServerEvent'), event)
        self.assertFalse(event.cancelled)
