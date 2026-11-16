# coding=utf-8
"""Native EN generation joins brownout networks and shares atomic source settlement."""

import unittest
from unittest.mock import Mock

import testsupport  # noqa: F401
from endernet.common.definitions import DefinitionSet, CATEGORY_ENERGY_INPUT
from endernet.common.identity import NodeKey
from endernet.common.materials import GENERATOR_ID, GENERATOR_RATE
from endernet.common.protocol import Reason
from endernet.server.adapters.compat.energy import EnergyInputs, BALANCE_FIELD
from endernet.server.adapters.production import ProductionService, FE_MACHINE_IDS
from endernet.server.bootstrap import ServerBootstrap
from endernet.server.network.controller import RunState
from endernet.server.network.energy import EnergyPolicy
from endernet.server.network.services import ServiceId
from endernet.server.runtime.events import EventKind, NormalizedEvent
from test_production import FakeApi, Saved, make_machine, plain_item, STATE_FIELD, GENERATOR


def key(x, y=64):
    return NodeKey(0, x, y, 0)


class World(object):
    def __init__(self):
        self.entities = {}

    def energy_block_entity(self, node_key):
        return self.entities.setdefault(node_key, Saved())

    def list_loaded_chunks(self):
        return []

    def block_entities_in_chunk(self, chunk_key):
        return []

    def register_block_remove_listener(self, block_ids):
        pass


def sources(machines):
    inputs = EnergyInputs(World(), generator_loader=machines.get)
    inputs.api = FakeApi()
    for node_key in machines:
        inputs.load(node_key, GENERATOR_ID)
    return inputs


class GeneratorNetworkTests(unittest.TestCase):
    def test_generator_is_zero_demand_zero_bandwidth_source_supported_by_dos(self):
        definitions = DefinitionSet()
        generator = definitions.get(GENERATOR_ID)
        self.assertEqual(generator.category, CATEGORY_ENERGY_INPUT)
        self.assertEqual(generator.bandwidth.demand_quarters(), 0)
        self.assertEqual(generator.energy.value_for_state("idle"), 0)
        self.assertEqual(generator.energy.value_for_state("work"), 0)
        self.assertIsNotNone(generator.supply)
        self.assertIn(GENERATOR_ID, definitions.get("endernet:dos_network_endpoint").allowed_device_ids)

    def test_generator_never_exposes_a_fe_port(self):
        machine, _, _ = make_machine(GENERATOR, amount=1000)
        service = ProductionService()
        service.load = Mock(return_value=machine)
        self.assertNotIn(GENERATOR_ID, FE_MACHINE_IDS)
        for face in range(6):
            self.assertIsNone(service.provide_energy(0, (0, 64, 0), face))

    def test_native_en_and_fe_ports_fund_one_atomic_deficit_without_conversion_loss(self):
        machine, _, saved = make_machine(GENERATOR, amount=1000)
        machine.tick()
        inputs = sources({key(0): machine})
        inputs.world.entities[key(1)] = Saved({BALANCE_FIELD: 73})
        buffer = inputs.load(key(1))
        self.assertEqual(inputs.spend([key(0), key(1)], 100), {key(0): 64, key(1): 36})
        self.assertEqual(machine.energy.get_amount(), 936)
        self.assertEqual(saved[STATE_FIELD]["energy"], 936)
        self.assertEqual(buffer.storage.get_amount(), 1)
        self.assertEqual(inputs.spend([key(0)], 1), {})

    def test_shortfall_aborts_every_source_and_preserves_generator_output_budget(self):
        machine, _, _ = make_machine(GENERATOR, amount=1000)
        machine.tick()
        inputs = sources({key(0): machine})
        inputs.world.entities[key(1)] = Saved({BALANCE_FIELD: 21})
        buffer = inputs.load(key(1))
        self.assertEqual(inputs.spend([key(0), key(1)], 75), {})
        self.assertEqual(machine.energy.get_amount(), 1000)
        self.assertEqual(machine.budget.get_amount(), 64)
        self.assertEqual(buffer.storage.get_amount(), 21)
        self.assertEqual(inputs.spend([key(0), key(1)], 74), {key(0): 64, key(1): 10})

    def test_multiple_generators_combine_and_unloaded_or_stale_sources_cannot_pay(self):
        machines = dict((key(x), make_machine(GENERATOR, amount=1000)[0]) for x in range(2))
        for machine in machines.values():
            machine.tick()
        inputs = sources(machines)
        self.assertEqual(inputs.spend([key(0), key(1)], 100), {key(0): 64, key(1): 36})
        inputs.unload(key(0))
        machines[key(1)].alive = False
        self.assertEqual(inputs.spend([key(0), key(1)], 1), {})
        self.assertTrue(machines[key(0)].alive)

    def build(self, amount=0, fuel=False):
        machine, slots, _ = make_machine(GENERATOR, amount=amount)
        inputs = sources({key(2): machine})
        server = ServerBootstrap(world=inputs.world, energy_policy=EnergyPolicy.dev_stand_in())
        server.energy_inputs = inputs
        server.start(bind_engine=False)
        for node_key, device_id in (
                (key(0), "endernet:network_baseband_8"),
                (key(0, 65), "endernet:dos_network_endpoint"),
                (key(1), "endernet:ender_cable"),
                (key(2), GENERATOR_ID),
                (key(3), "endernet:common_terminal_panel")):
            server.push(NormalizedEvent.place(node_key, device_id))
        server.tick()
        if fuel:
            slots.items[0] = plain_item("minecraft:coal", 1)
        return server, machine

    def pulse(self, server):
        server.event_binder.counts[EventKind.SIM_TICK] = server.event_binder.counts.get(EventKind.SIM_TICK, 0) + 1
        server.tick()

    def test_empty_network_starts_from_generator_and_loses_power_when_its_buffer_empties(self):
        server, machine = self.build(fuel=True)
        runtime = server.runtimes.runtime_of(key(2))
        self.assertIsNotNone(runtime)
        self.assertEqual(runtime.state(), RunState.NO_POWER)
        status = server.runtimes.device_status(server.registry.get(key(2)).ref)
        self.assertTrue(status.is_online)
        self.assertNotIn(Reason.BROWNOUT, [reason.code for reason in status.reasons])
        machine.tick()
        self.assertEqual(machine.energy.get_amount(), GENERATOR_RATE)
        self.pulse(server)
        self.assertEqual(runtime.state(), RunState.RUNNABLE)
        energy = runtime.services.get(ServiceId.ENERGY)
        demand = energy.totals()["demand_total"]
        self.assertEqual(machine.energy.get_amount(), GENERATOR_RATE - demand)
        # A repeated server frame without a simulation pulse cannot charge twice.
        server.tick()
        self.assertEqual(machine.energy.get_amount(), GENERATOR_RATE - demand)
        for _ in range(GENERATOR_RATE // demand + 1):
            self.pulse(server)
        self.assertEqual(runtime.state(), RunState.NO_POWER)
        self.assertTrue(server.runtimes.device_status(server.registry.get(key(2)).ref).is_online)

    def test_static_supply_preserves_fuel_energy_and_generator_removal_stops_funding(self):
        server, machine = self.build(amount=1000)
        machine.tick()
        server.push(NormalizedEvent.place(key(1, 63), "endernet:creative_energy_source"))
        server.tick()
        self.pulse(server)
        self.assertEqual(machine.energy.get_amount(), 1000)
        server.push(NormalizedEvent.remove(key(2), GENERATOR_ID))
        server.tick()
        self.assertNotIn(key(2), server._energy_nodes)
        self.assertNotIn(key(2), server.energy_inputs.generators)


if __name__ == "__main__":
    unittest.main()
