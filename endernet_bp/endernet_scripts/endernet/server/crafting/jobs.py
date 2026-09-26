# coding=utf-8
"""订单、步骤状态机、等待台账与订单集合（任务 09-01 / 09-02 / 09-06 / 09-08）。

**本模块是纯领域**：不认识网络服务、不认识引擎、一条命令都不发。它只回答
「一张订单现在是什么状态、它欠谁多少东西」。真正去动库存、去备料、去派发的
是 `executor.py`，装配它们的是 `executor.FactoryService`。

## 三份账必须分开记（09-01 的核心）

任务文档要求「所有过渡须记录**实际资源归属**」。这是有意的：一份计划里
同一个数量在三个不同的地方以三副面孔出现，混在一起就会出现「账面守恒、
实物不守恒」。

| 账 | 意思 | 谁说了算 |
| --- | --- | --- |
| `reserved` | 网络库存里**为这张订单锁住**的量 | 05 的预留台账（`Reservation.parts`） |
| `staged` | 已经离开网络、躺在样板口**中转位**里的量 | 06 的 `BatchState.staged` |
| `delivered` | 真的**进了外部机器**的量 | 06 的 `BatchState.delivered` |

`delivered` 是从 `PortState.batch` 读回来的**真实读数**，不是推算出来的。
因此「我以为我投了 10 个」与「机器真的收到了 10 个」在这份台账里是两个数，
而后者才允许拿去和产物回流对账。

## 步骤为什么按 `canonical` 记账，而资源键要另拿

08 的计划内容是**纯数据**（`to_dict()` 之后能直接 `json.dumps`），因此
`steps[].inputs[].key` 是名字字符串、`seal` 里的键也已经降成了
`{"key", "canonical"}`。而 05/06 的命令要的是活的 `ResourceKey`。

**资源键的唯一来源是 07 复核通过的那块样板本身**（`check_dispatch` 返回的
`entry.definition` 里的 `PatternStack.key`）。计划里的 `canonical` 用来**对齐**
两边（同一条输入在计划与样板里必须是同一个 `canonical`），对不上就说明
样板和计划说的不是一回事，直接拒绝——`ResourceKey` 没有「从 canonical 反解」
的入口，也不该有：反解会经过身份归一化，解出来的可能不是原来那个键。
"""

from ...common.protocol import Reason

#: 文本类型。**py2 里 `str` 与 `unicode` 是两个不同的类型**，而
#: `ResourceKey.canonical()` 在 py2 上返回的是 `unicode`——只判 `str`
#: 会让一份合法的规范文本被当成「读不懂的键」丢掉。
try:
    TEXT_TYPES = (str, unicode)  # noqa: F821 (仅 py2 存在)
except NameError:
    TEXT_TYPES = (str,)


class OrderStatus(object):
    """订单状态。**取值与迁移表是契约**，改这里就要改诊断与 11 的文案。"""

    #: 计划还没算完（提交时的工作量上限用尽，由执行器分帧接着算）。
    PLANNING = "planning"
    #: 计划与预留都成立，等着拿槽位开工。
    QUEUED = "queued"
    #: 正在推进步骤。
    RUNNING = "running"
    #: 有东西挡着，恢复条件满足后自己会回到 QUEUED/RUNNING。
    BLOCKED = "blocked"
    #: 收到取消请求，正在释放预留与注销等待台账。**过渡态**：
    #: 在途原料还没处置完之前不能算已取消。
    CANCELLING = "cancelling"
    #: 全部必需步骤完成，**且最终产物已实际存入网络管理的存储**。
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    #: 不可恢复的失败（订单自己的账已经对不上了）。与 `BLOCKED` 分开：
    #: 阻塞会自己好，失败不会，需要人来处理。
    FAILED = "failed"


#: 终态。到了这里就不再被 tick 推进，也不再接受取消。
TERMINAL_STATUSES = frozenset(
    [OrderStatus.COMPLETED, OrderStatus.CANCELLED, OrderStatus.FAILED]
)

#: 还活着的状态。`max_orders` 数的是这一族。
LIVE_STATUSES = frozenset(
    [
        OrderStatus.PLANNING,
        OrderStatus.QUEUED,
        OrderStatus.RUNNING,
        OrderStatus.BLOCKED,
        OrderStatus.CANCELLING,
    ]
)

#: 允许的迁移。**从这里之外来的边一律拒绝**，包括「从终态复活」——
#: 静默接受一条非法边，等于让一个已经被取消的订单某天又开始动别人的库存。
ORDER_TRANSITIONS = {
    OrderStatus.PLANNING: frozenset(
        [OrderStatus.QUEUED, OrderStatus.BLOCKED, OrderStatus.CANCELLING,
         OrderStatus.FAILED, OrderStatus.CANCELLED,
         # **`COMPLETED` 这一条边是真机上补的。** `HOLD` 口径下
         # 「库里已经够了」得到的是**零步骤但 COMPLETE** 的计划
         # （08 交付说明第五节），而那张订单从 `PLANNING` 直接结单——
         # 表里没有这条边时 `move_to` 静默返回 `False`，订单永远停在
         # 「规划中」空转：`plan_job` 已经是 `None`，`_step_plan` 变成了
         # 一个什么都不做的空操作，而它自己的账看起来全都对。
         # 离线没照到它，是因为没有一条端到端用例提交过零步骤订单
         # （只单测了 `Order.progress()` 的零分母口径）。
         OrderStatus.COMPLETED]
    ),
    OrderStatus.QUEUED: frozenset(
        [OrderStatus.RUNNING, OrderStatus.BLOCKED, OrderStatus.CANCELLING,
         OrderStatus.FAILED, OrderStatus.CANCELLED]
    ),
    OrderStatus.RUNNING: frozenset(
        [OrderStatus.BLOCKED, OrderStatus.QUEUED, OrderStatus.CANCELLING,
         OrderStatus.COMPLETED, OrderStatus.FAILED]
    ),
    OrderStatus.BLOCKED: frozenset(
        [OrderStatus.RUNNING, OrderStatus.QUEUED, OrderStatus.CANCELLING,
         OrderStatus.FAILED, OrderStatus.CANCELLED]
    ),
    OrderStatus.CANCELLING: frozenset(
        [OrderStatus.CANCELLED, OrderStatus.FAILED]
    ),
    OrderStatus.COMPLETED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.FAILED: frozenset(),
}


class StepStatus(object):
    """步骤状态。

    迁移是单向的一条链，**没有「退回去重来」的边**：原料已经进了外部机器就
    不可能假装没进（09 文档：「不伪造回滚外部机器」）。推进不下去时进
    `BLOCKED`，条件恢复后**从它自己原来的位置继续**，而不是从头再来一遍——
    从头再来就是「重发整批原料」，那是验收明令禁止的。
    """

    WAITING_INPUTS = "waiting_inputs"
    #: 正在把原料从网络搬进样板口的中转位（`port_stage`）。
    STAGING = "staging"
    #: 中转位里的料正被投进外部机器（接口自己的轮询在做这件事）。
    DISPATCHED = "dispatched"
    #: 料投完了，等产物回流（任务 10）。
    WAITING_OUTPUTS = "waiting_outputs"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


STEP_TERMINAL_STATUSES = frozenset(
    [StepStatus.COMPLETED, StepStatus.CANCELLED]
)

#: 链上每个状态可以从哪里来。与订单那张表同一条纪律。
STEP_TRANSITIONS = {
    StepStatus.WAITING_INPUTS: frozenset(
        [StepStatus.STAGING, StepStatus.BLOCKED, StepStatus.CANCELLED]
    ),
    StepStatus.STAGING: frozenset(
        [StepStatus.DISPATCHED, StepStatus.BLOCKED, StepStatus.CANCELLED]
    ),
    StepStatus.DISPATCHED: frozenset(
        [StepStatus.STAGING, StepStatus.WAITING_OUTPUTS, StepStatus.BLOCKED,
         StepStatus.CANCELLED]
    ),
    StepStatus.WAITING_OUTPUTS: frozenset(
        [StepStatus.COMPLETED, StepStatus.BLOCKED, StepStatus.CANCELLED]
    ),
    StepStatus.COMPLETED: frozenset(),
    StepStatus.BLOCKED: frozenset(
        [StepStatus.WAITING_INPUTS, StepStatus.STAGING, StepStatus.DISPATCHED,
         StepStatus.WAITING_OUTPUTS, StepStatus.CANCELLED]
    ),
    StepStatus.CANCELLED: frozenset(),
}


# -- 单据 ------------------------------------------------------------------


class StepInput(object):
    """一个步骤对**一种**原料的需求与它现在的真实归属。

    同一种料在计划里可能写成好几条（合成样板的有序摆位必然如此，
    `_check_layout` 要求每条占一个槽位），08 那边已经按键合并过，
    这里的 `required` 也是合并后的合计。
    """

    __slots__ = (
        "key",
        "canonical",
        "required",
        "staged",
        "delivered",
        "delivered_base",
        "detail",
    )

    def __init__(self, key, canonical, required):
        # type: (object, str, int) -> None
        #: 活的 `ResourceKey`，来自复核通过的样板定义（见模块 docstring）。
        self.key = key
        self.canonical = canonical
        #: 这一步一共要多少（计划里已经乘过批次数）。
        self.required = int(required)
        #: **累计离开网络**进了中转位的量。单调不减，换批也不清零：
        #: 它是「往中转位搬够了没有」的依据（`to_stage()`），
        #: 归零会让同一种料被搬第二遍。
        self.staged = 0
        #: **当前这批已经投进机器的量**（`delivered_base` + 活批次的读数）。
        #: 换批时把旧批次的终值折进 `delivered_base`，否则新批次的
        #: `delivered` 从 0 重新数，这一步会以为料还没投出去而重投整批。
        self.delivered = 0
        self.delivered_base = 0
        self.detail = ""

    def debt(self):
        # type: () -> int
        """还差多少没进机器。"""
        return max(0, self.required - self.delivered)

    def to_stage(self):
        # type: () -> int
        """还要往中转位搬多少。

        **用累计的 `staged` 而不是「中转位里现在有多少」**：中转位里躺着的
        那部分已经离开网络了，再搬一次就是重复抽取。至于中间被投进机器的
        那些——`staged` 只会更大，于是这里的差额只会更保守，不会超发。
        """
        return max(0, self.required - self.staged)

    def is_satisfied(self):
        # type: () -> bool
        return self.delivered >= self.required

    def to_dict(self):
        # type: () -> dict
        return {
            "key": self.canonical,
            "required": self.required,
            "staged": self.staged,
            "delivered": self.delivered,
            "debt": self.debt(),
            "detail": self.detail,
        }

    def __repr__(self):
        return "StepInput(%s, %d/%d)" % (self.canonical, self.delivered, self.required)


class StepOutput(object):
    """一个步骤**应当期待**回流的产物或返还物。

    `returns` 与 `outputs` 用同一个结构：两者都要等任务 10 分配回来，
    区别只在「它算不算这一步做成了」——`required=True` 的那些全到齐，
    这一步才叫完成（09 文档：「所需鱼已回流但空桶未到时步骤仍工作中」，
    说的正是返还物**不算**完成条件）。
    """

    __slots__ = ("key", "canonical", "expected", "received", "counts_for_completion")

    def __init__(self, key, canonical, expected, counts_for_completion=True):
        # type: (object, str, int, bool) -> None
        self.key = key
        self.canonical = canonical
        self.expected = int(expected)
        self.received = 0
        self.counts_for_completion = bool(counts_for_completion)

    def remaining(self):
        # type: () -> int
        return max(0, self.expected - self.received)

    def is_satisfied(self):
        # type: () -> bool
        return self.received >= self.expected

    def to_dict(self):
        # type: () -> dict
        return {
            "key": self.canonical,
            "expected": self.expected,
            "received": self.received,
            "remaining": self.remaining(),
            "counts_for_completion": self.counts_for_completion,
        }

    def __repr__(self):
        return "StepOutput(%s, %d/%d)" % (self.canonical, self.received, self.expected)


class OrderStep(object):
    """一个执行步骤的运行时状态。"""

    __slots__ = (
        "index",
        "rank",
        "canonical",
        "label",
        "device_id",
        "node_key",
        "generation",
        "slot",
        "digest",
        "mode",
        "batches",
        "status",
        "blocked_from",
        "inputs",
        "outputs",
        "returns",
        "execution_id",
        "op_id",
        "op_attempt",
        "port_node_key",
        "blockers",
        "detail",
        "residue",
    )

    def __init__(self, index, canonical, provider, batches=0):
        # type: (int, str, dict, int) -> None
        self.index = int(index)
        #: 依赖层级，仅用于排序与诊断。**推进顺序用的是 `execution_order()`**，
        #: 不是它（08 第七节第 13 条：`depth` 不满足拓扑序）。
        self.rank = 0
        self.canonical = canonical
        #: 可读名（给界面与诊断）。**与 `canonical` 分开**：`key` 这一字段
        #: 在 08 的计划里就是名字，两边读起来得是同一个东西；而排序、
        #: 对齐、去重一律走 `canonical`。取不到名字时退化成 `canonical`，
        #: 于是界面上显示的是身份文本——难看，但不是空。
        self.label = canonical
        provider = provider or {}
        #: 提供者的方块 id。诊断与「是不是装配仓」的判定要用它，而那一句
        #: 判定属于装配层（见 `executor._port_for`），这里只负责记着。
        self.device_id = provider.get("device_id") or ""
        self.node_key = provider.get("node_key")
        self.generation = provider.get("generation")
        self.slot = provider.get("slot")
        self.digest = provider.get("digest")
        self.mode = provider.get("mode")
        self.batches = int(batches)
        self.status = StepStatus.WAITING_INPUTS
        #: 被挡住之前在哪。恢复时**回到这里继续**，不是从头再来一遍——
        #: 从头再来就是「重发整批原料」，验收明令禁止。
        self.blocked_from = None
        self.inputs = []
        self.outputs = []
        self.returns = []
        #: 任务 10 用来认这一步的身份。**派发确认之后**才登记，
        #: 因此它在 WAITING_INPUTS 阶段是 None。
        self.execution_id = None
        #: 本步当前这一批备料的 `op_id`（可与 execution_id 不同：
        #: 一个步骤可能分几次备料）。
        self.op_id = None
        #: 已经开过几批。**`op_id` 必须带上它**：批次之间要能分开，
        #: 否则第二批会命中第一批的协调器账本，拿到**旧回执**，
        #: 而 `state.batch` 已经被新批次覆盖——两个读数从此对不上，
        #: 表现是 `delivered` 一帧涨一截而料根本没动。
        self.op_attempt = 0
        #: 备料落在哪台接口上。
        self.port_node_key = None
        self.blockers = []
        self.detail = ""
        #: 取消或让位时**说不清归属**的原料。R05 未定，替身口径是
        #: 「留在原地不动、报出来」，绝不悄悄丢弃或复制。
        self.residue = []

    def is_runnable(self):
        # type: () -> bool
        return self.status not in STEP_TERMINAL_STATUSES

    def is_terminal_step(self):
        # type: () -> bool
        return self.status in STEP_TERMINAL_STATUSES

    def all_inputs_delivered(self):
        # type: () -> bool
        return all(item.is_satisfied() for item in self.inputs)

    def outputs_satisfied(self):
        # type: () -> bool
        return all(
            item.is_satisfied()
            for item in self.outputs + self.returns
            if item.counts_for_completion
        )

    def to_dict(self):
        # type: () -> dict
        return {
            "index": self.index,
            "rank": self.rank,
            #: 与 08 的 `steps[]` 同形：`key` 是可读名、`canonical` 是身份文本。
            "key": self.label,
            "canonical": self.canonical,
            "status": self.status,
            "batches": self.batches,
            "slot": self.slot,
            "mode": self.mode,
            "digest": self.digest,
            "execution_id": self.execution_id,
            "op_id": self.op_id,
            "inputs": [item.to_dict() for item in self.inputs],
            "outputs": [item.to_dict() for item in self.outputs],
            "returns": [item.to_dict() for item in self.returns],
            "blockers": [reason.to_dict() for reason in self.blockers],
            "detail": self.detail,
            "residue": list(self.residue),
        }

    def __repr__(self):
        return "OrderStep(#%d, %s, %s)" % (self.index, self.canonical, self.status)


# -- 等待台账（与任务 10 的接缝，见 technical_notes/order_returns_contract.md）--


class WaitingLedger(object):
    """**每执行实例每产物的剩余需求**与回执按 `op_id` 去重。

    09 侧只做这两件事，全局的「资源键 → 等待执行实例」索引与分配顺序
    （R06）归 10 的 `returns.py`。分开的理由写在
    `plans/technical_notes/order_returns_contract.md`：索引要跨网络看所有
    等待者才能决定「这份产物该给谁」，而剩余需求是**某个执行实例自己的账**，
    只有它自己知道。

    取消时 09 必须 `unregister`，否则一份迟到的产物会去命中一个已经不存在的
    订单——那正是 10-07 要求「由 09 注销等待记录」的原因。
    """

    __slots__ = ("_steps", "_ops", "_op_order", "op_limit", "stats")

    def __init__(self, op_limit=4096):
        # type: (int) -> None
        #: `execution_id -> {canonical: StepOutput}`
        self._steps = {}
        #: `(execution_id, op_id, canonical) -> 接受量`。**去重的键含执行实例**：
        #: 同一个 op_id 不可能属于两笔订单，但把执行实例放进键里能让
        #: 「注销之后又来一条迟到回执」不至于命中别的实例。
        self._ops = {}
        #: 只用来按插入顺序淘汰最旧的那条。`dict` 在 py2 里没有有序迭代，
        #: 而「随机淘汰」会让去重在大表上变成偶发失效——那是查不出来的那种坏。
        self._op_order = []
        self.op_limit = max(1, int(op_limit))
        self.stats = {
            "registered": 0,
            "unregistered": 0,
            "observed": 0,
            "replayed": 0,
            "unknown": 0,
            "orphan": 0,
            "no_op_id": 0,
            "evicted": 0,
        }

    def _remember(self, dedup_key, accepted):
        # type: (tuple, int) -> None
        """记住一条回执。**表必须有上界**：它是唯一一张只增不减的表。"""
        if dedup_key in self._ops:
            return
        self._ops[dedup_key] = accepted
        self._op_order.append(dedup_key)
        while len(self._op_order) > self.op_limit:
            oldest = self._op_order.pop(0)
            self._ops.pop(oldest, None)
            self.stats["evicted"] += 1

    # -- 登记 ----------------------------------------------------------

    def register(self, execution_id, expectations, tick=0):
        # type: (str, list, int) -> bool
        """登记一个执行实例要等的产物。重复登记是幂等的（替换）。

        `expectations` 是 `StepOutput` 序列。替换而不是累加：重派同一个
        执行实例时（同一块样板、同一个步骤）期望应当被重算，累加会让
        剩余需求凭空翻倍，然后永远等不到。
        """
        if not execution_id:
            return False
        self._steps[execution_id] = {
            item.canonical: item for item in (expectations or ())
        }
        self.stats["registered"] += 1
        return True

    def unregister(self, execution_id):
        # type: (str) -> int
        """注销一个执行实例。返回它还有多少**没等到**的件数。

        返回值是诊断用的：取消时如果是正数，说明有产物还在外面，
        调用方要把这个数报出来，而不是让订单看起来干净地结束了。
        """
        pending = self._steps.pop(execution_id, None)
        if pending is None:
            return 0
        self.stats["unregistered"] += 1
        return sum(item.remaining() for item in pending.values())

    def is_registered(self, execution_id):
        # type: (str) -> bool
        return execution_id in self._steps

    # -- 回执 ----------------------------------------------------------

    def observe(self, execution_id, key, count, op_id):
        # type: (object, object, int, str) -> int
        """一条产物回执。返回**本次实际被接受的量**。

        三件事按顺序发生：

        1. **执行实例不认识 → 0**，并计数 `orphan`。迟到的产物命中的是
           一个已经取消的实例，此时既不接受也不报错——报错会让 10 的重试
           逻辑把同一条回执反复送进来。
        2. **`op_id` 已经见过 → 0**，并计数 `replayed`。返回 0 而不是原接受量：
           调用方问的是「这一次又收下多少」，重复回执收下的是 0。返回原值会
           让调用方（或恢复流程）把同一次入库算两遍——10-05 要防的正是这个。
        3. 正常路径：按**剩余需求**截断，**不接受超出的部分**。多出来的产物
           留在网络里当普通库存（10-06 明写「剩余不被订单接受的资源保留为
           普通库存或原缓冲，不丢弃」），因此这里不吞、不丢，只是不计数。
        """
        count = int(count)
        if count <= 0:
            return 0
        row = self._steps.get(execution_id)
        if row is None:
            self.stats["orphan"] += 1
            return 0
        canonical = _canonical_of(key)
        if canonical is None:
            # 既不是 `ResourceKey` 也不是规范文本。**不去猜**：猜错的后果是
            # 一条真实产物落进 `unknown` 而订单永远不完成，日志里什么都看不出来。
            self.stats["unknown"] += 1
            return 0
        dedup_key = (execution_id, op_id, canonical)
        if not op_id:
            # 没有 op_id 就没法去重（10-05 要求它非空）。**仍然收下**——
            # 丢掉一条真实入库会让订单永远等差，而那是更难查的一种坏；
            # 但要计数，让「谁没用 op_id」在诊断里看得见。
            self.stats["no_op_id"] += 1
        elif dedup_key in self._ops:
            self.stats["replayed"] += 1
            return 0
        item = row.get(canonical)
        if item is None:
            # 这个实例不等这个键。**不是错误**：同一种料可能同时是这一步的
            # 产物与别人的原料，而 10 的索引按键找等待者时会找到多个。
            self.stats["unknown"] += 1
            return 0
        accepted = min(count, item.remaining())
        if op_id:
            self._remember(dedup_key, accepted)
        if accepted <= 0:
            return 0
        item.received += accepted
        self.stats["observed"] += 1
        return accepted

    # -- 读法 ----------------------------------------------------------

    def pending(self, execution_id):
        # type: (str) -> dict
        row = self._steps.get(execution_id)
        if row is None:
            return {}
        return dict(
            (canonical, item.remaining())
            for canonical, item in row.items()
            if item.remaining() > 0
        )

    def is_satisfied(self, execution_id):
        # type: (str) -> bool
        row = self._steps.get(execution_id)
        if row is None:
            return False
        return all(
            item.is_satisfied()
            for item in row.values()
            if item.counts_for_completion
        )

    def view(self, execution_id=None):
        # type: (str) -> object
        if execution_id is not None:
            row = self._steps.get(execution_id)
            if row is None:
                return None
            return dict(
                (canonical, item.to_dict())
                for canonical, item in sorted(row.items())
            )
        return dict(
            (key, dict((c, item.to_dict()) for c, item in sorted(row.items())))
            for key, row in sorted(self._steps.items())
        )

    def diagnostics(self):
        # type: () -> dict
        return {
            "instances": len(self._steps),
            "pending": sum(
                1
                for row in self._steps.values()
                for item in row.values()
                if item.remaining() > 0
            ),
            "op_entries": len(self._ops),
            "op_limit": self.op_limit,
            "stats": dict(self.stats),
        }


def _canonical_of(key):
    # type: (object) -> object
    """回执里的资源键 -> 规范文本。**两种形状之外一律返回 None，不去猜。**

    接受 `ResourceKey`（有 `canonical()`）与**已经是规范文本的字符串**。
    **刻意不做 `str(key)` 兜底**：在 py2 上对一个非 ASCII 的 `unicode`
    调 `str()` 会抛 `UnicodeEncodeError`，而它带着的全部信息（那是一个
    资源键）正是调用方最需要看到的——把异常换成「静默记成 unknown」，
    表现就是订单永远停在等产物，而日志里一条线索都没有。
    """
    canonical = getattr(key, "canonical", None)
    if callable(canonical):
        return canonical()
    return key if isinstance(key, TEXT_TYPES) else None


# -- 订单 ------------------------------------------------------------------


class Order(object):
    """一张订单：计划、预留凭据、执行批次与阻塞原因。"""

    __slots__ = (
        "order_id",
        "component_id",
        "master_instance_id",
        "target",
        "canonical",
        "amount",
        "status",
        "status_detail",
        "plan",
        "plan_job",
        "steps",
        "reservations",
        "slot_lease",
        "memory_lease",
        "blockers",
        "paused",
        "created_tick",
        "updated_tick",
        "plan_digest",
        "inventory_version",
        "topology_version",
        "stats",
        "residue",
        "adopted",
        "request_id",
    )

    def __init__(self, order_id, component_id, master_instance_id, target, canonical,
                 amount, tick=0, request_id=""):
        # type: (str, int, str, object, str, int, int, str) -> None
        self.order_id = order_id
        #: **玩家那条命令的请求 ID**，与 `order_id` 是两件事：订单 ID 标识
        #: 「这一单」，请求 ID 标识「玩家点的哪一下」。规划是分帧的，拒绝要
        #: 过几帧才发生，那时玩家早就离开界面了——拒绝记录里存的是**它**，
        #: 客户端才认得出这是自己那条请求的回音（2026-09-26 追加决定 2）。
        self.request_id = request_id
        self.component_id = component_id
        #: 下单时那张网络的主控实例。**它变了订单就不该继续跑**（09 验收：
        #: 拓扑拆分后订单只属于原主控）。主控换人时订单转 BLOCKED 并报出来，
        #: 而不是跟着新主控继续动库存。
        self.master_instance_id = master_instance_id
        self.target = target
        self.canonical = canonical
        self.amount = int(amount)
        self.status = OrderStatus.PLANNING
        self.status_detail = ""
        #: `CraftPlan`（08 的只读产物）。规划中时为 None。
        self.plan = None
        #: `PlanJob`，规划中时持有；算完即丢。
        self.plan_job = None
        self.steps = []
        #: `canonical -> {"reservation_id", "key", "claimed", "reserved"}`。
        #: `claimed` 是计划声明的量，`reserved` 是**现在真的锁着**的量
        #: （备料时会部分释放），两者分开才能在诊断里看出差异。
        self.reservations = {}
        self.slot_lease = None
        self.memory_lease = None
        self.blockers = []
        #: 显式暂停（与「被挡住的阻塞」分开，见 `Reason.ORDER_PAUSED`）。
        self.paused = False
        self.created_tick = int(tick)
        self.updated_tick = int(tick)
        self.plan_digest = ""
        #: 计划依据的库存与拓扑版本。用于「变化了就重验」。
        self.inventory_version = None
        self.topology_version = None
        self.stats = {
            "ticks": 0,
            "plans": 0,
            "revalidations": 0,
            "reserves": 0,
            "releases": 0,
            "stagings": 0,
            "dispatches": 0,
            "outputs": 0,
        }
        #: 说不清归属的原料（取消时留在中转位/机器里的）。
        self.residue = []
        #: 计划是否已经**真正落地**（复核通过、步骤建好、材料锁住、租约到手）。
        #: 它与「有没有计划」是两件事：计划先落地一半就撞上容量不足的订单，
        #: 会停在 `BLOCKED` 上等槽位空出来，**那时它还没有租约、也没有步骤**。
        #: 少了这个标志，「阻塞解除 → 排队 → 按已落地往前推」会让一个
        #: 从未落地的订单去 `revalidate` 一份还没挂上的计划。
        self.adopted = False

    # -- 状态 ----------------------------------------------------------

    def is_terminal(self):
        # type: () -> bool
        return self.status in TERMINAL_STATUSES

    def can_move_to(self, status):
        # type: (str) -> bool
        return status in ORDER_TRANSITIONS.get(self.status, frozenset())

    def move_to(self, status, detail="", tick=0):
        # type: (str, str, int) -> bool
        """走一条状态边。**不在表里的边一律拒绝并返回 False**。

        调用方必须处理 `False`：静默吞掉它会让订单停在一个和代码以为的
        不一样的状态里，而那是最难查的一类偏差。
        """
        if status == self.status:
            return True
        if not self.can_move_to(status):
            return False
        self.status = status
        self.status_detail = detail or ""
        self.updated_tick = int(tick)
        return True

    def mark(self, tick):
        # type: (int) -> None
        self.updated_tick = int(tick)

    # -- 读法 ----------------------------------------------------------

    def step_at(self, index):
        # type: (int) -> object
        for step in self.steps:
            if step.index == index:
                return step
        return None

    def outstanding_step(self):
        # type: () -> object
        """第一个还没完成的步骤（`steps` 已按执行顺序排好）。"""
        for step in self.steps:
            if not step.is_terminal_step():
                return step
        return None

    def active_steps(self):
        # type: () -> list
        return [step for step in self.steps if step.is_runnable()]

    def reservations_of(self, canonical):
        # type: (str) -> dict
        return self.reservations.get(canonical, {})

    def total_reserved(self):
        # type: () -> int
        return sum(row.get("reserved", 0) for row in self.reservations.values())

    def progress(self):
        # type: () -> dict
        """订单进度：**已完成的配方数 / 计划里配方树的总配方数**。

        `total` 是计划展开出来的步骤数（一棵配方树展平之后的节点数），
        `done` 是其中 `COMPLETED` 的个数。**这一步不需要额外记账**——
        步骤的状态就是进度的唯一依据，再维护一个计数器只会多一个会走偏的数。

        几个刻意的口径：

        - `ratio` **只在 `total > 0` 时是除法**；零步骤的 `HOLD` 计划
          （「库里已经够了」）没有分母，直接按订单是不是已完成给 0.0 / 1.0。
          返回 `0/0` 或者 `1` 这种「看起来像百分比」的假值都会让界面
          显示出一个不存在的进度条。
        - `cancelled` / `blocked` 单独计数，**不从 `done` 里扣**：取消的步骤
          没有完成，而「8/10 完成、1 个被取消」与「7/10 完成」在界面上
          是两件不同的事。
        - `ratio` 是给界面用的**瞬时**读数，不参与任何判定——订单结单看的是
          「每一步都 `COMPLETED`」，不是这个比例。
        """
        total = len(self.steps)
        done = 0
        blocked = 0
        cancelled = 0
        by_status = {}
        for step in self.steps:
            by_status[step.status] = by_status.get(step.status, 0) + 1
            if step.status == StepStatus.COMPLETED:
                done += 1
            elif step.status == StepStatus.BLOCKED:
                blocked += 1
            elif step.status == StepStatus.CANCELLED:
                cancelled += 1
        if total <= 0:
            ratio = 1.0 if self.status == OrderStatus.COMPLETED else 0.0
        else:
            ratio = float(done) / total
        outstanding = self.outstanding_step()
        return {
            "steps_total": total,
            "steps_completed": done,
            "steps_blocked": blocked,
            "steps_cancelled": cancelled,
            "outstanding_step": outstanding.index if outstanding is not None else None,
            "by_status": by_status,
            "ratio": ratio,
        }

    def to_dict(self):
        # type: () -> dict
        return {
            "order_id": self.order_id,
            "request_id": self.request_id,
            "component_id": self.component_id,
            "master_instance_id": self.master_instance_id,
            "target": self.canonical,
            "amount": self.amount,
            "status": self.status,
            "status_detail": self.status_detail,
            "paused": self.paused,
            "adopted": self.adopted,
            "plan_digest": self.plan_digest,
            "inventory_version": self.inventory_version,
            "topology_version": self.topology_version,
            "created_tick": self.created_tick,
            "updated_tick": self.updated_tick,
            # **只给规范文本，不给 `ResourceKey` 对象。** 这份视图是要
            # `json.dumps` 出去给 11 与诊断用的（与 08 的计划同一条纪律），
            # 而塞一个活对象进去会让它在序列化那一刻才炸——那时离出错的地方
            # 已经很远了。`key` 内部字段也不透出：那是给 `reserve` 用的。
            "reservations": [
                {
                    "canonical": canonical,
                    "reservation_id": self.reservations[canonical].get(
                        "reservation_id"
                    ),
                    "claimed": self.reservations[canonical].get("claimed", 0),
                    "reserved": self.reservations[canonical].get("reserved", 0),
                }
                for canonical in sorted(self.reservations)
            ],
            "slot_lease": self.slot_lease.to_dict() if self.slot_lease else None,
            "memory_lease": (
                self.memory_lease.to_dict() if self.memory_lease else None
            ),
            "blockers": [reason.to_dict() for reason in self.blockers],
            #: 进度跟着订单一起给，**不另开一个查询入口**：界面（11）与
            #: 只读诊断读的是同一份数，两处各算一次迟早会不一样。
            "progress": self.progress(),
            "steps": [step.to_dict() for step in self.steps],
            "residue": list(self.residue),
            "stats": dict(self.stats),
        }

    def __repr__(self):
        return "Order(%s, %s, %s)" % (self.order_id, self.canonical, self.status)


# -- 被拒绝的请求 ----------------------------------------------------------


class RejectedRequest(object):
    """一条**被拒绝的下单请求**的记录（2026-09-26 规则新增）。

    **它不是订单。** 规则原文：「拒绝下达后保留一条独立的拒绝记录。
    保存目标、数量、拒绝原因、时间和请求 ID；不占内存、并行槽或活跃订单名额，
    不自动重试。」

    ## 为什么必须有这个东西

    规划是**分帧**的，因此「拒绝」是一件**异步**发生的事：玩家点下按钮时，
    命令先返回「规划中」；几帧之后才算出「这份请求不成立」。那时人多半已经
    离开界面了。**只剩一个统计数字等于没有结果**——玩家看不到任何东西，
    而服务端也说不清刚才拒了什么。

    因此这里存的是**可回看**的近期记录，不是计数器。
    """

    __slots__ = ("request_id", "target", "canonical", "amount", "reasons", "tick")

    def __init__(self, request_id, target, canonical, amount, reasons, tick=0):
        # type: (str, str, str, int, tuple, int) -> None
        self.request_id = request_id
        #: 可读名（给界面）。
        self.target = target
        #: 规范文本（给对齐与诊断）。
        self.canonical = canonical
        self.amount = int(amount)
        #: `BlockReason` 序列。**原样保留多个**：一条请求可能同时因为
        #: 「没样板」和「料被别人锁着」被拒，压成一句就等于丢掉一半的
        #: 「接下来该做什么」。
        self.reasons = tuple(reasons or ())
        self.tick = int(tick)

    def codes(self):
        # type: () -> tuple
        return tuple(reason.code for reason in self.reasons)

    def to_dict(self):
        # type: () -> dict
        return {
            "request_id": self.request_id,
            "target": self.target,
            "canonical": self.canonical,
            "amount": self.amount,
            "reasons": [reason.to_dict() for reason in self.reasons],
            "tick": self.tick,
        }

    def __repr__(self):
        return "RejectedRequest(%s, %s x%d)" % (
            self.request_id,
            self.target,
            self.amount,
        )


class RejectedRequestStore(object):
    """近期被拒请求的台账。**每张网络一份**，有数量上限。

    **它不占任何执行资源**：不进 `orders` 台账、不占活跃订单名额、不占内存、
    不占并行槽，也**不会自动重试**。上限到了就淘汰最旧的一条——它是给玩家
    回看的近期记录，不是审计日志。
    """

    __slots__ = ("limit", "_rows", "_order", "stats")

    def __init__(self, limit=64):
        # type: (int) -> None
        self.limit = max(1, int(limit))
        self._rows = {}
        #: 插入顺序。`dict` 在 py2 里没有有序迭代，而「随机淘汰」会让
        #: 「最近被拒的是哪条」变成一个查不出来的问题。
        self._order = []
        self.stats = {"rejected": 0, "evicted": 0}

    def add(self, request_id, target, canonical, amount, reasons, tick=0):
        # type: (str, str, str, int, tuple, int) -> object
        row = RejectedRequest(request_id, target, canonical, amount, reasons, tick)
        self._rows[request_id] = row
        if request_id in self._order:
            self._order.remove(request_id)
        self._order.append(request_id)
        self.stats["rejected"] += 1
        while len(self._order) > self.limit:
            oldest = self._order.pop(0)
            self._rows.pop(oldest, None)
            self.stats["evicted"] += 1
        return row

    def get(self, request_id):
        # type: (str) -> object
        return self._rows.get(request_id)

    def recent(self, limit=None):
        # type: (int) -> tuple
        """最近的若干条，**最新在前**。"""
        ids = list(self._order)
        ids.reverse()
        if limit is not None:
            ids = ids[: int(limit)]
        return tuple(self._rows[key] for key in ids if key in self._rows)

    def clear(self):
        # type: () -> int
        count = len(self._order)
        self._rows = {}
        self._order = []
        return count

    def diagnostics(self):
        # type: () -> dict
        return {
            "count": len(self._order),
            "limit": self.limit,
            "stats": dict(self.stats),
        }


# -- 订单集合 --------------------------------------------------------------


class OrderStore(object):
    """一张网络上的订单台账。

    **逐网络一份**（由 `FactoryService` 持有，而服务槽位是逐运行时各造一套），
    因此两张网络不会共享订单状态——03 验收第 4 条。订单 ID 里带上主控实例，
    所以「这张订单属于哪台主控」在字符串上就能看出来。
    """

    __slots__ = ("component_id", "_orders", "_order", "_seq", "stats")

    def __init__(self, component_id):
        # type: (int) -> None
        self.component_id = component_id
        self._orders = {}
        #: 稳定的遍历顺序 + 轮转游标。
        self._order = []
        self._seq = 0
        self.stats = {
            "created": 0,
            "refused": 0,
            "completed": 0,
            "cancelled": 0,
            "failed": 0,
        }

    def new_id(self, master_instance_id):
        # type: (str) -> str
        """生成订单 ID。

        **形状里带上主控实例**：任务文档要求「订单 ID 与主控实例 ID 稳定关联」，
        这样一张订单属于哪台主控在字符串上就看得出来，12 恢复时也不必
        另存一份对应关系。序号只在本网络内自增。
        """
        self._seq += 1
        return "order@%s/%s/%d" % (
            self.component_id,
            master_instance_id or "nomaster",
            self._seq,
        )

    def add(self, order):
        # type: (object) -> object
        if order.order_id in self._orders:
            return None
        self._orders[order.order_id] = order
        self._order.append(order.order_id)
        self.stats["created"] += 1
        return order

    def get(self, order_id):
        # type: (str) -> object
        return self._orders.get(order_id)

    def remove(self, order_id):
        # type: (str) -> bool
        """**只对终态订单生效。** 移除一个还在跑的订单等于让它的预留
        永远没人释放——预留是记在 05 那侧的，订单对象没了就查不出该放谁。"""
        order = self._orders.get(order_id)
        if order is None or not order.is_terminal():
            return False
        self._orders.pop(order_id, None)
        if order_id in self._order:
            self._order.remove(order_id)
        return True

    def discard(self, order_id):
        # type: (str) -> bool
        """丢掉一条**从未被接受**的请求记录。

        与 `remove` 分开：那个只对**终态订单**生效，理由是「移除一个还在跑的
        订单等于让它的预留永远没人释放」。而这里的对象根本没成为订单——
        它没通过准入，手上一个资源都没有，因此可以无条件丢掉。两条路的
        语义不同，共用一个入口会让「这条能不能丢」变成一个要读调用点
        才能回答的问题。
        """
        if order_id not in self._orders:
            return False
        self._orders.pop(order_id, None)
        if order_id in self._order:
            self._order.remove(order_id)
        return True

    def orders(self):
        # type: () -> tuple
        """按创建顺序。**不用字典的键序**——那会随哈希变化。"""
        return tuple(self._orders[key] for key in self._order if key in self._orders)

    def live(self):
        # type: () -> tuple
        return tuple(order for order in self.orders() if not order.is_terminal())

    def live_count(self):
        # type: () -> int
        return len(self.live())

    def ids(self):
        # type: () -> tuple
        return tuple(self._order)

    def diagnostics(self):
        # type: () -> dict
        counts = {}
        for order in self.orders():
            counts[order.status] = counts.get(order.status, 0) + 1
        return {
            "component_id": self.component_id,
            "orders": len(self._orders),
            "by_status": counts,
            "stats": dict(self.stats),
        }
