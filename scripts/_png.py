# coding=utf-8
"""脚本共用的 PNG 编解码：纯标准库（zlib + struct），不依赖 PIL。"""

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


def read_png(path):
    """读 PNG，返回 `rows[y][x] = (r, g, b, a)`。只认 8 位、非隔行的
    RGBA / RGB / 调色板图（Blockbench 与本仓库脚本导出的都是这几种）。"""
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("%s 不是 PNG" % path)
    offset = 8
    header = None
    palette = []
    alpha = b""
    idat = []
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset:offset + 4])
        tag = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + length]
        offset += 12 + length
        if tag == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif tag == b"PLTE":
            palette = [tuple(bytearray(body[i:i + 3])) for i in range(0, len(body), 3)]
        elif tag == b"tRNS":
            alpha = body
        elif tag == b"IDAT":
            idat.append(body)
        elif tag == b"IEND":
            break
    width, height, depth, color, _compression, _filter, interlace = header
    channels = {6: 4, 2: 3, 3: 1}.get(color)
    if depth != 8 or channels is None or interlace:
        raise ValueError(
            "%s：只支持 8 位非隔行的 RGBA/RGB/调色板 PNG（位深 %d，颜色类型 %d，隔行 %d）"
            % (path, depth, color, interlace)
        )
    raw = bytearray(zlib.decompress(b"".join(idat)))
    stride = width * channels
    rows = []
    previous = bytearray(stride)
    position = 0
    for _ in range(height):
        kind = raw[position]
        line = raw[position + 1:position + 1 + stride]
        position += 1 + stride
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = previous[i]
            corner = previous[i - channels] if i >= channels else 0
            if kind == 1:
                line[i] = (line[i] + left) & 0xFF
            elif kind == 2:
                line[i] = (line[i] + up) & 0xFF
            elif kind == 3:
                line[i] = (line[i] + ((left + up) >> 1)) & 0xFF
            elif kind == 4:
                estimate = left + up - corner
                pa, pb, pc = abs(estimate - left), abs(estimate - up), abs(estimate - corner)
                best = left if pa <= pb and pa <= pc else (up if pb <= pc else corner)
                line[i] = (line[i] + best) & 0xFF
        previous = line
        row = []
        for x in range(width):
            if color == 6:
                row.append(tuple(line[x * 4:x * 4 + 4]))
            elif color == 2:
                row.append(tuple(line[x * 3:x * 3 + 3]) + (255,))
            else:
                index = line[x]
                opacity = bytearray(alpha)[index] if index < len(alpha) else 255
                row.append(palette[index] + (opacity,))
        rows.append(row)
    return rows
