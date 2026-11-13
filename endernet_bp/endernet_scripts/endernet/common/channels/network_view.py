# coding=utf-8
"""Private, read-only viewer channel. No engine imports; both sides share the schema."""
from .cable_intent import _finite, _text, parse_intent
from ..protocol import _require_fields
from ..identity import _require_int

ACTION_VIEW = "view_network"
REQUEST = "endernet:network_view_request"
UPDATE = "endernet:network_view_update"
COLORS = {"green": (0.22, 0.9, 0.38, 1.0), "red": (1.0, 0.18, 0.16, 1.0),
          "orange": (1.0, 0.50, 0.08, 1.0),
          "gray": (0.55, 0.60, 0.65, 1.0), "line": (0.30, 0.72, 1.0, 1.0)}
LINE_WIDTH = 0.05
MARKER_SIZE = 0.36


def parse_request(data):
    _require_fields(data, ("session", "action", "intent"), "network_view_request")
    session = _text(data["session"], "session", False)
    if len(session) > 80 or data["action"] not in ("select", "close", "keepalive"):
        raise ValueError("invalid viewer request")
    if data["action"] == "select":
        intent = parse_intent(data["intent"])
        if intent.action != ACTION_VIEW:
            raise ValueError("invalid selection action")
    elif data["intent"] is not None:
        raise ValueError("unexpected intent")
    return session, data["action"], data["intent"]


def parse_update(data):
    _require_fields(data, ("session", "revision", "dimension", "clear", "message", "nodes", "edges"),
                    "network_view_update")
    _text(data["session"], "session", False)
    if _require_int(data["revision"], "revision") < 1:
        raise ValueError("invalid revision")
    _require_int(data["dimension"], "dimension")
    if type(data["clear"]) is not bool:
        raise ValueError("invalid clear flag")
    _text(data["message"], "message", True)
    if not isinstance(data["nodes"], list) or not isinstance(data["edges"], list):
        raise ValueError("invalid graph")
    ids = set()
    for node in data["nodes"]:
        _require_fields(node, ("id", "pos", "color", "marker"), "viewer_node")
        identity = _text(node["id"], "id", False)
        if identity in ids or node["color"] not in ("green", "red", "orange", "gray"):
            raise ValueError("invalid node")
        ids.add(identity)
        _finite(node["pos"], "pos")
        if type(node["marker"]) is not bool:
            raise ValueError("invalid marker")
    pairs = set()
    for edge in data["edges"]:
        if not isinstance(edge, (tuple, list)) or len(edge) != 2:
            raise ValueError("invalid edge")
        a, b = edge
        if a not in ids or b not in ids or a == b or (a, b) in pairs:
            raise ValueError("invalid edge endpoints")
        pairs.add((a, b))
    return data


def build_event_classes(events):
    class ViewRequest(events.CustomC2SEvent):
        name = REQUEST

        def __init__(self, payload=None, player_id=None):
            self.payload = payload if isinstance(payload, dict) else {}
            self.player_id = player_id

        def marshal(self):
            return self.payload

        @classmethod
        def unmarshal(cls, data):
            payload = dict(data) if isinstance(data, dict) else {}
            return cls(payload, payload.pop("__id__", None))

    class ViewUpdate(events.CustomS2CEvent):
        name = UPDATE

        def __init__(self, payload=None):
            self.payload = payload if isinstance(payload, dict) else {}

        def marshal(self):
            return self.payload

        @classmethod
        def unmarshal(cls, data):
            return cls(data)

    return ViewRequest, ViewUpdate
