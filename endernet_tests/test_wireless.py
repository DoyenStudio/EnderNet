# coding=utf-8
"""无线连接器与无线连接工具（2026-11-13，`.claude/17` §5），以及配置卡 / 工具的客户端动作表。

整条链路走 `ServerBootstrap`（`test_cable_panels.ServerRig`），世界替身多一份连接器的配对字段。
主网：8 位基带 (0,64,0)、控制器在下、能量源在上、东边一格玻璃光缆 (1,64,0)，再往东 (2,64,0)
是连接器 A；远处 (40,64,0) 是连接器 B，旁边 (41,64,0) 一台通用终端。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.channels.cable_intent import (
    ACTION_TUNNEL_INPUT,
    ACTION_TUNNEL_OUTPUT,
    ACTION_WIRELESS_LINK,
    ACTION_WIRELESS_UNLINK,
    action_for,
)
from endernet.common.definitions import (
    CONFIG_CARD_ID,
    WIRELESS_CONNECTOR_ID,
    WIRELESS_LINK_TOOL_ID,
    wireless_distance_units,
)
from endernet.server.devices.wireless import (
    CONFIG_LINK,
    CONFIG_MARK,
    CONFIG_PENDING,
    REASON_LINKED,
    REASON_OTHER_DIMENSION,
    REASON_SELECTED,
    REASON_TOO_FAR,
    REASON_TOOL_CLEARED,
    REASON_UNLINKED,
    TOOL_DATA_KEY,
    link_text,
    parse_link,
)
from endernet.server.network.services import ServiceId
from endernet.server.runtime.events import NormalizedEvent
from test_cable_intents import CENTER, PLAYER
from test_cable_panels import GLASS, PanelWorld, ServerRig, STONE
from test_cable_states import key
from test_tunnels import CardHand

TERM = "endernet:common_terminal_panel"
A, B = key(2), key(40)
FAR_TERMINAL = key(41)


class WirelessWorld(PanelWorld):
    """面板假世界 + 无线连接器的配对字段（`wireless_link` / `write_wireless_link`）。"""

    def __init__(self):
        PanelWorld.__init__(self)
        self.links = {}
        self.unreadable = set()

    def wireless_link(self, node_key):
        if node_key in self.unreadable:
            return None
        return self.links.get(node_key, "")

    def write_wireless_link(self, node_key, text):
        if node_key in self.unreadable:
            return False
        self.links[node_key] = text
        return True

    def terminal_inventory_id(self, node_key):
        if node_key in self.unreadable:
            return None
        return PanelWorld.terminal_inventory_id(self, node_key)


class WirelessRig(ServerRig):
    def __init__(self):
        ServerRig.__init__(self, world=WirelessWorld())

    def start(self):
        server = ServerRig.start(self)
        self.hand = CardHand()
        port = server.cable_intent_port
        port.read_held_data = self.hand.read
        port.write_held_data = self.hand.write
        port.notify = lambda player_id, text, title=None: self.notes.append(text)
        return server

    def layout(self, far=B, dimension=0):
        self.network()
        self.place(A, WIRELESS_CONNECTOR_ID)
        self.place(far, WIRELESS_CONNECTOR_ID)
        self.place(far.offset(1, 0, 0), TERM)
        self.settle()

    def tool(self, node_key, action, block=WIRELESS_CONNECTOR_ID):
        """站到那一格旁边，手持工具右键它。"""
        self.positions[PLAYER] = (node_key.x + 0.5, node_key.y, node_key.z + 2.5)
        self.dimensions[PLAYER] = node_key.dimension
        result = self.intent(node_key, block, action, CENTER, "south", WIRELESS_LINK_TOOL_ID)
        self.settle(4)
        return result

    def selection(self):
        return self.hand.data.get(WIRELESS_LINK_TOOL_ID, {}).get(TOOL_DATA_KEY, {}).get("link")

    def component(self, node_key):
        return self.server.topology.snapshot().component_of(node_key)


class WirelessFixture(unittest.TestCase):
    def setUp(self):
        self.rig = WirelessRig()

    def tearDown(self):
        self.rig.stop()

    def paired(self, far=B):
        rig = self.rig
        rig.layout(far)
        self.assertEqual(rig.tool(A, ACTION_WIRELESS_LINK).reason, REASON_SELECTED)
        result = rig.tool(far, ACTION_WIRELESS_LINK)
        self.assertEqual(result.reason, REASON_LINKED, result.message)
        return result


class TestLinkText(unittest.TestCase):
    def test_round_trip(self):
        text = link_text(key(3, y=70, z=-2, dimension=1), "tok-1")
        self.assertEqual(text, "1,3,70,-2,tok-1")
        self.assertEqual(parse_link(text), (key(3, y=70, z=-2, dimension=1), "tok-1"))
        for bad in ("", "1,2,3", "1,2,3,4,", "a,2,3,4,x", None):
            self.assertIsNone(parse_link(bad), bad)


class TestClientActions(unittest.TestCase):
    def test_config_card(self):
        self.assertEqual(action_for(CONFIG_CARD_ID, True, STONE), ACTION_TUNNEL_INPUT)
        self.assertEqual(action_for(CONFIG_CARD_ID, True, GLASS), ACTION_TUNNEL_INPUT)
        self.assertEqual(action_for(CONFIG_CARD_ID, False, GLASS), ACTION_TUNNEL_OUTPUT)
        self.assertIsNone(action_for(CONFIG_CARD_ID, False, STONE))

    def test_wireless_link_tool(self):
        self.assertEqual(action_for(WIRELESS_LINK_TOOL_ID, False, WIRELESS_CONNECTOR_ID),
                         ACTION_WIRELESS_LINK)
        self.assertIsNone(action_for(WIRELESS_LINK_TOOL_ID, False, GLASS))
        self.assertEqual(action_for(WIRELESS_LINK_TOOL_ID, True, STONE), ACTION_WIRELESS_UNLINK)
        self.assertEqual(action_for(WIRELESS_LINK_TOOL_ID, True, WIRELESS_CONNECTOR_ID),
                         ACTION_WIRELESS_UNLINK)


class TestPairing(WirelessFixture):
    def test_selecting_writes_the_tool(self):
        rig = self.rig
        rig.layout()
        rig.tool(A, ACTION_WIRELESS_LINK)
        mark = rig.server.registry.get(A).config[CONFIG_MARK]
        self.assertEqual(rig.selection(), link_text(A, mark))
        self.assertIn("待配对：(2, 64, 0)", rig.hand.tips[WIRELESS_LINK_TOOL_ID])

    def test_pairing_joins_the_far_side_into_one_network(self):
        rig = self.rig
        result = self.paired()
        server = rig.server
        self.assertIn("相距 38 格", result.message)
        self.assertIsNone(rig.selection(), "配对后工具清空")
        marks = dict((k, server.registry.get(k).config[CONFIG_MARK]) for k in (A, B))
        self.assertEqual(rig.world.links[A], link_text(B, marks[B]))
        self.assertEqual(rig.world.links[B], link_text(A, marks[A]))
        self.assertIs(rig.component(FAR_TERMINAL), rig.component(A))
        status = rig.status(server.registry.get(FAR_TERMINAL).ref)
        self.assertTrue(status.is_online, status.reasons)
        tree = server.topology.snapshot().line_tree_of(FAR_TERMINAL)
        self.assertEqual(tree.path_of(FAR_TERMINAL)[:3], (B, A, key(1)))

    def test_each_end_pays_for_the_distance(self):
        rig = self.rig
        self.paired()
        energy = rig.server.runtimes.runtime_of(A).services.get(ServiceId.ENERGY)
        rows = [row for row in energy.member_view() if row["device_id"] == WIRELESS_CONNECTOR_ID]
        units = wireless_distance_units(38)
        self.assertEqual(units, 6)
        self.assertEqual(sorted(row["demand"] for row in rows), [4 + units, 4 + units])

    def test_an_unpaired_connector_costs_its_base(self):
        rig = self.rig
        rig.layout()
        energy = rig.server.runtimes.runtime_of(A).services.get(ServiceId.ENERGY)
        rows = [row for row in energy.member_view() if row["device_id"] == WIRELESS_CONNECTOR_ID]
        self.assertEqual([row["demand"] for row in rows], [4])

    def test_unlinking_splits_both_ends(self):
        rig = self.rig
        self.paired()
        result = rig.tool(A, ACTION_WIRELESS_UNLINK)
        self.assertEqual(result.reason, REASON_UNLINKED)
        self.assertEqual((rig.world.links[A], rig.world.links[B]), ("", ""))
        self.assertIsNot(rig.component(FAR_TERMINAL), rig.component(A))

    def test_sneak_click_elsewhere_clears_the_tool(self):
        rig = self.rig
        rig.layout()
        rig.tool(A, ACTION_WIRELESS_LINK)
        rig.world.blocks[key(5, y=70)] = STONE
        result = rig.tool(key(5, y=70), ACTION_WIRELESS_UNLINK, block=STONE)
        self.assertEqual(result.reason, REASON_TOOL_CLEARED)
        self.assertIsNone(rig.selection())

    def test_too_far_is_refused(self):
        rig = self.rig
        far = key(300)
        rig.layout(far)
        rig.tool(A, ACTION_WIRELESS_LINK)
        result = rig.tool(far, ACTION_WIRELESS_LINK)
        self.assertEqual(result.reason, REASON_TOO_FAR)
        self.assertEqual(rig.world.links.get(A, ""), "")

    def test_another_dimension_is_refused(self):
        rig = self.rig
        far = key(40, dimension=1)
        rig.layout(far)
        rig.tool(A, ACTION_WIRELESS_LINK)
        self.assertEqual(rig.tool(far, ACTION_WIRELESS_LINK).reason, REASON_OTHER_DIMENSION)

    def test_a_replaced_connector_drops_the_pairing(self):
        """对端拆了再放一台：标记不同，配对自然失效。"""
        rig = self.rig
        self.paired()
        rig.remove(B, WIRELESS_CONNECTOR_ID)
        rig.settle()
        rig.world.markers.pop(B, None)
        rig.world.links[B] = ""
        rig.place(B, WIRELESS_CONNECTOR_ID)
        rig.settle()
        self.assertIsNone(rig.server.wireless.partner_of(A))
        self.assertIsNot(rig.component(B), rig.component(A))

    def test_pairing_with_a_third_unpairs_the_old_one(self):
        rig = self.rig
        third = key(20)
        self.paired()
        rig.place(third, WIRELESS_CONNECTOR_ID)
        rig.settle()
        rig.tool(third, ACTION_WIRELESS_LINK)
        self.assertEqual(rig.tool(A, ACTION_WIRELESS_LINK).reason, REASON_LINKED)
        self.assertEqual(rig.world.links[B], "")
        self.assertEqual(rig.server.wireless.partner_of(A), third)
        self.assertIsNot(rig.component(B), rig.component(A))

    def test_an_unloaded_far_end_only_breaks_the_link(self):
        """远端区块卸载：这条边不在了，远端不是成员，近端照常运行（不触发整网离线）。"""
        rig = self.rig
        self.paired()
        rig.server.push(NormalizedEvent.chunk_unloaded(B.chunk_key()))
        rig.settle()
        near = rig.component(A)
        self.assertTrue(near.is_runnable(), near.reason_list())
        self.assertNotIn(B, near.members)
        self.assertIsNone(rig.server.wireless.partner_of(A))


class TestEntityReread(unittest.TestCase):
    def test_an_unreadable_entity_is_read_again_later(self):
        rig = WirelessRig()
        try:
            rig.world.unreadable.add(A)
            rig.layout()
            record = rig.server.registry.get(A)
            self.assertTrue(record.config.get(CONFIG_PENDING))
            rig.world.unreadable.discard(A)
            rig.settle(40)
            record = rig.server.registry.get(A)
            self.assertIsNone(record.config.get(CONFIG_PENDING))
            self.assertTrue(record.config.get(CONFIG_MARK))
            self.assertEqual(record.config.get(CONFIG_LINK), "")
        finally:
            rig.stop()


if __name__ == "__main__":
    unittest.main()
