# coding=utf-8
"""设备生命周期、归一化事件队列与调度原语。

门面：只导出包外生产代码在用的名字（见 `__all__`）；包内模块之间写模块路径，不经本门面（`test_package_layout` 守着）。
"""

from .events import EventKind, EventQueue, NormalizedEvent, WakeSet
from .lifecycle import ChangeKind, DeviceLifecycle
from .scheduler import (
    DeviceLane,
    DueQueue,
    LogicalClock,
    PollOutcome,
    ScheduledTask,
    SchedulerPolicy,
    TaskKey,
    TaskKind,
)

__all__ = [
    "EventKind",
    "EventQueue",
    "NormalizedEvent",
    "WakeSet",
    "ChangeKind",
    "DeviceLifecycle",
    "DeviceLane",
    "DueQueue",
    "LogicalClock",
    "PollOutcome",
    "ScheduledTask",
    "SchedulerPolicy",
    "TaskKey",
    "TaskKind",
]
