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

#: 区块加载补查注册出来的设备，`ADDED` / `LOADED` 变化的 `detail`。光缆的状态维护靠它
#: 区分「刚放下」与「区块刚加载」（后者要等一会儿再读，`devices/cables.py`）。
SOURCE_BACKFILL = "backfill"


class ChangeKind(object):
    """设备层面的变化种类，供拓扑服务消费。"""

    ADDED = "added"
    REMOVED = "removed"
    LOADED = "loaded"
    UNLOADED = "unloaded"
    NEIGHBORS_AFFECTED = "neighbors_affected"
    #: 请求交互层决定是否拦截（R01），本身不改变注册表
    PLACEMENT_REVIEW = "placement_review"
    #: 一块面板离开宿主光缆（扳手拆下单块，或整格光缆被扳手拆掉，13 §6）。
    #: `node_key` / `ref` 是**宿主光缆**的位置与实例，`device_id` 是面板标识
    #: （`*_terminal_part`），`detail` 是面名——`PartKey(node_key, detail)` 就是那块面板。
    #: 面板不是独立的设备记录（它只存在于宿主的 `config["parts"]`），这条变化是会话与
    #: 存档清理（阶段 D）的钩子；拓扑照常把宿主标脏。
    #:
    #: 两条来路（阶段 D）：扳手拆下（`detach_part`，发出时宿主还在注册表里、物品已给玩家）；
    #: 整格光缆被挖掉、炸掉（移除事件，紧跟在宿主的 `REMOVED` 后面逐块发，发出时宿主已经
    #: 注销——消费者据此把面板物品掉在原地，见 `devices/panels.py`）。
    PART_DETACHED = "part_detached"
    #: 同一格的面板宿主换了方块（2026-11-10）：面板底座 → 光缆（手持光缆点底座），或光缆 →
    #: 面板底座（扳手潜行拆挂着面板的光缆）。**同一个实例**：`ref` 不变（代次不变），面板与
    #: 终端库存标记原样留在格子里——全局约束 5 的「同位置替换」，不是拆除 + 放置，面板不算
    #: 离开，会话与存档不作废。`device_id` 是新方块，`detail` 是旧方块。拓扑照常标脏重建，
    #: 状态维护重算这格与相邻光缆的外观。
    HOST_CONVERTED = "host_converted"
    #: 面板**正面所对**的那一格换了方块（面板骨架，2026-11-10；AE2 `IPart.onNeighborChanged`
    #: 只关心正面那一格的口径）。`node_key` / `ref` 是宿主，`device_id` 是面板，`detail` 是
    #: 面名——`PartKey(node_key, detail).front_key()` 就是变了的那一格。**不改拓扑**（拓扑
    #: 不为它标脏），只交给面板按种类处理（`devices/panels.PanelService.front_changed`）。
    PART_FRONT_CHANGED = "part_front_changed"


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
        "config_errors",
        "parts_detached",
        "hosts_converted",
        "part_fronts_changed",
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
        #: 注册时从世界读设备自带的 config（`(node_key, device_id) -> dict | None`）。
        #: 光缆要在注册那一刻带上切断面与面板，否则拓扑会先按「全开」连一遍再拆开。
        self._config_reader = None

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

    def set_config_reader(self, reader):
        # type: (object) -> None
        """设置注册时读 config 的口子：`reader(node_key, device_id)` 返回 dict 或 None。

        光缆由 `devices/cables.CableStateKeeper.read_config` 从方块实体读出切断面与面板
        （`.claude/13` §5）；别的设备返回 None，注册表照旧给空 config。放置与区块补查
        两条发现路径都经 `_register`，因此都带得上。
        """
        self._config_reader = reader

    def _read_config(self, node_key, device_id):
        # type: (NodeKey, str) -> object
        """读注册时的 config。读失败不阻断注册（与朝向同理），按「没有 config」处理。"""
        if self._config_reader is None:
            return None
        try:
            return self._config_reader(node_key, device_id)
        except Exception:
            self.stats.config_errors += 1
            return None

    def _register(self, node_key, device_id, facing_hint=None, tick=0):
        # type: (NodeKey, str, object, int) -> object
        """放置与补查共用的注册：朝向与 config 都在这里读，两条路径不会一边漏一边。

        光缆与面板底座没有朝向，不去读它（区块加载时每格省一次 `GetBlockStates`）。同位置
        已经是同一种方块（重复通知、区块重新加载）时注册表不收新 config，也就不读——
        重新加载的光缆由状态维护稍后重读（`devices/cables.py`）。
        """
        if self.definitions.has_face_data(device_id):
            facing = None
        else:
            facing = self._resolve_facing(node_key, facing_hint)
        existing = self.registry.get(node_key)
        if existing is not None and existing.device_id == device_id:
            config = None
        else:
            config = self._read_config(node_key, device_id)
        return self.registry.register(
            node_key, device_id, facing=facing, config=config, tick=tick
        )

    def detach_part(self, record, face, part_id):
        # type: (object, str, str) -> list
        """发一条「面板离开」（`ChangeKind.PART_DETACHED`），经唯一出口发布。

        扳手拆下单块面板、或拆掉整格光缆时由交互层调（`devices/cable_intents.py`）。
        **调用前宿主的 `config["parts"]` 与方块实体已经去掉了这一块**，下游只做清理。
        """
        self.stats.parts_detached += 1
        return self._publish(
            [LifecycleChange(ChangeKind.PART_DETACHED, record.node_key, part_id, record.ref, face)]
        )

    def convert(self, node_key, device_id):
        # type: (NodeKey, str) -> list
        """面板宿主原地换方块（`ChangeKind.HOST_CONVERTED`），经唯一出口发布并返回变化清单。

        **调用前世界里的方块与方块实体已经换好**（`CableStateKeeper.swap_block`）；这里只改
        注册表的型号（同一实例、同一代次，`DeviceRegistry.convert`）并通知下游。随后引擎派发的
        旧方块移除事件按型号对不上忽略，新方块的放置事件当重复，都不会再改一遍。没有这条
        记录（或型号本来就是它）返回空清单。
        """
        record = self.registry.get(node_key)
        old_device_id = record.device_id if record is not None else None
        converted = self.registry.convert(node_key, device_id)
        if converted is None:
            return []
        self.stats.hosts_converted += 1
        changes = [
            LifecycleChange(
                ChangeKind.HOST_CONVERTED, node_key, device_id, converted.ref, old_device_id
            )
        ]
        changes.extend(self._neighbor_changes(node_key))
        return self._publish(changes)

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

        outcome = self._register(
            node_key, device_id, facing_hint=event.data.get("facing"), tick=event.tick
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
        # 挖掉挂着面板的光缆（阶段 D）：面板就在刚注销的这条记录里，当场读出来，逐块发
        # 「面板离开」——之后再也读不到了（方块实体随方块没了）。扳手整格拆时面板已先摘掉，
        # 这里读到的是空的，不会再发一遍。
        for face, part_id in self.catalog.parts_of(removed):
            self.stats.parts_detached += 1
            changes.append(
                LifecycleChange(ChangeKind.PART_DETACHED, node_key, part_id, removed.ref, face)
            )
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
        out.extend(self._front_changes(node_key, neighbor_key))
        return out

    def _front_changes(self, node_key, neighbor_key):
        # type: (NodeKey, NodeKey) -> list
        """`node_key` 那一格的邻格 `neighbor_key` 变了：挂在朝它那一面的面板，正面所对的
        方块就变了（面板骨架）。没有面板、或不是正面那一格就没有。"""
        if node_key is None or neighbor_key is None:
            return []
        host = self.registry.get(node_key)
        if host is None:
            return []
        out = []
        for face, part_id in self.catalog.parts_of(host):
            if node_key.neighbor(face) == neighbor_key:
                self.stats.part_fronts_changed += 1
                out.append(
                    LifecycleChange(ChangeKind.PART_FRONT_CHANGED, node_key, part_id, host.ref, face)
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

        # 与放置事件走同一条注册（朝向与 config），否则重启后方向性设备会丢朝向、
        # 光缆会丢切断面与面板。
        outcome = self._register(node_key, device_id)
        changes = self._changes_from_outcome(outcome, node_key, device_id, SOURCE_BACKFILL)
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
