# coding=utf-8
"""末影光缆的按面数据与方块状态维护（`.claude/13` §5，照蔚蓝管道，细节见 11 §3）。

## 两份数据，一起写

- 记录的 `config`——**拓扑只读这一份**（经 `catalog.CableFaces`）：`config["cuts"]` 被扳手
  切断的面（6 位掩码，第 i 位 = `FACING_NAMES[i]`），`config["parts"]` 挂着的面板
  （`{面名: 面板设备标识}`）。
- 方块实体 `endernet:cuts` / `endernet:parts`（字段名只在 `adapters/world.py` 里）——存档
  就是它。注册时（放置、区块加载）读进记录（`read_config`，由生命周期调）；改的时候先写
  方块实体、写成了才改记录（`write_faces`），然后对该格 `topology.mark_dirty`。反过来的话，
  方块实体没写进去、记录却变了，重进世界就对不上。

方块实体还拿不到时（区块刚加载，引擎给 None）记录先拿「六面全切断」的占位 config，
并打上 `ENTITY_PENDING_KEY`：光缆先孤立（与 `CableFaces.from_record` 对读不懂的数据同一取向
——读成「连着」，玩家切断的面会悄悄连回去），稍后重读。带着这个标记的格子不写方块状态、
也不接扳手。

## 方块状态只是外观

事实来源是拓扑。每面的期望值：在 `catalog.parts_of` 里 → 2（面板）；在
`topology.link_state().linked` 里 → 1（连接臂）；邻格读不到（没有记录、所在区块也没加载，
或邻格还在等方块实体）→ **保留原值**；其余 → 0。只在值变了时写，一格一次
`SetBlockStates` 写完，只写方块上声明过的键。

## 什么时候重算（`observe` 收生命周期变化）

| 变化 | 重算哪格 | 什么时候 |
| --- | --- | --- |
| 放下光缆（`ADDED`，来自放置事件） | 这格 | 本帧 |
| 区块加载（`ADDED` 补查 / `LOADED`） | 这格，**先重读方块实体** | `chunk_settle_ticks` 帧后（30 帧 = 1 秒，照蔚蓝 `Delay(1)`：太早读，区块边缘的方块会读成空气） |
| 邻居变化（`NEIGHBORS_AFFECTED`：放置、拆除、引擎的邻居事件都会产生） | 这格 | 下一帧（拆除 `Delay(0)` 后复查：届时按现状算，快拆快放不会误清） |
| 面板离开（`PART_DETACHED`） | 宿主 | 本帧 |
| 扳手切断 / 拆面板（`write_faces`） | 这格与相邻的光缆 | 本帧 |
| 原地换方块（`HOST_CONVERTED`，底座 ↔ 光缆） | 这格与相邻的光缆 | 本帧 |

**面板底座**（`endernet:part_holder`，2026-11-10）与光缆同一套：方块实体、按面数据、方块
状态（每面只有 0 / 2）都归这里，判据是 `definitions.has_face_data`。底座不入网，期望值里
永远没有 1。原地换方块的数据一半是 `swap_block`（世界换方块 + 搬字段 + 改 config），注册表
与通知那一半是 `DeviceLifecycle.convert`。

邻居事件里「邻格还是那个方块、只改了状态」（`from == to`）在适配器就滤掉了：写方块状态
本身也会触发邻居事件。每帧在拓扑推进（发布）之后处理到期的格子（`advance`，由
`ServerBootstrap.tick` 调），按 `cells_per_tick` 限量。

## 按面数据变了要告诉谁（阶段 D）

`on_faces_changed(node_key)`：记录里的切断面 / 面板刚被改过（`write_faces` 写成了，或重读
方块实体读出了不一样的东西）。启动层接它刷新面板外观（`SlotDisplayTracker.mark_panels`）、
登记面板的终端库存（`TerminalInventoryArchive.discovered`）。回调抛异常只计数，不影响写入。
"""

from ...common.cable_shapes import SIDE_ARM, SIDE_NONE, SIDE_PANEL, side_state, sides_from_states
from ...common.identity import FACING_NAMES, face_mask
from ..runtime import SOURCE_BACKFILL, ChangeKind
from .catalog import CABLE_CUTS_KEY, CABLE_PARTS_KEY, CableFaces

#: 记录 `config` 里「方块实体还没读到，这是占位」的标记
ENTITY_PENDING_KEY = "entity_pending"
#: 六面全切断的掩码
ALL_FACES_MASK = (1 << len(FACING_NAMES)) - 1

#: `_read_entity` 的「方块实体还拿不到」
_UNREADABLE = object()

try:  # pragma: no cover - 仅在 Python 2 下成立
    _TEXT_TYPES = (str, unicode)
except NameError:  # pragma: no cover - 仅在 Python 3 下成立
    _TEXT_TYPES = (str,)


def config_from_entity(cuts, parts):
    # type: (object, object) -> dict
    """方块实体的两份原值 → 记录的 config 字段。

    字段不存在（引擎给 None）就是「没切断、没面板」。切断掩码读不懂的**原样放**，由
    `CableFaces.from_record` 按六面全切断处理；面板表只留面名合法、标识非空的条目
    （是不是登记过的面板、这种光缆能不能挂，由 `catalog.parts_of` 判）。
    """
    config = {CABLE_CUTS_KEY: 0 if cuts is None else cuts, CABLE_PARTS_KEY: {}}
    if isinstance(parts, dict):
        for face, part_id in parts.items():
            if face in FACING_NAMES and isinstance(part_id, _TEXT_TYPES) and part_id:
                config[CABLE_PARTS_KEY][face] = part_id
    return config


def pending_config():
    # type: () -> dict
    """方块实体还拿不到时的占位 config：六面全切断（光缆先孤立）并打上标记。"""
    return {CABLE_CUTS_KEY: ALL_FACES_MASK, CABLE_PARTS_KEY: {}, ENTITY_PENDING_KEY: True}


def is_pending(record):
    # type: (object) -> bool
    """这条记录的切断面与面板是不是还没从方块实体读到。"""
    return bool((getattr(record, "config", None) or {}).get(ENTITY_PENDING_KEY))


class _Due(object):
    """一格待重算：何时到期、要不要先重读方块实体、已经重读失败几次。"""

    __slots__ = ("ready", "reread", "attempts")

    def __init__(self, ready, reread=False, attempts=0):
        # type: (int, bool, int) -> None
        self.ready = ready
        self.reread = reread
        self.attempts = attempts


class CableStateKeeper(object):
    """光缆的按面数据读写与方块状态维护。可以脱离引擎构造：世界读写全走 `world` 的方法。

    `world` 要的方法（缺哪个，对应那一步就跳过并计数，不抛）：
    `cable_entity_fields` / `write_cable_entity_fields`（方块实体），`block_states_at` /
    `update_block_states`（方块状态），`check_chunk_state`（邻格的区块加载了没有）。
    `clock` 给逻辑帧（`LogicalClock.current_tick()`）；不给就按 0 算。
    """

    def __init__(
        self,
        definitions,
        registry,
        catalog,
        topology,
        world,
        clock=None,
        cells_per_tick=32,
        chunk_settle_ticks=30,
        neighbor_delay_ticks=1,
        entity_retries=5,
    ):
        # type: (object, object, object, object, object, object, int, int, int, int) -> None
        self.definitions = definitions
        self.registry = registry
        self.catalog = catalog
        self.topology = topology
        self.world = world
        self.clock = clock
        #: 每帧最多重算几格（一格至多一次读状态 + 一次写状态）
        self.cells_per_tick = int(cells_per_tick)
        #: 区块加载后等几帧再读（30 帧 = 1 秒，照蔚蓝 `Delay(1)`）
        self.chunk_settle_ticks = int(chunk_settle_ticks)
        #: 邻居变化（含拆除）后等几帧再算（照蔚蓝拆除的 `Delay(0)`：下一帧复查现状）
        self.neighbor_delay_ticks = int(neighbor_delay_ticks)
        #: 方块实体一直拿不到时最多重读几次（每次隔 `chunk_settle_ticks`）
        self.entity_retries = int(entity_retries)
        #: `NodeKey -> _Due`
        self._due = {}
        #: `node_key -> None`：记录的按面数据刚被改过（见模块说明）。启动层注入。
        self.on_faces_changed = None
        self.stats = dict.fromkeys(
            (
                "requested",
                "refreshed",
                "written",
                "unchanged",
                "skipped",
                "states_unreadable",
                "write_failures",
                "entity_reloaded",
                "entity_retries",
                "entity_unreadable",
                "entity_errors",
                "entity_writes",
                "entity_write_failures",
                "listener_errors",
                "swaps",
                "swap_failures",
            ),
            0,
        )

    def _now(self):
        # type: () -> int
        return self.clock.current_tick() if self.clock is not None else 0

    # -- 方块实体 --------------------------------------------------------

    def read_config(self, node_key, device_id):
        # type: (object, str) -> object
        """注册时的 config（`DeviceLifecycle.set_config_reader` 的口子）。不是光缆返回 None。

        方块实体还拿不到 → 占位 config（`pending_config`）；世界没有这个读口、或读的时候
        抛了 → 当作「没切断、没面板」（计 `entity_errors`）。
        """
        if not self.definitions.has_face_data(device_id):
            return None
        fields = self._read_entity(node_key)
        if fields is _UNREADABLE:
            return pending_config()
        return config_from_entity(*fields)

    def _read_entity(self, node_key):
        # type: (object) -> object
        reader = getattr(self.world, "cable_entity_fields", None)
        if reader is None:
            return None, None
        try:
            fields = reader(node_key)
        except Exception:
            self.stats["entity_errors"] += 1
            return None, None
        if fields is None:
            return _UNREADABLE
        return fields

    def write_faces(self, node_key, cuts=None, parts=None):
        # type: (object, object, object) -> bool
        """改一格光缆的切断面（`cuts`，掩码）与面板（`parts`，`{面名: 面板标识}`）。

        给 None 的那一份保持不变。**先写方块实体，写成了才改记录**，然后对该格标脏、
        请它与相邻的光缆重算外观。返回是否写成；写不成什么都不改。
        """
        record = self.registry.get(node_key)
        if record is None or not self.definitions.has_face_data(record.device_id):
            return False
        faces = CableFaces.from_record(record)
        new_cuts = face_mask(faces.cut) if cuts is None else int(cuts)
        new_parts = dict(faces.parts) if parts is None else dict(parts)
        writer = getattr(self.world, "write_cable_entity_fields", None)
        ok = False
        if writer is not None:
            try:
                ok = bool(writer(node_key, new_cuts, new_parts))
            except Exception:
                ok = False
        if not ok:
            self.stats["entity_write_failures"] += 1
            return False
        self.stats["entity_writes"] += 1
        self.registry.update_config(node_key, {
            CABLE_CUTS_KEY: new_cuts,
            CABLE_PARTS_KEY: new_parts,
            ENTITY_PENDING_KEY: None,
        })
        self.topology.mark_dirty(node_key)
        self._touch_around(node_key, include_self=True)
        self._faces_changed(node_key)
        return True

    def _faces_changed(self, node_key):
        # type: (object) -> None
        listener = self.on_faces_changed
        if listener is None:
            return
        try:
            listener(node_key)
        except Exception:
            self.stats["listener_errors"] += 1

    def swap_block(self, node_key, device_id, cuts=0):
        # type: (object, str, int) -> bool
        """面板宿主原地换方块（面板底座 ↔ 光缆，2026-11-10）的「数据」一半：世界里换方块并把
        面板表、切断掩码与按面字段（库存标记、面板配置）搬进新方块实体
        （`world.swap_faced_block`），写成了才改记录的 config。

        注册表的型号与下游通知由调用方随后经 `DeviceLifecycle.convert` 发
        （`ChangeKind.HOST_CONVERTED`）。面板原样保留：`parts` 取记录现在的那份。返回是否写成。
        """
        record = self.registry.get(node_key)
        if record is None or not self.definitions.has_face_data(record.device_id):
            return False
        if not self.definitions.has_face_data(device_id):
            return False
        parts = dict(CableFaces.from_record(record).parts)
        swapper = getattr(self.world, "swap_faced_block", None)
        ok = False
        if swapper is not None:
            try:
                ok = bool(swapper(node_key, device_id, int(cuts), parts))
            except Exception:
                ok = False
        if not ok:
            self.stats["swap_failures"] += 1
            return False
        self.stats["swaps"] += 1
        self.registry.update_config(node_key, {
            CABLE_CUTS_KEY: int(cuts),
            CABLE_PARTS_KEY: parts,
            ENTITY_PENDING_KEY: None,
        })
        return True

    def set_cut(self, node_key, face, cut):
        # type: (object, str, bool) -> bool
        """把一格光缆的某一面置为切断 / 不切断（只改这一面）。"""
        record = self.registry.get(node_key)
        if record is None:
            return False
        cuts = set(CableFaces.from_record(record).cut)
        if cut:
            cuts.add(face)
        else:
            cuts.discard(face)
        return self.write_faces(node_key, cuts=face_mask(cuts))

    # -- 方块状态 --------------------------------------------------------

    def expected_sides(self, record, current=None):
        # type: (object, dict) -> dict
        """`{面名: 期望值}`，值为 None 表示邻格读不到、保留原状态。`current` 是现在的
        `{面名: 状态值}`（判「读不到的邻格要不要问引擎」用，可以不给）。"""
        current = current or {}
        parts = dict(self.catalog.parts_of(record))
        linked = set(self.topology.link_state(record.node_key).linked)
        out = {}
        for face, neighbor_key in zip(FACING_NAMES, record.node_key.neighbors()):
            if face in parts:
                out[face] = SIDE_PANEL
            elif neighbor_key in linked:
                out[face] = SIDE_ARM
            elif self._unknown_neighbor(neighbor_key, current.get(face, SIDE_NONE)):
                out[face] = None
            else:
                out[face] = SIDE_NONE
        return out

    def _unknown_neighbor(self, neighbor_key, current_value):
        # type: (object, int) -> bool
        """邻格是不是读不到：没有记录且所在区块没加载，或邻格还在等方块实体。

        原值本来就是 0 的面不问引擎（期望也是 0，问了也不会写）。
        """
        record = self.registry.get(neighbor_key)
        if record is not None:
            return is_pending(record)
        if current_value == SIDE_NONE:
            return False
        checker = getattr(self.world, "check_chunk_state", None)
        if checker is None:
            return False
        try:
            return not checker(neighbor_key)
        except Exception:
            return True

    def current_sides(self, record):
        # type: (object) -> dict
        """这格现在的 `{面名: 状态值}`：读方块状态；读不到就按期望值（读不到的邻格按 0）。

        精确交互用它决定这格现在有哪些部件可点（`cable_shapes.pick`）。
        """
        states = self._read_states(record.node_key)
        if states is not None:
            return sides_from_states(states)
        expected = self.expected_sides(record)
        return dict((face, value or SIDE_NONE) for face, value in expected.items())

    def _read_states(self, node_key):
        # type: (object) -> object
        reader = getattr(self.world, "block_states_at", None)
        if reader is None:
            return None
        try:
            states = reader(node_key)
        except Exception:
            return None
        return states if isinstance(states, dict) else None

    # -- 排队与推进 ------------------------------------------------------

    def request(self, node_key, delay=0, reread=False):
        # type: (object, int, bool) -> None
        """请这格在 `delay` 帧后重算（`reread` 为真则先重读方块实体）。

        已经在队里的取较晚的到期时间：区块加载要等的那 1 秒不会被随后的邻居变化提前。
        """
        ready = self._now() + max(0, int(delay))
        entry = self._due.get(node_key)
        if entry is None:
            self._due[node_key] = _Due(ready, reread)
        else:
            entry.ready = max(entry.ready, ready)
            entry.reread = entry.reread or reread
        self.stats["requested"] += 1

    def _touch_around(self, node_key, include_self):
        # type: (object, bool) -> None
        """这格的按面数据变了：它自己与相邻的光缆都要重算（两格之间连不连看双方）。"""
        if include_self:
            self.request(node_key)
        for neighbor_key in node_key.neighbors():
            record = self.registry.get(neighbor_key)
            if record is not None and self.definitions.is_cable(record.device_id):
                self.request(neighbor_key)

    def observe(self, change):
        # type: (object) -> None
        """生命周期变化的消费者（`ServerBootstrap._emit`）：决定哪格什么时候重算。"""
        node_key = change.node_key
        if node_key is None:
            return
        kind = change.kind
        if kind == ChangeKind.PART_DETACHED:
            # `device_id` 是面板，`node_key` 是宿主光缆
            self.request(node_key)
            return
        if change.device_id is None or not self.definitions.has_face_data(change.device_id):
            return
        if kind == ChangeKind.HOST_CONVERTED:
            # 原地换了方块（底座 ↔ 光缆）：新方块的状态全是默认值，本帧按记录重算；相邻光缆
            # 的连接臂随拓扑变，也一起算
            self._touch_around(node_key, include_self=True)
        elif kind in (ChangeKind.REMOVED, ChangeKind.UNLOADED):
            # 拆掉的格子没有可写的了；卸载的格子写不进去，重新加载时再算
            self._due.pop(node_key, None)
        elif kind == ChangeKind.ADDED:
            if change.detail == SOURCE_BACKFILL:
                self.request(node_key, self.chunk_settle_ticks, reread=True)
            else:
                self.request(node_key)
        elif kind == ChangeKind.LOADED:
            self.request(node_key, self.chunk_settle_ticks, reread=True)
        elif kind == ChangeKind.NEIGHBORS_AFFECTED:
            self.request(node_key, self.neighbor_delay_ticks)

    def advance(self, tick=None):
        # type: (int) -> int
        """处理到期的格子，最多 `cells_per_tick` 格。返回处理了几格。"""
        if not self._due:
            return 0
        if tick is None:
            tick = self._now()
        due = sorted(
            (entry.ready, node_key.as_key(), node_key)
            for node_key, entry in self._due.items()
            if entry.ready <= tick
        )
        batch = [(node_key, self._due.pop(node_key)) for _r, _o, node_key in due[: self.cells_per_tick]]
        # 先把这一批要重读的方块实体都读完，再算方块状态：同一个区块里一起加载的几格，
        # 不会有哪格先按邻格的旧数据写一遍、下一帧再改回来。
        ready = []
        for node_key, entry in batch:
            record = self.registry.get(node_key)
            if record is None or not record.loaded or not self.definitions.has_face_data(record.device_id):
                self.stats["skipped"] += 1
                continue
            if (entry.reread or is_pending(record)) and not self._reload_entity(record, entry, tick):
                continue
            ready.append(record)
        for record in ready:
            self._refresh(record)
        return len(batch)

    def _refresh(self, record):
        # type: (object) -> None
        node_key = record.node_key
        self.stats["refreshed"] += 1
        states = self._read_states(node_key)
        if states is None:
            self.stats["states_unreadable"] += 1
            return
        expected = self.expected_sides(record, sides_from_states(states))
        changes = {}
        for face in FACING_NAMES:
            key = side_state(face)
            value = expected[face]
            # 只写方块上声明过的键；读不到的邻格保留原值；没变的不写
            if key not in states or value is None or states.get(key) == value:
                continue
            changes[key] = value
        if not changes:
            self.stats["unchanged"] += 1
            return
        writer = getattr(self.world, "update_block_states", None)
        ok = False
        if writer is not None:
            try:
                ok = bool(writer(node_key, changes, states))
            except Exception:
                ok = False
        self.stats["written" if ok else "write_failures"] += 1

    def _reload_entity(self, record, entry, tick):
        # type: (object, _Due, int) -> bool
        """重读方块实体并更新记录。返回能不能接着算方块状态（还拿不到就排下一次）。"""
        node_key = record.node_key
        fields = self._read_entity(node_key)
        if fields is _UNREADABLE:
            if entry.attempts + 1 < self.entity_retries:
                self._due[node_key] = _Due(tick + self.chunk_settle_ticks, True, entry.attempts + 1)
                self.stats["entity_retries"] += 1
            else:
                # 一直读不到：保持孤立（占位 config 不动），等下一次加载或邻居变化再试
                self.stats["entity_unreadable"] += 1
            return False
        config = config_from_entity(*fields)
        config[ENTITY_PENDING_KEY] = None
        if self.registry.update_config(node_key, config):
            self.stats["entity_reloaded"] += 1
            self.topology.mark_dirty(node_key)
            self._touch_around(node_key, include_self=False)
            self._faces_changed(node_key)
        return True

    # -- 其它 ------------------------------------------------------------

    def reset(self):
        # type: () -> None
        """停机：丢掉排着的格子（重启后区块重新加载会再排）。"""
        self._due = {}

    def pending_count(self):
        # type: () -> int
        return len(self._due)

    def diagnostics(self):
        # type: () -> dict
        return dict(self.stats, queued=len(self._due))
