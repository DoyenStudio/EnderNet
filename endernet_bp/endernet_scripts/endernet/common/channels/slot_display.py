# coding=utf-8
"""槽位模块显示：驱动器里的硬盘、内存槽里的内存条（2026-11-05）。**不 import 引擎**。

## 做法

框体是方块自己的几何体（`endernet_rp/models/blocks/`）。插在槽里的模块由
**自定义方块实体外观**画（资源包 `blocks.json` 的 `client_entity`）：
实体几何里「每个槽 × 每种模块」各有一根骨骼，渲染控制器按 molang 变量
`variable.s<槽号>` 决定这个槽显示哪一根。变量由客户端用
`SetBlockEntityMolangValue` 写，值由服务端按容器读数算好下发。

实体几何、渲染控制器、动画与图集由 `scripts/gen_slot_models.py` 生成，
**本文件是它与运行时共用的那份规则**：设备表、变量名、取值、朝向角。

## 变量取值

| 值 | 含义 | 显示 |
| --- | --- | --- |
| `0` | 空槽 | 什么都不画 |
| `1` | 有东西但认不出型号（例如载体记录缺失的孤儿介质） | 默认模块 |
| `2 + i` | 该类物品表里第 `i` 个型号 | 有专属模型就画专属的，否则画默认模块 |

「该类物品表」是 `DefinitionSet.item_mediums`（硬盘）或 `item_memories`（内存条），
顺序即定义表顺序。生成器与运行时读的是同一份，因此**加型号不用改这里**；
定义表改了顺序而没重跑生成器，`test_slot_models` 会红。

**只显示「设备认得的」那些。** 驱动器里叠放、重复、载体读不出的盘，
内存槽里不是内存条的东西，都不进读数，因而显示为空槽——显示与网络
实际在用的内容一致，而不是与容器里的实物一致。

## 下发通道

- C2S `endernet:slot_display_request`：客户端的方块实体外观加载好了
  （`ModBlockEntityLoadedClientEvent`），要这台设备的当前读数。
- S2C `endernet:slot_display`：一台设备的读数。服务端在读数变化时**广播**，
  回答请求时**单发**。客户端只对自己已加载的位置生效。

无会话、无身份：读数本来就是任何看得见这台方块的人都看得见的东西。
"""

from ..definitions import MEMORY_SLOT_COUNT, STORAGE_DRIVER_SLOT_COUNT
from ..identity import NodeKey
from ..protocol import _require_fields

#: 模块种类
DISK = "disk"
MEMORY = "memory"

#: 设备 -> (模块种类, 方块实体外观的实体标识)。生成器与 `gen_blocks.py` 也读这张表。
DISPLAY_DEVICES = {
    "endernet:storage_driver": (DISK, "endernet:storage_driver_modules"),
    "endernet:memory_slot": (MEMORY, "endernet:memory_slot_modules"),
}

#: 变量取值，见模块 docstring
EMPTY = 0
UNKNOWN = 1
FIRST_MODEL = 2

#: 朝向角变量。实体整体按它绕 Y 轴转（动画 `animation.endernet.slot_modules.facing`）。
FACING_VARIABLE = "variable.facing_yaw"

#: 朝向 -> 角度。**与 `scripts/gen_blocks.py` 的 `FACING_ROTATIONS` 同值**
#: （方块用 `minecraft:transformation` 转，实体用骨骼转，两者要转到同一个方向）。
#: 未验证：骨骼旋转与方块旋转的正负号是否一致。真机上若东西两向装反，
#: 只改这里的 east / west 两项即可（`test_slot_display` 只钉住它与方块表的关系）。
FACING_YAW = {
    "north": 0,
    "south": 180,
    "east": -90,
    "west": 90,
}

SLOT_DISPLAY_REQUEST = "endernet:slot_display_request"
SLOT_DISPLAY_UPDATE = "endernet:slot_display"

REQUEST_FIELDS = ("pos", "dimension")
UPDATE_FIELDS = ("pos", "dimension", "facing", "slots")


def slot_variable(slot):
    # type: (int) -> str
    """第 `slot` 个槽的 molang 变量名。"""
    return "variable.s%d" % (slot,)


def kind_of(device_id):
    # type: (str) -> str
    """设备的模块种类；不显示槽位模块的设备返回 None。"""
    spec = DISPLAY_DEVICES.get(device_id)
    return spec[0] if spec is not None else None


def slot_count(kind):
    # type: (str) -> int
    """这一种设备显示几个槽。与方块容器格数读同一条定义值。"""
    value = STORAGE_DRIVER_SLOT_COUNT.value if kind == DISK else MEMORY_SLOT_COUNT.value
    return int(value)


def model_items(definitions, kind):
    # type: (object, str) -> tuple
    """这一种模块对应的物品表（顺序即取值顺序）。"""
    if kind == DISK:
        return tuple(definitions.item_mediums)
    return tuple(definitions.item_memories)


def model_value(items, item_id):
    # type: (tuple, str) -> int
    """一个槽的变量值。`item_id` 为空 = 空槽；不在表里 = 认不出型号。"""
    if not item_id:
        return EMPTY
    if item_id in items:
        return FIRST_MODEL + items.index(item_id)
    return UNKNOWN


def facing_yaw(facing):
    # type: (str) -> int
    """朝向角。认不出的朝向按 north（0°）处理——宁可转错，不可不画。"""
    return FACING_YAW.get(facing, 0)


# -- 载荷 ------------------------------------------------------------------


def request_payload(node_key):
    # type: (NodeKey) -> dict
    return {"pos": list(node_key.as_tuple()), "dimension": node_key.dimension}


def parse_request(data):
    # type: (dict) -> NodeKey
    """解出请求的位置。形状不对就抛（与 `parse_identity` 同一纪律）。"""
    _require_fields(data, REQUEST_FIELDS, "slot_display_request")
    return _node_key(data)


def update_payload(node_key, facing, slots):
    # type: (NodeKey, str, tuple) -> dict
    """服务端侧：编成线上字典。**唯一的编码点**。"""
    return {
        "pos": list(node_key.as_tuple()),
        "dimension": node_key.dimension,
        "facing": facing or "",
        "slots": [int(value) for value in slots],
    }


def parse_update(data):
    # type: (dict) -> tuple
    """客户端侧：解出 `(NodeKey, facing, slots)`。形状不对就抛。"""
    _require_fields(data, UPDATE_FIELDS, "slot_display")
    node_key = _node_key(data)
    facing = data["facing"]
    if not isinstance(facing, ("".__class__, u"".__class__)):
        raise TypeError("facing 必须是文本，得到 %r" % (facing,))
    slots = data["slots"]
    if not isinstance(slots, (list, tuple)):
        raise TypeError("slots 必须是列表，得到 %r" % (type(slots),))
    values = []
    for value in slots:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError("slots 里有非法取值 %r" % (value,))
        values.append(int(value))
    return node_key, facing, tuple(values)


def _node_key(data):
    # type: (dict) -> NodeKey
    pos = data["pos"]
    if not isinstance(pos, (list, tuple)) or len(pos) != 3:
        raise ValueError("pos 必须是三个整数，得到 %r" % (pos,))
    # 整数校验交给 NodeKey（它对 bool 与非整数一律拒绝）。
    return NodeKey(data["dimension"], pos[0], pos[1], pos[2])


# -- 事件类 ----------------------------------------------------------------


def build_event_classes(events):
    # type: (object) -> tuple
    """以 `events` 模块为基类来源，构造 `(请求, 下发)` 两个事件类。

    **一个进程里每侧只能建一份**，理由见 `common/channels/device_identity.py` 的同名函数；
    两侧适配器各自用 `shared_event_classes()` 缓存。原始字典原样带着，
    解析在收件方（那里才有坏包计数的上下文）。
    """

    class SlotDisplayRequest(events.CustomC2SEvent):
        """客户端 → 服务端：「这台设备的方块实体外观刚加载，给我它的读数」。"""

        name = SLOT_DISPLAY_REQUEST

        def __init__(self, payload=None, player_id=None):
            self.payload = payload if isinstance(payload, dict) else {}
            #: 引擎附的发送者（`__id__`）。**只认它**，不认消息体里自称的身份。
            self.player_id = player_id

        def marshal(self):
            # type: () -> dict
            return dict(self.payload)

        @classmethod
        def unmarshal(cls, data):
            payload = dict(data) if isinstance(data, dict) else {}
            player_id = payload.pop("__id__", None)
            return cls(payload, player_id)

    class SlotDisplayUpdate(events.CustomS2CEvent):
        """服务端 → 客户端：一台设备的槽位模块读数。"""

        name = SLOT_DISPLAY_UPDATE

        def __init__(self, payload=None):
            self.payload = payload if isinstance(payload, dict) else {}

        def marshal(self):
            # type: () -> dict
            return self.payload

        @classmethod
        def unmarshal(cls, data):
            return cls(data)

    return SlotDisplayRequest, SlotDisplayUpdate
