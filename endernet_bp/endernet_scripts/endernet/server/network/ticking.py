# coding=utf-8
"""网络调度：跨网公平轮转、唤醒路由与 `SchedulerService`（任务 04-02 / 04-03 / 04-07）。

分工（三样东西各自只有一个归属，不互相复制）：

| 关注点 | 归属 | 理由 |
| --- | --- | --- |
| 每网络的到期队列、设备 lane、睡眠与唤醒 | 本模块的 `SchedulerService` | 分量私有状态：两个独立网络绝不共享（03 验收 #4） |
| 逻辑时钟 | `ServerBootstrap` 持有，经 `context["tick"]` 下传 | 全进程只有一个帧序；不放进服务，就不需要共享单例，工厂也不必注入额外参数 |
| 跨网轮转与唤醒路由 | `RuntimeManager`（用本模块的 `RotationCursor` / `NodeLocator`） | 只有它同时看得见所有运行时与全局预算 |

**服务不覆盖 `power_ok`。** 能源的供需账属能源服务（`network/energy.py`），
调度器不是能源权威：这里返回 `True`/`False` 都是越权作答。继承基类的
`None`（「待确认」）才是诚实的答案，而主控不会据此放行。**这与「能源还没
实现」不是同一句话**——能源落地了也不改变「由谁负责」。
同理不覆盖 `handle`：命令路由表里没有调度器这一格，真调到这里就是编程错误，
不该伪装成一条业务失败。

**「时间」的单位一律是逻辑帧**（`context["tick"]` 的刻度），不是秒。
`SchedulerPolicy` 里所有间隔数值都是尚未定案的测试替身，
`unresolved_report()` 会逐条列出。

给下游（05/06/08/09）的契约：

- `schedule(ref, kind, delay, period, handler, owner)`：`owner` 传自己的
  `service_id`；`delay`/`period` 的单位是帧；handler 返回工作量的**整数**
  （轮询类是 `PollOutcome` / `(changed, cost)`）。
- **不得在 handler 里直接改库存或推进订单**：调度只决定「什么时候轮到你」，
  提交完成后再发通知的纪律不变。
- 重排就是替换：同一 `(ref, kind)` 只有一条活体，不必先 `cancel`。
"""

from ..runtime.scheduler import (
    DeviceLane,
    DueQueue,
    PollOutcome,
    ScheduledTask,
    SchedulerPolicy,
    TaskKey,
    TaskKind,
)
from .services import NetworkService, ServiceId


class RotationCursor(object):
    """跨网公平轮转的游标与统计。

    **为什么需要它。** 此前 `RuntimeManager.tick` 按 `sorted(component_id)`
    先到先得地花全局预算：排在字典序前面的分量把预算吃满，后面的每帧拿到 0，
    会被**永久饿死**——不是「慢」，是「不动」。分量编号在进程重启后会重新
    分配，因此「谁排在前面」还是碰运气。

    **本帧的分配形状**（`plan_frame` 给出环序与保底额度，执行两轮）：

    1. 保底一轮：按环序，每个网络至多 `share = max(min_share_floor, budget // n)`；
    2. 余量一轮：保底没花完的额度按**同一环序**继续分（谁先花完谁先把额度
       还回池子，余量给环序靠前的那个）。

    环序的起点每帧前移一位，因此余量的优先权也在轮换，不会固定偏向某个网络。

    **能兑现的保证（不要吹成更强的）**：`budget >= n` 时每帧每个网络至少
    拿到 1 单位额度；`budget < n` 时不保证每帧人人有份，但连续 `n` 帧内
    每个网络至少当过一次首位。总消耗永远不超过预算。
    """

    __slots__ = ("_cursor", "max_passes", "min_share_floor", "frames", "firsts", "last_frame")

    def __init__(self, max_passes=2, min_share_floor=1):
        # type: (int, int) -> None
        self._cursor = 0
        self.max_passes = max(1, int(max_passes))
        self.min_share_floor = max(0, int(min_share_floor))
        #: 诊断：轮转了多少帧、每个分量当了几次首位。
        self.frames = 0
        self.firsts = {}
        #: 诊断：本帧每个分量拿到的额度与实际消耗。
        self.last_frame = []

    def cursor(self):
        # type: () -> int
        return self._cursor

    @staticmethod
    def plan_frame(keys, budget, cursor=0, min_share_floor=1):
        # type: (list, int, int, int) -> tuple
        """算出本帧的 `(ring, share)`。

        `ring` 是 `keys` 的**位置索引**（不是分量编号）：调用方拿它去索引
        自己的列表，因此这个函数保持纯粹、可以离线断言。

        `share` 是保底额度：`budget // n`，但不低于 `min_share_floor`、
        也不超过 `budget`（预算比网络数还少时，保底不能凭空放大总预算）。

        **轮数不在这里。** 每帧跑几轮由调用方的循环决定（`max_passes` 是
        `RotationCursor` 上的字段），放在签名里会让读签名的人以为传进去能
        改变分配形状。
        """
        count = len(keys)
        budget = int(budget)
        if count <= 0 or budget <= 0:
            return ([], 0)
        share = budget // count
        if share < min_share_floor:
            share = min_share_floor
        if share > budget:
            share = budget
        start = int(cursor) % count
        ring = list(range(start, count)) + list(range(0, start))
        return (ring, share)

    def advance(self):
        # type: () -> int
        """本帧结束：环序起点前移一位。"""
        self._cursor += 1
        return self._cursor

    def note_frame(self, allocations):
        # type: (list) -> None
        """记录本帧的分配结果，供诊断。

        `allocations` 是 `[(component_id, granted, spent)]`。

        **首位按「本帧第一个真的做了事的分量」算，不是环序首位。**
        保底一轮会给环序上每个未退役分量都授予 `share > 0`，因此「按额度
        非零」统计出来的恒是环序首位——那正是这里想避免的误导（首位恰好
        是空闲网络时，统计看着很均匀，实际额度全被后面那台吃掉了）。

        存的是**元组**而不是字典列表：这个方法每帧被调用一次，而
        `last_frame` 只在按需诊断时被读；每帧构造一批 dict 是纯浪费。
        """
        self.frames += 1
        self.last_frame = [
            (item[0], int(item[1]), int(item[2])) for item in allocations
        ]
        for component_id, _granted, spent in self.last_frame:
            if spent > 0:
                self.firsts[component_id] = self.firsts.get(component_id, 0) + 1
                break

    def describe(self):
        # type: () -> dict
        return {
            "cursor": self._cursor,
            "max_passes": self.max_passes,
            "min_share_floor": self.min_share_floor,
            "frames": self.frames,
            #: 每个分量「本帧第一个真的做了事」的次数。轮转健康时应当接近均匀。
            "firsts": dict(self.firsts),
            #: 上一帧的分配明细。这里才把内部存的元组转成字典。
            "last_frame": [
                {"component_id": item[0], "granted": item[1], "spent": item[2]}
                for item in self.last_frame
            ],
        }


class NodeLocator(object):
    """`NodeKey -> component_id` 的索引，供唤醒路由使用。

    唤醒信号（方块实体 tick）只带世界位置，不带网络归属；而归属只有运行时
    管理器知道。用一个索引把它变成一次字典查找，而不是「遍历所有运行时问
    它们认不认识这个位置」——后者在大网络上每帧都是 O(网络数 × 成员数)。
    """

    __slots__ = ("_by_node", "rebuilds", "unrouted")

    def __init__(self):
        # type: () -> None
        self._by_node = {}
        #: 诊断：重建了几次、有多少条唤醒信号找不到归属。
        self.rebuilds = 0
        self.unrouted = 0

    def rebuild(self, runtimes):
        # type: (list) -> int
        """按各运行时的当前成员重建索引，返回索引到的节点数。"""
        index = {}
        for runtime in runtimes:
            component_id = runtime.component_id
            for node_key in runtime.member_keys():
                index[node_key.as_key()] = component_id
        self._by_node = index
        self.rebuilds += 1
        return len(index)

    def locate(self, node_key):
        # type: (object) -> str
        """返回分量编号；不认识这个位置时返回 `None`（不是抛错）。

        找不到是正常情形：方块实体 tick 会在设备刚放下的那一帧到达，
        而那时运行时还没同步成员。**丢掉一条唤醒不会丢事件本身**，
        设备变化照旧走注册表，因此这里只记数不报错。
        """
        component_id = self._by_node.get(node_key.as_key())
        if component_id is None:
            self.unrouted += 1
        return component_id

    def count(self):
        # type: () -> int
        return len(self._by_node)

    def describe(self):
        # type: () -> dict
        return {
            "indexed": len(self._by_node),
            "rebuilds": self.rebuilds,
            "unrouted": self.unrouted,
        }


class SchedulerService(NetworkService):
    """一个网络的到期队列、设备 lane 与睡眠/唤醒。

    **空闲时的代价是 O(1)，与网络里有多少设备无关。** 睡眠的设备不在堆里
    （而不是「在堆里、每次被跳过」），因此空闲网络每帧只做两次比较：
    队列空不空、最早到期超过当前帧没有。
    """

    service_id = ServiceId.SCHEDULER
    #: 不依赖任何服务。想「先调度、再轮到自己」的服务应声明
    #: `requires = (ServiceId.SCHEDULER,)`。
    requires = ()
    is_stand_in = False
    pending_task = ""

    #: 非轮询任务的 handler 约定：`handler(task) -> 工作量(int) | None`。
    #: 轮询任务的 handler 走 `PollOutcome`（它还要回答「有没有变化」）。
    def __init__(self, runtime=None, policy=None, timer=None):
        # type: (object, SchedulerPolicy, object) -> None
        self.runtime = None
        self.policy = policy if policy is not None else SchedulerPolicy()
        #: 计时器。`None` = **完全不测执行时长**（离线确定性场景的默认），
        #: 传 `scheduler.default_timer` 才会计时。时长只用于诊断。
        self.timer = timer
        self._queue = DueQueue()
        #: `node_key.as_key() -> DeviceLane`
        self._lanes = {}
        #: 最近一次推进时的帧序。**`schedule()` / `wake()` 都不在 `tick()`
        #: 里被调用**，它们要用它来算「从现在起 delay 帧」——若不记录，
        #: 游戏内算出来的 due 会是 0（初始值），每个任务都会「立刻到期」。
        self._tick_now = 0
        self._clock_source = "unset"
        self.stats = {
            # 队列
            "scheduled_total": 0,
            "replaced_total": 0,
            "cancelled_total": 0,
            "stale_dropped": 0,
            "rejected_owner": 0,
            "queued_peak": 0,
            # 推进。`ticks` 记的是**真的看了活**的帧数，`idle_ticks` 是
            # 走了空闲短路、什么都没做的帧数——两者的比例就是「睡眠比例」。
            "ticks": 0,
            "idle_ticks": 0,
            "clock_backwards": 0,
            "executions": 0,
            "poll_executions": 0,
            "budget_granted": 0,
            "budget_spent": 0,
            "budget_spent_peak": 0,
            #: 只有本层会写：handler 自报负数时钳到 0 的次数。
            #: 运行时的同名计数归运行时（`NetworkRuntime.stats`），
            #: 两处分开才知道是哪一层拦下的。
            "negative_costs": 0,
            # 延期
            "deferred_total": 0,
            "deferred_unloaded": 0,
            "deferred_now": 0,
            "deferred_capped": 0,
            # 唤醒
            "wakes": 0,
            # 执行时长（仅诊断）
            "duration_ns_total": 0,
            "duration_ns_max": 0,
            "duration_ns_last": 0,
            "duration_samples": 0,
            "duration_unsampled": 0,
        }
        if runtime is not None:
            self.attach(runtime)

    # -- 契约：绑定 ----------------------------------------------------

    def attach(self, runtime):
        # type: (object) -> None
        """绑定运行时。**不在 `__init__` 里建 lane**：lane 由 `add_node` 按
        成员集合创建，两处都建就会出现「哪个才是真相」的问题。"""
        self.runtime = runtime

    def detach(self):
        # type: () -> None
        self.runtime = None
        self._lanes = {}
        self._queue = DueQueue()
        # 统计保留：诊断在被卸载之后仍然要能回答「它之前干了多少活」。

    def is_ready(self):
        # type: () -> bool
        return True

    # 不覆盖 `power_ok`：能源由能源服务负责（`network/energy.py`），
    # 调度器不是能源权威——**「由谁负责」与「有没有实现」是两件事**，
    # 这里不答是因为不该答，不是因为它还没写。
    # 不覆盖 `handle`：命令路由表里没有调度器。理由见模块 docstring。

    def save_node_data(self, node):
        # type: (object) -> object
        """`None`：不存。

        到期时刻是**帧序**，跨重启没有意义（下一次启动的帧序从 0 重来，
        存下来的 `due` 要么立刻全部到期、要么永远不到期）。未完成的周期
        任务是否落盘属任务 12 的决策，这里不预留半个假实现。
        """
        return None

    # -- 契约：成员 ----------------------------------------------------

    def add_node(self, node, saved_data=None):
        # type: (object, dict) -> bool
        """登记成员。**幂等**：拓扑重建会对同一节点反复调用。

        两种「真的做了事」的情形：新建 lane、设备被替换（换 ref 并作废旧
        任务）。其余重复广播默认**不重置倒计时**（`policy.wake_on_rebroadcast`），
        否则每次拓扑重建都会把退避打回初值，退避形同虚设。

        **不处理「从卸载恢复」**：`lane` 不再持有「是否加载」——那份状态在
        注册表里（`record.loaded`），而区块卸载的事件路径只到注册表为止，
        lane 上再存一份永远不会被更新，只会让诊断撒谎。冻结与否由 `_run`
        按注册表判定，不需要在这里报告变化。
        """
        key_tuple = node.node_key.as_key()
        lane = self._lanes.get(key_tuple)
        if lane is None:
            self._lanes[key_tuple] = self._make_lane(node)
            return True
        if lane.ref != node.ref:
            # 位置没变、实例换了：旧实例的任务全部作废。这是主路径——
            # 01 的「同位置替换」正是这样到达服务层的。
            self.cancel_all(lane.ref)
            lane.ref = node.ref
            lane.device_id = node.device_id
            lane.backoff = self.policy.make_backoff()
            lane.poll_handler = None
            lane.poll_owner = ""
            # 换了方块等于换了一台设备：轮询从头开始，而且**本帧就该看一次**，
            # 不能沿用旧实例的倒计时（那可能已经退避到最疏，让新设备几十帧
            # 不被发现）。这里必须给出一个真实的时刻，`_schedule_poll` 要用它。
            lane.poll_due = self._now()
            self._schedule_poll(lane)
            self.stats["replaced_total"] += 1
            return True
        if self.policy.wake_on_rebroadcast:
            self.wake(lane.ref)
            return True
        return False

    def remove_node(self, node):
        # type: (object) -> bool
        """卸载成员。按 `lane` 记的 ref 取消，不依赖传入对象的代次。"""
        key_tuple = node.node_key.as_key()
        lane = self._lanes.get(key_tuple)
        if lane is None:
            return False
        if lane.ref != node.ref:
            # 迟到的卸载通知（该位置已经被替换过）：只清它自己那个代次的
            # 任务，**不能连 lane 一起删掉**——那会把新实例的任务一起抹了。
            self.cancel_all(node.ref)
            return False
        self.cancel_all(lane.ref)
        del self._lanes[key_tuple]
        return True

    def note_domain_change(self, node, from_domain_ids, to_domain_ids):
        # type: (object, tuple, tuple) -> bool
        """`False`：调度不关心负载域。

        域变了不等于设备换了，到期周期与轮询间隔都不受影响。
        """
        return False

    def on_wake(self, node_key):
        # type: (object) -> bool
        """方块实体 tick 的落地处。**不是命令，因此不检查许可。**

        与 `add_node` / `note_domain_change` 同属内部信号路径：它不产生任何
        业务副作用，因此不违反「设备不得绕过主控改库存」。

        **它只把退避打回初值，不把轮询提前到本帧。** 这个区分是关键：
        方块实体 tick 是引擎每帧的心跳（开 `tick: true` 的设备约 20 Hz），
        不是「发生了变化」。若拿它当变化信号去立刻轮询，开 tick 的设备就
        退化成每帧一次扫描——正是 04-03 要避免的高频扫描。

        「真的变了、现在就该看」的入口是 `wake(ref)`，由知道变化发生的
        调用方使用。
        """
        lane = self._lanes.get(node_key.as_key())
        if lane is None:
            return False
        lane.wakes += 1
        self.stats["wakes"] += 1
        if lane.backoff.steps() > 0:
            # 还在心跳 ⇒ 不值得继续退避到最高间隔。
            lane.backoff.note_changed()
        return True

    # -- 推进 ----------------------------------------------------------

    def tick(self, context, budget):
        # type: (dict, int) -> int
        """推进到期任务，返回本帧消耗的工作量。

        **判定顺序是写死的**，改它等于改语义：

        1. 取当前帧（`context["tick"]`；没有就用**最近一次的权威读数**）；
        2. 队列为空 → 0（O(1)）；
        3. 最早到期还没到 → 0（O(1)）；
        4. 循环取任务，直到用完预算或没有到期任务。

        **前两步排在一切记账之前**，这样「空闲网络每帧只做两次比较」是字面
        成立的，而不是一句大约的说法（有用例断言空闲帧不动任何计数）。
        """
        context = context or {}
        now = context.get("tick")
        if now is None:
            # **没有 context 就用最近一次的权威读数，绝不自己数帧。**
            # 私有计数器与权威时钟混用会让时间倒流：先按 context 走到 500，
            # 再来一次没有 context 的调用把读数打回 0，于是带 delay 的任务
            # 全部排到过去、同帧集中触发。
            self._clock_source = "last"
            now = self._tick_now
        else:
            now = int(now)
            self._clock_source = "context"
            if now < self._tick_now:
                # 引擎帧序不该倒退。真倒退了也不跟——跟上去就是让一整批
                # 任务立刻到期。留痕，不静默。
                self.stats["clock_backwards"] += 1
                now = self._tick_now
        self._tick_now = now

        budget = int(budget)
        if budget <= 0:
            return 0
        # 空闲短路。
        if not self._queue:
            self.stats["idle_ticks"] += 1
            return 0
        due = self._queue.peek_due()
        if due is None or due > now:
            self.stats["idle_ticks"] += 1
            return 0

        self.stats["ticks"] += 1
        self.stats["budget_granted"] += budget
        spent = 0
        executions = 0
        cap = self.policy.max_tasks_per_frame
        # 两个终止条件缺一不可：预算按 handler 自报的工作量计，而一个恒
        # 返回 0 又把自己排到过去的任务会让 `spent` 一直不动。
        while spent < budget and executions < cap:
            task = self._queue.pop_due(now)
            if task is None:
                break
            spent += self._run(task, now)
            executions += 1
        self.stats["budget_spent"] += spent
        if spent > self.stats["budget_spent_peak"]:
            self.stats["budget_spent_peak"] = spent
        if self._queue:
            count, capped = self._queue.count_due(now, self.policy.deferral_scan_limit)
            # **帧级数字是估计值**：它不重演真实执行顺序。精确的积压量看
            # 每个任务的 `deferrals`（`queue_view()` 里能读到）。
            self.stats["deferred_now"] = count
            self.stats["deferred_capped"] = 1 if capped else 0
        else:
            self.stats["deferred_now"] = 0
            self.stats["deferred_capped"] = 0
        return spent

    def _run(self, task, now):
        # type: (ScheduledTask, int) -> int
        """执行一个已到期的任务。返回它消耗的工作量。"""
        lane = self._lanes.get(task.node_key.as_key())
        if lane is None:
            # lane 已消失（服务被卸载、或节点被移除而任务还留在堆里）。
            return 0
        record = self._record_of(task.ref)
        if record is None:
            # 第三道防线：位置已被替换或拆除，代次对不上了。
            self.stats["stale_dropped"] += 1
            return 0
        if not record.loaded:
            if self.policy.invalidate_on_unload:
                self.stats["stale_dropped"] += 1
                return 0
            # 冻结：不执行、不失效，重新计时而不是攒着一次补做。
            self.stats["deferred_total"] += 1
            self.stats["deferred_unloaded"] += 1
            task.deferrals += 1
            self._rearm(task, lane, now + self._freeze_delay(task))
            return 0

        cost = 0
        started = self._start_timer()
        try:
            cost = self._dispatch(task, lane, now)
        finally:
            # **先把下一次排定好，再让异常冒泡。** `pop_due` 已经把它从活体
            # 表里拿走了，不重排就是永久丢失：一次 handler 异常就让这台设备
            # 再也不被轮询（周期任务同理），而异常只在 `drive_once` 里记进
            # `tick_errors`——没人会把它和「轮询停了」联系起来。
            task.runs += 1
            task.last_run_tick = now
            task.last_cost = cost
            self.stats["executions"] += 1
            if task.kind == TaskKind.POLL:
                lane.polls += 1
                self.stats["poll_executions"] += 1
                self._rearm(task, lane, now + lane.backoff.delay())
            elif task.is_periodic():
                if self.policy.catch_up:
                    # 补做漏掉的周期（**默认关闭**）：`due` 只前进一个周期，
                    # 若仍在过去就会在同一帧里被再次弹出，直到追上当前帧。
                    # 不会无界——预算与 `max_tasks_per_frame` 两道闸门都在。
                    next_due = task.due + task.period
                else:
                    # 默认口径：跳过漏掉的周期，从当前帧重新计时。
                    # 这是 04 文档明写的要求（「不集中补做无限历史 tick」）。
                    next_due = now + task.period
                self._rearm(task, lane, next_due)
            self._stop_timer(started)
        return cost

    def _dispatch(self, task, lane, now):
        # type: (ScheduledTask, DeviceLane, int) -> int
        """调用 handler，把返回值归一化成「消耗的工作量」。"""
        if task.kind == TaskKind.POLL:
            handler = lane.poll_handler
            if handler is None:
                # **没有消费者就退避。** 没人能回答「它变了没有」，因此正确
                # 的行为是逐步拉长间隔，而不是每 `base` 帧白扫一次——
                # 后者会让退避形同虚设（本增量 100% 的 lane 都是这种状态）。
                lane.backoff.note_unchanged()
                return 0
            outcome = PollOutcome.of(handler(task))
            if outcome.changed:
                lane.backoff.note_changed()
            else:
                lane.backoff.note_unchanged()
            return self._clamp_cost(outcome.cost)
        if task.handler is None:
            return 0
        return self._clamp_cost(int(task.handler(task) or 0))

    def _clamp_cost(self, cost):
        # type: (int) -> int
        """消耗量下界为 0。

        负的消耗量会**反向放大**同一帧里后续任务的额度——一个返回 -5 的
        坏 handler 能让整帧多做一堆事，而且看上去什么错都没有。留一条
        计数，缺陷就会有痕迹。
        """
        cost = int(cost)
        if cost < 0:
            self.stats["negative_costs"] += 1
            return 0
        return cost

    # -- 注册与取消 ----------------------------------------------------

    def schedule(self, ref, kind, delay=0, period=None, handler=None, owner=None):
        # type: (object, str, int, int, object, str) -> ScheduledTask
        """登记或**重排**一个任务。返回活体任务对象；被拒绝时返回 `None`。

        **重排 = 替换**：同一 `(ref, kind)` 在活体表里有且只有一条，
        因此「同一任务重排不会重复执行」不需要调用方先 `cancel`。
        """
        key = TaskKey.of(ref, kind)
        if owner and not self._has_service(owner):
            # 任务不该跑到别的网络或别的服务上：owner 必须是自己人。
            self.stats["rejected_owner"] += 1
            return None
        delay = int(delay)
        if delay < 0:
            delay = 0
        if period is not None:
            period = int(period)
            if period < 1:
                raise ValueError("周期至少 1 帧: %r" % (period,))
        now = self._now()
        existing = self._queue.get(key)
        if existing is not None:
            self.stats["replaced_total"] += 1
        else:
            self.stats["scheduled_total"] += 1
        task = ScheduledTask(
            key,
            ref,
            now + delay,
            period=period,
            handler=handler,
            owner=owner or "",
            created_tick=now,
        )
        self._queue.put(task)
        self._note_queue_peak()
        return task

    def cancel(self, ref, kind):
        # type: (object, str) -> bool
        key = TaskKey.of(ref, kind)
        if self._queue.drop(key):
            self.stats["cancelled_total"] += 1
            return True
        return False

    def cancel_all(self, ref):
        # type: (object) -> int
        """取消该实例的全部任务。设备替换与移除都走它。

        判据复用 `DeviceRef.as_key()`（位置 + 代次）——**不在这里重写身份规则**，
        而且这是一趟 O(Q) 的遍历：`tasks()` 版会整表排序，而本方法在
        设备替换与区块卸载的路径上（卸载会对每个成员调一次）。
        """
        target = ref.as_key()
        removed = self._queue.drop_where(
            lambda task: task.ref.as_key() == target
        )
        if removed:
            self.stats["cancelled_total"] += removed
        return removed

    def wake(self, ref):
        # type: (object) -> bool
        """把该设备的轮询提前到本帧，并把退避打回初值。"""
        lane = self._lanes.get(ref.node_key.as_key())
        if lane is None or lane.ref != ref:
            return False
        lane.wakes += 1
        self.stats["wakes"] += 1
        lane.backoff.note_changed()
        lane.poll_due = self._now()
        self._arm_poll(lane)
        return True

    def sleep(self, ref):
        # type: (object) -> bool
        """让该设备睡到最低频：取消它的非轮询任务，轮询推到退避上限。

        睡眠**不是**「永远不会被发现」——轮询定时器还在，只是间隔最大。
        验收里「无事件容器可最终被轮询发现」靠的就是这条定时器。
        """
        lane = self._lanes.get(ref.node_key.as_key())
        if lane is None or lane.ref != ref:
            return False
        # 一趟遍历拿掉全部非轮询任务，不排序（`tasks()` 会整表排序）。
        target = ref.as_key()
        removed = self._queue.drop_where(
            lambda task: task.ref.as_key() == target
            and task.kind != TaskKind.POLL
        )
        self.stats["cancelled_total"] += removed
        while lane.backoff.delay() < lane.backoff.max_ticks:
            lane.backoff.note_unchanged()
        lane.poll_due = self._now() + lane.backoff.delay()
        self._arm_poll(lane)
        return True

    def set_poll_handler(self, ref, handler, owner=None):
        # type: (object, object, str) -> bool
        """注册该设备的轮询回调。`handler is None` = 撤销注册。"""
        lane = self._lanes.get(ref.node_key.as_key())
        if lane is None or lane.ref != ref:
            return False
        if owner and not self._has_service(owner):
            self.stats["rejected_owner"] += 1
            return False
        lane.poll_handler = handler
        lane.poll_owner = owner or ""
        if handler is not None:
            # 刚有人开始看它：把退避打回初值并重排一次。否则它会带着
            # 「没人看」期间攒下的最疏间隔继续跑，新消费者要等很久才被轮询到。
            lane.backoff.note_changed()
            lane.poll_due = self._now() + lane.backoff.delay()
            self._arm_poll(lane)
        return True

    # -- 查询 ----------------------------------------------------------

    def next_due(self):
        # type: () -> object
        """最早的到期帧；`None` = 队列里没有活体任务。"""
        return self._queue.peek_due()

    def due_tasks(self, now=None):
        # type: (int) -> list
        """**只读**：列出已到期的任务，不消费、不改状态。"""
        if now is None:
            now = self._now()
        return [task for task in self._queue.tasks() if task.due <= now]

    def drain_due(self, limit=8, now=None):
        # type: (int, int) -> list
        """拉取式消费：取走至多 `limit` 个到期任务并**执行**它们。

        给不用回调的消费者准备（它们自己的推进循环里不想被回调打断）。
        返回值里每条是 `(task, cost)`。
        """
        if now is None:
            now = self._now()
        out = []
        for _ in range(max(0, int(limit))):
            task = self._queue.pop_due(now)
            if task is None:
                break
            out.append((task, self._run(task, now)))
        return out

    def queue_view(self):
        # type: () -> list
        return [task.to_dict() for task in self._queue.tasks()]

    def lane_view(self):
        # type: () -> list
        """每条 lane 的只读视图。「是否加载」以**注册表**为准（`None` =
        该实例在注册表里已经不存在，不是「没加载」）。
        """
        out = []
        for key in sorted(self._lanes):
            lane = self._lanes[key]
            view = lane.to_dict()
            record = self._record_of(lane.ref)
            view["loaded"] = None if record is None else bool(record.loaded)
            out.append(view)
        return out

    def lane_count(self):
        # type: () -> int
        return len(self._lanes)

    def sleeping_lanes(self):
        # type: () -> int
        """睡着的设备数：除轮询外没有任何活体任务的 lane。

        「没有任务」与「没有在推进」是两回事，这里只数前者——后者由
        `budget_spent` 回答。
        """
        asleep = 0
        active = set()
        for task in self._queue.tasks():
            if task.kind != TaskKind.POLL:
                active.add(task.node_key.as_key())
        for key_tuple in self._lanes:
            if key_tuple not in active:
                asleep += 1
        return asleep

    # -- 状态与诊断 ----------------------------------------------------

    def status(self):
        # type: () -> dict
        return {
            "service_id": self.service_id,
            "available": True,
            "stand_in": False,
            "clock": self._now(),
            "clock_source": self._clock_source,
            "lanes": len(self._lanes),
            "sleeping": self.sleeping_lanes(),
            "queued": len(self._queue),
            "next_due": self._queue.peek_due(),
        }

    def diagnostics(self):
        # type: () -> dict
        """契约的 `diagnostics()` + 本服务的指标。

        **不手抄基类的尾巴**：`NetworkService.diagnostics()` 已经给出
        `stand_in` / `pending_task` / `requires`，抄一份的代价是基类将来
        新增字段时这里悄悄落后（而它只在 `debug_query("scheduler")` 里可见）。
        """
        state = NetworkService.diagnostics(self)
        state["stats"] = dict(self.stats)
        state["queue"] = self._queue.stats()
        lanes = len(self._lanes)
        # 睡眠比例用**整数千分比**：与「不用浮点累计」同一条纪律，
        # 也避免诊断数字在不同环境下最后一位不一样。`sleeping` 已经在
        # `status()` 里算过一次，直接用，不重复扫队列。
        state["sleep_permille"] = state["sleeping"] * 1000 // lanes if lanes else 0
        # 注册了轮询回调的设备数。本增量恒为 0——调度器只提供原语与定时器，
        # 没有任何设备注册消费者。**这是一个被记录的事实**，不是被隐藏的空白：
        # 「轮询在跑」与「有人被通知了」是两回事。
        state["poll_handlers"] = len(
            [lane for lane in self._lanes.values() if lane.has_consumer()]
        )
        policy = self.policy.describe()
        state["policy"] = policy
        # `describe()` 里已经带了 unresolved，取出来用，不再算第二遍。
        state["unresolved"] = policy["unresolved"]
        return state

    # -- 内部 ----------------------------------------------------------

    def _now(self):
        # type: () -> int
        """最近一次推进时的帧序。

        **权威时钟始终是 `context["tick"]`**（由 `ServerBootstrap` 持有并
        下传），这里只是它的最近读数，供不在 `tick()` 里被调用的
        `schedule` / `wake` / `sleep` 使用。误差最多一帧，且方向安全
        （任务可能早一帧到期，不会晚一整轮）。
        """
        return self._tick_now

    def _make_lane(self, node):
        # type: (object) -> DeviceLane
        lane = DeviceLane(node.ref, node.device_id, self.policy.make_backoff())
        lane.poll_due = self._now()
        self._schedule_poll(lane)
        return lane

    def _schedule_poll(self, lane):
        # type: (DeviceLane) -> ScheduledTask
        """排一个轮询任务。设备刚接入就到期一次，之后的间隔由退避决定。"""
        key = TaskKey.of(lane.ref, TaskKind.POLL)
        task = ScheduledTask(key, lane.ref, lane.poll_due)
        self._rearm(task, lane, lane.poll_due)
        return task

    def _arm_poll(self, lane):
        # type: (DeviceLane) -> None
        """把轮询任务改排到 `lane.poll_due`（唤醒与睡眠都用它）。

        **不接受 `due` 形参**：两个调用点都是「先写 `lane.poll_due` 再排」，
        多一个形参只会让人以为两者可以不同——而新建分支用的本来就是
        `lane.poll_due`，那种分歧会表现为「时钟偶尔跳一帧」。
        """
        key = TaskKey.of(lane.ref, TaskKind.POLL)
        task = self._queue.get(key)
        if task is None:
            self._schedule_poll(lane)
            return
        self._rearm(task, lane, lane.poll_due)

    def _rearm(self, task, lane, due):
        # type: (ScheduledTask, DeviceLane, int) -> None
        """把任务排到 `due`，**并让 lane 上的读数跟着走**。

        lane 记的 `poll_due` 与队列里那条任务必须一致：不一致时
        `lane_view()` 显示的是「上次执行前」的时刻，读起来像轮询卡住了
        （冻结分支就漏过一次）。
        """
        task.due = int(due)
        self._queue.put(task)
        if lane is not None and task.kind == TaskKind.POLL:
            lane.poll_due = task.due
        self._note_queue_peak()

    def _freeze_delay(self, task):
        # type: (ScheduledTask) -> int
        """冻结时的重新计时间隔。周期任务按自己的周期，其余按「最疏节奏」。"""
        if task.is_periodic():
            return max(1, task.period)
        return self.policy.freeze_delay()

    def _record_of(self, ref):
        # type: (object) -> object
        runtime = self.runtime
        registry = getattr(runtime, "registry", None)
        if registry is None:
            return None
        return registry.get_by_ref(ref)

    def _has_service(self, service_id):
        # type: (str) -> bool
        runtime = self.runtime
        services = getattr(runtime, "services", None)
        if services is None:
            # 还没绑定运行时：允许（离线单测直接构造服务时就是这种情形）。
            return True
        return services.get(service_id) is not None

    def _note_queue_peak(self):
        # type: () -> None
        queued = len(self._queue)
        if queued > self.stats["queued_peak"]:
            self.stats["queued_peak"] = queued

    def _start_timer(self):
        # type: () -> object
        if self.timer is None:
            self.stats["duration_unsampled"] += 1
            return None
        return self.timer()

    def _stop_timer(self, started):
        # type: (object) -> None
        if started is None or self.timer is None:
            return
        elapsed = self.timer() - started
        if elapsed < 0:
            elapsed = 0.0
        nanos = int(elapsed * 1000000000)
        self.stats["duration_ns_last"] = nanos
        self.stats["duration_ns_total"] += nanos
        self.stats["duration_samples"] += 1
        if nanos > self.stats["duration_ns_max"]:
            self.stats["duration_ns_max"] = nanos

    def __repr__(self):
        return "SchedulerService(component=%r, queued=%d, lanes=%d)" % (
            getattr(self.runtime, "component_id", None),
            len(self._queue),
            len(self._lanes),
        )
