# coding=utf-8
"""合成规划服务（任务 08）。

**全部离线**：规划器不认识引擎，两份输入都是纯数据快照。因此这里的夹具直接
构造 `PatternSnapshot` / `InventorySnapshot`；只有「提交前复核」那一族要接
**真的** `PatternStore`（它才拿得出 `check_dispatch`），容器用
`containers.FakeContainer` 替身。

这一套用例守的是 08 文档那五条验收，逐条对应：

1. 铁镐这类多级示例能得出正确批次与缺料清单，且**同一份库存材料不会被两个
   分支各用一次**（`TestIronPickaxe` / `TestSharedSubDemand`）。
2. 批次取整多出来的量被保留，多产物的产物被别的分支用掉时数量守恒
   （`TestBatchesAndSurplus`）。
3. 环与死路能在预算内停下并把原因分开报（`TestCyclesAndDeadEnds`）。
4. 一次运行与分帧运行逐字节一致，取消不改变任何真实库存
   （`TestDeterminismAndCancel`）。
5. 规划之后的库存/拓扑/样板变化能被提交方识别（`TestRevalidation`）。

另外两处容易被当成「细节」的东西在这里被钉死，因为它们是这一层最容易悄悄破的：

- **确定性**：`step(1)` 调 n 次与 `step(n)` 调一次必须给出同一份计划。破它的
  头号原因是快照里留了活的 `PatternProviderState`（`test_a_frozen_snapshot...`）。
- **不谎报**：一条深链上的死路不能让祖先步骤「看起来有货」
  （`test_a_dead_end_does_not_make_its_ancestors_look_covered`）。
"""

import json
import random
import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import DefinitionSet
from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.protocol import Reason
from endernet.common.resource_key import ResourceKey
from endernet.server.adapters.containers import FakeContainer
from endernet.server.crafting import patterns, planner
from endernet.server.crafting.store import PatternStore
from endernet.server.storage.service import StorageService

TEMPLATE_DEVICE = "endernet:bus_template_io"

#: 计划里的状态与原因码用得多，取个短名字，免得每一行都写一长串。
Status = planner.PlanStatus
Cause = planner.GapCause
Targets = planner.TargetSemantics
Candidates = planner.CandidateOrder
Alternatives = planner.AlternativeRecipes


# -- 夹具 ------------------------------------------------------------------


def item(name, count=1, unlimited=False):
    return patterns.PatternStack(ResourceKey.item(name), count, None, unlimited)


def recipe(inputs, outputs, returns=()):
    return patterns.PatternDefinition(patterns.MODE_FACTORY, inputs, outputs, returns)


def candidate(definition, **kwargs):
    """造一席候选。默认可用、位置固定，便于断言三元组。"""
    return planner.PatternCandidate(
        kwargs.get("ordinal", 0),
        kwargs.get("node_key", (0, 10, 64, 0)),
        kwargs.get("generation", 1),
        kwargs.get("device_id", TEMPLATE_DEVICE),
        kwargs.get("slot", 0),
        definition.digest(),
        definition.mode,
        definition,
        kwargs.get("available", True),
        kwargs.get("blocker"),
    )


def snapshot(recipes, unavailable=(), extra=()):
    """`{键: 配方}` -> 样板快照。`unavailable` 里的键的候选全部标成不可用。"""
    index = {}
    for key, definition in recipes.items():
        index[key] = (candidate(definition, available=key not in unavailable),)
    for key, rows in extra:
        index[key] = tuple(rows)
    return planner.PatternSnapshot(index)


def inventory(totals=None):
    """`{键: 数量}` 或 `{键: (stored, reserved, available)}` -> 库存快照。"""
    rows = {}
    for key, value in (totals or {}).items():
        if isinstance(value, tuple):
            rows[key] = value
        else:
            rows[key] = (value, 0, value)
    return planner.InventorySnapshot(rows)


def solve(target, amount, recipes, totals=None, policy=None, pattern_policy=None,
          unavailable=(), patterns_snapshot=None):
    """一次性算完，返回计划。**默认预算用不完**，只有专门的用例才分帧。"""
    job = planner.plan(
        target,
        amount,
        inventory(totals),
        patterns_snapshot if patterns_snapshot is not None else snapshot(recipes, unavailable),
        policy=policy,
        pattern_policy=pattern_policy,
    )
    job.step(10 ** 9)
    return job.result()


# -- 标准夹具：铁镐的多级依赖树 --------------------------------------------
#
#     铁镐 ← 3 铁锭 + 2 木棍
#     铁锭 ← 1 粗铁 + 1 煤（煤标记「不限量」，熔炉预设的形态）
#     木棍 ← 2 木板（产 4）
#     木板 ← 1 原木（产 4）
#
# 粗铁与原木在最底下：没有配方、只有「库存里有多少」。
# 四个例子同时也是 `plans/concepts.md` 那四条表达力的下游消费方。

PICKAXE = ResourceKey.item("minecraft:iron_pickaxe")
INGOT = ResourceKey.item("minecraft:iron_ingot")
STICK = ResourceKey.item("minecraft:stick")
PLANK = ResourceKey.item("minecraft:planks")
LOG = ResourceKey.item("minecraft:log")
RAW_IRON = ResourceKey.item("minecraft:raw_iron")
COAL = ResourceKey.item("minecraft:coal")


def pickaxe_tree():
    return {
        PICKAXE: recipe(
            [item("minecraft:iron_ingot", 3), item("minecraft:stick", 2)],
            [item("minecraft:iron_pickaxe")],
        ),
        INGOT: recipe(
            [item("minecraft:raw_iron"), item("minecraft:coal", 1, unlimited=True)],
            [item("minecraft:iron_ingot")],
        ),
        STICK: recipe([item("minecraft:planks", 2)], [item("minecraft:stick", 4)]),
        PLANK: recipe([item("minecraft:log")], [item("minecraft:planks", 4)]),
    }


def step_of(plan, key):
    """按资源键取步骤（计划里的步骤带 `canonical`，与键一一对应）。"""
    for step in plan.steps:
        if step["canonical"] == key.canonical():
            return step
    return None


def row_of(plan, key):
    for row in plan.materials:
        if row["canonical"] == key.canonical():
            return row
    return None


def gap_of(plan, key):
    for gap in plan.gaps:
        if gap["canonical"] == key.canonical():
            return gap
    return None


def blocked_of(plan, key):
    for entry in plan.blocked_keys:
        if entry["canonical"] == key.canonical():
            return entry
    return None


def dump(plan):
    """计划的规范化文本。**确定性的判据就是它逐字节相同。**"""
    return json.dumps(
        plan.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def assert_topological(case, plan):
    """断言计划的步骤顺序是合法拓扑序：**每条依赖都排在它的消费者之前**。

    判据取自步骤自己报出的输入（`inputs[].canonical`）与步骤顺序，
    因此它验的是「计划说它要什么」与「计划说按什么顺序做」是否自洽——
    而不是某一份具体顺序。依赖不在步骤表里（由库存或外部提供）时跳过。
    """
    index = {}
    for step in plan.steps:
        index[step["canonical"]] = step["index"]
    for step in plan.steps:
        for entry in step["inputs"]:
            child = entry["canonical"]
            if child in index:
                case.assertLess(
                    index[child],
                    step["index"],
                    u"%s 排在它的消费者 %s 之后（依赖必须先行）"
                    % (entry["key"], step["key"]),
                )


# -- 验收 1：多级示例的批次与缺料 ------------------------------------------


class TestIronPickaxe(unittest.TestCase):
    def setUp(self):
        self.plan = solve(PICKAXE, 1, pickaxe_tree(), {RAW_IRON: 2})

    def test_batches_are_exact(self):
        """3 个铁锭、1 批木棍（产 4）、1 批木板（产 4）、1 个铁镐。"""
        self.assertEqual(step_of(self.plan, PICKAXE)["batches"], 1)
        self.assertEqual(step_of(self.plan, INGOT)["batches"], 3)
        self.assertEqual(step_of(self.plan, STICK)["batches"], 1)
        self.assertEqual(step_of(self.plan, PLANK)["batches"], 1)
        self.assertEqual(len(self.plan.steps), 4)

    def test_execution_order_is_a_valid_topological_order(self):
        """原料先做、消费者后做：**每一条依赖都排在它的消费者之前**。

        判据写成「按边验证」而不是钉死一份顺序：顺序会随排序键的实现变化，
        而「依赖在前」这条性质是不许变的。两者混为一谈的代价是——
        哪天顺序变了，测试跟着改一行就过去了，而 09 会照着错序派发。
        """
        assert_topological(self, self.plan)
        self.assertEqual(self.plan.steps[-1]["key"], "minecraft:iron_pickaxe")

    def test_gaps_are_the_two_missing_base_materials(self):
        self.assertEqual(self.plan.status, Status.MISSING_MATERIALS)
        self.assertEqual(gap_of(self.plan, LOG)["shortfall"], 1)
        self.assertEqual(gap_of(self.plan, RAW_IRON)["shortfall"], 1)
        self.assertEqual(self.plan.summary()["gaps"], {Cause.NO_RECIPE: 2})

    def test_stock_is_used_before_crafting(self):
        """库里有 2 个粗铁：只用库存的那 2 个，缺 1 个。"""
        row = row_of(self.plan, RAW_IRON)
        self.assertEqual(row["demand"], 3)
        self.assertEqual(row["from_stock"], 2)
        self.assertEqual(row["outstanding"], 1)

    def test_the_three_numbers_share_one_convention(self):
        """08-01：`requested`/`held`/`produced` 一起给，09 与 11 读同一口径。"""
        summary = self.plan.summary()
        self.assertEqual(summary["requested"], 1)
        self.assertEqual(summary["held"], 0)
        self.assertEqual(summary["produced"], 1)
        self.assertEqual(self.plan.to_dict()["semantics"], Targets.HOLD)

    def test_unlimited_input_is_reported_but_not_demanded(self):
        """R09 替身口径：煤标了不限量，于是不进需求，只报成「外部自理」。"""
        step = step_of(self.plan, INGOT)
        self.assertEqual(
            [(entry["key"], entry["total"]) for entry in step["external_inputs"]],
            [("minecraft:coal", 3)],
        )
        self.assertIsNone(row_of(self.plan, COAL))
        self.assertIsNone(gap_of(self.plan, COAL))

    def test_unlimited_input_is_demanded_in_the_other_convention(self):
        """另一侧：`MODELING_ONLY` 下它与普通输入一样要备料。"""
        plan = solve(
            PICKAXE, 1, pickaxe_tree(), {RAW_IRON: 2},
            pattern_policy=patterns.PatternPolicy(
                unlimited_fuel=patterns.UnlimitedFuel.MODELING_ONLY
            ),
        )
        self.assertEqual(row_of(plan, COAL)["demand"], 3)
        self.assertEqual(gap_of(plan, COAL)["cause"], Cause.NO_RECIPE)
        self.assertEqual(step_of(plan, INGOT)["external_inputs"], [])

    def test_the_plan_is_plain_data(self):
        """08-07：不向客户端暴露引擎对象——整份计划必须能直接序列化。"""
        text = json.dumps(self.plan.to_dict(), ensure_ascii=False, sort_keys=True)
        self.assertIn("minecraft:iron_pickaxe", text)


class TestDuplicateInputs(unittest.TestCase):
    """同一种原料在一条配方里写成**多条**时，需求必须按合计算（不是按最后一条）。

    这不是边角情形：**合成样板的有序摆位必然这样写**——`patterns._check_layout`
    要求有序输入每条占一个槽位、且槽位不重复，于是「3 个铁锭」就是三条输入。
    工厂样板把同一种料写两遍同样是合法样板。
    逐条记需求时，第二条算出的增量是负的（第一条已经把那部分记进去了），
    账本只留下一条的量：`steps[].inputs` 说要 3 个、`materials[].demand` 写 1 个，
    计划照旧报 `COMPLETE`——09 按 `claims` 预留就会少扣。
    """

    def setUp(self):
        # 与铁镐那棵树同样的形状，但铁镐那份配方按**真实的有序写法**给出。
        self.recipes = {
            PICKAXE: patterns.PatternDefinition(
                patterns.MODE_CRAFTING,
                [
                    patterns.PatternStack(INGOT, 1, 0),
                    patterns.PatternStack(INGOT, 1, 1),
                    patterns.PatternStack(INGOT, 1, 2),
                    patterns.PatternStack(STICK, 1, 4),
                    patterns.PatternStack(STICK, 1, 7),
                ],
                [patterns.PatternStack(PICKAXE, 1)],
            ),
            INGOT: recipe([item("minecraft:raw_iron")], [item("minecraft:iron_ingot")]),
            STICK: recipe([item("minecraft:planks", 2)], [item("minecraft:stick", 4)]),
            PLANK: recipe([item("minecraft:log")], [item("minecraft:planks", 4)]),
        }
        self.plan = solve(PICKAXE, 1, self.recipes, {RAW_IRON: 2})

    def test_the_step_reports_the_summed_requirement(self):
        step = step_of(self.plan, PICKAXE)
        self.assertEqual(
            [(entry["key"], entry["per_batch"], entry["total"]) for entry in step["inputs"]],
            [("minecraft:iron_ingot", 3, 3), ("minecraft:stick", 2, 2)],
        )

    def test_the_ledger_records_the_same_numbers(self):
        """步骤说 3 个，材料清单就必须说 3 个——两个数分叉就是这一条的核心症状。"""
        self.assertEqual(row_of(self.plan, INGOT)["demand"], 3)
        self.assertEqual(row_of(self.plan, STICK)["demand"], 2)
        # 库存 2 个粗铁只够 2 个铁锭，第 3 个要报缺料。
        self.assertEqual(row_of(self.plan, RAW_IRON)["demand"], 3)
        self.assertEqual(row_of(self.plan, RAW_IRON)["from_stock"], 2)
        self.assertEqual(row_of(self.plan, RAW_IRON)["outstanding"], 1)

    def test_a_short_stock_is_not_reported_as_complete(self):
        """**回归的核心**：原料不足时绝不能报「能做完」。

        逐条记需求的实现会在这里返回 `complete`——它只记下了 1 个铁锭的需求，
        而库里的 2 个粗铁看起来绰绰有余。
        """
        self.assertEqual(self.plan.status, Status.MISSING_MATERIALS)
        self.assertNotEqual(self.plan.status, Status.COMPLETE)

    def test_a_sufficient_stock_is_complete_and_claims_everything(self):
        plan = solve(PICKAXE, 1, self.recipes, {RAW_IRON: 3, LOG: 1})
        self.assertEqual(plan.status, Status.COMPLETE)
        self.assertEqual(
            [(claim["key"], claim["count"]) for claim in plan.seal["claims"]],
            [("minecraft:log", 1), ("minecraft:raw_iron", 3)],
        )
        # 每一条被声明过的料都要能通过复核。
        report = plan.revalidate(
            inventory_snapshot=inventory({RAW_IRON: 3, LOG: 1}), topology_version=None
        )
        self.assertEqual(report.stale_stock, ())

    def test_the_steps_agree_with_the_materials_list(self):
        """每个键的**总需求**必须至少覆盖各步骤向它要的量之和。

        这是「同一份计划里的两个数不能互相矛盾」那条要求的直接判据：
        少记一次（比如漏掉重复输入的第二条）就会在这里露出来。
        """
        wanted = {}
        for step in self.plan.steps:
            for entry in step["inputs"]:
                wanted[entry["canonical"]] = wanted.get(entry["canonical"], 0) + entry["total"]
        for canonical, total in wanted.items():
            row = [item for item in self.plan.materials if item["canonical"] == canonical][0]
            self.assertGreaterEqual(row["demand"], total, canonical)


class TestSharedSubDemand(unittest.TestCase):
    """同一份库存材料不能被两个分支各用一次（08 验收第 1 条）。"""

    A = ResourceKey.item("t:a")
    B = ResourceKey.item("t:b")
    C = ResourceKey.item("t:c")
    D = ResourceKey.item("t:d")
    E = ResourceKey.item("t:e")

    def setUp(self):
        self.recipes = {
            self.A: recipe([item("t:b"), item("t:c")], [item("t:a")]),
            self.B: recipe([item("t:d", 2)], [item("t:b")]),
            self.C: recipe([item("t:d", 3)], [item("t:c")]),
            self.D: recipe([item("t:e")], [item("t:d")]),
        }

    def test_the_stock_pool_is_shared_not_duplicated(self):
        """两个分支要 2+3 个 D，库里有 2 个：**那 2 个只能用一次**。

        分支各自算账的实现会得出 5 个需求、用掉 4 个库存（2+2），
        而库里根本没有 4 个——这是最典型的一类重复承诺。
        """
        plan = solve(self.A, 1, self.recipes, {self.D: 2})
        row = row_of(plan, self.D)
        self.assertEqual(row["demand"], 5)
        self.assertEqual(row["from_stock"], 2)
        self.assertEqual(row["own_output"], 3)
        self.assertEqual(row["outstanding"], 0)

    def test_the_shared_key_is_crafted_exactly_once(self):
        plan = solve(self.A, 1, self.recipes, {self.D: 2})
        steps = [step for step in plan.steps if step["canonical"] == self.D.canonical()]
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["batches"], 3)

    def test_demand_propagates_through_the_shared_key(self):
        plan = solve(self.A, 1, self.recipes, {self.D: 2})
        self.assertEqual(row_of(plan, self.E)["demand"], 3)

    def test_a_branch_that_fits_is_not_blocked(self):
        """B 只要 2 个 D，库存那 2 个刚好够，因此 B 是可执行的；
        C 要 3 个而库存只剩 0 个（那 2 个已经归 B 了），因此 C 断料。
        **这条正是「库存只能被用一次」的行为面证据。**"""
        plan = solve(self.A, 1, self.recipes, {self.D: 2})
        self.assertIsNone(blocked_of(plan, self.B))
        self.assertEqual(blocked_of(plan, self.C)["cause"], Cause.MISSING_INPUTS)

    def test_without_the_shared_stock_both_branches_craft(self):
        plan = solve(self.A, 1, self.recipes, {})
        self.assertEqual(step_of(plan, self.D)["batches"], 5)
        self.assertEqual(row_of(plan, self.E)["demand"], 5)


# -- 验收 2：余量与多产物守恒 ----------------------------------------------


class TestExecutionOrder(unittest.TestCase):
    """步骤顺序必须是**合法拓扑序**：依赖先做，消费者后做。

    判据不是某一份具体顺序，而是「按边验证」——顺序会随排序键实现变化，
    而这条性质不许变。写成钉死一份顺序的用例的代价是：哪天顺序错了，
    测试跟着改一行就过去了，而 09 会照着错序派发。
    """

    T = ResourceKey.item("t:target")
    X = ResourceKey.item("t:x")
    Y = ResourceKey.item("t:y")

    def _shared(self):
        """`T ← X + Y`、`X ← Y`、`Y ← U`：Y 既是 T 的直接输入、又是 X 的依赖。

        Y 必须**有步骤**（不能由库存满足），否则它不进步骤表，也就谈不上排序。
        """
        return {
            self.T: recipe([item("t:x"), item("t:y")], [item("t:target")]),
            self.X: recipe([item("t:y")], [item("t:x")]),
            self.Y: recipe([item("t:u")], [item("t:y")]),
        }

    def test_a_shared_dependency_is_scheduled_before_its_consumer(self):
        """Y 既是 T 的直接输入、又是 X 的依赖，因此必须排在 X 之前。

        按「被见到的最浅深度」排序时，Y 的深度是 1（T 直接要它），与 X 同层，
        于是只能按资源身份打破并列——而 `t:x` 排在 `t:y` 前面，正好把 X 排在
        它的依赖之前。这条用例就是那个形状。
        """
        plan = solve(self.T, 1, self._shared())
        assert_topological(self, plan)
        index = dict((step["canonical"], step["index"]) for step in plan.steps)
        self.assertLess(index[self.Y.canonical()], index[self.X.canonical()])
        self.assertLess(index[self.X.canonical()], index[self.T.canonical()])

    def test_the_tree_example_is_topological(self):
        plan = solve(PICKAXE, 1, pickaxe_tree(), {RAW_IRON: 5})
        assert_topological(self, plan)

    def test_every_edge_goes_from_a_lower_rank_to_a_higher_one(self):
        """层级本身的性质：**有步骤的**依赖的 rank 严格小于消费者的 rank。

        （由库存或外部满足的依赖没有步骤，也就不参与排序。）
        """
        job = planner.plan(self.T, 1, inventory({}), snapshot(self._shared()))
        job.step(10 ** 6)
        ranks = job.execution_ranks()
        checked = 0
        for parent, child, _count in job.edges():
            if child not in ranks:
                continue
            checked += 1
            self.assertLess(ranks[child], ranks[parent])
        self.assertGreater(checked, 0)


class TestBatchesAndSurplus(unittest.TestCase):
    STICK = ResourceKey.item("t:stick")
    PLANK = ResourceKey.item("t:plank")
    U = ResourceKey.item("t:u")
    P = ResourceKey.item("t:p")
    B = ResourceKey.item("t:b")
    T = ResourceKey.item("t:t")
    A = ResourceKey.item("t:a")
    Z = ResourceKey.item("t:z")
    Q = ResourceKey.item("t:q")
    DUP = ResourceKey.item("t:dup")
    R = ResourceKey.item("t:r")

    def test_overshoot_is_kept_as_surplus(self):
        """要 2 个木棍，一批产 4 个：多的 2 个留在 surplus 里，不是凭空消失。"""
        recipes = {
            self.STICK: recipe([item("t:plank", 2)], [item("t:stick", 4)]),
        }
        plan = solve(self.STICK, 2, recipes, {patterns.PatternStack(ResourceKey.item("t:plank"), 2).key: 2})
        row = row_of(plan, self.STICK)
        self.assertEqual(row["own_output"], 4)
        self.assertEqual(row["surplus"], 2)
        self.assertEqual(row["outstanding"], 0)

    def test_duplicate_output_rows_are_summed(self):
        """`outputs` 里同一键出现两行是合法样板：每批 2+3=5 个，不是只算 2 个。"""
        recipes = {self.DUP: recipe([item("t:u")], [item("t:dup", 2), item("t:dup", 3)])}
        plan = solve(self.DUP, 7, recipes, {self.U: 2})
        self.assertEqual(step_of(plan, self.DUP)["batches"], 2)
        self.assertEqual(row_of(plan, self.DUP)["own_output"], 10)
        self.assertEqual(row_of(plan, self.DUP)["surplus"], 3)

    def test_a_byproduct_can_satisfy_a_demand_before_it_is_planned(self):
        """b 的副产物里有 p。b 先被展开（资源身份排序），于是 p 不必再单独做。

        这是**顺序相关**的、且是有意为之：处理顺序由「先浅后深、同深度按资源
        身份」唯一确定，因此结论可复现。
        """
        recipes = {
            self.T: recipe([item("t:p"), item("t:b")], [item("t:t")]),
            self.B: recipe([item("t:u", 4)], [item("t:b"), item("t:p", 2)]),
            self.P: recipe([item("t:q")], [item("t:p")]),
            self.Q: recipe([item("t:u")], [item("t:q")]),
        }
        plan = solve(self.T, 1, recipes, {self.U: 8})
        self.assertIsNone(step_of(plan, self.P))
        row = row_of(plan, self.P)
        self.assertEqual(row["byproduct_output"], 2)
        self.assertEqual(row["surplus"], 1)
        self.assertEqual(row["demand"], 1)
        # q 从未被需要：p 没有步骤，q 自然没有需求。
        self.assertIsNone(row_of(plan, self.Q))
        self.assertEqual(row_of(plan, self.U)["demand"], 4)
        self.assertEqual(plan.status, Status.COMPLETE)

    def test_a_byproduct_arriving_after_the_step_becomes_surplus(self):
        """同一个副产物**后**到账时，那一步已经安排好了，于是它变成余量。

        两件事都成立、结果不同，因此这一对用例把顺序钉死——否则「副产物能不能
        省一步」会随机表现出来，而那是没法排查的。
        """
        recipes = {
            self.T: recipe([item("t:a"), item("t:z")], [item("t:t")]),
            self.A: recipe([item("t:q")], [item("t:a")]),
            self.Z: recipe([item("t:u", 4)], [item("t:z"), item("t:a", 2)]),
            self.Q: recipe([item("t:u")], [item("t:q")]),
        }
        plan = solve(self.T, 1, recipes, {self.U: 8})
        self.assertIsNotNone(step_of(plan, self.A))
        row = row_of(plan, self.A)
        self.assertEqual(row["own_output"], 1)
        self.assertEqual(row["byproduct_output"], 2)
        self.assertEqual(row["surplus"], 2)
        self.assertEqual(plan.status, Status.COMPLETE)

    def test_returns_are_reported_but_never_credited(self):
        """返还物（空桶）只报给 10 的完成判定，**不进账本**。

        记账会让计划假定一份 10 明确说「可能不到」的回流，而据此少算一个
        要生产的数量时，玩家会得到一份永远做不完的计划。
        """
        IN = ResourceKey.item("t:in")
        BUCKET = ResourceKey.item("t:bucket")
        recipes = {self.R: recipe([item("t:in")], [item("t:r")], returns=(item("t:bucket"),))}
        plan = solve(self.R, 2, recipes, {IN: 2})
        step = step_of(plan, self.R)
        self.assertEqual(
            [(entry["key"], entry["total"]) for entry in step["returns"]],
            [("t:bucket", 2)],
        )
        self.assertIsNone(row_of(plan, BUCKET))
        self.assertEqual(plan.status, Status.COMPLETE)


# -- 验收 3：环与死路 ------------------------------------------------------


class TestCyclesAndDeadEnds(unittest.TestCase):
    A = ResourceKey.item("t:a")
    B = ResourceKey.item("t:b")
    T = ResourceKey.item("t:t")
    SELF = ResourceKey.item("t:self")

    def test_a_simple_cycle_is_reported_with_its_path(self):
        recipes = {
            self.A: recipe([item("t:b")], [item("t:a")]),
            self.B: recipe([item("t:a")], [item("t:b")]),
        }
        plan = solve(self.A, 1, recipes)
        self.assertEqual(plan.status, Status.CYCLE)
        gap = gap_of(plan, self.A)
        self.assertEqual(gap["cause"], Cause.CYCLE)
        # 环路**从被拒绝的那条输入开始写**，因此它的首尾是同一个键：
        # 读起来就是「A 要去 B，B 又回到 A」。顺序是确定的（DFS 按身份排序），
        # 因此这条断言不会今天过一个明天挂。
        self.assertIn(u"t:a -> t:b -> t:a", gap["detail"])

    def test_a_cycle_through_a_sibling_is_still_caught(self):
        """目标同时要 A 和 B，而 A、B 互为配方。

        **这条用例是环检测必须建在边图上的理由。** 按「祖先路径」判环的实现
        在这里看不到 `A→B` 这条已经存在的边（B 的路径是 `T,B`），于是两条边
        互相长大，只能等上限截断——报出来是「超限」而不是「循环」。
        """
        recipes = {
            self.T: recipe([item("t:a"), item("t:b")], [item("t:t")]),
            self.A: recipe([item("t:b")], [item("t:a")]),
            self.B: recipe([item("t:a")], [item("t:b")]),
        }
        plan = solve(self.T, 1, recipes)
        self.assertEqual(plan.status, Status.CYCLE)
        self.assertEqual(plan.limits["truncated"], False)
        self.assertLessEqual(len(plan.steps), 3)

    def test_a_self_referencing_recipe_is_a_cycle(self):
        """`A ← A + B`（催化剂/回收）。首版不支持循环生产，因此明确拒绝。"""
        recipes = {self.SELF: recipe([item("t:self"), item("t:u")], [item("t:self")])}
        plan = solve(self.SELF, 1, recipes)
        self.assertEqual(plan.status, Status.CYCLE)
        self.assertEqual(gap_of(plan, self.SELF)["cause"], Cause.CYCLE)

    def test_a_cycle_deep_in_the_tree_still_terminates(self):
        """A ← B ← C ← A。目标 A 的一切都在环上。"""
        C = ResourceKey.item("t:c")
        recipes = {
            self.A: recipe([item("t:b")], [item("t:a")]),
            self.B: recipe([item("t:c")], [item("t:b")]),
            C: recipe([item("t:a")], [item("t:c")]),
        }
        job = planner.plan(self.A, 1, inventory({}), snapshot(recipes))
        while job.status() == Status.RUNNING:
            job.step(1)
        self.assertEqual(job.status(), Status.CYCLE)
        self.assertLess(job.spent(), 1000)

    def test_a_dead_end_does_not_make_its_ancestors_look_covered(self):
        """一条深链断在底部时，**祖先步骤不能看起来有货**。

        没有收尾的可达量传播的话，每个祖先的 `own_output` 都照记，于是「铁镐」
        看起来已经规划好了、而真正缺的原木只在最底下报一句——玩家会以为
        这份计划能执行。
        """
        X = ResourceKey.item("t:x")
        Y = ResourceKey.item("t:y")
        Z = ResourceKey.item("t:z")
        recipes = {
            self.T: recipe([item("t:x")], [item("t:t")]),
            X: recipe([item("t:y")], [item("t:x")]),
            Y: recipe([item("t:z")], [item("t:y")]),
        }
        plan = solve(self.T, 1, recipes)
        self.assertEqual(plan.status, Status.MISSING_MATERIALS)
        # 断点自己有原因
        self.assertEqual(gap_of(plan, Z)["cause"], Cause.NO_RECIPE)
        # 整条链都被标成执行不了，且原因是「上游缺料」而不是「没有配方」
        for key in (self.T, X, Y):
            entry = blocked_of(plan, key)
            self.assertIsNotNone(entry, "%s 应当被标成执行不了" % (key.name,))
            self.assertEqual(entry["cause"], Cause.MISSING_INPUTS)
            self.assertTrue(entry["inherited"])
            self.assertIn("t:z", entry["detail"])

    def test_a_satisfied_chain_is_not_blocked(self):
        """同样的链，最底下有货：没有任何键被标成执行不了。"""
        X = ResourceKey.item("t:x")
        Y = ResourceKey.item("t:y")
        Z = ResourceKey.item("t:z")
        recipes = {
            self.T: recipe([item("t:x")], [item("t:t")]),
            X: recipe([item("t:y")], [item("t:x")]),
            Y: recipe([item("t:z")], [item("t:y")]),
        }
        plan = solve(self.T, 1, recipes, {Z: 1})
        self.assertEqual(plan.status, Status.COMPLETE)
        self.assertEqual(plan.blocked_keys, [])

    def test_a_target_without_any_recipe_is_no_recipe(self):
        """「做不出来」与「料不够」必须分开报。"""
        plan = solve(ResourceKey.item("t:unknown"), 1, {})
        self.assertEqual(plan.status, Status.NO_RECIPE)
        self.assertEqual(gap_of(plan, ResourceKey.item("t:unknown"))["cause"],
                         Cause.NO_RECIPE)

    def test_a_mutually_dependent_pair_is_not_reported_as_complete(self):
        """`A ← B` 且 `B ← A`、两者都没有库存：**必须报环，不能报「做完了」**。

        可达量的不动点如果从「全都能跑」往下减，就会得出「A 的 B 由 B 提供、
        B 的 A 由 A 提供」这种互相作保的结论，于是环在缺口被清掉之后
        伪装成一份能做完的计划。方向必须是从零往上加。
        """
        recipes = {
            self.A: recipe([item("t:b")], [item("t:a")]),
            self.B: recipe([item("t:a")], [item("t:b")]),
        }
        plan = solve(self.A, 1, recipes)
        self.assertEqual(plan.status, Status.CYCLE)
        self.assertNotEqual(plan.status, Status.COMPLETE)
        self.assertTrue(plan.blocked_keys)

    def test_a_refused_edge_does_not_inflate_demand(self):
        """被环拒绝的那条边**不记需求**，因此重走时也不会重复累加。

        记需求会让那个键的需求凭空膨胀（每重走一遍加一次），而膨胀出来的
        需求谁也满足不了——报告里就成了一个查不出原因的缺口。
        """
        job = planner.plan(
            self.A, 1, inventory({}),
            snapshot({
                self.A: recipe([item("t:b")], [item("t:a")]),
                self.B: recipe([item("t:a")], [item("t:b")]),
            }),
        )
        job.step(10 ** 6)
        # 需求只来自根与那条被接受的边：各 1，一共 2。
        self.assertEqual(job.ledger()[0].demand + job.ledger()[1].demand, 2)
        for _parent, _child, count in job.refused_edges():
            # 被拒绝的毛量只报一次（B 每批要 1 个 A、共 1 批）。
            self.assertEqual(count, 1)

    def test_a_cycle_gap_survives_when_the_chain_is_unrunnable(self):
        """环上的键**必须留住缺口**，否则「执行不了」这件事就没有出处。"""
        plan = solve(self.A, 1, {
            self.A: recipe([item("t:b")], [item("t:a")]),
            self.B: recipe([item("t:a")], [item("t:b")]),
        })
        gap = gap_of(plan, self.A)
        self.assertIsNotNone(gap)
        self.assertEqual(gap["cause"], Cause.CYCLE)
        self.assertEqual(gap["shortfall"], blocked_of(plan, self.A)["shortfall"])

    def _chain(self):
        """`T <- X <- Y <- Z`，而 Z 没有配方：整条链都执行不了。"""
        self.X = ResourceKey.item("t:x")
        self.Y = ResourceKey.item("t:y")
        self.Z = ResourceKey.item("t:z")
        return {
            self.T: recipe([item("t:x")], [item("t:t")]),
            self.X: recipe([item("t:y")], [item("t:x")]),
            self.Y: recipe([item("t:z")], [item("t:y")]),
        }

    def test_gaps_and_blocked_keys_agree_on_the_numbers(self):
        """同一个键在 `gaps` 与 `blocked_keys` 里必须是**同一个数**。

        两个字段用两把尺子（账本池子 vs 可达量）时，读的人只能猜哪个对；
        而 09 会据此决定要不要提交。
        """
        plan = solve(self.T, 1, self._chain())
        self.assertTrue(plan.gaps)
        for gap in plan.gaps:
            entry = [item for item in plan.blocked_keys
                     if item["canonical"] == gap["canonical"]]
            self.assertEqual(len(entry), 1, gap["key"])
            self.assertEqual(entry[0]["shortfall"], gap["shortfall"], gap["key"])

    def test_an_upstream_cause_is_named_not_just_guessed(self):
        """被上游连累的键，说明里要点名**是谁断了**、为什么。"""
        plan = solve(self.T, 1, self._chain())
        entry = blocked_of(plan, self.X)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["cause"], Cause.MISSING_INPUTS)
        self.assertIn("t:z", entry["detail"])
        self.assertNotIn("缺口记录已不在", entry["detail"])

    def test_a_cycle_upstream_is_followed_through_the_refused_edge(self):
        """断点在环上时，上游原因要顺着**被拒绝的边**找得到。

        被拒绝的边不进依赖图，只走依赖边的话这里会找不到任何上游缺口，
        只剩一句「缺口记录已不在」——而玩家要做的是去看那个成环的键。
        """
        plan = solve(self.A, 1, {
            self.A: recipe([item("t:b")], [item("t:a")]),
            self.B: recipe([item("t:a")], [item("t:b")]),
        })
        entry = blocked_of(plan, self.B)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["cause"], Cause.MISSING_INPUTS)
        self.assertIn("cycle", entry["detail"])
        self.assertIn("t:a", entry["detail"])

    def test_a_missing_base_material_is_not_reported_as_no_recipe(self):
        recipes = {self.A: recipe([item("t:ore")], [item("t:a")])}
        plan = solve(self.A, 1, recipes)
        self.assertEqual(plan.status, Status.MISSING_MATERIALS)
        self.assertEqual(gap_of(plan, ResourceKey.item("t:ore"))["cause"],
                         Cause.NO_RECIPE)

    def test_all_providers_blocked_is_no_provider(self):
        recipes = {self.A: recipe([item("t:ore")], [item("t:a")])}
        plan = solve(self.A, 1, recipes, {}, unavailable=(self.A,))
        self.assertEqual(plan.status, Status.NO_PROVIDER)
        self.assertEqual(gap_of(plan, self.A)["cause"], Cause.NO_PROVIDER)

    def test_flipping_one_provider_online_changes_the_answer(self):
        """把「没有提供者」与「没有配方」的差别做成行为断言，而不只是措辞。"""
        definition = recipe([item("t:ore")], [item("t:a")])
        offline = planner.PatternSnapshot(
            {self.A: (candidate(definition, available=False,
                                 blocker={"code": "driver_unloaded", "detail": "离线"}),)}
        )
        online = planner.PatternSnapshot({self.A: (candidate(definition),)})
        blocked = solve(self.A, 1, {}, {}, patterns_snapshot=offline)
        usable = solve(self.A, 1, {}, {}, patterns_snapshot=online)
        self.assertEqual(blocked.status, Status.NO_PROVIDER)
        self.assertEqual(usable.status, Status.MISSING_MATERIALS)

    def test_the_depth_limit_says_limited_not_no_recipe(self):
        recipes = {}
        for index in range(6):
            recipes[ResourceKey.item("c:%d" % index)] = recipe(
                [item("c:%d" % (index + 1))], [item("c:%d" % index)]
            )
        plan = solve(
            ResourceKey.item("c:0"), 1, recipes,
            policy=planner.PlanPolicy(max_depth=2),
        )
        self.assertEqual(plan.status, Status.LIMITED)
        self.assertEqual(plan.limits["truncated"], True)
        self.assertEqual(plan.limits["hit"], [Cause.DEPTH_LIMIT])
        self.assertEqual(gap_of(plan, ResourceKey.item("c:3"))["cause"],
                         Cause.DEPTH_LIMIT)

    def test_the_step_limit_says_limited(self):
        recipes = {}
        for index in range(6):
            recipes[ResourceKey.item("c:%d" % index)] = recipe(
                [item("c:%d" % (index + 1))], [item("c:%d" % index)]
            )
        plan = solve(
            ResourceKey.item("c:0"), 1, recipes,
            policy=planner.PlanPolicy(max_steps=3),
        )
        self.assertEqual(plan.status, Status.LIMITED)
        self.assertEqual(plan.limits["hit"], [Cause.NODE_LIMIT])

    def test_a_truncated_candidate_window_is_not_no_provider(self):
        """候选窗口截断与「候选都不可用」是两件事。

        混为一谈会让人去修一台没坏的设备——而真正要做的是放宽窗口或去掉
        前面那几块用不了的样板。
        """
        definition = recipe([item("t:u")], [item("t:a")])
        rows = [
            candidate(definition, ordinal=index, node_key=(0, index, 64, 0),
                      available=(index == 2),
                      blocker={"code": "driver_unloaded", "detail": "离线"})
            for index in range(4)
        ]
        plan = solve(
            self.A, 1, {}, {},
            patterns_snapshot=planner.PatternSnapshot({self.A: tuple(rows)}),
            policy=planner.PlanPolicy(max_candidates=2),
        )
        self.assertEqual(plan.status, Status.LIMITED)
        self.assertEqual(gap_of(plan, self.A)["cause"], Cause.CANDIDATE_LIMIT)

    def test_an_index_that_disagrees_with_its_content_builds_no_step(self):
        """候选说它产出这个键、内容里却没有（手工构造或从 JSON 恢复的快照可能
        与内容不一致）：**按没有配方处理，并且不留下一张 0 批次的空步骤**。

        留一张空步骤的代价不是「多一行」——它会进 `dispatch`，
        09 于是拿到一条「派 0 批」的凭据。
        """
        definition = recipe([item("t:u")], [item("t:other")])
        plan = solve(
            self.A, 1, {},
            patterns_snapshot=planner.PatternSnapshot({self.A: (candidate(definition),)}),
        )
        self.assertEqual(plan.status, Status.NO_RECIPE)
        self.assertEqual(plan.steps, [])
        self.assertEqual(gap_of(plan, self.A)["cause"], Cause.NO_RECIPE)
        self.assertIn(u"索引与内容不一致", gap_of(plan, self.A)["detail"])

    def test_a_batch_explosion_is_capped(self):
        """离谱的数量（一个配方要产 1% 的目标）会撞批次上限，而不是把帧跑爆。"""
        plan = solve(
            self.A, 1000000, {self.A: recipe([item("t:u")], [item("t:a")])},
            policy=planner.PlanPolicy(max_batches_per_step=10),
        )
        self.assertEqual(plan.status, Status.LIMITED)
        self.assertEqual(gap_of(plan, self.A)["cause"], Cause.BATCH_LIMIT)


# -- 验收 4：确定性与取消 --------------------------------------------------


class TestDeterminism(unittest.TestCase):
    def test_one_shot_and_frame_split_are_byte_identical(self):
        """同一份输入、不同的预算切法，必须给出**逐字节相同**的计划。"""
        arguments = (
            (PICKAXE, 1, pickaxe_tree(), {RAW_IRON: 2}),
            (ResourceKey.item("t:a"), 1,
             {ResourceKey.item("t:a"): recipe([item("t:b"), item("t:c")], [item("t:a")]),
              ResourceKey.item("t:b"): recipe([item("t:d", 2)], [item("t:b")]),
              ResourceKey.item("t:c"): recipe([item("t:d", 3)], [item("t:c")]),
              ResourceKey.item("t:d"): recipe([item("t:e")], [item("t:d")])},
             {ResourceKey.item("t:d"): 2}),
        )
        for arguments_ in arguments:
            texts = set()
            for budget in (1, 2, 7, 10 ** 9):
                job = planner.plan(
                    arguments_[0], arguments_[1],
                    inventory(arguments_[3]), snapshot(arguments_[2]),
                )
                while job.status() == Status.RUNNING:
                    job.step(budget)
                texts.add(dump(job.result()))
            self.assertEqual(len(texts), 1)

    def test_budget_zero_is_a_no_op(self):
        job = planner.plan(PICKAXE, 1, inventory({}), snapshot(pickaxe_tree()))
        self.assertEqual(job.step(0), Status.RUNNING)
        self.assertEqual(job.spent(), 0)
        self.assertEqual(job.progress()["steps"], 0)

    def test_a_negative_budget_is_refused(self):
        job = planner.plan(PICKAXE, 1, inventory({}), snapshot(pickaxe_tree()))
        with self.assertRaises(ValueError):
            job.step(-1)

    def test_the_same_input_twice_gives_the_same_plan(self):
        first = solve(PICKAXE, 1, pickaxe_tree(), {RAW_IRON: 2})
        second = solve(PICKAXE, 1, pickaxe_tree(), {RAW_IRON: 2})
        self.assertEqual(first.digest(), second.digest())
        self.assertEqual(dump(first), dump(second))

    def test_the_digest_ignores_the_basis(self):
        """指纹只认内容：库存版本不同、内容相同时**不算两份计划**。

        否则 09 的去重与 11 的「预览变了吗」会因为一次无关的库存变动而失效。
        """
        recipes = pickaxe_tree()
        one = solve(PICKAXE, 1, recipes, {RAW_IRON: 2})
        two = solve(PICKAXE, 1, recipes, {RAW_IRON: 2, LOG: 0})
        three = planner.plan(PICKAXE, 1,
                             planner.InventorySnapshot({RAW_IRON: (2, 0, 2)}, version=99),
                             snapshot(recipes))
        three.step(10 ** 9)
        self.assertEqual(one.digest(), two.digest())
        self.assertEqual(one.digest(), three.result().digest())
        self.assertNotEqual(one.seal["inventory_version"],
                            three.result().seal["inventory_version"])

    def test_a_frozen_snapshot_does_not_follow_store_changes(self):
        """快照必须深拷贝成纯值——否则持有活的 `PatternProviderState` 会让
        第 7 帧的结果与第 1 帧不同，而分帧推进正是 08-05 的要求。"""
        store_fixture = StoreFixture()
        store_fixture.setUp()
        store = store_fixture.store
        definition = recipe([item("minecraft:iron_ingot", 9)],
                            [item("minecraft:iron_block")])
        store_fixture.put_pattern(0, definition)
        captured = planner.PatternSnapshot.capture(store)

        # 采集之后把样板拔掉：快照照旧，活 store 已经不是那样了。
        store.uninstall(store_fixture.ref, 0)
        self.assertEqual(len(captured.candidates(ResourceKey.item("minecraft:iron_block"))), 1)
        self.assertEqual(store.candidates(ResourceKey.item("minecraft:iron_block")), [])

        job = planner.plan(
            ResourceKey.item("minecraft:iron_block"), 1,
            inventory({ResourceKey.item("minecraft:iron_ingot"): 9}), captured,
        )
        job.step(10 ** 9)
        self.assertEqual(job.status(), Status.COMPLETE)


class TestSnapshotImmutability(unittest.TestCase):
    """快照是**纯数据**：规划期间不许被改动。

    候选曾经在选中时被标一个 `selected`，于是「规划一次」会改掉快照的内容、
    连同它的指纹——一份被两个任务共用的快照，第二次规划看到的是别人留下的标记。
    """

    def test_planning_does_not_change_the_snapshot(self):
        snapshot_ = snapshot(pickaxe_tree())
        before_digest = snapshot_.digest()
        before_text = json.dumps(snapshot_.to_dict(), sort_keys=True, ensure_ascii=False)
        for _ in range(3):
            job = planner.plan(PICKAXE, 1, inventory({RAW_IRON: 4}), snapshot_)
            job.step(10 ** 6)
        self.assertEqual(snapshot_.digest(), before_digest)
        self.assertEqual(
            json.dumps(snapshot_.to_dict(), sort_keys=True, ensure_ascii=False),
            before_text,
        )

    def test_a_shared_snapshot_gives_both_jobs_the_same_plan(self):
        snapshot_ = snapshot(pickaxe_tree())
        first = planner.plan(PICKAXE, 1, inventory({RAW_IRON: 4}), snapshot_)
        first.step(10 ** 6)
        second = planner.plan(PICKAXE, 1, inventory({RAW_IRON: 4}), snapshot_)
        second.step(10 ** 6)
        self.assertEqual(dump(first.result()), dump(second.result()))


class TestCancellation(unittest.TestCase):
    def test_cancelling_at_every_point_leaves_no_result(self):
        """在每一个中途点取消都必须作废，**半份计划比没有计划更危险**。"""
        for budget in range(1, 12):
            job = planner.plan(PICKAXE, 1, inventory({RAW_IRON: 2}),
                               snapshot(pickaxe_tree()))
            job.step(budget)
            if job.status() != Status.RUNNING:
                break
            self.assertTrue(job.cancel())
            self.assertEqual(job.status(), Status.CANCELLED)
            self.assertIsNone(job.result())
            self.assertEqual(job.progress()["reason"], Reason.PLAN_CANCELLED)

    def test_cancelling_a_finished_job_is_a_no_op(self):
        job = planner.plan(PICKAXE, 1, inventory({}), snapshot(pickaxe_tree()))
        job.step(10 ** 9)
        self.assertFalse(job.cancel())
        self.assertIsNotNone(job.result())

    def test_a_cancelled_job_keeps_no_work_state(self):
        job = planner.plan(PICKAXE, 1, inventory({}), snapshot(pickaxe_tree()))
        job.step(3)
        job.cancel()
        self.assertEqual(job.step(10 ** 9), Status.CANCELLED)
        self.assertIsNone(job.result())


class _RecordingStorage(object):
    """只读库存替身，**记录有没有人来写过**。

    规划是只读计算（08 文档的边界），因此这个替身把「写」做成会抛异常的钩子：
    一旦有代码试图改库存，用例立刻炸——比事后断言「内容没变」更早、更准。
    """

    def __init__(self, rows=None, version=3):
        self._rows = dict(rows or {})
        self.index_version = version
        self.reads = 0

    def keys(self):
        return tuple(sorted(self._rows, key=lambda key: key.canonical()))

    def query(self, key):
        self.reads += 1
        stored, reserved, available = self._rows.get(key, (0, 0, 0))
        return {
            "key": key.to_dict(),
            "stored": stored,
            "reserved": reserved,
            "available": available,
            "version": self.index_version,
        }

    def _write(self, *args, **kwargs):
        raise AssertionError("规划不得改动库存")

    insert = _write
    extract = _write
    reserve = _write
    release = _write
    commit_insert = _write
    commit_extract = _write


class TestReadOnly(unittest.TestCase):
    def test_cancelling_never_touches_the_inventory(self):
        """取消**不可以**在库存上留下任何痕迹——用一个「谁敢写就炸」的替身证明。

        比事后断言「内容没变」更早也更准：后者只有在实现恰好写回原值时才漏判。
        """
        storage = _RecordingStorage({RAW_IRON: (2, 0, 2)})
        snapshot_ = planner.InventorySnapshot.capture(storage)
        job = planner.plan(PICKAXE, 1, snapshot_, snapshot(pickaxe_tree()))
        job.step(1)
        job.step(1)
        self.assertEqual(job.status(), Status.RUNNING)
        self.assertTrue(job.cancel())
        self.assertIsNone(job.result())
        self.assertEqual(job.step(10 ** 6), Status.CANCELLED)

    def test_cancelled_work_is_bounded(self):
        """取消之后不再消耗任何工作量：`step` 是幂等的。"""
        job = planner.plan(PICKAXE, 1, inventory({}), snapshot(pickaxe_tree()))
        job.step(2)
        spent = job.spent()
        job.cancel()
        for _ in range(3):
            job.step(1000)
        self.assertEqual(job.spent(), spent)

    def test_planning_never_touches_the_inventory(self):
        storage = _RecordingStorage({RAW_IRON: (2, 0, 2)})
        snapshot_ = planner.InventorySnapshot.capture(storage)
        job = planner.plan(PICKAXE, 1, snapshot_, snapshot(pickaxe_tree()))
        job.step(10 ** 9)
        self.assertEqual(job.status(), Status.MISSING_MATERIALS)
        # 采集是只读的，规划全程连一次采集都不需要。
        self.assertEqual(storage.reads, 1)

    def test_the_inventory_snapshot_reads_the_documented_surface(self):
        storage = _RecordingStorage({RAW_IRON: (5, 2, 3)})
        snapshot_ = planner.InventorySnapshot.capture(storage)
        self.assertEqual(snapshot_.stored(RAW_IRON), 5)
        self.assertEqual(snapshot_.reserved(RAW_IRON), 2)
        self.assertEqual(snapshot_.available(RAW_IRON), 3)
        self.assertEqual(snapshot_.version, 3)

    def test_reserved_stock_is_not_ours_to_use(self):
        """别人的预留看不到（05 的口径）：规划只认 available。"""
        plan = solve(PICKAXE, 1, pickaxe_tree(), {RAW_IRON: (3, 2, 1)})
        self.assertEqual(row_of(plan, RAW_IRON)["from_stock"], 1)
        self.assertEqual(row_of(plan, RAW_IRON)["reserved"], 2)
        self.assertEqual(row_of(plan, RAW_IRON)["outstanding"], 2)

    def test_the_real_storage_service_exposes_the_same_surface(self):
        """鸭子类型的契约检查：`capture` 要的那两个入口在真实实现上存在且可用。

        真正的数值核对在游戏内的 `debug_query("plan")` 上做——那里的库存
        是真的；这一条只保证「离线能采、形状对得上」。
        """
        service = StorageService()
        snapshot_ = planner.InventorySnapshot.capture(service)
        self.assertEqual(len(snapshot_), 0)
        self.assertEqual(snapshot_.version, service.index_version)
        payload = planner.InventorySnapshot.from_payload(service.snapshot())
        self.assertEqual(len(payload), 0)

    def test_an_inventory_payload_round_trips(self):
        """12/11 会从 JSON 交进来，因此纯字典入口也要能用。"""
        payload = {
            "version": 8,
            "keys": [RAW_IRON.to_dict()],
            "entries": [{"key": RAW_IRON.to_dict(), "stored": 4, "reserved": 1,
                         "available": 3}],
        }
        snapshot_ = planner.InventorySnapshot.from_payload(payload)
        self.assertEqual(snapshot_.available(RAW_IRON), 3)
        self.assertEqual(snapshot_.version, 8)
        self.assertTrue(isinstance(snapshot_.keys()[0], ResourceKey))


# -- 验收 5：提交前复核 ----------------------------------------------------


class StoreFixture(object):
    """接真 `PatternStore` 的最小夹具（容器是 `FakeContainer`）。

    复核那一条腿必须接真的 store：`check_dispatch` 的答案是 07 的规则，
    替身回答不出「文档里说的那几种失效」。
    """

    def setUp(self):
        self.definitions = DefinitionSet()
        self.registry = _Registry()
        self.containers = _ContainerProvider()
        self.store = PatternStore(self.definitions, self.registry, containers=self.containers)
        self.ref = DeviceRef(NodeKey(0, 10, 64, 0), 1)
        self.registry.add(self.ref, TEMPLATE_DEVICE)
        self.state = self.store.ensure(self.ref, TEMPLATE_DEVICE)
        self.container = self.containers.container_for(self.ref)

    def put_pattern(self, slot, definition):
        self.container.write_slot(slot, self.store.template_item(definition))
        self.store.refresh(self.ref, force=True)


class _Record(object):
    def __init__(self, ref, device_id):
        self.ref = ref
        self.device_id = device_id
        self.config = {}
        self.loaded = True
        self.facing = None


class _Registry(object):
    def __init__(self):
        self.records = {}

    def add(self, ref, device_id):
        record = _Record(ref, device_id)
        self.records[ref.node_key.as_key()] = record
        return record

    def get(self, node_key):
        return self.records.get(node_key.as_key())

    def get_by_ref(self, ref):
        record = self.records.get(ref.node_key.as_key())
        if record is None or record.ref.generation != ref.generation:
            return None
        return record


class _ContainerProvider(object):
    def __init__(self, slots=9, max_stack=1):
        self.slots = slots
        self.max_stack = max_stack
        self.by_position = {}

    def container_at(self, node_key):
        position = node_key.as_key()
        if position not in self.by_position:
            self.by_position[position] = FakeContainer(
                slots=self.slots, max_stack=self.max_stack
            )
        return self.by_position[position]

    def container_for(self, ref):
        return self.container_at(ref.node_key)


IRON_BLOCK = ResourceKey.item("minecraft:iron_block")
IRON_NUGGET = ResourceKey.item("minecraft:iron_nugget")


class TestRevalidation(StoreFixture, unittest.TestCase):
    """08 验收第 5 条：规划之后依据变了，提交方必须能识别。"""

    def setUp(self):
        StoreFixture.setUp(self)
        self.definition = recipe([item("minecraft:iron_ingot", 9)],
                                 [item("minecraft:iron_block")])
        self.put_pattern(0, self.definition)
        self.patterns_snapshot = planner.PatternSnapshot.capture(self.store, topology_version=5)
        self.inventory_snapshot = planner.InventorySnapshot.capture(
            _RecordingStorage({INGOT: (9, 0, 9)}), topology_version=5
        )
        job = planner.plan(IRON_BLOCK, 1, self.inventory_snapshot, self.patterns_snapshot)
        job.step(10 ** 9)
        self.plan = job.result()
        self.assertEqual(self.plan.status, Status.COMPLETE)

    def fresh_inventory(self, rows=None):
        return planner.InventorySnapshot.capture(
            _RecordingStorage(rows if rows is not None else {INGOT: (9, 0, 9)}),
            topology_version=5,
        )

    def revalidate(self, rows=None, topology_version=5, checker=True):
        return self.plan.revalidate(
            check_dispatch=self.store.check_dispatch if checker else None,
            inventory_snapshot=self.fresh_inventory(rows),
            topology_version=topology_version,
        )

    def test_the_plan_claims_the_stock_it_uses(self):
        self.assertEqual(
            [(claim["key"], claim["count"]) for claim in self.plan.seal["claims"]],
            [("minecraft:iron_ingot", 9)],
        )

    def test_the_dispatch_carries_the_triple_07_asked_for(self):
        """计划必须记住 `(提供者, 槽位, 指纹)`——07 交付说明第九节点名要的。"""
        dispatch = self.plan.seal["dispatch"]
        self.assertEqual(len(dispatch), 1)
        self.assertEqual(dispatch[0]["slot"], 0)
        self.assertEqual(dispatch[0]["digest"], self.definition.digest())
        self.assertEqual(dispatch[0]["mode"], patterns.MODE_FACTORY)
        ref, slot, digest, mode = self.plan.dispatch_triples()[0]
        self.assertEqual(ref, self.ref)
        self.assertEqual((slot, digest, mode), (0, self.definition.digest(), patterns.MODE_FACTORY))

    def test_unchanged_inputs_pass(self):
        report = self.revalidate()
        self.assertTrue(report.ok())
        self.assertTrue(report.fully_checked())
        self.assertEqual(report.reasons, [])

    def test_an_edited_pattern_is_stale(self):
        """样板被编辑成另一块（同一槽位）：07 的 `pattern_stale` 原样透出。"""
        self.put_pattern(0, recipe([item("minecraft:iron_ingot", 8)],
                                   [item("minecraft:iron_block")]))
        report = self.revalidate()
        self.assertFalse(report.ok())
        self.assertIn(Reason.PATTERN_STALE, report.codes())
        self.assertEqual(len(report.stale_dispatch), 1)

    def test_a_removed_provider_is_stale(self):
        self.store.drop(self.ref)
        report = self.revalidate()
        self.assertFalse(report.ok())
        self.assertIn(Reason.DEVICE_GONE, report.codes())

    def test_an_unloaded_provider_is_stale(self):
        self.store.mark_loaded(self.ref.node_key, False)
        report = self.revalidate()
        self.assertFalse(report.ok())
        self.assertIn(Reason.DRIVER_UNLOADED, report.codes())

    def test_shrunk_stock_is_reported_with_the_keys(self):
        report = self.revalidate({INGOT: (4, 0, 4)})
        self.assertFalse(report.ok())
        self.assertIn(Reason.PLAN_STOCK_CHANGED, report.codes())
        self.assertEqual(len(report.stale_stock), 1)
        self.assertEqual(report.stale_stock[0][1:], (9, 4))

    def test_a_vanished_claimed_key_is_reported_not_raised(self):
        """**声明要用的键整个消失了**，必须报 `plan_stock_changed`，不能抛异常。

        05 的索引在存量归零时会把整行删掉（`_set_stored`），因此「被拿光了」
        与「从来没见过」在新快照里是同一个样子——而那正是最该拦下来的一种变化。
        复核本身崩掉比误判更糟：09 会失去唯一那道闸，而异常还会冒到帧里。
        """
        report = self.revalidate({})
        self.assertFalse(report.ok())
        self.assertIn(Reason.PLAN_STOCK_CHANGED, report.codes())
        self.assertEqual(len(report.stale_stock), 1)
        self.assertEqual(report.stale_stock[0][-1], 0)
        # 报告照样写得出来，且带名字（名字来自计划自己记着的那份材料清单）。
        text = json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True)
        self.assertIn("minecraft:iron_ingot", text)

    def test_an_inventory_payload_as_a_mapping_is_accepted(self):
        """真实链路上复核拿到的是 `InventorySnapshot.capture` 的结果，
        离线这条用 `_RecordingStorage` 造同一种东西（契约一致）。"""
        fresh = planner.InventorySnapshot.capture(_RecordingStorage({}))
        report = self.plan.revalidate(
            check_dispatch=self.store.check_dispatch,
            inventory_snapshot=fresh,
            topology_version=5,
        )
        self.assertEqual(report.codes(), [Reason.PLAN_STOCK_CHANGED])

    def test_unrelated_stock_churn_does_not_invalidate(self):
        """计划没声明过的键怎么变都与它无关——否则 09 会反复重规划。"""
        report = self.revalidate({INGOT: (9, 0, 9), IRON_NUGGET: (0, 0, 0)})
        self.assertTrue(report.ok())

    def test_a_topology_bump_is_reported(self):
        report = self.revalidate(topology_version=6)
        self.assertFalse(report.ok())
        self.assertIn(Reason.PLAN_TOPOLOGY_CHANGED, report.codes())
        self.assertTrue(report.topology_changed)

    def test_a_missing_checker_is_not_a_pass(self):
        """**未检查不等于通过。** 没给复核器时 `ok` 必须是假。"""
        report = self.revalidate(checker=False)
        self.assertFalse(report.ok())
        self.assertFalse(report.fully_checked())
        self.assertFalse(report.checked["patterns"])

    def test_an_uncaptured_topology_is_not_a_pass(self):
        store = StoreFixture()
        store.setUp()
        store.put_pattern(0, self.definition)
        snapshot_ = planner.PatternSnapshot.capture(store.store)  # 不带拓扑版本
        job = planner.plan(IRON_BLOCK, 1, inventory({INGOT: 9}), snapshot_)
        job.step(10 ** 9)
        report = job.result().revalidate(
            check_dispatch=store.store.check_dispatch,
            inventory_snapshot=inventory({INGOT: 9}),
            topology_version=5,
        )
        self.assertFalse(report.ok())
        self.assertFalse(report.checked["topology"])

    def test_revalidation_never_mutates_anything(self):
        """复核只回答「依据还成立吗」，不替 09 做处置（不扣料、不重规划）。"""
        storage = _RecordingStorage({INGOT: (9, 0, 9)})
        before = storage.reads
        plan_before = dump(self.plan)
        self.revalidate({INGOT: (1, 0, 1)})
        self.assertEqual(storage.reads, before)
        self.assertEqual(dump(self.plan), plan_before)

    def test_the_report_is_plain_data(self):
        text = json.dumps(self.revalidate().to_dict(), ensure_ascii=False, sort_keys=True)
        self.assertIn("checked", text)


# -- 策略与接口 ------------------------------------------------------------


class TestTargetSemantics(unittest.TestCase):
    """R11 的两侧各有用例；替身取 `HOLD`。"""

    A = ResourceKey.item("t:a")
    B = ResourceKey.item("t:b")

    def setUp(self):
        self.recipes = {self.A: recipe([item("t:b")], [item("t:a")])}

    def test_hold_nets_out_what_is_already_stored(self):
        plan = solve(self.A, 3, self.recipes, {self.A: 5})
        self.assertEqual(plan.status, Status.COMPLETE)
        self.assertEqual(plan.steps, [])
        summary = plan.summary()
        self.assertEqual((summary["requested"], summary["held"], summary["produced"]), (3, 3, 0))

    def test_hold_crafts_only_the_difference(self):
        plan = solve(self.A, 3, self.recipes, {self.A: 1, self.B: 9})
        self.assertEqual(row_of(plan, self.A)["from_stock"], 1)
        self.assertEqual(row_of(plan, self.A)["own_output"], 2)

    def test_produce_ignores_what_is_already_stored(self):
        policy = planner.PlanPolicy(target_semantics=Targets.PRODUCE)
        plan = solve(self.A, 3, self.recipes, {self.A: 5, self.B: 9}, policy=policy)
        row = row_of(plan, self.A)
        self.assertEqual(row["from_stock"], 0)
        self.assertEqual(row["own_output"], 3)
        self.assertEqual(plan.summary()["held"], 0)

    def test_the_default_is_hold(self):
        self.assertEqual(planner.PlanPolicy().target_semantics, Targets.HOLD)

    def test_no_step_can_consume_the_target(self):
        """**没有哪条被接受的边指向目标键**——那样的边必然成环，因此被拒绝。

        这是结构性质而不是约定：每个步骤的需求都最终源自根，因此
        「某步 → 目标」与「目标 → … → 某步」合起来就是环。它让 R11 的
        `exempt` 只需要管根那一条需求：另一条路径根本不存在。

        注意判据是**边图**而不是步骤的 `inputs` 列表：后者照抄配方内容，
        因此被拒绝的那条输入仍然会列在里面（它确实要东西，只是这条依赖不被采纳）。
        """
        job = planner.plan(
            self.A, 1, inventory({}),
            snapshot({
                self.A: recipe([item("t:b")], [item("t:a")]),
                self.B: recipe([item("t:a")], [item("t:b")]),
            }),
        )
        job.step(10 ** 6)
        self.assertEqual(job.status(), Status.CYCLE)
        for _parent, child, _count in job.edges():
            self.assertNotEqual(child, self.A)
        self.assertEqual(
            [(parent, child) for parent, child, _count in job.refused_edges()],
            [(self.B, self.A)],
        )

    def test_unimplemented_policy_choices_raise_at_construction(self):
        """08-04 的两条还没定案：在装配期报错，而不是给出一个像样的顺序。"""
        with self.assertRaises(ValueError):
            planner.PlanPolicy(candidate_order=Candidates.LEAST_MISSING)
        with self.assertRaises(ValueError):
            planner.PlanPolicy(alternative_recipes=Alternatives.SEARCH_MULTIPLE)

    def test_an_unknown_target_semantics_raises(self):
        with self.assertRaises(ValueError):
            planner.PlanPolicy(target_semantics="whatever")


class TestCandidateSelection(unittest.TestCase):
    """候选怎么挑（08-04）。

    **这一族用例是本轮补上的**：原来没有任何夹具带**两个可用**的候选，
    于是「取第一个可用的」这条规则（`CandidateOrder.STABLE` 的全部内容）
    一次都没被验过——而它是 08-04 的替身口径，也是最容易被下一个人改掉的地方。
    """

    T = ResourceKey.item("t:t")

    def rows(self, count, usable):
        """造 `count` 个候选，只有下标在 `usable` 里的可用。"""
        definition = recipe([item("t:u")], [item("t:t")])
        return tuple(
            candidate(
                definition,
                ordinal=index,
                node_key=(0, 10 + index, 64, 0),
                slot=index,
                available=(index in usable),
                blocker=None if index in usable
                else {"code": "driver_unloaded", "detail": "离线"},
            )
            for index in range(count)
        )

    def solve_with(self, rows, policy=None):
        # 库存给的是**原料**（`t:u`）而不是目标——给目标的话 HOLD 口径下
        # 一步都不用做，也就没有步骤可选了。
        snapshot_ = planner.PatternSnapshot({self.T: tuple(rows)})
        return solve(self.T, 1, {}, {ResourceKey.item("t:u"): 1},
                     patterns_snapshot=snapshot_, policy=policy)

    def test_the_first_usable_candidate_is_chosen(self):
        """多个可用候选时取**快照顺序里的第一个可用**，不是第一个。"""
        plan = self.solve_with(self.rows(3, usable=(1, 2)))
        step = step_of(plan, self.T)
        self.assertEqual(step["provider"]["ordinal"], 1)
        self.assertEqual(step["provider"]["slot"], 1)
        self.assertEqual([item["ordinal"] for item in step["candidates"]], [0, 1, 2])

    def test_an_offline_candidate_earlier_in_the_order_is_skipped(self):
        plan = self.solve_with(self.rows(3, usable=(2,)))
        self.assertEqual(step_of(plan, self.T)["provider"]["ordinal"], 2)

    def test_the_window_is_sliced_before_the_availability_filter(self):
        """窗口之外**哪怕有可用候选也不算数**——「先截断再过滤」的顺序是刻意的。

        反过来（先过滤再取前 N 个）会让窗口宽度随「恰好有几台离线」变化，
        于是同一份输入不可复现，而截断还会被误报成「没有提供者」。
        """
        rows = self.rows(4, usable=(3,))
        plan = self.solve_with(rows, policy=planner.PlanPolicy(max_candidates=2))
        self.assertEqual(plan.status, Status.LIMITED)
        self.assertEqual(gap_of(plan, self.T)["cause"], Cause.CANDIDATE_LIMIT)
        # 放宽窗口之后同一个候选是可用的：证明刚才拒绝的原因是「没看完」。
        wider = self.solve_with(rows)
        self.assertEqual(step_of(wider, self.T)["provider"]["ordinal"], 3)

    def test_an_unimplemented_order_raises_instead_of_picking_one(self):
        with self.assertRaises(ValueError):
            self.solve_with(self.rows(2, usable=(0, 1)),
                            policy=planner.PlanPolicy(
                                candidate_order=Candidates.LEAST_MISSING))


class TestPolicyReport(unittest.TestCase):
    def test_every_field_is_reported_unresolved(self):
        """少报一条就等于正式验收时让 R11/R04/08-04 漏网（全局约束 7）。"""
        policy = planner.PlanPolicy()
        fields = set(item["field"] for item in policy.unresolved_report())
        self.assertEqual(len(policy.unresolved_report()), len(planner.UNRESOLVED_FIELDS))
        self.assertIn("plan.target_semantics", fields)
        self.assertIn("plan.memory_units_per_step", fields)
        rules = set(item["rule"] for item in policy.unresolved_report())
        self.assertEqual(rules, set(["R11", "R04", "08-04"]))

    def test_assert_settled_refuses_to_declare_victory(self):
        with self.assertRaises(AssertionError):
            planner.PlanPolicy().assert_settled()

    def test_describe_is_plain_data(self):
        text = json.dumps(planner.PlanPolicy().describe(), ensure_ascii=False, sort_keys=True)
        self.assertIn("target_semantics", text)

    def test_the_gap_causes_are_partitioned(self):
        self.assertEqual(Cause.LIMIT_CAUSES & Cause.ABSENCE_CAUSES, frozenset())
        self.assertEqual(
            Cause.LIMIT_CAUSES | Cause.ABSENCE_CAUSES | Cause.DERIVED_CAUSES
            | frozenset([Cause.CYCLE]),
            Cause.ALL,
        )


class TestPlanSurface(unittest.TestCase):
    """计划上那些**会给 09/11/12 读**的字段与访问器。

    它们大多只是转发，但正因为「只是转发」才更该有一句断言：少一个字段、
    或者把布尔写反了，离线用例全绿而下游拿到的是错的东西。
    """

    def test_the_identity_fields_match_the_target(self):
        plan = solve(PICKAXE, 3, pickaxe_tree(), {RAW_IRON: 6, LOG: 2})
        self.assertEqual(plan.version, planner.PLAN_FORMAT_VERSION)
        self.assertEqual(plan.amount, 3)
        self.assertEqual(plan.target, PICKAXE.canonical())

    def test_is_satisfied_follows_the_status(self):
        complete = solve(ResourceKey.item("t:none"), 1, {
            ResourceKey.item("t:none"): recipe([item("t:u")], [item("t:none")]),
        }, {ResourceKey.item("t:u"): 1})
        self.assertEqual(complete.status, Status.COMPLETE)
        self.assertTrue(complete.is_satisfied())
        # HOLD 口径下「库里已经够了」也是满足——零步骤不等于没算完。
        held = solve(ResourceKey.item("t:none"), 1,
                     {ResourceKey.item("t:none"): recipe([item("t:u")], [item("t:none")])},
                     {ResourceKey.item("t:none"): 2})
        self.assertEqual(held.steps, [])
        self.assertTrue(held.is_satisfied())
        missing = solve(PICKAXE, 1, pickaxe_tree(), {})
        self.assertFalse(missing.is_satisfied())

    def test_reasons_map_the_verdict_to_the_command_layer(self):
        """总判决 -> 命令原因码。**成功与进行中必须有空表**：
        空表是「没有失败原因」，不是「原因丢了」。"""
        ok = solve(ResourceKey.item("t:none"), 1, {
            ResourceKey.item("t:none"): recipe([item("t:u")], [item("t:none")]),
        }, {ResourceKey.item("t:u"): 1})
        self.assertEqual(ok.reasons(), [])
        problem = solve(ResourceKey.item("t:unknown"), 1, {})
        codes = [reason.code for reason in problem.reasons()]
        self.assertEqual(codes, [Reason.PLAN_NO_RECIPE])
        self.assertTrue(problem.reasons()[0].detail)

    def test_a_running_job_has_no_plan_and_no_reasons(self):
        job = planner.plan(PICKAXE, 1, inventory({}), snapshot(pickaxe_tree()))
        self.assertIsNone(job.result())
        self.assertEqual(job.status(), Status.RUNNING)

    def test_execution_order_covers_every_step_once(self):
        plan = solve(PICKAXE, 1, pickaxe_tree(), {RAW_IRON: 3, LOG: 1})
        order = plan.execution_order()
        self.assertEqual(sorted(order), list(range(len(plan.steps))))
        self.assertEqual(order, [step["index"] for step in plan.steps])


class TestEstimates(unittest.TestCase):
    def test_estimates_are_marked_as_estimates(self):
        """08-06：估计值**不是**真实资源已预留的证明。"""
        plan = solve(PICKAXE, 1, pickaxe_tree(), {RAW_IRON: 2})
        self.assertFalse(plan.estimates["authoritative"])
        self.assertEqual(plan.estimates["slots"], len(plan.steps))
        self.assertEqual(
            plan.estimates["memory_units"],
            len(plan.steps) * planner.PlanPolicy().memory_units_per_step,
        )
        self.assertIn("R04", json.dumps(plan.estimates["policy"]["unresolved"]))

    def test_the_estimate_policy_is_the_one_that_ran(self):
        """策略必须是**实际生效的那一份对象**，否则诊断报的数值与行为对不上。"""
        policy = planner.PlanPolicy(memory_units_per_step=7)
        plan = solve(PICKAXE, 1, pickaxe_tree(), {RAW_IRON: 2}, policy=policy)
        self.assertEqual(
            plan.estimates["memory_units"], len(plan.steps) * 7
        )
        self.assertEqual(plan.estimates["policy"]["memory_units_per_step"], 7)


class TestPlannerFacade(unittest.TestCase):
    def test_solve_returns_a_plan(self):
        facade = planner.Planner()
        plan = facade.solve(PICKAXE, 1, inventory({RAW_IRON: 2}), snapshot(pickaxe_tree()))
        self.assertEqual(plan.status, Status.MISSING_MATERIALS)
        self.assertEqual(facade.stats["solved"], 1)

    def test_solve_gives_up_at_the_work_limit(self):
        """防呆：调用方忘了循环 step 时不能在一帧里把整张图跑爆。"""
        facade = planner.Planner(work_limit=2)
        plan = facade.solve(PICKAXE, 1, inventory({RAW_IRON: 2}), snapshot(pickaxe_tree()))
        self.assertIsNone(plan)
        self.assertEqual(facade.stats["cancelled"], 1)

    def test_the_facade_hands_its_policy_to_the_job(self):
        policy = planner.PlanPolicy(memory_units_per_step=3)
        facade = planner.Planner(policy=policy)
        job = facade.plan(PICKAXE, 1, inventory({}), snapshot(pickaxe_tree()))
        self.assertIs(job.policy, policy)

    def test_capture_helpers_are_wired(self):
        facade = planner.Planner()
        storage = _RecordingStorage({RAW_IRON: (1, 0, 1)})
        snapshot_ = facade.capture_inventory(storage)
        self.assertEqual(snapshot_.available(RAW_IRON), 1)
        self.assertEqual(len(facade.capture_patterns(None)), 0)

    def test_diagnostics_name_what_is_not_done(self):
        report = planner.Planner().diagnostics()
        self.assertIn("09", report["scope_note"])
        self.assertEqual(len(report["unresolved"]), len(planner.UNRESOLVED_FIELDS))


class TestArgumentGuards(unittest.TestCase):
    def setUp(self):
        self.recipes = pickaxe_tree()

    def test_a_non_resource_target_is_refused(self):
        with self.assertRaises(TypeError):
            planner.plan("minecraft:iron_pickaxe", 1, inventory({}), snapshot(self.recipes))

    def test_a_non_positive_amount_is_refused(self):
        for amount in (0, -1):
            with self.assertRaises(ValueError):
                planner.plan(PICKAXE, amount, inventory({}), snapshot(self.recipes))

    def test_a_boolean_amount_is_refused(self):
        """`True` 是 `int` 的子类，但它不是数量（与 `patterns` 同一条判据）。"""
        with self.assertRaises(TypeError):
            planner.plan(PICKAXE, True, inventory({}), snapshot(self.recipes))

    def test_wrong_snapshot_types_are_refused(self):
        with self.assertRaises(TypeError):
            planner.plan(PICKAXE, 1, {}, snapshot(self.recipes))
        with self.assertRaises(TypeError):
            planner.plan(PICKAXE, 1, inventory({}), {})

    def test_progress_is_plain_data(self):
        job = planner.plan(PICKAXE, 1, inventory({}), snapshot(self.recipes))
        job.step(2)
        text = json.dumps(job.progress(), ensure_ascii=False, sort_keys=True)
        self.assertIn("pending", text)
        self.assertEqual(job.progress()["reason"], Reason.PLAN_BUDGET_EXHAUSTED)


# -- 性质测试：守恒、可解释、有界 ------------------------------------------
#
# 用固定种子生成随机图（含环），逐条核对账本恒等式与终止界。它们比逐个夹具的
# 用例更容易在**将来**抓到回归：夹具是人挑的，而随机图会撞到人没想到的形状。


def random_recipes(random_source, size, keys):
    """随机配方图。允许成环——环正是要考的东西。

    **输入里刻意保留重复条目**（同一种料写两遍）。第一版这里做了去重，
    于是「重复输入」那个缺陷在整个性质测试里一次都没被撞到——而真机上
    合成样板的有序摆位**必然**写成重复条目。去重的代价是这层测试对一个
    合法且常见的形状视而不见。
    """
    recipes = {}
    for index in range(size):
        key = keys[index]
        inputs = []
        for _ in range(random_source.randint(1, 3)):
            target = random_source.choice(keys)
            inputs.append(patterns.PatternStack(target, random_source.randint(1, 3)))
        yield_count = random_source.randint(1, 3)
        outputs = [patterns.PatternStack(key, yield_count)]
        if random_source.random() < 0.25:
            outputs.append(
                patterns.PatternStack(
                    random_source.choice(keys), random_source.randint(1, 2)
                )
            )
        recipes[key] = patterns.PatternDefinition(
            patterns.MODE_FACTORY, inputs, outputs
        )
    return recipes


class TestConservationProperties(unittest.TestCase):
    ROUNDS = 40

    def plans(self):
        for seed in range(self.ROUNDS):
            random_source = random.Random(20250925 + seed)
            size = random_source.randint(2, 6)
            keys = [ResourceKey.item("p:%d" % index) for index in range(size)]
            recipes = random_recipes(random_source, size, keys)
            if not recipes:
                continue
            target = random_source.choice(sorted(recipes, key=lambda key: key.canonical()))
            totals = {}
            for key in keys:
                if random_source.random() < 0.4:
                    totals[key] = random_source.randint(0, 4)
            policy = planner.PlanPolicy(
                max_depth=random_source.choice([2, 4, 16]),
                max_steps=random_source.choice([3, 20]),
            )
            job = planner.plan(
                target, random_source.randint(1, 4), inventory(totals),
                snapshot(recipes), policy=policy,
            )
            while job.status() == Status.RUNNING:
                job.step(random_source.choice([1, 3, 1000]))
            yield job, recipes, totals

    def test_the_ledger_identities_hold(self):
        """账本恒等式 + **两条真正能失败的量界**。

        前四个等式是派生量的定义（顺手核一下无妨），真正有判别力的是后两条：
        「用掉的库存不超过可用量」与「不许抵扣的部分一律不从库存里拿」——
        它们能被一个写错的 `_recompute_from_stock` 打破。
        """
        for job, _recipes, _storage in self.plans():
            snapshot_ = job.inventory
            for row in job.ledger():
                self.assertEqual(
                    row.pools(), row.from_stock + row.own_output + row.byproduct_output
                )
                self.assertEqual(row.covered(), min(row.demand, row.pools()))
                self.assertEqual(row.outstanding(), row.demand - row.covered())
                self.assertEqual(row.surplus(), row.pools() - row.covered())
                self.assertLessEqual(row.from_stock, snapshot_.available(row.key))
                creditable = max(0, row.demand - row.exempt)
                self.assertLessEqual(row.from_stock, creditable)

    def test_the_generator_exercises_real_shapes(self):
        """性质测试的输入必须有**够宽的形状**，否则「随机图」只是好看。

        第一版生成器去重了输入、又常生成只有一两个键的小图，于是几十轮里
        大半落在同一条分支上——一个缺陷可以在整套性质测试里一次都不现形。
        这里把「够宽」写成断言：足够的步骤、足够的键、既有环也有做不完的图，
        以及**至少一轮含「同一种料写了两遍」的配方**（那个形状正是缺陷藏身处）。
        """
        shapes = []
        for job, _recipes, _storage in self.plans():
            plan = job.result()
            shapes.append(
                (
                    len(plan.steps),
                    len(plan.materials),
                    plan.status,
                    # 配方的原始输入条数 > 合并后的条数 => 出现过重复输入
                    any(
                        len(step.required_inputs) > len(step.input_pairs())
                        for step in job._steps.values()
                    ),
                )
            )
        self.assertTrue(shapes)
        self.assertTrue(any(item[0] >= 3 for item in shapes))
        self.assertTrue(any(item[1] >= 4 for item in shapes))
        self.assertTrue(
            set(item[2] for item in shapes)
            & set([Status.CYCLE, Status.MISSING_MATERIALS])
        )
        self.assertTrue(any(item[3] for item in shapes))

    def test_every_planned_output_is_credited_exactly_once(self):
        """计划产出与副产物产出都必须与各步骤报出的量**一一对上**。

        两者分开核：某键自己的步骤产出的算 `own_output`，别的步骤当副产物
        产出的算 `byproduct_output`。混在一起的核对会漏掉「同一个键既是自己的
        产物、又是别人的副产物」那种形状——而那正是重复承诺最容易钻进去的地方。
        """
        for job, _recipes, _storage in self.plans():
            own = {}
            byproduct = {}
            for step in job._steps.values():
                own[step.key] = own.get(step.key, 0) + step.batches * step.yield_per_batch()
                for key, per in step.byproduct_pairs():
                    byproduct[key] = byproduct.get(key, 0) + per * step.batches
            for key, row in job._ledger.items():
                self.assertEqual(row.own_output, own.get(key, 0))
                self.assertEqual(row.byproduct_output, byproduct.get(key, 0))

    def test_the_edge_graph_is_acyclic(self):
        """**这条是终止性的根据**：每条被接受的边插入前都问过 `_reachable`，
        因此对任意一条边 `父 -> 子`，「子能不能回到父」必须是 `None`。

        第一版这里问的是「父能不能到达子」（那必然能，就是那条边本身），
        于是它永远为真、什么都没验。判据必须问**反向可达性**：
        能回到父的话，那条边插入时就该被判成环并拒绝。
        """
        exercised = 0
        for job, _recipes, _storage in self.plans():
            for parent, child, _count in job.edges():
                exercised += 1
                path, _visits = job._reachable(child, parent)
                self.assertIsNone(
                    path,
                    u"%s -> %s 这条边成环了（子能回到父）" % (child.name, parent.name),
                )
        self.assertGreater(exercised, 0, "随机图里一条边都没有，这条性质没被验到")

    def test_demand_is_explained_by_the_edges(self):
        """每个键的需求 = 根贡献 + **被接受的**入边毛量之和。

        **被拒绝的边不记需求**：它既不会被任何步骤产出、也不会由库存来补，
        记进去就等于凭空造出一笔永远满足不了的需求（那会让 `PRODUCE` 口径
        误以为自己用上了库存）。环由这个键自己的缺口、父步骤的 `blocked`
        与收尾的 `_achievable` 三处表达，一样都不会丢。
        """
        for job, _recipes, _storage in self.plans():
            incoming = {}
            for _parent, child, count in job.edges():
                incoming[child] = incoming.get(child, 0) + count
            for key, row in job._ledger.items():
                root = job.amount if key == job.target else 0
                self.assertEqual(row.demand, root + incoming.get(key, 0))

    def test_a_bigger_budget_never_changes_the_plan(self):
        for _job, recipes, totals in self.plans():
            for budget in (1, 3, 1000):
                target = None
                for key in sorted(recipes, key=lambda item_: item_.canonical()):
                    target = key
                    break
                job = planner.plan(target, 2, inventory(totals), snapshot(recipes))
                while job.status() == Status.RUNNING:
                    job.step(budget)
                if budget == 1:
                    reference = dump(job.result())
                else:
                    self.assertEqual(dump(job.result()), reference)

    def test_the_work_is_bounded(self):
        """「不会卡住服务端」写成**上界**，而不是「跑完没卡」的一次观察。"""
        for job, recipes, _storage in self.plans():
            # 每个键最多被选中「步骤上限」次（每次至少长一个批次），
            # 而收尾只跑一次；这个上界宽松但有限，且与图的大小无关。
            limit = len(recipes) * (job.policy.max_steps + 1) + 200
            self.assertLess(job.spent(), limit)

    def test_blocked_keys_are_explained(self):
        """被标成执行不了的键必须有原因，而原因必须是已知词汇表里的一个。"""
        for job, _recipes, _storage in self.plans():
            plan = job.result()
            for entry in plan.blocked_keys:
                self.assertIn(entry["cause"], Cause.ALL)
                self.assertGreater(entry["shortfall"], 0)


if __name__ == "__main__":
    unittest.main()
