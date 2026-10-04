# coding=utf-8
"""真实存档后端（12-03）：`ExtraDataRepository` + 引擎口子。

**全部离线。** 引擎访问被压进 `backend` 这个四原语口子，替身见
`testsupport.MemoryExtraDataBackend`——它刻意分「内存区 / 已提交」两份，
因为真实引擎就是这么分的（`persistence_backend.md` §3.3）。

这一份测的是**仓库层的契约**（键映射、索引维护、提交合并、版本闸门），
不是引擎行为。引擎那边的读数归真机，见 12 交付说明。
"""

import json
import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.server.persistence.migrations import Migrator
from endernet.server.adapters.persistence_engine import EngineExtraDataBackend
from endernet.server.persistence.repository import (
    ExtraDataRepository,
    MemoryRepository,
    Record,
    dumps_index,
    dumps_payload,
    index_key,
    loads_index,
    loads_payload,
    record_key,
)

MEDIUM = "endernet.storage_medium"
DRIVER = "endernet.storage_driver"


class TestTextCodec(unittest.TestCase):
    def test_unicode_payload_serializes_to_ascii_str(self):
        payload = {u"notes": u"驱动器已拆除", u"medium_id": u"medium:1"}
        raw = dumps_payload(payload)
        self.assertIs(type(raw), str)
        raw.encode("ascii")
        self.assertEqual(loads_payload(raw), payload)

    def test_unicode_index_serializes_to_ascii_str(self):
        ids = [u"medium:2", u"medium:1"]
        raw = dumps_index(ids)
        self.assertIs(type(raw), str)
        raw.encode("ascii")
        self.assertEqual(loads_index(raw), sorted(ids))

    def test_unicode_and_bytes_can_be_read(self):
        self.assertEqual(loads_payload(u'{"notes": "已拆除"}'),
                         {u"notes": u"已拆除"})
        self.assertEqual(loads_index(u'["medium:1"]'), [u"medium:1"])
        self.assertEqual(loads_payload(b'{"n": 1}'), {"n": 1})
        self.assertEqual(loads_index(b'["medium:1"]'), [u"medium:1"])

    def test_non_text_is_rejected(self):
        for value in (None, 1, {}, []):
            with self.assertRaises(TypeError):
                loads_payload(value)
            with self.assertRaises(TypeError):
                loads_index(value)


class BytesOnlyExtraDataComponent(object):
    """模拟 SDK 字节串边界；py3 也能检出 unicode 参数漏转。"""

    def __init__(self):
        self.values = {}
        self.auto_save = None
        self.saved = 0

    def GetExtraData(self, key):
        assert type(key) is bytes
        return self.values.get(key)

    def SetExtraData(self, key, value, auto_save):
        assert type(key) is bytes
        assert type(value) is bytes
        self.values[key] = value
        self.auto_save = auto_save
        return True

    def CleanExtraData(self, key):
        assert type(key) is bytes
        self.values.pop(key, None)
        return True

    def SaveExtraData(self):
        self.saved += 1
        return True


class TestEngineTextBoundary(unittest.TestCase):
    def setUp(self):
        self.component = BytesOnlyExtraDataComponent()
        self.backend = EngineExtraDataBackend()
        self.backend._component = lambda: self.component

    def test_unicode_key_and_value_are_encoded(self):
        self.assertTrue(self.backend.set(u"endernet:测试", u"载荷", False))
        self.assertEqual(self.backend.get(u"endernet:测试"),
                         u"载荷".encode("utf-8"))
        self.assertFalse(self.component.auto_save)
        self.assertTrue(self.backend.delete(u"endernet:测试"))
        self.assertIsNone(self.backend.get(u"endernet:测试"))

    def test_restored_medium_can_be_written_and_deleted(self):
        repo = ExtraDataRepository(self.backend)
        payload = {u"notes": u"驱动器已拆除"}
        repo.write(Record(MEDIUM, u"medium:1", payload))
        repo.commit()
        restored_id = repo.list_ids(MEDIUM)[0]
        record = repo.load(MEDIUM, restored_id)
        self.assertEqual(record.payload, payload)
        repo.write(record)
        self.assertTrue(repo.delete(MEDIUM, restored_id))
        repo.commit()
        self.assertEqual(repo.list_ids(MEDIUM), [])
        self.assertIsNone(repo.load(MEDIUM, restored_id))
        self.assertEqual(self.component.saved, 2)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.backend = testsupport.MemoryExtraDataBackend()
        self.repo = ExtraDataRepository(self.backend)

    def write(self, kind, record_id, payload=None, **kwargs):
        record = Record(kind, record_id, payload=payload or {"n": record_id}, **kwargs)
        self.repo.write(record)
        return record


class TestKeyMapping(unittest.TestCase):
    """键的形状是**存档的一部分**，改动等于不兼容变更。"""

    def test_record_key_shape(self):
        self.assertEqual(record_key(MEDIUM, "medium:1"), "endernet:endernet.storage_medium/medium:1")

    def test_index_key_shape(self):
        self.assertEqual(index_key(MEDIUM), "endernet:index/endernet.storage_medium")

    def test_kind_and_id_land_on_distinct_keys(self):
        """不同 kind 的同一个 ID 不能撞到一个键上。"""
        self.assertNotEqual(record_key(MEDIUM, "x"), record_key(DRIVER, "x"))


class TestRoundTrip(Fixture):
    def test_write_then_load(self):
        self.write(MEDIUM, "medium:1", {"count": 3})
        loaded = self.repo.load(MEDIUM, "medium:1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.payload, {"count": 3})
        self.assertEqual(loaded.record_id, "medium:1")

    def test_load_missing_returns_none(self):
        self.assertIsNone(self.repo.load(MEDIUM, "nope"))

    def test_payload_is_stored_as_a_string(self):
        """**存字符串不存嵌套字典**：只有字符串的字节级往返是实测过的。"""
        self.write(MEDIUM, "medium:1")
        raw = self.backend.get(record_key(MEDIUM, "medium:1"))
        self.assertIsInstance(raw, str)

    def test_non_ascii_text_survives(self):
        """中文 `notes`（py2 下是 UTF-8 字节串）往返不能坏。"""
        self.write(MEDIUM, "medium:1", {"notes": u"驱动器已拆除"})
        self.repo.commit()
        self.assertEqual(self.repo.load(MEDIUM, "medium:1").payload["notes"], u"驱动器已拆除")

    def test_owner_and_revision_survive(self):
        self.write(MEDIUM, "medium:1", owner_id="ctrl-1")
        loaded = self.repo.load(MEDIUM, "medium:1")
        self.assertEqual(loaded.owner_id, "ctrl-1")

    def test_load_prefers_the_key_over_the_payload(self):
        """键里的 (kind, id) 是权威：载荷里那两份只是副本。"""
        self.write(MEDIUM, "medium:1")
        loaded = self.repo.load(MEDIUM, "medium:1")
        self.assertEqual(loaded.kind, MEDIUM)

    def test_corrupt_payload_raises_instead_of_returning_empty(self):
        """**「读不懂」不等于「没有数据」。** 返回空字典会被写回去。"""
        self.backend.set(record_key(MEDIUM, "medium:1"), "这不是 JSON", False)
        with self.assertRaises(ValueError):
            self.repo.load(MEDIUM, "medium:1")

    def test_non_object_payload_raises(self):
        self.backend.set(record_key(MEDIUM, "medium:1"), "[1, 2]", False)
        with self.assertRaises(ValueError):
            self.repo.load(MEDIUM, "medium:1")


class TestIndexMaintenance(Fixture):
    """`list_ids` 只能靠索引键——引擎的 ExtraData 没有键枚举接口。"""

    def test_index_tracks_writes(self):
        self.write(MEDIUM, "medium:2")
        self.write(MEDIUM, "medium:1")
        self.assertEqual(self.repo.list_ids(MEDIUM), ["medium:1", "medium:2"])

    def test_index_is_per_kind(self):
        self.write(MEDIUM, "medium:1")
        self.write(DRIVER, "0,1,2,3")
        self.assertEqual(self.repo.list_ids(MEDIUM), ["medium:1"])
        self.assertEqual(self.repo.list_ids(DRIVER), ["0,1,2,3"])

    def test_rewriting_the_same_record_does_not_duplicate_the_index(self):
        for _ in range(3):
            self.write(MEDIUM, "medium:1")
        self.assertEqual(self.repo.list_ids(MEDIUM), ["medium:1"])

    def test_delete_removes_from_the_index(self):
        self.write(MEDIUM, "medium:1")
        self.assertTrue(self.repo.delete(MEDIUM, "medium:1"))
        self.assertEqual(self.repo.list_ids(MEDIUM), [])
        self.assertIsNone(self.repo.load(MEDIUM, "medium:1"))

    def test_delete_of_an_unknown_record_is_false(self):
        self.assertFalse(self.repo.delete(MEDIUM, "nope"))

    def test_empty_kind_has_no_ids(self):
        self.assertEqual(self.repo.list_ids(MEDIUM), [])

    def test_a_corrupt_index_raises_rather_than_looking_empty(self):
        """索引读不出来时**不能**报「一条都没有」——那会让恢复以为世界是空的。"""
        self.backend.set(index_key(MEDIUM), "{不是列表}", False)
        with self.assertRaises(ValueError):
            self.repo.list_ids(MEDIUM)


class TestCommitSemantics(Fixture):
    """提交点只有一个，且**按调用次数付费**（§3.2）。"""

    def test_writes_are_visible_before_commit(self):
        """引擎读的是内存区，所以提交前就查得到——但它还没进 committed。"""
        self.write(MEDIUM, "medium:1")
        self.assertIn(record_key(MEDIUM, "medium:1"), self.backend.pending)
        self.assertNotIn(record_key(MEDIUM, "medium:1"), self.backend.committed)

    def test_commit_promotes_pending(self):
        self.write(MEDIUM, "medium:1")
        self.assertEqual(self.repo.commit(), 1)
        self.assertIn(record_key(MEDIUM, "medium:1"), self.backend.committed)

    def test_many_writes_cost_one_save_call(self):
        """**合并不是优化，是必需**：固定开销按调用次数付。"""
        for index in range(20):
            self.write(MEDIUM, "medium:%d" % index)
        self.repo.commit()
        self.assertEqual(self.backend.saves, 1)

    def test_idle_commit_calls_nothing(self):
        """没有待提交的改动时一次 `SaveExtraData()` 都不发。"""
        self.repo.commit()
        self.assertEqual(self.backend.saves, 0)

    def test_second_commit_after_a_quiet_frame_is_a_no_op(self):
        self.write(MEDIUM, "medium:1")
        self.repo.commit()
        self.repo.commit()
        self.assertEqual(self.backend.saves, 1)

    def test_commit_reports_the_number_of_records(self):
        self.write(MEDIUM, "medium:1")
        self.write(MEDIUM, "medium:2")
        self.assertEqual(self.repo.commit(), 2)

    def test_dirty_count_tracks_uncommitted_writes(self):
        self.write(MEDIUM, "medium:1")
        self.assertEqual(self.repo.dirty_count(), 1)
        self.repo.commit()
        self.assertEqual(self.repo.dirty_count(), 0)

    def test_index_writes_are_committed_too(self):
        self.write(MEDIUM, "medium:1")
        self.repo.commit()
        self.assertIn(index_key(MEDIUM), self.backend.committed)


class TestVersionGate(Fixture):
    """12 验收：老版本读新存档不得当作空库存覆盖。"""

    def test_unknown_newer_version_is_preserved_read_only(self):
        record = Record(MEDIUM, "medium:future", payload={"count": 4096}, schema_version=99)
        # 绕过写入闸，模拟「新版本写下的存档」
        self.backend.set(
            record_key(MEDIUM, "medium:future"),
            json.dumps(record.to_dict()),
            False,
        )
        loaded = self.repo.load(MEDIUM, "medium:future")
        self.assertIsNotNone(loaded)
        self.assertTrue(loaded.read_only)
        self.assertEqual(loaded.payload["count"], 4096)

    def test_writing_a_read_only_record_is_refused(self):
        record = Record(MEDIUM, "medium:future", payload={"count": 4096}, schema_version=99)
        self.backend.set(
            record_key(MEDIUM, "medium:future"),
            json.dumps(record.to_dict()),
            False,
        )
        loaded = self.repo.load(MEDIUM, "medium:future")
        with self.assertRaises(ValueError):
            self.repo.write(loaded)

    def test_old_version_is_migrated_on_load(self):
        migrator = Migrator(target_version=2)
        migrator.register(MEDIUM, 1, lambda payload: dict(payload, upgraded=True))
        repo = ExtraDataRepository(self.backend, migrator=migrator)
        record = Record(MEDIUM, "medium:old", payload={"n": 1}, schema_version=1)
        self.backend.set(
            record_key(MEDIUM, "medium:old"),
            json.dumps(record.to_dict()),
            False,
        )
        loaded = repo.load(MEDIUM, "medium:old")
        self.assertEqual(loaded.schema_version, 2)
        self.assertTrue(loaded.payload["upgraded"])

    def test_unknown_version_reads_are_counted(self):
        record = Record(MEDIUM, "medium:future", schema_version=99)
        self.backend.set(
            record_key(MEDIUM, "medium:future"),
            json.dumps(record.to_dict()),
            False,
        )
        self.repo.load(MEDIUM, "medium:future")
        self.assertEqual(self.repo.diagnostics()["unknown_version_skips"], 1)


class TestBothBackendsAgree(unittest.TestCase):
    """内存替身与真实后端必须给出**同一个**版本结论。

    两份实现分叉的表现是「离线测试保留、真机覆盖」——那是最坏的一种。
    """

    def _load_with(self, repo, record):
        repo._store.setdefault(MEDIUM, {})[record.record_id] = record
        return repo.load(MEDIUM, record.record_id)

    def test_read_only_verdict_matches(self):
        future = Record(MEDIUM, "m-future", payload={"n": 1}, schema_version=99)
        memory = MemoryRepository()
        self.assertTrue(self._load_with(memory, future).read_only)

        backend = testsupport.MemoryExtraDataBackend()
        repo = ExtraDataRepository(backend)
        backend.set(record_key(MEDIUM, "m-future"), json.dumps(Record(
            MEDIUM, "m-future", payload={"n": 1}, schema_version=99
        ).to_dict()), False)
        self.assertTrue(repo.load(MEDIUM, "m-future").read_only)


if __name__ == "__main__":
    unittest.main()
