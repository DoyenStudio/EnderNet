# coding=utf-8
"""设备的在线 / 离线外观（2026-11-13，作者画的离线材质）：按设备状态写方块状态，面板按面换离线屏幕。

## 整方块设备

有离线外观的（`DefinitionSet.status_values_of` 非空）多一个方块状态 `endernet:status`：0 离线
（默认值，刚放下就是它）、1 在线、2 冲突（只有控制器有：所在网络里不止一台控制器，照 AE2 控制器冲突
变红）。结论取设备状态（`RuntimeManager.device_status`），与设备界面上的「在线 / 离线」是同一个。
只在变了时写；第一次写之前先读世界里现在的值（方块状态随存档留着上次的），一样就不写。贴图切换在
方块 JSON 的排列里（`scripts/gen_blocks.py`）。

物品栏图标画的是没放过的默认组合（放置痕迹，`definitions.PLACED_FACE_STATE`），那一套是在线外观。
玩家放下的方块不会是这个组合；`/setblock`、结构与旧存档放出的会是，第一次写时连同 `PLACED_HALF`
一起写，把它变成放过的，之后只看外观状态。

## 面板

面板没有自己的方块，离线走面板外观读数：`PanelService.display_values` 问 `panel_offline`，离线的那一面
取值加 `slot_display.OFFLINE_OFFSET`，画那一种的离线屏幕。这里记着每块面板上次的结论，变了就让外观
重比一次（`mark_panels`）。还没算过的面板按离线画，与整方块设备的默认值一致。只跟踪挂着面板的那几格。

## 结论还没出来时沿用上一次

接入一台设备、拆一段光缆，整网都要重建；重建、带宽与能源确认、控制器恢复都可能跨好几帧，这期间
设备状态是「待确认」，按字面画就是整网先灭一下再亮。所以设备状态带着 `HOLD_CODES` 里的原因时外观
不动：画着在线的继续在线，刚放下还没写过的继续是默认的离线。最多沿用 `hold_ticks` 帧，过了还没
结论就照实画离线，免得卡在待确认里的网络一直亮着。

## 什么时候重算

- 放下、区块加载、原地换方块：这一格排进待算；拆掉、卸载：忘掉；面板增减（按面数据变了）：
  这一格重新登记（`refresh_host`）；
- 拓扑发布、带宽判定变了、运行时变了：全部排进待算（`mark_all`；网络状态一变，整网的结论都可能变）；
- 此外每帧轮转复查几台：够不够电没有通知，靠它兜住。

每帧最多算 `budget` 台，待算的先、轮转的后（`advance`，由 `ServerBootstrap.tick` 在运行时推进之后调）。
"""

from collections import deque

from ...common.definitions import (
    PLACED_HALF,
    PLACED_HALF_STATE,
    STATUS_CONFLICT,
    STATUS_OFFLINE,
    STATUS_ONLINE,
    STATUS_STATE,
    looks_unplaced,
)
from ...common.identity import DeviceRef, PartKey
from ...common.protocol import Reason, reason_code
from ..runtime import ChangeKind

#: 跟踪的两种格子：有离线外观的整方块设备、挂着面板的光缆或面板底座
BLOCK = "block"
PANELS = "panels"

#: 只说明「结论还没出来」的原因：网络刷新中、控制器恢复中、带宽或能源确认中、网络功能启动中。设备状态
#: 带着其中任何一个时外观沿用上一次（见模块说明）。比 `network.controller.PENDING_CODES` 窄：没有
#: 控制器、拓扑未知、未配置这些会一直持续，照常画离线。
HOLD_CODES = frozenset([
    Reason.REBUILDING,
    Reason.MASTER_RECOVERING,
    Reason.BANDWIDTH_UNKNOWN,
    Reason.ENERGY_UNAVAILABLE,
    Reason.SERVICE_NOT_READY,
])

#: 外观沿用的上限（帧）：5 秒
HOLD_TICKS = 100


def _as_key(key):
    return key.as_key()


class StatusLooks(object):
    """设备外观状态的维护。可以脱离引擎构造：世界读写与设备状态都经注入的对象。

    - `world`：`block_states_at` / `update_block_states`（缺了就只记账、不写）；
    - `status_of(ref) -> DeviceNetworkStatus`：设备状态（`is_online`）；
    - `conflict_of(node_key) -> bool`：这一格所在的网络是不是控制器冲突；
    - `mark_panels(node_key)`：面板外观重比一次（`SlotDisplayTracker.mark_panels`）。
    """

    def __init__(self, definitions, registry, catalog, world, status_of, conflict_of=None,
                 mark_panels=None, budget=32, sweep=8, hold_ticks=HOLD_TICKS):
        self.definitions = definitions
        self.registry = registry
        self.catalog = catalog
        self.world = world
        self.status_of = status_of
        self.conflict_of = conflict_of
        self.mark_panels = mark_panels
        self.budget = int(budget)
        self.sweep = int(sweep)
        self.hold_ticks = int(hold_ticks)
        #: 推进过几帧（`advance` 每帧一次），沿用外观按它计时
        self._ticks = 0
        #: NodeKey -> BLOCK / PANELS
        self._tracked = {}
        #: 待算（先进先出，不重复）
        self._queue = deque()
        self._queued = set()
        #: 轮转复查的次序（跟踪的格子变了就重排）与游标
        self._order = None
        self._cursor = 0
        #: NodeKey -> 上次写进世界（或读到世界里已经是）的外观状态
        self._written = {}
        #: PartKey -> 上次的结论（True = 离线）
        self._panels = {}
        #: (NodeKey, 面或 None) -> 从哪一帧起在沿用外观
        self._holding = {}
        self.stats = dict.fromkeys(
            ("evaluated", "written", "already", "unplaced", "write_failures", "status_errors",
             "panel_changes", "held"),
            0,
        )

    # -- 跟踪 ------------------------------------------------------------

    def observe(self, change):
        # type: (object) -> None
        """`ServerBootstrap._emit` 的同一批设备变化。"""
        node_key = change.node_key
        if node_key is None:
            return
        kind = change.kind
        if kind in (ChangeKind.ADDED, ChangeKind.LOADED, ChangeKind.HOST_CONVERTED):
            self.refresh_host(node_key)
        elif kind in (ChangeKind.REMOVED, ChangeKind.UNLOADED):
            self._forget(node_key)
        elif kind == ChangeKind.PART_DETACHED:
            self.refresh_host(node_key)

    def refresh_host(self, node_key):
        # type: (object) -> None
        """按这一格现在的样子重新登记（面板增减、原地换方块之后），并排进待算。"""
        record = self.registry.get(node_key)
        if record is None:
            self._forget(node_key)
            return
        if self.definitions.status_values_of(record.device_id):
            kind = BLOCK
        elif self.definitions.accepts_parts(record.device_id) and self.catalog.parts_of(record):
            kind = PANELS
        else:
            kind = None
        if kind is None:
            self._forget(node_key)
            return
        if self._tracked.get(node_key) != kind:
            self._tracked[node_key] = kind
            self._order = None
        self._enqueue(node_key)

    def _forget(self, node_key):
        # type: (object) -> None
        if self._tracked.pop(node_key, None) is not None:
            self._order = None
        self._written.pop(node_key, None)
        self._queued.discard(node_key)
        self._drop_holding(node_key, keep=())
        if self._drop_panels(node_key, keep=()) and self.mark_panels is not None:
            self.mark_panels(node_key)

    def mark_all(self, *_ignored):
        # type: (object) -> None
        """全部排进待算（拓扑发布、带宽判定或运行时变了；参数是监听器给的，用不上）。"""
        for node_key in sorted(self._tracked, key=_as_key):
            self._enqueue(node_key)

    def _enqueue(self, node_key):
        # type: (object) -> None
        if node_key not in self._queued:
            self._queued.add(node_key)
            self._queue.append(node_key)

    # -- 推进 ------------------------------------------------------------

    def advance(self):
        # type: () -> int
        """算一帧：待算的先，再轮转复查 `sweep` 台，合计不超过 `budget`。返回算了几格。"""
        self._ticks += 1
        done = 0
        while self._queue and done < self.budget:
            node_key = self._queue.popleft()
            self._queued.discard(node_key)
            if node_key in self._tracked:
                self._evaluate(node_key)
                done += 1
        if not self._tracked:
            return done
        if self._order is None:
            self._order = sorted(self._tracked, key=_as_key)
            self._cursor = 0
        for _step in range(min(self.sweep, self.budget - done, len(self._order))):
            if self._cursor >= len(self._order):
                self._cursor = 0
            node_key = self._order[self._cursor]
            self._cursor += 1
            if node_key in self._tracked:
                self._evaluate(node_key)
                done += 1
        return done

    def _evaluate(self, node_key):
        # type: (object) -> None
        record = self.registry.get(node_key)
        if record is None or not record.loaded:
            return
        self.stats["evaluated"] += 1
        if self._tracked.get(node_key) == BLOCK:
            self._evaluate_block(record)
        else:
            self._evaluate_panels(record)

    def _online(self, ref, slot=None):
        # type: (object, object) -> object
        """设备状态的在线结论。问不到（抛了）、或结论还没出来且没沿用满 `hold_ticks` 帧，返回
        None：这次不改外观。`slot` 是面板的那一面，整方块设备为 None。"""
        try:
            status = self.status_of(ref)
        except Exception:
            self.stats["status_errors"] += 1
            return None
        hold_key = (ref.node_key, slot)
        if status.is_online or not _settling(status):
            self._holding.pop(hold_key, None)
            return bool(status.is_online)
        since = self._holding.setdefault(hold_key, self._ticks)
        if self._ticks - since < self.hold_ticks:
            self.stats["held"] += 1
            return None
        return False

    def _drop_holding(self, node_key, keep):
        # type: (object, object) -> None
        for hold_key in [hold_key for hold_key in self._holding
                         if hold_key[0] == node_key and hold_key[1] not in keep]:
            del self._holding[hold_key]

    # -- 整方块设备 ------------------------------------------------------

    def look_of(self, record):
        # type: (object) -> object
        """这台设备现在该是哪个外观状态；问不到返回 None。"""
        online = self._online(record.ref)
        if online is None:
            return None
        if online:
            return STATUS_ONLINE
        values = self.definitions.status_values_of(record.device_id)
        if STATUS_CONFLICT in values and self._conflicted(record.node_key):
            return STATUS_CONFLICT
        return STATUS_OFFLINE

    def _conflicted(self, node_key):
        # type: (object) -> bool
        if self.conflict_of is None:
            return False
        try:
            return bool(self.conflict_of(node_key))
        except Exception:
            return False

    def _evaluate_block(self, record):
        # type: (object) -> None
        node_key = record.node_key
        value = self.look_of(record)
        if value is None or self._written.get(node_key) == value:
            return
        current = None
        changes = {STATUS_STATE: value}
        if node_key not in self._written:
            current = self._read_states(node_key)
            if current is not None and looks_unplaced(current):
                # 还是没放过的默认组合（画的是图标那一套）：改成放过的，外观才跟着状态走
                changes[PLACED_HALF_STATE] = PLACED_HALF
                self.stats["unplaced"] += 1
            elif current is not None and current.get(STATUS_STATE) == value:
                self._written[node_key] = value
                self.stats["already"] += 1
                return
        writer = getattr(self.world, "update_block_states", None)
        ok = False
        if writer is not None:
            try:
                ok = bool(writer(node_key, changes, current))
            except Exception:
                ok = False
        if ok:
            self._written[node_key] = value
            self.stats["written"] += 1
        else:
            self.stats["write_failures"] += 1

    def _read_states(self, node_key):
        # type: (object) -> object
        reader = getattr(self.world, "block_states_at", None)
        if reader is None:
            return None
        try:
            states = reader(node_key)
        except Exception:
            return None
        return dict(states) if isinstance(states, dict) else None

    # -- 面板 ------------------------------------------------------------

    def _evaluate_panels(self, record):
        # type: (object) -> None
        node_key = record.node_key
        parts = self.catalog.parts_of(record)
        if not parts:
            self._forget(node_key)
            return
        changed = False
        faces = set()
        for face, _part_id in parts:
            faces.add(face)
            online = self._online(DeviceRef(node_key, record.generation, face), face)
            if online is None:
                continue
            part_key = PartKey(node_key, face)
            if self._panels.get(part_key) != (not online):
                self._panels[part_key] = not online
                changed = True
        self._drop_holding(node_key, keep=faces)
        if self._drop_panels(node_key, keep=faces):
            changed = True
        if changed:
            self.stats["panel_changes"] += 1
            if self.mark_panels is not None:
                self.mark_panels(node_key)

    def _drop_panels(self, node_key, keep):
        # type: (object, object) -> bool
        doomed = [part_key for part_key in self._panels
                  if part_key.node_key == node_key and part_key.slot not in keep]
        for part_key in doomed:
            del self._panels[part_key]
        return bool(doomed)

    def panel_offline(self, part_key):
        # type: (object) -> bool
        """这块面板上次算出来是不是离线（`PanelService.display_values` 用）；没算过按离线画。"""
        return bool(self._panels.get(PartKey.of(part_key), True))

    def diagnostics(self):
        # type: () -> dict
        return dict(self.stats, tracked=len(self._tracked), queued=len(self._queue),
                    offline_panels=sum(1 for offline in self._panels.values() if offline),
                    holding=len(self._holding))


def _settling(status):
    # type: (object) -> bool
    """设备状态是不是「结论还没出来」：原因里有 `HOLD_CODES` 的。"""
    return any(reason_code(reason) in HOLD_CODES for reason in status.reasons)
