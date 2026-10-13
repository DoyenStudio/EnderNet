# coding=utf-8
"""下单预览：一次**只读**的分帧规划。

权威规则见 `plans/technical_notes/terminal_pattern_order_preview.md`。
本模块只回答一件事：**「做 N 个这个东西，要花掉哪些原料、差不差料」**，
并把结论按分帧算出来、分批交给终端。

## 为什么它不在 09 的订单里

契约第 3 节明写「预览不创建执行订单，不占执行内存、并行槽、材料预留或
活跃执行订单名额」。这一条在这里是**结构性的**而不是纪律性的：本类的
构造函数收不到 `orders`、`memory`、`slots`，也没有任何地方调
`storage.reserve`——没有那只手，就写不出那一步。同理，`storage` 只经
`_storage_of()` 拿到、只走 `keys()` / `query()` 这类读口。

## 它算的是哪一份世界

**规划读全进程一份的样板索引**，与 09 的下单完全同源
（`bootstrap._factory_builder` 的说明：「样板装在方块里…本来就是跨网络」）。
界面上的 `+` 标记则是**按网络**算的——两者可能不一致：某行没有 `+`
（本网络没有提供者），但预览说它可造（别的网络有）。这是**既有**行为，
本模块不顺手改 09；见交付说明里的记录。

## 缺料怎么算

一行是不是红的，等于**它的 canonical 在不在 `plan.blocked_keys` 里**。
不另算一遍：`PlanJob._achievable()` 是从库存出发、只把「全部输入都已可得
的步骤」算作可运行的最小不动点，那正是契约第 4 节的
`step_missing = direct_missing OR any(child.step_missing)`；而 09 的
`submit_order` 拒绝的条件正是 `blocked_keys` 非空。于是**预览标红与下单
被拒是同一个谓词**——契约第 4 节末句要的就是这个。

## 分帧

每个预览每 tick 推进**一个逻辑配方节点**，与 09 的
`PLAN_NODES_PER_TICK = 1` 同量级，但**不复用那条常量**：那条是订单的规则，
订单的预算由 `OrderExecutor.tick` 统一调度；预览走自己的 `advance`，
不吃订单预算，因此必须有自己的并发上限（没有上限时 N 台终端能把服务端
算死——上限的**存在**是硬要求，取值是未定案）。
"""

from ...common.protocol import BlockReason, CommandResult, Reason
from ...common.resource_key import ResourceKey
from .planner import PlanStatus

try:
    INTEGER_TYPES = (int, long)
except NameError:
    INTEGER_TYPES = (int,)

#: 预览号的字面前缀。**只用于人读日志**：绑定靠的是会话与目标，不是
#: 「猜不到」——可猜性从来不是这里的安全边界（`PreviewService.consume`
#: 逐条核对会话、玩家、网络、目标与数量）。
PREVIEW_ID_PREFIX = "pv"

#: 每 tick 推给一个预览的配方节点数。**规则是 1**，与 09 同量级；
#: 写成常量而不是从 `executor` import，是因为两者服务的是不同的预算池。
PREVIEW_NODES_PER_TICK = 1


class PreviewPolicy(object):
    """预览的未定案旋钮。

    做成具名策略而不是散落的常数，理由与 `SyncPolicy` / `TransferPolicy`
    一致：这些都是**本次没有 User 口径**的数值（契约第 6 节明写
    「本次没有指定」），写死一个值等于用临时实现把玩法定下来。

    **并发上限的存在不是未定项。** 每个预览每帧白吃一个配方节点、
    不进订单预算，没有上限就是「开几个界面把服务端算死」。
    未定的只是取多大。

    `assert_settled()` **只应在正式验收时调用**，开发与离线测试不应调用。
    """

    __slots__ = (
        "ttl_ticks",
        "max_per_session",
        "max_per_network",
        "chunk_items",
        "max_rows",
        "consume_on_first_attempt",
    )

    def __init__(self, ttl_ticks=600, max_per_session=4, max_per_network=16,
                 chunk_items=32, max_rows=64, consume_on_first_attempt=True):
        # type: (int, int, int, int, int, bool) -> None
        #: 一份预览活多少**逻辑帧**。出厂取 600，与 `SessionPolicy.
        #: session_ttl_frames` 同一个数——理由是同一个：终端放着不动
        #: 二十秒就该当它走了。**未定案。**
        self.ttl_ticks = int(ttl_ticks)
        #: 一个会话最多同时有几份预览。**未定案。**
        self.max_per_session = int(max_per_session)
        #: 一张网络最多同时有几份预览（跨会话）。**未定案。**
        self.max_per_network = int(max_per_network)
        #: 一批发几行原料。**未定案。**
        self.chunk_items = int(chunk_items)
        #: 最多显示几行原料，超出即 `truncated`。默认与
        #: `order_preview_src_items` 网格的上限一致：截断了要**如实报**，
        #: 不能让一份算不全的结果看起来像「没有缺料」。
        self.max_rows = int(max_rows)
        #: 「下单」这一下算不算用掉了这份预览。取 `True`：**第一次走到
        #: 下单流水线就消费掉**，哪怕随后被规划或准入拒了。理由是预览的
        #: 依据在那些情形下正可能已经过期（契约第 5 节「过期结果不能绕过
        #: 校验」），逼玩家重新预览是诚实的；反过来（只在成功时消费）会
        #: 留下一件可反复使用、其有效性还要另行论证的凭证。
        self.consume_on_first_attempt = bool(consume_on_first_attempt)
        for name in ("ttl_ticks", "max_per_session", "max_per_network",
                     "chunk_items", "max_rows"):
            if getattr(self, name) <= 0:
                raise ValueError("%s 必须为正，得到 %r" % (name, getattr(self, name)))

    def unresolved_report(self):
        # type: () -> list
        """仍带未定标签的字段。与 `SyncPolicy` / `TransferPolicy` 同形。"""
        return [
            {"field": "preview.ttl_ticks", "rule": "11",
             "value": self.ttl_ticks, "note": "预览存活帧数；替身取 600"},
            {"field": "preview.max_per_session", "rule": "11",
             "value": self.max_per_session, "note": "每会话并发上限；替身取 4"},
            {"field": "preview.max_per_network", "rule": "11",
             "value": self.max_per_network, "note": "每网络并发上限；替身取 16"},
            {"field": "preview.chunk_items", "rule": "11",
             "value": self.chunk_items, "note": "结果分批大小；替身取 32"},
            {"field": "preview.max_rows", "rule": "11",
             "value": self.max_rows, "note": "原料行显示上限；替身取 64"},
        ]

    def assert_settled(self):
        # type: () -> None
        """正式验收用：仍有未定项就报错。开发与离线测试不应调用它。"""
        pending = self.unresolved_report()
        if pending:
            raise AssertionError(
                "预览策略仍有 %d 项未定案，不能作为正式玩法交付：\n  "
                % (len(pending),)
                + "\n  ".join(
                    "%s（规则 %s，当前值 %r）"
                    % (item["field"], item["rule"], item["value"])
                    for item in pending
                )
            )

    def describe(self):
        # type: () -> dict
        return {
            "ttl_ticks": self.ttl_ticks,
            "max_per_session": self.max_per_session,
            "max_per_network": self.max_per_network,
            "chunk_items": self.chunk_items,
            "max_rows": self.max_rows,
            "consume_on_first_attempt": self.consume_on_first_attempt,
        }


def parse_target(payload):
    # type: (dict) -> tuple
    """从命令载荷里读下单目标。返回 `(key, amount, reasons)`。

    **与 09 的 `_read_target` 读同一组键**（`target` 是资源键字典、
    `amount` 是正整数），但不 import 它：那是 `executor` 的私有函数，
    跨模块借用私有名会让「下单参数的唯一校验点」变成两个。
    两边的判据因此必须**同时**改——写在这里的注释就是那条提醒。
    """
    raw = (payload or {}).get("target")
    amount = (payload or {}).get("amount")
    if not isinstance(raw, dict):
        return None, None, [
            BlockReason(
                Reason.BAD_PARAMETER,
                u"预览要给出 target（资源键的字典形式）与 amount（正整数）",
            )
        ]
    try:
        key = ResourceKey.from_dict(raw)
    except Exception as error:
        return None, None, [
            BlockReason(
                Reason.BAD_PARAMETER, u"target 解不出一个资源键：%s" % (error,)
            )
        ]
    if isinstance(amount, bool) or not isinstance(amount, INTEGER_TYPES) or amount <= 0:
        return None, None, [
            BlockReason(
                Reason.BAD_PARAMETER,
                u"amount 必须是正整数，得到 %r" % (amount,),
            )
        ]
    return key, int(amount), None


class PreviewJob(object):
    """一份预览。**绑定的是会话，不是玩家手速。**"""

    __slots__ = (
        "preview_id",
        "request_id",
        "session_id",
        "player_id",
        "network_id",
        "device_ref",
        "epoch",
        "target",
        "amount",
        "semantics",
        "job",
        "generation",
        "created_tick",
        "status",
        "consumed",
        "delivery",
        "blocked_reason",
    )

    def __init__(self, preview_id, request_id, source, target, amount,
                 semantics, job, created_tick):
        # type: (str, str, object, object, int, str, object, int) -> None
        self.preview_id = preview_id
        self.request_id = request_id
        #: 会话五元组。**下单时必须逐条对上**——这是「断线重开后的迟到包」
        #: 与「旧会话的包覆盖新结果」唯一的拦法。
        self.session_id = getattr(source, "session_id", "")
        self.player_id = getattr(source, "player_id", "")
        self.network_id = getattr(source, "network_id", "")
        self.device_ref = getattr(source, "device_ref", None)
        self.epoch = getattr(source, "epoch", "")
        self.target = target
        self.amount = int(amount)
        #: R11 的 `TargetSemantics`。**随预览一起带走**：客户端据此标注
        #: 「库里已有的是否抵扣」，而下单时也不能与当时不同。
        self.semantics = semantics
        self.job = job
        #: 每重算一次加一。分片带着它，旧一轮的迟到分片因此拼不进来。
        self.generation = 1
        self.created_tick = int(created_tick)
        self.status = job.status()
        self.consumed = False
        self.delivery = None
        self.blocked_reason = ""

    def is_alive(self):
        # type: () -> bool
        """还能被消费或还在算。**取消与过期都不算活着**。"""
        return self.status in PlanStatus.TERMINAL and self.status != PlanStatus.CANCELLED

    def __repr__(self):
        return "PreviewJob(%s, %s, %s 个)" % (
            self.preview_id, self.status, self.amount,
        )


def _network_id_of(runtime):
    # type: (object) -> object
    """运行时的**线上网络标识**。取不到就返回 `None`。

    `network_id_of` 是同步层做的那次转换（`component_id` → 线上标识），
    而这里不 import 同步层——`crafting/` 不该依赖 `sync/`。装配层在注入
    时把两边对齐即可；取不到标识时那条「是不是同一张网」的检查退化成
    「不检查」，而**预览本来就是每网络一份**，那不是漏洞。
    """
    if runtime is None:
        return None
    return getattr(runtime, "component_id", None)


class PreviewService(object):
    """一张网络的预览集合。**每网络一份**，与订单同寿命。

    构造函数**故意收窄**：`planner` / `patterns` / `storage_of` / `policy`，
    没有订单、内存、槽位、预留。见模块 docstring。
    """

    def __init__(self, runtime=None, planner=None, patterns=None,
                 storage_of=None, policy=None):
        # type: (object, object, object, object, object) -> None
        self.runtime = runtime
        #: 本服务所属的网络（线上标识）。**每网络一份**，因此它同时就是
        #: 「一份预览属于哪张网络」的答案——`CommandSource` 上没有网络字段
        #: （`session.network_id` 已经是权威，命令不再带第二份）。
        self.network_id = None
        self.planner = planner
        self.patterns = patterns
        #: `() -> StorageService | None`。**只读口**：本类只走 `keys` /
        #: `query` 那一边，绝不 `reserve`。
        self._storage_of = storage_of if storage_of is not None else (lambda: None)
        self.policy = policy if policy is not None else PreviewPolicy()
        self._jobs = {}
        self._next = 0
        #: **本服务自己的「现在」**，只在 `advance` 里跟着逻辑帧走。
        #:
        #: 生命周期（创建、过期、消费）必须**只用这一个时钟**。命令那一侧
        #: 带的是 `CommandSource.received_tick`，而那是**中枢自己的帧号**
        #: （`TerminalSyncHub._frame`）——与逻辑时钟不是同一个数。两者相减
        #: 得到的差值没有任何意义，而症状是**每一份预览一建出来就被判过期**
        #: （中枢帧号远小于逻辑时钟）。2026-10-03 上机就是这么撞出来的。
        self._tick = 0
        self.stats = {
            "requested": 0,
            "rejected": 0,
            "resumed": 0,
            "completed": 0,
            "expired": 0,
            "cancelled": 0,
            "consumed": 0,
            "refused": 0,
        }

    def attach(self, runtime):
        # type: (object) -> None
        self.runtime = runtime
        self.network_id = _network_id_of(runtime)

    def is_ready(self):
        # type: () -> bool
        return self.planner is not None and self.patterns is not None

    # -- 请求 ----------------------------------------------------------

    def request(self, command, payload):
        # type: (object, dict) -> CommandResult
        """受理一条预览请求。**只建任务，不算内容**（分帧在 `advance`）。"""
        request_id = command.request_id
        if not self.is_ready():
            self.stats["rejected"] += 1
            return CommandResult.rejected(request_id, [
                BlockReason(
                    Reason.SERVICE_NOT_READY,
                    u"预览缺少依赖：规划器与样板索引都必须先装配",
                )
            ])
        target, amount, reasons = parse_target(payload)
        if reasons is not None:
            self.stats["rejected"] += 1
            return CommandResult.rejected(request_id, reasons)

        source = command.source
        session_id = getattr(source, "session_id", "")
        # **先看能不能复用**：同一个会话对同一个目标、同一个数量再点一次，
        # 复活既有那一份（换一代、重新抓快照），而不是再占一个并发额度。
        # 它顺带就是预览侧的防连点——连点两下不会变成两份任务。
        existing = self._find(session_id, target, amount)
        if existing is not None:
            self._restart(existing, request_id, self._tick)
            self.stats["resumed"] += 1
            return CommandResult.ok(request_id, {
                "preview_id": existing.preview_id,
                "generation": existing.generation,
                "status": existing.status,
            })

        live = self._live_of_session(session_id)
        if len(live) >= self.policy.max_per_session:
            self.stats["rejected"] += 1
            return CommandResult.rejected(request_id, [
                BlockReason(
                    Reason.NO_CAPACITY,
                    u"这个会话已经有 %d 份预览在算（上限 %d），"
                    u"先等它们算完或关掉预览"
                    % (len(live), self.policy.max_per_session),
                )
            ])
        if len(self._jobs) >= self.policy.max_per_network:
            self.stats["rejected"] += 1
            return CommandResult.rejected(request_id, [
                BlockReason(
                    Reason.NO_CAPACITY,
                    u"这张网络已经有 %d 份预览在算（上限 %d）"
                    % (len(self._jobs), self.policy.max_per_network),
                )
            ])

        self._next += 1
        preview_id = "%s%d" % (PREVIEW_ID_PREFIX, self._next)
        job = PreviewJob(
            preview_id, request_id, source, target, amount,
            self._semantics(), self._new_plan(target, amount), self._tick,
        )
        job.network_id = self.network_id
        job.delivery = self._running_delivery(job)
        self._jobs[preview_id] = job
        self.stats["requested"] += 1
        return CommandResult.ok(request_id, {
            "preview_id": preview_id,
            "generation": job.generation,
            "status": job.status,
        })

    def cancel(self, command, payload):
        # type: (object, dict) -> CommandResult
        """取消一份预览。**只关预览，不动任何订单**（契约第 5 节）。"""
        request_id = command.request_id
        preview_id = (payload or {}).get("preview_id") or ""
        job = self._jobs.get(preview_id)
        if job is None:
            # 已经算完或被别的路径清掉了：**取消是幂等的**，不是错误。
            return CommandResult.ok(request_id, {"preview_id": preview_id})
        source = command.source
        if job.session_id and job.session_id != getattr(source, "session_id", ""):
            self.stats["refused"] += 1
            return CommandResult.rejected(request_id, [
                BlockReason(
                    Reason.SESSION_INVALID,
                    u"这份预览不属于当前会话，不能取消",
                )
            ])
        job.job.cancel(u"调用方取消了预览")
        self._drop(preview_id)
        self.stats["cancelled"] += 1
        return CommandResult.ok(request_id, {"preview_id": preview_id})

    # -- 推进与过期 ----------------------------------------------------

    def advance(self, context):
        # type: (dict) -> int
        """每帧推进一步。返回本帧花掉的配方节点数（供预算记账）。"""
        context = context or {}
        tick = int(context.get("tick") or 0)
        frame = context.get("frame")
        # **先把「现在」定下来**，再拿它去算过期与创建时刻：本帧之后到达的
        # 请求用到的就是这个数，与下一次 `advance` 之间最多差一帧。
        self._tick = tick
        spent = 0
        self._reap(tick)
        for preview_id in sorted(self._jobs):
            job = self._jobs.get(preview_id)
            if job is None or job.status != PlanStatus.RUNNING:
                continue
            job.status = job.job.step(PREVIEW_NODES_PER_TICK, frame)
            spent += PREVIEW_NODES_PER_TICK
            if job.status != PlanStatus.RUNNING:
                self._finish(job)
        return spent

    def _finish(self, job):
        # type: (object) -> None
        """算完了：把结论冻成一份交付，并计数。"""
        job.delivery = self._build_delivery(job)
        self.stats["completed"] += 1

    def _reap(self, tick):
        # type: (int) -> None
        """超时的一律清掉。**算到一半的也要清**——留着它每帧白吃一个节点。"""
        if self.policy.ttl_ticks <= 0:
            return
        for preview_id in sorted(self._jobs):
            job = self._jobs[preview_id]
            if tick - job.created_tick < self.policy.ttl_ticks:
                continue
            job.job.cancel(u"预览超时")
            self._drop(preview_id)
            self.stats["expired"] += 1

    def forget_session(self, session_id):
        # type: (str) -> int
        """会话没了：它的预览一份都不留。**返回清掉几份**（诊断用）。

        与 09 的订单不同，预览是**纯算力**：会话一走就没有任何理由继续算，
        也没有任何东西需要善后（它从没占过库存、内存或槽位）。
        """
        dropped = 0
        for preview_id in sorted(self._jobs):
            if self._jobs[preview_id].session_id == session_id:
                self._jobs[preview_id].job.cancel(u"会话已结束")
                self._drop(preview_id)
                dropped += 1
        return dropped

    # -- 交付 ----------------------------------------------------------

    def delivery_for(self, session_id):
        # type: (str) -> object
        """这条会话当前该显示的那一份交付；没有就 `None`。

        一份会话同时只显示一份预览（界面上也只有一个窗口），因此按
        `created_tick` 取最新的一份——旧的要么已经被 `_find` 复活，
        要么是玩家换了目标，显示它才是错的。
        """
        newest = None
        for preview_id in sorted(self._jobs):
            job = self._jobs[preview_id]
            if job.session_id != session_id:
                continue
            if newest is None or job.created_tick >= newest.created_tick:
                newest = job
        return None if newest is None else newest.delivery

    # -- 消费（下单） --------------------------------------------------

    def consume(self, preview_id, source, target, amount):
        # type: (str, object, object, int) -> tuple
        """下单前认领一份预览。返回 `(目标, 数量, 拒绝原因)`。

        **六条各有各的拒绝理由，不合并**：契约第 6 节的验收表要能分辨
        「预览没了」「已经用过了」「不是你的」「目标对不上」「过期了」
        「它本来就说不该下单」——合并成一句「预览无效」之后，玩家与
        排查的人都只能猜。
        """
        job = self._jobs.get(preview_id)
        if job is None:
            return None, None, [
                BlockReason(Reason.STALE_VERSION, u"这份预览已经不存在了，请重新预览")
            ]
        if job.consumed:
            return None, None, [
                BlockReason(Reason.DUPLICATE_REQUEST, u"这份预览已经用过了，请重新预览")
            ]
        session_id = getattr(source, "session_id", "")
        player_id = getattr(source, "player_id", "")
        if (job.session_id and job.session_id != session_id) or \
                (job.player_id and job.player_id != player_id):
            return None, None, [
                BlockReason(Reason.SESSION_INVALID, u"这份预览不属于当前会话")
            ]
        #: 预览是**每网络一份**的，因此「这张网络吗」由 `self.network_id`
        #: 回答；`CommandSource` 上没有网络字段（会话已经是权威，命令不再
        #: 带第二份，见 `TerminalCommand` 的说明）。运行中被退役重建时
        #: 网络标识可能变，那正是这条要拦的情形。
        if job.network_id and self.network_id and job.network_id != self.network_id:
            return None, None, [
                BlockReason(Reason.WRONG_NETWORK, u"这份预览是在另一张网络上算的")
            ]
        #: 目标与数量**必须与预览时逐字相同**。这里用的是**预览记下来的**
        #: 那一对，不是客户端这回带上来的——客户端回显只用于比对，
        #: 从不作规划输入（契约第 5 节「不把客户端返回的配方树当权威计划」）。
        if job.target.canonical() != target.canonical() or job.amount != amount:
            return None, None, [
                BlockReason(
                    Reason.BAD_PARAMETER,
                    u"下单的目标或数量与这份预览对不上（预览的是 %s × %d）"
                    % (job.target.canonical(), job.amount),
                )
            ]
        if self._tick - job.created_tick >= self.policy.ttl_ticks:
            self._drop(preview_id)
            self.stats["expired"] += 1
            return None, None, [
                BlockReason(Reason.STALE_VERSION, u"这份预览过期了，请重新预览")
            ]
        # **「还没算完」与「缺料」分开报。** 两者都拒，但一句话说不清就会把
        # 玩家引去查材料——而真相只是「等一下」。契约第 6 节的验收表要求
        # 分辨这两种情形。
        if job.status == PlanStatus.RUNNING or job.delivery is None:
            return None, None, [
                BlockReason(Reason.STALE_VERSION, u"这份预览还没算完，请稍等")
            ]
        if not job.delivery["state"]["submit_ok"]:
            return None, None, [
                BlockReason(
                    Reason.INSUFFICIENT_STOCK, job.delivery["state"]["detail"]
                )
            ]
        if self.policy.consume_on_first_attempt:
            job.consumed = True
        self.stats["consumed"] += 1
        return job.target, job.amount, None

    def is_consumed(self, preview_id):
        # type: (str) -> bool
        """这份预览被用掉了没有。**下单流水线在真的建单之前再问一次**。"""
        job = self._jobs.get(preview_id)
        return job is not None and job.consumed

    # -- 内部 ----------------------------------------------------------

    def _semantics(self):
        # type: () -> str
        """R11 的目标语义。取不到就报**未定**的保守侧。"""
        policy = getattr(self.planner, "policy", None)
        return getattr(policy, "target_semantics", "hold")

    def _new_plan(self, target, amount):
        # type: (object, int) -> object
        """抓两份快照，起一个**还没推进**的规划任务。

        快照走的是 09 同一条路（`capture_inventory` / `capture_patterns`），
        因此「预览看见的世界」与「下单看见的世界」是同一份口径。
        """
        topology_version = getattr(getattr(self.runtime, "topology", None), "version", None)
        storage = self._storage_of()
        return self.planner.plan(
            target,
            amount,
            self.planner.capture_inventory(storage, topology_version),
            self.planner.capture_patterns(self.patterns, topology_version),
        )

    def _restart(self, job, request_id, tick):
        # type: (object, str, int) -> None
        """复活一份既有预览：换一代、重新抓快照、重新算。"""
        job.request_id = request_id
        job.job.cancel(u"同一目标的第二次预览请求，重新计算")
        job.job = self._new_plan(job.target, job.amount)
        job.generation += 1
        job.created_tick = tick
        job.status = job.job.status()
        job.consumed = False
        job.delivery = self._running_delivery(job)

    def _find(self, session_id, target, amount):
        # type: (str, object, int) -> object
        """同一个会话对同一个目标、同一个数量的**可复活**预览。"""
        canonical = target.canonical()
        for preview_id in sorted(self._jobs, reverse=True):
            job = self._jobs[preview_id]
            if job.session_id != session_id or job.consumed:
                continue
            if job.target.canonical() == canonical and job.amount == amount:
                return job
        return None

    def _live_of_session(self, session_id):
        # type: (str) -> list
        return [
            self._jobs[key] for key in sorted(self._jobs)
            if self._jobs[key].session_id == session_id
        ]

    def _drop(self, preview_id):
        # type: (str) -> None
        self._jobs.pop(preview_id, None)

    # -- 交付内容 ------------------------------------------------------

    def _base_state(self, job):
        # type: (object) -> dict
        """状态头里与算法进度无关的那一半。 """
        return {
            "request_id": job.request_id,
            "preview_id": job.preview_id,
            "generation": job.generation,
            "status": job.status,
            "detail": job.blocked_reason,
            "submit_ok": False,
            "target": {
                "canonical": job.target.canonical(),
                "name": job.target.name,
                "aux": job.target.aux or 0,
            },
            "count": job.amount,
            "semantics": job.semantics,
            "inventory_version": self._inventory_version(),
            "topology_version": self._topology_version(),
            "plan_digest": "",
            "row_count": 0,
            "packet_count": 0,
            "missing": False,
            "blocked": [],
            "truncated": False,
            "spent": job.job.spent(),
        }

    def _running_delivery(self, job):
        # type: (object) -> dict
        """还在算时的那一份：**只有状态头，没有行**。

        客户端据此保持 loading 可见（契约第 3 节），因此它必须在请求
        被受理的那一刻就能发出去，而不是等算完。
        """
        state = self._base_state(job)
        state["detail"] = u"正在计算订单所需的原料"
        #: `chunks` 而不是 `rows`：**分批只有这一个权威**。让 hub 拿行自己
        #: 再切一次，两边的批大小就会在边界上分家，而那种偏差的表现是
        #: 「客户端永远在等最后一片」——状态头报一个数、实际发另一个数。
        return {"state": state, "chunks": ()}

    def _build_delivery(self, job):
        # type: (object) -> dict
        """算完之后的交付：状态头 + 原料行（已按上限截断）。"""
        state = self._base_state(job)
        plan = job.job.result()
        if plan is None:
            # 取消或被求解上限中止。**都不是「没有缺料」**：明确说出来，
            # 否则界面会显示一份空的「所需原料」并让人以为可以下单。
            state["detail"] = u"这次预览没能算出一份完整的计划，请重新预览"
            state["submit_ok"] = False
            return {"state": state, "chunks": ()}
        state["plan_digest"] = plan.digest()
        rows, blocked, truncated = self._rows_of(job, plan)
        state["row_count"] = len(rows)
        state["missing"] = bool(blocked)
        state["blocked"] = blocked
        state["truncated"] = truncated
        state["spent"] = job.job.spent()
        state["detail"] = self._detail_of(job, plan, blocked, truncated)
        state["submit_ok"] = bool(
            job.status == PlanStatus.COMPLETE and not blocked and not truncated
        )
        chunks = self._chunks_of(rows)
        state["packet_count"] = len(chunks)
        return {"state": state, "chunks": chunks}

    def _chunks_of(self, rows):
        # type: (tuple) -> tuple
        """把原料行切成若干批。**空表切出零批**，不是一批空行——
        一批空行会让客户端「收齐 1 片」之后显示一份空白的原料表。"""
        size = self.policy.chunk_items
        if not rows or size <= 0:
            return ()
        return tuple(
            tuple(rows[index:index + size])
            for index in range(0, len(rows), size)
        )

    def _rows_of(self, job, plan):
        # type: (object, object) -> tuple
        """原料行、缺料结论、有没有被截断。

        **行取自账本而不是 `plan.materials`**：后者把键压成了名字串，
        而客户端画图标要完整身份（`canonical` 与 `name` / `aux`）。
        最终目标不是原料，不进入原料行；整单阻塞仍按完整计划判定。
        demand <= 0 的行是**纯副产物**——它出现在账本里是因为别的配方
        顺手产了它，那不是一条需求，显示出来只会让人以为要多备料。
        """
        blocked_by_canonical = {}
        for entry in plan.blocked_keys:
            blocked_by_canonical[entry["canonical"]] = entry
        parents = {}
        for parent, child, _count in job.job.edges():
            parents.setdefault(child.canonical(), set()).add(parent.canonical())
        rows = []
        for row in job.job.ledger():
            if row.demand <= 0 or row.key == job.target:
                continue
            canonical = row.key.canonical()
            entry = blocked_by_canonical.get(canonical)
            rows.append({
                "canonical": canonical,
                "name": row.key.name,
                "aux": row.key.aux or 0,
                "demand": int(row.demand),
                #: **本计划分配到的库存份额**，不是全网可用——同一份库存
                #: 不会在两行里各显示一遍（契约第 4 节末段）。
                "allocated": int(row.from_stock),
                "planned": int(row.own_output),
                "missing": entry is not None,
                "shortfall": int(entry["shortfall"]) if entry is not None else 0,
                "sources": sorted(parents.get(canonical, ())),
            })
        truncated = len(rows) > self.policy.max_rows
        if truncated:
            rows = rows[: self.policy.max_rows]
        blocked = [
            {"canonical": entry["canonical"], "detail": entry["detail"]}
            for entry in plan.blocked_keys
        ]
        return tuple(rows), blocked, truncated

    def _detail_of(self, job, plan, blocked, truncated):
        # type: (object, object, list, bool) -> str
        """给人看的一句话。**不把三种阻塞混成一种**（契约第 6 节）。"""
        if truncated:
            return u"原料行超过 %d 行，只显示了前 %d 行，因此不能下单" % (
                self.policy.max_rows, self.policy.max_rows,
            )
        if blocked:
            return u"有 %d 种原料凑不齐，不能下单" % (len(blocked),)
        if job.status == PlanStatus.COMPLETE:
            return u"原料齐备，可以下单"
        progress = job.job.progress()
        return progress.get("detail") or u"这份计划不能下单"

    def _inventory_version(self):
        # type: () -> int
        storage = self._storage_of()
        return int(getattr(storage, "index_version", 0) or 0)

    def _topology_version(self):
        # type: () -> int
        topology = getattr(self.runtime, "topology", None)
        return int(getattr(topology, "version", 0) or 0)

    # -- 诊断 ----------------------------------------------------------

    def diagnostics(self):
        # type: () -> dict
        return {
            "live": len(self._jobs),
            "policy": self.policy.describe(),
            "stats": dict(self.stats),
        }

    def __repr__(self):
        return "PreviewService(%d 份预览)" % (len(self._jobs),)
