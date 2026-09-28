# coding=utf-8
"""拓扑服务：连通分量、控制器计数、负载域结构与图版本发布。

对应任务 02-02 至 02-07。本服务**只给拓扑与资格**：它不读写库存、
不执行订单、不判定能源，也不分配带宽额度（那是 `admission.py`）。
它回答的是「谁和谁连在一起、这个分量可不可以工作、设备属于哪个负载域、
图版本是多少」。

四条纪律，逐条对应规划：

1. **相邻不等于连通。** 边只由 `DeviceCatalog`（分类白名单）与
   `node.PortRuleSet`（端口种类与方向）共同决定，见 `_linked_neighbors`。
2. **只重建受影响的部分。** 变化先标脏，调度阶段合并同帧脏点，
   用迭代 BFS 重建受影响分量，BFS 可跨帧续跑（`advance(step_budget)`）；
   没有脏点时 `advance` 一个节点都不访问（02 验收：无变化不逐块重搜）。
3. **重建期间撤销旧资格。** 标脏的那一刻，相关分量进入「重建中」，
   旧快照对它的资格立即失效；重建完成后先完成归属再发布新快照
   （02-06 的三段式：撤销 → 归属 → 发布）。
4. **未知不等于没有。** 分量里还有未加载的成员时，边界没有确认，
   零控制器只能报「未知」而不是「确认没有」——否则一个暂时不可访问的
   控制器会被当成已拆除，绕过「最多 1 个」的限制（02-03、架构规划 6.1）。

**版本号的语义。** `version` 是全局单调递增的图版本，每次「发布」加一。
发布的时刻只有两种：受到影响的资格被撤销（进入重建中），
或一次重建真正完成并完成归属。命令持有 `topology_version`，
版本对不上就必须重取快照（`validate()` 返回 stale）。

**编号是可重建的。** 分量编号在进程内分配，同一个分量在成员增减时
尽量沿用旧编号（从旧成员里选覆盖最多的那一个），使服务迁移不因
加了一台设备就全量重挂；进程重启后编号会重新分配，这与架构规划 6.5
「临时网络编号及汇总索引可以重建」一致。负载域与处理器群的编号
则由成员位置直接推导，因此是确定且可复现的。
"""

from ...common.definitions import CATEGORY_BASEBAND, CATEGORY_CONTROLLER
from ...common.identity import (  # noqa: F401 (NodeKey 仅用于 # type: 注释)
    FACING_NAMES,
    NodeKey,
)
from ...common.protocol import BlockReason, Reason, TopologyChanged
from ..runtime.lifecycle import ChangeKind
from .node import NetworkNode, PortRuleSet, opposite_face


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


class ProcessorOwnership(object):
    """处理器群到网络的归属。"""

    #: 群经连接器接入了某个负载域，正常贡献槽位。
    ATTACHED = "attached"
    #: 没有任何连接器，或连接器没有接到基带：不贡献槽位（已确定的规则）。
    UNATTACHED = "unattached"
    #: 群经多个连接器接到了**多个互不相连**的负载域，归属待定（R03）。
    #: 不擅自选一个域，因此也不贡献槽位。
    UNRESOLVED = "unresolved"


class MigrationKind(object):
    """一条归属迁移的种类，供 03 决定挂载/卸载。"""

    #: 换了连通分量（合网、拆网、或从无分量到有分量）。
    COMPONENT = "component"
    #: 分量没变，但负载域归属变了。
    DOMAIN = "domain"
    #: 该实例已经不在图里（被拆除或顶替）。
    DETACHED = "detached"


# -- 值对象 ----------------------------------------------------------------


class _SortedMixin(object):
    """统一按位置排序，保证遍历与发布顺序确定。"""

    @staticmethod
    def _sort_keys(keys):
        return sorted(keys, key=lambda key: key.as_key())


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

    @property
    def master_candidate(self):
        """唯一主控候选；不是恰好一个时返回 None。"""
        if self.controller_state != ControllerState.SINGLE:
            return None
        return self.controllers[0]

    def is_runnable(self):
        """分量是否可以运行业务。仅回答拓扑条件，不含能源与带宽。"""
        return (
            self.controller_state == ControllerState.SINGLE
            and not self.pending
        )

    def state_code(self):
        """给通知与诊断用的状态字符串。"""
        if self.pending:
            return Reason.REBUILDING
        if self.controller_state == ControllerState.CONFLICT:
            return Reason.CONTROLLER_CONFLICT
        if self.controller_state == ControllerState.NONE:
            return Reason.NO_CONTROLLER
        if self.controller_state == ControllerState.UNKNOWN:
            return Reason.TOPOLOGY_UNKNOWN
        return ""

    def reason_list(self):
        out = list(self.reasons)
        if self.pending:
            out.append(BlockReason(Reason.REBUILDING, "分量正在重建"))
        if self.controller_state == ControllerState.NONE:
            out.append(BlockReason(Reason.NO_CONTROLLER, "分量内没有控制器"))
        elif self.controller_state == ControllerState.CONFLICT:
            out.append(
                BlockReason(
                    Reason.CONTROLLER_CONFLICT,
                    "分量内有 %d 个控制器实例" % (len(self.controllers),),
                )
            )
        elif self.controller_state == ControllerState.UNKNOWN and self.partial:
            out.append(
                BlockReason(
                    Reason.TOPOLOGY_UNKNOWN,
                    "还有 %d 个成员未加载，控制器数量未确认"
                    % (len(self.members) - len(self.loaded_members),),
                )
            )
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
            "pending": self.pending,
            "partial": self.partial,
            "runnable": self.is_runnable(),
            "reasons": [reason.to_dict() for reason in self.reason_list()],
        }

    def __repr__(self):
        return "ComponentInfo(%s, %s, %d 个成员)" % (
            self.component_id,
            self.controller_state,
            len(self.members),
        )


class LoadDomain(_SortedMixin):
    """一个基带负载域：相连基带构成的整体。

    blocks.md：同一条相连基带共享负载；控制器分别连接到两条互不相连的
    基带时分别计算。因此域是**基带子图的连通分量**，而不是整个业务网络。
    容量与占用由 `admission.py` 计算，这里只给出结构。
    """

    __slots__ = ("domain_id", "component_id", "basebands", "device_keys", "controllers")

    def __init__(self, domain_id, component_id, basebands, device_keys, controllers):
        self.domain_id = domain_id
        self.component_id = component_id
        self.basebands = frozenset(basebands)
        #: 挂在域上的一切设备（含基带自身）。
        self.device_keys = frozenset(device_keys)
        self.controllers = tuple(controllers)

    def to_dict(self):
        # type: () -> dict
        return {
            "domain_id": self.domain_id,
            "component_id": self.component_id,
            "basebands": [key.as_key() for key in self._sort_keys(self.basebands)],
            "devices": len(self.device_keys),
        }

    def __repr__(self):
        return "LoadDomain(%s, %d 段基带, %d 台设备)" % (
            self.domain_id,
            len(self.basebands),
            len(self.device_keys),
        )


class ProcessorGroup(_SortedMixin):
    """一个处理器群：由相互连接的并行处理器构成，经连接器接入网络。

    blocks.md：单个处理器提供 1 个配方处理槽位；未通过连接器接入网络的
    处理器不提供槽位。同一处理器可经多条路径接入同一个域，**只计一次**。
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
        （`crafting/capacity.py` 的 `slot_capacity`），那里读的是
        `contributes_slots()` 与 `slots`，且**另带一层跨群去重**：02 建群
        时按 `visited` 去过重，工厂那一层不去信这个保证（万一去重被改坏，
        症状是槽位凭空变多，不设防的话几乎查不出来）。

        2026-09-28（台账 D-03）：这里曾经还有一个 `slot_count()`——本方法的
        `slots` 简写，唯一调用方是 02 的槽位来源清单，而那份清单没有消费者。
        清单连同它一并删除，理由见 `crafting/capacity.py` 模块 docstring。
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
    具体原因（分类不允许还是端口/方向不允许），而不是简单地不出现。
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


class TopologySnapshot(object):
    """一份稳定的图快照。

    发布后不再变化：再次重建会生成**新的**快照对象，
    旧快照继续可读（但它的版本号已经过期，`validate()` 会拒绝基于它的新转移）。
    """

    __slots__ = (
        "version",
        "tick",
        "components",
        "domains",
        "processor_groups",
        "pending_keys",
        "_by_node",
        "_domains_by_node",
        "_group_by_node",
    )

    def __init__(self, version, tick, components, domains, processor_groups, pending_keys):
        self.version = version
        self.tick = tick
        self.components = dict(components)
        self.domains = dict(domains)
        self.processor_groups = dict(processor_groups)
        #: 尚未归属到任何分量的位置（新设备、刚被拆掉的位置）。
        self.pending_keys = frozenset(pending_keys)
        self._by_node = None
        self._domains_by_node = None
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

        domains_by_node = {}
        for domain in self.domains.values():
            for node_key in domain.device_keys:
                domains_by_node.setdefault(node_key, []).append(domain.domain_id)
        self._domains_by_node = dict(
            (key, tuple(sorted(value))) for key, value in domains_by_node.items()
        )

        group_by_node = {}
        for group in self.processor_groups.values():
            for node_key in group.processors:
                group_by_node[node_key] = group.group_id
        self._group_by_node = group_by_node

    # -- 查询 ----------------------------------------------------------

    def component_id_of(self, node_key):
        # type: (NodeKey) -> object
        self._index()
        return self._by_node.get(node_key)

    def component_of(self, node_key):
        # type: (NodeKey) -> ComponentInfo
        return self.components.get(self.component_id_of(node_key))

    def domain_ids_of(self, node_key):
        # type: (NodeKey) -> tuple
        """该设备所属的负载域。多接入待定（R02）时会有多个。"""
        self._index()
        return self._domains_by_node.get(node_key, ())

    def domains_of(self, node_key):
        # type: (NodeKey) -> tuple
        return tuple(
            self.domains[domain_id]
            for domain_id in self.domain_ids_of(node_key)
            if domain_id in self.domains
        )

    def processor_group_of(self, node_key):
        # type: (NodeKey) -> ProcessorGroup
        self._index()
        group_id = self._group_by_node.get(node_key)
        return self.processor_groups.get(group_id) if group_id is not None else None

    def is_pending(self, node_key):
        # type: (NodeKey) -> bool
        """该位置是否处于「结果待确认」：分量在重建，或自己还没有归属。"""
        component = self.component_of(node_key)
        if component is None:
            return True
        return component.pending or node_key in self.pending_keys

    def controller_state_of(self, node_key):
        # type: (NodeKey) -> str
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
        for component in self.components.values():
            states[component.controller_state] = (
                states.get(component.controller_state, 0) + 1
            )
        return {
            "version": self.version,
            "components": len(self.components),
            "domains": len(self.domains),
            "processor_groups": len(self.processor_groups),
            "pending": len(self.pending_keys),
            "controller_states": states,
        }

    def to_dict(self):
        # type: () -> dict
        report = self.summary()
        report["component_list"] = [
            self.components[key].to_dict() for key in sorted(self.components)
        ]
        report["domain_list"] = [
            self.domains[key].to_dict() for key in sorted(self.domains)
        ]
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
    （`crafting/capacity.py`），推一份容量增量的清单只会是第二份来源。
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
        #: 受预算约束的节点展开次数
        self.steps = steps
        self.components_rebuilt = components_rebuilt
        self.seed_pops = seed_pops
        self.published = published
        self.queued = queued
        #: 本帧在**归属阶段**额外扫描的成员数（推导负载域与处理器群）。
        #: 它**不**受 `step_budget` 约束，见 `TopologyService.finish_cost` 的说明。
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
        self._domains = {}
        self._domain_ids_by_component = {}
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
            "components_rebuilt": 0,
            "publications": 0,
            "splits": 0,
            "merges": 0,
            "port_rejections": 0,
            "empty_passes": 0,
            #: 归属阶段扫描的成员总数。**不受步数预算约束**：
            #: 一个分量一旦走完 BFS，推导它的负载域与处理器群就在同一帧完成。
            #: 这一段目前是 O(分量规模)，大网络上需要 04 的预算重新评估。
            "structure_nodes": 0,
        }

    # -- 标脏 ----------------------------------------------------------

    def mark_dirty(self, node_key):
        # type: (NodeKey) -> None
        """标记一个位置需要重建（02-02 的入口）。

        标脏即刻生效：相关分量立刻进入「重建中」，旧快照对它的资格
        在**下一次发布**时被撤销。这就是 02-06 的「撤销旧资格」阶段。
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

        「尝试破坏」不改图（方块还在），因此不标脏；其余变化都只标脏，
        真正的重建留到推进阶段，保证同帧多次变化只算一次。
        """
        if change.kind == ChangeKind.PLACEMENT_REVIEW:
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

        `step_budget` 是**节点展开次数**预算；一个分量可以跨多帧重建，
        游标保存在服务里。
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
            if self._cursor["head"] >= len(self._cursor["queue"]):
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
        seed = min(self._seeds, key=lambda key: key.as_key())
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
            for key in sorted(leftovers, key=lambda k: k.as_key()):
                self._seed(key)
            self._needs_publish = True
            return False

        self._cursor = {
            "seed": seed,
            "queue": [seed],
            "head": 0,
            "visited": set([seed]),
            "members": [seed],
            #: 游标启动前就在队列里的种子。跨帧期间新标脏的种子不在此列，
            #: 因此不会被「本次重建已经覆盖」误判而丢弃。
            "seeds_at_start": set(self._seeds),
        }
        return True

    def _step_cursor(self, steps):
        # type: (int) -> int
        cursor = self._cursor
        queue = cursor["queue"]
        head = cursor["head"]
        visited = cursor["visited"]
        members = cursor["members"]
        used = 0
        while head < len(queue) and used < steps:
            key = queue[head]
            head += 1
            used += 1
            self.stats["node_visits"] += 1
            for neighbor_key in self._linked_neighbors(key):
                if neighbor_key in visited:
                    continue
                visited.add(neighbor_key)
                queue.append(neighbor_key)
                members.append(neighbor_key)
        cursor["head"] = head
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
            key=lambda key: key.as_key(),
        )
        if leftovers:
            # 旧分量被拆开：这一半沿用旧编号，另一半重新分配。
            self._pass_splits.append((inherited, leftovers))
            self.stats["splits"] += 1
            for key in leftovers:
                self._forget_node(key)

        if inherited is None:
            inherited = self._allocate_component_id()

        self._assign(inherited, sorted(member_set, key=lambda key: key.as_key()))

        # 编号回收必须在 `_assign` 之后：合并进来的成员此时才从旧桶里移走。
        for old_id in sorted(old_counts):
            if old_id != inherited and not self._members_by_id.get(old_id):
                self._retire(old_id)

        self._replace_structure(inherited, member_set)
        self._pending_components.discard(inherited)
        for key in member_set:
            self._pending_keys.discard(key)
        # 本次重建已经覆盖的旧种子无需再跑一遍；跨帧期间新到的种子不丢。
        self._seeds -= cursor["seeds_at_start"] & member_set
        # 跨帧期间到达的变更可能落在已经走过的部分，重新标脏再扫一遍。
        # 先把它从待重扫集合里摘掉，否则重扫出来的分量又会重扫自己。
        reseed = self._marked_during_pass & member_set
        self._marked_during_pass -= member_set
        for key in sorted(reseed, key=lambda item: item.as_key()):
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
        for domain_id in self._domain_ids_by_component.pop(component_id, ()):
            self._domains.pop(domain_id, None)
        for group_id in self._group_ids_by_component.pop(component_id, ()):
            self._groups.pop(group_id, None)

    def _retire(self, component_id):
        # type: (int) -> None
        self._members_by_id.pop(component_id, None)
        self._retire_indices(component_id)

    # -- 邻接 ----------------------------------------------------------

    def _probe_neighbors(self, node_key):
        # type: (NodeKey) -> tuple
        """按**三张表**逐个检查六个面的邻居，返回 (相连的, 被拒的)。

        相邻只是前提：分类、端口种类、方向三道判定都通过才算一条边。
        这里不缓存结果——邻接只在重建与诊断时查询，重建本身就是有限步的，
        缓存带来的失效风险大于收益。
        """
        record = self.registry.get(node_key)
        if record is None:
            return [], []
        category = self.definitions.category_of(record.device_id)
        linked = []
        rejected = []
        for index, neighbor_key in enumerate(node_key.neighbors()):
            neighbor = self.registry.get(neighbor_key)
            if neighbor is None:
                continue
            face_here = FACING_NAMES[index]
            face_there = opposite_face(face_here)

            verdict = self.catalog.can_connect(record.device_id, neighbor.device_id)
            if not verdict.allowed:
                rejected.append(
                    (neighbor_key, neighbor.device_id, verdict.reason, verdict.detail)
                )
                continue

            port_verdict = self.ports.link_verdict(
                category,
                self.definitions.category_of(neighbor.device_id),
                face_here,
                face_there,
            )
            if not port_verdict.allowed:
                rejected.append(
                    (
                        neighbor_key,
                        neighbor.device_id,
                        Reason.PORT_MISMATCH,
                        "端口不匹配：%s（%s）" % (port_verdict.reason, port_verdict.detail),
                    )
                )
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

    def _node_of(self, record):
        # type: (object) -> NetworkNode
        return NetworkNode.from_record(self.definitions, record, self.ports)

    # -- 结构推导（只在分量重建时执行）---------------------------------

    def _replace_structure(self, component_id, member_keys):
        # type: (int, set) -> None
        self._last_finish_nodes = len(member_keys)
        self.stats["structure_nodes"] += self._last_finish_nodes
        domains, groups = self._compute_structure(component_id, member_keys)
        for domain_id in self._domain_ids_by_component.pop(component_id, ()):
            self._domains.pop(domain_id, None)
        for group_id in self._group_ids_by_component.pop(component_id, ()):
            self._groups.pop(group_id, None)
        if domains:
            self._domain_ids_by_component[component_id] = tuple(
                domain.domain_id for domain in domains
            )
            for domain in domains:
                self._domains[domain.domain_id] = domain
        if groups:
            self._group_ids_by_component[component_id] = tuple(
                group.group_id for group in groups
            )
            for group in groups:
                self._groups[group.group_id] = group

    def _compute_structure(self, component_id, member_keys):
        # type: (int, set) -> tuple
        """在一个分量内部推导基带负载域与处理器群。

        顺序是有意的：先定域（基带子图），再把处理器群接到域上。
        处理器**不直接连基带**，它的归属只能经连接器推出。
        """
        members = sorted(member_keys, key=lambda key: key.as_key())
        nodes = {}
        for key in members:
            record = self.registry.get(key)
            if record is not None:
                nodes[key] = self._node_of(record)

        basebands = set(key for key in nodes if nodes[key].is_baseband())
        domains = self._build_domains(component_id, members, nodes, basebands)
        group_of_baseband = {}
        for domain in domains:
            for key in domain.basebands:
                group_of_baseband[key] = domain.domain_id

        groups = self._build_processor_groups(
            component_id, members, nodes, group_of_baseband
        )
        # 处理器群定下来之后，把处理器并进它所属的域。
        processors_by_domain = {}
        for group in groups:
            if group.ownership != ProcessorOwnership.ATTACHED:
                continue
            for domain_id in group.domain_ids:
                processors_by_domain.setdefault(domain_id, set()).update(group.processors)

        if processors_by_domain:
            domains = self._attach_processors(domains, processors_by_domain)
        return domains, groups

    def _build_domains(self, component_id, members, nodes, basebands):
        # type: (int, list, dict, set) -> list
        """基带子图的连通分量 = 负载域。"""
        visited = set()
        domains = []
        for start in sorted(basebands, key=lambda key: key.as_key()):
            if start in visited:
                continue
            queue = [start]
            visited.add(start)
            block = [start]
            while queue:
                current = queue.pop(0)
                for neighbor_key in self._linked_neighbors(current):
                    if neighbor_key in basebands and neighbor_key not in visited:
                        visited.add(neighbor_key)
                        queue.append(neighbor_key)
                        block.append(neighbor_key)

            # 挂在这段基带上的设备：与任一基带相连的非基带节点。
            block_set = set(block)
            attached = set()
            for baseband in block:
                for neighbor_key in self._linked_neighbors(baseband):
                    if neighbor_key not in basebands and neighbor_key in nodes:
                        attached.add(neighbor_key)
            device_keys = block_set | attached
            controllers = tuple(
                nodes[key].ref
                for key in sorted(device_keys, key=lambda key: key.as_key())
                if key in nodes and nodes[key].is_controller()
            )
            anchor = min(block, key=lambda key: key.as_key())
            domains.append(
                LoadDomain(
                    _domain_id(anchor),
                    component_id,
                    block_set,
                    device_keys,
                    controllers,
                )
            )
        return domains

    def _attach_processors(self, domains, processors_by_domain):
        # type: (list, dict) -> list
        out = []
        for domain in domains:
            extra = processors_by_domain.get(domain.domain_id)
            if not extra:
                out.append(domain)
                continue
            out.append(
                LoadDomain(
                    domain.domain_id,
                    domain.component_id,
                    domain.basebands,
                    domain.device_keys | extra,
                    domain.controllers,
                )
            )
        return out

    def _build_processor_groups(self, component_id, members, nodes, group_of_baseband):
        # type: (int, list, dict, dict) -> list
        processors = set(key for key in nodes if nodes[key].is_processor())
        connectors = set(key for key in nodes if nodes[key].is_connector())
        visited = set()
        groups = []
        for start in sorted(processors, key=lambda key: key.as_key()):
            if start in visited:
                continue
            # 处理器—处理器 连通分量
            queue = [start]
            visited.add(start)
            block = [start]
            while queue:
                current = queue.pop(0)
                for neighbor_key in self._linked_neighbors(current):
                    if neighbor_key in processors and neighbor_key not in visited:
                        visited.add(neighbor_key)
                        queue.append(neighbor_key)
                        block.append(neighbor_key)
            block_set = set(block)

            touched_connectors = set()
            for processor in block:
                for neighbor_key in self._linked_neighbors(processor):
                    if neighbor_key in connectors:
                        touched_connectors.add(neighbor_key)

            domain_ids = set()
            for connector in touched_connectors:
                for neighbor_key in self._linked_neighbors(connector):
                    domain_id = group_of_baseband.get(neighbor_key)
                    if domain_id is not None:
                        domain_ids.add(domain_id)

            reasons = []
            if not touched_connectors or not domain_ids:
                ownership = ProcessorOwnership.UNATTACHED
                reasons.append(
                    BlockReason(
                        Reason.PROCESSOR_GROUP_UNATTACHED,
                        "该处理器群没有经连接器接入基带，提供零槽位",
                    )
                )
            elif len(domain_ids) > 1:
                # R03：多连接器连到不同网络时的归属未定，不擅自选一个。
                ownership = ProcessorOwnership.UNRESOLVED
                reasons.append(
                    BlockReason(
                        Reason.PROCESSOR_GROUP_UNRESOLVED,
                        "该处理器群经连接器接到 %d 个互不相连的负载域，"
                        "归属待定（R03）" % (len(domain_ids),),
                    )
                )
            else:
                ownership = ProcessorOwnership.ATTACHED

            anchor = min(block_set, key=lambda key: key.as_key())
            groups.append(
                ProcessorGroup(
                    _group_id(anchor),
                    component_id,
                    block_set,
                    touched_connectors,
                    domain_ids,
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
            self._domains,
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
            members = sorted(bucket, key=lambda key: key.as_key())
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
        for key in sorted(touched, key=lambda item: item.as_key()):
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
        component_id = self._membership.get(key)
        if component_id is None:
            return ()
        out = []
        for domain_id in self._domain_ids_by_component.get(component_id, ()):
            domain = self._domains.get(domain_id)
            if domain is not None and key in domain.device_keys:
                out.append(domain_id)
        return tuple(sorted(out))

    # **没有 `_diff_slot_sources` 了。** 02-06 原文「通知 09 更新槽位来源」
    # 曾由它实现：把每个处理器群的槽位/归属前后值做成一份清单发出去。
    # 2026-09-28 随台账 D-03 定案删除——09 不消费这份清单，它每帧从
    # `snapshot().processor_groups` 重算容量（`crafting/capacity.py`），
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

        只回答**拓扑条件**：设备还在、分量不处于重建中、控制器恰好一个、
        命令依据的版本没有过期。能源与带宽分别由 04、02-08 判定。
        """
        snapshot = self._snapshot
        if expected_version is not None and expected_version != self.version:
            return TopologyVerdict(
                False, "stale", [BlockReason(Reason.STALE_VERSION, "图版本已过期")],
                self.version, None, (), ControllerState.UNKNOWN,
            )

        record = self.registry.get_by_ref(ref)
        if record is None:
            return TopologyVerdict(
                False, "stale", [BlockReason(Reason.DEVICE_GONE, "实例已不存在或代次过期")],
                self.version, None, (), ControllerState.UNKNOWN,
            )

        component = snapshot.component_of(ref.node_key)
        if component is None or snapshot.is_pending(ref.node_key):
            return TopologyVerdict(
                False, "blocked", [BlockReason(Reason.REBUILDING, "分量正在重建或尚未归属")],
                self.version, None, (), ControllerState.UNKNOWN,
            )
        if component.pending:
            return TopologyVerdict(
                False, "blocked", [BlockReason(Reason.REBUILDING, "分量正在重建")],
                self.version, component.component_id, snapshot.domain_ids_of(ref.node_key),
                ControllerState.UNKNOWN,
            )
        if component.controller_state != ControllerState.SINGLE:
            return TopologyVerdict(
                False, "blocked", component.reason_list(), self.version,
                component.component_id, snapshot.domain_ids_of(ref.node_key),
                component.controller_state,
            )
        if not record.loaded:
            return TopologyVerdict(
                False, "blocked", [BlockReason(Reason.UNLOADED_MEMBER, "设备所在区块未加载")],
                self.version, component.component_id, snapshot.domain_ids_of(ref.node_key),
                component.controller_state,
            )
        return TopologyVerdict(
            True, "ok", [], self.version, component.component_id,
            snapshot.domain_ids_of(ref.node_key), component.controller_state,
        )

    def bandwidth_domain_ids(self, ref):
        # type: (object) -> tuple
        """本设备的负载域编号。02-08 的界面字段。"""
        return self._snapshot.domain_ids_of(ref.node_key)

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
            face_b = opposite_face(face_a)
            verdict = self.catalog.can_connect(record_a.device_id, record_b.device_id)
            if not verdict.allowed:
                return {
                    "connected": False,
                    "reason": verdict.reason,
                    "detail": verdict.detail,
                }
            port_verdict = self.ports.link_verdict(
                self.definitions.category_of(record_a.device_id),
                self.definitions.category_of(record_b.device_id),
                face_a,
                face_b,
            )
            if not port_verdict.allowed:
                return {
                    "connected": False,
                    "reason": Reason.PORT_MISMATCH,
                    "detail": "%s（%s）" % (port_verdict.reason, port_verdict.detail),
                }
            return {"connected": True, "reason": "", "detail": "%s <-> %s" % (face_a, face_b)}
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


def _domain_id(anchor):
    # type: (NodeKey) -> str
    """负载域编号由成员位置直接推导，因此确定且可复现。"""
    return "domain:%d:%d,%d,%d" % (
        anchor.dimension,
        anchor.x,
        anchor.y,
        anchor.z,
    )


def _group_id(anchor):
    # type: (NodeKey) -> str
    return "processor_group:%d:%d,%d,%d" % (
        anchor.dimension,
        anchor.x,
        anchor.y,
        anchor.z,
    )
