# coding=utf-8
"""公共流体提供者与主动接口、样板投料的领域接线。"""

import testsupport  # noqa: F401

from test_io import (
    BootstrapFixture, PortFixture, WATER, STONE, TEMPLATE_DEVICE, FLUID_BUS, key,
)
from endernet.common.protocol import Reason, STATUS_BLOCKED, STATUS_OK
from endernet.server.adapters.containers import ContainerEntry, FakeContainer
from endernet.server.devices.io import neighbor_key, port_blocker
from endernet.server.runtime.events import NormalizedEvent


class FluidProviderStub(object):
    def __init__(self, target, tank):
        self.target = target
        self.tank = tank
        self.calls = []

    def container_at(self, node_key, face):
        self.calls.append((node_key, face))
        return self.tank if node_key == self.target else None


class TestActiveFluidPorts(PortFixture):
    device_id = "endernet:bus_fluid_output_io"

    def setUp(self):
        PortFixture.setUp(self)
        self.tank = FakeContainer(slots=1, max_stack=1000)
        self.fluids = FluidProviderStub(self.state.target_key(), self.tank)
        self.ports.fluids = self.fluids
        self.network_stub.stored[WATER] = 100
        self.configure(filter=(WATER,))

    def test_output_uses_fluid_endpoint_and_leaves_item_container_untouched(self):
        self.assertIsNone(port_blocker(self.state, self.policy))
        result = self.result()
        self.assertEqual(result.payload["receipt"]["moved"], 32)
        self.assertEqual(self.network_stub.stored[WATER], 68)
        self.assertEqual(self.tank.count_of(WATER), 32)
        self.assertEqual(self.box.entries(), [])
        self.assertEqual(self.fluids.calls[-1], (self.state.target_key(), 4))

    def test_input_draws_from_public_fluid_port(self):
        self.state.device_id = "endernet:bus_fluid_input_io"
        self.state.role = "intake"
        self.tank.set_slot(0, ContainerEntry(WATER, 50))
        result = self.result()
        self.assertEqual(result.payload["receipt"]["moved"], 32)
        self.assertEqual(self.network_stub.stored[WATER], 132)
        self.assertEqual(self.tank.count_of(WATER), 18)
        self.assertEqual(self.box.entries(), [])

    def test_missing_fluid_provider_never_falls_back_to_item_container(self):
        self.ports.fluids = None
        result = self.result()
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn(Reason.NO_ADAPTER, result.reason_codes())
        self.assertEqual(self.network_stub.stored[WATER], 100)
        self.assertEqual(self.box.entries(), [])

    def test_late_provider_becomes_available_without_recreating_port(self):
        self.fluids.tank = None
        self.assertEqual(self.result("missing").status, STATUS_BLOCKED)
        self.fluids.tank = self.tank
        self.assertEqual(self.result("ready").payload["receipt"]["moved"], 32)

    def test_all_six_faces_use_target_absolute_face(self):
        for facing, face in (("up", 0), ("down", 1), ("north", 3),
                             ("south", 2), ("west", 5), ("east", 4)):
            self.state.facing = facing
            self.fluids.target = self.state.target_key()
            endpoint, reason, _ = self.ports.facing_endpoint(self.state)
            self.assertIsNotNone(endpoint)
            self.assertEqual(reason, "")
            self.assertEqual(self.fluids.calls[-1], (self.state.target_key(), face))

    def test_external_fluid_capability_rejects_items(self):
        receipt = self.state.fluid_port.insert("item", STONE, 2)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(self.tank.entries(), [])

    def test_passive_fluid_port_keeps_local_tank_blocker(self):
        self.state.device_id = FLUID_BUS
        self.state.role = "passive"
        self.assertEqual(port_blocker(self.state, self.policy).code, Reason.NO_ADAPTER)
        self.assertEqual(self.result().status, STATUS_BLOCKED)


class TestTemplateFluidInputs(PortFixture):
    device_id = TEMPLATE_DEVICE

    def test_fluid_batch_uses_fluid_capability_on_six_adjacent_faces(self):
        tank = FakeContainer(slots=1, max_stack=1000)
        target = neighbor_key(self.state.node_key, "west")
        fluids = FluidProviderStub(target, tank)
        self.ports.fluids = fluids
        self.network_stub.stored[WATER] = 100
        self.configure()
        self.dispatcher.stage_batch(self.state, WATER, 20, "batch", "stage")
        result = self.result("feed")
        self.assertEqual(result.payload["batch"]["delivered"], 20)
        self.assertEqual(tank.count_of(WATER), 20)
        self.assertEqual(self.box.entries(), [])
        self.assertEqual(self.network_stub.stored[WATER], 80)
        self.assertEqual(fluids.calls, [
            (neighbor_key(self.state.node_key, facing), face)
            for facing, face in (("up", 0), ("down", 1), ("north", 3),
                                 ("south", 2), ("west", 5), ("east", 4))])

    def test_item_batch_still_uses_item_provider(self):
        fluids = FluidProviderStub(self.state.target_key(), FakeContainer())
        self.ports.fluids = fluids
        self.configure()
        self.dispatcher.stage_batch(self.state, STONE, 10, "batch", "stage")
        self.result("feed")
        self.assertEqual(self.box.count_of(STONE), 10)
        self.assertEqual(fluids.calls, [])


class TestFluidPortsThroughMaster(BootstrapFixture):
    def prepare_fluid_network(self, device_id):
        self.world.facing[key(3).as_key()] = "east"
        self.build_network(port_device=device_id)
        self.mount_a_disk()
        self.tank = FakeContainer(slots=1, max_stack=1000)
        self.server.ports.fluids = FluidProviderStub(key(4), self.tank)
        ref = self.ref_at(key(3))
        configured = self.submit("config", ref, "port_configure", config={
            "enabled": True, "filter": [WATER.to_dict()]})
        self.assertEqual(configured.status, STATUS_OK)
        self.accrue_quota()
        return ref

    def test_output_passes_controller_admission_and_deduplicates(self):
        ref = self.prepare_fluid_network("endernet:bus_fluid_output_io")
        seeded = self.submit("seed", ref, "insert", key=WATER.to_dict(),
                             count=20, op_id="seed-fluid")
        self.assertEqual(seeded.status, STATUS_OK)
        result = self.submit("dispatch", ref, "port_dispatch", intent="output")
        self.assertEqual(result.status, STATUS_OK)
        moved = result.payload["receipt"]["moved"]
        self.assertGreater(moved, 0)
        self.assertEqual(self.tank.count_of(WATER), moved)
        self.submit("dispatch", ref, "port_dispatch", intent="output")
        self.assertEqual(self.tank.count_of(WATER), moved)

    def test_intake_passes_controller_admission_and_deduplicates(self):
        ref = self.prepare_fluid_network("endernet:bus_fluid_input_io")
        self.tank.set_slot(0, ContainerEntry(WATER, 50))
        result = self.submit("dispatch", ref, "port_dispatch", intent="intake")
        self.assertEqual(result.status, STATUS_OK)
        moved = result.payload["receipt"]["moved"]
        self.assertGreater(moved, 0)
        self.assertEqual(self.tank.count_of(WATER), 50 - moved)
        self.submit("dispatch", ref, "port_dispatch", intent="intake")
        self.assertEqual(self.tank.count_of(WATER), 50 - moved)


class TestFluidProviderLifecycle(BootstrapFixture):
    def setUp(self):
        from test_fluids import FakeRadium
        from endernet.server.adapters.fluids import RadiumFluidProvider

        BootstrapFixture.setUp(self)
        self.api = FakeRadium()
        self.provider = RadiumFluidProvider(lambda: self.api)
        self.server.fluids = self.provider
        self.server.ports.fluids = self.provider
        self.target = key(4)

    def test_replacement_and_neighbor_changes_revoke_all_faces(self):
        events = (
            NormalizedEvent.place(self.target, "example:machine"),
            NormalizedEvent.remove(self.target, "example:machine"),
            NormalizedEvent.neighbor_changed(key(3), self.target),
        )
        for event in events:
            old = [self.provider.container_at(self.target, face) for face in range(6)]
            self.server.push(event)
            self.server._drain_events()
            self.assertTrue(all(not adapter.is_valid() for adapter in old))
            self.assertTrue(self.provider.container_at(self.target, 4).is_valid())

    def test_chunk_load_and_unload_revoke_old_wrappers(self):
        for event in (NormalizedEvent.chunk_loaded(self.target.chunk_key()),
                      NormalizedEvent.chunk_unloaded(self.target.chunk_key())):
            old = self.provider.container_at(self.target, 4)
            self.server.push(event)
            self.server._drain_events()
            self.assertFalse(old.is_valid())
            self.assertTrue(self.provider.container_at(self.target, 4).is_valid())

    def test_stop_and_manual_rescan_revoke_old_wrappers(self):
        old = self.provider.container_at(self.target, 4)
        self.server.reset_backfill()
        self.assertFalse(old.is_valid())
        old = self.provider.container_at(self.target, 4)
        self.server.stop()
        self.assertFalse(old.is_valid())
