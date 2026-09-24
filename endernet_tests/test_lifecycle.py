# coding=utf-8
"""01-05 / 01-06 验收：事件归一化与设备生命周期。"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import DefinitionSet
from endernet.common.identity import ChunkKey, NodeKey
from endernet.server.devices.catalog import DeviceCatalog
from endernet.server.devices.registry import DeviceRegistry
from endernet.server.runtime.events import EventKind, EventQueue, NormalizedEvent
from endernet.server.runtime.lifecycle import ChangeKind, DeviceLifecycle

BB8 = "endernet:network_baseband_8"
BB16 = "endernet:network_baseband_16"
DRIVER = "endernet:storage_driver"
TERMINAL = "endernet:common_terminal_panel"


def key(x, y=64, z=0, dimension=0):
    return NodeKey(dimension, x, y, z)


class FakeWorld(object):
    """最小 world 适配器替身：只提供补查需要的两个方法与朝向查询。"""

    def __init__(self, chunks=None, block_entities=None, facings=None, facing_raises=False):
        self._chunks = list(chunks or [])
        self._block_entities = dict(block_entities or {})
        #: `NodeKey.as_key() -> facing`，未列出的返回 None
        self._facings = dict(facings or {})
        self.facing_queries = []
        self._facing_raises = facing_raises

    def list_loaded_chunks(self):
        return list(self._chunks)

    def block_entities_in_chunk(self, chunk_key):
        return list(self._block_entities.get(chunk_key.as_key(), []))

    def facing_at(self, node_key):
        self.facing_queries.append(node_key.as_key())
        if self._facing_raises:
            raise IOError("朝向读取失败")
        return self._facings.get(node_key.as_key())


class LifecycleFixture(unittest.TestCase):
    def setUp(self):
        self.defs = DefinitionSet()
        self.registry = DeviceRegistry(self.defs)
        self.catalog = DeviceCatalog(self.defs)
        self.lifecycle = DeviceLifecycle(self.defs, self.registry, self.catalog)


class TestEventQueue(unittest.TestCase):
    """队列合并与有界推进。"""

    def test_push_and_drain(self):
        queue = EventQueue()
        queue.push(NormalizedEvent.place(key(0), BB8))
        self.assertEqual(len(queue), 1)
        batch = queue.drain()
        self.assertEqual(len(batch), 1)
        self.assertEqual(len(queue), 0)

    def test_same_position_same_kind_coalesces(self):
        """一次放置的多条引擎事件合并成一条工作。"""
        queue = EventQueue()
        queue.push(NormalizedEvent.place(key(0), BB8, source="block_entity"))
        queue.push(NormalizedEvent.place(key(0), BB8, source="after_place"))
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue.stats()["coalesced"], 1)

    def test_coalesced_keeps_last_payload(self):
        queue = EventQueue()
        queue.push(NormalizedEvent.place(key(0), BB8, source="first"))
        queue.push(NormalizedEvent.place(key(0), BB8, source="second"))
        batch = queue.drain()
        self.assertEqual(batch[0].data["source"], "second")

    def test_different_positions_do_not_coalesce(self):
        queue = EventQueue()
        queue.push(NormalizedEvent.place(key(0), BB8))
        queue.push(NormalizedEvent.place(key(1), BB8))
        self.assertEqual(len(queue), 2)

    def test_different_kinds_do_not_coalesce(self):
        queue = EventQueue()
        queue.push(NormalizedEvent.place(key(0), BB8))
        queue.push(NormalizedEvent.remove(key(0), BB8))
        self.assertEqual(len(queue), 2)

    def test_order_is_preserved(self):
        queue = EventQueue()
        queue.push(NormalizedEvent.place(key(2), BB8))
        queue.push(NormalizedEvent.place(key(0), BB8))
        queue.push(NormalizedEvent.place(key(1), BB8))
        batch = queue.drain()
        self.assertEqual([e.data["node_key"] for e in batch], [key(2), key(0), key(1)])

    def test_drain_respects_budget(self):
        queue = EventQueue()
        for index in range(10):
            queue.push(NormalizedEvent.place(key(index), BB8))
        self.assertEqual(len(queue.drain(limit=4)), 4)
        self.assertEqual(len(queue), 6)

    def test_drain_leaves_index_consistent(self):
        """部分取出后，剩余事件仍能正确合并。"""
        queue = EventQueue()
        for index in range(3):
            queue.push(NormalizedEvent.place(key(index), BB8))
        queue.drain(limit=1)
        queue.push(NormalizedEvent.place(key(2), BB8, source="updated"))
        batch = queue.drain()
        self.assertEqual(len(batch), 2)
        self.assertEqual(batch[1].data["source"], "updated")

    def test_overflow_is_counted_not_silent(self):
        queue = EventQueue(max_pending=2)
        for index in range(5):
            queue.push(NormalizedEvent.place(key(index), BB8))
        self.assertEqual(queue.stats()["dropped"], 3)

    def test_tick_events_are_not_coalesced(self):
        """tick 是时钟信号，不携带位置，不能合并。"""
        queue = EventQueue()
        for _ in range(3):
            queue.push(NormalizedEvent(EventKind.SCRIPT_TICK, tick=1))
        self.assertEqual(len(queue), 3)

    def test_clear(self):
        queue = EventQueue()
        queue.push(NormalizedEvent.place(key(0), BB8))
        self.assertEqual(queue.clear(), 1)
        self.assertEqual(len(queue), 0)


class TestEventOrderingPreserved(unittest.TestCase):
    """合并不得改变放置/拆除的真实顺序。

    `放置 A -> 拆除 A -> 放置 A` 是一个方块被拆掉又放回，最终**必须存在**。
    早先的合并按「类型+位置」全局索引，把两次放置压成拆除前的一次，
    于是最终注册表里设备为 None——方块明明在地上，系统却认为它不存在。
    """

    def _drain_kinds(self, queue):
        return [e.kind for e in queue.drain()]

    def test_place_remove_place_keeps_all_three(self):
        queue = EventQueue()
        queue.push(NormalizedEvent.place(key(0), BB8))
        queue.push(NormalizedEvent.remove(key(0), BB8))
        queue.push(NormalizedEvent.place(key(0), BB8))
        self.assertEqual(
            self._drain_kinds(queue),
            [EventKind.PLACE, EventKind.REMOVE, EventKind.PLACE],
        )

    def test_place_remove_place_ends_registered(self):
        """端到端：顺序错了最终设备会消失。"""
        defs = DefinitionSet()
        registry = DeviceRegistry(defs)
        lifecycle = DeviceLifecycle(defs, registry, DeviceCatalog(defs))
        queue = EventQueue()
        queue.push(NormalizedEvent.place(key(0), BB8))
        queue.push(NormalizedEvent.remove(key(0), BB8))
        queue.push(NormalizedEvent.place(key(0), BB8))
        for event in queue.drain():
            lifecycle.handle(event)
        self.assertIsNotNone(
            registry.get(key(0)), "拆掉又放回的方块必须最终存在"
        )

    def test_place_place_place_keeps_all_three(self):
        """同一位置连换三种方块，每一次替换都不能丢。"""
        queue = EventQueue()
        queue.push(NormalizedEvent.place(key(0), BB8))
        queue.push(NormalizedEvent.place(key(0), BB16))
        queue.push(NormalizedEvent.place(key(0), DRIVER))
        self.assertEqual(len(queue.drain()), 3)

    def test_alternating_place_remove_is_never_merged(self):
        queue = EventQueue()
        for _ in range(4):
            queue.push(NormalizedEvent.place(key(0), BB8))
            queue.push(NormalizedEvent.remove(key(0), BB8))
        self.assertEqual(len(queue.drain()), 8)

    def test_adjacent_duplicates_still_coalesce(self):
        """紧邻的同键事件仍要合并——放置一次的两条引擎通知是背靠背来的。"""
        queue = EventQueue()
        queue.push(NormalizedEvent.place(key(0), BB8, source="block_entity"))
        queue.push(NormalizedEvent.place(key(0), BB8, source="entity_place_after"))
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue.drain()[0].data["source"], "entity_place_after")

    def test_interleaved_positions_are_not_merged(self):
        """被别的位置隔开的重复通知不能合并，否则又是在重排顺序。"""
        queue = EventQueue()
        queue.push(NormalizedEvent.place(key(0), BB8, source="first"))
        queue.push(NormalizedEvent.place(key(1), BB8))
        queue.push(NormalizedEvent.place(key(0), BB8, source="second"))
        self.assertEqual(len(queue.drain()), 3)

    def test_ordering_holds_across_drain_boundary(self):
        """跨 drain 边界也要保序：先入队、部分取出、再入队。"""
        queue = EventQueue()
        queue.push(NormalizedEvent.place(key(0), BB8))
        queue.push(NormalizedEvent.remove(key(0), BB8))
        queue.drain(limit=1)
        queue.push(NormalizedEvent.place(key(0), BB8))
        self.assertEqual(
            self._drain_kinds(queue), [EventKind.REMOVE, EventKind.PLACE]
        )

    def test_cross_tick_backlog_preserves_order(self):
        """跨 tick 积压的序列同样保序。"""
        queue = EventQueue(max_pending=64)
        expected = []
        for index in range(5):
            queue.push(NormalizedEvent.place(key(index), BB8))
            expected.append(EventKind.PLACE)
            queue.push(NormalizedEvent.remove(key(index), BB8))
            expected.append(EventKind.REMOVE)
        drained = []
        while len(queue):
            drained.extend(e.kind for e in queue.drain(limit=3))
        self.assertEqual(drained, expected)


class TestPlacement(LifecycleFixture):
    def test_place_registers_device(self):
        changes = self.lifecycle.handle(NormalizedEvent.place(key(0), BB8))
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].kind, ChangeKind.ADDED)
        self.assertIsNotNone(self.registry.get(key(0)))

    def test_duplicate_place_emits_no_change(self):
        """重复事件与补查都只能建立一个实例，且不重复派发工作。"""
        first = self.lifecycle.handle(NormalizedEvent.place(key(0), BB8))
        second = self.lifecycle.handle(NormalizedEvent.place(key(0), BB8))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(self.lifecycle.stats.duplicate_place, 1)

    def test_place_marks_neighbors(self):
        """邻接设备需要被标脏以重建图。"""
        self.lifecycle.handle(NormalizedEvent.place(key(0), BB8))
        changes = self.lifecycle.handle(NormalizedEvent.place(key(1), BB8))
        kinds = [c.kind for c in changes]
        self.assertIn(ChangeKind.ADDED, kinds)
        self.assertIn(ChangeKind.NEIGHBORS_AFFECTED, kinds)

    def test_place_unknown_block_is_ignored(self):
        changes = self.lifecycle.handle(NormalizedEvent.place(key(0), "endernet:nope"))
        self.assertEqual(changes, [])
        self.assertEqual(self.lifecycle.stats.unknown_blocks, 1)

    def test_replace_emits_removal_then_addition(self):
        self.lifecycle.handle(NormalizedEvent.place(key(0), BB8))
        changes = self.lifecycle.handle(NormalizedEvent.place(key(0), BB16))
        self.assertEqual(changes[0].kind, ChangeKind.REMOVED)
        self.assertEqual(changes[1].kind, ChangeKind.ADDED)
        self.assertEqual(self.registry.get(key(0)).device_id, BB16)


class TestRemoval(LifecycleFixture):
    def test_remove_unregisters(self):
        self.lifecycle.handle(NormalizedEvent.place(key(0), BB8))
        changes = self.lifecycle.handle(NormalizedEvent.remove(key(0), BB8))
        self.assertEqual(changes[0].kind, ChangeKind.REMOVED)
        self.assertIsNone(self.registry.get(key(0)))

    def test_late_removal_of_old_block_does_not_hurt_new_one(self):
        """迟到的移除事件不得删掉刚放下的新方块。"""
        self.lifecycle.handle(NormalizedEvent.place(key(0), BB8))
        self.lifecycle.handle(NormalizedEvent.place(key(0), BB16))
        changes = self.lifecycle.handle(NormalizedEvent.remove(key(0), BB8))
        self.assertEqual(changes, [])
        self.assertEqual(self.registry.get(key(0)).device_id, BB16)
        self.assertEqual(self.lifecycle.stats.ignored_removals, 1)

    def test_remove_of_unknown_position_ignored(self):
        changes = self.lifecycle.handle(NormalizedEvent.remove(key(9), BB8))
        self.assertEqual(changes, [])
        self.assertEqual(self.lifecycle.stats.ignored_removals, 1)


class TestTryDestroyIsNotRemoval(LifecycleFixture):
    """「尝试破坏」不是「已拆除」。"""

    def test_try_destroy_keeps_device_registered(self):
        self.lifecycle.handle(NormalizedEvent.place(key(0), DRIVER))
        changes = self.lifecycle.handle(
            NormalizedEvent(EventKind.TRY_DESTROY, data={"node_key": key(0)})
        )
        self.assertIsNotNone(
            self.registry.get(key(0)), "尝试破坏不得注销设备，方块还在"
        )
        self.assertEqual(changes[0].kind, ChangeKind.PLACEMENT_REVIEW)

    def test_try_destroy_on_empty_position_is_ignored(self):
        changes = self.lifecycle.handle(
            NormalizedEvent(EventKind.TRY_DESTROY, data={"node_key": key(5)})
        )
        self.assertEqual(changes, [])

    def test_try_destroy_does_not_emit_removed_change(self):
        self.lifecycle.handle(NormalizedEvent.place(key(0), DRIVER))
        changes = self.lifecycle.handle(
            NormalizedEvent(EventKind.TRY_DESTROY, data={"node_key": key(0)})
        )
        self.assertNotIn(ChangeKind.REMOVED, [c.kind for c in changes])


class TestChunkLoadUnload(LifecycleFixture):
    """卸载不是拆除。"""

    def test_chunk_unload_keeps_records(self):
        self.lifecycle.handle(NormalizedEvent.place(key(0), DRIVER))
        changes = self.lifecycle.handle(
            NormalizedEvent.chunk_unloaded(ChunkKey(0, 0, 0))
        )
        self.assertEqual(changes[0].kind, ChangeKind.UNLOADED)
        record = self.registry.get(key(0))
        self.assertIsNotNone(record, "卸载不得删除记录")
        self.assertFalse(record.loaded)

    def test_chunk_unload_does_not_execute_drops(self):
        """卸载不产生掉落：记录仍在，代次不变。"""
        before = self.lifecycle.handle(NormalizedEvent.place(key(0), DRIVER))[0]
        self.lifecycle.handle(NormalizedEvent.chunk_unloaded(ChunkKey(0, 0, 0)))
        after = self.registry.get(key(0))
        self.assertEqual(after.generation, before.ref.generation)

    def test_chunk_reload_restores_same_instance(self):
        self.lifecycle.handle(NormalizedEvent.place(key(0), DRIVER))
        self.lifecycle.handle(NormalizedEvent.chunk_unloaded(ChunkKey(0, 0, 0)))
        changes = self.lifecycle.handle(
            NormalizedEvent.chunk_loaded(ChunkKey(0, 0, 0))
        )
        self.assertEqual(changes[0].kind, ChangeKind.LOADED)
        self.assertTrue(self.registry.get(key(0)).loaded)

    def test_chunk_load_backfills_block_entities(self):
        """区块加载事件携带的方块实体用于补查。"""
        self.lifecycle.handle(
            NormalizedEvent.chunk_loaded(
                ChunkKey(0, 0, 0),
                block_entities=[{"posX": 5, "posY": 64, "posZ": 6, "blockName": DRIVER}],
            )
        )
        self.assertIsNotNone(self.registry.get(key(5, 64, 6)))

    def test_chunk_load_backfill_ignores_foreign_blocks(self):
        self.lifecycle.handle(
            NormalizedEvent.chunk_loaded(
                ChunkKey(0, 0, 0),
                block_entities=[
                    {"posX": 5, "posY": 64, "posZ": 6, "blockName": "minecraft:chest"}
                ],
            )
        )
        self.assertEqual(self.registry.count(), 0)


class TestNeighborChanged(LifecycleFixture):
    def test_neighbor_change_marks_both_sides(self):
        self.lifecycle.handle(NormalizedEvent.place(key(0), BB8))
        self.lifecycle.handle(NormalizedEvent.place(key(1), BB8))
        changes = self.lifecycle.handle(
            NormalizedEvent.neighbor_changed(key(0), key(1), BB8)
        )
        marked = set(c.node_key for c in changes)
        self.assertEqual(marked, set([key(0), key(1)]))

    def test_neighbor_change_on_empty_positions_emits_nothing(self):
        changes = self.lifecycle.handle(
            NormalizedEvent.neighbor_changed(key(0), key(1), BB8)
        )
        self.assertEqual(changes, [])


class TestFacingResolution(LifecycleFixture):
    """朝向必须在**放置**与**补查**两条路径上都解析到。

    真机上踩到的坑：原先只有放置路径读朝向，重启后由区块加载补查发现的
    方向性设备 `facing=None`。朝向决定端口方向，丢了会让网络层判错连接。
    """

    def _lifecycle_with_facings(self, facings, **kwargs):
        world = FakeWorld(facings=facings, **kwargs)
        return DeviceLifecycle(self.defs, self.registry, self.catalog, world=world), world

    def test_place_resolves_facing_from_world(self):
        lifecycle, world = self._lifecycle_with_facings({key(0).as_key(): "south"})
        lifecycle.handle(NormalizedEvent.place(key(0), "endernet:tiny_network_endpoint"))
        self.assertEqual(self.registry.get(key(0)).facing, "south")

    def test_event_supplied_facing_wins(self):
        lifecycle, world = self._lifecycle_with_facings({key(0).as_key(): "north"})
        lifecycle.handle(
            NormalizedEvent.place(key(0), "endernet:tiny_network_endpoint", facing="east")
        )
        self.assertEqual(self.registry.get(key(0)).facing, "east")
        self.assertEqual(world.facing_queries, [], "事件已带朝向时不应再查世界")

    def test_backfill_resolves_facing_too(self):
        """回归：补查路径必须与放置路径解析出同样的朝向。"""
        chunk = ChunkKey(0, 0, 0)
        node = key(1, 64, 2)
        lifecycle, world = self._lifecycle_with_facings({node.as_key(): "west"})
        lifecycle.handle(
            NormalizedEvent.chunk_loaded(
                chunk,
                block_entities=[
                    {"posX": 1, "posY": 64, "posZ": 2,
                     "blockName": "endernet:tiny_network_endpoint"}
                ],
            )
        )
        self.assertEqual(
            self.registry.get(node).facing, "west",
            "区块加载补查的设备丢失了朝向",
        )

    def test_startup_backfill_resolves_facing(self):
        chunk = ChunkKey(0, 0, 0)
        node = key(5, 64, 6)
        world = FakeWorld(
            chunks=[chunk],
            block_entities={
                chunk.as_key(): [
                    {"posX": 5, "posY": 64, "posZ": 6,
                     "blockName": "endernet:bus_item_io"}
                ]
            },
            facings={node.as_key(): "north"},
        )
        lifecycle = DeviceLifecycle(self.defs, self.registry, self.catalog, world=world)
        list(lifecycle.plan_startup_backfill())
        self.assertEqual(self.registry.get(node).facing, "north")

    def test_facing_read_failure_does_not_block_registration(self):
        """读朝向失败只能退化为无方向，不能把设备整个丢掉。"""
        lifecycle, world = self._lifecycle_with_facings({}, facing_raises=True)
        lifecycle.handle(NormalizedEvent.place(key(0), "endernet:tiny_network_endpoint"))
        record = self.registry.get(key(0))
        self.assertIsNotNone(record, "朝向读取失败不应阻断注册")
        self.assertIsNone(record.facing)

    def test_no_world_adapter_is_tolerated(self):
        """离线场景可以不传 world，此时朝向为 None 而不是报错。"""
        lifecycle = DeviceLifecycle(self.defs, self.registry, self.catalog)
        lifecycle.handle(NormalizedEvent.place(key(0), "endernet:tiny_network_endpoint"))
        self.assertIsNone(self.registry.get(key(0)).facing)


class TestStartupBackfill(LifecycleFixture):
    """启动时已加载区块的分帧补查（01-06）。"""

    def test_explicit_chunk_keys_backfill(self):
        self.lifecycle.plan_startup_backfill(chunk_keys=[ChunkKey(0, 0, 0)])
        # 生成器需要被驱动
        changes = list(
            DeviceLifecycle(
                self.defs,
                self.registry,
                self.catalog,
                world=FakeWorld(),
            ).plan_startup_backfill(chunk_keys=[ChunkKey(0, 0, 0)])
        )
        self.assertEqual(len(changes), 1)

    def test_backfill_registers_devices_from_world(self):
        chunk = ChunkKey(0, 0, 0)
        world = FakeWorld(
            chunks=[chunk],
            block_entities={
                chunk.as_key(): [
                    {"posX": 1, "posY": 64, "posZ": 2, "blockName": DRIVER},
                    {"posX": 3, "posY": 64, "posZ": 4, "blockName": TERMINAL},
                ]
            },
        )
        lifecycle = DeviceLifecycle(self.defs, self.registry, self.catalog, world=world)
        list(lifecycle.plan_startup_backfill())
        self.assertEqual(self.registry.count(), 2)

    def test_backfill_is_resumable_and_skips_done_chunks(self):
        """分帧继续：已补查过的区块不重复扫描。"""
        chunks = [ChunkKey(0, 0, 0), ChunkKey(0, 1, 0), ChunkKey(0, 2, 0)]
        world = FakeWorld(chunks=chunks)
        lifecycle = DeviceLifecycle(self.defs, self.registry, self.catalog, world=world)

        generator = lifecycle.plan_startup_backfill()
        next(generator)  # 第一个区块
        next(generator)  # 第二个区块
        self.assertEqual(lifecycle.stats.backfilled_chunks, 2)

        # 重新规划：前两个应被跳过
        rest = list(lifecycle.plan_startup_backfill())
        self.assertEqual(len(rest), 1)
        self.assertEqual(lifecycle.stats.backfilled_chunks, 3)

    def test_backfill_twice_does_not_duplicate_devices(self):
        chunk = ChunkKey(0, 0, 0)
        world = FakeWorld(
            chunks=[chunk],
            block_entities={
                chunk.as_key(): [{"posX": 1, "posY": 64, "posZ": 2, "blockName": DRIVER}]
            },
        )
        lifecycle = DeviceLifecycle(self.defs, self.registry, self.catalog, world=world)
        list(lifecycle.plan_startup_backfill())
        lifecycle.forget_backfill()
        list(lifecycle.plan_startup_backfill())
        self.assertEqual(self.registry.count(), 1, "重复补查不得新建实例")

    def test_backfill_then_place_event_is_idempotent(self):
        """补查之后再收到放置事件，仍然只有一个实例。"""
        chunk = ChunkKey(0, 0, 0)
        world = FakeWorld(
            chunks=[chunk],
            block_entities={
                chunk.as_key(): [{"posX": 1, "posY": 64, "posZ": 2, "blockName": DRIVER}]
            },
        )
        lifecycle = DeviceLifecycle(self.defs, self.registry, self.catalog, world=world)
        list(lifecycle.plan_startup_backfill())
        changes = lifecycle.handle(NormalizedEvent.place(key(1, 64, 2), DRIVER))
        self.assertEqual(changes, [])
        self.assertEqual(self.registry.count(), 1)

    def test_backfill_requires_world_adapter(self):
        with self.assertRaises(RuntimeError):
            list(self.lifecycle.plan_startup_backfill())


class TestDiagnostics(LifecycleFixture):
    def test_stats_track_everything(self):
        self.lifecycle.handle(NormalizedEvent.place(key(0), BB8))
        self.lifecycle.handle(NormalizedEvent.place(key(0), BB8))
        self.lifecycle.handle(NormalizedEvent.remove(key(0), BB8))
        report = self.lifecycle.diagnostics()
        self.assertEqual(report["placed"], 1)
        self.assertEqual(report["duplicate_place"], 1)
        self.assertEqual(report["removed"], 1)
        self.assertEqual(report["registry"]["devices"], 0)


if __name__ == "__main__":
    unittest.main()
