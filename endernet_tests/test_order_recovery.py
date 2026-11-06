# coding=utf-8
"""订单经 JSON 存档、重建服务后继续；已派发实例不得重复投料。"""
import json
import test_assembly
import test_returns

import testsupport  # noqa: F401
from endernet.server.bootstrap import ServerBootstrap
from endernet.server.crafting.jobs import OrderStatus, StepStatus
from endernet.server.network.energy import EnergyPolicy
from endernet.server.network.services import ServiceId
from endernet.server.persistence.repository import MemoryRepository, Record
from endernet.server.runtime.events import NormalizedEvent
from endernet.server.storage.service import NETWORK_RECORD_KIND
from test_executor import OneLevel, IRON, BLOCK, PORT_KEY, DRIVER_KEY, DRIVER, BASEBAND, CONTROLLER, key


class RestartMixin(object):
    def restart(self, mutate=None):
        repo = self.server.repository or MemoryRepository()
        self.server.repository = repo
        self.server.persistence_enabled = True
        self.server.stop()
        # 内存仓库也走 JSON 往返，覆盖 tuple/资源键/非 ASCII 内容。
        for kind in list(repo._store):
            for record_id in repo.list_ids(kind):
                record = repo.load(kind, record_id)
                record.payload = json.loads(json.dumps(record.payload))
                repo.write(record)
        if mutate is not None:
            mutate(repo)
        self.sink = self.build_sink()
        if hasattr(self.sink, "auto"):
            self.sink.auto = False
        self.server = ServerBootstrap(
            world=self.world, repository=repo,
            energy_policy=EnergyPolicy.dev_stand_in(),
            container_provider=self.containers, returns_sink=self.sink)
        self.server.start(bind_engine=False)
        # FakeWorld 不枚举实体；从旧拓扑布局重放发现，不重造容器物品。
        for node_key, device_id in self.layout:
            self.server.push(NormalizedEvent.place(node_key, device_id))
        self.server.tick()
        self.runtime = self.server.runtimes.runtimes()[0]
        self.service = self.runtime.services.get(ServiceId.FACTORY)
        self.sink.storage = self.runtime.services.get(ServiceId.STORAGE)
        self.storage_service = self.runtime.services.get(ServiceId.STORAGE)
        self.returns = self.server.returns_sink
        self.port = self.service.returns
        self.accrue_quota()


class TestOrderRecovery(RestartMixin, OneLevel):
    def setUp(self):
        OneLevel.setUp(self)
        self.sink.auto = False
        self.layout = [(record.ref.node_key, record.device_id)
                       for record in self.server.registry.all_records()]

    def test_planning_order_restarts_planning_with_same_identity(self):
        self.seed(IRON, 20)
        order_id = self.submit_order(amount=3).payload["order_id"]
        self.restart()
        self.assertIsNotNone(self.service.orders.get(order_id))
        self.settle(order_id)
        self.assertTrue(self.service.orders.get(order_id).adopted)

    def test_waiting_order_keeps_progress_and_never_reissues_inputs(self):
        self.seed(IRON, 20)
        order_id = self.submit_order(amount=3).payload["order_id"]
        self.settle(order_id)
        self.drive(50)
        order = self.service.orders.get(order_id)
        before = [(instance.execution_id, instance.inputs[0].staged,
                   instance.inputs[0].delivered) for instance in order.executions()]
        self.assertTrue(before)
        self.assertTrue(all(instance.status == StepStatus.WAITING_OUTPUTS
                            for instance in order.executions()))
        memory = order.memory_bytes
        self.restart()
        order = self.service.orders.get(order_id)
        self.assertEqual(before, [(instance.execution_id, instance.inputs[0].staged,
                                 instance.inputs[0].delivered) for instance in order.executions()])
        self.assertEqual(self.service.memory.used(), memory)
        self.assertEqual(self.service.slots.used(), len(before))
        self.drive(20)
        self.assertEqual(before, [(instance.execution_id, instance.inputs[0].staged,
                                 instance.inputs[0].delivered) for instance in order.executions()])
        for _ in range(6):
            self.sink.flush(self.service.executor)
            self.drive(20)
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(self.service.memory.used(), 0)
        self.assertEqual(self.service.slots.used(), 0)

    def test_paused_order_keeps_pause_and_remaining_reservation(self):
        self.seed(IRON, 20)
        order_id = self.submit_order(amount=3).payload["order_id"]
        self.settle(order_id)
        self.order("pause", request_id="pause", order_id=order_id)
        remaining = sum(row.count for row in self.reservations(order_id))
        created = self.service.orders.get(order_id).steps[0].created
        self.restart()
        order = self.service.orders.get(order_id)
        self.assertTrue(order.paused)
        self.assertEqual(sum(row.count for row in self.reservations(order_id)), remaining)
        self.assertEqual(order.steps[0].created, created)
        self.service.import_order_book(self.service.export_order_book())
        self.assertEqual(self.service.orders.live_count(), 1)
        self.assertEqual(self.service.memory.used(), order.memory_bytes)

    def test_cancel_after_restart_releases_all_restored_resources(self):
        self.seed(IRON, 20)
        order_id = self.submit_order(amount=3).payload["order_id"]
        self.settle(order_id)
        self.drive(50)
        self.restart()
        self.order("cancel", request_id="cancel", order_id=order_id)
        self.assertEqual(self.service.orders.get(order_id).status, OrderStatus.CANCELLED)
        self.assertEqual(self.service.waiting.diagnostics()["instances"], 0)
        self.assertEqual(self.service.memory.used(), 0)
        self.assertEqual(self.service.slots.used(), 0)

    def test_missing_provider_keeps_visible_order_and_reservations(self):
        self.seed(IRON, 20)
        order_id = self.submit_order(amount=3).payload["order_id"]
        self.settle(order_id)
        remaining = sum(row.count for row in self.reservations(order_id))
        self.layout = [row for row in self.layout if row[0] != PORT_KEY]
        self.restart()
        order = self.service.orders.get(order_id)
        self.assertTrue(order.recovery_pending)
        self.assertEqual(order.status, OrderStatus.BLOCKED)
        self.assertEqual(sum(row.count for row in self.reservations(order_id)), remaining)
        self.assertEqual(self.service.memory.used(), order.memory_bytes)

    def test_controller_discovered_before_driver_waits_then_restores(self):
        self.seed(IRON, 20)
        order_id = self.submit_order(amount=3).payload["order_id"]
        self.settle(order_id)
        remaining = sum(row.count for row in self.reservations(order_id))
        self.layout = [row for row in self.layout if row[0] != DRIVER_KEY]
        self.restart()
        self.assertIsNotNone(self.service.orders.get(order_id))
        self.assertFalse(self.runtime.is_open())
        self.assertFalse(self.server.persistence_restore_error)
        self.place(DRIVER_KEY, DRIVER)
        self.drive(4)
        self.assertTrue(self.runtime.is_open())
        self.assertEqual(sum(row.count for row in self.reservations(order_id)), remaining)

    def assert_unknown_version_isolated(self, level):
        self.seed(IRON, 4)
        order_id = self.submit_order().payload["order_id"]
        original = []

        def mutate(repo):
            record = repo.load(NETWORK_RECORD_KIND, repo.list_ids(NETWORK_RECORD_KIND)[0])
            if level == "envelope":
                record.schema_version = 999
            elif level == "network":
                record.payload["schema_version"] = 999
            else:
                record.payload["orders"]["resume_version"] = 999
            original.append(json.dumps(record.to_dict(), sort_keys=True))
        self.restart(mutate)
        self.assertTrue(self.server.recovery.is_open())
        self.assertFalse(self.runtime.is_open())
        self.assertIn("unsupported", self.runtime.close_reason())
        self.assertIsNone(self.service.orders.get(order_id))
        # 另一张网及后续两次拓扑发布不应放开冻结的旧网，也不能连坐正常主控。
        self.place(key(30), BASEBAND)
        self.place(key(31), CONTROLLER)
        self.drive(4)
        healthy = self.server.runtimes.runtime_for(self.server.registry.get(key(31)).ref)
        self.assertTrue(healthy.is_open())
        self.assertFalse(self.runtime.open_if_ready())
        self.server.stop()
        repo = self.server.repository
        record = repo.load(NETWORK_RECORD_KIND, repo.list_ids(NETWORK_RECORD_KIND)[0])
        self.assertEqual(json.dumps(record.to_dict(), sort_keys=True), original[0])

    def test_unknown_order_version_isolated(self):
        self.assert_unknown_version_isolated("order")

    def test_unknown_network_version_isolated(self):
        self.assert_unknown_version_isolated("network")

    def test_unknown_envelope_version_isolated(self):
        self.assert_unknown_version_isolated("envelope")

    def test_partial_read_failure_blocks_writes_until_a_clean_restart(self):
        self.seed(IRON, 12)
        order_id = self.submit_order().payload["order_id"]
        self.restart()
        self.server.stop()
        repo = self.server.repository
        self.assertEqual(len(repo.list_ids(NETWORK_RECORD_KIND)), 1)
        record_id = repo.list_ids(NETWORK_RECORD_KIND)[0]
        # 先读入另一条记录，随后恰好在含真实订单的主控处读取失败。
        earlier_id = "0,-1,64,0"
        repo.write(Record(NETWORK_RECORD_KIND, earlier_id, {"schema_version": 1}))
        before = json.dumps(repo.snapshot_payloads(), sort_keys=True)
        repo.inject_fault("load", kind=NETWORK_RECORD_KIND, record_id=record_id)
        failed = ServerBootstrap(world=self.world, repository=repo)
        failed.start(bind_engine=False)
        for node_key, device_id in self.layout:
            failed.push(NormalizedEvent.place(node_key, device_id))
        self.assertIn(earlier_id, failed.archived_networks)
        self.assertNotIn(record_id, failed.archived_networks)
        self.assertFalse(failed.recovery.is_open())
        self.assertTrue(failed.persistence_restore_error)
        for _ in range(3):
            self.assertEqual(failed.tick()["persisted_records"], 0)
        self.assertEqual(failed.write_network_states(), 0)
        self.assertEqual(failed.flush_persistence(), 0)
        failed.stop()
        self.assertEqual(json.dumps(repo.snapshot_payloads(), sort_keys=True), before)

        self.restart()
        self.assertTrue(self.runtime.is_open())
        self.assertIsNotNone(self.service.orders.get(order_id))

    def test_repeated_restart_keeps_original_ids_and_sequence(self):
        self.seed(IRON, 20)
        order_id = self.submit_order(amount=3).payload["order_id"]
        self.settle(order_id)
        self.drive(50)
        self.restart()
        self.restart()
        self.assertEqual(self.service.orders.ids(), (order_id,))
        new_id = self.submit_order(request_id="second").payload["order_id"]
        self.assertNotEqual(new_id, order_id)
        self.assertEqual(int(new_id.rsplit("/", 1)[1]), 2)

    def test_an_order_already_belonging_to_another_master_stays_blocked(self):
        self.seed(IRON, 20)
        order_id = self.submit_order(amount=3).payload["order_id"]
        self.settle(order_id)
        self.service.orders.get(order_id).master_instance_id = "old-controller"
        self.restart()
        order = self.service.orders.get(order_id)
        self.assertTrue(order.recovery_error)
        self.assertEqual(order.status, OrderStatus.BLOCKED)
        self.assertEqual(self.service.memory.used(), order.memory_bytes)

    def test_receipt_dedup_preserves_partial_output_count(self):
        self.seed(IRON, 20)
        order_id = self.submit_order().payload["order_id"]
        self.settle(order_id)
        self.drive(50)
        instance = self.service.orders.get(order_id).executions()[0]
        instance.outputs[0].expected = 2
        self.assertEqual(self.service.executor.receive_output(instance.execution_id, BLOCK, 1, "partial"), 1)
        self.restart()
        instance = self.service.orders.get(order_id).executions()[0]
        self.assertEqual(instance.outputs[0].received, 1)
        self.assertEqual(self.service.executor.receive_output(instance.execution_id, BLOCK, 1, "partial"), 0)
        self.assertEqual(instance.outputs[0].received, 1)


class TestRealReturnsRecovery(RestartMixin, test_returns.OneLevel):
    def setUp(self):
        test_returns.OneLevel.setUp(self)
        self.layout = [(record.ref.node_key, record.device_id)
                       for record in self.server.registry.all_records()]

    def test_receipt_dedup_and_received_products_survive(self):
        self.seed(IRON, 20)
        order_id = self.submit_order(amount=3).payload["order_id"]
        self.drive(60)
        instance = self.service.orders.get(order_id).executions()[0]
        self.returns._entries(self.runtime.component_id)[0].seq = 100
        self.deposit(BLOCK, 1, "first-product")
        self.assertEqual(instance.outputs[0].received, 1)
        self.restart()
        order = self.service.orders.get(order_id)
        self.assertGreaterEqual(order.steps[0].completed, 1)
        self.assertIn((self.runtime.component_id, "first-product"), self.returns._ops)
        self.assertGreaterEqual(self.returns._seq, 100)
        for index in range(2):
            self.drive(60)
            self.deposit(BLOCK, 1, "later-product-%d" % index)
        self.drive(30)
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(self.storage_service.query(BLOCK)["stored"], 3)
        self.assertFalse(self.reservations(order_id))


class TestSharedOutputKey(RestartMixin, test_returns.ReturnsFixture):
    patterns = {0: test_returns.factory([test_returns.stack(IRON, 2)],
                                        [test_returns.stack(BLOCK, 2)],
                                        [test_returns.stack(BLOCK, 1)])}

    def start_waiting(self, required_returns=True):
        self.layout = [(record.ref.node_key, record.device_id)
                       for record in self.server.registry.all_records()]
        self.service.policy.returns_block_completion = required_returns
        self.configure_port()
        self.seed(IRON, 2)
        order_id = self.submit_order(amount=2).payload["order_id"]
        self.settle(order_id)
        self.drive(20)
        instance = self.service.orders.get(order_id).executions()[0]
        self.assertEqual(instance.status, StepStatus.WAITING_OUTPUTS)
        return order_id

    def test_same_product_and_required_return_across_restart(self):
        order_id = self.start_waiting()
        self.assertEqual(self.deposit(BLOCK, 1, "first").moved, 1)
        self.drive(3)
        self.assertEqual(self.service.orders.get(order_id).steps[0].completed, 0)
        self.restart()
        order = self.service.orders.get(order_id)
        instance = order.executions()[0]
        self.assertEqual((instance.outputs[0].received, instance.returns[0].received), (1, 0))
        repeat = test_returns.TransferRequest("first", "outside", "net", BLOCK, 1,
                                              origin=test_returns.ORIGIN_EXTERNAL)
        self.assertEqual(self.port(repeat, 1, 0), 0)
        self.assertEqual(self.deposit(BLOCK, 1, "second").moved, 1)
        self.drive(3)
        self.assertEqual((instance.outputs[0].received, instance.returns[0].received), (2, 0))
        self.assertEqual(order.steps[0].completed, 0)
        self.assertEqual(self.service.slots.used(), 1)
        self.assertEqual(self.deposit(BLOCK, 1, "third").moved, 1)
        self.drive(3)
        self.assertEqual((instance.outputs[0].received, instance.returns[0].received), (2, 1))
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(order.steps[0].completed, 1)
        self.assertEqual(self.service.slots.used(), 0)
        self.assertEqual(self.stored(BLOCK), 3)

    def test_same_optional_return_does_not_displace_required_product(self):
        order_id = self.start_waiting(required_returns=False)
        order = self.service.orders.get(order_id)
        instance = order.executions()[0]
        self.assertEqual(self.deposit(BLOCK, 1, "first").moved, 1)
        self.drive(3)
        self.assertEqual((instance.outputs[0].received, instance.returns[0].received), (1, 0))
        self.assertEqual(order.steps[0].completed, 0)
        self.assertEqual(self.deposit(BLOCK, 1, "second").moved, 1)
        self.drive(3)
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual((instance.outputs[0].received, instance.returns[0].received), (2, 0))
        self.assertEqual(self.stored(BLOCK), 2)


class TestAssemblyRecovery(RestartMixin, test_returns.ReturnsFixture):
    build_network = test_assembly.TestAssembly.build_network
    place = test_assembly.TestAssembly.place
    drive_until = test_assembly.TestAssembly.drive_until
    current = test_assembly.TestAssembly.current
    instance = test_assembly.TestAssembly.instance

    def setUp(self):
        test_returns.ReturnsFixture.setUp(self)
        self.layout = [(record.ref.node_key, record.device_id)
                       for record in self.server.registry.all_records()]

    def test_consumed_assembly_material_is_not_consumed_or_created_twice(self):
        self.seed(IRON, 2)
        self.submit_order()
        self.drive_until(lambda: bool(self.current().executions()) and
                         self.instance().assembly is not None and
                         self.instance().assembly["phase"] == "outputs")
        order_id = self.current().order_id
        self.restart()
        self.drive_until(lambda: self.current().status == OrderStatus.COMPLETED)
        self.assertEqual(self.current().order_id, order_id)
        self.assertEqual(self.storage_service.query(IRON)["stored"], 0)
        self.assertEqual(self.storage_service.query(BLOCK)["stored"], 1)
        self.assertEqual(self.server.patterns.assembly.leases, {})
