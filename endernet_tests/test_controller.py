# coding=utf-8
"""任务 03-02 至 03-05、03-07 的离线验收：准入门链、去重账本与只读状态。

覆盖 03 验收清单里「命令能不能做、为什么不能做」的部分：

- 准入门链**逐条**各有用例。其中「主控实例已加载」与「拓扑判定」两条是
  `plans/tasks/01-交付说明.md` 第八节点名要 03 明确的第一项契约：
  02 的 `validate()` 在控制器区块卸载而目标设备仍加载时**会放行**，
  业务不能据此开工。`TestMasterLoaded` 里有一条用例先把这个行为钉住，
  再用下一条证明主控确实挡住了。
- **未确认不等于可以**：带宽为 `unknown` 时不放行。这里用的是真实存在
  的那类设备（`parallel_processor_connector`，`blocks.md` 未列其带宽占用），
  不是编造出来的状态。
- 同一条请求只执行一次；被拒的请求不留在途标记。
- 只读状态在无主控、主控卸载、设备已拆除时同样可读；控制器本身不适用
  设备状态栏。

**这里的服务是记录型测试双件**：只数调用次数、不假装有库存。09 的真实替身
（业务一律拒绝）在 `test_services.py` 与 `test_bootstrap.py` 里验，能源的真实
判定在 `test_energy.py` 里验——**本文件一律不等真装配的能源结论**，因为准入
链要测的是「拿到某个结论之后怎么走」，而不是结论本身怎么来的。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.identity import ChunkKey, DeviceRef, NodeKey
from endernet.common.protocol import (
    BlockReason,
    CommandKind,
    CommandResult,
    CommandSource,
    DeviceCommand,
    Reason,
    STATUS_BLOCKED,
    STATUS_IN_FLIGHT,
    STATUS_OK,
    STATUS_REJECTED,
    STATUS_STALE,
)
from endernet.server.bootstrap import ServerBootstrap
from endernet.server.network.admission import BandwidthState
from endernet.server.network.controller import (
    LedgerPolicy,
    MasterController,
    RequestLedger,
    RunState,
    compose_device_status,
    run_state_of,
)
from endernet.server.network.services import (
    AssemblyProfile,
    NetworkService,
    ServiceFactory,
    ServiceId,
    StandInService,
)
from endernet.server.runtime.events import NormalizedEvent

BB8 = "endernet:network_baseband_8"
TINY = "endernet:tiny_network_endpoint"
DOS = "endernet:dos_network_endpoint"
DRIVER = "endernet:storage_driver"
#: `blocks.md` 没有给出这个型号的带宽占用，因此它的带宽判定是「未知」
#: 而不是 0。用来验证「未确认不等于可以」。
CONNECTOR = "endernet:parallel_processor_connector"


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


# -- 记录型测试双件 --------------------------------------------------------


class RecordingService(NetworkService):
    """记录型双件：**只在拿到许可后才被调用**，且不假装有库存。

    `calls` 存的是 `request_id`。验收要求「输入、输出、玩家操作与订单
    任务确实通过主控」，证据就是：没过准入时它一次都没被调用过；
    过了准入时它是被调用的那一个。
    """

    #: 计数用：每次成功处理搬动的数量。验证重复请求只搬一次。
    moved = 0

    def __init__(self, service_id, requires=()):
        # type: (str, tuple) -> None
        self.service_id = service_id
        self.requires = tuple(requires)
        self.calls = []
        self.mounted = []
        self.removed = []
        self.ticks = 0
        self.moved = 0
        #: 挂载时收到的局部恢复数据，键是节点。
        self.saved_seen = {}

    def add_node(self, node, saved_data=None):
        # type: (object, dict) -> bool
        self.saved_seen[node.node_key] = saved_data
        if node.node_key in self.mounted:
            return False
        self.mounted.append(node.node_key)
        return True

    def remove_node(self, node):
        # type: (object) -> bool
        if node.node_key in self.mounted:
            self.mounted.remove(node.node_key)
            self.removed.append(node.node_key)
            return True
        return False

    def tick(self, context, budget):
        # type: (dict, int) -> int
        self.ticks += 1
        return 1

    def handle(self, grant, command):
        # type: (object, object) -> object
        self.calls.append(command.request_id)
        self.moved += 1
        return CommandResult.ok(
            command.request_id, {"by": self.service_id, "moved": self.moved}
        )

    def save_node_data(self, node):
        # type: (object) -> object
        return {"service": self.service_id}


class RecordingEnergy(RecordingService):
    """能源双件。`power` 取 True/False/None 分别造出有电、断电、待确认。"""

    def __init__(self, service_id, power=True):
        # type: (str, object) -> None
        RecordingService.__init__(self, service_id)
        self.power = power

    def power_ok(self, node_key):
        # type: (object) -> object
        return self.power


class NotReadyService(RecordingService):
    """已装配、未就绪。给用户的信息是 `service_not_ready`，不是「没写」。"""

    def is_ready(self):
        # type: () -> bool
        return False


# -- 夹具 ------------------------------------------------------------------


class ControllerFixture(unittest.TestCase):
    """真实装配 + 记录型服务。

    布局跨两个区块，是刻意的：`TestMasterLoaded` 要能只卸载控制器所在的
    那个区块，而让目标设备仍处于加载状态——那正是 02 的 `validate()`
    会放行、而业务不能开工的缝隙。

        (15,64,0) 基带 ← (16,64,0) 基带 ← (16,65,0) 控制器   [区块 (1,0)]
          ↑
        (15,64,1) 存储驱动器                                  [区块 (0,0)]
    """

    #: 控制器型号。改成 DOS 可以造出「主控不支持该设备」。
    controller_device = TINY
    #: 目标设备型号。
    target_device = DRIVER
    #: 能源服务给出的答案。
    energy_power = True
    assembly_profile = AssemblyProfile.DEV

    BB_A = key(15, 64, 0)
    BB_B = key(16, 64, 0)
    CONTROLLER = key(16, 65, 0)
    TARGET = key(15, 64, 1)

    def setUp(self):
        self.world = FakeWorld()
        self.factories = ServiceFactory(self.builders())
        self.server = ServerBootstrap(
            world=self.world,
            service_factories=self.factories,
            assembly_profile=self.assembly_profile,
        )
        self.server.start(bind_engine=False)
        self.build_network()

    def builders(self):
        # type: () -> dict
        """三个槽位换成记录型服务，**工厂槽位显式装一个替身**。

        **为什么是显式的替身而不是「保持默认」**：任务 09 之后默认表里的
        工厂已经是正式实现，而它在这个夹具里 `is_ready()` 为假（没有规划器、
        没有样板索引），于是 `order` 命令会得到 `service_not_ready` 而不是
        `service_absent`——那是另一条闸门、另一个原因码。夹具要验的是
        「这条命令要的那项服务不可用」，因此把「不可用」这件事**明写出来**，
        而不是靠默认表恰好是什么样。
        """
        return {
            ServiceId.STORAGE: lambda runtime: self.make_service(ServiceId.STORAGE),
            ServiceId.SCHEDULER: lambda runtime: self.make_service(ServiceId.SCHEDULER),
            ServiceId.ENERGY: lambda runtime: RecordingEnergy(
                ServiceId.ENERGY, self.energy_power
            ),
            ServiceId.FACTORY: lambda runtime: StandInService(
                ServiceId.FACTORY, "09", "订单与执行"
            ),
        }

    def make_service(self, service_id):
        # type: (str) -> NetworkService
        return RecordingService(service_id)

    # -- 世界操作 ------------------------------------------------------

    def drive(self, *events):
        for event in events:
            self.server.push(event)
        self.server.tick()

    def build_network(self):
        self.drive(
            NormalizedEvent.place(self.BB_A, BB8),
            NormalizedEvent.place(self.BB_B, BB8),
            NormalizedEvent.place(self.CONTROLLER, self.controller_device),
            NormalizedEvent.place(self.TARGET, self.target_device),
        )

    def place(self, position, device_id):
        self.drive(NormalizedEvent.place(position, device_id))

    def remove(self, position, device_id):
        self.drive(NormalizedEvent.remove(position, device_id))

    def unload_chunk(self, position):
        chunk = ChunkKey(0, position.x // 16, position.z // 16)
        self.drive(NormalizedEvent.chunk_unloaded(chunk))

    # -- 查询 ----------------------------------------------------------

    def ref_of(self, position):
        record = self.server.registry.get(position)
        self.assertIsNotNone(record, "位置 %s 没有设备" % (position.as_key(),))
        return record.ref

    def runtime(self):
        runtimes = self.server.runtimes.runtimes()
        self.assertEqual(len(runtimes), 1, "本夹具只应有一个网络")
        return runtimes[0]

    def master(self):
        # type: () -> MasterController
        master = self.runtime().master
        self.assertIsNotNone(master, "夹具期望该网络有主控")
        return master

    def service(self, service_id):
        # type: (str) -> NetworkService
        return self.runtime().services.get(service_id)

    def target_ref(self):
        return self.ref_of(self.TARGET)

    def command(self, kind=CommandKind.STORAGE, request_id="r1", ref=None, **kwargs):
        # type: (str, str, DeviceRef, dict) -> DeviceCommand
        target = ref if ref is not None else self.target_ref()
        source = CommandSource(
            kwargs.pop("player_id", "p1"),
            kwargs.pop("source_ref", target),
            kwargs.pop("dimension", 0),
            distance_ok=kwargs.pop("distance_ok", True),
            session_id=kwargs.pop("session_id", ""),
        )
        return DeviceCommand(
            request_id,
            kind,
            source,
            payload=kwargs.pop("payload", None),
            topology_version=kwargs.pop("topology_version", None),
            device_ref=target,
        )

    def submit(self, command=None, **kwargs):
        if command is None:
            command = self.command(**kwargs)
        return self.server.submit_command(command)

    # -- 断言辅助 ------------------------------------------------------

    def assertBlocked(self, result, code):
        self.assertEqual(result.status, STATUS_BLOCKED, result.to_dict())
        self.assertIn(code, result.reason_codes(), result.to_dict())
        return result

    def assertRejected(self, result, code):
        self.assertEqual(result.status, STATUS_REJECTED, result.to_dict())
        self.assertIn(code, result.reason_codes(), result.to_dict())
        return result

    def assertStale(self, result, code):
        self.assertEqual(result.status, STATUS_STALE, result.to_dict())
        self.assertIn(code, result.reason_codes(), result.to_dict())
        return result


# -- 第 1 条：命令形状 -----------------------------------------------------


class TestChainShape(ControllerFixture):
    def test_known_command_is_admitted(self):
        result = self.submit()
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertEqual(result.payload["by"], ServiceId.STORAGE)

    def test_unknown_kind_is_rejected(self):
        """未知类型不兜底推断：猜一个服务出来就等于让设备绕过准入。"""
        result = self.submit(self.command(kind="teleport"))
        self.assertRejected(result, Reason.BAD_PARAMETER)

    def test_missing_target_is_rejected(self):
        source = CommandSource("p1", None, 0)
        result = self.submit(DeviceCommand("r1", CommandKind.STORAGE, source))
        self.assertRejected(result, Reason.BAD_PARAMETER)

    def test_missing_source_is_rejected(self):
        """主控收到的命令来源为空：那是编程错误，但回报要是可读的。"""
        command = DeviceCommand(
            "r1", CommandKind.STORAGE, None, device_ref=self.target_ref()
        )
        result = self.master().submit(command)
        self.assertRejected(result, Reason.BAD_PARAMETER)

    def test_empty_request_id_is_refused_at_construction(self):
        """形状检查的第一道在构造期：没有 request_id 就无法去重。"""
        with self.assertRaises(ValueError):
            DeviceCommand("", CommandKind.STORAGE, CommandSource("p1", None, 0))


# -- 第 2 条：去重账本 -----------------------------------------------------


class TestChainDedup(ControllerFixture):
    def test_same_request_runs_once(self):
        """验收：同一条请求只产生一次搬运。"""
        storage = self.service(ServiceId.STORAGE)
        first = self.submit(request_id="dup")
        second = self.submit(request_id="dup")
        self.assertEqual(first.status, STATUS_OK, first.to_dict())
        self.assertEqual(second.status, STATUS_OK, second.to_dict())
        self.assertEqual(storage.calls, ["dup"], "重复请求又执行了一次")
        self.assertEqual(storage.moved, 1)

    def test_replay_is_marked(self):
        self.submit(request_id="dup")
        replayed = self.submit(request_id="dup")
        self.assertTrue(replayed.payload["replayed"])
        self.assertIn(Reason.DUPLICATE_REQUEST, replayed.reason_codes())
        self.assertEqual(replayed.payload["moved"], 1, "重放的应是原结果")

    def test_different_request_runs_again(self):
        storage = self.service(ServiceId.STORAGE)
        self.submit(request_id="a")
        self.submit(request_id="b")
        self.assertEqual(storage.calls, ["a", "b"])

    def test_key_includes_the_source_device(self):
        """键是 `(request_id, 来源设备实例)`：别的来源用同一个号不算重复。"""
        storage = self.service(ServiceId.STORAGE)
        self.submit(request_id="same")
        other = self.ref_of(self.CONTROLLER)
        self.submit(self.command(request_id="same", source_ref=other))
        self.assertEqual(storage.calls, ["same", "same"])

    def test_refused_requests_do_not_enter_the_ledger(self):
        """被拒的请求没有副作用，不该让它拿同一个号永远重试不成功。"""
        self.unload_chunk(self.CONTROLLER)
        self.assertBlocked(self.submit(request_id="blocked"), Reason.MASTER_UNLOADED)
        self.assertEqual(len(self.master().ledger), 0)

    def test_in_flight_is_reported_for_a_reentrant_request(self):
        """处理中又来了同号请求：报 `in_flight`，而不是再执行一遍。

        服务在处理一条请求时向上游回问同一件事，是很自然的形态。
        """
        storage = self.service(ServiceId.STORAGE)
        master = self.master()
        command = self.command(request_id="nested")
        nested = []
        original_handle = storage.handle

        def handle(grant, cmd):
            nested.append(master.submit(command))
            return original_handle(grant, cmd)

        storage.handle = handle
        result = self.submit(command)
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertEqual(len(nested), 1)
        self.assertEqual(nested[0].status, STATUS_IN_FLIGHT, nested[0].to_dict())
        self.assertEqual(storage.calls, ["nested"])


class TestLedgerUnit(unittest.TestCase):
    """账本本身的三态与保留期。不经过主控，直接验机制。"""

    def setUp(self):
        self.ledger = RequestLedger(LedgerPolicy(retention_ticks=10))

    def test_begin_three_states(self):
        key_ = ("r1", "dev")
        self.assertEqual(self.ledger.begin(key_, 0), RequestLedger.BEGIN_STARTED)
        self.assertEqual(self.ledger.begin(key_, 1), RequestLedger.BEGIN_IN_FLIGHT)
        self.ledger.finish(key_, CommandResult.ok("r1"), 2)
        self.assertEqual(self.ledger.begin(key_, 3), RequestLedger.BEGIN_REPLAYED)

    def test_abort_clears_the_in_flight_marker(self):
        key_ = ("r1", "dev")
        self.ledger.begin(key_, 0)
        self.assertTrue(self.ledger.abort(key_))
        self.assertEqual(self.ledger.begin(key_, 1), RequestLedger.BEGIN_STARTED)

    def test_only_applied_results_are_recorded(self):
        key_ = ("r1", "dev")
        self.ledger.begin(key_, 0)
        self.assertFalse(self.ledger.finish(key_, CommandResult.blocked("r1", []), 1))
        self.assertEqual(len(self.ledger), 0, "被阻塞的结果不该留成终局")

    def test_prune_drops_expired_entries(self):
        key_ = ("r1", "dev")
        self.ledger.begin(key_, 0)
        self.ledger.finish(key_, CommandResult.ok("r1"), 0)
        self.assertEqual(self.ledger.prune(5), 0, "保留期内不得清理")
        self.assertEqual(self.ledger.prune(20), 1)
        self.assertEqual(len(self.ledger), 0)

    def test_expired_request_may_run_again(self):
        """保留期过了就允许重发：账本防的是重复执行，不是永远记住。"""
        key_ = ("r1", "dev")
        self.ledger.begin(key_, 0)
        self.ledger.finish(key_, CommandResult.ok("r1"), 0)
        self.ledger.prune(100)
        self.assertEqual(self.ledger.begin(key_, 100), RequestLedger.BEGIN_STARTED)

    def test_max_entries_evicts_oldest(self):
        ledger = RequestLedger(LedgerPolicy(retention_ticks=10000, max_entries=3))
        for index in range(5):
            ledger.begin(("r%d" % index, "dev"), index)
        self.assertEqual(len(ledger), 3)
        self.assertEqual(ledger.keys(), [("r2", "dev"), ("r3", "dev"), ("r4", "dev")])

    def test_export_import_round_trip(self):
        """留给任务 12 的钩子：结构必须能原样往返。"""
        key_ = ("r1", "dev")
        self.ledger.begin(key_, 0)
        self.ledger.finish(key_, CommandResult.ok("r1", {"x": 1}), 3)

        other = RequestLedger()
        self.assertEqual(other.import_state(self.ledger.export_state()), 1)
        self.assertEqual(other.replay(key_).payload, {"x": 1})

    def test_import_refuses_unknown_version(self):
        """未知的**更新**版本原样保留、标记只读，不得读成空值再覆盖。"""
        with self.assertRaises(ValueError):
            self.ledger.import_state({"schema_version": 999, "entries": []})

    def test_scope_and_retention_are_declared(self):
        """范围与期限要有明确交代，不能靠读代码猜（03-05 的要求）。"""
        described = LedgerPolicy().describe()
        self.assertEqual(described["scope"], "master_instance")
        self.assertFalse(described["persisted"], "本次不接线，不能写成已持久化")


# -- 第 3 条：来源 ---------------------------------------------------------


class TestChainSource(ControllerFixture):
    def test_out_of_range_is_rejected(self):
        result = self.submit(self.command(distance_ok=False))
        self.assertRejected(result, Reason.OUT_OF_RANGE)

    def test_session_check_is_invoked(self):
        """有校验器时才会出现 `SESSION_INVALID`；没有就不假装检查过。"""
        checked = []

        def session_check(source):
            checked.append(source.session_id)
            return False

        self.master().session_check = session_check
        result = self.submit(self.command(session_id="s1"))
        self.assertRejected(result, Reason.SESSION_INVALID)
        self.assertEqual(checked, ["s1"])

    def test_no_session_check_means_no_session_rejection(self):
        """会话层属任务 11；在那之前不能在这里假装有一张会话表。"""
        result = self.submit(self.command(session_id=""))
        self.assertEqual(result.status, STATUS_OK, result.to_dict())


# -- 第 4 条：目标属于本网络 -----------------------------------------------


class TestChainRouting(ControllerFixture):
    def setUp(self):
        ControllerFixture.setUp(self)
        # 第二个互相独立的分量：位置远离，中间没有基带相连。
        self.OTHER_BB = key(100, 64, 0)
        self.OTHER_CTRL = key(100, 65, 0)
        self.OTHER_DEV = key(101, 64, 0)
        self.drive(
            NormalizedEvent.place(self.OTHER_BB, BB8),
            NormalizedEvent.place(self.OTHER_CTRL, TINY),
            NormalizedEvent.place(self.OTHER_DEV, DRIVER),
        )

    def test_two_independent_networks(self):
        """验收：每个独立网络拥有独立主控。"""
        runtimes = self.server.runtimes.runtimes()
        self.assertEqual(len(runtimes), 2)
        masters = set(item.master.instance_id for item in runtimes)
        self.assertEqual(len(masters), 2, "两个网络不该共用同一个主控实例")

    def test_independent_services_per_network(self):
        """验收：每个独立网络拥有独立的服务状态。"""
        runtimes = self.server.runtimes.runtimes()
        storages = set(id(item.services.get(ServiceId.STORAGE)) for item in runtimes)
        self.assertEqual(len(storages), 2, "两个运行时不该共用同一个服务实例")
        runtimes[0].services.get(ServiceId.STORAGE).moved = 7
        self.assertEqual(runtimes[1].services.get(ServiceId.STORAGE).moved, 0)

    def test_wrong_network_is_rejected(self):
        """「走错门」是拒绝而不是阻塞：命令本身没问题，只是不该发给这个主控。"""
        other_ref = self.ref_of(self.OTHER_DEV)
        # 直连第一个网络的主控：走应用层会被路由到正确的那一个，
        # 就测不到「送错门」这件事了。
        home = self.server.runtimes.runtime_for(self.target_ref())
        result = home.master.submit(self.command(ref=other_ref, request_id="cross"))
        self.assertRejected(result, Reason.WRONG_NETWORK)

    def test_application_routes_to_the_right_master(self):
        """路由由应用层负责：送错门的命令不该由错误的主控执行。"""
        other_ref = self.ref_of(self.OTHER_DEV)
        result = self.server.submit_command(
            self.command(request_id="routed", ref=other_ref, source_ref=other_ref)
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        owner = self.server.runtimes.runtime_for(other_ref)
        for runtime in self.server.runtimes.runtimes():
            calls = runtime.services.get(ServiceId.STORAGE).calls
            if runtime is owner:
                self.assertEqual(calls, ["routed"])
            else:
                self.assertEqual(calls, [], "命令被送给了不属于它的主控")


# -- 第 5 条：主控实例存在且已加载（01 契约 #1） ---------------------------


class TestMasterLoaded(ControllerFixture):
    def test_topology_still_allows_when_the_master_chunk_is_unloaded(self):
        """先钉住 02 的行为：这正是 03 必须补上的那个缝隙。

        控制器区块卸载而目标设备仍加载时，`validate()` 依然放行——
        那是有意的（不把「暂时访问不到」当成「已拆除」）。如果哪天 02
        改成拒绝，这条会失败，提醒我们第 5 条可以简化。
        """
        self.unload_chunk(self.CONTROLLER)
        self.assertFalse(self.master().is_loaded())
        self.assertTrue(self.server.registry.get_by_ref(self.target_ref()).loaded)
        verdict = self.server.topology.validate(self.target_ref(), None)
        self.assertTrue(verdict.allowed, "02 的 validate 在有意的放宽下应当放行")

    def test_command_is_blocked_when_the_master_chunk_is_unloaded(self):
        """但业务必须停下。这条与上一条成对，缺一不可。"""
        self.unload_chunk(self.CONTROLLER)
        result = self.submit(request_id="while-unloaded")
        self.assertBlocked(result, Reason.MASTER_UNLOADED)
        self.assertEqual(
            self.runtime().state(), RunState.MASTER_UNLOADED, "状态桶也要跟上"
        )

    def test_target_chunk_unload_is_reported_by_topology(self):
        """目标设备自己卸载是另一回事，由拓扑判定透传 `unloaded_member`。"""
        self.unload_chunk(self.TARGET)
        result = self.submit(request_id="target-unloaded")
        self.assertBlocked(result, Reason.UNLOADED_MEMBER)

    def test_destroying_the_controller_leaves_no_master(self):
        """拆掉控制器不是「卸载」：分量还在，但没人接管了。"""
        self.remove(self.CONTROLLER, self.controller_device)
        runtime = self.runtime()
        self.assertIsNone(runtime.master)
        result = self.submit(request_id="no-master")
        self.assertBlocked(result, Reason.NO_CONTROLLER)

    def test_runtime_is_kept_when_the_master_is_lost(self):
        """失去主控不等于运行时退役：库存与订单归属属于运行时。"""
        self.remove(self.CONTROLLER, self.controller_device)
        self.assertEqual(len(self.server.runtimes.runtimes()), 1)

    def test_recovering_master_blocks_business(self):
        self.master().begin_recovery()
        result = self.submit(request_id="recovering")
        self.assertBlocked(result, Reason.MASTER_RECOVERING)

    def test_recovery_does_not_block_status(self):
        """03 验收末条：主控恢复之前仍允许状态视图与必要的恢复管理。"""
        self.master().begin_recovery()
        status = self.submit(self.command(kind=CommandKind.STATUS, request_id="s1"))
        self.assertEqual(status.status, STATUS_OK, status.to_dict())


# -- 第 6 条：图版本 -------------------------------------------------------


class TestChainVersion(ControllerFixture):
    def test_stale_version_is_refused(self):
        result = self.submit(
            self.command(topology_version=self.server.topology.version + 1)
        )
        self.assertStale(result, Reason.STALE_VERSION)

    def test_matching_version_passes(self):
        result = self.submit(self.command(topology_version=self.server.topology.version))
        self.assertEqual(result.status, STATUS_OK, result.to_dict())

    def test_version_may_be_omitted(self):
        """只读查询常不带版本；带了才必须对得上。"""
        result = self.submit(self.command(topology_version=None))
        self.assertEqual(result.status, STATUS_OK, result.to_dict())


# -- 第 7 条：拓扑判定 -----------------------------------------------------


class TestChainTopologyVerdict(ControllerFixture):
    def test_command_targeting_a_removed_device_is_stale(self):
        ref = self.target_ref()
        self.remove(self.TARGET, self.target_device)
        result = self.master().submit(self.command(ref=ref, request_id="gone"))
        self.assertStale(result, Reason.DEVICE_GONE)

    def test_topology_verdict_is_passed_through(self):
        """主控不改判拓扑的结论：`validate()` 说过期，它就说过期。"""
        ref = self.target_ref()
        self.remove(self.TARGET, self.target_device)
        result = self.master().submit(self.command(ref=ref, request_id="gone"))
        verdict = self.server.topology.validate(ref, None)
        self.assertEqual(result.status, verdict.status)
        self.assertEqual(
            result.reason_codes(), tuple(reason.code for reason in verdict.reasons)
        )


# -- 第 8 条：网络运行状态 -------------------------------------------------


class TestRunState(ControllerFixture):
    def test_status_codes_are_stable_strings(self):
        """状态码是界面与日志的契约，拼写不能随手改。"""
        self.assertEqual(RunState.NO_CONTROLLER, "no_controller")
        self.assertEqual(RunState.RUNNABLE, "runnable")
        for state in sorted(RunState.ALL):
            self.assertIn(state, RunState.TEXT)

    def test_run_state_from_blockers(self):
        self.assertEqual(run_state_of(()), RunState.RUNNABLE)
        self.assertEqual(
            run_state_of((BlockReason(Reason.BROWNOUT, ""),)), RunState.NO_POWER
        )
        self.assertEqual(
            run_state_of((BlockReason(Reason.SERVICE_ABSENT, ""),)),
            RunState.SERVICE_ABSENT,
        )
        self.assertEqual(
            run_state_of((BlockReason(Reason.NO_CONTROLLER, ""),)),
            RunState.NO_CONTROLLER,
        )

    def test_rebuilding_outranks_no_controller(self):
        """重建期间控制器计数本来就作数不了，先报「没有」会把待确认说成确认。

        这条顺序是 `_BLOCKER_STATES` 的用意，写下来防止有人按字母序"整理"。
        """
        self.assertEqual(
            run_state_of(
                (
                    BlockReason(Reason.NO_CONTROLLER, ""),
                    BlockReason(Reason.REBUILDING, ""),
                )
            ),
            RunState.REBUILDING,
        )

    def test_default_assembly_makes_the_network_unavailable(self):
        """默认装配下网络不可用——这是诚实的结果，但**理由变了**。

        能源已是正式实现，因此拦住它的不再是「服务尚未实现」，而是库默认
        口径给出的「能源判定尚未确认」：基带耗能未列（R10）、工作态与装置
        数量还没接线，严格说算不出这台网络要多少电。游戏内由
        `start_server()` 显式换成 DEV 口径，理由写在那个函数里。
        """
        runtime = self.plain_server().runtimes.runtimes()[0]
        self.assertEqual(runtime.state(), RunState.NO_POWER)
        self.assertIn(Reason.ENERGY_UNAVAILABLE, [r.code for r in runtime.blockers()])

    def test_multiple_blockers_coexist(self):
        """一个网络可以同时「某个服务还没有」又「能源待确认」。

        **不截断成第一条**：原因清单是可定位的清单，界面不该从状态字符串
        反推原因（03-07）。

        两件事本来就可能同时发生，而且要把人引向完全不同的排查方向
        （去看装配表 vs 去看供能与定义表），因此必须能并列出现。
        """
        server = self.layout(
            ServiceFactory(
                {
                    ServiceId.SCHEDULER: lambda runtime: StandInService(
                        ServiceId.SCHEDULER, "04"
                    ),
                    ServiceId.ENERGY: lambda runtime: RecordingEnergy(
                        ServiceId.ENERGY, None
                    ),
                }
            )
        )
        blockers = server.runtimes.runtimes()[0].master.blockers()
        codes = [reason.code for reason in blockers]
        self.assertGreaterEqual(len(blockers), 2)
        self.assertIn(Reason.SERVICE_ABSENT, codes)
        self.assertIn(Reason.ENERGY_UNAVAILABLE, codes)

    def test_energy_pending_does_not_grant(self):
        """未确认不等于有电。"""
        master = self.recording_server(None).runtimes.runtimes()[0].master
        self.assertEqual(master.state(), RunState.NO_POWER)
        self.assertIn(Reason.ENERGY_UNAVAILABLE, [r.code for r in master.blockers()])

    def test_energy_false_is_a_brownout(self):
        master = self.recording_server(False).runtimes.runtimes()[0].master
        self.assertEqual(master.state(), RunState.NO_POWER)
        self.assertIn(Reason.BROWNOUT, [r.code for r in master.blockers()])

    def test_blocked_result_carries_the_reason(self):
        server = self.recording_server(False)
        target = server.registry.get(key(15, 64, 1)).ref
        result = server.submit_command(
            DeviceCommand(
                "r1",
                CommandKind.STORAGE,
                CommandSource("p1", target, 0),
                device_ref=target,
            )
        )
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn(Reason.BROWNOUT, result.reason_codes())

    # -- 装配辅助 ------------------------------------------------------

    def plain_server(self):
        # type: () -> ServerBootstrap
        """原样默认装配：能源是正式实现、库默认（保守）口径，工厂仍是替身。

        **不是「全替身的默认装配」**——那句话在能源落地之后就不成立了。
        """
        return self.layout(ServiceFactory())

    def recording_server(self, power):
        # type: (object) -> ServerBootstrap
        """三个槽位换成记录型双件、能源给出指定答案。"""
        return self.layout(
            ServiceFactory(
                {
                    ServiceId.STORAGE: lambda runtime: RecordingService(ServiceId.STORAGE),
                    ServiceId.FACTORY: lambda runtime: RecordingService(ServiceId.FACTORY),
                    ServiceId.SCHEDULER: lambda runtime: RecordingService(
                        ServiceId.SCHEDULER
                    ),
                    ServiceId.ENERGY: lambda runtime: RecordingEnergy(
                        ServiceId.ENERGY, power
                    ),
                }
            )
        )

    def layout(self, factory):
        # type: (object) -> ServerBootstrap
        server = ServerBootstrap(world=FakeWorld(), service_factories=factory)
        server.start(bind_engine=False)
        for event in (
            NormalizedEvent.place(key(15, 64, 0), BB8),
            NormalizedEvent.place(key(16, 64, 0), BB8),
            NormalizedEvent.place(key(16, 65, 0), TINY),
            NormalizedEvent.place(key(15, 64, 1), DRIVER),
        ):
            server.push(event)
        server.tick()
        return server


# -- 第 9 条：主控型号支持 -------------------------------------------------


class TestControllerServes(ControllerFixture):
    """DOS 控制器能控制什么，是 `definitions.py` 里的显式清单，不按名称推断。

    DOS **确实**认存储驱动器与基带（`allowed_device_ids` 里都有），
    所以夹具的目标设备换成它不认的那个：并行处理器连接器。
    """

    controller_device = DOS
    target_device = CONNECTOR

    def test_the_network_is_otherwise_runnable(self):
        """必须先过第 8 条，否则下面那条测的就不是第 9 条。"""
        self.assertEqual(self.runtime().state(), RunState.RUNNABLE)
        info = self.server.catalog.controller_serves(DOS, CONNECTOR)
        self.assertFalse(info.allowed, "夹具依赖 DOS 不认连接器")
        self.assertEqual(info.reason, "controller_does_not_accept_device")

    def test_dos_controller_refuses_devices_it_does_not_control(self):
        """「拥有主控」不等于「支持全部功能」。

        那台设备连得上、也有主控，但主控的型号不支持它。这是**拒绝**
        而不是阻塞：命令本身没问题，只是不该发给这个主控。
        """
        result = self.submit(request_id="dos")
        self.assertRejected(result, Reason.CONTROLLER_DOES_NOT_SERVE)


# -- 第 10 条：装配档位 ----------------------------------------------------


class TestAssemblyProfileGuard(ControllerFixture):
    assembly_profile = AssemblyProfile.PRODUCTION

    def test_production_with_unresolved_policy_refuses_business(self):
        """01 交付说明第八节的第二项契约：替身数值不得流入正式装配。

        DEV 档下这是「记录」，PRODUCTION 档下是拒绝——两种档位的差别
        必须能被观察到，否则档位就只是个装饰。
        """
        result = self.submit(request_id="prod")
        self.assertBlocked(result, Reason.ASSEMBLY_UNRESOLVED)

    def test_status_is_still_readable_in_production(self):
        """档位拦的是业务，不是诊断：出问题时恰恰最需要能查。"""
        result = self.submit(self.command(kind=CommandKind.STATUS, request_id="s"))
        self.assertEqual(result.status, STATUS_OK, result.to_dict())

    def test_dev_profile_is_not_blocked_by_the_same_policy(self):
        """对照组：同一份未定策略在 DEV 档下不拦业务。"""
        dev = self.dev_server()
        target = dev.registry.get(self.TARGET).ref
        result = dev.submit_command(
            self.command(request_id="dev", ref=target, source_ref=target)
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())

    def dev_server(self):
        # type: () -> ServerBootstrap
        server = ServerBootstrap(
            world=FakeWorld(),
            service_factories=self.factories,
            assembly_profile=AssemblyProfile.DEV,
        )
        server.start(bind_engine=False)
        for event in (
            NormalizedEvent.place(self.BB_A, BB8),
            NormalizedEvent.place(self.BB_B, BB8),
            NormalizedEvent.place(self.CONTROLLER, self.controller_device),
            NormalizedEvent.place(self.TARGET, self.target_device),
        ):
            server.push(event)
        server.tick()
        return server


# -- 第 11 条：设备带宽 ----------------------------------------------------


class TestChainBandwidth(ControllerFixture):
    #: 紧挨着基带、带宽占用在 `blocks.md` 中未列出的设备（R10）。
    UNKNOWN = key(14, 64, 0)

    def setUp(self):
        ControllerFixture.setUp(self)
        self.place(self.UNKNOWN, CONNECTOR)

    def test_the_device_really_is_unconfirmed(self):
        """先确认夹具造出的确实是「未知」而不是「拒绝」，否则下面两条无意义。"""
        status = self.server.admission.status_of(self.ref_of(self.UNKNOWN))
        self.assertEqual(status.state, BandwidthState.UNKNOWN)
        self.assertIsNone(status.ok)

    def test_unconfirmed_bandwidth_does_not_grant(self):
        """`unknown` 既不是 0 也不是「没有需求」，更不是可以。"""
        result = self.submit(self.command(ref=self.ref_of(self.UNKNOWN), request_id="bw"))
        self.assertBlocked(result, Reason.BANDWIDTH_UNKNOWN)
        self.assertIn(Reason.DEFINITION_UNCONFIGURED, result.reason_codes())

    def test_granted_bandwidth_passes(self):
        status = self.server.admission.status_of(self.target_ref())
        self.assertEqual(status.state, BandwidthState.GRANTED)
        self.assertIs(status.ok, True)


# -- 第 12 条：所需服务可取 ------------------------------------------------


class TestChainRouteBlocker(ControllerFixture):
    def test_order_without_a_factory_service_is_blocked(self):
        """`ORDER` 要的是工厂服务，而这张网络的工厂是不可用的（夹具装的是替身）。

        **这条验的是闸门 12 的形状**，与「09 实现到什么程度」无关：
        替身与「装了但没就绪」给的是两个不同的原因码（`service_absent` 与
        `service_not_ready`），而对玩家都是同一句话——这条命令现在发不出去。
        """
        result = self.submit(self.command(kind=CommandKind.ORDER, request_id="o1"))
        self.assertBlocked(result, Reason.SERVICE_ABSENT)

    def test_only_the_route_the_command_needs_is_checked(self):
        """`STORAGE` 不该被工厂服务拖累：按命令逐条算，不按「全部服务」。"""
        self.assertTrue(self.service(ServiceId.FACTORY).is_stand_in)
        result = self.submit(request_id="storage-only")
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertEqual(self.service(ServiceId.STORAGE).calls, ["storage-only"])

    def test_not_ready_is_distinguished_from_absent(self):
        """「没写」与「写了但还不能用」给用户的信息不同。"""
        factory = ServiceFactory(
            {
                ServiceId.STORAGE: lambda runtime: NotReadyService(ServiceId.STORAGE),
                ServiceId.FACTORY: lambda runtime: RecordingService(ServiceId.FACTORY),
                ServiceId.SCHEDULER: lambda runtime: RecordingService(ServiceId.SCHEDULER),
                ServiceId.ENERGY: lambda runtime: RecordingEnergy(ServiceId.ENERGY, True),
            }
        )
        server = ServerBootstrap(world=FakeWorld(), service_factories=factory)
        server.start(bind_engine=False)
        for event in (
            NormalizedEvent.place(key(15, 64, 0), BB8),
            NormalizedEvent.place(key(16, 64, 0), BB8),
            NormalizedEvent.place(key(16, 65, 0), TINY),
            NormalizedEvent.place(key(15, 64, 1), DRIVER),
        ):
            server.push(event)
        server.tick()
        target = server.registry.get(key(15, 64, 1)).ref
        result = server.submit_command(
            DeviceCommand(
                "r1",
                CommandKind.STORAGE,
                CommandSource("p1", target, 0),
                device_ref=target,
            )
        )
        self.assertEqual(result.status, STATUS_BLOCKED, result.to_dict())
        self.assertIn(Reason.SERVICE_NOT_READY, result.reason_codes())
        self.assertNotIn(Reason.SERVICE_ABSENT, result.reason_codes())


# -- 只读路径 --------------------------------------------------------------


class TestReadOnlyPath(ControllerFixture):
    def test_status_needs_no_bandwidth_or_service(self):
        """只读命令走到运行状态那一步就返回快照，不查型号支持与服务。"""
        result = self.submit(self.command(kind=CommandKind.STATUS, request_id="s"))
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertIn("run_state", result.payload)

    def test_status_does_not_touch_the_ledger(self):
        """重复读取没有副作用，缓存快照只会让终端显示过期数据。"""
        for _ in range(3):
            self.submit(self.command(kind=CommandKind.STATUS, request_id="same"))
        self.assertEqual(len(self.master().ledger), 0)

    def test_recovery_report_is_read_only(self):
        result = self.submit(
            self.command(
                kind=CommandKind.RECOVERY, request_id="rec", payload={"op": "report"}
            )
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertIn("instance_id", result.payload)

    def test_recovery_refuses_anything_but_report(self):
        """恢复入口不能变成绕过运行条件的写入通道。"""
        result = self.submit(
            self.command(
                kind=CommandKind.RECOVERY, request_id="rec", payload={"op": "restock"}
            )
        )
        self.assertRejected(result, Reason.UNSUPPORTED)

    def test_status_is_available_without_any_master(self):
        """没有主控时状态仍要能查——那时界面最需要原因。"""
        orphan = key(300, 64, 0)
        self.place(orphan, DRIVER)
        ref = self.ref_of(orphan)
        runtime = self.server.runtimes.runtime_for(ref)
        self.assertIsNotNone(runtime, "单独一台设备也应归属到一个分量")
        self.assertIsNone(runtime.master)

        result = self.server.submit_command(
            DeviceCommand(
                "s1", CommandKind.STATUS, CommandSource("p1", ref, 0), device_ref=ref
            )
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertIsNone(result.payload["master_instance_id"])
        self.assertEqual(result.payload["run_state"], RunState.NO_CONTROLLER)

    def test_business_command_without_a_master_is_blocked(self):
        orphan = key(300, 64, 0)
        self.place(orphan, DRIVER)
        ref = self.ref_of(orphan)
        result = self.server.submit_command(
            DeviceCommand(
                "r1", CommandKind.STORAGE, CommandSource("p1", ref, 0), device_ref=ref
            )
        )
        self.assertBlocked(result, Reason.MASTER_ABSENT)

    def test_status_for_a_removed_device_is_still_answered(self):
        """设备已拆除时连运行时都没有：应用层要自己给答案，而不是返回 None。"""
        ref = self.target_ref()
        self.remove(self.TARGET, self.target_device)
        self.assertIsNone(self.server.runtimes.runtime_for(ref))
        result = self.server.submit_command(
            DeviceCommand(
                "s1", CommandKind.STATUS, CommandSource("p1", ref, 0), device_ref=ref
            )
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertIsNone(result.payload["component_id"])
        self.assertIn("device", result.payload)
        self.assertFalse(result.payload["device"]["online"])


# -- 03-07：设备总体状态 ---------------------------------------------------


class TestDeviceStatus(ControllerFixture):
    def test_online_when_everything_is_satisfied(self):
        status = self.server.application.device_status(self.target_ref())
        self.assertTrue(status.is_online, status.to_dict())
        self.assertEqual(status.state_text(), "在线")
        self.assertEqual(status.validity, "ok")
        self.assertEqual(status.master_instance_id, self.master().instance_id)
        self.assertEqual(status.reason_codes(), ())

    def test_only_two_texts_exist(self):
        """界面只需要绿或红：待确认也显示离线，原因在详情里说（blocks.md 第 9 条）。"""
        for ref in (self.target_ref(), self.ref_of(self.CONTROLLER)):
            text = self.server.application.device_status(ref).state_text()
            self.assertIn(text, ("在线", "离线"))

    def test_controller_does_not_use_the_device_status_bar(self):
        """控制器自己有界面，不用设备状态栏（blocks.md 第 5 条）。"""
        controller = self.server.application.device_status(self.ref_of(self.CONTROLLER))
        self.assertFalse(controller.ui_applicable)
        target = self.server.application.device_status(self.target_ref())
        self.assertTrue(target.ui_applicable)

    def test_offline_when_the_master_is_unloaded(self):
        """主控区块没加载是**已确认**的结论，因此 `validity` 是 `ok` 不是 `pending`。"""
        self.unload_chunk(self.CONTROLLER)
        status = self.server.application.device_status(self.target_ref())
        self.assertFalse(status.is_online)
        self.assertEqual(status.validity, "ok")
        self.assertIn(Reason.MASTER_UNLOADED, status.reason_codes())
        self.assertEqual(status.state_text(), "离线")

    def test_pending_is_marked_but_still_offline(self):
        """待确认与确认离线对界面是同一个结论，诊断里必须能分辨。"""
        self.master().begin_recovery()
        status = self.server.application.device_status(self.target_ref())
        self.assertEqual(status.validity, "pending")
        self.assertFalse(status.is_online)
        self.assertEqual(status.state_text(), "离线")

    def test_unconfirmed_bandwidth_is_pending(self):
        """带宽未知同样算待确认，而不是「看起来正常」。"""
        unknown = key(14, 64, 0)
        self.place(unknown, CONNECTOR)
        status = self.server.application.device_status(self.ref_of(unknown))
        self.assertFalse(status.is_online)
        self.assertEqual(status.validity, "pending")
        self.assertIsNone(status.bandwidth_ok)

    def test_readable_for_a_removed_device(self):
        ref = self.target_ref()
        self.remove(self.TARGET, self.target_device)
        status = self.server.application.device_status(ref)
        self.assertFalse(status.is_online)
        self.assertIn(Reason.DEVICE_GONE, status.reason_codes())

    def test_no_longer_online_when_the_energy_verdict_is_unconfirmed(self):
        """能源判定不成立时设备状态栏显示离线，而不是「看起来正常」。

        注意理由：不是「服务没写」，而是「判定尚未确认」（见
        `test_default_assembly_makes_the_network_unavailable`）。
        """
        plain = ServerBootstrap(world=FakeWorld())
        plain.start(bind_engine=False)
        for event in (
            NormalizedEvent.place(key(15, 64, 0), BB8),
            NormalizedEvent.place(key(16, 64, 0), BB8),
            NormalizedEvent.place(key(16, 65, 0), TINY),
            NormalizedEvent.place(key(15, 64, 1), DRIVER),
        ):
            plain.push(event)
        plain.tick()
        status = plain.application.device_status(plain.registry.get(key(15, 64, 1)).ref)
        self.assertFalse(status.is_online)
        self.assertIn(Reason.ENERGY_UNAVAILABLE, status.reason_codes())

    def test_status_without_any_runtime_still_answers(self):
        """`compose_device_status` 是模块级函数，正是为了这一条：

        分量不存在时连运行时都没有，但注册表、拓扑与带宽判定依然可读。
        """
        status = compose_device_status(
            DeviceRef(key(500, 64, 0), 1),
            self.server.definitions,
            self.server.registry,
            self.server.catalog,
            self.server.topology,
            self.server.admission,
        )
        self.assertFalse(status.is_online)
        self.assertIn(Reason.DEVICE_GONE, status.reason_codes())


if __name__ == "__main__":
    unittest.main()
