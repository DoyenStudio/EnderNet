# coding=utf-8
"""三种末影光缆与线缆面板：几何体、贴图、面板的方块实体外观、物品形态的几何体；另附扳手的物品占位图标。

    py -3.11 -B scripts/gen_cable_models.py             # 生成；贴图缺了才补
    py -3.11 -B scripts/gen_cable_models.py --check     # 只比对、不写（贴图只查在不在、尺寸对不对）
    py -3.11 -B scripts/gen_cable_models.py --layout    # 打印各贴图的 UV 分区（照着重画）
    py -3.11 -B scripts/gen_cable_models.py --textures  # 贴图也按本脚本重画（**覆盖**已有贴图）
    py -3.11 -B scripts/gen_cable_models.py --probe     # 真机排查：六面先各显示一块面板
    py -3.11 -B scripts/gen_cable_models.py --icons     # 另补物品占位图标（只补缺的；见下）

本脚本只出外观；方块 JSON、`blocks.json` 与物品由 `gen_blocks.py` / `gen_items.py` 注册
（阶段 C，`.claude/13` 文末的执行记录）。面板的接法与待定项见
`.claude/12-cable-panel-modeling.md`。部件尺寸、光缆的骨骼清单与显示条件在
`common/cable_shapes.py`（与方块碰撞盒、`bone_visibility`、运行时的精确交互共用一份）。

## 三种光缆（`CABLES`，2026-11-10 美术重做，`.claude/cable-art/README.md`）

末影玻璃光缆、末影光缆、致密末影光缆，线芯 4 / 6 / 10 px（AE2 玻璃线 / 包层线 /
致密线）。前两种能挂面板（普通光缆多 6 根 `stub_<面>`，玻璃由连接臂兼管）；致密不挂
（13 §0-8）。照 AE2 的三分法，
每格画三种之一（骨骼清单与条件见 `cable_shapes.cable_bones`）：

- 直通 `straight_<轴>`：一根贯穿整格的管子，不画线芯，两端画截面；
- 否则线芯：外连的面接 `arm_<面>`（外端截面封口——粗细相接处有面），挂面板的面接
  `stub_<面>`（短接线长度不变），空着的面画 `cap_<面>`（线芯面 + 凸起 1 px 的端口盖板）；
- 致密光缆的线芯外观缩进 1 px（`cable_shapes.CORE_INSET`，作者 2026-11-10 在 Blockbench 里改）：
  线芯面与盖板随之往里挪，连接臂仍是 10 px 截面、内端面露出的一圈画线芯空面，线芯面与臂之间
  补一块 1 px 的过渡（侧面取线芯空面的边，朝线芯那面取盖板）。这几处贴图照作者的拉伸写
  （`region_uv(..., stretch=True)`）；
- 玻璃光缆：四根 1×1 棱条 + 四块半透明填充面 + 正中 1×1 的芯（`glass_bones`）；线芯拆成
  框角 / 棱 / 封面分别显隐，框只出现在真实的棱上。逐面写材质实例
  `glass`（填充）/ `fiber`（芯），方块整根 `blend`（`gen_blocks.py`）。为了把骨骼数压到
  `BONE_VISIBILITY_LIMIT` 以内（2026-11-10 真机：73 根时方块加载失败），玻璃的实心与填充同在
  一根骨骼里、没有短接线（挂面板的面由连接臂画到方块面，多出的一段藏在面板实体里）、样稿的
  填充条并进连接臂（臂的填充面往线芯里多铺 1 px），共 28 根（`cable_shapes` 模块说明）。

贴图分区是固定表 `REGIONS`（`--layout` 打印）：侧面是整 16 px 长的条带，每个侧面按它在
格子里的位置取对应的那几列；**沿长度每列相同、只沿截面变化**，任何拼法格与格之间都连续。
贴图由 `build_texture` 画（配色、截面、盖板图案都在本文件）。

端口盖板只是外观：碰撞盒、精确点击（`cable_shapes.part_boxes`）都不算它。

## 面板的显示照搬槽位模块（`gen_slot_models.py`、`common/channels/slot_display.py`）

槽 = 方块的六个面，模块 = 面板种类。实体几何里每面一根框体骨骼 `frame_<面>`，
其下「每种面板」各一根屏幕骨骼 `panel_<面>_<种类>`（形状相同，UV 指向图集里
各自的屏幕区）；一个渲染控制器按 `variable.p<面序号>` 决定显示哪根。面序号 =
`identity.FACING_NAMES` 的下标；取值沿用 slot_display：0 无面板、1 认不出的种类
（画 `default`）、2 + i = `PANEL_KINDS` 第 i 个。

## 产物

    endernet_rp/models/blocks/<光缆>.geo.json              每种光缆一份：直通 / 连接臂 / 短接线 / 空面
    endernet_rp/models/blocks/<光缆>_item.geo.json         光缆物品：一段直通（方块 JSON 的 item_visual 指向它）
    endernet_rp/models/blocks/<面板>.geo.json              面板物品：居中、屏幕朝南的单块面板（面板展示方块的几何）
    endernet_rp/models/entity/ender_cable_panels.geo.json  一格 6 面 × 每种面板（方块实体外观）
    endernet_rp/models/entity/ender_cable_panel.geo.json   单块朝北（同上的北面那组骨骼，给 Blockbench 画图）
    endernet_rp/entity/ender_cable_panels.entity.json      方块实体外观（玻璃与普通光缆在 blocks.json 引用它）
    endernet_rp/render_controllers/endernet_cable_panels.render_controllers.json
    endernet_rp/textures/endernet/models/<光缆>.png        光缆贴图：每种一张（模型方块贴图的约定目录）
    endernet_rp/textures/endernet/entity/ender_cable_panels.png  面板占位图集（两份面板几何共用）
    endernet_rp/textures/endernet/items/<物品>.png         物品占位图标（只有 `--icons` 才写）

几何体与 json 每次按参数重算，按内容比（`gen_slot_models.same_content`）：作者在 Blockbench
里另存、只改了排版的文件不算变，不会被改回脚本的排版；作者在 Blockbench 里改了**形状或贴法**，
要把改动写回本脚本（2026-11-10 的致密线芯缩进、面板框侧边方向都是这样回写的），否则下次生成会
改回去、`--check` 也会报。贴图是**种子**：缺了才写（光缆照 `build_texture`、面板图集画占位图），
已有的（作者重画过的）只查尺寸、不覆盖。改尺寸、`REGIONS` 或面板种类表会改 UV 分区，重画过的
贴图要按 `--layout` 对一遍。`--check --textures` 连贴图内容一起比。

面板框侧边的条带从背面（靠宿主）画到正面（屏幕那侧）：作者 2026-11-10 重画的图集里深色一行 /
一列在背面，并在面板物品的几何里把方向反了的两个侧边翻了过来；实体外观与物品形态共用
`panel_shell_faces`，六个朝向一个口径（`edge_flip`）。

## 物品形态（2026-11-10 作者定：物品栏里显示 3D，照 AE2）

- 光缆是方块物品，方块 JSON 的 `minecraft:item_visual` 指向 `<光缆>_item`：一段 12 px 长、
  东西向的直通（AE2 光缆物品同样是一段不顶格的直段），两端封口。不在 item_visual 里写骨骼
  显隐复用方块几何：网易认不认没有依据；单独一份物品几何是蔚蓝电线、管道已验证的做法。
- 四种面板注册成同名的展示方块（方案 B，`gen_blocks.build_part_item_json`；基岩版普通物品在
  物品栏里只能画 2D 图标），几何就是 `<面板>`：贴在南面的面板整体挪到方块中心（AE2 的面板
  物品同理），屏幕朝南——物品栏镜头看的是方块的南面。贴图是面板图集。
- 物品栏、手持、掉落物共用这份几何（基岩版没有逐视角的变换），大小与角度只能改几何。

## 物品图标为什么要 `--icons` 才写

只剩扳手一张 16×16 占位图标（光缆、面板在物品栏里显示 3D，不画图标）。`test_assets` 不许
物品贴图目录里有没登记进 `item_texture.json` 的图（`test_no_unreferenced_item_textures`），所以
`--icons` **只写物品图集里登记了的**（登记由 `gen_items.py` 做，先跑它）。只补缺的，已有的
（作者重画过的）一律不碰，`--textures` 也不碰；`--check` 只在带 `--icons` 时才查登记了的
图标在不在。

## 坐标

- 方块几何体：中心原点，x、z ∈ [-8, 8]，y ∈ [0, 16]。**文件 x 与世界 x 相反**
  （`GEO_X`）：世界东 = 文件 -x；南北、上下不反。逐面 UV 的键就是世界方位名。
- 实体几何体：原点在方块角点，照 `gen_slot_models.ENTITY_ORIGIN` 平移（这一格是
  文件 x -16..0、z 0..16）。
- 尺寸都写成「离方块中心 / 离方块面多少 px」，六个方向由同一段代码转出。
"""

import argparse
import collections
import math
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_ROOT = os.path.join(os.path.dirname(HERE), "endernet_bp", "endernet_scripts")
for path in (SCRIPTS_ROOT, HERE):
    if path not in sys.path:
        sys.path.insert(0, path)

from _console import use_utf8_stdout  # noqa: E402
from _png import png_bytes  # noqa: E402
# 方块实体外观的角点原点、实体材质只在一处定义（真机踩过的坑）；输出与比对也照它写
from gen_slot_models import (  # noqa: E402
    ENTITY_ORIGIN,
    MATERIAL,
    RP_ROOT,
    num,
    plural_dumps,
    rel,
    write_if_changed,
)
# 模型方块的贴图目录只在 gen_blocks 写一处（它负责地形图集）
from gen_blocks import MODEL_TEXTURE_DIR  # noqa: E402

from endernet.common.definitions import CABLES as CABLE_DEFINITIONS  # noqa: E402
from endernet.common.definitions import DefinitionSet  # noqa: E402
# 面的顺序全仓库共用一份（拓扑、端口都按它）；面板变量沿用槽位模块的取值
from endernet.common.identity import FACING_NAMES, opposite_face  # noqa: E402
from endernet.common.channels.slot_display import EMPTY, FIRST_MODEL, UNKNOWN  # noqa: E402
# 部件尺寸与盒子只写一份：几何体、方块碰撞盒（gen_blocks）与运行时的精确交互共用（13 §6）
from endernet.common.cable_shapes import (  # noqa: E402
    AXIS_FACES,
    BACK_DEPTH,
    BACK_SIZE,
    BONE_VISIBILITY_LIMIT,
    CENTER_Y,
    CORE_INSET,
    CORE_PX,
    DIRECTIONS,
    HALF,
    MATERIAL_FIBER,
    MATERIAL_GLASS,
    PANEL_DEPTH,
    PANEL_ENTITY_ID,
    PANEL_KINDS,
    PANEL_SIZE,
    PLATE_DEPTH,
    PLATE_INSET,
    ROLE_ARM,
    ROLE_CAP,
    ROLE_EDGE,
    ROLE_HUB,
    ROLE_STRAIGHT,
    ROLE_STUB,
    SCREEN_DEPTH,
    STUB_FROM_EDGE,
    arm_length,
    back_box,
    cable_bones,
    face_axis,
    face_center,
    frame_box,
    geometry_id,
    is_glass,
    screen_box,
    short_name,
)
from endernet.common.cable_shapes import stub_length as shape_stub_length  # noqa: E402

# -- 尺寸参数（单位 px，1 格 = 16 px）------------------------------------------
#
# 线芯边长、面板 / 背板 / 屏幕的尺寸与短接线外端的位置都在 `common/cable_shapes.py`：
# 方块的碰撞盒（`gen_blocks.py`）与运行时的精确交互要用同一份，改尺寸去那里改，
# 再重跑本脚本与 `gen_blocks.py`。

class Cable(collections.namedtuple(
        "Cable", "device_id name label core panels glass plate_inset core_inset")):
    """一种光缆：`name` 是方块短名，也是几何体、贴图、物品图标的名字；`core` 是线芯边长
    （直通、连接臂、短接线的截面与它同宽）；`panels` 为真才能挂面板、才有 `stub_<面>`；
    `glass` 为真走玻璃那套骨骼；`plate_inset` 是端口盖板每边比线芯面缩进几 px（玻璃为 None）；
    `core_inset` 是线芯外观每边比 `core` 缩进几 px（`cable_shapes.CORE_INSET`，多数为 0）。"""

    __slots__ = ()

    @property
    def half(self):
        return self.core / 2.0


#: 三种光缆（13 §2），按定义表的顺序：短名、中文名与能否挂面板取自定义表
#: （`definitions.CABLES`；致密不挂面板，13 §0-8），线芯边长、盖板与线芯外观的缩进取自
#: `cable_shapes`（`CORE_PX`：AE2 玻璃线 4、包层 / 智能线 6、致密线 10；`PLATE_INSET`；
#: `CORE_INSET`）。
CABLES = tuple(
    Cable(definition.id, short_name(definition.id), definition.label, CORE_PX[definition.id],
          definition.accepts_parts, is_glass(definition.id), PLATE_INSET.get(definition.id),
          CORE_INSET.get(definition.id, 0))
    for definition in CABLE_DEFINITIONS
)
#: 短接线外端离方块中心多远：面板 + 背板之内（`cable_shapes.stub_box` 同一个数）
STUB_END = HALF - STUB_FROM_EDGE

#: 面板种类表 `PANEL_KINDS`（(短名, 外观所照的整方块终端)）在 `common/cable_shapes.py`：
#: 服务端下发的外观取值要与这里的骨骼顺序同源（阶段 D）。变量值 = FIRST_MODEL + 下标；
#: 短名进骨骼名 `panel_<面>_<短名>`，也决定面板物品名 `<短名>_terminal_part`（13 §2），
#: 物品图标的正面就取这台终端的正面贴图。往后加种类去那里追加再重跑（UV 分区随之变）。
#: 认不出的种类（变量值 1）画的那块
DEFAULT_PANEL = "default"
#: `--probe`：不靠服务端，六面先各显示一块。东西两面颜色最分明（青 = 通用，
#: 琥珀 = 合成），真机一看就知道东西有没有装反；上下画默认面板。
PROBE = {
    "east": "common",
    "west": "crafting",
    "north": "template",
    "south": "factoric",
    "up": DEFAULT_PANEL,
    "down": DEFAULT_PANEL,
}

#: 贴图宽（1 px 模型 = 1 个贴图像素）；高按用量取 2 的幂、至少 16（光缆按 `REGIONS`：
#: 玻璃、普通 32×16，致密 32×32）。
CABLE_TEXTURE_WIDTH = 32
PANEL_TEXTURE_WIDTH = 64

# -- 坐标约定 --------------------------------------------------------------

#: 几何文件的 x 与世界 x 相反：文件 +x = 世界西。证据（同一引擎上跑通的）：
#: 蔚蓝电线 / 管道给邻格 (1, 0, 0) 置 `connection_east`（`transmitters/constants.py`
#: 的 `DXYZ_FACING`），显示它的骨骼 `east` 却在文件 x -8..-2（`models/blocks/wire.json`），
#: 同一排列的 `netease:aabb`（世界坐标）朝 x = 1.0 伸；BedrockWiki 的 SelectionBoxes
#: 也写明 geo.json 的 origin 从方块东北角量起。
GEO_X = -1
#: 逐面 UV 的写出顺序（照 Blockbench 导出）
FACE_ORDER = ("north", "east", "south", "west", "up", "down")

#: 网易运行时能加载的几何版本之一（见 test_assets）；蔚蓝管道与本仓库实体几何
#: 都用它，本模型也用不到逐面 `uv_rotation`。
FORMAT_VERSION = "1.12.0"
#: (visible_bounds 宽, 高, 偏移)：方块几何体照蔚蓝电线；实体照槽位模块（原点在角点，要罩住整格）
BLOCK_BOUNDS = (2, 2.5, (0, 0.75, 0))
ENTITY_BOUNDS = (3, 2.5, (0, 0.75, 0))

# -- 产物 ------------------------------------------------------------------

PANELS = "ender_cable_panels"
PANEL = "ender_cable_panel"
#: 光缆的方块模型贴图照本仓库模型方块的约定放 `textures/endernet/models/<短名>.png`
#: （图集键 `endernet:<短名>_model`，`test_assets` 按这个目录核贴图尺寸）；面板图集是
#: 方块实体外观的贴图，与槽位模块的图集同放 `textures/endernet/entity/`。
CABLE_TEXTURE_DIR = MODEL_TEXTURE_DIR
PANEL_TEXTURE_DIR = "textures/endernet/entity"
ENTITY_ID = PANEL_ENTITY_ID
CONTROLLER_ID = "controller.render.endernet.ender_cable_panels"
ENTITY_FILE = os.path.join(RP_ROOT, "entity", PANELS + ".entity.json")
CONTROLLER_FILE = os.path.join(
    RP_ROOT, "render_controllers", "endernet_cable_panels.render_controllers.json"
)

# -- 占位配色（取自 `.claude/endernet-art-direction.html` 的暗色调色板）-----------

NET = ((0x5E, 0xF7, 0xDC), (0x1F, 0xBF, 0xA6), (0x13, 0x54, 0x4C))
ITEM = ((0xFF, 0xC9, 0x5C), (0xD1, 0x8F, 0x2A), (0x5A, 0x41, 0x19))
CRAFT = ((0xC3, 0xA2, 0xFF), (0x8A, 0x63, 0xE6), (0x3A, 0x2A, 0x66))
GRAY = ((0xC8, 0xD0, 0xDC), (0x7A, 0x86, 0x98), (0x3A, 0x43, 0x51))

#: 面板图集 UV 分区的种类：(中文名, 填充, 描边)
KINDS = {
    "frame_back": ("框体背面", (0x3A, 0x43, 0x51), (0x21, 0x28, 0x33)),
    "frame_edge": ("框体侧边", (0x7A, 0x86, 0x98), (0x3A, 0x43, 0x51)),
    "back_face": ("背板背面", (0x2B, 0x33, 0x40), (0x16, 0x1B, 0x24)),
    "back_edge": ("背板侧边", (0x53, 0x5E, 0x70), (0x2B, 0x33, 0x40)),
    "screen": ("屏幕", (0x0D, 0x10, 0x16), (0x7A, 0x86, 0x98)),
}
#: 各种屏幕的色系（照美术方向稿：通用 net、合成 item、样板 / 工厂 craft）与屏内示意
#: （列表、合成格、样板卡、流程图；10×8，`#` = 色系中间色），只为一眼分清种类
SCREENS = {
    DEFAULT_PANEL: (GRAY, ("", "", "   ####", "   #  #", "   #  #", "   ####")),
    "common": (NET, ("", " ########", "", " ######", "", " #######")),
    "crafting": (ITEM, ("", "  # # #", "", "  # # # #", "", "  # # #")),
    "template": (CRAFT, (" ######", " #    #", " # ## #", " #    #", " ######")),
    "factoric": (CRAFT, ("", " ##   ##", " ######", " ##   ##", "", "    ##")),
}
#: 屏幕左上角的小白块：看得出屏幕在 Blockbench / 真机里有没有被转反、镜像
SCREEN_CORNER = (0xFF, 0xFF, 0xFF)
TRANSPARENT = (0, 0, 0, 0)

# -- 物品占位图标（16×16，只有 --icons 才写；配色走 gen_device_textures 的色标）---

#: 与样板、内存图标同一个目录；文件名 = 物品短名（同 `gen_items.item_texture_name`）
ICON_DIR = os.path.join(RP_ROOT, "textures", "endernet", "items")
ICON_SIZE = 16
ICON_OUTLINE = "k1"
#: 网络扳手的物品短名（13 §2）
WRENCH = "network_wrench"
#: 扳手：斜放的开口扳手，握柄用网络色。字符 -> 色标，`.` 透明
WRENCH_ART = (
    ".........###....",
    "........#LLL#...",
    ".......#LLL#....",
    "......#LLL#...#.",
    ".....#LLL#...#D#",
    ".....#LLLM#.#DD#",
    ".....#LLMMM#DDD#",
    ".....#LMMMDDDD#.",
    ".....#MMMDDDD#..",
    "....#gMMDDDD#...",
    "...#gGh#####....",
    "..#gGh#.........",
    ".#gGh#..........",
    "#gGh#...........",
    ".#h#............",
    "..#.............",
)
WRENCH_TOKENS = {
    "#": ICON_OUTLINE, "L": "k7", "M": "k6", "D": "k5", "g": "net0", "G": "net1", "h": "net2",
}


class CableModelError(Exception):
    pass


# -- 面板图集的 UV 分区 -----------------------------------------------------


class Region(object):
    def __init__(self, kind, variant, width, height, u, v):
        self.kind = kind
        #: 屏幕的面板种类（其余分区为 None）
        self.variant = variant
        self.width = width
        self.height = height
        self.u = u
        self.v = v


class Atlas(object):
    """一张贴图的 UV 分区（面板图集用；光缆走固定表 `REGIONS`）：按登记顺序从左到右、
    逐行摆放。

    同一种类、同样宽高的面共用一块（六块面板的框体侧边画一块就全有了）；横放与竖放
    尺寸不同，各占一块，免得拉伸。
    """

    def __init__(self, name, width):
        self.name = name
        self.width = width
        self.regions = []
        self._x = 0
        self._y = 0
        self._row = 0

    def add(self, kind, width, height, variant=None):
        width, height = num(width), num(height)
        if self.find(kind, width, height, variant) is not None:
            return
        cells_w, cells_h = int(math.ceil(width)), int(math.ceil(height))
        if cells_w > self.width:
            raise CableModelError(
                "%s 的%s分区宽 %s，超过贴图宽 %d" % (self.name, KINDS[kind][0], width, self.width)
            )
        if self._x + cells_w > self.width:
            self._x, self._y, self._row = 0, self._y + self._row, 0
        self.regions.append(Region(kind, variant, width, height, self._x, self._y))
        self._x += cells_w
        self._row = max(self._row, cells_h)

    def find(self, kind, width, height, variant=None):
        for region in self.regions:
            if (region.kind, region.variant, region.width, region.height) == (
                kind, variant, width, height
            ):
                return region
        return None

    @property
    def size(self):
        used, height = self._y + self._row, 16
        while height < used:
            height *= 2
        return self.width, height

    def face_uv(self, face, kind, width, height, variant=None, flip=None):
        """这一面的 UV；`flip` 为 `"u"` / `"v"` 时把那根贴图轴反过来（`edge_flip`）。"""
        region = self.find(kind, width, height, variant)
        if region is None:
            raise CableModelError(
                "%s 没有 %s×%s 的%s分区" % (self.name, width, height, KINDS[kind][0])
            )
        u, v, size_u, size_v = region.u, region.v, width, height
        if face in ("up", "down"):
            # Blockbench 导出上下两面时 UV 倒着写（起点在对角、尺寸取负）；照写，
            # 作者在 Blockbench 里看到的才是正的
            u, v, size_u, size_v = u + width, v + height, -width, -height
        if flip == "u":
            u, size_u = u + size_u, -size_u
        elif flip == "v":
            v, size_v = v + size_v, -size_v
        return {"uv": [num(u), num(v)], "uv_size": [num(size_u), num(size_v)]}


# -- 几何辅助 --------------------------------------------------------------


def axis_of(vector):
    for axis, component in enumerate(vector):
        if component:
            return axis
    raise ValueError(vector)


def face_name(vector):
    for name, candidate in DIRECTIONS:
        if tuple(candidate) == tuple(vector):
            return name
    raise ValueError(vector)


def opposite(vector):
    return tuple(-component for component in vector)


def side_faces(vector):
    """与 vector 垂直的四个面（面板框、背板画这四面）。"""
    axis = axis_of(vector)
    return [name for name, candidate in DIRECTIONS if axis_of(candidate) != axis]


def file_point(point, offset=(0, 0, 0)):
    """世界坐标点 -> 几何文件坐标：x 取反，再加偏移。"""
    x, y, z = point
    return [num(GEO_X * x + offset[0]), num(y + offset[1]), num(z + offset[2])]


def face_extent(face, size):
    """面在贴图上的宽高：南北面 x×y，东西面 z×y，上下面 x×z（与引擎展开一致）。"""
    sx, sy, sz = size
    if face in ("north", "south"):
        return sx, sy
    if face in ("east", "west"):
        return sz, sy
    return sx, sz


def cube(box, faces, atlas, offset=(0, 0, 0), variant=None):
    """世界坐标盒 -> 几何文件里的方块。只写 `faces`（面名 -> 分区种类，或 `(分区种类, 翻转)`）
    列出的面，其余不画。"""
    (x0, y0, z0), (x1, y1, z1) = box
    size = (x1 - x0, y1 - y0, z1 - z0)
    if min(size) <= 0:
        raise CableModelError("退化的方块 %r" % (box,))
    left = min(GEO_X * x0, GEO_X * x1)
    uv = {}
    for face in FACE_ORDER:
        if face in faces:
            kind, flip = faces[face] if isinstance(faces[face], tuple) else (faces[face], None)
            width, height = face_extent(face, size)
            uv[face] = atlas.face_uv(face, kind, num(width), num(height), variant, flip)
    return {
        "origin": [num(left + offset[0]), num(y0 + offset[1]), num(z0 + offset[2])],
        "size": [num(value) for value in size],
        "uv": uv,
    }


def geometry(name, size, bones, bounds):
    width, height = size
    bounds_width, bounds_height, bounds_offset = bounds
    return {
        "format_version": FORMAT_VERSION,
        "minecraft:geometry": [{
            "description": {
                "identifier": geometry_id(name),
                "texture_width": width,
                "texture_height": height,
                "visible_bounds_width": bounds_width,
                "visible_bounds_height": bounds_height,
                "visible_bounds_offset": list(bounds_offset),
            },
            "bones": bones,
        }],
    }


# -- 光缆（方块几何体）------------------------------------------------------


def stub_length(core):
    """短接线长度（`cable_shapes.stub_length`）；这种线芯放不下面板 + 背板就报错。"""
    length = shape_stub_length(core)
    if length <= 0:
        raise CableModelError(
            "线芯 %s 离方块面只剩 %s，放不下厚 %s 的面板 + 背板和短接线"
            % (num(core), num(arm_length(core)), num(STUB_FROM_EDGE))
        )
    return length


#: 光缆贴图的分区：`{光缆短名: {分区: (左上角 u, v, 宽, 高)}}`——作者重画时照这张表
#: （`--layout` 打印）。重画规则（`.claude/cable-art/README.md` §3.5）：
#:
#: 1. 侧面条带（`*_h` 横放、`*_v` 竖放，都是整 16 px 长）沿长度每列相同、只沿截面变化：
#:    每个侧面按它在格子里的位置取对应的那几列，UV 方向怎么定接缝都对得上；
#: 2. 截面方向左右对称（同一条带在不同朝向的面上可能被镜像）；
#: 3. 线芯空面 `hub` 的四条边各等于一份侧面截面（与连接臂接得上）；
#: 4. 玻璃填充 `pane_*` 的 alpha 决定有多透；它与不透明分区之间留 2 px 空白，防远处 mipmap 串色。
REGIONS = {
    "ender_cable": {
        "side_h": (0, 0, 16, 6),
        "side_v": (16, 0, 6, 16),
        "hub": (22, 0, 6, 6),
        "end": (22, 6, 6, 6),
        "plate": (28, 0, 4, 4),
        "plate_side_h": (28, 4, 4, 1),
        "plate_side_v": (28, 5, 1, 4),
    },
    "ender_dense_cable": {
        "side_h": (0, 0, 16, 10),
        "side_v": (16, 0, 10, 16),
        "hub": (0, 10, 10, 10),
        "end": (0, 20, 10, 10),
        "plate": (26, 0, 6, 6),
        "plate_side_h": (26, 6, 6, 1),
        "plate_side_v": (26, 7, 1, 6),
    },
    "ender_glass_cable": {
        "rod_h": (0, 0, 16, 1),
        "fiber_h": (0, 1, 16, 1),
        "pane_h": (0, 4, 16, 2),
        "rod_v": (16, 0, 1, 16),
        "fiber_v": (17, 0, 1, 16),
        "pane_v": (20, 0, 2, 16),
        "joint": (24, 0, 1, 1),
        "node": (25, 0, 1, 1),
        "cap": (24, 4, 2, 2),
    },
}
#: 分区的中文名（`--layout` 用）
REGION_LABELS = {
    "side_h": "侧面条带（横放）：直通、连接臂、短接线共用，按位置取列",
    "side_v": "侧面条带（竖放）",
    "hub": "线芯空面（端口盖板四周露出的一圈）",
    "end": "截面（连接臂 / 直通的外端）",
    "plate": "端口盖板正面",
    "plate_side_h": "端口盖板侧边（横放）",
    "plate_side_v": "端口盖板侧边（竖放）",
    "rod_h": "棱条（横放）",
    "rod_v": "棱条（竖放）",
    "fiber_h": "芯（横放）",
    "fiber_v": "芯（竖放）",
    "pane_h": "半透明填充（横放）",
    "pane_v": "半透明填充（竖放）",
    "joint": "线芯的框角",
    "node": "芯的交汇点",
    "cap": "空面的半透明封面",
}

# -- 光缆：配色与图案（取自 textures/endernet/devices/ 与作者手改的 ender_cable.png）-----

#: 不透明像素的色标
PAL = {
    # 外壳白（设备外框的 e3e4e6 / f3f5f8）、银灰
    "W": (0xE3, 0xE4, 0xE6), "w": (0xEA, 0xEC, 0xEF), "K": (0xC9, 0xCF, 0xD8),
    "k": (0xDD, 0xE2, 0xE8),
    # 作者手改贴图里的灰
    "G": (0x58, 0x5E, 0x69), "g": (0x64, 0x6B, 0x77), "h": (0x5F, 0x65, 0x6F),
    # 深石板（设备内框、作者线芯的深框）
    "a": (0x38, 0x42, 0x58), "b": (0x32, 0x39, 0x48), "s": (0x2C, 0x32, 0x3D),
    "S": (0x39, 0x40, 0x4E),
    # 网络青（作者线芯中心、基带 8 / 16）
    "C": (0x2F, 0xB7, 0xE0), "c": (0x19, 0x8B, 0xAD), "L": (0x7F, 0xE9, 0xFF),
    "l": (0x51, 0xD5, 0xFD), "d": (0x1A, 0x54, 0x6B), "e": (0x1D, 0x5A, 0x72),
    # 致密：基带 32 的紫
    "P": (0xB4, 0x60, 0xFF), "p": (0x58, 0x34, 0x99), "q": (0x46, 0x29, 0x7A),
    "r": (0x32, 0x24, 0x36),
}
#: 玻璃填充（RGBA）：管身淡青、约 28% 不透明（透得出芯）；空面的封面磨砂一些、带一颗反光
GLASS_PAL = {
    "1": (0xD2, 0xF4, 0xFF, 0x8C),
    "2": (0xA8, 0xE4, 0xF4, 0x48),
    "3": (0xFF, 0xFF, 0xFF, 0xC0),
}
#: 侧面截面（沿宽度的一列，从一条棱到另一条棱；**左右对称**，沿长度每列相同）
SIDE_PROFILE = {
    # 作者手改版：白棱 / 灰 / 2 px 青色光道 / 灰 / 白棱
    "ender_cable": "WGCCGW",
    # 致密：白棱 / 灰 / 两条紫色光道（暗边 + 亮芯）/ 深石板 / … 对称
    "ender_dense_cable": "WGpPSSPpGW",
}
#: 端口盖板正面：作者线芯里那圈深框 + 中间一颗灯（只亮一个像素）
PLATE_ART = {
    "ender_cable": ("abba",
                    "bCdb",
                    "bedb",
                    "abba"),
    "ender_dense_cable": ("abbbba",
                          "brrrrb",
                          "brPprb",
                          "brpqrb",
                          "brrrrb",
                          "abbbba"),
}
PLATE_SIDE = "g"
#: 截面（连接臂 / 直通的外端）：外圈银灰「切口」，往里灰，中心是光道的截面
END_ART = {
    "ender_cable": ("KKKKKK",
                    "KGGGGK",
                    "KGCCGK",
                    "KGCCGK",
                    "KGGGGK",
                    "KKKKKK"),
    "ender_dense_cable": ("KKKKKKKKKK",
                          "KGGGGGGGGK",
                          "KGssssssGK",
                          "KGsPpqPsGK",
                          "KGspSSpsGK",
                          "KGsqSSqsGK",
                          "KGsPqpPsGK",
                          "KGssssssGK",
                          "KGGGGGGGGK",
                          "KKKKKKKKKK"),
}
#: 侧面条带里算「光道」的色标：线芯空面的内圈在光道穿过处接上光道
LANE_TOKENS = "CPp"


# -- 光缆：贴图 --------------------------------------------------------------


def cable_texture_size(cable):
    """光缆贴图的宽高：宽 `CABLE_TEXTURE_WIDTH`，高取放得下 `REGIONS` 的 2 的幂、至少 16。"""
    used = max(v + h for _u, v, _w, h in REGIONS[cable.name].values())
    height = 16
    while height < used:
        height *= 2
    return CABLE_TEXTURE_WIDTH, height


def blank(size):
    width, height = size
    return [[TRANSPARENT for _x in range(width)] for _y in range(height)]


def put(rows, x, y, color):
    rows[y][x] = color if len(color) == 4 else tuple(color) + (255,)


def paint_art(rows, region, art):
    """把图案（每行一个字符串，字符是 `PAL` / `GLASS_PAL` 的色标）画进分区。"""
    u, v, w, h = region
    if len(art) != h or any(len(line) != w for line in art):
        raise CableModelError("图案 %r 与分区 %r 的大小对不上" % (art, region))
    for dy, line in enumerate(art):
        for dx, token in enumerate(line):
            put(rows, u + dx, v + dy, GLASS_PAL[token] if token in GLASS_PAL else PAL[token])


def strip_art(profile, length, along):
    """沿长度每列相同的条带：along='u' 时横放（行 = 截面），'v' 时竖放。"""
    if along == "u":
        return tuple(token * length for token in profile)
    return tuple(profile for _ in range(length))


def hub_art(profile, inset):
    """线芯空面：四条边各是一份侧面截面（与连接臂接得上），往里一圈在光道处接进盖板，
    盖板底下填深色（被盖住，看不见）。"""
    n = len(profile)
    lanes = set(i for i, token in enumerate(profile) if token in LANE_TOKENS)
    art = []
    for r in range(n):
        line = []
        for c in range(n):
            edge_r, edge_c = r in (0, n - 1), c in (0, n - 1)
            if edge_r:
                line.append(profile[c])
            elif edge_c:
                line.append(profile[r])
            elif min(r, c, n - 1 - r, n - 1 - c) < inset:
                # 盖板外露的内圈（致密才有）：光道穿过处接上光道，其余深色
                if (r in (1, n - 2) and c in lanes) or (c in (1, n - 2) and r in lanes):
                    line.append(profile[c] if r in (1, n - 2) else profile[r])
                else:
                    line.append("s")
            else:
                line.append("b")
        art.append("".join(line))
    return tuple(art)


def build_texture(cable):
    """一种光缆的贴图（RGBA 行），分区照 `REGIONS`。"""
    rows = blank(cable_texture_size(cable))
    regions = REGIONS[cable.name]
    if cable.glass:
        paint_art(rows, regions["rod_h"], strip_art("K", 16, "u"))
        paint_art(rows, regions["rod_v"], strip_art("K", 16, "v"))
        paint_art(rows, regions["fiber_h"], strip_art("C", 16, "u"))
        paint_art(rows, regions["fiber_v"], strip_art("C", 16, "v"))
        paint_art(rows, regions["pane_h"], strip_art("22", 16, "u"))
        paint_art(rows, regions["pane_v"], strip_art("22", 16, "v"))
        paint_art(rows, regions["joint"], ("K",))
        paint_art(rows, regions["node"], ("L",))
        paint_art(rows, regions["cap"], ("31", "12"))
        return rows
    profile = SIDE_PROFILE[cable.name]
    paint_art(rows, regions["side_h"], strip_art(profile, 16, "u"))
    paint_art(rows, regions["side_v"], strip_art(profile, 16, "v"))
    paint_art(rows, regions["hub"], hub_art(profile, cable.plate_inset))
    paint_art(rows, regions["end"], END_ART[cable.name])
    paint_art(rows, regions["plate"], PLATE_ART[cable.name])
    plate = cable.core - 2 * cable.plate_inset
    paint_art(rows, regions["plate_side_h"], (PLATE_SIDE * plate,))
    paint_art(rows, regions["plate_side_v"], tuple(PLATE_SIDE for _ in range(plate)))
    return rows


# -- 光缆：几何体 ------------------------------------------------------------

#: 面在贴图上的展开方向（世界坐标；照 Blockbench 显示：从外面看，u 向右、v 向下）：
#: 面 -> (u 轴, u 方向, v 轴, v 方向)。上下两面在文件里按 Blockbench 导出习惯写负尺寸
#: （`face_uv` 写出时换算）。
DISPLAY = {
    "north": (0, -1, 1, -1),
    "south": (0, 1, 1, -1),
    "east": (2, -1, 1, -1),
    "west": (2, 1, 1, -1),
    "up": (0, 1, 2, 1),
    "down": (0, 1, 2, -1),
}


def world_box(ranges):
    """`{轴: (lo, hi)}`（相对方块中心）→ 世界盒（相对方块底面中心）。"""
    lo = [ranges[axis][0] for axis in range(3)]
    hi = [ranges[axis][1] for axis in range(3)]
    lo[1] += CENTER_Y
    hi[1] += CENTER_Y
    return tuple(lo), tuple(hi)


def local(axis, value):
    """世界坐标（相对方块底面中心）→ 沿该轴的格内位置 0～16。"""
    return value if axis == 1 else value + HALF


class CubeSpec(object):
    """几何体里的一个方块：世界盒 + 要画的面（`{面: (取图方式, 参数, 材质实例或 None)}`，
    取图方式见 `strip_uv` / `region_uv`）。零厚度的盒只画朝外的那一面。"""

    def __init__(self, ranges, faces):
        self.box = world_box(ranges)
        self.faces = faces


def side_faces_of(axis):
    return [face for face in FACING_NAMES if face_axis(face)[0] != axis]


def strip_uv(family, axis, material=None):
    """侧面取条带 `<family>_h` / `_v`（长度沿 axis），按这个面在格子里的位置取列。"""
    return ("strip", (family, axis), material)


def region_uv(key, material=None, stretch=False, edge=False):
    """整面取一块固定分区：`key`、`key_h`、`key_v` 里宽高正好对得上的那块。

    `stretch`：宽高对不上也整块贴上去（拉伸）——作者在 Blockbench 里缩致密线芯时留下的贴法，
    照写；`edge`：只取分区的边（这一面横长取上沿一行，竖长取右沿一列），同样拉伸。
    """
    if edge:
        return ("edge", key, material)
    return ("stretch" if stretch else "region", key, material)


def face_uv(cable, face, spec, cube_box):
    kind, arg, material = spec
    regions = REGIONS[cable.name]
    lo, hi = cube_box
    u_axis, u_dir, v_axis, v_dir = DISPLAY[face]
    extent_u = hi[u_axis] - lo[u_axis]
    extent_v = hi[v_axis] - lo[v_axis]
    if kind == "strip":
        family, axis = arg
        p0, p1 = local(axis, lo[axis]), local(axis, hi[axis])
        if u_axis == axis:
            ru, rv, rw, rh = regions[family + "_h"]
            start = p0 if u_dir > 0 else 2 * HALF - p1
            if extent_v != rh:
                raise CableModelError("%s %s 面截面 %s ≠ 分区 %s" % (family, face, extent_v, rh))
            uv, size = (ru + start, rv), (p1 - p0, rh)
        elif v_axis == axis:
            ru, rv, rw, rh = regions[family + "_v"]
            start = p0 if v_dir > 0 else 2 * HALF - p1
            if extent_u != rw:
                raise CableModelError("%s %s 面截面 %s ≠ 分区 %s" % (family, face, extent_u, rw))
            uv, size = (ru, rv + start), (rw, p1 - p0)
        else:
            raise CableModelError("%s 面不沿 %s 轴" % (face, axis))
    elif kind in ("stretch", "edge"):
        if arg not in regions:
            raise CableModelError("%s 没有 %s 分区" % (cable.name, arg))
        ru, rv, rw, rh = regions[arg]
        if kind == "edge" and extent_u > extent_v:
            rh = 1
        elif kind == "edge" and extent_u < extent_v:
            ru, rw = ru + rw - 1, 1
        elif kind == "edge":
            raise CableModelError("%s 面 %s×%s 分不出取哪条边" % (face, extent_u, extent_v))
        uv, size = (ru, rv), (rw, rh)
    else:
        for key in (arg, arg + "_h", arg + "_v"):
            if key in regions and tuple(regions[key][2:]) == (extent_u, extent_v):
                ru, rv, rw, rh = regions[key]
                break
        else:
            raise CableModelError("%s 没有 %s×%s 的 %s 分区" % (cable.name, extent_u, extent_v, arg))
        uv, size = (ru, rv), (rw, rh)
    uv = [num(uv[0]), num(uv[1])]
    size = [num(size[0]), num(size[1])]
    if face in ("up", "down"):
        # 照 Blockbench 导出：上下两面起点在对角、尺寸取负，作者在 Blockbench 里看到的才是正的
        uv = [num(uv[0] + size[0]), num(uv[1] + size[1])]
        size = [num(-size[0]), num(-size[1])]
    out = {"uv": uv, "uv_size": size}
    if material:
        out["material_instance"] = material
    return out


def cube_json(cable, spec):
    (x0, y0, z0), (x1, y1, z1) = spec.box
    uv = {}
    for face in FACE_ORDER:
        if face in spec.faces:
            uv[face] = face_uv(cable, face, spec.faces[face], spec.box)
    return {
        "origin": [num(min(GEO_X * x0, GEO_X * x1)), num(y0), num(z0)],
        "size": [num(x1 - x0), num(y1 - y0), num(z1 - z0)],
        "uv": uv,
    }


def along_ranges(axis, near, far, cross):
    """沿 axis 从 near 到 far（可为负）、另两轴各取 cross[i] 的范围。"""
    ranges = {}
    others = [a for a in range(3) if a != axis]
    ranges[axis] = (min(near, far), max(near, far))
    ranges[others[0]] = cross[0]
    ranges[others[1]] = cross[1]
    return ranges


def plane_ranges(axis, at, cross):
    return along_ranges(axis, at, at, cross)


def tube_solid(cable, axis, near, far, end_faces=()):
    """不透明光缆的一段管子：四个侧面取侧面条带，end_faces 里的面画截面。"""
    h = cable.half
    faces = dict((face, strip_uv("side", axis)) for face in side_faces_of(axis))
    for face in end_faces:
        faces[face] = region_uv("end")
    return CubeSpec(along_ranges(axis, near, far, ((-h, h), (-h, h))), faces)


def glass_tube(cable, axis, near, far, fiber_near=None, pane_near=None):
    """玻璃的一段：(实心部分 cubes, 填充面 cubes)。实心 = 芯 + 四根棱条，填充 = 四块半透明面。

    `fiber_near` / `pane_near`：芯 / 填充面从哪里起（默认与棱条同在 `near`）。"""
    h = cable.half
    solid = []
    lo_f = fiber_near if fiber_near is not None else near
    lo_p = pane_near if pane_near is not None else near
    solid.append(CubeSpec(along_ranges(axis, lo_f, far, ((-0.5, 0.5), (-0.5, 0.5))),
                          dict((face, strip_uv("fiber", axis, MATERIAL_FIBER))
                               for face in side_faces_of(axis))))
    corners = ((h - 1, h), (-h, -h + 1))
    for first in corners:
        for second in corners:
            solid.append(CubeSpec(along_ranges(axis, near, far, (first, second)),
                                  dict((face, strip_uv("rod", axis)) for face in side_faces_of(axis))))
    panes = []
    for face in side_faces_of(axis):
        normal_axis, sign = face_axis(face)
        ranges = {axis: (min(lo_p, far), max(lo_p, far)), normal_axis: (sign * h, sign * h)}
        third = [a for a in range(3) if a not in ranges][0]
        ranges[third] = (-h + 1, h - 1)
        panes.append(CubeSpec(ranges, {face: strip_uv("pane", axis, MATERIAL_GLASS)}))
    return solid, panes


def corner_range(cable, face):
    """玻璃线芯靠 `face` 那一侧 1 px 厚的一层（沿 face 的轴）。"""
    h = cable.half
    return (h - 1, h) if face_axis(face)[1] > 0 else (-h, -h + 1)


def opaque_cubes(cable, bone):
    """不透明光缆一根骨骼的方块（`cable_shapes.cable_bones` 的一项）。"""
    h = cable.half
    if bone.role == ROLE_STRAIGHT:
        return [tube_solid(cable, bone.axis, -HALF, HALF, AXIS_FACES[bone.axis])]
    face = bone.faces[0]
    axis, sign = face_axis(face)
    # 线芯外观的半边长：多数就是 h；致密缩进 1 px（`CORE_INSET`）
    inner = h - cable.core_inset
    if bone.role == ROLE_ARM:
        arm = tube_solid(cable, axis, sign * h, sign * HALF, (face,))
        if not cable.core_inset:
            return [arm]
        # 线芯缩进了：臂的内端面露出一圈，画线芯空面；线芯面到臂之间补一块过渡（截面同线芯外观），
        # 侧面取线芯空面的边、朝线芯那面取盖板——都照作者在 Blockbench 里的拉伸贴法
        inward = opposite_face(face)
        arm.faces[inward] = region_uv("hub")
        collar_faces = dict((other, region_uv("hub", edge=True)) for other in side_faces_of(axis))
        collar_faces[inward] = region_uv("plate", stretch=True)
        collar = CubeSpec(along_ranges(axis, sign * inner, sign * h,
                                       ((-inner, inner), (-inner, inner))), collar_faces)
        return [arm, collar]
    if bone.role == ROLE_STUB:
        return [tube_solid(cable, axis, sign * h, sign * STUB_END)]
    if bone.role == ROLE_CAP:
        # 线芯的这一面（零厚度，四边是侧面截面）+ 凸起 PLATE_DEPTH 的端口盖板；线芯缩进时两块
        # 一起往里挪，线芯面照作者的贴法把整块 `hub` 拉伸上去
        plate = h - cable.plate_inset
        hub_face = CubeSpec(plane_ranges(axis, sign * inner, ((-inner, inner), (-inner, inner))),
                            {face: region_uv("hub", stretch=bool(cable.core_inset))})
        plate_faces = dict((other, region_uv("plate_side")) for other in side_faces_of(axis))
        plate_faces[face] = region_uv("plate")
        plate_cube = CubeSpec(along_ranges(axis, sign * inner, sign * (inner + PLATE_DEPTH),
                                           ((-plate, plate), (-plate, plate))), plate_faces)
        return [hub_face, plate_cube]
    raise CableModelError("不透明光缆没有 %s 这种骨骼（%s）" % (bone.role, bone.name))


def glass_cubes(cable, bone):
    """玻璃光缆一根骨骼的方块。管身（直通、连接臂）的实心（芯、棱条）与半透明填充面在同一根
    骨骼里，**实心在前**（同一格里先画框和芯、后画填充面）。"""
    h = cable.half
    if bone.role == ROLE_STRAIGHT:
        solid, panes = glass_tube(cable, bone.axis, -HALF, HALF)
        return solid + panes
    if bone.role == ROLE_HUB:
        # 芯的交汇点 + 八个框角（非直通时常显）
        corners = ((h - 1, h), (-h, -h + 1))
        cubes = [CubeSpec({0: (-0.5, 0.5), 1: (-0.5, 0.5), 2: (-0.5, 0.5)},
                          dict((face, region_uv("node", MATERIAL_FIBER)) for face in FACING_NAMES))]
        for rx in corners:
            for ry in corners:
                for rz in corners:
                    cubes.append(CubeSpec({0: rx, 1: ry, 2: rz},
                                          dict((face, region_uv("joint")) for face in FACING_NAMES)))
        return cubes
    if bone.role == ROLE_EDGE:
        # 线芯的棱：相邻两面都空着才是真实的棱
        first, second = bone.faces
        along = 3 - face_axis(first)[0] - face_axis(second)[0]
        ranges = {face_axis(first)[0]: corner_range(cable, first),
                  face_axis(second)[0]: corner_range(cable, second),
                  along: (-h + 1, h - 1)}
        return [CubeSpec(ranges, dict((face, strip_uv("rod", along)) for face in side_faces_of(along)))]
    if bone.role == ROLE_ARM:
        # 连接臂（兼管挂面板的面，玻璃没有短接线）：棱条从线芯面画到方块面，挂着面板时多出的
        # 那段落在面板实体的背板与面板框里面、看不见；芯从交汇点起。填充面往线芯里多铺 1 px：
        # 线芯上与这条臂相邻的空面，靠臂那一条（样稿的 `skirt_<空面>_<臂>`）就是它
        face = bone.faces[0]
        axis, sign = face_axis(face)
        solid, panes = glass_tube(cable, axis, sign * h, sign * HALF, fiber_near=sign * 0.5,
                                  pane_near=sign * (h - 1))
        return solid + panes
    if bone.role == ROLE_CAP:
        # 空面：中间 2×2 的半透明封面
        face = bone.faces[0]
        axis, sign = face_axis(face)
        return [CubeSpec(plane_ranges(axis, sign * h, ((-h + 1, h - 1), (-h + 1, h - 1))),
                         {face: region_uv("cap", MATERIAL_GLASS)})]
    raise CableModelError("玻璃光缆没有 %s 这种骨骼（%s）" % (bone.role, bone.name))


def opaque_bones(cable):
    """`[(骨骼, [CubeSpec])]`：骨骼清单与顺序取 `cable_shapes.cable_bones`。"""
    return [(bone, opaque_cubes(cable, bone)) for bone in cable_bones(cable.device_id, cable.panels)]


def glass_bones(cable):
    """同上（玻璃：每根骨骼里实心方块在前、填充面在后；空面封面的骨骼排在最后）。"""
    return [(bone, glass_cubes(cable, bone)) for bone in cable_bones(cable.device_id, cable.panels)]


def build_bones(cable):
    return glass_bones(cable) if cable.glass else opaque_bones(cable)


def bone_pivot(cable, bone):
    """骨骼的轴心（文件坐标）：方块中心。线芯缩进的光缆，空面骨骼的轴心随线芯面往里挪
    `core_inset`——作者在 Blockbench 里整组挪过去的位置；骨骼不转，轴心不影响画面。"""
    pivot = [0, CENTER_Y, 0]
    if bone.role == ROLE_CAP and cable.core_inset:
        axis, sign = face_axis(bone.faces[0])
        pivot[axis] -= sign * cable.core_inset * (GEO_X if axis == 0 else 1)
    return [num(value) for value in pivot]


def build_cable(cable):
    bones = [
        {"name": bone.name, "pivot": bone_pivot(cable, bone),
         "cubes": [cube_json(cable, spec) for spec in cubes]}
        for bone, cubes in build_bones(cable)
    ]
    return geometry(cable.name, cable_texture_size(cable), bones, BLOCK_BOUNDS)


# -- 面板（实体几何体 + 方块实体外观）----------------------------------------


def screen_variants():
    """屏幕的种类：`default` 在前，其余按种类表顺序（与槽位模块一样）。"""
    return [DEFAULT_PANEL] + [kind for kind, _device in PANEL_KINDS]


def panel_variable(index):
    """第 `index` 面（`FACING_NAMES` 的下标）的 molang 变量名，照 `slot_variable` 的写法。"""
    return "variable.p%d" % (index,)


def panel_value(variant):
    """某种面板的变量值：`default` = UNKNOWN，第 i 种 = FIRST_MODEL + i。"""
    if variant == DEFAULT_PANEL:
        return UNKNOWN
    return FIRST_MODEL + [kind for kind, _device in PANEL_KINDS].index(variant)


def frame_bone(face):
    return "frame_" + face


def panel_bone(face, variant):
    return "panel_%s_%s" % (face, variant)


def panel_atlas(name):
    atlas = Atlas(name, PANEL_TEXTURE_WIDTH)
    for variant in screen_variants():
        atlas.add("screen", PANEL_SIZE, PANEL_SIZE, variant=variant)
    atlas.add("frame_back", PANEL_SIZE, PANEL_SIZE)
    atlas.add("frame_edge", PANEL_SIZE, PANEL_DEPTH)
    atlas.add("frame_edge", PANEL_DEPTH, PANEL_SIZE)
    atlas.add("back_face", BACK_SIZE, BACK_SIZE)
    atlas.add("back_edge", BACK_SIZE, BACK_DEPTH)
    atlas.add("back_edge", BACK_DEPTH, BACK_SIZE)
    return atlas


def edge_flip(face, vector):
    """贴在 `vector` 那面的面板，框的侧边 `face` 要不要翻：条带一律从背面（靠宿主）画到正面
    （屏幕那侧）——作者 2026-11-10 重画的图集里深色一行 / 一列在背面，并在面板物品里把反了的两个
    侧边翻了过来。这一面在贴图上沿面板法线的那根轴（`DISPLAY`）与法线反向，就翻那根轴。"""
    axis = axis_of(vector)
    u_axis, u_dir, v_axis, v_dir = DISPLAY[face]
    if u_axis == axis and u_dir * vector[axis] < 0:
        return "u"
    if v_axis == axis and v_dir * vector[axis] < 0:
        return "v"
    return None


def panel_shell_faces(vector):
    """贴在 `vector` 那面的面板框与背板各画哪几面：`(面板框, 背板)`，面名 -> 分区种类（框的
    侧边带上翻转，`edge_flip`）。

    朝外的正面不画（由屏幕补上），背板贴着面板框的那面不画。实体外观与物品形态共用。
    """
    inward = face_name(opposite(vector))
    frame = dict((face, ("frame_edge", edge_flip(face, vector))) for face in side_faces(vector))
    frame[inward] = "frame_back"
    back = dict.fromkeys(side_faces(vector), "back_edge")
    back[inward] = "back_face"
    return frame, back


def panel_bones(face, vector, atlas):
    """贴在 `face` 面的一组骨骼（实体坐标）：框体（面板框 + 背板）一根，
    其下每种面板一根只有屏幕面的子骨骼。"""
    outward = face
    frame, back = panel_shell_faces(vector)
    pivot = file_point(face_center(vector), ENTITY_ORIGIN)
    bones = [{"name": frame_bone(face), "pivot": pivot, "cubes": [
        cube(frame_box(vector), frame, atlas, ENTITY_ORIGIN),
        cube(back_box(vector), back, atlas, ENTITY_ORIGIN),
    ]}]
    screen = screen_box(vector)
    for variant in screen_variants():
        bones.append({"name": panel_bone(face, variant), "parent": frame_bone(face),
                      "pivot": list(pivot), "cubes": [
                          cube(screen, {outward: "screen"}, atlas, ENTITY_ORIGIN, variant),
                      ]})
    return bones


def build_panels(atlas):
    bones = []
    for face, vector in DIRECTIONS:
        bones.extend(panel_bones(face, vector, atlas))
    return geometry(PANELS, atlas.size, bones, ENTITY_BOUNDS)


def build_single_panel(atlas):
    """6 面几何里北面那组骨骼原样抽出（屏幕骨骼彼此重合，画哪种就只留哪根可见）。"""
    north = dict(DIRECTIONS)["north"]
    return geometry(PANEL, atlas.size, panel_bones("north", north, atlas), ENTITY_BOUNDS)


# -- 物品形态（2026-11-10 作者定：物品栏里显示 3D）---------------------------------

#: 光缆物品显示的直通段长度（px）：照 AE2 光缆物品（`item/*_cable_base.json` 沿轴 2..14），
#: 两端各让出 2 px，物品栏格子里不顶边。
CABLE_ITEM_LENGTH = 12
#: 光缆物品的轴：东西向（同蔚蓝 `general_wire_item`），物品栏的斜视角里斜着放。
CABLE_ITEM_AXIS = 0
#: 面板物品的屏幕朝哪面：物品栏镜头看的是方块的**南面**（11-05 真机：物品几何转 180° 后
#: 图标才对；蔚蓝电池矩阵的 item_visual 也把正面画在 south）。
PANEL_ITEM_FACE = "south"


def cable_item_name(cable):
    """光缆物品几何的名字 `<光缆短名>_item`（`gen_blocks` 的 `minecraft:item_visual` 按同名指向它）。"""
    return cable.name + "_item"


def cable_item_cubes(cable):
    """光缆物品的一段直管：方块形态的直通段截成 `CABLE_ITEM_LENGTH`，两端封口。

    不透明光缆两端画截面分区 `end`（与方块形态的直通段同一块贴图）。玻璃光缆方块形态的
    直通段两端敞开（总有更粗的光缆或设备挡着），物品形态两端露在外面，整个截面要封满：
    四角是棱条的端面（分区 `joint`，与线芯框角同一块），中间 2×2 封一块半透明面（分区
    `cap`，与空面封面同一块），棱条之间的四条 2×1 缝用填充面的条带补（`pane`，截面正好
    2 px）。几块互不重叠；芯的端面落在封面后面，不另画，免得与封面共面闪烁。
    """
    axis = CABLE_ITEM_AXIS
    far = CABLE_ITEM_LENGTH / 2.0
    ends = AXIS_FACES[axis]
    if not cable.glass:
        return [tube_solid(cable, axis, -far, far, ends)]
    solid, panes = glass_tube(cable, axis, -far, far)
    for rod in solid[1:]:  # solid[0] 是芯，其后四根是棱条（`glass_tube`）
        for face in ends:
            rod.faces[face] = region_uv("joint")
    h = cable.half
    inner = (-h + 1, h - 1)
    edges = ((h - 1, h), (-h, -h + 1))
    across = [a for a in range(3) if a != axis]
    caps = []
    for face in ends:
        at = face_axis(face)[1] * far
        caps.append(CubeSpec(plane_ranges(axis, at, (inner, inner)),
                             {face: region_uv("cap", MATERIAL_GLASS)}))
        for thin, wide in (across, across[::-1]):
            for edge in edges:
                caps.append(CubeSpec({axis: (at, at), thin: edge, wide: inner},
                                     {face: strip_uv("pane", thin, MATERIAL_GLASS)}))
    return solid + panes + caps


def build_cable_item(cable):
    bones = [{"name": "item", "pivot": [0, CENTER_Y, 0],
              "cubes": [cube_json(cable, spec) for spec in cable_item_cubes(cable)]}]
    return geometry(cable_item_name(cable), cable_texture_size(cable), bones, BLOCK_BOUNDS)


def panel_item_name(kind):
    """面板物品几何的名字 = 面板物品短名（`gen_blocks` 的面板展示方块按同名取几何）。"""
    return part_item(kind)


def panel_item_offset(vector):
    """把贴在 `vector` 那面的面板（面板框 + 背板 + 屏幕）整体挪到方块中心的文件坐标偏移。"""
    axis = axis_of(vector)
    boxes = (frame_box(vector), back_box(vector), screen_box(vector))
    low = min(box[0][axis] for box in boxes)
    high = max(box[1][axis] for box in boxes)
    middle = CENTER_Y if axis == 1 else 0
    offset = [0, 0, 0]
    offset[axis] = (middle - (low + high) / 2.0) * (GEO_X if axis == 0 else 1)
    return tuple(offset)


def build_panel_item(kind, atlas):
    """一种面板的物品几何：一根骨骼，面板框 + 背板 + 这一种的屏幕，挪到方块中心、屏幕朝南。"""
    vector = dict(DIRECTIONS)[PANEL_ITEM_FACE]
    frame, back = panel_shell_faces(vector)
    offset = panel_item_offset(vector)
    bones = [{"name": "panel", "pivot": [0, CENTER_Y, 0], "cubes": [
        cube(frame_box(vector), frame, atlas, offset),
        cube(back_box(vector), back, atlas, offset),
        cube(screen_box(vector), {PANEL_ITEM_FACE: "screen"}, atlas, offset, kind),
    ]}]
    return geometry(panel_item_name(kind), atlas.size, bones, BLOCK_BOUNDS)


def visibility_rules():
    """`[(骨骼名, molang)]`：有面板就画框体，且每面恰好一块屏幕可见。

    与 `gen_slot_models.visibility_rules` 同一规则：`default` 管 1 与种类表以外的值。
    """
    specific = [panel_value(kind) for kind, _device in PANEL_KINDS]
    rules = []
    for index, (face, _vector) in enumerate(DIRECTIONS):
        variable = panel_variable(index)
        present = "%s >= %d" % (variable, UNKNOWN)
        rules.append((frame_bone(face), present))
        rules.append((panel_bone(face, DEFAULT_PANEL), " && ".join(
            [present] + ["%s != %d" % (variable, value) for value in specific])))
        for kind, _device in PANEL_KINDS:
            rules.append((panel_bone(face, kind), "%s == %d" % (variable, panel_value(kind))))
    return rules


def build_controller():
    return {
        "format_version": "1.8.0",
        "render_controllers": {
            CONTROLLER_ID: {
                "geometry": "Geometry.default",
                "materials": [{"*": "Material.default"}],
                "textures": ["Texture.default"],
                "part_visibility": [{bone: expression} for bone, expression in visibility_rules()],
            }
        },
    }


def build_entity(probe=False):
    initial = [
        (panel_variable(index), panel_value(PROBE[face]) if probe else EMPTY)
        for index, (face, _vector) in enumerate(DIRECTIONS)
    ]
    return {
        "format_version": "1.10.0",
        "minecraft:client_entity": {
            "description": {
                "identifier": ENTITY_ID,
                "materials": {"default": MATERIAL},
                "textures": {"default": PANEL_TEXTURE_DIR + "/" + PANELS},
                "geometry": {"default": geometry_id(PANELS)},
                "scripts": {"initialize": ["%s = %d;" % pair for pair in initial]},
                "render_controllers": [CONTROLLER_ID],
            }
        },
    }


# -- 面板图集的占位贴图 ------------------------------------------------------


def fill(rows, x0, y0, x1, y1, rgb):
    for y in range(max(0, y0), min(len(rows), y1)):
        for x in range(max(0, x0), min(len(rows[0]), x1)):
            rows[y][x] = tuple(rgb) + (255,)


def paint(rows, region):
    """色块 + 1 px 描边（太窄的只填色）；屏幕画该种类色系的顶条、屏内示意与左上角白块。"""
    _label, body, edge = KINDS[region.kind]
    x0, y0 = int(region.u), int(region.v)
    x1 = int(math.ceil(region.u + region.width))
    y1 = int(math.ceil(region.v + region.height))
    width, height = x1 - x0, y1 - y0
    fill(rows, x0, y0, x1, y1, body)
    if min(width, height) >= 3:
        fill(rows, x0, y0, x1, y0 + 1, edge)
        fill(rows, x0, y1 - 1, x1, y1, edge)
        fill(rows, x0, y0, x0 + 1, y1, edge)
        fill(rows, x1 - 1, y0, x1, y1, edge)
    if region.kind == "screen" and min(width, height) >= 5:
        tones, glyph = SCREENS.get(region.variant, SCREENS[DEFAULT_PANEL])
        fill(rows, x0 + 1, y0 + 1, x1 - 1, y0 + 2, tones[0])
        for dy, line in enumerate(glyph):
            for dx, mark in enumerate(line):
                if mark == "#" and x0 + 1 + dx < x1 - 1 and y0 + 3 + dy < y1 - 1:
                    fill(rows, x0 + 1 + dx, y0 + 3 + dy, x0 + 2 + dx, y0 + 4 + dy, tones[1])
        fill(rows, x0 + 1, y0 + 1, x0 + 3, y0 + 3, SCREEN_CORNER)


def placeholder(atlas):
    width, height = atlas.size
    rows = [[TRANSPARENT] * width for _ in range(height)]
    for region in atlas.regions:
        paint(rows, region)
    return rows


# -- 物品占位图标 ----------------------------------------------------------


def part_item(kind):
    """挂在光缆上的面板物品短名（13 §2）。"""
    return kind + "_terminal_part"


def wrench_icon(tone):
    return [[TRANSPARENT if mark == "." else tone(WRENCH_TOKENS[mark]) for mark in line]
            for line in WRENCH_ART]


def icon_file(name):
    return os.path.join(ICON_DIR, name + ".png")


def registered_icon_names():
    """物品图集（`item_texture.json`）里登记了的物品贴图名（不含扩展名）。

    `--icons` 只补这些：物品贴图目录里不许有没登记的图（`test_no_unreferenced_item_textures`），
    登记是 `gen_items.py` 的事。读图集的函数照用它的，不另写一份。
    """
    from gen_items import RP_ITEM_TEXTURE, read_json

    prefix = "textures/endernet/items/"
    names = set()
    for entry in read_json(RP_ITEM_TEXTURE).get("texture_data", {}).values():
        path = entry.get("textures", "")
        if isinstance(path, str) and path.startswith(prefix):
            names.add(path[len(prefix):])
    return names


def icon_outputs():
    """`[(路径, 像素)]`：扳手的占位图标（光缆、面板在物品栏里显示 3D，不画图标）。

    调色板与样板、内存图标同一套（`gen_device_textures`），只在画图标时才导入。
    """
    from gen_device_textures import DEFAULT_THEME, resolve, rgba

    def tone(token):
        return rgba(resolve(token, DEFAULT_THEME))

    return [(icon_file(WRENCH), wrench_icon(tone))]


# -- 输出 ------------------------------------------------------------------


def block_file(name):
    return os.path.join(RP_ROOT, "models", "blocks", name + ".geo.json")


def entity_geometry_file(name):
    return os.path.join(RP_ROOT, "models", "entity", name + ".geo.json")


def texture_file(directory, name):
    return os.path.join(RP_ROOT, *(directory + "/" + name + ".png").split("/"))


def json_bytes(payload):
    """UTF-8、CRLF（与仓库检出一致；比对时 `same_content` 不计换行差异）。"""
    return plural_dumps(payload).replace("\n", "\r\n").encode("utf-8")


def png_size(path):
    with open(path, "rb") as handle:
        header = handle.read(24)
    return struct.unpack(">II", header[16:24])


def validate_regions(cable):
    """`REGIONS` 里这种光缆的分区都在贴图宽度之内、互不重叠。"""
    if cable.name not in REGIONS:
        raise CableModelError("REGIONS 没给%s分贴图区" % cable.label)
    items = sorted(REGIONS[cable.name].items())
    for key, (u, v, w, h) in items:
        if u < 0 or v < 0 or w <= 0 or h <= 0 or u + w > CABLE_TEXTURE_WIDTH:
            raise CableModelError("%s 的分区 %s 超出贴图宽 %d" % (cable.name, key, CABLE_TEXTURE_WIDTH))
    for index, (key, (u, v, w, h)) in enumerate(items):
        for other, (ou, ov, ow, oh) in items[index + 1:]:
            if u < ou + ow and ou < u + w and v < ov + oh and ov < v + h:
                raise CableModelError("%s 的分区 %s 与 %s 重叠" % (cable.name, key, other))


def validate():
    names = [cable.name for cable in CABLES]
    if len(set(names)) != len(names) or set(names) & {PANELS, PANEL}:
        raise CableModelError("CABLES 里有重名的光缆，或与面板几何撞名")
    for cable in CABLES:
        if not 0 < cable.core < 2 * HALF:
            raise CableModelError("%s的线芯 %s，应在 0～16 之间" % (cable.label, num(cable.core)))
        if cable.glass != (cable.plate_inset is None):
            raise CableModelError("%s：不透明光缆要在 PLATE_INSET 里登记盖板，玻璃不登记" % cable.label)
        if not cable.glass and not 0 < cable.core - 2 * cable.plate_inset:
            raise CableModelError("%s的端口盖板缩进 %s，盖板没了" % (cable.label, cable.plate_inset))
        bones = len(cable_bones(cable.device_id, cable.panels))
        if bones > BONE_VISIBILITY_LIMIT:
            # 每根骨骼一条 bone_visibility，超了整个方块加载失败（2026-11-10 真机）
            raise CableModelError("%s有 %d 根骨骼，超过 bone_visibility 的上限 %d"
                                  % (cable.label, bones, BONE_VISIBILITY_LIMIT))
        validate_regions(cable)
    if not 0 < BACK_SIZE <= PANEL_SIZE <= 2 * HALF:
        raise CableModelError("应满足 0 < BACK_SIZE ≤ PANEL_SIZE ≤ 16")
    if PANEL_DEPTH <= 0 or BACK_DEPTH <= 0:
        raise CableModelError("面板与背板的厚度都要大于 0")
    if not 0 < SCREEN_DEPTH <= PANEL_DEPTH:
        raise CableModelError("应满足 0 < SCREEN_DEPTH ≤ PANEL_DEPTH")
    if PANEL_SIZE / 2.0 > HALF - PANEL_DEPTH:
        # AE2 的 12×12×2 正好卡住：相邻两面的面板只在棱上相碰
        raise CableModelError(
            "PANEL_SIZE %s 配厚度 %s 时，相邻两面的面板会互相穿插（要 ≤ %s）"
            % (num(PANEL_SIZE), num(PANEL_DEPTH), num(2 * (HALF - PANEL_DEPTH)))
        )
    for cable in CABLES:
        if not cable.panels:
            continue
        stub_length(cable.core)  # 放不下短接线就报错
        if cable.core > BACK_SIZE:
            raise CableModelError(
                "%s的短接线（截面 %s）比背板（%s）粗，会从背板边上露出来"
                % (cable.label, num(cable.core), num(BACK_SIZE))
            )
    kinds = [kind for kind, _device in PANEL_KINDS]
    if DEFAULT_PANEL in kinds or len(set(kinds)) != len(kinds):
        raise CableModelError("PANEL_KINDS 的短名重复，或与 %r 撞名" % DEFAULT_PANEL)
    devices = DefinitionSet().devices
    unknown = [device for _kind, device in PANEL_KINDS if device not in devices]
    if unknown:
        raise CableModelError("PANEL_KINDS 里有定义表不认识的设备 %r" % (unknown,))
    if sorted(PROBE) != sorted(FACING_NAMES) or not set(PROBE.values()) <= set(screen_variants()):
        raise CableModelError("PROBE 要给六个面各指定一种面板")


def outputs(probe=False):
    """`([(路径, 字节串)], [(贴图路径, (宽, 高), 像素)])`：json 与贴图分开，贴图另有种子规则。

    光缆贴图照 `build_texture` 画，面板图集画占位图。
    """
    validate()
    documents, textures = [], []
    for cable in CABLES:
        documents.append((block_file(cable.name), build_cable(cable)))
        textures.append((texture_file(CABLE_TEXTURE_DIR, cable.name), cable_texture_size(cable),
                         build_texture(cable)))
        documents.append((block_file(cable_item_name(cable)), build_cable_item(cable)))
    panels = panel_atlas(PANELS)
    documents.extend([
        (entity_geometry_file(PANELS), build_panels(panels)),
        (entity_geometry_file(PANEL), build_single_panel(panels)),
        (ENTITY_FILE, build_entity(probe)),
        (CONTROLLER_FILE, build_controller()),
    ])
    for kind, _terminal in PANEL_KINDS:
        documents.append((block_file(panel_item_name(kind)), build_panel_item(kind, panels)))
    textures.append((texture_file(PANEL_TEXTURE_DIR, PANELS), panels.size, placeholder(panels)))
    return [(path, json_bytes(payload)) for path, payload in documents], textures


def print_layout():
    """光缆贴图的固定分区 `REGIONS`，再加面板图集的自动分区。"""
    for cable in CABLES:
        size = cable_texture_size(cable)
        print("%s（%d×%d）" % ((rel(texture_file(CABLE_TEXTURE_DIR, cable.name)),) + size))
        for key, (u, v, w, h) in REGIONS[cable.name].items():
            print("  %-13s %-5s 左上角 (%d, %d)  %s" % (key, "%d×%d" % (w, h), u, v, REGION_LABELS[key]))
    panels = panel_atlas(PANELS)
    print("%s（%d×%d）" % ((rel(texture_file(PANEL_TEXTURE_DIR, PANELS)),) + panels.size))
    for region in panels.regions:
        what = "：%s" % region.variant if region.variant else ""
        print("  %s%s  %s×%s  左上角 (%d, %d)" % (
            KINDS[region.kind][0], what, region.width, region.height, region.u, region.v))


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成末影光缆与线缆面板的几何体、外观和贴图")
    parser.add_argument("--check", action="store_true", help="只比对，不写文件")
    parser.add_argument("--textures", action="store_true",
                        help="贴图也按本脚本重画（光缆照 build_texture、面板图集画占位图，覆盖已有"
                             "贴图）；配 --check 时连贴图内容一起比")
    parser.add_argument("--layout", action="store_true", help="打印各贴图的 UV 分区")
    parser.add_argument("--probe", action="store_true", help="真机排查：六面先各显示一块面板")
    parser.add_argument("--icons", action="store_true",
                        help="另补物品占位图标（只补物品图集里登记了的、缺的，绝不覆盖；先跑 gen_items.py）")
    use_utf8_stdout()
    args = parser.parse_args(argv)
    try:
        documents, textures = outputs(probe=args.probe)
        icons = icon_outputs() if args.icons else []
    except CableModelError as error:
        print("参数有问题：%s" % error)
        return 1
    if args.layout:
        print_layout()
        return 0
    changed, problems = [], []
    for path, payload in documents:
        if write_if_changed(path, payload, args.check):
            changed.append(rel(path))
    for path, size, pixels in textures:
        if args.textures or not os.path.isfile(path):
            if write_if_changed(path, png_bytes(pixels), args.check):
                changed.append(rel(path))
        elif png_size(path) != tuple(size):
            problems.append("%s 是 %d×%d，几何体按 %d×%d 写的 UV（照 --layout 重画，或加 --textures "
                            "换回本脚本画的那张）" % ((rel(path),) + png_size(path) + tuple(size)))
    registered = registered_icon_names() if icons else set()
    for path, rows in icons:
        # 只补物品图集里登记了的；已有的图标（作者重画过的）一律不碰
        if os.path.splitext(os.path.basename(path))[0] not in registered:
            continue
        if not os.path.isfile(path) and write_if_changed(path, png_bytes(rows), args.check):
            changed.append(rel(path))
    if args.check:
        stale = changed + problems
        if stale:
            print("以下文件需要重新生成或调整（共 %d 项）：" % len(stale))
            for line in stale:
                print("  " + line)
            return 1
        print("光缆与面板模型已是最新")
        return 0
    if args.probe:
        print("【探针版】六面先各显示一块面板；排查完请不带参数再运行一次")
    print("已生成光缆与面板模型，更新 %d 个文件" % len(changed))
    for path in changed:
        print("  " + path)
    for line in problems:
        print("注意：" + line)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
