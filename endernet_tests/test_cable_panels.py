# coding=utf-8
"""一格 6 块面板（阶段 D，`.claude/13` §7）：挂、开、拆、挖与面板作为一台终端的全部行为。

覆盖：面板实例引用（`DeviceRef.slot`）与线上格式；种类表单一来源；客户端何时拦、发什么；
挂面板的校验（致密拒绝、占面拒绝、线芯入射面、扣物品 / 创造不扣、写不进去退还）；打开面板
（推哪个屏、没点中不开）；会话以面板为设备（距离、维度、拆后关闭、同格互不干扰）；在线 /
离线随线路准入与控制器范围变化；耗能计入；外观读数与刷新；挖整格掉落；终端库存按面记账、
拆下时与整方块终端同样处置、重启后各归各位。

2026-11-10 真机反馈：挂面板照 AE2（点中的那一面放不了就像火把放到相邻格，点普通方块贴在它
上面、正面朝着它）、面板底座（新建、不入网、最后一块拆掉就消失、手持光缆点它换成光缆且会话
与库存不丢）、放不上静默；面板骨架（种类、正面所对的格子与它的变化、每块面板自己的配置）。
"""

import json
import unittest

import testsupport

from endernet.client.channels.cable_intent import CableIntentClient
from endernet.client.channels.slot_display import SlotDisplayClient
from endernet.common.cable_shapes import PANEL_KINDS, PANEL_PART_IDS, panel_part_id
from endernet.common.channels.cable_intent import (
    ACTION_ADD_CABLE,
    ACTION_CUT,
    ACTION_DISMANTLE,
    ACTION_PLACE_PANEL,
    WRENCH_ID,
    action_for,
    parse_intent,
)
from endernet.common.channels.slot_display import (
    DISK,
    EMPTY,
    FACING_VARIABLE,
    FIRST_MODEL,
    OFFLINE_OFFSET,
    PANEL,
    PANEL_DEVICES,
    has_facing,
    kind_of,
    parse_update,
    request_payload,
    slot_count,
    variable_of,
)
from endernet.common.definitions import (
    CATEGORY_TERMINAL,
    FORM_PART,
    PART_KIND_TERMINAL,
    PART_KIND_TUNNEL,
    TUNNEL_PART_ID,
    PATTERN_MODE_FACTORY,
    DefinitionSet,
    DeviceDefinition,
)
from endernet.common.identity import FACING_NAMES, DeviceRef, NodeKey, PartKey, is_part_ref, ref_key
from endernet.common.protocol import (
    MSG_DEVICE_STATUS,
    MSG_ERROR,
    MSG_SESSION_READY,
    CommandKind,
    CommandSource,
    DeviceCommand,
    Reason,
    SessionOpen,
    SyncEnvelope,
    VIEW_ITEM,
)
from endernet.common.resources.key import ResourceKey
from endernet.server.devices.catalog import CABLE_PARTS_KEY
from endernet.server.adapters.events import ServerEventBinder
from endernet.server.devices.panels import (
    REASON_CABLE_ADDED,
    REASON_CANNOT_HOLD_PANELS,
    REASON_ITEM_GONE,
    REASON_NO_ROOM,
    REASON_NOT_A_HOLDER,
    REASON_PANEL_PLACED,
    PanelService,
    part_ref,
)
from endernet.server.devices.cable_intents import (
    REASON_BLOCK_MISMATCH,
    REASON_CABLE_REMOVED,
    REASON_HOLDER_HAS_NO_LINKS,
    REASON_MISSED,
    REASON_TOO_FAR,
    REASON_WRITE_FAILED,
    REASON_WRONG_ITEM,
)
from endernet.server.interaction.feedback import PlaceFeedback
from endernet.server.devices.slot_display import SlotDisplayTracker
from endernet.server.network.energy import EnergyPolicy
from endernet.server.network.services import ServiceId
from endernet.server.persistence.terminals import instance_key, row_key, saved_row
from endernet.server.runtime.events import EventKind, NormalizedEvent
from endernet.server.runtime.lifecycle import ChangeKind
from endernet.server.crafting.terminals.template import INPUT_SLOT
from test_cable_intents import (
    CENTER,
    PLAYER,
    FakeBus,
    FakeClientEvents,
    FakeItem,
    FakePlayers,
    FakeUse,
    FakeView,
    IntentRig,
    payload_for,
    px,
)
from test_cable_shapes import ray_toward
from test_cable_states import FakeCableWorld, key, sides

GLASS = "endernet:ender_glass_cable"
CABLE = "endernet:ender_cable"
DENSE = "endernet:ender_dense_cable"
HOLDER = "endernet:part_holder"
STONE = "minecraft:stone"
COMMON_TERMINAL = "endernet:common_terminal_panel"
BB8 = "endernet:network_baseband_8"
BB32 = "endernet:network_baseband_32"
TINY = "endernet:tiny_network_endpoint"
DOS = "endernet:dos_network_endpoint"
ENERGY = "endernet:creative_energy_source"
COMMON_PART = "endernet:common_terminal_part"
CRAFT_PART = "endernet:crafting_terminal_part"
TEMPLATE_PART = "endernet:template_terminal_part"
FACTORY_PART = "endernet:factoric_terminal_part"
CRAFT_SCREEN = "endernet_crafting_terminal_panel.main"
IRON = ResourceKey.item("minecraft:iron_ingot")
BLANK = ResourceKey.item("endernet:factorical_template")


# -- 替身 ----------------------------------------------------------------------


class PanelWorld(FakeCableWorld):
    """光缆假世界 + 终端库存的实例标记（宿主方块实体里按面各一份）。"""

    def __init__(self):
        FakeCableWorld.__init__(self)
        #: PartKey -> 标记；引擎读不到就现发一个（同 `terminal_inventory_id`）
        self.markers = {}
        self.renewed = []
        self.renew_ok = True
        self._serial = 0

    def _token(self):
        self._serial += 1
        return "tok-%d" % (self._serial,)

    def terminal_inventory_id(self, key):
        part = PartKey.of(key)
        if part not in self.markers:
            self.markers[part] = self._token()
        return self.markers[part]

    def renew_terminal_inventory_id(self, part_key):
        self.renewed.append(part_key)
        if not self.renew_ok:
            return False
        self.markers[PartKey.of(part_key)] = self._token()
        return True


class PanelPlayers(FakePlayers):
    def __init__(self):
        FakePlayers.__init__(self, item=COMMON_PART)
        self.creative = False
        self.take_ok = True
        self.taken = []
        #: 右键开界面那条路读的潜行（`ServerEventBinder._is_sneaking`）
        self.sneaking = False

    def is_creative(self, player_id):
        return self.creative

    def take_held_item(self, player_id, item_id):
        if not self.take_ok:
            return False
        self.taken.append((player_id, item_id))
        return True


class ServerUse(object):
    """`ServerBlockUseEvent` / `ServerItemUseOnEvent` 的替身：两条事件的坐标、方块、物品与命中点
    字段同名，玩家字段不同（`playerId` / `entityId`），这里两个都给。"""

    def __init__(self, node_key, block, click=None, item="", player=PLAYER):
        self.playerId = self.entityId = player
        self.dimensionId = node_key.dimension
        self.x, self.y, self.z = node_key.x, node_key.y, node_key.z
        self.blockName = block
        self.clickX, self.clickY, self.clickZ = click if click is not None else (None, None, None)
        self.item = FakeItem(item) if item else None
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def right_click(binder, node_key, block, target, face, item="", eye=True, player=PLAYER):
    """玩家从 `face` 那一侧右键 `node_key` 这一格的 `target`（格内坐标）：引擎同一下派的两条服务端
    事件（方块使用；手持物品时再来一条物品使用）依次喂给事件适配器。眼睛放在离那一格 2 格的
    射线起点（换回世界坐标）；`eye=False` 是读不到眼睛。返回 `(方块事件, 物品事件或 None)`。"""
    local, _direction = ray_toward(target, face, 2.0)
    position = (node_key.x + local[0], node_key.y + local[1], node_key.z + local[2])
    binder._player_eye = lambda player_id: position if eye else None
    block_event = ServerUse(node_key, block, target, item, player)
    binder._on_block_use(block_event)
    item_event = None
    if item:
        item_event = ServerUse(node_key, block, target, item, player)
        binder._on_item_use_on(item_event)
    return block_event, item_event


class PanelRig(IntentRig):
    """`IntentRig`（注册表、拓扑、生命周期、状态维护、意图服务）+ 面板服务 + 事件适配器（右键
    开界面的统一入口与服务端拦截挂在它上面，推屏换成记账）。"""

    def __init__(self, **options):
        options.setdefault("world", PanelWorld())
        IntentRig.__init__(self, **options)
        self.players = PanelPlayers()
        self.service.players = self.players
        self.service.items.players = self.players
        self.opened = []
        self.panels = PanelService(
            self.definitions, self.registry, self.catalog, self.keeper, self.world, self.players,
        )
        self.binder = ServerEventBinder(None, None, self.registry, definitions=None)
        self.binder._notify_ui = lambda *args: self.opened.append(args)
        self.binder._is_sneaking = lambda player_id: self.players.sneaking
        self.panels.register(self.service, guard=self.binder.use_guard, openers=self.binder.openers)

    def parts(self, node_key):
        return self.registry.get(node_key).config[CABLE_PARTS_KEY]

    def act(self, node_key, block, action, target, face, item=None):
        if item is not None:
            self.players.item = item
        return self.send(payload_for(node_key, block, action, target, face, item=self.players.item))

    def use(self, node_key, block, target, face, item="", eye=True):
        return right_click(self.binder, node_key, block, target, face, item, eye)


# -- 实例引用与线上格式 ----------------------------------------------------------


class TestPanelReferences(unittest.TestCase):
    def test_center_refs_keep_their_old_shape(self):
        ref = DeviceRef(key(1), 3)
        self.assertEqual(ref.to_dict(), {"node_key": key(1).to_dict(), "generation": 3})
        self.assertEqual(ref.as_key(), (key(1).as_key(), 3))
        self.assertFalse(ref.is_part())
        self.assertFalse(is_part_ref(ref))
        self.assertIs(ref.host(), ref)
        self.assertEqual(ref_key(ref), key(1))

    def test_panel_refs(self):
        ref = DeviceRef(key(1), 3, "north")
        self.assertEqual(ref, DeviceRef(PartKey(key(1), "north"), 3))
        self.assertEqual(ref.part_key, PartKey(key(1), "north"))
        self.assertEqual(ref_key(ref), PartKey(key(1), "north"))
        self.assertEqual(ref.host(), DeviceRef(key(1), 3))
        self.assertFalse(ref.matches(ref.host()), "面板与宿主光缆不是同一个实例")
        self.assertNotEqual(ref, DeviceRef(key(1), 3, "south"))
        self.assertEqual(ref.to_dict()["slot"], "north")
        self.assertEqual(DeviceRef.from_dict(json.loads(json.dumps(ref.to_dict()))), ref)
        self.assertIn("north", repr(ref))
        self.assertEqual(len(set([ref, ref.host(), DeviceRef(key(1), 3, "up")])), 3)

    def test_bad_slots_are_refused(self):
        with self.assertRaises(ValueError):
            DeviceRef(key(1), 1, "middle")
        with self.assertRaises(ValueError):
            DeviceRef(PartKey(key(1), "up"), 1, "down")
        with self.assertRaises(ValueError):
            DeviceRef.from_dict({"node_key": key(1).to_dict(), "generation": 1, "slot": "center"})

    def test_session_open_carries_the_slot(self):
        ref = DeviceRef(key(2), 5, "east")
        envelope = SyncEnvelope(SessionOpen("r1", ref, 0), view_kind=VIEW_ITEM)
        wire = json.loads(json.dumps(envelope.to_dict()))
        self.assertEqual(SyncEnvelope.from_dict(wire).message.device_ref, ref)
        wire["message"]["device_ref"]["player"] = "me"
        with self.assertRaises(ValueError):
            SyncEnvelope.from_dict(wire)

    def test_registry_never_mistakes_a_panel_for_its_cable(self):
        rig = PanelRig()
        rig.place(key(0), GLASS, parts={"up": COMMON_PART})
        host = rig.registry.get(key(0))
        panel = part_ref(host, "up")
        self.assertIsNone(rig.registry.get_by_ref(panel))
        self.assertEqual(rig.catalog.resolve(rig.registry, panel), (host, COMMON_PART))
        self.assertEqual(rig.catalog.resolve(rig.registry, host.ref), (host, GLASS))
        self.assertEqual(rig.catalog.resolve(rig.registry, part_ref(host, "down")), (None, None))
        stale = DeviceRef(key(0), host.generation + 1, "up")
        self.assertEqual(rig.catalog.resolve(rig.registry, stale), (None, None))


class TestPanelKinds(unittest.TestCase):
    def test_kinds_are_the_registered_parts(self):
        """外观种类表是四种终端面板，P2P 隧道另有一块屏幕排在最后（`PANEL_SCREENS`）；合起来就是
        全部面板。"""
        definitions = DefinitionSet()
        terminals = [part_id for part_id in definitions.parts
                     if definitions.part_kind_of(part_id) == PART_KIND_TERMINAL]
        self.assertEqual(sorted(PANEL_PART_IDS[:len(PANEL_KINDS)]), sorted(terminals))
        self.assertEqual(PANEL_PART_IDS[len(PANEL_KINDS):], (TUNNEL_PART_ID,))
        self.assertEqual(sorted(PANEL_PART_IDS), sorted(definitions.parts))
        for (kind, terminal), part_id in zip(PANEL_KINDS, PANEL_PART_IDS):
            self.assertEqual(part_id, panel_part_id(kind))
            self.assertEqual(definitions.get(part_id).whole_block_id, terminal)
            self.assertEqual(definitions.terminal_screen_of(part_id),
                             definitions.terminal_screen_of(terminal))

    def test_the_generator_reads_the_same_table(self):
        from test_cable_blocks import scripts_module

        generator = scripts_module("gen_cable_models")
        self.assertIs(generator.PANEL_KINDS, PANEL_KINDS)
        for index, (kind, _terminal) in enumerate(PANEL_KINDS):
            self.assertEqual(generator.panel_value(kind), FIRST_MODEL + index)
        for index in range(len(FACING_NAMES)):
            self.assertEqual(generator.panel_variable(index), variable_of(PANEL, index))

    def test_display_table(self):
        self.assertEqual(sorted(PANEL_DEVICES), sorted([CABLE, GLASS, HOLDER]),
                         "面板底座的外观全靠面板实体")
        self.assertEqual(kind_of(GLASS), PANEL)
        self.assertEqual(kind_of(HOLDER), PANEL)
        self.assertIsNone(kind_of(DENSE))
        self.assertEqual(slot_count(PANEL), 6)
        self.assertFalse(has_facing(PANEL))
        self.assertTrue(has_facing(DISK))
        self.assertEqual(variable_of(PANEL, 2), "variable.p2")
        self.assertEqual(variable_of(DISK, 2), "variable.s2")


# -- 客户端：什么时候拦、发什么 --------------------------------------------------


class TestClientActions(unittest.TestCase):
    def setUp(self):
        self.events = FakeClientEvents()
        self.view = FakeView()
        self.client = CableIntentClient(
            FakeBus(), self.events, "block_use", "item_use_on", self.view, [CABLE, GLASS, DENSE]
        )
        self.client.start()

    def test_action_table(self):
        for block in (GLASS, CABLE, DENSE, HOLDER, STONE, BB8):
            self.assertEqual(action_for(CRAFT_PART, False, block), ACTION_PLACE_PANEL, block)
            self.assertEqual(action_for(CRAFT_PART, True, block), ACTION_PLACE_PANEL,
                             "潜行也挂（2026-11-10）")
        self.assertIsNone(action_for(CRAFT_PART, False, COMMON_TERMINAL),
                          "右键会开界面的设备照旧开界面")
        self.assertIsNone(action_for(CRAFT_PART, False, "endernet:storage_driver"))
        for block in (COMMON_TERMINAL, "endernet:storage_driver", "minecraft:chest"):
            self.assertEqual(action_for(CRAFT_PART, True, block), ACTION_PLACE_PANEL,
                             "潜行能贴到会开界面的方块上")
        for block in (GLASS, CABLE, HOLDER, DENSE, STONE):
            self.assertIsNone(action_for("", False, block), "右键面板开界面交给服务端的原版事件")
            self.assertIsNone(action_for(STONE, False, block))
        self.assertIsNone(action_for("", True, CABLE))
        for cable in (GLASS, CABLE, DENSE):
            self.assertEqual(action_for(cable, False, HOLDER), ACTION_ADD_CABLE, cable)
            self.assertIsNone(action_for(cable, True, HOLDER), "潜行照原版放方块")
            self.assertIsNone(action_for(cable, False, GLASS), "只对底座")
        self.assertIsNone(action_for(CRAFT_PART, False), "不给被点的光缆就只认扳手（C 的调用形状）")
        self.assertEqual(action_for(WRENCH_ID, True, GLASS), ACTION_DISMANTLE)

    def test_opening_a_panel_is_left_to_the_server(self):
        """空手或手持别的东西点能挂面板的方块：客户端不接管。点没点中面板只有服务端知道，拦下来
        就没法再把这一下还给原版；交给服务端的原版事件（`server/interaction/opening.py`）。"""
        for item in ("", STONE):
            self.view.item = item
            event = FakeUse(block=GLASS)
            self.assertFalse(self.client.on_block_use(event), item)
            self.assertFalse(event.cancelled)
            used = FakeUse(block=GLASS, item=FakeItem(STONE), face=5)
            self.assertFalse(self.client.on_item_use_on(used))
            self.assertFalse(used.cancelled)
        self.assertEqual(self.events.sent, [])

    def test_empty_hand_on_a_dense_cable_keeps_the_vanilla_interaction(self):
        self.view.item = ""
        event = FakeUse(block=DENSE)
        self.assertFalse(self.client.on_block_use(event))
        self.assertFalse(event.cancelled)
        self.assertEqual(self.events.sent, [])

    def test_a_panel_item_is_intercepted_and_sent(self):
        event = FakeUse(block=CABLE, item=FakeItem(CRAFT_PART), face=4)
        self.assertTrue(self.client.on_item_use_on(event))
        self.assertTrue(event.cancelled)
        intent = parse_intent(self.events.sent[0])
        self.assertEqual((intent.action, intent.item, intent.face),
                         (ACTION_PLACE_PANEL, CRAFT_PART, "west"))

    def test_a_panel_item_on_any_block_is_sent_with_the_clicked_face(self):
        event = FakeUse(block=STONE, item=FakeItem(CRAFT_PART), face=5, click=(1.0, 0.3, 0.6))
        self.assertTrue(self.client.on_item_use_on(event))
        self.assertTrue(event.cancelled)
        intent = parse_intent(self.events.sent[0])
        self.assertEqual((intent.action, intent.block, intent.face), (ACTION_PLACE_PANEL, STONE, "east"))

    def test_block_use_on_other_blocks_waits_for_the_item_event(self):
        """方块使用事件不带面：普通方块要等随后的物品事件（带面）才挂。"""
        self.view.item = CRAFT_PART
        event = FakeUse(block="othermod:machine")
        self.assertFalse(self.client.on_block_use(event))
        self.assertFalse(event.cancelled)
        self.assertEqual(self.events.sent, [])

    def test_terminals_keep_opening_their_screens(self):
        event = FakeUse(block=COMMON_TERMINAL, item=FakeItem(CRAFT_PART), face=1)
        self.assertFalse(self.client.on_item_use_on(event))
        self.assertFalse(event.cancelled)

    def test_a_cable_item_on_a_holder_is_sent(self):
        client = CableIntentClient(
            FakeBus(), self.events, "block_use", "item_use_on", self.view, [CABLE, GLASS, HOLDER])
        client.start()
        event = FakeUse(block=HOLDER, item=FakeItem(GLASS), face=2)
        self.assertTrue(client.on_item_use_on(event))
        self.assertEqual(parse_intent(self.events.sent[-1]).action, ACTION_ADD_CABLE)

    def test_sneaking_with_a_panel_places_it_even_on_a_terminal(self):
        self.view.sneak = True
        event = FakeUse(block=COMMON_TERMINAL, item=FakeItem(CRAFT_PART), face=1)
        self.assertTrue(self.client.on_item_use_on(event))
        self.assertTrue(event.cancelled)
        intent = parse_intent(self.events.sent[0])
        self.assertEqual((intent.action, intent.face), (ACTION_PLACE_PANEL, "up"))

    def test_other_items_and_sneaking_keep_the_vanilla_interaction(self):
        for item, sneak in (("minecraft:stone", False), (GLASS, True), ("", True)):
            self.view.sneak = sneak
            self.view.item = item
            event = FakeUse(block=CABLE, item=FakeItem(item) if item else None)
            handled = self.client.on_item_use_on(event) if item else self.client.on_block_use(event)
            self.assertFalse(handled, (item, sneak))
            self.assertFalse(event.cancelled, (item, sneak))
        self.assertEqual(self.events.sent, [])


# -- 挂面板 --------------------------------------------------------------------


class TestPlace(unittest.TestCase):
    """对着光缆挂：点中的那一面空着就挂上，否则像火把一样放到相邻格（AE2）。放不上一律静默。"""

    def setUp(self):
        self.rig = PanelRig()
        self.rig.place(key(0), GLASS)
        self.rig.place(key(1), CABLE)
        self.rig.step(3)

    def holder_at(self, node_key):
        """把补进来的放置事件交给生命周期（启动层里由事件队列做），再推进几帧。"""
        rig = self.rig
        for event in rig.pushed:
            rig.lifecycle.handle(event)
        rig.pushed[:] = []
        rig.step(3)
        return rig.registry.get(node_key)

    def test_a_core_hit_takes_the_face_the_ray_enters(self):
        rig = self.rig
        result = rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up")
        self.assertEqual((result.ok, result.reason, result.message), (True, REASON_PANEL_PLACED, ""))
        self.assertEqual(rig.parts(key(0)), {"up": COMMON_PART})
        self.assertEqual(rig.world.entities[key(0)][1], {"up": COMMON_PART}, "方块实体与记录一起写")
        self.assertEqual(rig.players.taken, [(PLAYER, COMMON_PART)])
        self.assertEqual(rig.world.renewed, [PartKey(key(0), "up")], "这一面换了新的库存标记")
        self.assertEqual(rig.world.part_configs[PartKey(key(0), "up")], {}, "配置从空开始")
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), sides(up=2, east=1))

    def test_an_arm_hit_takes_that_face_and_the_link_gives_way(self):
        rig = self.rig
        result = rig.act(key(0), GLASS, ACTION_PLACE_PANEL, (px(14), 0.5, 0.5), "south")
        self.assertEqual(result.reason, REASON_PANEL_PLACED)
        self.assertEqual(rig.parts(key(0)), {"east": COMMON_PART})
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), sides(east=2))
        self.assertEqual(rig.world.sides(key(1)), sides(), "面板占面即不外连")

    def test_clicking_a_panel_places_the_next_one_in_the_neighbouring_cell(self):
        """点中已有的面板 → 像火把：被点格 + 被点击的面，面板贴回来、正面朝着被点的格。"""
        rig = self.rig
        rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up")
        rig.step(3)
        result = rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up", item=CRAFT_PART)
        self.assertEqual((result.ok, result.reason, result.message), (True, REASON_PANEL_PLACED, ""))
        above = key(0, y=65)
        self.assertEqual(rig.parts(key(0)), {"up": COMMON_PART}, "被点的那格不动")
        self.assertEqual(rig.world.placed, [(above, HOLDER)])
        self.assertEqual(rig.world.entities[above], (0, {"down": CRAFT_PART}))
        self.assertEqual(rig.world.renewed[-1], PartKey(above, "down"))
        self.assertEqual([(e.kind, e.node_key, e.data["device_id"]) for e in rig.pushed],
                         [(EventKind.PLACE, above, HOLDER)])
        holder = self.holder_at(above)
        self.assertEqual((holder.device_id, holder.config[CABLE_PARTS_KEY]), (HOLDER, {"down": CRAFT_PART}))
        self.assertEqual(rig.world.sides(above), sides(down=2))
        self.assertEqual(PartKey(above, "down").front_key(), key(0), "正面朝着被点的格子")

    def test_a_recorded_panel_not_yet_drawn_also_sends_it_next_door(self):
        """记录里已经有、外观还没刷到（射线穿过去点中线芯）：照样算占着，放到相邻格。"""
        rig = self.rig
        self.assertTrue(rig.keeper.write_faces(key(0), parts={"north": CRAFT_PART}))
        result = rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "north")
        self.assertEqual(result.reason, REASON_PANEL_PLACED)
        self.assertEqual(rig.parts(key(0)), {"north": CRAFT_PART})
        self.assertEqual(rig.world.placed, [(key(0, z=-1), HOLDER)])
        self.assertEqual(rig.world.entities[key(0, z=-1)][1], {"south": COMMON_PART})

    def test_a_dense_cable_sends_it_next_door(self):
        rig = self.rig
        rig.place(key(5), DENSE)
        rig.step(2)
        result = rig.act(key(5), DENSE, ACTION_PLACE_PANEL, CENTER, "up")
        self.assertEqual((result.ok, result.reason, result.message), (True, REASON_PANEL_PLACED, ""))
        self.assertEqual(rig.registry.get(key(5)).config[CABLE_PARTS_KEY], {})
        self.assertEqual(rig.world.entities[key(5, y=65)][1], {"down": COMMON_PART})

    def test_the_event_face_wins_over_the_hull(self):
        """事件带了被点击的面就用它（不再按外包盒求）。"""
        rig = self.rig
        rig.place(key(5), DENSE)
        rig.step(2)
        payload = payload_for(key(5), DENSE, ACTION_PLACE_PANEL, CENTER, "up", item=COMMON_PART,
                              clicked="west")
        self.assertEqual(rig.send(payload).reason, REASON_PANEL_PLACED)
        self.assertEqual(rig.world.entities[key(4)][1], {"east": COMMON_PART})

    def test_next_door_can_be_a_cable_with_that_face_free(self):
        rig = self.rig
        rig.place(key(0, y=65), CABLE)
        rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up")
        rig.step(3)
        result = rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up", item=CRAFT_PART)
        self.assertEqual(result.reason, REASON_PANEL_PLACED)
        self.assertEqual(rig.parts(key(0, y=65)), {"down": CRAFT_PART})
        self.assertEqual(rig.world.placed, [], "加在已有的光缆上，不新建底座")

    def test_next_door_that_cannot_take_it_places_nothing_silently(self):
        rig = self.rig
        rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up")
        rig.step(3)
        for block in (STONE, DENSE):
            rig.world.blocks[key(0, y=65)] = block
            if block == DENSE:
                rig.place(key(0, y=65), DENSE)
                rig.step(2)
            result = rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up", item=CRAFT_PART)
            self.assertEqual((result.ok, result.reason, result.message), (False, REASON_NO_ROOM, ""))
        self.assertEqual(len(rig.players.taken), 1, "放不上不扣物品")
        self.assertEqual(rig.world.placed, [])

    def test_next_door_face_already_taken_places_nothing(self):
        rig = self.rig
        rig.place(key(0, y=65), CABLE, parts={"down": TEMPLATE_PART})
        rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up")
        rig.step(3)
        result = rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up", item=CRAFT_PART)
        self.assertEqual(result.reason, REASON_NO_ROOM)

    def test_creative_mode_does_not_consume(self):
        rig = self.rig
        rig.players.creative = True
        self.assertTrue(rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up").ok)
        self.assertEqual(rig.players.taken, [])
        self.assertEqual(rig.parts(key(0)), {"up": COMMON_PART})

    def test_an_item_that_is_gone_is_refused(self):
        rig = self.rig
        rig.players.take_ok = False
        result = rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up")
        self.assertEqual((result.ok, result.reason, result.message), (False, REASON_ITEM_GONE, ""))
        self.assertEqual(rig.parts(key(0)), {})

    def test_a_failed_write_gives_the_item_back(self):
        rig = self.rig
        rig.world.entity_writable = False
        result = rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up")
        self.assertEqual((result.ok, result.reason, result.message), (False, REASON_WRITE_FAILED, ""))
        self.assertEqual(rig.parts(key(0)), {})
        self.assertEqual(rig.world.given, [(PLAYER, [COMMON_PART])])
        rig.players.creative = True
        rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up")
        self.assertEqual(len(rig.world.given), 1, "创造模式没扣，也就不退")

    def test_a_failed_identity_renewal_keeps_the_face_empty(self):
        rig = self.rig
        rig.world.renew_ok = False
        result = rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up")
        self.assertEqual(result.reason, REASON_WRITE_FAILED)
        self.assertEqual(rig.parts(key(0)), {})
        self.assertEqual(rig.world.given, [(PLAYER, [COMMON_PART])])

    def test_a_holder_that_cannot_be_written_is_taken_down_again(self):
        rig = self.rig
        rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up")
        rig.step(3)
        rig.world.renew_ok = False
        result = rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up", item=CRAFT_PART)
        self.assertEqual(result.reason, REASON_WRITE_FAILED)
        self.assertEqual(rig.world.removed, [key(0, y=65)], "新建的底座拆回空气")
        self.assertEqual(rig.pushed, [], "没登记")
        self.assertEqual(rig.world.given[-1], (PLAYER, [CRAFT_PART]))

    def test_the_held_item_must_be_a_panel(self):
        result = self.rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up", item="minecraft:stone")
        self.assertEqual((result.reason, result.message), (REASON_WRONG_ITEM, ""), "放面板不弹提示")

    def test_six_panels_on_one_cable(self):
        rig = PanelRig()
        rig.place(key(10), GLASS)
        rig.step(2)
        rig.players.position = (10.5, 64.0, 0.5)
        for face, part_id in zip(FACING_NAMES, PANEL_PART_IDS + PANEL_PART_IDS[:2]):
            result = rig.act(key(10), GLASS, ACTION_PLACE_PANEL, CENTER, face, item=part_id)
            self.assertEqual(result.reason, REASON_PANEL_PLACED, face)
            rig.step(2)
        self.assertEqual(sorted(rig.parts(key(10))), sorted(FACING_NAMES))
        self.assertEqual(len(set(rig.world.markers[PartKey(key(10), face)] for face in FACING_NAMES)), 6)
        self.assertEqual(rig.world.sides(key(10)), dict((face, 2) for face in FACING_NAMES))


class TestPlaceOnBlocks(unittest.TestCase):
    """对着普通方块 B 的面 F 挂：放到 B + F，面 = F 的反方向（贴在 B 上、正面朝着 B）。"""

    def setUp(self):
        self.rig = PanelRig()
        self.rig.world.blocks[key(3)] = STONE
        self.rig.players.position = (3.5, 64.0, 2.5)

    def click(self, face, node_key=None, block=STONE, item=COMMON_PART):
        node_key = node_key if node_key is not None else key(3)
        self.rig.players.item = item
        payload = payload_for(node_key, block, ACTION_PLACE_PANEL, CENTER, face, item=item, clicked=face)
        return self.rig.send(payload)

    def test_a_new_holder_sticks_to_the_clicked_face_and_looks_at_the_block(self):
        rig = self.rig
        result = self.click("east")
        self.assertEqual((result.ok, result.reason, result.message), (True, REASON_PANEL_PLACED, ""))
        self.assertEqual(rig.world.placed, [(key(4), HOLDER)])
        self.assertEqual(rig.world.entities[key(4)], (0, {"west": COMMON_PART}))
        self.assertEqual(PartKey(key(4), "west").front_key(), key(3))
        for event in rig.pushed:
            rig.lifecycle.handle(event)
        rig.step(3)
        self.assertEqual(rig.registry.get(key(4)).device_id, HOLDER)
        self.assertEqual(rig.world.sides(key(4)), sides(west=2))
        self.assertEqual(rig.panels.display_values(key(4))[FACING_NAMES.index("west")],
                         FIRST_MODEL + PANEL_PART_IDS.index(COMMON_PART))

    def test_a_cable_next_to_the_block_takes_it_on_that_face(self):
        rig = self.rig
        rig.place(key(3, y=65), CABLE)
        rig.step(2)
        result = self.click("up")
        self.assertEqual(result.reason, REASON_PANEL_PLACED)
        self.assertEqual(rig.parts(key(3, y=65)), {"down": COMMON_PART})
        self.assertEqual(rig.world.placed, [])

    def test_a_registered_device_is_just_a_block_here(self):
        rig = self.rig
        rig.place(key(3), BB8)
        result = self.click("north", block=BB8)
        self.assertEqual(result.reason, REASON_PANEL_PLACED)
        self.assertEqual(rig.world.entities[key(3, z=-1)][1], {"south": COMMON_PART})

    def test_without_the_clicked_face_nothing_is_placed(self):
        rig = self.rig
        payload = payload_for(key(3), STONE, ACTION_PLACE_PANEL, CENTER, "east", item=COMMON_PART)
        result = rig.send(payload)
        self.assertEqual((result.ok, result.reason, result.message), (False, REASON_NO_ROOM, ""))
        self.assertEqual(rig.players.taken, [])

    def test_the_block_must_be_what_the_client_saw(self):
        result = self.click("east", block="minecraft:dirt")
        self.assertEqual((result.reason, result.message), (REASON_BLOCK_MISMATCH, ""))

    def test_too_far_is_silent(self):
        self.rig.players.position = (40.0, 64.0, 0.0)
        result = self.click("east")
        self.assertEqual((result.reason, result.message), (REASON_TOO_FAR, ""))

    def test_an_occupied_cell_places_nothing(self):
        """世界没有 `may_place`（问不了可不可替换）就只认空气。"""
        rig = self.rig
        rig.world.blocks[key(4)] = "minecraft:water"
        self.assertEqual(self.click("east").reason, REASON_NO_ROOM)
        self.assertEqual(rig.world.placed, [])


    def test_a_replaceable_block_next_door_is_taken_like_air(self):
        """草、水这类可替换方块当空气（世界的 `may_place`，引擎 `MayPlace`）：新建底座顶掉它。"""
        rig = self.rig
        rig.world.blocks[key(4)] = "minecraft:tallgrass"
        asked = []

        def may_place(node_key, block_id, face):
            asked.append((node_key, block_id, face))
            return rig.world.blocks.get(node_key) in ("minecraft:tallgrass", "minecraft:water")

        rig.world.may_place = may_place
        self.assertEqual(self.click("east").reason, REASON_PANEL_PLACED)
        self.assertEqual(rig.world.placed, [(key(4), HOLDER)])
        self.assertEqual(asked, [(key(4), HOLDER, "east")], "按点中的那一面问")
        rig.world.blocks[key(3, y=65)] = "minecraft:water"
        rig.world.blocks[key(3, y=63)] = "minecraft:chest"
        self.assertEqual(self.click("up").reason, REASON_PANEL_PLACED)
        self.assertEqual(self.click("down").reason, REASON_NO_ROOM, "不可替换的方块不放")
        self.assertEqual(rig.world.placed, [(key(4), HOLDER), (key(3, y=65), HOLDER)])

    def test_air_never_asks_the_engine(self):
        rig = self.rig
        rig.world.may_place = lambda *args: self.fail("空气直接放，不问引擎")
        self.assertEqual(self.click("east").reason, REASON_PANEL_PLACED)

    def test_a_failing_may_place_counts_as_not_replaceable(self):
        rig = self.rig
        rig.world.blocks[key(4)] = "minecraft:tallgrass"
        rig.world.may_place = lambda *args: 1 / 0
        self.assertEqual(self.click("east").reason, REASON_NO_ROOM)


class TestAddCable(unittest.TestCase):
    """先放面板、后放光缆：手持玻璃或普通光缆点面板底座，原地换成那种光缆。"""

    def setUp(self):
        self.rig = PanelRig()
        self.rig.place(key(0), HOLDER, parts={"north": COMMON_PART, "up": CRAFT_PART})
        self.rig.place(key(1), CABLE)
        self.rig.step(3)
        self.rig.changes = []

    def add(self, item):
        self.rig.players.item = item
        return self.rig.send(payload_for(key(0), HOLDER, ACTION_ADD_CABLE, CENTER, "north", item=item))

    def test_the_holder_becomes_that_cable_with_its_panels(self):
        rig = self.rig
        before = rig.registry.get(key(0)).ref
        result = self.add(GLASS)
        self.assertEqual((result.ok, result.reason, result.message), (True, REASON_CABLE_ADDED, ""))
        record = rig.registry.get(key(0))
        self.assertEqual((record.device_id, record.ref), (GLASS, before), "同一个实例：面板引用不变")
        self.assertEqual(rig.parts(key(0)), {"north": COMMON_PART, "up": CRAFT_PART})
        self.assertEqual(rig.players.taken, [(PLAYER, GLASS)])
        self.assertEqual([c.kind for c in rig.changes if c.kind in (
            ChangeKind.HOST_CONVERTED, ChangeKind.PART_DETACHED, ChangeKind.REMOVED)],
            [ChangeKind.HOST_CONVERTED], "面板没有被当成离开")
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), sides(north=2, up=2, east=1))
        self.assertEqual(rig.world.sides(key(1)), sides(west=1))

    def test_a_dense_cable_is_not_placed(self):
        rig = self.rig
        result = self.add(DENSE)
        self.assertEqual((result.ok, result.reason, result.message), (False, REASON_CANNOT_HOLD_PANELS, ""))
        self.assertEqual(rig.registry.get(key(0)).device_id, HOLDER)
        self.assertEqual(rig.players.taken, [])

    def test_creative_mode_does_not_consume(self):
        self.rig.players.creative = True
        self.assertTrue(self.add(CABLE).ok)
        self.assertEqual(self.rig.players.taken, [])

    def test_a_failed_swap_gives_the_cable_back(self):
        rig = self.rig
        rig.world.swap_ok = False
        result = self.add(GLASS)
        self.assertEqual((result.reason, result.message), (REASON_WRITE_FAILED, ""))
        self.assertEqual(rig.registry.get(key(0)).device_id, HOLDER)
        self.assertEqual(rig.world.given, [(PLAYER, [GLASS])])

    def test_only_a_holder_takes_a_cable(self):
        rig = self.rig
        rig.players.item = GLASS
        payload = payload_for(key(1), CABLE, ACTION_ADD_CABLE, CENTER, "north", item=GLASS)
        self.assertEqual(rig.send(payload).reason, REASON_NOT_A_HOLDER)
        self.assertEqual(rig.registry.get(key(1)).device_id, CABLE)


class TestHolderPanels(unittest.TestCase):
    """面板底座上的面板：空手能开，扳手能拆。"""

    def test_opening_a_panel_on_a_holder(self):
        rig = PanelRig()
        rig.place(key(0), HOLDER, parts={"east": CRAFT_PART})
        rig.step(3)
        block, _item = rig.use(key(0), HOLDER, CENTER, "east")
        self.assertTrue(block.cancelled)
        self.assertEqual(rig.opened[0][1], CRAFT_SCREEN)


# -- 伪原版交互的服务端一半：扣物品、放置音效、再拦一道、扳手在底座上静默（2026-11-10）--------


class TestPlaceFeedback(unittest.TestCase):
    """放成功才播原版放置音效（`dig.stone`），在放下的那一格中心；放不上不播。"""

    def setUp(self):
        self.rig = PanelRig()
        self.sounds = []
        self.rig.panels.feedback = PlaceFeedback(
            play_sound=lambda *args: self.sounds.append(args) or True)
        self.rig.world.blocks[key(3)] = STONE
        self.rig.players.position = (3.5, 64.0, 2.5)

    def click(self, face):
        self.rig.players.item = COMMON_PART
        return self.rig.send(payload_for(key(3), STONE, ACTION_PLACE_PANEL, CENTER, face,
                                         item=COMMON_PART, clicked=face))

    def test_a_placed_panel_plays_the_vanilla_place_sound_once(self):
        self.assertEqual(self.click("east").reason, REASON_PANEL_PLACED)
        self.assertEqual(self.sounds, [(PLAYER, "dig.stone", (4.5, 64.5, 0.5), 1.0, 0.9)])

    def test_nothing_placed_nothing_heard(self):
        self.rig.world.blocks[key(4)] = STONE
        self.assertEqual(self.click("east").reason, REASON_NO_ROOM)
        self.rig.world.blocks.pop(key(4))
        self.rig.world.set_ok = False
        self.assertEqual(self.click("east").reason, REASON_WRITE_FAILED)
        self.assertEqual(self.sounds, [])

    def test_a_cable_added_to_a_holder_plays_it_too(self):
        rig = self.rig
        rig.place(key(0), HOLDER, parts={"north": COMMON_PART})
        rig.step(3)
        rig.players.item = GLASS
        result = rig.send(payload_for(key(0), HOLDER, ACTION_ADD_CABLE, CENTER, "north", item=GLASS))
        self.assertEqual(result.reason, REASON_CABLE_ADDED)
        self.assertEqual([sound[2] for sound in self.sounds], [(0.5, 64.5, 0.5)])

    def test_a_silent_engine_does_not_break_the_placement(self):
        self.rig.panels.feedback = PlaceFeedback(play_sound=lambda *args: 1 / 0)
        self.assertEqual(self.click("east").reason, REASON_PANEL_PLACED)
        self.assertEqual(self.rig.panels.feedback.stats["play_failures"], 1)


class TestServerGuard(unittest.TestCase):
    """以服务端为准再拦一道（`ServerItemUseOnEvent`）：手持光缆（不潜行）点面板底座；手持终端面板
    点任何方块（物品形态是展示方块，放不下）；点中会开界面的东西（`TestOpen`）。"""

    def setUp(self):
        rig = PanelRig()
        self.binder = rig.binder
        self.sneaking = set()
        self.binder._is_sneaking = lambda player_id: player_id in self.sneaking

    def use(self, block, item, player="p1"):
        event = ServerUse(key(0), block, None, item, player)
        self.binder._on_item_use_on(event)
        return event.cancelled

    def test_a_cable_item_on_a_holder_is_cancelled(self):
        for cable in (GLASS, CABLE, DENSE):
            self.assertTrue(self.use(HOLDER, cable), cable)
        self.assertEqual(self.binder.counts["item_use_on_add_cable"], 3)

    def test_sneaking_and_other_items_and_blocks_are_left_alone(self):
        self.sneaking.add("p2")
        self.assertFalse(self.use(HOLDER, GLASS, player="p2"), "潜行照原版放方块")
        self.assertFalse(self.use(HOLDER, "minecraft:stone"))
        self.assertFalse(self.use(HOLDER, ""))
        self.assertFalse(self.use(GLASS, GLASS), "手持光缆点光缆：原版照常（客户端按下判定另拦）")
        self.assertFalse(self.use(STONE, GLASS))

    def test_the_open_rule_comes_first(self):
        self.assertEqual(self.binder.use_guard.rules(), ["open", "add_cable", "panel_item"])
        self.assertEqual(self.binder.openers.names(), ["terminal", "panel"])
        self.assertTrue(self.use(COMMON_TERMINAL, GLASS))
        self.assertEqual(self.binder.counts["item_use_on_open"], 1)

    def test_a_panel_item_is_never_placed_as_a_block(self):
        """面板的物品形态是展示方块：对任何方块、潜不潜行都不放下（挂面板走客户端接管的意图）。"""
        self.sneaking.add("p2")
        for block in (STONE, GLASS, COMMON_TERMINAL):
            self.assertTrue(self.use(block, CRAFT_PART, player="p2"), block)
        self.assertEqual(self.binder.counts["item_use_on_panel_item"], 3)

    def test_sneaking_is_read_only_when_a_rule_matches(self):
        reads = []
        self.binder._is_sneaking = lambda player_id: reads.append(player_id) or False
        self.use(STONE, "minecraft:dirt")
        self.use(GLASS, GLASS)
        self.assertEqual(reads, [])
        self.use(HOLDER, GLASS)
        self.assertEqual(reads, ["p1"])


class TestWrenchOnAHolder(unittest.TestCase):
    """扳手不潜行点面板底座（切断）：底座没有线芯、不连任何东西，什么都不做，静默。"""

    def test_cutting_on_a_holder_is_silent(self):
        rig = PanelRig()
        rig.place(key(0), HOLDER, parts={"east": CRAFT_PART})
        rig.step(3)
        rig.players.item = WRENCH_ID
        for target, face in ((CENTER, "east"), ((px(15), 0.5, 0.5), "east"), ((0.5, 0.5, 0.0), "north")):
            result = rig.send(payload_for(key(0), HOLDER, ACTION_CUT, target, face))
            self.assertEqual((result.ok, result.reason, result.message),
                             (False, REASON_HOLDER_HAS_NO_LINKS, ""), (target, face))
        self.assertEqual(rig.parts(key(0)), {"east": CRAFT_PART})

    def test_cutting_a_cable_still_talks(self):
        rig = PanelRig()
        rig.place(key(0), GLASS, parts={"east": CRAFT_PART})
        rig.step(3)
        rig.players.item = WRENCH_ID
        result = rig.send(payload_for(key(0), GLASS, ACTION_CUT, (px(15), 0.5, 0.5), "east"))
        self.assertTrue(result.message, "光缆上点中面板照旧提示")
        missed = rig.send(payload_for(key(0), GLASS, ACTION_CUT, (0.05, 0.95, 0.5), "north"))
        self.assertEqual((missed.reason, missed.message), (REASON_MISSED, "没有点中光缆"))


# -- 打开面板 ------------------------------------------------------------------


class TestOpen(unittest.TestCase):
    """右键面板开界面：与整方块终端同一个入口（`server/interaction/opening.py`）——同一套潜行放行、
    取消原版、限速与推屏；手持任何物品都开，原版那次放方块一并取消（2026-11-10 真机反馈）。"""

    def setUp(self):
        self.rig = PanelRig()
        self.rig.place(key(0), GLASS, parts={"east": CRAFT_PART, "north": TEMPLATE_PART})
        self.rig.step(3)
        self.host = self.rig.registry.get(key(0))

    def test_opening_a_panel_pushes_its_terminal_screen(self):
        rig = self.rig
        block, _item = rig.use(key(0), GLASS, CENTER, "east")
        self.assertTrue(block.cancelled)
        ((player, ui_key, params),) = rig.opened
        self.assertEqual((player, ui_key), (PLAYER, CRAFT_SCREEN))
        self.assertEqual(params["dimension"], 0)
        ref = DeviceRef.from_dict(json.loads(json.dumps(params["device_ref"])))
        self.assertEqual(ref, DeviceRef(key(0), self.host.generation, "east"))
        self.assertEqual(rig.binder.counts["block_use_opened"], 1)

    def test_a_held_block_opens_the_panel_and_is_not_placed(self):
        """手持方块右键面板：照样开界面，原版那次放方块取消——与右键整方块终端一样。"""
        block, item = self.rig.use(key(0), GLASS, CENTER, "east", item=STONE)
        self.assertTrue(block.cancelled)
        self.assertTrue(item.cancelled, "不取消的话手里的方块会被放下")
        self.assertEqual(self.rig.opened[0][1], CRAFT_SCREEN)
        self.assertEqual(self.rig.binder.counts["item_use_on_open"], 1)

    def test_every_kind_opens_its_whole_block_terminal_screen(self):
        definitions = DefinitionSet()
        for face, part_id in zip(("up", "down", "south", "west"), PANEL_PART_IDS):
            rig = PanelRig()
            rig.place(key(0), GLASS, parts={face: part_id})
            rig.step(2)
            rig.use(key(0), GLASS, CENTER, face)
            self.assertEqual(rig.opened[0][1], definitions.terminal_screen_of(
                definitions.get(part_id).whole_block_id), part_id)

    def test_missing_the_panels_leaves_the_vanilla_interaction_alone(self):
        """点中的是线芯：不认领，贴着光缆放方块照常。"""
        block, item = self.rig.use(key(0), GLASS, CENTER, "west", item=STONE)
        self.assertFalse(block.cancelled or item.cancelled)
        self.assertEqual(self.rig.opened, [])

    def test_sneaking_leaves_the_vanilla_interaction_alone(self):
        self.rig.players.sneaking = True
        block, item = self.rig.use(key(0), GLASS, CENTER, "east", item=STONE)
        self.assertFalse(block.cancelled or item.cancelled)
        self.assertEqual(self.rig.opened, [])

    def test_the_same_limiter_as_whole_block_terminals(self):
        rig = self.rig
        rig.use(key(0), GLASS, CENTER, "east")
        again, _item = rig.use(key(0), GLASS, CENTER, "north")
        self.assertTrue(again.cancelled)
        self.assertEqual(len(rig.opened), 1)
        self.assertEqual(rig.binder.counts["block_use_limited"], 1)

    def test_an_unreadable_eye_is_not_claimed(self):
        block, item = self.rig.use(key(0), GLASS, CENTER, "east", item=STONE, eye=False)
        self.assertFalse(block.cancelled or item.cancelled)
        self.assertEqual(self.rig.opened, [])

    def test_a_dense_cable_is_never_asked(self):
        rig = PanelRig()
        rig.place(key(0), DENSE)
        rig.step(2)
        block, item = rig.use(key(0), DENSE, CENTER, "east", item=STONE)
        self.assertFalse(block.cancelled or item.cancelled)


# -- 外观 ----------------------------------------------------------------------


class TestAppearance(unittest.TestCase):
    def setUp(self):
        self.rig = PanelRig()
        self.sent = []
        self.tracker = SlotDisplayTracker(
            self.rig.definitions, drivers=_NoStates(), memory_slots=_NoStates(),
            publish=self.sent.append, panels=self.rig.panels.display_values,
        )

    def values(self, **faces):
        by_face = dict((face, EMPTY) for face in FACING_NAMES)
        for face, part_id in faces.items():
            by_face[face] = FIRST_MODEL + PANEL_PART_IDS.index(part_id)
        return tuple(by_face[face] for face in FACING_NAMES)

    def test_values_follow_the_parts_and_the_kind_table(self):
        rig = self.rig
        rig.place(key(0), GLASS, parts={"east": CRAFT_PART, "up": COMMON_PART})
        rig.place(key(1, y=70), DENSE)
        rig.place(key(2, y=70), BB8)
        self.assertEqual(rig.panels.display_values(key(0)),
                         self.values(east=CRAFT_PART, up=COMMON_PART))
        self.assertIsNone(rig.panels.display_values(key(1, y=70)), "致密光缆不显示面板")
        self.assertIsNone(rig.panels.display_values(key(2, y=70)))
        self.assertIsNone(rig.panels.display_values(key(9)))

    def test_requests_are_answered_only_for_cables_with_panels(self):
        rig = self.rig
        rig.place(key(0), GLASS, parts={"south": FACTORY_PART})
        rig.place(key(1), CABLE)
        payload = self.tracker.answer(key(0))
        node_key, facing, slots = parse_update(payload)
        self.assertEqual((node_key, facing, slots), (key(0), "", self.values(south=FACTORY_PART)))
        self.assertIsNone(self.tracker.answer(key(1)), "一面都没挂：外观初值就是全空")
        self.assertEqual(self.tracker.stats["empty_panels"], 1)
        self.assertIsNone(self.tracker.answer(key(5)))
        self.assertEqual(self.tracker.stats["unknown_requests"], 1)

    def test_marked_cells_are_broadcast_only_when_they_changed(self):
        rig = self.rig
        rig.place(key(0), GLASS)
        self.tracker.mark_panels(key(0))
        self.assertEqual(self.tracker.advance(), 0, "从没挂过、现在也没有：不广播")
        rig.keeper.write_faces(key(0), parts={"up": CRAFT_PART})
        self.tracker.mark_panels(key(0))
        self.assertEqual(self.tracker.advance(), 1)
        self.assertEqual(parse_update(self.sent[-1])[2], self.values(up=CRAFT_PART))
        self.tracker.mark_panels(key(0))
        self.assertEqual(self.tracker.advance(), 0, "没变不重发")
        rig.keeper.write_faces(key(0), parts={})
        self.tracker.mark_panels(key(0))
        self.assertEqual(self.tracker.advance(), 1)
        self.assertEqual(parse_update(self.sent[-1])[2], self.values())
        self.assertEqual(self.tracker.diagnostics()["panel_cables"], 0, "全空的不记")

    def test_an_answered_reading_is_cleared_later(self):
        """区块加载时就挂着面板（只答过、没广播过），后来全拆了：照样广播一次全空。"""
        rig = self.rig
        rig.place(key(0), GLASS, parts={"up": CRAFT_PART})
        self.assertIsNotNone(self.tracker.answer(key(0)))
        rig.keeper.write_faces(key(0), parts={})
        self.tracker.mark_panels(key(0))
        self.assertEqual(self.tracker.advance(), 1)
        self.assertEqual(parse_update(self.sent[-1])[2], self.values())

    def test_a_removed_cable_is_forgotten(self):
        rig = self.rig
        rig.place(key(0), GLASS, parts={"up": CRAFT_PART})
        self.tracker.answer(key(0))
        rig.remove(key(0), GLASS)
        self.tracker.mark_panels(key(0))
        self.assertEqual(self.tracker.advance(), 0)
        self.assertEqual(self.tracker.diagnostics()["panel_cables"], 0)

    def test_the_client_writes_the_six_face_variables(self):
        events = _SlotEvents()
        bus = FakeBus()
        writes = []
        client = SlotDisplayClient(bus, events, "loaded", "removed",
                                   lambda pos, name, value: writes.append((pos, name, value)) or True)
        client.start()
        client.on_loaded(_Entity(GLASS, 3, 64, 1))
        client.on_loaded(_Entity(DENSE, 4, 64, 1))
        self.assertEqual(events.to_server, [request_payload(NodeKey(0, 3, 64, 1))])
        self.rig.place(NodeKey(0, 3, 64, 1), GLASS, parts={"down": TEMPLATE_PART})
        payload = self.tracker.answer(NodeKey(0, 3, 64, 1))
        self.assertTrue(client.receive(client.update.unmarshal(payload)))
        written = dict((name, value) for _pos, name, value in writes)
        self.assertNotIn(FACING_VARIABLE, written, "面板不转")
        self.assertEqual(sorted(written), ["variable.p%d" % index for index in range(6)])
        self.assertEqual(written["variable.p1"], float(FIRST_MODEL + PANEL_PART_IDS.index(TEMPLATE_PART)))
        self.assertEqual(written["variable.p0"], 0.0)


class _NoStates(object):
    def states(self):
        return ()

    def get(self, node_key):
        return None


class _Entity(object):
    def __init__(self, block, x, y, z, dimension=0):
        self.blockName = block
        self.posX, self.posY, self.posZ = x, y, z
        self.dimensionId = dimension


class _SlotEvents(object):
    def __init__(self):
        self.to_server = []
        events = self

        class CustomC2SEvent(object):
            def send(self):
                events.to_server.append(self.marshal())

        class CustomS2CEvent(object):
            def send(self, client_id):
                pass

            def sendAll(self):
                pass

        self.CustomC2SEvent = CustomC2SEvent
        self.CustomS2CEvent = CustomS2CEvent


# -- 挖整格 --------------------------------------------------------------------


class TestDigging(unittest.TestCase):
    def test_digging_reads_every_panel_on_the_same_tick(self):
        rig = PanelRig()
        rig.place(key(0), GLASS, parts={"up": COMMON_PART, "east": CRAFT_PART})
        host = rig.registry.get(key(0))
        rig.changes = []
        rig.remove(key(0), GLASS)
        kinds = [(c.kind, c.detail, c.device_id) for c in rig.changes
                 if c.kind in (ChangeKind.REMOVED, ChangeKind.PART_DETACHED)]
        self.assertEqual(kinds, [
            (ChangeKind.REMOVED, "", GLASS),
            (ChangeKind.PART_DETACHED, "up", COMMON_PART),
            (ChangeKind.PART_DETACHED, "east", CRAFT_PART),
        ])
        detached = [c for c in rig.changes if c.kind == ChangeKind.PART_DETACHED]
        self.assertTrue(all(c.ref == host.ref for c in detached))
        for change in detached:
            self.assertTrue(rig.panels.drop_if_orphaned(change))
        self.assertEqual(rig.world.dropped, [
            ((0, 0, 64, 0), [(COMMON_PART, 1)]),
            ((0, 0, 64, 0), [(CRAFT_PART, 1)]),
        ])

    def test_wrench_paths_do_not_drop(self):
        """扳手的三条路：拆一块（宿主还在）、只拆光缆（面板不离开）、拆底座最后一块（底座随之
        消失，面板先摘掉）——都不在地上掉东西，补进来的移除事件也不再发第二遍离开。"""
        rig = PanelRig()
        rig.place(key(0), GLASS, parts={"up": COMMON_PART, "east": CRAFT_PART})
        rig.step(3)
        rig.players.item = WRENCH_ID
        rig.players.position = (0.5, 64.0, 1.5)
        rig.act(key(0), GLASS, ACTION_DISMANTLE, CENTER, "up")
        rig.step(2)
        rig.act(key(0), GLASS, ACTION_DISMANTLE, CENTER, "west")
        self.assertEqual(rig.registry.get(key(0)).device_id, HOLDER)
        rig.step(2)
        rig.act(key(0), HOLDER, ACTION_DISMANTLE, CENTER, "east")
        for change in [c for c in rig.changes if c.kind == ChangeKind.PART_DETACHED]:
            self.assertFalse(rig.panels.drop_if_orphaned(change), "宿主还在：物品已经给了玩家")
        for event in rig.pushed:
            rig.lifecycle.handle(event)
        self.assertIsNone(rig.registry.get(key(0)))
        self.assertEqual(rig.world.dropped, [])
        self.assertEqual(len([c for c in rig.changes if c.kind == ChangeKind.PART_DETACHED]), 2,
                         "面板先摘掉了，补进来的移除事件不再发第二遍")
        self.assertEqual(rig.world.given, [(PLAYER, [COMMON_PART]), (PLAYER, [GLASS]),
                                           (PLAYER, [CRAFT_PART])])


# -- 整条链路（ServerBootstrap）----------------------------------------------------


class ServerRig(object):
    """基带 + 控制器 + 创造能量源 + 一格挂着面板的光缆，经 `ServerBootstrap` 装配。"""

    def __init__(self, world=None, persistence=None, controller=TINY, start=True):
        self.world = world if world is not None else PanelWorld()
        self.backend = persistence
        self.controller = controller
        self.positions = {}
        self.dimensions = {}
        self.opened = []
        self.notes = []
        self.slot_sent = []
        self.events = testsupport.FakeEventsModule()
        self.server = None
        if start:
            self.start()

    def start(self):
        from endernet.server.bootstrap import ServerBootstrap

        kwargs = dict(
            world=self.world,
            sync_events=self.events,
            player_dimension=lambda player_id: self.dimensions.get(player_id, 0),
            player_position=lambda player_id: self.positions.get(player_id, (1.5, 64.0, 2.5)),
            energy_policy=EnergyPolicy.dev_stand_in(),
            session_check=lambda source: True,
            on_slot_display=self.slot_sent.append,
        )
        if self.backend is not None:
            kwargs["persistence_backend"] = self.backend
        self.server = ServerBootstrap(**kwargs)
        self.server.start(bind_engine=False)
        port = self.server.cable_intent_port
        port.held_item = lambda player_id: self.item
        port.creative = lambda player_id: False
        port.take_held = lambda player_id, item_id: True
        port.notify = lambda player_id, text: self.notes.append(text)
        self.server.event_binder._notify_ui = lambda *args: self.opened.append(args)
        self.server.event_binder._is_sneaking = lambda player_id: False
        self.sounds = []
        self.server.place_feedback.play_sound = lambda *args: self.sounds.append(args) or True
        self.item = ""
        return self.server

    def stop(self):
        self.server.stop()

    def place(self, node_key, device_id, parts=None, fresh=True):
        if fresh and self.server.definitions.has_face_data(device_id):
            self.world.put_cable(node_key, parts=parts)
        self.world.blocks[node_key] = device_id
        self.server.push(NormalizedEvent.place(node_key, device_id, source="entity_place_after"))

    def remove(self, node_key, device_id):
        self.world.states.pop(node_key, None)
        self.world.blocks.pop(node_key, None)
        self.server.push(NormalizedEvent.remove(node_key, device_id, source="block_remove"))

    def settle(self, frames=6):
        for _ in range(frames):
            self.server.drive_once()
        assert self.server.tick_errors == [], self.server.tick_errors

    def network(self, host=key(1), parts=None, fresh=True):
        """BB8 (0,64,0) ← 控制器在下、能量源在上；光缆在它东边。"""
        self.place(key(0), BB8, fresh=fresh)
        self.place(key(0, y=63), self.controller, fresh=fresh)
        self.place(key(0, y=65), ENERGY, fresh=fresh)
        self.place(host, GLASS, parts=parts, fresh=fresh)
        self.settle()

    def ref(self, face, host=key(1)):
        return part_ref(self.server.registry.get(host), face)

    def status(self, ref):
        return self.server.runtimes.device_status(ref)

    def open(self, ref, player="p1"):
        reply = self.server.sync.handle(
            SyncEnvelope(SessionOpen("r-" + player, ref, 0), view_kind=VIEW_ITEM), player)
        return reply

    def intent(self, node_key, block, action, target, face, item, clicked=""):
        self.item = item
        return self.server.cable_intents.handle(
            PLAYER, payload_for(node_key, block, action, target, face, item=item, clicked=clicked))


class TestServerPanels(unittest.TestCase):
    def setUp(self):
        self.rig = ServerRig()
        self.rig.network(parts={"up": CRAFT_PART, "north": COMMON_PART})

    def tearDown(self):
        self.rig.stop()

    def test_a_panel_is_online_like_its_terminal(self):
        status = self.rig.status(self.rig.ref("up"))
        self.assertTrue(status.is_online, status.reasons)
        self.assertEqual(status.device_id, CRAFT_PART)
        self.assertEqual(status.node_key, PartKey(key(1), "up"))
        self.assertEqual((status.bandwidth_ok, status.power_ok), (True, True))
        self.assertEqual(status.bandwidth_domain_id,
                         self.rig.server.topology.snapshot().line_of(key(1)))

    def test_each_panel_draws_its_own_energy(self):
        server = self.rig.server
        energy = server.runtimes.runtime_of(key(1)).services.get(ServiceId.ENERGY)
        rows = [row for row in energy.member_view() if row["device_id"] in (CRAFT_PART, COMMON_PART)]
        self.assertEqual(sorted((row["device_id"], row["demand"]) for row in rows),
                         [(COMMON_PART, 4), (CRAFT_PART, 4)])
        before = energy.totals()["demand_total"]
        server.cables.write_faces(key(1), parts={"up": CRAFT_PART})
        server.lifecycle.detach_part(server.registry.get(key(1)), "north", COMMON_PART)
        self.rig.settle()
        self.assertEqual(energy.totals()["demand_total"], before - 4)
        self.assertIsNone(energy.power_ok(PartKey(key(1), "north")), "拆下的面板不再记账")
        self.assertTrue(energy.power_ok(PartKey(key(1), "up")))

    def test_a_session_opens_on_a_panel(self):
        reply = self.rig.open(self.rig.ref("up"))
        self.assertEqual(reply.message.kind, MSG_SESSION_READY)
        session = self.rig.server.sync.registry.get(reply.session_id)
        self.assertEqual(session.device_ref, self.rig.ref("up"))
        self.assertEqual(session.anchor_position, key(1).as_tuple(), "距离按宿主那一格")

    def test_the_status_bar_of_a_panel_session(self):
        """面板的会话照整方块终端收状态包：设备是那块面板，在线；宿主光缆被拆就关掉。"""
        rig = self.rig
        reply = rig.open(rig.ref("up"))
        before = len(rig.events.sent)
        rig.server.tick()
        statuses = [payload for _player, payload in rig.events.sent[before:]
                    if payload["message"]["kind"] == MSG_DEVICE_STATUS]
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["session_id"], reply.session_id)
        self.assertEqual(statuses[0]["message"]["device_id"], CRAFT_PART)
        self.assertEqual(statuses[0]["message"]["state_text"], "在线")

    def test_distance_and_dimension_are_checked_like_a_terminal(self):
        rig = self.rig
        rig.positions["p1"] = (30.0, 64.0, 0.0)
        reply = rig.open(rig.ref("up"))
        self.assertEqual((reply.message.kind, reply.message.code), (MSG_ERROR, Reason.OUT_OF_RANGE))
        rig.positions["p1"] = (1.5, 64.0, 2.5)
        rig.dimensions["p1"] = 1
        reply = rig.open(rig.ref("up"))
        self.assertEqual(reply.message.code, Reason.SESSION_INVALID)
        self.assertEqual(rig.server.sync.registry.count(), 0)

    def test_a_face_without_a_panel_or_a_stale_host_is_refused(self):
        rig = self.rig
        reply = rig.open(DeviceRef(key(1), rig.ref("up").generation, "south"))
        self.assertEqual(reply.message.code, Reason.DEVICE_GONE)
        stale = DeviceRef(key(1), rig.ref("up").generation + 1, "up")
        self.assertEqual(rig.open(stale).message.code, Reason.STALE_INSTANCE)

    def test_detaching_closes_only_that_panels_session(self):
        rig = self.rig
        up = rig.open(rig.ref("up"), player="p1").session_id
        north = rig.open(rig.ref("north"), player="p2").session_id
        self.assertTrue(up and north)
        rig.intent(key(1), GLASS, ACTION_DISMANTLE, CENTER, "up", WRENCH_ID)
        registry = rig.server.sync.registry
        self.assertIsNone(registry.get(up))
        self.assertIsNotNone(registry.get(north), "同一格别的面板不受影响")
        self.assertEqual(rig.server.sync.stats["gone_notices"], 1, "服务端单方面关的要告诉客户端")
        rig.settle()
        self.assertIsNotNone(registry.get(north))

    def test_online_follows_the_line_admission(self):
        """BB32 → K1（玻璃 8u，4 块）→ K2（5 块）：K2 最后一块被挤掉，它的命令照整方块终端
        一样被带宽挡下；K1 拆一块，它就上线、命令也放行。"""
        rig = ServerRig()
        try:
            rig.place(key(0), BB32)
            rig.place(key(0, y=63), TINY)
            rig.place(key(0, y=65), ENERGY)
            rig.place(key(1), GLASS, parts=dict((f, COMMON_PART) for f in ("up", "down", "north", "south")))
            parts = dict((f, COMMON_PART) for f in ("up", "down", "north", "south"))
            parts["east"] = TEMPLATE_PART
            rig.place(key(2), GLASS, parts=parts)
            rig.settle(8)
            last = rig.ref("east", host=key(2))
            status = rig.status(last)
            self.assertFalse(status.is_online)
            self.assertIn(Reason.BANDWIDTH_LINE_FULL, status.reason_codes())
            self.assertTrue(rig.status(rig.ref("up", host=key(2))).is_online)
            server = rig.server
            state = server.templates.state_for(last)

            def switch(request_id):
                return server.submit_command(DeviceCommand(
                    request_id, CommandKind.STORAGE, CommandSource("p1", last, 0),
                    payload={"action": "template_mode", "target": PATTERN_MODE_FACTORY,
                             "slot_version": state.revision},
                    device_ref=last,
                ))

            reply = switch("c1")
            self.assertFalse(reply.is_applied())
            self.assertIn(Reason.BANDWIDTH_LINE_FULL, [reason.code for reason in reply.reasons])
            server.cables.write_faces(key(1), parts=dict((f, COMMON_PART) for f in ("up", "down", "north")))
            server.lifecycle.detach_part(server.registry.get(key(1)), "south", COMMON_PART)
            rig.settle(8)
            self.assertTrue(rig.status(last).is_online, rig.status(last).reasons)
            self.assertTrue(switch("c2").is_applied())
            self.assertEqual(state.config.mode, PATTERN_MODE_FACTORY)
        finally:
            rig.stop()

    def test_the_controller_scope_follows_the_whole_block_terminal(self):
        """DOS 控制器认通用 / 合成终端，不认样板终端：面板形态同一口径。"""
        rig = ServerRig(controller=DOS)
        try:
            rig.network(parts={"up": CRAFT_PART, "north": TEMPLATE_PART})
            self.assertTrue(rig.status(rig.ref("up")).is_online)
            status = rig.status(rig.ref("north"))
            self.assertFalse(status.is_online)
            self.assertIn(Reason.CONTROLLER_DOES_NOT_SERVE, status.reason_codes())
        finally:
            rig.stop()

    def test_terminal_state_is_kept_per_panel(self):
        server = self.rig.server
        server.cables.write_faces(key(1), parts={"up": CRAFT_PART, "north": CRAFT_PART,
                                                 "south": TEMPLATE_PART, "east": COMMON_PART})
        self.rig.settle()
        up, north = self.rig.ref("up"), self.rig.ref("north")
        grid_up, grid_north = server.workbench.grid_for(up), server.workbench.grid_for(north)
        self.assertIsNotNone(grid_up)
        self.assertIsNot(grid_up, grid_north, "同一格两块合成面板各有九宫格")
        self.assertIsNone(server.workbench.grid_for(self.rig.ref("east")), "通用面板没有九宫格")
        self.assertIsNone(server.workbench.grid_for(self.rig.ref("south")))
        self.assertIsNotNone(server.templates.state_for(self.rig.ref("south")))
        self.assertIsNone(server.workbench.grid_for(DeviceRef(key(1), up.generation, "down")))
        self.assertIsNone(server.workbench.grid_for(up.host()), "光缆本身不是终端")

    def test_commands_on_a_panel_go_through_the_same_admission(self):
        server = self.rig.server
        server.cables.write_faces(key(1), parts={"up": TEMPLATE_PART})
        self.rig.settle()
        ref = self.rig.ref("up")
        state = server.templates.state_for(ref)
        command = DeviceCommand(
            "c1", CommandKind.STORAGE, CommandSource("p1", ref, 0),
            payload={"action": "template_mode", "target": PATTERN_MODE_FACTORY,
                     "slot_version": state.revision},
            device_ref=ref,
        )
        reply = server.submit_command(command)
        self.assertTrue(reply.is_applied(), reply.to_dict())
        self.assertEqual(state.config.mode, PATTERN_MODE_FACTORY)

    def test_placing_and_opening_through_the_bootstrap(self):
        rig = self.rig
        result = rig.intent(key(1), GLASS, ACTION_PLACE_PANEL, CENTER, "east", TEMPLATE_PART)
        self.assertEqual(result.reason, REASON_PANEL_PLACED)
        rig.settle()
        self.assertEqual(rig.world.sides(key(1))["east"], 2)
        payload = [p for p in rig.slot_sent if tuple(p["pos"]) == key(1).as_tuple()][-1]
        self.assertEqual(parse_update(payload)[2][FACING_NAMES.index("east")],
                         FIRST_MODEL + PANEL_PART_IDS.index(TEMPLATE_PART))
        block, item = right_click(rig.server.event_binder, key(1), GLASS, CENTER, "east",
                                  item=STONE)
        self.assertTrue(block.cancelled and item.cancelled)
        self.assertEqual(rig.opened[-1][1], "endernet_template_terminal_panel.main")
        self.assertEqual(rig.server.diagnostics()["panels"]["placed"], 1)
        self.assertEqual(rig.server.event_binder.counts["block_use_opened"], 1)

    def test_digging_the_cable_drops_every_panel_and_closes_their_sessions(self):
        rig = self.rig
        up = rig.open(rig.ref("up")).session_id
        rig.remove(key(1), GLASS)
        rig.settle()
        self.assertIsNone(rig.server.sync.registry.get(up))
        self.assertEqual(sorted(rig.world.dropped), sorted([
            ((0, 1, 64, 0), [(CRAFT_PART, 1)]),
            ((0, 1, 64, 0), [(COMMON_PART, 1)]),
        ]))
        self.assertEqual(rig.server.diagnostics()["panels"]["dropped"], 2)


class TestHolderThroughTheServer(unittest.TestCase):
    """面板底座经 `ServerBootstrap`：不入网的原因、先面板后光缆、扳手只拆光缆。"""

    def setUp(self):
        self.rig = ServerRig()
        self.rig.network(parts={"up": CRAFT_PART, "north": COMMON_PART})

    def tearDown(self):
        self.rig.stop()

    def assert_not_on_a_cable(self, ref):
        status = self.rig.status(ref)
        self.assertFalse(status.is_online)
        self.assertEqual([(r.code, r.detail) for r in status.reasons],
                         [(Reason.PART_NOT_ON_CABLE, "面板没有接在光缆上")], "只报这一条")
        return status

    def test_a_panel_on_a_holder_says_it_is_not_on_a_cable(self):
        rig = self.rig
        rig.place(key(3), HOLDER, parts={"north": COMMON_PART})
        rig.settle()
        status = self.assert_not_on_a_cable(rig.ref("north", host=key(3)))
        self.assertEqual(status.device_id, COMMON_PART)
        self.assertEqual(rig.world.sides(key(3)), sides(north=2))
        # 打开它的界面：会话按同一句拒绝，状态栏显示的就是这一句（与没接网的整方块终端一样开不成）
        reply = rig.open(rig.ref("north", host=key(3)))
        self.assertEqual((reply.message.kind, reply.message.code),
                         (MSG_ERROR, Reason.PART_NOT_ON_CABLE))

    def test_panel_first_cable_later_keeps_the_terminal_state(self):
        rig = self.rig
        rig.place(key(2), HOLDER, parts={"north": TEMPLATE_PART})
        rig.settle()
        ref = rig.ref("north", host=key(2))
        state = rig.server.templates.state_for(ref)
        self.assertIsNotNone(state)
        result = rig.intent(key(2), HOLDER, ACTION_ADD_CABLE, CENTER, "north", GLASS)
        self.assertEqual((result.reason, result.message), (REASON_CABLE_ADDED, ""))
        rig.settle(8)
        self.assertEqual(rig.ref("north", host=key(2)), ref, "面板引用不变")
        self.assertIs(rig.server.templates.state_for(ref), state, "终端状态没丢")
        self.assertTrue(rig.status(ref).is_online, rig.status(ref).reasons)
        self.assertEqual(rig.world.sides(key(2))["west"], 1)
        self.assertEqual(rig.open(ref).message.kind, MSG_SESSION_READY)

    def test_the_cable_can_go_out_and_back_in_under_an_open_session(self):
        rig = self.rig
        up = rig.ref("up")
        reply = rig.open(up)
        state = rig.server.workbench.grid_for(up)
        rig.intent(key(1), GLASS, ACTION_DISMANTLE, CENTER, "west", WRENCH_ID)
        rig.settle(4)
        self.assert_not_on_a_cable(up)
        result = rig.intent(key(1), HOLDER, ACTION_ADD_CABLE, CENTER, "west", CABLE)
        self.assertEqual(result.reason, REASON_CABLE_ADDED)
        rig.settle(8)
        self.assertEqual(rig.server.registry.get(key(1)).device_id, CABLE)
        self.assertIsNotNone(rig.server.sync.registry.get(reply.session_id), "会话没丢")
        self.assertIs(rig.server.workbench.grid_for(up), state)
        self.assertTrue(rig.status(up).is_online, rig.status(up).reasons)

    def test_stripping_the_cable_keeps_the_panels_and_their_sessions(self):
        rig = self.rig
        up = rig.ref("up")
        reply = rig.open(up)
        grid = rig.server.workbench.grid_for(up)
        grid.set_slot(0, IRON, 3)
        result = rig.intent(key(1), GLASS, ACTION_DISMANTLE, CENTER, "west", WRENCH_ID)
        self.assertEqual(result.reason, REASON_CABLE_REMOVED)
        self.assertEqual(rig.server.registry.get(key(1)).device_id, HOLDER)
        rig.settle(8)
        self.assertEqual(rig.ref("up"), up)
        self.assertIsNotNone(rig.server.sync.registry.get(reply.session_id))
        self.assertIs(rig.server.workbench.grid_for(up), grid)
        self.assertEqual(len(rig.server.workbench.orphans()), 0, "九宫格没有进孤儿")
        self.assert_not_on_a_cable(up)
        self.assertEqual(rig.world.sides(key(1)), sides(up=2, north=2))
        self.assertEqual(rig.world.dropped, [])

    def test_placing_against_a_block_through_the_bootstrap(self):
        rig = self.rig
        result = rig.intent(key(0), BB8, ACTION_PLACE_PANEL, CENTER, "north", COMMON_PART,
                            clicked="north")
        self.assertEqual((result.reason, result.message), (REASON_PANEL_PLACED, ""))
        rig.settle()
        holder = rig.server.registry.get(key(0, z=-1))
        self.assertEqual((holder.device_id, holder.config[CABLE_PARTS_KEY]),
                         (HOLDER, {"south": COMMON_PART}))
        self.assertEqual(rig.world.sides(key(0, z=-1)), sides(south=2))
        self.assertEqual([sound[1:3] for sound in rig.sounds], [("dig.stone", (0.5, 64.5, -0.5))],
                         "放成功播一次原版放置音效")
        self.assertEqual(rig.server.event_binder.use_guard.rules(),
                         ["open", "add_cable", "panel_item"])
        self.assertEqual(rig.server.event_binder.openers.names(), ["terminal", "panel"])
        payload = rig.server.slot_displays.answer(key(0, z=-1))
        # 底座上的面板不入网，外观是离线屏幕（2026-11-13）
        self.assertEqual(parse_update(payload)[2][FACING_NAMES.index("south")],
                         FIRST_MODEL + PANEL_PART_IDS.index(COMMON_PART) + OFFLINE_OFFSET)
        self.assert_not_on_a_cable(rig.ref("south", host=key(0, z=-1)))

    def test_digging_a_holder_drops_its_panels_and_not_itself(self):
        rig = self.rig
        rig.place(key(3), HOLDER, parts={"north": COMMON_PART, "up": CRAFT_PART})
        rig.settle()
        rig.remove(key(3), HOLDER)
        rig.settle()
        self.assertEqual(sorted(rig.world.dropped), sorted([
            ((0, 3, 64, 0), [(COMMON_PART, 1)]),
            ((0, 3, 64, 0), [(CRAFT_PART, 1)]),
        ]))

    def test_the_panel_kind_hears_when_its_front_changes(self):
        rig = self.rig
        panels = rig.server.panels
        heard = []
        panels.register_kind(PART_KIND_TERMINAL, screen_of=panels._terminal_screen,
                             on_front_changed=lambda *args: heard.append(args))
        rig.server.push(NormalizedEvent.neighbor_changed(key(1), key(1, y=65), to_device_id=STONE))
        rig.settle()
        self.assertEqual(heard, [(PartKey(key(1), "up"), CRAFT_PART, key(1, y=65))])
        self.assertEqual(rig.server.diagnostics()["panels"]["front_changes"], 1)


# -- 面板骨架 --------------------------------------------------------------------


class TestPanelFramework(unittest.TestCase):
    """给以后的面板种类留的口子：种类、正面所对的格子、每块面板自己的配置。"""

    def test_every_panel_has_a_kind(self):
        definitions = DefinitionSet()
        for part_id in definitions.parts:
            expected = PART_KIND_TUNNEL if part_id == TUNNEL_PART_ID else PART_KIND_TERMINAL
            self.assertEqual(definitions.part_kind_of(part_id), expected, part_id)
        self.assertIsNone(definitions.part_kind_of(GLASS))
        with self.assertRaises(ValueError):
            DeviceDefinition("endernet:x_part", "x", CATEGORY_TERMINAL, form=FORM_PART,
                             whole_block_id=COMMON_TERMINAL)
        with self.assertRaises(ValueError):
            # 终端面板必须指明整方块终端；没有整方块对应物的只有隧道
            DeviceDefinition("endernet:x_part", "x", CATEGORY_TERMINAL, form=FORM_PART,
                             part_kind=PART_KIND_TERMINAL)
        with self.assertRaises(ValueError):
            DeviceDefinition("endernet:x", "x", CATEGORY_TERMINAL, whole_block_id=COMMON_TERMINAL)
        with self.assertRaises(ValueError):
            DeviceDefinition("endernet:x_part", "x", CATEGORY_TERMINAL, form=FORM_PART,
                             whole_block_id=COMMON_TERMINAL, part_kind="bus")
        with self.assertRaises(ValueError):
            DeviceDefinition("endernet:x", "x", CATEGORY_TERMINAL, part_kind=PART_KIND_TERMINAL)

    def test_the_holder_is_a_part_host_that_joins_nothing(self):
        from endernet.server.devices.catalog import DeviceCatalog

        definitions = DefinitionSet()
        self.assertTrue(definitions.is_part_holder(HOLDER))
        self.assertTrue(definitions.accepts_parts(HOLDER))
        self.assertTrue(definitions.has_face_data(HOLDER))
        self.assertFalse(definitions.is_cable(HOLDER))
        self.assertFalse(definitions.accepts_parts(DENSE))
        catalog = DeviceCatalog(definitions)
        for other in (GLASS, CABLE, DENSE, BB8, TINY, COMMON_TERMINAL, HOLDER):
            self.assertFalse(catalog.can_connect(HOLDER, other).allowed, other)

    def test_the_front_is_the_cell_the_panel_faces(self):
        for face in FACING_NAMES:
            self.assertEqual(PartKey(key(0), face).front_key(), key(0).neighbor(face))
            self.assertEqual(PanelService.front_of(PartKey(key(0), face)), key(0).neighbor(face))
        self.assertIsNone(PartKey(key(0)).front_key())

    def test_each_panel_keeps_its_own_config_and_a_new_one_starts_empty(self):
        rig = PanelRig()
        rig.place(key(0), GLASS, parts={"up": COMMON_PART})
        rig.step(2)
        panels = rig.panels
        self.assertTrue(panels.set_part_config(PartKey(key(0), "up"), {"mode": 2}))
        self.assertFalse(panels.set_part_config(PartKey(key(0), "down"), {"mode": 1}), "那一面没有面板")
        self.assertEqual(panels.part_config(PartKey(key(0), "up")), {"mode": 2})
        rig.players.item = WRENCH_ID
        rig.act(key(0), GLASS, ACTION_DISMANTLE, CENTER, "up")
        rig.step(2)
        rig.act(key(0), GLASS, ACTION_PLACE_PANEL, CENTER, "up", item=CRAFT_PART)
        self.assertEqual(panels.part_config(PartKey(key(0), "up")), {}, "同一面再挂一块不继承")

    def test_a_kind_without_a_screen_leaves_the_click_to_vanilla(self):
        """点中的是面板，只是这种面板没有界面（P2P 隧道）：不认领，原版交互照常——手里的方块
        贴着这一面放下去。"""
        rig = PanelRig()
        rig.place(key(0), GLASS, parts={"east": TUNNEL_PART_ID})
        rig.step(2)
        block, item = rig.use(key(0), GLASS, CENTER, "east", item=STONE)
        self.assertFalse(block.cancelled or item.cancelled)
        self.assertEqual(rig.opened, [])

    def test_a_kind_registered_without_a_screen_is_not_claimed_either(self):
        rig = PanelRig()
        rig.place(key(0), GLASS, parts={"east": CRAFT_PART})
        rig.step(2)
        rig.panels.register_kind(PART_KIND_TERMINAL)
        block, item = rig.use(key(0), GLASS, CENTER, "east", item=STONE)
        self.assertFalse(block.cancelled or item.cancelled)
        self.assertEqual(rig.opened, [])

    def test_front_change_without_a_hook_is_only_counted(self):
        rig = PanelRig()
        rig.place(key(0), GLASS, parts={"east": CRAFT_PART})
        change = [c for c in rig.lifecycle.handle(NormalizedEvent.neighbor_changed(
            key(0), key(1), to_device_id=STONE)) if c.kind == ChangeKind.PART_FRONT_CHANGED]
        self.assertEqual(len(change), 1)
        self.assertFalse(rig.panels.front_changed(change[0]))

        def boom(*args):
            raise RuntimeError("boom")

        rig.panels.register_kind(PART_KIND_TERMINAL, on_front_changed=boom)
        self.assertFalse(rig.panels.front_changed(change[0]))
        self.assertEqual(rig.panels.diagnostics()["front_handler_errors"], 1)


# -- 终端库存档（接了存档）--------------------------------------------------------


class TestPanelInventories(unittest.TestCase):
    def setUp(self):
        self.world = PanelWorld()
        self.backend = testsupport.MemoryExtraDataBackend()
        self.rig = ServerRig(world=self.world, persistence=self.backend)
        self.rig.network(parts={"up": CRAFT_PART, "north": CRAFT_PART, "south": TEMPLATE_PART})

    def tearDown(self):
        self.rig.stop()

    def restart(self):
        self.rig.stop()
        self.rig.start()
        self.rig.network(fresh=False)

    def test_keys_carry_the_face(self):
        ref = self.rig.ref("up")
        self.assertEqual(instance_key(ref), key(1).as_key() + (ref.generation, "up"))
        self.assertEqual(instance_key(ref.host()), key(1).as_key() + (ref.generation,))
        row = saved_row(("0", 1, 2, 3, "tok", "east"), {"detached": False})
        self.assertEqual(row["slot"], "east")
        self.assertEqual(row_key(json.loads(json.dumps(row))), ("0", 1, 2, 3, "tok", "east"))
        self.assertEqual(row_key({"node": [0, 1, 2, 3], "generation": "tok"}), (0, 1, 2, 3, "tok"))
        self.assertIsNone(row_key({"node": [0, 1, 2, 3], "generation": "tok", "slot": "middle"}))

    def test_two_panels_on_one_cable_survive_a_restart_separately(self):
        server = self.rig.server
        server.workbench.grid_for(self.rig.ref("up")).set_slot(0, IRON, 3)
        server.workbench.grid_for(self.rig.ref("north")).set_slot(4, IRON, 7)
        self.restart()
        server = self.rig.server
        self.assertEqual(server.workbench.grid_for(self.rig.ref("up")).entry(0), (IRON, 3))
        self.assertEqual(server.workbench.grid_for(self.rig.ref("north")).entry(4), (IRON, 7))
        self.assertEqual(server.workbench.grid_for(self.rig.ref("north")).count_at(0), 0)

    def test_a_detached_template_panel_drops_its_items_like_the_block(self):
        server = self.rig.server
        server.templates.state_for(self.rig.ref("south")).slots.set_slot(INPUT_SLOT, BLANK, 3)
        self.rig.intent(key(1), GLASS, ACTION_DISMANTLE, CENTER, "south", WRENCH_ID)
        self.assertEqual(self.world.dropped,
                         [((0, 1, 64, 0), [("endernet:factorical_template", 3)])])
        self.assertIsNone(server.templates.get(self.rig.ref("south")))

    def test_a_detached_crafting_panel_keeps_its_grid_as_an_orphan(self):
        server = self.rig.server
        server.workbench.grid_for(self.rig.ref("up")).set_slot(0, IRON, 5)
        self.rig.intent(key(1), GLASS, ACTION_DISMANTLE, CENTER, "up", WRENCH_ID)
        self.assertEqual([grid.entry(0) for grid in server.workbench.orphans()], [(IRON, 5)])
        self.assertEqual(server.workbench.grid_for(self.rig.ref("north")).count_at(0), 0)

    def test_panel_first_cable_later_keeps_the_saved_inventory(self):
        """面板先挂在底座上，再换成光缆：同一份库存，重启后还在。"""
        rig = self.rig
        rig.place(key(2), HOLDER, parts={"north": CRAFT_PART})
        rig.settle()
        ref = rig.ref("north", host=key(2))
        rig.server.workbench.grid_for(ref).set_slot(0, IRON, 2)
        self.assertEqual(rig.intent(key(2), HOLDER, ACTION_ADD_CABLE, CENTER, "north", GLASS).reason,
                         REASON_CABLE_ADDED)
        rig.settle()
        self.assertEqual(rig.server.workbench.grid_for(ref).entry(0), (IRON, 2))
        self.restart()
        rig.place(key(2), GLASS, fresh=False)
        rig.settle()
        self.assertEqual(rig.server.workbench.grid_for(rig.ref("north", host=key(2))).entry(0), (IRON, 2))
        self.assertEqual(len(rig.server.workbench.orphans()), 0)

    def test_a_new_panel_on_the_same_face_starts_empty(self):
        server = self.rig.server
        server.workbench.grid_for(self.rig.ref("up")).set_slot(0, IRON, 5)
        self.rig.intent(key(1), GLASS, ACTION_DISMANTLE, CENTER, "up", WRENCH_ID)
        self.rig.settle()
        result = self.rig.intent(key(1), GLASS, ACTION_PLACE_PANEL, CENTER, "up", CRAFT_PART)
        self.assertEqual(result.reason, REASON_PANEL_PLACED)
        self.rig.settle()
        self.assertEqual(server.workbench.grid_for(self.rig.ref("up")).count_at(0), 0)
        self.restart()
        self.assertEqual(self.rig.server.workbench.grid_for(self.rig.ref("up")).count_at(0), 0)
        self.assertEqual(len(self.rig.server.workbench.orphans()), 1, "旧的那份还在孤儿里")


if __name__ == "__main__":
    unittest.main()
