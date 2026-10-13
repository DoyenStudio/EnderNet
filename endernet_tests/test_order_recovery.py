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
from endernet.server.persistence.repository import MemoryRepository
from endernet.server.runtime.events import NormalizedEvent
from test_executor import OneLevel, IRON, BLOCK, PORT_KEY, DRIVER_KEY, DRIVER


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

    def test_unknown_archive_version_stops_business_and_preserves_record(self):
        from endernet.server.storage.service import NETWORK_RECORD_KIND
        self.seed(IRON, 4)
        self.submit_order()

        def mutate(repo):
            record = repo.load(NETWORK_RECORD_KIND, repo.list_ids(NETWORK_RECORD_KIND)[0])
            record.payload["orders"]["resume_version"] = 999
            repo.write(record)
        self.restart(mutate)
        self.assertFalse(self.server.recovery.is_open())
        self.assertFalse(self.runtime.is_open())
        self.assertIn("unsupported", self.server.persistence_restore_error)
        self.server.stop()
        repo = self.server.repository
        record = repo.load(NETWORK_RECORD_KIND, repo.list_ids(NETWORK_RECORD_KIND)[0])
        self.assertEqual(record.payload["orders"]["resume_version"], 999)

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
