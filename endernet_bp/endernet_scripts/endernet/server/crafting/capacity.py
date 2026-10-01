# coding=utf-8
"""订单的处理器槽位与内存租约（任务 09-03）。

## 为什么槽位与内存是**网络级**的对象

`capabilities.ProcessingSlots` 的三个方法是 `slot_count()` /
`claim_slot(execution_id)` / `release_slot(execution_id)`——领用与释放按
**执行实例**计，这本来就是网络级的语义，不是某台方块自己的属性。
`capabilities.Memory` 的 `memory_units()` 同理：单根内存条单独看没有意义，
「这张网络现在有多少内存字节」才是订单要问的问题。

## 两个台账的计费单位都是 R04 已定案的（2026-09-26）

- **槽位按「一次配方执行」计**：一个执行实例领 1 槽、**完成时**释放；
  一张订单可以同时占满网络的剩余槽位。因此槽位租约的拥有者是**执行实例 ID**
  （`订单ID/步骤下标/执行序号`），不是订单 ID——「整单领一次」是旧口径，
  已经删掉了。
- **内存按字节计**：`required_bytes = 根原料总单位物 × 1 B/单位物`，准入时
  一次性定值、原子占用，此后**不随进度、暂停、阻塞或活跃配方数重算**，
  直到完成或玩家手动中止。不足时**拒绝下达**（`ORDER_NO_MEMORY`），
  **不是**排进队列等内存。
- **两者不是同一类「放不下」**：槽位不足是等待（会自己好），内存不足是拒绝
  （要玩家重下）。因此它们各有各的原因码，见 `slot_starvation_blocker` 与
  `memory_admission_blockers`。

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

## 「每帧重算」就是本模块存在的理由（2026-09-28，台账 D-03）

02 曾经随每次发布推一份「槽位来源变化」清单（`SlotSourceChange` /
`TopologyPublication.slot_changes`），02-06 的原文是「通知 09 更新槽位来源」。
**本模块从不消费它，也不应该消费**——那张清单已随 D-03 定案删除，连只为它
存在的 `ProcessorGroup.slot_count()` 一并删掉。理由是这份**拉取**口径本身：

- **一份来源。** `slot_capacity()` 每次从权威快照现算，容量没有第二份账。
  再挂一条推送，就有了「推送到达时」与「下次重算时」两个时刻的容量，
  而两者不一致时以谁为准没有答案。
- **能自愈。** 存档重进、服务重建、事件漏一拍，下一帧重算自己就对；
  推送漏一次就**永久**偏，且症状是「槽位比实际少」，从账面上看不出来。
- **与既有纪律一致。** `script_architecture.md` §3 早就写定
  「容量从 02 的处理器群与网络成员**每帧重算**，不另建设备索引」。

**推送不是一概不要**：同一批发布的 `migrations` 就有真消费者
（`runtime.py` 的 `_apply_migration` 与 `admission.py`），因为服务迁移
**必须**在发布那一刻被处理，下一帧自己好不了。判据是「晚一帧有没有后果」。

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

    `bytes_per_memory_slot` 是这里的核心替身：**定义表里没有「一根内存条多大」
    这个配置**，R04 的「内存条规格」至今未定。它的单位是**字节**——与
    `planner.CraftPlan.required_bytes()` 同尺度，因此「内存够不够」是这两个
    数直接比大小，中间不再有第二套换算。取 1024 B 而不是 1：1 B 会让任何
    真实配方（两个铁锭就是 2 B）在准入时当场被拒，替身必须落在一个能开工的
    量级上。它照样进 `unresolved_report()`，因此没有任何一条正式验收能带着
    它盖章。

    **`slots_per_order` 与 `memory_units_per_step` 已删除**：前者是「整单领一次
    槽位」的旧口径，后者是「按聚合步骤数估内存」的旧口径，两者都被 R04 的
    定案取代（每次配方执行各领 1 槽；内存按根原料字节计）。
    """

    __slots__ = (
        "bytes_per_memory_slot",
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
        bytes_per_memory_slot=1024,
        returns_block_completion=True,
        submit_plan_budget=4096,
        plan_budget_per_tick=1024,
        orders_per_tick=4,
        max_orders=64,
        op_ledger_limit=4096,
        rejected_request_limit=64,
    ):
        # type: (int, bool, int, int, int, int, int, int) -> None
        #: **R04**：一个内存槽方块提供多少**字节**。定义表里没有这一项。
        #: 替身取 1024（见类 docstring：取 1 会让真实配方一律被拒）。
        self.bytes_per_memory_slot = int(bytes_per_memory_slot)
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
            "bytes_per_memory_slot",
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
                "field": "order.bytes_per_memory_slot",
                "rule": "R04",
                "value": self.bytes_per_memory_slot,
                "note": "一个内存槽方块提供多少字节；定义表里没有这一项，"
                        "替身取 1024 B（各内存条容量仍未定）",
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
            "bytes_per_memory_slot": self.bytes_per_memory_slot,
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

    __slots__ = ("policy", "leases", "_by_owner", "capacity", "_seq", "stats", "on_changed")

    #: 诊断里用的名字，子类覆盖。
    kind = ""

    def __init__(self, policy=None, on_changed=None):
        # type: (object, object) -> None
        self.policy = policy if policy is not None else OrderPolicy()
        self.on_changed = on_changed
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
        """领一份租约。不足时返回 `None`，**不拆一半**。

        **`units == 0` 是合法的**，只有负数才抛错。零字节的内存需求是真会
        出现的：一份计划的叶子全被库存或外部输入覆盖时 `required_bytes()`
        就是 0（见 `planner.ByteRounding`），而「每一个已通过的订单都有一条
        内存租约」是诊断与恢复要用的不变式——放行一个没有租约的订单，会让
        「它没占内存」与「它压根没通过准入」在账面上长得一模一样。
        """
        units = int(units)
        if units < 0:
            raise ValueError("租约单位数不能为负")
        if self.available() < units:
            self.stats["refused"] += 1
            return None
        lease = Lease(self._new_id(owner_id), owner_id, units, tick)
        self.leases[lease.lease_id] = lease
        self._by_owner.setdefault(owner_id, []).append(lease.lease_id)
        self.stats["claimed"] += 1
        if self.on_changed is not None:
            self.on_changed()
        return lease

    def release(self, owner_id):
        # type: (str) -> int
        """释放某个拥有者的**全部**租约，返回释放掉的单位数。

        一次放光而不是「放最新那条」：**槽位租约的拥有者是一个执行实例**
        （`claim_slot(execution_id)`），它只可能持有一条；**内存租约的拥有者
        是一张订单**，它也只可能持有一条（准入时一次性定值）。因此「全部」
        与「那一条」在这里是同一件事，分次释放反而会让「这个拥有者还占着
        几个单位」变成一个要遍历的答案。

        允许一个拥有者持有多条租约只是为了**不丢账**（见 `_by_owner` 的
        说明），不是为了让调用方分次释放。对未知的 `owner_id` 返回 0 且
        不报错——**但正因如此，传错 id 的表现是「可用量平白多出来」而
        账面上查不出多在哪**，所以释放方必须按登记过的 id 逐个调用。
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
        if total and self.on_changed is not None:
            self.on_changed()
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

    # **没有 `claim_for_order` 了。** 它曾经按 `policy.slots_per_order` 给整张
    # 订单一次领完，那是 R04 定案之前的旧口径。槽位的计费单位是**一次配方
    # 执行**，因此唯一的入口就是上面那对契约方法——它们本来就是按
    # `execution_id` 的形状，之前是订单侧把它用错了。


class MemoryLedger(_Ledger):
    """内存**字节**的租约台账。

    **它实现 `capabilities.Memory` 的 `memory_units()`**：这里答的是
    「这张网络现在有多少内存字节」，不是某根内存条的规格（见模块 docstring）。
    """

    __slots__ = ()
    kind = "memory"

    def memory_units(self):
        # type: () -> int
        """本网络当前提供的内存**字节**总数。**这是契约方法，不是「已用」。**"""
        return self.capacity

    def claim_order_bytes(self, order_id, required_bytes):
        # type: (str, int) -> object
        """按一张订单的**字节需求**领内存：全留或全不留。

        `required_bytes` 是 `planner.CraftPlan.required_bytes()`（根原料微单位
        ÷ 50，`ByteRounding` 向上取整），**不是剩余原料、不是步骤数、不是
        计划规模**：规则要求订单开始时一次性定值，此后不随进度、暂停、阻塞
        或活跃配方数重算（R04 / 09-03/08）。

        **零字节放行而不是抛错**：一份叶子全被库存覆盖的计划就是 0 B，它是一份
        合法且应当通过的订单。`claim(owner, 0)` 会给出一条 0 单位的租约，
        于是「已通过准入」这件事在内存台账上仍然看得见（见 `_Ledger.claim`）。
        负数仍然抛错——那说明上游算错了，不是业务结论。
        """
        return self.claim(order_id, int(required_bytes))


# -- 容量来源 --------------------------------------------------------------


def slot_capacity(snapshot, component_id):
    # type: (object, int) -> tuple
    """返回去重后的处理槽位数及不贡献的群说明。"""
    sources, idle = slot_sources(snapshot, component_id)
    return len(sources), idle


def slot_sources(snapshot, component_id):
    # type: (object, int) -> tuple
    """返回 `(有效处理器位置集合, 不贡献的群说明)`，容量与能源共用。

    **只读 02 已经算好的结论**，不自己遍历邻居。第二个返回值是给诊断与
    验收用的：它把「有处理器但不贡献槽位」的群逐条列出来，否则一张
    「零槽位」的网络在界面上与「根本没有处理器」一模一样。

    不贡献的两种情形各有各的原因，也已经由 02 写进 `group.reasons`
    （`PROCESSOR_GROUP_UNATTACHED` / `R03` 的 `PROCESSOR_GROUP_UNRESOLVED`），
    这里只把群引用与原因带出去。
    """
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
    return seen, idle


def memory_capacity(members, definitions, policy):
    # type: (object, object, object) -> tuple
    """从网络成员算出内存**字节**总数。返回 `(内存字节, 内存槽数量)`。

    `members` 是 `NetworkRuntime.members()` 的返回值（`NetworkNode` 列表）。
    **按定义表的分类筛**，不按方块 id 拼字符串——分类的真相只有定义表一处。
    区块未加载的成员不计入：它的状态没人能确认，而内存是要拿去开工的。

    第二个返回值仍然给**内存槽数量**（不是字节）：诊断里要能分辨「只有一根
    很大的内存条」与「有一堆很小的」，而两者在字节合计上可能一样。
    """
    count = 0
    for node in members or ():
        if getattr(node, "category", None) != CATEGORY_MEMORY:
            continue
        if not getattr(node, "loaded", False):
            continue
        count += 1
    return count * policy.bytes_per_memory_slot, count


def memory_admission_blockers(memory, required_bytes):
    # type: (object, int) -> list
    """**准入**的内存判定：不够则拒绝下达，不是排队。

    规则原文：「先按 08 分帧完成规划，再复核可用字节并原子取得内存租约；
    不足时**拒绝下达**，不转为自动等待内存的已接受订单」，并且「**等于可用值
    通过**内存这一项检查」。因此这里必须用 `>=` 放行、`<` 才拒绝——写成 `>`
    会让一份恰好占满内存的计划被无谓拒掉。

    零需求直接放行：一份叶子全被库存覆盖的计划不占内存，也不该被内存挡住。
    """
    need = max(0, int(required_bytes))
    if need <= 0 or memory.available() >= need:
        return []
    return [
        BlockReason(
            Reason.ORDER_NO_MEMORY,
            u"内存字节不够：本网络共 %d B，已租出 %d B，可用 %d B，"
            u"这份计划要占 %d B"
            % (memory.capacity, memory.used(), memory.available(), need),
        )
    ]


def slot_starvation_blocker(slots, needed=1):
    # type: (object, int) -> object
    """槽位不够时给的**仅供显示**的原因。没有空闲槽就返回 `None`。

    **它不是准入闸门。** 「一个订单应持续派发依赖已满足的任务，直到网络无
    可用槽位或没有可执行任务」——槽位用尽是**正常等待**，订单留在运行中，
    只是这一帧一个新任务都开不出来。把它做成 `BlockReason` 是为了让界面答得
    出「它为什么不动」，而不是让玩家去猜机器坏了。

    **与内存那条分开**：内存不足是拒绝下达（`ORDER_NO_MEMORY`，要玩家重下），
    槽位不足是等待（`ORDER_NO_CAPACITY`，下一帧自己会好）。两者混用会让玩家
    对着一个马上要动的订单反复重下。
    """
    if slots.available() >= int(needed):
        return None
    return BlockReason(
        Reason.ORDER_NO_CAPACITY,
        u"没有空闲的配方处理槽位：本网络 %d 个，已租出 %d 个；"
        u"跑完一个任务就会空出来" % (slots.capacity, slots.used()),
    )
