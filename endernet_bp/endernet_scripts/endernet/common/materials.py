# coding=utf-8
"""生存材料、独立加工机与配方的单一数据源（materials.md）。

机器不属于仓储网络设备，不参与拓扑或带宽准入。
"""


class MaterialSpec(object):
    __slots__ = ("id", "label", "tip", "max_stack_size")

    def __init__(self, name, label, tip):
        self.id = "endernet:" + name
        self.label = label
        self.tip = tip
        self.max_stack_size = 64


MATERIAL_ITEMS = (
    MaterialSpec("resonance_crystal", "谐振水晶", "从末地谐振矿石中取得。"),
    MaterialSpec("ender_dust", "末影粉", "传输核心与磁性带基的材料。"),
    MaterialSpec("charged_resonance_crystal", "充能谐振水晶", "可手工充能，也可使用谐振充能器。"),
    MaterialSpec("resonance_glass", "谐振玻璃", "青蓝色光学组件。"),
    MaterialSpec("end_casing", "末地机壳", "末地石骨架、石英饰面、铁质边框。"),
    MaterialSpec("reinforced_end_casing", "强化末地机壳", "黑石外甲、紫珀内衬与石英边框。"),
    MaterialSpec("quartz_dust", "石英粉", "在熔炉中烧制为硅片。"),
    MaterialSpec("silicon", "硅片", "芯片封装的共同基底。"),
    MaterialSpec("magnetic_strip", "磁性带基", "磁带的存储载体。"),
    MaterialSpec("logic_circuit", "逻辑电路", "金质压印电路，尚未封装。"),
    MaterialSpec("calculation_circuit", "运算电路", "紫水晶运算电路，尚未封装。"),
    MaterialSpec("engineering_circuit", "工程电路", "钻石压印电路，尚未封装。"),
    MaterialSpec("logic_chip", "逻辑芯片", "基础仓储、筛选与显示控制。"),
    MaterialSpec("calculation_chip", "运算芯片", "配方计算、存储升级与内存。"),
    MaterialSpec("engineering_chip", "工程芯片", "机械执行、并行与远距控制。"),
    MaterialSpec("convergence_core", "收束核心", "外界到网络的存货功能核心。"),
    MaterialSpec("expansion_core", "展开核心", "网络到外界的取货功能核心。"),
    MaterialSpec("display_component", "显示组件", "终端的显示与输入组件。"),
    MaterialSpec("fluid_seal", "流体密封组件", "青金石密封环与流体观察窗。"),
    MaterialSpec("wireless_resonance_core", "无线谐振核心", "无线连接器的远距谐振组件。"),
    MaterialSpec("medium_casing", "介质外壳", "硬盘和高密磁带的外壳。"),
    MaterialSpec("storage_component_1k", "1K 存储组件", "用于制造 1K 软盘和硬盘。"),
    MaterialSpec("storage_component_4k", "4K 存储组件", "用于制造 4K 软盘和硬盘。"),
    MaterialSpec("storage_component_16k", "16K 存储组件", "用于制造 16K 硬盘和高密归档核心。"),
    MaterialSpec("archive_core", "高密归档核心", "数据库高密磁带的核心。"),
)


class MachineSpec(object):
    __slots__ = ("id", "label", "slots", "screen", "tip")

    def __init__(self, name, label, slots, tip):
        self.id = "endernet:" + name
        self.label = label
        self.slots = slots
        self.screen = "endernet_" + name + ".main"
        self.tip = tip


MACHINE_BLOCKS = (
    MachineSpec("resonance_charger", "谐振充能器", 2, "独立充能：32 FE/t，每次 4 秒。"),
    MachineSpec("resonance_inscriber", "谐振压印机", 4, "粉碎、压印和封装：32 FE/t，每次 4 秒。"),
    MachineSpec("resonance_generator", "谐振燃料发电机", 1, "煤炭或木炭发电，六面合计 64 FE/t。"),
)
ORE_BLOCKS = (("endernet:end_resonance_ore", "末地谐振矿石"),)
ORE_ID = ORE_BLOCKS[0][0]

# Shared by the authoritative generator and optional recipe viewers.
GENERATOR_RATE = 64
FUEL_TICKS = 1600
GENERATOR_FUELS = (("minecraft:coal", 0), ("minecraft:coal", 1),
                   ("minecraft:charcoal", 0))


class MachineRecipe(object):
    __slots__ = ("id", "machine", "mode", "inputs", "output", "count", "duration_ticks", "power_fe")

    def __init__(self, recipe_id, machine, mode, inputs, output, count=1):
        self.id = "endernet:" + recipe_id
        self.machine = "endernet:" + machine
        self.mode = mode
        self.inputs = tuple(inputs)
        self.output = "endernet:" + output
        self.count = count
        self.duration_ticks = 80
        self.power_fe = 32


MACHINE_RECIPES = (
    MachineRecipe("charge_crystal", "resonance_charger", "charge", (("endernet:resonance_crystal", 1),), "charged_resonance_crystal"),
    MachineRecipe("grind_quartz", "resonance_inscriber", "grind", (("minecraft:quartz", 1),), "quartz_dust", 2),
    MachineRecipe("press_logic", "resonance_inscriber", "press", (("minecraft:gold_ingot", 1),), "logic_circuit"),
    MachineRecipe("press_calculation", "resonance_inscriber", "press", (("endernet:resonance_crystal", 1), ("minecraft:amethyst_shard", 1)), "calculation_circuit"),
    MachineRecipe("press_engineering", "resonance_inscriber", "press", (("minecraft:diamond", 1),), "engineering_circuit"),
) + tuple(
    MachineRecipe("assemble_" + kind, "resonance_inscriber", "assemble",
                  (("endernet:" + kind + "_circuit", 1), ("endernet:silicon", 1), ("minecraft:redstone", 1)), kind + "_chip")
    for kind in ("logic", "calculation", "engineering")
)


class CraftRecipe(object):
    __slots__ = ("id", "output", "count", "inputs", "kind")

    def __init__(self, recipe_id, output, count, inputs, kind="crafting"):
        self.id = "endernet:" + recipe_id
        self.output = output
        self.count = count
        self.inputs = tuple(inputs)
        self.kind = kind


# 三元输入 (identifier, count, data)；data=None 表示任意辅助值。
# 木板使用原版 planks 标签，避免限制到橡木。
CRAFT_RECIPES = (
    CraftRecipe('ender_dust_craft', 'endernet:ender_dust', 1, (('minecraft:ender_pearl', 1, 0), ('minecraft:redstone', 1, 0)), 'crafting'),
    CraftRecipe('charged_resonance_crystal_craft', 'endernet:charged_resonance_crystal', 1, (('endernet:resonance_crystal', 1, 0), ('minecraft:redstone', 1, 0), ('minecraft:glowstone_dust', 1, 0)), 'crafting'),
    CraftRecipe('resonance_glass_craft', 'endernet:resonance_glass', 4, (('minecraft:glass', 4, 0), ('minecraft:quartz', 2, 0), ('minecraft:amethyst_shard', 1, 0), ('endernet:resonance_crystal', 1, 0), ('minecraft:dye', 1, 6)), 'crafting'),
    CraftRecipe('end_casing_craft', 'endernet:end_casing', 2, (('minecraft:end_stone', 2, 0), ('minecraft:quartz', 4, 0), ('minecraft:iron_ingot', 2, 0)), 'crafting'),
    CraftRecipe('reinforced_end_casing_craft', 'endernet:reinforced_end_casing', 1, (('endernet:end_casing', 1, 0), ('minecraft:blackstone', 2, 0), ('minecraft:purpur_block', 2, 0), ('endernet:charged_resonance_crystal', 2, 0), ('minecraft:iron_ingot', 2, 0)), 'crafting'),
    CraftRecipe('silicon_craft', 'endernet:silicon', 1, (('endernet:quartz_dust', 1, 0),), 'furnace'),
    CraftRecipe('magnetic_strip_craft', 'endernet:magnetic_strip', 4, (('minecraft:paper', 2, 0), ('minecraft:iron_ingot', 1, 0), ('endernet:ender_dust', 1, 0)), 'crafting'),
    CraftRecipe('convergence_core_craft', 'endernet:convergence_core', 2, (('endernet:logic_chip', 1, 0), ('endernet:ender_dust', 1, 0), ('endernet:resonance_crystal', 1, 0)), 'crafting'),
    CraftRecipe('expansion_core_craft', 'endernet:expansion_core', 2, (('endernet:logic_chip', 1, 0), ('endernet:ender_dust', 1, 0), ('minecraft:quartz', 1, 0)), 'crafting'),
    CraftRecipe('display_component_craft', 'endernet:display_component', 2, (('endernet:resonance_glass', 2, 0), ('minecraft:redstone', 2, 0), ('minecraft:glowstone_dust', 1, 0), ('endernet:logic_chip', 1, 0)), 'crafting'),
    CraftRecipe('fluid_seal_craft', 'endernet:fluid_seal', 2, (('minecraft:bucket', 1, 0), ('endernet:resonance_glass', 2, 0), ('minecraft:iron_ingot', 1, 0), ('minecraft:dye', 1, 4)), 'crafting'),
    CraftRecipe('wireless_resonance_core_craft', 'endernet:wireless_resonance_core', 1, (('endernet:charged_resonance_crystal', 2, 0), ('minecraft:ender_eye', 1, 0), ('endernet:engineering_chip', 1, 0), ('minecraft:purpur_block', 2, 0)), 'crafting'),
    CraftRecipe('medium_casing_craft', 'endernet:medium_casing', 1, (('minecraft:iron_ingot', 4, 0), ('endernet:resonance_glass', 2, 0), ('minecraft:redstone', 1, 0)), 'crafting'),
    CraftRecipe('resonance_charger_craft', 'endernet:resonance_charger', 1, (('endernet:end_casing', 1, 0), ('minecraft:iron_ingot', 2, 0), ('minecraft:redstone', 2, 0), ('endernet:resonance_crystal', 2, 0)), 'crafting'),
    CraftRecipe('resonance_inscriber_craft', 'endernet:resonance_inscriber', 1, (('endernet:end_casing', 1, 0), ('minecraft:piston', 2, 0), ('minecraft:iron_ingot', 2, 0), ('endernet:resonance_crystal', 2, 0), ('minecraft:redstone', 1, 0)), 'crafting'),
    CraftRecipe('resonance_generator_craft', 'endernet:resonance_generator', 1, (('endernet:end_casing', 1, 0), ('minecraft:furnace', 1, 0), ('minecraft:iron_ingot', 2, 0), ('minecraft:redstone', 2, 0), ('endernet:resonance_crystal', 1, 0)), 'crafting'),
    CraftRecipe('dos_network_endpoint_craft', 'endernet:dos_network_endpoint', 1, (('endernet:end_casing', 1, 0), ('endernet:logic_chip', 2, 0), ('endernet:charged_resonance_crystal', 2, 0), ('minecraft:ender_eye', 1, 0), ('endernet:resonance_glass', 1, 0)), 'crafting'),
    CraftRecipe('tiny_network_endpoint_craft', 'endernet:tiny_network_endpoint', 1, (('endernet:reinforced_end_casing', 1, 0), ('endernet:logic_chip', 2, 0), ('endernet:calculation_chip', 2, 0), ('endernet:engineering_chip', 1, 0), ('minecraft:ender_eye', 1, 0), ('endernet:charged_resonance_crystal', 1, 0), ('endernet:resonance_glass', 1, 0)), 'crafting'),
    CraftRecipe('network_baseband_8_craft', 'endernet:network_baseband_8', 1, (('endernet:end_casing', 1, 0), ('minecraft:blackstone', 2, 0), ('endernet:charged_resonance_crystal', 2, 0), ('minecraft:redstone', 2, 0), ('endernet:resonance_glass', 1, 0)), 'crafting'),
    CraftRecipe('network_baseband_16_craft', 'endernet:network_baseband_16', 1, (('endernet:network_baseband_8', 1, 0), ('endernet:calculation_chip', 1, 0), ('endernet:charged_resonance_crystal', 2, 0), ('minecraft:purpur_block', 2, 0)), 'crafting'),
    CraftRecipe('network_baseband_32_craft', 'endernet:network_baseband_32', 1, (('endernet:network_baseband_16', 1, 0), ('endernet:engineering_chip', 1, 0), ('endernet:charged_resonance_crystal', 4, 0), ('minecraft:amethyst_shard', 2, 0)), 'crafting'),
    CraftRecipe('energy_input_interface_craft', 'endernet:energy_input_interface', 1, (('endernet:end_casing', 1, 0), ('minecraft:iron_ingot', 2, 0), ('minecraft:redstone', 2, 0), ('endernet:charged_resonance_crystal', 2, 0), ('minecraft:amethyst_shard', 1, 0)), 'crafting'),
    CraftRecipe('ender_glass_cable_craft', 'endernet:ender_glass_cable', 8, (('endernet:resonance_glass', 2, 0), ('endernet:charged_resonance_crystal', 1, 0)), 'crafting'),
    CraftRecipe('ender_cable_craft', 'endernet:ender_cable', 4, (('endernet:ender_glass_cable', 4, 0), ('minecraft:iron_ingot', 1, 0), ('minecraft:redstone', 1, 0)), 'crafting'),
    CraftRecipe('ender_dense_cable_craft', 'endernet:ender_dense_cable', 2, (('endernet:ender_cable', 4, 0), ('endernet:charged_resonance_crystal', 1, 0), ('minecraft:amethyst_shard', 1, 0)), 'crafting'),
    CraftRecipe('p2p_network_tunnel_craft', 'endernet:p2p_network_tunnel', 2, (('endernet:ender_cable', 2, 0), ('endernet:convergence_core', 1, 0), ('endernet:expansion_core', 1, 0), ('endernet:engineering_chip', 1, 0), ('minecraft:ender_eye', 1, 0)), 'crafting'),
    CraftRecipe('wireless_connector_craft', 'endernet:wireless_connector', 1, (('endernet:reinforced_end_casing', 1, 0), ('endernet:ender_dense_cable', 2, 0), ('endernet:wireless_resonance_core', 1, 0), ('endernet:engineering_chip', 1, 0)), 'crafting'),
    CraftRecipe('bus_item_input_io_craft', 'endernet:bus_item_input_io', 1, (('endernet:end_casing', 1, 0), ('endernet:convergence_core', 1, 0), ('minecraft:hopper', 1, 0), ('endernet:ender_glass_cable', 1, 0)), 'crafting'),
    CraftRecipe('bus_item_output_io_craft', 'endernet:bus_item_output_io', 1, (('endernet:end_casing', 1, 0), ('endernet:expansion_core', 1, 0), ('minecraft:piston', 1, 0), ('endernet:ender_glass_cable', 1, 0)), 'crafting'),
    CraftRecipe('bus_item_io_craft', 'endernet:bus_item_io', 1, (('endernet:end_casing', 1, 0), ('endernet:convergence_core', 1, 0), ('endernet:expansion_core', 1, 0), ('minecraft:chest', 1, None), ('endernet:ender_glass_cable', 1, 0)), 'crafting'),
    CraftRecipe('bus_fluid_input_io_craft', 'endernet:bus_fluid_input_io', 1, (('endernet:end_casing', 1, 0), ('endernet:convergence_core', 1, 0), ('endernet:fluid_seal', 1, 0), ('endernet:ender_glass_cable', 1, 0)), 'crafting'),
    CraftRecipe('bus_fluid_output_io_craft', 'endernet:bus_fluid_output_io', 1, (('endernet:end_casing', 1, 0), ('endernet:expansion_core', 1, 0), ('endernet:fluid_seal', 1, 0), ('endernet:ender_glass_cable', 1, 0)), 'crafting'),
    CraftRecipe('bus_fluid_io_craft', 'endernet:bus_fluid_io', 1, (('endernet:end_casing', 1, 0), ('endernet:convergence_core', 1, 0), ('endernet:expansion_core', 1, 0), ('endernet:fluid_seal', 2, 0), ('endernet:ender_glass_cable', 1, 0)), 'crafting'),
    CraftRecipe('bus_template_io_craft', 'endernet:bus_template_io', 1, (('endernet:end_casing', 1, 0), ('endernet:convergence_core', 1, 0), ('endernet:expansion_core', 1, 0), ('minecraft:chest', 1, None), ('endernet:ender_glass_cable', 1, 0), ('endernet:calculation_chip', 1, 0), ('endernet:engineering_chip', 1, 0), ('endernet:silicon', 1, 0)), 'crafting'),
    CraftRecipe('storage_driver_craft', 'endernet:storage_driver', 1, (('endernet:end_casing', 1, 0), ('minecraft:chest', 2, None), ('endernet:logic_chip', 2, 0), ('endernet:resonance_glass', 2, 0)), 'crafting'),
    CraftRecipe('transcriber_craft', 'endernet:transcriber', 1, (('endernet:end_casing', 1, 0), ('minecraft:chest', 2, None), ('endernet:logic_chip', 2, 0), ('endernet:convergence_core', 1, 0), ('endernet:expansion_core', 1, 0)), 'crafting'),
    CraftRecipe('storage_transfer_craft', 'endernet:storage_transfer', 1, (('endernet:end_casing', 1, 0), ('minecraft:chest', 2, None), ('endernet:logic_chip', 2, 0), ('endernet:convergence_core', 2, 0), ('endernet:calculation_chip', 1, 0), ('minecraft:hopper', 1, 0)), 'crafting'),
    CraftRecipe('common_terminal_part_craft', 'endernet:common_terminal_part', 1, (('endernet:display_component', 1, 0), ('endernet:logic_chip', 1, 0), ('endernet:ender_glass_cable', 1, 0)), 'crafting'),
    CraftRecipe('crafting_terminal_part_craft', 'endernet:crafting_terminal_part', 1, (('endernet:display_component', 1, 0), ('endernet:logic_chip', 1, 0), ('endernet:ender_glass_cable', 1, 0), ('minecraft:crafting_table', 1, 0), ('endernet:calculation_chip', 1, 0)), 'crafting'),
    CraftRecipe('template_terminal_part_craft', 'endernet:template_terminal_part', 1, (('endernet:display_component', 1, 0), ('endernet:logic_chip', 1, 0), ('endernet:ender_glass_cable', 1, 0), ('minecraft:crafting_table', 1, 0), ('endernet:calculation_chip', 1, 0), ('endernet:engineering_chip', 1, 0), ('endernet:silicon', 1, 0)), 'crafting'),
    CraftRecipe('factoric_terminal_part_craft', 'endernet:factoric_terminal_part', 1, (('endernet:display_component', 1, 0), ('endernet:logic_chip', 1, 0), ('endernet:ender_glass_cable', 1, 0), ('endernet:calculation_chip', 2, 0), ('endernet:engineering_chip', 1, 0)), 'crafting'),
    CraftRecipe('common_terminal_panel_craft', 'endernet:common_terminal_panel', 1, (('endernet:display_component', 1, 0), ('endernet:logic_chip', 1, 0), ('endernet:ender_glass_cable', 1, 0), ('endernet:end_casing', 1, 0)), 'crafting'),
    CraftRecipe('crafting_terminal_panel_craft', 'endernet:crafting_terminal_panel', 1, (('endernet:display_component', 1, 0), ('endernet:logic_chip', 1, 0), ('endernet:ender_glass_cable', 1, 0), ('minecraft:crafting_table', 1, 0), ('endernet:calculation_chip', 1, 0), ('endernet:end_casing', 1, 0)), 'crafting'),
    CraftRecipe('template_terminal_panel_craft', 'endernet:template_terminal_panel', 1, (('endernet:display_component', 1, 0), ('endernet:logic_chip', 1, 0), ('endernet:ender_glass_cable', 1, 0), ('minecraft:crafting_table', 1, 0), ('endernet:calculation_chip', 1, 0), ('endernet:engineering_chip', 1, 0), ('endernet:silicon', 1, 0), ('endernet:end_casing', 1, 0)), 'crafting'),
    CraftRecipe('factoric_terminal_panel_craft', 'endernet:factoric_terminal_panel', 1, (('endernet:display_component', 1, 0), ('endernet:logic_chip', 1, 0), ('endernet:ender_glass_cable', 1, 0), ('endernet:calculation_chip', 2, 0), ('endernet:engineering_chip', 1, 0), ('endernet:end_casing', 1, 0)), 'crafting'),
    CraftRecipe('parallel_processor_connector_craft', 'endernet:parallel_processor_connector', 1, (('endernet:end_casing', 1, 0), ('endernet:ender_cable', 2, 0), ('endernet:calculation_chip', 1, 0), ('minecraft:redstone', 2, 0)), 'crafting'),
    CraftRecipe('parallel_processor_craft', 'endernet:parallel_processor', 1, (('endernet:reinforced_end_casing', 1, 0), ('endernet:calculation_chip', 2, 0), ('endernet:engineering_chip', 1, 0), ('endernet:charged_resonance_crystal', 2, 0)), 'crafting'),
    CraftRecipe('memory_slot_craft', 'endernet:memory_slot', 1, (('endernet:end_casing', 1, 0), ('endernet:calculation_chip', 1, 0), ('minecraft:gold_ingot', 2, 0), ('minecraft:redstone', 2, 0), ('minecraft:chest', 1, None)), 'crafting'),
    CraftRecipe('assemble_chamber_craft', 'endernet:assemble_chamber', 1, (('endernet:end_casing', 1, 0), ('minecraft:crafting_table', 1, 0), ('endernet:convergence_core', 1, 0), ('endernet:expansion_core', 1, 0), ('endernet:engineering_chip', 1, 0), ('endernet:resonance_glass', 2, 0)), 'crafting'),
    CraftRecipe('network_wrench_craft', 'endernet:network_wrench', 1, (('minecraft:iron_ingot', 3, 0), ('endernet:resonance_crystal', 1, 0)), 'crafting'),
    CraftRecipe('config_card_craft', 'endernet:config_card', 1, (('minecraft:iron_ingot', 2, 0), ('endernet:logic_chip', 1, 0), ('minecraft:redstone', 1, 0)), 'crafting'),
    CraftRecipe('wireless_link_tool_craft', 'endernet:wireless_link_tool', 1, (('minecraft:iron_ingot', 2, 0), ('minecraft:ender_eye', 1, 0), ('endernet:logic_chip', 1, 0)), 'crafting'),
    CraftRecipe('factorical_template_craft', 'endernet:factorical_template', 4, (('endernet:resonance_glass', 2, 0), ('minecraft:iron_ingot', 1, 0), ('minecraft:redstone', 1, 0)), 'crafting'),
    CraftRecipe('storage_component_1k_craft', 'endernet:storage_component_1k', 1, (('endernet:logic_chip', 1, 0), ('endernet:resonance_crystal', 4, 0), ('minecraft:redstone', 4, 0)), 'crafting'),
    CraftRecipe('storage_component_4k_craft', 'endernet:storage_component_4k', 1, (('endernet:storage_component_1k', 3, 0), ('endernet:calculation_chip', 1, 0), ('endernet:charged_resonance_crystal', 2, 0), ('minecraft:redstone', 3, 0)), 'crafting'),
    CraftRecipe('storage_component_16k_craft', 'endernet:storage_component_16k', 1, (('endernet:storage_component_4k', 3, 0), ('endernet:calculation_chip', 1, 0), ('endernet:charged_resonance_crystal', 2, 0), ('minecraft:redstone', 3, 0)), 'crafting'),
    CraftRecipe('archive_core_craft', 'endernet:archive_core', 1, (('endernet:storage_component_16k', 4, 0), ('endernet:engineering_chip', 4, 0), ('minecraft:nether_star', 1, 0)), 'crafting'),
    CraftRecipe('singleside_tape_craft', 'endernet:singleside_tape', 1, (('endernet:magnetic_strip', 1, 0), ('minecraft:iron_ingot', 1, 0), ('#minecraft:planks', 1, None)), 'crafting'),
    CraftRecipe('doubleside_tape_craft', 'endernet:doubleside_tape', 1, (('endernet:magnetic_strip', 2, 0), ('minecraft:iron_ingot', 1, 0), ('#minecraft:planks', 1, None)), 'crafting'),
    CraftRecipe('database_compressed_tape_craft', 'endernet:database_compressed_tape', 1, (('endernet:archive_core', 1, 0), ('endernet:magnetic_strip', 4, 0), ('endernet:engineering_chip', 2, 0), ('minecraft:ender_eye', 1, 0), ('endernet:medium_casing', 1, 0)), 'crafting'),
    CraftRecipe('softdisk_1k_craft', 'endernet:softdisk_1k', 1, (('endernet:storage_component_1k', 1, 0), ('minecraft:paper', 2, 0), ('minecraft:iron_ingot', 2, 0)), 'crafting'),
    CraftRecipe('softdisk_4k_craft', 'endernet:softdisk_4k', 1, (('endernet:storage_component_4k', 1, 0), ('minecraft:paper', 2, 0), ('minecraft:iron_ingot', 2, 0)), 'crafting'),
    CraftRecipe('harddisk_1k_craft', 'endernet:harddisk_1k', 1, (('endernet:storage_component_1k', 1, 0), ('endernet:medium_casing', 1, 0), ('endernet:engineering_chip', 1, 0)), 'crafting'),
    CraftRecipe('harddisk_4k_craft', 'endernet:harddisk_4k', 1, (('endernet:storage_component_4k', 1, 0), ('endernet:medium_casing', 1, 0), ('endernet:engineering_chip', 1, 0)), 'crafting'),
    CraftRecipe('harddisk_16_craft', 'endernet:harddisk_16', 1, (('endernet:storage_component_16k', 1, 0), ('endernet:medium_casing', 1, 0), ('endernet:engineering_chip', 1, 0)), 'crafting'),
    CraftRecipe('memory_128b_craft', 'endernet:memory_128b', 1, (('endernet:calculation_chip', 1, 0), ('minecraft:redstone', 2, 0), ('minecraft:iron_ingot', 1, 0)), 'crafting'),
    CraftRecipe('memory_256b_craft', 'endernet:memory_256b', 1, (('endernet:memory_128b', 2, 0), ('minecraft:redstone', 1, 0)), 'crafting'),
    CraftRecipe('memory_512b_craft', 'endernet:memory_512b', 1, (('endernet:memory_256b', 2, 0), ('minecraft:redstone', 1, 0)), 'crafting'),
    CraftRecipe('memory_1k_craft', 'endernet:memory_1k', 1, (('endernet:memory_512b', 2, 0), ('minecraft:gold_ingot', 1, 0), ('endernet:calculation_chip', 1, 0)), 'crafting'),
    CraftRecipe('memory_4k_craft', 'endernet:memory_4k', 1, (('endernet:memory_1k', 3, 0), ('endernet:calculation_chip', 1, 0), ('endernet:charged_resonance_crystal', 2, 0), ('minecraft:gold_ingot', 1, 0)), 'crafting'),
    CraftRecipe('memory_16k_craft', 'endernet:memory_16k', 1, (('endernet:memory_4k', 3, 0), ('endernet:calculation_chip', 1, 0), ('endernet:charged_resonance_crystal', 2, 0), ('minecraft:gold_ingot', 1, 0)), 'crafting'),
    CraftRecipe('memory_64k_craft', 'endernet:memory_64k', 1, (('endernet:memory_16k', 3, 0), ('endernet:calculation_chip', 2, 0), ('endernet:charged_resonance_crystal', 2, 0), ('minecraft:gold_ingot', 1, 0)), 'crafting'),
    CraftRecipe('memory_256k_craft', 'endernet:memory_256k', 1, (('endernet:memory_64k', 3, 0), ('endernet:engineering_chip', 1, 0), ('endernet:charged_resonance_crystal', 2, 0), ('minecraft:gold_ingot', 2, 0)), 'crafting'),
    CraftRecipe('smelt_resonance_ore', 'endernet:resonance_crystal', 1, (('endernet:end_resonance_ore', 1, 0),), 'furnace'),
)
