# coding=utf-8
"""`slot_layout` 的路径解析与命中范围。"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.client.ui import slot_layout


class FakeScreen(object):
    def __init__(self, tree):
        self.tree = tree
        self.lookups = []

    def GetBaseUIControl(self, path):
        self.lookups.append(path)
        return self.tree.get(path)


class TestMountRoot(unittest.TestCase):
    def test_every_lookup_goes_through_the_window_under_the_full_screen_root(self):
        window = slot_layout.full_path("")
        screen = FakeScreen({window: "window", window + "/inventory": "inventory"})
        self.assertEqual(slot_layout.control(screen, slot_layout.CONTENT_PATH), "window")
        screen.lookups = []
        self.assertEqual(slot_layout.control(screen, "/inventory"), "inventory")
        self.assertEqual(screen.lookups, [window + "/inventory"], "只问一次，不再试旧挂法")
        self.assertEqual(slot_layout.path_of(screen, "/inventory"), window + "/inventory")
        self.assertIsNone(slot_layout.path_of(screen, "/slot_info"))


class TestDeviceGrids(unittest.TestCase):
    def test_only_the_screens_own_device_areas_are_hit_tested(self):
        screen = FakeScreen({})
        self.assertEqual(slot_layout.device_grids(screen), [])
        screen.craft = object()
        self.assertEqual([name for name, _ in slot_layout.device_grids(screen)], ["grid", "output"])
        screen.template = object()
        self.assertEqual(sorted(name for name, _ in slot_layout.device_grids(screen)),
                         sorted(slot_layout.DEVICE_GRIDS))

    def test_a_plain_terminal_does_not_probe_device_paths(self):
        screen = FakeScreen({})
        screen.list_view = None
        self.assertIsNone(slot_layout.slot_at(screen, (0, 0)))
        probed = [path for path in screen.lookups
                  if any(grid[0] in path for grid in slot_layout.DEVICE_GRIDS.values())]
        self.assertEqual(probed, [])


if __name__ == "__main__":
    unittest.main()
