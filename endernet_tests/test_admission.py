# coding=utf-8
"""任务 02-05 与 02-08 的离线验收：线路容量、逐段准入与设备带宽状态。

2026-11-09 网络改版（`.claude/13` §4）后的口径，取代 R02 的「基带负载域」：

- 根是基带的外露面，容量 = 该基带的档位；一块基带的几个外露面各是一条线路，
  不合成一个池（作者定案 13 §0.1）。
- 分层 BFS 建树（32u → 16u → 8u → 设备透传），消费者按访问顺序沿父链逐段核容量：
  光缆用本档、设备用透传 8u（R14-a，作者定案）、根用档位。拒绝原因「所在线路已满」
  并点名瓶颈段。
- 只是设备增减或需求变化时沿路径增量更新；结构没变的线路原样保留；超预算分帧。
- R13（恰好满载的显示口径）仍是具名替身；R02 的策略字段全部退役。

断言里的期望值都按规则**独立写出**（哪一台被拒、瓶颈是哪一段、各段读数），
不从实现反推。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import UNCONFIGURED, DefinitionSet
from endernet.common.identity import DeviceRef, NodeKey, PartKey
from endernet.common.protocol import Reason
from endernet.server.devices.catalog import CABLE_PARTS_KEY, DeviceCatalog
from endernet.server.devices.registry import DeviceRegistry
from endernet.server.network.admission import (
    AdmissionService,
    BandwidthPolicy,
    BandwidthRole,
    BandwidthState,
    SaturationMode,
    UNRESOLVED_FIELDS,
)
from endernet.server.network.topology import TopologyService

BB8 = "endernet:network_baseband_8"
BB16 = "endernet:network_baseband_16"
BB32 = "endernet:network_baseband_32"
TINY = "endernet:tiny_network_endpoint"
DRIVER = "endernet:storage_driver"
TERM = "endernet:common_terminal_panel"
BUS = "endernet:bus_item_io"
MEMORY = "endernet:memory_slot"
CONNECTOR = "endernet:parallel_processor_connector"
PROCESSOR = "endernet:parallel_processor"
ENERGY = "endernet:energy_input_interface"
GLASS = "endernet:ender_glass_cable"
CABLE16 = "endernet:ender_cable"
DENSE = "endernet:ender_dense_cable"
TERM_PART = "endernet:common_terminal_part"

#: 1u = 4 个四分之一 u
U = 4


def key(x, y=64, z=0, dimension=0):
    return NodeKey(dimension, x, y, z)


class AdmissionFixture(unittest.TestCase):
    """共享夹具。子类可以用 `policy_kwargs` 指定策略。"""

    policy_kwargs = {}

    def setUp(self):
        self.defs = DefinitionSet()
        self.registry = DeviceRegistry(self.defs)
        self.catalog = DeviceCatalog(self.defs)
        self.topology = TopologyService(self.defs, self.registry, self.catalog)
        self.policy = BandwidthPolicy(**self.policy_kwargs)
        self.admission = AdmissionService(
            self.defs, self.registry, self.topology, policy=self.policy
        )
        self.changes = []
        self.admission.add_listener(self.changes.extend)

    # -- 世界操作 ------------------------------------------------------

    def place(self, position, device_id, config=None):
        self.registry.register(position, device_id, config=config)
        self.topology.mark_dirty(position)
        return self.registry.get(position)

    def remove(self, position, device_id):
        self.registry.unregister_matching(position, device_id)
        self.topology.mark_dirty(position)

    def settle(self, budget=100000):
        for _ in range(64):
            self.topology.advance(budget)
            if self.topology.is_idle():
                break
        # 稳定态不允许残留「重建中」：否则设备会永远显示待确认。
        snapshot = self.topology.snapshot()
        pending = [
            component.component_id
            for component in snapshot.components.values()
            if component.pending
        ]
        self.assertEqual(pending, [], "稳定态仍有分量停在重建中")

    # -- 读数 ----------------------------------------------------------

    def status(self, position):
        record = self.registry.get(position)
        self.assertIsNotNone(record, "位置 %r 没有设备" % (position.as_key(),))
        return self.admission.status_of(record.ref)

    def line_of(self, position):
        root_id = self.topology.snapshot().line_of(position)
        self.assertIsNotNone(root_id, "位置 %r 不在任何线路上" % (position.as_key(),))
        line = self.admission.line(root_id)
        self.assertIsNotNone(line, "线路 %s 还没算完" % (root_id,))
        return line

    def granted(self, positions):
        return [p for p in positions if self.status(p).state == BandwidthState.GRANTED]

    def denied(self, positions):
        return [p for p in positions if self.status(p).state == BandwidthState.DENIED]

    # -- 常用布局 ------------------------------------------------------

    def glass_branch_on_dense_trunk(self):
        """32 位基带 → 致密主干三格 → 玻璃支线三格，支线上挂 9 台终端（各 1u）。

        访问顺序（独立推出来的）：致密先走完，再走玻璃；每格玻璃展开时按位置
        访问它周围的终端与下一格玻璃。于是 9 台终端依次是 x=1、2、3 各三台，
        最后一台是 (3,65,3)，它走到 G1 时 G1 已经 8/8。终端都不贴主干，
        否则会被先展开的致密光缆收走。
        """
        self.place(key(0, 64, 0), BB32)
        self.place(key(0, 63, 0), TINY)
        trunk = [key(0, 64, 1), key(0, 64, 2), key(0, 64, 3)]
        for position in trunk:
            self.place(position, DENSE)
        branch = [key(1, 64, 3), key(2, 64, 3), key(3, 64, 3)]
        for position in branch:
            self.place(position, GLASS)
        terminals = []
        for x in (1, 2, 3):
            for position in (key(x, 63, 3), key(x, 64, 4), key(x, 65, 3)):
                terminals.append(position)
        for position in terminals:
            self.place(position, TERM)
        self.settle()
        return trunk, branch, terminals


class TestPolicyGuard(unittest.TestCase):
    """R13 仍是具名替身；R02 的策略字段随负载域退役。"""

    def test_only_r13_is_left(self):
        self.assertEqual(
            [(field, rule) for field, rule, _ in UNRESOLVED_FIELDS],
            [("saturation", "R13")],
        )

    def test_report_lists_every_unresolved_field(self):
        report = BandwidthPolicy().unresolved_report()
        self.assertEqual(
            set(item["field"] for item in report),
            set(field for field, _, _ in UNRESOLVED_FIELDS),
        )
        self.assertNotIn("R02", [item["rule"] for item in report])

    def test_assert_settled_refuses_to_declare_victory(self):
        """规则未定稿前，不能宣布带宽数值已验收。"""
        with self.assertRaises(AssertionError):
            BandwidthPolicy().assert_settled()

    def test_policy_is_in_the_diagnostics(self):
        report = BandwidthPolicy().describe()
        self.assertEqual(report["saturation"], SaturationMode.GRANTED_IS_OK)
        self.assertEqual(len(report["unresolved"]), 1)

    def test_retired_r02_knobs_are_gone(self):
        """容量合成、环路、多接入、取整、优先级都不再是可调的策略字段。"""
        policy = BandwidthPolicy()
        for name in ("capacity_mode", "loop_mode", "multi_access", "rounding", "priorities"):
            self.assertFalse(hasattr(policy, name), name)
        with self.assertRaises(TypeError):
            BandwidthPolicy(capacity_mode="sum_of_segments")

    def test_unknown_saturation_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            BandwidthPolicy(saturation="whatever")


class TestRootCapacity(AdmissionFixture):
    """13 §4.1：根是外露面，容量 = 该基带的档位；外露面之间不合池。"""

    def test_each_tier_outputs_its_own_capacity_per_face(self):
        for x, device_id, units in ((0, BB8, 8), (20, BB16, 16), (40, BB32, 32)):
            self.place(key(x), device_id)
            self.place(key(x, 65), TERM)
        self.settle()
        for x, units in ((0, 8), (20, 16), (40, 32)):
            line = self.line_of(key(x, 65))
            self.assertEqual(line.capacity_quarters, units * U)
            self.assertEqual(line.used_quarters, U)

    def test_faces_do_not_pool(self):
        """东面那条线路满了，同一块基带西面那条照样有自己的 8u。"""
        self.place(key(0, 64, 0), BB8)
        self.place(key(0, 63, 0), TINY)
        west = key(-1, 64, 0)
        self.place(west, TERM)
        for x in (1, 2, 3):
            self.place(key(x, 64, 0), GLASS)
        east = []
        for x in (1, 2):
            for position in (key(x, 63, 0), key(x, 64, -1), key(x, 64, 1), key(x, 65, 0)):
                east.append(position)
        east.append(key(3, 65, 0))
        for position in east:
            self.place(position, TERM)
        self.settle()

        self.assertEqual(self.granted(east), east[:8])
        self.assertEqual(self.denied(east), [key(3, 65, 0)])
        self.assertEqual(self.line_of(key(1, 64, 0)).used_quarters, 8 * U)
        self.assertEqual(self.status(west).state, BandwidthState.GRANTED)
        self.assertEqual(self.line_of(west).used_quarters, U)
        self.assertNotEqual(
            self.status(west).bandwidth_domain_id, self.status(east[0]).bandwidth_domain_id
        )

    def test_mixed_tier_core_faces_output_their_own_tier(self):
        """R14-b（2026-11-09 作者改定允许混搭）：8 位与 16 位基带贴着组成一个核心，
        各自的外露面出各自的档位。"""
        self.place(key(0), BB8)
        self.place(key(1), BB16)
        self.place(key(0, 63), TINY)
        self.place(key(0, 65), TERM)
        self.place(key(1, 65), TERM)
        self.settle()
        self.assertEqual(self.line_of(key(0, 65)).capacity_quarters, 8 * U)
        self.assertEqual(self.line_of(key(1, 65)).capacity_quarters, 16 * U)
        component = self.topology.snapshot().component_of(key(0))
        self.assertEqual(component.baseband_state, "valid")


class TestPathCapacity(AdmissionFixture):
    """13 §4.3：沿父链逐段核容量，拒绝原因点名瓶颈段。"""

    def test_glass_branch_on_dense_trunk_is_capped_at_8u(self):
        trunk, branch, terminals = self.glass_branch_on_dense_trunk()
        self.assertEqual(self.granted(terminals), terminals[:8])
        self.assertEqual(self.denied(terminals), [terminals[8]])

        result = self.status(terminals[8])
        self.assertEqual(result.reason_codes(), (Reason.BANDWIDTH_LINE_FULL,))
        self.assertEqual(result.reasons[0].detail, "所在线路已满：末影玻璃光缆 8/8 u")
        self.assertEqual(result.segment.segment_id, branch[0])
        self.assertEqual((result.segment.used_quarters, result.segment.capacity_quarters), (32, 32))
        self.assertFalse(result.ok)
        self.assertEqual(result.allocated_quarters, 0)

        line = self.line_of(trunk[0])
        # 主干每一段都过了 8 台：8u / 32u；根同样 8u / 32u。
        for position in trunk:
            self.assertEqual((line.used[position], line.capacity[position]), (32, 128))
        self.assertEqual((line.used_quarters, line.capacity_quarters), (32, 128))
        # 支线：G1 过 8 台、G2 过 x=2、3 的五台、G3 过 x=3 的两台（第 9 台被拒）。
        self.assertEqual([line.used[position] for position in branch], [32, 20, 8])

    def test_devices_on_the_trunk_are_unaffected_by_a_full_branch(self):
        trunk, branch, terminals = self.glass_branch_on_dense_trunk()
        extra = self.place(key(-1, 64, 1), TERM)  # 贴着致密主干第一格
        self.settle()
        self.assertEqual(self.status(extra.node_key).state, BandwidthState.GRANTED)
        self.assertEqual(self.denied(terminals), [terminals[8]])

    def test_three_tiers_interconnect_and_the_narrowest_segment_binds(self):
        """32 位基带 → 致密 → 普通 → 玻璃；玻璃之后的 9 台只放得下 8 台。"""
        self.place(key(0), BB32)
        self.place(key(0, 63), TINY)
        self.place(key(1), DENSE)
        self.place(key(2), CABLE16)
        self.place(key(3), GLASS)
        terminals = [
            key(3, 65), key(3, 63), key(3, 64, 1), key(3, 64, -1),
            key(4), key(4, 65), key(4, 63), key(4, 64, 1), key(4, 64, -1),
        ]
        for position in terminals:
            self.place(position, TERM)
        self.settle()
        denied = self.denied(terminals)
        self.assertEqual(len(denied), 1)
        result = self.status(denied[0])
        self.assertEqual(result.segment.label, "末影玻璃光缆")
        line = self.line_of(key(1))
        self.assertEqual(line.capacity[key(2)], 16 * U)
        self.assertEqual(line.used[key(2)], 8 * U)
        self.assertEqual(line.used[key(1)], 8 * U)

    def test_device_passthrough_is_8u(self):
        """R14-a（作者定案 8u）：9 台终端都挂在第一台终端后面，只放得下 8 台。"""
        self.place(key(0, 64, 0), BB32)
        self.place(key(0, 63, 0), TINY)
        head = key(0, 65, 0)
        self.place(head, TERM)
        behind = [
            key(-1, 65, 0), key(0, 65, -1), key(0, 65, 1), key(0, 66, 0), key(1, 65, 0),
            key(-2, 65, 0), key(0, 65, 2), key(0, 67, 0), key(2, 65, 0),
        ]
        for position in behind:
            self.place(position, TERM)
        self.settle()

        self.assertEqual(self.status(head).state, BandwidthState.GRANTED)
        self.assertEqual(self.denied(behind), [key(2, 65, 0)])
        result = self.status(key(2, 65, 0))
        self.assertEqual(result.reasons[0].detail, "所在线路已满：通用终端面板 8/8 u")
        line = self.line_of(head)
        self.assertEqual(line.used[head], 8 * U)
        # 根：第一台自己 1u + 它后面放行的 8u。
        self.assertEqual(line.used_quarters, 9 * U)

    def test_a_processor_group_counts_once(self):
        """2026-11-09 作者定案：经连接器连成的一组处理器整组只算一次（同 AE2 合成 CPU）。

        需求取定义表里一台并行处理器的 4u，由最先访问到的那台代表；组内连接
        不受透传容量限制。三台都在线，线路上只记 4u。
        """
        self.place(key(0, 64, 0), BB32)
        self.place(key(0, 63, 0), TINY)
        self.place(key(0, 65, 0), CONNECTOR)
        processors = [key(0, 66, 0), key(0, 67, 0), key(0, 68, 0)]
        for position in processors:
            self.place(position, PROCESSOR)
        self.settle()
        self.assertEqual(self.granted(processors), processors)
        line = self.line_of(key(0, 65, 0))
        # 连接器自己的带宽未列（R10），不计入；整组只记一台处理器的 4u。
        self.assertEqual(line.used_quarters, 4 * U)
        self.assertEqual(line.used[key(0, 65, 0)], 4 * U)
        lead = self.status(processors[0])
        self.assertEqual((lead.demand_quarters, lead.allocated_quarters), (4 * U, 4 * U))
        self.assertTrue(any("处理器组共 3 台" in note for note in lead.notes))
        for position in processors[1:]:
            member = self.status(position)
            self.assertEqual((member.demand_quarters, member.allocated_quarters), (0, 0))
            self.assertTrue(member.ok)
            self.assertIn("由 (0, 66, 0) 那台代表计入", member.notes[0])

    def test_groups_behind_one_connector_share_its_passthrough(self):
        """每组按 4u 算；一台连接器透传 8u，第三组被拒，瓶颈是连接器，整组一起离线。"""
        self.place(key(0, 64, 0), BB32)
        self.place(key(0, 63, 0), TINY)
        self.place(key(0, 65, 0), CONNECTOR)
        north = [key(0, 65, -1)]
        south = [key(0, 65, 1)]
        up = [key(0, 66, 0), key(0, 67, 0)]  # 按位置最后访问到的一组
        for position in north + south + up:
            self.place(position, PROCESSOR)
        self.settle()
        self.assertEqual(self.granted(north + south), north + south)
        self.assertEqual(self.denied(up), up)
        for position in up:
            self.assertEqual(
                self.status(position).reasons[0].detail, "所在线路已满：并行处理器连接器 8/8 u"
            )
        self.assertEqual(self.line_of(key(0, 65, 0)).used_quarters, 8 * U)

    def test_a_group_reached_from_two_lines_is_charged_once(self):
        self.place(key(0, 64, 0), BB32)
        self.place(key(0, 63, 0), TINY)
        self.place(key(0, 65, 0), CONNECTOR)  # 上面那条线路
        self.place(key(0, 64, 1), CONNECTOR)  # 南面那条线路
        group = [key(0, 65, 1), key(0, 66, 1)]
        for position in group:
            self.place(position, PROCESSOR)
        self.settle()
        self.assertEqual(self.granted(group), group)
        # 两台连接器都在深度 1，按位置南面那台先展开，整组记在南面那条线路上。
        self.assertEqual(self.line_of(key(0, 64, 1)).used_quarters, 4 * U)
        self.assertEqual(self.line_of(key(0, 65, 0)).used_quarters, 0)
        self.assertEqual(
            set(self.status(position).bandwidth_domain_id for position in group),
            set(["line:0:0,64,0:south"]),
        )

    def test_processors_do_not_carry_the_line_onward(self):
        """处理器不透传（13 §3）：只经处理器才够得着的连接器与它后面的设备不在线路上。"""
        self.place(key(0, 64, 0), BB32)
        self.place(key(0, 63, 0), TINY)
        self.place(key(0, 65, 0), CONNECTOR)
        self.place(key(0, 66, 0), PROCESSOR)
        self.place(key(0, 67, 0), CONNECTOR)
        terminal = self.place(key(0, 68, 0), TERM)
        self.settle()
        snapshot = self.topology.snapshot()
        self.assertEqual(
            snapshot.component_id_of(terminal.node_key), snapshot.component_id_of(key(0))
        )
        result = self.status(terminal.node_key)
        self.assertEqual(result.state, BandwidthState.DENIED)
        self.assertIn(Reason.NO_BANDWIDTH_PATH, result.reason_codes())
        self.assertIsNone(snapshot.line_of(key(0, 67, 0)))


class TestBoundaries(AdmissionFixture):
    """恰好满载、单机需求超过一段、没满但剩余不够：三种情形分开说。"""

    def glass_line(self, count):
        """8 位基带东面一条玻璃线（三格），按访问顺序挂 `count` 台终端。"""
        self.place(key(0, 64, 0), BB8)
        self.place(key(0, 63, 0), TINY)
        for x in (1, 2, 3):
            self.place(key(x, 64, 0), GLASS)
        slots = []
        for x in (1, 2, 3):
            slots.extend([key(x, 63, 0), key(x, 64, -1), key(x, 64, 1), key(x, 65, 0)])
        for position in slots[:count]:
            self.place(position, TERM)
        return slots

    def test_exactly_full_is_still_ok(self):
        """R13 的建议口径：已获准即满足；满载只做说明。"""
        slots = self.glass_line(8)
        self.settle()
        result = self.status(slots[7])
        self.assertEqual(result.state, BandwidthState.GRANTED)
        self.assertTrue(result.ok)
        self.assertTrue(any("已满载" in note for note in result.notes))
        self.assertEqual(result.segment.text(), "末影玻璃光缆 8/8 u")

    def test_one_more_device_is_denied_without_disturbing_the_others(self):
        slots = self.glass_line(8)
        self.settle()
        ninth = self.place(key(4, 64, 0), TERM)  # 贴着 G3，访问顺序在最后
        self.settle()
        self.assertEqual(self.status(ninth.node_key).state, BandwidthState.DENIED)
        self.assertEqual(self.granted(slots[:8]), slots[:8])

    def test_not_full_but_insufficient_room_is_not_reported_as_full(self):
        """7/8 u 时再来一台要 2u 的驱动器：说清还剩多少、它要多少。"""
        slots = self.glass_line(7)
        driver = self.place(slots[7], DRIVER, config={"disk_count": 8})
        self.settle()
        result = self.status(driver.node_key)
        self.assertEqual(result.state, BandwidthState.DENIED)
        self.assertEqual(
            result.reasons[0].detail,
            "所在线路已满：末影玻璃光缆 7/8 u，本设备需要 2 u",
        )

    def test_single_demand_beyond_a_segment_is_not_reported_as_full(self):
        """单机需求超过一段的容量：那一段还空着也放不下，原因必须分开报。"""
        definition = self.defs.get(TERM)
        original = definition.bandwidth.base_quarters
        try:
            definition.bandwidth.base_quarters = 10 * U
            slots = self.glass_line(1)
            self.settle()
            result = self.status(slots[0])
        finally:
            definition.bandwidth.base_quarters = original
        self.assertEqual(result.state, BandwidthState.DENIED)
        self.assertEqual(result.reason_codes(), (Reason.BANDWIDTH_DEMAND_EXCEEDS_LINE,))
        self.assertIn("末影玻璃光缆 0/8 u", result.reasons[0].detail)


class TestSaturationMode(AdmissionFixture):
    """恰好满载的显示口径（R13）。"""

    policy_kwargs = {"saturation": SaturationMode.REQUIRE_HEADROOM}

    def test_require_headroom_flips_a_granted_device(self):
        self.place(key(0, 64, 0), BB8)
        self.place(key(0, 63, 0), TINY)
        self.place(key(1, 64, 0), GLASS)
        drivers = [key(1, 63, 0), key(1, 64, -1), key(1, 64, 1), key(1, 65, 0)]
        for position in drivers:
            self.place(position, DRIVER, config={"disk_count": 8})  # 各 2u，合计 8/8 u
        self.settle()
        result = self.status(drivers[-1])
        self.assertEqual(result.state, BandwidthState.GRANTED)
        self.assertFalse(result.ok, "该策略下满载即不再视为 OK")


class TestIncrementalUpdates(AdmissionFixture):
    """13 §4.4：结构没变的线路不重算；叶子增减与需求变化沿路径增量更新。"""

    def baseline(self):
        """8 位基带：西面一台终端（自成一条），东面玻璃线上 8 台终端，正好满。"""
        self.place(key(0, 64, 0), BB8)
        self.place(key(0, 63, 0), TINY)
        self.place(key(-1, 64, 0), TERM)
        for x in (1, 2, 3):
            self.place(key(x, 64, 0), GLASS)
        east = []
        for x in (1, 2):
            east.extend([key(x, 63, 0), key(x, 64, -1), key(x, 64, 1), key(x, 65, 0)])
        for position in east:
            self.place(position, TERM)
        self.settle()
        return east

    def allocation(self, admission, positions):
        """独立核对用：每台设备的 (状态, 分配量)。"""
        return [
            (
                admission.status_of(self.registry.get(position).ref).state,
                admission.status_of(self.registry.get(position).ref).allocated_quarters,
            )
            for position in positions
        ]

    def full_recompute(self):
        """在同一个世界上另起一份准入服务——它只会整条重算——作为对照。"""
        return AdmissionService(self.defs, self.registry, self.topology)

    def test_a_line_whose_structure_did_not_change_is_kept(self):
        east = self.baseline()
        west = self.status(key(-1, 64, 0))
        kept = self.admission.stats["lines_kept"]
        recomputed = self.admission.stats["lines_recomputed"]

        self.place(key(0, 64, 1), TERM)  # 南面新开一条线路
        self.settle()

        self.assertEqual(self.admission.stats["lines_recomputed"], recomputed + 1)
        self.assertGreaterEqual(self.admission.stats["lines_kept"], kept + 2)
        self.assertEqual(self.status(key(-1, 64, 0)).fingerprint(), west.fingerprint())
        self.assertEqual(self.granted(east), east)

    def test_adding_a_leaf_at_the_end_walks_only_its_path(self):
        east = self.baseline()
        recomputed = self.admission.stats["lines_recomputed"]
        updates = self.admission.stats["path_updates"]

        late = self.place(key(3, 65, 0), TERM)  # 挂在 G3 上，访问顺序在最后
        self.settle()

        self.assertEqual(self.admission.stats["lines_recomputed"], recomputed)
        self.assertGreater(self.admission.stats["path_updates"], updates)
        self.assertEqual(self.status(late.node_key).state, BandwidthState.DENIED)
        reference = self.full_recompute()
        everyone = east + [key(3, 65, 0)]
        self.assertEqual(self.allocation(self.admission, everyone), self.allocation(reference, everyone))

    def test_removing_a_granted_leaf_with_nobody_waiting_is_a_path_update(self):
        east = self.baseline()
        recomputed = self.admission.stats["lines_recomputed"]
        self.remove(east[0], TERM)
        self.settle()
        self.assertEqual(self.admission.stats["lines_recomputed"], recomputed)
        self.assertEqual(self.line_of(key(1, 64, 0)).used_quarters, 7 * U)

    def test_a_multi_frame_rebuild_does_not_throw_the_lines_away(self):
        """拓扑跨帧重建期间线路树暂时不在快照里，线路结果要留到重建完。"""
        east = self.baseline()
        recomputed = self.admission.stats["lines_recomputed"]
        self.remove(east[0], TERM)
        rounds = 0
        while not self.topology.is_idle():
            self.topology.advance(3)
            rounds += 1
        self.assertGreater(rounds, 1)
        self.assertEqual(self.admission.stats["lines_recomputed"], recomputed)
        self.assertEqual(self.line_of(key(1, 64, 0)).used_quarters, 7 * U)
        self.assertEqual(self.granted(east[1:]), east[1:])

    def test_removing_a_granted_leaf_while_someone_waits_recomputes_the_line(self):
        east = self.baseline()
        waiting = self.place(key(3, 65, 0), TERM)
        self.settle()
        self.assertEqual(self.status(waiting.node_key).state, BandwidthState.DENIED)
        recomputed = self.admission.stats["lines_recomputed"]

        self.remove(east[0], TERM)
        self.settle()

        self.assertEqual(self.admission.stats["lines_recomputed"], recomputed + 1)
        self.assertEqual(self.status(waiting.node_key).state, BandwidthState.GRANTED)
        self.assertEqual(self.line_of(key(1, 64, 0)).used_quarters, 8 * U)

    def test_demand_change_within_room_is_a_path_update(self):
        self.place(key(0, 64, 0), BB8)
        self.place(key(0, 63, 0), TINY)
        self.place(key(1, 64, 0), GLASS)
        first = self.place(key(1, 65, 0), DRIVER, config={"disk_count": 0})
        second = self.place(key(1, 63, 0), DRIVER, config={"disk_count": 4})
        self.settle()
        recomputed = self.admission.stats["lines_recomputed"]
        updates = self.admission.stats["path_updates"]

        first.config["disk_count"] = 8  # 0 → 2u，玻璃上 1u + 2u，仍有余量
        self.admission.note_device_load_changed(first.node_key)

        self.assertEqual(self.admission.stats["lines_recomputed"], recomputed)
        self.assertEqual(self.admission.stats["path_updates"], updates + 1)
        self.assertEqual(self.line_of(key(1, 64, 0)).used_quarters, 3 * U)
        self.assertEqual(self.status(first.node_key).allocated_quarters, 2 * U)
        self.assertEqual(self.status(second.node_key).state, BandwidthState.GRANTED)

    def test_demand_change_that_does_not_fit_recomputes_and_matches_a_full_pass(self):
        """排在前面的驱动器插满了盘：后面的设备按「先到先得」被挤出去。"""
        self.place(key(0, 64, 0), BB8)
        self.place(key(0, 63, 0), TINY)
        self.place(key(1, 64, 0), GLASS)
        early = self.place(key(1, 63, 0), DRIVER, config={"disk_count": 0})
        others = [key(1, 64, -1), key(1, 64, 1), key(1, 65, 0)]
        for position in others:
            self.place(position, DRIVER, config={"disk_count": 10})  # 各 2.5u
        self.settle()
        self.assertEqual(self.granted(others), others)
        recomputed = self.admission.stats["lines_recomputed"]

        early.config["disk_count"] = 10  # 0 → 2.5u：前三台 7.5u，第四台放不下
        self.admission.note_device_load_changed(early.node_key)

        self.assertEqual(self.admission.stats["lines_recomputed"], recomputed + 1)
        self.assertEqual(self.denied(others), [key(1, 65, 0)])
        reference = self.full_recompute()
        everyone = [key(1, 63, 0)] + others
        self.assertEqual(self.allocation(self.admission, everyone), self.allocation(reference, everyone))

    def test_demand_drop_frees_room_for_a_waiting_device(self):
        self.place(key(0, 64, 0), BB8)
        self.place(key(0, 63, 0), TINY)
        self.place(key(1, 64, 0), GLASS)
        early = self.place(key(1, 63, 0), DRIVER, config={"disk_count": 10})
        others = [key(1, 64, -1), key(1, 64, 1), key(1, 65, 0)]
        for position in others:
            self.place(position, DRIVER, config={"disk_count": 10})
        self.settle()
        self.assertEqual(self.denied(others), [key(1, 65, 0)])

        early.config["disk_count"] = 0
        self.admission.note_device_load_changed(early.node_key)
        self.assertEqual(self.granted(others), others)
        self.assertEqual(self.line_of(key(1, 64, 0)).used_quarters, 30)


class TestFraming(AdmissionFixture):
    """13 §4.4：整条重算超出预算就分帧，期间那条线路上的设备报「带宽计算中」。"""

    def test_line_recompute_is_framed_and_ends_with_the_same_result(self):
        framed = AdmissionService(self.defs, self.registry, self.topology, step_budget=2)
        self.place(key(0, 64, 0), BB8)
        self.place(key(0, 63, 0), TINY)
        for x in (1, 2, 3):
            self.place(key(x, 64, 0), GLASS)
        terminals = []
        for x in (1, 2, 3):
            terminals.extend([key(x, 63, 0), key(x, 64, -1), key(x, 64, 1), key(x, 65, 0)])
        for position in terminals:
            self.place(position, TERM)
        self.settle()

        self.assertFalse(framed.is_idle())
        pending = framed.status_of(self.registry.get(terminals[-1]).ref)
        self.assertEqual(pending.state, BandwidthState.UNKNOWN)
        self.assertIn(Reason.BANDWIDTH_UNKNOWN, pending.reason_codes())

        rounds = 0
        while not framed.is_idle():
            self.assertLessEqual(framed.advance(), 3 * 2)
            rounds += 1
            self.assertLess(rounds, 100)
        self.assertGreater(rounds, 1)
        for position in terminals:
            ref = self.registry.get(position).ref
            self.assertEqual(
                framed.status_of(ref).fingerprint(), self.admission.status_of(ref).fingerprint()
            )
        self.assertEqual(len(self.denied(terminals)), 4)


class TestDeterminism(unittest.TestCase):
    """13 §4.2：结果只取决于世界本身，与放置顺序无关。"""

    LAYOUT = [
        (key(0, 64, 0), BB8),
        (key(0, 63, 0), TINY),
        (key(1, 64, 0), GLASS),
        (key(2, 64, 0), GLASS),
        (key(0, 65, 0), DENSE),
        (key(1, 65, 0), DENSE),
        (key(2, 65, 0), DENSE),
    ] + [
        (position, TERM)
        for position in (
            key(1, 63, 0), key(1, 64, 1), key(1, 64, -1), key(2, 63, 0),
            key(2, 64, 1), key(2, 64, -1), key(3, 64, 0), key(1, 66, 0),
            key(2, 66, 0), key(3, 65, 0), key(0, 66, 0), key(3, 66, 0),
        )
    ]

    def build(self, order):
        defs = DefinitionSet()
        registry = DeviceRegistry(defs)
        topology = TopologyService(defs, registry, DeviceCatalog(defs))
        admission = AdmissionService(defs, registry, topology)
        for position, device_id in order:
            registry.register(position, device_id)
            topology.mark_dirty(position)
            topology.advance(3)  # 故意小步推进，让重建跨很多帧
        for _ in range(200):
            topology.advance(100000)
            if topology.is_idle():
                break
        out = {}
        for position, device_id in self.LAYOUT:
            if device_id != TERM:
                continue
            result = admission.status_of(registry.get(position).ref)
            out[position.as_key()] = (
                result.state,
                result.segment.as_tuple() if result.segment is not None else None,
            )
        tree = topology.snapshot().line_tree_of(key(0))
        return out, [item.as_key() for item in tree.order]

    def test_same_world_same_allocation_regardless_of_placement_order(self):
        forward = self.build(self.LAYOUT)
        backward = self.build(list(reversed(self.LAYOUT)))
        self.assertEqual(forward, backward)
        states = [state for state, _ in forward[0].values()]
        self.assertIn(BandwidthState.DENIED, states)
        self.assertIn(BandwidthState.GRANTED, states)


class TestPanels(AdmissionFixture):
    """面板挂在宿主光缆下面，需求计入宿主所在线路（13 §4.2、§7）。"""

    def test_panels_are_consumers_under_their_host(self):
        self.place(key(0), BB8)
        self.place(key(0, 63), TINY)
        host = self.place(
            key(1), GLASS, config={CABLE_PARTS_KEY: {"up": TERM_PART, "south": TERM_PART}}
        )
        self.settle()
        for face in ("up", "south"):
            result = self.admission.status_of_part(PartKey(host.node_key, face))
            self.assertEqual(result.state, BandwidthState.GRANTED)
            self.assertEqual(result.device_id, TERM_PART)
            self.assertEqual(result.demand_quarters, U)
            self.assertEqual(result.ref, host.ref)
            self.assertEqual(
                result.bandwidth_domain_id, self.topology.snapshot().line_of(host.node_key)
            )
        line = self.line_of(host.node_key)
        self.assertEqual(line.used[host.node_key], 2 * U)
        self.assertEqual(line.used_quarters, 2 * U)

    def test_panels_share_the_capacity_of_the_cables_above_them(self):
        """K1 挂 4 块、K2 挂 5 块，全都经过 K1（玻璃 8u）：K2 的最后一块被拒。"""
        self.place(key(0, 64, 0), BB32)
        self.place(key(0, 63, 0), TINY)
        k1 = self.place(
            key(1, 64, 0), GLASS,
            config={CABLE_PARTS_KEY: dict((face, TERM_PART) for face in ("up", "down", "north", "south"))},
        )
        k2 = self.place(
            key(2, 64, 0), GLASS,
            config={CABLE_PARTS_KEY: dict((face, TERM_PART) for face in ("up", "down", "north", "south", "east"))},
        )
        self.settle()
        results = [
            self.admission.status_of_part(PartKey(host.node_key, face))
            for host, faces in (
                (k1, ("up", "down", "north", "south")),
                (k2, ("up", "down", "north", "south", "east")),
            )
            for face in faces
        ]
        self.assertEqual(
            [result.state for result in results],
            [BandwidthState.GRANTED] * 8 + [BandwidthState.DENIED],
        )
        self.assertEqual(results[-1].node_key, PartKey(k2.node_key, "east"))
        self.assertEqual(results[-1].reasons[0].detail, "所在线路已满：末影玻璃光缆 8/8 u")
        self.assertEqual(results[-1].segment.segment_id, k1.node_key)

    def test_panel_on_a_cable_without_a_baseband_has_no_path(self):
        host = self.place(key(30), GLASS, config={CABLE_PARTS_KEY: {"up": TERM_PART}})
        self.place(key(31), TERM)
        self.settle()
        result = self.admission.status_of_part(PartKey(host.node_key, "up"))
        self.assertEqual(result.state, BandwidthState.DENIED)
        self.assertIn(Reason.NO_BANDWIDTH_PATH, result.reason_codes())

    def test_a_face_without_a_panel_reports_it_gone(self):
        host = self.place(key(30), GLASS)
        self.settle()
        result = self.admission.status_of_part(PartKey(host.node_key, "up"))
        self.assertEqual(result.state, BandwidthState.UNKNOWN)
        self.assertIn(Reason.DEVICE_GONE, result.reason_codes())


class TestReplacedInstance(AdmissionFixture):
    """同位置换方块后，缓存里那条判定属于已经不存在的实例。"""

    def test_status_never_returns_a_replaced_device_verdict(self):
        self.place(key(0), BB8)
        self.place(key(0, 65), TINY)
        terminal = self.place(key(0, 63), TERM)
        self.settle()
        self.assertEqual(self.admission.status_of(terminal.ref).demand_quarters, U)

        # 同一个位置换成内存槽（需求 2u）：代次变了，旧判定不得再返回。
        # 这里**不**推进拓扑，模拟 tick 中事件刚消费完、重建还没跑的那一瞬。
        memory = self.place(key(0, 63), MEMORY)
        result = self.admission.status_of(memory.ref)
        self.assertEqual(result.ref, memory.ref)
        self.assertEqual(result.device_id, MEMORY)
        self.assertEqual(result.demand_quarters, 2 * U)
        self.assertNotEqual(result.fingerprint(), self.admission.status_of(terminal.ref).fingerprint())


class TestDeviceStatus(AdmissionFixture):
    """02-08：三态、原因与只读性。"""

    def setUp(self):
        AdmissionFixture.setUp(self)
        # 一块 8 位基带，(0,65) 挂控制器，其余五个面留给被测设备
        self.place(key(0), BB8)
        self.place(key(0, 65), TINY)

    def test_granted_state(self):
        terminal = self.place(key(0, 63), TERM)
        self.settle()
        result = self.admission.status_of(terminal.ref)
        self.assertEqual(result.state, BandwidthState.GRANTED)
        self.assertTrue(result.ok)
        self.assertEqual(result.demand_quarters, U)
        self.assertEqual(result.allocated_quarters, U)
        self.assertEqual(result.segment.text(), "8位网络基带 1/8 u")

    def test_status_carries_the_numbers_the_ui_needs(self):
        terminal = self.place(key(0, 63), TERM)
        self.settle()
        payload = self.admission.status_of(terminal.ref).to_dict()
        for field in (
            "bandwidth_ok", "bandwidth_domain_id", "demand_quarters",
            "allocated_quarters", "segment", "reasons", "generation",
        ):
            self.assertIn(field, payload)
        self.assertEqual(payload["bandwidth_domain_id"], "line:0:0,64,0:down")
        self.assertEqual(payload["segment"]["capacity_quarters"], 8 * U)

    def test_no_path_is_denied_with_a_specific_reason(self):
        lonely = self.place(key(20), TERM)
        self.settle()
        result = self.admission.status_of(lonely.ref)
        self.assertEqual(result.state, BandwidthState.DENIED)
        self.assertFalse(result.ok)
        self.assertIn(Reason.NO_BANDWIDTH_PATH, result.reason_codes())
        self.assertIsNone(result.bandwidth_domain_id)

    def test_unconfigured_definition_is_unknown_and_not_charged(self):
        """R10：blocks.md 没给连接器的带宽，未知不等于 0。"""
        connector = self.place(key(0, 64, -1), CONNECTOR)
        self.settle()
        result = self.admission.status_of(connector.ref)
        self.assertEqual(result.state, BandwidthState.UNKNOWN)
        self.assertIsNone(result.ok)
        self.assertIn(Reason.DEFINITION_UNCONFIGURED, result.reason_codes())
        self.assertEqual(self.line_of(connector.node_key).used_quarters, 0)

    def test_unconfigured_capacity_on_the_path_is_unknown(self):
        definition = self.defs.get(BB16)
        original = definition.bandwidth.capacity_quarters
        try:
            definition.bandwidth.capacity_quarters = UNCONFIGURED
            self.place(key(1), BB16)
            terminal = self.place(key(2), TERM)
            self.settle()
            result = self.admission.status_of(terminal.ref)
        finally:
            definition.bandwidth.capacity_quarters = original
        self.assertEqual(result.state, BandwidthState.UNKNOWN)
        self.assertIn(Reason.BANDWIDTH_UNCONFIGURED, result.reason_codes())

    def test_carrier_and_core_do_not_compete_for_capacity(self):
        cable = self.place(key(0, 63), GLASS)
        self.place(key(0, 62), TERM)
        self.settle()
        baseband = self.admission.status_of(self.registry.get(key(0)).ref)
        controller = self.admission.status_of(self.registry.get(key(0, 65)).ref)
        carrier = self.admission.status_of(cable.ref)
        for result, role in (
            (baseband, BandwidthRole.CARRIER),
            (controller, BandwidthRole.CORE),
            (carrier, BandwidthRole.CARRIER),
        ):
            self.assertEqual(result.role, role)
            self.assertEqual(result.state, BandwidthState.GRANTED)
            self.assertEqual(result.demand_quarters, 0)
        self.assertEqual(self.line_of(cable.node_key).used_quarters, U)

    def test_energy_input_takes_no_bandwidth(self):
        """blocks.md：能源接入口不占带宽——是 0，不是未配置。"""
        energy = self.place(key(0, 64, 1), ENERGY)
        self.settle()
        result = self.admission.status_of(energy.ref)
        self.assertEqual(result.state, BandwidthState.GRANTED)
        self.assertEqual(result.demand_quarters, 0)

    def test_reading_status_has_no_side_effects(self):
        terminal = self.place(key(0, 63), TERM)
        self.settle()
        first = self.admission.status_of(terminal.ref)
        used = self.line_of(terminal.node_key).used_quarters
        second = self.admission.status_of(terminal.ref)
        self.assertEqual(first.fingerprint(), second.fingerprint())
        self.assertEqual(self.line_of(terminal.node_key).used_quarters, used)

    def test_status_survives_chunk_unload(self):
        """区块卸载（近似断电/暂停）不抹除仍然有效的负载判定。"""
        terminal = self.place(key(0, 63), TERM)
        self.settle()
        self.registry.mark_unloaded(terminal.node_key)
        self.topology.mark_dirty(terminal.node_key)
        self.settle()
        result = self.admission.status_of(terminal.ref)
        self.assertEqual(result.state, BandwidthState.GRANTED)
        self.assertTrue(result.ok)

    def test_bandwidth_is_judged_without_any_energy_input(self):
        """带宽判定与能源判定独立：本服务不读任何能源状态。"""
        report = self.admission.diagnostics()
        self.assertTrue(report["energy_independent"])
        self.assertNotIn(
            "energy",
            set(name for name in dir(self.admission) if not name.startswith("_")),
        )

    def test_gone_instance_is_unknown(self):
        terminal = self.place(key(0, 63), TERM)
        self.settle()
        ghost = DeviceRef(terminal.node_key, terminal.generation + 3)
        result = self.admission.status_of(ghost)
        self.assertEqual(result.state, BandwidthState.UNKNOWN)
        self.assertIn(Reason.DEVICE_GONE, result.reason_codes())

    def test_rebuild_makes_the_status_pending(self):
        terminal = self.place(key(0, 63), TERM)
        self.settle()
        self.topology.mark_dirty(key(0))
        self.topology.advance(1)
        result = self.admission.status_of(terminal.ref)
        self.assertEqual(result.state, BandwidthState.UNKNOWN)
        self.assertIn(Reason.REBUILDING, result.reason_codes())


class TestDeviceStatusComposition(AdmissionFixture):
    """设备状态栏的组合函数要报得出新原因与用量（`controller.compose_device_status`）。"""

    def compose(self, position):
        from endernet.server.network.controller import compose_device_status

        record = self.registry.get(position)
        return compose_device_status(
            record.ref, self.defs, self.registry, self.catalog, self.topology, self.admission
        )

    def test_a_full_line_is_explained_with_its_bottleneck(self):
        self.place(key(0, 64, 0), BB8)
        self.place(key(0, 63, 0), TINY)
        self.place(key(1, 64, 0), GLASS)
        terminals = [key(1, 63, 0), key(1, 64, -1), key(1, 64, 1), key(1, 65, 0), key(2, 64, 0)]
        for position in terminals[:4]:
            self.place(position, DRIVER, config={"disk_count": 8})  # 4 × 2u = 8u
        self.place(terminals[4], TERM)
        self.settle()
        status = self.compose(terminals[4])
        self.assertFalse(status.is_online)
        self.assertIs(status.bandwidth_ok, False)
        self.assertEqual(status.bandwidth_domain_id, "line:0:0,64,0:east")
        details = [reason.detail for reason in status.reasons if reason.code == Reason.BANDWIDTH_LINE_FULL]
        self.assertEqual(details, ["所在线路已满：末影玻璃光缆 8/8 u"])

    def test_a_core_violation_takes_the_whole_network_offline(self):
        from endernet.server.network.controller import RunState

        for x in range(8):
            self.place(key(x), BB8)
        self.place(key(0, 63), TINY)
        terminal = self.place(key(0, 65), TERM)
        self.settle()
        status = self.compose(terminal.node_key)
        self.assertFalse(status.is_online)
        self.assertEqual(status.run_state, RunState.BASEBAND_INVALID)
        self.assertIn(
            (Reason.BASEBAND_TOO_LARGE, "网络基带超出 7×7×7 的范围"),
            [(reason.code, reason.detail) for reason in status.reasons],
        )


class TestFramingInTheServer(unittest.TestCase):
    """分帧没算完的线路由服务端每帧接着算（`RuntimeManager.tick` → `advance`）。"""

    def test_server_ticks_finish_a_framed_line(self):
        from endernet.server.bootstrap import ServerBootstrap
        from endernet.server.runtime.events import NormalizedEvent

        server = ServerBootstrap()
        server.start(bind_engine=False)
        server.admission.step_budget = 1
        layout = [(key(0, 64, 0), BB8), (key(0, 63, 0), TINY)]
        layout += [(key(x, 64, 0), GLASS) for x in (1, 2, 3)]
        terminals = []
        for x in (1, 2, 3):
            terminals.extend([key(x, 63, 0), key(x, 64, -1), key(x, 64, 1)])
        layout += [(position, TERM) for position in terminals]
        for position, device_id in layout:
            server.push(NormalizedEvent.place(position, device_id))
        ticks = 0
        while ticks < 200:
            server.tick()
            ticks += 1
            if server.topology.is_idle() and server.admission.is_idle():
                break
        self.assertTrue(server.admission.is_idle())
        self.assertGreater(server.admission.stats["deferred"], 0)
        states = [
            server.admission.status_of(server.registry.get(position).ref).state
            for position in terminals
        ]
        self.assertEqual(states, [BandwidthState.GRANTED] * 8 + [BandwidthState.DENIED])
        server.stop()


class TestNotifications(AdmissionFixture):
    """02-08：只读状态变化通知。"""

    def test_change_is_emitted_only_when_the_judgement_changes(self):
        self.place(key(0), BB8)
        self.place(key(0, 65), TINY)
        terminal = self.place(key(0, 63), TERM)
        self.settle()
        del self.changes[:]

        # 一次不影响该设备判定的图变化：另一张网上新增一台设备
        self.place(key(20), BB8)
        self.place(key(20, 65), BUS)
        self.settle()
        self.assertEqual(
            [
                change for change in self.changes
                if change.after is not None and change.after.node_key == terminal.node_key
            ],
            [],
            "判定没变就不应推送",
        )

        # 该设备所在分量被标脏 → 判定变为「待确认」
        self.topology.mark_dirty(key(0))
        self.topology.advance(1)
        own = [
            change
            for change in self.changes
            if change.after is not None and change.after.node_key == terminal.node_key
        ]
        self.assertTrue(own)
        self.assertEqual(own[-1].after.state, BandwidthState.UNKNOWN)

    def test_notification_carries_a_protocol_object(self):
        self.place(key(0), BB8)
        self.place(key(0, 65), TINY)
        self.place(key(0, 63), TERM)
        self.settle()
        del self.changes[:]
        self.place(key(0, 64, -1), BUS)
        self.settle()

        self.assertTrue(self.changes)
        payload = self.changes[-1].notification.to_dict()
        self.assertEqual(payload["kind"], "device_status_changed")
        self.assertIn("bandwidth_ok", payload["status"])


if __name__ == "__main__":
    unittest.main()
