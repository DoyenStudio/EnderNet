# coding=utf-8
"""产物回流与分配（任务 10-01 … 10-08）。

**本模块是纯领域**：不认识引擎、一条命令都不发、一帧都不推。它只回答一件事——
「这一份**真的进了网络存储**的资源，该记在哪个等待中的执行实例头上」。

## 两条接缝，一个对象

任务 10 要接的是两条**早就预留好、都没有调用方**的线：

| 接缝 | 在哪一边 | 谁调用 |
| --- | --- | --- |
| `register` / `unregister` | 09 的 `ReturnsSink` 契约 | `OrderExecutor._finish_inputs`（**派发确认之后**） |
| `__call__(request, count, now)` | 06 的 `TransferCoordinator.sink` | `_deliver_external_input`（`commit()` **成功之后**） |

`ReturnsPort` 把两条都实现，**同一个对象**既当 09 的 `returns` 又当 06 的
`reflow_sink`。这样「订单在等什么」与「东西到了该给谁」是同一份索引，
不会长出两份可以对不上的账。

06 的调用时点是要紧的：`_deliver_external_input` 只在提交**成功之后**才发，
所以 `count` 就是**真的进了网络存储**的量——10-02「从真实接受数量进行分配」
在这一点上是结构性成立的，10 不需要再自己去核对一次库存。

## 剩余需求以 09 那一份为准

契约（`plans/technical_notes/order_returns_contract.md` §1）把
「每实例每产物的剩余需求」判给 09 的 `WaitingLedger`，把
「资源键 → 等待执行实例」的索引判给 10。因此本模块**不另存一份剩余需求**，
只持有 09 的 `StepOutput` 对象的引用，按它的 `remaining()` 排序与截断。

再存一份的代价不是「多写几行」，而是**两次计数在多步订单重派、存档恢复时
可以对不上**——症状是订单永远等差，而那种坏查不出来。只读 09 那一份时，
10 的剩余需求恒等于 09 的；唯一可能分叉的方向是 09 先满足了而 10 的索引
还旧一步，那由 `receive_output` 的返回值兜住（见 `_allocate` 里
`accepted < take` 那一段）。

## 跨分量

索引是**全局**的（契约 §1：要跨网络看全部等待者才排得出顺序），但分配
**按分量过滤**：到达网络 N 的资源只可能满足 N 上的订单，跨网络没有转移
通道（06 的协调器只在本网络内搬）。全局那一份用于诊断。

## 归属与计数在同一个受控提交阶段完成（10-03）

分配分两步，顺序不能反：先 `StorageService.reserve(order_id, ...)` 把这份
资源**锁给那张订单**，再 `receive_output(...)` 让它计数。只有计数没有归属
正是 10-03 禁止的形状（「禁止只有历史计数而没有材料归属」）。

预留的拥有者取**订单 ID**，于是 09 自己的 `reservations_of(order_id)` /
`release_owned_by(order_id)` 天然认得它：结单与取消时 09 会把它一并释放，
10 不需要另写一条清理路径（`_complete_if_done` 里的 `_release_everything`
就是那个点）。
"""

from ...common.protocol import ORIGIN_EXTERNAL, ORIGIN_INTERNAL
from ..network.services import ServiceId
from .jobs import _canonical_of

#: 策略字段到**待定依据**的映射。第二项是决策台账编号——
#: 与 `PlanPolicy.UNRESOLVED_FIELDS` 同格式。
UNRESOLVED_FIELDS = (
    ("priority", "R06", "回流分配优先级：同一份资源同时有多个等待者时先记在谁头上"),
    ("starvation", "R06", "防饥饿方式：等久了的执行实例要不要提权"),
)


# -- 策略 ------------------------------------------------------------------


class Priority(object):
    """**R06 的前半条**：同一份资源该先满足谁。**尚未定案。**

    替身取 `FIFO`（谁的步骤先派发出去谁先拿）——它等于「不额外排序」，
    因此不引入任何玩法判断，也不会把某个顺序冒充成规则。
    """

    #: 替身：登记先后。**它就是替身取值**，正式档会被装配检查拦下。
    FIFO = "fifo"
    #: 另一侧：先把一张订单喂饱再开下一张。**尚未实现**（R06）。
    PER_ORDER = "per_order"

    ALL = frozenset([FIFO, PER_ORDER])


class Starvation(object):
    """**R06 的后半条**：防饥饿。**尚未定案。**

    替身取 `NONE`（不设任何提权）。理由不是「省事」：加一条没定案的提权
    规则，会在诊断与代码里看起来像一条**正式规则**，而它其实是谁定的都不知道。
    """

    #: 替身：不设提权，按 `Priority` 的顺序自然推进。
    NONE = "none"
    #: 另一侧：等待越久优先级越高。**尚未实现**（R06）。
    AGING = "aging"

    ALL = frozenset([NONE, AGING])


class ReturnPolicy(object):
    """回流侧的未定案旋钮（R06）。

    与 `BandwidthPolicy` / `PlanPolicy` / `OrderPolicy` 同形：做成具名策略而不是
    常数，因为 R06 还没定案（全局约束 7）。`assert_settled()` **只应在正式
    验收时调用**——开发与离线测试不应调用它。

    **默认值是可用的替身，而不是拒绝。** 这一点与 `EnergyPolicy` 相反、
    与 `BandwidthPolicy` 相同，理由是分配顺序**不授予任何权限、不破坏守恒**：
    它只回答「两份等待里先给谁」，给错了既不会凭空造出资源，也不会让谁拿到
    本不该有的东西。真正的安全阀在别处（预留的全有或全无、09 的剩余需求截断、
    10-03 要求的归属与计数同步）。所以让 DEV 档能真的走完，比让整条链在 R06
    定案之前一步都走不动更有价值。

    `priority` 与 `starvation` 都进 `unresolved_report()`，并由
    `bootstrap.policy_bundle()` 一起交给装配检查；正式档下
    `assert_assembly_ok()` 会因此把它们报出来。
    """

    __slots__ = ("priority", "starvation", "exclusive", "receipt_limit")

    def __init__(
        self,
        priority=Priority.FIFO,
        starvation=Starvation.NONE,
        exclusive=True,
        receipt_limit=4096,
    ):
        # type: (str, str, bool, int) -> None
        if priority not in Priority.ALL:
            raise ValueError(u"未知的回流分配优先级: %r" % (priority,))
        if priority != Priority.FIFO:
            # **在装配期就报错，而不是等到某次分配给出一个「更合理」的顺序。**
            # 替身实现出来的顺序会被当成规则生效（与 `PlanPolicy.candidate_order`
            # 同一处置）。
            raise ValueError(
                u"回流分配优先级 %r 尚未实现：R06 只定了「每份只计一个订单」，"
                u"「先满足谁」还没有定案。目前只能用 %r"
                % (priority, Priority.FIFO)
            )
        if starvation not in Starvation.ALL:
            raise ValueError(u"未知的防饥饿口径: %r" % (starvation,))
        if starvation != Starvation.NONE:
            raise ValueError(
                u"防饥饿口径 %r 尚未实现：提权规则会在「谁先拿」之上再叠一层"
                u"没定案的判断，而它看起来会像一条正式规则。目前只能用 %r"
                % (starvation, Starvation.NONE)
            )
        if not exclusive:
            # **这半条已经定案**，所以它不是 `UNRESOLVED_FIELDS` 里的一项，
            # 而是留成字段（照 `SchedulerPolicy.catch_up` 的先例）：
            # 10 文档「同一份物品只能使一个订单的结果计数增加」。
            raise ValueError(
                u"「同一份资源只让一个订单的结果计数增加」已经定案"
                u"（任务 10 的目标与边界），不能关掉"
            )
        self.priority = priority
        self.starvation = starvation
        #: **已定案**，留成字段给测试与诊断用（不进 `unresolved_report()`）。
        self.exclusive = True
        #: **帧安全阀**，不是玩法数值：回执去重表是唯一一张只增不减的表。
        #: 与 `OrderPolicy.op_ledger_limit` 同一个量级、同一个理由。
        self.receipt_limit = max(1, int(receipt_limit))

    # -- 判定 ----------------------------------------------------------

    def ordered(self, waiting):
        # type: (list) -> list
        """按 R06 给同键的等待者排序。**替身口径就是登记先后**。

        做成策略方法而不是在分配循环里直接 `sorted(key=seq)`：R06 定案时要改的
        正是这一处，而分配循环（扣减本次剩余可分配量、预留、截断后回收）
        不该跟着动。
        """
        if self.priority == Priority.FIFO:
            return sorted(waiting, key=_by_seq)
        # 构造期已经挡掉了另一侧，走到这里说明有人绕过了 `__init__`。
        raise ValueError(u"回流分配优先级 %r 尚未实现（R06）" % (self.priority,))

    # -- 未定案守卫 ----------------------------------------------------

    def unresolved_report(self):
        # type: () -> list
        return [
            {
                "field": "return." + field,
                "rule": rule,
                "value": getattr(self, field, None),
                "note": note,
            }
            for field, rule, note in UNRESOLVED_FIELDS
        ]

    def assert_settled(self):
        # type: () -> None
        """正式验收用：仍有未定项就报错。开发与离线测试不应调用它。"""
        pending = self.unresolved_report()
        if pending:
            raise AssertionError(
                u"回流策略仍有 %d 项未定案（R06），不能作为正式玩法交付：\n  "
                % (len(pending),)
                + "\n  ".join(
                    u"%s（当前值 %r）" % (item["field"], item["value"])
                    for item in pending
                )
            )

    def describe(self):
        # type: () -> dict
        return {
            "priority": self.priority,
            "starvation": self.starvation,
            "exclusive": self.exclusive,
            "receipt_limit": self.receipt_limit,
            "unresolved": self.unresolved_report(),
        }

    def __repr__(self):
        return "ReturnPolicy(%s, %s)" % (self.priority, self.starvation)


def _by_seq(entry):
    # type: (object) -> int
    return entry.seq


def _component_of(runtime):
    # type: (object) -> str
    return getattr(runtime, "component_id", "") or ""


# -- 索引里的一行 ----------------------------------------------------------


class _Waiting(object):
    """「某个执行实例在等某个键」的一条记录。

    **剩余需求不在这里**（见模块 docstring）：`item` 是 09 的 `StepOutput`
    本体，`remaining()` / `allocated()` 都从它读。10 只负责把它排进索引。
    """

    __slots__ = (
        "execution_id",
        "order_id",
        "component_id",
        "key",
        "canonical",
        "item",
        "seq",
    )

    def __init__(self, execution_id, order_id, component_id, key, canonical, item, seq):
        # type: (str, str, str, object, str, object, int) -> None
        self.execution_id = execution_id
        self.order_id = order_id
        self.component_id = component_id
        #: 活的 `ResourceKey`（来自样板）。诊断与兜底用；真正喂给 05/09 的是
        #: 回执里那个键——它才是此刻躺在存储里的那一个。
        self.key = key
        self.canonical = canonical
        self.item = item
        #: 登记序号。R06 的替身口径（FIFO）就按它排。
        self.seq = int(seq)

    def remaining(self):
        # type: () -> int
        return int(self.item.remaining())

    def allocated(self):
        # type: () -> int
        return int(self.item.received)

    def name(self):
        # type: () -> str
        return getattr(self.key, "name", None) or self.canonical

    def to_dict(self):
        # type: () -> dict
        return {
            "execution_id": self.execution_id,
            "order_id": self.order_id,
            "component_id": self.component_id,
            "key": self.canonical,
            "name": self.name(),
            "expected": int(self.item.expected),
            "allocated": self.allocated(),
            "remaining": self.remaining(),
            "counts_for_completion": bool(self.item.counts_for_completion),
            "seq": self.seq,
        }

    def __repr__(self):
        return "_Waiting(%s, %s, %d/%d)" % (
            self.execution_id,
            self.canonical,
            self.allocated(),
            int(self.item.expected),
        )


# -- 服务 ------------------------------------------------------------------


class ReturnsService(object):
    """全局的「资源键 → 等待执行实例」索引与分配器（任务 10 的主体）。

    **全进程一份**（装配层注入，与 08 的规划器同一种共享方式）：索引要跨分量
    看全部等待者才排得出顺序。每个分量各有一个 `ReturnsPort` 把它接到自己的
    执行器与存储上，端口本身几乎不持状态。

    `describe()["available"]` 必须是 `True`——09 靠它区分「接上了」与「还是
    替身」（契约 §8）。它答的是「这一半实现了没有」，**不是**「此刻有没有
    等待者」：没有等待者是正常工作状态，那不该让订单去报 `order_returns_absent`。
    """

    adapter_id = "returns"

    def __init__(self, policy=None):
        # type: (object) -> None
        self.policy = policy if policy is not None else ReturnPolicy()
        #: `canonical -> [_Waiting]`。追加序即 `seq` 序，但真正的顺序由
        #: `ReturnPolicy.ordered()` 决定——不要在这里假设它已排好。
        self._index = {}
        #: `execution_id -> [canonical]`。让注销是 O(等待项) 而不是扫全表：
        #: 取消是每帧都可能发生的路径。
        self._by_instance = {}
        #: `component_id -> ReturnsPort`。端口没有可变状态，缓存住它是为了让
        #: 两条接缝拿到同一个对象、诊断里的身份稳定。
        self._ports = {}
        self._seq = 0
        #: 回执去重表（10-05）。键是 `(component_id, op_id)`：`op_id` 只在
        #: 一张网络里唯一（05 的去重就是按分量各记一份的）。
        self._ops = {}
        self._op_order = []
        self._last_receipt = None
        self.stats = {
            "registered": 0,
            "replaced": 0,
            "unregistered": 0,
            "receipts": 0,
            "allocated": 0,
            "trimmed": 0,
            "orphan": 0,
            "satisfied": 0,
            "duplicate": 0,
            "rejected_origin": 0,
            "no_op_id": 0,
            "unknown": 0,
            "not_ready": 0,
            "reserve_failed": 0,
            "evicted": 0,
        }

    # -- 端口 ----------------------------------------------------------

    def port_for(self, runtime):
        # type: (object) -> ReturnsPort
        """某一台主控/分量的回流端口。同一个分量只会有一个。"""
        component_id = _component_of(runtime)
        port = self._ports.get(component_id)
        if port is None:
            port = ReturnsPort(self, runtime)
            self._ports[component_id] = port
        return port

    def inbox_for(self, runtime):
        # type: (object) -> ReturnsPort
        """`StorageService(reflow_sink=...)` 那一边的入口。

        与 `port_for` **是同一个对象**：两条接缝本来就该是同一份索引，
        分开造两个只会让「订单在等什么」与「东西给了谁」对不上。
        名字分开只是让两个调用点各自读得懂。
        """
        return self.port_for(runtime)

    # -- 09 → 10：登记 / 注销 ------------------------------------------

    def register(self, component_id, execution_id, expectations, order_id=""):
        # type: (str, str, list, str) -> bool
        """登记一个已经**确认派发**的执行实例要等的全部产物（10-01）。

        **替换语义**（契约 §3）：同一 `execution_id` 再登记一次覆盖旧的。
        累加会让剩余需求凭空翻倍，然后永远等不到——重派同一步正是会走这条路。

        `expectations` 是 `StepOutput` 序列，**产物与返还物都在里面**
        （契约 §6.2：10 不需要区分两者，按同一个剩余需求记账）。
        折叠规则与 09 的 `WaitingLedger.register` **逐字一致**（`{canonical: item}`，
        同键后者胜）：两边对同一条 `expectations` 得出不同的键集合，就会出现
        「10 认为在等、09 认为不在等」的等待者。
        """
        if not execution_id:
            return False
        if self._drop(execution_id):
            self.stats["replaced"] += 1
        row = dict((item.canonical, item) for item in (expectations or ()))
        for canonical, item in row.items():
            self._seq += 1
            entry = _Waiting(
                execution_id,
                order_id,
                component_id,
                getattr(item, "key", None),
                canonical,
                item,
                self._seq,
            )
            self._index.setdefault(canonical, []).append(entry)
        self._by_instance[execution_id] = sorted(row.keys())
        self.stats["registered"] += 1
        return True

    def unregister(self, execution_id):
        # type: (str) -> int
        """注销一个执行实例，返回它还有多少**没等到**的件数。

        正数说明有产物还在外面——调用方要把这个数记进 `residue` 报出来，
        而不是让订单看起来干净地结束了（契约 §5）。之后到达的产物会命中
        不到等待者，按契约 §6.3 走普通库存（见 `_arrive` 的 `orphan`）。
        """
        entries = self._entries_of(execution_id)
        if not entries:
            return 0
        pending = sum(entry.remaining() for entry in entries)
        self._drop(execution_id)
        self.stats["unregistered"] += 1
        return pending

    def _drop(self, execution_id):
        # type: (str) -> int
        """移除一个执行实例的全部等待记录，返回移除的条数。"""
        canonicals = self._by_instance.pop(execution_id, None)
        if not canonicals:
            return 0
        removed = 0
        for canonical in canonicals:
            rows = self._index.get(canonical)
            if not rows:
                continue
            kept = [entry for entry in rows if entry.execution_id != execution_id]
            removed += len(rows) - len(kept)
            if kept:
                self._index[canonical] = kept
            else:
                self._index.pop(canonical, None)
        return removed

    def _entries_of(self, execution_id):
        # type: (str) -> list
        out = []
        for canonical in self._by_instance.get(execution_id, ()):
            for entry in self._index.get(canonical, ()):
                if entry.execution_id == execution_id:
                    out.append(entry)
        return out

    # -- 到达（两条接缝共用）-------------------------------------------

    def _arrive(self, port, op_id, key, count, now, origin, gated):
        # type: (object, str, object, int, int, str, bool) -> int
        """一条到达通知。返回**本次实际分配出去**的件数。

        `gated` 决定要不要过来源闸（10-04）。**闸与来源标识是两件事**，
        必须分开：同一个 `ORIGIN_INTERNAL` 会从两条不同的路上过来——
        「缓冲↔网络、介质搬运、补偿、索引重建」不能算新回流，而 07-06 的
        装配产出**算**（10-04 明写「本地装配产出可以进入」）。只看 origin
        是分不开这两者的，所以由**入口**决定，而不是由标识决定。
        """
        count = int(count)
        if count <= 0:
            return 0
        component_id = port.component_id
        canonical = _canonical_of(key)
        self._last_receipt = {
            "op_id": op_id,
            "key": canonical,
            "component_id": component_id,
            "count": count,
            "accepted": 0,
            "origin": origin,
            "at": int(now or 0),
            "outcome": "pending",
        }
        self.stats["receipts"] += 1
        if canonical is None:
            # 与 09 的 `WaitingLedger.observe` 同一条处置：**不去猜**。
            # 猜错的后果是一条真实产物落进 unknown 而订单永远不完成，
            # 日志里什么都看不出来。
            self.stats["unknown"] += 1
            return self._note("unknown")
        if gated and origin != ORIGIN_EXTERNAL:
            # 10-04：模拟、拒收、缓存重建、内部移动、补偿、恢复都不产生新计数。
            # **不记 op_id**：这条回执一件都没分配出去，把它记成「已消费」
            # 只会让一次重试白跑。
            self.stats["rejected_origin"] += 1
            return self._note("rejected_origin")
        dedup_key = None
        if op_id:
            dedup_key = (component_id, op_id)
            if dedup_key in self._ops:
                self.stats["duplicate"] += 1
                return self._note("duplicate")
        else:
            # 没有 op_id 就没法去重（10-05 要求它非空）。**仍然分配**——
            # 丢掉一条真实入库会让订单永远等差，而那是更难查的一种坏；
            # 但要计数，让「谁没用 op_id」在诊断里看得见（与 09 同一处置）。
            self.stats["no_op_id"] += 1
        storage, executor = port.resolve()
        if storage is None or executor is None:
            # **不抛错**：`__call__` 是在 06 的提交路径上被调的，抛出去会把
            # 一次**成功**的入库变成一次异常（`_deliver_external_input` 不捕它）。
            self.stats["not_ready"] += 1
            return self._note("not_ready")
        waiting = [
            entry
            for entry in self._index.get(canonical, ())
            if entry.component_id == component_id and entry.remaining() > 0
        ]
        if not waiting:
            # 契约 §6.3：迟到产物（等待记录已注销）与「没人等这个键」都
            # **不报错**——报错会让重试逻辑把同一条回执反复送进来。
            # 资源就留在网络存储里当普通库存（10-06 末句「不丢弃」）。
            outcome = "orphan" if not self._index.get(canonical) else "satisfied"
            self.stats[outcome] += 1
            return self._note(outcome)
        allocated = self._allocate(storage, executor, waiting, key, count, op_id, now)
        if allocated > 0 and dedup_key is not None:
            # **只记真的分配过的回执。** 孤儿、预留失败、执行器没接上都
            # 一件没分出去，记成「已消费」会让重试白跑，而重试恰恰是
            # 契约 §6.3 要求必须有接收路径的那种情形。
            self._remember(dedup_key, allocated)
        return self._note("allocated" if allocated > 0 else "unneeded", allocated)

    def _allocate(self, storage, executor, waiting, key, count, op_id, now):
        # type: (object, object, list, object, int, str, int) -> int
        """按 R06 的有序贪心把 `count` 分下去，返回实际分配量。

        **每分配一部分都扣减本次剩余可分配量**（10-02）：`left` 是这一次回执
        自己的余额，不是「这两个等待者还剩多少」。少了它，「A、B 各需 8 个、
        输入 10 个」就会给两边各记 8 个——那是验收 A 明令禁止的形状。
        """
        left = int(count)
        allocated = 0
        for entry in self.policy.ordered(waiting):
            if left <= 0:
                break
            take = min(left, entry.remaining())
            if take <= 0:
                continue
            # 10-03：**先锁给这张订单，再计数。** 顺序反了就会出现
            # 「历史计数有了、材料归属没有」——那正是这一条禁止的形状。
            reservation = storage.reserve(entry.order_id, key, take, now)
            if reservation is None:
                # **停手，不跳过去满足后面的实例。** R06 问的就是「先满足谁」，
                # 跳过一位等于换了一条规则——而那正是还没定案的那部分。
                # 余量留在网络里当普通库存（10-06「不丢弃」）。
                self.stats["reserve_failed"] += 1
                break
            accepted = int(
                executor.receive_output(entry.execution_id, key, take, op_id, entry.name())
                or 0
            )
            if accepted < take:
                # 09 按**它的**剩余需求截断，而我们的索引可能比它旧一步
                # （比如取过一次货、或这条回执被重放过）。多留的那一份
                # 必须当场还回去，否则归属就多了一块。
                storage.release(reservation.reservation_id, take - accepted)
                self.stats["trimmed"] += take - accepted
            if accepted <= 0:
                # 这一位其实不要了。**不扣 `left`**，让后面的人有机会拿到。
                continue
            allocated += accepted
            left -= accepted
            self.stats["allocated"] += accepted
        return allocated

    def _remember(self, dedup_key, accepted):
        # type: (tuple, int) -> None
        """记住一条真的分配过的回执。**表必须有上界**：它是唯一一张只增不减的表。"""
        if dedup_key in self._ops:
            return
        self._ops[dedup_key] = accepted
        self._op_order.append(dedup_key)
        while len(self._op_order) > self.policy.receipt_limit:
            oldest = self._op_order.pop(0)
            self._ops.pop(oldest, None)
            self.stats["evicted"] += 1

    def _note(self, outcome, accepted=0):
        # type: (str, int) -> int
        if self._last_receipt is not None:
            self._last_receipt["outcome"] = outcome
            self._last_receipt["accepted"] = int(accepted)
        return int(accepted)

    # -- 读法 ----------------------------------------------------------

    def _entries(self, component_id=None):
        # type: (str) -> list
        out = []
        for canonical in sorted(self._index.keys()):
            for entry in self._index[canonical]:
                if component_id is None or entry.component_id == component_id:
                    out.append(entry)
        return sorted(out, key=_by_seq)

    def describe(self, component_id=None):
        # type: (str) -> dict
        """摘要诊断。09 读 `available`，装配层读其余部分。

        `available` **恒为真**——见类 docstring。`waiting_instances` 为 0
        是正常的（此刻没人等产物），不是「没接上」。
        """
        entries = self._entries(component_id)
        out = {
            "adapter_id": self.adapter_id,
            "available": True,
            "policy": self.policy.describe(),
            "components": sorted(self._ports.keys()),
            "waiting_instances": len(set(entry.execution_id for entry in entries)),
            "waiting_entries": len(entries),
            "waiting_by_key": self._count_by_key(entries),
            "stats": dict(self.stats),
            "last_receipt": (
                dict(self._last_receipt) if self._last_receipt is not None else None
            ),
        }
        if component_id is not None:
            port = self._ports.get(component_id)
            out["component_id"] = component_id
            #: 两条接缝是不是都真的够得着执行器与存储。**这是「接线对不对」
            #: 而不是「有没有等待者」**，所以它出现在逐分量的视图里。
            out["wired"] = bool(port is not None and all(port.resolve()))
        return out

    def _count_by_key(self, entries):
        # type: (list) -> dict
        out = {}
        for entry in entries:
            row = out.setdefault(entry.canonical, {"entries": 0, "remaining": 0})
            row["entries"] += 1
            row["remaining"] += entry.remaining()
        return out

    def view(self, component_id=None, canonical=None, execution_id=None):
        # type: (str, str, str) -> dict
        """逐条的**只读**明细（10-08：预期、已分配、剩余、归属、最后回执）。"""
        entries = self._entries(component_id)
        if canonical is not None:
            entries = [entry for entry in entries if entry.canonical == canonical]
        if execution_id is not None:
            entries = [entry for entry in entries if entry.execution_id == execution_id]
        return {
            "entries": [entry.to_dict() for entry in entries],
            "summary": self.describe(component_id),
        }

    def diagnostics(self):
        # type: () -> dict
        return self.describe()


# -- 端口 ------------------------------------------------------------------


class ReturnsPort(object):
    """某一台主控/分量上的回流端口：**两条接缝的同一个对象**。

    09 把它当 `ReturnsSink`（`register` / `unregister` / `describe` / `blocker`），
    06 把它当 `reflow_sink`（可调用，`(request, count, now)`）。装配层两边
    注入的是同一个实例，因此「订单在等什么」与「东西给了谁」是同一份索引。

    **它几乎不持状态**：索引、去重表、统计都在 `ReturnsService` 上
    （那份要跨分量看）。端口只记住自己属于哪个分量，以及惰性解析出来的
    存储与执行器。

    **不继承 `ReturnsSink`**：契约明写那是鸭子类型（`executor.py` 的
    `ReturnsSink` docstring），而这样 `executor.py` 与 `returns.py` 互不
    import——两个模块之间没有任何环。
    """

    adapter_id = "returns"

    def __init__(self, service, runtime):
        # type: (object, object) -> None
        self.service = service
        self.runtime = runtime
        self.component_id = _component_of(runtime)

    # -- 09 的 ReturnsSink 契约 ----------------------------------------

    def register(self, execution_id, expectations, order_id=""):
        # type: (str, list, str) -> bool
        """09 在**派发确认之后**登记（10-01）。恒返回 `True`——接上了就是接上了。"""
        return self.service.register(
            self.component_id, execution_id, expectations, order_id
        )

    def unregister(self, execution_id):
        # type: (str) -> int
        """09 在步骤完成或订单取消时注销（10-07），返回还没等到的件数。"""
        return self.service.unregister(execution_id)

    def blocker(self):
        # type: () -> None
        """接上了就没有要报的阻塞。

        **这个方法必须存在**：09 的 `_sink_blocker` 只在 `available` 为假时
        才来找它，但它一旦缺失，09 会退回「产物回流入口尚未接线（任务 10）」
        那条兜底说明——而接上之后那句话是假话。这里明确回答「没有阻塞」。
        """
        return None

    def describe(self):
        # type: () -> dict
        return self.service.describe(self.component_id)

    def view(self, canonical=None, execution_id=None):
        # type: (str, str) -> dict
        """逐条明细（10-08）。**只读**：`debug_query("returns")` 走这里。"""
        return self.service.view(self.component_id, canonical, execution_id)

    # -- 06 的合格回流入口 ---------------------------------------------

    def __call__(self, request, count, now=0):
        # type: (object, int, int) -> int
        """`TransferCoordinator.sink(request, count, now)`。

        `count` 是**实际到达量**：06 只在 `commit()` 成功之后才发这条通知，
        因此它天然满足 10-02「从真实接受数量进行分配」。
        """
        return self.service._arrive(
            self,
            getattr(request, "op_id", ""),
            getattr(request, "resource_key", None),
            count,
            now,
            getattr(request, "origin", None),
            True,
        )

    def accept_assembled(self, op_id, key, count, now=0):
        # type: (str, object, int, int) -> int
        """**07-06 装配产出的具名接缝。** 10-04 明写「本地装配产出可以进入」。

        生产调用方为 `assembly.AssemblyChamberExecution`：06 已将输出槽的
        实物存入网络后，才以唯一转移 op_id 和实际入库量调用这里。

        走这条入口**不过来源闸**：装配产出是本模组自己搬的，来源标识就是
        `ORIGIN_INTERNAL`，而那个标识同时也是「补偿、缓存重建、内部移动」的
        标识——只看标识分不开这两者（见 `_arrive` 对 `gated` 的说明）。
        """
        return self.service._arrive(
            self, op_id, key, count, now, ORIGIN_INTERNAL, False
        )

    # -- 惰性解析 ------------------------------------------------------

    def resolve(self):
        # type: () -> tuple
        """取本分量的（存储服务, 执行器）。**名字对不上就当没接上。**

        刻意**不抛错**：`__call__` 是在 06 的提交路径上被调的，抛出去会把
        一次**成功**的入库变成一次异常（`_deliver_external_input` 不捕它），
        而那条路走不到的话，症状是「外部输入通知丢了」——比「订单等差」
        更难查。够不着就计数（`stats["not_ready"]`），让诊断说得出来。
        """
        runtime = self.runtime
        if runtime is None:
            return None, None
        services = getattr(runtime, "services", None)
        if services is None:
            return None, None
        storage = services.get(ServiceId.STORAGE)
        factory = services.get(ServiceId.FACTORY)
        executor = getattr(factory, "executor", None)
        if not callable(getattr(storage, "reserve", None)):
            # 替身服务没有 `reserve`——那是「这半边是替身」，不是「没接上 10」。
            storage = None
        if not callable(getattr(executor, "receive_output", None)):
            executor = None
        return storage, executor

    def __repr__(self):
        return "ReturnsPort(%s)" % (self.component_id,)
