# coding=utf-8
"""逻辑时钟、到期队列与任务代次（任务 04-01 / 04-02）。

纯领域原语：不 import 任何引擎模块，也不依赖 `server/` 下的其他层。
`server/network/ticking.py` 的调度服务建立在这上面。

**这里没有「每秒多少次」。** 逻辑时钟只是一个整数帧序：引擎每帧调用一次
`ServerBootstrap.drive_once()`，时钟就前进一步。频率留给真机实测——
`LogicalClock.describe()` 给出脚本 tick、模拟 tick 与方块实体 tick
三种脉冲各自的**计数**供人算比值，代码里不出现任何换算。
`OnScriptTickServer`（实测约 30 Hz）与 `OnSimTickServerEvent`
（实测约 17.3 Hz）是**两个不同的时钟**，写死任何一个都会在另一个上出错。

**到期队列的三条纪律：**

1. 堆元组是 `(due, sequence, key)`，`sequence` 由队列自己单调分配，
   因此两个同刻到期的任务也永远可比较。**绝不能把 `NodeKey` / `DeviceRef`
   放进堆元组的前两位**：它们没有定义 `__lt__`，并列时在 Python 3 下
   直接 `TypeError`——而这个错误只在「恰好同刻到期」时才出现。
2. 重排与取消走**懒删除**：`drop()` 只从活体表里拿走，堆里的旧条目留到
   弹出时按「活体表里还是它、且 `due` 与 `sequence` 都对得上」校验后丢弃。
   同一 `(ref, kind)` 在活体表里**有且只有一条**，所以「同一任务重排
   不会重复执行」是结构性的——不要求调用方记得先 `cancel`。
3. 任务失效由**代次**决定，不靠调用方自觉。`TaskKey` 带 `DeviceRef` 的
   代次：同一坐标上旧方块的任务与新方块的任务是两个键，永远不会互相顶替
   （`common/identity.py` 的「旧方块的到期任务不能在新方块上执行」）。
   执行时还有第三道检查：注册表按代次回查（见 `network/ticking.py`）。

**「空」的语义沿用仓库口径**：`peek_due()` 返回 `None` = 队列里没有活体
任务（不是「到期时刻是 0」）；`ScheduledTask.period is None` = 一次性任务；
`DeviceLane.poll_handler is None` = 没有消费者注册轮询回调（轮询本身照跑，
并计入指标，不是「轮询无效」）。
"""

import heapq

from ...common.identity import DeviceRef  # noqa: F401（供 # type: 注释与 matches_ref）

try:  # pragma: no cover - 仅在 Python 3 下成立
    from time import perf_counter as _perf_counter
except ImportError:  # pragma: no cover - 仅在 Python 2 下成立
    _perf_counter = None


def default_timer():
    # type: () -> float
    """默认计时器。**只用于诊断**，不参与任何调度判定。

    `timer=None`（默认）**完全不计时**，因此这个函数只在有人显式要计时
    时才被调用。

    **所有 import 都写在函数体内，顶层一个都不留。** 网易 ModSDK 的沙箱
    里**没有 `timeit`**（真机实测：顶层 `import timeit` 会一路炸到
    `modMain`，整个模组起不来），而 `py2check` 只查语法、查不出「模块不存在」。
    这类依赖必须能失败在调用点，不能失败在导入链上。

    取序：py3 用 `perf_counter`（单调）；py2 优先 `timeit.default_timer`
    （存在时是单调的），没有就退回 `time.time()`——它是墙钟、可能回拨，
    但这里只用于诊断，而**宁可有误差也不能让模组起不来**。
    """
    if _perf_counter is not None:
        return _perf_counter()
    try:
        import timeit

        return timeit.default_timer()
    except ImportError:  # pragma: no cover - 网易沙箱走这条
        import time

        return time.time()


class TaskKind(object):
    """本层自带的任务种类。

    消费者（05/06/08/09）注册自己的任务时，kind 建议带上自己的服务前缀
    （如 `"storage:io"`），避免和别的服务撞在同一个 `(ref, kind)` 上——
    `TaskKey` 的相等性由 kind 参与，撞了就等于互相顶替。
    """

    #: 低频退避轮询（04-02）。每个 lane 一个，由调度器自己拥有。
    POLL = "scheduler:poll"


class PollOutcome(object):
    """轮询回调的返回值。

    handler 返回 `(changed, cost)` 元组或 `None`（等价于「没变化、零成本」）。
    用一个具名类而不是裸元组，是因为两个布尔/整数紧挨着很容易写反，
    而写反的表现是「退避永不拉长」或「预算永不消耗」，都不容易当场看出来。
    """

    __slots__ = ("changed", "cost")

    def __init__(self, changed=False, cost=0):
        # type: (bool, int) -> None
        self.changed = bool(changed)
        self.cost = int(cost)

    @classmethod
    def of(cls, value):
        # type: (object) -> PollOutcome
        """把 handler 的返回值归一化。`None` → 没变化、零成本。"""
        if value is None:
            return cls(False, 0)
        if isinstance(value, PollOutcome):
            return value
        changed, cost = value
        return cls(changed, cost)

    def __repr__(self):
        return "PollOutcome(changed=%r, cost=%r)" % (self.changed, self.cost)


# -- 逻辑时钟 --------------------------------------------------------------


class LogicalClock(object):
    """全进程唯一的逻辑时钟：**一个整数帧序**。

    它不是墙钟，单位也不是秒。`advance()` 只加，`current_tick()` 只读，
    两者都不做任何频率换算——换算需要真机实测，而实测结果要写进交付
    说明，不是写进这里。

    **它不记引擎脉冲。** 04-01 要量「方块实体 tick / 脚本 tick」的比值，
    但那份计数归事件适配器（`ServerEventBinder.counts`），不在这里另开一份：
    曾经这里有一个 `pulses` 字典，而适配器只更新了自己那份，于是
    `pulses["block_entity_tick"]` 在真机上永远是 0——**同一个数字存两处，
    分叉只是时间问题**。现在这里只有帧序。
    """

    __slots__ = ("_tick", "advances")

    def __init__(self, start=0):
        # type: (int) -> None
        self._tick = int(start)
        #: `advance()` 被调用的次数。与 `current_tick()` 的差值是
        #: 「位移总量」，单列出来是因为「时钟走了几帧」与「被推进了几次」
        #: 在诊断里是两个不同的问题（一次 `advance(100)` 是 1 次推进、
        #: 100 帧位移）。
        self.advances = 0

    def current_tick(self):
        # type: () -> int
        """当前帧序。**权威时钟**，业务时间的唯一依据。

        与 `NormalizedEvent.tick` 的区别：那一个只是入队时刻的戳，
        受同帧回调顺序影响（差 1 帧），注释里已写明它不是权威时钟。
        """
        return self._tick

    def advance(self, count=1):
        # type: (int) -> int
        """前进 `count` 帧，返回新的帧序。

        只允许向前。倒退会让已经排定的到期任务变成「过去的时间」，
        而队列对过去的处理是立刻全部执行——一次时钟倒退就能造成
        一整批任务同时触发。
        """
        count = int(count)
        if count < 0:
            raise ValueError("逻辑时钟不能倒退: %r" % (count,))
        self._tick += count
        self.advances += 1
        return self._tick

    def describe(self):
        # type: () -> dict
        return {
            "tick": self._tick,
            "advances": self.advances,
        }

    def __repr__(self):
        return "LogicalClock(tick=%d)" % (self._tick,)


# -- 任务身份 --------------------------------------------------------------


class TaskKey(object):
    """一个到期任务的稳定身份 `(位置, 代次, 任务种类)`。

    三个部分都参与身份，各自的理由不同：位置定位设备；代次让「同一坐标上
    先后两个方块」的任务互不顶替；种类让同一台设备的多种任务各自独立
    （05 的 IO 与 06 的自动输入输出不该互相取消）。
    """

    __slots__ = ("node_key", "generation", "kind")

    def __init__(self, node_key, generation, kind):
        # type: (object, int, str) -> None
        if not kind:
            raise ValueError("任务种类不能为空")
        self.node_key = node_key
        self.generation = int(generation)
        self.kind = kind

    @classmethod
    def of(cls, ref, kind):
        # type: (object, str) -> TaskKey
        """由 `DeviceRef` 造键。这是调用方唯一该用的构造方式。"""
        return cls(ref.node_key, ref.generation, kind)

    def as_key(self):
        # type: () -> tuple
        """字典键。嵌套元组而非 `repr`——`repr` 会在坐标改动后悄悄失效。"""
        return (self.node_key.as_key(), self.generation, self.kind)

    def matches_ref(self, ref):
        # type: (object) -> bool
        """与 `DeviceRef.matches` **同一判据**——不在这里重写一遍。

        重写一份的代价是它会在 `DeviceRef` 的规则演进（比如将来加入实例 id）
        时被落下，而症状是「旧方块的到期任务跑到了新方块上」。
        """
        return DeviceRef(self.node_key, self.generation).matches(ref)

    def to_dict(self):
        # type: () -> dict
        return {
            "node_key": self.node_key.to_dict(),
            "generation": self.generation,
            "kind": self.kind,
        }

    def __eq__(self, other):
        if not isinstance(other, TaskKey):
            return NotImplemented
        return self.as_key() == other.as_key()

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return hash(self.as_key())

    def __repr__(self):
        return "TaskKey(%s, gen=%d, %s)" % (self.node_key, self.generation, self.kind)


class ScheduledTask(object):
    """一个到期任务。

    **任务本身没有身份，`key` 才是身份。** 重排是「换掉活体表里的那一条」，
    不是「再加一条然后靠调用方记住旧的」——后者必然漏，而且漏了以后
    表现为「一次重排变成了两次执行」，很难从现象倒推回原因。

    `due` 与 `period` 的单位都是**逻辑帧**，不是秒（见模块 docstring）。
    """

    __slots__ = (
        "key",
        "ref",
        "handler",
        "owner",
        "due",
        "period",
        "sequence",
        "runs",
        "deferrals",
        "created_tick",
        "last_run_tick",
        "last_cost",
    )

    def __init__(
        self, key, ref, due, period=None, handler=None, owner=None, created_tick=0
    ):
        # type: (TaskKey, object, int, object, object, str, int) -> None
        self.key = key
        #: 排定时的设备实例。执行前会拿它按代次回查注册表。
        self.ref = ref
        self.due = int(due)
        #: 周期（帧）。`None` = 一次性任务。
        self.period = None if period is None else int(period)
        #: 到期时调用。签名 `handler(task) -> PollOutcome | (changed, cost) | None`。
        self.handler = handler
        #: 注册方（服务 id）。空表示调度器自己的任务。
        self.owner = owner or ""
        #: 由 `DueQueue` 分配，用于堆内定序。**不要手工设置**。
        self.sequence = -1
        self.runs = 0
        #: 被跳过的次数。**精确值**（与帧级的估计值相对），
        #: 「卡顿期间积压了多少」看它，不要看 `deferred_now`。
        self.deferrals = 0
        #: 排定时的帧，**不是到期帧**。两者的差就是「这个任务等了多久」，
        #: 拿去算积压诊断时方向不能反。
        self.created_tick = int(created_tick)
        self.last_run_tick = None
        self.last_cost = 0

    @property
    def kind(self):
        # type: () -> str
        return self.key.kind

    @property
    def node_key(self):
        # type: () -> object
        return self.key.node_key

    def is_periodic(self):
        # type: () -> bool
        return self.period is not None

    def to_dict(self):
        # type: () -> dict
        return {
            "key": self.key.to_dict(),
            "due": self.due,
            "period": self.period,
            "owner": self.owner,
            "runs": self.runs,
            "deferrals": self.deferrals,
            "created_tick": self.created_tick,
            "last_run_tick": self.last_run_tick,
            "last_cost": self.last_cost,
        }

    def __repr__(self):
        return "ScheduledTask(%r, due=%d, period=%r, runs=%d)" % (
            self.key,
            self.due,
            self.period,
            self.runs,
        )


# -- 到期队列 --------------------------------------------------------------


class DueQueue(object):
    """按 `due` 排序的任务堆，带懒删除。见模块 docstring 的三条纪律。"""

    __slots__ = ("_heap", "_tasks", "_sequence")

    def __init__(self):
        # type: () -> None
        #: `[(due, sequence, key_tuple)]`
        self._heap = []
        #: `key_tuple -> ScheduledTask`，**活体表**。长度就是队列长度。
        self._tasks = {}
        self._sequence = 0

    def __len__(self):
        return len(self._tasks)

    def __contains__(self, key):
        return key.as_key() in self._tasks

    # -- 读写 ----------------------------------------------------------

    def put(self, task):
        # type: (ScheduledTask) -> ScheduledTask
        """入队或**覆盖**同键的旧条目。

        覆盖时旧条目不在堆里删——它成了墓碑，等弹到它时被跳过。
        堆只增不减是有意的：删除堆中间的元素要多一份索引，
        而墓碑的数量受「重排次数」约束，不受时间约束。
        """
        task.sequence = self._sequence
        self._sequence += 1
        key_tuple = task.key.as_key()
        self._tasks[key_tuple] = task
        heapq.heappush(self._heap, (task.due, task.sequence, key_tuple))
        return task

    def drop(self, key):
        # type: (TaskKey) -> bool
        """取消。返回是否真的拿掉了一条活体。"""
        return self._tasks.pop(key.as_key(), None) is not None

    def get(self, key):
        # type: (TaskKey) -> ScheduledTask
        """取活体任务；没有返回 `None`。"""
        return self._tasks.get(key.as_key())

    def _discard_dead(self):
        # type: () -> int
        """丢弃堆顶的墓碑，返回丢弃条数。

        一条堆顶条目是活体，当且仅当活体表里还是**同一个对象**
        （`sequence` 相同）且它的 `due` 没被改过。只比 `sequence`
        不够：周期任务执行后 `due` 会前移，而堆里还留着旧 due 的那一条。
        """
        dropped = 0
        heap = self._heap
        while heap:
            due, sequence, key_tuple = heap[0]
            live = self._tasks.get(key_tuple)
            if live is not None and live.sequence == sequence and live.due == due:
                return dropped
            heapq.heappop(heap)
            dropped += 1
        return dropped

    def peek_due(self):
        # type: () -> object
        """最早的到期时刻；`None` = 队列里没有活体任务。"""
        self._discard_dead()
        if not self._heap:
            return None
        return self._heap[0][0]

    def pop_due(self, now):
        # type: (int) -> ScheduledTask
        """取出一个 `due <= now` 的活体任务；没有返回 `None`。

        内部循环而不是「弹一次、不对就放弃」：墓碑只能靠弹出来清除，
        放弃就等于把后面的活体任务一起堵死。
        """
        while True:
            self._discard_dead()
            if not self._heap or self._heap[0][0] > now:
                return None
            due, sequence, key_tuple = heapq.heappop(self._heap)
            task = self._tasks.pop(key_tuple, None)
            if task is not None and task.sequence == sequence and task.due == due:
                return task

    # -- 查询 ----------------------------------------------------------

    def drop_where(self, predicate):
        # type: (object) -> int
        """按判据批量取消，返回取消条数。判据收到活体任务。

        **不排序、不构造额外对象**：这是设备替换/移除的热路径
        （区块卸载会对每个成员调一次），`tasks()` 那一版会整表排序，
        把一次卸载变成 O(N·Q log Q)。这里只做一趟 O(Q) 的遍历。
        """
        removed = 0
        # 先收集再删：直接在遍历中删字典会 RuntimeError。
        doomed = []
        for key_tuple in self._tasks:
            if predicate(self._tasks[key_tuple]):
                doomed.append(key_tuple)
        for key_tuple in doomed:
            del self._tasks[key_tuple]
            removed += 1
        return removed

    def count_due(self, now, limit):
        # type: (int, int) -> tuple
        """估算已到期、还没执行的任务数。返回 `(数量, 是否被上限截断)`。

        **这是估计值。** 它扫活体表，不重演真实执行顺序（真实顺序受
        预算与服务调用次序影响），因此「下一次 tick 能执行几条」与它
        无关。要精确的积压量请看每个任务的 `deferrals`。
        两个值都给出来，是为了让读者不会把估计当精确。
        """
        limit = int(limit)
        if limit <= 0:
            return 0, False
        count = 0
        for task in self._tasks.values():
            if task.due <= now:
                count += 1
                if count > limit:
                    # 只有**真的多出来一条**才算被截断。恰好等于上限时
                    # 报「积压更多」是虚报——按这个标志下结论会得出错误的
                    # 积压判断。
                    return limit, True
        return count, False

    def keys(self):
        # type: () -> list
        return sorted(self._tasks)

    def tasks(self):
        # type: () -> list
        """活体任务，按 `(due, sequence)` 排序——稳定的可复现顺序。"""
        return sorted(
            self._tasks.values(),
            key=lambda task: (task.due, task.sequence),
        )

    def stats(self):
        # type: () -> dict
        return {
            "queued": len(self._tasks),
            #: 堆里未被清理的墓碑条数。持续增长说明重排/取消很频繁。
            "tombstones": len(self._heap) - len(self._tasks),
        }


# -- 退避轮询 --------------------------------------------------------------


class PollBackoff(object):
    """外部无通知容器的低频退避轮询（04-02）。

    一直没变化就把间隔逐步拉长到 `max_ticks`；一旦有变化（或收到唤醒
    信号）就回到初值——「还在动的容器值得继续低频探测，已经静止的容器
    不该每帧问一次」。

    **整数运算，不用 `**`。** 两个理由：Python 2 的 `int ** int` 在大指数
    下会变成 `long`；而 `//` 对负数的取整方向在 py2 与 py3 上相反。
    本项目一律绕开这两类写法。循环里的乘法带提前退出，`steps` 再大
    也不会溢出。
    """

    __slots__ = ("base_ticks", "max_ticks", "factor", "_steps", "unchanged", "changed")

    def __init__(self, base_ticks, max_ticks, factor=2):
        # type: (int, int, int) -> None
        base_ticks = int(base_ticks)
        max_ticks = int(max_ticks)
        factor = int(factor)
        if base_ticks < 1:
            raise ValueError("退避初值至少 1 帧: %r" % (base_ticks,))
        if max_ticks < base_ticks:
            raise ValueError(
                "退避上限不能小于初值: %r < %r" % (max_ticks, base_ticks)
            )
        if factor < 2:
            raise ValueError("退避倍数至少为 2: %r" % (factor,))
        self.base_ticks = base_ticks
        self.max_ticks = max_ticks
        self.factor = factor
        self._steps = 0
        #: 诊断计数：连续「没变化」拉长了几次、被「有变化」打断了几次。
        self.unchanged = 0
        self.changed = 0

    def delay(self):
        # type: () -> int
        """当前应该等待的间隔（帧）。"""
        delay = self.base_ticks
        remaining = self._steps
        while remaining > 0:
            delay = delay * self.factor
            if delay >= self.max_ticks:
                return self.max_ticks
            remaining -= 1
        return delay

    def note_unchanged(self):
        # type: () -> int
        """这一轮没发现变化：加一档，返回新的间隔。"""
        self.unchanged += 1
        self._steps += 1
        return self.delay()

    def note_changed(self):
        # type: () -> int
        """发现变化 / 收到唤醒：回到初值，返回新的间隔。"""
        self.changed += 1
        self._steps = 0
        return self.delay()

    def steps(self):
        # type: () -> int
        """当前退避档位。0 = 初值。"""
        return self._steps

    def describe(self):
        # type: () -> dict
        return {
            "base_ticks": self.base_ticks,
            "max_ticks": self.max_ticks,
            "factor": self.factor,
            "steps": self._steps,
            "delay": self.delay(),
            "unchanged": self.unchanged,
            "changed": self.changed,
        }

    def __repr__(self):
        return "PollBackoff(%d->%d, steps=%d)" % (
            self.base_ticks,
            self.max_ticks,
            self._steps,
        )


class DeviceLane(object):
    """一台设备在调度器里的那条道。

    一个 lane 只承载**一台**设备（一个 `DeviceRef`）的调度状态：它的
    轮询计时器与退避档位。设备被替换时整条 lane 跟着换 ref，旧任务
    由调用方 `cancel_all` 清掉——lane 不是「这个位置的历史」，位置的历史
    属于注册表。
    """

    __slots__ = (
        "node_key",
        "ref",
        "device_id",
        "poll_due",
        "backoff",
        "poll_handler",
        "poll_owner",
        "wakes",
        "polls",
    )

    def __init__(self, ref, device_id, backoff):
        # type: (object, str, PollBackoff) -> None
        self.node_key = ref.node_key
        self.ref = ref
        self.device_id = device_id
        #: 下次轮询的逻辑帧；`None` = 还没排定。
        #:
        #: **lane 不持有「是否加载」**：那是注册表的 `record.loaded`，
        #: 而区块卸载的事件路径只到注册表为止，lane 这份拷贝永远不会被
        #: 更新——它会一直显示 true，让诊断撒谎（曾经如此）。
        self.poll_due = None
        self.backoff = backoff
        #: 轮询回调。`None` = **本设备没有消费者**，轮询照跑并计入指标，
        #: 不是「轮询无效」。
        self.poll_handler = None
        #: 注册轮询回调的服务的 id。
        self.poll_owner = ""
        #: 诊断：被唤醒了几次、轮询执行了几次。
        self.wakes = 0
        self.polls = 0

    def has_consumer(self):
        # type: () -> bool
        return self.poll_handler is not None

    def to_dict(self):
        # type: () -> dict
        """**不含「是否加载」**：那份状态在注册表里，由调用方补齐
        （见 `SchedulerService.lane_view()`）。这里再存一份只会不同步。
        """
        return {
            "ref": self.ref.as_key(),
            "device_id": self.device_id,
            "poll_due": self.poll_due,
            "poll_owner": self.poll_owner,
            "has_consumer": self.has_consumer(),
            "wakes": self.wakes,
            "polls": self.polls,
            "backoff": self.backoff.describe(),
        }

    def __repr__(self):
        return "DeviceLane(%r, %s)" % (self.ref, self.device_id)


# -- 策略 ------------------------------------------------------------------

#: 策略字段到**待定依据**的映射。第二项可以是决策台账的规则编号
#: （`R08` 之类），也可以是任务编号（`04-02` 之类）——哪个是真正待定的
#: 那一份，就写哪一个，不要为了「格式统一」去凑一个不相干的规则号。
#:
#: 每一项都是配置点，不是已确认玩法。
UNRESOLVED_FIELDS = (
    ("backoff_max_ticks", "04-02", "退避上限（帧）——它同时是冻结任务的复查节奏"),
    ("backoff_base_ticks", "04-02", "退避初值（帧）"),
    ("backoff_factor", "04-02", "退避倍数"),
    ("deferral_scan_limit", "04-03", "帧级延期估计的扫描上限"),
    ("max_tasks_per_frame", "04-03", "单帧最多执行多少个到期任务"),
    ("wake_on_rebroadcast", "04-02", "同一节点重复广播是否重置倒计时"),
    ("invalidate_on_unload", "01/04", "区块卸载时任务冻结还是作废"),
    ("rotation_max_passes", "04-03", "每帧轮转的轮数"),
    ("min_share_floor", "04-03", "每个网络每帧的保底额度"),
)


class SchedulerPolicy(object):
    """调度策略的配置点。

    默认值全部是**开发测试替身**，`unresolved_report()` 会逐条列出。
    `assert_settled()` 供正式验收使用：规则没有定稿就不能宣布调度
    数值已验收。**它只在被显式调用时才会报错**，开发与离线测试不应
    调用——与 `BandwidthPolicy` 同一条纪律（全局约束 7）。
    """

    def __init__(
        self,
        backoff_base_ticks=20,
        backoff_max_ticks=200,
        backoff_factor=2,
        deferral_scan_limit=64,
        max_tasks_per_frame=64,
        wake_on_rebroadcast=False,
        invalidate_on_unload=False,
        catch_up=False,
        rotation_max_passes=2,
        min_share_floor=1,
    ):
        # type: (int, int, int, int, int, int, bool, bool, bool, int, int) -> None
        self.backoff_base_ticks = int(backoff_base_ticks)
        self.backoff_max_ticks = int(backoff_max_ticks)
        self.backoff_factor = int(backoff_factor)
        self.deferral_scan_limit = int(deferral_scan_limit)
        #: 单帧执行上限。**这是一道安全阀，不只是配额**：预算按「工作量」
        #: 计，而工作量由 handler 自己报——一个恒返回 0 又把自己排到过去的
        #: 任务会让 `while spent < budget` 永远转下去。有这条上限在，
        #: 无论 handler 怎么返回，一帧都必然结束。
        self.max_tasks_per_frame = max(0, int(max_tasks_per_frame))
        self.wake_on_rebroadcast = bool(wake_on_rebroadcast)
        #: 区块卸载时任务冻结（`False`）还是作废（`True`）。
        #: **默认冻结**，依据 01 的「卸载不是拆除」；验收原文里的「卸载」
        #: 两种读法都能自圆其说，因此这是一个具名配置点，不在这里定案。
        self.invalidate_on_unload = bool(invalidate_on_unload)
        #: 时钟落后（卡顿、关服、区块卸载）之后是否补做漏掉的周期。
        #: **默认不补做**——这是 04 文档明写的要求（「不集中补做无限历史
        #: tick」），因此它不是待定项；留成字段是为了让测试能构造另一种
        #: 口径来证明默认值确实生效。
        self.catch_up = bool(catch_up)
        self.rotation_max_passes = int(rotation_max_passes)
        self.min_share_floor = int(min_share_floor)

    def make_backoff(self):
        # type: () -> PollBackoff
        return PollBackoff(
            self.backoff_base_ticks,
            self.backoff_max_ticks,
            self.backoff_factor,
        )

    def freeze_delay(self):
        # type: () -> int
        """冻结中的任务多久复查一次。

        用退避上限（「最疏的节奏」）：冻结意味着设备不在世界里，没必要比
        它醒着时更勤快；同时也不会让它永远不再被检查。**没有单独的
        「空闲轮询间隔」旋钮**——那与退避上限是同一个量，两个名字只会让人
        以为它们可以不同。
        """
        return max(self.backoff_max_ticks, 1)

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
        """正式验收前的守卫：策略字段没有对应的决策就不算定案。

        调用它意味着「现在要宣布调度数值可以验收了」。
        开发与离线集成测试**不应**调用它。
        """
        raise AssertionError(
            "调度策略尚未定案，不能宣布数值已验收。待定项：\n  "
            + "\n  ".join(
                "%s（%s）：%s" % (field, rule, note)
                for field, rule, note in UNRESOLVED_FIELDS
            )
        )

    def describe(self):
        # type: () -> dict
        return {
            "backoff": {
                "base_ticks": self.backoff_base_ticks,
                "max_ticks": self.backoff_max_ticks,
                "factor": self.backoff_factor,
            },
            "deferral_scan_limit": self.deferral_scan_limit,
            "max_tasks_per_frame": self.max_tasks_per_frame,
            "wake_on_rebroadcast": self.wake_on_rebroadcast,
            "invalidate_on_unload": self.invalidate_on_unload,
            "catch_up": self.catch_up,
            "rotation_max_passes": self.rotation_max_passes,
            "min_share_floor": self.min_share_floor,
            "unresolved": self.unresolved_report(),
        }
