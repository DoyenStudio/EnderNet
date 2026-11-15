# coding=utf-8
"""生成生存配方、矿物世界生成/掉落和新增内容语言名。"""

import argparse
import io
import os
import sys

from gen_blocks import REPO, write_json
from endernet.common.definitions import MACHINE_BLOCKS, MATERIAL_ITEMS, ORE_BLOCKS, MEDIUMS
from endernet.common.materials import CRAFT_RECIPES, ORE_ID


def recipe_json(recipe):
    if recipe.kind == "furnace":
        return {"format_version": "1.20.10", "minecraft:recipe_furnace": {
            "description": {"identifier": recipe.id}, "tags": ["furnace"],
            "input": recipe.inputs[0][0], "output": recipe.output,
        }}
    ingredients = []
    for name, count, data in recipe.inputs:
        entry = {"tag": name[1:]} if name.startswith("#") else {"item": name}
        if data is not None:
            entry["data"] = data
        ingredients.extend(dict(entry) for _ in range(count))
    if len(ingredients) > 9:
        raise ValueError("配方 %s 超过九宫格" % recipe.id)
    return {"format_version": "1.20.10", "minecraft:recipe_shapeless": {
        "description": {"identifier": recipe.id}, "tags": ["crafting_table"],
        "unlock": {"context": "AlwaysUnlocked"}, "ingredients": ingredients,
        "result": {"item": recipe.output, "data": 0, "count": recipe.count},
    }}


def ore_feature():
    return {"format_version": "1.14.0", "minecraft:ore_feature": {
        "description": {"identifier": "endernet:end_resonance_ore_feature"}, "count": 8,
        "replace_rules": [{"places_block": ORE_ID, "may_replace": ["minecraft:end_stone"]}],
    }}


def ore_rule():
    return {"format_version": "1.14.0", "minecraft:feature_rules": {
        "description": {"identifier": "endernet:end_resonance_ore_rule",
                        "places_feature": "endernet:end_resonance_ore_feature"},
        "conditions": {"placement_pass": "underground_pass",
                       "minecraft:biome_filter": [{"test": "has_biome_tag", "operator": "==", "value": "the_end"}]},
        "distribution": {"iterations": 6, "coordinate_eval_order": "yxz",
                         "x": {"distribution": "uniform", "extent": [0, 16]},
                         "y": {"distribution": "uniform", "extent": [32, 72]},
                         "z": {"distribution": "uniform", "extent": [0, 16]}},
    }}


def ore_loot():
    # NetEase handles silk-touch self drops and netease:tier before this table.
    # A match_tool silk_touch level-0 condition rejects unenchanted pickaxes.
    return {"pools": [{"rolls": 1, "entries": [
        {"type": "item", "name": "endernet:resonance_crystal", "weight": 1}]}]}


def generated_files():
    result = {}
    for recipe in CRAFT_RECIPES:
        path = "endernet_bp/netease_recipes/" + recipe.id.replace(":", "_") + ".json"
        if path in result:
            raise ValueError("配方标识重复: %s" % recipe.id)
        result[path] = recipe_json(recipe)
    result["endernet_bp/netease_features/end_resonance_ore_feature.json"] = ore_feature()
    result["endernet_bp/netease_feature_rules/end_resonance_ore_rule.json"] = ore_rule()
    result["endernet_bp/loot_tables/endernet/end_resonance_ore.json"] = ore_loot()
    return result


def localization():
    return dict(
        [("item.%s.name" % spec.id, spec.label) for spec in MATERIAL_ITEMS + MEDIUMS]
        + [("tile.%s.name" % spec.id, spec.label) for spec in MACHINE_BLOCKS]
        + [("tile.%s.name" % block_id, label) for block_id, label in ORE_BLOCKS]
    )


def update_language(check):
    path = os.path.join(REPO, "endernet_rp", "texts", "zh_CN.lang")
    with io.open(path, "r", encoding="utf-8") as handle:
        old = handle.read()
    remaining = localization()
    lines = []
    for line in old.splitlines():
        key = line.partition("=")[0]
        if key in remaining:
            line = key + "=" + remaining.pop(key)
        lines.append(line)
    lines.extend(key + "=" + value for key, value in sorted(remaining.items()))
    new = "\n".join(lines) + "\n"
    if new == old:
        return False
    if not check:
        with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(new)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成 EnderNet 生存内容")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    changed = []
    for relative, payload in sorted(generated_files().items()):
        if write_json(os.path.join(REPO, relative), payload, args.check):
            changed.append(relative)
    if update_language(args.check):
        changed.append("endernet_rp/texts/zh_CN.lang")
    print("Materials: %d recipes, %d changed files" % (len(CRAFT_RECIPES), len(changed)))
    if args.check and changed:
        for path in changed:
            print(path)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
