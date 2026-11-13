# coding=utf-8
"""Viewer regression coverage: authoritative routes, lifetimes, transport and render ownership."""
import unittest

import testsupport
from endernet.common.channels.network_view import ACTION_VIEW, build_event_classes, parse_update
from endernet.common.definitions import NETWORK_VISUALIZER_ID
from endernet.common.identity import PartKey
from endernet.client.channels.network_view import ViewerState, ViewRule
from endernet.client.interaction.takeover import BlockUse
from endernet.client.network_overlay import OverlayRenderer
from endernet.server.network.admission import AdmissionService
from endernet.server.network.view_graph import ViewGraph, identity, point
from endernet.server.network.viewer import NetworkViewer
from endernet.server.runtime.lifecycle import LifecycleChange, ChangeKind
from test_admission import AdmissionFixture, BB8, TINY, TERM, key
from test_topology import GLASS, TERM_PART
from endernet.server.devices.catalog import CABLE_PARTS_KEY
from test_cable_intents import IntentRig, payload_for, CENTER
from test_remote_links import FakeLinks
from test_device_status import make_status


class TunnelEndpoints(object):
    def __init__(self):
        self.mapping = {}

    def endpoints_at(self, via, front):
        return self.mapping.get((via, front), ())


class StatusReader(object):
    def __init__(self):
        self.online = True
        self.validity = "ok"
        self.refs = []

    def __call__(self, ref):
        self.refs.append(ref)
        return make_status(ref, online=self.online, validity=self.validity)


class GraphTests(AdmissionFixture):
    def setUp(self):
        AdmissionFixture.setUp(self)
        self.tunnels = TunnelEndpoints()
        self.status_reader = StatusReader()
        self.graph = ViewGraph(self.defs, self.registry, self.catalog, self.topology,
                               self.admission, self.tunnels, self.status_reader)

    def network(self):
        self.place(key(0), BB8)
        self.place(key(0, 63), TINY)
        self.place(key(1), GLASS)
        self.place(key(2), TERM)
        self.place(key(-1), GLASS)
        self.place(key(-2), TERM)
        self.settle()

    def test_selects_one_line_and_baseband_face(self):
        self.network()
        nodes, edges, message, keys = self.graph.build(key(1), "", 0)
        self.assertEqual(keys, set([key(0), key(1), key(2)]))
        self.assertEqual(len(edges), 2)
        self.assertEqual(next(n for n in nodes if n["id"] == identity(key(0)))["pos"], [1.0, 64.5, 0.5])
        self.assertEqual(next(n for n in nodes if n["id"] == identity(key(2)))["color"], "green")
        self.assertEqual(self.graph.build(key(0), "west", 0)[3], set([key(0), key(-1), key(-2)]))

    def test_overflow_stays_red_when_devices_are_offline(self):
        trunk, branch, terminals = self.glass_branch_on_dense_trunk()
        nodes = dict((n["id"], n) for n in self.graph.build(branch[0], "", 0)[0])
        self.assertEqual(sum(nodes[identity(k)]["color"] == "green" for k in terminals), 8)
        self.assertEqual(sum(nodes[identity(k)]["color"] == "red" for k in terminals), 1)
        self.status_reader.online = False
        nodes = dict((n["id"], n) for n in self.graph.build(branch[0], "", 0)[0])
        self.assertEqual(sum(nodes[identity(k)]["color"] == "orange" for k in terminals), 8)
        self.assertEqual(sum(nodes[identity(k)]["color"] == "red" for k in terminals), 1)

    def test_offline_pending_unloaded_and_online_colors(self):
        self.network()
        self.status_reader.online = False
        self.assertEqual(self.graph.color(key(2)), "orange")
        self.status_reader.validity = "pending"
        self.assertEqual(self.graph.color(key(2)), "gray")
        self.status_reader.validity = "ok"
        self.registry.get(key(2)).loaded = False
        self.assertEqual(self.graph.color(key(2)), "gray")
        self.registry.get(key(2)).loaded = True
        self.status_reader.online = True
        self.assertEqual(self.graph.color(key(2)), "green")

    def test_loop_contains_only_the_chosen_tree_edges(self):
        self.network()
        for position in (key(1, 64, 1), key(2, 64, 1)):
            self.place(position, GLASS)
        self.settle()
        nodes, edges, message, keys = self.graph.build(key(1), "", 0)
        self.assertEqual(len(edges), len(keys) - 1)
        self.assertNotIn(identity(key(-1)), [n["id"] for n in nodes])

    def test_unconfigured_bandwidth_is_gray_on_a_valid_network(self):
        self.network()
        self.place(key(1, 65), "endernet:parallel_processor_connector")
        self.settle()
        nodes = dict((n["id"], n) for n in self.graph.build(key(1), "", 0)[0])
        self.assertEqual(nodes[identity(key(1, 65))]["color"], "gray")
        self.assertEqual(nodes[identity(key(2))]["color"], "green")

    def test_rebuild_removes_links_and_invalidates_green(self):
        self.network()
        old = self.graph.build(key(1), "", 0)[3]
        self.topology.mark_dirty(key(1))
        self.topology.advance(0)
        nodes, edges, message, keys = self.graph.build(key(1), "", 0, old)
        self.assertEqual(edges, [])
        self.assertTrue(all(n["color"] == "gray" for n in nodes))
        self.assertIn("重建", message)
        self.settle()
        self.assertEqual(len(self.graph.build(key(1), "", 0)[1]), 2)

    def test_core_without_a_line_is_gray(self):
        self.network()
        nodes, edges, message, keys = self.graph.build(key(0, 63), "", 0)
        self.assertEqual((len(nodes), edges, nodes[0]["color"]), (1, [], "gray"))
        self.assertEqual(message, "未找到带宽线路")

    def test_color_poll_preserves_root_position_and_pending_or_no_line_gray(self):
        self.network()
        nodes, edges, message, keys = self.graph.build(key(0), "east", 0)
        root = next(n for n in nodes if n["id"] == identity(key(0)))
        self.status_reader.online = False
        refreshed = self.graph.refresh_colors(key(0), "east", [root], set([key(0)]))
        self.assertEqual(refreshed[0]["color"], "orange")
        self.assertEqual(refreshed[0]["pos"], root["pos"])
        self.assertEqual(root["color"], "green")
        core = key(0, 63)
        gray, edges, message, keys = self.graph.build(core, "", 0)
        self.assertEqual(self.graph.refresh_colors(core, "", gray, keys)[0]["color"], "gray")
        self.topology.mark_dirty(key(0))
        self.topology.advance(0)
        gray, edges, message, keys = self.graph.build(key(0), "east", 0)
        self.assertTrue(all(n["color"] == "gray"
                            for n in self.graph.refresh_colors(key(0), "east", gray, keys)))

    def test_panel_center_and_independent_marker(self):
        self.network()
        self.registry.get(key(1)).config[CABLE_PARTS_KEY] = {"up": TERM_PART}
        self.topology.mark_dirty(key(1))
        self.settle()
        part = PartKey(key(1), "up")
        self.assertEqual(point(part), [1.5, 64.96875, 0.5])
        nodes = dict((n["id"], n) for n in self.graph.build(part, "", 0)[0])
        self.assertTrue(nodes[identity(part)]["marker"])
        self.assertFalse(nodes[identity(key(1))]["marker"])
        self.status_reader.online = False
        nodes = dict((n["id"], n) for n in self.graph.build(part, "", 0)[0])
        self.assertEqual(nodes[identity(part)]["color"], "orange")
        self.assertTrue(any(ref.slot == "up" for ref in self.status_reader.refs))

    def test_wireless_remote_edges_and_cross_dimension_clipping(self):
        self.network()
        remote = FakeLinks()
        self.topology.add_remote_links(remote)
        self.place(key(20, dimension=1), GLASS)
        self.place(key(21, dimension=1), TERM)
        remote.connect(key(1), key(20, dimension=1))
        self.topology.mark_dirty(key(1))
        self.settle()
        nodes, edges, message, keys = self.graph.build(key(1), "", 0)
        self.assertIn(key(21, dimension=1), keys)
        self.assertNotIn(identity(key(20, dimension=1)), [n["id"] for n in nodes])
        self.assertIn("跨维度", message)

    def test_tunnel_uses_real_panel_endpoints(self):
        self.network()
        remote = FakeLinks()
        self.topology.add_remote_links(remote)
        self.place(key(20), GLASS)
        self.place(key(21), TERM)
        self.place(key(1, 65), GLASS, {CABLE_PARTS_KEY: {"down": "endernet:p2p_network_tunnel"}})
        self.place(key(20, 65), GLASS, {CABLE_PARTS_KEY: {"down": "endernet:p2p_network_tunnel"}})
        source, target = PartKey(key(1, 65), "down"), PartKey(key(20, 65), "down")
        self.tunnels.mapping[("ABCD", key(1))] = (source,)
        self.tunnels.mapping[("ABCD", key(20))] = (target,)
        remote.connect(key(1), key(20), "ABCD")
        self.topology.mark_dirty(key(1))
        self.settle()
        nodes, edges, message, keys = self.graph.build(key(1), "", 0)
        self.assertIn([identity(source), identity(target)], edges)
        self.assertIn([identity(key(1)), identity(source)], edges)
        self.assertIn([identity(target), identity(key(20))], edges)


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.rig = rig = IntentRig()
        rig.players.item = NETWORK_VISUALIZER_ID
        rig.place(key(0), GLASS)
        rig.step(3)
        admission = AdmissionService(rig.definitions, rig.registry, rig.topology)
        self.status_reader = StatusReader()
        graph = ViewGraph(rig.definitions, rig.registry, rig.catalog, rig.topology, admission,
                          TunnelEndpoints(), self.status_reader)
        self.sent = []
        self.time = [0.0]
        self.alive = [True]
        self.viewer = NetworkViewer(graph, rig.service, rig.players,
                                    lambda p, d: self.sent.append((p, d)),
                                    alive=lambda p: self.alive[0], now=lambda: self.time[0])

    def select(self, player="one", session="s1", position=None, block=GLASS):
        self.rig.clock.tick += 1
        intent = payload_for(position or key(0), block, ACTION_VIEW, CENTER, "south",
                             item=NETWORK_VISUALIZER_ID)
        self.viewer.receive(player, {"session": session, "action": "select", "intent": intent})

    def test_private_subscriptions_and_wrong_close(self):
        self.select()
        self.select("two", "s2")
        self.assertEqual(set(self.viewer.sessions), set(["one", "two"]))
        self.viewer.receive("one", {"session": "s2", "action": "close", "intent": None})
        self.assertIn("one", self.viewer.sessions)
        self.viewer.close("one")
        self.assertEqual(set(self.viewer.sessions), set(["two"]))
        self.assertEqual(self.sent[-1][0], "one")

    def test_swapping_item_death_dimension_and_expiry_close(self):
        for reason in ("item", "death", "dimension", "expiry"):
            self.rig.players.item = NETWORK_VISUALIZER_ID
            self.rig.players.dimension = 0
            self.alive[0] = True
            self.select(session=reason)
            self.assertIn("one", self.viewer.sessions)
            if reason == "item":
                self.rig.players.item = "minecraft:stick"
            elif reason == "death":
                self.alive[0] = False
            elif reason == "dimension":
                self.rig.players.dimension = 1
            else:
                self.time[0] += 6
            self.viewer.advance()
            self.assertEqual(self.viewer.sessions, {})
            self.assertTrue(self.sent[-1][1]["clear"])

    def test_same_position_replacement_does_not_inherit_selection(self):
        self.select()
        self.rig.remove(key(0), GLASS)
        self.rig.place(key(0), GLASS)
        self.viewer.advance()
        self.assertFalse(self.viewer.sessions)

    def test_panel_detach_invalidates_even_if_replaced_between_ticks(self):
        self.rig.remove(key(0), GLASS)
        self.rig.place(key(0), GLASS, parts={"south": TERM_PART})
        self.rig.step(3)
        self.select()
        self.assertEqual(self.viewer.sessions["one"].key.slot, "south")
        self.viewer.observe(LifecycleChange(ChangeKind.PART_DETACHED, key(0), TERM_PART,
                                             self.rig.registry.get(key(0)).ref, "south"))
        self.assertFalse(self.viewer.sessions)

    def test_wrong_item_distance_and_malformed_packet_are_rejected(self):
        self.rig.players.item = "minecraft:stick"
        self.select()
        self.assertFalse(self.viewer.sessions)
        self.rig.players.item = NETWORK_VISUALIZER_ID
        self.rig.players.position = (1000, 64, 0)
        self.select()
        self.assertFalse(self.viewer.sessions)
        self.viewer.receive("one", {"session": []})
        self.assertGreaterEqual(self.viewer.stats["rejected"], 3)

    def test_dirty_updates_coalesce_and_idle_does_no_work(self):
        self.select()
        built = self.viewer.stats["built"]
        for unused in range(10):
            self.viewer.mark_dirty()
            self.viewer.advance()
        self.assertEqual(self.viewer.stats["built"], built)
        self.time[0] += 0.21
        self.viewer.advance()
        self.assertEqual(self.viewer.stats["built"], built + 1)
        self.assertEqual(self.viewer.stats["published"], 1)
        self.viewer.shutdown()
        self.viewer.mark_dirty()
        self.viewer.advance()
        self.assertEqual(self.viewer.stats["built"], built + 1)

    def test_power_status_changes_refresh_without_rebuilding_route(self):
        self.rig.place(key(-1), BB8)
        self.rig.place(key(-1, 63), TINY)
        self.rig.place(key(1), TERM)
        self.rig.step(5)
        self.select()
        original = self.sent[-1][1]
        self.assertTrue(original["edges"])
        built = self.viewer.stats["built"]
        self.status_reader.online = False
        self.time[0] += 0.21
        self.viewer.advance()
        offline = self.sent[-1][1]
        self.assertEqual(next(n["color"] for n in offline["nodes"] if n["id"] == identity(key(1))), "orange")
        self.assertEqual(offline["edges"], original["edges"])
        self.assertEqual(self.viewer.stats["built"], built)
        count = len(self.sent)
        self.time[0] += 0.21
        self.viewer.advance()
        self.assertEqual(len(self.sent), count)
        self.status_reader.online = True
        self.time[0] += 0.21
        self.viewer.advance()
        self.assertEqual(next(n["color"] for n in self.sent[-1][1]["nodes"]
                              if n["id"] == identity(key(1))), "green")
        self.assertGreater(self.sent[-1][1]["revision"], offline["revision"])


class FakeModelPort(object):
    def __init__(self):
        self.created, self.updated, self.removed = [], [], []

    def create(self, dimension):
        self.created.append(dimension)
        return len(self.created)

    def update(self, model, value):
        self.updated.append((model, value))
        return True

    def remove(self, model):
        self.removed.append(model)


def snapshot(session, revision=1):
    return {"session": session, "revision": revision, "dimension": 0, "clear": False, "message": "",
            "nodes": [{"id": "a", "pos": [0, 64, 0], "color": "green", "marker": True},
                      {"id": "b", "pos": [1, 64, 0], "color": "gray", "marker": False}],
            "edges": [["a", "b"]]}


class ClientTests(unittest.TestCase):
    def test_late_packets_cannot_reopen_or_replace_new_session(self):
        port = FakeModelPort()
        renderer = OverlayRenderer(port)
        sent = []
        state = ViewerState(sent.append, renderer)
        state.select({}, 0)
        first = state.session
        self.assertTrue(state.receive(snapshot(first)))
        state.tick(NETWORK_VISUALIZER_ID, 0)
        self.assertEqual(len(renderer.models), 2)
        self.assertFalse(state.receive(snapshot(first)))
        state.select({}, 0)
        self.assertFalse(state.receive(snapshot(first, 2)))
        state.tick("minecraft:stick", 0)
        self.assertFalse(state.receive(snapshot(sent[-1]["session"], 3)))
        self.assertFalse(renderer.models)

    def test_models_are_reused_and_creation_is_budgeted(self):
        port = FakeModelPort()
        renderer = OverlayRenderer(port, budget=1)
        data = snapshot("s")
        renderer.apply(data)
        renderer.advance()
        self.assertEqual(len(port.created), 1)
        renderer.advance()
        self.assertEqual(len(port.created), 2)
        data["nodes"][0]["color"] = "orange"
        self.assertIs(parse_update(data), data)
        renderer.apply(data)
        renderer.advance()
        self.assertEqual(len(port.created), 2)
        self.assertEqual(len(port.updated), 3)
        data["edges"] = []
        renderer.apply(data)
        self.assertEqual(len(port.removed), 1)
        renderer.clear()
        self.assertEqual(len(port.removed), 2)

    def test_engine_sender_and_packet_validation(self):
        request, update = build_event_classes(testsupport.FakeEventsModule())
        event = request.unmarshal({"__id__": "real", "session": "s"})
        self.assertEqual(event.player_id, "real")
        self.assertNotIn("__id__", event.payload)
        data = snapshot("s")
        data["edges"] = [["a", "missing"]]
        with self.assertRaises(ValueError):
            parse_update(data)

    def test_viewer_claims_devices_without_sneaking(self):
        rule = ViewRule()
        for block in (TERM, GLASS, BB8, "endernet:storage_driver"):
            use = BlockUse((0, 64, 0), block, NETWORK_VISUALIZER_ID, None, None, False)
            self.assertTrue(rule.wants(use))
            self.assertEqual(rule.decide(use, False), ACTION_VIEW)
        self.assertFalse(rule.wants(BlockUse((0, 0, 0), "minecraft:stone", NETWORK_VISUALIZER_ID,
                                             None, None, False)))

    def test_server_fallback_cancels_terminal_and_container_opening(self):
        from test_cable_panels import ServerRig

        class Event(object):
            x, y, z = 0, 64, 0
            playerId = "one"
            item = {"newItemName": NETWORK_VISUALIZER_ID}
            cancelled = False

            def cancel(self):
                self.cancelled = True

        rig = ServerRig()
        try:
            for block in (TERM, "endernet:storage_driver"):
                event = Event()
                event.blockName = block
                rig.server.event_binder._on_block_use(event)
                self.assertTrue(event.cancelled)
                self.assertEqual(rig.opened, [])
        finally:
            rig.stop()


if __name__ == "__main__":
    unittest.main()
