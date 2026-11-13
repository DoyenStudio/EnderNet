# coding=utf-8
"""拓扑的远程连边（2026-11-13，`.claude/17` §4.3 / §5.3）：P2P 隧道与无线连接器共用的入口。

提供者用一个替身（`FakeLinks`）驱动：拓扑只认 `links_of(格子) -> [(远端, 隧道组)]`，条件
（配对、加载、距离）都在提供者那边，见 `test_tunnels.py` / `test_wireless.py`。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.server.network.topology import LineKind, TunnelSegment
from test_topology import BB8, CABLE16, DENSE, GLASS, TERM, TINY, NetworkFixture, key

WIRELESS = "endernet:wireless_connector"


class FakeLinks(object):
    """远程连边的替身：对称登记，`links_of` 原样交出。"""

    def __init__(self):
        self.links = {}
        self.calls = 0
        self.broken = False

    def connect(self, first, second, via=None):
        self.links.setdefault(first, []).append((second, via))
        self.links.setdefault(second, []).append((first, via))

    def cut(self, first, second):
        self.links[first] = [item for item in self.links.get(first, []) if item[0] != second]
        self.links[second] = [item for item in self.links.get(second, []) if item[0] != first]

    def links_of(self, node_key):
        self.calls += 1
        if self.broken:
            raise RuntimeError("provider broke")
        return list(self.links.get(node_key, ()))


class RemoteFixture(NetworkFixture):
    def setUp(self):
        NetworkFixture.setUp(self)
        self.links = FakeLinks()
        self.topology.add_remote_links(self.links)

    def core(self):
        """基带 + 主控，基带东面接一段致密光缆（外露面 east）。"""
        self.place(key(0), BB8)
        self.place(key(0, 65), TINY)


class TestRemoteComponents(RemoteFixture):
    def test_a_remote_link_joins_two_islands_into_one_network(self):
        self.core()
        self.place(key(1), DENSE)
        self.place(key(50), DENSE)
        self.place(key(51), TERM)
        self.links.connect(key(1), key(50), None)
        self.settle()
        near = self.component_of(key(1))
        self.assertIs(near, self.component_of(key(51)))
        self.assertTrue(near.is_runnable())
        self.assertIn(key(50), self.topology.link_state(key(1)).linked)

    def test_cutting_the_link_splits_the_network(self):
        self.core()
        self.place(key(1), DENSE)
        self.place(key(50), DENSE)
        self.links.connect(key(1), key(50), None)
        self.settle()
        self.assertIs(self.component_of(key(1)), self.component_of(key(50)))
        self.links.cut(key(1), key(50))
        self.topology.mark_dirty(key(1))
        self.topology.mark_dirty(key(50))
        self.settle()
        self.assertIsNot(self.component_of(key(1)), self.component_of(key(50)))
        self.assertFalse(self.component_of(key(50)).is_runnable())

    def test_an_end_without_a_record_is_not_linked(self):
        self.core()
        self.place(key(1), DENSE)
        self.links.connect(key(1), key(50), None)
        self.settle()
        self.assertEqual(set(self.component_of(key(1)).members), {key(0), key(0, 65), key(1)})

    def test_a_new_cell_revokes_the_far_network_at_once(self):
        """新放下的格子经远程连边能连上的那张网，标脏那一刻就进入重建中。"""
        self.core()
        self.place(key(1), DENSE)
        self.settle()
        self.place(key(50), DENSE)
        self.links.connect(key(1), key(50), None)
        self.topology.mark_dirty(key(50))
        self.topology.advance(0)
        self.assertTrue(self.topology.snapshot().component_of(key(1)).pending)
        self.settle()
        self.assertIs(self.component_of(key(1)), self.component_of(key(50)))

    def test_a_broken_provider_is_counted_not_raised(self):
        self.core()
        self.place(key(1), DENSE)
        self.links.broken = True
        self.settle()
        self.assertGreater(self.topology.stats["remote_link_errors"], 0)
        self.assertTrue(self.component_of(key(1)).is_runnable())


class TestTunnelSegments(RemoteFixture):
    def test_the_tunnel_segment_sits_between_the_two_fronts(self):
        self.core()
        self.place(key(1), DENSE)
        self.place(key(50), CABLE16)
        self.place(key(51), TERM)
        self.links.connect(key(1), key(50), "3F2A")
        self.settle()
        tree = self.topology.snapshot().line_tree_of(key(51))
        segment = TunnelSegment("3F2A")
        node = tree.node(segment)
        self.assertEqual(node.kind, LineKind.TUNNEL)
        self.assertEqual(node.parent, key(1))
        self.assertEqual(node.capacity_quarters, 32 * 4)
        self.assertEqual(node.device_id, "endernet:p2p_network_tunnel")
        self.assertEqual(tree.node(key(50)).parent, segment)
        root = "line:0:0,64,0:east"
        self.assertEqual(tree.path_of(key(51)), (key(50), segment, key(1), root))
        self.assertEqual(tree.consumers_of(root), [key(51)])
        self.assertEqual(tree.reported_of(root), [key(51)])
        self.assertEqual(tree.tunnel_line("3F2A"), root)
        self.assertEqual(tree.capacity_of(segment), 32 * 4)
        self.assertIn(("tunnel", "3F2A"), [entry[0] for entry in tree.signature_of(root)[1:]])

    def test_several_outputs_share_one_segment(self):
        self.core()
        self.place(key(1), DENSE)
        for x in (50, 60):
            self.place(key(x), GLASS)
            self.place(key(x + 1), TERM)
        self.links.connect(key(1), key(50), "AAAA")
        self.links.connect(key(1), key(60), "AAAA")
        self.links.connect(key(50), key(60), "AAAA")
        self.settle()
        tree = self.topology.snapshot().line_tree_of(key(1))
        segment = TunnelSegment("AAAA")
        self.assertEqual(tree.node(key(50)).parent, segment)
        self.assertEqual(tree.node(key(60)).parent, segment)
        self.assertEqual([k for k in tree.nodes if isinstance(k, TunnelSegment)], [segment])

    def test_direction_does_not_matter(self):
        """控制器在哪一端都一样：先走到的那一端是隧道段的父节点。"""
        self.place(key(100), BB8)
        self.place(key(100, 65), TINY)
        self.place(key(101), DENSE)
        self.place(key(1), DENSE)
        self.place(key(2), TERM)
        self.links.connect(key(1), key(101), "0001")
        self.settle()
        tree = self.topology.snapshot().line_tree_of(key(2))
        self.assertEqual(tree.node(TunnelSegment("0001")).parent, key(101))
        self.assertEqual(tree.node(key(1)).parent, TunnelSegment("0001"))

    def test_a_wireless_pair_hangs_directly(self):
        self.core()
        self.place(key(1), WIRELESS)
        self.place(key(80), WIRELESS)
        self.place(key(81), TERM)
        self.links.connect(key(1), key(80), None)
        self.settle()
        tree = self.topology.snapshot().line_tree_of(key(81))
        self.assertEqual(tree.node(key(1)).kind, LineKind.CABLE)
        self.assertEqual(tree.node(key(1)).capacity_quarters, 32 * 4)
        self.assertEqual(tree.node(key(80)).parent, key(1))
        self.assertEqual(tree.path_of(key(81)), (key(80), key(1), "line:0:0,64,0:east"))
        self.assertEqual(tree.consumers_of("line:0:0,64,0:east"), [key(81)])


if __name__ == "__main__":
    unittest.main()
