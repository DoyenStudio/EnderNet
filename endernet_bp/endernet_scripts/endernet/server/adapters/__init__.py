# coding=utf-8
"""引擎边界：唯一可以 import tooldelta / `mod.*` 的服务端目录，且只在函数体内（`test_import_boundaries` 守着）。

门面：只导出包外生产代码在用的名字（见 `__all__`）；包内模块之间写模块路径，不经本门面（`test_package_layout` 守着）。
`engine_text` 不导出：函数名与子模块同名，导出后 `pkg.engine_text` 会变成函数。
"""

from .events import ServerEventBinder
from .interaction import engine_play_sound
from .messages import AdapterSyncPort
from .order_notifications import notify_order_completed
from .persistence_engine import EngineExtraDataBackend
from .recipes import EngineRecipeSource
from .template_tips import resource_display_name
from .world import ToolDeltaWorldAdapter

__all__ = [
    "ServerEventBinder",
    "engine_play_sound",
    "AdapterSyncPort",
    "notify_order_completed",
    "EngineExtraDataBackend",
    "EngineRecipeSource",
    "resource_display_name",
    "ToolDeltaWorldAdapter",
]
