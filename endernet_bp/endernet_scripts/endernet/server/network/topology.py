# coding=utf-8
"""拓扑服务：连通分量、控制器计数、基带核心校验、线路树与图版本发布。

对应任务 02-02 至 02-07，2026-11-09 按网络改版（`.claude/13` §3、§4）重写结构部分。
本服务**只给拓扑与资格**：它不读写库存、不执行订单、不判定能源，也不分配带宽
额度（那是 `admission.py`）。它回答的是「谁和谁连在一起、这个分量可不可以工作、
每台设备挂在哪条线路的哪个位置、图版本是多少」。

六条纪律，逐条对应规划：

1. **相邻不等于连通。** 边由 `DeviceCatalog`（分类白名单 + 光缆按面的状态）与
   `node.PortRuleSet`（端口种类与方向）共同决定，见 `_link_verdict`。
2. **只重建受影响的部分。** 变化先标脏，调度阶段合并同帧脏点，
   用迭代 BFS 重建受影响分量，BFS 可跨帧续跑（`advance(step_budget)`）；
   分量走完之后的**线路树**（第二阶段）同样按步数预算分帧；
   没有脏点时 `advance` 一个节点都不访问（02 验收：无变化不逐块重搜）。
3. **重建期间撤销旧资格。** 标脏的那一刻，相关分量进入「重建中」，
   旧快照对它的资格立即失效；重建完成后先完成归属再发布新快照
   （02-06 的三段式：撤销 → 归属 → 发布）。
4. **未知不等于没有。** 分量里还有未加载的成员时，边界没有确认，
   零控制器、零基带只能报「未知」而不是「确认没有」——否则一个暂时不可访问的
   控制器会被当成已拆除，绕过「最多 1 个」的限制（02-03、架构规划 6.1）。
5. **结构在分量收尾时一次算完**（13 §3、§4）：
   - 基带核心校验：包围盒每轴 ≤ 7、只有一组结构、没有十字（`_validate_core`）；
   - 主控是否贴着基带；
   - 线路树：每个外露面（基带贴着光缆或设备的面）是一条线路的根，分层 BFS
     先走 32u 光缆、再 16u、再 8u、最后设备（透传），层内按 BFS 距离与位置定序；
     面板挂在宿主光缆下面；经连接器连成的一组处理器整组一起挂上去，只算一个
     消费者（`LineTree`）。
6. **成员全部加载才运行**（2026-11-09 作者改定，取代 01/03「只阻塞那台设备与主控」
   的放宽）：分量里任何一台设备所在区块未加载，整网离线。

**版本号的语义。** `version` 是全局单调递增的图版本，每次「发布」加一。
发布的时刻只有两种：受到影响的资格被撤销（进入重建中），
或一次重建真正完成并完成归属。命令持有 `topology_version`，
版本对不上就必须重取快照（`validate()` 返回 stale）。

**编号是可重建的。** 分量编号在进程内分配，同一个分量在成员增减时
尽量沿用旧编号（从旧成员里选覆盖最多的那一个），使服务迁移不因
加了一台设备就全量重挂；进程重启后编号会重新分配，这与架构规划 6.5
「临时网络编号及汇总索引可以重建」一致。线路与处理器群的编号
则由成员位置直接推导，因此是确定且可复现的。
"""

import heapq

from ...common.definitions import (
    CATEGORY_BASEBAND,
    CATEGORY_CABLE,
    CATEGORY_CONTROLLER,
    CATEGORY_PROCESSOR,
    CATEGORY_PROCESSOR_CONNECTOR,
    UNCONFIGURED,
)
from ...common.identity import (  # noqa: F401 (NodeKey 仅用于 # type: 注释)
    FACING_NAMES,
    NodeKey,
    PartKey,
    ref_key,
)
from ...common.protocol import BlockReason, Reason, TopologyChanged
from ..runtime import ChangeKind
from .node import PortRuleSet, opposite_face


# -- 状态词汇 --------------------------------------------------------------


class ControllerState(object):
    """一个连通分量里的「网络控制器」分组实例计数。"""

    #: 边界未确认（还有未加载成员），或正在重建中。**不等于没有控制器。**
    UNKNOWN = "unknown"
    #: 已确认零个：分量保留，但不能执行任何网络业务。
    NONE = "no_controller"
    #: 恰好一个：它是唯一的主控制器候选。
    SINGLE = "single"
    #: 两个及以上：非法连接，不选主、不容错。
    CONFLICT = "conflict"


class BasebandState(object):
    """一个连通分量里网络基带核心的校验结论（13 §3，照 AE2 控制器多方块）。

    违规的任何一种都让**整网**离线（13 §0.7）。几种同时成立时按
    包围盒 → 多组 → 十字 的顺序只报第一种（11 §2.2 的检查顺序）。
    """

    #: 重建中，或边界未确认时一块基带都没看到。**不等于没有基带。**
    UNKNOWN = "unknown"
    #: 已确认一块基带都没有。
    NONE = "none"
    #: 核心有效。
    VALID = "valid"
    #: 某组基带的包围盒有一轴超过 `CORE_MAX_SPAN`。
    TOO_LARGE = "too_large"
    #: 有多组互不相连的基带（两组经光缆或设备相连也算）。
    SPLIT = "split"
    #: 某块基带在两个及以上的轴上两侧都贴着基带。
    CROSS = "cross"


#: 基带核心包围盒每轴的上限（13 §3：「包围盒每轴 ≤ 7」）。
CORE_MAX_SPAN = 7

#: 不进线路树的分类：基带是根所在的核心，主控只贴核心、不在任何线路上。
_CORE_CATEGORIES = frozenset([CATEGORY_BASEBAND, CATEGORY_CONTROLLER])


class ProcessorOwnership(object):
    """处理器群到网络的归属。"""

    #: 群经连接器接入了网络（连接器贴着基带、光缆或设备），正常贡献槽位。
    ATTACHED = "attached"
    #: 没有任何连接器，或连接器没有接到网络：不贡献槽位（已确定的规则）。
    UNATTACHED = "unattached"
    # 原来还有「接到多个互不相连的网络、归属待定」（R03）。2026-11-09 作者确认
    # 关闭：改版后处理器群与它的连接器总在同一个网络里（两张网经群连上就是一张，
    # 两台主控报冲突），归属问题在结构上不再出现，这一态连同原因码一并删除。


class MigrationKind(object):
    """一条归属迁移的种类，供 03 决定挂载/卸载。"""

    #: 换了连通分量（合网、拆网、或从无分量到有分量）。
    COMPONENT = "component"
    #: 分量没变，但所在线路变了（`domain_ids` 即线路编号，见 `LineTree`）。
    DOMAIN = "domain"
    #: 该实例已经不在图里（被拆除或顶替）。
    DETACHED = "detached"


class LineKind(object):
    """线路树上节点的种类。"""

    #: 光缆：载体，按本档容量承载，自身不占带宽。
    CABLE = "cable"
    #: 整方块设备：消费者，同时按透传容量（R14-a）让线路经过它继续往下走。
    DEVICE = "device"
    #: 面板：只消费，不透传；挂在宿主光缆下面（13 §4.2）。
    PART = "part"
    #: 并行处理器：经连接器连成的一组整组一起挂上去，**整组只算一个消费者**
    #: （2026-11-09 作者定案，同 AE2 合成 CPU 多方块只占一个频道）。组里最先被
    #: 访问到的那台代表整组计带宽，其余几台不计；组内处理器之间的连接不受透传
    #: 容量限制；处理器不透传（13 §3），线路不经处理器往外走。
    PROCESSOR = "processor"


# -- 值对象 ----------------------------------------------------------------


def _as_key(key):
    # type: (object) -> tuple
    return key.as_key()


class _SortedMixin(object):
    """统一按位置排序，保证遍历与发布顺序确定。"""

    @staticmethod
    def _sort_keys(keys):
        return sorted(keys, key=_as_key)


class CoreVerdict(object):
    """一个分量的基带核心校验结果（13 §3）。分量收尾时算一次。"""

    __slots__ = ("state", "groups", "span", "crosses", "controller_attached")

    def __init__(self, state, groups=0, span=(0, 0, 0), crosses=(), controller_attached=None):
        # type: (str, int, tuple, tuple, object) -> None
        self.state = state
        #: 互不相连的基带组数。
        self.groups = int(groups)
        #: 各组包围盒在 x/y/z 三轴上的最大跨度（格数）。
        self.span = tuple(span)
        #: 构成十字的基带位置，按位置排序。
        self.crosses = tuple(crosses)
        #: 分量里的控制器是否都贴着基带；没有控制器时为 None。
        self.controller_attached = controller_attached

    def to_dict(self):
        # type: () -> dict
        return {
            "state": self.state,
            "groups": self.groups,
            "span": list(self.span),
            "crosses": [key.as_key() for key in self.crosses],
            "controller_attached": self.controller_attached,
        }

    def __repr__(self):
        return "CoreVerdict(%s, %d 组)" % (self.state, self.groups)


#: 核心违规的原因码与玩家话（13 §3 的表）。
_CORE_REASONS = {
    BasebandState.NONE: (Reason.BASEBAND_NONE, "网络中没有网络基带"),
    BasebandState.TOO_LARGE: (
        Reason.BASEBAND_TOO_LARGE,
        "网络基带超出 %d×%d×%d 的范围" % (CORE_MAX_SPAN, CORE_MAX_SPAN, CORE_MAX_SPAN),
    ),
    BasebandState.SPLIT: (Reason.BASEBAND_SPLIT, "网络里有多组互不相连的网络基带"),
    BasebandState.CROSS: (Reason.BASEBAND_CROSS, "网络基带不能摆成十字形"),
}


class ComponentInfo(_SortedMixin):
    """一个连通分量。"""

    __slots__ = (
        "component_id",
        "version",
        "members",
        "basebands",
        "controllers",
        "loaded_members",
        "partial",
        "pending",
        "controller_state",
        "reasons",
        "core",
    )

    def __init__(
        self,
        component_id,
        version,
        members,
        basebands,
        controllers,
        loaded_members,
        pending,
        controller_state,
        reasons=(),
        core=None,
    ):
        self.component_id = component_id
        self.version = version
        self.members = frozenset(members)
        self.basebands = frozenset(basebands)
        #: 控制器实例列表（`DeviceRef`），**含未加载的**，按位置排序。
        self.controllers = tuple(controllers)
        self.loaded_members = frozenset(loaded_members)
        #: True 表示分量里还有未加载成员，边界未确认。
        self.partial = len(loaded_members) != len(self.members)
        self.pending = bool(pending)
        self.controller_state = controller_state
        self.reasons = tuple(reasons)
        #: 基带核心的校验结果（`CoreVerdict`）；从未算过时为 None。
        self.core = core

    @property
    def master_candidate(self):
        """唯一主控候选；不是恰好一个时返回 None。"""
        if self.controller_state != ControllerState.SINGLE:
            return None
        return self.controllers[0]

    @property
    def baseband_state(self):
        # type: () -> str
        """基带核心的结论。重建中报未知；边界未确认时「一块都没有」也只报未知。"""
        if self.pending or self.core is None:
            return BasebandState.UNKNOWN
        if self.core.state == BasebandState.NONE and self.partial:
            return BasebandState.UNKNOWN
        return self.core.state

    @property
    def controller_attached(self):
        # type: () -> object
        """唯一的控制器是否贴着网络基带。不是恰好一个控制器时为 None。"""
        if self.controller_state != ControllerState.SINGLE or self.core is None:
            return None
        return self.core.controller_attached

    def is_runnable(self):
        """分量是否可以运行业务。仅回答拓扑条件，不含能源与带宽（13 §3）：

        成员全部加载且不在重建中；主控恰好一台，并且贴着基带核心；基带核心有效。

        「成员全部加载」是 2026-11-09 作者改定的：任何一台设备所在区块未加载，
        整网离线。此前 01/03 的约定是有意放宽的——未加载的成员只阻塞它自己与
        主控，其余照常工作——那条放宽随之作废。后续另行调研主动加载区块。
        """
        return (
            not self.pending
            and not self.partial
            and self.controller_state == ControllerState.SINGLE
            and self.baseband_state == BasebandState.VALID
            and self.controller_attached is True
        )

    def state_code(self):
        """给通知与诊断用的状态字符串。优先级与主控的 `_BLOCKER_STATES` 一致。"""
        if self.pending:
            return Reason.REBUILDING
        if self.controller_state == ControllerState.CONFLICT:
            return Reason.CONTROLLER_CONFLICT
        if self.controller_state == ControllerState.UNKNOWN:
            return Reason.TOPOLOGY_UNKNOWN
        if self.controller_state == ControllerState.NONE:
            return Reason.NO_CONTROLLER
        core_state = self.baseband_state
        if core_state in _CORE_REASONS:
            return _CORE_REASONS[core_state][0]
        if self.controller_attached is False:
            return Reason.CONTROLLER_DETACHED
        if self.partial:
            return Reason.UNLOADED_MEMBER
        return ""

    def reason_list(self):
        out = list(self.reasons)
        unloaded_text = "网络中有 %d 台设备所在区块未加载" % (
            len(self.members) - len(self.loaded_members),
        )
        if self.pending:
            out.append(BlockReason(Reason.REBUILDING, "网络正在刷新，请稍候"))
        if self.controller_state == ControllerState.NONE:
            out.append(BlockReason(Reason.NO_CONTROLLER, "网络中没有控制器"))
        elif self.controller_state == ControllerState.CONFLICT:
            out.append(
                BlockReason(
                    Reason.CONTROLLER_CONFLICT,
                    "网络中有 %d 个控制器，只能保留一个" % (len(self.controllers),),
                )
            )
        elif self.controller_state == ControllerState.UNKNOWN and self.partial:
            # 零控制器时「没有」作不了数（02-03），说法仍是「未知」。
            out.append(BlockReason(Reason.TOPOLOGY_UNKNOWN, unloaded_text))
        core_state = self.baseband_state
        if core_state in _CORE_REASONS:
            code, text = _CORE_REASONS[core_state]
            out.append(BlockReason(code, text))
        if self.controller_attached is False:
            out.append(BlockReason(Reason.CONTROLLER_DETACHED, "控制器没有贴着网络基带"))
        if self.partial and self.controller_state != ControllerState.UNKNOWN:
            # 2026-11-09 作者改定：有成员所在区块未加载，整网离线（见 `is_runnable`）。
            out.append(BlockReason(Reason.UNLOADED_MEMBER, unloaded_text))
        return out

    def to_dict(self):
        # type: () -> dict
        return {
            "component_id": self.component_id,
            "version": self.version,
            "members": len(self.members),
            "basebands": len(self.basebands),
            "controllers": [ref.to_dict() for ref in self.controllers],
            "controller_state": self.controller_state,
            "baseband_state": self.baseband_state,
            "controller_attached": self.controller_attached,
            "core": self.core.to_dict() if self.core is not None else None,
            "pending": self.pending,
            "partial": self.partial,
            "runnable": self.is_runnable(),
            "reasons": [reason.to_dict() for reason in self.reason_list()],
        }

    def __repr__(self):
        return "ComponentInfo(%s, %s, %s, %d 个成员)" % (
            self.component_id,
            self.controller_state,
            self.baseband_state,
            len(self.members),
        )


class LineRoot(object):
    """一条线路的根：基带的一个外露面（13 §4.1）。容量 = 该基带的档位。"""

    __slots__ = ("root_id", "baseband", "face", "child", "device_id", "capacity_quarters")

    def __init__(self, root_id, baseband, face, child, device_id, capacity_quarters):
        # type: (str, NodeKey, str, NodeKey, str, object) -> None
        self.root_id = root_id
        self.baseband = baseband
        self.face = face
        #: 贴在这一面上的光缆或设备（这条线路的第一个节点）。
        self.child = child
        self.device_id = device_id
        #: 四分之一 u；基带漏配容量时是 `UNCONFIGURED`。
        self.capacity_quarters = capacity_quarters

    def to_dict(self):
        # type: () -> dict
        return {
            "root_id": self.root_id,
            "baseband": self.baseband.as_key(),
            "face": self.face,
            "device_id": self.device_id,
            "capacity_quarters": (
                None if self.capacity_quarters is UNCONFIGURED else self.capacity_quarters
            ),
        }

    def __repr__(self):
        return "LineRoot(%s)" % (self.root_id,)


class LineNode(object):
    """线路树上的一个节点：光缆、设备、处理器（`NodeKey`）或面板（`PartKey`）。"""

    __slots__ = (
        "key",
        "kind",
        "device_id",
        "parent",
        "root_id",
        "depth",
        "order",
        "capacity_quarters",
        "group",
    )

    def __init__(
        self, key, kind, device_id, parent, root_id, depth, order, capacity_quarters, group=None
    ):
        self.key = key
        self.kind = kind
        self.device_id = device_id
        #: 父节点的键；线路的第一个节点挂在根上，这里是根的 `root_id`（字符串）。
        self.parent = parent
        self.root_id = root_id
        #: 离根的跳数，根的子节点为 1。
        self.depth = depth
        #: 全分量的访问序号，准入按它先到先得。
        self.order = order
        #: 这一段能让多少带宽经过：光缆取本档、设备取透传容量；面板与处理器为 None
        #: （处理器组内的连接不受透传容量限制）。
        self.capacity_quarters = capacity_quarters
        #: 处理器所在那一组的代表（组里最先被访问到的那台）；其余种类为 None。
        self.group = group

    def is_consumer(self):
        # type: () -> bool
        """要单独计带宽的节点：设备、面板，以及处理器组的代表。"""
        if self.kind == LineKind.PROCESSOR:
            return self.group == self.key
        return self.kind != LineKind.CABLE

    def is_group_member(self):
        # type: () -> bool
        """处理器组里不是代表的那几台：不计带宽，结论随代表。"""
        return self.kind == LineKind.PROCESSOR and self.group != self.key

    def __repr__(self):
        return "LineNode(%s, %s, #%d)" % (self.device_id, self.kind, self.order)


class LineTree(object):
    """一个分量的线路树（13 §4.2）。

    每个外露面是一条线路的根。分层 BFS：先只走 32u 光缆，再 16u，再 8u，最后走
    设备（透传）；同一层里按离根的跳数、再按位置定序。一个节点**第一次被访问
    就定下父节点**，之后不再回头（AE2 `PathingCalculation` 同一口径），因此结果
    只取决于世界本身，与事件到达顺序无关。面板在宿主光缆被展开时挂上去。

    线段的标识：节点用它的键（`NodeKey` / `PartKey`），根用 `root_id` 字符串。
    """

    __slots__ = (
        "roots",
        "nodes",
        "order",
        "hosted",
        "_roots_by_id",
        "_keys_by_root",
        "_roots_by_baseband",
        "_group_sizes",
    )

    def __init__(self, roots, nodes, order, hosted=None):
        self.roots = tuple(roots)
        self.nodes = dict(nodes)
        #: 访问顺序（键）。
        self.order = tuple(order)
        #: 分量里**每一格**光缆挂着的面板（含线路没走到的）：`PartKey -> 面板设备标识`。
        self.hosted = dict(hosted or {})
        self._roots_by_id = dict((root.root_id, root) for root in self.roots)
        by_root = {}
        for key in self.order:
            by_root.setdefault(self.nodes[key].root_id, []).append(key)
        self._keys_by_root = dict((root_id, tuple(keys)) for root_id, keys in by_root.items())
        by_baseband = {}
        for root in self.roots:
            by_baseband.setdefault(root.baseband, []).append(root.root_id)
        self._roots_by_baseband = dict(
            (key, tuple(sorted(root_ids))) for key, root_ids in by_baseband.items()
        )
        sizes = {}
        for node in self.nodes.values():
            if node.group is not None:
                sizes[node.group] = sizes.get(node.group, 0) + 1
        self._group_sizes = sizes

    # -- 查询 ----------------------------------------------------------

    def root(self, root_id):
        # type: (str) -> LineRoot
        return self._roots_by_id.get(root_id)

    def root_ids(self):
        # type: () -> list
        return [root.root_id for root in self.roots]

    def node(self, key):
        # type: (object) -> LineNode
        return self.nodes.get(key)

    def line_of(self, key):
        # type: (object) -> object
        """这个键所在线路的根编号；不在任何线路上（含基带与主控）时为 None。"""
        node = self.nodes.get(key)
        return node.root_id if node is not None else None

    def line_ids_of(self, key):
        # type: (object) -> tuple
        """节点所在的线路；基带给出它各外露面的线路；其余为空。"""
        node = self.nodes.get(key)
        if node is not None:
            return (node.root_id,)
        return self._roots_by_baseband.get(key, ())

    def keys_of(self, root_id):
        # type: (str) -> tuple
        """一条线路上的全部节点，按访问顺序。"""
        return self._keys_by_root.get(root_id, ())

    def consumers_of(self, root_id):
        # type: (str) -> list
        """一条线路上要计带宽的消费者（设备、面板、处理器组的代表），按访问顺序。"""
        return [key for key in self.keys_of(root_id) if self.nodes[key].is_consumer()]

    def reported_of(self, root_id):
        # type: (str) -> list
        """一条线路上有带宽结论的全部节点（消费者加处理器组的其余成员）。"""
        return [
            key for key in self.keys_of(root_id) if self.nodes[key].kind != LineKind.CABLE
        ]

    def group_size(self, entry):
        # type: (object) -> int
        """以 `entry` 为代表的处理器组有几台；不是代表时为 0。"""
        return self._group_sizes.get(entry, 0)

    def is_segment_root(self, segment_id):
        # type: (object) -> bool
        """这一段是不是根（根用 `root_id` 字符串表示，节点用键）。

        不按类型判断：py2 下同一个编号可能是 `str` 也可能是 `unicode`。
        """
        return segment_id in self._roots_by_id

    def path_of(self, key):
        # type: (object) -> tuple
        """从父节点一路到根的各段标识（不含它自己），最后一个是根的 `root_id`。"""
        node = self.nodes.get(key)
        if node is None:
            return ()
        out = []
        parent = node.parent
        while not self.is_segment_root(parent):
            out.append(parent)
            parent = self.nodes[parent].parent
        out.append(parent)
        return tuple(out)

    def capacity_of(self, segment_id):
        # type: (object) -> object
        """一段的容量：根取档位，光缆取本档，设备取透传容量。"""
        if self.is_segment_root(segment_id):
            return self._roots_by_id[segment_id].capacity_quarters
        return self.nodes[segment_id].capacity_quarters

    def device_id_of(self, segment_id):
        # type: (object) -> str
        if self.is_segment_root(segment_id):
            return self._roots_by_id[segment_id].device_id
        return self.nodes[segment_id].device_id

    def signature_of(self, root_id):
        # type: (str) -> tuple
        """一条线路的结构指纹，供准入判断「这条线路变没变」。

        首项是根（基带型号与容量），其后按访问顺序逐个节点：位置、父节点、种类、
        型号、容量。两次重建的指纹相同，说明这条线路的形状与先后顺序都没变。
        """
        root = self._roots_by_id[root_id]
        entries = [(root.device_id, root.capacity_quarters)]
        for key in self.keys_of(root_id):
            node = self.nodes[key]
            parent = node.parent
            parent_mark = parent if self.is_segment_root(parent) else parent.as_key()
            entries.append(
                (key.as_key(), parent_mark, node.kind, node.device_id, node.capacity_quarters)
            )
        return tuple(entries)

    def to_dict(self):
        # type: () -> dict
        return {
            "roots": [root.to_dict() for root in self.roots],
            "nodes": len(self.nodes),
            "parts": len(self.hosted),
        }

    def __repr__(self):
        return "LineTree(%d 条线路, %d 个节点)" % (len(self.roots), len(self.nodes))


class ProcessorGroup(_SortedMixin):
    """一个处理器群：由相互连接的并行处理器构成，经连接器接入网络。

    blocks.md：单个处理器提供 1 个配方处理槽位；未通过连接器接入网络的
    处理器不提供槽位。同一处理器可经多条路径接入，**只计一次**。
    `domain_ids` 是群的连接器所在的线路编号（诊断用）。
    """

    __slots__ = (
        "group_id",
        "component_id",
        "processors",
        "connectors",
        "domain_ids",
        "ownership",
        "reasons",
    )

    def __init__(
        self,
        group_id,
        component_id,
        processors,
        connectors,
        domain_ids,
        ownership,
        reasons=(),
    ):
        self.group_id = group_id
        self.component_id = component_id
        self.processors = frozenset(processors)
        self.connectors = frozenset(connectors)
        self.domain_ids = tuple(sorted(domain_ids))
        self.ownership = ownership
        self.reasons = tuple(reasons)

    @property
    def slots(self):
        # type: () -> int
        """该群提供多少槽位。每个处理器一个，去重后计数。"""
        return len(self.processors)

    def contributes_slots(self):
        # type: () -> bool
        """是否真的向网络贡献槽位。归属待定时不予贡献。

        **工厂服务不靠这个数开工。** 它每帧从快照重算本网络的槽位容量
        （`crafting/orders/capacity.py` 的 `slot_capacity`），那里读的是
        `contributes_slots()` 与 `slots`，且**另带一层跨群去重**：02 建群
        时按 `visited` 去过重，工厂那一层不去信这个保证（万一去重被改坏，
        症状是槽位凭空变多，不设防的话几乎查不出来）。

        2026-09-28（台账 D-03）：这里曾经还有一个 `slot_count()`——本方法的
        `slots` 简写，唯一调用方是 02 的槽位来源清单，而那份清单没有消费者。
        清单连同它一并删除，理由见 `crafting/orders/capacity.py` 模块 docstring。
        """
        return self.ownership == ProcessorOwnership.ATTACHED

    def to_dict(self):
        # type: () -> dict
        return {
            "group_id": self.group_id,
            "component_id": self.component_id,
            "processors": [key.as_key() for key in self._sort_keys(self.processors)],
            "connectors": [key.as_key() for key in self._sort_keys(self.connectors)],
            "domain_ids": list(self.domain_ids),
            "ownership": self.ownership,
            "slots": self.slots,
            "contributes": self.contributes_slots(),
            "reasons": [reason.to_dict() for reason in self.reasons],
        }

    def __repr__(self):
        return "ProcessorGroup(%s, %s, %d 个处理器)" % (
            self.group_id,
            self.ownership,
            len(self.processors),
        )


class TopologyVerdict(_SortedMixin):
    """一次「这个实例现在能不能动业务」的判定。

    `status` 取 `common/protocol.py` 的结果状态：
    可运行是 `ok`，重建中是 `blocked`，实例或版本失效是 `stale`。
    """

    __slots__ = (
        "allowed",
        "status",
        "reasons",
        "version",
        "component_id",
        "domain_ids",
        "state",
    )

    def __init__(self, allowed, status, reasons=(), version=None, component_id=None, domain_ids=(), state=""):
        self.allowed = allowed
        self.status = status
        self.reasons = tuple(reasons)
        self.version = version
        self.component_id = component_id
        self.domain_ids = tuple(domain_ids)
        self.state = state

    def __bool__(self):
        return self.allowed

    __nonzero__ = __bool__

    def reason_codes(self):
        # type: () -> tuple
        return tuple(
            reason.code if isinstance(reason, BlockReason) else str(reason)
            for reason in self.reasons
        )

    def to_dict(self):
        # type: () -> dict
        return {
            "allowed": self.allowed,
            "status": self.status,
            "state": self.state,
            "reasons": [
                reason.to_dict() if isinstance(reason, BlockReason) else reason
                for reason in self.reasons
            ],
            "version": self.version,
            "component_id": self.component_id,
            "domain_ids": list(self.domain_ids),
        }

    def __repr__(self):
        return "TopologyVerdict(%s, %s)" % (self.allowed, self.status)


class LinkState(_SortedMixin):
    """一台设备的连接状态：连上了谁、谁没连上、为什么。

    02-07 要求「不把业务错误仅显示为离线」，因此被拒绝的邻居必须带着
    具体原因（分类不允许、光缆这一面切断或被面板占住、端口/方向不允许），
    而不是简单地不出现。
    """

    __slots__ = (
        "node_key",
        "device_id",
        "component_id",
        "linked",
        "rejected",
        "pending",
        "controller_state",
        "runnable",
        "reasons",
    )

    def __init__(
        self,
        node_key,
        device_id,
        component_id,
        linked=(),
        rejected=(),
        pending=True,
        controller_state=ControllerState.UNKNOWN,
        runnable=False,
        reasons=(),
    ):
        self.node_key = node_key
        self.device_id = device_id
        self.component_id = component_id
        #: 已建立连接边的邻居位置。
        self.linked = tuple(linked)
        #: `(邻居位置, 设备标识, 原因码, 说明)`，按位置排序。
        self.rejected = tuple(rejected)
        self.pending = pending
        self.controller_state = controller_state
        self.runnable = runnable
        self.reasons = tuple(reasons)

    def to_dict(self):
        # type: () -> dict
        return {
            "node_key": self.node_key.to_dict(),
            "device_id": self.device_id,
            "component_id": self.component_id,
            "linked": [key.as_key() for key in self._sort_keys(self.linked)],
            "rejected": [
                {
                    "node_key": key.as_key(),
                    "device_id": device_id,
                    "reason": reason,
                    "detail": detail,
                }
                for key, device_id, reason, detail in self.rejected
            ],
            "pending": self.pending,
            "controller_state": self.controller_state,
            "runnable": self.runnable,
            "reasons": [
                reason.to_dict() if isinstance(reason, BlockReason) else reason
                for reason in self.reasons
            ],
        }

    def __repr__(self):
        return "LinkState(%s, %d 条边, %d 条拒绝)" % (
            self.device_id,
            len(self.linked),
            len(self.rejected),
        )


class MigrationNotice(_SortedMixin):
    """一台设备的归属变化。02-06 交给 03 决定挂载/卸载。"""

    __slots__ = (
        "node_key",
        "device_id",
        "ref",
        "kind",
        "from_component_id",
        "to_component_id",
        "from_domain_ids",
        "to_domain_ids",
    )

    def __init__(
        self,
        node_key,
        device_id,
        ref,
        kind,
        from_component_id,
        to_component_id,
        from_domain_ids=(),
        to_domain_ids=(),
    ):
        self.node_key = node_key
        self.device_id = device_id
        self.ref = ref
        self.kind = kind
        self.from_component_id = from_component_id
        self.to_component_id = to_component_id
        self.from_domain_ids = tuple(from_domain_ids)
        self.to_domain_ids = tuple(to_domain_ids)

    def to_dict(self):
        # type: () -> dict
        return {
            "node_key": self.node_key.to_dict(),
            "device_id": self.device_id,
            "generation": self.ref.generation if self.ref is not None else None,
            "kind": self.kind,
            "from_component_id": self.from_component_id,
            "to_component_id": self.to_component_id,
            "from_domain_ids": list(self.from_domain_ids),
            "to_domain_ids": list(self.to_domain_ids),
        }

    def __repr__(self):
        return "MigrationNotice(%s, %s -> %s)" % (
            self.device_id,
            self.from_component_id,
            self.to_component_id,
        )


def _host_of(key):
    # type: (object) -> NodeKey
    """部件键 → 它所在的那一格；`NodeKey` 原样返回。"""
    if isinstance(key, PartKey):
        return key.node_key
    return key


class TopologySnapshot(object):
    """一份稳定的图快照。

    发布后不再变化：再次重建会生成**新的**快照对象，
    旧快照继续可读（但它的版本号已经过期，`validate()` 会拒绝基于它的新转移）。
    查询都接受 `PartKey`：面板按宿主那一格查分量与重建状态。
    """

    __slots__ = (
        "version",
        "tick",
        "components",
        "line_trees",
        "processor_groups",
        "pending_keys",
        "_by_node",
        "_lines_by_node",
        "_group_by_node",
    )

    def __init__(self, version, tick, components, line_trees, processor_groups, pending_keys):
        self.version = version
        self.tick = tick
        self.components = dict(components)
        #: 分量编号 -> `LineTree`。
        self.line_trees = dict(line_trees)
        self.processor_groups = dict(processor_groups)
        #: 尚未归属到任何分量的位置（新设备、刚被拆掉的位置）。
        self.pending_keys = frozenset(pending_keys)
        self._by_node = None
        self._lines_by_node = None
        self._group_by_node = None

    # -- 惰性反向索引 --------------------------------------------------

    def _index(self):
        if self._by_node is not None:
            return
        by_node = {}
        for component_id, component in self.components.items():
            for node_key in component.members:
                by_node[node_key] = component_id
        self._by_node = by_node

        lines_by_node = {}
        for tree in self.line_trees.values():
            for key in tree.nodes:
                lines_by_node[key] = tree.line_ids_of(key)
            for root in tree.roots:
                lines_by_node[root.baseband] = tree.line_ids_of(root.baseband)
        self._lines_by_node = lines_by_node

        group_by_node = {}
        for group in self.processor_groups.values():
            for node_key in group.processors:
                group_by_node[node_key] = group.group_id
        self._group_by_node = group_by_node

    # -- 查询 ----------------------------------------------------------

    def component_id_of(self, node_key):
        # type: (object) -> object
        self._index()
        return self._by_node.get(_host_of(node_key))

    def component_of(self, node_key):
        # type: (object) -> ComponentInfo
        return self.components.get(self.component_id_of(node_key))

    def line_tree_of(self, node_key):
        # type: (object) -> LineTree
        """该位置（或面板）所在分量的线路树；不属于任何分量时为 None。"""
        return self.line_trees.get(self.component_id_of(node_key))

    def line_of(self, key):
        # type: (object) -> object
        """设备、光缆或面板所在线路的根编号；不在任何线路上时为 None。"""
        tree = self.line_tree_of(key)
        return tree.line_of(key) if tree is not None else None

    def domain_ids_of(self, node_key):
        # type: (object) -> tuple
        """该设备的「带宽域」，2026-11-09 起就是它所在的线路（至多一条）。

        基带给出它各外露面的线路；主控与没走到的设备为空。名字沿用旧的
        「负载域」接口（迁移通知、设备状态栏的 `bandwidth_domain_id`）。
        """
        self._index()
        return self._lines_by_node.get(node_key, ())

    def processor_group_of(self, node_key):
        # type: (NodeKey) -> ProcessorGroup
        self._index()
        group_id = self._group_by_node.get(node_key)
        return self.processor_groups.get(group_id) if group_id is not None else None

    def is_pending(self, node_key):
        # type: (object) -> bool
        """该位置是否处于「结果待确认」：分量在重建，或自己还没有归属。"""
        component = self.component_of(node_key)
        if component is None:
            return True
        return component.pending or _host_of(node_key) in self.pending_keys

    def controller_state_of(self, node_key):
        # type: (object) -> str
        component = self.component_of(node_key)
        if component is None:
            return ControllerState.UNKNOWN
        return component.controller_state

    def master_candidates(self):
        # type: () -> list
        return [
            component.master_candidate
            for component in self.components.values()
            if component.master_candidate is not None
        ]

    def summary(self):
        # type: () -> dict
        states = {}
        cores = {}
        for component in self.components.values():
            states[component.controller_state] = (
                states.get(component.controller_state, 0) + 1
            )
            cores[component.baseband_state] = cores.get(component.baseband_state, 0) + 1
        return {
            "version": self.version,
            "components": len(self.components),
            "lines": sum(len(tree.roots) for tree in self.line_trees.values()),
            "processor_groups": len(self.processor_groups),
            "pending": len(self.pending_keys),
            "controller_states": states,
            "baseband_states": cores,
        }

    def to_dict(self):
        # type: () -> dict
        report = self.summary()
        report["component_list"] = [
            self.components[key].to_dict() for key in sorted(self.components)
        ]
        report["line_trees"] = dict(
            (key, self.line_trees[key].to_dict()) for key in sorted(self.line_trees)
        )
        report["processor_group_list"] = [
            self.processor_groups[key].to_dict() for key in sorted(self.processor_groups)
        ]
        return report

    def __repr__(self):
        return "TopologySnapshot(v%d, %d 个分量)" % (self.version, len(self.components))


class TopologyPublication(object):
    """一次发布的全部结果。监听者（03、09、11）按需取用。

    **这里只有「变化」是给人推的，容量不是。** `migrations` 有真消费者
    （03 的 `_apply_migration` 与 `admission`），因为服务迁移**必须**在
    发布的那一刻被处理；槽位容量不同——09 每帧从 `snapshot` 重算
    （`crafting/orders/capacity.py`），推一份容量增量的清单只会是第二份来源。
    02 曾经发过这样一份清单（`slot_changes`），2026-09-28 随台账 D-03
    删除，逐条理由见该模块 docstring。
    """

    __slots__ = (
        "version",
        "tick",
        "snapshot",
        "migrations",
        "reasons",
        "rebuilt_components",
        "notification",
    )

    def __init__(self, version, tick, snapshot, migrations, reasons, rebuilt_components):
        self.version = version
        self.tick = tick
        self.snapshot = snapshot
        self.migrations = tuple(migrations)
        self.reasons = tuple(reasons)
        self.rebuilt_components = tuple(rebuilt_components)
        self.notification = TopologyChanged(
            version, None, self._run_state(snapshot), self.reasons
        )

    @staticmethod
    def _run_state(snapshot):
        if not snapshot.components:
            return ControllerState.UNKNOWN
        if any(component.pending for component in snapshot.components.values()):
            return Reason.REBUILDING
        if any(
            component.controller_state == ControllerState.CONFLICT
            for component in snapshot.components.values()
        ):
            return ControllerState.CONFLICT
        return ControllerState.SINGLE

    def to_dict(self):
        # type: () -> dict
        return {
            "version": self.version,
            "rebuilt": list(self.rebuilt_components),
            "migrations": [item.to_dict() for item in self.migrations],
            "reasons": [item.to_dict() for item in self.reasons],
        }

    def __repr__(self):
        return "TopologyPublication(v%d, %d 条迁移)" % (
            self.version,
            len(self.migrations),
        )


class RebuildReport(object):
    """`advance()` 的一帧结果。"""

    __slots__ = (
        "version",
        "steps",
        "components_rebuilt",
        "seed_pops",
        "published",
        "queued",
        "structure_nodes",
    )

    def __init__(
        self, version, steps, components_rebuilt, seed_pops, published, queued, structure_nodes=0
    ):
        self.version = version
        #: 受预算约束的节点展开次数（分量 BFS 与线路树两个阶段合计）
        self.steps = steps
        self.components_rebuilt = components_rebuilt
        self.seed_pops = seed_pops
        self.published = published
        self.queued = queued
        #: 本帧在**归属阶段**额外扫描的成员数（核心校验、面板、处理器群）。
        #: 它**不**受 `step_budget` 约束，见 `TopologyService.stats` 的说明。
        self.structure_nodes = structure_nodes

    def to_dict(self):
        # type: () -> dict
        return {
            "version": self.version,
            "steps": self.steps,
            "components_rebuilt": self.components_rebuilt,
            "seed_pops": self.seed_pops,
            "published": self.published,
            "queued": self.queued,
            "structure_nodes": self.structure_nodes,
        }

    def __repr__(self):
        return "RebuildReport(v%d, %d 步, %d 个分量)" % (
            self.version,
            self.steps,
            self.components_rebuilt,
        )


# -- 服务 ------------------------------------------------------------------


class TopologyService(object):
    """建立并维护网络图。

    `registry` / `catalog` 来自任务 01；`ports` 是 02-01 的端口规则表。
    本服务**只读**注册表，不修改设备记录。
    """

    def __init__(
        self,
        definitions,
        registry,
        catalog,
        ports=None,
        step_budget=64,
    ):
        # type: (object, object, object, PortRuleSet, int) -> None
        self.definitions = definitions
        self.registry = registry
        self.catalog = catalog
        self.ports = ports if ports is not None else PortRuleSet()
        self.step_budget = int(step_budget)

        #: 图版本。每次发布加一。
        self.version = 0
        self.tick_counter = 0

        # -- 分量归属 --
        self._membership = {}
        #: component_id -> set(NodeKey)
        self._members_by_id = {}
        self._next_component_id = 0

        # -- 重建游标 --
        self._seeds = set()
        #: 已标脏、结果待确认的位置
        self._pending_keys = set()
        #: 正在重建、旧资格已撤销的分量
        self._pending_components = set()
        self._cursor = None
        self._needs_publish = False
        #: 跨帧重建期间新到的变更，游标结束后要重扫（见 `_seed`）。
        self._marked_during_pass = set()
        #: 最近一次归属阶段扫描的成员数，计入 `RebuildReport`。
        self._last_finish_nodes = 0

        # -- 派生结构（只有被重建的分量会更新，其余沿用）--
        #: component_id -> LineTree
        self._trees = {}
        #: component_id -> CoreVerdict
        self._cores = {}
        self._groups = {}
        self._group_ids_by_component = {}

        self._snapshot = TopologySnapshot(0, 0, {}, {}, {}, ())
        self._listeners = []
        self._touched = set()
        self._pass_merges = []
        self._pass_splits = []
        self._pass_reasons = []

        self.stats = {
            "marks": 0,
            "seed_pops": 0,
            "node_visits": 0,
            #: 线路树阶段展开的节点数（与 `node_visits` 一样受步数预算约束）。
            "line_visits": 0,
            "components_rebuilt": 0,
            "publications": 0,
            "splits": 0,
            "merges": 0,
            "port_rejections": 0,
            "empty_passes": 0,
            #: 归属阶段扫描的成员总数。**不受步数预算约束**：一个分量的线路树
            #: 走完之后，核心校验、面板清点与处理器群就在同一帧完成。
            #: 这一段是 O(分量规模)，大网络上需要 04 的预算重新评估。
            "structure_nodes": 0,
        }

    # -- 标脏 ----------------------------------------------------------

    def mark_dirty(self, node_key):
        # type: (NodeKey) -> None
        """标记一个位置需要重建（02-02 的入口）。

        标脏即刻生效：相关分量立刻进入「重建中」，旧快照对它的资格
        在**下一次发布**时被撤销。这就是 02-06 的「撤销旧资格」阶段。
        光缆的切断面或面板变了（阶段 C 写完 `config` 之后）也走这里。
        """
        self._seed(node_key)

    def mark_all_dirty(self):
        # type: () -> int
        """把所有已注册设备标脏。用于启动与手动重扫。"""
        count = 0
        for record in self.registry.all_records():
            self._seed(record.node_key)
            count += 1
        return count

    def _seed(self, node_key):
        # type: (NodeKey) -> None
        self.stats["marks"] += 1
        self._seeds.add(node_key)
        component_id = self._membership.get(node_key)
        if component_id is not None:
            self._pending_components.add(component_id)
        else:
            self._pending_keys.add(node_key)
            # 新位置还没有归属，但它可能把邻接的几个分量连起来——
            # 那些分量的旧资格必须立刻撤销，否则「撤销旧资格」这一步
            # 就要依赖调用方每次都记得报告邻接变化。
            #
            # 撤销的同时**必须把那个邻居也标脏**：「重建中」只在分量被重建时
            # 才会解除，如果这个分量一个种子都没有，它会永远停在待确认，
            # 而它本来什么事都没有。
            for neighbor_key in node_key.neighbors():
                neighbor_component = self._membership.get(neighbor_key)
                if neighbor_component is not None:
                    self._seeds.add(neighbor_key)
                    self._pending_components.add(neighbor_component)
        if self._cursor is not None:
            # 跨帧重建期间到达的变更：邻接可能在游标走过之后才改变，
            # 所以这一帧的重建结果不能算数，等游标结束后重新扫一遍。
            self._marked_during_pass.add(node_key)
        self._needs_publish = True

    # -- 生命周期变化的消费者 ------------------------------------------

    def on_lifecycle_change(self, change):
        # type: (object) -> None
        """`ServerBootstrap` 的变更接收端。

        「尝试破坏」不改图（方块还在），「面板正面那一格变了」也不改图（面板只连宿主，
        正面对着什么与连接无关），因此都不标脏；其余变化都只标脏，真正的重建留到推进阶段，
        保证同帧多次变化只算一次。
        """
        if change.kind in (ChangeKind.PLACEMENT_REVIEW, ChangeKind.PART_FRONT_CHANGED):
            return
        if change.node_key is not None:
            self.mark_dirty(change.node_key)

    def add_listener(self, listener):
        # type: (object) -> None
        self._listeners.append(listener)

    def remove_listener(self, listener):
        # type: (object) -> None
        if listener in self._listeners:
            self._listeners.remove(listener)

    # -- 推进 ----------------------------------------------------------

    def advance(self, step_budget=None):
        # type: (int) -> RebuildReport
        """有限步推进重建。没有脏点时不会访问任何节点。

        `step_budget` 是**节点展开次数**预算（分量 BFS 与线路树合计）；
        一个分量可以跨多帧重建，游标保存在服务里。
        """
        if step_budget is None:
            step_budget = self.step_budget
        self.tick_counter += 1

        steps = 0
        finished = []
        pops = 0
        structure_nodes = 0
        while steps < step_budget:
            if self._cursor is None:
                seed = self._pop_seed()
                if seed is None:
                    break
                pops += 1
                if not self._start_cursor(seed):
                    continue
            used = self._step_cursor(step_budget - steps)
            steps += used
            if self._cursor_done():
                component_id = self._finish_cursor()
                if component_id is not None:
                    finished.append(component_id)
                    structure_nodes += self._last_finish_nodes

        published = self._publish_if_needed(finished)
        queued = len(self._seeds) + (1 if self._cursor is not None else 0)
        if steps == 0 and not finished and not published:
            self.stats["empty_passes"] += 1
        return RebuildReport(
            self.version, steps, len(finished), pops, published is not None, queued,
            structure_nodes,
        )

    def _pop_seed(self):
        # type: () -> NodeKey
        if not self._seeds:
            return None
        seed = min(self._seeds, key=_as_key)
        self._seeds.discard(seed)
        self.stats["seed_pops"] += 1
        return seed

    def _start_cursor(self, seed):
        # type: (NodeKey) -> bool
        """从种子开始一次 BFS。返回 False 表示这个种子没有可重建的东西。"""
        record = self.registry.get(seed)
        if record is None:
            # 被拆掉的位置：它自己已经没有节点，改从原分量的其余成员重建，
            # 否则「删掉桥接基带后分量拆开」永远不会被发现。
            old_id = self._membership.get(seed)
            if old_id is None:
                # 位置上没有节点、当前也没有归属：没有可重建的东西。
                #
                # **不要顺手把它从 `_touched` 里删掉。** 同一个位置也可能刚刚
                # 作为原分量的成员被扫过（邻接的重建先跑完，成员桶里还留着它），
                # 那样删掉就把这条 `DETACHED` 迁移通知吞了——而 03 靠它卸载
                # 已拆除设备的服务。真的从来没归属过的位置不会因此多出通知：
                # `_diff_membership` 要求「上一份快照里它属于某个分量」才发。
                self._pending_keys.discard(seed)
                return False
            leftovers = [
                key
                for key in self._members_by_id.get(old_id, ())
                if key != seed
            ]
            self._clear_membership(seed)
            # **只撤掉派生索引，不拆成员桶**：拆线后的两半要靠
            # 「谁还留在旧桶里」来判定，桶先没了的话第一半会被直接
            # 顶到旧编号上，第二半则被当成多余成员拆掉重编——
            # 结果就是没有任何真实变化的那一侧也被通知「换了网络」。
            self._retire_indices(old_id)
            # 记进本批变更：它要产出一条「已脱离图」的迁移通知，
            # 让 03 卸载对应的服务。
            self._touched.add(seed)
            for key in sorted(leftovers, key=_as_key):
                self._seed(key)
            self._needs_publish = True
            return False

        self._cursor = {
            "seed": seed,
            #: 1 = 分量 BFS；2 = 线路树（见 `_begin_lines`）。
            "phase": 1,
            "queue": [seed],
            "head": 0,
            "visited": set([seed]),
            "members": [seed],
            #: 第一阶段顺手记下的邻接表，第二阶段与收尾都读它，不再重新探邻。
            "adjacency": {},
            #: 游标启动前就在队列里的种子。跨帧期间新标脏的种子不在此列，
            #: 因此不会被「本次重建已经覆盖」误判而丢弃。
            "seeds_at_start": set(self._seeds),
        }
        return True

    def _step_cursor(self, steps):
        # type: (int) -> int
        cursor = self._cursor
        used = 0
        if cursor["phase"] == 1:
            queue = cursor["queue"]
            head = cursor["head"]
            visited = cursor["visited"]
            members = cursor["members"]
            adjacency = cursor["adjacency"]
            while head < len(queue) and used < steps:
                key = queue[head]
                head += 1
                used += 1
                self.stats["node_visits"] += 1
                linked = self._linked_neighbors(key)
                adjacency[key] = linked
                for neighbor_key in linked:
                    if neighbor_key in visited:
                        continue
                    visited.add(neighbor_key)
                    queue.append(neighbor_key)
                    members.append(neighbor_key)
            cursor["head"] = head
            if head >= len(queue):
                self._begin_lines(cursor)
        if cursor["phase"] == 2 and used < steps:
            used += self._step_lines(cursor, steps - used)
        return used

    def _cursor_done(self):
        # type: () -> bool
        cursor = self._cursor
        return cursor is not None and cursor["phase"] == 2 and not cursor["heap"]

    # -- 线路树（第二阶段）---------------------------------------------

    def _begin_lines(self, cursor):
        # type: (dict) -> None
        """分量 BFS 走完：校验基带核心、找出外露面、把每个根的第一个节点入队。

        这一步是 O(分量规模) 的一次扫描，不计步数（与收尾同一口径）；
        之后逐个展开节点的部分按步数预算分帧（`_step_lines`）。
        """
        adjacency = cursor["adjacency"]
        info = {}
        basebands = []
        controllers = []
        cables = []
        for key in cursor["members"]:
            record = self.registry.get(key)
            if record is None:
                continue
            definition = self.definitions.get(record.device_id)
            info[key] = (record, definition)
            if definition.category == CATEGORY_BASEBAND:
                basebands.append(key)
            elif definition.category == CATEGORY_CONTROLLER:
                controllers.append(key)
            elif definition.category == CATEGORY_CABLE:
                cables.append(key)

        parts_by_host = {}
        hosted = {}
        for key in sorted(cables, key=_as_key):
            parts = self.catalog.parts_of(info[key][0])
            if parts:
                parts_by_host[key] = parts
                for face, part_id in parts:
                    hosted[PartKey(key, face)] = part_id

        roots = []
        for baseband in sorted(basebands, key=_as_key):
            linked = set(adjacency.get(baseband, ()))
            definition = info[baseband][1]
            for index, neighbor in enumerate(baseband.neighbors()):
                if neighbor not in linked:
                    continue
                entry = info.get(neighbor)
                if entry is None or entry[1].category in _CORE_CATEGORIES:
                    continue
                face = FACING_NAMES[index]
                roots.append(
                    LineRoot(
                        _root_id(baseband, face),
                        baseband,
                        face,
                        neighbor,
                        definition.id,
                        _capacity_of(definition),
                    )
                )

        cursor.update(
            {
                "phase": 2,
                "info": info,
                "core": _validate_core(basebands, controllers, adjacency),
                "roots": roots,
                "parts_by_host": parts_by_host,
                "hosted": hosted,
                "passthrough": self.definitions.device_passthrough_quarters(),
                "heap": [],
                "nodes": {},
                "order": [],
                "seq": 0,
            }
        )
        # 第一层：各根的第一个节点按位置定序（13 §4.2「层内按 BFS 距离和 NodeKey」）；
        # 同一格贴着几个外露面时，归排在前面的那个根（基带位置、面序）。
        for root in sorted(
            roots,
            key=lambda item: (
                item.child.as_key(), item.baseband.as_key(), FACING_NAMES.index(item.face)
            ),
        ):
            if root.child not in cursor["nodes"]:
                self._discover(cursor, root.child, root.root_id, root.root_id, 1)

    def _discover(self, cursor, key, parent, root_id, depth):
        # type: (dict, NodeKey, object, str, int) -> None
        """第一次访问到一个节点：定下父节点与访问序号，按层入队。

        出队优先级：光缆先于设备；光缆之间容量大的先（32u → 16u → 8u）；
        同一层里离根近的先，再按位置。
        """
        definition = cursor["info"][key][1]
        if definition.category == CATEGORY_PROCESSOR:
            self._discover_processor_group(cursor, key, parent, root_id, depth)
            return
        if definition.category == CATEGORY_CABLE:
            kind = LineKind.CABLE
            capacity = _capacity_of(definition)
            rank = (0, 0 if capacity is UNCONFIGURED else -capacity)
        else:
            kind = LineKind.DEVICE
            capacity = cursor["passthrough"]
            rank = (1, 0)
        order = cursor["order"]
        cursor["nodes"][key] = LineNode(
            key, kind, definition.id, parent, root_id, depth, len(order), capacity
        )
        order.append(key)
        cursor["seq"] += 1
        heapq.heappush(
            cursor["heap"], (rank, depth, key.as_key(), cursor["seq"], key)
        )

    def _discover_processor_group(self, cursor, entry, parent, root_id, depth):
        # type: (dict, NodeKey, object, str, int) -> None
        """第一次访问到某组的一台处理器：把整组一次挂上去（同一条线路、连续的序号）。

        它就是这一组的代表；组里其余几台沿处理器之间的连接挂在它后面（按位置）。
        处理器不入队、不展开：处理器不透传（13 §3），线路不经处理器往外走；
        于是只经处理器才够得着的连接器不在任何线路上。整组一次挂完，也保证
        同一组不会被另一台连接器从别的线路再收走一部分。
        """
        nodes = cursor["nodes"]
        order = cursor["order"]
        info = cursor["info"]
        nodes[entry] = LineNode(
            entry, LineKind.PROCESSOR, info[entry][1].id, parent, root_id, depth,
            len(order), None, entry,
        )
        order.append(entry)
        queue = [entry]
        head = 0
        while head < len(queue):
            current = queue[head]
            head += 1
            for neighbor in sorted(cursor["adjacency"].get(current, ()), key=_as_key):
                if neighbor in nodes:
                    continue
                entry_info = info.get(neighbor)
                if entry_info is None or entry_info[1].category != CATEGORY_PROCESSOR:
                    continue
                nodes[neighbor] = LineNode(
                    neighbor, LineKind.PROCESSOR, entry_info[1].id, current, root_id,
                    nodes[current].depth + 1, len(order), None, entry,
                )
                order.append(neighbor)
                queue.append(neighbor)

    def _step_lines(self, cursor, steps):
        # type: (dict, int) -> int
        """按优先级逐个展开节点：先按面序挂它的面板，再按位置访问还没访问过的邻居。"""
        heap = cursor["heap"]
        nodes = cursor["nodes"]
        order = cursor["order"]
        used = 0
        while heap and used < steps:
            key = heapq.heappop(heap)[-1]
            used += 1
            self.stats["line_visits"] += 1
            node = nodes[key]
            for face, part_id in cursor["parts_by_host"].get(key, ()):
                part_key = PartKey(key, face)
                nodes[part_key] = LineNode(
                    part_key, LineKind.PART, part_id, key, node.root_id,
                    node.depth + 1, len(order), None,
                )
                order.append(part_key)
            for neighbor in sorted(cursor["adjacency"].get(key, ()), key=_as_key):
                if neighbor in nodes:
                    continue
                entry = cursor["info"].get(neighbor)
                if entry is None or entry[1].category in _CORE_CATEGORIES:
                    continue
                self._discover(cursor, neighbor, key, node.root_id, node.depth + 1)
        return used

    def _finish_cursor(self):
        # type: () -> object
        cursor = self._cursor
        members = list(cursor["members"])
        seed = cursor["seed"]
        self._cursor = None

        member_set = set(members)
        # 归属变更前先把「受影响的旧分量」找齐：合网时旧分量会整体并入，
        # 拆网时旧分量里没被走到的成员必须重新成图。
        stale = set()
        old_counts = {}
        for key in members:
            old_id = self._membership.get(key)
            if old_id is None:
                continue
            old_counts[old_id] = old_counts.get(old_id, 0) + 1
            stale |= self._members_by_id.get(old_id, set())

        inherited = None
        if old_counts:
            # 沿用覆盖成员最多的旧编号，避免「加一台设备就整网重挂」。
            inherited = sorted(
                old_counts.items(), key=lambda item: (-item[1], item[0])
            )[0][0]
            if len(old_counts) > 1:
                self._pass_merges.append(
                    (inherited, sorted(key for key in old_counts if key != inherited))
                )
                self.stats["merges"] += 1

        leftovers = sorted(
            (key for key in stale if key not in member_set),
            key=_as_key,
        )
        if leftovers:
            # 旧分量被拆开：这一半沿用旧编号，另一半重新分配。
            self._pass_splits.append((inherited, leftovers))
            self.stats["splits"] += 1
            for key in leftovers:
                self._forget_node(key)

        if inherited is None:
            inherited = self._allocate_component_id()

        self._assign(inherited, sorted(member_set, key=_as_key))

        # 编号回收必须在 `_assign` 之后：合并进来的成员此时才从旧桶里移走。
        for old_id in sorted(old_counts):
            if old_id != inherited and not self._members_by_id.get(old_id):
                self._retire(old_id)

        self._replace_structure(inherited, members, cursor)
        self._pending_components.discard(inherited)
        for key in member_set:
            self._pending_keys.discard(key)
        # 本次重建已经覆盖的旧种子无需再跑一遍；跨帧期间新到的种子不丢。
        self._seeds -= cursor["seeds_at_start"] & member_set
        # 跨帧期间到达的变更可能落在已经走过的部分，重新标脏再扫一遍。
        # 先把它从待重扫集合里摘掉，否则重扫出来的分量又会重扫自己。
        reseed = self._marked_during_pass & member_set
        self._marked_during_pass -= member_set
        for key in sorted(reseed, key=_as_key):
            self._seed(key)
        for key in leftovers:
            self._seed(key)

        self._touched |= member_set
        self._touched.add(seed)
        self.stats["components_rebuilt"] += 1
        self._needs_publish = True
        return inherited

    # -- 归属维护 ------------------------------------------------------

    def _allocate_component_id(self):
        # type: () -> int
        self._next_component_id += 1
        return self._next_component_id

    def _assign(self, component_id, member_keys):
        # type: (int, list) -> None
        bucket = self._members_by_id.setdefault(component_id, set())
        for key in member_keys:
            old_id = self._membership.get(key)
            if old_id is not None and old_id != component_id:
                old_bucket = self._members_by_id.get(old_id)
                if old_bucket is not None:
                    old_bucket.discard(key)
            self._membership[key] = component_id
            bucket.add(key)

    def _detach(self, key, component_id):
        # type: (NodeKey, int) -> None
        """把一个位置从分量里摘掉，桶空了就一并删除。"""
        bucket = self._members_by_id.get(component_id)
        if bucket is None:
            return
        bucket.discard(key)
        if not bucket:
            del self._members_by_id[component_id]

    def _forget_node(self, key):
        # type: (NodeKey) -> None
        """让一个位置不再属于任何分量，并保持「结果待确认」。"""
        old_id = self._membership.pop(key, None)
        if old_id is not None:
            self._detach(key, old_id)
        self._pending_keys.add(key)
        self._touched.add(key)
        self._needs_publish = True

    def _clear_membership(self, key):
        # type: (NodeKey) -> None
        old_id = self._membership.pop(key, None)
        if old_id is not None:
            self._detach(key, old_id)

    def _retire_indices(self, component_id):
        # type: (int) -> None
        """撤销一个分量的派生索引与「重建中」标记，但保留成员桶。"""
        self._pending_components.discard(component_id)
        self._trees.pop(component_id, None)
        self._cores.pop(component_id, None)
        for group_id in self._group_ids_by_component.pop(component_id, ()):
            self._groups.pop(group_id, None)

    def _retire(self, component_id):
        # type: (int) -> None
        self._members_by_id.pop(component_id, None)
        self._retire_indices(component_id)

    # -- 邻接 ----------------------------------------------------------

    def _link_verdict(self, record_a, face_a, record_b, face_b):
        # type: (object, str, object, str) -> tuple
        """两格之间这一对面能不能连：`(是否相连, 原因码, 说明)`。

        分类白名单与光缆按面的状态问 `catalog`，端口种类与方向问 `ports`。
        建图与诊断（`link_state` / `explain_link`）共用这一处，不会说两套话。
        """
        verdict = self.catalog.link_verdict(record_a, face_a, record_b, face_b)
        if not verdict.allowed:
            return False, verdict.reason, verdict.detail
        port_verdict = self.ports.link_verdict(
            self.definitions.category_of(record_a.device_id),
            self.definitions.category_of(record_b.device_id),
            face_a,
            face_b,
        )
        if not port_verdict.allowed:
            return (
                False,
                Reason.PORT_MISMATCH,
                "端口不匹配：%s（%s）" % (port_verdict.reason, port_verdict.detail),
            )
        return True, "", "%s <-> %s" % (face_a, face_b)

    def _probe_neighbors(self, node_key):
        # type: (NodeKey) -> tuple
        """逐个检查六个面的邻居，返回 (相连的, 被拒的)。

        相邻只是前提：分类、光缆按面的状态、端口种类与方向都通过才算一条边。
        这里不缓存结果——邻接只在重建与诊断时查询，重建本身就是有限步的，
        缓存带来的失效风险大于收益（一次重建内的复用由游标的邻接表负责）。
        """
        record = self.registry.get(node_key)
        if record is None:
            return [], []
        linked = []
        rejected = []
        for index, neighbor_key in enumerate(node_key.neighbors()):
            neighbor = self.registry.get(neighbor_key)
            if neighbor is None:
                continue
            face_here = FACING_NAMES[index]
            allowed, reason, detail = self._link_verdict(
                record, face_here, neighbor, opposite_face(face_here)
            )
            if not allowed:
                rejected.append((neighbor_key, neighbor.device_id, reason, detail))
                continue
            linked.append(neighbor_key)
        return linked, rejected

    def _linked_neighbors(self, node_key):
        # type: (NodeKey) -> list
        """建图用的邻接查询。诊断计数只在这里加——`link_state()` 之类的
        只读查询不应当让「端口拒绝数」这个指标变高，那不是图的工作量。"""
        linked, rejected = self._probe_neighbors(node_key)
        if rejected:
            self.stats["port_rejections"] += len(rejected)
        return linked

    # -- 结构推导（只在分量重建时执行）---------------------------------

    def _replace_structure(self, component_id, members, cursor):
        # type: (int, list, dict) -> None
        """把收尾时算好的线路树、核心结论与处理器群登记到这个分量上。"""
        self._last_finish_nodes = len(members)
        self.stats["structure_nodes"] += self._last_finish_nodes
        tree = LineTree(cursor["roots"], cursor["nodes"], cursor["order"], cursor["hosted"])
        groups = self._build_processor_groups(
            component_id, members, cursor["info"], cursor["adjacency"], tree
        )
        self._trees[component_id] = tree
        self._cores[component_id] = cursor["core"]
        for group_id in self._group_ids_by_component.pop(component_id, ()):
            self._groups.pop(group_id, None)
        if groups:
            self._group_ids_by_component[component_id] = tuple(
                group.group_id for group in groups
            )
            for group in groups:
                self._groups[group.group_id] = group

    def _build_processor_groups(self, component_id, members, info, adjacency, tree):
        # type: (int, list, dict, dict, LineTree) -> list
        """处理器群：处理器之间相连的块，经连接器接入网络。

        连接器只要贴着基带、光缆或设备之一（2026-11-09 起不限基带）就算接入；
        群与它的连接器总在同一个分量里，因此不再有「接到两张网」的情形
        （R03 已于 2026-11-09 关闭，见 `ProcessorOwnership`）。
        """
        processors = set(
            key for key in members
            if key in info and info[key][1].category == CATEGORY_PROCESSOR
        )
        connectors = set(
            key for key in members
            if key in info and info[key][1].category == CATEGORY_PROCESSOR_CONNECTOR
        )
        visited = set()
        groups = []
        for start in sorted(processors, key=_as_key):
            if start in visited:
                continue
            # 处理器—处理器 连通分量
            queue = [start]
            visited.add(start)
            block = [start]
            while queue:
                current = queue.pop(0)
                for neighbor_key in adjacency.get(current, ()):
                    if neighbor_key in processors and neighbor_key not in visited:
                        visited.add(neighbor_key)
                        queue.append(neighbor_key)
                        block.append(neighbor_key)
            block_set = set(block)

            touched_connectors = set()
            for processor in block:
                for neighbor_key in adjacency.get(processor, ()):
                    if neighbor_key in connectors:
                        touched_connectors.add(neighbor_key)

            attached = [
                connector
                for connector in sorted(touched_connectors, key=_as_key)
                if any(
                    neighbor_key not in processors
                    for neighbor_key in adjacency.get(connector, ())
                )
            ]
            line_ids = set()
            for connector in attached:
                line_id = tree.line_of(connector)
                if line_id is not None:
                    line_ids.add(line_id)

            reasons = []
            if attached:
                ownership = ProcessorOwnership.ATTACHED
            else:
                ownership = ProcessorOwnership.UNATTACHED
                reasons.append(
                    BlockReason(
                        Reason.PROCESSOR_GROUP_UNATTACHED,
                        "该处理器群没有经连接器接入网络，提供零槽位",
                    )
                )

            anchor = min(block_set, key=_as_key)
            groups.append(
                ProcessorGroup(
                    _group_id(anchor),
                    component_id,
                    block_set,
                    touched_connectors,
                    line_ids,
                    ownership,
                    reasons,
                )
            )
        return groups

    # -- 发布 ----------------------------------------------------------

    def _publish_if_needed(self, finished):
        # type: (list) -> TopologyPublication
        if not self._needs_publish and not finished:
            return None

        self.version += 1
        self._needs_publish = False
        touched = self._touched
        self._touched = set()
        finished_ids = [component_id for component_id in finished if component_id is not None]
        reasons = list(self._pass_reasons)
        self._pass_reasons = []

        migrations = self._diff_membership(touched)
        for new_id, merged_ids in self._pass_merges:
            reasons.append(
                BlockReason(
                    Reason.COMPONENT_MERGED,
                    "分量 %s 并入 %s" % (merged_ids, new_id),
                )
            )
        for old_id, leftovers in self._pass_splits:
            reasons.append(
                BlockReason(
                    Reason.COMPONENT_SPLIT,
                    "分量 %s 拆开，%d 个位置重新成图"
                    % (old_id, len(leftovers)),
                )
            )
        self._pass_merges = []
        self._pass_splits = []

        snapshot = TopologySnapshot(
            self.version,
            self.tick_counter,
            self._build_component_infos(),
            self._trees,
            self._groups,
            self._pending_keys,
        )
        self._snapshot = snapshot
        publication = TopologyPublication(
            self.version,
            self.tick_counter,
            snapshot,
            migrations,
            reasons,
            finished_ids,
        )
        self.stats["publications"] += 1
        self._emit(publication)
        return publication

    def _build_component_infos(self):
        # type: () -> dict
        infos = {}
        for component_id, bucket in self._members_by_id.items():
            if not bucket:
                continue
            members = sorted(bucket, key=_as_key)
            basebands = []
            controllers = []
            loaded = []
            for key in members:
                record = self.registry.get(key)
                if record is None:
                    continue
                if record.loaded:
                    loaded.append(key)
                category = self.definitions.category_of(record.device_id)
                if category == CATEGORY_BASEBAND:
                    basebands.append(key)
                elif category == CATEGORY_CONTROLLER:
                    controllers.append(record.ref)
            pending = component_id in self._pending_components
            infos[component_id] = ComponentInfo(
                component_id,
                self.version,
                members,
                basebands,
                controllers,
                loaded,
                pending,
                _controller_state(controllers, len(loaded), len(members), pending),
                core=self._cores.get(component_id),
            )
        return infos

    def _previous_of(self, key):
        # type: (NodeKey) -> tuple
        """上一份快照里该位置的归属。"""
        previous = self._snapshot
        component = previous.component_of(key)
        return (
            component.component_id if component is not None else None,
            previous.domain_ids_of(key),
        )

    def _diff_membership(self, touched):
        # type: (set) -> list
        migrations = []
        for key in sorted(touched, key=_as_key):
            record = self.registry.get(key)
            from_component, from_domains = self._previous_of(key)
            if record is None:
                if from_component is not None:
                    migrations.append(
                        MigrationNotice(
                            key, None, None, MigrationKind.DETACHED, from_component, None,
                            from_domains, (),
                        )
                    )
                continue
            to_component = self._membership.get(key)
            if to_component == from_component and to_component is not None:
                to_domains = self._domain_ids_for(key)
                if to_domains != tuple(from_domains):
                    migrations.append(
                        MigrationNotice(
                            key, record.device_id, record.ref, MigrationKind.DOMAIN,
                            from_component, to_component, from_domains, to_domains,
                        )
                    )
                continue
            if to_component is None:
                # 还没轮到重建：保持「结果待确认」，不产生迁移。
                continue
            migrations.append(
                MigrationNotice(
                    key, record.device_id, record.ref, MigrationKind.COMPONENT,
                    from_component, to_component, from_domains,
                    self._domain_ids_for(key),
                )
            )
        return migrations

    def _domain_ids_for(self, key):
        # type: (NodeKey) -> tuple
        """该位置当前所在的线路（与快照的 `domain_ids_of` 同一口径）。"""
        tree = self._trees.get(self._membership.get(key))
        if tree is None:
            return ()
        return tree.line_ids_of(key)

    # **没有 `_diff_slot_sources` 了。** 02-06 原文「通知 09 更新槽位来源」
    # 曾由它实现：把每个处理器群的槽位/归属前后值做成一份清单发出去。
    # 2026-09-28 随台账 D-03 定案删除——09 不消费这份清单，它每帧从
    # `snapshot().processor_groups` 重算容量（`crafting/orders/capacity.py`），
    # 那是**唯一**一份来源。保留一个没人接的推送，只会让下一个读者以为
    # 槽位变更必须被消费。

    def _emit(self, publication):
        # type: (TopologyPublication) -> None
        for listener in list(self._listeners):
            listener(publication)

    # -- 对外查询 ------------------------------------------------------

    def snapshot(self):
        # type: () -> TopologySnapshot
        """当前稳定快照。发布后不变。"""
        return self._snapshot

    def is_idle(self):
        # type: () -> bool
        """是否没有待重建的工作。"""
        return not self._seeds and self._cursor is None

    def validate(self, ref, expected_version=None):
        # type: (object, int) -> TopologyVerdict
        """这个设备实例现在能不能动业务（02-06/02-07）。

        只回答**拓扑条件**：设备还在、分量不处于重建中、主控恰好一台且贴着
        基带核心、基带核心有效、命令依据的版本没有过期。能源与带宽分别由 04、
        02-08 判定。

        面板引用（阶段 D）按宿主那一格判：宿主还在、代次对得上、那一面还挂着面板；
        分量与加载状态都是宿主的，线路编号是面板自己的。
        """
        snapshot = self._snapshot
        if expected_version is not None and expected_version != self.version:
            return TopologyVerdict(
                False, "stale", [BlockReason(Reason.STALE_VERSION, "图版本已过期")],
                self.version, None, (), ControllerState.UNKNOWN,
            )

        record, _device_id = self.catalog.resolve(self.registry, ref)
        if record is None:
            return TopologyVerdict(
                False, "stale", [BlockReason(Reason.DEVICE_GONE, "实例已不存在或代次过期")],
                self.version, None, (), ControllerState.UNKNOWN,
            )

        key = ref_key(ref)
        component = snapshot.component_of(ref.node_key)
        if component is None or snapshot.is_pending(ref.node_key):
            return TopologyVerdict(
                False, "blocked", [BlockReason(Reason.REBUILDING, "网络正在刷新，请稍候")],
                self.version, None, (), ControllerState.UNKNOWN,
            )
        if component.pending:
            return TopologyVerdict(
                False, "blocked", [BlockReason(Reason.REBUILDING, "网络正在刷新，请稍候")],
                self.version, component.component_id, snapshot.domain_ids_of(key),
                ControllerState.UNKNOWN,
            )
        if not component.is_runnable():
            return TopologyVerdict(
                False, "blocked", component.reason_list(), self.version,
                component.component_id, snapshot.domain_ids_of(key),
                component.controller_state,
            )
        if not record.loaded:
            return TopologyVerdict(
                False, "blocked", [BlockReason(Reason.UNLOADED_MEMBER, "设备所在区块未加载")],
                self.version, component.component_id, snapshot.domain_ids_of(key),
                component.controller_state,
            )
        return TopologyVerdict(
            True, "ok", [], self.version, component.component_id,
            snapshot.domain_ids_of(key), component.controller_state,
        )

    def bandwidth_domain_ids(self, ref):
        # type: (object) -> tuple
        """本设备（或面板）所在的线路编号。02-08 的界面字段。"""
        return self._snapshot.domain_ids_of(ref_key(ref))

    def link_state(self, node_key):
        # type: (NodeKey) -> LinkState
        """一台设备的连接状态：连上了谁、谁被拒、为什么。

        读的是**当前世界**而不是某份快照：这是诊断入口（02-07），
        要能回答「我刚放下的这台设备到底接上没有」。
        """
        record = self.registry.get(node_key)
        if record is None:
            return LinkState(
                node_key, None, None,
                reasons=[BlockReason(Reason.DEVICE_GONE, "该位置没有设备")],
            )
        linked, rejected = self._probe_neighbors(node_key)
        snapshot = self._snapshot
        component = snapshot.component_of(node_key)
        pending = snapshot.is_pending(node_key)
        state = (
            component.controller_state if component is not None else ControllerState.UNKNOWN
        )
        reasons = list(component.reason_list()) if component is not None else []
        if record.loaded is False:
            reasons.append(BlockReason(Reason.UNLOADED_MEMBER, "设备所在区块未加载"))
        return LinkState(
            node_key,
            record.device_id,
            component.component_id if component is not None else None,
            linked=linked,
            rejected=rejected,
            pending=pending,
            controller_state=state,
            runnable=bool(component is not None and component.is_runnable() and not pending),
            reasons=reasons,
        )

    def explain_link(self, node_key_a, node_key_b):
        # type: (NodeKey, NodeKey) -> dict
        """两个**相邻**位置之间为什么不连通。诊断用，不做图遍历。"""
        record_a = self.registry.get(node_key_a)
        record_b = self.registry.get(node_key_b)
        if record_a is None or record_b is None:
            return {
                "connected": False,
                "reason": Reason.DEVICE_GONE,
                "detail": "两端必须都有设备才能判定连接",
            }
        for index, neighbor_key in enumerate(node_key_a.neighbors()):
            if neighbor_key != node_key_b:
                continue
            face_a = FACING_NAMES[index]
            allowed, reason, detail = self._link_verdict(
                record_a, face_a, record_b, opposite_face(face_a)
            )
            return {"connected": allowed, "reason": reason, "detail": detail}
        return {
            "connected": False,
            "reason": "not_adjacent",
            "detail": "两个位置在六个面上都不相邻",
        }

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report["version"] = self.version
        report["pending_keys"] = len(self._pending_keys)
        report["pending_components"] = len(self._pending_components)
        report["seeded"] = len(self._seeds)
        report["cursor_active"] = self._cursor is not None
        report["snapshot"] = self._snapshot.summary()
        return report


def _controller_state(controllers, loaded_count, member_count, pending):
    # type: (list, int, int, bool) -> str
    """按实例数量判定分量状态。

    两个及以上一定是冲突（两个已确认的实例就足以判定）。
    恰好一个时是**候选**主控：分量里若还有未加载成员，仍可能藏着别的
    控制器，所以「候选」二字保留在 `master_candidate` 的语义里，
    但拓扑条件成立。
    零个时只有在边界确认后才敢报「确认没有」——这就是 02-03 的
    「未知/未恢复拓扑不视作确认无控制器」。
    """
    if pending:
        return ControllerState.UNKNOWN
    if len(controllers) >= 2:
        return ControllerState.CONFLICT
    if len(controllers) == 1:
        return ControllerState.SINGLE
    if loaded_count != member_count:
        return ControllerState.UNKNOWN
    return ControllerState.NONE


#: 三个轴各自的一对面（`FACING_NAMES` 的下标）：上下、南北、东西。
_AXES = ((0, 1), (2, 3), (4, 5))


def _validate_core(basebands, controllers, adjacency):
    # type: (list, list, dict) -> CoreVerdict
    """基带核心校验（13 §3，照 AE2 `ControllerValidator`）。

    1. 包围盒：每组基带在三个轴上的跨度都不超过 `CORE_MAX_SPAN`；
    2. 只有一组：基带之间面贴面连成一个结构（经光缆或设备相连的两组不算一组）；
    3. 没有十字：任何一块基带至多在一个轴上两侧都贴着基带。

    异档基带相邻即相连（R14-b，2026-11-09 作者改定允许混搭），因此这里只看
    「是不是基带」，不看档位。控制器不计入包围盒与十字（11 §1 第 5 条），
    只判它是否贴着某一块基带。最多 343 块，代价可以忽略。
    """
    baseband_set = set(basebands)
    attached = None
    if controllers:
        attached = all(
            any(neighbor in baseband_set for neighbor in adjacency.get(controller, ()))
            for controller in controllers
        )
    if not basebands:
        return CoreVerdict(BasebandState.NONE, controller_attached=attached)

    groups = []
    seen = set()
    for start in sorted(basebands, key=_as_key):
        if start in seen:
            continue
        seen.add(start)
        group = [start]
        head = 0
        while head < len(group):
            current = group[head]
            head += 1
            for neighbor in adjacency.get(current, ()):
                if neighbor in baseband_set and neighbor not in seen:
                    seen.add(neighbor)
                    group.append(neighbor)
        groups.append(group)

    span = [0, 0, 0]
    for group in groups:
        for axis, values in enumerate(
            ([key.x for key in group], [key.y for key in group], [key.z for key in group])
        ):
            span[axis] = max(span[axis], max(values) - min(values) + 1)

    crosses = []
    for baseband in sorted(basebands, key=_as_key):
        linked = set(adjacency.get(baseband, ()))
        neighbors = baseband.neighbors()
        flanked = 0
        for first, second in _AXES:
            if (
                neighbors[first] in baseband_set
                and neighbors[second] in baseband_set
                and neighbors[first] in linked
                and neighbors[second] in linked
            ):
                flanked += 1
        if flanked > 1:
            crosses.append(baseband)

    if max(span) > CORE_MAX_SPAN:
        state = BasebandState.TOO_LARGE
    elif len(groups) > 1:
        state = BasebandState.SPLIT
    elif crosses:
        state = BasebandState.CROSS
    else:
        state = BasebandState.VALID
    return CoreVerdict(state, len(groups), span, crosses, attached)


def _capacity_of(definition):
    # type: (object) -> object
    """载体容量（四分之一 u）。漏配时是 `UNCONFIGURED`，不当成 0。"""
    capacity = definition.bandwidth.capacity_quarters
    if capacity is None or capacity is UNCONFIGURED:
        return UNCONFIGURED
    return int(capacity)


def _root_id(baseband, face):
    # type: (NodeKey, str) -> str
    """线路编号由基带位置与外露面直接推导，因此确定且可复现。"""
    return "line:%d:%d,%d,%d:%s" % (
        baseband.dimension,
        baseband.x,
        baseband.y,
        baseband.z,
        face,
    )


def _group_id(anchor):
    # type: (NodeKey) -> str
    return "processor_group:%d:%d,%d,%d" % (
        anchor.dimension,
        anchor.x,
        anchor.y,
        anchor.z,
    )
