# coding=utf-8
"""四个终端屏与终端专用逻辑：显示缓存、长列表、背包、输入、触屏与悬浮提示。

门面：只导出包外生产代码在用的名字（见 `__all__`）；包内模块之间写模块路径，不经本门面（`test_package_layout` 守着）。
"""

from .cache import TerminalCache

__all__ = [
    "TerminalCache",
]
