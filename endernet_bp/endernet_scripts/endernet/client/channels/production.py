# coding=utf-8
"""Read-only production snapshots and authenticated machine UI requests."""
from ...common.channels.production import build_event_classes

_SERVICE = None
_EVENT_CLASSES = {}


class ProductionCache(object):
    """A snapshot opens a proxy only once; updates cannot revive a closed screen."""

    def __init__(self):
        self.pending = {}
        self.current = {}

    def offer(self, payload):
        if not isinstance(payload, dict):
            return False
        device_id = payload.get("device_id")
        if device_id not in ("endernet:resonance_charger", "endernet:resonance_inscriber",
                             "endernet:resonance_generator"):
            return False
        session = payload.get("session")
        if not session or not isinstance(payload.get("pos"), (list, tuple)) or len(payload["pos"]) != 3:
            return False
        if payload.get("opened"):
            previous = self.pending.get(device_id)
            if previous is not None and previous != session:
                self.current.pop(previous, None)
            self.pending[device_id] = session
        elif session not in self.current:
            return False
        self.current[session] = dict(payload)
        return True

    def take(self, device_id):
        return self.pending.pop(device_id, None)

    def close(self, session):
        self.current.pop(session, None)
        for key in tuple(self.pending):
            if self.pending[key] == session:
                del self.pending[key]


class ProductionClient(object):
    def __init__(self, bus, events):
        if events not in _EVENT_CLASSES:
            _EVENT_CLASSES[events] = build_event_classes(events)
        self.request, self.update = _EVENT_CLASSES[events]
        self.bus = bus
        self.cache = ProductionCache()

    def start(self):
        self.bus.ListenEvent(self.update, 0)(self.receive)

    def receive(self, event):
        return self.cache.offer(event.payload)

    def send(self, session, action, mode=None):
        snapshot = self.cache.current.get(session)
        if snapshot is None:
            return False
        payload = {"action": action, "session": session, "device_id": snapshot["device_id"],
                   "dimension": snapshot["dimension"], "pos": snapshot["pos"]}
        if mode is not None:
            payload["mode"] = mode
        self.request(payload).send()
        return True

    def close(self, session):
        self.send(session, "close")
        self.cache.close(session)

    def stop(self):
        for session in tuple(self.cache.current):
            self.close(session)
        self.bus.RemoveEventListener(self.update, self.receive, 0)


def get_production_client():
    return _SERVICE


def start_production_client():
    global _SERVICE
    from ....tooldelta import events
    from ....tooldelta.events.event_bus import GetMCClientEventBus

    stop_production_client()
    _SERVICE = ProductionClient(GetMCClientEventBus(), events)
    _SERVICE.start()
    return _SERVICE


def stop_production_client():
    global _SERVICE
    if _SERVICE is not None:
        _SERVICE.stop()
        _SERVICE = None
