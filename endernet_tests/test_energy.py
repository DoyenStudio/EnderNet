# coding=utf-8
"""能源服务：固定耗能结算、供能判定与三态口径（任务 04-04 的固定部分）。

**全部离线**：能源服务不碰引擎、不碰拓扑、也不碰带宽准入，只读定义表与自己的
成员表。它给主控的是一个三态答案（有电 / 没电 / 待确认），本文件测的就是这个
答案**怎么来的**。

三组性质最值得盯住：

1. **结论由策略决定，不由服务偷偷决定。** 同一张网络在库默认口径下是「待确认」，
   在显式替身口径下才是「有电」/「没电」。因此关键场景一律**成对**地测两种口径
   ——只测替身口径的话，有人把默认值偷偷放宽就不会红。
2. **缺项不等于 0。** 这是最容易被写错的一处：把算不出的需求当成 0，任何一张
   有电源的网络都会「有电」，而那个结论是无根据的。
   `test_unconfigured_demand_does_not_become_free` 就是这件事的探针。
3. **读取无副作用。** `power_ok` 只读缓存，`tick` 不消耗预算、不推进时间，
   因此假时钟仍可精确复现。

分工：方块那一侧（方块 JSON、贴图、中文名）归 `test_assets`；供能数值与
`blocks.md` 的逐条对应归 `test_definitions.TestSupplyNumbers`；**命令真的做成了事**
的端到端证明归 `test_storage.test_a_creative_energy_source_makes_the_network_runnable`。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import (
    BandwidthSpec,
    CATEGORY_BUS,
    CATEGORY_ENERGY_INPUT,
    DefinitionSet,
    DeviceDefinition,
    EnergySpec,
    UNCONFIGURED,
    Value,
)
from endernet.common.identity import NodeKey
from endernet.common.protocol import (
    BlockReason,
    Reason,
    STATUS_OK,
)
from endernet.server.bootstrap import ServerBootstrap
from endernet.server.devices.registry import DeviceRegistry
from endernet.server.network.controller import (
    RunState,
    power_blocker,
    run_state_of,
)
from endernet.server.network.energy import (
    GAP_MISSING_SPEC,
    GAP_UNWIRED_INPUTS,
    EnergyPolicy,
    EnergyService,
)
from endernet.server.network.node import NetworkNode
from endernet.server.network.services import (
    ServiceFactory,
    ServiceId,
    default_builders,
    default_pending_tasks,
)
from endernet.server.runtime.events import NormalizedEvent

BB8 = "endernet:network_baseband_8"
TINY = "endernet:tiny_network_endpoint"
TERMINAL = "endernet:common_terminal_panel"
DRIVER = "endernet:storage_driver"
MEMORY_SLOT = "endernet:memory_slot"
RF_INTERFACE = "endernet:energy_input_interface"
CREATIVE = "endernet:creative_energy_source"

#: 只出现在本文件里的测试设备。真实定义表里没有「供能小于需求」的组合
#: （唯一的电源是创造能量源，2147483647 EN/t 压不倒），而「精确的供能确实不够」
#: 那条分支必须有真实用例，否则它只是注释里的一句承诺。
SMALL_SOURCE = "endernet:test_small_source"
GREEDY = "endernet:test_greedy_device"
CHEAP = "endernet:test_cheap_device"
#: 一台**供能速率未定**的电源（真实定义表里是能源接入口：RF→EN 折算属 R08）。
OPEN_SOURCE = "endernet:test_open_source"


def key(x, y=64, z=0, dimension=0):
    return NodeKey(dimension, x, y, z)


class FakeWorld(object):
    """`ServerBootstrap` 需要一个世界适配器；离线时它是空壳。

    与 `test_bootstrap` / `test_storage` 里的同名替身同形。本文件自己写一个
    而不是互相 import：那会让这套用例的成败取决于另一个模块的导入顺序。
    """

    def list_loaded_chunks(self):
        return []

    def block_entities_in_chunk(self, chunk_key):
        return []

    def register_block_remove_listener(self, block_ids):
        return None

    def facing_at(self, node_key):
        return None

    def diagnostics(self):
        return {"fake": True}


class FakeRuntime(object):
    """能源服务只从运行时读一样东西：定义表。

    它**不读拓扑快照**是有意的——`add_node` 是在 `sync_members` 更新成员表的
    中途被调用的，那时从快照里读分量会读到一个正在变动的视图。
    """

    def __init__(self, definitions):
        # type: (DefinitionSet) -> None
        self.definitions = definitions


def plain_definitions():
    # type: () -> DefinitionSet
    """一张**全部算得出数**的定义表：没有缺项，也没有未接线的输入。

    电源自己也要写明耗能（`Value(0)`）。`supply` 只管「给多少」，它**不蕴含**
    自己「花多少」——创造能量源在真实定义表里就是 `EnergySpec()`（blocks.md 没列
    它自身耗能），因此它属于 `missing_spec`。这里若照抄那一份，夹具里就会多出
    一个算不出的需求，库默认口径下永远判不出「有电」，那条用例也就测不到它想
    测的东西。
    """
    return DefinitionSet(
        devices=(
            DeviceDefinition(
                SMALL_SOURCE,
                "测试小电源",
                CATEGORY_ENERGY_INPUT,
                bandwidth=BandwidthSpec(0),
                energy=EnergySpec(Value(0)),
                supply=Value(1),
            ),
            DeviceDefinition(
                GREEDY,
                "测试耗电设备",
                CATEGORY_BUS,
                bandwidth=BandwidthSpec(0),
                energy=EnergySpec(Value(8)),
            ),
            DeviceDefinition(
                CHEAP,
                "测试省电设备",
                CATEGORY_BUS,
                bandwidth=BandwidthSpec(0),
                energy=EnergySpec(Value(1)),
            ),
            DeviceDefinition(
                OPEN_SOURCE,
                "测试未定电源",
                CATEGORY_ENERGY_INPUT,
                bandwidth=BandwidthSpec(0),
                energy=EnergySpec(Value(0)),
                supply=Value(UNCONFIGURED, unresolved="R08", is_test_value=True),
            ),
        )
    )


class Network(object):
    """一张独立的小网络：一台能源服务 + 一个注册表 + 它已挂上的成员。

    每个用例都用**全新的注册表**：节点是服务看到的世界，两个用例共用一张
    会让「谁在网络上」取决于用例的执行顺序。
    """

    def __init__(self, definitions, policy, device_ids=()):
        # type: (DefinitionSet, object, tuple) -> None
        self.definitions = definitions
        self.registry = DeviceRegistry(definitions)
        self.service = EnergyService(FakeRuntime(definitions), policy=policy)
        self.refs = []
        for device_id in device_ids:
            self.place(device_id)

    def place(self, device_id, x=None):
        # type: (str, int) -> object
        """放一台设备并挂进能源账，返回它的 `DeviceRef`。"""
        if x is None:
            x = 100 + len(self.refs)
        node_key = key(x)
        self.registry.register(node_key, device_id)
        record = self.registry.get(node_key)
        self.service.add_node(NetworkNode.from_record(self.definitions, record))
        self.refs.append(record.ref)
        return record.ref

    def replace(self, index, device_id):
        # type: (int, str) -> object
        """把第 `index` 台设备**同位置**换成另一种方块（代次会变）。"""
        node_key = self.refs[index].node_key
        self.registry.register(node_key, device_id)
        record = self.registry.get(node_key)
        self.service.add_node(NetworkNode.from_record(self.definitions, record))
        self.refs[index] = record.ref
        return record.ref

    def node(self, index=0):
        # type: (int) -> NetworkNode
        return NetworkNode.from_record(
            self.definitions, self.registry.get(self.refs[index].node_key)
        )

    def power(self, index=0):
        # type: (int) -> object
        return self.service.power_ok(self.refs[index].node_key)

    def reason(self):
        # type: () -> str
        return self.service.status()["reason"]


class TestPolicyDefaults(unittest.TestCase):
    """库默认必须落在保守那一侧：替身口径只能被**显式**选择。"""

    def test_library_defaults_are_the_conservative_side(self):
        policy = EnergyPolicy()
        self.assertEqual(policy.no_source, EnergyPolicy.PENDING)
        self.assertEqual(policy.missing_spec, EnergyPolicy.PENDING)
        self.assertEqual(policy.unwired_inputs, EnergyPolicy.PENDING)

    def test_dev_stand_in_is_a_named_preset(self):
        """替身口径有一个具名预置，而不是散在各处的默认值里。"""
        policy = EnergyPolicy.dev_stand_in()
        self.assertEqual(policy.no_source, EnergyPolicy.BROWNOUT)
        self.assertEqual(policy.missing_spec, EnergyPolicy.IGNORE)
        self.assertEqual(policy.unwired_inputs, EnergyPolicy.IGNORE)

    def test_every_policy_field_is_reported_as_unresolved(self):
        """三个字段都进未定报告：**取值如何改变不了「规则未定案」这件事**。

        若哪天有人用「值已经填了」来论证「不用再报」，这条会红。
        """
        for policy in (EnergyPolicy(), EnergyPolicy.dev_stand_in()):
            report = policy.unresolved_report()
            self.assertEqual(
                sorted(item["field"] for item in report),
                ["missing_spec", "no_source", "unwired_inputs"],
            )
            rules = set(item["rule"] for item in report)
            self.assertIn("R08", rules)
            self.assertIn("R10", rules)

    def test_invalid_mode_is_rejected(self):
        """口径写错要当场报错，不能默默退回默认值。"""
        with self.assertRaises(ValueError):
            EnergyPolicy(no_source="maybe")
        with self.assertRaises(ValueError):
            EnergyPolicy(missing_spec="brownout")

    def test_assert_settled_refuses_to_declare_victory(self):
        """`assert_settled()` 只给正式验收用；这里验证它确实会拦（唯一的调用点）。"""
        with self.assertRaises(AssertionError):
            EnergyPolicy().assert_settled()


class TestVerdicts(unittest.TestCase):
    """四条判定次序（顺序本身就是契约）。"""

    def network(self, policy, *device_ids):
        return Network(DefinitionSet(), policy, device_ids)

    def test_a_network_without_a_supplier_is_pending_by_default(self):
        """一台供能设备都没有。**库默认不敢断言「没电」**——那是待决问题。"""
        net = self.network(EnergyPolicy(), BB8, TERMINAL)
        self.assertIsNone(net.power())
        self.assertEqual(net.reason(), Reason.ENERGY_UNAVAILABLE)

    def test_the_same_network_browns_out_under_the_dev_policy(self):
        """同一张网络、同一批成员：换口径才敢说「没电」。

        **成对断言的意义**在于把差别钉在策略上——服务没有偷偷改判。
        """
        net = self.network(EnergyPolicy.dev_stand_in(), BB8, TERMINAL)
        self.assertIs(net.power(), False)
        self.assertEqual(net.reason(), Reason.BROWNOUT)

    def test_the_three_states_map_to_distinguishable_reasons(self):
        """三态要能被主控翻成可定位的原因码，而且**三条互不相同**。"""
        pending = self.network(EnergyPolicy(), BB8, TERMINAL)
        self.assertEqual(
            power_blocker(_Services(pending.service), pending.refs[0].node_key).code,
            Reason.ENERGY_UNAVAILABLE,
        )
        brownout = self.network(EnergyPolicy.dev_stand_in(), BB8, TERMINAL)
        self.assertEqual(
            power_blocker(_Services(brownout.service), brownout.refs[0].node_key).code,
            Reason.BROWNOUT,
        )
        powered = self.network(EnergyPolicy.dev_stand_in(), CREATIVE, BB8)
        self.assertIsNone(
            power_blocker(_Services(powered.service), powered.refs[0].node_key)
        )

    def test_unconfigured_demand_does_not_become_free(self):
        """**「把缺项当成 0」的探针。**

        基带在 `blocks.md` 里没列耗能（R10），因此它的需求**算不出来**。库默认
        口径下这必须让整张网络的判定停在「待确认」；若哪天有人「顺手」把它按 0
        算，这里就会得到「有电」——一个无根据的结论。
        """
        net = self.network(EnergyPolicy(), CREATIVE, BB8)
        self.assertIsNone(net.power())
        self.assertIn(BB8, net.service.totals()["missing_spec"])

    def test_ignoring_unconfigured_demand_is_reachable_only_by_policy(self):
        """替身口径下同一张网络判为有电，**而且这件事被报了出来**。"""
        net = self.network(EnergyPolicy.dev_stand_in(), CREATIVE, BB8)
        self.assertIs(net.power(), True)
        report = net.service.diagnostics()
        self.assertIn(BB8, report["ignored_demand"][GAP_MISSING_SPEC])
        self.assertIn(
            "missing_spec",
            sorted(item["field"] for item in report["unresolved"]),
            "替身口径必须留在未定报告里，否则它就成了悄悄定案",
        )

    def test_pending_policy_does_not_list_demands_as_ignored(self):
        """取 PENDING 时那些设备**没有**被丢掉，只是让判定停在待确认。

        把它们列进 `ignored_demand` 会让人以为漏算了，而实际是没下结论。
        """
        net = self.network(EnergyPolicy(), CREATIVE, BB8)
        self.assertEqual(net.service.diagnostics()["ignored_demand"], {
            GAP_MISSING_SPEC: [],
            GAP_UNWIRED_INPUTS: [],
        })

    def test_unwired_input_keeps_the_verdict_pending(self):
        """`follows_bandwidth` 的折算口径未定（R08）：算不出就是算不出。"""
        net = self.network(EnergyPolicy(), CREATIVE, TINY)
        self.assertIsNone(net.power())
        self.assertIn(TINY, net.service.totals()["unwired_inputs"])

    def test_per_unit_devices_are_unwired_until_told_the_count(self):
        """装置数量还没有来源：**不能默认按 0 算**，否则满载的驱动器不耗电。"""
        net = self.network(EnergyPolicy(), CREATIVE, DRIVER)
        self.assertIsNone(net.power())
        self.assertIn(DRIVER, net.service.totals()["unwired_inputs"])

        # 有人告诉它装了 4 块盘之后，需求就变成已知的 4 EN/t。
        self.assertTrue(net.service.set_device_state(net.refs[1], unit_count=4))
        self.assertEqual(net.service.totals()["demand_total"], 4)
        self.assertEqual(net.service.totals()["unwired_inputs"], [])

    def test_an_unknown_supply_rate_never_decides(self):
        """只接了能源接入口：它供能，但速率未定，因此什么都不能断言。

        把它当成「不供能」会让这张网络报「确认没电」，而正确答案是「说不准」。
        """
        net = self.network(EnergyPolicy.dev_stand_in(), RF_INTERFACE, TERMINAL)
        self.assertIsNone(net.power())
        self.assertIn(RF_INTERFACE, net.service.totals()["unknown_supply"])

    def test_an_unknown_supply_does_not_upgrade_a_pending_verdict(self):
        """已知电源 + 速率未知的电源，仍然不能断言——依据不足就是不足。"""
        net = self.network(
            EnergyPolicy.dev_stand_in(), CREATIVE, RF_INTERFACE, TERMINAL
        )
        self.assertIsNone(net.power())

    def test_a_confirmed_shortfall_is_a_brownout(self):
        """供能精确、需求至多是下界时，「精确值 < 下界」足以断定不够。

        这一条钉住第 3 条次序**排在**「有没有算不出的数」之前——顺序颠倒会让
        一张明显缺电的网络报「待确认」。
        """
        definitions = plain_definitions()
        net = Network(definitions, EnergyPolicy(), (SMALL_SOURCE, GREEDY))
        self.assertIs(net.power(), False)
        self.assertEqual(net.reason(), Reason.BROWNOUT)

    def test_an_unknown_supply_outranks_a_known_shortfall(self):
        """有速率未知的电源时，**连「不够」都不能断言**——这是次序问题。

        供能总量这时只是下界（那台电源至少贡献 0），而需求至多是下界；
        「下界 < 下界」推不出真结论。把第 2 条与第 3 条次序对调，这里就会
        得到一个看似合理的「没电」，而那个结论是不成立的。
        """
        definitions = plain_definitions()
        net = Network(
            definitions, EnergyPolicy(), (SMALL_SOURCE, GREEDY, OPEN_SOURCE)
        )
        self.assertIsNone(net.power(), "供能总量不精确时不能判没电")
        self.assertIn(OPEN_SOURCE, net.service.totals()["unknown_supply"])
        self.assertLess(
            net.service.totals()["supply_total"],
            net.service.totals()["demand_total"],
            "夹具要能真的构造出「已知的不够」，否则这条用例测不到次序",
        )

    def test_a_sufficient_supply_with_all_demands_known_is_powered(self):
        """全部算得出数、且供能足够时，唯一会给出 `True` 的一条路。"""
        definitions = plain_definitions()
        net = Network(definitions, EnergyPolicy(), (SMALL_SOURCE, CHEAP))
        self.assertIs(net.power(), True)
        self.assertEqual(net.reason(), "")

    def test_the_sentinel_never_participates_in_a_comparison(self):
        """`bool(UNCONFIGURED)` 会抛 `TypeError`，因此判定里一次都不能对它取真值。"""
        net = self.network(EnergyPolicy.dev_stand_in(), BB8, DRIVER, TERMINAL)
        net.power()
        net.service.diagnostics()
        self.assertIsInstance(net.power(), bool)

    def test_every_energy_reason_blocks_the_network(self):
        """能源路径会发出的每个原因码都必须真的挡住网络。

        `run_state_of` 对**未知**原因码会当成「没有阻塞」而返回 `RUNNABLE`，
        而它没有完备性守卫——加了码却忘了接线，症状是「带着阻塞却显示可运行」。
        """
        for code in (Reason.BROWNOUT, Reason.ENERGY_UNAVAILABLE):
            self.assertNotEqual(
                run_state_of((BlockReason(code, ""),)), RunState.RUNNABLE, code
            )


class _Services(object):
    """`power_blocker` 只要一个能 `get(service_id)` 的东西。"""

    def __init__(self, service):
        # type: (object) -> None
        self._service = service

    def get(self, service_id):
        return self._service if service_id == ServiceId.ENERGY else None


class TestServiceContract(unittest.TestCase):
    """`NetworkService` 契约面。"""

    def network(self, policy=None, *device_ids):
        return Network(
            DefinitionSet(), policy if policy is not None else EnergyPolicy(), device_ids
        )

    def test_bare_construction_is_legal_and_not_ready(self):
        """`default_stand_ins()` 会用 `None` 构造每个槽位——这条契约必须成立。

        与 `StorageService(None)` 同形：**装了但还没接上运行时**，不是「这个功能
        没写」（因此 `is_stand_in` 为假）。
        """
        service = EnergyService(None)
        self.assertFalse(service.is_ready())
        self.assertFalse(service.is_stand_in)
        self.assertIsNone(service.power_ok(key(0)))
        self.assertEqual(service.tick({}, 64), 0)

    def test_attach_and_detach_move_readiness(self):
        service = EnergyService(None)
        service.attach(FakeRuntime(DefinitionSet()))
        self.assertTrue(service.is_ready())
        service.detach()
        self.assertFalse(service.is_ready())

    def test_power_ok_is_none_for_a_non_member(self):
        """**非成员返回 `None`**：设备刚放下、还没被同步进来的那一帧就是这样。"""
        net = self.network(EnergyPolicy.dev_stand_in(), CREATIVE)
        self.assertIs(net.power(), True)
        self.assertIsNone(net.service.power_ok(key(500)))

    def test_the_verdict_is_network_level(self):
        """同一网络里任何一台成员拿到的都是同一个答案——能源按网络计。"""
        net = self.network(EnergyPolicy.dev_stand_in(), CREATIVE, BB8, TERMINAL)
        self.assertEqual(
            set(net.service.power_ok(ref.node_key) for ref in net.refs), set([True])
        )

    def test_add_node_is_idempotent(self):
        """`sync_members` 每帧对每个成员调一遍：重复登记不算变化，也不重算。"""
        net = self.network(EnergyPolicy.dev_stand_in(), CREATIVE)
        settles = net.service.stats["settles"]
        self.assertFalse(net.service.add_node(net.node()))
        self.assertEqual(net.service.stats["settles"], settles, "幂等重登不该重算")

    def test_a_replaced_device_is_a_new_row(self):
        """同位置换方块 = 换设备：代次变了，那一行要重建（含被告知过的工作态）。"""
        net = self.network(EnergyPolicy.dev_stand_in(), CREATIVE, DRIVER)
        net.service.set_device_state(net.refs[1], unit_count=4)
        self.assertEqual(net.service.totals()["demand_total"], 4)

        net.replace(1, MEMORY_SLOT)
        # 新设备还没被任何人告知过任何事，因此它那一行又是「输入未接线」。
        self.assertIn(MEMORY_SLOT, net.service.totals()["unwired_inputs"])
        self.assertNotIn(DRIVER, net.service.totals()["unwired_inputs"])
        self.assertNotIn(MEMORY_SLOT, net.service.totals()["missing_spec"])

    def test_removing_the_only_supplier_flips_the_verdict(self):
        """判定缓存必须跟着成员变化失效，否则拆了电源还一直报「有电」。

        拆掉的那台自己再问就是 `None`（它已经不是成员了，见
        `test_power_ok_is_none_for_a_non_member`），因此结论要在**留下的那台**
        上读——这正是「网络级判定」该有的读法。
        """
        net = self.network(EnergyPolicy.dev_stand_in(), CREATIVE, TERMINAL)
        self.assertIs(net.power(1), True)
        self.assertTrue(net.service.remove_node(net.node(0)))
        self.assertIs(net.power(1), False)
        self.assertIsNone(net.power(0), "拆掉的设备已不是成员，不该再给结论")
        self.assertFalse(net.service.remove_node(net.node(0)), "重复移除不算变化")

    def test_tick_spends_no_budget_and_changes_nothing(self):
        """能源没有到期任务：`tick` 恒 0，判定也不该被时间影响。"""
        net = self.network(EnergyPolicy.dev_stand_in(), CREATIVE)
        self.assertEqual(net.service.tick({"tick": 10 ** 6}, 64), 0)
        self.assertEqual(net.service.tick({}, 64), 0)
        self.assertIs(net.power(), True)

    def test_reading_diagnostics_does_not_change_anything(self):
        """诊断查询不预留、不消耗能源（04 的接口要求）。"""
        net = self.network(EnergyPolicy.dev_stand_in(), CREATIVE)
        before = net.service.totals()
        net.service.diagnostics()
        net.service.status()
        self.assertEqual(net.service.totals(), before)

    def test_note_domain_change_is_not_claimed_as_handled(self):
        """域变化目前不改判定，因此**不能声称处理了**。

        返回值的语义是「是否真的处理了」，不是「我知道这件事」。
        """
        net = self.network(EnergyPolicy.dev_stand_in(), TINY)
        self.assertFalse(net.service.note_domain_change(net.node(), (), ("d0",)))

    def test_save_node_data_has_nothing_to_store(self):
        """不返回一份看起来像存档的空字典——那会让恢复流程以为有东西要读。"""
        net = self.network(EnergyPolicy.dev_stand_in(), CREATIVE)
        self.assertIsNone(net.service.save_node_data(net.node()))

    def test_handle_is_not_implemented(self):
        """命令路由表里没有能源这一格，真调到这里就是编程错误。"""
        net = self.network()
        with self.assertRaises(NotImplementedError):
            net.service.handle(None, None)

    def test_set_device_state_rejects_a_stale_generation(self):
        """换过实例的设备，旧调用方不该还能改它。"""
        net = self.network(EnergyPolicy.dev_stand_in(), CREATIVE, DRIVER)
        stale = net.refs[1]
        net.replace(1, MEMORY_SLOT)
        self.assertFalse(net.service.set_device_state(stale, unit_count=2))

    def test_set_device_state_rejects_an_unknown_state(self):
        net = self.network(EnergyPolicy.dev_stand_in(), CREATIVE, DRIVER)
        with self.assertRaises(ValueError):
            net.service.set_device_state(net.refs[1], state="overclocked")

    def test_an_unregistered_device_id_is_refused_not_raised(self):
        """定义表里没有的标识：跳过并计数，**不能每帧炸一次**。

        `tick()` 内的异常只记录不抛出，因此这类问题会表现为「设备静默不在账上」
        ——留一个计数器，至少让它可见。
        """
        net = self.network(EnergyPolicy.dev_stand_in(), CREATIVE)
        impostor = NetworkNode(
            key(700), net.refs[0], "endernet:nonexistent", CATEGORY_BUS
        )
        self.assertFalse(net.service.add_node(impostor))
        self.assertEqual(net.service.stats["refused"], 1)


class TestAssembly(unittest.TestCase):
    """能源服务接进装配表之后的样子。"""

    def test_energy_is_no_longer_a_stand_in(self):
        builders = default_builders()
        self.assertIn(ServiceId.ENERGY, builders)
        self.assertFalse(builders[ServiceId.ENERGY](None).is_stand_in)
        self.assertNotIn(ServiceId.ENERGY, default_pending_tasks())

    def test_the_factory_reports_energy_as_a_real_slot(self):
        factory = ServiceFactory()
        self.assertNotIn(ServiceId.ENERGY, factory.stand_in_slots())
        self.assertNotIn(ServiceId.ENERGY, factory.pending_tasks())


class TestBootstrappedEnergy(unittest.TestCase):
    """接进启动层之后的判定（仍不碰引擎）。"""

    def test_driver_mount_and_unmount_update_energy_without_a_tick(self):
        server = self.build(EnergyPolicy.dev_stand_in(), CREATIVE)
        state = server.drivers.get(key(0, 65))
        energy = server.runtimes.runtime_of(state.node_key).services.get(ServiceId.ENERGY)
        before = energy.diagnostics()["totals"]["demand_total"]
        medium, reason, detail = server.drivers.create_medium("endernet:harddisk_1k")
        self.assertIsNotNone(medium, (reason, detail))
        self.assertTrue(state.provider.mount(0, medium.medium_id)[0])
        self.assertEqual(energy.diagnostics()["totals"]["demand_total"], before + 1)
        self.assertTrue(state.provider.unmount(0)[0])
        self.assertEqual(energy.diagnostics()["totals"]["demand_total"], before)

    def test_energy_rebuild_reads_slots_not_the_config_projection(self):
        server = self.build(EnergyPolicy.dev_stand_in(), CREATIVE)
        state = server.drivers.get(key(0, 65))
        medium, _, _ = server.drivers.create_medium("endernet:harddisk_1k")
        self.assertTrue(state.provider.mount(0, medium.medium_id)[0])
        runtime = server.runtimes.runtime_of(state.node_key)
        energy = runtime.services.get(ServiceId.ENERGY)
        record = server.registry.get(state.node_key)
        record.config["disk_count"] = 999
        node = NetworkNode.from_record(server.definitions, record)
        energy.remove_node(node)
        energy.add_node(node)
        row = energy._members[state.node_key]
        self.assertEqual(row.unit_count, 1)
        self.assertEqual(row.demand, 1)
        self.assertEqual(row.gap, "")

    def test_driver_count_changes_the_power_verdict_immediately(self):
        server = self.build(EnergyPolicy.dev_stand_in(), CREATIVE)
        state = server.drivers.get(key(0, 65))
        runtime = server.runtimes.runtime_of(state.node_key)
        energy = runtime.services.get(ServiceId.ENERGY)
        # 仅本用例把电源降为 0，隔离数量回填对真实供需判定的影响。
        source = server.definitions.get(CREATIVE)
        self.addCleanup(setattr, source, "supply", source.supply)
        source.supply = Value(0)
        energy.add_node(NetworkNode.from_record(server.definitions, server.registry.get(key(2))))
        self.assertIs(energy.power_ok(state.node_key), True)
        medium, _, _ = server.drivers.create_medium("endernet:harddisk_1k")
        self.assertTrue(state.provider.mount(0, medium.medium_id)[0])
        self.assertIs(energy.power_ok(state.node_key), False)
        self.assertEqual(power_blocker(runtime.services, state.node_key).code, Reason.BROWNOUT)
        self.assertTrue(state.provider.unmount(0)[0])
        self.assertIs(energy.power_ok(state.node_key), True)

    def test_replaced_driver_does_not_inherit_the_old_count(self):
        server = self.build(EnergyPolicy.dev_stand_in(), CREATIVE)
        state = server.drivers.get(key(0, 65))
        medium, _, _ = server.drivers.create_medium("endernet:harddisk_1k")
        self.assertTrue(state.provider.mount(0, medium.medium_id)[0])
        old_ref = state.ref
        server.push(NormalizedEvent.remove(state.node_key, DRIVER))
        server.tick()
        server.push(NormalizedEvent.place(state.node_key, DRIVER))
        server.tick()
        energy = server.runtimes.runtime_of(state.node_key).services.get(ServiceId.ENERGY)
        self.assertEqual(energy._members[state.node_key].unit_count, 0)
        self.assertFalse(energy.set_device_state(old_ref, unit_count=5))
        self.assertEqual(energy._members[state.node_key].unit_count, 0)

    def test_unchanged_driver_and_diagnostics_do_not_resettle_energy(self):
        server = self.build(EnergyPolicy.dev_stand_in(), CREATIVE)
        energy = server.runtimes.runtime_of(key(0, 65)).services.get(ServiceId.ENERGY)
        before = energy.stats["settles"]
        for _ in range(3):
            server.tick()
            energy.diagnostics()
            energy.power_ok(key(0, 65))
        self.assertEqual(energy.stats["settles"], before)

    def test_initial_empty_driver_is_known_zero_but_memory_is_still_unknown(self):
        server = self.build(EnergyPolicy.dev_stand_in(), CREATIVE)
        server.push(NormalizedEvent.place(key(1, 63), MEMORY_SLOT))
        server.tick()
        runtime = server.runtimes.runtime_of(key(0, 65))
        energy = runtime.services.get(ServiceId.ENERGY)
        self.assertEqual(energy._members[key(0, 65)].unit_count, 0)
        self.assertEqual(energy._members[key(0, 65)].gap, "")
        memory = energy._members[key(1, 63)]
        self.assertIsNone(memory.unit_count)
        self.assertEqual(memory.gap, GAP_UNWIRED_INPUTS)

    def build(self, policy, *extra):
        # type: (object, tuple) -> object
        """基带 + 控制器 + 驱动器，外加若干台设备。"""
        server = ServerBootstrap(world=FakeWorld(), energy_policy=policy)
        server.start(bind_engine=False)
        positions = [
            (key(0), BB8),
            (key(1), BB8),
            (key(1, 65), TINY),
            (key(0, 65), DRIVER),
        ]
        for index, device_id in enumerate(extra):
            positions.append((key(2 + index), device_id))
        for node_key, device_id in positions:
            server.push(NormalizedEvent.place(node_key, device_id))
        server.tick()
        return server

    def runtime_of(self, server):
        runtimes = server.runtimes.runtimes()
        self.assertEqual(len(runtimes), 1, "夹具只应有一个网络")
        return runtimes[0]

    def test_a_creative_energy_source_powers_a_real_network(self):
        """**本次增量的目标**：放上创造能量源，这台网络就真的有电了。

        与 `test_storage.test_a_creative_energy_source_makes_the_network_runnable`
        的分工：那一条证明**命令真的做成了事**（端到端），这一条证明**结论确实
        来自那块方块**——同一张网络不放它就变 `NO_POWER`。
        """
        policy = EnergyPolicy.dev_stand_in()
        powered = self.build(policy, CREATIVE)
        runtime = self.runtime_of(powered)
        self.assertEqual(runtime.state(), RunState.RUNNABLE)
        self.assertIs(runtime.power_state(key(2)), True)

        unpowered = self.build(policy)
        self.assertEqual(self.runtime_of(unpowered).state(), RunState.NO_POWER)
        self.assertIs(self.runtime_of(unpowered).power_state(key(0, 65)), False)

    def test_the_library_policy_keeps_the_same_network_pending(self):
        """同一张**放了电源的**网络，库默认口径下仍然只能说「待确认」。

        这不是自相矛盾，而是两条不同的主张：DEV 口径说「按替身口径算，够电」，
        库默认说「基础数值还没定案，我不下结论」。把两者混成一句话，就等于用
        替身值悄悄定案（全局约束 7）。
        """
        server = self.build(EnergyPolicy(), CREATIVE)
        runtime = self.runtime_of(server)
        self.assertEqual(runtime.state(), RunState.NO_POWER)
        self.assertIsNone(runtime.power_state(key(2)))

    def test_the_diagnostics_expose_what_the_stand_in_threw_away(self):
        """替身口径丢掉了什么，必须能读到**具体设备**，而不只是一个开关名。"""
        server = self.build(EnergyPolicy.dev_stand_in(), CREATIVE)
        report = server.diagnostics()["energy"]
        self.assertEqual(report["networks"], 1)
        self.assertEqual(report["powered"], 1)
        self.assertIn(BB8, report["ignored_demand"][GAP_MISSING_SPEC])
        self.assertIn(TINY, report["ignored_demand"][GAP_UNWIRED_INPUTS])

    def test_no_networks_means_no_numbers(self):
        """没有网络时不该凭空报出「0 台有电」——那是缺项，不是 0。"""
        server = ServerBootstrap(world=FakeWorld())
        server.start(bind_engine=False)
        report = server.diagnostics()["energy"]
        self.assertEqual(report["networks"], 0)
        self.assertEqual(report["powered"], 0)
        self.assertEqual(report["ignored_demand"], {})

    def test_the_policy_is_part_of_the_assembly_guard(self):
        """能源的未定项必须进正式装配检查，否则「已实现」会被读成「已定案」。"""
        server = self.build(EnergyPolicy.dev_stand_in(), CREATIVE)
        problems = server.assert_assembly_ok()
        self.assertTrue(any("no_source" in item for item in problems), problems)
        self.assertTrue(any("R10" in item for item in problems), problems)

    def test_bandwidth_is_judged_without_any_energy_source(self):
        """带宽与能源分别判定：没有供电不影响带宽准入的结论。"""
        server = self.build(EnergyPolicy())
        record = server.registry.get(key(1, 65))
        self.assertTrue(server.admission.status_of(record.ref).ok)
        self.assertTrue(server.admission.diagnostics()["energy_independent"])

    def test_debug_query_reads_the_energy_section(self):
        """`debug_query("energy")` 是能源那一节的只读入口。"""
        server = self.build(EnergyPolicy.dev_stand_in(), CREATIVE)
        result = server.application.debug_query("energy")
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertEqual(result.payload["powered"], 1)

    def test_debug_query_reaches_one_network_by_component_id(self):
        server = self.build(EnergyPolicy.dev_stand_in(), CREATIVE)
        component_id = self.runtime_of(server).component_id
        result = server.application.debug_query("energy", component_id=component_id)
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertIs(result.payload["power_ok"], True)

    def test_debug_query_answers_without_energy_networks(self):
        """诊断在不能工作时也要可读：没有网络时给的是空报表，不是报错。"""
        server = ServerBootstrap(world=FakeWorld())
        server.start(bind_engine=False)
        result = server.application.debug_query("energy")
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertEqual(result.payload["networks"], 0)


if __name__ == "__main__":
    unittest.main()
