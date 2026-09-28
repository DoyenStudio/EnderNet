# coding=utf-8
"""接口设备：总线接口、存货口、取货口与样板口。

对应任务 06-04 至 06-08 的离线验证。**全部离线**：朝向容器用假适配器，
网络那一端用一个最小的假存储端点，引擎一行都不碰。

这一套用例守的是几件「看起来能用、其实会出错」的事：

1. **外部写入与内部移动必须分开。** 混在一起会让同一份资源被反复计入
   订单产物（全局约束 3）。
2. **没搬动要退避，不能忙轮询。** 判据是 handler 返回 `changed=False`。
3. **不确定要停住。** `UNCERTAIN` 之后不许自动重试，只能人工销账。
4. **轮询只注册一次。** 每次 `add_node` 都注册一次的话，
   `set_poll_handler` 会把退避永远打回初值。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import (
    PASSIVE_PORT_SLOT_COUNT,
    PICKUP_FILTER_LIMIT,
    TEMPLATE_PATTERN_SLOT_COUNT,
)
from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.protocol import (
    CommandResult,
    CommandSource,
    ORIGIN_EXTERNAL,
    ORIGIN_INTERNAL,
    Reason,
    STATUS_BLOCKED,
    STATUS_OK,
    TransferReceipt,
    TransferStatus,
)
from endernet.common.resource_key import ResourceKey
from endernet.server.adapters.containers import (
    ContainerEntry,
    FakeContainer,
    NoSimulateContainer,
)
from endernet.server.devices.io import (
    BUFFER_CONTAINER,
    BUFFER_MEMORY,
    BUFFERED_ROLES,
    EMPTY_MEANS_EVERYTHING,
    MODE_BLACKLIST,
    MODE_WHITELIST,
    ROLE_FILTER_MODES,
    DIRECTION_BOTH,
    DIRECTION_EXPORT,
    DIRECTION_IMPORT,
    FACING_ROLES,
    FLUID_DEVICES,
    INTAKE,
    OUTPUT,
    PASSIVE,
    ROLE_OF,
    TEMPLATE,
    BatchState,
    PortConfig,
    PortDispatcher,
    PortGateway,
    PortPolicy,
    PortRunner,
    PortState,
    PortStore,
    is_fluid_port,
    is_port_device,
    neighbor_key,
    port_blocker,
    role_of,
)
from endernet.server.runtime.lifecycle import ChangeKind, LifecycleChange
from endernet.server.storage.transfer import (
    ContainerEndpoint,
    NetworkEndpoint,
    TransferBuffer,
    TransferCoordinator,
)

STONE = ResourceKey.item("minecraft:stone")
DIRT = ResourceKey.item("minecraft:dirt")
WATER = ResourceKey.fluid("minecraft:water")

BUS = "endernet:bus_item_io"
INPUT = "endernet:bus_item_input_io"
OUTPUT_DEVICE = "endernet:bus_item_output_io"
TEMPLATE_DEVICE = "endernet:bus_template_io"
FLUID_BUS = "endernet:bus_fluid_io"


def key(x, y=64, z=0):
    return NodeKey(0, x, y, z)


class _Registry(object):
    """最小的注册表替身：只回答 `get_by_ref` / `get`。"""

    def __init__(self):
        self.records = {}

    def add(self, ref, device_id, facing=None, config=None):
        record = _Record(ref, device_id, facing, config)
        self.records[ref.node_key.as_key()] = record
        return record

    def get(self, node_key):
        return self.records.get(node_key.as_key())

    def get_by_ref(self, ref):
        record = self.records.get(ref.node_key.as_key())
        if record is None or record.ref.generation != ref.generation:
            return None
        return record


class _Record(object):
    def __init__(self, ref, device_id, facing, config):
        self.ref = ref
        self.device_id = device_id
        self.facing = facing
        self.config = dict(config or {})
        self.loaded = True


class _NetworkStub(object):
    """网络那一端的替身：一个内存字典 + 与存储服务同形的查询面。"""

    def __init__(self, stored=None):
        self.stored = dict(stored or {})
        self.inserts = []
        self.extracts = []

    def is_ready(self):
        return True

    def query(self, key):
        count = self.stored.get(key, 0)
        return {"stored": count, "reserved": 0, "available": count, "version": 1}

    def simulate_insert(self, key, count):
        return {"accepted": count}

    def simulate_extract(self, key, count):
        return {"accepted": min(count, self.stored.get(key, 0))}

    def commit_insert(self, op_id, key, count, owner_id=""):
        self.inserts.append((op_id, key, count))
        self.stored[key] = self.stored.get(key, 0) + count
        return TransferReceipt(op_id, count, count, TransferStatus.COMPLETE, "", key)

    def commit_extract(self, op_id, key, count, owner_id=""):
        have = self.stored.get(key, 0)
        moved = min(count, have)
        self.extracts.append((op_id, key, moved))
        self.stored[key] = have - moved
        if moved <= 0:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED,
                Reason.INSUFFICIENT_STOCK, key,
            )
        status = (
            TransferStatus.COMPLETE if moved == count else TransferStatus.PARTIAL
        )
        reason = "" if moved == count else Reason.INSUFFICIENT_STOCK
        return TransferReceipt(op_id, count, moved, status, reason, key)


class _Containers(object):
    """`ContainerProvider` 的替身：位置 -> 假容器。"""

    def __init__(self, boxes=None):
        self.boxes = dict(boxes or {})

    def container_at(self, node_key):
        return self.boxes.get(node_key.as_key())

    def diagnostics(self):
        return {"cached": len(self.boxes)}


class _Scheduler(object):
    """调度器的替身：只记注册/注销与撤销调用。"""

    def __init__(self):
        self.polls = {}
        self.cancelled = []

    def set_poll_handler(self, ref, handler, owner=None):
        self.polls[ref.as_key()] = (handler, owner)
        return True

    def cancel_all(self, ref):
        self.cancelled.append(ref.as_key())
        return 1


class PortFixture(unittest.TestCase):
    """一台接口 + 一个朝向容器 + 一个网络替身的公共夹具。"""

    device_id = OUTPUT_DEVICE
    facing = "east"

    def setUp(self):
        self.registry = _Registry()
        self.ref = DeviceRef(key(1), 0)
        self.registry.add(self.ref, self.device_id, self.facing)
        # 朝向 +x 的容器（也注册成一件设备，好让注册表认得它）
        self.box = FakeContainer(slots=4, max_stack=64)
        self.target_ref = DeviceRef(key(2), 0)
        self.registry.add(self.target_ref, "endernet:storage_driver")
        self.containers = _Containers({key(2).as_key(): self.box})
        self.policy = PortPolicy(template_buffer_capacity=64, max_batch=32,
                                 probe_unit=1)
        self.ports = PortStore(
            None, self.registry, policy=self.policy, containers=self.containers
        )
        self.state = self.ports.ensure(self.ref, self.device_id, self.facing)
        self.network_stub = _NetworkStub({STONE: 40})
        self.network = NetworkEndpoint(self.network_stub, "c1")
        self.coordinator = TransferCoordinator()
        self.dispatcher = PortDispatcher(self.ports, self.coordinator, self.network)

    def configure(self, **kwargs):
        kwargs.setdefault("enabled", True)
        kwargs.setdefault("filter", (STONE,))
        self.ports.configure(self.ref, PortConfig(**kwargs))
        return self.state.config

    def result(self, command="r1"):
        return self.dispatcher.dispatch(self.state, command)


# -- 设备与角色 ------------------------------------------------------------


class TestRoleTables(unittest.TestCase):
    def test_every_bus_device_has_a_role(self):
        self.assertEqual(sorted(ROLE_OF), [
            "endernet:bus_fluid_input_io",
            "endernet:bus_fluid_io",
            "endernet:bus_fluid_output_io",
            "endernet:bus_item_input_io",
            "endernet:bus_item_io",
            "endernet:bus_item_output_io",
            "endernet:bus_template_io",
        ])

    def test_roles_match_blocks_md(self):
        self.assertEqual(role_of(BUS), PASSIVE)
        self.assertEqual(role_of(INPUT), INTAKE)
        self.assertEqual(role_of(OUTPUT_DEVICE), OUTPUT)
        self.assertEqual(role_of(TEMPLATE_DEVICE), TEMPLATE)

    def test_non_bus_devices_are_not_ports(self):
        for device_id in ("endernet:storage_driver", "endernet:network_baseband_8",
                          "endernet:creative_energy_source"):
            self.assertFalse(is_port_device(device_id))
            self.assertEqual(role_of(device_id), "")

    def test_fluid_devices_are_flagged(self):
        for device_id in FLUID_DEVICES:
            self.assertTrue(is_fluid_port(device_id))
        self.assertFalse(is_fluid_port(BUS))

    def test_only_the_passive_and_template_roles_own_a_buffer(self):
        """缓冲归「自己和外部交换」的两类：被动总线与样板口。
        存货口/取货口动的是**邻居**，它们的余量回原地，不需要中转位。"""
        self.assertEqual(sorted(BUFFERED_ROLES), sorted([PASSIVE, TEMPLATE]))
        self.assertEqual(sorted(FACING_ROLES), sorted([INTAKE, OUTPUT, TEMPLATE]))
        self.assertNotIn(PASSIVE, FACING_ROLES)

    def test_neighbor_lookup_matches_the_facing_names(self):
        self.assertEqual(neighbor_key(key(5), "north"), key(5, z=-1))
        self.assertEqual(neighbor_key(key(5), "south"), key(5, z=1))
        self.assertEqual(neighbor_key(key(5), "west"), key(4))
        self.assertEqual(neighbor_key(key(5), "east"), key(6))
        self.assertEqual(neighbor_key(key(5), "up"), key(5, y=65))

    def test_an_unknown_facing_does_not_guess(self):
        """猜错会把东西送进错误的容器，那是不可逆的。"""
        for facing in (None, "", "northwest", "minecraft:north"):
            self.assertIsNone(neighbor_key(key(5), facing))


class TestPortConfig(unittest.TestCase):
    def test_an_empty_filter_is_not_ready_under_the_stand_in(self):
        """**空名单的含义尚未定案**（规格把它列为待确认）。

        替身取「什么都不搬」——那是唯一不会让一台刚放下的接口立刻动手的
        解释。旧代码把存货口的空过滤当成「接收任意物品」，那是一次推断。
        """
        config = PortConfig()
        for role in (PASSIVE, INTAKE, OUTPUT, TEMPLATE):
            self.assertFalse(config.is_ready_for(role))
            self.assertFalse(config.is_ready_for(role, PortPolicy()))

    def test_the_empty_filter_knob_changes_it(self):
        """口径可注入：把空名单解释成「全部」时，存货口就开工了。"""
        policy = PortPolicy(empty_filter_means=EMPTY_MEANS_EVERYTHING)
        self.assertTrue(PortConfig().is_ready_for(INTAKE, policy))
        self.assertTrue(PortConfig().matches(STONE, policy))
        # 取货类**不**受这个口径影响：它的空名单永远是「没配置」。
        self.assertFalse(PortConfig().is_ready_for(OUTPUT, policy))

    def test_supply_slots_are_sorted_and_deduplicated(self):
        config = PortConfig(supply_slots=(3, 0, 3, 1))
        self.assertEqual(config.supply_slots, (0, 1, 3))

    def test_intake_slots_are_the_complement_of_supply_slots(self):
        """**两个集合不相交**——这就是「不来回搬」的结构性根据。"""
        config = PortConfig(supply_slots=(0, 4))
        self.assertEqual(config.intake_slots(6), [1, 2, 3, 5])
        self.assertEqual(
            sorted(list(config.supply_slots) + config.intake_slots(6)),
            [0, 1, 2, 3, 4, 5],
        )

    def test_only_the_pickup_roles_accept_a_blacklist(self):
        self.assertEqual(ROLE_FILTER_MODES[OUTPUT], frozenset([MODE_WHITELIST]))
        self.assertEqual(ROLE_FILTER_MODES[PASSIVE], frozenset([MODE_WHITELIST]))
        self.assertIn(MODE_BLACKLIST, ROLE_FILTER_MODES[INTAKE])

    def test_supply_slots_survive_a_round_trip(self):
        config = PortConfig(True, "both", (STONE,), 5, MODE_BLACKLIST, (0, 4))
        self.assertEqual(PortConfig.from_dict(config.to_dict()).to_dict(),
                         config.to_dict())

    def test_a_v1_payload_reads_as_the_old_semantics(self):
        """2026-09-25 之前的存档没有 `filter_mode` / `supply_slots`：
        那时只有白名单、也没有供货位，因此默认值正好是旧语义。"""
        legacy = {"enabled": True, "direction": "import",
                  "filter": [STONE.to_dict()], "limit": 7}
        config = PortConfig.from_dict(legacy)
        self.assertEqual(config.filter_mode, MODE_WHITELIST)
        self.assertEqual(config.supply_slots, ())
        self.assertEqual(config.limit, 7)

    def test_a_filtered_config_is_ready_for_every_role(self):
        config = PortConfig(filter=(STONE,))
        for role in (PASSIVE, INTAKE, OUTPUT, TEMPLATE):
            self.assertTrue(config.is_ready_for(role))

    def test_disabling_stops_every_role(self):
        config = PortConfig(enabled=False, filter=(STONE,))
        for role in (PASSIVE, INTAKE, OUTPUT, TEMPLATE):
            self.assertFalse(config.is_ready_for(role))

    def test_direction_gates_the_passive_exchange(self):
        self.assertTrue(PortConfig(direction=DIRECTION_BOTH).allows(DIRECTION_IMPORT))
        self.assertTrue(PortConfig(direction=DIRECTION_BOTH).allows(DIRECTION_EXPORT))
        only_out = PortConfig(direction=DIRECTION_EXPORT)
        self.assertFalse(only_out.allows(DIRECTION_IMPORT))
        self.assertTrue(only_out.allows(DIRECTION_EXPORT))

    def test_round_trip_through_dict(self):
        config = PortConfig(True, DIRECTION_IMPORT, (STONE, WATER), 12)
        self.assertEqual(PortConfig.from_dict(config.to_dict()).to_dict(),
                         config.to_dict())

    def test_from_dict_defaults_to_enabled(self):
        """**回归**：`from_dict` 曾经把 `enabled` 默认成 False，而构造器与
        文档都说是 True。界面只发一个 `filter` 时接口会被悄悄关掉，
        玩家看到的是「尚未配置」而真实原因是「被关上了」。"""
        restored = PortConfig.from_dict({"filter": [STONE.to_dict()]})
        self.assertTrue(restored.enabled)
        self.assertTrue(restored.is_ready_for(OUTPUT))

    def test_bad_values_are_rejected(self):
        with self.assertRaises(ValueError):
            PortConfig(direction="sideways")
        with self.assertRaises(ValueError):
            PortConfig(limit=-1)


class TestFacingResolution(unittest.TestCase):
    """朝向三种失败要分开报：它们要玩家做的事完全不同。"""

    def test_an_unknown_facing_is_reported_as_such(self):
        state = PortState(DeviceRef(key(1), 0), OUTPUT_DEVICE, OUTPUT, facing=None)
        ports = PortStore(None, _Registry(), containers=_Containers())
        endpoint, reason, _ = ports.facing_endpoint(state)
        self.assertIsNone(endpoint)
        self.assertEqual(reason, Reason.FACING_UNKNOWN)

    def test_an_empty_neighbour_is_reported_as_no_container(self):
        state = PortState(DeviceRef(key(1), 0), OUTPUT_DEVICE, OUTPUT, facing="east")
        ports = PortStore(None, _Registry(), containers=_Containers())
        endpoint, reason, _ = ports.facing_endpoint(state)
        self.assertIsNone(endpoint)
        self.assertEqual(reason, Reason.NO_CONTAINER)

    def test_a_missing_provider_is_no_container_not_an_empty_box(self):
        """**不能返回一个空容器**：那会被当成「有这个容器但它是空的」，
        于是取货口会一直往一个不存在的地方送货。"""
        state = PortState(DeviceRef(key(1), 0), OUTPUT_DEVICE, OUTPUT, facing="east")
        ports = PortStore(None, _Registry(), containers=None)
        endpoint, reason, _ = ports.facing_endpoint(state)
        self.assertIsNone(endpoint)
        self.assertEqual(reason, Reason.NO_CONTAINER)

    def test_a_resolved_facing_yields_an_endpoint(self):
        box = FakeContainer(slots=1)
        state = PortState(DeviceRef(key(1), 0), OUTPUT_DEVICE, OUTPUT, facing="east")
        ports = PortStore(
            None, _Registry(), containers=_Containers({key(2).as_key(): box})
        )
        endpoint, reason, detail = ports.facing_endpoint(state)
        self.assertIsNotNone(endpoint)
        self.assertEqual(reason, "")
        self.assertEqual(detail, "")


# -- 状态集合 --------------------------------------------------------------


class TestPortStore(unittest.TestCase):
    def setUp(self):
        self.registry = _Registry()
        self.ref = DeviceRef(key(0), 0)
        self.registry.add(self.ref, BUS, "north")
        self.store = PortStore(None, self.registry, containers=_Containers())

    def test_ensure_is_idempotent(self):
        first = self.store.ensure(self.ref, BUS)
        second = self.store.ensure(self.ref, BUS)
        self.assertIs(first, second)

    def test_only_buffered_roles_get_a_buffer(self):
        self.assertIsNotNone(self.store.ensure(self.ref, BUS).buffer)
        intake_ref = DeviceRef(key(1), 0)
        self.registry.add(intake_ref, INPUT)
        self.assertIsNone(self.store.ensure(intake_ref, INPUT).buffer)

    def test_an_unknown_device_yields_no_state(self):
        other = DeviceRef(key(9), 0)
        self.registry.add(other, "endernet:storage_driver")
        self.assertIsNone(self.store.ensure(other, "endernet:storage_driver"))

    def test_a_stale_generation_misses(self):
        self.store.ensure(self.ref, BUS)
        stale = DeviceRef(self.ref.node_key, self.ref.generation + 1)
        self.assertIsNone(self.store.get_by_ref(stale))
        self.assertIsNotNone(self.store.get_by_ref(self.ref))

    def test_removal_keeps_the_buffer_and_config(self):
        """**06 验收：外部物流已送入缓冲的资源仍保留。**"""
        state = self.store.ensure(self.ref, BUS)
        self.store.configure(self.ref, PortConfig(True, filter=(STONE,)))
        state.buffer.deposit("op", STONE, 7, ORIGIN_EXTERNAL)
        dropped = self.store.drop(self.ref)
        self.assertIsNotNone(dropped)
        self.assertEqual(dropped.buffer.total_of(STONE), 7)
        self.assertEqual(dropped.buffer.lots()[0]["origin"], ORIGIN_EXTERNAL)
        # **余料记录必须活得比 state 久**：写在 state 上会跟着对象一起消失，
        # 那 R05「拆除后余料处置」就等于没实现。
        self.assertEqual(len(self.store.disposed()), 1)
        report = self.store.disposed()[0]
        self.assertEqual(report["held"], 7)
        self.assertEqual(report["node_key"], self.ref.node_key.as_key())
        self.assertEqual(self.store.stats["disposed"], 7)

    def test_unloading_does_not_touch_config_or_buffer(self):
        state = self.store.ensure(self.ref, BUS)
        self.store.configure(self.ref, PortConfig(True, filter=(STONE,)))
        state.buffer.deposit("op", STONE, 3, ORIGIN_EXTERNAL)
        self.assertTrue(self.store.mark_loaded(self.ref.node_key, False))
        state = self.store.get(self.ref.node_key)
        self.assertFalse(state.loaded)
        self.assertEqual(state.buffer.total_of(STONE), 3)
        self.assertEqual(len(state.config.filter), 1)
        self.assertIsNotNone(port_blocker(state))
        self.assertEqual(port_blocker(state).code, Reason.DRIVER_UNLOADED)

    def test_mark_all_unloaded_is_a_stop_not_a_wipe(self):
        self.store.ensure(self.ref, BUS).buffer.deposit(
            "op", STONE, 5, ORIGIN_EXTERNAL
        )
        self.assertEqual(self.store.mark_all_unloaded(), 1)
        self.assertEqual(self.store.get(self.ref.node_key).buffer.total(), 5)

    def test_config_is_projected_to_the_device_record(self):
        """**这是全仓库第一个写 `DeviceRecord.config` 的地方**（除了 disk_count）。"""
        self.store.ensure(self.ref, BUS)
        self.store.configure(self.ref, PortConfig(True, filter=(STONE,)))
        record = self.registry.get(self.ref.node_key)
        self.assertEqual(record.config["port"]["filter"][0], STONE.to_dict())

    def test_observe_creates_and_drops_states(self):
        change = LifecycleChange(
            ChangeKind.ADDED, self.ref.node_key, BUS, self.ref
        )
        self.assertEqual(self.store.observe(change), 1)
        self.assertIsNotNone(self.store.get(self.ref.node_key))
        removed = LifecycleChange(
            ChangeKind.REMOVED, self.ref.node_key, BUS, self.ref
        )
        self.assertEqual(self.store.observe(removed), 1)
        self.assertIsNone(self.store.get(self.ref.node_key))

    def test_observe_ignores_non_port_devices(self):
        ref = DeviceRef(key(5), 0)
        change = LifecycleChange(
            ChangeKind.ADDED, ref.node_key, "endernet:storage_driver", ref
        )
        self.assertEqual(self.store.observe(change), 0)

    def test_round_trip_through_the_save_structure(self):
        state = self.store.ensure(self.ref, BUS)
        self.store.configure(self.ref, PortConfig(True, DIRECTION_IMPORT, (STONE,), 5))
        state.buffer.deposit("op", STONE, 6, ORIGIN_EXTERNAL, tick=3)
        state.batch = BatchState("batch-1", STONE, 9)
        state.batch.delivered = 4
        payload = self.store.save_all()

        fresh = PortStore(None, self.registry, containers=_Containers())
        self.assertEqual(fresh.restore(payload), 1)
        restored = fresh.get(self.ref.node_key)
        self.assertEqual(restored.buffer.total_of(STONE), 6)
        self.assertEqual(restored.buffer.lots()[0]["origin"], ORIGIN_EXTERNAL)
        self.assertEqual(restored.config.direction, DIRECTION_IMPORT)
        self.assertEqual(restored.config.limit, 5)
        self.assertEqual(restored.batch.delivered, 4)

    def test_restore_skips_positions_that_are_no_longer_that_device(self):
        self.store.ensure(self.ref, BUS)
        payload = self.store.save_all()
        self.registry.records[self.ref.node_key.as_key()] = _Record(
            self.ref, "endernet:storage_driver", None, None
        )
        fresh = PortStore(None, self.registry, containers=_Containers())
        self.assertEqual(fresh.restore(payload), 0)

    def test_an_unreadable_record_is_kept_not_read_as_empty(self):
        """05 明令：未知版本**不得读成空值再覆盖**。"""
        self.store.ensure(self.ref, BUS)
        payload = self.store.save_all()
        payload["states"][0]["buffer"]["schema_version"] = 99
        fresh = PortStore(None, self.registry, containers=_Containers())
        fresh.restore(payload)
        self.assertEqual(fresh.stats["unrecovered"], 1)
        self.assertEqual(len(fresh.unrecovered()), 1)
        self.assertEqual(fresh.unrecovered()[0]["node_key"], self.ref.node_key.as_key())

    def test_diagnostics_report_the_unresolved_rules(self):
        report = PortStore(None, _Registry(), containers=_Containers()).diagnostics()
        self.assertEqual(report["ports"], 0)
        self.assertTrue(report["unresolved"])
        self.assertIn(report["disposition"], ("retain",))


class TestExternalPortObjects(PortFixture):
    """对外那一侧的**实际能力对象**（架构 §4：不能只返回标签）。"""

    device_id = BUS
    facing = None

    def setUp(self):
        PortFixture.setUp(self)
        self.own_box = FakeContainer(slots=1, max_stack=64)
        self.containers.boxes[self.ref.node_key.as_key()] = self.own_box
        self.state.buffer.backing = ContainerEndpoint(self.own_box, "own-box")

    def test_the_state_carries_actual_capability_objects(self):
        """`has_capability` 为真而注册表里什么都没有，正是「已声明能力不等于
        已实例化能力对象」的形态。

        **注册范围取自定义表**：物品总线只挂物品端口，不顺手多挂一个流体端口
        ——多挂一样就多一个「声称能传、背后没有实现」的对象。
        """
        self.assertEqual(sorted(self.state.capabilities.names()), ["item_port"])
        self.assertIs(self.state.capabilities.get("item_port"), self.state.item_port)
        self.assertIsNone(self.state.fluid_port)
        self.assertIsNone(self.state.capabilities.get("fluid_port"))

    def test_an_external_insert_lands_in_the_bus_buffer_as_external(self):
        """**外部物流的脚本层入口**：放进去的东西带 `external` 来源。"""
        receipt = self.state.item_port.insert("ext-1", STONE, 7)
        self.assertEqual(receipt.status, TransferStatus.COMPLETE)
        self.assertEqual(receipt.moved, 7)
        self.assertEqual(self.state.buffer.total_of(STONE), 7)
        self.assertEqual(self.own_box.count_of(STONE), 7)
        lot = self.state.buffer.lots()[0]
        self.assertEqual(lot["origin"], ORIGIN_EXTERNAL)
        self.assertEqual(lot["owner_id"], "")

    def test_an_external_extract_takes_it_back_out(self):
        self.state.item_port.insert("ext-1", STONE, 7)
        receipt = self.state.item_port.extract("ext-2", STONE, 4)
        self.assertEqual(receipt.moved, 4)
        self.assertEqual(self.state.buffer.total_of(STONE), 3)

    def test_the_item_port_never_touches_the_network(self):
        """**它不是业务命令的旁路**：网络存量根本不在这条路径上。"""
        self.state.item_port.insert("ext-1", STONE, 5)
        self.assertEqual(self.network_stub.stored[STONE], 40, "网络存量一字未动")
        self.assertEqual(self.network_stub.inserts, [])

    def test_the_port_describes_which_side_is_external(self):
        self.assertEqual(
            self.state.item_port.describe()["external_side"], "buffer"
        )

    def test_a_fluid_port_object_refuses_with_a_reason(self):
        """13-03 未做：**明确拒绝而不是「搬了 0 个」**，后者会被读成
        「这台容器是空的」。"""
        ref = DeviceRef(key(9), 0)
        self.registry.add(ref, FLUID_BUS)
        fluid = self.ports.ensure(ref, FLUID_BUS).capabilities.get("fluid_port")
        receipt = fluid.insert("f1", WATER, 100)
        self.assertEqual(receipt.status, TransferStatus.UNSUPPORTED)
        self.assertEqual(receipt.reason, Reason.NO_ADAPTER)
        self.assertEqual(receipt.moved, 0)
        self.assertFalse(fluid.describe()["available"])
        self.assertEqual(fluid.describe()["pending_task"], "13-03")
        # 撤掉旧的 `self.state.capabilities.get("fluid_port")` 断言：
        # 物品总线不该有流体端口对象（注册范围取自定义表）。
        self.assertIsNone(self.state.capabilities.get("fluid_port"))

    def test_capability_objects_survive_a_drop(self):
        """拆除时状态被移除，但对象本身仍然可用（诊断要能解释发生了什么）。"""
        port = self.state.item_port
        self.ports.drop(self.ref)
        self.assertEqual(port.describe()["role"], PASSIVE)


class TestFacingSideExternalPort(unittest.TestCase):
    """存货口/取货口的对外那一侧是**朝向的容器**，不是自己的缓冲。"""

    def setUp(self):
        registry = _Registry()
        self.ref = DeviceRef(key(1), 0)
        registry.add(self.ref, OUTPUT_DEVICE, "east")
        self.box = FakeContainer(slots=1, max_stack=64)
        self.ports = PortStore(
            None, registry, containers=_Containers({key(2).as_key(): self.box})
        )
        self.state = self.ports.ensure(self.ref, OUTPUT_DEVICE, "east")

    def test_there_is_no_local_buffer_to_talk_about(self):
        self.assertIsNone(self.state.buffer)

    def test_the_external_side_is_the_facing_container(self):
        self.assertEqual(
            self.state.item_port.describe()["external_side"], "facing"
        )
        receipt = self.state.item_port.insert("ext-1", STONE, 6)
        self.assertEqual(receipt.moved, 6)
        self.assertEqual(self.box.count_of(STONE), 6)

    def test_a_missing_container_refuses_with_a_locating_reason(self):
        ports = PortStore(None, _Registry(), containers=_Containers())
        state = ports.ensure(self.ref, OUTPUT_DEVICE, "east")
        receipt = state.item_port.insert("ext-1", STONE, 6)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.reason, Reason.NO_CONTAINER)


class TestPortPolicy(unittest.TestCase):
    """**已定案的槽数不在策略里。**

    槽数进定义表且不带 unresolved 标签——把已经定案的数值继续挂在替身规则上，
    等于让正式验收为一条不存在的规则盖章。
    """

    def test_settled_slot_counts_are_not_in_the_policy(self):
        fields = set(item["field"] for item in PortPolicy().unresolved_report())
        for field in ("port.passive_slot_count",
                      "port.template_pattern_slot_count",
                      "port.pickup_filter_limit"):
            self.assertNotIn(field, fields)

    def test_the_pickup_limit_comes_from_the_definition_table(self):
        self.assertEqual(
            PortPolicy().filter_limit_for(OUTPUT), PICKUP_FILTER_LIMIT.value
        )
        self.assertEqual(
            PortPolicy().filter_limit_for(PASSIVE), PICKUP_FILTER_LIMIT.value
        )

    def test_the_intake_limit_is_not_derived_from_the_pickup_one(self):
        """**文档明写不得从取货的 9 种推导存货过滤上限**，替身因此刻意
        取一个不等于 9 的值——共用一个数就是那条禁令要防的事。"""
        policy = PortPolicy()
        self.assertNotEqual(
            policy.filter_limit_for(INTAKE), PICKUP_FILTER_LIMIT.value
        )

    def test_unresolved_fields_carry_their_rule_numbers(self):
        report = PortPolicy().unresolved_report()
        self.assertTrue(report)
        for item in report:
            self.assertTrue(item["field"].startswith("port."))
            self.assertEqual(item["rule"], "R05")

    def test_assert_settled_refuses_to_declare_victory_early(self):
        with self.assertRaises(AssertionError):
            PortPolicy().assert_settled()

    def test_bad_numbers_are_rejected(self):
        for kwargs in ({"template_buffer_capacity": 0},
                       {"intake_filter_limit": 0},
                       {"max_batch": 0}, {"probe_unit": 0},
                       {"default_filter_mode": "whatever"},
                       {"empty_filter_means": "whatever"}):
            with self.assertRaises(ValueError):
                PortPolicy(**kwargs)


# -- 驱动 ------------------------------------------------------------------


class TestPortRunner(unittest.TestCase):
    def setUp(self):
        self.registry = _Registry()
        self.ref = DeviceRef(key(0), 0)
        self.registry.add(self.ref, BUS, "north")
        self.ports = PortStore(None, self.registry, containers=_Containers())
        # 注册轮询需要一个**已存在的接口状态**（`add_node` 与 `PortStore.ensure`
        # 是两条路，顺序由服务侧保证：先建状态，再注册）。
        self.ports.ensure(self.ref, BUS, "north")
        self.scheduler = _Scheduler()
        self.runner = PortRunner(self.ports, PortGateway(None))

    def node(self, generation=0):
        return _Node(self.ref, generation)

    def test_the_poll_handler_is_registered_exactly_once_per_instance(self):
        """**没有这一条就是缺陷**：`set_poll_handler` 每调用一次就把退避打回
        初值，而 `add_node` 对每个成员反复调用——每次都在里面注册的话，
        退避永远形同虚设（表现为「每帧都在轮询」）。"""
        node = self.node()
        self.assertTrue(self.runner.attach(node, self.scheduler))
        self.assertFalse(self.runner.attach(node, self.scheduler))
        self.assertFalse(self.runner.attach(node, self.scheduler))
        self.assertEqual(len(self.scheduler.polls), 1)

    def test_a_new_generation_gets_its_own_registration(self):
        """同位置换了一台设备就是**另一条 lane**（调度器按位置+代次建 lane），
        因此新实例要单独注册一次。"""
        self.runner.attach(self.node(0), self.scheduler)
        self.ports.ensure(DeviceRef(self.ref.node_key, 1), BUS, "north")
        self.assertTrue(self.runner.attach(self.node(1), self.scheduler))
        self.assertEqual(len(self.scheduler.polls), 2)

    def test_attach_without_a_state_does_nothing(self):
        """服务侧的顺序是「先建状态、再注册轮询」；没状态就不该注册。"""
        self.ports.drop(self.ref)
        self.assertFalse(self.runner.attach(self.node(), self.scheduler))
        self.assertEqual(self.scheduler.polls, {})

    def test_detach_unregisters_and_cancels(self):
        node = self.node()
        self.runner.attach(node, self.scheduler)
        self.assertTrue(self.runner.detach(node, self.scheduler))
        self.assertEqual(self.scheduler.polls[self.ref.as_key()], (None, None))
        self.assertEqual(self.scheduler.cancelled, [self.ref.as_key()])
        self.assertFalse(self.runner.detach(node, self.scheduler))

    def test_wakes_are_counted_but_change_nothing(self):
        """唤醒只把退避打回初值（那件事归调度器），不发起搬运。"""
        self.runner.attach(self.node(), self.scheduler)
        state = self.ports.get(self.ref.node_key)
        self.assertTrue(self.runner.note_wake(self.ref.node_key))
        self.assertEqual(state.stats["wakes"], 1)
        self.assertEqual(state.stats["dispatches"], 0)


class _Node(object):
    """`NetworkNode` 的替身：接口驱动只用到 ref / node_key / device_id / facing。
    """

    def __init__(self, ref, generation=None):
        self.ref = ref
        self.node_key = ref.node_key
        self.device_id = "endernet:bus_item_io"
        self.facing = "north"
        if generation is not None and generation != ref.generation:
            self.ref = DeviceRef(ref.node_key, generation)


class TestRunnerWithoutAWorkingNetwork(unittest.TestCase):
    """**「主控无效或断电不继续抽取源容器」在这里是结构性的**：
    接口连命令都发不出去，更别说去动源容器了。"""

    def setUp(self):
        registry = _Registry()
        self.ref = DeviceRef(key(0), 0)
        registry.add(self.ref, BUS, None)
        # 被动接口的 9 格就是它自己的方块容器，因此容器必须真的在
        # 容器提供者里——导入扫的是**存货位**，不是缓冲台账。
        self._box = FakeContainer(
            slots=PASSIVE_PORT_SLOT_COUNT.value, max_stack=64
        )
        self.ports = PortStore(
            None, registry, containers=_Containers({key(0).as_key(): self._box})
        )
        self.state = self.ports.ensure(self.ref, BUS, None)
        self.ports.configure(self.ref, PortConfig(True, filter=(STONE,)))
        # 存货位（默认 9 格全是）里放 5 个，模拟外部物流已送入的东西。
        self._box.set_slot(0, ContainerEntry(STONE, 5))

    def test_no_master_means_the_poll_does_nothing(self):
        runner = PortRunner(self.ports, PortGateway(None))
        self.assertFalse(runner.gateway.available())
        outcome = runner._poll(_Task(self.ref))
        self.assertFalse(outcome.changed)
        self.assertEqual(self._box.count_of(STONE), 5, "存货位原样不动")

    def test_a_closed_runtime_also_does_nothing(self):
        runner = PortRunner(
            self.ports, PortGateway(_FakeRuntime(master=_Master(), open_=False))
        )
        outcome = runner._poll(_Task(self.ref))
        self.assertFalse(outcome.changed)
        self.assertEqual(self._box.count_of(STONE), 5, "存货位原样不动")

    def test_no_dispatch_when_the_port_is_held(self):
        """**不确定之后一律不重试**，哪怕网络好端端的。"""
        runner = PortRunner(
            self.ports, PortGateway(_FakeRuntime(master=_Master()))
        )
        self.state.hold = Reason.CONTAINER_UNCERTAIN
        outcome = runner._poll(_Task(self.ref))
        self.assertFalse(outcome.changed)
        self.assertEqual(self.state.stats["dispatches"], 0)

    def test_an_uncertain_dispatch_stops_the_pipeline(self):
        """**回归**：`receipt_result` 曾经对 `UNCERTAIN` 返回不带 payload 的
        `blocked`，于是轮询读不到回执里的 `status`，`hold` 永远不会被设上——
        整个「不确定不重放」的闸门在生产路径上是死的，每一轮都会换一个新
        op_id 再搬一次。
        """
        class UncertainNetwork(_NetworkStub):
            def commit_insert(self, op_id, key, count, owner_id=""):
                return TransferReceipt(
                    op_id, count, 0, TransferStatus.UNCERTAIN,
                    Reason.CONTAINER_UNCERTAIN, key,
                )

        stub = UncertainNetwork({STONE: 5})
        # **物品必须真的在那个方块容器的存货位里**：被动接口的导入只扫
        # 存货位（供货位的补集），往缓冲台账里塞东西是不算数的——
        # 台账记的是归属，实际的货在容器里。
        self._box.set_slot(0, ContainerEntry(STONE, 5))
        self.ports.configure(self.ref, PortConfig(True, DIRECTION_IMPORT, (STONE,)))
        runner = PortRunner(self.ports, PortGateway(_FakeRuntime(master=_Master())))
        runner.gateway.runtime.master.submit = lambda command: self.server_dispatch(
            command, stub
        )
        outcome = runner._poll(_Task(self.ref))
        self.assertFalse(outcome.changed)
        self.assertEqual(self.state.hold, Reason.CONTAINER_UNCERTAIN)
        self.assertIn("不确定", self.state.hold_detail)

    def server_dispatch(self, command, stub):
        """用给定的网络替身跑一次接口派发，返回一条命令结果。"""
        dispatcher = PortDispatcher(
            self.ports, TransferCoordinator(),
            NetworkEndpoint(stub, "c1"),
        )
        return dispatcher.dispatch(self.state, command.request_id)

    def test_an_unloaded_port_is_not_polled_into_action(self):
        runner = PortRunner(
            self.ports, PortGateway(_FakeRuntime(master=_Master()))
        )
        self.ports.mark_loaded(self.ref.node_key, False)
        outcome = runner._poll(_Task(self.ref))
        self.assertFalse(outcome.changed)
        self.assertEqual(self.state.stats["dispatches"], 0)


class _Task(object):
    def __init__(self, ref):
        self.ref = ref


# -- 命令处理：取货口（06-06）----------------------------------------------


class TestOutputPort(PortFixture):
    """取货口：按过滤从网络取，向朝向容器供给。"""

    device_id = OUTPUT_DEVICE

    def test_unconfigured_ports_do_nothing(self):
        result = self.result()
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.payload["moved"], 0)
        self.assertIn("没有配置的过滤资源", result.payload["note"])

    def test_it_supplies_the_facing_container(self):
        self.configure()
        result = self.result()
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(self.box.count_of(STONE), 32, "受 max_batch 限制")
        self.assertEqual(self.network_stub.stored[STONE], 8)

    def test_an_empty_network_is_idle_not_an_error(self):
        self.configure()
        self.network_stub.stored[STONE] = 0
        result = self.result()
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.payload["moved"], 0)

    def test_a_full_container_leaves_the_network_untouched(self):
        """**不丢弃余量**：送不进去的部分不能从网络里白扣。"""
        self.configure()
        self.box.max_stack = 0
        result = self.result()
        self.assertEqual(result.payload["moved"], 0)
        self.assertEqual(self.network_stub.stored[STONE], 40, "一个都没动")

    def test_a_partial_target_returns_the_leftover_to_the_network(self):
        """目标只收一部分时，余量**退回网络**，不是丢在地上。"""
        self.configure()
        self.containers.boxes[key(2).as_key()] = _HalfFillingBox(
            slots=1, max_stack=64, accept=5
        )
        result = self.result()
        receipt = result.payload["receipt"]
        self.assertEqual(receipt["moved"], 5)
        self.assertEqual(self.network_stub.stored[STONE], 40 - 5, "只扣掉真的送出去的")

    def test_a_target_without_simulate_is_probed_one_at_a_time(self):
        """目标不支持模拟时**只试探一个**：多搬的那部分要回滚，
        而回滚本身可能失败——宁可慢。"""
        self.configure()
        self.containers.boxes[key(2).as_key()] = NoSimulateContainer(slots=1)
        before = self.network_stub.stored[STONE]
        receipt = self.result().payload["receipt"]
        self.assertLessEqual(receipt["moved"], self.policy.probe_unit)
        self.assertGreaterEqual(self.network_stub.stored[STONE], before - 1)

    def test_only_the_configured_filter_moves(self):
        self.configure(filter=(DIRT,))
        self.network_stub.stored[DIRT] = 6
        result = self.result()
        self.assertEqual(result.payload["receipt"]["moved"], 6)
        self.assertEqual(self.box.count_of(STONE), 0)

    def test_a_fluid_port_refuses_instead_of_pretending(self):
        """流体真实适配属 13-03：**明确回 no_adapter**，不是安静地搬 0 个。"""
        ref = DeviceRef(key(0), 0)
        self.registry.add(ref, FLUID_BUS, "east")
        state = self.ports.ensure(ref, FLUID_BUS, "east")
        self.ports.configure(ref, PortConfig(True, filter=(WATER,)))
        result = self.dispatcher.dispatch(state, "r")
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn(Reason.NO_ADAPTER, result.reason_codes())


class _HalfFillingBox(FakeContainer):
    """只在某些槽里放得下这么多：用来精确控制「只收一部分」。"""

    adapter_id = "half_filling"

    def __init__(self, accept, **kwargs):
        FakeContainer.__init__(self, **kwargs)
        self.accept = accept

    def _do_insert(self, op_id, resource_key, count):
        return FakeContainer._do_insert(
            self, op_id, resource_key, min(count, self.accept)
        )


# -- 命令处理：存货口（06-05）----------------------------------------------


class TestIntakePort(PortFixture):
    """存货口：读朝向容器的内容存进网络。"""

    device_id = INPUT

    def test_an_empty_whitelist_moves_nothing_under_the_stand_in(self):
        """**空名单的含义尚未定案**，替身口径取「什么都不搬」。

        旧实现把存货口的空过滤当成「接收任意物品」——那是一次**推断**，
        而 2026-09-25 的规格把「空名单行为」明确列为待确认。现在它走
        `PortPolicy.empty_filter_means`，替身取最保守的那一侧。
        """
        self.box.set_slot(0, ContainerEntry(STONE, 9))
        self.configure(filter=())
        self.assertFalse(self.state.config.is_ready_for(INTAKE, self.ports.policy))
        # **门在 `port_blocker`**：轮询、命令处理与只读诊断共用它。
        # 命令处理器本身不重复判定（那正是「一份门」的意思）。
        blocker = port_blocker(self.state, self.ports.policy)
        self.assertIsNotNone(blocker)
        self.assertEqual(blocker.code, Reason.PORT_UNCONFIGURED)

    def test_a_blacklist_takes_everything_but_the_listed(self):
        """存货口支持黑名单：名单外的都搬。"""
        self.box.set_slot(0, ContainerEntry(STONE, 9))
        self.box.set_slot(1, ContainerEntry(DIRT, 4))
        self.configure(filter=(STONE,), filter_mode=MODE_BLACKLIST)
        result = self.result()
        self.assertEqual(result.payload["receipt"]["moved"], 4, "只搬泥土")
        self.assertEqual(self.box.count_of(STONE), 9, "被拉黑的不动")

    def test_a_filter_narrows_what_is_taken(self):
        self.box.set_slot(0, ContainerEntry(STONE, 9))
        self.box.set_slot(1, ContainerEntry(DIRT, 4))
        self.configure(filter=(DIRT,))
        self.result()
        self.assertEqual(self.box.count_of(STONE), 9, "没配置的不动")
        self.assertEqual(self.network_stub.stored.get(DIRT, 0), 4)

    def test_an_empty_source_is_idle(self):
        self.configure()
        result = self.result()
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.payload["moved"], 0)

    def test_the_resource_order_is_the_configured_order(self):
        """每轮只搬一种，**「先搬什么」按玩家的配置顺序**而不是容器槽位顺序：
        后者取决于谁先被放进去，结果不可解释。"""
        self.network_stub = _NetworkStub({})
        self.network = NetworkEndpoint(self.network_stub, "c1")
        self.dispatcher = PortDispatcher(self.ports, self.coordinator, self.network)
        self.box.set_slot(0, ContainerEntry(STONE, 3))
        self.box.set_slot(1, ContainerEntry(DIRT, 3))
        self.configure(filter=(DIRT, STONE))
        self.result()
        self.assertEqual(self.network_stub.stored.get(DIRT, 0), 3)
        self.assertEqual(self.network_stub.stored.get(STONE, 0), 0)

    def test_it_records_an_external_input(self):
        """**从外面搬进来的就是外部输入**，要为订单结果累加器贡献一次。"""
        delivh = []
        self.coordinator.sink = lambda request, count, tick: delivh.append(
            (request.op_id, count)
        )
        self.box.set_slot(0, ContainerEntry(STONE, 5))
        self.configure()
        self.result("intake-1")
        self.assertEqual(delivh, [("intake-1", 5)])

    def test_a_refusing_network_leaves_the_items_where_they_were(self):
        """**网络拒收部分留在可追踪位置**——退回它本来的家（朝向容器）。"""
        self.box.set_slot(0, ContainerEntry(STONE, 5))
        self.configure()
        dispatcher = PortDispatcher(
            self.ports, self.coordinator, NetworkEndpoint(_RefusingNetwork(), "c1")
        )
        result = dispatcher.dispatch(self.state, "r1")
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertEqual(self.box.count_of(STONE), 5, "原样留在容器里")


class _RefusingNetwork(object):
    def is_ready(self):
        return True

    def query(self, key):
        return {"stored": 0, "reserved": 0, "available": 0, "version": 1}

    def simulate_insert(self, key, count):
        return {"accepted": 0}

    def simulate_extract(self, key, count):
        return {"accepted": 0}

    def commit_insert(self, op_id, key, count, owner_id=""):
        return TransferReceipt(
            op_id, count, 0, TransferStatus.REFUSED, Reason.NO_CAPACITY, key
        )

    def commit_extract(self, op_id, key, count, owner_id=""):
        return TransferReceipt(
            op_id, count, 0, TransferStatus.REFUSED,
            Reason.INSUFFICIENT_STOCK, key,
        )


# -- 命令处理：被动总线（06-04）--------------------------------------------


class TestPassiveBus(PortFixture):
    """被动接口：外部物流写进它的 9 格，网络按配置与它交换。

    **这一组用例的核心是「不来回搬」**：存货物只往网络走、供货物只从网络来，
    两个方向作用在不相交的槽上。旧实现两个方向共用一份过滤与一份缓冲，
    刚补进去的供货资源下一轮就被搬回网络——真机上表现为无休止的来回搬。
    """

    device_id = BUS
    facing = None

    def setUp(self):
        PortFixture.setUp(self)
        # 被动接口的 9 格是**存取共用的真实库存**，就是它自己的方块容器。
        self.own_box = FakeContainer(
            slots=PASSIVE_PORT_SLOT_COUNT.value, max_stack=64
        )
        self.containers.boxes[self.ref.node_key.as_key()] = self.own_box

    def configure(self, **kwargs):
        kwargs.setdefault("filter", (STONE,))
        return PortFixture.configure(self, **kwargs)

    def put_outside(self, count, slot=3):
        """外部物流往**存货位**里放东西（模拟别的模组/漏斗）。"""
        return self.own_box.set_slot(slot, ContainerEntry(STONE, count)) or self.own_box

    def test_an_import_only_drains_intake_slots(self):
        """外部放进存货位的东西被搬进网络，且来源是 `external`。"""
        delivered = []
        self.coordinator.sink = lambda request, count, tick: delivered.append(count)
        before = self.network_stub.stored[STONE]
        self.put_outside(7)
        # 一个供货位都不指定：9 格全是存货位。
        self.configure(direction=DIRECTION_IMPORT)
        result = self.result("imp-1")
        self.assertEqual(result.payload["receipt"]["moved"], 7)
        self.assertEqual(self.network_stub.stored[STONE], before + 7)
        self.assertEqual(delivered, [7], "外部送进来的那一份要发一次回流")

    def test_an_export_only_fills_supply_slots(self):
        """网络只往**供货位**补货，且不覆盖存货位里的东西。"""
        delivered = []
        self.coordinator.sink = lambda request, count, tick: delivered.append(count)
        self.put_outside(5, slot=3)
        self.configure(direction=DIRECTION_EXPORT, supply_slots=(0,))
        result = self.result("exp-1")
        self.assertEqual(result.payload["receipt"]["moved"], 32, "受 max_batch 限制")
        self.assertEqual(self.own_box.count_of(STONE), 5 + 32)
        # 供货位是槽 0，存货位（槽 3）原样不动。
        self.assertEqual(self.own_box._slots[0].count, 32)
        self.assertEqual(self.own_box._slots[3].count, 5)
        self.assertEqual(delivered, [], "网络往外搬不是外部输入")

    def test_supply_does_not_get_imported_back(self):
        """**回归：不来回搬。**

        补进供货位的货落在槽 0，而导入只扫存货位——因此它**不可能**被自己
        搬回网络。旧实现里这一条会失败：两个方向共用一份过滤与缓冲。
        """
        self.configure(direction=DIRECTION_BOTH, supply_slots=(0,))
        # 先让网络把货补进供货位。
        first = self.result("round-1")
        self.assertEqual(first.payload["receipt"]["moved"], 32)
        self.assertEqual(self.own_box._slots[0].count, 32)
        # **把网络抽干**，否则「保持满」会一直往上补，分不清是补货还是回流。
        for key in list(self.network_stub.stored):
            self.network_stub.stored[key] = 0
        for index in range(3):
            self.result("round-%d" % (index + 2))
        self.assertEqual(
            self.own_box._slots[0].count, 32,
            "供货位上的货被搬回网络了——这就是来回搬",
        )
        self.assertEqual(self.network_stub.stored[STONE], 0, "一个都没进网络")

    def test_a_both_way_dispatch_prefers_intake_over_supply(self):
        """同一轮里两个方向都开着：先清存货位，再补供货位。"""
        self.put_outside(4, slot=3)
        self.configure(direction=DIRECTION_BOTH, supply_slots=(0,))
        result = self.result("both-1")
        # 导入先跑：外部那 4 个进了网络。
        self.assertEqual(result.payload["receipt"]["moved"], 4)
        self.assertEqual(self.own_box._slots[3], None, "存货位被清空")
        self.assertEqual(self.network_stub.stored[STONE], 40 + 4)

    def test_default_config_supplies_nothing(self):
        """一个供货位都不指定时，接口只导入、不供货——
        **不替玩家猜一个供货策略**。"""
        self.configure(direction=DIRECTION_BOTH)
        self.assertEqual(self.state.config.supply_slots, ())
        self.assertEqual(self.result().payload["moved"], 0)
        self.assertEqual(self.own_box.count_of(STONE), 0)

    def test_a_blacklisted_resource_is_not_imported(self):
        """存货位里的东西也受过滤约束（这里是白名单：只搬 STONE）。"""
        self.own_box.set_slot(3, ContainerEntry(DIRT, 4))
        self.configure(direction=DIRECTION_IMPORT)
        self.assertEqual(self.result().payload["moved"], 0)
        self.assertEqual(self.own_box.count_of(DIRT), 4, "不在名单里的不动")

    def test_no_container_is_blocked_not_silently_idle(self):
        """那个位置不是容器时要**明确报出来**，不是安静地什么都不做。"""
        self.containers.boxes.pop(self.ref.node_key.as_key())
        self.configure(direction=DIRECTION_IMPORT)
        result = self.result()
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn(Reason.NO_ADAPTER, result.reason_codes())


class TestTemplateDoesNotTouchPatternSlots(PortFixture):
    """**06-07：投料不得占用样板槽。**

    样板接口的 9 格**专用于工厂样板**（归任务 07），原料缓冲是独立的
    内存缓冲。旧实现把方块自身容器挂成原料中转位，那正是冲突的根源。
    """

    device_id = TEMPLATE_DEVICE

    def setUp(self):
        PortFixture.setUp(self)
        # 这台设备位置上**有**一个容器（装工厂样板用的），06 不该碰它。
        self.pattern_box = FakeContainer(
            slots=TEMPLATE_PATTERN_SLOT_COUNT.value, max_stack=64
        )
        self.containers.boxes[self.ref.node_key.as_key()] = self.pattern_box

    def test_the_pattern_container_is_not_bound_as_the_material_buffer(self):
        endpoint = self.ports.buffer_endpoint(self.state, "probe")
        self.assertIsNotNone(endpoint)
        self.assertIsNone(
            endpoint.buffer.backing,
            "原料缓冲不该挂在样板容器上——那会让投料占用样板槽",
        )
        self.assertEqual(self.state.buffer_kind, BUFFER_MEMORY)

    def test_staging_and_feeding_leave_the_pattern_slots_alone(self):
        """放一个样板进样板槽，然后备料 + 投料，样板槽必须原样不动。"""
        self.pattern_box.set_slot(0, ContainerEntry(DIRT, 1))  # 假装是一个样板
        self.configure(filter=(STONE,))
        self.dispatcher.stage_batch(self.state, STONE, 10, "batch-op", "r1")
        self.assertEqual(self.result("feed-1").payload["batch"]["delivered"], 10)
        self.assertEqual(self.pattern_box.count_of(DIRT), 1, "样板槽被动过了")
        self.assertEqual(self.pattern_box.slots_count(),
                         TEMPLATE_PATTERN_SLOT_COUNT.value)
        self.assertEqual(self.box.count_of(STONE), 10, "原料进了朝向的箱子")

    def test_the_passive_port_does_bind_its_own_container(self):
        """反过来：被动接口**要**挂自己的容器（那 9 格就是它的库存）。"""
        ref = DeviceRef(key(7), 0)
        self.registry.add(ref, BUS, None)
        self.containers.boxes[key(7).as_key()] = FakeContainer(
            slots=PASSIVE_PORT_SLOT_COUNT.value, max_stack=64
        )
        state = self.ports.ensure(ref, BUS, None)
        endpoint = self.ports.buffer_endpoint(state, "probe")
        self.assertIsNotNone(endpoint.buffer.backing)
        self.assertEqual(state.buffer_kind, BUFFER_CONTAINER)


# -- 命令处理：样板口（06-07）----------------------------------------------


class TestTemplatePort(PortFixture):
    """样板口：成批备料 + 本地缓冲回执。"""

    device_id = TEMPLATE_DEVICE

    def test_staging_takes_a_batch_from_the_network(self):
        self.configure()
        result = self.dispatcher.stage_batch(
            self.state, STONE, 20, "batch-op", "r1"
        )
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.payload["batch"]["staged"], 20)
        self.assertEqual(result.payload["batch"]["requested"], 20)
        self.assertEqual(self.state.buffer.total_of(STONE), 20)

    def test_feeding_delivers_the_batch_to_the_facing_container(self):
        self.configure()
        self.dispatcher.stage_batch(self.state, STONE, 10, "batch-op", "r1")
        result = self.result("feed-1")
        batch = result.payload["batch"]
        self.assertEqual(batch["delivered"], 10)
        self.assertTrue(batch["remaining"] == 0)
        self.assertEqual(self.box.count_of(STONE), 10)

    def test_a_partial_delivery_keeps_the_batch_state(self):
        """**外部机器部分接收时保留批次状态，交由 09 决定后续推进。**
        本模块不替它决定（不重投、不取消）。"""
        self.configure()
        self.containers.boxes[key(2).as_key()] = _HalfFillingBox(
            slots=1, max_stack=64, accept=4
        )
        self.dispatcher.stage_batch(self.state, STONE, 10, "batch-op", "r1")
        result = self.result("feed-1")
        batch = result.payload["batch"]
        self.assertEqual(batch["delivered"], 4)
        self.assertEqual(batch["remaining"], 6)
        self.assertIn("只接收了一部分", batch["reason"])
        # 余量还在中转位里，**位置可追踪**。
        self.assertEqual(self.state.buffer.total_of(STONE), 6)

    def test_feeding_without_a_batch_is_idle(self):
        self.configure()
        result = self.result()
        self.assertEqual(result.payload["moved"], 0)
        self.assertIn("没有待投的样板批次", result.payload["note"])

    def test_a_batch_whose_buffer_dried_up_is_blocked_not_guessed(self):
        """中转位空了但批次没完成：**不猜**，交给 09 重新决定。"""
        self.configure()
        self.dispatcher.stage_batch(self.state, STONE, 10, "batch-op", "r1")
        self.state.buffer.withdraw("drain", STONE, 10)
        result = self.result("feed-1")
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn(Reason.INSUFFICIENT_STOCK, result.reason_codes())

    def test_a_replayed_batch_does_not_stage_twice(self):
        self.configure()
        self.dispatcher.stage_batch(self.state, STONE, 10, "batch-op", "r1")
        self.dispatcher.stage_batch(self.state, STONE, 10, "batch-op", "r1")
        self.assertEqual(self.state.buffer.total_of(STONE), 10)
        self.assertEqual(len(self.network_stub.extracts), 1)


# -- 角色越权与占位 --------------------------------------------------------


class TestDispatcherGuards(PortFixture):
    def test_a_disabled_port_reports_unconfigured(self):
        self.ports.configure(self.ref, PortConfig(enabled=False, filter=(STONE,)))
        blocker = port_blocker(self.state)
        self.assertEqual(blocker.code, Reason.PORT_UNCONFIGURED)

    def test_two_ports_of_the_same_role_get_different_request_ids(self):
        """**回归**：`request_id` 曾经只有角色与序号，不含设备。

        存储服务自己的账本键是 `(op_id, 方向)`（不含设备），因此两台同角色的
        接口会生成一模一样的 op_id：后一台的写入被当成前一台的「重发」，
        回执说成功、东西没进网络，而它自己那份已经扣掉了。
        """
        first = PortState(DeviceRef(key(1), 0), BUS, PASSIVE,
                          PortConfig(True, filter=(STONE,)), TransferBuffer(8, "a"))
        second = PortState(DeviceRef(key(2), 0), BUS, PASSIVE,
                           PortConfig(True, filter=(STONE,)), TransferBuffer(8, "b"))
        self.assertNotEqual(first.next_request_id(), second.next_request_id())

    def test_the_gateway_refuses_to_submit_without_a_master(self):
        gateway = PortGateway(None)
        self.assertFalse(gateway.available())
        self.assertIsNone(gateway.submit(None))
        self.assertIsNone(gateway.topology_version())


class _FakeRuntime(object):
    def __init__(self, master=None, open_=True, version=3):
        self.master = master
        self._open = open_
        self.topology = _Topology(version)

    def is_open(self):
        return self._open


class _Topology(object):
    def __init__(self, version):
        self.version = version


class _Master(object):
    def __init__(self):
        self.commands = []

    def submit(self, command):
        self.commands.append(command)
        return CommandResult.ok(getattr(command, "request_id", "r"), {"moved": 0})


class TestPortGateway(unittest.TestCase):
    def test_a_closed_runtime_is_not_available(self):
        """关闭中的运行时**不该把命令交出去**：那会以异常的形式冒到帧边界上。"""
        gateway = PortGateway(_FakeRuntime(master=_Master(), open_=False))
        self.assertFalse(gateway.available())
        self.assertIsNone(gateway.submit(None))

    def test_an_open_runtime_with_a_master_submits_through_it(self):
        master = _Master()
        gateway = PortGateway(_FakeRuntime(master=master))
        self.assertTrue(gateway.available())
        self.assertEqual(gateway.topology_version(), 3)
        gateway.submit("command")
        self.assertEqual(master.commands, ["command"])

    def test_the_runner_builds_a_device_originated_command(self):
        master = _Master()
        runner = PortRunner(
            PortStore(None, _Registry(), containers=_Containers()),
            PortGateway(_FakeRuntime(master=master)),
        )
        state = PortState(
            DeviceRef(key(0), 0), BUS, PASSIVE, PortConfig(True, filter=(STONE,)),
            TransferBuffer(64, "p"),
        )
        command = runner._build_command(state)
        self.assertEqual(command.kind, "storage")
        self.assertIsNone(command.source.player_id, "接口不是玩家")
        self.assertEqual(command.topology_version, 3)
        self.assertEqual(command.device_ref, state.ref)
        self.assertEqual(command.payload["intent"], PASSIVE)
        # **必须带设备位置**：存储服务的账本键是 `(op_id, 方向)`，不含设备，
        # 因此两台同角色的接口若生成同一个 op_id，后一台的写入会被当成
        # 「前一台的重发」而回执成功、实际没进网络。
        self.assertTrue(
            command.request_id.startswith("port:%r:" % (state.node_key.as_key(),)),
            command.request_id,
        )


# -- 经主控与存储服务的整条链路 --------------------------------------------


class FakeWorld(object):
    """`test_bootstrap` / `test_storage` / `test_energy` 里都有同形的替身。

    本文件自己写一个而不是互相 import：那会让这套用例的成败取决于另一个
    模块的导入顺序（`test_energy.py` 里有同样的注释）。
    """

    def __init__(self):
        self.facing = {}

    def list_loaded_chunks(self):
        return []

    def block_entities_in_chunk(self, chunk_key):
        return []

    def register_block_remove_listener(self, block_ids):
        pass

    def facing_at(self, node_key):
        return self.facing.get(node_key.as_key())

    def note_chunk_loaded(self, chunk_key, block_entities=()):
        pass

    def note_chunk_unloaded(self, chunk_key):
        pass

    def diagnostics(self):
        return {"fake": True}


class FakeContainerProvider(object):
    def __init__(self, boxes=None):
        self.boxes = dict(boxes or {})

    def container_at(self, node_key):
        return self.boxes.get(node_key.as_key())

    def diagnostics(self):
        return {"cached": len(self.boxes)}


class FakeInventoryProvider(object):
    """玩家背包提供者。用 `test_transfer.py` 里同形的假原语驱动真适配器，
    因此 `inv_*` 走的是**真实的** `EnginePlayerInventory` + 槽锚。"""

    def __init__(self):
        from endernet.server.adapters.containers_engine import (
            EnginePlayerInventory,
            build_stack_limit,
        )

        self.primitives = _PlayerPrimitives()
        self.limit = build_stack_limit(self.primitives)
        self._cache = {}
        self.adapter_class = EnginePlayerInventory

    def inventory_of(self, player_id):
        return self.endpoint_of(player_id).adapter

    def endpoint_of(self, player_id):
        from endernet.server.storage.transfer import InventoryEndpoint

        if not player_id:
            return None
        adapter = self._cache.get(player_id)
        if adapter is None:
            adapter = self.adapter_class(self.primitives, player_id, self.limit)
            self._cache[player_id] = adapter
        return InventoryEndpoint(adapter, player_id)

    def diagnostics(self):
        return {"cached_players": len(self._cache)}


class _PlayerPrimitives(object):
    PLAYER_SLOTS = 36

    def __init__(self):
        self.slots = [None] * self.PLAYER_SLOTS

    def player_slot_count(self, player_id):
        return self.PLAYER_SLOTS

    def player_item(self, player_id, slot):
        data = self.slots[slot]
        return dict(data) if data else None

    def set_player_item(self, player_id, slot, data):
        if data.get("count", 0) <= 0 or data.get("newItemName") == "minecraft:air":
            self.slots[slot] = None
        else:
            self.slots[slot] = dict(data)
        return True

    def stack_limit(self, name, aux):
        return 64


class BootstrapFixture(unittest.TestCase):
    """一条**真的能跑起来的**离线网络：创造能量源供电，一台驱动器带一块盘。

    这套用例走的是完整链路——主控准入 → 存储服务 → 转移协调器，
    因此它同时验证了「接口不绕过主控」这条结构性约束。
    """

    def setUp(self):
        from endernet.server.bootstrap import ServerBootstrap
        from endernet.server.network.energy import EnergyPolicy

        self.world = FakeWorld()
        self.driver_box = FakeContainer(slots=2, max_stack=64)
        self.containers = FakeContainerProvider()
        self.inventories = FakeInventoryProvider()
        self.server = ServerBootstrap(
            world=self.world,
            energy_policy=EnergyPolicy.dev_stand_in(),
            container_provider=self.containers,
            inventory_provider=self.inventories,
        )
        self.server.start(bind_engine=False)

    def place(self, node_key, device_id):
        from endernet.server.runtime.events import NormalizedEvent

        self.server.push(NormalizedEvent.place(node_key, device_id))

    def build_network(self, port_device=OUTPUT_DEVICE, offset=3):
        """基带 0..offset，控制器与电源挂在上面，驱动器与接口各占一格。"""
        for x in range(0, offset + 1):
            self.place(key(x), "endernet:network_baseband_8")
        self.place(key(1, 65), "endernet:tiny_network_endpoint")
        self.place(key(0, 65), "endernet:creative_energy_source")
        self.place(key(2, 65), "endernet:storage_driver")
        self.place(key(offset), port_device)
        if port_device in (INPUT, OUTPUT_DEVICE, TEMPLATE_DEVICE):
            self.world.facing[key(offset).as_key()] = "east"
            self.containers.boxes[key(offset + 1).as_key()] = self.driver_box
        if port_device in (BUS, TEMPLATE_DEVICE):
            self.containers.boxes[key(offset).as_key()] = _own_box(self)
        self.server.tick()
        return self.server.runtimes.runtimes()[0]

    def ref_at(self, node_key):
        return self.server.registry.get(node_key).ref

    def command(self, request_id, ref, action, **payload):
        from endernet.common.protocol import CommandKind, DeviceCommand

        return DeviceCommand(
            request_id,
            CommandKind.STORAGE,
            CommandSource("p1", ref, 0),
            payload=dict(payload, action=action),
            topology_version=self.server.topology.version,
            device_ref=ref,
        )

    def submit(self, request_id, ref, action, **payload):
        return self.server.submit_command(
            self.command(request_id, ref, action, **payload)
        )

    def mount_a_disk(self, request_id="mount"):
        return self.submit(
            request_id, self.ref_at(key(2, 65)), "mount",
            slot=0, model_id="endernet:database_compressed_tape",
        )

    def accrue_quota(self, ticks=10 ** 6):
        """把逻辑时钟推远，让介质额度桶攒满。

        **不硬编码帧频**：额度按「逻辑帧」记，因此这里推的是一个远到
        桶必然封顶的帧号（与 `test_storage.py` 的 `FAR_TICK` 同一手法）。
        """
        self.server.clock.advance(ticks)
        self.server.tick()


def _own_box(fixture):
    """被动总线/样板口自己的方块容器。**一台设备一份**，不共享。"""
    box = fixture.__dict__.get("_own_boxes", {}).get(id(fixture))
    if box is None:
        box = FakeContainer(slots=2, max_stack=64)
        fixture.__dict__.setdefault("_own_boxes", {})[id(fixture)] = box
    return box


class TestPortCommandsThroughTheMaster(BootstrapFixture):
    """接口命令走主控准入链，落到存储服务上执行（06-08 的接线）。"""

    def test_the_network_actually_becomes_runnable(self):
        runtime = self.build_network()
        self.mount_a_disk()
        self.assertEqual(runtime.state(), "runnable")

    def test_port_status_answers_even_without_a_running_network(self):
        """只读面不受运行条件限制（03 验收末条）。"""
        self.build_network()
        ref = self.ref_at(key(3))
        result = self.submit("r1", ref, "port_status")
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.payload["role"], OUTPUT)
        self.assertEqual(result.payload["target"]["z"], 0)
        self.assertTrue(result.payload["blocker"] is not None)

    def test_port_configure_writes_the_config_into_the_device_record(self):
        self.build_network()
        ref = self.ref_at(key(3))
        result = self.submit(
            "c1", ref, "port_configure",
            config={"enabled": True, "direction": "export",
                    "filter": [STONE.to_dict()], "limit": 7},
        )
        self.assertEqual(result.status, STATUS_OK)
        record = self.server.registry.get(key(3))
        self.assertEqual(record.config["port"]["limit"], 7)
        self.assertEqual(record.config["port"]["filter"][0], STONE.to_dict())

    def test_port_configure_merges_instead_of_replacing(self):
        """**回归**：整体替换会把没提到的字段打回默认值——
        玩家改了过滤，方向却跟着回去了。
        """
        self.build_network()
        ref = self.ref_at(key(3))
        self.submit("c1", ref, "port_configure",
                    config={"direction": "export", "limit": 9,
                            "filter": [STONE.to_dict()]})
        self.submit("c2", ref, "port_configure", config={"limit": 3})
        stored = self.server.registry.get(key(3)).config["port"]
        self.assertEqual(stored["limit"], 3)
        self.assertEqual(stored["direction"], "export", "没提到的字段不该被重置")
        self.assertEqual(len(stored["filter"]), 1)
        self.assertTrue(stored["enabled"])

    def test_the_tenth_pickup_filter_entry_is_refused_by_the_server(self):
        """11-07 的验收：**非法第 10 种取货配置被服务端拒绝**。

        校验必须在服务端——只在界面上拦住的话，改过的客户端照样能提交。
        """
        self.build_network()
        ref = self.ref_at(key(3))
        keys = [ResourceKey.item("minecraft:item_%d" % (index,))
                for index in range(PICKUP_FILTER_LIMIT.value + 1)]
        result = self.submit(
            "c1", ref, "port_configure",
            config={"filter": [item.to_dict() for item in keys],
                    "direction": "export"},
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn(Reason.FILTER_LIMIT_REACHED, result.reason_codes())

    def test_the_pickup_roles_reject_a_blacklist(self):
        """取货只接受白名单：`blocks.md` 对取货的原话是「设定 N 种」，
        那是个显式集合，没有「除这些之外都取」的语义。"""
        self.build_network()
        ref = self.ref_at(key(3))
        result = self.submit(
            "c1", ref, "port_configure",
            config={"filter": [STONE.to_dict()],
                    "filter_mode": "blacklist", "direction": "export"},
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn(Reason.FILTER_MODE_MISMATCH, result.reason_codes())

    def test_supply_slots_are_only_for_the_passive_port(self):
        """主动取货口没有共享槽，因此没有供货位可言。"""
        self.build_network()
        ref = self.ref_at(key(3))
        result = self.submit(
            "c1", ref, "port_configure",
            config={"filter": [STONE.to_dict()],
                    "direction": "export", "supply_slots": [0]},
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn(Reason.SUPPLY_SLOTS_INVALID, result.reason_codes())

    def test_an_out_of_range_supply_slot_is_refused(self):
        self.build_network(port_device=BUS)
        ref = self.ref_at(key(3))
        result = self.submit(
            "c1", ref, "port_configure",
            config={"enabled": True, "filter": [STONE.to_dict()],
                    "supply_slots": [PASSIVE_PORT_SLOT_COUNT.value]},
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn(Reason.SUPPLY_SLOTS_INVALID, result.reason_codes())

    def test_port_configure_rejects_an_unknown_field(self):
        self.build_network()
        ref = self.ref_at(key(3))
        result = self.submit("c1", ref, "port_configure", config={"teleport": True})
        self.assertEqual(result.status, "rejected")
        self.assertIn(Reason.BAD_PARAMETER, result.reason_codes())

    def test_port_configure_rejects_a_bad_payload(self):
        self.build_network()
        ref = self.ref_at(key(3))
        result = self.submit("c1", ref, "port_configure", config={"direction": "up"})
        self.assertEqual(result.status, "rejected")
        self.assertIn(Reason.BAD_PARAMETER, result.reason_codes())

    def test_dispatching_for_another_role_is_rejected(self):
        """设备不得要求一台取货口去做存货口的事（全局约束 2）。"""
        self.build_network()
        ref = self.ref_at(key(3))
        result = self.submit("d1", ref, "port_dispatch", intent=INTAKE)
        self.assertEqual(result.status, "rejected")
        self.assertIn(Reason.PORT_DIRECTION_MISMATCH, result.reason_codes())

    def test_an_unconfigured_port_is_blocked_with_a_locating_reason(self):
        runtime = self.build_network()
        self.mount_a_disk()
        self.assertEqual(runtime.state(), "runnable")
        ref = self.ref_at(key(3))
        result = self.submit("d1", ref, "port_dispatch", intent=OUTPUT)
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn(Reason.PORT_UNCONFIGURED, result.reason_codes())

    def test_a_held_port_refuses_to_dispatch(self):
        """06-02：不确定之后**一律不重试**，只能人工或恢复流程销账。"""
        runtime = self.build_network()
        self.mount_a_disk()
        self.assertEqual(runtime.state(), "runnable")
        ref = self.ref_at(key(3))
        self.submit("c1", ref, "port_configure",
                    config={"enabled": True, "filter": [STONE.to_dict()]})
        state = self.server.ports.get(key(3))
        state.hold = Reason.CONTAINER_UNCERTAIN
        result = self.submit("d1", ref, "port_dispatch", intent=OUTPUT)
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn(Reason.CONTAINER_UNCERTAIN, result.reason_codes())

    def test_a_fluid_port_is_refused_rather_than_pretending(self):
        self.build_network(port_device=FLUID_BUS)
        ref = self.ref_at(key(3))
        result = self.submit("d1", ref, "port_dispatch", intent=PASSIVE)
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn(Reason.NO_ADAPTER, result.reason_codes())

    def test_the_whole_output_path_moves_real_items(self):
        """**端到端**：网络 → 取货口 → 朝向容器，走主控与存储服务。"""
        runtime = self.build_network()
        self.mount_a_disk()
        self.assertEqual(runtime.state(), "runnable")
        ref = self.ref_at(key(3))
        self.submit("c1", ref, "port_configure",
                    config={"enabled": True, "direction": "export",
                            "filter": [STONE.to_dict()]})
        self.accrue_quota()
        self.assertEqual(
            self.submit("seed", ref, "insert", key=STONE.to_dict(), count=20,
                        op_id="seed-1").status,
            STATUS_OK,
        )
        result = self.submit("d1", ref, "port_dispatch", intent=OUTPUT)
        self.assertEqual(result.status, STATUS_OK)
        self.assertGreater(result.payload["receipt"]["moved"], 0)
        self.assertEqual(
            self.driver_box.count_of(STONE), result.payload["receipt"]["moved"]
        )

    def test_port_clear_lifts_the_hold(self):
        runtime = self.build_network()
        self.mount_a_disk()
        self.assertEqual(runtime.state(), "runnable")
        ref = self.ref_at(key(3))
        self.submit("c1", ref, "port_configure",
                    config={"enabled": True, "filter": [STONE.to_dict()]})
        self.server.ports.get(key(3)).hold = Reason.CONTAINER_UNCERTAIN
        result = self.submit("clear", ref, "port_clear")
        self.assertEqual(result.status, STATUS_OK)
        self.assertTrue(result.payload["cleared"])
        self.assertEqual(self.server.ports.get(key(3)).hold, "")
        # 重复点一次**不是错误**：得到同一条回答，只是 cleared 为假。
        again = self.submit("clear2", ref, "port_clear")
        self.assertEqual(again.status, STATUS_OK)
        self.assertFalse(again.payload["cleared"])

    def test_port_stage_is_only_for_the_template_port(self):
        self.build_network()
        ref = self.ref_at(key(3))
        result = self.submit("s1", ref, "port_stage", key=STONE.to_dict(), count=5)
        self.assertEqual(result.status, "rejected")
        self.assertIn(Reason.UNSUPPORTED, result.reason_codes())

    def test_an_unknown_action_is_still_rejected(self):
        self.build_network()
        result = self.submit("x1", self.ref_at(key(3)), "port_teleport")
        self.assertEqual(result.status, "rejected")
        self.assertIn(Reason.UNSUPPORTED, result.reason_codes())


def _stone_dict(count):
    return {"newItemName": "minecraft:stone", "newAuxValue": 0,
            "count": count, "showInHand": True}


class TestTemplatePortThroughTheMaster(BootstrapFixture):
    def prepare_template_port(self, seed=12, port_device=TEMPLATE_DEVICE):
        """一台通电、已配置、料也备齐的样板口。返回 `(runtime, ref)`。"""
        runtime = self.build_network(port_device=port_device)
        self.mount_a_disk()
        self.assertEqual(runtime.state(), "runnable")
        ref = self.ref_at(key(3))
        self.submit("c1", ref, "port_configure",
                    config={"enabled": True, "filter": [STONE.to_dict()]})
        self.accrue_quota()
        self.submit("seed", ref, "insert", key=STONE.to_dict(), count=seed,
                    op_id="seed-1")
        return runtime, ref

    def provider_port(self):
        """那台样板口的**能力对象**——07 的契约入口就在这里。"""
        return self.server.patterns.get(key(3)).provider_port

    def test_stage_then_feed_a_batch(self):
        """06-07：成批备料 -> 投给外部机器 -> 本地缓冲回执。"""
        runtime, ref = self.prepare_template_port()
        staged = self.submit("s1", ref, "port_stage", key=STONE.to_dict(), count=10,
                             op_id="batch-1")
        self.assertEqual(staged.status, STATUS_OK)
        self.assertEqual(staged.payload["batch"]["staged"], 10)
        fed = self.submit("f1", ref, "port_dispatch", intent=TEMPLATE)
        self.assertEqual(fed.status, STATUS_OK)
        self.assertEqual(fed.payload["batch"]["delivered"], 10)
        self.assertEqual(self.driver_box.count_of(STONE), 10)

    # -- 07-05 的契约入口：push_inputs（D-01）----------------------------

    def test_push_inputs_stages_through_the_master(self):
        """**D-01 的验收点。** `capabilities.PatternProvider.push_inputs`
        对**非订单**调用方现在真的能用了，而且走的是那条唯一的路：
        `port_stage` 命令 → 主控准入 → 存储服务。

        判据落在那台口的中转位上：料真的进了缓冲，批次记录也认这笔账。
        """
        self.prepare_template_port(seed=12)
        port = self.server.ports.get(key(3))
        port_state = self.server.patterns.get(key(3))
        receipt = port_state.provider_port.push_inputs("exec-1", [(STONE, 10)])
        self.assertEqual(receipt.reason, "")
        self.assertEqual(receipt.moved, 10)
        self.assertTrue(receipt.is_applied())
        # 料真的在中转位里，批次也记着是这一批。
        self.assertEqual(port.buffer.total_of(STONE), 10)
        self.assertEqual(port.batch.op_id, "push/exec-1/%s#10" % (STONE.canonical(),))
        self.assertEqual(port.batch.staged, 10)

    def test_push_inputs_is_idempotent_for_the_same_execution(self):
        """**同一个执行、同样的量 → 一个字节都不再动。**

        这不是「顺手优化」：`op_id` 是确定性的，而主控账本有保留期
        （`LedgerPolicy.retention_ticks`）。只靠账本重放挡的话，保留期一过
        同一个请求就会**真的再备一遍**——中转位翻倍，而两侧账本各自都正常。
        """
        self.prepare_template_port(seed=12)
        port = self.server.ports.get(key(3))
        provider = self.server.patterns.get(key(3)).provider_port
        first = provider.push_inputs("exec-1", [(STONE, 4)])
        self.assertEqual(first.moved, 4)
        second = provider.push_inputs("exec-1", [(STONE, 4)])
        self.assertEqual(second.moved, 4)
        self.assertTrue(second.is_applied())
        # **中转位没有翻倍。**
        self.assertEqual(port.buffer.total_of(STONE), 4)

    def factory_service(self):
        """那台网络上的 `ServiceId.FACTORY`。"""
        from endernet.server.network.services import ServiceId

        runtime = self.server.runtimes.runtimes()[0]
        return runtime.services.get(ServiceId.FACTORY)

    def test_stage_inputs_does_not_restage_without_the_ledger(self):
        """**账本之外的那一次。** 这里直接调服务门面，因此主控的去重账本
        完全不参与——正是保留期过了之后的样子（`LedgerPolicy.retention_ticks`
        到期就不再重放，而 `op_id` 是确定性的）。

        没有这一层保护，同一个请求会**真的再备一遍**：中转位翻倍，
        而两侧账本各自都显示正常。
        """
        self.prepare_template_port(seed=12)
        port = self.server.ports.get(key(3))
        factory = self.factory_service()
        op_id = "op-no-ledger"
        first = factory.stage_inputs(key(3), STONE, 5, op_id)
        self.assertEqual(first.payload["batch"]["staged"], 5)
        second = factory.stage_inputs(key(3), STONE, 5, op_id)
        # 第二次拿到的是**现成的批次读数**，没有第二条命令。
        self.assertEqual(second.payload["batch"]["staged"], 5)
        self.assertNotIn("receipt", second.payload)
        self.assertEqual(port.buffer.total_of(STONE), 5)

    def test_an_empty_batch_of_our_own_does_not_poison_the_slot(self):
        """**空批次的解药。** `stage_batch` 是**先**把批次写进状态槽、
        **再**判中转位，因此一次没备到料的推送会留下一个 `staged == 0`
        的自家批次。没有「自家空批次放行重发」这一支，那台口会被它
        永久占住：同一个 `op_id` 一直被当成「已经备过了」。
        """
        self.prepare_template_port(seed=0)
        port = self.server.ports.get(key(3))
        factory = self.factory_service()
        op_id = "op-empty"
        first = factory.stage_inputs(key(3), STONE, 5, op_id)
        self.assertEqual(first.payload["batch"]["staged"], 0)
        self.assertEqual(port.batch.op_id, op_id)
        # 料来了，同一个 `op_id` 再来一次必须**真的重发**，而不是回放那个空批次。
        self.submit("seed2", self.ref_at(key(3)), "insert",
                    key=STONE.to_dict(), count=5, op_id="seed-2")
        again = factory.stage_inputs(key(3), STONE, 5, op_id)
        self.assertEqual(again.payload["batch"]["staged"], 5)
        self.assertEqual(port.buffer.total_of(STONE), 5)

    def test_push_inputs_yields_to_a_batch_that_is_still_alive(self):
        """批次槽只有一格。**别人的活批次占着时让位**，不冲掉它的投料记录。

        判据用的是 09 的 `_foreign_batch`（订单侧同一条），因此两边的
        让位规则不会分叉——分叉的症状是订单与契约入口互相冲掉对方的批次，
        而两边都以为自己才是正主。
        """
        self.prepare_template_port(seed=12)
        port = self.server.ports.get(key(3))
        provider = self.server.patterns.get(key(3)).provider_port
        # 要 30 个但只有 12 个 → 批次**未完成**，槽被占住了。
        first = provider.push_inputs("exec-a", [(STONE, 30)])
        self.assertEqual(first.moved, 12)
        self.assertEqual(first.status, TransferStatus.PARTIAL)
        self.assertFalse(port.batch.is_complete())
        other = provider.push_inputs("exec-b", [(STONE, 1)])
        self.assertEqual(other.reason, Reason.ORDER_PORT_BUSY)
        self.assertEqual(other.moved, 0)
        self.assertFalse(other.is_applied())
        # 原来那一批**原样还在**，没有被后来的请求冲掉。
        self.assertEqual(port.batch.op_id, "push/exec-a/%s#30" % (STONE.canonical(),))

    def test_push_inputs_refuses_two_resources_in_one_call(self):
        """批次槽一格放不下两种资源。第二种既不在批次记录里、也永远不会
        被投出去——**账外资产**。宁可整批拒绝。"""
        self.prepare_template_port(seed=4)
        port = self.server.ports.get(key(3))
        provider = self.server.patterns.get(key(3)).provider_port
        receipt = provider.push_inputs("exec-1", [(STONE, 2), (DIRT, 2)])
        self.assertEqual(receipt.reason, Reason.UNSUPPORTED)
        self.assertEqual(receipt.requested, 4)
        self.assertEqual(port.buffer.total_of(STONE), 0)
        self.assertIsNone(port.batch)

    def test_push_inputs_to_assembly_requires_order_path(self):
        """装配仓必须由带租约的订单推进，裸投料不能绕过租约。"""
        self.build_network(port_device="endernet:assemble_chamber")
        state = self.server.patterns.get(key(3))
        self.assertIsNotNone(state, "装配仓也是样板提供者")
        receipt = state.provider_port.push_inputs("exec-1", [(STONE, 1)])
        self.assertFalse(receipt.is_applied())
        self.assertEqual(receipt.reason, Reason.UNSUPPORTED)
        # **不是 `no_adapter`**：那会把人引去查「为什么没有接口状态」。
        self.assertNotIn(Reason.NO_ADAPTER, (receipt.reason,))


class TestInventoryCommands(BootstrapFixture):
    """06-09 的服务端半边：自定义背包槽的取放。**客户端与界面属 11。**"""

    def setUp(self):
        BootstrapFixture.setUp(self)
        runtime = self.build_network()
        self.mount_a_disk()
        # 背包命令要走主控准入，因此这台网络必须是 runnable 的。
        self.assertEqual(runtime.state(), "runnable")
        self.accrue_quota()
        self.ref = self.ref_at(key(3))

    def test_a_cursor_payload_is_refused_because_r12_is_unresolved(self):
        """R12 未定案：**不猜**，遇到就明确拒绝（替身口径是「不接管」）。"""
        result = self.submit(
            "t1", self.ref, "inv_take", key=STONE.to_dict(), count=1,
            slot=0, cursor={"key": STONE.to_dict(), "count": 1},
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn(Reason.CURSOR_UNRESOLVED, result.reason_codes())

    def test_an_unknown_player_is_rejected(self):
        from endernet.common.protocol import CommandKind, DeviceCommand

        command = DeviceCommand(
            "t1", CommandKind.STORAGE, CommandSource("", self.ref, 0),
            payload={"action": "inv_take", "key": STONE.to_dict(), "count": 1},
            topology_version=self.server.topology.version, device_ref=self.ref,
        )
        result = self.server.submit_command(command)
        self.assertEqual(result.status, "rejected")
        self.assertIn(Reason.BAD_PARAMETER, result.reason_codes())

    def test_an_anchored_put_lands_in_the_anchored_slot(self):
        """**回归**：`anchored_insert` 曾经委托给按资源键的 `insert`，
        后者从 0 号槽起整表扫描——于是「放进第 7 槽」变成了
        「放进第一个同种或空着的槽」。总量守恒让这种错对不上账也查不出来。
        """
        self.submit("seed", self.ref, "insert", key=STONE.to_dict(), count=12,
                    op_id="seed-1")
        endpoint = self.inventories.endpoint_of("p1")
        endpoint.adapter.write_slot(0, _stone_dict(60))
        endpoint.adapter.write_slot(7, None)
        result = self.submit(
            "t1", self.ref, "inv_put", key=STONE.to_dict(), count=5,
            slot=7, slot_version=endpoint.slot_view(7)["version"],
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertEqual(endpoint.slot_view(7)["count"], 5, "第 7 槽必须真的被填")
        self.assertEqual(endpoint.slot_view(0)["count"], 60, "第 0 槽不该被动")

    def test_an_anchored_take_only_touches_that_slot(self):
        """**回归**：取也一样。两个槽里有同种物品时，按槽取必须取那个槽。"""
        endpoint = self.inventories.endpoint_of("p1")
        endpoint.adapter.write_slot(0, _stone_dict(64))
        endpoint.adapter.write_slot(20, _stone_dict(64))
        result = self.submit(
            "t1", self.ref, "inv_take", key=STONE.to_dict(), count=5,
            slot=20, slot_version=endpoint.slot_view(20)["version"],
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertEqual(endpoint.slot_view(0)["count"], 64, "第 0 槽不该被动")
        self.assertEqual(endpoint.slot_view(20)["count"], 59)

    def test_a_bad_count_is_rejected(self):
        result = self.submit("t1", self.ref, "inv_take", key=STONE.to_dict(),
                             count=0, slot=0)
        self.assertEqual(result.status, "rejected")
        self.assertIn(Reason.OUT_OF_RANGE, result.reason_codes())

    def test_an_anchored_take_refuses_a_slot_holding_something_else(self):
        """**「背包槽在点击后变更时不会误取替换后的物品」**（06 验收末条）。

        归宿是 `stale` 而不是 `rejected`：客户端的下一步是**重新取快照
        （刷新界面）再发**，而不是「这个操作不合法」。
        """
        endpoint = self.inventories.endpoint_of("p1")
        endpoint.adapter.insert("seed", DIRT, 3)
        result = self.submit(
            "t1", self.ref, "inv_take", key=STONE.to_dict(), count=3, slot=0
        )
        self.assertEqual(result.status, "stale")
        self.assertIn(Reason.SLOT_CHANGED, result.reason_codes())
        self.assertEqual(endpoint.adapter.count_of(DIRT), 3, "泥土一个都没少")
        self.assertEqual(endpoint.adapter.count_of(STONE), 0)

    def test_a_stale_slot_version_is_reported_as_stale(self):
        endpoint = self.inventories.endpoint_of("p1")
        endpoint.adapter.insert("seed", STONE, 5)
        result = self.submit(
            "t1", self.ref, "inv_take", key=STONE.to_dict(), count=5,
            slot=0, slot_version=99,
        )
        self.assertEqual(result.status, "stale")
        self.assertIn(Reason.STALE_VERSION, result.reason_codes())

    def test_quick_move_requires_a_target(self):
        result = self.submit("t1", self.ref, "inv_quick_move", slot=0)
        self.assertEqual(result.status, "rejected")
        self.assertIn(Reason.BAD_PARAMETER, result.reason_codes())

    def test_merge_needs_two_distinct_slots(self):
        result = self.submit(
            "t1", self.ref, "inv_merge", slot=0, into_slot=0,
            key=STONE.to_dict(),
        )
        self.assertEqual(result.status, "rejected")
        self.assertIn(Reason.BAD_PARAMETER, result.reason_codes())

    def test_an_anchored_take_moves_both_legs(self):
        """**回归**：锚定路径曾经是一条旁路，只动背包、不动网络——
        `inv_take` 于是把背包里的石头凭空删掉，`inv_put` 凭空造出来。
        判据是**独立求和**：两侧合计在操作前后必须一致。
        """
        endpoint = self.inventories.endpoint_of("p1")
        endpoint.adapter.insert("seed", STONE, 8)
        before = self._total_of(STONE)
        result = self.submit(
            "t1", self.ref, "inv_take", key=STONE.to_dict(), count=5, slot=0
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        moved = result.payload["receipt"]["moved"]
        self.assertEqual(moved, 5)
        self.assertEqual(endpoint.adapter.count_of(STONE), 3, "背包少了 5")
        self.assertEqual(self._total_of(STONE), before, "两侧合计守恒")

    def test_an_anchored_put_moves_both_legs(self):
        """**回归**：`inv_put` 曾经只往背包写，网络一个都没扣。"""
        self.submit("seed", self.ref, "insert", key=STONE.to_dict(), count=12,
                    op_id="seed-1")
        before = self._total_of(STONE)
        endpoint = self.inventories.endpoint_of("p1")
        result = self.submit(
            "t1", self.ref, "inv_put", key=STONE.to_dict(), count=5, slot=0
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertEqual(result.payload["receipt"]["moved"], 5)
        self.assertEqual(endpoint.adapter.count_of(STONE), 5)
        self.assertEqual(self._total_of(STONE), before, "两侧合计守恒")

    def _total_of(self, key):
        """背包 + 网络里该资源的合计（**独立求和**）。

        两边各走自己那条读取路径：背包问适配器、网络走存储服务的查询面，
        因此它不是把被测记账再算一遍。

        **每次都要换 request_id。** 存储命令不是只读命令，因此会进主控的
        去重账本——沿用同一个 id 会让第二次查询命中去重而返回**操作前**的
        旧快照，于是守恒断言看着像不守恒（这个坑在本轮真的踩了一次）。
        """
        self._query_seq = getattr(self, "_query_seq", 0) + 1
        stored = self.submit(
            "q%d" % (self._query_seq,), self.ref, "query", key=key.to_dict()
        ).payload["entry"]["stored"]
        endpoint = self.inventories.endpoint_of("p1")
        return endpoint.adapter.count_of(key) + stored

    def test_split_requires_a_slot(self):
        result = self.submit("t1", self.ref, "inv_split", key=STONE.to_dict(),
                            count=1)
        self.assertEqual(result.status, "rejected")
        self.assertIn(Reason.BAD_PARAMETER, result.reason_codes())


if __name__ == "__main__":
    unittest.main()
