# coding=utf-8
"""存储驱动器的领域层：介质、槽位、汇总索引、额度与受控读写。

对应任务 05-01 至 05-07 的驱动器侧。**全部离线**：引擎边界不在这里，
领域层不许 import `tooldelta` / `mod.*`，`test_import_boundaries` 静态拦着。

这一套测试刻意不使用复制实现逻辑的断言：凡是「守恒」「不重复计入」这类
性质，都用**独立求和**（`Medium.recompute_usage`、`StorageService.audit`）
去验证，而不是把被测代码的算法再抄一遍。抄一遍只会一起错。

**额度是所有用例里最容易写错的地方。** 夹具取 `frames_per_second=1`，
于是「1 逻辑帧 = 1 秒」；介质规格又是「单位物/秒」，而 1 物品 = 50 微单位物。
一块小型单面磁带因此**每帧最多放行 4 件**——凡是需要更多库存的用例都走
`seed()`，它按额度分帧存入，而不是假装额度不存在。
"""

import io
import json
import os
import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import (
    STORAGE_DRIVER_SLOT_COUNT,
    DefinitionSet,
)
from endernet.common.identity import NodeKey
from endernet.common.protocol import (
    CommandKind,
    CommandSource,
    DeviceCommand,
    Reason,
    STATUS_BLOCKED,
    STATUS_OK,
    STATUS_REJECTED,
    TransferStatus,
)
from endernet.common.resource_key import ResourceKey
from endernet.server.devices.capabilities import STORAGE_PROVIDER
from endernet.server.devices.registry import DeviceRegistry
from endernet.server.devices.storage_driver import (
    DISK_SCOPE_HARDDISKS_ONLY,
    DRIVER_DEVICE_ID,
    DRIVER_RECORD_KIND,
    MEDIUM_RECORD_KIND,
    DriverPolicy,
    StorageDriverStore,
    medium_sequence,
)
from endernet.server.persistence.repository import MemoryRepository, Record
from endernet.server.network.controller import RunState
from endernet.server.network.node import NetworkNode
from endernet.server.runtime.events import NormalizedEvent
from endernet.server.runtime.lifecycle import ChangeKind, LifecycleChange
from endernet.server.storage.medium import (
    DIRECTION_READ,
    DIRECTION_WRITE,
    Medium,
    MediumPolicy,
    MediumRecovery,
    TransferBudget,
)
from endernet.server.storage.service import StorageService

TAPE = "endernet:singleside_tape"
FLOPPY = "endernet:softdisk_1k"
HARDDISK = "endernet:harddisk_1k"
BIG_TAPE = "endernet:database_compressed_tape"

STONE = ResourceKey.item("minecraft:stone")
DIRT = ResourceKey.item("minecraft:dirt")
WATER = ResourceKey.fluid("minecraft:water")

#: 小型单面磁带 4 单位物/s，夹具取 1 帧/秒、桶容量 1 秒 => 每帧 4 件。
TAPE_QUOTA = 4

#: 一个远到额度必然攒满的逻辑帧号。用于「只考察容量/类型」的用例：
#: 桶容量封顶，因此推得足够远之后额度就不再是限制项。
FAR_TICK = 10 ** 7


def key(x, y=64, z=0, dimension=0):
    # type: (int, int, int, int) -> NodeKey
    return NodeKey(dimension, x, y, z)


def ample_policy(**kwargs):
    """一个把额度挪开的策略：容量与类型配额才是唯一的限制。

    「桶容量」本身就是可配的策略（R07），因此测试用它来隔离变量是完全
    正当的做法——比在每条用例里手工凑帧数清楚得多。
    """
    kwargs.setdefault("frames_per_second", 1)
    kwargs.setdefault("bucket_seconds", 10 ** 6)
    return MediumPolicy(**kwargs)


class Fixture(unittest.TestCase):
    """一套最小的驱动器 + 网络服务夹具。"""

    def setUp(self):
        self.definitions = DefinitionSet()
        self.registry = DeviceRegistry(self.definitions)
        self.loads = []
        self.medium_policy = MediumPolicy(frames_per_second=1)
        self.driver_policy = DriverPolicy()
        self.store = StorageDriverStore(
            self.definitions,
            self.registry,
            policy=self.driver_policy,
            medium_policy=self.medium_policy,
            on_load_changed=self.loads.append,
        )
        self.service = StorageService(drivers=self.store, policy=self.medium_policy)
        self.record = self.place(0)
        self.state = self.store.get_by_ref(self.record.ref)
        self.node = NetworkNode.from_record(self.definitions, self.record)

    # -- 夹具动作 ------------------------------------------------------

    def place(self, x, device_id=DRIVER_DEVICE_ID):
        """放置一台驱动器，走与运行时同一条生命周期入口。"""
        outcome = self.registry.register(key(x), device_id)
        self.store.observe(
            LifecycleChange(
                ChangeKind.ADDED, outcome.record.node_key, device_id, outcome.record.ref
            )
        )
        return outcome.record

    def new_medium(self, model=TAPE, slot=0, mount_network=True):
        """新建一块盘并装进槽位；默认同时挂到网络上。"""
        medium, reason, detail = self.store.create_medium(model)
        self.assertIsNotNone(medium, "%s / %s" % (reason, detail))
        ok, reason, detail = self.state.provider.mount(slot, medium.medium_id)
        self.assertTrue(ok, "%s / %s" % (reason, detail))
        if mount_network:
            self.service.add_node(self.node)
        return medium

    def seed(self, resource, count, tick=10):
        """按额度分帧存入 `count` 件，返回下一次可用的帧号。

        **不假装额度不存在**：一次请求放不下就分到下一帧去，这样用例
        表达的才是「库存有多少」，而不是「桶有多大」。
        """
        remaining = count
        while remaining > 0:
            chunk = min(remaining, TAPE_QUOTA)
            receipt = self.service.commit_insert(
                "seed:%s:%d" % (resource.name, tick), resource, chunk, now_tick=tick
            )
            self.assertEqual(receipt.moved, chunk, receipt)
            remaining -= chunk
            tick += 1
        return tick

    def command(self, payload, kind=CommandKind.CONFIGURE, request_id="r1"):
        # type: (dict, str, str) -> DeviceCommand
        ref = self.record.ref
        return DeviceCommand(
            request_id,
            kind,
            CommandSource("p1", ref, 0),
            payload=payload,
            device_ref=ref,
        )


class TestDriverSlots(Fixture):
    """槽位、投影与策略。"""

    def test_slot_count_comes_from_the_definition_table(self):
        """槽数与方块的容器格数同源，不在这里另写一个数字。"""
        self.assertEqual(self.state.slot_count, STORAGE_DRIVER_SLOT_COUNT.value)
        self.assertEqual(len(self.state.slots), STORAGE_DRIVER_SLOT_COUNT.value)

    def test_empty_driver_is_consistent(self):
        self.assertEqual(self.state.occupied_count(), 0)
        self.assertEqual(self.state.disk_count(self.store.medium), 0)
        self.assertEqual(self.service.medium_ids(), ())
        self.assertEqual(self.service.snapshot()["entries"], [])
        self.assertEqual(self.record.config["disk_count"], 0)
        self.assertTrue(self.service.audit()["ok"])

    def test_mount_projects_disk_count_and_notifies_load(self):
        """`disk_count` 是投影，且**先改计数再交易**。"""
        medium, _, _ = self.store.create_medium(TAPE)
        self.loads[:] = []
        self.state.provider.mount(0, medium.medium_id)
        self.assertEqual(self.state.disk_count(self.store.medium), 1)
        self.assertEqual(self.record.config["disk_count"], 1)
        self.assertEqual(self.loads, [self.record.node_key], "挂载后必须通知负载重算")

    def test_unmount_projects_zero_and_keeps_the_medium_intact(self):
        medium = self.new_medium()
        self.seed(STONE, 5)
        self.loads[:] = []
        self.state.provider.unmount(0)
        self.assertEqual(self.record.config["disk_count"], 0)
        self.assertEqual(self.loads, [self.record.node_key])
        # **卸载保留内容**：只解除贡献，不清空介质。
        self.assertEqual(medium.count_of(STONE), 5)

    def test_a_direct_provider_unmount_is_reconciled_on_the_next_sync(self):
        """绕过服务直接改槽位时，索引必须在下次同步收敛，不能一直多算。

        「库存比设备上多」这种不一致只在很久以后表现为凭空多出来的物品，
        所以这里钉住的是**收敛性**：同步一次之后索引必须与权威槽位一致。
        """
        medium = self.new_medium()
        self.seed(STONE, 5)

        # 绕过服务：直接拔掉槽位，服务这时还不知道。
        self.state.provider.unmount(0)
        self.assertIsNotNone(self.service.item_totals.get(STONE), "这之前索引确实还没收敛")

        self.assertTrue(self.service.add_node(self.node), "同步应当发现挂载变了")
        self.assertEqual(self.service.medium_ids(), ())
        self.assertIsNone(self.service.item_totals.get(STONE))
        self.assertEqual(self.service.query(STONE)["stored"], 0)
        self.assertEqual(medium.count_of(STONE), 5, "介质内容仍然保留")
        self.assertTrue(self.service.audit()["ok"])
        self.assertFalse(self.service.add_node(self.node), "收敛之后不该再有变化")

    def test_slot_index_out_of_range_is_refused(self):
        medium, _, _ = self.store.create_medium(TAPE)
        for slot in (-1, STORAGE_DRIVER_SLOT_COUNT, 999):
            ok, reason, _ = self.state.provider.mount(slot, medium.medium_id)
            self.assertFalse(ok, "槽号 %r 不该被接受" % (slot,))
            self.assertEqual(reason, Reason.OUT_OF_RANGE)

    def test_occupied_slot_is_refused(self):
        first = self.new_medium(TAPE, slot=0)
        second, _, _ = self.store.create_medium(FLOPPY)
        ok, reason, _ = self.state.provider.mount(0, second.medium_id)
        self.assertFalse(ok)
        self.assertEqual(reason, Reason.SLOT_OCCUPIED)
        self.assertEqual(self.state.medium_ids(), (first.medium_id,))

    def test_stale_slot_version_is_refused(self):
        """基于旧槽位状态的请求必须整体作废，而不是照着旧状态执行。"""
        self.new_medium(TAPE, slot=0)
        second, _, _ = self.store.create_medium(FLOPPY)
        # 槽 0 现在的版本是 1；请求却声称它当时是 0（空槽）。
        ok, reason, _ = self.state.provider.mount(
            0, second.medium_id, expected_slot_version=0
        )
        self.assertFalse(ok)
        self.assertEqual(reason, Reason.STALE_VERSION)

    def test_the_same_medium_cannot_occupy_two_slots(self):
        medium = self.new_medium()
        ok, reason, _ = self.state.provider.mount(5, medium.medium_id)
        self.assertFalse(ok)
        self.assertEqual(reason, Reason.MEDIUM_DUPLICATE_MOUNT)

    def test_disk_count_scope_is_a_policy_not_a_constant(self):
        """R10：「硬盘个数」的计入口径未定案，两种取法都要能表达。"""
        floppy = self.definitions.get_medium(FLOPPY)
        tape = self.definitions.get_medium(TAPE)
        harddisk = self.definitions.get_medium(HARDDISK)

        counted = DriverPolicy()
        only_disks = DriverPolicy(disk_count_scope=DISK_SCOPE_HARDDISKS_ONLY)
        # 替身口径：装了什么就算几个硬盘。
        self.assertTrue(counted.counts_as_disk(floppy))
        self.assertTrue(counted.counts_as_disk(harddisk))
        # 另一种取法：只有硬盘型号计入，磁带与软盘都不算。
        self.assertFalse(only_disks.counts_as_disk(floppy))
        self.assertFalse(only_disks.counts_as_disk(tape))
        self.assertTrue(only_disks.counts_as_disk(harddisk))
        # 型号查不到时**计入**：信息不全时宁可多算需求。
        self.assertTrue(only_disks.counts_as_disk(None))

    def test_a_client_dictionary_cannot_write_disk_count(self):
        """投影是唯一写入点：命令载荷里带什么都没用。"""
        self.new_medium()
        self.record.config["disk_count"] = 99
        self.assertTrue(self.store.project(self.state))
        self.assertEqual(self.record.config["disk_count"], 1)
        self.assertEqual(self.state.disk_count(self.store.medium), 1)


class TestDriverLifecycle(Fixture):
    """区块卸载/重载与拆除。"""

    def test_chunk_unload_keeps_slots_and_medium_ids(self):
        """**区块重载不得重新分配介质 ID**，也不得让内容看起来消失过。"""
        medium = self.new_medium()
        self.seed(STONE, 3)
        before = self.state.medium_ids()

        self.store.observe(
            LifecycleChange(
                ChangeKind.UNLOADED,
                self.record.node_key,
                DRIVER_DEVICE_ID,
                self.record.ref,
            )
        )
        self.assertFalse(self.state.loaded)
        self.assertEqual(self.state.medium_ids(), before)
        self.assertEqual(self.state.slots[0].medium_id, before[0])
        self.assertEqual(self.store.medium(medium.medium_id).count_of(STONE), 3)

        self.store.observe(
            LifecycleChange(
                ChangeKind.LOADED, self.record.node_key, DRIVER_DEVICE_ID, self.record.ref
            )
        )
        self.assertTrue(self.state.loaded)
        self.assertEqual(self.state.medium_ids(), before, "重载后介质 ID 必须逐项相等")

    def test_resync_after_reload_does_not_double_count(self):
        medium = self.new_medium()
        self.seed(STONE, 3)
        self.service.remove_node(self.node)
        self.assertEqual(self.service.medium_ids(), ())

        self.assertTrue(self.service.add_node(self.node))
        self.assertEqual(self.service.medium_ids(), (medium.medium_id,))
        self.assertEqual(self.service.query(STONE)["stored"], 3, "重挂不得重复计入")
        # 再一次是幂等的：既不改挂载集合，也不重复计数。
        self.assertFalse(self.service.add_node(self.node))
        self.assertEqual(self.service.query(STONE)["stored"], 3)
        self.assertTrue(self.service.audit()["ok"])

    def test_a_slot_move_is_reconciled(self):
        """同盘换槽位是合法移动，重挂后必须指向新槽位。"""
        medium = self.new_medium(slot=0)
        self.state.provider.unmount(0)
        self.state.provider.mount(4, medium.medium_id)
        self.assertTrue(self.service.add_node(self.node))
        self.assertEqual(self.service.mount_record(medium.medium_id)["slot"], 4)
        self.assertFalse(self.service.add_node(self.node), "再同步一次不该有变化")

    def test_removal_orphans_mediums_but_never_deletes_them(self):
        """拆除**不自行决定掉落或保管**：介质留着并标为失去归属。"""
        medium = self.new_medium()
        self.seed(STONE, 3)
        self.store.observe(
            LifecycleChange(
                ChangeKind.REMOVED, self.record.node_key, DRIVER_DEVICE_ID, self.record.ref
            )
        )
        self.assertIsNone(self.store.get_by_ref(self.record.ref))
        self.assertTrue(medium.orphaned)
        self.assertFalse(medium.is_tradeable())
        self.assertIn(medium.medium_id, self.store.orphans())
        # 内容一字未动。
        self.assertEqual(medium.count_of(STONE), 3)
        self.assertIsNotNone(self.store.medium(medium.medium_id))

    def test_a_removed_driver_no_longer_contributes(self):
        self.new_medium()
        self.seed(STONE, 3)
        self.store.observe(
            LifecycleChange(
                ChangeKind.REMOVED, self.record.node_key, DRIVER_DEVICE_ID, self.record.ref
            )
        )
        self.service.remove_node(self.node)
        self.assertIsNone(self.service.item_totals.get(STONE))
        self.assertTrue(self.service.audit()["ok"])

    def test_stop_marks_unloaded_without_touching_content(self):
        medium = self.new_medium()
        self.seed(STONE, 3)
        self.assertEqual(self.store.mark_all_unloaded(), 1)
        self.assertFalse(self.state.loaded)
        self.assertEqual(medium.count_of(STONE), 3)


class TestMediumRules(Fixture):
    """单介质的容量、类型配额与恢复。"""

    def test_unknown_model_is_refused(self):
        medium, reason, _ = self.store.create_medium("minecraft:stone")
        self.assertIsNone(medium)
        self.assertEqual(reason, Reason.NOT_A_MEDIUM)
        self.assertEqual(self.store.mediums(), {})

    def test_a_model_the_driver_does_not_accept_is_refused(self):
        """**定义表里有某型号，不等于这个驱动器接受它。**"""
        store = StorageDriverStore(
            self.definitions,
            self.registry,
            policy=DriverPolicy(accepted_models=(TAPE,)),
            medium_policy=self.medium_policy,
        )
        self.assertIsNotNone(store.create_medium(TAPE)[0])
        medium, reason, _ = store.create_medium(FLOPPY)
        self.assertIsNone(medium)
        self.assertEqual(reason, Reason.NOT_A_MEDIUM)

    def _write(self, medium, resource, count, tick):
        plan = medium.plan(resource, count, DIRECTION_WRITE, tick)
        if plan.ok:
            medium.commit(plan, tick)
        return plan

    def test_type_limit_is_reached_independently_of_capacity(self):
        """类型满但容量远未满时，也必须准确拒绝新增资源。"""
        medium = Medium.create("m", FLOPPY, self.definitions, ample_policy())
        medium.refresh_budget(FAR_TICK)
        spec = self.definitions.get_medium(FLOPPY)
        self.assertEqual(spec.max_types, 32)

        for index in range(spec.max_types):
            plan = self._write(
                medium, ResourceKey.item("minecraft:test_%d" % index), 1, FAR_TICK
            )
            self.assertTrue(plan.ok, plan)
        self.assertEqual(medium.type_count, spec.max_types)
        self.assertGreater(medium.free_micro(), 0, "容量还远没满")

        plan = medium.plan(
            ResourceKey.item("minecraft:overflow"), 1, DIRECTION_WRITE, FAR_TICK
        )
        self.assertFalse(plan.ok)
        self.assertEqual(plan.reason, Reason.TYPE_LIMIT_REACHED)
        medium.assert_consistent()

    def test_capacity_is_reached_independently_of_types(self):
        """容量满但类型远未满时同样要拒绝，且原因与类型满分开。"""
        medium = Medium.create("m", TAPE, self.definitions, ample_policy())
        medium.refresh_budget(FAR_TICK)
        room = medium.free_micro() // STONE.micro_units(1)
        plan = self._write(medium, STONE, room, FAR_TICK)
        self.assertEqual(plan.accepted, room, "容量是这里唯一的限制")
        self.assertEqual(medium.free_micro(), 0)
        self.assertEqual(medium.type_count, 1, "容量用完了，类型只占了一个")

        plan = medium.plan(STONE, 1, DIRECTION_WRITE, FAR_TICK)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.reason, Reason.NO_CAPACITY)
        medium.assert_consistent()

    def test_emptying_a_key_releases_its_type_slot(self):
        medium = Medium.create("m", TAPE, self.definitions, ample_policy())
        medium.refresh_budget(FAR_TICK)
        self._write(medium, STONE, 1, FAR_TICK)
        self.assertEqual(medium.type_count, 1)
        plan = medium.plan(STONE, 1, DIRECTION_READ, FAR_TICK)
        medium.commit(plan, FAR_TICK)
        self.assertEqual(medium.type_count, 0, "取空最后一格要释放类型占用")
        self.assertEqual(medium.count_of(STONE), 0)
        self.assertEqual(medium.keys(), ())
        medium.assert_consistent()

    def test_fluid_does_not_take_a_type_slot_by_default(self):
        """R07 未定案：替身取「流体不占类型配额」，但这是显式策略。"""
        medium = Medium.create("m", TAPE, self.definitions, ample_policy())
        self._write(medium, WATER, 25, 10)
        self.assertEqual(medium.type_count, 0)
        self.assertEqual(medium.count_of(WATER), 25, "25mB 不该被截断")
        self.assertEqual(medium.used_micro, 25, "1mB 记 1 微单位物")

        strict = Medium.create(
            "m2", TAPE, self.definitions, ample_policy(counts_fluid_as_type=True)
        )
        self._write(strict, WATER, 25, 10)
        self.assertEqual(strict.type_count, 1)
        self.assertEqual(strict.used_micro, 25)

    def test_large_capacity_stays_exact(self):
        """大容量数量范围不得溢出或丢精度。"""
        big = Medium.create("m", BIG_TAPE, self.definitions, ample_policy())
        spec = self.definitions.get_medium(BIG_TAPE)
        self.assertEqual(big.capacity_micro(), spec.capacity_units * 50)
        self.assertEqual(spec.capacity_units, 2 ** 30)

    def test_unreadable_payloads_are_preserved_not_emptied(self):
        """未知版本、缺型号：一律原样保留并禁止交易。"""
        good = Medium.create("m", TAPE, self.definitions, self.medium_policy)
        payload = good.to_dict()

        broken = dict(payload)
        broken["schema_version"] = 999
        restored = Medium.from_dict(broken, self.definitions, self.medium_policy)
        self.assertEqual(restored.recovery, MediumRecovery.UNKNOWN_SCHEMA)
        self.assertFalse(restored.is_tradeable())
        self.assertEqual(restored.raw_payload, broken, "原始载荷必须保留")

        missing = dict(payload)
        missing["model_id"] = "endernet:no_such_model"
        restored = Medium.from_dict(missing, self.definitions, self.medium_policy)
        self.assertEqual(restored.recovery, MediumRecovery.MISSING_MODEL)
        self.assertFalse(restored.is_tradeable())
        self.assertEqual(restored.raw_payload, missing)

        # 读不懂的盘**不接受任何交易**，但也不报成「就是空的」。
        plan = restored.plan(STONE, 1, DIRECTION_WRITE, 100)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.reason, Reason.MEDIUM_UNRECOVERED)

    def test_a_payload_without_a_schema_version_is_not_assumed_current(self):
        """缺版本字段 = 认不出来，不得当成 v1 直接读。"""
        payload = Medium.create("m", TAPE, self.definitions, self.medium_policy).to_dict()
        payload.pop("schema_version")
        restored = Medium.from_dict(payload, self.definitions, self.medium_policy)
        self.assertEqual(restored.recovery, MediumRecovery.UNKNOWN_SCHEMA)
        self.assertEqual(restored.raw_payload, payload)

    def test_malformed_entries_are_corrupt_not_empty(self):
        """载荷结构坏了就判损坏，**绝不当空盘读成 0 库存**。"""
        payload = Medium.create("m", TAPE, self.definitions, self.medium_policy).to_dict()
        payload["entries"] = [{"key": {"kind": "nonsense"}, "count": 3}]
        restored = Medium.from_dict(payload, self.definitions, self.medium_policy)
        self.assertEqual(restored.recovery, MediumRecovery.CORRUPT)
        self.assertFalse(restored.is_tradeable())
        self.assertEqual(restored.raw_payload, payload)

    def test_round_trip_preserves_contents(self):
        medium = Medium.create("m", TAPE, self.definitions, ample_policy())
        for resource, count in ((STONE, 7), (DIRT, 3)):
            self._write(medium, resource, count, 10)
        restored = Medium.from_dict(
            medium.to_dict(), self.definitions, self.medium_policy
        )
        self.assertEqual(restored.recovery, MediumRecovery.OK)
        self.assertEqual(restored.stored_view(), medium.stored_view())
        self.assertEqual(restored.used_micro, medium.used_micro)
        self.assertEqual(restored.type_count, medium.type_count)
        restored.assert_consistent()

    def test_an_orphaned_medium_is_preserved_but_not_tradeable(self):
        medium = Medium.create("m", TAPE, self.definitions, ample_policy())
        self._write(medium, STONE, 2, 10)
        medium.mark_orphaned("驱动器已拆除")
        self.assertFalse(medium.is_tradeable())
        self.assertEqual(medium.count_of(STONE), 2)
        plan = medium.plan(STONE, 1, DIRECTION_READ, 10)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.reason, Reason.MEDIUM_ORPHANED)


class TestBudget(Fixture):
    """共享读写额度、余数保留与时钟回退。"""

    def test_quota_is_shared_by_every_caller(self):
        """多个接口从同一块盘取货时，谁也拿不到第二份额度（05-03）。"""
        self.new_medium(TAPE)
        self.seed(STONE, 12)
        tick = 100

        first = self.service.commit_extract("e1", STONE, 3, now_tick=tick)
        second = self.service.commit_extract("e2", STONE, 3, now_tick=tick)
        third = self.service.commit_extract("e3", STONE, 3, now_tick=tick)
        self.assertEqual(first.moved, 3)
        self.assertEqual(second.moved, 1, "同一帧里剩下的额度只有 1 件")
        self.assertEqual(third.moved, 0)
        self.assertEqual(third.reason, Reason.RATE_LIMITED)
        self.assertEqual(self.service.query(STONE)["stored"], 8, "只该扣走实际取到的 4 件")

    def test_a_later_tick_restores_the_quota(self):
        self.new_medium(TAPE)
        self.seed(STONE, 12)
        self.assertEqual(
            self.service.commit_extract("e1", STONE, 4, now_tick=100).moved, 4
        )
        self.assertEqual(
            self.service.commit_extract("e2", STONE, 4, now_tick=100).moved, 0
        )
        self.assertEqual(
            self.service.commit_extract("e3", STONE, 4, now_tick=101).moved, 4
        )

    def test_the_bucket_starts_empty(self):
        """验收要求「在初始额度为零的假时钟中」验证累积。"""
        self.new_medium(TAPE)
        receipt = self.service.commit_insert("op1", STONE, 1, now_tick=0)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(receipt.reason, Reason.RATE_LIMITED)

    def test_sub_unit_remainders_accumulate(self):
        """不足一件物品的余数必须跨帧累积，否则低速介质永远取不出东西。"""
        medium = Medium.create(
            "m", TAPE, self.definitions, MediumPolicy(frames_per_second=3)
        )
        # 4 单位物/s = 200 微单位/s；3 帧/秒 => 每帧 66 微单位，一件需要 50。
        self.assertEqual(medium.read_bucket.peek(1, 3), 66)
        self.assertEqual(medium.read_bucket.peek(2, 3), 133)
        self.assertEqual(medium.read_bucket.peek(3, 3), 200)
        for tick in (1, 2, 3):
            plan = medium.plan(STONE, 1, DIRECTION_READ, tick)
            del plan
        self.assertEqual(medium.read_bucket.balance_micro, 0, "预演不得消耗额度")

    def test_the_bucket_is_bounded(self):
        """有限桶：空转再久也只能攒一个桶的量。"""
        medium = Medium.create(
            "m", TAPE, self.definitions, MediumPolicy(frames_per_second=1)
        )
        self.assertEqual(medium.read_bucket.capacity_micro, 200)
        self.assertEqual(medium.read_bucket.peek(10 ** 6, 1), 200)

    def test_clock_rollback_grants_nothing(self):
        """重进世界时钟归零**不是**刷额度路径。"""
        medium = Medium.create(
            "m", TAPE, self.definitions, MediumPolicy(frames_per_second=1)
        )
        medium.write_bucket.refill(100, 1)
        before = medium.write_bucket.balance_micro
        self.assertGreater(before, 0)
        self.assertEqual(medium.write_bucket.peek(0, 1), before, "回退不得发放额度")
        medium.refresh_budget(0)
        self.assertEqual(medium.write_bucket.balance_micro, before)

    def test_shared_budget_is_an_explicit_policy(self):
        """R07：读写是否共桶是两种取法，都要能表达。"""
        separate = Medium.create("m", TAPE, self.definitions, MediumPolicy(frames_per_second=1))
        self.assertIsNot(separate.read_bucket, separate.write_bucket)
        shared = Medium.create(
            "m2",
            TAPE,
            self.definitions,
            MediumPolicy(frames_per_second=1, shares_read_write_budget=True),
        )
        self.assertIs(shared.read_bucket, shared.write_bucket)

    def test_simulation_never_consumes_stock_or_quota(self):
        medium = self.new_medium(TAPE)
        self.seed(STONE, 5)
        before_balance = medium.write_bucket.balance_micro
        before_revision = medium.revision

        for _ in range(3):
            self.service.simulate_insert(STONE, 2, now_tick=100)
            self.service.simulate_extract(STONE, 2, now_tick=100)

        self.assertEqual(self.service.query(STONE)["stored"], 5)
        self.assertEqual(medium.write_bucket.balance_micro, before_balance)
        self.assertEqual(medium.revision, before_revision)

    def test_simulate_reports_what_a_commit_would_move(self):
        self.new_medium(TAPE)
        self.seed(STONE, 6)
        predicted = self.service.simulate_extract(STONE, 2, now_tick=100)
        self.assertEqual(predicted["accepted"], 2)
        actual = self.service.commit_extract("e1", STONE, 2, now_tick=100)
        self.assertEqual(actual.moved, predicted["accepted"])


class TestTransfers(Fixture):
    """提交、去重与守恒。"""

    def test_refused_requests_do_not_consume_stock(self):
        self.new_medium(TAPE)
        self.seed(STONE, 4)
        receipt = self.service.commit_extract("e1", STONE, 99, now_tick=100)
        self.assertEqual(receipt.moved, 4, "只有库存里真的有的那份")
        self.assertEqual(self.service.commit_extract("e2", STONE, 99, now_tick=101).moved, 0)
        self.assertEqual(self.service.query(STONE)["stored"] if self.service.query(STONE)["stored"] else 0, 0)

    def test_a_repeated_op_returns_the_original_receipt(self):
        """**重发已确认的操作只返回原结果**，不再次扣物品。"""
        self.new_medium(TAPE)
        self.seed(STONE, 8)
        first = self.service.commit_extract("e1", STONE, 2, now_tick=100)
        self.assertEqual(first.moved, 2)
        self.assertEqual(self.service.query(STONE)["stored"], 6)

        again = self.service.commit_extract("e1", STONE, 2, now_tick=100)
        self.assertEqual(again.moved, 2)
        self.assertEqual(again.status, first.status)
        self.assertEqual(self.service.query(STONE)["stored"], 6, "不得二次扣减")

    def test_a_partial_transfer_reports_the_actual_amount(self):
        """额度不足时返回部分成功，`moved` 是核对后的实际量。"""
        self.new_medium(TAPE)
        receipt = self.service.commit_insert("op1", STONE, 100, now_tick=10)
        self.assertEqual(receipt.status, TransferStatus.PARTIAL)
        self.assertEqual(receipt.moved, TAPE_QUOTA)
        self.assertEqual(receipt.requested, 100)
        self.assertEqual(receipt.remainder, 100 - TAPE_QUOTA)

    def test_a_negative_count_is_refused(self):
        self.new_medium(TAPE)
        with self.assertRaises(ValueError):
            self.service.commit_insert("op", STONE, -1, now_tick=10)

    def test_reservations_hide_stock_from_ordinary_extraction(self):
        """两个订单不能预留同一份剩余材料，普通取货也看不到他人预留物品。"""
        self.new_medium(TAPE)
        self.seed(STONE, 8)
        reservation = self.service.reserve("order1", STONE, 8, tick=100)
        self.assertIsNotNone(reservation)
        self.assertIsNone(self.service.reserve("order2", STONE, 1, tick=100))
        self.assertEqual(self.service.query(STONE)["available"], 0)
        self.assertEqual(self.service.commit_extract("e1", STONE, 1, now_tick=200).moved, 0)

        self.assertEqual(self.service.release(reservation.reservation_id), 8)
        self.assertEqual(self.service.query(STONE)["available"], 8)
        self.assertEqual(self.service.commit_extract("e2", STONE, 1, now_tick=200).moved, 1)

    def test_a_partial_release_keeps_the_rest(self):
        self.new_medium(TAPE)
        self.seed(STONE, 8)
        reservation = self.service.reserve("order1", STONE, 8, tick=100)
        self.assertEqual(self.service.release(reservation.reservation_id, 3), 3)
        self.assertEqual(self.service.query(STONE)["reserved"], 5)
        self.assertEqual(self.service.query(STONE)["available"], 3)
        self.assertTrue(self.service.audit()["ok"])

    def test_unmounting_a_medium_revokes_its_reservations(self):
        """拔盘引起预留失效时撤销有效性并报出受影响的拥有者。"""
        self.new_medium(TAPE)
        self.seed(STONE, 8)
        self.service.reserve("order1", STONE, 8, tick=100)
        ok, _, detail = self.service.unmount_medium(self.service.medium_ids()[0])
        self.assertTrue(ok)
        self.assertEqual(detail["revoked_owners"], ("order1",))
        self.assertEqual(self.service.reservations_of("order1"), ())
        self.assertEqual(self.service.query(STONE)["reserved"], 0)
        self.assertTrue(self.service.audit()["ok"])

    def test_two_orders_cannot_share_the_same_remainder(self):
        self.new_medium(TAPE)
        self.seed(STONE, 8)
        first = self.service.reserve("order1", STONE, 6, tick=100)
        second = self.service.reserve("order2", STONE, 2, tick=100)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second, "剩下的 2 件可以给第二个订单")
        self.assertIsNone(self.service.reserve("order3", STONE, 1, tick=100))
        self.assertEqual(self.service.query(STONE)["available"], 0)
        self.assertEqual(first.mediums(), second.mediums())

    def test_audit_stays_consistent_across_a_scripted_run(self):
        """独立求和守恒：一段脚本化的存取/拒绝/挂卸之后逐项核对。"""
        first = self.new_medium(TAPE, slot=0)
        second, _, _ = self.store.create_medium(FLOPPY)
        self.state.provider.mount(1, second.medium_id)
        self.service.add_node(self.node)

        tick = 100
        for index in range(6):
            # 每一帧的写入额度正好 4 件，因此两种资源各存 2 件刚好放得下。
            self.service.commit_insert("in_%d" % index, STONE, 2, now_tick=tick)
            self.service.commit_insert("in_d_%d" % index, DIRT, 2, now_tick=tick)
            tick += 1
            self.service.commit_extract("out_%d" % index, STONE, 1, now_tick=tick)
            # 库里没有的资源：拒绝，且不消耗任何额度。
            self.service.commit_extract("miss_%d" % index, WATER, 99, now_tick=tick)
            self.assertTrue(
                self.service.audit()["ok"], self.service.audit()["problems"]
            )

        report = self.service.audit()
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["keys"], 2)
        self.assertEqual(self.service.query(STONE)["stored"], 6)
        self.assertEqual(self.service.query(DIRT)["stored"], 12)
        self.assertEqual(first.count_of(STONE) + second.count_of(STONE), 6)

        # 重建索引之后仍然一致——「增量更新」与「重建」不能给出不同结论。
        self.service.rebuild_index()
        self.assertTrue(self.service.audit()["ok"], self.service.audit()["problems"])
        self.assertEqual(self.service.query(STONE)["stored"], 6)

    def test_the_snapshot_does_not_leak_mutable_state(self):
        self.new_medium()
        self.seed(STONE, 1)
        snapshot = self.service.snapshot()
        snapshot["entries"][0]["stored"] = 999
        snapshot["mediums"][0]["used_micro"] = 999
        self.assertEqual(self.service.query(STONE)["stored"], 1)
        view = self.service.medium_view(self.service.medium_ids()[0])
        view["used_micro"] = 999
        self.assertEqual(self.service.query(STONE)["stored"], 1)

    def test_dirty_keys_are_deduplicated_and_advance_the_publish_version(self):
        self.new_medium()
        before = self.service.publish_version
        self.seed(STONE, 1, tick=10)
        self.service.commit_insert("op2", STONE, 1, now_tick=11)
        self.service.commit_insert("op3", DIRT, 1, now_tick=12)
        keys = self.service.take_dirty_keys()
        self.assertEqual(len(keys), 2, "同一个键在窗口内只出现一次")
        self.assertEqual(self.service.publish_version, before + 1)
        self.assertEqual(self.service.take_dirty_keys(), (), "取空之后不推进版本")
        self.assertEqual(self.service.publish_version, before + 1)


class TestItemViewMarking(Fixture):
    """脏标记只收物品：流体不得推进物品视图版本。

    `dirty_items` 的契约是「待发布的**物品**键」，而介质里同时装物品与流体
    （`stored_view()` 返回全部条目）。若不在这里挡一道，一次纯流体提交就会
    推进 `publish_version`，客户端于是收到一批它根本不显示的键。

    这**不是**「库存索引不含流体」——正相反，索引必须含流体，额度、预留与
    `audit()` 都建立在全量之上。两类断言因此必须分开写。
    """

    def test_fluid_only_commit_does_not_advance_publish_version(self):
        self.new_medium()
        tick = self.seed(STONE, 1, tick=10)
        self.service.take_dirty_keys()  # 清空并推进到基线
        version = self.service.publish_version
        index_version = self.service.index_version

        receipt = self.service.commit_insert("fluid:1", WATER, 2, now_tick=tick)
        self.assertEqual(receipt.moved, 2, receipt)

        self.assertEqual(self.service.query(WATER)["stored"], 2, "索引仍然记流体")
        self.assertGreater(
            self.service.index_version,
            index_version,
            "流体提交仍是存储提交，交易验证必须看得见它——两个版本分工不同",
        )
        self.assertFalse(self.service.has_dirty_items())
        self.assertEqual(self.service.take_dirty_keys(), ())
        self.assertEqual(self.service.publish_version, version, "物品视图版本不动")

    def test_item_commit_advances_publish_version_only_when_taken(self):
        """推进属于发布窗口，不属于提交本身。"""
        self.new_medium()
        before = self.service.publish_version
        self.seed(STONE, 1, tick=10)
        self.assertTrue(self.service.has_dirty_items())
        self.assertEqual(
            self.service.publish_version, before, "没取走就不推进"
        )
        self.service.take_dirty_keys()
        self.assertEqual(self.service.publish_version, before + 1)

    def test_iter_item_records_matches_keys_order_and_excludes_fluids(self):
        self.new_medium()
        tick = self.seed(STONE, 1, tick=10)
        tick = self.seed(DIRT, 1, tick=tick)
        self.service.commit_insert("fluid:1", WATER, 2, now_tick=tick)

        records = self.service.keys()
        self.assertIn(WATER, records, "流体确实在索引里，只是不进物品视图")

        view_keys = [key for key, _, _ in self.service.iter_item_records()]
        self.assertEqual(
            view_keys,
            [key for key in records if key.is_item()],
            "与 keys() 的相对顺序一致",
        )
        self.assertEqual(view_keys, [DIRT, STONE], "canonical 序（dirt 在 stone 前）")
        for _, stored, reserved in self.service.iter_item_records():
            self.assertEqual((stored, reserved), (1, 0))

    def test_iter_item_records_is_immutable_and_does_not_leak_the_index(self):
        """窗口冻结的快照必须是不可变的——发布期间还会有提交落进来。"""
        self.new_medium()
        self.seed(STONE, 1, tick=10)
        frozen = self.service.iter_item_records()
        self.seed(STONE, 1, tick=20)
        self.assertEqual(frozen[0][1], 1, "冻结之后索引变了，快照不该跟着变")
        self.assertEqual(self.service.query(STONE)["stored"], 2)


class TestProviderContract(Fixture):
    """能力对象与命令入口。"""

    def test_the_capability_object_really_exists(self):
        """架构 §4：不能只返回标签，要有一个真的实现对象。"""
        self.assertTrue(self.state.capabilities.has(STORAGE_PROVIDER))
        provider = self.state.capabilities.get(STORAGE_PROVIDER)
        for name in ("list_mediums", "read", "write", "save_node_data"):
            self.assertTrue(callable(getattr(provider, name)), name)

    def test_provider_read_and_write_move_real_stock(self):
        medium = self.new_medium(TAPE)
        provider = self.state.provider
        written = provider.write(
            medium.medium_id, STONE, 2, TransferBudget(10, "w1")
        )
        self.assertEqual(written.moved, 2)
        self.assertEqual(medium.count_of(STONE), 2)
        read = provider.read(medium.medium_id, STONE, 1, TransferBudget(20, "r1"))
        self.assertEqual(read.moved, 1)
        self.assertEqual(medium.count_of(STONE), 1)
        self.assertEqual(provider.list_mediums(), [medium.medium_id])
        self.assertTrue(provider.save_node_data()["slots"])

    def test_provider_refuses_a_medium_that_is_not_mounted(self):
        medium, _, _ = self.store.create_medium(TAPE)
        receipt = self.state.provider.write(
            medium.medium_id, STONE, 1, TransferBudget(10, "w")
        )
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(receipt.reason, Reason.MEDIUM_NOT_MOUNTED)

    def test_capabilities_declare_what_the_driver_can_do(self):
        caps = self.state.provider.capabilities()
        self.assertTrue(caps.can_read)
        self.assertTrue(caps.can_insert)
        self.assertTrue(caps.can_simulate)
        self.assertEqual(caps.max_slots, STORAGE_DRIVER_SLOT_COUNT.value)

    def test_unknown_action_is_refused_not_faked(self):
        """架构 §7：尚不支持的命令明确拒绝，不能返回假成功。"""
        result = self.service.handle(None, self.command({"action": "teleport"}))
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertIn(Reason.UNSUPPORTED, result.reason_codes())

    def test_missing_action_is_refused(self):
        result = self.service.handle(None, self.command({}))
        self.assertIn(Reason.BAD_PARAMETER, result.reason_codes())

    def test_a_bad_action_payload_is_refused(self):
        result = self.service.handle(
            None, self.command({"action": "mount", "slot": "zero", "model_id": TAPE})
        )
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertIn(Reason.BAD_PARAMETER, result.reason_codes())

    def test_bare_construction_is_not_ready_but_not_broken(self):
        """裸构造是合法的（`default_stand_ins()` 会这么造），此时给未就绪。"""
        service = StorageService()
        self.assertFalse(service.is_ready())
        result = service.handle(None, self.command({"action": "snapshot"}))
        self.assertIn(Reason.SERVICE_NOT_READY, result.reason_codes())

    def test_mount_and_unmount_commands_round_trip(self):
        result = self.service.handle(
            None, self.command({"action": "mount", "slot": 2, "model_id": TAPE})
        )
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        medium_id = result.payload["medium_id"]
        self.assertEqual(result.payload["disk_count"], 1)
        self.assertEqual(self.service.medium_ids(), (medium_id,))
        self.assertEqual(self.record.config["disk_count"], 1)

        result = self.service.handle(None, self.command({"action": "unmount", "slot": 2}))
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        self.assertEqual(self.service.medium_ids(), ())
        self.assertEqual(self.record.config["disk_count"], 0)

    def test_mount_refuses_an_unregistered_model(self):
        result = self.service.handle(
            None,
            self.command({"action": "mount", "slot": 0, "model_id": "minecraft:stone"}),
        )
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertIn(Reason.NOT_A_MEDIUM, result.reason_codes())
        self.assertEqual(self.record.config["disk_count"], 0, "拒绝不得改计数")

    def test_a_duplicate_network_mount_is_refused(self):
        """同一块盘在网络上只能挂一处；第二条路径必须被拦下。"""
        medium, _, _ = self.store.create_medium(TAPE)
        self.state.provider.mount(0, medium.medium_id)
        self.assertTrue(
            self.service.mount_medium(
                medium.medium_id, self.record.node_key, 0, device_ref=self.record.ref
            )[0]
        )
        other = self.place(5)
        ok, reason, _ = self.service.mount_medium(
            medium.medium_id, other.node_key, 1, device_ref=other.ref
        )
        self.assertFalse(ok)
        self.assertEqual(reason, Reason.MEDIUM_DUPLICATE_MOUNT)
        self.assertEqual(len(self.service.medium_ids()), 1, "不得重复计入")

    def test_driver_status_reports_slots_and_recovery(self):
        self.new_medium(TAPE, slot=1)
        result = self.service.handle(None, self.command({"action": "driver_status"}))
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        slots = result.payload["slots"]
        self.assertEqual(len(slots), STORAGE_DRIVER_SLOT_COUNT.value)
        self.assertEqual(slots[0]["state"], "empty")
        self.assertEqual(slots[1]["state"], "occupied")
        self.assertEqual(result.payload["recovery"], "ok")

    def test_commands_on_an_unloaded_driver_are_blocked(self):
        self.store.mark_loaded(self.record.node_key, False)
        result = self.service.handle(None, self.command({"action": "driver_status"}))
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn(Reason.DRIVER_UNLOADED, result.reason_codes())

    def test_the_service_has_nothing_node_local_to_persist(self):
        """介质权威归驱动器状态集合，因此本服务不产出第二份节点存档。"""
        self.assertIsNone(self.service.save_node_data(self.node))


class TestPersistenceStructures(Fixture):
    """12-03 需要的可序列化结构：导得出、读得回、内容不丢。

    本轮**没有**接真实仓储后端（那是任务 12），因此这里验证的是
    「结构本身可往返」这一件事，不是「存档已经接通」。
    """

    def test_a_medium_survives_a_save_and_restore_round_trip(self):
        medium = self.new_medium()
        self.seed(STONE, 4)

        payload = self.store.save_all()
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["mediums"]), 1)
        self.assertEqual(len(payload["drivers"]), 1)

        restored = StorageDriverStore(
            self.definitions,
            self.registry,
            policy=self.driver_policy,
            medium_policy=self.medium_policy,
        )
        stats = restored.restore(payload)
        self.assertEqual(stats, {"drivers": 1, "mediums": 1})

        copy = restored.medium(medium.medium_id)
        self.assertIsNotNone(copy, "介质 ID 必须原样恢复，不得重新分配")
        self.assertEqual(copy.count_of(STONE), medium.count_of(STONE))
        self.assertEqual(copy.used_micro, medium.used_micro)
        self.assertEqual(copy.type_count, medium.type_count)
        copy.assert_consistent()

    def test_a_restored_driver_keeps_its_slots(self):
        medium = self.new_medium(slot=3)
        payload = self.store.save_all()

        restored = StorageDriverStore(
            self.definitions,
            self.registry,
            policy=self.driver_policy,
            medium_policy=self.medium_policy,
        )
        restored.restore(payload)
        state = restored.get_by_ref(self.record.ref)
        self.assertIsNotNone(state)
        self.assertEqual(state.slot_of(medium.medium_id), 3)
        self.assertEqual(
            state.assignment(3).slot_version, self.state.assignment(3).slot_version
        )

    def test_restore_refuses_to_reuse_a_medium_id(self):
        """重复 ID 不许悄悄覆盖：那会把两块盘的内容并成一块。"""
        medium = self.new_medium()
        payload = self.store.save_all()
        with self.assertRaises(ValueError):
            self.store.restore(payload)
        self.assertEqual(self.store.medium(medium.medium_id).count_of(STONE), 0)

    def test_save_all_is_pure_data(self):
        """导出的必须是纯数据：改它不能反过来影响权威状态。"""
        medium = self.new_medium()
        self.seed(STONE, 2)
        payload = self.store.save_all()
        payload["mediums"][0]["entries"] = []
        payload["drivers"][0]["slots"] = []
        self.assertEqual(self.store.medium(medium.medium_id).count_of(STONE), 2)
        self.assertEqual(self.state.slot_of(medium.medium_id), 0)


class TestPolicies(Fixture):
    """未定案规则必须可查，且不得被静默定案。"""

    def test_unresolved_rules_are_reported_with_their_ids(self):
        for policy in (self.driver_policy, self.medium_policy):
            report = policy.unresolved_report()
            self.assertTrue(report)
            for item in report:
                self.assertTrue(item["rule"], item)
                self.assertIn("value", item)

    def test_settled_policies_raise(self):
        """`assert_settled()` 只给正式验收用；这里验证它确实会拦。"""
        with self.assertRaises(AssertionError):
            self.driver_policy.assert_settled()
        with self.assertRaises(AssertionError):
            self.medium_policy.assert_settled()

    def test_the_definition_table_has_no_silently_settled_value(self):
        self.definitions.assert_no_silent_unresolved()

    def test_slot_count_is_marked_unresolved_in_the_definition_table(self):
        """槽数是 R10 未定案：它以测试替身的形式存在，且能被诊断查到。"""
        report = dict(self.definitions.unresolved_report())
        self.assertIn("config.storage_driver.slot_count", report)
        entry = report["config.storage_driver.slot_count"]
        self.assertEqual(entry.unresolved, "R10")
        self.assertTrue(entry.is_test_value)

    def test_no_production_code_calls_assert_settled(self):
        """开发与离线测试都不应调用它——静态钉住，而不是靠自觉。"""
        offenders = []
        for path in testsupport.source_files():
            with io.open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
            if ".assert_settled()" in source:
                offenders.append(os.path.relpath(path, testsupport.REPO_ROOT))
        self.assertEqual(offenders, [], "assert_settled() 不应出现在业务代码里")


class TestMediumCapacityDisplay(unittest.TestCase):
    """介质物品详情要显示的两个数（`plans/items.md` / `medium_lore.md`）。

    这一层**只算与渲染**：写回时机、在既有 Lore 里认领自己那两行、身份规则
    （介质实例用持久稳定 ID、仅排除这两行动态容量行）都属 12-02，
    这里不碰。诊断里现在就能查到同一份数值。

    **判据一律用独立求和**（`Medium.recompute_usage`），不把被测的
    `type_count` 再算一遍——那只会一起错。
    """

    def setUp(self):
        self.definitions = DefinitionSet()
        self.policy = MediumPolicy(
            frames_per_second=1, bucket_seconds=10 ** 6
        )
        self.now = 0
        self.medium = Medium.create("medium:1", TAPE, self.definitions, self.policy)

    def store(self, key, count):
        """存进去。额度桶给得足够大，这里考的是容量不是限速。"""
        plan = self.medium.plan(key, count, DIRECTION_WRITE, FAR_TICK)
        self.assertEqual(plan.accepted, count, plan.reason)
        self.medium.commit(plan, FAR_TICK)

    def independent(self):
        # type: () -> tuple
        """`(used_micro, type_count)` 的独立全量求和。"""
        return self.medium.recompute_usage()

    def test_an_empty_tape_matches_the_spec_example(self):
        """`items.md` 逐字给了这个例子：空的小型单面磁带显示
        `剩余种类数：12`、`剩余总数量：1728 单位物`。"""
        self.assertEqual(
            self.medium.capacity_lines(),
            ("剩余种类数：12", "剩余总数量：1728 单位物"),
        )

    def test_remaining_types_follows_the_independent_sum(self):
        self.store(STONE, 1)
        self.store(DIRT, 1)
        used_micro, used_types = self.independent()
        self.assertEqual(self.medium.remaining_types(),
                         self.medium.max_types() - used_types)
        self.assertEqual(self.medium.remaining_capacity_micro(),
                         self.medium.capacity_micro() - used_micro)

    def test_storing_consumes_both_counts(self):
        self.store(STONE, 1)
        self.assertEqual(self.medium.remaining_types(), 11)
        self.assertEqual(self.medium.remaining_capacity_units(), 1727)

    def test_the_same_type_again_does_not_consume_a_type_slot(self):
        """同种物品再存一次只吃容量，不吃种类数。"""
        self.store(STONE, 1)
        self.store(STONE, 3)
        self.assertEqual(self.medium.remaining_types(), 11)
        self.assertEqual(self.medium.remaining_capacity_units(), 1724)

    def test_a_full_type_list_still_accepts_existing_types(self):
        """**剩余种类数为 0 不等于写不进去。**

        把类型配额用满之后，已存在的种类仍可继续追加数量。
        把它显示成「已满」会误导玩家——所以这里把这条钉住。
        """
        for index in range(self.medium.max_types()):
            self.store(ResourceKey.item("minecraft:filler_%d" % (index,)), 1)
        self.assertEqual(self.medium.remaining_types(), 0)
        before = self.medium.remaining_capacity_units()
        self.store(ResourceKey.item("minecraft:filler_0"), 1)
        self.assertEqual(self.medium.remaining_capacity_units(), before - 1)
        self.assertEqual(self.medium.remaining_types(), 0)

    def test_reserving_does_not_change_either_number(self):
        """**预留仍占容量**，预留/释放本身不改这两个数——预留不是取出。

        这一条由 `Medium` 的层负责：预留台账在存储服务那边，
        介质只看实际条目。因此这里存进去之后两个数就该稳定。
        """
        self.store(STONE, 5)
        lines = self.medium.capacity_lines()
        view = self.medium.capacity_view()
        # 再读一次：读取本身绝不能有副作用。
        self.assertEqual(self.medium.capacity_lines(), lines)
        self.assertEqual(self.medium.capacity_view(), view)

    def test_a_fluid_remnant_never_rounds_up(self):
        """**不向上取整夸大容量。**

        1 mB = 1 微单位物，因此含流体时剩余量往往不是 50 的整数倍。
        换算成「单位物」时**向下取整**：向上取整会让一块只剩 3 mB 的盘
        显示成「还剩 1 单位物」，而那一单位物根本放不进去。
        权威刻度是微单位，展示格式由 R07 / 12-02 定。
        """
        self.store(WATER, 3)
        remaining_micro = self.medium.remaining_capacity_micro()
        self.assertEqual(remaining_micro % 50, 47, "3 mB 吃掉 3 个微单位")
        self.assertEqual(
            self.medium.remaining_capacity_units(), remaining_micro // 50
        )
        self.assertLess(self.medium.remaining_capacity_units() * 50,
                        remaining_micro + 50)
        self.assertIn("剩余总数量：%d 单位物" % (remaining_micro // 50,),
                      self.medium.capacity_lines())

    def test_the_view_is_plain_data(self):
        """诊断、界面与将来的物品 Lore 共用这一份——它必须能直接序列化。"""
        self.store(STONE, 2)
        view = self.medium.capacity_view()
        self.assertEqual(sorted(view["lines"]), sorted(self.medium.capacity_lines()))
        text = json.dumps(view, ensure_ascii=False, sort_keys=True)
        self.assertIn("remaining_types", text)

    def test_it_reports_the_medium_not_the_network(self):
        """**用介质自身的权威数据，不用整个网络的汇总值。**

        汇总里含别人的盘，拿它算会显示出一个凭空多出来的容量。
        这里把同一块盘的内容改掉，读数跟着变；而本用例从不碰任何服务。
        """
        before = self.medium.remaining_capacity_units()
        self.store(STONE, 10)
        self.assertEqual(self.medium.remaining_capacity_units(), before - 10)


class TestAssembly(unittest.TestCase):
    """接进启动层之后的行为（不碰引擎）。"""

    def setUp(self):
        from endernet.server.bootstrap import ServerBootstrap

        self.server = ServerBootstrap()
        self.server.start(bind_engine=False)

    def build_network(self):
        self.server.push(NormalizedEvent.place(key(0), "endernet:network_baseband_8"))
        self.server.push(NormalizedEvent.place(key(1), "endernet:network_baseband_8"))
        self.server.push(
            NormalizedEvent.place(key(1, 65), "endernet:tiny_network_endpoint")
        )
        self.server.push(NormalizedEvent.place(key(0, 65), DRIVER_DEVICE_ID))
        return self.server.tick()

    def test_no_service_slot_is_a_stand_in_any_more(self):
        """任务 09 之后四个槽位都是正式实现，装配表里一个替身都不剩。

        **「没有替身」不等于「能玩」**：这一条只断言槽位。能玩与否仍然卡在
        能源判定、带宽判定与回流入口（任务 10）那几处，各自的诊断里都写着。
        """
        report = self.server.diagnostics()
        self.assertEqual(sorted(report["assembly"]["stand_ins"]), [])
        self.assertEqual(
            report["assembly"]["slots"], ["energy", "factory", "scheduler", "storage"]
        )

    def storage_command(self, request_id="r1"):
        # type: (str) -> DeviceCommand
        ref = self.server.registry.get(key(0, 65)).ref
        return DeviceCommand(
            request_id,
            CommandKind.STORAGE,
            CommandSource("p1", ref, 0),
            payload={"action": "snapshot"},
            device_ref=ref,
        )

    def test_business_commands_are_still_refused_without_a_power_source(self):
        """**存储与能源都是正式实现了，仍不等于能用**：这台网络没有供电源。

        库默认口径下判定是「待确认」而不是「没电」——因为基带的耗能
        blocks.md 未列（R10），严格说我们**算不出**这个网络要多少电。
        「装了东西但没接电源」这件事只有在 DEV 口径下才敢断言成没电。
        """
        self.build_network()
        runtimes = self.server.runtimes.runtimes()
        self.assertEqual(len(runtimes), 1)
        service = runtimes[0].services.get("storage")
        self.assertFalse(service.is_stand_in)
        self.assertTrue(service.is_ready(), "启动层必须把驱动器集合注入进去")
        self.assertEqual(runtimes[0].state(), RunState.NO_POWER)

        result = self.server.submit_command(self.storage_command())
        self.assertEqual(result.status, STATUS_BLOCKED)
        self.assertIn(Reason.ENERGY_UNAVAILABLE, result.reason_codes())

    def test_a_creative_energy_source_makes_the_network_runnable(self):
        """**创造能量源是现在在用的能量源。** 放上它，这台网络就真的能开工。

        这一条是本次增量的目标本身：能源落地之前，「存储能用」只能靠注入
        记录型替身来假装；现在它由一块真实方块（`endernet:creative_energy_source`）
        加 DEV 档的能源口径兑现，走的是与游戏内完全相同的装配路径。
        """
        from endernet.server.bootstrap import ServerBootstrap
        from endernet.server.network.energy import EnergyPolicy

        self.server = ServerBootstrap(energy_policy=EnergyPolicy.dev_stand_in())
        self.server.start(bind_engine=False)
        self.build_network()
        # (2,64,0) 与基带 (1,64,0) 相邻，因此它会成为这个分量的一员。
        self.server.push(
            NormalizedEvent.place(key(2), "endernet:creative_energy_source")
        )
        self.server.tick()

        runtimes = self.server.runtimes.runtimes()
        self.assertEqual(len(runtimes), 1)
        self.assertEqual(runtimes[0].state(), RunState.RUNNABLE)
        result = self.server.submit_command(self.storage_command("r-powered"))
        self.assertEqual(result.status, STATUS_OK, result.to_dict())

    def test_storage_diagnostics_are_reported(self):
        report = self.server.diagnostics()["storage"]
        self.assertIn("unresolved", report)
        self.assertFalse(report["durable_medium_ids"], "介质 ID 还不是持久身份")
        self.assertIn("scope_note", report)
        self.assertEqual(report["slot_count"], STORAGE_DRIVER_SLOT_COUNT.value)

    def test_the_production_profile_notices_the_storage_rules(self):
        """漏报未定规则正是全局约束 7 最怕的形态。"""
        problems = self.server.assert_assembly_ok()
        self.assertTrue(any("R10" in item for item in problems), problems)
        self.assertTrue(any("R07" in item for item in problems), problems)

    def test_the_server_dresses_a_placed_driver(self):
        self.build_network()
        record = self.server.registry.get(key(0, 65))
        from endernet.server.devices.storage_driver import DRIVER_DEVICE_ID as ID

        self.assertEqual(record.device_id, ID)
        self.assertIsNotNone(self.server.drivers.get_by_ref(record.ref))
        self.assertEqual(record.config["disk_count"], 0)

    def test_the_drivers_follow_the_same_lifecycle_changes(self):
        self.server.push(NormalizedEvent.place(key(3), DRIVER_DEVICE_ID))
        self.server.tick()
        record = self.server.registry.get(key(3))
        self.assertIsNotNone(self.server.drivers.get_by_ref(record.ref))

        self.server.push(NormalizedEvent.remove(key(3), DRIVER_DEVICE_ID))
        self.server.tick()
        self.assertIsNone(self.server.drivers.get_by_ref(record.ref))

    def test_a_placed_driver_can_accept_a_medium_end_to_end(self):
        """从生命周期到槽位到投影的一条最短路径。"""
        self.server.push(NormalizedEvent.place(key(3), DRIVER_DEVICE_ID))
        self.server.tick()
        record = self.server.registry.get(key(3))
        state = self.server.drivers.get_by_ref(record.ref)

        medium, reason, detail = self.server.drivers.create_medium(TAPE)
        self.assertIsNotNone(medium, "%s / %s" % (reason, detail))
        ok, reason, detail = state.provider.mount(0, medium.medium_id)
        self.assertTrue(ok, "%s / %s" % (reason, detail))
        self.assertEqual(record.config["disk_count"], 1)
        self.assertEqual(state.disk_count(self.server.drivers.medium), 1)

    def test_stop_marks_the_drivers_unloaded(self):
        self.server.push(NormalizedEvent.place(key(3), DRIVER_DEVICE_ID))
        self.server.tick()
        record = self.server.registry.get(key(3))
        state = self.server.drivers.get_by_ref(record.ref)
        self.server.stop()
        self.assertFalse(state.loaded)
        self.assertIsNotNone(self.server.drivers.medium(medium_id="nothing") or state)


class TestRepositoryArchive(Fixture):
    """12-03：驱动器槽位与介质**走真实仓库**往返。

    与 `TestPersistenceStructures` 的区别是：那边验的是 `save_all()` /
    `restore()` 这对内存结构，这里验的是记录级的存档路径
    （`save_to_repository` / `restore_from_repository`），包括
    **重启后设备代次变了**这一条——那正是真机上最容易丢盘的地方。
    """

    def setUp(self):
        Fixture.setUp(self)
        self.repo = MemoryRepository()

    def _restart(self):
        """模拟「退出世界再进来」：新的注册表（**代次从头数**）+ 新的状态集合。"""
        registry = DeviceRegistry(self.definitions)
        store = StorageDriverStore(
            self.definitions,
            registry,
            policy=self.driver_policy,
            medium_policy=self.medium_policy,
        )
        store.restore_from_repository(self.repo)
        store.use_durable_ids()
        return registry, store

    def _archive(self):
        self.store.save_to_repository(self.repo)
        self.repo.commit()

    # -- 内容跟随身份 --------------------------------------------------

    def test_medium_content_survives(self):
        medium = self.new_medium()
        self.seed(STONE, 4)
        self._archive()

        _, store = self._restart()
        copy = store.medium(medium.medium_id)
        self.assertIsNotNone(copy, "介质 ID 必须原样回来，不得重新分配")
        self.assertEqual(copy.count_of(STONE), 4)
        self.assertEqual(copy.used_micro, medium.used_micro)
        copy.assert_consistent()

    def test_medium_is_keyed_by_its_own_id(self):
        first = self.new_medium(slot=0)
        self._archive()
        self.assertIn(first.medium_id, self.repo.list_ids(MEDIUM_RECORD_KIND))

    def test_orphan_flag_survives(self):
        medium = self.new_medium()
        self.store.drop(self.record.ref)
        self._archive()
        _, store = self._restart()
        self.assertTrue(store.medium(medium.medium_id).orphaned)

    def test_a_medium_without_a_record_is_not_an_empty_disk(self):
        """记录缺 → 引用还在但**不可交易**，绝不当空盘按 0 处理。"""
        medium = self.new_medium()
        self._archive()
        self.repo.delete(MEDIUM_RECORD_KIND, medium.medium_id)

        registry, store = self._restart()
        registry.register(key(0), DRIVER_DEVICE_ID)
        state = store.ensure(registry.get(key(0)).ref)
        # 槽位还是它的（存档里写着），但介质对象不在表里。
        self.assertEqual(state.slot_of(medium.medium_id), 0)
        receipt = state.provider.read(medium.medium_id, STONE, 1)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(receipt.reason, Reason.MEDIUM_UNRECOVERED)

    # -- 槽位按位置重挂（代次变了也要挂上）------------------------------

    def test_slots_are_reattached_by_position_not_generation(self):
        medium = self.new_medium(slot=3)
        self._archive()

        registry, store = self._restart()
        outcome = registry.register(key(0), DRIVER_DEVICE_ID)
        state = store.ensure(outcome.record.ref)
        self.assertEqual(state.slot_of(medium.medium_id), 3)
        self.assertEqual(
            state.assignment(3).slot_version, self.state.assignment(3).slot_version
        )

    def test_slots_land_at_the_documented_slot_only(self):
        medium = self.new_medium(slot=2)
        self._archive()
        registry, store = self._restart()
        registry.register(key(0), DRIVER_DEVICE_ID)
        state = store.ensure(registry.get(key(0)).ref)
        self.assertIsNone(state.assignment(0))
        self.assertEqual(state.slot_of(medium.medium_id), 2)

    def test_a_different_position_does_not_claim_the_slots(self):
        medium = self.new_medium(slot=1)
        self._archive()
        registry, store = self._restart()
        outcome = registry.register(key(9), DRIVER_DEVICE_ID)
        state = store.ensure(outcome.record.ref)
        self.assertIsNone(state.slot_of(medium.medium_id))

    def test_restoring_twice_does_not_duplicate_the_medium(self):
        medium = self.new_medium()
        self._archive()
        _, store = self._restart()
        store.restore_from_repository(self.repo)
        self.assertEqual(sorted(store.mediums()), [medium.medium_id])

    # -- 未知新版本 -------------------------------------------------------

    def _make_record_future(self, kind, record_id):
        """把一条记录改成「更新的构建写下的版本」，绕过写入闸。"""
        record = self.repo.load(kind, record_id)
        record.schema_version = 99
        self.repo._store[kind][record_id] = record

    def test_a_future_version_record_is_kept_read_only(self):
        medium = self.new_medium(slot=0)
        self._archive()
        self._make_record_future(MEDIUM_RECORD_KIND, medium.medium_id)

        _, store = self._restart()
        # 内容接管进内存（不能丢），但**冻结**：不进任何写回路径。
        self.assertIsNotNone(store.medium(medium.medium_id))
        self.assertIn(medium.medium_id, store.diagnostics()["frozen_mediums"])

    def test_a_frozen_medium_is_never_rewritten(self):
        """覆盖掉一份更新的存档，是本任务最不能犯的错。"""
        medium = self.new_medium(slot=0)
        self._archive()
        self._make_record_future(MEDIUM_RECORD_KIND, medium.medium_id)
        _, store = self._restart()

        store.note_medium_changed(medium.medium_id)
        self.assertEqual(store.pending_archive_writes(), 0)
        # 连拆除导致的孤儿标记也不写回。
        store.drop(self.record.ref)
        self.assertEqual(store.pending_archive_writes(), 0)

    # -- 读不懂的存档一律不写回（代码评审揪出的两条真缺陷）----------------

    def _make_payload_unreadable(self, record_id):
        """把介质**载荷自己的**版本抬到 2——信封不动。

        这模拟的是「载荷读不懂」，与「信封版本过新」是**两套**判据。
        只按信封判的实现在这里会放行。
        """
        record = self.repo.load(MEDIUM_RECORD_KIND, record_id)
        record.payload["schema_version"] = 2
        self.repo._store[MEDIUM_RECORD_KIND][record_id] = record

    def test_an_unreadable_payload_is_frozen_not_just_the_envelope(self):
        medium = self.new_medium(slot=0)
        self.seed(STONE, 4)
        self._archive()
        self._make_payload_unreadable(medium.medium_id)

        _, store = self._restart()
        recovered = store.medium(medium.medium_id)
        self.assertIsNotNone(recovered)
        self.assertNotEqual(recovered.recovery, "ok")
        self.assertFalse(recovered.is_tradeable())
        self.assertIn(medium.medium_id, store.diagnostics()["frozen_mediums"])

    def test_a_broken_driver_does_not_overwrite_an_unreadable_disk(self):
        """**最严重的一条**：载荷读不懂的盘被写回去会变成一枚**空盘**，
        内容永久消失——`Medium.to_dict()` 没有 `raw_payload` 回退。

        注意**必须在新 store 里重新登记设备再拆**：拿夹具原来那个 `ref` 去拆，
        `get_by_ref` 找不到（新 store 里一台设备都没有），`drop` 是个空操作，
        用例就会**永远通过**——它测的东西一次都没跑过。
        """
        medium = self.new_medium(slot=0)
        self.seed(STONE, 4)
        self._archive()
        self._make_payload_unreadable(medium.medium_id)
        before = dict(self.repo._store[MEDIUM_RECORD_KIND][medium.medium_id].payload)

        registry, store = self._restart()
        outcome = registry.register(key(0), DRIVER_DEVICE_ID)
        state = store.ensure(outcome.record.ref)
        self.assertEqual(state.slot_of(medium.medium_id), 0, "槽位没挂上，下面测不到")
        # 拆掉驱动器 → `drop()` → 标脏（修复前这里就把空盘写回去了）
        self.assertIsNotNone(store.drop(outcome.record.ref), "drop 没生效")
        store.save_to_repository(self.repo)

        after = self.repo._store[MEDIUM_RECORD_KIND][medium.medium_id].payload
        self.assertEqual(after, before, "读不懂的载荷被改写了")
        self.assertEqual(after["schema_version"], 2)

    def test_a_future_version_driver_record_is_not_rewritten(self):
        """驱动器那半边同样只管了信封版本，漏掉了写回。"""
        medium = self.new_medium(slot=0)
        self._archive()
        key_text = "0,0,64,0"
        record = self.repo.load(DRIVER_RECORD_KIND, key_text)
        record.schema_version = 99
        record.payload["future_only_field"] = "keepme"
        self.repo._store[DRIVER_RECORD_KIND][key_text] = record

        registry, store = self._restart()
        outcome = registry.register(key(0), DRIVER_DEVICE_ID)
        state = store.ensure(outcome.record.ref)
        # 槽位照常按最好猜测应用……
        self.assertEqual(state.slot_of(medium.medium_id), 0)
        # ……但任何槽位变化都**不写回**。
        self.assertTrue(state.provider.unmount(0)[0])
        self.assertTrue(state.provider.mount(0, medium.medium_id)[0])
        store.save_to_repository(self.repo)

        stored = self.repo._store[DRIVER_RECORD_KIND][key_text]
        self.assertEqual(stored.schema_version, 99)
        self.assertEqual(stored.payload.get("future_only_field"), "keepme")
        self.assertIn(key_text, store.diagnostics()["frozen_drivers"])

    def test_two_unreadable_medium_records_do_not_abort_the_restore(self):
        """读不懂的载荷没有 `medium_id`，会全用占位 ID 撞在一起，
        异常从恢复循环逃出去——**后面连槽位都恢复不了**。"""
        self._archive()
        for record_id in ("medium:x", "medium:y"):
            self.repo.write(Record(MEDIUM_RECORD_KIND, record_id, {"junk": True}))
        self.repo.commit()

        # 不能抛：抛了 `bootstrap.restore_persistence` 会把整轮恢复判失败。
        _, store = self._restart()
        self.assertIsNotNone(store.medium("medium:x"))
        self.assertIsNotNone(store.medium("medium:y"))
        self.assertEqual(sorted(store.diagnostics()["frozen_mediums"]), ["medium:x", "medium:y"])

    # -- 槽数放不下的槽位：保留不截断 --------------------------------------

    def test_slots_that_do_not_fit_are_preserved_not_truncated(self):
        """R10 槽数未定案：存档里有、当前槽数放不下的槽位**不能静默丢掉**，
        更不能把「装得下的那几个」写回去而让档案里那条引用永久消失。"""
        medium = self.new_medium(slot=3)
        self._archive()
        before = dict(self.repo._store[DRIVER_RECORD_KIND]["0,0,64,0"].payload)

        # 用更小的槽数重启（R10 改小 / 未配置）
        registry = DeviceRegistry(self.definitions)
        store = StorageDriverStore(
            self.definitions,
            registry,
            policy=DriverPolicy(slot_count=2),
            medium_policy=self.medium_policy,
        )
        store.restore_from_repository(self.repo)
        store.use_durable_ids()
        outcome = registry.register(key(0), DRIVER_DEVICE_ID)
        state = store.ensure(outcome.record.ref)

        self.assertIsNone(state.slot_of(medium.medium_id))
        self.assertEqual(store.diagnostics()["unplaced_slots"], {"0,0,64,0": 1})
        # 档案原样：那条放不下的引用还在。
        store.save_to_repository(self.repo)
        self.assertEqual(
            self.repo._store[DRIVER_RECORD_KIND]["0,0,64,0"].payload, before
        )

    # -- 拆除 ------------------------------------------------------------

    def test_a_dropped_driver_record_is_removed(self):
        """同位置重放的新实例**不得继承**旧槽位引用（12 验收）。"""
        self.new_medium(slot=0)
        self.store.drop(self.record.ref)
        self._archive()
        self.assertEqual(self.repo.list_ids(DRIVER_RECORD_KIND), [])

    # -- 持久 ID ---------------------------------------------------------

    def test_durable_ids_continue_above_the_archive(self):
        first = self.new_medium(slot=0)
        self._archive()
        _, store = self._restart()
        second, reason, detail = store.create_medium(TAPE)
        self.assertIsNotNone(second, "%s / %s" % (reason, detail))
        self.assertNotEqual(second.medium_id, first.medium_id)
        self.assertGreater(
            medium_sequence(second.medium_id), medium_sequence(first.medium_id)
        )

    def test_durable_flag_is_reported(self):
        self.assertFalse(self.store.diagnostics()["durable_medium_ids"])
        self.store.use_durable_ids()
        self.assertTrue(self.store.diagnostics()["durable_medium_ids"])

    # -- 跨驱动器重复挂载 -------------------------------------------------

    def test_the_same_medium_cannot_be_mounted_on_two_drivers(self):
        """两台驱动器各插一块同 ID 的盘，会让一份内容被记两次。"""
        medium = self.new_medium(slot=0)
        other = self.place(5)
        other_state = self.store.get_by_ref(other.ref)
        ok, reason, detail = other_state.provider.mount(0, medium.medium_id)
        self.assertFalse(ok)
        self.assertEqual(reason, Reason.MEDIUM_DUPLICATE_MOUNT)
        # 报错要说清楚它现在装在哪——否则玩家只知道「不行」。
        self.assertIn("0 号槽", detail)

    def test_the_same_medium_can_be_remounted_after_unmount(self):
        """拒绝的是**同时**挂两处，不是「这块盘从此不能动」。"""
        medium = self.new_medium(slot=0)
        other = self.place(5)
        other_state = self.store.get_by_ref(other.ref)
        self.assertTrue(self.state.provider.unmount(0)[0])
        ok, reason, detail = other_state.provider.mount(0, medium.medium_id)
        self.assertTrue(ok, "%s / %s" % (reason, detail))


class TestBootstrapPersistence(unittest.TestCase):
    """12-03：装配层真的接上了存档后端。

    用 `ServerBootstrap` 的**离线路径**验接线：`bind_engine=False` 不碰引擎，
    后端换成内存替身。测的是「恢复在区块发现之前」「帧尾提交」「停机落盘」
    这三条时序，不是引擎行为。
    """

    def setUp(self):
        from endernet.server.bootstrap import ServerBootstrap

        self.backend = testsupport.MemoryExtraDataBackend()
        self.server = ServerBootstrap(
            world=test_bootstrap_world(),
            persistence_backend=self.backend,
        )

    def test_diagnostics_report_the_backend(self):
        self.server.start(bind_engine=False)
        report = self.server.diagnostics()["persistence"]
        self.assertTrue(report["enabled"])
        self.assertEqual(report["backend"], "MemoryExtraDataBackend")

    def test_diagnostics_report_disabled_without_a_backend(self):
        from endernet.server.bootstrap import ServerBootstrap

        server = ServerBootstrap(world=test_bootstrap_world())
        self.assertFalse(server.diagnostics()["persistence"]["enabled"])

    def test_a_placed_driver_and_medium_are_persisted(self):
        self.server.start(bind_engine=False)
        self.server.push(NormalizedEvent.place(key(3), DRIVER_DEVICE_ID))
        self.server.tick()
        record = self.server.registry.get(key(3))
        state = self.server.drivers.get_by_ref(record.ref)
        medium, _, _ = self.server.drivers.create_medium(TAPE)
        self.assertTrue(state.provider.mount(0, medium.medium_id)[0])

        self.server.tick()
        self.server.stop()
        kind = "endernet.storage_medium"
        self.assertIn(medium.medium_id, self.server.repository.list_ids(kind))

    def test_an_idle_frame_does_not_write_to_disk(self):
        """没有脏记录时一次 `SaveExtraData()` 都不发。"""
        self.server.start(bind_engine=False)
        self.server.tick()
        self.server.tick()
        self.assertEqual(self.backend.saves, 0)

    def test_restore_happens_before_chunk_discovery(self):
        """恢复必须先于区块发现：反过来槽位就会建在空状态上。"""
        self.server.start(bind_engine=False)
        self.server.push(NormalizedEvent.place(key(3), DRIVER_DEVICE_ID))
        self.server.tick()
        record = self.server.registry.get(key(3))
        state = self.server.drivers.get_by_ref(record.ref)
        medium, _, _ = self.server.drivers.create_medium(TAPE)
        self.assertTrue(state.provider.mount(2, medium.medium_id)[0])
        self.server.tick()
        self.server.stop()

        # 重进世界：新的装配层，读同一份后端。
        from endernet.server.bootstrap import ServerBootstrap

        revived = ServerBootstrap(
            world=test_bootstrap_world(), persistence_backend=self.backend
        )
        revived.start(bind_engine=False)
        revived.push(NormalizedEvent.place(key(3), DRIVER_DEVICE_ID))
        revived.tick()
        revived_record = revived.registry.get(key(3))
        revived_state = revived.drivers.get_by_ref(revived_record.ref)
        self.assertEqual(revived_state.slot_of(medium.medium_id), 2)


def test_bootstrap_world():
    """一份不碰引擎的世界替身。见 `test_bootstrap.FakeWorld` 的同一理由。"""
    from test_bootstrap import FakeWorld

    return FakeWorld()


if __name__ == "__main__":
    unittest.main()
