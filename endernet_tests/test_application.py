# coding=utf-8
"""任务 03-04 的离线验收：应用层的路由、无网络时的回答与调试入口。

对应 03 验收里「命令送到谁手上、送不到时说什么」的部分。这一层**不重复
主控的准入判定**，因此这里不测门链（那在 `test_controller.py`），只测三件
主控做不了的事：

- **路由**：主控只知道自己的分量，只有运行时管理器同时看得见全部网络。
- **无网络时的诚实回答**：设备不属于任何网络、或网络没有主控时，必须给出
  带原因的 `blocked`，而不是 `None`。
- **兜住异常**：主控按纪律不吞异常，这条链接着引擎回调，异常要在这里
  变成 `rejected internal_error`，缺陷仍然看得见，帧不会烂。

**夹具默认用 04/05/09 的真实替身**（业务一律拒绝），因为那正是游戏内
当前的装配形态：设备因此全部离线、没有网络是 runnable。需要观察「命令
到底送到了谁手上」时换 `RoutedFixture` —— 替身永远拒绝业务，那种情况下
服务有没有被调用根本不是路由的证据。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.identity import ChunkKey, NodeKey
from endernet.common.protocol import (
    BlockReason,
    CommandKind,
    CommandResult,
    CommandSource,
    DeviceCommand,
    Reason,
)
from endernet.server.application import (
    ApplicationService,
    DEBUG_OPS,
    READ_ONLY_KINDS,
    summarize_result,
)
from endernet.server.bootstrap import ServerBootstrap
from endernet.server.network.services import (
    AssemblyProfile,
    NetworkService,
    ServiceFactory,
    ServiceId,
)
from endernet.server.runtime.events import NormalizedEvent

BB8 = "endernet:network_baseband_8"
TINY = "endernet:tiny_network_endpoint"
DRIVER = "endernet:storage_driver"

STATUS = "ok"
BLOCKED = "blocked"
REJECTED = "rejected"


def key(x, y=64, z=0, dimension=0):
    return NodeKey(dimension, x, y, z)


class FakeWorld(object):
    """最小世界替身。事件直接入队，因此这里只需能够被构造。"""

    def list_loaded_chunks(self):
        return []

    def block_entities_in_chunk(self, chunk_key):
        return []

    def register_block_remove_listener(self, block_ids):
        pass

    def facing_at(self, node_key):
        return None

    def diagnostics(self):
        return {"fake": True}


class RecordingService(NetworkService):
    """记录型双件：只数「谁被调用过」，不假装有库存。

    与 `test_controller.py` 里同名双件的区别只有一处：那条链上测的是
    **准入**，这里测的是**路由**，所以要看的是「哪个网络的那一份实例
    被调用了」。两份实例互相独立，因此调用记录本身就是路由的证据。
    """

    def __init__(self, service_id):
        # type: (str) -> None
        self.service_id = service_id
        self.handled = []

    def power_ok(self, node_key):
        # type: (object) -> object
        return True

    def handle(self, grant, command):
        # type: (object, object) -> object
        self.handled.append(command.request_id)
        return CommandResult.ok(command.request_id, {"by": self.service_id})


class ApplicationFixture(unittest.TestCase):
    """两个互不相连的网络；A 的控制器与目标设备**分处不同区块**。

    跨区块是刻意的：`test_unloaded_master_blocks_the_network_snapshot`
    要能只卸载控制器所在的那个区块，而让目标设备仍处于加载状态。
    同区块的话两个一起卸载，测到的是「设备没了」而不是「主控够不着」。

        (15,64,0) 基带 ← (16,64,0) 基带 ← (16,65,0) 控制器   [区块 (1,0)]
          ↑
        (15,64,1) 存储驱动器                                  [区块 (0,0)]

        (32,64,0) 基带 ← (32,65,0) 控制器 + (32,64,1) 驱动器  [区块 (2,0)]
    """

    A_BB = key(15, 64, 0)
    A_BB2 = key(16, 64, 0)
    A_CTRL = key(16, 65, 0)
    A_DEV = key(15, 64, 1)
    B_BB = key(32, 64, 0)
    B_CTRL = key(32, 65, 0)
    B_DEV = key(32, 64, 1)

    def service_factories(self):
        # type: () -> object
        """默认 None = 用 04/05/09 的替身。子类覆盖它来换双件。"""
        return None

    def setUp(self):
        self.server = ServerBootstrap(
            world=FakeWorld(), service_factories=self.service_factories()
        )
        self.server.start(bind_engine=False)
        self.drive(
            NormalizedEvent.place(self.A_BB, BB8),
            NormalizedEvent.place(self.A_BB2, BB8),
            NormalizedEvent.place(self.A_CTRL, TINY),
            NormalizedEvent.place(self.A_DEV, DRIVER),
            NormalizedEvent.place(self.B_BB, BB8),
            NormalizedEvent.place(self.B_CTRL, TINY),
            NormalizedEvent.place(self.B_DEV, DRIVER),
        )

    @property
    def application(self):
        return self.server.application

    def drive(self, *events):
        for event in events:
            self.server.push(event)
        self.server.tick()

    def ref_of(self, position):
        record = self.server.registry.get(position)
        self.assertIsNotNone(record, "位置 %s 没有设备" % (position.as_key(),))
        return record.ref

    def command(self, kind=CommandKind.STORAGE, position=None, request_id="r1", **kwargs):
        ref = self.ref_of(position if position is not None else self.A_DEV)
        source = CommandSource(
            kwargs.pop("player_id", "p1"),
            kwargs.pop("source_ref", ref),
            kwargs.pop("dimension", 0),
            distance_ok=kwargs.pop("distance_ok", True),
            session_id=kwargs.pop("session_id", ""),
        )
        return DeviceCommand(
            request_id, kind, source, payload=kwargs.pop("payload", None), device_ref=ref
        )

    def submit(self, kind=CommandKind.STORAGE, position=None, request_id="r1", **kwargs):
        return self.application.submit(
            self.command(kind, position, request_id, **kwargs)
        )

    def runtime_for(self, position):
        return self.server.runtimes.runtime_for(self.ref_of(position))

    def unload_chunk_of(self, position):
        chunk = ChunkKey(
            position.dimension, position.x // 16, position.z // 16
        )
        self.drive(NormalizedEvent.chunk_unloaded(chunk))


class RoutedFixture(ApplicationFixture):
    """换成记录型服务：网络真的 runnable，才看得见命令落到了哪一份实例上。"""

    def service_factories(self):
        return ServiceFactory(
            {
                ServiceId.STORAGE: lambda runtime: RecordingService(ServiceId.STORAGE),
                ServiceId.SCHEDULER: lambda runtime: RecordingService(ServiceId.SCHEDULER),
                ServiceId.ENERGY: lambda runtime: RecordingService(ServiceId.ENERGY),
            }
        )


class TestRouting(RoutedFixture):
    def test_command_goes_to_the_master_of_its_own_network(self):
        """两个网络的服务是各自独立的，因此「谁被调用」就是路由的证据。"""
        first = self.server.runtimes.runtime_for(self.ref_of(self.A_DEV))
        second = self.server.runtimes.runtime_for(self.ref_of(self.B_DEV))
        self.assertIsNot(first, second)
        self.submit(position=self.B_DEV, request_id="to-b")
        self.assertEqual(first.services.get(ServiceId.STORAGE).handled, [])
        self.assertEqual(second.services.get(ServiceId.STORAGE).handled, ["to-b"])

    def test_a_routed_command_is_applied(self):
        result = self.submit(request_id="moved")
        self.assertEqual(result.status, STATUS, result.to_dict())
        self.assertEqual(result.payload["by"], ServiceId.STORAGE)

    def test_routing_is_counted_for_diagnostics(self):
        self.submit(request_id="a")
        self.submit(position=self.B_DEV, request_id="b")
        self.assertEqual(self.application.stats["routed"], 2)
        self.assertEqual(self.application.stats["unrouted"], 0)

    def test_unknown_device_is_still_answered(self):
        """设备已拆除：没有运行时，但调用方拿到的是一条带原因的结果。"""
        ref = self.ref_of(self.A_DEV)
        self.drive(NormalizedEvent.remove(self.A_DEV, DRIVER))
        command = DeviceCommand(
            "gone", CommandKind.STORAGE, CommandSource("p1", ref, 0), device_ref=ref
        )
        result = self.application.submit(command)
        self.assertEqual(result.status, BLOCKED, result.to_dict())
        self.assertIn(Reason.MASTER_ABSENT, result.reason_codes())
        self.assertEqual(self.application.stats["unrouted"], 1)


class TestRoutingWithoutServices(ApplicationFixture):
    """替身装配下的路由：结论一样，只是理由不同。"""

    def test_unknown_kind_never_reaches_a_runtime(self):
        result = self.application.submit(self.command(kind="teleport"))
        self.assertEqual(result.status, REJECTED, result.to_dict())
        self.assertIn(Reason.BAD_PARAMETER, result.reason_codes())

    def test_missing_target_is_rejected(self):
        command = DeviceCommand(
            "r1", CommandKind.STORAGE, CommandSource("p1", None, 0)
        )
        result = self.application.submit(command)
        self.assertEqual(result.status, REJECTED, result.to_dict())
        self.assertIn(Reason.BAD_PARAMETER, result.reason_codes())

    def test_business_is_refused_while_the_services_are_stand_ins(self):
        """DEV 档下命令确实进得了主控，但会被服务这一层如实拒绝。

        这正是游戏内当前的样子：`service_absent`，不是故障也不是没路由。
        """
        result = self.submit(request_id="r1")
        self.assertEqual(result.status, BLOCKED, result.to_dict())
        self.assertIn(Reason.SERVICE_ABSENT, result.reason_codes())
        self.assertEqual(self.application.stats["routed"], 1)


class TestReadOnlyWithoutAMaster(ApplicationFixture):
    """只读路径没有门禁：诊断本来就要在不能工作时也能看。"""

    def drop_master(self):
        self.drive(NormalizedEvent.remove(self.A_CTRL, TINY))
        self.assertIsNone(self.runtime_for(self.A_DEV).master)

    def test_status_is_answered_without_a_master(self):
        self.drop_master()
        result = self.submit(CommandKind.STATUS, request_id="s1")
        self.assertEqual(result.status, STATUS, result.to_dict())
        self.assertEqual(result.payload["run_state"], "no_controller")
        self.assertIn("no_controller", [r["code"] for r in result.payload["blockers"]])

    def test_business_is_blocked_without_a_master(self):
        self.drop_master()
        result = self.submit(request_id="r1")
        self.assertEqual(result.status, BLOCKED, result.to_dict())
        self.assertIn(Reason.NO_CONTROLLER, result.reason_codes())

    def test_recovery_report_is_answered_without_a_master(self):
        self.drop_master()
        result = self.submit(
            CommandKind.RECOVERY, request_id="rec", payload={"op": "report"}
        )
        self.assertEqual(result.status, STATUS, result.to_dict())
        self.assertIn("recovery", result.payload)
        self.assertEqual(result.payload["component_id"], self.runtime_for(self.A_DEV).component_id)

    def test_status_is_answered_for_a_device_outside_any_network(self):
        """孤零零一台设备也要给结论——那正是最需要能查到的一条。

        它自己就是一个分量（没有控制器），因此走的是「有网络、没主控」
        这条路，而不是「不属于任何网络」。
        """
        orphan = key(300, 64, 0)
        self.drive(NormalizedEvent.place(orphan, DRIVER))
        result = self.submit(CommandKind.STATUS, position=orphan, request_id="s1")
        self.assertEqual(result.status, STATUS, result.to_dict())
        self.assertEqual(result.payload["run_state"], "no_controller")
        self.assertEqual(result.payload["members"], 1)
        self.assertFalse(self.application.device_status(self.ref_of(orphan)).is_online)

    def test_status_is_answered_for_a_removed_device(self):
        """设备连分量都没有了：`device` 一节仍然给出「实例已不存在」。"""
        ref = self.ref_of(self.A_DEV)
        self.drive(NormalizedEvent.remove(self.A_DEV, DRIVER))
        command = DeviceCommand(
            "s1", CommandKind.STATUS, CommandSource("p1", ref, 0), device_ref=ref
        )
        result = self.application.submit(command)
        self.assertEqual(result.status, STATUS, result.to_dict())
        self.assertIsNone(result.payload["component_id"])
        self.assertFalse(result.payload["device"]["online"])
        self.assertIn(Reason.DEVICE_GONE, result.payload["device"]["reasons"][0]["code"])

    def test_unloaded_master_blocks_the_network_snapshot(self):
        """主控区块未加载时，**命令态是保守的**：只报阻塞，不端出网络快照。

        这与设备状态栏不同，而且是有意的：状态栏是一台设备自己的结论
        （`blocks.md` 第 9 条要求它在任何情况下都能显示离线与原因），
        而 `STATUS` 命令代表的是**整个网络**的当前状态——主控不可达时
        那份快照不配称作网络的权威状态。
        """
        self.unload_chunk_of(self.A_CTRL)
        result = self.submit(CommandKind.STATUS, request_id="s1")
        self.assertEqual(result.status, BLOCKED, result.to_dict())
        self.assertIn(Reason.MASTER_UNLOADED, result.reason_codes())
        # 但设备状态栏照样能读，且说的就是「主控不可达」。
        status = self.application.device_status(self.ref_of(self.A_DEV))
        self.assertFalse(status.is_online)
        self.assertIn(Reason.MASTER_UNLOADED, status.reason_codes())

    def test_status_is_answered_under_a_controller_conflict(self):
        """再放一个控制器进 A：冲突时没有主控，但状态照样答得出来。"""
        self.drive(NormalizedEvent.place(key(15, 65, 0), TINY))
        self.assertIsNone(self.runtime_for(self.A_DEV).master)
        result = self.submit(CommandKind.STATUS, request_id="s1")
        self.assertEqual(result.status, STATUS, result.to_dict())
        self.assertEqual(result.payload["run_state"], "controller_conflict")


class TestErrorsAreContained(ApplicationFixture):
    def test_submit_never_raises(self):
        """主控不吞异常，但这一层必须吞：它接着引擎事件回调。"""

        class Boom(object):
            def submit(self, command):
                raise RuntimeError("实现缺陷")

            def __getattr__(self, name):
                return None

        runtime = self.runtime_for(self.A_DEV)
        runtime.master = Boom()
        result = self.submit(request_id="boom")
        self.assertEqual(result.status, REJECTED, result.to_dict())
        self.assertIn(Reason.INTERNAL, result.reason_codes())
        self.assertEqual(self.application.stats["errors"], 1)

    def test_the_error_detail_names_the_exception_type(self):
        """缺陷要看得见：堆栈由调用方记，类型与消息在这里。"""

        class Boom(object):
            def submit(self, command):
                raise ValueError("坏参数")

            def __getattr__(self, name):
                return None

        runtime = self.runtime_for(self.A_DEV)
        runtime.master = Boom()
        result = self.submit(request_id="boom")
        self.assertIn("ValueError", result.reasons[0].detail)
        self.assertIn("坏参数", result.reasons[0].detail)


class TestReadOnlyFacade(RoutedFixture):
    def test_device_status_works_for_both_networks(self):
        for position in (self.A_DEV, self.B_DEV):
            status = self.application.device_status(self.ref_of(position))
            self.assertTrue(status.is_online, status.to_dict())

    def test_network_status_returns_every_runtime(self):
        views = self.application.network_status()
        self.assertEqual(len(views), 2)
        one = self.application.network_status(self.runtime_for(self.A_DEV).component_id)
        self.assertIn("run_state", one)

    def test_network_status_for_an_unknown_component_is_empty(self):
        self.assertEqual(self.application.network_status("nope"), {})

    def test_snapshot_lists_runnable_networks(self):
        snapshot = self.application.status_snapshot()
        self.assertEqual(snapshot["count"], 2)
        self.assertEqual(len(snapshot["runnable"]), 2)


class TestAssemblyView(ApplicationFixture):
    def test_devices_are_offline_while_the_services_are_stand_ins(self):
        """DEV 档 + 替身：设备**必须**报离线，理由是「服务尚未实现」。

        这条钉住的是「不要让替身看起来像能用」——若哪天替身被换个实现
        悄悄返回 True，设备会集体变成在线，而库存其实根本没人管。
        """
        for position in (self.A_DEV, self.B_DEV):
            status = self.application.device_status(self.ref_of(position))
            self.assertFalse(status.is_online)
            self.assertIn(Reason.SERVICE_ABSENT, status.reason_codes())

    def test_no_network_is_runnable_while_the_services_are_stand_ins(self):
        snapshot = self.application.status_snapshot()
        self.assertEqual(snapshot["count"], 2)
        self.assertEqual(snapshot["runnable"], [])

    def test_assembly_view_reports_unresolved_rules(self):
        view = self.application.assembly_view()
        self.assertEqual(view["profile"], AssemblyProfile.DEV)
        self.assertGreaterEqual(view["unresolved_count"], 1)
        self.assertNotIn("blocking", view, "DEV 档不拦业务")

    def test_read_only_kinds_match_the_routes(self):
        from endernet.server.network.services import is_read_only

        for kind in sorted(READ_ONLY_KINDS):
            self.assertTrue(is_read_only(kind))
        self.assertNotIn(CommandKind.STORAGE, READ_ONLY_KINDS)


class TestDebugQuery(RoutedFixture):
    def test_every_whitelisted_op_is_implemented(self):
        """白名单加了一项却没在 `_run_query` 里实现，是个缺陷，不该静默返回空。"""
        for op in sorted(DEBUG_OPS):
            self.application._run_query(op, {})

    def test_unknown_op_is_refused(self):
        result = self.application.debug_query("give_me_items")
        self.assertEqual(result.status, REJECTED, result.to_dict())
        self.assertIn(Reason.UNSUPPORTED, result.reason_codes())
        self.assertEqual(self.application.stats["queries_refused"], 1)

    def test_refusal_lists_what_is_available(self):
        detail = self.application.debug_query("nope").reasons[0].detail
        self.assertIn("device", detail)
        self.assertIn("assembly", detail)

    def test_no_write_operations_are_whitelisted(self):
        """「调试需要」不是加写操作的理由：那正是绕过运行条件的定义。"""
        for op in DEBUG_OPS:
            for word in ("write", "set", "give", "insert", "extract", "craft", "force"):
                self.assertNotIn(word, op)

    def test_device_query_returns_the_same_data_as_the_status_bar(self):
        ref = self.ref_of(self.A_DEV)
        payload = self.application.debug_query("device", ref=ref).payload
        self.assertEqual(payload, self.application.device_status(ref).to_dict())

    def test_device_query_without_a_ref_is_an_error_payload(self):
        payload = self.application.debug_query("device").payload
        self.assertIn("error", payload)

    def test_ledger_query_reports_when_there_is_no_master(self):
        payload = self.application.debug_query("ledger", component_id=None).payload
        self.assertIsNone(payload["ledger"])
        self.assertIn("note", payload)

    def test_ledger_query_after_a_command(self):
        """账本查的是「这条请求来过」，而 `open` 说明它没有卡在在途状态。"""
        self.submit(request_id="r1")
        runtime = self.runtime_for(self.A_DEV)
        payload = self.application.debug_query(
            "ledger", component_id=runtime.component_id
        ).payload
        self.assertEqual(payload["size"], 1)
        self.assertEqual(payload["open"], 0)

    def test_queries_are_counted(self):
        self.application.debug_query("snapshot")
        self.assertEqual(self.application.stats["queries"], 1)


class TestResultSummary(unittest.TestCase):
    def test_applied_result_shows_its_payload(self):
        text = summarize_result(CommandResult.ok("r1", {"moved": 3}))
        self.assertIn("ok", text)
        self.assertIn("moved", text)

    def test_failed_result_shows_its_reason_codes(self):
        """失败摘要给**原因码**而不是一句「失败了」：日志里要能直接 grep。"""
        text = summarize_result(
            CommandResult.blocked("r1", [BlockReason(Reason.BROWNOUT, "能源不足")])
        )
        self.assertIn("blocked", text)
        self.assertIn(Reason.BROWNOUT, text)

    def test_failure_without_a_reason_says_so(self):
        self.assertIn("无原因", summarize_result(CommandResult.blocked("r1", [])))


class TestApplicationWiring(unittest.TestCase):
    """应用层的装配来自 bootstrap：档位与策略必须与运行时一致。"""

    def test_application_uses_the_assembly_profile(self):
        server = ServerBootstrap(world=FakeWorld())
        self.assertEqual(server.application.profile, server.assembly_profile)
        self.assertEqual(server.application.profile, AssemblyProfile.DEV)

    def test_application_shares_the_admission_policy(self):
        """两级读的必须是同一份策略：不然「未定规则」会在两处各说一套。"""
        server = ServerBootstrap(world=FakeWorld())
        self.assertIs(server.application.policy, server.admission.policy)

    def test_diagnostics_expose_the_new_sections(self):
        report = ServerBootstrap(world=FakeWorld()).diagnostics()
        for section in ("assembly", "runtimes", "application"):
            self.assertIn(section, report)
        self.assertIn(AssemblyProfile.DEV, report["scope_note"])
        self.assertIn("service_absent", report["scope_note"])

    def test_application_service_can_be_built_standalone(self):
        """单独构造（测试与将来的工具链用），不依赖 bootstrap 的其余部分。"""
        server = ServerBootstrap(world=FakeWorld())
        app = ApplicationService(
            server.runtimes, server.topology, server.admission, registry=server.registry
        )
        self.assertEqual(app.profile, server.assembly_profile)
        self.assertEqual(app.tick(8), 0)


if __name__ == "__main__":
    unittest.main()
