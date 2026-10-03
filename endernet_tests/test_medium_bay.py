# coding=utf-8
"""存储驱动器的容器读数：槽位权威、载体写回与展示写回。

对应 05-04（介质挂载、拔出、跨驱动器移动）与 05 的介质详情增量
（2026-10-04）。**这一层是纯领域层**：不 import 引擎，容器是注入的替身。

这里守的是**只有游戏里才看得见**的几条：

- 空白介质第一次进网要发号，并把载体写进**那件物品**——写回要是没核对上，
  下一帧重读看到的还是一块没有载体的空白物品，没有 `_pending_carriers`
  这条账就会**每帧发一个新号**，一口气造出一堆幽灵盘。
- 真机实测 `SpawnItemToContainer` **写成功却返回 `False`**，因此判据只能是
  「再读一次」。
- 读不到容器**不等于盘被拔了**：保留上一次读数，只标 `unreadable`。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common import medium_item
from endernet.common.definitions import DefinitionSet
from endernet.common.identity import NodeKey
from endernet.common.item_codec import item_key_from_dict
from endernet.common.medium_item import CARRIER_VERSION
from endernet.common.protocol import Reason
from endernet.common.resource_key import ResourceKey
from endernet.server.adapters.containers import ContainerEntry, FakeContainer
from endernet.server.devices.registry import DeviceRegistry
from endernet.server.devices.storage_driver import (
    DRIVER_DEVICE_ID,
    DRIVER_PENDING_MAX_ATTEMPTS,
    DRIVER_SETTLE_FRAMES,
    DriverState,
    StorageDriverStore,
    is_storage_driver,
)
from endernet.server.runtime.lifecycle import ChangeKind, LifecycleChange
from endernet.server.storage.medium import DIRECTION_WRITE, MediumPolicy

TAPE = "endernet:singleside_tape"
FLOPPY = "endernet:softdisk_1k"
STONE = ResourceKey.item("minecraft:stone", 0)


def node_key(x, y=64, z=0, dimension=0):
    return NodeKey(dimension, x, y, z)


class LyingContainer(FakeContainer):
    """**写成功却返回 `False`。**

    真机实测 `SpawnItemToContainer` 会这样（`resource_identity.md` §2.3），
    因此判据只能是读回，不能看返回值。
    """

    def write_slot(self, slot, data):
        FakeContainer.write_slot(self, slot, data)
        return False


class DeafContainer(FakeContainer):
    """真的写不进去：写入被丢掉，返回值也是 `False`。

    配合 `seed()` 用：先**绕过写入口**把物品摆进 `_slots`，
    于是「槽里有东西，但怎么改都改不动」这个局面才成立。
    """

    def write_slot(self, slot, data):
        return False

    def seed(self, slot, data):
        self._slots[slot] = ContainerEntry(item_key_from_dict(data), int(data["count"]))


class BlindContainer(object):
    """没有按槽读取能力的容器（不退化）—— 只有格数，没有 `read_slot`。"""

    def slots_count(self):
        return 10


class FlakyContainer(FakeContainer):
    """某个槽读一次就抛（模拟中途读失败）。"""

    def __init__(self, slots=10, bad=5):
        FakeContainer.__init__(self, slots=slots, max_stack=1)
        self.bad = bad

    def read_slot(self, slot):
        if slot == self.bad:
            raise IOError("这一格读不出来")
        return FakeContainer.read_slot(self, slot)


class _Provider(object):
    """位置 -> 假容器。**每个位置一个独立容器**。"""

    def __init__(self, slots=10, factory=None):
        self.slots = int(slots)
        self.factory = factory
        self.by_position = {}

    def container_at(self, node_key):
        position = node_key.as_key()
        if position not in self.by_position:
            if self.factory is None:
                self.by_position[position] = FakeContainer(
                    slots=self.slots, max_stack=1
                )
            else:
                self.by_position[position] = self.factory()
        return self.by_position[position]

    def replace(self, node_key, container):
        self.by_position[node_key.as_key()] = container

    def diagnostics(self):
        return {"fake": True, "cached": len(self.by_position)}


class BayCase(unittest.TestCase):
    """一台驱动器 + 一个可读写的假容器。"""

    def setUp(self):
        self.definitions = DefinitionSet()
        self.registry = DeviceRegistry(self.definitions)
        self.medium_policy = MediumPolicy(frames_per_second=1)
        self.provider = _Provider()
        self.notified = []
        self.store = StorageDriverStore(
            self.definitions,
            self.registry,
            medium_policy=self.medium_policy,
            containers=self.provider,
            on_load_changed=self.notified.append,
        )
        self.ref = self.registry.register(node_key(0), DRIVER_DEVICE_ID).record.ref
        self.state = self.store.ensure(self.ref)

    # -- 夹具动作 ------------------------------------------------------

    def box(self, ref=None):
        return self.provider.container_at((ref or self.ref).node_key)

    def put(self, slot, data, refresh=True):
        """往槽里摆一件物品（**不经任何命令**），可选立刻重读。"""
        container = self.box()
        if hasattr(container, "seed"):
            # 「写不进去」的容器：只能绕过写入口摆。
            container.seed(slot, data)
        else:
            container.write_slot(slot, data)
        if refresh:
            self.assertTrue(self.store.refresh(self.state.ref, force=True))

    def clear(self, slot, refresh=True):
        container = self.box()
        if hasattr(container, "seed"):
            container._slots[slot] = None
        else:
            container.write_slot(slot, None)
        if refresh:
            self.assertTrue(self.store.refresh(self.state.ref, force=True))

    def blank(self, name=TAPE, count=1):
        return testsupport.item_dict_without_carrier(name, count)

    def with_carrier(self, name, medium_id, version=None, count=1):
        return testsupport.item_dict_with_carrier(
            name, medium_id, count=count, version=version
        )


# -- 成员与容器 --------------------------------------------------------------


class TestMembership(BayCase):
    def test_is_storage_driver_goes_by_category(self):
        self.assertTrue(is_storage_driver(DRIVER_DEVICE_ID, self.definitions))
        self.assertFalse(is_storage_driver("endernet:memory_slot", self.definitions))
        self.assertFalse(
            is_storage_driver("endernet:common_terminal_panel", self.definitions)
        )
        # 定义表缺失（离线夹具）时退回名字比较。
        self.assertTrue(is_storage_driver(DRIVER_DEVICE_ID, None))
        self.assertFalse(is_storage_driver("endernet:memory_slot", None))

    def test_slot_count_comes_from_the_container(self):
        """格数由容器回答，不是定义表里的那个数。"""
        self.assertEqual(self.state.slot_count, 10)
        provider = _Provider(slots=3)
        store = StorageDriverStore(
            self.definitions,
            self.registry,
            medium_policy=self.medium_policy,
            containers=provider,
        )
        state = store.ensure(self.ref)
        self.assertEqual(state.slot_count, 3)

    def test_a_container_without_slot_reads_is_reported_not_guessed(self):
        self.provider.replace(self.ref.node_key, BlindContainer())
        self.assertFalse(self.store.refresh(self.state.ref, force=True))
        self.assertIn(u"不支持按槽读取", self.state.unreadable)

    def test_an_unreadable_container_keeps_the_previous_reading(self):
        """**区块卸载不等于盘被拔了**：读不到就沿用上一次的读数。"""
        medium, _, _ = self.store.create_medium(TAPE)
        self.put(0, self.with_carrier(TAPE, medium.medium_id))
        self.assertEqual(self.state.slot_of(medium.medium_id), 0)

        self.store.containers = None
        self.assertFalse(self.store.refresh(self.state.ref, force=True))
        self.assertTrue(self.state.unreadable)
        self.assertEqual(self.state.slot_of(medium.medium_id), 0, "读不到不改旧读数")

    def test_a_midway_read_failure_discards_the_whole_reading(self):
        """半份结果会让剩下的槽看起来是空的，因此整次作废。"""
        medium, _, _ = self.store.create_medium(TAPE)
        self.put(0, self.with_carrier(TAPE, medium.medium_id))
        self.provider.replace(self.ref.node_key, FlakyContainer(slots=10, bad=5))
        self.assertFalse(self.store.refresh(self.state.ref, force=True))
        self.assertTrue(self.state.unreadable)
        self.assertEqual(self.state.slot_of(medium.medium_id), 0, "旧读数原样保留")


# -- 空白介质：发号 + 写回 ----------------------------------------------------


class TestBlankMedium(BayCase):
    def test_a_blank_medium_gets_a_carrier_written_into_it(self):
        self.put(0, self.blank(TAPE))
        medium_ids = self.state.medium_ids()
        self.assertEqual(len(medium_ids), 1)
        medium_id = medium_ids[0]
        self.assertIsNotNone(self.store.medium(medium_id))
        self.assertEqual(
            testsupport.carrier_of_item(self.box().read_slot(0)),
            (medium_id, CARRIER_VERSION),
        )
        self.assertEqual(self.store.stats["carriers_written"], 1)
        self.assertEqual(self.store.diagnostics()["pending_carriers"], {})

    def test_reading_twice_does_not_allocate_a_second_id(self):
        """写回成功之后就没有待办了，重读多少次都只有那一个号。"""
        self.put(0, self.blank(TAPE))
        first = self.state.medium_ids()[0]
        for _ in range(3):
            self.assertTrue(self.store.refresh(self.state.ref, force=True))
        self.assertEqual(self.state.medium_ids(), (first,))
        self.assertEqual(len(self.store.mediums()), 1)

    def test_a_lying_write_still_counts_as_a_mount(self):
        """`SpawnItemToContainer` 会写成功却返回 `False`——**判据是读回**。"""
        self.provider.replace(self.ref.node_key, LyingContainer(slots=10, max_stack=1))
        self.put(0, self.blank(TAPE))
        self.assertEqual(len(self.state.medium_ids()), 1)

    def test_a_write_that_never_lands_reuses_one_id(self):
        """写不进去时**保留待办**，下一帧用同一个号重试——绝不每帧发新号。"""
        self.provider.replace(self.ref.node_key, DeafContainer(slots=10, max_stack=1))
        self.put(0, self.blank(TAPE))
        for _ in range(3):
            # 读得到容器（refresh 成功），只是写不进去。
            self.assertTrue(self.store.refresh(self.state.ref, force=True))
        pending = self.store.diagnostics()["pending_carriers"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(self.store.mediums()), 1, "三次重读只发了一个号")
        self.assertEqual(self.state.medium_ids(), (), "没核对上就不挂载")
        self.assertEqual(
            self.state.invalid.get(0, {}).get("code"), "carrier_write_unverified"
        )

    def test_the_pending_entry_gives_up_after_the_attempt_limit(self):
        self.provider.replace(self.ref.node_key, DeafContainer(slots=10, max_stack=1))
        self.put(0, self.blank(TAPE))
        for _ in range(DRIVER_PENDING_MAX_ATTEMPTS + 3):
            self.store.refresh(self.state.ref, force=True)
        self.assertEqual(len(self.store.mediums()), 1, "放弃之后也不再发新号")
        self.assertIn(u"已停止重试", self.state.invalid.get(0, {}).get("detail", u""))

    def test_a_different_model_in_the_same_slot_drops_the_pending_entry(self):
        """槽里换成另一种型号：旧的待办作废（那块介质记录留着，不标孤儿）。"""
        self.provider.replace(self.ref.node_key, DeafContainer(slots=10, max_stack=1))
        self.put(0, self.blank(TAPE))
        first = self.store.diagnostics()["pending_carriers"]
        self.assertEqual(len(first), 1)
        self.put(0, self.blank(FLOPPY))
        pending = self.store.diagnostics()["pending_carriers"]
        self.assertEqual(len(pending), 1)
        self.assertNotIn(list(first.values())[0], list(pending.values()))


# -- 载体命中、孤儿与坏载体 --------------------------------------------------


class TestCarrier(BayCase):
    def test_a_known_carrier_mounts_without_allocating(self):
        medium, _, _ = self.store.create_medium(TAPE)
        before = len(self.store.mediums())
        self.put(0, self.with_carrier(TAPE, medium.medium_id))
        self.assertEqual(self.state.slot_of(medium.medium_id), 0)
        self.assertEqual(len(self.store.mediums()), before, "不发新号")

    def test_a_carrier_without_a_record_is_an_orphan_that_keeps_the_slot(self):
        """**占着槽、不可交易、不当空盘、不发号。**"""
        self.put(0, self.with_carrier(TAPE, "medium:999"))
        self.assertEqual(self.state.slot_of("medium:999"), 0)
        self.assertIsNone(self.store.medium("medium:999"))
        self.assertEqual(self.store.stats["mediums_created"], 0, "不发号")
        receipt = self.state.provider.read("medium:999", STONE, 1)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(receipt.reason, Reason.MEDIUM_UNRECOVERED)

    def test_a_malformed_carrier_is_not_treated_as_blank(self):
        """有载体键却读不动 → **绝不当空白盘再发号**（那会把有内容的盘重识别成新盘）。

        只列**两种语言下都读不动**的形状：ID 不是文本（`7`）、载体本身不是字典
        （`None`）。py2 独有的那条「unicode ID 被引擎丢掉」在 py3 上复现不了，
        由 `medium_item` 自己的用例覆盖。
        """
        for bad_value in (7, None):
            store = self._fresh_store()
            state = store.get_by_ref(self.ref)
            bad = self.blank(TAPE)
            bad["userData"] = {medium_item.MEDIUM_CARRIER_KEY: bad_value}
            self.box().write_slot(0, bad)
            store.refresh(self.ref, force=True)
            self.assertEqual(
                state.invalid.get(0, {}).get("code"), "malformed_carrier", bad_value
            )
            self.assertEqual(state.medium_ids(), ())
            self.assertEqual(len(store.mediums()), 0, "不发号")
            self.box().write_slot(0, None)

    def test_a_bad_version_field_is_a_malformed_carrier(self):
        bad = self.blank(TAPE)
        bad["userData"] = {medium_item.MEDIUM_CARRIER_KEY: {"id": "medium:1", "v": "1"}}
        self.put(0, bad)
        self.assertEqual(self.state.invalid.get(0, {}).get("code"), "malformed_carrier")

    def test_an_unknown_carrier_version_is_not_mounted(self):
        """不猜新版本的语义：报出来，不挂载。"""
        self.put(
            0,
            self.with_carrier(TAPE, "medium:1", version=CARRIER_VERSION + 1),
        )
        self.assertEqual(
            self.state.invalid.get(0, {}).get("code"), "unknown_carrier_version"
        )
        self.assertEqual(self.state.medium_ids(), ())

    def _fresh_store(self):
        self.store = StorageDriverStore(
            self.definitions,
            self.registry,
            medium_policy=self.medium_policy,
            containers=self.provider,
        )
        self.state = self.store.ensure(self.ref)
        return self.store


# -- 非法内容与重复 ----------------------------------------------------------


class TestInvalidContent(BayCase):
    def test_a_non_medium_item_is_reported_and_not_moved(self):
        self.put(0, self.blank("minecraft:stone"))
        self.assertEqual(self.state.invalid.get(0, {}).get("code"), "not_a_medium")
        self.assertIsNotNone(self.box().read_slot(0), "**不搬动它**")

    def test_a_stack_in_one_slot_is_reported_and_left_alone(self):
        """一格叠了多块：拆开是一道未定规则，本轮不改动它。"""
        self.put(0, self.blank(TAPE, count=64))
        self.assertEqual(self.state.invalid.get(0, {}).get("code"), "stacked_media")
        self.assertEqual(len(self.store.mediums()), 0, "不发号")
        self.assertEqual(self.box().read_slot(0)["count"], 64, "原样不动")

    def test_the_same_medium_in_two_slots_of_one_driver(self):
        medium, _, _ = self.store.create_medium(TAPE)
        item = self.with_carrier(TAPE, medium.medium_id)
        self.box().write_slot(0, dict(item))
        self.box().write_slot(3, dict(item))
        self.assertTrue(self.store.refresh(self.state.ref, force=True))
        self.assertEqual(self.state.slot_of(medium.medium_id), 0, "槽号小者胜")
        self.assertEqual(
            self.state.invalid.get(3, {}).get("code"), "duplicate_in_driver"
        )

    def test_the_same_medium_on_two_drivers_is_resolved_deterministically(self):
        """两台驱动器各插一块同 ID 的盘：按 (位置文本, 槽号) 保留一个。"""
        other_ref = self.registry.register(
            node_key(5), DRIVER_DEVICE_ID
        ).record.ref
        other = self.store.ensure(other_ref)
        medium, _, _ = self.store.create_medium(TAPE)
        item = self.with_carrier(TAPE, medium.medium_id)
        self.box().write_slot(0, dict(item))
        self.provider.container_at(other_ref.node_key).write_slot(0, dict(item))

        self.assertTrue(self.store.refresh(self.state.ref, force=True))
        self.assertTrue(self.store.refresh(other_ref, force=True))
        self.assertEqual(self.state.slot_of(medium.medium_id), 0, "字典序小者胜")
        self.assertIsNone(other.slot_of(medium.medium_id))
        self.assertEqual(
            other.invalid.get(0, {}).get("code"), "duplicate_across_drivers"
        )
        self.assertEqual(len(self.store.diagnostics()["orphan_slots"]), 0)


# -- 三态与变化 --------------------------------------------------------------


class TestReadings(BayCase):
    def test_never_read_is_none_but_read_empty_is_zero(self):
        blind = StorageDriverStore(
            self.definitions,
            self.registry,
            medium_policy=self.medium_policy,
        )
        state = blind.ensure(self.ref)
        self.assertFalse(state.has_reading())
        self.assertIsNone(blind.disk_count_of(self.ref), "从没读到过 → 待确认")
        self.assertTrue(self.state.has_reading())
        self.assertEqual(self.store.disk_count_of(self.ref), 0, "读到空 = 已知 0")

    def test_swapping_the_medium_bumps_the_slot_version(self):
        first, _, _ = self.store.create_medium(TAPE)
        self.put(0, self.with_carrier(TAPE, first.medium_id))
        version = self.state.assignment(0).slot_version
        second, _, _ = self.store.create_medium(FLOPPY)
        self.put(0, self.with_carrier(FLOPPY, second.medium_id))
        self.assertEqual(self.state.slot_of(second.medium_id), 0)
        self.assertGreater(self.state.assignment(0).slot_version, version)

    def test_taking_the_item_out_keeps_the_record_tradeable(self):
        medium, _, _ = self.store.create_medium(TAPE)
        self.put(0, self.with_carrier(TAPE, medium.medium_id))
        self.clear(0)
        self.assertEqual(self.state.medium_ids(), ())
        self.assertIsNotNone(self.store.medium(medium.medium_id))
        self.assertFalse(self.store.medium(medium.medium_id).orphaned)
        self.assertIn(medium.medium_id, self.store.diagnostics()["unclaimed_mediums"])

    def test_the_lifecycle_marks_and_drops(self):
        medium, _, _ = self.store.create_medium(TAPE)
        self.put(0, self.with_carrier(TAPE, medium.medium_id))

        self.store.observe(
            LifecycleChange(
                ChangeKind.UNLOADED, self.ref.node_key, DRIVER_DEVICE_ID, self.ref
            )
        )
        self.assertFalse(self.state.loaded)
        self.assertEqual(self.state.slot_of(medium.medium_id), 0, "卸载不清读数")
        self.assertGreater(self.state.dirty_tick, -1, "重载后要重读")

        self.store.observe(
            LifecycleChange(
                ChangeKind.LOADED, self.ref.node_key, DRIVER_DEVICE_ID, self.ref
            )
        )
        self.assertTrue(self.state.loaded)
        self.store.observe(
            LifecycleChange(
                ChangeKind.REMOVED, self.ref.node_key, DRIVER_DEVICE_ID, self.ref
            )
        )
        self.assertIsNone(self.store.get_by_ref(self.ref))
        self.assertFalse(medium.orphaned, "拆除不标孤儿")


# -- 展示写回（容量 Lore）----------------------------------------------------


class TestDisplayWriteBack(BayCase):
    def lore_of(self, slot=0):
        return testsupport.managed_lines_of(self.box().read_slot(slot))

    def test_the_managed_lines_are_written_back_once(self):
        medium, _, _ = self.store.create_medium(TAPE)
        self.put(0, self.with_carrier(TAPE, medium.medium_id))
        self.assertEqual(self.lore_of(), (), "刚挂上时物品上还没有摘要")

        self.store.tick = DRIVER_SETTLE_FRAMES + 1
        self.assertEqual(self.store.advance_display(), 1)
        lines = self.lore_of()
        self.assertEqual(len(lines), 2, lines)
        for line in lines:
            self.assertTrue(medium_item.is_managed_line(line))
        # **显示不变就跳过写回**：重新标一次脏，再跑一次仍然一个字都不写
        # （比较的是管理行本身，不是内容版本——取放相抵时版本回了原样，
        # 而 Lore 本来就没变，不该白写一遍）。
        self.store.note_display_dirty(medium.medium_id)
        self.store.tick += DRIVER_SETTLE_FRAMES + 1
        self.assertEqual(self.store.advance_display(), 0)
        self.assertGreaterEqual(self.store.stats["display_skipped"], 1)
        self.assertEqual(self.lore_of(), lines, "写过的行一字未动")

    def test_a_content_change_marks_the_display_dirty(self):
        medium, _, _ = self.store.create_medium(TAPE)
        self.put(0, self.with_carrier(TAPE, medium.medium_id))
        self.store.tick = DRIVER_SETTLE_FRAMES + 1
        self.assertEqual(self.store.advance_display(), 1)
        before = tuple(self.lore_of())

        plan = medium.plan(STONE, 3, DIRECTION_WRITE, 100)
        self.assertTrue(plan.ok)
        medium.commit(plan, 100)
        self.store.note_medium_changed(medium.medium_id)
        self.store.note_display_dirty(medium.medium_id)

        self.store.tick += DRIVER_SETTLE_FRAMES + 1
        self.assertEqual(self.store.advance_display(), 1)
        after = self.lore_of()
        self.assertEqual(len(after), 2)
        self.assertNotEqual(tuple(after), before, "内容变了，摘要该跟着变")

    def test_a_players_own_lore_line_is_kept(self):
        medium, _, _ = self.store.create_medium(TAPE)
        item = self.with_carrier(TAPE, medium.medium_id)
        item["userData"]["display"] = {"Lore": [u"玩家自己的说明"]}
        self.put(0, item)

        self.store.tick = DRIVER_SETTLE_FRAMES + 1
        self.store.advance_display()
        lore = self.box().read_slot(0)["userData"]["display"]["Lore"]
        self.assertIn(u"玩家自己的说明", lore)
        self.assertEqual(len(lore), 3, "两句管理行 + 玩家那一句")

    def test_writing_the_lines_does_not_change_the_identity(self):
        """容量行是**派生展示**：写它不得改变那件物品的资源身份。"""
        medium, _, _ = self.store.create_medium(TAPE)
        self.put(0, self.with_carrier(TAPE, medium.medium_id))
        before = item_key_from_dict(self.box().read_slot(0)).canonical()

        self.store.tick = DRIVER_SETTLE_FRAMES + 1
        self.assertEqual(self.store.advance_display(), 1)
        after = item_key_from_dict(self.box().read_slot(0)).canonical()
        self.assertEqual(before, after)

    def test_a_medium_that_left_the_slot_is_dropped_from_the_queue(self):
        medium, _, _ = self.store.create_medium(TAPE)
        self.put(0, self.with_carrier(TAPE, medium.medium_id))
        self.clear(0)
        self.store.tick = DRIVER_SETTLE_FRAMES + 1
        self.assertEqual(self.store.advance_display(), 0)
        self.assertEqual(self.store.pending_display_writes(), 0)
        self.assertGreaterEqual(self.store.stats["display_skipped"], 1)

    def test_the_display_write_waits_for_the_settle_window(self):
        medium, _, _ = self.store.create_medium(TAPE)
        self.put(0, self.with_carrier(TAPE, medium.medium_id))
        self.store.tick = 1
        self.assertEqual(self.store.advance_display(), 0, "静默期没到，不写")
        self.assertGreater(self.store.pending_display_writes(), 0)


class TestRefreshNeverRaises(BayCase):
    """`refresh` **保证不抛**（同 `PatternStore.refresh`），异常只登记不外泄。

    2026-10-04 真机补的一次：最先那次指纹与最后那几步落地原先都在 `try`
    外面，一次偶发失败就真的逃了出去。调用方分布在引擎事件与每帧推进两条
    路径上，逃出去会作废本帧剩余的事件批次——因此契约必须由用例钉住。
    """

    def _break(self, patch, restore):
        def outer(fn):
            setattr(self.store, patch, fn)
            try:
                return self.store.refresh(self.state.ref, force=True)
            finally:
                setattr(self.store, patch, restore)
        return outer

    def test_a_fingerprint_failure_does_not_escape(self):
        medium, _, _ = self.store.create_medium(TAPE)
        self.put(0, self.with_carrier(TAPE, medium.medium_id))
        original = DriverState.content_fingerprint

        def broken(state):
            raise TypeError("an integer is required")

        DriverState.content_fingerprint = broken
        try:
            self.assertFalse(self.store.refresh(self.state.ref, force=True))
        finally:
            DriverState.content_fingerprint = original
        self.assertIn(u"an integer is required", self.state.unreadable)
        self.assertEqual(self.state.stats["errors"], 1)
        self.assertEqual(self.state.slot_of(medium.medium_id), 0, "旧读数原样保留")

    def test_a_read_slots_failure_does_not_escape(self):
        original = self.store._read_slots
        result = self._break("_read_slots", original)(
            lambda state: (_ for _ in ()).throw(IOError("boom"))
        )
        self.assertFalse(result)
        self.assertEqual(self.state.stats["errors"], 1)

    def test_a_dedup_failure_does_not_escape(self):
        original = self.store._resolve_cross_driver_duplicates
        result = self._break("_resolve_cross_driver_duplicates", original)(
            lambda: (_ for _ in ()).throw(ValueError("boom"))
        )
        self.assertFalse(result)
        self.assertEqual(self.state.stats["errors"], 1)

    def test_advance_does_not_let_one_bad_driver_kill_the_frame(self):
        """`advance` 里单台出错只记账，不连累排在后面的设备。"""
        self.store.mark_dirty(self.ref.node_key, tick=0)
        self.store._read_slots = lambda state: (
            _ for _ in ()
        ).throw(IOError("boom"))
        try:
            self.assertEqual(self.store.advance(DRIVER_SETTLE_FRAMES + 2), 0)
        finally:
            del self.store._read_slots
        self.assertEqual(self.state.stats["errors"], 1)
        self.assertIsNotNone(self.store.get_by_ref(self.ref))


if __name__ == "__main__":
    unittest.main()
