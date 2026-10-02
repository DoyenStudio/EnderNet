# coding=utf-8
"""设备界面的身份下发（11-07）。**不 import 引擎**。

## 为什么需要这条通道

容器类设备（总线接口、装配仓一类）的界面由**引擎**按方块组件
`netease:block_container` 自动打开，脚本侧的屏幕是挂上去的代理
（`@RegistToolDeltaScreen(screen, is_proxy=True)`）。代理屏**拿不到任何参数**
（`tooldelta/ui/general_screen.py` 的 `_register_as_proxy` 把
`_initial_params` 置 `None`），因此它不知道自己是哪台方块。

而会话要的 `DeviceRef` 里的 `generation` 由服务端发号
（`common/identity.py` 的 `GenerationAllocator`），**客户端编不出来**。
于是身份只能由服务端在玩家右键那一刻查到注册表之后推过去。

## 为什么不搭在同步通道上

身份必须在**开会话之前**到达，而同步通道的每一条消息都要带会话号
（`SyncEnvelope._validate`，只有 `error` 例外）。会话又要等屏幕采纳了身份
才开得起来——先有会话再有身份是个循环。因此这是一条**独立的、无会话的**
下行通道，形状照 `common/item_catalog.py`（那里也是同一处境：目录要在
任何会话之前拿到）。

## 载荷

```
{"key": <界面键>, "device_ref": {"node_key": {...}, "generation": n}, "dimension": n}
```

`key` 就是 `@RegistToolDeltaScreen` 注册的那串，也是设备 JSON 里
`namespace.main` 的屏幕名——客户端拿它与自己屏的 `_screen_key` 比对，
不匹配就不采纳。

字段是**严格**的（缺项与多项都拒），理由与同步通道一样：多出来一个字段
说明两侧对协议的理解已经分叉，静默忽略只会让它继续分叉下去。
"""

from .identity import DeviceRef
from .protocol import DEVICE_IDENTITY_CHANNEL_S2C, _require_fields, require_text
from .resource_key import _require_count

#: 线上字段。序号在这里，`_require_fields` 逐条对。
IDENTITY_FIELDS = ("key", "device_ref", "dimension")


def identity_payload(ui_key, device_ref, dimension):
    # type: (str, DeviceRef, int) -> dict
    """服务端侧：编成线上字典。**这是唯一的编码点**。

    编码在这里而不是在发送器里，两侧才共用同一份规则（与
    `common/item_catalog.py` 的 `marshal` 同一分工）。
    """
    if not isinstance(device_ref, DeviceRef):
        raise TypeError("device_ref 必须是 DeviceRef，得到 %r" % (type(device_ref),))
    require_text(ui_key, "key")
    _require_count(dimension, "dimension")
    return {
        "key": ui_key,
        "device_ref": device_ref.to_dict(),
        "dimension": dimension,
    }


def parse_identity(data):
    # type: (dict) -> tuple
    """客户端侧：解出 `(ui_key, DeviceRef, dimension)`。

    **形状不对就抛**（`ValueError` / `TypeError` / `KeyError`），由调用方
    `DeviceIdentityLatch.offer` 接住并计数——那里才有「这条包不要了」
    的上下文。这里不返回 `None`：一个静默的空结果与「确实没有身份」在
    调用方看来一模一样，而那正是最难查的那类故障。
    """
    _require_fields(data, IDENTITY_FIELDS, "device_identity")
    key = require_text(data["key"], "key")
    _require_count(data["dimension"], "dimension")
    _require_fields(
        data["device_ref"], ("node_key", "generation"), "device_identity.device_ref"
    )
    return key, DeviceRef.from_dict(data["device_ref"]), data["dimension"]


def build_event_classes(events):
    # type: (object) -> tuple
    """以 `events` 模块为基类来源，构造身份下行的事件类。

    与同步通道的两个类同一形状（`client/network_client.py`
    的 `build_event_classes`）：**原始字典原样带着，不在这里解析**——
    解析在 `DeviceIdentityLatch.offer`，那里才有坏包计数的上下文。

    **一个进程里只能建一份**：`EventBus` 以事件类对象为键判重，每个新类
    都是引擎侧的一次新注册，同名的处理器会被后者覆盖，于是「多建一份」
    的代价是**先注册的那个收不到**（实测见 `client/network_client.py`
    的 `_EVENT_CLASSES` 注释）。客户端侧的 `shared_event_classes()` 管这件事。
    """

    class DeviceScreenIdentity(events.CustomS2CEvent):
        """服务端 → 客户端：「你打开的这个界面是这台设备」。"""

        name = DEVICE_IDENTITY_CHANNEL_S2C

        def __init__(self, payload=None):
            #: 已经编码好的线上字典（`identity_payload` 的结果）。
            self.payload = payload if isinstance(payload, dict) else {}

        def marshal(self):
            # type: () -> dict
            return self.payload

        @classmethod
        def unmarshal(cls, data):
            return cls(data)

    return (DeviceScreenIdentity,)
