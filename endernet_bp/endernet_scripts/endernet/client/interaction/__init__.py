# coding=utf-8
"""伪原版交互的客户端一侧：把右键（方块使用 / 物品使用）接管成我们的动作——一次按下的判定、
接管后整次按下都拦、组射线、挥手（`takeover.py` 的模块说明）。光缆与面板的意图通道
（`client/channels/cable_intent.py`）是第一个使用者，以后的总线面板、P2P 面板照它接。

门面：只导出包外生产代码在用的名字（见 `__all__`）；包内模块之间写模块路径，不经本门面（`test_package_layout` 守着）。
"""

from .engine import EngineView
from .takeover import UseTakeover

__all__ = [
    "EngineView",
    "UseTakeover",
]
