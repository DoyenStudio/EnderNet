# coding=utf-8
"""01-04 / 02-01 验收：设备注册表、实例代次与连接规则。"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import UNCONFIGURED, DefinitionSet
from endernet.common.identity import ChunkKey, DeviceRef, NodeKey
from endernet.common.protocol import Reason
from endernet.server.devices.capabilities import (
    ALL_CAPABILITIES,
    CapabilityRegistry,
    ITEM_PORT,
)
from endernet.server.devices.catalog import DeviceCatalog
from endernet.server.devices.registry import DeviceRecord, DeviceRegistry

BB8 = "endernet:network_baseband_8"
BB16 = "endernet:network_baseband_16"
TINY = "endernet:tiny_network_endpoint"
DOS = "endernet:dos_network_endpoint"
DRIVER = "endernet:storage_driver"
TERMINAL = "endernet:common_terminal_panel"
BUS = "endernet:bus_item_io"
PROCESSOR = "endernet:parallel_processor"
CONNECTOR = "endernet:parallel_processor_connector"
MEMORY = "endernet:memory_slot"


def key(x, y=64, z=0, dimension=0):
    return NodeKey(dimension, x, y, z)


class RegistryFixture(unittest.TestCase):
    def setUp(self):
        self.defs = DefinitionSet()
        self.registry = DeviceRegistry(self.defs)
        self.catalog = DeviceCatalog(self.defs)


class TestIdempotentRegistration(RegistryFixture):
    """放置一次、重复事件、重复恢复都只注册一次。"""

    def test_first_register_creates(self):
        outcome = self.registry.register(key(0), BB8)
        self.assertTrue(outcome.created)
        self.assertEqual(outcome.record.generation, 0)

    def test_duplicate_register_returns_same_record(self):
        first = self.registry.register(key(0), BB8)
        second = self.registry.register(key(0), BB8)
        self.assertFalse(second.created)
        self.assertIs(second.record, first.record)
        self.assertEqual(self.registry.count(), 1)

    def test_duplicate_register_does_not_bump_generation(self):
        """方块实例没变，旧任务必须仍然有效。"""
        first = self.registry.register(key(0), BB8)
        second = self.registry.register(key(0), BB8)
        self.assertEqual(first.record.generation, second.record.generation)

    def test_duplicate_events_are_counted(self):
        for _ in range(5):
            self.registry.register(key(0), BB8)
        self.assertEqual(self.registry.stats()["duplicate_events"], 4)

    def test_unknown_device_id_rejected(self):
        with self.assertRaises(KeyError):
            self.registry.register(key(0), "endernet:not_a_block")


class TestDimensionIsolation(RegistryFixture):
    """两个维度的相同坐标互不覆盖。"""

    def test_same_coords_different_dimensions(self):
        overworld = self.registry.register(key(0, 64, 0, 0), BB8)
        nether = self.registry.register(key(0, 64, 0, 1), BB16)
        self.assertTrue(overworld.created)
        self.assertTrue(nether.created)
        self.assertEqual(self.registry.count(), 2)
        self.assertEqual(self.registry.get(key(0, 64, 0, 0)).device_id, BB8)
        self.assertEqual(self.registry.get(key(0, 64, 0, 1)).device_id, BB16)

    def test_unregister_one_dimension_leaves_the_other(self):
        self.registry.register(key(0, 64, 0, 0), BB8)
        self.registry.register(key(0, 64, 0, 1), BB8)
        self.registry.unregister(key(0, 64, 0, 0))
        self.assertIsNone(self.registry.get(key(0, 64, 0, 0)))
        self.assertIsNotNone(self.registry.get(key(0, 64, 0, 1)))


class TestGeneration(unittest.TestCase):
    """同位置替换使旧任务失效。"""

    def setUp(self):
        self.registry = DeviceRegistry(DefinitionSet())

    def test_replace_with_different_device_bumps_generation(self):
        old = self.registry.register(key(0), BB8)
        new = self.registry.register(key(0), BB16)
        self.assertTrue(new.created)
        self.assertGreater(new.record.generation, old.record.generation)
        self.assertIs(new.replaced, old.record)

    def test_old_ref_no_longer_matches(self):
        old_ref = self.registry.register(key(0), BB8).record.ref
        self.registry.register(key(0), BB16)
        self.assertIsNone(self.registry.get_by_ref(old_ref))
        self.assertFalse(self.registry.matches(old_ref))

    def test_stale_removal_event_cannot_delete_new_device(self):
        """旧代次的移除事件不得删掉刚放下的新设备。"""
        old_ref = self.registry.register(key(0), BB8).record.ref
        self.registry.register(key(0), BB16)  # 新方块顶替
        removed = self.registry.unregister_if_generation(old_ref)
        self.assertIsNone(removed)
        self.assertIsNotNone(self.registry.get(key(0)))
        self.assertEqual(self.registry.get(key(0)).device_id, BB16)

    def test_matching_removal_event_works(self):
        ref = self.registry.register(key(0), BB8).record.ref
        removed = self.registry.unregister_if_generation(ref)
        self.assertIsNotNone(removed)
        self.assertIsNone(self.registry.get(key(0)))

    def test_remove_then_place_gets_new_generation(self):
        first = self.registry.register(key(0), BB8).record.ref
        self.registry.unregister(key(0))
        second = self.registry.register(key(0), BB8).record.ref
        self.assertGreater(second.generation, first.generation)
        self.assertIsNone(self.registry.get_by_ref(first))

    def test_generations_are_monotonic_across_many_replacements(self):
        generations = []
        for device_id in (BB8, BB16, BB8, TINY, DRIVER):
            generations.append(self.registry.register(key(0), device_id).record.generation)
        self.assertEqual(generations, sorted(generations))
        self.assertEqual(len(set(generations)), len(generations))


class TestLoadUnload(RegistryFixture):
    """卸载不是拆除：保留记录，不执行掉落。"""

    def test_unload_keeps_record(self):
        self.registry.register(key(0), DRIVER)
        self.assertTrue(self.registry.mark_unloaded(key(0)))
        record = self.registry.get(key(0))
        self.assertIsNotNone(record, "卸载不得删除记录")
        self.assertFalse(record.loaded)

    def test_unload_does_not_bump_generation(self):
        record = self.registry.register(key(0), DRIVER).record
        generation = record.generation
        self.registry.mark_unloaded(key(0))
        self.assertEqual(self.registry.get(key(0)).generation, generation)

    def test_reload_same_instance(self):
        self.registry.register(key(0), DRIVER)
        self.registry.mark_unloaded(key(0))
        outcome = self.registry.register(key(0), DRIVER)
        self.assertFalse(outcome.created, "重新加载不得新建实例")
        self.assertTrue(outcome.record.loaded)

    def test_unload_is_idempotent(self):
        self.registry.register(key(0), DRIVER)
        self.assertTrue(self.registry.mark_unloaded(key(0)))
        self.assertFalse(self.registry.mark_unloaded(key(0)))

    def test_chunk_unload_returns_affected_positions(self):
        self.registry.register(key(0), BB8)
        self.registry.register(key(1), DRIVER)
        self.registry.register(key(100), TERMINAL)  # 另一个区块
        affected = self.registry.unload_chunk(ChunkKey(0, 0, 0))
        self.assertEqual(len(affected), 2)
        self.assertTrue(self.registry.is_loaded(key(100)))

    def test_records_in_chunk(self):
        self.registry.register(key(0), BB8)
        self.registry.register(key(1), DRIVER)
        self.registry.register(key(100), TERMINAL)
        records = self.registry.records_in_chunk(ChunkKey(0, 0, 0))
        self.assertEqual(len(records), 2)

    def test_unloaded_records_excluded_from_loaded_list(self):
        self.registry.register(key(0), BB8)
        self.registry.register(key(1), DRIVER)
        self.registry.mark_unloaded(key(0))
        self.assertEqual(len(self.registry.loaded_records()), 1)


class TestControllerQuery(RegistryFixture):
    """控制器查询必须包含未加载的实例。"""

    def test_controller_records_includes_unloaded(self):
        self.registry.register(key(0), TINY)
        self.registry.register(key(1), BB8)
        self.registry.mark_unloaded(key(0))
        controllers = self.registry.controller_records()
        self.assertEqual(len(controllers), 1)
        self.assertEqual(controllers[0].device_id, TINY)
        self.assertFalse(controllers[0].loaded)

    def test_non_controllers_excluded(self):
        self.registry.register(key(0), BB8)
        self.registry.register(key(1), DRIVER)
        self.assertEqual(self.registry.controller_records(), [])


class TestRecordRoundTrip(RegistryFixture):
    def test_record_round_trip(self):
        record = self.registry.register(key(3, 64, 7), DRIVER, facing="north").record
        restored = DeviceRecord.from_dict(record.to_dict())
        self.assertEqual(restored.ref, record.ref)
        self.assertEqual(restored.device_id, record.device_id)
        self.assertEqual(restored.facing, "north")

    def test_ref_round_trip(self):
        ref = DeviceRef(NodeKey(2, 1, 2, 3), 7)
        self.assertEqual(DeviceRef.from_dict(ref.to_dict()), ref)

    def test_node_key_round_trip(self):
        node = NodeKey(1, 4, 5, 6)
        self.assertEqual(NodeKey.from_dict(node.to_dict()), node)


class TestCatalogConnectionRules(RegistryFixture):
    """相邻不等于连通。"""

    def test_baseband_connects_to_controller_and_devices(self):
        for other in (TINY, DRIVER, TERMINAL, BUS, MEMORY, CONNECTOR):
            self.assertTrue(
                self.catalog.can_connect(BB8, other).allowed, "%s 应可连基带" % other
            )

    def test_same_capacity_basebands_connect(self):
        """同容量基带直接连通。"""
        self.assertTrue(self.catalog.can_connect(BB8, BB8).allowed)
        self.assertTrue(self.catalog.can_connect(BB16, BB16).allowed)

    def test_different_baseband_widths_do_not_connect(self):
        """blocks.md：8/16/32 位基带只与同容量基带直接连通。

        异容量即使相邻也不建立连接、不因此合并网络或负载域。
        （2026-09-24 定的规则；此前实现是「相邻即连通、容量逐段相加」。）
        """
        for other in (BB16, "endernet:network_baseband_32"):
            verdict = self.catalog.can_connect(BB8, other)
            self.assertFalse(verdict.allowed, "8 位基带不应与 %s 连通" % (other,))
            self.assertEqual(verdict.reason, Reason.BASEBAND_CAPACITY_MISMATCH)
        # 反向与 16/32 组合同样成立，拒绝理由一致。
        self.assertFalse(
            self.catalog.can_connect(BB16, "endernet:network_baseband_32").allowed
        )

    def test_capacity_mismatch_detail_names_both_capacities(self):
        verdict = self.catalog.can_connect(BB8, BB16)
        self.assertIn("8u", verdict.detail)
        self.assertIn("16u", verdict.detail)

    def test_unconfigured_capacity_does_not_block_the_link(self):
        """容量未知时不拦：未知不等于 0，也不等于「与对方不同」。

        把缺项判成异容量等于用一个没定过的数值定了案；准入层遇见未知容量
        会把整个域报成待确认，业务本来就不会放行。
        """
        self.registry  # 夹具已建好定义表；这里直接查定义层
        definition = self.defs.get(BB8)
        original = definition.bandwidth.capacity_quarters
        try:
            definition.bandwidth.capacity_quarters = UNCONFIGURED
            self.assertTrue(self.catalog.can_connect(BB8, BB16).allowed)
        finally:
            definition.bandwidth.capacity_quarters = original

    def test_processor_cannot_connect_directly_to_baseband(self):
        verdict = self.catalog.can_connect(PROCESSOR, BB8)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "processor_must_use_connector")

    def test_processor_cannot_connect_directly_to_controller(self):
        self.assertFalse(self.catalog.can_connect(PROCESSOR, TINY).allowed)

    def test_connector_connects_processor_and_baseband(self):
        self.assertTrue(self.catalog.can_connect(CONNECTOR, PROCESSOR).allowed)
        self.assertTrue(self.catalog.can_connect(CONNECTOR, BB8).allowed)

    def test_connector_rejects_terminal(self):
        self.assertFalse(self.catalog.can_connect(CONNECTOR, TERMINAL).allowed)

    def test_processor_connects_to_processor(self):
        self.assertTrue(self.catalog.can_connect(PROCESSOR, PROCESSOR).allowed)

    def test_devices_do_not_connect_to_each_other(self):
        self.assertFalse(self.catalog.can_connect(TERMINAL, DRIVER).allowed)
        self.assertFalse(self.catalog.can_connect(BUS, MEMORY).allowed)

    def test_controller_connects_physically_only_to_baseband(self):
        """blocks.md：控制器直接与基带连接，不直接贴着驱动器或终端。"""
        self.assertTrue(self.catalog.can_connect(DOS, BB8).allowed)
        self.assertFalse(self.catalog.can_connect(DOS, DRIVER).allowed)
        self.assertFalse(self.catalog.can_connect(DOS, BUS).allowed)

    def test_dos_controller_service_scope_rejects_bus(self):
        """功能范围与物理连接是两件事：DOS 控制器不认总线。"""
        verdict = self.catalog.controller_serves(DOS, BUS)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "controller_does_not_accept_device")

    def test_dos_controller_service_scope_accepts_documented_targets(self):
        for device_id in (DRIVER, TERMINAL, "endernet:crafting_terminal_panel", BB8):
            self.assertTrue(
                self.catalog.controller_serves(DOS, device_id).allowed, device_id
            )

    def test_tiny_controller_serves_buses(self):
        self.assertTrue(self.catalog.controller_serves(TINY, BUS).allowed)

    def test_service_scope_on_non_controller_is_rejected(self):
        verdict = self.catalog.controller_serves(BB8, BUS)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "not_a_controller")

    def test_unknown_device_reason(self):
        verdict = self.catalog.can_connect(BB8, "endernet:ghost_block")
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "unknown_device")

    def test_verdict_is_falsy_when_denied(self):
        self.assertTrue(self.catalog.can_connect(BB8, DRIVER))
        self.assertFalse(self.catalog.can_connect(PROCESSOR, BB8))


class TestCatalogNeighbors(RegistryFixture):
    """按规则过滤的邻接查询。"""

    def _place(self, positions, device_id):
        for position in positions:
            self.registry.register(position, device_id)

    def test_adjacent_basebands_are_neighbors(self):
        self.registry.register(key(0), BB8)
        self.registry.register(key(1), BB8)
        neighbors = self.catalog.connected_neighbors(self.registry, key(0))
        self.assertEqual([n[0] for n in neighbors], [key(1)])

    def test_processor_adjacent_to_baseband_is_not_a_neighbor(self):
        self.registry.register(key(0), BB8)
        self.registry.register(key(1), PROCESSOR)
        self.assertEqual(self.catalog.connected_neighbors(self.registry, key(0)), [])
        self.assertEqual(self.catalog.connected_neighbors(self.registry, key(1)), [])

    def test_diagonal_blocks_are_not_neighbors(self):
        """只有六个面相邻算连接，斜角不算。"""
        self.registry.register(key(0), BB8)
        self.registry.register(NodeKey(0, 1, 65, 1), BB8)
        self.assertEqual(self.catalog.connected_neighbors(self.registry, key(0)), [])

    def test_neighbor_of_empty_position_is_empty(self):
        self.assertEqual(self.catalog.connected_neighbors(self.registry, key(0)), [])

    def test_unloaded_neighbor_is_still_listed_then_filtered_by_caller(self):
        """拓扑需要知道邻接关系；是否可工作由上层按 loaded 判定。"""
        self.registry.register(key(0), BB8)
        self.registry.register(key(1), BB8)
        self.registry.mark_unloaded(key(1))
        neighbors = self.catalog.connected_neighbors(self.registry, key(0))
        self.assertEqual(len(neighbors), 1)
        self.assertFalse(neighbors[0][1].loaded)

    def test_chain_is_connected_step_by_step(self):
        """一条 4 节链：两端各 1 个邻居，中间各 2 个。"""
        self._place([key(x) for x in range(4)], BB8)
        expected = [1, 2, 2, 1]
        for index, count in enumerate(expected):
            neighbors = self.catalog.connected_neighbors(self.registry, key(index))
            self.assertEqual(len(neighbors), count, "第 %d 节邻居数" % index)

    def test_branch_position_has_three_neighbors(self):
        """基带 + 三个方向的分支。"""
        self.registry.register(key(0), BB8)
        self.registry.register(key(1), BB8)
        self.registry.register(key(-1), BB8)
        self.registry.register(key(0, 65, 0), BB8)
        self.assertEqual(len(self.catalog.connected_neighbors(self.registry, key(0))), 3)


class TestCapabilityRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = CapabilityRegistry()

    def test_register_and_get(self):
        implementation = object()
        self.registry.register(ITEM_PORT, implementation)
        self.assertIs(self.registry.get(ITEM_PORT), implementation)
        self.assertTrue(self.registry.has(ITEM_PORT))

    def test_unknown_capability_name_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.register("teleport", object())

    def test_none_implementation_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.register(ITEM_PORT, None)

    def test_duplicate_registration_rejected(self):
        """两个实现互相覆盖会导致行为莫名改变，必须显式报错。"""
        self.registry.register(ITEM_PORT, object())
        with self.assertRaises(ValueError):
            self.registry.register(ITEM_PORT, object())

    def test_unregister_then_re_register(self):
        self.registry.register(ITEM_PORT, object())
        self.registry.unregister(ITEM_PORT)
        self.registry.register(ITEM_PORT, object())

    def test_clear(self):
        self.registry.register(ITEM_PORT, object())
        self.assertEqual(self.registry.clear(), 1)
        self.assertEqual(len(self.registry), 0)

    def test_names_sorted(self):
        from endernet.server.devices.capabilities import MEMORY, STORAGE_PROVIDER

        self.registry.register(STORAGE_PROVIDER, object())
        self.registry.register(ITEM_PORT, object())
        self.registry.register(MEMORY, object())
        self.assertEqual(
            self.registry.names(), tuple(sorted([ITEM_PORT, MEMORY, STORAGE_PROVIDER]))
        )

    def test_all_capabilities_are_lowercase_identifiers(self):
        for name in ALL_CAPABILITIES:
            self.assertEqual(name, name.lower())
            self.assertTrue(name.replace("_", "").isalpha(), name)


class TestCapabilityNamesMatchDefinitions(unittest.TestCase):
    """能力名在定义表与能力模块之间必须一致，否则挂载会静默失败。"""

    def test_definition_capabilities_are_known(self):
        defs = DefinitionSet()
        for device_id in defs.devices:
            for capability in defs.get(device_id).capabilities:
                self.assertIn(
                    capability,
                    ALL_CAPABILITIES,
                    "%s 声明了未知能力 %s" % (device_id, capability),
                )

    def test_storage_driver_declares_storage_provider(self):
        from endernet.server.devices.capabilities import STORAGE_PROVIDER

        self.assertIn(STORAGE_PROVIDER, DefinitionSet().get(DRIVER).capabilities)


if __name__ == "__main__":
    unittest.main()
