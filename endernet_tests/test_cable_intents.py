# coding=utf-8
"""光缆的精确交互（阶段 C，`.claude/13` §6）：意图载荷、服务端校验与分派、切断与拆卸、
客户端组射线与拦截、服务端通道，以及经 `ServerBootstrap` 的一整条链路。

2026-11-10：客户端射线改为「相机原点 → 引擎给的命中点」（`click_ray`），不再换算触点；
扳手潜行点线芯 / 臂照 AE2 只拆光缆，挂着面板的原地换成面板底座。"""

import unittest

import testsupport

from endernet.client.channels.cable_intent import CableIntentClient
from endernet.client.interaction.press import HOLD_GAP_FRAMES, FrameClock, PressTracker
from endernet.client.interaction.ray import parse_ray, view_center
from endernet.common.cable_shapes import click_point, click_ray
from endernet.common.channels.cable_intent import (
    ACTION_CUT,
    ACTION_DISMANTLE,
    WRENCH_ID,
    action_for,
    build_event_classes,
    face_of_engine,
    intent_payload,
    parse_intent,
)
from endernet.common.identity import FACING_NAMES, face_mask, faces_of_mask
from endernet.server.adapters.channels.cable_intent import CableIntentPort
from endernet.server.devices.cable_intents import (
    REASON_BAD_REQUEST,
    REASON_BLOCK_MISMATCH,
    REASON_CABLE_REMOVED,
    REASON_CUT,
    REASON_JOINED,
    REASON_MISSED,
    REASON_NO_PLAYER,
    REASON_NOT_A_CABLE,
    REASON_NO_PANEL,
    REASON_NOT_READY,
    REASON_NOTHING_TO_CUT,
    REASON_PANEL_REMOVED,
    REASON_PANEL_TARGETED,
    REASON_SAME_TICK,
    REASON_REMOVE_FAILED,
    REASON_TOO_FAR,
    REASON_UNKNOWN_ACTION,
    REASON_WRONG_ITEM,
    CableIntentService,
    IntentResult,
)
from endernet.server.devices.catalog import CABLE_CUTS_KEY, CABLE_PARTS_KEY
from endernet.server.runtime.events import EventKind, NormalizedEvent
from endernet.server.runtime.lifecycle import ChangeKind
from test_cable_shapes import ray_toward
from test_cable_states import UNREADABLE, FakeCableWorld, Rig, key, sides

GLASS = "endernet:ender_glass_cable"
CABLE = "endernet:ender_cable"
HOLDER = "endernet:part_holder"
BB8 = "endernet:network_baseband_8"
TERM_PART = "endernet:common_terminal_part"
CRAFT_PART = "endernet:crafting_terminal_part"
PLAYER = "-4294967295"

CENTER = (0.5, 0.5, 0.5)


def px(value):
    return value / 16.0


def payload_for(node_key, block, action, target, face, item=WRENCH_ID, distance=3.0, clicked=""):
    """玩家从 `face` 那一侧朝格内点 `target` 打出的一条意图（射线换回世界坐标）。

    `clicked` 是事件带的被点击的面（`ClientItemUseOnEvent`）；默认 `""`（方块使用事件不带，
    服务端按外包盒求）。
    """
    local, direction = ray_toward(target, face, distance)
    origin = (node_key.x + local[0], node_key.y + local[1], node_key.z + local[2])
    return intent_payload(node_key.as_tuple(), block, item, action, origin, direction, clicked)


class FakePlayers(object):
    def __init__(self, dimension=0, position=(0.5, 64.0, 2.5), item=WRENCH_ID):
        self.dimension = dimension
        self.position = position
        self.item = item

    def dimension_of(self, player_id):
        return self.dimension

    def position_of(self, player_id):
        return self.position

    def held_item_of(self, player_id):
        return self.item


def within_eight(anchor, position):
    if position is None:
        return False
    return sum((a - b) * (a - b) for a, b in zip(anchor, position)) <= 64.0


class IntentRig(Rig):
    """`Rig` + 意图服务。`send` 默认每条先把逻辑帧推进一格（服务端同一玩家同一 tick 只处理
    一条）；`same_tick=True` 留在当前这一 tick。"""

    def __init__(self, **options):
        Rig.__init__(self, **options)
        self.players = FakePlayers()
        self.pushed = []
        self.service = CableIntentService(
            self.definitions,
            self.registry,
            self.catalog,
            self.keeper,
            self.world,
            self.players,
            self.lifecycle,
            reach=within_eight,
            push_event=self.pushed.append,
            clock=self.clock,
        )

    def send(self, payload, player_id=PLAYER, same_tick=False):
        if not same_tick:
            self.clock.tick += 1
        return self.service.handle(player_id, payload)

    def detached(self):
        return [(c.node_key, c.detail, c.device_id) for c in self.changes
                if c.kind == ChangeKind.PART_DETACHED]

    def cuts(self, node_key):
        return faces_of_mask(self.registry.get(node_key).config[CABLE_CUTS_KEY])


class TestPayload(unittest.TestCase):
    def test_round_trip(self):
        data = intent_payload((3, 64, -2), CABLE, WRENCH_ID, ACTION_CUT, (3.5, 66.1, 1.0), (0, -1, 0))
        intent = parse_intent(data)
        self.assertEqual(intent.pos, (3, 64, -2))
        self.assertEqual((intent.block, intent.item, intent.action), (CABLE, WRENCH_ID, ACTION_CUT))
        self.assertEqual(intent.direction, (0.0, -1.0, 0.0))
        for got, want in zip(intent.local_origin(), (0.5, 2.1, 3.0)):
            self.assertAlmostEqual(got, want)

    def test_parse_is_strict(self):
        good = intent_payload((0, 64, 0), CABLE, "", ACTION_CUT, (0.5, 66, 0.5), (0, -1, 0))
        broken = [
            dict(good, player="someone"),
            dict((k, v) for k, v in good.items() if k != "item"),
            dict(good, pos=[0, True, 0]),
            dict(good, pos=[0.5, 64, 0]),
            dict(good, pos=[0, 64]),
            dict(good, origin=[float("nan"), 0, 0]),
            dict(good, direction=[0, 0, 0]),
            dict(good, block=""),
            dict(good, item=3),
            dict((k, v) for k, v in good.items() if k != "face"),
            dict(good, face="middle"),
            dict(good, face=1),
            "not a dict",
        ]
        for data in broken:
            with self.assertRaises((KeyError, TypeError, ValueError)):
                parse_intent(data)

    def test_the_clicked_face_travels_as_a_face_name(self):
        data = intent_payload((0, 64, 0), CABLE, "", ACTION_CUT, (0.5, 66, 0.5), (0, -1, 0), "up")
        self.assertEqual(parse_intent(data).face, "up")
        self.assertEqual(parse_intent(dict(data, face="")).face, "")

    def test_action_for(self):
        self.assertEqual(action_for(WRENCH_ID, False), ACTION_CUT)
        self.assertEqual(action_for(WRENCH_ID, True), ACTION_DISMANTLE)
        self.assertEqual(action_for(WRENCH_ID, True, HOLDER), ACTION_DISMANTLE, "底座也收扳手")
        self.assertIsNone(action_for(WRENCH_ID, False, "minecraft:stone"), "扳手只对光缆与底座")
        self.assertIsNone(action_for("", False))
        self.assertIsNone(action_for(TERM_PART, False), "不给被点的光缆就只认扳手（阶段 D 的分支见 test_cable_panels）")
        self.assertIsNone(action_for("minecraft:stone", True))

    def test_wrench_is_the_registered_tool(self):
        from endernet.common.definitions import DefinitionSet

        definitions = DefinitionSet()
        self.assertTrue(definitions.has_tool(WRENCH_ID))
        self.assertIn(WRENCH_ID, definitions.item_keys())

    def test_identity_comes_from_the_engine_only(self):
        (request,) = build_event_classes(testsupport.FakeEventsModule())
        event = request.unmarshal({"__id__": PLAYER, "pos": [0, 0, 0]})
        self.assertEqual(event.player_id, PLAYER)
        self.assertEqual(event.payload, {"pos": [0, 0, 0]})


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.rig = IntentRig()
        self.rig.place(key(0), CABLE)
        self.rig.place(key(1), CABLE)
        self.rig.step(3)

    def arm_payload(self, **overrides):
        options = dict(block=CABLE, action=ACTION_CUT)
        options.update(overrides)
        return payload_for(key(0), options["block"], options["action"], (px(14), 0.5, 0.5), "south")

    def test_bad_requests(self):
        self.assertEqual(self.rig.send({"pos": [0, 64, 0]}).reason, REASON_BAD_REQUEST)
        self.assertEqual(self.rig.send(self.arm_payload(), player_id="").reason, REASON_BAD_REQUEST)

    def test_unknown_action(self):
        self.assertEqual(self.rig.send(self.arm_payload(action="paint")).reason, REASON_UNKNOWN_ACTION)

    def test_one_intent_per_player_per_server_tick(self):
        """没有秒级冷却：只防同一 tick 里刷进来的第二条（静默丢弃）。"""
        rig = self.rig
        payload = payload_for(key(0), CABLE, ACTION_CUT, (px(14), 0.5, 0.5), "south")
        self.assertEqual(rig.send(payload).reason, REASON_CUT)
        dropped = rig.send(payload, same_tick=True)
        self.assertEqual((dropped.reason, dropped.message), (REASON_SAME_TICK, ""))
        self.assertEqual(rig.cuts(key(0)), frozenset(["east"]), "第二条没有处理")
        self.assertEqual(rig.send(payload, player_id="other", same_tick=True).reason,
                         REASON_JOINED, "按玩家分开")
        self.assertEqual(rig.send(payload).reason, REASON_CUT, "下一 tick 立即生效，没有冷却")

    def test_player_without_a_dimension(self):
        self.rig.players.dimension = None
        self.assertEqual(self.rig.send(self.arm_payload()).reason, REASON_NO_PLAYER)

    def test_not_a_cable(self):
        self.rig.place(key(5), BB8)
        payload = payload_for(key(5), BB8, ACTION_CUT, CENTER, "south")
        self.assertEqual(self.rig.send(payload).reason, REASON_NOT_A_CABLE)
        payload = payload_for(key(9), CABLE, ACTION_CUT, CENTER, "south")
        self.assertEqual(self.rig.send(payload).reason, REASON_NOT_A_CABLE)

    def test_block_mismatch(self):
        self.assertEqual(self.rig.send(self.arm_payload(block=GLASS)).reason, REASON_BLOCK_MISMATCH)

    def test_cable_still_waiting_for_its_entity(self):
        self.rig.world.put_cable(key(0, z=5))
        self.rig.world.entities[key(0, z=5)] = UNREADABLE
        self.rig.lifecycle.handle(NormalizedEvent.place(key(0, z=5), CABLE))
        payload = payload_for(key(0, z=5), CABLE, ACTION_CUT, CENTER, "south")
        self.assertEqual(self.rig.send(payload).reason, REASON_NOT_READY)

    def test_too_far(self):
        self.rig.players.position = (40.0, 64.0, 0.0)
        result = self.rig.send(self.arm_payload())
        self.assertEqual(result.reason, REASON_TOO_FAR)
        self.rig.players.position = None
        self.assertEqual(self.rig.send(self.arm_payload()).reason, REASON_TOO_FAR, "查不到位置按拒绝")

    def test_wrong_item(self):
        self.rig.players.item = "minecraft:stick"
        result = self.rig.send(self.arm_payload())
        self.assertEqual(result.reason, REASON_WRONG_ITEM)
        self.assertIn("网络扳手", result.message)
        self.assertEqual(self.rig.cuts(key(0)), frozenset(), "什么都没改")

    def test_ray_that_misses_every_part(self):
        # 东臂在、北边没有：从上面射向东北角
        payload = payload_for(key(0), CABLE, ACTION_CUT, (px(14), 0.5, px(2)), "up")
        self.assertEqual(self.rig.send(payload).reason, REASON_MISSED)

    def test_results_are_counted(self):
        self.rig.send({"pos": 1})
        self.rig.send(self.arm_payload(block=GLASS))
        report = self.rig.service.diagnostics()
        self.assertEqual(report["results"][REASON_BAD_REQUEST], 1)
        self.assertEqual(report["results"][REASON_BLOCK_MISMATCH], 1)
        self.assertEqual(report["actions"], [ACTION_CUT, ACTION_DISMANTLE])


class TestCut(unittest.TestCase):
    def setUp(self):
        self.rig = IntentRig()
        self.rig.place(key(0), CABLE)
        self.rig.place(key(1), GLASS)
        self.rig.step(3)

    def test_cutting_an_arm_is_written_on_both_cables(self):
        rig = self.rig
        result = rig.send(payload_for(key(0), CABLE, ACTION_CUT, (px(14), 0.5, 0.5), "south"))
        self.assertTrue(result.ok)
        self.assertEqual(result.reason, REASON_CUT)
        self.assertIn("东", result.message)
        self.assertEqual(rig.cuts(key(0)), frozenset(["east"]))
        self.assertEqual(rig.cuts(key(1)), frozenset(["west"]), "对面也是光缆：对称写")
        # 存档（方块实体）与记录一致
        self.assertEqual(rig.world.entities[key(0)][0], face_mask(["east"]))
        self.assertEqual(rig.world.entities[key(1)][0], face_mask(["west"]))
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), sides())
        self.assertEqual(rig.world.sides(key(1)), sides())
        self.assertNotIn(key(1), rig.topology.link_state(key(0)).linked)

    def test_cutting_again_joins_through_the_core(self):
        rig = self.rig
        rig.send(payload_for(key(0), CABLE, ACTION_CUT, (px(14), 0.5, 0.5), "south"))
        rig.step(3)
        # 臂没了：从东边看过去点到的是线芯的东面
        result = rig.send(payload_for(key(0), CABLE, ACTION_CUT, CENTER, "east"))
        self.assertEqual(result.reason, REASON_JOINED)
        self.assertEqual(rig.cuts(key(0)), frozenset())
        self.assertEqual(rig.cuts(key(1)), frozenset())
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), sides(east=1))
        self.assertEqual(rig.world.sides(key(1)), sides(west=1))

    def test_core_hit_picks_the_face_the_ray_comes_from(self):
        rig = self.rig
        rig.place(key(0, z=-1), BB8)
        rig.step(3)
        result = rig.send(payload_for(key(0), CABLE, ACTION_CUT, CENTER, "up"))
        self.assertEqual(result.reason, REASON_NOTHING_TO_CUT, "上面什么都没有")
        # 北臂伸到 z = 0：从北边正对线芯看，先点到的是北臂
        result = rig.send(payload_for(key(0), CABLE, ACTION_CUT, CENTER, "north"))
        self.assertEqual(result.reason, REASON_CUT)
        self.assertEqual(rig.cuts(key(0)), frozenset(["north"]))
        self.assertEqual(rig.registry.get(key(0, z=-1)).config, {}, "基带没有切断标记")

    def test_nothing_to_cut_leaves_no_hidden_flag(self):
        rig = self.rig
        result = rig.send(payload_for(key(0), CABLE, ACTION_CUT, CENTER, "west"))
        self.assertEqual(result.reason, REASON_NOTHING_TO_CUT)
        self.assertEqual(rig.cuts(key(0)), frozenset())
        self.assertEqual(rig.world.entity_writes, [])

    def test_panel_is_left_alone(self):
        rig = IntentRig()
        rig.place(key(0), GLASS, parts={"east": TERM_PART})
        rig.step(2)
        result = rig.send(payload_for(key(0), GLASS, ACTION_CUT, CENTER, "east"))
        self.assertEqual(result.reason, REASON_PANEL_TARGETED)
        self.assertEqual(rig.world.entity_writes, [])

    def test_neighbor_that_is_not_ready_is_not_written(self):
        rig = IntentRig()
        rig.place(key(0), CABLE)
        rig.world.put_cable(key(1))
        rig.world.entities[key(1)] = UNREADABLE
        rig.lifecycle.handle(NormalizedEvent.place(key(1), CABLE))
        rig.world.states[key(0)]["endernet:side_east"] = 1
        result = rig.send(payload_for(key(0), CABLE, ACTION_CUT, CENTER, "east"))
        self.assertEqual(result.reason, REASON_CUT)
        self.assertEqual([at for at, _c, _p in rig.world.entity_writes], [key(0)])


class TestDismantle(unittest.TestCase):
    def setUp(self):
        self.rig = IntentRig()
        self.rig.place(key(0), GLASS, parts={"east": TERM_PART, "north": CRAFT_PART})
        self.rig.place(key(0, y=65), CABLE)
        self.rig.step(3)
        self.rig.players.position = (0.5, 64.0, 1.5)

    def test_states_show_the_panels(self):
        self.assertEqual(self.rig.world.sides(key(0)), sides(east=2, north=2, up=1))

    def test_removing_one_panel(self):
        rig = self.rig
        result = rig.send(payload_for(key(0), GLASS, ACTION_DISMANTLE, CENTER, "east"))
        self.assertEqual(result.reason, REASON_PANEL_REMOVED)
        self.assertIn("通用终端面板", result.message)
        self.assertEqual(rig.registry.get(key(0)).config[CABLE_PARTS_KEY], {"north": CRAFT_PART})
        self.assertEqual(rig.world.entities[key(0)][1], {"north": CRAFT_PART})
        self.assertEqual(rig.detached(), [(key(0), "east", TERM_PART)], "面板离开的钩子")
        self.assertEqual(rig.world.given, [(PLAYER, [TERM_PART])])
        self.assertEqual(rig.world.removed, [], "只拆面板，光缆还在")
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), sides(north=2, up=1))

    def test_panel_shown_but_not_recorded_is_not_there(self):
        """外观还画着面板、记录里却没有（例如别处刚拆掉、外观还没刷新）：不给物品。"""
        rig = IntentRig()
        rig.place(key(0), GLASS)
        rig.world.states[key(0)]["endernet:side_east"] = 2
        result = rig.send(payload_for(key(0), GLASS, ACTION_DISMANTLE, CENTER, "east"))
        self.assertEqual(result.reason, REASON_NO_PANEL)
        self.assertEqual(rig.world.given, [])
        self.assertEqual(rig.detached(), [])

    def test_full_inventory_drops_at_the_feet(self):
        rig = self.rig
        rig.world.inventory_space = 0
        rig.send(payload_for(key(0), GLASS, ACTION_DISMANTLE, CENTER, "east"))
        self.assertEqual(rig.world.dropped, [((0, 0, 64, 1), [(TERM_PART, 1)])])

    def test_the_core_takes_only_the_cable_and_leaves_a_holder(self):
        """照 AE2：潜行用扳手点线芯只拆光缆本身，面板留下，这一格原地换成面板底座。"""
        rig = self.rig
        before = rig.registry.get(key(0)).ref
        result = rig.send(payload_for(key(0), GLASS, ACTION_DISMANTLE, CENTER, "west"))
        self.assertEqual(result.reason, REASON_CABLE_REMOVED)
        self.assertEqual(result.message, "已拆下末影玻璃光缆（面板留在原处）")
        record = rig.registry.get(key(0))
        self.assertEqual((record.device_id, record.ref), (HOLDER, before), "同一个实例、同一代次")
        self.assertEqual(record.config[CABLE_PARTS_KEY], {"east": TERM_PART, "north": CRAFT_PART})
        self.assertEqual(rig.world.swaps, [
            (key(0), HOLDER, 0, {"east": TERM_PART, "north": CRAFT_PART})])
        self.assertEqual(rig.world.removed, [], "格子没有拆成空气")
        self.assertEqual(rig.detached(), [], "面板没有离开")
        self.assertEqual(rig.world.given, [(PLAYER, [GLASS])])
        self.assertEqual(rig.pushed, [])
        self.assertEqual([c.kind for c in rig.changes if c.kind == ChangeKind.HOST_CONVERTED],
                         [ChangeKind.HOST_CONVERTED])
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), sides(east=2, north=2), "底座没有连接臂")
        self.assertEqual(rig.world.sides(key(0, y=65)), sides(), "上面的光缆收回朝它的臂")

    def test_an_arm_hit_also_takes_only_the_cable(self):
        rig = self.rig
        result = rig.send(payload_for(key(0), GLASS, ACTION_DISMANTLE, (0.5, px(14), 0.5), "south"))
        self.assertEqual(result.reason, REASON_CABLE_REMOVED)
        self.assertEqual(rig.registry.get(key(0)).device_id, HOLDER)

    def test_a_cable_without_panels_is_removed_whole(self):
        rig = self.rig
        result = rig.send(payload_for(key(0, y=65), CABLE, ACTION_DISMANTLE, CENTER, "west"))
        self.assertEqual((result.reason, result.message), (REASON_CABLE_REMOVED, "已拆下末影光缆"))
        self.assertEqual(rig.world.removed, [key(0, y=65)])
        self.assertEqual(rig.world.given, [(PLAYER, [CABLE])])
        self.assertEqual([(e.kind, e.node_key, e.data["device_id"]) for e in rig.pushed],
                         [(EventKind.REMOVE, key(0, y=65), CABLE)])

    def test_removal_runs_once_even_with_the_engine_event(self):
        rig = self.rig
        rig.send(payload_for(key(0, y=65), CABLE, ACTION_DISMANTLE, CENTER, "west"))
        # 补进来的那条 + 引擎自己的那条，到生命周期只处理一次
        for event in rig.pushed + [NormalizedEvent.remove(key(0, y=65), CABLE, source="block_remove")]:
            rig.lifecycle.handle(event)
        removed = [c for c in rig.changes if c.kind == ChangeKind.REMOVED]
        self.assertEqual([c.node_key for c in removed], [key(0, y=65)])
        self.assertEqual(rig.lifecycle.stats.ignored_removals, 1)
        self.assertIsNone(rig.registry.get(key(0, y=65)))
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), sides(east=2, north=2))

    def test_failed_removal_changes_nothing(self):
        rig = self.rig
        rig.world.remove_ok = False
        result = rig.send(payload_for(key(0, y=65), CABLE, ACTION_DISMANTLE, CENTER, "west"))
        self.assertEqual(result.reason, REASON_REMOVE_FAILED)
        self.assertIsNotNone(rig.registry.get(key(0, y=65)))
        self.assertEqual((rig.world.given, rig.pushed), ([], []))

    def test_a_failed_swap_keeps_the_cable_and_its_panels(self):
        rig = self.rig
        rig.world.swap_ok = False
        result = rig.send(payload_for(key(0), GLASS, ACTION_DISMANTLE, CENTER, "west"))
        self.assertEqual(result.reason, REASON_REMOVE_FAILED)
        record = rig.registry.get(key(0))
        self.assertEqual(record.device_id, GLASS)
        self.assertEqual(record.config[CABLE_PARTS_KEY], {"east": TERM_PART, "north": CRAFT_PART})
        self.assertEqual((rig.world.given, rig.detached()), ([], []))

    def test_the_last_panel_of_a_holder_takes_the_holder_with_it(self):
        rig = IntentRig()
        rig.place(key(0), HOLDER, parts={"east": TERM_PART, "north": CRAFT_PART})
        rig.step(3)
        rig.players.position = (0.5, 64.0, 1.5)
        result = rig.send(payload_for(key(0), HOLDER, ACTION_DISMANTLE, CENTER, "east"))
        self.assertEqual(result.reason, REASON_PANEL_REMOVED)
        self.assertEqual(rig.world.removed, [], "还剩一块，底座留着")
        rig.step(2)
        result = rig.send(payload_for(key(0), HOLDER, ACTION_DISMANTLE, CENTER, "north"))
        self.assertEqual(result.reason, REASON_PANEL_REMOVED)
        self.assertEqual(rig.world.removed, [key(0)])
        self.assertEqual(rig.world.given, [(PLAYER, [TERM_PART]), (PLAYER, [CRAFT_PART])],
                         "底座自己没有物品")
        self.assertEqual(rig.detached(), [(key(0), "east", TERM_PART), (key(0), "north", CRAFT_PART)])
        for event in rig.pushed + [NormalizedEvent.remove(key(0), HOLDER, source="block_remove")]:
            rig.lifecycle.handle(event)
        self.assertIsNone(rig.registry.get(key(0)))
        self.assertEqual(len(rig.detached()), 2, "面板先摘掉了，拆除不再发第二遍")

    def test_a_holder_that_cannot_be_removed_keeps_its_last_panel(self):
        rig = IntentRig()
        rig.place(key(0), HOLDER, parts={"east": TERM_PART})
        rig.step(3)
        rig.players.position = (0.5, 64.0, 1.5)
        rig.world.remove_ok = False
        result = rig.send(payload_for(key(0), HOLDER, ACTION_DISMANTLE, CENTER, "east"))
        self.assertEqual(result.reason, REASON_REMOVE_FAILED)
        self.assertEqual(rig.registry.get(key(0)).config[CABLE_PARTS_KEY], {"east": TERM_PART})
        self.assertEqual((rig.world.given, rig.detached()), ([], []))


class TestDispatchTable(unittest.TestCase):
    def test_registering_a_new_action(self):
        rig = IntentRig()
        rig.place(key(0), GLASS)
        rig.step(2)
        seen = []

        def handler(context):
            seen.append((context.hit.part, context.hit.face, context.record.node_key, context.item_id))
            return IntentResult(True, "probed", "ok")

        rig.service.register("probe", handler, lambda item_id: item_id == TERM_PART, "要手持面板")
        rig.players.item = TERM_PART
        result = rig.send(payload_for(key(0), GLASS, "probe", CENTER, "down", item=TERM_PART))
        self.assertEqual(result.reason, "probed")
        self.assertEqual(seen, [("core", "down", key(0), TERM_PART)])
        rig.players.item = WRENCH_ID
        result = rig.send(payload_for(key(0), GLASS, "probe", CENTER, "down"))
        self.assertEqual((result.reason, result.message), (REASON_WRONG_ITEM, "要手持面板"))
        self.assertIn("probe", rig.service.actions())


# -- 客户端 --------------------------------------------------------------------


class FakeBus(object):
    def __init__(self):
        self.listeners = []

    def ListenEvent(self, event_cls, priority):
        def wrapper(handler):
            self.listeners.append((event_cls, handler))
            return handler
        return wrapper

    def RemoveEventListener(self, event_cls, handler, priority):
        self.listeners.remove((event_cls, handler))


class FakeClientEvents(object):
    """假的 `tooldelta.events`：C2S 的 `send()` 只记账。"""

    def __init__(self):
        self.sent = []
        sent = self.sent

        class CustomC2SEvent(object):
            def send(self):
                sent.append(self.marshal())

        class ClientEvent(object):
            pass

        self.CustomC2SEvent = CustomC2SEvent
        self.ClientEvent = ClientEvent


class FakeView(object):
    """客户端引擎读数的替身：相机原点、主手物品、潜行、挥手（离线没有 `Swing`，数调用次数）。"""

    def __init__(self):
        self.origin = (0.5, 66.0, 0.5)
        self.origin_error = None
        self.origin_calls = 0
        self.item = WRENCH_ID
        self.sneak = False
        self.sneak_reads = 0
        self.swings = 0
        self.swing_error = None

    def camera_origin(self):
        if self.origin_error is not None:
            raise self.origin_error
        self.origin_calls += 1
        return self.origin

    def held_item_id(self):
        return self.item

    def sneaking(self):
        self.sneak_reads += 1
        return self.sneak

    def swing(self):
        if self.swing_error is not None:
            raise self.swing_error
        self.swings += 1
        return True


class FakeUse(object):
    """`ClientBlockUseEvent` / `ClientItemUseOnEvent` 的替身。`click` 是格内命中点（0～1），
    `face` 是引擎 `Facing` 值（方块使用事件不带，None）。"""

    def __init__(self, block=CABLE, item=None, click=(0.5, 0.625, 0.5), face=None, at=(0, 64, 0)):
        self.blockName = block
        self.x, self.y, self.z = at
        self.clickX, self.clickY, self.clickZ = click
        self.face = face
        self.item = item
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeItem(object):
    def __init__(self, name):
        self.newItemName = name


def assert_close(case, got, want, places=7):
    case.assertEqual(len(got), len(want))
    for a, b in zip(got, want):
        case.assertAlmostEqual(a, b, places=places)


class TestRay(unittest.TestCase):
    """客户端组射线的纯函数（2026-11-10）：相机原点 → 引擎给的命中点。"""

    def test_view_center_is_only_used_to_ask_for_the_origin(self):
        self.assertEqual(view_center((2000.0, 1000.0, 12.0, 3.0)), (1000.0, 500.0))
        self.assertEqual(view_center((1794, 828)), (897.0, 414.0))

    def test_parse_ray(self):
        self.assertEqual(parse_ray({"origin": (1, 2, 3), "direction": [0, 1, 0]}),
                         ((1.0, 2.0, 3.0), (0.0, 1.0, 0.0)))
        for bad in (None, {}, {"origin": (1, 2), "direction": (0, 1, 0)},
                    {"origin": ("a", 2, 3), "direction": (0, 1, 0)}):
            self.assertIsNone(parse_ray(bad))

    def test_click_point_is_block_position_plus_the_in_block_ratio(self):
        self.assertEqual(click_point((3, 64, -2), (0.25, 1.0, 0.5)), (3.25, 65.0, -1.5))

    def test_the_ray_runs_from_the_camera_through_the_hit_point(self):
        origin, direction = click_ray((0.5, 66.0, 0.5), (0, 64, 0), (0.5, 0.625, 0.5))
        self.assertEqual(origin, (0.5, 66.0, 0.5))
        assert_close(self, direction, (0.0, -1.0, 0.0))
        # 斜着看：方向是单位向量，沿它走「两点距离」正好落在命中点上
        origin, direction = click_ray((4.0, 65.7, -1.2), (0, 64, 0), (1.0, 0.5, 0.375))
        length = sum(c * c for c in direction) ** 0.5
        self.assertAlmostEqual(length, 1.0)
        distance = sum((h - o) ** 2 for h, o in zip((1.0, 64.5, 0.375), origin)) ** 0.5
        assert_close(self, tuple(o + d * distance for o, d in zip(origin, direction)),
                     (1.0, 64.5, 0.375))

    def test_degenerate_or_broken_readings_give_no_ray(self):
        self.assertIsNone(click_ray((0.5, 64.625, 0.5), (0, 64, 0), (0.5, 0.625, 0.5)), "两点重合")
        self.assertIsNone(click_ray((float("nan"), 66.0, 0.5), (0, 64, 0), (0.5, 0.5, 0.5)))
        self.assertIsNone(click_ray((0.5, 66.0, 0.5), (0, 64, 0), (None, 0.5, 0.5)))
        self.assertIsNone(click_ray((0.5, 66.0), (0, 64, 0), (0.5, 0.5, 0.5)))

    def test_engine_faces_map_to_face_names(self):
        self.assertEqual([face_of_engine(value) for value in range(6)],
                         ["down", "up", "north", "south", "west", "east"])
        for bad in (None, -1, 6, True, "up", 1.0):
            self.assertEqual(face_of_engine(bad), "", bad)

    def test_a_third_person_or_touch_ray_picks_the_part_under_the_hit_point(self):
        """不管相机在哪（第三人称、触屏点在屏幕边上），射线都穿过引擎给的命中点：落在东臂端面
        上的命中点，服务端求交就是东臂。"""
        from endernet.common.cable_shapes import PART_ARM, core_px_of, pick

        sides_now = {"east": 1}
        for camera in ((3.0, 65.2, 0.5), (2.4, 64.1, 2.9), (6.0, 70.0, -4.0)):
            origin, direction = click_ray(camera, (0, 64, 0), (1.0, 0.5, 0.5))
            local = tuple(o - p for o, p in zip(origin, (0, 64, 0)))
            hit = pick(core_px_of(CABLE), sides_now, local, direction)
            self.assertEqual((hit.part, hit.face), (PART_ARM, "east"), camera)


class TestClient(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.events = FakeClientEvents()
        self.view = FakeView()
        self.client = CableIntentClient(
            self.bus, self.events, "block_use", "item_use_on", self.view, [CABLE, GLASS]
        )
        self.client.start()

    def test_start_and_stop(self):
        self.assertEqual(len(self.bus.listeners), 2, "不再监听点按事件")
        self.client.stop()
        self.assertEqual(self.bus.listeners, [])

    def test_other_blocks_are_left_alone(self):
        event = FakeUse(block="minecraft:stone")
        self.assertFalse(self.client.on_block_use(event))
        self.assertFalse(event.cancelled)

    def test_other_items_keep_the_vanilla_interaction(self):
        """空手（打开面板）与手持面板物品（挂面板）阶段 D 起会被拦，见 `test_cable_panels`。"""
        for item in ("minecraft:stone", "minecraft:oak_planks"):
            self.view.item = item
            event = FakeUse()
            self.assertFalse(self.client.on_block_use(event))
            self.assertFalse(event.cancelled, item)
        self.assertEqual(self.events.sent, [])

    def test_item_events_that_cannot_act_do_not_ask_the_engine(self):
        """按住右键时每 tick 一条物品事件：选不出动作的，连潜行都不问。"""
        for _ in range(3):
            self.assertFalse(self.client.on_item_use_on(
                FakeUse(block="minecraft:stone", item=FakeItem("minecraft:dirt"), face=1)))
        self.assertFalse(self.client.on_item_use_on(
            FakeUse(block="minecraft:stone", item=FakeItem(WRENCH_ID), face=1)), "扳手只对光缆")
        self.assertEqual(self.view.sneak_reads, 0)

    def test_wrench_click_is_intercepted_and_sent(self):
        event = FakeUse()
        self.assertTrue(self.client.on_block_use(event))
        self.assertTrue(event.cancelled)
        self.assertEqual(self.view.origin_calls, 1)
        (sent,) = self.events.sent
        intent = parse_intent(sent)
        self.assertEqual((intent.pos, intent.block, intent.action), ((0, 64, 0), CABLE, ACTION_CUT))
        self.assertEqual(intent.origin, (0.5, 66.0, 0.5), "起点是相机原点")
        assert_close(self, intent.direction, (0.0, -1.0, 0.0))
        self.assertEqual(intent.face, "", "方块使用事件不带面")

    def test_the_ray_ends_at_the_engine_hit_point(self):
        self.view.origin = (2.0, 66.0, 3.0)
        self.client.on_block_use(FakeUse(click=(1.0, 0.5, 0.5)))
        intent = parse_intent(self.events.sent[0])
        _origin, want = click_ray((2.0, 66.0, 3.0), (0, 64, 0), (1.0, 0.5, 0.5))
        assert_close(self, intent.direction, want)

    def test_sneaking_dismantles_through_item_use_on(self):
        self.view.sneak = True
        event = FakeUse(item=FakeItem(WRENCH_ID), face=1)
        self.assertTrue(self.client.on_item_use_on(event))
        intent = parse_intent(self.events.sent[0])
        self.assertEqual((intent.action, intent.face), (ACTION_DISMANTLE, "up"))

    def test_one_click_two_events_one_intent(self):
        first, second = FakeUse(), FakeUse(item=FakeItem(WRENCH_ID))
        self.client.on_block_use(first)
        self.client.on_item_use_on(second)
        self.assertTrue(first.cancelled and second.cancelled, "两条都要拦")
        self.assertEqual(len(self.events.sent), 1)
        self.assertEqual(self.client.stats["held"], 1)

    def test_missing_camera_interface_still_blocks_the_click(self):
        self.view.origin_error = AttributeError("GetWorldRayFromScreenPoint")
        event = FakeUse()
        self.assertTrue(self.client.on_block_use(event))
        self.assertTrue(event.cancelled)
        self.assertEqual(self.events.sent, [])
        self.assertEqual(self.client.stats["ray_failures"], 1)

    def test_an_origin_on_the_hit_point_is_a_ray_failure(self):
        self.view.origin = (0.5, 64.625, 0.5)
        self.assertTrue(self.client.on_block_use(FakeUse()))
        self.assertEqual((self.events.sent, self.client.stats["ray_failures"]), ([], 1))


#: 游戏刻 → 客户端帧（`OnScriptTickClient` 每秒 30 帧，游戏刻每秒 20 刻）
def frame_of(game_tick):
    return (game_tick * 30) // 20


class TestPressByPress(unittest.TestCase):
    """按一次算一次，没有冷却（作者 2026-11-10）：相邻事件 ≤ 1 个游戏刻且目标不变算同一次按下。"""

    def setUp(self):
        self.events = FakeClientEvents()
        self.view = FakeView()
        self.clock = FrameClock()
        self.client = CableIntentClient(
            FakeBus(), self.events, "block_use", "item_use_on", self.view, [CABLE, GLASS],
            clock=self.clock,
        )
        self.client.start()

    def at(self, game_tick, event=None):
        self.clock.frame = frame_of(game_tick)
        event = event if event is not None else FakeUse()
        self.assertTrue(self.client.on_block_use(event))
        self.assertTrue(event.cancelled, "同一次按下的后续事件照样拦掉原版交互")
        return event

    def test_the_hold_window_is_one_game_tick(self):
        self.assertEqual(HOLD_GAP_FRAMES, 2)
        self.assertEqual([frame_of(t + 1) - frame_of(t) for t in range(4)], [1, 2, 1, 2])
        self.assertEqual(frame_of(2) - frame_of(0), 3, "隔 2 个游戏刻 > 2 帧")

    def test_two_clicks_two_game_ticks_apart_both_count(self):
        self.at(0)
        self.at(2)
        self.assertEqual(len(self.events.sent), 2)

    def test_holding_for_ten_game_ticks_counts_once(self):
        for tick in range(10):
            self.at(tick)
        self.assertEqual(len(self.events.sent), 1)
        self.assertEqual(self.client.stats["held"], 9)
        self.at(12)
        self.assertEqual(len(self.events.sent), 2, "松手再按是新的一次")

    def test_two_events_in_the_same_tick_send_once(self):
        self.at(5)
        self.at(5, FakeUse(item=FakeItem(WRENCH_ID)))
        self.assertEqual(len(self.events.sent), 1)

    def test_changing_the_target_while_holding_does_not_fire_again(self):
        """按住时换方块、换潜行、换手持都不再触发：否则按住对着墙会一路往外长面板底座、
        按住潜行扳手会一格接一格地拆（2026-11-10）。松手再按才算新的一次。"""
        self.at(0)
        self.at(1, FakeUse(at=(1, 64, 0)))
        self.view.sneak = True
        self.at(2, FakeUse(at=(1, 64, 0)))
        self.view.sneak = False
        self.view.item = "endernet:common_terminal_part"
        self.at(3, FakeUse(at=(1, 64, 0)))
        self.assertEqual(len(self.events.sent), 1, "一直按着，只算第一下")
        self.at(10, FakeUse(at=(1, 64, 0)))
        self.assertEqual([parse_intent(p).action for p in self.events.sent],
                         [ACTION_CUT, "place_panel"], "松手再按才算新的一次")

    def test_fast_clicking_never_waits(self):
        for tick in range(0, 20, 2):
            self.at(tick)
        self.assertEqual(len(self.events.sent), 10, "每一下都生效，没有冷却")

    def test_the_tick_event_drives_the_clock(self):
        bus = FakeBus()
        client = CableIntentClient(bus, self.events, "block_use", "item_use_on", self.view,
                                   [CABLE], tick_event="tick")
        client.start()
        self.assertEqual(len(bus.listeners), 3)
        ((_cls, tick),) = [entry for entry in bus.listeners if entry[0] == "tick"]
        for _ in range(4):
            tick(object())
        self.assertEqual(client.clock.now(), 4)
        client.stop()
        self.assertEqual(bus.listeners, [])

    def test_press_tracker(self):
        presses = PressTracker(gap=2)
        self.assertTrue(presses.press(0))
        self.assertFalse(presses.owned)
        presses.take()
        self.assertFalse(presses.press(2))
        self.assertTrue(presses.owned, "接管一直记到这次按下结束")
        self.assertFalse(presses.press(4))
        self.assertTrue(presses.owned)
        self.assertTrue(presses.press(7), "断开超过 2 帧：新的一次")
        self.assertFalse(presses.owned, "新的一次按下清掉接管")
        self.assertFalse(presses.press(8), "一直按着就一直不触发")
        self.assertTrue(presses.press(3), "时钟倒退（重启）也算新的一次")
        presses.take()
        presses.reset()
        self.assertFalse(presses.owned)
        self.assertTrue(presses.press(3))


# -- 伪原版交互：接管后整次按下都拦、挥手（2026-11-10）----------------------------


class TestTakeover(unittest.TestCase):
    """`client/interaction/takeover.UseTakeover`（经光缆意图通道）：一次按下只要被接管过，到松手
    为止的所有使用事件都拦掉原版交互、不发、不挥；松手后恢复正常。挥手只在发出意图时挥一次。"""

    def setUp(self):
        self.events = FakeClientEvents()
        self.view = FakeView()
        self.clock = FrameClock()
        self.client = CableIntentClient(
            FakeBus(), self.events, "block_use", "item_use_on", self.view, [CABLE, GLASS, HOLDER],
            clock=self.clock,
        )
        self.client.start()

    def at(self, game_tick, event, via_item=True):
        self.clock.frame = frame_of(game_tick)
        if via_item:
            return self.client.on_item_use_on(event)
        return self.client.on_block_use(event)

    def test_panel_first_cable_later_places_no_extra_cable(self):
        """真机反馈：先放面板、后放光缆，光缆常多放一格。第一下把底座换成光缆后玩家还按着，
        后续事件点在新光缆上（手持光缆点光缆，本来不接管）——现在整次按下都拦。"""
        self.view.item = GLASS
        first = FakeUse(block=HOLDER, item=FakeItem(GLASS), face=2)
        self.assertTrue(self.at(0, first))
        self.assertTrue(first.cancelled)
        later = []
        for tick in range(1, 8):
            event = FakeUse(block=GLASS, item=FakeItem(GLASS), face=2)
            self.assertTrue(self.at(tick, event), tick)
            later.append(event)
            block_event = FakeUse(block=GLASS)
            self.assertTrue(self.at(tick, block_event, via_item=False), tick)
            later.append(block_event)
        self.assertTrue(all(event.cancelled for event in later), "按住期间原版一格也不放")
        self.assertEqual([parse_intent(p).action for p in self.events.sent], ["add_cable"])
        self.assertEqual(self.view.swings, 1, "只在发意图那一下挥")
        self.assertEqual(self.client.stats["held"], 14)
        self.assertEqual(self.view.sneak_reads, 1, "被吞掉的事件连潜行都不问")

    def test_after_release_the_vanilla_interaction_comes_back(self):
        self.view.item = GLASS
        self.at(0, FakeUse(block=HOLDER, item=FakeItem(GLASS), face=2))
        self.at(1, FakeUse(block=GLASS, item=FakeItem(GLASS), face=2))
        again = FakeUse(block=GLASS, item=FakeItem(GLASS), face=2)
        self.assertFalse(self.at(4, again), "松手（断开超过 1 个游戏刻）再按是新的一次")
        self.assertFalse(again.cancelled, "手持光缆点光缆：原版照常放")
        holder = FakeUse(block=HOLDER, item=FakeItem(GLASS), face=2)
        self.assertTrue(self.at(8, holder))
        self.assertEqual(len(self.events.sent), 2)
        self.assertEqual(self.view.swings, 2)

    def test_whatever_is_held_or_targeted_stays_blocked_until_release(self):
        self.at(0, FakeUse(block=CABLE), via_item=False)
        for tick, event in enumerate((
                FakeUse(block="minecraft:stone", item=FakeItem("minecraft:dirt"), face=1),
                FakeUse(block=CABLE, item=FakeItem(CABLE), face=1),
                FakeUse(block="othermod:machine"),
        ), 1):
            self.assertTrue(self.at(tick, event, via_item=event.item is not None))
            self.assertTrue(event.cancelled, event.blockName)
        self.assertEqual(len(self.events.sent), 1)

    def test_a_vanilla_press_is_left_alone_while_held(self):
        for tick in range(5):
            event = FakeUse(block="minecraft:stone", item=FakeItem("minecraft:dirt"), face=1)
            self.assertFalse(self.at(tick, event))
            self.assertFalse(event.cancelled)
        self.assertEqual((self.events.sent, self.view.swings), ([], 0), "原版照常处理的不额外挥手")

    def test_a_block_event_left_to_vanilla_can_still_be_taken_by_the_item_event(self):
        """方块使用事件不带面：普通方块要等同一刻随后的物品事件才接得下来。"""
        self.view.item = "endernet:common_terminal_part"
        block_event = FakeUse(block="othermod:machine")
        self.assertFalse(self.at(0, block_event, via_item=False))
        item_event = FakeUse(block="othermod:machine", item=FakeItem("endernet:common_terminal_part"),
                             face=5)
        self.assertTrue(self.at(0, item_event))
        self.assertEqual([parse_intent(p).action for p in self.events.sent], ["place_panel"])
        self.assertEqual(self.view.swings, 1)

    def test_swing_once_per_intent(self):
        self.at(0, FakeUse(), via_item=False)
        self.at(0, FakeUse(item=FakeItem(WRENCH_ID)))
        self.assertEqual((len(self.events.sent), self.view.swings), (1, 1), "一次点击两条事件只挥一次")
        for tick in range(0, 12, 2):
            self.at(tick + 10, FakeUse(), via_item=False)
        self.assertEqual((len(self.events.sent), self.view.swings), (7, 7), "快速连点每下都挥")

    def test_no_swing_when_no_intent_goes_out(self):
        self.view.origin_error = AttributeError("GetWorldRayFromScreenPoint")
        event = FakeUse()
        self.assertTrue(self.at(0, event, via_item=False))
        self.assertTrue(event.cancelled, "照样拦")
        self.assertEqual((self.events.sent, self.view.swings), ([], 0))

    def test_a_failed_swing_does_not_stop_the_intent(self):
        self.view.swing_error = RuntimeError("Swing")
        self.assertTrue(self.at(0, FakeUse(), via_item=False))
        self.assertEqual(len(self.events.sent), 1)
        self.assertEqual(self.client.stats["swing_failures"], 1)

    def test_stop_forgets_the_press(self):
        self.at(0, FakeUse(), via_item=False)
        self.client.stop()
        self.client.start()
        event = FakeUse(block=GLASS, item=FakeItem(GLASS), face=2)
        self.assertFalse(self.at(1, event), "重新开始后不再沿用上一次按下")
        self.assertFalse(event.cancelled)

    def test_the_rule_and_the_takeover_are_separate(self):
        """封装可复用：任何规则 + 发送函数都能接上（以后的面板种类照这个接）。"""
        from endernet.client.interaction.takeover import UseTakeover

        sent = []

        class Rule(object):
            def wants(self, use):
                return use.block == "mod:bus_panel"

            def decide(self, use, sneaking):
                return "configure" if not sneaking else None

        takeover = UseTakeover(FakeBus(), "b", "i", self.view, Rule(),
                               lambda use, action, ray: sent.append((use.pos, action, ray[0])),
                               clock=self.clock)
        event = FakeUse(block="mod:bus_panel", item=FakeItem("minecraft:stick"), face=1, at=(4, 70, 4))
        self.assertTrue(takeover.on_item_use_on(event))
        self.assertEqual(sent, [((4, 70, 4), "configure", self.view.origin)])
        self.assertEqual((event.cancelled, self.view.swings), (True, 1))
        self.view.sneak = True
        self.clock.frame += 10
        other = FakeUse(block="mod:bus_panel", item=FakeItem("minecraft:stick"), face=1)
        self.assertFalse(takeover.on_item_use_on(other))
        self.assertFalse(other.cancelled)


# -- 服务端通道 ----------------------------------------------------------------


class FakeBinder(object):
    def __init__(self):
        self.registered = []

    def register(self, event_cls, func, priority=0):
        self.registered.append((event_cls, func))
        return func


class TestServerPort(unittest.TestCase):
    def setUp(self):
        self.binder = FakeBinder()
        self.events = testsupport.FakeEventsModule()
        self.notes = []
        self.port = CableIntentPort(
            self.binder,
            events=self.events,
            player_dimension=lambda player_id: 0,
            player_position=lambda player_id: [1.0, 64.0, 2.0],
            held_item=lambda player_id: WRENCH_ID,
            notify=lambda player_id, text: self.notes.append((player_id, text)),
        )

    def dispatch(self, data):
        (request,) = self.port.classes()
        (_cls, func), = self.binder.registered
        return func(request.unmarshal(data))

    def test_listen_and_notify(self):
        calls = []

        def handler(player_id, payload):
            calls.append((player_id, payload))
            return IntentResult(True, "cut", "已切断东面的连接")

        self.assertEqual(self.port.listen(handler), 1)
        self.dispatch({"__id__": PLAYER, "pos": [0, 64, 0]})
        self.assertEqual(calls, [(PLAYER, {"pos": [0, 64, 0]})])
        self.assertEqual(self.notes, [(PLAYER, "已切断东面的连接")])

    def test_silent_results_and_handler_errors(self):
        def silent(player_id, payload):
            return IntentResult(False, "rate_limited")

        self.port.listen(silent)
        self.dispatch({"__id__": PLAYER})
        self.assertEqual(self.notes, [])

        def boom(player_id, payload):
            raise RuntimeError("boom")

        self.binder.registered = []
        self.port.listen(boom)
        self.dispatch({"__id__": PLAYER})
        self.assertEqual(self.port.stats["handler_errors"], 1)

    def test_player_facts(self):
        self.assertEqual(self.port.dimension_of(PLAYER), 0)
        self.assertEqual(self.port.position_of(PLAYER), (1.0, 64.0, 2.0))
        self.assertEqual(self.port.held_item_of(PLAYER), WRENCH_ID)
        self.assertIsNone(self.port.dimension_of(""))
        broken = CableIntentPort(self.binder, events=self.events,
                                 player_dimension=lambda player_id: 1 / 0)
        self.assertIsNone(broken.dimension_of(PLAYER))


class TestThroughTheServer(unittest.TestCase):
    """`ServerBootstrap` 装配的整条链路：意图事件 → 校验 → 切断 → 方块状态。"""

    def test_cut_through_the_bootstrap(self):
        from endernet.server.bootstrap import ServerBootstrap
        from test_bootstrap import FakeEventApi

        world = FakeCableWorld()
        for node_key in (key(0), key(1)):
            world.put_cable(node_key)
        api = FakeEventApi()
        notes = []
        server = ServerBootstrap(
            world=world,
            event_api=api,
            sync_events=testsupport.FakeEventsModule(),
            player_dimension=lambda player_id: 0,
            player_position=lambda player_id: (0.5, 64.0, 2.5),
        )
        server.cable_intent_port.held_item = lambda player_id: WRENCH_ID
        server.cable_intent_port.notify = lambda player_id, text: notes.append(text)
        server.start(bind_engine=True)
        try:
            server.push(NormalizedEvent.place(key(0), CABLE, source="entity_place_after"))
            server.push(NormalizedEvent.place(key(1), CABLE, source="entity_place_after"))
            for _ in range(4):
                server.drive_once()
            self.assertEqual(world.sides(key(0)), sides(east=1))
            (request,) = server.cable_intent_port.classes()
            payload = payload_for(key(0), CABLE, ACTION_CUT, (px(14), 0.5, 0.5), "south")
            payload["__id__"] = PLAYER
            api.dispatch(request, request.unmarshal(payload))
            self.assertEqual(notes, ["已切断东面的连接"])
            for _ in range(4):
                server.drive_once()
            self.assertEqual(server.tick_errors, [])
            self.assertEqual(world.sides(key(0)), sides())
            self.assertEqual(world.sides(key(1)), sides())
            report = server.diagnostics()["cable_intents"]
            self.assertEqual(report["results"], {REASON_CUT: 1})
        finally:
            server.stop()
        self.assertEqual(api.total_listeners(), 0, "意图通道随 stop() 一起注销")


if __name__ == "__main__":
    unittest.main()
