# coding=utf-8
"""Request the selector catalogue once after UI initialization."""

from weakref import WeakSet

from ..common.item_catalog import build_event_classes

_SERVICE = None


class ItemCatalogClient(object):
    def __init__(self, bus, events, ready_event):
        self.bus = bus
        self.request, self.response = build_event_classes(events)
        self.ready_event = ready_event
        self.item_ids = []
        self.fluid_ids = []
        self.bucket_fluid_ids = {}
        self.requested = False
        self.windows = WeakSet()

    def start(self):
        self.bus.ListenEvent(self.response, 0, static=True)(self.receive)
        self.bus.ListenEvent(self.ready_event, 0, static=True)(self.on_ready)

    def on_ready(self, event):
        if not self.requested:
            self.requested = True
            self.request().send()

    def receive(self, event):
        self.item_ids = list(event.item_ids)
        self.fluid_ids = list(event.fluid_ids)
        self.bucket_fluid_ids = dict(event.bucket_fluid_ids)
        for window in list(self.windows):
            window.refresh()

    def stop(self):
        for window in list(self.windows):
            window.close()
        self.bus._dyn_unlisten(self.response, self.receive, 0)
        self.bus._dyn_unlisten(self.ready_event, self.on_ready, 0)
        self.item_ids = []
        self.fluid_ids = []
        self.bucket_fluid_ids = {}


def get_catalog():
    return _SERVICE


def start_catalog():
    global _SERVICE
    from ...tooldelta import events
    from ...tooldelta.events.client import UiInitFinishedEvent
    from ...tooldelta.events.event_bus import GetMCClientEventBus

    stop_catalog()
    _SERVICE = ItemCatalogClient(GetMCClientEventBus(), events, UiInitFinishedEvent)
    _SERVICE.start()


def stop_catalog():
    global _SERVICE
    if _SERVICE is not None:
        _SERVICE.stop()
        _SERVICE = None
