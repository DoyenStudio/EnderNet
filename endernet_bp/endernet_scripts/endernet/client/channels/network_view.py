# coding=utf-8
"""Viewer interaction and session lifecycle. No topology decisions on the client."""
import time
import uuid

from ...common.channels.cable_intent import face_of_engine, intent_payload
from ...common.channels.network_view import ACTION_VIEW, build_event_classes, parse_update
from ...common.definitions import DefinitionSet, NETWORK_VISUALIZER_ID
from ..interaction import EngineView, UseTakeover
from ..network_overlay import ModelPort, OverlayRenderer

_SERVICE = None
_EVENT_CLASSES = {}


class ViewRule(object):
    def __init__(self):
        definitions = DefinitionSet()
        self.blocks = frozenset(d.id for d in definitions.all_definitions())

    def wants(self, use):
        return use.item_id == NETWORK_VISUALIZER_ID and use.block in self.blocks

    def decide(self, use, sneaking):
        return ACTION_VIEW


class ViewerState(object):
    """Testable state machine; late responses cannot recreate a closed overlay."""
    def __init__(self, send, renderer, now=None):
        self.send = send
        self.renderer = renderer
        self.now = now if now is not None else time.time
        self.session = None
        self.dimension = None
        self.revision = 0
        self.last_keepalive = 0
        self.message = ""

    def request(self, action, intent=None):
        self.send({"session": self.session, "action": action, "intent": intent})

    def select(self, intent, dimension):
        self.close()
        self.session = uuid.uuid4().hex
        self.dimension = dimension
        self.revision = 0
        self.last_keepalive = self.now()
        self.request("select", intent)

    def receive(self, payload):
        try:
            data = parse_update(payload)
        except (ValueError, TypeError, KeyError):
            return False
        if data["session"] != self.session or data["revision"] <= self.revision:
            return False
        if not data["clear"] and data["dimension"] != self.dimension:
            return False
        self.revision = data["revision"]
        self.message = data["message"]
        if data["clear"]:
            self.session = None
            self.renderer.clear()
        else:
            self.renderer.apply(data)
        return True

    def tick(self, held, dimension, alive=True):
        if self.session is not None:
            if held != NETWORK_VISUALIZER_ID or dimension != self.dimension or not alive:
                self.close()
            elif self.now() - self.last_keepalive >= 1.0:
                self.request("keepalive")
                self.last_keepalive = self.now()
        self.renderer.advance()

    def close(self):
        if self.session is not None:
            self.request("close")
        self.session = None
        self.dimension = None
        self.message = ""
        self.renderer.clear()


class ViewerClient(object):
    def __init__(self, bus, events, block_event, item_event, tick_event):
        if events not in _EVENT_CLASSES:
            _EVENT_CLASSES[events] = build_event_classes(events)
        self.request, self.update = _EVENT_CLASSES[events]
        self.bus = bus
        self.tick_event = tick_event
        self.view = EngineView()
        self.state = ViewerState(lambda payload: self.request(payload).send(), OverlayRenderer(ModelPort()))
        self.takeover = UseTakeover(bus, block_event, item_event, self.view, ViewRule(),
                                    self.select, tick_event=tick_event)

    @staticmethod
    def dimension():
        from ....tooldelta.api.client.player import GetPlayerDimensionId
        return GetPlayerDimensionId()

    @staticmethod
    def alive():
        from mod.client.extraClientApi import GetEngineCompFactory, GetLocalPlayerId, GetMinecraftEnum
        health = GetEngineCompFactory().CreateAttr(GetLocalPlayerId()).GetAttrValue(GetMinecraftEnum().AttrType.HEALTH)
        return health is not None and health > 0

    def select(self, use, action, ray):
        payload = intent_payload(use.pos, use.block, use.item_id, action, ray[0], ray[1],
                                 face=face_of_engine(use.face))
        self.state.select(payload, self.dimension())

    def receive(self, event):
        previous = self.state.message
        if self.state.receive(event.payload) and self.state.message and self.state.message != previous:
            from ....tooldelta.api.client.tips import SetPopupNotice
            SetPopupNotice(self.state.message, "§b网络可视化查看器§f")

    def tick(self, event=None):
        if self.state.session is not None:
            self.state.tick(self.view.held_item_id(), self.dimension(), self.alive())

    def start(self):
        self.bus.ListenEvent(self.update, 0)(self.receive)
        self.bus.ListenEvent(self.tick_event, 0)(self.tick)
        self.takeover.start()

    def stop(self):
        self.takeover.stop()
        self.bus.RemoveEventListener(self.update, self.receive, 0)
        self.bus.RemoveEventListener(self.tick_event, self.tick, 0)
        self.state.close()

    def diagnostics(self):
        return {"session": self.state.session, "revision": self.state.revision,
                "render": self.state.renderer.diagnostics(), "input": self.takeover.diagnostics()}


def start_network_view():
    global _SERVICE
    from ....tooldelta import events
    from ....tooldelta.events.client import ClientBlockUseEvent, ClientItemUseOnEvent, OnScriptTickClient
    from ....tooldelta.events.event_bus import GetMCClientEventBus
    stop_network_view()
    _SERVICE = ViewerClient(GetMCClientEventBus(), events, ClientBlockUseEvent,
                            ClientItemUseOnEvent, OnScriptTickClient)
    _SERVICE.start()


def stop_network_view():
    global _SERVICE
    if _SERVICE is not None:
        _SERVICE.stop()
        _SERVICE = None


def get_network_view():
    return _SERVICE
