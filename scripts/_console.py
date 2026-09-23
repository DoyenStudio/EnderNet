# coding=utf-8
"""脚本共用的控制台辅助。"""
from __future__ import absolute_import, division, print_function

import sys


def use_utf8_stdout():
    # type: () -> None
    """Windows 控制台默认 GBK，中文输出会变成乱码。强制 UTF-8。

    errors='replace' 保证即使外层仍是 GBK 也只损失个别字符，
    不会抛 UnicodeEncodeError 把整个脚本打断。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, IOError):
            pass
