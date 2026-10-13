# coding=utf-8
"""样板提供者状态与产物索引（任务 07-04）。

**全部离线**：容器用 `containers.FakeContainer` 替身，引擎一行都不碰。

这一套用例守的是几件「离线看着对、真机上会出事」的事：

1. **9 格的权威是容器，索引是派生的。** 拔出、编辑、整块换掉都必须在
   下一次重读时自然生效——不需要另一套对账，也就不会有第二份真相。
2. **读不到容器不等于样板没了。** 区块卸载时索引要保留，否则 09 会看到
   「样板消失」而去重做计划，而不是等待。
3. **过时与不合法分开报。** 不是样板 / 是样板但坏了 / 是样板但模式不对 /
   是样板但格式过旧，四者的处置完全不同。
4. **旧规划不能直接派发。** 编辑之后，拿着旧 digest 回来必须拿到
   `pattern_stale`，而**已派发的批次不因此被删或重发**。
5. **样板槽不被 06 的投料占用。** 这一条跨到了 06 的地盘，但它是
   `blocks.md` 的明文要求，值得在这里钉死。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import DefinitionSet
from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.item_codec import item_dict_from_key
from endernet.common.protocol import (
    STATUS_BLOCKED,
    STATUS_OK,
    BlockReason,
    CommandResult,
    Reason,
    TransferStatus,
)
from endernet.common.resource_key import ResourceKey
from endernet.server.adapters.containers import FakeContainer
from endernet.server.crafting import patterns
from endernet.server.crafting.execution import (
    AssemblyChamberExecution,
    NoPresetRecipeSource,
)
from endernet.server.crafting.store import (
    PATTERN_MAX_STALE_FRAMES,
    PATTERN_REFRESH_BUDGET,
    PATTERN_SETTLE_FRAMES,
    PatternStore,
    container_slot_count,
    is_pattern_provider,
)
from endernet.server.devices.io import (
    BUFFER_MEMORY,
    TEMPLATE,
    PortPolicy,
    PortStore,
    role_of,
)
from endernet.server.runtime.lifecycle import ChangeKind, LifecycleChange

TEMPLATE_DEVICE = "endernet:bus_template_io"
PASSIVE_DEVICE = "endernet:bus_item_io"
CHAMBER_DEVICE = "endernet:assemble_chamber"

IRON = ResourceKey.item("minecraft:iron_ingot")
IRON_BLOCK = ResourceKey.item("minecraft:iron_block")
STONE = ResourceKey.item("minecraft:stone")


def key(x, y=64, z=0, dimension=0):
    return NodeKey(dimension, x, y, z)


class _Record(object):
    def __init__(self, ref, device_id, config=None):
        self.ref = ref
        self.device_id = device_id
        self.config = dict(config or {})
        self.loaded = True
        self.facing = None


class _Registry(object):
    """最小的注册表替身：只回答 `get` / `get_by_ref`。"""

    def __init__(self):
        self.records = {}

    def add(self, ref, device_id):
        record = _Record(ref, device_id)
        self.records[ref.node_key.as_key()] = record
        return record

    def get(self, node_key):
        return self.records.get(node_key.as_key())

    def get_by_ref(self, ref):
        record = self.records.get(ref.node_key.as_key())
        if record is None or record.ref.generation != ref.generation:
            return None
        return record


class _ContainerProvider(object):
    """位置 -> 容器。**每台设备一个独立的假容器**，避免用例互相污染。"""

    def __init__(self, slots=9, max_stack=1, container_cls=None):
        self.slots = slots
        self.max_stack = max_stack
        self.container_cls = container_cls if container_cls is not None else FakeContainer
        self.by_position = {}
        self.fail = False

    def container_at(self, node_key):
        if self.fail:
            return None
        position = node_key.as_key()
        if position not in self.by_position:
            self.by_position[position] = self.container_cls(
                slots=self.slots, max_stack=self.max_stack
            )
        return self.by_position[position]

    def container_for(self, ref):
        return self.container_at(ref.node_key)


class _LyingContainer(FakeContainer):
    """`write_slot` 报成功、实际什么都没写。

    这是架构规划 §6.3 点名的缺陷形态（`PutItemIntoContainer` 不看
    `SetContainerItem` 的布尔返回值），在真实引擎上的对应物是
    `SpawnItemToContainer` 对满容器/被锁容器静静地什么都不做。
    """

    adapter_id = "lying"

    def write_slot(self, slot, data):
        return True


class _NoCountContainer(FakeContainer):
    """`slots_count()` 返回 `None`。13-01 的文档明说这是合法答案
    （「不可枚举，如流体罐」）。"""

    adapter_id = "no_count"

    def slots_count(self):
        return None


class _AngryContainer(FakeContainer):
    """读格数时抛异常的双件，用于验证「一台出错不连累其余」。"""

    adapter_id = "angry"

    def slots_count(self):
        raise RuntimeError("引擎炸了")


class _FlakyContainer(FakeContainer):
    """读某一个槽时抛异常，其余正常。"""

    adapter_id = "flaky"

    def __init__(self, bad_slot, **kwargs):
        FakeContainer.__init__(self, **kwargs)
        self.bad_slot = bad_slot

    def read_slot(self, slot):
        if slot == self.bad_slot:
            raise IOError("这一格读不出来")
        return FakeContainer.read_slot(self, slot)


def factory(inputs, outputs, returns=()):
    return patterns.PatternDefinition(patterns.MODE_FACTORY, inputs, outputs, returns)


def stack(name, count=1, **kwargs):
    return patterns.PatternStack(ResourceKey.item(name), count, **kwargs)


def simple_pattern(count=1, output=IRON_BLOCK):
    return factory([stack("minecraft:iron_ingot", count)], [patterns.PatternStack(output, 1)])


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.definitions = DefinitionSet()
        self.registry = _Registry()
        self.containers = _ContainerProvider()
        self.store = PatternStore(
            self.definitions,
            self.registry,
            containers=self.containers,
        )
        self.ref = DeviceRef(key(10), 1)
        self.registry.add(self.ref, TEMPLATE_DEVICE)
        self.state = self.store.ensure(self.ref, TEMPLATE_DEVICE)
        self.container = self.containers.container_for(self.ref)

    def put_item(self, slot, item_dict, refresh=True):
        """直接往方块容器里摆一件物品（模拟玩家拖进去）。"""
        self.container.write_slot(slot, item_dict)
        if refresh:
            self.store.refresh(self.ref, force=True)

    def put_pattern(self, slot, definition, refresh=True):
        self.put_item(slot, self.store.template_item(definition), refresh)

    def slot_codes(self):
        return dict(
            (slot, self.state.invalid[slot]["code"])
            for slot in self.state.invalid
        )


# -- 生命周期 --------------------------------------------------------------


class TestProviderMembership(StoreCase):
    def test_only_pattern_providers_are_tracked(self):
        """**按能力判定，不按设备名**：样板接口与装配仓都算提供者。"""
        self.assertTrue(is_pattern_provider(TEMPLATE_DEVICE, self.definitions))
        self.assertTrue(is_pattern_provider(CHAMBER_DEVICE, self.definitions))
        self.assertFalse(is_pattern_provider(PASSIVE_DEVICE, self.definitions))
        self.assertFalse(is_pattern_provider("minecraft:chest", self.definitions))

    def test_ensure_is_idempotent(self):
        again = self.store.ensure(self.ref, TEMPLATE_DEVICE)
        self.assertIs(again, self.state)
        self.assertEqual(self.store.stats["created"], 1)

    def test_ensure_refuses_a_device_that_is_not_a_provider(self):
        self.assertIsNone(self.store.ensure(DeviceRef(key(11), 1), PASSIVE_DEVICE))

    def test_generation_mismatch_invalidates_the_state(self):
        """代次不符即失效——这是延迟任务的自毁检查。"""
        stale = DeviceRef(self.ref.node_key, self.ref.generation + 1)
        self.assertIsNone(self.store.get_by_ref(stale))

    def test_slot_count_comes_from_the_container(self):
        self.assertEqual(self.state.slot_count, 9)
        store = PatternStore(
            self.definitions, self.registry, containers=_ContainerProvider(slots=4)
        )
        state = store.ensure(self.ref, TEMPLATE_DEVICE)
        self.assertEqual(state.slot_count, 4)

    def test_slot_count_falls_back_to_the_documented_nine(self):
        """容器答不出格数时退回定义表那条已定案的 9——它与 `gen_blocks.py`
        写进方块容器的 `container_size` 是同一个数。"""

        class _NoCount(FakeContainer):
            def slots_count(self):
                return 0

        class _Provider(object):
            def container_at(self, node_key):
                return _NoCount(slots=0)

        store = PatternStore(self.definitions, self.registry, containers=_Provider())
        state = store.ensure(self.ref, TEMPLATE_DEVICE)
        self.assertEqual(state.slot_count, 9)

    def test_a_container_without_slot_access_is_reported_not_guessed(self):
        """没有按槽读取能力时**明确报读不到**，不退化成按资源键的整表扫描
        ——后者读不出「哪个槽里是什么」，而槽位正是这块功能的全部意义。"""

        class _NoSlots(object):
            def container_at(self, node_key):
                return object()

        store = PatternStore(self.definitions, self.registry, containers=_NoSlots())
        state = store.ensure(self.ref, TEMPLATE_DEVICE)
        self.assertTrue(state.unreadable)
        self.assertFalse(state.is_usable())

    def test_container_slot_count_prefers_the_slot_backed_name(self):
        class _Slotted(object):
            def slot_count(self):
                return 7

            def slots_count(self):
                return 99

        self.assertEqual(container_slot_count(_Slotted()), 7)

    def test_drop_forgets_the_provider(self):
        self.assertIs(self.store.drop(self.ref), self.state)
        self.assertIsNone(self.store.get_by_ref(self.ref))

    def test_unloaded_is_not_removed(self):
        """**未加载不等于被拆掉**（与驱动器同一条纪律）。"""
        self.assertEqual(self.store.mark_loaded(self.ref.node_key, False), True)
        self.assertIsNotNone(self.store.get_by_ref(self.ref))
        self.assertFalse(self.state.is_usable())
        self.assertTrue(self.state.loaded is False)

    def test_observe_walks_the_lifecycle(self):
        store = PatternStore(
            self.definitions, self.registry, containers=self.containers
        )
        ref = DeviceRef(key(20), 1)
        self.registry.add(ref, TEMPLATE_DEVICE)
        added = LifecycleChange(ChangeKind.ADDED, ref.node_key, TEMPLATE_DEVICE, ref)
        self.assertEqual(store.observe(added), 1)
        self.assertIsNotNone(store.get_by_ref(ref))
        # 重复的 ADDED 是幂等的（拓扑重建会反复调用）。
        self.assertEqual(store.observe(added), 1)

        unloaded = LifecycleChange(
            ChangeKind.UNLOADED, ref.node_key, TEMPLATE_DEVICE, ref
        )
        self.assertEqual(store.observe(unloaded), 1)
        self.assertFalse(store.get_by_ref(ref).loaded)

        removed = LifecycleChange(
            ChangeKind.REMOVED, ref.node_key, TEMPLATE_DEVICE, ref
        )
        self.assertEqual(store.observe(removed), 1)
        self.assertIsNone(store.get_by_ref(ref))

    def test_observe_ignores_devices_that_are_not_providers(self):
        store = PatternStore(
            self.definitions, self.registry, containers=self.containers
        )
        ref = DeviceRef(key(21), 1)
        self.registry.add(ref, PASSIVE_DEVICE)
        change = LifecycleChange(ChangeKind.ADDED, ref.node_key, PASSIVE_DEVICE, ref)
        self.assertEqual(store.observe(change), 0)


# -- 读槽 ------------------------------------------------------------------


class TestSlotReading(StoreCase):
    def test_empty_container_has_no_entries(self):
        self.assertEqual(self.state.entries, {})
        self.assertEqual(self.state.invalid, {})
        self.assertEqual(self.state.unreadable, "")

    def test_a_plain_item_is_reported_as_not_a_template(self):
        self.put_item(2, item_dict_from_key(STONE, 1))
        self.assertEqual(self.slot_codes(), {2: patterns.PatternError.NOT_A_PATTERN})

    def test_a_crafting_template_in_a_factory_slot_is_a_kind_mismatch(self):
        """「拒收不适用的样板类型」在领域层的落点（插槽那一刻由事件挡，
        这里兜的是「已经躺在里面」的情形）。"""
        crafting = patterns.PatternDefinition(
            patterns.MODE_CRAFTING,
            [stack("minecraft:iron_ingot", 1, slot=0)],
            [patterns.PatternStack(IRON_BLOCK, 1)],
        )
        self.put_pattern(4, crafting)
        self.assertEqual(self.slot_codes(), {4: patterns.PatternError.KIND_MISMATCH})

    def test_a_broken_payload_is_reported_with_its_own_code(self):
        item = dict(self.store.template_item(simple_pattern()))
        item["userData"] = {"endernet:pattern": {"format": 1, "mode": "factory"}}
        self.put_item(5, item)
        self.assertEqual(self.slot_codes(), {5: patterns.PatternError.MALFORMED})

    def test_a_too_new_format_is_not_read_as_empty(self):
        """更新的格式读不懂就不读，**保留在槽里**、报原因，不当作空样板。"""
        item = dict(self.store.template_item(simple_pattern()))
        item["userData"] = {
            "endernet:pattern": patterns.encode_pattern(simple_pattern())
        }
        item["userData"]["endernet:pattern"]["format"] = 99
        self.put_item(6, item)
        self.assertEqual(self.slot_codes(), {6: patterns.PatternError.FORMAT_TOO_NEW})

    def test_a_template_that_is_not_a_pattern_item_is_reported(self):
        """是 `endernet:` 的物品但不是样板物品（比如介质）。"""
        two_side = ResourceKey.item("endernet:singleside_tape")
        self.put_item(7, item_dict_from_key(two_side, 1))
        self.assertEqual(self.slot_codes(), {7: patterns.PatternError.NOT_A_PATTERN})

    def test_invalid_slots_do_not_become_entries(self):
        self.put_item(2, item_dict_from_key(STONE, 1))
        self.assertEqual(self.state.entries, {})
        self.assertEqual(self.state.usable_entries(), [])

    def test_reading_from_a_missing_container_keeps_the_last_known_entries(self):
        """**读不到容器不等于样板没了。** 清空会让 09 看到「样板消失」
        而不是「设备离线」——前者会让它去重做计划。"""
        self.put_pattern(3, simple_pattern())
        self.assertEqual(sorted(self.state.entries), [3])
        self.containers.fail = True
        self.store.refresh(self.ref, force=True)
        self.assertEqual(sorted(self.state.entries), [3])
        self.assertTrue(self.state.unreadable)
        self.assertFalse(self.state.is_usable())

    def test_recovering_from_an_unreadable_container_clears_the_reason(self):
        self.containers.fail = True
        self.store.refresh(self.ref, force=True)
        self.assertTrue(self.state.unreadable)
        self.containers.fail = False
        self.store.refresh(self.ref, force=True)
        self.assertEqual(self.state.unreadable, "")
        self.assertTrue(self.state.is_usable())

    def test_refresh_is_refused_while_unloaded(self):
        self.store.mark_loaded(self.ref.node_key, False)
        self.assertFalse(self.store.refresh(self.ref))

    def test_force_refresh_works_while_unloaded(self):
        self.store.mark_loaded(self.ref.node_key, False)
        self.assertTrue(self.store.refresh(self.ref, force=True))


# -- 索引 ------------------------------------------------------------------


class TestOutputIndex(StoreCase):
    def test_candidates_find_the_provider_by_output(self):
        self.put_pattern(3, simple_pattern())
        self.assertEqual(len(self.store.candidates(IRON_BLOCK)), 1)
        self.assertEqual(self.store.candidates(IRON_BLOCK)[0]["entry"].slot, 3)

    def test_an_unrelated_output_has_no_candidates(self):
        self.put_pattern(3, simple_pattern())
        self.assertEqual(self.store.candidates(ResourceKey.item("minecraft:diamond")), [])

    def test_inputs_are_not_indexed(self):
        """索引按**产物**建：原料不该让人以为网络能做它。"""
        self.put_pattern(3, simple_pattern())
        self.assertEqual(self.store.candidates(IRON), [])

    def test_every_output_of_a_multi_output_recipe_is_findable(self):
        pattern = factory(
            [stack("minecraft:raw_fish", 8)],
            [
                patterns.PatternStack(ResourceKey.item("minecraft:cooked_fish"), 8),
                patterns.PatternStack(ResourceKey.item("minecraft:bone_meal"), 1),
            ],
        )
        self.put_pattern(1, pattern)
        self.assertEqual(len(self.store.candidates(ResourceKey.item("minecraft:cooked_fish"))), 1)
        self.assertEqual(len(self.store.candidates(ResourceKey.item("minecraft:bone_meal"))), 1)

    def test_two_providers_are_both_returned(self):
        second = DeviceRef(key(30), 1)
        self.registry.add(second, TEMPLATE_DEVICE)
        self.store.ensure(second, TEMPLATE_DEVICE)
        self.put_pattern(3, simple_pattern())
        self.containers.container_for(second).write_slot(
            5, self.store.template_item(simple_pattern())
        )
        self.store.refresh(second, force=True)
        self.assertEqual(len(self.store.candidates(IRON_BLOCK)), 2)
        positions = [
            item["provider"].node_key.as_key()
            for item in self.store.candidates(IRON_BLOCK)
        ]
        self.assertEqual(positions, sorted(positions))

    def test_the_last_offline_provider_is_not_dispatchable(self):
        """07 验收：「最后一个提供者离线后不会继续作为可派发设备」。"""
        self.put_pattern(3, simple_pattern())
        self.assertEqual(len(self.store.candidates(IRON_BLOCK)), 1)
        self.store.mark_loaded(self.ref.node_key, False)
        self.assertEqual(self.store.candidates(IRON_BLOCK), [])
        # 但诊断要能说出为什么，否则玩家只看到「没配方」。
        listed = self.store.candidates(IRON_BLOCK, include_unavailable=True)
        self.assertEqual(len(listed), 1)
        self.assertFalse(listed[0]["available"])
        self.assertEqual(
            listed[0]["blocker"].code, Reason.DRIVER_UNLOADED
        )

    def test_an_unreadable_provider_is_not_dispatchable(self):
        self.put_pattern(3, simple_pattern())
        self.containers.fail = True
        self.store.refresh(self.ref, force=True)
        self.assertEqual(self.store.candidates(IRON_BLOCK), [])
        listed = self.store.candidates(IRON_BLOCK, include_unavailable=True)
        self.assertEqual(listed[0]["blocker"].code, Reason.NO_CONTAINER)

    def test_candidate_order_is_stable_but_is_not_a_priority(self):
        """顺序由（设备标识、位置、槽位）决定，同样的输入永远给同样的顺序。
        **它不是优先级**——那是 08-04 的规则。"""
        self.put_pattern(8, simple_pattern())
        self.put_pattern(2, simple_pattern())
        first = [item["entry"].slot for item in self.store.candidates(IRON_BLOCK)]
        self.assertEqual(first, [2, 8])
        for _ in range(3):
            again = [item["entry"].slot for item in self.store.candidates(IRON_BLOCK)]
            self.assertEqual(again, first)

    def test_index_is_rebuilt_after_a_change(self):
        self.assertEqual(self.store.candidates(IRON_BLOCK), [])
        self.put_pattern(3, simple_pattern())
        self.assertEqual(len(self.store.candidates(IRON_BLOCK)), 1)
        self.store.uninstall(self.ref, 3)
        self.assertEqual(self.store.candidates(IRON_BLOCK), [])

    def test_index_is_derived_not_authoritative(self):
        """索引随时可以丢掉重建——这正是 12 的「派生样板索引在恢复后重建」。"""
        self.put_pattern(3, simple_pattern())
        before = self.store.candidates(IRON_BLOCK)[0]["entry"].digest()
        self.store.mark_all_dirty()
        self.assertEqual(self.store.refresh_all(), 1)
        after = self.store.candidates(IRON_BLOCK)[0]["entry"].digest()
        self.assertEqual(before, after)

    def test_stale_patterns_are_not_indexed(self):
        """格式过旧的样板读得出来（界面要看得见），但不进候选——
        列进去就会被排进计划，然后卡在派发那一步。"""
        item = dict(self.store.template_item(simple_pattern()))
        item["userData"] = {"endernet:pattern": patterns.encode_pattern(simple_pattern())}
        item["userData"]["endernet:pattern"]["format"] = (
            patterns.PATTERN_MIN_DISPATCHABLE_FORMAT - 1
        )
        self.put_item(3, item)
        self.assertEqual(sorted(self.state.entries), [3])
        self.assertEqual(self.state.usable_entries(), [])
        self.assertEqual(self.store.candidates(IRON_BLOCK), [])


class TestPatternsDoNotLeakIntoInventory(StoreCase):
    """样板是自包含物品，但**绝不进网络库存索引**。

    机制是结构性的：06 给样板接口挂的是**纯内存的中转缓冲**
    （`BUFFER_MEMORY`），它的 9 格方块容器根本没有被挂成存放入口。
    因此「投料占用样板槽」不是靠自觉避免的，是接不上。
    """

    def test_pattern_items_are_distinct_resources(self):
        """`user_data` 参与身份，所以两块内容不同的样板是两个资源。
        这是对的（内容不同就是不同的东西），也是为什么它们不能进库存。"""
        one = self.store.template_item(simple_pattern(1))
        two = self.store.template_item(simple_pattern(2))
        from endernet.common.item_codec import item_key_from_dict

        self.assertNotEqual(item_key_from_dict(one), item_key_from_dict(two))

    def test_the_template_port_buffer_never_touches_the_block_container(self):
        ports = PortStore(
            self.definitions, self.registry, policy=PortPolicy(),
            containers=self.containers,
        )
        state = ports.ensure(self.ref, TEMPLATE_DEVICE)
        self.assertEqual(state.role, TEMPLATE)
        self.assertEqual(state.buffer_kind, BUFFER_MEMORY)
        # 中转位是纯内存的：它没有任何方块容器作为存放处。
        endpoint = ports.buffer_endpoint(state, "op-1")
        self.assertIsNotNone(endpoint)
        self.assertIsNone(state.buffer.backing)

    def test_a_pattern_in_slot_zero_is_invisible_to_the_transfer_buffer(self):
        self.put_pattern(0, simple_pattern())
        ports = PortStore(
            self.definitions, self.registry, policy=PortPolicy(),
            containers=self.containers,
        )
        state = ports.ensure(self.ref, TEMPLATE_DEVICE)
        self.assertEqual(state.buffer.total(), 0)
        self.assertEqual(state.buffer.total_of(IRON_BLOCK), 0)

    def test_the_passive_interface_keeps_using_the_block_container(self):
        """对照：被动接口那 9 格**确实是**存取共用的真实库存。
        两者形态不同这件事，正是「样板槽未被投料占用」的根据。"""
        passive = DeviceRef(key(40), 1)
        self.registry.add(passive, PASSIVE_DEVICE)
        ports = PortStore(
            self.definitions, self.registry, policy=PortPolicy(),
            containers=self.containers,
        )
        state = ports.ensure(passive, PASSIVE_DEVICE)
        self.assertNotEqual(state.buffer_kind, BUFFER_MEMORY)
        self.assertEqual(role_of(PASSIVE_DEVICE), "passive")


# -- 派发前复核 ------------------------------------------------------------


class TestDispatchRecheck(StoreCase):
    """07-04「版本变化使旧规划需复核」+ 07 验收「样板编辑后旧版本计划不能直接派发」。"""

    def setUp(self):
        StoreCase.setUp(self)
        self.put_pattern(3, simple_pattern(1))
        self.entry = self.state.entry_at(3)
        self.digest = self.entry.digest()

    def test_a_unchanged_pattern_passes(self):
        entry, blocker = self.store.check_dispatch(self.ref, 3, self.digest)
        self.assertIs(entry, self.entry)
        self.assertIsNone(blocker)

    def test_an_edited_pattern_makes_the_old_plan_stale(self):
        self.put_pattern(3, simple_pattern(2))
        entry, blocker = self.store.check_dispatch(self.ref, 3, self.digest)
        self.assertIsNone(entry)
        self.assertEqual(blocker.code, Reason.PATTERN_STALE)
        self.assertTrue(blocker.detail)

    def test_a_removed_pattern_makes_the_old_plan_stale(self):
        self.store.uninstall(self.ref, 3)
        entry, blocker = self.store.check_dispatch(self.ref, 3, self.digest)
        self.assertIsNone(entry)
        self.assertEqual(blocker.code, Reason.PATTERN_STALE)

    def test_a_swapped_pattern_makes_the_old_plan_stale(self):
        """换成另一块内容不同的样板：digest 变了，旧计划同样要复核。"""
        other = factory([stack("minecraft:dirt", 1)], [patterns.PatternStack(IRON_BLOCK, 1)])
        self.put_pattern(3, other)
        self.assertNotEqual(self.state.entry_at(3).digest(), self.digest)
        self.assertEqual(
            self.store.check_dispatch(self.ref, 3, self.digest)[1].code,
            Reason.PATTERN_STALE,
        )

    def test_a_removed_provider_is_reported_as_gone(self):
        self.store.drop(self.ref)
        entry, blocker = self.store.check_dispatch(self.ref, 3, self.digest)
        self.assertIsNone(entry)
        self.assertEqual(blocker.code, Reason.DEVICE_GONE)

    def test_an_unloaded_provider_blocks_dispatch(self):
        self.store.mark_loaded(self.ref.node_key, False)
        entry, blocker = self.store.check_dispatch(self.ref, 3, self.digest)
        self.assertIsNone(entry)
        self.assertEqual(blocker.code, Reason.DRIVER_UNLOADED)

    def test_the_wrong_mode_is_reported_separately(self):
        entry, blocker = self.store.check_dispatch(
            self.ref, 3, self.digest, expected_mode=patterns.MODE_CRAFTING
        )
        self.assertIsNone(entry)
        self.assertEqual(blocker.code, Reason.PATTERN_KIND_MISMATCH)

    def test_a_stale_format_is_reported_separately(self):
        item = dict(self.store.template_item(simple_pattern(1)))
        item["userData"] = {"endernet:pattern": patterns.encode_pattern(simple_pattern(1))}
        item["userData"]["endernet:pattern"]["format"] = (
            patterns.PATTERN_MIN_DISPATCHABLE_FORMAT - 1
        )
        self.put_item(3, item)
        entry, blocker = self.store.check_dispatch(self.ref, 3, self.digest)
        self.assertIsNone(entry)
        self.assertEqual(blocker.code, Reason.PATTERN_FORMAT_UNSUPPORTED)

    def test_a_dirty_mark_forces_a_fresh_answer(self):
        """**「派发前复核」必须按现在的样子判断。**

        去抖本来会把重读推到若干帧之后。玩家在第 N 帧把样板拖走了、
        09 在第 N+1 帧来问，若这里读的仍是第 N-1 帧那份记录，
        答案会是「还能派发」——而那正是 07 验收里
        「样板编辑后旧版本计划不能直接派发」要防的事。
        """
        self.container.write_slot(3, None)
        self.store.mark_dirty(self.ref.node_key)
        # 刻意不调 `advance`：去抖还没到期。
        entry, blocker = self.store.check_dispatch(self.ref, 3, self.digest)
        self.assertIsNone(entry)
        self.assertEqual(blocker.code, Reason.PATTERN_STALE)

    def test_an_unrelated_dirty_mark_still_answers_from_the_last_read(self):
        """只有**比上次重读更新**的变更才触发补读，代价因此有界。"""
        before = self.state.stats["refreshes"]
        entry, blocker = self.store.check_dispatch(self.ref, 3, self.digest)
        self.assertIsNotNone(entry)
        self.assertEqual(self.state.stats["refreshes"], before)

    def test_a_slot_holding_something_else_reports_its_own_reason(self):
        """**「槽里的东西坏了」与「样板没了」是两回事。**

        前者玩家要做的是把东西拿出来或升级模组，后者只能重新找样板；
        一律回 `PATTERN_STALE` 会让人以为自己的操作丢了东西。
        """
        self.put_item(4, item_dict_from_key(STONE, 1))
        self.assertEqual(
            self.store.check_dispatch(self.ref, 4, self.digest)[1].code,
            Reason.PATTERN_NOT_A_TEMPLATE,
        )
        crafting = patterns.PatternDefinition(
            patterns.MODE_CRAFTING,
            [stack("minecraft:iron_ingot", 1, slot=0)],
            [patterns.PatternStack(IRON_BLOCK, 1)],
        )
        self.put_pattern(5, crafting)
        self.assertEqual(
            self.store.check_dispatch(self.ref, 5, self.digest)[1].code,
            Reason.PATTERN_KIND_MISMATCH,
        )
        too_new = dict(self.store.template_item(simple_pattern(1)))
        too_new["userData"] = {
            "endernet:pattern": patterns.encode_pattern(simple_pattern(1))
        }
        too_new["userData"]["endernet:pattern"]["format"] = 99
        self.put_item(6, too_new)
        self.assertEqual(
            self.store.check_dispatch(self.ref, 6, self.digest)[1].code,
            Reason.PATTERN_FORMAT_UNSUPPORTED,
        )

    def test_an_unreadable_slot_still_says_stale(self):
        """真正空着的槽才是「没了」——那是 `PATTERN_STALE` 的本来意思。"""
        self.assertEqual(
            self.store.check_dispatch(self.ref, 8, self.digest)[1].code,
            Reason.PATTERN_STALE,
        )

    def test_rechecking_never_touches_the_pattern(self):
        """**它只回答「能不能」，不做事后处置。** 已派发的批次属 09 的台账，
        本模块既不删也不重发。"""
        before = self.state.entry_at(3).digest()
        self.store.check_dispatch(self.ref, 3, "bogus")
        self.store.check_dispatch(self.ref, 3, self.digest)
        self.assertEqual(self.state.entry_at(3).digest(), before)
        self.assertEqual(sorted(self.state.entries), [3])


# -- 写入口 ----------------------------------------------------------------


class TestDispatchRevision(StoreCase):
    def test_one_dirty_signal_reads_provider_once_for_multiple_dispatch_checks(self):
        self.put_pattern(3, simple_pattern(1))
        digest = self.state.entry_at(3).digest()
        self.store.mark_dirty(self.ref.node_key)
        before = self.state.stats["refreshes"]
        for _ in range(5):
            self.assertIsNone(self.store.check_dispatch(self.ref, 3, digest)[1])
        self.assertEqual(self.state.stats["refreshes"], before + 1)
        self.container.write_slot(3, None)
        self.store.mark_dirty(self.ref.node_key)
        self.assertEqual(self.store.check_dispatch(self.ref, 3, digest)[1].code, Reason.PATTERN_STALE)
        self.assertEqual(self.state.stats["refreshes"], before + 2)

    def test_dirty_signal_during_read_is_not_consumed_by_that_read(self):
        self.put_pattern(3, simple_pattern(1))
        digest = self.state.entry_at(3).digest()
        original = self.store._read_slots
        def read_then_change(state):
            ok = original(state)
            self.container.write_slot(3, None)
            self.store.mark_dirty(self.ref.node_key)
            return ok
        self.store._read_slots = read_then_change
        self.store.refresh(self.ref, force=True)
        self.store._read_slots = original
        self.assertEqual(self.store.check_dispatch(self.ref, 3, digest)[1].code, Reason.PATTERN_STALE)

    def test_same_tick_dirty_marks_each_invalidate_dispatch(self):
        before = self.store.dispatch_revision()
        self.store.mark_dirty(self.ref.node_key)
        first = self.store.dispatch_revision()
        self.store.mark_dirty(self.ref.node_key)
        self.assertNotEqual(before, first)
        self.assertNotEqual(first, self.store.dispatch_revision())

    def test_unchanged_refresh_does_not_invalidate_dispatch(self):
        before = self.store.dispatch_revision()
        self.assertTrue(self.store.refresh(self.ref, force=True))
        self.assertEqual(self.store.dispatch_revision(), before)

    def test_read_failure_and_recovery_each_invalidate_dispatch(self):
        before = self.store.dispatch_revision()
        original = self.store.container_at
        self.store.container_at = lambda node_key: None
        self.assertFalse(self.store.refresh(self.ref, force=True))
        failed = self.store.dispatch_revision()
        self.assertNotEqual(before, failed)
        self.store.container_at = original
        self.assertTrue(self.store.refresh(self.ref, force=True))
        self.assertNotEqual(failed, self.store.dispatch_revision())


class TestWritePath(StoreCase):
    def test_install_then_read_back(self):
        ok, detail = self.store.install(self.ref, 3, simple_pattern())
        self.assertTrue(ok, detail)
        self.assertEqual(sorted(self.state.entries), [3])
        self.assertEqual(self.state.entry_at(3).definition, simple_pattern())

    def test_install_updates_the_index_immediately(self):
        self.store.install(self.ref, 3, simple_pattern())
        self.assertEqual(len(self.store.candidates(IRON_BLOCK)), 1)

    def test_install_rejects_a_mode_this_device_does_not_take(self):
        crafting = patterns.PatternDefinition(
            patterns.MODE_CRAFTING,
            [stack("minecraft:iron_ingot", 1, slot=0)],
            [patterns.PatternStack(IRON_BLOCK, 1)],
        )
        ok, detail = self.store.install(self.ref, 3, crafting)
        self.assertFalse(ok)
        self.assertIn(patterns.MODE_CRAFTING, detail)
        self.assertEqual(self.state.entries, {})

    def test_install_rejects_an_out_of_range_slot(self):
        for slot in (-1, 9):
            ok, detail = self.store.install(self.ref, slot, simple_pattern())
            self.assertFalse(ok, slot)
            self.assertIn("越界", detail)

    def test_install_rejects_a_non_definition(self):
        ok, detail = self.store.install(self.ref, 3, "not a pattern")
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_uninstall_clears_the_slot(self):
        self.store.install(self.ref, 3, simple_pattern())
        ok, detail = self.store.uninstall(self.ref, 3)
        self.assertTrue(ok, detail)
        self.assertEqual(self.state.entries, {})
        self.assertEqual(self.store.candidates(IRON_BLOCK), [])

    def test_writes_go_to_the_real_container(self):
        """写的是**方块容器**，不是内存投影——因此拔出/换块都自然生效。"""
        self.store.install(self.ref, 3, simple_pattern())
        data = self.container.read_slot(3)
        self.assertEqual(data["newItemName"], "endernet:factorical_template")
        self.assertIn("endernet:pattern", data["userData"])

    def test_a_written_item_carries_a_readable_tip(self):
        """样板是塞在容器里的实物，玩家只能靠悬停分辨——所以要有提示。"""
        self.store.install(self.ref, 3, simple_pattern())
        data = self.container.read_slot(3)
        lore = data["userData"]["display"]["Lore"]
        self.assertTrue(lore)
        self.assertIn("minecraft:iron_block", lore[0])
        self.assertFalse(data.get("customTips"))

    def test_install_refuses_when_the_container_cannot_be_read(self):
        self.containers.fail = True
        ok, detail = self.store.install(self.ref, 3, simple_pattern())
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_install_does_not_claim_success_when_the_write_was_dropped(self):
        """**写回之后必须核对，不能凭 `write_slot` 的布尔值报成功。**

        架构规划 §6.3 点名的真实缺陷就是「说写进去了、其实没有」
        （`PutItemIntoContainer` 不看 `SetContainerItem` 的返回值）；
        真实引擎上的对应物是 `SpawnItemToContainer` 对满容器/被锁容器
        静静地什么都不做。报成功的代价是玩家看到「已安装」而那一格是空的。
        """
        store = PatternStore(
            self.definitions,
            self.registry,
            containers=_ContainerProvider(container_cls=_LyingContainer),
        )
        state = store.ensure(self.ref, TEMPLATE_DEVICE)
        ok, detail = store.install(self.ref, 0, simple_pattern())
        self.assertFalse(ok)
        self.assertTrue(detail)
        self.assertEqual(state.entries, {})
        self.assertEqual(state.stats["installs"], 0, "没生效就不该计数")

    def test_uninstall_does_not_claim_success_on_a_dropped_write(self):
        """**空槽上的「谎报成功」是测不出来的**——期望的终态（空）本来就成立。
        要测这条必须让那一格真的有东西：先正常装进去，再让容器只谎报写入。"""
        self.put_pattern(3, simple_pattern())
        self.container.__class__ = _LyingContainer
        ok, detail = self.store.uninstall(self.ref, 3)
        self.assertFalse(ok, "清空没生效：那一格读回来还有东西")
        self.assertTrue(detail)
        self.assertEqual(self.state.stats["uninstalls"], 0)
        self.assertEqual(sorted(self.state.entries), [3], "没生效就不该改动索引")

    def test_writes_are_refused_while_unloaded(self):
        """未加载时写不进去，也不该报成功。"""
        self.store.mark_loaded(self.ref.node_key, False)
        self.assertFalse(self.store.install(self.ref, 0, simple_pattern())[0])
        self.assertFalse(self.store.uninstall(self.ref, 0)[0])

    def test_install_refuses_without_a_provider(self):
        ok, detail = self.store.install(DeviceRef(key(99), 1), 0, simple_pattern())
        self.assertFalse(ok)
        self.assertTrue(detail)

    def test_template_item_is_built_by_mode(self):
        """物品标识按模式反查定义表，因此「工厂配方写成合成样板物品」
        这条路径不存在。"""
        self.assertEqual(
            self.store.template_item(simple_pattern())["newItemName"],
            "endernet:factorical_template",
        )
        crafting = patterns.PatternDefinition(
            patterns.MODE_CRAFTING,
            [stack("minecraft:iron_ingot", 1, slot=0)],
            [patterns.PatternStack(IRON_BLOCK, 1)],
        )
        self.assertEqual(
            self.store.template_item(crafting)["newItemName"],
            "endernet:factorical_template",
        )

    def test_template_item_has_no_recipe_when_the_mode_has_no_item(self):
        empty = DefinitionSet(item_templates=())
        store = PatternStore(
            empty, self.registry, containers=self.containers
        )
        state = store.ensure(self.ref, TEMPLATE_DEVICE)
        self.assertIsNone(store.template_item(simple_pattern()))
        self.assertIsNotNone(state)


# -- 重读节拍 --------------------------------------------------------------


class TestRefreshCadence(StoreCase):
    def setUp(self):
        StoreCase.setUp(self)
        # `ensure` 已经读过一次，因此此刻是「干净」的。
        self.store.advance(1)

    def test_a_dirty_mark_is_not_read_in_the_very_same_frame(self):
        """事件触发时容器内容可能还没落定（同框架的参考实现用 `@Delay(0)`
        解决同一件事）。这里走静默 + 去抖，比它更保守。"""
        self.put_item(3, self.store.template_item(simple_pattern()), refresh=False)
        self.store.mark_dirty(self.ref.node_key)
        self.assertEqual(self.store.advance(1), 0)
        self.assertEqual(self.state.entries, {})

    def test_a_dirty_mark_is_read_after_settling(self):
        self.put_item(3, self.store.template_item(simple_pattern()), refresh=False)
        self.store.mark_dirty(self.ref.node_key)
        self.store.advance(1 + PATTERN_SETTLE_FRAMES)
        self.assertEqual(sorted(self.state.entries), [3])

    def test_a_burst_of_changes_collapses_into_one_read(self):
        """合堆、分堆、移动会派发多条乱序事件；去抖把它们并成一次重读。"""
        item = self.store.template_item(simple_pattern())
        self.put_item(3, item, refresh=False)
        for tick in range(1, 6):
            self.store.advance(tick)
            self.store.mark_dirty(self.ref.node_key)
            self.put_item(4, item, refresh=False)
        before = self.state.stats["refreshes"]
        self.store.advance(6 + PATTERN_SETTLE_FRAMES)
        self.assertEqual(sorted(self.state.entries), [3, 4])
        self.assertEqual(self.state.stats["refreshes"], before + 1)

    def test_changes_that_never_settle_still_get_read(self):
        """**这是 `PATTERN_MAX_STALE_FRAMES` 存在的理由。** 一台被漏斗持续
        投料的接口会让脏标记永不到期，少了这条兜底就永远不同步。"""
        item = self.store.template_item(simple_pattern())
        self.put_item(3, item, refresh=False)
        self.store.mark_dirty(self.ref.node_key)
        for tick in range(1, PATTERN_MAX_STALE_FRAMES + 2):
            self.store.advance(tick)
            self.store.mark_dirty(self.ref.node_key)
        self.assertEqual(sorted(self.state.entries), [3])

    def test_unloaded_providers_do_not_eat_the_budget(self):
        """一台卸载的区块不该把已加载设备的刷新饿死。"""
        others = []
        for index in range(PATTERN_REFRESH_BUDGET + 2):
            ref = DeviceRef(key(50 + index), 1)
            self.registry.add(ref, TEMPLATE_DEVICE)
            state = self.store.ensure(ref, TEMPLATE_DEVICE)
            self.store.mark_loaded(ref.node_key, False)
            others.append(state)
        self.put_item(8, self.store.template_item(simple_pattern()), refresh=False)
        self.store.mark_dirty(self.ref.node_key)
        self.store.advance(1 + PATTERN_SETTLE_FRAMES)
        self.assertEqual(sorted(self.state.entries), [8])

    def test_advance_respects_the_budget(self):
        refs = []
        for index in range(PATTERN_REFRESH_BUDGET + 3):
            ref = DeviceRef(key(80 + index), 1)
            self.registry.add(ref, TEMPLATE_DEVICE)
            self.store.ensure(ref, TEMPLATE_DEVICE)
            refs.append(ref)
        for ref in refs:
            self.store.mark_dirty(ref.node_key)
        self.assertLessEqual(self.store.advance(10), PATTERN_REFRESH_BUDGET)

    def test_the_same_frame_refresh_does_not_swallow_a_later_mark(self):
        """同一帧里「先刷新、后收到变更」是常规情形（`observe(ADDED)`
        会先读一次）。用 `>=` 判「已覆盖」会把那次标记吃掉，
        症状是「拖进去的第一块样板不生效、第二块才生效」。"""
        ref = DeviceRef(key(90), 1)
        self.registry.add(ref, TEMPLATE_DEVICE)
        state = self.store.ensure(ref, TEMPLATE_DEVICE)
        container = self.containers.container_for(ref)
        container.write_slot(1, self.store.template_item(simple_pattern()))
        self.store.mark_dirty(ref.node_key)
        self.store.advance(1 + PATTERN_SETTLE_FRAMES)
        self.assertEqual(sorted(state.entries), [1])

    def test_an_unreadable_provider_is_retried_on_its_own(self):
        """**一次瞬时读失败不该让这台设备的索引永远空着。**

        而且那种失败之后**不会有任何变更事件再来**——玩家没有动手，
        容器也没有变化，因此没有别的东西会来叫醒它。这条兜底是唯一的
        重试机会，症状不补就是「这台设备的样板一直显示不出来」。
        """
        self.containers.fail = True
        self.store.refresh(self.ref, force=True)
        self.assertTrue(self.state.unreadable)
        # 这一步是真的写进去了，只是此刻读不到。
        self.containers.fail = False
        self.container.write_slot(3, self.store.template_item(simple_pattern()))
        self.store.advance(PATTERN_MAX_STALE_FRAMES + 1)
        self.assertEqual(sorted(self.state.entries), [3])
        self.assertEqual(self.state.unreadable, "")

    def test_an_unreadable_provider_is_not_retried_every_frame(self):
        """兜底是低频的：读不到的设备不该每帧都去戳一次引擎。"""
        self.containers.fail = True
        self.store.refresh(self.ref, force=True)
        before = self.state.stats["refreshes"]
        for tick in range(self.store.tick + 1, self.store.tick + 5):
            self.store.advance(tick)
        self.assertEqual(self.state.stats["refreshes"], before)

    def test_marking_an_unknown_position_is_harmless(self):
        self.assertFalse(self.store.mark_dirty(key(1234)))

    def test_mark_all_dirty_then_refresh_all(self):
        self.put_pattern(3, simple_pattern())
        self.assertEqual(self.store.mark_all_dirty(), 1)
        self.assertEqual(self.store.refresh_all(), 1)


# -- 上下文与桩 ------------------------------------------------------------


class TestProviderPort(StoreCase):
    def test_the_capability_object_is_registered(self):
        """架构 §4：已声明能力必须真的实例化出对象，不能只有标签。"""
        self.assertIsNotNone(self.state.provider_port)
        self.assertTrue(self.state.capabilities.has("pattern_provider"))

    def test_list_patterns_only_returns_dispatchable_ones(self):
        self.put_pattern(3, simple_pattern())
        listed = self.state.provider_port.list_patterns()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0], simple_pattern())

    def test_push_inputs_without_wiring_refuses_loudly(self):
        """07-05 已落地，但**没接线时照样拒绝，且不能假成功**：`moved=0`
        因此 `is_applied()` 为假，去重账本不会把它记成「已完成」而挡掉
        真正的重试。

        这里的 `service_not_ready` 与「这个功能没做」是两回事：入口在，
        只是这一份夹具没给它工厂服务。所以文案与诊断必须能分辨。
        """
        receipt = self.state.provider_port.push_inputs(
            "op-1", [patterns.PatternStack(IRON, 3)]
        )
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(receipt.requested, 3)
        self.assertEqual(receipt.reason, Reason.SERVICE_NOT_READY)
        self.assertFalse(receipt.is_applied())

    def test_accept_returns_refuses_loudly(self):
        receipt = self.state.provider_port.accept_returns("op-1", [])
        self.assertFalse(receipt.is_applied())
        self.assertEqual(receipt.reason, Reason.BAD_PARAMETER)

    def test_push_inputs_survives_entries_it_cannot_read(self):
        """回执里的 `requested` 只是个对照读数，一条读不懂的条目不该让
        「拒绝」这件事本身**抛异常**。

        但「不抛」不等于「放过」：读不懂就是投不了，理由如实报
        `bad_parameter`，`moved` 仍是 0。**不做成假成功**——把读不懂的
        条目当成 0 件然后报成功，会让调用方以为料已经备好了。
        """
        receipt = self.state.provider_port.push_inputs("op-1", ["junk", None])
        self.assertEqual(receipt.requested, 0)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(receipt.reason, Reason.BAD_PARAMETER)
        self.assertFalse(receipt.is_applied())


class _FakeFactory(object):
    """只记账、不干活的工厂服务门面（`FactoryService.stage_inputs` 的替身）。"""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def stage_inputs(self, node_key, key, count, op_id):
        self.calls.append((node_key, key, count, op_id))
        return self.result


def receipt_dict(status, moved, requested, reason="", holder="p"):
    """`TransferReceipt.to_dict()` 的形状。"""
    return {
        "op_id": "x",
        "requested": requested,
        "moved": moved,
        "status": status,
        "reason": reason,
        "holder": holder,
        "resource_key": None,
    }


def result(status, payload=None, reasons=()):
    """造一个 `CommandResult`。

    **用原始构造器而不是 `CommandResult.blocked`**：那几个快捷方式把
    `payload` 写死成 `None`（`ok`/`partial` 才有），因此造不出
    「blocked **且带着回执**」那一种——而 `uncertain` 恰恰就是那一种。
    """
    return CommandResult("r", status, dict(payload or {}), tuple(reasons))


class TestProviderPortPushInputs(StoreCase):
    """07-05 的执行半边。**全部离线**：假工厂喂造好的 `CommandResult`。

    这一套守的是「薄封装」这四个字：本对象**一个判断都不该自己下**，
    只把活交给 09 的门面，再把回来的结果如实翻译成回执。
    """

    def setUp(self):
        StoreCase.setUp(self)
        self.factory = _FakeFactory()
        self.state.provider_port.factory_of = lambda node_key: self.factory

    def push(self, entries, execution_id="exec-1"):
        return self.state.provider_port.push_inputs(execution_id, entries)

    def one_call(self):
        self.assertEqual(len(self.factory.calls), 1)
        return self.factory.calls[0]

    def test_a_complete_result_becomes_an_applied_receipt(self):
        self.factory.result = result(
            STATUS_OK, {"receipt": receipt_dict("complete", 3, 3)}
        )
        receipt = self.push([patterns.PatternStack(IRON, 3)])
        self.assertEqual(receipt.status, TransferStatus.COMPLETE)
        self.assertEqual(receipt.moved, 3)
        self.assertEqual(receipt.requested, 3)
        self.assertTrue(receipt.is_applied())
        self.assertEqual(receipt.resource_key, IRON)

    def test_the_command_goes_out_through_the_factory_facade(self):
        """**一个判断都不自己下**：位置、资源、数量、`op_id` 原样交出去，
        由 09 的门面判定与发命令。"""
        self.factory.result = result(
            STATUS_OK, {"receipt": receipt_dict("complete", 2, 2)}
        )
        self.push([(IRON, 2)])
        node_key, key, count, op_id = self.one_call()
        self.assertEqual(node_key, self.state.node_key)
        self.assertEqual(key, IRON)
        self.assertEqual(count, 2)
        self.assertIn(IRON.canonical(), op_id)

    def test_the_holder_is_a_string_not_a_tuple(self):
        """`holder` 的契约是 `str`（空串意为「说不清」）。元组不报错，
        只会一路躺进诊断里，而按字符串读它的地方看到的是一串 repr。"""
        self.factory.result = result(
            STATUS_BLOCKED, reasons=[BlockReason(Reason.ORDER_PORT_BUSY, u"占着")]
        )
        receipt = self.push([(IRON, 1)])
        self.assertIsInstance(receipt.holder, str)

    def test_the_op_id_is_stable_for_the_same_request(self):
        """同一次执行、同样的量与资源 → 同一个 `op_id`。去重账本按它记账，
        这是「同一次备料不备第二遍」的全部依据。"""
        self.factory.result = result(
            STATUS_OK, {"receipt": receipt_dict("complete", 1, 1)}
        )
        self.push([(IRON, 1)])
        self.push([(IRON, 1)])
        self.assertEqual(self.factory.calls[0][3], self.factory.calls[1][3])

    def test_the_op_id_changes_when_the_amount_changes(self):
        """**改了量就是另一次备料。** 若 `op_id` 只按 `(执行, 资源)` 派生，
        改量重投会命中去重账本的重放、拿回上一次那份回执：数字对不上，
        而两边都不觉得自己错。"""
        self.factory.result = result(
            STATUS_OK, {"receipt": receipt_dict("complete", 1, 1)}
        )
        self.push([(IRON, 1)])
        self.push([(IRON, 5)])
        self.assertNotEqual(self.factory.calls[0][3], self.factory.calls[1][3])

    def test_two_resources_are_refused_before_anything_is_sent(self):
        """样板口的批次槽只有一格。投两种资源会有一种既不在批次记录里、
        也永远不会被投出去——**账外资产**。宁可拒绝。

        `requested` 报的是**整批**的件数（本来想投多少），不是被采纳的那
        一部分：报小了会让调用方以为少投的那部分是别的原因。
        """
        receipt = self.push([(IRON, 3), (STONE, 4)])
        self.assertEqual(receipt.reason, Reason.UNSUPPORTED)
        self.assertEqual(receipt.requested, 7)
        self.assertEqual(receipt.moved, 0)
        self.assertFalse(receipt.is_applied())
        self.assertEqual(self.factory.calls, [])

    def test_an_entry_without_a_readable_key_is_not_silently_dropped(self):
        """数得出件数、却读不出资源键的条目**必须报错**。当成没看见的话，
        那几件原料既进不了批次记录也永远不会被投出去。"""
        receipt = self.push([(IRON, 3), ("junk", 2)])
        self.assertEqual(receipt.requested, 5)
        self.assertEqual(receipt.reason, Reason.BAD_PARAMETER)
        self.assertFalse(receipt.is_applied())
        self.assertEqual(self.factory.calls, [])

    def test_an_uncertain_receipt_is_never_reported_as_partial(self):
        """**假成功的最短路径。** `uncertain` 的回执里 `moved` 可能大于 0
        （动作发出去了、只是没得到确认）。按「搬动了多少」推断就会在
        **不确定**上报成功，而 `uncertain` 的整条纪律是「不得据此重发整批」。

        注意它是 `blocked` **且带着回执**——`CommandResult.blocked()`
        那个快捷方式丢 payload，所以这一种只能由 06 的 `receipt_result`
        用原始构造器造出来，本用例照那个形状还原。
        """
        self.factory.result = result(
            STATUS_BLOCKED,
            {"receipt": receipt_dict("uncertain", 3, 3)},
            [BlockReason(Reason.CONTAINER_UNCERTAIN, u"不确定")],
        )
        receipt = self.push([(IRON, 3)])
        self.assertEqual(receipt.status, TransferStatus.UNCERTAIN)
        self.assertFalse(receipt.is_applied())

    def test_a_result_without_a_receipt_carries_its_reason(self):
        self.factory.result = result(
            STATUS_BLOCKED, reasons=[BlockReason(Reason.ORDER_PORT_BUSY, u"占着")]
        )
        receipt = self.push([(IRON, 3)])
        self.assertEqual(receipt.reason, Reason.ORDER_PORT_BUSY)
        self.assertEqual(receipt.moved, 0)
        self.assertFalse(receipt.is_applied())

    def test_a_replayed_result_is_marked_as_such(self):
        """重放的回执里 `moved` 是**那一次**搬的量，本次一件都没动。
        不标出来的话，按 `moved` 累加的调用方会重复计数。"""
        self.factory.result = result(
            STATUS_OK,
            {"receipt": receipt_dict("complete", 3, 3)},
            [BlockReason(Reason.DUPLICATE_REQUEST, u"同一条请求已处理过")],
        )
        receipt = self.push([(IRON, 3)])
        self.assertEqual(receipt.reason, Reason.DUPLICATE_REQUEST)
        self.assertTrue(receipt.is_applied())

    def test_an_existing_batch_is_read_back_without_sending_again(self):
        """自家那一批已经备进去过了 → 门面只把批次读数报回来（没发命令）。
        此时回执按**批次**推状态：备了一半就是 `partial`。"""
        self.factory.result = result(
            STATUS_OK, {"batch": {"staged": 2, "requested": 5, "op_id": "x"}}
        )
        receipt = self.push([(IRON, 5)])
        self.assertEqual(receipt.moved, 2)
        self.assertEqual(receipt.status, TransferStatus.PARTIAL)
        self.assertTrue(receipt.is_applied())

    def test_a_returned_batch_reads_back_as_complete_only_when_it_is(self):
        self.factory.result = result(
            STATUS_OK, {"batch": {"staged": 5, "requested": 5, "op_id": "x"}}
        )
        receipt = self.push([(IRON, 5)])
        self.assertEqual(receipt.status, TransferStatus.COMPLETE)
        self.assertEqual(receipt.moved, 5)

    def test_a_missing_gateway_is_reported_not_swallowed(self):
        """门面返回 `None`＝命令网关不可用（没有主控 / 网络没开放）。
        与 `_port_stage` 的约定一致，这里也必须如实报出来。"""
        self.factory.result = None
        receipt = self.push([(IRON, 3)])
        self.assertEqual(receipt.reason, Reason.MASTER_ABSENT)
        self.assertFalse(receipt.is_applied())

    def test_a_device_on_no_network_is_refused(self):
        self.state.provider_port.factory_of = lambda node_key: None
        receipt = self.push([(IRON, 3)])
        self.assertEqual(receipt.reason, Reason.MASTER_ABSENT)
        self.assertEqual(receipt.requested, 3)
        self.assertFalse(receipt.is_applied())

    def test_the_store_hands_the_factory_lookup_to_the_port(self):
        """装配层的注入落在能力对象上——**不是只落在 store 上**。
        两处分开存的话，诊断里写着「已接线」而 `push_inputs` 仍报
        `service_not_ready`。"""
        def lookup(node_key):
            return None

        store = PatternStore(
            self.definitions,
            self.registry,
            containers=self.containers,
            factory_of=lookup,
        )
        state = store.ensure(self.ref, TEMPLATE_DEVICE)
        self.assertIs(state.provider_port.factory_of, lookup)

    def test_describe_admits_the_push_half_is_available(self):
        described = self.state.provider_port.describe()
        self.assertTrue(described["push_inputs"])
        self.assertFalse(described["accept_returns"])
        self.assertEqual(described["pending_task"], "")

    def test_populated_capability_list_is_exposed(self):
        self.assertIn("pattern_provider", self.state.to_dict()["capabilities"])


class TestStubs(StoreCase):
    def test_preset_recipe_source_refuses_rather_than_returning_empty(self):
        """返回空表会让「预设配方功能没做」被读成「这个配方不存在」。

        **查询入口也要拒**：只让 `to_pattern` 拒绝的话，调用方从
        `recipes_for` 拿到的 `[]` 仍然与「查过了，确实没有」无法区分，
        而 `to_pattern` 那条路根本走不到。
        """
        source = NoPresetRecipeSource()
        self.assertIsNotNone(source.blocker())
        self.assertFalse(source.describe()["available"])
        with self.assertRaises(NotImplementedError):
            source.recipes_for(IRON_BLOCK)
        with self.assertRaises(NotImplementedError):
            source.to_pattern(object(), patterns.MODE_CRAFTING)


class TestWriteVerification(StoreCase):
    """写入口的核对与诊断的无副作用（评审查出来的两条）。"""

    def test_diagnostics_do_not_rebuild_the_index(self):
        """**诊断不该有副作用。** 在这里重建索引会把 `index_rebuilds` 加一，
        于是「谁在反复重建索引」这个问题的答案，会被诊断调用自己污染。"""
        self.put_pattern(3, simple_pattern())
        self.store.candidates(IRON_BLOCK)
        before = self.store.stats["index_rebuilds"]
        for _ in range(3):
            self.store.diagnostics()
        self.assertEqual(self.store.stats["index_rebuilds"], before)

    def test_diagnostics_say_when_the_index_count_is_stale(self):
        """过时的计数要标出来，而不是假装它是准的。"""
        report = self.store.diagnostics()
        self.assertIn("index_current", report)
        self.put_pattern(3, simple_pattern())
        # 内容变了、还没查过候选 → 计数是旧的，必须能看出来。
        self.assertFalse(self.store.diagnostics()["index_current"])
        self.store.candidates(IRON_BLOCK)
        self.assertTrue(self.store.diagnostics()["index_current"])

    def test_assembly_chamber_does_not_claim_slots_on_its_own(self):
        """执行器所有权属 09：装配仓不独立领取或释放同一槽位。"""
        chamber = AssemblyChamberExecution()
        self.assertFalse(chamber.accepts_lease(object()))
        self.assertTrue(chamber.describe()["available"])
        self.assertFalse(hasattr(chamber, "claim_slot"))


class TestDegenerateContainers(StoreCase):
    """容器适配器答不上来的时候，样板层必须**照样活着**。

    这一组守的是一类共同的后果：任何一处没兜住的异常都会从
    `refresh` 冒到 `bootstrap.tick()`，被记成 tick_error 就结束本帧——
    本帧剩余的事件批次作废，而那台设备下一帧还会再崩一次。
    在游戏里表现为「有时候同步、有时候不同步」，最难查。
    """

    def _assert_fingerprint_failure_recovers(self, fail_at):
        self.put_pattern(3, simple_pattern())
        self.store.candidates(IRON_BLOCK)
        old_entries = self.state.entries
        old_invalid = self.state.invalid
        revision = self.state.revision
        store_revision = self.store._revision
        changes = self.state.stats["changes"]
        refreshes = self.state.stats["refreshes"]
        store_refreshes = self.store.stats["refreshes"]
        errors = self.state.stats.get("errors", 0)
        self.put_item(3, None, refresh=False)
        self.put_pattern(4, simple_pattern(2), refresh=False)
        state_class = type(self.state)
        original = state_class.content_fingerprint
        calls = []

        def flaky_fingerprint(state):
            calls.append(state)
            if len(calls) == fail_at:
                raise TypeError("fingerprint failed")
            return original(state)

        state_class.content_fingerprint = flaky_fingerprint
        try:
            self.store.tick += 1
            self.assertFalse(self.store.refresh(self.ref, force=True))
        finally:
            state_class.content_fingerprint = original
        self.assertEqual(len(calls), fail_at)
        self.assertIn("TypeError: fingerprint failed", self.state.unreadable)
        self.assertEqual(self.state.stats["errors"], errors + 1)
        self.assertEqual(self.state.refresh_tick, self.store.tick)
        self.assertEqual(self.state.stats["refreshes"], refreshes + 1)
        self.assertEqual(self.store.stats["refreshes"], store_refreshes + 1)
        self.assertEqual(self.state.revision, revision)
        self.assertEqual(self.store._revision, store_revision)
        self.assertEqual(self.state.stats["changes"], changes)
        self.assertIs(self.state.entries, old_entries)
        self.assertIs(self.state.invalid, old_invalid)
        self.assertEqual(self.store.candidates(IRON_BLOCK), [])
        self.store.advance(self.state.refresh_tick + PATTERN_MAX_STALE_FRAMES - 1)
        self.assertEqual(self.state.stats["refreshes"], refreshes + 1)
        self.store.advance(self.state.refresh_tick + PATTERN_MAX_STALE_FRAMES)
        self.assertEqual(self.state.unreadable, "")
        self.assertEqual(self.state.revision, revision + 1)
        self.assertEqual(self.store._revision, store_revision + 1)
        self.assertEqual(self.state.stats["changes"], changes + 1)
        candidates = self.store.candidates(IRON_BLOCK)
        self.assertEqual([item["entry"].slot for item in candidates], [4])

    def test_before_fingerprint_failure_is_contained_and_retried(self):
        self._assert_fingerprint_failure_recovers(1)

    def test_after_fingerprint_failure_is_contained_and_retried(self):
        self._assert_fingerprint_failure_recovers(2)

    def test_a_container_that_answers_none_for_its_size_is_tolerated(self):
        """`slots_count() -> None` 是 13-01 文档允许的答案。
        先 `int()` 再判空会在这里抛 `TypeError`。"""
        store = PatternStore(
            self.definitions,
            self.registry,
            containers=_ContainerProvider(container_cls=_NoCountContainer),
        )
        state = store.ensure(self.ref, TEMPLATE_DEVICE)
        self.assertEqual(state.slot_count, 9)
        self.assertEqual(state.unreadable, "")

    def test_a_container_that_raises_never_escapes_refresh(self):
        """**`refresh` 保证不抛。** 它是重读的唯一入口，调用方分布在引擎
        事件、每帧推进与派发前复核三条性质完全不同的路径上。"""
        store = PatternStore(
            self.definitions,
            self.registry,
            containers=_ContainerProvider(container_cls=_AngryContainer),
        )
        state = store.ensure(self.ref, TEMPLATE_DEVICE)
        self.assertFalse(store.refresh(self.ref, force=True))
        self.assertTrue(state.unreadable)
        self.assertIn("RuntimeError", state.unreadable)

    def test_one_broken_provider_does_not_starve_the_others(self):
        """一台出错不能连累排在它后面的——症状是「有的设备同步、
        有的从不同步」，而那看起来像随机故障。"""
        broken = DeviceRef(key(1), 1)
        healthy = DeviceRef(key(2), 1)
        self.registry.add(broken, TEMPLATE_DEVICE)
        self.registry.add(healthy, TEMPLATE_DEVICE)
        provider = _ContainerProvider()
        store = PatternStore(self.definitions, self.registry, containers=provider)
        broken_state = store.ensure(broken, TEMPLATE_DEVICE)
        healthy_state = store.ensure(healthy, TEMPLATE_DEVICE)
        # 只把「坏的那台」的容器换成会抛的
        provider.by_position[broken.node_key.as_key()] = _AngryContainer(slots=9)
        provider.by_position[healthy.node_key.as_key()].write_slot(
            3, store.template_item(simple_pattern())
        )
        store.mark_dirty(broken.node_key)
        store.mark_dirty(healthy.node_key)
        store.advance(store.tick + PATTERN_SETTLE_FRAMES + 1)
        self.assertTrue(broken_state.unreadable)
        self.assertEqual(sorted(healthy_state.entries), [3])

    def test_a_mid_loop_failure_keeps_the_last_known_entries(self):
        """**半份结果不是结果。** 中途读失败时若把已经读到的那部分当结果，
        「读不到时保留上一次的索引」这条纪律在**中途失败**的路径上就不成立
        ——那台设备会以一份缺了几格的索引对外服务，而缺的那几格正是
        「样板被拔了」这个结论，09 会据此去重做计划。"""
        self.put_pattern(3, simple_pattern())
        self.put_pattern(8, simple_pattern(2))
        self.assertEqual(sorted(self.state.entries), [3, 8])
        container = self.container
        container.__class__ = _FlakyContainer  # 只让这一个位置在 slot 5 上失败
        container.bad_slot = 5
        self.store.refresh(self.ref, force=True)
        self.assertTrue(self.state.unreadable)
        self.assertEqual(sorted(self.state.entries), [3, 8], "上一次的内容必须原样保留")

    def test_a_flaky_read_is_retried_and_recovers(self):
        container = self.container
        container.__class__ = _FlakyContainer
        container.bad_slot = 5
        self.put_item(3, self.store.template_item(simple_pattern()))
        self.store.refresh(self.ref, force=True)
        self.assertEqual(self.state.entries, {}, "读不动时不该留下半份结果")
        container.bad_slot = -1
        self.store.advance(self.store.tick + PATTERN_MAX_STALE_FRAMES + 1)
        self.assertEqual(sorted(self.state.entries), [3])
        self.assertEqual(self.state.unreadable, "")


class TestViews(StoreCase):
    def test_editable_reports_invalid_and_stale_separately(self):
        self.put_pattern(3, simple_pattern())
        self.put_item(4, item_dict_from_key(STONE, 1))
        view = self.store.editable(self.ref)
        self.assertEqual(view["slot_count"], 9)
        self.assertEqual([row["slot"] for row in view["invalid_slots"]], [4])
        self.assertEqual(view["stale_slots"], [])
        self.assertEqual(len(view["patterns"]), 1)
        self.assertTrue(view["patterns"][0]["dispatchable"])
        self.assertTrue(view["editable"]["ordered"])

    def test_editable_never_leaks_engine_objects(self):
        """08/11 拿到的必须是纯数据（「不向客户端暴露引擎对象」）。"""
        self.put_pattern(3, simple_pattern())
        view = self.store.editable(self.ref)
        for row in view["patterns"]:
            self.assertTrue(
                isinstance(row["digest"], (str,)) or type(row["digest"]) is str
            )
            self.assertTrue(isinstance(row["inputs"], int))

    def test_editable_on_an_unknown_position_says_so(self):
        view = self.store.editable(DeviceRef(key(999), 1))
        self.assertIn("error", view)

    def test_view_by_position_and_by_ref(self):
        self.put_pattern(3, simple_pattern())
        self.assertEqual(len(self.store.view(ref=self.ref)["provider"]["entries"]), 1)
        self.assertEqual(
            len(self.store.view(node_key=self.ref.node_key)["provider"]["entries"]), 1
        )
        self.assertEqual(len(self.store.view()["providers"]), 1)

    def test_view_reports_unknown_targets(self):
        self.assertIn("error", self.store.view(node_key=key(999)))

    def test_diagnostics_names_what_is_not_done(self):
        note = self.store.diagnostics()["scope_note"]
        self.assertIn("09", note)
        self.assertIn("07-03", note)
        report = self.store.diagnostics()
        self.assertEqual(report["providers"], 1)
        # R09 只剩一条（两类样板适用范围）。「无限燃料」（2026-09-30 定案：
        # 外部自理）与「返还物显式声明」（同日定案）都不再出现在这份报告里。
        self.assertEqual(len(report["unresolved"]), 1)
        fields = [item["field"] for item in report["unresolved"]]
        self.assertNotIn("pattern.unlimited_fuel", fields)
        self.assertNotIn("pattern.return_rule", fields)
        self.assertIn("pattern.kind_scope", fields)


if __name__ == "__main__":
    unittest.main()
