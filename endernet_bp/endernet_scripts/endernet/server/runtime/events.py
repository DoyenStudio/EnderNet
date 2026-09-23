# coding=utf-8
"""归一化事件与事件队列。

对应任务 01-05。

**分层理由。** 引擎回调只做两件事：把平台事件翻译成本模块的
`NormalizedEvent`，然后入队；真正的网络业务交由主控制器与服务处理
（架构规划 5.3「修改通过命令，通知用于派生状态」）。
翻译动作本身是纯数据变换，因此放在 runtime 层、可脱离游戏测试；
真正 import `tooldelta` 的监听器注册在 `server/adapters/events.py`。

**关于两种 tick。** `OnScriptTickServer` 与 `OnSimTickServerEvent` 是
**两个不同的时钟**，文档明确警告不得混同，且封装库没有记录任何一个的频率。
这里把它们归一化成 `SCRIPT_TICK` 与 `SIM_TICK` 两种独立事件，
由调度器决定以哪一个作为权威时钟（`LogicalClock`）。
本模块不做任何「每秒多少次」的假设。

**关于队列合并。** 一次放置可能同时触发
`ServerPlaceBlockEntityEvent` 与 `EntityPlaceBlockAfterServerEvent`；
区块卸载会一次性丢弃大量实体。队列按位置合并同类事件，
使「同帧多个事件描述同一件事」只产生一条工作，
这同时是 01 验收里「放置一次、重复事件、重复恢复都只注册一次」的第一道闸门。
"""
from __future__ import absolute_import, division, print_function

from ...common.identity import ChunkKey, NodeKey


class EventKind(object):
    """归一化事件种类。"""

    #: 方块放下（可能由多条引擎事件翻译而来）
    PLACE = "place"
    #: 方块被移除（真实拆除）
    REMOVE = "remove"
    #: 玩家尝试破坏：可取消，是 R01 的拦截点
    TRY_DESTROY = "try_destroy"
    #: 邻接方块变化，用于唤醒邻接设备
    NEIGHBOR_CHANGED = "neighbor_changed"
    #: 区块加载完成
    CHUNK_LOADED = "chunk_loaded"
    #: 区块被卸载/丢弃
    CHUNK_UNLOADED = "chunk_unloaded"
    #: 自定义方块实体 tick（每秒 20 次，仅对声明了 tick 的方块）
    BLOCK_ENTITY_TICK = "block_entity_tick"
    #: 脚本 tick（平台「每秒若干次」的这一种）
    SCRIPT_TICK = "script_tick"
    #: 模拟 tick（**另一种**时钟，不得与上者混同）
    SIM_TICK = "sim_tick"
    #: 玩家离开，用于解除订阅
    PLAYER_LEFT = "player_left"
    #: 脚本卸载/世界退出
    SHUTDOWN = "shutdown"


#: 可以在同一帧内按位置合并的事件种类。
_COALESCIBLE = frozenset(
    [
        EventKind.PLACE,
        EventKind.REMOVE,
        EventKind.TRY_DESTROY,
        EventKind.NEIGHBOR_CHANGED,
        EventKind.BLOCK_ENTITY_TICK,
    ]
)


class NormalizedEvent(object):
    """一条翻译好的事件。

    `tick` 是入队时的逻辑时刻，仅用于诊断与合并；
    它**不是**权威时钟，权威时钟在调度器里。
    """

    __slots__ = ("kind", "tick", "data", "sequence")

    def __init__(self, kind, tick=0, data=None, sequence=0):
        # type: (str, int, dict, int) -> None
        self.kind = kind
        self.tick = int(tick)
        self.data = dict(data or {})
        self.sequence = int(sequence)

    # -- 便捷构造 ------------------------------------------------------

    @classmethod
    def place(cls, node_key, device_id, tick=0, facing=None, source=""):
        return cls(
            EventKind.PLACE,
            tick,
            {"node_key": node_key, "device_id": device_id, "facing": facing, "source": source},
        )

    @classmethod
    def remove(cls, node_key, device_id, tick=0, source=""):
        return cls(
            EventKind.REMOVE, tick, {"node_key": node_key, "device_id": device_id, "source": source}
        )

    @classmethod
    def chunk_loaded(cls, chunk_key, block_entities=(), tick=0):
        return cls(
            EventKind.CHUNK_LOADED,
            tick,
            {"chunk_key": chunk_key, "block_entities": tuple(block_entities)},
        )

    @classmethod
    def chunk_unloaded(cls, chunk_key, tick=0):
        return cls(EventKind.CHUNK_UNLOADED, tick, {"chunk_key": chunk_key})

    @classmethod
    def neighbor_changed(cls, node_key, neighbor_key, to_device_id=None, tick=0):
        return cls(
            EventKind.NEIGHBOR_CHANGED,
            tick,
            {
                "node_key": node_key,
                "neighbor_key": neighbor_key,
                "to_device_id": to_device_id,
            },
        )

    # -- 查询 ----------------------------------------------------------

    @property
    def node_key(self):
        return self.data.get("node_key")

    @property
    def chunk_key(self):
        return self.data.get("chunk_key")

    def coalesce_key(self):
        # type: () -> object
        """同帧合并的键。不可合并的种类返回 None（永不合并）。"""
        if self.kind not in _COALESCIBLE:
            return None
        node_key = self.data.get("node_key")
        if node_key is None:
            return None
        return (self.kind, node_key.as_key())

    def to_dict(self):
        # type: () -> dict
        payload = {}
        for key, value in self.data.items():
            if isinstance(value, NodeKey):
                payload[key] = value.to_dict()
            elif isinstance(value, ChunkKey):
                payload[key] = value.to_dict()
            else:
                payload[key] = value
        return {
            "kind": self.kind,
            "tick": self.tick,
            "sequence": self.sequence,
            "data": payload,
        }

    def __repr__(self):
        return "NormalizedEvent(%s, %r)" % (self.kind, self.data.get("node_key"))


class EventQueue(object):
    """有界事件队列，按位置合并同类事件。

    合并策略：同一帧内、同一种类、同一位置只保留**最后一条**。
    对放置而言后到者携带更新后的信息；对移除而言只要知道「没了」就够。
    """

    def __init__(self, max_pending=4096):
        # type: (int) -> None
        self.max_pending = int(max_pending)
        self._pending = []
        #: `coalesce_key -> NormalizedEvent`，指向 `_pending` 中的当前代表
        self._index = {}
        self._sequence = 0
        self.pushed = 0
        self.coalesced = 0
        self.dropped = 0

    def push(self, event):
        # type: (NormalizedEvent) -> bool
        """入队。返回 False 表示因为队列已满而被丢弃。"""
        self._sequence += 1
        event.sequence = self._sequence
        self.pushed += 1

        key = event.coalesce_key()
        if key is not None:
            existing = self._index.get(key)
            if existing is not None:
                # 用新事件替换旧代表，位置不变，保持先来后到的处理顺序。
                index = self._pending.index(existing)
                self._pending[index] = event
                self._index[key] = event
                self.coalesced += 1
                return True

        if len(self._pending) >= self.max_pending:
            self.dropped += 1
            return False

        self._pending.append(event)
        if key is not None:
            self._index[key] = event
        return True

    def extend(self, events):
        # type: (list) -> int
        accepted = 0
        for event in events:
            if self.push(event):
                accepted += 1
        return accepted

    def drain(self, limit=None):
        # type: (int) -> list
        """取出一批事件。

        `limit` 是本帧允许处理的**事件条数**预算；剩余留到下一帧，
        避免一次区块加载把整帧时间吃光。
        """
        if limit is None or limit >= len(self._pending):
            batch = self._pending
            self._pending = []
            self._index = {}
            return batch

        batch = self._pending[:limit]
        self._pending = self._pending[limit:]
        # 重建索引，指向仍然留在队列里的代表
        self._index = {}
        for event in self._pending:
            key = event.coalesce_key()
            if key is not None:
                self._index[key] = event
        return batch

    def clear(self):
        # type: () -> int
        count = len(self._pending)
        self._pending = []
        self._index = {}
        return count

    def __len__(self):
        return len(self._pending)

    def stats(self):
        # type: () -> dict
        return {
            "pending": len(self._pending),
            "pushed": self.pushed,
            "coalesced": self.coalesced,
            "dropped": self.dropped,
        }
