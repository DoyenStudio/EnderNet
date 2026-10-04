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

import json

from .migrations import (
    CURRENT_SCHEMA_VERSION,
    Migrator,
    UnsupportedVersionError,
)

try:
    _TEXT_TYPES = (str, unicode)  # noqa: F821
except NameError:
    _TEXT_TYPES = (str, bytes)


# -- 版本闸门（所有后端共用一处）------------------------------------------


def migrate_or_preserve(record, migrator):
    # type: (Record, Migrator) -> bool
    """就地把记录升到当前版本。返回**是否因版本过新而被保留**。

    版本过新时置 `read_only` 并**原样返回同一个对象**——绝不返回 None：
    那会被上层当成「没有数据」并写回空值，直接销毁玩家的东西（12 验收
    单列的一条）。

    抽成模块级函数而不是留在某个后端的 `load` 里，是因为 `MemoryRepository`
    与 `ExtraDataRepository` 必须给出**同一个**版本结论。两份实现迟早会分叉，
    而分叉的表现是「离线测试保留、真机覆盖」。
    """
    if not migrator.needs_migration(record.kind, record.schema_version):
        return False
    try:
        new_version, payload = migrator.migrate(
            record.kind, record.schema_version, record.payload
        )
    except UnsupportedVersionError:
        record.read_only = True
        return True
    record.schema_version = new_version
    record.payload = payload
    record.touch()
    return False


def require_writable(record, migrator):
    # type: (Record, Migrator) -> None
    """写入前的两道闸。顺序有意——报错更具体的那条在前。

    `read_only` 是**读取阶段已经确认过的结论**（「我看不懂它」），
    比「版本号更高」更具体；先报它，上层才分得清两种情况。
    """
    if record.read_only:
        raise ValueError(
            "记录 %s/%s 因版本过新而为只读，不得写入" % (record.kind, record.record_id)
        )
    if record.schema_version > migrator.target_version:
        raise UnsupportedVersionError(
            record.kind, record.schema_version, migrator.target_version
        )


#: 存档后端里记录键的前缀。`(kind, record_id)` → `endernet:<kind>/<record_id>`，
#: 与 12-02 交付说明选的形状一致（一块介质一个键）。
RECORD_KEY_PREFIX = "endernet:"

#: 每类记录一张索引表。**引擎的 ExtraData 没有键枚举接口**，`list_ids` 只能
#: 靠它；见 `persistence_backend.md` 与 12-02 交付说明。
INDEX_KEY_PREFIX = "endernet:index/"


def record_key(kind, record_id):
    # type: (str, str) -> str
    return "%s%s/%s" % (RECORD_KEY_PREFIX, kind, record_id)


def index_key(kind):
    # type: (str) -> str
    return "%s%s" % (INDEX_KEY_PREFIX, kind)


def dumps_payload(payload):
    # type: (dict) -> str
    """把载荷编成写进后端的字符串。

    **存字符串而不是嵌套字典**，尽管 ExtraData 号称「支持 python 基本数据类型」：
    12-02 只实测过**字符串**的字节级往返（§3.1），嵌套 dict 的 NBT 往返行为
    **没有测过**。而 `userData` 那边同一个引擎会把嵌套结构包成
    `{"__type__": T, "__value__": V}`（`resource_identity.md` §2.1）——
    在这里赌它不包装是不必要的风险。

    `ensure_ascii=True` 只保证内容为 ASCII；py2 仍可能返回 unicode，
    必须显式编码成 str 才能交给 ExtraData。
    """
    text = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    if not isinstance(text, str):  # pragma: no cover - py2 unicode
        return text.encode("ascii")
    return text


def loads_payload(text):
    # type: (object) -> dict
    """`dumps_payload` 的逆。读不出结构就抛——**不返回空字典**。

    「读不懂」与「没有数据」是两件事：返回 `{}` 会被上层当成一份合法的空载荷
    写回去（同 `resources.py` 的原样保留纪律）。
    """
    if isinstance(text, bytes) and not isinstance(text, str):  # pragma: no cover - py3
        text = text.decode("utf-8")
    if not isinstance(text, _TEXT_TYPES):
        raise TypeError("存档载荷必须是字符串，得到 %r" % (type(text),))
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("存档载荷必须是对象，得到 %r" % (type(payload),))
    return payload


def dumps_index(ids):
    # type: (object) -> str
    """索引表（一串 record_id）编成字符串。"""
    text = json.dumps(sorted(ids), ensure_ascii=True)
    if not isinstance(text, str):  # pragma: no cover - py2 unicode
        return text.encode("ascii")
    return text


def loads_index(text):
    # type: (object) -> list
    if isinstance(text, bytes) and not isinstance(text, str):  # pragma: no cover - py3
        text = text.decode("utf-8")
    if not isinstance(text, _TEXT_TYPES):
        raise TypeError("索引载荷必须是字符串，得到 %r" % (type(text),))
    ids = json.loads(text)
    if not isinstance(ids, list):
        raise ValueError("索引载荷必须是列表，得到 %r" % (type(ids),))
    return sorted(ids)


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

        if migrate_or_preserve(record, self.migrator):
            self._unknown_version_skips += 1
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
        require_writable(record, self.migrator)
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


class ExtraDataRepository(Repository):
    """真实存档后端（12-03）：一块记录一个键，每类一张索引表。

    **它自己不碰引擎。** 引擎访问被压进一个 `backend` 对象（四个原语：
    `get` / `set` / `delete` / `save`），真实实现是
    `server/adapters/persistence_engine.py` 的 `EngineExtraDataBackend`，
    离线测试给它一个字典替身即可。这样切有两个理由：

    1. `server/persistence/` 属 `DOMAIN_PREFIXES`，**全树禁止引擎导入**
       （`test_import_boundaries.py`），本模块放不下 `import mod.*`。
    2. 版本闸门、键映射、索引维护都是纯逻辑，脱机能验的部分应当脱机验完。

    ## 提交语义与内存后端不同

    引擎的 ExtraData 自己就是「待提交的内存区」：`set(..., auto_save=False)`
    只改内存，`SaveExtraData()` 才提交（`persistence_backend.md` §3.3）。
    因此 `write` 写进引擎的内存区，`commit` 才是那次唯一的提交调用。
    **没有待提交的改动时不调用 `save()`**——固定开销约 39.8 KB 是**按调用
    次数**付的（§3.2），空转帧不该付这笔钱。
    """

    def __init__(self, backend, migrator=None, commit_limit=None):
        # type: (object, Migrator, int) -> None
        self.backend = backend
        self.migrator = migrator if migrator is not None else Migrator()
        self.commit_limit = commit_limit
        self._write_attempts = 0
        self._write_count = 0
        self._deleted_count = 0
        self._unknown_version_skips = 0
        self._index_rewrites = 0
        #: 自上次提交以来有多少次写／删还没进盘。
        self._pending = 0
        self._save_calls = 0

    # -- Repository ----------------------------------------------------

    def load(self, kind, record_id):
        # type: (str, str) -> Record
        text = self.backend.get(record_key(kind, record_id))
        if text is None:
            return None
        payload = loads_payload(text)
        record = Record.from_dict(payload)
        # 键里的 (kind, record_id) 是权威：载荷里那两份只是副本，
        # 不一致说明有人在别处改过它，以键为准。
        record.kind = kind
        record.record_id = record_id
        record.dirty = False
        if migrate_or_preserve(record, self.migrator):
            self._unknown_version_skips += 1
        return record

    def list_ids(self, kind):
        # type: (str) -> list
        text = self.backend.get(index_key(kind))
        if text is None:
            # 从没存过这一类，与「存过但一条都不剩」同形——都是空。
            return []
        return loads_index(text)

    def write(self, record):
        # type: (Record) -> bool
        if not isinstance(record, Record):
            raise TypeError("write 需要 Record，得到 %r" % (type(record),))
        self._write_attempts += 1
        require_writable(record, self.migrator)
        # 索引先读后写：同一批里连写多条时，第二条要看得见第一条。
        self._add_to_index(record.kind, record.record_id)
        self.backend.set(
            record_key(record.kind, record.record_id),
            dumps_payload(record.to_dict()),
            False,
        )
        record.dirty = False
        self._write_count += 1
        self._pending += 1
        return True

    def delete(self, kind, record_id):
        # type: (str, str) -> bool
        existed = self.backend.get(record_key(kind, record_id)) is not None
        removed = self._remove_from_index(kind, record_id)
        if not existed and not removed:
            return False
        self.backend.delete(record_key(kind, record_id))
        self._deleted_count += 1
        self._pending += 1
        return True

    def commit(self, limit=None):
        # type: (int) -> int
        """提交。**只调一次 `SaveExtraData()`**，返回本次提交的记录数。

        没有待提交的改动时一次调用都不发。

        `limit` 在这里**没有意义**：引擎的提交是全量的、没有按条数分批的接口。
        「每帧最多写几条」由调用方在**写**的那一侧控（`WriteCoordinator`
        的批大小）——那才是省下固定开销的地方。
        """
        if self._pending <= 0:
            return 0
        written = self._pending
        self.backend.save()
        self._save_calls += 1
        self._pending = 0
        return written

    def flush_all(self):
        # type: () -> int
        return self.commit(limit=None)

    def dirty_count(self):
        # type: () -> int
        """还没进盘的改动数。**语义是「已写进引擎内存区」**，不是「没写」。"""
        return self._pending

    # -- 索引维护（`list_ids` 的唯一依据）-------------------------------

    def _add_to_index(self, kind, record_id):
        # type: (str, str) -> None
        known = self.list_ids(kind)
        if record_id in known:
            return
        known.append(record_id)
        self._write_index(kind, known)

    def _remove_from_index(self, kind, record_id):
        # type: (str, str) -> bool
        known = self.list_ids(kind)
        if record_id not in known:
            return False
        known.remove(record_id)
        self._write_index(kind, known)
        return True

    def _write_index(self, kind, ids):
        # type: (str, list) -> None
        self.backend.set(index_key(kind), dumps_index(ids), False)
        self._index_rewrites += 1

    # -- 诊断 ----------------------------------------------------------

    def diagnostics(self):
        # type: () -> dict
        return {
            "backend": type(self.backend).__name__,
            "write_attempts": self._write_attempts,
            "writes": self._write_count,
            "deletes": self._deleted_count,
            "index_rewrites": self._index_rewrites,
            "save_calls": self._save_calls,
            "pending": self._pending,
            "unknown_version_skips": self._unknown_version_skips,
            "migrator": self.migrator.diagnostics(),
        }


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
