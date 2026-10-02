# coding=utf-8
"""工作台预设配方：规范化与摆位匹配（11-04）。

**全部离线。** `common/crafting_recipe.py` 不 import 引擎，因此这里只要
喂原始字典就够了。

贯穿全篇的判据只有一条：**夹具是实测打出来的，不是照文档写的。**
下面前四个夹具逐字来自 2026-10-01 在运行中的游戏里对
`GetRecipesByResult(..., "crafting_table", ...)` 的打印，其余是按实测形状
构造的边界。因此这些用例钉住的是**引擎的实际输出**，而不是我们对它的想象——
一旦引擎换了形状，红的是这里，不是游戏里某个玩家点不动的按钮。

第二个判据是**保守那侧**：匹配不到只是「这次点不动」，匹配错了是「悄悄
吃掉一个不在配方里的物品」。凡是两种读法都说得通的，用例都钉在保守那侧。
"""

import unittest

import testsupport  # noqa: F401 - 导入即完成 sys.path 设置

from endernet.common.crafting_recipe import (
    BAD_SHAPE,
    EMPTY_PATTERN,
    EMPTY_SYMBOL_KEY,
    NO_RESULT,
    PATTERN_TOO_BIG,
    RECIPE_TAG,
    SLOT_COUNT,
    UNKNOWN_SYMBOL,
    PresetRecipe,
    RecipeError,
    RecipeIngredient,
    RecipePolicy,
    find_match,
    find_matches,
    match_ordered,
    match_shapeless,
    normalize_recipe,
)


# -- 实测夹具 --------------------------------------------------------------

#: `GetRecipesByResult("minecraft:golden_apple", "crafting_table", 0, -1)` 的第 0 条。
GOLDEN_APPLE = {
    "pattern": ["AAA", "ABA", "AAA"],
    "key": {"A": {"item": "minecraft:gold_ingot"},
            "B": {"item": "minecraft:apple"}},
    "result": [{"item": "minecraft:golden_apple", "data": 0}],
}

#: `... ("minecraft:stick", ...)` 的第 0 条：`item` 是**列表**（标签展开）。
STICK_FROM_PLANKS = {
    "pattern": ["A", "A"],
    "key": {"A": {"item": [
        "minecraft:acacia_planks", "minecraft:bamboo_planks",
        "minecraft:birch_planks", "minecraft:cherry_planks",
        "minecraft:crimson_planks", "minecraft:dark_oak_planks",
        "minecraft:jungle_planks", "minecraft:mangrove_planks",
        "minecraft:oak_planks", "minecraft:pale_oak_planks",
        "minecraft:spruce_planks", "minecraft:warped_planks",
    ]}},
    "result": [{"count": 4, "item": "minecraft:stick", "data": 0}],
}

#: `... ("minecraft:book", ...)` 的第 0 条：**无序**，且 `count: 3`。
BOOK = {
    "ingredients": [{"count": 3, "item": "minecraft:paper", "data": 0},
                    {"item": "minecraft:leather", "data": 0}],
    "result": [{"item": "minecraft:book", "data": 0}],
}

#: `... ("minecraft:crafting_table", ...)` 的第 0 条：**result 没有 count**。
CRAFTING_TABLE = {
    "pattern": ["AA", "AA"],
    "key": {"A": {"item": "minecraft:oak_planks"}},
    "result": [{"item": "minecraft:crafting_table", "data": 0}],
}

#: `... ("minecraft:oak_planks", ...)` 的第 0 条：1×1，**key 没有 data**。
PLANKS_FROM_LOG = {
    "pattern": ["A"],
    "key": {"A": {"item": "minecraft:oak_log"}},
    "result": [{"count": 4, "item": "minecraft:oak_planks", "data": 0}],
}

#: `... ("minecraft:torch", ...)` 的第 0 条：两个符号，A 是列表、B 是单串。
TORCH = {
    "pattern": ["A", "B"],
    "key": {"A": {"item": ["minecraft:charcoal", "minecraft:coal"]},
            "B": {"item": "minecraft:stick"}},
    "result": [{"count": 4, "item": "minecraft:torch", "data": 0}],
}


def cells(*entries):
    """按 9 格摆位构造 `cells`。

    传 9 项，每项 `None` 或 `(名字, aux, 数量)`；也接受 `(名字, aux)`（数量按 1）。
    """
    if len(entries) != SLOT_COUNT:
        raise AssertionError("摆位必须给满 9 格，得到 %d" % (len(entries),))
    result = []
    for entry in entries:
        if entry is None or len(entry) == 3:
            result.append(entry)
        else:
            result.append((entry[0], entry[1], 1))
    return tuple(result)


def empty():
    return (None,) * SLOT_COUNT


class TestNormalizeShaped(unittest.TestCase):

    def test_the_real_tag_is_not_the_obvious_one(self):
        # 这个值不是读来的，是量出来的：第一版按常识猜了 "crafting"，
        # 实测返回空表，而且**不报错**。
        self.assertEqual("crafting_table", RECIPE_TAG)
        self.assertNotEqual("crafting", RECIPE_TAG)

    def test_golden_apple_becomes_three_rows_and_two_symbols(self):
        recipe = normalize_recipe(GOLDEN_APPLE)
        self.assertTrue(recipe.ordered)
        self.assertEqual(("AAA", "ABA", "AAA"), recipe.rows)
        self.assertEqual(3, recipe.height)
        self.assertEqual(3, recipe.width)
        self.assertEqual(set(["A", "B"]), set(recipe.symbols))

    def test_a_result_without_count_is_one(self):
        recipe = normalize_recipe(CRAFTING_TABLE)
        self.assertEqual(1, recipe.output_preview().count)

    def test_a_result_with_count_keeps_it(self):
        recipe = normalize_recipe(STICK_FROM_PLANKS)
        self.assertEqual(4, recipe.output_preview().count)
        self.assertEqual(u"minecraft:stick", recipe.output_preview().name)

    def test_a_tag_expanded_symbol_keeps_every_alternative(self):
        recipe = normalize_recipe(STICK_FROM_PLANKS)
        self.assertEqual(12, len(recipe.symbols["A"]))
        names = set(item.name for item in recipe.symbols["A"])
        self.assertIn(u"minecraft:oak_planks", names)
        self.assertIn(u"minecraft:warped_planks", names)

    def test_a_bare_string_and_a_list_are_both_accepted(self):
        recipe = normalize_recipe(TORCH)
        self.assertEqual(2, len(recipe.symbols["A"]))
        self.assertEqual(1, len(recipe.symbols["B"]))

    def test_required_slots_counts_only_non_empty_pattern_cells(self):
        self.assertEqual(9, normalize_recipe(GOLDEN_APPLE).required_slots())
        self.assertEqual(2, normalize_recipe(STICK_FROM_PLANKS).required_slots())
        self.assertEqual(4, normalize_recipe(CRAFTING_TABLE).required_slots())

    def test_the_preview_lays_the_pattern_out_from_the_top_left(self):
        recipe = normalize_recipe(GOLDEN_APPLE)
        layout = recipe.slot_layout()
        self.assertEqual(SLOT_COUNT, len(layout))
        for index in range(SLOT_COUNT):
            self.assertIsNotNone(layout[index], "第 %d 格应当有预览" % (index,))

    def test_the_preview_leaves_untouched_cells_empty(self):
        # 1×1 的配方只该点亮第 0 格。
        layout = normalize_recipe(PLANKS_FROM_LOG).slot_layout()
        self.assertIsNotNone(layout[0])
        for index in range(1, SLOT_COUNT):
            self.assertIsNone(layout[index], "第 %d 格不该有预览" % (index,))


class TestNormalizeShapeless(unittest.TestCase):

    def test_book_is_shapeless_with_a_counted_ingredient(self):
        recipe = normalize_recipe(BOOK)
        self.assertFalse(recipe.ordered)
        self.assertEqual((), recipe.rows)
        self.assertEqual(2, len(recipe.ingredients))
        paper = [item for item in recipe.ingredients
                 if item.name == u"minecraft:paper"]
        self.assertEqual(1, len(paper))
        self.assertEqual(3, paper[0].count)

    def test_a_shapeless_recipe_needs_no_pattern(self):
        self.assertNotIn("pattern", BOOK)
        self.assertFalse(normalize_recipe(BOOK).ordered)


class TestNormalizeRejects(unittest.TestCase):

    def test_neither_shape_is_refused_not_guessed(self):
        with self.assertRaises(RecipeError) as caught:
            normalize_recipe({"result": [{"item": "minecraft:stone"}]})
        self.assertEqual(BAD_SHAPE, caught.exception.code)

    def test_a_non_dict_is_refused(self):
        for raw in (None, [], "x", 7):
            with self.assertRaises(RecipeError) as caught:
                normalize_recipe(raw)
            self.assertEqual(BAD_SHAPE, caught.exception.code)

    def test_a_pattern_wider_than_the_bench_is_refused(self):
        raw = dict(GOLDEN_APPLE)
        raw["pattern"] = ["AAAA"]
        raw["key"] = {"A": {"item": "minecraft:gold_ingot"}}
        with self.assertRaises(RecipeError) as caught:
            normalize_recipe(raw)
        self.assertEqual(PATTERN_TOO_BIG, caught.exception.code)

    def test_a_pattern_taller_than_the_bench_is_refused(self):
        raw = dict(STICK_FROM_PLANKS)
        raw["pattern"] = ["A", "A", "A", "A"]
        with self.assertRaises(RecipeError) as caught:
            normalize_recipe(raw)
        self.assertEqual(PATTERN_TOO_BIG, caught.exception.code)

    def test_a_symbol_missing_from_key_is_refused(self):
        raw = dict(GOLDEN_APPLE)
        raw["key"] = {"A": {"item": "minecraft:gold_ingot"}}
        with self.assertRaises(RecipeError) as caught:
            normalize_recipe(raw)
        self.assertEqual(UNKNOWN_SYMBOL, caught.exception.code)

    def test_an_all_blank_pattern_is_refused(self):
        raw = {"pattern": ["   ", "   "], "key": {},
               "result": [{"item": "minecraft:stone"}]}
        with self.assertRaises(RecipeError) as caught:
            normalize_recipe(raw)
        self.assertEqual(EMPTY_PATTERN, caught.exception.code)

    def test_no_result_is_refused(self):
        raw = dict(GOLDEN_APPLE)
        raw["result"] = []
        with self.assertRaises(RecipeError) as caught:
            normalize_recipe(raw)
        self.assertEqual(NO_RESULT, caught.exception.code)

    def test_a_symbol_with_no_alternatives_is_refused(self):
        raw = {"pattern": ["A"], "key": {"A": {"item": []}},
               "result": [{"item": "minecraft:stone"}]}
        with self.assertRaises(RecipeError) as caught:
            normalize_recipe(raw)
        self.assertEqual(EMPTY_SYMBOL_KEY, caught.exception.code)

    def test_a_non_integer_count_is_refused(self):
        raw = {"ingredients": [{"count": "3", "item": "minecraft:paper"}],
               "result": [{"item": "minecraft:book"}]}
        with self.assertRaises(RecipeError) as caught:
            normalize_recipe(raw)
        self.assertEqual(BAD_SHAPE, caught.exception.code)


class TestMatchOrdered(unittest.TestCase):

    def setUp(self):
        self.recipe = normalize_recipe(CRAFTING_TABLE)  # 2×2 of oak planks

    def test_the_recipe_matches_in_its_top_left_corner(self):
        found = match_ordered(self.recipe, cells(
            ("minecraft:oak_planks", 0, 1), ("minecraft:oak_planks", 0, 1), None,
            ("minecraft:oak_planks", 0, 1), ("minecraft:oak_planks", 0, 1), None,
            None, None, None,
        ))
        self.assertIsNotNone(found)
        self.assertEqual((0, 1, 3, 4), found.slots())

    def test_the_recipe_matches_shifted_to_another_corner(self):
        # 原版里 2×2 的配方摆在右下角照样能合成：pattern 是最小包围盒，
        # 不是绝对坐标。只认左上角会把这一半的合法摆法判死。
        found = match_ordered(self.recipe, cells(
            None, None, None,
            None, ("minecraft:oak_planks", 0, 1), ("minecraft:oak_planks", 0, 1),
            None, ("minecraft:oak_planks", 0, 1), ("minecraft:oak_planks", 0, 1),
        ))
        self.assertIsNotNone(found)
        self.assertEqual((4, 5, 7, 8), found.slots())

    def test_an_extra_item_inside_the_bounding_box_blocks_the_match(self):
        found = match_ordered(self.recipe, cells(
            ("minecraft:oak_planks", 0, 1), ("minecraft:oak_planks", 0, 1), None,
            ("minecraft:oak_planks", 0, 1), ("minecraft:stone", 0, 1), None,
            None, None, None,
        ))
        self.assertIsNone(found)

    def test_an_item_outside_the_bounding_box_blocks_the_match(self):
        # 原版拿整张工作台去比配方，图案矩形之外按「空」这一味配料逐格比。
        # 曾经这里只查矩形那一块，于是多摆的那件**参与匹配却被无视**：
        # 预览亮着、点下去照做，多出来的物品既没进产物也没被吃掉
        # （User 2026-10-02 报告的就是这个）。
        found = match_ordered(self.recipe, cells(
            ("minecraft:oak_planks", 0, 1), ("minecraft:oak_planks", 0, 1), None,
            ("minecraft:oak_planks", 0, 1), ("minecraft:oak_planks", 0, 1), None,
            ("minecraft:stone", 0, 1), None, None,
        ))
        self.assertIsNone(found)

    def test_an_item_outside_the_bounding_box_blocks_every_offset(self):
        # 平移要枚举全部偏移，但「矩形之外必须空」对**每一个**候选偏移都成立：
        # 把多出来的那件挪到另一处不会突然匹配得上。
        for slot in range(SLOT_COUNT):
            if slot in (0, 1, 3, 4):
                continue
            layout = [None] * SLOT_COUNT
            for filled in (0, 1, 3, 4):
                layout[filled] = ("minecraft:oak_planks", 0, 1)
            layout[slot] = ("minecraft:stone", 0, 1)
            self.assertIsNone(
                match_ordered(self.recipe, tuple(layout)),
                "第 %d 格多摆一件却仍然匹配" % (slot,),
            )

    def test_a_wrong_ingredient_blocks_the_match(self):
        found = match_ordered(self.recipe, cells(
            ("minecraft:oak_planks", 0, 1), ("minecraft:stone", 0, 1), None,
            ("minecraft:oak_planks", 0, 1), ("minecraft:oak_planks", 0, 1), None,
            None, None, None,
        ))
        self.assertIsNone(found)

    def test_too_few_items_blocks_the_match(self):
        found = match_ordered(self.recipe, cells(
            ("minecraft:oak_planks", 0, 1), None, None,
            None, None, None,
            None, None, None,
        ))
        self.assertIsNone(found)

    def test_a_tag_alternative_matches_any_of_its_members(self):
        recipe = normalize_recipe(STICK_FROM_PLANKS)
        found = match_ordered(recipe, cells(
            ("minecraft:warped_planks", 0, 1), None, None,
            ("minecraft:acacia_planks", 0, 1), None, None,
            None, None, None,
        ))
        self.assertIsNotNone(found)

    def test_a_one_by_one_recipe_matches_anywhere(self):
        recipe = normalize_recipe(PLANKS_FROM_LOG)
        for slot in range(SLOT_COUNT):
            layout = [None] * SLOT_COUNT
            layout[slot] = ("minecraft:oak_log", 0, 1)
            self.assertIsNotNone(
                match_ordered(recipe, tuple(layout)),
                "第 %d 格应当能合成木板" % (slot,),
            )


class TestMatchShapeless(unittest.TestCase):

    def setUp(self):
        self.recipe = normalize_recipe(BOOK)  # 3 paper + 1 leather

    def test_the_stack_in_one_slot_is_enough(self):
        found = match_shapeless(self.recipe, cells(
            ("minecraft:paper", 0, 3), ("minecraft:leather", 0, 1), None,
            None, None, None, None, None, None,
        ))
        self.assertIsNotNone(found)
        self.assertEqual(((0, 3), (1, 1)), found.consumed)

    def test_position_does_not_matter(self):
        found = match_shapeless(self.recipe, cells(
            None, None, ("minecraft:leather", 0, 1),
            None, None, None,
            ("minecraft:paper", 0, 3), None, None,
        ))
        self.assertIsNotNone(found)
        self.assertEqual(((2, 1), (6, 3)), found.consumed)

    def test_two_loose_sheets_are_not_three(self):
        # 只看名字会让这一格「够」——预览亮着，点下去却做不出来。
        found = match_shapeless(self.recipe, cells(
            ("minecraft:paper", 0, 2), ("minecraft:leather", 0, 1), None,
            None, None, None, None, None, None,
        ))
        self.assertIsNone(found)

    def test_paper_may_be_split_across_slots_is_not_supported(self):
        # 一条配料要 3 张纸，而工作台里那 3 张是堆在一个格子里的；
        # 分放在三格不是原版语义，这里明确判不匹配（保守那侧）。
        found = match_shapeless(self.recipe, cells(
            ("minecraft:paper", 0, 1), ("minecraft:paper", 0, 1),
            ("minecraft:paper", 0, 1), ("minecraft:leather", 0, 1),
            None, None, None, None, None,
        ))
        self.assertIsNone(found)

    def test_a_missing_ingredient_blocks_the_match(self):
        found = match_shapeless(self.recipe, cells(
            ("minecraft:paper", 0, 3), None, None,
            None, None, None, None, None, None,
        ))
        self.assertIsNone(found)

    def test_an_extra_item_blocks_the_match(self):
        # 配料都摆齐了，但旁边多出一件。原版比的是「非空格数 == 配料条目数」，
        # 多出来的那件让整条配方不成立——它既不会进产物，也不该被无视。
        found = match_shapeless(self.recipe, cells(
            ("minecraft:paper", 0, 3), ("minecraft:leather", 0, 1),
            ("minecraft:diamond", 0, 1),
            None, None, None, None, None, None,
        ))
        self.assertIsNone(found)

    def test_an_extra_item_blocks_the_match_wherever_it_sits(self):
        # 无序配方不看位置，但「不许有剩货」对每一格都成立。
        for slot in range(SLOT_COUNT):
            if slot in (0, 1):
                continue
            layout = [None] * SLOT_COUNT
            layout[0] = ("minecraft:paper", 0, 3)
            layout[1] = ("minecraft:leather", 0, 1)
            layout[slot] = ("minecraft:diamond", 0, 1)
            self.assertIsNone(
                match_shapeless(self.recipe, tuple(layout)),
                "第 %d 格多摆一件却仍然匹配" % (slot,),
            )

    def test_a_bigger_stack_than_asked_for_is_not_extra(self):
        # 「多摆一件」按**格子**算，不按数量算：一格里有 5 张纸而配方要 3 张，
        # 仍然是「一格纸」，剩下 2 张留在格子里，不是多出来的第二件配料。
        found = match_shapeless(self.recipe, cells(
            ("minecraft:paper", 0, 5), ("minecraft:leather", 0, 1),
            None, None, None, None, None, None, None,
        ))
        self.assertIsNotNone(found)
        self.assertEqual(((0, 3), (1, 1)), found.consumed)


class TestAuxPolicy(unittest.TestCase):

    def setUp(self):
        self.recipe = normalize_recipe(PLANKS_FROM_LOG)  # key 没有 data

    def test_no_data_means_zero_by_default(self):
        # 实测里凡是带 data 的一律是 0，缺席的样本 aux 也是 0，两种读法
        # 在实测样本上一致。默认取严格那侧：只可能少匹配，不会误吃。
        ingredient = self.recipe.symbols["A"][0]
        self.assertEqual(0, ingredient.aux)
        self.assertTrue(ingredient.matches(u"minecraft:oak_log", 0))
        self.assertFalse(ingredient.matches(u"minecraft:oak_log", 3))

    def test_the_knob_can_widen_it_to_any_aux(self):
        policy = RecipePolicy()
        policy.aux_wildcard_when_absent = True
        ingredient = normalize_recipe(PLANKS_FROM_LOG, policy).symbols["A"][0]
        self.assertIsNone(ingredient.aux)
        self.assertTrue(ingredient.matches(u"minecraft:oak_log", 3))

    def test_an_explicit_data_is_always_honoured(self):
        raw = {"pattern": ["A"], "key": {"A": {"item": "minecraft:wool", "data": 4}},
               "result": [{"item": "minecraft:white_wool"}]}
        ingredient = normalize_recipe(raw).symbols["A"][0]
        self.assertEqual(4, ingredient.aux)
        self.assertTrue(ingredient.matches(u"minecraft:wool", 4))
        self.assertFalse(ingredient.matches(u"minecraft:wool", 0))

    def test_the_default_policy_says_what_it_is(self):
        described = RecipePolicy().describe()
        self.assertIn("aux_wildcard_when_absent", described)
        self.assertIn("deliver_all_results", described)


class TestFindMatches(unittest.TestCase):

    def test_all_matching_recipes_are_returned_not_just_the_first(self):
        # 实测里 minecraft:white_wool 同时有「四根线」与若干条染色配方。
        # 摆进去到底会做出哪一个是要被看见的事实。
        thread = normalize_recipe({
            "pattern": ["AA", "AA"], "key": {"A": {"item": "minecraft:string"}},
            "result": [{"item": "minecraft:white_wool"}],
        })
        recipes = [normalize_recipe(CRAFTING_TABLE), thread]
        found = find_matches(recipes, cells(
            ("minecraft:string", 0, 1), ("minecraft:string", 0, 1), None,
            ("minecraft:string", 0, 1), ("minecraft:string", 0, 1), None,
            None, None, None,
        ))
        self.assertEqual(1, len(found))
        self.assertIs(thread, found[0].recipe)

    def test_find_match_returns_the_first(self):
        recipes = [normalize_recipe(CRAFTING_TABLE), normalize_recipe(STICK_FROM_PLANKS)]
        found = find_match(recipes, cells(
            ("minecraft:oak_planks", 0, 1), ("minecraft:oak_planks", 0, 1), None,
            ("minecraft:oak_planks", 0, 1), ("minecraft:oak_planks", 0, 1), None,
            None, None, None,
        ))
        self.assertIs(recipes[0], found.recipe)

    def test_an_empty_bench_matches_nothing(self):
        recipes = [normalize_recipe(CRAFTING_TABLE), normalize_recipe(BOOK)]
        self.assertEqual([], find_matches(recipes, empty()))
        self.assertIsNone(find_match(recipes, empty()))


class TestDigestAndProjection(unittest.TestCase):

    def test_the_digest_is_stable_for_the_same_content(self):
        self.assertEqual(normalize_recipe(BOOK).digest(), normalize_recipe(BOOK).digest())

    def test_the_digest_is_short_enough_to_send_per_candidate(self):
        # 整段指纹文本有八百多个字符（`minecraft:stick` 带着 12 种木板
        # 的候选表），每条候选都要发一次。8 个十六进制字符够用。
        for raw in (GOLDEN_APPLE, STICK_FROM_PLANKS, BOOK, TORCH):
            digest = normalize_recipe(raw).digest()
            self.assertEqual(8, len(digest), digest)
            int(digest, 16)  # 必须是合法十六进制

    def test_the_digest_changes_when_the_content_changes(self):
        other = dict(BOOK)
        other["result"] = [{"count": 2, "item": "minecraft:book"}]
        self.assertNotEqual(normalize_recipe(BOOK).digest(),
                            normalize_recipe(other).digest())

    def test_two_recipes_of_the_same_output_differ_by_digest(self):
        # 这就是「多配方选择」能工作的前提：客户端回传的是指纹，不是序号。
        a = normalize_recipe({
            "pattern": ["A"], "key": {"A": {"item": "minecraft:oak_log"}},
            "result": [{"count": 4, "item": "minecraft:oak_planks"}],
        })
        b = normalize_recipe({
            "pattern": ["A"], "key": {"A": {"item": "minecraft:birch_log"}},
            "result": [{"count": 4, "item": "minecraft:oak_planks"}],
        })
        self.assertNotEqual(a.digest(), b.digest())

    def test_the_projection_carries_the_digest_and_the_preview(self):
        payload = normalize_recipe(BOOK).to_dict()
        self.assertEqual(normalize_recipe(BOOK).digest(), payload["digest"])
        self.assertFalse(payload["ordered"])
        self.assertEqual(SLOT_COUNT, len(payload["slots"]))
        self.assertEqual(1, len(payload["outputs"]))
        self.assertEqual(u"minecraft:book", payload["outputs"][0]["name"])

    def test_the_projection_is_json_serialisable(self):
        import json
        for raw in (GOLDEN_APPLE, STICK_FROM_PLANKS, BOOK, TORCH, PLANKS_FROM_LOG):
            json.dumps(normalize_recipe(raw).to_dict())

    def test_the_projection_of_an_ordered_recipe_keeps_the_rows(self):
        payload = normalize_recipe(GOLDEN_APPLE).to_dict()
        self.assertEqual(["AAA", "ABA", "AAA"], payload["rows"])


class TestIngredient(unittest.TestCase):

    def test_equality_and_hash_follow_the_content(self):
        a = RecipeIngredient("minecraft:paper", 0, 3)
        b = RecipeIngredient("minecraft:paper", 0, 3)
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))
        self.assertNotEqual(a, RecipeIngredient("minecraft:paper", 0, 1))

    def test_a_wildcard_matches_any_aux(self):
        item = RecipeIngredient("minecraft:wool", None, 1)
        self.assertTrue(item.matches("minecraft:wool", 7))
        self.assertFalse(item.matches("minecraft:string", 7))

    def test_from_dict_round_trips(self):
        item = RecipeIngredient("minecraft:paper", None, 2)
        self.assertEqual(item, RecipeIngredient.from_dict(item.to_dict()))


class TestEngineRecipeSource(unittest.TestCase):
    """适配器：**引擎调用是注入的**，因此这一整类都不进游戏也能跑。

    这一层要钉住的不是「引擎会返回什么」（那是夹具的活），而是
    **我们怎么对待引擎的返回**：标签对不对、脏配方跳过还是炸、
    查不到算不算错误、缓存什么时候清。
    """

    def setUp(self):
        from endernet.server.adapters.recipes import EngineRecipeSource
        self.EngineRecipeSource = EngineRecipeSource
        self.calls = []

    def source_returning(self, payload):
        def fetch(name, tag, aux, max_results):
            self.calls.append((name, tag, aux, max_results))
            return payload
        return self.EngineRecipeSource(fetch=fetch)

    def test_it_asks_the_tag_that_actually_works(self):
        from endernet.common.resource_key import ResourceKey
        source = self.source_returning([GOLDEN_APPLE])
        source.recipes_for(ResourceKey.item("minecraft:golden_apple"))
        name, tag, aux, _max = self.calls[0]
        self.assertEqual(u"minecraft:golden_apple", name)
        self.assertEqual("crafting_table", tag)
        self.assertEqual(0, aux)

    def test_the_output_aux_is_passed_through(self):
        from endernet.common.resource_key import ResourceKey
        source = self.source_returning([])
        source.recipes_for(ResourceKey.item("minecraft:wool", aux=4))
        self.assertEqual(4, self.calls[0][2])

    def test_an_unknown_item_is_an_empty_list_not_an_error(self):
        # 实测：查不存在的物品返回 []，不抛。空表是引擎的回答。
        from endernet.common.resource_key import ResourceKey
        source = self.source_returning([])
        self.assertEqual([], source.recipes_for(ResourceKey.item("endernet:nope")))

    def test_a_non_list_return_is_not_read_as_no_recipes(self):
        # 实测：引擎在参数类型不对时返回 `False`。若把它读成空表，
        # 「这条查询整个坏了」与「这个产物确实没有配方」就分不开了。
        from endernet.common.resource_key import ResourceKey
        source = self.source_returning(False)
        with self.assertRaises(TypeError):
            source.recipes_for(ResourceKey.item("minecraft:stone"))

    def test_a_none_return_is_not_read_as_no_recipes_either(self):
        from endernet.common.resource_key import ResourceKey
        source = self.source_returning(None)
        with self.assertRaises(TypeError):
            source.recipes_for(ResourceKey.item("minecraft:stone"))

    def test_a_dirty_recipe_is_skipped_and_remembered(self):
        from endernet.common.resource_key import ResourceKey
        source = self.source_returning([{"nonsense": True}, GOLDEN_APPLE])
        recipes = source.recipes_for(ResourceKey.item("minecraft:golden_apple"))
        self.assertEqual(1, len(recipes))
        self.assertEqual(1, source.stats["skipped"])
        self.assertEqual(1, len(source.skipped))
        self.assertEqual(u"minecraft:golden_apple", source.skipped[0][0])
        self.assertEqual(BAD_SHAPE, source.skipped[0][1])

    def test_the_engine_dict_is_kept_for_diagnosis(self):
        from endernet.common.resource_key import ResourceKey
        raw = {"nonsense": True}
        source = self.source_returning([raw])
        source.recipes_for(ResourceKey.item("minecraft:stone"))
        self.assertIs(raw, source.skipped[0][2])

    def test_only_the_last_few_skips_are_kept(self):
        from endernet.common.resource_key import ResourceKey
        source = self.source_returning([{"nonsense": True}] * 20)
        source.recipes_for(ResourceKey.item("minecraft:stone"))
        self.assertEqual(20, source.stats["skipped"])
        self.assertLessEqual(len(source.skipped), 8)

    def test_the_same_output_is_asked_once(self):
        from endernet.common.resource_key import ResourceKey
        source = self.source_returning([GOLDEN_APPLE])
        key = ResourceKey.item("minecraft:golden_apple")
        source.recipes_for(key)
        source.recipes_for(key)
        self.assertEqual(1, len(self.calls))
        self.assertEqual(1, source.stats["cache_hits"])

    def test_forget_clears_the_cache(self):
        from endernet.common.resource_key import ResourceKey
        source = self.source_returning([GOLDEN_APPLE])
        key = ResourceKey.item("minecraft:golden_apple")
        source.recipes_for(key)
        source.forget()
        source.recipes_for(key)
        self.assertEqual(2, len(self.calls))
        # 清掉之后又查了一次，因此是「没有命中」，不是「没有再查」。
        self.assertEqual(0, source.stats["cache_hits"])

    def test_two_aux_values_are_two_cache_entries(self):
        from endernet.common.resource_key import ResourceKey
        source = self.source_returning([])
        source.recipes_for(ResourceKey.item("minecraft:wool", aux=0))
        source.recipes_for(ResourceKey.item("minecraft:wool", aux=4))
        self.assertEqual(2, len(self.calls))

    def test_describe_reports_what_it_did(self):
        from endernet.common.resource_key import ResourceKey
        source = self.source_returning([{"nonsense": True}, GOLDEN_APPLE])
        source.recipes_for(ResourceKey.item("minecraft:golden_apple"))
        report = source.describe()
        self.assertTrue(report["available"])
        self.assertEqual(1, report["results"])
        self.assertEqual([{"name": u"minecraft:golden_apple", "code": BAD_SHAPE}],
                         report["last_skipped"])
        self.assertIn("aux_wildcard_when_absent", report["policy"])

    def test_the_name_is_encoded_before_it_reaches_the_engine(self):
        # 实测：`GetRecipesByResult` 收到 unicode 时返回 False，整个查询
        # 静默失败；而 `ResourceKey.name` 恰恰是 unicode。
        # **这条只对量过的那个接口成立**——`GetItemBasicInfo` 实测是
        # 接受 unicode 的，所以这不是「引擎一律拒 unicode」。
        from endernet.server.adapters.engine_text import engine_text
        import sys
        name = u"minecraft:stick"
        if sys.version_info[0] < 3:
            self.assertIsInstance(engine_text(name), str)
            self.assertNotIsInstance(engine_text(name), unicode)  # noqa: F821
        else:
            self.assertIsInstance(engine_text(name), bytes)
        self.assertEqual(b"minecraft:stick", engine_text(name))
        # 已经是字节串的原样返回，不再编一次。
        self.assertEqual(b"minecraft:stick", engine_text(b"minecraft:stick"))

    def test_a_unicode_name_survives_the_round_trip(self):
        from endernet.server.adapters.engine_text import engine_text
        self.assertEqual(u"minecraft:stick".encode("utf-8"),
                         engine_text(u"minecraft:stick"))

    def test_the_module_stays_importable_without_the_engine(self):
        # 模块顶层不许碰 tooldelta：碰了整个测试目录都跑不起来。
        import sys
        self.assertNotIn("tooldelta", sys.modules)
        import endernet.server.adapters.recipes as module
        self.assertNotIn("tooldelta", sys.modules)
        self.assertTrue(callable(module._fetch_from_engine))


if __name__ == "__main__":
    unittest.main()
