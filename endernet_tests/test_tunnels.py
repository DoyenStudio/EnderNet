# coding=utf-8
"""P2P 网络隧道（2026-11-13，`.claude/17` §4）：频率与一入多出、配置卡、拓扑连通、准入把关、耗能。

整条链路走 `ServerBootstrap`（`test_cable_panels.ServerRig`）：挂面板、配置卡都经意图服务，
拓扑、准入、运行时与能源都是真实装配。布局（y=64、z=0 平面，除非另写）：

    (0,63,0) 小型控制器    (0,65,0) 创造能量源
    (0,64,0) 8 位基带 —东→ (1,64,0) 玻璃光缆 H1 —东→ (2,64,0) 玻璃光缆 H2
             —南→ (0,64,1) 致密光缆 —东→ (1,64,1) 致密光缆 F1（H1 南面隧道的正面）
    H2 上面隧道的正面 F2 = (2,65,0) 玻璃光缆，再往上 (2,66,0) 通用终端（整方块）

H1、H2 在主网上（隧道面板各占 1u）；F2 与终端只经隧道接到主网。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.channels.cable_intent import (
    ACTION_DISMANTLE,
    ACTION_PLACE_PANEL,
    ACTION_TUNNEL_INPUT,
    ACTION_TUNNEL_OUTPUT,
    WRENCH_ID,
)
from endernet.common.definitions import CONFIG_CARD_ID, TUNNEL_PART_ID
from endernet.common.identity import PartKey
from endernet.common.protocol import Reason
from endernet.server.devices.tunnels import (
    CARD_DATA_KEY,
    GROUP_LINKED,
    GROUP_MULTIPLE_INPUTS,
    GROUP_NO_INPUT,
    REASON_ALREADY_INPUT,
    REASON_CARD_CLEARED,
    REASON_CARD_EMPTY,
    REASON_NOT_A_TUNNEL,
    REASON_TUNNEL_INPUT,
    REASON_TUNNEL_OUTPUT,
    endpoint_of_config,
    format_freq,
    parse_freq,
)
from endernet.server.network.admission import BandwidthState
from endernet.server.network.services import ServiceId
from endernet.server.network.topology import TunnelSegment
from test_cable_intents import CENTER
from test_cable_panels import GLASS, ServerRig, STONE
from test_cable_states import key

DENSE = "endernet:ender_dense_cable"
TERM = "endernet:common_terminal_panel"

H1, H2 = key(1), key(2)
F1, F2 = key(1, z=1), key(2, y=65)
SOUTH_FEED = key(0, z=1)
TERMINAL = key(2, y=66)
INPUT = PartKey(H1, "south")
OUTPUT = PartKey(H2, "up")


class CardHand(object):
    """主手物品数据的替身：按物品记 `userData` 与说明。"""

    def __init__(self):
        self.data = {}
        self.tips = {}

    def read(self, player_id, item_id):
        return dict(self.data.get(item_id, {}))

    def write(self, player_id, item_id, key_name, value, tips):
        data = self.data.setdefault(item_id, {})
        if value is None:
            data.pop(key_name, None)
        else:
            data[key_name] = dict(value)
        self.tips[item_id] = tips
        return True


class TunnelRig(ServerRig):
    def start(self):
        server = ServerRig.start(self)
        self.hand = CardHand()
        port = server.cable_intent_port
        port.read_held_data = self.hand.read
        port.write_held_data = self.hand.write
        port.notify = lambda player_id, text, title=None: self.notes.append(text)
        return server

    def hang(self, part_key):
        """手持隧道点宿主那一面挂上去。"""
        result = self.intent(part_key.node_key, GLASS, ACTION_PLACE_PANEL, CENTER, part_key.slot,
                             TUNNEL_PART_ID)
        self.settle(2)
        return result

    def card(self, part_key, action, block=GLASS):
        result = self.intent(part_key.node_key, block, action, CENTER, part_key.slot, CONFIG_CARD_ID)
        self.settle(4)
        return result

    def card_freq(self):
        return self.hand.data.get(CONFIG_CARD_ID, {}).get(CARD_DATA_KEY, {}).get("freq")

    def layout(self, host_two=H2, extra=()):
        self.network(host=H1)
        self.place(host_two, GLASS)
        self.place(SOUTH_FEED, DENSE)
        self.place(F1, DENSE)
        self.place(F2, GLASS)
        self.place(TERMINAL, TERM)
        for position, device_id in extra:
            self.place(position, device_id)
        self.settle()

    def term_status(self):
        return self.status(self.server.registry.get(TERMINAL).ref)


class TunnelFixture(unittest.TestCase):
    def setUp(self):
        self.rig = TunnelRig()

    def tearDown(self):
        self.rig.stop()

    def linked(self):
        """主网 + 两块隧道配成一组（H1 南面输入，H2 上面输出）。"""
        rig = self.rig
        rig.layout()
        rig.hang(INPUT)
        rig.hang(OUTPUT)
        self.assertEqual(rig.card(INPUT, ACTION_TUNNEL_INPUT).reason, REASON_TUNNEL_INPUT)
        self.assertEqual(rig.card(OUTPUT, ACTION_TUNNEL_OUTPUT).reason, REASON_TUNNEL_OUTPUT)
        return rig.card_freq()


class TestFrequencies(unittest.TestCase):
    def test_frequency_text(self):
        self.assertEqual(format_freq(0x3F2A), "3F2A")
        self.assertEqual(format_freq(5), "0005")
        self.assertEqual(parse_freq(u"3f2a"), "3F2A")
        self.assertIs(type(parse_freq(u"3f2a")), str)
        for bad in ("", "3F2", "3F2AB", "GGGG", None, 12):
            self.assertIsNone(parse_freq(bad), bad)

    def test_endpoint_config(self):
        self.assertEqual(endpoint_of_config({"freq": "00ff", "role": "input"}), ("00FF", "input"))
        self.assertIsNone(endpoint_of_config({}))
        self.assertIsNone(endpoint_of_config({"freq": "00FF", "role": "sideways"}))
        self.assertIsNone(endpoint_of_config(None))


class TestConfigCard(TunnelFixture):
    def test_sneak_click_sets_an_input_and_stores_the_frequency(self):
        rig = self.rig
        rig.layout()
        rig.hang(INPUT)
        result = rig.card(INPUT, ACTION_TUNNEL_INPUT)
        freq = rig.card_freq()
        self.assertTrue(result.ok)
        self.assertIsNotNone(parse_freq(freq))
        self.assertEqual(rig.world.part_configs[INPUT], {"freq": freq, "role": "input"})
        self.assertIn("频率 #%s" % (freq,), rig.hand.tips[CONFIG_CARD_ID])
        self.assertIn("已设为输入", result.message)

    def test_sneak_clicking_the_input_again_only_reads_its_frequency(self):
        rig = self.rig
        freq = self.linked()
        rig.hand.data.clear()
        result = rig.card(INPUT, ACTION_TUNNEL_INPUT)
        self.assertEqual((result.reason, rig.card_freq()), (REASON_TUNNEL_INPUT, freq))
        self.assertIn("已记下", result.message)
        self.assertEqual(rig.world.part_configs[INPUT]["freq"], freq)

    def test_sneak_click_elsewhere_clears_the_card(self):
        rig = self.rig
        self.linked()
        rig.world.blocks[key(5, y=70)] = STONE
        result = rig.intent(key(5, y=70), STONE, ACTION_TUNNEL_INPUT, CENTER, "up", CONFIG_CARD_ID)
        self.assertEqual(result.reason, REASON_CARD_CLEARED)
        self.assertIsNone(rig.card_freq())
        self.assertNotIn("频率 #", rig.hand.tips[CONFIG_CARD_ID])

    def test_an_empty_card_cannot_set_an_output(self):
        rig = self.rig
        rig.layout()
        rig.hang(OUTPUT)
        result = rig.card(OUTPUT, ACTION_TUNNEL_OUTPUT)
        self.assertEqual(result.reason, REASON_CARD_EMPTY)
        self.assertEqual(rig.world.part_configs.get(OUTPUT, {}), {})

    def test_the_input_does_not_become_its_own_output(self):
        rig = self.rig
        self.linked()
        self.assertEqual(rig.card(INPUT, ACTION_TUNNEL_OUTPUT).reason, REASON_ALREADY_INPUT)

    def test_clicking_a_cable_without_a_tunnel(self):
        rig = self.rig
        rig.layout()
        self.assertEqual(rig.card(PartKey(H2, "north"), ACTION_TUNNEL_OUTPUT).reason,
                         REASON_NOT_A_TUNNEL)

    def test_a_new_input_gets_a_new_frequency(self):
        rig = self.rig
        freq = self.linked()
        rig.card(OUTPUT, ACTION_TUNNEL_INPUT)
        self.assertNotEqual(rig.card_freq(), freq)


class TestTunnelTopology(TunnelFixture):
    def test_a_linked_group_joins_the_far_side(self):
        rig = self.rig
        rig.layout()
        rig.hang(INPUT)
        rig.hang(OUTPUT)
        snapshot = rig.server.topology.snapshot()
        self.assertIsNot(snapshot.component_of(TERMINAL), snapshot.component_of(H1),
                         "还没设频率：两侧不通")
        rig.card(INPUT, ACTION_TUNNEL_INPUT)
        rig.card(OUTPUT, ACTION_TUNNEL_OUTPUT)
        freq = rig.card_freq()
        snapshot = rig.server.topology.snapshot()
        self.assertIs(snapshot.component_of(TERMINAL), snapshot.component_of(H1))
        tree = snapshot.line_tree_of(TERMINAL)
        self.assertEqual(tree.path_of(TERMINAL)[:3], (F2, TunnelSegment(freq), F1))
        self.assertEqual(rig.server.tunnels.group_state(freq), GROUP_LINKED)

    def test_one_input_feeds_several_outputs(self):
        """一入多出（同 AE2）：同一张卡依次右键两块隧道，两块都是这个频率的输出，各自后面的终端
        都在线，两条出口挂在同一个隧道段下面、分摊这一组的 32u。"""
        rig = self.rig
        third_host, third_front, third_terminal = key(3), key(3, y=63), key(3, y=62)
        freq = self.linked()
        for position, device_id in ((third_host, GLASS), (third_front, GLASS),
                                    (third_terminal, TERM)):
            rig.place(position, device_id)
        rig.settle()
        second_output = PartKey(third_host, "down")
        rig.hang(second_output)
        self.assertEqual(rig.card(second_output, ACTION_TUNNEL_OUTPUT).reason, REASON_TUNNEL_OUTPUT)
        self.assertEqual(rig.card_freq(), freq, "设输出不动卡里的频率")
        server = rig.server
        self.assertEqual(server.tunnels.members_of(freq),
                         {INPUT: "input", OUTPUT: "output", second_output: "output"})
        tree = server.topology.snapshot().line_tree_of(TERMINAL)
        segment = TunnelSegment(freq)
        self.assertEqual(tree.node(F2).parent, segment)
        self.assertEqual(tree.node(third_front).parent, segment)
        for terminal in (TERMINAL, third_terminal):
            status = rig.status(server.registry.get(terminal).ref)
            self.assertTrue(status.is_online, (terminal, status.reasons))

    def test_the_far_terminal_is_online_through_the_tunnel(self):
        rig = self.rig
        self.linked()
        status = rig.term_status()
        self.assertTrue(status.is_online, status.reasons)

    def test_a_second_input_breaks_the_group(self):
        rig = self.rig
        freq = self.linked()
        rig.world.part_configs[OUTPUT] = {"freq": freq, "role": "input"}
        rig.server.tunnels._configs.pop(OUTPUT, None)
        rig.server.tunnels.refresh_host(H2)
        rig.settle(4)
        self.assertEqual(rig.server.tunnels.group_state(freq), GROUP_MULTIPLE_INPUTS)
        snapshot = rig.server.topology.snapshot()
        self.assertIsNot(snapshot.component_of(TERMINAL), snapshot.component_of(H1))

    def test_removing_the_input_cuts_the_outputs(self):
        rig = self.rig
        freq = self.linked()
        # 扳手潜行拆下输入那块：面板离开（PART_DETACHED），缓存的频率一并丢掉
        rig.intent(H1, GLASS, ACTION_DISMANTLE, CENTER, "south", WRENCH_ID)
        rig.settle(4)
        self.assertEqual(rig.server.tunnels.group_state(freq), GROUP_NO_INPUT)
        snapshot = rig.server.topology.snapshot()
        self.assertIsNot(snapshot.component_of(TERMINAL), snapshot.component_of(H1))
        self.assertNotIn(INPUT, rig.server.tunnels._configs)

    def test_a_cut_front_face_does_not_link(self):
        rig = self.rig
        self.linked()
        rig.server.cables.set_cut(F2, "down", True)
        rig.settle(4)
        snapshot = rig.server.topology.snapshot()
        self.assertIsNot(snapshot.component_of(TERMINAL), snapshot.component_of(H1))


class TestTunnelAdmission(TunnelFixture):
    def test_tunnel_panels_take_one_u_and_two_en(self):
        rig = self.rig
        self.linked()
        server = rig.server
        for part_key in (INPUT, OUTPUT):
            result = server.admission.status_of_part(part_key)
            self.assertEqual((result.state, result.demand_quarters), (BandwidthState.GRANTED, 4))
        energy = server.runtimes.runtime_of(H1).services.get(ServiceId.ENERGY)
        rows = [row for row in energy.member_view() if row["device_id"] == TUNNEL_PART_ID]
        self.assertEqual(sorted(row["demand"] for row in rows), [2, 2])

    def test_behind_a_tunnel_whose_end_has_no_bandwidth(self):
        """输入那块隧道的宿主是一格孤立的光缆：两侧照样连成一张网，但隧道后面的设备不准入。"""
        rig = self.rig
        lonely = key(1, z=2)
        rig.network(host=key(1, z=-5))
        for position, device_id in ((SOUTH_FEED, DENSE), (F1, DENSE), (H2, GLASS), (F2, GLASS),
                                    (TERMINAL, TERM), (lonely, GLASS)):
            rig.place(position, device_id)
        rig.settle()
        entry = PartKey(lonely, "north")
        rig.hang(entry)
        rig.hang(OUTPUT)
        rig.card(entry, ACTION_TUNNEL_INPUT)
        rig.card(OUTPUT, ACTION_TUNNEL_OUTPUT)
        snapshot = rig.server.topology.snapshot()
        self.assertIs(snapshot.component_of(TERMINAL), snapshot.component_of(F1))
        result = rig.server.admission.status_of(rig.server.registry.get(TERMINAL).ref)
        self.assertEqual(result.state, BandwidthState.DENIED)
        self.assertEqual(result.reason_codes(), (Reason.TUNNEL_OFFLINE,))
        detail = result.reasons[0].detail
        self.assertEqual(detail, "P2P 隧道（频率 #%s）未接通：(1, 64, 2) 未接入网络"
                         % (rig.card_freq(),))
        self.assertFalse(rig.term_status().is_online)

    def test_a_nested_tunnel_is_refused(self):
        """输出那块的宿主只经隧道本身才连得到主网：它自己不准入，隧道后面也就不通。"""
        rig = self.rig
        remote_host = key(4)
        far_front = key(4, y=65)
        rig.network(host=H1)
        for position, device_id in ((SOUTH_FEED, DENSE), (F1, DENSE), (remote_host, GLASS),
                                    (far_front, GLASS), (key(4, y=66), TERM),
                                    (key(5), GLASS), (key(5, y=65), GLASS)):
            rig.place(position, device_id)
        rig.settle()
        exit_part = PartKey(remote_host, "up")
        rig.hang(INPUT)
        rig.hang(exit_part)
        rig.card(INPUT, ACTION_TUNNEL_INPUT)
        rig.card(exit_part, ACTION_TUNNEL_OUTPUT)
        server = rig.server
        snapshot = server.topology.snapshot()
        self.assertIs(snapshot.component_of(remote_host), snapshot.component_of(H1))
        nested = server.admission.status_of_part(exit_part)
        self.assertEqual(nested.reason_codes(), (Reason.TUNNEL_NESTED,))
        far = server.admission.status_of(server.registry.get(key(4, y=66)).ref)
        self.assertEqual(far.reason_codes(), (Reason.TUNNEL_OFFLINE,))
        self.assertTrue(far.reasons[0].detail.endswith("(4, 64, 0) 隧道嵌套"), far.reasons[0].detail)

    def test_the_tunnel_comes_back_when_its_end_is_admitted_again(self):
        rig = self.rig
        self.linked()
        server = rig.server
        self.assertTrue(rig.term_status().is_online)
        # 宿主那一格切断与主网的连接：输入那块隧道没了带宽路径
        server.cables.set_cut(H1, "west", True)
        rig.settle(6)
        self.assertEqual(server.admission.status_of(server.registry.get(TERMINAL).ref).reason_codes(),
                         (Reason.TUNNEL_OFFLINE,))
        server.cables.set_cut(H1, "west", False)
        rig.settle(6)
        self.assertTrue(rig.term_status().is_online, rig.term_status().reasons)


if __name__ == "__main__":
    unittest.main()
