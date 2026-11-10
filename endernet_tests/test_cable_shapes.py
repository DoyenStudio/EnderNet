# coding=utf-8
"""光缆部件形状与射线求交（`common/cable_shapes.py`，阶段 C）。

期望值**独立写出**（`.claude/12` §2 的世界坐标表），不从被测函数反推：
线芯玻璃 6～10 px、普通 5～11、致密 3～13；面板框 12×12×2、背板 8×8×1、短接线到离方块面 3。

骨骼与显示条件（`bone_visibility`，2026-11-10 光缆美术重做）按 molang 原文逐个排列求值：
直通时只显示直通骨骼，空面显示 `cap`，面板面显示 `stub`（玻璃由 `arm` 兼管），每面恰好一种
部件负责。玻璃接入游戏时合并到 28 根（方块加载对 `bone_visibility` 条数有上限）。
"""

import itertools
import re
import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.cable_shapes import (
    CORE_PX,
    HOLDER_SIDE_VALUES,
    PART_ARM,
    PART_CORE,
    PART_PANEL,
    CORE_INSET,
    PLATE_DEPTH,
    PLATE_INSET,
    SIDE_ARM,
    SIDE_NONE,
    SIDE_PANEL,
    arm_box,
    back_box,
    bone_visibility,
    cable_bones,
    core_box,
    core_px_of,
    enter,
    facing_against,
    frame_box,
    hull_entry,
    part_boxes,
    pick,
    side_state,
    side_values,
    sides_from_states,
    straight_axis,
    stub_box,
    stub_length,
    to_unit,
    unit_hull,
)
from endernet.common.definitions import DefinitionSet
from endernet.common.identity import FACING_NAMES, FACING_OFFSETS

GLASS = "endernet:ender_glass_cable"
CABLE = "endernet:ender_cable"
DENSE = "endernet:ender_dense_cable"

#: 面 → 世界单位偏移
OFFSET = dict(zip(FACING_NAMES, FACING_OFFSETS))


def px(value):
    """像素 → 格内坐标。"""
    return value / 16.0


def box(x0, y0, z0, x1, y1, z1):
    """格内坐标盒（按像素写，读起来与 12 §1 的表对得上）。"""
    return (px(x0), px(y0), px(z0)), (px(x1), px(y1), px(z1))


def ray_toward(point, face, distance=3.0):
    """从 `face` 那一侧离开 `distance` 格的地方，朝 `point` 射过去的射线（格内坐标）。"""
    dx, dy, dz = OFFSET[face]
    origin = (point[0] + dx * distance, point[1] + dy * distance, point[2] + dz * distance)
    return origin, (-dx, -dy, -dz)


CENTER = (0.5, 0.5, 0.5)

#: molang 里唯一允许出现的查询：`q.block_state('endernet:side_<面>')`
STATE_QUERY = re.compile(r"q\.block_state\('endernet:side_(\w+)'\)")
#: 换成 Python 之后允许剩下的东西：状态取值、比较、逻辑运算、括号、整数
_MOLANG_REST = re.compile(r"S\['\w+'\]|==|!=|&&|\|\||[!()0-9\s]")


def molang_python(expression):
    """`bone_visibility` 的 molang → Python 表达式（`S[面]` 是该面的状态值）。

    只认 `q.block_state('endernet:side_<面>')`、`==` `!=` `&&` `||` `!`、括号与整数；
    别的东西（其它查询、变量、函数）一律报错——条件只能看 `side_<面>`。
    """
    text = STATE_QUERY.sub(lambda match: "S['%s']" % match.group(1), expression)
    rest = _MOLANG_REST.sub("", text)
    if rest:
        raise ValueError("molang 里有 side_<面> 以外的东西 %r：%s" % (rest, expression))
    text = text.replace("&&", " and ").replace("||", " or ")
    return re.sub(r"!(?!=)", " not ", text).strip()


def compile_visibility(rules):
    """`{骨骼: molang}` → `{骨骼: 函数(sides) -> bool}`。"""
    compiled = {}
    for bone, expression in rules.items():
        code = compile(molang_python(expression), bone, "eval")
        compiled[bone] = (lambda code: lambda sides: bool(
            eval(code, {"__builtins__": {}}, {"S": sides})))(code)
    return compiled


class TestSizes(unittest.TestCase):
    def test_core_sizes_follow_ae2(self):
        self.assertEqual(CORE_PX, {GLASS: 4, CABLE: 6, DENSE: 10})

    def test_every_cable_in_the_definition_table_has_a_core_size(self):
        self.assertEqual(sorted(CORE_PX), sorted(DefinitionSet().cables))

    def test_core_boxes(self):
        self.assertEqual(to_unit(core_box(4)), box(6, 6, 6, 10, 10, 10))
        self.assertEqual(to_unit(core_box(6)), box(5, 5, 5, 11, 11, 11))
        self.assertEqual(to_unit(core_box(10)), box(3, 3, 3, 13, 13, 13))

    def test_arm_runs_from_the_core_face_to_the_block_face(self):
        east = OFFSET["east"]
        self.assertEqual(to_unit(arm_box(4, east)), box(10, 6, 6, 16, 10, 10))
        self.assertEqual(to_unit(arm_box(6, OFFSET["north"])), box(5, 5, 0, 11, 11, 5))
        self.assertEqual(to_unit(arm_box(10, OFFSET["down"])), box(3, 0, 3, 13, 3, 13))

    def test_stub_stops_at_the_back_plate(self):
        self.assertEqual(to_unit(stub_box(4, OFFSET["east"])), box(10, 6, 6, 13, 10, 10))
        self.assertEqual(to_unit(stub_box(6, OFFSET["east"])), box(11, 5, 5, 13, 11, 11))
        self.assertEqual(stub_length(4), 3)
        self.assertEqual(stub_length(6), 2)
        self.assertLessEqual(stub_length(10), 0, "致密线芯放不下面板")

    def test_panel_frame_and_back_plate(self):
        north = OFFSET["north"]
        self.assertEqual(to_unit(frame_box(north)), box(2, 2, 0, 14, 14, 2))
        self.assertEqual(to_unit(back_box(north)), box(4, 4, 2, 12, 12, 3))
        self.assertEqual(to_unit(frame_box(OFFSET["up"])), box(2, 14, 2, 14, 16, 14))

    def test_side_values(self):
        self.assertEqual(side_values(True), (SIDE_NONE, SIDE_ARM, SIDE_PANEL))
        self.assertEqual(side_values(False), (SIDE_NONE, SIDE_ARM))
        self.assertEqual(side_state("east"), "endernet:side_east")

    def test_sides_from_states_reads_only_known_values(self):
        states = {
            side_state("up"): 1,
            side_state("down"): 2,
            side_state("north"): 7,
            side_state("south"): True,
            "minecraft:cardinal_direction": "north",
        }
        self.assertEqual(
            sides_from_states(states),
            {"up": 1, "down": 2, "north": 0, "south": 0, "west": 0, "east": 0},
        )
        self.assertEqual(sides_from_states(None), dict.fromkeys(FACING_NAMES, 0))


class TestParts(unittest.TestCase):
    def test_only_the_core_when_every_side_is_empty(self):
        parts = part_boxes(6, {})
        self.assertEqual([(part, face) for part, face, _box in parts], [(PART_CORE, None)])

    def test_arm_and_panel_parts(self):
        parts = part_boxes(6, {"east": SIDE_ARM, "north": SIDE_PANEL})
        kinds = [(part, face) for part, face, _box in parts]
        self.assertEqual(kinds.count((PART_ARM, "east")), 1)
        # 面板 = 框 + 背板 + 短接线
        self.assertEqual(kinds.count((PART_PANEL, "north")), 3)


class TestHull(unittest.TestCase):
    def test_core_only(self):
        self.assertEqual(unit_hull(6, {}), box(5, 5, 5, 11, 11, 11))

    def test_one_arm_reaches_the_block_face(self):
        self.assertEqual(unit_hull(4, {"east": SIDE_ARM}), box(6, 6, 6, 16, 10, 10))
        self.assertEqual(unit_hull(4, {"down": SIDE_ARM}), box(6, 0, 6, 10, 10, 10))

    def test_six_arms_fill_the_block(self):
        self.assertEqual(unit_hull(10, dict.fromkeys(FACING_NAMES, SIDE_ARM)), box(0, 0, 0, 16, 16, 16))

    def test_a_panel_widens_the_box_to_the_frame(self):
        self.assertEqual(unit_hull(6, {"north": SIDE_PANEL}), box(2, 2, 0, 14, 14, 11))

    def test_every_face_of_every_combination_reaches_the_edge_exactly_when_it_is_used(self):
        """每面的外包盒是否到方块边，只看那一面有没有东西（各面互不影响）。"""
        for core in (4, 6):
            for values in itertools.product((0, 1, 2), repeat=6):
                sides = dict(zip(FACING_NAMES, values))
                low, high = unit_hull(core, sides)
                reaches = {
                    "west": low[0] == 0.0, "east": high[0] == 1.0,
                    "down": low[1] == 0.0, "up": high[1] == 1.0,
                    "north": low[2] == 0.0, "south": high[2] == 1.0,
                }
                for face in FACING_NAMES:
                    self.assertEqual(reaches[face], sides[face] != SIDE_NONE, (core, sides, face))


class TestEnter(unittest.TestCase):
    def test_entry_distance_and_face(self):
        unit = box(0, 0, 0, 16, 16, 16)
        self.assertEqual(enter(unit, (3.0, 0.5, 0.5), (-1.0, 0.0, 0.0)), (2.0, "east"))
        self.assertEqual(enter(unit, (0.5, -2.0, 0.5), (0.0, 1.0, 0.0)), (2.0, "down"))

    def test_box_behind_the_ray_is_not_hit(self):
        unit = box(0, 0, 0, 16, 16, 16)
        self.assertIsNone(enter(unit, (3.0, 0.5, 0.5), (1.0, 0.0, 0.0)))

    def test_parallel_ray_outside_the_slab_misses(self):
        unit = box(0, 0, 0, 16, 16, 16)
        self.assertIsNone(enter(unit, (3.0, 2.0, 0.5), (-1.0, 0.0, 0.0)))

    def test_origin_inside(self):
        unit = box(0, 0, 0, 16, 16, 16)
        self.assertEqual(enter(unit, CENTER, (1.0, 0.0, 0.0)), (0.0, None))

    def test_facing_against(self):
        self.assertEqual(facing_against((1.0, 0.0, 0.0)), "west")
        self.assertEqual(facing_against((0.0, -1.0, 0.2)), "up")
        self.assertEqual(facing_against((0.1, 0.2, -3.0)), "south")
        self.assertIsNone(facing_against((0.0, 0.0, 0.0)))


class TestPick(unittest.TestCase):
    def test_core_hit_from_each_side_reports_that_side(self):
        for face in FACING_NAMES:
            origin, direction = ray_toward(CENTER, face)
            hit = pick(6, {}, origin, direction)
            self.assertEqual((hit.part, hit.face), (PART_CORE, face), face)

    def test_arm_hit_along_its_axis(self):
        for face in FACING_NAMES:
            origin, direction = ray_toward(CENTER, face)
            hit = pick(4, {face: SIDE_ARM}, origin, direction)
            self.assertEqual((hit.part, hit.face), (PART_ARM, face), face)
            self.assertAlmostEqual(hit.distance, 2.5)

    def test_arm_hit_from_the_side(self):
        # 东臂（玻璃，x 10～16 px），从南边横着射到 x = 14 px 处
        target = (px(14), 0.5, 0.5)
        origin, direction = ray_toward(target, "south")
        hit = pick(4, {"east": SIDE_ARM}, origin, direction)
        self.assertEqual((hit.part, hit.face), (PART_ARM, "east"))

    def test_without_the_arm_the_same_ray_misses(self):
        target = (px(14), 0.5, 0.5)
        origin, direction = ray_toward(target, "south")
        self.assertIsNone(pick(4, {}, origin, direction))

    def test_panel_hit_on_every_face(self):
        for face in FACING_NAMES:
            origin, direction = ray_toward(CENTER, face)
            hit = pick(6, {face: SIDE_PANEL}, origin, direction)
            self.assertEqual((hit.part, hit.face), (PART_PANEL, face), face)

    def test_panel_edge_away_from_the_core(self):
        # 北面板框的一角（x 3 px、y 13 px），线芯在那里根本不在
        target = (px(3), px(13), 0.5)
        origin, direction = ray_toward(target, "north")
        hit = pick(6, {"north": SIDE_PANEL}, origin, direction)
        self.assertEqual((hit.part, hit.face), (PART_PANEL, "north"))

    def test_the_stub_belongs_to_its_panel(self):
        # 玻璃短接线（东，x 10～13 px）从上面射下去：不碰框与背板，只碰短接线
        target = (px(11.5), 0.5, 0.5)
        origin, direction = ray_toward(target, "up")
        hit = pick(4, {"east": SIDE_PANEL}, origin, direction)
        self.assertEqual((hit.part, hit.face), (PART_PANEL, "east"))

    def test_hull_corner_without_parts_is_a_miss(self):
        """L 形（北臂 + 东臂）的外包盒把东北角包了进去，但那里没有部件。"""
        sides = {"north": SIDE_ARM, "east": SIDE_ARM}
        low, high = unit_hull(6, sides)
        target = (px(14), 0.5, px(2))
        self.assertTrue(all(low[i] <= target[i] <= high[i] for i in range(3)), "点在外包盒里")
        origin, direction = ray_toward(target, "up")
        self.assertIsNone(pick(6, sides, origin, direction))

    def test_diagonal_rays_from_outside(self):
        sides = {"east": SIDE_ARM, "up": SIDE_PANEL}
        cases = [
            # 从东北上方斜着打到线芯的北面
            ((0.5 + 2.0, 0.5 + 0.0, 0.5 - 4.0), (-2.0, 0.0, 4.0), (PART_CORE, "north")),
            # 从下方斜着打到东臂
            ((px(14) - 1.0, -3.0, 0.5), (1.0, 3.5, 0.0), (PART_ARM, "east")),
            # 从西上方打到上面板
            ((-2.0, 3.0, 0.5), (2.5, -2.05, 0.0), (PART_PANEL, "up")),
        ]
        for origin, direction, expected in cases:
            hit = pick(6, sides, origin, direction)
            self.assertIsNotNone(hit, (origin, direction))
            self.assertEqual((hit.part, hit.face), expected, (origin, direction))

    def test_ray_pointing_away_misses(self):
        origin, direction = ray_toward(CENTER, "east")
        away = tuple(-value for value in direction)
        self.assertIsNone(pick(6, dict.fromkeys(FACING_NAMES, SIDE_ARM), origin, away))

    def test_zero_direction_misses(self):
        self.assertIsNone(pick(6, {}, (3.0, 0.5, 0.5), (0.0, 0.0, 0.0)))

    def test_origin_inside_the_core_faces_the_ray(self):
        hit = pick(6, {}, CENTER, (0.0, 0.0, 1.0))
        self.assertEqual((hit.part, hit.face), (PART_CORE, "north"))

    def test_nearest_part_wins(self):
        # 从东面看：东臂在前、线芯在后
        origin, direction = ray_toward(CENTER, "east")
        hit = pick(6, {"east": SIDE_ARM, "west": SIDE_ARM}, origin, direction)
        self.assertEqual((hit.part, hit.face), (PART_ARM, "east"))

    def test_seam_between_arm_and_core_prefers_the_arm(self):
        # 正好擦着东臂与线芯的交界面（x = 11 px）从上往下射
        target = (px(11), 0.5, 0.5)
        origin, direction = ray_toward(target, "up")
        hit = pick(6, {"east": SIDE_ARM}, origin, direction)
        self.assertEqual((hit.part, hit.face), (PART_ARM, "east"))

    def test_rays_do_not_need_unit_length(self):
        origin, direction = ray_toward(CENTER, "south")
        scaled = tuple(value * 7.5 for value in direction)
        self.assertEqual(pick(6, {}, origin, scaled).face, "south")


class TestPartHolderShapes(unittest.TestCase):
    """面板底座（2026-11-10）：没有线芯，只有面板框与背板；每面只取 0 / 2。"""

    HOLDER = "endernet:part_holder"

    def test_no_core_size_and_only_none_or_panel(self):
        self.assertIsNone(core_px_of(self.HOLDER))
        self.assertEqual(core_px_of(CABLE), 6)
        self.assertEqual(HOLDER_SIDE_VALUES, (SIDE_NONE, SIDE_PANEL))

    def test_a_panel_is_the_frame_and_the_back_plate(self):
        boxes = part_boxes(None, {"north": SIDE_PANEL, "east": SIDE_ARM})
        self.assertEqual([(part, face) for part, face, _box in boxes],
                         [(PART_PANEL, "north"), (PART_PANEL, "north")], "没有线芯、臂与短接线")
        self.assertEqual(to_unit(boxes[0][2]), box(2, 2, 0, 14, 14, 2))
        self.assertEqual(to_unit(boxes[1][2]), box(4, 4, 2, 12, 12, 3))

    def test_hull_is_the_panels_or_a_small_box(self):
        self.assertEqual(unit_hull(None, {"north": SIDE_PANEL}), box(2, 2, 0, 14, 14, 3))
        self.assertEqual(unit_hull(None, {"north": SIDE_PANEL, "south": SIDE_PANEL}),
                         box(2, 2, 0, 14, 14, 16))
        self.assertEqual(unit_hull(None, {}), box(6, 6, 6, 10, 10, 10), "一块都没有：居中小盒")

    def test_only_panels_can_be_picked(self):
        origin, direction = ray_toward(CENTER, "north")
        hit = pick(None, {"north": SIDE_PANEL}, origin, direction)
        self.assertEqual((hit.part, hit.face), (PART_PANEL, "north"))
        origin, direction = ray_toward(CENTER, "south")
        self.assertEqual(pick(None, {"north": SIDE_PANEL}, origin, direction).face, "north",
                         "从背面穿过空格子也点到它")
        origin, direction = ray_toward(CENTER, "up")
        self.assertIsNone(pick(None, {}, origin, direction))


class TestHullEntry(unittest.TestCase):
    """被点击的那一面（事件没带面时，按外包盒求）。"""

    def test_the_face_the_ray_enters_the_hull_through(self):
        for face in FACING_NAMES:
            origin, direction = ray_toward(CENTER, face)
            self.assertEqual(hull_entry(6, {}, origin, direction), face)

    def test_an_arm_moves_the_entry_out_to_the_block_face(self):
        # 东臂伸到方块面：从东上方射向东臂端面的中心，进入面是东
        origin = (2.0, 2.0, 0.5)
        direction = (-1.0, -1.5, 0.0)
        self.assertEqual(hull_entry(6, {"east": SIDE_ARM}, origin, direction), "east")
        self.assertEqual(hull_entry(6, {}, origin, direction), "up",
                         "没有臂时同一条射线碰不到线芯：退回迎着射线的那一面")

    def test_inside_or_missing_falls_back_to_facing_the_ray(self):
        self.assertEqual(hull_entry(6, {}, CENTER, (0.0, -1.0, 0.0)), "up")
        self.assertIsNone(hull_entry(6, {}, CENTER, (0.0, 0.0, 0.0)))


class TestPortPlate(unittest.TestCase):
    """端口盖板（不透明光缆空面上凸起的一块）只是外观：不进部件盒、碰撞盒与精确点击。"""

    def test_plate_sizes(self):
        self.assertEqual(PLATE_INSET, {CABLE: 1, DENSE: 2}, "玻璃没有盖板")
        self.assertEqual(PLATE_DEPTH, 1)
        self.assertEqual(CORE_INSET, {DENSE: 1}, "作者 2026-11-10 把致密的线芯外观缩到 8 px")

    def test_plates_are_not_parts(self):
        for device_id in (CABLE, DENSE):
            core = CORE_PX[device_id]
            self.assertEqual(part_boxes(core, {}), [(PART_CORE, None, core_box(core))])
            self.assertEqual(unit_hull(core, {}), to_unit(core_box(core)))
            # 从正上方 3 格射向中心：点到线芯顶面（y = 8 + 线芯半宽 px），不是再高 1 px 的盖板
            origin, direction = ray_toward(CENTER, "up")
            self.assertEqual(pick(core, {}, origin, direction).distance,
                             3.5 - px(8 + core / 2.0), device_id)


#: (光缆, 能否挂面板)
VISIBILITY_CASES = ((GLASS, True), (CABLE, True), (DENSE, False))
#: 轴 → (负方向的面, 正方向的面)，独立写出
AXIS_PAIRS = (("west", "east"), ("down", "up"), ("north", "south"))
STRAIGHT_NAMES = ("straight_ew", "straight_ud", "straight_ns")
#: 面 → 轴（独立写出）
AXIS_OF = {"west": 0, "east": 0, "down": 1, "up": 1, "north": 2, "south": 2}


def full_sides(**sides):
    out = dict.fromkeys(FACING_NAMES, SIDE_NONE)
    out.update(sides)
    return out


class TestBoneVisibility(unittest.TestCase):
    """`cable_shapes.bone_visibility` 的 molang 原文逐个排列求值（与产物无关；产物另在
    `test_cable_blocks` 里核对与几何体对得上）。"""

    @classmethod
    def setUpClass(cls):
        cls.compiled = dict(
            (device_id, compile_visibility(bone_visibility(device_id, accepts)))
            for device_id, accepts in VISIBILITY_CASES
        )

    def shown(self, device_id, sides):
        return set(name for name, visible in self.compiled[device_id].items() if visible(sides))

    def test_bone_lists(self):
        """玻璃 28 根（2026-11-10 从样稿的 73 根合并：73 条 `bone_visibility` 时方块整块加载
        失败）；普通 21、致密 15 不变。都不超过 32。"""
        counts = {GLASS: 28, CABLE: 21, DENSE: 15}
        for device_id, accepts in VISIBILITY_CASES:
            names = [bone.name for bone in cable_bones(device_id, accepts)]
            self.assertEqual(len(names), counts[device_id], device_id)
            self.assertLessEqual(len(names), 32, device_id)
            self.assertEqual(len(set(names)), len(names), device_id)
            self.assertEqual(sorted(names), sorted(bone_visibility(device_id, accepts)), device_id)
            self.assertNotIn("core", names, "线芯不再常显：拆成各面的 cap")
            self.assertEqual(names[:3], list(STRAIGHT_NAMES), device_id)
        glass = [bone.name for bone in cable_bones(GLASS, True)]
        expected = (list(STRAIGHT_NAMES) + ["hub"]
                    + ["edge_%s_%s" % (first, second)
                       for index, first in enumerate(FACING_NAMES)
                       for second in FACING_NAMES[index + 1:]
                       if AXIS_OF[first] != AXIS_OF[second]]
                    + ["arm_" + face for face in FACING_NAMES]
                    + ["cap_" + face for face in FACING_NAMES])
        self.assertEqual(glass, expected, "玻璃：直通、hub、棱、连接臂、空面")
        self.assertFalse([name for name in glass
                          if name.endswith("_glass") or name.startswith(("stub_", "skirt_"))],
                         "填充面并进各自的骨骼；短接线与填充条并进连接臂")

    def test_dense_has_no_stub(self):
        names = [bone.name for bone in cable_bones(DENSE, False)]
        self.assertFalse([name for name in names if name.startswith("stub_")])
        for expression in bone_visibility(DENSE, False).values():
            self.assertNotIn("== 2", expression)
            self.assertNotIn("!= 2", expression)

    def test_conditions_only_read_the_side_states(self):
        for device_id, accepts in VISIBILITY_CASES:
            values = side_values(accepts)
            for bone, expression in bone_visibility(device_id, accepts).items():
                molang_python(expression)  # 有别的查询就报错
                faces = STATE_QUERY.findall(expression)
                self.assertTrue(faces, bone)
                self.assertTrue(set(faces) <= set(FACING_NAMES), (device_id, bone))
                for value in re.findall(r"[=!]= (\d+)", expression):
                    self.assertIn(int(value), values, (device_id, bone, expression))

    def test_straight_axis(self):
        self.assertEqual(straight_axis(full_sides(west=1, east=1)), 0)
        self.assertEqual(straight_axis(full_sides(down=1, up=1)), 1)
        self.assertEqual(straight_axis({"north": 1, "south": 1}), 2, "缺的面按 0")
        self.assertIsNone(straight_axis(full_sides()))
        self.assertIsNone(straight_axis(full_sides(east=1)))
        self.assertIsNone(straight_axis(full_sides(west=1, east=1, up=1)))
        self.assertIsNone(straight_axis(full_sides(west=1, up=1)), "拐角不是直通")
        self.assertIsNone(straight_axis(full_sides(west=1, east=1, north=2)), "挂着面板要画线芯")
        self.assertIsNone(straight_axis(full_sides(west=2, east=2)))

    def test_straight_shows_only_the_straight_bone(self):
        for device_id, _accepts in VISIBILITY_CASES:
            for axis, (neg, pos) in enumerate(AXIS_PAIRS):
                expected = {STRAIGHT_NAMES[axis]}
                self.assertEqual(self.shown(device_id, full_sides(**{neg: 1, pos: 1})), expected,
                                 (device_id, axis))

    def test_empty_faces_show_their_caps(self):
        for device_id, _accepts in VISIBILITY_CASES:
            shown = self.shown(device_id, full_sides(east=1))
            self.assertIn("arm_east", shown)
            self.assertNotIn("cap_east", shown)
            for face in FACING_NAMES:
                if face != "east":
                    self.assertIn("cap_" + face, shown, (device_id, face))
                    self.assertNotIn("arm_" + face, shown, (device_id, face))
            self.assertFalse(shown & set(STRAIGHT_NAMES), device_id)
            isolated = self.shown(device_id, full_sides())
            self.assertTrue(set("cap_" + face for face in FACING_NAMES) <= isolated, device_id)

    def test_panel_faces_show_their_stubs(self):
        """普通光缆挂面板的面画 `stub`；玻璃没有短接线，由 `arm` 兼管（画到方块面，多出的那段
        藏在面板实体里）。"""
        for device_id, panel_bone in ((GLASS, "arm_north"), (CABLE, "stub_north")):
            shown = self.shown(device_id, full_sides(west=1, east=1, north=2))
            self.assertTrue({"arm_west", "arm_east", panel_bone} <= shown, device_id)
            self.assertFalse({"cap_north", "cap_west", "cap_east"} & shown, device_id)
            self.assertFalse(shown & set(STRAIGHT_NAMES), "挂着面板不是直通")
        self.assertNotIn("arm_north", self.shown(CABLE, full_sides(west=1, east=1, north=2)))
        self.assertIn("arm_north", self.shown(GLASS, full_sides(north=2)), "只挂一块面板")

    def test_glass_core_details(self):
        shown = self.shown(GLASS, full_sides(east=1))
        self.assertIn("hub", shown)
        self.assertIn("edge_up_north", shown, "上、北两面都空着：那条棱是真的")
        self.assertNotIn("edge_up_east", shown, "东面外连：上东那条棱被臂接走")
        self.assertIn("arm_east", shown, "上面靠东那条填充（样稿的 skirt）由东臂铺")
        self.assertNotIn("hub", self.shown(GLASS, full_sides(west=1, east=1)), "直通不画线芯")
        self.assertIn("edge_up_north", self.shown(GLASS, full_sides(east=2)), "挂面板的面同样接走棱")
        self.assertNotIn("edge_up_east", self.shown(GLASS, full_sides(east=2)))

    def test_every_permutation_one_part_per_face(self):
        """每个排列：直通时只有一根直通骨骼、六面都没有臂 / 短接线 / 空面；否则每面恰好一种。"""
        for device_id, accepts in VISIBILITY_CASES:
            for combo in itertools.product(side_values(accepts), repeat=6):
                sides = dict(zip(FACING_NAMES, combo))
                shown = self.shown(device_id, sides)
                straight = shown & set(STRAIGHT_NAMES)
                axis = straight_axis(sides)
                self.assertEqual(straight, set() if axis is None else {STRAIGHT_NAMES[axis]},
                                 (device_id, combo))
                for face in FACING_NAMES:
                    owners = shown & {"arm_" + face, "stub_" + face, "cap_" + face}
                    if axis is not None:
                        self.assertEqual(owners, set(), (device_id, combo, face))
                        continue
                    part = {SIDE_NONE: "cap_", SIDE_ARM: "arm_", SIDE_PANEL: "stub_"}[sides[face]]
                    if device_id == GLASS and sides[face] == SIDE_PANEL:
                        part = "arm_"  # 玻璃没有短接线，挂面板的面也是连接臂
                    self.assertEqual(owners, {part + face}, (device_id, combo, face))


if __name__ == "__main__":
    unittest.main()
