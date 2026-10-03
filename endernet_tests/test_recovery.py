# coding=utf-8
"""12-04 ~ 12-08 验收：接口/网络台账存档、分阶段恢复、拆除与一致性。

这一套守的是几件「离线看不出来、真机上才现形」的事：

1. **接口状态按位置存**（不是按代次）：重进世界后设备代次从 0 重来，
   按代次存的记录会**查不到**，表现是「配置与缓冲全没了」。
2. **同位置换方块不继承旧记录**：把取货口的缓存配置装到存货口上，
   会让它按错误的语义工作。
3. **待核对标记与待核对转移要落盘、且恢复后不重放**（12-07）：
   不确定的事重开一次世界不等于它可以重试了。
4. **预留不复活**：订单不跨主控实例续跑，一份没有主的预留会把库存永久锁住。
5. **恢复闸门**：读存档失败时**不接受新存取**，而不是当成空存档继续。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.protocol import Reason
from endernet.common.resource_key import ResourceKey
from endernet.server.adapters.containers import FakeContainer
from endernet.server.devices.io import (
    MODE_BLACKLIST,
    ORIGIN_EXTERNAL,
    ORIGIN_INTERNAL,
    BatchState,
    PortConfig,
    PortPolicy,
    PortStore,
    PORT_DISPOSAL_RECORD_KIND,
    PORT_RECORD_KIND,
)
from endernet.server.persistence.repository import MemoryRepository
from endernet.server.devices.storage_driver import DRIVER_DEVICE_ID
from endernet.server.runtime.events import NormalizedEvent
from endernet.server.persistence.recovery import (
    PHASE_LOADING,
    PHASE_READY,
    PHASE_RECONCILING,
    RecoveryGate,
    consistency_report,
)
from endernet.server.storage.service import Reservation, StorageService
from endernet.server.storage.transfer import TransferCoordinator

from test_io import (
    OUTPUT_DEVICE,
    STONE,
    _Containers,
    _Registry,
)


def key(x, y=64, z=0, dimension=0):
    return NodeKey(dimension, x, y, z)


INPUT_DEVICE = "endernet:bus_item_input_io"
TEMPLATE_DEVICE = "endernet:bus_template_io"


class PortArchiveFixture(unittest.TestCase):
    """一台接口 + 一个新的「重启后」注册表。"""

    def setUp(self):
        self.repo = MemoryRepository()
        self.registry = _Registry()
        self.ref = DeviceRef(key(1), 0)
        self.registry.add(self.ref, OUTPUT_DEVICE, facing="east")
        self.policy = PortPolicy(max_batch=32)
        self.containers = _Containers(
            {key(1).as_key(): FakeContainer(slots=5, max_stack=64)}
        )
        self.ports = PortStore(
            None, self.registry, policy=self.policy, containers=self.containers
        )
        self.state = self.ports.ensure(self.ref, OUTPUT_DEVICE, "east")

    def restart(self, device_id=None, generation=7):
        """模拟「退出世界再进来」：新注册表（**代次从头重来**）+ 新状态集合。

        代次故意给一个与原来不同的值：按代次存的实现会在这里查不到记录。
        """
        registry = _Registry()
        ref = DeviceRef(key(1), generation)
        registry.add(ref, device_id or OUTPUT_DEVICE, facing="east")
        ports = PortStore(
            None, registry, policy=self.policy, containers=self.containers
        )
        ports.restore_from_repository(self.repo)
        state = ports.ensure(ref, device_id or OUTPUT_DEVICE, "east")
        return ports, state


class TestPortArchive(PortArchiveFixture):
    def test_config_survives_by_position_not_generation(self):
        self.ports.configure(
            self.ref,
            PortConfig(
                enabled=False,
                filter=(STONE,),
                filter_mode=MODE_BLACKLIST,
                cache_enabled=False,
            ),
        )
        self.ports.save_to_repository(self.repo)

        _, state = self.restart()
        self.assertFalse(state.config.enabled)
        self.assertFalse(state.config.cache_enabled, "关闭状态不得被默认值顶掉")
        self.assertEqual(state.config.filter_mode, MODE_BLACKLIST)
        self.assertEqual(state.config.filter, (STONE,))

    def test_a_port_with_nothing_changed_is_not_written(self):
        """空转不写盘：没标脏时一条记录都不产生。"""
        self.assertEqual(self.ports.save_to_repository(self.repo), 0)
        self.assertEqual(self.repo.list_ids(PORT_RECORD_KIND), [])

    def test_the_hold_flag_survives(self):
        """12-07：不确定的外部投料重开世界后仍要停在那条通路上。"""
        self.state.hold = Reason.CONTAINER_UNCERTAIN
        self.state.hold_detail = "结果不确定，已停止这条通路"
        self.ports.note_changed(self.state)
        self.ports.save_to_repository(self.repo)

        _, state = self.restart()
        self.assertEqual(state.hold, Reason.CONTAINER_UNCERTAIN)
        self.assertIn("结果不确定", state.hold_detail)
        self.assertTrue(state.is_held())

    def test_memory_buffer_and_batch_survive(self):
        """样板口的原料缓冲是纯内存的——**不存就等于丢**。"""
        ref = DeviceRef(key(9), 0)
        self.registry.add(ref, TEMPLATE_DEVICE, facing="east")
        state = self.ports.ensure(ref, TEMPLATE_DEVICE, "east")
        endpoint = self.ports.buffer_endpoint(state, "op1", ORIGIN_EXTERNAL)
        endpoint.commit_insert("op1", STONE, 3, "", ORIGIN_EXTERNAL)
        state.batch = BatchState("op1", STONE, 5)
        state.batch.delivered = 2
        self.ports.note_changed(state)
        self.ports.save_to_repository(self.repo)

        registry = _Registry()
        ref2 = DeviceRef(key(9), 3)
        registry.add(ref2, TEMPLATE_DEVICE, facing="east")
        ports2 = PortStore(None, registry, policy=self.policy,
                           containers=self.containers)
        ports2.restore_from_repository(self.repo)
        state2 = ports2.ensure(ref2, TEMPLATE_DEVICE, "east")

        self.assertEqual(state2.buffer.total(), 3)
        self.assertEqual(state2.buffer.total_of(STONE), 3)
        origins = dict((lot["origin"], lot["count"]) for lot in state2.buffer.lots())
        self.assertEqual(origins.get(ORIGIN_EXTERNAL), 3, "来源标识丢了就分不出内外")
        self.assertIsNotNone(state2.batch)
        self.assertEqual(state2.batch.delivered, 2)
        self.assertEqual(state2.batch.requested, 5)

    def test_a_future_version_port_record_is_never_rewritten(self):
        """覆盖掉一份更新的存档，是这一层最不能犯的错。"""
        self.ports.configure(self.ref, PortConfig(enabled=False))
        self.ports.save_to_repository(self.repo)
        record_id = "0,1,64,0"
        record = self.repo.load(PORT_RECORD_KIND, record_id)
        record.schema_version = 99
        record.payload["future_only"] = "keepme"
        self.repo._store[PORT_RECORD_KIND][record_id] = record
        before = dict(record.payload)

        ports, state = self.restart()
        # 配置照最好猜测应用……
        self.assertFalse(state.config.enabled)
        # ……但任何改动都不写回。
        ports.configure(state.ref, PortConfig(enabled=True))
        ports.save_to_repository(self.repo)
        stored = self.repo._store[PORT_RECORD_KIND][record_id]
        self.assertEqual(stored.schema_version, 99)
        self.assertEqual(stored.payload, before)
        self.assertIn(record_id, ports.diagnostics()["frozen_ports"])

    def test_a_different_device_does_not_inherit_the_record(self):
        """同位置换成存货口：取货口的缓存配置不得装上去。"""
        self.ports.configure(
            self.ref, PortConfig(filter=(STONE,), cache_enabled=False)
        )
        self.ports.save_to_repository(self.repo)

        _, state = self.restart(device_id=INPUT_DEVICE)
        self.assertTrue(state.config.cache_enabled, "存货口继承了取货口的缓存开关")
        self.assertEqual(state.config.filter, ())

    def test_unloading_does_not_delete_the_record(self):
        """区块卸载 ≠ 拆除。卸载就删记录会让「走远一点」变成「东西全没了」。"""
        self.ports.configure(self.ref, PortConfig(enabled=False))
        self.ports.save_to_repository(self.repo)
        before = self.repo.load(PORT_RECORD_KIND, "0,1,64,0").payload

        self.ports.mark_loaded(key(1), False)
        self.ports.save_to_repository(self.repo)
        self.assertEqual(
            self.repo.load(PORT_RECORD_KIND, "0,1,64,0").payload, before
        )

    def test_a_dropped_port_record_is_removed(self):
        """12-06：拆掉的接口记录要删掉，同位置重放不继承。"""
        self.ports.configure(self.ref, PortConfig(filter=(STONE,)))
        self.ports.save_to_repository(self.repo)
        self.assertIn("0,1,64,0", self.repo.list_ids(PORT_RECORD_KIND))

        self.ports.drop(self.ref)
        self.ports.save_to_repository(self.repo)
        self.assertEqual(self.repo.list_ids(PORT_RECORD_KIND), [])
        self.assertEqual(self.ports.diagnostics()["pending_archive_writes"], 0)

    def test_a_re_placed_port_cancels_the_pending_delete(self):
        """同一局里「拆掉 → 又放一台」：保存顺序是**先写后删**，
        不撤销删除意图的话，新设备的记录刚写下就被删掉。"""
        self.ports.configure(self.ref, PortConfig(filter=(STONE,)))
        self.ports.save_to_repository(self.repo)
        self.ports.drop(self.ref)

        ref2 = DeviceRef(key(1), 2)
        self.registry.add(ref2, OUTPUT_DEVICE, facing="east")
        self.ports.ensure(ref2, OUTPUT_DEVICE, "east")
        self.ports.configure(ref2, PortConfig(enabled=False))
        self.ports.save_to_repository(self.repo)

        ids = self.repo.list_ids(PORT_RECORD_KIND)
        self.assertIn("0,1,64,0", ids)
        payload = self.repo.load(PORT_RECORD_KIND, "0,1,64,0").payload
        self.assertFalse(payload["config"]["enabled"])

    def test_a_dropped_port_with_a_buffered_load_leaves_a_disposal_record(self):
        """缓冲里还有东西时拆除：接口记录删掉，但「还剩多少」必须留痕。"""
        ref = DeviceRef(key(11), 0)
        self.registry.add(ref, TEMPLATE_DEVICE, facing="east")
        state = self.ports.ensure(ref, TEMPLATE_DEVICE, "east")
        endpoint = self.ports.buffer_endpoint(state, "op1", ORIGIN_INTERNAL)
        endpoint.commit_insert("op1", STONE, 4, "", ORIGIN_INTERNAL)
        self.ports.note_changed(state)
        self.ports.save_to_repository(self.repo)

        self.ports.drop(ref)
        self.ports.save_to_repository(self.repo)
        self.assertEqual(self.repo.list_ids(PORT_RECORD_KIND), [])
        disposal_ids = self.repo.list_ids(PORT_DISPOSAL_RECORD_KIND)
        self.assertEqual(len(disposal_ids), 1)
        report = self.repo.load(PORT_DISPOSAL_RECORD_KIND, disposal_ids[0]).payload
        self.assertEqual(report.get("held"), 4)


class TestReservationArchive(unittest.TestCase):
    """12-04：预留凭据与网络级台账。**存档进出，但不复活。**"""

    def _service_with_a_reservation(self):
        service = StorageService()
        service._reservations["order/1:1"] = Reservation(
            "order/1:1", "order@1/ctrl/1", STONE, 3, (("medium:1", 3),), 12
        )
        service._reservation_seq = 1
        return service

    def test_export_carries_reservations_and_the_sequence(self):
        service = self._service_with_a_reservation()
        payload = service.export_network_state()
        self.assertEqual(payload["reservation_seq"], 1)
        self.assertEqual(len(payload["reservations"]), 1)
        self.assertEqual(payload["reservations"][0]["owner_id"], "order@1/ctrl/1")

    def test_import_archives_but_does_not_revive_reservations(self):
        """一份没有主的预留会把库存永久锁住——比丢了它更糟。"""
        payload = self._service_with_a_reservation().export_network_state()
        service = StorageService()
        stats = service.import_network_state(payload)

        self.assertEqual(stats["reservations_archived"], 1)
        self.assertEqual(service._reservations, {})
        self.assertEqual(service.archived_reservations()[0]["owner_id"],
                         "order@1/ctrl/1")

    def test_import_raises_the_sequence_above_the_archive(self):
        """不抬水位会让重进世界后的新预留与旧预留**撞号**。"""
        payload = self._service_with_a_reservation().export_network_state()
        service = StorageService()
        service.import_network_state(payload)
        self.assertGreaterEqual(service._reservation_seq, 1)

    def test_an_unreadable_reservation_is_skipped_not_fatal(self):
        service = StorageService()
        stats = service.import_network_state(
            {"schema_version": 1, "reservations": [{"junk": True}]}
        )
        self.assertEqual(stats["reservations_archived"], 0)


class TestPendingTransferArchive(unittest.TestCase):
    """12-07：可能成功但未能确认的转移。**恢复成待核对，绝不重放。**"""

    def _coordinator_with_a_pending(self):
        coordinator = TransferCoordinator()
        coordinator._pending["op1"] = {
            "op_id": "op1",
            "resource_key": STONE,
            "count": 3,
            "moved": 0,
            "source_id": "container:a",
            "target_id": "container:b",
            "holder": "说不清",
            "detail": "读回不一致",
            "tick": 5,
        }
        coordinator._pending_order.append("op1")
        return coordinator

    def test_pending_round_trips_as_plain_data(self):
        exported = self._coordinator_with_a_pending().export_pending()
        self.assertEqual(len(exported), 1)
        self.assertIsInstance(exported[0]["resource_key"], dict)

        restored = TransferCoordinator()
        self.assertEqual(restored.import_pending(exported), 1)
        snapshot = restored.pending_reconcile()
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["resource_key"], STONE)

    def test_a_restored_pending_is_not_replayed_by_ticks(self):
        """恢复之后账本仍是那一条，业务路径不会去动它——**没有重放。**"""
        restored = TransferCoordinator()
        restored.import_pending(self._coordinator_with_a_pending().export_pending())
        before = restored.export_pending()
        for tick in range(5):
            restored.tick(tick)
        self.assertEqual(restored.export_pending(), before)
        self.assertEqual(restored.stats["replayed"], 0)

    def test_only_an_explicit_clear_removes_a_pending(self):
        restored = TransferCoordinator()
        restored.import_pending(self._coordinator_with_a_pending().export_pending())
        self.assertTrue(restored.clear_pending("op1"))
        self.assertEqual(restored.export_pending(), [])


class TestRecoveryGate(unittest.TestCase):
    """12-05：阶段闸门。**未开放时不接受新存取。**"""

    def test_ready_is_the_only_open_phase(self):
        gate = RecoveryGate()
        self.assertFalse(gate.is_open())
        gate.begin()
        self.assertEqual(gate.phase, PHASE_LOADING)
        gate.note_restored(True)
        self.assertEqual(gate.phase, PHASE_RECONCILING)
        self.assertFalse(gate.is_open(), "对账阶段就不该放行业务")
        gate.mark_ready(4)
        self.assertEqual(gate.phase, PHASE_READY)
        self.assertTrue(gate.is_open())
        self.assertEqual(gate.ready_tick, 4)

    def test_a_failed_restore_stays_closed(self):
        """读存档失败**不等于没有存档**：停在 loading，业务一律被拒。"""
        gate = RecoveryGate()
        gate.begin()
        gate.note_restored(False, "载荷读不动")
        self.assertEqual(gate.phase, PHASE_LOADING)
        self.assertFalse(gate.is_open())
        self.assertIn("不把读不出的存档当成空存档", gate.blocker())
        self.assertFalse(gate.diagnostics()["open"])

    def test_steps_are_a_reproducible_record(self):
        gate = RecoveryGate()
        gate.begin()
        gate.note_restored(True, "驱动器 1、接口 2")
        gate.mark_ready(3)
        phases = [item["phase"] for item in gate.diagnostics()["steps"]]
        self.assertEqual(phases, [PHASE_LOADING, PHASE_RECONCILING, PHASE_READY])


class TestConsistencyReport(unittest.TestCase):
    def test_a_healthy_assembly_reports_no_problems(self):
        from endernet.server.bootstrap import ServerBootstrap

        server = ServerBootstrap(world=_FakeWorld())
        server.start(bind_engine=False)
        report = consistency_report(server)
        self.assertEqual(report["problems"], [])
        self.assertIn("checked", report)


class _Cmd(object):
    """命令替身：`_recovery_blocked` 只读 `request_id`。"""

    def __init__(self, request_id):
        self.request_id = request_id


class TestBootstrapRecovery(unittest.TestCase):
    """12-04/12-05/12-08：装配层的接线。**离线路径**（不碰引擎）。"""

    def setUp(self):
        from endernet.server.bootstrap import ServerBootstrap

        from test_bootstrap import FakeWorld

        self.backend = testsupport.MemoryExtraDataBackend()
        self.server = ServerBootstrap(
            world=FakeWorld(), persistence_backend=self.backend
        )

    def test_the_gate_opens_after_the_first_tick(self):
        self.server.start(bind_engine=False)
        self.assertEqual(self.server.recovery.phase, PHASE_RECONCILING)
        self.assertFalse(self.server.recovery.is_open())
        self.server.tick()
        self.assertEqual(self.server.recovery.phase, PHASE_READY)
        self.assertTrue(self.server.recovery.is_open())

    def test_a_command_before_recovery_is_blocked(self):
        """阶段未完成不接受新存取——**玩家那条路也要挡**。"""
        self.server.start(bind_engine=False)
        result = self.server.submit_command(_Cmd("r1"))
        self.assertEqual(result.status, "blocked")
        self.assertIn(Reason.SERVICE_NOT_READY, result.reason_codes())

    def test_a_port_config_survives_a_restart(self):
        self.server.start(bind_engine=False)
        self.server.push(NormalizedEvent.place(key(3), OUTPUT_DEVICE))
        self.server.tick()
        record = self.server.registry.get(key(3))
        self.server.ports.configure(
            record.ref, PortConfig(enabled=False, filter=(STONE,))
        )
        self.server.tick()
        self.server.stop()

        from endernet.server.bootstrap import ServerBootstrap

        from test_bootstrap import FakeWorld

        revived = ServerBootstrap(
            world=FakeWorld(), persistence_backend=self.backend
        )
        revived.start(bind_engine=False)
        revived.push(NormalizedEvent.place(key(3), OUTPUT_DEVICE))
        revived.tick()
        revived_record = revived.registry.get(key(3))
        revived_state = revived.ports.get_by_ref(revived_record.ref)
        self.assertFalse(revived_state.config.enabled)
        self.assertEqual(revived_state.config.filter, (STONE,))

    def test_dirty_records_share_one_commit(self):
        """12-08：多条脏记录合并成**一次** `SaveExtraData()`。"""
        self.server.start(bind_engine=False)
        self.server.push(NormalizedEvent.place(key(3), OUTPUT_DEVICE))
        self.server.push(NormalizedEvent.place(key(4), OUTPUT_DEVICE))
        self.server.tick()
        self.server.tick()
        first = self.server.registry.get(key(3))
        second = self.server.registry.get(key(4))
        self.server.ports.configure(first.ref, PortConfig(enabled=False))
        self.server.ports.configure(second.ref, PortConfig(enabled=False))

        before = self.backend.saves
        self.server.tick()
        self.assertEqual(self.backend.saves - before, 1)

    def test_a_write_fault_keeps_the_record_dirty_for_retry(self):
        """14 的中断注入入口：写失败**不摘脏**，下一帧重试。"""
        from endernet.server.bootstrap import ServerBootstrap
        from endernet.server.persistence.repository import MemoryRepository

        from test_bootstrap import FakeWorld

        repo = MemoryRepository()
        server = ServerBootstrap(world=FakeWorld(), repository=repo)
        server.start(bind_engine=False)
        server.push(NormalizedEvent.place(key(3), OUTPUT_DEVICE))
        server.tick()
        record = server.registry.get(key(3))
        server.ports.configure(record.ref, PortConfig(enabled=False))

        repo.inject_fault("write", kind=PORT_RECORD_KIND, times=1)
        with self.assertRaises(IOError):
            server.tick()
        self.assertGreater(server.ports.pending_archive_writes(), 0)
        server.tick()
        self.assertEqual(server.ports.pending_archive_writes(), 0)

    def _form_a_network(self, server):
        """摆一个基带 + 一个主控 + 一台驱动器，凑成一张能开起来的网络。"""
        server.push(NormalizedEvent.place(key(1), "endernet:network_baseband_8"))
        server.push(NormalizedEvent.place(key(2), "endernet:tiny_network_endpoint"))
        server.push(NormalizedEvent.place(key(3), DRIVER_DEVICE_ID))
        for _ in range(4):
            server.tick()
        for runtime in server.runtimes.runtimes():
            storage = runtime.services.get("storage")
            if isinstance(storage, StorageService) and runtime.master is not None:
                return runtime, storage
        return None, None

    def _storage_with_a_reservation(self, server):
        from endernet.server.storage.service import Reservation

        runtime, storage = self._form_a_network(server)
        self.assertIsNotNone(storage, "网络没凑起来，下面测不到")
        storage._reservations["probe:1"] = Reservation(
            "probe:1", "order@probe", STONE, 2, (("medium:probe", 2),), 7
        )
        storage._reservation_seq = 1
        return runtime, storage

    def test_network_state_is_written_on_stop(self):
        """停机时写一条网络级台账记录（预留/待核对/订单簿）。"""
        from endernet.server.storage.service import NETWORK_RECORD_KIND

        self.server.start(bind_engine=False)
        self._storage_with_a_reservation(self.server)
        self.server.stop()

        ids = self.server.repository.list_ids(NETWORK_RECORD_KIND)
        self.assertEqual(len(ids), 1, "一张网络应当恰好一条台账")
        payload = self.server.repository.load(NETWORK_RECORD_KIND, ids[0]).payload
        self.assertIn("storage", payload)
        self.assertIn("orders", payload)
        self.assertEqual(len(payload["storage"]["reservations"]), 1)

    def test_an_archived_reservation_is_reported_but_not_revived(self):
        """**真机上撞到过的那条**：记录里是装配层的信封 `{storage, orders}`，
        存储服务要的是信封里那一份。传错一层会让预留台账**永远恢复不出来**，
        而表面上一切正常（记录写了、应用了、一条报错都没有）。
        """
        self.server.start(bind_engine=False)
        self._storage_with_a_reservation(self.server)
        self.server.stop()

        from endernet.server.bootstrap import ServerBootstrap

        from test_bootstrap import FakeWorld

        revived = ServerBootstrap(
            world=FakeWorld(), repository=self.server.repository
        )
        revived.start(bind_engine=False)
        _, storage = self._form_a_network(revived)

        self.assertEqual(storage._reservations, {}, "预留不得复活")
        archived = storage.archived_reservations()
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["owner_id"], "order@probe")
        self.assertGreaterEqual(storage._reservation_seq, 1)


class _FakeWorld(object):
    def list_loaded_chunks(self):
        return []

    def block_entities_in_chunk(self, chunk_key):
        return []

    def register_block_remove_listener(self, block_ids):
        pass

    def facing_at(self, node_key):
        return None

    def diagnostics(self):
        return {"fake": True}


if __name__ == "__main__":
    unittest.main()
