# coding=utf-8
"""订单领域层（任务 09-01 / 09-03）的离线验收。

覆盖范围：订单与步骤的**状态迁移表**（包括每一条非法边都要被拒绝）、
进度口径、等待台账（登记/注销/去重/上限/孤儿）、订单集合与 ID 形状、
哨兵替身 `AbsentReturns`，以及槽位与内存租约（含「同一拥有者领两次」
这条会静默漏账的边界）。

**这里不碰网络**：`jobs.py` 与 `capacity.py` 都是纯领域，一条命令都不发。
真的去预留、备料、派发的那条链在 `test_executor.py` 里。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.protocol import Reason
from endernet.common.resource_key import ResourceKey
from endernet.server.crafting import capacity, jobs
from endernet.server.crafting.executor import AbsentReturns

IRON = ResourceKey.item("minecraft:iron_ingot")
GOLD = ResourceKey.item("minecraft:gold_ingot")
BLOCK = ResourceKey.item("minecraft:iron_block")


class TestOrderStatusMachine(unittest.TestCase):
    """订单状态机：**不在表里的边一律拒绝**，包括「从终态复活」。"""

    def order(self, status=None):
        row = jobs.Order("order@1/m1/1", 1, "m1", BLOCK, BLOCK.canonical(), 4)
        if status is not None:
            row.status = status
        return row

    def test_the_documented_states_are_all_reachable(self):
        for status in (
            jobs.OrderStatus.PLANNING,
            jobs.OrderStatus.QUEUED,
            jobs.OrderStatus.RUNNING,
            jobs.OrderStatus.BLOCKED,
            jobs.OrderStatus.CANCELLING,
            jobs.OrderStatus.COMPLETED,
            jobs.OrderStatus.CANCELLED,
            jobs.OrderStatus.FAILED,
        ):
            self.assertIn(status, jobs.ORDER_TRANSITIONS)

    def test_every_edge_in_the_table_is_accepted(self):
        """表里写着的每一条边都必须真的能走——表与实现不能各说各话。"""
        for source in sorted(jobs.ORDER_TRANSITIONS):
            for target in sorted(jobs.ORDER_TRANSITIONS[source]):
                row = self.order(source)
                self.assertTrue(
                    row.move_to(target, "测试", 7),
                    u"%s -> %s 应当被接受" % (source, target),
                )
                self.assertEqual(row.status, target)
                self.assertEqual(row.updated_tick, 7)

    def test_terminal_statuses_have_no_way_out(self):
        """终态是**没有出口**的：静默接受一条复活边，等于让一个已经被取消的
        订单某天又开始动别人的库存。"""
        for source in (
            jobs.OrderStatus.COMPLETED,
            jobs.OrderStatus.CANCELLED,
            jobs.OrderStatus.FAILED,
        ):
            self.assertEqual(jobs.ORDER_TRANSITIONS[source], frozenset())
            for target in sorted(jobs.ORDER_TRANSITIONS):
                if target == source:
                    continue
                self.assertFalse(self.order(source).move_to(target), target)

    def test_an_edge_outside_the_table_is_refused(self):
        """`PLANNING -> RUNNING` 不在表里：没排过队就不能直接开跑。"""
        row = self.order(jobs.OrderStatus.PLANNING)
        self.assertFalse(row.move_to(jobs.OrderStatus.RUNNING))
        self.assertEqual(row.status, jobs.OrderStatus.PLANNING)

    def test_moving_to_the_same_status_is_a_no_op_not_an_error(self):
        row = self.order(jobs.OrderStatus.BLOCKED)
        self.assertTrue(row.move_to(jobs.OrderStatus.BLOCKED))
        self.assertEqual(row.status, jobs.OrderStatus.BLOCKED)

    def test_a_failed_move_does_not_write_the_detail(self):
        """被拒绝的迁移不能把 `status_detail` 改掉——那是一句会骗人的话。

        用 `QUEUED -> COMPLETED` 当例子：一份排着队、**一步都还没跑**的订单
        不该直接结单。`PLANNING -> COMPLETED` 反而是合法的（零步骤的
        `HOLD` 计划就从那里直接结单，见 `test_an_order_whose_target_is_
        already_in_stock_finishes_at_once`）——这条用例原先挑的正是那一条边，
        真机上补上它之后这里才换成一条真的非法的边。
        """
        row = self.order(jobs.OrderStatus.QUEUED)
        row.move_to(jobs.OrderStatus.COMPLETED, u"做完了")
        self.assertEqual(row.status_detail, "")
        self.assertEqual(row.status, jobs.OrderStatus.QUEUED)

    def test_terminal_and_live_partition_every_status(self):
        union = jobs.TERMINAL_STATUSES | jobs.LIVE_STATUSES
        self.assertEqual(union, frozenset(jobs.ORDER_TRANSITIONS))
        self.assertEqual(jobs.TERMINAL_STATUSES & jobs.LIVE_STATUSES, frozenset())


class TestStepStatusMachine(unittest.TestCase):
    def step(self, status):
        row = jobs.OrderStep(0, BLOCK.canonical(), {}, 1)
        row.status = status
        return row

    def test_the_chain_is_one_way(self):
        """链是单向的：料真的进了机器就不可能假装没进（「不伪造回滚外部机器」）。

        两个例外，各自都有明确的理由：

        - `BLOCKED` 不是链上的一环，而是「暂停在这里」，因此可以从任何非终态
          进来、也可以回到当时那一处继续（`blocked_from` 记着是哪一处）。
        - `DISPATCHED -> STAGING` 是**「还有差额没进机器」**那条路：外部机器
          当时只收了一部分，余量留在中转位，于是回去按差额再备一点。
          它走的不是「从头再来一遍」——从头再来就是重发整批原料。
        """
        chain = [
            jobs.StepStatus.WAITING_INPUTS,
            jobs.StepStatus.STAGING,
            jobs.StepStatus.DISPATCHED,
            jobs.StepStatus.WAITING_OUTPUTS,
            jobs.StepStatus.COMPLETED,
        ]
        for index, source in enumerate(chain[:-1]):
            self.assertIn(chain[index + 1], jobs.STEP_TRANSITIONS[source])
        backward = []
        for index, source in enumerate(chain[1:], start=1):
            for earlier in chain[:index]:
                if earlier in jobs.STEP_TRANSITIONS[source]:
                    backward.append((source, earlier))
        self.assertEqual(
            backward, [(jobs.StepStatus.DISPATCHED, jobs.StepStatus.STAGING)]
        )

    def test_completed_and_cancelled_are_terminal(self):
        self.assertEqual(jobs.STEP_TRANSITIONS[jobs.StepStatus.COMPLETED], frozenset())
        self.assertEqual(jobs.STEP_TRANSITIONS[jobs.StepStatus.CANCELLED], frozenset())
        self.assertTrue(self.step(jobs.StepStatus.COMPLETED).is_terminal_step())
        self.assertFalse(self.step(jobs.StepStatus.BLOCKED).is_terminal_step())

    def test_blocked_can_resume_anywhere(self):
        """阻塞之后要能回到**它自己原来的位置**，而不是从头再来一遍——
        从头再来就是「重发整批原料」，验收明令禁止。"""
        for target in (
            jobs.StepStatus.WAITING_INPUTS,
            jobs.StepStatus.STAGING,
            jobs.StepStatus.DISPATCHED,
            jobs.StepStatus.WAITING_OUTPUTS,
        ):
            self.assertIn(target, jobs.STEP_TRANSITIONS[jobs.StepStatus.BLOCKED])

    def test_a_blocked_step_remembers_where_it_was(self):
        step = self.step(jobs.StepStatus.WAITING_OUTPUTS)
        step.blocked_from = jobs.StepStatus.WAITING_OUTPUTS
        step.status = jobs.StepStatus.BLOCKED
        self.assertEqual(step.blocked_from, jobs.StepStatus.WAITING_OUTPUTS)


class TestStepInput(unittest.TestCase):
    def test_debt_and_to_stage_track_different_things(self):
        """`debt`（还没进机器）与 `to_stage`（还没离开网络）是两个数。

        搬进中转位的那部分已经**离开网络**了，再搬一次就是重复抽取；
        但它还没进机器，因此这一步仍然没做完。两个数分开，才不会
        一边重发一边提前完成。
        """
        row = jobs.StepInput(IRON, IRON.canonical(), 10)
        self.assertEqual((row.debt(), row.to_stage()), (10, 10))
        row.staged = 10
        self.assertEqual((row.debt(), row.to_stage()), (10, 0))
        row.delivered = 4
        self.assertEqual((row.debt(), row.to_stage()), (6, 0))
        row.delivered = 10
        self.assertTrue(row.is_satisfied())

    def test_staging_more_than_required_never_shows_as_negative(self):
        row = jobs.StepInput(IRON, IRON.canonical(), 2)
        row.staged = 5
        row.delivered = 5
        self.assertEqual(row.to_stage(), 0)
        self.assertEqual(row.debt(), 0)


class TestOrderProgress(unittest.TestCase):
    """进度 = 已完成的配方数 / 计划里配方树的总配方数。"""

    def order(self, statuses):
        row = jobs.Order("order@1/m1/1", 1, "m1", BLOCK, BLOCK.canonical(), 1)
        row.steps = []
        for index, status in enumerate(statuses):
            step = jobs.OrderStep(index, BLOCK.canonical(), {}, 1)
            step.status = status
            row.steps.append(step)
        return row

    def test_ratio_is_completed_over_total(self):
        row = self.order(
            [
                jobs.StepStatus.COMPLETED,
                jobs.StepStatus.COMPLETED,
                jobs.StepStatus.WAITING_OUTPUTS,
                jobs.StepStatus.WAITING_INPUTS,
            ]
        )
        view = row.progress()
        self.assertEqual(view["steps_total"], 4)
        self.assertEqual(view["steps_completed"], 2)
        self.assertAlmostEqual(view["ratio"], 0.5)
        self.assertEqual(view["outstanding_step"], 2)
        self.assertEqual(view["by_status"][jobs.StepStatus.COMPLETED], 2)

    def test_a_zero_step_plan_has_no_denominator(self):
        """零步骤的 `HOLD` 计划（「库里已经够了」）没有分母。

        返回 `0/0` 或者一个凭空的 `1` 都会让界面画出一条不存在的进度条；
        这里按**订单是不是已完成**给 0.0 / 1.0，并且说明写清楚。
        """
        row = self.order([])
        self.assertEqual(row.progress()["ratio"], 0.0)
        row.status = jobs.OrderStatus.COMPLETED
        self.assertEqual(row.progress()["ratio"], 1.0)
        self.assertEqual(row.progress()["steps_total"], 0)

    def test_cancelled_and_blocked_are_counted_separately(self):
        """取消的步骤**不从完成数里扣**：「8/10 完成、1 个取消」与
        「7/10 完成」在界面上是两件不同的事。"""
        row = self.order(
            [
                jobs.StepStatus.COMPLETED,
                jobs.StepStatus.CANCELLED,
                jobs.StepStatus.BLOCKED,
            ]
        )
        view = row.progress()
        self.assertEqual(view["steps_completed"], 1)
        self.assertEqual(view["steps_cancelled"], 1)
        self.assertEqual(view["steps_blocked"], 1)
        self.assertEqual(view["steps_total"], 3)

    def test_progress_is_part_of_the_order_view(self):
        """进度跟着订单一起给，**不另开一个查询入口**——两处各算一次迟早不一样。"""
        row = self.order([jobs.StepStatus.COMPLETED])
        self.assertIn("progress", row.to_dict())
        self.assertEqual(
            row.to_dict()["progress"]["steps_completed"], 1
        )


class TestWaitingLedger(unittest.TestCase):
    """每执行实例每产物的剩余需求 + 回执按 `op_id` 去重。"""

    def setUp(self):
        self.ledger = jobs.WaitingLedger()
        self.expectation = jobs.StepOutput(BLOCK, BLOCK.canonical(), 5, True)

    def register(self, execution_id="e1"):
        return self.ledger.register(execution_id, [self.expectation])

    def test_register_then_observe_accumulates(self):
        self.register()
        self.assertEqual(self.ledger.observe("e1", BLOCK, 2, "op1"), 2)
        self.assertEqual(self.ledger.observe("e1", BLOCK, 3, "op2"), 3)
        self.assertTrue(self.ledger.is_satisfied("e1"))
        self.assertEqual(self.ledger.pending("e1"), {})

    def test_a_replayed_op_id_is_not_counted_twice(self):
        """10-05：同一条回执重试不得再次加数。

        返回 0 而不是「上次接受的那一份」：调用方问的是「这一次又收下多少」，
        重复回执收下的是 0。返回原值会让恢复流程把同一次入库算两遍。
        """
        self.register()
        self.assertEqual(self.ledger.observe("e1", BLOCK, 2, "op1"), 2)
        self.assertEqual(self.ledger.observe("e1", BLOCK, 2, "op1"), 0)
        self.assertEqual(self.expectation.received, 2)
        self.assertEqual(self.ledger.stats["replayed"], 1)

    def test_over_delivery_is_not_accepted(self):
        """多出来的产物**不接受**，留在网络里当普通库存（10-06 明写不丢弃）。"""
        self.register()
        self.assertEqual(self.ledger.observe("e1", BLOCK, 9, "op1"), 5)
        self.assertEqual(self.ledger.observe("e1", BLOCK, 1, "op2"), 0)

    def test_an_unknown_execution_id_is_counted_not_raised(self):
        """迟到的产物命中的**可能是一个已经取消的实例**。

        报错会让 10 的重试逻辑把同一条回执反复送进来；静默丢掉又会让
        「谁在等」这件事查不出来。因此收下、计数、返回 0。
        """
        self.register()
        self.assertEqual(self.ledger.observe("gone", BLOCK, 3, "op1"), 0)
        self.assertEqual(self.ledger.stats["orphan"], 1)

    def test_a_key_this_instance_does_not_wait_for(self):
        self.register()
        self.assertEqual(self.ledger.observe("e1", GOLD, 3, "op1"), 0)
        self.assertEqual(self.ledger.stats["unknown"], 1)

    def test_canonical_text_is_accepted_as_well_as_a_key(self):
        """两种形状（活的 `ResourceKey` 与规范文本）必须得到同一个结果。

        10 那边可能拿着字符串来问（它自己也是从纯数据里读的），
        只认对象会把一条真实回执丢掉。
        """
        self.register()
        self.assertEqual(self.ledger.observe("e1", BLOCK.canonical(), 2, "op1"), 2)
        self.assertEqual(self.ledger.observe("e1", BLOCK, 3, "op2"), 3)
        self.assertTrue(self.ledger.is_satisfied("e1"))

    def test_something_that_is_neither_is_refused_rather_than_guessed(self):
        """既不是键也不是规范文本的东西**不去猜**。

        猜错的后果是一条真实产物落进 `unknown`、订单永远不完成，
        而日志里连一条线索都没有。
        """
        self.register()
        self.assertEqual(self.ledger.observe("e1", object(), 3, "op1"), 0)
        self.assertEqual(self.expectation.received, 0)

    def test_an_empty_op_id_is_accepted_but_counted(self):
        """没有 `op_id` 就没法去重（10-05 要求它非空）。

        **仍然收下**——丢掉一条真实入库会让订单永远等差，那是更难查的一种坏；
        但要计数，让「谁没用 op_id」在诊断里看得见。
        """
        self.register()
        self.assertEqual(self.ledger.observe("e1", BLOCK, 2, ""), 2)
        self.assertEqual(self.ledger.observe("e1", BLOCK, 2, ""), 2)
        self.assertEqual(self.ledger.stats["no_op_id"], 2)

    def test_the_dedup_table_has_a_ceiling(self):
        """去重表是唯一一张只增不减的表，**必须有上界**。

        没有上界的话一张长跑存档会一直长下去；而它挡的只是「同一条回执
        重试」，保留最近的若干条足够。
        """
        ledger = jobs.WaitingLedger(op_limit=4)
        item = jobs.StepOutput(BLOCK, BLOCK.canonical(), 1000, True)
        ledger.register("e1", [item])
        for index in range(20):
            ledger.observe("e1", BLOCK, 1, "op%d" % (index,))
        self.assertEqual(len(ledger._ops), 4)
        self.assertEqual(ledger.stats["evicted"], 16)
        # 最近的那条仍然记得住。
        self.assertEqual(ledger.observe("e1", BLOCK, 1, "op19"), 0)

    def test_registering_again_replaces_rather_than_accumulates(self):
        """重派同一个执行实例时，期望应当被**重算**。

        累加会让剩余需求凭空翻倍，然后永远等不到。
        """
        self.register()
        self.ledger.observe("e1", BLOCK, 2, "op1")
        fresh = jobs.StepOutput(BLOCK, BLOCK.canonical(), 5, True)
        self.ledger.register("e1", [fresh])
        self.assertEqual(self.ledger.pending("e1"), {BLOCK.canonical(): 5})

    def test_unregister_reports_what_is_still_out_there(self):
        """注销时返回**还没等到多少件**：取消时如果是正数，说明有产物还在
        外面，调用方要把这个数报出来，而不是让订单看起来干净地结束了。"""
        self.register()
        self.ledger.observe("e1", BLOCK, 2, "op1")
        self.assertEqual(self.ledger.unregister("e1"), 3)
        self.assertEqual(self.ledger.unregister("e1"), 0)
        self.assertFalse(self.ledger.is_registered("e1"))

    def test_a_return_that_does_not_count_for_completion(self):
        """返还物（`counts_for_completion=False`）没到齐时**这一步仍然完成**。

        09 文档的另一侧：「8 鱼 + 1 空桶只回鱼时不完成」是默认那一侧的
        既定玩法（由 `OrderPolicy.returns_block_completion` 决定），
        而这里的标志位是给「明确外部自理」的条目用的（R09 的无限燃料一侧）。
        """
        outputs = jobs.StepOutput(BLOCK, BLOCK.canonical(), 1, True)
        returns = jobs.StepOutput(GOLD, GOLD.canonical(), 1, False)
        self.ledger.register("e1", [outputs, returns])
        self.ledger.observe("e1", BLOCK, 1, "op1")
        self.assertTrue(self.ledger.is_satisfied("e1"))
        # 但它仍然记着「还欠一份」，只是不挡住完成。
        self.assertEqual(self.ledger.pending("e1"), {GOLD.canonical(): 1})

    def test_the_view_is_plain_data(self):
        self.register()
        view = self.ledger.view("e1")
        self.assertEqual(view[BLOCK.canonical()]["expected"], 5)
        self.assertIsNone(self.ledger.view("nope"))
        self.assertIn("e1", self.ledger.view())
        self.assertEqual(self.ledger.diagnostics()["instances"], 1)


class TestOrderStore(unittest.TestCase):
    def setUp(self):
        self.store = jobs.OrderStore(3)

    def make(self, status=None):
        order = jobs.Order(self.store.new_id("m1"), 3, "m1", BLOCK, "iron_block", 1)
        if status is not None:
            order.status = status
        self.store.add(order)
        return order

    def test_the_id_carries_the_master_instance(self):
        """任务文档：**订单 ID 与主控实例 ID 稳定关联**。

        形状里带上它，一张订单属于哪台主控在字符串上就看得出来，
        12 恢复时也不必另存一份对应关系。
        """
        first = self.store.new_id("m1")
        second = self.store.new_id("m2")
        self.assertIn("m1", first)
        self.assertIn("m2", second)
        self.assertTrue(first.startswith("order@3/"))
        self.assertNotEqual(first, second)

    def test_orders_keep_creation_order(self):
        """按创建顺序，**不用字典的键序**——那会随哈希变化。"""
        ids = [self.make().order_id for _ in range(5)]
        self.assertEqual([row.order_id for row in self.store.orders()], ids)

    def test_only_terminal_orders_can_be_removed(self):
        """移除一个还在跑的订单等于让它的预留永远没人释放——
        预留记在 05 那侧，订单对象没了就查不出该放谁。"""
        live = self.make(jobs.OrderStatus.RUNNING)
        done = self.make(jobs.OrderStatus.COMPLETED)
        self.assertFalse(self.store.remove(live.order_id))
        self.assertIsNotNone(self.store.get(live.order_id))
        self.assertTrue(self.store.remove(done.order_id))
        self.assertIsNone(self.store.get(done.order_id))

    def test_live_count_skips_terminal_orders(self):
        self.make(jobs.OrderStatus.RUNNING)
        self.make(jobs.OrderStatus.COMPLETED)
        self.make(jobs.OrderStatus.CANCELLED)
        self.make(jobs.OrderStatus.BLOCKED)
        self.assertEqual(self.store.live_count(), 2)

    def test_adding_the_same_id_twice_is_refused(self):
        order = self.make()
        self.assertIsNone(self.store.add(order))

    def test_diagnostics_counts_by_status(self):
        self.make(jobs.OrderStatus.RUNNING)
        self.make(jobs.OrderStatus.BLOCKED)
        self.make(jobs.OrderStatus.BLOCKED)
        report = self.store.diagnostics()
        self.assertEqual(report["orders"], 3)
        self.assertEqual(report["by_status"][jobs.OrderStatus.BLOCKED], 2)


class TestAbsentReturns(unittest.TestCase):
    """任务 10 未接线时的具名替身：**它不假装能用**。"""

    def test_it_says_it_is_not_wired(self):
        sink = AbsentReturns()
        self.assertFalse(sink.describe()["available"])
        self.assertEqual(sink.describe()["pending_task"], "10")

    def test_register_refuses_but_still_records(self):
        """返回 False 而不是抛错：登记本身不该让订单提交失败；
        但收下内容，诊断才能回答「如果 10 在，它会看到哪些等待者」。"""
        sink = AbsentReturns()
        item = jobs.StepOutput(BLOCK, BLOCK.canonical(), 3, True)
        self.assertFalse(sink.register("e1", [item], "order@1"))
        self.assertEqual(sink.describe()["registered"], 1)
        self.assertEqual(sink.registered["e1"]["order_id"], "order@1")

    def test_the_blocker_names_the_pending_task(self):
        blocker = AbsentReturns().blocker()
        self.assertEqual(blocker.code, Reason.ORDER_RETURNS_ABSENT)
        self.assertIn("10", blocker.detail)

    def test_unregister_is_idempotent(self):
        sink = AbsentReturns()
        sink.register("e1", [])
        self.assertTrue(sink.unregister("e1"))
        self.assertFalse(sink.unregister("e1"))


class TestOrderPolicy(unittest.TestCase):
    def test_every_symbolic_knob_is_reported(self):
        """替身值必须逐条报出来，否则正式验收会为一条不存在的规则盖章。"""
        fields = set(
            item["field"] for item in capacity.OrderPolicy().unresolved_report()
        )
        for name in (
            "order.slots_per_order",
            "order.memory_units_per_slot",
            "order.memory_units_per_step",
            "order.returns_block_completion",
        ):
            self.assertIn(name, fields)

    def test_assert_settled_refuses_to_stamp_an_unsettled_policy(self):
        with self.assertRaises(AssertionError):
            capacity.OrderPolicy().assert_settled()

    def test_returns_block_completion_defaults_to_the_documented_side(self):
        """09 验收原文「所需鱼已回流但空桶未到时步骤仍工作中」是**看得见的
        既定玩法**，替身必须取这一侧。"""
        self.assertTrue(capacity.OrderPolicy().returns_block_completion)

    def test_non_positive_knobs_are_refused_at_construction(self):
        for kwargs in (
            {"slots_per_order": 0},
            {"memory_units_per_slot": 0},
            {"memory_units_per_step": 0},
            {"orders_per_tick": 0},
            {"max_orders": 0},
            {"op_ledger_limit": 0},
        ):
            with self.assertRaises(ValueError):
                capacity.OrderPolicy(**kwargs)

    def test_describe_matches_the_report_fields(self):
        policy = capacity.OrderPolicy()
        described = policy.describe()
        for item in policy.unresolved_report():
            name = item["field"].split(".", 1)[1]
            self.assertIn(name, described)
            self.assertEqual(described[name], item["value"])


class TestSlotLedger(unittest.TestCase):
    def setUp(self):
        self.ledger = capacity.SlotLedger()
        self.ledger.refresh(4)

    def test_claim_release_round_trip(self):
        lease = self.ledger.claim("o1", 2)
        self.assertEqual(lease.units, 2)
        self.assertEqual(self.ledger.used(), 2)
        self.assertEqual(self.ledger.available(), 2)
        self.assertEqual(self.ledger.release("o1"), 2)
        self.assertEqual(self.ledger.used(), 0)

    def test_it_refuses_rather_than_splitting(self):
        """要么全留、要么不留：给两个订单各发半份槽位，谁也开不了工，
        而两边的账都显示「拿到了」。"""
        self.assertIsNone(self.ledger.claim("o1", 5))
        self.assertEqual(self.ledger.stats["refused"], 1)
        self.assertEqual(self.ledger.leases, {})

    def test_the_same_owner_claiming_twice_does_not_leak(self):
        """**同一拥有者领两次不能把第一条租约弄丢。**

        存单个 id 的版本里，第二次领会把第一条的 id 覆盖掉——那条租约还躺在
        表里、`active` 仍是真，于是**永远放不掉**：可用量平白少掉几个槽位，
        而账面上查不出少在哪。
        """
        self.ledger.claim("o1", 1)
        self.ledger.claim("o1", 2)
        self.assertEqual(self.ledger.used(), 3)
        self.assertEqual(self.ledger.release("o1"), 3)
        self.assertEqual(self.ledger.used(), 0)
        self.assertEqual(self.ledger.leases, {})

    def test_release_returns_zero_for_an_unknown_owner(self):
        self.assertEqual(self.ledger.release("nobody"), 0)

    def test_capacity_shrinking_does_not_take_leases_back(self):
        """处理器被拔掉之后 `used > capacity` 是允许的：新的领不到，
        已有的跑完自然释放。强行收回会让一个已经投了料的订单失去它的
        槽位记录，而料是真的投出去了。"""
        lease = self.ledger.claim("o1", 3)
        self.assertEqual(self.ledger.refresh(1), -3)
        self.assertIsNotNone(lease)
        self.assertEqual(self.ledger.used(), 3)
        self.assertEqual(self.ledger.available(), 0)
        self.assertEqual(self.ledger.over_subscribed(), 2)
        self.assertEqual(self.ledger.stats["shrinkages"], 1)
        # 缩水期间新的领不到，也不会出现负的「可用」。
        self.assertIsNone(self.ledger.claim("o2", 1))

    def test_it_implements_the_processing_slots_contract(self):
        """`capacity.py` 的两个台账**就是**那两个能力契约的实现。"""
        self.assertEqual(self.ledger.slot_count(), 4)
        self.assertTrue(self.ledger.claim_slot("e1"))
        self.assertEqual(self.ledger.used(), 1)
        self.ledger.release_slot("e1")
        self.assertEqual(self.ledger.used(), 0)

    def test_claim_for_order_uses_the_policy(self):
        policy = capacity.OrderPolicy(slots_per_order=2)
        ledger = capacity.SlotLedger(policy)
        ledger.refresh(3)
        self.assertIsNotNone(ledger.claim_for_order("o1"))
        self.assertEqual(ledger.used(), 2)
        self.assertIsNone(ledger.claim_for_order("o2"))

    def test_non_positive_units_are_a_programming_error(self):
        with self.assertRaises(ValueError):
            self.ledger.claim("o1", 0)

    def test_describe_and_diagnostics_are_plain_data(self):
        self.ledger.claim("o1", 1)
        view = self.ledger.describe()
        self.assertEqual(view["kind"], "slot")
        self.assertEqual(view["used"], 1)
        self.assertEqual(len(view["leases"]), 1)
        self.assertIn("stats", self.ledger.diagnostics())


class TestMemoryLedger(unittest.TestCase):
    def test_memory_units_is_the_contract_method(self):
        ledger = capacity.MemoryLedger()
        ledger.refresh(6)
        self.assertEqual(ledger.memory_units(), 6)

    def test_claim_for_order_scales_with_the_step_count(self):
        policy = capacity.OrderPolicy(memory_units_per_step=2)
        ledger = capacity.MemoryLedger(policy)
        ledger.refresh(10)
        self.assertIsNotNone(ledger.claim_for_order("o1", 3))
        self.assertEqual(ledger.used(), 6)
        self.assertIsNone(ledger.claim_for_order("o2", 3))

    def test_zero_steps_is_refused_loudly(self):
        """零步骤的计划根本不该走到领租约这一步（它在 `_adopt_plan` 里就
        直接结单了）。返回 `None` 会让「不该调」与「内存不够」在调用方
        看来一模一样——后者是可恢复的业务结论，前者是代码写错了。"""
        ledger = capacity.MemoryLedger()
        ledger.refresh(10)
        with self.assertRaises(ValueError):
            ledger.claim_for_order("o1", 0)


class TestCapacitySources(unittest.TestCase):
    """容量只从**权威来源**算：02 的处理器群与网络的成员列表。"""

    def group(self, group_id, component_id, processors, ownership="attached",
              contributes=True, reasons=()):
        class FakeGroup(object):
            pass

        row = FakeGroup()
        row.group_id = group_id
        row.component_id = component_id
        row.processors = frozenset(processors)
        row.ownership = ownership
        row.reasons = reasons
        row.contributes_slots = lambda: contributes
        return row

    def snapshot(self, groups):
        class FakeSnapshot(object):
            pass

        row = FakeSnapshot()
        row.processor_groups = groups
        return row

    def test_only_contributing_groups_of_this_component_count(self):
        snapshot = self.snapshot(
            {
                "a": self.group("a", 1, [(0, 1, 64, 0), (0, 2, 64, 0)]),
                "b": self.group("b", 2, [(0, 3, 64, 0)]),
                "c": self.group("c", 1, [(0, 4, 64, 0)], contributes=False),
            }
        )
        total, idle = capacity.slot_capacity(snapshot, 1)
        self.assertEqual(total, 2)
        self.assertEqual(len(idle), 1)
        self.assertEqual(idle[0]["group_id"], "c")
        self.assertEqual(idle[0]["processors"], 1)

    def test_a_processor_in_two_groups_is_counted_once(self):
        """同一物理处理器**不被重复统计**（09 验收）。

        02 建群时已经去过重，因此这条正常恒不触发；这里构造的是那个
        「万一有人改了建群算法而忘了去重」的情形——症状是槽位凭空变多，
        而那种问题不看这个数几乎查不出来。
        """
        shared = (0, 1, 64, 0)
        snapshot = self.snapshot(
            {
                "a": self.group("a", 1, [shared, (0, 2, 64, 0)]),
                "b": self.group("b", 1, [shared]),
            }
        )
        total, idle = capacity.slot_capacity(snapshot, 1)
        self.assertEqual(total, 2)
        overlap = [row for row in idle if row["overlap"]]
        self.assertEqual(len(overlap), 1)
        self.assertEqual(overlap[0]["overlap"], 1)

    def test_no_topology_at_all_means_no_capacity(self):
        total, idle = capacity.slot_capacity(None, 1)
        self.assertEqual(total, 0)
        self.assertEqual(idle, [])

    def test_memory_comes_from_loaded_memory_devices(self):
        class FakeNode(object):
            def __init__(self, category, loaded):
                self.category = category
                self.loaded = loaded

        members = [
            FakeNode("memory", True),
            FakeNode("memory", True),
            FakeNode("memory", False),
            FakeNode("baseband", True),
        ]
        policy = capacity.OrderPolicy(memory_units_per_slot=3)
        total, count = capacity.memory_capacity(members, None, policy)
        self.assertEqual(count, 2)
        self.assertEqual(total, 6)

    def test_capacity_blockers_report_both_sides_separately(self):
        """两个都不够时**两条都要出现**：只报前一条会让玩家加完处理器
        再发现还缺内存。"""
        slots = capacity.SlotLedger(capacity.OrderPolicy(slots_per_order=2))
        memory = capacity.MemoryLedger(capacity.OrderPolicy(memory_units_per_step=4))
        slots.refresh(1)
        memory.refresh(2)
        blockers = capacity.capacity_blockers(slots, memory, 3)
        self.assertEqual(len(blockers), 2)
        details = " | ".join(item.detail for item in blockers)
        self.assertIn(u"槽位", details)
        self.assertIn(u"内存", details)
        for item in blockers:
            self.assertEqual(item.code, Reason.ORDER_NO_CAPACITY)

    def test_no_blockers_when_both_fit(self):
        slots = capacity.SlotLedger()
        memory = capacity.MemoryLedger()
        slots.refresh(1)
        memory.refresh(1)
        self.assertEqual(capacity.capacity_blockers(slots, memory, 1), [])


if __name__ == "__main__":
    unittest.main()
