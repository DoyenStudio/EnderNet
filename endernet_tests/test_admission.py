# coding=utf-8
"""任务 02-05 与 02-08 的离线验收：负载域容量、准入与设备带宽状态。

覆盖 02 验收清单里与数值有关的部分：

- 按选定负载策略验证 8/16/32u 边界、插拔介质引发的动态负载变化、不同设备优先级。
- 一个基带域满载时，不误报同控制器另一独立域的设备；
  已获准设备与新请求设备的带宽显示符合选定满载规则。

**未定玩法一律是具名配置点。** 容量合成、环路、多接入、取整（R02）
与恰好满载的显示口径（R13）都还没有定稿，本文件验证的是**机制**，
并用 `TestPolicyGuard` 锁住「不能宣布数值已验收」这件事。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import DefinitionSet
from endernet.common.identity import NodeKey
from endernet.common.protocol import Reason
from endernet.server.devices.catalog import DeviceCatalog
from endernet.server.devices.registry import DeviceRegistry
from endernet.server.network.admission import (
    AdmissionService,
    BandwidthPolicy,
    BandwidthState,
    CapacityMode,
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
ENERGY = "endernet:energy_input_interface"

#: 1u = 4 个四分之一 u
U = 4


def key(x, y=64, z=0, dimension=0):
    return NodeKey(dimension, x, y, z)


def around(x, y=64, z=0):
    """位置 (x, y, z) 六个面的邻居。"""
    return [
        key(x, y + 1, z),
        key(x, y - 1, z),
        key(x, y, z - 1),
        key(x, y, z + 1),
        key(x - 1, y, z),
        key(x + 1, y, z),
    ]


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

    def status(self, position):
        record = self.registry.get(position)
        self.assertIsNotNone(record, "位置 %r 没有设备" % (position.as_key(),))
        return self.admission.status_of(record.ref)

    def domain_of(self, position):
        domain_ids = self.topology.snapshot().domain_ids_of(position)
        self.assertEqual(len(domain_ids), 1, "该位置应恰好属于一个负载域")
        return self.admission.domain(domain_ids[0])


class TestPolicyGuard(unittest.TestCase):
    """R02/R13 未定：策略字段必须是可被点名的配置点。"""

    def test_unresolved_fields_are_enumerable(self):
        rules = set(rule for _, rule, _ in UNRESOLVED_FIELDS)
        self.assertIn("R02", rules)
        self.assertIn("R13", rules)

    def test_report_lists_every_unresolved_field(self):
        report = BandwidthPolicy().unresolved_report()
        names = set(item["field"] for item in report)
        self.assertEqual(names, set(field for field, _, _ in UNRESOLVED_FIELDS))

    def test_assert_settled_refuses_to_declare_victory(self):
        """规则未定稿前，不能宣布带宽数值已验收。"""
        with self.assertRaises(AssertionError):
            BandwidthPolicy().assert_settled()

    def test_policy_is_in_the_diagnostics(self):
        report = BandwidthPolicy().describe()
        self.assertEqual(report["capacity_mode"], CapacityMode.SUM)
        self.assertEqual(report["saturation"], SaturationMode.GRANTED_IS_OK)


class TestCapacityValues(AdmissionFixture):
    """02-05：容量来自基带定义，不按名称猜测。"""

    def _single_baseband_capacity(self, device_id):
        self.place(key(0), device_id)
        self.place(key(0, 65), TINY)
        self.settle()
        return self.domain_of(key(0)).capacity_quarters

    def test_single_segment_capacities(self):
        self.assertEqual(self._single_baseband_capacity(BB8), 8 * U)
        self.assertEqual(self._single_baseband_capacity(BB16), 16 * U)
        self.assertEqual(self._single_baseband_capacity(BB32), 32 * U)

    def test_same_capacity_domain_depends_on_the_configured_mode(self):
        """同容量域的容量合成（R02）仍由策略决定，不是一个写死的答案。

        混合容量**已不再是本层的题目**：blocks.md 禁止异容量基带直接连通，
        因此一个负载域内的基带容量必然一致（见
        `test_mixed_basebands_end_up_in_separate_domains`）。但「同容量域
        容量怎么算」仍未定案，`capacity_mode` 依旧是具名配置点。
        """
        self.place(key(0), BB8)
        self.place(key(1), BB8)
        self.place(key(0, 65), TINY)
        self.settle()

        self.policy.capacity_mode = CapacityMode.MIN
        self.admission.refresh()
        minimum = self.domain_of(key(0)).capacity_quarters

        self.policy.capacity_mode = CapacityMode.MAX
        self.admission.refresh()
        maximum = self.domain_of(key(0)).capacity_quarters

        self.policy.capacity_mode = CapacityMode.SUM
        self.admission.refresh()
        total = self.domain_of(key(0)).capacity_quarters

        # 同容量域下 MIN 与 MAX 必然相等，只有 SUM 与之区分——这不代表
        # 两个配置项等价，而是混合容量已被连接规则挡在域之外。
        self.assertEqual(minimum, 8 * U)
        self.assertEqual(maximum, 8 * U)
        self.assertEqual(total, 16 * U)

    def test_mixed_basebands_end_up_in_separate_domains(self):
        """异容量基带相邻也不合并：两条互不相干的网络带，各自算各自的容量。"""
        self.place(key(0), BB8)
        self.place(key(1), BB16)
        self.settle()

        ids_8 = self.topology.snapshot().domain_ids_of(key(0))
        ids_16 = self.topology.snapshot().domain_ids_of(key(1))
        self.assertEqual(len(ids_8), 1, "8 位基带应恰好属于一个域")
        self.assertEqual(len(ids_16), 1, "16 位基带应恰好属于一个域")
        self.assertNotEqual(ids_8[0], ids_16[0], "异容量基带不应归入同一个负载域")
        self.assertEqual(self.admission.domain(ids_8[0]).capacity_quarters, 8 * U)
        self.assertEqual(self.admission.domain(ids_16[0]).capacity_quarters, 16 * U)

    def test_loop_segments_still_count_under_the_test_policy(self):
        """环路下的容量是 R02 待定项；测试默认逐个计段。"""
        for position in (key(0, 64, 0), key(1, 64, 0), key(1, 64, 1), key(0, 64, 1)):
            self.place(position, BB8)
        self.place(key(0, 64, -1), TINY)
        self.settle()
        self.assertEqual(self.domain_of(key(0)).capacity_quarters, 4 * 8 * U)


class TestCapacityBoundary(AdmissionFixture):
    """按测试策略验证 8u 边界。"""

    #: 两节 8u 基带取最窄 → 8u 容量，正好够 8 台 1u 设备。
    policy_kwargs = {"capacity_mode": CapacityMode.MIN}

    def _faces(self):
        """两节基带外围共 10 个可用面。"""
        occupied = set([key(0).as_key(), key(1).as_key()])
        return [
            position
            for position in around(0) + around(1)
            if position.as_key() not in occupied
        ]

    def _fill(self, count, device_id=TERM):
        faces = self._faces()
        self.place(key(0), BB8)
        self.place(key(1), BB8)
        record = self.place(faces[0], TINY)
        placed = [record]
        for position in faces[1 : 1 + count]:
            placed.append(self.place(position, device_id))
        self.settle()
        return placed

    def test_exactly_full_is_still_ok(self):
        """恰好满载：已获准的设备带宽仍然是 OK（R13 的建议口径）。"""
        records = self._fill(8)
        self.settle()
        domain = self.domain_of(key(0))
        self.assertEqual(domain.capacity_quarters, 8 * U)
        self.assertEqual(domain.used_quarters, 8 * U)
        self.assertEqual(domain.denied, ())

        for record in records[1:]:
            result = self.admission.status_of(record.ref)
            self.assertEqual(result.state, BandwidthState.GRANTED)
            self.assertTrue(result.ok, "已获准且恰好满载应为 OK")
            self.assertEqual(result.allocated_quarters, U)

    def test_one_more_device_is_denied_without_disturbing_the_others(self):
        records = self._fill(8)
        ninth = self.place(self._faces()[9], TERM)
        self.settle()

        denied = self.admission.status_of(ninth.ref)
        self.assertEqual(denied.state, BandwidthState.DENIED)
        self.assertFalse(denied.ok)
        self.assertEqual(denied.allocated_quarters, 0)
        self.assertIn(Reason.BANDWIDTH_DENIED, denied.reason_codes())
        self.assertIn(Reason.BANDWIDTH_OVERLOADED, denied.reason_codes())

        for record in records[1:]:
            result = self.admission.status_of(record.ref)
            self.assertEqual(result.state, BandwidthState.GRANTED)
            self.assertTrue(result.ok, "已被挤出的设备不应受新请求影响")

    def test_single_demand_beyond_domain_capacity_is_not_reported_as_full(self):
        """单机需求超过整个域容量时，域**还是空的**——不能报「负载域已满载」。

        两种情形都该拒绝，但原因必须分开说：一种是被别的设备挤掉，
        另一种是自己一台就放不下。界面靠这条原因解释设备为什么离线，
        把空域说成「已满载」会让玩家去查一个根本不存在的占用。
        """
        self.place(key(0), BB8)  # 8u 域 = 32 个四分之一 u
        self.place(key(0, 65), DRIVER, config={"disk_count": 40})  # 40q = 10u
        self.settle()

        result = self.status(key(0, 65))
        self.assertEqual(result.state, BandwidthState.DENIED)
        self.assertFalse(result.ok)
        self.assertEqual(result.demand_quarters, 40)
        self.assertEqual(result.allocated_quarters, 0)

        domain = self.domain_of(key(0))
        self.assertEqual(domain.capacity_quarters, 8 * U)
        self.assertEqual(domain.used_quarters, 0, "被拒的设备不占额度")
        self.assertFalse(
            domain.used_quarters >= domain.capacity_quarters, "前提：这个域并没有满"
        )
        self.assertEqual(list(domain.denied), [key(0, 65)])

        codes = result.reason_codes()
        self.assertIn(Reason.BANDWIDTH_DENIED, codes)
        self.assertIn(Reason.BANDWIDTH_DEMAND_EXCEEDS_DOMAIN, codes)
        self.assertNotIn(
            Reason.BANDWIDTH_OVERLOADED, codes, "域没满，不该说「已满载」"
        )

    def test_not_full_but_insufficient_room_is_not_reported_as_full(self):
        """域没满、单机需求也没超过总容量，只是**剩余额度不够**。

        这时也不该说「负载域已满载」——域还有空余额度。三条拒绝路径
        （一台放不下 / 挤不进来 / 剩余不够）都成立，区别只在解释，
        因此各有一条用例把它们钉住。
        """
        self.place(key(0), BB8)  # 8u 域 = 32 个四分之一 u
        self.place(key(1), BB8)
        # 占用 24q（终端 6 台 × 1u），留 8q
        faces = [key(0, 65), key(0, 63), key(0, 64, -1), key(0, 64, 1), key(-1, 64), key(2, 64)]
        for position in faces:
            self.place(position, TERM)
        # 驱动器要 16q：16 <= 32（没超过总容量），但 24 + 16 > 32
        self.place(key(1, 65), DRIVER, config={"disk_count": 16})
        self.settle()

        domain = self.domain_of(key(0))
        self.assertEqual(domain.capacity_quarters, 8 * U)
        self.assertEqual(domain.used_quarters, 6 * U, "前提：域被占用了 24q，还没满")

        result = self.status(key(1, 65))
        self.assertEqual(result.state, BandwidthState.DENIED)
        self.assertEqual(result.demand_quarters, 16)

        codes = result.reason_codes()
        self.assertIn(Reason.BANDWIDTH_DENIED, codes)
        self.assertNotIn(Reason.BANDWIDTH_DEMAND_EXCEEDS_DOMAIN, codes, "16 没超过总容量 32")
        self.assertNotIn(Reason.BANDWIDTH_OVERLOADED, codes, "域只用了 24/32，没满载")
        # 第一条原因必须自带这两个数，否则玩家无从判断。
        detail = result.reasons[0].detail
        self.assertIn("16", detail)
        self.assertIn("24/32", detail)

    def test_status_carries_the_numbers_the_ui_needs(self):
        records = self._fill(2)
        result = self.admission.status_of(records[1].ref)
        payload = result.to_dict()
        self.assertEqual(payload["bandwidth_ok"], True)
        self.assertEqual(payload["demand_quarters"], U)
        self.assertEqual(payload["allocated_quarters"], U)
        self.assertEqual(payload["domain_capacity_quarters"], 8 * U)
        self.assertEqual(payload["domain_used_quarters"], 2 * U)
        self.assertEqual(payload["bandwidth_domain_id"], result.bandwidth_domain_id)
        self.assertIsNotNone(payload["bandwidth_domain_id"])
        self.assertEqual(payload["generation"], records[1].generation)


class TestDisjointDomains(AdmissionFixture):
    """一个域满载不误报同一控制器另一独立域上的设备。"""

    policy_kwargs = {"capacity_mode": CapacityMode.MIN}

    def setUp(self):
        AdmissionFixture.setUp(self)
        # 控制器 (0,64,0)；上下各一段互不相连的 8u 基带
        self.place(key(0, 64, 0), TINY)
        self.place(key(0, 65, 0), BB8)
        self.place(key(0, 63, 0), BB8)

    def test_full_domain_does_not_affect_the_other(self):
        """上面那一段基带被填满，不会误报下面那一段上的设备。"""
        # 上面这段的可用面：4 个 2u 内存槽正好填满 8u
        filler_faces = [key(0, 66, 0), key(0, 65, -1), key(0, 65, 1), key(-1, 65, 0)]
        for position in filler_faces:
            self.place(position, MEMORY)
        lower = self.place(key(0, 62, 0), TERM)
        self.settle()

        upper_domain = self.domain_of(key(0, 65, 0))
        self.assertEqual(upper_domain.capacity_quarters, 8 * U)
        self.assertEqual(upper_domain.used_quarters, 8 * U)
        self.assertEqual(upper_domain.denied, ())

        # 上面这个域已经一点余量都没有，新请求必须被拒
        extra = self.place(key(1, 65, 0), TERM)
        self.settle()
        extra_result = self.admission.status_of(extra.ref)
        self.assertEqual(extra_result.state, BandwidthState.DENIED)
        self.assertFalse(extra_result.ok)

        # 关键断言：另一个域的设备完全不受影响
        lower_result = self.admission.status_of(lower.ref)
        self.assertEqual(lower_result.state, BandwidthState.GRANTED)
        self.assertTrue(lower_result.ok)
        self.assertNotEqual(
            lower_result.bandwidth_domain_id, extra_result.bandwidth_domain_id
        )
        self.assertEqual(lower_result.domain_used_quarters, U)

    def test_controller_bridging_two_domains_is_not_flagged_as_undecided(self):
        """控制器同时属于两个域是 blocks.md 明确规定的情形，不是待定项。"""
        self.settle()
        controller = self.status(key(0, 64, 0))
        self.assertEqual(controller.state, BandwidthState.GRANTED)
        self.assertTrue(controller.ok)
        self.assertEqual(len(controller.domain_ids), 2)
        # 两个域的预算不合并成一个池
        self.assertEqual(controller.notes[0][:0], "")
        self.assertIn("不合并", "".join(controller.notes))


class TestMultiAccess(AdmissionFixture):
    """多接入（R02）未定：不擅自替设备选一个域。"""

    def test_device_between_two_domains_reports_unknown(self):
        """一台设备同时贴着两段互不相连的基带：归属待定。"""
        self.place(key(0), BB8)
        self.place(key(2), BB8)
        self.place(key(0, 65), TINY)
        middle = self.place(key(1), TERM)  # 左右各一段基带，互不相连
        self.settle()

        result = self.admission.status_of(middle.ref)
        self.assertEqual(len(result.domain_ids), 2)
        self.assertEqual(result.state, BandwidthState.UNKNOWN)
        self.assertIsNone(result.ok, "归属未定时不能声称 OK，也不能声称未满足")
        self.assertIn(Reason.MULTI_DOMAIN_DEVICE, result.reason_codes())


class TestReplacedInstance(AdmissionFixture):
    """同位置换方块后，缓存里那条判定属于已经不存在的实例。"""

    def test_status_never_returns_a_replaced_device_verdict(self):
        self.place(key(0), BB8)
        self.place(key(0, 65), TINY)
        terminal = self.place(key(0, 63), TERM)
        self.settle()
        self.assertEqual(self.admission.status_of(terminal.ref).demand_quarters, U)

        # 同一个位置换成内存槽（需求 2u）：代次变了，旧判定不得再返回。
        # 这里**不**推进拓扑，模拟 tick 中事件刚消费完、重建还没跑的那一瞬——
        # 那正是处理设备变化的服务读取状态的时刻。
        memory = self.place(key(0, 63), MEMORY)
        result = self.admission.status_of(memory.ref)
        self.assertEqual(result.ref, memory.ref)
        self.assertEqual(result.device_id, MEMORY)
        self.assertEqual(result.demand_quarters, 2 * U)
        self.assertNotEqual(result.fingerprint(), self.admission.status_of(terminal.ref).fingerprint())


class TestMultiDomainCharging(AdmissionFixture):
    """多接入（R02）：归属未定的设备不得占用任何域的额度。"""

    def setUp(self):
        AdmissionFixture.setUp(self)
        # 一段设备夹在两段互不相连的基带之间
        self.place(key(0), BB8)
        self.place(key(2), BB8)
        self.place(key(0, 65), TINY)
        self.place(key(2, 65), TINY)
        self.middle = self.place(key(1), MEMORY)

    def test_unresolved_device_is_not_charged_to_either_domain(self):
        self.settle()
        domain_ids = self.topology.snapshot().domain_ids_of(key(1))
        self.assertEqual(len(domain_ids), 2)

        for domain_id in domain_ids:
            domain = self.admission.domain(domain_id)
            self.assertEqual(domain.used_quarters, 0, "未定归属的设备不应占用额度")
            self.assertNotIn(key(1), domain.granted)
            self.assertIn(key(1), domain.unresolved)

        result = self.admission.status_of(self.middle.ref)
        self.assertEqual(result.state, BandwidthState.UNKNOWN)
        self.assertEqual(result.allocated_quarters, 0)

    def test_grants_and_usage_stay_consistent(self):
        """`used` 必须等于已获准设备的分配量之和。"""
        self.settle()
        for domain_id in self.topology.snapshot().domain_ids_of(key(1)):
            domain = self.admission.domain(domain_id)
            total = sum(
                self.admission.demand_of(key)
                for key in domain.granted
            )
            self.assertEqual(domain.used_quarters, total)

    def test_resolving_the_rule_charges_exactly_one_domain(self):
        """配好 R02 的多接入策略后，只算到被选中的那个域。"""
        self.admission.policy = BandwidthPolicy(
            multi_access=lambda key, domain_ids: sorted(domain_ids)[0]
        )
        self.admission.refresh()
        self.settle()

        domain_ids = sorted(self.topology.snapshot().domain_ids_of(key(1)))
        self.assertEqual(self.admission.domain(domain_ids[0]).used_quarters, 2 * U)
        self.assertEqual(self.admission.domain(domain_ids[1]).used_quarters, 0)

        result = self.admission.status_of(self.middle.ref)
        self.assertEqual(result.state, BandwidthState.GRANTED)
        self.assertEqual(result.bandwidth_domain_id, domain_ids[0])


class TestDomainRename(AdmissionFixture):
    """负载域编号由基带位置推导：改名不得清空「先到先得」的顺序。"""

    policy_kwargs = {"capacity_mode": CapacityMode.MIN}

    def test_renaming_a_domain_does_not_reset_arrival_order(self):
        # 三节 8u 基带（取最窄仍是 8u）留出足够的面：
        # (0,65) 给控制器，8 台终端占掉 8u，(-1,64) 预留给后面新加的基带。
        for x in range(3):
            self.place(key(x), BB8)
        self.place(key(0, 65), TINY)
        records = [
            self.place(position, TERM)
            for position in (
                key(0, 63),
                key(0, 64, -1),
                key(0, 64, 1),
                key(1, 65),
                key(1, 63),
                key(1, 64, -1),
                key(1, 64, 1),
                key(2, 65),
            )
        ]
        self.settle()

        before = self.domain_of(key(0)).domain_id
        self.assertEqual(self.domain_of(key(0)).used_quarters, 8 * U)

        # 同一批里往更低坐标加一段基带（域编号会变）并放第九台设备
        self.place(key(-1), BB8)
        ninth = self.place(key(-1, 65), TERM)
        self.settle()

        self.assertNotEqual(
            self.domain_of(key(0)).domain_id, before, "该场景应当真的触发了改名"
        )
        self.assertEqual(
            self.admission.status_of(ninth.ref).state, BandwidthState.DENIED
        )
        for record in records:
            self.assertTrue(
                self.admission.status_of(record.ref).ok,
                "域改名不得让既有设备丢失额度",
            )


class TestDynamicLoad(AdmissionFixture):
    """插拔介质引发的动态负载变化。"""

    policy_kwargs = {"capacity_mode": CapacityMode.MIN}

    def setUp(self):
        AdmissionFixture.setUp(self)
        self.place(key(0), BB8)
        self.place(key(1), BB8)
        self.place(key(1, 65), TINY)

    def test_disk_count_drives_demand(self):
        driver = self.place(key(0, 65), DRIVER, config={"disk_count": 0})
        self.settle()
        self.assertEqual(self.admission.demand_of(driver.node_key), 0)

        driver.config["disk_count"] = 4  # 4 块硬盘 = 1u
        self.admission.note_device_load_changed(driver.node_key)
        self.assertEqual(self.admission.demand_of(driver.node_key), U)

        driver.config["disk_count"] = 8  # 8 块硬盘 = 2u
        self.admission.note_device_load_changed(driver.node_key)
        self.assertEqual(self.admission.demand_of(driver.node_key), 2 * U)

    def test_inserting_disks_can_push_a_device_out(self):
        driver = self.place(key(0, 65), DRIVER, config={"disk_count": 0})
        for position in around(0)[2:6] + around(1)[2:6]:
            if position.as_key() in (key(0).as_key(), key(1).as_key()):
                continue
            self.place(position, TERM)
        self.settle()

        before = self.admission.status_of(driver.ref)
        self.assertEqual(before.state, BandwidthState.GRANTED)
        used_before = self.domain_of(key(0)).used_quarters

        driver.config["disk_count"] = 8  # 插入 8 块硬盘 → 需求 2u
        self.admission.note_device_load_changed(driver.node_key)
        self.settle()

        after = self.admission.status_of(driver.ref)
        self.assertEqual(after.demand_quarters, 2 * U)
        self.assertGreater(
            self.domain_of(key(0)).used_quarters - used_before, 0,
            "插盘后域占用必须真的变大",
        )
        self.assertIn(after.state, (BandwidthState.GRANTED, BandwidthState.DENIED))

    def test_removing_disks_frees_capacity(self):
        driver = self.place(key(0, 65), DRIVER, config={"disk_count": 8})
        self.settle()
        self.assertEqual(self.domain_of(key(0)).used_quarters, 2 * U)

        driver.config["disk_count"] = 0
        self.admission.note_device_load_changed(driver.node_key)
        self.assertEqual(self.domain_of(key(0)).used_quarters, 0)


class TestPriority(AdmissionFixture):
    """容量不足时的设备优先级（R02 的配置点）。"""

    policy_kwargs = {
        "capacity_mode": CapacityMode.MIN,
        "priorities": {DRIVER: -1, TERM: 0},
    }

    def _fit_eight_terminals(self):
        """两节基带共 10 个空面，放 1 控制器 + 8 终端正好 8u。"""
        occupied = set([key(0).as_key(), key(1).as_key()])
        faces = [
            position
            for position in around(0) + around(1)
            if position.as_key() not in occupied
        ]
        self.place(key(0), BB8)
        self.place(key(1), BB8)
        self.place(faces[0], TINY)
        terminals = [self.place(position, TERM) for position in faces[1:9]]
        self.settle()
        return faces, terminals

    def test_priority_overtakes_arrival_order(self):
        faces, terminals = self._fit_eight_terminals()
        self.assertEqual(self.domain_of(key(0)).used_quarters, 8 * U)

        # 高优先级设备后到，仍应挤掉一个低优先级设备
        driver = self.place(faces[9], DRIVER, config={"disk_count": 4})
        self.settle()

        driver_result = self.admission.status_of(driver.ref)
        self.assertEqual(driver_result.state, BandwidthState.GRANTED)
        self.assertTrue(driver_result.ok)

        denied = [
            record
            for record in terminals
            if self.admission.status_of(record.ref).state == BandwidthState.DENIED
        ]
        self.assertEqual(len(denied), 1, "应有一个低优先级设备被挤出去")

    def test_without_priorities_it_is_first_come_first_served(self):
        policy = BandwidthPolicy(capacity_mode=CapacityMode.MIN)
        self.admission.policy = policy
        faces, terminals = self._fit_eight_terminals()
        self.admission.refresh()

        driver = self.place(faces[9], DRIVER, config={"disk_count": 4})
        self.settle()
        self.assertEqual(
            self.admission.status_of(driver.ref).state, BandwidthState.DENIED
        )
        for record in terminals:
            self.assertTrue(self.admission.status_of(record.ref).ok)


class TestSaturationMode(AdmissionFixture):
    """恰好满载的显示口径（R13）。"""

    policy_kwargs = {
        "capacity_mode": CapacityMode.MIN,
        "saturation": SaturationMode.REQUIRE_HEADROOM,
    }

    def test_require_headroom_flips_a_granted_device(self):
        occupied = set([key(0).as_key(), key(1).as_key()])
        faces = [
            position
            for position in around(0) + around(1)
            if position.as_key() not in occupied
        ]
        self.place(key(0), BB8)
        self.place(key(1), BB8)
        self.place(faces[0], TINY)
        last = None
        for position in faces[1:9]:
            last = self.place(position, TERM)
        self.settle()

        result = self.admission.status_of(last.ref)
        self.assertEqual(result.state, BandwidthState.GRANTED)
        self.assertFalse(result.ok, "该策略下满载即不再视为 OK")


class TestDeviceStatus(AdmissionFixture):
    """02-08：三态、原因与只读性。"""

    def setUp(self):
        AdmissionFixture.setUp(self)
        # 一段 8u 基带，(0,65) 挂控制器，其余五个面留给被测设备
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

    def test_no_domain_is_denied_with_a_specific_reason(self):
        lonely = self.place(key(20), TERM)
        self.settle()
        result = self.admission.status_of(lonely.ref)
        self.assertEqual(result.state, BandwidthState.DENIED)
        self.assertFalse(result.ok)
        self.assertIn(Reason.NO_BANDWIDTH_DOMAIN, result.reason_codes())
        self.assertIsNone(result.bandwidth_domain_id)

    def test_unconfigured_definition_is_unknown_and_not_charged(self):
        """R10：blocks.md 没给连接器的带宽，未知不等于 0。"""
        connector = self.place(key(0, 64, -1), CONNECTOR)
        self.settle()
        result = self.admission.status_of(connector.ref)
        self.assertEqual(result.state, BandwidthState.UNKNOWN)
        self.assertIsNone(result.ok)
        self.assertIn(Reason.DEFINITION_UNCONFIGURED, result.reason_codes())
        # 它没有被算进占用，所以域占用是已知部分的下界
        self.assertEqual(self.domain_of(key(0)).used_quarters, 0)

    def test_carrier_and_core_do_not_compete_for_capacity(self):
        self.settle()
        baseband = self.admission.status_of(self.registry.get(key(0)).ref)
        controller = self.admission.status_of(self.registry.get(key(0, 65)).ref)
        self.assertEqual(baseband.state, BandwidthState.GRANTED)
        self.assertEqual(controller.state, BandwidthState.GRANTED)
        self.assertEqual(baseband.demand_quarters, 0)
        self.assertEqual(self.domain_of(key(0)).used_quarters, 0)

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
        used = self.domain_of(key(0)).used_quarters
        second = self.admission.status_of(terminal.ref)
        self.assertEqual(first.fingerprint(), second.fingerprint())
        self.assertEqual(self.domain_of(key(0)).used_quarters, used)

    def test_status_survives_chunk_unload(self):
        """区块卸载（近似断电/暂停）不抹除仍然有效的负载判定。"""
        terminal = self.place(key(0, 63), TERM)
        self.settle()
        self.assertEqual(
            self.admission.status_of(terminal.ref).state, BandwidthState.GRANTED
        )

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
        from endernet.common.identity import DeviceRef

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


class TestNotifications(AdmissionFixture):
    """02-08：只读状态变化通知。"""

    def test_change_is_emitted_only_when_the_judgement_changes(self):
        self.place(key(0), BB8)
        self.place(key(0, 65), TINY)
        terminal = self.place(key(0, 63), TERM)
        self.settle()
        del self.changes[:]

        # 一次不影响该设备判定的图变化：另一段基带上新增一台设备
        self.place(key(20), BB8)
        self.place(key(20, 65), BUS)
        self.settle()
        self.assertEqual(
            [change for change in self.changes if change.after.node_key == terminal.node_key],
            [],
            "判定没变就不应推送",
        )

        # 该设备所在分量被标脏 → 判定变为「待确认」
        self.topology.mark_dirty(key(0))
        self.topology.advance(1)
        own = [
            change
            for change in self.changes
            if change.after.node_key == terminal.node_key
        ]
        self.assertTrue(own)
        self.assertEqual(own[-1].after.state, BandwidthState.UNKNOWN)

    def test_notification_carries_a_protocol_object(self):
        self.place(key(0), BB8)
        self.place(key(0, 65), TINY)
        self.place(key(0, 63), TERM)
        self.settle()
        del self.changes[:]
        # 新增设备会把该基带域重新分配，终端会被重新判定
        self.place(key(0, 64, -1), BUS)
        self.settle()

        self.assertTrue(self.changes)
        payload = self.changes[-1].notification.to_dict()
        self.assertEqual(payload["kind"], "device_status_changed")
        self.assertIn("bandwidth_ok", payload["status"])


if __name__ == "__main__":
    unittest.main()
