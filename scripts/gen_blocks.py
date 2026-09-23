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
from __future__ import absolute_import, division, print_function

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
    DefinitionSet,
)

#: 方块 JSON 的 format_version。与同环境可正常运行的 SkyblueTech 保持一致：
#: `netease:tier` 与 traits 需要较新的格式。
FORMAT_VERSION = "1.21.70"

#: 创造栏分类，需与 endernet_bp/netease_tab/endernet_category_config.json 的 name 一致
CREATIVE_CATEGORY = "endernet"

#: 贴图键与路径。未列出的方块默认用自己的短名。
#: 已有美术资源的方块在这里指过去，不为它们生成占位图。
TEXTURE_OVERRIDES = {
    "endernet:bus_item_io": "endernet:bus_item_io",
    "endernet:storage_driver": "endernet:frame_top",
    "endernet:network_baseband_8": "endernet:network_baseband_8",
    "endernet:network_baseband_16": "endernet:network_baseband_16",
}

#: 已经存在贴图文件的贴图键，生成占位图时跳过这些。
EXISTING_TEXTURE_FILES = {
    "endernet:bus_item_io": "textures/endernet/devices/bus_item_io",
    "endernet:frame_top": "textures/endernet/devices/frame_top",
    "endernet:network_baseband_8": "textures/endernet/devices/network_baseband_8",
    "endernet:network_baseband_16": "textures/endernet/devices/network_baseband_16",
}

#: 需要逐 tick 的设备（`netease:block_entity.tick = true`）。
#: 依据是该设备是否要被动推进：控制器排程、总线搬运、装配仓加工、处理器工作态、
#: 能源接入口持续取电。基带与驱动器靠事件驱动，不需要逐 tick。
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
}


def short_name(device_id):
    return device_id.split(":", 1)[1]


def block_file_name(device_id):
    # type: (str) -> str
    """方块 JSON 的文件名：`endernet:foo_bar` -> `endernet_foo_bar.json`。

    与同环境可运行的 SkyblueTech（`skybluetech:air_compressor` ->
    `skybluetech_air_compressor.json`）以及本仓库已有文件保持一致：
    命名空间作为前缀、冒号换成下划线，不用裸的短名——否则不同命名空间的
    同名方块会在同一个目录里撞车。
    """
    return device_id.replace(":", "_") + ".json"


def texture_key(device_id):
    return TEXTURE_OVERRIDES.get(device_id, "endernet:" + short_name(device_id))


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
    components["minecraft:material_instances"] = {
        "*": {
            "render_method": "opaque",
            "texture": texture_key(device_id),
        }
    }

    # 自定义方块实体：tick 决定是否派发 ServerBlockEntityTickEvent（每秒 20 次）。
    # 即使不需要 tick 也要声明，否则方块实体数据无处存放。
    components["netease:block_entity"] = {"tick": bool(TICKING.get(device_id, False))}

    # 移除事件白名单。没有它引擎不会派发 BlockRemoveServerEvent，
    # 设备拆除将完全无感。（运行时另有 AddBlocksToBlockRemoveListener 兜底。）
    components["netease:listen_block_remove"] = {"value": True}

    if device_id in TIPS:
        components["netease:custom_tips"] = {"value": "\n§7" + TIPS[device_id]}

    description = {
        "category": CREATIVE_CATEGORY,
        "identifier": device_id,
    }
    if DIRECTIONAL.get(device_id):
        description["traits"] = {
            "minecraft:placement_direction": {
                "enabled_states": ["minecraft:cardinal_direction"],
                "y_rotation_offset": 180.0,
            }
        }

    return {
        "format_version": FORMAT_VERSION,
        "minecraft:block": {
            "components": components,
            "description": description,
        },
    }


def build_blocks_json(definitions):
    """资源包的 blocks.json：方块标识 -> 贴图键。"""
    payload = {}
    for device_id in sorted(definitions.devices):
        payload[device_id] = {
            "textures": texture_key(device_id),
            "sound": "stone",
        }
    payload["format_version"] = [1, 1, 0]
    return payload


def build_terrain_texture(existing, definitions):
    """在地形图集里保证每个方块都有贴图条目，保留已有条目。"""
    texture_data = dict(existing.get("texture_data", {}))
    for device_id in sorted(definitions.devices):
        key = texture_key(device_id)
        if key in texture_data:
            continue
        if key in EXISTING_TEXTURE_FILES:
            texture_data[key] = {"textures": EXISTING_TEXTURE_FILES[key]}
        else:
            texture_data[key] = {
                "textures": "textures/endernet/devices/" + short_name(device_id)
            }
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
    """列出还没有贴图文件的方块。"""
    out = []
    for device_id in sorted(definitions.devices):
        key = texture_key(device_id)
        if key in EXISTING_TEXTURE_FILES:
            continue
        path = os.path.join(
            RP_ROOT, "textures", "endernet", "devices", short_name(device_id) + ".png"
        )
        if not os.path.isfile(path):
            out.append((device_id, path, key))
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
        print("仍需补贴图的方块（%d 个）：" % len(missing))
        for device_id, path, key in missing:
            print("  %-40s -> %s" % (device_id, os.path.relpath(path, REPO)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
