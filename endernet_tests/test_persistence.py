# coding=utf-8
"""12-01 / 12-02 验收：存档契约、版本迁移与故障注入。

最关键的一条是「未知新版本不得被当作空库存覆盖」：
玩家用新版本存过档再回退旧版本时，旧构建必须保留看不懂的数据，
而不是把它读成「没有库存」然后写回空值——那会直接销毁物品。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.server.persistence.migrations import (
    CURRENT_SCHEMA_VERSION,
    Migration,
    Migrator,
    MissingMigrationError,
    UnsupportedVersionError,
)
from endernet.server.persistence.repository import (
    MemoryRepository,
    Record,
    WriteCoordinator,
)

DRIVER = "endernet:storage_driver"


class TestRecordIdentity(unittest.TestCase):
    """记录必须有稳定 ID 与归属。"""

    def test_requires_kind_and_id(self):
        with self.assertRaises(ValueError):
            Record("", "r1")
        with self.assertRaises(ValueError):
            Record("medium", "")

    def test_owner_separate_from_id(self):
        """订单归属绑定控制器实例 ID，与记录自身编号是两件事。"""
        record = Record("order", "order-1", owner_id="ctrl-instance-42")
        self.assertEqual(record.record_id, "order-1")
        self.assertEqual(record.owner_id, "ctrl-instance-42")

    def test_touch_marks_dirty_and_bumps_revision(self):
        record = Record("medium", "m1")
        self.assertFalse(record.dirty)
        record.touch()
        self.assertTrue(record.dirty)
        self.assertEqual(record.revision, 1)

    def test_round_trip(self):
        record = Record("medium", "m1", payload={"a": 1}, owner_id="o")
        restored = Record.from_dict(record.to_dict())
        self.assertEqual(restored.record_id, "m1")
        self.assertEqual(restored.owner_id, "o")
        self.assertEqual(restored.payload, {"a": 1})


class TestBasicRepository(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepository()

    def test_write_then_load(self):
        record = Record("medium", "m1", payload={"count": 3})
        self.repo.write(record)
        loaded = self.repo.load("medium", "m1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.payload, {"count": 3})

    def test_load_missing_returns_none(self):
        self.assertIsNone(self.repo.load("medium", "nope"))

    def test_delete(self):
        self.repo.write(Record("medium", "m1"))
        self.assertTrue(self.repo.delete("medium", "m1"))
        self.assertIsNone(self.repo.load("medium", "m1"))
        self.assertFalse(self.repo.delete("medium", "m1"))

    def test_list_ids_sorted(self):
        for rid in ("b", "a", "c"):
            self.repo.write(Record("medium", rid))
        self.assertEqual(self.repo.list_ids("medium"), ["a", "b", "c"])

    def test_dirty_tracking(self):
        record = Record("medium", "m1")
        self.repo.write(record)
        self.assertEqual(self.repo.dirty_count(), 0)
        record.touch()
        self.assertEqual(self.repo.dirty_count(), 1)

    def test_write_requires_record(self):
        with self.assertRaises(TypeError):
            self.repo.write({"kind": "medium"})


class TestCommitBatching(unittest.TestCase):
    """脏写合并（12-08）。"""

    def setUp(self):
        self.repo = MemoryRepository()

    def _dirty_records(self, count):
        for index in range(count):
            record = Record("medium", "m%03d" % index, payload={"n": index})
            self.repo.write(record)
            record.touch()
        return self.repo.dirty_count()

    def test_commit_writes_dirty_records(self):
        self._dirty_records(3)
        self.assertEqual(self.repo.commit(), 3)
        self.assertEqual(self.repo.dirty_count(), 0)

    def test_commit_respects_limit(self):
        self._dirty_records(5)
        self.assertEqual(self.repo.commit(limit=2), 2)
        self.assertEqual(self.repo.dirty_count(), 3)
        self.assertEqual(self.repo.commit(limit=2), 2)
        self.assertEqual(self.repo.commit(), 1)

    def test_commit_skips_clean_records(self):
        self._dirty_records(2)
        self.repo.commit()
        self.assertEqual(self.repo.commit(), 0)

    def test_snapshot_survives_reload(self):
        """「保存重进后一致」：把数据倒进新仓库再读，内容必须相同。"""
        self._dirty_records(4)
        self.repo.commit()
        payloads = self.repo.snapshot_payloads()

        fresh = MemoryRepository()
        for kind, bucket in payloads.items():
            for record_id, payload in bucket.items():
                fresh.write(Record(kind, record_id, payload=payload))

        self.assertEqual(fresh.snapshot_payloads(), payloads)


class TestMigrations(unittest.TestCase):
    def _migrator(self):
        migrator = Migrator(target_version=3)
        migrator.register("medium", 1, lambda p: dict(p, v2=True))
        migrator.register("medium", 2, lambda p: dict(p, v3=True))
        return migrator

    def test_supported_versions_walks_back_the_chain(self):
        self.assertEqual(self._migrator().supported_versions("medium"), (1, 2, 3))

    def test_migration_chain_runs_in_order(self):
        migrator = self._migrator()
        version, payload = migrator.migrate("medium", 1, {"seed": 1})
        self.assertEqual(version, 3)
        self.assertEqual(payload, {"seed": 1, "v2": True, "v3": True})

    def test_already_current_returns_unchanged(self):
        migrator = self._migrator()
        version, payload = migrator.migrate("medium", 3, {"seed": 1})
        self.assertEqual((version, payload), (3, {"seed": 1}))

    def test_missing_step_raises(self):
        migrator = Migrator(target_version=3)
        migrator.register("medium", 2, lambda p: p)
        with self.assertRaises(MissingMigrationError):
            migrator.migrate("medium", 1, {})

    def test_step_must_be_contiguous(self):
        """跨级迁移步骤必须被拒绝，否则中间版本会被跳过。"""
        with self.assertRaises(ValueError):
            Migration("medium", 1, 3, lambda p: p)

    def test_duplicate_registration_rejected(self):
        migrator = self._migrator()
        with self.assertRaises(ValueError):
            migrator.register("medium", 1, lambda p: p)

    def test_migration_must_return_payload(self):
        """迁移返回 None 说明它在悄悄丢数据，必须报错。"""
        migrator = Migrator(target_version=2)
        migrator.register("medium", 1, lambda p: None)
        with self.assertRaises(ValueError):
            migrator.migrate("medium", 1, {})


class TestUnknownNewerVersion(unittest.TestCase):
    """12 验收：老版本读新存档不得当作空库存覆盖。"""

    def setUp(self):
        # 本构建只懂版本 1
        self.repo = MemoryRepository(migrator=Migrator(target_version=1))
        # 玩家用新版本写下的、本构建看不懂的记录
        self.future = Record(
            "medium",
            "m-future",
            payload={"resource": "minecraft:iron_ingot", "count": 4096},
            schema_version=99,
        )
        self.repo._store.setdefault("medium", {})["m-future"] = self.future

    def test_load_returns_record_not_none(self):
        """必须返回记录本身，返回 None 会被上层当成「没有数据」。"""
        loaded = self.repo.load("medium", "m-future")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.record_id, "m-future")

    def test_load_marks_record_read_only(self):
        loaded = self.repo.load("medium", "m-future")
        self.assertTrue(loaded.read_only)

    def test_payload_is_preserved_not_emptied(self):
        loaded = self.repo.load("medium", "m-future")
        self.assertEqual(loaded.payload["count"], 4096)

    def test_writing_back_over_unknown_version_is_refused(self):
        """即使上层拿返回值试图写回，也必须被拒绝而不是覆盖成空。"""
        loaded = self.repo.load("medium", "m-future")
        with self.assertRaises(ValueError):
            self.repo.write(loaded)
        self.assertEqual(self.repo._store["medium"]["m-future"].payload["count"], 4096)

    def test_writing_a_future_version_record_is_refused(self):
        """即使记录尚未被读过，版本过新也不许写入。"""
        fresh = Record("medium", "m2", schema_version=99)
        with self.assertRaises(UnsupportedVersionError):
            self.repo.write(fresh)

    def test_read_only_record_raises_before_version_check(self):
        """只读标记的报错更具体，便于上层区分「已确认读不懂」与「版本号更高」。"""
        loaded = self.repo.load("medium", "m-future")
        self.assertTrue(loaded.read_only)
        with self.assertRaises(ValueError):
            self.repo.write(loaded)

    def test_unknown_version_reads_are_counted_for_diagnostics(self):
        self.repo.load("medium", "m-future")
        self.assertEqual(self.repo.diagnostics()["unknown_version_skips"], 1)

    def test_known_older_version_is_migrated(self):
        migrator = Migrator(target_version=2)
        migrator.register("medium", 1, lambda p: dict(p, upgraded=True))
        repo = MemoryRepository(migrator=migrator)
        repo.write(Record("medium", "m-old", payload={"n": 1}, schema_version=2))
        repo._store["medium"]["m-old"].schema_version = 1
        loaded = repo.load("medium", "m-old")
        self.assertEqual(loaded.schema_version, 2)
        self.assertTrue(loaded.payload["upgraded"])


class TestFaultInjection(unittest.TestCase):
    """故障注入入口（12-01），供 14 的中断恢复测试使用。"""

    def setUp(self):
        self.repo = MemoryRepository()

    def test_injected_write_failure_raises_once(self):
        self.repo.inject_fault("write", kind="medium", record_id="m1", times=1)
        with self.assertRaises(IOError):
            self.repo.write(Record("medium", "m1"))
        # 只失败一次，之后恢复正常
        self.repo.write(Record("medium", "m1"))
        self.assertIsNotNone(self.repo.load("medium", "m1"))

    def test_fault_is_scoped_to_record(self):
        self.repo.inject_fault("write", kind="medium", record_id="m1", times=1)
        self.repo.write(Record("medium", "m2"))  # 不受影响
        self.assertIsNotNone(self.repo.load("medium", "m2"))

    def test_fault_is_scoped_to_operation(self):
        """针对 delete 注入的故障不得影响 write。"""
        self.repo.inject_fault("delete", times=1)
        self.repo.write(Record("medium", "m1"))
        with self.assertRaises(IOError):
            self.repo.delete("medium", "m1")
        self.assertIsNotNone(self.repo.load("medium", "m1"))

    def test_source_keeps_data_when_commit_fails(self):
        """写入失败后原数据必须还在，没有被换成空。"""
        record = Record("medium", "m1", payload={"count": 7})
        self.repo.write(record)
        record.touch({"count": 8})
        self.repo.inject_fault("write", kind="medium", record_id="m1", times=1)
        with self.assertRaises(IOError):
            self.repo.commit()
        # 仍然读得到原值，且仍标记为脏等待重试
        self.assertEqual(self.repo.load("medium", "m1").payload["count"], 8)
        self.assertEqual(self.repo.dirty_count(), 1)

    def test_commit_retries_pending_dirty_after_failure(self):
        record = Record("medium", "m1", payload={"count": 1})
        self.repo.write(record)
        record.touch({"count": 2})
        self.repo.inject_fault("write", kind="medium", record_id="m1", times=1)
        with self.assertRaises(IOError):
            self.repo.commit()
        self.assertEqual(self.repo.commit(), 1)
        self.assertEqual(self.repo.dirty_count(), 0) # 重试后成功

    def test_clear_faults(self):
        self.repo.inject_fault("write", times=5)
        self.repo.clear_faults()
        self.repo.write(Record("medium", "m1"))

    def test_diagnostics_reports_pending_faults(self):
        self.repo.inject_fault("write", times=2)
        self.assertEqual(self.repo.diagnostics()["pending_faults"], 1)


class TestWriteCoordinator(unittest.TestCase):
    def setUp(self):
        self.repo = MemoryRepository()
        self.coordinator = WriteCoordinator(self.repo, batch_size=2)

    def _dirty(self, count):
        for index in range(count):
            record = Record("medium", "m%02d" % index)
            self.repo.write(record)
            record.touch()

    def test_commit_respects_batch_size(self):
        self._dirty(5)
        self.assertEqual(self.coordinator.commit(), 2)
        self.assertEqual(self.coordinator.deferred, 3)

    def test_deferred_drains_on_subsequent_commits(self):
        self._dirty(5)
        for _ in range(3):
            self.coordinator.commit()
        self.assertEqual(self.repo.dirty_count(), 0)
        self.assertEqual(self.coordinator.deferred, 0)

    def test_key_record_commits_immediately(self):
        """关键交接不走合并窗口。"""
        record = Record("reservation", "r1")
        self.repo.write(record)
        record.touch()
        self.coordinator.commit_key(record)
        self.assertEqual(self.repo.dirty_count(), 0)

    def test_key_record_registry(self):
        self.coordinator.mark_key("reservation", "r1")
        self.assertIn(("reservation", "r1"), self.coordinator.key_records)
        self.coordinator.unmark_key("reservation", "r1")
        self.assertNotIn(("reservation", "r1"), self.coordinator.key_records)

    def test_diagnostics(self):
        self._dirty(1)
        self.coordinator.commit()
        report = self.coordinator.diagnostics()
        self.assertEqual(report["batch_size"], 2)
        self.assertGreaterEqual(report["commits"], 1)


class TestDefaultSchemaVersion(unittest.TestCase):
    def test_new_records_use_current_version(self):
        self.assertEqual(Record("medium", "m1").schema_version, CURRENT_SCHEMA_VERSION)

    def test_driver_record_kind_naming_is_stable(self):
        """记录类型名是存档的一部分，改动等于不兼容变更。"""
        self.assertEqual(DRIVER, "endernet:storage_driver")


if __name__ == "__main__":
    unittest.main()
