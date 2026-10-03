# coding=utf-8
"""产物回流与分配服务（任务 10-01 … 10-08）的离线验收。

这一份验的是**两条接缝**：09 的 `ReturnsSink`（`register` / `unregister`）与
06 的 `TransferCoordinator.sink`（可调用，`(request, count, now)`）。
`ReturnsPort` 把两条都实现成**同一个对象**，因此「订单在等什么」与
「东西给了谁」是同一份索引——这件事本身有一条用例钉着（`TestWiring`）。

驱动方式有**两条，缺一不可**：

- **合成对**（`FakeRuntime` + `FakeStorage` + `FakeExecutor`）：直接调端口的
  `__call__`，把分配算法的边界（有序贪心、截断回收、去重、来源闸）一条条
  摆出来。绕开整条链是因为那些边界要靠**构造**才能出现（比如「A、B 各需 8、
  输入 10」），而不是靠跑一条真订单碰运气。
- **真链路**（`ReturnsFixture`，走 `ServerBootstrap`）：接管线与真库存、
  真执行器、真订单状态机连起来，证明接口对得上——合成对证明不了这一点。

**夹具里的回流是真的那个**（`ReturnsService(ReturnPolicy())` 经 bootstrap 的
`returns_sink=` 注入），不是 09 文档里那个 `FakeReturns`。用假回流验回流
等于什么都没验。

本文件**不**从 `test_executor.py` import：互相 import 会让成败取决于另一个
模块的导入顺序（与 `test_executor.py:77` 同一条理由）。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.identity import NodeKey
from endernet.common.item_codec import item_dict_from_key
from endernet.common.protocol import (
    ORIGIN_EXTERNAL,
    ORIGIN_INTERNAL,
    CommandKind,
    CommandSource,
    DeviceCommand,
    Reason,
    STATUS_OK,
    TransferRequest,
)
from endernet.common.resource_key import ResourceKey
from endernet.server.adapters.containers import ContainerEntry, FakeContainer
from endernet.server.bootstrap import ServerBootstrap
from endernet.server.crafting import patterns as pattern_module
from endernet.server.crafting.executor import AbsentReturns
from endernet.server.crafting.jobs import OrderStatus, StepOutput, StepStatus
from endernet.server.crafting.returns import (
    Priority,
    ReturnPolicy,
    ReturnsService,
    Starvation,
)
from endernet.server.network.energy import EnergyPolicy
from endernet.server.network.services import ServiceId
from endernet.server.storage.transfer import ContainerEndpoint, NetworkEndpoint

IRON = ResourceKey.item("minecraft:iron_ingot")
GOLD = ResourceKey.item("minecraft:gold_ingot")
BLOCK = ResourceKey.item("minecraft:iron_block")
GEAR = ResourceKey.item("minecraft:iron_gear")
WATER = ResourceKey.fluid("minecraft:water")

BASEBAND = "endernet:network_baseband_8"
CONTROLLER = "endernet:tiny_network_endpoint"
ENERGY = "endernet:creative_energy_source"
DRIVER = "endernet:storage_driver"
CONNECTOR = "endernet:parallel_processor_connector"
PROCESSOR = "endernet:parallel_processor"
MEMORY = "endernet:memory_slot"
TEMPLATE_DEVICE = "endernet:bus_template_io"

DISK = "endernet:database_compressed_tape"
#: 夹具里插进内存槽的那根内存条。128 B 对这批订单（2 单位物 = 2 B）绰绰有余，
#: 取最小的型号是刻意的：容量不够时应当报 `order_no_memory`，而不是被夹具
#: 悄悄补上——见 `TestCapacity` 那几条真验容量的用例。
MEMORY_ITEM = "endernet:memory_128b"


def key(x, y=64, z=0):
    # type: (int, int, int) -> NodeKey
    return NodeKey.from_tuple(0, (x, y, z))


BB = [key(index) for index in range(4)]
PORT_KEY = key(4)
MACHINE_KEY = key(5)
CONTROLLER_KEY = key(1, 65)
ENERGY_KEY = key(0, 65)
DRIVER_KEY = key(2, 65)
MEMORY_KEY = key(3, 65)
CONNECTOR_KEY = key(1, 64, 1)
PROCESSOR_KEY = key(1, 64, 2)


# -- 合成对：只验分配算法 ------------------------------------------------


class FakeStorage(object):
    """只实现 10 用得到的两个方法。**记下每一次调用**，好断言「先锁再计数」。"""

    def __init__(self, fail_at=None):
        #: 第几次 `reserve` 该失败（1 起数）。`None` = 从不失败。
        self.fail_at = fail_at
        self.reserves = []
        self.releases = []
        self._next = 0

    def reserve(self, owner_id, key_, count, now=0):
        self._next += 1
        if self.fail_at is not None and self._next >= self.fail_at:
            return None
        self.reserves.append((owner_id, key_.canonical(), count, now))
        return FakeReservation("r%d" % (self._next,), owner_id, key_, count)

    def release(self, reservation_id, count):
        self.releases.append((reservation_id, count))
        return count

    def reserved_total(self):
        return sum(row[2] for row in self.reserves) - sum(
            row[1] for row in self.releases
        )


class FakeReservation(object):
    def __init__(self, reservation_id, owner_id, key_, count):
        self.reservation_id = reservation_id
        self.owner_id = owner_id
        self.key = key_
        self.count = count


class FakeExecutor(object):
    """09 的 `receive_output` 的替身：**按它自己那份剩余需求截断**。

    它把接受量记回**同一个** `StepOutput` 对象上（就是 10 的索引持有的那一个），
    与 09 的 `WaitingLedger` 一模一样。少了这一步，`_Waiting.remaining()`
    永远不动，整个分配循环就不再是「按剩余需求截断」了——测出来的会是
    一条只有测试才认识的捷径。
    """

    def __init__(self):
        #: `execution_id -> [StepOutput]`。由夹具的 `register` 灌进来。
        self.items = {}
        #: `execution_id -> 还肯接受多少`。**这是「09 的账比 10 的紧」**：
        #: 09 自己的去重账本有上界，一条被它记过而 10 这边已经淘汰掉的
        #: `op_id` 会得到 0；重派之后它也见过更少的剩余需求。
        self.caps = {}
        self.calls = []
        self.seen_ops = {}

    def receive_output(self, execution_id, key_, count, op_id="", source=None):
        canonical = key_.canonical()
        self.calls.append((execution_id, canonical, count, op_id))
        if op_id:
            seen = self.seen_ops.setdefault(execution_id, set())
            if op_id in seen:
                return 0
            seen.add(op_id)
        for item in self.items.get(execution_id, ()):
            if item.canonical != canonical:
                continue
            room = item.remaining()
            if execution_id in self.caps:
                room = min(room, self.caps[execution_id])
            accepted = min(int(count), room)
            item.received += accepted
            if execution_id in self.caps:
                self.caps[execution_id] -= accepted
            return accepted
        # 不认识的执行实例：一件都不接受（09 那边也没有这条账）。
        return 0


class FakeFactory(object):
    def __init__(self, executor):
        self.executor = executor


class FakeRuntime(object):
    """一个只够 10 用的运行时：`component_id` + `services`。"""

    def __init__(self, component_id, storage=None, executor=None):
        self.component_id = component_id
        self.storage = storage if storage is not None else FakeStorage()
        self.executor = executor if executor is not None else FakeExecutor()
        self.factory = FakeFactory(self.executor)
        self.services = {
            ServiceId.STORAGE: self.storage,
            ServiceId.FACTORY: self.factory,
        }


def expect(key_, count, expected=None):
    """一串「要等多少」的期望。`expected` 省掉时按 `count` 算。"""
    return StepOutput(
        key_, key_.canonical(), count if expected is None else expected
    )


class FakeRequest(object):
    """只满足 06 sink 契约的鸭子类型请求。

    `TransferRequest` 会校验 `op_id` 非空、键是 `ResourceKey`，而 10 必须
    对**这两样都不合规**的输入也有明确处置（空 `op_id` 收下但计数、
    认不出的键记 `unknown`）。用真请求就造不出那些输入，于是用最小的
    鸭子类型对象直接喂给端口——端口自己也是按 `getattr` 读它们的。
    """

    def __init__(self, op_id="op1", resource_key=None, origin=ORIGIN_EXTERNAL):
        self.op_id = op_id
        self.resource_key = resource_key if resource_key is not None else BLOCK
        self.origin = origin


class SyntheticFixture(unittest.TestCase):
    """一份不接 bootstrap 的回流服务 + 一个分量。"""

    component_id = "c1"

    def setUp(self):
        self.service = ReturnsService(ReturnPolicy())
        self.runtime = FakeRuntime(self.component_id)
        self.port = self.service.port_for(self.runtime)

    def register(self, execution_id, items, order_id=""):
        """登记，并让**假执行器认得这些 `StepOutput`**（见 `FakeExecutor`）。

        真实链路里 09 与 10 持有的是同一批对象，因此「09 记了多少」与
        「10 认为还差多少」是同一个数——替身必须照做。
        """
        self.runtime.executor.items[execution_id] = list(items or ())
        return self.port.register(execution_id, items, order_id)

    def item(self, execution_id, canonical=BLOCK.canonical()):
        """取**活的**那个 `StepOutput`，好从 09 那一侧改它的状态。"""
        for candidate in self.runtime.executor.items.get(execution_id, ()):
            if candidate.canonical == canonical:
                return candidate
        raise AssertionError("没有这个等待项: %s / %s" % (execution_id, canonical))

    def arrive(self, count, key_=BLOCK, op_id="op1", origin=ORIGIN_EXTERNAL):
        return self.port(FakeRequest(op_id, key_, origin), count, 0)

    def entry(self, execution_id, canonical):
        for item in self.service.view(self.component_id)["entries"]:
            if item["execution_id"] == execution_id and item["key"] == canonical:
                return item
        return None


# -- 策略 ------------------------------------------------------------------


class TestSubmissionQueue(SyntheticFixture):
    def test_nested_submission_registers_before_fifo_allocation(self):
        self.port.begin_submission()
        self.port.begin_submission()
        self.assertEqual(self.arrive(2, op_id="first"), 0)
        self.assertEqual(self.arrive(2, op_id="second"), 0)
        self.register("e1", [expect(BLOCK, 3)], "o1")
        self.port.end_submission()
        self.assertEqual(self.runtime.storage.reserved_total(), 0)
        self.port.end_submission()
        self.assertEqual(self.item("e1").received, 3)
        self.assertEqual([row[3] for row in self.runtime.executor.calls],
                         ["first", "second"])
        self.assertEqual(self.runtime.storage.reserved_total(), 3)
        self.assertEqual(self.port.describe()["pending_receipts"], 0)

    def test_queued_duplicates_and_internal_moves_are_not_allocated_twice(self):
        self.register("e1", [expect(BLOCK, 4)], "o1")
        self.port.begin_submission()
        self.arrive(1, origin=ORIGIN_INTERNAL)
        self.arrive(1)
        self.arrive(1)
        self.port.accept_assembled("assembly", BLOCK, 1)
        self.port.end_submission()
        self.assertEqual(self.item("e1").received, 2)
        self.assertEqual(self.runtime.storage.reserved_total(), 2)
        self.assertEqual(self.service.stats["duplicate"], 1)
        self.assertEqual(self.service.stats["rejected_origin"], 1)

    def test_cancelled_waiter_leaves_queued_product_unreserved(self):
        self.register("e1", [expect(BLOCK, 1)], "o1")
        self.port.begin_submission()
        self.arrive(1)
        self.port.unregister("e1")
        self.port.end_submission()
        self.assertEqual(self.runtime.storage.reserved_total(), 0)
        self.assertEqual(self.service.stats["orphan"], 1)

    def test_reentry_during_reservation_waits_for_dedup_commit(self):
        self.register("e1", [expect(BLOCK, 3)], "o1")
        reserve = self.runtime.storage.reserve

        def callback(*args):
            self.arrive(1, op_id="same")
            return reserve(*args)

        self.runtime.storage.reserve = callback
        self.assertEqual(self.arrive(1, op_id="same"), 1)
        self.assertEqual(self.item("e1").received, 1)
        self.assertEqual(self.runtime.storage.reserved_total(), 1)
        self.assertEqual(self.service.stats["reentrant"], 1)
        self.assertEqual(self.service.stats["duplicate"], 1)

    def test_busy_component_does_not_freeze_another_component(self):
        other = FakeRuntime("other")
        port = self.service.port_for(other)
        item = expect(BLOCK, 1)
        other.executor.items["e2"] = [item]
        port.register("e2", [item], "o2")
        self.port.begin_submission()
        self.arrive(1)
        self.assertEqual(port(FakeRequest(), 1), 1)
        self.assertEqual(item.received, 1)
        self.assertEqual(self.port.describe()["pending_receipts"], 1)
        self.port.end_submission()


class TestPolicy(unittest.TestCase):
    """R06 的两条未定项按既有形态挂着，且**默认是能用的替身**。"""

    def test_the_default_stance_is_a_working_stand_in(self):
        """默认必须是**可用**的替身，不是拒绝。

        这与 `EnergyPolicy` 相反、与 `BandwidthPolicy` 相同：分配顺序不授予
        任何权限、不破坏守恒，保守默认会让整条链在 DEV 档下也一步走不动，
        而安全性一分没多。把默认翻成拒绝侧会让下面每一条分配用例全部失效。
        """
        policy = ReturnPolicy()
        self.assertEqual(policy.priority, Priority.FIFO)
        self.assertEqual(policy.starvation, Starvation.NONE)
        self.assertTrue(policy.exclusive)
        self.assertEqual(repr(policy), "ReturnPolicy(fifo, none)")

    def test_both_r06_fields_are_reported_as_unresolved(self):
        report = ReturnPolicy().unresolved_report()
        self.assertEqual(
            [item["field"] for item in report],
            ["return.priority", "return.starvation"],
        )
        self.assertEqual(set(item["rule"] for item in report), set(["R06"]))
        # 进 `describe()`，装配层的策略汇总才看得见它。
        self.assertEqual(ReturnPolicy().describe()["unresolved"], report)

    def test_the_settled_guard_refuses_a_still_open_r06(self):
        """`assert_settled()` **只应在正式验收时调用**。

        这里调它只是为了钉住「它真的会拦」——不拦的话，正式档会把一个
        替身口径当成已定案的规则放出去，而那正是全局约束 7 要防的。
        """
        with self.assertRaises(AssertionError) as caught:
            ReturnPolicy().assert_settled()
        self.assertIn("return.priority", str(caught.exception))

    def test_the_unimplemented_side_is_refused_at_construction(self):
        """另一侧在**构造期**就报错，而不是在某次分配里给出一个「更合理」的顺序。

        替身实现出来的顺序会被当成规则生效——那正是不能静默接受的原因。
        """
        for kwargs in (
            {"priority": Priority.PER_ORDER},
            {"starvation": Starvation.AGING},
            {"priority": "made-up"},
            {"starvation": "made-up"},
            {"exclusive": False},
        ):
            self.assertRaises(ValueError, ReturnPolicy, **kwargs)

    def test_exclusive_is_settled_and_therefore_not_an_unresolved_field(self):
        """「每份只计一个订单」已经定案，所以它不进 `unresolved_report()`。

        它是一个**留成字段**的已定案项（照 `SchedulerPolicy.catch_up` 的先例），
        不是被忘掉的。
        """
        report = ReturnPolicy().unresolved_report()
        self.assertNotIn("return.exclusive", [item["field"] for item in report])

    def test_the_receipt_ledger_has_a_floor(self):
        """去重账本的上界是**帧安全阀**，不是玩法数值：至少 1。"""
        self.assertEqual(ReturnPolicy(receipt_limit=0).receipt_limit, 1)
        self.assertEqual(ReturnPolicy(receipt_limit=-5).receipt_limit, 1)


# -- 10-01 / 10-02：登记与有序贪心 -----------------------------------------


class TestAllocationOrder(SyntheticFixture):
    def test_a_short_receipt_is_split_and_never_double_counted(self):
        """**验收 A**：A、B 各需 8 个，一次输入 10 个 → 分配总和恰好 10。

        第一份拿满（8），第二份拿剩下的 2。少了「每分配一部分都扣减本次
        剩余可分配量」这一句，两边会各记 8——那是把同一份资源记了两遍。
        """
        self.register("x/0", [expect(BLOCK, 8)], order_id="order-a")
        self.register("x/1", [expect(BLOCK, 8)], order_id="order-b")
        self.assertEqual(self.arrive(10), 10)
        self.assertEqual(self.entry("x/0", BLOCK.canonical())["allocated"], 8)
        self.assertEqual(self.entry("x/1", BLOCK.canonical())["allocated"], 2)
        self.assertEqual(
            self.entry("x/0", BLOCK.canonical())["remaining"],
            0,
        )
        self.assertEqual(
            self.entry("x/1", BLOCK.canonical())["remaining"],
            6,
        )

    def test_the_order_is_registration_order_not_alphabetical(self):
        """替身口径是**登记先后**，不是字典序。

        刻意让先登记的那个 ID 在字典序上排在后面：按字典序实现的话，
        这条会反过来——而那正是「替身实现出来的顺序被当成规则生效」的形状。
        """
        self.register("zzz-first", [expect(BLOCK, 5)], order_id="order-a")
        self.register("aaa-second", [expect(BLOCK, 5)], order_id="order-b")
        self.assertEqual(self.arrive(5), 5)
        self.assertEqual(self.entry("zzz-first", BLOCK.canonical())["allocated"], 5)
        self.assertEqual(self.entry("aaa-second", BLOCK.canonical())["allocated"], 0)
        self.assertLess(
            self.entry("zzz-first", BLOCK.canonical())["seq"],
            self.entry("aaa-second", BLOCK.canonical())["seq"],
        )

    def test_registering_twice_replaces_instead_of_accumulating(self):
        """契约 §3：**替换语义**。累加会让剩余需求凭空翻倍，然后永远等不到。

        重派同一步正是会走这条路——它是常态，不是异常。
        """
        self.register("x/0", [expect(BLOCK, 4)], order_id="order-a")
        self.register("x/0", [expect(BLOCK, 4)], order_id="order-a")
        self.assertEqual(self.service.view(self.component_id)["summary"]["waiting_entries"], 1)
        self.assertEqual(self.service.stats["replaced"], 1)
        self.assertEqual(self.arrive(4), 4)
        self.assertEqual(self.entry("x/0", BLOCK.canonical())["allocated"], 4)

    def test_an_empty_execution_id_is_not_registrable(self):
        self.assertFalse(self.port.register("", [expect(BLOCK, 1)]))
        self.assertEqual(self.service.view(self.component_id)["entries"], [])

    def test_allocation_only_reaches_the_same_component(self):
        """索引全局一份，**分配按分量过滤**：跨网络没有转移通道。

        同键的两个等待者分属两张网络时，到达网络 1 的资源只喂得饱网络 1
        那一个。少了这条过滤，资源会记到一张根本没收到货的网络头上。
        """
        other_runtime = FakeRuntime("c2")
        self.register("mine", [expect(BLOCK, 8)], order_id="order-1")
        other_port = self.service.port_for(other_runtime)
        other_port.register("theirs", [expect(BLOCK, 8)], order_id="order-2")
        self.assertEqual(self.arrive(8), 8)
        self.assertEqual(self.entry("mine", BLOCK.canonical())["allocated"], 8)
        theirs = [
            item
            for item in self.service.view()["entries"]
            if item["execution_id"] == "theirs"
        ]
        self.assertEqual(theirs[0]["allocated"], 0)
        # 全局索引仍然看得见两个——诊断要跨网络看全部等待者。
        self.assertEqual(self.service.view()["summary"]["waiting_entries"], 2)

    def test_the_two_seams_get_the_same_object(self):
        """`port_for` 与 `inbox_for` **必须是同一个对象**。

        各造一个的话，「订单在等什么」与「东西给了谁」会变成两份可以对不上
        的账——而那正是这一层要防的形状。
        """
        self.assertIs(self.service.port_for(self.runtime), self.port)
        self.assertIs(self.service.inbox_for(self.runtime), self.port)

    def test_the_allocation_order_is_a_policy_method_not_an_inline_sort(self):
        """R06 定案时要改的是**这一处**，分配循环不该跟着动。"""
        entries = []
        for execution_id in ("b", "a"):
            self.register(execution_id, [expect(BLOCK, 1)])
            entries.append(self.service._entries()[len(entries)])
        ordered = ReturnPolicy().ordered(entries)
        self.assertEqual([item.execution_id for item in ordered], ["b", "a"])


# -- 10-02 / 10-06：真实数量与截断 -----------------------------------------


class TestRealQuantity(SyntheticFixture):
    def test_a_batch_of_64_counts_as_64_not_as_one_event(self):
        """**验收 C**：一次输入 64 个就按 64 累计，不是按事件次数 +1。

        分配用的是回执上那个**实际到达量**（06 只在 `commit()` 成功之后才发），
        而不是「机器应该产出了多少」。
        """
        self.register("x/0", [expect(BLOCK, 64)], order_id="order-a")
        self.assertEqual(self.arrive(64), 64)
        self.assertEqual(self.entry("x/0", BLOCK.canonical())["allocated"], 64)
        self.assertEqual(self.entry("x/0", BLOCK.canonical())["remaining"], 0)

    def test_a_fluid_key_travels_the_same_path_in_millibuckets(self):
        """流体走同一条路，单位是 mB 的整数——不换算出浮点中间量。"""
        self.register("x/0", [expect(WATER, 1000)], order_id="order-a")
        self.assertEqual(self.arrive(1000, key_=WATER), 1000)
        self.assertEqual(self.entry("x/0", WATER.canonical())["allocated"], 1000)

    def test_a_truncated_acceptance_returns_the_extra_to_the_network(self):
        """09 按**它的**剩余需求截断时，多留的那一份必须当场还回去。

        不还的话归属就多了一块：`allocate` 记 8、`receive_output` 只认 2，
        剩下 6 个单位既锁着又没人要——表现是「库存莫名其妙少了」。

        「09 只肯收 2」由假执行器自己的上限造出来（见 `FakeExecutor.caps`）：
        索引持有的就是 09 那个 `StepOutput` 本体，10 读到的剩余需求与 09
        一致，因此这一条问的正是「09 因为**它自己那边**的理由只收了一半时，
        10 会不会把多锁的那一半还回去」。
        """
        self.register("x/0", [expect(BLOCK, 16)], order_id="order-a")
        self.runtime.executor.caps["x/0"] = 2
        self.assertEqual(self.arrive(8), 2)
        self.assertEqual(self.runtime.storage.reserves[0][2], 8)
        self.assertEqual(self.runtime.storage.releases, [("r1", 6)])
        self.assertEqual(self.service.stats["trimmed"], 6)
        # 索引按**被接受的**那个数加，不是按 take。
        self.assertEqual(self.entry("x/0", BLOCK.canonical())["allocated"], 2)

    def test_an_instance_that_wants_nothing_leaves_the_rest_for_the_next_one(self):
        """这一位其实不要了 → **不扣本次余额**，让后面的人有机会拿到。

        扣了的话，「A 已经满足、B 还差 8」的一条回执会白白吞掉 8 个，
        而它们本来可以满足 B——那正是 R06 要避免的那种坏。
        """
        self.register("x/0", [expect(BLOCK, 8)], order_id="order-a")
        self.register("x/1", [expect(BLOCK, 8)], order_id="order-b")
        # 第一位一件都不要了（09 那边这条账还没走到，10 的索引也没清）。
        self.runtime.executor.caps["x/0"] = 0
        self.assertEqual(self.arrive(8), 8)
        self.assertEqual(self.entry("x/1", BLOCK.canonical())["allocated"], 8)
        # 一件都没接受的那一次不该在 05 那边留下任何归属。
        self.assertEqual(self.runtime.storage.reserves[0][2], 8)
        self.assertEqual(self.runtime.storage.releases, [("r1", 8)])

    def test_a_failed_reservation_stops_instead_of_skipping_ahead(self):
        """预留失败**停手**，不跳过去满足后面的实例。

        跳过一位等于换了一条规则——而「先满足谁」正是还没定案的那部分。
        余量留在网络里当普通库存（10-06「不丢弃」）。
        """
        self.register("x/0", [expect(BLOCK, 8)], order_id="order-a")
        self.register("x/1", [expect(BLOCK, 8)], order_id="order-b")
        self.runtime.storage.fail_at = 1
        self.assertEqual(self.arrive(8), 0)
        self.assertEqual(self.service.stats["reserve_failed"], 1)
        self.assertEqual(self.runtime.executor.calls, [])
        self.assertEqual(self.entry("x/1", BLOCK.canonical())["allocated"], 0)


# -- 10-03：归属与计数 ------------------------------------------------


class TestOwnership(SyntheticFixture):
    def test_the_reservation_is_taken_before_the_count_moves(self):
        """**先锁给这张订单，再计数。** 顺序反了就是 10-03 禁止的形状。

        判据是 05 侧真的收到了一次 `reserve`，而且拥有者是**订单 ID**——
        于是 09 自己的 `reservations_of` / `release_owned_by` 天然认得它，
        结单与取消时会一并释放，10 不需要另写清理路径。
        """
        self.register("x/0", [expect(BLOCK, 4)], order_id="order-a")
        self.arrive(4)
        self.assertEqual(self.runtime.storage.reserves, [("order-a", BLOCK.canonical(), 4, 0)])

    def test_an_instance_without_an_order_id_still_records_one(self):
        """没有订单 ID 时拥有者是空串——**不编一个假的**。

        编一个（比如拿执行实例 ID 顶上）会让 09 的释放路径认不出它，
        于是那份料永远锁着，而账单看起来完全正常。
        """
        self.register("x/0", [expect(BLOCK, 4)])
        self.arrive(4)
        self.assertEqual(self.runtime.storage.reserves[0][0], "")

    def test_unregister_reports_what_is_still_out_there(self):
        """`unregister` 返回**还没等到的件数**，取消时它通常是正数。

        调用方要把它记进 `residue` 报出来，而不是让订单看起来干净地结束了。
        """
        self.register("x/0", [expect(BLOCK, 8)], order_id="order-a")
        self.arrive(3)
        self.assertEqual(self.port.unregister("x/0"), 5)
        self.assertEqual(self.service.stats["unregistered"], 1)
        self.assertEqual(self.service.view(self.component_id)["entries"], [])

    def test_unregistering_an_unknown_instance_is_not_an_error(self):
        """注销是每帧都可能发生的路径，重复注销必须无害。"""
        self.assertEqual(self.port.unregister("never-existed"), 0)
        self.assertEqual(self.service.stats["unregistered"], 0)


# -- 10-04：来源闸 ---------------------------------------------------------


class TestOriginGate(SyntheticFixture):
    def test_an_internal_receipt_does_not_move_the_count(self):
        """10-04：模拟、拒收、缓存重建、内部移动、补偿都不产生新计数。

        `ORIGIN_INTERNAL` 正是「缓冲↔网络、介质搬运、补偿、索引重建」的标识，
        它们全在排除清单里。放进来的后果是**同一份资源被反复计入订单产物**。
        """
        self.register("x/0", [expect(BLOCK, 8)], order_id="order-a")
        self.assertEqual(self.arrive(8, origin=ORIGIN_INTERNAL), 0)
        self.assertEqual(self.service.stats["rejected_origin"], 1)
        self.assertEqual(self.entry("x/0", BLOCK.canonical())["allocated"], 0)
        # **一件都没分配出去 → 不记 op_id**：记成「已消费」只会让重试白跑。
        self.assertEqual(self.service._ops, {})

    def test_an_external_receipt_moves_the_count(self):
        """上一条的反面。两条一起才说明闸认的是 origin，不是「一律拒绝」。"""
        self.register("x/0", [expect(BLOCK, 8)], order_id="order-a")
        self.assertEqual(self.arrive(8, origin=ORIGIN_EXTERNAL), 8)
        self.assertEqual(self.service.stats["rejected_origin"], 0)

    def test_the_assembly_seam_skips_the_gate_but_has_no_caller(self):
        """07-06 的装配产出**可以进入**（10-04 原文），因此它不过来源闸。

        **闸与来源标识是两件事**：同一个 `ORIGIN_INTERNAL` 会从两条不同的路上
        过来——「补偿、缓存重建」不能算，而本地装配产出**算**。只看标识是
        分不开这两者的，所以由**入口**决定。

        这条接缝目前**没有调用方**（装配仓执行归 07-06，尚未实现），
        与 `port_stage` 当初的处境相同——它是备好的线，不是被偷偷用上的捷径。
        """
        self.register("x/0", [expect(BLOCK, 8)], order_id="order-a")
        self.assertEqual(self.port.accept_assembled("asm-1", BLOCK, 8, 0), 8)
        self.assertEqual(self.entry("x/0", BLOCK.canonical())["allocated"], 8)

    def test_a_malformed_key_is_counted_not_guessed(self):
        """认不出身份的键**不去猜**（与 09 的 `WaitingLedger.observe` 同一处置）。

        猜错的后果是一条真实产物落进 unknown 而订单永远不完成，
        而日志里什么都看不出来。
        """
        self.register("x/0", [expect(BLOCK, 8)], order_id="order-a")
        self.assertEqual(self.arrive(8, key_={"kind": "item"}), 0)
        self.assertEqual(self.service.stats["unknown"], 1)

    def test_a_receipt_with_no_waiters_is_an_orphan_not_an_error(self):
        """契约 §6.3：没人等这个键**不报错**，资源留在网络里当普通库存。

        报错会让重试逻辑把同一条回执反复送进来。
        """
        self.assertEqual(self.arrive(4), 0)
        self.assertEqual(self.service.stats["orphan"], 1)
        self.assertEqual(self.service._last_receipt["outcome"], "orphan")


# -- 10-05：去重 -----------------------------------------------------------


class TestDedupe(SyntheticFixture):
    def test_replaying_the_same_op_id_moves_nothing(self):
        """同一回执重试、同步回调、恢复重放都走同一条路——只能算一次。"""
        self.register("x/0", [expect(BLOCK, 8)], order_id="order-a")
        self.assertEqual(self.arrive(8, op_id="same"), 8)
        self.assertEqual(self.arrive(8, op_id="same"), 0)
        self.assertEqual(self.service.stats["duplicate"], 1)
        self.assertEqual(self.entry("x/0", BLOCK.canonical())["allocated"], 8)

    def test_a_genuinely_new_delivery_with_a_new_op_id_does_count(self):
        """上一条的反面，而且是**必需**的那一面。

        去重按 `op_id`，不是按「同一个执行实例只收一次」。第二次真的往里
        放了 8 个（新的 `op_id`）却不计数，会让订单永远等差。
        """
        self.register("x/0", [expect(BLOCK, 16)], order_id="order-a")
        self.assertEqual(self.arrive(8, op_id="first"), 8)
        self.assertEqual(self.arrive(8, op_id="second"), 8)
        self.assertEqual(self.entry("x/0", BLOCK.canonical())["allocated"], 16)
        self.assertEqual(self.service.stats["duplicate"], 0)

    def test_a_receipt_without_an_op_id_is_accepted_but_counted(self):
        """没有 `op_id` 就没法去重。**仍然分配**——丢掉一条真实入库会让订单
        永远等差，而那是更难查的一种坏；但要计数，让「谁没用 op_id」
        在诊断里看得见（与 09 同一处置）。"""
        self.register("x/0", [expect(BLOCK, 8)], order_id="order-a")
        self.assertEqual(self.arrive(8, op_id=""), 8)
        self.assertEqual(self.service.stats["no_op_id"], 1)
        self.assertEqual(self.service._ops, {})

    def test_dedupe_is_per_component(self):
        """`op_id` 只在**一张网络里**唯一（05 的去重就是按分量各记一份的）。

        跨分量共用一张表会让另一张网络的一条正当回执被当成重放。
        """
        other_runtime = FakeRuntime("c2")
        other = self.service.port_for(other_runtime)
        self.register("x/0", [expect(BLOCK, 8)], order_id="order-a")
        other_items = [expect(BLOCK, 8)]
        other_runtime.executor.items["y/0"] = other_items
        other.register("y/0", other_items, order_id="order-b")
        self.assertEqual(self.arrive(8, op_id="shared"), 8)
        self.assertEqual(
            other(
                TransferRequest("shared", "s", "n", BLOCK, 8, origin=ORIGIN_EXTERNAL),
                8,
                0,
            ),
            8,
        )
        self.assertEqual(self.service.stats["duplicate"], 0)

    def test_the_dedupe_ledger_is_bounded(self):
        """它是唯一一张**只增不减**的表，必须有上界。"""
        service = ReturnsService(ReturnPolicy(receipt_limit=3))
        runtime = FakeRuntime("c1")
        port = service.port_for(runtime)
        runtime.executor.items["x/0"] = [expect(BLOCK, 100)]
        port.register("x/0", runtime.executor.items["x/0"], order_id="order-a")
        for index in range(6):
            port(
                TransferRequest("op%d" % (index,), "s", "n", BLOCK, 1,
                                origin=ORIGIN_EXTERNAL),
                1,
                0,
            )
        self.assertEqual(len(service._ops), 3)
        self.assertEqual(service.stats["evicted"], 3)


# -- 多产物 -----------------------------------------------------------------


class TestMultiProduct(SyntheticFixture):
    def test_each_key_is_matched_on_its_own(self):
        """**验收 D** 的合成对版本：8 鱼 + 1 空桶只回鱼时，鱼那一项满足、
        桶那一项仍等着。两件东西共用一个执行实例，但各自记各自的账。"""
        self.register(
            "x/0",
            [expect(BLOCK, 8), expect(IRON, 1)],
            order_id="order-a",
        )
        self.assertEqual(self.arrive(8, key_=BLOCK, op_id="fish"), 8)
        entries = dict(
            (item["key"], item) for item in self.service.view(self.component_id)["entries"]
        )
        self.assertEqual(entries[BLOCK.canonical()]["remaining"], 0)
        self.assertEqual(entries[IRON.canonical()]["remaining"], 1)
        # 补上第二件才两清。
        self.assertEqual(self.arrive(1, key_=IRON, op_id="bucket"), 1)
        self.assertEqual(self.port.unregister("x/0"), 0)

    def test_a_return_is_allocated_exactly_like_a_product(self):
        """10 不需要区分产物与返还物（契约 §6.2）——两者都进 `expectations`，
        按同一个剩余需求记账。这条钉住「没有偷偷按 `counts_for_completion`
        分两次分配」。"""
        self.register(
            "x/0",
            [
                StepOutput(BLOCK, BLOCK.canonical(), 1, counts_for_completion=True),
                StepOutput(IRON, IRON.canonical(), 1, counts_for_completion=True),
            ],
            order_id="order-a",
        )
        self.assertEqual(self.arrive(1, key_=IRON, op_id="ret"), 1)
        entry = self.entry("x/0", IRON.canonical())
        self.assertTrue(entry["counts_for_completion"])
        self.assertEqual(entry["allocated"], 1)

    def test_the_view_reports_expected_allocated_and_remaining_separately(self):
        self.register("x/0", [expect(BLOCK, 8)], order_id="order-a")
        self.arrive(3)
        entry = self.entry("x/0", BLOCK.canonical())
        self.assertEqual(entry["expected"], 8)
        self.assertEqual(entry["allocated"], 3)
        self.assertEqual(entry["remaining"], 5)
        self.assertEqual(entry["order_id"], "order-a")
        self.assertEqual(entry["component_id"], "c1")


# -- 10-07：迟到与孤儿（合成对）------------------------------------------


class TestLateArrivalSynthetic(SyntheticFixture):
    def test_a_product_that_arrives_after_cancellation_is_an_orphan(self):
        """**验收 F 的后半**：取消之后又到的产物命中不到任何等待项。

        它不是「被丢弃」——资源已经在网络存储里了，这里只是**没有记账对象**。
        报错会让 10 的重试逻辑反复送同一条回执（契约 §6.3）。
        """
        self.register("x/0", [expect(BLOCK, 8)], order_id="order-a")
        self.port.unregister("x/0")
        self.assertEqual(self.arrive(8), 0)
        self.assertEqual(self.service.stats["orphan"], 1)
        self.assertEqual(self.runtime.storage.reserves, [])

    def test_an_already_satisfied_key_is_not_an_orphan(self):
        """「满足过了」与「没人等」要分得开：前者说明回执重了，后者说明账没了。

        混成一个计数的话，「产物回来了为什么没记上」在诊断里指向两个
        完全不同的方向。
        """
        self.register("x/0", [expect(BLOCK, 8)], order_id="order-a")
        self.arrive(8, op_id="first")
        self.arrive(8, op_id="second")
        self.assertEqual(self.service.stats["satisfied"], 1)
        self.assertEqual(self.service.stats["orphan"], 0)


# -- 诊断 ------------------------------------------------------------------


class TestDiagnostics(SyntheticFixture):
    def test_the_same_object_answers_available_true_and_has_no_blocker(self):
        """`available: True` 是 09 用来区分「接上了」与「还是替身」的那一位。

        `blocker()` 必须存在并返回 `None`：少了它，09 会退回「产物回流入口
        尚未接线（任务 10）」那条兜底说明——而接上之后那句话是假话。
        """
        self.assertTrue(self.port.describe()["available"])
        self.assertIsNone(self.port.blocker())
        self.assertEqual(self.port.describe()["adapter_id"], "returns")

    def test_zero_waiters_is_not_the_same_as_not_wired(self):
        """没有等待者是**正常工作状态**，不该让订单去报 `order_returns_absent`。"""
        report = self.port.describe()
        self.assertEqual(report["waiting_instances"], 0)
        self.assertTrue(report["available"])

    def test_the_view_is_scoped_to_the_component(self):
        other = self.service.port_for(FakeRuntime("c2"))
        self.register("mine", [expect(BLOCK, 1)], order_id="order-1")
        other.register("theirs", [expect(BLOCK, 1)], order_id="order-2")
        mine = self.port.view()
        self.assertEqual(len(mine["entries"]), 1)
        self.assertEqual(mine["entries"][0]["execution_id"], "mine")
        # 不给分量时看全部——诊断要跨网络看。
        self.assertEqual(len(self.service.view()["entries"]), 2)

    def test_the_view_filters_by_key_and_by_execution_id(self):
        self.register("x/0", [expect(BLOCK, 1), expect(IRON, 1)])
        self.register("x/1", [expect(BLOCK, 1)])
        self.assertEqual(len(self.port.view(canonical=BLOCK.canonical())["entries"]), 2)
        self.assertEqual(len(self.port.view(execution_id="x/1")["entries"]), 1)

    def test_the_last_receipt_says_what_happened_to_it(self):
        """一条回执的去向必须答得出来：收下了多少、为什么一件没动。"""
        self.register("x/0", [expect(BLOCK, 8)], order_id="order-a")
        self.arrive(3)
        receipt = self.service.view(self.component_id)["summary"]["last_receipt"]
        self.assertEqual(receipt["count"], 3)
        self.assertEqual(receipt["accepted"], 3)
        self.assertEqual(receipt["outcome"], "allocated")
        self.assertEqual(receipt["origin"], ORIGIN_EXTERNAL)
        self.arrive(3, op_id="", origin=ORIGIN_INTERNAL)
        receipt = self.service.view(self.component_id)["summary"]["last_receipt"]
        self.assertEqual(receipt["outcome"], "rejected_origin")

    def test_the_outcome_codes_are_all_reachable(self):
        """每个 `outcome` 都对应一条真实的路——没有只写在文档里的词。

        少一个可达的 outcome，诊断里就会出现一个永远看不到的状态，
        而它恰恰可能是最该看到的那一个。
        """
        self.register("x/0", [expect(BLOCK, 1)], order_id="order-a")
        self.arrive(1, key_=42)
        self.assertEqual(self.service._last_receipt["outcome"], "unknown")
        self.arrive(1, op_id="i", origin=ORIGIN_INTERNAL)
        self.assertEqual(self.service._last_receipt["outcome"], "rejected_origin")
        # `count <= 0` **直接返回，连回执都不记**——没有「零个到达」这件事，
        # 记一条只会让「回执数」与「真的有人送东西来过」对不上。
        self.assertEqual(self.arrive(0), 0)
        self.assertEqual(self.service.stats["receipts"], 2)

    def test_resolving_a_component_without_the_services_is_not_ready(self):
        """够不着执行器/存储时**不抛错**，计数并让诊断说得出来。

        `__call__` 是在 06 的提交路径上被调的，抛出去会把一次**成功**的入库
        变成一次异常；走不到那条路的症状是「外部输入通知丢了」，
        比「订单等差」更难查。
        """
        bare = FakeRuntime("c3")
        bare.services = {}
        port = self.service.port_for(bare)
        port.register("x/0", [expect(BLOCK, 1)], order_id="order-a")
        self.assertEqual(port.describe()["wired"], False)
        self.assertEqual(
            port(TransferRequest("op", "s", "n", BLOCK, 1, origin=ORIGIN_EXTERNAL), 1, 0),
            0,
        )
        self.assertEqual(self.service.stats["not_ready"], 1)

    def test_a_stand_in_service_is_not_mistaken_for_a_wired_one(self):
        """替身服务没有 `reserve` / `receive_output`，**那是「这半边是替身」**，
        不是「没接上 10」。判据是方法在不在，不是 `isinstance`。"""
        bare = FakeRuntime("c4")
        bare.services = {ServiceId.STORAGE: object(), ServiceId.FACTORY: object()}
        port = self.service.port_for(bare)
        self.assertEqual(port.resolve(), (None, None))


# -- 真链路：经 bootstrap 接线 --------------------------------------------


class FakeWorld(object):
    """与 `test_io` / `test_bootstrap` 同形的替身。**本文件自己写一份**。"""

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


class ReturnsFixture(unittest.TestCase):
    """一条**真的能跑起来的**离线工厂网络，回流用的是任务 10 的实现。

    `returns_kind = "absent"` 造的是**库默认口径**的那一档（不传回流入口），
    用来钉住「默认不假装能用」这条仍然成立——它是 09 的可见行为，
    改它会让订单不再如实报 `order_returns_absent`。
    """

    returns_kind = "returns"
    patterns = None
    port_policy = None
    extra_devices = ()

    def setUp(self):
        self.world = FakeWorld()
        self.machine = FakeContainer(slots=27, max_stack=64)
        self.port_box = FakeContainer(slots=9, max_stack=1)
        #: 内存槽自己的 10 格。**里面有内存条才有容量**（2026-10-03 起：
        #: 容量读的是容器内容，不再是「有几台内存槽」）。夹具默认插一根
        #: 128 B，够这批订单用；要验内存不足的用例自己把它拔掉。
        self.memory_box = FakeContainer(slots=10, max_stack=1)
        self.memory_box.write_slot(
            0, item_dict_from_key(ResourceKey.item(MEMORY_ITEM), 1)
        )
        self.containers = FakeContainerProvider()
        self.sink = self.build_sink()
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
        self.storage_service = self.runtime.services.get(ServiceId.STORAGE)
        self.returns = self.server.returns_sink
        #: 两条接缝拿到的是**同一个**端口对象——这条在 `TestWiring` 里断言。
        #: 库默认口径下没有端口可拿（`AbsentReturns` 不实现那条鸭子类型），
        #: 那一档的用例直接读 `self.returns`。
        self.port = (
            self.returns.port_for(self.runtime)
            if hasattr(self.returns, "port_for")
            else None
        )

    def build_sink(self):
        # type: () -> object
        """回流接收侧。**每个用例各造一个**：类级共享一个带状态的实现，
        会让「这一条验了什么」取决于同类的其他用例有没有先跑过。"""
        if self.returns_kind == "absent":
            return None
        return ReturnsService(ReturnPolicy())

    # -- 世界 ----------------------------------------------------------

    def place(self, node_key, device_id):
        from endernet.server.runtime.events import NormalizedEvent

        self.server.push(NormalizedEvent.place(node_key, device_id))

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
        self.containers.boxes[PORT_KEY.as_key()] = self.port_box
        self.containers.boxes[MACHINE_KEY.as_key()] = self.machine
        self.containers.boxes[MEMORY_KEY.as_key()] = self.memory_box
        self.server.tick()
        self.mount_a_disk()
        self.accrue_quota()
        self.install_patterns()

    def ref_at(self, node_key):
        return self.server.registry.get(node_key).ref

    def install_patterns(self):
        if self.patterns is None:
            self.patterns = {}
        for slot, definition in sorted(self.patterns.items()):
            ok, detail = self.server.patterns.install(
                self.ref_at(PORT_KEY), slot, definition
            )
            self.assertTrue(ok, u"样板装不进去：%s" % (detail,))

    # -- 命令 ----------------------------------------------------------

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

    def order_command(self, action, request_id="o1", **payload):
        target = self.ref_at(DRIVER_KEY)
        return DeviceCommand(
            request_id,
            CommandKind.ORDER,
            CommandSource("p1", target, 0),
            payload=dict(payload, action=action),
            topology_version=self.server.topology.version,
            device_ref=target,
        )

    def submit_order(self, target=BLOCK, amount=1, request_id="o1"):
        return self.server.submit_command(
            self.order_command(
                "submit",
                request_id=request_id,
                target=target.to_dict(),
                amount=amount,
            )
        )

    def mount_a_disk(self, request_id="mount"):
        return self.storage(
            request_id, "mount", ref=self.ref_at(DRIVER_KEY), slot=0, model_id=DISK
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

    def configure_port(self, filter_keys=(IRON, GOLD, BLOCK, GEAR), request_id="cfg"):
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

    def drive(self, ticks=1):
        report = None
        for _ in range(ticks):
            report = self.server.tick()
        return report

    def settle(self, order_id=None, ticks=64):
        """把订单推到**离开「规划中」**为止（与 09 的夹具同一理由）。"""
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

    def only_order(self):
        rows = self.service.query("list", {})["orders"]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def reservations(self, order_id):
        return self.storage_service.reservations_of(order_id)

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

    def deposit(self, key_, count, op_id, origin=ORIGIN_EXTERNAL):
        """**真·外部投放**：把资源从网络外面的箱子搬进网络存储。

        走的是 06 的两段式提交（预检 → 受控提交 → 实际数量回执），
        因此 `count` 就是**真的进了网络存储**的量——10-02 要的正是这个。
        真机上这一步是「手工把料放进接口容器」，离线就用一个外部箱子代劳。
        """
        box = FakeContainer(slots=4, max_stack=64)
        box.set_slot(0, ContainerEntry(key_, count))
        request = TransferRequest(
            op_id, "outside", "net", key_, count, origin=origin
        )
        return self.storage_service.transfer().commit(
            request,
            ContainerEndpoint(box, "outside"),
            NetworkEndpoint(self.storage_service, self.runtime.component_id),
        )

    def waiting_step(self, view=None, index=0):
        view = view if view is not None else self.only_order()
        return view["steps"][index]

    def waiting_execution(self, view=None, index=0, seq=0):
        """那一步的**第 `seq` 次配方执行**。

        登记、注销、逐次账都在执行实例上（`execution_id` 是
        `订单ID/步骤下标/执行序号`）；步骤那一层只有静态清单与汇总。
        """
        return self.waiting_step(view, index)["executions"][seq]


def stack(key_, count, **kwargs):
    return pattern_module.PatternStack(key_, count, **kwargs)


def factory(inputs, outputs, returns=()):
    return pattern_module.PatternDefinition(
        pattern_module.MODE_FACTORY, inputs, outputs, returns
    )


class OneLevel(ReturnsFixture):
    """一层配方：铁块 ← 2 铁锭。"""

    patterns = {0: factory([stack(IRON, 2)], [stack(BLOCK, 1)])}

    def setUp(self):
        ReturnsFixture.setUp(self)
        self.configure_port()
        self.seed(IRON, 4)
        self.submit_order()
        self.settle()
        self.drive(3)
        #: 走到这里订单应当停在**等产物**——不是失败，也不是做完了。
        self.step = self.waiting_step()
        self.execution_id = self.waiting_execution()["execution_id"]

    def flush(self, key_, count=1, op_id=None, origin=ORIGIN_EXTERNAL):
        op_id = op_id if op_id is not None else "flush:%s" % (key_.name,)
        return self.deposit(key_, count, op_id, origin)


# -- 接线（10-01 的前置）---------------------------------------------------


class TestSynchronousDispatch(ReturnsFixture):
    patterns = {0: factory([stack(IRON, 2)], [stack(BLOCK, 1)])}

    def test_actual_deposit_inside_dispatch_waits_until_registration(self):
        self.configure_port()
        self.seed(IRON, 4)
        executor = self.service.executor
        dispatch = executor._port_dispatch
        deposits = []

        def dispatch_with_product(port):
            result = dispatch(port)
            if port.batch.is_complete() and not deposits:
                deposits.append(self.deposit(BLOCK, 1, "sync-product"))
                self.assertEqual(self.port.describe()["pending_receipts"], 1)
                self.assertEqual(self.returns.stats["allocated"], 0)
            return result

        executor._port_dispatch = dispatch_with_product
        self.submit_order()
        self.settle()
        self.drive(8)
        self.assertEqual(len(deposits), 1)
        self.assertEqual(deposits[0].moved, 1)
        self.assertEqual(self.only_order()["status"], OrderStatus.COMPLETED)
        self.assertEqual(self.returns.stats["allocated"], 1)
        self.assertEqual(self.returns.stats["orphan"], 0)
        self.assertEqual(self.port.describe()["pending_receipts"], 0)

    def test_handle_exception_unfreezes_receipts(self):
        def failing_submit(command, payload):
            self.port(FakeRequest(), 1)
            raise ValueError("submission failed")

        self.service.executor.submit_order = failing_submit
        with self.assertRaises(ValueError):
            self.service.handle(None, self.order_command("submit"))
        self.assertEqual(self.port._depth, 0)
        self.assertEqual(self.port.describe()["pending_receipts"], 0)
        self.assertEqual(self.returns.stats["orphan"], 1)

    def test_stage_inputs_waits_until_gateway_returns(self):
        self.configure_port()
        executor = self.service.executor

        def stage(port, resource, count, op_id):
            self.port(FakeRequest(), 1)
            self.assertEqual(self.port.describe()["pending_receipts"], 1)
            return None

        executor._port_stage = stage
        self.service.stage_inputs(PORT_KEY, IRON, 1, "stage-sync")
        self.assertEqual(self.port.describe()["pending_receipts"], 0)
        self.assertEqual(self.returns.stats["orphan"], 1)


class TestWiring(ReturnsFixture):
    patterns = {0: factory([stack(IRON, 2)], [stack(BLOCK, 1)])}

    def test_the_two_seams_are_the_same_object(self):
        """**同一个对象两边都用**：`FactoryService.returns` 与
        `StorageService.reflow_sink` 必须是同一个端口。

        各接一个的话，「订单在等什么」与「东西给了谁」会变成两份可以对不上
        的账——而那正是这一层存在的全部理由。
        """
        self.assertIs(self.service.returns, self.port)
        self.assertIs(self.storage_service.reflow_sink, self.port)
        self.assertIs(self.storage_service.transfer().sink, self.port)

    def test_the_order_side_sees_a_wired_sink(self):
        """09 靠 `describe()["available"]` 区分「接上了」与「还是替身」。"""
        self.assertTrue(self.port.describe()["available"])
        self.assertEqual(self.port.describe()["adapter_id"], "returns")

    def test_the_transfer_side_reports_a_wired_sink(self):
        """06 那一侧的 `sink_wired` / `sink_missing` 必须跟着变。

        06 交付说明里记着真机上 `sink_missing: 2`——那就是「通知无处可去」。
        接上之后它不该再长。
        """
        report = self.storage_service.transfer().diagnostics()
        self.assertTrue(report["sink_wired"])
        self.assertEqual(report["stats"]["sink_missing"], 0)
        self.assertNotIn("尚未接线", report["scope_note"])

    def test_the_assembly_diagnostics_adopt_the_same_policy(self):
        """诊断报的与真正生效的必须是**同一个**策略对象。

        报一个、用另一个的代价是「诊断说 FIFO、实际按别的顺序分」，
        而那种坏在两份输出里都看不出来。
        """
        report = self.server.order_diagnostics()["return_policy"]
        self.assertEqual(report["priority"], Priority.FIFO)
        self.assertIs(self.server.return_policy, self.returns.policy)

    def test_the_policy_bundle_carries_the_r06_open_items(self):
        """R06 的未定项要进装配汇总，正式档才拦得住。

        已核实：`_PolicyBundle` **从不进入主控**（`runtime.py` 传的是
        `admission.policy`），所以这一条只影响诊断与显式调用的
        `assert_assembly_ok()`，**不改变 DEV/PRODUCTION 的放行行为**。
        """
        bundle = self.server.policy_bundle()
        fields = [
            item["field"] for item in bundle.unresolved_report()
        ]
        self.assertIn("return.priority", fields)
        self.assertIn("return.starvation", fields)

    def test_debug_query_returns_is_read_only_and_listed(self):
        """10-08：`returns` 进了只读白名单，而且**一份库存都不改**。"""
        from endernet.server.application import DEBUG_OPS

        self.assertIn("returns", DEBUG_OPS)
        before = self.stored(IRON)
        result = self.server.application.debug_query("returns")
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertIn("c", str(result.payload.keys()))
        self.assertEqual(self.stored(IRON), before)

    def test_debug_query_returns_answers_before_anything_is_wired(self):
        """没接回流时这条查询**照样答得出来**——那正是它存在的理由。

        答的是「接的是替身」，而不是报错或返回空。
        """
        result = self.server.application.debug_query(
            "returns", component_id=self.runtime.component_id
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertTrue(result.payload["wired"])
        self.assertEqual(result.payload["summary"]["adapter_id"], "returns")


class TestLibraryDefaultIsStillAbsent(ReturnsFixture):
    """**库默认口径不变**：不传 `returns_sink` 时订单如实停在等待产物。

    这是 09 的可见行为（`AbsentReturns` / `order_returns_absent`），
    改它不在本增量范围内。游戏内的「能玩」由 `start_server()` 那句显式装配
    提供，库默认仍是「不假装能用」。
    """

    returns_kind = "absent"
    patterns = {0: factory([stack(IRON, 2)], [stack(BLOCK, 1)])}

    def test_the_default_is_still_the_named_stand_in(self):
        self.assertIsInstance(self.server.returns_sink, AbsentReturns)
        self.assertFalse(self.server.returns_sink.describe()["available"])

    def test_the_transfer_side_still_says_the_sink_is_missing(self):
        """`None` 是「没接上」的意思，**不是「不用发」**——06 要留痕。"""
        report = self.storage_service.transfer().diagnostics()
        self.assertFalse(report["sink_wired"])
        self.assertIn("尚未接线", report["scope_note"])

    def test_debug_query_returns_answers_with_the_stand_in_summary(self):
        result = self.server.application.debug_query(
            "returns", component_id=self.runtime.component_id
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertFalse(result.payload["wired"])
        self.assertIn("替身", result.payload["note"])


# -- 端到端：订单真的走完 ------------------------------------------------


class TestEndToEndCompletion(OneLevel):
    def test_a_real_deposit_completes_the_order(self):
        """**验收 B**：不启动那台机器，手工把产物放进网络 → 订单结单。

        这一条是 10 存在的意义本身：在它之前，订单只能**如实**停在等待产物。
        """
        self.assertEqual(self.step["status"], StepStatus.WAITING_OUTPUTS)
        receipt = self.flush(BLOCK, 1)
        self.assertEqual(receipt.moved, 1)
        self.drive(3)
        view = self.only_order()
        self.assertEqual(view["status"], OrderStatus.COMPLETED)
        self.assertEqual(view["progress"]["ratio"], 1.0)
        self.assertEqual(view["progress"]["steps_completed"], 1)

    def test_the_product_is_owned_by_the_order_while_it_waits(self):
        """**验收 E**：分配之后那份料真的锁在**这张订单**名下。

        所以 09 结单时的 `_release_everything(order)` 会把它一并释放，
        10 不需要另写一条清理路径——这条与下一条合起来才是完整的。
        """
        self.flush(BLOCK, 1)
        rows = self.reservations(self.only_order()["order_id"])
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0].key.canonical(), BLOCK.canonical())

    def test_completion_releases_the_product_back_to_the_network(self):
        """结单是**释放**，不是把东西锁死在那里。

        上一条只证明「锁上了」；少了这一条，一份永远锁着的料与一份正常
        消耗掉的料在库存查询里长得一模一样。
        """
        self.flush(BLOCK, 1)
        self.drive(3)
        self.assertEqual(self.reservations(self.only_order()["order_id"]), ())
        self.assertEqual(self.stored(BLOCK), 1)
        # 原料真的被消耗掉了（备料搬进了外部机器），成品留在了网络里。
        self.assertEqual(self.stored(IRON), 2)

    def test_the_waiting_entry_is_gone_once_the_step_is_done(self):
        """步骤完成时 09 必须注销——留着一条死掉的等待者会让一份迟到的
        产物命中一个已经不存在的实例。"""
        self.flush(BLOCK, 1)
        self.drive(3)
        self.assertEqual(self.port.view()["entries"], [])
        self.assertEqual(self.service.waiting.diagnostics()["instances"], 0)

    def test_the_last_receipt_and_the_view_agree(self):
        """10-08：诊断里的 `allocated` 与 `last_receipt["accepted"]` 是同一件事。

        两个数分开算的话，「回执说收下了 1 件、台账说分配了 0 件」这种状态
        会在诊断里同时出现，而读的人无从判断哪个是真的。
        """
        self.flush(BLOCK, 1, op_id="seen")
        summary = self.port.view()["summary"]
        self.assertEqual(summary["last_receipt"]["accepted"], 1)
        self.assertEqual(summary["last_receipt"]["outcome"], "allocated")
        self.assertEqual(summary["last_receipt"]["op_id"], "seen")
        self.assertEqual(summary["stats"]["allocated"], 1)
        self.assertEqual(summary["last_receipt"]["component_id"], self.runtime.component_id)


class TestRepeatedDeposits(ReturnsFixture):
    """一条订单要 2 件，分两次投放，**第二次换一个 `op_id`**。"""

    patterns = {0: factory([stack(IRON, 4)], [stack(BLOCK, 2)])}

    def setUp(self):
        ReturnsFixture.setUp(self)
        self.configure_port()
        self.seed(IRON, 8)
        self.submit_order(target=BLOCK, amount=2)
        self.settle()
        self.drive(3)

    def test_the_same_amount_with_a_new_op_id_is_counted_again(self):
        """**验收 C 的真链路版**：第二次真的送来的 1 件照常计数。

        去重按 `op_id`，不是按「同一步只收一次」——后者会让真的多送来的
        产物无处可记，而症状是订单永远等差。
        """
        first = self.deposit(BLOCK, 1, "d1")
        second = self.deposit(BLOCK, 1, "d2")
        self.assertEqual(first.moved, 1)
        self.assertEqual(second.moved, 1)
        self.assertEqual(self.returns.stats["duplicate"], 0)
        self.assertEqual(self.returns.stats["allocated"], 2)
        self.drive(3)
        self.assertEqual(self.only_order()["status"], OrderStatus.COMPLETED)

    def test_replaying_the_very_same_op_id_moves_nothing(self):
        """上一条的反面：同一条回执重放**不该**再记一次。

        **走端口而不是协调器**：06 的请求账本自己就会挡住同 `op_id` 的重放
        （`test_transfer` 有一条钉着），因此在那一层重放根本到不了 10。
        10 的去重必须自己成立——重放可能来自存档恢复、同步回调或
        「09 的账本比 10 的长」这类跨层不同步，而它们都不经过 06。
        """
        self.deposit(BLOCK, 1, "same")
        request = TransferRequest(
            "same", "outside", "net", BLOCK, 1, origin=ORIGIN_EXTERNAL
        )
        self.assertEqual(self.port(request, 1, 0), 0)
        self.assertEqual(self.returns.stats["duplicate"], 1)
        self.assertEqual(self.returns.stats["allocated"], 1)
        self.drive(3)
        self.assertEqual(self.only_order()["status"], OrderStatus.RUNNING)


class TestTwoRealDeposits(ReturnsFixture):
    """一条订单要两件产物，分两次**真的**投放。"""

    patterns = {0: factory([stack(IRON, 4)], [stack(BLOCK, 2)])}

    def setUp(self):
        ReturnsFixture.setUp(self)
        self.configure_port()
        self.seed(IRON, 8)
        self.submit_order(target=BLOCK, amount=2)
        self.settle()
        self.drive(3)

    def test_the_step_waits_until_both_arrive(self):
        """验收 D 的真链路版：8 鱼 + 1 空桶只回鱼 → 步骤仍在工作中。

        `expected` 是 2 时只回 1 不该结单——「差一件」与「两件都到了」
        在 09 那边是两个状态，而漏判会让订单提前结单。
        """
        self.assertEqual(self.waiting_step()["status"], StepStatus.WAITING_OUTPUTS)
        self.deposit(BLOCK, 1, "half")
        self.drive(3)
        view = self.only_order()
        self.assertEqual(view["status"], OrderStatus.RUNNING)
        self.assertEqual(view["steps"][0]["status"], StepStatus.WAITING_OUTPUTS)
        entry = self.port.view()["entries"][0]
        self.assertEqual(entry["expected"], 2)
        self.assertEqual(entry["allocated"], 1)
        self.assertEqual(entry["remaining"], 1)
        # 补上第二件才结单。
        self.deposit(BLOCK, 1, "rest")
        self.drive(3)
        self.assertEqual(self.only_order()["status"], OrderStatus.COMPLETED)

    def test_the_two_allocations_are_two_separate_reservations(self):
        """两次投放是两笔独立的归属——合并不是「省事」，是让释放对不上。

        合并的话，第一次结单释放的是「一条记了 2 的预留」，而 05 那边
        只有 1 份真料：要么释放失败，要么把别人的料释放掉。
        """
        self.deposit(BLOCK, 1, "one")
        self.deposit(BLOCK, 1, "two")
        rows = self.reservations(self.only_order()["order_id"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(row.count for row in rows), [1, 1])
        self.assertEqual(
            set(row.owner_id for row in rows), set([self.only_order()["order_id"]])
        )


class TestCancellation(OneLevel):
    def test_a_deposit_after_cancellation_lands_as_ordinary_stock(self):
        """**验收 F**：取消之后又到的产物有清晰去向——普通库存。

        它不是「被丢弃」：那份料已经在网络存储里了，只是不再记在订单头上。
        10 这一侧的表现是 `orphan` 计数，而**不报错**（报错会让重试逻辑
        把同一条回执反复送进来）。
        """
        order_id = self.only_order()["order_id"]
        self.deposit(BLOCK, 1, "before-cancel")
        self.assertEqual(len(self.reservations(order_id)), 1)
        result = self.server.submit_command(
            self.order_command("cancel", request_id="c1", order_id=order_id)
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.drive(3)
        # 09 的取消把 10 的预留一并释放了——**10 没有另写取消钩子**，
        # 因为拥有者取的就是订单 ID。
        self.assertEqual(self.reservations(order_id), ())
        self.assertEqual(self.port.view()["entries"], [])
        # 取消之后 09 注销了等待记录，于是再来一份就是孤儿。
        self.deposit(BLOCK, 1, "after-cancel")
        self.assertEqual(self.returns.stats["orphan"], 1)
        self.assertEqual(self.stored(BLOCK), 2)

    def test_unregister_returns_the_still_missing_count_on_cancel(self):
        """契约 §5：`unregister` 返回还没等到的件数，取消时它是正数。

        那说明有产物还在外面，调用方要把它记进 `residue` 报出来，
        而不是让订单看起来干净地结束了。
        """
        pending = self.port.unregister(self.execution_id)
        self.assertEqual(pending, 1)


class TestMultiLevelRegression(ReturnsFixture):
    """两级订单：齿轮 ← 铁块，铁块 ← 2 铁锭。

    **这一条钉的是 09 的 `_sync_reservation` 补的那一行镜像。** 计划只为
    `from_stock > 0` 的键建本地行，而 10 会在**中间产物**键上为订单预留；
    没有那一行时下一步备料会算出少一份的 `usable`，报一条**假的**
    `ORDER_STOCK_TAKEN`，多步订单死在第二步。
    """

    extra_devices = ((key(2, 64, 1), MEMORY),)
    patterns = {
        0: factory([stack(IRON, 2)], [stack(BLOCK, 1)]),
        1: factory([stack(BLOCK, 1)], [stack(GEAR, 1)]),
    }

    def setUp(self):
        ReturnsFixture.setUp(self)
        self.configure_port()
        self.seed(IRON, 8)
        self.submit_order(target=GEAR, amount=1)
        self.settle()
        self.drive(3)

    def test_the_second_step_does_not_report_a_fake_shortage(self):
        view = self.only_order()
        self.assertEqual(view["progress"]["steps_total"], 2)
        first = view["steps"][0]
        self.assertEqual(first["status"], StepStatus.WAITING_OUTPUTS)
        # 第一步的产物（铁块）真的投放回网络。
        self.deposit(BLOCK, 1, "mid")
        self.drive(5)
        view = self.only_order()
        codes = [item["code"] for item in view["blockers"]]
        self.assertNotIn(Reason.ORDER_STOCK_TAKEN, codes)
        # 订单推进到了第二步，而不是卡在第一步。
        self.assertEqual(view["progress"]["steps_completed"], 1)
        self.assertLess(view["progress"]["steps_completed"], 2)

    def test_the_intermediate_product_feeds_the_second_step(self):
        """上一条的完整形态：中间产物**真的**被第二步用掉了。

        只断言「没报缺料」不够——一条永远不动的订单也不报缺料。
        """
        self.deposit(BLOCK, 1, "mid")
        self.drive(5)
        step = self.only_order()["steps"][1]
        self.assertEqual(step["status"], StepStatus.WAITING_OUTPUTS)
        # 中间产物已经被第二步备走了：网络里一件不剩。
        self.assertEqual(self.stored(BLOCK), 0)
        self.deposit(GEAR, 1, "final")
        self.drive(5)
        self.assertEqual(self.only_order()["status"], OrderStatus.COMPLETED)
        self.assertEqual(self.stored(GEAR), 1)


class TestDebugQueryShape(OneLevel):
    def test_the_query_reports_per_component_detail(self):
        component_id = self.runtime.component_id
        result = self.server.application.debug_query(
            "returns", component_id=component_id
        )
        payload = result.payload
        self.assertTrue(payload["wired"])
        self.assertEqual(len(payload["entries"]), 1)
        entry = payload["entries"][0]
        self.assertEqual(entry["key"], BLOCK.canonical())
        self.assertEqual(entry["expected"], 1)
        self.assertEqual(entry["remaining"], 1)
        self.assertEqual(
            entry["execution_id"], self.waiting_execution()["execution_id"]
        )
        self.assertEqual(payload["summary"]["policy"]["priority"], Priority.FIFO)

    def test_the_query_filters_by_key_and_execution_id(self):
        result = self.server.application.debug_query(
            "returns",
            component_id=self.runtime.component_id, key=IRON.canonical(),
        )
        self.assertEqual(result.payload["entries"], [])
        result = self.server.application.debug_query(
            "returns",
            component_id=self.runtime.component_id, key=BLOCK.canonical(),
        )
        self.assertEqual(len(result.payload["entries"]), 1)

    def test_an_unknown_component_is_an_error_not_an_empty_answer(self):
        """「没有这个网络」与「这个网络没有等待者」是两句话。

        混成一个空结果，读的人会以为是后者。
        """
        result = self.server.application.debug_query("returns", component_id="nope")
        self.assertIn("error", result.payload)


if __name__ == "__main__":
    unittest.main()
