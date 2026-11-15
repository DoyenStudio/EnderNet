# coding=utf-8
import sys, time, __builtin__
from endernet_scripts.tooldelta.api.server.block import SetBlock, GetBlockName
production = next(m for n,m in sys.modules.items() if n.endswith("endernet.server.adapters.production"))
service = production.get_production()
probe = {"dimension":0,"started_epoch":time.time(),"execution":"native BlockEntityTickEvent; no direct machine.tick calls","cases":[]}
__builtin__._endernet_recipe_probe = probe
for index, recipe in enumerate(production.MACHINE_RECIPES):
    pos = ((64,66,68,70)[index % 4],63,36 if index < 4 else 39)
    gen_pos = (pos[0],63,pos[2]+1)
    case = {"recipe":recipe.id,"machine":recipe.machine,"mode":recipe.mode,"pos":list(pos),"generator_pos":list(gen_pos),"expected_inputs":list(recipe.inputs),"expected_output":{"name":recipe.output,"count":recipe.count},"original_blocks":[GetBlockName(0,pos),GetBlockName(0,gen_pos)]}
    probe["cases"].append(case)
    case["placed"] = [SetBlock(0,pos,recipe.machine),SetBlock(0,gen_pos,"endernet:resonance_generator")]
    machine = service.load(0,pos)
    generator = service.load(0,gen_pos)
    case["loaded"] = machine is not None and generator is not None
    if not case["loaded"]:
        continue
    if recipe.machine.endswith("inscriber"):
        case["mode_selected"] = machine.set_mode(recipe.mode)
    slots = 2 if recipe.machine.endswith("charger") else 4
    case["input_writes"] = []
    for slot,(name,count) in enumerate(recipe.inputs):
        case["input_writes"].append(machine.inventory.write(slot,{"newItemName":name,"newAuxValue":0,"count":count}))
    case["before"] = {"slots":[machine.inventory.read(s) for s in range(slots)],"state":service.snapshot(0,pos)}
    case["fuel_write"] = generator.inventory.write(0,{"newItemName":"minecraft:coal","newAuxValue":0,"count":1})
    case["generator_before"] = service.snapshot(0,gen_pos)
    case["start_tick"] = service.ticks.get(machine.inventory.key,0)
_result = probe
