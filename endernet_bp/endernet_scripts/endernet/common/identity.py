# coding=utf-8
"""位置、区块与设备实例身份。

对应任务 01-01/01-04 的身份部分。这些是纯值对象，不依赖任何引擎接口，
被注册表、拓扑、调度、能源与同步共同使用，因此放在 common 层。

**实例代次（generation）是这里的重点。** 同一个坐标上先后存在过两个方块
是常态。延迟任务、订阅与请求都携带代次；代次对不上就必须整体作废，
否则「旧方块的到期任务」会在新方块上执行。

架构规划 5.1 把这一层记为 `NodeKey = (dimension, x, y, z)`：
`NodeKey` 定位世界位置，`DeviceRef` 再附加实例代次。

2026-11-09 网络改版（`.claude/13` §7）加了 `PartKey = NodeKey + 槽`：光缆一格
最多挂 6 块面板，「一格一设备」不再成立。`NodeKey` 本身不变，旧接口拿到的
`NodeKey` 一律按 center 槽解释，见 `PartKey` 的说明。
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


def opposite_face(face):
    # type: (str) -> str
    """对面：上 ↔ 下、北 ↔ 南、西 ↔ 东（`FACING_NAMES` 两两相邻成对）。"""
    return FACING_NAMES[FACING_NAMES.index(face) ^ 1]


def face_mask(faces):
    # type: (object) -> int
    """面名集合 → 6 位整数掩码，第 i 位对应 `FACING_NAMES[i]`。

    方块实体 `endernet:cuts`（被扳手切断的面，13 §2）就是这个掩码。
    未知面名直接报错：静默丢掉一个写错的面，等于让那一面悄悄连回去。
    """
    mask = 0
    for face in faces:
        if face not in FACING_NAMES:
            raise ValueError("未知的面 %r（可用：%s）" % (face, ", ".join(FACING_NAMES)))
        mask |= 1 << FACING_NAMES.index(face)
    return mask


def faces_of_mask(mask):
    # type: (int) -> frozenset
    """6 位整数掩码 → 面名集合。`face_mask` 的逆。超出 6 位的值报错。"""
    value = _require_int(mask, "mask")
    if value < 0 or value >= 1 << len(FACING_NAMES):
        raise ValueError("面掩码只有 6 位，得到 %r" % (mask,))
    return frozenset(
        name for index, name in enumerate(FACING_NAMES) if value & (1 << index)
    )


#: 单个区块的边长
CHUNK_SIZE = 16


def node_key_text(node_key):
    # type: (NodeKey) -> str
    """`NodeKey` 的存档文本 `"维度,x,y,z"`。**位置才是稳定身份**。

    存档记录的主键用它，而不是 `DeviceRef`：实例代次不持久
    （`GenerationAllocator` 重启后从 0 重来），拿它当键会让「重进世界」
    变成「查不到」。驱动器槽位、接口状态、网络记录都按这个键落盘。

    抽到 common 层是因为它现在有**三个引用方**（驱动器、接口、网络记录），
    各写一份的代价是其中一份改了分隔符而其余两份照旧——那会让重进世界时
    只有一半的设备找得回自己的记录。
    """
    dimension, x, y, z = node_key.as_key()
    return "%d,%d,%d,%d" % (dimension, x, y, z)


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

    def neighbor(self, face):
        # type: (str) -> NodeKey
        """`face` 那一面相邻的位置（面名取 `FACING_NAMES`）。"""
        dx, dy, dz = FACING_OFFSETS[FACING_NAMES.index(face)]
        return NodeKey(self.dimension, self.x + dx, self.y + dy, self.z + dz)

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


#: 一格里的槽（13 §7）：`center` 是线芯，也是整方块设备唯一的槽；其余是六个面。
SLOT_CENTER = "center"
PART_SLOTS = (SLOT_CENTER,) + FACING_NAMES

#: 面板槽的存档文本里，坐标与面名之间的分隔符（见 `PartKey`）。
_PART_TEXT_SEPARATOR = "@"


class PartKey(object):
    """`NodeKey` + 槽：一格里的某一个部件（13 §7）。

    光缆一格最多挂 6 块面板（每面一块），线芯本身占 `center` 槽。整方块设备
    只有 `center` 这一个槽，因此**旧接口拿到的 `NodeKey` 一律按 center 解释**：

    - `PartKey(k) == k` 且 `hash(PartKey(k)) == hash(k)`：同一个字典或集合里，
      `NodeKey` 与 center 槽的 `PartKey` 是同一个键，旧代码继续拿 `NodeKey`
      查表不受影响；面板槽（六个面）与任何 `NodeKey` 都不相等。
    - `PartKey.of(key)` 把 `NodeKey` 或 `PartKey` 规范成 `PartKey`。
    - `as_key()`：center 槽与 `NodeKey.as_key()` 相同，面板槽在末尾多一个面名，
      因此两种键混在一起按 `as_key()` 排序也是确定的。

    **存档文本**（`to_text` / `from_text`，持久化要用）：

    - center 槽：`"维度,x,y,z"`，与 `node_key_text` **逐字相同**——旧存档里
      按位置存的键直接当 center 槽读，不需要迁移；
    - 面板槽：`"维度,x,y,z@面名"`，面名取 `FACING_NAMES`，例如
      `"0,12,64,-3@north"`；
    - center 槽**不写** `@center` 后缀（读到会报错）：同一个部件只有一种写法，
      否则存档里会出现两个键指向同一块面板。

    拓扑与准入里面板节点挂在宿主光缆节点下（阶段 A）；会话、存档、能源与设备状态
    按它记账（阶段 D，设备实例引用见 `DeviceRef.slot` / `DeviceRef.part_key`）。
    """

    __slots__ = ("node_key", "slot")

    def __init__(self, node_key, slot=SLOT_CENTER):
        # type: (NodeKey, str) -> None
        if not isinstance(node_key, NodeKey):
            raise TypeError("node_key 必须是 NodeKey，得到 %r" % (type(node_key),))
        if slot not in PART_SLOTS:
            raise ValueError("未知的槽 %r（可用：%s）" % (slot, ", ".join(PART_SLOTS)))
        self.node_key = node_key
        self.slot = slot

    @classmethod
    def of(cls, key):
        # type: (object) -> PartKey
        """`NodeKey` → center 槽；`PartKey` 原样返回。"""
        if isinstance(key, PartKey):
            return key
        return cls(key)

    def is_center(self):
        # type: () -> bool
        return self.slot == SLOT_CENTER

    def front_key(self):
        # type: () -> object
        """面板**正面所对**的那一格（面板骨架，2026-11-10）：挂在 `slot` 面上的面板，正面朝
        这一面的外侧，对着的就是这一面相邻的格子。center 槽没有正面，返回 None。

        AE2 的 `IPart` 靠所在的面（`getSide`）找它要操作的方块（总线、P2P 等），这里同一口径。
        """
        if self.slot == SLOT_CENTER:
            return None
        return self.node_key.neighbor(self.slot)

    def as_key(self):
        # type: () -> tuple
        """字典键。center 槽等于 `NodeKey.as_key()`，面板槽多一个面名。"""
        if self.slot == SLOT_CENTER:
            return self.node_key.as_key()
        return self.node_key.as_key() + (self.slot,)

    def to_text(self):
        # type: () -> str
        """存档文本，格式见类说明。"""
        text = node_key_text(self.node_key)
        if self.slot == SLOT_CENTER:
            return text
        return text + _PART_TEXT_SEPARATOR + self.slot

    @classmethod
    def from_text(cls, text):
        # type: (str) -> PartKey
        """`to_text` 的逆。格式不对就报错，不猜。"""
        position, separator, slot = text.partition(_PART_TEXT_SEPARATOR)
        fields = position.split(",")
        if len(fields) != 4:
            raise ValueError("部件文本应为 维度,x,y,z[@面名]，得到 %r" % (text,))
        if separator and slot == SLOT_CENTER:
            raise ValueError("center 槽不写后缀，得到 %r" % (text,))
        dimension, x, y, z = [int(field) for field in fields]
        return cls(NodeKey(dimension, x, y, z), slot if separator else SLOT_CENTER)

    def to_dict(self):
        # type: () -> dict
        return {"node_key": self.node_key.to_dict(), "slot": self.slot}

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> PartKey
        return cls(NodeKey.from_dict(data["node_key"]), data.get("slot", SLOT_CENTER))

    def __eq__(self, other):
        if isinstance(other, PartKey):
            return self.slot == other.slot and self.node_key == other.node_key
        if isinstance(other, NodeKey):
            return self.slot == SLOT_CENTER and self.node_key == other
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return hash(self.as_key())

    def __repr__(self):
        return "PartKey(%r, %s)" % (self.node_key, self.slot)


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

    **面板（阶段 D，13 §7）**：多一个 `slot`（`PartKey` 的槽）。面板不是独立的设备记录，
    它的引用 = 宿主光缆的位置与代次 + 面名：`DeviceRef(宿主位置, 宿主代次, "north")`，
    也可以直接拿 `PartKey` 构造。宿主被拆掉重放（代次变了）它就过期，与整方块设备同一
    条纪律；同一格同一面拆下再挂一块，会话与存档由「面板离开」的钩子清掉。

    - center 槽（整方块设备，默认）的 `as_key` / `to_dict` 与改版前**逐字相同**，旧代码、
      旧存档、旧线上格式都不受影响；
    - 面板槽的 `as_key` 末尾多一个面名、`to_dict` 多一个 `"slot"`，因此与宿主光缆的引用
      互不相等（`matches` 为假）——不认识面板的旧路径拿到面板引用只会「查不到」，
      不会把它当成那格光缆。
    """

    __slots__ = ("node_key", "generation", "slot")

    def __init__(self, node_key, generation, slot=None):
        # type: (object, int, str) -> None
        if isinstance(node_key, PartKey):
            if slot is not None and slot != node_key.slot:
                raise ValueError("PartKey 的槽 %r 与 slot %r 不一致" % (node_key.slot, slot))
            slot = node_key.slot
            node_key = node_key.node_key
        if not isinstance(node_key, NodeKey):
            raise TypeError("node_key 必须是 NodeKey，得到 %r" % (type(node_key),))
        if slot is None:
            slot = SLOT_CENTER
        if slot not in PART_SLOTS:
            raise ValueError("未知的槽 %r（可用：%s）" % (slot, ", ".join(PART_SLOTS)))
        self.node_key = node_key
        self.generation = _require_int(generation, "generation")
        self.slot = slot

    @property
    def part_key(self):
        # type: () -> PartKey
        """这个实例占的那个部件：整方块设备是 center 槽（等于 `node_key`），面板是那一面。"""
        return PartKey(self.node_key, self.slot)

    def is_part(self):
        # type: () -> bool
        """是不是面板（非 center 槽）。"""
        return self.slot != SLOT_CENTER

    def host(self):
        # type: () -> DeviceRef
        """宿主那一格的引用（center 槽）。整方块设备返回自己。"""
        if self.slot == SLOT_CENTER:
            return self
        return DeviceRef(self.node_key, self.generation)

    def as_key(self):
        # type: () -> tuple
        if self.slot == SLOT_CENTER:
            return (self.node_key.as_key(), self.generation)
        return (self.node_key.as_key(), self.generation, self.slot)

    def matches(self, other):
        # type: (object) -> bool
        """代次、位置与槽都相同才算同一个实例。"""
        if not isinstance(other, DeviceRef):
            return False
        return self.as_key() == other.as_key()

    def to_dict(self):
        # type: () -> dict
        data = {
            "node_key": self.node_key.to_dict(),
            "generation": self.generation,
        }
        if self.slot != SLOT_CENTER:
            data["slot"] = self.slot
        return data

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> DeviceRef
        slot = data.get("slot", SLOT_CENTER)
        if slot == SLOT_CENTER and "slot" in data:
            # 同 `PartKey.from_text`：center 槽只有一种写法（不写），免得同一个实例两种键
            raise ValueError("center 槽不写 slot，得到 %r" % (data,))
        return cls(NodeKey.from_dict(data["node_key"]), data["generation"], slot)

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
        if self.slot != SLOT_CENTER:
            return "DeviceRef(%r, gen=%d, %s)" % (self.node_key, self.generation, self.slot)
        return "DeviceRef(%r, gen=%d)" % (self.node_key, self.generation)


def is_part_ref(ref):
    # type: (object) -> bool
    """`ref` 是不是面板的实例引用（`DeviceRef.slot` 不是 center）。

    鸭子类型：没有 `slot` 的引用（测试替身、旧调用方自制的对象）按 center 解释。
    """
    return getattr(ref, "slot", SLOT_CENTER) != SLOT_CENTER


def ref_key(ref):
    # type: (object) -> object
    """实例引用占的那个键：整方块设备是 `NodeKey`，面板是 `PartKey`（拓扑、准入、能源按它记账）。"""
    if is_part_ref(ref):
        return ref.part_key
    return ref.node_key


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
