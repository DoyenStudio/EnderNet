# coding=utf-8
"""样板提供者状态与产物索引（任务 07-04）。

**本模块是领域层**：不 import 引擎（`test_import_boundaries` 静态拦着）。
引擎访问全部经注入的契约——`ContainerProvider.container_at(node_key)`
（生产实现在 `adapters/containers_engine.py`，测试传 `containers.FakeContainer`）。

## 三条分工

| 关注点 | 归属 |
| --- | --- |
| 样板是什么、怎么编解码 | `patterns.py` |
| **哪台设备上有哪些样板、现在能不能派发** | 本模块 |
| 真正投料与执行 | `execution.py`（09 未实现，留桩） |

这条分工就是架构规划 §5.2 的「**配方定义与当前可用的提供者分离**」：
`PatternDefinition` 是纯数据，可用性活在提供者上。因此 `store` 只管
「哪里有」，`patterns` 只管「是什么」，两边不会互相长进对方。

## 9 格的权威是方块容器，不是这张索引

任务 12 的原文：「分别保存……**9 格工厂样板库存**、独立原料缓冲及批次归属……
**派生样板索引在恢复后重建**」。因此本模块**不存 9 格的内容**——
每次重读方块容器再解。三个后果都是想要的：

- 存档不需要为样板写迁移（12 只需保证旧的 `buffer` 载荷不被重解释成样板槽）；
- 拔出、编辑、整块换掉都会在下一次重读时自然生效，不需要另一套对账；
- 内存里的索引随时可以丢掉重建，它不是权威数据。

## 变更检测：事件 -> 脏标记 -> 去抖重读

ModAPI 明说容器事件的顺序不可依赖：

    容器内物品移动，合堆，分堆的操作会分多次事件触发并且顺序不定，
    编写逻辑时请勿依赖事件触发顺序。

因此**事件只当脏标记**。`mark_dirty()` 是引擎无关的入口（适配器把容器
位置翻译成 `NodeKey` 后调它），实际状态一律重读。同框架的参考实现
（SkyblueTech 的 `ContainerItemChangedServerEvent` 处理）用 `@Delay(0)`
把「读容器」推到下一帧，理由是一样的：事件触发时容器内容可能还没落定。
这里推到 `advance()`（由 `bootstrap.tick()` 每帧调用）并额外加去抖，
比 `Delay(0)` 更保守，也因此不需要动 `tooldelta` 的 `Delay`。

**两个节拍常量各有理由，都不能省**：

- `PATTERN_SETTLE_FRAMES`：事件之后静默这么多帧才读，避开「内容还没落定」；
- `PATTERN_MAX_STALE_FRAMES`：**即使变更一直不停**也强制重读一次。
  少了它，一台被漏斗持续投料的接口会让脏标记永不到期，索引永远不刷新
  ——而这种「一直在动、就是不同步」的故障最难查。

## 读不到容器时**不清空**索引

区块卸载、容器暂时读不出来，都**不是**「这台设备被拆了、样板没了」。
因此 `_read_slots` 在拿不到适配器时保留上一次读到的内容、只标
`unreadable`，并靠 `loaded` 把候选查询挡在外面。清空的话，09 会看到
「样板消失」而不是「设备离线」——前者会让它去重做计划，后者只是等待。
这与驱动器那边「未加载不等于被拆掉」是同一条纪律。
"""

from ...common.definitions import TEMPLATE_PATTERN_SLOT_COUNT, devices_for_mode
from ...common.item_codec import (
    is_empty_item,
    item_dict_from_key,
    item_key_from_dict,
    unbox_engine_value,
)
from ...common.protocol import BlockReason, Reason
from ...common.resource_key import ResourceKey  # noqa: F401 (仅用于 # type: 注释)
from ..devices.capabilities import PATTERN_PROVIDER, CapabilityRegistry
from ..runtime.lifecycle import ChangeKind
from . import patterns
from .execution import PatternProviderPort, editable_view
from .patterns import PatternError, PatternPolicy, PatternValidationError

# -- 节拍 ------------------------------------------------------------------

#: 收到变更之后再静默这么多帧才重读。
#:
#: 一次「拖进去」在引擎那边会派发多条事件（移动、合堆、分堆各算一条），
#: 且顺序不定。等它们静默下来再读一次，比逐条响应省得多，也更准。
PATTERN_SETTLE_FRAMES = 2

#: 一台提供者最多这么久没被重读过，**即使变更一直不停**。
#:
#: 见模块 docstring：少了它，持续投料的接口会让脏标记永不到期。
#: 取 40 帧（约 1.3 秒 @30Hz）是「人不会察觉、但也不会无限拖下去」的量级；
#: 它**不是**玩法数值，因此不进策略、不进诊断的未定清单。
PATTERN_MAX_STALE_FRAMES = 40

#: 每帧最多重读几台提供者。重读要读满 9 个槽，是真实的引擎调用。
PATTERN_REFRESH_BUDGET = 4

#: 声明了 `pattern_provider` 能力、但定义表里查不到时（离线夹具）的兜底集合。
#: **从 `PATTERN_DEVICES` 派生**，不另抄一份设备清单。
_FALLBACK_PROVIDER_IDS = frozenset(
    device_id
    for mode in patterns.MODES
    for device_id in devices_for_mode(mode)
)


def is_pattern_provider(device_id, definitions=None):
    # type: (str, object) -> bool
    """这台设备是不是样板提供者。

    **按能力判定，不按设备名**：装配仓与总线样板接口都要算，而将来新增
    提供者时只要定义表里声明了能力就自动纳入。定义表缺失（离线夹具）时
    退到 `_FALLBACK_PROVIDER_IDS`。
    """
    if definitions is not None:
        definition = definitions.devices.get(device_id)
        if definition is not None:
            return definition.has_capability(PATTERN_PROVIDER)
    return device_id in _FALLBACK_PROVIDER_IDS


# -- 一条样板 --------------------------------------------------------------


class PatternEntry(object):
    """一台提供者某个槽里的一块样板（已解码）。

    `decoded` 里同时带着内容与格式：内容用作「版本」（`digest()`），
    格式决定能不能派发。两者分开的理由见 `patterns.DecodedPattern`。
    """

    __slots__ = ("slot", "decoded", "item_key")

    def __init__(self, slot, decoded, item_key=None):
        # type: (int, object, object) -> None
        self.slot = int(slot)
        self.decoded = decoded
        #: 槽里那件物品的资源键（诊断用：回答「这一格物理上是哪个物品」）。
        self.item_key = item_key

    @property
    def definition(self):
        return self.decoded.definition

    def digest(self):
        # type: () -> str
        return self.decoded.digest()

    def is_dispatchable(self):
        # type: () -> bool
        return self.decoded.is_dispatchable()

    def output_keys(self):
        # type: () -> tuple
        return self.decoded.definition.output_keys()

    def to_dict(self):
        # type: () -> dict
        return {
            "slot": self.slot,
            "digest": self.digest(),
            "format": self.decoded.format,
            "dispatchable": self.is_dispatchable(),
            "definition": self.decoded.definition.to_dict(),
            "summary": self.decoded.definition.summary(),
            "item": self.item_key.name if self.item_key is not None else None,
        }

    def __repr__(self):
        return "PatternEntry(slot=%d, %r)" % (self.slot, self.definition)


# -- 一台提供者 ------------------------------------------------------------


class PatternProviderState(object):
    """一台样板提供者的权威状态。**由启动层持有**，不挂在网络运行时上。

    与 `DriverState` / `PortState` 同一条理由（架构 §3）：样板装在方块容器里，
    它不能因为网络断开、主控被移除或运行时重建而消失。**未加载也不等于
    被拆掉**——`is_usable()` 报的是「这台设备现在能不能参与业务」，
    而不是「它在世界上还在不在」。
    """

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
        "revision",
        "capabilities",
        "provider_port",
        "stats",
    )

    def __init__(self, ref, device_id, slot_count=0):
        # type: (object, str, int) -> None
        self.ref = ref
        self.device_id = device_id
        self.loaded = True
        #: 这台设备有多少个样板槽。**由容器回答**（`container_slot_count`），
        #: 容器答不出时退回定义表那条已定案的 9——它与 `gen_blocks.py` 写进
        #: 方块容器的 `container_size` 是同一个数，不可能分叉。
        self.slot_count = int(slot_count)
        #: `slot -> PatternEntry`。只放**解得出来**的槽。
        self.entries = {}
        #: `slot -> {"code", "detail"}`。槽里有东西但解不成样板。
        #: **与「空槽」分开**：空槽是没东西，这里是「有东西但不能用」。
        self.invalid = {}
        #: 读不到容器的原因（空串表示读得到）。**不清空 `entries`**，见模块 docstring。
        self.unreadable = ""
        #: 最后一次收到变更的帧号。0 表示从没收到过。
        #: **-1 表示「从没收到过变更」**，而不是 0：逻辑时钟的读数可以是 0
        #: （启动头几帧、离线测试的起点），拿 0 当哨兵会把真实的变更吃掉。
        self.dirty_tick = -1
        #: 最后一次真的重读过的帧号。-1 表示从没读过。
        self.refresh_tick = -1
        #: 内容版本，每次重读发现变化就 +1。索引按它判断要不要重建。
        self.revision = 0
        #: 挂在这台设备上的**实际能力对象**（架构 §4）。光有定义表里的能力
        #: 标签不算数：`has_capability("pattern_provider")` 为真而这里什么都没有，
        #: 正是「已声明能力不等于已实例化能力对象」的形态。
        self.capabilities = CapabilityRegistry()
        self.provider_port = None
        self.stats = {
            "refreshes": 0,
            "changes": 0,
            "unreadable": 0,
            "invalid_slots": 0,
            "installs": 0,
            "uninstalls": 0,
            "stale_checks": 0,
            "forced_refreshes": 0,
        }

    # -- 查询 ----------------------------------------------------------

    @property
    def node_key(self):
        return self.ref.node_key

    def is_usable(self):
        # type: () -> bool
        """能不能参与业务。**未加载不等于被拆掉**（与驱动器同一条纪律）。"""
        return self.loaded and not self.unreadable

    def usable_entries(self):
        # type: () -> list
        """**内容层面**可派的样板，按槽位排序。

        只看格式能不能派（`is_dispatchable`），**不看设备在不在线**——
        在线与否由准入门与 `candidates()` 回答。两件事混在一个方法里，
        诊断就分不清「这块样板过时了」与「这台设备离线了」。
        """
        return [
            self.entries[slot]
            for slot in sorted(self.entries)
            if self.entries[slot].is_dispatchable()
        ]

    def entry_at(self, slot):
        # type: (int) -> object
        return self.entries.get(int(slot))

    def content_fingerprint(self):
        # type: () -> tuple
        """内容指纹，用于判断「这次重读有没有读出不一样的东西」。

        不含 `loaded` / `unreadable` / 帧号：那些是设备状态，不是样板内容，
        把它们算进来会让每次挂载变化都触发一次索引重建。
        """
        return (
            tuple(
                (slot, self.entries[slot].digest(), self.entries[slot].decoded.format)
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
            "entries": [self.entries[slot].to_dict() for slot in sorted(self.entries)],
            "invalid": [
                {"slot": slot, "error": dict(self.invalid[slot])}
                for slot in sorted(self.invalid)
            ],
            "unreadable": self.unreadable,
            "usable": len(self.usable_entries()),
            # 实际挂上的能力对象（架构 §4）。界面与诊断靠它回答
            # 「这台设备到底提供了什么」，而不是看定义表里的标签。
            "capabilities": sorted(self.capabilities.names()),
            "stats": dict(self.stats),
        }

    def __repr__(self):
        return "PatternProviderState(%s @ %r, %d 块样板)" % (
            self.device_id,
            self.node_key,
            len(self.entries),
        )


# -- 集合 ------------------------------------------------------------------


class PatternStore(object):
    """样板提供者状态集合 + 产物索引。

    与 `StorageDriverStore` / `PortStore` 同地位：既是提供者状态的唯一权威，
    也是「产物 -> 谁能做」这张索引的建造者。**索引是派生的**，见模块 docstring。
    """

    def __init__(self, definitions, registry, policy=None, containers=None):
        # type: (object, object, PatternPolicy, object) -> None
        #: 定义表。它被读三处：判定谁是提供者、查物品标识与模式、查模式对应的设备。
        self.definitions = definitions
        self.registry = registry
        self.policy = policy if policy is not None else PatternPolicy()
        #: `ContainerProvider`：位置 -> ContainerAdapter。生产实现在
        #: `adapters/containers_engine.py`，测试传 `containers.FakeContainer`。
        #: **可为 None**，此时所有需要容器的动作明确报读不到，而不是当成空槽。
        self.containers = containers
        self._states = {}
        self.tick = 0
        #: 索引的建筑版本：`_revision` 是全部提供者的版本之和，索引记着自己
        #: 是按哪个版本建的。这样查询只在真的变过之后才重建，而「有没有变过」
        #: 由重读时的内容指纹回答——不是靠调用方记得通知。
        self._revision = 0
        self._index = {}
        self._index_revision = -1
        self.stats = {
            "created": 0,
            "dropped": 0,
            "load_marks": 0,
            "marks": 0,
            "refreshes": 0,
            "index_rebuilds": 0,
        }

    # -- 状态 ----------------------------------------------------------

    def ensure(self, ref, device_id=None):
        # type: (object, str) -> PatternProviderState
        """取或建一台提供者的状态。同一代次重复调用是幂等的。"""
        existing = self.get_by_ref(ref)
        if existing is not None:
            return existing
        if device_id is None:
            # 只在没给型号时才查注册表（与 `PortStore.ensure` 同一形状）：
            # 离线夹具常直接给型号，那时连注册表都不必构造。
            record = self.registry.get_by_ref(ref)
            device_id = record.device_id if record is not None else ""
        if not is_pattern_provider(device_id, self.definitions):
            return None
        state = PatternProviderState(ref, device_id)
        state.provider_port = PatternProviderPort(state, self)
        state.capabilities.register(PATTERN_PROVIDER, state.provider_port)
        self._states[ref.node_key.as_key()] = state
        self.stats["created"] += 1
        # **建好就读一次**：`slot_count` 由容器回答（界面格数与领域层槽数
        # 因此不可能分叉），不读的话它会停在 0，而 `install` 会以
        # 「槽位越界」拒绝每一次写入——一个看起来像配置问题的空状态。
        self.refresh(ref)
        return state

    def get(self, node_key):
        # type: (object) -> PatternProviderState
        state = self._states.get(node_key.as_key())
        if state is None or not is_pattern_provider(state.device_id, self.definitions):
            return None
        return state

    def get_by_ref(self, ref):
        # type: (object) -> PatternProviderState
        """按实例代次取。**代次不符即失效**——这是延迟任务的自毁检查。"""
        state = self._states.get(ref.node_key.as_key())
        if state is None or state.ref.generation != ref.generation:
            return None
        return state

    def drop(self, ref):
        # type: (object) -> PatternProviderState
        """拆除：移除状态。

        **样板本身不丢**——它们是方块容器里的实物物品，容器随方块一起
        按游戏规则处理（掉落、留存），不在本模块的职责里。这里只是忘掉
        这台设备的内存投影。这与 `PortStore.drop` 形成对照：那边要留处置报告，
        是因为缓冲里的东西**不在容器里**（样板口的原料缓冲是纯内存的）。
        """
        state = self.get_by_ref(ref)
        if state is None:
            return None
        self._states.pop(ref.node_key.as_key(), None)
        self._revision += 1
        self.stats["dropped"] += 1
        return state

    def mark_loaded(self, node_key, loaded):
        # type: (object, bool) -> bool
        """区块加载状态变化。**样板内容一字不改**。"""
        state = self.get(node_key)
        if state is None or state.loaded == bool(loaded):
            return False
        state.loaded = bool(loaded)
        self.stats["load_marks"] += 1
        return True

    def mark_all_unloaded(self):
        # type: () -> int
        """停机：全部标为未加载。**不清样板、不写仓库。**"""
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

    # -- 生命周期 ------------------------------------------------------

    def observe(self, change):
        # type: (object) -> int
        """从设备生命周期变化推导提供者状态。**唯一入口**，与拓扑同一批变化。

        由启动层的 `_emit` 调用，因此不会出现「拓扑知道设备没了、
        样板索引还以为在」的分叉。
        """
        kind = change.kind
        device_id = change.device_id
        if kind == ChangeKind.ADDED:
            if not is_pattern_provider(device_id, self.definitions):
                return 0
            # `ensure` 自己会读一次（容器此时是空的，但读一次才知道它有多大）。
            return 1 if self.ensure(change.ref, device_id) is not None else 0
        if kind == ChangeKind.REMOVED:
            if not is_pattern_provider(device_id, self.definitions):
                return 0
            return 1 if self.drop(change.ref) is not None else 0
        if kind in (ChangeKind.LOADED, ChangeKind.UNLOADED):
            if not is_pattern_provider(device_id, self.definitions):
                return 0
            if not self.mark_loaded(change.node_key, kind == ChangeKind.LOADED):
                return 0
            # 重新加载之后内容可能已经变了（别的模组、漏斗、退出前的手工调整），
            # 因此挂载变化也标脏，而不是信任内存里那一份。
            self.mark_dirty(change.node_key)
            return 1
        # NEIGHBORS_AFFECTED 与 PLACEMENT_REVIEW 是拓扑与交互层的事。
        return 0

    # -- 变更信号 ------------------------------------------------------

    def mark_dirty(self, node_key, tick=None):
        # type: (object, int) -> bool
        """这台设备所在的容器变了。**只记一笔**，不在这里读容器。

        引擎无关的入口：适配器把容器事件翻译成位置后调它。不立刻读的
        两个理由（都在模块 docstring 里）：事件顺序不可依赖；事件触发时
        容器内容可能还没落定。
        """
        state = self.get(node_key)
        if state is None:
            return False
        state.dirty_tick = self.tick if tick is None else int(tick)
        self.stats["marks"] += 1
        return True

    def mark_all_dirty(self):
        # type: () -> int
        """把全部提供者标脏。恢复流程与手动重扫用它重建索引。"""
        now = self.tick
        count = 0
        for state in self._states.values():
            state.dirty_tick = now
            count += 1
        return count

    # -- 重读 ----------------------------------------------------------

    def advance(self, tick):
        # type: (int) -> int
        """推进节拍并重读到期的提供者。返回本帧重读了几台。

        由 `bootstrap.tick()` 每帧调用一次。**没到期的提供者一台都不碰**，
        因此空闲帧的代价是遍历一遍状态表（几张设备而已）。
        """
        self.tick = int(tick)
        # **未加载的先排除**。`refresh` 本来就会拒掉它们，但那样它们会白白
        # 占住每帧那有限的几个名额（`PATTERN_REFRESH_BUDGET`），
        # 排在后面的已加载设备就永远轮不到——一台卸载的区块
        # 能把另一台的索引饿死，而这种故障从外面看只是「有时同步有时不同步」。
        pending = [
            state
            for state in self.states()
            if state.loaded and self._is_due(state)
        ]
        # 每帧限量：一次世界加载可能同时唤醒许多台，读满是真实引擎调用。
        pending = pending[:PATTERN_REFRESH_BUDGET]
        refreshed = 0
        for state in pending:
            # `refresh` 自己保证不抛（见那边的说明），这里的兜底是第二道闸：
            # **一台出问题不能连累其余**。少了它，一次意外会让排在后面的
            # 提供者永远刷不到——症状是「有的设备同步、有的从不同步」。
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
        # type: (PatternProviderState) -> bool
        """这台提供者现在该不该重读。"""
        if state.refresh_tick < 0:
            # 从没读到过（`ensure` 那一次被拒了，比如当时还没加载）。
            return True
        if state.unreadable:
            # **读不到容器的要定期重试。** 一次瞬时读失败（方块实体刚放下
            # 还没就绪、适配器刚被作废）不该让这台设备的索引永远空着——
            # 而那种失败之后**不会有任何变更事件再来**，因此没有别的机会
            # 重试。节拍取与「变更一直不停」同一个兜底量级，所以是低频的。
            return self.tick - state.refresh_tick >= PATTERN_MAX_STALE_FRAMES
        if state.dirty_tick < 0:
            # 从没收到过变更，也读得到：没有理由再读一遍。
            return False
        if state.refresh_tick > state.dirty_tick:
            # 这一次变更已经被**之后**的某次重读覆盖过了，不必再来一遍。
            #
            # **必须是严格大于。** 同一帧里「先刷新、后收到变更」是常规情形
            # （`observe(ADDED)` 会先读一次，同一帧稍后容器事件才标脏），
            # 用 `>=` 会把那次标记判成「已覆盖」，于是这一帧的变化永远
            # 不会被读到——症状是「拖进去的第一块样板不生效，第二块才生效」。
            return False
        if self.tick - state.dirty_tick >= PATTERN_SETTLE_FRAMES:
            return True
        # 变更一直不停时的兜底，见 `PATTERN_MAX_STALE_FRAMES`。
        return self.tick - state.refresh_tick >= PATTERN_MAX_STALE_FRAMES

    def refresh(self, ref, force=False):
        # type: (object, bool) -> bool
        """立刻重读一台提供者的 9 格并重建它的内容。返回是否读到了。

        `force=True` 用于人工重扫与测试：跳过时机判断，直接读。
        """
        state = self.get_by_ref(ref)
        if state is None:
            return False
        if not state.loaded and not force:
            # **未加载就不读**：读也读不到，而每次都试会白花引擎调用。
            # 内容保留着，重新加载时由 `observe` 标脏再读。
            return False
        if force:
            state.stats["forced_refreshes"] += 1
        before = state.content_fingerprint()
        try:
            # **`refresh` 保证不抛。** 它是重读的唯一入口，调用方分布在
            # 三条性质完全不同的路径上：引擎事件（`observe` → `ensure`）、
            # 每帧推进（`advance`）、以及 09 的派发前复核（`check_dispatch`）。
            # 让异常从任一条冒出去，代价都不一样而且都很难看：
            # 前两条会作废本帧剩余的事件批次（`tick()` 把异常记成 tick_error
            # 就结束这一帧），第三条会把「能不能派发」变成一次崩溃。
            #
            # **这不是「吞异常」**：类型与文本记进 `unreadable`，诊断里
            # 看得见，与「什么都没读到」分得开。与适配器在同一批引擎调用上的
            # 处置一致（见 `EngineContainerProvider.container_at`）。
            try:
                ok = self._read_slots(state)
            except Exception as error:
                state.unreadable = u"重读时出错：%s: %s" % (
                    type(error).__name__,
                    error,
                )
                state.stats["errors"] = state.stats.get("errors", 0) + 1
                ok = False
        finally:
            # **帧号用 `finally` 记。** 写在 `return` 之前的话，一次抛错就
            # 会让它停在 `-1`，于是 `_is_due` 判它「从没读过」——
            # 那台设备会**每帧**重试（不是每 `PATTERN_MAX_STALE_FRAMES` 帧），
            # 白白吃掉每帧那有限的几个名额。
            state.refresh_tick = self.tick
            state.stats["refreshes"] += 1
            self.stats["refreshes"] += 1
        if ok and state.content_fingerprint() != before:
            state.revision += 1
            self._revision += 1
            state.stats["changes"] += 1
        return ok

    def refresh_all(self, force=True):
        # type: (bool) -> int
        """重读全部提供者。恢复流程与手动重扫的入口。"""
        return sum(
            1 for state in self.states() if self.refresh(state.ref, force=force)
        )

    def _read_slots(self, state):
        # type: (PatternProviderState) -> bool
        """读一台提供者的全部槽位。返回是否真的读到了容器。

        **读不到时保留上一次的内容、只标 `unreadable`**，理由见模块 docstring：
        清空会让 09 看到「样板没了」而不是「设备离线」。
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
            # 这个适配器没有**按槽**读取能力（13-01 的通用只读面只保证
            # `entries()`）。**明确报读不到，不退化成按资源键的整表扫描**：
            # 后者读不出「哪个槽里是什么」，而样板槽位是这块功能的全部意义。
            # 与 06 的 `SlotView` 遇到同类情形时的处置一致。
            state.unreadable = (
                "这台设备的容器适配器不支持按槽读取，读不出 9 个样板槽"
            )
            state.stats["unreadable"] += 1
            return False
        state.unreadable = ""
        state.slot_count = container_slot_count(adapter, state.slot_count or None)
        entries = {}
        invalid = {}
        for slot in range(state.slot_count):
            entry, error = self._read_one(state, adapter, slot)
            if state.unreadable:
                # **中途读失败，整次作废。** 半份结果不是结果：把它当成
                # 「本次读到的内容」会让剩下的槽看起来是空的，于是
                # 「读不到时保留上一次的索引」这条纪律在**中途失败**的路径上
                # 并不成立（它只对「拿不到适配器」成立）。那台设备会以
                # 一份缺了几格的索引对外服务，而缺的那几格正是「样板被拔了」
                # 这个结论——09 会据此去重做计划。
                state.stats["unreadable"] += 1
                return False
            if entry is not None:
                entries[slot] = entry
            elif error is not None:
                invalid[slot] = error
        state.entries = entries
        state.invalid = invalid
        state.stats["invalid_slots"] = len(invalid)
        return True

    def _read_one(self, state, adapter, slot):
        # type: (PatternProviderState, object, int) -> tuple
        """读一个槽。返回 `(entry | None, error | None)`，两者不会同时非空。

        三种「不是有效样板」分得很清楚，因为玩家要做的事完全不同：
        空槽（什么都不算）、不是样板（把东西拿出来）、是样板但坏了
        （重新编辑一次）。
        """
        try:
            data = adapter.read_slot(slot)
        except Exception as error:
            # **当成读不到容器**，而不是当成空槽——空槽会让这台设备看起来
            # 「样板被拔了」，而 09 会据此去重做计划。
            #
            # 刻意捕得比 `IOError` 一族宽：这一层的下一跳是引擎
            # （`GetContainerItem`），而适配器自己在同一批调用上用的是裸
            # `except Exception`（见 `EngineContainerProvider.container_at`
            # 的说明：「区块没加载、位置没有方块实体等都会抛」）。
            # 捕到之后**记进 `unreadable` 而不是默默吞掉**：诊断里看得见
            # 异常类型与文本，与「什么都没读到」分得开。
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
            # 这里**不能 `%s` 那个异常对象**：`str(exc)` 在 py2 上对含非 ASCII
            # 的 `args[0]` 会按 ASCII 编码并抛 `UnicodeEncodeError`。
            # 异常对象属于外部输入的一部分，取值时要当它可能是 unicode。
            return None, {
                "code": PatternError.MALFORMED,
                "detail": u"槽里的物品字典读不动：%s: %s"
                % (type(error).__name__, error),
            }
        if item_key is None:
            return None, None
        mode = self.definitions.template_mode_of(item_key.name)
        if not mode:
            return None, {
                "code": PatternError.NOT_A_PATTERN,
                "detail": u"%s 不是本模组的样板物品" % (item_key.name,),
            }
        if not self.policy.accepts_mode(state.device_id, mode):
            return None, {
                "code": PatternError.KIND_MISMATCH,
                "detail": "这是一块%s样板，不该放在 %s 上（它只收 %s）"
                % (
                    mode,
                    state.device_id,
                    _accepted_modes(self.policy, state.device_id),
                ),
            }
        try:
            # **先解引擎的类型标签再解码。** 引擎读回来的 `userData` 里每个
            # 标量都是 `{"__type__": T, "__value__": V}`（实测），直接喂给
            # `read_pattern` 会得到「格式版本必须是整数，得到 {...}」。
            # 解包是引擎边界的事，因此实现在 `common/item_codec.py`。
            decoded = patterns.read_pattern(
                unbox_engine_value(data.get("userData"))
            )
        except PatternValidationError as error:
            return None, error.to_dict()
        return PatternEntry(slot, decoded, item_key), None

    # -- 索引 ----------------------------------------------------------

    def _ensure_index(self):
        # type: () -> dict
        """按需重建产物索引。返回 `资源键 -> [(提供者, 条目)]`。"""
        if self._index_revision == self._revision:
            return self._index
        index = {}
        for state in self.states():
            for slot in sorted(state.entries):
                entry = state.entries[slot]
                # **过时的样板不进索引**：它出现在候选里就会被排进计划，
                # 然后卡在派发那一步。诊断里查得到它（`editable_view` 报
                # `stale_slots`），但 08 看不见它。
                if not entry.is_dispatchable():
                    continue
                for key in entry.output_keys():
                    index.setdefault(key, []).append((state, entry))
        # 稳定顺序：**不是优先级**，只是「同样的输入永远给同样的顺序」。
        # 优先级属 08-04（见 `candidates` 的说明）。
        for key in index:
            index[key].sort(key=_candidate_sort_key)
        self._index = index
        self._index_revision = self._revision
        self.stats["index_rebuilds"] += 1
        return index

    def candidates(self, output_key, include_unavailable=False):
        # type: (ResourceKey, bool) -> list
        """按产物查候选提供者。

        **顺序是稳定的，但不是优先级。** 排序键取「设备标识、位置、槽位」，
        它保证同样的输入得到同样的输出（08-04 要在其之上定义候选优先级，
        因此它必须能复现）；而「哪个更优」是 08 的规则，这里不替它决定——
        混为一谈会让 08 的一个策略决定被 07 悄悄定案。

        默认**只给当前可用的**（07 验收：「最后一个提供者离线后不会继续
        作为可派发设备」）。`include_unavailable=True` 把它们也列出来并带上
        `blocker`，供诊断与 09 的「提供者离线」暂停理由使用。
        """
        out = []
        for state, entry in self._ensure_index().get(output_key, ()):
            blocker = self.provider_blocker(state)
            if blocker is not None and not include_unavailable:
                continue
            out.append(
                {
                    "provider": state,
                    "entry": entry,
                    "available": blocker is None,
                    "blocker": blocker,
                }
            )
        return out

    def output_keys(self):
        # type: () -> list
        """索引里出现过的全部产物，按资源指纹排序（诊断用）。"""
        return sorted(self._ensure_index(), key=lambda key: key.canonical())

    def indexed_output_count(self):
        # type: () -> int
        """**上一次建好的**索引里有几个产物。不触发重建。

        诊断读它而不是读 `_ensure_index()`：那会在**只读**路径上重建索引、
        顺带把 `index_rebuilds` 加一——于是「谁在反复重建索引」这个问题的
        答案，会被诊断调用自己污染。配 `index_is_current()` 一起看：
        过时的计数会标出来，而不是假装它是准的。
        """
        return len(self._index)

    def index_is_current(self):
        # type: () -> bool
        """索引是不是按最新内容建的。False 表示上面那个计数已经过时。"""
        return self._index_revision == self._revision

    # -- 派发前复核 ----------------------------------------------------

    def check_dispatch(self, ref, slot, digest, expected_mode=None):
        # type: (object, int, str, str) -> tuple
        """派发前复核一块样板还是不是原来那一块。返回 `(entry, blocker)`。

        这是 07-04 的「**版本变化使旧规划需复核**」与 07 验收「**样板编辑后
        旧版本计划不能直接派发**」的唯一落点。09 提交时拿着计划里记的
        `(提供者, 槽位, digest)` 回来问，得到的只有两种答案：还能用，
        或者为什么不能。

        **它只回答「能不能」，不做事后处置。** 已派发的批次属 09 的台账，
        本模块既不删也不重发（09 文档：「不得因样板消失而删除已派发台账
        或重发整批原料」）。
        """
        state = self.get_by_ref(ref)
        if state is None:
            return None, BlockReason(
                Reason.DEVICE_GONE, "目标位置上已经没有这台样板提供者"
            )
        state.stats["stale_checks"] += 1
        # **先确保这份答案是新鲜的。**
        #
        # 去抖本来会把重读推到若干帧之后，而「派发前复核」的全部意义就是
        # 「按**现在**的样子判断」：玩家在第 N 帧把样板拖走了，09 在第 N+1 帧
        # 来问，若这里读的仍是第 N-1 帧那份记录，答案会是「还能派发」——
        # 而那正是 07 验收里「样板编辑后旧版本计划不能直接派发」要防的事。
        #
        # 只在**变更不比上次重读更旧**时强制读一次，因此代价有界（一条脏标记
        # 最多换来一次读）。读不到的设备不在这里重试——那由 `advance` 按
        # 固定节拍兜底，否则一个卡住的提供者会被 09 每帧戳一次引擎。
        #
        # **这里用 `>=`，与 `_is_due` 的 `>` 刻意相反。** `_is_due` 是节流
        # 判断，同一帧算「已经覆盖」可以省一次读；这里是**正确性**判断，
        # 同一帧时读没读到那次变更是说不准的，而代价是一份计划照着
        # 一个已经不存在的槽去派发。宁可多读一次。
        if state.dirty_tick >= 0 and state.dirty_tick >= state.refresh_tick:
            self.refresh(ref)
        blocker = self.provider_blocker(state)
        if blocker is not None:
            return None, blocker
        entry = state.entry_at(slot)
        if entry is None:
            # **「槽里的东西坏了」与「样板没了」是两回事。**
            # 前者玩家要做的是把东西拿出来或升级模组，后者只能重新找样板；
            # 一律回 `PATTERN_STALE` 会让人以为是自己的操作丢了东西。
            error = state.invalid.get(int(slot))
            if error is not None:
                return None, BlockReason(
                    reason_for_pattern_error(error["code"]), error["detail"]
                )
            return None, BlockReason(
                Reason.PATTERN_STALE,
                "槽位 %d 里已经没有样板了；这一批按旧计划不能再派发"
                % (int(slot),),
            )
        if digest and entry.digest() != digest:
            return None, BlockReason(
                Reason.PATTERN_STALE,
                "槽位 %d 里的样板已经被编辑或换成另一块（计划记的是 %s，"
                "现在的是 %s）；复核之前不派发"
                % (int(slot), _short(digest), _short(entry.digest())),
            )
        if expected_mode and entry.definition.mode != expected_mode:
            return None, BlockReason(
                Reason.PATTERN_KIND_MISMATCH,
                "槽位 %d 里的样板模式是 %s，与这次派发要求的 %s 不符"
                % (int(slot), entry.definition.mode, expected_mode),
            )
        if not entry.is_dispatchable():
            return None, BlockReason(
                Reason.PATTERN_FORMAT_UNSUPPORTED,
                "槽位 %d 里的样板格式版本是 %d，已不再允许派发；"
                "重新编辑并保存一次即可升级"
                % (int(slot), entry.decoded.format),
            )
        return entry, None

    def provider_blocker(self, state):
        # type: (PatternProviderState) -> BlockReason
        """这台提供者现在为什么不参与业务。**诊断与候选查询共用这一份措辞。**"""
        if state is None:
            return BlockReason(Reason.DEVICE_GONE, "没有这台样板提供者")
        if state.unreadable:
            return BlockReason(Reason.NO_CONTAINER, state.unreadable)
        if not state.loaded:
            return BlockReason(
                Reason.DRIVER_UNLOADED,
                "样板提供者所在区块未加载；这不代表它已被拆除",
            )
        return None

    # -- 写入口（领域 API）--------------------------------------------

    def install(self, ref, slot, definition):
        # type: (object, int, object) -> tuple
        """把一份样板写进某个槽位。返回 `(ok, detail)`。

        **这是「编辑」的落点**：写回之后立刻重读，因此索引与 digest 自动跟上，
        旧计划在下一次 `check_dispatch` 时拿到 `PATTERN_STALE`。

        **本次没有命令调用方**：样板终端的编辑界面属任务 11。这里不开
        命令路径，是因为绕过主控的写入口（哪怕是给调试用的）正是全局约束 2
        说的那种旁路；11 落地时按「取放必须经过服务端交接」接上。
        """
        state = self.get_by_ref(ref)
        if state is None:
            return False, "目标位置上没有样板提供者"
        if not isinstance(definition, patterns.PatternDefinition):
            return False, "要安装的必须是一份 PatternDefinition"
        if not self.policy.accepts_mode(state.device_id, definition.mode):
            return False, "%s 不收 %s 模式的样板" % (state.device_id, definition.mode)
        item = self.template_item(definition)
        if item is None:
            return False, "没有与 %s 模式对应的样板物品" % (definition.mode,)
        if not state.loaded:
            return False, "设备所在区块未加载，此时写入无法确认"
        adapter = self.container_at(state.node_key)
        if adapter is None:
            return False, "读不到这台设备的方块容器，写入也不会生效"
        slot = int(slot)
        if slot < 0 or slot >= state.slot_count:
            return False, "槽位 %d 越界（这台设备有 %d 格）" % (slot, state.slot_count)
        # **不看 `write_slot` 的返回值，一律读回核对。**
        #
        # 架构规划 §6.3 点名的真实缺陷就是「说写进去了、其实没有」
        # （`PutItemIntoContainer` 不看 `SetContainerItem` 的返回值）。
        # 反过来的情形**也在真机出现过**：`SpawnItemToContainer` 在
        # 某些替换状态下返回 `False`，而东西其实写进去了（2026-09-25 实测）。
        # 因此返回值只当提示，判据是读回来的内容——这正是 06 的
        # `SlotBackedContainer._do_insert` 逐槽读回的同一套做法。
        wrote = bool(adapter.write_slot(slot, item))
        if not self.refresh(ref, force=True):
            return False, "写进去了，但紧接着读不回来，无法确认结果"
        entry = state.entry_at(slot)
        if entry is None or entry.definition != definition:
            hint = "容器报告写入失败" if not wrote else "容器报告写入成功"
            return False, "写入没有生效（%s）：这一格读回来不是刚写进去的那块样板" % (
                hint,
            )
        state.stats["installs"] += 1
        return True, ""

    def uninstall(self, ref, slot):
        # type: (object, int) -> tuple
        """清空一个样板槽。返回 `(ok, detail)`。理由与 `install` 同：写回后核对。"""
        state = self.get_by_ref(ref)
        if state is None:
            return False, "目标位置上没有样板提供者"
        if not state.loaded:
            return False, "设备所在区块未加载，此时写入无法确认"
        adapter = self.container_at(state.node_key)
        if adapter is None:
            return False, "读不到这台设备的方块容器，写入也不会生效"
        slot = int(slot)
        if slot < 0 or slot >= state.slot_count:
            return False, "槽位 %d 越界（这台设备有 %d 格）" % (slot, state.slot_count)
        # 理由与 `install` 同：返回值只当提示，判据是读回来的内容。
        wrote = bool(adapter.write_slot(slot, None))
        if not self.refresh(ref, force=True):
            return False, "清空了，但紧接着读不回来，无法确认结果"
        if state.entry_at(slot) is not None or state.invalid.get(slot) is not None:
            return False, "清空没有生效%s：这一格读回来还有东西" % (
                "（容器报告写入失败）" if not wrote else "（容器报告写入成功）",
            )
        state.stats["uninstalls"] += 1
        return True, ""

    def template_item(self, definition):
        # type: (object) -> dict
        """按配方模式造一件样板物品的引擎字典（含编码好的 `userData`）。

        物品标识**按模式反查定义表**，因此「合成配方却写成工厂样板物品」
        这条路径不存在——两者查的是同一个映射。

        `customTips` 与 `userData` 一起进**资源键**，因为 `item_codec`
        把两者都算作身份：写进去的和读回来的必须是同一个键，否则
        「刚装上去就被判成换了另一块」。
        """
        item_id = self.definitions.item_id_for_template_mode(definition.mode)
        if not item_id:
            return None
        user_data = patterns.write_pattern(None, definition)
        key = ResourceKey.item(
            item_id,
            0,
            custom_tips=patterns.describe_tip(definition),
            user_data=user_data,
        )
        return item_dict_from_key(key, 1)

    # -- 插槽判定（引擎事件路径的入口）--------------------------------

    def accepts_put(self, device_id, item_name):
        # type: (str, str) -> bool
        """玩家要把 `item_name` 放进这台设备的槽位，该不该拒绝。

        **只在「这台设备是样板提供者」时有话说**；其余设备一律放行——
        被动接口那 9 格是存取共用的真实库存，归 06，样板层不替它做决定。

        判定**只看物品标识，不看内容**：物品类型就是配方模式
        （`TemplateSpec.mode`），因此这里不需要读 `userData`，
        也就绕开了「事件里的 `itemDict` 带不带 userData」这个不确定项。
        内容是否合法在重读路径上判，坏样板报进 `invalid_slots` 而不是拒绝
        玩家把它放进去——**拒收只针对「不该在这里的东西」**，
        内容坏了的东西拒收会让玩家拿着一块取不出来的物品无计可施。

        「不该在这里」有两种，`blocks.md` 与 14 的验收各点名了一条：
        普通原料，以及不适用的样板类型（合成样板插进样板接口）。
        """
        if not is_pattern_provider(device_id, self.definitions):
            return True
        if not self.definitions.has_template(item_name):
            return False
        return self.policy.accepts_mode(
            device_id, self.definitions.template_mode_of(item_name)
        )

    # -- 容器 ----------------------------------------------------------

    def container_at(self, node_key):
        # type: (object) -> object
        """取某个位置的容器适配器。没有提供者时返回 None（**不是空容器**）。"""
        if self.containers is None:
            return None
        return self.containers.container_at(node_key)

    # -- 视图与诊断 ----------------------------------------------------

    def editable(self, ref):
        # type: (object) -> dict
        """给样板终端（11）看的只读视图（07-07）。"""
        state = self.get_by_ref(ref)
        if state is None:
            return {"error": "目标位置上没有样板提供者", "ref": str(ref)}
        return editable_view(state, self.policy)

    def view(self, node_key=None, ref=None):
        # type: (object, object) -> dict
        """只读视图。`ref` 给实例（带代次），`node_key` 给位置，都没有时列全部。"""
        if ref is not None:
            state = self.get_by_ref(ref)
            if state is None:
                return {"error": "该代次的样板提供者不存在", "ref": str(ref)}
            return {"provider": state.to_dict(), "editable": self.editable(ref)}
        if node_key is not None:
            state = self.get(node_key)
            if state is None:
                return {"error": "该位置上没有样板提供者", "node_key": str(node_key)}
            return {"provider": state.to_dict(), "editable": self.editable(state.ref)}
        return {
            "providers": [state.to_dict() for state in self.states()],
            "diagnostics": self.diagnostics(),
        }

    def diagnostics(self):
        # type: () -> dict
        by_device = {}
        usable = 0
        invalid = 0
        unreadable = 0
        for state in self._states.values():
            by_device[state.device_id] = by_device.get(state.device_id, 0) + 1
            usable += len(state.usable_entries())
            invalid += len(state.invalid)
            if state.unreadable:
                unreadable += 1
        return {
            "providers": len(self._states),
            "by_device": by_device,
            "patterns": sum(len(state.entries) for state in self._states.values()),
            "usable": usable,
            "invalid": invalid,
            "unreadable": unreadable,
            # **只读，不重建。** 诊断不该有副作用——在这里调
            # `_ensure_index()` 会让「谁在反复重建索引」被诊断自己扰动。
            "indexed_outputs": self.indexed_output_count(),
            "index_current": self.index_is_current(),
            "index_revision": self._index_revision,
            "tick": self.tick,
            "stats": dict(self.stats),
            "policy": self.policy.describe(),
            "unresolved": self.policy.unresolved_report(),
            "scope_note": (
                "样板结构、编解码、9 格读写与产物索引已实现；"
                "**执行半边（投料与回流）未实现**（07-05/06，依赖 09），"
                "预设配方适配未实现（07-03），编辑界面属任务 11。"
                "样板槽的变更由容器事件标脏、去抖后重读，"
                "因此索引永远是容器内容的派生——它不存档、也不需要迁移。"
            ),
        }

    def __repr__(self):
        return "PatternStore(%d 台提供者)" % (len(self._states),)


#: `PatternError` 的码 -> 命令层的 `Reason`。**两套词汇的会合点。**
#:
#: 分开是刻意的：`PatternError` 描述「这份样板自身哪里不对」（界面按它
#: 指到具体哪一行），`Reason` 描述「这条命令为什么被拒」（调用方按它决定
#: 下一步）。混成一套会让界面拿到一个太粗的码，或者让 09 拿到一个它
#: 不认识的说法。
_PATTERN_REASONS = {
    PatternError.NOT_A_PATTERN: Reason.PATTERN_NOT_A_TEMPLATE,
    PatternError.KIND_MISMATCH: Reason.PATTERN_KIND_MISMATCH,
    PatternError.FORMAT_TOO_NEW: Reason.PATTERN_FORMAT_UNSUPPORTED,
}


def reason_for_pattern_error(code):
    # type: (str) -> str
    """把一份样板的问题翻译成命令层的拒绝原因。**未登记的码归 `pattern_invalid`。**

    兜底那一支是有意的：将来新增 `PatternError` 码时忘了登记，得到的是
    「这块样板不合法」而不是一个空原因或异常——一个太粗的说法好过没有说法。
    """
    return _PATTERN_REASONS.get(code, Reason.PATTERN_INVALID)


def _candidate_sort_key(item):
    # type: (tuple) -> tuple
    """候选的稳定排序键：设备标识、位置、槽位。

    **它不是优先级。** 见 `PatternStore.candidates` 的说明。
    """
    state, entry = item
    return (state.device_id, state.node_key.as_key(), entry.slot)


def container_slot_count(adapter, fallback=None):
    # type: (object, object) -> int
    """一个容器适配器有多少格。

    **两个方法名都要试**，这是 13-01 那套契约的既有形状：
    `ContainerAdapter.slots_count()` 是通用只读面（假容器实现它），
    `SlotBackedContainer.slot_count()` 是按槽读写面（引擎容器实现它）。
    只认一个的话，「离线夹具通过、真机槽数 0」这种偏差就只能在游戏里暴露。

    都拿不到时退回**定义表那条已定案的 9**（`TEMPLATE_PATTERN_SLOT_COUNT`）——
    它同时就是 `gen_blocks.py` 给方块容器写的 `container_size`，
    因此这个回退不可能与真实格数分叉。
    """
    for name in ("slot_count", "slots_count"):
        counter = getattr(adapter, name, None)
        if not callable(counter):
            continue
        # **`None` 是文档允许的答案**（13-01：`slots_count()` 返回 None
        # 表示「不可枚举，如流体罐」）。先 `int()` 再判会在这里抛
        # `TypeError: int() argument must be ... not 'NoneType'`，
        # 而它逃不出调用方那圈捕获——一次发生在 `ensure` 里的抛错能让
        # 整帧的事件批次作废。
        size = counter()
        if size is None:
            continue
        try:
            size = int(size)
        except (TypeError, ValueError):
            continue
        if size > 0:
            return size
    if fallback is not None:
        return int(fallback)
    return int(TEMPLATE_PATTERN_SLOT_COUNT.value)


def _accepted_modes(policy, device_id):
    # type: (object, str) -> str
    """这台设备当前收哪些模式，用于拒绝理由的措辞。"""
    out = sorted(
        mode for mode in patterns.MODES if policy.accepts_mode(device_id, mode)
    )
    return "、".join(out) if out else "（当前口径下不收任何模式）"


def _short(digest):
    # type: (str) -> str
    """指纹的前若干字符。诊断里贴一整串规范树没人看得下去。"""
    text = digest or ""
    return text[:16] + u"…" if len(text) > 16 else text
