# coding=utf-8
"""订单执行器与工厂服务（任务 09-02 / 09-04 / 09-06 / 09-07 / 09-08）的离线验收。

这一份走的是**完整链路**：主控准入 → 工厂服务 → 05 的预留 → 06 的
`port_stage` 备料 → 接口轮询投料 → 等待台账。因此它同时验证了
「工厂不绕过主控」与「计划不是预留」这两条结构性约束。

**回流（任务 10）用的是一个具名的假实现**（`FakeReturns`），
这是任务 09 文档明写允许的「初期可用假回流验证状态机」。
库默认口径下用的是 `AbsentReturns`，订单会**如实**停在等待产物——
那一条单独有用例，见 `TestReturnsNotWired`。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.identity import NodeKey
from endernet.common.protocol import (
    CommandKind,
    CommandSource,
    DeviceCommand,
    Reason,
    STATUS_OK,
    STATUS_REJECTED,
)
from endernet.common.resource_key import ResourceKey
from endernet.server.bootstrap import ServerBootstrap
from endernet.server.crafting.executor import AbsentReturns, ReturnsSink
from endernet.server.crafting.jobs import OrderStatus, StepStatus
from endernet.server.crafting import patterns as pattern_module
from endernet.server.network.energy import EnergyPolicy
from endernet.server.network.services import ServiceId
from endernet.server.adapters.containers import FakeContainer

IRON = ResourceKey.item("minecraft:iron_ingot")
GOLD = ResourceKey.item("minecraft:gold_ingot")
BLOCK = ResourceKey.item("minecraft:iron_block")
GEAR = ResourceKey.item("minecraft:iron_gear")

BASEBAND = "endernet:network_baseband_8"
CONTROLLER = "endernet:tiny_network_endpoint"
ENERGY = "endernet:creative_energy_source"
DRIVER = "endernet:storage_driver"
CONNECTOR = "endernet:parallel_processor_connector"
PROCESSOR = "endernet:parallel_processor"
MEMORY = "endernet:memory_slot"
TEMPLATE_DEVICE = "endernet:bus_template_io"

DISK = "endernet:database_compressed_tape"


def key(x, y=64, z=0):
    # type: (int, int, int) -> NodeKey
    return NodeKey.from_tuple(0, (x, y, z))


#: 网络布局。**每一处相邻都是有意的**：连接器必须贴着基带、处理器必须
#: 贴着连接器，否则处理器群不贡献槽位（02 的归属判定）。
#:
#:     (0..3, 64, 0) 基带串
#:     (1, 65, 0) 控制器   (0, 65, 0) 创造能量源   (2, 65, 0) 驱动器
#:     (3, 65, 0) 内存槽   (1, 64, 1) 连接器       (1, 64, 2) 处理器
#:     (4, 64, 0) 样板口   朝向 east → (5, 64, 0) 外部机器
BB = [key(index) for index in range(4)]
PORT_KEY = key(4)
MACHINE_KEY = key(5)
CONTROLLER_KEY = key(1, 65)
ENERGY_KEY = key(0, 65)
DRIVER_KEY = key(2, 65)
MEMORY_KEY = key(3, 65)
CONNECTOR_KEY = key(1, 64, 1)
PROCESSOR_KEY = key(1, 64, 2)


class FakeWorld(object):
    """与 `test_io` / `test_bootstrap` 同形的替身。**本文件自己写一份**：
    互相 import 会让成败取决于另一个模块的导入顺序。"""

    def __init__(self):
        # type: () -> None
        self.facing = {}

    def list_loaded_chunks(self):
        return []

    def block_entities_in_chunk(self, chunk_key):
        return []

    def register_block_remove_listener(self, block_ids):
        pass

    def facing_at(self, node_key):
        return self.facing.get(node_key.as_key())

    def note_chunk_loaded(self, chunk_key, block_entities=()):
        pass

    def note_chunk_unloaded(self, chunk_key):
        pass

    def diagnostics(self):
        return {"fake": True}


class FakeContainerProvider(object):
    def __init__(self, boxes=None):
        self.boxes = dict(boxes or {})

    def container_at(self, node_key):
        return self.boxes.get(node_key.as_key())

    def diagnostics(self):
        return {"cached": len(self.boxes)}


class FakeReturns(ReturnsSink):
    """**具名的假回流**：把产物直接记进等待台账，不碰网络。

    它的形状与任务 10 将来要写的 `returns.py` 一致（登记 / 注销 / 交回），
    因此状态机走的确实是那条路，而不是一条只有测试才知道的捷径。

    `auto` 为真时登记即满足——**这是为了让状态机可测**，不代表
    「产物一定会到」；`auto=False` 的实例用来验「回流没到时步骤仍在工作」。
    """

    adapter_id = "fake"

    def __init__(self, auto=True, skip=()):
        self.auto = auto
        #: 这些资源键**不回**。用来构造「产物到了、返还物没到」。
        self.skip = tuple(skip)
        self.registered = {}
        self.unregistered = []
        self.delivered = []
        #: 由夹具注入的存储服务。**它必须先真的把产物写进网络，再分配**
        #: ——这正是任务 10 的形状（10-02「从真实接受数量进行分配」）。
        #: 只通知不入库的假回流会让多级配方在第二步卡住：中间产物根本
        #: 不在网络里，而下游是从网络取料的。
        self.storage = None

    def register(self, execution_id, expectations, order_id=""):
        self.registered[execution_id] = list(expectations or ())
        return True

    def unregister(self, execution_id):
        if execution_id not in self.registered:
            return False
        self.registered.pop(execution_id, None)
        self.unregistered.append(execution_id)
        return True

    def describe(self):
        return {"adapter_id": self.adapter_id, "available": True}

    def flush(self, executor):
        # type: (object) -> int
        """把登记过的期望全部「回流」一遍。返回送回的条目数。

        **两段，顺序不能反**：先真的把产物写进网络存储（拿到实际接受量），
        再按那个量分配给等待的实例。只做第二段的话，订单的账是对的，
        而网络里什么都没有——下游步骤去取料时会发现中间产物不在，
        而它报出来的会是「缺料」，与真相（假回流没入库）完全不同。
        """
        moved = 0
        for execution_id in sorted(self.registered):
            for item in list(self.registered[execution_id]):
                if item.key in self.skip:
                    continue
                if item.remaining() <= 0:
                    continue
                op_id = "op:%s:%s" % (execution_id, item.canonical)
                want = item.remaining()
                if self.storage is not None:
                    receipt = self.storage.commit_insert(
                        op_id, item.key, want, owner_id="fake-returns"
                    )
                    want = receipt.moved
                if want <= 0:
                    continue
                accepted = executor.receive_output(
                    execution_id, item.key, want, op_id
                )
                if accepted:
                    self.delivered.append((execution_id, item.canonical, accepted))
                    moved += accepted
        return moved


class ExecutorFixture(unittest.TestCase):
    """一条**真的能跑起来的**离线工厂网络。"""

    #: 用哪个回流实现：`"fake"`（具名假回流）或 `"absent"`（库默认的替身）。
    returns_kind = "fake"
    #: 假回流**跳过**的资源键。用它构造「产物到了、返还物没到」。
    returns_skip = ()
    #: 接口策略。改成一份容量很小的原料缓冲可以构造出「只备了一部分」。
    port_policy = None
    patterns = None
    #: 额外摆进网络的 `(位置, 方块 id)`。多级配方要更多内存单位，
    #: 子类拿它补设备——**容量是从网络成员真算出来的**，不是夹具里写死的数。
    extra_devices = ()

    def setUp(self):
        self.world = FakeWorld()
        self.machine = FakeContainer(slots=27, max_stack=64)
        self.port_box = FakeContainer(slots=9, max_stack=1)
        self.containers = FakeContainerProvider()
        self.sink = self.build_sink()
        #: 只读查询的自增请求号。**必须每次都不一样**：`query` 走的是
        #: `CommandKind.STORAGE`（写路由），因此它进主控的去重账本——
        #: 复用同一个 `request_id` 会让第二次查询拿到**第一次的缓存结果**，
        #: 而那是「取消之后库存看起来没变」这类假失败最常见的来源。
        self._query_seq = 0
        self.server = ServerBootstrap(
            world=self.world,
            energy_policy=EnergyPolicy.dev_stand_in(),
            container_provider=self.containers,
            returns_sink=self.sink,
            port_policy=self.port_policy,
        )
        self.server.start(bind_engine=False)
        self.build_network()
        self.runtime = self.server.runtimes.runtimes()[0]
        self.service = self.runtime.services.get(ServiceId.FACTORY)
        # 假回流要能真的入库，才能驱动多级配方（见 `FakeReturns.flush`）。
        self.sink.storage = self.runtime.services.get(ServiceId.STORAGE)

    def build_sink(self):
        # type: () -> object
        """回流接收侧。**每个用例各造一个**：类级共享一个带状态的替身，
        会让「这一条验了什么」取决于同类的其他用例有没有先跑过。"""
        if self.returns_kind == "absent":
            return AbsentReturns()
        return FakeReturns(skip=self.returns_skip)

    # -- 世界 ----------------------------------------------------------

    def place(self, node_key, device_id):
        from endernet.server.runtime.events import NormalizedEvent

        self.server.push(NormalizedEvent.place(node_key, device_id))

    def remove(self, node_key, device_id):
        """走**正常拆除路径**而不是直接动注册表：拓扑要跟着重建，
        否则 `refresh_capacity()` 读到的还是旧快照。"""
        from endernet.server.runtime.events import NormalizedEvent

        self.server.push(NormalizedEvent.remove(node_key, device_id))
        self.server.tick()

    def build_network(self):
        for row in BB:
            self.place(row, BASEBAND)
        self.place(CONTROLLER_KEY, CONTROLLER)
        self.place(ENERGY_KEY, ENERGY)
        self.place(DRIVER_KEY, DRIVER)
        self.place(MEMORY_KEY, MEMORY)
        self.place(CONNECTOR_KEY, CONNECTOR)
        self.place(PROCESSOR_KEY, PROCESSOR)
        self.place(PORT_KEY, TEMPLATE_DEVICE)
        for node_key, device_id in self.extra_devices:
            self.place(node_key, device_id)
        self.world.facing[PORT_KEY.as_key()] = "east"
        # 样板口自己的容器装样板，朝向的那个装「外部机器收到的料」。
        self.containers.boxes[PORT_KEY.as_key()] = self.port_box
        self.containers.boxes[MACHINE_KEY.as_key()] = self.machine
        self.server.tick()
        self.mount_a_disk()
        self.accrue_quota()
        self.install_patterns()

    def ref_at(self, node_key):
        return self.server.registry.get(node_key).ref

    def install_patterns(self):
        """装几块真样板。默认是一层的「铁块 ← 2 铁锭」与两级的「齿轮 ← 铁块」。"""
        if self.patterns is None:
            self.patterns = {}
        for slot, definition in sorted(self.patterns.items()):
            ok, detail = self.server.patterns.install(
                self.ref_at(PORT_KEY), slot, definition
            )
            self.assertTrue(ok, u"样板装不进去：%s" % (detail,))

    # -- 命令 ----------------------------------------------------------

    def order_command(self, action, request_id="o1", ref=None, **payload):
        target = ref if ref is not None else self.ref_at(DRIVER_KEY)
        return DeviceCommand(
            request_id,
            CommandKind.ORDER,
            CommandSource("p1", target, 0),
            payload=dict(payload, action=action),
            topology_version=self.server.topology.version,
            device_ref=target,
        )

    def order(self, action, request_id="o1", **payload):
        return self.server.submit_command(
            self.order_command(action, request_id, **payload)
        )

    def submit_order(self, target=BLOCK, amount=1, request_id="o1", ref=None):
        return self.order(
            "submit",
            request_id=request_id,
            ref=ref,
            target=target.to_dict(),
            amount=amount,
        )

    def storage_command(self, request_id, action, ref=None, **payload):
        target = ref if ref is not None else self.ref_at(DRIVER_KEY)
        return DeviceCommand(
            request_id,
            CommandKind.STORAGE,
            CommandSource("p1", target, 0),
            payload=dict(payload, action=action),
            topology_version=self.server.topology.version,
            device_ref=target,
        )

    def storage(self, request_id, action, ref=None, **payload):
        return self.server.submit_command(
            self.storage_command(request_id, action, ref, **payload)
        )

    def mount_a_disk(self, request_id="mount"):
        return self.storage(
            request_id,
            "mount",
            ref=self.ref_at(DRIVER_KEY),
            slot=0,
            model_id=DISK,
        )

    def accrue_quota(self, ticks=10 ** 6):
        """把逻辑时钟推远，让介质额度桶攒满（**不硬编码帧频**）。"""
        self.server.clock.advance(ticks)
        self.server.tick()

    def seed(self, key_, count, request_id="seed"):
        return self.storage(
            request_id,
            "insert",
            ref=self.ref_at(DRIVER_KEY),
            key=key_.to_dict(),
            count=count,
            op_id=request_id,
        )

    def configure_port(self, filter_keys=(IRON, GOLD, BLOCK), request_id="cfg"):
        return self.storage(
            request_id,
            "port_configure",
            ref=self.ref_at(PORT_KEY),
            config={
                "enabled": True,
                "filter": [item.to_dict() for item in filter_keys],
            },
        )

    # -- 推进 ----------------------------------------------------------

    def drive(self, ticks=1, budget=None):
        """推进若干帧。返回最后一次的 `tick` 报告。

        `budget` 给定时走**运行时管理器**而不是启动层：启动层的预算在构造时
        就定死了（`runtime_budget`），要按帧给不同预算只能从这里下去，
        而这也正是 04 那条「总消耗不超预算」不变量所在的那一层。
        """
        report = None
        for _ in range(ticks):
            if budget is None:
                report = self.server.tick()
            else:
                context = self.server._tick_context()
                self.server.runtimes.tick(budget, context)
                report = self.server.tick()
        return report

    def stock(self, key_):
        self._query_seq += 1
        return self.storage(
            "q%d" % (self._query_seq,),
            "query",
            ref=self.ref_at(DRIVER_KEY),
            key=key_.to_dict(),
        )

    def stored(self, key_):
        return self.stock(key_).payload["entry"]["stored"]

    def raw_stock(self, key_):
        """**不经主控**读库存。网络关掉、没有主控时命令发不出去，
        而验收要看的是「关掉之后没有东西被动过」——那时只能这么读。"""
        storage = self.runtime.services.get(ServiceId.STORAGE)
        return storage.query(key_)

    def reservations(self, order_id):
        storage = self.runtime.services.get(ServiceId.STORAGE)
        return storage.reservations_of(order_id)

    def order_view(self, order_id):
        return self.service.query("detail", {"order_id": order_id})

    def only_order(self):
        rows = self.service.query("list", {})["orders"]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def settle(self, order_id=None, ticks=32):
        """把订单推到**离开「规划中」**为止，返回那一刻的视图；被拒绝则 `None`。

        **新规则（2026-09-26）下「提交完立刻看状态」这件事不再成立**：
        提交那一帧不吃规划预算，请求先进「规划中」，配方节点从**下一帧**起
        每订单每 tick 一个。因此本模块里每一条「提交之后应当是什么状态」的
        断言都必须先经过这里——这不是夹具在迁就实现，`PLAN_NODES_PER_TICK`
        与「提交不吃预算」正是规则本身。

        返回 `None` 只有一个含义：**它从台账里消失了**，也就是被拒绝
        （拒绝的请求不是订单，见 `_reject_request`）。

        多订单用例**必须传 `order_id`**：不传的话拿到的是台账里第一张已经
        不处于规划中的订单，而那多半是另一张。
        """
        view = None
        for _ in range(ticks):
            self.server.tick()
            rows = self.service.query("list", {})["orders"]
            if order_id is not None:
                rows = [row for row in rows if row["order_id"] == order_id]
            if not rows:
                return None
            view = rows[0]
            if view["status"] != OrderStatus.PLANNING:
                return view
        return view

    def assert_admitted(self, view):
        """订单**真的通过了准入**：有预留、有两份租约、可以开工了。

        刻意**不**断言 `QUEUED`：调度器会在同一帧里把同一个运行时访问两次
        （`_step_plan` 的 docstring 写着这件事），因此落地那一帧就已经进了
        `RUNNING` 并开始推进步骤——`QUEUED` 从外面根本观察不到。要证的是
        「过了准入」，那就断言那件事本身：`adopted` 为真且不再被挡着。
        """
        self.assertIsNotNone(view, u"订单既没有落地，也没有被拒")
        self.assertTrue(view["adopted"], view["order_id"])
        self.assertIn(
            view["status"], (OrderStatus.QUEUED, OrderStatus.RUNNING), view["status"]
        )
        self.assertNotEqual(view["reservations"], [])

    def last_rejection(self):
        """取**唯一一条**拒绝记录。多于一条说明这一步本来就是另一件事。"""
        rows = self.service.rejected.recent()
        self.assertEqual(len(rows), 1, [row.to_dict() for row in rows])
        return rows[0]


# -- 配方 ------------------------------------------------------------------


def stack(key_, count, **kwargs):
    return pattern_module.PatternStack(key_, count, **kwargs)


def factory(inputs, outputs, returns=()):
    return pattern_module.PatternDefinition(
        pattern_module.MODE_FACTORY, inputs, outputs, returns
    )


class OneLevel(ExecutorFixture):
    """一层配方：铁块 ← 2 铁锭。"""

    patterns = {0: factory([stack(IRON, 2)], [stack(BLOCK, 1)])}

    def setUp(self):
        ExecutorFixture.setUp(self)
        self.configure_port()


class TestSubmit(OneLevel):
    def test_a_submitted_order_plans_reserves_and_queues(self):
        """提交走的是：**规划中 → 复核 → 预留 → 租约 → 排队**。每一步都留下凭据。"""
        self.assertEqual(self.seed(IRON, 4).status, STATUS_OK)
        result = self.submit_order()
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        # 命令那一帧**只看得到「规划中」**：节点从下一帧起每订单每 tick 一个，
        # 这条边是新规则的形状，不是实现的临时状态。
        self.assertEqual(result.payload["status"], OrderStatus.PLANNING)
        view = self.settle()
        self.assert_admitted(view)
        self.assertEqual(view["progress"]["steps_total"], 1)
        self.assertEqual(view["progress"]["steps_completed"], 0)

        # 预留真的落在 05 那侧，而且**已经被这一单自己花掉了**：落地那一帧
        # 就备了料（释放预留 → 搬走 → 把没搬走的补回来），因此这里看到的是
        # 「2 个已经离开网络」而不是「2 个还锁着」。
        entry = self.stock(IRON).payload["entry"]
        self.assertEqual(entry["stored"], 2)
        self.assertEqual(entry["reserved"], 0)
        self.assertEqual(self.reservations(view["order_id"]), ())
        # 搬走的那一批记的就是这一单的执行实例（`订单/步骤下标/执行序号`）。
        batch = self.server.ports.get(PORT_KEY).batch
        self.assertTrue(
            batch.op_id.startswith("%s/" % (view["order_id"],)), batch.op_id
        )
        # 订单自己的账仍分着记「计划声明了多少」与「现在真的锁着多少」——
        # 备料只释放本次要取的量，声明本身不因此变小。
        self.assertEqual(view["reservations"][0]["canonical"], IRON.canonical())
        self.assertEqual(view["reservations"][0]["claimed"], 2)

        # 槽位与内存各拿到一份租约（09-03：一个订单不等于一个槽位）。
        self.assertIsNotNone(view["slot_lease"])
        self.assertIsNotNone(view["memory_lease"])
        self.assertEqual(self.service.slots.used(), 1)

    def test_the_order_belongs_to_the_network_not_to_the_entry_device(self):
        """订单归属该设备所在的网络，**不绑在入口设备上**。

        入口设备被拆、区块卸载都不该让订单消失——它记在网络上，
        而网络状态由主控回答。
        """
        self.seed(IRON, 4)
        submitted = self.submit_order(request_id="r1").payload
        other = self.submit_order(request_id="r2", ref=self.ref_at(MEMORY_KEY)).payload
        self.assertEqual(submitted["component_id"], other["component_id"])
        self.assertEqual(submitted["master_instance_id"], other["master_instance_id"])

    def test_the_master_instance_is_part_of_the_order_id(self):
        self.seed(IRON, 4)
        view = self.submit_order().payload
        self.assertIn(view["master_instance_id"], view["order_id"])

    def test_bad_parameters_are_rejected_not_blocked(self):
        """参数写法不对是 `rejected`（重发无用），不是 `blocked`。"""
        result = self.order("submit", target="not-a-key", amount=1)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertIn(Reason.BAD_PARAMETER, result.reason_codes())
        result = self.order("submit", target=BLOCK.to_dict(), amount=0)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertIn(Reason.BAD_PARAMETER, result.reason_codes())

    def test_an_unknown_action_is_refused(self):
        result = self.order("explode", order_id="x")
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertIn(Reason.UNSUPPORTED, result.reason_codes())

    def test_a_plan_that_cannot_be_made_reports_the_plan_codes(self):
        """没有样板时，08 的原因码**原样透出**，不压成一句「订单失败」。

        「去装一块样板」与「去补原料」是两句话。

        **判据在拒绝记录上，不在命令的返回上**：分帧规划让拒绝变成异步的，
        命令那一帧答得了的只有「规划中」（追加决定 2）。
        """
        self.seed(IRON, 4)
        result = self.submit_order(target=GOLD, request_id="no-recipe")
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertIsNone(self.settle())
        record = self.last_rejection()
        # 记录里是**玩家那条命令的请求 ID**，不是引擎内部的订单 ID。
        self.assertEqual(record.request_id, "no-recipe")
        self.assertIn(Reason.PLAN_NO_RECIPE, record.codes())

    def test_an_order_whose_target_is_already_in_stock_finishes_after_planning(self):
        """**「库里已经够了」的零步骤 `HOLD` 计划**（08 交付说明第五节）。

        它是订单状态机里唯一一条 `PLANNING -> COMPLETED` 的边，而这条边
        原本**不在迁移表里**：`move_to` 静默返回 `False`，订单从此停在
        「规划中」空转——`plan_job` 已经是 `None`，`_step_plan` 变成一个
        什么都不做的空操作，而它自己的账看起来全都对。
        **这条是真机上撞出来的**：离线只单测了 `Order.progress()` 的零分母
        口径，没有一条端到端用例真的提交过零步骤订单。
        """
        self.seed(BLOCK, 1)
        result = self.submit_order(request_id="already-have-it")
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        # 「库里已经够了」也要**先规划完**才知道——零步骤是计划算出来的
        # 结论，不是提交时的猜测。
        view = self.settle()
        self.assertIsNotNone(view)
        self.assertEqual(view["status"], OrderStatus.COMPLETED)
        self.assertEqual(view["progress"]["steps_total"], 0)
        self.assertEqual(view["progress"]["ratio"], 1.0)
        self.assertEqual(view["steps"], [])
        # 一份都不该预留：没有步骤要消耗它。
        self.assertEqual(view["reservations"], [])
        self.assertEqual(self.service.slots.used(), 0)
        self.assertEqual(self.service.memory.used(), 0)

    def test_no_order_is_left_behind_when_the_plan_is_refused(self):
        """做不出计划的请求**不留在订单台账里**：一张从来没有过预留、
        也永远不会被推进的订单记录，与一张真的排着队的订单长得一模一样，
        而 11 的列表会把它们并排显示。它留下的是**另一张表**上的一条记录。"""
        self.seed(IRON, 4)
        self.submit_order(target=GOLD, request_id="no-recipe")
        self.assertIsNone(self.settle())
        self.assertEqual(self.service.query("list", {})["orders"], [])
        # 台账里没有它，拒绝记录里有——「被拒绝」不等于「没发生过」。
        self.assertIn(Reason.PLAN_NO_RECIPE, self.last_rejection().codes())

    def test_missing_materials_do_not_create_half_reservations(self):
        """材料不够时**在准入之前就拒绝**，一份都不预留。

        判据落在拒绝记录上，因为拒绝是**异步**的（追加决定 2）。
        """
        self.seed(IRON, 1)
        result = self.submit_order()
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertIsNone(self.settle())
        self.assertIn(Reason.PLAN_MISSING_MATERIALS, self.last_rejection().codes())
        storage = self.runtime.services.get(ServiceId.STORAGE)
        self.assertEqual(storage.query(IRON)["reserved"], 0)

    def test_two_orders_never_get_the_same_stock(self):
        """验收：「两个订单不会同时许给同一份库存」。

        库里刚好够一份：第一单落地时锁住，第二单**拿不到**。
        它拦住第二单的地方是**准入那一步的预留**，而不是规划——两份计划是
        各自按**提交那一刻**的快照算的，那时谁也还没锁（分帧规划把这两个
        时刻拉开了），因此第二单算得出计划、却锁不到料。
        库里刚好够一份。分帧规划把「算计划」与「准入」拉开了：第二单的计划
        **算得出来**（它看的是自己提交那一刻的快照，那时谁也还没锁），拦住
        它的是**准入那一步的复核**——复核读的是当下的可用量，而那一份已经在
        第一单名下了。它因此**在准入之前被拒**，一个执行资源都没拿到。
        """
        self.seed(IRON, 2)
        first = self.submit_order(request_id="a").payload
        second = self.submit_order(request_id="b").payload
        self.assert_admitted(self.settle(first["order_id"]))
        self.assertIsNone(self.settle(second["order_id"]))
        record = self.last_rejection()
        self.assertEqual(record.request_id, "b")
        self.assertIn(Reason.PLAN_STOCK_CHANGED, record.codes())
        # 记录说得清是**哪种料**不够。
        detail = u"".join(row.detail for row in record.reasons)
        self.assertIn(u"minecraft:iron_ingot", detail)
        self.assertIn(u"现有 0", detail)
        # **已知缺口（记在盘点里，本次不改）**：这条复核腿报的是
        # 「声明 2，现有 0」，它分不开「网络里确实没有」与「有，但被别的
        # 订单锁着」——后者由 `_taken_stock_reasons` 补，而那条补注只挂在
        # **计划失败**那条路上（`plan.gaps` 为空就不补）。新规则把复核这条
        # 路变成了并发下单的常见路径，补注该跟着搬过来。
        # 而且它一份都没锁到：05 那边只有第一单名下的东西。
        storage = self.runtime.services.get(ServiceId.STORAGE)
        self.assertEqual(storage.reservations_of(second["order_id"]), ())

    def test_the_stock_is_held_until_the_order_finishes(self):
        """预留真的挡住了别人：`available` 里看不到被锁的那一份。

        **先把样板口关掉**：备料在落地那一帧就会把料搬出网络（释放预留 →
        搬走 → 把没搬走的补回来），而这条用例要看的正是**搬走之前**那一刻
        的账。关掉之后订单停在阻塞上，料与预留都留在原地。
        """
        self.seed(IRON, 4)
        self.storage(
            "cfg-off",
            "port_configure",
            ref=self.ref_at(PORT_KEY),
            config={"enabled": False, "filter": []},
        )
        self.submit_order()
        self.settle()
        entry = self.stock(IRON).payload["entry"]
        self.assertEqual(entry["stored"], 4)
        self.assertEqual(entry["reserved"], 2)
        self.assertEqual(entry["available"], 2)


class TestCapacity(OneLevel):
    def test_no_processor_means_no_slots(self):
        """验收：「无连接器处理器提供零槽位」。"""
        self.seed(IRON, 4)
        # 把处理器与连接器都拆掉，重算容量。
        self.remove(CONNECTOR_KEY, CONNECTOR)
        self.remove(PROCESSOR_KEY, PROCESSOR)
        self.service.refresh_capacity()
        self.assertEqual(self.service.slots.capacity, 0)
        self.submit_order()
        view = self.settle()
        self.assertIsNotNone(view)
        self.assertEqual(view["status"], OrderStatus.BLOCKED)
        codes = [item["code"] for item in view["blockers"]]
        self.assertIn(Reason.ORDER_NO_CAPACITY, codes)

    def test_an_unattached_processor_group_contributes_nothing(self):
        """处理器没经连接器接入基带 → 零槽位（02 的归属判定，09 只是读它）。"""
        storage = self.runtime.services.get(ServiceId.STORAGE)
        self.assertGreaterEqual(self.service.slots.capacity, 1)
        # 把连接器挪走（拆掉再放到一个不挨着基带的位置）。
        self.remove(CONNECTOR_KEY, CONNECTOR)
        self.remove(PROCESSOR_KEY, PROCESSOR)
        self.place(key(9, 40), CONNECTOR)
        self.place(key(9, 41), PROCESSOR)
        self.server.tick()
        self.service.refresh_capacity()
        self.assertEqual(self.service.slots.capacity, 0)
        # **不贡献的群的清单按分量过滤**（`slot_capacity` 只看本网络）：
        # 那一群连基带都没接上，它属于另一个分量，因此这里看不到它。
        # 「没接连接器」与「根本没有处理器」在容量上都是 0，分得开的是
        # `test_jobs.TestCapacitySources` 那一组判别用例。
        self.assertEqual(self.service.idle_groups, [])
        self.assertIsNotNone(storage)

    def test_memory_units_come_from_memory_devices(self):
        self.assertEqual(self.service.memory.capacity, 1)
        self.assertEqual(self.service.memory_slots, 1)

    def test_shrinking_capacity_does_not_take_leases_back(self):
        """09-07：「拔处理器/内存时停止超额新派发，现有执行保留状态」。"""
        self.seed(IRON, 8)
        first = self.submit_order(request_id="a").payload
        self.assert_admitted(self.settle(first["order_id"]))
        self.remove(PROCESSOR_KEY, PROCESSOR)
        self.service.refresh_capacity()
        self.assertEqual(self.service.slots.capacity, 0)
        # 已有租约仍在——料是真的投出去了，强行收回会让那个订单失去账。
        self.assertEqual(self.service.slots.used(), 1)
        self.assertEqual(self.service.slots.over_subscribed(), 1)
        # 新的领不到。
        second = self.submit_order(request_id="b").payload
        view = self.settle(second["order_id"])
        self.assertEqual(view["status"], OrderStatus.BLOCKED)

    def test_a_second_order_waits_for_the_first_slot(self):
        self.seed(IRON, 8)
        first = self.submit_order(request_id="a").payload
        self.assert_admitted(self.settle(first["order_id"]))
        second = self.submit_order(request_id="b").payload
        # 只有一个槽位，第二单等（容量在**预留之前**判，因此它连料都没锁）。
        view = self.settle(second["order_id"])
        self.assertEqual(view["status"], OrderStatus.BLOCKED)
        self.assertFalse(view["adopted"])
        self.assertEqual(view["reservations"], [])
        codes = [item["code"] for item in view["blockers"]]
        self.assertIn(Reason.ORDER_NO_CAPACITY, codes)


class TestRevalidation(OneLevel):
    def test_a_changed_inventory_after_planning_is_caught(self):
        """08 的交接：**计划不是预留**。

        提交时按当时的快照复核过了，但那份快照是**算计划那一刻**的。
        这里构造的是「有人在我们复核与预留之间把料拿走了」——用领域 API
        直接扣掉库存来模拟，预留因此拿不到，报 `order_stock_taken`。
        """
        self.seed(IRON, 4)
        storage = self.runtime.services.get(ServiceId.STORAGE)
        # 先让计划成立（这一步成功），再在预留之前把料扣掉。
        original = storage.reserve

        def steal(owner_id, key_, count, tick=0):
            storage.commit_extract("steal", key_, storage.query(key_)["stored"])
            return original(owner_id, key_, count, tick)

        storage.reserve = steal
        try:
            result = self.submit_order()
            self.assertEqual(result.status, STATUS_OK, result.to_dict())
            # **补丁必须一直挂到落地为止**：分帧规划把「算计划」与「预留」
            # 之间从同一帧拉开成了好几帧，而这条用例要证的正是那个窗口。
            view = self.settle()
        finally:
            storage.reserve = original
        self.assertIsNotNone(view)
        self.assertEqual(view["status"], OrderStatus.BLOCKED)
        codes = [item["code"] for item in view["blockers"]]
        self.assertIn(Reason.ORDER_STOCK_TAKEN, codes)

    def test_a_stale_pattern_blocks_the_order(self):
        """样板在提交前被改掉 → 复核的样板腿报 07 的原因码，不合并成一句。"""
        self.seed(IRON, 4)
        # 把槽 0 的样板换成产出别的资源的。
        self.server.patterns.uninstall(self.ref_at(PORT_KEY), 0)
        self.submit_order(target=BLOCK)
        self.assertIsNone(self.settle())
        self.assertIn(Reason.PLAN_NO_RECIPE, self.last_rejection().codes())


class TestStaging(OneLevel):
    """备料、批次与预留的三条边界。

    **比 `OneLevel` 多一组处理器与内存槽**：`test_a_second_order_does_not_
    clobber_...` 要让第二个订单真的走到备料那一步，而容量是从网络成员
    **真算出来的**（`refresh_capacity` 每帧重算），手工调 `slots.refresh`
    会在下一帧被当场推翻——那样这条用例看着是绿的，其实第二个订单连
    槽位都没拿到，什么也没证到。
    """

    extra_devices = (
        (key(0, 64, 1), CONNECTOR),
        (key(0, 64, 2), PROCESSOR),
        (key(2, 64, 1), MEMORY),
    )

    def test_staging_takes_material_out_of_the_network(self):
        """备料真的把料从网络搬进了样板口的中转位。

        **这一条钉住的是本模块最容易写错的那个顺序**：05 的可用量是
        `stored - reserved`（含自己的预留），因此「先预留再取」会取到 0。
        正确的顺序是先按本次要取的量释放、取完再把没搬走的补回来。
        """
        self.assertEqual(self.seed(IRON, 4).status, STATUS_OK)
        self.submit_order()
        self.settle()
        self.drive(2)
        # 料已经离开网络：库里只剩 2 个，预留也归零（那 2 个已在路上）。
        entry = self.stock(IRON).payload["entry"]
        self.assertEqual(entry["stored"], 2)
        state = self.server.ports.get(PORT_KEY)
        self.assertIsNotNone(state.batch)
        self.assertEqual(state.batch.staged, 2)

    def test_the_batch_stays_when_the_machine_takes_nothing(self):
        """外部机器不收时批次**留在中转位**，订单如实阻塞。

        06 明写「后续推进交由 09 决定」，09 的决定是不清批次、不重发，
        把 `batch.reason` 原样报出来。
        """
        self.seed(IRON, 4)
        # 让朝向的容器读不到：外部机器「没有接收」。
        self.containers.boxes.pop(MACHINE_KEY.as_key(), None)
        self.submit_order()
        self.drive(40)
        state = self.server.ports.get(PORT_KEY)
        self.assertEqual(state.batch.staged, 2)
        self.assertEqual(state.batch.delivered, 0)
        # 料备好了、投不出去：**订单不被打成 BLOCKED**，因为这件事下一秒
        # 就可能变（放个容器上去就好），而执行器每帧本来就会再试一次。
        # 让人看得见的是步骤上的那句话（`ORDER_BATCH_STALLED` 属于
        # `RETRYABLE_CODES`）。
        view = self.only_order()
        self.assertEqual(view["status"], OrderStatus.RUNNING)
        self.assertTrue(view["steps"][0]["detail"])

    def test_a_second_order_does_not_clobber_the_first_ones_batch(self):
        """批次槽只有一格：第一个订单的批次**还在进行中**时，第二个让位。

        冲掉的表现是前一个订单的 `delivered` 被清零、然后原料被投第二遍。
        「还在进行中」是这条用例的关键条件——批次**投完了**之后格子本来就
        该被下一个批次接管，那时覆盖不是抢占。
        """
        self.seed(IRON, 8)
        # 机器收不到：第一批就一直挂在「投不完」上。
        self.containers.boxes.pop(MACHINE_KEY.as_key(), None)
        self.submit_order(request_id="a")
        self.drive(3)
        state = self.server.ports.get(PORT_KEY)
        first_op = state.batch.op_id
        self.assertIsNotNone(first_op)
        self.assertFalse(state.batch.is_complete())
        # 第二组处理器与内存槽是**摆在网络里**的（见类 docstring），因此
        # 第二个订单真的领得到槽位、真的会去抢那一格批次。
        second = self.submit_order(request_id="b")
        self.assertEqual(second.status, STATUS_OK, second.to_dict())
        self.assert_admitted(self.settle(second.payload["order_id"]))
        self.drive(5)
        self.assertEqual(state.batch.op_id, first_op)


class StaleMirror(OneLevel):
    """原料缓冲只装得下一件：**备走一件之后，另一件还被锁着**。

    要验「订单不拿本地镜像当真相」，就得让镜像里有一个**非零**的数，
    而那个数正是会被 05 单方面收走的那个。默认夹具一落地就把整批搬走、
    预留当场归零，构造不出这个局面——因此这里比 `OneLevel` 小一号缓冲。
    """

    def setUp(self):
        from endernet.server.devices.io import PortPolicy

        self.port_policy = PortPolicy(template_buffer_capacity=1)
        OneLevel.setUp(self)

    def test_a_released_reservation_is_not_trusted_from_the_local_mirror(self):
        """**预留的真相在 05，不在订单的镜像里。**

        介质被拔出时 05 会 `revoke_reservations_of_medium` 把预留收走，
        并明确把「通知订单重验」留给 09。订单的本地记录这时还停在原来
        那个数字上——照着它算会得出「料还锁着、拿得到」的结论，
        然后发出一批注定搬不动的命令，最后表现为「备料一直备不动」。
        """
        self.seed(IRON, 4)
        order_id = self.submit_order().payload["order_id"]
        self.settle(order_id)
        row = self.service.orders.get(order_id)
        # 备走了一件（缓冲只装得下一件），没搬走的那件**被重新锁上**。
        self.assertEqual(row.reservations[IRON.canonical()]["reserved"], 1)
        # 模拟「介质被拔出」：05 把预留收走，订单并不知情。
        storage = self.runtime.services.get(ServiceId.STORAGE)
        self.assertEqual(storage.release_owned_by(order_id), 1)
        self.assertEqual(row.reservations[IRON.canonical()]["reserved"], 1)
        # 而且料真的没了（另一条没有预留的路径把它取走了）。
        gone = storage.query(IRON)["stored"]
        self.assertEqual(storage.commit_extract("gone", IRON, gone).moved, gone)
        self.drive(3)
        view = self.only_order()
        self.assertEqual(view["status"], OrderStatus.BLOCKED)
        codes = [item["code"] for item in view["blockers"]]
        self.assertIn(Reason.ORDER_STOCK_TAKEN, codes)
        # 镜像在这一轮里被对齐了——界面看到的数与事实一致，
        # 而不是一个「其实还锁着」的乐观读数。
        self.assertEqual(row.reservations[IRON.canonical()]["reserved"], 0)


class TestCompletion(OneLevel):
    def test_a_step_completes_when_the_products_come_back(self):
        self.seed(IRON, 4)
        self.submit_order()
        self.settle()
        self.drive(3)
        step = self.only_order()["steps"][0]
        self.assertEqual(step["status"], StepStatus.WAITING_OUTPUTS)
        self.assertEqual(step["execution_id"] is not None, True)
        # 假回流把产物送回来。
        self.assertEqual(self.sink.flush(self.service.executor), 1)
        self.drive(3)
        view = self.only_order()
        self.assertEqual(view["status"], OrderStatus.COMPLETED)
        self.assertEqual(view["progress"]["steps_completed"], 1)
        self.assertEqual(view["progress"]["ratio"], 1.0)

    def test_completion_releases_the_slot_and_memory(self):
        self.seed(IRON, 4)
        self.submit_order()
        self.settle()
        self.drive(3)
        self.sink.flush(self.service.executor)
        self.drive(3)
        self.assertEqual(self.service.slots.used(), 0)
        self.assertEqual(self.service.memory.used(), 0)

    def test_the_waiting_ledger_is_unregistered_on_completion(self):
        self.seed(IRON, 4)
        self.submit_order()
        self.settle()
        self.drive(3)
        self.assertTrue(self.sink.registered)
        self.sink.flush(self.service.executor)
        self.drive(3)
        self.assertEqual(self.sink.registered, {})
        self.assertEqual(self.service.waiting.diagnostics()["instances"], 0)



class ReturnsNotArrived(OneLevel):
    """产出铁块、**返还铁锭**：返还物没到齐时这一步不算完成。

    默认的 `returns_skip = (IRON,)` 让假回流只送产物、不送返还物；
    下面第二条用例把它清掉，作为对照。
    """

    returns_skip = (IRON,)
    patterns = {
        0: factory([stack(IRON, 2)], [stack(BLOCK, 1)], returns=[stack(IRON, 1)])
    }

    def test_the_return_reaches_the_plan_step(self):
        """先确认夹具造出来的确实是「有返还物的配方」，否则下面两条无意义。"""
        self.seed(IRON, 4)
        self.submit_order()
        view = self.settle()
        self.assertIsNotNone(view)
        returns = view["steps"][0]["returns"]
        self.assertEqual(len(returns), 1)
        self.assertEqual(returns[0]["expected"], 1)
        self.assertTrue(returns[0]["counts_for_completion"])

    def test_a_return_that_has_not_arrived_keeps_the_step_working(self):
        """验收原文：「所需鱼已回流但空桶未到时步骤仍工作中」。

        返还物**算完成条件**（`OrderPolicy.returns_block_completion` 的既定侧），
        因此只回产物时这一步仍然在等——它不是失败，也不是「做完了」。
        这正是「一个标志位值多少钱」的那种用例：把它翻成 `False` 会让这条
        验收在实现里直接反过来，而没有任何别的用例会发现。
        """
        self.seed(IRON, 4)
        self.submit_order()
        self.settle()
        self.drive(3)
        # 只把产物送回来；返还物（这里的铁锭）被 `returns_skip` 挡着不发。
        self.assertEqual(self.sink.flush(self.service.executor), 1)
        self.drive(3)
        view = self.only_order()
        step = view["steps"][0]
        self.assertEqual(step["status"], StepStatus.WAITING_OUTPUTS)
        self.assertNotEqual(view["status"], OrderStatus.COMPLETED)
        # 产物到了、返还物还欠着——两个数分开记，界面才说得清在等什么。
        pending = self.service.waiting.pending(step["execution_id"])
        self.assertEqual(list(pending), [IRON.canonical()])

    def test_the_step_completes_once_the_return_arrives_too(self):
        """补上返还物之后才结单——上一条的反面，两条一起才有意义。"""
        self.seed(IRON, 4)
        self.submit_order()
        self.settle()
        self.drive(3)
        self.sink.skip = ()
        self.sink.flush(self.service.executor)
        self.drive(3)
        self.assertEqual(self.only_order()["status"], OrderStatus.COMPLETED)


class TwoLevels(ExecutorFixture):
    """两级配方：齿轮 ← 铁块，铁块 ← 2 铁锭。

    比单级多摆一个内存槽：两个步骤按 R04 的替身口径要 2 个内存单位，
    而夹具原本只有 1 个——那是**容量真的不够**，不是缺陷。
    """

    patterns = {
        0: factory([stack(IRON, 2)], [stack(BLOCK, 1)]),
        1: factory([stack(BLOCK, 1)], [stack(GEAR, 1)]),
    }
    extra_devices = ((key(2, 64, 1), MEMORY),)

    def setUp(self):
        ExecutorFixture.setUp(self)
        self.configure_port()


class TestMultiLevel(TwoLevels):
    def test_the_plan_expands_the_whole_tree(self):
        self.seed(IRON, 4)
        self.submit_order(target=GEAR, amount=1)
        view = self.settle()
        self.assertIsNotNone(view)
        self.assertEqual(view["progress"]["steps_total"], 2)
        self.assertEqual(view["progress"]["steps_completed"], 0)

    def test_steps_run_in_topological_order(self):
        """依赖先做、消费者后做。`execution_order()` 是合法拓扑序，
        **不是** `depth`（08 第七节第 13 条）。"""
        self.seed(IRON, 4)
        self.submit_order(target=GEAR, amount=1)
        self.settle()
        view = self.only_order()
        indices = [row["index"] for row in view["steps"]]
        gears = [row for row in view["steps"] if row["key"] == "minecraft:iron_gear"]
        blocks = [row for row in view["steps"] if row["key"] == "minecraft:iron_block"]
        self.assertEqual(len(gears), 1)
        self.assertEqual(len(blocks), 1)
        self.assertLess(indices.index(blocks[0]["index"]), indices.index(gears[0]["index"]))

    def test_the_second_step_waits_for_the_first(self):
        """依赖没满足时**不提前派发**（验收第一条）。"""
        self.seed(IRON, 4)
        self.submit_order(target=GEAR, amount=1)
        self.settle()
        self.drive(3)
        view = self.only_order()
        by_key = dict((row["key"], row) for row in view["steps"])
        self.assertEqual(by_key["minecraft:iron_block"]["status"], StepStatus.WAITING_OUTPUTS)
        self.assertEqual(by_key["minecraft:iron_gear"]["status"], StepStatus.WAITING_INPUTS)

    def test_the_whole_tree_completes_with_a_fake_return_path(self):
        self.seed(IRON, 4)
        self.submit_order(target=GEAR, amount=1)
        for _ in range(8):
            self.drive(3)
            self.sink.flush(self.service.executor)
        view = self.only_order()
        self.assertEqual(view["status"], OrderStatus.COMPLETED)
        self.assertEqual(view["progress"]["steps_completed"], 2)
        self.assertEqual(view["progress"]["ratio"], 1.0)


class TestReturnsNotWired(OneLevel):
    """**库默认口径**：回流入口（任务 10）还没接上。"""

    returns_kind = "absent"

    def test_the_order_stops_and_says_why(self):
        """订单**如实**停在等待产物，报 `order_returns_absent`。

        这不是失败、也不是缺料——是那一半还没实现。少了这个码，
        「订单为什么卡住」看起来会像机器坏了，然后有人去修一台没坏的机器。
        """
        self.seed(IRON, 4)
        self.submit_order()
        self.drive(6)
        view = self.only_order()
        self.assertEqual(view["status"], OrderStatus.BLOCKED)
        codes = [item["code"] for item in view["blockers"]]
        self.assertIn(Reason.ORDER_RETURNS_ABSENT, codes)
        self.assertEqual(view["steps"][0]["status"], StepStatus.WAITING_OUTPUTS)

    def test_a_step_whose_products_all_arrived_still_finishes(self):
        """**产物真的到齐了就得收尾，哪怕回流入口还是替身。**

        这条是**真机上撞出来的**：订单因为 `order_returns_absent` 转成
        BLOCKED 之后，就再也回不到 `_track_outputs` 去看产物到了没有——
        而产物是真的会到的（10 接上之后，或者有人把入库的资源分配过来）。
        真机上的表现是：步骤的 `received` 已经是 1/1，订单却停在阻塞上、
        进度停在 0。

        离线之所以照不到，是因为假回流报 `available: True`，
        订单一直留在 RUNNING 每帧轮询。因此这里**必须**用替身 +
        手工分配来构造。
        """
        self.seed(IRON, 4)
        self.submit_order()
        self.drive(4)
        order_id = self.only_order()["order_id"]
        step = self.order_view(order_id)["steps"][0]
        self.assertEqual(step["status"], StepStatus.WAITING_OUTPUTS)
        self.assertEqual(self.only_order()["status"], OrderStatus.BLOCKED)
        # 手工把产物分配过来（10 接上之后由它做，这里直接调它唯一的那条入口）。
        accepted = self.service.executor.receive_output(
            step["execution_id"], BLOCK, 1, "op:manual"
        )
        self.assertEqual(accepted, 1)
        self.drive(3)
        view = self.only_order()
        self.assertEqual(view["status"], OrderStatus.COMPLETED)
        self.assertEqual(view["progress"]["ratio"], 1.0)
        self.assertEqual(self.service.slots.used(), 0)

    def test_a_capacity_blocked_order_still_resumes_when_a_slot_frees_up(self):
        """**回流入口没接线不该盖掉别的阻塞原因。**

        这条也是真机上撞出来的：被容量挡下的订单在槽位释放之后本该自己恢复，
        却一直卡在 `order_returns_absent` 上——因为那条提示原先按**订单级**
        报，而它答的其实是「为什么这一步等不到产物」。一个还在
        `waiting_inputs`、甚至还没落地的订单根本谈不上等产物，
        却因为这条连 `_resume_adoption` 都走不到。
        """
        self.seed(IRON, 8)
        first = self.submit_order(request_id="a").payload
        self.assert_admitted(self.settle(first["order_id"]))
        second = self.submit_order(request_id="b").payload
        view = self.settle(second["order_id"])
        self.assertEqual(view["status"], OrderStatus.BLOCKED)
        self.assertFalse(view["adopted"])
        self.assertIn(
            Reason.ORDER_NO_CAPACITY, [b["code"] for b in view["blockers"]]
        )
        # 让位：取消第一张，槽位与内存都空出来。
        self.order("cancel", request_id="c1", order_id=first["order_id"])
        self.assertEqual(self.service.slots.used(), 0)
        self.drive(4)
        view = self.order_view(second["order_id"])
        # **自己恢复了**：落地走完、开始推进，而且报的是真实原因而不是
        # 「回流入口没接线」。
        self.assertTrue(view["adopted"])
        self.assertEqual(view["status"], OrderStatus.BLOCKED)
        self.assertIn(
            Reason.ORDER_RETURNS_ABSENT, [b["code"] for b in view["blockers"]]
        )
        self.assertEqual(view["steps"][0]["status"], StepStatus.WAITING_OUTPUTS)

    def test_it_never_reaches_completed(self):
        """跑很久也不会「成功」——默认装配里不存在任何会假装收货的实现。"""
        self.seed(IRON, 4)
        self.submit_order()
        self.drive(60)
        view = self.only_order()
        self.assertNotEqual(view["status"], OrderStatus.COMPLETED)
        self.assertGreater(
            self.service.waiting.diagnostics()["pending"], 0
        )


class TestCancelAndPause(OneLevel):
    def test_cancel_releases_everything(self):
        """09-08：取消释放未消费的预留与租约。

        **先把样板口关掉**：备料会把预留花掉（释放 → 搬走），而这条用例要
        验的正是「手上还有一份没花掉的预留时，取消要把它放干净」。
        """
        self.seed(IRON, 4)
        self.storage(
            "cfg-off",
            "port_configure",
            ref=self.ref_at(PORT_KEY),
            config={"enabled": False, "filter": []},
        )
        order_id = self.submit_order().payload["order_id"]
        self.settle(order_id)
        before = self.stock(IRON).payload["entry"]
        self.assertEqual(before["reserved"], 2)
        result = self.order("cancel", request_id="c1", order_id=order_id)
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertEqual(result.payload["status"], OrderStatus.CANCELLED)
        after = self.stock(IRON).payload["entry"]
        self.assertEqual(after["reserved"], 0)
        self.assertEqual(self.service.slots.used(), 0)
        self.assertEqual(self.reservations(order_id), ())

    def test_cancel_records_what_is_already_out_there(self):
        """在途资源**如实记下来**，不悄悄丢弃也不复制（R05 待定 → 保守侧）。"""
        self.seed(IRON, 4)
        order_id = self.submit_order().payload["order_id"]
        self.settle(order_id)
        self.drive(2)
        result = self.order("cancel", request_id="c1", order_id=order_id)
        residue = result.payload["residue"]
        self.assertTrue(residue, result.payload)
        row = residue[0]
        self.assertEqual(row["key"], IRON.canonical())
        self.assertGreater(row["staged"], 0)
        self.assertIn(u"不能取回", row["note"])

    def test_cancelling_twice_is_refused_with_the_right_code(self):
        """已经取消的订单不是「没有这个订单」——两句不同的话。"""
        self.seed(IRON, 4)
        order_id = self.submit_order().payload["order_id"]
        self.order("cancel", request_id="c1", order_id=order_id)
        again = self.order("cancel", request_id="c2", order_id=order_id)
        self.assertEqual(again.status, STATUS_REJECTED)
        self.assertIn(Reason.ORDER_TERMINAL, again.reason_codes())

    def test_an_unknown_order_is_a_different_code(self):
        result = self.order("cancel", request_id="c1", order_id="nope")
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertIn(Reason.ORDER_UNKNOWN, result.reason_codes())

    def test_cancel_unregisters_the_waiting_ledger(self):
        """10-07：由 09 注销等待记录，避免回调命中已取消的实例。"""
        self.seed(IRON, 4)
        order_id = self.submit_order().payload["order_id"]
        self.settle(order_id)
        self.drive(3)
        self.assertIn("%s/0" % (order_id,), self.sink.registered)
        self.order("cancel", request_id="c1", order_id=order_id)
        self.assertNotIn("%s/0" % (order_id,), self.sink.registered)
        self.assertEqual(self.service.waiting.diagnostics()["instances"], 0)

    def test_pause_and_resume(self):
        self.seed(IRON, 4)
        order_id = self.submit_order().payload["order_id"]
        self.settle(order_id)
        self.drive(1)
        paused = self.order("pause", request_id="p1", order_id=order_id)
        self.assertEqual(paused.payload["status"], OrderStatus.BLOCKED)
        codes = [item["code"] for item in paused.payload["blockers"]]
        self.assertIn(Reason.ORDER_PAUSED, codes)
        # 暂停期间不推进：库存与批次都不动。
        batch = self.server.ports.get(PORT_KEY).batch
        self.drive(5)
        self.assertEqual(self.server.ports.get(PORT_KEY).batch, batch)
        resumed = self.order("resume", request_id="r1", order_id=order_id)
        self.assertEqual(resumed.payload["status"], OrderStatus.QUEUED)
        self.assertFalse(resumed.payload["paused"])


class TestFreezeAndRecover(OneLevel):
    def test_the_network_closing_freezes_the_order(self):
        """断电/主控消失时订单**冻结**。

        这是**结构性**的：`NetworkRuntime.tick` 在网络不开放时直接返回 0，
        服务的 `tick` 根本不会被调用。因此这里断言的是「没有任何东西被推进」，
        而不是「有一个标志位被置上了」。
        """
        self.seed(IRON, 4)
        self.submit_order()
        self.settle()
        self.drive(2)
        state = self.server.ports.get(PORT_KEY)
        staged = state.batch.staged
        self.runtime.close("测试：断电")
        self.drive(20)
        # 步骤没往前走，库存也没变——**用不过主控的读法**，因为网络关掉之后
        # 命令根本发不出去（那正是「冻结」的结构性证据）。
        self.assertEqual(self.raw_stock(IRON)["stored"], 4 - staged)
        view = self.service.query("detail", {
            "order_id": self.service.query("list", {})["orders"][0]["order_id"]
        })
        self.assertNotEqual(view["status"], OrderStatus.COMPLETED)

    def test_reopening_rechecks_before_continuing(self):
        """重开之后**必须重新复核**，否则会拿着旧拓扑继续派发。"""
        self.seed(IRON, 4)
        order_id = self.submit_order().payload["order_id"]
        self.drive(1)
        row = self.service.orders.get(order_id)
        row.stats["revalidations"] = 0
        self.runtime.close("测试")
        self.drive(3)
        self.assertEqual(row.stats["revalidations"], 0, "关着的时候不该推进")
        # 网络关闭期间拓扑变了，重开之后必须被发现并重验。
        self.place(key(7), BASEBAND)
        self.server.tick()
        self.runtime.open_if_ready()
        self.drive(2)
        self.assertGreater(row.stats["revalidations"], 0)

    def test_an_order_does_not_follow_a_new_master_instance(self):
        """09 验收：拓扑拆分后订单**只属于原主控**，不跟着新主控继续动库存。

        这里直接把订单记着的主控实例改成一个别的（模拟控制器被换掉：
        新实例、新代次），订单必须挡下来而不是接着动库存。
        用白盒改而不是真的换设备，是因为同一位置重建得到的是**同一个实例号**
        （`instance_id` = 位置 + 代次，而重建的代次不一定前进），
        那样构造出来的场景其实什么都没变。
        """
        self.seed(IRON, 4)
        order_id = self.submit_order().payload["order_id"]
        self.drive(1)
        row = self.service.orders.get(order_id)
        row.master_instance_id = "somewhere-else#7"
        self.drive(3)
        view = self.only_order()
        self.assertEqual(view["status"], OrderStatus.BLOCKED)
        codes = [item["code"] for item in view["blockers"]]
        self.assertIn(Reason.MASTER_ABSENT, codes)

    def test_the_order_stays_with_its_own_component(self):
        """订单记在**它自己那张网络的台账**上：拓扑拆开之后，
        另一个分量里一张订单都没有（09 验收：「没有控制器的分量不能继续执行该订单」）。
        """
        self.seed(IRON, 4)
        self.submit_order()
        self.drive(1)
        self.assertEqual(len(self.service.query("list", {})["orders"]), 1)
        original = self.service.orders.component_id
        # 拆掉控制器旁边那条基带：分量一分为二。
        self.remove(BB[0], BASEBAND)
        self.server.tick()
        self.assertEqual(self.service.orders.component_id, original)
        self.assertEqual(self.service.query("list", {})["orders"][0]["component_id"], original)


class TestAssemblyChamber(OneLevel):
    def test_an_assembly_provider_is_refused_with_the_pending_task(self):
        """装配仓的执行半边是 07-06，**明确拒绝而不是假装能投**。

        一张「投了半天没人接」的订单比一条清清楚楚的 `service_absent`
        难查一百倍。
        """
        blocker = None
        step = type("S", (), {})()
        step.node_key = key(0, 0, 0).as_key()
        step.generation = 1
        step.slot = 0
        step.digest = "x"
        step.index = 0
        step.device_id = "endernet:assemble_chamber"
        step.op_id = None
        step.inputs = []
        step.execution_id = None
        step.status = StepStatus.WAITING_INPUTS
        # 借一台真的装配仓节点来问那句判定。
        self.place(key(9, 30), "endernet:assemble_chamber")
        self.server.tick()
        step.node_key = key(9, 30).as_key()
        port, blocker = self.service.executor._port_for(step)
        self.assertIsNone(port)
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker.code, Reason.SERVICE_ABSENT)
        self.assertIn("07-06", blocker.detail)


class TestReadOnlyQuery(OneLevel):
    def test_the_order_query_works_without_a_master(self):
        """全局约束 9：设备离线、网络不能工作时仍要能查看诊断。"""
        self.seed(IRON, 4)
        self.submit_order()
        self.runtime.close("测试")
        result = self.server.application.debug_query("order")
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        networks = result.payload["networks"]
        self.assertEqual(len(networks), 1)
        view = list(networks.values())[0]
        self.assertEqual(len(view["orders"]), 1)
        self.assertIn("progress", view["orders"][0])

    def test_the_query_accepts_a_component_id_and_an_order_id(self):
        self.seed(IRON, 4)
        order_id = self.submit_order().payload["order_id"]
        self.settle(order_id)
        result = self.server.application.debug_query(
            "order", component_id=self.runtime.component_id, order_id=order_id
        )
        self.assertEqual(result.payload["order_id"], order_id)
        self.assertEqual(result.payload["progress"]["steps_total"], 1)

    def test_an_unknown_id_lists_what_is_known(self):
        self.seed(IRON, 4)
        order_id = self.submit_order().payload["order_id"]
        result = self.server.application.debug_query(
            "order", component_id=self.runtime.component_id, order_id="nope"
        )
        self.assertEqual(result.payload["order_id"], "nope")
        self.assertEqual(result.payload["known"], [order_id])

    def test_the_query_never_writes_anything(self):
        """只读：查完之后库存、预留、台账一个数都不变。"""
        self.seed(IRON, 4)
        self.submit_order()
        before = self.stock(IRON).payload["entry"]
        progress_before = self.only_order()["progress"]
        for _ in range(3):
            self.server.application.debug_query("order")
            self.server.application.debug_query(
                "order", component_id=self.runtime.component_id
            )
        self.assertEqual(self.stock(IRON).payload["entry"], before)
        self.assertEqual(self.only_order()["progress"], progress_before)

    def test_the_order_section_is_in_the_bootstrap_diagnostics(self):
        self.seed(IRON, 4)
        self.submit_order()
        report = self.server.diagnostics()["order"]
        self.assertIn("policy", report)
        self.assertIn("unresolved", report)
        # 这个夹具注入的是**具名假回流**，诊断如实报它接上了；
        # 「默认口径是替身」在 `TestOrderPolicyWiring` 里单独验。
        self.assertEqual(report["returns_sink"]["adapter_id"], "fake")
        self.assertTrue(report["returns_sink"]["available"])
        self.assertEqual(report["runtimes"]["networks"], 1)
        self.assertEqual(report["runtimes"]["live"], 1)
        # 这个夹具注入的是**具名假回流**，因此这一项是 0。
        # 「默认口径是替身、订单会停在等待产物」在 `TestReturnsNotWired` 里验。
        self.assertEqual(report["runtimes"]["returns_absent"], 0)


class TestOrderPolicyWiring(ExecutorFixture):
    patterns = {0: factory([stack(IRON, 2)], [stack(BLOCK, 1)])}

    def test_the_service_uses_the_injected_policy(self):
        """诊断报出来的策略**就是**实际生效的那份（不是各存一份）。"""
        policy = self.server.order_policy
        self.assertIs(self.service.policy, policy)
        self.assertIs(self.service.slots.policy, policy)
        self.assertIs(self.service.executor.policy, policy)

    def test_the_policy_is_part_of_the_assembly_report(self):
        fields = set(
            item["field"] for item in self.server.policy_bundle().unresolved_report()
        )
        self.assertIn("order.slots_per_order", fields)
        self.assertIn("order.memory_units_per_slot", fields)

    def test_absent_returns_is_the_library_default(self):
        """默认装配**不接受**一个什么都不做的假实现。"""
        server = ServerBootstrap(world=FakeWorld())
        self.assertIsInstance(server.returns_sink, AbsentReturns)
        server.stop()


class PartialStaging(OneLevel):
    """原料缓冲只装得下一件：备料**只备了一部分**。

    这一条钉住的是本模块里唯一「说不清归属」的窗口——05 没有 consume 入口，
    所以备料前按本次要取的量释放预留、备完再把没搬走的补回来。补不回来就
    如实报错，不当作成功。
    """

    def setUp(self):
        from endernet.server.devices.io import PortPolicy

        self.port_policy = PortPolicy(template_buffer_capacity=1)
        OneLevel.setUp(self)

    def test_the_unstaged_part_is_locked_again(self):
        self.seed(IRON, 4)
        order_id = self.submit_order().payload["order_id"]
        # 先让规划走完（提交那一帧不算节点），再**只推一帧**：那一帧就完成了
        # 「释放 → 备料 → 补回」这一整轮。多推几帧会把没搬走的那一件也备走，
        # 那时预留归零是正确的。
        self.settle(order_id)
        self.drive(1)
        row = self.service.orders.get(order_id)
        state = self.server.ports.get(PORT_KEY)
        # 只备走了一件，另一件没搬走 —— **必须重新锁上**，
        # 否则它在一段没人看着的时间窗口里不受本订单保护。
        self.assertEqual(row.reservations[IRON.canonical()]["reserved"], 1)
        self.assertEqual(
            self.runtime.services.get(ServiceId.STORAGE).query(IRON)["reserved"], 1
        )
        self.assertEqual(state.batch.staged, 1)

    def test_the_order_finishes_without_sending_the_whole_batch_twice(self):
        """验收：「部分投料……不重复发送整批原料」。

        原料缓冲一次只装一件，因此这一步要来回好几轮才备齐。**机器最后收到
        的总量必须恰好等于配方要的量**——多一件就说明某一轮把整批重发了一遍，
        而那种缺陷在缓冲够大的夹具里根本不会出现。
        """
        self.seed(IRON, 4)
        order_id = self.submit_order().payload["order_id"]
        for _ in range(12):
            self.drive(2)
        view = self.order_view(order_id)
        self.assertEqual(view["steps"][0]["status"], StepStatus.WAITING_OUTPUTS)
        self.assertEqual([item["delivered"] for item in view["steps"][0]["inputs"]], [2])
        self.assertEqual(self.machine.count_of(IRON), 2)
        # 库里也正好少了 2 个，不是 4 个。
        self.assertEqual(
            self.runtime.services.get(ServiceId.STORAGE).query(IRON)["stored"], 2
        )


class TestMultipleOrders(OneLevel):
    """09-07 的基础轮转：多张订单共享一帧预算。

    夹具补上**第二组处理器与内存槽**——容量是从网络成员真算出来的
    （`refresh_capacity` 每帧重算），手工调 `slots.refresh` 会被它当场推翻。
    """

    extra_devices = (
        (key(0, 64, 1), CONNECTOR),
        (key(0, 64, 2), PROCESSOR),
        (key(2, 64, 1), MEMORY),
    )

    def two_orders(self):
        self.seed(IRON, 8)
        self.assertEqual(self.service.slots.capacity, 2)
        self.assertEqual(self.service.memory.capacity, 2)
        first = self.submit_order(request_id="a").payload["order_id"]
        second = self.submit_order(request_id="b").payload["order_id"]
        return first, second

    def test_the_two_orders_do_not_share_material(self):
        first, second = self.two_orders()
        self.assert_admitted(self.settle(first))
        self.assert_admitted(self.settle(second))
        self.assertEqual(self.service.slots.used(), 2)
        rows = self.service.orders
        self.assertEqual(rows.live_count(), 2)
        first_row = rows.get(first).reservations[IRON.canonical()]
        second_row = rows.get(second).reservations[IRON.canonical()]
        self.assertEqual(first_row["claimed"], 2)
        self.assertEqual(second_row["claimed"], 2)
        # 声明只是纸面数，真正的凭据在 05。**守恒**：库里的 + 样板口备着的
        # + 已经投进机器的 = 原来的 8；而「还锁着的 + 搬出网络的」= 两份
        # 声明之和 4——一份料被两个订单各算一遍才会在这里出现 8。
        # 两个订单谁先备料是调度顺序决定的，因此断言写成**与顺序无关**的
        # 守恒式，而不是钉死某一个中间状态。
        storage = self.runtime.services.get(ServiceId.STORAGE)
        entry = storage.query(IRON)
        batch = self.server.ports.get(PORT_KEY).batch
        staged = batch.staged if batch is not None else 0
        delivered = self.machine.count_of(IRON)
        self.assertEqual(entry["stored"] + staged + delivered, 8)
        self.assertEqual(entry["reserved"] + staged + delivered, 4)

    def test_a_one_unit_budget_still_advances_both_of_them(self):
        """**游标每帧前移一位**，因此排在后面的订单不会饿死。

        `budget=1` 时一帧只够推一张订单——这正是 04 的 `RuntimeManager`
        修过的那个形状（按分量编号先到先得会让后面的永久饿死）。
        """
        first, second = self.two_orders()
        states = []
        for _ in range(4):
            self.drive(1, budget=1)
            states.append(
                (
                    self.service.orders.get(first).stats["ticks"],
                    self.service.orders.get(second).stats["ticks"],
                )
            )
        first_ticks = [row[0] for row in states]
        second_ticks = [row[1] for row in states]
        # 两张都在往前走，而不是一张连走四帧、另一张一帧没动。
        self.assertGreater(first_ticks[-1], 0)
        self.assertGreater(second_ticks[-1], 0)
        self.assertLessEqual(abs(first_ticks[-1] - second_ticks[-1]), 2)

    def test_the_tick_never_spends_more_than_its_budget(self):
        """预算上界是硬约束。

        `ServerBootstrap.tick()` 用的是装配时的 `runtime_budget`，因此这里
        改用**夹具自带的预算**推进，再直接读运行时管理器报出来的消耗——
        这正是 04 修「按分量编号先到先得」时留下的那条不变量。
        """
        self.two_orders()
        for _ in range(4):
            spent = self.server.runtimes.tick(1, self.server._tick_context())
            self.assertLessEqual(spent, 1)


if __name__ == "__main__":
    unittest.main()
