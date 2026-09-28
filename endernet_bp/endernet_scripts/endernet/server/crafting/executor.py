# coding=utf-8
"""订单执行器与工厂服务门面（任务 09-02 / 09-04 / 09-06 / 09-07 / 09-08）。

模块分工见 `crafting/__init__.py`：`jobs.py` 是纯领域的订单与台账，
本模块是**唯一会动真实资源**的那一层，也是 `ServiceId.FACTORY` 的服务实现。

## 一、提交时：计划不是预留

08 交付说明第六节钉死的那条在这里落地：

    提交时必须先 revalidate()，通过后按 seal["claims"] 逐个 reserve()，
    预留成功才建订单。

顺序不能反：05 的可用量是 `stored - reserved`，而 `reserved` **含本订单
自己的预留**。同理，**备料**时也要先把本次要取的那部分预留放掉、取完再把
没搬走的补回来——那条链完整地写在 `_stage_next_input()` 的 docstring 里，
它是本模块最容易写错的一处。

## 二、资源键从哪来

计划的 `steps[].inputs[].key` 是名字字符串（08 的计划要能 `json.dumps`）。
执行侧要的活 `ResourceKey` **只从复核通过的样板定义里取**
（`check_dispatch` 返回的 `entry.definition`），并用 `canonical` 与计划对齐。
对不上就拒绝——`ResourceKey` 没有「从 canonical 反解」的入口，也不该有：
反解会经过身份归一化，解出来的可能不是原来那个键（`resource_identity.md`）。

## 三、回流（任务 10）没接线时不许假装成功

步骤投料完成后登记等待台账（本模块 + `ReturnsSink`），产物只有真的**进了
网络管理的存储**（10 的分配回执）才会把这一步算成收到。库默认注入的是
`AbsentReturns`：它把登记记下来、明确回答「我没接上」，订单因此停在
等待产物并报 `ORDER_RETURNS_ABSENT`。**这不是失败，也不是缺料**——
是那一半还没实现，而诊断里必须一眼看出来。

离线测试注入具名假回流来驱动状态机（09 文档明写允许）；正式可玩验收
要等 10 把 `crafting/returns.py` 写出来并接上同一个 `receive_output`。
"""

from ...common.identity import NodeKey
from ...common.protocol import (
    BlockReason,
    CommandKind,
    CommandResult,
    CommandSource,
    DeviceCommand,
    Reason,
)
from ...common.resource_key import ResourceKey
from ..network.services import NetworkService, ServiceId
from .capacity import (
    MemoryLedger,
    OrderPolicy,
    SlotLedger,
    memory_admission_blockers,
    memory_capacity,
    slot_capacity,
    slot_starvation_blocker,
)
from .jobs import (
    Order,
    OrderStatus,
    OrderStep,
    OrderStore,
    RejectedRequestStore,
    StepStatus,
    WaitingLedger,
)

#: 本模组样板接口的角色名（06 的 `devices/io.py::TEMPLATE`）。由装配层注入，
#: `crafting/` 不 import 设备模块——注入而不是 import 的理由与 08 把
#: `check_dispatch` 做成鸭子类型是同一条：少一个反向依赖，
#: 离线夹具就能只造它需要的那一小块。
DEFAULT_TEMPLATE_ROLE = "template"

#: 装配仓的分类名（`definitions.CATEGORY_ASSEMBLY`）。它的执行半边是 07-06。
CATEGORY_ASSEMBLY = "assembly"

#: 每个订单每帧最多计算几个**配方节点**。
#:
#: **它是规则，不是可调参数。** [订单规划与执行规则对齐] 原文：
#: 「每订单每 tick 只计算 1 个配方节点，子树最早下一 tick 开始」，
#: 并且明确「不能简单把通用工作量预算设成 1 就宣称符合，工作单位必须与
#: 配方节点对应」。因此它**刻意不做成策略字段**——一个叫
#: `plan_nodes_per_tick` 的旋钮会让人以为调大它就是优化，而调大它
#: 会把「逐节点分帧」这条规则本身改掉。
PLAN_NODES_PER_TICK = 1

#: **可重试**的障碍：它们不把订单打成 `BLOCKED`。
#:
#: 判据是「再试一次有没有意义」——外部机器这次没接收、另一条订单正占着
#: 批次槽，两者下一秒就可能变，而执行器下一帧本来就会再试一次。
#: 把它们打成 `BLOCKED` 的代价是订单看起来停了，而它其实一直在动；
#: 更糟的是恢复要等一次重新判定，而重判的节奏由别人定（接口的轮询退避
#: 默认 20 帧起步、上限 200 帧），于是「投料要等好几秒」会被读成「卡住了」。
#:
#: 结构性的障碍（提供者被拆、接口没配置、装配仓没实现、料被别人拿走）
#: **照旧阻塞**：那些不会自己好，必须让人看见。
RETRYABLE_CODES = frozenset(
    [Reason.ORDER_BATCH_STALLED, Reason.ORDER_PORT_BUSY]
)


class ReturnsSink(object):
    """任务 10 的接收侧契约（**鸭子类型**）。

    实现是 `crafting/returns.py` 的 `ReturnsPort`——它**不继承本类**（契约
    明写这里是鸭子类型，因此两个模块互不 import，没有任何环）：它维护
    「资源键 → 等待执行实例」的索引与每实例每产物的剩余需求，按 R06 的顺序
    把真实回执分配下去，再调 `OrderExecutor.receive_output()` 把分配结果
    交回来。

    本类只作为**形状的声明**保留：`ReturnsSink` 的实例化对象仍然不可用。

    三个方法就是两边的全部接缝：

      - `register(execution_id, expectations, order_id)` —— 09 在**派发确认后**
        登记（10-01：「仅登记已经确认派发的执行实例」）；
      - `unregister(execution_id)` —— 09 在取消或完成时注销（10-07：
        「由 09 注销等待记录，避免回调命中已取消实例」）；
      - `describe()` —— 诊断，`available` 为假表示这一半还没接上。
    """

    adapter_id = ""

    def register(self, execution_id, expectations, order_id=""):
        # type: (str, list, str) -> bool
        raise NotImplementedError

    def unregister(self, execution_id):
        # type: (str) -> bool
        raise NotImplementedError

    def describe(self):
        # type: () -> dict
        raise NotImplementedError


class AbsentReturns(ReturnsSink):
    """回流入口**没接上**时的**具名替身**，也是库默认装配的那一个。

    10 的实现（`crafting/returns.py` 的 `ReturnsPort`）已经落地，但**库默认
    不把它接上**：`ServerBootstrap` 不传 `returns_sink` 时仍是本类，
    `runtime.order_view()["returns_absent"]` 与一整套用例都钉在
    「库默认不假装能用」上。游戏内由 `start_server()` 显式装配。

    **它不假装能用**：`register` 返回 `False`，并把登记内容原样收下，
    好让诊断能回答「如果 10 在，它会看到哪些等待者」。订单因此停在等待产物
    并报 `ORDER_RETURNS_ABSENT`——那是「这条装配没把 10 接上」，
    与「机器没出货」「缺料」都是不同的话。

    返回 `False` 而不是抛错：登记本身不该让订单提交失败。任务文档要的是
    「初期可用假回流验证状态机」，而假回流是**测试注入**的，
    不是让库默认口径假装接通。
    """

    adapter_id = "absent"
    pending_task = "10"

    def __init__(self):
        # type: () -> None
        self.registered = {}
        self.stats = {"registered": 0, "unregistered": 0, "refused": 0}

    def register(self, execution_id, expectations, order_id=""):
        # type: (str, list, str) -> bool
        self.registered[execution_id] = {
            "order_id": order_id,
            "expected": dict(
                (item.canonical, item.expected) for item in (expectations or ())
            ),
        }
        self.stats["registered"] += 1
        self.stats["refused"] += 1
        return False

    def unregister(self, execution_id):
        # type: (str) -> bool
        if execution_id not in self.registered:
            return False
        self.registered.pop(execution_id, None)
        self.stats["unregistered"] += 1
        return True

    def blocker(self):
        # type: () -> BlockReason
        return BlockReason(
            Reason.ORDER_RETURNS_ABSENT,
            u"产物回流入口尚未接线（任务 10 已实现，但这条装配没注入 "
            u"returns_sink）：步骤已经投料完毕、正在等产物，"
            u"但没有任何东西会把入库的资源分配给订单；"
            u"当前登记着 %d 个等待实例" % (len(self.registered),),
        )

    def describe(self):
        # type: () -> dict
        return {
            "adapter_id": self.adapter_id,
            "available": False,
            "pending_task": self.pending_task,
            "registered": len(self.registered),
            "stats": dict(self.stats),
        }


class OrderExecutor(object):
    """把一张订单从计划推到完成（或推到它真的走不动的地方）。

    **它不持有状态**：订单、预留、等待台账都在门面（`FactoryService`）那边，
    本对象只是一组动作。这样做的理由是取消与恢复都要**从订单自己的账**出发
    去释放资源，而不是从执行器的临时字段出发——后者会随重建丢失。
    """

    def __init__(self, service):
        # type: (object) -> None
        self.service = service

    # -- 便捷读法 ------------------------------------------------------

    @property
    def policy(self):
        return self.service.policy

    @property
    def orders(self):
        return self.service.orders

    @property
    def waiting(self):
        return self.service.waiting

    @property
    def runtime(self):
        return self.service.runtime

    def _storage(self):
        # type: () -> object
        """本网络正在跑的存储服务。

        **不经许可**：服务之间互相取用走注册表，这是 03 给服务间协作定的路
        （`StorageService._scheduler()` 取调度器用的是同一条）。
        业务命令才需要主控签发的许可。
        """
        runtime = self.runtime
        if runtime is None:
            return None
        return runtime.services.get(ServiceId.STORAGE)

    def _store(self):
        # type: () -> object
        """07 的样板索引（全进程一份，由装配层注入）。"""
        return self.service.patterns

    def _planner(self):
        # type: () -> object
        return self.service.planner

    def _master_instance(self):
        # type: () -> str
        master = getattr(self.runtime, "master", None)
        return master.instance_id if master is not None else ""

    def _topology_version(self):
        # type: () -> object
        topology = getattr(self.runtime, "topology", None)
        return getattr(topology, "version", None)

    def _inventory_version(self):
        # type: () -> object
        storage = self._storage()
        return getattr(storage, "index_version", None) if storage is not None else None

    # -- 提交（09-02）--------------------------------------------------

    def submit_order(self, command, payload):
        # type: (object, dict) -> CommandResult
        """受理一条下单命令。"""
        request_id = command.request_id
        target, amount, reasons = _read_target(payload)
        if reasons is not None:
            return CommandResult.rejected(request_id, reasons)

        service = self.service
        runtime = service.runtime
        if runtime is None or service.orders is None:
            return _blocked(
                request_id,
                Reason.SERVICE_NOT_READY,
                u"工厂服务尚未绑定到任何网络运行时",
            )
        if self._storage() is None or self._planner() is None or self._store() is None:
            return _blocked(
                request_id,
                Reason.SERVICE_NOT_READY,
                u"工厂缺少依赖：规划器、样板索引与存储服务都必须先装配",
            )
        live = service.orders.live_count()
        if live >= self.policy.max_orders:
            return _blocked(
                request_id,
                Reason.ORDER_NO_CAPACITY,
                u"这张网络已经有 %d 张未完成的订单（上限 %d）；"
                u"先完成或取消一些再下单" % (live, self.policy.max_orders),
            )

        tick = int(getattr(command.source, "received_tick", 0) or 0)
        order = Order(
            service.orders.new_id(self._master_instance()),
            runtime.component_id,
            self._master_instance(),
            target,
            target.canonical(),
            amount,
            tick,
            request_id=request_id,
        )
        service.orders.add(order)
        self._start_planning(order, tick)

        if order.status in (OrderStatus.FAILED, OrderStatus.CANCELLED):
            # 计划同步就失败了：**不留下这条订单**——一张从来没有过预留、
            # 也永远不会被推进的订单记录，在 11 的列表里与一张真的排着队的
            # 订单长得一模一样。
            reasons = list(order.blockers) or [
                BlockReason(Reason.INTERNAL, u"订单在提交阶段就失败了")
            ]
            service.orders.remove(order.order_id)
            return CommandResult.rejected(request_id, reasons)
        # **注意这里只排除了「失败」与「已取消」。** 零步骤的 `HOLD` 计划
        # 会**在提交这一帧就完结**（`status == COMPLETED`），而它是一次
        # 成功——把它也当成失败丢掉，表现是玩家下单「做 1 个铁块」、
        # 库里刚好有，却收到一条 `internal_error`。
        return CommandResult.ok(request_id, order.to_dict())

    def _start_planning(self, order, tick):
        # type: (object, int) -> None
        """抓两份快照、起一个可继续推进的 `PlanJob`，并吃掉第一口预算。"""
        planner = self._planner()
        topology_version = self._topology_version()
        inventory = planner.capture_inventory(self._storage(), topology_version)
        patterns = planner.capture_patterns(self._store(), topology_version)
        order.plan_job = planner.plan(order.target, order.amount, inventory, patterns)
        order.inventory_version = inventory.version
        order.topology_version = topology_version
        order.stats["plans"] += 1
        # **提交这一帧不吃预算。** 规则：下单请求先进入「规划中」，
        # 此时它还不是通过准入的订单；节点从**下一帧**起每帧算一个。
        # 于是「每订单每 tick 1 个节点」是字面成立的，而不是
        # 「提交一帧 + 每帧一个」这种没人说得清的口径。

    def _step_plan(self, order, nodes, tick, frame=None):
        # type: (object, int, int, object) -> bool
        """推一口计划（`nodes` 个**配方节点**）。返回「这一步之后订单还活着吗」。

        帧号要传下去：调度器会在同一帧里把同一个运行时访问两次，
        而规则要求「同 tick 的重复调度不能额外计算节点」。
        """
        job = order.plan_job
        if job is None:
            # 「正在规划」却没有规划任务，只能是状态机上有别的地方没接好
            # （真机上就是这么撞出来的：零步骤的 HOLD 计划走了一条**不在
            # 迁移表里**的边，`move_to` 静默返回 False，于是订单停在这里
            # 空转——而它的账看起来全都对）。**如实判失败**，
            # 不要让它无声地转下去：那比一条明确的错误难查得多。
            if order.status == OrderStatus.PLANNING:
                order.blockers = [
                    BlockReason(Reason.INTERNAL, u"订单停在规划中，却没有可推进的计划")
                ]
                self._fail(order, u"规划状态与计划任务不一致")
                return False
            return not order.is_terminal()
        if job.status() == "running":
            job.step(nodes, frame)
        if job.status() == "running":
            order.mark(tick)
            order.status_detail = u"正在计算计划（已算 %d 个配方节点）" % (job.spent(),)
            return True
        plan = job.result()
        order.plan_job = None
        if plan is None:
            # 取消或超出求解上限。两者都不是「做不出来」，但也都不能继续。
            progress = job.progress()
            detail = progress.get("detail") or progress.get("reason") or u"计划求解被中止"
            return self._reject_request(
                order,
                [BlockReason(Reason.PLAN_LIMIT_REACHED, detail)],
                tick,
            )
        return self._adopt_plan(order, plan, tick)

    # -- 计划落地（09-02）----------------------------------------------

    def _adopt_plan(self, order, plan, tick):
        # type: (object, object, int) -> bool
        """复核、建步骤、预留、租约，然后把订单排进队列。

        **每一步失败都不留半份资源**：预留是逐个做的，任何一步返回 `None`
        都会把已经留到的逐个放回去。留一半的后果是「同一种料被两个订单各锁
        一半」，而两份订单谁也开不了工。
        """
        if plan.status != "complete":
            # 规划失败的原因码由 08 原样透出（缺料 / 无样板 / 超限），
            # 不在这里压成一句「订单失败」——那几种要教玩家做的事完全不同。
            # 成环**不在这一列**：2026-09-26 起规划器把「候选会绕回自己」
            # 折进缺料（`plan_missing_materials`），环路只作诊断留在 detail 里。
            reasons = list(plan.reasons()) or [
                BlockReason(Reason.PLAN_LIMIT_REACHED, u"计划不是一份可执行的计划")
            ]
            reasons.extend(self._taken_stock_reasons(plan))
            return self._reject_request(order, reasons, tick)
        if plan.blocked_keys:
            # 08 明文交接：`blocked_keys` 非空就整份拒绝。
            # **不能只看 `status`**：`status` 为 complete 而某些步骤执行不了的
            # 组合是可能的（副产物把上游喂饱了），那时照旧不能开工。
            return self._reject_request(
                order, [_blocked_keys_reason(plan)], tick
            )

        topology_version = self._topology_version()
        fresh = self._planner().capture_inventory(self._storage(), topology_version)
        captured = {}

        def check_dispatch(ref, slot, digest, mode):
            # type: (object, int, str, str) -> tuple
            """转发给 07，**顺手把复核通过的那块样板留下来**。

            留它是因为执行侧要活的 `ResourceKey`，而计划里只有名字字符串
            （见模块 docstring 第二节）。这样只走一遍 `revalidate` 的样板腿，
            不会出现「复核看到的」与「派发用的」是两次不同读数的情形。
            """
            entry, blocker = self._store().check_dispatch(ref, slot, digest, mode)
            if blocker is None:
                captured[(ref.node_key.as_key(), ref.generation, slot)] = entry
            return entry, blocker

        report = plan.revalidate(check_dispatch, fresh, topology_version)
        order.inventory_version = fresh.version
        order.topology_version = topology_version
        order.stats["revalidations"] += 1
        if not report.ok():
            order.blockers = list(report.reasons) or [
                BlockReason(Reason.PLAN_STOCK_CHANGED, u"复核没有通过")
            ]
            if not all(report.checked.values()):
                # 「没查」与「查过了没问题」在报告里长得一样（`ok()` 会把它
                # 算成不通过，但原因列表可能是空的）。这里补一条，
                # 否则玩家看到的是一张没有任何原因的失败订单。
                unsaid = [
                    name for name, done in sorted(report.checked.items()) if not done
                ]
                order.blockers.append(
                    BlockReason(
                        Reason.PLAN_STOCK_CHANGED,
                        u"复核有 %d 条腿没有真的跑起来（%s）"
                        % (len(unsaid), u"、".join(unsaid)),
                    )
                )
            return self._reject_request(order, order.blockers, tick)

        if not self._build_steps(order, plan, captured):
            return self._reject_request(
                order,
                [
                    BlockReason(
                        Reason.PATTERN_STALE,
                        u"计划引用的样板内容与计划对不上，取不到可信的资源键",
                    )
                ],
                tick,
            )

        # 计划与步骤**当场挂上**，后面的几步（容量、预留、租约）由
        # `_finish_adoption` 走，而且**可以重来**：容量不足时会停在
        # `BLOCKED` 上等槽位空出来，那时订单已经有计划、有步骤，只是还没有
        # 材料与租约。把「落地」拆成两截就是为了这个——不拆的话，
        # 「阻塞解除之后接着走」会从一个没有计划的状态开始。
        order.plan = plan
        order.plan_digest = plan.digest()
        order.blockers = []
        if not order.steps:
            # `HOLD` 口径下「库里已经够了」得到的是**零步骤但 COMPLETE** 的
            # 计划（08 交付说明第五节）。这不是什么都没做，是「已在库中满足」。
            order.adopted = True
            order.move_to(
                OrderStatus.COMPLETED, u"库存已满足：计划没有需要生产的步骤", tick
            )
            self._release_everything(order)
            self.orders.stats["completed"] += 1
            self.service.stats["completed"] += 1
            return False
        return self._finish_adoption(order, tick)

    def _finish_adoption(self, order, tick):
        # type: (object, int) -> bool
        """把一份**已经挂上计划**的订单推到 `QUEUED`。**可重复调用。**

        只有一种情况会停在这里：**材料被别的订单锁着**（`_reserve` 返回假），
        那种情况会自己好，因此停在 `BLOCKED` 等。**内存不足不再停**——R04
        要的是「拒绝下达，不转为自动等待内存的已接受订单」（见
        `memory_admission_blockers`）：一张排在队列里等内存的订单，与一张
        真的排着队的订单，在界面上长得一模一样。

        **准入不看槽位。** 此刻一个执行实例都还没派；槽位的计费单位是
        「一次配方执行」，它是**派发期**的竞争资源——派不出去就留在运行中
        等下一帧（见 `slot_starvation_blocker`），不是拒绝下单的理由。
        """
        self.refresh_capacity()
        need = int(order.plan.required_bytes)
        blockers = memory_admission_blockers(self.service.memory, need)
        if blockers:
            # **先判再领**，不留「领了又退」的窗口。
            return self._reject_request(order, blockers, tick)

        # `required_bytes` 可以是 0（叶子全被库存或外部输入覆盖），
        # 那时仍要领一条 0 单位的租约：「每一个通过准入的订单都有一条内存
        # 租约」是诊断与恢复要用的不变式（见 `_Ledger.claim`）。
        memory_lease = self.service.memory.claim_order_bytes(order.order_id, need)
        if memory_lease is None:
            # 复核与领用之间被别的订单抢走了。**同样拒绝**：规则明确不要
            # 「已接受但缺内存、自动等待」的那种订单。
            return self._reject_request(
                order,
                [
                    BlockReason(
                        Reason.ORDER_NO_MEMORY,
                        u"内存字节在这几帧里被别的订单拿走了（要 %d B，可用 %d B）"
                        % (need, self.service.memory.available()),
                    )
                ],
                tick,
            )

        if not self._reserve(order, order.plan.seal.get("claims") or (), tick):
            # 材料被锁住：**把刚领的内存退回去**再停。留着的话「一份没落地的
            # 订单持有一份内存租约」就成了一条会长期存在的状态，而
            # `adopted` 为假、界面上看不出它占了内存。
            self.service.memory.release(order.order_id)
            return False

        order.memory_bytes = need
        order.memory_lease = memory_lease
        order.tasks_total = int(order.plan.total_recipe_tasks)
        order.adopted = True
        order.blockers = []
        order.move_to(OrderStatus.QUEUED, u"已预留材料并取得订单内存", tick)
        return True

    def _reserve(self, order, claims, tick):
        # type: (object, object, list, int) -> bool
        """按 `claims` 逐个预留。**要么全留、要么全不留。**"""
        storage = self._storage()
        done = []
        for claim in claims:
            canonical = claim.get("canonical")
            count = int(claim.get("count") or 0)
            if count <= 0:
                continue
            key = self._key_of_claim(order, canonical)
            if key is None:
                # 计划说要用某种料，但没有任何一步的样板认这个键。
                # 继续下去就是拿一个来路不明的数量去锁别人的库存。
                self._unreserve(done)
                return self._reject_request(
                    order,
                    [
                        BlockReason(
                            Reason.PATTERN_STALE,
                            u"计划的材料清单里有 %s，但没有任何一步的样板认这个键；"
                            u"不去锁一份来路不明的库存" % (canonical,),
                        )
                    ],
                    tick,
                )
            reservation = storage.reserve(order.order_id, key, count, tick)
            if reservation is None:
                self._unreserve(done)
                row = storage.query(key)
                order.blockers = [
                    BlockReason(
                        Reason.ORDER_STOCK_TAKEN,
                        u"%s 还差 %d 个：网络里有 %d 个，但 %d 个已经被别的订单"
                        u"锁住了（可用 %d）"
                        % (
                            key.name,
                            count,
                            row.get("stored", 0),
                            row.get("reserved", 0),
                            row.get("available", 0),
                        ),
                    )
                ]
                order.move_to(OrderStatus.BLOCKED, u"材料已被别的订单预留", tick)
                return False
            order.reservations[canonical] = {
                "reservation_id": reservation.reservation_id,
                "key": key,
                "claimed": count,
                "reserved": count,
            }
            done.append(reservation.reservation_id)
            order.stats["reserves"] += 1
        return True

    def _taken_stock_reasons(self, plan):
        # type: (object) -> list
        """计划缺料时，**分清「网络里没有」与「被别人锁着」**。

        08 的规划器只看得到 `available = stored - reserved`，因此
        「这一份被别人预留了」在它眼里与「根本没有」长得一模一样，
        报出来的都是「去补基础原料」。而玩家该做的事正相反：
        一种是去挖矿，另一种是等前面那单做完。

        这里**补一条、不替换** 08 的原因——两条一起给，读的人自己判断。

        **活的资源键从 05 的索引拿（`keys()` 返回的就是它们）**，
        与计划里的 `canonical` 对齐。刻意不去反解计划里的名字：
        反解会经过身份归一化，解出来的可能不是原来那个键
        （`resource_identity.md` 第 2 节），而这条补注本来只是诊断。
        """
        storage = self._storage()
        if storage is None:
            return []
        wanted = set(
            gap.get("canonical") for gap in (plan.gaps or ()) if gap.get("canonical")
        )
        if not wanted:
            return []
        out = []
        for key in storage.keys():
            canonical = key.canonical()
            if canonical not in wanted:
                continue
            row = storage.query(key)
            if int(row.get("reserved", 0)) > 0 and int(row.get("available", 0)) <= 0:
                out.append(
                    BlockReason(
                        Reason.ORDER_STOCK_TAKEN,
                        u"%s 网络里有 %d 个，但都被别的订单锁住了（可用 %d）——"
                        u"这一份不用去补，等前面那单做完"
                        % (key.name, row.get("stored", 0), row.get("available", 0)),
                    )
                )
        return out

    def _key_of_claim(self, order, canonical):
        # type: (object, str) -> object
        """把一条 `claims` 的规范文本换成活的 `ResourceKey`。

        **来源只有两处，都必须是活的键**：某一步的输入（这是绝大多数情形）、
        或者订单的目标本身。计划内容里的键已经被 `to_dict()` 降成名字，
        而名字反解回键会经过身份归一化——解出来的可能不是原来那个键
        （`resource_identity.md` 第 2 节），所以那条路不走。
        """
        for step in order.steps:
            for item_canonical, _per_batch in step.input_specs:
                if item_canonical == canonical:
                    key = step.keys.get(canonical)
                    if key is not None:
                        return key
        if order.canonical == canonical:
            return order.target
        return None

    def _unreserve(self, reservation_ids):
        # type: (list) -> int
        storage = self._storage()
        total = 0
        for reservation_id in reservation_ids:
            total += storage.release(reservation_id)
        return total

    def _release_reservations(self, order):
        # type: (object) -> int
        """放掉订单名下**全部**预留，并把订单自己的账清零。

        用 `release_owned_by` 而不是逐条 `release`：订单的本地账可能因为
        备料时的部分释放与补回而与 05 那边对不上，而**所有者索引是唯一的
        真相**。以它为准，「放干净」这件事才不依赖本地账没错。
        """
        released = self._storage().release_owned_by(order.order_id)
        for row in order.reservations.values():
            row["reserved"] = 0
            row["reservation_id"] = None
        order.stats["releases"] += released
        return released

    def _release_everything(self, order):
        # type: (object) -> None
        """释放预留与两份租约。完成、取消、失败三条路都走它。

        **槽位要逐个实例释放**：拥有者是执行实例 ID，不是订单 ID。
        按订单 ID 放一次会静默地一个都放不掉（`_Ledger.release` 对未知的
        拥有者返回 0 且不报错），而症状是「槽位再也回不来」——一张订单
        结单之后网络凭空少几个槽位，账面上查不出少在哪。

        内存仍然按订单一次放光：它的拥有者就是订单（准入时一次性定值）。
        """
        self._release_reservations(order)
        self._release_slots(order)
        self.service.memory.release(order.order_id)
        order.memory_lease = None

    def _release_slots(self, order):
        # type: (object) -> int
        """把订单名下**全部在途实例**的槽位放掉，返回放掉的个数。"""
        released = 0
        for instance in order.inflight_executions():
            self.service.slots.release_slot(instance.execution_id)
            released += 1
        order.inflight = {}
        return released

    def _reject_request(self, order, reasons, tick):
        # type: (object, tuple, int) -> bool
        """这条请求**根本不成立**：记一条拒绝记录，然后把它从订单台账里丢掉。

        规则（2026-09-26）：「拒绝下达后保留一条独立的拒绝记录。保存目标、
        数量、拒绝原因、时间和请求 ID；**不占内存、并行槽或活跃订单名额**，
        不自动重试。」

        ## 为什么是「丢掉」而不是「标成失败」

        它从来没成为订单：准入前的请求手上**一个执行资源都没有**（内存与
        槽位是在准入通过之后才领的），因此没有东西需要清理。把它留在订单
        台账里反而有害——那张表数的是「活跃订单名额」，而一条被拒的请求
        不该占着它。

        ## 为什么必须留下记录

        规划是分帧的，所以「拒绝」是**异步**发生的：玩家点下按钮时命令先返回
        「规划中」，几帧之后才知道不成立，那时人多半已经离开界面了。
        只剩一个统计数字等于没有结果。

        **预留要回滚**：它是准入前唯一可能已经拿到手的资源（`_reserve`
        逐条做，可能成功几条之后才失败）。

        ## 未定案：内存不足是否也该留一条拒绝记录

        09-02 明写「拒绝记录保留方式和其他失败终态**另行确认**」。本轮
        **内存不足与缺料共用这一个入口**，因此它同样会落一条记录——但这
        只是沿用既有形状，**不是已定案的口径**。两者是否该有区别（例如
        内存不足只回一条即时错误、不占近期记录的名额），要等 09-02 那句
        「另行确认」有结论之后才能写死。**不得把当前行为当成定案**
        （全局约束 7）。
        """
        reasons = tuple(reasons or ())
        self._release_reservations(order)
        self.service.rejected.add(
            # **记玩家那条命令的请求 ID，不是订单 ID。** 记录的意义就是
            # 「几帧之后告诉玩家他刚才那一下不成立」，而订单 ID 是引擎内部
            # 的名字，玩家手上没有它（追加决定 2 的原话是「请求 ID」）。
            order.request_id or order.order_id,
            order.target.name,
            order.canonical,
            order.amount,
            reasons,
            tick,
        )
        self.orders.discard(order.order_id)
        self.orders.stats["refused"] += 1
        self.service.stats["rejected"] += 1
        return False

    def _fail(self, order, detail):
        # type: (object, str) -> None
        """把订单判为失败，并把已经拿到的资源全部放回去。"""
        self._release_everything(order)
        order.move_to(OrderStatus.FAILED, detail)
        self.orders.stats["failed"] += 1

    # -- 步骤构造 ------------------------------------------------------

    def _build_steps(self, order, plan, captured):
        # type: (object, object, dict) -> bool
        """把计划里的步骤变成可执行的 `OrderStep`，并推出各自的依赖边。

        顺序**必须**用 `execution_order()`（合法的拓扑序），
        而不是 `steps[].depth`——08 第七节第 13 条：`depth` 会被更浅的路径
        调小，「每条边都从 depth 指向 depth+1」不成立。
        """
        by_index = {}
        for row in plan.steps:
            by_index[row.get("index")] = row
        dispatch = {}
        for item in plan.seal.get("dispatch") or ():
            dispatch[item.get("step")] = item

        sequence = list(plan.execution_order())
        position = {}
        for place, index in enumerate(sequence):
            position[index] = place
        #: `canonical -> 生产它的步骤下标`，**按执行顺序取第一个**。
        #:
        #: 只认 `outputs`：`returns` 是做完之后回到网络的东西（空桶之类），
        #: 它同样会进库存，但「因为它到了所以下游能开工」不成立——返还物是
        #: 结果不是来源。**`external_inputs` 也不认**：那是配方外部自理的
        #: 输入（R09），网络里根本没有它的生产步骤。
        producer = {}
        for index in sequence:
            for entry in (by_index.get(index) or {}).get("outputs") or ():
                canonical = entry.get("canonical")
                if canonical and canonical not in producer:
                    producer[canonical] = index

        steps = []
        for index in sequence:
            row = by_index.get(index)
            if row is None:
                return False
            item = dispatch.get(index) or {}
            entry = captured.get(
                (item.get("node_key"), item.get("generation"), item.get("slot"))
            )
            if entry is None:
                # 复核通过却没留下样板：`revalidate` 的样板腿报告里没这条。
                # 没有定义就取不到活的资源键，而拿名字去猜一个键是模块
                # docstring 明令禁止的。
                return False
            device_id = item.get("device_id") or _provider_device(row)
            step = OrderStep(index, row.get("canonical", ""), item, row.get("batches", 0))
            step.rank = int(row.get("rank") or 0)
            step.device_id = device_id
            keymap = _key_map(entry.definition)
            # 可读名从**样板定义**里取（那里有活的键），取不到就退化成
            # `canonical`——一个副产物步骤的产物未必在这块样板的输出里。
            named = keymap.get(step.canonical)
            if named is not None:
                step.label = named.name
            if not _fill_step(step, row, keymap, self.policy):
                return False
            # **依赖边只从「执行顺序在我之前」的生产者连过来。** 同一步既产出
            # 又消耗同一个键（自循环配方）时 `producer[canonical]` 就是它自己，
            # 那条边要丢掉——留着会让这一步永远等自己完成。
            step.depends_on = tuple(
                sorted(
                    set(
                        producer[entry.get("canonical")]
                        for entry in (row.get("inputs") or ())
                        if entry.get("canonical") in producer
                        and position.get(producer[entry.get("canonical")], -1)
                        < position.get(index, 0)
                    )
                )
            )
            steps.append(step)
        order.steps = steps
        return True

    # -- 取消与暂停（09-08）--------------------------------------------

    def cancel(self, order_id):
        # type: (str) -> CommandResult
        order = self.orders.get(order_id)
        if order is None:
            return _rejected(
                Reason.ORDER_UNKNOWN, u"这张网络上没有订单 %s" % (order_id,)
            )
        if order.is_terminal():
            return _rejected(
                Reason.ORDER_TERMINAL,
                u"订单 %s 已经是 %s，不能再取消" % (order_id, order.status),
            )
        self._finish_cancel(order)
        return CommandResult.ok("cancel", order.to_dict())

    def _finish_cancel(self, order):
        # type: (object) -> None
        """释放未消费的预留、注销等待台账，并**如实记下在途资源**。

        R05 未定（取消后资源处置），因此替身口径取最保守的一侧：
        **留在原地不动、逐条报出来**。已经进了外部机器的那部分不可能
        「拿回来」（09 文档：不伪造回滚外部机器），而躺在前端中转位里的那部分
        属于谁、该退回哪里，是一条还没定案的规则——替身不替它做决定。
        """
        tick = int(getattr(self.runtime, "tick_count", 0) or 0)
        order.move_to(OrderStatus.CANCELLING, u"正在释放资源", tick)
        residue = []
        for step in order.steps:
            step_residue = []
            if not step.is_terminal_step():
                # **逐实例报，不逐步骤报。** 同一个步骤可能同时有 4 次执行在
                # 途、各自停在不同相位、各自压着不同的料；按步骤聚合成一行
                # 会把「哪一次执行的那一份」丢掉，而取消之后的善后正是按
                # 执行实例去查的（`execution_id` 是 10 那边的索引键）。
                for instance in list(step.executions):
                    if instance.is_terminal():
                        continue
                    if instance.assembly is not None:
                        row = {"execution_id": instance.execution_id,
                               "assembly": instance.assembly,
                               "note": u"R05 未定：装配实物与待产出账保留，不自动回滚"}
                        residue.append(row)
                        step_residue.append(row)
                        self.service.patterns.assembly.release(instance)
                    for item in instance.inputs:
                        if item.staged <= 0 and item.delivered <= 0:
                            continue
                        row = {
                            "step": step.index,
                            "execution_id": instance.execution_id,
                            "key": item.canonical,
                            "staged": item.staged,
                            "delivered": item.delivered,
                            "in_buffer": max(0, item.staged - item.delivered),
                            "note": u"已投进外部机器的部分不能取回；"
                                    u"中转位里的余量按 R05 待定，暂不搬动",
                        }
                        residue.append(row)
                        step_residue.append(row)
                    instance.status = StepStatus.CANCELLED
                    instance.blocked_from = None
                    instance.blockers = []
                    instance.detail = u"订单取消"
                    instance.tick = tick
                    # 注销等待台账（10 那边按同一个 ID 索引）。
                    self._unregister(instance.execution_id)
                step.cancelled = True
                step.blockers = []
                step.detail = u"订单取消"
            step.residue = step_residue
        order.residue = residue
        self._release_everything(order)
        order.blockers = []
        order.paused = False
        order.move_to(OrderStatus.CANCELLED, u"已取消", tick)
        self.orders.stats["cancelled"] += 1

    def pause(self, order_id):
        # type: (str) -> CommandResult
        order = self.orders.get(order_id)
        if order is None:
            return _rejected(Reason.ORDER_UNKNOWN, u"没有订单 %s" % (order_id,))
        if order.is_terminal():
            return _rejected(
                Reason.ORDER_TERMINAL, u"订单已经是 %s" % (order.status,)
            )
        tick = int(getattr(self.runtime, "tick_count", 0) or 0)
        order.paused = True
        if order.status != OrderStatus.BLOCKED:
            order.move_to(OrderStatus.BLOCKED, u"已暂停", tick)
        order.blockers = [
            BlockReason(Reason.ORDER_PAUSED, u"订单已被暂停；继续之后会重新复核")
        ]
        return CommandResult.ok("pause", order.to_dict())

    def resume(self, order_id):
        # type: (str) -> CommandResult
        order = self.orders.get(order_id)
        if order is None:
            return _rejected(Reason.ORDER_UNKNOWN, u"没有订单 %s" % (order_id,))
        if order.is_terminal():
            return _rejected(
                Reason.ORDER_TERMINAL, u"订单已经是 %s" % (order.status,)
            )
        tick = int(getattr(self.runtime, "tick_count", 0) or 0)
        order.paused = False
        order.blockers = []
        if order.adopted:
            order.move_to(OrderStatus.QUEUED, u"已继续，等待重新复核", tick)
        else:
            # 还没落地（在等槽位的半路上被暂停的）：接着把落地走完，
            # 而不是直接推到 `QUEUED`——那会让一个没有租约的订单被当成
            # 已开工，进而在没有槽位的情况下开始派发。
            self._resume_adoption(order, tick)
        return CommandResult.ok("resume", order.to_dict())

    # -- 回流（任务 10 的入口）------------------------------------------

    def receive_output(self, execution_id, key, count, op_id="", source=None):
        # type: (str, object, int, str, object) -> int
        """任务 10 把一份**已经真的进了网络存储**的资源分配给它等待的实例。

        返回本次实际被接受的量。**多出来的不接受**（留在网络里当普通库存，
        10-06 明写「不丢弃」），重复的 `op_id` 返回 0 且不再加数
        （10-05 要求覆盖同一回执的重试）。

        `source` 只用于诊断：10-04 要求区分「外部新输入」与「内部移动」，
        但**那个判定属于 10**——它才是看得见回执来源的一方。
        """
        accepted = self.waiting.observe(execution_id, key, count, op_id)
        if accepted <= 0:
            return 0
        # **按 `inflight` 反查，不再扫步骤。** 在途表本来就以 `execution_id`
        # 为键（它同时是槽位租约的拥有者），而逐步骤逐实例地比 ID 既慢又
        # 少了一条信息：扫不到时无法区分「这次执行早就完成了」与
        # 「ID 根本不属于本网络」。
        for order in self.orders.orders():
            if execution_id in order.inflight:
                order.stats["outputs"] += accepted
                order.mark(int(getattr(self.runtime, "tick_count", 0) or 0))
                return accepted
        return accepted

    def _unregister(self, execution_id):
        # type: (str) -> None
        self.waiting.unregister(execution_id)
        self.service.returns.unregister(execution_id)

    # -- 分帧推进（09-04 / 09-07）---------------------------------------

    def tick(self, context, budget):
        # type: (dict, int) -> int
        """推进一步。返回本帧消耗的工作量。

        公平轮转：游标每帧前移一位，因此**排在字典序后面的订单不会饿死**
        （04 的 `RuntimeManager` 修过同一个形状的缺陷）。每帧最多碰
        `orders_per_tick` 个订单，且总消耗不超过本帧预算。
        """
        budget = int(budget or 0)
        if budget <= 0:
            return 0
        tick = int(context.get("tick") or 0)
        #: 帧号（调度器给的那个）。**没有就不判重**——离线直接调用时
        #: 每次 `tick` 都算一帧，与引入它之前一致。
        frame = context.get("frame")
        service = self.service
        service.refresh_capacity()
        orders = service.orders.live()
        if not orders:
            return 0
        spent = 0
        advanced = 0
        cursor = service.cursor % len(orders)
        for offset in range(len(orders)):
            if spent >= budget or advanced >= self.policy.orders_per_tick:
                break
            order = orders[(cursor + offset) % len(orders)]
            if order.paused:
                continue
            cost = self._advance(order, tick, budget - spent, frame)
            spent += max(0, cost)
            advanced += 1
        service.cursor = cursor + 1
        return spent

    def _advance(self, order, tick, budget, frame=None):
        # type: (object, int, int, object) -> int
        """推进一张订单，返回消耗的工作量。"""
        order.stats["ticks"] += 1
        order.mark(tick)
        if order.status == OrderStatus.PLANNING:
            self._step_plan(order, PLAN_NODES_PER_TICK, tick, frame)
            return 1
        if order.status == OrderStatus.CANCELLING:
            self._finish_cancel(order)
            return 1
        if order.status == OrderStatus.BLOCKED:
            self._unblock(order, tick)
            return 1
        if order.status in (OrderStatus.QUEUED, OrderStatus.RUNNING):
            # **订单级的闸门每一帧都要过**，不能只在「排队 → 运行」那一次过。
            # 主控换人（控制器被换/被拆）与网络关门都不保证会有通知送到
            # 订单手上——尤其 `NetworkRuntime.tick` 在网络不开放时根本不调
            # 服务的 `tick`，于是「重开之后」是唯一能发现它的时刻。
            blockers = self._order_obstacles(order)
            if blockers:
                order.blockers = blockers
                order.move_to(OrderStatus.BLOCKED, blockers[0].detail, tick)
                return 1
            if not self._recheck_if_stale(order, tick):
                return 1
            if order.status == OrderStatus.QUEUED:
                order.move_to(OrderStatus.RUNNING, u"开始推进步骤", tick)
        if order.status != OrderStatus.RUNNING:
            return 0
        return self._dispatch_round(order, tick, budget)

    def _resume_adoption(self, order, tick):
        # type: (object, int) -> bool
        """订单还没真正落地时，把落地那几步接着走完。"""
        if order.plan is None:
            # 到这一步还没有计划，说明它连规划都没成功过——那不该出现在
            # 一个非终态的订单上。**如实报出来**，不要假装它能往下走。
            order.blockers = [
                BlockReason(Reason.INTERNAL, u"订单没有可用的计划，无法继续")
            ]
            order.move_to(OrderStatus.FAILED, u"订单状态与计划不一致")
            self.orders.stats["failed"] += 1
            return False
        return self._finish_adoption(order, tick)

    def _unblock(self, order, tick):
        # type: (object, int) -> None
        """看看挡住它的东西还在不在。

        **这是 09-08 的「重验」入口。** 它必须真的重新问一遍，而不是
        等一个事件——断电、提供者离线、写盘阻塞都不保证会有通知送回来
        （网络不开放时连 `tick` 都不会被调用，见 `NetworkRuntime.tick`）。
        """
        blockers = self._blockers_for(order)
        if blockers:
            order.blockers = blockers
            return
        order.blockers = []
        if not order.adopted:
            # 「挡着它的东西没了」与「它可以开工了」是两件事：容量不足
            # 挡住的订单**还没有材料与租约**，得把落地那几步接着走完。
            self._resume_adoption(order, tick)
            return
        order.move_to(OrderStatus.QUEUED, u"阻塞已解除", tick)
        self.service.stats["unblocked"] += 1

    def _order_obstacles(self, order):
        # type: (object) -> list
        """订单级的闸门：**与具体哪一步无关**的那几条。

        单独拆出来是因为它有两个调用点，而两处的时机不同：
        `_advance` 每帧都过（**包括正在跑的订单**），`_unblock` 在阻塞之后
        再确认一遍。只放在 `_unblock` 里会让「订单已经在 RUNNING、这时
        主控被换掉了」这种情况永远发现不了——它不会自己变成 BLOCKED。
        """
        out = []
        runtime = self.runtime
        master = getattr(runtime, "master", None)
        if master is None:
            return [
                BlockReason(Reason.MASTER_ABSENT, u"这张网络现在没有主控，订单冻结")
            ]
        if master.instance_id != order.master_instance_id:
            # 09 验收：「拓扑拆分后订单只属于原主控」。主控换人（控制器被拆、
            # 被换、被重建）之后，订单的预留与在途批次都记在旧代次上，
            # **不能跟着新主控继续动库存**；也不自动改判失败——
            # 在途的原料还躺在某个中转位里，那张账只有人看得懂。
            return [
                BlockReason(
                    Reason.MASTER_ABSENT,
                    u"订单下单时的主控是 %s，现在是 %s；订单不跟着新主控继续，"
                    u"请取消后重新下单"
                    % (order.master_instance_id, master.instance_id),
                )
            ]
        state = runtime.state()
        if state != "runnable":
            return [
                BlockReason(
                    Reason.MASTER_RECOVERING, u"网络状态是 %s，订单冻结" % (state,)
                )
            ]
        storage = self._storage()
        if storage is None or not storage.is_ready():
            return [BlockReason(Reason.SERVICE_NOT_READY, u"存储服务现在不可用")]
        return out

    def _blockers_for(self, order):
        # type: (object) -> list
        """现在到底还有什么挡着这张订单。空列表表示可以往前走。

        **按执行实例看，不按步骤看。** R04 之后同一张订单可以同时有若干次
        执行在途（各自占一个槽、各自停在不同相位），因此「订单被什么挡着」
        是这些实例各自障碍的汇总，而不是某一个「当前步骤」的障碍。
        """
        out = self._order_obstacles(order)
        if out:
            return out
        # **「有实例能自己收尾」排在「回流入口接没接上」前面。** 顺序反了会有
        # 一个很隐蔽的后果：订单一旦因为 `order_returns_absent` 转成 BLOCKED，
        # 就再也不会回到 `_track_outputs` 去看产物到了没有——而产物是真的会到的
        # （10 一旦接上，或者有人手工把入库的资源分配过来）。真机上就是这么
        # 撞出来的：步骤的 `received` 已经是 1/1，订单却还停在阻塞上、进度停在 0。
        # **离线用例照不到它**，因为假回流报 `available: True`，订单一直留在
        # RUNNING 每帧轮询。
        for instance in order.inflight_executions():
            if _ready_to_finish(instance, self.waiting):
                return out
        # **这条提示只在「真的有执行在等产物」时才算数。** 它是订单级的
        # （回流入口没接线是全局事实），但它答的是「为什么这一次执行等不到
        # 产物」——而一次还在备料、甚至还没开出来的执行根本谈不上等产物。
        # 放宽到订单级会把别的阻塞原因**盖掉**：真机上被容量挡下的订单，
        # 在槽位释放之后本该自己恢复，却因为这条一直卡在 `order_returns_absent`
        # 上，连 `_resume_adoption` 都走不到。
        blocker = _sink_blocker(self.service)
        if blocker is not None:
            for instance in order.inflight_executions():
                if instance.status == StepStatus.WAITING_OUTPUTS:
                    out.append(blocker)
                    break
        for instance in order.inflight_executions():
            if instance.status != StepStatus.BLOCKED:
                continue
            # **重新算一遍，不读 `instance.blockers`。** 那份列表是**被挡住
            # 那一刻**的快照，而挡住它的东西大多会自己消失：外部机器腾出空间、
            # 另一条订单的批次投完、别的路径把料还了回来。读旧快照的表现是
            # 「接口早就恢复了而订单永远醒不过来」——而它自己报的原因还是对的，
            # 看起来一点都不像坏了。
            obstacle = self._instance_obstacle(order, instance)
            # 可重试的那一类**不把订单留在这里**：它们下一秒就可能变，
            # 而留在 BLOCKED 上等于等一次重新判定的节奏（那是别人定的）。
            if obstacle is not None and obstacle.code not in RETRYABLE_CODES:
                out.append(obstacle)
                break
        return out

    def _instance_obstacle(self, order, instance):
        # type: (object, object) -> object
        """只读地算一遍**这一次执行**现在挡在哪。返回 `BlockReason` 或 `None`。

        **一个状态都不改。** 它调用的是执行路径上那几个判据函数本身
        （`_port_for` / `_foreign_batch` / `_stock_blocker_for_staging`），
        因此两处不会各说各话——另写一套判断才会。
        """
        if instance.step.mode == "crafting":
            if instance.assembly and instance.assembly["uncertain"]:
                return BlockReason(Reason.CONTAINER_UNCERTAIN, instance.assembly["uncertain"])
            return None
        if instance.status == StepStatus.WAITING_OUTPUTS:
            return None
        # **这里曾经有一条 `if instance.op_id is not None: return None`。**
        # 它当时的意思是「手上有批次就是在投料，没什么可报的」——串行时成立，
        # 换成逐实例之后它是个**错误答案**：调用方只对 `BLOCKED` 的实例问这
        # 一条，而能把它置成 `BLOCKED` 的只有**不可重试**的原因
        # （可重试的那些连状态都不改，见 `_block_instance`），也就意味着
        # 端口上那一批**已经不再是它的了**。此时 `op_id` 还是上一个批次的
        # 残值，守卫会据此答「没事」，于是订单永远停在 `RUNNING` 而
        # `blockers` 是空的——界面上一片正常，料却早就被人拿走了。
        # 现在照直往下算：端口归属、批次的死活、库存够不够，都在下面重算一遍。
        if not [item for item in instance.inputs if item.to_stage() > 0]:
            leftover = [item for item in instance.inputs if not item.is_satisfied()]
            if not leftover:
                return None
            return BlockReason(
                Reason.ORDER_BATCH_STALLED,
                u"中转位里还压着 %s 没进外部机器" % (leftover[0].key.name,),
            )
        port, blocker = self._port_for(instance)
        if blocker is not None:
            return blocker
        foreign = _foreign_batch(port, instance)
        if foreign is not None:
            return foreign
        if _owns_live_batch(port, instance):
            batch = port.batch
            if batch.reason:
                return BlockReason(Reason.ORDER_BATCH_STALLED, batch.reason)
            return None
        item = [row for row in instance.inputs if row.to_stage() > 0][0]
        return self._stock_blocker_for_staging(order, item, item.to_stage())

    def _recheck_if_stale(self, order, tick):
        # type: (object, int) -> bool
        """拓扑或库存变了就重新复核一遍。返回「还活着吗」。

        **不重规划。** 08 的 `revalidate` 只回答「依据还成立吗」，
        处置规则也是它定的：不成立时要么用新快照重规划，要么拒绝并报告。
        这里选后者——重规划会换掉 `seal`，而订单已经按旧 `seal` 预留过料了。
        """
        topology_version = self._topology_version()
        inventory_version = self._inventory_version()
        if order.plan is None:
            # 还没落地就没有「依据」可复核。走到这里说明状态机上有别的地方
            # 出了偏差（`_advance` 只对已落地的订单调这一条），
            # **不在这里静默放行**：交给 `_resume_adoption` 判。
            return self._resume_adoption(order, tick)
        if (
            order.topology_version == topology_version
            and order.inventory_version == inventory_version
        ):
            return True
        report = order.plan.revalidate(
            self._store().check_dispatch, None, topology_version
        )
        order.stats["revalidations"] += 1
        blockers = list(report.reasons)
        blockers.extend(self._stock_blockers(order))
        order.topology_version = topology_version
        order.inventory_version = inventory_version
        if blockers:
            order.blockers = blockers
            order.move_to(OrderStatus.BLOCKED, u"重验没有通过", tick)
            return False
        return True

    def _stock_blockers(self, order):
        # type: (object) -> list
        """重验时的库存腿。**不能直接用 `revalidate` 的库存腿。**

        快照认的是 `available = stored - reserved`，而 `reserved` **含订单
        自己的预留**——照搬会让一张刚刚预留完的订单在第一次重验时判自己
        「缺料」。正确的问法是「除我之外没人锁住、且还没被我搬走的量，
        够不够我剩下的需求」：

            拿得到的 = available + 我锁住的     （available 已扣掉别人的）
            还差的   = 还没送进机器的部分
        """
        storage = self._storage()
        out = []
        for canonical in sorted(order.reservations):
            row = order.reservations[canonical]
            key = row.get("key")
            if key is None:
                continue
            need = self._remaining_need(order, canonical)
            if need <= 0:
                continue
            entry = storage.query(key)
            # 同样按 05 的活预约占算，理由见 `_stock_blocker_for_staging`。
            mine = self._live_reserved(order, canonical)
            self._sync_reservation(order, canonical)
            usable = int(entry.get("available", 0)) + mine
            if usable < need:
                out.append(
                    BlockReason(
                        Reason.PLAN_STOCK_CHANGED,
                        u"%s 还差 %d 个进机器，但现在只拿得到 %d 个"
                        u"（可用 %d + 我已锁住的 %d）"
                        % (key.name, need, usable, entry.get("available", 0), mine),
                    )
                )
        return out

    def _remaining_need(self, order, canonical):
        # type: (object, str) -> int
        """这种料还要往机器里送多少。

        **两段相加，缺一不可**：

        - 已经开出来的执行实例还欠的（`item.debt()`）；
        - **还没开出来的批次**（`open_batches() * per_batch`）。

        只算前一段会让重验在「第 1 次执行刚备完料」时判自己「料够了」——
        而剩下 9 次的料早就被别人拿走了。那时候放行，后面每一批都会在
        `_stock_blocker_for_staging` 上撞墙，而重验本该在那之前就把这件事
        说出来。反过来只算后一段会让「已经备进机器的」被算两遍。
        """
        total = 0
        for step in order.steps:
            if step.is_terminal_step():
                continue
            per_batch = dict(step.input_specs).get(canonical)
            if per_batch is None:
                continue
            for instance in step.executions:
                if instance.is_terminal():
                    continue
                for item in instance.inputs:
                    if item.canonical == canonical:
                        total += item.debt()
            total += per_batch * step.open_batches()
        return total

    # -- 执行实例推进（R04 之后派发的单位）-------------------------------

    def _dispatch_round(self, order, tick, budget):
        # type: (object, int, int) -> int
        """派发轮：**同一次调用里推进所有依赖已满足的执行实例**。

        R04 已定案：「配方执行 10 次、网络有 4 个空闲槽且任务均已就绪时，
        可有 4 次同时在途；完成一个后可继续派发一个」。因此这里不再像从前
        那样每帧只推「最靠前的那个未完成步骤」——那等于把并行度钉死成 1，
        单订单永远用不满剩余槽位。

        ## 顺序

        1. 轮转步骤表（`order.dispatch_cursor`），让多步订单不会总是同一步
           先拿资源；
        2. 依赖已满足的步骤：先推进**已经开出来的**实例；
        3. 再看槽位有没有空的，有就再开新的实例（每开一个领 1 槽）；
        4. 收尾时判一次订单级 `BLOCKED`（见 `_settle_blockers`）。

        ## 预算

        **每一次推进、每开一个实例都算 1 点工作**，且每次记账前都先查一次
        预算，因此返回值恒不超过 `budget`——04 的 `RuntimeManager` 上那条
        「本帧总消耗不超预算」的不变量就是靠这个成立的。
        """
        budget = max(0, int(budget))
        spent = 0
        progressed = False
        starved = {}
        for step in self._rotated_steps(order):
            if spent >= budget:
                break
            if step.is_terminal_step():
                continue
            if not self._dependencies_met(order, step):
                continue
            for instance in list(step.executions):
                if spent >= budget:
                    break
                if instance.is_terminal():
                    continue
                cost = self._advance_instance(order, instance, tick)
                spent += max(0, cost)
                if cost > 0:
                    progressed = True
            if spent >= budget or step.open_batches() <= 0:
                continue
            while step.open_batches() > 0 and spent < budget:
                if self.service.slots.available() < 1:
                    # 槽位用满：**如实报出来，但不把订单冻住**（`ORDER_NO_CAPACITY`
                    # 在 `RETRYABLE_CODES` 里）。R04 只说「单订单可用满剩余槽位」，
                    # 没说过「没槽位就转阻塞」——它不是一条要玩家动作的规则，
                    # 跑完一次执行就会空出来一个。
                    starved[step.index] = slot_starvation_blocker(self.service.slots)
                    break
                instance = step.open_execution(order.order_id)
                if instance is None:
                    break
                spent += 1
                if not self._claim_slot(instance):
                    # 理论上到不了这里（上面刚查过 `available()`）。真到了就
                    # **撤回这次实例**——它一秒活都没干过，留着会让 `created`
                    # 记着一次从未存在的执行，进度条永久缺一格。
                    step.abandon_execution(instance)
                    starved[step.index] = slot_starvation_blocker(self.service.slots)
                    break
                order.inflight[instance.execution_id] = instance
                progressed = True
                if spent < budget:
                    # **开实例本身已经记了 1 点。** 这一帧预算不够就不再推它，
                    # 下一帧照样会轮到——省下的这 1 点让别的步骤/订单一帧
                    # 也能动一下。上面那条 `spent <= budget` 的不变量靠这里守住。
                    spent += max(0, self._advance_instance(order, instance, tick))
        order.dispatch_cursor += 1
        # 槽位提示**每一轮重判**：空出来之后还挂着它，界面会在订单明明正跑着的
        # 时候一直说「没有空闲的配方处理槽位」。
        for step in order.steps:
            reason = starved.get(step.index)
            if reason is not None:
                step.blockers = [reason]
            elif step.blockers and step.blockers[0].code == Reason.ORDER_NO_CAPACITY:
                step.blockers = []
        self._settle_blockers(order, tick, progressed)
        return spent

    def _claim_slot(self, instance):
        # type: (object) -> bool
        """给一次执行领 1 个槽位。领不到返回 `False`。

        **拥有者是执行实例 ID，不是订单 ID**——R04：「每次配方执行占 1 槽
        至完成」。租约因此天然是「谁在跑」，`order.inflight` 的键与它一一对应。
        """
        return self.service.slots.claim_slot(instance.execution_id) is not None

    def _settle_blockers(self, order, tick, progressed):
        # type: (object, int, bool) -> None
        """按「本帧有没有真的往前走」决定订单要不要转 `BLOCKED`。

        **这是本次改动里最容易写错的一处。** 从前的口径是「哪一步撞墙就把
        整张订单冻住」，串行时它恰好等价；并行之后它不是——同一张订单里
        另一次执行可能正躺在 `WAITING_OUTPUTS` 等产物，而被冻住之后
        `_track_outputs` 再也不会被调用，产物到了也没人收。那是**新引入的
        活性缺陷**，因此判据必须换成订单级的：**一次都没推进成功、而且
        确实有东西挡着，才算被挡住**。
        """
        blockers = self._blockers_for(order)
        if blockers:
            order.blockers = blockers
            if not progressed and order.status == OrderStatus.RUNNING:
                order.move_to(OrderStatus.BLOCKED, blockers[0].detail, tick)
            return
        order.blockers = []

    def _rotated_steps(self, order):
        # type: (object) -> list
        """按 `dispatch_cursor` 轮转过的步骤表。

        总从 `execution_order()` 的第一个开始扫，会让多步订单永远按同一个
        顺序占资源：第一步开满槽位之后，后面的步骤一帧都轮不到。外层
        `FactoryService.cursor` 轮的是订单，这一层轮的是同一张订单内部的步骤。
        """
        steps = order.steps
        if not steps:
            return []
        cursor = order.dispatch_cursor % len(steps)
        return list(steps[cursor:]) + list(steps[:cursor])

    def _dependencies_met(self, order, step):
        # type: (object, object) -> bool
        """这一步的生产者步骤是不是都做完了。

        依赖边在 `_build_steps` 里由计划的产出声明推出来（`step.depends_on`）。
        **边是显式的，不看 `rank`**：`rank` 是拓扑深度，它答的是「至少要几步」，
        不是「谁喂谁」——两个同 `rank` 的步骤之间本来就没有依赖，而一个
        `rank` 更深的步骤完全可能不依赖某个更浅的。
        """
        for index in step.depends_on:
            producer = order.step_at(index)
            if producer is None or not producer.is_fulfilled():
                return False
        return True

    def _advance_instance(self, order, instance, tick):
        # type: (object, object, int) -> int
        """把一次执行往前推一格，返回消耗的工作量。"""
        if instance.step.mode == "crafting":
            return self._advance_assembly(order, instance, tick)
        if instance.status == StepStatus.BLOCKED:
            # 从**它自己原来的位置**继续，而不是从头再来一遍——
            # 从头再来就是「重发整批原料」，那是验收明令禁止的。
            instance.status = instance.blocked_from or StepStatus.STAGING
            instance.blocked_from = None
            instance.blockers = []
        if instance.status == StepStatus.WAITING_INPUTS:
            instance.status = StepStatus.STAGING
            instance.tick = tick
        if instance.status == StepStatus.STAGING:
            return self._stage_next_input(order, instance, tick)
        if instance.status == StepStatus.DISPATCHED:
            return self._track_dispatch(order, instance, tick)
        if instance.status == StepStatus.WAITING_OUTPUTS:
            return self._track_outputs(order, instance, tick)
        return 0

    def _advance_assembly(self, order, instance, tick):
        # type: (object, object, int) -> int
        result = self._assembly_command(order, instance)
        if result.reasons:
            return self._block_instance(order, instance, tick, result.reasons[0])
        if instance.status == StepStatus.BLOCKED:
            instance.status = instance.blocked_from or StepStatus.STAGING
            instance.blocked_from = None
            instance.blockers = []
        return int((result.payload or {}).get("work", 0))

    def _assembly_command(self, order, instance):
        # type: (object, object) -> object
        gateway = self.service.gateway
        ref = self._step_ref(instance)
        if gateway is None or not gateway.available() or ref is None:
            return CommandResult.blocked(instance.execution_id,
                                         [BlockReason(Reason.MASTER_ABSENT, u"装配派发网关不可用")])
        instance.op_attempt += 1
        return gateway.submit(DeviceCommand(
            "assemble/%s/%d" % (instance.execution_id, instance.op_attempt),
            CommandKind.STORAGE,
            CommandSource(None, ref, ref.node_key.as_key()[0], distance_ok=True),
            payload={"action": "assembly_advance", "order_id": order.order_id,
                     "execution_id": instance.execution_id},
            topology_version=gateway.topology_version(), device_ref=ref))

    def _block_instance(self, order, instance, tick, reason):
        # type: (object, object, int, object) -> int
        """把**一次执行**挡下来，并记住它原来在哪。

        **它不碰订单状态**：订单是不是被挡住由 `_settle_blockers` 按
        「本帧有没有推进」统一判（同一个订单里别处可能还在跑）。

        **可重试的障碍连状态都不改**（见 `RETRYABLE_CODES`）：把原因写进
        `instance.detail` 让界面看得见，下一帧执行器本来就会再试一次。
        返回值固定是 0——**「发现被挡住」不是工作成果**，让调用方据此判
        「本帧有没有往前走」正是这条返回值存在的意义。
        """
        if reason.code in RETRYABLE_CODES:
            instance.detail = reason.detail
            return 0
        if instance.status != StepStatus.BLOCKED:
            instance.blocked_from = instance.status
        instance.status = StepStatus.BLOCKED
        instance.blockers = [reason]
        instance.tick = tick
        return 0

    def _stage_next_input(self, order, instance, tick):
        # type: (object, object, int) -> int
        """把这一步还缺的料往样板口的中转位搬一批。

        ## 顺序（本模块最容易写错的一处）

        05 的 `_do_extract` 里可用量是 `stored - reserved`，而 `reserved`
        **含本订单自己的预留**，并且 05 **没有**任何 consume/commit-reservation
        入口。所以「先预留、再让 `port_stage` 去取」会取到 0，而症状看起来
        与「网络里没料了」一模一样。正确的顺序是：

            1. 按本次要取的量**释放**预留（`release` 支持部分释放）；
            2. 同一次调用里发 `port_stage`（**中间没有让出点**，
               因此不存在「别的订单插进来把这份料拿走」的时刻）；
            3. 没搬走的部分**补回预留**；补不回来就报 `ORDER_STOCK_TAKEN`
               并让订单阻塞——**不当作成功**。

        ## 一次只搬一种料

        `PortState.batch` **只有一个格子**，而 `stage_batch` 每次都用新批次
        覆盖它。因此同一步要备两种料时只能串行：这一种投完（`is_complete()`）
        再备下一种。抢占别人的批次的表现是那个订单的 `delivered` 被清零、
        原料被投第二遍，所以动手之前先看格子里的 `op_id` 是不是自己的。
        """
        pending = [item for item in instance.inputs if item.to_stage() > 0]
        if not instance.inputs:
            # 配方不需要网络供给的输入。放在取接口之前判，是因为一台装配仓
            # 提供者（07-06）本来就不该被这条路碰到。
            return self._finish_inputs(order, instance, tick)
        port, blocker = self._port_for(instance)
        if blocker is not None:
            return self._block_instance(order, instance, tick, blocker)
        # **先对齐一次读数**再决定要不要再备。接口的轮询随时可能在推进这一批，
        # 而这里若拿着几帧前的 `delivered`，就会去补一份已经在路上的料。
        self._sync_own_batch(instance, port)
        busy = _foreign_batch(port, instance)
        if busy is not None:
            return self._block_instance(order, instance, tick, busy)
        if not pending:
            # 没有要备的料了（都搬出去了，或者本来就不用搬）。
            # **但「搬出去了」不等于「进了机器」**：中转位里可能还压着一部分
            # （外部机器当时只收了一半）。这一段要交给投料那半边走，
            # 在那里继续推、继续读读数——**不能在这里判「都进了」**，
            # 判早了的后果是这一步在原料还没到的时候就开始等产物，
            # 而那份产物永远不会来，因为机器上根本没收到料。
            if instance.all_inputs_delivered():
                return self._finish_inputs(order, instance, tick)
            if port.batch is not None and port.batch.op_id == instance.op_id:
                instance.status = StepStatus.DISPATCHED
                return 1
            return self._block_instance(
                order,
                instance,
                tick,
                BlockReason(
                    Reason.ORDER_BATCH_STALLED,
                    u"中转位里还压着 %s 没进外部机器，而且已经不记得是哪一批了"
                    % (
                        u"、".join(
                            u"%s %d/%d"
                            % (item.key.name, item.delivered, item.required)
                            for item in instance.inputs
                            if not item.is_satisfied()
                        ),
                    ),
                ),
            )
        if _owns_live_batch(port, instance):
            # **本步上一批还在投，绝不再备一次。** 这条判据不是优化，
            # 是正确性：同一帧里 `RuntimeManager` 的余量轮会把同一个运行时
            # 访问两次，于是 `_stage_next_input` 可能对同一个 `op_id` 发第二条
            # 命令——协调器账本按 `op_id` 去重，**会把上一次的回执原样返回**，
            # 于是 `moved` 被加第二次，中转位里凭空多出一份料。
            # 靠「一帧只做一次」来防是脆的（帧号可能缺失），靠批次归属才是结构性的。
            instance.status = StepStatus.DISPATCHED
            return 1
        item = pending[0]
        need = item.to_stage()
        blocker = self._stock_blocker_for_staging(order, item, need)
        if blocker is not None:
            return self._block_instance(order, instance, tick, blocker)
        # **每一批一个新 `op_id`。** 复用会命中协调器的去重账本，拿到
        # 上一批的回执，而 `state.batch` 已经是新的一批了——两个读数
        # 从此对不上，症状是 `delivered` 一帧涨一截而料根本没动。
        # 「同一次备料不备第二遍」由 `_owns_live_batch` 保证（有活批次就
        # 不再开新的），不靠复用 `op_id`。
        #
        # 前缀是 `execution_id`（`订单/步骤/执行序号`），因此**同一步骤的
        # 不同次执行天然分得开**——它们本来就会同时挂在同一台接口上排队。
        instance.op_attempt += 1
        op_id = "%s/%s#%d" % (instance.execution_id, item.canonical, instance.op_attempt)
        # 换批之前把上一批的终值折进基线：新批次的 `delivered` 从 0 重新数，
        # 不折基线会让这一步以为料还没投出去，然后**把整批重投一遍**。
        item.delivered_base = item.delivered
        released = self._release_for_staging(order, item, need)
        result = self._port_stage(port, item.key, need, op_id)
        if result is None:
            # 网关不可用（没有主控 / 网络没开放）。预留已经放掉了，
            # 立刻补回去——下一帧再来。
            self._restore_reservation(order, item, released)
            return self._block_instance(
                order,
                instance,
                tick,
                BlockReason(Reason.MASTER_ABSENT, u"网络现在不能接收命令，备料暂停"),
            )
        batch = (result.payload or {}).get("batch")
        if batch is None:
            self._restore_reservation(order, item, released)
            reason = (list(result.reasons) or [
                BlockReason(Reason.UNSUPPORTED, u"接口没有受理这次备料")
            ])[0]
            return self._block_instance(order, instance, tick, reason)
        moved = int(batch.get("staged") or 0)
        if op_id != instance.op_id:
            # **只在真的换了一条命令时累加。** 同一条 `op_id` 的第二条命令会
            # 拿到重放的原回执（数字和上次一样），累加就是重复计数。
            item.staged += moved
        instance.op_id = op_id
        instance.port_node_key = port.node_key.as_key()
        if released > moved:
            self._restore_reservation(order, item, released - moved)
        instance.detail = u"已备 %d/%d" % (item.staged, item.required)
        self.service.stats["stagings"] += 1
        order.stats["stagings"] += 1
        return 1

    def _stock_blocker_for_staging(self, order, item, need):
        # type: (object, object, int) -> object
        """备料前确认这份料**现在真的拿得到**。返回 `BlockReason` 或 None。

        05 的抽取只认全局的 `stored - reserved`，它**不认「谁的预留」**：
        一条没有预留的路径（玩家手动取、调试入口、13 的外部输入）可以从物理上
        取走本订单锁着的那一份。账面不会破（`reserved` 还是那个数），但
        位置变了，下一次抽取会拿不到——而症状是「备料一直备不动」，
        看不出是别人动过。

        所以动手之前先算一次：**除我之外没人锁住的量 + 我锁住的量**，
        够不够这次要取的。不够就如实报出来，而不是发出一條注定搬不动的命令。
        """
        # **先对齐镜像，再读它。** 我锁住的量按 05 的活预约占算：介质被拔出时
        # 05 会把预留收走，而镜像还停在原来那个数上——照镜像算会得出
        # 「拿得到」的结论，然后发出一批注定搬不动的命令。
        # 顺带把镜像更新掉，是因为订单视图是给界面读的，报一个早知道不对的
        # 数字与报错一样有害。
        self._sync_reservation(order, item.canonical)
        row = order.reservations.get(item.canonical)
        mine = int((row or {}).get("reserved") or 0)
        key = (row or {}).get("key") or item.key
        entry = self._storage().query(key)
        usable = int(entry.get("available", 0)) + mine
        if usable >= need:
            return None
        return BlockReason(
            Reason.ORDER_STOCK_TAKEN,
            u"%s 这次要取 %d 个，但现在只拿得到 %d 个"
            u"（网络可用 %d + 本订单锁住的 %d）；有别的路径动过这份料，"
            u"不发出注定搬不动的命令"
            % (item.key.name, need, usable, entry.get("available", 0), mine),
        )

    def _live_reservations(self, order, canonical):
        # type: (object, str) -> list
        """**05 那边**现在真的压在这个键上的预留。

        **一切以它为准，不看订单自己的镜像。** 镜像会在两种情况下与事实
        脱节，而两种都真的会发生：介质被拔出时 05 会
        `revoke_reservations_of_medium` 把预留收走（它明确把「通知订单重验」
        留给 09），而订单的本地记录还停在原来的数字上；取消之后订单对象
        还会在台账里存在到被清理为止。照着镜像去释放，会去释放一个
        已经不存在的预留——而 `release` 对未知 id **返回 0 且不报错**，
        于是症状是「备料越备越少料」，完全看不出原因。
        """
        storage = self._storage()
        if storage is None:
            return []
        out = []
        for reservation in storage.reservations_of(order.order_id):
            if reservation.key.canonical() == canonical:
                out.append(reservation)
        return out

    def _live_reserved(self, order, canonical):
        # type: (object, str) -> int
        return sum(int(r.count) for r in self._live_reservations(order, canonical))

    def _sync_reservation(self, order, canonical):
        # type: (object, str) -> int
        """把订单的镜像对齐到 05 的活预约占。返回活着的量。"""
        row = order.reservations.get(canonical)
        live = self._live_reservations(order, canonical)
        if row is None and live:
            # **10 会在计划没认领的键上为这张订单预留。** 中间产物走的就是
            # 这条路：计划只为 `from_stock > 0` 的键建行（`planner` 的 claims），
            # 而 10 把回流到的中间产物按订单归属锁住。
            #
            # 没有本地行时 `_stock_blocker_for_staging` 会算 `mine = 0`，
            # 而它读的 `available` 已经把**我们自己**锁住的那一份扣掉了，
            # 于是 `usable` 少算一份 → 报一条**假的** `ORDER_STOCK_TAKEN`
            # → 多步订单永远停在第二步备料上。
            #
            # 补一行镜像就是修法：镜像的职责本来就是「05 的活预约占在这里
            # 长什么样」，而不是「计划当时认领了多少」。`claimed` 填 0
            # 与 `Order.to_dict()` 的 `.get("claimed", 0)` 相容。
            row = {
                "reservation_id": None,
                "key": live[0].key,
                "claimed": 0,
                "reserved": 0,
            }
            order.reservations[canonical] = row
        if row is not None:
            row["reserved"] = sum(int(r.count) for r in live)
            row["reservation_id"] = live[0].reservation_id if live else None
        return sum(int(r.count) for r in live)

    def _release_for_staging(self, order, item, need):
        # type: (object, object, int) -> int
        """为本次备料放掉一部分预留。返回实际放掉的量。

        **按 05 的活预留逐条释放**（一条条 `release`，每条可以部分释放），
        而不是「算一个数、调一次 `release`」：后者要求订单的镜像与 05
        完全一致，而它并不总是——见 `_live_reservations` 的说明。
        """
        storage = self._storage()
        want = min(int(need), self._live_reserved(order, item.canonical))
        released = 0
        for reservation in self._live_reservations(order, item.canonical):
            if released >= want:
                break
            released += storage.release(reservation.reservation_id, want - released)
        if released:
            order.stats["releases"] += released
        self._sync_reservation(order, item.canonical)
        return released

    def _restore_reservation(self, order, item, count):
        # type: (object, object, int) -> bool
        """把没搬走的量重新锁上。**失败要如实报出来，不当成功。**"""
        count = int(count)
        if count <= 0:
            return True
        row = order.reservations.get(item.canonical)
        if row is None or row.get("key") is None:
            return True
        reservation = self._storage().reserve(
            order.order_id, row["key"], count, order.updated_tick
        )
        self._sync_reservation(order, item.canonical)
        if reservation is None:
            blocker = BlockReason(
                Reason.ORDER_STOCK_TAKEN,
                u"备料没搬走的 %d 个 %s 没能重新锁上，它们现在不受本订单保护"
                % (count, item.key.name),
            )
            order.blockers = [blocker]
            item.detail = blocker.detail
            return False
        order.stats["reserves"] += 1
        return True

    def _step_ref(self, step):
        # type: (object) -> object
        """这一步的提供者当前的设备实例引用。取不到返回 `None`。

        从**注册表**拿而不是把 `DeviceRef` 存进步骤里：步骤的字段要能
        `json.dumps`（订单视图是给 11 与诊断读的），而引用在恢复之后要按
        代次重新对。位置记在步骤上，实例每次现取——这也是 01「代次要跟着
        设备走」那条纪律在订单侧的落点。
        """
        registry = self.service.registry
        node_key = _node_key_of(step)
        if registry is None or node_key is None:
            return None
        record = registry.get(node_key)
        return record.ref if record is not None else None

    def _port_for(self, step):
        # type: (object) -> tuple
        """这一步该往哪台接口投料。返回 `(PortState, blocker)`。"""
        service = self.service
        if service.ports is None:
            return None, BlockReason(
                Reason.NO_ADAPTER, u"本次装配没有注入接口状态集合"
            )
        node_key = _node_key_of(step)
        if node_key is None:
            return None, BlockReason(
                Reason.PATTERN_STALE,
                u"第 %d 步没有可用的提供者位置" % (_step_index_of(step),),
            )
        return self._resolve_port(node_key)

    def stage_inputs(self, node_key, key, count, op_id):
        # type: (object, object, int, str) -> object
        """把一批料备到 `node_key` 这台样板口的中转位上。**返回 `CommandResult`。**

        这是 07 的 `push_inputs` 唯一的落点，也就是 `execution.py` 那句
        「`push_inputs` 必须是 `port_stage` 的薄封装」的**实现**：它自己
        不碰容器、不碰中转位、不写批次，只做判定然后把命令交给
        `_port_stage`（经注入的 `PortGateway` → 主控准入链）。

        **为什么不是又一份命令构造**：`port_stage` 命令只有一个构造点
        （`_port_stage`），请求号的形状只有一个来源
        （`_port_stage_request_id`）。多一份构造就会在 `op_id` 形状上分叉，
        而去重账本按请求号记账——分叉之后同一批料备两遍，两侧账本各自
        都显示正常。

        它与订单那条路（`_stage_next_input`）的区别只有两点，都是**有意的**：
        订单先放掉预留再取、取完补回来，这里没有订单可依附，因此**不动预留**；
        订单每一步新开一个 `op_id`，这里的 `op_id` 由调用方按执行给，
        同一个执行重投要能命中同一个批次。

        四分支，**顺序即语义**：

        1. 口不可用（不是样板口 / 装配仓 / 被 `port_blocker` 挡）→ 原样报出；
        2. **自己的 `op_id` 且那一批已经备进去过料** → 用现成的批次**应答，
           不再发命令**。这一支不是优化，是正确性：`op_id` 是确定性的，
           而主控账本有保留期（`LedgerPolicy.retention_ticks`），过期之后
           重放不再命中——`stage_batch` 会**真的再备一遍**；
        3. **自己的 `op_id` 但那一批一个都没备进去**（`staged == 0`）→
           **放行重发**。`stage_batch` 是**先**把批次写进状态槽、**再**判
           中转位，因此一次被拒的推送会留下一个 `staged == 0` 的自家批次；
           这一支是它唯一的解药，没有它那台口会被一个空批次永久占住；
        4. 槽被**别的**批次占着 → `ORDER_PORT_BUSY`，让位。

        返回 `None` 只有一种情况：命令网关不可用（没有主控 / 网络没开放），
        与 `_port_stage` 的约定一致。
        """
        request_id = _port_stage_request_id(op_id, node_key)
        port, blocker = self._resolve_port(node_key)
        if blocker is not None:
            return CommandResult.blocked(request_id, [blocker])
        batch = getattr(port, "batch", None)
        if (
            batch is not None
            and batch.op_id == op_id
            and int(batch.staged or 0) > 0
        ):
            # 同一批已经备过了：把**那一个**批次的读数原样报回去，一个字节
            # 都不再动。`moved` 因此是「这一批总共备进去多少」，
            # 而不是「这次新搬了多少」——调用方按 `op_id` 去重，不能累加。
            return CommandResult.ok(request_id, {"batch": batch.to_dict()})
        foreign = _foreign_batch_for(port, op_id)
        if foreign is not None:
            return CommandResult.blocked(request_id, [foreign])
        return self._port_stage(port, key, count, op_id)

    def _resolve_port(self, node_key):
        # type: (object) -> tuple
        """按**位置**判定「这台设备能不能作为样板口接收投料」。返回 `(port, blocker)`。

        与 `_port_for` 分开是因为：它只有位置这一个输入，因此订单执行器
        （`_port_for`，位置从步骤上现取）与 `stage_inputs`（07 的契约入口
        `push_inputs` 走这里）**共用同一套判定**。两处各写一份的代价是
        两边会在「什么样的口能投料」上分叉，而症状是一条命令莫名其妙被拒。

        **判定的顺序即语义**：装配仓的拒绝必须排在 `ports.get` **之前**。
        `ROLE_OF`（`devices/io.py`）里没有 `assemble_chamber`，装配仓根本
        没有对应的 `PortState`，先查口只会得到 `no_adapter`——
        那是一个**指错方向**的诊断：它会让人去查「为什么没有接口状态」，
        装配仓使用 assembly_advance 与实体槽，不能使用接口的单格中转批次。
        """
        service = self.service
        if service.ports is None:
            return None, BlockReason(
                Reason.NO_ADAPTER, u"本次装配没有注入接口状态集合"
            )
        record = service.registry.get(node_key) if service.registry is not None else None
        if record is not None and service.definitions is not None:
            definition = service.definitions.get(record.device_id)
            if getattr(definition, "category", "") == CATEGORY_ASSEMBLY:
                # 装配须带订单执行实例与槽位租约，不能从裸 push_inputs 绕过。
                return None, BlockReason(
                    Reason.UNSUPPORTED,
                    u"装配仓须由订单经 assembly_advance 推进：%s 不接受接口备料"
                    % (record.device_id,),
                )
        port = service.ports.get(node_key)
        if port is None:
            return None, BlockReason(
                Reason.NO_ADAPTER,
                u"提供者不是可投料的样板接口（06 的 %s 角色）"
                % (service.template_role,),
            )
        if port.role != service.template_role:
            return None, BlockReason(
                Reason.UNSUPPORTED,
                u"%s 的角色是 %s，只有样板口能接收工厂投料"
                % (port.device_id, port.role),
            )
        if service.port_blocker is not None:
            blocker = service.port_blocker(port, getattr(service.ports, "policy", None))
            if blocker is not None:
                return None, blocker
        return port, None

    def _port_stage(self, port, key, count, op_id):
        # type: (object, object, int, str) -> object
        """把 `port_stage` 作为一条**设备发起的存储命令**交出去。

        这是 07-05 明令的那条路：`push_inputs` 必须是 `port_stage` 的薄封装、
        经主控准入、不得自己碰容器或中转位。命令形状抄 06 的
        `PortRunner._build_command`（`source.player_id` 为 None——它不是玩家，
        是设备自己），这样它照样过主控的去重账本与准入链。

        **这是全项目唯一一处构造 `port_stage` 命令的地方。** 订单执行器
        （`_stage_next_input`）与契约入口（`FactoryService.stage_inputs`，
        即 07 的 `push_inputs`）都经由它，因此请求号的形状只有一个来源。
        """
        gateway = self.service.gateway
        if gateway is None or not gateway.available():
            return None
        ref = port.ref
        command = DeviceCommand(
            _port_stage_request_id(op_id, ref.node_key),
            CommandKind.STORAGE,
            CommandSource(
                None, ref, ref.node_key.as_key()[0], distance_ok=True
            ),
            payload={
                "action": "port_stage",
                "key": key.to_dict(),
                "count": int(count),
                "op_id": op_id,
            },
            topology_version=gateway.topology_version(),
            device_ref=ref,
        )
        return gateway.submit(command)

    def _track_dispatch(self, order, instance, tick):
        # type: (object, object, int) -> int
        """盯着接口把中转位里的料投进机器。

        读数来自 06 的 `PortState.batch`（06 明确把「后续推进」交给 09）。
        **只认 `op_id` 是自己的那一个批次**：批次槽只有一个，
        `stage_batch` 会覆盖它，而把别人的批次当自己的读会让这一次执行
        以为料已经投出去了。

        **批次槽只有一个，而执行可以有多个。** 同一台接口上先做第 1 次、
        再排队等第 2、3、4 次是正常形状（`_finish_inputs` 会把 `op_id` 清掉
        让开槽位），因此「别人的批次占着」不是错误，只是要等——它是
        `ORDER_PORT_BUSY`，可重试，下一帧再看。
        """
        ports = self.service.ports
        ref = self._step_ref(instance)
        # **按实例引用取，不按位置取。** 位置拿到的是「现在站在那里的东西」，
        # 而这一次执行要的是「当初投料的那一台」——设备被换掉之后两者不是
        # 一回事，而拿新设备去读旧批次的表现是「读数莫名其妙地变了」。
        port = ports.get_by_ref(ref) if (ports is not None and ref is not None) else None
        if port is None:
            return self._block_instance(
                order,
                instance,
                tick,
                BlockReason(Reason.DEVICE_GONE, u"投料用的样板口已经不在这个位置了"),
            )
        batch = port.batch
        if batch is None or batch.op_id != instance.op_id:
            busy = _foreign_batch(port, instance)
            if busy is not None:
                return self._block_instance(order, instance, tick, busy)
            return self._block_instance(
                order,
                instance,
                tick,
                BlockReason(
                    Reason.ORDER_BATCH_STALLED,
                    u"中转位的批次记录不见了，不知道已经投出去多少",
                ),
            )
        self._sync_own_batch(instance, port)
        order.stats["dispatches"] += 1
        if not batch.is_complete():
            # **推它一把。** 接口自己也有一轮轮询在做同一件事，但那一轮的
            # 节奏是调度器的退避（20 帧起步、上限 200 帧）——等它意味着
            # 「投一次料要好几秒」，而执行器本来就在每帧推进这张订单。
            # 两边同时做不会超发：`dispatch_template` 每次都用**当时的**
            # `batch.remaining()` 夹一次，而两次调用在同一线程里是先后发生的。
            self._port_dispatch(port)
            # 推完**再读一次**：这一次可能就投完了，而多等一帧纯属浪费。
            self._sync_own_batch(instance, port)
        if _batch_is_dead(port, instance):
            # 这一批推不动了（还差一截，而中转位空了）。回 STAGING 按差额
            # 再备一批——**只备差额**，不是把整批重发一遍。
            instance.status = StepStatus.STAGING
            instance.detail = u"这一批没投完，按差额重新备料"
            return 1
        if not batch.is_complete():
            if batch.reason:
                return self._block_instance(
                    order,
                    instance,
                    tick,
                    BlockReason(Reason.ORDER_BATCH_STALLED, batch.reason),
                )
            return 1
        if instance.all_inputs_delivered():
            return self._finish_inputs(order, instance, tick)
        instance.status = StepStatus.STAGING
        return 1

    def _port_dispatch(self, port):
        # type: (object) -> object
        """让这台接口把中转位里的料投给朝向容器。**返回结果或 None。**

        与 `_port_stage` 同一条路：设备发起的 `STORAGE` 命令、经主控准入。
        `intent` 必须是接口自己的角色，否则 06 会以 `PORT_DIRECTION_MISMATCH`
        拒绝——那条校验在服务端，不靠调用方自觉。
        """
        gateway = self.service.gateway
        if gateway is None or not gateway.available():
            return None
        ref = port.ref
        return gateway.submit(
            DeviceCommand(
                port.next_request_id(),
                CommandKind.STORAGE,
                CommandSource(None, ref, ref.node_key.as_key()[0], distance_ok=True),
                payload={"action": "port_dispatch", "intent": port.role},
                topology_version=gateway.topology_version(),
                device_ref=ref,
            )
        )

    def _sync_own_batch(self, instance, port):
        # type: (object, object) -> int
        """把**这一次执行的那一个批次**的真实读数同步进它的 `StepInput`。

        **绝对量，不是最大值。** `delivered` = 本批之前已经投进去的
        （`delivered_base`）+ 本批的读数。用 `max()` 在换批时会丢读数：
        新批次的 `delivered` 从 0 重新数，而 `max` 会把它当成「没变」，
        于是这次执行以为料还没投出去，然后**把整批重投一遍**。

        只认 `op_id` 是自己的那一个批次：批次槽只有一格，`stage_batch` 会
        覆盖它，而把别人的批次当自己的读会让这次执行以为料已经投出去了。
        """
        batch = getattr(port, "batch", None)
        if batch is None or instance.op_id is None or batch.op_id != instance.op_id:
            return 0
        item = _input_of(instance, batch.resource_key)
        if item is None:
            return 0
        item.delivered = item.delivered_base + int(batch.delivered or 0)
        return int(batch.delivered or 0)

    def _finish_inputs(self, order, instance, tick):
        # type: (object, object, int) -> int
        """这一次执行的料全进了机器：登记等待台账，然后等产物。

        **登记的单位是执行实例，不是步骤**（契约 §2：一个执行实例 = 一次
        配方执行）。`execution_id` 在实例创建那一刻就定下了（`订单/步骤/执行
        序号`），这里只是把它交给 10——重试沿用同一个 ID，等待索引因此不会断。

        **不在这里判订单级阻塞。** 回流入口没接线是订单级的事实，而此刻
        同一张订单里别处可能还在推；由 `_settle_blockers` 按「本帧有没有真的
        往前走」统一判。
        """
        expectations = instance.outputs + instance.returns
        self.waiting.register(instance.execution_id, expectations, tick)
        self.service.returns.register(
            instance.execution_id, expectations, order.order_id
        )
        instance.op_id = None
        instance.port_node_key = None
        if instance.outputs_satisfied():
            return self._complete_execution(order, instance, tick)
        instance.status = StepStatus.WAITING_OUTPUTS
        instance.tick = tick
        instance.detail = u"已投料，等待产物回流"
        return 1

    def _track_outputs(self, order, instance, tick):
        # type: (object, object, int) -> int
        if _ready_to_finish(instance, self.waiting):
            return self._complete_execution(order, instance, tick)
        return 0

    def _complete_execution(self, order, instance, tick):
        # type: (object, object, int) -> int
        """一次配方执行完成：**释放它的那 1 个槽位**，进度加 1。

        R04：「每次配方执行占 1 槽**至完成**」——`WAITING_OUTPUTS` 期间仍然
        占着，因此释放点在这里，不在投完料那一刻。释放之后下一次派发轮
        （`_dispatch_round`）就会用这个空位开出下一个实例。
        """
        self._unregister(instance.execution_id)
        instance.status = StepStatus.COMPLETED
        instance.blockers = []
        instance.detail = u"已完成"
        instance.tick = tick
        self.service.slots.release_slot(instance.execution_id)
        order.inflight.pop(instance.execution_id, None)
        # `finish_execution` 是**唯一**让进度加一的地方，而且幂等：
        # 重复回执走不到这里（`waiting.observe` 已经按 `op_id` 去过重）。
        instance.step.finish_execution(instance)
        self._complete_if_done(order, tick)
        return 1

    def _complete_if_done(self, order, tick):
        # type: (object, int) -> int
        """全部步骤的每一次执行都完成了就结单。

        **「最终产物已实际存入网络管理的存储」是结构性成立的，不在这里
        再查一次库存。** 执行实例只有收到 `receive_output()` 才算完成，而那份
        回执按 10 的契约（10-04）只在资源**真的进入了网络管理的存储**时才发；
        零步骤的 `HOLD` 计划则由「已在库中满足」直接结单。

        再查一次 `query(target).stored >= amount` 反而是错的：玩家随时可以
        把产物取走，那时订单会被判成永远完不成，而它其实早就做完了。
        """
        for step in order.steps:
            if not step.is_fulfilled():
                return 0
        self._release_everything(order)
        order.blockers = []
        order.move_to(
            OrderStatus.COMPLETED,
            u"全部 %d 个步骤、%d 次配方执行完成，产物已进入网络存储"
            % (len(order.steps), order.tasks_total),
            tick,
        )
        self.orders.stats["completed"] += 1
        self.service.stats["completed"] += 1
        return 1

    # -- 容量与只读视图 ------------------------------------------------

    def refresh_capacity(self):
        # type: () -> None
        self.service.refresh_capacity()

    def view(self, order_id=None):
        # type: (str) -> object
        if order_id is not None:
            order = self.orders.get(order_id)
            return order.to_dict() if order is not None else None
        return [order.to_dict() for order in self.orders.orders()]


# -- 工具 ------------------------------------------------------------------


def service_sink_available(service):
    # type: (object) -> bool
    """回流的接收侧是不是真的接上了。

    `AbsentReturns` 是替身，它的 `describe()["available"]` 为假。
    写成函数而不是直接看类名：任何将来接上的实现只要答得出这一项就行。
    """
    return bool(service.returns.describe().get("available", True))


def _sink_blocker(service):
    # type: (object) -> object
    """替身要报的那条原因；真接上了就返回 None。"""
    if service_sink_available(service):
        return None
    blocker = getattr(service.returns, "blocker", None)
    if blocker is None:
        return BlockReason(
            Reason.ORDER_RETURNS_ABSENT, u"产物回流入口尚未接线（任务 10）"
        )
    return blocker()


def _blocked(request_id, code, detail):
    # type: (str, str, str) -> CommandResult
    return CommandResult.blocked(request_id, [BlockReason(code, detail)])


def _rejected(code, detail):
    # type: (str, str) -> CommandResult
    return CommandResult.rejected("query", [BlockReason(code, detail)])


def _blocked_keys_reason(plan):
    # type: (object) -> BlockReason
    rows = plan.blocked_keys or []
    return BlockReason(
        Reason.PLAN_MISSING_MATERIALS,
        u"计划里有 %d 种资源实际上拿不到（%s）"
        % (
            len(rows),
            u"、".join(item.get("canonical", "?") for item in rows[:4]),
        ),
    )


def _read_target(payload):
    # type: (dict) -> tuple
    """读下单命令的 `target` 与 `amount`。**参数校验与「做不到」分开报。**"""
    raw = (payload or {}).get("target")
    amount = (payload or {}).get("amount")
    if not isinstance(raw, dict):
        return None, None, [
            BlockReason(
                Reason.BAD_PARAMETER,
                u"下单要给出 target（资源键的字典形式）与 amount（正整数）",
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
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        return None, None, [
            BlockReason(
                Reason.BAD_PARAMETER, u"amount 必须是正整数，得到 %r" % (amount,)
            )
        ]
    return key, amount, None


def _key_map(definition):
    # type: (object) -> dict
    """样板定义里的 `canonical -> ResourceKey`。输入、产物、返还物都要进。"""
    out = {}
    for stack in (
        tuple(definition.inputs)
        + tuple(definition.outputs)
        + tuple(definition.returns)
    ):
        out[stack.key.canonical()] = stack.key
    return out


def _fill_step(step, row, keymap, policy):
    # type: (object, dict, dict, object) -> bool
    """把计划里那一步的**配方清单**填进 `OrderStep`。

    对不上就返回 `False`：计划说有、样板说没有，两者说的是两件事，
    照着任何一边去做都是错的。

    ## 填的是 `per_batch`，不是 `total`

    `total` 是「这一步跑完全部批次要多少」，而 R04 之后**一次配方执行
    就是一批**：每个执行实例领自己那一份（见 `ExecutionInstance.__init__`）。
    填 `total` 会让一次执行去认领整步的量，10 个实例各要一整份——
    实际抽取量会变成计划量的 10 倍。08 已经保证 `total == per_batch * batches`
    （`planner.CraftStep.input_totals`），因此两种填法在整步这一层等价，
    差别只在**谁认领**。

    **`external_inputs` 刻意不进 `inputs`**：那是配方「外部自理」的输入
    （R09 的无限燃料一侧），网络不该去替它备料——把它算进来会去抽一份
    真实库存里根本没有、也不该有的东西。

    **返还物算不算完成条件由 `OrderPolicy.returns_block_completion` 决定**
    （R09），替身取 `True`：09 验收原文「所需鱼已回流但空桶未到时步骤仍工作中」
    说的正是「返还物没到齐就不算做完」。把它硬编码成 `False` 会让那条验收
    在实现里直接反过来。**在构建时定一次**，于是还没有任何实例时步骤视图
    也答得出这个问题。
    """
    inputs = {}
    for entry in row.get("inputs") or ():
        canonical = entry.get("canonical")
        key = keymap.get(canonical)
        if key is None:
            return False
        step.keys[canonical] = key
        inputs[canonical] = int(entry.get("per_batch") or 0)
    outputs = {}
    for entry in row.get("outputs") or ():
        canonical = entry.get("canonical")
        key = keymap.get(canonical)
        if key is None:
            return False
        step.keys[canonical] = key
        outputs[canonical] = int(entry.get("per_batch") or 0)
    returns = {}
    for entry in row.get("returns") or ():
        canonical = entry.get("canonical")
        key = keymap.get(canonical)
        if key is None:
            return False
        step.keys[canonical] = key
        returns[canonical] = int(entry.get("per_batch") or 0)
    step.input_specs = tuple(sorted(inputs.items()))
    step.output_specs = tuple(sorted(outputs.items()))
    step.return_specs = tuple(
        (canonical, per_batch, policy.returns_block_completion)
        for canonical, per_batch in sorted(returns.items())
    )
    return True


def _provider_device(row):
    # type: (dict) -> str
    provider = row.get("provider") or {}
    return provider.get("device_id") or ""


def _step_index_of(step):
    # type: (object) -> int
    """**步骤**下标。

    执行实例上也有一个 `index`，那是**执行序号**（这一步的第几次执行），
    两者在诊断里是完全不同的东西。执行实例把所属步骤放在 `step` 上，
    因此这里统一从它取。
    """
    inner = getattr(step, "step", None)
    if inner is not None:
        return int(inner.index)
    return int(step.index)


def _node_key_of(step):
    # type: (object) -> object
    """步骤的提供者位置。计划里存的是元组，注册表要的是 `NodeKey`。

    步骤与执行实例都答得出这一项（实例把它代理给所属步骤，
    见 `ExecutionInstance.node_key`），因此两边共用这一条读法。
    """
    node_key = step.node_key
    if node_key is None:
        return None
    if hasattr(node_key, "as_key"):
        return node_key
    try:
        values = tuple(node_key)
        return NodeKey.from_tuple(values[0], values[1:])
    except (TypeError, ValueError, IndexError):
        return None


def _buffered_of(port, key):
    # type: (object, object) -> int
    """这台接口的中转位里还躺着多少该资源。取不到读数时返回 0。"""
    buffer = getattr(port, "buffer", None)
    if buffer is None:
        return 0
    return int(buffer.total_of(key) or 0)


def _ready_to_finish(step, waiting):
    # type: (object, object) -> bool
    """这一步的产物已经收齐、只差收尾了吗。

    **两种收齐都要认**：台账说「满足」（`WaitingLedger.is_satisfied`），
    或者步骤自己的产物账说「满足」（`StepOutput.received`）。前者是 10
    登记过的那种，后者是**没登记过却已经收到**的那种——真机上手工把入库的
    资源分配过来时走的就是第二条，只认第一条会让订单永远停在阻塞上。
    """
    if step.status != StepStatus.WAITING_OUTPUTS:
        return False
    if step.execution_id is not None and waiting.is_satisfied(step.execution_id):
        return True
    return step.outputs_satisfied()


def _owns_batch(port, step):
    # type: (object, object) -> bool
    """样板口上的这一批**是本步**的吗（不区分投完没有）。"""
    batch = getattr(port, "batch", None)
    return (
        batch is not None
        and step.op_id is not None
        and batch.op_id == step.op_id
    )


def _batch_is_dead(port, step):
    # type: (object, object) -> bool
    """本步的批次**已经推不动了**：还没投完，而中转位里已经没有它的料。

    这条判据是必需的。备料只备了一部分时（介质读速、原料缓冲容量都会
    造成这种情况），批次记的 `requested` 是**想要的量**，而中转位里只有
    实际搬来的那些。投完之后批次仍然不满足 `is_complete()`，接口那边也会
    报「中转位里已经没有这批资源」——**而它自己不会放弃**。

    没有这条判据，执行器会一直等一个永远完不成的批次：它既不重试、
    也不报错，`delivered` 停在半路上，而界面上看起来「还在投」。
    """
    batch = getattr(port, "batch", None)
    if batch is None or batch.is_complete() or step.op_id is None:
        return False
    if batch.op_id != step.op_id:
        return False
    return _buffered_of(port, batch.resource_key) <= 0


def _owns_live_batch(port, step):
    # type: (object, object) -> bool
    """样板口上正在投的这一批**是本步**的吗，而且还有东西要投。"""
    return _owns_batch(port, step) and not _batch_is_dead(port, step)


def _port_stage_request_id(op_id, node_key):
    # type: (str, object) -> str
    """`port_stage` 命令的请求号。

    **只有这一个来源。** 主控的去重账本按 `(request_id, device_ref)` 记账
    （`network/controller.py` 的 `ledger_key`），因此请求号的形状一旦有两份
    写法，同一条命令就会被记成两笔；而症状是「重试时又备了一遍」，
    两侧账本各自都显示正常。位置进请求号是必需的：同一个 `op_id`
    落到不同口上**本来就是两条不同的命令**。
    """
    return "%s:%s" % (op_id, node_key.as_key())


def _foreign_batch(port, step):
    # type: (object, object) -> object
    """样板口的批次槽是不是被别人占着。返回 `BlockReason` 或 None。"""
    return _foreign_batch_for(port, step.op_id)


def _foreign_batch_for(port, op_id):
    # type: (object, str) -> object
    """样板口的批次槽是不是被**别的** `op_id` 占着。返回 `BlockReason` 或 None。

    判据只有三条，**每一条都是必需的，且不得放松**：

    - 槽是空的 → 随便用；
    - 槽里那批**已投完** → 它已经完成了使命，让位；
    - 槽里那批就是自己的 → 自己的批次，接着推。

    否则就是别人的活批次，**本订单让位**。这里**刻意不看中转位里有没有料**：
    「批次未完成 + 中转位空着」正是 `_batch_is_dead` 描述的那种局面，
    此时冲掉记录会把一批已经搬进来、还没投出去的料变成**账外资产**——
    批次没了，没人知道中转位里那些料是谁的。
    """
    batch = getattr(port, "batch", None)
    if batch is None:
        return None
    if batch.is_complete():
        return None
    if op_id is not None and batch.op_id == op_id:
        return None
    return BlockReason(
        Reason.ORDER_PORT_BUSY,
        u"样板口上挂着另一个批次（%s，还差 %d 个）；批次槽只有一格，"
        u"本订单让位，不冲掉它的投料记录"
        % (batch.op_id, batch.remaining()),
    )


def _input_of(step, key):
    # type: (object, object) -> object
    canonical = key.canonical() if hasattr(key, "canonical") else str(key)
    for item in step.inputs:
        if item.canonical == canonical:
            return item
    return None


# -- 网络服务门面 ----------------------------------------------------------


class FactoryService(NetworkService):
    """`ServiceId.FACTORY` 的服务实现（订单与执行）。

    **每张网络一份**（服务槽位是逐运行时各造一套），因此两张网络不共享订单、
    预留或槽位租约——03 验收第 4 条。共享的东西由装配层注入：规划器、样板
    索引、接口状态集合都是**全进程一份**的（它们本来就跨网络，见各自模块）。
    """

    service_id = ServiceId.FACTORY
    #: **只依赖存储。** 订单不注册到期任务（推进是每帧 `tick` 的直接工作），
    #: 因此不需要排在调度器之后——声明一个用不到的依赖只会让装配顺序多一条
    #: 并非真需要的边。存储是**真的**依赖：预留、库存查询与 `port_stage`
    #: 都落在它身上，没有它订单一步都走不动。
    requires = (ServiceId.STORAGE,)
    is_stand_in = False

    def __init__(
        self,
        runtime=None,
        policy=None,
        planner=None,
        patterns=None,
        ports=None,
        gateway=None,
        returns=None,
        port_blocker=None,
        template_role=DEFAULT_TEMPLATE_ROLE,
        registry=None,
        definitions=None,
    ):
        # type: (object, object, object, object, object, object, object, object, str, object, object) -> None
        self.service_id = ServiceId.FACTORY
        self.policy = policy if policy is not None else OrderPolicy()
        #: 08 的规划器门面。**同一个对象**：诊断报出来的策略就是实际生效的那份。
        self.planner = planner
        #: 07 的样板索引（只读用它）。
        self.patterns = patterns
        #: 06 的接口状态集合（读批次状态 + 找投料口）。跨网络共享一份。
        self.ports = ports
        #: 设备发起的命令网关（鸭子类型：`available()` / `topology_version()` /
        #: `submit()`）。由装配层注入而不是 `crafting/` 自己 import 设备模块。
        self.gateway = gateway
        #: 任务 10 的接收侧。默认是明确报「没接上」的替身。
        self.returns = returns if returns is not None else AbsentReturns()
        #: 06 的 `port_blocker(state, policy)`，同样是注入。
        self.port_blocker = port_blocker
        self.template_role = template_role
        self.registry = registry
        self.definitions = definitions
        self.runtime = None
        #: 槽位与内存台账。它们**就是** `capabilities.ProcessingSlots` 与
        #: `capabilities.Memory` 的实现（见 `capacity.py` 的 docstring）。
        self.slots = SlotLedger(self.policy)
        self.memory = MemoryLedger(self.policy)
        #: 每网络的订单集合。**在 attach 时按 component_id 建**，
        #: 因此它不可能串到别的网络上。
        self.orders = None
        #: 去重表的上界取自策略：它是唯一一张只增不减的表（见 `jobs`）。
        self.waiting = WaitingLedger(self.policy.op_ledger_limit)
        #: **被拒请求的台账**（2026-09-26 规则）。它**不是订单**：不占活跃
        #: 名额、不占内存与并行槽，只是给玩家回看的近期记录。
        self.rejected = RejectedRequestStore(self.policy.rejected_request_limit)
        self.cursor = 0
        #: 最近一次容量重算里「贡献零槽位」的处理器群（诊断用）。
        self.idle_groups = []
        self.memory_slots = 0
        self.executor = OrderExecutor(self)
        self.stats = {
            "submitted": 0,
            "completed": 0,
            "unblocked": 0,
            "stagings": 0,
            #: 被拒绝的下单请求数。**与订单统计分开**：被拒的不是订单。
            "rejected": 0,
        }
        if runtime is not None:
            self.attach(runtime)

    # -- 契约 ----------------------------------------------------------

    def attach(self, runtime):
        # type: (object) -> None
        self.runtime = runtime
        if self.orders is None:
            self.orders = OrderStore(runtime.component_id)
        if self.registry is None:
            self.registry = runtime.registry
        if self.definitions is None:
            self.definitions = runtime.definitions

    def detach(self):
        # type: () -> None
        self.runtime = None

    def is_ready(self):
        # type: () -> bool
        return self.planner is not None and self.patterns is not None

    def handle(self, grant, command):
        # type: (object, object) -> CommandResult
        """订单命令。`order` 是**写命令**：它一条都走主控准入与去重账本。"""
        payload = dict(command.payload or {})
        action = payload.get("action")
        if action == "submit":
            self.stats["submitted"] += 1
            return self.executor.submit_order(command, payload)
        if action == "cancel":
            return _with_request(command.request_id, self.executor.cancel(
                payload.get("order_id") or ""))
        if action == "pause":
            return _with_request(command.request_id, self.executor.pause(
                payload.get("order_id") or ""))
        if action == "resume":
            return _with_request(command.request_id, self.executor.resume(
                payload.get("order_id") or ""))
        if action in ("list", "detail"):
            # 只读查询也放在这里：11 的终端会把它当**命令**发（要带会话与
            # 距离），而 `debug_query` 那条路不经过主控、只在没有主控时
            # 用来排障。两条路给的是同一份纯数据。
            return CommandResult.ok(
                command.request_id, self.query(action, payload)
            )
        return CommandResult.rejected(
            command.request_id,
            [
                BlockReason(
                    Reason.UNSUPPORTED,
                    u"订单命令支持 submit / cancel / pause / resume / list / detail，"
                    u"得到 %r" % (action,),
                )
            ],
        )

    def tick(self, context, budget):
        # type: (dict, int) -> int
        return self.executor.tick(context, budget)

    def add_node(self, node, saved_data=None):
        # type: (object, dict) -> bool
        """**不做逐节点的挂载。**

        槽位来自拓扑的处理器群、内存来自网络成员，两者每帧从权威来源重算
        （`refresh_capacity()`），因此这里没有「第二份设备索引」要维护——
        `script_architecture.md` §3 定的纪律是一个事实只有一个来源。
        返回 `False` 表示没有新的东西被挂上，这是如实的。
        """
        return False

    def remove_node(self, node):
        # type: (object) -> bool
        """**不撤销订单。**

        一台设备离开这张网络不等于它的订单没了：预留记在 05、在途批次记在
        06，两者都跨网络重建存活。订单会在下一次推进时发现自己被挡住
        （`_blockers_for` 会看到主控或网络状态不对），转成 `BLOCKED` 并
        报出原因，而不是被悄悄清掉。
        """
        return False

    # -- 给样板提供者的契约入口（07-05）---------------------------------

    def stage_inputs(self, node_key, key, count, op_id):
        # type: (object, object, int, str) -> object
        """**07 的契约入口走的唯一一条路。**见
        `OrderExecutor.stage_inputs` ——它才是判定与发命令的地方，
        这一层只是把服务门面暴露出来（07 拿到的是 `FactoryService`，
        不是执行器）。
        """
        return self.executor.stage_inputs(node_key, key, count, op_id)

    def status(self):
        # type: () -> dict
        return {
            "service_id": self.service_id,
            "available": not self.is_stand_in,
            "ready": self.is_ready(),
            #: 还活着的订单条数（终态的不算）。逐张明细在 `diagnostics()`。
            "orders": self.orders.live_count() if self.orders is not None else 0,
            "slots": self.slots.capacity,
            "memory": self.memory.capacity,
            #: 回流入口接上了没有。**布尔**，与 `diagnostics()["returns"]`
            #: 那个字典不同名——同名同槽位会让「接上了没有」被一份明细盖掉，
            #: 而读的人看到的是一份永远为真的字典。
            "returns_wired": service_sink_available(self),
        }

    def diagnostics(self):
        # type: () -> dict
        """逐张订单在 `query()` 里；这里只给台账、租约与策略。

        **键名与 `status()` 刻意错开**（`orders` 对 `order_store`、
        `returns_wired` 对 `returns`）：两层都给同一件事时，字典赋值会把
        前者悄悄盖掉，而盖掉之后的形状还挺像那么回事——查起来极费劲。
        """
        view = self.status()
        view["stand_in"] = self.is_stand_in
        view["pending_task"] = self.pending_task
        view["requires"] = list(self.requires)
        view["stats"] = dict(self.stats)
        view["cursor"] = self.cursor
        view["leases"] = {
            "slots": self.slots.diagnostics(),
            "memory": self.memory.diagnostics(),
        }
        view["waiting"] = self.waiting.diagnostics()
        view["rejected_requests"] = self.rejected.diagnostics()
        view["returns"] = self.returns.describe()
        view["order_store"] = (
            self.orders.diagnostics() if self.orders is not None else None
        )
        view["idle_processor_groups"] = list(self.idle_groups)
        view["memory_slots"] = self.memory_slots
        view["progress"] = self.progress()
        view["policy"] = self.policy.describe()
        view["unresolved"] = self.policy.unresolved_report()
        return view

    def progress(self):
        # type: () -> dict
        """本网络**全部未完成订单**的进度合计：配方数已做 / 总数。

        合计而不是平均：平均会让「10/10 完成」与「0/5 完成」加起来是 50%，
        而那份 0/5 一张都还没做。两个数（`tasks_completed` / `tasks_total`）
        一起给，读的人自己决定怎么用——界面要比例就除，要条数就直接用。

        **汇总的键是 `tasks_*`（配方执行次数），不是 `steps_*`（步骤数）。**
        两个分母同时给出去，必然有一处被读错：把「步骤数」当成进度条的分母，
        一条 10 批的订单会在第 1 批完成时显示 100%。`Order.progress()` 里
        `steps_*` 仍然给（它答的是「这张订单分几种配方」），但**这里只汇总
        `tasks_*`**。

        逐张订单的进度在 `query("list")` 的 `progress` 字段里，
        两边是同一个算法（`Order.progress()`），不存在第二份口径。
        """
        done = 0
        total = 0
        for order in self.orders.live() if self.orders is not None else ():
            row = order.progress()
            done += row["tasks_completed"]
            total += row["tasks_total"]
        return {
            "orders": self.orders.live_count() if self.orders is not None else 0,
            "tasks_completed": done,
            "tasks_total": total,
            "ratio": (float(done) / total) if total > 0 else 0.0,
            "note": "只统计未完成的订单；零任务的订单没有分母，不计入比例",
        }

    # -- 只读查询（11 与 `debug_query("order")` 共用同一份纯数据）--------

    def query(self, action="list", payload=None):
        # type: (str, dict) -> object
        """只读入口。**没有主控也能答**，答的正是最该看的那部分。"""
        payload = payload or {}
        if action == "detail":
            order_id = payload.get("order_id")
            view = self.executor.view(order_id)
            if view is None:
                return {
                    "error": "没有这张订单",
                    "order_id": order_id,
                    "known": [item["order_id"] for item in self.executor.view()],
                }
            return view
        orders = self.executor.view()
        if payload.get("status"):
            want = payload["status"]
            orders = [item for item in orders if item["status"] == want]
        return {
            "component_id": self.runtime.component_id if self.runtime else None,
            "orders": orders,
            "waiting": self.waiting.view(),
            "rejected_requests": [
                row.to_dict() for row in self.rejected.recent()
            ],
            "capacity": {
                "slots": self.slots.describe(),
                "memory": self.memory.describe(),
                "idle_processor_groups": list(self.idle_groups),
                "memory_slots": self.memory_slots,
            },
            "returns": self.returns.describe(),
            "policy": self.policy.describe(),
        }

    # -- 容量 ----------------------------------------------------------

    def refresh_capacity(self):
        # type: () -> None
        """从**权威来源**重算槽位与内存容量。

        槽位来自 02 的处理器群（本模块不自己遍历邻居去数方块），内存来自
        网络的成员列表。容量缩水**不夺回已有租约**，见 `capacity.py`。
        """
        runtime = self.runtime
        if runtime is None:
            return
        topology = getattr(runtime, "topology", None)
        snapshot = topology.snapshot() if topology is not None else None
        slots, idle = slot_capacity(snapshot, runtime.component_id)
        self.slots.refresh(slots)
        self.idle_groups = idle
        members = runtime.members() if hasattr(runtime, "members") else ()
        memory, memory_slots = memory_capacity(members, self.definitions, self.policy)
        self.memory.refresh(memory)
        self.memory_slots = memory_slots


def _with_request(request_id, result):
    # type: (str, object) -> CommandResult
    """把执行器给的结果换成带正确 `request_id` 的那一条。

    执行器不知道是谁在问（它拿的是订单 ID，不是请求 ID），因此由门面在
    最后一刻补上。**保留 status / payload / reasons 一字不动**——
    重造一份内容不同的结果，就等于把执行器的结论悄悄改掉。
    """
    if result.request_id == request_id:
        return result
    return CommandResult(
        request_id, result.status, result.payload, result.reasons,
        result.topology_version,
    )
