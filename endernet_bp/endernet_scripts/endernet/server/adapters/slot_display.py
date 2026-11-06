# coding=utf-8
"""槽位模块显示的引擎出口（2026-11-05）。

规则与载荷在 `common/slot_display.py`，算读数的是 `server/devices/slot_display.py`
的 `SlotDisplayTracker`；本模块只负责收发：

- `publish_slot_display(payload)`：读数变了，**全服广播**。由启动层作为
  `SlotDisplayTracker.publish` 注入（`start_server` 里写明）。
- `SlotDisplayRequests`：接客户端的单点请求，回给请求者本人。由
  `modMain.OnServerInited` 启动，与物品目录同一形状。

引擎 import 一律在函数体内（与 `server/adapters/device_identity.py` 同一手法），
本模块必须能离线导入。

**为什么广播而不是按距离发**：读数只在有人插拔时变，一条不到一百字节；
客户端只对自己已加载的位置生效，别处的读数直接丢弃（不缓存）。按距离筛
要每次查一遍玩家位置，换来的只是省掉几条本来就很小的包。
"""

from ...common.slot_display import build_event_classes, parse_request

#: 进程内共享的事件类：`引擎 events 模块 -> (请求, 下发)`。
#: 广播与请求监听**必须用同一份类**：每建一份新类都是引擎侧的一次新注册
#: （见 `common/device_identity.py` 的 `build_event_classes`）。
_EVENT_CLASSES = {}

_SERVICE = None


def engine_events():
    # type: () -> object
    from ....tooldelta import events as events_module

    return events_module


def shared_event_classes(events_module):
    # type: (object) -> tuple
    cached = _EVENT_CLASSES.get(events_module)
    if cached is None:
        cached = build_event_classes(events_module)
        _EVENT_CLASSES[events_module] = cached
    return cached


def publish_slot_display(payload, events_module=None):
    # type: (dict, object) -> None
    """把一台设备的读数广播给所有客户端。`events_module` 是离线测试的注入点。"""
    classes = shared_event_classes(
        events_module if events_module is not None else engine_events()
    )
    classes[1](payload).sendAll()


class SlotDisplayRequests(object):
    """客户端「这台设备现在插着什么」的请求。"""

    def __init__(self, bus, events, tracker_of):
        # type: (object, object, object) -> None
        self.bus = bus
        self.request, self.update = shared_event_classes(events)
        #: `() -> SlotDisplayTracker | None`。**每次现取**：服务端热重载会换掉
        #: 启动层实例，监听器却留在总线上。
        self.tracker_of = tracker_of
        self.stats = {"answered": 0, "unknown": 0, "rejected": 0}

    def start(self):
        # 不带 `static=True`，理由同 `server/adapters/item_catalog.py`。
        self.bus.ListenEvent(self.request, 0)(self.receive)

    def stop(self):
        self.bus._dyn_unlisten(self.request, self.receive, 0)

    def receive(self, event):
        if not event.player_id:
            return
        try:
            node_key = parse_request(event.payload)
        except (KeyError, TypeError, ValueError):
            self.stats["rejected"] += 1
            return
        tracker = self.tracker_of()
        payload = tracker.answer(node_key) if tracker is not None else None
        if payload is None:
            self.stats["unknown"] += 1
            return
        self.update(payload).send(event.player_id)
        self.stats["answered"] += 1


def _current_tracker():
    from ..bootstrap import get_server

    server = get_server()
    return getattr(server, "slot_displays", None) if server is not None else None


def start_slot_display():
    global _SERVICE
    from ....tooldelta import events
    from ....tooldelta.events.event_bus import GetMCServerEventBus

    stop_slot_display()
    _SERVICE = SlotDisplayRequests(GetMCServerEventBus(), events, _current_tracker)
    _SERVICE.start()


def stop_slot_display():
    global _SERVICE
    if _SERVICE is not None:
        _SERVICE.stop()
        _SERVICE = None
