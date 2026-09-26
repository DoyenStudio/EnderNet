# coding=utf-8
"""发布窗口的纯计算部分：分片、冻结快照、增量批次。

与 `terminal.py` 分开，是因为这几样东西**完全不需要知道会话、订阅者或
引擎**——它们只回答「把一条记录序列切成若干个不超限的包」。混在同一个
文件里会让 `terminal.py` 长到需要滚动才能读完，而滚动阅读正是漏掉边界
条件的地方。

**分片大小只作分片依据，不承诺等于引擎实际包大小。** 真正的线上编码由
`ResourceDescriptions` 的序号替换，而同一条记录在不同会话里的序号不同，
因此这里量的是「带完整描述的最坏情况」——它是上界，不是等号。用它分片
是安全的（不会因为描述而突然翻倍），把它当成流量统计则是错的。
"""

import json

#: 记录在包里的三种形态。用常量而不是 `None` 之类的位置约定：
#: 「stored 是不是 None」这种判据在加了一个可空字段之后就会失效。
ENTRY_UPSERT = "u"
ENTRY_REMOVE = "r"


def measure_payload(payload):
    # type: (object) -> int
    """一份待发载荷的 UTF-8 JSON 字节数。

    **先编码再量长度，不估公式。** 估算（「每条大约 40 字节」）会在资源键
    带 `userData`（原版物品的完整 NBT 快照）时严重偏低，而那正是最需要
    分片的一类条目。多花的一次编码换掉一整类「平时都对、上线就爆」的
    分片失效。
    """
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if isinstance(text, bytes):
        # py3 的 `json.dumps` 返回 `str`，走不到这里；py2 的纯 ASCII 结果
        # 是 `str`（即字节串），长度就是字节数。
        return len(text)
    return len(text.encode("utf-8"))


def entry_size(entry):
    # type: (tuple) -> int
    """一条待发条目的上界字节数。`entry` 形如 `(ENTRY_*, key, stored, reserved)`。"""
    if entry[0] == ENTRY_REMOVE:
        return measure_payload(entry[1].to_dict())
    return measure_payload([entry[1].to_dict(), entry[2], entry[3]])


def _fits(current, size, max_items, max_bytes, current_bytes):
    # type: (list, int, int, int, int) -> bool
    if not current:
        # **单片至少一条。** 单条就超过 `max_bytes` 时让它自己成一片发出去，
        # 而不是卡在原地反复切空片——卡住比超限严重得多：超限只是这一包
        # 大一点，卡住是整个网络从此不再发布。
        return True
    if max_items > 0 and len(current) >= max_items:
        return False
    if max_bytes > 0 and current_bytes + size > max_bytes:
        return False
    return True


def split_entries(entries, max_items, max_bytes):
    # type: (tuple, int, int) -> tuple
    """按条数与字节两个上限切分，返回若干片（每片是一个元组）。

    两个上限**任一触顶就换片**；两者都 `<= 0` 表示不限制（退化成一片）。
    顺序即输入顺序，因此同样的输入永远切出同样的片——分片结果可被断言。
    """
    chunks = []
    current = []
    current_bytes = 0
    for entry in entries:
        size = entry_size(entry)
        if not _fits(current, size, max_items, max_bytes, current_bytes):
            chunks.append(tuple(current))
            current = []
            current_bytes = 0
        current.append(entry)
        current_bytes += size
    if current:
        chunks.append(tuple(current))
    return tuple(chunks)


def record_entries(records):
    # type: (tuple) -> tuple
    """`(key, stored, reserved)` 序列 → 分片用的条目序列。"""
    return tuple(
        (ENTRY_UPSERT, key, stored, reserved) for key, stored, reserved in records
    )


def split_records(records, max_items, max_bytes):
    # type: (tuple, int, int) -> tuple
    """把快照记录切成分片，返回若干 `(key, stored, reserved)` 元组。

    快照与增量走的是**同一套切分规则**（`split_entries`）：两条路径各写
    一份的话，改了一处忘了另一处，表现是「快照的分片数量对不上客户端
    收到的」——而那种偏差只在特定条目规模下才出现。
    """
    return tuple(
        tuple((entry[1], entry[2], entry[3]) for entry in chunk)
        for chunk in split_entries(record_entries(records), max_items, max_bytes)
    )


class FrozenSnapshot(object):
    """一次发布窗口里冻结下来的整份物品视图。

    **`records` 与 `chunks` 都是不可变的**，同版本的多名订阅者共享同一份，
    不必各自复制一遍（`terminal_item_index.md` §6 末段要求）。冻结发生在
    发布窗口内部，之后到达的提交只会以增量出现，因此这份快照永远自洽。
    """

    __slots__ = ("snapshot_id", "version", "records", "chunks", "bytes")

    def __init__(self, snapshot_id, version, records, policy):
        self.snapshot_id = snapshot_id
        self.version = version
        self.records = tuple(records)
        self.chunks = split_records(
            self.records, policy.snapshot_chunk_items, policy.snapshot_chunk_bytes
        )
        self.bytes = sum(entry_size(entry) for entry in record_entries(self.records))

    def item_count(self):
        # type: () -> int
        return len(self.records)

    def chunk_count(self):
        # type: () -> int
        return len(self.chunks)

    def to_dict(self):
        # type: () -> dict
        return {
            "snapshot_id": self.snapshot_id,
            "version": self.version,
            "items": len(self.records),
            "chunks": len(self.chunks),
            "bytes": self.bytes,
        }


class DeltaPacket(object):
    """一个增量批次里的一包。

    同批的所有包共享 `base_version` / `new_version`（在 `DeltaBatch` 上），
    因此这里只带自己的分片位置与内容。
    """

    __slots__ = ("packet_index", "packet_count", "upserts", "removes", "bytes")

    def __init__(self, packet_index, packet_count, upserts, removes, size):
        self.packet_index = packet_index
        self.packet_count = packet_count
        self.upserts = tuple(upserts)
        self.removes = tuple(removes)
        self.bytes = size

    def item_count(self):
        # type: () -> int
        return len(self.upserts) + len(self.removes)

    def is_last(self):
        # type: () -> bool
        return self.packet_index == self.packet_count - 1

    def __repr__(self):
        return "DeltaPacket(%d/%d, +%d -%d)" % (
            self.packet_index,
            self.packet_count,
            len(self.upserts),
            len(self.removes),
        )


class DeltaBatch(object):
    """一个发布窗口产出的增量批次。

    **一次窗口 = 一次版本推进 = 1..N 个包。** 不用链式 `V→V+1→…→V+N`：
    那会把一个窗口变成 N 次推进，断档判定随之复杂化，而收益只是让单包
    能独立应用（`terminal_item_index.md` §6 表）。
    """

    __slots__ = ("batch_id", "base_version", "new_version", "upserts",
                 "removes", "packets", "frame")

    def __init__(self, batch_id, base_version, new_version, upserts, removes,
                 packets, frame):
        self.batch_id = batch_id
        self.base_version = base_version
        self.new_version = new_version
        self.upserts = tuple(upserts)
        self.removes = tuple(removes)
        self.packets = tuple(packets)
        self.frame = frame

    def key_count(self):
        # type: () -> int
        return len(self.upserts) + len(self.removes)

    def bytes(self):
        # type: () -> int
        return sum(packet.bytes for packet in self.packets)

    def to_dict(self):
        # type: () -> dict
        return {
            "batch_id": self.batch_id,
            "base_version": self.base_version,
            "new_version": self.new_version,
            "upserts": len(self.upserts),
            "removes": len(self.removes),
            "packets": len(self.packets),
        }

    def __repr__(self):
        return "DeltaBatch(%s, %d->%d, %d 键)" % (
            self.batch_id,
            self.base_version,
            self.new_version,
            self.key_count(),
        )


def build_delta_packets(batch_id, base_version, new_version, upserts, removes,
                        max_items, max_bytes, frame=0):
    # type: (str, int, int, tuple, tuple, int, int, int) -> DeltaBatch
    """把一批 upsert / remove 切成若干包，装成 `DeltaBatch`。

    upsert 在前、remove 在后，顺序固定；同样的输入永远切出同样的包，
    因此「一批分几包」可以被断言，而不是每帧碰运气。
    """
    entries = [(ENTRY_UPSERT, key, stored, reserved)
               for key, stored, reserved in upserts]
    entries += [(ENTRY_REMOVE, key, 0, 0) for key in removes]
    chunks = split_entries(tuple(entries), max_items, max_bytes)
    count = len(chunks)
    packets = []
    for index, chunk in enumerate(chunks):
        packet_upserts = []
        packet_removes = []
        size = 0
        for entry in chunk:
            size += entry_size(entry)
            if entry[0] == ENTRY_UPSERT:
                packet_upserts.append((entry[1], entry[2], entry[3]))
            else:
                packet_removes.append(entry[1])
        packets.append(
            DeltaPacket(index, count, packet_upserts, packet_removes, size)
        )
    return DeltaBatch(
        batch_id,
        base_version,
        new_version,
        upserts,
        removes,
        packets,
        frame,
    )
