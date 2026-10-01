# coding=utf-8
"""D-05：真实订单/转移/回流服务，只有引擎容器使用离线替身。"""
import testsupport  # noqa: F401

from test_returns import (
    ReturnsFixture, PORT_KEY, TEMPLATE_DEVICE, IRON, BLOCK, GOLD,
    PROCESSOR_KEY, ENERGY_KEY,
    key, CONNECTOR, PROCESSOR,
)
from endernet.server.adapters.containers import FakeContainer
from endernet.server.crafting.patterns import PatternDefinition, PatternStack, MODE_CRAFTING
from endernet.server.storage.transfer import slot_read
from endernet.common.item_codec import item_dict_from_key
from endernet.common.protocol import Reason, DeviceCommand, CommandKind, CommandSource
from endernet.server.runtime.events import NormalizedEvent
from endernet.server.network.services import ServiceId
from endernet.server.network.node import NetworkNode
from endernet.common.definitions import Value


class TestAssembly(ReturnsFixture):
    def energy_member(self, node_key):
        runtime = self.server.runtimes.runtime_of(node_key)
        return runtime.services.get(ServiceId.ENERGY)._members[node_key]

    def test_energy_work_interval_ends_on_completion(self):
        self.assertEqual(self.energy_member(PROCESSOR_KEY).state, "idle")
        self.assertEqual(self.energy_member(PORT_KEY).state, "idle")
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: bool(self.server.patterns.assembly.leases))
        self.assertEqual(self.energy_member(PROCESSOR_KEY).state, "work")
        self.assertEqual(self.energy_member(PROCESSOR_KEY).demand, 32)
        self.assertEqual(self.energy_member(PORT_KEY).state, "work")
        self.assertEqual(self.energy_member(PORT_KEY).demand, 8)
        self.drive_until(lambda: self.current().status == "completed")
        self.assertEqual(self.energy_member(PROCESSOR_KEY).state, "idle")
        self.assertEqual(self.energy_member(PORT_KEY).state, "idle")

    def test_waiting_for_output_space_remains_work_and_cancel_clears_immediately(self):
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: bool(self.server.patterns.assembly.leases))
        self.port_box.write_slot(9, item_dict_from_key(GOLD, 1))
        self.ticks()
        self.assertEqual(self.energy_member(PORT_KEY).state, "work")
        self.assertEqual(self.energy_member(PROCESSOR_KEY).state, "work")
        result = self.server.submit_command(
            self.order_command("cancel", "cancel-energy", order_id=self.current().order_id))
        self.assertFalse(result.reasons, result.to_dict())
        self.assertEqual(self.energy_member(PORT_KEY).state, "idle")
        self.assertEqual(self.energy_member(PROCESSOR_KEY).state, "idle")
        self.assertEqual(slot_read(self.port_box, 9)["count"], 1)

    def test_work_energy_shortfall_blocks_before_first_material_move(self):
        energy = self.runtime.services.get(ServiceId.ENERGY)
        source = self.server.definitions.get("endernet:creative_energy_source")
        original_supply = source.supply
        self.addCleanup(setattr, source, "supply", source.supply)
        # 刚好够处理器工作，但不够装配仓从空闲切到工作。
        source.supply = Value(energy.totals()["demand_total"] +
                              32 - self.energy_member(PROCESSOR_KEY).demand)
        energy.add_node(NetworkNode.from_record(
            self.server.definitions, self.server.registry.get(ENERGY_KEY)))
        self.server.tick()
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: bool(self.server.patterns.assembly.leases))
        self.assertIs(energy.power_ok(PORT_KEY), False)
        self.assertEqual(slot_read(self.port_box, 4)["count"], 0)
        storage = self.runtime.services.get(ServiceId.STORAGE)
        self.assertEqual(storage.query(IRON)["stored"], 2)
        self.assertEqual(storage.query(BLOCK)["stored"], 0)
        source.supply = original_supply
        energy.add_node(NetworkNode.from_record(
            self.server.definitions, self.server.registry.get(ENERGY_KEY)))
        self.drive_until(lambda: self.current().status == "completed")
        self.assertEqual(storage.query(BLOCK)["stored"], 1)
        self.assertEqual(self.energy_member(PORT_KEY).state, "idle")

    def test_parallel_leases_count_processors_once_and_shrink_keeps_existing_leases(self):
        for index in (0, 2, 3):
            self.place(key(index, 64, 1), CONNECTOR)
            self.place(key(index, 64, 2), PROCESSOR)
        self.server.tick()
        self.seed(IRON, 8)
        self.submit_order(amount=4)
        self.drive_until(lambda: self.service.slots.used() == 4)
        energy = self.runtime.services.get(ServiceId.ENERGY)
        rows = [row for row in energy.member_view() if row["device_id"] == PROCESSOR]
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(row["demand"] for row in rows), 4 * 32)
        self.assertEqual(len(self.server.patterns.assembly.leases), 1)
        self.assertEqual(self.energy_member(PORT_KEY).demand, 8)
        # 暂停不释放租约；移除一台后只向当前网络里的三台计费。
        self.service.executor.pause(self.current().order_id)
        self.server.push(NormalizedEvent.remove(key(3, 64, 2), PROCESSOR))
        self.server.tick()
        rows = [row for row in energy.member_view() if row["device_id"] == PROCESSOR]
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(row["demand"] for row in rows), 3 * 32)
        self.assertEqual(self.service.slots.used(), 4)
        self.service.executor.cancel(self.current().order_id)
        rows = [row for row in energy.member_view() if row["device_id"] == PROCESSOR]
        self.assertTrue(all(row["state"] == "idle" for row in rows))

    def test_old_assembly_release_does_not_change_replacement_state(self):
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: bool(self.server.patterns.assembly.leases))
        self.service.executor.pause(self.current().order_id)
        previous = self.ref_at(PORT_KEY)
        self.server.push(NormalizedEvent.remove(PORT_KEY, "endernet:assemble_chamber"))
        self.server.tick()
        self.place(PORT_KEY, "endernet:assemble_chamber")
        self.server.tick()
        self.assertFalse(previous.matches(self.ref_at(PORT_KEY)))
        self.assertEqual(self.energy_member(PORT_KEY).state, "idle")
        self.service.executor.cancel(self.current().order_id)
        self.assertEqual(self.energy_member(PORT_KEY).state, "idle")

    def test_processor_energy_is_scoped_to_its_network(self):
        # 第二张独立网络只提供槽位，不接订单；第一张的租约不能点亮它。
        self.place(key(30), "endernet:network_baseband_8")
        self.place(key(30, 65), "endernet:tiny_network_endpoint")
        self.place(key(30, 64, 1), CONNECTOR)
        self.place(key(30, 64, 2), PROCESSOR)
        self.server.tick()
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: bool(self.server.patterns.assembly.leases))
        self.assertEqual(self.energy_member(PROCESSOR_KEY).state, "work")
        self.assertEqual(self.energy_member(key(30, 64, 2)).state, "idle")

    def place(self, node_key, device_id):
        if device_id == TEMPLATE_DEVICE:
            device_id = "endernet:assemble_chamber"
        return ReturnsFixture.place(self, node_key, device_id)

    def build_network(self):
        self.port_box = FakeContainer(slots=19, max_stack=64)
        self.patterns = {10: PatternDefinition(
            MODE_CRAFTING, [PatternStack(IRON, 2, slot=4)],
            [PatternStack(BLOCK, 1)])}
        ReturnsFixture.build_network(self)

    def drive_until(self, predicate, limit=150):
        for _ in range(limit):
            self.server.clock.advance(20)
            self.server.tick()
            if predicate():
                return
        self.fail(str([(order.status, [instance.to_dict() for instance in order.inflight.values()])
                       for order in self.service.orders.orders()]))

    def current(self):
        return self.service.orders.orders()[0]

    def instance(self):
        return self.current().steps[0].executions[0]

    def ticks(self, count=30):
        for _ in range(count):
            self.server.tick()

    def install(self, inputs, outputs, returns=()):
        ok, detail = self.server.patterns.install(self.ref_at(PORT_KEY), 10,
                                                PatternDefinition(MODE_CRAFTING, inputs, outputs, returns))
        self.assertTrue(ok, detail)

    def test_multiple_instances_do_not_mix_materials(self):
        for index in (0, 2, 3):
            self.place(key(index, 64, 1), CONNECTOR)
            self.place(key(index, 64, 2), PROCESSOR)
        self.server.tick()
        self.seed(IRON, 8)
        self.submit_order(amount=4)
        self.drive_until(lambda: self.service.slots.used() == 4)
        self.assertEqual(len(self.server.patterns.assembly.leases), 1)
        self.drive_until(lambda: self.current().status == "completed", 300)
        self.assertEqual(self.stored(BLOCK), 4)
        self.assertEqual(self.stored(IRON), 0)
        self.assertEqual(self.current().steps[0].completed, 4)
        self.assertEqual(self.server.patterns.assembly.leases, {})

    def test_output_occupied_does_not_consume_and_recovers(self):
        self.port_box.write_slot(9, item_dict_from_key(GOLD, 1))
        self.seed(IRON, 2)
        self.submit_order()
        self.ticks()
        self.assertEqual(self.stored(IRON), 2)
        self.assertEqual(self.stored(BLOCK), 0)
        self.port_box.write_slot(9, None)
        self.drive_until(lambda: self.current().status == "completed")
        self.assertEqual(self.stored(BLOCK), 1)

    def test_missing_inputs_do_not_create_output(self):
        self.seed(IRON, 1)
        self.submit_order()
        self.ticks()
        self.assertEqual(self.stored(IRON), 1)
        self.assertEqual(self.stored(BLOCK), 0)
        self.assertEqual(slot_read(self.port_box, 9)["count"], 0)

    def test_no_processor_slot_does_not_feed(self):
        self.server.push(NormalizedEvent.remove(PROCESSOR_KEY, "endernet:parallel_processor"))
        self.server.tick()
        self.seed(IRON, 2)
        self.submit_order()
        self.ticks()
        self.assertEqual(self.stored(IRON), 2)
        self.assertEqual(slot_read(self.port_box, 4)["count"], 0)

    def test_unordered_multiple_inputs_outputs_and_returns(self):
        self.install([PatternStack(IRON, 2), PatternStack(GOLD, 1)],
                     [PatternStack(BLOCK, 2)], [PatternStack(GOLD, 1)])
        self.seed(IRON, 2)
        self.seed(GOLD, 1, "gold")
        self.submit_order(amount=2)
        self.drive_until(lambda: self.current().status == "completed")
        self.assertEqual(self.stored(IRON), 0)
        self.assertEqual(self.stored(GOLD), 1)
        self.assertEqual(self.stored(BLOCK), 2)

    def test_output_larger_than_stack_is_drained_in_chunks(self):
        self.install([PatternStack(IRON, 2)], [PatternStack(BLOCK, 130)])
        self.seed(IRON, 2)
        self.submit_order(amount=130)
        self.drive_until(lambda: self.current().status == "completed")
        self.assertEqual(self.stored(BLOCK), 130)
        self.assertEqual(slot_read(self.port_box, 9)["count"], 0)

    def test_cancel_after_feed_preserves_real_material(self):
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: slot_read(self.port_box, 4)["count"] == 2)
        order = self.current()
        self.server.submit_command(self.order_command("cancel", "cancel", order_id=order.order_id))
        self.ticks()
        self.assertEqual(order.status, "cancelled")
        self.assertEqual(slot_read(self.port_box, 4)["count"], 2)
        self.assertEqual(self.stored(BLOCK), 0)
        self.assertTrue(order.residue)
        self.assertEqual(self.service.slots.used(), 0)

    def test_cancel_after_production_does_not_generate_again(self):
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: slot_read(self.port_box, 9)["count"] == 1)
        order = self.current()
        self.server.submit_command(self.order_command("cancel", "cancel", order_id=order.order_id))
        self.ticks()
        self.assertEqual(slot_read(self.port_box, 9)["count"], 1)
        self.assertEqual(self.stored(BLOCK), 0)
        self.assertTrue(order.residue[0]["assembly"]["output"])

    def test_write_then_raise_never_replays_production(self):
        self.seed(IRON, 2)
        self.submit_order()
        write = self.port_box.write_slot

        def broken(slot, data):
            result = write(slot, data)
            if slot == 9 and data is not None:
                raise RuntimeError("write acknowledged too late")
            return result

        self.port_box.write_slot = broken
        self.drive_until(lambda: slot_read(self.port_box, 9)["count"] == 1)
        self.port_box.write_slot = write
        self.ticks()
        self.assertTrue(self.instance().assembly["uncertain"])
        self.assertEqual(self.energy_member(PORT_KEY).state, "work")
        self.assertEqual(self.energy_member(PROCESSOR_KEY).state, "work")
        self.assertEqual(slot_read(self.port_box, 9)["count"], 1)
        self.assertEqual(self.stored(BLOCK), 0)

    def test_power_loss_freezes_real_slots(self):
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: slot_read(self.port_box, 4)["count"] == 2)
        self.server.push(NormalizedEvent.remove(ENERGY_KEY, "endernet:creative_energy_source"))
        self.ticks()
        self.assertEqual(self.energy_member(PORT_KEY).state, "work")
        self.assertEqual(self.energy_member(PROCESSOR_KEY).state, "work")
        self.assertEqual(slot_read(self.port_box, 4)["count"], 2)
        self.assertEqual(slot_read(self.port_box, 9)["count"], 0)
        self.place(ENERGY_KEY, "endernet:creative_energy_source")
        self.drive_until(lambda: self.current().status == "completed")
        self.assertEqual(self.stored(BLOCK), 1)

    def test_provider_return_capability_only_drains_existing_output(self):
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: slot_read(self.port_box, 9)["count"] == 1)
        provider = self.server.patterns.get_by_ref(self.ref_at(PORT_KEY)).provider_port
        execution_id = self.instance().execution_id
        bad = provider.accept_returns(execution_id, [(GOLD, 1)])
        self.assertEqual(bad.reason, Reason.BAD_PARAMETER)
        receipt = provider.accept_returns(execution_id, [(BLOCK, 1)])
        self.assertEqual(receipt.moved, 1)
        self.assertEqual(provider.accept_returns(execution_id, [(BLOCK, 1)]).moved, 0)
        self.drive_until(lambda: self.current().status == "completed")
        self.assertEqual(self.stored(BLOCK), 1)

    def test_full_storage_retains_output_and_retries_only_remainder(self):
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: slot_read(self.port_box, 9)["count"] == 1)
        space = self.storage_service.simulate_insert
        self.storage_service.simulate_insert = lambda *args, **kwargs: {"accepted": 0}
        self.ticks()
        self.assertEqual(slot_read(self.port_box, 9)["count"], 1)
        self.assertEqual(self.stored(BLOCK), 0)
        self.storage_service.simulate_insert = space
        self.drive_until(lambda: self.current().status == "completed")
        self.assertEqual(self.stored(BLOCK), 1)

    def test_duplicate_master_command_does_not_collect_twice(self):
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: slot_read(self.port_box, 9)["count"] == 1)
        ref = self.ref_at(PORT_KEY)
        command = DeviceCommand(
            "repeat-assembly", CommandKind.STORAGE,
            CommandSource(None, ref, 0, distance_ok=True),
            payload={"action": "assembly_advance", "order_id": self.current().order_id,
                     "execution_id": self.instance().execution_id},
            topology_version=self.server.topology.version, device_ref=ref)
        first = self.server.submit_command(command)
        second = self.server.submit_command(command)
        self.assertEqual(first.payload["moved"], 1)
        self.assertEqual(second.payload["moved"], 1)
        self.assertIn(Reason.DUPLICATE_REQUEST, second.reason_codes())
        self.drive_until(lambda: self.current().status == "completed")
        self.assertEqual(self.stored(BLOCK), 1)

    def test_player_cannot_invoke_internal_assembly_command(self):
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: slot_read(self.port_box, 9)["count"] == 1)
        result = self.storage("spoof", "assembly_advance", ref=self.ref_at(PORT_KEY),
                              order_id=self.current().order_id,
                              execution_id=self.instance().execution_id)
        self.assertIn(Reason.BAD_PARAMETER, result.reason_codes())
        self.assertEqual(slot_read(self.port_box, 9)["count"], 1)

    def test_replaced_chamber_cannot_consume_old_batch(self):
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: slot_read(self.port_box, 4)["count"] == 2)
        self.server.push(NormalizedEvent.remove(PORT_KEY, "endernet:assemble_chamber"))
        self.server.tick()
        self.place(PORT_KEY, "endernet:assemble_chamber")
        self.ticks()
        self.assertEqual(slot_read(self.port_box, 4)["count"], 2)
        self.assertEqual(self.stored(BLOCK), 0)

    def test_edited_pattern_does_not_consume_staged_material(self):
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: slot_read(self.port_box, 4)["count"] == 2)
        self.install([PatternStack(IRON, 2, slot=4)], [PatternStack(BLOCK, 3)])
        self.ticks()
        self.assertEqual(slot_read(self.port_box, 4)["count"], 2)
        self.assertEqual(self.stored(BLOCK), 0)

    def test_uncertain_consumption_does_not_create_product(self):
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: slot_read(self.port_box, 4)["count"] == 2)
        write = self.port_box.write_slot

        def fail_consumption(slot, data):
            if slot == 4 and data is None:
                write(slot, data)
                raise RuntimeError("lost consumption acknowledgement")
            return write(slot, data)

        self.port_box.write_slot = fail_consumption
        self.ticks()
        self.port_box.write_slot = write
        self.ticks()
        self.assertTrue(self.instance().assembly["uncertain"])
        self.assertEqual(slot_read(self.port_box, 4)["count"], 0)
        self.assertEqual(self.stored(BLOCK), 0)

    def test_read_failure_before_staging_is_retryable(self):
        self.seed(IRON, 2)
        read = self.port_box.read_slot

        def fail_input_read(slot):
            if slot < 10:
                raise RuntimeError("unloaded container")
            return read(slot)

        self.port_box.read_slot = fail_input_read
        self.submit_order()
        self.ticks()
        self.assertEqual(self.stored(IRON), 2)
        self.port_box.read_slot = read
        self.drive_until(lambda: self.current().status == "completed")
        self.assertEqual(self.stored(BLOCK), 1)

    def test_false_write_acknowledgement_uses_actual_slot_changes(self):
        self.seed(IRON, 2)
        write = self.port_box.write_slot

        def false_ack(slot, data):
            write(slot, data)
            return False

        self.port_box.write_slot = false_ack
        self.submit_order()
        self.drive_until(lambda: self.current().status == "completed")
        self.assertEqual(self.stored(IRON), 0)
        self.assertEqual(self.stored(BLOCK), 1)
        self.assertEqual(slot_read(self.port_box, 4)["count"], 0)
        self.assertEqual(slot_read(self.port_box, 9)["count"], 0)

    def test_order_uses_real_slots_and_returns(self):
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: slot_read(self.port_box, 4)["count"] == 2)
        self.assertEqual(slot_read(self.port_box, 4)["key"], IRON)
        self.assertEqual(self.stored(BLOCK), 0)
        self.drive_until(lambda: slot_read(self.port_box, 9)["count"] == 1)
        self.assertEqual(slot_read(self.port_box, 4)["count"], 0)
        self.drive_until(lambda: self.current().status == "completed")
        self.assertEqual(self.stored(IRON), 0)
        self.assertEqual(self.stored(BLOCK), 1)
        self.assertEqual(self.service.slots.used(), 0)
        self.assertEqual(self.service.memory.used(), 0)
        self.assertTrue(self.port_box.read_slot(10))
        for _ in range(20):
            self.server.tick()
        self.assertEqual(self.stored(BLOCK), 1)
