# coding=utf-8
"""下单预览：只读的分帧规划、缺料结论与提交闸（任务 11 增量）。

**全部离线。** 夹具用**真的 `PlanJob`**（`planner.plan`）：预览的算法部分
就是规划器，替身一份「差不多」的实现只会测出替身的语义。替的只有
「快照从哪来」——真实那份要接 `PatternStore` 与 `StorageService`，
和 `test_planner.py` 里的做法一致。

这一套用例守的是契约里几条「错了也看不出来」的：

1. **预览不占任何执行资源**：它没有订单、没有内存租约、没有预留，
   `advance` 只推一个配方节点。
2. **缺料是整单的**：任何一步缺料就禁用「下单」，且标红的行与
   `plan.blocked_keys` 是同一批。
3. **提交闸六条各有各的拒绝理由**，尤其是「同 `request_id` 重放」由主控
   去重账本管、「换 `request_id` 拿同一份预览」由「已消费」管——两层缺一不可。
4. **分批只有一份权威**：状态头报的片数与实际发的批数同一个来源。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.protocol import (
    CommandKind,
    CommandResult,
    CommandSource,
    DeviceCommand,
    Reason,
    STATUS_OK,
    STATUS_REJECTED,
)
from endernet.common.resource_key import ResourceKey
from endernet.server.crafting import planner, preview

BLOCK = ResourceKey.item("minecraft:iron_block")
INGOT = ResourceKey.item("minecraft:iron_ingot")
COAL = ResourceKey.item("minecraft:coal")

Status = planner.PlanStatus

#: 样板索引的替身。`PreviewService` 只把它转交给 `capture_patterns`，
#: 而夹具的 `capture_patterns` 根本不看它——但**没有它就该报「没接上」**
#: （见 `is_ready`），因此不能传 `None`。
PATTERNS = object()


# -- 夹具 ------------------------------------------------------------------


def stack(name, count=1):
    return planner  # 占位：下面用 patterns.PatternStack


def _stack(name, count=1):
    from endernet.server.crafting import patterns
    return patterns.PatternStack(ResourceKey.item(name), count)


def _recipe(inputs, outputs):
    from endernet.server.crafting import patterns
    return patterns.PatternDefinition(patterns.MODE_FACTORY, inputs, outputs)


def _candidate(definition):
    return planner.PatternCandidate(
        0, (0, 10, 64, 0), 1, "endernet:bus_template_io", 0,
        definition.digest(), definition.mode, definition, True, None,
    )


class StubPlanner(object):
    """`PreviewService` 要的那几个口。**算的是真的 `PlanJob`**。

    `capture_inventory` / `capture_patterns` 的形参（存储服务与样板索引）
    在这里用不上——真实那份就是从它们身上抓快照的；替身直接把夹具给的
    数字与配方交出去。`policy` 是真的 `PlanPolicy`：预览要读它的
    `target_semantics`（R11）。
    """

    def __init__(self, recipes, totals=None, policy=None):
        self.recipes = recipes
        self.totals = totals or {}
        self.policy = policy if policy is not None else planner.PlanPolicy()

    def capture_inventory(self, storage=None, topology_version=None):
        return planner.InventorySnapshot(
            dict((key, (count, 0, count)) for key, count in self.totals.items())
        )

    def capture_patterns(self, store=None, topology_version=None):
        index = {}
        for key, definition in self.recipes.items():
            index[key] = (_candidate(definition),)
        return planner.PatternSnapshot(index)

    def plan(self, target, amount, inventory, patterns):
        return planner.plan(
            target, amount, inventory, patterns, policy=self.policy
        )


def one_step_recipes():
    """一块铁块 = 9 铁锭 + 1 煤。**两步之外的都被这一条挡住。**"""
    return {
        BLOCK: _recipe(
            [_stack("minecraft:iron_ingot", 9), _stack("minecraft:coal", 1)],
            [_stack("minecraft:iron_block", 1)],
        ),
    }


class PreviewCase(unittest.TestCase):
    def setUp(self):
        self.service = preview.PreviewService(
            planner=StubPlanner(one_step_recipes()),
            patterns=PATTERNS,
            policy=preview.PreviewPolicy(),
        )

    def command(self, action, **overrides):
        source = CommandSource(
            overrides.pop("player_id", "p1"),
            None,
            0,
            session_id=overrides.pop("session_id", "s1"),
            received_tick=overrides.pop("tick", 0),
        )
        return DeviceCommand(
            overrides.pop("request_id", "r1"),
            CommandKind.ORDER,
            source,
            payload=dict(overrides),
        )

    def request(self, target=BLOCK, amount=1, **kwargs):
        payload = {"target": target.to_dict(), "amount": amount}
        payload.update(kwargs)
        return self.service.request(self.command("preview_request"), payload)

    def run_to_completion(self, limit=200):
        """把预算喂到算完为止，返回喂了几帧。"""
        for index in range(limit):
            self.service.advance({"tick": index, "frame": index})
            if not self.service._jobs:
                return index + 1
            if all(job.status != Status.RUNNING
                   for job in self.service._jobs.values()):
                return index + 1
        raise AssertionError("预览没有在 %d 帧内算完" % (limit,))

    def delivery(self, session_id="s1"):
        return self.service.delivery_for(session_id)

    def solo(self):
        return list(self.service._jobs.values())[0]

    def stocked(self, totals=None):
        """一份**料够**的服务：目标能从库存里直接凑出来。"""
        return preview.PreviewService(
            planner=StubPlanner(
                one_step_recipes(), totals or {INGOT: 9, COAL: 1}
            ),
            patterns=PATTERNS,
            policy=preview.PreviewPolicy(),
        )


# -- 只读与分帧 ------------------------------------------------------------


class TestStepping(PreviewCase):
    def test_a_request_only_creates_the_job_and_does_not_step(self):
        """**请求那一帧不算任何节点**：09 的订单也是这个形状
        （`_start_planning` 明写「节点从下一帧起每帧算一个」）。"""
        result = self.request(amount=4)
        self.assertEqual(result.status, STATUS_OK)
        job = self.solo()
        self.assertEqual(job.status, Status.RUNNING)
        self.assertEqual(job.job.spent(), 0)

    def test_a_running_preview_reports_no_rows_and_no_submit(self):
        self.request()
        state = self.delivery()["state"]
        self.assertEqual(state["status"], Status.RUNNING)
        self.assertFalse(state["submit_ok"], "还在算就不许下单")
        self.assertEqual(state["packet_count"], 0)
        self.assertEqual(self.delivery()["chunks"], ())

    def test_each_frame_advances_exactly_one_recipe_node(self):
        self.request(amount=4)
        spent = []
        for index in range(3):
            self.service.advance({"tick": index, "frame": index})
            spent.append(self.solo().job.spent())
        # 每一帧恰好 +1；一帧里多算一步就说明预算单位不是配方节点。
        self.assertEqual(spent, [1, 2, 3])

    def test_the_same_frame_does_not_double_charge(self):
        self.request(amount=4)
        self.service.advance({"tick": 0, "frame": 7})
        first = self.solo().job.spent()
        self.service.advance({"tick": 0, "frame": 7})
        self.assertEqual(self.solo().job.spent(), first, "同一帧重入不多算")

    def test_completing_turns_the_state_into_rows(self):
        self.service = self.stocked()
        self.request(amount=1)
        self.run_to_completion()
        delivery = self.delivery()
        self.assertEqual(delivery["state"]["status"], Status.COMPLETE)
        self.assertTrue(delivery["state"]["submit_ok"])
        self.assertTrue(delivery["chunks"], "算完了就该有原料行")

    def test_a_preview_owns_no_execution_resources(self):
        """**预览不建订单、不占内存、不预留。** 结构上就不该有那只手。"""
        self.request()
        service = self.service
        for name in ("orders", "memory", "slots"):
            self.assertFalse(
                hasattr(service, name), "预览服务不该有 %s" % (name,)
            )


# -- 缺料 ------------------------------------------------------------------


class TestShortfall(PreviewCase):
    def test_nothing_in_stock_means_missing_and_no_submit(self):
        self.request(amount=1)
        self.run_to_completion()
        delivery = self.delivery()
        self.assertTrue(delivery["state"]["missing"])
        self.assertFalse(delivery["state"]["submit_ok"], "缺料就不许下单")
        rows = [row for chunk in delivery["chunks"] for row in chunk]
        missing = dict((row["canonical"], row["missing"]) for row in rows)
        self.assertTrue(missing[INGOT.canonical()], "铁锭缺")
        self.assertTrue(missing[COAL.canonical()], "煤缺")

    def test_enough_stock_means_no_missing_and_submit_allowed(self):
        self.service = preview.PreviewService(
            planner=StubPlanner(
                one_step_recipes(),
                {INGOT: 9, COAL: 1},
            ),
            patterns=PATTERNS,
            policy=preview.PreviewPolicy(),
        )
        self.request(amount=1)
        self.run_to_completion()
        delivery = self.delivery()
        self.assertFalse(delivery["state"]["missing"])
        self.assertTrue(delivery["state"]["submit_ok"])

    def test_the_red_rows_are_exactly_the_blocked_keys(self):
        """**标红与「下单被拒」是同一个谓词**（契约第 4 节末句）。"""
        self.service = preview.PreviewService(
            planner=StubPlanner(one_step_recipes(), {INGOT: 9}),
            patterns=PATTERNS,
            policy=preview.PreviewPolicy(),
        )
        self.request(amount=1)
        self.run_to_completion()
        delivery = self.delivery()
        plan = self.solo().job.result()
        blocked = set(entry["canonical"] for entry in plan.blocked_keys)
        rows = [row for chunk in delivery["chunks"] for row in chunk]
        red = set(row["canonical"] for row in rows if row["missing"])
        self.assertEqual(red, blocked)
        # **目标键自己也在里面**：它的配方选不出来，因此它也是「拿不到」
        # 的那一个。契约第 4 节的传播正是这么定义的——受影响的不只是叶子。
        self.assertEqual(
            blocked,
            set([BLOCK.canonical(), COAL.canonical()]),
            "缺煤，于是铁块也做不出来",
        )

    def test_a_byproduct_row_is_not_a_requirement(self):
        """`demand <= 0` 的行是**纯副产物**，不是一条需求。"""
        self.request(amount=1)
        self.run_to_completion()
        rows = [row for chunk in self.delivery()["chunks"] for row in chunk]
        self.assertTrue(all(row["demand"] > 0 for row in rows))

    def test_the_blocked_table_says_what_is_missing_in_words(self):
        self.request(amount=1)
        self.run_to_completion()
        blocked = self.delivery()["state"]["blocked"]
        self.assertTrue(blocked)
        for entry in blocked:
            self.assertTrue(entry["canonical"])
            self.assertTrue(entry["detail"], "光有键没有说明，界面上看不出缺什么")


# -- 分批 ------------------------------------------------------------------


class TestChunks(PreviewCase):
    def test_the_header_packet_count_matches_the_chunks_sent(self):
        self.service = preview.PreviewService(
            planner=StubPlanner(one_step_recipes()),
            patterns=PATTERNS,
            policy=preview.PreviewPolicy(chunk_items=1),
        )
        self.request(amount=1)
        self.run_to_completion()
        delivery = self.delivery()
        self.assertEqual(
            delivery["state"]["packet_count"], len(delivery["chunks"]),
            "状态头报的片数必须就是实际要发的批数",
        )
        self.assertGreater(len(delivery["chunks"]), 1, "这一份确实被切成多批")

    def test_every_chunk_is_non_empty(self):
        self.service = preview.PreviewService(
            planner=StubPlanner(one_step_recipes()),
            patterns=PATTERNS,
            policy=preview.PreviewPolicy(chunk_items=2),
        )
        self.request(amount=1)
        self.run_to_completion()
        for chunk in self.delivery()["chunks"]:
            self.assertTrue(chunk, "空批会让客户端显示一份空白原料表")

    def test_too_many_rows_are_truncated_and_block_submit(self):
        self.service = preview.PreviewService(
            planner=StubPlanner(one_step_recipes()),
            patterns=PATTERNS,
            policy=preview.PreviewPolicy(max_rows=1),
        )
        self.request(amount=1)
        self.run_to_completion()
        state = self.delivery()["state"]
        self.assertTrue(state["truncated"])
        self.assertFalse(state["submit_ok"], "算不全就不许下单")
        self.assertEqual(state["row_count"], 1)
        self.assertIn(u"行", state["detail"])


# -- 提交闸 ----------------------------------------------------------------


class TestConsume(PreviewCase):
    """提交闸。**夹具的料是够的**——闸门本身要与「缺料」那条路分开测，
    否则「被拒」到底是被闸门拒的还是被缺料拒的，从断言上看不出来。"""

    def setUp(self):
        PreviewCase.setUp(self)
        self.service = self.stocked()

    def open_preview(self, amount=1):
        self.request(amount=amount)
        self.run_to_completion()
        return self.solo().preview_id

    def test_a_valid_preview_is_consumed_once(self):
        preview_id = self.open_preview()
        source = self.command("x").source
        target, amount, reasons = self.service.consume(
            preview_id, source, BLOCK, 1
        )
        self.assertIsNone(reasons)
        self.assertEqual(target.canonical(), BLOCK.canonical())
        self.assertEqual(amount, 1)

    def test_a_preview_that_is_still_running_is_refused_as_unfinished(self):
        """**「还没算完」不是「缺料」。** 两者都拒，但一句话说不清就会把玩家
        引去查材料，而真相只是「等一下」（契约第 6 节的验收表要能分辨）。"""
        preview_id = self.request(amount=1).payload["preview_id"]
        source = self.command("x").source
        _t, _a, reasons = self.service.consume(preview_id, source, BLOCK, 1)
        self.assertEqual([reason.code for reason in reasons],
                         [Reason.STALE_VERSION])
        self.assertIn(u"还没算完", reasons[0].detail)

    def test_the_same_preview_cannot_be_used_twice(self):
        """**换一个 `request_id`、拿同一份预览再下一次**——去重账本管不了
        这一种（它按请求记账，而这两次请求本来就不同）。"""
        preview_id = self.open_preview()
        source = self.command("x").source
        self.assertIsNone(self.service.consume(preview_id, source, BLOCK, 1)[2])
        _t, _a, reasons = self.service.consume(preview_id, source, BLOCK, 1)
        self.assertEqual([reason.code for reason in reasons],
                         [Reason.DUPLICATE_REQUEST])

    def test_another_session_cannot_claim_it(self):
        preview_id = self.open_preview()
        other = self.command("x", session_id="s2").source
        _t, _a, reasons = self.service.consume(preview_id, other, BLOCK, 1)
        self.assertEqual([reason.code for reason in reasons],
                         [Reason.SESSION_INVALID])

    def test_another_player_cannot_claim_it(self):
        preview_id = self.open_preview()
        other = self.command("x", player_id="p2").source
        _t, _a, reasons = self.service.consume(preview_id, other, BLOCK, 1)
        self.assertEqual([reason.code for reason in reasons],
                         [Reason.SESSION_INVALID])

    def test_a_mismatched_target_or_amount_is_refused(self):
        preview_id = self.open_preview()
        source = self.command("x").source
        for target, amount in ((COAL, 1), (BLOCK, 2)):
            _t, _a, reasons = self.service.consume(
                preview_id, source, target, amount
            )
            self.assertEqual([reason.code for reason in reasons],
                             [Reason.BAD_PARAMETER],
                             "%s × %d 与该预览对不上" % (target.canonical(), amount))

    def test_an_unknown_preview_is_refused(self):
        source = self.command("x").source
        _t, _a, reasons = self.service.consume("pv404", source, BLOCK, 1)
        self.assertEqual([reason.code for reason in reasons],
                         [Reason.STALE_VERSION])

    def test_a_preview_that_says_no_is_refused_even_if_it_exists(self):
        """缺料的预览**存在**，但它的结论就是「别下单」。

        **这一条正是「颜色只是表现，服务端必须独立拒绝」**（契约第 4 节
        末句）：界面上按钮禁不禁用是客户端的事，服务端拿一份缺料的预览
        来下单照样要拒。
        """
        self.service = preview.PreviewService(
            planner=StubPlanner(one_step_recipes()),
            patterns=PATTERNS,
            policy=preview.PreviewPolicy(),
        )
        preview_id = self.open_preview()
        source = self.command("x").source
        _t, _a, reasons = self.service.consume(preview_id, source, BLOCK, 1)
        # 这一份是缺料的（库里什么都没有），因此该被拒。
        self.assertIsNotNone(reasons)
        self.assertEqual([reason.code for reason in reasons],
                         [Reason.INSUFFICIENT_STOCK])

    def test_an_expired_preview_is_refused(self):
        """过了存活期就认不回来了。

        **时刻只有一个来源**（`PreviewService._tick`，跟着逻辑帧走）。
        命令那一侧带的 `received_tick` 是**中枢自己的帧号**，两个数相减
        没有任何意义——2026-10-03 上机时正是这一条让每一份预览刚建出来
        就被判过期。
        """
        preview_id = self.open_preview()
        source = self.command("x").source
        # 把逻辑帧推过存活期：回收会先把它清掉，所以这里是「不存在」。
        self.service.advance({"tick": 10 ** 6, "frame": 10 ** 6})
        _t, _a, reasons = self.service.consume(preview_id, source, BLOCK, 1)
        self.assertEqual([reason.code for reason in reasons],
                         [Reason.STALE_VERSION])

    def test_the_lifecycle_clock_never_comes_from_the_command(self):
        """**中枢帧号与逻辑帧不能混用。**

        夹具里命令的 `received_tick` 一律是 0，而 `advance` 喂的是 0 起步的
        逻辑帧。两者若被混着用，`created_tick` 会是一个恒为 0 的数，
        于是每一份预览在第一帧就「过期」——而那正是上机撞出来的那个症状。
        """
        # 先过一帧，让服务知道自己现在是什么时候。
        self.service.advance({"tick": 100, "frame": 0})
        self.request(amount=4)
        job = self.solo()
        self.assertEqual(job.created_tick, 100, "创建时刻取的是逻辑帧")
        self.assertEqual(job.status, Status.RUNNING, "而且它还没过期")
        self.service.advance({"tick": 101, "frame": 1})
        self.assertEqual(self.solo().job.spent(), 1, "照常推进")

    def test_a_successful_preview_can_be_consumed(self):
        preview_id = self.open_preview()
        source = self.command("x").source
        _t, _a, reasons = self.service.consume(preview_id, source, BLOCK, 1)
        self.assertIsNone(reasons)


# -- 生命周期 --------------------------------------------------------------


class TestLifecycle(PreviewCase):
    def test_expiry_drops_the_job(self):
        self.service = preview.PreviewService(
            planner=StubPlanner(one_step_recipes()),
            patterns=PATTERNS,
            policy=preview.PreviewPolicy(ttl_ticks=3),
        )
        self.request()
        self.service.advance({"tick": 10, "frame": 10})
        self.assertEqual(self.service._jobs, {})
        self.assertEqual(self.service.stats["expired"], 1)

    def test_repeating_the_same_request_resumes_instead_of_allocating(self):
        self.request(amount=4)
        first = self.solo()
        self.service.advance({"tick": 0, "frame": 0})
        self.request(amount=4)
        self.assertEqual(len(self.service._jobs), 1, "不新占一份并发额度")
        self.assertIs(self.solo(), first)
        self.assertEqual(self.solo().generation, 2, "换了一代")
        self.assertEqual(self.service.stats["resumed"], 1)

    def test_a_resumed_preview_starts_over(self):
        """复活是**重新算**，不是接着上一份算：依据的那份世界可能已经变了。"""
        self.request(amount=4)
        for index in range(2):
            self.service.advance({"tick": index, "frame": index})
        self.assertGreater(self.solo().job.spent(), 0)
        self.request(amount=4)
        self.assertEqual(self.solo().job.spent(), 0)

    def test_cancelling_removes_it_and_never_touches_orders(self):
        preview_id = self.request().payload["preview_id"]
        result = self.service.cancel(self.command("x"), {"preview_id": preview_id})
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(self.service._jobs, {})
        self.assertEqual(self.service.stats["cancelled"], 1)

    def test_cancelling_an_unknown_preview_is_idempotent(self):
        result = self.service.cancel(self.command("x"), {"preview_id": "pv404"})
        self.assertEqual(result.status, STATUS_OK, "取消已经没了的东西不是错误")

    def test_cancelling_someone_elses_preview_is_refused(self):
        preview_id = self.request().payload["preview_id"]
        result = self.service.cancel(
            self.command("x", session_id="s2"), {"preview_id": preview_id}
        )
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(len(self.service._jobs), 1, "别人的预览动不了")

    def test_losing_the_session_clears_its_previews(self):
        self.request()
        self.assertEqual(self.service.forget_session("s1"), 1)
        self.assertEqual(self.service._jobs, {})
        self.assertIsNone(self.delivery())

    def test_the_session_cap_is_enforced(self):
        self.service = preview.PreviewService(
            planner=StubPlanner(one_step_recipes()),
            patterns=PATTERNS,
            policy=preview.PreviewPolicy(max_per_session=2, max_per_network=8),
        )
        self.request(target=BLOCK, amount=1)
        self.request(target=BLOCK, amount=2)
        result = self.request(target=BLOCK, amount=3)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.reasons[0].code, Reason.NO_CAPACITY)

    def test_the_network_cap_is_enforced(self):
        self.service = preview.PreviewService(
            planner=StubPlanner(one_step_recipes()),
            patterns=PATTERNS,
            policy=preview.PreviewPolicy(max_per_session=8, max_per_network=2),
        )
        self.request(target=BLOCK, amount=1)
        self.request(target=BLOCK, amount=2)
        result = self.request(target=BLOCK, amount=3)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.reasons[0].code, Reason.NO_CAPACITY)

    def test_a_bad_amount_is_refused_before_anything_is_allocated(self):
        self.assertEqual(
            self.request(amount=0).status, STATUS_REJECTED
        )
        self.assertEqual(self.service._jobs, {})
        self.assertEqual(self.request(amount=-1).status, STATUS_REJECTED)
        self.assertEqual(self.service._jobs, {})

    def test_an_unwired_service_says_so_instead_of_pretending(self):
        """**没有规划器就不是「没有缺料」**——那是另一个结论，
        而且正好是会让玩家点下「下单」的那一个。"""
        service = preview.PreviewService()
        result = service.request(
            self.command("x"), {"target": BLOCK.to_dict(), "amount": 1}
        )
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertEqual(result.reasons[0].code, Reason.SERVICE_NOT_READY)

    def test_the_policy_reports_its_unsettled_numbers(self):
        """这几条本次没有 User 口径，**必须带未定标签**（契约第 6 节）。"""
        report = preview.PreviewPolicy().unresolved_report()
        self.assertTrue(report)
        with self.assertRaises(AssertionError):
            preview.PreviewPolicy().assert_settled()

    def test_the_policy_refuses_a_zero_cap(self):
        for name in ("ttl_ticks", "max_per_session", "max_per_network",
                     "chunk_items", "max_rows"):
            with self.assertRaises(ValueError):
                preview.PreviewPolicy(**{name: 0})


if __name__ == "__main__":
    unittest.main()
