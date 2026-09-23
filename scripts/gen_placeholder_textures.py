# coding=utf-8
"""为缺少美术资源的设备生成 16x16 占位贴图。

    python scripts/gen_placeholder_textures.py            # 只补缺失的
    python scripts/gen_placeholder_textures.py --force    # 全部重写

**这些是占位图，不是成品美术。** 它们的作用是让方块在游戏里可辨认、
不至于显示为紫黑缺失贴图。每个方块用分类底色 + 分类图形 + 序号圆点区分。
用真实贴图覆盖同名文件即可，不需要改任何 JSON。

只依赖标准库（zlib + struct 手写 PNG），因此不需要 PIL。
"""
from __future__ import absolute_import, division, print_function

import argparse
import os
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _console import use_utf8_stdout  # noqa: E402
from gen_blocks import (  # noqa: E402
    EXISTING_TEXTURE_FILES,
    short_name,
    texture_key,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_ROOT = os.path.join(REPO, "endernet_bp", "endernet_scripts")
OUT_DIR = os.path.join(REPO, "endernet_rp", "textures", "endernet", "devices")

if SCRIPTS_ROOT not in sys.path:
    sys.path.insert(0, SCRIPTS_ROOT)

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

SIZE = 16

#: 每个分类的 基色 / 边框色 / 图形色
PALETTE = {
    CATEGORY_CONTROLLER: ("#6A3FB5", "#3A2064", "#C9A9F0"),
    CATEGORY_BASEBAND: ("#2E8B84", "#16443F", "#8FE0D8"),
    CATEGORY_BUS: ("#3A6EA5", "#1D3A59", "#9CC4EA"),
    CATEGORY_TERMINAL: ("#3F8B4A", "#1F4A26", "#A3E0AC"),
    CATEGORY_STORAGE: ("#8B6A3F", "#4A3820", "#E0C79C"),
    CATEGORY_PROCESSOR: ("#A5443A", "#5A211C", "#EFA79E"),
    CATEGORY_PROCESSOR_CONNECTOR: ("#7A2F28", "#3F1713", "#D08C84"),
    CATEGORY_MEMORY: ("#B5A03F", "#5F5320", "#EDDF9C"),
    CATEGORY_ASSEMBLY: ("#B5762E", "#5F3D17", "#EDC28F"),
    CATEGORY_ENERGY_INPUT: ("#2FA8C4", "#165669", "#9CE0F0"),
}
FALLBACK_PALETTE = ("#777777", "#3B3B3B", "#CCCCCC")

#: 8x8 分类图形。1 = 画图形色。刻意做得一眼能认出类别。
GLYPHS = {
    CATEGORY_CONTROLLER: (
        ".#....#.",
        ".#....#.",
        ".######.",
        ".#....#.",
        ".######.",
        ".#....#.",
        ".#....#.",
        "........",
    ),
    CATEGORY_BASEBAND: (
        "........",
        "........",
        "########",
        "........",
        "........",
        "########",
        "........",
        "........",
    ),
    CATEGORY_BUS: (
        "........",
        "###..###",
        "........",
        "........",
        "###..###",
        "........",
        "........",
        "........",
    ),
    CATEGORY_TERMINAL: (
        "########",
        "#......#",
        "#.####.#",
        "#.####.#",
        "#......#",
        "########",
        "...##...",
        "........",
    ),
    CATEGORY_STORAGE: (
        "########",
        "#......#",
        "########",
        "#......#",
        "########",
        "#......#",
        "########",
        "........",
    ),
    CATEGORY_PROCESSOR: (
        "########",
        "#......#",
        "#.####.#",
        "#.#..#.#",
        "#.#..#.#",
        "#.####.#",
        "#......#",
        "########",
    ),
    CATEGORY_PROCESSOR_CONNECTOR: (
        "........",
        "..####..",
        "..####..",
        "........",
        "..####..",
        "..####..",
        "........",
        "........",
    ),
    CATEGORY_MEMORY: (
        "........",
        ".#.#.#.#",
        ".#.#.#.#",
        ".#.#.#.#",
        ".#.#.#.#",
        ".#.#.#.#",
        "#.#.#.#.",
        "........",
    ),
    CATEGORY_ASSEMBLY: (
        "...##...",
        "..####..",
        ".######.",
        "########",
        "#......#",
        "#.####.#",
        "#......#",
        "########",
    ),
    CATEGORY_ENERGY_INPUT: (
        "...###..",
        "..###...",
        ".###....",
        "########",
        "...###..",
        "..###...",
        ".###....",
        "........",
    ),
}
FALLBACK_GLYPH = (
    "........",
    ".######.",
    ".#....#.",
    ".#....#.",
    ".#....#.",
    ".#....#.",
    ".######.",
    "........",
)


def parse_color(text):
    text = text.lstrip("#")
    return (
        int(text[0:2], 16),
        int(text[2:4], 16),
        int(text[4:6], 16),
        255 if len(text) < 8 else int(text[6:8], 16),
    )


def blend(base, tint, ratio):
    """按比例混合两色，ratio=0 返回 base。"""
    return tuple(
        int(round(base[i] + (tint[i] - base[i]) * ratio)) for i in range(3)
    ) + (255,)


def write_png(path, pixels):
    """把 SIZE x SIZE 的 RGBA 像素写成 PNG。纯标准库。"""
    height = len(pixels)
    width = len(pixels[0])
    raw = b"".join(
        b"\x00" + b"".join(struct.pack("BBBB", *px) for px in row) for row in pixels
    )

    def chunk(tag, data):
        payload = tag + data
        return (
            struct.pack(">I", len(data))
            + payload
            + struct.pack(">I", zlib.crc32(payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    blob = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "wb") as handle:
        handle.write(blob)


def build_pixels(category, index_in_category):
    """16x16 占位图：底色 + 深色边框 + 分类图形 + 序号圆点。"""
    base_hex, border_hex, glyph_hex = PALETTE.get(category, FALLBACK_PALETTE)
    base = parse_color(base_hex)
    border = parse_color(border_hex)
    glyph = parse_color(glyph_hex)
    # 顶部一条略亮的横带，让同类方块在物品栏里更容易区分
    band = blend(base, glyph, 0.25)

    rows = []
    for y in range(SIZE):
        row = []
        for x in range(SIZE):
            if x == 0 or y == 0 or x == SIZE - 1 or y == SIZE - 1:
                row.append(border)
            elif y < 3:
                row.append(band)
            else:
                row.append(base)
        rows.append(row)

    pattern = GLYPHS.get(category, FALLBACK_GLYPH)
    for gy, line in enumerate(pattern):
        for gx, mark in enumerate(line):
            if mark == "#":
                rows[4 + gy][4 + gx] = glyph

    # 右下角的序号圆点：同分类内第几个（0-4 个点）
    pips = min(index_in_category, 4)
    for index in range(pips):
        px = SIZE - 3 - index * 2
        py = SIZE - 3
        if 0 < px < SIZE - 1:
            rows[py][px] = glyph

    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成 EnderNet 占位贴图")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的贴图")
    use_utf8_stdout()
    args = parser.parse_args(argv)

    definitions = DefinitionSet()
    by_category = {}
    for device_id in sorted(definitions.devices):
        # 复用已有美术的方块不需要占位图，否则会留下没人引用的散图。
        if texture_key(device_id) in EXISTING_TEXTURE_FILES:
            continue
        by_category.setdefault(definitions.category_of(device_id), []).append(device_id)

    written = []
    skipped = []
    for category in sorted(by_category):
        for index, device_id in enumerate(by_category[category]):
            name = short_name(device_id)
            path = os.path.join(OUT_DIR, name + ".png")
            if os.path.isfile(path) and not args.force:
                skipped.append(name)
                continue
            write_png(path, build_pixels(category, index))
            written.append(name)

    print("已生成 %d 张占位贴图，跳过 %d 张已存在的" % (len(written), len(skipped)))
    print("这些是**占位图，不是成品美术**；用同名真实贴图覆盖即可，无需改动 JSON。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
