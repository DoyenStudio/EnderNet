# coding=utf-8
"""设备注册、能力契约与各设备的状态集合（驱动器、内存槽、总线接口、槽位显示、光缆与面板、
P2P 隧道与无线连接器）。

门面：只导出包外生产代码在用的名字（见 `__all__`）；包内模块之间写模块路径，不经本门面（`test_package_layout` 守着）。
"""

from .capabilities import CapabilityRegistry, EnergyPort, PATTERN_PROVIDER
from .bus_io import (
    FILTER_MODES,
    INTAKE,
    MODE_WHITELIST,
    OUTPUT,
    PASSIVE,
    PortConfig,
    PortDispatcher,
    PortGateway,
    PortPolicy,
    PortRunner,
    PortStore,
    ROLE_FILTER_MODES,
    TEMPLATE,
    config_view,
    is_port_device,
    port_blocker,
    role_of,
)
from .catalog import DeviceCatalog
from .cable_intents import CableIntentService
from .cables import CableStateKeeper
from .memory_slot import MemorySlotStore
from .panels import PanelService
from .registry import DeviceRegistry
from .slot_display import SlotDisplayTracker
from .storage_driver import DRIVER_DEVICE_ID, DriverPolicy, StorageDriverStore, is_storage_driver
from .tunnels import TunnelService
from .wireless import WirelessService

__all__ = [
    "CapabilityRegistry",
    "EnergyPort",
    "PATTERN_PROVIDER",
    "FILTER_MODES",
    "INTAKE",
    "MODE_WHITELIST",
    "OUTPUT",
    "PASSIVE",
    "PortConfig",
    "PortDispatcher",
    "PortGateway",
    "PortPolicy",
    "PortRunner",
    "PortStore",
    "ROLE_FILTER_MODES",
    "TEMPLATE",
    "config_view",
    "is_port_device",
    "port_blocker",
    "role_of",
    "DeviceCatalog",
    "CableIntentService",
    "CableStateKeeper",
    "MemorySlotStore",
    "PanelService",
    "DeviceRegistry",
    "SlotDisplayTracker",
    "DRIVER_DEVICE_ID",
    "DriverPolicy",
    "StorageDriverStore",
    "is_storage_driver",
    "TunnelService",
    "WirelessService",
]
