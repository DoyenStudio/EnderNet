# coding=utf-8
"""01-04 / 02-01 验收：设备注册表、实例代次与连接规则。"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import UNCONFIGURED, DefinitionSet
from endernet.common.identity import (
    FACING_NAMES,
    ChunkKey,
    DeviceRef,
    NodeKey,
    PartKey,
    face_mask,
    faces_of_mask,
    node_key_text,
)
from endernet.common.protocol import Reason
from endernet.server.devices.capabilities import (
    ALL_CAPABILITIES,
    CapabilityRegistry,
    ITEM_PORT,
)
from endernet.server.devices.catalog import (
    CABLE_CUTS_KEY,
    CABLE_PARTS_KEY,
    CableFaces,
    DeviceCatalog,
)
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
BB32 = "endernet:network_baseband_32"
GLASS = "endernet:ender_glass_cable"
CABLE16 = "endernet:ender_cable"
DENSE = "endernet:ender_dense_cable"
TERM_PART = "endernet:common_terminal_part"
CRAFT_PART = "endernet:crafting_terminal_part"


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


class TestPartKey(unittest.TestCase):
    """13 §7：`PartKey = NodeKey + 槽`。旧接口拿 `NodeKey` 按 center 槽解释。"""

    def setUp(self):
        self.node = NodeKey(0, 12, 64, -3)

    def test_center_slot_is_the_node_key_itself(self):
        center = PartKey(self.node)
        self.assertTrue(center.is_center())
        self.assertEqual(center, self.node)
        self.assertEqual(self.node, center)
        self.assertFalse(center != self.node)
        self.assertFalse(self.node != center)
        self.assertEqual(hash(center), hash(self.node))
        self.assertEqual(center.as_key(), self.node.as_key())

    def test_old_code_keeps_working_with_node_keys(self):
        """同一个字典或集合里，`NodeKey` 与 center 槽是同一个键。"""
        table = {PartKey(self.node): "center"}
        self.assertEqual(table[self.node], "center")
        self.assertEqual({self.node: 1}[PartKey(self.node)], 1)
        self.assertEqual(len(set([self.node, PartKey(self.node)])), 1)

    def test_face_slots_are_distinct_keys(self):
        north = PartKey(self.node, "north")
        self.assertNotEqual(north, self.node)
        self.assertNotEqual(self.node, north)
        self.assertNotEqual(north, PartKey(self.node, "south"))
        self.assertEqual(north, PartKey(NodeKey(0, 12, 64, -3), "north"))
        table = dict((PartKey(self.node, face), face) for face in FACING_NAMES)
        table[self.node] = "center"
        self.assertEqual(len(table), 7)
        self.assertNotIn(self.node, set(PartKey(self.node, face) for face in FACING_NAMES))

    def test_text_format(self):
        """存档文本：center 与 `node_key_text` 逐字相同；面板槽加「@面名」。"""
        self.assertEqual(PartKey(self.node).to_text(), node_key_text(self.node))
        self.assertEqual(PartKey(self.node).to_text(), "0,12,64,-3")
        self.assertEqual(PartKey(self.node, "north").to_text(), "0,12,64,-3@north")
        for slot in ("center",) + FACING_NAMES:
            key = PartKey(self.node, slot)
            self.assertEqual(PartKey.from_text(key.to_text()), key)
        self.assertEqual(PartKey.from_text("0,12,64,-3"), self.node)

    def test_malformed_text_is_rejected(self):
        for text in ("0,12,64", "0,12,64,-3@center", "0,12,64,-3@sideways", "a,b,c,d", "0,12,64,-3,1"):
            with self.assertRaises((ValueError, TypeError)):
                PartKey.from_text(text)

    def test_dict_round_trip_and_normalisation(self):
        key = PartKey(self.node, "east")
        self.assertEqual(PartKey.from_dict(key.to_dict()), key)
        self.assertEqual(PartKey.from_dict({"node_key": self.node.to_dict()}), self.node)
        self.assertIs(PartKey.of(key), key)
        self.assertEqual(PartKey.of(self.node), PartKey(self.node))

    def test_mixed_keys_sort_deterministically(self):
        keys = [PartKey(self.node, "west"), self.node, PartKey(self.node, "east"), NodeKey(0, 1, 64, 0)]
        ordered = sorted(keys, key=lambda item: item.as_key())
        self.assertEqual(
            ordered,
            [NodeKey(0, 1, 64, 0), self.node, PartKey(self.node, "east"), PartKey(self.node, "west")],
        )

    def test_bad_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            PartKey(self.node, "sideways")
        with self.assertRaises(TypeError):
            PartKey((0, 1, 2, 3), "up")

    def test_face_mask_round_trip(self):
        """`endernet:cuts` 的 6 位掩码：第 i 位对应 `FACING_NAMES[i]`。"""
        self.assertEqual(face_mask([]), 0)
        self.assertEqual(face_mask(["up"]), 1)
        self.assertEqual(face_mask(["east"]), 32)
        self.assertEqual(face_mask(FACING_NAMES), 63)
        for mask in range(64):
            self.assertEqual(face_mask(faces_of_mask(mask)), mask)
        with self.assertRaises(ValueError):
            face_mask(["sideways"])
        for bad in (64, -1):
            with self.assertRaises(ValueError):
                faces_of_mask(bad)
        with self.assertRaises(TypeError):
            faces_of_mask("3")


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

    def test_baseband_connects_to_controller_cables_and_devices(self):
        for other in (TINY, DRIVER, TERMINAL, BUS, MEMORY, CONNECTOR, GLASS, CABLE16, DENSE):
            self.assertTrue(
                self.catalog.can_connect(BB8, other).allowed, "%s 应可连基带" % other
            )

    def test_same_capacity_basebands_connect(self):
        """同容量基带直接连通。"""
        self.assertTrue(self.catalog.can_connect(BB8, BB8).allowed)
        self.assertTrue(self.catalog.can_connect(BB16, BB16).allowed)

    def test_different_baseband_tiers_connect(self):
        """R14-b：2026-11-09 作者改定异档基带允许混搭，相邻即相连。

        改定之前（R02 2026-09-24 定、曾验收）这里断言的是「异容量不连、
        原因 `baseband_capacity_mismatch`」；那条拒绝与它的原因码一起删除。
        """
        for first, second in ((BB8, BB16), (BB8, BB32), (BB16, BB32)):
            for a, b in ((first, second), (second, first)):
                verdict = self.catalog.can_connect(a, b)
                self.assertTrue(verdict.allowed, "%s <-> %s" % (a, b))
                self.assertEqual(verdict.reason, "")
        self.assertFalse(hasattr(Reason, "BASEBAND_CAPACITY_MISMATCH"))

    def test_unconfigured_capacity_does_not_block_the_link(self):
        """容量未知时同样相连：连接只看分类，不看档位。"""
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

    def test_connector_also_reaches_cables_and_devices(self):
        """13 §3：连接器原来只接基带，现扩到光缆与设备。"""
        for other in (GLASS, CABLE16, DENSE, TERMINAL, DRIVER, BUS, MEMORY):
            self.assertTrue(self.catalog.can_connect(CONNECTOR, other).allowed, other)

    def test_connector_does_not_reach_controllers_or_other_connectors(self):
        self.assertFalse(self.catalog.can_connect(CONNECTOR, TINY).allowed)
        self.assertEqual(
            self.catalog.can_connect(CONNECTOR, TINY).reason,
            "controller_only_accepts_baseband",
        )
        self.assertFalse(self.catalog.can_connect(CONNECTOR, CONNECTOR).allowed)

    def test_processor_connects_to_processor(self):
        self.assertTrue(self.catalog.can_connect(PROCESSOR, PROCESSOR).allowed)

    def test_devices_pass_through_each_other(self):
        """13 §0.5：设备之间透传（之前不透传是漏设计）。处理器除外。"""
        self.assertTrue(self.catalog.can_connect(TERMINAL, DRIVER).allowed)
        self.assertTrue(self.catalog.can_connect(BUS, MEMORY).allowed)
        self.assertTrue(self.catalog.can_connect(TERMINAL, TERMINAL).allowed)
        for other in (TERMINAL, DRIVER, BUS, MEMORY, GLASS):
            verdict = self.catalog.can_connect(PROCESSOR, other)
            self.assertFalse(verdict.allowed, other)
            self.assertEqual(verdict.reason, "processor_must_use_connector")

    def test_cables_interconnect_and_reach_devices(self):
        """13 §0.9：三种光缆互相能连；光缆接基带、设备与连接器。"""
        for first in (GLASS, CABLE16, DENSE):
            for second in (GLASS, CABLE16, DENSE, BB8, TERMINAL, DRIVER, CONNECTOR):
                self.assertTrue(self.catalog.can_connect(first, second).allowed, (first, second))

    def test_controller_connects_physically_only_to_baseband(self):
        """13 §0.6：主控只接基带——贴着光缆、驱动器或总线都不连。"""
        self.assertTrue(self.catalog.can_connect(DOS, BB8).allowed)
        for other in (DRIVER, BUS, GLASS, DENSE, TINY):
            verdict = self.catalog.can_connect(DOS, other)
            self.assertFalse(verdict.allowed, other)
            self.assertEqual(verdict.reason, "controller_only_accepts_baseband")

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


class TestCableFaces(RegistryFixture):
    """光缆按面的判定接口（13 §3）：切断的面、被面板占住的面不连。

    数据从光缆记录的 `config` 读（离线替身，阶段 C 从方块实体
    `endernet:cuts` / `endernet:parts` 写进来）。
    """

    def record(self, position, device_id, cuts=None, parts=None):
        config = {}
        if cuts is not None:
            config[CABLE_CUTS_KEY] = cuts
        if parts is not None:
            config[CABLE_PARTS_KEY] = parts
        self.registry.register(position, device_id, config=config)
        return self.registry.get(position)

    def test_open_faces_by_default(self):
        cable = self.record(key(0), GLASS)
        for face in ("up", "down", "north", "south", "west", "east"):
            self.assertTrue(self.catalog.face_verdict(cable, face).allowed, face)

    def test_cut_face_is_closed(self):
        cable = self.record(key(0), GLASS, cuts=face_mask(["east", "up"]))
        verdict = self.catalog.face_verdict(cable, "east")
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, Reason.CABLE_FACE_CUT)
        self.assertFalse(self.catalog.face_verdict(cable, "up").allowed)
        self.assertTrue(self.catalog.face_verdict(cable, "west").allowed)

    def test_panel_occupies_its_face(self):
        cable = self.record(key(0), CABLE16, parts={"north": TERM_PART})
        verdict = self.catalog.face_verdict(cable, "north")
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, Reason.CABLE_FACE_OCCUPIED)
        self.assertIn(TERM_PART, verdict.detail)

    def test_devices_have_no_face_restrictions(self):
        """切断与面板只对光缆有意义；写在别的设备记录上也不生效。"""
        terminal = self.record(key(0), TERMINAL, cuts=face_mask(["east"]))
        self.assertTrue(self.catalog.face_verdict(terminal, "east").allowed)

    def test_link_needs_both_faces_open(self):
        left = self.record(key(0), GLASS)
        right = self.record(key(1), GLASS, cuts=face_mask(["west"]))
        verdict = self.catalog.link_verdict(left, "east", right, "west")
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, Reason.CABLE_FACE_CUT)
        self.assertFalse(self.catalog.link_verdict(right, "west", left, "east").allowed)

    def test_link_still_checks_categories_first(self):
        cable = self.record(key(0), GLASS)
        controller = self.record(key(1), TINY)
        verdict = self.catalog.link_verdict(cable, "east", controller, "west")
        self.assertEqual(verdict.reason, "controller_only_accepts_baseband")

    def test_parts_are_listed_in_face_order_and_filtered(self):
        cable = self.record(
            key(0), GLASS,
            parts={"east": CRAFT_PART, "up": TERM_PART, "west": TERMINAL, "nowhere": TERM_PART},
        )
        self.assertEqual(self.catalog.parts_of(cable), [("up", TERM_PART), ("east", CRAFT_PART)])

    def test_dense_cable_holds_no_parts(self):
        """13 §0.8：致密光缆不挂面板；写进去的既不算面板，也不占面。"""
        cable = self.record(key(0), DENSE, parts={"up": TERM_PART})
        self.assertEqual(self.catalog.parts_of(cable), [])
        self.assertTrue(self.catalog.face_verdict(cable, "up").allowed)

    def test_unreadable_cut_data_closes_every_face(self):
        """读不懂的切断数据按六面全切断：把坏数据读成「连着」会让切断的面悄悄连回去。"""
        for raw in ("east", 99, -1, 1.5, True):
            faces = CableFaces.from_record(self.record(key(0), GLASS, cuts=raw))
            self.assertEqual(len(faces.cut), 6, raw)

    def test_neighbors_respect_cut_faces(self):
        self.record(key(0), BB8)
        self.record(key(1), GLASS, cuts=face_mask(["east"]))
        self.record(key(2), TERMINAL)
        self.assertEqual(
            [n[0] for n in self.catalog.connected_neighbors(self.registry, key(1))], [key(0)]
        )
        self.assertEqual(self.catalog.connected_neighbors(self.registry, key(2)), [])


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
