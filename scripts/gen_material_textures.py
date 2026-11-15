# coding=utf-8
"""EnderNet 生存材料的 16x16 像素画源文件。

    python -B scripts/gen_material_textures.py
    python -B scripts/gen_material_textures.py --check
    python -B scripts/gen_material_textures.py --preview <绝对PNG路径>

默认只补缺失的 PNG。已有贴图（含手绘调整）保留；--force 仅用于明确重画
本脚本列出的新增图标，永远不涉及旧设备、旧内存或已有工具贴图。
资产编码只用标准库；可选的预览拼图使用 Pillow。

视觉约定：青蓝水晶/光学、金色逻辑、紫色运算、冰蓝工程、冷白石英饰面。
材料轮廓区分原料、薄电路、封装芯片、功能核心、介质；无字母占位或纯色方块。
"""

import argparse
import os
import sys

from _console import use_utf8_stdout
from _png import png_bytes, read_png


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXTURE_ROOT = os.path.join(REPO, "endernet_rp", "textures", "endernet")
TRANSPARENT = (0, 0, 0, 0)
COLORS = {
    ".": TRANSPARENT,
    "ink": "#1b222d", "black": "#242c38", "deep": "#343d4c",
    "shadow": "#5c6777", "edge": "#8995a5", "steel": "#b5c1cf",
    "white": "#e3e8ef", "light": "#f7fbff", "bluegray": "#7394a6",
    "cyan0": "#d1f7ff", "cyan1": "#7fe9ff", "cyan2": "#2fb7e0",
    "cyan3": "#177b9d", "cyan4": "#1d4d62",
    "gold0": "#fff0a6", "gold1": "#ffc95c", "gold2": "#d18f2a",
    "gold3": "#896322", "gold4": "#503c21",
    "violet0": "#e8d7ff", "violet1": "#c48dff", "violet2": "#8f52e8",
    "violet3": "#6539a2", "violet4": "#372650",
    "blue0": "#bdd7ff", "blue1": "#7aa4ff", "blue2": "#3a68d9",
    "blue3": "#223c7e", "blue4": "#182744",
    "wood0": "#ebd4a5", "wood1": "#c7a571", "wood2": "#a88654",
    "wood3": "#775a37", "wood4": "#4f3c28",
    "end0": "#edf0bf", "end1": "#dfe3aa", "end2": "#ccd393",
    "end3": "#bbc380", "end4": "#a4ac6f",
    "hot0": "#ffecb2", "hot1": "#ffbb55", "hot2": "#e96938",
    "hot3": "#9d3434", "hot4": "#582f39",
}


def color(token):
    value = COLORS.get(token, token)
    if isinstance(value, tuple):
        return value
    value = value.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4)) + (255,)


class PixelArt(object):
    def __init__(self, background="."):
        self.rows = [[color(background) for _ in range(16)] for _ in range(16)]

    def dot(self, x, y, token):
        if 0 <= x < 16 and 0 <= y < 16:
            self.rows[y][x] = color(token)
        return self

    def rect(self, x, y, w, h, token):
        for py in range(y, y + h):
            for px in range(x, x + w):
                self.dot(px, py, token)
        return self

    def h(self, x, y, length, token):
        return self.rect(x, y, length, 1, token)

    def v(self, x, y, length, token):
        return self.rect(x, y, 1, length, token)

    def box(self, x, y, w, h, token):
        return self.h(x, y, w, token).h(x, y + h - 1, w, token).v(
            x, y, h, token).v(x + w - 1, y, h, token)

    def points(self, points, token):
        for x, y in points:
            self.dot(x, y, token)
        return self


def crystal(charged=False):
    art = PixelArt()
    # Tall central prism plus two slanting shards. Facets stay readable at 16px.
    for y, left, right in ((1, 8, 8), (2, 7, 9), (3, 6, 10), (4, 6, 10),
                           (5, 6, 10), (6, 6, 10), (7, 6, 10), (8, 6, 10),
                           (9, 6, 10), (10, 6, 10), (11, 7, 9), (12, 8, 8)):
        art.h(left, y, right - left + 1, "cyan4")
        if right > left:
            art.h(left + 1, y, right - left, "cyan2")
            art.dot(right, y, "cyan3")
    art.v(7, 4, 7, "cyan0" if charged else "cyan1").v(8, 3, 8, "cyan2")
    art.dot(8, 2, "light").dot(9, 4, "cyan1")
    art.points(((3, 6), (4, 7), (5, 8), (5, 9), (6, 10), (6, 11), (7, 12)), "cyan4")
    art.points(((3, 7), (3, 8), (4, 9), (4, 10), (5, 11), (6, 12)), "cyan3")
    art.points(((4, 7), (4, 8), (5, 9), (5, 10), (6, 11)), "cyan1")
    art.points(((13, 6), (12, 7), (12, 8), (11, 9), (11, 10), (10, 11)), "cyan4")
    art.points(((12, 8), (12, 9), (11, 10), (11, 11), (10, 12)), "cyan2")
    art.h(5, 13, 7, "cyan4").h(6, 12, 5, "cyan3")
    if charged:
        art.v(8, 4, 7, "cyan0").dot(9, 6, "light")
        art.points(((2, 2), (1, 3), (3, 3), (2, 4), (13, 2), (14, 3), (13, 4)), "cyan1")
        art.dot(2, 3, "light").dot(13, 3, "light").dot(14, 11, "cyan0")
    return art


def dust(quartz=False):
    art = PixelArt()
    colors = ("edge", "steel", "white", "light") if quartz else (
        "cyan4", "cyan3", "cyan2", "cyan1")
    outline, dark, mid, high = colors
    spans = ((5, 7, 8), (6, 6, 9), (7, 5, 10), (8, 4, 11),
             (9, 3, 12), (10, 2, 13), (11, 2, 13), (12, 3, 12))
    for y, a, b in spans:
        art.h(a, y, b - a + 1, outline).h(a + 1, y, max(0, b - a - 1), dark)
    art.h(5, 8, 5, mid).h(4, 10, 7, mid).h(6, 7, 3, mid)
    art.points(((7, 6), (6, 8), (9, 8), (4, 10), (7, 10), (10, 11)), high)
    art.points(((2, 7), (12, 5), (11, 3), (5, 3), (1, 12), (14, 10)), mid)
    art.points(((4, 13), (10, 13), (13, 8)), outline)
    return art


def glass_panel():
    art = PixelArt()
    art.rect(3, 2, 10, 12, "cyan4").box(2, 1, 12, 14, "edge")
    art.h(3, 1, 10, "light").v(2, 2, 11, "white")
    art.h(3, 14, 10, "steel").v(13, 2, 12, "bluegray")
    art.rect(4, 3, 8, 9, "cyan3").v(11, 3, 9, "cyan2")
    art.points(((5, 4), (6, 4), (4, 5), (5, 5), (6, 8), (7, 7), (8, 6),
                (9, 5), (9, 10), (10, 9)), "cyan0")
    art.h(4, 12, 8, "cyan2")
    return art


def casing(reinforced=False):
    art = PixelArt()
    art.rect(2, 4, 10, 10, "edge").box(2, 4, 10, 10, "steel")
    art.h(3, 3, 10, "light").h(4, 2, 9, "white")
    art.v(13, 3, 9, "shadow").v(12, 4, 9, "edge")
    art.rect(4, 6, 6, 6, "black" if reinforced else "end3")
    art.box(3, 5, 8, 8, "white").h(4, 5, 6, "light")
    art.h(4, 11, 6, "deep" if reinforced else "end2")
    art.v(9, 6, 5, "shadow" if reinforced else "end4")
    art.points(((3, 5), (10, 5), (3, 12), (10, 12)), "edge")
    if reinforced:
        art.points(((5, 7), (7, 9), (5, 10)), "deep")
        art.v(12, 6, 4, "violet4").dot(12, 7, "violet2")
    else:
        art.points(((5, 7), (7, 8), (5, 10)), "end1")
    return art


def silicon():
    art = PixelArt()
    for y, left, right in ((3, 6, 10), (4, 4, 12), (5, 3, 13), (6, 2, 13),
                           (7, 2, 13), (8, 2, 12), (9, 3, 11), (10, 4, 10)):
        art.h(left, y, right - left + 1, "shadow")
        art.h(left + 1, y, max(0, right - left - 1), "steel")
    art.h(5, 4, 6, "white").h(3, 6, 8, "white").h(3, 7, 4, "light")
    art.h(4, 9, 6, "edge").dot(12, 5, "bluegray")
    art.points(((5, 5), (8, 5), (4, 8), (8, 8), (11, 6)), "edge")
    return art


def magnetic_strip():
    art = PixelArt()
    art.rect(2, 4, 5, 8, "shadow").rect(3, 3, 3, 10, "steel")
    art.rect(3, 5, 3, 6, "black").v(3, 5, 6, "white")
    art.h(6, 4, 6, "edge").rect(6, 5, 7, 3, "deep")
    art.h(7, 7, 5, "ink").rect(11, 7, 3, 5, "black")
    art.h(7, 11, 6, "steel").h(7, 10, 5, "deep")
    art.h(8, 5, 4, "shadow").h(8, 10, 4, "cyan4")
    return art


def circuit(family):
    art = PixelArt()
    art.rect(2, 3, 12, 10, family + "4").box(2, 3, 12, 10, family + "3")
    art.h(3, 3, 10, family + "1").v(2, 4, 7, family + "2")
    art.rect(6, 6, 4, 4, family + "2").box(6, 6, 4, 4, family + "0")
    art.h(3, 6, 3, family + "1").v(4, 7, 4, family + "2")
    art.h(9, 5, 3, family + "2").v(11, 6, 5, family + "1")
    art.h(5, 10, 4, family + "1").dot(3, 9, family + "0")
    for x in (3, 5, 7, 9, 11):
        art.dot(x, 13, "gold2")
    return art


def chip(family):
    art = PixelArt()
    for p in (5, 7, 9, 11):
        art.v(p, 1, 3, "steel").v(p, 12, 3, "edge")
        art.h(1, p, 3, "steel").h(12, p, 3, "edge")
    art.rect(3, 3, 10, 10, "ink").box(3, 3, 10, 10, "edge")
    art.h(4, 3, 8, "white").v(3, 4, 8, "steel")
    art.rect(5, 5, 6, 6, family + "3").box(5, 5, 6, 6, family + "2")
    art.rect(6, 6, 4, 4, family + "1").h(6, 6, 4, family + "0")
    art.v(6, 7, 2, family + "0").rect(8, 8, 2, 2, family + "2")
    art.dot(4, 11, "gold1")
    return art


def direction_core(expansion=False):
    art = PixelArt()
    art.rect(4, 3, 8, 10, "gold4").box(4, 3, 8, 10, "gold2")
    art.h(5, 2, 6, "gold1").h(5, 13, 6, "gold3")
    art.rect(6, 4, 4, 8, "cyan4").h(6, 4, 4, "cyan2")
    art.rect(7, 6, 2, 4, "cyan1").dot(7, 6, "cyan0")
    if expansion:
        art.points(((3, 5), (2, 6), (1, 7), (2, 8), (3, 9),
                    (12, 5), (13, 6), (14, 7), (13, 8), (12, 9)), "cyan1")
        art.points(((1, 7), (14, 7)), "cyan0")
    else:
        art.points(((1, 5), (2, 6), (3, 7), (2, 8), (1, 9),
                    (14, 5), (13, 6), (12, 7), (13, 8), (14, 9)), "cyan1")
        art.points(((3, 7), (12, 7)), "cyan0")
    return art


def display_component():
    art = PixelArt()
    art.rect(1, 2, 14, 11, "edge").box(1, 2, 14, 11, "steel")
    art.h(2, 2, 12, "light").v(1, 3, 9, "white")
    art.rect(3, 4, 10, 7, "ink").h(4, 5, 7, "cyan1")
    art.h(4, 7, 5, "cyan3").h(4, 9, 7, "cyan2").dot(11, 9, "cyan0")
    art.rect(5, 13, 6, 2, "shadow").h(6, 13, 4, "gold1")
    return art


def fluid_seal():
    art = PixelArt()
    for y, a, b in ((2, 5, 10), (3, 3, 12), (4, 2, 13), (5, 2, 13),
                     (6, 1, 14), (7, 1, 14), (8, 1, 14), (9, 2, 13),
                     (10, 2, 13), (11, 3, 12), (12, 5, 10)):
        art.h(a, y, b - a + 1, "blue3")
    art.h(5, 2, 6, "blue0").h(3, 3, 9, "blue1").v(2, 5, 4, "blue2")
    art.rect(5, 5, 6, 5, "cyan4").box(4, 4, 8, 7, "steel")
    art.rect(6, 6, 4, 3, ".").h(5, 5, 5, "cyan1")
    art.h(5, 11, 6, "blue2").h(6, 12, 4, "blue4")
    art.points(((2, 6), (12, 4), (12, 10)), "white")
    return art


def wireless_core():
    art = PixelArt()
    art.rect(5, 4, 6, 9, "gold3").box(5, 4, 6, 9, "gold1")
    art.rect(6, 5, 4, 7, "cyan3").v(7, 5, 6, "cyan1").dot(7, 6, "cyan0")
    art.h(6, 13, 4, "gold2").h(6, 2, 4, "cyan2")
    art.points(((3, 3), (2, 4), (2, 5), (3, 7), (12, 3), (13, 4), (13, 5), (12, 7)), "cyan2")
    art.points(((1, 1), (0, 3), (0, 5), (1, 7), (14, 1), (15, 3), (15, 5), (14, 7)), "cyan4")
    return art


def medium_casing():
    art = PixelArt()
    art.rect(2, 2, 12, 12, "shadow").box(2, 2, 12, 12, "steel")
    art.h(3, 2, 10, "light").v(2, 3, 10, "white")
    art.rect(4, 4, 8, 8, "ink").h(4, 11, 8, "deep")
    art.rect(5, 5, 6, 5, "edge").rect(6, 6, 4, 3, "deep")
    for x in (4, 6, 8, 10):
        art.dot(x, 14, "gold1")
    art.points(((3, 3), (12, 3), (3, 12), (12, 12)), "shadow")
    return art


def storage_component(tier):
    art = PixelArt()
    art.rect(2, 3, 12, 10, "deep").box(2, 3, 12, 10, "edge")
    art.h(3, 2, 10, "white").h(3, 3, 10, "steel")
    art.v(2, 4, 8, "white").h(3, 12, 10, "shadow")
    if tier == 1:
        art.rect(5, 5, 6, 5, "gold3").box(5, 5, 6, 5, "gold1")
        art.rect(6, 6, 4, 3, "gold2").dot(6, 6, "gold0")
    else:
        for x in (4, 9):
            for y in (5, 9) if tier == 3 else (6,):
                art.rect(x, y, 3, 3, "violet3").h(x, y, 3, "violet1")
                art.dot(x + 1, y + 1, "violet0" if tier == 3 else "violet2")
        art.v(7, 6, 4, "cyan3").h(6, 8, 4, "cyan2")
    for x in (4, 6, 8, 10):
        art.v(x, 13, 2, "gold2")
    for i in range(tier):
        art.dot(11 - i * 2, 3, "cyan1")
    return art


def archive_core():
    art = PixelArt()
    art.rect(3, 3, 10, 10, "violet4").box(3, 3, 10, 10, "gold2")
    art.h(4, 2, 8, "gold1").v(2, 4, 8, "gold1")
    for x, y in ((4, 4), (10, 4), (4, 10), (10, 10)):
        art.rect(x, y, 2, 2, "violet2").dot(x, y, "violet1")
    art.h(5, 7, 6, "cyan2").v(7, 5, 6, "cyan2")
    art.rect(6, 6, 4, 4, "gold1").h(5, 7, 6, "white").v(7, 5, 6, "white")
    art.rect(7, 7, 2, 2, "light").dot(8, 9, "gold0")
    art.points(((1, 7), (14, 7), (7, 1), (7, 14)), "cyan1")
    return art


def tape(archive=False):
    art = PixelArt()
    art.rect(1, 3, 14, 10, "deep" if archive else "wood3")
    art.box(1, 3, 14, 10, "edge" if archive else "wood2")
    art.h(2, 3, 12, "white" if archive else "wood0")
    art.h(2, 12, 12, "shadow" if archive else "wood4")
    for x in (3, 9):
        art.rect(x, 5, 4, 5, "gold3" if archive else "wood4")
        art.box(x, 5, 4, 5, "gold1" if archive else "wood1")
        art.rect(x + 1, 6, 2, 3, "black")
        art.dot(x + 1, 7, "cyan0" if archive else "steel")
    art.h(6, 7, 4, "cyan3" if archive else "wood4")
    art.h(5, 10, 6, "gold1" if archive else "wood0")
    art.points(((3, 11), (12, 11)), "cyan2" if archive else "wood2")
    if archive:
        art.h(5, 2, 6, "gold2").dot(7, 2, "gold0")
        art.points(((3, 4), (12, 4)), "violet1")
    return art


def softdisk():
    art = PixelArt()
    art.rect(3, 2, 10, 12, "shadow").box(3, 2, 10, 12, "steel")
    art.h(4, 2, 8, "white").v(3, 3, 10, "white")
    art.rect(5, 3, 6, 4, "deep").rect(7, 3, 3, 3, "steel")
    art.rect(5, 8, 6, 4, "white").h(6, 9, 4, "violet3")
    art.points(((6, 11), (7, 11), (9, 11), (10, 11)), "violet2")
    art.dot(12, 2, ".").dot(4, 12, "ink")
    return art


def harddisk(tier):
    art = PixelArt()
    art.rect(2, 1, 12, 13, "shadow").box(2, 1, 12, 13, "steel")
    art.h(3, 1, 10, "light").v(2, 2, 11, "white")
    art.rect(4, 3, 8, 8, "black")
    for y, a, b in ((3, 6, 9), (4, 5, 10), (5, 4, 11), (6, 4, 11),
                     (7, 4, 11), (8, 5, 10), (9, 6, 9)):
        art.h(a, y, b - a + 1, "edge")
    art.h(6, 4, 4, "white").h(5, 5, 5, "steel").v(5, 6, 2, "steel")
    art.rect(7, 6, 2, 2, "cyan3" if tier == 1 else "violet3")
    art.dot(7, 6, "cyan1" if tier == 1 else "violet1")
    art.points(((11, 10), (10, 9), (9, 8), (8, 7)), "gold2")
    art.points(((10, 10), (9, 9), (8, 8)), "gold0")
    art.h(4, 12, 8, "deep")
    for i in range(tier):
        art.dot(5 + i * 2, 12, "cyan1" if tier == 1 else "violet1")
    art.h(4, 14, 7, "gold2").points(((4, 14), (6, 14), (8, 14), (10, 14)), "gold0")
    art.points(((3, 2), (12, 2), (3, 11)), "shadow")
    return art


def ore():
    art = PixelArt("end1")
    # Hand-placed end-stone pores and bevels; no random noise changes between builds.
    pores = ((0, 0, 3, 2), (5, 1, 3, 2), (12, 0, 3, 3), (2, 5, 3, 2),
             (10, 5, 4, 2), (0, 10, 3, 3), (6, 10, 3, 2), (12, 12, 4, 2),
             (3, 14, 4, 2), (8, 3, 2, 2))
    for x, y, w, h in pores:
        art.rect(x, y, w, h, "end2").h(x, y + h - 1, w, "end3")
        art.h(x + 1, y - 1, max(1, w - 1), "end0")
    veins = ((4, 3, 3, 3), (11, 8, 3, 3), (3, 11, 2, 3), (8, 7, 2, 2))
    for x, y, w, h in veins:
        art.rect(x, y, w, h, "cyan4").h(x, y, w, "cyan2")
        art.v(x, y, h - 1, "cyan3").dot(x + 1, y + 1, "cyan1")
        art.dot(x + 1, y, "cyan0")
    art.points(((7, 4), (12, 7), (5, 13), (9, 8)), "cyan2")
    return art


def machine_frame():
    art = PixelArt("white")
    art.box(0, 0, 16, 16, "light").box(1, 1, 14, 14, "shadow")
    art.rect(2, 2, 12, 12, "steel").h(2, 2, 12, "white")
    art.points(((2, 2), (13, 2), (2, 13), (13, 13)), "edge")
    art.h(4, 14, 8, "edge")
    return art


def charger():
    art = machine_frame()
    art.rect(3, 3, 10, 9, "deep").box(3, 3, 10, 9, "bluegray")
    art.h(4, 4, 8, "cyan4").h(4, 10, 8, "cyan4")
    art.rect(4, 5, 2, 5, "shadow").rect(10, 5, 2, 5, "shadow")
    art.v(5, 5, 5, "gold1").v(10, 5, 5, "gold2")
    art.h(6, 6, 4, "cyan3").h(6, 8, 4, "cyan3")
    art.rect(7, 5, 2, 5, "cyan2").dot(7, 5, "cyan0")
    art.v(7, 6, 3, "cyan1").dot(8, 10, "cyan4")
    art.h(4, 13, 3, "shadow").h(9, 13, 3, "cyan2").dot(9, 13, "cyan0")
    return art


def inscriber():
    art = machine_frame()
    art.rect(3, 3, 10, 9, "ink")
    art.rect(4, 3, 8, 2, "shadow").h(4, 3, 8, "white")
    art.rect(6, 5, 4, 2, "edge").h(5, 7, 6, "steel")
    art.h(5, 8, 6, "white").h(5, 10, 6, "violet2")
    art.h(6, 9, 4, "gold1").h(7, 9, 2, "gold0")
    art.h(4, 11, 8, "shadow").v(3, 5, 6, "steel").v(12, 5, 6, "edge")
    art.h(4, 13, 3, "shadow").points(((9, 13), (10, 13), (11, 13)), "violet1")
    return art


def generator():
    art = machine_frame()
    art.rect(3, 3, 10, 9, "ink").box(3, 3, 10, 9, "shadow")
    art.rect(4, 5, 8, 6, "hot4").h(4, 10, 8, "hot3")
    art.points(((5, 9), (5, 8), (6, 7), (7, 8), (7, 6), (8, 5),
                (8, 8), (9, 7), (10, 8), (10, 9)), "hot2")
    art.rect(6, 8, 4, 2, "hot1").dot(8, 7, "hot1").h(7, 9, 2, "hot0")
    art.h(4, 11, 8, "deep").points(((5, 10), (7, 10), (9, 10), (11, 10)), "shadow")
    art.h(4, 4, 8, "steel").h(4, 13, 4, "shadow")
    art.h(10, 13, 2, "gold1").dot(10, 13, "gold0")
    return art


ITEM_DESIGNS = {
    "resonance_crystal": lambda: crystal(False),
    "charged_resonance_crystal": lambda: crystal(True),
    "ender_dust": lambda: dust(False),
    "quartz_dust": lambda: dust(True),
    "resonance_glass": glass_panel,
    "end_casing": lambda: casing(False),
    "reinforced_end_casing": lambda: casing(True),
    "silicon": silicon,
    "magnetic_strip": magnetic_strip,
    "logic_circuit": lambda: circuit("gold"),
    "calculation_circuit": lambda: circuit("violet"),
    "engineering_circuit": lambda: circuit("cyan"),
    "logic_chip": lambda: chip("gold"),
    "calculation_chip": lambda: chip("violet"),
    "engineering_chip": lambda: chip("cyan"),
    "convergence_core": lambda: direction_core(False),
    "expansion_core": lambda: direction_core(True),
    "display_component": display_component,
    "fluid_seal": fluid_seal,
    "wireless_resonance_core": wireless_core,
    "medium_casing": medium_casing,
    "storage_component_1k": lambda: storage_component(1),
    "storage_component_4k": lambda: storage_component(2),
    "storage_component_16k": lambda: storage_component(3),
    "archive_core": archive_core,
    "doubleside_tape": lambda: tape(False),
    "database_compressed_tape": lambda: tape(True),
    "softdisk_4k": softdisk,
    "harddisk_1k": lambda: harddisk(1),
    "harddisk_4k": lambda: harddisk(2),
    "harddisk_16": lambda: harddisk(3),
}
BLOCK_DESIGNS = {
    "resonance_charger_front": charger,
    "resonance_inscriber_front": inscriber,
    "resonance_generator_front": generator,
}


def textures():
    for folder, designs in (("items", ITEM_DESIGNS), ("devices", BLOCK_DESIGNS),
                            ("block_resources", {"end_resonance_ore_all": ore})):
        for name, design in sorted(designs.items()):
            yield folder, name, design


def preview(path):
    from PIL import Image, ImageDraw, ImageFont
    entries = list(textures())
    columns, width, height = 7, 176, 170
    board = Image.new("RGB", (columns * width, ((len(entries) + columns - 1) // columns) * height), "#242731")
    font_path = "C:/Windows/Fonts/consola.ttf"
    font = ImageFont.truetype(font_path, 12) if os.path.isfile(font_path) else ImageFont.load_default()
    draw = ImageDraw.Draw(board)
    for index, (folder, name, _design) in enumerate(entries):
        x, y = index % columns * width, index // columns * height
        source = os.path.join(TEXTURE_ROOT, folder, name + ".png")
        img = Image.open(source).convert("RGBA").resize((128, 128), Image.Resampling.NEAREST)
        board.paste(img, (x + 24, y + 8), img)
        label = name
        if len(label) > 23:
            split = label.rfind("_", 0, 23)
            label = label[:split] + "\n" + label[split + 1:]
        draw.text((x + 7, y + 142), label, font=font, fill="#e3e8ef")
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        os.makedirs(directory)
    board.save(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成 EnderNet 生存材料像素图标")
    parser.add_argument("--check", action="store_true", help="检查文件齐全、16x16、物品留透明边")
    parser.add_argument("--force", action="store_true", help="重绘本脚本清单内的新增图标；会覆盖后续手绘调整")
    parser.add_argument("--preview", help="可选：用实际PNG制作预览拼图（需要Pillow）")
    args = parser.parse_args(argv)
    use_utf8_stdout()
    missing, invalid, written = [], [], []
    for folder, name, design in textures():
        path = os.path.join(TEXTURE_ROOT, folder, name + ".png")
        exists = os.path.isfile(path)
        if not exists:
            missing.append(path)
        if args.check:
            if exists:
                rows = read_png(path)
                if len(rows) != 16 or any(len(row) != 16 for row in rows):
                    invalid.append(path + ": not 16x16")
                elif folder == "items" and not any(px[3] == 0 for row in rows for px in row):
                    invalid.append(path + ": item background is opaque")
                elif folder == "devices" and any(px[3] != 255 for row in rows for px in row):
                    invalid.append(path + ": block texture contains transparent pixels")
            continue
        if exists and not args.force:
            continue
        directory = os.path.dirname(path)
        if not os.path.isdir(directory):
            os.makedirs(directory)
        with open(path, "wb") as stream:
            stream.write(png_bytes(design().rows))
        written.append(path)
    if args.check:
        if missing or invalid:
            for issue in missing + invalid:
                print(issue)
            return 1
        print("35 张材料/介质/矿石/机器贴图齐全，尺寸与透明度检查通过")
    else:
        print("写入 %d 张新增贴图，已有贴图默认保留" % len(written))
    if args.preview:
        preview(args.preview)
        print("预览：" + os.path.abspath(args.preview))
    return 0


if __name__ == "__main__":
    sys.exit(main())
