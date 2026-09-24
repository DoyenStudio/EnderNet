# coding=utf-8
"""任务 02-01 至 02-07 的离线验收：端口规则、连通分量、重建与发布。

覆盖 02 验收清单中不依赖具体带宽数值的部分：

- 链、分支、环和跨区块图均正确连通；删掉桥接基带后分量正确拆开。
- 两个单控网络连通后不继续形成两个可写主控；断开后各自重新校验并恢复。
- 一个处理器通过多条路径可达时槽位不重复；未接入连接器的群提供零槽位。
- 拓扑重建跨帧期间不能使用旧版本进行新转移；无变化时不逐块重搜网络。

**离线验证的边界**：这里用的是内存注册表，不启动游戏。
引擎会发出哪些事件、事件字段是否与适配器的预期一致，不在本文件覆盖范围内。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import (
    CATEGORY_BASEBAND,
    CATEGORY_CONTROLLER,
    CATEGORY_TERMINAL,
    ConnectionRules,
    DefinitionSet,
)
from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.protocol import Reason
from endernet.server.devices.catalog import DeviceCatalog
from endernet.server.devices.registry import DeviceRegistry
from endernet.server.network.node import ALL_FACES, PortKind, PortRuleSet, opposite_face
from endernet.server.network.topology import (
    ControllerState,
    MigrationKind,
    ProcessorOwnership,
    TopologyService,
)
from endernet.server.runtime.lifecycle import ChangeKind, LifecycleChange

BB8 = "endernet:network_baseband_8"
BB16 = "endernet:network_baseband_16"
BB32 = "endernet:network_baseband_32"
TINY = "endernet:tiny_network_endpoint"
DOS = "endernet:dos_network_endpoint"
DRIVER = "endernet:storage_driver"
TERM = "endernet:common_terminal_panel"
BUS = "endernet:bus_item_io"
CONNECTOR = "endernet:parallel_processor_connector"
PROCESSOR = "endernet:parallel_processor"
MEMORY = "endernet:memory_slot"


def key(x, y=64, z=0, dimension=0):
    return NodeKey(dimension, x, y, z)


class NetworkFixture(unittest.TestCase):
    """共享建图夹具：`place`/`remove` 改变世界，`settle()` 推进重建。"""

    def setUp(self):
        self.defs = DefinitionSet()
        self.registry = DeviceRegistry(self.defs)
        self.catalog = DeviceCatalog(self.defs)
        self.topology = TopologyService(self.defs, self.registry, self.catalog)
        # 注意：监听器绑定的是这个列表对象本身，
        # 因此清空只能用 `del self.publications[:]`，不能重新赋值为新列表。
        self.publications = []
        self.topology.add_listener(self.publications.append)

    # -- 世界操作 ------------------------------------------------------

    def place(self, position, device_id, config=None, topology=None):
        self.registry.register(position, device_id, config=config)
        (topology or self.topology).mark_dirty(position)
        return self.registry.get(position)

    def build(self, position, device_id, config=None, topology=None):
        """放置并立刻推进到稳定。"""
        record = self.place(position, device_id, config=config, topology=topology)
        self.settle(topology=topology)
        return record

    def remove(self, position, device_id, topology=None):
        self.registry.unregister_matching(position, device_id)
        (topology or self.topology).mark_dirty(position)

    def settle(self, budget=100000, topology=None):
        target = topology or self.topology
        report = None
        for _ in range(64):
            report = target.advance(budget)
            if target.is_idle():
                break
        self.assert_settled(target)
        return report

    def assert_settled(self, topology=None):
        """稳定态的不变量：没有任何分量停在「重建中」。

        「重建中」只在分量被真正重建时才解除，因此每一个被撤销资格的分量
        都必须至少有一个种子能把它扫到。漏掉这一点会让一台设备永远显示
        「待确认」，而它其实什么都没发生。
        """
        target = topology or self.topology
        snapshot = target.snapshot()
        pending = [
            component.component_id
            for component in snapshot.components.values()
            if component.pending
        ]
        self.assertEqual(pending, [], "稳定态仍有分量停在重建中")
        self.assertEqual(snapshot.pending_keys, frozenset(), "稳定态仍有位置未归属")

    # -- 断言辅助 ------------------------------------------------------

    def component_of(self, position):
        return self.topology.snapshot().component_of(position)

    def state_of(self, position):
        return self.topology.snapshot().controller_state_of(position)

    def reason_codes(self, publication_list=None):
        return [
            reason.code
            for publication in (self.publications if publication_list is None else publication_list)
            for reason in publication.reasons
        ]


class TestPortRules(NetworkFixture):
    """02-01：连接的类型、方向与端口都是显式规则。"""

    def test_tables_are_consistent(self):
        """分类白名单里的每一对都必须有配套端口，否则永远建不出边。"""
        PortRuleSet.assert_consistent_with(ConnectionRules)

    def test_guard_catches_half_a_table(self):
        """守卫本身要能抓到问题，否则它只是装饰。"""

        class BrokenRules(object):
            ALLOWED_PAIRS = frozenset([(CATEGORY_TERMINAL, "endernet_not_a_category")])

        with self.assertRaises(AssertionError):
            PortRuleSet.assert_consistent_with(BrokenRules)

    def test_compatible_port_kinds(self):
        rules = PortRuleSet()
        for category_a, category_b in (
            (CATEGORY_BASEBAND, CATEGORY_BASEBAND),
            (CATEGORY_BASEBAND, CATEGORY_CONTROLLER),
            (CATEGORY_BASEBAND, "processor_connector"),
            ("processor_connector", "processor"),
            ("processor", "processor"),
        ):
            verdict = rules.link_verdict(category_a, category_b, "north", "south")
            self.assertTrue(verdict.allowed, "%s <-> %s" % (category_a, category_b))

    def test_processor_cannot_reach_baseband_by_port(self):
        """处理器与基带的端口种类不同，端口层本身就拒绝。"""
        verdict = PortRuleSet().link_verdict("processor", CATEGORY_BASEBAND, "up", "down")
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "no_shared_port_kind")

    def test_unknown_category_has_no_ports(self):
        verdict = PortRuleSet().link_verdict("ghost", CATEGORY_BASEBAND)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason, "no_ports_declared")

    def test_default_faces_are_explicitly_unrestricted(self):
        """plans 没有规定面限制，默认是**显式声明的**六面皆可。"""
        rules = PortRuleSet()
        self.assertEqual(rules.faces_of(CATEGORY_BASEBAND, PortKind.NETWORK), ALL_FACES)
        self.assertEqual(rules.faces_of(CATEGORY_TERMINAL, PortKind.NETWORK), ALL_FACES)
        self.assertEqual(rules.faces_of("processor", PortKind.PROCESSOR), ALL_FACES)

    def test_opposite_face_mirrors(self):
        self.assertEqual(opposite_face("north"), "south")
        self.assertEqual(opposite_face("up"), "down")
        self.assertEqual(opposite_face("east"), "west")

    def test_injected_face_restriction_actually_blocks_edges(self):
        """方向不是装饰：收紧面限制后，贴在一起也不会连。"""
        restricted = PortRuleSet(
            {(CATEGORY_TERMINAL, PortKind.NETWORK): frozenset(["north"])}
        )
        topology = TopologyService(
            self.defs, self.registry, self.catalog, ports=restricted
        )
        baseband = key(0, 64, 0)
        blocked = key(0, 64, -1)  # 从终端看基带在南面 → 不在白名单
        allowed = key(0, 64, 1)  # 从终端看基带在北面 → 允许

        self.place(baseband, BB8, topology=topology)
        self.place(blocked, TERM, topology=topology)
        self.place(allowed, TERM, topology=topology)
        self.settle(topology=topology)

        snapshot = topology.snapshot()
        # 被面规则拒绝的终端只能自成孤立分量
        self.assertNotEqual(
            snapshot.component_id_of(blocked), snapshot.component_id_of(baseband)
        )
        self.assertEqual(len(snapshot.component_of(blocked).members), 1)
        self.assertEqual(
            snapshot.component_id_of(allowed), snapshot.component_id_of(baseband)
        )
        self.assertGreater(topology.stats["port_rejections"], 0)


class TestComponentBuilding(NetworkFixture):
    """02-02 的基础：链、分支、环、跨区块。"""

    def test_chain_is_one_component(self):
        for x in range(5):
            self.place(key(x), BB8)
        self.settle()
        self.assertEqual(len(self.component_of(key(0)).members), 5)

    def test_different_capacity_basebands_are_two_components(self):
        """异容量基带相邻也只是两条不相干的网络带。

        blocks.md：8/16/32 位基带只与同容量基带直接连通，不同容量即使
        相邻也不建立连接、不因此合并网络或负载域。因此这两个位置各自
        成一条分量，而不是被并成一条。
        """
        self.place(key(0), BB8)
        self.place(key(1), BB16)
        self.place(key(2), BB8)
        self.settle()

        left = self.component_of(key(0))
        right = self.component_of(key(2))
        self.assertNotEqual(left.component_id, right.component_id)
        self.assertEqual(set(left.members), set([key(0)]))
        self.assertEqual(set(right.members), set([key(2)]))
        # 中间那段 16 位基带自成一条，不被两侧的 8 位基带拉过去。
        self.assertEqual(set(self.component_of(key(1)).members), set([key(1)]))

    def test_controller_can_bridge_two_capacities_as_separate_domains(self):
        """同一控制器分别接 8 位与 16 位分支是允许的。

        blocks.md 明确这不属于「基带直接混联」；两段基带彼此不相邻，
        经控制器桥接后仍必须是两个各自独立的负载域。
        """
        self.place(key(0), BB8)
        self.place(key(1), BB8)
        self.place(key(2), TINY)
        self.place(key(3), BB16)
        self.place(key(4), BB16)
        self.settle()

        # 控制器把两段都连上，分量是一个（同一张图、同一个主控）。
        component = self.component_of(key(0))
        self.assertEqual(len(component.members), 5)
        # 但两段基带是两个负载域，各算各的容量。
        snapshot = self.topology.snapshot()
        self.assertEqual(len(snapshot.domain_ids_of(key(0))), 1)
        self.assertEqual(len(snapshot.domain_ids_of(key(4))), 1)
        self.assertNotEqual(
            list(snapshot.domain_ids_of(key(0)))[0],
            list(snapshot.domain_ids_of(key(4)))[0],
        )

    def test_branch_is_one_component(self):
        self.place(key(0), BB8)
        self.place(key(1), BB8)
        self.place(key(-1), BB8)
        self.place(key(0, 65), BB8)
        self.settle()
        self.assertEqual(len(self.component_of(key(0)).members), 4)

    def test_loop_is_one_component(self):
        """四段基带围成一个环。"""
        for position in (key(0, 64, 0), key(1, 64, 0), key(1, 64, 1), key(0, 64, 1)):
            self.place(position, BB8)
        self.settle()
        component = self.component_of(key(0, 64, 0))
        self.assertEqual(len(component.members), 4)
        self.assertEqual(component.controller_state, ControllerState.NONE)
        self.assertFalse(component.is_runnable())

    def test_cross_chunk_connection(self):
        """区块边界不切断图：x=15 与 x=16 分属两个区块。"""
        self.place(key(15), BB8)
        self.place(key(16), BB8)
        self.settle()
        snapshot = self.topology.snapshot()
        self.assertNotEqual(key(15).chunk_key(), key(16).chunk_key())
        self.assertEqual(
            snapshot.component_id_of(key(15)), snapshot.component_id_of(key(16))
        )

    def test_diagonal_is_not_connected(self):
        self.place(key(0, 64, 0), BB8)
        self.place(key(1, 65, 1), BB8)
        self.settle()
        snapshot = self.topology.snapshot()
        self.assertNotEqual(
            snapshot.component_id_of(key(0, 64, 0)),
            snapshot.component_id_of(key(1, 65, 1)),
        )

    def test_adjacency_without_rule_does_not_connect(self):
        """驱动器与终端贴在一起也不连通：相邻不等于连通。"""
        self.place(key(0), DRIVER)
        self.place(key(1), TERM)
        self.settle()
        snapshot = self.topology.snapshot()
        self.assertNotEqual(
            snapshot.component_id_of(key(0)), snapshot.component_id_of(key(1))
        )

    def test_controller_connects_only_to_baseband(self):
        self.place(key(0), BB8)
        self.place(key(1), TINY)
        self.place(key(1, 65), DRIVER)  # 与控制器相邻，但控制器不接驱动器
        self.settle()
        snapshot = self.topology.snapshot()
        self.assertEqual(
            snapshot.component_id_of(key(1)), snapshot.component_id_of(key(0))
        )
        self.assertNotEqual(
            snapshot.component_id_of(key(1, 65)), snapshot.component_id_of(key(0))
        )


class TestControllerCounting(NetworkFixture):
    """02-03：控制器分组计数。"""

    def test_zero_controllers_is_not_runnable(self):
        self.place(key(0), BB8)
        self.build(key(1), DRIVER)
        component = self.component_of(key(0))
        self.assertEqual(component.controller_state, ControllerState.NONE)
        self.assertFalse(component.is_runnable())
        self.assertIn(Reason.NO_CONTROLLER, [r.code for r in component.reason_list()])

    def test_single_controller_is_the_candidate(self):
        self.place(key(0), BB8)
        self.place(key(1), TINY)
        self.settle()
        component = self.component_of(key(0))
        self.assertEqual(component.controller_state, ControllerState.SINGLE)
        self.assertTrue(component.is_runnable())
        self.assertEqual(component.master_candidate, self.registry.get(key(1)).ref)

    def test_two_controllers_are_a_conflict(self):
        self.place(key(0), BB8)
        self.place(key(1), BB8)
        self.place(key(0, 65), TINY)
        self.build(key(1, 65), DOS)
        component = self.component_of(key(0))
        self.assertEqual(component.controller_state, ControllerState.CONFLICT)
        self.assertFalse(component.is_runnable())
        self.assertIsNone(component.master_candidate)
        self.assertIn(
            Reason.CONTROLLER_CONFLICT, [r.code for r in component.reason_list()]
        )

    def test_unloaded_member_does_not_confirm_absence_of_controller(self):
        """02-03：未知/未恢复拓扑不视作确认无控制器。"""
        self.place(key(0), BB8)
        self.build(key(1), BB8)
        self.registry.mark_unloaded(key(1))
        self.topology.mark_dirty(key(1))
        self.settle()

        component = self.component_of(key(0))
        self.assertTrue(component.partial)
        self.assertEqual(component.controller_state, ControllerState.UNKNOWN)
        self.assertFalse(component.is_runnable())
        self.assertNotEqual(component.state_code(), Reason.NO_CONTROLLER)

    def test_unloaded_controller_still_counts(self):
        """暂时不可访问的控制器不能被当成已拆除。"""
        self.place(key(0), BB8)
        self.place(key(1), TINY)
        self.settle()
        self.registry.mark_unloaded(key(1))
        self.topology.mark_dirty(key(1))
        self.settle()

        component = self.component_of(key(0))
        self.assertEqual(component.controller_state, ControllerState.SINGLE)
        self.assertEqual(len(component.controllers), 1)
        self.assertTrue(component.partial)


class TestLinkDiagnostics(NetworkFixture):
    """02-07：连接状态与原因要能定位到具体设备，不能只说「离线」。"""

    def test_link_state_lists_linked_and_rejected(self):
        self.place(key(0), BB8)
        self.place(key(1), BB8)
        self.place(key(0, 63), TINY)
        self.place(key(0, 65), DRIVER)
        self.place(key(1, 65), TERM)
        self.settle()

        state = self.topology.link_state(key(0, 65))
        self.assertEqual(state.device_id, DRIVER)
        self.assertIsNotNone(state.component_id)
        self.assertTrue(state.runnable)
        self.assertIn(key(0), state.linked)
        # 驱动器与终端贴在一起，但按分类规则不允许连接
        others = [(item[1], item[2]) for item in state.rejected]
        self.assertIn((TERM, "categories_not_connectable"), others)

    def test_link_state_reports_port_level_rejection(self):
        """端口层面被拒时给的是端口原因，不是笼统的「不连通」。"""
        self.place(key(0), BB8)
        self.place(key(0, 65), PROCESSOR)
        self.settle()
        state = self.topology.link_state(key(0, 65))
        reasons = [item[2] for item in state.rejected]
        self.assertIn("processor_must_use_connector", reasons)

    def test_link_state_of_empty_position(self):
        state = self.topology.link_state(key(9))
        self.assertIsNone(state.device_id)
        self.assertIn(Reason.DEVICE_GONE, [r.code for r in state.reasons])

    def test_link_state_reflects_rebuild(self):
        self.place(key(0), BB8)
        self.place(key(1), TINY)
        self.settle()
        self.assertTrue(self.topology.link_state(key(0)).runnable)

        self.topology.mark_dirty(key(0))
        self.topology.advance(1)
        self.assertTrue(self.topology.link_state(key(0)).pending)
        self.assertFalse(self.topology.link_state(key(0)).runnable)

    def test_explain_link(self):
        self.place(key(0), BB8)
        self.place(key(1), BB8)
        self.place(key(2), BB8)
        self.place(key(2, 65), PROCESSOR)
        self.place(key(9), BB8)
        self.settle()

        self.assertTrue(self.topology.explain_link(key(0), key(1))["connected"])

        # 处理器贴着基带：原因必须是「必须经连接器」，不是笼统的不连通
        blocked = self.topology.explain_link(key(2, 65), key(2))
        self.assertFalse(blocked["connected"])
        self.assertEqual(blocked["reason"], "processor_must_use_connector")

        not_adjacent = self.topology.explain_link(key(0), key(9))
        self.assertFalse(not_adjacent["connected"])
        self.assertEqual(not_adjacent["reason"], "not_adjacent")

    def test_link_state_is_json_friendly(self):
        self.place(key(0), BB8)
        self.place(key(0, 65), DRIVER)
        self.settle()
        payload = self.topology.link_state(key(0, 65)).to_dict()
        self.assertEqual(payload["device_id"], DRIVER)
        self.assertEqual(payload["linked"], [key(0).as_key()])
        self.assertIn("reasons", payload)


class TestSplitAndMerge(NetworkFixture):
    """02-02 与验收：拆线、合网与控制器冲突。"""

    def _two_networks(self):
        """两个各自带控制器的网络，中间隔一个空位。"""
        self.place(key(0), BB8)
        self.place(key(1), TINY)
        self.place(key(3), BB8)
        self.place(key(4), TINY)
        self.settle()

    def test_removing_bridge_splits_component(self):
        for x in range(5):
            self.place(key(x), BB8)
        self.build(key(0, 65), TINY)
        self.settle()
        self.assertEqual(len(self.component_of(key(0)).members), 6)

        self.remove(key(2), BB8)
        self.settle()

        left = self.component_of(key(0))
        right = self.component_of(key(4))
        self.assertNotEqual(left.component_id, right.component_id)
        self.assertEqual(len(left.members), 3)
        self.assertEqual(len(right.members), 2)
        self.assertEqual(left.controller_state, ControllerState.SINGLE)
        self.assertEqual(right.controller_state, ControllerState.NONE)

    def test_split_keeps_the_surviving_half_identity(self):
        """拆线后只有真正被拆出去的一半换编号。

        另一半若也被通知「换了网络」，03 就会把本来好好的服务全部重挂；
        这比状态错更隐蔽，因为它不报错，只是让每次拆线都全网抖一下。
        """
        for x in range(5):
            self.place(key(x), BB8)
        self.build(key(0, 65), TINY)
        self.settle()
        original = self.component_of(key(0)).component_id
        del self.publications[:]

        self.remove(key(2), BB8)
        self.settle()

        self.assertEqual(
            self.component_of(key(0)).component_id, original, "含原锚点的一半应沿用旧编号"
        )
        self.assertNotEqual(self.component_of(key(4)).component_id, original)

        moved = set(
            notice.node_key
            for publication in self.publications
            for notice in publication.migrations
            if notice.kind == MigrationKind.COMPONENT
        )
        self.assertNotIn(key(0), moved)
        self.assertNotIn(key(0, 65), moved)
        self.assertIn(key(3), moved)
        self.assertIn(key(4), moved)
        # 一次拆线只该报一次
        self.assertEqual(self.reason_codes().count(Reason.COMPONENT_SPLIT), 1)

    def test_split_is_reported_with_a_locatable_reason(self):
        for x in range(5):
            self.place(key(x), BB8)
        self.settle()
        del self.publications[:]
        self.remove(key(2), BB8)
        self.settle()
        self.assertIn(Reason.COMPONENT_SPLIT, self.reason_codes())

    def test_two_single_controller_networks_conflict_then_recover(self):
        self._two_networks()
        self.assertEqual(self.state_of(key(0)), ControllerState.SINGLE)
        self.assertEqual(self.state_of(key(3)), ControllerState.SINGLE)

        # 连通：两个各带主控的网络相接，必须进入冲突而不是各自可写
        self.build(key(2), BB8)
        self.assertEqual(
            self.component_of(key(0)).component_id,
            self.component_of(key(3)).component_id,
        )
        self.assertEqual(self.state_of(key(0)), ControllerState.CONFLICT)
        self.assertFalse(self.component_of(key(0)).is_runnable())

        # 断开：各自重新校验并恢复
        self.remove(key(2), BB8)
        self.settle()
        self.assertNotEqual(
            self.component_of(key(0)).component_id,
            self.component_of(key(3)).component_id,
        )
        self.assertEqual(self.state_of(key(0)), ControllerState.SINGLE)
        self.assertEqual(self.state_of(key(3)), ControllerState.SINGLE)
        self.assertTrue(self.component_of(key(0)).is_runnable())

    def test_merge_is_reported(self):
        self._two_networks()
        del self.publications[:]
        self.build(key(2), BB8)
        self.assertIn(Reason.COMPONENT_MERGED, self.reason_codes())

    def test_adding_a_device_keeps_the_component_id(self):
        """加一台设备不该让整个分量换编号，否则服务会整网重挂。"""
        self.place(key(0), BB8)
        self.place(key(1), TINY)
        self.settle()
        before = self.component_of(key(0)).component_id

        self.build(key(0, 65), DRIVER)
        self.assertEqual(self.component_of(key(0)).component_id, before)
        self.assertEqual(len(self.component_of(key(0)).members), 3)


class TestProcessorGroups(NetworkFixture):
    """02-04：处理器—处理器—连接器—基带。"""

    def simple_network(self):
        """两节基带 + 一台控制器，空出 (0,65) 与 (0,66..) 给处理器群。"""
        self.place(key(0), BB8)
        self.place(key(1), BB8)
        self.place(key(1, 65), TINY)
        self.settle()

    def test_group_without_connector_contributes_zero(self):
        self.simple_network()
        self.place(key(5, 65), PROCESSOR)
        self.place(key(5, 66), PROCESSOR)
        self.settle()

        group = self.topology.snapshot().processor_group_of(key(5, 65))
        self.assertIsNotNone(group)
        self.assertEqual(group.ownership, ProcessorOwnership.UNATTACHED)
        self.assertEqual(group.slots, 2)
        self.assertEqual(group.slot_count(), 0)
        self.assertIn(
            Reason.PROCESSOR_GROUP_UNATTACHED,
            [reason.code for reason in group.reasons],
        )

    def test_group_via_connector_contributes_slots(self):
        self.simple_network()
        self.place(key(0, 65), CONNECTOR)  # 贴着基带 (0,64)
        for position in (key(0, 66), key(0, 67), key(0, 68)):
            self.place(position, PROCESSOR)
        self.settle()

        group = self.topology.snapshot().processor_group_of(key(0, 66))
        self.assertEqual(group.ownership, ProcessorOwnership.ATTACHED)
        self.assertEqual(group.slots, 3)
        self.assertEqual(group.slot_count(), 3)

    def test_multiple_paths_to_same_domain_do_not_duplicate_slots(self):
        """一个处理器群经两条路径接进同一个域，槽位只算一次。"""
        self.simple_network()
        self.place(key(0, 65), CONNECTOR)
        self.place(key(1, 65), CONNECTOR)
        self.place(key(0, 66), PROCESSOR)
        self.place(key(1, 66), PROCESSOR)
        self.settle()

        snapshot = self.topology.snapshot()
        group = snapshot.processor_group_of(key(0, 66))
        self.assertEqual(group.ownership, ProcessorOwnership.ATTACHED)
        self.assertEqual(len(group.processors), 2)
        self.assertEqual(group.slot_count(), 2)
        self.assertEqual(
            snapshot.processor_group_of(key(1, 66)).group_id, group.group_id
        )

    def test_group_across_two_domains_is_unresolved(self):
        """R03 未定：连接器接到互不相连的两个域时不擅自选一个。"""
        # 两段基带隔一格，中间放连接器——连接器本身不是基带，
        # 因此两段基带仍然是**两个**互不相连的负载域。
        self.place(key(0, 64, 0), BB8)
        self.place(key(0, 64, 2), BB8)
        self.place(key(1, 64, 0), TINY)
        self.place(key(0, 64, 1), CONNECTOR)
        self.place(key(0, 65, 1), PROCESSOR)
        self.settle()

        group = self.topology.snapshot().processor_group_of(key(0, 65, 1))
        self.assertEqual(group.ownership, ProcessorOwnership.UNRESOLVED)
        self.assertEqual(len(group.domain_ids), 2)
        self.assertEqual(group.slot_count(), 0)
        self.assertIn(
            Reason.PROCESSOR_GROUP_UNRESOLVED,
            [reason.code for reason in group.reasons],
        )

    def test_processor_never_links_to_baseband(self):
        """处理器直接贴着基带时，两者仍分属不同分量。"""
        self.place(key(0), BB8)
        self.place(key(0, 65), PROCESSOR)
        self.settle()
        snapshot = self.topology.snapshot()
        self.assertNotEqual(
            snapshot.component_id_of(key(0)), snapshot.component_id_of(key(0, 65))
        )

    def test_processor_joins_the_domain_of_its_connector(self):
        self.simple_network()
        self.place(key(0, 65), CONNECTOR)
        self.place(key(0, 66), PROCESSOR)
        self.settle()
        snapshot = self.topology.snapshot()
        self.assertEqual(
            snapshot.domain_ids_of(key(0, 66)), snapshot.domain_ids_of(key(0, 65))
        )


class TestLoadDomains(NetworkFixture):
    """02-05：业务网络与基带负载域分开建模。"""

    def test_baseband_chain_is_one_domain(self):
        for x in range(3):
            self.place(key(x), BB8)
        self.place(key(1, 65), TERM)
        self.settle()
        snapshot = self.topology.snapshot()
        self.assertEqual(len(snapshot.domains), 1)
        expected = snapshot.domain_ids_of(key(0))
        self.assertEqual(snapshot.domain_ids_of(key(2)), expected)
        self.assertEqual(snapshot.domain_ids_of(key(1, 65)), expected)

    def test_two_disjoint_branches_under_one_controller(self):
        """同一控制器接出的互不相连基带分别计算（blocks.md 明写的情形）。"""
        self.place(key(0, 64, 0), TINY)
        self.place(key(0, 65, 0), BB8)
        self.place(key(0, 63, 0), BB8)
        self.place(key(0, 66, 0), TERM)
        self.place(key(0, 62, 0), DRIVER)
        self.settle()

        snapshot = self.topology.snapshot()
        controller_domains = snapshot.domain_ids_of(key(0, 64, 0))
        upper = snapshot.domain_ids_of(key(0, 66, 0))
        lower = snapshot.domain_ids_of(key(0, 62, 0))

        self.assertEqual(len(snapshot.domains), 2, "两段互不相连的基带应各自成域")
        self.assertEqual(len(controller_domains), 2, "控制器同时属于两个域")
        self.assertEqual(len(upper), 1)
        self.assertEqual(len(lower), 1)
        self.assertNotEqual(upper, lower)
        # 图仍然是一个分量、一个控制器
        self.assertEqual(len(self.component_of(key(0, 64, 0)).members), 5)
        self.assertEqual(self.state_of(key(0, 64, 0)), ControllerState.SINGLE)

    def test_device_touching_two_domains_reports_both(self):
        """多接入（R02）：一台设备同时贴着两段互不相连的基带。"""
        self.place(key(0), BB8)
        self.place(key(2), BB8)
        self.place(key(1), TERM)
        self.settle()
        self.assertEqual(len(self.topology.snapshot().domain_ids_of(key(1))), 2)

    def test_domain_holds_only_its_own_devices(self):
        """一个域只包含自己这段基带上的设备。"""
        self.place(key(0), BB8)
        self.place(key(0, 65), TERM)
        self.place(key(9), BB8)
        self.place(key(9, 65), DRIVER)
        self.settle()
        snapshot = self.topology.snapshot()
        upper = snapshot.domain_ids_of(key(0, 65))
        lower = snapshot.domain_ids_of(key(9, 65))
        self.assertNotEqual(upper, lower)
        self.assertNotIn(key(9, 65), snapshot.domains[upper[0]].device_keys)


class TestBoundedRebuild(NetworkFixture):
    """02-02：跨帧重建与「无变化不重搜」。"""

    def test_idle_topology_visits_nothing(self):
        for x in range(6):
            self.place(key(x), BB8)
        self.settle()
        before = self.topology.stats["node_visits"]
        report = self.topology.advance(1024)
        self.assertEqual(report.steps, 0)
        self.assertEqual(report.components_rebuilt, 0)
        self.assertFalse(report.published)
        self.assertEqual(self.topology.stats["node_visits"], before)

    def test_rebuild_spans_frames_and_blocks_transfers(self):
        for x in range(6):
            self.place(key(x), BB8)
        self.build(key(0, 65), TINY)
        version_before = self.topology.version

        self.place(key(6), BB8)
        self.topology.advance(1)  # 只给一步预算，这一帧走不完整个分量
        self.assertFalse(self.topology.is_idle())

        snapshot = self.topology.snapshot()
        self.assertTrue(snapshot.is_pending(key(6)))

        verdict = self.topology.validate(self.registry.get(key(0, 65)).ref)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.status, "blocked")
        self.assertIn(Reason.REBUILDING, verdict.reason_codes())

        self.settle()
        self.assertGreater(self.topology.version, version_before)
        self.assertTrue(
            self.topology.validate(self.registry.get(key(0, 65)).ref).allowed
        )

    def test_step_budget_bounds_one_frame(self):
        for x in range(20):
            self.place(key(x), BB8)
        self.place(key(0, 65), TINY)
        self.settle()
        self.topology.mark_all_dirty()
        report = self.topology.advance(5)
        self.assertLessEqual(report.steps, 5)
        self.settle()
        self.assertEqual(len(self.component_of(key(0)).members), 21)

    def test_neighbouring_but_unconnected_device_does_not_leave_pending(self):
        """贴着但连不上的设备，不能把邻居的分量永久挂成「重建中」。"""
        self.place(key(0), BB8)
        self.place(key(0, 65), TINY)
        self.settle()

        self.place(key(0, 66), PROCESSOR)  # 与控制器相邻，但处理器不接控制器
        self.settle()

        snapshot = self.topology.snapshot()
        self.assertFalse(snapshot.is_pending(key(0)))
        self.assertTrue(snapshot.component_of(key(0)).is_runnable())
        self.assertNotEqual(
            snapshot.component_id_of(key(0, 66)), snapshot.component_id_of(key(0))
        )

    def test_change_during_cross_frame_rebuild_is_not_lost(self):
        """跨帧期间到达的变化不能因为「这一帧已经扫过这里」被丢掉。"""
        for x in range(8):
            self.place(key(x), BB8)
        self.place(key(0, 65), TINY)
        self.settle()

        self.topology.mark_all_dirty()  # 制造一次需要多帧的重建
        self.topology.advance(2)
        self.place(key(3, 65), DRIVER)  # 重建尚未完成时再加一台设备
        self.settle()

        snapshot = self.topology.snapshot()
        self.assertFalse(snapshot.is_pending(key(3, 65)))
        self.assertEqual(
            snapshot.component_id_of(key(3, 65)), snapshot.component_id_of(key(0))
        )


class TestPublication(NetworkFixture):
    """02-06 与 02-07：版本发布、迁移通知与可定位原因。"""

    def test_version_only_moves_on_change(self):
        self.place(key(0), BB8)
        self.settle()
        version = self.topology.version
        self.topology.advance(1024)
        self.assertEqual(self.topology.version, version)

    def test_migration_notice_for_new_device(self):
        self.place(key(0), BB8)
        self.place(key(1), TINY)
        self.settle()
        del self.publications[:]

        record = self.build(key(0, 65), DRIVER)
        notices = [
            notice
            for publication in self.publications
            for notice in publication.migrations
            if notice.device_id == DRIVER
        ]
        self.assertTrue(notices)
        self.assertEqual(notices[-1].ref, record.ref)
        self.assertEqual(notices[-1].kind, MigrationKind.COMPONENT)
        self.assertIsNone(notices[-1].from_component_id)
        self.assertIsNotNone(notices[-1].to_component_id)

    def test_migration_notice_for_detached_device(self):
        self.place(key(0), BB8)
        self.place(key(1), TINY)
        self.build(key(0, 65), DRIVER)
        del self.publications[:]

        self.remove(key(0, 65), DRIVER)
        self.settle()
        kinds = [
            notice.kind
            for publication in self.publications
            for notice in publication.migrations
        ]
        self.assertIn(MigrationKind.DETACHED, kinds)

    def test_slot_source_changes_are_published(self):
        """02-06：交给 09 的槽位来源变化。"""
        self.place(key(0), BB8)
        self.place(key(1), BB8)
        self.place(key(1, 65), TINY)
        self.place(key(0, 65), CONNECTOR)
        self.place(key(0, 66), PROCESSOR)
        self.settle()

        del self.publications[:]
        self.remove(key(0, 65), CONNECTOR)
        self.settle()
        changes = [
            change
            for publication in self.publications
            for change in publication.slot_changes
        ]
        self.assertTrue(changes)
        self.assertTrue(
            any(
                change.before_slots == 1 and change.after_slots == 0
                for change in changes
            )
        )

    def test_snapshot_is_stable_across_rebuilds(self):
        """旧快照发布后不再变化：新的重建生成的是**新**快照对象。"""
        self.place(key(0), BB8)
        self.settle()
        first = self.topology.snapshot()

        self.build(key(1), BB8)
        second = self.topology.snapshot()

        self.assertIsNot(first, second)
        self.assertEqual(len(first.components), 1)
        self.assertEqual(len(second.components), 1)
        self.assertLess(first.version, second.version)
        # 旧快照仍然读出旧成员数
        self.assertEqual(first.component_of(key(1)), None)

    def test_validate_rejects_stale_version(self):
        self.place(key(0), BB8)
        self.place(key(1), TINY)
        self.settle()
        ref = self.registry.get(key(1)).ref

        verdict = self.topology.validate(ref, expected_version=self.topology.version - 1)
        self.assertFalse(verdict.allowed)
        self.assertIn(Reason.STALE_VERSION, verdict.reason_codes())
        self.assertEqual(verdict.status, "stale")

        self.assertTrue(self.topology.validate(ref, self.topology.version).allowed)

    def test_validate_reports_gone_instance(self):
        self.place(key(0), BB8)
        self.place(key(1), TINY)
        self.settle()
        record = self.registry.get(key(1))
        ghost = DeviceRef(record.node_key, record.generation + 7)

        verdict = self.topology.validate(ghost)
        self.assertFalse(verdict.allowed)
        self.assertIn(Reason.DEVICE_GONE, verdict.reason_codes())

    def test_validate_blocks_on_controller_conflict(self):
        self.place(key(0), BB8)
        self.place(key(1), BB8)
        self.place(key(0, 65), TINY)
        self.build(key(1, 65), DOS)
        verdict = self.topology.validate(self.registry.get(key(0, 65)).ref)
        self.assertFalse(verdict.allowed)
        self.assertIn(Reason.CONTROLLER_CONFLICT, verdict.reason_codes())

    def test_validate_blocks_unloaded_device(self):
        self.place(key(0), BB8)
        self.place(key(1), TINY)
        self.settle()
        ref = self.registry.get(key(1)).ref
        self.registry.mark_unloaded(key(1))
        self.topology.mark_dirty(key(1))
        self.settle()
        # 卸载不改变图的边界，但不可访问的设备不能放行新业务；
        # 原因必须是「所在区块未加载」，不是笼统的「离线」。
        verdict = self.topology.validate(ref)
        self.assertFalse(verdict.allowed)
        self.assertIn(Reason.UNLOADED_MEMBER, verdict.reason_codes())

    def test_lifecycle_place_review_does_not_dirty(self):
        """「尝试破坏」不改图：方块还在。"""
        self.place(key(0), BB8)
        self.settle()
        del self.publications[:]
        self.topology.on_lifecycle_change(
            LifecycleChange(
                ChangeKind.PLACEMENT_REVIEW, key(0), BB8, self.registry.get(key(0)).ref
            )
        )
        self.assertEqual(self.topology.advance(16).steps, 0)
        self.assertEqual(self.publications, [])

    def test_same_frame_changes_are_merged_into_one_rebuild(self):
        """同帧多次变化只算一次重建：版本不会因为设备数量线性增长。"""
        self.topology.mark_all_dirty()
        for x in range(6):
            self.registry.register(key(x), BB8)
            self.topology.mark_dirty(key(x))
        before = self.topology.version
        self.settle()
        self.assertLessEqual(self.topology.version - before, 3)


if __name__ == "__main__":
    unittest.main()
