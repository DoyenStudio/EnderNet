# coding=utf-8
"""设备生命周期：放置、邻接、拆除、加载、卸载与启动补查。

对应任务 01-05/01-06。

本模块把归一化事件变成**设备注册表的变化**，并输出一份变更清单供拓扑服务
标脏。它不建图、不读写库存、不推进订单——那分别是 02 与后续服务的事。

四条必须成立的纪律：

1. **「尝试破坏」不是「已拆除」。** `TRY_DESTROY` 只代表玩家挥了一下镐子，
   方块还在。把它当作拆除会让设备在一次误触后凭空消失。
2. **卸载不是拆除。** 区块卸载只把设备标记为未加载、卸下运行时能力，
   不执行掉落、不删除记录；重新加载恢复的是同一个实例与同一个代次。
3. **重复事件只建立一个实例。** 引擎可能对一次放置发出多条事件，
   启动补查也可能重复覆盖同一区域；注册表负责幂等，本模块只负责转发。
4. **迟到的事件不得误伤新设备。** 移除事件按 (位置, 方块种类) 匹配，
   位置已被别的方块占用时该事件无效。
"""

from ...common.identity import NodeKey
from .events import EventKind, NormalizedEvent  # noqa: F401 (NormalizedEvent 仅用于 # type: 注释)


class ChangeKind(object):
    """设备层面的变化种类，供拓扑服务消费。"""

    ADDED = "added"
    REMOVED = "removed"
    LOADED = "loaded"
    UNLOADED = "unloaded"
    NEIGHBORS_AFFECTED = "neighbors_affected"
    #: 请求交互层决定是否拦截（R01），本身不改变注册表
    PLACEMENT_REVIEW = "placement_review"


class LifecycleChange(object):
    """一条设备变化。"""

    __slots__ = ("kind", "node_key", "device_id", "ref", "detail")

    def __init__(self, kind, node_key, device_id=None, ref=None, detail=""):
        self.kind = kind
        self.node_key = node_key
        self.device_id = device_id
        self.ref = ref
        self.detail = detail

    def to_dict(self):
        # type: () -> dict
        return {
            "kind": self.kind,
            "node_key": self.node_key.to_dict(),
            "device_id": self.device_id,
            "generation": self.ref.generation if self.ref is not None else None,
            "detail": self.detail,
        }

    def __repr__(self):
        return "LifecycleChange(%s, %r, %s)" % (self.kind, self.node_key, self.device_id)


class LifecycleStats(object):
    __slots__ = (
        "placed",
        "duplicate_place",
        "removed",
        "ignored_removals",
        "unloaded",
        "loaded",
        "try_destroy_seen",
        "backfilled_chunks",
        "unknown_blocks",
    )

    def __init__(self):
        for name in self.__slots__:
            setattr(self, name, 0)

    def to_dict(self):
        # type: () -> dict
        return dict((name, getattr(self, name)) for name in self.__slots__)


class DeviceLifecycle(object):
    """把事件翻译成注册表变化。

    `world` 是一个鸭子类型的适配器，只需要两个方法，且仅在启动补查时用到：
      - `list_loaded_chunks()` -> list[ChunkKey]
      - `block_entities_in_chunk(chunk_key)` -> list[dict]，每项含
        `posX/posY/posZ/blockName`
    离线测试可以传 None，补查相关方法会明确报错而不是静默跳过。
    """

    def __init__(self, definitions, registry, catalog, world=None):
        self.definitions = definitions
        self.registry = registry
        self.catalog = catalog
        self.world = world
        self.stats = LifecycleStats()
        #: 已补查过的区块，避免重复扫描（重复扫描不得重复挂载）
        self._backfilled = set()
        #: 设备变化的唯一发布出口。拓扑等下游只认这一个口子。
        self._change_sink = None
        self.published = 0

    # -- 变化发布 ------------------------------------------------------

    def set_change_sink(self, sink):
        # type: (object) -> None
        """设置变化消费者。接收**一批**变化（`list`），通常是 bootstrap 的 `_emit`。

        **所有**设备状态变更都必须经过这里，包括启动补查与手动重扫。
        早先补查把变化清单直接丢掉，于是补查出来的设备只进了注册表、
        没进拓扑——注册表里有 2 台设备，拓扑里对应分量却是 None。
        """
        self._change_sink = sink

    def _publish(self, changes):
        # type: (list) -> list
        """发布一批变化。返回同一批，便于调用方继续使用。"""
        if changes and self._change_sink is not None:
            self._change_sink(changes)
        self.published += len(changes)
        return changes

    # -- 事件入口 ------------------------------------------------------

    def handle(self, event):
        # type: (NormalizedEvent) -> list
        """处理一条事件，发布并返回设备变化清单。"""
        handler = _HANDLERS.get(event.kind)
        if handler is None:
            return []
        return self._publish(handler(self, event))

    # -- 各类事件 ------------------------------------------------------

    def _resolve_facing(self, node_key, hint=None):
        # type: (NodeKey, object) -> object
        """读取方块朝向。

        两条发现路径都必须走这里：**放置事件**与**区块加载补查**。
        早先在事件适配器里读朝向，补查路径就漏掉了——真机重启后
        方向性设备全部丢失朝向（`facing=None`），而朝向决定端口方向。
        读取失败一律返回 None，不阻断注册：方向性设备退化为无方向，
        总好过整个设备注册不上。
        """
        if hint:
            return hint
        if self.world is None:
            return None
        try:
            return self.world.facing_at(node_key)
        except Exception:
            return None

    def _changes_from_outcome(self, outcome, node_key, device_id, source):
        # type: (object, NodeKey, str, str) -> list
        """把注册结果翻译成生命周期变化。

        **放置与补查共用这一个出口**，两条路径因此不可能对同一件事
        给出不同的结论。注册结果必须区分新增/替换/重新加载/纯重复：
        只看 `created` 布尔值的话，「重新加载」会被当成重复而丢掉，
        拓扑就永远收不到 `LOADED`。
        """
        if outcome.is_reload():
            self.stats.loaded += 1
            return [
                LifecycleChange(
                    ChangeKind.LOADED, node_key, device_id, outcome.record.ref, source
                )
            ]
        if outcome.is_duplicate():
            self.stats.duplicate_place += 1
            return []

        changes = []
        if outcome.replaced is not None:
            changes.append(
                LifecycleChange(
                    ChangeKind.REMOVED,
                    node_key,
                    outcome.replaced.device_id,
                    outcome.replaced.ref,
                    "replaced",
                )
            )
        self.stats.placed += 1
        changes.append(
            LifecycleChange(
                ChangeKind.ADDED, node_key, device_id, outcome.record.ref, source
            )
        )
        return changes

    def _on_place(self, event):
        node_key = event.data.get("node_key")
        device_id = event.data.get("device_id")
        if node_key is None or not device_id:
            return []
        if not self.definitions.has(device_id):
            self.stats.unknown_blocks += 1
            return []

        outcome = self.registry.register(
            node_key,
            device_id,
            facing=self._resolve_facing(node_key, event.data.get("facing")),
            tick=event.tick,
        )
        changes = self._changes_from_outcome(
            outcome, node_key, device_id, event.data.get("source", "")
        )
        if changes:
            changes.extend(self._neighbor_changes(node_key))
        return changes

    def _on_remove(self, event):
        node_key = event.data.get("node_key")
        device_id = event.data.get("device_id")
        if node_key is None or not device_id:
            return []

        removed = self.registry.unregister_matching(node_key, device_id)
        if removed is None:
            # 迟到的移除，或该位置根本没有对应设备。
            self.stats.ignored_removals += 1
            return []

        self.stats.removed += 1
        changes = [
            LifecycleChange(ChangeKind.REMOVED, node_key, device_id, removed.ref)
        ]
        changes.extend(self._neighbor_changes(node_key))
        return changes

    def _on_try_destroy(self, event):
        """玩家尝试破坏。

        **不改变注册表**：方块还在。只产出一条待交互层裁决的变化，
        对应 R01 里「放置/破坏时直接拦截」与「冲突停机」的选择。
        """
        node_key = event.data.get("node_key")
        if node_key is None:
            return []
        record = self.registry.get(node_key)
        if record is None:
            return []
        self.stats.try_destroy_seen += 1
        return [
            LifecycleChange(
                ChangeKind.PLACEMENT_REVIEW,
                node_key,
                record.device_id,
                record.ref,
                "try_destroy",
            )
        ]

    def _on_neighbor_changed(self, event):
        node_key = event.data.get("node_key")
        neighbor_key = event.data.get("neighbor_key")
        out = []
        for candidate in (node_key, neighbor_key):
            if candidate is not None and self.registry.get(candidate) is not None:
                out.append(
                    LifecycleChange(
                        ChangeKind.NEIGHBORS_AFFECTED,
                        candidate,
                        self.registry.get(candidate).device_id,
                        self.registry.get(candidate).ref,
                    )
                )
        return out

    def restore_chunk(self, chunk_key, block_entities=()):
        # type: (ChunkKey, list) -> list
        """恢复一个区块内的设备。

        **两条发现路径（区块加载事件、启动补查/手动重扫）共用这一个实现**，
        因此不可能对同一件事给出不同结论。

        修复「重载不通知拓扑」的关键**不是**这里的顺序，而是注册结果能区分
        `RELOADED` 与 `DUPLICATE`：只要区分了，两条路径任一条都能发出 LOADED。
        顺序仍按「先已知、后补查」写，因为这样语义更直白，且当引擎给出的
        `blockEntities` 列表不完整时，已知设备一定会被 `mark_loaded` 这条
        独立路径兜住（该列表的完整性是尚未验证的风险点）。
        """
        changes = []
        for record in self.registry.records_in_chunk(chunk_key):
            if self.registry.mark_loaded(record.node_key):
                self.stats.loaded += 1
                changes.append(
                    LifecycleChange(
                        ChangeKind.LOADED, record.node_key, record.device_id, record.ref
                    )
                )
        for entry in block_entities or ():
            changes.extend(self._backfill_entry(chunk_key, entry))
        return changes

    def _on_chunk_loaded(self, event):
        chunk_key = event.data.get("chunk_key")
        if chunk_key is None:
            return []
        changes = self.restore_chunk(chunk_key, event.data.get("block_entities"))
        self._backfilled.add(chunk_key.as_key())
        return changes

    def _on_chunk_unloaded(self, event):
        chunk_key = event.data.get("chunk_key")
        if chunk_key is None:
            return []
        changes = []
        for node_key in self.registry.unload_chunk(chunk_key):
            record = self.registry.get(node_key)
            self.stats.unloaded += 1
            changes.append(
                LifecycleChange(
                    ChangeKind.UNLOADED,
                    node_key,
                    record.device_id if record else None,
                    record.ref if record else None,
                )
            )
        return changes

    # -- 辅助 ----------------------------------------------------------

    def _backfill_entry(self, chunk_key, entry):
        """按一条方块实体记录补查设备。"""
        try:
            node_key = self._node_key_of(chunk_key, entry)
            device_id = entry.get("blockName") or entry.get("block_name")
        except (KeyError, TypeError, AttributeError):
            return []
        if not device_id or not self.definitions.has(device_id):
            return []

        # 与放置事件走同一条朝向解析，否则重启后方向性设备会丢朝向。
        outcome = self.registry.register(
            node_key, device_id, facing=self._resolve_facing(node_key)
        )
        changes = self._changes_from_outcome(outcome, node_key, device_id, "backfill")
        if changes:
            changes.extend(self._neighbor_changes(node_key))
        return changes

    @staticmethod
    def _node_key_of(chunk_key, entry):
        return NodeKey(
            chunk_key.dimension,
            int(entry["posX"]),
            int(entry["posY"]),
            int(entry["posZ"]),
        )

    def _neighbor_changes(self, node_key):
        """本次变化会影响到的邻接设备，供拓扑标脏。"""
        out = []
        for neighbor_key in node_key.neighbors():
            record = self.registry.get(neighbor_key)
            if record is not None:
                out.append(
                    LifecycleChange(
                        ChangeKind.NEIGHBORS_AFFECTED,
                        neighbor_key,
                        record.device_id,
                        record.ref,
                    )
                )
        return out

    # -- 启动补查 ------------------------------------------------------

    def plan_startup_backfill(self, chunk_keys=None, skip_done=True):
        # type: (list, bool) -> object
        """生成器：逐个区块补查设备，可跨帧继续。

        **这不是设备发现的主要路径。** 世界在脚本初始化时尚未加载，
        之后每个区块加载都会触发 `CHUNK_LOADED` 事件并附带方块实体列表，
        由 `_on_chunk_loaded` 直接处理。本方法用于兜底与手动重扫。

        分帧是硬要求：主世界一次加载的区块可能上千个，
        一次性扫完会卡住服务端主线程（架构规划 6.1）。

        `chunk_keys` 为 None 时向 world 适配器索取当前已知的已加载区块。
        """
        if chunk_keys is None:
            if self.world is None:
                raise RuntimeError(
                    "启动补查需要 world 适配器；离线测试请显式传入 chunk_keys"
                )
            chunk_keys = self.world.list_loaded_chunks()

        for chunk_key in chunk_keys:
            if skip_done and chunk_key.as_key() in self._backfilled:
                continue
            # 与区块加载事件走同一条恢复路径，并且**经同一个出口发布**——
            # 否则补查出来的设备只进注册表、进不了拓扑。
            self._publish(
                self.restore_chunk(chunk_key, self._entries_in_chunk(chunk_key))
            )
            self._backfilled.add(chunk_key.as_key())
            self.stats.backfilled_chunks += 1
            # 让出控制权，由调度器决定何时继续
            yield chunk_key

    def _entries_in_chunk(self, chunk_key):
        if self.world is None:
            return ()
        return self.world.block_entities_in_chunk(chunk_key) or ()

    def forget_backfill(self, chunk_key=None):
        # type: (object) -> None
        """丢弃补查记录。仅用于世界重载等确实需要重扫的场景。"""
        if chunk_key is None:
            self._backfilled = set()
        else:
            self._backfilled.discard(chunk_key.as_key())

    def diagnostics(self):
        # type: () -> dict
        report = self.stats.to_dict()
        report["backfilled_chunks_tracked"] = len(self._backfilled)
        report["registry"] = self.registry.stats()
        return report


_HANDLERS = {
    EventKind.PLACE: DeviceLifecycle._on_place,
    EventKind.REMOVE: DeviceLifecycle._on_remove,
    EventKind.TRY_DESTROY: DeviceLifecycle._on_try_destroy,
    EventKind.NEIGHBOR_CHANGED: DeviceLifecycle._on_neighbor_changed,
    EventKind.CHUNK_LOADED: DeviceLifecycle._on_chunk_loaded,
    EventKind.CHUNK_UNLOADED: DeviceLifecycle._on_chunk_unloaded,
}
