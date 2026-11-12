# coding=utf-8
"""由设备定义表生成方块 JSON 与资源包映射。

    python scripts/gen_blocks.py          # 生成
    python scripts/gen_blocks.py --check  # 只检查是否与当前文件一致（CI 用）

单一事实来源是 `endernet_bp/endernet_scripts/endernet/common/definitions.py`：
方块标识、分类、是否逐 tick、是否有朝向，都从那里读，避免两处各写一份。

**外观元数据（破坏耗时、挖掘等级、贴图名）放在本文件**，因为它们决定的是
资源产出，不是运行时玩法数值——把两者混在一起会让 `definitions.py`
背上大量与本模组规则无关的美术参数。

**三种末影光缆**（`definitions.cables`，2026-11-09 网络改版阶段 C）另走一条路
（`build_cable_json`）：不进 `BLOCK_LAYOUTS`，没有朝向；每面一个整数状态
`endernet:side_<面>`（0 无、1 连接臂、2 面板，致密只取 0 / 1），骨骼按状态显示，
每个排列一个 `netease:aabb` 外包盒。状态名、骨骼清单与显示条件（`cable_shapes.bone_visibility`）
与部件尺寸都取自 `common/cable_shapes.py`——几何体（`gen_cable_models.py`）与运行时的
精确交互用的是同一份。玻璃光缆整根 `blend`（填充面、芯另有材质实例，2026-11-10 美术重做），
其余 `opaque`。

**在线 / 离线外观**（2026-11-13，作者画的离线材质）：有离线外观的整方块设备
（`DefinitionSet.status_values_of`）多一个方块状态 `endernet:status`（0 离线、1 在线，控制器另有
2 冲突），贴图是 `<贴图>_offline` / `<贴图>` / `<贴图>_conflict`（正面布局连通用机壳三面一起换，
模型布局换模型贴图）。默认值 0 离线，为的是放下的那一刻不先亮一下：方块先按离线画，服务端确认在线
才写成 1。物品栏图标画的也是物品带的默认状态，要它是在线外观，另加放置痕迹（原版特征
`minecraft:placement_position`，见 `definitions.PLACED_FACE_STATE`）：`components` 里是在线那一套，
离线排列的条件是「状态 0 且不是没放过的默认组合 (down, bottom)」，冲突一条排列。
**排列里换贴图只在声明了几何体的方块上生效**：不声明 `minecraft:geometry` 时引擎按资源包
`blocks.json` 的贴图画整块，排列里的 `material_instances` 不起作用（作者真机反馈：只有正面布局的
终端换了；蔚蓝科技按状态换贴图的 26 个方块都声明了 `minecraft:geometry.full_block`），所以有离线
外观的六面同图方块也声明 `full_block`。

**面板底座**（`definitions.part_holders`，2026-11-10 作者定案）也走这条路
（`build_part_holder_json`）：每面只取 0 / 2（64 种），没有线芯与连接臂，方块网格
借普通光缆的几何体、骨骼全部隐藏，外观全靠面板的方块实体外观；不进创造栏，
自身不掉落（原版的空战利品表），面板由脚本掉。
"""

import argparse
import io
import json
import os
import re
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
    CATEGORY_CABLE,
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
    PLACED_FACE_STATE,
    PLACED_HALF_STATE,
    SIX_WAY_ITEM_PORTS,
    STATUS_CONFLICT,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    STATUS_STATE,
    STORAGE_DRIVER_SLOT_COUNT,
    QUARTERS_PER_UNIT,
    TEMPLATE_CONTAINER_SLOT_COUNT,
    UNPLACED_FACE,
    UNPLACED_HALF,
    DefinitionSet,
)
from endernet.common.channels.slot_display import DISPLAY_DEVICES  # noqa: E402
from endernet.common.channels.cable_intent import WRENCH_ID  # noqa: E402
from endernet.common.cable_shapes import (  # noqa: E402
    HOLDER_SIDE_VALUES,
    MATERIAL_FIBER,
    MATERIAL_GLASS,
    PANEL_ENTITY_ID,
    bone_visibility,
    cable_bones,
    core_px_of,
    geometry_id,
    is_glass,
    side_state,
    side_values,
    unit_hull,
)
from endernet.common.identity import FACING_NAMES  # noqa: E402

#: 方块 JSON 的 format_version。与同环境可正常运行的 SkyblueTech 保持一致：
#: `netease:tier` 与 traits 需要较新的格式。
FORMAT_VERSION = "1.21.70"

#: 创造栏分类，需与 endernet_bp/netease_tab/endernet_category_config.json 的 name 一致
CREATIVE_CATEGORY = "endernet"

#: 不进创造栏的方块的分类（面板底座：没有对应物品，只由脚本放置）。写法照同环境可运行的
#: SkyblueTech（`netease_blocks/liquid/skybluetech_flowing_brine.json` 的 `"category": "none"`）。
HIDDEN_CATEGORY = "none"

#: 方块的外观分三种，写法全部照同环境可运行的 SkyblueTech：
#:
#: - `uniform`：六面同一张贴图 `<短名>_all.png`，`material_instances` 只写 `*`
#:   （SkyblueTech 有 47 个方块是这个形状）。
#: - `front`：正面 `<短名>_front.png` 画在 **north** 面，其余各面用通用机壳
#:   `common_device_{top,side,bottom}.png`；几何体为 `minecraft:geometry.full_block`，
#:   按 `cardinal_direction` 用 `minecraft:transformation` 旋转，正面朝向放置者
#:   （照 `skybluetech_alloy_furnace.json`）。
#: - `model`：自定义几何体（`endernet_rp/models/blocks/*.geo.json`）+ 一张模型贴图，
#:   同样按朝向旋转（照 `skybluetech_charger.json`）。物品栏里直接渲染同一个几何体。
#:   **不写 `minecraft:item_visual`**，挂了方块实体外观（`client_entity`）的也不写：
#:   它不吃 `permutations` 里的朝向旋转，2026-11-05 加过一版，物品栏图标背面朝外；
#:   改成转 180° 的物品几何体后正常，但 User 2026-11-06 判定那份是多余的模型，删掉。
#:   （若哪天图标又不对：先看 `client_entity.hand_model_use_client_entity`。）
#:   光缆不同：它没有朝向旋转，物品形态要显示一段直通而不是方块的默认状态（只有线芯），
#:   所以光缆写 `item_visual`，指向 `gen_cable_models.py` 出的专用物品几何（2026-11-10）。
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
    "endernet:wireless_connector": LAYOUT_UNIFORM,
    # 正面 + 通用机壳
    "endernet:assemble_chamber": LAYOUT_FRONT,
    "endernet:common_terminal_panel": LAYOUT_FRONT,
    "endernet:crafting_terminal_panel": LAYOUT_FRONT,
    "endernet:template_terminal_panel": LAYOUT_FRONT,
    "endernet:factoric_terminal_panel": LAYOUT_FRONT,
    # 自定义模型
    "endernet:storage_driver": LAYOUT_MODEL,
    "endernet:memory_slot": LAYOUT_MODEL,
}

#: 自定义模型：设备 -> (几何体标识, 模型文件与贴图的基名)。
#: 几何体在 `endernet_rp/models/blocks/<基名>.geo.json`，
#: 贴图在 `textures/endernet/models/<基名>.png`，图集键 `endernet:<基名>_model`。
MODELS = {
    "endernet:storage_driver": ("geometry.endernet.storage_driver", "storage_driver"),
    "endernet:memory_slot": ("geometry.endernet.memory_slot", "memory_slot"),
}

#: 模型贴图的渲染方式。贴图里**被取样的地方**有透明像素就必须 `alpha_test`
#: （驱动器从正面开口能看到的内部底板就有），否则 `opaque` 会把它们画成黑点。
#: 内存槽贴图里留空的角落不会被任何面取样，用 `opaque`。
MODEL_RENDER_METHODS = {
    "endernet:storage_driver": "alpha_test",
    "endernet:memory_slot": "opaque",
}

#: 不满一整格的模型：碰撞箱与选择框 `(origin, size)`。不在表里的是整格。
#: 内存槽是半砖高（框体顶面 y=8；插着的内存条再露出 3 格，不算进碰撞）。
MODEL_BOXES = {
    "endernet:memory_slot": ([-8, 0, -8], [16, 8, 16]),
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
    "endernet:storage_driver": ("endernet_storage_driver.main", STORAGE_DRIVER_SLOT_COUNT),
    "endernet:bus_item_io": ("endernet_bus_item_io.main", PASSIVE_PORT_SLOT_COUNT),
    "endernet:bus_template_io": ("endernet_bus_template_io.main", TEMPLATE_CONTAINER_SLOT_COUNT),
    "endernet:assemble_chamber": (
        "endernet_assemble_chamber.main",
        ASSEMBLE_CHAMBER_SLOT_COUNT,
    ),
    "endernet:bus_item_input_io": (
        "endernet_bus_item_input_io.main",
        ITEM_INTAKE_PORT_SLOT_COUNT,
    ),
    "endernet:bus_item_output_io": (
        "endernet_bus_item_output_io.main",
        ITEM_OUTPUT_PORT_SLOT_COUNT,
    ),
    "endernet:memory_slot": ("endernet_memory_slot.main", MEMORY_SLOT_COUNT),
    "endernet:bus_fluid_input_io": (
        "endernet_bus_fluid_input_io.main",
        FLUID_PORT_SLOT_COUNT,
    ),
    "endernet:bus_fluid_output_io": (
        "endernet_bus_fluid_output_io.main",
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
    # 光缆与基带同为传输介质，先照基带（阶段 C 的占位口径，待作者定）
    CATEGORY_CABLE: 2.0,
}
DEFAULT_DESTROY_SECONDS = 3.0

#: 挖掘等级：需要镐子。
DIGGER = "pickaxe"
TIER_LEVEL = {
    CATEGORY_BASEBAND: 1,
    CATEGORY_CABLE: 1,
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
    "endernet:wireless_connector": "用无线连接工具与另一台配对，两端合成一张网（同维度、256 格内）。",
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


#: 外观状态 -> 贴图名后缀（`<短名>_all_offline`、`common_device_side_offline`、`<短名>_all_conflict`）
STATUS_SUFFIXES = {STATUS_ONLINE: "", STATUS_OFFLINE: "_offline", STATUS_CONFLICT: "_conflict"}


def status_texture(texture, status):
    # type: (tuple, int) -> tuple
    """`(图集键, 路径)` 换成某个外观状态的那一张。"""
    suffix = STATUS_SUFFIXES[status]
    return texture[0] + suffix, texture[1] + suffix


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


def face_textures(device_id, status=STATUS_ONLINE):
    # type: (str, int) -> dict
    """`material_instances` 的面名 -> 图集键（`status` 是外观状态，见模块说明）。"""
    layout = layout_of(device_id)
    if layout == LAYOUT_UNIFORM:
        return {"*": status_texture(uniform_texture(device_id), status)[0]}
    if layout == LAYOUT_MODEL:
        return {"*": status_texture(model_texture(device_id), status)[0]}
    side = status_texture(common_device_texture("side"), status)[0]
    return {
        "*": side,
        "up": status_texture(common_device_texture("top"), status)[0],
        "down": status_texture(common_device_texture("bottom"), status)[0],
        "north": status_texture(front_texture(device_id), status)[0],
        "south": side,
        "east": side,
        "west": side,
    }


def textures_of(device_id, statuses=(STATUS_ONLINE,)):
    # type: (str, tuple) -> list
    """该方块用到的全部 `(图集键, 路径)`；`statuses` 是它的外观状态（`status_values_of`）。"""
    layout = layout_of(device_id)
    if layout == LAYOUT_MODEL:
        base = [model_texture(device_id)]
    elif layout == LAYOUT_UNIFORM:
        base = [uniform_texture(device_id)]
    else:
        base = [front_texture(device_id)] + [
            common_device_texture(face) for face, _ in COMMON_DEVICE_TEXTURES
        ]
    return [status_texture(texture, status) for status in statuses for texture in base]


def statuses_of(device_id, definitions):
    # type: (str, object) -> tuple
    """这台方块的外观状态；没有离线外观的只有在线这一种。"""
    return definitions.status_values_of(device_id) or (STATUS_ONLINE,)


def material_instances(device_id, status=STATUS_ONLINE):
    # type: (str, int) -> dict
    """`minecraft:material_instances`：模型的渲染方式逐个声明（`MODEL_RENDER_METHODS`），整块不透明。"""
    render_method = (
        MODEL_RENDER_METHODS[device_id] if layout_of(device_id) == LAYOUT_MODEL else "opaque"
    )
    return dict(
        (face, {"render_method": render_method, "texture": key})
        for face, key in sorted(face_textures(device_id, status).items())
    )


def plural_dumps(payload):
    """稳定的输出格式：UTF-8、保留非 ASCII、缩进 4、行尾 LF。"""
    return json.dumps(payload, indent=4, ensure_ascii=False) + "\n"


#: 缩进写法里一个纯数字数组（`[\n 0.375,\n 1.0\n]`）
_NUMBER_ARRAY = re.compile(r"\[\s+(-?[0-9.]+(?:,\s+-?[0-9.]+)*)\s+\]")


def compact_dumps(payload):
    """同 `plural_dumps`，只把纯数字数组写在一行（`[0.375, 0.375, 0.375]`）。

    给光缆方块用：729 个排列各有两个碰撞盒，逐个数字占一行的话一个文件近 1 MB，
    写成一行小一半，逐条也好看。其余方块仍用 `plural_dumps`，不动已有产物。
    """
    return _NUMBER_ARRAY.sub(
        lambda match: "[" + ", ".join(part.strip() for part in match.group(1).split(",")) + "]",
        plural_dumps(payload),
    )


def mining_components(category):
    # type: (str) -> dict
    """破坏耗时与挖掘等级：设备与光缆共用同一套口径（按分类查表）。"""
    return {
        "minecraft:destructible_by_mining": {
            "seconds_to_destroy": DESTROY_SECONDS.get(category, DEFAULT_DESTROY_SECONDS)
        },
        "netease:tier": {
            "destroy_special": True,
            "digger": DIGGER,
            "level": TIER_LEVEL.get(category, DEFAULT_TIER_LEVEL),
        },
    }


def definition_statuses(device_id):
    # type: (str) -> tuple
    """这台方块的外观状态（`DefinitionSet.status_values_of`，第一个是默认值）；没有离线外观为空。"""
    return _DEFINITIONS.status_values_of(device_id)


def status_condition(status):
    # type: (int) -> str
    """换成某个外观状态那条排列的条件。离线要排除没放过的默认组合：那是物品栏图标，画在线。"""
    condition = "q.block_state('%s') == %d" % (STATUS_STATE, status)
    if status != STATUS_OFFLINE:
        return condition
    return "%s && !(q.block_state('%s') == '%s' && q.block_state('%s') == '%s')" % (
        condition, PLACED_FACE_STATE, UNPLACED_FACE, PLACED_HALF_STATE, UNPLACED_HALF)


_DEFINITIONS = DefinitionSet()


def build_block_json(device_id, definition):
    """生成单个方块的行为包定义。"""
    components = mining_components(definition.category)
    layout = layout_of(device_id)
    if layout != LAYOUT_UNIFORM and not DIRECTIONAL.get(device_id):
        raise ValueError(
            "%s 的外观布局是 %s，需要随朝向旋转，但它不在 DIRECTIONAL 里" % (device_id, layout)
        )
    if layout == LAYOUT_FRONT or (layout == LAYOUT_UNIFORM and definition_statuses(device_id)):
        # 有离线外观的六面同图方块也要声明几何体，排列里换贴图才生效（见模块说明）
        components["minecraft:geometry"] = {"identifier": "minecraft:geometry.full_block"}
    elif layout == LAYOUT_MODEL:
        components["minecraft:geometry"] = {"identifier": MODELS[device_id][0]}
        # 非整块模型：不挡光，否则凹进去的部分落在自己的阴影里发黑
        # （照 skybluetech_charger.json）。
        components["minecraft:light_dampening"] = 0
        if device_id in MODEL_BOXES:
            origin, size = MODEL_BOXES[device_id]
            box = {"origin": list(origin), "size": list(size)}
            components["minecraft:collision_box"] = box
            components["minecraft:selection_box"] = dict(box)
    components["minecraft:material_instances"] = material_instances(device_id)

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
    traits = {}
    if DIRECTIONAL.get(device_id):
        traits["minecraft:placement_direction"] = {
            "enabled_states": [
                "minecraft:facing_direction" if device_id in SIX_WAY_ITEM_PORTS
                else "minecraft:cardinal_direction"
            ],
            "y_rotation_offset": 180.0,
        }
    if definition_statuses(device_id):
        # 放置痕迹：物品栏图标（没放过的默认组合）画在线，放下的方块画离线（见模块说明）
        traits["minecraft:placement_position"] = {
            "enabled_states": [PLACED_FACE_STATE, PLACED_HALF_STATE],
        }
    if traits:
        description["traits"] = traits

    block = {
        "components": components,
        "description": description,
    }
    permutations = []
    if layout != LAYOUT_UNIFORM:
        # 正面画在 north 面，按朝向整体旋转（照 skybluetech_alloy_furnace.json）。
        permutations.extend(
            {
                "condition": "q.block_state('minecraft:cardinal_direction') == '%s'" % facing,
                "components": {"minecraft:transformation": {"rotation": [0, angle, 0]}},
            }
            for facing, angle in FACING_ROTATIONS
        )
    statuses = definition_statuses(device_id)
    if statuses:
        # 外观状态：`components` 是在线那一套，离线、冲突各换一套贴图；与朝向那几条排列各管各的
        # 组件，同时成立时一起生效
        description["states"] = {STATUS_STATE: list(statuses)}
        permutations.extend(
            {
                "condition": status_condition(status),
                "components": {"minecraft:material_instances": material_instances(device_id, status)},
            }
            for status in statuses
            if status != STATUS_ONLINE
        )
    if permutations:
        block["permutations"] = permutations
    return {
        "format_version": FORMAT_VERSION,
        "minecraft:block": block,
    }


# -- 末影光缆（13 §2、§5）--------------------------------------------------


def cable_texture(device_id):
    # type: (str) -> tuple
    """光缆的模型贴图 `(图集键, 路径)`，与模型方块同一约定：`textures/endernet/models/<短名>`、
    图集键 `endernet:<短名>_model`。贴图本身由 `gen_cable_models.py` 生成。"""
    base = short_name(device_id)
    return ("endernet:" + base + "_model", MODEL_TEXTURE_DIR + "/" + base)


def cable_geometry_file(device_id):
    # type: (str) -> str
    return os.path.join(RP_ROOT, "models", "blocks", short_name(device_id) + ".geo.json")


def side_combinations(values):
    # type: (tuple) -> list
    """每面取 `values` 之一的全部排列：每项 `{面名: 状态值}`，按 `FACING_NAMES` 逐位、
    取值从小到大枚举。"""
    combinations = [{}]
    for face in FACING_NAMES:
        combinations = [
            dict(sides, **{face: value})
            for sides in combinations
            for value in values
        ]
    return combinations


def cable_side_combinations(accepts_parts):
    # type: (bool) -> list
    """光缆的全部排列：能挂面板的光缆 3⁶ = 729 种，致密 2⁶ = 64 种（13 §2）。"""
    return side_combinations(side_values(accepts_parts))


def cable_condition(sides):
    # type: (dict) -> str
    """一个排列的条件：六面状态全部写明（`&&` 连接）。"""
    return " && ".join(
        "q.block_state('%s') == %d" % (side_state(face), sides[face]) for face in FACING_NAMES
    )


def cable_aabb(device_id, sides):
    # type: (str, dict) -> dict
    """`netease:aabb`：线芯 + 状态 1 的臂 + 状态 2 的面板的外包盒，碰撞与选取同一个盒（照蔚蓝）。

    面板底座也用它：`core_px_of` 给 None，外包盒只算面板框与背板，一块面板都没有时是
    `cable_shapes.EMPTY_HULL_PX` 的居中小盒。
    """
    low, high = unit_hull(core_px_of(device_id), sides)
    box = {"min": list(low), "max": list(high)}
    return {"collision": box, "clip": dict(box)}


def cable_material_instances(device_id):
    # type: (str) -> dict
    """光缆的 `minecraft:material_instances`，三个实例同一张模型贴图。

    玻璃光缆整根 `blend`：一个方块的所有材质实例必须同一种 `render_method`（BedrockWiki），
    外框与芯靠贴图 alpha = 255 显得不透明。几何体里逐面写的实例（`cable_shapes` 的
    `MATERIAL_GLASS` 填充面、`MATERIAL_FIBER` 芯）在这里另配：填充不做环境光遮蔽，芯另外
    不随朝向变暗（各方向一样亮，像在发光）。其余光缆 `opaque`，只写 `*`。
    """
    texture = cable_texture(device_id)[0]
    if not is_glass(device_id):
        return {"*": {"render_method": "opaque", "texture": texture}}
    return {
        "*": {"render_method": "blend", "texture": texture},
        MATERIAL_GLASS: {"render_method": "blend", "texture": texture, "ambient_occlusion": False},
        MATERIAL_FIBER: {"render_method": "blend", "texture": texture, "face_dimming": False,
                         "ambient_occlusion": False},
    }


def cable_tip(definition, definitions):
    # type: (object, object) -> str
    """光缆的方块说明。带宽、能否挂面板与扳手的名字都取自定义表，不在这里另写一份。"""
    units = definition.bandwidth.capacity_quarters / float(QUARTERS_PER_UNIT)
    parts = "可在各面挂终端面板" if definition.accepts_parts else "不能挂终端面板"
    wrench = definitions.get_tool(WRENCH_ID).label
    return "每格最多传输 %gu 网络带宽，%s。\n§7%s右键可切断或恢复某一面，潜行右键拆下。" % (
        units, parts, wrench)


def build_cable_json(device_id, definition, definitions):
    """一种光缆的行为包定义（照蔚蓝管道：几何体 + 骨骼显示 + 每个排列一个碰撞盒）。

    - 方块实体：拓扑与方块状态要从方块实体数据读切断面与面板（`endernet:cuts` /
      `endernet:parts`），区块加载时也靠方块实体名单发现它；不逐 tick。
    - `netease:neighborchanged_sendto_script`：邻居变化要通知脚本（运行时过滤
      `from == to`：改状态本身也会触发邻居事件）。
    - 骨骼显示：每根骨骼一条 molang（`cable_shapes.bone_visibility`，只用 `side_<面>`）。
    - 渲染：不挡光；玻璃整根 `blend`，其余 `opaque`（`cable_material_instances`）。
    - 掉落是方块本身（不写 loot，与其它方块相同）。
    """
    accepts_parts = definition.accepts_parts
    if core_px_of(device_id) is None:
        # `cable_aabb` 对没有线芯的方块按面板底座算盒；光缆漏登记线芯时在这里拦下
        raise KeyError("%s 没有在 cable_shapes.CORE_PX 里登记线芯边长" % (device_id,))
    components = mining_components(definition.category)
    components["minecraft:geometry"] = {
        "identifier": geometry_id(short_name(device_id)),
        "bone_visibility": bone_visibility(device_id, accepts_parts),
    }
    # 物品形态：一段直通（`gen_cable_models.build_cable_item`），材质与方块同一套
    components["minecraft:item_visual"] = {
        "geometry": geometry_id(short_name(device_id) + "_item"),
        "material_instances": cable_material_instances(device_id),
    }
    components["minecraft:light_dampening"] = 0
    components["minecraft:material_instances"] = cable_material_instances(device_id)
    # 默认（全 0）就是只有线芯；每个排列另写自己的盒
    components["netease:aabb"] = cable_aabb(device_id, {})
    components["netease:block_entity"] = {"tick": False}
    components["netease:listen_block_remove"] = {"value": True}
    components["netease:neighborchanged_sendto_script"] = {"value": True}
    components["netease:custom_tips"] = {"value": "\n§7" + cable_tip(definition, definitions)}
    description = {
        "category": CREATIVE_CATEGORY,
        "identifier": device_id,
        "states": dict(
            (side_state(face), list(side_values(accepts_parts))) for face in FACING_NAMES
        ),
    }
    permutations = [
        {
            "condition": cable_condition(sides),
            "components": {"netease:aabb": cable_aabb(device_id, sides)},
        }
        for sides in cable_side_combinations(accepts_parts)
    ]
    return {
        "format_version": FORMAT_VERSION,
        "minecraft:block": {
            "components": components,
            "description": description,
            "permutations": permutations,
        },
    }


def build_cable_blocks_entry(definition):
    # type: (object) -> dict
    """资源包 blocks.json 里一种光缆（或面板底座）的条目。

    模型方块不写 textures（同 `build_blocks_json`）。能挂面板的两种绑定面板的方块实体外观
    （照驱动器的写法，手持仍用方块几何）；致密不挂面板，不绑。面板底座 `accepts_parts`
    为真，同样绑定——它的外观全靠这个实体。
    """
    entry = {"sound": "stone"}
    if definition.accepts_parts:
        entry["client_entity"] = {
            "hand_model_use_client_entity": False,
            "identifier": PANEL_ENTITY_ID,
        }
    return entry


# -- 面板底座（2026-11-10 作者定案）--------------------------------------------

#: 面板底座借这种光缆的几何体与模型贴图。方块网格总得有一个几何体（不写的话引擎把
#: `material_instances` 的贴图画满一整块），骨骼全部隐藏后它什么也不画；贴图键给破坏粒子
#: 与引擎一个已登记的值，图集不必为底座新增条目。借普通光缆（`opaque`、21 根骨骼）：
#: 玻璃光缆 2026-11-10 起是 `blend` + 逐面材质实例、73 根骨骼，不适合拿来什么都不画。
HOLDER_LOOKS_LIKE = "endernet:ender_cable"

#: 面板底座的战利品表：原版行为包自带的空表 `loot_tables/empty.json`（内容 `{}`，原版的
#: 玩家、NPC、tripod_camera 也指向它）。2026-11-10 用 mcdk 的 `assets empty --bp` 核实原版
#: 有这个文件，因此本模组不另写一份。底座自身不掉落，面板由脚本掉。
EMPTY_LOOT_TABLE = "loot_tables/empty.json"


def holder_bone_visibility(definitions):
    # type: (object) -> dict
    """面板底座的骨骼显示：借来的几何体里**全部**骨骼一律隐藏（直通、连接臂、短接线、空面）。

    骨骼清单取 `cable_shapes.cable_bones`（几何体就是照它出的），不在这里另写骨骼名。
    取值写布尔 `false` 而不是 Molang `"false"`：`bone_visibility` 最早就只收布尔，Molang
    条件是后来加的扩展；这里是与方块状态无关的常量，用不着按排列求值。几何体里的骨骼都是
    顶层骨骼（没有 `parent`），逐个写明，不依赖「父骨骼隐藏时子骨骼跟着隐藏」。
    骨骼清单与几何体文件逐个对得上由 `test_cable_blocks` 检查。
    """
    looks = definitions.get(HOLDER_LOOKS_LIKE)
    bones = cable_bones(HOLDER_LOOKS_LIKE, looks.accepts_parts)
    return dict((bone.name, False) for bone in bones)


def build_part_holder_json(device_id, definition, definitions):
    """面板底座的行为包定义（照光缆：每面一个整数状态、每个排列一个碰撞盒）。

    - 状态 `endernet:side_<面>` 每面只取 0 / 2（`HOLDER_SIDE_VALUES`，2⁶ = 64 种），没有连接臂；
    - 碰撞盒是面板框与背板的外包盒（`unit_hull(None, sides)`），一块面板都没有时是小盒；
    - 方块网格什么也不画（`holder_bone_visibility`），外观全靠资源包 `blocks.json` 绑定的
      面板方块实体外观（`build_cable_blocks_entry`，与能挂面板的光缆同一条）；
    - 挖掘耗时与等级照光缆（`mining_components(CATEGORY_CABLE)`）：底座上的面板与光缆上的
      面板是同一种东西，挖起来不该一快一慢，也不为它在 `DESTROY_SECONDS` / `TIER_LEVEL`
      另立一行；
    - 方块实体、移除事件、邻居事件与光缆同一套（同一个 `CableStateKeeper` 维护状态）；
    - 不进创造栏（`HIDDEN_CATEGORY`），自身不掉落（`EMPTY_LOOT_TABLE`）；不写方块说明——
      没有对应物品，玩家拿不到它。
    """
    if not definition.accepts_parts:
        raise ValueError("%s 是面板底座却不能挂面板" % (device_id,))
    if HOLDER_LOOKS_LIKE not in definitions.cables:
        raise KeyError("面板底座借外观的 %s 不在光缆表里" % (HOLDER_LOOKS_LIKE,))
    components = mining_components(CATEGORY_CABLE)
    components["minecraft:geometry"] = {
        "identifier": geometry_id(short_name(HOLDER_LOOKS_LIKE)),
        "bone_visibility": holder_bone_visibility(definitions),
    }
    components["minecraft:light_dampening"] = 0
    components["minecraft:loot"] = EMPTY_LOOT_TABLE
    components["minecraft:material_instances"] = cable_material_instances(HOLDER_LOOKS_LIKE)
    # 默认（全 0）是一块面板都没有的小盒；每个排列另写自己的盒
    components["netease:aabb"] = cable_aabb(device_id, {})
    components["netease:block_entity"] = {"tick": False}
    components["netease:listen_block_remove"] = {"value": True}
    components["netease:neighborchanged_sendto_script"] = {"value": True}
    description = {
        "category": HIDDEN_CATEGORY,
        "identifier": device_id,
        "states": dict(
            (side_state(face), list(HOLDER_SIDE_VALUES)) for face in FACING_NAMES
        ),
    }
    permutations = [
        {
            "condition": cable_condition(sides),
            "components": {"netease:aabb": cable_aabb(device_id, sides)},
        }
        for sides in side_combinations(HOLDER_SIDE_VALUES)
    ]
    return {
        "format_version": FORMAT_VERSION,
        "minecraft:block": {
            "components": components,
            "description": description,
            "permutations": permutations,
        },
    }


# -- 面板物品的展示方块（2026-11-10 作者定方案 B）---------------------------------

#: 面板图集在地形图集里的键。图集文件由 `gen_cable_models.py` 生成（方块实体外观也用它）。
PANEL_ITEM_TEXTURE_KEY = "endernet:ender_cable_panels"


def panel_item_texture():
    # type: () -> tuple
    """面板图集 `(图集键, 路径)`。路径取自 `gen_cable_models`（图集是它出的）；它在模块顶层
    导入本模块，所以这里用时才导入。"""
    from gen_cable_models import PANEL_TEXTURE_DIR, PANELS
    return PANEL_ITEM_TEXTURE_KEY, PANEL_TEXTURE_DIR + "/" + PANELS


def build_part_item_json(part_id):
    """一种面板物品的展示方块：只为让物品栏、手持、掉落物显示 3D 面板而注册，玩家放不下它。

    基岩版普通物品在物品栏里只能画 2D 图标（`.claude/15` §2），所以四种 `*_terminal_part` 与
    P2P 网络隧道都改成同名方块，物品形态就是方块形态（隧道用默认面板那块屏幕）：
    - 几何 `geometry.endernet.<面板短名>`（`gen_cable_models.build_panel_item`：居中、屏幕朝南）。
      没有方块状态、没有排列，不写 `item_visual`；
    - 材质：面板图集，`alpha_test`（对应方块实体外观的 `entity_alphatest`）；
    - 进 `endernet` 创造栏；不写说明（作者 2026-11-10 删掉了面板的冗余提示）；
    - 放置由服务端 `ServerEntityTryPlaceBlockEvent` 拦掉（`server/adapters/events.py`），挂面板走
      客户端接管的意图；万一被放下也不掉东西（`EMPTY_LOOT_TABLE`），不挡光。
    """
    return {
        "format_version": FORMAT_VERSION,
        "minecraft:block": {
            "components": {
                "minecraft:geometry": geometry_id(short_name(part_id)),
                "minecraft:light_dampening": 0,
                "minecraft:loot": EMPTY_LOOT_TABLE,
                "minecraft:material_instances": {
                    "*": {"render_method": "alpha_test", "texture": PANEL_ITEM_TEXTURE_KEY},
                },
            },
            "description": {"category": CREATIVE_CATEGORY, "identifier": part_id},
        },
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
            if device_id in DISPLAY_DEVICES:
                # 槽位模块由方块实体外观画（实体在 endernet_rp/entity/，由
                # scripts/gen_slot_models.py 生成）。手持 / 掉落仍用方块几何。
                entry["client_entity"] = {
                    "hand_model_use_client_entity": False,
                    "identifier": DISPLAY_DEVICES[device_id][1],
                }
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
    for device_id in sorted(definitions.cables) + sorted(definitions.part_holders):
        payload[device_id] = build_cable_blocks_entry(definitions.get(device_id))
    for part_id in sorted(definitions.parts):
        # 面板展示方块：模型方块，不写 textures（同上）
        payload[part_id] = {"sound": "stone"}
    payload = dict(sorted(payload.items()))
    payload["format_version"] = [1, 1, 0]
    return payload


def build_terrain_texture(existing, definitions):
    """地形图集：设备与模型贴图条目按布局整体重建，其它条目原样保留。

    设备 / 模型目录下的条目**不沿用旧值**：换布局后旧键留在图集里会指向
    已删除的文件，游戏里不报错、`test_every_referenced_texture_file_exists` 却会红。

    面板底座不新增条目：它用的是 `HOLDER_LOOKS_LIKE` 那种光缆的键，已在光缆一节登记。
    """
    prefixes = (DEVICE_TEXTURE_DIR + "/", MODEL_TEXTURE_DIR + "/")
    texture_data = dict(
        (key, entry)
        for key, entry in existing.get("texture_data", {}).items()
        if not str(entry.get("textures", "")).startswith(prefixes)
    )
    for device_id in sorted(definitions.devices):
        for key, path in textures_of(device_id, statuses_of(device_id, definitions)):
            texture_data[key] = {"textures": path}
    for device_id in sorted(definitions.cables):
        key, path = cable_texture(device_id)
        texture_data[key] = {"textures": path}
    key, path = panel_item_texture()
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


def write_json(path, payload, check_only, dumps=plural_dumps):
    text = dumps(payload)
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
        for key, path in textures_of(device_id, statuses_of(device_id, definitions)):
            full = os.path.join(RP_ROOT, *(path + ".png").split("/"))
            if full not in seen and not os.path.isfile(full):
                seen.add(full)
                out.append((device_id, full, key))
        if layout_of(device_id) == LAYOUT_MODEL:
            geo = model_geometry_file(device_id)
            if not os.path.isfile(geo):
                out.append((device_id, geo, MODELS[device_id][0]))
    for device_id in sorted(definitions.cables):
        key, path = cable_texture(device_id)
        full = os.path.join(RP_ROOT, *(path + ".png").split("/"))
        if not os.path.isfile(full):
            out.append((device_id, full, key))
        geo = cable_geometry_file(device_id)
        if not os.path.isfile(geo):
            out.append((device_id, geo, geometry_id(short_name(device_id))))
    for device_id in sorted(definitions.part_holders):
        # 底座借 `HOLDER_LOOKS_LIKE` 的几何体与贴图；光缆缺了也在这里替底座报一次
        key, path = cable_texture(HOLDER_LOOKS_LIKE)
        full = os.path.join(RP_ROOT, *(path + ".png").split("/"))
        if not os.path.isfile(full):
            out.append((device_id, full, key))
        geo = cable_geometry_file(HOLDER_LOOKS_LIKE)
        if not os.path.isfile(geo):
            out.append((device_id, geo, geometry_id(short_name(HOLDER_LOOKS_LIKE))))
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

    for device_id in sorted(definitions.cables):
        definition = definitions.get(device_id)
        path = os.path.join(BP_BLOCKS, block_file_name(device_id))
        payload = build_cable_json(device_id, definition, definitions)
        if write_json(path, payload, args.check, dumps=compact_dumps):
            changed.append(os.path.relpath(path, REPO))

    for device_id in sorted(definitions.part_holders):
        definition = definitions.get(device_id)
        path = os.path.join(BP_BLOCKS, block_file_name(device_id))
        payload = build_part_holder_json(device_id, definition, definitions)
        if write_json(path, payload, args.check, dumps=compact_dumps):
            changed.append(os.path.relpath(path, REPO))

    for part_id in sorted(definitions.parts):
        path = os.path.join(BP_BLOCKS, block_file_name(part_id))
        if write_json(path, build_part_item_json(part_id), args.check):
            changed.append(os.path.relpath(path, REPO))

    if write_json(RP_BLOCKS, build_blocks_json(definitions), args.check):
        changed.append(os.path.relpath(RP_BLOCKS, REPO))

    terrain = read_json(RP_TERRAIN)
    if write_json(RP_TERRAIN, build_terrain_texture(terrain, definitions), args.check):
        changed.append(os.path.relpath(RP_TERRAIN, REPO))

    missing = missing_textures(definitions)
    block_count = (
        len(definitions.devices) + len(definitions.cables) + len(definitions.part_holders)
        + len(definitions.parts)
    )

    if args.check:
        if changed:
            print("以下文件需要重新生成（共 %d 个）：" % len(changed))
            for path in changed:
                print("  " + path)
            return 1
        print("方块 JSON 与资源映射已是最新（%d 个方块）" % block_count)
        return 0

    print("已生成 %d 个方块定义，更新 %d 个文件" % (block_count, len(changed)))
    if missing:
        print("仍缺文件（%d 个）；贴图可先用 scripts/gen_device_textures.py 补占位：" % len(missing))
        for device_id, path, key in missing:
            print("  %-40s -> %s" % (device_id, os.path.relpath(path, REPO)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
