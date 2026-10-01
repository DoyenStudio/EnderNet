# coding=utf-8
"""11-09 验收：稳定资源键的长列表模型（条目、顺序、批次、查询代次、选择）。

本文件不装假引擎：`client/ui/item_list_model.py` 与 `inventory_model.py`
一样是纯领域，输入只有已经解码好的 `(资源键, 已存, 已预留)`。这正是它
能被离线钉住的原因——列表跳行、旧结果盖新结果、点错格子这三类问题在实机上
都表现为「有时候」，只有在这里才谈得上逐条断言。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.client.ui.item_list_model import (
    SORT_COUNT,
    SORT_KIND,
    SORT_NAME,
    ItemListModel,
    ListResyncRequired,
    StaleQuery,
)
from endernet.common.resource_key import ResourceKey

STONE = ResourceKey.item("minecraft:stone")
DIRT = ResourceKey.item("minecraft:dirt")
SAND = ResourceKey.item("minecraft:sand")
SWORD = "minecraft:diamond_sword"
SWORD_PLAIN = ResourceKey.item(SWORD, aux=0)
SWORD_BROKEN = ResourceKey.item(SWORD, aux=1)
WATER = ResourceKey.fluid("minecraft:water")


def keys_of(page):
    # type: (tuple) -> list
    return [entry.key for entry in page]


class ModelFixture(unittest.TestCase):
    def setUp(self):
        self.model = ItemListModel()

    def seed(self, version, records):
        # type: (int, list) -> int
        """装一份快照并返回新的查询代次（替换快照会推进代次）。"""
        self.model.replace_snapshot(version, records)
        return self.model.query_generation()

    def order(self):
        # type: () -> list
        return list(self.model.query()[1])

    def ascending(self):
        # type: () -> None
        """把排序钉成「名称升序」并**立刻把顺序建出来**。

        模型的默认方向是**降序**（见 `test_the_default_order_is_descending`），
        因此凡是断言里写着具体顺序、而方向本身不是它要考的东西的用例，都先
        调这一下——不钉的话，「默认改了」会让一批与默认无关的用例跟着红，而
        红的理由读者从用例名里看不出来。

        顺手 `query()` 一次是**故意的**：调用方常在这之后数 `rebuilds()`，
        把这次重排留在计数窗口里会让那个断言莫名其妙地多出一次。也因此它
        会推进查询代次——`seed()` 返回的代次要在这之后再取。
        """
        self.model.set_sort(SORT_NAME, descending=False)
        self.model.query()


class TestEntriesAndOrder(ModelFixture):
    """条目写入、删除与顺序。"""

    def test_upsert_adds_then_updates(self):
        self.assertTrue(self.model.upsert(STONE, 3, 0))
        self.assertFalse(self.model.upsert(STONE, 7, 2))
        self.assertEqual(self.model.count(), 1)
        entry = self.model.query_page(self.model.query_generation(), 0, 1)[0]
        self.assertEqual(entry.stored, 7)
        self.assertEqual(entry.reserved, 2)

    def test_remove_reports_whether_it_was_there(self):
        self.model.upsert(STONE, 3, 0)
        self.assertTrue(self.model.remove(STONE))
        self.assertFalse(self.model.remove(STONE))
        self.assertEqual(self.model.count(), 0)

    def test_remove_takes_the_key_out_of_the_live_order(self):
        """删除**当场**摘掉顺序表里的键。

        推迟到批次结束的话，那个键在中间这段时间是指向已删条目的悬空键，
        一取就 KeyError——而它还要跨好几帧（分片还在路上）。
        """
        self.seed(1, [(STONE, 1, 0), (DIRT, 2, 0)])
        token = self.model.begin_batch(1, 2)
        self.model.remove(STONE)
        self.assertEqual(keys_of(self.model.query_page(self.model.query_generation(), 0, 9)), [DIRT])
        self.model.end_batch(token)
        self.assertEqual(keys_of(self.model.query_page(self.model.query_generation(), 0, 9)), [DIRT])

    def test_default_name_is_the_resource_name(self):
        self.model.upsert(STONE, 1, 0)
        entry = self.model.query_page(self.model.query_generation(), 0, 1)[0]
        self.assertEqual(entry.name, "minecraft:stone")
        self.assertEqual(entry.kind, "item")

    def test_a_resolver_supplies_the_display_name(self):
        model = ItemListModel(name_resolver=lambda key: u"石头")
        model.upsert(STONE, 1, 0)
        entry = model.query_page(model.query_generation(), 0, 1)[0]
        self.assertEqual(entry.name, u"石头")

    def test_search_text_keeps_the_resource_name(self):
        """显示名是本地化的，**资源名一起收**：只收显示名的话，中文界面里
        搜 `stone` 一条都搜不到，而那种「搜了没反应」没人会想到是搜索文本的
        问题。"""
        model = ItemListModel(name_resolver=lambda key: u"石头")
        model.upsert(STONE, 1, 0)
        model.set_filter_text("stone")
        self.assertEqual(model.count(), 1)
        model.set_filter_text(u"石头")
        self.assertEqual(model.count(), 1)
        model.set_filter_text("dirt")
        self.assertEqual(model.count(), 0)

    def test_fluids_are_refused(self):
        """物品视图里出现流体是**发布端**的问题，静默跳过会把那个缺陷藏到
        玩家看见为止。"""
        with self.assertRaises(ValueError):
            self.model.upsert(WATER, 1, 0)
        with self.assertRaises(TypeError):
            self.model.upsert("minecraft:stone", 1, 0)

    def test_counts_are_validated(self):
        for bad in (True, 1.0, "3", None):
            with self.assertRaises(TypeError):
                self.model.upsert(STONE, bad, 0)
        with self.assertRaises(ValueError):
            self.model.upsert(STONE, -1, 0)
        with self.assertRaises(ValueError):
            self.model.upsert(STONE, 1, -1)

    def test_the_sorts_are_total_and_deterministic(self):
        """同名物品（附加值不同）靠资源键指纹兜底，否则先后取决于字典序，
        每次运行都可能不同——列表会在两次进游戏之间自己换位置，且没有任何
        一处代码有错。"""
        for field in (SORT_NAME, SORT_COUNT, SORT_KIND):
            self.model.set_sort(field)
            self.model.upsert(SWORD_BROKEN, 5, 0)
            self.model.upsert(SWORD_PLAIN, 5, 0)
            self.model.upsert(STONE, 5, 0)
            expected = sorted(
                [SWORD_BROKEN, SWORD_PLAIN, STONE], key=lambda key: key.canonical()
            )
            self.assertEqual(self.order(), expected, field)
            self.model.clear()

    def test_the_default_order_is_descending(self):
        """默认**降序**（对齐 `plans/ui/common_terminal_panel.md` 第 79 行、
        `plans/technical_notes/terminal_item_index.md` 第 120 行）。

        单独钉这一条是**故意**的：别的用例只关心「某个排序下的顺序」，方向
        对它们只是前提（用 `ascending()` 钉住了）。默认方向要是没人测，改掉
        它不会有任何东西变红——而那正是玩家一进终端就看得见的那一件事。
        """
        self.seed(1, [(STONE, 1, 0), (DIRT, 2, 0), (SAND, 3, 0)])
        self.assertEqual(
            self.order(),
            sorted(
                [STONE, DIRT, SAND],
                key=lambda key: key.canonical(),
                reverse=True,
            ),
        )

    def test_total_quantity_follows_the_filter(self):
        self.seed(1, [(STONE, 3, 0), (DIRT, 4, 0)])
        self.assertEqual(self.model.total_quantity(), 7)
        self.model.set_filter_text("stone")
        self.assertEqual(self.model.total_quantity(), 3)


class TestSnapshot(ModelFixture):
    """快照替换：整体换、先校验后换、消失的键清选择。"""

    def test_replace_drops_the_old_entries(self):
        self.seed(1, [(STONE, 3, 0), (DIRT, 1, 0)])
        self.model.replace_snapshot(2, [(SAND, 9, 0)])
        self.assertEqual(self.order(), [SAND])
        self.assertEqual(self.model.version(), 2)

    def test_a_bad_record_leaves_the_previous_list_intact(self):
        """中途失败不能留下半份列表：那会让「哪一半是新的」无从判断，
        而列表显示的错误数字看起来与服务端没什么两样。"""
        self.seed(1, [(STONE, 3, 0)])
        with self.assertRaises(ValueError):
            self.model.replace_snapshot(2, [(DIRT, 1, 0), (WATER, 1, 0)])
        self.assertEqual(self.order(), [STONE])
        self.assertEqual(self.model.version(), 1)

    def test_duplicate_keys_are_refused(self):
        """服务端按资源键聚合成一条，出现两条意味着它的索引坏了；静默取
        最后一条会把那个缺陷藏起来。"""
        with self.assertRaises(ValueError):
            self.model.replace_snapshot(1, [(STONE, 3, 0), (STONE, 4, 0)])
        self.assertEqual(self.model.count(), 0)

    def test_replace_advances_the_query_generation(self):
        """整份列表都变了，在途的旧查询（页偏移按旧表算的）必须作废，
        否则它会拿着旧偏移去切新表，切出来的行看着没错、其实错位。"""
        before = self.seed(1, [(STONE, 3, 0)])
        after = self.seed(2, [(STONE, 3, 0)])
        self.assertTrue(self.model.is_stale_generation(before))
        self.assertFalse(self.model.is_stale_generation(after))

    def test_a_key_that_disappears_clears_the_selection(self):
        self.seed(1, [(STONE, 3, 0), (DIRT, 1, 0)])
        self.assertTrue(self.model.select(DIRT))
        result = self.model.replace_snapshot(2, [(STONE, 3, 0)])
        self.assertTrue(result.selection_cleared)
        self.assertIsNone(self.model.selected_key())
        # 选中的键还在时不报「被清掉」——说了的话界面会去重画一个没变的状态。
        self.model.select(STONE)
        result = self.model.replace_snapshot(3, [(STONE, 3, 0)])
        self.assertFalse(result.selection_cleared)
        self.assertEqual(self.model.selected_key(), STONE)

    def test_a_removal_clears_the_selection_right_away(self):
        """不等批次结束：留着它，界面下一帧会拿着一个不存在的键去问数量，
        或者更糟——把这个键写进命令里。"""
        self.seed(1, [(STONE, 3, 0)])
        self.model.select(STONE)
        token = self.model.begin_batch(1, 2)
        self.model.remove(STONE)
        self.assertIsNone(self.model.selected_key())
        self.assertTrue(self.model.end_batch(token).selection_cleared)


class TestBatchMerging(ModelFixture):
    """三条不变量里最容易被改坏的一条：批次与重排。"""

    def test_a_quantity_change_does_not_reorder_under_the_name_sort(self):
        g = self.seed(1, [(STONE, 1, 0), (DIRT, 2, 0), (SAND, 3, 0)])
        self.ascending()
        self.model.set_anchor(SAND, 0)
        rebuilds = self.model.rebuilds()
        token = self.model.begin_batch(self.model.version(), 2)
        self.model.upsert(SAND, 99, 0)
        result = self.model.end_batch(token)
        self.assertFalse(result.reordered)
        self.assertEqual(self.order(), [DIRT, SAND, STONE])
        self.assertEqual(self.model.rebuilds(), rebuilds, "一次都不该重排")
        self.assertEqual(result.anchor_key, SAND)
        self.assertEqual(result.anchor_offset, 1)

    def test_the_count_sort_reorders_at_most_once_per_batch(self):
        self.model.set_sort(SORT_COUNT, descending=True)
        g = self.seed(1, [(STONE, 1, 0), (DIRT, 2, 0), (SAND, 3, 0)])
        del g
        rebuilds = self.model.rebuilds()
        token = self.model.begin_batch(self.model.version(), 2)
        for round_index in range(20):
            self.model.upsert([STONE, DIRT, SAND][round_index % 3], round_index, 0)
            # 批次进行中取一次结果：**不能**因此触发重排。
            self.model.query_page(self.model.query_generation(), 0, 3)
        self.assertEqual(self.model.rebuilds(), rebuilds, "批次进行中重排了")
        # 循环之后 SAND=17、STONE=18、DIRT=19，降序即 [DIRT, STONE, SAND]；
        # 而批次开始前是快照那一版的数量降序 [SAND, DIRT, STONE]。
        result = self.model.end_batch(token)
        self.assertTrue(result.reordered)
        self.assertEqual(self.model.rebuilds(), rebuilds + 1)
        self.assertEqual(self.order(), [DIRT, STONE, SAND])

    def test_a_new_key_is_deferred_to_the_end_of_the_batch(self):
        """新键在任何排序下顺序都会变，但**变化本身也要等批结束**：
        边收包边插行，玩家看到的是表格在抖。"""
        self.seed(1, [(STONE, 1, 0)])
        self.ascending()
        rebuilds = self.model.rebuilds()
        token = self.model.begin_batch(1, 2)
        self.model.upsert(DIRT, 1, 0)
        self.assertEqual(self.model.rebuilds(), rebuilds)
        self.model.end_batch(token)
        self.assertEqual(self.order(), [DIRT, STONE])

    def test_the_first_ever_query_is_not_deferred(self):
        """「顺序还没建过」不是推迟，是「没有」：新会话的第一批增量可能早于
        第一次渲染，这时不建就等于列表空着。"""
        token = self.model.begin_batch(0, 1)
        self.model.upsert(STONE, 1, 0)
        self.assertEqual(self.order(), [STONE])
        self.model.end_batch(token)

    def test_changed_keys_are_sorted_and_include_the_removed(self):
        """键一律按指纹排序：调用方拿它去遍历重画，不排序的话「同一批增量
        画出来的顺序」两次运行可以不同。"""
        self.seed(1, [(STONE, 1, 0), (DIRT, 2, 0), (SAND, 3, 0)])
        token = self.model.begin_batch(1, 2)
        self.model.upsert(SAND, 9, 0)
        self.model.remove(DIRT)
        self.model.upsert(SAND, 10, 0)
        result = self.model.end_batch(token)
        self.assertEqual(list(result.changed_keys), sorted([SAND, DIRT], key=lambda k: k.canonical()))
        self.assertEqual(list(result.removed_keys), [DIRT])
        self.assertEqual(result.version, 2)

    def test_a_same_named_pair_keeps_its_order_across_quantity_changes(self):
        self.model.upsert(SWORD_BROKEN, 1, 0)
        self.model.upsert(SWORD_PLAIN, 1, 0)
        before = self.order()
        token = self.model.begin_batch(0, 1)
        self.model.upsert(SWORD_PLAIN, 64, 0)
        self.model.upsert(SWORD_BROKEN, 64, 0)
        self.model.end_batch(token)
        self.assertEqual(self.order(), before)

    def test_a_gap_in_the_version_chain_asks_for_a_resync(self):
        self.seed(3, [(STONE, 1, 0)])
        with self.assertRaises(ListResyncRequired):
            self.model.begin_batch(2, 3)
        # 拒了之后不该留下一个开着的一半批次。
        token = self.model.begin_batch(3, 4)
        self.model.upsert(STONE, 2, 0)
        self.assertEqual(self.model.end_batch(token).version, 4)

    def test_the_batch_token_is_checked(self):
        self.seed(1, [])
        self.model.begin_batch(1, 2)
        with self.assertRaises(ValueError):
            self.model.end_batch(999)
        with self.assertRaises(ValueError):
            self.model.begin_batch(1, 2)
        self.model.end_batch(1)

    def test_the_anchor_falls_back_to_its_old_row(self):
        """锚点自己没了时退到它原来的位置，越界则贴到末尾：玩家盯着第 3 行，
        第 3 行没了，那就还是「第 3 行那个位置」——把他甩回列表顶部是更糟的
        答案。"""
        self.seed(1, [(STONE, 1, 0), (DIRT, 2, 0), (SAND, 3, 0)])
        self.ascending()
        self.assertEqual(self.order(), [DIRT, SAND, STONE])
        self.model.set_anchor(SAND, 2)
        token = self.model.begin_batch(1, 2)
        self.model.remove(SAND)
        result = self.model.end_batch(token)
        self.assertEqual(result.anchor_key, STONE)
        self.assertEqual(result.anchor_offset, 1, "原位置越界，贴到末尾")

    def test_a_snapshot_reports_the_keys_that_disappeared(self):
        """快照把整份列表换掉，因此消失的行只能从**换之前**那一份里找。
        不报的话，界面会继续画着几行早已不存在的东西。"""
        self.seed(1, [(STONE, 1, 0), (DIRT, 2, 0), (SAND, 3, 0)])
        result = self.model.replace_snapshot(2, [(STONE, 1, 0), (SAND, 3, 0)])
        self.assertEqual(list(result.removed_keys), [DIRT])
        self.assertEqual(
            list(result.changed_keys),
            sorted([STONE, DIRT, SAND], key=lambda key: key.canonical()),
        )
        self.assertTrue(result.reordered)

    def test_the_anchor_reattaches_to_a_live_row(self):
        """锚点落定之后挂到那一行上。挂回原来那个已经消失的键的话，它会
        一直「不存在」，每次重排都要从头算一遍回退位置，而那个位置会随着
        列表缩短一路往前漂。"""
        self.seed(1, [(STONE, 1, 0), (DIRT, 2, 0), (SAND, 3, 0)])
        self.model.set_anchor(SAND, 2)
        self.model.replace_snapshot(2, [(STONE, 1, 0), (DIRT, 2, 0)])
        key, offset = self.model.anchor()
        self.assertNotEqual(key, SAND)
        self.assertEqual(self.order()[offset], key)

    def test_the_anchor_falls_back_when_a_filter_hides_it(self):
        self.seed(1, [(STONE, 1, 0), (DIRT, 2, 0)])
        self.model.set_anchor(DIRT, 0)
        self.model.set_filter_text("stone")
        self.assertEqual(self.model.anchor(), (DIRT, 0), "筛选本身不改锚点")
        token = self.model.begin_batch(1, 2)
        self.model.upsert(STONE, 5, 0)
        result = self.model.end_batch(token)
        self.assertEqual(result.anchor_key, STONE)
        self.assertEqual(result.anchor_offset, 0)
        self.assertEqual(self.model.anchor(), (STONE, 0))

    def test_a_snapshot_discards_an_open_batch(self):
        self.seed(1, [(STONE, 1, 0)])
        self.model.begin_batch(1, 2)
        self.model.replace_snapshot(7, [(DIRT, 1, 0)])
        with self.assertRaises(ValueError):
            self.model.end_batch(1)
        self.assertEqual(self.model.version(), 7)


class TestQueryGeneration(ModelFixture):
    """不变量 2：旧查询不覆盖新查询。"""

    def test_a_stale_generation_is_refused_not_answered(self):
        generation = self.seed(1, [(STONE, 1, 0), (DIRT, 2, 0)])
        self.model.set_filter_text("stone")
        with self.assertRaises(StaleQuery):
            self.model.query_page(generation, 0, 9)
        # 拿当前代次去取就正常。
        self.assertEqual(
            keys_of(self.model.query_page(self.model.query_generation(), 0, 9)),
            [STONE],
        )

    def test_a_filter_change_advances_the_generation(self):
        first = self.model.query_generation()
        second = self.model.set_filter_text("stone")
        third = self.model.set_sort(SORT_COUNT, descending=True)
        self.assertGreater(second, first)
        self.assertGreater(third, second)
        self.assertTrue(self.model.is_stale_generation(first))
        self.assertFalse(self.model.is_stale_generation(third))

    def test_the_generation_and_the_result_travel_together(self):
        """两者在同一个返回值的两半：调用方没法只拿结果、忘了代次。"""
        generation, keys = self.model.query()
        self.assertEqual(generation, self.model.query_generation())
        self.assertEqual(list(keys), [])

    def test_an_unknown_sort_field_is_refused(self):
        with self.assertRaises(ValueError):
            self.model.set_sort("size")

    def test_an_unknown_generation_is_stale(self):
        current = self.model.query_generation()
        self.assertTrue(self.model.is_stale_generation(current + 1))
        self.assertTrue(self.model.is_stale_generation(current - 1))
        self.assertFalse(self.model.is_stale_generation(current))

    def test_a_delta_does_not_advance_the_generation(self):
        """数据变了不等于条件变了：代次管的是**查询条件**。推进它会让每一次
        收包都把调用方正在滚动的那一页判成过期，于是列表再也滚不动。"""
        generation = self.seed(1, [(STONE, 1, 0)])
        token = self.model.begin_batch(1, 2)
        self.model.upsert(STONE, 5, 0)
        self.model.end_batch(token)
        self.assertEqual(self.model.query_generation(), generation)
        self.assertEqual(
            keys_of(self.model.query_page(generation, 0, 9)), [STONE]
        )


class TestPaging(ModelFixture):
    """渲染切片。与 `snapshot_chunk` 的传输分批是两回事。"""

    def test_an_empty_list_pages_to_nothing(self):
        generation, keys = self.model.query()
        self.assertEqual(keys, ())
        self.assertEqual(self.model.query_page(generation, 0, 10), ())
        self.assertEqual(self.model.query_page(generation, 0, 0), ())

    def test_a_single_row(self):
        generation = self.seed(1, [(STONE, 1, 0)])
        self.assertEqual(keys_of(self.model.query_page(generation, 0, 10)), [STONE])
        self.assertEqual(self.model.query_page(generation, 1, 10), ())

    def test_a_window_in_the_middle_of_a_long_list(self):
        records = [
            (ResourceKey.item("minecraft:test_%02d" % (index,)), index, 0)
            for index in range(25)
        ]
        self.seed(1, records)
        self.ascending()
        generation = self.model.query_generation()
        self.assertEqual(self.model.count(), 25)
        page = self.model.query_page(generation, 10, 5)
        self.assertEqual(keys_of(page), [record[0] for record in records[10:15]])
        self.assertEqual([entry.stored for entry in page], [10, 11, 12, 13, 14])
        # 越界偏移返回空而不是抛：滚动到底时算出来的偏移本来就可能越界。
        self.assertEqual(self.model.query_page(generation, 100, 5), ())

    def test_offsets_and_limits_are_validated(self):
        generation, _ = self.model.query()
        for bad in (True, 1.0, "2", None):
            with self.assertRaises(TypeError):
                self.model.query_page(generation, bad, 5)
            with self.assertRaises(TypeError):
                self.model.query_page(generation, 0, bad)
        with self.assertRaises(ValueError):
            self.model.query_page(generation, -1, 5)
        with self.assertRaises(ValueError):
            self.model.query_page(generation, 0, -5)


class TestSelectionAndAnchor(ModelFixture):
    """不变量 3：点击用稳定键，下标只是这一帧的布局。"""

    def test_a_click_after_a_reorder_submits_the_recorded_key(self):
        self.seed(1, [(STONE, 1, 0), (DIRT, 2, 0), (SAND, 3, 0)])
        page = self.model.query_page(self.model.query_generation(), 0, 3)
        recorded = page[1].key          # 界面按这一页布局，记下第 2 行是谁
        self.assertEqual(recorded, SAND)

        # 改成按数量排序，并把玩家记下的那一行顶到最前：第 2 行换人了。
        self.model.set_sort(SORT_COUNT, descending=True)
        token = self.model.begin_batch(self.model.version(), 2)
        self.model.upsert(SAND, 100, 0)
        self.model.end_batch(token)
        self.assertEqual(
            self.model.query_page(self.model.query_generation(), 0, 3)[1].key,
            DIRT,
            "先确认重排真的发生了，否则这条用例什么也没验",
        )

        self.assertTrue(self.model.select(recorded))
        self.assertEqual(self.model.selected_key(), SAND, "点的是玩家看见的那一行")

    def test_a_display_index_is_not_an_identity(self):
        self.seed(1, [(STONE, 1, 0), (DIRT, 2, 0)])
        with self.assertRaises(TypeError):
            self.model.select(1)

    def test_selecting_a_key_that_is_gone_reports_failure(self):
        """点击与收包本来就是竞态，玩家点在一行刚消失的格子上不是错误。
        **也不拿下标兜底**：「那一片区域现在是谁」与「玩家看见的是谁」
        是两回事。"""
        self.seed(1, [(STONE, 1, 0)])
        self.assertFalse(self.model.select(DIRT))
        self.assertIsNone(self.model.selected_key())

    def test_the_anchor_is_confirmed_by_its_key(self):
        """锚点不是选中项：滚动不改选中，选中也不该让列表跳走。"""
        self.seed(1, [(STONE, 1, 0), (DIRT, 2, 0)])
        self.ascending()
        self.assertEqual(self.order(), [DIRT, STONE])
        self.model.set_anchor(DIRT, 3)
        # 报出来的是**它在当前顺序里的真实下标**，不是调用方报上来的那个数：
        # 调用方按它那一帧的布局报行号，而中间可能已经插进了别的行。
        self.assertEqual(self.model.anchor(), (DIRT, 0))
        self.model.select(STONE)
        self.assertEqual(self.model.anchor(), (DIRT, 0), "选中不改锚点")
        # 还没落进可见列表的锚点先原样记着，下一次重排再退到那个位置
        # （见 test_the_anchor_falls_back_to_its_old_row）。
        self.model.set_anchor(SAND, 3)
        self.assertEqual(self.model.anchor(), (SAND, 3))
        self.model.set_anchor(None, 0)
        self.assertEqual(self.model.anchor(), (None, 0))

    def test_an_index_is_the_only_thing_index_of_gives(self):
        self.seed(1, [(STONE, 1, 0), (DIRT, 2, 0)])
        self.ascending()
        self.assertEqual(self.model.index_of(DIRT), 0)
        self.assertEqual(self.model.index_of(STONE), 1)
        self.assertEqual(self.model.index_of(SAND), -1)

    def test_index_of_respects_the_filter(self):
        self.seed(1, [(STONE, 1, 0), (DIRT, 2, 0)])
        self.model.set_filter_text("stone")
        self.assertEqual(self.model.index_of(STONE), 0)
        self.assertEqual(self.model.index_of(DIRT), -1)

    def test_clear_resets_the_view_but_not_the_version(self):
        generation = self.seed(4, [(STONE, 1, 0)])
        self.model.select(STONE)
        self.model.set_anchor(STONE, 0)
        self.model.clear()
        self.assertEqual(self.model.count(), 0)
        self.assertIsNone(self.model.selected_key())
        self.assertEqual(self.model.anchor(), (None, 0))
        self.assertTrue(self.model.is_stale_generation(generation))
        # 版本留着：归零的话，一个迟到的增量批次会以为自己接得上
        # （base_version=0 与本地 0 相等），于是往一张本该由快照重建的表里
        # 写进几条绝对量。
        self.assertEqual(self.model.version(), 4)
        with self.assertRaises(ListResyncRequired):
            self.model.begin_batch(0, 1)


if __name__ == "__main__":
    unittest.main()
