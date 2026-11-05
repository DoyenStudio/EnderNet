# coding=utf-8
"""脚本共用的 PNG 编码：纯标准库（zlib + struct），不依赖 PIL。"""

import os
import struct
import zlib


def png_bytes(pixels):
    """把二维 RGBA 像素表（`rows[y][x] = (r, g, b, a)`）编码成 PNG 字节串。

    输出是确定的（固定压缩级别、不写时间戳），因此可以拿它与磁盘上的文件
    逐字节比较，判断贴图是否需要重新生成。
    """
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
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def write_png(path, pixels):
    """把像素表写成 PNG 文件，必要时创建目录。"""
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    with open(path, "wb") as handle:
        handle.write(png_bytes(pixels))
