# coding=utf-8
"""带宽准入：外露面为根的线路、逐段核容量、设备获准与 `bandwidth_ok`。

对应任务 02-05 与 02-08，2026-11-09 按网络改版（`.claude/13` §4）重写，取代
R02 的「基带负载域」。拓扑（`topology.py`）给出**结构**——分量、基带核心、
线路树（每个外露面是一棵树的根，分层 BFS 定下每个节点的父节点与访问顺序）；
本模块给出**数值与资格**——每条线路上各段用了多少、每台设备（含面板）
获准没有。

规则（13 §4）：

1. **根**是基带的外露面，容量 = 该基带的档位（8/16/32u）。一块基带的几个
   外露面各是一条线路，不合成一个池。
2. **准入**：消费者（设备、面板）按建树时的访问顺序逐个处理，沿父链核对每一段：
   光缆用本档容量，设备用透传容量（R14-a，8u），根用档位。路径上**每一段**都满足
   「已用 + 需求 ≤ 容量」才准入并逐段累加；否则拒绝，原因「所在线路已满」，
   详情点名瓶颈是哪一段（如「末影玻璃光缆 8/8 u」）。
3. **重算时机**：拓扑发布时按线路比对结构指纹——结构没变的线路原样保留；
   只是叶子设备（含面板）增减、或设备需求变化（插拔介质）时，沿该设备的路径
   增量更新（几条能证明与整条重算结果一致的快路径，见 `_apply_demand_change` /
   `_apply_leaf_changes`）；其余整条线路重算，超出预算按 `step_budget` 分帧
   （`advance`）。分帧期间那条线路上的设备报「带宽计算中」。
4. R02 的 `CapacityMode` / `loop_mode` / `multi_access` / `rounding` / `priorities`
   随负载域一起退役；R13（恰好满载的显示口径）保留为具名替身。
5. **处理器组整组只算一次**（2026-11-09 作者定案，同 AE2 合成 CPU）：经连接器连成的
   一组处理器按一个消费者计带宽，需求取定义表里一台并行处理器的 4u，由组里最先被
   访问到的那台代表；其余几台不计，结论随代表；组内连接不受透传容量限制。
6. **P2P 隧道把关**（2026-11-13，`.claude/17` §4.3 的 T1）：线路经过隧道段
   （`topology.TunnelSegment`）的消费者，除了逐段核容量，还要这条隧道进出两端都「接通」——
   那一端正面对着它的隧道面板里，至少一块自己获准了带宽、而且宿主网络能运行（问
   `set_tunnels` 给的隧道服务 `endpoints_at`）。不满足就拒绝，原因 `TUNNEL_OFFLINE`，占用照算。
   隧道面板自己的线路经过隧道（嵌套）一律拒绝，原因 `TUNNEL_NESTED`；因此隧道面板的结论
   从不依赖别的隧道，刷新时先算它们、再算其余消费者，不会循环。隧道面板的「接通」变了，
   这一组隧道段所在线路上的设备随即重算。

三条纪律保留：

- **带宽与能源分别判定。** 本模块不读取任何能源状态，断电既不会把
  `bandwidth_ok` 变成 False，也不会抹掉已经生效的判定。
- **未知不等于 0。** 需求未配置（R10）的设备不计入、报待确认；路径上有一段
  容量未配置时整台报待确认。
- **读取不产生副作用。** `status_of()` 只查快照与已算好的线路。
"""

from ...common.definitions import (
    CATEGORY_BASEBAND,
    CATEGORY_CABLE,
    CATEGORY_CONTROLLER,
    CATEGORY_WIRELESS,
    PART_KIND_TUNNEL,
    UNCONFIGURED,
)
from ...common.identity import PartKey, is_part_ref
from ...common.protocol import BlockReason, DeviceStatusChanged, Reason
from .topology import LineKind, TunnelSegment


def _u_text(quarters):
    # type: (int) -> str
    """带宽计数（四分之一 u）→ 玩家看的 u 数：4 → 1，1 → 0.25，6 → 1.5。"""
    return ("%.2f" % (quarters / 4.0)).rstrip("0").rstrip(".")


def _sort_key(key):
    # type: (object) -> tuple
    return key.as_key()


def _is_part(key):
    # type: (object) -> bool
    """面板槽的键（center 槽的 `PartKey` 就是那一格本身，不算面板）。"""
    return isinstance(key, PartKey) and not key.is_center()


def _host_of(key):
    # type: (object) -> object
    return key.node_key if isinstance(key, PartKey) else key


# -- 设备在带宽上的角色 ----------------------------------------------------


class BandwidthRole(object):
    """设备在带宽体系里的角色。"""

    #: 载体：提供容量，自身不消耗（网络基带、末影光缆）。
    CARRIER = "carrier"
    #: 核心：网络本身，不参与准入（网络控制器）。
    CORE = "core"
    #: 消费者：需要获准才能工作。
    CONSUMER = "consumer"
    #: 定义缺项（R10）：blocks.md 没有给出它的带宽占用，
    #: 因此**不能**把它当成 0 计费，只能报「未配置」。
    UNKNOWN = "unknown"


#: 不参与准入的分类。显式列出，不从「有没有写带宽」反推——
#: 控制器与基带的 `base_quarters` 是 UNCONFIGURED（未列出），
#: 与「未列出所以是 0」是两回事，这里必须自己声明。无线连接器与光缆一样是载体。
NON_CONSUMER_ROLES = {
    CATEGORY_BASEBAND: BandwidthRole.CARRIER,
    CATEGORY_CABLE: BandwidthRole.CARRIER,
    CATEGORY_WIRELESS: BandwidthRole.CARRIER,
    CATEGORY_CONTROLLER: BandwidthRole.CORE,
}


def role_of(definitions, device_id):
    # type: (object, str) -> str
    """判定的唯一入口。"""
    definition = definitions.get(device_id)
    role = NON_CONSUMER_ROLES.get(definition.category)
    if role is not None:
        return role
    if definition.bandwidth.base_quarters is UNCONFIGURED:
        return BandwidthRole.UNKNOWN
    return BandwidthRole.CONSUMER


# -- 状态词汇 --------------------------------------------------------------


class BandwidthState(object):
    """一台设备的带宽判定状态。"""

    #: 已获额度。恰好满载时仍是它（R13 的建议口径）。
    GRANTED = "granted"
    #: 未获额度：所在线路某一段不够，或根本不在任何线路上。
    DENIED = "denied"
    #: 结果待确认：拓扑重建中、线路计算中、定义缺项。
    #: 界面此时显示「—/待确认」，它不是第三个业务检查项。
    UNKNOWN = "unknown"


class SaturationMode(object):
    """恰好满载时的显示口径（R13）。"""

    #: 已获准即为 OK，即使所在线路一点余量都没有。
    #: 这是 blocks.md 第 7 行的**建议**口径，尚未定稿。
    GRANTED_IS_OK = "granted_is_ok"
    #: 需要所在线路每一段都还有余量才算 OK。
    REQUIRE_HEADROOM = "require_headroom"


#: 策略字段到待定规则编号的映射。每一项都是配置点，不是已确认玩法。
#: 2026-11-09 起只剩 R13：R02 的五项随负载域退役（见模块说明第 4 条）。
UNRESOLVED_FIELDS = (
    ("saturation", "R13", "恰好满载时的带宽显示口径"),
)


class BandwidthPolicy(object):
    """准入的策略配置点。

    默认值全部是**开发测试替身**，`unresolved_report()` 会逐条列出。
    `assert_settled()` 供正式验收使用：规则没有定稿就不能宣布带宽数值已验收
    （对应 02 文档「这些规则未定时不能宣布带宽数值…已验收」）。

    2026-11-09 网络改版后只剩 R13 一项。容量由外露面档位与逐段核算决定，
    取整由作者定为「1/4u 精度、不取整」，谁先获准由线路树的访问顺序决定，
    这几件都不再是策略字段。
    """

    def __init__(self, saturation=SaturationMode.GRANTED_IS_OK):
        # type: (str) -> None
        if saturation not in (SaturationMode.GRANTED_IS_OK, SaturationMode.REQUIRE_HEADROOM):
            raise ValueError("未知的满载口径: %r" % (saturation,))
        self.saturation = saturation

    def saturation_ok(self, granted, used, capacity):
        # type: (bool, int, int) -> bool
        """已获准/已拒绝的设备，其 `bandwidth_ok` 应该是什么。

        `used` / `capacity` 是这台设备路径上**余量最小的那一段**的读数。
        """
        if not granted:
            return False
        if self.saturation == SaturationMode.REQUIRE_HEADROOM:
            return used < capacity
        return True

    # -- 守卫与诊断 ----------------------------------------------------

    def unresolved_report(self):
        # type: () -> list
        """仍是测试替身的策略字段。这是诊断，不是错误。"""
        return [
            {
                "field": field,
                "rule": rule,
                "value": getattr(self, field, None),
                "note": note,
            }
            for field, rule, note in UNRESOLVED_FIELDS
        ]

    def assert_settled(self):
        # type: () -> None
        """正式验收前的守卫：策略字段没有对应的规则决策就不算定案。

        调用它意味着「现在要宣布带宽数值可以验收了」。
        开发与离线集成测试**不应**调用它。
        """
        raise AssertionError(
            "带宽准入策略尚未定案，不能宣布数值已验收。待定项：\n  "
            + "\n  ".join(
                "%s（%s）：%s" % (field, rule, note)
                for field, rule, note in UNRESOLVED_FIELDS
            )
        )

    def describe(self):
        # type: () -> dict
        return {
            "saturation": self.saturation,
            "unresolved": self.unresolved_report(),
        }


# -- 结果对象 --------------------------------------------------------------


def _segment_text(segment_id):
    # type: (object) -> object
    """根的编号原样给出；节点给出位置（面板多一个面名）。"""
    as_key = getattr(segment_id, "as_key", None)
    return segment_id if as_key is None else list(as_key())


class Segment(object):
    """线路上的一段：根（外露面）、光缆或透传设备的一份读数。显示与诊断用。"""

    __slots__ = ("segment_id", "label", "used_quarters", "capacity_quarters")

    def __init__(self, segment_id, label, used_quarters, capacity_quarters):
        # type: (object, str, int, int) -> None
        self.segment_id = segment_id
        self.label = label
        self.used_quarters = used_quarters
        self.capacity_quarters = capacity_quarters

    def text(self):
        # type: () -> str
        """玩家话：「末影玻璃光缆 8/8 u」。"""
        return "%s %s/%s u" % (
            self.label,
            _u_text(self.used_quarters),
            _u_text(self.capacity_quarters),
        )

    def is_full(self):
        # type: () -> bool
        return self.used_quarters >= self.capacity_quarters

    def as_tuple(self):
        # type: () -> tuple
        return (self.label, self.used_quarters, self.capacity_quarters)

    def to_dict(self):
        # type: () -> dict
        return {
            "segment": _segment_text(self.segment_id),
            "label": self.label,
            "used_quarters": self.used_quarters,
            "capacity_quarters": self.capacity_quarters,
        }

    def __repr__(self):
        return "Segment(%s)" % (self.text(),)


#: 一台消费者在线路上的判定。
_GRANTED = "granted"
_DENIED = "denied"
#: 需求未配置（R10）：不计入，报待确认。
_UNCONFIGURED = "unconfigured"
#: 路径上有一段容量未配置：算不出，报待确认。
_UNKNOWN = "unknown"


class LineAdmission(object):
    """一条线路（外露面为根的子树）的准入结果。

    `used` 是**最终**读数（全部消费者处理完之后每一段的占用）；被拒设备的
    瓶颈段记的是**判定那一刻**的读数——玩家要看的是「我被谁挡住了」。
    """

    __slots__ = (
        "root_id",
        "version",
        "signature",
        "consumers",
        "position",
        "demand",
        "paths",
        "granted",
        "denied",
        "unconfigured",
        "unknown",
        "used",
        "capacity",
        "labels",
        "refs",
    )

    def __init__(self, root_id, signature):
        # type: (str, tuple) -> None
        self.root_id = root_id
        self.version = 0
        #: 算这份结果时线路的结构指纹（`LineTree.signature_of`）。
        self.signature = signature
        #: 消费者，按访问顺序。
        self.consumers = []
        #: 消费者 -> 在 `consumers` 里的下标。
        self.position = {}
        #: 消费者 -> 判定时的需求（四分之一 u 或 UNCONFIGURED）。
        self.demand = {}
        #: 消费者 -> 父链上的各段（不含它自己，最后一个是根）。
        self.paths = {}
        self.granted = set()
        #: 被拒的消费者 -> 瓶颈段（判定那一刻的读数）。
        self.denied = {}
        self.unconfigured = set()
        self.unknown = set()
        #: 段 -> 已用（最终读数）。
        self.used = {}
        #: 段 -> 容量。
        self.capacity = {}
        #: 段 -> 显示名（根取基带名，其余取设备名）。
        self.labels = {}
        #: 消费者 -> 判定时的实例（面板取宿主光缆的实例）。实例换了而线路还没
        #: 重算时，旧判定不能拿来回答新实例。
        self.refs = {}

    def decision_of(self, key):
        # type: (object) -> object
        if key in self.granted:
            return _GRANTED
        if key in self.denied:
            return _DENIED
        if key in self.unconfigured:
            return _UNCONFIGURED
        if key in self.unknown:
            return _UNKNOWN
        return None

    def segment(self, segment_id):
        # type: (object) -> Segment
        return Segment(
            segment_id,
            self.labels.get(segment_id, ""),
            self.used.get(segment_id, 0),
            self.capacity.get(segment_id, UNCONFIGURED),
        )

    def tightest(self, key):
        # type: (object) -> Segment
        """这台设备路径上余量最小的一段（同等余量取离设备近的）。"""
        best = None
        for segment_id in self.paths.get(key, ()):
            capacity = self.capacity.get(segment_id, UNCONFIGURED)
            if capacity is UNCONFIGURED:
                continue
            headroom = capacity - self.used.get(segment_id, 0)
            if best is None or headroom < best[0]:
                best = (headroom, segment_id)
        return self.segment(best[1]) if best is not None else None

    @property
    def used_quarters(self):
        # type: () -> int
        """根这一段的占用：这条线路一共放出去多少。"""
        return self.used.get(self.root_id, 0)

    @property
    def capacity_quarters(self):
        # type: () -> object
        return self.capacity.get(self.root_id, UNCONFIGURED)

    def to_dict(self):
        # type: () -> dict
        capacity = self.capacity_quarters
        return {
            "root_id": self.root_id,
            "version": self.version,
            "capacity_quarters": None if capacity is UNCONFIGURED else capacity,
            "used_quarters": self.used_quarters,
            "consumers": len(self.consumers),
            "granted": [list(key.as_key()) for key in sorted(self.granted, key=_sort_key)],
            "denied": [
                {"node_key": list(key.as_key()), "bottleneck": self.denied[key].to_dict()}
                for key in sorted(self.denied, key=_sort_key)
            ],
            "unconfigured": [
                list(key.as_key()) for key in sorted(self.unconfigured, key=_sort_key)
            ],
            "unknown": [list(key.as_key()) for key in sorted(self.unknown, key=_sort_key)],
        }

    def __repr__(self):
        return "LineAdmission(%s, %s/%s)" % (
            self.root_id,
            self.used_quarters,
            self.capacity_quarters,
        )


class AdmissionResult(object):
    """一台设备的带宽判定（02-08 的对外结构）。

    界面要的字段都在这里：设备代次、`bandwidth_domain_id`（所在线路）、需求量、
    分配量、路径上关键的那一段（被拒时是瓶颈段，获准时是余量最小的一段）、
    判定版本，以及「未获额度 / 已获额度 / 待确认」三态和可定位原因。
    面板的 `node_key` 是 `PartKey`，`ref` 是宿主光缆的实例。
    """

    __slots__ = (
        "node_key",
        "ref",
        "device_id",
        "role",
        "state",
        "ok",
        "demand_quarters",
        "allocated_quarters",
        "domain_ids",
        "segment",
        "version",
        "reasons",
        "notes",
    )

    def __init__(
        self,
        node_key,
        ref,
        device_id,
        role,
        state,
        ok,
        demand_quarters,
        allocated_quarters,
        domain_ids=(),
        segment=None,
        version=0,
        reasons=(),
        notes=(),
    ):
        self.node_key = node_key
        self.ref = ref
        self.device_id = device_id
        self.role = role
        self.state = state
        #: True / False / None（待确认）。
        self.ok = ok
        self.demand_quarters = demand_quarters
        self.allocated_quarters = allocated_quarters
        self.domain_ids = tuple(domain_ids)
        #: `Segment` 或 None。
        self.segment = segment
        self.version = version
        self.reasons = tuple(reasons)
        self.notes = tuple(notes)

    @property
    def bandwidth_domain_id(self):
        # type: () -> object
        """本设备所在的线路编号。不在线路上（或载体跨几条线路）时为 None。"""
        if len(self.domain_ids) == 1:
            return self.domain_ids[0]
        return None

    def reason_codes(self):
        # type: () -> tuple
        return tuple(
            reason.code if isinstance(reason, BlockReason) else str(reason)
            for reason in self.reasons
        )

    def is_pending(self):
        # type: () -> bool
        return self.state == BandwidthState.UNKNOWN

    def fingerprint(self):
        # type: () -> tuple
        """用于变化比较。不含版本号，避免「只是版本变了」被当成状态变化。"""
        return (
            self.state,
            self.ok,
            self.demand_quarters,
            self.allocated_quarters,
            self.domain_ids,
            self.segment.as_tuple() if self.segment is not None else None,
            self.reason_codes(),
        )

    def to_dict(self):
        # type: () -> dict
        """交给 11 的字段。`bandwidth_ok` 的 None 表示「待确认」。"""
        return {
            "node_key": self.node_key.to_dict(),
            "device_id": self.device_id,
            "generation": self.ref.generation if self.ref is not None else None,
            "role": self.role,
            "state": self.state,
            "bandwidth_ok": self.ok,
            "bandwidth_domain_id": self.bandwidth_domain_id,
            "domain_ids": list(self.domain_ids),
            "demand_quarters": (
                None if self.demand_quarters is UNCONFIGURED else self.demand_quarters
            ),
            "allocated_quarters": self.allocated_quarters,
            "segment": self.segment.to_dict() if self.segment is not None else None,
            "version": self.version,
            "reasons": [
                reason.to_dict() if isinstance(reason, BlockReason) else reason
                for reason in self.reasons
            ],
            "notes": list(self.notes),
        }

    def __repr__(self):
        return "AdmissionResult(%s, %s, ok=%s)" % (
            self.device_id,
            self.state,
            self.ok,
        )


class BandwidthChange(object):
    """一次带宽判定的变化，供只读订阅推送（11-13）。"""

    __slots__ = ("before", "after", "notification")

    def __init__(self, before, after):
        self.before = before
        self.after = after
        self.notification = DeviceStatusChanged(after)

    def to_dict(self):
        # type: () -> dict
        return {
            "before": self.before.to_dict() if self.before is not None else None,
            "after": self.after.to_dict() if self.after is not None else None,
        }

    def __repr__(self):
        return "BandwidthChange(%s -> %s)" % (
            self.before.state if self.before is not None else None,
            self.after.state if self.after is not None else None,
        )


class _LineJob(object):
    """一条线路的整条重算，可跨帧续跑。"""

    __slots__ = ("root_id", "tree", "state", "consumers", "index")

    def __init__(self, root_id, tree, state, consumers):
        self.root_id = root_id
        self.tree = tree
        self.state = state
        self.consumers = list(consumers)
        self.index = 0


# -- 服务 ------------------------------------------------------------------


class AdmissionService(object):
    """线路容量与设备准入。

    `topology` 是 `TopologyService`：本服务订阅它的发布，只重算结构变了的线路。
    `step_budget` 是一次推进最多核对多少段（整条线路重算的分帧单位）。
    """

    def __init__(self, definitions, registry, topology, policy=None, step_budget=4096):
        # type: (object, object, object, BandwidthPolicy, int) -> None
        self.definitions = definitions
        self.registry = registry
        self.topology = topology
        self.policy = policy if policy is not None else BandwidthPolicy()
        self.step_budget = int(step_budget)

        #: 判定版本。每次刷新加一。
        self.version = 0
        #: root_id -> LineAdmission（已算完的线路）
        self._lines = {}
        #: root_id -> _LineJob（排队或算到一半的整条重算）
        self._jobs = {}
        self._job_order = []
        #: 键（`NodeKey` / `PartKey`）-> AdmissionResult（只缓存消费者）
        self._results = {}
        #: 判定可能变了、下次刷新要重算结果的消费者。
        self._dirty = set()
        self._listeners = []
        #: 隧道服务（`set_tunnels`）：`endpoints_at(频率, 正面格)`、`endpoint(PartKey)`
        self.tunnels = None
        #: 隧道面板 PartKey -> (频率, 是否接通)：上一次刷新时的样子，变了就重算它那一组
        self._tunnel_live = {}
        self.stats = {
            "refreshes": 0,
            #: 结构没变、原样保留的线路次数。
            "lines_kept": 0,
            #: 整条重算完成的线路次数。
            "lines_recomputed": 0,
            #: 沿单台设备路径做的增量更新次数。
            "path_updates": 0,
            #: 整条重算核对过的段数（分帧预算的计量单位）。
            "segments_checked": 0,
            #: 因超出预算留到下一帧的推进次数。
            "deferred": 0,
            "changes_emitted": 0,
        }
        topology.add_listener(self._on_topology_published)
        # 订阅时拓扑可能已经有快照（服务装配顺序不固定），先补算一次，
        # 否则第一次查询会落到「未知」。
        snapshot = topology.snapshot()
        self._sync(snapshot, sorted(snapshot.line_trees))

    def set_tunnels(self, tunnels):
        # type: (object) -> None
        """接上 P2P 隧道服务（`server/devices/tunnels.TunnelService`）。没接时线路上不会有
        隧道段（拓扑没有隧道的远程连边），把关恒通过。"""
        self.tunnels = tunnels

    # -- 订阅拓扑 ------------------------------------------------------

    def _on_topology_published(self, publication):
        # type: (object) -> None
        """只处理被重建过的分量：它们的线路逐条比对结构指纹。

        没被重建的分量，线路树就是上一份，判定原样有效；进入「重建中」的
        分量由结果刷新时的扫描改报待确认。
        """
        self._sync(publication.snapshot, publication.rebuilt_components)

    def _sync(self, snapshot, component_ids):
        # type: (object, object) -> int
        touched = set()
        for component_id in sorted(component_ids):
            tree = snapshot.line_trees.get(component_id)
            if tree is None:
                continue
            for root_id in tree.root_ids():
                self._reconcile_line(tree, root_id)
                touched.update(tree.reported_of(root_id))
        self._prune(snapshot)
        self._run_jobs(self.step_budget)
        return self._refresh(snapshot, touched)

    def add_listener(self, listener):
        # type: (object) -> None
        self._listeners.append(listener)

    def remove_listener(self, listener):
        # type: (object) -> None
        if listener in self._listeners:
            self._listeners.remove(listener)

    # -- 设备负载变化 --------------------------------------------------

    def note_device_load_changed(self, node_key):
        # type: (object) -> int
        """插拔介质等改变设备需求的事件入口（05 调用）。

        只动这台设备所在的那一条线路；能证明结果不变的情形沿它自己的路径
        增量更新，否则整条线路重算。返回判定发生变化的设备数。
        """
        snapshot = self.topology.snapshot()
        tree = snapshot.line_tree_of(node_key)
        root_id = tree.line_of(node_key) if tree is not None else None
        if root_id is None:
            return 0
        state = self._lines.get(root_id)
        if state is None or root_id in self._jobs or not self._apply_demand_change(
            state, tree, node_key, self.demand_of(node_key)
        ):
            self._enqueue(tree, root_id)
            self._run_jobs(self.step_budget)
        return self._refresh(snapshot, set(tree.reported_of(root_id)))

    # -- 分帧推进 ------------------------------------------------------

    def advance(self, step_budget=None):
        # type: (int) -> int
        """继续上一帧没算完的线路。没有待算的线路时什么都不做。

        由运行时管理器每帧调用一次（`RuntimeManager.tick`）。返回核对的段数。
        """
        if not self._jobs:
            return 0
        used = self._run_jobs(self.step_budget if step_budget is None else step_budget)
        self._refresh(self.topology.snapshot(), set())
        return used

    def is_idle(self):
        # type: () -> bool
        """是否没有待算的线路。"""
        return not self._jobs

    # -- 线路比对 ------------------------------------------------------

    def _reconcile_line(self, tree, root_id):
        # type: (object, str) -> None
        """一条线路在新树里的处置：原样保留、沿路径增量更新，或整条重算。"""
        signature = tree.signature_of(root_id)
        job = self._jobs.get(root_id)
        if job is not None:
            if job.state.signature == signature:
                # 形状没变：已算的部分仍然有效，换上新树接着算。
                job.tree = tree
                return
            self._enqueue(tree, root_id)
            return
        state = self._lines.get(root_id)
        if state is not None:
            unchanged = state.signature == signature
            if unchanged or self._apply_leaf_changes(state, tree, root_id):
                if self._refresh_demands(state, tree):
                    if unchanged:
                        self.stats["lines_kept"] += 1
                    return
        self._enqueue(tree, root_id)

    def _refresh_demands(self, state, tree):
        # type: (LineAdmission, object) -> bool
        """结构没变时，逐台核对需求；变了的按增量规则改。改不了返回 False。"""
        for key in list(state.consumers):
            demand, ref = self._judged(key, tree)
            if state.refs.get(key) != ref:
                # 同型号原位顶替：判定只取决于先后、需求与路径，换实例本身不改判定。
                state.refs[key] = ref
                self._dirty.add(key)
            if not _same_value(demand, state.demand.get(key)):
                if not self._apply_demand_change(state, tree, key, demand):
                    return False
        return True

    def _apply_demand_change(self, state, tree, key, demand):
        # type: (LineAdmission, object, object, object) -> bool
        """一台设备的需求变了：能证明与整条重算结果一致时沿它的路径更新。

        四条快路径（按「先到先得」逐条推出来的，不是近似）：

        1. 它是这条线路上**最后**一台：只影响它自己，撤掉后按最终读数重判；
        2. 它已获准、需求**变小**，且它后面没有被拒的设备：后面的照样放得下，
           没有人等着补位，路径上逐段扣回差额即可；
        3. 它已获准、需求**变大**，且路径上每一段加上差额后都不超：它自己与后面
           已获准的设备照样放得下，被拒的照样被拒，逐段加上差额即可；
        4. 它被拒、需求不变小：照样被拒，瓶颈段不变。

        其余情形（被拒的需求变小、变大后放不下、未配置与已配置互换……）可能
        牵动后面的设备，返回 False 交给整条重算。
        """
        old = state.demand.get(key)
        if _same_value(demand, old):
            return True
        position = state.position.get(key)
        if position is None:
            return False
        path = state.paths.get(key, ())
        decision = state.decision_of(key)
        if position == len(state.consumers) - 1:
            ref = state.refs.get(key)
            _withdraw(state, key)
            _decide(state, key, demand, path, ref)
            return self._noted(key)
        known = demand is not UNCONFIGURED and old is not None and old is not UNCONFIGURED
        if decision == _GRANTED and known:
            delta = demand - old
            if delta < 0 and not _denied_after(state, position):
                for segment_id in path:
                    state.used[segment_id] += delta
                state.demand[key] = demand
                return self._noted(key)
            if delta > 0 and all(
                state.used.get(segment_id, 0) + delta <= state.capacity[segment_id]
                for segment_id in path
            ):
                for segment_id in path:
                    state.used[segment_id] = state.used.get(segment_id, 0) + delta
                state.demand[key] = demand
                return self._noted(key)
            return False
        if decision == _DENIED and known:
            if demand >= old:
                state.demand[key] = demand
                return self._noted(key)
        return False

    def _apply_leaf_changes(self, state, tree, root_id):
        # type: (LineAdmission, object, str) -> bool
        """只是叶子节点增减（放下或拆掉一台末端设备、挂上或拆下一块面板）。

        判据：去掉新增与删除的节点之后，新旧两份指纹逐项相同——其余节点的
        父节点、种类、容量与先后顺序都没变。在此之上：

        - 拆掉的消费者若已获准，它后面不能有被拒的设备（否则有人该补位）；
        - 新增的消费者必须都排在这条线路的最后（否则它会抢在已有设备前面）。

        满足就逐台沿路径扣回 / 判定，否则返回 False 交给整条重算。
        """
        old_signature = state.signature
        new_signature = tree.signature_of(root_id)
        if old_signature[0] != new_signature[0]:
            return False
        old_entries = old_signature[1:]
        new_entries = new_signature[1:]
        old_marks = set(entry[0] for entry in old_entries)
        new_marks = set(entry[0] for entry in new_entries)
        removed = old_marks - new_marks
        added = new_marks - old_marks
        if not removed and not added:
            return False
        if [entry for entry in old_entries if entry[0] not in removed] != [
            entry for entry in new_entries if entry[0] not in added
        ]:
            return False

        gone = [key for key in state.consumers if key.as_key() in removed]
        for key in gone:
            if key in state.granted and _denied_after(state, state.position[key]):
                return False
        consumers = tree.consumers_of(root_id)
        survivors = [key for key in consumers if key.as_key() not in added]
        newcomers = [key for key in consumers if key.as_key() in added]
        if consumers != survivors + newcomers:
            return False

        for key in gone:
            _withdraw(state, key)
            self._noted(key)
        for segment_id in list(state.capacity):
            if hasattr(segment_id, "as_key") and segment_id.as_key() in removed:
                state.capacity.pop(segment_id, None)
                state.used.pop(segment_id, None)
                state.labels.pop(segment_id, None)
        for key in tree.keys_of(root_id):
            if key.as_key() in added and tree.node(key).kind not in _UNBOUNDED_KINDS:
                state.capacity[key] = tree.capacity_of(key)
                state.labels[key] = self._label_of(tree.device_id_of(key))
        state.consumers = list(consumers)
        state.position = dict((key, index) for index, key in enumerate(consumers))
        for key in newcomers:
            demand, ref = self._judged(key, tree)
            _decide(state, key, demand, tree.path_of(key), ref)
            self._noted(key)
        state.signature = new_signature
        state.version = self.version + 1
        return True

    def _noted(self, key):
        # type: (object) -> bool
        self.stats["path_updates"] += 1
        self._dirty.add(key)
        return True

    def _enqueue(self, tree, root_id):
        # type: (object, str) -> None
        """把一条线路排进整条重算（已在排队的从头再来）。"""
        state = LineAdmission(root_id, tree.signature_of(root_id))
        state.capacity[root_id] = tree.capacity_of(root_id)
        state.labels[root_id] = self._label_of(tree.device_id_of(root_id))
        for key in tree.keys_of(root_id):
            node = tree.node(key)
            if node.kind in _UNBOUNDED_KINDS:
                continue
            state.capacity[key] = node.capacity_quarters
            state.labels[key] = self._label_of(node.device_id)
        consumers = tree.consumers_of(root_id)
        state.consumers = list(consumers)
        state.position = dict((key, index) for index, key in enumerate(consumers))
        if root_id not in self._jobs:
            self._job_order.append(root_id)
        self._jobs[root_id] = _LineJob(root_id, tree, state, consumers)

    def _run_jobs(self, budget):
        # type: (int) -> int
        """按排队顺序整条重算，核对的段数到预算就停在原处，下一帧接着算。"""
        used = 0
        while self._job_order and used < budget:
            root_id = self._job_order[0]
            job = self._jobs[root_id]
            while job.index < len(job.consumers) and used < budget:
                key = job.consumers[job.index]
                job.index += 1
                demand, ref = self._judged(key, job.tree)
                used += _decide(job.state, key, demand, job.tree.path_of(key), ref)
            if job.index < len(job.consumers):
                break
            self._job_order.pop(0)
            del self._jobs[root_id]
            previous = self._lines.get(root_id)
            if previous is not None:
                self._dirty.update(previous.consumers)
            job.state.version = self.version + 1
            self._lines[root_id] = job.state
            # 处理器组的其余几台不在消费者里，但结论随代表，一起刷新。
            self._dirty.update(job.tree.reported_of(root_id))
            self.stats["lines_recomputed"] += 1
        self.stats["segments_checked"] += used
        if self._jobs:
            self.stats["deferred"] += 1
        return used

    def _prune(self, snapshot):
        # type: (object) -> None
        """丢掉已经不存在的线路（根所在的基带被拆、外露面被盖住……）。

        **只在图稳定时清。** 跨帧重建期间，被拆开重扫的分量会暂时没有线路树，
        这时清掉的话，重建完成后每条线路都只能整条重算，增量更新就落空了；
        那段时间里这些设备本来就报「网络正在刷新」，留着旧结果不会被读到。
        """
        if snapshot.pending_keys or any(
            component.pending for component in snapshot.components.values()
        ):
            return
        live = set()
        for tree in snapshot.line_trees.values():
            live.update(tree.root_ids())
        for root_id in [root_id for root_id in self._lines if root_id not in live]:
            self._dirty.update(self._lines.pop(root_id).consumers)
        for root_id in [root_id for root_id in self._jobs if root_id not in live]:
            del self._jobs[root_id]
            self._job_order.remove(root_id)

    # -- 结果 ----------------------------------------------------------

    def _refresh(self, snapshot, touched):
        # type: (object, set) -> int
        """重算受影响设备的判定，发出变化。

        受影响 = 本次比对过的线路上的消费者 + 增量更新或重算碰到的 + 缓存里
        已经不在线路上、进入重建中或线路还在算的。其余缓存原样保留。
        """
        self.version += 1
        keys = set(touched) | self._dirty
        self._dirty = set()
        for key in self._results:
            if key in keys:
                continue
            root_id = snapshot.line_of(key)
            if (
                root_id is None
                or snapshot.is_pending(key)
                or root_id in self._jobs
                or self.registry.get(_host_of(key)) is None
            ):
                keys.add(key)
        changes = []
        ordered = sorted(keys, key=_sort_key)
        # 隧道面板先算：别的设备的隧道把关要读它们的结论（见模块说明第 6 条）
        tunnel_keys = [
            key for key in ordered
            if key in self._tunnel_live or self._is_tunnel_part(snapshot, key)
        ]
        first = set(tunnel_keys)
        for key in tunnel_keys:
            changes.extend(self._refresh_key(snapshot, key))
        rest = [key for key in ordered if key not in first]
        flipped = self._update_tunnel_live(snapshot, tunnel_keys)
        if flipped:
            extra = set()
            for tree in snapshot.line_trees.values():
                for via in flipped:
                    root_id = tree.tunnel_line(via)
                    if root_id is not None:
                        extra.update(tree.reported_of(root_id))
            rest = sorted(set(rest) | (extra - first), key=_sort_key)
        for key in rest:
            changes.extend(self._refresh_key(snapshot, key))
        self.stats["refreshes"] += 1
        if changes:
            self.stats["changes_emitted"] += len(changes)
            self._emit(changes)
        return len(changes)

    def _refresh_key(self, snapshot, key):
        # type: (object, object) -> list
        if not self._cacheable(snapshot, key):
            return self._drop(key)
        return self._store(key, self._device_result(snapshot, key))

    # -- P2P 隧道 --------------------------------------------------------

    def _is_tunnel_part(self, snapshot, key):
        # type: (object, object) -> bool
        if not _is_part(key):
            return False
        tree = snapshot.line_tree_of(key)
        part_id = tree.hosted.get(key) if tree is not None else None
        return part_id is not None and self.definitions.part_kind_of(part_id) == PART_KIND_TUNNEL

    def _update_tunnel_live(self, snapshot, keys):
        # type: (object, list) -> set
        """记下这些隧道面板现在的 `(频率, 是否接通)`，返回变了的频率（前后两边都算）。"""
        flipped = set()
        for key in keys:
            before = self._tunnel_live.get(key)
            after = None
            endpoint = self.tunnels.endpoint(key) if self.tunnels is not None else None
            if endpoint is not None and key in self._results:
                after = (endpoint[0], self._endpoint_live(snapshot, key))
            if after is None:
                self._tunnel_live.pop(key, None)
            else:
                self._tunnel_live[key] = after
            if before != after:
                for entry in (before, after):
                    if entry is not None:
                        flipped.add(entry[0])
        return flipped

    def _endpoint_live(self, snapshot, part_key):
        # type: (object, PartKey) -> bool
        return self._endpoint_problem(snapshot, part_key) is None

    def _endpoint_problem(self, snapshot, part_key):
        # type: (object, PartKey) -> object
        """这块隧道面板为什么没接通（短语，状态栏一行放得下，接在坐标后面）；接通了返回 None。

        接通 = 自己获准了带宽（隧道面板与别的设备一样，挂的那格光缆要经光缆连到网络基带，
        占 1u；同 AE2 每块隧道自己占 1 个频道），而且它所在的网络能运行。
        """
        result = self._results.get(part_key)
        if result is None:
            # 不在任何线路上的面板不缓存（`_cacheable`），按需现算（纯函数）
            result = self._device_result(snapshot, part_key)
        if result.state == BandwidthState.UNKNOWN:
            return "带宽确认中"
        if result.state != BandwidthState.GRANTED or result.ok is not True:
            codes = result.reason_codes()
            if Reason.NO_BANDWIDTH_PATH in codes:
                return "未接入网络"
            if Reason.TUNNEL_NESTED in codes:
                return "隧道嵌套"
            return "带宽不足"
        component = snapshot.component_of(part_key)
        if snapshot.is_pending(part_key) or component is None or component.pending:
            return "网络刷新中"
        if not component.is_runnable():
            return "所在网络离线"
        return None

    def _front_problem(self, snapshot, via, front):
        # type: (object, str, object) -> object
        """隧道段这一端（正面对着 `front` 的那几块隧道）接通没有：有一块接通就算通，返回 None；
        都不通返回 `(那块隧道, 原因)`，没有已加载的隧道时那块隧道是 None。"""
        if self.tunnels is None:
            return None
        first = None
        for part_key in self.tunnels.endpoints_at(via, front):
            problem = self._endpoint_problem(snapshot, part_key)
            if problem is None:
                return None
            if first is None:
                first = (part_key, problem)
        return first if first is not None else (None, "隧道未加载")

    def _tunnel_block(self, snapshot, key, device_id, path):
        # type: (object, object, str, tuple) -> object
        """线路经过隧道段时的把关：不通过返回原因（`BlockReason`），通过返回 None。

        `path` 是父链（不含自己，最后一个是根）。隧道段的父节点是进入隧道的那一端正面格，
        父链上排在它前面的那一项（没有就是自己）是出来的那一端正面格。
        """
        segments = [
            (index, segment_id) for index, segment_id in enumerate(path)
            if isinstance(segment_id, TunnelSegment)
        ]
        if not segments:
            return None
        if _is_part(key) and self.definitions.part_kind_of(device_id) == PART_KIND_TUNNEL:
            return BlockReason(
                Reason.TUNNEL_NESTED, "P2P 隧道不能嵌套（频率 #%s）" % (segments[0][1].via,))
        for index, segment_id in segments:
            exit_front = path[index - 1] if index > 0 else key
            entry_front = path[index + 1]
            for front in (entry_front, exit_front):
                found = self._front_problem(snapshot, segment_id.via, front)
                if found is None:
                    continue
                # 坐标给那块隧道挂的那一格（要去修的地方）；那一端没有隧道就给正面格
                part_key, problem = found
                at = part_key.node_key if part_key is not None else front
                return BlockReason(
                    Reason.TUNNEL_OFFLINE,
                    "P2P 隧道（频率 #%s）未接通：(%d, %d, %d) %s"
                    % (segment_id.via, at.x, at.y, at.z, problem),
                )
        return None

    def _cacheable(self, snapshot, key):
        # type: (object, object) -> bool
        """只缓存消费者，而且只缓存在线路上的（或正在重建、结论待定的）。

        载体、核心与不在任何线路上的设备按需现算（`status_of`），它们的结论不随
        别的设备变化。
        """
        record = self.registry.get(_host_of(key))
        if record is None:
            return False
        device_id = self._device_id_of(snapshot, key, record)
        if device_id is None:
            return False
        if role_of(self.definitions, device_id) in (BandwidthRole.CARRIER, BandwidthRole.CORE):
            return False
        return snapshot.line_of(key) is not None or snapshot.is_pending(key)

    def _device_id_of(self, snapshot, key, record):
        # type: (object, object, object) -> object
        """面板的型号来自宿主光缆上挂着的那一块；整方块设备取记录。"""
        if not _is_part(key):
            return record.device_id
        tree = snapshot.line_tree_of(key)
        if tree is None:
            return None
        return tree.hosted.get(key)

    def _device_result(self, snapshot, key):
        # type: (object, object) -> AdmissionResult
        """一台设备（或一块面板）的带宽判定。**纯函数**：只读快照、注册表与已算好的线路。"""
        record = self.registry.get(_host_of(key))
        if record is None:
            return self._unknown(
                key, None, BandwidthRole.UNKNOWN,
                [BlockReason(Reason.DEVICE_GONE, "设备已被拆除，请重新打开界面")],
            )
        device_id = self._device_id_of(snapshot, key, record)
        if device_id is None:
            if snapshot.is_pending(key):
                return self._unknown(
                    key, record, BandwidthRole.UNKNOWN,
                    [BlockReason(Reason.REBUILDING, "网络正在刷新，请稍候")],
                )
            return self._unknown(
                key, record, BandwidthRole.UNKNOWN,
                [BlockReason(Reason.DEVICE_GONE, "面板已被拆下，请重新打开界面")],
            )

        role = role_of(self.definitions, device_id)
        domain_ids = snapshot.domain_ids_of(key)
        if role in (BandwidthRole.CARRIER, BandwidthRole.CORE):
            return self._carrier_or_core_result(key, record, device_id, role, domain_ids)

        demand = self._demand_of_record(key, device_id, record)
        if snapshot.is_pending(key):
            return self._unknown(
                key, record, role,
                [BlockReason(Reason.REBUILDING, "网络正在刷新，请稍候")],
                device_id=device_id, domain_ids=domain_ids, demand=demand,
            )

        root_id = snapshot.line_of(key)
        if root_id is None:
            return AdmissionResult(
                key, record.ref, device_id, role,
                BandwidthState.DENIED, False, demand, 0,
                version=self.version,
                reasons=[
                    BlockReason(
                        Reason.NO_BANDWIDTH_PATH,
                        "该设备没有经光缆或其他设备连到网络基带",
                    )
                ],
            )

        tree = snapshot.line_tree_of(key)
        node = tree.node(key) if tree is not None else None
        if node is not None and node.is_group_member():
            return self._group_member_result(snapshot, key, record, device_id, node)
        group_size = tree.group_size(key) if tree is not None else 0
        group_notes = (
            ["处理器组共 %d 台，整组只算一次带宽（同 AE2 合成 CPU）" % (group_size,)]
            if group_size > 1
            else []
        )

        state = self._lines.get(root_id)
        decision = state.decision_of(key) if state is not None else None
        if root_id in self._jobs or decision is None or state.refs.get(key) != record.ref:
            return self._unknown(
                key, record, role,
                [BlockReason(Reason.BANDWIDTH_UNKNOWN, "带宽计算中，请稍候")],
                device_id=device_id, domain_ids=(root_id,), demand=demand,
            )

        if role == BandwidthRole.UNKNOWN or decision == _UNCONFIGURED:
            # R10：blocks.md 没给这个型号的带宽占用。未知不等于 0，
            # 因此不把它算进占用，也不假装它获准了。
            return AdmissionResult(
                key, record.ref, device_id, BandwidthRole.UNKNOWN,
                BandwidthState.UNKNOWN, None, UNCONFIGURED, 0,
                domain_ids=(root_id,),
                segment=state.tightest(key),
                version=self.version,
                reasons=[BlockReason(Reason.DEFINITION_UNCONFIGURED, "带宽状态未知")],
                notes=["该设备的占用未计入线路负载，线路占用是已知部分的下界"] + group_notes,
            )
        if decision == _UNKNOWN:
            return self._unknown(
                key, record, role,
                [BlockReason(Reason.BANDWIDTH_UNCONFIGURED, "带宽状态未知")],
                device_id=device_id, domain_ids=(root_id,), demand=demand,
            )

        if decision == _GRANTED:
            tight = state.tightest(key)
            granted_demand = state.demand.get(key, demand)
            notes = list(group_notes)
            blocked = self._tunnel_block(snapshot, key, device_id, state.paths.get(key, ()))
            if blocked is not None:
                # 线路上的占用照算（判定没有变），只是隧道没接通，这台设备不准入
                return AdmissionResult(
                    key, record.ref, device_id, BandwidthRole.CONSUMER,
                    BandwidthState.DENIED, False, granted_demand, 0,
                    domain_ids=(root_id,),
                    segment=tight,
                    version=self.version,
                    reasons=[blocked],
                    notes=notes,
                )
            ok = True
            if tight is not None:
                ok = self.policy.saturation_ok(True, tight.used_quarters, tight.capacity_quarters)
                if tight.is_full():
                    # 恰好满载：按 R13 的建议口径仍然是 OK，这里只做辅助说明。
                    notes.append("所在线路已满载（%s），本设备为已获准设备" % (tight.text(),))
            return AdmissionResult(
                key, record.ref, device_id, BandwidthRole.CONSUMER,
                BandwidthState.GRANTED, ok, granted_demand, granted_demand,
                domain_ids=(root_id,),
                segment=tight,
                version=self.version,
                notes=notes,
            )

        bottleneck = state.denied[key]
        denied_demand = state.demand.get(key, demand)
        if denied_demand > bottleneck.capacity_quarters:
            # 那一段还空着也放不下：不是「被别的设备挤掉」，而是单机需求超过了
            # 这一段的容量。两种情形都拒绝，但原因必须分开报。
            reasons = [
                BlockReason(
                    Reason.BANDWIDTH_DEMAND_EXCEEDS_LINE,
                    "这台设备需要 %s u，超过所在线路一段的容量：%s"
                    % (_u_text(denied_demand), bottleneck.text()),
                )
            ]
        else:
            detail = "所在线路已满：%s" % (bottleneck.text(),)
            if not bottleneck.is_full():
                detail += "，本设备需要 %s u" % (_u_text(denied_demand),)
            reasons = [BlockReason(Reason.BANDWIDTH_LINE_FULL, detail)]
        return AdmissionResult(
            key, record.ref, device_id, BandwidthRole.CONSUMER,
            BandwidthState.DENIED, False, denied_demand, 0,
            domain_ids=(root_id,),
            segment=bottleneck,
            version=self.version,
            reasons=reasons,
            notes=group_notes,
        )

    def _group_member_result(self, snapshot, key, record, device_id, node):
        # type: (object, object, object, str, object) -> AdmissionResult
        """处理器组里不是代表的那几台：自己不计带宽，结论随代表（整组只算一次）。"""
        lead = self._device_result(snapshot, node.group)
        return AdmissionResult(
            key, record.ref, device_id, lead.role,
            lead.state, lead.ok, 0, 0,
            domain_ids=lead.domain_ids,
            segment=lead.segment,
            version=self.version,
            reasons=lead.reasons,
            notes=[
                "处理器组整组只算一次带宽（同 AE2 合成 CPU），由 (%d, %d, %d) 那台代表计入"
                % (node.group.x, node.group.y, node.group.z)
            ],
        )

    def _carrier_or_core_result(self, key, record, device_id, role, domain_ids):
        # type: (object, object, str, str, tuple) -> AdmissionResult
        """载体与核心不需要带宽，结论恒为满足。"""
        note = (
            "%s是载体，不参与带宽准入" % (self._label_of(device_id),)
            if role == BandwidthRole.CARRIER
            else "控制器是网络核心，不占用带宽"
        )
        return AdmissionResult(
            key, record.ref, device_id, role,
            BandwidthState.GRANTED, True, 0, 0,
            domain_ids=domain_ids,
            version=self.version,
            notes=[note],
        )

    def _unknown(self, key, record, role, reasons, device_id=None, domain_ids=(), demand=0):
        # type: (...) -> AdmissionResult
        return AdmissionResult(
            key,
            record.ref if record is not None else None,
            device_id if device_id is not None else (
                record.device_id if record is not None and not _is_part(key) else None
            ),
            role,
            BandwidthState.UNKNOWN,
            None,
            demand,
            0,
            domain_ids=domain_ids,
            version=self.version,
            reasons=reasons,
        )

    def _store(self, key, result):
        # type: (object, AdmissionResult) -> list
        before = self._results.get(key)
        self._results[key] = result
        if before is not None and before.fingerprint() == result.fingerprint():
            return []
        return [BandwidthChange(before, result)]

    def _drop(self, key):
        # type: (object) -> list
        before = self._results.pop(key, None)
        if before is None:
            return []
        return [BandwidthChange(before, None)]

    def _emit(self, changes):
        # type: (list) -> None
        for listener in list(self._listeners):
            listener(changes)

    # -- 需求 ----------------------------------------------------------

    def _label_of(self, device_id):
        # type: (str) -> str
        if device_id is None or not self.definitions.has(device_id):
            return str(device_id)
        return self.definitions.get(device_id).label

    def _demand_of_record(self, key, device_id, record):
        # type: (object, str, object) -> object
        """需求（四分之一 u 或 UNCONFIGURED）。面板没有装置，只有固定需求。"""
        spec = self.definitions.get(device_id).bandwidth
        if _is_part(key):
            return spec.demand_quarters()
        disks = int(record.config.get("disk_count", 0) or 0)
        memories = int(record.config.get("memory_count", 0) or 0)
        return spec.demand_quarters(disks, memories)

    def _judged(self, key, tree):
        # type: (object, object) -> tuple
        """判定一台消费者要用的 `(需求, 实例)`：型号取建树时那一份。

        记录已经没了就按「需求未配置、没有实例」处理（重建马上会来）。
        """
        record = self.registry.get(_host_of(key))
        if record is None:
            return UNCONFIGURED, None
        return self._demand_of_record(key, tree.node(key).device_id, record), record.ref

    def demand_of(self, key):
        # type: (object) -> object
        """本设备（或面板）的带宽需求。返回四分之一 u 的整数，或 UNCONFIGURED。

        存储驱动器与内存槽的需求随所装介质变化，因此按**当前**记录读取，
        拔盘之后重算就会变小。介质数量放在 `DeviceRecord.config` 里，
        由任务 05 在插拔时写入。
        """
        record = self.registry.get(_host_of(key))
        if record is None:
            return UNCONFIGURED
        device_id = self._device_id_of(self.topology.snapshot(), key, record)
        if device_id is None:
            return UNCONFIGURED
        return self._demand_of_record(key, device_id, record)

    # -- 查询 ----------------------------------------------------------

    def status_of(self, ref):
        # type: (object) -> AdmissionResult
        """**只读**查询：本设备的带宽判定。断电、超载、无主控时同样可读。

        消费者设备的判定在每次发布与每次负载变化时算好并缓存，
        因此这里通常只是查一次表。缓存没有的分支（载体、核心、
        不在线路上、代次过期）按需现算，同样不改动任何状态。

        面板引用（阶段 D）转给 `status_of_part`：宿主的位置与代次对得上才查。
        """
        if is_part_ref(ref):
            if self.registry.get_by_ref(ref.host()) is None:
                return AdmissionResult(
                    ref.part_key, ref, None, BandwidthRole.UNKNOWN, BandwidthState.UNKNOWN,
                    None, UNCONFIGURED, 0, version=self.version,
                    reasons=[BlockReason(Reason.DEVICE_GONE, "实例已不存在或代次过期")],
                )
            return self.status_of_part(ref.part_key)
        node_key = ref.node_key
        record = self.registry.get_by_ref(ref)
        if record is None:
            return AdmissionResult(
                node_key, ref, None, BandwidthRole.UNKNOWN, BandwidthState.UNKNOWN,
                None, UNCONFIGURED, 0, version=self.version,
                reasons=[BlockReason(Reason.DEVICE_GONE, "实例已不存在或代次过期")],
            )
        cached = self._results.get(node_key)
        if cached is not None and cached.ref == ref and not _is_part(cached.node_key):
            return cached
        # 缓存是按**位置**存的，而位置可以被顶替：同一个坐标先放终端、
        # 再换成内存槽，缓存里那条判定属于已经不存在的那个实例。
        # 代次对不上就必须重算，否则会把旧设备的绿色判定连同错误的需求量
        # 一起发给调用方——而调用方正好是在 `tick()` 里消费设备变化的那些服务。
        return self._device_result(self.topology.snapshot(), node_key)

    def status_of_part(self, part_key):
        # type: (PartKey) -> AdmissionResult
        """**只读**查询：一块面板的带宽判定（13 §7）。会话、设备状态与命令准入经
        `status_of(面板引用)` 走到这里（阶段 D）。"""
        cached = self._results.get(part_key)
        record = self.registry.get(_host_of(part_key))
        if cached is not None and record is not None and cached.ref == record.ref:
            return cached
        return self._device_result(self.topology.snapshot(), part_key)

    def line(self, root_id):
        # type: (str) -> LineAdmission
        """一条已算完的线路；还在算或不存在时为 None。"""
        if root_id in self._jobs:
            return None
        return self._lines.get(root_id)

    def lines(self):
        # type: () -> dict
        return dict(
            (root_id, state)
            for root_id, state in self._lines.items()
            if root_id not in self._jobs
        )

    # -- 诊断 ----------------------------------------------------------

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report.update(
            {
                "version": self.version,
                "lines": dict(
                    (root_id, state.to_dict()) for root_id, state in self.lines().items()
                ),
                "pending_lines": list(self._job_order),
                "tracked_devices": len(self._results),
                "policy": self.policy.describe(),
                "energy_independent": True,
            }
        )
        return report


# -- 线路内的纯计算 --------------------------------------------------------

#: 不设容量的节点：面板（叶子，不让线路经过）与处理器（组内连接不受透传限制，
#: 线路也不经处理器往外走）。它们不会出现在任何一台消费者的计费路径上。
_UNBOUNDED_KINDS = frozenset([LineKind.PART, LineKind.PROCESSOR])


def _same_value(first, second):
    # type: (object, object) -> bool
    """需求比较：`UNCONFIGURED` 只等于它自己（它不能当布尔值用）。"""
    if first is UNCONFIGURED or second is UNCONFIGURED:
        return first is second
    return first == second


def _decide(state, key, demand, path, ref):
    # type: (LineAdmission, object, object, tuple, object) -> int
    """按「先到先得」判一台消费者，返回核对过的段数（分帧预算的计量单位）。"""
    state.demand[key] = demand
    state.paths[key] = path
    state.refs[key] = ref
    if demand is UNCONFIGURED:
        state.unconfigured.add(key)
        return 1
    for segment_id in path:
        if state.capacity.get(segment_id, UNCONFIGURED) is UNCONFIGURED:
            state.unknown.add(key)
            return max(1, len(path))
    for segment_id in path:
        if state.used.get(segment_id, 0) + demand > state.capacity[segment_id]:
            state.denied[key] = state.segment(segment_id)
            return max(1, len(path))
    for segment_id in path:
        state.used[segment_id] = state.used.get(segment_id, 0) + demand
    state.granted.add(key)
    return max(1, len(path))


def _withdraw(state, key):
    # type: (LineAdmission, object) -> None
    """撤掉一台消费者的判定；已获准的沿路径扣回。"""
    if key in state.granted:
        demand = state.demand[key]
        for segment_id in state.paths.get(key, ()):
            state.used[segment_id] = state.used.get(segment_id, 0) - demand
        state.granted.discard(key)
    state.denied.pop(key, None)
    state.unconfigured.discard(key)
    state.unknown.discard(key)
    state.demand.pop(key, None)
    state.paths.pop(key, None)
    state.refs.pop(key, None)


def _denied_after(state, position):
    # type: (LineAdmission, int) -> bool
    """这条线路上，排在 `position` 之后有没有被拒的设备。"""
    for key in state.denied:
        if state.position.get(key, -1) > position:
            return True
    return False
