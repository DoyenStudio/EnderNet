# coding=utf-8
"""由定义表生成物品 JSON 与物品图集（存储介质 + 工厂样板 + 内存条 + 工具）。

    python scripts/gen_items.py          # 生成
    python scripts/gen_items.py --check  # 只检查是否与当前文件一致（CI 用）

单一事实来源是 `endernet_bp/endernet_scripts/endernet/common/definitions.py`：

- **哪些物品已经做出来了**由 `ITEM_MEDIUMS` / `ITEM_TEMPLATES` /
  `ITEM_MEMORIES` 决定；
- 介质的容量、类型数、读写速度从各自的 `MediumSpec` 读出，
  因此游戏内提示里的数字不可能与领域层的规格分叉；
- 样板的用途说明从 `TemplateSpec.usage` 读出（items.md 的原文）；
- 内存条的容量从 `MemorySpec.capacity_bytes` 读出，同理不会分叉；
- 工具（网络扳手）由 `ITEM_TOOLS` 决定，说明写在本文件的 `TOOL_TIPS`；
- 四种终端面板（定义表的 `parts`，`*_terminal_part`）**不在这里**：2026-11-10 作者定方案 B，
  它们注册成同名的展示方块（`gen_blocks.py` 的 `build_part_item_json`），物品栏里显示 3D 面板，
  物品形态是方块物品，不占物品 JSON 与物品图集。

产物三份：

    endernet_bp/netease_items_beh/endernet_<短名>.json    行为包物品定义
    endernet_rp/netease_items_res/endernet_<短名>.json    资源包物品定义（图标）
    endernet_rp/textures/item_texture.json                物品图集

**目录名与格式版本跟随已验证的 NetEase 路径**（行为包 `netease_items_beh/`、
资源包 `netease_items_res/`、`format_version "1.10"`），与同环境可正常运行的
SkyblueTech 一致。本项目方块走的是原版 `blocks/`，但物品没有已验证的原版先例，
因此不拿它当赌注。

贴图本身由 `gen_placeholder_textures.py` 生成到 `textures/endernet/items/`；扳手的
占位图标由 `gen_cable_models.py --icons` 补（先跑本脚本登记图集，它只补登记了的）。
"""

import argparse
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_ROOT = os.path.join(REPO, "endernet_bp", "endernet_scripts")
BP_ITEMS = os.path.join(REPO, "endernet_bp", "netease_items_beh")
RP_ITEMS = os.path.join(REPO, "endernet_rp", "netease_items_res")
RP_ITEM_TEXTURE = os.path.join(REPO, "endernet_rp", "textures", "item_texture.json")
ITEM_TEXTURE_DIR = os.path.join(REPO, "endernet_rp", "textures", "endernet", "items")

if SCRIPTS_ROOT not in sys.path:
    sys.path.insert(0, SCRIPTS_ROOT)
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _console import use_utf8_stdout  # noqa: E402
from gen_blocks import CREATIVE_CATEGORY, plural_dumps, short_name  # noqa: E402

from endernet.common.definitions import (  # noqa: E402
    DefinitionSet,
    devices_for_mode,
)

#: NetEase 物品 JSON 的格式版本。与 SkyblueTech 的可用物品保持一致。
ITEM_FORMAT_VERSION = "1.10"

#: **没有自带堆叠上限的**物品的默认值：介质。
#:
#: 介质要携带自己的身份与内容；允许堆叠的话，一叠 64 个会共享同一份物品
#: 数据，拔出其中一个就无法确定它到底是哪一块盘。SkyblueTech 的同类物品
#: 同样取 1。
#:
#: **样板不读这个默认值**：它们各自带 `TemplateSpec.max_stack_size`
#: （`definitions.py`，2026-10-02 由 User 定：工厂样板 64）。
#: 工厂样板取 64 是为样板终端的输入槽能一次放一叠，代价是一条**尚未验证**的
#: 引擎风险——配方编码在 `userData` 里，若引擎会把 `userData` 不同的物品并成
#: 一叠，两块不同配置的样板会互相覆盖。安全的机制是「引擎本就按 `userData`
#: 分堆」。见 `plans/items.md` 的样板一节与 `plans/tasks/11-交付说明.md`。
ITEM_MAX_STACK_SIZE = 1

#: 工具的物品说明。每件登记为物品的工具都要有一条，缺了生成时报错（不写一条
#: 空说明了事）。扳手这条是作者 2026-11-10 在物品 JSON 里定的原文。
TOOL_TIPS = {
    "endernet:network_wrench": "用于对终界网络设备进行维护",
}


def max_stack_size_of(spec):
    # type: (object) -> int
    """该物品的堆叠上限。样板自带，其余退回 `ITEM_MAX_STACK_SIZE`。

    用 `getattr` 而不是给两种规格都加字段：`MediumSpec` 的堆叠上限是**常量**
    （没有一件介质不是 1），给每个规格对象复制一份只会让「哪里才是那个数」
    多出一个副本。
    """
    return getattr(spec, "max_stack_size", ITEM_MAX_STACK_SIZE)


def item_file_name(item_id):
    # type: (str) -> str
    """物品 JSON 的文件名：与方块同一套命名（命名空间作前缀，冒号换下划线）。"""
    return item_id.replace(":", "_") + ".json"


def item_texture_name(item_id):
    # type: (str) -> str
    """贴图文件名（不含扩展名）：短名。`gen_placeholder_textures.py` 也读它。"""
    return short_name(item_id)


def item_icon_key(item_id):
    # type: (str) -> str
    """图集里的贴图键。物品标识本身就是键，与原版图集惯例一致。"""
    return item_id


def medium_tip(spec):
    # type: (object) -> str
    """介质说明。数字全部来自定义表，不在这里另写一份。"""
    return "\n§7容量：%d 类型 %d 单位物\n§7读 %d 单位物/s，写 %d 单位物/s" % (
        spec.max_types,
        spec.capacity_units,
        spec.read_units_per_second,
        spec.write_units_per_second,
    )


def template_tip(spec, definitions):
    # type: (object, object) -> str
    """样板说明。用途与设备名**全部取自定义表**，不在这里另写一份。

    「放进哪台设备」由 `PATTERN_DEVICES` 反查，设备名取定义表的 `label`。
    写死「装配仓」「总线样板接口」的话，那两句话就有三个副本，
    而玩家最容易搞错的恰恰是这一点（两块样板的名字很像）。
    """
    names = [
        definitions.get(device_id).label
        for device_id in sorted(devices_for_mode("crafting") | devices_for_mode("factory"))
        if definitions.has(device_id)
    ]
    where = "、".join(names) if names else "（尚未指定设备）"
    return "\n§7%s\n§7放入%s使用。" % (spec.usage, where)


def memory_tip(spec):
    # type: (object) -> str
    """内存条说明。容量取自定义表，不在这里另写一份。

    提示里**不重复型号名**（物品名已经写着「16KB 集成式内存卡」），
    只给字节数——它是玩法数值，也是内存槽实际记账用的那个单位。
    """
    return "\n§7内存容量 %d 字节" % (spec.capacity_bytes,)


def tool_tip(spec):
    # type: (object) -> str
    """工具说明：取 `TOOL_TIPS`，没写的工具直接报错。"""
    if spec.id not in TOOL_TIPS:
        raise KeyError("工具 %s 没有在 TOOL_TIPS 里写说明" % (spec.id,))
    return "\n§7" + TOOL_TIPS[spec.id]


def build_behavior_json(spec, tip):
    """行为包物品定义。`tip` 由调用方按物品类型给出（`item_tips`）。"""
    components = {
        "minecraft:max_stack_size": max_stack_size_of(spec),
        "netease:customtips": {"value": tip},
    }
    return {
        "format_version": ITEM_FORMAT_VERSION,
        "minecraft:item": {
            "components": components,
            "description": {
                "category": CREATIVE_CATEGORY,
                "identifier": spec.id,
            },
        },
    }


def build_resource_json(spec):
    """资源包物品定义。资源侧**没有** category，与方块那边的分工不同。"""
    return {
        "format_version": ITEM_FORMAT_VERSION,
        "minecraft:item": {
            "components": {"minecraft:icon": item_icon_key(spec.id)},
            "description": {"identifier": spec.id},
        },
    }


def build_item_texture(existing, definitions):
    """物品图集。保留已有条目，只补本模组自己的物品（`item_tips` 列出的全部）。"""
    texture_data = dict(existing.get("texture_data", {}))
    texture_data.pop("endernet:crafting_template", None)
    # 终端面板 2026-11-10 起是方块物品（同名展示方块），物品栏画方块几何，不再占物品图集
    for part_id in definitions.parts:
        texture_data.pop(item_icon_key(part_id), None)
    for spec, _tip in item_tips(definitions):
        texture_data[item_icon_key(spec.id)] = {
            "textures": "textures/endernet/items/" + item_texture_name(spec.id)
        }
    payload = dict(existing)
    payload["resource_pack_name"] = existing.get("resource_pack_name", "vanilla")
    payload["texture_name"] = existing.get("texture_name", "atlas.items")
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
    """列出还没有贴图文件的物品。"""
    out = []
    for spec, _tip in item_tips(definitions):
        path = os.path.join(ITEM_TEXTURE_DIR, item_texture_name(spec.id) + ".png")
        if not os.path.isfile(path):
            out.append((spec.id, path))
    return out


def item_tips(definitions):
    # type: (object) -> list
    """`(规格, 提示文本)` 清单：**本脚本生成的全部物品**。各类的提示规则不同，但只有
    这一处分派：介质给容量与读写速度，样板给用途与「放进哪台设备」，内存条给字节容量，
    工具给 `TOOL_TIPS`。终端面板不在这里（2026-11-10 起是同名展示方块，见模块说明）。"""
    out = []
    for spec in definitions.item_medium_specs():
        out.append((spec, medium_tip(spec)))
    for spec in definitions.item_template_specs():
        out.append((spec, template_tip(spec, definitions)))
    for spec in definitions.item_memory_specs():
        out.append((spec, memory_tip(spec)))
    for spec in definitions.item_tool_specs():
        out.append((spec, tool_tip(spec)))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成 EnderNet 物品 JSON 与物品图集")
    parser.add_argument("--check", action="store_true", help="只检查是否需要重新生成")
    use_utf8_stdout()
    args = parser.parse_args(argv)

    definitions = DefinitionSet()
    changed = []

    for spec, tip in item_tips(definitions):
        name = item_file_name(spec.id)
        behavior = os.path.join(BP_ITEMS, name)
        resource = os.path.join(RP_ITEMS, name)
        if write_json(behavior, build_behavior_json(spec, tip), args.check):
            changed.append(os.path.relpath(behavior, REPO))
        if write_json(resource, build_resource_json(spec), args.check):
            changed.append(os.path.relpath(resource, REPO))

    atlas = read_json(RP_ITEM_TEXTURE)
    if write_json(RP_ITEM_TEXTURE, build_item_texture(atlas, definitions), args.check):
        changed.append(os.path.relpath(RP_ITEM_TEXTURE, REPO))

    missing = missing_textures(definitions)
    # **总数从 `item_tips()` 派生**（它就是上面逐个写出的那份清单），不在这里把各类再数
    # 一遍：数漏一类的话 `--check` 照旧报「已是最新」，而那正是「新物品没生成」最难发现的一处。
    total = len(item_tips(definitions))

    if args.check:
        if changed:
            print("以下文件需要重新生成（共 %d 个）：" % len(changed))
            for path in changed:
                print("  " + path)
            return 1
        if missing:
            # **`--check` 也要看贴图在不在。** 少了这一段，删掉一张贴图之后
            # `--check` 照旧报「已是最新」并返回 0，而图集里那条指向一个
            # 不存在的文件——CI 与交付说明里的那条命令于是都不拦这件事。
            print("以下物品缺贴图文件（共 %d 个）：" % len(missing))
            for item_id, path in missing:
                print("  %-40s -> %s" % (item_id, os.path.relpath(path, REPO)))
            print("请先运行 python scripts/gen_placeholder_textures.py")
            return 1
        print("物品 JSON 与物品图集已是最新（%d 个物品）" % total)
        return 0

    print("已生成 %d 个物品，更新 %d 个文件" % (total, len(changed)))
    if missing:
        print("仍缺贴图的物品（%d 个）：" % len(missing))
        for item_id, path in missing:
            print("  %-40s -> %s" % (item_id, os.path.relpath(path, REPO)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
