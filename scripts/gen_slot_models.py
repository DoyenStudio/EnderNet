# coding=utf-8
"""把槽位模块（硬盘、内存条）组装成驱动器 / 内存槽的方块实体外观。

    python scripts/gen_slot_models.py          # 生成
    python scripts/gen_slot_models.py --check  # 只检查是否与当前文件一致
    python scripts/gen_slot_models.py --probe  # 真机排查：每个槽都先显示默认模块

`--probe` 只改实体的初始变量（全部槽 = 1），**不依赖服务端与网络**：进游戏放一台，
看得见 10 个默认模块 = 实体外观、位置、朝向都通了，剩下的问题在消息链路上；
看不见 = 外观本身没起来。排查完**不带参数再跑一次**（`test_slot_display` 会拦住
留在仓库里的探针版本）。

## 模块源（手工或 Blockbench 制作，这是唯一要改的地方）

    endernet_rp/models/slot_modules/<种类>/<名字>.geo.json
    endernet_rp/textures/endernet/slot_modules/<种类>/<名字>.png

- 种类：`disk`（驱动器里的硬盘）或 `memory`（内存槽里的内存条）。
- 名字：`default`（**必须有**，没有专属模型的型号都用它），或物品短名
  （`softdisk_1k`、`memory_4k`……），有了就替换那个型号的显示。
- 标准尺寸与本地坐标（文件坐标，水平居中、底面贴 y=0），超出就报错：
  硬盘平放 6(x) x 2(y) x 6(z)，正面朝 north（z=-3）；
  内存条竖插 6(x) x 8(y) x 1(z)。
- 贴图大小随意（默认的是 16x16），`texture_width/height` 要与 png 一致。
  骨骼不要带旋转（方块级旋转由实体整体负责）；方块自身的 rotation/pivot 可以有。

## 产物（都由本脚本生成，不要手改）

    endernet_rp/models/entity/<设备>_modules.geo.json        每槽 x 每种模块一根骨骼
    endernet_rp/textures/endernet/entity/slot_modules.png    全部模块贴图拼成的图集
    endernet_rp/entity/<设备>_modules.entity.json            方块实体外观
    endernet_rp/render_controllers/endernet_slot_modules.render_controllers.json
    endernet_rp/animations/endernet_slot_modules.animation.json

骨骼 `s<槽>_<名字>` 的显隐由 `variable.s<槽>` 决定，取值规则见
`endernet/common/channels/slot_display.py`（运行时与本脚本共用）。
"""

import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPTS_ROOT = os.path.join(REPO, "endernet_bp", "endernet_scripts")
RP_ROOT = os.path.join(REPO, "endernet_rp")
for path in (SCRIPTS_ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from _console import use_utf8_stdout  # noqa: E402
from _png import png_bytes, read_png  # noqa: E402

from endernet.common.definitions import DefinitionSet  # noqa: E402
from endernet.common.channels.slot_display import (  # noqa: E402
    DISK,
    DISPLAY_DEVICES,
    FACING_VARIABLE,
    FIRST_MODEL,
    MEMORY,
    UNKNOWN,
    model_items,
    slot_count,
    slot_variable,
)

SOURCE_GEO_DIR = os.path.join(RP_ROOT, "models", "slot_modules")
SOURCE_TEX_DIR = os.path.join(RP_ROOT, "textures", "endernet", "slot_modules")
DEFAULT = "default"

ATLAS_TEXTURE = "textures/endernet/entity/slot_modules"
ANIMATION_ID = "animation.endernet.slot_modules.facing"
ANIMATION_FILE = os.path.join(RP_ROOT, "animations", "endernet_slot_modules.animation.json")
CONTROLLER_FILE = os.path.join(
    RP_ROOT, "render_controllers", "endernet_slot_modules.render_controllers.json"
)
#: 实体材质：`entity_alphatest` 允许模块贴图里有镂空。它不剔除背面，因此模块
#: 方块统一内缩 `INSET`（`inflate`），免得与槽壁共面闪烁。
MATERIAL = "entity_alphatest"
INSET = 0.05

#: 模块的标准包围盒（文件坐标，本地原点 = 底面中心）：`(最小角, 最大角)`。
STANDARD_BOXES = {
    DISK: ((-3, 0, -3), (3, 2, 3)),
    MEMORY: ((-3, 0, -0.5), (3, 8, 0.5)),
}

#: **方块实体模型的原点在方块的角点，不在中心**（2026-11-06 真机：按中心放的
#: 模块整体偏出半格、绕角点旋转，结果看不见）。证据是 SkyblueTech 挖掘机的实体
#: 几何：底座方块 `origin [-16, 0, 0] size [16, 4, 16]`，旋转用的 root 枢轴
#: `[-8, 8, 8]`——文件坐标里这一格是 x -16..0、z 0..16。
#: 下面的锚点仍按**方块几何体**的坐标写（中心原点，与 `models/blocks/` 对得上），
#: 放进实体时统一加上这个偏移。
ENTITY_ORIGIN = (-8, 0, 8)
#: 朝向旋转绕方块中心（照挖掘机的 root 枢轴）
ROOT_PIVOT = [-8, 8, 8]

#: 槽位锚点（方块几何体坐标，模块本地原点放到这里）。**顺序 = 容器槽号**。
#:
#: 界面是 5 列 x 2 行（`grid_dimensions [5, 2]`），第 1 行（槽 0~4）对应站在
#: 正面看的**左手边**，第 2 行（槽 5~9）对应右手边。左手边 = 文件 x 为负，
#: 与用户驱动器模型里 `left` 骨骼同侧。
#:
#: - 驱动器：每列 5 格从上到下（舱底 y = 13/10/7/4/1，舱高 2），硬盘前脸缩进
#:   前框 1 格（z -7..-1）。
#: - 内存槽：每组 5 条从前到后（槽口 z = -6/-3/0/3/6，宽 1），条底贴槽底 y=3、
#:   露出外框顶面 3 格。
ANCHORS = {
    "endernet:storage_driver": tuple(
        (x, y, -4) for x in (-4, 4) for y in (13, 10, 7, 4, 1)
    ),
    "endernet:memory_slot": tuple(
        (x, 3, z) for x in (-4, 4) for z in (-5.5, -2.5, 0.5, 3.5, 6.5)
    ),
}


class SlotModelError(Exception):
    pass


def short_name(item_id):
    return item_id.split(":", 1)[1]


def device_base(device_id):
    return short_name(device_id)


def geometry_id(device_id):
    return "geometry.endernet.%s.modules" % device_base(device_id)


def controller_id(device_id):
    return "controller.render.endernet.%s.modules" % device_base(device_id)


def geometry_file(device_id):
    return os.path.join(RP_ROOT, "models", "entity", device_base(device_id) + "_modules.geo.json")


def entity_file(device_id):
    return os.path.join(RP_ROOT, "entity", device_base(device_id) + "_modules.entity.json")


def atlas_file():
    return os.path.join(RP_ROOT, *(ATLAS_TEXTURE + ".png").split("/"))


def rel(path):
    """报错与输出里用的相对路径；不在同一个盘上就给绝对路径（Windows 下 relpath 会抛）。"""
    try:
        return os.path.relpath(path, REPO)
    except ValueError:
        return path


def plural_dumps(payload):
    return json.dumps(payload, indent=4, ensure_ascii=False) + "\n"


def num(value):
    """整数写成整数、小数保留三位：输出稳定，Blockbench 打开也干净。"""
    value = round(float(value), 3)
    return int(value) if value == int(value) else value


# -- 读模块源 --------------------------------------------------------------


class Module(object):
    def __init__(self, kind, name, cubes, pixels, width, height):
        self.kind = kind
        self.name = name
        self.cubes = cubes
        self.pixels = pixels
        self.width = width
        self.height = height
        #: 在图集里的纵向偏移，`build_atlas` 填
        self.v_offset = 0


def read_json(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_module(kind, name):
    geo_path = os.path.join(SOURCE_GEO_DIR, kind, name + ".geo.json")
    tex_path = os.path.join(SOURCE_TEX_DIR, kind, name + ".png")
    label = "%s/%s" % (kind, name)
    if not os.path.isfile(tex_path):
        raise SlotModelError("模块 %s 缺贴图 %s" % (label, rel(tex_path)))
    geometries = read_json(geo_path).get("minecraft:geometry") or []
    if len(geometries) != 1:
        raise SlotModelError("模块 %s 应当恰好有一个几何体，得到 %d 个" % (label, len(geometries)))
    geometry = geometries[0]
    description = geometry.get("description", {})
    pixels = read_png(tex_path)
    width, height = len(pixels[0]), len(pixels)
    if (description.get("texture_width"), description.get("texture_height")) != (width, height):
        raise SlotModelError(
            "模块 %s 的 texture_width/height %r 与贴图 %dx%d 不一致"
            % (label, (description.get("texture_width"), description.get("texture_height")),
               width, height)
        )
    (lo, hi) = STANDARD_BOXES[kind]
    cubes = []
    for bone in geometry.get("bones", []):
        if any(bone.get("rotation") or [0, 0, 0]):
            raise SlotModelError("模块 %s 的骨骼 %s 带旋转；请把旋转放到方块上" % (label, bone.get("name")))
        for cube in bone.get("cubes", []):
            origin, size = cube["origin"], cube["size"]
            for axis in range(3):
                if origin[axis] < lo[axis] - 1e-6 or origin[axis] + size[axis] > hi[axis] + 1e-6:
                    raise SlotModelError(
                        "模块 %s 超出标准尺寸：方块 origin=%r size=%r，允许范围 %r..%r"
                        % (label, origin, size, lo, hi)
                    )
            cubes.append(cube)
    if not cubes:
        raise SlotModelError("模块 %s 没有任何方块" % label)
    return Module(kind, name, cubes, pixels, width, height)


def module_names(kind, items):
    """这一种类有哪些模块源：`default` 在前，其余按物品表顺序。"""
    directory = os.path.join(SOURCE_GEO_DIR, kind)
    present = set()
    if os.path.isdir(directory):
        present = set(
            name[: -len(".geo.json")] for name in os.listdir(directory) if name.endswith(".geo.json")
        )
    if DEFAULT not in present:
        raise SlotModelError(
            "缺默认模块 %s" % rel(os.path.join(directory, DEFAULT + ".geo.json"))
        )
    known = [short_name(item_id) for item_id in items]
    unknown = sorted(present - set(known) - {DEFAULT})
    if unknown:
        raise SlotModelError(
            "%s 下有认不出的模块 %r：文件名要么是 default，要么是这些物品短名之一 %r"
            % (rel(directory), unknown, known)
        )
    return [DEFAULT] + [name for name in known if name in present]


def load_modules(definitions):
    """`{种类: [Module, ...]}`，每种 `default` 在前。"""
    return dict(
        (kind, [load_module(kind, name) for name in module_names(kind, model_items(definitions, kind))])
        for kind in (DISK, MEMORY)
    )


# -- 图集 ------------------------------------------------------------------


def build_atlas(modules):
    """全部模块贴图从上到下拼成一张。返回像素表，并填好每个模块的 `v_offset`。"""
    ordered = modules[DISK] + modules[MEMORY]
    width = max(module.width for module in ordered)
    rows = []
    for module in ordered:
        module.v_offset = len(rows)
        for row in module.pixels:
            rows.append(list(row) + [(0, 0, 0, 0)] * (width - len(row)))
    return rows


# -- 几何体 ----------------------------------------------------------------


def shifted_uv(uv, v_offset):
    if isinstance(uv, list):
        return [num(uv[0]), num(uv[1] + v_offset)]
    faces = {}
    for face, entry in sorted(uv.items()):
        moved = dict(entry)
        moved["uv"] = [num(entry["uv"][0]), num(entry["uv"][1] + v_offset)]
        faces[face] = moved
    return faces


def entity_anchor(anchor):
    """方块几何体坐标 -> 方块实体坐标。"""
    return tuple(a + o for a, o in zip(anchor, ENTITY_ORIGIN))


def placed_cube(cube, anchor, v_offset):
    ax, ay, az = anchor
    out = {
        "origin": [num(cube["origin"][0] + ax), num(cube["origin"][1] + ay), num(cube["origin"][2] + az)],
        "size": [num(value) for value in cube["size"]],
        "inflate": num(cube.get("inflate", 0) - INSET),
        "uv": shifted_uv(cube["uv"], v_offset),
    }
    if "pivot" in cube:
        out["pivot"] = [num(cube["pivot"][0] + ax), num(cube["pivot"][1] + ay), num(cube["pivot"][2] + az)]
    if "rotation" in cube:
        out["rotation"] = [num(value) for value in cube["rotation"]]
    if cube.get("mirror"):
        out["mirror"] = True
    return out


def bone_name(slot, module_name):
    return "s%d_%s" % (slot, module_name)


def build_geometry(device_id, kind, modules, atlas_size):
    anchors = ANCHORS[device_id]
    if len(anchors) != slot_count(kind):
        raise SlotModelError(
            "%s 有 %d 个锚点，但定义表说它有 %d 个槽" % (device_id, len(anchors), slot_count(kind))
        )
    bones = [{"name": "root", "pivot": list(ROOT_PIVOT)}]
    for slot, block_anchor in enumerate(anchors):
        anchor = entity_anchor(block_anchor)
        for module in modules[kind]:
            bones.append({
                "name": bone_name(slot, module.name),
                "parent": "root",
                "pivot": [num(value) for value in anchor],
                "cubes": [placed_cube(cube, anchor, module.v_offset) for cube in module.cubes],
            })
    return {
        "format_version": "1.12.0",
        "minecraft:geometry": [{
            "description": {
                "identifier": geometry_id(device_id),
                "texture_width": atlas_size[0],
                "texture_height": atlas_size[1],
                # 照挖掘机：原点在角点，包围盒要罩住整格
                "visible_bounds_width": 3,
                "visible_bounds_height": 2.5,
                "visible_bounds_offset": [0, 0.75, 0],
            },
            "bones": bones,
        }],
    }


# -- 渲染控制器 / 实体 / 动画 -----------------------------------------------


def visibility_rules(kind, modules, items):
    """`[(骨骼名, molang)]`：每槽恰好一根骨骼可见（或都不可见 = 空槽）。"""
    specific = [
        (module.name, FIRST_MODEL + [short_name(i) for i in items].index(module.name))
        for module in modules[kind]
        if module.name != DEFAULT
    ]
    rules = []
    for slot in range(slot_count(kind)):
        variable = slot_variable(slot)
        default = ["%s >= %d" % (variable, UNKNOWN)] + [
            "%s != %d" % (variable, value) for _name, value in specific
        ]
        rules.append((bone_name(slot, DEFAULT), " && ".join(default)))
        for name, value in specific:
            rules.append((bone_name(slot, name), "%s == %d" % (variable, value)))
    return rules


def build_controllers(definitions, modules):
    controllers = {}
    for device_id, (kind, _entity) in sorted(DISPLAY_DEVICES.items()):
        rules = visibility_rules(kind, modules, model_items(definitions, kind))
        controllers[controller_id(device_id)] = {
            "geometry": "Geometry.default",
            "materials": [{"*": "Material.default"}],
            "textures": ["Texture.default"],
            "part_visibility": [{bone: expression} for bone, expression in rules],
        }
    return {"format_version": "1.8.0", "render_controllers": controllers}


def build_entity(device_id, kind, probe=False):
    slots = [slot_variable(slot) for slot in range(slot_count(kind))]
    initial = [(FACING_VARIABLE, 0)] + [(name, UNKNOWN if probe else 0) for name in slots]
    return {
        "format_version": "1.10.0",
        "minecraft:client_entity": {
            "description": {
                "identifier": DISPLAY_DEVICES[device_id][1],
                "materials": {"default": MATERIAL},
                "textures": {"default": ATLAS_TEXTURE},
                "geometry": {"default": geometry_id(device_id)},
                "scripts": {
                    "initialize": ["%s = %d;" % pair for pair in initial],
                    "animate": ["facing"],
                },
                "animations": {"facing": ANIMATION_ID},
                "render_controllers": [controller_id(device_id)],
            }
        },
    }


def build_animation():
    return {
        "format_version": "1.8.0",
        "animations": {
            ANIMATION_ID: {
                "loop": True,
                "bones": {"root": {"rotation": [0, FACING_VARIABLE, 0]}},
            }
        },
    }


# -- 输出 ------------------------------------------------------------------


def outputs(definitions, probe=False):
    """`[(路径, 字节串)]`：本脚本负责的全部文件。"""
    modules = load_modules(definitions)
    atlas = build_atlas(modules)
    atlas_size = (len(atlas[0]), len(atlas))
    files = [(atlas_file(), png_bytes(atlas))]
    for device_id, (kind, _entity) in sorted(DISPLAY_DEVICES.items()):
        files.append((geometry_file(device_id),
                      plural_dumps(build_geometry(device_id, kind, modules, atlas_size)).encode("utf-8")))
        files.append((entity_file(device_id),
                      plural_dumps(build_entity(device_id, kind, probe)).encode("utf-8")))
    files.append((CONTROLLER_FILE, plural_dumps(build_controllers(definitions, modules)).encode("utf-8")))
    files.append((ANIMATION_FILE, plural_dumps(build_animation()).encode("utf-8")))
    return files


def same_content(path, payload):
    """磁盘上的文件是否已是这份内容。文本文件忽略换行差异（Windows 检出会转成 CRLF）。"""
    if not os.path.isfile(path):
        return False
    with open(path, "rb") as handle:
        existing = handle.read()
    if path.endswith(".png"):
        return existing == payload
    return existing.replace(b"\r\n", b"\n") == payload.replace(b"\r\n", b"\n")


def write_if_changed(path, payload, check_only):
    if same_content(path, payload):
        return False
    if not check_only:
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, "wb") as handle:
            handle.write(payload)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="组装槽位模块的方块实体外观")
    parser.add_argument("--check", action="store_true", help="只检查是否需要重新生成")
    parser.add_argument("--probe", action="store_true", help="真机排查：每个槽都先显示默认模块")
    use_utf8_stdout()
    args = parser.parse_args(argv)
    definitions = DefinitionSet()
    try:
        files = outputs(definitions, probe=args.probe)
    except SlotModelError as error:
        print("模块源有问题：%s" % error)
        return 1
    changed = [
        rel(path)
        for path, payload in files
        if write_if_changed(path, payload, args.check)
    ]
    if args.check:
        if changed:
            print("以下文件需要重新生成（共 %d 个）：" % len(changed))
            for path in changed:
                print("  " + path)
            return 1
        print("槽位模块外观已是最新")
        return 0
    if args.probe:
        print("【探针版】每个槽都会显示默认模块；排查完请不带参数再运行一次")
    print("已生成槽位模块外观，更新 %d 个文件" % len(changed))
    for path in changed:
        print("  " + path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
