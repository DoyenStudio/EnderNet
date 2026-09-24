# coding=utf-8
"""任务 03-02、03-06 的离线验收：分量级运行时、迁移顺序与服务取用。

覆盖 03 验收清单里「运行时怎么随图变化」的部分：

- **每个独立网络拥有独立主控与服务实例**（验收 #4）。靠工厂为每个运行时
  各造一套来保证，因此这里验的是「结构上不可能共享」，不是自觉。
- **迁移顺序固定**：旧服务卸载 → 更新归属 → 新服务挂载 → 校验完成后开放。
  顺序颠倒会让服务在「图已经变了、归属还没更新」的半更新状态下收到回调，
  因此这里用一条**共享的顺序记录**把次序钉住，而不是分别断言两个结果。
- **合网出现控制器冲突时运行时保留**（服务状态不清空），只是没有主控。
- **拆网后各自重绑**。
- `runtime.service()` 的四种非法取用全部 `AccessDenied`——这是「设备不得
  绕过主控制器直接改库存」的结构性落点。

这里的服务是**记录型测试双件**：只记调用顺序、不假装有库存。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.protocol import CommandKind, CommandResult, CommandSource, DeviceCommand
from endernet.server.bootstrap import ServerBootstrap
from endernet.server.network.controller import AccessGrant, RunState
from endernet.server.network.runtime import (
    AccessDenied,
    NODE_RECORD_KIND,
    RUNTIME_RECORD_KIND,
    RuntimeChange,
)
from endernet.server.network.services import NetworkService, ServiceFactory, ServiceId
from endernet.server.network.topology import MigrationKind
from endernet.server.persistence.repository import MemoryRepository
from endernet.server.runtime.events import NormalizedEvent

BB8 = "endernet:network_baseband_8"
TINY = "endernet:tiny_network_endpoint"
DRIVER = "endernet:storage_driver"


def key(x, y=64, z=0, dimension=0):
    return NodeKey(dimension, x, y, z)


class FakeWorld(object):
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


class TraceService(NetworkService):
    """记录型双件。把每次挂载/卸载写进**共享**的顺序记录。

    共享是刻意的：迁移顺序是一个跨运行时的性质，「节点先从 A 卸下、
    再挂到 B 上」只有把两边的记录放进同一条时间线才验得出来。
    """

    #: 每次 `tick` 消耗的工作量。用 1 而不是 0，才能验证预算确实按服务切分。
    COST = 1

    def __init__(self, service_id, trace):
        # type: (str, list) -> None
        self.service_id = service_id
        self.trace = trace
        self.requires = ()
        self.mounted = {}
        self.removed = []
        self.ticks = 0
        self.handled = []
        #: 测试自己打的记号，用来验证「状态没在运行时之间串门」。
        self.marker = None

    def add_node(self, node, saved_data=None):
        # type: (object, dict) -> bool
        self.mounted[node.node_key] = saved_data
        self.trace.append(("add", self.service_id, node.node_key.as_key()))
        return True

    def remove_node(self, node):
        # type: (object) -> bool
        self.removed.append(node.node_key)
        self.trace.append(("remove", self.service_id, node.node_key.as_key()))
        return True

    def power_ok(self, node_key):
        # type: (object) -> object
        # 只有能源服务会被问；未确认不等于有电，因此这里明确给 True 而不是
        # 让默认的 None 把整个网络判成「能源待确认」。
        return True

    def tick(self, context, budget):
        # type: (dict, int) -> int
        self.ticks += 1
        return self.COST

    def handle(self, grant, command):
        # type: (object, object) -> object
        self.handled.append(command.request_id)
        return CommandResult.ok(command.request_id, {"by": self.service_id})

    def save_node_data(self, node):
        # type: (object) -> object
        if self.service_id != ServiceId.STORAGE:
            return None
        return {"service": self.service_id, "marker": self.marker}


class RuntimeFixture(unittest.TestCase):
    """真实装配 + 记录型服务。

    默认布局是一个可运行的小网络；合网/拆网的用例用 `LAYOUT_A` /
    `LAYOUT_B` 与 `BRIDGE` 单独摆。
    """

    A_BB = key(10, 64, 0)
    A_CTRL = key(10, 65, 0)
    A_DEV = key(10, 64, 1)
    B_BB = key(12, 64, 0)
    B_CTRL = key(12, 65, 0)
    B_DEV = key(12, 64, 1)
    #: 一个基带就能把相隔一格的 A 与 B 连起来（10 — 11 — 12）。
    BRIDGE = key(11, 64, 0)

    def setUp(self):
        self.world = FakeWorld()
        self.trace = []
        self.changes = []
        self.repo = MemoryRepository()
        self.server = ServerBootstrap(
            world=self.world,
            service_factories=ServiceFactory(self.builders()),
            repository=self.repo,
        )
        self.server.runtimes.add_listener(self._on_changes)
        self.server.start(bind_engine=False)

    def _on_changes(self, changes):
        self.changes.extend(changes)
        # 运行时通知与服务的挂载/卸载记进**同一条**时间线：只有这样才能
        # 断言「先挂载、后开放」这种跨两者的次序。
        for change in changes:
            self.trace.append(("notify", change.kind, change.component_id))

    def builders(self):
        # type: () -> dict
        def make(service_id):
            def build(runtime):
                return TraceService(service_id, self.trace)

            return build

        return dict((service_id, make(service_id)) for service_id in ServiceId.ALL)

    def place(self, position, device_id):
        self.drive(NormalizedEvent.place(position, device_id))

    def remove(self, position, device_id):
        self.drive(NormalizedEvent.remove(position, device_id))

    def drive(self, *events):
        for event in events:
            self.server.push(event)
        self.server.tick()

    # -- 查询 ----------------------------------------------------------

    def ref_of(self, position):
        record = self.server.registry.get(position)
        self.assertIsNotNone(record, "位置 %s 没有设备" % (position.as_key(),))
        return record.ref

    def runtimes(self):
        return self.server.runtimes.runtimes()

    def runtime_for(self, position):
        runtime = self.server.runtimes.runtime_for(self.ref_of(position))
        self.assertIsNotNone(runtime, "%s 没有归属的运行时" % (position.as_key(),))
        return runtime

    def service(self, runtime, service_id=ServiceId.STORAGE):
        # type: (object, str) -> NetworkService
        return runtime.services.get(service_id)

    def kinds(self):
        # type: () -> list
        return [change.kind for change in self.changes]

    def watch_migrations(self):
        # type: () -> list
        """记录 02 实际发布了哪些迁移通知。

        直接包住私有方法是刻意的：迁移通知没有对外的观察口，而「02 到底
        发了什么、03 又是怎么处理的」是两件必须分开看的事——把它们混成
        「结果对不对」会让顺序问题在将来某次重构里悄悄溜过去。
        """
        seen = []
        original = self.server.runtimes._apply_migration

        def spy(notice):
            seen.append(notice)
            return original(notice)

        self.server.runtimes._apply_migration = spy
        return seen

    def assertDenied(self, runtime, service_id=ServiceId.STORAGE, grant=None):
        with self.assertRaises(AccessDenied):
            runtime.service(service_id, grant)


class TestRuntimeBasics(RuntimeFixture):
    def setUp(self):
        RuntimeFixture.setUp(self)
        self.drive(
            NormalizedEvent.place(self.A_BB, BB8),
            NormalizedEvent.place(self.A_CTRL, TINY),
            NormalizedEvent.place(self.A_DEV, DRIVER),
        )

    def test_one_runtime_per_component(self):
        self.assertEqual(len(self.runtimes()), 1)
        runtime = self.runtimes()[0]
        # 成员按 `as_key()` 排序，遍历与发布顺序因此是确定的。
        self.assertEqual(runtime.member_keys(), (self.A_BB, self.A_DEV, self.A_CTRL))
        self.assertTrue(runtime.contains_node(self.A_BB))
        self.assertEqual(runtime.version, self.server.topology.version)

    def test_runtime_is_open_and_runnable(self):
        runtime = self.runtime_for(self.A_DEV)
        self.assertTrue(runtime.is_open(), runtime.close_reason())
        self.assertEqual(runtime.state(), RunState.RUNNABLE)
        self.assertIsNotNone(runtime.master)

    def test_runtime_lifecycle_is_announced(self):
        """创建 → 绑定主控 → 开放，三类变化都要发出来。

        04 靠它决定起停 ticking，11 靠它刷新状态：漏一类不是少一条日志，
        而是某个界面永远不更新。
        """
        self.assertIn(RuntimeChange.CREATED, self.kinds())
        self.assertIn(RuntimeChange.MASTER_BOUND, self.kinds())
        self.assertIn(RuntimeChange.OPENED, self.kinds())
        self.assertLess(self.kinds().index(RuntimeChange.CREATED), self.kinds().index(RuntimeChange.OPENED))

    def test_open_is_announced_after_the_members_are_mounted(self):
        """验收：校验完成后才开放业务。

        如果先开放再挂载，服务会在「还没登记这个节点」的状态下短暂接受
        业务——那种窗口在离线测试里几乎不会自己暴露，因此按顺序记录来钉。
        """
        runtime = self.runtime_for(self.A_DEV)
        opened = self.trace.index(("notify", RuntimeChange.OPENED, runtime.component_id))
        added = [index for index, item in enumerate(self.trace) if item[0] == "add"]
        self.assertTrue(added, "应当有挂载记录")
        self.assertLess(max(added), opened, "开放业务时还有节点没挂上")

    def test_runtime_for_looks_up_by_location_only(self):
        """代次过期由主控在准入链里判，这里若也判一次就会出现两套说法。"""
        stale = DeviceRef(self.A_DEV, 99)
        self.assertIs(self.server.runtimes.runtime_for(stale), self.runtime_for(self.A_DEV))

    def test_runtime_of_returns_none_for_an_unknown_position(self):
        self.assertIsNone(self.server.runtimes.runtime_of(key(200, 64, 0)))

    def test_services_are_mounted_with_saved_data(self):
        """任务 12 的入口先留好：局部恢复数据要能到达服务。"""
        runtime = self.runtime_for(self.A_DEV)
        runtime.import_node_data(self.A_DEV, {"marker": 7})
        runtime.sync_component(self.server.topology.snapshot())
        storage = self.service(runtime)
        self.assertEqual(storage.mounted[self.A_DEV], {"marker": 7})


class TestIndependentNetworks(RuntimeFixture):
    def setUp(self):
        RuntimeFixture.setUp(self)
        self.drive(
            NormalizedEvent.place(self.A_BB, BB8),
            NormalizedEvent.place(self.A_CTRL, TINY),
            NormalizedEvent.place(self.A_DEV, DRIVER),
            NormalizedEvent.place(self.B_BB, BB8),
            NormalizedEvent.place(self.B_CTRL, TINY),
            NormalizedEvent.place(self.B_DEV, DRIVER),
        )

    def test_two_networks_have_independent_runtimes(self):
        """验收 #4：每个独立网络拥有独立主控与独立服务状态。"""
        runtimes = self.runtimes()
        self.assertEqual(len(runtimes), 2)
        self.assertEqual(
            len(set(item.component_id for item in runtimes)), 2, "分量编号重复"
        )
        self.assertEqual(
            len(set(id(item.services) for item in runtimes)), 2, "两个网络共用了服务集合"
        )
        self.assertEqual(
            len(set(item.master.instance_id for item in runtimes)),
            2,
            "两个网络共用了主控实例",
        )

    def test_service_state_does_not_leak_between_networks(self):
        """在 A 上留下的痕迹不得出现在 B 上。

        两个网络的成员完全同构（都是基带 + 控制器 + 驱动器），因此这条
        测的确实是「实例不共用」，而不是「图不一样」。
        """
        first = self.runtime_for(self.A_DEV)
        second = self.runtime_for(self.B_DEV)
        a, b = self.service(first), self.service(second)
        a.marker = "A"
        a.handled.append("x")
        self.assertIsNone(b.marker)
        self.assertEqual(b.handled, [])
        self.assertIsNot(a.mounted, b.mounted)


class TestMergeAndSplit(RuntimeFixture):
    """合网冲突与拆网重绑。运行时按分量组织，控制器只是当前的主控身份。"""

    def setUp(self):
        RuntimeFixture.setUp(self)
        # 先摆两个各自可运行、彼此不相连的网络。
        self.drive(
            NormalizedEvent.place(self.A_BB, BB8),
            NormalizedEvent.place(self.A_CTRL, TINY),
            NormalizedEvent.place(self.A_DEV, DRIVER),
            NormalizedEvent.place(self.B_BB, BB8),
            NormalizedEvent.place(self.B_CTRL, TINY),
            NormalizedEvent.place(self.B_DEV, DRIVER),
        )
        self.assertEqual(len(self.runtimes()), 2)
        for runtime in self.runtimes():
            self.service(runtime).marker = runtime.component_id

    def merge(self):
        self.place(self.BRIDGE, BB8)

    def split(self):
        self.remove(self.BRIDGE, BB8)

    def surviving(self):
        """合网后活下来的那个运行时：被沿用的分量编号是 A 与 B 之一的那个。"""
        runtimes = self.runtimes()
        self.assertEqual(len(runtimes), 1)
        return runtimes[0]

    def test_merge_keeps_the_runtime_and_drops_the_master(self):
        """合并出控制器冲突时**运行时活下来**，只是没有主控。

        库存与订单归属属于运行时。若因为一次冲突就把它清掉，用户会发现
        「把两段线接上再拆开」等于把里面的东西弄丢了。
        """
        self.merge()
        runtime = self.surviving()
        self.assertIsNone(runtime.master, "冲突状态下不该有主控")
        self.assertEqual(runtime.state(), RunState.CONTROLLER_CONFLICT)
        self.assertFalse(runtime.is_open())
        self.assertIn(RuntimeChange.MASTER_LOST, self.kinds())
        self.assertIn(RuntimeChange.CLOSED, self.kinds())

    def test_merge_preserves_service_state_and_members(self):
        self.merge()
        runtime = self.surviving()
        self.assertIsNotNone(self.service(runtime).marker, "存活运行时的服务状态被清空了")
        # 两边的 3 + 3 个成员，加上把它俩连起来的那个基带。
        self.assertEqual(len(runtime.member_keys()), 7, "两个分量的成员都该并进来")

    def test_no_business_reaches_the_service_while_conflicting(self):
        """冲突期间命令被挡在准入里，服务一次都没被调用。

        验收：输入、输出、玩家操作与订单任务**确实通过主控**——
        证据就是没过准入时服务根本没被碰到。
        """
        self.merge()
        runtime = self.surviving()
        storage = self.service(runtime)
        before = len(storage.handled)
        result = self.server.submit_command(self._command())
        self.assertEqual(result.status, "blocked", result.to_dict())
        self.assertEqual(len(storage.handled), before)

    def _command(self):
        # type: () -> DeviceCommand
        ref = self.ref_of(self.A_DEV)
        return DeviceCommand(
            "r1",
            CommandKind.STORAGE,
            CommandSource("p1", ref, 0),
            device_ref=ref,
        )

    def test_retired_runtime_is_gone(self):
        """另一个编号在合网时被回收，它的运行时随之退役。"""
        self.merge()
        self.assertEqual(self.server.runtimes.stats["retired"], 1)
        self.assertIn(RuntimeChange.RETIRED, self.kinds())

    def test_split_rebinds_a_master_on_each_side(self):
        self.merge()
        self.split()
        runtimes = self.runtimes()
        self.assertEqual(len(runtimes), 2, "拆开后应各自成立")
        for runtime in runtimes:
            self.assertIsNotNone(runtime.master, "拆开后每半边都该有自己的主控")
            self.assertTrue(runtime.is_open(), runtime.close_reason())
            self.assertEqual(runtime.state(), RunState.RUNNABLE)
            self.assertEqual(len(runtime.member_keys()), 3)

    def test_split_nodes_end_up_on_the_right_side(self):
        self.merge()
        self.split()
        left = self.runtime_for(self.A_DEV)
        right = self.runtime_for(self.B_DEV)
        self.assertIsNot(left, right)
        self.assertIn(self.A_CTRL, left.member_keys())
        self.assertIn(self.B_CTRL, right.member_keys())
        self.assertNotIn(self.B_CTRL, left.member_keys())
        self.assertIs(left.master.ref, self.ref_of(self.A_CTRL))
        self.assertIs(right.master.ref, self.ref_of(self.B_CTRL))


class TestMigrationOrder(RuntimeFixture):
    def setUp(self):
        RuntimeFixture.setUp(self)
        self.drive(
            NormalizedEvent.place(self.A_BB, BB8),
            NormalizedEvent.place(self.A_CTRL, TINY),
            NormalizedEvent.place(self.A_DEV, DRIVER),
            NormalizedEvent.place(self.B_BB, BB8),
            NormalizedEvent.place(self.B_CTRL, TINY),
            NormalizedEvent.place(self.B_DEV, DRIVER),
        )

    def test_unload_precedes_mount_across_runtimes(self):
        """迁移顺序：旧服务卸载 → 更新归属 → 新服务挂载。

        共享一条记录才看得出这个次序。反过来做的话，服务会在自己还持有
        这个节点时看到它出现在别人那里。
        """
        self.trace[:] = []
        self.place(self.BRIDGE, BB8)
        moved = [
            index
            for index, item in enumerate(self.trace)
            if item[2] == self.B_BB.as_key()
        ]
        self.assertTrue(moved, "B 的成员应当有一次卸载与一次挂载")
        add = [index for index in moved if self.trace[index][0] == "add"]
        remove = [index for index in moved if self.trace[index][0] == "remove"]
        self.assertTrue(remove, "被并入的运行时应当先卸下它的成员")
        self.assertTrue(add, "并入的运行时应当重新挂载")
        self.assertLess(max(remove), min(add), "挂载不得早于卸载")

    def test_detached_migration_removes_the_node(self):
        """拆掉一台设备：02 发 `DETACHED`，03 据此卸载。"""
        seen = self.watch_migrations()
        runtime = self.runtime_for(self.A_DEV)
        self.remove(self.A_DEV, DRIVER)
        kinds = [notice.kind for notice in seen if notice.node_key == self.A_DEV]
        self.assertIn(MigrationKind.DETACHED, kinds)
        self.assertNotIn(self.A_DEV, runtime.member_keys())
        self.assertIn(self.A_DEV, self.service(runtime).removed)


class TestServiceAccess(RuntimeFixture):
    """`runtime.service()` 是「设备不得绕过主控」的结构性落点。"""

    def setUp(self):
        RuntimeFixture.setUp(self)
        self.drive(
            NormalizedEvent.place(self.A_BB, BB8),
            NormalizedEvent.place(self.A_CTRL, TINY),
            NormalizedEvent.place(self.A_DEV, DRIVER),
        )
        self.runtime = self.runtime_for(self.A_DEV)

    def grant(self, **overrides):
        # type: (dict) -> AccessGrant
        master = self.runtime.master
        return AccessGrant(
            overrides.pop("request_id", "r1"),
            overrides.pop("controller_instance_id", master.instance_id),
            overrides.pop("topology_version", self.server.topology.version),
            overrides.pop("issued_tick", master.tick_count),
            overrides.pop("service_id", ServiceId.STORAGE),
        )

    def test_a_valid_grant_yields_the_service(self):
        service = self.runtime.service(ServiceId.STORAGE, self.grant())
        self.assertIs(service, self.runtime.services.get(ServiceId.STORAGE))

    def test_missing_grant_is_denied(self):
        self.assertDenied(self.runtime, grant=None)

    def test_stale_grant_is_denied(self):
        """许可依据的图版本过期就不算数：图变了，准入的结论也可能变。"""
        self.assertDenied(
            self.runtime, grant=self.grant(topology_version=self.server.topology.version - 1)
        )

    def test_grant_from_another_master_is_denied(self):
        self.assertDenied(
            self.runtime, grant=self.grant(controller_instance_id="somewhere-else")
        )

    def test_grant_for_another_service_is_denied(self):
        self.assertDenied(
            self.runtime, grant=self.grant(service_id=ServiceId.FACTORY)
        )

    def test_a_grant_can_only_be_consumed_once(self):
        """一次许可对应一次动作。可重复使用的许可等于没有准入。"""
        grant = self.grant()
        self.runtime.service(ServiceId.STORAGE, grant)
        self.assertTrue(grant.consumed)
        self.assertDenied(self.runtime, grant=grant)

    def test_closed_runtime_refuses_services(self):
        self.runtime.close("测试")
        self.assertDenied(self.runtime, grant=self.grant())

    def test_runtime_without_a_master_refuses_services(self):
        grant = self.grant()
        self.remove(self.A_CTRL, TINY)
        self.assertIsNone(self.runtime.master, "拆掉控制器后不该还有主控")
        self.assertDenied(self.runtime, grant=grant)

    def test_unknown_service_slot_raises_key_error(self):
        """槽位没装配是装配错误（`ServiceAbsentError`），与「许可不合法」分开。"""
        with self.assertRaises(KeyError):
            self.runtime.service("quantum", self.grant(service_id=None))


class TestTickAndBudget(RuntimeFixture):
    def setUp(self):
        RuntimeFixture.setUp(self)
        self.drive(
            NormalizedEvent.place(self.A_BB, BB8),
            NormalizedEvent.place(self.A_CTRL, TINY),
            NormalizedEvent.place(self.A_DEV, DRIVER),
        )
        self.runtime = self.runtime_for(self.A_DEV)

    def test_services_tick_only_while_open(self):
        """网络不能工作时还让服务跑，等于让它们绕过运行条件自己动起来。"""
        self.assertEqual(self.runtime.tick(context={}, budget=64), 4)
        self.assertEqual(self.runtime.tick(context={}, budget=64), 4)
        self.runtime.close("测试")
        self.assertEqual(self.runtime.tick(context={}, budget=64), 0)

    def test_the_master_clock_runs_even_when_closed(self):
        """去重账本的保留期按主控时钟算，它不能跟着业务开关一起停。"""
        master = self.runtime.master
        before = master.tick_count
        self.runtime.close("测试")
        self.runtime.tick(context={}, budget=64)
        self.assertEqual(master.tick_count, before + 1)

    def test_budget_is_split_across_services(self):
        """预算按服务切分：花完就停，剩余的服务留到下一帧。

        夹具在装配期已经推进过若干帧，因此这里看的是**增量**。
        """
        energy = self.service(self.runtime, ServiceId.ENERGY)
        storage = self.service(self.runtime, ServiceId.STORAGE)
        before_energy, before_storage = energy.ticks, storage.ticks
        self.assertEqual(self.runtime.tick(context={}, budget=1), 1)
        self.assertEqual(energy.ticks, before_energy + 1, "按装配顺序从能源开始")
        self.assertEqual(storage.ticks, before_storage, "预算花完就该停下")


class TestSaveNodeData(RuntimeFixture):
    def setUp(self):
        RuntimeFixture.setUp(self)
        self.drive(
            NormalizedEvent.place(self.A_BB, BB8),
            NormalizedEvent.place(self.A_CTRL, TINY),
            NormalizedEvent.place(self.A_DEV, DRIVER),
        )
        self.runtime = self.runtime_for(self.A_DEV)

    def test_runtime_record_carries_identity_and_master(self):
        payload = self.runtime.save_node_data()
        self.assertEqual(payload["component_id"], self.runtime.component_id)
        self.assertIsNotNone(payload["master"])
        record = self.repo.load(RUNTIME_RECORD_KIND, self.runtime.runtime_record_id())
        self.assertIsNotNone(record)
        self.assertEqual(record.owner_id, self.runtime.master.instance_id)
        self.assertEqual(record.payload["open"], True)

    def test_node_records_are_written_for_reporting_services(self):
        """只有交出东西的服务才留节点记录，避免写一堆空壳。"""
        self.runtime.save_node_data()
        record = self.repo.load(NODE_RECORD_KIND, self.A_DEV.as_key())
        self.assertIsNotNone(record)
        self.assertIn(ServiceId.STORAGE, record.payload["data"])
        self.assertNotIn(ServiceId.SCHEDULER, record.payload["data"])

    def test_node_data_is_keyed_by_position(self):
        """位置是稳定的，分量编号不是——节点数据按位置存。"""
        self.runtime.save_node_data()
        self.assertIn(self.A_BB.as_key(), self.repo.list_ids(NODE_RECORD_KIND))

    def test_saving_without_a_repository_still_returns_the_payload(self):
        server = ServerBootstrap(
            world=FakeWorld(), service_factories=ServiceFactory(self.builders())
        )
        server.start(bind_engine=False)
        for event in (
            NormalizedEvent.place(self.A_BB, BB8),
            NormalizedEvent.place(self.A_CTRL, TINY),
            NormalizedEvent.place(self.A_DEV, DRIVER),
        ):
            server.push(event)
        server.tick()
        runtime = server.runtimes.runtime_for(server.registry.get(self.A_DEV).ref)
        self.assertIn("members", runtime.save_node_data())

    def test_component_id_is_not_yet_a_durable_identity(self):
        """分量编号在进程重启后会重新分配，这里如实标出而不是假装已解决。"""
        self.assertEqual(
            self.runtime.runtime_record_id(), "runtime@%s" % (self.runtime.component_id,)
        )


class TestRetire(RuntimeFixture):
    def setUp(self):
        RuntimeFixture.setUp(self)
        self.drive(
            NormalizedEvent.place(self.A_BB, BB8),
            NormalizedEvent.place(self.A_CTRL, TINY),
            NormalizedEvent.place(self.A_DEV, DRIVER),
        )

    def test_runtime_retires_when_the_component_disappears(self):
        runtime = self.runtime_for(self.A_DEV)
        storage = self.service(runtime)
        for position, device_id in (
            (self.A_DEV, DRIVER),
            (self.A_CTRL, TINY),
            (self.A_BB, BB8),
        ):
            self.remove(position, device_id)
        self.assertEqual(self.runtimes(), [])
        self.assertFalse(runtime.is_open())
        self.assertEqual(runtime.member_keys(), ())
        self.assertEqual(len(storage.removed), 3, "退役时必须逐个卸载成员")
        self.assertIn(RuntimeChange.RETIRED, self.kinds())

    def test_retired_runtime_stops_announcing(self):
        """退役后不再发通知，否则 04 会继续为一个不存在的网络起停。"""
        runtime = self.runtime_for(self.A_DEV)
        for position, device_id in (
            (self.A_DEV, DRIVER),
            (self.A_CTRL, TINY),
            (self.A_BB, BB8),
        ):
            self.remove(position, device_id)
        before = len(self.changes)
        runtime.close("再关一次")
        self.assertEqual(len(self.changes), before)


class TestCommandPathThroughRuntime(RuntimeFixture):
    """端到端一条：命令经主控准入后才拿到服务，服务才被调用。"""

    def setUp(self):
        RuntimeFixture.setUp(self)
        self.drive(
            NormalizedEvent.place(self.A_BB, BB8),
            NormalizedEvent.place(self.A_CTRL, TINY),
            NormalizedEvent.place(self.A_DEV, DRIVER),
        )

    def command(self, request_id="r1"):
        ref = self.ref_of(self.A_DEV)
        return DeviceCommand(
            request_id,
            CommandKind.STORAGE,
            CommandSource("p1", ref, 0),
            device_ref=ref,
        )

    def test_service_is_called_only_after_admission(self):
        runtime = self.runtime_for(self.A_DEV)
        storage = self.service(runtime)
        result = self.server.submit_command(self.command())
        self.assertEqual(result.status, "ok", result.to_dict())
        self.assertEqual(storage.handled, ["r1"])

    def test_a_manually_closed_runtime_surfaces_as_an_internal_error(self):
        """手工关掉一个健康的运行时是**缺陷**，不能伪装成一条业务失败。

        引擎自己不会制造这个状态：`close()` 只在分量重建中、无主控、
        冲突或退役时被调用，那些情况在准入链里都已经有对应的阻塞原因。
        因此这里要的是「缺陷露出来」——`RuntimeService` 兜底顶层把异常
        转成 `rejected internal_error`，而不是让它返回一个空成功。
        """
        runtime = self.runtime_for(self.A_DEV)
        storage = self.service(runtime)
        runtime.close("测试")
        result = self.server.submit_command(self.command("r2"))
        self.assertEqual(result.status, "rejected", result.to_dict())
        self.assertIn("internal_error", result.reason_codes())
        self.assertEqual(storage.handled, [], "没过准入却调用了服务")

    def test_a_direct_service_call_is_impossible_from_outside(self):
        """服务只能从运行时取，而运行时要求许可——这就是那条结构性保证。"""
        runtime = self.runtime_for(self.A_DEV)
        self.assertNotIn(ServiceId.STORAGE, dir(self.server))
        self.assertRaises(AccessDenied, runtime.service, ServiceId.STORAGE, None)


if __name__ == "__main__":
    unittest.main()
