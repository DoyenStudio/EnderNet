# coding=utf-8
"""三种光缆方块的生成物（`scripts/gen_blocks.py` 的 `build_cable_json`，阶段 C）。

查产物文件本身：方块状态与取值、排列数 729 / 64、每个排列的碰撞盒、骨骼显示条件与几何体
里的骨骼对得上、资源包的方块实体外观绑定、贴图与几何体在不在；另查几何体里的部件盒
就是 `common/cable_shapes` 的那几个（生成器、碰撞盒、精确交互三处同一份）。
碰撞盒的期望值按 12 §1 的像素表**独立推出**，不调被测的 `unit_hull`。

2026-11-10 光缆美术重做（`.claude/cable-art/README.md`）后另查：玻璃整根 `blend` 加两个材质
实例；几何体逐个排列「表面不缺面、没有悬空面、没有同向共面重叠的面」
（`TestGeometryCoverage`，出样脚本自检的纯 Python 版，读的是产物）。

同日真机：玻璃光缆 73 条 `bone_visibility` 时整块加载失败（`too many input elements for an
associative container`），合并到 28 根骨骼（README §3.6）。`TestGeometryComponent` 钉住每个方块
`bone_visibility` 不超过 32 条、`minecraft:geometry` 只写合法字段。

面板底座（`build_part_holder_json`，2026-11-10）另成一组（`PartHolderFixture`）：每面 0 / 2、
64 个排列、碰撞盒是面板框与背板的外包盒（独立推出，再与 `unit_hull(None, sides)` 对一遍）、
借来的普通光缆几何体骨骼全部隐藏、绑定面板外观实体、不进创造栏、空战利品表、没有说明。
"""

import io
import itertools
import json
import os
import re
import struct
import sys
import unittest

import testsupport

from endernet.common.cable_shapes import (
    CORE_INSET,
    CORE_PX,
    HOLDER_SIDE_VALUES,
    PANEL_ENTITY_ID,
    PLATE_DEPTH,
    PLATE_INSET,
    along,
    arm_box,
    bone_visibility,
    cable_bones,
    core_box,
    hull,
    stub_box,
    stub_length,
    unit_hull,
)
from endernet.common.definitions import PART_HOLDER_ID, DefinitionSet
from endernet.common.identity import FACING_NAMES, FACING_OFFSETS
from test_cable_shapes import STATE_QUERY, compile_visibility, molang_python

REPO = testsupport.REPO_ROOT
BP_BLOCKS = os.path.join(REPO, "endernet_bp", "blocks")
RP_ROOT = os.path.join(REPO, "endernet_rp")
SCRIPTS = os.path.join(REPO, "scripts")

GLASS = "endernet:ender_glass_cable"
CABLE = "endernet:ender_cable"
DENSE = "endernet:ender_dense_cable"

CONDITION_TERM = re.compile(r"q\.block_state\('endernet:side_(\w+)'\) == (\d)")

#: 轴 → (负方向的面, 正方向的面)；面 → 轴；直通骨骼名（按轴）。独立写出
AXIS_PAIRS = (("west", "east"), ("down", "up"), ("north", "south"))
AXIS_OF = dict((face, axis) for axis, pair in enumerate(AXIS_PAIRS) for face in pair)
STRAIGHT_NAMES = ("straight_ew", "straight_ud", "straight_ns")


def read_json(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def world_box_of(cube):
    """几何文件里的 cube → 世界像素盒（相对方块底面中心；文件 x 与世界 x 相反）。"""
    (ox, oy, oz), (sx, sy, sz) = cube["origin"], cube["size"]
    return (-(ox + sx), oy, oz), (-ox, oy + sy, oz + sz)


def scripts_module(name):
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    return __import__(name)


def short(device_id):
    return device_id.split(":", 1)[1]


def expected_hull(core, sides):
    """碰撞盒的独立推导（像素）：线芯 core 居中；状态 1 的面伸到方块边、截面同线芯；
    状态 2 的面伸到方块边、截面 12（面板框）。每面互不影响，取并集。"""
    half = core / 2.0
    low = [8 - half] * 3
    high = [8 + half] * 3
    axis_of = {"west": 0, "east": 0, "down": 1, "up": 1, "north": 2, "south": 2}
    positive = {"east", "up", "south"}
    for face, value in sides.items():
        if value == 0:
            continue
        axis = axis_of[face]
        if face in positive:
            high[axis] = 16
        else:
            low[axis] = 0
        if value == 2:
            for other in range(3):
                if other != axis:
                    low[other] = min(low[other], 2)
                    high[other] = max(high[other], 14)
    return [value / 16.0 for value in low], [value / 16.0 for value in high]


def expected_holder_hull(sides):
    """面板底座碰撞盒的独立推导（像素）：没有线芯；状态 2 的面从方块边向里 3（面板框 2 +
    背板 1）、截面 12（面板框 2～14，背板 8 在框内）；一块面板都没有时取居中 4 像素小盒。"""
    axis_of = {"west": 0, "east": 0, "down": 1, "up": 1, "north": 2, "south": 2}
    positive = {"east", "up", "south"}
    boxes = []
    for face, value in sides.items():
        if value != 2:
            continue
        axis = axis_of[face]
        low = [2, 2, 2]
        high = [14, 14, 14]
        if face in positive:
            low[axis], high[axis] = 13, 16
        else:
            low[axis], high[axis] = 0, 3
        boxes.append((low, high))
    if not boxes:
        boxes = [([6, 6, 6], [10, 10, 10])]
    low = [min(box[0][axis] for box in boxes) for axis in range(3)]
    high = [max(box[1][axis] for box in boxes) for axis in range(3)]
    return [value / 16.0 for value in low], [value / 16.0 for value in high]


class CableBlockFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definitions = DefinitionSet()
        cls.blocks = {}
        for device_id in cls.definitions.cables:
            path = os.path.join(BP_BLOCKS, device_id.replace(":", "_") + ".json")
            cls.blocks[device_id] = read_json(path)["minecraft:block"]

    def accepts(self, device_id):
        return self.definitions.accepts_parts(device_id)


class TestStates(CableBlockFixture):
    def test_three_cables_are_generated(self):
        self.assertEqual(sorted(self.blocks), sorted([GLASS, CABLE, DENSE]))

    def test_one_integer_state_per_face(self):
        for device_id, block in self.blocks.items():
            states = block["description"]["states"]
            self.assertEqual(
                sorted(states), sorted("endernet:side_" + face for face in FACING_NAMES), device_id
            )
            expected = [0, 1, 2] if self.accepts(device_id) else [0, 1]
            for name, values in states.items():
                self.assertEqual(values, expected, (device_id, name))

    def test_dense_cannot_hold_panels(self):
        self.assertTrue(self.accepts(GLASS))
        self.assertTrue(self.accepts(CABLE))
        self.assertFalse(self.accepts(DENSE))


class TestPermutations(CableBlockFixture):
    def parse(self, condition):
        terms = CONDITION_TERM.findall(condition)
        self.assertEqual(len(terms), 6, condition)
        self.assertEqual(condition.count("&&"), 5, condition)
        return dict((face, int(value)) for face, value in terms)

    def test_permutation_counts(self):
        self.assertEqual(len(self.blocks[GLASS]["permutations"]), 729)
        self.assertEqual(len(self.blocks[CABLE]["permutations"]), 729)
        self.assertEqual(len(self.blocks[DENSE]["permutations"]), 64)

    def test_every_combination_appears_exactly_once(self):
        for device_id, block in self.blocks.items():
            values = (0, 1, 2) if self.accepts(device_id) else (0, 1)
            seen = [
                tuple(self.parse(permutation["condition"])[face] for face in FACING_NAMES)
                for permutation in block["permutations"]
            ]
            self.assertEqual(sorted(seen), sorted(itertools.product(values, repeat=6)), device_id)

    def test_each_permutation_only_sets_the_collision_box(self):
        for device_id, block in self.blocks.items():
            for permutation in block["permutations"]:
                self.assertEqual(list(permutation["components"]), ["netease:aabb"], device_id)

    def test_collision_equals_clip_and_matches_the_parts(self):
        for device_id, block in self.blocks.items():
            core = CORE_PX[device_id]
            for permutation in block["permutations"]:
                sides = self.parse(permutation["condition"])
                aabb = permutation["components"]["netease:aabb"]
                self.assertEqual(aabb["collision"], aabb["clip"], device_id)
                low, high = expected_hull(core, sides)
                self.assertEqual(aabb["collision"]["min"], low, (device_id, sides))
                self.assertEqual(aabb["collision"]["max"], high, (device_id, sides))

    def test_default_box_is_the_core(self):
        for device_id, block in self.blocks.items():
            low, high = expected_hull(CORE_PX[device_id], {})
            aabb = block["components"]["netease:aabb"]
            self.assertEqual(aabb["collision"], {"min": low, "max": high}, device_id)
            self.assertEqual(aabb["clip"], aabb["collision"], device_id)

    def test_known_boxes_from_the_modeling_note(self):
        """12 §2 的表：普通光缆线芯 0.3125～0.6875，东臂到 x = 1；北面板框 x、y 0.125～0.875。"""
        perms = dict(
            (tuple(sorted(self.parse(p["condition"]).items())), p["components"]["netease:aabb"])
            for p in self.blocks[CABLE]["permutations"]
        )
        empty = tuple(sorted(dict.fromkeys(FACING_NAMES, 0).items()))
        self.assertEqual(perms[empty]["collision"], {"min": [0.3125] * 3, "max": [0.6875] * 3})
        east = dict.fromkeys(FACING_NAMES, 0)
        east["east"] = 1
        box = perms[tuple(sorted(east.items()))]["collision"]
        self.assertEqual(box, {"min": [0.3125] * 3, "max": [1.0, 0.6875, 0.6875]})
        north = dict.fromkeys(FACING_NAMES, 0)
        north["north"] = 2
        box = perms[tuple(sorted(north.items()))]["collision"]
        self.assertEqual(box, {"min": [0.125, 0.125, 0.0], "max": [0.875, 0.875, 0.6875]})


class TestComponents(CableBlockFixture):
    def test_block_entity_remove_listener_and_neighbor_events(self):
        for device_id, block in self.blocks.items():
            components = block["components"]
            self.assertEqual(components["netease:block_entity"], {"tick": False}, device_id)
            self.assertEqual(components["netease:listen_block_remove"], {"value": True})
            self.assertEqual(components["netease:neighborchanged_sendto_script"], {"value": True})

    def test_no_facing_and_no_container(self):
        for device_id, block in self.blocks.items():
            self.assertNotIn("traits", block["description"], device_id)
            self.assertNotIn("netease:block_container", block["components"], device_id)
            self.assertNotIn("minecraft:loot", block["components"], "掉落是方块本身")

    def test_material_and_light(self):
        """普通、致密 `opaque`；玻璃整根 `blend`（一个方块的材质实例只能同一种渲染方式），
        几何体里逐面写的 `glass`（填充）/ `fiber`（芯）另配，三个实例同一张模型贴图。"""
        for device_id, block in self.blocks.items():
            components = block["components"]
            texture = "endernet:%s_model" % short(device_id)
            self.assertEqual(components["minecraft:light_dampening"], 0, device_id)
            if device_id != GLASS:
                self.assertEqual(components["minecraft:material_instances"],
                                 {"*": {"render_method": "opaque", "texture": texture}}, device_id)
                continue
            self.assertEqual(components["minecraft:material_instances"], {
                "*": {"render_method": "blend", "texture": texture},
                "glass": {"render_method": "blend", "texture": texture, "ambient_occlusion": False},
                "fiber": {"render_method": "blend", "texture": texture, "face_dimming": False,
                          "ambient_occlusion": False},
            })

    def test_geometry_material_instances_are_declared(self):
        """几何体逐面写的 `material_instance` 都在方块的 `material_instances` 里；只有玻璃写。"""
        for device_id, block in self.blocks.items():
            path = os.path.join(RP_ROOT, "models", "blocks", short(device_id) + ".geo.json")
            used = set()
            for bone in read_json(path)["minecraft:geometry"][0]["bones"]:
                for cube in bone["cubes"]:
                    used.update(spec["material_instance"] for spec in cube["uv"].values()
                                if "material_instance" in spec)
            declared = set(block["components"]["minecraft:material_instances"])
            self.assertTrue(used <= declared, (device_id, used))
            self.assertEqual(used, {"glass", "fiber"} if device_id == GLASS else set(), device_id)

    def test_tip_quotes_the_bandwidth(self):
        units = {GLASS: "8u", CABLE: "16u", DENSE: "32u"}
        for device_id, block in self.blocks.items():
            self.assertIn(units[device_id], block["components"]["netease:custom_tips"]["value"])


class TestGeometryAndVisibility(CableBlockFixture):
    def geometry(self, device_id):
        path = os.path.join(RP_ROOT, "models", "blocks", short(device_id) + ".geo.json")
        self.assertTrue(os.path.isfile(path), path)
        return read_json(path)

    def test_block_points_at_its_geometry(self):
        for device_id, block in self.blocks.items():
            identifier = block["components"]["minecraft:geometry"]["identifier"]
            self.assertEqual(identifier, "geometry.endernet." + short(device_id))
            payload = self.geometry(device_id)
            self.assertIn(payload["format_version"], ("1.12.0", "1.21.0", "1.21.70"))
            names = [g["description"]["identifier"] for g in payload["minecraft:geometry"]]
            self.assertEqual(names, [identifier])

    def test_bone_visibility_names_existing_bones(self):
        """每根骨骼都有一条条件（没有常显的骨骼）；骨骼与顺序就是 `cable_shapes.cable_bones`，
        条件就是 `cable_shapes.bone_visibility`，只读 `endernet:side_<面>`、取值在状态范围内。"""
        for device_id, block in self.blocks.items():
            accepts = self.accepts(device_id)
            visibility = block["components"]["minecraft:geometry"]["bone_visibility"]
            bones = [bone["name"] for bone in self.geometry(device_id)["minecraft:geometry"][0]["bones"]]
            self.assertEqual(bones, [bone.name for bone in cable_bones(device_id, accepts)], device_id)
            self.assertEqual(sorted(visibility), sorted(bones), device_id)
            self.assertEqual(visibility, bone_visibility(device_id, accepts), device_id)
            values = [0, 1, 2] if accepts else [0, 1]
            for bone, expression in visibility.items():
                molang_python(expression)  # 有 side_<面> 以外的查询就报错
                self.assertTrue(set(STATE_QUERY.findall(expression)) <= set(FACING_NAMES), bone)
                for value in re.findall(r"[=!]= (\d+)", expression):
                    self.assertIn(int(value), values, (device_id, bone))

    def test_model_texture_matches_the_geometry(self):
        for device_id in self.blocks:
            description = self.geometry(device_id)["minecraft:geometry"][0]["description"]
            path = os.path.join(RP_ROOT, "textures", "endernet", "models", short(device_id) + ".png")
            self.assertTrue(os.path.isfile(path), path)
            with open(path, "rb") as handle:
                size = struct.unpack(">II", handle.read(24)[16:24])
            self.assertEqual(size, (description["texture_width"], description["texture_height"]))

    def shell_hulls(self, device_id):
        """`{骨骼: 外包盒}`（世界像素坐标，相对方块底面中心）。玻璃的芯（每个面都是 `fiber`
        材质的方块）不算：它从交汇点伸出，另由 `test_glass_fiber_runs_from_the_node` 核对。"""
        out = {}
        for bone in self.geometry(device_id)["minecraft:geometry"][0]["bones"]:
            boxes = [world_box_of(cube) for cube in bone["cubes"]
                     if not all(spec.get("material_instance") == "fiber"
                                for spec in cube["uv"].values())]
            if boxes:
                out[bone["name"]] = hull(boxes)
        return out

    def test_geometry_cubes_are_the_shared_part_boxes(self):
        """几何体的部件就是 `cable_shapes` 的盒（文件 x 与世界相反）：直通 = 两条臂 + 线芯贯穿
        整格；连接臂 = `arm_box`；短接线 = `stub_box`；空面 = 线芯面 + 凸起 `PLATE_DEPTH` 的端口
        盖板（不透明）/ 线芯面中间留 1 px 边的封面（玻璃）。玻璃按骨骼的外包盒核对（实心与填充
        同在一根骨骼里）：`hub` 是线芯；连接臂的填充面往线芯里多铺 1 px（样稿的填充条并进来），
        外包盒从离中心 `half - 1` 起；玻璃没有短接线骨骼。

        致密光缆的线芯外观缩进 `CORE_INSET`（作者 2026-11-10 在 Blockbench 里改）：空面随线芯面
        往里挪；连接臂从缩进后的线芯面起（多一圈过渡），截面仍按 `CORE_PX`。"""
        offsets = dict(zip(FACING_NAMES, FACING_OFFSETS))
        for device_id in self.blocks:
            core = CORE_PX[device_id]
            half = core / 2.0
            glass = device_id == GLASS
            hulls = self.shell_hulls(device_id)
            expected = {}
            for axis, (neg, pos) in enumerate(AXIS_PAIRS):
                expected[STRAIGHT_NAMES[axis]] = hull(
                    [arm_box(core, offsets[neg]), core_box(core), arm_box(core, offsets[pos])])
            for face in FACING_NAMES:
                vector = offsets[face]
                if glass:
                    expected["arm_" + face] = along(vector, half - 1, 8, half)
                    expected["cap_" + face] = along(vector, half, half, half - 1)
                    continue
                inner = half - CORE_INSET.get(device_id, 0)
                expected["arm_" + face] = (
                    arm_box(core, vector) if inner == half else along(vector, inner, 8, half))
                if self.accepts(device_id):
                    expected["stub_" + face] = stub_box(core, vector)
                expected["cap_" + face] = along(vector, inner, inner + PLATE_DEPTH, inner)
            if glass:
                expected["hub"] = core_box(core)
            for name, box in sorted(expected.items()):
                self.assertEqual(hulls[name], box, (device_id, name))
            # 棱（玻璃线芯的 12 根 `edge_*`）是线芯里的细条，不是部件盒，不在这张表里
            self.assertEqual(sorted(name for name in hulls if not name.startswith("edge_")),
                             sorted(expected), device_id)

    def test_stub_lengths_are_unchanged(self):
        """短接线从线芯面伸到面板背板：玻璃 3、普通 2（与面板、碰撞盒同一份尺寸）。

        玻璃几何体里没有短接线骨骼（2026-11-10 为压骨骼数并进连接臂，画到方块面，多出的 3 px
        藏在面板实体的背板与面板框里），但碰撞盒与精确点击用的 `stub_box` 仍是 3。"""
        hulls = self.shell_hulls(CABLE)
        for face in FACING_NAMES:
            low, high = hulls["stub_" + face]
            axis = AXIS_OF[face]
            self.assertEqual(high[axis] - low[axis], 2, face)
        self.assertEqual(stub_length(CORE_PX[GLASS]), 3)
        offsets = dict(zip(FACING_NAMES, FACING_OFFSETS))
        for face in FACING_NAMES:
            low, high = stub_box(CORE_PX[GLASS], offsets[face])
            self.assertEqual(high[AXIS_OF[face]] - low[AXIS_OF[face]], 3, face)
        self.assertFalse([name for name in self.shell_hulls(GLASS) if name.startswith("stub_")])

    def test_port_plate_cubes(self):
        """不透明光缆的 `cap_<面>`：零厚度的线芯面 + 每边缩进 `PLATE_INSET`、厚 `PLATE_DEPTH`
        的盖板（普通 4×4、致密 6×6）。致密的线芯外观缩进 1 px（8×8），两块一起往里挪。"""
        offsets = dict(zip(FACING_NAMES, FACING_OFFSETS))
        for device_id, plate_px, face_px in ((CABLE, 4, 6), (DENSE, 6, 8)):
            core = CORE_PX[device_id]
            inner = face_px / 2.0
            self.assertEqual(core - 2 * PLATE_INSET[device_id], plate_px)
            self.assertEqual(core - 2 * CORE_INSET.get(device_id, 0), face_px)
            bones = dict((bone["name"], bone["cubes"])
                         for bone in self.geometry(device_id)["minecraft:geometry"][0]["bones"])
            for face in FACING_NAMES:
                vector = offsets[face]
                core_face, plate = [world_box_of(cube) for cube in bones["cap_" + face]]
                self.assertEqual(core_face, along(vector, inner, inner, inner), (device_id, face))
                self.assertEqual(plate, along(vector, inner, inner + PLATE_DEPTH, plate_px / 2.0),
                                 (device_id, face))

    def test_dense_arms_close_the_inset_core(self):
        """致密的连接臂：10 px 截面的臂（内端面露出的一圈画线芯空面）+ 线芯面与臂之间 8×8×1 的
        过渡（作者 2026-11-10）；过渡朝线芯那面在线芯里面，看不见。"""
        offsets = dict(zip(FACING_NAMES, FACING_OFFSETS))
        bones = dict((bone["name"], bone["cubes"])
                     for bone in self.geometry(DENSE)["minecraft:geometry"][0]["bones"])
        for face in FACING_NAMES:
            vector = offsets[face]
            arm, collar = bones["arm_" + face]
            self.assertEqual(world_box_of(arm), arm_box(10, vector), face)
            self.assertEqual(world_box_of(collar), along(vector, 4, 5, 4), face)
            self.assertEqual(len(arm["uv"]), 6, "四个侧面 + 外端截面 + 内端面")
            self.assertEqual(len(collar["uv"]), 5, "四个侧面 + 朝线芯那面")

    def test_glass_fiber_runs_from_the_node(self):
        """玻璃的芯是正中 1×1：交汇点在 `hub`，每条臂 / 短接线的芯从交汇点伸到外端，直通的芯贯穿整格。"""
        bones = dict((bone["name"], bone["cubes"])
                     for bone in self.geometry(GLASS)["minecraft:geometry"][0]["bones"])
        offsets = dict(zip(FACING_NAMES, FACING_OFFSETS))

        def fibers(name):
            return [world_box_of(cube) for cube in bones[name]
                    if all(spec.get("material_instance") == "fiber" for spec in cube["uv"].values())]

        self.assertEqual(fibers("hub"), [core_box(1)])
        for axis, (neg, pos) in enumerate(AXIS_PAIRS):
            self.assertEqual(fibers(STRAIGHT_NAMES[axis]), [
                hull([arm_box(1, offsets[neg]), arm_box(1, offsets[pos])])])
        for face in FACING_NAMES:
            self.assertEqual(fibers("arm_" + face), [arm_box(1, offsets[face])])
            self.assertNotIn("stub_" + face, bones, "玻璃的短接线并进了连接臂")

    def test_glass_bones_draw_solid_before_translucent(self):
        """玻璃整根 blend：每根骨骼里先画实心（棱条、芯、框角），后画半透明的填充面与封面。"""
        for bone in self.geometry(GLASS)["minecraft:geometry"][0]["bones"]:
            translucent = [all(spec.get("material_instance") == "glass" for spec in cube["uv"].values())
                           for cube in bone["cubes"]]
            first = translucent.index(True) if True in translucent else len(translucent)
            self.assertTrue(all(translucent[first:]), bone["name"])


#: 面 → (轴, 正负)，独立写出
FACE_AXIS = dict((face, (axis, -1 if index == 0 else 1))
                 for axis, pair in enumerate(AXIS_PAIRS) for index, face in enumerate(pair))
#: 短接线外端离方块中心多远（px）：8 - 面板框 2 - 背板 1
STUB_END_PX = 5
#: 端口盖板 (每边比线芯面缩进, 凸出线芯面)，px；玻璃没有
PLATE_PX = {CABLE: (1, 1), DENSE: (2, 1)}
#: 线芯外观每边比线芯缩进几 px（作者 2026-11-10 改的致密光缆：线芯画 8 px，臂仍 10 px 截面，
#: 中间一圈 1 px 的过渡）；碰撞盒与精确点击不变
CORE_INSET_PX = {DENSE: 1}


def box_along(axis, near, far, half):
    """以方块中心为原点的盒：沿 axis 从 near 到 far，另两轴 ±half。"""
    lo, hi = [-half] * 3, [half] * 3
    lo[axis], hi[axis] = min(near, far), max(near, far)
    return tuple(lo), tuple(hi)


def expected_volume(device_id, sides):
    """这一排列下光缆实体占的体积（以方块中心为原点的盒）与允许的开口，**独立推出**：

    - 直通（恰好一对对面外连、其余为空）：贯穿整格的一根；
    - 否则线芯 + 外连的臂（到方块面）+ 面板的短接线（到背板）+ 不透明光缆空面上的盖板；
    - 玻璃挂面板的面没有短接线，由连接臂画到方块面（2026-11-10 为压骨骼数合并）：多出的
      3 px 在面板实体的背板与面板框里面，方块几何体自己看就是一条臂；
    - 线芯外观缩进（致密）：线芯小一圈，外连的臂与线芯之间是一圈同线芯外观截面的过渡，盖板
      贴在缩进后的线芯面上；
    - 开口 `(轴, 正负, 平面, 截面半宽)`：玻璃臂 / 直通的外端（总被邻居或面板盖住，不封口）、
      短接线外端（被面板背板盖住）。
    """
    half = CORE_PX[device_id] / 2.0
    inner = half - CORE_INSET_PX.get(device_id, 0)
    glass = device_id == GLASS
    boxes, openings = [], []
    for axis, pair in enumerate(AXIS_PAIRS):
        if all(sides[face] == (1 if face in pair else 0) for face in FACING_NAMES):
            boxes.append(box_along(axis, -8, 8, half))
            if glass:
                openings += [(axis, -1, -8, half), (axis, 1, 8, half)]
            return boxes, openings
    boxes.append(box_along(0, -inner, inner, inner))
    for face in FACING_NAMES:
        axis, sign = FACE_AXIS[face]
        if sides[face] == 1 or (glass and sides[face] == 2):
            boxes.append(box_along(axis, sign * half, sign * 8, half))
            if inner < half:
                boxes.append(box_along(axis, sign * inner, sign * half, inner))
            if glass:
                openings.append((axis, sign, sign * 8, half))
        elif sides[face] == 2:
            boxes.append(box_along(axis, sign * half, sign * STUB_END_PX, half))
            openings.append((axis, sign, sign * STUB_END_PX, half))
        elif not glass:
            inset, depth = PLATE_PX[device_id]
            boxes.append(box_along(axis, sign * inner, sign * (inner + depth), half - inset))
    return boxes, openings


def geometry_quads(payload):
    """几何文件 → `{骨骼: [(轴, 正负, 平面, ((lo, hi), (lo, hi)))]}`：每个画出来的面朝哪、
    在哪个平面、占另两轴的哪一段（以方块中心为原点，px）。"""
    out = {}
    for bone in payload["minecraft:geometry"][0]["bones"]:
        quads = out.setdefault(bone["name"], [])
        for cube in bone["cubes"]:
            (x0, y0, z0), (x1, y1, z1) = world_box_of(cube)
            lo, hi = (x0, y0 - 8, z0), (x1, y1 - 8, z1)
            for face in cube["uv"]:
                axis, sign = FACE_AXIS[face]
                rect = tuple((lo[other], hi[other]) for other in range(3) if other != axis)
                quads.append((axis, sign, hi[axis] if sign > 0 else lo[axis], rect))
    return out


class CellGrid(object):
    """按全部坐标切出的网格。格子是 `(i, j, k)`；一个网格面是 `(背后的格, 前面的格)`——朝向
    就是从背后指向前面。体积与面都落在格线上，逐格比对就是精确的（不靠随机射线）。"""

    def __init__(self, coordinates):
        self.index = [dict((value, i) for i, value in enumerate(sorted(set(values))))
                      for values in coordinates]
        self._cells = {}
        self._skin = {}

    def span(self, axis, lo, hi):
        return range(self.index[axis][lo], self.index[axis][hi])

    def cells(self, box):
        if box not in self._cells:
            lo, hi = box
            self._cells[box] = frozenset(itertools.product(
                *[self.span(axis, lo[axis], hi[axis]) for axis in range(3)]))
        return self._cells[box]

    def skin(self, box):
        """盒的表面：盒内格子朝盒外的网格面。"""
        if box not in self._skin:
            cells = self.cells(box)
            faces = set()
            for cell in cells:
                for axis in range(3):
                    for sign in (-1, 1):
                        front = list(cell)
                        front[axis] += sign
                        front = tuple(front)
                        if front not in cells:
                            faces.add((cell, front))
            self._skin[box] = frozenset(faces)
        return self._skin[box]

    def faces(self, axis, sign, plane, rect):
        """朝 sign 的一个面 → 它盖住的网格面（列表，保留重复，用来查共面重叠）。"""
        at = self.index[axis][plane]
        back = at - 1 if sign > 0 else at
        others = [other for other in range(3) if other != axis]
        out = []
        for p in self.span(others[0], *rect[0]):
            for q in self.span(others[1], *rect[1]):
                cell = [0, 0, 0]
                cell[axis], cell[others[0]], cell[others[1]] = back, p, q
                front = list(cell)
                front[axis] += sign
                out.append((tuple(cell), tuple(front)))
        return out


class TestGeometryCoverage(CableBlockFixture):
    """产物几何体逐个排列（729 / 729 / 64）的表面检查——出样脚本 `gen_cable_art.py` 自检的
    纯 Python 版（原版用 numpy 打随机射线；这里改成按格线切网格逐格比对，结果是精确的）。
    读的是产物：资源包的几何体 + 行为包方块 JSON 的 `bone_visibility`。

    1. 不缺面：光缆实体（`expected_volume`，独立推出）表面的每一处都有一个朝外的面盖着，
       允许的开口只有玻璃臂 / 直通的外端与短接线外端；
    2. 没有悬空的面：面的前后都在实体外；
    3. 不闪烁：同一排列里没有同向、共面、重叠的两个面。
    """

    @classmethod
    def setUpClass(cls):
        CableBlockFixture.setUpClass()
        cls.models = {}
        for device_id, block in cls.blocks.items():
            path = os.path.join(RP_ROOT, "models", "blocks", short(device_id) + ".geo.json")
            cls.models[device_id] = (
                geometry_quads(read_json(path)),
                compile_visibility(block["components"]["minecraft:geometry"]["bone_visibility"]),
            )

    def combinations(self, device_id):
        values = (0, 1, 2) if self.accepts(device_id) else (0, 1)
        return [dict(zip(FACING_NAMES, combo)) for combo in itertools.product(values, repeat=6)]

    def survey(self, device_id, quads, visible, combinations):
        """逐个排列检查，返回 `[(排列, 缺面数, 悬空面数, 共面重叠数)]`（只列有问题的）。"""
        volumes = [expected_volume(device_id, sides) for sides in combinations]
        coordinates = ([], [], [])
        for boxes, openings in volumes:
            for lo, hi in boxes:
                for axis in range(3):
                    coordinates[axis].extend((lo[axis], hi[axis]))
            for axis, _sign, plane, half in openings:
                coordinates[axis].append(plane)
                for other in range(3):
                    if other != axis:
                        coordinates[other].extend((-half, half))
        for bone_quads in quads.values():
            for axis, _sign, plane, rect in bone_quads:
                coordinates[axis].append(plane)
                others = [other for other in range(3) if other != axis]
                coordinates[others[0]].extend(rect[0])
                coordinates[others[1]].extend(rect[1])
        grid = CellGrid(coordinates)
        bone_faces = dict(
            (bone, [face for quad in bone_quads for face in grid.faces(*quad)])
            for bone, bone_quads in quads.items()
        )
        problems = []
        for sides, (boxes, openings) in zip(combinations, volumes):
            inside = frozenset().union(*[grid.cells(box) for box in boxes])
            surface = set(face for box in boxes for face in grid.skin(box) if face[1] not in inside)
            drawn = [face for bone in quads if visible[bone](sides) for face in bone_faces[bone]]
            covered = set(drawn)
            allowed = set()
            for axis, sign, plane, half in openings:
                allowed.update(grid.faces(axis, sign, plane, ((-half, half), (-half, half))))
            missing = len(surface - covered - allowed)
            floating = sum(1 for back, front in covered if back not in inside and front not in inside)
            overlapping = len(drawn) - len(covered)
            if missing or floating or overlapping:
                problems.append((sides, missing, floating, overlapping))
        return problems

    def test_every_permutation_is_closed_and_clean(self):
        for device_id in sorted(self.blocks):
            quads, visible = self.models[device_id]
            problems = self.survey(device_id, quads, visible, self.combinations(device_id))
            self.assertEqual(problems[:3], [], "%s：%d 个排列有问题（排列, 缺面, 悬空, 共面重叠）"
                             % (device_id, len(problems)))

    def test_the_survey_catches_broken_geometry(self):
        """检查本身靠得住：藏掉一块空面、让一条臂常显，都会被查出来。"""
        quads, visible = self.models[CABLE]
        combinations = self.combinations(CABLE)
        broken = dict(visible)
        broken["cap_up"] = lambda sides: False
        self.assertTrue(self.survey(CABLE, quads, broken, combinations[:40]))
        broken = dict(visible)
        broken["arm_east"] = lambda sides: True
        self.assertTrue(self.survey(CABLE, quads, broken, combinations[:40]))
        glass_quads, glass_visible = self.models[GLASS]
        end = [dict(dict.fromkeys(FACING_NAMES, 0), east=1)]
        broken = dict(glass_visible)
        broken["edge_up_north"] = lambda sides: False
        self.assertTrue(self.survey(GLASS, glass_quads, broken, end), "玻璃少一条棱也查得出")
        broken = dict(glass_visible)
        broken["cap_up"] = lambda sides: False
        self.assertTrue(self.survey(GLASS, glass_quads, broken, end), "玻璃少一块封面也查得出")
        broken = dict(glass_visible)
        broken["edge_up_east"] = lambda sides: True
        self.assertTrue(self.survey(GLASS, glass_quads, broken, end),
                        "臂铺进线芯的那 1 px 填充与棱叠在一起也查得出")


#: 一个方块 `minecraft:geometry.bone_visibility` 最多几条。引擎有上限、文档查不到确切数字：
#: 作者 2026-11-10 真机日志里 73 条的玻璃光缆整块加载失败（`bone_visibility: too many input
#: elements for an associative container`），21 条（普通光缆、面板底座）与 15 条（致密）都能
#: 加载。按 32 收（独立写出，不读 `cable_shapes.BONE_VISIBILITY_LIMIT`）。
BONE_VISIBILITY_LIMIT = 32

#: `minecraft:geometry` 对象写法的合法字段（Bedrock Wiki「Block Components → Geometry」：
#: identifier、bone_visibility、culling、culling_layer、uv_lock）
GEOMETRY_KEYS = frozenset(("identifier", "bone_visibility", "culling", "culling_layer", "uv_lock"))


def block_components(block):
    """方块的全部组件表：顶层的 + 每个排列的。"""
    yield block.get("components", {})
    for permutation in block.get("permutations", ()):
        yield permutation.get("components", {})


class TestGeometryComponent(unittest.TestCase):
    """行为包每个方块的 `minecraft:geometry`：`bone_visibility` 不超过上限、只写合法字段。

    同一份真机日志里还有一句 `minecraft:geometry: invalid string`：这个组件有字符串与对象两种
    写法，对象那条解析失败（条数超限）后引擎再按字符串解析一遍、也失败，两句是同一个原因。
    """

    @classmethod
    def setUpClass(cls):
        cls.blocks = {}
        for name in sorted(os.listdir(BP_BLOCKS)):
            if name.endswith(".json"):
                cls.blocks[name] = read_json(os.path.join(BP_BLOCKS, name))["minecraft:block"]

    def geometries(self):
        for name, block in sorted(self.blocks.items()):
            for components in block_components(block):
                if "minecraft:geometry" in components:
                    yield name, components["minecraft:geometry"]

    def test_bone_visibility_stays_under_the_engine_limit(self):
        counts = {}
        for name, geometry in self.geometries():
            if isinstance(geometry, dict) and "bone_visibility" in geometry:
                counts[name] = len(geometry["bone_visibility"])
        self.assertEqual(
            dict((name, count) for name, count in counts.items() if count > BONE_VISIBILITY_LIMIT),
            {}, "bone_visibility 超过 %d 条，方块会整块加载失败" % BONE_VISIBILITY_LIMIT)
        self.assertEqual(
            dict((name, counts.get(name)) for name in (
                "endernet_ender_glass_cable.json", "endernet_ender_cable.json",
                "endernet_ender_dense_cable.json", "endernet_part_holder.json")),
            {"endernet_ender_glass_cable.json": 28, "endernet_ender_cable.json": 21,
             "endernet_ender_dense_cable.json": 15, "endernet_part_holder.json": 21})

    def test_cable_geometries_have_at_most_the_limit_of_bones(self):
        """每根骨骼一条 `bone_visibility`，所以光缆几何体的骨骼数也不能超。"""
        for device_id in (GLASS, CABLE, DENSE):
            path = os.path.join(RP_ROOT, "models", "blocks", short(device_id) + ".geo.json")
            bones = read_json(path)["minecraft:geometry"][0]["bones"]
            self.assertLessEqual(len(bones), BONE_VISIBILITY_LIMIT, device_id)

    def test_geometry_component_fields_are_legal(self):
        for name, geometry in self.geometries():
            if not isinstance(geometry, dict):
                self.assertIsInstance(geometry, str, name)
                continue
            self.assertTrue(set(geometry) <= GEOMETRY_KEYS, (name, sorted(geometry)))
            self.assertIsInstance(geometry.get("identifier"), str, name)
            self.assertTrue(geometry["identifier"].startswith(("geometry.", "minecraft:geometry.")),
                            (name, geometry["identifier"]))
            for bone, value in geometry.get("bone_visibility", {}).items():
                self.assertTrue(re.match(r"^[A-Za-z][A-Za-z0-9_.]*$", bone), (name, bone))
                self.assertIsInstance(value, (str, bool), (name, bone))


class TestResourcePack(CableBlockFixture):
    @classmethod
    def setUpClass(cls):
        CableBlockFixture.setUpClass()
        cls.blocks_json = read_json(os.path.join(RP_ROOT, "blocks.json"))
        cls.terrain = read_json(os.path.join(RP_ROOT, "textures", "terrain_texture.json"))

    def test_panel_capable_cables_bind_the_panel_appearance(self):
        for device_id in self.blocks:
            entry = self.blocks_json[device_id]
            self.assertNotIn("textures", entry, "模型方块不写 textures")
            if self.accepts(device_id):
                self.assertEqual(entry["client_entity"], {
                    "hand_model_use_client_entity": False,
                    "identifier": PANEL_ENTITY_ID,
                })
            else:
                self.assertNotIn("client_entity", entry, device_id)

    def test_panel_entity_exists(self):
        entity = read_json(os.path.join(RP_ROOT, "entity", "ender_cable_panels.entity.json"))
        description = entity["minecraft:client_entity"]["description"]
        self.assertEqual(description["identifier"], PANEL_ENTITY_ID)
        texture = description["textures"]["default"]
        self.assertTrue(os.path.isfile(os.path.join(RP_ROOT, *(texture + ".png").split("/"))))

    def test_model_texture_is_registered(self):
        data = self.terrain["texture_data"]
        for device_id in self.blocks:
            key = "endernet:%s_model" % short(device_id)
            self.assertEqual(data[key], {"textures": "textures/endernet/models/" + short(device_id)})


class TestPanelFrameEdges(unittest.TestCase):
    """面板框侧边的条带从背面画到正面（作者 2026-11-10 重画图集——深色一行 / 一列在背面——并在
    面板物品的几何里翻过两个侧边）。实体外观与物品形态同一份贴法：物品是挪到方块中心、屏幕朝南
    的面板，实体里朝南那组的面板框与它逐面同一份 UV。"""

    def test_the_entity_frame_matches_the_items(self):
        entity = read_json(os.path.join(RP_ROOT, "models", "entity", "ender_cable_panels.geo.json"))
        bones = dict((bone["name"], bone) for bone in entity["minecraft:geometry"][0]["bones"])
        frame, back = bones["frame_south"]["cubes"]
        for kind in ("common", "crafting", "template", "factoric"):
            item = read_json(os.path.join(RP_ROOT, "models", "blocks", kind + "_terminal_part.geo.json"))
            item_frame, item_back, _screen = item["minecraft:geometry"][0]["bones"][0]["cubes"]
            self.assertEqual(item_frame["uv"], frame["uv"], kind)
            self.assertEqual(item_back["uv"], back["uv"], kind)


class PartHolderFixture(unittest.TestCase):
    """面板底座的产物（`gen_blocks.build_part_holder_json`）；对照它借外观的普通光缆的产物。"""

    #: 面板底座借这种光缆的几何体与贴图键（2026-11-10 起借普通光缆：opaque、21 根骨骼）
    LOOKS = CABLE

    @classmethod
    def setUpClass(cls):
        cls.definitions = DefinitionSet()
        cls.block = read_json(
            os.path.join(BP_BLOCKS, PART_HOLDER_ID.replace(":", "_") + ".json"))["minecraft:block"]
        cls.looks = read_json(
            os.path.join(BP_BLOCKS, cls.LOOKS.replace(":", "_") + ".json"))["minecraft:block"]

    def parse(self, condition):
        terms = CONDITION_TERM.findall(condition)
        self.assertEqual(len(terms), 6, condition)
        self.assertEqual(condition.count("&&"), 5, condition)
        return dict((face, int(value)) for face, value in terms)


class TestPartHolderStates(PartHolderFixture):
    def test_the_holder_is_generated(self):
        self.assertEqual(sorted(self.definitions.part_holders), [PART_HOLDER_ID])
        self.assertEqual(self.block["description"]["identifier"], PART_HOLDER_ID)

    def test_each_face_is_none_or_panel(self):
        """没有连接臂：每面只有 0（无）与 2（面板）。"""
        self.assertEqual(tuple(HOLDER_SIDE_VALUES), (0, 2))
        states = self.block["description"]["states"]
        self.assertEqual(sorted(states), sorted("endernet:side_" + face for face in FACING_NAMES))
        for name, values in states.items():
            self.assertEqual(values, [0, 2], name)

    def test_64_permutations_each_combination_once(self):
        permutations = self.block["permutations"]
        self.assertEqual(len(permutations), 64)
        seen = [
            tuple(self.parse(permutation["condition"])[face] for face in FACING_NAMES)
            for permutation in permutations
        ]
        self.assertEqual(sorted(seen), sorted(itertools.product((0, 2), repeat=6)))

    def test_each_permutation_only_sets_the_collision_box(self):
        for permutation in self.block["permutations"]:
            self.assertEqual(list(permutation["components"]), ["netease:aabb"])


class TestPartHolderBoxes(PartHolderFixture):
    SMALL = {"min": [0.375] * 3, "max": [0.625] * 3}

    def test_collision_is_the_hull_of_the_panels(self):
        """独立推出的期望值，再与运行时精确交互用的 `unit_hull(None, sides)` 对一遍。"""
        for permutation in self.block["permutations"]:
            sides = self.parse(permutation["condition"])
            aabb = permutation["components"]["netease:aabb"]
            self.assertEqual(aabb["collision"], aabb["clip"], sides)
            low, high = expected_holder_hull(sides)
            self.assertEqual(aabb["collision"], {"min": low, "max": high}, sides)
            shared_low, shared_high = unit_hull(None, sides)
            self.assertEqual(
                aabb["collision"], {"min": list(shared_low), "max": list(shared_high)}, sides
            )

    def test_no_panel_uses_the_small_box(self):
        """默认盒与全 0 的排列：居中小盒（底座刚放下、状态还没刷到的那一下），不是空盒。"""
        aabb = self.block["components"]["netease:aabb"]
        self.assertEqual(aabb["collision"], self.SMALL)
        self.assertEqual(aabb["clip"], self.SMALL)
        empty = [
            permutation["components"]["netease:aabb"]
            for permutation in self.block["permutations"]
            if set(self.parse(permutation["condition"]).values()) == {0}
        ]
        self.assertEqual(empty, [{"collision": self.SMALL, "clip": self.SMALL}])

    def test_known_box_of_a_single_north_panel(self):
        """北面一块面板：框 x、y 0.125～0.875，深 3 像素（框 2 + 背板 1）。"""
        north = dict.fromkeys(FACING_NAMES, 0)
        north["north"] = 2
        for permutation in self.block["permutations"]:
            if self.parse(permutation["condition"]) == north:
                self.assertEqual(
                    permutation["components"]["netease:aabb"]["collision"],
                    {"min": [0.125, 0.125, 0.0], "max": [0.875, 0.875, 0.1875]},
                )
                return
        self.fail("没有只挂北面板的排列")


class TestPartHolderComponents(PartHolderFixture):
    def test_block_entity_and_events_like_the_cables(self):
        components = self.block["components"]
        self.assertEqual(components["netease:block_entity"], {"tick": False})
        for name in ("netease:block_entity", "netease:listen_block_remove",
                     "netease:neighborchanged_sendto_script"):
            self.assertEqual(components[name], self.looks["components"][name], name)

    def test_mining_like_the_cables(self):
        for name in ("minecraft:destructible_by_mining", "netease:tier"):
            self.assertEqual(self.block["components"][name], self.looks["components"][name], name)

    def test_drops_nothing(self):
        """自身不掉落：原版行为包自带的空战利品表（内容 `{}`）；面板由脚本掉。"""
        self.assertEqual(self.block["components"]["minecraft:loot"], "loot_tables/empty.json")

    def test_stays_out_of_the_creative_tab(self):
        self.assertEqual(self.block["description"]["category"], "none")

    def test_no_tip_no_facing_no_container(self):
        components = self.block["components"]
        self.assertNotIn("netease:custom_tips", components, "没有对应物品，不写说明")
        self.assertNotIn("netease:block_container", components)
        self.assertNotIn("traits", self.block["description"])
        self.assertNotIn("base_block", self.block["description"])

    def test_material_and_light(self):
        """借来的普通光缆是 `opaque`，只写 `*`（不像玻璃那样 `blend` + 逐面材质实例）。"""
        components = self.block["components"]
        self.assertEqual(components["minecraft:light_dampening"], 0)
        self.assertEqual(
            components["minecraft:material_instances"],
            {"*": {"render_method": "opaque", "texture": "endernet:%s_model" % short(self.LOOKS)}},
        )
        self.assertEqual(components["minecraft:material_instances"],
                         self.looks["components"]["minecraft:material_instances"])


class TestPartHolderAppearance(PartHolderFixture):
    def test_borrows_the_cable_geometry_with_every_bone_hidden(self):
        """方块网格什么都不画：借来的普通光缆几何体里的**每一根**骨骼都写成布尔 false
        （直通、连接臂、短接线、空面）；清单就是 `cable_shapes.cable_bones` 的。"""
        geometry = self.block["components"]["minecraft:geometry"]
        self.assertEqual(geometry["identifier"], "geometry.endernet." + short(self.LOOKS))
        payload = read_json(os.path.join(RP_ROOT, "models", "blocks", short(self.LOOKS) + ".geo.json"))
        bones = [bone["name"] for bone in payload["minecraft:geometry"][0]["bones"]]
        self.assertEqual(len(bones), 21)
        self.assertEqual(sorted(geometry["bone_visibility"]), sorted(bones))
        self.assertEqual(bones, [bone.name for bone in cable_bones(self.LOOKS, True)])
        for name, visible in geometry["bone_visibility"].items():
            self.assertIs(visible, False, name)

    def test_binds_the_panel_appearance(self):
        """外观全靠面板的方块实体外观（同能挂面板的光缆）；模型方块不写 textures。"""
        blocks_json = read_json(os.path.join(RP_ROOT, "blocks.json"))
        self.assertEqual(blocks_json[PART_HOLDER_ID], {
            "sound": "stone",
            "client_entity": {
                "hand_model_use_client_entity": False,
                "identifier": PANEL_ENTITY_ID,
            },
        })

    def test_reuses_the_cable_atlas_entry(self):
        terrain = read_json(os.path.join(RP_ROOT, "textures", "terrain_texture.json"))
        data = terrain["texture_data"]
        key = self.block["components"]["minecraft:material_instances"]["*"]["texture"]
        self.assertEqual(data[key], {"textures": "textures/endernet/models/" + short(self.LOOKS)})
        self.assertFalse([name for name in data if "part_holder" in name], "底座不新增图集条目")


class TestGenerators(unittest.TestCase):
    """生成器与产物一致（`--check` 走的同一条路），图标只补登记了的。"""

    def test_blocks_and_items_are_up_to_date(self):
        gen_blocks = scripts_module("gen_blocks")
        gen_items = scripts_module("gen_items")
        with testsupport_quiet():
            self.assertEqual(gen_blocks.main(["--check"]), 0)
            self.assertEqual(gen_items.main(["--check"]), 0)

    def test_cable_models_are_up_to_date(self):
        gen_cable_models = scripts_module("gen_cable_models")
        with testsupport_quiet():
            self.assertEqual(gen_cable_models.main(["--check", "--icons"]), 0)

    def test_icons_follow_the_item_atlas(self):
        gen_cable_models = scripts_module("gen_cable_models")
        names = gen_cable_models.registered_icon_names()
        self.assertIn("network_wrench", names)
        for name in ("common_terminal_part", "crafting_terminal_part",
                     "template_terminal_part", "factoric_terminal_part"):
            self.assertNotIn(name, names, "面板是方块物品（展示方块），物品栏显示 3D，不登记 2D 图标")
        for cable in (GLASS, CABLE, DENSE):
            self.assertNotIn(short(cable), names, "光缆是方块物品，2D 图标不登记")

    def test_generator_cables_follow_the_definition_table(self):
        gen_cable_models = scripts_module("gen_cable_models")
        definitions = DefinitionSet()
        self.assertEqual(
            [(c.name, c.label, c.core, c.panels) for c in gen_cable_models.CABLES],
            [(short(d), definitions.get(d).label, CORE_PX[d], definitions.accepts_parts(d))
             for d in (GLASS, CABLE, DENSE)],
        )


class testsupport_quiet(object):
    """把生成器打印的「已是最新」吞掉，测试输出保持干净。"""

    def __enter__(self):
        self._stdout = sys.stdout
        sys.stdout = io.StringIO()
        return self

    def __exit__(self, *exc):
        sys.stdout = self._stdout
        return False


if __name__ == "__main__":
    unittest.main()
