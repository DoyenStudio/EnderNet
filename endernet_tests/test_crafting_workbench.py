# coding=utf-8
"""合成终端的工作台：九宫格、存档、容器适配与计划（11-04）。

**全部离线。** `server/crafting/workbench.py` 不 import 引擎。

三条贯穿全篇的判据：

1. **不丢物。** 格子里是玩家真实的原料，任何一条路径都不许在没有核对回执的
   情况下让某件物品消失——清空走的是容器回执、导入不覆盖已有实物、
   设备被拆时状态留着而不是删掉；
2. **不静默。** 「格子满了没法导入」和「导入成功但少摆了几格」必须是两件事，
   不能都表现为「点了没反应」；
3. **回执由基类核对。** `WorkbenchContainer` 只说它「声称」放了多少，
   实际数量由 `ContainerAdapter` 读前后总数自己算——因此这一层说错话
   不会流到调用方。
"""

import unittest

import testsupport  # noqa: F401 - 导入即完成 sys.path 设置

from endernet.common.crafting_recipe import normalize_recipe
from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.protocol import TransferStatus
from endernet.common.resource_key import ResourceKey
from endernet.server.crafting.workbench import (
    CLEAR_KEEP_REMAINDER,
    IMPORT_KEEP,
    IMPORT_REFUSE,
    OUTPUT_PLAYER_INVENTORY,
    REFILL_NETWORK_ONLY,
    SLOT_COUNT,
    WORKBENCH_SCHEMA_VERSION,
    CraftingPolicy,
    WorkbenchContainer,
    WorkbenchGrid,
    WorkbenchStore,
    plan_import,
    plan_refill,
)

PLANKS = ResourceKey.item("minecraft:oak_planks")
BIRCH = ResourceKey.item("minecraft:birch_planks")
STICK = ResourceKey.item("minecraft:stick")
LOG = ResourceKey.item("minecraft:oak_log")
PAPER = ResourceKey.item("minecraft:paper")
LEATHER = ResourceKey.item("minecraft:leather")

#: 实测夹具，与 `test_crafting_recipe.py` 同源。
CRAFTING_TABLE = {
    "pattern": ["AA", "AA"], "key": {"A": {"item": "minecraft:oak_planks"}},
    "result": [{"item": "minecraft:crafting_table"}],
}
STICK_FROM_PLANKS = {
    "pattern": ["A", "A"],
    "key": {"A": {"item": ["minecraft:oak_planks", "minecraft:birch_planks"]}},
    "result": [{"count": 4, "item": "minecraft:stick"}],
}
BOOK = {
    "ingredients": [{"count": 3, "item": "minecraft:paper"},
                    {"item": "minecraft:leather"}],
    "result": [{"item": "minecraft:book"}],
}


def device(generation=1, x=0):
    return DeviceRef(NodeKey(0, x, 64, 0), generation)


class TestGrid(unittest.TestCase):

    def test_a_new_grid_is_nine_empty_slots(self):
        grid = WorkbenchGrid()
        self.assertEqual(SLOT_COUNT, len(grid.slots))
        self.assertTrue(grid.is_empty())
        self.assertEqual((), grid.occupied_slots())

    def test_it_refuses_a_wrong_slot_count(self):
        with self.assertRaises(ValueError):
            WorkbenchGrid([None] * 5)

    def test_setting_and_reading_a_slot(self):
        grid = WorkbenchGrid()
        grid.set_slot(3, PLANKS, 4)
        self.assertEqual(PLANKS, grid.key_at(3))
        self.assertEqual(4, grid.count_at(3))
        self.assertEqual((3,), grid.occupied_slots())
        self.assertFalse(grid.is_empty())

    def test_setting_zero_empties_the_slot(self):
        grid = WorkbenchGrid()
        grid.set_slot(3, PLANKS, 4)
        grid.set_slot(3, PLANKS, 0)
        self.assertIsNone(grid.key_at(3))
        self.assertTrue(grid.is_empty())

    def test_adding_stacks_only_the_same_resource(self):
        grid = WorkbenchGrid()
        grid.set_slot(0, PLANKS, 2)
        self.assertEqual(3, grid.add_at(0, PLANKS, 3))
        self.assertEqual(5, grid.count_at(0))
        # 异种**不挤**：返回 0 而不是把原物顶掉。
        self.assertEqual(0, grid.add_at(0, BIRCH, 1))
        self.assertEqual(5, grid.count_at(0))

    def test_adding_to_an_empty_slot_creates_it(self):
        grid = WorkbenchGrid()
        self.assertEqual(2, grid.add_at(5, BIRCH, 2))
        self.assertEqual(BIRCH, grid.key_at(5))

    def test_taking_more_than_there_is_takes_what_there_is(self):
        grid = WorkbenchGrid()
        grid.set_slot(1, PAPER, 3)
        self.assertEqual(3, grid.take_at(1, 10))
        self.assertIsNone(grid.key_at(1))

    def test_taking_part_leaves_the_rest(self):
        grid = WorkbenchGrid()
        grid.set_slot(1, PAPER, 3)
        self.assertEqual(2, grid.take_at(1, 2))
        self.assertEqual(1, grid.count_at(1))

    def test_taking_from_an_empty_slot_is_zero(self):
        self.assertEqual(0, WorkbenchGrid().take_at(4, 1))

    def test_totals_span_every_slot(self):
        grid = WorkbenchGrid()
        grid.set_slot(0, PLANKS, 2)
        grid.set_slot(4, PLANKS, 3)
        grid.set_slot(8, BIRCH, 1)
        self.assertEqual(5, grid.total_of(PLANKS))
        self.assertEqual(1, grid.total_of(BIRCH))
        self.assertEqual(0, grid.total_of(STICK))

    def test_every_write_bumps_the_revision(self):
        grid = WorkbenchGrid()
        start = grid.revision
        grid.set_slot(0, PLANKS, 1)
        self.assertEqual(start + 1, grid.revision)
        grid.set_slot(0, PLANKS, 2)
        self.assertEqual(start + 2, grid.revision)

    def test_a_write_that_changes_nothing_still_bumps_it(self):
        # 宁可多重发一次，也不要漏一次：`revision` 只用来「认新旧」，
        # 不变化时它单调增是无害的，漏增会让客户端以为没更新。
        grid = WorkbenchGrid()
        grid.set_recipe("abc")
        before = grid.revision
        grid.set_recipe("abc")
        self.assertEqual(before, grid.revision)

    def test_cells_flatten_the_key_to_name_and_aux(self):
        grid = WorkbenchGrid()
        grid.set_slot(0, ResourceKey.item("minecraft:wool", aux=4), 2)
        self.assertEqual(("minecraft:wool", 4, 2), grid.cells()[0])
        self.assertIsNone(grid.cells()[1])

    def test_the_recipe_note_is_kept_but_is_not_content(self):
        grid = WorkbenchGrid()
        grid.set_recipe("deadbeef")
        self.assertEqual("deadbeef", grid.recipe)
        # 备忘不影响「格子空不空」。
        self.assertTrue(grid.is_empty())


class TestStore(unittest.TestCase):

    def setUp(self):
        self.store = WorkbenchStore()

    def test_the_same_device_gets_the_same_grid(self):
        ref = device()
        self.assertIs(self.store.of(ref), self.store.of(ref))

    def test_a_rebuilt_device_gets_a_fresh_empty_grid(self):
        # 同一个坐标、更大的代次 = 另一个方块实例。它不该继承旧格里的东西
        # （那是凭空多出来的物品），旧的那份也不该被删（那是凭空少掉）。
        old = self.store.of(device(generation=1))
        old.set_slot(0, PLANKS, 5)
        new = self.store.of(device(generation=2))
        self.assertIsNot(old, new)
        self.assertTrue(new.is_empty())
        self.assertEqual(5, old.count_at(0))

    def test_another_position_is_another_grid(self):
        self.assertIsNot(self.store.of(device(x=0)), self.store.of(device(x=90)))

    def test_get_does_not_create(self):
        self.assertIsNone(self.store.get(device()))
        self.assertEqual(0, self.store.stats["created"])
        self.store.of(device())
        self.assertIsNotNone(self.store.get(device()))

    def test_forget_unindexes_but_does_not_delete(self):
        ref = device()
        grid = self.store.of(ref)
        grid.set_slot(0, PLANKS, 5)
        self.assertTrue(self.store.forget(ref))
        self.assertIsNone(self.store.get(ref))
        self.assertFalse(self.store.forget(ref))
        # 物品真的还在孤儿索引里——不是「测试还握着引用所以没被回收」。
        # 直接丢引用等于替「拆掉方块之后格子里的东西归谁」这条未定规则
        # 定了「丢掉」，而那是一件都追不回来的那一侧。
        self.assertEqual(1, len(self.store.orphans()))
        self.assertEqual(5, self.store.orphans()[0].count_at(0))
        self.assertEqual(5, self.store.diagnostics()["orphan_items"])

    def test_an_orphan_is_not_handed_to_the_next_device(self):
        ref = device()
        self.store.of(ref).set_slot(0, PLANKS, 5)
        self.store.forget(ref)
        # 同一个代次再出现也不自动继承——继承是未定的另一侧。
        self.assertTrue(self.store.of(ref).is_empty())

    def test_adopt_puts_an_orphan_back(self):
        # 「将来定成还回去」那一刻的落点。本轮没有生产调用点。
        ref = device()
        self.store.of(ref).set_slot(0, PLANKS, 5)
        self.store.forget(ref)
        adopted = self.store.adopt(ref)
        self.assertIsNotNone(adopted)
        self.assertEqual(5, self.store.of(ref).count_at(0))
        self.assertEqual(0, len(self.store.orphans()))
        self.assertIsNone(self.store.adopt(ref))

    def test_orphans_survive_a_save_and_restore(self):
        ref = device()
        self.store.of(ref).set_slot(0, PLANKS, 5)
        self.store.forget(ref)
        other = WorkbenchStore()
        self.assertEqual(1, other.restore(self.store.save_all()))
        self.assertEqual(1, len(other.orphans()))
        self.assertEqual(5, other.orphans()[0].count_at(0))

    def test_it_refuses_a_non_device_key(self):
        with self.assertRaises(ValueError):
            self.store.of("not-a-ref")

    def test_save_and_restore_round_trip(self):
        grid = self.store.of(device())
        grid.set_slot(2, PLANKS, 7)
        grid.set_recipe("cafebabe")
        payload = self.store.save_all()

        other = WorkbenchStore()
        self.assertEqual(1, other.restore(payload))
        restored = other.of(device())
        self.assertEqual(PLANKS, restored.key_at(2))
        self.assertEqual(7, restored.count_at(2))
        self.assertEqual("cafebabe", restored.recipe)

    def test_a_newer_schema_is_left_alone_not_read_as_empty(self):
        payload = self.store.save_all()
        payload["schema_version"] = WORKBENCH_SCHEMA_VERSION + 1
        other = WorkbenchStore()
        self.assertEqual(0, other.restore(payload))
        self.assertEqual(0, len(other.grids()))

    def test_diagnostics_say_how_many_are_occupied(self):
        self.store.of(device(x=0)).set_slot(0, PLANKS, 1)
        self.store.of(device(x=90))
        report = self.store.diagnostics()
        self.assertEqual(2, report["grids"])
        self.assertEqual(1, report["occupied"])


class TestContainer(unittest.TestCase):

    def setUp(self):
        self.grid = WorkbenchGrid()
        self.container = WorkbenchContainer(self.grid)

    def test_it_reports_nine_slots(self):
        self.assertEqual(SLOT_COUNT, self.container.slots_count())

    def test_the_default_limit_is_one_not_the_bench_size(self):
        # 导入的规则本来就是「每个原料槽位只摆一个」；退成 1 最多是少放。
        self.assertEqual(1, self.container.limit_of(PLANKS))

    def test_an_injected_limit_is_honoured(self):
        container = WorkbenchContainer(self.grid, limit_of=lambda key: 64)
        self.assertEqual(64, container.limit_of(PLANKS))

    def test_a_bad_injected_limit_falls_back_to_one(self):
        for bad in (0, -3, None, "64", True):
            container = WorkbenchContainer(self.grid, limit_of=lambda key: bad)
            self.assertEqual(1, container.limit_of(PLANKS), repr(bad))

    def test_insert_writes_into_the_grid_and_verifies(self):
        receipt = self.container.insert("op1", PLANKS, 1)
        self.assertEqual(TransferStatus.COMPLETE, receipt.status)
        self.assertEqual(1, receipt.moved)
        self.assertEqual(1, self.grid.count_at(0))

    def test_insert_fills_a_slot_up_before_opening_the_next(self):
        # 容器的语义：填满一格再开下一格。九宫格只有 9 格，
        # 「每格一个」是**导入**的规则（走 `insert_at`），不是容器的规则。
        container = WorkbenchContainer(self.grid, limit_of=lambda key: 8)
        container.insert("op1", PLANKS, 3)
        self.assertEqual([0], list(self.grid.occupied_slots()))
        self.assertEqual(3, self.grid.count_at(0))

    def test_insert_tops_up_an_existing_stack_before_an_empty_slot(self):
        # 9 格很紧，先把同种的半堆叠满能省下一格。
        self.grid.set_slot(4, PLANKS, 2)
        container = WorkbenchContainer(self.grid, limit_of=lambda key: 8)
        container.insert("op1", PLANKS, 2)
        self.assertEqual(4, self.grid.count_at(4))
        self.assertEqual((4,), self.grid.occupied_slots())

    def test_insert_at_puts_it_in_the_slot_it_was_told(self):
        # 导入要的是「摆在哪一格由配方决定」，这正是 `insert` 不做的事。
        receipt = self.container.insert_at("op1", 4, PLANKS, 1)
        self.assertEqual(TransferStatus.COMPLETE, receipt.status)
        self.assertEqual(1, receipt.moved)
        self.assertEqual(PLANKS, self.grid.key_at(4))
        self.assertIsNone(self.grid.key_at(0))

    def test_insert_at_refuses_an_occupied_slot_of_another_kind(self):
        self.grid.set_slot(4, BIRCH, 1)
        receipt = self.container.insert_at("op1", 4, PLANKS, 1)
        self.assertEqual(TransferStatus.REFUSED, receipt.status)
        self.assertEqual(0, receipt.moved)
        # 原有的东西一个没动。
        self.assertEqual(BIRCH, self.grid.key_at(4))
        self.assertEqual(1, self.grid.count_at(4))

    def test_insert_at_refuses_a_slot_outside_the_bench(self):
        for bad in (-1, SLOT_COUNT, 99):
            receipt = self.container.insert_at("op1", bad, PLANKS, 1)
            self.assertEqual(TransferStatus.REFUSED, receipt.status, bad)
            self.assertEqual(0, receipt.moved)

    def test_insert_at_never_places_more_than_the_limit(self):
        container = WorkbenchContainer(self.grid, limit_of=lambda key: 1)
        receipt = container.insert_at("op1", 0, PLANKS, 5)
        self.assertEqual(TransferStatus.PARTIAL, receipt.status)
        self.assertEqual(1, receipt.moved)
        self.assertEqual(1, self.grid.count_at(0))

    def test_insert_at_stacks_onto_the_same_kind(self):
        self.grid.set_slot(0, PLANKS, 1)
        container = WorkbenchContainer(self.grid, limit_of=lambda key: 4)
        receipt = container.insert_at("op1", 0, PLANKS, 2)
        self.assertEqual(TransferStatus.COMPLETE, receipt.status)
        self.assertEqual(3, self.grid.count_at(0))

    def test_insert_refuses_when_every_slot_is_taken_by_something_else(self):
        for index in range(SLOT_COUNT):
            self.grid.set_slot(index, BIRCH, 1)
        receipt = self.container.insert("op1", PLANKS, 1)
        self.assertEqual(TransferStatus.REFUSED, receipt.status)
        self.assertEqual(0, receipt.moved)

    def test_a_partial_insert_says_so(self):
        for index in range(SLOT_COUNT - 1):
            self.grid.set_slot(index, BIRCH, 1)
        receipt = self.container.insert("op1", PLANKS, 3)
        self.assertEqual(TransferStatus.PARTIAL, receipt.status)
        self.assertEqual(1, receipt.moved)

    def test_extract_pulls_from_every_slot(self):
        self.grid.set_slot(0, PLANKS, 1)
        self.grid.set_slot(7, PLANKS, 1)
        receipt = self.container.extract("op1", PLANKS, 2)
        self.assertEqual(TransferStatus.COMPLETE, receipt.status)
        self.assertTrue(self.grid.is_empty())

    def test_extract_when_there_is_nothing_is_refused(self):
        receipt = self.container.extract("op1", PLANKS, 1)
        self.assertEqual(TransferStatus.REFUSED, receipt.status)
        self.assertEqual(0, receipt.moved)

    def test_count_of_says_what_is_there(self):
        self.grid.set_slot(0, PLANKS, 3)
        self.assertEqual(3, self.container.count_of(PLANKS))
        self.assertEqual(0, self.container.count_of(BIRCH))

    def test_simulate_answers_how_much_room_is_left(self):
        self.grid.set_slot(0, PLANKS, 1)
        container = WorkbenchContainer(self.grid, limit_of=lambda key: 4)
        # 第 0 格还差 3 个，另外 8 格各 4 个。
        self.assertEqual(4, container.can_accept(PLANKS, 4))
        self.assertEqual(35, container.can_accept(PLANKS, 99))

    def test_simulate_says_zero_when_the_kind_is_wrong(self):
        for index in range(SLOT_COUNT):
            self.grid.set_slot(index, BIRCH, 1)
        self.assertEqual(0, self.container.can_accept(PLANKS, 1))

    def test_entries_lists_only_occupied_slots(self):
        self.grid.set_slot(3, PLANKS, 2)
        entries = self.container.entries()
        self.assertEqual(1, len(entries))
        self.assertEqual(3, entries[0][0])


class TestCraftingPolicy(unittest.TestCase):

    def test_the_confirmed_choices_are_what_the_user_said(self):
        policy = CraftingPolicy()
        self.assertEqual(OUTPUT_PLAYER_INVENTORY, policy.output_destination)
        self.assertEqual(REFILL_NETWORK_ONLY, policy.refill_source)
        self.assertEqual(IMPORT_REFUSE, policy.import_when_occupied)
        self.assertEqual(CLEAR_KEEP_REMAINDER, policy.clear_remainder)
        self.assertEqual(1, policy.import_per_slot)

    def test_it_names_what_is_still_open(self):
        report = CraftingPolicy().unresolved_report()
        self.assertIn("refill_per_slot", report["unresolved"])
        self.assertIn("import_when_occupied", report["unresolved"])
        self.assertIn("import_when_occupied", report["policy"])

    def test_assert_settled_is_meant_to_fail_while_items_are_open(self):
        # 它问的是「替身口径有没有被正式裁定过」，不是「代码对不对」。
        # 开发期它本来就该失败——因此这里钉住的是它会失败，而不是结果。
        with self.assertRaises(AssertionError):
            CraftingPolicy().assert_settled()

    def test_the_knobs_can_be_flipped(self):
        policy = CraftingPolicy()
        policy.import_when_occupied = IMPORT_KEEP
        self.assertEqual(IMPORT_KEEP, policy.describe()["import_when_occupied"])


class TestPlanImport(unittest.TestCase):

    def test_an_ordered_recipe_is_laid_out_from_the_top_left(self):
        plan = plan_import(normalize_recipe(CRAFTING_TABLE))
        self.assertEqual([0, 1, 3, 4], [slot for slot, _alts, _n in plan])
        for _slot, alternatives, count in plan:
            self.assertEqual(1, len(alternatives))
            self.assertEqual(u"minecraft:oak_planks", alternatives[0].name)
            self.assertEqual(1, count)

    def test_a_shifted_recipe_still_plans_the_canonical_layout(self):
        # `plan_import` 给的是「按左上角铺开」的那一套，与玩家实际摆在哪儿
        # 无关——导入是按配方重摆，不是按他现在的摆法补。
        plan = plan_import(normalize_recipe(STICK_FROM_PLANKS))
        self.assertEqual([0, 3], [slot for slot, _alts, _n in plan])

    def test_a_symbol_keeps_every_alternative(self):
        plan = plan_import(normalize_recipe(STICK_FROM_PLANKS))
        alternatives = plan[0][1]
        self.assertEqual(2, len(alternatives))
        names = set(item.name for item in alternatives)
        self.assertEqual(set([u"minecraft:oak_planks", u"minecraft:birch_planks"]),
                         names)

    def test_a_shapeless_recipe_takes_successive_slots(self):
        plan = plan_import(normalize_recipe(BOOK))
        self.assertEqual([0, 1], [slot for slot, _alts, _n in plan])
        self.assertEqual(3, plan[0][2])   # 三张纸
        self.assertEqual(1, plan[1][2])   # 一张皮革

    def test_untouched_cells_are_absent_from_the_plan(self):
        # 「配方本来没有原料的位置保持为空」：计划里不出现，调用方就不会去动它。
        plan = plan_import(normalize_recipe(STICK_FROM_PLANKS))
        self.assertNotIn(1, [slot for slot, _alts, _n in plan])

    def test_the_per_slot_count_can_be_overridden(self):
        plan = plan_import(normalize_recipe(CRAFTING_TABLE), per_slot=5)
        for _slot, _alts, count in plan:
            self.assertEqual(5, count)


class TestPlanRefill(unittest.TestCase):

    def setUp(self):
        self.recipe = normalize_recipe(STICK_FROM_PLANKS)

    def test_only_the_slots_actually_used_are_refilled(self):
        cells = [None] * SLOT_COUNT
        cells[0] = ("minecraft:oak_planks", 0, 1)
        cells[3] = ("minecraft:birch_planks", 0, 1)
        plan = plan_refill(self.recipe, tuple(cells))
        self.assertEqual([0, 3], [slot for slot, _alts, _n in plan])

    def test_it_follows_the_players_actual_placement_not_the_canonical_one(self):
        # 有序配方允许整体平移。补料要补的是**他摆的那两格**，
        # 不是配方文档里那两格。
        cells = [None] * SLOT_COUNT
        cells[4] = ("minecraft:oak_planks", 0, 1)
        cells[7] = ("minecraft:oak_planks", 0, 1)
        plan = plan_refill(self.recipe, tuple(cells))
        self.assertEqual([4, 7], [slot for slot, _alts, _n in plan])

    def test_an_empty_bench_plans_nothing(self):
        self.assertEqual((), plan_refill(self.recipe, (None,) * SLOT_COUNT))

    def test_the_amount_is_the_policy_value_not_the_import_value(self):
        cells = [None] * SLOT_COUNT
        cells[0] = ("minecraft:oak_planks", 0, 1)
        cells[3] = ("minecraft:oak_planks", 0, 1)
        plan = plan_refill(self.recipe, tuple(cells), per_slot=1)
        for _slot, _alts, count in plan:
            self.assertEqual(1, count)

    def test_a_shapeless_recipe_refills_by_what_is_sitting_there(self):
        recipe = normalize_recipe(BOOK)
        cells = [None] * SLOT_COUNT
        cells[2] = ("minecraft:paper", 0, 1)
        cells[5] = ("minecraft:leather", 0, 1)
        plan = plan_refill(recipe, tuple(cells))
        self.assertEqual([2, 5], [slot for slot, _alts, _n in plan])


if __name__ == "__main__":
    unittest.main()
