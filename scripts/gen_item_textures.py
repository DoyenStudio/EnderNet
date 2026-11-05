# coding=utf-8
"""按美术基调为样板与内存物品生成 16x16 图标（亮色，AE2 向）。

    python scripts/gen_item_textures.py                 # 只补缺失的
    python scripts/gen_item_textures.py --force         # 全部重画（会覆盖手绘图！）
    python scripts/gen_item_textures.py --theme dark    # 暗色调色板
    python scripts/gen_item_textures.py --check         # 只列出缺失的文件

覆盖的物品：工厂样板 1 种、内存 8 种（三档感应式内存块、四档集成式内存卡、
256KB 内存条）。介质（磁带、软盘）不在这里，仍由 `gen_placeholder_textures.py`
补占位。文件名由 `gen_items.item_texture_name` 决定，与物品图集同源。

**档位怎么读**：
- 感应式内存块：芯片中心亮几格 + 底边亮几颗点 = 第几档（128B / 256B / 512B）；
- 集成式内存卡：卡上有几颗芯片 = 第几档（1KB / 4KB / 16KB / 64KB）；
- 内存条：顶部紫色散热条 + 8 颗芯片，单独一档（256KB）。

图形按 EnderNet 美术基调（亮色、AE2 向）绘制，调色板与绘制器复用
`gen_device_textures.py`。只依赖标准库，不需要 PIL。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _console import use_utf8_stdout  # noqa: E402
from _png import png_bytes  # noqa: E402
from gen_device_textures import (  # noqa: E402
    DEFAULT_THEME,
    SIZE,
    THEMES,
    Tex,
    resolve,
    rgba,
)
from gen_items import item_texture_name  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_ROOT = os.path.join(REPO, "endernet_bp", "endernet_scripts")
ITEM_OUT_DIR = os.path.join(REPO, "endernet_rp", "textures", "items")
if SCRIPTS_ROOT not in sys.path:
    sys.path.insert(0, SCRIPTS_ROOT)

from endernet.common.definitions import DefinitionSet  # noqa: E402

TRANSPARENT = (0, 0, 0, 0)


def pixels_of(tex, theme):
    """物品图标允许留空：未上色的格子是透明的。"""
    rows = []
    for y in range(SIZE):
        row = []
        for x in range(SIZE):
            token = tex.p[y * SIZE + x]
            row.append(TRANSPARENT if token is None else rgba(resolve(token, theme)))
        rows.append(row)
    return rows


def factory_template():
    """工厂样板：带折角的紫色样板卡，3x3 配方点阵，底部青色产物线。"""
    t = Tex()
    t.rect(3, 1, 10, 14, "craft2").box(3, 1, 10, 14, "craft1")
    t.pts(((12, 1), (11, 1), (12, 2)), None).set(11, 2, "craft1")
    for x in (5, 7, 9):
        for y in (4, 6, 8):
            t.set(x, y, "craft0" if (x + y) % 4 == 1 else "craft1")
    t.h(5, 11, 5, "net1").set(10, 11, "net0")
    return t


def sensing_block(tier):
    """感应式内存块（1~3 档）：带引脚的芯片，中心与底边亮点数 = 档位。"""
    t = Tex()
    for p in (5, 7, 8, 10):
        t.v(p, 2, 2, "k5").v(p, 12, 2, "k5").h(2, p, 2, "k5").h(12, p, 2, "k5")
    t.rect(4, 4, 8, 8, "k3").box(4, 4, 8, 8, "k5")
    t.rect(6, 6, 4, 4, "craft2").rect(7, 7, 2, 2, "craft1")
    for x, y in ((7, 7), (8, 8), (8, 7))[:tier]:
        t.set(x, y, "craft0")
    for i in range(3):
        t.set(5 + i * 3, 15, "craft0" if i < tier else "k4")
    return t


def integrated_card(tier):
    """集成式内存卡（1~4 档）：电路板 + 金手指，芯片颗数 = 档位。"""
    t = Tex()
    t.rect(1, 4, 14, 8, "k4").box(1, 4, 14, 8, "k5")
    for x in (2, 5, 8, 11)[:tier]:
        t.rect(x, 6, 2, 3, "k1").set(x, 6, "craft1")
    for x in range(2, 14, 2):
        t.set(x, 11, "item1")
    t.set(13, 5, "craft0")
    return t


def memory_stick():
    """内存条（256KB）：紫色散热条 + 一排芯片 + 带缺口的金手指。"""
    t = Tex()
    t.rect(0, 5, 16, 6, "k4").box(0, 5, 16, 6, "k5").h(1, 5, 14, "craft1")
    for x in (1, 3, 5, 10, 12, 14):
        t.rect(x, 7, 1, 2, "k1")
    t.pts(((7, 7), (8, 8)), "craft0")
    for x in range(1, 15, 2):
        t.set(x, 10, "item1")
    t.pts(((7, 10), (8, 10)), None)
    return t


#: 物品 -> 画法
DESIGNS = {
    "endernet:factorical_template": factory_template,
    "endernet:memory_128b": lambda: sensing_block(1),
    "endernet:memory_256b": lambda: sensing_block(2),
    "endernet:memory_512b": lambda: sensing_block(3),
    "endernet:memory_1k": lambda: integrated_card(1),
    "endernet:memory_4k": lambda: integrated_card(2),
    "endernet:memory_16k": lambda: integrated_card(3),
    "endernet:memory_64k": lambda: integrated_card(4),
    "endernet:memory_256k": memory_stick,
}


def main(argv=None):
    parser = argparse.ArgumentParser(description="生成 EnderNet 样板与内存物品图标")
    parser.add_argument("--theme", choices=sorted(THEMES), default=DEFAULT_THEME,
                        help="调色板（默认 %s）" % DEFAULT_THEME)
    parser.add_argument("--force", action="store_true", help="覆盖已有文件（包括手绘图）")
    parser.add_argument("--check", action="store_true", help="只列出缺失的文件")
    use_utf8_stdout()
    args = parser.parse_args(argv)

    definitions = DefinitionSet()
    items = list(definitions.item_templates) + list(definitions.item_memories)
    undesigned = [item_id for item_id in items if item_id not in DESIGNS]
    unknown = sorted(set(DESIGNS) - set(items))
    if undesigned or unknown:
        print("画法表与定义表不一致：缺画法 %r，多余 %r" % (undesigned, unknown))
        return 1

    missing = []
    written = []
    for item_id in items:
        path = os.path.join(ITEM_OUT_DIR, item_texture_name(item_id) + ".png")
        exists = os.path.isfile(path)
        if not exists:
            missing.append(os.path.relpath(path, REPO))
        if args.check or (exists and not args.force):
            continue
        if not os.path.isdir(ITEM_OUT_DIR):
            os.makedirs(ITEM_OUT_DIR)
        with open(path, "wb") as handle:
            handle.write(png_bytes(pixels_of(DESIGNS[item_id](), args.theme)))
        written.append(os.path.relpath(path, REPO))

    if args.check:
        if missing:
            print("缺少物品图标（%d 张）：" % len(missing))
            for path in missing:
                print("  " + path)
            return 1
        print("样板与内存图标齐全")
        return 0
    print("已按 %s 主题写入 %d 张物品图标" % (args.theme, len(written)))
    for path in written:
        print("  " + path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
