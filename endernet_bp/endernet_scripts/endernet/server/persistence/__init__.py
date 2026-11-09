# coding=utf-8
"""存档契约、记录与写协调、恢复闸门、版本迁移与终端库存存档。

门面：只导出包外生产代码在用的名字（见 `__all__`）；包内模块之间写模块路径，不经本门面（`test_package_layout` 守着）。
"""

from .recovery import RecoveryGate, assert_consistent, consistency_report
from .repository import ExtraDataRepository, Record
from .terminals import TerminalInventoryArchive

__all__ = [
    "RecoveryGate",
    "assert_consistent",
    "consistency_report",
    "ExtraDataRepository",
    "Record",
    "TerminalInventoryArchive",
]
