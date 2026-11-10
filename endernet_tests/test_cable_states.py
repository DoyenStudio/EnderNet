# coding=utf-8
"""光缆的方块状态维护（`server/devices/cables.py`，阶段 C，`.claude/13` §5）。

覆盖：注册时从方块实体读切断面与面板；期望状态（面板 2、连通 1、其余 0、读不到的邻格保留）；
只在变化时写、一格一次、只写声明过的键、按帧限量；拆除下一帧复查（快拆快放不误清）；
区块加载等一会儿再重读；邻居事件过滤 `from == to`；移除白名单覆盖光缆。
"""

import unittest

import testsupport

from endernet.common.cable_shapes import side_state
from endernet.common.definitions import DefinitionSet
from endernet.common.identity import FACING_NAMES, ChunkKey, NodeKey, face_mask
from endernet.server.adapters.events import ServerEventBinder
from endernet.server.devices.cables import (
    ALL_FACES_MASK,
    CableStateKeeper,
    config_from_entity,
    is_pending,
)
from endernet.server.devices.catalog import CABLE_CUTS_KEY, CABLE_PARTS_KEY, DeviceCatalog
from endernet.server.devices.registry import DeviceRegistry
from endernet.server.network.topology import TopologyService
from endernet.server.runtime.events import EventQueue, NormalizedEvent
from endernet.server.runtime.lifecycle import ChangeKind, DeviceLifecycle, LifecycleChange

GLASS = "endernet:ender_glass_cable"
CABLE = "endernet:ender_cable"
DENSE = "endernet:ender_dense_cable"
BB8 = "endernet:network_baseband_8"
DOS = "endernet:dos_network_endpoint"
TERM_PART = "endernet:common_terminal_part"
HOLDER = "endernet:part_holder"
AIR = "minecraft:air"

#: 方块实体「还拿不到」
UNREADABLE = object()


def key(x, y=64, z=0, dimension=0):
    return NodeKey(dimension, x, y, z)


class FakeClock(object):
    def __init__(self):
        self.tick = 0

    def current_tick(self):
        return self.tick


class FakeCableWorld(object):
    """假世界：方块、方块状态、光缆方块实体、区块加载、拆方块、背包与掉落都只记账。"""

    def __init__(self):
        #: NodeKey -> 方块标识（没记的格子是空气）
        self.blocks = {}
        #: NodeKey -> 方块状态字典（只有光缆与面板底座有）
        self.states = {}
        #: NodeKey -> (切断掩码, 面板表) 或 UNREADABLE
        self.entities = {}
        #: PartKey -> 面板配置
        self.part_configs = {}
        self.unloaded_chunks = set()
        self.state_writes = []
        self.entity_writes = []
        self.removed = []
        self.placed = []
        self.swaps = []
        self.given = []
        self.dropped = []
        self.facing_reads = 0
        #: 背包还能放几件；None 表示不限
        self.inventory_space = None
        self.remove_ok = True
        self.set_ok = True
        self.swap_ok = True
        self.entity_writable = True
        self.whitelisted = set()

    # -- 装配与生命周期要的 ------------------------------------------------

    def list_loaded_chunks(self):
        return []

    def block_entities_in_chunk(self, chunk_key):
        return []

    def register_block_remove_listener(self, block_ids):
        self.whitelisted.update(block_ids)

    def facing_at(self, node_key):
        self.facing_reads += 1
        return None

    def note_chunk_loaded(self, chunk_key, block_entities=()):
        pass

    def note_chunk_unloaded(self, chunk_key):
        pass

    def diagnostics(self):
        return {"fake": True}

    # -- 光缆 --------------------------------------------------------------

    def put_cable(self, node_key, cuts=None, parts=None, declared=FACING_NAMES, block=None):
        self.states[node_key] = dict((side_state(face), 0) for face in declared)
        self.entities[node_key] = (cuts, parts)
        if block is not None:
            self.blocks[node_key] = block

    def block_name_at(self, node_key):
        return self.blocks.get(node_key, AIR)

    def set_block(self, node_key, block_id):
        if not self.set_ok:
            return False
        self.placed.append((node_key, block_id))
        self.blocks[node_key] = block_id
        if block_id == HOLDER:
            self.put_cable(node_key)
        return True

    def swap_faced_block(self, node_key, block_id, cuts, parts):
        if not self.swap_ok or self.entities.get(node_key) is UNREADABLE:
            return False
        self.swaps.append((node_key, block_id, cuts, dict(parts)))
        self.blocks[node_key] = block_id
        self.states[node_key] = dict((side_state(face), 0) for face in FACING_NAMES)
        self.entities[node_key] = (cuts, dict(parts))
        return True

    def part_config(self, part_key):
        return dict(self.part_configs.get(part_key, {}))

    def write_part_config(self, part_key, data):
        if not self.entity_writable:
            return False
        self.part_configs[part_key] = dict(data)
        return True

    def sides(self, node_key):
        states = self.states[node_key]
        return dict((face, states.get(side_state(face))) for face in FACING_NAMES)

    def cable_entity_fields(self, node_key):
        entry = self.entities.get(node_key, (None, None))
        return None if entry is UNREADABLE else entry

    def write_cable_entity_fields(self, node_key, cuts, parts):
        if not self.entity_writable or self.entities.get(node_key) is UNREADABLE:
            return False
        self.entities[node_key] = (cuts, dict(parts))
        self.entity_writes.append((node_key, cuts, dict(parts)))
        return True

    def block_states_at(self, node_key):
        states = self.states.get(node_key)
        return dict(states) if states is not None else None

    def update_block_states(self, node_key, changes, current=None):
        self.states[node_key].update(changes)
        self.state_writes.append((node_key, dict(changes)))
        return True

    def check_chunk_state(self, node_key):
        return node_key.chunk_key() not in self.unloaded_chunks

    def remove_block(self, node_key):
        if not self.remove_ok:
            return False
        self.removed.append(node_key)
        self.states.pop(node_key, None)
        self.blocks.pop(node_key, None)
        return True

    def give_items(self, player_id, item_ids):
        given, leftovers = [], []
        for item_id in item_ids:
            if self.inventory_space is None or self.inventory_space > 0:
                given.append(item_id)
                if self.inventory_space is not None:
                    self.inventory_space -= 1
            else:
                leftovers.append(item_id)
        self.given.append((player_id, given))
        return leftovers

    def drop_items_at(self, at, items):
        self.dropped.append((at, [(resource.name, count) for resource, count in items]))
        return len(items)


class Rig(object):
    """注册表 + 拓扑 + 生命周期 + 状态维护，变化经同一个出口喂给拓扑与状态维护。"""

    def __init__(self, world=None, **keeper_options):
        self.definitions = DefinitionSet()
        self.registry = DeviceRegistry(self.definitions)
        self.catalog = DeviceCatalog(self.definitions)
        self.topology = TopologyService(self.definitions, self.registry, self.catalog)
        self.world = world if world is not None else FakeCableWorld()
        self.clock = FakeClock()
        self.keeper = CableStateKeeper(
            self.definitions, self.registry, self.catalog, self.topology, self.world,
            clock=self.clock, **keeper_options
        )
        self.lifecycle = DeviceLifecycle(
            self.definitions, self.registry, self.catalog, world=self.world
        )
        self.lifecycle.set_config_reader(self.keeper.read_config)
        self.lifecycle.set_change_sink(self._sink)
        self.changes = []

    def _sink(self, changes):
        for change in changes:
            self.changes.append(change)
            self.topology.on_lifecycle_change(change)
            self.keeper.observe(change)

    def place(self, node_key, device_id, cuts=None, parts=None):
        if self.definitions.has_face_data(device_id):
            self.world.put_cable(node_key, cuts=cuts, parts=parts)
        self.world.blocks[node_key] = device_id
        return self.lifecycle.handle(
            NormalizedEvent.place(node_key, device_id, tick=self.clock.tick,
                                  source="entity_place_after")
        )

    def remove(self, node_key, device_id):
        self.world.states.pop(node_key, None)
        self.world.blocks.pop(node_key, None)
        return self.lifecycle.handle(NormalizedEvent.remove(node_key, device_id))

    def step(self, ticks=1):
        for _ in range(ticks):
            self.topology.advance()
            self.keeper.advance(self.clock.tick)
            self.clock.tick += 1

    def writes_for(self, node_key, since=0):
        return [changes for at, changes in self.world.state_writes[since:] if at == node_key]


def sides(**values):
    out = dict.fromkeys(FACING_NAMES, 0)
    out.update(values)
    return out


class TestConfigFromTheEntity(unittest.TestCase):
    def test_fresh_cable_has_no_cuts_and_no_panels(self):
        self.assertEqual(config_from_entity(None, None), {CABLE_CUTS_KEY: 0, CABLE_PARTS_KEY: {}})

    def test_bad_panel_entries_are_dropped_and_odd_cuts_kept_as_is(self):
        config = config_from_entity("x", {"north": TERM_PART, "sideways": TERM_PART, "up": ""})
        self.assertEqual(config, {CABLE_CUTS_KEY: "x", CABLE_PARTS_KEY: {"north": TERM_PART}})

    def test_place_reads_cuts_and_parts_into_the_record(self):
        rig = Rig()
        rig.place(key(0), CABLE, cuts=face_mask(["east"]), parts={"north": TERM_PART})
        record = rig.registry.get(key(0))
        self.assertEqual(record.config, {
            CABLE_CUTS_KEY: face_mask(["east"]),
            CABLE_PARTS_KEY: {"north": TERM_PART},
        })

    def test_cables_do_not_read_a_facing(self):
        rig = Rig()
        rig.place(key(0), CABLE)
        rig.place(key(1), BB8)
        self.assertEqual(rig.world.facing_reads, 1, "只有基带读了朝向")

    def test_unreadable_entity_isolates_the_cable(self):
        rig = Rig()
        rig.world.put_cable(key(0))
        rig.world.entities[key(0)] = UNREADABLE
        rig.lifecycle.handle(NormalizedEvent.place(key(0), CABLE))
        record = rig.registry.get(key(0))
        self.assertTrue(is_pending(record))
        self.assertEqual(record.config[CABLE_CUTS_KEY], ALL_FACES_MASK)

    def test_world_without_cable_support_gives_a_plain_cable(self):
        class BareWorld(object):
            def facing_at(self, node_key):
                return None

        rig = Rig(world=BareWorld())
        self.assertEqual(rig.keeper.read_config(key(0), CABLE), {CABLE_CUTS_KEY: 0, CABLE_PARTS_KEY: {}})
        self.assertIsNone(rig.keeper.read_config(key(0), BB8))


class TestExpectedStates(unittest.TestCase):
    def test_two_cables_grow_arms_toward_each_other(self):
        rig = Rig()
        rig.place(key(0), CABLE)
        rig.place(key(1), GLASS)
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), sides(east=1))
        self.assertEqual(rig.world.sides(key(1)), sides(west=1))

    def test_each_cell_is_written_once_and_only_when_it_changes(self):
        rig = Rig()
        rig.place(key(0), CABLE)
        rig.place(key(1), CABLE)
        rig.step(3)
        for node_key in (key(0), key(1)):
            self.assertEqual(len(rig.writes_for(node_key)), 1, node_key)
        written = len(rig.world.state_writes)
        rig.keeper.request(key(0))
        rig.keeper.request(key(1))
        rig.step(3)
        self.assertEqual(len(rig.world.state_writes), written, "没变就不写")
        self.assertEqual(rig.keeper.stats["unchanged"], 2)

    def test_all_six_faces_go_out_in_one_write(self):
        rig = Rig()
        rig.place(key(0), CABLE)
        for x, y, z in ((1, 64, 0), (-1, 64, 0), (0, 65, 0), (0, 63, 0), (0, 64, 1), (0, 64, -1)):
            rig.place(NodeKey(0, x, y, z), GLASS)
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), dict.fromkeys(FACING_NAMES, 1))
        self.assertEqual(len(rig.writes_for(key(0))), 1)

    def test_arm_toward_a_device_and_a_baseband(self):
        rig = Rig()
        rig.place(key(0), CABLE)
        rig.place(key(1), BB8)
        rig.place(key(-1), "endernet:common_terminal_panel")
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), sides(east=1, west=1))

    def test_no_arm_toward_a_controller(self):
        rig = Rig()
        rig.place(key(0), CABLE)
        rig.place(key(1), DOS)
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), sides())

    def test_cut_face_has_no_arm_on_either_side(self):
        rig = Rig()
        rig.place(key(0), CABLE, cuts=face_mask(["east"]))
        rig.place(key(1), CABLE)
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), sides())
        self.assertEqual(rig.world.sides(key(1)), sides())

    def test_panel_face_is_two_and_does_not_connect(self):
        rig = Rig()
        rig.place(key(0), GLASS, parts={"east": TERM_PART})
        rig.place(key(1), CABLE)
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), sides(east=2))
        self.assertEqual(rig.world.sides(key(1)), sides())

    def test_dense_cable_never_shows_a_panel(self):
        rig = Rig()
        rig.place(key(0), DENSE, parts={"north": TERM_PART})
        rig.place(key(0, z=-1), CABLE)
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), sides(north=1))

    def test_unknown_neighbor_keeps_the_arm(self):
        rig = Rig()
        rig.place(key(15), CABLE)
        rig.world.states[key(15)][side_state("east")] = 1
        rig.world.unloaded_chunks.add(key(16).chunk_key())
        rig.keeper.request(key(15))
        rig.step(2)
        self.assertEqual(rig.world.sides(key(15))["east"], 1, "邻格区块没加载：保留")
        rig.world.unloaded_chunks.clear()
        rig.keeper.request(key(15))
        rig.step(2)
        self.assertEqual(rig.world.sides(key(15))["east"], 0, "邻格加载了、确实没东西：清掉")

    def test_pending_neighbor_keeps_the_arm(self):
        rig = Rig()
        rig.place(key(0), CABLE)
        rig.world.states[key(0)][side_state("east")] = 1
        rig.world.put_cable(key(1))
        rig.world.entities[key(1)] = UNREADABLE
        rig.lifecycle.handle(NormalizedEvent.place(key(1), CABLE))
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0))["east"], 1)
        self.assertEqual(rig.writes_for(key(1)), [], "等方块实体的格子不写")

    def test_only_declared_keys_are_written(self):
        rig = Rig()
        rig.world.put_cable(key(0), declared=[f for f in FACING_NAMES if f != "east"])
        rig.lifecycle.handle(NormalizedEvent.place(key(0), CABLE))
        rig.place(key(1), CABLE)
        rig.place(key(-1), CABLE)
        rig.step(3)
        self.assertNotIn(side_state("east"), rig.world.states[key(0)])
        self.assertEqual(rig.writes_for(key(0)), [{side_state("west"): 1}])

    def test_budget_limits_cells_per_tick(self):
        rig = Rig(cells_per_tick=1)
        for x in range(3):
            rig.place(key(x), CABLE)
        rig.topology.advance()
        rig.clock.tick = 5
        self.assertEqual([rig.keeper.advance(rig.clock.tick) for _ in range(4)], [1, 1, 1, 0])
        self.assertEqual(len(rig.world.state_writes), 3)


class TestRemovalRecheck(unittest.TestCase):
    def setUp(self):
        self.rig = Rig()
        self.rig.place(key(0), CABLE)
        self.rig.place(key(1), CABLE)
        self.rig.step(3)

    def test_neighbor_arm_is_cleared_on_the_next_tick(self):
        rig = self.rig
        rig.remove(key(1), CABLE)
        since = len(rig.world.state_writes)
        rig.topology.advance()
        rig.keeper.advance(rig.clock.tick)
        self.assertEqual(rig.writes_for(key(0), since), [], "拆除当帧不动（下一帧复查）")
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0))["east"], 0)

    def test_quick_replace_keeps_the_arm(self):
        rig = self.rig
        rig.remove(key(1), CABLE)
        rig.place(key(1), CABLE)
        since = len(rig.world.state_writes)
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0))["east"], 1)
        self.assertEqual(rig.writes_for(key(0), since), [], "快拆快放：邻格的臂一次也没被清")
        self.assertEqual(rig.world.sides(key(1)), sides(west=1))


class TestChunkLoad(unittest.TestCase):
    def entry(self, node_key, device_id):
        return {"posX": node_key.x, "posY": node_key.y, "posZ": node_key.z, "blockName": device_id}

    def test_loaded_cables_wait_then_reread(self):
        rig = Rig()
        for node_key in (key(0), key(1)):
            rig.world.put_cable(node_key)
        chunk = ChunkKey(0, 0, 0)
        rig.lifecycle.handle(NormalizedEvent.chunk_loaded(
            chunk, [self.entry(key(0), CABLE), self.entry(key(1), CABLE)]))
        # 注册之后、到点之前，玩家（或别的模组）改了方块实体：到点重读才算数
        rig.world.entities[key(1)] = (face_mask(["west"]), None)
        rig.step(rig.keeper.chunk_settle_ticks - 1)
        self.assertEqual(rig.world.state_writes, [], "区块加载后先等")
        rig.step(3)
        self.assertEqual(rig.registry.get(key(1)).config[CABLE_CUTS_KEY], face_mask(["west"]))
        self.assertEqual(rig.world.sides(key(0)), sides())
        self.assertEqual(rig.world.sides(key(1)), sides())

    def test_unreadable_entity_is_retried_then_connects(self):
        rig = Rig()
        for node_key in (key(0), key(1)):
            rig.world.put_cable(node_key)
        rig.world.entities[key(0)] = UNREADABLE
        rig.lifecycle.handle(NormalizedEvent.chunk_loaded(
            ChunkKey(0, 0, 0), [self.entry(key(0), CABLE), self.entry(key(1), CABLE)]))
        self.assertTrue(is_pending(rig.registry.get(key(0))))
        rig.step(rig.keeper.chunk_settle_ticks + 2)
        self.assertEqual(rig.world.state_writes, [])
        self.assertEqual(rig.keeper.stats["entity_retries"], 1)
        rig.world.entities[key(0)] = (None, None)
        rig.step(rig.keeper.chunk_settle_ticks + 3)
        self.assertFalse(is_pending(rig.registry.get(key(0))))
        self.assertEqual(rig.world.sides(key(0)), sides(east=1))
        self.assertEqual(rig.world.sides(key(1)), sides(west=1))

    def test_gives_up_after_the_retries(self):
        rig = Rig(entity_retries=2)
        rig.world.put_cable(key(0))
        rig.world.entities[key(0)] = UNREADABLE
        rig.lifecycle.handle(NormalizedEvent.chunk_loaded(ChunkKey(0, 0, 0), [self.entry(key(0), CABLE)]))
        rig.step(rig.keeper.chunk_settle_ticks * 4)
        self.assertEqual(rig.keeper.stats["entity_unreadable"], 1)
        self.assertTrue(is_pending(rig.registry.get(key(0))), "读不到就一直孤立，不猜")
        self.assertEqual(rig.keeper.pending_count(), 0)


class TestObserve(unittest.TestCase):
    def test_part_detached_refreshes_the_host(self):
        rig = Rig()
        rig.place(key(0), GLASS)
        rig.step(2)
        record = rig.registry.get(key(0))
        rig.keeper.observe(LifecycleChange(ChangeKind.PART_DETACHED, key(0), TERM_PART, record.ref, "east"))
        self.assertEqual(rig.keeper.pending_count(), 1)

    def test_unloaded_cell_is_dropped_from_the_queue(self):
        rig = Rig()
        rig.place(key(0), CABLE)
        rig.lifecycle.handle(NormalizedEvent.chunk_unloaded(key(0).chunk_key()))
        self.assertEqual(rig.keeper.pending_count(), 0)

    def test_write_faces_needs_the_entity(self):
        rig = Rig()
        rig.place(key(0), CABLE)
        rig.world.entity_writable = False
        self.assertFalse(rig.keeper.write_faces(key(0), cuts=face_mask(["up"])))
        self.assertEqual(rig.registry.get(key(0)).config[CABLE_CUTS_KEY], 0, "方块实体没写进去，记录也不动")


class TestNeighborEventFilter(unittest.TestCase):
    def setUp(self):
        self.definitions = DefinitionSet()
        self.registry = DeviceRegistry(self.definitions)
        self.registry.register(key(0), CABLE)
        self.queue = EventQueue()
        self.binder = ServerEventBinder(self.queue, FakeCableWorld(), self.registry, self.definitions)

    def test_state_only_change_is_ignored(self):
        self.binder._on_neighbor(0, 0, 64, 0, 1, 64, 0, CABLE, CABLE)
        self.assertEqual(len(self.queue), 0)
        self.assertEqual(self.binder.counts["neighbor_same_block"], 1)

    def test_real_change_is_queued(self):
        self.binder._on_neighbor(0, 0, 64, 0, 1, 64, 0, "minecraft:stone", "minecraft:air")
        self.assertEqual(len(self.queue), 1)

    def test_events_without_from_still_pass(self):
        self.binder._on_neighbor(0, 0, 64, 0, 1, 64, 0, CABLE)
        self.assertEqual(len(self.queue), 1)

    def test_remove_whitelist_covers_the_cables(self):
        world = FakeCableWorld()
        binder = ServerEventBinder(self.queue, world, self.registry, self.definitions)
        binder.register_remove_whitelist()
        for cable in (GLASS, CABLE, DENSE, HOLDER):
            self.assertIn(cable, world.whitelisted)
        self.assertIn(BB8, world.whitelisted)
        self.assertNotIn(TERM_PART, world.whitelisted, "面板不是方块")


class TestPartHolder(unittest.TestCase):
    """面板底座（2026-11-10）：按面数据与方块状态与光缆同一套，但不与任何设备相连。"""

    def test_registration_reads_the_panels_and_states_show_them(self):
        rig = Rig()
        rig.place(key(0), HOLDER, parts={"north": TERM_PART})
        rig.place(key(1), CABLE)
        rig.place(key(0, y=65), BB8)
        rig.step(3)
        record = rig.registry.get(key(0))
        self.assertIsNone(record.facing, "底座没有朝向")
        self.assertEqual(rig.world.facing_reads, 1, "只有基带读了朝向")
        self.assertEqual(record.config[CABLE_PARTS_KEY], {"north": TERM_PART})
        self.assertEqual(rig.world.sides(key(0)), sides(north=2), "不与相邻光缆、基带连")
        self.assertEqual(rig.world.sides(key(1)), sides(), "光缆也不朝底座长臂")
        self.assertEqual(set(rig.topology.link_state(key(0)).linked), set())

    def test_converting_in_place_keeps_the_instance(self):
        rig = Rig()
        rig.place(key(0), HOLDER, parts={"north": TERM_PART})
        rig.place(key(1), CABLE)
        rig.step(3)
        before = rig.registry.get(key(0)).ref
        self.assertTrue(rig.keeper.swap_block(key(0), GLASS))
        changes = rig.lifecycle.convert(key(0), GLASS)
        self.assertEqual([(c.kind, c.device_id, c.detail) for c in changes[:1]],
                         [(ChangeKind.HOST_CONVERTED, GLASS, HOLDER)])
        record = rig.registry.get(key(0))
        self.assertEqual((record.device_id, record.ref), (GLASS, before), "同一个实例、同一代次")
        self.assertEqual(record.config[CABLE_PARTS_KEY], {"north": TERM_PART})
        self.assertEqual(rig.world.entities[key(0)], (0, {"north": TERM_PART}))
        rig.step(3)
        self.assertEqual(rig.world.sides(key(0)), sides(north=2, east=1))
        self.assertEqual(rig.world.sides(key(1)), sides(west=1))
        # 引擎随后的旧方块移除 / 新方块放置事件都不再改它
        rig.lifecycle.handle(NormalizedEvent.remove(key(0), HOLDER))
        rig.lifecycle.handle(NormalizedEvent.place(key(0), GLASS))
        self.assertEqual(rig.registry.get(key(0)).ref, before)
        self.assertEqual(rig.registry.stats()["conversions"], 1)

    def test_a_failed_swap_changes_nothing(self):
        rig = Rig()
        rig.place(key(0), HOLDER, parts={"north": TERM_PART})
        rig.world.swap_ok = False
        self.assertFalse(rig.keeper.swap_block(key(0), GLASS))
        self.assertEqual(rig.registry.get(key(0)).device_id, HOLDER)
        self.assertFalse(rig.keeper.swap_block(key(0), BB8), "只在按面的方块之间换")

    def test_digging_a_holder_detaches_its_panels(self):
        rig = Rig()
        rig.place(key(0), HOLDER, parts={"north": TERM_PART, "up": TERM_PART})
        rig.changes = []
        rig.remove(key(0), HOLDER)
        self.assertEqual(
            [(c.kind, c.detail) for c in rig.changes if c.kind == ChangeKind.PART_DETACHED],
            [(ChangeKind.PART_DETACHED, "up"), (ChangeKind.PART_DETACHED, "north")])

    def test_front_change_reaches_only_the_panel_facing_it(self):
        rig = Rig()
        rig.place(key(0), GLASS, parts={"north": TERM_PART, "up": TERM_PART})
        rig.changes = []
        rig.lifecycle.handle(NormalizedEvent.neighbor_changed(
            key(0), key(0).neighbor("north"), to_device_id="minecraft:chest"))
        fronts = [(c.node_key, c.device_id, c.detail) for c in rig.changes
                  if c.kind == ChangeKind.PART_FRONT_CHANGED]
        self.assertEqual(fronts, [(key(0), TERM_PART, "north")])
        rig.changes = []
        rig.lifecycle.handle(NormalizedEvent.neighbor_changed(
            key(0), key(0).neighbor("south"), to_device_id="minecraft:chest"))
        self.assertEqual([c for c in rig.changes if c.kind == ChangeKind.PART_FRONT_CHANGED], [])
        self.assertEqual(rig.lifecycle.stats.part_fronts_changed, 1)

    def test_front_change_does_not_dirty_the_topology(self):
        rig = Rig()
        rig.place(key(0), GLASS, parts={"north": TERM_PART})
        rig.step(3)
        marks = rig.topology.stats["marks"]
        change = LifecycleChange(ChangeKind.PART_FRONT_CHANGED, key(0), TERM_PART,
                                 rig.registry.get(key(0)).ref, "north")
        rig.topology.on_lifecycle_change(change)
        self.assertEqual(rig.topology.stats["marks"], marks)


class TestServerWiring(unittest.TestCase):
    """经 `ServerBootstrap` 走一遍：放置 → 拓扑 → 方块状态。"""

    def test_placing_cables_through_the_server_writes_states(self):
        from endernet.server.bootstrap import ServerBootstrap

        world = FakeCableWorld()
        for node_key in (key(0), key(1)):
            world.put_cable(node_key)
        server = ServerBootstrap(world=world)
        server.start(bind_engine=False)
        try:
            server.push(NormalizedEvent.place(key(0), CABLE, source="entity_place_after"))
            server.push(NormalizedEvent.place(key(1), GLASS, source="entity_place_after"))
            for _ in range(4):
                server.drive_once()
            self.assertEqual(server.tick_errors, [])
            self.assertEqual(world.sides(key(0)), sides(east=1))
            self.assertEqual(world.sides(key(1)), sides(west=1))
            report = server.diagnostics()["cables"]
            self.assertEqual(report["written"], 2)
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
