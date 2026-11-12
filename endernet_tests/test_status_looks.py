# coding=utf-8
"""设备的在线 / 离线外观（2026-11-13，作者画的离线材质）：方块 JSON 的状态与贴图切换、面板图集的
离线屏幕、服务端按设备状态写方块状态与面板外观读数。"""

import os
import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.cable_shapes import PANEL_PART_IDS
from endernet.common.channels.slot_display import FIRST_MODEL, OFFLINE_OFFSET, parse_update
from endernet.common.definitions import (
    NO_STATUS_LOOK,
    STATUS_CONFLICT,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    STATUS_STATE,
    DefinitionSet,
)
from endernet.common.identity import FACING_NAMES
from test_cable_blocks import scripts_module
from test_cable_panels import HOLDER, TINY, PanelWorld, ServerRig
from test_cable_states import key

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BP_BLOCKS = os.path.join(REPO, "endernet_bp", "blocks")
RP_ROOT = os.path.join(REPO, "endernet_rp")
TERM = "endernet:common_terminal_panel"
COMMON_PART = "endernet:common_terminal_part"


def block_json(device_id):
    import json
    with open(os.path.join(BP_BLOCKS, device_id.replace(":", "_") + ".json"), "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))["minecraft:block"]


class TestStatusLookAssets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definitions = DefinitionSet()
        cls.gen_blocks = scripts_module("gen_blocks")
        import json
        with open(os.path.join(RP_ROOT, "textures", "terrain_texture.json"), "rb") as handle:
            cls.terrain = json.loads(handle.read().decode("utf-8"))["texture_data"]

    def test_which_devices_have_a_look(self):
        for device_id in self.definitions.devices:
            values = self.definitions.status_values_of(device_id)
            if device_id in NO_STATUS_LOOK:
                self.assertEqual(values, (), device_id)
            elif self.definitions.is_controller(device_id):
                self.assertEqual(values, (STATUS_ONLINE, STATUS_OFFLINE, STATUS_CONFLICT))
            else:
                self.assertEqual(values, (STATUS_ONLINE, STATUS_OFFLINE), device_id)
        for device_id in list(self.definitions.cables) + list(self.definitions.parts):
            self.assertEqual(self.definitions.status_values_of(device_id), (), device_id)

    def test_block_json_switches_textures_by_status(self):
        for device_id in self.definitions.devices:
            block = block_json(device_id)
            values = self.definitions.status_values_of(device_id)
            states = block["description"].get("states", {})
            if not values:
                self.assertNotIn(STATUS_STATE, states, device_id)
                continue
            self.assertEqual(states[STATUS_STATE], list(values), device_id)
            online = block["components"]["minecraft:material_instances"]
            for value in values:
                if value == STATUS_ONLINE:
                    continue
                suffix = "_offline" if value == STATUS_OFFLINE else "_conflict"
                matches = [
                    permutation for permutation in block["permutations"]
                    if permutation["condition"] == "q.block_state('%s') == %d" % (STATUS_STATE, value)
                ]
                self.assertEqual(len(matches), 1, (device_id, value))
                instances = matches[0]["components"]["minecraft:material_instances"]
                self.assertEqual(sorted(instances), sorted(online), device_id)
                for face, instance in instances.items():
                    self.assertEqual(instance["texture"], online[face]["texture"] + suffix,
                                     (device_id, face))
                    self.assertIn(instance["texture"], self.terrain, (device_id, face))

    def test_texture_swapping_blocks_declare_a_geometry(self):
        """排列里换贴图只在声明了几何体的方块上生效：不声明时引擎按 blocks.json 的贴图画整块
        （作者真机反馈：六面同图的设备离线没换，只有声明了 full_block 的终端换了）。"""
        for device_id in self.definitions.devices:
            if self.definitions.status_values_of(device_id):
                self.assertIn("minecraft:geometry", block_json(device_id)["components"], device_id)

    def test_model_blocks_swap_their_model_texture(self):
        for device_id in ("endernet:storage_driver", "endernet:memory_slot"):
            block = block_json(device_id)
            offline = [permutation for permutation in block["permutations"]
                       if STATUS_STATE in permutation["condition"]]
            self.assertEqual(len(offline), 1, device_id)
            instance = offline[0]["components"]["minecraft:material_instances"]["*"]
            self.assertTrue(instance["texture"].endswith("_model_offline"), instance)
            self.assertEqual(instance["render_method"],
                             block["components"]["minecraft:material_instances"]["*"]["render_method"])

    def test_every_status_texture_file_exists(self):
        for device_id in self.definitions.devices:
            statuses = self.gen_blocks.statuses_of(device_id, self.definitions)
            for _key, path in self.gen_blocks.textures_of(device_id, statuses):
                self.assertTrue(os.path.isfile(os.path.join(RP_ROOT, *(path + ".png").split("/"))),
                                path)


class TestPanelAtlas(unittest.TestCase):
    def test_offline_screens_come_after_the_painted_regions(self):
        """作者画过的分区位置不变，离线屏幕排在后面，图集变高。"""
        generator = scripts_module("gen_cable_models")
        atlas = generator.panel_atlas(generator.PANELS)
        layout = [(region.kind, region.variant, region.u, region.v) for region in atlas.regions]
        self.assertEqual(layout[:11], [
            ("screen", "default", 0, 0), ("screen", "common", 12, 0),
            ("screen", "crafting", 24, 0), ("screen", "template", 36, 0),
            ("screen", "factoric", 48, 0), ("frame_back", None, 0, 12),
            ("frame_edge", None, 12, 12), ("frame_edge", None, 24, 12),
            ("back_face", None, 26, 12), ("back_edge", None, 34, 12),
            ("back_edge", None, 42, 12),
        ])
        offline = [generator.offline_variant(variant)
                   for variant in generator.terminal_screen_variants()]
        self.assertEqual([variant for _kind, variant, _u, _v in layout[11:16]], offline)
        # 隧道的屏幕（在线、离线）更晚加，排在最后
        self.assertEqual(layout[16:], [("screen", "tunnel", 48, 24), ("screen", "tunnel_offline", 0, 36)])
        self.assertEqual(atlas.size, (64, 64))

    def test_each_face_shows_exactly_one_screen_for_every_value(self):
        generator = scripts_module("gen_cable_models")
        rules = dict(generator.visibility_rules())
        values = [1] + [FIRST_MODEL + i for i in range(len(PANEL_PART_IDS))] + [9]
        values += [value + OFFLINE_OFFSET for value in values]
        for value in values:
            shown = [bone for bone, expression in rules.items()
                     if bone.startswith("panel_up_")
                     and eval(expression.replace("variable.p0", str(value))
                              .replace("&&", " and "))]
            self.assertEqual(len(shown), 1, (value, shown))
            self.assertEqual(shown[0].endswith("_offline"), value > OFFLINE_OFFSET, value)


class LooksWorld(PanelWorld):
    """面板假世界 + 任何方块的方块状态（整方块设备也记）。"""

    def update_block_states(self, node_key, changes, current=None):
        self.states.setdefault(node_key, {}).update(changes)
        self.state_writes.append((node_key, dict(changes)))
        return True


class TestStatusLooksThroughTheServer(unittest.TestCase):
    def setUp(self):
        self.rig = ServerRig(world=LooksWorld())

    def tearDown(self):
        self.rig.stop()

    def status_of(self, node_key):
        return self.rig.world.states.get(node_key, {}).get(STATUS_STATE)

    def status_writes(self, node_key):
        return [changes for at, changes in self.rig.world.state_writes
                if at == node_key and STATUS_STATE in changes]

    def test_online_devices_look_online(self):
        rig = self.rig
        rig.network()
        rig.place(key(2), TERM)
        rig.settle()
        self.assertEqual(self.status_of(key(2)), STATUS_ONLINE)
        self.assertEqual(self.status_of(key(0)), STATUS_ONLINE)
        self.assertEqual(self.status_of(key(0, y=63)), STATUS_ONLINE)

    def test_a_device_off_the_network_looks_offline(self):
        rig = self.rig
        rig.network()
        rig.place(key(9), TERM)
        rig.settle()
        self.assertEqual(self.status_of(key(9)), STATUS_OFFLINE)

    def test_two_controllers_turn_red_and_the_rest_offline(self):
        rig = self.rig
        rig.network()
        rig.place(key(2), TERM)
        rig.place(key(0, y=64, z=1), TINY)
        rig.settle(12)
        self.assertEqual(self.status_of(key(0, y=63)), STATUS_CONFLICT)
        self.assertEqual(self.status_of(key(2)), STATUS_OFFLINE)
        self.assertEqual(self.status_of(key(0)), STATUS_OFFLINE)

    def test_the_state_is_written_only_when_it_changes(self):
        rig = self.rig
        rig.network()
        rig.place(key(2), TERM)
        rig.settle()
        before = len(self.status_writes(key(2)))
        rig.settle(30)
        self.assertEqual(len(self.status_writes(key(2))), before)
        rig.remove(key(0, y=63), TINY)
        rig.settle(6)
        self.assertEqual(self.status_of(key(2)), STATUS_OFFLINE)
        self.assertEqual(len(self.status_writes(key(2))), before + 1)

    def test_a_saved_state_that_already_matches_is_not_rewritten(self):
        rig = self.rig
        rig.world.states[key(9)] = {STATUS_STATE: STATUS_OFFLINE}
        rig.network()
        rig.place(key(9), TERM)
        rig.settle()
        self.assertEqual(self.status_writes(key(9)), [])
        self.assertGreater(rig.server.status_looks.stats["already"], 0)

    def test_panels_show_their_offline_screen(self):
        rig = self.rig
        rig.network(parts={"up": COMMON_PART})
        rig.place(key(5), HOLDER, parts={"north": COMMON_PART})
        rig.settle()
        online = parse_update(rig.server.slot_displays.answer(key(1)))[2]
        offline = parse_update(rig.server.slot_displays.answer(key(5)))[2]
        value = FIRST_MODEL + PANEL_PART_IDS.index(COMMON_PART)
        self.assertEqual(online[FACING_NAMES.index("up")], value)
        self.assertEqual(offline[FACING_NAMES.index("north")], value + OFFLINE_OFFSET)
        # 控制器拆掉：整网离线，光缆上的面板跟着换离线屏幕
        rig.remove(key(0, y=63), TINY)
        rig.settle(6)
        online = parse_update(rig.server.slot_displays.answer(key(1)))[2]
        self.assertEqual(online[FACING_NAMES.index("up")], value + OFFLINE_OFFSET)

    def test_devices_without_a_look_are_left_alone(self):
        rig = self.rig
        rig.network()
        rig.settle()
        self.assertIsNone(self.status_of(key(0, y=65)), "创造能量源没有离线外观")
        self.assertIsNone(self.status_of(key(1)), "光缆没有")


if __name__ == "__main__":
    unittest.main()
