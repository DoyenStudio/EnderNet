# coding=utf-8
"""容器契约与真实容器/玩家背包的引擎实现。

门面：只导出包外生产代码在用的名字（见 `__all__`）；包内模块之间写模块路径，不经本门面（`test_package_layout` 守着）。
"""

from .contract import ContainerAdapter, ContainerEntry
from .engine import (
    EngineContainerProvider,
    EngineInventoryProvider,
    EngineItemPrimitives,
    build_stack_limit,
)

__all__ = [
    "ContainerAdapter",
    "ContainerEntry",
    "EngineContainerProvider",
    "EngineInventoryProvider",
    "EngineItemPrimitives",
    "build_stack_limit",
]
