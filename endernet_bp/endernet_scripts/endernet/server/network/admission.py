# coding=utf-8
"""带宽负载准入：负载域容量、设备获准与 `bandwidth_ok`。

对应任务 02-05 与 02-08。拓扑（`topology.py`）给出**结构**——谁和谁相连、
哪些基带属于同一个负载域；本模块给出**数值与资格**——某个域能承载多少、
每台设备的需求是否获准。

三条纪律：

1. **业务网络与基带负载域分开算。** 域是基带子图的连通分量，
   控制器分别连接的互不相连基带各算各的。一个域满载**不会**误报
   同一控制器另一独立域上的设备（02 验收单列的一条）。
2. **带宽与能源分别判定。** 本模块不读取任何能源状态，
   因此断电既不会把 `bandwidth_ok` 变成 False，也不会抹掉已经生效的
   判定（02-08「断电不抹除仍有效的负载判定」由构造保证）。
3. **未定玩法不悄悄定案。** 混合 8/16/32 位基带的容量合成、环路、
   多接入、负载取整（R02）与恰好满载的显示口径（R13）都还没有定稿。
   它们在这里是 `BandwidthPolicy` 的**具名配置点**，默认值一律标注为
   开发测试替身（`unresolved_report()` 会逐条列出），
   正式验收前必须用 `assert_settled()` 挡住。

读取不产生副作用：`status_of()` 只查拓扑快照与设备记录，
不预留、不消耗、不改变任何资格。
"""

from ...common.definitions import (
    CATEGORY_BASEBAND,
    CATEGORY_CONTROLLER,
    QUARTERS_PER_UNIT,
    UNCONFIGURED,
    BandwidthRounding,
)
from ...common.protocol import BlockReason, DeviceStatusChanged, Reason


# -- 设备在带宽上的角色 ----------------------------------------------------


class BandwidthRole(object):
    """设备在带宽体系里的角色。"""

    #: 载体：提供容量，自身不消耗（网络基带）。
    CARRIER = "carrier"
    #: 核心：网络本身，不参与准入（网络控制器）。
    CORE = "core"
    #: 消费者：需要获准才能工作。
    CONSUMER = "consumer"
    #: 定义缺项（R10）：blocks.md 没有给出它的带宽占用，
    #: 因此**不能**把它当成 0 计费，只能报「未配置」。
    UNKNOWN = "unknown"


#: 不参与准入的两个分类。显式列出，不从「有没有写带宽」反推——
#: 控制器与基带的 `base_quarters` 是 UNCONFIGURED（未列出），
#: 与「未列出所以是 0」是两回事，这里必须自己声明。
NON_CONSUMER_ROLES = {
    CATEGORY_BASEBAND: BandwidthRole.CARRIER,
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
    #: 未获额度：容量不够，或根本没有所属负载域。
    DENIED = "denied"
    #: 结果待确认：拓扑重建中、定义缺项、多接入归属未定。
    #: 界面此时显示「—/待确认」，它不是第三个业务检查项。
    UNKNOWN = "unknown"


class CapacityMode(object):
    """一个域里多段基带的容量如何合成（R02）。"""

    #: 各段容量相加。**测试默认值，尚未定案。**
    SUM = "sum_of_segments"
    #: 取最窄的一段。
    MIN = "narrowest_segment"
    #: 取最宽的一段。
    MAX = "widest_segment"


class LoopMode(object):
    """环路如何计容量（R02）。"""

    #: 环上的每一段都计入。**测试默认值，尚未定案。**
    COUNT_EVERY_SEGMENT = "count_every_segment"
    #: 环路额外容量不计。
    IGNORE_LOOP_GAIN = "ignore_loop_gain"


class SaturationMode(object):
    """恰好满载时的显示口径（R13）。"""

    #: 已获准即为 OK，即使域已经一点余量都没有。
    #: 这是 blocks.md 第 7 行的**建议**口径，尚未定稿。
    GRANTED_IS_OK = "granted_is_ok"
    #: 需要有余量才算 OK。
    REQUIRE_HEADROOM = "require_headroom"


#: 策略字段到待定规则编号的映射。每一项都是配置点，不是已确认玩法。
UNRESOLVED_FIELDS = (
    ("capacity_mode", "R02", "混合 8/16/32 位基带的容量合成"),
    ("loop_mode", "R02", "环路是否带来额外容量"),
    ("multi_access", "R02", "同一设备落在多个互不相连的域时的归属"),
    ("rounding", "R02", "负载取整"),
    ("saturation", "R13", "恰好满载时的带宽显示口径"),
    ("priority", "R02", "容量不足时的设备优先级"),
)


class BandwidthPolicy(object):
    """负载准入的策略配置点。

    默认值全部是**开发测试替身**，`unresolved_report()` 会逐条列出。
    `assert_settled()` 供正式验收使用：规则没有定稿就不能宣布带宽数值已验收
    （对应 02 文档「这些规则未定时不能宣布带宽数值…已验收」）。
    """

    def __init__(
        self,
        capacity_mode=CapacityMode.SUM,
        loop_mode=LoopMode.COUNT_EVERY_SEGMENT,
        multi_access=None,
        rounding=None,
        saturation=SaturationMode.GRANTED_IS_OK,
        priorities=None,
    ):
        # type: (str, str, object, object, str, dict) -> None
        self.capacity_mode = capacity_mode
        self.loop_mode = loop_mode
        #: None 表示多接入归属未定（R02）：报「待确认」而不是选一个域。
        self.multi_access = multi_access
        #: None 表示沿用每个设备定义自己的取整设置。
        self.rounding = rounding
        self.saturation = saturation
        #: 设备标识 -> 优先级整数（小的先获准）。空表示按到达顺序（先到先得）。
        self.priorities = dict(priorities or {})

    # -- 取值 ----------------------------------------------------------

    def capacity_quarters(self, segments):
        # type: (list) -> object
        """由各段基带的容量算出域容量。返回 int 或 UNCONFIGURED。

        `segments` 是各段基带的 `BandwidthSpec`。任意一段的容量未配置，
        整个域的容量就**未知**——不能把未知当成 0，也不能把已知的几段
        先加起来当结论。
        """
        if not segments:
            return UNCONFIGURED
        values = []
        for spec in segments:
            capacity = getattr(spec, "capacity_quarters", None)
            if capacity is None or capacity is UNCONFIGURED:
                return UNCONFIGURED
            values.append(int(capacity))
        if self.capacity_mode == CapacityMode.MIN:
            return min(values)
        if self.capacity_mode == CapacityMode.MAX:
            return max(values)
        if self.capacity_mode == CapacityMode.SUM:
            return sum(values)
        raise ValueError("未知的容量合成方式: %r" % (self.capacity_mode,))

    def round_demand(self, quarters, definition_rounding=None):
        # type: (int, object) -> int
        """按 R02 的取整口径折算需求。默认不取整（以 1/4u 整数记账）。"""
        mode = self.rounding if self.rounding is not None else definition_rounding
        if mode in (None, BandwidthRounding.EXACT_QUARTERS):
            return int(quarters)
        if mode == BandwidthRounding.CEIL_TO_U:
            return -(-int(quarters) // QUARTERS_PER_UNIT) * QUARTERS_PER_UNIT
        if mode == BandwidthRounding.FLOOR_TO_U:
            return int(quarters) // QUARTERS_PER_UNIT * QUARTERS_PER_UNIT
        raise ValueError("未知的取整方式: %r" % (mode,))

    def saturation_ok(self, granted, used, capacity):
        # type: (bool, int, int) -> bool
        """已获准/已拒绝的设备，其 `bandwidth_ok` 应该是什么。"""
        if not granted:
            return False
        if self.saturation == SaturationMode.REQUIRE_HEADROOM:
            return used < capacity
        return True

    def priority_of(self, device_id):
        # type: (str) -> int
        return int(self.priorities.get(device_id, 0))

    def has_priorities(self):
        # type: () -> bool
        return bool(self.priorities)

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
            "带宽负载策略尚未定案，不能宣布数值已验收。待定项：\n  "
            + "\n  ".join(
                "%s（%s）：%s" % (field, rule, note)
                for field, rule, note in UNRESOLVED_FIELDS
            )
        )

    def describe(self):
        # type: () -> dict
        return {
            "capacity_mode": self.capacity_mode,
            "loop_mode": self.loop_mode,
            "multi_access": self.multi_access,
            "rounding": self.rounding,
            "saturation": self.saturation,
            "priorities": dict(self.priorities),
            "unresolved": self.unresolved_report(),
        }


# -- 结果对象 --------------------------------------------------------------


class DomainAdmission(object):
    """一个负载域的整体判定。"""

    __slots__ = (
        "domain_id",
        "version",
        "capacity_quarters",
        "used_quarters",
        "granted",
        "denied",
        "unconfigured",
        "unresolved",
        "consumers",
    )

    def __init__(
        self,
        domain_id,
        version,
        capacity_quarters,
        used_quarters,
        granted,
        denied,
        unconfigured,
        consumers,
        unresolved=(),
    ):
        self.domain_id = domain_id
        self.version = version
        self.capacity_quarters = capacity_quarters
        self.used_quarters = used_quarters
        self.granted = tuple(granted)
        self.denied = tuple(denied)
        #: 定义缺项（R10）因而无法计入的设备，按位置排序。
        self.unconfigured = tuple(unconfigured)
        #: 归属未定（R02 多接入）因而**不占用任何域额度**的设备。
        #: 把它们算进任何一个域，都等于替设备选了一个域。
        self.unresolved = tuple(unresolved)
        self.consumers = tuple(consumers)

    @property
    def free_quarters(self):
        # type: () -> object
        if self.capacity_quarters is UNCONFIGURED:
            return UNCONFIGURED
        return self.capacity_quarters - self.used_quarters

    def is_overloaded(self):
        # type: () -> bool
        """本域是否存在**未获额度**的设备。

        **不等于「域已满」。** 单台设备需求超过域总容量时，域可能还空着
        （`used_quarters == 0`）却已经有 `denied`。判断「是否真的满了」
        看 `used_quarters >= capacity_quarters`，不要用这个方法——
        文案「负载域已满载」曾经由它推出，那是错的（见
        `Reason.BANDWIDTH_DEMAND_EXCEEDS_DOMAIN`）。
        """
        return bool(self.denied)

    def to_dict(self):
        # type: () -> dict
        return {
            "domain_id": self.domain_id,
            "version": self.version,
            "capacity_quarters": (
                None if self.capacity_quarters is UNCONFIGURED else self.capacity_quarters
            ),
            "used_quarters": self.used_quarters,
            "free_quarters": (
                None if self.capacity_quarters is UNCONFIGURED else self.free_quarters
            ),
            "granted": [key.as_key() for key in self.granted],
            "denied": [key.as_key() for key in self.denied],
            "unconfigured": [key.as_key() for key in self.unconfigured],
            "unresolved": [key.as_key() for key in self.unresolved],
        }

    def __repr__(self):
        return "DomainAdmission(%s, %s/%s)" % (
            self.domain_id,
            self.used_quarters,
            self.capacity_quarters,
        )


class AdmissionResult(object):
    """一台设备的带宽判定（02-08 的对外结构）。

    界面要的字段都在这里：设备代次、`bandwidth_domain_id`、需求量、
    分配量、域容量与占用、判定版本，以及「未获额度 / 已获额度 / 待确认」
    三态和可定位原因。
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
        "domain_capacity_quarters",
        "domain_used_quarters",
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
        domain_capacity_quarters=UNCONFIGURED,
        domain_used_quarters=0,
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
        self.domain_capacity_quarters = domain_capacity_quarters
        self.domain_used_quarters = domain_used_quarters
        self.version = version
        self.reasons = tuple(reasons)
        self.notes = tuple(notes)

    @property
    def bandwidth_domain_id(self):
        # type: () -> object
        """本设备的负载域编号。无域或多域（待定）时为 None。"""
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
            self.domain_capacity_quarters,
            self.domain_used_quarters,
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
            "domain_capacity_quarters": (
                None
                if self.domain_capacity_quarters is UNCONFIGURED
                else self.domain_capacity_quarters
            ),
            "domain_used_quarters": self.domain_used_quarters,
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
            "after": self.after.to_dict(),
        }

    def __repr__(self):
        return "BandwidthChange(%s -> %s)" % (
            self.before.state if self.before is not None else None,
            self.after.state,
        )


# -- 服务 ------------------------------------------------------------------


class AdmissionService(object):
    """负载域容量与设备准入。

    `topology` 是 `TopologyService`：本服务订阅它的发布，只重算受影响的域。
    """

    def __init__(self, definitions, registry, topology, policy=None):
        # type: (object, object, object, BandwidthPolicy) -> None
        self.definitions = definitions
        self.registry = registry
        self.topology = topology
        self.policy = policy if policy is not None else BandwidthPolicy()

        #: 判定版本。每次重算加一。
        self.version = 0
        #: domain_id -> DomainAdmission
        self._domains = {}
        #: NodeKey -> 到达顺序（决定容量不足时谁先获准）。
        #: **按位置而不是按域保存**：域编号会随基带位置变化而改变，
        #: 按域保存会让整网的先后顺序在一次改名中丢失。
        self._arrival = {}
        self._next_arrival = 0
        #: NodeKey -> AdmissionResult（只覆盖已计算的域）
        self._results = {}
        self._listeners = []
        self.stats = {
            "refreshes": 0,
            "domains_recomputed": 0,
            "changes_emitted": 0,
        }
        topology.add_listener(self._on_topology_published)
        # 订阅时拓扑可能已经有快照（服务装配顺序不固定），先补算一次，
        # 否则第一次查询会落到「未知」。
        self.refresh()

    # -- 订阅拓扑 ------------------------------------------------------

    def _on_topology_published(self, publication):
        # type: (object) -> None
        """只重算受影响的域，避免每次发布都全量扫描（04 的预算要求）。

        **不能只看迁移通知。** 归属通知是按「位置 → 分量/域编号」的差异产生的，
        而域编号由基带位置推导：同一个位置把 8u 基带换成 16u 基带时，
        编号没变、成员没变，却必须重算容量。因此凡是**被重建过的分量**
        所属的域，一律重算。
        """
        snapshot = publication.snapshot
        affected = set()
        rebuilt = set(publication.rebuilt_components)
        if rebuilt:
            for domain_id, domain in snapshot.domains.items():
                if domain.component_id in rebuilt:
                    affected.add(domain_id)
        for notice in publication.migrations:
            affected.update(notice.from_domain_ids)
            affected.update(notice.to_domain_ids)
        if not self._domains:
            affected.update(snapshot.domains.keys())
        self.refresh(affected_domains=affected)

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

        只重算该设备所属的域，不重算全网：一次拔盘不该让别的分支重新分配名额。
        """
        affected = set(self.topology.snapshot().domain_ids_of(node_key))
        if not affected:
            return 0
        return self.refresh(affected_domains=affected)

    # -- 重算 ----------------------------------------------------------

    def refresh(self, affected_domains=None):
        # type: (object) -> int
        """重算指定域（None 表示全部），并刷新这些域上设备的判定。

        代价是 O(受影响设备 + 已缓存设备)：缓存里只有消费者设备
        （载体与核心按需即时计算），且这一步不做任何图遍历。
        返回判定发生变化的设备数。
        """
        snapshot = self.topology.snapshot()
        if affected_domains is None:
            affected = set(snapshot.domains)
        else:
            affected = set(affected_domains)

        # 快照里有、缓存里没有的域要补算；已经消失的域要清掉。
        for domain_id in snapshot.domains:
            if domain_id not in self._domains:
                affected.add(domain_id)
        vanished = [
            domain_id for domain_id in self._domains if domain_id not in snapshot.domains
        ]
        for domain_id in vanished:
            del self._domains[domain_id]

        touched = set()
        for domain_id in sorted(affected):
            if domain_id not in snapshot.domains:
                continue
            self._recompute_domain(snapshot, domain_id)
            touched |= snapshot.domains[domain_id].device_keys

        self.version += 1
        changes = []
        for key in sorted(touched, key=lambda item: item.as_key()):
            record = self.registry.get(key)
            if record is None:
                changes.extend(self._drop(key))
                continue
            # 载体与核心不缓存：它们的判定不消耗容量，按需即时计算即可。
            if role_of(self.definitions, record.device_id) in (
                BandwidthRole.CARRIER,
                BandwidthRole.CORE,
            ):
                changes.extend(self._drop(key))
                continue
            changes.extend(self._store(key, self._device_result(snapshot, key)))

        changes.extend(self._sweep(snapshot, touched))

        self.stats["refreshes"] += 1
        if changes:
            self.stats["changes_emitted"] += len(changes)
            self._emit(changes)
        return len(changes)

    def _sweep(self, snapshot, touched):
        # type: (object, set) -> list
        """对缓存中的设备做一次廉价复核。

        两件事只有这一步能发现：设备所在的域消失了（要撤回旧判定），
        以及分量进入重建中（旧判定必须立刻变成「待确认」，
        不能因为是「这一帧还没轮到算」就继续显示绿色）。
        """
        self._prune_arrival()
        changes = []
        for key in list(self._results):
            if key in touched:
                continue
            record = self.registry.get(key)
            if record is None or not snapshot.domain_ids_of(key):
                changes.extend(self._drop(key))
                continue
            if snapshot.is_pending(key):
                changes.extend(self._store(key, self._device_result(snapshot, key)))
        return changes

    def _recompute_domain(self, snapshot, domain_id):
        # type: (object, str) -> None
        """只更新域本身的容量与获准集合；设备判定由 `_device_result` 负责。"""
        domain = snapshot.domains[domain_id]
        self.stats["domains_recomputed"] += 1

        consumers = []
        unresolved = []
        segments = []
        for key in sorted(domain.device_keys, key=lambda item: item.as_key()):
            record = self.registry.get(key)
            if record is None:
                continue
            role = role_of(self.definitions, record.device_id)
            if role == BandwidthRole.CARRIER:
                segments.append(self.definitions.get(record.device_id).bandwidth)
            elif role in (BandwidthRole.CONSUMER, BandwidthRole.UNKNOWN):
                # 定义缺项（R10）的设备也要有判定结果——它的结果是
                # 「未配置，因此未计入」，与「没有结果」不是一回事。
                if not self._chargeable(snapshot, key, domain_id):
                    unresolved.append(key)
                    continue
                consumers.append(key)

        capacity = self.policy.capacity_quarters(segments)
        order = self._order_of(domain_id, consumers)
        used = 0
        granted = []
        denied = []
        unconfigured = []
        for key in order:
            demand = self.demand_of(key)
            if demand is UNCONFIGURED:
                unconfigured.append(key)
                continue
            if capacity is UNCONFIGURED:
                denied.append(key)
                continue
            if used + demand <= capacity:
                used += demand
                granted.append(key)
            else:
                denied.append(key)

        self._domains[domain_id] = DomainAdmission(
            domain_id,
            self.version + 1,
            capacity,
            used,
            granted,
            denied,
            unconfigured,
            order,
            unresolved,
        )

    def _chargeable(self, snapshot, key, domain_id):
        # type: (object, object, str) -> bool
        """这台设备是否应当占用**本域**的额度。

        多接入（R02）未定时归属未定，因此不占任何域的额度：
        随便算进一个域等于替设备选了域，而且会连累那个域上本来能工作的设备。
        """
        domain_ids = snapshot.domain_ids_of(key)
        if len(domain_ids) <= 1:
            return True
        return self._resolve_multi_domain(key, domain_ids) == domain_id

    def _order_of(self, domain_id, consumers):
        # type: (str, list) -> list
        """容量不足时谁先获准。

        先到先得：**已获准的设备保持在前面**，新设备排在其后。
        这样插拔介质或新增设备都不会让既有设备忽然丢失额度
        （02-08「断电不抹除仍有效的负载判定」的同类要求：
        判定不应因为一次无关的重建而翻面）。设备优先级（R02）未定，
        需要时由 `BandwidthPolicy.priorities` 注入。

        顺序号保存在**服务级**的一张表里，而不是按域保存：负载域编号由
        基带位置推导，往更低坐标加一段基带就会让整个域改名，
        按域保存会把全网设备的先后顺序一起清空，新设备就能挤掉老设备。
        """
        for key in sorted(consumers, key=lambda item: item.as_key()):
            if key not in self._arrival:
                self._arrival[key] = self._next_arrival
                self._next_arrival += 1
        ordered = sorted(consumers, key=lambda key: (self._arrival.get(key, 0), key.as_key()))
        if self.policy.has_priorities():
            ordered.sort(
                key=lambda key: (
                    self.policy.priority_of(self.registry.get(key).device_id),
                    self._arrival.get(key, 0),
                )
            )
        return ordered

    def _prune_arrival(self):
        # type: () -> None
        """丢掉已经不在注册表里的位置的顺序号。"""
        for key in list(self._arrival):
            if self.registry.get(key) is None:
                del self._arrival[key]

    def _device_result(self, snapshot, key):
        # type: (object, object) -> AdmissionResult
        """一台设备的带宽判定。**纯函数**：只读快照、注册表与已算好的域。

        多接入（R02）在这里统一处理：一台设备可能同时落在多个域上，
        因此判定必须按「设备的全部域」来做，而不是按某一个域做——
        否则两个域各算一次会互相覆盖。
        """
        record = self.registry.get(key)
        if record is None:
            return self._unknown(
                key, None, None, BandwidthRole.UNKNOWN,
                [BlockReason(Reason.DEVICE_GONE, "设备已不存在")],
            )

        role = role_of(self.definitions, record.device_id)
        demand = self.demand_of(key)
        domain_ids = snapshot.domain_ids_of(key)

        if role in (BandwidthRole.CARRIER, BandwidthRole.CORE):
            # 载体与核心不参与准入，也不缓存（见 `refresh`）。
            return self._carrier_or_core_result(record, role, domain_ids)

        if snapshot.is_pending(key):
            return self._unknown(
                key, record, role,
                [BlockReason(Reason.REBUILDING, "拓扑重建中，带宽判定待确认")],
                domain_ids=domain_ids, demand=demand,
            )

        if not domain_ids:
            return AdmissionResult(
                key, record.ref, record.device_id, role,
                BandwidthState.DENIED, False, demand, 0,
                version=self.version,
                reasons=[
                    BlockReason(
                        Reason.NO_BANDWIDTH_DOMAIN,
                        "该设备没有连到任何网络基带，不属于任何负载域",
                    )
                ],
            )

        if len(domain_ids) > 1:
            chosen = self._resolve_multi_domain(key, domain_ids)
            if chosen is None:
                # R02 未定：不擅自替设备选一个域。
                return self._unknown(
                    key, record, role,
                    [
                        BlockReason(
                            Reason.MULTI_DOMAIN_DEVICE,
                            "该设备同时接在 %d 个互不相连的负载域上，归属待定（R02）"
                            % (len(domain_ids),),
                        )
                    ],
                    domain_ids=domain_ids, demand=demand,
                )
            domain_ids = (chosen,)

        admission = self._domains.get(domain_ids[0])
        if admission is None:
            return self._unknown(
                key, record, role,
                [BlockReason(Reason.BANDWIDTH_UNKNOWN, "负载域尚未计算")],
                domain_ids=domain_ids, demand=demand,
            )
        if admission.capacity_quarters is UNCONFIGURED:
            return self._unknown(
                key, record, role,
                [BlockReason(Reason.BANDWIDTH_UNCONFIGURED, "负载域容量未配置")],
                domain_ids=domain_ids, demand=demand, admission=admission,
            )

        if role == BandwidthRole.UNKNOWN or demand is UNCONFIGURED:
            # R10：blocks.md 没给这个型号的带宽占用。未知不等于 0，
            # 因此不把它算进占用，也不假装它获准了。
            return AdmissionResult(
                key, record.ref, record.device_id, BandwidthRole.UNKNOWN,
                BandwidthState.UNKNOWN, None, UNCONFIGURED, 0,
                domain_ids=domain_ids,
                domain_capacity_quarters=admission.capacity_quarters,
                domain_used_quarters=admission.used_quarters,
                version=self.version,
                reasons=[
                    BlockReason(
                        Reason.DEFINITION_UNCONFIGURED,
                        "%s 的带宽占用在 blocks.md 中未列出（R10）"
                        % (record.device_id,),
                    )
                ],
                notes=["该设备的占用未计入域负载，域占用是已知部分的下界"],
            )

        granted = key in admission.granted
        allocated = demand if granted else 0
        ok = self.policy.saturation_ok(
            granted, admission.used_quarters, admission.capacity_quarters
        )
        reasons = []
        notes = []
        if not granted:
            # 第一条始终给出「本设备要多少 · 域已经用了多少」这两个数，
            # 让玩家自己也能算出结论；具体归属哪一种原因另说。
            reasons.append(
                BlockReason(
                    Reason.BANDWIDTH_DENIED,
                    "本设备需求 %s，域 %s 已占用 %d/%d 个四分之一 u"
                    % (
                        "未知" if demand is UNCONFIGURED else "%d" % (demand,),
                        domain_ids[0],
                        admission.used_quarters,
                        admission.capacity_quarters,
                    ),
                )
            )
            if demand is not UNCONFIGURED and demand > admission.capacity_quarters:
                # 域还空着也放不下：这不是「被别的设备挤掉」，而是单机需求
                # 超过整个域的承载上限。两种情形都拒绝，但原因必须分开报——
                # 否则界面会告诉玩家「负载域已满载」，而他看到一个 0/N 的空域。
                reasons.append(
                    BlockReason(
                        Reason.BANDWIDTH_DEMAND_EXCEEDS_DOMAIN,
                        "单台设备需求 %d 个四分之一 u 已超过本域总容量 %d"
                        % (demand, admission.capacity_quarters),
                    )
                )
            elif admission.used_quarters >= admission.capacity_quarters:
                # 真的满了：额度全部分配出去，被拒是「挤不进来」。
                reasons.append(BlockReason(Reason.BANDWIDTH_OVERLOADED, "负载域已满载"))
            # 剩下的情形是「域没满，但剩余额度不够本设备用」——第一条原因里
            # 已经给了需求与占用的两个数，结论由玩家自己算得出，这里不再补
            # 一句会误导的「满载」。三种情形都拒绝，区别只在解释。
        elif admission.used_quarters >= admission.capacity_quarters:
            # 恰好满载：按 R13 的建议口径仍然是 OK，这里只做辅助说明。
            notes.append("负载域已满载，本设备为已获准设备")

        return AdmissionResult(
            key,
            record.ref,
            record.device_id,
            BandwidthRole.CONSUMER,
            BandwidthState.GRANTED if granted else BandwidthState.DENIED,
            ok,
            demand,
            allocated,
            domain_ids=domain_ids,
            domain_capacity_quarters=admission.capacity_quarters,
            domain_used_quarters=admission.used_quarters,
            version=self.version,
            reasons=reasons,
            notes=notes,
        )

    def _carrier_or_core_result(self, record, role, domain_ids):
        # type: (object, str, tuple) -> AdmissionResult
        note = (
            "网络基带是负载载体，不参与带宽准入"
            if role == BandwidthRole.CARRIER
            else "控制器是网络核心，不占用带宽"
        )
        if len(domain_ids) == 1:
            return AdmissionResult(
                record.node_key, record.ref, record.device_id, role,
                BandwidthState.GRANTED, True, 0, 0,
                domain_ids=domain_ids,
                domain_capacity_quarters=self._capacity_of(domain_ids),
                domain_used_quarters=self._used_of(domain_ids),
                version=self.version,
                notes=[note],
            )
        if not domain_ids:
            # 载体/核心不需要带宽，但也没接在任何基带上：
            # 既不能说 OK，也不是它的带宽不够，因此报「待确认」。
            return AdmissionResult(
                record.node_key, record.ref, record.device_id, role,
                BandwidthState.UNKNOWN, None, 0, 0,
                version=self.version,
                reasons=[
                    BlockReason(
                        Reason.NO_BANDWIDTH_DOMAIN,
                        "该设备没有连到任何网络基带，不属于任何负载域",
                    )
                ],
            )
        # 控制器分别连接两条互不相连的基带是 blocks.md 规定的正常形态，
        # 但**各域的预算不合并**：这里不给出一个加总后的容量数字。
        return AdmissionResult(
            record.node_key, record.ref, record.device_id, role,
            BandwidthState.GRANTED, True, 0, 0,
            domain_ids=domain_ids,
            version=self.version,
            notes=[note, "该设备同时属于多个负载域，各域预算不合并计算"],
        )

    def _unknown(self, key, record, role, reasons, domain_ids=(), demand=0, admission=None):
        # type: (...) -> AdmissionResult
        return AdmissionResult(
            key,
            record.ref if record is not None else None,
            record.device_id if record is not None else None,
            role,
            BandwidthState.UNKNOWN,
            None,
            demand,
            0,
            domain_ids=domain_ids,
            domain_capacity_quarters=(
                admission.capacity_quarters if admission is not None else UNCONFIGURED
            ),
            domain_used_quarters=admission.used_quarters if admission is not None else 0,
            version=self.version,
            reasons=reasons,
        )

    def _resolve_multi_domain(self, key, domain_ids):
        # type: (object, tuple) -> object
        """多接入归属（R02）。未配置策略时返回 None：不擅自选一个域。"""
        resolver = self.policy.multi_access
        if resolver is None:
            return None
        if callable(resolver):
            chosen = resolver(key, domain_ids)
        else:
            chosen = resolver
        if chosen not in domain_ids:
            raise ValueError(
                "多接入策略返回的域 %r 不在该设备的域集合 %r 中"
                % (chosen, domain_ids)
            )
        return chosen

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

    # -- 查询 ----------------------------------------------------------

    def demand_of(self, node_key):
        # type: (object) -> object
        """本设备的带宽需求。返回四分之一 u 的整数，或 UNCONFIGURED。

        存储驱动器与内存槽的需求随所装介质变化，因此按**当前**记录读取，
        拔盘之后重算就会变小。介质数量放在 `DeviceRecord.config` 里，
        由任务 05 在插拔时写入。
        """
        record = self.registry.get(node_key)
        if record is None:
            return UNCONFIGURED
        spec = self.definitions.get(record.device_id).bandwidth
        disks = int(record.config.get("disk_count", 0) or 0)
        memories = int(record.config.get("memory_count", 0) or 0)
        quarters = spec.demand_quarters(disks, memories)
        if quarters is UNCONFIGURED:
            return UNCONFIGURED
        return self.policy.round_demand(quarters, spec.rounding)

    def status_of(self, ref):
        # type: (object) -> AdmissionResult
        """**只读**查询：本设备的带宽判定。断电、超载、无主控时同样可读。

        消费者设备的判定在每次发布与每次负载变化时算好并缓存，
        因此这里通常只是查一次表。缓存没有的分支（载体、核心、
        无域、代次过期）按需现算，同样不改动任何状态。
        """
        node_key = ref.node_key
        record = self.registry.get_by_ref(ref)
        if record is None:
            return AdmissionResult(
                node_key, ref, None, BandwidthRole.UNKNOWN, BandwidthState.UNKNOWN,
                None, UNCONFIGURED, 0, version=self.version,
                reasons=[BlockReason(Reason.DEVICE_GONE, "实例已不存在或代次过期")],
            )
        cached = self._results.get(node_key)
        if cached is not None and cached.ref == ref:
            return cached
        # 缓存是按**位置**存的，而位置可以被顶替：同一个坐标先放终端、
        # 再换成内存槽，缓存里那条判定属于已经不存在的那个实例。
        # 代次对不上就必须重算，否则会把旧设备的绿色判定连同错误的需求量
        # 一起发给调用方——而调用方正好是在 `tick()` 里消费设备变化的那些服务。
        return self._device_result(self.topology.snapshot(), node_key)

    def _capacity_of(self, domain_ids):
        for domain_id in domain_ids:
            admission = self._domains.get(domain_id)
            if admission is not None:
                return admission.capacity_quarters
        return UNCONFIGURED

    def _used_of(self, domain_ids):
        total = 0
        for domain_id in domain_ids:
            admission = self._domains.get(domain_id)
            if admission is not None:
                total += admission.used_quarters
        return total

    def domain(self, domain_id):
        # type: (str) -> DomainAdmission
        return self._domains.get(domain_id)

    def domains(self):
        # type: () -> dict
        return dict(self._domains)

    # -- 诊断 ----------------------------------------------------------

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report.update(
            {
                "version": self.version,
                "domains": dict(
                    (domain_id, admission.to_dict())
                    for domain_id, admission in self._domains.items()
                ),
                "tracked_devices": len(self._results),
                "policy": self.policy.describe(),
                "energy_independent": True,
            }
        )
        return report
