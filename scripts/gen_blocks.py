# coding=utf-8
"""由设备定义表生成方块 JSON 与资源包映射。

    python scripts/gen_blocks.py          # 生成
    python scripts/gen_blocks.py --check  # 只检查是否与当前文件一致（CI 用）

单一事实来源是 `endernet_bp/endernet_scripts/endernet/common/definitions.py`：
方块标识、分类、是否逐 tick、是否有朝向，都从那里读，避免两处各写一份。

**外观元数据（破坏耗时、挖掘等级、贴图名）放在本文件**，因为它们决定的是
资源产出，不是运行时玩法数值——把两者混在一起会让 `definitions.py`
背上大量与本模组规则无关的美术参数。
"""

import argparse
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_ROOT = os.path.join(REPO, "endernet_bp", "endernet_scripts")
BP_BLOCKS = os.path.join(REPO, "endernet_bp", "blocks")
RP_ROOT = os.path.join(REPO, "endernet_rp")
RP_BLOCKS = os.path.join(RP_ROOT, "blocks.json")
RP_TERRAIN = os.path.join(RP_ROOT, "textures", "terrain_texture.json")

if SCRIPTS_ROOT not in sys.path:
    sys.path.insert(0, SCRIPTS_ROOT)
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _console import use_utf8_stdout  # noqa: E402

from endernet.common.definitions import (  # noqa: E402
    ASSEMBLE_CHAMBER_SLOT_COUNT,
    CATEGORY_ASSEMBLY,
    CATEGORY_BASEBAND,
    CATEGORY_BUS,
    CATEGORY_CONTROLLER,
    CATEGORY_ENERGY_INPUT,
    CATEGORY_MEMORY,
    CATEGORY_PROCESSOR,
    CATEGORY_PROCESSOR_CONNECTOR,
    CATEGORY_STORAGE,
    CATEGORY_TERMINAL,
    FLUID_PORT_SLOT_COUNT,
    ITEM_INTAKE_PORT_SLOT_COUNT,
    ITEM_OUTPUT_PORT_SLOT_COUNT,
    MEMORY_SLOT_COUNT,
    PASSIVE_PORT_SLOT_COUNT,
    STORAGE_DRIVER_SLOT_COUNT,
    TEMPLATE_CONTAINER_SLOT_COUNT,
    DefinitionSet,
)

#: 方块 JSON 的 format_version。与同环境可正常运行的 SkyblueTech 保持一致：
#: `netease:tier` 与 traits 需要较新的格式。
FORMAT_VERSION = "1.21.70"

#: 创造栏分类，需与 endernet_bp/netease_tab/endernet_category_config.json 的 name 一致
CREATIVE_CATEGORY = "endernet"

#: 方块的外观分三种，写法全部照同环境可运行的 SkyblueTech：
#:
#: - `uniform`：六面同一张贴图 `<短名>_all.png`，`material_instances` 只写 `*`
#:   （SkyblueTech 有 47 个方块是这个形状）。
#: - `front`：正面 `<短名>_front.png` 画在 **north** 面，其余各面用通用机壳
#:   `common_device_{top,side,bottom}.png`；几何体为 `minecraft:geometry.full_block`，
#:   按 `cardinal_direction` 用 `minecraft:transformation` 旋转，正面朝向放置者
#:   （照 `skybluetech_alloy_furnace.json`）。
#: - `model`：自定义几何体（`endernet_rp/models/blocks/*.geo.json`）+ 一张模型贴图，
#:   同样按朝向旋转（照 `skybluetech_charger.json`）。物品栏里直接渲染同一个几何体，
#:   不需要 `minecraft:item_visual`——那只在方块本体几何为空时才用（`skybluetech_digger`）。
LAYOUT_UNIFORM = "uniform"
LAYOUT_FRONT = "front"
LAYOUT_MODEL = "model"

BLOCK_LAYOUTS = {
    # 六面同图：传输介质、无朝向的设备、以及 User 2026-11-05 指定改回单图的设备
    "endernet:network_baseband_8": LAYOUT_UNIFORM,
    "endernet:network_baseband_16": LAYOUT_UNIFORM,
    "endernet:network_baseband_32": LAYOUT_UNIFORM,
    "endernet:parallel_processor": LAYOUT_UNIFORM,
    "endernet:creative_energy_source": LAYOUT_UNIFORM,
    "endernet:dos_network_endpoint": LAYOUT_UNIFORM,
    "endernet:tiny_network_endpoint": LAYOUT_UNIFORM,
    "endernet:bus_item_io": LAYOUT_UNIFORM,
    "endernet:bus_fluid_io": LAYOUT_UNIFORM,
    "endernet:bus_item_input_io": LAYOUT_UNIFORM,
    "endernet:bus_item_output_io": LAYOUT_UNIFORM,
    "endernet:bus_fluid_input_io": LAYOUT_UNIFORM,
    "endernet:bus_fluid_output_io": LAYOUT_UNIFORM,
    "endernet:bus_template_io": LAYOUT_UNIFORM,
    "endernet:parallel_processor_connector": LAYOUT_UNIFORM,
    "endernet:energy_input_interface": LAYOUT_UNIFORM,
    # 正面 + 通用机壳
    "endernet:assemble_chamber": LAYOUT_FRONT,
    "endernet:common_terminal_panel": LAYOUT_FRONT,
    "endernet:crafting_terminal_panel": LAYOUT_FRONT,
    "endernet:template_terminal_panel": LAYOUT_FRONT,
    "endernet:factoric_terminal_panel": LAYOUT_FRONT,
    "endernet:memory_slot": LAYOUT_FRONT,
    # 自定义模型
    "endernet:storage_driver": LAYOUT_MODEL,
}

#: 自定义模型：设备 -> (几何体标识, 模型文件与贴图的基名)。
#: 几何体在 `endernet_rp/models/blocks/<基名>.geo.json`，
#: 贴图在 `textures/endernet/models/<基名>.png`，图集键 `endernet:<基名>_model`。
MODELS = {
    "endernet:storage_driver": ("geometry.endernet.storage_driver", "storage_driver"),
}

#: 通用机壳贴图：`front` 布局的方块共用。面 -> 贴图基名。
COMMON_DEVICE_TEXTURES = (
    ("top", "common_device_top"),
    ("side", "common_device_side"),
    ("bottom", "common_device_bottom"),
)

#: 设备与模型贴图目录（资源包内的相对路径）
DEVICE_TEXTURE_DIR = "textures/endernet/devices"
MODEL_TEXTURE_DIR = "textures/endernet/models"

#: 朝向 -> 绕 Y 轴旋转角。正面画在 north 面；`placement_direction` 的
#: `y_rotation_offset = 180` 让方块朝向放置者，数值与 SkyblueTech 逐字一致。
FACING_ROTATIONS = (
    ("south", 180),
    ("north", 0),
    ("east", -90),
    ("west", 90),
)

#: 需要逐 tick 的设备（`netease:block_entity.tick = true`）。
#: 依据是该设备是否要被动推进：控制器排程、总线搬运、装配仓加工、处理器工作态、
#: 能源接入口持续取电。基带与驱动器靠事件驱动，不需要逐 tick。
#:
#: **创造能量源不在表里**：它是静态电源，供能速率来自定义表、由能源服务在成员
#: 变化时读取，没有任何需要每帧推进的状态。给它开 tick 只会让引擎每帧白派发一次
#: 方块实体 tick（并吃掉唤醒预算）。
TICKING = {
    "endernet:dos_network_endpoint": True,
    "endernet:tiny_network_endpoint": True,
    "endernet:bus_item_io": True,
    "endernet:bus_fluid_io": True,
    "endernet:bus_item_output_io": True,
    "endernet:bus_fluid_output_io": True,
    "endernet:bus_item_input_io": True,
    "endernet:bus_fluid_input_io": True,
    "endernet:bus_template_io": True,
    "endernet:assemble_chamber": True,
    "endernet:parallel_processor": True,
    "endernet:energy_input_interface": True,
}

#: 声明 `netease:block_container` 的方块：`device_id -> (screen_name, 槽数来源)`。
#:
#: 声明了容器组件就必须同时把 `description.base_block` 设为 `netease_container`，
#: 否则引擎不会按自定义容器处理它（见 build_block_json）。
#:
#: **槽数不在这里写**，取自 `endernet.common.definitions` 里那条 Value。
#: 写在一处是为了防「界面上 10 格、领域层只认 8 格」那种分叉——
#: 但**别把这句话读成「领域层一定在读同一份」**：今天真正两侧都读的只有
#: 驱动器与三条接口；2026-09-29 新增的四条（存货口／取货口／内存槽／流体口）
#: **只有方块容器在读**，领域层的缓冲尚未实现（11-07 的 C1）。
#: 其中驱动器槽数仍是 R10 的测试替身，两个流体口是 R07 的占位。
#:
#: 屏幕名形如 `namespace.screenName`，指向资源包里已存在的 `main` 屏幕。
#:
#: **为什么是这几台。**
#: - `storage_driver`：介质要能放进去（05）。
#: - `bus_item_io`：它的 9 格是**存取共用的真实库存**，外部物流要能直接
#:   存取（06-04）。没有容器组件，方块实体数据里就没有 `Items`，
#:   别的模组/漏斗没有地方放东西，那条通路只能停留在契约上。
#: - `bus_template_io`：它的 9 格**专用于工厂样板**（06-07 之后的规格），
#:   容器由任务 07 使用；**任务 06 的原料缓冲不再挂在这个容器上**。
#: - `bus_item_input_io` / `bus_item_output_io` / `memory_slot`：界面**本来就
#:   画了**引擎集合（继承 `container_grid_items`）而方块没有容器，那些格子
#:   因而没有数据源。2026-09-29 User 定案：「不是终端类方块且能存储物品且有
#:   UI 有物品槽的都要有网易容器」。终端那四台仍然不在此列。
#: - `bus_fluid_input_io` / `bus_fluid_output_io`：同上，但格数是 **1 格占位**、
#:   且界面上**刻意不画**那个集合（免得玩家看见一个与流体无关的物品格）。
#:   这是 User 明说的「**先设置**为」，值本身带 `unresolved="R07"`，
#:   真实流体适配属 13-03。
#:
#: **仍然不在内的两台，理由不同，别合并读：**
#: - `bus_fluid_io`（合并式流体总线）：缓冲是 mB 流体，真实流体适配属 13-03、
#:   尚未实现；给它挂一个**物品**容器会得到一个「看起来能用、实际存错东西」
#:   的方块，比没有更糟。它的物品侧对照 `bus_item_io` 也**没做完**，
#:   User 2026-09-29 明确把它与 `bus_item_io` 一并搁置。
#:   **这与上面两个流体口不矛盾**：那两台是 User 单独点名的占位，这台不是。
#:   不要因为「都是流体」就顺手补齐。
CONTAINERS = {
    "endernet:storage_driver": ("storage_driver_ui.main", STORAGE_DRIVER_SLOT_COUNT),
    "endernet:bus_item_io": ("bus_item_io_ui.main", PASSIVE_PORT_SLOT_COUNT),
    "endernet:bus_template_io": ("bus_template_io_ui.main", TEMPLATE_CONTAINER_SLOT_COUNT),
    "endernet:assemble_chamber": (
        "assemble_chamber_ui.main",
        ASSEMBLE_CHAMBER_SLOT_COUNT,
    ),
    "endernet:bus_item_input_io": (
        "bus_item_input_io_ui.main",
        ITEM_INTAKE_PORT_SLOT_COUNT,
    ),
    "endernet:bus_item_output_io": (
        "bus_item_output_io_ui.main",
        ITEM_OUTPUT_PORT_SLOT_COUNT,
    ),
    "endernet:memory_slot": ("memory_slot_ui.main", MEMORY_SLOT_COUNT),
    "endernet:bus_fluid_input_io": (
        "bus_fluid_input_io_ui.main",
        FLUID_PORT_SLOT_COUNT,
    ),
    "endernet:bus_fluid_output_io": (
        "bus_fluid_output_io_ui.main",
        FLUID_PORT_SLOT_COUNT,
    ),
}

#: 需要朝向状态的设备（放置方向决定端口朝向）。
DIRECTIONAL = {
    "endernet:dos_network_endpoint": True,
    "endernet:tiny_network_endpoint": True,
    "endernet:bus_item_io": True,
    "endernet:bus_fluid_io": True,
    "endernet:bus_item_output_io": True,
    "endernet:bus_fluid_output_io": True,
    "endernet:bus_item_input_io": True,
    "endernet:bus_fluid_input_io": True,
    "endernet:bus_template_io": True,
    "endernet:storage_driver": True,
    "endernet:common_terminal_panel": True,
    "endernet:crafting_terminal_panel": True,
    "endernet:template_terminal_panel": True,
    "endernet:factoric_terminal_panel": True,
    "endernet:parallel_processor_connector": True,
    "endernet:memory_slot": True,
    "endernet:assemble_chamber": True,
    "endernet:energy_input_interface": True,
}

#: 破坏耗时（秒）。基带更脆，其余设备按机器处理。
DESTROY_SECONDS = {
    CATEGORY_BASEBAND: 2.0,
}
DEFAULT_DESTROY_SECONDS = 3.0

#: 挖掘等级：需要镐子。
DIGGER = "pickaxe"
TIER_LEVEL = {
    CATEGORY_BASEBAND: 1,
    CATEGORY_CONTROLLER: 2,
    CATEGORY_PROCESSOR: 2,
}
DEFAULT_TIER_LEVEL = 2

#: 方块的简短说明，写进游戏内提示
TIPS = {
    "endernet:dos_network_endpoint": "最简单的仓储网络核心。不支持自动存货/取货与订单处理。",
    "endernet:tiny_network_endpoint": "允许接入总线类设备；不支持订单处理。",
    "endernet:network_baseband_8": "网络传输介质，可接受最多 8u 网络负载。",
    "endernet:network_baseband_16": "网络传输介质，可接受最多 16u 网络负载。",
    "endernet:network_baseband_32": "网络传输介质，可接受最多 32u 网络负载。",
    "endernet:bus_item_io": "与外部物流系统交换特定物品。",
    "endernet:bus_fluid_io": "与外部物流系统交换特定流体。",
    "endernet:bus_item_output_io": "持续从网络中取出指定物品，自动输入朝向的容器。",
    "endernet:bus_fluid_output_io": "持续从网络中取出指定流体，自动输入朝向的容器。",
    "endernet:bus_item_input_io": "持续从朝向的容器取出物品并存入网络。",
    "endernet:bus_fluid_input_io": "持续从朝向的容器取出流体并存入网络。",
    "endernet:bus_template_io": "按工厂样板，向邻近容器投入原料。",
    "endernet:storage_driver": "放入存储介质，为网络提供仓储容量。",
    "endernet:common_terminal_panel": "管理网络的仓储信息。",
    "endernet:crafting_terminal_panel": "在通用终端的基础上增加工作台功能。",
    "endernet:template_terminal_panel": "在通用终端的基础上增加样板配置功能。",
    "endernet:factoric_terminal_panel": "在通用终端的基础上增加订单发配与状态查看。",
    "endernet:parallel_processor_connector": "将并行处理器接入网络基带。",
    "endernet:parallel_processor": "为网络提供 1 个配方处理槽位，须经连接器接入。",
    "endernet:memory_slot": "插入内存条，为网络提供配方运行内存。",
    "endernet:assemble_chamber": "为网络提供自动合成能力。",
    "endernet:energy_input_interface": "将蔚蓝科技的 RF 转换为本模组的 EN。",
    "endernet:creative_energy_source": "为网络提供 2147483647 EN/t 的能源功率。不占带宽。",
}


def short_name(device_id):
    return device_id.split(":", 1)[1]


def container_size_of(device_id):
    # type: (str) -> int
    """该方块的容器格数。

    与领域层读**同一份**定义表取值。三条接口槽数都是**已定案**的
    （`blocks.md` 逐条写明），驱动器槽数则仍是 R10 的测试替身。
    方块的 `container_size` 必须等于它，否则界面上能放 10 格、
    领域层只认 8 格——而那种分叉在离线测试里看不出来。
    """
    return int(CONTAINERS[device_id][1].value)


def block_file_name(device_id):
    # type: (str) -> str
    """方块 JSON 的文件名：`endernet:foo_bar` -> `endernet_foo_bar.json`。

    与同环境可运行的 SkyblueTech（`skybluetech:air_compressor` ->
    `skybluetech_air_compressor.json`）以及本仓库已有文件保持一致：
    命名空间作为前缀、冒号换成下划线，不用裸的短名——否则不同命名空间的
    同名方块会在同一个目录里撞车。
    """
    return device_id.replace(":", "_") + ".json"


def layout_of(device_id):
    # type: (str) -> str
    """该方块的外观布局（`uniform` / `front` / `model`）。"""
    if device_id not in BLOCK_LAYOUTS:
        raise KeyError("%s 没有在 BLOCK_LAYOUTS 里声明外观布局" % device_id)
    return BLOCK_LAYOUTS[device_id]


def _device_texture(name):
    # type: (str) -> tuple
    return ("endernet:" + name, DEVICE_TEXTURE_DIR + "/" + name)


def uniform_texture(device_id):
    # type: (str) -> tuple
    """六面同图：`(图集键, 路径)`，文件名 `<短名>_all`。"""
    return _device_texture(short_name(device_id) + "_all")


def front_texture(device_id):
    # type: (str) -> tuple
    """正面：`(图集键, 路径)`，文件名 `<短名>_front`。"""
    return _device_texture(short_name(device_id) + "_front")


def common_device_texture(face):
    # type: (str) -> tuple
    """通用机壳的某一面：`(图集键, 路径)`。"""
    return _device_texture(dict(COMMON_DEVICE_TEXTURES)[face])


def model_texture(device_id):
    # type: (str) -> tuple
    """模型贴图：`(图集键, 路径)`，图集键带 `_model` 后缀以免与设备贴图同名。"""
    base = MODELS[device_id][1]
    return ("endernet:" + base + "_model", MODEL_TEXTURE_DIR + "/" + base)


def model_geometry_file(device_id):
    # type: (str) -> str
    return os.path.join(RP_ROOT, "models", "blocks", MODELS[device_id][1] + ".geo.json")


def face_textures(device_id):
    # type: (str) -> dict
    """`material_instances` 的面名 -> 图集键。"""
    layout = layout_of(device_id)
    if layout == LAYOUT_UNIFORM:
        return {"*": uniform_texture(device_id)[0]}
    if layout == LAYOUT_MODEL:
        return {"*": model_texture(device_id)[0]}
    side = common_device_texture("side")[0]
    return {
        "*": side,
        "up": common_device_texture("top")[0],
        "down": common_device_texture("bottom")[0],
        "north": front_texture(device_id)[0],
        "south": side,
        "east": side,
        "west": side,
    }


def textures_of(device_id):
    # type: (str) -> list
    """该方块用到的全部 `(图集键, 路径)`。"""
    layout = layout_of(device_id)
    if layout == LAYOUT_UNIFORM:
        return [uniform_texture(device_id)]
    if layout == LAYOUT_MODEL:
        return [model_texture(device_id)]
    return [front_texture(device_id)] + [
        common_device_texture(face) for face, _ in COMMON_DEVICE_TEXTURES
    ]


def plural_dumps(payload):
    """稳定的输出格式：UTF-8、保留非 ASCII、缩进 4、行尾 LF。"""
    return json.dumps(payload, indent=4, ensure_ascii=False) + "\n"


def build_block_json(device_id, definition):
    """生成单个方块的行为包定义。"""
    category = definition.category
    components = {}

    components["minecraft:destructible_by_mining"] = {
        "seconds_to_destroy": DESTROY_SECONDS.get(category, DEFAULT_DESTROY_SECONDS)
    }
    components["netease:tier"] = {
        "destroy_special": True,
        "digger": DIGGER,
        "level": TIER_LEVEL.get(category, DEFAULT_TIER_LEVEL),
    }
    layout = layout_of(device_id)
    if layout != LAYOUT_UNIFORM and not DIRECTIONAL.get(device_id):
        raise ValueError(
            "%s 的外观布局是 %s，需要随朝向旋转，但它不在 DIRECTIONAL 里" % (device_id, layout)
        )
    if layout == LAYOUT_FRONT:
        components["minecraft:geometry"] = {"identifier": "minecraft:geometry.full_block"}
    elif layout == LAYOUT_MODEL:
        components["minecraft:geometry"] = {"identifier": MODELS[device_id][0]}
        # 非整块模型：不挡光，否则凹进去的部分落在自己的阴影里发黑
        # （照 skybluetech_charger.json）。
        components["minecraft:light_dampening"] = 0
    # 模型贴图里有透明像素（从正面开口能看到的内部底板就有），`opaque` 会把它们
    # 画成黑点；`alpha_test` 让它们镂空，与 Blockbench 里看到的一致。整块方块不透明。
    render_method = "alpha_test" if layout == LAYOUT_MODEL else "opaque"
    components["minecraft:material_instances"] = dict(
        (face, {"render_method": render_method, "texture": key})
        for face, key in sorted(face_textures(device_id).items())
    )

    # 自定义方块实体：tick 决定是否派发 ServerBlockEntityTickEvent（每秒 20 次）。
    # 即使不需要 tick 也要声明，否则方块实体数据无处存放。
    components["netease:block_entity"] = {"tick": bool(TICKING.get(device_id, False))}

    # 移除事件白名单。没有它引擎不会派发 BlockRemoveServerEvent，
    # 设备拆除将完全无感。（运行时另有 AddBlocksToBlockRemoveListener 兜底。）
    components["netease:listen_block_remove"] = {"value": True}

    # 自定义容器。引擎原生驱动背包界面与槽位物品，因此方块自身的
    # 方块实体数据里会出现 `Items` —— 那才是驱动器 10 个槽位的真实存放处。
    if device_id in CONTAINERS:
        components["netease:block_container"] = {
            "custom_description": definition.label,
            "screen_name": CONTAINERS[device_id][0],
            "container_size": container_size_of(device_id),
        }

    if device_id in TIPS:
        components["netease:custom_tips"] = {"value": "\n§7" + TIPS[device_id]}

    description = {
        "category": CREATIVE_CATEGORY,
        "identifier": device_id,
    }
    if device_id in CONTAINERS:
        # 写在 description 而不是 components 里——这是引擎认的字段位置。
        description["base_block"] = "netease_container"
    if DIRECTIONAL.get(device_id):
        description["traits"] = {
            "minecraft:placement_direction": {
                "enabled_states": ["minecraft:cardinal_direction"],
                "y_rotation_offset": 180.0,
            }
        }

    block = {
        "components": components,
        "description": description,
    }
    if layout != LAYOUT_UNIFORM:
        # 正面画在 north 面，按朝向整体旋转（照 skybluetech_alloy_furnace.json）。
        block["permutations"] = [
            {
                "condition": "q.block_state('minecraft:cardinal_direction') == '%s'" % facing,
                "components": {"minecraft:transformation": {"rotation": [0, angle, 0]}},
            }
            for facing, angle in FACING_ROTATIONS
        ]
    return {
        "format_version": FORMAT_VERSION,
        "minecraft:block": block,
    }


def build_blocks_json(definitions):
    """资源包的 blocks.json：方块标识 -> 贴图。

    六面同图与模型方块写一个图集键（破坏粒子取它）；正面布局逐面写明，
    与行为包的 `material_instances` 一致（照 SkyblueTech 的六面写法）。
    """
    payload = {}
    for device_id in sorted(definitions.devices):
        layout = layout_of(device_id)
        entry = {"sound": "stone"}
        if layout == LAYOUT_MODEL:
            # 模型方块不写 textures（照 SkyblueTech 充电器）：外观与物品栏图标都由
            # 行为包的几何体 + material_instances 决定；写了反而可能让引擎退回
            # 「把 32x32 的 UV 图贴满一整块」的平面方块。
            payload[device_id] = entry
            continue
        faces = face_textures(device_id)
        if layout == LAYOUT_FRONT:
            entry["textures"] = dict(
                (face, faces[face]) for face in ("up", "down", "north", "south", "east", "west")
            )
        else:
            entry["textures"] = faces["*"]
        payload[device_id] = dict(sorted(entry.items(), key=lambda kv: kv[0] != "textures"))
    payload["format_version"] = [1, 1, 0]
    return payload


def build_terrain_texture(existing, definitions):
    """地形图集：设备与模型贴图条目按布局整体重建，其它条目原样保留。

    设备 / 模型目录下的条目**不沿用旧值**：换布局后旧键留在图集里会指向
    已删除的文件，游戏里不报错、`test_every_referenced_texture_file_exists` 却会红。
    """
    prefixes = (DEVICE_TEXTURE_DIR + "/", MODEL_TEXTURE_DIR + "/")
    texture_data = dict(
        (key, entry)
        for key, entry in existing.get("texture_data", {}).items()
        if not str(entry.get("textures", "")).startswith(prefixes)
    )
    for device_id in sorted(definitions.devices):
        for key, path in textures_of(device_id):
            texture_data[key] = {"textures": path}
    payload = dict(existing)
    payload["resource_pack_name"] = existing.get("resource_pack_name", "vanilla")
    payload["texture_name"] = existing.get("texture_name", "atlas.terrain")
    payload["texture_data"] = dict(sorted(texture_data.items()))
    return payload


def read_json(path, default=None):
    if not os.path.isfile(path):
        return {} if default is None else default
    with io.open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, payload, check_only):
    text = plural_dumps(payload)
    if os.path.isfile(path):
        with io.open(path, "r", encoding="utf-8") as handle:
            if handle.read() == text:
                return False
    if not check_only:
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    return True


def missing_textures(definitions):
    """列出还缺文件的 `(方块, 路径, 说明)`：贴图与模型几何体。"""
    out = []
    seen = set()
    for device_id in sorted(definitions.devices):
        for key, path in textures_of(device_id):
            full = os.path.join(RP_ROOT, *(path + ".png").split("/"))
            if full not in seen and not os.path.isfile(full):
                seen.add(full)
                out.append((device_id, full, key))
        if layout_of(device_id) == LAYOUT_MODEL:
            geo = model_geometry_file(device_id)
            if not os.path.isfile(geo):
                out.append((device_id, geo, MODELS[device_id][0]))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成 EnderNet 方块 JSON 与资源映射")
    parser.add_argument("--check", action="store_true", help="只检查是否需要重新生成")
    use_utf8_stdout()
    args = parser.parse_args(argv)

    definitions = DefinitionSet()
    changed = []

    for device_id in sorted(definitions.devices):
        definition = definitions.get(device_id)
        path = os.path.join(BP_BLOCKS, block_file_name(device_id))
        if write_json(path, build_block_json(device_id, definition), args.check):
            changed.append(os.path.relpath(path, REPO))

    if write_json(RP_BLOCKS, build_blocks_json(definitions), args.check):
        changed.append(os.path.relpath(RP_BLOCKS, REPO))

    terrain = read_json(RP_TERRAIN)
    if write_json(RP_TERRAIN, build_terrain_texture(terrain, definitions), args.check):
        changed.append(os.path.relpath(RP_TERRAIN, REPO))

    missing = missing_textures(definitions)

    if args.check:
        if changed:
            print("以下文件需要重新生成（共 %d 个）：" % len(changed))
            for path in changed:
                print("  " + path)
            return 1
        print("方块 JSON 与资源映射已是最新（%d 个方块）" % len(definitions.devices))
        return 0

    print("已生成 %d 个方块定义，更新 %d 个文件" % (len(definitions.devices), len(changed)))
    if missing:
        print("仍缺文件（%d 个）；贴图可先用 scripts/gen_device_textures.py 补占位：" % len(missing))
        for device_id, path, key in missing:
            print("  %-40s -> %s" % (device_id, os.path.relpath(path, REPO)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
