# coding=utf-8
"""光缆精确交互的引擎出口（阶段 C，`.claude/13` §6）。本模块是**引擎边界**。

规则与载荷在 `common/channels/cable_intent.py`，校验与分派在
`server/devices/cable_intents.py`；本模块只做翻译：

- **收**：C2S `endernet:cable_intent`，经 `ServerEventBinder.register` 挂上——与同步通道
  同一本账，`stop()` 时随其它监听器一起注销。发件人只认引擎附的 `__id__`。
- **查**：处理函数要的玩家事实——所在维度、脚下坐标、主手物品、是不是创造模式；
  放面板时从主手扣一个（阶段 D）。
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


def engine_notify(player_id, text):
    # type: (str, str) -> None
    from .....tooldelta.api.server.tips import SetOnePopupNotice

    SetOnePopupNotice(engine_text(player_id), engine_text(text), engine_text(NOTICE_TITLE))


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
    ):
        # type: (object, object, object, object, object, object, object, object) -> None
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
                try:
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
