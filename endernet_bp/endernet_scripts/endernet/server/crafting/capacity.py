# coding=utf-8
"""订单的处理器槽位与内存租约（任务 09-03）。

## 为什么槽位与内存是**网络级**的对象

`capabilities.ProcessingSlots` 的三个方法是 `slot_count()` /
`claim_slot(execution_id)` / `release_slot(execution_id)`——领用与释放按
**执行实例**计，这本来就是网络级的语义，不是某台方块自己的属性。
`capabilities.Memory` 的 `memory_units()` 同理：单根内存条单独看没有意义，
「这张网络现在有多少内存单位」才是订单要问的问题。

因此本模块的两个台账 **就是** 那两个契约的实现，由工厂服务（网络服务）
构造并持有，经 `FactoryService.processing_slots()` / `memory_ledger()`
取得。**不为每台处理器再建一个能力对象**：`script_architecture.md` §3 的
纪律是「不要另建一套平行索引」，而一个只报告「我是 1 个槽位」的对象
既答不了领用问题，又给了调用方一个可以绕过台账去数数的入口。

## 槽位数量的唯一依据是 02 的处理器群

`topology.snapshot().processor_groups` 已经把归属算完了，本模块只是**读**
它，绝不自己遍历邻居去数方块：

- 没经连接器接入基带的群 `ownership == UNATTACHED` → `contributes_slots()`
  为假 → **贡献零槽位**（验收「无连接器处理器提供零槽位」）；
- 经连接器接到多个互不相连的负载域的群是 R03 未定案 → 同样不贡献；
- 群里的 `processors` 在 02 建群时按 `visited` 去过重，因此**同一物理处理器
  只属于一个群、只计一次**（验收「同一物理处理器不会被重复统计」）。

## 槽位减少时不夺回已经在跑的租约

`refresh()` 只更新容量，**不动已有租约**。处理器被拔掉之后
`used > capacity` 是允许出现的状态，表现是「新的领不到，直到有订单跑完」——
这正是 09-07 要的「停止超额新派发，现有执行保留状态，不伪造回滚外部机器」。
把租约强行收回会让一个已经投了料的订单失去它的槽位记录，而料是真的投出去了。
"""

from ...common.protocol import BlockReason, Reason

#: 内存槽设备的分类（`definitions.CATEGORY_MEMORY`）。只用来从成员里筛出
#: 内存槽方块，不在本模块做任何分类判定——分类的真相在定义表。
CATEGORY_MEMORY = "memory"


class OrderPolicy(object):
    """订单的未定案旋钮（R04 及其执行侧的同族开关）。

    与 `MediumPolicy` / `PortPolicy` / `PlanPolicy` 同形：做成具名策略而不是
    常数，因为这几条都还没定案（全局约束 7）。`assert_settled()`
    **只应在正式验收时调用**——开发与离线测试不应调用它。

    `memory_units_per_slot` 是这里的核心替身：**定义表里没有「一根内存条多大」
    这个配置**，R04 的「内存条规格和计费公式」至今未定。取 1 是保守的下界
    （「一个内存槽就是一个内存单位」），而它照样会进 `unresolved_report()`，
    因此没有任何一条正式验收能带着它盖章。
    """

    __slots__ = (
        "slots_per_order",
        "memory_units_per_slot",
        "memory_units_per_step",
        "returns_block_completion",
        "submit_plan_budget",
        "plan_budget_per_tick",
        "orders_per_tick",
        "max_orders",
        "op_ledger_limit",
        "rejected_request_limit",
    )

    def __init__(
        self,
        slots_per_order=1,
        memory_units_per_slot=1,
        memory_units_per_step=1,
        returns_block_completion=True,
        submit_plan_budget=4096,
        plan_budget_per_tick=1024,
        orders_per_tick=4,
        max_orders=64,
        op_ledger_limit=4096,
        rejected_request_limit=64,
    ):
        # type: (int, int, int, bool, int, int, int, int, int) -> None
        #: **R04**：一个订单要占几个配方处理槽位。
        #: `blocks.md` 只说了「单个处理器提供 1 个配方处理槽位」，
        #: 没说一个订单吃几个。取 1 就是文档原文的「一个订单不等于一个槽位」
        #: 那件事**还没有定案**时的保守替身——真正要防的隐含假设是
        #: 「订单数和槽位数相等」，因此这里刻意留成一个可以大于 1 的旋钮。
        self.slots_per_order = int(slots_per_order)
        #: **R04**：一个内存槽方块提供多少内存单位。定义表里没有这一项。
        self.memory_units_per_slot = int(memory_units_per_slot)
        #: **R04**：每个执行步骤要计多少内存单位。
        self.memory_units_per_step = int(memory_units_per_step)
        #: **R09**：返还物没到齐算不算「这一步没做完」。
        #: 09 验收原文「所需鱼已回流但空桶未到时步骤仍工作中」是**看得到的
        #: 既定玩法**，因此替身取 `True`（返还物到齐才算完成）。R09 的另一侧
        #: （返还物只是「顺带捞回来」，不挡住完成）是具名可选项，
        #: 但默认不是它——把既定行为当成未定项的另一侧交付是错的。
        self.returns_block_completion = bool(returns_block_completion)
        #: **R04（计费公式未定）**：提交时用来算计划的工作量上限。
        #: 它同时决定「提交能不能同步答完」——算不完的订单进 `planning`，
        #: 由执行器分帧接着算，而不是让一条命令卡住整帧。
        self.submit_plan_budget = int(submit_plan_budget)
        #: 每帧给一个正在规划的订单多少工作量。帧安全阀，不是玩法数值。
        self.plan_budget_per_tick = int(plan_budget_per_tick)
        #: 每帧最多推进几个订单（09-07 的基础轮转粒度）。
        self.orders_per_tick = int(orders_per_tick)
        #: 一张网络最多同时存在几个非终态订单。防止一条命令把内存吃光。
        self.max_orders = int(max_orders)
        #: 近期**被拒请求**记录的条数上限（帧安全阀）。它不占任何执行资源，
        #: 只是给玩家回看的近期记录，因此上限到了就淘汰最旧的。
        self.rejected_request_limit = int(rejected_request_limit)
        #: 等待台账里 `op_id` 去重表的条目上限（帧安全阀）。
        #: **必须有上界**：它是唯一一张只增不减的表，一张长跑的存档会让它
        #: 一直长下去；而它挡的是「同一条回执重试」，保留最近的几千条足够。
        self.op_ledger_limit = int(op_ledger_limit)

        for name in (
            "slots_per_order",
            "memory_units_per_slot",
            "memory_units_per_step",
            "plan_budget_per_tick",
            "orders_per_tick",
            "max_orders",
            "op_ledger_limit",
            "rejected_request_limit",
        ):
            if getattr(self, name) <= 0:
                raise ValueError("%s 必须为正，得到 %r" % (name, getattr(self, name)))
        if self.submit_plan_budget < 0:
            raise ValueError("submit_plan_budget 不能为负")

    def unresolved_report(self):
        # type: () -> list
        return [
            {
                "field": "order.slots_per_order",
                "rule": "R04",
                "value": self.slots_per_order,
                "note": "一个订单占几个配方处理槽位；替身取 1，"
                        "「订单数 == 槽位数」这个隐含假设正是它要防的",
            },
            {
                "field": "order.memory_units_per_slot",
                "rule": "R04",
                "value": self.memory_units_per_slot,
                "note": "一个内存槽方块提供多少内存单位；定义表里没有这一项，替身取 1",
            },
            {
                "field": "order.memory_units_per_step",
                "rule": "R04",
                "value": self.memory_units_per_step,
                "note": "每个执行步骤计多少内存单位；计费公式未定，替身取 1",
            },
            {
                "field": "order.returns_block_completion",
                "rule": "R09",
                "value": self.returns_block_completion,
                "note": "返还物没到齐算不算这一步没做完；替身取「算」"
                        "（09 验收原文的那一侧）",
            },
            {
                "field": "order.submit_plan_budget",
                "rule": "R04",
                "value": self.submit_plan_budget,
                "note": "提交时算计划的工作量上限；帧安全阀而非玩法数值",
            },
            {
                "field": "order.plan_budget_per_tick",
                "rule": "R04",
                "value": self.plan_budget_per_tick,
                "note": "每帧给正在规划的订单多少工作量；帧安全阀而非玩法数值",
            },
            {
                "field": "order.orders_per_tick",
                "rule": "R04",
                "value": self.orders_per_tick,
                "note": "每帧最多推进几个订单；帧安全阀而非玩法数值",
            },
        ]

    def assert_settled(self):
        # type: () -> None
        """正式验收用：仍有未定项就报错。开发与离线测试不应调用它。"""
        pending = self.unresolved_report()
        if pending:
            raise AssertionError(
                u"订单策略仍有 %d 项未定案，不能作为正式玩法交付：\n  "
                % (len(pending),)
                + u"\n  ".join(
                    u"%s（规则 %s，当前值 %r）"
                    % (item["field"], item["rule"], item["value"])
                    for item in pending
                )
            )

    def describe(self):
        # type: () -> dict
        return {
            "slots_per_order": self.slots_per_order,
            "memory_units_per_slot": self.memory_units_per_slot,
            "memory_units_per_step": self.memory_units_per_step,
            "returns_block_completion": self.returns_block_completion,
            "submit_plan_budget": self.submit_plan_budget,
            "plan_budget_per_tick": self.plan_budget_per_tick,
            "orders_per_tick": self.orders_per_tick,
            "max_orders": self.max_orders,
            "op_ledger_limit": self.op_ledger_limit,
            "rejected_request_limit": self.rejected_request_limit,
        }


# -- 租约 ------------------------------------------------------------------


class Lease(object):
    """一份可追踪的租约（槽位或内存）。"""

    __slots__ = ("lease_id", "owner_id", "units", "active", "tick")

    def __init__(self, lease_id, owner_id, units, tick=0):
        # type: (str, str, int, int) -> None
        self.lease_id = lease_id
        self.owner_id = owner_id
        self.units = int(units)
        self.active = True
        self.tick = int(tick)

    def to_dict(self):
        # type: () -> dict
        return {
            "lease_id": self.lease_id,
            "owner_id": self.owner_id,
            "units": self.units,
            "active": self.active,
            "tick": self.tick,
        }

    def __repr__(self):
        return "Lease(%s, %s, %d)" % (self.lease_id, self.owner_id, self.units)


class _Ledger(object):
    """槽位与内存租约的公共部分。

    **要么全留、要么不留**——与 05 的预留同一条口径，理由也一样：给两个订单
    各发半份槽位，谁也开不了工，而两边的账都显示「拿到了」。调用方拿到 `None`
    时的正确动作是「这单先排队」，不是「那我少占一点」。
    """

    __slots__ = ("policy", "leases", "_by_owner", "capacity", "_seq", "stats")

    #: 诊断里用的名字，子类覆盖。
    kind = ""

    def __init__(self, policy=None):
        # type: (object) -> None
        self.policy = policy if policy is not None else OrderPolicy()
        self.leases = {}
        #: `owner_id -> [lease_id]`。**必须是列表。** 存单个 id 时，
        #: 同一个拥有者领第二次会把第一条的 id 覆盖掉——那条租约还躺在
        #: `self.leases` 里、`active` 仍是真，于是它**永远放不掉**：
        #: `used()` 虚高，可用量平白少掉几个槽位，而且从账面上查不出少在哪。
        self._by_owner = {}
        self.capacity = 0
        self._seq = 0
        self.stats = {
            "claimed": 0,
            "released": 0,
            "refused": 0,
            "shrinkages": 0,
        }

    # -- 容量 ----------------------------------------------------------

    def refresh(self, capacity):
        # type: (int) -> int
        """更新本网络的容量，返回它相对上一次的变化量。

        **不动任何已有租约。** 处理器被拔掉之后 `used > capacity` 是允许的
        状态：新的领不到，已有的跑完自然释放。理由见模块 docstring。
        """
        capacity = max(0, int(capacity))
        delta = capacity - self.capacity
        if delta < 0:
            self.stats["shrinkages"] += 1
        self.capacity = capacity
        return delta

    def used(self):
        # type: () -> int
        return sum(lease.units for lease in self.leases.values() if lease.active)

    def available(self):
        # type: () -> int
        return max(0, self.capacity - self.used())

    def over_subscribed(self):
        # type: () -> int
        """超出容量的部分。正常为 0；容量缩水后可能为正，**不是错误**。"""
        return max(0, self.used() - self.capacity)

    # -- 租约 ----------------------------------------------------------

    def _new_id(self, owner_id):
        # type: (str) -> str
        self._seq += 1
        return "%s:%s:%d" % (self.kind or "lease", owner_id or "anon", self._seq)

    def claim(self, owner_id, units, tick=0):
        # type: (str, int, int) -> object
        """领一份租约。不足时返回 `None`，**不拆一半**。"""
        units = int(units)
        if units <= 0:
            raise ValueError("租约单位数必须为正")
        if self.available() < units:
            self.stats["refused"] += 1
            return None
        lease = Lease(self._new_id(owner_id), owner_id, units, tick)
        self.leases[lease.lease_id] = lease
        self._by_owner.setdefault(owner_id, []).append(lease.lease_id)
        self.stats["claimed"] += 1
        return lease

    def release(self, owner_id):
        # type: (str) -> int
        """释放某个拥有者的**全部**租约，返回释放掉的单位数。

        一次放光而不是「放最新那条」：订单的槽位与内存在它自己的账里是
        一份（`claim_for_order` 一次领完），只有取消/完成时整体释放。
        允许一个拥有者持有多条租约只是为了**不丢账**，不是为了让调用方
        分次释放——分次释放会让「这个订单还占着几个槽位」变成一个要
        遍历的答案。
        """
        lease_ids = self._by_owner.pop(owner_id, None)
        if not lease_ids:
            return 0
        total = 0
        for lease_id in lease_ids:
            lease = self.leases.pop(lease_id, None)
            if lease is None:
                continue
            lease.active = False
            total += lease.units
        self.stats["released"] += total
        return total

    def lease_of(self, owner_id):
        # type: (str) -> object
        """该拥有者名下的第一条租约（诊断用）。没有则返回 None。"""
        for lease_id in self._by_owner.get(owner_id) or ():
            lease = self.leases.get(lease_id)
            if lease is not None:
                return lease
        return None

    def owners(self):
        # type: () -> tuple
        return tuple(sorted(self._by_owner))

    def describe(self):
        # type: () -> dict
        return {
            "kind": self.kind,
            "capacity": self.capacity,
            "used": self.used(),
            "available": self.available(),
            "over_subscribed": self.over_subscribed(),
            "leases": [
                self.leases[key].to_dict() for key in sorted(self.leases)
            ],
            "policy": self.policy.describe(),
        }

    def diagnostics(self):
        # type: () -> dict
        view = self.describe()
        view["stats"] = dict(self.stats)
        return view


class SlotLedger(_Ledger):
    """配方处理槽位的租约台账。

    它**就是** `capabilities.ProcessingSlots` 的实现（见模块 docstring）：
    `slot_count()` / `claim_slot()` / `release_slot()` 是那份契约的三个方法，
    另外三个（`used` / `available` / `lease_of`）是订单与诊断用的读法。
    """

    __slots__ = ()
    kind = "slot"

    # -- capabilities.ProcessingSlots ----------------------------------

    def slot_count(self):
        # type: () -> int
        """本网络当前提供的槽位数。**这是契约方法，不是「已用」。**"""
        return self.capacity

    def claim_slot(self, execution_id):
        # type: (str) -> bool
        """按契约给一个执行实例领 1 个槽位。"""
        return self.claim(execution_id, 1) is not None

    def release_slot(self, execution_id):
        # type: (str) -> None
        self.release(execution_id)

    # -- 订单侧 --------------------------------------------------------

    def claim_for_order(self, order_id, policy=None):
        # type: (str, object) -> object
        """按策略给一个订单领它该占的槽位。"""
        policy = policy if policy is not None else self.policy
        return self.claim(order_id, policy.slots_per_order)


class MemoryLedger(_Ledger):
    """内存单位的租约台账。

    **它实现 `capabilities.Memory` 的 `memory_units()`**：这里答的是
    「这张网络现在有多少内存单位」，不是某根内存条的规格（见模块 docstring）。
    """

    __slots__ = ()
    kind = "memory"

    def memory_units(self):
        # type: () -> int
        """本网络当前提供的内存单位总数。**这是契约方法，不是「已用」。**"""
        return self.capacity

    def claim_for_order(self, order_id, steps, policy=None):
        # type: (str, int, object) -> object
        """按策略给一个订单领内存：每步若干单位，全留或全不留。

        `steps <= 0` **直接抛错**，不返回 `None`：一份零步骤的计划根本不该走到
        领租约这一步（零步骤的 `HOLD` 计划在 `_adopt_plan` 里就直接结单了），
        而返回 `None` 会让「不该调」与「内存不够」在调用方看来一模一样——
        后者是可恢复的业务结论，前者是代码写错了。
        """
        policy = policy if policy is not None else self.policy
        if int(steps) <= 0:
            raise ValueError("零步骤的计划不该申领内存租约")
        return self.claim(order_id, int(steps) * policy.memory_units_per_step)


# -- 容量来源 --------------------------------------------------------------


def slot_capacity(snapshot, component_id):
    # type: (object, int) -> tuple
    """从拓扑快照算出这张网络的槽位数。返回 `(槽位数, 不贡献的群说明)`。

    **只读 02 已经算好的结论**，不自己遍历邻居。第二个返回值是给诊断与
    验收用的：它把「有处理器但不贡献槽位」的群逐条列出来，否则一张
    「零槽位」的网络在界面上与「根本没有处理器」一模一样。

    不贡献的两种情形各有各的原因，也已经由 02 写进 `group.reasons`
    （`PROCESSOR_GROUP_UNATTACHED` / `R03` 的 `PROCESSOR_GROUP_UNRESOLVED`），
    这里只把群引用与原因带出去。
    """
    total = 0
    idle = []
    seen = set()
    for group_id in sorted(getattr(snapshot, "processor_groups", {}) or {}):
        group = snapshot.processor_groups[group_id]
        if group.component_id != component_id:
            continue
        if not group.contributes_slots():
            idle.append(
                {
                    "group_id": group_id,
                    "processors": len(group.processors),
                    "ownership": group.ownership,
                    "overlap": 0,
                    "reasons": [
                        reason.to_dict() for reason in (group.reasons or ())
                    ],
                }
            )
            continue
        # 同一个处理器只属于一个群（02 建群时按 `visited` 去过重），因此
        # `overlap` 正常恒为 0。这里再挡一道的理由是：万一日后有人改了建群
        # 算法而忘了去重，症状是**槽位凭空变多**，而那种问题不看这个数
        # 几乎查不出来（每一层单独看都对）。
        fresh = set(group.processors) - seen
        overlap = len(group.processors) - len(fresh)
        seen |= fresh
        total += len(fresh)
        if overlap:
            idle.append(
                {
                    "group_id": group_id,
                    "processors": len(group.processors),
                    "ownership": group.ownership,
                    "overlap": overlap,
                    "reasons": [],
                }
            )
    return total, idle


def memory_capacity(members, definitions, policy):
    # type: (object, object, object) -> tuple
    """从网络成员算出内存单位总数。返回 `(内存单位, 内存槽数量)`。

    `members` 是 `NetworkRuntime.members()` 的返回值（`NetworkNode` 列表）。
    **按定义表的分类筛**，不按方块 id 拼字符串——分类的真相只有定义表一处。
    区块未加载的成员不计入：它的状态没人能确认，而内存单位是要拿去开工的。
    """
    count = 0
    for node in members or ():
        if getattr(node, "category", None) != CATEGORY_MEMORY:
            continue
        if not getattr(node, "loaded", False):
            continue
        count += 1
    return count * policy.memory_units_per_slot, count


def capacity_blockers(slots, memory, steps):
    # type: (object, object, int) -> list
    """两份台账都撑不住一个新订单时给出的原因。

    共用一个 `ORDER_NO_CAPACITY` 码，但**说明分开写、两个都报**：加处理器与
    加内存槽是两件不同的事，只说一句「容量不足」等于让玩家去猜该加哪一个。
    两个都不够时两条都要出现——只报前一条会让玩家加完处理器再发现还缺内存。
    """
    out = []
    if slots.available() < slots.policy.slots_per_order:
        out.append(
            BlockReason(
                Reason.ORDER_NO_CAPACITY,
                u"配方处理槽位不够：本网络 %d 个，已租出 %d 个，"
                u"一个订单要 %d 个"
                % (slots.capacity, slots.used(), slots.policy.slots_per_order),
            )
        )
    need = max(0, int(steps)) * memory.policy.memory_units_per_step
    if need > 0 and memory.available() < need:
        out.append(
            BlockReason(
                Reason.ORDER_NO_CAPACITY,
                u"内存单位不够：本网络 %d 个，已租出 %d 个，"
                u"这份计划 %d 步要 %d 个"
                % (memory.capacity, memory.used(), int(steps), need),
            )
        )
    return out
