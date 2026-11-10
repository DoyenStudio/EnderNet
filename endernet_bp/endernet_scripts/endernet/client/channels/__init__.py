# coding=utf-8
"""旁路通道的客户端一侧：设备身份认领、槽位模块显示、选择器目录、光缆交互意图（发）。

门面：只导出包外生产代码在用的名字（见 `__all__`）；包内模块之间写模块路径，不经本门面（`test_package_layout` 守着）。
"""

from .cable_intent import get_cable_intent, start_cable_intent, stop_cable_intent
from .device_identity import get_device_identity, latch, start_device_identity, stop_device_identity
from .item_catalog import get_catalog, start_catalog, stop_catalog
from .slot_display import get_slot_display, start_slot_display, stop_slot_display

__all__ = [
    "get_cable_intent",
    "start_cable_intent",
    "stop_cable_intent",
    "get_device_identity",
    "latch",
    "start_device_identity",
    "stop_device_identity",
    "get_catalog",
    "start_catalog",
    "stop_catalog",
    "get_slot_display",
    "start_slot_display",
    "stop_slot_display",
]
