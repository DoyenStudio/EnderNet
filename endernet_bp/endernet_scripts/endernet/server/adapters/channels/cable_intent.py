# coding=utf-8
"""光缆精确交互的引擎出口（阶段 C，`.claude/13` §6）。本模块是**引擎边界**。

规则与载荷在 `common/channels/cable_intent.py`，校验与分派在
`server/devices/cable_intents.py`；本模块只做翻译：

- **收**：C2S `endernet:cable_intent`，经 `ServerEventBinder.register` 挂上——与同步通道
  同一本账，`stop()` 时随其它监听器一起注销。发件人只认引擎附的 `__id__`。
- **查**：处理函数要的玩家事实——所在维度、脚下坐标、主手物品、是不是创造模式；
  放面板时从主手扣一个（阶段 D）。
  配置卡与无线连接工具还要读写主手那一件的 `userData` 与说明（2026-11-13）。
- **回**：处理结果那一句话用弹出提示给发件人（同蔚蓝管道的扳手提示）；同一 tick 多出来的、
  以及登记成静默的动作（挂面板、底座换光缆）没有话，不弹。

引擎 import 一律在函数体内（同 `adapters/channels/slot_display.py`），本模块能离线导入；
每个引擎口子都能注入替身。玩家维度与坐标照用事件适配器与同步通道的那两个实现
（函数体内导入：`adapters/events.py` 在导入期就要导入本包，顶层导回去会成环）。
"""

from ....common.channels.cable_intent import build_event_classes
from ..engine_text import engine_text

#: 进程内共享的事件类：`引擎 events 模块 -> (CableIntentRequest,)`。理由同
#: `adapters/messages.py` 的 `_EVENT_CLASSES`：每建一个新类都是引擎侧的一次新注册。
_EVENT_CLASSES = {}

#: 弹出提示的副标题
NOTICE_TITLE = "§6网络扳手§f"


def engine_events():
    # type: () -> object
    from .....tooldelta import events as events_module

    return events_module


def shared_event_classes(events_module):
    # type: (object) -> tuple
    cached = _EVENT_CLASSES.get(events_module)
    if cached is None:
        cached = build_event_classes(events_module)
        _EVENT_CLASSES[events_module] = cached
    return cached


def engine_player_dimension(player_id):
    # type: (str) -> int
    from ..events import engine_player_dimension as dimension_of

    return dimension_of(player_id)


def engine_player_position(player_id):
    # type: (str) -> tuple
    from ..messages import engine_player_position as position_of

    return position_of(player_id)


def engine_held_item(player_id):
    # type: (str) -> str
    """玩家主手物品的标识；空手为 ""。"""
    from .....tooldelta.api.server.player import GetPlayerMainhandItem

    item = GetPlayerMainhandItem(player_id)
    return item.newItemName if item is not None else ""


def engine_is_creative(player_id):
    # type: (str) -> bool
    """玩家是不是创造模式（放面板不扣物品，阶段 D）。"""
    from mod.server.extraServerApi import GetMinecraftEnum
    from .....tooldelta.api.server.player import GetPlayerGameType

    return GetPlayerGameType(player_id) == GetMinecraftEnum().GameType.Creative


def engine_take_held_item(player_id, item_id):
    # type: (str, str) -> bool
    """主手那一格扣一个 `item_id`。那一格已经不是它（或空了）就不扣，返回 False。"""
    from mod.server.extraServerApi import GetMinecraftEnum
    from .....tooldelta.api.server.player import (
        GetPlayerItem,
        GetSelectedSlot,
        SetInventorySlotItemCount,
    )

    slot = GetSelectedSlot(player_id)
    item = GetPlayerItem(player_id, GetMinecraftEnum().ItemPosType.INVENTORY, slot)
    if item is None or item.newItemName != item_id or item.count < 1:
        return False
    return bool(SetInventorySlotItemCount(player_id, slot, item.count - 1))


def _held_item(player_id):
    # type: (str) -> tuple
    """`(主手物品栏槽位, tooldelta Item 或 None)`，带 `userData`。"""
    from mod.server.extraServerApi import GetMinecraftEnum
    from .....tooldelta.api.server.player import GetPlayerItem, GetSelectedSlot

    slot = GetSelectedSlot(player_id)
    return slot, GetPlayerItem(player_id, GetMinecraftEnum().ItemPosType.INVENTORY, slot, True)


def engine_held_user_data(player_id, item_id):
    # type: (str, str) -> object
    """主手那一件 `item_id` 的 `userData`（解掉引擎的 NBT 包装）；主手已经不是它返回 None，
    没有 `userData` 返回 `{}`。"""
    from ....common.resources.item_codec import unbox_engine_value

    _slot, item = _held_item(player_id)
    if item is None or item.newItemName != item_id or item.count < 1:
        return None
    data = unbox_engine_value(item.userData) if item.userData is not None else None
    return data if isinstance(data, dict) else {}


def engine_write_held_user_data(player_id, item_id, key, value, tips):
    # type: (str, str, str, object, str) -> bool
    """改写主手那一件 `item_id` 的 `userData` 与说明。主手已经不是它返回 False。

    `userData` 整张换成只有本模组这一项（`value` 为 None 就是空的）：配置卡与无线连接工具是
    本模组自己的物品，不带别人的数据；读回来的旧值里键是 `unicode`，原样写回会被引擎整条
    丢掉（样板那边实测过，见 `server/crafting/patterns.py` 的 ASCII 编码一节），所以不回写。
    键与值都过 `engine_text`；值是字符串到字符串的字典。说明整段写进顶层 `customTips`
    （盖掉物品 JSON 的那一份），引擎复制进 `userData` 的旧副本一并不要。写完读回核一遍。
    """
    from mod.server.extraServerApi import GetMinecraftEnum
    from .....tooldelta.api.server.player import SetPlayerAllItems

    slot, item = _held_item(player_id)
    if item is None or item.newItemName != item_id or item.count < 1:
        return False
    data = {}
    if value is not None:
        data[engine_text(key)] = dict(
            (engine_text(name), engine_text(text)) for name, text in dict(value).items()
        )
    # 整张换掉：引擎读物品时复制进 `userData` 的旧说明副本（`ItemCustomTips`，见
    # `common/resources/item_codec.py` 的 `_ENGINE_CUSTOM_TIPS_KEY`）也就不在了
    item.userData = data
    item.customTips = engine_text(tips)
    SetPlayerAllItems(player_id, {(GetMinecraftEnum().ItemPosType.INVENTORY, slot): item})
    written = engine_held_user_data(player_id, item_id)
    if written is None:
        return False
    if value is None:
        return key not in written
    return written.get(key) == dict(value)


def engine_notify(player_id, text, title=None):
    # type: (str, str, object) -> None
    from .....tooldelta.api.server.tips import SetOnePopupNotice

    SetOnePopupNotice(
        engine_text(player_id), engine_text(text), engine_text(title or NOTICE_TITLE)
    )


class CableIntentPort(object):
    """意图通道的引擎侧，也是处理函数的「玩家事实」来源（`dimension_of` / `position_of` /
    `held_item_of`）。**可以脱离引擎构造**：`__init__` 不 import 任何东西。"""

    def __init__(
        self,
        binder,
        events=None,
        player_dimension=None,
        player_position=None,
        held_item=None,
        notify=None,
        creative=None,
        take_held=None,
        held_user_data=None,
        write_held_user_data=None,
    ):
        # type: (object, object, object, object, object, object, object, object, object, object) -> None
        #: 注册账本：`ServerEventBinder`（`stop()` 时一并注销）
        self.binder = binder
        self.events = events if events is not None else engine_events
        self.player_dimension = (
            player_dimension if player_dimension is not None else engine_player_dimension
        )
        self.player_position = (
            player_position if player_position is not None else engine_player_position
        )
        self.held_item = held_item if held_item is not None else engine_held_item
        self.notify = notify if notify is not None else engine_notify
        self.creative = creative if creative is not None else engine_is_creative
        self.take_held = take_held if take_held is not None else engine_take_held_item
        self.read_held_data = (
            held_user_data if held_user_data is not None else engine_held_user_data
        )
        self.write_held_data = (
            write_held_user_data if write_held_user_data is not None
            else engine_write_held_user_data
        )
        self._event_classes = None
        self.stats = {"received": 0, "handler_errors": 0, "notified": 0, "notify_errors": 0}

    def classes(self):
        # type: () -> tuple
        if self._event_classes is None:
            self._event_classes = shared_event_classes(self.events())
        return self._event_classes

    # -- 玩家事实 --------------------------------------------------------

    def dimension_of(self, player_id):
        # type: (str) -> object
        return self._ask(self.player_dimension, player_id)

    def position_of(self, player_id):
        # type: (str) -> object
        position = self._ask(self.player_position, player_id)
        return tuple(position) if position is not None else None

    def held_item_of(self, player_id):
        # type: (str) -> object
        return self._ask(self.held_item, player_id)

    def is_creative(self, player_id):
        # type: (str) -> object
        """创造模式为 True；查不到为 None（按生存模式处理，由调用方决定）。"""
        return self._ask(self.creative, player_id)

    def take_held_item(self, player_id, item_id):
        # type: (str, str) -> bool
        """从主手扣一个 `item_id`；扣不成（换了物品、玩家刚离线、引擎抛了）返回 False。"""
        if not player_id:
            return False
        try:
            return bool(self.take_held(player_id, item_id))
        except Exception:
            return False

    def held_user_data(self, player_id, item_id):
        # type: (str, str) -> object
        """主手那一件 `item_id` 的 `userData`；主手已经不是它（或读不到）返回 None。"""
        if not player_id:
            return None
        try:
            return self.read_held_data(player_id, item_id)
        except Exception:
            return None

    def write_held_user_data(self, player_id, item_id, key, value, tips):
        # type: (str, str, str, object, str) -> bool
        """改写主手那一件的 `userData[key]` 与说明；写不进去返回 False。"""
        if not player_id:
            return False
        try:
            return bool(self.write_held_data(player_id, item_id, key, value, tips))
        except Exception:
            return False

    @staticmethod
    def _ask(asker, player_id):
        # type: (object, str) -> object
        """查一次；玩家刚离线、实体刚卸载都算「这次拿不到」，由处理函数按拒绝处理。"""
        if not player_id:
            return None
        try:
            return asker(player_id)
        except Exception:
            return None

    # -- 注册 ------------------------------------------------------------

    def listen(self, handler):
        # type: (object) -> int
        """挂上 C2S 接收侧：`handler(player_id, payload) -> IntentResult`。返回注册的监听器个数。

        回调里抛出的异常在这里接住并计数：抛回引擎会打断同一批派发。
        """
        (request,) = self.classes()

        def dispatch(event):
            self.stats["received"] += 1
            player_id = getattr(event, "player_id", None) or ""
            try:
                result = handler(player_id, getattr(event, "payload", None))
            except Exception:
                self.stats["handler_errors"] += 1
                return None
            message = getattr(result, "message", "")
            if message and player_id:
                title = getattr(result, "title", "")
                try:
                    if title:
                        self.notify(player_id, message, title)
                    else:
                        self.notify(player_id, message)
                    self.stats["notified"] += 1
                except Exception:
                    self.stats["notify_errors"] += 1
            return result

        self.binder.register(request, dispatch)
        return 1

    def diagnostics(self):
        # type: () -> dict
        return dict(self.stats)
