# coding=utf-8
"""设备通过节点暴露的能力接口。

对应任务 01-04/02-01 的能力部分，参考 AE2 的 `IGridNode.addService/getService`。

这里**只用文档化的鸭子类型**，不用 ABC、不做 isinstance 校验：
网络服务在节点加入时查询它关心的方法，节点离开时解除挂载。
这样做的好处是设备类不需要 import 任何服务，服务也不需要 import 任何设备类。

约定：能力实现由设备侧提供（真实设备在后续任务的设备类里），
本模块只定义**名字与语义**，以及一个用于装配期自检的注册表。
"""
from __future__ import absolute_import, division, print_function

#: 能力名。与 `common/definitions.py` 的 CAP_* 常量必须一致。
STORAGE_PROVIDER = "storage_provider"
PATTERN_PROVIDER = "pattern_provider"
ITEM_PORT = "item_port"
FLUID_PORT = "fluid_port"
ENERGY_PORT = "energy_port"
TERMINAL_ENDPOINT = "terminal_endpoint"
PROCESSING_SLOTS = "processing_slots"
MEMORY = "memory"

ALL_CAPABILITIES = frozenset(
    [
        STORAGE_PROVIDER,
        PATTERN_PROVIDER,
        ITEM_PORT,
        FLUID_PORT,
        ENERGY_PORT,
        TERMINAL_ENDPOINT,
        PROCESSING_SLOTS,
        MEMORY,
    ]
)


class StorageProvider(object):
    """存储提供者（任务 05 实现）。

    存储驱动器通过它把介质挂载到网络的存储服务上。

    必须提供：
      - `list_mediums()` -> list          当前装载的介质句柄
      - `read(medium_id, resource_key, count, budget)` -> TransferReceipt
      - `write(medium_id, resource_key, count, budget)` -> TransferReceipt
      - `save_node_data()` -> dict        节点私有存档（含版本）
    """

    def list_mediums(self):
        raise NotImplementedError

    def read(self, medium_id, resource_key, count, budget):
        raise NotImplementedError

    def write(self, medium_id, resource_key, count, budget):
        raise NotImplementedError

    def save_node_data(self):
        raise NotImplementedError


class PatternProvider(object):
    """样板提供者（任务 07 实现）。

    总线样板接口与装配仓通过它把样板注册给工厂服务。
    工厂样板**不记录**外部机器类型，机器种类由该接口所连接的设备决定。

    必须提供：
      - `list_patterns()` -> list
      - `push_inputs(execution_id, entries)` -> TransferReceipt
      - `accept_returns(execution_id, entries)` -> TransferReceipt
    """

    def list_patterns(self):
        raise NotImplementedError

    def push_inputs(self, execution_id, entries):
        raise NotImplementedError

    def accept_returns(self, execution_id, entries):
        raise NotImplementedError


class ItemPort(object):
    """物品端口（任务 06 实现）。

    必须提供：`extract(op_id, resource_key, count) -> TransferReceipt`、
             `insert(op_id, resource_key, count) -> TransferReceipt`。
    """

    def extract(self, op_id, resource_key, count):
        raise NotImplementedError

    def insert(self, op_id, resource_key, count):
        raise NotImplementedError


class FluidPort(object):
    """流体端口（任务 06/13 实现）。语义与 `ItemPort` 相同，单位是 mB。"""

    def extract(self, op_id, resource_key, count):
        raise NotImplementedError

    def insert(self, op_id, resource_key, count):
        raise NotImplementedError


class EnergyPort(object):
    """能源端口（任务 04/13-02 实现）。

    RF 换算待定（R08），因此接口按「实际接收多少 RF、换出多少 EN、
    余下多少」三分量返回，保留整数余数，避免反复转换获利。

    必须提供：`accept_rf(rf) -> (accepted_rf, converted_en, remainder)`
    """

    def accept_rf(self, rf):
        raise NotImplementedError


class TerminalEndpoint(object):
    """终端端点（任务 11 实现）。

    必须提供：`open_session(session)`、`close_session(session_id)`。
    """

    def open_session(self, session):
        raise NotImplementedError

    def close_session(self, session_id):
        raise NotImplementedError


class ProcessingSlots(object):
    """处理槽位来源（任务 02-04/09 实现）。

    并行处理器经连接器接入网络后提供槽位。**未接入连接器的处理器贡献零槽位**，
    由 `network/node.py` 的处理器群解析负责判定。

    必须提供：`slot_count()` -> int、`claim_slot(execution_id)` -> bool、
             `release_slot(execution_id)`。
    """

    def slot_count(self):
        raise NotImplementedError

    def claim_slot(self, execution_id):
        raise NotImplementedError

    def release_slot(self, execution_id):
        raise NotImplementedError


class Memory(object):
    """配方运行内存来源（任务 09 实现，规格待定 R04）。

    必须提供：`memory_units()` -> int。
    """

    def memory_units(self):
        raise NotImplementedError


class CapabilityRegistry(object):
    """节点上的能力注册表。

    只做「名字 -> 实现」的存放与替换；不做类型校验，也不查找服务。
    重复注册同名能力会明确报错，避免两个实现互相覆盖后行为莫名改变。
    """

    __slots__ = ("_entries",)

    def __init__(self):
        self._entries = {}

    def register(self, name, implementation):
        # type: (str, object) -> None
        if name not in ALL_CAPABILITIES:
            raise ValueError("未定义的能力名: %r" % (name,))
        if implementation is None:
            raise ValueError("能力实现不能为 None: %s" % (name,))
        if name in self._entries:
            raise ValueError("能力 %s 已注册；请先卸载再替换" % (name,))
        self._entries[name] = implementation

    def unregister(self, name):
        # type: (str) -> object
        return self._entries.pop(name, None)

    def get(self, name):
        # type: (str) -> object
        return self._entries.get(name)

    def has(self, name):
        # type: (str) -> bool
        return name in self._entries

    def names(self):
        # type: () -> tuple
        return tuple(sorted(self._entries.keys()))

    def clear(self):
        # type: () -> int
        count = len(self._entries)
        self._entries = {}
        return count

    def __len__(self):
        return len(self._entries)
