# coding=utf-8
"""01-01 / 01-06 验收：入口可脱离游戏构造、可显式销毁、分帧补查有界。

这一组测试同时是「模组能不能加载」的离线检查：
`modMain.py` 依赖 `server.bootstrap.start_server` 与 `client.bootstrap.start_client`，
如果这两个入口或它们引用的模块有任何导入期错误，这里会直接失败。
"""

import json
import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import DefinitionSet
from endernet.common.identity import ChunkKey, DeviceRef, NodeKey
from endernet.common.resource_key import ResourceKey
from endernet.common.protocol import (
    ORDER_SOURCE_FEED_WAITING_INPUTS,
    ORDER_SOURCE_STAGE_DONE,
    ORDER_SOURCE_STAGE_RUNNING,
    ORDER_SOURCE_STAGE_WAITING,
    CommandKind,
    CommandSource,
    DeviceCommand,
    MSG_DEVICE_STATUS,
    MSG_SESSION_READY,
    Reason,
    SessionOpen,
    STATUS_BLOCKED,
    STATUS_OK,
    STATUS_REJECTED,
    SyncEnvelope,
    VIEW_ITEM,
)
from endernet.server.adapters.containers import FakeContainer
from endernet.server.bootstrap import (
    ServerBootstrap,
    _order_memory,
    _order_row,
    _order_source_rows,
)
from endernet.server.crafting import patterns
from endernet.server.crafting.jobs import Order, OrderStep
from endernet.server.crafting.planner import PlanPolicy, TargetSemantics
from endernet.server.crafting.store import PATTERN_SETTLE_FRAMES
from endernet.server.network.controller import RunState
from endernet.server.network.runtime import RuntimeChange
from endernet.server.network.services import AssemblyProfile
from endernet.server.runtime.events import EventKind, NormalizedEvent
from endernet.server.runtime.lifecycle import ChangeKind
from endernet.server.sync.sessions import SessionPolicy
from endernet.server.sync.terminal import SyncPolicy, TerminalSyncHub

BB8 = "endernet:network_baseband_8"
TEMPLATE_IO = "endernet:bus_template_io"
PASSIVE_IO = "endernet:bus_item_io"
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

    def drop_items_at(self, node_key, items):
        """假世界也记账：真实现走 `CreateEngineItemEntity`，这里只记下来。

        `items` 是 `((ResourceKey, 数量), ...)`——字典形状由适配器自己构造
        （真机要旧式键名、且不接受 `None` 的 `customTips`/`durability`）。
        """
        self.dropped.append((node_key, list(items)))
        return len(items)

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
        self.assertEqual(report["definitions"]["devices"], 23)
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
        # 现在拦住它的是**能源判定**：默认口径下算不出需求（基带耗能未列、
        # 工作态没接线），因此是「待确认」而不是「没电」也不是「服务没写」。
        self.assertEqual(runtime.state(), RunState.NO_POWER)

    def test_business_commands_are_refused_by_the_energy_verdict(self):
        """拦下业务的不再是替身服务，而是当地那台网络的能源判定。

        默认装配档下能源是**正式实现**，所以拒绝理由从
        「服务尚未实现」变成了「能源判定尚未确认」——同样是诚实的结果，
        但读的人该去查的是供能与定义表，不是「04 还没写」。
        """
        self.build_network()
        result = self.server.submit_command(self.command(request_id="r1"))
        self.assertEqual(result.status, STATUS_BLOCKED, result.to_dict())
        self.assertIn(Reason.ENERGY_UNAVAILABLE, result.reason_codes())
        self.assertNotIn(Reason.SERVICE_ABSENT, result.reason_codes())

    def test_status_is_answered_while_business_is_refused(self):
        self.build_network()
        result = self.server.submit_command(
            self.command(request_id="s1", kind=CommandKind.STATUS)
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertEqual(result.payload["run_state"], RunState.NO_POWER)
        self.assertIn(
            "energy_unavailable", [r["code"] for r in result.payload["blockers"]]
        )

    def test_device_status_is_readable_and_honest(self):
        """状态栏说的是「能源判定尚未确认」，不是「没电」也不是「离线故障」。

        `assertNotIn(BROWNOUT)` 这一句是本节的重点：**「还没法确认」与
        「确认没电」是两句不同的话**，而默认口径必须是前者——把它说成断电
        会让人去修供电，真正该做的是把定义表与运行期输入补齐。
        """
        self.build_network()
        ref = self.server.registry.get(key(0, 65)).ref
        status = self.server.application.device_status(ref)
        self.assertFalse(status.is_online)
        self.assertIn(Reason.ENERGY_UNAVAILABLE, status.reason_codes())
        self.assertNotIn(Reason.BROWNOUT, status.reason_codes())
        self.assertNotIn(Reason.SERVICE_ABSENT, status.reason_codes())

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
        """服务推进量落在预算内；没有到期工作时为 0。

        原先断言的是恒为 0（「服务都是替身」），那在调度器落地后既太弱也不
        真实——真正要钉住的是**预算上界**这条不变量。
        """
        report = self.build_network()
        self.assertIn("service_steps", report)
        self.assertGreaterEqual(report["service_steps"], 0)
        self.assertLessEqual(
            report["service_steps"], self.server.runtime_budget
        )
        self.assertEqual(
            report["service_steps"], 0, "本帧没有到期任务，不该消耗预算"
        )

    def test_diagnostics_include_the_three_new_sections(self):
        self.build_network()
        report = self.server.diagnostics()
        self.assertEqual(report["assembly"]["profile"], AssemblyProfile.DEV)
        # 调度核心（04）、存储领域层（05）、能源的固定结算（04）与
        # 订单执行（09）都已落地，四个槽位一个替身都不剩。
        self.assertEqual(sorted(report["assembly"]["stand_ins"]), [])
        self.assertGreaterEqual(len(report["assembly"]["unresolved"]), 1)
        self.assertEqual(report["runtimes"]["count"], 1)
        self.assertIn("submitted", report["application"])
        # 能源那一节读的是**判定**，不是「有没有写」。
        self.assertIn("energy", report)
        self.assertEqual(report["energy"]["networks"], 1)
        self.assertEqual(report["energy"]["pending"], 1)
        # 订单那一节（09）同理：**首要信息是「回流入口还没接线」**，
        # 否则「订单为什么卡住」在最外层看起来像什么都没发生。
        self.assertIn("order", report)
        self.assertFalse(report["order"]["returns_sink"]["available"])
        self.assertEqual(report["order"]["returns_sink"]["pending_task"], "10")

    def test_scope_note_says_the_network_cannot_be_used_yet(self):
        """诊断里必须写明「业务会被拒」，否则 DEV 档看起来像网络能用了。

        而且要说清**为什么**：拒绝理由现在是能源/带宽的「待确认」，
        不是「服务没写」——这两句会把人引向完全不同的排查方向。
        """
        note = self.server.diagnostics()["scope_note"]
        self.assertIn(AssemblyProfile.DEV, note)
        self.assertIn("energy_unavailable", note)
        self.assertIn("不是「没电」", note)

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
        self.server = ServerBootstrap(
            world=self.world,
            event_api=self.api,
            sync_events=testsupport.FakeEventsModule(),
        )
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
        self.server = ServerBootstrap(
            world=self.world,
            event_api=self.api,
            sync_events=testsupport.FakeEventsModule(),
        )
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


class _ContainerProvider(object):
    """位置 -> 假容器（07 的夹具）。与 06 的同类夹具同形。"""

    def __init__(self, slots=9):
        self.slots = slots
        self.by_position = {}

    def container_at(self, node_key):
        position = node_key.as_key()
        if position not in self.by_position:
            self.by_position[position] = FakeContainer(
                slots=self.slots, max_stack=1
            )
        return self.by_position[position]

    def diagnostics(self):
        # `io_diagnostics()` 会伸手要它，与本仓库其他提供者替身保持同形。
        return {"fake": True, "cached": len(self.by_position)}


class _PutEvent(object):
    """`PlayerTryPutCustomContainerItemServerEvent` 的替身。

    **`cancel()` 的存在与写法对齐 tooldelta**：那边是往原始事件字典里
    写 `cancel = True`。这里只要能被调用并记下来即可。
    """

    def __init__(self, item_name, x, y, z, playerId="player-1"):
        self.item = SimplePayload(newItemName=item_name)
        self.playerId = playerId
        self.x, self.y, self.z = x, y, z
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class TestPatternContainerEvents(unittest.TestCase):
    """容器事件 -> 样板索引（07）。

    这一组让离线测试覆盖到**适配器里的样板接线**：哪些事件当脏标记、
    哪些插槽要被拒收。它守的是三件只在真机上才看得见的事：

    1. 玩家手工拖进去的样板能被发现（容器没有「放了什么」的通知顺序）；
    2. 样板槽拒收普通原料与不适用的样板类型；
    3. **被动接口绝不被误伤**——那是 06 的 9 格存取位。
    """

    def setUp(self):
        self.world = FakeWorld()
        self.api = FakeEventApi()
        self.containers = _ContainerProvider()
        self.server = ServerBootstrap(
            world=self.world,
            event_api=self.api,
            container_provider=self.containers,
            # 引擎的 `GetPlayerDimensionId` 在离线环境不存在。**注入替身**
            # 而不是让那条路径整个走不通——插槽拒收正挂在它上面。
            player_dimension=lambda player_id: 0,
            # 同步通道要的三个事件基类同理：`tooldelta.events` 在游戏外
            # 导不进来，而注册发生在 `start()` 里，不注入就整个走不通。
            sync_events=testsupport.FakeEventsModule(),
        )
        self.server.start(bind_engine=True)

    # -- 辅助 ----------------------------------------------------------

    def place(self, device_id, x, y=64, z=0):
        self.api.dispatch(
            self.api.event("ServerPlaceBlockEntityEvent"),
            SimplePayload(dimension=0, posX=x, posY=y, posZ=z, blockName=device_id),
        )
        self.server.tick()
        record = self.server.registry.get(key(x, y, z))
        self.assertIsNotNone(record, device_id)
        return record.ref

    def container_for(self, x, y=64, z=0):
        return self.containers.container_at(key(x, y, z))

    def settle(self):
        """推进足够帧数让样板层重读（它是去抖的，不会立刻读）。"""
        self.server.clock.advance(PATTERN_SETTLE_FRAMES + 1)
        self.server.tick()

    def factory_pattern(self, count=1):
        return patterns.PatternDefinition(
            patterns.MODE_FACTORY,
            [patterns.PatternStack(ResourceKey.item("minecraft:iron_ingot"), count)],
            [patterns.PatternStack(ResourceKey.item("minecraft:iron_block"), 1)],
        )

    # -- 脏标记 -> 重读 ------------------------------------------------

    def test_container_change_is_discovered_after_settling(self):
        ref = self.place(TEMPLATE_IO, 7)
        container = self.container_for(7)
        container.write_slot(2, self.server.patterns.template_item(self.factory_pattern()))
        self.api.dispatch(
            self.api.event("ContainerItemChangedServerEvent"),
            SimplePayload(dimensionId=0, pos=(7, 64, 0), slot=2),
        )
        state = self.server.patterns.get_by_ref(ref)
        self.assertEqual(state.entries, {}, "收到变更的同一帧不该立刻读到（内容还没落定）")
        self.settle()
        self.assertEqual(sorted(state.entries), [2])

    def test_container_change_without_a_position_is_ignored(self):
        self.place(TEMPLATE_IO, 7)
        # 引擎在某些容器类型上会把 `pos` 给成 None（ModAPI 备注如此）。
        self.api.dispatch(
            self.api.event("ContainerItemChangedServerEvent"),
            SimplePayload(dimensionId=0, pos=None, slot=0),
        )
        self.assertEqual(self.server.diagnostics()["tick_errors"]["count"], 0)

    def test_player_remove_marks_dirty(self):
        ref = self.place(TEMPLATE_IO, 7)
        state = self.server.patterns.get_by_ref(ref)
        self.api.dispatch(
            self.api.event("PlayerTryRemoveCustomContainerItemServerEvent"),
            SimplePayload(playerId="p", x=7, y=64, z=0),
        )
        self.assertGreater(state.dirty_tick, -1)

    def test_hopper_events_mark_dirty(self):
        ref = self.place(TEMPLATE_IO, 7)
        state = self.server.patterns.get_by_ref(ref)
        for name in (
            "ItemPushInCustomContainerServerEvent",
            "ItemPullOutCustomContainerServerEvent",
        ):
            state.dirty_tick = -1
            self.api.dispatch(
                self.api.event(name),
                SimplePayload(dimension=0, x=7, y=64, z=0),
            )
            self.assertGreater(state.dirty_tick, -1, name)

    def test_events_for_unknown_positions_are_harmless(self):
        self.place(TEMPLATE_IO, 7)
        self.api.dispatch(
            self.api.event("ContainerItemChangedServerEvent"),
            SimplePayload(dimensionId=0, pos=(999, 64, 0), slot=0),
        )
        self.assertEqual(self.server.diagnostics()["tick_errors"]["count"], 0)

    def test_a_missing_player_dimension_is_not_fatal(self):
        """拿不到玩家维度时放弃这一次，**不猜一个维度**。"""
        server = ServerBootstrap(
            world=self.world,
            event_api=self.api,
            container_provider=self.containers,
            player_dimension=lambda player_id: (_ for _ in ()).throw(ValueError("no")),
            sync_events=testsupport.FakeEventsModule(),
        )
        server.start(bind_engine=True)
        server.event_binder._on_custom_put(
            _PutEvent("minecraft:stone", 7, 64, 0)
        )
        self.assertEqual(server.diagnostics()["tick_errors"]["count"], 0)

    # -- 插槽拒收 ------------------------------------------------------

    def test_a_plain_item_is_rejected_from_the_pattern_slots(self):
        """14 验收：「样板槽拒收普通原料」。"""
        self.place(TEMPLATE_IO, 7)
        event = _PutEvent("minecraft:stone", 7, 64, 0)
        self.api.dispatch(self.api.event("PlayerTryPutCustomContainerItemServerEvent"), event)
        self.assertTrue(event.cancelled)

    def test_a_crafting_template_is_rejected_from_the_factory_slots(self):
        """「拒收不适用的样板类型」（07-02/04/05 的规格补充）。"""
        self.place(TEMPLATE_IO, 7)
        event = _PutEvent("endernet:crafting_template", 7, 64, 0)
        self.api.dispatch(self.api.event("PlayerTryPutCustomContainerItemServerEvent"), event)
        self.assertTrue(event.cancelled)

    def test_a_factory_template_is_accepted(self):
        ref = self.place(TEMPLATE_IO, 7)
        event = _PutEvent("endernet:factorical_template", 7, 64, 0)
        self.api.dispatch(self.api.event("PlayerTryPutCustomContainerItemServerEvent"), event)
        self.assertFalse(event.cancelled)
        self.assertGreater(self.server.patterns.get_by_ref(ref).dirty_tick, -1)

    def test_the_passive_interface_is_never_touched(self):
        """**被动接口的 9 格是 06 的存取位，样板层一律放行。**

        误伤的后果很重：玩家往总线物品接口里放石头会被无声地挡回去，
        而那条通路（外界 -> 缓冲 -> 网络）正是 06 的全部意义。
        """
        self.place(PASSIVE_IO, 8)
        for name in ("minecraft:stone", "endernet:crafting_template"):
            event = _PutEvent(name, 8, 64, 0)
            self.api.dispatch(
                self.api.event("PlayerTryPutCustomContainerItemServerEvent"), event
            )
            self.assertFalse(event.cancelled, name)

    def test_a_hopper_cannot_bypass_the_rejection(self):
        """**漏斗是第二条放入路径，拒收规则必须同样生效。**

        少了它，一台对着样板接口的漏斗能把任意物品塞进那 9 格——而
        `blocks.md` 明写它们「专用于存放工厂样板」，14 的验收表要求
        「样板槽拒收普通原料」。这与玩家拖入是同一个规则，两条路径共用
        `_veto_or_mark` 一份实现。
        """
        self.place(TEMPLATE_IO, 7)
        event = _PutEvent("minecraft:stone", 7, 64, 0)
        event.dimension = 0
        self.api.dispatch(
            self.api.event("ItemPushInCustomContainerServerEvent"), event
        )
        self.assertTrue(event.cancelled)
        self.assertEqual(
            self.server.pattern_diagnostics()["container_events"][
                "pattern_slot_rejected"
            ],
            1,
        )

    def test_a_hopper_may_feed_a_pattern_template_in(self):
        """反过来：工厂样板**可以**用漏斗送进来（那是合法内容）。"""
        ref = self.place(TEMPLATE_IO, 7)
        event = _PutEvent("endernet:factorical_template", 7, 64, 0)
        event.dimension = 0
        self.api.dispatch(
            self.api.event("ItemPushInCustomContainerServerEvent"), event
        )
        self.assertFalse(event.cancelled)
        self.assertGreater(self.server.patterns.get_by_ref(ref).dirty_tick, -1)

    def test_the_passive_interface_is_not_touched_by_the_hopper_path(self):
        """被动接口的 9 格正是给外部物流用的，漏斗往里塞东西是它的设计用途。"""
        self.place(PASSIVE_IO, 8)
        event = _PutEvent("minecraft:stone", 8, 64, 0)
        event.dimension = 0
        self.api.dispatch(
            self.api.event("ItemPushInCustomContainerServerEvent"), event
        )
        self.assertFalse(event.cancelled)

    def test_a_hopper_event_without_an_item_still_marks_dirty(self):
        """读不出物品名时**照旧标脏**：容器确实变了，那与能否读出名字无关。
        漏掉这一支的后果是「某些改法不进索引」。"""
        ref = self.place(TEMPLATE_IO, 7)
        self.api.dispatch(
            self.api.event("ItemPushInCustomContainerServerEvent"),
            SimplePayload(dimension=0, x=7, y=64, z=0),
        )
        self.assertGreater(self.server.patterns.get_by_ref(ref).dirty_tick, -1)
        self.assertEqual(self.server.diagnostics()["tick_errors"]["count"], 0)

    def test_a_rejected_put_does_not_mark_dirty(self):
        """被挡回去的东西没有落进容器，不需要重读。"""
        ref = self.place(TEMPLATE_IO, 7)
        state = self.server.patterns.get_by_ref(ref)
        before = state.dirty_tick
        self.api.dispatch(
            self.api.event("PlayerTryPutCustomContainerItemServerEvent"),
            _PutEvent("minecraft:stone", 7, 64, 0),
        )
        self.assertEqual(state.dirty_tick, before)

    def test_rejections_are_counted_for_diagnostics(self):
        self.place(TEMPLATE_IO, 7)
        self.api.dispatch(
            self.api.event("PlayerTryPutCustomContainerItemServerEvent"),
            _PutEvent("minecraft:stone", 7, 64, 0),
        )
        report = self.server.pattern_diagnostics()
        self.assertEqual(report["container_events"]["pattern_slot_rejected"], 1)
        self.assertEqual(report["container_events"]["custom_container_put"], 1)

    def test_container_events_are_counted(self):
        """计数是「这条事件在真机上到底派发没有」的离线可读证据。"""
        self.place(TEMPLATE_IO, 7)
        self.api.dispatch(
            self.api.event("ContainerItemChangedServerEvent"),
            SimplePayload(dimensionId=0, pos=(7, 64, 0), slot=0),
        )
        self.assertEqual(
            self.server.pattern_diagnostics()["container_events"]["container_changed"],
            1,
        )


class TestPatternWiring(unittest.TestCase):
    """装配层：样板层确实挂上了、也确实被销毁与推进。"""

    def setUp(self):
        # 用假世界而不是默认的 `ToolDeltaWorldAdapter`：默认那个在需要时
        # 会去 import 引擎，而这些用例一个引擎接口都不该碰。
        self.server = ServerBootstrap(world=FakeWorld())

    def test_patterns_are_assembled(self):
        self.assertIsNotNone(self.server.patterns)
        self.assertIn("pattern", self.server.diagnostics())

    def test_the_pattern_policy_is_in_the_assembly_check(self):
        """R09 仍未定的那一条必须进 `policy_bundle()`，否则正式验收时漏网。

        `pattern.unlimited_fuel`（2026-09-30 定案：外部自理）与
        `pattern.return_rule`（同日定案：返还物一律显式声明）**都不在这里**，
        留在策略字段里但不进报告（见 `test_patterns.TestPolicy`）。
        """
        fields = [
            item["field"] for item in self.server.policy_bundle().unresolved_report()
        ]
        self.assertNotIn("pattern.return_rule", fields)
        self.assertIn("pattern.kind_scope", fields)
        self.assertNotIn("pattern.unlimited_fuel", fields)

    def test_stop_marks_patterns_unloaded(self):
        """停机**不清样板**：它们是方块容器里的实物物品，不是内存状态。"""
        outcome = self.server.registry.register(key(7), TEMPLATE_IO)
        ref = outcome.record.ref
        state = self.server.patterns.ensure(ref, TEMPLATE_IO)
        self.assertTrue(state.loaded)
        self.server.start(bind_engine=False)
        self.server.stop()
        self.assertFalse(state.loaded)
        self.assertIsNotNone(self.server.patterns.get_by_ref(ref))

    def test_tick_reports_pattern_reads(self):
        self.server.start(bind_engine=False)
        report = self.server.tick()
        self.assertIn("pattern_reads", report)

    def test_diagnostics_say_what_is_not_done(self):
        report = self.server.pattern_diagnostics()
        self.assertIn("09", report["scope_note"])
        self.assertIn("07-03", report["scope_note"])


class TestPlannerWiring(unittest.TestCase):
    """装配层：规划器确实挂上了、也确实进了未定规则清单（08）。"""

    def setUp(self):
        self.server = ServerBootstrap(world=FakeWorld())

    def test_the_planner_is_assembled(self):
        self.assertIsNotNone(self.server.planner)
        self.assertIn("plan", self.server.diagnostics())

    def test_the_plan_policy_is_in_the_assembly_check(self):
        """R11/R04/08-04 必须在 `policy_bundle()` 里，否则正式验收时漏网。

        这八条与 R09 那三条**各放各的**：合成一个字段名会让「哪条规则在拦我」
        查不出来（一个是「这份配方要备哪些料」，一个是「要几个批次」）。
        """
        fields = [
            item["field"] for item in self.server.policy_bundle().unresolved_report()
        ]
        for expected in (
            "plan.target_semantics",
            "plan.candidate_order",
            "plan.alternative_recipes",
            "plan.max_depth",
            "plan.max_steps",
            "plan.max_candidates",
            "plan.max_batches_per_step",
            "plan.byte_rounding",
        ):
            self.assertIn(expected, fields)

    def test_the_planner_shares_the_assembly_policies(self):
        """门面里绑的必须就是**全进程那一份**策略对象，否则诊断报的数值
        与真正生效的对不上（与 `SchedulerPolicy`/`EnergyPolicy` 同一条纪律）。"""
        self.assertIs(self.server.planner.policy, self.server.plan_policy)
        self.assertIs(self.server.planner.pattern_policy, self.server.pattern_policy)

    def test_an_injected_policy_is_the_one_that_runs(self):
        policy = PlanPolicy(target_semantics=TargetSemantics.PRODUCE)
        server = ServerBootstrap(world=FakeWorld(), plan_policy=policy)
        self.assertIs(server.planner.policy, policy)
        self.assertEqual(server.plan_diagnostics()["policy"]["target_semantics"],
                         TargetSemantics.PRODUCE)

    def test_assert_assembly_ok_refuses_the_unresolved_plan_rules(self):
        """正式档的装配检查必须把规划侧的未定项报出来——它**不会被自动调用**，
        因此这里显式调一次，证明它确实拦得住。"""
        problems = self.server.assert_assembly_ok()
        self.assertTrue(
            any("plan.target_semantics" in item for item in problems), problems
        )
        self.assertTrue(
            any("plan.byte_rounding" in item for item in problems), problems
        )

    def test_diagnostics_say_what_is_not_done(self):
        report = self.server.plan_diagnostics()
        self.assertIn("09", report["scope_note"])
        self.assertIn("只读", report["scope_note"])
        # 逐次计划的内容属于订单，不在启动层留缓存（那是第二份真相）。
        self.assertNotIn("steps", report)

    def test_the_planner_reports_its_policy(self):
        report = self.server.plan_diagnostics()
        self.assertEqual(len(report["unresolved"]), 8)
        self.assertIn("pattern_policy", report)


class TestSyncWiring(unittest.TestCase):
    """装配层：同步中枢真的接上了几个口子（11-01/11-02 的接线，任务 11 步骤 6）。

    **这一组不测同步语义**——会话校验、发布窗口、分块传输分别由
    `test_sync_sessions.py` / `test_sync_publish.py` 覆盖，它们直接驱动中枢。
    这里只回答「组合根有没有把这些口子接到正确的东西上」，因为接错了不会
    报错，而且**不同原因的症状一模一样**：身份取不到、上下文接错、设备引用
    传错对象，三者都表现成「终端一台都打不开」。只有把每一段单独钉住，
    实机上那个症状才查得下去。

    **夹具用默认策略**（不注入 `session_policy`），并给端口接上假的维度与坐标
    解析——于是「离线没有引擎可问坐标」这件事不再需要靠放宽策略来绕开，
    `require_position` 的默认口径（查不到就拒）在绝大多数用例里都是**开着**的。
    专门要验 fail closed 的那一条自己把坐标解析摘掉（见
    `test_the_default_policy_refuses_a_session_without_a_position`）。
    """

    BB8 = BB8
    TINY = "endernet:tiny_network_endpoint"
    #: 建网时那台驱动器所在的位置，也是假引擎默认把玩家放在的地方。
    DEVICE = key(0, 65)

    def setUp(self):
        self.world = FakeWorld()
        self.api = FakeEventApi()
        #: 留着这个实例：`_on_sync_request` 的回包发到它的 S2C `send` 上，
        #: 断言「回给谁」靠它。真引擎那一侧另有 `AdapterSyncPort.stats` 计数，
        #: 两者分开是「没人在收」与「发不出去」要能分开看。
        self.events = testsupport.FakeEventsModule()
        #: 假引擎的玩家脚部坐标。默认「谁都在设备那一格上」，需要拉开距离的
        #: 用例自己往这里塞一项。**按玩家分开**而不是一个全局值：否则
        #: 「只关掉离线那个人的会话」这类用例会因为两人都在同一处而失去意义。
        self.positions = {}
        self.server = self.build()

    # -- 假的引擎位置查询 ----------------------------------------------

    def player_dimension(self, player_id):
        # type: (str) -> object
        """假引擎：玩家在维度 0（本组用例的设备都建在那一维）。"""
        return 0

    def player_position(self, player_id):
        # type: (str) -> object
        """假引擎：玩家脚部坐标，默认就在设备那一格上（距离为 0）。"""
        return self.positions.get(player_id, self.DEVICE.as_tuple())

    def build(self, **kwargs):
        # type: (...) -> object
        """建一个已绑定引擎、带假事件模块的服务端。

        **维度解析只注入一次、两处共用**（容器事件与会话校验），所以这里
        只写一个 `player_dimension`；传 `None` 即退回引擎实现——离线拿不到
        就返回 `None`，而那是「拒绝」，不是「放行」。
        """
        kwargs.setdefault("world", FakeWorld())
        kwargs.setdefault("event_api", self.api)
        kwargs.setdefault("sync_events", self.events)
        kwargs.setdefault("player_dimension", self.player_dimension)
        kwargs.setdefault("player_position", self.player_position)
        server = ServerBootstrap(**kwargs)
        server.start(bind_engine=True)
        return server

    def drive(self, *events):
        for event in events:
            self.server.push(event)
        return self.server.tick()

    def build_network(self):
        """基带 → 控制器 → 驱动器：一套能开出运行时的最小网络。"""
        return self.drive(
            NormalizedEvent.place(key(0), self.BB8),
            NormalizedEvent.place(key(1), self.BB8),
            NormalizedEvent.place(key(1, 65), self.TINY),
            NormalizedEvent.place(key(0, 65), DRIVER),
        )

    def device_ref(self):
        return self.server.registry.get(key(0, 65)).ref

    def open_message(self, device_ref, request_id="r1", player_id="p1"):
        # type: (object, str, str) -> object
        """把一条 `session_open` 装成引擎会交给监听器的样子。

        **走一遍 JSON 往返**：线上就是这么走的，能顺带钉住「这些字段真的
        可序列化」。`__id__` 与消息体**平级**，因为引擎就是这么附上去的；
        消息体里没有玩家字段（协议层根本没定义），因此没有可伪造的余地。
        """
        envelope = SyncEnvelope(
            SessionOpen(request_id, device_ref, 0), view_kind=VIEW_ITEM
        )
        data = json.loads(json.dumps(envelope.to_dict()))
        data["__id__"] = player_id
        return SimplePayload(data=data)

    # -- 装配 ----------------------------------------------------------

    def test_the_port_is_attached_to_the_hub(self):
        self.assertIs(self.server.sync.port, self.server.sync_port)

    def test_the_runtime_manager_uses_the_hub_as_its_session_probe(self):
        """主控的命令准入探针必须就是 `hub.accepts`。

        接不上（构造顺序反了）时主控拿到 `None`，而那等于**全部放行**——
        一道看起来在、其实没有的门禁。
        """
        probe = self.server.runtimes.session_check
        self.assertIsNotNone(probe)
        self.assertTrue(probe(CommandSource("p1", None, 0)), "无会话的命令一律放行")
        self.assertFalse(
            probe(CommandSource("p1", None, 0, session_id="s-not-real")),
            "带一个不存在的会话号必须被拒",
        )

    def test_an_injected_probe_is_not_overridden(self):
        """05/08 的既有调用方会自己传探针，覆盖它属于改别人的可见行为。"""

        def sentinel(source):
            return False

        server = self.build(session_check=sentinel)
        self.assertIs(server.runtimes.session_check, sentinel)

    def test_both_sync_listeners_are_registered_and_unregistered(self):
        """C2S 与玩家离线两条监听器都挂在同一个注册账本上。

        **必须走 `event_binder`**：自己留一份账本的话 `stop()` 会漏掉它们，
        而热重载后回调翻倍的症状极难定位（01 已经吃过一次亏）。
        """
        c2s, _ = self.server.sync_port.classes()
        self.assertGreaterEqual(self.api.listeners_for(c2s), 1)
        self.assertGreaterEqual(
            self.api.listeners_for(self.events.DelServerPlayerEvent), 1
        )

        self.server.stop()
        self.assertEqual(self.api.listeners_for(c2s), 0)
        self.assertEqual(self.api.listeners_for(self.events.DelServerPlayerEvent), 0)

    # -- 请求路径 ------------------------------------------------------

    def test_a_c2s_message_reaches_the_hub(self):
        """总线 → 端口 → 中枢 → 回给**同一个**玩家。

        这是整条同步通道离线能跑到的最远一处：再往后就是引擎真的发包。
        """
        self.build_network()
        c2s, _ = self.server.sync_port.classes()

        self.api.dispatch(c2s, self.open_message(self.device_ref()))

        self.assertEqual(self.server.sync_port.stats["requests"], 1)
        self.assertEqual(self.server.sync_port.stats["handler_errors"], 0)
        self.assertEqual(self.server.sync.stats["sessions_opened"], 1)
        self.assertEqual(len(self.events.sent), 1, "回包必须发出去")
        self.assertEqual(self.events.sent[0][0], "p1", "回给发消息的那个人")

    def test_the_reply_is_encoded_and_decodable(self):
        """回包是**线上格式**：能解回来，且带着该会话的资源描述表。

        编码在 hub 里做（那张表只有它拿得到），因此解码必须用**同一个**
        会话实例上的那张表——用别的表解出来的东西看着也对，只是资源序号
        指向了别的键。
        """
        self.build_network()
        c2s, _ = self.server.sync_port.classes()
        self.api.dispatch(c2s, self.open_message(self.device_ref()))

        _, payload = self.events.sent[0]
        session = self.server.sync.registry.get(payload["session_id"])
        envelope = SyncEnvelope.from_dict(
            json.loads(json.dumps(payload)), session.descriptions
        )
        self.assertEqual(envelope.message.kind, MSG_SESSION_READY)
        self.assertEqual(envelope.session_id, session.session_id)

    def test_a_self_declared_player_is_ignored(self):
        """消息体里塞一个玩家 id 只会让整条消息被判非法。

        **协议层没有那个字段**，于是这里没有「忘了忽略它」的机会：
        身份只有一个来源，就是引擎附的 `__id__`。
        """
        self.build_network()
        c2s, _ = self.server.sync_port.classes()
        message = self.open_message(self.device_ref())
        message.data["player_id"] = "p2"

        self.api.dispatch(c2s, message)

        self.assertEqual(self.server.sync.stats["sessions_opened"], 0)
        self.assertEqual(self.server.sync_port.stats["requests"], 1)

    def test_the_session_belongs_to_the_engine_identity(self):
        """`__id__` 换成谁，会话就开在谁名下——消息体说了不算。"""
        self.build_network()
        c2s, _ = self.server.sync_port.classes()

        self.api.dispatch(c2s, self.open_message(self.device_ref(), player_id="p9"))

        sessions = self.server.sync.registry.sessions_for_player("p9")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(self.server.sync.registry.count(), 1)

    def test_a_message_from_an_unknown_place_is_refused_not_crashed(self):
        """没有网络的设备：拒绝并计数，而不是抛穿引擎回调。"""
        c2s, _ = self.server.sync_port.classes()
        message = self.open_message(DeviceRef(key(30, 64, 30), 1))
        self.api.dispatch(c2s, message)

        self.assertEqual(self.server.sync.stats["sessions_refused"], 1)
        self.assertEqual(self.server.sync_port.stats["handler_errors"], 0)

    def test_a_player_leaving_closes_his_sessions(self):
        """离线事件经端口到中枢。

        不接它的后果不是报错，是**会话泄漏**：人走了订阅表里还留着，
        发布窗口从此每帧都以为有人在看，而包发给一个已经不在的玩家。
        """
        self.build_network()
        c2s, _ = self.server.sync_port.classes()
        self.api.dispatch(c2s, self.open_message(self.device_ref()))
        self.assertEqual(self.server.sync.registry.count(), 1)

        self.api.dispatch(self.events.DelServerPlayerEvent, SimplePayload(id="p1"))

        self.assertEqual(self.server.sync_port.stats["leaves"], 1)
        self.assertEqual(self.server.sync.registry.count(), 0)

    # -- 设备总体状态（11-08/11-13）-----------------------------------

    def test_the_device_status_reader_is_the_runtime_lookup(self):
        """状态口子必须接在**运行时**上：接错了不报错，只是界面永远不亮。

        `sync/` 不许 import `server/network/`，所以这一格只能是注入的绑定
        方法。绑错对象（比如绑成存储、或绑成 `None` 兜底）时症状是「状态栏
        一直是空的」，与「设备真的离线」在界面上长得一样。
        """
        #: 绑定方法每次取属性都是新对象，因此比相等而不是同一。
        self.assertEqual(self.server.sync._status_of, self.server._sync_status_of)

    def test_a_status_packet_follows_the_session(self):
        """开会话 → 推进一帧 → 状态包发给同一个玩家，且带着该会话的路由字段。

        这条把「组合根把状态口子接上了」钉到可观察的层面：读不到状态的
        时候一个包都不会有，而这里的写入方没有任何别的来源。
        """
        self.build_network()
        c2s, _ = self.server.sync_port.classes()
        self.api.dispatch(c2s, self.open_message(self.device_ref()))
        # `FakeEventsModule` 把 `sent` 捕进了闭包，重新赋值不会改发送目标，
        # 因此记长度、取增量，而不是清空列表。
        before = len(self.events.sent)

        self.server.tick()

        payloads = [payload for _pid, payload in self.events.sent[before:]]
        statuses = [p for p in payloads if p["message"]["kind"] == MSG_DEVICE_STATUS]
        self.assertEqual(len(statuses), 1, payloads)
        status = statuses[0]
        session = self.server.sync.registry.sessions_for_player("p1")[0]
        self.assertEqual(status["session_id"], session.session_id)
        self.assertEqual(status["network_id"], session.network_id)
        self.assertEqual(status["epoch"], session.epoch)
        self.assertEqual(status["message"]["device_id"], DRIVER)
        self.assertEqual(status["message"]["state_text"], "离线")

    def test_the_status_query_writes_nothing(self):
        """11-08：「状态查询不触发业务写入」。读是纯的，版本与账本都不动。

        查一次就推进一次版本、或往去重账本里记一笔的话，症状是「玩家只是
        点开看了两眼，命令却开始被判成重复/过期」——查不出根因的那种。
        """
        self.build_network()
        ref = self.device_ref()
        before = (
            self.server.topology.version,
            self.server.admission.version,
            self.server.sync.stats["commands"],
        )

        for _ in range(5):
            status = self.server._sync_status_of(ref)
            self.assertIsNotNone(status)

        after = (
            self.server.topology.version,
            self.server.admission.version,
            self.server.sync.stats["commands"],
        )
        self.assertEqual(before, after)

    def test_only_the_leaving_player_loses_his_sessions(self):
        self.build_network()
        c2s, _ = self.server.sync_port.classes()
        self.api.dispatch(c2s, self.open_message(self.device_ref(), player_id="p1"))
        self.api.dispatch(c2s, self.open_message(self.device_ref(), player_id="p2"))

        self.api.dispatch(self.events.DelServerPlayerEvent, SimplePayload(id="p1"))

        self.assertEqual(self.server.sync.registry.sessions_for_player("p1"), [])
        self.assertEqual(len(self.server.sync.registry.sessions_for_player("p2")), 1)

    def test_the_default_policy_refuses_a_session_without_a_position(self):
        """默认口径下「查不到玩家坐标」是**拒绝**（fail closed）。

        **只摘掉坐标解析，维度照给**：两条都摘的话先撞上的是维度那条判据，
        用例会为一个与它名字无关的理由变绿。给上维度之后，这里拒绝的唯一
        原因就只能是位置。
        """
        server = self.build(player_position=None)  # 退回引擎实现 → 离线取不到
        server.push(NormalizedEvent.place(key(0), self.BB8))
        server.push(NormalizedEvent.place(key(1), self.BB8))
        server.push(NormalizedEvent.place(key(1, 65), self.TINY))
        server.push(NormalizedEvent.place(key(0, 65), DRIVER))
        server.tick()

        c2s, _ = server.sync_port.classes()
        self.api.dispatch(
            c2s, self.open_message(server.registry.get(key(0, 65)).ref)
        )

        self.assertEqual(server.sync.stats["sessions_opened"], 0)
        self.assertEqual(server.sync.stats["sessions_refused"], 1)
        self.assertEqual(server.sync.registry.count(), 0)

    # -- 上下文与代次 --------------------------------------------------

    def test_the_context_hands_back_a_device_ref(self):
        """`record_ref` 是 `DeviceRef`，**不是** `DeviceRecord`。

        传错的症状不是报错：`DeviceRef.matches` 对非 `DeviceRef` 一律返回
        False，于是**每一次**开会话都被判成「设备已被替换」。这条钉的就是
        那个「终端一台都打不开」的成因。
        """
        self.build_network()
        record = self.server.registry.get(key(0, 65))
        context = self.server._sync_context_of("p1", record.ref)

        self.assertIsNotNone(context, "设备在网络里，上下文必须解析得出来")
        self.assertTrue(
            context.record_ref.matches(record.ref),
            "会话存的引用与注册表当前的引用必须是同一台",
        )

    def test_the_context_carries_the_online_network_id_and_epoch(self):
        """网络标识是**线上形态**（文本），而代次非空。

        两者都要进会话表与每条 S2C；不一致的表现是「每一条包都因资源序号
        对不上而解码失败」，离原因很远。
        """
        self.build_network()
        runtime = self.server.runtimes.runtimes()[0]
        context = self.server._sync_context_of("p1", self.device_ref())

        self.assertEqual(context.network_id, TerminalSyncHub.network_id_of(runtime.component_id))
        self.assertEqual(context.epoch, self.server._epoch_of(runtime.component_id))
        self.assertEqual(context.master_instance_id, runtime.master.instance_id)
        self.assertTrue(context.network_workable)

    def test_the_context_is_missing_for_an_unknown_device(self):
        """设备不在任何网络里 → 没有上下文 → 开会话被拒（fail closed）。"""
        self.build_network()
        self.assertIsNone(
            self.server._sync_context_of("p1", DeviceRef(key(30, 64, 30), 1))
        )
        self.assertIsNone(self.server._sync_context_of("p1", None))

    def test_the_epoch_is_stable_until_the_network_retires(self):
        """代次在实例存活期间恒定：它进会话表，也进每条 S2C 的资源描述表。"""
        self.build_network()
        component_id = self.server.runtimes.runtimes()[0].component_id

        first = self.server._epoch_of(component_id)
        self.assertEqual(self.server._epoch_of(component_id), first)

    def test_a_reused_component_id_gets_a_fresh_epoch(self):
        """编号回收后重建的实例拿到的必须是**新**代次。

        沿用旧代次是最坏的一种：旧会话在新的网络实例上被当成有效。
        """
        self.build_network()
        component_id = self.server.runtimes.runtimes()[0].component_id
        first = self.server._epoch_of(component_id)

        self.server._on_runtime_change(
            (RuntimeChange(component_id, RuntimeChange.RETIRED),)
        )

        self.assertNotEqual(self.server._epoch_of(component_id), first)

    def test_a_failing_sync_callback_is_recorded_and_the_loop_goes_on(self):
        """回调抛异常时记账并继续，而不是打挂本帧后续的全部工作。

        `RuntimeChange` 的广播挂在拓扑发布路径上（`topology.advance()` →
        `on_publication` → `_emit`），抛出去的代价远大于少关一个会话。

        **同时钉住「代次清理在 `try` 外面」**：回调失败也不该让一个已经
        退役的分量编号继续占着旧代次——那正是「旧会话在新的网络实例上被
        当成有效」的成因。
        """
        self.build_network()
        component_id = self.server.runtimes.runtimes()[0].component_id
        first = self.server._epoch_of(component_id)

        def boom(change):
            raise RuntimeError("模拟的同步回调失败")

        self.server.sync.on_runtime_change = boom
        self.server._on_runtime_change(
            (RuntimeChange(component_id, RuntimeChange.RETIRED),)
        )

        errors = self.server.diagnostics()["tick_errors"]
        self.assertEqual(errors["count"], 1)
        self.assertIn("模拟的同步回调失败", errors["samples"][0])
        self.assertNotEqual(
            self.server._epoch_of(component_id), first, "退役时代次必须作废"
        )

    # -- 帧与停机 ------------------------------------------------------

    def test_tick_reports_how_many_packets_the_sync_layer_sent(self):
        report = self.drive()
        self.assertIn("sync_published", report)

    def test_stop_shuts_the_sync_hub_down(self):
        """停机必须真的清空会话表，而不是只翻一个标志位。"""
        self.build_network()
        c2s, _ = self.server.sync_port.classes()
        self.api.dispatch(c2s, self.open_message(self.device_ref()))
        self.assertEqual(self.server.sync.registry.count(), 1)

        self.server.stop()

        self.assertEqual(self.server.sync.registry.count(), 0)
        self.assertEqual(self.server.sync.diagnostics()["networks"], {})

    def test_diagnostics_report_the_sync_section(self):
        """`diagnostics()["sync"]` 要能在**没有一面界面**的情况下回答
        「同步层现在是什么形状」。

        这一节是实机上唯一能自己读到的同步状态（引擎不给日志），因此逐项
        钉住它至少带着什么：中枢自己的计数、端口的收发计数、未定规则清单，
        以及一句说清「现在没有任何客户端在订阅」的说明。**不含任何库存
        内容**——诊断里出现物品就是另一份真相。
        """
        report = self.server.diagnostics()["sync"]
        for field in (
            "frames",
            "packets",
            "sessions_opened",
            "sessions_refused",
            "bad_packets",
        ):
            self.assertIn(field, report)
        self.assertEqual(report["frames"], 0)
        self.assertEqual(report["networks"], {}, "还没有任何界面接上它")
        self.assertIn("stats", report["port"])
        self.assertTrue(report["unresolved"], "未定规则必须报得出来")
        self.assertIn("11-14", report["scope_note"])

    def test_diagnostics_do_not_leak_inventory_into_the_sync_section(self):
        """诊断里**不能有物品**：它给实机排障看，出现库存内容就意味着这里长了
        第二份真相（与 05 的 `debug_query` 同一条纪律）。

        钉的是**每个网络那一项的字段集合**：会话数、版本、历史长度这类计数
        可以有，`stored`/`reserved`/资源键这类内容不行。会话的资源描述表挂在
        `Session` 上，一旦有人图省事把整个会话塞进诊断，这条会立刻红。
        """
        self.build_network()
        c2s, _ = self.server.sync_port.classes()
        self.api.dispatch(c2s, self.open_message(self.device_ref()))

        networks = self.server.sync_diagnostics()["networks"]
        self.assertTrue(networks, "开了一个会话，网络那一节不该是空的")
        allowed = (
            "sessions",
            "publish_version",
            "history",
            "history_bytes",
            "snapshots",
            "batches",
        )
        for entry in networks.values():
            self.assertEqual(sorted(entry), sorted(allowed))

    # -- 未定规则 ------------------------------------------------------

    def test_the_sync_policies_are_in_the_assembly_check(self):
        """11 的两份策略都要在 `policy_bundle()` 里，否则正式验收时整块漏网。

        **注册一份没实现 `unresolved_report()` 的策略是静默空操作**
        （`_PolicyBundle` 会跳过它），所以这里既查字段、也查它真的报得出来。
        """
        report = self.server.policy_bundle().unresolved_report()
        fields = [item["field"] for item in report]
        for expected in (
            "session.max_interaction_distance",
            "session.session_ttl_frames",
            "session.max_sessions_per_player",
            "session.max_sessions_per_network",
            "sync.publish_budget_keys",
            "sync.snapshot_chunk_items",
            "sync.snapshot_chunk_bytes",
            "sync.max_backlog_items",
            "sync.history_batches",
            "sync.max_failures",
            "sync.backoff_frames",
            "sync.require_admission",
        ):
            self.assertIn(expected, fields)

        rules = dict((item["field"], item["rule"]) for item in report)
        self.assertEqual(rules["sync.require_admission"], "R13")
        self.assertEqual(rules["session.session_ttl_frames"], "11-14")
        self.assertEqual(rules["sync.publish_budget_keys"], "11-14")

    def test_the_sync_policy_is_the_one_that_runs(self):
        """注入的那一份必须就是诊断与生效共用的那一个对象。

        两份并存时 `unresolved_report()` 报的数值与真正生效的对不上——
        正是全局约束 7 要防的「假装检查过」。会话那份同理：`SessionRegistry`
        必须是拿**注入的那一个**建出来的，自己新建一份的话，注入的距离上限
        与 TTL 全都不会生效，而诊断报的还是注入值。
        """
        policy = SyncPolicy(publish_budget_keys=7)
        sessions = SessionPolicy(max_interaction_distance=3.5)
        server = ServerBootstrap(
            world=FakeWorld(), sync_policy=policy, session_policy=sessions
        )
        self.assertIs(server.sync.policy, policy)
        self.assertIs(server.sync_policy, policy)
        self.assertIs(server.session_policy, sessions)
        self.assertIs(server.sync.registry.policy, sessions)

    def test_the_session_policy_is_the_one_that_runs(self):
        """注入的距离上限真的进了校验，而不只是进了诊断。

        对着 policy 字段断言而不是硬编码一个距离：改策略时这条跟着变，
        写死数值的话它会在策略改掉之后继续为旧口径背书。
        """
        sessions = SessionPolicy(max_interaction_distance=1.0)
        server = self.build(session_policy=sessions)
        server.push(NormalizedEvent.place(key(0), self.BB8))
        server.push(NormalizedEvent.place(key(1), self.BB8))
        server.push(NormalizedEvent.place(key(1, 65), self.TINY))
        server.push(NormalizedEvent.place(key(0, 65), DRIVER))
        server.tick()

        # 站在设备正上方一格：距离 1，刚好卡在注入的上限上（含端点）。
        self.positions["p1"] = (self.DEVICE.x, self.DEVICE.y + 1, self.DEVICE.z)
        c2s, _ = server.sync_port.classes()
        self.api.dispatch(c2s, self.open_message(server.registry.get(self.DEVICE).ref))
        self.assertEqual(server.sync.stats["sessions_opened"], 1, "边界含端点")

        server.sync.registry.close_all("test")
        self.positions["p1"] = (self.DEVICE.x, self.DEVICE.y + 2, self.DEVICE.z)
        self.api.dispatch(c2s, self.open_message(server.registry.get(self.DEVICE).ref))
        self.assertEqual(server.sync.stats["sessions_refused"], 1, "超出上限必须拒")


class TestTemplateTerminalWiring(unittest.TestCase):
    """11-05 的装配接线：型号闸、状态投影、堆叠上限与拆方块掉落。"""

    DEVICE = "endernet:template_terminal_panel"
    CRAFTING = "endernet:crafting_terminal_panel"

    def setUp(self):
        self.world = FakeWorld()
        self.world.dropped = []
        self.server = ServerBootstrap(world=self.world)
        self.server.start(bind_engine=False)

    def place(self, x=0, device=None):
        self.server.push(NormalizedEvent.place(
            key(x), device or self.DEVICE))
        self.server.tick()
        return self.server.registry.get(key(x)).ref

    def test_only_the_template_terminal_passes_the_gate(self):
        ref = self.place()
        self.assertTrue(self.server._is_template_terminal(ref))
        self.assertFalse(self.server._is_template_terminal(None))
        self.assertFalse(self.server._is_template_terminal(
            self.place(x=5, device=self.CRAFTING)))

    def test_an_unregistered_position_never_admits(self):
        self.assertFalse(self.server._is_template_terminal(
            DeviceRef(key(9), 1)))

    def test_the_stack_limit_comes_from_the_definition_table(self):
        self.assertEqual(
            64, self.server._template_stack_limit(
                ResourceKey.item("endernet:factorical_template")))
        self.assertEqual(
            1, self.server._template_stack_limit(
                ResourceKey.item("minecraft:stone")))
        self.assertEqual(1, self.server._template_stack_limit(None))

    def test_a_non_terminal_gets_no_state_at_all(self):
        ref = self.place(x=5, device=self.CRAFTING)
        self.assertIsNone(self.server._sync_template_of(ref))

    def test_the_state_carries_the_four_groups_and_the_limits(self):
        ref = self.place()
        state = self.server._sync_template_of(ref)
        self.assertEqual(9, len(state["source"]))
        self.assertEqual(3, len(state["outputs"]))
        self.assertEqual(1, len(state["input"]))
        self.assertEqual(1, len(state["result"]))
        self.assertEqual("crafting", state["mode"])
        self.assertEqual(65535, state["limits"]["max_count"])
        self.assertEqual(1, state["limits"]["default_count"])
        self.assertEqual([False] * 9,
                         [row["locked"] for row in state["source"]])

    def test_the_crafting_outputs_are_locked_and_empty_without_a_recipe(self):
        """合成模式下三个输出格**都锁定**；没有检测到配方时**三格都是空行**。

        **中间那格不再发 `minecraft:barrier` 占位**（User 2026-10-02 改）：
        「没有产物」由 `id_aux` 回 0 表达，与下半屏背包槽同一套做法。空行的
        四条字段一起空，这是那一行形状本来就有的写法（非空位必须给名字）。
        """
        ref = self.place()
        state = self.server._sync_template_of(ref)
        rows = state["outputs"]
        self.assertEqual([True, True, True], [row["locked"] for row in rows])
        for index in range(3):
            self.assertEqual("", rows[index]["name"], index)
            self.assertEqual("", rows[index]["canonical"], index)
            self.assertEqual(0, rows[index]["count"], index)
            self.assertEqual(0, rows[index]["aux"], index)

    def test_the_crafting_middle_cell_carries_the_detected_product(self):
        """检测到配方时中间那格是产物——空行只表示「没有」这一种情况。

        不走 `_sync_template_of`：那要一份能匹配的九宫格，而这里没有引擎、
        配方来源一被问到就会打到真机上。产物这一侧是纯函数，直接喂预览。
        """
        ref = self.place()
        config = self.server.templates.state_for(ref).config
        self.assertEqual("crafting", config.mode)
        preview = {"name": "minecraft:wooden_button", "aux": 0, "count": 1}
        rows = self.server._template_output_rows(config, preview)
        self.assertEqual("", rows[0]["name"])
        self.assertEqual("minecraft:wooden_button", rows[1]["name"])
        self.assertEqual(1, rows[1]["count"])
        self.assertTrue(rows[1]["locked"])
        self.assertEqual("", rows[2]["name"])

    def test_a_row_with_no_key_has_all_four_empty_fields(self):
        ref = self.place()
        row = self.server._sync_template_of(ref)["input"][0]
        self.assertEqual("", row["canonical"])
        self.assertEqual("", row["name"])
        self.assertEqual(0, row["count"])
        self.assertEqual(0, row["aux"])

    def test_breaking_the_block_drops_the_real_slots(self):
        ref = self.place()
        state = self.server.templates.state_for(ref)
        state.slots.set_slot(0, ResourceKey.item(
            "endernet:factorical_template"), 3)
        state.slots.set_slot(1, ResourceKey.item(
            "endernet:factorical_template"), 2)

        self.server.push(NormalizedEvent.remove(key(0), self.DEVICE))
        self.server.tick()

        self.assertEqual(1, len(self.world.dropped))
        node_key, items = self.world.dropped[0]
        self.assertEqual((0, 0, 64, 0), node_key)
        self.assertEqual([2, 3], sorted(count for _key, count in items))
        self.assertIsNone(self.server.templates.get(ref), "拆了就不在活动索引里")
        self.assertEqual(1, len(self.server.templates.orphans()))

    def test_breaking_a_non_template_block_drops_nothing(self):
        self.place(x=5, device=self.CRAFTING)
        self.server.push(NormalizedEvent.remove(key(5), self.CRAFTING))
        self.server.tick()
        self.assertEqual([], self.world.dropped)

    def test_the_assembly_is_wired_end_to_end(self):
        """中枢拿到的 `template_of` 确实指向装配层那一份状态。"""
        ref = self.place()
        state = self.server.sync._template_of(ref)
        self.assertIsNotNone(state, "中枢的 template_of 必须指向装配层")
        self.assertEqual(state["revision"],
                         self.server.templates.get(ref).revision)
        self.assertIs(self.server.template_policy,
                      self.server.template_policy)


class OrderProjectionTests(unittest.TestCase):
    """11-06 的订单投影：把 09 的 `Order` / `OrderStep` 摊成线上那两张表。

    这是**唯一一处**把领域对象翻成界面数据的代码，而它翻错的每一种方式在
    界面上都只表现为「某个数字不对」：配方行按执行次数而不是物品数、阶段
    三态与投料三态串了位置、内存报成格数而不是字节。

    夹具用**真的 `Order` / `OrderStep`**（它们是纯数据、不碰引擎），不是
    再造一份形状——造形状的代价是「领域层加一个字段，替身没跟着加」，
    而那种错测试永远照不出来。
    """

    def order_of(self, steps, tasks_total=8, amount=64, status="running",
                 memory_bytes=1024):
        key = ResourceKey.item("minecraft:stone")
        order = Order("order@net-1/master-1/1", 1, "master-1", key,
                      key.canonical(), amount)
        order.steps = steps
        order.tasks_total = tasks_total
        order.status = status
        order.memory_bytes = memory_bytes
        return order

    def step(self, canonical="minecraft:stone", batches=4, completed=1,
             per_batch=2):
        key = ResourceKey.item(canonical)
        step = OrderStep(0, canonical, {}, batches)
        step.keys = {canonical: key}
        step.output_specs = ((canonical, per_batch),)
        step.completed = completed
        return step

    def test_the_order_row_carries_what_the_screen_draws(self):
        row = _order_row(self.order_of([self.step()]))
        self.assertEqual("order@net-1/master-1/1", row["order_id"])
        self.assertEqual("minecraft:stone", row["name"])
        self.assertEqual(64, row["amount"])
        self.assertEqual("running", row["status"])
        self.assertEqual(1, row["tasks_completed"])
        self.assertEqual(8, row["tasks_total"])
        self.assertEqual(1024, row["memory_bytes"])

    def test_the_order_row_counts_completed_steps_not_batches(self):
        """分子是**各步已完成的配方任务数之和**（`Order.progress()` 的口径）。"""
        order = self.order_of([self.step(completed=2), self.step(
            canonical="minecraft:stick", completed=3)])
        self.assertEqual(5, _order_row(order)["tasks_completed"])

    def test_the_source_row_counts_items_not_batches(self):
        """交接第 4 节：配方条目用**物品数**，不混用执行次数。

        `4 批 × 每批 2 件`，已完成 1 批 → `2/8`；写成批次数就是 `1/4`，
        而界面上那句话会少一个数量级。
        """
        rows = _order_source_rows(self.order_of([self.step(
            batches=4, completed=1, per_batch=2)]))
        self.assertEqual(2, rows[0]["done"])
        self.assertEqual(8, rows[0]["total"])

    def test_a_step_with_no_declared_output_shows_no_item_progress(self):
        """找不到产出声明时退成 **0**，不是 1：退成 1 会把一个猜出来的数量
        写进界面（见 `_order_step_stack_of`）。"""
        step = self.step()
        step.output_specs = ()
        rows = _order_source_rows(self.order_of([step]))
        self.assertEqual(0, rows[0]["done"])
        self.assertEqual(0, rows[0]["total"])

    def test_the_three_stages(self):
        waiting = self.step(batches=4, completed=0)
        running = self.step(batches=4, completed=1)
        running.created = 1
        done = self.step(batches=4, completed=4)
        rows = _order_source_rows(self.order_of([waiting, running, done]))
        self.assertEqual(ORDER_SOURCE_STAGE_WAITING, rows[0]["stage"])
        self.assertEqual(ORDER_SOURCE_STAGE_RUNNING, rows[1]["stage"])
        self.assertEqual(ORDER_SOURCE_STAGE_DONE, rows[2]["stage"])

    def test_the_feed_side_is_a_separate_axis_from_the_stage(self):
        """阶段说这一步整体走到哪，投料说它此刻卡在哪一头——两者不是一条轴
        （制作中且正在等产物的那一条，`stage` 与 `feed` 会分别落在两档）。"""
        staging = self.step(batches=4, completed=1)
        staging.created = 1
        rows = _order_source_rows(self.order_of([staging]))
        self.assertEqual(ORDER_SOURCE_STAGE_RUNNING, rows[0]["stage"])
        self.assertEqual(ORDER_SOURCE_FEED_WAITING_INPUTS, rows[0]["feed"])

    def test_every_step_is_published_including_the_finished_ones(self):
        """交接第 3 节：**全配方树都要显示**，不能只显示直接原料。"""
        rows = _order_source_rows(self.order_of([
            self.step(), self.step(canonical="minecraft:stick",
                                   batches=2, completed=2)]))
        self.assertEqual(2, len(rows))
        self.assertEqual(["minecraft:stone", "minecraft:stick"],
                         [row["canonical"] for row in rows])

    def test_every_source_row_names_its_order(self):
        rows = _order_source_rows(self.order_of([self.step()]))
        self.assertEqual("order@net-1/master-1/1", rows[0]["order_id"])

    def test_memory_is_bytes_not_slot_count(self):
        """任务文档定的是**字节（B）**；`slots` 是内存条格数，与字节不是一回事。"""

        class Ledger(object):
            def describe(self):
                return {"capacity": 4096, "used": 1024, "available": 3072}

        class Factory(object):
            memory = Ledger()
            memory_slots = 4

        view = _order_memory(Factory())
        self.assertEqual(3072, view["available"])
        self.assertEqual(4096, view["capacity"])
        self.assertEqual(4, view["slots"])

    def test_memory_without_a_ledger_is_all_zero_not_a_crash(self):
        class Factory(object):
            memory = None
            memory_slots = 0

        self.assertEqual({"capacity": 0, "used": 0, "available": 0,
                          "slots": 0}, _order_memory(Factory()))


class OrderSyncTests(unittest.TestCase):
    """`_sync_order_of` 的取值路径：**按网络取**，取不到返回 `None`。

    `None` 与「一单都没有」是两回事——前者是「这条网络现在读不到订单」，
    hub 据此一条都不发（见 `_publish_order_state`）。
    """

    class Services(object):
        def __init__(self, factory):
            self.factory = factory

        def get(self, service_id):
            return self.factory

    def fake(self, runtime):
        class Fake(object):
            _sync_runtime = staticmethod(lambda network_id: runtime)
            _order_row = staticmethod(_order_row)
            _order_source_rows = staticmethod(_order_source_rows)

        fake = Fake()
        from endernet.server.bootstrap import ServerBootstrap
        fake._sync_order_of = lambda nid: ServerBootstrap._sync_order_of(
            fake, nid)
        return fake

    def test_no_runtime_means_none(self):
        self.assertIsNone(self.fake(None)._sync_order_of("net-1"))

    def test_no_factory_service_means_none(self):
        class Runtime(object):
            services = OrderSyncTests.Services(None)

        self.assertIsNone(self.fake(Runtime())._sync_order_of("net-1"))

    def test_a_factory_that_has_not_attached_means_none(self):
        class Factory(object):
            orders = None
            memory = None

        class Runtime(object):
            services = OrderSyncTests.Services(Factory())

        self.assertIsNone(self.fake(Runtime())._sync_order_of("net-1"))

    def test_an_attached_factory_yields_both_tables(self):
        projection = OrderProjectionTests()

        class Store(object):
            def live(self):
                return (projection.order_of([projection.step()]),)

        class Factory(object):
            orders = Store()
            memory = None
            memory_slots = 0

        class Runtime(object):
            services = OrderSyncTests.Services(Factory())

        view = self.fake(Runtime())._sync_order_of("net-1")
        self.assertEqual(1, len(view["orders"]))
        self.assertEqual(1, len(view["sources"]))
        self.assertEqual(0, view["memory"]["capacity"])


if __name__ == "__main__":
    unittest.main()
