# coding=utf-8
"""旁路通道的协议半边：事件类与载荷编解码（设备身份下行、槽位模块显示、选择器目录）。

门面：只导出包外生产代码在用的名字（见 `__all__`）；包内模块之间写模块路径，不经本门面（`test_package_layout` 守着）。
同名不导出（各模块各有一个，写模块路径）：`build_event_classes`。
"""

from .device_identity import identity_payload, parse_identity
from .fluid_catalog import build_fluid_catalog
from .slot_display import (
    DISK,
    EMPTY,
    FACING_VARIABLE,
    MEMORY,
    OFFLINE_OFFSET,
    PANEL,
    UNKNOWN,
    facing_yaw,
    kind_of,
    model_items,
    model_value,
    parse_request,
    parse_update,
    request_payload,
    slot_count,
    slot_variable,
    update_payload,
)

__all__ = [
    "identity_payload",
    "parse_identity",
    "build_fluid_catalog",
    "DISK",
    "EMPTY",
    "FACING_VARIABLE",
    "MEMORY",
    "OFFLINE_OFFSET",
    "PANEL",
    "UNKNOWN",
    "facing_yaw",
    "kind_of",
    "model_items",
    "model_value",
    "parse_request",
    "parse_update",
    "request_payload",
    "slot_count",
    "slot_variable",
    "update_payload",
]
