# coding=utf-8
"""位置、区块与设备实例身份。

对应任务 01-01/01-04 的身份部分。这些是纯值对象，不依赖任何引擎接口，
被注册表、拓扑、调度、能源与同步共同使用，因此放在 common 层。

**实例代次（generation）是这里的重点。** 同一个坐标上先后存在过两个方块
是常态。延迟任务、订阅与请求都携带代次；代次对不上就必须整体作废，
否则「旧方块的到期任务」会在新方块上执行。

架构规划 5.1 把这一层记为 `NodeKey = (dimension, x, y, z)`：
`NodeKey` 定位世界位置，`DeviceRef` 再附加实例代次。
"""

try:  # pragma: no cover - 仅在 Python 2 下成立
    _INT_TYPES = (int, long)
except NameError:  # pragma: no cover - 仅在 Python 3 下成立
    _INT_TYPES = (int,)

#: 六个面朝向的偏移量，顺序固定，供邻接查询使用。
FACING_OFFSETS = (
    (0, 1, 0),   # up
    (0, -1, 0),  # down
    (0, 0, -1),  # north
    (0, 0, 1),   # south
    (-1, 0, 0),  # west
    (1, 0, 0),   # east
)

FACING_NAMES = ("up", "down", "north", "south", "west", "east")

#: 单个区块的边长
CHUNK_SIZE = 16


def _require_int(value, label):
    # type: (object, str) -> int
    if isinstance(value, bool) or not isinstance(value, _INT_TYPES):
        raise TypeError("%s 必须是整数，得到 %r" % (label, value))
    return int(value)


class NodeKey(object):
    """一个世界位置 `(dimension, x, y, z)`。

    不同维度的相同坐标是两个不同的节点——这是节点索引必须分维度维护的原因。
    """

    __slots__ = ("dimension", "x", "y", "z")

    def __init__(self, dimension, x, y, z):
        # type: (int, int, int, int) -> None
        self.dimension = _require_int(dimension, "dimension")
        self.x = _require_int(x, "x")
        self.y = _require_int(y, "y")
        self.z = _require_int(z, "z")

    @classmethod
    def from_tuple(cls, dimension, position):
        # type: (int, tuple) -> NodeKey
        return cls(dimension, position[0], position[1], position[2])

    def as_tuple(self):
        # type: () -> tuple
        return (self.x, self.y, self.z)

    def as_key(self):
        # type: () -> tuple
        """`(dimension, x, y, z)`，用于字典键。"""
        return (self.dimension, self.x, self.y, self.z)

    def chunk_key(self):
        # type: () -> ChunkKey
        return ChunkKey(
            self.dimension,
            self.x // CHUNK_SIZE,
            self.z // CHUNK_SIZE,
        )

    def offset(self, dx, dy, dz):
        # type: (int, int, int) -> NodeKey
        return NodeKey(self.dimension, self.x + dx, self.y + dy, self.z + dz)

    def neighbors(self):
        # type: () -> tuple
        """六个面相邻的位置，顺序与 `FACING_NAMES` 一致。"""
        return tuple(
            NodeKey(self.dimension, self.x + dx, self.y + dy, self.z + dz)
            for dx, dy, dz in FACING_OFFSETS
        )

    def to_dict(self):
        # type: () -> dict
        return {"dimension": self.dimension, "x": self.x, "y": self.y, "z": self.z}

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> NodeKey
        return cls(data["dimension"], data["x"], data["y"], data["z"])

    def __eq__(self, other):
        if not isinstance(other, NodeKey):
            return NotImplemented
        return self.as_key() == other.as_key()

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return hash(self.as_key())

    def __repr__(self):
        return "NodeKey(dim=%d, %d, %d, %d)" % (
            self.dimension,
            self.x,
            self.y,
            self.z,
        )


class ChunkKey(object):
    """区块身份 `(dimension, chunk_x, chunk_z)`。"""

    __slots__ = ("dimension", "chunk_x", "chunk_z")

    def __init__(self, dimension, chunk_x, chunk_z):
        # type: (int, int, int) -> None
        self.dimension = _require_int(dimension, "dimension")
        self.chunk_x = _require_int(chunk_x, "chunk_x")
        self.chunk_z = _require_int(chunk_z, "chunk_z")

    def as_key(self):
        # type: () -> tuple
        return (self.dimension, self.chunk_x, self.chunk_z)

    def to_dict(self):
        # type: () -> dict
        return {
            "dimension": self.dimension,
            "chunk_x": self.chunk_x,
            "chunk_z": self.chunk_z,
        }

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> ChunkKey
        return cls(data["dimension"], data["chunk_x"], data["chunk_z"])

    def __eq__(self, other):
        if not isinstance(other, ChunkKey):
            return NotImplemented
        return self.as_key() == other.as_key()

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return hash(self.as_key())

    def __repr__(self):
        return "ChunkKey(dim=%d, %d, %d)" % (
            self.dimension,
            self.chunk_x,
            self.chunk_z,
        )


class DeviceRef(object):
    """`NodeKey` 加上实例代次，指向**某一次**存在的方块实例。

    代次使延迟任务具备自毁能力：方块被拆除后同位置重新放置会得到更大的代次，
    旧任务带着旧代次回来时无法匹配，直接丢弃。
    """

    __slots__ = ("node_key", "generation")

    def __init__(self, node_key, generation):
        # type: (NodeKey, int) -> None
        if not isinstance(node_key, NodeKey):
            raise TypeError("node_key 必须是 NodeKey，得到 %r" % (type(node_key),))
        self.node_key = node_key
        self.generation = _require_int(generation, "generation")

    def as_key(self):
        # type: () -> tuple
        return (self.node_key.as_key(), self.generation)

    def matches(self, other):
        # type: (object) -> bool
        """代次与位置都相同才算同一个实例。"""
        if not isinstance(other, DeviceRef):
            return False
        return self.as_key() == other.as_key()

    def to_dict(self):
        # type: () -> dict
        return {
            "node_key": self.node_key.to_dict(),
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> DeviceRef
        return cls(NodeKey.from_dict(data["node_key"]), data["generation"])

    def __eq__(self, other):
        if not isinstance(other, DeviceRef):
            return NotImplemented
        return self.as_key() == other.as_key()

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return hash(self.as_key())

    def __repr__(self):
        return "DeviceRef(%r, gen=%d)" % (self.node_key, self.generation)


class GenerationAllocator(object):
    """按位置分配单调递增的实例代次。

    代次只增不减，且不持久化：重启后从 0 重新开始也不会误判，
    因为旧代次的任务本来就不会跨重启存活。
    """

    __slots__ = ("_next_by_node",)

    def __init__(self):
        self._next_by_node = {}

    def allocate(self, node_key):
        # type: (NodeKey) -> int
        generation = self._next_by_node.get(node_key.as_key(), 0)
        self._next_by_node[node_key.as_key()] = generation + 1
        return generation

    def current(self, node_key):
        # type: (NodeKey) -> int
        """最近一次分配出去的代次；从未分配过时为 -1。"""
        return self._next_by_node.get(node_key.as_key(), 0) - 1

    def forget(self, node_key):
        # type: (NodeKey) -> None
        """丢弃该位置的计数器。仅用于方块彻底消失且不打算复用时。"""
        self._next_by_node.pop(node_key.as_key(), None)

    def __len__(self):
        return len(self._next_by_node)
