# coding=utf-8
from MEIScripts.api.context import ExtensionContext
from endernet_scripts.endernet.common.materials import MACHINE_BLOCKS, MACHINE_RECIPES
ctx = ExtensionContext()
manager = ctx.Registry.RecipeManager
report = {'categories': {}, 'outputs': [], 'uses': [], 'stations': []}
for machine in MACHINE_BLOCKS:
    recipes, complete = manager.QueryAll(machine.id)
    report['categories'][machine.id] = {'count': len(recipes), 'complete': complete, 'ids': [r.Id for r in recipes]}
    item = ctx.Registry.ItemFactory.GetItem(ItemId=machine.id, AuxValue=0)
    report['stations'].append([machine.id, len(manager.QueryRecipe(item, 'input', [machine.id]).get(machine.id, []))])
for spec in MACHINE_RECIPES:
    item = ctx.Registry.ItemFactory.GetItem(ItemId=spec.output, AuxValue=0)
    found = manager.QueryRecipe(item, 'output', [spec.machine]).get(spec.machine, [])
    recipe = next(r for r in found if r.Id == spec.id)
    report['outputs'].append({'id': recipe.Id, 'found': True, 'data': recipe.Data})
for name, aux, expected in [('minecraft:coal', 0, 1), ('minecraft:coal', 1, 1), ('minecraft:charcoal', 0, 1), ('minecraft:coal', 2, 0)]:
    item = ctx.Registry.ItemFactory.GetItem(ItemId=name, AuxValue=aux)
    found = manager.QueryRecipe(item, 'input', ['endernet:resonance_generator'])
    count = len(found.get('endernet:resonance_generator', []))
    assert count == expected
    report['uses'].append([name, aux, count, expected])
_result = report
