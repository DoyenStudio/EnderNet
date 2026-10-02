# coding=utf-8
"""设备身份下行的引擎侧实现（11-07）。

**这是身份通道唯一的引擎出口。** 载荷的编解码在 `common/device_identity.py`
（那一份不 import 引擎、两侧共用），本模块只负责把它交出去。

引擎 import 一律在**方法体/函数体内**，与 `server/adapters/events.py`
同一手法：适配器模块必须能离线导入，否则整条链路在没有 `mod.server.*`
的环境里连构造都构造不了（`test_import_boundaries.py` 也逐文件扫这一条）。

## 为什么用 `NotifyToClient` 而不是 `PushUIRequest`

容器类设备的屏幕是**引擎自己**按方块组件打开的，脚本侧的屏幕只是挂上去的
代理。代理类没有 `_screen_cls`，`PushUI` / `CreateUI` 对它必抛
（`tooldelta/ui/general_screen.py`），因此不能借那条路把参数捎过去。
这里发的是一条**无会话的自定义 S2C 事件**，形状与理由见
`common/device_identity.py`。
"""

from ...common.device_identity import build_event_classes

#: 进程内共享的事件类：`引擎 events 模块 -> (DeviceScreenIdentity,)`。
#:
#: 客户端与服务端各持一份（两个方向、两份缓存，与同步通道同理）。服务端
#: 这一侧**只发不收**，因此不存在「多建一份就多一个引擎侧监听器」的问题；
#: 仍然缓存，是为了两侧的构造方式保持一致，将来若有人往这里加监听不至于
#: 踩到那条坑。
_EVENT_CLASSES = {}


def engine_events():
    # type: () -> object
    """引擎的 `tooldelta.events` 模块。导入留在函数体内（见模块 docstring）。"""
    from ....tooldelta import events as events_module

    return events_module


def shared_event_classes(events_module):
    # type: (object) -> tuple
    """进程内共享的事件类；同一个 `events_module` 只建一份。"""
    cached = _EVENT_CLASSES.get(events_module)
    if cached is None:
        cached = build_event_classes(events_module)
        _EVENT_CLASSES[events_module] = cached
    return cached


def send_device_identity(player_id, payload, events_module=None, notify=None):
    # type: (str, dict, object, object) -> bool
    """把一条身份下发推给一个玩家，返回是否真的发了。

    `events_module` 与 `notify` 是离线测试的注入点：不给就现取引擎的。
    `notify` 签名与 `tooldelta.events.notify.NotifyToClient(target, event)` 相同。

    **没有收件人就什么都不做**（返回 False）：`playerId` 为空时
    `NotifyToClient` 会往一个不存在的目标发，症状是日志里一条无主的报错，
    而这一刻的调用方（方块使用事件）本来就该安静地跳过。
    """
    if not player_id:
        return False
    if notify is None:
        from ....tooldelta.events.notify import NotifyToClient

        notify = NotifyToClient
    classes = shared_event_classes(
        events_module if events_module is not None else engine_events()
    )
    notify(player_id, classes[0](payload))
    return True
