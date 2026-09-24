# coding=utf-8
"""01-01 / 01-06 验收：入口可脱离游戏构造、可显式销毁、分帧补查有界。

这一组测试同时是「模组能不能加载」的离线检查：
`modMain.py` 依赖 `server.bootstrap.start_server` 与 `client.bootstrap.start_client`，
如果这两个入口或它们引用的模块有任何导入期错误，这里会直接失败。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import DefinitionSet
from endernet.common.identity import ChunkKey, NodeKey
from endernet.common.protocol import (
    CommandKind,
    CommandSource,
    DeviceCommand,
    Reason,
    STATUS_BLOCKED,
    STATUS_OK,
    STATUS_REJECTED,
)
from endernet.server.bootstrap import ServerBootstrap
from endernet.server.network.controller import RunState
from endernet.server.network.services import AssemblyProfile
from endernet.server.runtime.events import EventKind, NormalizedEvent
from endernet.server.runtime.lifecycle import ChangeKind

BB8 = "endernet:network_baseband_8"
DRIVER = "endernet:storage_driver"


def key(x, y=64, z=0, dimension=0):
    return NodeKey(dimension, x, y, z)


class FakeWorld(object):
    def __init__(self, chunks=None, block_entities=None):
        self._chunks = list(chunks or [])
        self._block_entities = dict(block_entities or {})
        self.whitelisted = set()
        #: `block_entities_in_chunk` 被调用的次数，用于验证「不重复空转」
        self.chunk_scans = 0

    def list_loaded_chunks(self):
        return list(self._chunks)

    def block_entities_in_chunk(self, chunk_key):
        self.chunk_scans += 1
        return list(self._block_entities.get(chunk_key.as_key(), []))

    def register_block_remove_listener(self, block_ids):
        self.whitelisted.update(block_ids)

    def facing_at(self, node_key):
        return None

    def note_chunk_loaded(self, chunk_key, block_entities=()):
        self._chunks.append(chunk_key)
        self._block_entities[chunk_key.as_key()] = list(block_entities)

    def note_chunk_unloaded(self, chunk_key):
        pass

    def diagnostics(self):
        return {"fake": True}


class TestImportability(unittest.TestCase):
    """入口模块必须能在没有 mod.server.* 的环境下导入。

    适配器把引擎调用放在方法体内部，就是为了让这一点成立；
    否则整个领域层连离线测试都跑不了。
    """

    def test_server_bootstrap_imports(self):
        from endernet.server import bootstrap

        self.assertTrue(callable(bootstrap.start_server))
        self.assertTrue(callable(bootstrap.stop_server))

    def test_client_bootstrap_imports(self):
        from endernet.client import bootstrap

        self.assertTrue(callable(bootstrap.start_client))
        self.assertTrue(callable(bootstrap.stop_client))

    def test_adapters_import_without_engine(self):
        from endernet.server.adapters import containers, events, world

        self.assertTrue(hasattr(events, "ServerEventBinder"))
        self.assertTrue(hasattr(world, "ToolDeltaWorldAdapter"))
        self.assertTrue(hasattr(containers, "ContainerAdapter"))


class TestServerLifecycle(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld()
        self.server = ServerBootstrap(world=self.world)

    def test_starts_and_stops_headless(self):
        self.server.start(bind_engine=False)
        self.assertTrue(self.server.started)
        self.server.stop()
        self.assertFalse(self.server.started)

    def test_tick_before_start_raises(self):
        with self.assertRaises(RuntimeError):
            self.server.tick()

    def test_start_is_idempotent(self):
        self.server.start(bind_engine=False)
        self.server.start(bind_engine=False)
        self.assertTrue(self.server.started)

    def test_stop_drains_pending_events(self):
        """销毁后不得残留待办，否则热重载会拿到上一轮的陈旧事件。"""
        self.server.start(bind_engine=False)
        self.server.push(NormalizedEvent.place(key(0), BB8))
        self.server.stop()
        self.assertEqual(len(self.server.queue), 0)

    def test_restart_is_clean(self):
        self.server.start(bind_engine=False)
        self.server.push(NormalizedEvent.place(key(0), BB8))
        self.server.tick()
        self.assertEqual(self.server.registry.count(), 1)
        self.server.stop()
        self.server.start(bind_engine=False)
        self.assertEqual(self.server.registry.count(), 1, "重启不应丢失已注册设备")


class TestTickPipeline(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld()
        self.changes = []
        self.server = ServerBootstrap(world=self.world, change_sink=self.changes.append)
        self.server.start(bind_engine=False)

    def test_place_event_registers_device(self):
        self.server.push(NormalizedEvent.place(key(0), BB8))
        self.server.tick()
        self.assertIsNotNone(self.server.registry.get(key(0)))

    def test_change_sink_receives_changes(self):
        self.server.push(NormalizedEvent.place(key(0), BB8))
        self.server.tick()
        self.assertEqual([c.kind for c in self.changes], [ChangeKind.ADDED])

    def test_tick_is_bounded(self):
        """一次 tick 不得消费超过 drain_limit 条事件。"""
        server = ServerBootstrap(world=self.world, drain_limit=3)
        server.start(bind_engine=False)
        for index in range(20):
            server.push(NormalizedEvent.place(key(index, 64, 0, 0), BB8))
        report = server.tick()
        self.assertEqual(report["events_drained"], 3)
        self.assertEqual(len(server.queue), 17)
        self.assertEqual(server.registry.count(), 3)

    def test_changes_outnumber_events(self):
        """一次放置不止一条变化：放下本身 + 邻接设备标脏。"""
        server = ServerBootstrap(world=self.world, drain_limit=10)
        server.start(bind_engine=False)
        server.push(NormalizedEvent.place(key(0), BB8))
        server.push(NormalizedEvent.place(key(1), BB8))
        report = server.tick()
        self.assertEqual(report["events_drained"], 2)
        self.assertGreater(report["changes"], 2)

    def test_queue_drains_over_multiple_ticks(self):
        server = ServerBootstrap(world=self.world, drain_limit=3)
        server.start(bind_engine=False)
        for index in range(10):
            server.push(NormalizedEvent.place(key(index, 64, 0, 0), BB8))
        for _ in range(4):
            server.tick()
        self.assertEqual(server.registry.count(), 10)

    def test_duplicate_place_through_pipeline(self):
        """整条链路上的幂等：重复事件只建一个实例。"""
        self.server.push(NormalizedEvent.place(key(0), BB8))
        self.server.push(NormalizedEvent.place(key(0), BB8))
        self.server.tick()
        self.assertEqual(self.server.registry.count(), 1)

    def test_tick_report_shape(self):
        report = self.server.tick()
        self.assertIn("backfilled_chunks", report)
        self.assertIn("changes", report)
        self.assertEqual(report["tick"], 1)


class TestTickDriver(unittest.TestCase):
    """整条链路需要一个驱动器，否则事件只入队、没人消费。

    真机验证时就是这么发现的：事件计数一直在涨，`ticks` 恒为 0，
    设备永远注册不上。离线时用 `drive_once()` 覆盖同一条路径。
    """

    def setUp(self):
        self.world = FakeWorld()
        self.server = ServerBootstrap(world=self.world)
        self.server.start(bind_engine=False)

    def test_drive_once_consumes_events(self):
        self.server.push(NormalizedEvent.place(key(0), BB8))
        self.server.drive_once()
        self.assertEqual(self.server.tick_count, 1)
        self.assertIsNotNone(self.server.registry.get(key(0)))

    def test_drive_once_keeps_going_across_many_calls(self):
        """等价于引擎每秒数十次的调用。"""
        for index in range(50):
            self.server.push(NormalizedEvent.place(key(index, 64, 0, 0), BB8))
        for _ in range(20):
            self.server.drive_once()
        self.assertEqual(self.server.tick_count, 20)
        self.assertEqual(self.server.registry.count(), 50)

    def test_drive_once_does_not_raise_on_bad_event(self):
        """一帧出错不得让后续帧全部失效。"""
        self.server.push(NormalizedEvent("bogus_kind", data={}))
        self.server.drive_once()  # 不得抛出
        self.assertEqual(self.server.tick_error_count, 0, "未知事件应被忽略而不是报错")

    def test_drive_once_captures_exception_and_continues(self):
        """真正抛异常时要记录堆栈，并且循环继续。"""
        sink = []

        def exploding_sink(change):
            sink.append(change)
            raise RuntimeError("故意炸一个")

        server = ServerBootstrap(world=FakeWorld(), change_sink=exploding_sink)
        server.start(bind_engine=False)
        server.push(NormalizedEvent.place(key(0), BB8))
        server.drive_once()  # 不得向上抛

        self.assertEqual(server.tick_error_count, 1)
        self.assertEqual(len(server.tick_errors), 1)
        self.assertIn("故意炸一个", server.tick_errors[0])

        # 后续帧仍然推进
        server.push(NormalizedEvent.place(key(1), BB8))
        server.drive_once()
        self.assertEqual(server.tick_count, 2)

    def test_tick_error_samples_are_bounded(self):
        """持续故障不得无限吃内存。"""
        def always_boom(change):
            raise RuntimeError("一直炸")

        server = ServerBootstrap(
            world=FakeWorld(), change_sink=always_boom, max_tick_errors=3
        )
        server.start(bind_engine=False)
        for index in range(10):
            server.push(NormalizedEvent.place(key(index, 64, 0, 0), BB8))
            server.drive_once()
        self.assertEqual(server.tick_error_count, 10)
        self.assertEqual(len(server.tick_errors), 3)

    def test_diagnostics_expose_tick_errors(self):
        report = self.server.diagnostics()
        self.assertIn("tick_errors", report)
        self.assertEqual(report["tick_errors"]["count"], 0)


class TestBackfillIsBounded(unittest.TestCase):
    """启动补查必须分帧（01-06）。"""

    def _server(self, chunk_count, per_tick=4):
        chunks = [ChunkKey(0, index, 0) for index in range(chunk_count)]
        entities = {}
        for chunk in chunks:
            entities[chunk.as_key()] = [
                {
                    "posX": chunk.chunk_x * 16 + 1,
                    "posY": 64,
                    "posZ": 2,
                    "blockName": DRIVER,
                }
            ]
        world = FakeWorld(chunks=chunks, block_entities=entities)
        server = ServerBootstrap(world=world, backfill_chunks_per_tick=per_tick)
        server.start(bind_engine=False)
        return server, chunks

    def test_backfill_is_split_across_ticks(self):
        server, chunks = self._server(10, per_tick=4)
        first = server.tick()
        self.assertEqual(first["backfilled_chunks"], 4, "一帧最多补查 4 个区块")
        self.assertEqual(server.registry.count(), 4)

        second = server.tick()
        self.assertEqual(second["backfilled_chunks"], 4)
        self.assertEqual(server.registry.count(), 8)

    def test_backfill_completes(self):
        server, chunks = self._server(10, per_tick=4)
        for _ in range(5):
            server.tick()
        self.assertEqual(server.registry.count(), 10)
        self.assertEqual(server.lifecycle.stats.backfilled_chunks, 10)

    def test_backfill_registers_every_device_once(self):
        """重复补查不得重复挂载（01 验收）。"""
        server, chunks = self._server(3, per_tick=4)
        server.tick()
        # 强制再跑一整轮，重复扫描不得新建实例
        server.lifecycle.forget_backfill()
        server.reset_backfill()
        for _ in range(3):
            server.tick()
        self.assertEqual(server.registry.count(), 3)

    def test_backfill_runs_once_and_stops(self):
        """跑完后不再每帧重建生成器空转。"""
        server, chunks = self._server(3, per_tick=4)
        server.tick()
        scans_after_first_round = server.world.chunk_scans
        for _ in range(20):
            server.tick()
        self.assertEqual(
            server.world.chunk_scans,
            scans_after_first_round,
            "补查跑完后不应再扫描区块",
        )

    def test_reset_backfill_allows_another_round(self):
        server, chunks = self._server(3, per_tick=4)
        server.tick()
        scans = server.world.chunk_scans
        server.reset_backfill()
        server.tick()
        self.assertGreater(server.world.chunk_scans, scans)

    def test_force_backfill(self):
        server, chunks = self._server(3)
        self.assertEqual(server.force_backfill(chunks), 3)


class TestChunkEventsAreThePrimaryDiscoveryPath(unittest.TestCase):
    """区块加载事件是设备发现的主要路径。

    脚本初始化时世界尚未加载，之后每个区块加载都会发事件并附带方块实体，
    因此不依赖启动补查也能发现全部设备。
    """

    def setUp(self):
        self.world = FakeWorld()  # 初始化时没有任何已加载区块
        self.server = ServerBootstrap(world=self.world)
        self.server.start(bind_engine=False)

    def chunk(self, cx, cz=0):
        return ChunkKey(0, cx, cz)

    def test_no_backfill_work_when_world_starts_empty(self):
        report = self.server.tick()
        self.assertEqual(report["backfilled_chunks"], 0)

    def test_chunk_load_event_registers_devices(self):
        """事件携带的方块实体被补查为本模组设备。"""
        self.server.push(
            NormalizedEvent.chunk_loaded(
                self.chunk(0),
                block_entities=[
                    {"posX": 1, "posY": 64, "posZ": 2, "blockName": BB8},
                    {"posX": 3, "posY": 64, "posZ": 4, "blockName": DRIVER},
                ],
            )
        )
        self.server.tick()
        self.assertEqual(self.server.registry.count(), 2)
        self.assertIsNotNone(self.server.registry.get(key(1, 64, 2)))

    def test_chunk_load_event_ignores_foreign_blocks(self):
        self.server.push(
            NormalizedEvent.chunk_loaded(
                self.chunk(0),
                block_entities=[
                    {"posX": 1, "posY": 64, "posZ": 2, "blockName": "minecraft:chest"}
                ],
            )
        )
        self.server.tick()
        self.assertEqual(self.server.registry.count(), 0)

    def test_all_loaded_chunks_are_discovered_via_events(self):
        """多个区块依次加载，全部被事件发现，无需补查。"""
        for index in range(8):
            self.server.push(
                NormalizedEvent.chunk_loaded(
                    self.chunk(index),
                    block_entities=[
                        {
                            "posX": index * 16 + 1,
                            "posY": 64,
                            "posZ": 2,
                            "blockName": BB8,
                        }
                    ],
                )
            )
        self.server.tick()
        self.assertEqual(self.server.registry.count(), 8)

    def test_unload_then_reload_restores_same_instance(self):
        """卸载后重新加载会再次发事件，恢复的是同一个实例与代次。"""
        self.server.push(
            NormalizedEvent.chunk_loaded(
                self.chunk(0),
                block_entities=[{"posX": 1, "posY": 64, "posZ": 2, "blockName": BB8}],
            )
        )
        self.server.tick()
        generation = self.server.registry.get(key(1, 64, 2)).generation

        self.server.push(NormalizedEvent.chunk_unloaded(self.chunk(0)))
        self.server.tick()
        self.assertFalse(self.server.registry.is_loaded(key(1, 64, 2)))

        self.server.push(
            NormalizedEvent.chunk_loaded(
                self.chunk(0),
                block_entities=[{"posX": 1, "posY": 64, "posZ": 2, "blockName": BB8}],
            )
        )
        self.server.tick()
        record = self.server.registry.get(key(1, 64, 2))
        self.assertTrue(record.loaded)
        self.assertEqual(record.generation, generation, "重新加载应是同一个实例")


class TestFullScenario(unittest.TestCase):
    """一个贴近实际的最小场景：放置 -> 邻接 -> 拆除 -> 重放。"""

    def setUp(self):
        self.world = FakeWorld()
        self.changes = []
        self.server = ServerBootstrap(world=self.world, change_sink=self.changes.append)
        self.server.start(bind_engine=False)

    def drive(self, *events):
        for event in events:
            self.server.push(event)
        self.server.tick()

    def test_baseband_controller_driver_chain(self):
        self.drive(
            NormalizedEvent.place(key(0), BB8),
            NormalizedEvent.place(key(1), BB8),
            NormalizedEvent.place(key(2), "endernet:tiny_network_endpoint"),
            NormalizedEvent.place(key(3), DRIVER),
        )
        self.assertEqual(self.server.registry.count(), 4)

        # 邻接关系按连接规则成立
        neighbors = self.server.catalog.connected_neighbors(self.server.registry, key(1))
        self.assertEqual(len(neighbors), 2)

    def test_remove_then_replace_gets_new_generation(self):
        self.drive(NormalizedEvent.place(key(0), BB8))
        first = self.server.registry.get(key(0)).generation

        self.drive(NormalizedEvent.remove(key(0), BB8))
        self.assertIsNone(self.server.registry.get(key(0)))

        self.drive(NormalizedEvent.place(key(0), BB8))
        self.assertGreater(self.server.registry.get(key(0)).generation, first)

    def test_diagnostics_shape(self):
        self.drive(NormalizedEvent.place(key(0), BB8))
        report = self.server.diagnostics()
        self.assertTrue(report["started"])
        self.assertEqual(report["definitions"]["devices"], 22)
        self.assertEqual(report["registry"]["devices"], 1)
        self.assertIn("scope_note", report)


class TestNetworkWiring(unittest.TestCase):
    """02 接入 bootstrap：事件 -> 设备 -> 拓扑 -> 带宽判定 的整条链路。

    这一组是「装配是否真的接上了」的检查：拓扑与准入服务在
    `ServerBootstrap` 里被创建，设备变化被它们消费，`tick()` 内有限步推进。
    领域服务各自的算法由 `test_topology.py` / `test_admission.py` 覆盖。
    """

    BB8 = BB8
    TINY = "endernet:tiny_network_endpoint"

    def setUp(self):
        self.world = FakeWorld()
        self.server = ServerBootstrap(world=self.world)
        self.server.start(bind_engine=False)

    def drive(self, *events):
        for event in events:
            self.server.push(event)
        return self.server.tick()

    def test_devices_form_a_component_through_the_event_queue(self):
        self.drive(
            NormalizedEvent.place(key(0), self.BB8),
            NormalizedEvent.place(key(1), self.BB8),
            NormalizedEvent.place(key(1, 65), self.TINY),
            NormalizedEvent.place(key(0, 65), DRIVER),
        )
        snapshot = self.server.topology.snapshot()
        component = snapshot.component_of(key(0))
        self.assertEqual(len(component.members), 4)
        self.assertTrue(component.is_runnable())

    def test_bandwidth_status_is_readable_for_a_placed_device(self):
        self.drive(
            NormalizedEvent.place(key(0), self.BB8),
            NormalizedEvent.place(key(1, 65), self.TINY),
            NormalizedEvent.place(key(0, 65), "endernet:common_terminal_panel"),
        )
        record = self.server.registry.get(key(0, 65))
        result = self.server.admission.status_of(record.ref)
        self.assertEqual(result.state, "granted")
        self.assertTrue(result.ok)
        self.assertEqual(result.demand_quarters, 4)

    def test_tick_exposes_the_rebuild_budget(self):
        report = self.drive(NormalizedEvent.place(key(0), self.BB8))
        self.assertIn("topology_steps", report)
        self.assertIn("topology_version", report)
        self.assertGreaterEqual(report["topology_version"], 1)

    def test_removing_a_bridge_splits_through_the_event_queue(self):
        self.drive(
            NormalizedEvent.place(key(0), self.BB8),
            NormalizedEvent.place(key(1), self.BB8),
            NormalizedEvent.place(key(2), self.BB8),
            NormalizedEvent.place(key(0, 65), self.TINY),
        )
        self.drive(NormalizedEvent.remove(key(1), self.BB8))

        snapshot = self.server.topology.snapshot()
        self.assertNotEqual(
            snapshot.component_id_of(key(0)), snapshot.component_id_of(key(2))
        )
        self.assertEqual(snapshot.controller_state_of(key(0)), "single")
        self.assertEqual(snapshot.controller_state_of(key(2)), "no_controller")

    def test_diagnostics_include_topology_and_admission(self):
        self.drive(NormalizedEvent.place(key(0), self.BB8))
        report = self.server.diagnostics()
        self.assertIn("topology", report)
        self.assertIn("admission", report)
        self.assertIn("policy", report["admission"])
        self.assertEqual(report["topology"]["snapshot"]["components"], 1)
        self.assertIn("任务 02", report["scope_note"])

    def test_restart_rebuilds_the_graph_from_the_registry(self):
        events = [
            NormalizedEvent.place(key(0), self.BB8),
            NormalizedEvent.place(key(1), self.BB8),
            NormalizedEvent.place(key(1, 65), self.TINY),
        ]
        self.drive(*events)
        version = self.server.topology.version

        self.server.stop()
        self.server.start(bind_engine=False)
        self.server.tick()

        snapshot = self.server.topology.snapshot()
        self.assertGreater(self.server.topology.version, version)
        self.assertEqual(len(snapshot.component_of(key(0)).members), 3)
        self.assertTrue(snapshot.component_of(key(0)).is_runnable())


class TestRuntimeWiring(unittest.TestCase):
    """03 接入 bootstrap：事件 → 设备 → 拓扑发布 → 运行时开放 → 命令被拒。

    这一组只验「装配是否真的接上了」。准入门链逐条、去重账本、迁移顺序
    分别由 `test_controller.py` / `test_runtime.py` 覆盖。

    **装配用的是 04/05/09 的真实替身**（`ServerBootstrap` 的默认），
    因此这里同时是「游戏内现在到底能不能用」的离线证据：能建起网络、
    能查状态、业务命令一律 `service_absent`。
    """

    BB8 = BB8
    TINY = "endernet:tiny_network_endpoint"

    def setUp(self):
        self.world = FakeWorld()
        self.server = ServerBootstrap(world=self.world)
        self.server.start(bind_engine=False)

    def drive(self, *events):
        for event in events:
            self.server.push(event)
        return self.server.tick()

    def build_network(self):
        """基带 -> 控制器 -> 驱动器。控制器与驱动器**同区块**：
        这一组不测「主控够不着」（那是 test_controller 的事）。"""
        return self.drive(
            NormalizedEvent.place(key(0), self.BB8),
            NormalizedEvent.place(key(1), self.BB8),
            NormalizedEvent.place(key(1, 65), self.TINY),
            NormalizedEvent.place(key(0, 65), DRIVER),
        )

    def runtime(self):
        runtimes = self.server.runtimes.runtimes()
        self.assertEqual(len(runtimes), 1, "夹具只应有一个网络")
        return runtimes[0]

    def command(self, request_id="r1", kind=CommandKind.STORAGE):
        # type: (str, str) -> DeviceCommand
        ref = self.server.registry.get(key(0, 65)).ref
        return DeviceCommand(
            request_id, kind, CommandSource("p1", ref, 0), device_ref=ref
        )

    def test_a_placed_network_gets_a_runtime_with_a_master(self):
        report = self.build_network()
        self.assertEqual(report["runtimes"], 1)
        runtime = self.runtime()
        self.assertIsNotNone(runtime.master, "拓扑单控应绑出主控")
        self.assertTrue(runtime.is_open(), "结构性校验通过后业务就该开放")

    def test_open_does_not_mean_runnable(self):
        """「开放业务」是结构性校验，「能不能做」是逐条命令的事。

        两者分开是有意的：在这里一并要求服务齐全，会把「某个功能还没写」
        误报成「整个网络不能开」，连状态都查不了。
        """
        self.build_network()
        runtime = self.runtime()
        self.assertTrue(runtime.is_open())
        self.assertEqual(runtime.state(), RunState.SERVICE_ABSENT)

    def test_business_commands_are_refused_by_the_stand_ins(self):
        self.build_network()
        result = self.server.submit_command(self.command(request_id="r1"))
        self.assertEqual(result.status, STATUS_BLOCKED, result.to_dict())
        self.assertIn(Reason.SERVICE_ABSENT, result.reason_codes())

    def test_status_is_answered_while_business_is_refused(self):
        self.build_network()
        result = self.server.submit_command(
            self.command(request_id="s1", kind=CommandKind.STATUS)
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertEqual(result.payload["run_state"], RunState.SERVICE_ABSENT)
        self.assertIn("service_absent", [r["code"] for r in result.payload["blockers"]])

    def test_device_status_is_readable_and_honest(self):
        """状态栏说的是「服务尚未实现」，不是「没电」也不是「离线故障」。"""
        self.build_network()
        ref = self.server.registry.get(key(0, 65)).ref
        status = self.server.application.device_status(ref)
        self.assertFalse(status.is_online)
        self.assertIn(Reason.SERVICE_ABSENT, status.reason_codes())
        self.assertNotIn(Reason.BROWNOUT, status.reason_codes())

    def test_removing_the_controller_leaves_the_runtime_without_a_master(self):
        """拆掉控制器不销毁运行时：库存与订单的归属不能被一次拆除清掉。"""
        self.build_network()
        before = self.runtime()
        self.drive(NormalizedEvent.remove(key(1, 65), self.TINY))
        after = self.runtime()
        self.assertIs(after, before, "运行时实例应当保留下来")
        self.assertIsNone(after.master)
        self.assertFalse(after.is_open(), "没有主控就不能再开放业务")

    def test_tick_reports_the_service_budget(self):
        report = self.build_network()
        self.assertIn("service_steps", report)
        self.assertEqual(report["service_steps"], 0, "替身服务不消耗预算")

    def test_diagnostics_include_the_three_new_sections(self):
        self.build_network()
        report = self.server.diagnostics()
        self.assertEqual(report["assembly"]["profile"], AssemblyProfile.DEV)
        self.assertEqual(
            sorted(report["assembly"]["stand_ins"]),
            [
                "替身服务 energy（待任务 04 实现）",
                "替身服务 factory（待任务 09 实现）",
                "替身服务 scheduler（待任务 04 实现）",
                "替身服务 storage（待任务 05 实现）",
            ],
        )
        self.assertGreaterEqual(len(report["assembly"]["unresolved"]), 1)
        self.assertEqual(report["runtimes"]["count"], 1)
        self.assertIn("submitted", report["application"])

    def test_scope_note_says_the_network_cannot_be_used_yet(self):
        """诊断里必须写明「业务会被拒」，否则 DEV 档看起来像网络能用了。"""
        note = self.server.diagnostics()["scope_note"]
        self.assertIn(AssemblyProfile.DEV, note)
        self.assertIn("service_absent", note)

    def test_unknown_commands_never_reach_a_service(self):
        """未知类型不兜底推断：猜一个服务出来就等于让设备绕过准入。"""
        self.build_network()
        result = self.server.submit_command(
            self.command(request_id="bad", kind="teleport")
        )
        self.assertEqual(result.status, STATUS_REJECTED, result.to_dict())
        self.assertIn(Reason.BAD_PARAMETER, result.reason_codes())


class EventType(object):
    """事件类别标识。用对象身份比较，与引擎事件的用法一致。

    名字沿用 tooldelta 的**真名**，与适配器实际挂载的事件类对齐
    （由 `testsupport.declared_event_class_names()` 从适配器代码里读出）。
    """

    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "EventType(%s)" % (self.name,)


class FakeEventApi(object):
    """模拟引擎事件总线。

    行为对齐 tooldelta：按 `(priority, event)` 分桶存放回调，
    同一优先级内按注册顺序调用，注销按**函数对象**移除。
    """

    def __init__(self, class_names=None):
        # 直接从适配器代码取类名，假总线与真适配器不可能对不上。
        names = (class_names if class_names is not None
                 else testsupport.declared_event_class_names())
        self.classes = {}
        for name in names:
            event_type = EventType(name)
            self.classes[name] = event_type
            setattr(self, name, event_type)
        self.bucket = {}
        self.listen_calls = 0
        self.remove_calls = 0
        #: 置 True 后所有 remove 都抛异常，用于验证失败留痕
        self.fail_removes = False

    # -- 引擎 API ------------------------------------------------------

    def listen(self, event_cls, priority=0):
        def wrapper(func):
            self.listen_calls += 1
            self.bucket.setdefault((priority, event_cls), []).append(func)
            return func

        return wrapper

    def remove(self, event_cls, func, priority=0):
        self.remove_calls += 1
        if self.fail_removes:
            # 模拟引擎侧注销出错（tooldelta 的 `_dyn_unlisten` 在监听器
            # 不在表里时会 `list.remove` 抛 ValueError）。
            raise ValueError("模拟的注销失败")
        bucket = self.bucket.get((priority, event_cls))
        if not bucket or func not in bucket:
            return False
        bucket.remove(func)
        if not bucket:
            del self.bucket[(priority, event_cls)]
        return True

    # -- 测试辅助 ------------------------------------------------------

    def event(self, class_name):
        """按类名取事件类别，供派发使用。"""
        return self.classes[class_name]

    def dispatch(self, event_type, payload=None):
        """按优先级从高到低派发，返回被调用的回调数。"""
        calls = 0
        for key in sorted(self.bucket.keys(), key=lambda k: -k[0]):
            if key[1] is not event_type:
                continue
            for func in list(self.bucket[key]):
                func(payload)
                calls += 1
        return calls

    def total_listeners(self):
        return sum(len(v) for v in self.bucket.values())

    def listeners_for(self, event_type):
        return sum(
            len(v) for k, v in self.bucket.items() if k[1] is event_type
        )


class SimplePayload(object):
    """可指定任意字段的事件负载替身。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class TestExplicitStopUnregisters(unittest.TestCase):
    """显式停止必须真的注销引擎监听，不能只翻一个布尔值。

    只改标志位不会让回调消失：监听器注册在进程级表里，同进程内
    stop 再 start 会留下旧回调，而重新 bind 创建的是新闭包，
    靠函数对象去重也拦不住。真机上没有暴露是因为每次都是重启进程，
    但热重载与同进程重建都会踩到。
    """

    def setUp(self):
        self.world = FakeWorld()
        self.api = FakeEventApi()
        self.server = ServerBootstrap(world=self.world, event_api=self.api)
        self.server.start(bind_engine=True)

    def test_bind_registers_listeners(self):
        self.assertGreater(self.api.total_listeners(), 0)
        self.assertTrue(self.api.listeners_for(self.api.event("OnScriptTickServer")) >= 1)

    def test_stop_removes_every_listener(self):
        self.server.stop()
        self.assertEqual(
            self.api.total_listeners(), 0, "停止后仍有监听器残留在引擎里"
        )

    def test_stop_removes_the_tick_driver(self):
        """tick 驱动也在这个出口里，不能漏。"""
        self.server.stop()
        self.assertEqual(self.api.listeners_for(self.api.event("OnScriptTickServer")), 0)

    def test_no_callback_runs_after_stop(self):
        """停止后派发事件，不应有任何回调被调用。"""
        self.server.stop()
        called = self.api.dispatch(self.api.event("OnScriptTickServer"), SimplePayload())
        self.assertEqual(called, 0)

    def test_restart_advances_exactly_once_per_tick(self):
        """重启后每帧只推进一次，而不是两次。

        `script_tick` 上本来就有两个监听器（事件计数 + tick 驱动），
        因此断言的是「重启前后数量一致」，而不是某个具体数字。
        """
        baseline = self.api.listeners_for(self.api.event("OnScriptTickServer"))
        self.assertGreater(baseline, 0)
        self.server.stop()
        self.server.start(bind_engine=True)
        self.assertEqual(
            self.api.listeners_for(self.api.event("OnScriptTickServer")), baseline,
            "重启后 tick 回调数应保持不变",
        )

        before = self.server.tick_count
        self.api.dispatch(self.api.event("OnScriptTickServer"), SimplePayload())
        self.assertEqual(
            self.server.tick_count - before, 1, "一次 tick 事件只应推进一帧"
        )

    def test_repeated_restart_does_not_accumulate(self):
        baseline = self.api.listeners_for(self.api.event("OnScriptTickServer"))
        for _ in range(5):
            self.server.stop()
            self.server.start(bind_engine=True)
        self.assertEqual(self.api.listeners_for(self.api.event("OnScriptTickServer")), baseline)
        before = self.server.tick_count
        self.api.dispatch(self.api.event("OnScriptTickServer"), SimplePayload())
        self.assertEqual(self.server.tick_count - before, 1)

    def test_stop_is_idempotent(self):
        self.server.stop()
        self.assertEqual(self.server.stop(), None)
        self.assertEqual(self.api.total_listeners(), 0)

    def test_unbind_records_failures_instead_of_swallowing(self):
        """注销失败必须留痕。

        静默吞掉的话，「注销没生效」的表现是热重载后回调翻倍——
        症状离原因很远，极难定位。失败要记进 `unregister_failures`。
        """
        self.api.fail_removes = True
        removed = self.server.event_binder.unbind()
        self.assertEqual(removed, 0)
        failures = self.server.event_binder.unregister_failures
        self.assertTrue(failures, "注销失败没有被记录")
        self.assertEqual(len(failures), self.api.total_listeners())
        self.assertIn("模拟的注销失败", failures[0])

    def test_unbind_reports_how_many_were_removed(self):
        expected = self.server.event_binder.registered_count()
        self.assertGreater(expected, 0)
        removed = self.server.event_binder.unbind()
        self.assertEqual(removed, expected)

    def test_every_registered_listener_is_tracked(self):
        """注册数必须等于实际挂上去的数量，否则一定有漏注销的。"""
        self.assertEqual(
            self.server.event_binder.registered_count(), self.api.total_listeners()
        )


class TestEventDispatchThroughBinding(unittest.TestCase):
    """经真实 `bind()` 路径派发事件，验证翻译正确。

    这一组让离线测试覆盖到适配器本身——此前适配器从不被离线导入，
    是整片验证盲区。
    """

    def setUp(self):
        self.world = FakeWorld()
        self.api = FakeEventApi()
        self.server = ServerBootstrap(world=self.world, event_api=self.api)
        self.server.start(bind_engine=True)

    def test_place_event_registers_device(self):
        self.api.dispatch(
            self.api.event("ServerPlaceBlockEntityEvent"),
            SimplePayload(dimension=0, posX=3, posY=64, posZ=4,
                          blockName=BB8),
        )
        self.server.tick()
        self.assertIsNotNone(self.server.registry.get(NodeKey(0, 3, 64, 4)))

    def test_foreign_block_is_ignored(self):
        self.api.dispatch(
            self.api.event("ServerPlaceBlockEntityEvent"),
            SimplePayload(dimension=0, posX=3, posY=64, posZ=4,
                          blockName="minecraft:stone"),
        )
        self.server.tick()
        self.assertEqual(self.server.registry.count(), 0)

    def test_remove_event_unregisters(self):
        self.api.dispatch(
            self.api.event("ServerPlaceBlockEntityEvent"),
            SimplePayload(dimension=0, posX=3, posY=64, posZ=4, blockName=BB8),
        )
        self.server.tick()
        self.api.dispatch(
            self.api.event("BlockRemoveServerEvent"),
            SimplePayload(dimension=0, x=3, y=64, z=4, fullName=BB8),
        )
        self.server.tick()
        self.assertIsNone(self.server.registry.get(NodeKey(0, 3, 64, 4)))

    def test_try_destroy_does_not_unregister(self):
        """「尝试破坏」不是「已拆除」——真机语义同样要在适配器层守住。"""
        self.api.dispatch(
            self.api.event("ServerPlaceBlockEntityEvent"),
            SimplePayload(dimension=0, posX=3, posY=64, posZ=4, blockName=BB8),
        )
        self.server.tick()
        self.api.dispatch(
            self.api.event("ServerPlayerTryDestroyBlockEvent"),
            SimplePayload(dimensionId=0, x=3, y=64, z=4, fullName=BB8),
        )
        self.server.tick()
        self.assertIsNotNone(self.server.registry.get(NodeKey(0, 3, 64, 4)))

    def test_chunk_loaded_event_registers_from_block_entities(self):
        self.api.dispatch(
            self.api.event("ChunkLoadedServerEvent"),
            SimplePayload(dimension=0, chunkPosX=0, chunkPosZ=0,
                          blockEntities=[{"posX": 0, "posY": 64, "posZ": 0,
                                          "blockName": BB8}]),
        )
        self.server.tick()
        self.assertIsNotNone(self.server.registry.get(NodeKey(0, 0, 64, 0)))


class TestLifecycleNotificationsReachTopology(unittest.TestCase):
    """跨层一致性：**注册表恢复不等于拓扑恢复**。

    这一组专门盯住「注册表对了、图没跟上」的失败模式。
    只断言 `record.loaded` 是看不出来的——必须查拓扑快照，
    否则区块重载这类问题会一路溜到 03。

    历史症状：三个设备经历加载、卸载、重载后，注册表已恢复，
    但拓扑版本仍是卸载时的版本、快照里的已加载成员仍是 0。
    """

    def setUp(self):
        self.world = FakeWorld()
        self.server = ServerBootstrap(world=self.world, backfill_chunks_per_tick=8)
        self.server.start(bind_engine=False)

    def drive(self, *events):
        for event in events:
            self.server.push(event)
        return self.server.tick()

    @staticmethod
    def chunk():
        return ChunkKey(0, 0, 0)

    def _place_three(self):
        self.drive(
            NormalizedEvent.place(key(0), BB8),
            NormalizedEvent.place(key(1), BB8),
            NormalizedEvent.place(key(2), BB8),
        )

    @staticmethod
    def _reload_event():
        """真实的区块加载事件**携带该区块的方块实体列表**。

        这一点决定了回归测试是否有效：如果事件里没有方块实体，
        重载只能靠 `mark_loaded` 恢复，那条路径本来就没坏；
        只有事件**带着**方块实体时，补查会先一步把设备静默标回已加载，
        才暴露出「注册表恢复了、拓扑没恢复」。
        """
        return NormalizedEvent.chunk_loaded(
            TestLifecycleNotificationsReachTopology.chunk(),
            block_entities=[
                {"posX": 0, "posY": 64, "posZ": 0, "blockName": BB8},
                {"posX": 1, "posY": 64, "posZ": 0, "blockName": BB8},
                {"posX": 2, "posY": 64, "posZ": 0, "blockName": BB8},
            ],
        )

    def test_chunk_reload_restores_topology_membership(self):
        self._place_three()
        component = self.server.topology.snapshot().component_of(key(0))
        self.assertEqual(len(component.loaded_members), 3)

        self.drive(NormalizedEvent.chunk_unloaded(self.chunk()))
        component = self.server.topology.snapshot().component_of(key(0))
        self.assertEqual(
            len(component.loaded_members), 0, "卸载后不应还有已加载成员"
        )

        self.drive(self._reload_event())

        # 注册表恢复
        self.assertEqual(self.server.registry.count(), 3)
        self.assertTrue(all(r.loaded for r in self.server.registry.all_records()))

        # 拓扑也必须恢复——这一条才是回归点
        component = self.server.topology.snapshot().component_of(key(0))
        self.assertEqual(
            len(component.loaded_members), 3,
            "注册表恢复了但拓扑没恢复：LOADED 变化没有发出去",
        )

    def test_reload_emits_loaded_changes(self):
        """直接盯发布出口：重载必须产出 LOADED，而不是被当成重复吞掉。"""
        self._place_three()
        self.drive(NormalizedEvent.chunk_unloaded(self.chunk()))

        seen = []
        self.server.change_sink = seen.append
        try:
            self.drive(self._reload_event())
        finally:
            self.server.change_sink = None

        kinds = [c.kind for c in seen]
        self.assertEqual(
            kinds.count(ChangeKind.LOADED), 3, "重载应发出 3 条 LOADED，实际 %r" % kinds
        )

    def test_reload_does_not_recreate_devices(self):
        """重载是恢复不是重建：代次不变、不重复派发。"""
        self._place_three()
        before = [r.generation for r in self.server.registry.all_records()]
        self.drive(NormalizedEvent.chunk_unloaded(self.chunk()))
        self.drive(self._reload_event())
        after = [r.generation for r in self.server.registry.all_records()]
        self.assertEqual(before, after)

    def test_reload_twice_still_notifies_each_time(self):
        """反复卸载/重载，每一次都要通知。"""
        self._place_three()
        for round_index in range(3):
            self.drive(NormalizedEvent.chunk_unloaded(self.chunk()))
            self.drive(self._reload_event())
            component = self.server.topology.snapshot().component_of(key(0))
            self.assertEqual(
                len(component.loaded_members), 3, "第 %d 轮重载后拓扑未恢复" % round_index
            )


class TestBackfillPublishesChanges(unittest.TestCase):
    """补查发现的设备必须进拓扑，不能只进注册表。

    历史症状：已加载区块构造启动场景，连续推进后注册表有 2 台设备，
    拓扑中对应分量为 None——补查把变化清单丢弃了。
    """

    def _server_with_loaded_chunk(self, positions, chunks_per_tick=8):
        chunk = ChunkKey(0, 0, 0)
        entities = [
            {"posX": x, "posY": y, "posZ": z, "blockName": device}
            for (x, y, z), device in positions
        ]
        world = FakeWorld(chunks=[chunk], block_entities={chunk.as_key(): entities})
        server = ServerBootstrap(world=world, backfill_chunks_per_tick=chunks_per_tick)
        server.start(bind_engine=False)
        for _ in range(4):
            server.tick()
        return server

    def test_startup_backfill_reaches_topology(self):
        server = self._server_with_loaded_chunk(
            [((0, 64, 0), BB8), ((1, 64, 0), BB8)]
        )
        self.assertEqual(server.registry.count(), 2, "注册表应有 2 台设备")

        component = server.topology.snapshot().component_of(NodeKey(0, 0, 64, 0))
        self.assertIsNotNone(component, "补查发现的设备没有进拓扑")
        self.assertEqual(len(component.members), 2)

    def test_startup_backfill_emits_added_changes(self):
        seen = []
        chunk = ChunkKey(0, 0, 0)
        world = FakeWorld(
            chunks=[chunk],
            block_entities={
                chunk.as_key(): [
                    {"posX": 0, "posY": 64, "posZ": 0, "blockName": BB8}
                ]
            },
        )
        server = ServerBootstrap(world=world, change_sink=seen.append)
        server.start(bind_engine=False)
        for _ in range(3):
            server.tick()
        self.assertIn(ChangeKind.ADDED, [c.kind for c in seen])

    def test_manual_rescan_recovers_and_publishes(self):
        """手动重扫走的是同一个出口：卸载后重扫必须把设备恢复并通知。"""
        server = self._server_with_loaded_chunk([((0, 64, 0), BB8)])
        node = NodeKey(0, 0, 64, 0)

        server.push(NormalizedEvent.chunk_unloaded(ChunkKey(0, 0, 0)))
        server.tick()
        self.assertFalse(server.registry.get(node).loaded)

        seen = []
        server.change_sink = seen.append
        try:
            server.reset_backfill()
            server.tick()
        finally:
            server.change_sink = None

        self.assertTrue(server.registry.get(node).loaded, "重扫应恢复设备")
        self.assertIn(
            ChangeKind.LOADED, [c.kind for c in seen], "重扫恢复设备后必须通知"
        )

    def test_rescan_with_no_change_publishes_nothing(self):
        """什么都没变时重扫不应产生噪声——拓扑也没必要重建。"""
        server = self._server_with_loaded_chunk([((0, 64, 0), BB8)])
        seen = []
        server.change_sink = seen.append
        try:
            server.reset_backfill()
            server.tick()
        finally:
            server.change_sink = None
        self.assertEqual(seen, [])

    def test_backfill_and_event_path_agree(self):
        """两条发现路径必须给出同样的结论（同一条实现）。"""
        by_backfill = self._server_with_loaded_chunk([((0, 64, 0), BB8)])
        record_backfill = by_backfill.registry.get(NodeKey(0, 0, 64, 0))

        server = ServerBootstrap(world=FakeWorld())
        server.start(bind_engine=False)
        server.push(NormalizedEvent.place(NodeKey(0, 0, 64, 0), BB8))
        server.tick()
        record_event = server.registry.get(NodeKey(0, 0, 64, 0))

        self.assertEqual(record_backfill.device_id, record_event.device_id)
        self.assertEqual(record_backfill.generation, record_event.generation)
        self.assertEqual(record_backfill.loaded, record_event.loaded)


class TestClientLifecycle(unittest.TestCase):
    def test_client_starts_and_stops(self):
        from endernet.client.bootstrap import ClientBootstrap

        client = ClientBootstrap()
        client.start()
        self.assertTrue(client.started)
        client.stop()
        self.assertFalse(client.started)

    def test_client_holds_no_authoritative_state(self):
        """客户端不得持有库存或订单（架构规划 6.6）。"""
        from endernet.client.bootstrap import ClientBootstrap

        client = ClientBootstrap()
        client.start()
        report = client.diagnostics()
        self.assertNotIn("inventory", report)
        self.assertNotIn("orders", report)


if __name__ == "__main__":
    unittest.main()
