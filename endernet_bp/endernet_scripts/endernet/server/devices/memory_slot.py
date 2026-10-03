# coding=utf-8
"""内存槽状态集合：网络的内存容量与内存槽能耗的唯一权威。

**本模块是领域层**：不 import 引擎（`test_import_boundaries` 静态拦着）。
引擎访问全部经注入的契约——`ContainerProvider.container_at(node_key)`
（生产实现在 `adapters/containers_engine.py`，测试传 `containers.FakeContainer`）。

## 它替换掉了什么

在这之前，内存容量是一个**替身**：`crafting/capacity.py` 的
`memory_capacity()` 数网络里有几台内存槽，乘 `OrderPolicy.bytes_per_memory_slot`
（默认 1024 B）。那个数不是玩法数值，而是「R04 的内存条规格未定」时
唯一能开工的量级。内存条物品（`plans/items.md` 的「内存」一节，2026-10-03）
一落地，容量就有了权威来源：**网络里每台内存槽方块容器中**插着的内存条，
各按 `MemorySpec.capacity_bytes` 合计。替身随之删除。

能源同理：`blocks.md` 写的是「耗能 (内存条个数 × 8) EN/t」，而今天
`energy.set_device_state(unit_count=...)` 的 `unit_count` 就是这里数出来的根数。

## 权威在方块容器，不在本模块

与 `PatternStore` 同一条纪律（**派生索引在恢复后重建**）：本模块**不存档**，
每次重读方块容器。三个后果都是想要的：

- 存档不需要为内存写迁移——内存条是方块容器里的实物，容器随方块一起落盘；
- 插拔、整块换掉、别的模组动过容器，都会在下一帧的重读里自然生效；
- 内存里的这份投影随时可以丢掉重建。

## 变更检测：事件 -> 脏标记 -> 去抖重读

与 `crafting/store.py` **同一套机制、同一套理由**（ModAPI 明说容器事件
「分多次触发并且顺序不定」，因此事件只当脏标记）。

**常量在这里另写一份，不 import `crafting/store.py`**：那是 crafting 层，
而 `crafting/store.py:69` 已经 `from ..devices.capabilities import`——
反向 import 会成环（分层由 `test_import_boundaries.py` 静态强制）。
两侧数值今天相同，但它们是**两条独立的节拍**，改一侧不会连累另一侧。

## 读不到容器时保留上一次的读数

区块卸载、容器暂时读不出来，都**不是**「内存条被拔了」。因此 `_read_slots`
在拿不到适配器时保留上一次读到的内容、只标 `unreadable`。

与 `PatternStore` 唯一的差别在**「从没读到过」**这一支：
- 本模块：`has_reading()` 为假 → 容量算 **0**、能耗报 **None（待确认）**。
  理由与 `capacity.py` 的既有口径一致——「它的状态没人能确认，而内存是要
  拿去开工的」，宁可少算不可多算。
- 读到过、之后读不到：沿用上一次的读数（与内存条实物还在容器里这一事实相符），
  免得一次瞬时读失败让整个网络的内存凭空归零、把玩家刚提交的订单顶回去。

## 只收内存条

`accepts_put()` 供 `adapters/events.py` 的两条放入路径（玩家拖入、漏斗漏入）
共用：内存槽只接受**已注册为游戏物品**的内存条，其余一律拒收
（`plans/items.md` 的内存一节，2026-10-03 定）。拒收发生在放入那一刻；
至于「早就躺在里面的非内存物品」（别的模组塞的、规则生效前放进去的），
这里不搬动它们，只在 `invalid` 里报出来、容量上不计。
"""

from ...common.definitions import CATEGORY_MEMORY, MEMORY_SLOT_COUNT
from ...common.item_codec import is_empty_item, item_key_from_dict
from ..runtime.lifecycle import ChangeKind

# -- 节拍 ------------------------------------------------------------------

#: 收到变更之后再静默这么多帧才重读。理由同 `crafting/store.py`
#: 的 `PATTERN_SETTLE_FRAMES`（一次拖入会派发多条事件，且顺序不定）。
MEMORY_SETTLE_FRAMES = 2

#: 一台内存槽最多这么久没被重读过，**即使变更一直不停**。
#: 取与样板那边相同的量级（40 帧 ≈ 1.3 秒 @30Hz）；**不是玩法数值**。
MEMORY_MAX_STALE_FRAMES = 40

#: 每帧最多重读几台内存槽。重读要读满 10 个槽，是真实的引擎调用。
MEMORY_REFRESH_BUDGET = 4


def is_memory_slot(device_id, definitions=None):
    # type: (str, object) -> bool
    """这台设备是不是内存槽。

    **按分类判定，不按设备名**：分类的真相只有定义表一处
    （与 `crafting/capacity.py` 筛成员时用的是同一个 `CATEGORY_MEMORY`）。
    定义表缺失（离线夹具）时退到「标识就叫 `endernet:memory_slot`」。
    """
    if definitions is not None:
        definition = definitions.devices.get(device_id)
        if definition is not None:
            return definition.category == CATEGORY_MEMORY
    return device_id == "endernet:memory_slot"


def _slot_count_of(adapter, fallback):
    # type: (object, int) -> int
    """一个容器适配器有多少格。

    **两个方法名都要试**，这是 13-01 那套契约的既有形状：
    `ContainerAdapter.slots_count()` 是通用只读面（假容器实现它），
    `SlotBackedContainer.slot_count()` 是按槽读写面（引擎容器实现它）。
    只认一个的话，「离线夹具通过、真机槽数 0」这种偏差只能在游戏里暴露。

    **与 `crafting/store.py:container_slot_count` 是同一条规则的两个副本**，
    不是漏改：跨层 import 会成环（见模块 docstring）。拿不到时退回定义表
    那条已定案的 10——它同时就是 `gen_blocks.py` 写进方块容器的
    `container_size`，因此这个回退不可能与真实格数分叉。
    """
    for name in ("slot_count", "slots_count"):
        counter = getattr(adapter, name, None)
        if not callable(counter):
            continue
        size = counter()
        if size is None:
            # `None` 是文档允许的答案（不可枚举，如流体罐）。
            continue
        try:
            size = int(size)
        except (TypeError, ValueError):
            continue
        if size > 0:
            return size
    return int(fallback)


# -- 一根内存条 ------------------------------------------------------------


class MemoryStickEntry(object):
    """一个槽里插着的一根内存条。"""

    __slots__ = ("slot", "memory_id", "capacity_bytes", "count")

    def __init__(self, slot, memory_id, capacity_bytes, count=1):
        # type: (int, str, int, int) -> None
        self.slot = int(slot)
        self.memory_id = memory_id
        #: 该型号一根多少字节（定义表的 `MemorySpec.capacity_bytes`）。
        self.capacity_bytes = int(capacity_bytes)
        #: 引擎报的物品个数。**正常恒为 1**（堆叠上限 1）；大于 1 属异常，
        #: 记进诊断但**不放大容量**（见模块 docstring）。
        self.count = int(count)

    def to_dict(self):
        # type: () -> dict
        return {
            "slot": self.slot,
            "memory_id": self.memory_id,
            "capacity_bytes": self.capacity_bytes,
            "count": self.count,
        }

    def __repr__(self):
        return "MemoryStickEntry(slot=%d, %s, %d B)" % (
            self.slot,
            self.memory_id,
            self.capacity_bytes,
        )


# -- 一台内存槽 ------------------------------------------------------------


class MemorySlotState(object):
    """一台内存槽当前插着什么。"""

    __slots__ = (
        "ref",
        "device_id",
        "loaded",
        "slot_count",
        "entries",
        "invalid",
        "unreadable",
        "dirty_tick",
        "refresh_tick",
        "readings",
        "revision",
        "stats",
    )

    def __init__(self, ref, device_id, slot_count=0):
        # type: (object, str, int) -> None
        self.ref = ref
        self.device_id = device_id
        self.loaded = True
        #: 这台设备的容器格数。**由容器回答**（`_slot_count_of`），
        #: 答不出时退回定义表的 10。
        self.slot_count = int(slot_count)
        #: `slot -> MemoryStickEntry`。只放**认得出来的内存条**。
        self.entries = {}
        #: `slot -> {"code", "detail"}`。槽里有东西但不是本模组的内存条。
        #: **与「空槽」分开**：空槽是没东西，这里是「有东西但不提供内存」。
        self.invalid = {}
        #: 读不到容器的原因（空串表示读得到）。**不清空 `entries`**。
        self.unreadable = ""
        #: 最后一次收到变更的帧号。**-1 表示「从没收到过」**，不是 0：
        #: 逻辑时钟的读数可以是 0（启动头几帧、离线测试起点）。
        self.dirty_tick = -1
        #: 最后一次尝试重读的帧号。-1 表示从没试过。
        #: **它是「试过」不是「读到」**——试过但失败也要记，否则
        #: `_is_due` 会把一台读不到的设备判成「从没读过」而每帧重试。
        self.refresh_tick = -1
        #: **成功读到过几次。** 与 `refresh_tick` 分开：读失败也算「试过」，
        #: 但「有没有读到过」是另一件事——`unit_count_of` 靠它把
        #: 「从没读到过」报成 `None`（待确认），而不是伪造一个 0 根。
        self.readings = 0
        #: 内容版本，每次重读发现变化就 +1。
        self.revision = 0
        self.stats = {
            "refreshes": 0,
            "changes": 0,
            "unreadable": 0,
            "invalid_slots": 0,
            "overstacked_slots": 0,
            "forced_refreshes": 0,
            "errors": 0,
        }

    # -- 查询 ----------------------------------------------------------

    @property
    def node_key(self):
        return self.ref.node_key

    def has_reading(self):
        # type: () -> bool
        """**成功**读到过容器没有。

        **它不等于 `is_usable()`**：后者还要求此刻读得到。也**不等于**
        `refresh_tick >= 0`——那是「试过」，试过而失败仍然是「不知道」。
        """
        return self.readings > 0

    def is_usable(self):
        # type: () -> bool
        """当前能不能拿它记账（已加载且这一次读得到）。

        未加载不等于被拆掉（与驱动器、样板同一条纪律）。
        """
        return self.loaded and not self.unreadable

    def sticks(self):
        # type: () -> int
        """插着几根内存条。**按占用的槽数计**，不累加物品 `count`。"""
        return len(self.entries)

    def bytes(self):
        # type: () -> int
        """这些内存条合计多少字节。"""
        return sum(entry.capacity_bytes for entry in self.entries.values())

    def entry_at(self, slot):
        # type: (int) -> MemoryStickEntry
        return self.entries.get(int(slot))

    def memory_ids(self):
        # type: () -> list
        """插着的型号清单，按槽位排序。**证据留给诊断，不留计算**。"""
        return [self.entries[slot].memory_id for slot in sorted(self.entries)]

    def content_fingerprint(self):
        # type: () -> tuple
        """内容指纹，用于判断「这次重读有没有读出不一样的东西」。

        不含 `loaded` / `unreadable` / 帧号：那些是设备状态，不是容器内容。
        """
        return (
            tuple(
                (slot, self.entries[slot].memory_id, self.entries[slot].count)
                for slot in sorted(self.entries)
            ),
            tuple(
                (slot, self.invalid[slot]["code"]) for slot in sorted(self.invalid)
            ),
        )

    def to_dict(self):
        # type: () -> dict
        return {
            "node_key": self.node_key.as_key(),
            "device_id": self.device_id,
            "generation": self.ref.generation,
            "loaded": self.loaded,
            "slot_count": self.slot_count,
            "sticks": self.sticks(),
            "capacity_bytes": self.bytes(),
            "memory_ids": self.memory_ids(),
            "invalid": [
                {"slot": slot, "error": dict(self.invalid[slot])}
                for slot in sorted(self.invalid)
            ],
            "unreadable": self.unreadable,
            "stats": dict(self.stats),
        }

    def __repr__(self):
        return "MemorySlotState(%s @ %r, %d 根 / %d B)" % (
            self.device_id,
            self.node_key,
            self.sticks(),
            self.bytes(),
        )


# -- 集合 ------------------------------------------------------------------


class MemorySlotStore(object):
    """内存槽状态集合。

    与 `StorageDriverStore` / `PortStore` / `PatternStore` 同地位：
    由启动层持有，网络断开、主控移除、运行时重建都不等于内存条消失。
    """

    def __init__(self, definitions, registry, containers=None,
                 on_load_changed=None):
        # type: (object, object, object, object) -> None
        #: 定义表。读三处：判分类、判「这根算不算内存条」、取型号容量。
        self.definitions = definitions
        self.registry = registry
        #: `ContainerProvider`：位置 -> ContainerAdapter。生产实现在
        #: `adapters/containers_engine.py`，测试传 `containers.FakeContainer`。
        #: **可为 None**，此时所有需要容器的动作明确报读不到，而不是当成空槽。
        self.containers = containers
        #: 读数变化后的钩子（`bootstrap._note_memory_load_changed`），
        #: 用来把根数推给能源服务。与 `StorageDriverStore.on_load_changed`
        #: 同一形状；**不通知负载重算**——内存槽的带宽是固定 2u，不随根数变。
        self.on_load_changed = on_load_changed

        self._states = {}
        self.tick = 0
        self.stats = {
            "created": 0,
            "dropped": 0,
            "load_marks": 0,
            "marks": 0,
            "refreshes": 0,
            "rejections": 0,
        }

    # -- 状态 ----------------------------------------------------------

    def ensure(self, ref, device_id=None):
        # type: (object, str) -> MemorySlotState
        """取或建一台内存槽的状态。同一代次重复调用是幂等的。"""
        existing = self.get_by_ref(ref)
        if existing is not None:
            return existing
        if device_id is None:
            # 只在没给型号时才查注册表（与 `PortStore.ensure` 同一形状）。
            record = self.registry.get_by_ref(ref)
            device_id = record.device_id if record is not None else ""
        if not is_memory_slot(device_id, self.definitions):
            return None
        state = MemorySlotState(ref, device_id, MEMORY_SLOT_COUNT.value)
        self._states[ref.node_key.as_key()] = state
        self.stats["created"] += 1
        # **建好就读一次**：`slot_count` 由容器回答，容量也立刻有值，
        # 否则这台设备会在下一次重读之前一直报 0 根、0 字节——
        # 而玩家刚插进去的内存条会像没生效一样。
        self.refresh(ref)
        return state

    def get(self, node_key):
        # type: (object) -> MemorySlotState
        state = self._states.get(node_key.as_key())
        if state is None or not is_memory_slot(state.device_id, self.definitions):
            return None
        return state

    def get_by_ref(self, ref):
        # type: (object) -> MemorySlotState
        """按实例代次取。**代次不符即失效**——同位置换了方块就不该再认。"""
        state = self._states.get(ref.node_key.as_key())
        if state is None or state.ref.generation != ref.generation:
            return None
        return state

    def drop(self, ref):
        # type: (object) -> MemorySlotState
        """拆除：移除状态。

        **内存条本身不丢**——它们是方块容器里的实物物品，容器随方块一起
        按游戏规则处理（掉落、留存），不在本模块的职责里。
        """
        state = self.get_by_ref(ref)
        if state is None:
            return None
        self._states.pop(ref.node_key.as_key(), None)
        self.stats["dropped"] += 1
        return state

    def mark_loaded(self, node_key, loaded):
        # type: (object, bool) -> bool
        """区块加载状态变化。**容器内容一字不改**。"""
        state = self.get(node_key)
        if state is None or state.loaded == bool(loaded):
            return False
        state.loaded = bool(loaded)
        self.stats["load_marks"] += 1
        return True

    def mark_all_unloaded(self):
        # type: () -> int
        """停机：全部标为未加载。**不清读数、不写任何一份存档。**"""
        count = 0
        for state in self._states.values():
            if state.loaded:
                state.loaded = False
                count += 1
        return count

    def states(self):
        # type: () -> list
        return [
            self._states[key]
            for key in sorted(self._states, key=lambda item: tuple(item))
        ]

    # -- 容量与根数 ----------------------------------------------------

    def bytes_of(self, node_key):
        # type: (object) -> int
        """这台内存槽提供多少字节。**读不到／从没读到过一律算 0。**

        用于容量：宁可少算，不可多算（见模块 docstring）。
        要区分「0 根」与「读不到」，用 `unit_count_of`。
        """
        state = self.get(node_key)
        if state is None:
            return 0
        return state.bytes()

    def sticks_of(self, node_key):
        # type: (object) -> int
        """这台内存槽插着几根内存条。口径同 `bytes_of`。"""
        state = self.get(node_key)
        if state is None:
            return 0
        return state.sticks()

    def unit_count_of(self, ref):
        # type: (object) -> object
        """能源要的 `unit_count`：根数；**从没读到过则是 `None`（待确认）**。

        返回 `None` 而不是 0 是有意的：`EnergyService.set_device_state` 把
        `None` 当作「不可知」，判定停在待确认，而不是伪造一个「这台设备
        一根内存条都没有、所以不耗电」的结论。
        """
        state = self.get_by_ref(ref)
        if state is None or not state.has_reading():
            return None
        return state.sticks()

    # -- 放入判定 ------------------------------------------------------

    def accepts_put(self, device_id, item_name):
        # type: (str, str) -> bool
        """这个物品能不能放进这台设备的容器。**只答内存槽那一部分。**

        不是内存槽一律放行——别家容器（被动接口、取样板口、驱动器）
        各有各的规则，不归本模块管；`events._veto_or_mark` 会逐个 store 问。
        """
        if not is_memory_slot(device_id, self.definitions):
            return True
        if not item_name:
            # 读不出物品名就不拦（与 `_veto_or_mark` 同一口径）。
            return True
        return bool(self.definitions.has_memory(item_name))

    # -- 生命周期 ------------------------------------------------------

    def observe(self, change):
        # type: (object) -> int
        """从设备生命周期变化推导状态。**唯一入口**，与拓扑同一批变化。"""
        kind = change.kind
        device_id = change.device_id
        if kind == ChangeKind.ADDED:
            if not is_memory_slot(device_id, self.definitions):
                return 0
            return 1 if self.ensure(change.ref, device_id) is not None else 0
        if kind == ChangeKind.REMOVED:
            if not is_memory_slot(device_id, self.definitions):
                return 0
            return 1 if self.drop(change.ref) is not None else 0
        if kind in (ChangeKind.LOADED, ChangeKind.UNLOADED):
            if not is_memory_slot(device_id, self.definitions):
                return 0
            if not self.mark_loaded(change.node_key, kind == ChangeKind.LOADED):
                return 0
            # 重新加载之后内容可能已经变了（别的模组、退出前的手工调整），
            # 因此挂载变化也标脏，而不是信任内存里那一份。
            self.mark_dirty(change.node_key)
            return 1
        return 0

    # -- 变更信号 ------------------------------------------------------

    def mark_dirty(self, node_key, tick=None):
        # type: (object, int) -> bool
        """这台内存槽的容器变了。**只记一笔**，不在这里读容器。

        引擎无关的入口：适配器把容器事件翻译成位置后调它。不立刻读的
        两个理由同 `PatternStore.mark_dirty`：事件顺序不可依赖；
        事件触发时容器内容可能还没落定。
        """
        state = self.get(node_key)
        if state is None:
            return False
        state.dirty_tick = self.tick if tick is None else int(tick)
        self.stats["marks"] += 1
        return True

    def mark_all_dirty(self):
        # type: () -> int
        """把全部内存槽标脏。恢复流程与手动重扫用它重建读数。"""
        now = self.tick
        count = 0
        for state in self._states.values():
            state.dirty_tick = now
            count += 1
        return count

    # -- 重读 ----------------------------------------------------------

    def advance(self, tick):
        # type: (int) -> int
        """推进节拍并重读到期的内存槽。返回本帧重读了几台。

        由 `bootstrap.tick()` 每帧调用一次。**没到期的一台都不碰**，
        因此空闲帧的代价只是遍历一遍状态表（几张设备而已）。
        """
        self.tick = int(tick)
        # 未加载的先排除：`refresh` 本来就会拒掉它们，但那样它们会白白
        # 占住每帧那有限的几个名额，排在后面的已加载设备就永远轮不到。
        pending = [
            state
            for state in self.states()
            if state.loaded and self._is_due(state)
        ]
        pending = pending[:MEMORY_REFRESH_BUDGET]
        refreshed = 0
        for state in pending:
            # 一台出问题不能连累其余：少了这层兜底，一次意外会让排在后面的
            # 内存槽永远刷不到——症状是「有的设备同步、有的从不同步」。
            try:
                if self.refresh(state.ref):
                    refreshed += 1
            except Exception as error:  # pragma: no cover - 防御性
                state.stats["errors"] = state.stats.get("errors", 0) + 1
                state.unreadable = u"重读时出错：%s: %s" % (
                    type(error).__name__,
                    error,
                )
        return refreshed

    def _is_due(self, state):
        # type: (MemorySlotState) -> bool
        """这台内存槽现在该不该重读。规则同 `PatternStore._is_due`。"""
        if state.refresh_tick < 0:
            # 从没读到过（`ensure` 那一次被拒了，比如当时还没加载）。
            return True
        if state.unreadable:
            # 读不到的要定期重试：一次瞬时失败之后**不会有任何变更事件再来**，
            # 没有别的机会重读。节拍与「变更一直不停」同一量级，因此是低频的。
            return self.tick - state.refresh_tick >= MEMORY_MAX_STALE_FRAMES
        if state.dirty_tick < 0:
            # 从没收到过变更，也读得到：没有理由再读一遍。
            return False
        if state.refresh_tick > state.dirty_tick:
            # 这一次变更已被之后的某次重读覆盖过了。**必须是严格大于**：
            # 同一帧里「先刷新、后收到变更」是常规情形，用 `>=` 会把那次
            # 标记判成「已覆盖」，于是拖进去的内存条要等下一次事件才生效。
            return False
        if self.tick - state.dirty_tick >= MEMORY_SETTLE_FRAMES:
            return True
        return self.tick - state.refresh_tick >= MEMORY_MAX_STALE_FRAMES

    def refresh(self, ref, force=False):
        # type: (object, bool) -> bool
        """立刻重读一台内存槽的全部格并重算容量。返回是否读到了。

        `force=True` 用于人工重扫与测试：跳过时机判断，直接读。
        """
        state = self.get_by_ref(ref)
        if state is None:
            return False
        if not state.loaded and not force:
            # 未加载就不读：读也读不到，而每次都试会白花引擎调用。
            return False
        if force:
            state.stats["forced_refreshes"] += 1
        previous_entries = state.entries
        previous_invalid = state.invalid
        before_bytes = state.bytes()
        before_sticks = state.sticks()
        try:
            # **`refresh` 保证不抛**（同 `PatternStore.refresh`）：它的调用方
            # 分布在引擎事件、每帧推进两条路径上，让异常冒出去会作废本帧
            # 剩余的事件批次。这不是「吞异常」：类型与文本记进 `unreadable`。
            try:
                fingerprint = state.content_fingerprint()
                ok = self._read_slots(state)
                if ok:
                    state.readings += 1
                    if state.content_fingerprint() != fingerprint:
                        state.revision += 1
                        state.stats["changes"] += 1
            except Exception as error:
                # 后置指纹失败时也保留旧内容，避免新内容绕过版本更新。
                state.entries = previous_entries
                state.invalid = previous_invalid
                state.stats["invalid_slots"] = len(previous_invalid)
                state.unreadable = u"重读时出错：%s: %s" % (
                    type(error).__name__,
                    error,
                )
                state.stats["errors"] += 1
                ok = False
        finally:
            # **帧号用 `finally` 记**：写在 `return` 之前的话，一次抛错就会
            # 让它停在 -1，于是 `_is_due` 判它「从没读过」、每帧重试。
            state.refresh_tick = self.tick
            state.stats["refreshes"] += 1
            self.stats["refreshes"] += 1
        if ok and (state.bytes() != before_bytes or state.sticks() != before_sticks):
            self._notify(state)
        return ok

    def refresh_all(self, force=True):
        # type: (bool) -> int
        """重读全部内存槽。恢复流程与手动重扫的入口。"""
        return sum(
            1 for state in self.states() if self.refresh(state.ref, force=force)
        )

    def _notify(self, state):
        # type: (MemorySlotState) -> None
        """读数变了：把根数推给能源。

        **只在这一处发通知**，且只在真的变了的时候发（同 `project()` 的形状）。
        回调由启动层注入，因此领域层不认识能源服务。
        """
        if self.on_load_changed is None:
            return
        self.on_load_changed(state.node_key)

    def _read_slots(self, state):
        # type: (MemorySlotState) -> bool
        """读一台内存槽的全部格。返回是否真的读到了容器。

        **读不到时保留上一次的内容、只标 `unreadable`**，理由见模块 docstring。
        """
        adapter = self.container_at(state.node_key)
        if adapter is None:
            state.unreadable = (
                "读不到这台设备的方块容器（区块未加载、位置已无方块、"
                "或该类型尚未登记适配器）"
            )
            state.stats["unreadable"] += 1
            return False
        if not callable(getattr(adapter, "read_slot", None)):
            # 没有按槽读取能力就不退化成整表扫描：内存容量是**逐槽**的，
            # 扫描读不出「哪一格插着什么」，而且会把同一型号的两根
            # 误当成一根的两倍。与 06 的 `SlotView` 遇到同类情形时一致。
            state.unreadable = "这台设备的容器适配器不支持按槽读取，读不出 10 个槽"
            state.stats["unreadable"] += 1
            return False
        state.unreadable = ""
        state.slot_count = _slot_count_of(adapter, state.slot_count or MEMORY_SLOT_COUNT.value)
        entries = {}
        invalid = {}
        overstacked = 0
        for slot in range(state.slot_count):
            entry, error = self._read_one(state, adapter, slot)
            if state.unreadable:
                # 中途读失败，整次作废：半份结果会让剩下的槽看起来是空的，
                # 于是「读不到时保留上一次的读数」这条纪律在**中途失败**的
                # 路径上并不成立（它只对「拿不到适配器」成立）。
                state.stats["unreadable"] += 1
                return False
            if entry is not None:
                entries[slot] = entry
                if entry.count > 1:
                    overstacked += 1
            elif error is not None:
                invalid[slot] = error
        state.entries = entries
        state.invalid = invalid
        state.stats["invalid_slots"] = len(invalid)
        state.stats["overstacked_slots"] = overstacked
        return True

    def _read_one(self, state, adapter, slot):
        # type: (MemorySlotState, object, int) -> tuple
        """读一个槽。返回 `(entry | None, error | None)`，两者不会同时非空。

        三种情形分得很清楚，因为玩家要做的事完全不同：空槽（什么都不算）、
        不是内存条（**早就躺在里面**的东西，取出来即可）、是内存条（记账）。
        """
        try:
            data = adapter.read_slot(slot)
        except Exception as error:
            # **当成读不到容器**，而不是当成空槽——空槽会让这台设备看起来
            # 「内存条被拔了」，而容量会据此归零。
            #
            # 刻意捕得比 `IOError` 一族宽：这一层的下一跳是引擎
            # （`GetContainerItem`），适配器自己在同一批调用上用的是裸
            # `except Exception`（见 `EngineContainerProvider.container_at`）。
            state.unreadable = u"读取槽位 %d 时容器读不出来：%s: %s" % (
                slot,
                type(error).__name__,
                error,
            )
            return None, None
        if is_empty_item(data):
            return None, None
        try:
            item_key = item_key_from_dict(data)
        except (TypeError, ValueError) as error:
            # 这里**不能 `%s` 那个异常对象**：`str(exc)` 在 py2 上对含
            # 非 ASCII 的 `args[0]` 会按 ASCII 编码并抛 `UnicodeEncodeError`。
            return None, {
                "code": "malformed",
                "detail": u"槽里的物品字典读不动：%s: %s"
                % (type(error).__name__, error),
            }
        if item_key is None:
            return None, None
        if not self.definitions.has_memory(item_key.name):
            return None, {
                "code": "not_a_memory",
                "detail": u"%s 不是本模组的内存条（放进来的那一刻就该被拒收，"
                          u"这一根是规则生效之前留下的、或由别的模组塞进来的）"
                          % (item_key.name,),
            }
        spec = self.definitions.get_memory(item_key.name)
        count = _item_count(data)
        return (
            MemoryStickEntry(slot, item_key.name, spec.capacity_bytes, count),
            None,
        )

    def container_at(self, node_key):
        # type: (object) -> object
        """这台设备的容器适配器；拿不到时返回 `None`（**不是空容器**）。"""
        if self.containers is None:
            return None
        return self.containers.container_at(node_key)

    # -- 诊断 ----------------------------------------------------------

    def diagnostics(self):
        # type: () -> dict
        return {
            "devices": len(self._states),
            "sticks": sum(state.sticks() for state in self._states.values()),
            "capacity_bytes": sum(
                state.bytes() for state in self._states.values()
            ),
            "unreadable": [
                state.node_key.as_key()
                for state in self.states()
                if state.unreadable
            ],
            "never_read": [
                state.node_key.as_key()
                for state in self.states()
                if not state.has_reading()
            ],
            "invalid_slots": sum(
                len(state.invalid) for state in self._states.values()
            ),
            "stats": dict(self.stats),
        }


def _item_count(data):
    # type: (object) -> int
    """引擎物品字典里的个数。读不出或不是正数时按 1（**不放大容量**）。"""
    if not isinstance(data, dict):
        return 1
    count = data.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        return 1
    return count if count > 0 else 1
