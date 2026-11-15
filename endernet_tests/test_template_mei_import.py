# coding=utf-8
"""MEI 配方导入经真实命令链原子修改样板配置，不转移物品。"""

from copy import deepcopy
import unittest

from test_template_handlers import Fixture, LOG, PLANKS, STICK, FACTORY
from endernet.common.crafting_recipe import PresetRecipe, RecipeIngredient
from endernet.common.protocol import Reason
from endernet.common.resources.key import ResourceKey
from endernet.server.adapters.recipes import EngineRecipeSource
from endernet.server.crafting.terminals.template import (
    GROUP_SOURCE, GROUP_OUTPUTS, INPUT_SLOT, RESULT_SLOT,
)


def entry(key, count=1):
    return {"key": key.to_dict(), "count": count}


class TestMeiImport(Fixture):
    CONFIG_ACTIONS = Fixture.CONFIG_ACTIONS | frozenset(["template_mei_import"])

    def crafting(self):
        inputs = [None] * 9
        inputs[2] = entry(PLANKS)
        inputs[5] = entry(PLANKS)
        return {"mode": "crafting", "inputs": inputs, "outputs": [entry(STICK, 4)]}

    def factory(self):
        return {"mode": "factory", "inputs": [entry(LOG, 12), entry(PLANKS, 3)],
                "outputs": [entry(STICK, 8), entry(LOG, 2), entry(PLANKS, 5)]}

    def import_mei(self, config, **fields):
        payload = {"action": "template_mei_import", "config": config}
        payload.update(fields)
        return self.send(payload)

    def test_crafting_selects_mode_preserves_shape_and_only_advances_once(self):
        self.config.set_mode("factory")
        self.config.set_cell(GROUP_SOURCE, 8, LOG, 3)
        self.config.set_cell(GROUP_OUTPUTS, 2, LOG, 7)
        self.slots.set_slot(INPUT_SLOT, FACTORY, 3)
        self.slots.set_slot(RESULT_SLOT, LOG, 2)
        slots_before = self.slots.to_dict()
        revision = self.state.revision
        result = self.import_mei(self.crafting())
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual("crafting", self.config.mode)
        self.assertEqual((2, 5), self.config.occupied(GROUP_SOURCE))
        self.assertEqual((PLANKS, 1), self.config.cell(GROUP_SOURCE, 2))
        self.assertFalse(any(self.config.outputs))
        self.assertEqual(revision + 1, self.state.revision)
        self.assertEqual(slots_before, self.slots.to_dict())
        self.assertEqual([], self.inventory.calls)

    def test_factory_selects_mode_keeps_quantities_outputs_and_encodes_them(self):
        self.config.set_cell(GROUP_SOURCE, 8, LOG, 1)
        revision = self.state.revision
        result = self.import_mei(self.factory())
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual("factory", self.config.mode)
        self.assertEqual(revision + 1, self.state.revision)
        self.assertEqual([(LOG, 12), (PLANKS, 3)] + [None] * 7, self.config.source)
        self.assertEqual([(STICK, 8), (LOG, 2), (PLANKS, 5)], self.config.outputs)
        self.assertEqual([], self.inventory.calls)
        self.slots.set_slot(INPUT_SLOT, FACTORY, 1)
        self.assertTrue(self.send({"action": "template_encode"}).is_applied())
        definition = self.encoded().definition
        self.assertEqual("factory", definition.mode)
        self.assertEqual([(LOG, 12), (PLANKS, 3)], [(s.key, s.count) for s in definition.inputs])
        self.assertEqual([(STICK, 8), (LOG, 2), (PLANKS, 5)],
                         [(s.key, s.count) for s in definition.outputs])

    def test_crafting_encodes_only_server_detected_output(self):
        self.assertTrue(self.import_mei(self.crafting()).is_applied())
        self.slots.set_slot(INPUT_SLOT, FACTORY, 1)
        self.assertTrue(self.send({"action": "template_encode"}).is_applied())
        definition = self.encoded().definition
        self.assertEqual([(2, PLANKS, 1), (5, PLANKS, 1)],
                         [(s.slot, s.key, s.count) for s in definition.inputs])
        self.assertEqual([(STICK, 4)], [(s.key, s.count) for s in definition.outputs])

    def assert_refused_without_edit(self, config, **fields):
        before = deepcopy(self.config.to_dict())
        slots_before = self.slots.to_dict()
        revision = self.state.revision
        result = self.import_mei(config, **fields)
        self.assertFalse(result.is_applied(), result.to_dict())
        self.assertEqual(before, self.config.to_dict())
        self.assertEqual(revision, self.state.revision)
        self.assertEqual(slots_before, self.slots.to_dict())
        self.assertEqual([], self.inventory.calls)
        return result

    def test_stale_revision_and_non_template_device_are_refused(self):
        result = self.assert_refused_without_edit(self.factory(), slot_version=99)
        self.assertEqual([Reason.STALE_VERSION], self.reasons(result))
        self.store._accepts = lambda ref: False
        result = self.assert_refused_without_edit(self.factory())
        self.assertEqual([Reason.UNSUPPORTED], self.reasons(result))

    def test_invalid_late_output_leaves_existing_mode_and_all_cells_intact(self):
        self.config.set_cell(GROUP_SOURCE, 8, LOG, 3)
        bad = self.factory()
        bad["outputs"][2]["count"] = 65536
        self.assert_refused_without_edit(bad)

    def test_capacity_and_empty_recipes_are_refused_without_truncation(self):
        for field, entries in (("inputs", [entry(LOG)] * 10),
                               ("outputs", [entry(STICK)] * 4),
                               ("inputs", []), ("outputs", [None] * 3)):
            with self.subTest(field=field, entries=entries):
                config = self.factory()
                config[field] = entries
                self.assert_refused_without_edit(config)

    def test_malformed_items_and_counts_are_refused(self):
        malformed = [None, [], {"mode": []}, {"mode": "other"}]
        for config in malformed:
            self.assert_refused_without_edit(config)
        for invalid in ("item", {}, {"key": None, "count": 1},
                        entry(ResourceKey.item("minecraft:air")),
                        entry(LOG, True), entry(LOG, 0), entry(LOG, -1), entry(LOG, 1.5)):
            with self.subTest(invalid=invalid):
                config = self.factory()
                config["outputs"][2] = invalid
                self.assert_refused_without_edit(config)

    def test_crafting_rejects_unknown_recipe_changed_output_or_stacked_input(self):
        config = self.crafting()
        config["inputs"][2] = entry(LOG)
        self.assert_refused_without_edit(config)
        for output in (entry(LOG, 4), entry(STICK, 3),
                       entry(ResourceKey.item(STICK.name, 1), 4)):
            config = self.crafting()
            config["outputs"] = [output]
            self.assert_refused_without_edit(config)
        config = self.crafting()
        config["inputs"][2]["count"] = 2
        self.assert_refused_without_edit(config)
        config = self.crafting()
        config["outputs"].append(entry(LOG))
        self.assert_refused_without_edit(config)

    def test_factory_fluids_follow_existing_pattern_support_and_crafting_rejects_them(self):
        water = ResourceKey.fluid("minecraft:water")
        config = self.factory()
        config["inputs"][0] = entry(water, 1000)
        self.assertTrue(self.import_mei(config).is_applied())
        self.assertEqual((water, 1000), self.config.cell(GROUP_SOURCE, 0))
        config = self.crafting()
        config["inputs"][2] = entry(water)
        self.assert_refused_without_edit(config)

    def test_unavailable_or_failing_recipe_adapter_preserves_config(self):
        self.service.terminal.recipes = None
        result = self.assert_refused_without_edit(self.crafting())
        self.assertEqual([Reason.NO_ADAPTER], self.reasons(result))
        self.service.terminal.recipes = self.recipes
        def fail(key):
            raise RuntimeError("recipe source unavailable")
        self.recipes.recipes_for_input = fail
        result = self.assert_refused_without_edit(self.crafting())
        self.assertEqual([Reason.NO_ADAPTER], self.reasons(result))


class TestShapelessMeiImport(Fixture):
    """使用引擎实际返回的 count / 标签候选形状走导入、预览与编码。"""

    CONFIG_ACTIONS = TestMeiImport.CONFIG_ACTIONS
    import_mei = TestMeiImport.import_mei
    assert_refused_without_edit = TestMeiImport.assert_refused_without_edit

    def install_recipe(self, raw):
        source = EngineRecipeSource(fetch=lambda *args: [deepcopy(raw)],
                                    input_fetch=lambda *args: [deepcopy(raw)])
        self.service.terminal.recipes = source
        return source

    def book(self):
        return {"ingredients": [{"item": "minecraft:paper", "data": 0, "count": 3},
                                {"item": "minecraft:leather", "data": 0}],
                "result": [{"item": "minecraft:book", "data": 0}]}

    def test_counted_shapeless_inputs_import_preview_and_encode_as_separate_slots(self):
        source = self.install_recipe(self.book())
        paper = ResourceKey.item("minecraft:paper")
        leather = ResourceKey.item("minecraft:leather")
        book = ResourceKey.item("minecraft:book")
        self.config.set_mode("factory")
        revision = self.state.revision
        config = {"mode": "crafting",
                  "inputs": [entry(paper), entry(paper), entry(paper), entry(leather)] + [None] * 5,
                  "outputs": [entry(book)]}
        result = self.import_mei(config)
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual(revision + 1, self.state.revision)
        self.assertEqual("crafting", self.config.mode)
        self.assertEqual((0, 1, 2, 3), self.config.occupied(GROUP_SOURCE))
        self.assertEqual(0, source.stats["skipped"])
        self.slots.set_slot(INPUT_SLOT, FACTORY, 1)
        self.assertTrue(self.send({"action": "template_encode"}).is_applied())
        definition = self.encoded().definition
        self.assertEqual([(0, paper, 1), (1, paper, 1), (2, paper, 1), (3, leather, 1)],
                         [(value.slot, value.key, value.count) for value in definition.inputs])
        self.assertEqual([(book, 1)], [(value.key, value.count) for value in definition.outputs])
        self.assertEqual([], self.inventory.calls)

    def test_shapeless_tag_candidate_and_duplicate_inputs_are_not_dropped(self):
        strip = ResourceKey.item("endernet:magnetic_strip")
        iron = ResourceKey.item("minecraft:iron_ingot")
        planks = ResourceKey.item("minecraft:spruce_planks")
        tape = ResourceKey.item("endernet:doubleside_tape")
        raw = {"ingredients": [{"item": strip.name, "data": 0, "count": 2},
                               {"item": iron.name, "data": 0},
                               {"item": ["minecraft:acacia_planks", planks.name]}],
               "result": [{"item": tape.name, "data": 0}]}
        source = self.install_recipe(raw)
        result = self.import_mei({"mode": "crafting",
                                  "inputs": [entry(strip), entry(strip), entry(iron), entry(planks)],
                                  "outputs": [entry(tape)]})
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual((planks, 1), self.config.cell(GROUP_SOURCE, 3))
        self.assertEqual(0, source.stats["skipped"])
        self.slots.set_slot(INPUT_SLOT, FACTORY, 1)
        self.assertTrue(self.send({"action": "template_encode"}).is_applied())
        self.assertEqual(tape, self.encoded().definition.outputs[0].key)

    def test_builtin_output_import_also_expands_counted_shapeless_inputs(self):
        self.install_recipe(self.book())
        book = ResourceKey.item("minecraft:book")
        result = self.send({"action": "template_import", "key": book.to_dict()})
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual((0, 1, 2, 3), self.config.occupied(GROUP_SOURCE))
        self.assertEqual([1] * 4, [self.config.cell(GROUP_SOURCE, index)[1] for index in range(4)])
        self.slots.set_slot(INPUT_SLOT, FACTORY, 1)
        self.assertTrue(self.send({"action": "template_encode"}).is_applied())

    def test_shapeless_missing_or_extra_slot_still_cannot_import(self):
        self.install_recipe(self.book())
        paper = ResourceKey.item("minecraft:paper")
        leather = ResourceKey.item("minecraft:leather")
        book = ResourceKey.item("minecraft:book")
        for inputs in ([entry(paper), entry(paper), entry(leather)],
                       [entry(paper)] * 4 + [entry(leather)],
                       [entry(paper)] * 3 + [entry(leather), entry(LOG)]):
            self.assert_refused_without_edit({"mode": "crafting", "inputs": inputs,
                                             "outputs": [entry(book)]})

    def test_unvalidated_recipe_source_cannot_truncate_an_oversized_import(self):
        book = ResourceKey.item("minecraft:book")
        self.recipes.by_output[book.name] = [PresetRecipe(
            False, (), {}, (RecipeIngredient("minecraft:paper", 0, 10),),
            (RecipeIngredient(book.name, 0),))]
        before = self.config.to_dict()
        revision = self.state.revision
        result = self.send({"action": "template_import", "key": book.to_dict()})
        self.assertFalse(result.is_applied())
        self.assertEqual([Reason.CRAFT_NO_RECIPE], self.reasons(result))
        self.assertEqual(before, self.config.to_dict())
        self.assertEqual(revision, self.state.revision)
        self.assertEqual([], self.inventory.calls)


if __name__ == "__main__":
    unittest.main()
