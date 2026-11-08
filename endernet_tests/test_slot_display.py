# coding=utf-8
"""槽位模块显示（2026-11-05）：协议、服务端追踪器、客户端写变量、生成的资源。

被测的是**规则与接线**，不是引擎：总线、事件基类、变量写入口都是替身。
真机上要另外核对的几条（实体与方块几何能否并存、朝向正负号、物品栏图标）
写在 `common/slot_display.py` 与交付说明里，这里测不出来。
"""

import io
import json
import os
import sys
import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import DefinitionSet
from endernet.common.identity import NodeKey
from endernet.common.slot_display import (
    DISK,
    DISPLAY_DEVICES,
    EMPTY,
    FACING_VARIABLE,
    FACING_YAW,
    FIRST_MODEL,
    MEMORY,
    UNKNOWN,
    build_event_classes,
    model_items,
    model_value,
    parse_request,
    parse_update,
    request_payload,
    slot_count,
    slot_variable,
    update_payload,
)
from endernet.client.slot_display import SlotDisplayClient
from endernet.server.adapters.slot_display import SlotDisplayRequests
from endernet.server.devices.slot_display import SlotDisplayTracker

REPO = testsupport.REPO_ROOT
RP_ROOT = os.path.join(REPO, "endernet_rp")
DRIVER = "endernet:storage_driver"
MEMORY_SLOT = "endernet:memory_slot"


def read_json(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _scripts_module(name):
    scripts = os.path.join(REPO, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return __import__(name)


# -- 替身 ------------------------------------------------------------------


class FakeEvents(object):
    """假的 `tooldelta.events`：C2S 的 `send()` 与 S2C 的 `send/sendAll` 都只记账。"""

    def __init__(self):
        self.to_server = []
        self.to_client = []
        self.broadcast = []
        events = self

        class CustomC2SEvent(object):
            def send(self):
                events.to_server.append(self.marshal())

        class CustomS2CEvent(object):
            def send(self, client_id):
                events.to_client.append((client_id, self.marshal()))

            def sendAll(self):
                events.broadcast.append(self.marshal())

        self.CustomC2SEvent = CustomC2SEvent
        self.CustomS2CEvent = CustomS2CEvent


class FakeBus(object):
    def __init__(self):
        self.handlers = {}

    def ListenEvent(self, event_cls, priority, static=False):
        if static:
            raise AssertionError("绑定方法不能带 static=True（py2 挂不住 _td_static）")

        def wrapper(func):
            self.handlers.setdefault(event_cls, []).append(func)
            return func

        return wrapper

    def RemoveEventListener(self, event_cls, func, priority=0):
        self.handlers.get(event_cls, []).remove(func)

    def _dyn_unlisten(self, event_cls, func, priority=0):
        self.RemoveEventListener(event_cls, func, priority)

    def dispatch(self, event_cls, event):
        for func in list(self.handlers.get(event_cls, [])):
            func(event)


class EngineEvent(object):
    """`ModBlockEntityLoaded/RemoveClientEvent` 的形状。"""

    def __init__(self, block, x, y, z, dimension=0):
        self.blockName = block
        self.posX, self.posY, self.posZ = x, y, z
        self.dimensionId = dimension


class Ref(object):
    def __init__(self, node_key, generation=1):
        self.node_key = node_key
        self.generation = generation


class Assignment(object):
    def __init__(self, slot, medium_id):
        self.slot = slot
        self.medium_id = medium_id


class Medium(object):
    def __init__(self, model_id):
        self.model_id = model_id


class FakeState(object):
    def __init__(self, node_key, generation=1):
        self.ref = Ref(node_key, generation)
        self.node_key = node_key
        self.revision = 0
        #: 驱动器用
        self.assignments = []
        #: 内存槽用：slot -> 带 memory_id 的对象
        self.entries = {}

    def occupied(self):
        return list(self.assignments)


class FakeStore(object):
    def __init__(self):
        self._states = {}
        self.mediums = {}

    def add(self, state):
        self._states[state.node_key.as_key()] = state
        return state

    def remove(self, state):
        del self._states[state.node_key.as_key()]

    def states(self):
        return [self._states[key] for key in sorted(self._states)]

    def get(self, node_key):
        return self._states.get(node_key.as_key())

    def medium(self, medium_id):
        return self.mediums.get(medium_id)


class Entry(object):
    def __init__(self, memory_id):
        self.memory_id = memory_id


# -- 规则 ------------------------------------------------------------------


class TestRules(unittest.TestCase):
    def setUp(self):
        self.definitions = DefinitionSet()

    def test_value_encodes_empty_unknown_and_item_position(self):
        items = model_items(self.definitions, MEMORY)
        self.assertEqual(model_value(items, None), EMPTY)
        self.assertEqual(model_value(items, ""), EMPTY)
        self.assertEqual(model_value(items, "minecraft:dirt"), UNKNOWN)
        for index, item_id in enumerate(items):
            self.assertEqual(model_value(items, item_id), FIRST_MODEL + index)

    def test_item_tables_are_the_definition_tables(self):
        self.assertEqual(model_items(self.definitions, DISK), tuple(self.definitions.item_mediums))
        self.assertEqual(model_items(self.definitions, MEMORY), tuple(self.definitions.item_memories))

    def test_slot_counts_follow_the_container_sizes(self):
        gen_blocks = _scripts_module("gen_blocks")
        for device_id, (kind, _entity) in DISPLAY_DEVICES.items():
            self.assertEqual(slot_count(kind), gen_blocks.container_size_of(device_id), device_id)

    def test_entity_yaw_matches_the_block_rotation(self):
        """实体与方块要转到同一个方向：两张表必须同值（正负号见 FACING_YAW 的注释）。"""
        gen_blocks = _scripts_module("gen_blocks")
        self.assertEqual(FACING_YAW, dict(gen_blocks.FACING_ROTATIONS))

    def test_variable_names_fit_the_engine_limit(self):
        """`SetBlockEntityMolangValue`：以 `variable.` 开头，且不超过两个点。"""
        for name in [FACING_VARIABLE] + [slot_variable(i) for i in range(10)]:
            self.assertTrue(name.startswith("variable."), name)
            self.assertLessEqual(name.count("."), 2, name)


class TestPayloads(unittest.TestCase):
    def test_update_round_trip(self):
        node_key = NodeKey(0, -12, 64, 7)
        payload = update_payload(node_key, "east", (0, 2, 1) + (0,) * 7)
        self.assertEqual(parse_update(payload), (node_key, "east", (0, 2, 1) + (0,) * 7))

    def test_update_is_strict(self):
        good = update_payload(NodeKey(0, 1, 2, 3), "north", (0,) * 10)
        for broken in (
            dict(good, extra=1),
            dict((k, v) for k, v in good.items() if k != "facing"),
            dict(good, slots=[0, -1]),
            dict(good, slots=[True]),
            dict(good, pos=[1, 2]),
            dict(good, pos=[1, 2, "3"]),
            dict(good, facing=3),
        ):
            with self.assertRaises((KeyError, TypeError, ValueError)):
                parse_update(broken)

    def test_request_round_trip(self):
        node_key = NodeKey(1, 5, 70, -9)
        self.assertEqual(parse_request(request_payload(node_key)), node_key)

    def test_request_identity_comes_from_the_engine_only(self):
        request_cls, _update_cls = build_event_classes(FakeEvents())
        event = request_cls.unmarshal(dict(request_payload(NodeKey(0, 1, 2, 3)), __id__="p1"))
        self.assertEqual(event.player_id, "p1")
        self.assertNotIn("__id__", event.payload)
        self.assertEqual(parse_request(event.payload), NodeKey(0, 1, 2, 3))


# -- 服务端 ----------------------------------------------------------------


class TrackerFixture(unittest.TestCase):
    def setUp(self):
        self.definitions = DefinitionSet()
        self.drivers = FakeStore()
        self.memories = FakeStore()
        self.sent = []
        self.facings = {}
        self.tracker = SlotDisplayTracker(
            self.definitions,
            self.drivers,
            self.memories,
            facing_of=lambda node_key: self.facings.get(node_key.as_key()),
            publish=self.sent.append,
        )
        self.disks = model_items(self.definitions, DISK)
        self.sticks = model_items(self.definitions, MEMORY)


class TestTracker(TrackerFixture):
    def test_empty_device_is_not_published(self):
        self.memories.add(FakeState(NodeKey(0, 0, 64, 0)))
        self.assertEqual(self.tracker.advance(), 0)
        self.assertEqual(self.sent, [])

    def test_publishes_once_per_change(self):
        node_key = NodeKey(0, 0, 64, 0)
        self.facings[node_key.as_key()] = "west"
        state = self.memories.add(FakeState(node_key))
        state.entries = {0: Entry(self.sticks[3]), 7: Entry(self.sticks[0])}
        state.revision = 1
        self.assertEqual(self.tracker.advance(), 1)
        expected = [EMPTY] * 10
        expected[0] = FIRST_MODEL + 3
        expected[7] = FIRST_MODEL
        self.assertEqual(self.sent, [update_payload(node_key, "west", expected)])
        # 没有新读数：不再发
        self.assertEqual(self.tracker.advance(), 0)
        # 读数版本变了但内容没变（例如同型号换了一根）：不发
        state.revision = 2
        self.assertEqual(self.tracker.advance(), 0)
        # 拔掉一根：发
        state.entries = {0: Entry(self.sticks[3])}
        state.revision = 3
        self.assertEqual(self.tracker.advance(), 1)
        self.assertEqual(self.sent[-1]["slots"][7], EMPTY)

    def test_driver_reads_the_medium_model(self):
        node_key = NodeKey(0, 3, 64, 0)
        state = self.drivers.add(FakeState(node_key))
        self.drivers.mediums = {"m1": Medium(self.disks[1])}
        state.assignments = [Assignment(2, "m1"), Assignment(9, "orphan")]
        state.revision = 1
        self.tracker.advance()
        slots = self.sent[-1]["slots"]
        self.assertEqual(slots[2], FIRST_MODEL + 1)
        # 载体记录缺失：插着，但认不出型号 -> 默认模块
        self.assertEqual(slots[9], UNKNOWN)
        self.assertEqual(slots.count(EMPTY), 8)

    def test_answer_does_not_swallow_the_broadcast(self):
        """单点回答**不记成已发**：否则别的已加载玩家会错过这次变化。"""
        node_key = NodeKey(0, 0, 64, 0)
        state = self.memories.add(FakeState(node_key))
        state.entries = {1: Entry(self.sticks[0])}
        state.revision = 1
        answer = self.tracker.answer(node_key)
        self.assertEqual(answer["slots"][1], FIRST_MODEL)
        self.assertEqual(self.tracker.advance(), 1)

    def test_unknown_position_gets_no_answer(self):
        self.assertIsNone(self.tracker.answer(NodeKey(0, 9, 9, 9)))
        self.assertEqual(self.tracker.stats["unknown_requests"], 1)

    def test_facing_is_read_once_and_failures_fall_back_to_north(self):
        calls = []

        def facing_of(node_key):
            calls.append(node_key)
            raise RuntimeError("engine unavailable")

        self.tracker.facing_of = facing_of
        node_key = NodeKey(0, 0, 64, 0)
        self.memories.add(FakeState(node_key))
        self.assertEqual(self.tracker.answer(node_key)["facing"], "")
        self.assertEqual(self.tracker.stats["errors"], 1)
        self.tracker.facing_of = lambda key: "south"
        self.assertEqual(self.tracker.answer(node_key)["facing"], "south")
        self.tracker.facing_of = lambda key: "east"
        self.assertEqual(self.tracker.answer(node_key)["facing"], "south", "同一代次只读一次")

    def test_new_generation_starts_over(self):
        node_key = NodeKey(0, 0, 64, 0)
        state = self.memories.add(FakeState(node_key, generation=1))
        state.entries = {0: Entry(self.sticks[0])}
        state.revision = 1
        self.tracker.advance()
        self.memories.remove(state)
        self.tracker.advance()
        self.assertEqual(self.tracker.diagnostics()["tracked"], 0)
        replaced = self.memories.add(FakeState(node_key, generation=2))
        replaced.entries = {0: Entry(self.sticks[0])}
        replaced.revision = 1
        self.assertEqual(self.tracker.advance(), 1, "换了一台方块，即使内容一样也要重发")

    def test_one_broken_device_does_not_stop_the_others(self):
        broken = self.memories.add(FakeState(NodeKey(0, 0, 64, 0)))
        broken.entries = {0: object()}
        broken.revision = 1
        good = self.memories.add(FakeState(NodeKey(0, 1, 64, 0)))
        good.entries = {0: Entry(self.sticks[0])}
        good.revision = 1
        self.assertEqual(self.tracker.advance(), 1)
        self.assertEqual(self.tracker.stats["errors"], 1)

    def test_publish_failure_is_counted_not_raised(self):
        def explode(payload):
            raise RuntimeError("send failed")

        self.tracker.publish = explode
        state = self.memories.add(FakeState(NodeKey(0, 0, 64, 0)))
        state.entries = {0: Entry(self.sticks[0])}
        state.revision = 1
        self.assertEqual(self.tracker.advance(), 0)
        self.assertEqual(self.tracker.stats["errors"], 1)

    def test_without_publisher_nothing_is_sent(self):
        self.tracker.publish = None
        state = self.memories.add(FakeState(NodeKey(0, 0, 64, 0)))
        state.entries = {0: Entry(self.sticks[0])}
        state.revision = 1
        self.assertEqual(self.tracker.advance(), 0)


class TestServerRequests(TrackerFixture):
    def test_request_is_answered_to_the_sender_only(self):
        events = FakeEvents()
        bus = FakeBus()
        service = SlotDisplayRequests(bus, events, lambda: self.tracker)
        service.start()
        node_key = NodeKey(0, 0, 64, 0)
        state = self.memories.add(FakeState(node_key))
        state.entries = {4: Entry(self.sticks[2])}
        state.revision = 1
        request = service.request.unmarshal(dict(request_payload(node_key), __id__="p7"))
        bus.dispatch(service.request, request)
        self.assertEqual(len(events.to_client), 1)
        player, payload = events.to_client[0]
        self.assertEqual(player, "p7")
        self.assertEqual(payload["slots"][4], FIRST_MODEL + 2)
        self.assertEqual(events.broadcast, [])
        # 坏包与不认识的位置都安静地丢掉
        bus.dispatch(service.request, service.request.unmarshal({"pos": [1], "__id__": "p7"}))
        bus.dispatch(service.request, service.request.unmarshal(
            dict(request_payload(NodeKey(0, 50, 50, 50)), __id__="p7")))
        self.assertEqual(len(events.to_client), 1)
        self.assertEqual(service.stats["rejected"], 1)
        self.assertEqual(service.stats["unknown"], 1)
        service.stop()
        self.assertEqual(bus.handlers[service.request], [])


class TestBootstrapWiring(unittest.TestCase):
    def test_bootstrap_owns_a_tracker_and_reports_it(self):
        from endernet.server.bootstrap import ServerBootstrap
        from test_bootstrap import FakeWorld

        sent = []
        server = ServerBootstrap(world=FakeWorld(), on_slot_display=sent.append)
        self.assertIs(server.slot_displays.drivers, server.drivers)
        self.assertIs(server.slot_displays.memory_slots, server.memory_slots)
        self.assertIs(server.slot_displays.publish.__self__, sent)
        server.start(bind_engine=False)
        try:
            self.assertIn("slot_displays", server.diagnostics())
            self.assertEqual(server.tick()["slot_displays"], 0)
        finally:
            server.stop()
        self.assertEqual(sent, [])


# -- 客户端 ----------------------------------------------------------------


class Loaded(object):
    pass


class Removed(object):
    pass


class TestClient(unittest.TestCase):
    def setUp(self):
        self.events = FakeEvents()
        self.bus = FakeBus()
        self.writes = []
        self.result = True
        self.client = SlotDisplayClient(self.bus, self.events, Loaded, Removed, self.set_variable)
        self.client.start()

    def set_variable(self, pos, name, value):
        self.writes.append((pos, name, value))
        return self.result

    def load(self, block=DRIVER, x=1, y=64, z=2):
        self.bus.dispatch(Loaded, EngineEvent(block, x, y, z))

    def push(self, payload):
        self.bus.dispatch(self.client.update, self.client.update.unmarshal(payload))

    def test_loading_a_display_block_requests_its_reading(self):
        self.load()
        self.assertEqual(self.events.to_server, [request_payload(NodeKey(0, 1, 64, 2))])

    def test_other_blocks_are_ignored(self):
        self.load(block="endernet:network_baseband_8")
        self.assertEqual(self.events.to_server, [])

    def test_reading_is_written_into_molang_variables(self):
        self.load()
        slots = [EMPTY] * 10
        slots[3] = FIRST_MODEL
        self.push(update_payload(NodeKey(0, 1, 64, 2), "east", slots))
        written = dict((name, value) for _pos, name, value in self.writes)
        self.assertEqual(written[FACING_VARIABLE], float(FACING_YAW["east"]))
        self.assertEqual(written[slot_variable(3)], float(FIRST_MODEL))
        self.assertEqual(len(written), 11)
        self.assertTrue(all(pos == (1, 64, 2) for pos, _n, _v in self.writes))
        self.assertTrue(all(isinstance(value, float) for _p, _n, value in self.writes))

    def test_readings_for_unloaded_positions_are_dropped(self):
        self.push(update_payload(NodeKey(0, 1, 64, 2), "north", [EMPTY] * 10))
        self.assertEqual(self.writes, [])
        self.load()
        self.bus.dispatch(Removed, EngineEvent(DRIVER, 1, 64, 2))
        self.push(update_payload(NodeKey(0, 1, 64, 2), "north", [EMPTY] * 10))
        self.assertEqual(self.writes, [])
        self.assertEqual(self.client.stats["ignored"], 2)

    def test_bad_payload_never_raises(self):
        self.load()
        self.push({"pos": "nope"})
        self.assertEqual(self.client.stats["rejected"], 1)
        self.assertEqual(self.writes, [])

    def test_failed_writes_are_counted(self):
        self.result = False
        self.load()
        self.push(update_payload(NodeKey(0, 1, 64, 2), "north", [EMPTY] * 10))
        self.assertEqual(self.client.stats["set_failures"], 11)

    def test_stop_unlistens_everything(self):
        self.client.stop()
        self.assertTrue(all(handlers == [] for handlers in self.bus.handlers.values()))


# -- 生成的资源 ------------------------------------------------------------


def molang_true(expression, variables):
    """只认生成器写出的那几种形状：`variable.sN OP K` 用 ` && ` 连起来。"""
    ops = {
        ">=": lambda a, b: a >= b,
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
    }
    for term in expression.split(" && "):
        name, op, value = term.split(" ")
        if not ops[op](variables.get(name, 0), float(value)):
            return False
    return True


class TestSlotModelAssets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gen = _scripts_module("gen_slot_models")
        cls.definitions = DefinitionSet()
        cls.blocks_json = read_json(os.path.join(RP_ROOT, "blocks.json"))
        cls.controllers = read_json(cls.gen.CONTROLLER_FILE)["render_controllers"]

    def test_generated_files_are_up_to_date(self):
        stale = [path for path, payload in self.gen.outputs(self.definitions)
                 if not self.gen.same_content(path, payload)]
        self.assertEqual(stale, [], "请运行 python scripts/gen_slot_models.py")

    def test_blocks_json_points_at_the_entities(self):
        for device_id, (_kind, entity_id) in DISPLAY_DEVICES.items():
            client_entity = self.blocks_json[device_id]["client_entity"]
            self.assertEqual(client_entity["identifier"], entity_id)
            self.assertFalse(client_entity["hand_model_use_client_entity"])
            entity = read_json(self.gen.entity_file(device_id))["minecraft:client_entity"]["description"]
            self.assertEqual(entity["identifier"], entity_id)

    def test_entity_references_resolve(self):
        animation = read_json(self.gen.ANIMATION_FILE)["animations"]
        for device_id in DISPLAY_DEVICES:
            entity = read_json(self.gen.entity_file(device_id))["minecraft:client_entity"]["description"]
            geometry = read_json(self.gen.geometry_file(device_id))["minecraft:geometry"][0]
            self.assertEqual(entity["geometry"]["default"], geometry["description"]["identifier"])
            for controller in entity["render_controllers"]:
                self.assertIn(controller, self.controllers)
            for animation_id in entity["animations"].values():
                self.assertIn(animation_id, animation)
            texture = os.path.join(RP_ROOT, *(entity["textures"]["default"] + ".png").split("/"))
            self.assertTrue(os.path.isfile(texture), texture)
            size = testsupport_png_size(texture)
            self.assertEqual(
                size,
                (geometry["description"]["texture_width"], geometry["description"]["texture_height"]),
            )

    def test_every_variable_is_initialised(self):
        """`SetBlockEntityMolangValue` 只能写 entity.json 里初始化过的变量。"""
        for device_id, (kind, _entity) in DISPLAY_DEVICES.items():
            entity = read_json(self.gen.entity_file(device_id))["minecraft:client_entity"]["description"]
            declared = set(line.split(" = ")[0] for line in entity["scripts"]["initialize"])
            expected = set([FACING_VARIABLE] + [slot_variable(i) for i in range(slot_count(kind))])
            self.assertEqual(declared, expected, device_id)

    def test_each_slot_shows_exactly_the_right_module(self):
        """把每种取值代进渲染控制器：空槽一根都不显示，其余恰好一根，
        有专属模型的型号显示专属骨骼，其余显示默认模块。"""
        for device_id, (kind, _entity) in DISPLAY_DEVICES.items():
            items = model_items(self.definitions, kind)
            geometry = read_json(self.gen.geometry_file(device_id))["minecraft:geometry"][0]
            bones = set(bone["name"] for bone in geometry["bones"])
            rules = {}
            for entry in self.controllers[self.gen.controller_id(device_id)]["part_visibility"]:
                rules.update(entry)
            self.assertTrue(set(rules) <= bones, device_id)
            names = set(bone.split("_", 1)[1] for bone in rules)
            for slot in range(slot_count(kind)):
                for value in range(len(items) + FIRST_MODEL):
                    variables = {slot_variable(slot): float(value)}
                    shown = [
                        bone for bone, expression in rules.items()
                        if bone.startswith("s%d_" % slot) and molang_true(expression, variables)
                    ]
                    if value == EMPTY:
                        self.assertEqual(shown, [], (device_id, slot))
                        continue
                    if value >= FIRST_MODEL:
                        name = items[value - FIRST_MODEL].split(":", 1)[1]
                        expected = name if name in names else "default"
                    else:
                        expected = "default"
                    self.assertEqual(shown, ["s%d_%s" % (slot, expected)], (device_id, slot, value))

    def test_modules_sit_inside_the_block(self):
        """方块实体模型的原点在方块**角点**：这一格在文件坐标里是 x -16..0、z 0..16
        （照 SkyblueTech 挖掘机）。按中心原点写会整体偏出半格——2026-11-06 真机
        「插了东西看不见」就是这个。朝向旋转绕方块中心。"""
        for device_id in DISPLAY_DEVICES:
            geometry = read_json(self.gen.geometry_file(device_id))["minecraft:geometry"][0]
            root = [bone for bone in geometry["bones"] if bone["name"] == "root"][0]
            self.assertEqual(root["pivot"], [-8, 8, 8], device_id)
            for bone in geometry["bones"]:
                for cube in bone.get("cubes", []):
                    for axis, (low, high) in enumerate(((-16, 0), (0, 16), (0, 16))):
                        self.assertGreaterEqual(cube["origin"][axis], low, (device_id, bone["name"]))
                        self.assertLessEqual(
                            cube["origin"][axis] + cube["size"][axis], high, (device_id, bone["name"])
                        )

    def test_probe_mode_shows_every_slot_without_the_server(self):
        """`--probe`：实体初始变量全是 1（默认模块），不依赖消息链路；正常版全是 0。"""
        for probe, expected in ((True, UNKNOWN), (False, EMPTY)):
            for device_id, (kind, _entity) in DISPLAY_DEVICES.items():
                entity = self.gen.build_entity(device_id, kind, probe)
                lines = entity["minecraft:client_entity"]["description"]["scripts"]["initialize"]
                values = dict(line.rstrip(";").split(" = ") for line in lines)
                self.assertEqual(values.pop(FACING_VARIABLE), "0")
                self.assertEqual(set(values.values()), {str(expected)}, (device_id, probe))

    def test_every_kind_has_a_default_module(self):
        for kind in (DISK, MEMORY):
            names = self.gen.module_names(kind, model_items(self.definitions, kind))
            self.assertEqual(names[0], "default", kind)

    def test_memory_slot_is_a_half_height_model(self):
        components = read_json(
            os.path.join(REPO, "endernet_bp", "blocks", "endernet_memory_slot.json")
        )["minecraft:block"]["components"]
        box = {"origin": [-8, 0, -8], "size": [16, 8, 16]}
        self.assertEqual(components["minecraft:collision_box"], box)
        self.assertEqual(components["minecraft:selection_box"], box)
        self.assertEqual(components["minecraft:geometry"], {"identifier": "geometry.endernet.memory_slot"})

    def test_display_blocks_render_their_own_geometry_as_item(self):
        """不写 `item_visual`：它不吃朝向旋转，加过一版图标就背面朝外（2026-11-06），
        用户判定转 180° 的物品几何体多余、删掉。物品栏直接用方块几何体。"""
        for device_id in DISPLAY_DEVICES:
            components = read_json(
                os.path.join(REPO, "endernet_bp", "blocks", device_id.replace(":", "_") + ".json")
            )["minecraft:block"]["components"]
            self.assertNotIn("minecraft:item_visual", components, device_id)


class TestSpecificModules(unittest.TestCase):
    """有专属模型的型号显示专属骨骼（仓库里暂时只有默认模块，这条路径靠临时源走一遍）。"""

    def setUp(self):
        import shutil
        import tempfile

        self.gen = _scripts_module("gen_slot_models")
        self.definitions = DefinitionSet()
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        geo_dir = os.path.join(self.root, "geo")
        tex_dir = os.path.join(self.root, "tex")
        for kind in (DISK, MEMORY):
            os.makedirs(os.path.join(geo_dir, kind))
            os.makedirs(os.path.join(tex_dir, kind))
            shutil.copy(os.path.join(self.gen.SOURCE_GEO_DIR, kind, "default.geo.json"),
                        os.path.join(geo_dir, kind, "default.geo.json"))
            shutil.copy(os.path.join(self.gen.SOURCE_TEX_DIR, kind, "default.png"),
                        os.path.join(tex_dir, kind, "default.png"))
        self.special = model_items(self.definitions, MEMORY)[4]
        name = self.special.split(":", 1)[1]
        shutil.copy(os.path.join(geo_dir, MEMORY, "default.geo.json"),
                    os.path.join(geo_dir, MEMORY, name + ".geo.json"))
        shutil.copy(os.path.join(tex_dir, MEMORY, "default.png"),
                    os.path.join(tex_dir, MEMORY, name + ".png"))
        self.name = name
        self.saved = (self.gen.SOURCE_GEO_DIR, self.gen.SOURCE_TEX_DIR)
        self.gen.SOURCE_GEO_DIR, self.gen.SOURCE_TEX_DIR = geo_dir, tex_dir

    def tearDown(self):
        self.gen.SOURCE_GEO_DIR, self.gen.SOURCE_TEX_DIR = self.saved

    def test_specific_module_takes_over_its_item_only(self):
        modules = self.gen.load_modules(self.definitions)
        self.assertEqual([m.name for m in modules[MEMORY]], ["default", self.name])
        atlas = self.gen.build_atlas(modules)
        self.assertEqual(len(atlas), sum(m.height for m in modules[DISK] + modules[MEMORY]))
        rules = {}
        for entry in self.gen.build_controllers(self.definitions, modules)[
                "render_controllers"][self.gen.controller_id(MEMORY_SLOT)]["part_visibility"]:
            rules.update(entry)
        special_value = FIRST_MODEL + 4
        for value, expected in ((special_value, "s2_" + self.name), (FIRST_MODEL, "s2_default"),
                                (UNKNOWN, "s2_default")):
            shown = [bone for bone, expression in rules.items()
                     if bone.startswith("s2_") and molang_true(expression, {"variable.s2": value})]
            self.assertEqual(shown, [expected], value)
        geometry = self.gen.build_geometry(MEMORY_SLOT, MEMORY, modules, (len(atlas[0]), len(atlas)))
        bones = dict((b["name"], b) for b in geometry["minecraft:geometry"][0]["bones"])
        offset = modules[MEMORY][1].v_offset
        self.assertGreater(offset, 0)
        default_uv = bones["s2_default"]["cubes"][0]["uv"]["north"]["uv"]
        special_uv = bones["s2_" + self.name]["cubes"][0]["uv"]["north"]["uv"]
        self.assertEqual(special_uv[1] - default_uv[1], offset - modules[MEMORY][0].v_offset)

    def test_misnamed_or_oversized_sources_are_rejected(self):
        bad = os.path.join(self.gen.SOURCE_GEO_DIR, MEMORY, "memory_9999.geo.json")
        with io.open(bad, "w", encoding="utf-8") as handle:
            handle.write(u"{}")
        with self.assertRaises(self.gen.SlotModelError):
            self.gen.load_modules(self.definitions)
        os.remove(bad)
        path = os.path.join(self.gen.SOURCE_GEO_DIR, DISK, "default.geo.json")
        geo = read_json(path)
        geo["minecraft:geometry"][0]["bones"][0]["cubes"][0]["size"][1] = 3
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(geo, ensure_ascii=False))
        with self.assertRaises(self.gen.SlotModelError):
            self.gen.load_modules(self.definitions)


def testsupport_png_size(path):
    import struct

    with open(path, "rb") as handle:
        header = handle.read(24)
    return struct.unpack(">II", header[16:24])


if __name__ == "__main__":
    unittest.main()
