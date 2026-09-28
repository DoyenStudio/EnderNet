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


class TestAssembly(ReturnsFixture):
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
        self.assertEqual(slot_read(self.port_box, 9)["count"], 1)
        self.assertEqual(self.stored(BLOCK), 0)

    def test_power_loss_freezes_real_slots(self):
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: slot_read(self.port_box, 4)["count"] == 2)
        self.server.push(NormalizedEvent.remove(ENERGY_KEY, "endernet:creative_energy_source"))
        self.ticks()
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
