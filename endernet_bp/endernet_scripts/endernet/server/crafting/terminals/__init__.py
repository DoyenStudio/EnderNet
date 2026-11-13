# coding=utf-8
"""合成终端工作台、样板终端的设备侧状态、实体槽原语与终端交互用例（11-04、11-05）。

门面：只导出包外生产代码在用的名字（见 `__all__`）；包内模块之间写模块路径，不经本门面（`test_package_layout` 守着）。
"""

from .actions import TerminalActions
from .template import (
    CRAFT_PREVIEW_CELL,
    GROUP_OUTPUTS,
    GROUP_SOURCE,
    INPUT_SLOT,
    OUTPUT_CELL_COUNT,
    RESULT_SLOT,
    SOURCE_CELL_COUNT,
    TEMPLATE_SCHEMA_VERSION,
    TemplatePolicy,
    TemplateStore,
    detect_crafting_output,
    encode_template_item,
)
from .workbench import (
    CraftingPolicy,
    WORKBENCH_SCHEMA_VERSION,
    WorkbenchStore,
    preview_of,
)

__all__ = [
    "TerminalActions",
    "CRAFT_PREVIEW_CELL",
    "GROUP_OUTPUTS",
    "GROUP_SOURCE",
    "INPUT_SLOT",
    "OUTPUT_CELL_COUNT",
    "RESULT_SLOT",
    "SOURCE_CELL_COUNT",
    "TEMPLATE_SCHEMA_VERSION",
    "TemplatePolicy",
    "TemplateStore",
    "detect_crafting_output",
    "encode_template_item",
    "CraftingPolicy",
    "WORKBENCH_SCHEMA_VERSION",
    "WorkbenchStore",
    "preview_of",
]
