# coding=utf-8
"""设备注册表：位置、区块与实例代次索引。

对应任务 01-04。

四条必须成立的性质：

1. **注册幂等。** 放置一次、引擎重复发事件、启动恢复重复补查，
   都只能产生一个设备实例与一个代次。
2. **维度隔离。** 两个维度的相同坐标是两个不同的设备，互不覆盖。
3. **代次单调。** 同位置换上不同方块（或先拆后放）会分配新代次；
   旧代次回来的延迟任务、请求、订阅因此无法匹配而被丢弃。
4. **卸载不是拆除。** 区块卸载只把记录标记为未加载并卸下运行时能力，
   保留记录与存档；不得执行掉落或删除记录。
"""
from __future__ import absolute_import, division, print_function

from ...common.identity import ChunkKey, DeviceRef, GenerationAllocator, NodeKey


class DeviceRecord(object):
    """一个已注册设备实例的运行时记录。"""

    __slots__ = (
        "ref",
        "device_id",
        "facing",
        "config",
        "loaded",
        "spawned_tick",
        "notes",
    )

    def __init__(self, ref, device_id, facing=None, config=None, spawned_tick=0):
        # type: (DeviceRef, str, object, dict, int) -> None
        self.ref = ref
        self.device_id = device_id
        self.facing = facing
        self.config = dict(config or {})
        #: 区块是否已加载。未加载时设备仍存在于存档与索引中。
        self.loaded = True
        self.spawned_tick = spawned_tick
        self.notes = ""

    @property
    def node_key(self):
        return self.ref.node_key

    @property
    def generation(self):
        return self.ref.generation

    @property
    def dimension(self):
        return self.ref.node_key.dimension

    def to_dict(self):
        # type: () -> dict
        return {
            "ref": self.ref.to_dict(),
            "device_id": self.device_id,
            "facing": self.facing,
            "config": dict(self.config),
            "loaded": self.loaded,
            "spawned_tick": self.spawned_tick,
        }

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> DeviceRecord
        record = cls(
            DeviceRef.from_dict(data["ref"]),
            data["device_id"],
            facing=data.get("facing"),
            config=data.get("config"),
            spawned_tick=data.get("spawned_tick", 0),
        )
        record.loaded = bool(data.get("loaded", True))
        return record

    def __repr__(self):
        return "DeviceRecord(%s @ %r gen=%d%s)" % (
            self.device_id,
            self.node_key,
            self.generation,
            "" if self.loaded else ", unloaded",
        )


class RegisterOutcome(object):
    """注册结果。`created=False` 表示这是一次重复事件，不应重复派发工作。"""

    __slots__ = ("record", "created", "replaced")

    def __init__(self, record, created, replaced=None):
        # type: (DeviceRecord, bool, DeviceRecord) -> None
        self.record = record
        self.created = created
        #: 被同位置新方块顶替掉的旧记录（若有）
        self.replaced = replaced

    def __bool__(self):
        return self.created

    __nonzero__ = __bool__

    def __repr__(self):
        return "RegisterOutcome(%r, created=%s)" % (self.record, self.created)


class DeviceRegistry(object):
    """按位置、区块与代次索引设备记录。"""

    def __init__(self, definitions, allocator=None):
        # type: (object, GenerationAllocator) -> None
        self.definitions = definitions
        self.allocator = allocator if allocator is not None else GenerationAllocator()
        self._by_node = {}
        #: ChunkKey -> set(NodeKey)
        self._by_chunk = {}
        #: 未加载但仍保留在索引中的记录
        self._unloaded = set()
        self.duplicate_events = 0
        self.replacements = 0

    # -- 注册 / 注销 ---------------------------------------------------

    def register(self, node_key, device_id, facing=None, config=None, tick=0):
        # type: (NodeKey, str, object, dict, int) -> RegisterOutcome
        """注册一个设备实例。重复调用是安全的。"""
        self.definitions.get(device_id)  # 未注册的标识立刻报错，不静默接受

        existing = self._by_node.get(node_key)
        if existing is not None and existing.device_id == device_id:
            # 同一位置同一方块的重复事件（放置事件叠加恢复补查等）。
            self.duplicate_events += 1
            # 重新加载：把记录标回已加载，但不分配新代次——
            # 方块实例没变，旧任务仍然有效。
            if not existing.loaded:
                existing.loaded = True
                self._unloaded.discard(node_key)
            return RegisterOutcome(existing, False)

        replaced = None
        if existing is not None:
            # 同位置换了别的方块：旧实例就此终结。
            replaced = self._drop(node_key)
            self.replacements += 1

        generation = self.allocator.allocate(node_key)
        record = DeviceRecord(
            DeviceRef(node_key, generation),
            device_id,
            facing=facing,
            config=config,
            spawned_tick=tick,
        )
        self._by_node[node_key] = record
        self._by_chunk.setdefault(node_key.chunk_key(), set()).add(node_key)
        return RegisterOutcome(record, True, replaced)

    def unregister(self, node_key):
        # type: (NodeKey) -> DeviceRecord
        """彻底移除该位置的设备（真实拆除）。"""
        record = self._by_node.get(node_key)
        if record is None:
            return None
        return self._drop(node_key)

    def unregister_matching(self, node_key, device_id):
        # type: (NodeKey, str) -> DeviceRecord
        """仅当该位置的方块种类与事件一致时才移除。

        引擎的移除事件只携带位置与方块名，不带实例代次。
        若该位置已经被另一种方块顶替，这次（迟到的）移除必须无效，
        否则会删掉刚放下的新设备。
        """
        record = self._by_node.get(node_key)
        if record is None or record.device_id != device_id:
            return None
        return self._drop(node_key)

    def unregister_if_generation(self, ref):
        # type: (DeviceRef) -> DeviceRecord
        """按实例代次移除。

        拆除事件可能是延迟到达的：若该位置已经被新方块占用，
        代次对不上，这次移除**必须无效**，否则会删掉刚放下的新设备。
        """
        record = self._by_node.get(ref.node_key)
        if record is None:
            return None
        if record.generation != ref.generation:
            return None
        return self._drop(ref.node_key)

    def _drop(self, node_key):
        # type: (NodeKey) -> DeviceRecord
        record = self._by_node.pop(node_key, None)
        if record is None:
            return None
        bucket = self._by_chunk.get(node_key.chunk_key())
        if bucket is not None:
            bucket.discard(node_key)
            if not bucket:
                del self._by_chunk[node_key.chunk_key()]
        self._unloaded.discard(node_key)
        return record

    # -- 加载 / 卸载 ---------------------------------------------------

    def mark_loaded(self, node_key):
        # type: (NodeKey) -> bool
        record = self._by_node.get(node_key)
        if record is None or record.loaded:
            return False
        record.loaded = True
        self._unloaded.discard(node_key)
        return True

    def mark_unloaded(self, node_key):
        # type: (NodeKey) -> bool
        """区块卸载：保留记录，只卸下运行时能力。

        **不得**执行拆除掉落，也不得删除记录——重新加载时要恢复同一个实例。
        """
        record = self._by_node.get(node_key)
        if record is None or not record.loaded:
            return False
        record.loaded = False
        self._unloaded.add(node_key)
        return True

    def unload_chunk(self, chunk_key):
        # type: (ChunkKey) -> list
        """卸载整个区块，返回受影响的位置列表。"""
        affected = []
        for node_key in sorted(
            self._by_chunk.get(chunk_key, set()), key=lambda k: k.as_key()
        ):
            if self.mark_unloaded(node_key):
                affected.append(node_key)
        return affected

    def is_loaded(self, node_key):
        # type: (NodeKey) -> bool
        record = self._by_node.get(node_key)
        return bool(record is not None and record.loaded)

    # -- 查询 ----------------------------------------------------------

    def get(self, node_key):
        # type: (NodeKey) -> DeviceRecord
        return self._by_node.get(node_key)

    def get_by_ref(self, ref):
        # type: (DeviceRef) -> DeviceRecord
        """按实例代次取记录。代次不符返回 None——这是延迟任务的失效检查。"""
        record = self._by_node.get(ref.node_key)
        if record is None or record.generation != ref.generation:
            return None
        return record

    def matches(self, ref):
        # type: (DeviceRef) -> bool
        return self.get_by_ref(ref) is not None

    def matches_key(self, node_key):
        # type: (NodeKey) -> bool
        """该位置是否有已注册的设备（不区分是否已加载）。"""
        return node_key in self._by_node

    def records_in_chunk(self, chunk_key):
        # type: (ChunkKey) -> list
        return [
            self._by_node[key]
            for key in sorted(self._by_chunk.get(chunk_key, set()), key=lambda k: k.as_key())
        ]

    def all_records(self):
        # type: () -> list
        return [self._by_node[key] for key in sorted(self._by_node.keys(), key=lambda k: k.as_key())]

    def device_ids(self):
        # type: () -> set
        return set(record.device_id for record in self._by_node.values())

    def count(self):
        # type: () -> int
        return len(self._by_node)

    def loaded_records(self):
        # type: () -> list
        return [record for record in self.all_records() if record.loaded]

    def controller_records(self):
        # type: () -> list
        """全部控制器实例，**含未加载的**。

        拓扑必须知道「有一个控制器但暂时不可访问」，
        不能因为区块没加载就把它当成已经拆掉（架构规划 6.1）。
        """
        return [
            record
            for record in self.all_records()
            if self.definitions.is_controller(record.device_id)
        ]

    def stats(self):
        # type: () -> dict
        return {
            "devices": len(self._by_node),
            "chunks": len(self._by_chunk),
            "unloaded": len(self._unloaded),
            "duplicate_events": self.duplicate_events,
            "replacements": self.replacements,
        }
