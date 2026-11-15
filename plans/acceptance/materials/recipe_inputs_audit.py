# coding=utf-8
"""Read-only native recipe audit (run with Python 3.11 -B).

1. Run this file with ``prepare``.
2. mcdk call execute_code --session SESSION --args-file
   .mcdev/materials-debug/recipe_inputs_args.json --output
   .mcdev/materials-debug/recipe_inputs_result.json --timeout 45
3. Run this file with ``check`` to save recipe_inputs_audit_result.json.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import gen_materials

BASE = Path(__file__).resolve().parent
DEBUG = ROOT / ".mcdev" / "materials-debug"
RECIPES = [recipe for recipe in gen_materials.CRAFT_RECIPES if recipe.kind != "furnace"]


def prepare():
    DEBUG.mkdir(parents=True, exist_ok=True)
    code = "\n".join([
        "import mod.server.extraServerApi as recipe_audit_api",
        "recipe_audit_comp = recipe_audit_api.GetEngineCompFactory().CreateRecipe(recipe_audit_api.GetLevelId())",
        "recipe_audit_ids = " + repr([recipe.id for recipe in RECIPES]),
        "_result = {recipe_id: recipe_audit_comp.GetRecipeByRecipeId(recipe_id, 'crafting_table') for recipe_id in recipe_audit_ids}",
    ])
    (DEBUG / "recipe_inputs_probe.py").write_text("# coding=utf-8\n" + code + "\n", encoding="utf-8")
    (DEBUG / "recipe_inputs_args.json").write_text(json.dumps({"code": code, "is_client": False}), encoding="utf-8")


def canonical(name, data):
    # The native recipe loader resolves legacy dye metadata to modern IDs.
    if name == "minecraft:dye":
        name, data = {4: ("minecraft:lapis_lazuli", 0), 6: ("minecraft:cyan_dye", 0)}.get(data, (name, data))
    return name, data


def check():
    envelope = json.loads((DEBUG / "recipe_inputs_result.json").read_text(encoding="utf-8"))
    text = envelope["content"][0]["text"]
    actual = json.loads(text.split("Return value JSON: ", 1)[1])
    rows = []
    for recipe in RECIPES:
        found = actual.get(recipe.id)
        expected_inputs = Counter()
        for name, count, data in recipe.inputs:
            expected_inputs[canonical(name, data)] += count
        actual_inputs = Counter()
        tags = []
        for entry in (found or {}).get("ingredients", []):
            name = entry["item"]
            data = entry.get("data", None if name == "minecraft:chest" else 0)
            if isinstance(name, list):
                tags.append(name)
                if name and all(item.startswith("minecraft:") and item.endswith("_planks") for item in name) and "minecraft:oak_planks" in name:
                    name, data = "#minecraft:planks", None
                else:
                    name = tuple(name)
            actual_inputs[canonical(name, data)] += entry.get("count", 1)
        outputs = [(entry["item"], entry.get("count", 1), entry.get("data", 0)) for entry in (found or {}).get("result", [])]
        row = {"id": recipe.id, "pass": expected_inputs == actual_inputs and outputs == [(recipe.output, recipe.count, 0)],
               "expected_slots": sum(expected_inputs.values()), "actual_slots": sum(actual_inputs.values()),
               "expected_output": [recipe.output, recipe.count, 0], "actual_outputs": outputs}
        if tags:
            row["expanded_tags"] = tags
        if expected_inputs != actual_inputs:
            row["expected_inputs"] = [(name, data, count) for (name, data), count in expected_inputs.items()]
            row["actual_inputs"] = [(name, data, count) for (name, data), count in actual_inputs.items()]
        rows.append(row)
    result = {"recipes": rows, "total": len(rows), "passed": sum(row["pass"] for row in rows),
              "normalization": {"minecraft:dye:4": "minecraft:lapis_lazuli:0", "minecraft:dye:6": "minecraft:cyan_dye:0"}}
    (BASE / "recipe_inputs_audit_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "recipes"}, indent=2))
    for row in rows:
        if not row["pass"]:
            print(json.dumps(row, indent=2))
    return 0 if result["passed"] == result["total"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "check"))
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare()
    else:
        sys.exit(check())
