# coding=utf-8
"""Independent production-container UI messages; no network session required."""

REQUEST_CHANNEL = "endernet:production_request"
UPDATE_CHANNEL = "endernet:production_update"


def build_event_classes(events):
    """Return (ProductionRequest, ProductionUpdate); callers cache per event API."""
    class ProductionRequest(events.CustomC2SEvent):
        name = REQUEST_CHANNEL

        def __init__(self, payload=None, player_id=None):
            self.payload = payload if isinstance(payload, dict) else {}
            self.player_id = player_id

        def marshal(self):
            return self.payload

        @classmethod
        def unmarshal(cls, data):
            return cls(data, data.get("__id__"))

    class ProductionUpdate(events.CustomS2CEvent):
        name = UPDATE_CHANNEL

        def __init__(self, payload=None):
            self.payload = payload if isinstance(payload, dict) else {}

        def marshal(self):
            return self.payload

        @classmethod
        def unmarshal(cls, data):
            return cls(data)

    return ProductionRequest, ProductionUpdate
