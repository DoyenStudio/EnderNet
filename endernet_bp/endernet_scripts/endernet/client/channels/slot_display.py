# coding=utf-8
"""槽位模块显示的客户端一侧（2026-11-05）：把服务端的读数写进方块实体外观的 molang 变量。

规则与载荷在 `common/channels/slot_display.py`。流程：

1. 方块实体外观加载好（`ModBlockEntityLoadedClientEvent`，只认驱动器与内存槽）
   → 记下这个位置，向服务端要一次读数。**变量必须等这个事件之后才写得进去**
   （`SetBlockEntityMolangValue` 的前提，照 SkyblueTech 挖掘机的做法）。
2. 收到读数（请求的回答或服务端的广播）→ 位置已加载就写：朝向角一个、
   每槽一个；没加载就丢掉——下次加载时会重新要，不缓存。
3. 方块实体外观卸载（`ModBlockEntityRemoveClientEvent`）→ 忘掉这个位置。

能挂面板的两种光缆（阶段 D）走同一条路：种类是 `PANEL`，写的是 `variable.p0`～`p5`
（`variable_of`），不写朝向角（`has_facing`）。

**不持有权威状态**：只是把服务端说的数转交给渲染。坏包只计数、永不抛出
（同 `DeviceIdentityLatch.offer`：总线回调里抛出去会打断同一批派发）。
"""

from ...common.identity import NodeKey
from ...common.channels.slot_display import (
    FACING_VARIABLE,
    build_event_classes,
    facing_yaw,
    has_facing,
    kind_of,
    parse_update,
    request_payload,
    slot_count,
    variable_of,
)

_SERVICE = None

#: 进程内共享的事件类，理由同 `client/channels/device_identity.py` 的 `_EVENT_CLASSES`。
_EVENT_CLASSES = {}


def shared_event_classes(events_module):
    # type: (object) -> tuple
    cached = _EVENT_CLASSES.get(events_module)
    if cached is None:
        cached = build_event_classes(events_module)
        _EVENT_CLASSES[events_module] = cached
    return cached


class SlotDisplayClient(object):
    """可以脱离引擎构造：总线、引擎事件类与变量写入口都由调用方注入。"""

    def __init__(self, bus, events, loaded_event, removed_event, set_variable):
        # type: (object, object, object, object, object) -> None
        self.bus = bus
        self.request, self.update = shared_event_classes(events)
        self.loaded_event = loaded_event
        self.removed_event = removed_event
        #: `(pos_tuple, 变量名, float) -> bool`，生产实现是 `SetBlockEntityMolangValue`。
        self.set_variable = set_variable
        #: `(dimension, x, y, z) -> 模块种类`：外观已加载、可以写变量的位置。
        self._loaded = {}
        self._listening = False
        self.stats = {
            "requested": 0,
            "applied": 0,
            "ignored": 0,
            "rejected": 0,
            "set_failures": 0,
        }

    def _listeners(self):
        return (
            (self.update, self.receive),
            (self.loaded_event, self.on_loaded),
            (self.removed_event, self.on_removed),
        )

    def start(self):
        if self._listening:
            return
        # 不带 `static=True`，理由同 `client/channels/device_identity.py`。
        for event_cls, handler in self._listeners():
            self.bus.ListenEvent(event_cls, 0)(handler)
        self._listening = True

    def stop(self):
        if not self._listening:
            return
        for event_cls, handler in self._listeners():
            self.bus.RemoveEventListener(event_cls, handler, 0)
        self._listening = False
        self._loaded = {}

    # -- 引擎事件 ------------------------------------------------------

    def on_loaded(self, event):
        kind = kind_of(getattr(event, "blockName", None))
        if kind is None:
            return
        try:
            node_key = NodeKey(event.dimensionId, event.posX, event.posY, event.posZ)
        except (TypeError, ValueError):
            self.stats["rejected"] += 1
            return
        self._loaded[node_key.as_key()] = kind
        self.request(request_payload(node_key)).send()
        self.stats["requested"] += 1

    def on_removed(self, event):
        if kind_of(getattr(event, "blockName", None)) is None:
            return
        key = (event.dimensionId, event.posX, event.posY, event.posZ)
        self._loaded.pop(key, None)

    def receive(self, event):
        """服务端的读数。**永不抛出**。"""
        try:
            node_key, facing, slots = parse_update(getattr(event, "payload", None))
        except (KeyError, TypeError, ValueError):
            self.stats["rejected"] += 1
            return False
        kind = self._loaded.get(node_key.as_key())
        if kind is None:
            self.stats["ignored"] += 1
            return False
        self.apply(node_key, kind, facing, slots)
        return True

    def apply(self, node_key, kind, facing, slots):
        # type: (NodeKey, str, str, tuple) -> None
        pos = node_key.as_tuple()
        writes = [(FACING_VARIABLE, facing_yaw(facing))] if has_facing(kind) else []
        for slot in range(min(len(slots), slot_count(kind))):
            writes.append((variable_of(kind, slot), slots[slot]))
        for name, value in writes:
            try:
                ok = self.set_variable(pos, name, float(value))
            except Exception:
                ok = False
            if not ok:
                # 返回 False 多半是外观还没就绪；下次加载会重新要一次。
                self.stats["set_failures"] += 1
        self.stats["applied"] += 1

    def diagnostics(self):
        # type: () -> dict
        return dict(self.stats, listening=self._listening, loaded=len(self._loaded))


def get_slot_display():
    # type: () -> SlotDisplayClient
    return _SERVICE


def start_slot_display():
    # type: () -> SlotDisplayClient
    """由 `client/bootstrap.py::start_client` 调用。"""
    global _SERVICE
    from ....tooldelta import events
    from ....tooldelta.api.client.block import SetBlockEntityMolangValue
    from ....tooldelta.events.client import (
        ModBlockEntityLoadedClientEvent,
        ModBlockEntityRemoveClientEvent,
    )
    from ....tooldelta.events.event_bus import GetMCClientEventBus

    stop_slot_display()
    _SERVICE = SlotDisplayClient(
        GetMCClientEventBus(),
        events,
        ModBlockEntityLoadedClientEvent,
        ModBlockEntityRemoveClientEvent,
        SetBlockEntityMolangValue,
    )
    _SERVICE.start()
    return _SERVICE


def stop_slot_display():
    # type: () -> None
    global _SERVICE
    if _SERVICE is not None:
        _SERVICE.stop()
        _SERVICE = None
