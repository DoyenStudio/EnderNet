# coding=utf-8
"""生存内容的可达性、状态安全及生成文件回归。"""

import json
import os
import sys
import unittest

import testsupport

from endernet.common.definitions import DefinitionSet, MATERIAL_ITEMS, MACHINE_BLOCKS, ORE_BLOCKS
from endernet.common.materials import CRAFT_RECIPES, MACHINE_RECIPES, ORE_ID, GENERATOR_ID

SCRIPTS = os.path.join(testsupport.REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
import gen_materials  # noqa: E402
import gen_blocks  # noqa: E402


class TestSurvivalRecipes(unittest.TestCase):
    def test_recipe_ids_and_ingredient_multisets_are_unique(self):
        ids = [recipe.id for recipe in CRAFT_RECIPES]
        self.assertEqual(len(ids), len(set(ids)))
        keys = [(recipe.kind, tuple(sorted(recipe.inputs))) for recipe in CRAFT_RECIPES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_crafting_slots_and_batch_yields(self):
        outputs = {}
        for recipe in CRAFT_RECIPES:
            self.assertGreater(recipe.count, 0)
            self.assertLessEqual(sum(count for _, count, _ in recipe.inputs), 9)
            outputs[recipe.output] = recipe.count
        for short, count in (("ender_glass_cable", 8), ("ender_cable", 4),
                             ("ender_dense_cable", 2), ("p2p_network_tunnel", 2),
                             ("factorical_template", 4), ("end_casing", 2)):
            self.assertEqual(outputs["endernet:" + short], count)

    def test_all_registered_survival_content_has_a_reachable_recipe(self):
        defs = DefinitionSet()
        targets = set(defs.devices) | set(defs.cables) | set(defs.parts) | set(defs.item_keys())
        targets.update(spec.id for spec in MACHINE_BLOCKS)
        targets.difference_update(("endernet:creative_energy_source", "endernet:network_visualizer"))
        # 矿石由世界生成提供，原版材料由原版玩法提供。
        available = {ORE_ID}
        all_recipes = [(r.output, [name for name, _, _ in r.inputs]) for r in CRAFT_RECIPES]
        all_recipes += [(r.output, [name for name, _ in r.inputs] + [r.machine]) for r in MACHINE_RECIPES]
        for _output, inputs in all_recipes:
            available.update(name for name in inputs if name.startswith(("minecraft:", "#minecraft:")))
        while True:
            before = len(available)
            for output, inputs in all_recipes:
                if all(name in available for name in inputs):
                    available.add(output)
            if len(available) == before:
                break
        self.assertEqual(targets - available, set())

    def test_stateful_products_are_never_consumed_by_native_recipes(self):
        defs = DefinitionSet()
        forbidden = set(defs.devices) | set(defs.parts) | set(defs.item_mediums) | set(defs.item_templates) | set(defs.item_tools)
        forbidden.difference_update(("endernet:network_baseband_8", "endernet:network_baseband_16"))
        for recipe in CRAFT_RECIPES:
            self.assertFalse(forbidden.intersection(name for name, _, _ in recipe.inputs), recipe.id)

    def test_four_terminal_shapes_are_distinct_outputs(self):
        recipes = dict((r.output, r) for r in CRAFT_RECIPES)
        for kind in ("common", "crafting", "template", "factoric"):
            part = recipes["endernet:" + kind + "_terminal_part"]
            block = recipes["endernet:" + kind + "_terminal_panel"]
            self.assertNotEqual(part.id, block.id)
            self.assertEqual(block.inputs, part.inputs + (("endernet:end_casing", 1, 0),))

    def test_netease_dye_data_and_planks_tag_are_explicit(self):
        recipes = dict((r.output, r) for r in CRAFT_RECIPES)
        self.assertIn(("minecraft:dye", 1, 6), recipes["endernet:resonance_glass"].inputs)
        self.assertIn(("minecraft:dye", 1, 4), recipes["endernet:fluid_seal"].inputs)
        payload = gen_materials.recipe_json(recipes["endernet:singleside_tape"])
        self.assertIn({"tag": "minecraft:planks"}, payload["minecraft:recipe_shapeless"]["ingredients"])

    def test_chests_use_vanilla_unrestricted_ingredient_metadata(self):
        count = 0
        for recipe in CRAFT_RECIPES:
            payload = gen_materials.recipe_json(recipe)
            if recipe.kind != "crafting":
                continue
            for ingredient in payload["minecraft:recipe_shapeless"]["ingredients"]:
                if ingredient.get("item") == "minecraft:chest":
                    count += 1
                    self.assertNotIn("data", ingredient, recipe.id)
        self.assertGreater(count, 0)


class TestProductionAssets(unittest.TestCase):
    def test_ore_normal_loot_does_not_require_a_nonexistent_enchantment(self):
        pools = gen_materials.ore_loot()["pools"]
        self.assertEqual(len(pools), 1)
        self.assertNotIn("conditions", pools[0])
        self.assertEqual(pools[0]["rolls"], 1)
        self.assertEqual(pools[0]["entries"], [
            {"type": "item", "name": "endernet:resonance_crystal", "weight": 1}])

    def test_feature_filenames_match_their_identifiers(self):
        for relative, payload in gen_materials.generated_files().items():
            if "/netease_features/" in relative:
                body = payload["minecraft:ore_feature"]
            elif "/netease_feature_rules/" in relative:
                body = payload["minecraft:feature_rules"]
            else:
                continue
            short_id = body["description"]["identifier"].split(":", 1)[1]
            self.assertEqual(os.path.basename(relative), short_id + ".json")

    def test_all_generated_files_match_source(self):
        for relative, expected in gen_materials.generated_files().items():
            path = os.path.join(testsupport.REPO_ROOT, relative)
            with open(path, "rb") as handle:
                actual = json.loads(handle.read().decode("utf-8"))
            self.assertEqual(actual, expected, relative)

    def test_only_generator_enters_network_catalog_and_native_containers_remain(self):
        defs = DefinitionSet()
        for spec in MACHINE_BLOCKS:
            self.assertEqual(defs.has(spec.id), spec.id == GENERATOR_ID)
            payload = gen_blocks.build_independent_machine_json(spec)["minecraft:block"]
            self.assertEqual(payload["components"]["netease:block_container"]["container_size"], spec.slots)
            self.assertTrue(payload["components"]["netease:block_entity"]["tick"])

    def test_ore_only_replaces_end_stone_in_end_biomes(self):
        self.assertEqual(gen_materials.ore_feature()["format_version"], "1.14.0")
        ore = gen_materials.ore_feature()["minecraft:ore_feature"]
        self.assertEqual(ore["replace_rules"], [{"places_block": ORE_ID, "may_replace": ["minecraft:end_stone"]}])
        rule = gen_materials.ore_rule()["minecraft:feature_rules"]
        self.assertEqual(rule["conditions"]["minecraft:biome_filter"][0]["value"], "the_end")
        self.assertEqual(rule["distribution"]["y"]["extent"], [32, 72])
        self.assertEqual(gen_blocks.build_ore_json(ORE_ID)["minecraft:block"]["components"]["netease:tier"]["level"], 2)

    def test_source_materials_and_blocks_are_named(self):
        names = gen_materials.localization()
        for spec in MATERIAL_ITEMS:
            self.assertEqual(names["item.%s.name" % spec.id], spec.label)
        for block_id, label in ORE_BLOCKS:
            self.assertEqual(names["tile.%s.name" % block_id], label)
