# coding=utf-8
"""存储介质、转移端点与中转缓冲、终端显示投影，以及每网络一份的存储服务。

门面：只导出包外生产代码在用的名字（见 `__all__`）；包内模块之间写模块路径，不经本门面（`test_package_layout` 守着）。
`service.py` 暂不进门面：集成模块，顶层导入 devices/crafting；进门面会让 devices↔storage 的包级环变成导入期的模块级环。拆分 `service.py` 后再纳入。
"""

from .medium import (
    DIRECTION_READ,
    DIRECTION_WRITE,
    Medium,
    MediumPolicy,
    MediumRecovery,
    TransferBudget,
)
from .projection import NetworkItemProjection
from .transfer import (
    BufferDisposition,
    ContainerEndpoint,
    InventoryEndpoint,
    SlotView,
    TransferBuffer,
    TransferPolicy,
    receipt_result,
    slot_extract,
    slot_insert,
    slot_limit_of,
    slot_read,
)

__all__ = [
    "DIRECTION_READ",
    "DIRECTION_WRITE",
    "Medium",
    "MediumPolicy",
    "MediumRecovery",
    "TransferBudget",
    "NetworkItemProjection",
    "BufferDisposition",
    "ContainerEndpoint",
    "InventoryEndpoint",
    "SlotView",
    "TransferBuffer",
    "TransferPolicy",
    "receipt_result",
    "slot_extract",
    "slot_insert",
    "slot_limit_of",
    "slot_read",
]
