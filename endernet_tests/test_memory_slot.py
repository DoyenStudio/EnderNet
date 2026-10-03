# coding=utf-8
"""内存槽状态集合：容量与内存槽能耗的权威（2026-10-03）。

**全部离线**：容器用 `containers.FakeContainer` 替身，引擎一行都不碰。

这一套用例守的是几件「离线看着对、真机上会出事」的事：

1. **容量来自容器里的内存条，不来自内存槽的台数。** 在这之前容量是
   `内存槽台数 × 1024 B` 的替身；现在放一台空的内存槽，网络的内存是 **0**。
2. **插拔要在下一次重读时自然生效**，中间靠脏标记 + 去抖，不靠第二条对账。
3. **读不到容器不等于内存条被拔了。** 区块卸载时保留上一次的读数，
   否则一次瞬时失败会让整个网络的内存归零。
4. **从没读到过 ≠ 读到 0 根**：前者算 0 字节、能耗报「待确认」。
5. **只收内存条**：`accepts_put` 是拒收规则的领域层半边
   （`events._veto_or_mark` 只是翻译），且**别家容器一律放行**。
6. **每帧重读有上限**：一台出问题不能连累其余，也不能把帧吃满。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import DefinitionSet
from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.item_codec import item_dict_from_key
from endernet.common.resource_key import ResourceKey
from endernet.server.adapters.containers import FakeContainer
from endernet.server.devices.memory_slot import (
    MEMORY_MAX_STALE_FRAMES,
    MEMORY_REFRESH_BUDGET,
    MEMORY_SETTLE_FRAMES,
    MemorySlotStore,
    is_memory_slot,
)
from endernet.server.runtime.lifecycle import ChangeKind, LifecycleChange

MEMORY_DEVICE = "endernet:memory_slot"
PASSIVE_DEVICE = "endernet:bus_item_io"
TEMPLATE_DEVICE = "endernet:bus_template_io"

STICK_128 = "endernet:memory_128b"
STICK_1K = "endernet:memory_1k"
IRON = ResourceKey.item("minecraft:iron_ingot")


def key(x, y=64, z=0, dimension=0):
    return NodeKey(dimension, x, y, z)


def stick(item_id, count=1):
    return item_dict_from_key(ResourceKey.item(item_id), count)


class _Record(object):
    def __init__(self, ref, device_id):
        self.ref = ref
        self.device_id = device_id
        self.config = {}
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

    def __init__(self, slots=10, max_stack=1):
        self.slots = slots
        self.max_stack = max_stack
        self.by_position = {}
        self.fail = False

    def container_at(self, node_key):
        if self.fail:
            return None
        position = node_key.as_key()
        if position not in self.by_position:
            self.by_position[position] = FakeContainer(
                slots=self.slots, max_stack=self.max_stack
            )
        return self.by_position[position]

    def container_for(self, ref):
        return self.container_at(ref.node_key)


class _FlakyContainer(FakeContainer):
    """读某一个槽时抛异常，其余正常。"""

    def __init__(self, bad_slot, **kwargs):
        FakeContainer.__init__(self, **kwargs)
        self.bad_slot = bad_slot

    def read_slot(self, slot):
        if slot == self.bad_slot:
            raise IOError("这一格读不出来")
        return FakeContainer.read_slot(self, slot)


class MemorySlotCase(unittest.TestCase):
    def setUp(self):
        self.definitions = DefinitionSet()
        self.registry = _Registry()
        self.containers = _ContainerProvider()
        self.notified = []
        self.store = MemorySlotStore(
            self.definitions,
            self.registry,
            containers=self.containers,
            on_load_changed=self.notified.append,
        )
        self.ref = DeviceRef(key(20), 1)
        self.registry.add(self.ref, MEMORY_DEVICE)
        self.state = self.store.ensure(self.ref, MEMORY_DEVICE)
        self.container = self.containers.container_for(self.ref)

    def put(self, slot, item_dict, refresh=True):
        """直接往方块容器里摆一件物品（模拟玩家拖进去）。"""
        self.container.write_slot(slot, item_dict)
        if refresh:
            self.store.refresh(self.ref, force=True)

    def put_stick(self, slot, item_id=STICK_128, refresh=True):
        self.put(slot, stick(item_id), refresh)


# -- 收录与格数 ------------------------------------------------------------


class TestMembership(MemorySlotCase):
    def test_only_memory_slots_are_tracked(self):
        """**按分类判定，不按设备名**（与 `capacity.py` 筛成员同一个口径）。"""
        self.assertTrue(is_memory_slot(MEMORY_DEVICE, self.definitions))
        self.assertFalse(is_memory_slot(PASSIVE_DEVICE, self.definitions))
        self.assertFalse(is_memory_slot(TEMPLATE_DEVICE, self.definitions))
        self.assertFalse(is_memory_slot("minecraft:chest", self.definitions))

    def test_ensure_is_idempotent(self):
        again = self.store.ensure(self.ref, MEMORY_DEVICE)
        self.assertIs(again, self.state)
        self.assertEqual(self.store.stats["created"], 1)

    def test_ensure_refuses_a_device_that_is_not_a_memory_slot(self):
        self.assertIsNone(self.store.ensure(DeviceRef(key(21), 1), PASSIVE_DEVICE))

    def test_generation_mismatch_invalidates_the_state(self):
        """代次不符即失效——同位置换了方块就不该再认。"""
        stale = DeviceRef(self.ref.node_key, self.ref.generation + 1)
        self.assertIsNone(self.store.get_by_ref(stale))

    def test_slot_count_comes_from_the_container(self):
        self.assertEqual(self.state.slot_count, 10)
        store = MemorySlotStore(
            self.definitions, self.registry, containers=_ContainerProvider(slots=4)
        )
        self.assertEqual(store.ensure(self.ref, MEMORY_DEVICE).slot_count, 4)

    def test_slot_count_falls_back_to_the_documented_ten(self):
        """容器答不出格数时退回定义表那条已定案的 10——它与 `gen_blocks.py`
        写进方块容器的 `container_size` 是同一个数。"""

        class _Provider(object):
            def container_at(self, node_key):
                return FakeContainer(slots=0)

        store = MemorySlotStore(
            self.definitions, self.registry, containers=_Provider()
        )
        self.assertEqual(
            store.ensure(self.ref, MEMORY_DEVICE).slot_count, 10
        )

    def test_a_container_without_slot_access_is_reported_not_guessed(self):
        """没有按槽读取能力时**明确报读不到**：整表扫描读不出「哪一格插着什么」，
        会把同型号的两根误当成一根的两倍。"""

        class _Provider(object):
            def container_at(self, node_key):
                return object()

        store = MemorySlotStore(
            self.definitions, self.registry, containers=_Provider()
        )
        state = store.ensure(self.ref, MEMORY_DEVICE)
        self.assertTrue(state.unreadable)
        self.assertEqual(state.bytes(), 0)

    def test_no_container_provider_at_all_is_reported(self):
        store = MemorySlotStore(self.definitions, self.registry, containers=None)
        state = store.ensure(self.ref, MEMORY_DEVICE)
        self.assertTrue(state.unreadable)


# -- 容量 ------------------------------------------------------------------


class TestCapacity(MemorySlotCase):
    def test_empty_memory_slot_contributes_nothing(self):
        """**放一台空的内存槽，网络的内存是 0。** 这正是它替换掉旧替身的意义：
        「有几台内存槽」与「有多少内存」从此是两个数。"""
        self.assertEqual(self.state.sticks(), 0)
        self.assertEqual(self.state.bytes(), 0)
        self.assertEqual(self.store.bytes_of(self.ref.node_key), 0)

    def test_ensure_reads_once_so_inserted_sticks_count_immediately(self):
        """`ensure` 建好就读一次：玩家插进去的内存条不该像没生效一样。"""
        self.put_stick(0)
        fresh = MemorySlotStore(
            self.definitions, self.registry, containers=self.containers
        )
        state = fresh.ensure(self.ref, MEMORY_DEVICE)
        self.assertEqual(state.sticks(), 1)
        self.assertEqual(state.bytes(), 128)

    def test_sticks_add_up_by_model_capacity(self):
        self.put_stick(0, STICK_128)
        self.put_stick(3, STICK_1K)
        self.assertEqual(self.state.sticks(), 2)
        self.assertEqual(self.state.bytes(), 128 + 1024)
        self.assertEqual(
            self.state.memory_ids(), [STICK_128, STICK_1K]
        )

    def test_a_non_memory_item_is_reported_but_contributes_nothing(self):
        """规则生效之前留下的、或别的模组塞进来的东西：不搬动、不记账。"""
        self.put(2, item_dict_from_key(IRON, 1), refresh=False)
        self.put_stick(0)
        self.assertEqual(self.state.sticks(), 1)
        self.assertEqual(self.state.bytes(), 128)
        self.assertIn(2, self.state.invalid)
        self.assertEqual(self.state.invalid[2]["code"], "not_a_memory")

    def test_an_overstacked_slot_is_flagged_and_does_not_inflate_capacity(self):
        """堆叠上限是 1，`count > 1` 属异常：记进诊断，但**不放大容量**
        （宁可少算不可多算，与容量那条纪律一致）。"""
        self.put(0, stick(STICK_1K, count=3))
        self.assertEqual(self.state.sticks(), 1)
        self.assertEqual(self.state.bytes(), 1024)
        self.assertEqual(self.state.stats["overstacked_slots"], 1)

    def test_unknown_position_contributes_zero(self):
        self.assertEqual(self.store.bytes_of(key(99)), 0)
        self.assertEqual(self.store.sticks_of(key(99)), 0)

    def test_unit_count_of_an_unread_slot_is_none(self):
        """没读到过就是「不知道」，不是「0 根」——能源据此停在待确认。"""
        store = MemorySlotStore(self.definitions, self.registry, containers=None)
        store.ensure(self.ref, MEMORY_DEVICE)
        self.assertIsNone(store.unit_count_of(self.ref))

    def test_unit_count_of_a_read_empty_slot_is_zero(self):
        """读到过、并且确实是空的，才是「已知 0 根」。"""
        self.assertEqual(self.store.unit_count_of(self.ref), 0)

    def test_unit_count_of_a_removed_device_is_none(self):
        self.store.drop(self.ref)
        self.assertIsNone(self.store.unit_count_of(self.ref))


# -- 变更检测 --------------------------------------------------------------


class TestRefreshPolicy(MemorySlotCase):
    def test_mark_dirty_does_not_read_the_container(self):
        self.put_stick(0, refresh=False)
        self.assertTrue(self.store.mark_dirty(self.ref.node_key))
        self.assertEqual(self.state.sticks(), 0, "标脏那一刻还不该读")

    def test_nothing_is_read_before_the_settle_window(self):
        self.put_stick(0, refresh=False)
        self.store.mark_dirty(self.ref.node_key)
        for tick in range(1, MEMORY_SETTLE_FRAMES):
            self.store.advance(tick)
        self.assertEqual(self.state.sticks(), 0)
        self.store.advance(MEMORY_SETTLE_FRAMES)
        self.assertEqual(self.state.sticks(), 1)

    def test_a_read_on_the_dirty_frame_does_not_cancel_the_mark(self):
        """**同一帧里「先刷新、后收到变更」是常规情形**（`observe(ADDED)` 先读
        一次，同一帧稍后容器事件才标脏）。那一刻的读数**早于**变更，因此这次
        标记不能被当成「已覆盖」——否则拖进去的第一根内存条要等下一次事件才生效。
        """
        self.store.advance(1)
        self.store.mark_dirty(self.ref.node_key, tick=1)
        self.store.refresh(self.ref, force=True)
        count = self.state.stats["refreshes"]
        self.store.advance(2)
        self.assertEqual(self.state.stats["refreshes"], count, "去抖窗口内不该再读")
        self.store.advance(1 + MEMORY_SETTLE_FRAMES)
        self.assertEqual(
            self.state.stats["refreshes"], count + 1, "静默之后要补读一次"
        )

    def test_a_later_read_covers_an_earlier_mark(self):
        """被之后的某次重读覆盖过的标记，不该再触发第二次重读。"""
        self.put_stick(0, refresh=False)
        self.store.mark_dirty(self.ref.node_key, tick=1)
        self.store.advance(1 + MEMORY_SETTLE_FRAMES)
        self.assertEqual(self.state.sticks(), 1)
        count = self.state.stats["refreshes"]
        for tick in range(4, 30):
            self.store.advance(tick)
        self.assertEqual(self.state.stats["refreshes"], count)

    def test_continuous_changes_still_get_reread(self):
        """**变更一直不停时的兜底。** 少了它，一台被漏斗持续投料的设备
        会让脏标记永不到期，「一直在动、就是不同步」。"""
        for tick in range(1, MEMORY_MAX_STALE_FRAMES + 1):
            self.store.mark_dirty(self.ref.node_key, tick=tick)
            self.store.advance(tick)
        self.assertGreaterEqual(
            self.state.stats["refreshes"], 2, "陈旧兜底没有生效"
        )

    def test_mark_dirty_ignores_other_devices(self):
        self.assertFalse(self.store.mark_dirty(key(99)))

    def test_each_frame_reads_at_most_the_budget(self):
        for index in range(MEMORY_REFRESH_BUDGET + 3):
            ref = DeviceRef(key(30 + index), 1)
            self.registry.add(ref, MEMORY_DEVICE)
            self.store.ensure(ref, MEMORY_DEVICE)
            self.store.mark_dirty(ref.node_key)
        for state in self.store.states():
            state.dirty_tick = 0
            state.refresh_tick = -1
        self.store.tick = 10
        read = self.store.advance(10)
        self.assertEqual(read, MEMORY_REFRESH_BUDGET)


# -- 读不到容器 ------------------------------------------------------------


class TestUnreadable(MemorySlotCase):
    def test_the_last_reading_survives_an_unreadable_container(self):
        """区块卸载、适配器暂时读不到，**都不是「内存条被拔了」**。"""
        self.put_stick(0)
        self.containers.fail = True
        self.assertFalse(self.store.refresh(self.ref, force=True))
        self.assertTrue(self.state.unreadable)
        self.assertEqual(self.state.sticks(), 1)
        self.assertEqual(self.state.bytes(), 128)

    def test_a_midway_failure_discards_the_whole_read(self):
        """中途读失败整次作废：半份结果会让剩下的格子看起来是空的，
        而那正是「内存条被拔了」这个结论。"""
        self.put_stick(0)
        flaky = _FlakyContainer(bad_slot=5, slots=10, max_stack=1)
        flaky.write_slot(0, stick(STICK_1K))
        flaky.write_slot(1, stick(STICK_1K))
        self.containers.by_position[self.ref.node_key.as_key()] = flaky
        self.assertFalse(self.store.refresh(self.ref, force=True))
        self.assertEqual(self.state.bytes(), 128, "应当保留上一次的读数")

    def test_unreadable_states_are_retried_at_the_stale_cadence(self):
        self.containers.fail = True
        self.store.refresh(self.ref, force=True)
        self.store.advance(1)
        self.store.advance(2)
        self.assertGreaterEqual(self.state.stats["refreshes"], 1)


# -- 通知 ------------------------------------------------------------------


class TestNotification(MemorySlotCase):
    def test_a_changed_reading_notifies_once(self):
        self.notified[:] = []
        self.put_stick(0)
        self.assertEqual(self.notified, [self.ref.node_key])

    def test_an_unchanged_reading_does_not_notify(self):
        self.put_stick(0)
        self.notified[:] = []
        self.store.refresh(self.ref, force=True)
        self.assertEqual(self.notified, [])

    def test_removing_a_stick_notifies(self):
        self.put_stick(0)
        self.notified[:] = []
        self.container.write_slot(0, None)
        self.store.refresh(self.ref, force=True)
        self.assertEqual(self.notified, [self.ref.node_key])
        self.assertEqual(self.state.sticks(), 0)


# -- 拒收 ------------------------------------------------------------------


class TestAcceptsPut(MemorySlotCase):
    def test_only_memory_sticks_are_accepted(self):
        self.assertTrue(
            self.store.accepts_put(MEMORY_DEVICE, "endernet:memory_128b")
        )
        self.assertTrue(
            self.store.accepts_put(MEMORY_DEVICE, "endernet:memory_256k")
        )
        self.assertFalse(self.store.accepts_put(MEMORY_DEVICE, "minecraft:iron_ingot"))
        self.assertFalse(
            self.store.accepts_put(MEMORY_DEVICE, "endernet:crafting_template")
        )
        self.assertFalse(self.store.accepts_put(MEMORY_DEVICE, "minecraft:stone"))

    def test_other_devices_are_left_to_their_own_rules(self):
        """别家容器各有各的规则（取样板口认样板、被动接口全都收），
        本模块一律放行——它只答内存槽那一部分。"""
        self.assertTrue(self.store.accepts_put(PASSIVE_DEVICE, "minecraft:stone"))
        self.assertTrue(
            self.store.accepts_put(TEMPLATE_DEVICE, "minecraft:stone")
        )

    def test_an_unreadable_item_name_is_not_rejected(self):
        """读不出物品名就不拦（与 `_veto_or_mark` 同一口径）。"""
        self.assertTrue(self.store.accepts_put(MEMORY_DEVICE, ""))


# -- 生命周期 --------------------------------------------------------------


class TestLifecycle(MemorySlotCase):
    def change(self, kind, ref=None, device_id=MEMORY_DEVICE):
        ref = ref if ref is not None else self.ref
        return LifecycleChange(kind, ref.node_key, device_id, ref=ref)

    def test_added_and_removed_track_the_state(self):
        ref = DeviceRef(key(40), 1)
        self.registry.add(ref, MEMORY_DEVICE)
        self.assertEqual(self.store.observe(self.change(ChangeKind.ADDED, ref)), 1)
        self.assertIsNotNone(self.store.get_by_ref(ref))
        self.assertEqual(self.store.observe(self.change(ChangeKind.REMOVED, ref)), 1)
        self.assertIsNone(self.store.get_by_ref(ref))

    def test_other_devices_are_ignored(self):
        self.assertEqual(
            self.store.observe(self.change(ChangeKind.ADDED, device_id=PASSIVE_DEVICE)),
            0,
        )

    def test_reloading_marks_dirty(self):
        """重新加载之后内容可能已经变了（别的模组、退出前的手工调整）。"""
        self.store.advance(5)
        self.store.observe(self.change(ChangeKind.UNLOADED))
        self.assertFalse(self.state.loaded)
        self.assertEqual(self.store.observe(self.change(ChangeKind.LOADED)), 1)
        self.assertEqual(self.state.dirty_tick, 5)
        self.assertTrue(self.state.loaded)

    def test_stop_marks_everything_unloaded(self):
        self.assertEqual(self.store.mark_all_unloaded(), 1)
        self.assertFalse(self.state.loaded)

    def test_an_unloaded_slot_is_not_reread(self):
        self.put_stick(0, refresh=False)
        self.store.mark_all_unloaded()
        self.store.mark_dirty(self.ref.node_key)
        self.store.advance(10)
        self.assertEqual(self.state.sticks(), 0, "未加载的槽不该被重读")


# -- 诊断 ------------------------------------------------------------------


class TestDiagnostics(MemorySlotCase):
    def test_diagnostics_separate_devices_from_capacity(self):
        """**「有几台内存槽」与「有多少内存」必须分得开**——这正是旧替身
        把两者混为一谈的地方。"""
        self.put_stick(0, STICK_1K)
        report = self.store.diagnostics()
        self.assertEqual(report["devices"], 1)
        self.assertEqual(report["sticks"], 1)
        self.assertEqual(report["capacity_bytes"], 1024)

    def test_state_dump_carries_what_a_player_would_ask(self):
        self.put_stick(0, STICK_1K)
        dumped = self.state.to_dict()
        self.assertEqual(dumped["sticks"], 1)
        self.assertEqual(dumped["capacity_bytes"], 1024)
        self.assertEqual(dumped["memory_ids"], [STICK_1K])
        self.assertEqual(dumped["slot_count"], 10)


if __name__ == "__main__":
    unittest.main()
