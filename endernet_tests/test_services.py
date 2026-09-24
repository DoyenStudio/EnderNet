# coding=utf-8
"""任务 03-01 的离线验收：服务槽位、装配顺序、替身与命令路由。

覆盖 03 验收清单里与「服务怎么装、怎么被拒绝」有关的部分：

- 四个槽位（调度/能源/存储/工厂）都以显式契约注册，重复 id、空 id、
  未知槽位、未知依赖全部在装配期报错而不是留到运行期。
- 依赖按拓扑排序；缺依赖与成环都抛 `AssemblyError`。
- 替身**不假装能用**：不挂载能力、业务一律 `service_absent`。
- 开发档容忍替身并逐条列出；正式档直接报错，不做「先跑起来再说」的妥协。

**未定规则仍在。** `BandwidthPolicy()` 的字段全是测试替身，本文件只验证
「替身不会被静默使用」这一机制。**不调用 `assert_settled()`**——那意味着
宣布数值可以验收了，不属于离线开发阶段。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.protocol import (
    CommandKind,
    CommandResult,
    CommandSource,
    DeviceCommand,
    Reason,
)
from endernet.server.network.admission import BandwidthPolicy
from endernet.server.network.services import (
    AssemblyError,
    AssemblyProfile,
    COMMAND_ROUTES,
    NetworkService,
    ServiceAbsentError,
    ServiceFactory,
    ServiceId,
    ServiceRegistry,
    StandInService,
    default_builders,
    default_pending_tasks,
    is_read_only,
    route_of,
)


class EchoService(NetworkService):
    """最小正式服务：用来验证「非替身」这一侧的行为。"""

    def __init__(self, service_id, requires=()):
        # type: (str, tuple) -> None
        self.service_id = service_id
        self.requires = tuple(requires)
        self.nodes = []
        self.ticked = 0

    def add_node(self, node, saved_data=None):
        # type: (object, dict) -> bool
        self.nodes.append(node)
        return True

    def tick(self, context, budget):
        # type: (dict, int) -> int
        self.ticked += 1
        return 1

    def handle(self, grant, command):
        # type: (object, object) -> object
        return CommandResult.ok(command.request_id, {"service": self.service_id})


def ref(x=0, generation=1):
    return DeviceRef(NodeKey(0, x, 64, 0), generation)


def command(kind=CommandKind.STORAGE, request_id="r1"):
    return DeviceCommand(
        request_id,
        kind,
        CommandSource("p1", ref(), 0),
    )


class TestServiceIds(unittest.TestCase):
    def test_four_slots_are_registered(self):
        self.assertEqual(
            sorted(ServiceId.ALL),
            ["energy", "factory", "scheduler", "storage"],
        )

    def test_route_table_covers_every_command_kind(self):
        """每一种命令类型都必须有明确路由，否则会出现「没人接」的命令。"""
        for kind in sorted(CommandKind.ALL):
            self.assertIn(kind, COMMAND_ROUTES, "命令类型 %s 没有路由" % (kind,))

    def test_unknown_kind_has_no_route(self):
        """未知类型不兜底推断：猜一个服务出来就等于让设备绕过准入。"""
        self.assertIsNone(route_of("teleport"))
        self.assertIsNone(route_of(""))

    def test_read_only_kinds(self):
        self.assertTrue(is_read_only(CommandKind.STATUS))
        self.assertTrue(is_read_only(CommandKind.RECOVERY))
        self.assertFalse(is_read_only(CommandKind.STORAGE))
        self.assertFalse(is_read_only(CommandKind.ORDER))
        self.assertFalse(is_read_only(CommandKind.CONFIGURE))

    def test_routes_point_at_the_documented_slots(self):
        self.assertEqual(route_of(CommandKind.STORAGE)[0], ServiceId.STORAGE)
        self.assertEqual(route_of(CommandKind.CONFIGURE)[0], ServiceId.STORAGE)
        self.assertEqual(route_of(CommandKind.ORDER)[0], ServiceId.FACTORY)
        # 只读命令不落到业务服务上：STATUS 由主控答，RECOVERY 由运行时答。
        self.assertIsNone(route_of(CommandKind.STATUS)[0])
        self.assertIsNone(route_of(CommandKind.RECOVERY)[0])


class TestAssemblyProfile(unittest.TestCase):
    def test_known_profiles(self):
        self.assertEqual(AssemblyProfile.validate("dev"), AssemblyProfile.DEV)
        self.assertEqual(
            AssemblyProfile.validate("production"), AssemblyProfile.PRODUCTION
        )

    def test_unknown_profile_raises(self):
        with self.assertRaises(ValueError):
            AssemblyProfile.validate("staging")


class TestServiceRegistry(unittest.TestCase):
    def test_empty_registry_gets_the_default_stand_ins(self):
        """不给服务即装默认替身——这是开发档最常用的形态。"""
        registry = ServiceRegistry()
        self.assertEqual(sorted(registry.ids()), sorted(ServiceId.ALL))
        self.assertEqual(len(registry.stand_ins()), 4)

    def test_explicit_services_do_not_get_defaults(self):
        registry = ServiceRegistry([EchoService(ServiceId.STORAGE)])
        self.assertEqual(registry.ids(), (ServiceId.STORAGE,))
        self.assertEqual(registry.stand_ins(), [])

    def test_install_rejects_empty_id(self):
        class Nameless(NetworkService):
            pass

        with self.assertRaises(ValueError):
            ServiceRegistry([EchoService(ServiceId.STORAGE), Nameless()])

    def test_install_rejects_unknown_slot(self):
        with self.assertRaises(ValueError):
            ServiceRegistry([EchoService("quantum")])

    def test_install_rejects_duplicate_slot(self):
        """重复占用同一个槽位是装配错误，不静默覆盖。"""
        with self.assertRaises(ValueError):
            ServiceRegistry(
                [EchoService(ServiceId.STORAGE), EchoService(ServiceId.STORAGE)]
            )

    def test_install_rejects_unknown_dependency(self):
        with self.assertRaises(ValueError):
            ServiceRegistry([EchoService(ServiceId.STORAGE, requires=("quantum",))])

    def test_missing_dependency_raises_on_assembly(self):
        registry = ServiceRegistry(
            [EchoService(ServiceId.STORAGE, requires=(ServiceId.ENERGY,))]
        )
        with self.assertRaises(AssemblyError):
            registry.assembly_order()

    def test_cycle_raises_on_assembly(self):
        """成环不静默放过：它在运行期只表现为「有时这个顺序、有时那个」。"""
        registry = ServiceRegistry(
            [
                EchoService(ServiceId.STORAGE, requires=(ServiceId.FACTORY,)),
                EchoService(ServiceId.FACTORY, requires=(ServiceId.STORAGE,)),
            ]
        )
        with self.assertRaises(AssemblyError):
            registry.assembly_order()

    def test_assembly_order_respects_dependencies(self):
        registry = ServiceRegistry(
            [
                EchoService(ServiceId.FACTORY, requires=(ServiceId.STORAGE,)),
                EchoService(ServiceId.STORAGE, requires=(ServiceId.ENERGY,)),
                EchoService(ServiceId.ENERGY),
            ]
        )
        order = [service.service_id for service in registry.assembly_order()]
        self.assertLess(order.index(ServiceId.ENERGY), order.index(ServiceId.STORAGE))
        self.assertLess(order.index(ServiceId.STORAGE), order.index(ServiceId.FACTORY))

    def test_assembly_order_is_stable(self):
        """同一份装配表两次排序结果一致，否则推进顺序会逐帧漂移。"""
        registry = ServiceRegistry()
        first = [item.service_id for item in registry.assembly_order()]
        second = [item.service_id for item in registry.assembly_order()]
        self.assertEqual(first, second)

    def test_get_and_require_differ_on_missing(self):
        """`get` 缺失返回 None，`require` 抛错——调用方要能区分。"""
        registry = ServiceRegistry([EchoService(ServiceId.STORAGE)])
        self.assertIsNone(registry.get(ServiceId.FACTORY))
        with self.assertRaises(ServiceAbsentError):
            registry.require(ServiceId.FACTORY)

    def test_diagnostics_lists_stand_ins(self):
        report = ServiceRegistry().diagnostics()
        self.assertEqual(sorted(report["stand_ins"]), sorted(ServiceId.ALL))
        self.assertEqual(len(report["services"]), 4)


class TestStandInService(unittest.TestCase):
    def setUp(self):
        self.service = StandInService(ServiceId.STORAGE, "05", "介质与库存")

    def test_rejects_unknown_slot(self):
        with self.assertRaises(ValueError):
            StandInService("quantum", "99")

    def test_is_a_stand_in_and_not_ready(self):
        self.assertTrue(self.service.is_stand_in)
        self.assertFalse(self.service.is_ready())

    def test_add_node_mounts_nothing(self):
        """替身登记节点时**不挂载任何能力**，只记一笔供诊断。"""
        self.assertFalse(self.service.add_node(object()))
        self.assertEqual(self.service.nodes_seen, 1)

    def test_tick_does_no_work(self):
        self.assertEqual(self.service.tick({}, 64), 0)

    def test_business_is_refused_not_faked(self):
        """替代品返回的是「还没做」，不是一份看起来正常的空库存。"""
        result = self.service.handle(None, command())
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason_codes(), (Reason.SERVICE_ABSENT,))
        self.assertEqual(result.first_reason(), Reason.SERVICE_ABSENT)
        self.assertIn("05", result.reasons[0].detail)

    def test_power_is_unconfirmed(self):
        """未确认不等于有电：替身给 None，主控不会据此放行。"""
        self.assertIsNone(self.service.power_ok(object()))

    def test_tick_is_counted_for_diagnostics(self):
        self.service.tick({}, 64)
        self.assertEqual(self.service.tick_calls, 1)

    def test_status_says_unavailable(self):
        status = self.service.status()
        self.assertFalse(status["available"])
        self.assertTrue(status["stand_in"])
        self.assertEqual(status["pending_task"], "05")


class TestServiceFactory(unittest.TestCase):
    def test_every_slot_has_a_builder(self):
        factory = ServiceFactory()
        self.assertEqual(sorted(factory.ids()), sorted(ServiceId.ALL))
        self.assertEqual(sorted(factory.stand_in_slots()), sorted(ServiceId.ALL))

    def test_create_gives_fresh_instances_each_time(self):
        """两个运行时绝不共享服务实例（验收 #4）。"""
        factory = ServiceFactory()
        first = factory.create(None)
        second = factory.create(None)
        self.assertIsNot(first.get(ServiceId.STORAGE), second.get(ServiceId.STORAGE))

    def test_create_returns_a_full_registry(self):
        registry = ServiceFactory().create(None)
        self.assertEqual(sorted(registry.ids()), sorted(ServiceId.ALL))
        for service in registry.services():
            self.assertTrue(service.is_stand_in)

    def test_partial_override_keeps_other_slots(self):
        """只换一个槽位不该把别的槽位弄丢。

        丢掉一个槽位在运行期表现为神秘的 `service_absent`，
        不值得让人去查装配表。
        """
        factory = ServiceFactory(
            {ServiceId.STORAGE: lambda runtime: EchoService(ServiceId.STORAGE)}
        )
        self.assertEqual(sorted(factory.ids()), sorted(ServiceId.ALL))
        self.assertEqual(factory.stand_in_slots(), (ServiceId.ENERGY, ServiceId.FACTORY, ServiceId.SCHEDULER))
        registry = factory.create(None)
        self.assertFalse(registry.get(ServiceId.STORAGE).is_stand_in)

    def test_override_rejects_unknown_slot(self):
        with self.assertRaises(ValueError):
            ServiceFactory({"quantum": lambda runtime: None})

    def test_override_rejects_non_callable(self):
        with self.assertRaises(TypeError):
            ServiceFactory({ServiceId.STORAGE: object()})

    def test_replace_after_construction(self):
        factory = ServiceFactory()
        factory.replace(ServiceId.FACTORY, lambda runtime: EchoService(ServiceId.FACTORY))
        self.assertEqual(factory.stand_in_slots(), (ServiceId.ENERGY, ServiceId.SCHEDULER, ServiceId.STORAGE))

    def test_pending_tasks_match_the_documented_tasks(self):
        """替身槽位对应的任务编号是可查的，界面才能说清「等哪个任务」。"""
        pending = ServiceFactory().pending_tasks()
        self.assertEqual(pending[ServiceId.STORAGE], "05")
        self.assertEqual(pending[ServiceId.FACTORY], "09")
        self.assertEqual(pending[ServiceId.ENERGY], "04")
        self.assertEqual(pending[ServiceId.SCHEDULER], "04")

    def test_no_pending_tasks_after_full_override(self):
        builders = dict(
            (service_id, (lambda sid: lambda runtime: EchoService(sid))(service_id))
            for service_id in ServiceId.ALL
        )
        factory = ServiceFactory(builders)
        self.assertEqual(factory.stand_in_slots(), ())
        self.assertEqual(factory.pending_tasks(), {})

    def test_default_builders_cover_every_slot(self):
        """默认表与会话槽位表必须同步，否则新增槽位会漏装。"""
        self.assertEqual(sorted(default_builders()), sorted(ServiceId.ALL))
        self.assertEqual(sorted(default_pending_tasks()), sorted(ServiceId.ALL))


class TestProfileGuard(unittest.TestCase):
    """开发档与正式档的分界：替身与未定规则不得静默流入正式装配。"""

    def test_dev_tolerates_and_reports(self):
        registry = ServiceRegistry()
        problems = registry.assert_profile_ok(AssemblyProfile.DEV, BandwidthPolicy())
        # 4 个替身 + 未定策略字段，逐条列出而不是一句「有警告」
        self.assertGreaterEqual(len(problems), 4)
        self.assertTrue(any("替身服务" in item for item in problems))

    def test_production_refuses_stand_ins(self):
        registry = ServiceRegistry()
        with self.assertRaises(AssemblyError):
            registry.assert_profile_ok(AssemblyProfile.PRODUCTION, BandwidthPolicy())

    def test_production_accepts_a_fully_resolved_assembly(self):
        """全部换成正式实现、策略也没有未定项时，正式档必须放行。"""
        registry = ServiceRegistry(
            [EchoService(service_id) for service_id in sorted(ServiceId.ALL)]
        )
        self.assertEqual(
            registry.assert_profile_ok(AssemblyProfile.PRODUCTION, None), []
        )

    def test_production_refuses_unresolved_policy_even_without_stand_ins(self):
        """01 交付说明第八节点名的那条契约：替身数值不能被当成正式结果。"""
        registry = ServiceRegistry(
            [EchoService(service_id) for service_id in sorted(ServiceId.ALL)]
        )
        with self.assertRaises(AssemblyError):
            registry.assert_profile_ok(AssemblyProfile.PRODUCTION, BandwidthPolicy())

    def test_error_message_names_the_unresolved_rules(self):
        """报错要说清是哪些规则没定案，否则只能去翻代码。"""
        registry = ServiceRegistry()
        with self.assertRaises(AssemblyError) as caught:
            registry.assert_profile_ok(AssemblyProfile.PRODUCTION, BandwidthPolicy())
        self.assertIn("R02", str(caught.exception))
        self.assertIn("替身服务", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
