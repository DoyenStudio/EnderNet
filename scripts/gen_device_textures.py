# coding=utf-8
"""按美术基调为设备方块补 16x16 占位贴图（只补缺失的，不覆盖手绘图）。

    python scripts/gen_device_textures.py                 # 只补缺失的（亮色，AE2 向）
    python scripts/gen_device_textures.py --theme dark    # 暗色调色板
    python scripts/gen_device_textures.py --force         # 全部重画（会覆盖手绘图！）
    python scripts/gen_device_textures.py --check         # 只列出缺失的文件

**手绘贴图才是成品。** 2026-11-05 起设备贴图以 User 重绘的为准；本脚本只在
新增设备、或某张图还没画时补一张风格一致的占位，默认绝不覆盖已有文件。

文件名与图集键由 `gen_blocks.py` 的布局决定（两边同源）：

- `uniform`：`<短名>_all.png`，画该设备的功能图形；
- `front`：`<短名>_front.png` 画功能图形，另加三张共用的通用机壳
  `common_device_{top,side,bottom}.png`；
- `model`：模型贴图由 User 在 Blockbench 里画，本脚本不生成。

图形与调色板按 EnderNet 美术基调（亮色、AE2 向）绘制，本脚本就是它的权威实现。
只依赖标准库（`_png.py`），不需要 PIL。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _console import use_utf8_stdout  # noqa: E402
from _png import png_bytes  # noqa: E402
from gen_blocks import (  # noqa: E402
    COMMON_DEVICE_TEXTURES,
    LAYOUT_FRONT,
    LAYOUT_MODEL,
    LAYOUT_UNIFORM,
    REPO,
    common_device_texture,
    front_texture,
    layout_of,
    uniform_texture,
)

SCRIPTS_ROOT = os.path.join(REPO, "endernet_bp", "endernet_scripts")
RP_ROOT = os.path.join(REPO, "endernet_rp")
if SCRIPTS_ROOT not in sys.path:
    sys.path.insert(0, SCRIPTS_ROOT)

from endernet.common.definitions import DefinitionSet  # noqa: E402

SIZE = 16

# ---------------------------------------------------------------------------
# 调色板
#
# 两套主题共用同一套色标：k0/k1 永远是深色凹槽与窗口，k2~k7 是机壳本体，
# kb 是底面底板，g0~g2 是装配仓视窗，功能色每组 0=核心 1=光晕 2=暗槽。
# ---------------------------------------------------------------------------

THEMES = {
    "dark": {
        "k0": "#0d1016", "k1": "#161b24", "k2": "#212833", "k3": "#2b3340",
        "k4": "#3a4351", "k5": "#535e70", "k6": "#7a8698", "k7": "#c8d0dc",
        "kb": "#181d26",
        "g0": "#1a2632", "g1": "#223648", "g2": "#5b7f99",
        "net": ("#5ef7dc", "#1fbfa6", "#13544c"),
        "item": ("#ffc95c", "#d18f2a", "#5a4119"),
        "fluid": ("#6cc4ff", "#2f86d6", "#183d5e"),
        "craft": ("#c3a2ff", "#8a63e6", "#3a2a66"),
        "power": ("#ff74b5", "#d9418a", "#5c1f3d"),
    },
    "light": {
        "k0": "#1b1f27", "k1": "#2c323d", "k2": "#c9cfd8", "k3": "#dde2e8",
        "k4": "#8d96a4", "k5": "#a9b1bd", "k6": "#f3f5f8", "k7": "#ffffff",
        "kb": "#9aa3b0",
        "g0": "#6f93ab", "g1": "#a8cbe0", "g2": "#eaf6ff",
        "net": ("#7fe9ff", "#2fb7e0", "#1d5a72"),
        "item": ("#ffc95c", "#d18f2a", "#5a4119"),
        "fluid": ("#7aa4ff", "#3a68d9", "#1d336e"),
        "craft": ("#c48dff", "#8f52e8", "#43276f"),
        "power": ("#ff74b5", "#d9418a", "#5c1f3d"),
    },
}
DEFAULT_THEME = "light"
FAMILIES = ("net", "item", "fluid", "craft", "power")


def resolve(token, theme):
    """色标 -> `#rrggbb`。贴图按「运行」态出图：状态灯取功能色核心。"""
    palette = THEMES[theme]
    if token.startswith("S:"):
        return palette[token[2:]][0]
    if token in palette and not isinstance(palette[token], tuple):
        return palette[token]
    family, level = token[:-1], token[-1:]
    if family in FAMILIES and level in "012":
        return palette[family][int(level)]
    raise ValueError("未知色标 %r" % (token,))


def rgba(hex_color):
    text = hex_color.lstrip("#")
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), 255)


# ---------------------------------------------------------------------------
# 16x16 绘制器
# ---------------------------------------------------------------------------


class Tex(object):
    """一张 16x16 贴图，每格存一个色标。"""

    def __init__(self):
        self.p = [None] * (SIZE * SIZE)

    def set(self, x, y, token):
        if 0 <= x < SIZE and 0 <= y < SIZE:
            self.p[y * SIZE + x] = token
        return self

    def rect(self, x, y, w, h, token):
        for j in range(h):
            for i in range(w):
                self.set(x + i, y + j, token)
        return self

    def h(self, x, y, w, token):
        return self.rect(x, y, w, 1, token)

    def v(self, x, y, h, token):
        return self.rect(x, y, 1, h, token)

    def box(self, x, y, w, h, token):
        self.h(x, y, w, token)
        self.h(x, y + h - 1, w, token)
        self.v(x, y, h, token)
        self.v(x + w - 1, y, h, token)
        return self

    def pts(self, points, token):
        for x, y in points:
            self.set(x, y, token)
        return self

    def pixels(self, theme):
        rows = []
        for y in range(SIZE):
            row = []
            for x in range(SIZE):
                token = self.p[y * SIZE + x]
                if token is None:
                    raise ValueError("贴图在 (%d, %d) 处留空" % (x, y))
                row.append(rgba(resolve(token, theme)))
            rows.append(row)
        return rows


CORNERS = ((0, 0), (15, 0), (0, 15), (15, 15))


def chassis(fill="k2", hi="k6", gap=False):
    """机壳：1px 斜面边框（上左亮、下右暗、四角倒角），可选内圈凹槽。"""
    t = Tex()
    t.rect(0, 0, 16, 16, fill)
    t.h(0, 0, 16, hi).v(0, 0, 16, hi)
    t.h(0, 15, 16, "k4").v(15, 0, 16, "k4")
    t.pts(CORNERS, "k3")
    if gap:
        t.box(1, 1, 14, 14, "k1")
        t.h(2, 2, 12, "k3")
    return t


# ---------------------------------------------------------------------------
# 顶面与底面（通用）
# ---------------------------------------------------------------------------


def top_face(fam):
    """通用顶面：四颗螺钉 + 中心接口枢纽（功能色）。"""
    t = chassis(gap=True)
    t.pts(((3, 3), (12, 3), (3, 12), (12, 12)), "k4")
    t.box(6, 6, 4, 4, "k1")
    t.rect(7, 7, 2, 2, fam + "2")
    t.set(7, 7, fam + "1")
    return t


def side_face():
    """通用侧面：信道带切穿边框 + 状态灯 + 散热缝。"""
    t = chassis(gap=True)
    t.h(0, 7, 16, "k0").h(0, 8, 16, "net2").set(3, 8, "net1").set(12, 8, "net1")
    t.set(3, 12, "S:net")
    t.h(9, 11, 4, "k1").h(9, 13, 4, "k1")
    return t


def bottom_face():
    """通用底面：更暗的底板 + 四角铆钉 + 中心网络插口。"""
    t = chassis(fill="kb")
    t.box(1, 1, 14, 14, "k1")
    t.pts(((3, 3), (12, 3), (3, 12), (12, 12)), "k4")
    t.box(5, 5, 6, 6, "k4")
    t.rect(6, 6, 4, 4, "k0")
    t.rect(7, 7, 2, 2, "net2")
    t.set(7, 7, "net1")
    t.h(5, 12, 6, "k4")
    return t


# ---------------------------------------------------------------------------
# 功能图形（侧面；六面相同的方块三面都用它）
# ---------------------------------------------------------------------------


def f_dos():
    """DOS 网络控制器：末影之眼 + 右下角光标。"""
    t = chassis()
    t.rect(1, 1, 14, 14, "k1")
    t.pts(((2, 2), (3, 2), (2, 3), (13, 2), (12, 2), (13, 3),
           (2, 13), (3, 13), (2, 12), (13, 13), (12, 13), (13, 12)), "k4")
    t.h(0, 7, 2, "k0").h(0, 8, 2, "net2").h(14, 7, 2, "k0").h(14, 8, 2, "net2")
    lens = {4: (6, 9), 5: (4, 11), 6: (3, 12), 7: (2, 13), 8: (2, 13),
            9: (3, 12), 10: (4, 11), 11: (6, 9)}
    for y in sorted(lens):
        a, b = lens[y]
        t.h(a, y, b - a + 1, "net2").set(a, y, "net1").set(b, y, "net1")
    t.h(6, 4, 4, "net1").h(6, 11, 4, "net1")
    t.h(6, 5, 4, "net1").h(6, 10, 4, "net1")
    for y in range(6, 10):
        t.set(5, y, "net1").set(10, y, "net1").h(6, y, 4, "net0")
    t.v(7, 6, 4, "k0").v(8, 6, 4, "k0")
    t.h(11, 13, 2, "net0")
    return t


def f_tiny():
    """小型网络控制器：缩小的末影之眼。"""
    t = chassis(gap=True)
    t.h(0, 8, 2, "net2").h(14, 8, 2, "net2")
    lens = {6: (5, 10), 7: (4, 11), 8: (4, 11), 9: (5, 10)}
    for y in sorted(lens):
        a, b = lens[y]
        t.h(a, y, b - a + 1, "net2").set(a, y, "net1").set(b, y, "net1")
    t.h(5, 6, 6, "net1").h(5, 9, 6, "net1")
    t.pts(((6, 7), (9, 7), (6, 8), (9, 8)), "net0")
    t.rect(7, 7, 2, 2, "k0")
    t.set(3, 12, "S:net")
    return t


def f_baseband(bits):
    """网络基带：信道数 = 位宽 / 8，十字贯通，交叉点为节点。"""
    t = chassis()
    t.rect(1, 1, 14, 14, "k1")
    lanes = {8: (7,), 16: (5, 9), 32: (2, 5, 9, 12)}[bits]
    for a in lanes:
        t.h(0, a, 16, "net2").h(0, a + 1, 16, "k0")
    for a in lanes:
        t.v(a, 0, 16, "net2").v(a + 1, 0, 16, "k0")
    for a in lanes:
        t.set((a * 5 + 3) % 16, a, "net1").set(a, (a * 7 + 11) % 16, "net1")
    for a in lanes:
        for b in lanes:
            if bits < 32:
                t.set(a, b, "net0").set(a + 1, b, "net1")
                t.set(a, b + 1, "net1").set(a + 1, b + 1, "net2")
            else:
                t.set(a, b, "net0")
    return t


def f_slots(fam, fluid):
    """被动接口：3x3 槽阵 = 存取共用 9 槽。"""
    t = chassis()
    t.rect(1, 1, 14, 14, "k3")
    occupied = (1, 0, 1, 1, 1, 0, 0, 1, 0)
    for j in range(3):
        for i in range(3):
            x, y = 1 + i * 5, 1 + j * 5
            t.box(x, y, 4, 4, fam + "2").rect(x + 1, y + 1, 2, 2, "k0")
            if occupied[j * 3 + i]:
                if fluid:
                    t.h(x + 1, y + 2, 2, fam + "1").h(x + 1, y + 1, 2, fam + "2")
                else:
                    t.set(x + 1, y + 1, fam + "1")
                    t.set(x + 2, y + 1, fam + "2").set(x + 1, y + 2, fam + "2")
    return t


def f_port(fam, direction, fluid):
    """存货口（in）/ 取货口（out）：双箭头指向横条 + 缓存指示。"""
    t = chassis(gap=True)
    c0, c1 = fam + "0", fam + "1"
    t.box(3, 2, 10, 10, fam + "2").rect(4, 3, 8, 8, "k0")
    t.pts(((3, 2), (12, 2), (3, 11), (12, 11)), "k2")
    if direction == "in":
        t.pts(((5, 4), (10, 4), (6, 5), (9, 5), (7, 6), (8, 6)), c1)
        t.pts(((5, 7), (10, 7), (6, 8), (9, 8), (7, 9), (8, 9)), c0)
        t.h(5, 10, 6, c1)
    else:
        t.h(5, 3, 6, c1)
        t.pts(((7, 4), (8, 4), (6, 5), (9, 5), (5, 6), (10, 6)), c0)
        t.pts(((7, 7), (8, 7), (6, 8), (9, 8), (5, 9), (10, 9)), c1)
    if fluid:
        t.h(3, 13, 10, "k1").h(3, 13, 6, c1)
    else:
        for index, x in enumerate((3, 5, 7, 9, 11)):
            t.set(x, 13, c0 if index < 2 else "k3")
    return t


def f_template_io():
    """样板接口：3x3 样板点阵，亮点为已装样板。"""
    t = chassis(gap=True)
    t.rect(2, 2, 12, 12, "k1")
    grid = (3, 7, 11)
    occupied = (1, 1, 0, 1, 1, 1, 0, 1, 0)
    for j in range(3):
        for i in range(2):
            t.rect(grid[i] + 2, grid[j], 2, 2, "craft2")
    for i in range(3):
        for j in range(2):
            t.rect(grid[i], grid[j] + 2, 2, 2, "craft2")
    for j in range(3):
        for i in range(3):
            x, y = grid[i], grid[j]
            if occupied[j * 3 + i]:
                t.rect(x, y, 2, 2, "craft1").set(x, y, "craft0")
            else:
                t.rect(x, y, 2, 2, "k3")
    return t


def f_driver():
    """存储驱动器：2x5 仓位 = 10 槽，每仓一颗灯，中脊为数据总线。"""
    t = chassis()
    t.rect(1, 1, 14, 14, "k3")
    t.v(7, 1, 14, "net2").v(8, 1, 14, "k1").set(7, 7, "net1").set(7, 8, "net1")
    occupied = (1, 1, 1, 0, 1, 1, 0, 1, 1, 0)
    for r, y in enumerate((1, 4, 7, 10, 13)):
        for side in (0, 1):
            x = 9 if side else 1
            full = occupied[side * 5 + r]
            t.h(x, y, 6, "k5" if full else "k0")
            t.h(x, y + 1, 6, "k4" if full else "k1")
            if full:
                t.set(14 if side else 1, y, "k6")
            t.set(9 if side else 6, y + 1, "net0" if full else "k2")
    return t


def terminal(draw):
    """终端：统一屏幕框，屏内内容即功能。"""
    t = chassis()
    t.rect(1, 1, 14, 14, "k2").h(1, 1, 14, "k3")
    t.box(2, 2, 12, 10, "k0").rect(3, 3, 10, 8, "k1").set(12, 3, "k3")
    t.h(3, 13, 2, "k4").h(6, 13, 2, "k4").set(12, 13, "S:net")
    draw(t)
    return t


def _common_screen(t):
    t.h(4, 4, 6, "k3")
    for y, length in ((6, 4), (8, 3), (10, 4)):
        t.set(4, y, "item1").set(5, y, "item2").h(7, y, length, "net1")
    t.v(12, 4, 7, "k2").v(12, 4, 2, "net2")


def _crafting_screen(t):
    for a in (4, 6, 8):
        for b in (4, 6, 8):
            t.set(a, b, "item1" if (a + b) % 4 == 0 else "item2")
    t.pts(((9, 5), (9, 6), (9, 7), (10, 6)), "net1")
    t.rect(11, 5, 2, 2, "item0")
    t.h(4, 10, 5, "net2")


def _template_screen(t):
    t.box(5, 3, 6, 8, "craft1").rect(6, 4, 4, 6, "craft2")
    t.set(10, 3, "k1").set(9, 4, "craft1")
    t.h(6, 5, 3, "craft0").h(6, 7, 4, "craft0").h(6, 9, 2, "craft0")


def _factory_screen(t):
    t.rect(4, 4, 2, 2, "craft1").rect(4, 8, 2, 2, "craft1").rect(10, 6, 2, 2, "net0")
    t.pts(((6, 4), (7, 4), (8, 5), (9, 6)), "craft2")
    t.pts(((6, 9), (7, 9), (8, 8), (9, 7)), "craft2")
    t.h(4, 10, 8, "k2").h(4, 10, 5, "craft1")


def f_processor():
    """并行处理器：芯片 + 4 条并行走线。"""
    t = chassis()
    t.rect(1, 1, 14, 14, "k1")
    for p in (5, 7, 8, 10):
        t.v(p, 2, 2, "k5").v(p, 12, 2, "k5").h(2, p, 2, "k5").h(12, p, 2, "k5")
    t.rect(4, 4, 8, 8, "k3").box(4, 4, 8, 8, "k4")
    for x in (5, 7, 8, 10):
        t.v(x, 5, 6, "craft2")
    t.pts(((5, 6), (7, 9), (8, 6), (10, 9)), "craft0")
    t.pts(((5, 7), (7, 8), (8, 7), (10, 8)), "craft1")
    return t


def f_connector():
    """并行处理器连接器：网络信道与计算信道在耦合器汇合。"""
    t = chassis(gap=True)
    t.h(0, 7, 5, "net1").h(0, 8, 5, "net2").h(11, 7, 5, "craft1").h(11, 8, 5, "craft2")
    t.box(5, 5, 6, 6, "k4").rect(6, 6, 4, 4, "k1")
    t.pts(((7, 7), (8, 8)), "net0").pts(((8, 7), (7, 8)), "craft0")
    t.set(3, 12, "S:net")
    return t


def f_memory_slot():
    """内存槽：2x5 竖插槽 = 10 根内存条。"""
    t = chassis()
    t.rect(1, 1, 14, 14, "k3")
    occupied = (1, 1, 1, 1, 0, 1, 1, 0, 0, 0)
    for r in (0, 1):
        y = 9 if r else 2
        for i, x in enumerate((1, 4, 7, 10, 13)):
            if occupied[r * 5 + i]:
                t.rect(x, y, 2, 5, "craft2").h(x, y, 2, "k5")
                t.set(x, y + 2, "craft1").set(x + 1, y + 3, "craft0")
            else:
                t.rect(x, y, 2, 5, "k0").h(x, y + 4, 2, "k1")
    t.h(1, 7, 14, "k1").h(1, 8, 14, "k4")
    return t


def f_assembler():
    """装配仓：视窗 + 紫色装配核 + 四只机械臂。"""
    t = chassis()
    t.rect(1, 1, 14, 14, "k4").box(2, 2, 12, 12, "k3")
    t.rect(3, 3, 10, 10, "g1").box(3, 3, 10, 10, "g0")
    t.pts(((4, 6), (5, 5), (6, 4), (4, 8), (5, 7), (7, 5), (8, 4)), "g2")
    t.rect(6, 6, 4, 4, "craft2").rect(7, 7, 2, 2, "craft0")
    t.pts(((6, 6), (9, 6), (6, 9), (9, 9)), "craft1")
    t.pts(((5, 5), (10, 5), (5, 10), (10, 10)), "k6")
    t.set(13, 14, "S:net")
    return t


def f_energy_input():
    """能源接入口：圆角接口框里一道闪电。"""
    t = chassis(gap=True)
    t.h(2, 7, 2, "power2").h(2, 8, 2, "power2").h(12, 7, 2, "power2").h(12, 8, 2, "power2")
    t.rect(4, 4, 8, 8, "power2").pts(((4, 4), (11, 4), (4, 11), (11, 11)), "k2")
    t.rect(5, 5, 6, 6, "k0")
    t.pts(((8, 5), (9, 5), (7, 6), (8, 6), (6, 7), (7, 7), (8, 7), (9, 7),
           (7, 8), (8, 8), (6, 9), (7, 9), (6, 10)), "power0")
    t.pts(((9, 5), (6, 10)), "power1")
    t.set(3, 12, "S:net")
    return t


def f_creative():
    """创造能量源：纯白高光边框 + ∞ 核心 + 四角放射线。"""
    t = chassis(hi="k7")
    t.rect(1, 1, 14, 14, "k1")
    t.pts(((2, 2), (3, 3), (13, 2), (12, 3), (2, 13), (3, 12), (13, 13), (12, 12)), "power2")
    t.pts(((4, 5), (5, 5), (10, 5), (11, 5), (3, 6), (6, 6), (9, 6), (12, 6),
           (3, 7), (7, 7), (8, 7), (12, 7), (3, 8), (6, 8), (9, 8), (12, 8),
           (4, 9), (5, 9), (10, 9), (11, 9)), "power0")
    return t


def f_wireless():
    """无线连接器：中央天线 + 两侧三层信号弧（占位，作者后补正式贴图）。"""
    t = chassis(gap=True)
    t.v(7, 5, 8, "net2").v(8, 5, 8, "k0")
    t.rect(6, 4, 4, 2, "net1").set(7, 4, "net0")
    t.h(5, 13, 6, "k4")
    for offset, token in ((2, "net0"), (3, "net1"), (4, "net2")):
        t.v(7 - offset - 1, 6 - offset // 2, 3 + offset, token)
        t.v(8 + offset + 1, 6 - offset // 2, 3 + offset, token)
    t.set(3, 12, "S:net")
    return t


#: 设备 -> 功能图形。`uniform` 画进 `<短名>_all`，`front` 画进 `<短名>_front`；
#: 存储驱动器是模型，不在表里。
#:
#: DOS 与小型控制器**互换**：User 2026-11-05 把两者的贴图对调了，占位图跟着对调，
#: 免得补出来的图和手绘图对不上。
DESIGNS = {
    "endernet:dos_network_endpoint": f_tiny,
    "endernet:tiny_network_endpoint": f_dos,
    "endernet:network_baseband_8": lambda: f_baseband(8),
    "endernet:network_baseband_16": lambda: f_baseband(16),
    "endernet:network_baseband_32": lambda: f_baseband(32),
    "endernet:bus_item_io": lambda: f_slots("item", False),
    "endernet:bus_fluid_io": lambda: f_slots("fluid", True),
    "endernet:bus_item_input_io": lambda: f_port("item", "in", False),
    "endernet:bus_item_output_io": lambda: f_port("item", "out", False),
    "endernet:bus_fluid_input_io": lambda: f_port("fluid", "in", True),
    "endernet:bus_fluid_output_io": lambda: f_port("fluid", "out", True),
    "endernet:bus_template_io": f_template_io,
    "endernet:common_terminal_panel": lambda: terminal(_common_screen),
    "endernet:crafting_terminal_panel": lambda: terminal(_crafting_screen),
    "endernet:template_terminal_panel": lambda: terminal(_template_screen),
    "endernet:factoric_terminal_panel": lambda: terminal(_factory_screen),
    "endernet:assemble_chamber": f_assembler,
    "endernet:parallel_processor": f_processor,
    "endernet:parallel_processor_connector": f_connector,
    "endernet:memory_slot": f_memory_slot,
    "endernet:energy_input_interface": f_energy_input,
    "endernet:creative_energy_source": f_creative,
    "endernet:wireless_connector": f_wireless,
}

#: 通用机壳三面的占位画法
COMMON_PAINTERS = {
    "top": lambda: top_face("net"),
    "side": side_face,
    "bottom": bottom_face,
}


def outputs(devices):
    """需要的占位贴图：`[(资源包内路径, 画法)]`，去重。"""
    out = []
    seen = set()

    def add(path, painter):
        if path not in seen:
            seen.add(path)
            out.append((path, painter))

    for device_id in devices:
        layout = layout_of(device_id)
        if layout == LAYOUT_MODEL:
            continue
        if layout == LAYOUT_UNIFORM:
            add(uniform_texture(device_id)[1], DESIGNS[device_id])
        elif layout == LAYOUT_FRONT:
            add(front_texture(device_id)[1], DESIGNS[device_id])
            for face, _ in COMMON_DEVICE_TEXTURES:
                add(common_device_texture(face)[1], COMMON_PAINTERS[face])
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description="为 EnderNet 设备方块补占位贴图")
    parser.add_argument("--theme", choices=sorted(THEMES), default=DEFAULT_THEME,
                        help="调色板（默认 %s）" % DEFAULT_THEME)
    parser.add_argument("--force", action="store_true", help="覆盖已有文件（包括手绘图）")
    parser.add_argument("--check", action="store_true", help="只列出缺失的文件")
    use_utf8_stdout()
    args = parser.parse_args(argv)

    devices = sorted(DefinitionSet().devices)
    undesigned = [
        d for d in devices if layout_of(d) != LAYOUT_MODEL and d not in DESIGNS
    ]
    if undesigned:
        print("这些设备还没有占位画法：%r" % (undesigned,))
        return 1

    missing = []
    written = []
    for path, painter in outputs(devices):
        full = os.path.join(RP_ROOT, *(path + ".png").split("/"))
        exists = os.path.isfile(full)
        if not exists:
            missing.append(os.path.relpath(full, REPO))
        if args.check or (exists and not args.force):
            continue
        directory = os.path.dirname(full)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(full, "wb") as handle:
            handle.write(png_bytes(painter().pixels(args.theme)))
        written.append(os.path.relpath(full, REPO))

    if args.check:
        if missing:
            print("缺少设备贴图（%d 张）：" % len(missing))
            for path in missing:
                print("  " + path)
            return 1
        print("设备贴图齐全")
        return 0
    print("已按 %s 主题写入 %d 张占位贴图" % (args.theme, len(written)))
    for path in written:
        print("  " + path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
