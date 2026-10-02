# coding=utf-8
"""样板终端的设备侧状态：两个真实槽、虚拟配置、存档与「配置 → 样板」（11-05）。

**全部离线。** `server/crafting/template_terminal.py` 不 import 引擎——需要引擎
的两件事（拆方块掉落、某个资源键的堆叠上限）都是注入的回调。

三条贯穿全篇的判据：

1. **虚拟配置不产生实物。** 一次 `set_cell` 只改一份配置：不进网络、不进背包、
   也不生成任何物品。真的东西只在两个真实槽里。
2. **不丢物。** 拆方块时实物先尝试掉落，**掉不掉的留在孤儿索引里**，
   任何一条路径都不许在没有核对回执的情况下让某件物品消失。
3. **两条状态共用一个修订号。** 真实槽与虚拟配置各记各的号，会让
   「只动过真实槽」与「只动过配置」撞上同一个号，于是客户端的陈旧编辑被当成新的。
"""

import unittest

import testsupport  # noqa: F401 - 导入即完成 sys.path 设置

from endernet.common.crafting_recipe import normalize_recipe
from endernet.common.definitions import (
    PATTERN_MODE_CRAFTING,
    PATTERN_MODE_FACTORY,
)
from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.resource_key import ResourceKey
from endernet.server.crafting import patterns
from endernet.server.crafting.template_terminal import (
    BREAK_KEEP,
    DEFAULT_STACK_LIMIT,
    GROUP_OUTPUTS,
    GROUP_SOURCE,
    INPUT_SLOT,
    OUTPUT_CELL_COUNT,
    OUTPUT_GATE_EMPTY_OR_SAME,
    REAL_SLOT_COUNT,
    RESULT_SLOT,
    SOURCE_CELL_COUNT,
    TEMPLATE_SCHEMA_VERSION,
    TemplateConfig,
    TemplateDeviceState,
    TemplatePolicy,
    TemplateSlots,
    TemplateStore,
    detect_crafting_output,
    pattern_from_config,
)

FACTORY = ResourceKey.item("endernet:factorical_template")
CRAFTING = ResourceKey.item("endernet:crafting_template")
LOG = ResourceKey.item("minecraft:oak_log")
PLANKS = ResourceKey.item("minecraft:oak_planks")
STICK = ResourceKey.item("minecraft:stick")

#: 实测夹具，与 `test_crafting_recipe.py` 同源。
STICK_FROM_PLANKS = {
    "pattern": ["A", "A"],
    "key": {"A": {"item": ["minecraft:oak_planks", "minecraft:birch_planks"]}},
    "result": [{"count": 4, "item": "minecraft:stick"}],
}
CRAFTING_TABLE = {
    "pattern": ["AA", "AA"], "key": {"A": {"item": "minecraft:oak_planks"}},
    "result": [{"item": "minecraft:crafting_table"}],
}


def device(generation=1, x=0):
    return DeviceRef(NodeKey(0, x, 64, 0), generation)


class FakeRecipeSource(object):
    """鸭子类型的配方来源：本模块只用到 `recipes_for_input` 一个方法。"""

    def __init__(self, by_input=None, raises=None):
        self.by_input = by_input or {}
        self.raises = raises
        self.asked = []

    def recipes_for_input(self, key):
        self.asked.append(key.name)
        if self.raises is not None:
            raise self.raises
        return self.by_input.get(key.name, ())


def source_cells(*entries):
    """按 `(下标, 资源键, 数量)` 摆一个九宫格；返回 `config.cells(GROUP_SOURCE)`。"""
    config = TemplateConfig()
    for index, key, count in entries:
        config.set_cell(GROUP_SOURCE, index, key, count)
    return config.cells(GROUP_SOURCE)


class TestTemplateSlots(unittest.TestCase):

    def test_a_new_slot_set_is_two_empty_slots(self):
        slots = TemplateSlots()
        self.assertEqual(REAL_SLOT_COUNT, len(slots.slots))
        self.assertTrue(slots.is_empty())
        self.assertEqual((), slots.occupied_slots())

    def test_put_stacking_and_taking_move_the_revision(self):
        slots = TemplateSlots()
        start = slots.revision.value
        slots.set_slot(INPUT_SLOT, FACTORY, 5)
        self.assertEqual(5, slots.count_at(INPUT_SLOT))
        self.assertEqual(3, slots.add_at(INPUT_SLOT, FACTORY, 3))
        self.assertEqual(8, slots.count_at(INPUT_SLOT))
        self.assertEqual(3, slots.take_at(INPUT_SLOT, 3))
        self.assertEqual(5, slots.count_at(INPUT_SLOT))
        self.assertEqual(5, slots.take_at(INPUT_SLOT, 99))
        self.assertEqual(0, slots.count_at(INPUT_SLOT))
        self.assertIsNone(slots.entry(INPUT_SLOT))
        self.assertGreater(slots.revision.value, start)

    def test_a_different_resource_never_merges_into_an_occupied_slot(self):
        slots = TemplateSlots()
        slots.set_slot(INPUT_SLOT, FACTORY, 2)
        self.assertEqual(0, slots.add_at(INPUT_SLOT, CRAFTING, 4))
        self.assertEqual(FACTORY, slots.key_at(INPUT_SLOT))
        self.assertEqual(2, slots.count_at(INPUT_SLOT))

    def test_setting_zero_or_none_clears_the_slot(self):
        slots = TemplateSlots()
        slots.set_slot(RESULT_SLOT, FACTORY, 4)
        slots.set_slot(RESULT_SLOT, FACTORY, 0)
        self.assertIsNone(slots.entry(RESULT_SLOT))
        slots.set_slot(RESULT_SLOT, FACTORY, 4)
        slots.set_slot(RESULT_SLOT, None, 4)
        self.assertIsNone(slots.entry(RESULT_SLOT))

    def test_an_out_of_range_or_boolean_index_is_refused(self):
        slots = TemplateSlots()
        for bad in (-1, REAL_SLOT_COUNT, True, "0", None):
            with self.assertRaises(ValueError):
                slots.set_slot(bad, FACTORY, 1)

    def test_the_round_trip_keeps_keys_and_counts(self):
        slots = TemplateSlots()
        slots.set_slot(INPUT_SLOT, FACTORY, 5)
        slots.set_slot(RESULT_SLOT, CRAFTING, 1)
        again = TemplateSlots.from_dict(slots.to_dict())
        self.assertEqual(FACTORY, again.key_at(INPUT_SLOT))
        self.assertEqual(5, again.count_at(INPUT_SLOT))
        self.assertEqual(CRAFTING, again.key_at(RESULT_SLOT))

    def test_the_schema_version_rides_along(self):
        self.assertEqual(TEMPLATE_SCHEMA_VERSION,
                         TemplateSlots().to_dict()["schema_version"])


class TestTemplateConfig(unittest.TestCase):

    def test_a_new_config_is_crafting_mode_and_all_empty(self):
        config = TemplateConfig()
        self.assertEqual(PATTERN_MODE_CRAFTING, config.mode)
        self.assertEqual(SOURCE_CELL_COUNT, len(config.source))
        self.assertEqual(OUTPUT_CELL_COUNT, len(config.outputs))
        self.assertTrue(config.is_empty(GROUP_SOURCE))
        self.assertTrue(config.is_empty(GROUP_OUTPUTS))

    def test_putting_overwrites_the_previous_content(self):
        config = TemplateConfig()
        config.set_cell(GROUP_SOURCE, 0, LOG, 1)
        config.set_cell(GROUP_SOURCE, 0, PLANKS, 7)
        self.assertEqual(PLANKS, config.cell(GROUP_SOURCE, 0)[0])
        self.assertEqual(7, config.count_of(GROUP_SOURCE, 0))

    def test_clearing_an_empty_cell_reports_no_change(self):
        config = TemplateConfig()
        self.assertFalse(config.clear_cell(GROUP_SOURCE, 4))
        config.set_cell(GROUP_SOURCE, 4, LOG, 1)
        self.assertTrue(config.clear_cell(GROUP_SOURCE, 4))
        self.assertIsNone(config.cell(GROUP_SOURCE, 4))

    def test_the_count_only_changes_when_it_really_changes(self):
        config = TemplateConfig()
        self.assertFalse(config.set_count(GROUP_SOURCE, 0, 3))
        config.set_cell(GROUP_SOURCE, 0, LOG, 1)
        self.assertFalse(config.set_count(GROUP_SOURCE, 0, 1))
        self.assertTrue(config.set_count(GROUP_SOURCE, 0, 12))
        self.assertEqual(12, config.count_of(GROUP_SOURCE, 0))

    def test_switching_mode_keeps_the_source_and_clears_the_outputs(self):
        """User 2026-10-02 定的口径，逐条钉住。"""
        config = TemplateConfig()
        config.set_cell(GROUP_SOURCE, 2, LOG, 3)
        config.set_cell(GROUP_OUTPUTS, 0, STICK, 4)
        self.assertTrue(config.set_mode(PATTERN_MODE_FACTORY))
        self.assertEqual(PATTERN_MODE_FACTORY, config.mode)
        self.assertEqual((2,), config.occupied(GROUP_SOURCE))
        self.assertEqual((), config.occupied(GROUP_OUTPUTS))

    def test_switching_to_the_same_mode_changes_nothing(self):
        config = TemplateConfig()
        before = config.revision.value
        self.assertFalse(config.set_mode(PATTERN_MODE_CRAFTING))
        self.assertEqual(before, config.revision.value)

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            TemplateConfig(mode="smelting")
        with self.assertRaises(ValueError):
            TemplateConfig().set_mode("smelting")

    def test_the_outputs_are_locked_in_crafting_mode_only(self):
        config = TemplateConfig()
        for index in range(OUTPUT_CELL_COUNT):
            self.assertTrue(config.locked(GROUP_OUTPUTS, index))
        self.assertFalse(config.locked(GROUP_SOURCE, 0))
        config.set_mode(PATTERN_MODE_FACTORY)
        for index in range(OUTPUT_CELL_COUNT):
            self.assertFalse(config.locked(GROUP_OUTPUTS, index))

    def test_cells_flatten_to_name_aux_and_count(self):
        cells = source_cells((0, LOG, 3))
        self.assertEqual((LOG.name, LOG.aux, 3), cells[0])
        self.assertIsNone(cells[1])
        self.assertEqual(SOURCE_CELL_COUNT, len(cells))

    def test_the_round_trip_keeps_mode_source_and_outputs(self):
        config = TemplateConfig()
        config.set_mode(PATTERN_MODE_FACTORY)
        config.set_cell(GROUP_SOURCE, 1, LOG, 2)
        config.set_cell(GROUP_OUTPUTS, 2, STICK, 8)
        again = TemplateConfig.from_dict(config.to_dict())
        self.assertEqual(PATTERN_MODE_FACTORY, again.mode)
        self.assertEqual(LOG, again.cell(GROUP_SOURCE, 1)[0])
        self.assertEqual(8, again.count_of(GROUP_OUTPUTS, 2))

    def test_an_unknown_group_is_refused(self):
        config = TemplateConfig()
        with self.assertRaises(ValueError):
            config.cell("inputs", 0)


class TestSharedRevision(unittest.TestCase):

    def test_both_halves_move_the_same_counter(self):
        """真实槽与虚拟配置共用一个号——这是模块文档里那条判断的落点。"""
        state = TemplateDeviceState()
        start = state.revision
        state.config.set_cell(GROUP_SOURCE, 0, LOG, 1)
        after_config = state.revision
        state.slots.set_slot(INPUT_SLOT, FACTORY, 1)
        after_slots = state.revision
        self.assertEqual(start + 1, after_config)
        self.assertEqual(start + 2, after_slots)

    def test_the_state_round_trip_keeps_the_revision(self):
        state = TemplateDeviceState()
        state.config.set_cell(GROUP_SOURCE, 0, LOG, 1)
        state.slots.set_slot(INPUT_SLOT, FACTORY, 2)
        again = TemplateDeviceState.from_dict(state.to_dict())
        self.assertEqual(state.revision, again.revision)
        self.assertEqual(FACTORY, again.slots.key_at(INPUT_SLOT))
        self.assertEqual(LOG, again.config.cell(GROUP_SOURCE, 0)[0])


class TestTemplateStore(unittest.TestCase):

    def test_the_accepts_gate_refuses_without_creating_anything(self):
        store = TemplateStore(accepts=lambda ref: False)
        self.assertIsNone(store.state_for(device()))
        self.assertEqual(0, store.diagnostics()["devices"])

    def test_a_broken_accepts_callback_refuses_rather_than_admits(self):
        def broken(ref):
            raise RuntimeError("registry exploded")
        store = TemplateStore(accepts=broken)
        self.assertIsNone(store.state_for(device()))

    def test_get_does_not_create_while_state_for_does(self):
        store = TemplateStore()
        self.assertIsNone(store.get(device()))
        self.assertIsNotNone(store.state_for(device()))
        self.assertIsNotNone(store.get(device()))

    def test_a_new_generation_at_the_same_position_is_a_different_state(self):
        store = TemplateStore()
        first = store.state_for(device(generation=1))
        first.slots.set_slot(INPUT_SLOT, FACTORY, 5)
        second = store.state_for(device(generation=2))
        self.assertIsNot(first, second)
        self.assertTrue(second.slots.is_empty())

    def test_the_stack_limit_comes_from_the_injected_lookup(self):
        store = TemplateStore(limit_of=lambda key: 64 if key == FACTORY else 1)
        self.assertEqual(64, store.limit_of(FACTORY))
        self.assertEqual(1, store.limit_of(CRAFTING))

    def test_without_a_lookup_the_limit_is_the_conservative_one(self):
        self.assertEqual(DEFAULT_STACK_LIMIT,
                         TemplateStore().limit_of(FACTORY))

    def test_the_room_is_zero_for_a_different_resource(self):
        store = TemplateStore(limit_of=lambda key: 64)
        state = store.state_for(device())
        state.slots.set_slot(INPUT_SLOT, CRAFTING, 1)
        self.assertEqual(0, store.room_for(state, INPUT_SLOT, FACTORY))
        self.assertEqual(64, store.room_for(state, INPUT_SLOT, CRAFTING) + 1)

    def test_the_room_shrinks_with_what_is_already_there(self):
        store = TemplateStore(limit_of=lambda key: 64)
        state = store.state_for(device())
        state.slots.set_slot(INPUT_SLOT, FACTORY, 60)
        self.assertEqual(4, store.room_for(state, INPUT_SLOT, FACTORY))

    def test_the_round_trip_restores_live_and_orphan_states(self):
        store = TemplateStore()
        live = store.state_for(device(x=1))
        live.slots.set_slot(INPUT_SLOT, FACTORY, 3)
        store.state_for(device(x=2))
        store.forget(device(x=2))
        other = TemplateStore()
        self.assertEqual(2, other.restore(store.save_all()))
        self.assertEqual(FACTORY, other.get(device(x=1)).slots.key_at(INPUT_SLOT))
        self.assertEqual(1, len(other.orphans()))

    def test_a_newer_schema_is_skipped_rather_than_read_as_empty(self):
        store = TemplateStore()
        payload = store.save_all()
        payload["schema_version"] = TEMPLATE_SCHEMA_VERSION + 1
        self.assertEqual(0, store.restore(payload))

    def test_forgetting_an_unknown_device_reports_no_change(self):
        self.assertFalse(TemplateStore().forget(device()))

    def test_a_ref_without_a_node_key_is_refused(self):
        store = TemplateStore()
        with self.assertRaises(ValueError):
            store.state_for(object())

    def test_the_diagnostics_report_the_source_count_and_orphans(self):
        store = TemplateStore()
        state = store.state_for(device())
        state.config.set_cell(GROUP_SOURCE, 0, LOG, 1)
        store.forget(device())
        report = store.diagnostics()
        self.assertEqual(0, report["devices"])
        self.assertEqual(1, report["orphaned"])


class TestBreakDisposition(unittest.TestCase):
    """拆方块时两个真实槽的实物怎么处置（User 2026-10-02：掉落为掉落物）。"""

    def _store(self, drop, policy=None):
        return TemplateStore(drop=drop, policy=policy)

    def test_the_real_slots_are_dropped_at_the_block_position(self):
        seen = []

        def drop(node_key, items):
            seen.append((node_key, items))
            return len(items)

        store = self._store(drop)
        state = store.state_for(device())
        state.slots.set_slot(INPUT_SLOT, FACTORY, 5)
        state.slots.set_slot(RESULT_SLOT, FACTORY, 2)
        self.assertTrue(store.forget(device()))
        self.assertEqual(1, len(seen))
        node_key, items = seen[0]
        self.assertEqual((0, 0, 64, 0), node_key)
        self.assertEqual(2, len(items))
        self.assertEqual(0, store.stats["dropped_failed"])
        self.assertEqual(7, store.stats["dropped_items"])
        self.assertEqual(2, store.stats["dropped_stacks"])
        self.assertEqual(0, store.diagnostics()["orphan_items"])

    def test_what_could_not_be_dropped_stays_in_the_orphans(self):
        """**掉不掉的绝不删。** 掉落是主路径，孤儿索引是失败桶。"""
        store = self._store(lambda node_key, items: 1)
        state = store.state_for(device())
        state.slots.set_slot(INPUT_SLOT, FACTORY, 5)
        state.slots.set_slot(RESULT_SLOT, FACTORY, 2)
        store.forget(device())
        self.assertEqual(1, store.stats["dropped_failed"])
        self.assertEqual(2, store.diagnostics()["orphan_items"])
        leftover = store.orphans()[0]
        self.assertIsNone(leftover.slots.entry(INPUT_SLOT))
        self.assertEqual(2, leftover.slots.count_at(RESULT_SLOT))

    def test_a_raising_drop_callback_keeps_everything(self):
        def broken(node_key, items):
            raise RuntimeError("no dimension")

        store = self._store(broken)
        state = store.state_for(device())
        state.slots.set_slot(INPUT_SLOT, FACTORY, 3)
        store.forget(device())
        self.assertEqual(3, store.diagnostics()["orphan_items"])

    def test_without_a_drop_callback_nothing_disappears(self):
        """离线夹具与没有引擎面的场合：状态原样进孤儿索引。"""
        store = TemplateStore()
        state = store.state_for(device())
        state.slots.set_slot(INPUT_SLOT, FACTORY, 3)
        store.forget(device())
        self.assertEqual(3, store.diagnostics()["orphan_items"])

    def test_the_keep_disposition_never_calls_drop(self):
        calls = []
        store = TemplateStore(drop=lambda node_key, items: calls.append(1) or 1,
                              policy=_keep_policy())
        state = store.state_for(device())
        state.slots.set_slot(INPUT_SLOT, FACTORY, 3)
        store.forget(device())
        self.assertEqual([], calls)
        self.assertEqual(3, store.diagnostics()["orphan_items"])

    def test_an_empty_state_never_calls_drop(self):
        calls = []
        store = TemplateStore(drop=lambda node_key, items: calls.append(1) or 0)
        store.state_for(device())
        store.forget(device())
        self.assertEqual([], calls)


def _keep_policy():
    class Keeping(TemplatePolicy):
        break_disposition = BREAK_KEEP
    return Keeping()


class TestObserve(unittest.TestCase):

    def test_only_a_removal_moves_anything(self):
        calls = []
        store = TemplateStore(drop=lambda node_key, items: calls.append(1) or 1)
        state = store.state_for(device())
        state.slots.set_slot(INPUT_SLOT, FACTORY, 1)

        class Change(object):
            def __init__(self, kind):
                self.kind = kind
                self.ref = device()

        self.assertEqual(0, store.observe(Change("added")))
        self.assertEqual(0, store.observe(Change("loaded")))
        self.assertEqual(1, store.observe(Change("removed")))
        self.assertEqual(1, len(calls))

    def test_a_change_without_a_ref_is_ignored(self):
        store = TemplateStore()

        class Change(object):
            kind = "removed"
            ref = None

        self.assertEqual(0, store.observe(Change()))


class TestPatternFromConfig(unittest.TestCase):

    def test_crafting_patterns_are_ordered_and_carry_the_preview(self):
        cells = source_cells((0, PLANKS, 1), (3, PLANKS, 1))
        config = TemplateConfig()
        config.set_cell(GROUP_SOURCE, 0, PLANKS, 1)
        config.set_cell(GROUP_SOURCE, 3, PLANKS, 1)
        definition = pattern_from_config(
            config, {"name": "minecraft:stick", "aux": 0, "count": 4})
        self.assertEqual(PATTERN_MODE_CRAFTING, definition.mode)
        self.assertTrue(definition.is_ordered())
        self.assertEqual((0, 3), tuple(stack.slot for stack in definition.inputs))
        self.assertEqual(4, definition.outputs[0].count)
        self.assertEqual("minecraft:stick", definition.outputs[0].key.name)
        self.assertEqual(SOURCE_CELL_COUNT, len(cells))

    def test_the_interface_only_placeholder_cells_are_not_encoded(self):
        """界面上的上下两格只是画出来的，配置里根本没有它们——**永不进样板**。
        """
        config = TemplateConfig()
        config.set_cell(GROUP_SOURCE, 0, PLANKS, 1)
        definition = pattern_from_config(
            config, {"name": "minecraft:stick", "aux": 0, "count": 4})
        self.assertEqual(1, len(definition.outputs))
        self.assertEqual("minecraft:stick", definition.outputs[0].key.name)

    def test_without_a_preview_a_crafting_pattern_has_no_output(self):
        """检测不到配方 → 产物为空 → `PatternDefinition` 抛 `EMPTY_OUTPUTS`。"""
        config = TemplateConfig()
        config.set_cell(GROUP_SOURCE, 0, PLANKS, 1)
        with self.assertRaises(patterns.PatternValidationError) as caught:
            pattern_from_config(config, None)
        self.assertEqual(patterns.PatternError.EMPTY_OUTPUTS, caught.exception.code)

    def test_an_empty_source_is_refused(self):
        config = TemplateConfig()
        with self.assertRaises(patterns.PatternValidationError) as caught:
            pattern_from_config(config, {"name": "minecraft:stick", "count": 1})
        self.assertEqual(patterns.PatternError.EMPTY_INPUTS, caught.exception.code)

    def test_factory_patterns_are_unordered_and_keep_the_display_order(self):
        config = TemplateConfig(mode=PATTERN_MODE_FACTORY)
        config.set_cell(GROUP_SOURCE, 4, LOG, 1)
        config.set_cell(GROUP_OUTPUTS, 0, STICK, 8)
        config.set_cell(GROUP_OUTPUTS, 2, PLANKS, 1)
        definition = pattern_from_config(config, None)
        self.assertEqual(PATTERN_MODE_FACTORY, definition.mode)
        self.assertFalse(definition.is_ordered())
        self.assertEqual(
            ["minecraft:stick", "minecraft:oak_planks"],
            [stack.key.name for stack in definition.outputs],
        )
        self.assertIsNone(definition.inputs[0].slot)

    def test_the_first_output_cell_is_the_primary_one_by_order_only(self):
        """「主产物」只是界面顺序，不进数据：产物列表是扁平的。"""
        config = TemplateConfig(mode=PATTERN_MODE_FACTORY)
        config.set_cell(GROUP_SOURCE, 0, LOG, 1)
        config.set_cell(GROUP_OUTPUTS, 1, STICK, 1)
        config.set_cell(GROUP_OUTPUTS, 0, PLANKS, 1)
        definition = pattern_from_config(config, None)
        self.assertEqual(
            ["minecraft:oak_planks", "minecraft:stick"],
            [stack.key.name for stack in definition.outputs],
        )
        self.assertEqual(2, len(definition.outputs))

    def test_every_template_this_terminal_makes_declares_no_returns(self):
        """R09 要求返还物显式声明，而本界面没有承载它的控件——如实记为缺口。"""
        config = TemplateConfig(mode=PATTERN_MODE_FACTORY)
        config.set_cell(GROUP_SOURCE, 0, LOG, 1)
        config.set_cell(GROUP_OUTPUTS, 0, STICK, 1)
        definition = pattern_from_config(config, None)
        self.assertEqual((), definition.returns)


class TestDetectCraftingOutput(unittest.TestCase):

    def test_a_matching_recipe_gives_its_output(self):
        source = FakeRecipeSource(
            {"minecraft:oak_planks": [normalize_recipe(STICK_FROM_PLANKS)]})
        cells = source_cells((0, PLANKS, 1), (3, PLANKS, 1))
        preview = detect_crafting_output(cells, source)
        self.assertEqual("minecraft:stick", preview["name"])
        self.assertEqual(4, preview["count"])

    def test_the_first_non_empty_cell_is_the_one_asked_with(self):
        source = FakeRecipeSource(
            {"minecraft:oak_planks": [normalize_recipe(STICK_FROM_PLANKS)]})
        cells = source_cells((0, LOG, 1), (1, PLANKS, 1), (3, PLANKS, 1))
        detect_crafting_output(cells, source)
        self.assertEqual(["minecraft:oak_log", "minecraft:oak_planks"],
                         source.asked)

    def test_a_recipe_that_does_not_match_the_layout_is_skipped(self):
        """按某个原料查到的配方，摆位对不上就不算数（引擎按原料查，不按摆位查）。"""
        source = FakeRecipeSource(
            {"minecraft:oak_planks": [normalize_recipe(CRAFTING_TABLE),
                                      normalize_recipe(STICK_FROM_PLANKS)]})
        cells = source_cells((0, PLANKS, 1), (3, PLANKS, 1))
        preview = detect_crafting_output(cells, source)
        self.assertEqual("minecraft:stick", preview["name"])

    def test_an_extra_item_in_the_grid_gives_no_preview(self):
        """User 2026-10-02 报告的场景：一块木板能做按钮，多摆一颗钻石就不能。

        合成模式的中间预览格走的就是这条判定，所以「预览仍亮着」与
        「点下去真能做出按钮」是同一件事。多摆的那件必须让整条配方不成立，
        否则它会**参与匹配却被无视**——既没进产物，也没被吃掉。
        """
        button = normalize_recipe({
            "ingredients": [{"item": "minecraft:oak_planks"}],
            "result": [{"item": "minecraft:oak_button"}],
        })
        source = FakeRecipeSource({"minecraft:oak_planks": [button]})
        diamond = ResourceKey.item("minecraft:diamond")

        preview = detect_crafting_output(source_cells((0, PLANKS, 1)), source)
        self.assertEqual("minecraft:oak_button", preview["name"])

        for slot in range(1, 9):
            cells = source_cells((0, PLANKS, 1), (slot, diamond, 1))
            self.assertIsNone(
                detect_crafting_output(cells, source),
                "第 %d 格多摆一颗钻石却仍然检测出配方" % (slot,),
            )

    def test_no_recipe_at_all_gives_nothing(self):
        source = FakeRecipeSource({})
        cells = source_cells((0, LOG, 1))
        self.assertIsNone(detect_crafting_output(cells, source))

    def test_an_empty_grid_asks_nothing(self):
        source = FakeRecipeSource({"minecraft:oak_log": []})
        self.assertIsNone(detect_crafting_output(source_cells(), source))
        self.assertEqual([], source.asked)

    def test_the_same_resource_is_asked_about_only_once(self):
        source = FakeRecipeSource({"minecraft:oak_planks": []})
        cells = source_cells((0, PLANKS, 1), (1, PLANKS, 1), (2, PLANKS, 1))
        detect_crafting_output(cells, source)
        self.assertEqual(["minecraft:oak_planks"], source.asked)

    def test_without_a_recipe_source_there_is_no_preview(self):
        self.assertIsNone(detect_crafting_output(source_cells((0, LOG, 1)), None))


class TestTemplatePolicy(unittest.TestCase):

    def test_the_settled_values_are_not_listed_as_unresolved(self):
        policy = TemplatePolicy()
        for name in ("max_config_count", "encode_count_per_cell",
                     "encode_output_count", "break_disposition", "preview_pick",
                     "mode_switch_keeps_source", "mode_switch_clears_outputs"):
            self.assertNotIn(name, policy.UNRESOLVED)
            self.assertIn(name, policy.describe())

    def test_the_stand_ins_are_listed(self):
        policy = TemplatePolicy()
        for name in policy.UNRESOLVED:
            self.assertIn(name, policy.describe())
        self.assertIn("returns_source", policy.UNRESOLVED)

    def test_the_report_points_at_both_the_values_and_the_open_items(self):
        report = TemplatePolicy().unresolved_report()
        self.assertEqual(list(TemplatePolicy.UNRESOLVED), report["unresolved"])
        self.assertEqual(OUTPUT_GATE_EMPTY_OR_SAME,
                         report["policy"]["encode_output_gate"])
        self.assertIn("R09", report["scope_note"])

    def test_the_settled_assertion_fires_while_anything_is_open(self):
        """只在验收时调用；这里只确认它**会**抛，不改任何生产口径。"""
        with self.assertRaises(AssertionError):
            TemplatePolicy().assert_settled()


if __name__ == "__main__":
    unittest.main()
