# coding=utf-8
"""存档仓库契约与内存替身。

对应任务 12-01/12-02。

- `Repository`：存档边界。真实后端（方块实体数据 / ExtraData / 自定义存档）
  由后续任务接入；本批次只交付契约、内存替身与故障注入。
- `MemoryRepository`：测试替身，同时也是 14 的故障注入入口。
  它可以按操作的「第几次」定点失败，用来验证恢复流程不会盲目重放。
- `WriteCoordinator`：把脏记录合并成批量提交。

刻意**不在这里**调用任何引擎接口：选型依赖真实 SDK 能力验证（12-02 未完成），
`ExtraDataRepository` 之类不得在未验证的情况下接线。
"""

from .migrations import (
    CURRENT_SCHEMA_VERSION,
    Migrator,
    UnsupportedVersionError,
)


class Record(object):
    """一份带版本与归属的权威数据。

    - `record_id` 稳定且全局唯一，不得使用会随运行时变化的东西（如网络编号）。
    - `owner_id` 说明「谁拥有它」。订单与预留绑定的是**控制器实例 ID**，
      这样两个网络连通时不会把别人的订单并过来（架构规划 6.5）。
    """

    __slots__ = (
        "kind",
        "record_id",
        "schema_version",
        "owner_id",
        "payload",
        "revision",
        "dirty",
        "read_only",
    )

    def __init__(
        self,
        kind,
        record_id,
        payload=None,
        schema_version=CURRENT_SCHEMA_VERSION,
        owner_id=None,
    ):
        # type: (str, str, dict, int, str) -> None
        if not kind:
            raise ValueError("记录必须有类型")
        if not record_id:
            raise ValueError("记录必须有稳定 ID")
        self.kind = kind
        self.record_id = record_id
        self.schema_version = int(schema_version)
        self.owner_id = owner_id
        self.payload = dict(payload or {})
        self.revision = 0
        self.dirty = False
        #: 版本过新而无法理解时置位。此类记录只能原样保留，不得覆盖。
        self.read_only = False

    def touch(self, payload=None):
        # type: (dict) -> Record
        """标记为脏。返回自身以便链式调用。"""
        if payload is not None:
            self.payload = dict(payload)
        self.revision += 1
        self.dirty = True
        return self

    def to_dict(self):
        # type: () -> dict
        return {
            "kind": self.kind,
            "record_id": self.record_id,
            "schema_version": self.schema_version,
            "owner_id": self.owner_id,
            "payload": dict(self.payload),
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> Record
        record = cls(
            data["kind"],
            data["record_id"],
            payload=data.get("payload"),
            schema_version=data.get("schema_version", CURRENT_SCHEMA_VERSION),
            owner_id=data.get("owner_id"),
        )
        record.revision = int(data.get("revision", 0))
        return record

    def __repr__(self):
        return "Record(%s/%s, v%s)" % (self.kind, self.record_id, self.schema_version)


class Repository(object):
    """存档边界。后端实现可以是内存、ExtraData 或方块实体数据。"""

    def load(self, kind, record_id):
        # type: (str, str) -> Record
        """读取。不存在返回 None。版本过新时返回 `read_only` 的记录。"""
        raise NotImplementedError

    def list_ids(self, kind):
        # type: (str) -> list
        raise NotImplementedError

    def write(self, record):
        # type: (Record) -> bool
        """写入一条记录。返回是否真的写了。"""
        raise NotImplementedError

    def delete(self, kind, record_id):
        # type: (str, str) -> bool
        raise NotImplementedError

    def commit(self, limit=None):
        # type: (int) -> int
        """提交脏记录，返回提交条数。"""
        raise NotImplementedError

    def flush_all(self):
        # type: () -> int
        return self.commit(limit=None)

    def diagnostics(self):
        # type: () -> dict
        return {}


class FaultInjection(object):
    """定点失败规则。

    `op` 取 'write' / 'delete' / 'load'；`kind`、`record_id` 为 None 表示不限定。
    `times` 是该规则还能触发几次。
    """

    __slots__ = ("op", "kind", "record_id", "times", "exception_factory", "note")

    def __init__(self, op, kind=None, record_id=None, times=1, exception_factory=None, note=""):
        self.op = op
        self.kind = kind
        self.record_id = record_id
        self.times = times
        self.exception_factory = exception_factory or (
            lambda: IOError("注入的存档故障: %s" % op)
        )
        self.note = note

    def matches(self, op, kind, record_id):
        # type: (str, str, str) -> bool
        if self.times <= 0 or self.op != op:
            return False
        if self.kind is not None and self.kind != kind:
            return False
        if self.record_id is not None and self.record_id != record_id:
            return False
        return True

    def consume(self):
        self.times -= 1


class MemoryRepository(Repository):
    """内存仓库：测试替身 + 故障注入入口。

    `commit` 只写脏记录并遵守 `limit`，因此可以用来验证
    「关键交接按后端能力提交、非关键脏写合并」（12-08）。
    """

    def __init__(self, migrator=None, commit_limit=None):
        # type: (Migrator, int) -> None
        self.migrator = migrator if migrator is not None else Migrator()
        self.commit_limit = commit_limit
        #: kind -> {record_id: Record}
        self._store = {}
        self._faults = []
        self._write_attempts = 0
        self._write_count = 0
        self._deleted_count = 0
        self._unknown_version_skips = 0

    # -- 故障注入 ------------------------------------------------------

    def inject_fault(self, op, kind=None, record_id=None, times=1, exception_factory=None, note=""):
        # type: (str, str, str, int, object, str) -> FaultInjection
        fault = FaultInjection(op, kind, record_id, times, exception_factory, note)
        self._faults.append(fault)
        return fault

    def clear_faults(self):
        # type: () -> None
        self._faults = []

    def _maybe_fail(self, op, kind, record_id):
        for fault in self._faults:
            if fault.matches(op, kind, record_id):
                fault.consume()
                raise fault.exception_factory()

    # -- Repository ----------------------------------------------------

    def load(self, kind, record_id):
        # type: (str, str) -> Record
        self._maybe_fail("load", kind, record_id)
        record = self._store.get(kind, {}).get(record_id)
        if record is None:
            return None

        if self.migrator.needs_migration(kind, record.schema_version):
            try:
                new_version, payload = self.migrator.migrate(
                    kind, record.schema_version, record.payload
                )
            except UnsupportedVersionError:
                # 看不懂的存档：原样返回并置只读，交由上层决定暂停该记录。
                # 绝不返回 None —— 那会被上层当成「没有数据」并写回空值。
                record.read_only = True
                self._unknown_version_skips += 1
                return record
            record.schema_version = new_version
            record.payload = payload
            record.touch()
        return record

    def list_ids(self, kind):
        # type: (str) -> list
        return sorted(self._store.get(kind, {}).keys())

    def write(self, record):
        # type: (Record) -> bool
        if not isinstance(record, Record):
            raise TypeError("write 需要 Record，得到 %r" % (type(record),))
        self._write_attempts += 1
        self._maybe_fail("write", record.kind, record.record_id)
        # 先看已知的只读标记，再按版本号兜底：两条路径都必须拒绝写入。
        # 顺序是有意的——read_only 是读取阶段就确认过的结论，报错更具体。
        if record.read_only:
            raise ValueError(
                "记录 %s/%s 因版本过新而为只读，不得写入" % (record.kind, record.record_id)
            )
        if record.schema_version > self.migrator.target_version:
            raise UnsupportedVersionError(
                record.kind, record.schema_version, self.migrator.target_version
            )
        self._store.setdefault(record.kind, {})[record.record_id] = record
        record.dirty = False
        self._write_count += 1
        return True

    def delete(self, kind, record_id):
        # type: (str, str) -> bool
        self._maybe_fail("delete", kind, record_id)
        bucket = self._store.get(kind)
        if not bucket or record_id not in bucket:
            return False
        del bucket[record_id]
        self._deleted_count += 1
        return True

    def commit(self, limit=None):
        # type: (int) -> int
        if limit is None:
            limit = self.commit_limit
        written = 0
        for kind in sorted(self._store.keys()):
            for record_id in sorted(self._store[kind].keys()):
                record = self._store[kind][record_id]
                if not record.dirty:
                    continue
                if limit is not None and written >= limit:
                    return written
                self.write(record)
                written += 1
        return written

    # -- 诊断 ----------------------------------------------------------

    def dirty_count(self):
        # type: () -> int
        total = 0
        for bucket in self._store.values():
            for record in bucket.values():
                if record.dirty:
                    total += 1
        return total

    def diagnostics(self):
        # type: () -> dict
        return {
            "kinds": sorted(self._store.keys()),
            "records": sum(len(b) for b in self._store.values()),
            "dirty": self.dirty_count(),
            "write_attempts": self._write_attempts,
            "writes": self._write_count,
            "deletes": self._deleted_count,
            "unknown_version_skips": self._unknown_version_skips,
            "pending_faults": sum(1 for f in self._faults if f.times > 0),
            "migrator": self.migrator.diagnostics(),
        }

    # -- 测试辅助 ------------------------------------------------------

    def snapshot_payloads(self):
        # type: () -> dict
        """`{kind: {record_id: payload}}`，用于「保存重进后一致」的断言。"""
        out = {}
        for kind, bucket in self._store.items():
            out[kind] = dict((rid, dict(rec.payload)) for rid, rec in bucket.items())
        return out


class WriteCoordinator(object):
    """把脏记录合并成批量提交。

    对应 12-08「合并非关键脏写」。每帧提交上限由预算决定，
    剩余脏记录留到下一帧，不会因为一次大保存卡住服务端主线程。
    """

    def __init__(self, repository, batch_size=64, key_records=()):
        # type: (Repository, int, tuple) -> None
        self.repository = repository
        self.batch_size = int(batch_size)
        #: 需要立即提交的关键记录 `(kind, record_id)`，不参与合并。
        self.key_records = set(key_records)
        self.commits = 0
        self.deferred = 0

    def mark_key(self, kind, record_id):
        # type: (str, str) -> None
        self.key_records.add((kind, record_id))

    def unmark_key(self, kind, record_id):
        # type: (str, str) -> None
        self.key_records.discard((kind, record_id))

    def commit_key(self, record):
        # type: (Record) -> bool
        """关键交接单独提交，不等待合并窗口。"""
        self.commits += 1
        return self.repository.write(record)

    def commit(self, budget_size=None):
        # type: (int) -> int
        size = self.batch_size if budget_size is None else int(budget_size)
        written = self.repository.commit(limit=size)
        self.commits += 1
        remaining = self.repository.dirty_count()
        self.deferred = remaining
        return written

    def diagnostics(self):
        # type: () -> dict
        return {
            "batch_size": self.batch_size,
            "commits": self.commits,
            "deferred": self.deferred,
            "key_records": len(self.key_records),
        }
