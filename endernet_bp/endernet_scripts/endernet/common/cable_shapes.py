# coding=utf-8
"""末影光缆与线缆面板的部件形状、方块状态与射线求交。**不 import 引擎**。

部件尺寸只在这里写一份（`.claude/13` §6「部件盒尺寸……生成器与运行时共用同一份」），
三处都从这里取：

- `scripts/gen_cable_models.py`：几何体里的方块（线芯、连接臂、短接线、面板框、背板、屏幕）；
- `scripts/gen_blocks.py`：方块状态 `endernet:side_<面>`、骨骼显示条件、每个排列的
  `netease:aabb` 外包盒；
- 服务端的精确交互（`server/devices/cable_intents.py`）：射线与部件盒求交，判出点中的是
  线芯、哪一面的连接臂，还是哪一面的面板；右键开界面（`server/interaction/opening.py`）也用
  同一个求交问「点中的是不是一块面板」（`server/devices/panels.py::claim_open`）。

## 两套坐标

- **像素坐标**（生成器用）：1 格 = 16 px，原点在方块**底面中心**，x、z ∈ [-8, 8]，
  y ∈ [0, 16]，方向与世界相同。几何文件的 x 与世界相反（`gen_cable_models.GEO_X`），
  那是生成器写文件时的事，这里不管。
- **格内坐标**（碰撞盒与求交用）：原点在方块**最小角**，各轴 0～1，与 `netease:aabb`
  同一口径——东 = +x、上 = +y、南 = +z。`to_unit` 把前者换成后者。

尺寸都写成「离方块中心 / 离方块面多少 px」，六个方向由同一段代码转出，顺序一律按
`identity.FACING_NAMES`（上、下、北、南、西、东）。

## 方块状态（13 §2）

每面一个整数状态 `endernet:side_<面>`：`SIDE_NONE` 0 什么都没有、`SIDE_ARM` 1 连接臂、
`SIDE_PANEL` 2 挂着面板（只有能挂面板的光缆才有这一档，致密光缆只取 0 / 1）。几何体里
哪根骨骼在哪些状态下显示见下面「骨骼」一节（`bone_visibility`）。

## 骨骼（2026-11-10 光缆美术重做，`.claude/cable-art/README.md` §3）

照 AE2 的三分法，每格画三种之一，条件只用 `endernet:side_<面>`（状态与排列数不变）：

- **直通** `straight_<轴>`（`straight_ew` / `_ud` / `_ns`）：恰好一对对面是连接臂、其余四面
  为空（`straight_axis`）时只画一根贯穿整格的管子，不画线芯；
- 否则画**线芯**：外连的面接 `arm_<面>`，挂面板的面接 `stub_<面>`，空着的面画 `cap_<面>`
  （不透明光缆 = 线芯面 + 凸起 `PLATE_DEPTH` 的端口盖板；玻璃 = 一块半透明封面）；
- 玻璃光缆另把线芯拆成 `hub`（八个框角 + 芯的交汇点）与 12 根 `edge_<面>_<面>`（相邻两面
  都空着才显示）。**接入游戏时为压到 `BONE_VISIBILITY_LIMIT` 根以内合并过**（样稿 73 根，
  方块加载报 `bone_visibility: too many input elements`；README §3.6）：
  - 实心（棱条、芯）与半透明填充放在同一根骨骼里，骨骼内实心在前；
  - **没有 `stub_<面>`**：挂面板的面也由 `arm_<面>` 负责（条件「这一面不为 0」），一直画到
    方块面，比短接线多出的那 3 px 被面板实体的背板与面板框整个包住；
  - 样稿的 `skirt_<空面>_<f>`（空面靠外连那侧的一条填充）并进 `arm_<f>`：臂的填充面往线芯里
    多铺 1 px。代价只在两条相邻臂都外连的凹角：这 1 px 落在交界内部，透过玻璃多叠一层淡填充。
  顺序 `straight_*`、`hub`、`edge_*`、`arm_*`、`cap_*`，共 28 根。

盖板只是外观：**不进 `part_boxes`**，碰撞盒、精确点击与短接线长度都不算它。

## 部件

| 部件 | 何时有 | 由哪些盒组成 |
| --- | --- | --- |
| 线芯 `core` | 常有 | 线芯立方 |
| 连接臂 `arm` | 该面状态 1 | 线芯面 → 方块面，截面同线芯 |
| 面板 `panel` | 该面状态 2 | 面板框 + 背板 + 短接线（短接线只伸到背板，是面板的安装座） |

面板的屏幕方块在面板框之内，不单列。短接线算进面板而不算线芯：它只在挂着面板时才有，
玩家点它就是想动那块面板。

## 面板底座（2026-11-10）

`endernet:part_holder` 只装面板：**没有线芯**（`CORE_PX` 里没有它，`core_px_of` 给 None），
也就没有连接臂与短接线，每面只取 0 / 2（`HOLDER_SIDE_VALUES`，64 种）。`part_boxes` /
`unit_hull` / `pick` 收 `core=None` 时只算面板框与背板；一块面板都没有的排列（底座刚放下、
方块状态还没刷到的那一下）外包盒取 `EMPTY_HULL_PX` 的小方块，免得碰撞盒为空。
"""

import math

from .identity import FACING_NAMES, FACING_OFFSETS

# -- 尺寸（单位 px，1 格 = 16 px）--------------------------------------------

#: 一格的像素数；方块中心到各面的距离；方块中心的 y
BLOCK_PX = 16
HALF = 8
CENTER_Y = 8

#: 面板正面边长、厚度。AE2 终端类：12×12×2（局部 2～14，贴附面一侧 14～16）。
PANEL_SIZE = 12
PANEL_DEPTH = 2
#: 背板（面板背后、朝线缆一侧）边长、厚度。AE2 终端类：8×8×1（4～12，13～14）。
BACK_SIZE = 8
BACK_DEPTH = 1
#: 屏幕骨骼那块方块的厚度。它只画朝外一面，正好补上框体不画的正面；
#: 厚度本身看不见，只为在 Blockbench 里好选中。须 > 0 且 ≤ PANEL_DEPTH。
SCREEN_DEPTH = 0.5
#: 短接线外端离方块面的距离：只伸到背板，不到方块边（否则与面板正面共面闪烁）。
#: = 面板厚 + 背板厚 = 3，即 AE2 终端类 `getCableConnectionLength` 的 3
#: （AE2 的面板连接从离方块边这么远处画到线芯）。
STUB_FROM_EDGE = PANEL_DEPTH + BACK_DEPTH

#: 三种光缆的线芯边长（px），照 AE2：玻璃线 4（6～10）、包层 / 智能线 6（5～11）、
#: 致密线 10（3～13）（13 §2）。连接臂、短接线的截面与线芯同宽。能不能挂面板不在这里，
#: 看定义表的 `accepts_parts`（致密不挂：它的线芯离方块面只剩 3 = 面板 + 背板的厚度，
#: 本来也放不下短接线）。
CORE_PX = {
    "endernet:ender_glass_cable": 4,
    "endernet:ender_cable": 6,
    "endernet:ender_dense_cable": 10,
}

#: 端口盖板（不透明光缆空面上凸起的一块）：比线芯面每边缩进几 px（普通 4×4、致密 6×6）。
#: 玻璃光缆没有盖板，不在表里。只是外观，不进 `part_boxes`（碰撞盒与精确点击不算它）。
PLATE_INSET = {
    "endernet:ender_cable": 1,
    "endernet:ender_dense_cable": 2,
}
#: 端口盖板凸出线芯面的厚度（px）
PLATE_DEPTH = 1
#: 线芯外观比 `CORE_PX` 每边缩进几 px（作者 2026-11-10 在 Blockbench 里改的致密光缆：线芯画成
#: 8 px，连接臂仍按 10 px 截面，中间补一圈 1 px 的过渡）。只是外观，同盖板：碰撞盒、精确点击、
#: 连接臂与短接线的长度仍按 `CORE_PX`。不在表里的不缩。
CORE_INSET = {
    "endernet:ender_dense_cable": 1,
}

#: 玻璃光缆：骨骼是另一套（棱条 + 半透明填充 + 芯），方块整根 `blend`
GLASS_CABLE_ID = "endernet:ender_glass_cable"
#: 玻璃光缆几何体里逐面写的材质实例名：半透明填充面、芯（其余面用默认的 `*`）
MATERIAL_GLASS = "glass"
MATERIAL_FIBER = "fiber"

#: 一块面板都没有的面板底座的外包盒边长（px，居中）。正常不会停在这个排列上，只是
#: 底座刚放下、方块状态还没刷到的那一下；给一个最细光缆线芯大小的盒，不让碰撞盒为空。
EMPTY_HULL_PX = 4


def core_px_of(device_id):
    # type: (str) -> object
    """这种方块的线芯边长；面板底座（以及别的没有线芯的方块）返回 None。"""
    return CORE_PX.get(device_id)

#: (面名, 世界单位偏移)，顺序即 `FACING_NAMES`：骨骼顺序、方块状态与面板变量的面序号都按它
DIRECTIONS = tuple(zip(FACING_NAMES, FACING_OFFSETS))

# -- 方块状态、骨骼与几何体标识 ----------------------------------------------

SIDE_NONE = 0
SIDE_ARM = 1
SIDE_PANEL = 2
SIDE_STATE_PREFIX = "endernet:side_"

#: 几何体标识的前缀：`geometry.endernet.<方块短名>`（13 §2）
GEOMETRY_PREFIX = "geometry.endernet."
#: 面板的方块实体外观（`gen_cable_models.py` 生成）。能挂面板的两种光缆在资源包 `blocks.json`
#: 里绑定它（`gen_blocks.py`），每面画哪块面板由 `variable.p0`～`p5` 决定（13 §2）。
PANEL_ENTITY_ID = "endernet:ender_cable_panels"

#: 面板种类表（13 §2、12 §3.3）：`(短名, 外观所照的整方块终端)`。**生成器与运行时共用这一份**
#: （阶段 D 从 `gen_cable_models.py` 挪来）：
#:
#: - 外观变量 `variable.p<面序号>` 的取值 = `slot_display.FIRST_MODEL` + 下标（生成器的骨骼
#:   `panel_<面>_<短名>`，服务端按 `config["parts"]` 算好下发）；
#: - 面板设备 / 物品标识 = `panel_part_id(短名)`，即 `endernet:<短名>_terminal_part`；
#: - 第二列只给生成器校验定义表、取物品图标的正面贴图。
#:
#: 往后加种类直接追加，再重跑生成器（图集多一块屏幕）。
PANEL_KINDS = (
    ("common", "endernet:common_terminal_panel"),
    ("crafting", "endernet:crafting_terminal_panel"),
    ("template", "endernet:template_terminal_panel"),
    ("factoric", "endernet:factoric_terminal_panel"),
)


def panel_part_id(kind):
    # type: (str) -> str
    """某种面板的设备 / 物品标识（13 §2）。"""
    return "endernet:%s_terminal_part" % (kind,)


#: 面板设备标识，按 `PANEL_KINDS` 的顺序——也就是外观取值的顺序
PANEL_PART_IDS = tuple(panel_part_id(kind) for kind, _terminal in PANEL_KINDS)

#: 部件种类
PART_CORE = "core"
PART_ARM = "arm"
PART_PANEL = "panel"


def side_state(face):
    # type: (str) -> str
    """某一面的方块状态名 `endernet:side_<面>`。"""
    return SIDE_STATE_PREFIX + face


#: 面板底座每面能取的状态值：没有连接臂，只有「无」与「面板」
HOLDER_SIDE_VALUES = (SIDE_NONE, SIDE_PANEL)


def side_values(accepts_parts):
    # type: (bool) -> tuple
    """这种光缆每面能取的状态值：能挂面板的 0 / 1 / 2，致密 0 / 1。"""
    if accepts_parts:
        return (SIDE_NONE, SIDE_ARM, SIDE_PANEL)
    return (SIDE_NONE, SIDE_ARM)


def arm_bone(face):
    # type: (str) -> str
    return "arm_" + face


def stub_bone(face):
    # type: (str) -> str
    return "stub_" + face


def geometry_id(name):
    # type: (str) -> str
    """几何体标识。`name` 是方块短名（`ender_cable`）或面板几何名。"""
    return GEOMETRY_PREFIX + name


def short_name(device_id):
    # type: (str) -> str
    return device_id.split(":", 1)[1]


def sides_from_states(states):
    # type: (dict) -> dict
    """方块状态字典 → `{面名: 状态值}`。读不出的面按 0（什么都没有）。"""
    states = states or {}
    out = {}
    for face in FACING_NAMES:
        value = states.get(side_state(face), SIDE_NONE)
        if isinstance(value, bool) or not isinstance(value, int):
            value = SIDE_NONE
        out[face] = value if value in (SIDE_NONE, SIDE_ARM, SIDE_PANEL) else SIDE_NONE
    return out


# -- 骨骼与显示条件（2026-11-10 光缆美术重做）------------------------------------
#
# 骨骼清单与显示条件只写这一份：几何体（`gen_cable_models.py` 按清单逐根出方块）与方块 JSON
# 的 `bone_visibility`（`gen_blocks.py`）都从 `cable_bones` 取，骨骼名与顺序对得上。

#: 轴（0 = 东西 x、1 = 上下 y、2 = 南北 z）→ (负方向的面, 正方向的面)
AXIS_FACES = (("west", "east"), ("down", "up"), ("north", "south"))
#: 直通骨骼名，按轴（用世界方向，作者在 Blockbench 里一眼能认）
STRAIGHT_BONES = ("straight_ew", "straight_ud", "straight_ns")
#: 玻璃光缆的线芯骨骼（八个框角 + 芯的交汇点）
HUB_BONE = "hub"

#: 一个方块 `minecraft:geometry.bone_visibility` 最多写几条（也就是光缆几何体最多几根骨骼，
#: 每根都要一条）。引擎有上限、文档查不到确切数字：作者真机日志里 73 条的玻璃光缆整块加载
#: 失败（`bone_visibility: too many input elements for an associative container`），21 条的
#: 普通光缆与面板底座、15 条的致密光缆都能加载（2026-11-10）。按 32 收，生成器超了就报错。
BONE_VISIBILITY_LIMIT = 32

#: 骨骼的角色（生成器按它出方块）
ROLE_STRAIGHT = "straight"
ROLE_ARM = "arm"
ROLE_STUB = "stub"
ROLE_CAP = "cap"
ROLE_HUB = "hub"
ROLE_EDGE = "edge"


def is_glass(device_id):
    # type: (str) -> bool
    return device_id == GLASS_CABLE_ID


def face_axis(face):
    # type: (str) -> tuple
    """面 → `(轴, 正负)`：east → (0, 1)、down → (1, -1)。"""
    for axis, pair in enumerate(AXIS_FACES):
        if face in pair:
            return axis, (1 if face == pair[1] else -1)
    raise ValueError("不认识的面 %r" % (face,))


def straight_bone(axis):
    # type: (int) -> str
    return STRAIGHT_BONES[axis]


def cap_bone(face):
    # type: (str) -> str
    return "cap_" + face


def edge_bone(first, second):
    # type: (str, str) -> str
    """玻璃线芯上 `first`、`second` 两面相交的那条棱。"""
    return "edge_%s_%s" % (first, second)


def straight_axis(sides):
    # type: (dict) -> object
    """直通的轴：恰好一对对面是连接臂（状态 1）、其余四面为空时返回那条轴，否则 None。

    挂着面板（状态 2）的面不算直通：要画线芯与短接线。`sides` 缺的面按 0。
    """
    sides = sides or {}
    for axis, pair in enumerate(AXIS_FACES):
        if all(sides.get(face, SIDE_NONE) == (SIDE_ARM if face in pair else SIDE_NONE)
               for face in FACING_NAMES):
            return axis
    return None


def _straight_sides(axis):
    pair = AXIS_FACES[axis]
    return dict((face, SIDE_ARM if face in pair else SIDE_NONE) for face in FACING_NAMES)


def _state_in(face, values, universe):
    """molang：`side_<face>` 的取值在 `values` 之内。只一个值写 `==`，补集只一个值写 `!=`。"""
    query = "q.block_state('%s')" % side_state(face)
    if len(values) == 1:
        return "%s == %d" % (query, next(iter(values)))
    rest = [value for value in universe if value not in values]
    if len(rest) == 1:
        return "%s != %d" % (query, rest[0])
    return "(%s)" % " || ".join("%s == %d" % (query, value) for value in sorted(values))


def straight_condition(axis, universe):
    # type: (int, tuple) -> str
    """直通骨骼的条件：六面状态全部写明。`universe` 是每面能取的值（`side_values`）。"""
    sides = _straight_sides(axis)
    return " && ".join(
        _state_in(face, frozenset((sides[face],)), universe) for face in FACING_NAMES
    )


def bone_condition(base, universe):
    # type: (dict, tuple) -> str
    """骨骼的条件：`base`（`{面: 允许的取值}`）成立，且不是直通。

    「不是直通」只写与 `base` 相容的那几种直通，并省掉 `base` 已经保证的项，molang 短一些。
    `base` 为空（玻璃的 `hub`）就是「不是任何一种直通」。
    """
    base = dict((face, frozenset(values)) for face, values in base.items())
    parts = [_state_in(face, base[face], universe) for face in FACING_NAMES if face in base]
    for axis in range(3):
        straight = _straight_sides(axis)
        if any(straight[face] not in values for face, values in base.items()):
            continue  # 与 base 矛盾：这种直通不会发生
        residual = [
            _state_in(face, frozenset((straight[face],)), universe)
            for face in FACING_NAMES
            if base.get(face) != frozenset((straight[face],))
        ]
        if not residual:
            raise ValueError("骨骼条件 %r 只在直通时成立" % (base,))
        parts.append("!(%s)" % " && ".join(residual))
    return " && ".join(parts)


class CableBone(object):
    """光缆几何体里的一根骨骼。

    - `role`：`ROLE_*` 之一，生成器按它出方块；
    - `axis`：直通骨骼的轴（其余为 None）；
    - `faces`：连接臂 / 短接线 / 空面是 `(面,)`，玻璃的棱是 `(面, 面)`，直通与 `hub` 为空；
    - `condition`：`bone_visibility` 里的 molang。
    """

    __slots__ = ("name", "role", "condition", "axis", "faces")

    def __init__(self, name, role, condition, axis=None, faces=()):
        # type: (str, str, str, object, tuple) -> None
        self.name = name
        self.role = role
        self.condition = condition
        self.axis = axis
        self.faces = tuple(faces)

    def __repr__(self):
        return "CableBone(%s)" % (self.name,)


def cable_bones(device_id, accepts_parts):
    # type: (str, bool) -> list
    """这种光缆几何体里的全部骨骼，按几何文件里的顺序：`[CableBone]`。

    `accepts_parts` 取定义表的（能挂面板的每面多一档状态 2）。
    不透明光缆：直通 3 + 连接臂 6（+ 能挂面板的短接线 6）+ 空面 6（21 / 15 根）；
    玻璃：直通 3 + `hub` + 棱 12 + 连接臂 6（兼管挂面板的面，没有短接线）+ 空面 6，共 28 根
    （合并的理由与代价见模块说明「骨骼」一节）。总数不超过 `BONE_VISIBILITY_LIMIT`。
    """
    universe = side_values(accepts_parts)
    none = frozenset((SIDE_NONE,))
    opened = frozenset(value for value in universe if value != SIDE_NONE)
    glass = is_glass(device_id)
    straights = [
        CableBone(straight_bone(axis), ROLE_STRAIGHT, straight_condition(axis, universe), axis=axis)
        for axis in range(3)
    ]
    # 玻璃的连接臂兼管挂面板的面（状态 1 与 2），所以没有单独的短接线
    arm_values = opened if glass else frozenset((SIDE_ARM,))
    arms = [
        CableBone(arm_bone(face), ROLE_ARM, bone_condition({face: arm_values}, universe),
                  faces=(face,))
        for face in FACING_NAMES
    ]
    stubs = [
        CableBone(stub_bone(face), ROLE_STUB, bone_condition({face: (SIDE_PANEL,)}, universe),
                  faces=(face,))
        for face in FACING_NAMES
    ] if accepts_parts and not glass else []
    caps = [
        CableBone(cap_bone(face), ROLE_CAP, bone_condition({face: none}, universe), faces=(face,))
        for face in FACING_NAMES
    ]
    if not glass:
        return straights + arms + stubs + caps
    hub = CableBone(HUB_BONE, ROLE_HUB, bone_condition({}, universe))
    edges = []
    for index, first in enumerate(FACING_NAMES):
        for second in FACING_NAMES[index + 1:]:
            if face_axis(first)[0] != face_axis(second)[0]:
                edges.append(CableBone(
                    edge_bone(first, second), ROLE_EDGE,
                    bone_condition({first: none, second: none}, universe), faces=(first, second)))
    return straights + [hub] + edges + arms + caps


def bone_visibility(device_id, accepts_parts):
    # type: (str, bool) -> dict
    """方块 JSON 的 `minecraft:geometry.bone_visibility`：`{骨骼: molang}`，每根骨骼都写。"""
    return dict((bone.name, bone.condition) for bone in cable_bones(device_id, accepts_parts))


# -- 像素盒（相对方块底面中心）--------------------------------------------


def centered(half):
    """以方块中心为心、边长 2 × half 的立方体。盒子都用世界方向，相对方块底面中心。"""
    return (-half, CENTER_Y - half, -half), (half, CENTER_Y + half, half)


def along(vector, near, far, half):
    """沿 vector 方向、离中心 near..far、截面半宽 half 的长方体。"""
    low, high = [], []
    for axis, component in enumerate(vector):
        middle = CENTER_Y if axis == 1 else 0
        if component:
            ends = (middle + component * near, middle + component * far)
            low.append(min(ends))
            high.append(max(ends))
        else:
            low.append(middle - half)
            high.append(middle + half)
    return tuple(low), tuple(high)


def face_center(vector):
    """方块某一面的中心点（像素坐标）。"""
    return tuple((CENTER_Y if axis == 1 else 0) + component * HALF
                 for axis, component in enumerate(vector))


def arm_length(core):
    """连接臂的长度：线芯面 → 方块面。"""
    return HALF - core / 2.0


def stub_length(core):
    """短接线的长度：线芯面 → 背板背面。≤ 0 表示这种线芯放不下面板（生成器据此报错）。"""
    return HALF - STUB_FROM_EDGE - core / 2.0


def core_box(core):
    return centered(core / 2.0)


def arm_box(core, vector):
    half = core / 2.0
    return along(vector, half, HALF, half)


def stub_box(core, vector):
    half = core / 2.0
    return along(vector, half, HALF - STUB_FROM_EDGE, half)


def frame_box(vector):
    """面板框：12×12×2，贴着方块面。"""
    return along(vector, HALF - PANEL_DEPTH, HALF, PANEL_SIZE / 2.0)


def back_box(vector):
    """背板：8×8×1，在面板框背后。"""
    return along(vector, HALF - PANEL_DEPTH - BACK_DEPTH, HALF - PANEL_DEPTH, BACK_SIZE / 2.0)


def screen_box(vector):
    """屏幕方块：只画朝外一面，在面板框之内。"""
    return along(vector, HALF - SCREEN_DEPTH, HALF, PANEL_SIZE / 2.0)


# -- 部件与外包盒 ------------------------------------------------------------


def part_boxes(core, sides):
    # type: (float, dict) -> list
    """这一格现有的部件盒：`[(部件, 面, 像素盒)]`，线芯的面为 None。

    `sides` 是 `{面名: 状态值}`（缺的面按 0）。一块面板由框、背板、短接线三个盒组成，
    三条的部件与面相同。`core` 为 None（面板底座）时没有线芯、连接臂与短接线，面板只有
    框与背板。
    """
    sides = sides or {}
    boxes = [] if core is None else [(PART_CORE, None, core_box(core))]
    for face, vector in DIRECTIONS:
        value = sides.get(face, SIDE_NONE)
        if value == SIDE_ARM and core is not None:
            boxes.append((PART_ARM, face, arm_box(core, vector)))
        elif value == SIDE_PANEL:
            panel = [frame_box(vector), back_box(vector)]
            if core is not None:
                panel.append(stub_box(core, vector))
            for box in panel:
                boxes.append((PART_PANEL, face, box))
    return boxes


def to_unit(box):
    """像素盒 → 格内坐标盒（原点在方块最小角，各轴 0～1）。"""
    (x0, y0, z0), (x1, y1, z1) = box
    return (
        ((x0 + HALF) / float(BLOCK_PX), y0 / float(BLOCK_PX), (z0 + HALF) / float(BLOCK_PX)),
        ((x1 + HALF) / float(BLOCK_PX), y1 / float(BLOCK_PX), (z1 + HALF) / float(BLOCK_PX)),
    )


def hull(boxes):
    """几个盒的外包盒。"""
    lows = [box[0] for box in boxes]
    highs = [box[1] for box in boxes]
    return (
        tuple(min(low[axis] for low in lows) for axis in range(3)),
        tuple(max(high[axis] for high in highs) for axis in range(3)),
    )


def unit_hull(core, sides):
    # type: (float, dict) -> tuple
    """这一格的碰撞 / 选取外包盒（格内坐标）：线芯 + 状态 1 的臂 + 状态 2 的面板。

    `netease:aabb` 每个排列只有一个盒（蔚蓝做法），L 形、T 形会把空角包进去。面板底座
    （`core` 为 None）一块面板都没有时取 `EMPTY_HULL_PX` 的居中小盒。
    """
    boxes = part_boxes(core, sides)
    if not boxes:
        return to_unit(core_box(EMPTY_HULL_PX))
    return hull([to_unit(box) for _part, _face, box in boxes])


# -- 射线求交 ----------------------------------------------------------------

#: 格内坐标的轴 → (射线朝正方向时进入的面, 朝负方向时进入的面)
_ENTRY_FACES = (("west", "east"), ("down", "up"), ("north", "south"))
#: 方向分量的绝对值小于它就当作与该轴平行
_PARALLEL = 1e-12
#: 同一距离上的优先次序：面板、臂优先于线芯（点在交界处时取更具体的那个）
_RANK = {PART_PANEL: 0, PART_ARM: 1, PART_CORE: 2}


class CableHit(object):
    """一次命中：点中的部件、面与射线距离。

    - `part` 是 `PART_CORE` / `PART_ARM` / `PART_PANEL`；
    - 臂与面板的 `face` 是它们所在的那一面；
    - 线芯的 `face` 是**射线进入线芯的那一面**（13 §6「线芯朝向某面」）：从东边看过去
      点中线芯，就是东面。射线起点在线芯里面时退回「迎着射线的那一面」。
    """

    __slots__ = ("part", "face", "distance")

    def __init__(self, part, face, distance):
        # type: (str, str, float) -> None
        self.part = part
        self.face = face
        self.distance = distance

    def to_dict(self):
        # type: () -> dict
        return {"part": self.part, "face": self.face, "distance": self.distance}

    def __eq__(self, other):
        if not isinstance(other, CableHit):
            return NotImplemented
        return (self.part, self.face) == (other.part, other.face)

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return hash((self.part, self.face))

    def __repr__(self):
        return "CableHit(%s, %s, %.4f)" % (self.part, self.face, self.distance)


def enter(box, origin, direction):
    """slab 法：射线进入盒子时的 `(距离, 进入面)`；不相交返回 None。

    `box` 与 `origin` 用同一套坐标；`direction` 不必归一（距离以它的长度为单位）。
    擦边（恰好碰到棱或面）算命中。起点在盒子里面时距离为 0、进入面为 None；
    盒子整个在射线背后不算命中。
    """
    low, high = box
    t_near = float("-inf")
    t_far = float("inf")
    face = None
    for axis in range(3):
        start = origin[axis]
        step = direction[axis]
        if abs(step) < _PARALLEL:
            if start < low[axis] or start > high[axis]:
                return None
            continue
        inverse = 1.0 / step
        t1 = (low[axis] - start) * inverse
        t2 = (high[axis] - start) * inverse
        if step > 0:
            near, far, side = t1, t2, _ENTRY_FACES[axis][0]
        else:
            near, far, side = t2, t1, _ENTRY_FACES[axis][1]
        if near > t_near:
            t_near, face = near, side
        if far < t_far:
            t_far = far
        if t_near > t_far:
            return None
    if t_far < 0:
        return None
    if t_near < 0:
        return 0.0, None
    return t_near, face


def facing_against(direction):
    """迎着射线的那一面：射线朝东走，看到的就是物体的西面。方向为零向量时返回 None。"""
    axis = max(range(3), key=lambda index: abs(direction[index]))
    if abs(direction[axis]) < _PARALLEL:
        return None
    return _ENTRY_FACES[axis][0] if direction[axis] > 0 else _ENTRY_FACES[axis][1]


def pick(core, sides, origin, direction):
    # type: (float, dict, tuple, tuple) -> CableHit
    """格内坐标的射线与这一格现有的部件求交，取最近的命中；一个都没碰到返回 None。

    `origin` 已减去方块最小角（方块占 [0, 1]³），`direction` 不必归一。只碰到外包盒、
    没碰到任何部件（L 形外包盒的空角）也是 None。同一距离上面板、臂优先于线芯，
    再按面序，结果是确定的。`core` 为 None 是面板底座（只有面板可点）。
    """
    if facing_against(direction) is None:
        return None
    best = None
    for part, face, box in part_boxes(core, sides):
        hit = enter(to_unit(box), origin, direction)
        if hit is None:
            continue
        distance, entered = hit
        rank = (distance, _RANK[part], FACING_NAMES.index(face) if face else -1)
        if best is None or rank < best[0]:
            best = (rank, part, face, entered)
    if best is None:
        return None
    (distance, _rank, _order), part, face, entered = best
    if part == PART_CORE:
        face = entered if entered is not None else facing_against(direction)
    return CableHit(part, face, distance)


def hull_entry(core, sides, origin, direction):
    # type: (object, dict, tuple, tuple) -> object
    """射线从哪一面进这一格的外包盒（`unit_hull`，就是引擎拿来点选的 clip 盒）——也就是
    「被点击的那一面」。面板像火把一样放到相邻格时用它（客户端事件没带面时）。

    射线起点就在盒内、或根本碰不到盒时退回「迎着射线的那一面」；方向为零向量返回 None。
    """
    hit = enter(unit_hull(core, sides), origin, direction)
    if hit is not None and hit[1] is not None:
        return hit[1]
    return facing_against(direction)


# -- 射线从哪来 ----------------------------------------------------------------
#
# 两侧的使用事件（客户端 `ClientBlockUseEvent` / `ClientItemUseOnEvent`，服务端
# `ServerBlockUseEvent` / `ServerItemUseOnEvent`）都带被点的方块坐标与格内命中点
# `clickX/Y/Z`：引擎沿真实的触点或准星射线求出来的，落在该格 clip 盒（`unit_hull`）的表面上。
# **口径**：文档原文「点击点的 x 比例位置」，即命中点相对方块最小角的格内坐标（各轴 0～1，
# 不是相对 clip 盒的比例），蔚蓝管道的扳手（`transmitters/base/action.py::get_pick_facing`）
# 正是这样拿它与格内阈值比的。
#
# 射线 = 观察点 → 命中点：客户端发意图时观察点是相机原点（`client/interaction`），服务端
# 右键开界面时是玩家眼睛（`server/interaction/opening.py`）。两条都穿过同一个命中点：命中点
# 落在某个部件的外表面上时，求交结果与观察点无关。


#: 观察点与命中点贴得比这还近（格），方向就算不出来
MIN_RAY_LENGTH = 1e-6


def click_point(pos, click):
    # type: (tuple, tuple) -> tuple
    """被点的方块坐标 + 事件的格内命中点（`clickX/Y/Z`，0～1）→ 命中点的世界坐标。"""
    return tuple(float(pos[axis]) + float(click[axis]) for axis in range(3))


def click_ray(origin, pos, click):
    # type: (tuple, tuple, tuple) -> object
    """观察点 → 命中点的射线 `(起点, 单位方向)`（世界坐标）；读数不全、不是有限数或两点重合
    返回 None。"""
    try:
        start = tuple(float(value) for value in origin)
        end = click_point(pos, click)
    except (TypeError, ValueError, IndexError):
        return None
    if len(start) != 3:
        return None
    for value in start + end:
        if math.isnan(value) or math.isinf(value):
            return None
    delta = tuple(end[axis] - start[axis] for axis in range(3))
    length = math.sqrt(sum(component * component for component in delta))
    if length < MIN_RAY_LENGTH:
        return None
    return start, tuple(float(component) / length for component in delta)
