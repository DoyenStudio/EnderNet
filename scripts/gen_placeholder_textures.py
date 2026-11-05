# coding=utf-8
"""为缺少美术资源的**物品**生成 16x16 占位贴图。

    python scripts/gen_placeholder_textures.py            # 只补缺失的
    python scripts/gen_placeholder_textures.py --force    # 全部重写

**这些是占位图，不是成品美术。** 它们的作用是让物品在游戏里可辨认、
不至于显示为紫黑缺失贴图。用真实贴图覆盖同名文件即可，不需要改任何 JSON。

设备方块的贴图不由本脚本生成，见 `scripts/gen_device_textures.py`。
样板与内存的图标已有美术基调版本（`scripts/gen_item_textures.py`），文件
存在时本脚本会跳过它们；目前只有介质（磁带、软盘）还是占位图。

只依赖标准库（`_png.py` 手写 PNG），因此不需要 PIL。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _console import use_utf8_stdout  # noqa: E402
from _png import write_png  # noqa: E402
from gen_items import item_texture_name  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_ROOT = os.path.join(REPO, "endernet_bp", "endernet_scripts")
#: 介质物品的贴图**必须**单独一个目录。放进 devices/ 会立刻撞上
#: `test_assets.TestResourcePackMapping.test_no_unreferenced_device_textures`——
#: 那条测试拿地形图集的引用去比对该目录下的每张图。
ITEM_OUT_DIR = os.path.join(REPO, "endernet_rp", "textures", "items")

if SCRIPTS_ROOT not in sys.path:
    sys.path.insert(0, SCRIPTS_ROOT)

from endernet.common.definitions import DefinitionSet  # noqa: E402

SIZE = 16

#: 介质物品的占位配色与图形。
ITEM_PALETTE = ("#8B6A3F", "#4A3820", "#E0C79C")
ITEM_GLYPH = (
    "########",
    "#......#",
    "#.####.#",
    "#.####.#",
    "#......#",
    "#.####.#",
    "#......#",
    "########",
)

#: 样板的占位配色与图形。**必须与介质区分开**：两者都是物品、都会被玩家
#: 拿在手里，同一套底色会让「放进去的是盘还是样板」要靠读名字才知道。
#: 用绿色，图形画成网格状——工作台摆位的意象。
TEMPLATE_PALETTE = ("#3F8B4A", "#1F4A26", "#A3E0AC")
TEMPLATE_GLYPH = (
    "########",
    "#.#..#.#",
    "#.#..#.#",
    "#......#",
    "########",
    "#.#..#.#",
    "#.#..#.#",
    "########",
)

#: 内存条的占位配色与图形。**第三套，与介质、样板都不同**（理由同上：
#: 三样都是拿在手里的物品，同底色就只能靠读名字分辨）。用金色，图形画成
#: 记忆体的金手指——一排在边框内的方点，与介质的「盘」、样板的「网格」都不同形。
MEMORY_PALETTE = ("#B5A03F", "#5F5320", "#EDDF9C")
MEMORY_GLYPH = (
    "########",
    "#......#",
    "#.####.#",
    "#.#..#.#",
    "#.####.#",
    "#......#",
    "#.####.#",
    "##.##.##",
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


def render_pixels(palette, pattern, pips):
    """16x16 占位图：底色 + 深色边框 + 图形 + 序号圆点。

    设备方块与介质物品走的是同一个渲染器，只有配色、图形和圆点数不同。
    """
    base_hex, border_hex, glyph_hex = palette
    base = parse_color(base_hex)
    border = parse_color(border_hex)
    glyph = parse_color(glyph_hex)
    # 顶部一条略亮的横带，让同类贴图在物品栏里更容易区分
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

    for gy, line in enumerate(pattern):
        for gx, mark in enumerate(line):
            if mark == "#":
                rows[4 + gy][4 + gx] = glyph

    # 右下角的序号圆点：同组内第几个（0-4 个点）
    for index in range(min(pips, 4)):
        px = SIZE - 3 - index * 2
        py = SIZE - 3
        if 0 < px < SIZE - 1:
            rows[py][px] = glyph

    return rows


def build_item_pixels(index_in_items):
    """介质物品的占位图。"""
    return render_pixels(ITEM_PALETTE, ITEM_GLYPH, index_in_items)


def build_template_pixels(index_in_items):
    """样板物品的占位图。"""
    return render_pixels(TEMPLATE_PALETTE, TEMPLATE_GLYPH, index_in_items)


def build_memory_pixels(index_in_items):
    """内存条的占位图。"""
    return render_pixels(MEMORY_PALETTE, MEMORY_GLYPH, index_in_items)


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成 EnderNet 物品占位贴图")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的贴图")
    use_utf8_stdout()
    args = parser.parse_args(argv)

    definitions = DefinitionSet()

    # 物品：文件名与图集键都由 gen_items.py 的命名规则决定，这里保持一致
    # （`item_texture_name(item_id)`），否则图集指向的路径会落空。
    #
    # 三类物品各用一个 `(清单, 画法)`：序号从 0 重新开始，因此各类物品的
    # 右上角圆点各数各的；配色与图形不同，再把三类本身也区分开。
    item_written = []
    item_skipped = []
    item_groups = (
        (definitions.item_mediums, build_item_pixels),
        (definitions.item_templates, build_template_pixels),
        (definitions.item_memories, build_memory_pixels),
    )
    for item_ids, painter in item_groups:
        for index, item_id in enumerate(item_ids):
            name = item_texture_name(item_id)
            path = os.path.join(ITEM_OUT_DIR, name + ".png")
            if os.path.isfile(path) and not args.force:
                item_skipped.append(name)
                continue
            write_png(path, painter(index))
            item_written.append(name)

    print("已生成 %d 张占位贴图（物品），跳过 %d 张已存在的" % (len(item_written), len(item_skipped)))
    print("这些是**占位图，不是成品美术**；用同名真实贴图覆盖即可，无需改动 JSON。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
