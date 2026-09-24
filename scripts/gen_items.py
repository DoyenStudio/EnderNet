# coding=utf-8
"""由介质定义表生成存储介质物品 JSON 与物品图集。

    python scripts/gen_items.py          # 生成
    python scripts/gen_items.py --check  # 只检查是否与当前文件一致（CI 用）

单一事实来源是 `endernet_bp/endernet_scripts/endernet/common/definitions.py`：
**哪些介质已经做成了游戏物品**由 `ITEM_MEDIUMS` 决定，容量、类型数、读写速度
由各自的 `MediumSpec` 读出，因此游戏内提示里的数字不可能与领域层的规格分叉。

产物三份：

    endernet_bp/netease_items_beh/endernet_<短名>.json    行为包物品定义
    endernet_rp/netease_items_res/endernet_<短名>.json    资源包物品定义（图标）
    endernet_rp/textures/item_texture.json                物品图集

**目录名与格式版本跟随已验证的 NetEase 路径**（行为包 `netease_items_beh/`、
资源包 `netease_items_res/`、`format_version "1.10"`），与同环境可正常运行的
SkyblueTech 一致。本项目方块走的是原版 `blocks/`，但物品没有已验证的原版先例，
因此不拿它当赌注。

贴图本身由 `gen_placeholder_textures.py` 生成到 `textures/items/`。
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
ITEM_TEXTURE_DIR = os.path.join(REPO, "endernet_rp", "textures", "items")

if SCRIPTS_ROOT not in sys.path:
    sys.path.insert(0, SCRIPTS_ROOT)
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _console import use_utf8_stdout  # noqa: E402
from gen_blocks import CREATIVE_CATEGORY, plural_dumps, short_name  # noqa: E402

from endernet.common.definitions import DefinitionSet  # noqa: E402

#: NetEase 物品 JSON 的格式版本。与 SkyblueTech 的可用物品保持一致。
ITEM_FORMAT_VERSION = "1.10"

#: 介质的堆叠上限。
#:
#: **必须是 1。** 介质要携带自己的身份与内容；允许堆叠的话，一叠 64 个会共享
#: 同一份物品数据，拔出其中一个就无法确定它到底是哪一块盘。SkyblueTech 的
#: 两类样板同样取 1。
MEDIUM_MAX_STACK_SIZE = 1


def item_file_name(medium_id):
    # type: (str) -> str
    """物品 JSON 的文件名：与方块同一套命名（命名空间作前缀，冒号换下划线）。"""
    return medium_id.replace(":", "_") + ".json"


def item_texture_name(medium_id):
    # type: (str) -> str
    """贴图文件名（不含扩展名）。`gen_placeholder_textures.py` 也读它。"""
    return "endernet_" + short_name(medium_id)


def item_icon_key(medium_id):
    # type: (str) -> str
    """图集里的贴图键。物品标识本身就是键，与原版图集惯例一致。"""
    return medium_id


def medium_tip(spec):
    # type: (object) -> str
    """物品说明。数字全部来自定义表，不在这里另写一份。"""
    return "\n§7容量：%d 类型 %d 单位物\n§7读 %d 单位物/s，写 %d 单位物/s" % (
        spec.max_types,
        spec.capacity_units,
        spec.read_units_per_second,
        spec.write_units_per_second,
    )


def build_behavior_json(spec):
    """行为包物品定义。"""
    return {
        "format_version": ITEM_FORMAT_VERSION,
        "minecraft:item": {
            "components": {
                "minecraft:max_stack_size": MEDIUM_MAX_STACK_SIZE,
                "netease:customtips": {"value": medium_tip(spec)},
            },
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
    """物品图集。保留已有条目，只补本模组的介质。"""
    texture_data = dict(existing.get("texture_data", {}))
    for spec in definitions.item_medium_specs():
        texture_data[item_icon_key(spec.id)] = {
            "textures": "textures/items/" + item_texture_name(spec.id)
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
    """列出还没有贴图文件的介质物品。"""
    out = []
    for spec in definitions.item_medium_specs():
        path = os.path.join(ITEM_TEXTURE_DIR, item_texture_name(spec.id) + ".png")
        if not os.path.isfile(path):
            out.append((spec.id, path))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成 EnderNet 介质物品 JSON 与物品图集")
    parser.add_argument("--check", action="store_true", help="只检查是否需要重新生成")
    use_utf8_stdout()
    args = parser.parse_args(argv)

    definitions = DefinitionSet()
    changed = []

    for spec in definitions.item_medium_specs():
        name = item_file_name(spec.id)
        behavior = os.path.join(BP_ITEMS, name)
        resource = os.path.join(RP_ITEMS, name)
        if write_json(behavior, build_behavior_json(spec), args.check):
            changed.append(os.path.relpath(behavior, REPO))
        if write_json(resource, build_resource_json(spec), args.check):
            changed.append(os.path.relpath(resource, REPO))

    atlas = read_json(RP_ITEM_TEXTURE)
    if write_json(RP_ITEM_TEXTURE, build_item_texture(atlas, definitions), args.check):
        changed.append(os.path.relpath(RP_ITEM_TEXTURE, REPO))

    missing = missing_textures(definitions)

    if args.check:
        if changed:
            print("以下文件需要重新生成（共 %d 个）：" % len(changed))
            for path in changed:
                print("  " + path)
            return 1
        print("介质物品 JSON 与物品图集已是最新（%d 个介质物品）" % len(definitions.item_mediums))
        return 0

    print(
        "已生成 %d 个介质物品，更新 %d 个文件"
        % (len(definitions.item_mediums), len(changed))
    )
    if missing:
        print("仍缺贴图的介质物品（%d 个）：" % len(missing))
        for medium_id, path in missing:
            print("  %-40s -> %s" % (medium_id, os.path.relpath(path, REPO)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
