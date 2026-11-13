# coding=utf-8
"""Engine bridge for the private viewer channel; lifecycle owned by ServerEventBinder."""
from ....common.channels.network_view import build_event_classes

_EVENT_CLASSES = {}


def shared_event_classes(events):
    if events not in _EVENT_CLASSES:
        _EVENT_CLASSES[events] = build_event_classes(events)
    return _EVENT_CLASSES[events]


def alive(player):
    from mod.server.extraServerApi import GetEngineCompFactory, GetMinecraftEnum
    value = GetEngineCompFactory().CreateAttr(player).GetAttrValue(GetMinecraftEnum().AttrType.HEALTH)
    return value is not None and value > 0


def bind_viewer(binder, viewer, events):
    if events is None:
        from .....tooldelta import events
    request, update = shared_event_classes(events)
    viewer.publish = lambda player, payload: update(payload).send(player)
    viewer.alive = alive
    binder.register(request, lambda event: viewer.receive(event.player_id, event.payload))
