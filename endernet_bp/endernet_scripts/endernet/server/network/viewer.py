# coding=utf-8
"""Per-player, transient viewer subscriptions. Reads topology; never changes it."""
import time

from ...common.cable_shapes import PART_PANEL, enter
from ...common.channels.network_view import ACTION_VIEW, parse_request
from ...common.definitions import NETWORK_VISUALIZER_ID
from ...common.identity import PartKey
from ..devices.cable_intents import IntentResult
from ..runtime import ChangeKind
from .view_graph import host

UPDATE_SECONDS = 0.2
LEASE_SECONDS = 5.0


class Selection(object):
    def __init__(self, session, key, record, face, now):
        self.session = session
        self.key = key
        self.ref = record.ref
        self.face = face
        self.revision = 0
        self.expires = now + LEASE_SECONDS
        self.last_publish = now - UPDATE_SECONDS
        self.dirty = True
        self.keys = set()
        self.content = None


class NetworkViewer(object):
    def __init__(self, graph, intents, players, publish=None, alive=None, now=None):
        self.graph = graph
        self.intents = intents
        self.players = players
        self.publish = publish if publish is not None else lambda player, data: None
        self.alive = alive if alive is not None else lambda player: True
        self.now = now if now is not None else time.time
        self.sessions = {}
        self._selecting = {}
        self.stats = {"published": 0, "built": 0, "rejected": 0}
        intents.register(ACTION_VIEW, self._selected, lambda item: item == NETWORK_VISUALIZER_ID,
                         any_block=True, needs_hit=False)
        graph.topology.add_listener(self.mark_dirty)
        graph.admission.add_listener(self.mark_dirty)

    def receive(self, player, payload):
        if not player:
            return
        try:
            session, action, intent = parse_request(payload)
        except (ValueError, TypeError, KeyError):
            self.stats["rejected"] += 1
            return
        current = self.sessions.get(player)
        if action == "close":
            if current is not None and current.session == session:
                self.close(player)
        elif action == "keepalive":
            if current is not None and current.session == session:
                current.expires = self.now() + LEASE_SECONDS
        else:
            self.close(player)
            self._selecting[player] = session
            try:
                result = self.intents.handle(player, intent)
            finally:
                self._selecting.pop(player, None)
            if not result.ok:
                self.stats["rejected"] += 1
                self.publish(player, {"session": session, "revision": 1, "dimension": 0,
                                      "clear": True, "nodes": [], "edges": [],
                                      "message": result.message or "无法查看该节点，请重新点击"})

    def _selected(self, context):
        session = self._selecting.get(context.player_id)
        record = context.record
        if session is None or record is None or not self.alive(context.player_id):
            return IntentResult(False, "viewer_invalid_target")
        # A remote camera may be behind the player, but must still be near them.
        if not self.intents.reach(context.intent.origin, context.position):
            return IntentResult(False, "viewer_invalid_ray")
        key = record.node_key
        face = context.face
        if self.graph.definitions.has_face_data(record.device_id):
            if context.hit is None:
                return IntentResult(False, "viewer_missed")
            if context.hit.part == PART_PANEL:
                key = PartKey(key, context.hit.face)
        else:
            hit = enter(((0, 0, 0), (1, 1, 1)), context.intent.local_origin(), context.intent.direction)
            if hit is None:
                return IntentResult(False, "viewer_missed")
            # Trust the actual ray intersection, not the client's claimed face.
            face = hit[1] or face
        self.sessions[context.player_id] = Selection(session, key, record, face, self.now())
        self.advance()
        return IntentResult(True, "viewer_selected")

    def mark_dirty(self, *unused):
        for selection in self.sessions.values():
            selection.dirty = True

    def observe(self, change):
        for player, selection in list(self.sessions.items()):
            if (change.kind == ChangeKind.PART_DETACHED
                    and isinstance(selection.key, PartKey)
                    and selection.key.node_key == change.node_key and selection.key.slot == change.detail):
                self.close(player)
            elif change.node_key in set(host(k) for k in selection.keys) or change.node_key == host(selection.key):
                selection.dirty = True

    def advance(self):
        now = self.now()
        for player, selection in list(self.sessions.items()):
            dimension = host(selection.key).dimension
            if (now >= selection.expires or not self.alive(player)
                    or self.players.held_item_of(player) != NETWORK_VISUALIZER_ID
                    or self.players.dimension_of(player) != dimension
                    or self.graph.registry.get_by_ref(selection.ref) is None
                    or not self.graph.exists(selection.key)):
                self.close(player)
                continue
            if now - selection.last_publish < UPDATE_SECONDS:
                continue
            if selection.dirty:
                nodes, edges, message, keys = self.graph.build(
                    selection.key, selection.face, dimension, selection.keys)
                self.stats["built"] += 1
                selection.keys = keys
                selection.dirty = False
            else:
                nodes, edges, message = selection.content
                nodes = self.graph.refresh_colors(selection.key, selection.face, nodes, selection.keys)
            selection.last_publish = now
            content = (nodes, edges, message)
            if selection.content == content:
                continue
            selection.content = content
            selection.revision += 1
            self.publish(player, {"session": selection.session, "revision": selection.revision,
                                  "dimension": dimension, "clear": False,
                                  "nodes": nodes, "edges": edges, "message": message})
            self.stats["published"] += 1

    def close(self, player, *unused):
        selection = self.sessions.pop(player, None)
        if selection is not None:
            self.publish(player, {"session": selection.session, "revision": selection.revision + 1,
                                  "dimension": host(selection.key).dimension, "clear": True,
                                  "nodes": [], "edges": [], "message": ""})

    def shutdown(self):
        for player in list(self.sessions):
            self.close(player)
        self._selecting.clear()

    def diagnostics(self):
        return dict(self.stats, subscriptions=len(self.sessions))
