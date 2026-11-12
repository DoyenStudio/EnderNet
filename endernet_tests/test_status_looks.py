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
    PLACED_FACE_STATE,
    PLACED_HALF_STATE,
    STATUS_CONFLICT,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    STATUS_STATE,
    DefinitionSet,
    looks_unplaced,
)
from endernet.common.identity import FACING_NAMES, PartKey
from test_cable_blocks import scripts_module
from test_cable_panels import GLASS, HOLDER, TINY, PanelWorld, ServerRig
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


def holds(condition, states):
    """排列条件在这组方块状态下成不成立。只认生成器写得出的几种写法：`q.block_state('名')`、
    `==`、`&&`、`||`、`!(`。"""
    import re
    text = re.sub(r"q\.block_state\('([^']+)'\)", lambda match: repr(states[match.group(1)]), condition)
    text = text.replace("&&", " and ").replace("||", " or ").replace("!(", " not (")
    return eval(text)


def drawn_with(block, states):
    """这组方块状态下画的 `material_instances`：`components` 的，被成立的换贴图排列盖掉。"""
    instances = block["components"]["minecraft:material_instances"]
    for permutation in block.get("permutations", ()):
        swapped = permutation["components"].get("minecraft:material_instances")
        if swapped is not None and holds(permutation["condition"], states):
            instances = swapped
    return dict((face, instance["texture"]) for face, instance in instances.items())


#: 物品栏图标（没放过的默认组合）与几种玩家放置留下的痕迹（点顶面、点底面、点侧面上半 / 下半）
ICON = {PLACED_FACE_STATE: "down", PLACED_HALF_STATE: "bottom"}
PLACED = (
    {PLACED_FACE_STATE: "up", PLACED_HALF_STATE: "bottom"},
    {PLACED_FACE_STATE: "down", PLACED_HALF_STATE: "top"},
    {PLACED_FACE_STATE: "north", PLACED_HALF_STATE: "top"},
    {PLACED_FACE_STATE: "east", PLACED_HALF_STATE: "bottom"},
)


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
                self.assertEqual(values, (STATUS_OFFLINE, STATUS_ONLINE, STATUS_CONFLICT))
            else:
                self.assertEqual(values, (STATUS_OFFLINE, STATUS_ONLINE), device_id)
        for device_id in list(self.definitions.cables) + list(self.definitions.parts):
            self.assertEqual(self.definitions.status_values_of(device_id), (), device_id)

    def test_block_json_switches_textures_by_status(self):
        """默认值（列表第一个）是离线；`components` 是在线那一套，离线、冲突各一条排列。"""
        for device_id in self.definitions.devices:
            block = block_json(device_id)
            values = self.definitions.status_values_of(device_id)
            states = block["description"].get("states", {})
            if not values:
                self.assertNotIn(STATUS_STATE, states, device_id)
                continue
            self.assertEqual(states[STATUS_STATE], list(values), device_id)
            self.assertEqual(values[0], STATUS_OFFLINE, device_id)
            online = block["components"]["minecraft:material_instances"]
            swaps = [permutation for permutation in block["permutations"]
                     if "minecraft:material_instances" in permutation["components"]]
            self.assertEqual(len(swaps), len(values) - 1, device_id)
            for permutation in swaps:
                instances = permutation["components"]["minecraft:material_instances"]
                self.assertEqual(sorted(instances), sorted(online), device_id)
                for face, instance in instances.items():
                    self.assertIn(instance["texture"], self.terrain, (device_id, face))

    def test_the_icon_looks_online_and_a_placed_block_starts_offline(self):
        """放置痕迹：物品栏图标（状态 0、没放过的默认组合）画在线；玩家放下的方块状态 0 画离线；
        服务端写了 1 画在线、写了 2 画冲突，与痕迹无关。"""
        for device_id in self.definitions.devices:
            values = self.definitions.status_values_of(device_id)
            block = block_json(device_id)
            traits = block["description"].get("traits", {})
            if not values:
                self.assertNotIn("minecraft:placement_position", traits, device_id)
                continue
            self.assertEqual(traits["minecraft:placement_position"]["enabled_states"],
                             [PLACED_FACE_STATE, PLACED_HALF_STATE], device_id)
            online = drawn_with(block, dict(ICON, **{STATUS_STATE: STATUS_ONLINE}))
            for face, texture in online.items():
                self.assertFalse(texture.endswith(("_offline", "_conflict")), (device_id, face))

            def drawn(status, mark):
                states = dict(mark, **{STATUS_STATE: status})
                states["minecraft:cardinal_direction"] = "north"
                states["minecraft:facing_direction"] = "north"
                return drawn_with(block, states)

            self.assertEqual(drawn(STATUS_OFFLINE, ICON), online, device_id)
            offline = dict((face, texture + "_offline") for face, texture in online.items())
            conflict = dict((face, texture + "_conflict") for face, texture in online.items())
            for mark in PLACED:
                self.assertEqual(drawn(STATUS_OFFLINE, mark), offline, (device_id, mark))
            for mark in (ICON,) + PLACED:
                self.assertEqual(drawn(STATUS_ONLINE, mark), online, (device_id, mark))
                if STATUS_CONFLICT in values:
                    self.assertEqual(drawn(STATUS_CONFLICT, mark), conflict, (device_id, mark))

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


_DEFINITIONS = DefinitionSet()


class LooksWorld(PanelWorld):
    """面板假世界 + 任何方块的方块状态（整方块设备也记）。有离线外观的方块像引擎一样带着默认值
    （`status_values_of` 的第一个：离线），以及玩家站在地上放下时的放置痕迹（点的是下面那格的
    顶面、落在下半）；要模拟 `/setblock` 放出的，先在 `states` 里记上没放过的默认组合。"""

    def block_states_at(self, node_key):
        states = PanelWorld.block_states_at(self, node_key)
        values = _DEFINITIONS.status_values_of(self.blocks.get(node_key))
        if values:
            states = states or {}
            states.setdefault(STATUS_STATE, values[0])
            states.setdefault(PLACED_FACE_STATE, "up")
            states.setdefault(PLACED_HALF_STATE, "bottom")
        return states

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
        return (self.rig.world.block_states_at(node_key) or {}).get(STATUS_STATE)

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
        """刚放下就是默认的离线，一直离线：从头到尾一次都不写（真机反馈：放下先亮一下再灭）。"""
        rig = self.rig
        rig.network()
        rig.place(key(9), TERM)
        rig.settle()
        self.assertEqual(self.status_of(key(9)), STATUS_OFFLINE)
        self.assertEqual(self.status_writes(key(9)), [])

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
        rig.world.states[key(2)] = {STATUS_STATE: STATUS_ONLINE}
        rig.network()
        rig.place(key(2), TERM)
        rig.settle()
        self.assertEqual(self.status_writes(key(2)), [])
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

    def look_of(self, node_key):
        """这一格看上去是哪个外观：状态 0 还带着没放过的默认组合时画的是图标那一套（在线）。"""
        states = self.rig.world.block_states_at(node_key)
        if states[STATUS_STATE] == STATUS_OFFLINE and looks_unplaced(states):
            return STATUS_ONLINE
        return states[STATUS_STATE]

    def test_a_block_set_by_command_is_marked_placed_on_its_first_write(self):
        """`/setblock`、结构、旧存档放出的方块还是没放过的默认组合，看上去是在线：第一次写外观时
        连同放置痕迹一起改，离线的也要写。"""
        rig = self.rig
        for node_key in (key(2), key(9)):
            rig.world.states[node_key] = dict(ICON)
        rig.network()
        rig.place(key(2), TERM)
        rig.place(key(9), TERM)
        rig.settle()
        self.assertEqual(self.status_writes(key(2)),
                         [{STATUS_STATE: STATUS_ONLINE, PLACED_HALF_STATE: "top"}])
        self.assertEqual(self.status_writes(key(9)),
                         [{STATUS_STATE: STATUS_OFFLINE, PLACED_HALF_STATE: "top"}])
        self.assertEqual(self.look_of(key(2)), STATUS_ONLINE)
        self.assertEqual(self.look_of(key(9)), STATUS_OFFLINE)
        self.assertEqual(rig.server.status_looks.stats["unplaced"], 2)

    def test_a_panel_not_yet_evaluated_shows_offline(self):
        self.assertTrue(self.rig.server.status_looks.panel_offline(PartKey(key(1), "up")))

    def test_devices_without_a_look_are_left_alone(self):
        rig = self.rig
        rig.network()
        rig.settle()
        self.assertIsNone(self.status_of(key(0, y=65)), "创造能量源没有离线外观")
        self.assertIsNone(self.status_of(key(1)), "光缆没有")


class TestNoFlickerWhileTheNetworkRebuilds(unittest.TestCase):
    """接入一台设备整网都要重建，重建跨好几帧（真机反馈：整网闪一下离线）。主网东边一串玻璃光缆，
    尽头 (12,64,0) 一台终端、(1,64,0) 光缆朝上挂一块面板；拓扑每帧只推进 3 步，让重建拖上好几帧。"""

    def setUp(self):
        self.rig = rig = ServerRig(world=LooksWorld())
        rig.network(parts={"up": COMMON_PART})
        for x in range(2, 12):
            rig.place(key(x), GLASS)
        rig.place(key(12), TERM)
        rig.settle(10)
        rig.server.rebuild_steps_per_tick = 3
        self.watched = (key(12), key(0), key(0, y=63))
        for node_key in self.watched:
            self.assertEqual(self.status_of(node_key), STATUS_ONLINE, node_key)
        self.panel = PartKey(key(1), "up")
        self.assertFalse(rig.server.status_looks.panel_offline(self.panel))

    def tearDown(self):
        self.rig.stop()

    def status_of(self, node_key):
        return (self.rig.world.block_states_at(node_key) or {}).get(STATUS_STATE)

    def join_a_device(self, frames=16):
        """光缆 (5,64,0) 上面放一台物品总线，逐帧记下终端的结论是否待确认、面板是否离线。"""
        rig = self.rig
        server = rig.server
        start = len(rig.world.state_writes)
        rig.place(key(5, y=65), "endernet:bus_item_io")
        validity, panel_offline = [], []
        for _frame in range(frames):
            server.drive_once()
            validity.append(server.runtimes.device_status(server.registry.get(key(12)).ref).validity)
            panel_offline.append(server.status_looks.panel_offline(self.panel))
        self.assertEqual(server.tick_errors, [])
        writes = [(at, changes[STATUS_STATE]) for at, changes in rig.world.state_writes[start:]
                  if STATUS_STATE in changes]
        return writes, validity, panel_offline

    def test_the_rest_of_the_network_keeps_its_look(self):
        writes, validity, panel_offline = self.join_a_device()
        self.assertGreaterEqual(validity.count("pending"), 3, "重建确实拖了好几帧")
        self.assertEqual(validity[-1], "ok")
        for node_key in self.watched:
            self.assertEqual([value for at, value in writes if at == node_key], [], node_key)
        self.assertEqual(set(panel_offline), set([False]))
        # 新放的那台：默认离线，确认在线后只写一次
        self.assertEqual([value for at, value in writes if at == key(5, y=65)], [STATUS_ONLINE])

    def test_a_verdict_that_stays_pending_falls_back_to_offline(self):
        """沿用有上限：待确认超过 `hold_ticks` 帧还没结论，照实画离线。"""
        self.rig.server.status_looks.hold_ticks = 2
        writes, validity, _panel_offline = self.join_a_device()
        self.assertGreaterEqual(validity.count("pending"), 4)
        self.assertEqual([value for at, value in writes if at == key(12)],
                         [STATUS_OFFLINE, STATUS_ONLINE])


if __name__ == "__main__":
    unittest.main()
