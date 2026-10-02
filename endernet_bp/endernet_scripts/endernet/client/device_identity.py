# coding=utf-8
"""设备界面的身份认领（11-07）。

容器类设备的屏幕由**引擎**按方块组件自动打开，脚本侧只是挂在它上面的
代理——代理屏**拿不到任何参数**（`tooldelta/ui/general_screen.py` 的
`_register_as_proxy` 把 `_initial_params` 置 `None`），因此它不知道自己
对应哪台方块。服务端在玩家右键那一刻查到注册表后，把权威 `DeviceRef`
沿一条**无会话**的下行通道推过来（`common/device_identity.py`）。

本模块是那条通道的客户端半边：

- `DeviceIdentityLatch`：纯模型，`ui_key -> (DeviceRef, dimension)`。
  **取出即消费**——身份是一次性的，留着它只会让下一次开屏认领到上一次的
  设备（那台设备可能已经被拆了）。
- `DeviceIdentityClient`：引擎侧的监听。形状照 `client/item_catalog.py`
  （同一个处境：要在任何会话之前拿到东西）。
- `start_device_identity()` / `stop_device_identity()` / `latch()`：进程级
  单例，由 `client/bootstrap.py` 起停。

## 为什么屏要「等」

服务端那条推送与引擎开屏是**两个**动作，先后没有保证。屏在 `OnCreate`
先试一次，没有就什么都不做（引擎自己会把容器格子画出来，屏绝不能因此
变白），此后每帧再试一次，拿到才开会话。两种到达顺序都覆盖。
"""

from ..common.device_identity import build_event_classes, parse_identity

#: 进程内的监听服务单例
_SERVICE = None

#: 进程内共享的事件类：`引擎 events 模块 -> (DeviceScreenIdentity,)`。
#:
#: **必须进程内只建一份**：`EventBus` 以事件类对象为键判重，每新建一个类
#: 就是引擎侧的一次新注册，而同名处理器会被后者 `setattr` 覆盖，结果是
#: **先注册的那个收不到任何消息**（实测与机理见 `network_client.py`
#: 的 `_EVENT_CLASSES`）。屏会开关多次，但这个监听只起一次。
_EVENT_CLASSES = {}


def shared_event_classes(events_module):
    # type: (object) -> tuple
    """进程内共享的事件类；同一个 `events_module` 只建一份。"""
    cached = _EVENT_CLASSES.get(events_module)
    if cached is None:
        cached = build_event_classes(events_module)
        _EVENT_CLASSES[events_module] = cached
    return cached


class DeviceIdentityLatch(object):
    """待认领的设备身份。**不持有任何权威状态**——它只是把服务端说过的话
    转交给屏，转交完就忘。

    同一个 `ui_key` 后到的覆盖先到的：服务端每次右键都重推，而屏只认
    「最近一次」。取走（`take`）之后条目就没了，因此一次推送只会被认领
    一次——留着复用会让下一次开屏认到一台已经拆掉的设备。
    """

    __slots__ = ("_by_key", "stats")

    def __init__(self):
        self._by_key = {}
        #: **不数「问了个空」**：屏在 `OnTicking` 里每帧来问一次，那个数会
        #: 涨成「等了多久（帧）」而不是「有几条身份没人接」，在实机上误导人。
        #: 要判断「有没有推过来」看 `accepted`，判断「接了没有」看 `taken`。
        self.stats = {"accepted": 0, "rejected": 0, "taken": 0}

    def offer(self, payload):
        # type: (dict) -> bool
        """收一条下行身份。形状不对就**只计数、不采纳**，返回 False。

        这里吞掉异常是有意的：事件回调抛出去会一路回到引擎，而一条读不懂
        的包不该让后续所有身份都收不到（`ClientEventPort.dispatch` 同一条
        纪律）。代价是坏包在实机上只表现为「屏一直没认到身份」，
        因此 `diagnostics()` 里那个 `rejected` 数是排障的第一个落点。
        """
        try:
            key, device_ref, dimension = parse_identity(payload)
        except (ValueError, TypeError, KeyError):
            self.stats["rejected"] += 1
            return False
        self._by_key[key] = (device_ref, dimension)
        self.stats["accepted"] += 1
        return True

    def take(self, ui_key):
        # type: (str) -> tuple
        """取走并消费一条身份，返回 `(DeviceRef, dimension)`；没有则 `None`。"""
        entry = self._by_key.pop(ui_key, None)
        if entry is None:
            return None
        self.stats["taken"] += 1
        return entry

    def pending(self):
        # type: () -> tuple
        """当前还挂着、无人认领的界面键（诊断用）。"""
        return tuple(sorted(self._by_key))

    def clear(self):
        # type: () -> None
        self._by_key = {}

    def diagnostics(self):
        # type: () -> dict
        result = dict(self.stats)
        result["pending"] = self.pending()
        return result


class DeviceIdentityClient(object):
    """把下行身份写进 latch 的监听服务。可以脱离引擎构造（注入替身总线）。"""

    def __init__(self, bus, events, latch=None):
        # type: (object, object, object) -> None
        self.bus = bus
        (self.event_cls,) = shared_event_classes(events)
        self.latch = latch if latch is not None else DeviceIdentityLatch()
        self._handle = None

    def start(self):
        # type: () -> int
        """注册监听，返回本次注册的个数（重复注册返回 0）。

        不带 `static=True`：`self.receive` 是绑定方法，py2 在它上面挂不住
        `_td_static`，挂就是 `AttributeError`（2026-09-29 真机撞出来的，
        见 `server/adapters/item_catalog.py` 的同款注释）。
        """
        if self._handle is not None:
            return 0
        self.bus.ListenEvent(self.event_cls, 0)(self.receive)
        self._handle = self.receive
        return 1

    def receive(self, event):
        # type: (object) -> bool
        """总线回调。**永不抛出**（理由见 `DeviceIdentityLatch.offer`）。"""
        payload = getattr(event, "payload", None)
        return self.latch.offer(payload if isinstance(payload, dict) else {})

    def listening(self):
        # type: () -> bool
        return self._handle is not None

    def stop(self):
        # type: () -> int
        if self._handle is None:
            return 0
        self.bus.RemoveEventListener(self.event_cls, self._handle, 0)
        self._handle = None
        self.latch.clear()
        return 1

    def diagnostics(self):
        # type: () -> dict
        return {"listening": self.listening(), "latch": self.latch.diagnostics()}


#: 监听没起来时的兜底 latch。**是一个常驻实例，不是每次新建**：屏在
#: `OnTicking` 里每帧来问一次，每帧 new 一个字典只是白白的垃圾。
#: 返回空壳而不是 `None`：调用方（屏的 `OnCreate`）不必到处判空，而空的
#: latch 恰好给出正确的行为——认不到身份，屏就一直不动。
_FALLBACK_LATCH = DeviceIdentityLatch()


def latch():
    # type: () -> DeviceIdentityLatch
    """进程级的身份 latch；监听没起来时返回那只常驻的空 latch。"""
    if _SERVICE is None:
        return _FALLBACK_LATCH
    return _SERVICE.latch


def get_device_identity():
    # type: () -> DeviceIdentityClient
    return _SERVICE


def start_device_identity():
    # type: () -> DeviceIdentityClient
    """由 `client/bootstrap.py::start_client` 调用。"""
    global _SERVICE
    from ...tooldelta import events
    from ...tooldelta.events.event_bus import GetMCClientEventBus

    stop_device_identity()
    _SERVICE = DeviceIdentityClient(GetMCClientEventBus(), events)
    _SERVICE.start()
    return _SERVICE


def stop_device_identity():
    # type: () -> None
    global _SERVICE
    if _SERVICE is not None:
        _SERVICE.stop()
        _SERVICE = None
