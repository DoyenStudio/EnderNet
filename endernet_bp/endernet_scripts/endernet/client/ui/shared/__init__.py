# coding=utf-8
"""跨屏共用、引擎无关的界面工具：槽位几何、堆叠上限、飞行动画、控件 API 的形状坑。

门面：只导出包外生产代码在用的名字（见 `__all__`）；包内模块之间写模块路径，不经本门面（`test_package_layout` 守着）。
"""

from .control_api import on_click, show_item
from .slot_layout import window_control
from .stack_size import id_aux

__all__ = [
    "on_click",
    "show_item",
    "window_control",
    "id_aux",
]
