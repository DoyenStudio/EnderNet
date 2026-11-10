# coding=utf-8
"""光缆与面板精确交互的客户端一侧（阶段 C / D，`.claude/13` §6；2026-11-10 改射线、改走
伪原版交互封装）：点中方块时把意图发给服务端。

规则与载荷在 `common/channels/cable_intent.py`；监听、「一次按下」、接管后整次按下都拦、组射线、
挥手都在 `client/interaction/`（`UseTakeover`），这里只给它两样东西：

- **规则**（`CableIntentRule`）：按手持物品、潜行与被点的方块选得出动作（`action_for`）的那一下
  才管——扳手点光缆 / 面板底座；面板物品点任何方块（不潜行时右键开界面的本模组设备除外，
  潜行时连它们也挂）；光缆物品点面板底座。选不出就不碰，原版照常；右键面板开界面也在其中，
  由服务端的原版事件判（`server/interaction/opening.py`，与整方块终端同一个入口）。
  `ClientBlockUseEvent` 只管按面的方块：别的方块要靠事件带的面才放得了面板，那一下交给同一刻
  随后的 `ClientItemUseOnEvent`；
- **发出去**（`CableIntentClient._send`）：编成意图载荷（被点的方块、手持物品、动作、被点的面、
  射线），C2S 发给服务端。

**不持有任何权威状态**：点中的是哪个部件由服务端按该格的方块状态求交（客户端拿不到方块
状态，见 `common/channels/cable_intent.py`）。
"""

from ...common.channels.cable_intent import (
    FACED_BLOCK_IDS,
    action_for,
    build_event_classes,
    face_of_engine,
    intent_payload,
)
from ..interaction import EngineView, UseTakeover

_SERVICE = None

#: 进程内共享的事件类：`引擎 events 模块 -> (CableIntentRequest,)`。
#: 理由同 `client/channels/device_identity.py` 的 `_EVENT_CLASSES`。
_EVENT_CLASSES = {}


def shared_event_classes(events_module):
    # type: (object) -> tuple
    cached = _EVENT_CLASSES.get(events_module)
    if cached is None:
        cached = build_event_classes(events_module)
        _EVENT_CLASSES[events_module] = cached
    return cached


class CableIntentRule(object):
    """`UseTakeover` 的规则：什么时候接管、接管成哪个动作。`faced_ids` 是按面的方块（光缆与
    面板底座）：方块使用事件只管它们。"""

    def __init__(self, faced_ids):
        self.faced_ids = frozenset(faced_ids)

    def wants(self, use):
        # type: (object) -> bool
        """不读引擎的预筛：潜行与否至少有一种选得出动作。按住右键时每刻一条的物品事件大多到这里
        就结束，不必每次都去问引擎潜没潜行。"""
        if not use.via_item and use.block not in self.faced_ids:
            return False
        return (action_for(use.item_id, False, use.block) is not None
                or action_for(use.item_id, True, use.block) is not None)

    def decide(self, use, sneaking):
        # type: (object, bool) -> object
        return action_for(use.item_id, sneaking, use.block)


class CableIntentClient(object):
    """可以脱离引擎构造：总线、事件类、引擎读数都由调用方注入（见 `UseTakeover`）。"""

    def __init__(self, bus, events, block_use_event, item_use_on_event, view, faced_ids,
                 tick_event=None, clock=None):
        # type: (object, object, object, object, object, object, object, object) -> None
        (self.request,) = shared_event_classes(events)
        self.takeover = UseTakeover(
            bus, block_use_event, item_use_on_event, view, CableIntentRule(faced_ids),
            self._send, tick_event=tick_event, clock=clock,
        )

    def _send(self, use, action, ray):
        payload = intent_payload(
            use.pos, use.block, use.item_id, action, ray[0], ray[1], face_of_engine(use.face)
        )
        self.request(payload).send()

    # -- 交给接管层 ----------------------------------------------------------

    def start(self):
        self.takeover.start()

    def stop(self):
        self.takeover.stop()

    def on_block_use(self, event):
        return self.takeover.on_block_use(event)

    def on_item_use_on(self, event):
        return self.takeover.on_item_use_on(event)

    @property
    def stats(self):
        # type: () -> dict
        return self.takeover.stats

    @property
    def clock(self):
        return self.takeover.clock

    @property
    def presses(self):
        return self.takeover.presses

    def diagnostics(self):
        # type: () -> dict
        return self.takeover.diagnostics()


def get_cable_intent():
    # type: () -> CableIntentClient
    return _SERVICE


def start_cable_intent():
    # type: () -> CableIntentClient
    """由 `client/bootstrap.py::start_client` 调用。"""
    global _SERVICE
    from ....tooldelta import events
    from ....tooldelta.events.client import (
        ClientBlockUseEvent,
        ClientItemUseOnEvent,
        OnScriptTickClient,
    )
    from ....tooldelta.events.event_bus import GetMCClientEventBus

    stop_cable_intent()
    _SERVICE = CableIntentClient(
        GetMCClientEventBus(),
        events,
        ClientBlockUseEvent,
        ClientItemUseOnEvent,
        EngineView(),
        FACED_BLOCK_IDS,
        tick_event=OnScriptTickClient,
    )
    _SERVICE.start()
    return _SERVICE


def stop_cable_intent():
    # type: () -> None
    global _SERVICE
    if _SERVICE is not None:
        _SERVICE.stop()
        _SERVICE = None
