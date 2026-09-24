# coding=utf-8
"""服务端装配与启动/销毁。

对应任务 01-01、01-06 的入口与分帧补查部分，03 起接入主控与网络运行时。

**本批次的范围。** 装配 01 的设备层、02 的拓扑/负载层、03 的主控与运行时。
网络服务里存储、工厂、能源、调度仍是**替身**（任务 04/05/09），因此
**网络业务命令一律会被拒绝**并给出 `service_absent`——这是诚实的结果，
不是故障。所以默认装配档是 `AssemblyProfile.DEV`。

`PRODUCTION` 档**不是靠启动失败来保护的**，而是靠主控准入链：它的第 10 条
（装配档位）在策略仍有未定规则时把业务命令一律拒掉并给出
`assembly_unresolved`，第 12 条（所需服务）再拒掉替身服务。带未定编号的
数值因此**一次都不会**流进库存或订单，读起来是「待确认」——这正是
`01-交付说明.md` §5.7-2 要的形态（那里明确要的是运行期的「待确认」，
而不是让正式档起不来）。更严格的装配期检查是
`ServiceRegistry.assert_profile_ok()`，它**只在被显式调用时**才报错，
留给任务 14 的发布验收用。

**可以脱离游戏构造。** 适配器把引擎调用放在方法体内部而不是模块顶层，
所以本模块导入时不需要 `mod.server.*`；离线测试传 `bind_engine=False`
并自己注入假 world 即可驱动整条链路。
"""

from ..common.definitions import DefinitionSet
from .adapters.events import ServerEventBinder
from .adapters.world import ToolDeltaWorldAdapter
from .application import ApplicationService
from .devices.catalog import DeviceCatalog
from .devices.registry import DeviceRegistry
from .devices.storage_driver import DriverPolicy, StorageDriverStore
from .network.admission import AdmissionService
from .network.runtime import RuntimeManager
from .network.services import (
    AssemblyProfile,
    ServiceFactory,
    ServiceId,
    ServiceRegistry,
)
from .network.ticking import SchedulerService
from .network.topology import TopologyService
from .runtime.events import EventQueue
from .runtime.lifecycle import DeviceLifecycle
from .runtime.scheduler import LogicalClock, SchedulerPolicy
from .storage.medium import MediumPolicy
from .storage.service import StorageService


def _scheduler_builder(policy):
    # type: (object) -> object
    """把调度策略绑进调度服务的构造器（供 `ServiceFactory` 使用）。

    写成显式工厂而不是 `lambda runtime: SchedulerService(runtime, policy=policy)`：
    默认参数之外的闭包捕获容易在循环或后续重构里指向错的对象，而这里绑的
    是**全进程唯一**的那份策略。
    """
    def build(runtime):
        # type: (object) -> SchedulerService
        return SchedulerService(runtime, policy=policy)

    return build


def _storage_builder(drivers, medium_policy):
    # type: (object, object) -> object
    """把**驱动器状态集合**绑进存储服务的构造器。

    服务槽位是逐运行时各造一份的，而驱动器与介质是**跨网络共享**的
    （架构 §3：驱动器状态不能依赖某个网络实例存活）。因此注入的不是
    「每个网络一份的状态」，而是同一个集合——两个网络同时挂同一块盘时
    由服务侧的重复挂载检测拦住。
    """
    def build(runtime):
        # type: (object) -> StorageService
        return StorageService(runtime, drivers=drivers, policy=medium_policy)

    return build


class _PolicyBundle(object):
    """把几份未定案策略合成一份，交给 `assert_profile_ok` 一起检查。

    与 `BandwidthPolicy.unresolved_report()` 同形（列表元素是带 `field`/
    `rule`/`value` 的字典），因此不用改装配层的检查代码。
    少合一份的代价是正式验收时漏报一条未定规则——那正是全局约束 7 要防的。
    """

    __slots__ = ("_policies",)

    def __init__(self, policies):
        # type: (tuple) -> None
        self._policies = tuple(item for item in policies if item is not None)

    def unresolved_report(self):
        # type: () -> list
        out = []
        for policy in self._policies:
            report = getattr(policy, "unresolved_report", None)
            if report is None:
                continue
            out.extend(report())
        return out


class ServerBootstrap(object):
    """服务端运行时装配。

    `bind_engine=False` 时不注册任何引擎监听器，也不会调用引擎接口，
    用于离线验证；游戏内由 `start_server()` 以默认参数启动。
    """

    def __init__(
        self,
        definitions=None,
        world=None,
        drain_limit=256,
        backfill_chunks_per_tick=4,
        rebuild_steps_per_tick=64,
        change_sink=None,
        max_tick_errors=8,
        event_api=None,
        assembly_profile=AssemblyProfile.DEV,
        service_factories=None,
        dedup_policy=None,
        repository=None,
        runtime_budget=64,
        session_check=None,
        clock=None,
        scheduler_policy=None,
        driver_policy=None,
        medium_policy=None,
    ):
        # type: (DefinitionSet, object, int, int, int, object, int, object, str, object, object, object, int, object, object, object, object) -> None
        self.definitions = definitions if definitions is not None else DefinitionSet()
        #: 逻辑时钟（04-01）。**它是业务时间的唯一依据**，由 `drive_once()`
        #: 每帧推进一步，经 `context["tick"]` 下传给各服务。注入自有实例即可
        #: 在离线测试里精确控制时间（`clock.advance(200); server.tick()`），
        #: 这就是「假时钟可精确重现低速累积、卡顿和恢复」的入口。
        self.clock = clock if clock is not None else LogicalClock()
        self.world = world if world is not None else ToolDeltaWorldAdapter()
        self.drain_limit = int(drain_limit)
        self.backfill_chunks_per_tick = int(backfill_chunks_per_tick)
        #: 每帧允许展开多少个图节点。跨帧重建的预算（02-02）。
        self.rebuild_steps_per_tick = int(rebuild_steps_per_tick)
        #: **额外**的设备变化观察者。拓扑服务始终消费设备变化，
        #: 这个钩子只是让测试与后续服务也能看到同一批变化。
        self.change_sink = change_sink

        self.registry = DeviceRegistry(self.definitions)
        self.catalog = DeviceCatalog(self.definitions)
        self.queue = EventQueue()
        self.lifecycle = DeviceLifecycle(
            self.definitions, self.registry, self.catalog, world=self.world
        )
        self.topology = TopologyService(
            self.definitions, self.registry, self.catalog
        )
        self.admission = AdmissionService(self.definitions, self.registry, self.topology)
        #: 每帧给运行时的服务推进预算。与图重建预算分开计：两者消耗的
        #: 是同一帧的时间，但一个是节点展开、一个是业务工作，不能混在一起。
        self.runtime_budget = int(runtime_budget)
        self.assembly_profile = AssemblyProfile.validate(assembly_profile)
        self.repository = repository
        #: 调度策略（04-03 的轮转旋钮与 04-02 的退避数值）。
        #: **全进程只有这一份对象**：跨网轮转（`RuntimeManager`）与每个网络的
        #: `SchedulerService` 都读它。各存一份的代价是
        #: `SchedulerPolicy.unresolved_report()` 报出来的数字与真正生效的
        #: 不是同一个（全局约束 7 要防的正是「用替身值悄悄定案」）。
        self.scheduler_policy = (
            scheduler_policy if scheduler_policy is not None else SchedulerPolicy()
        )
        #: 驱动器策略（R10）与介质策略（R07/R08）。同样**全进程各一份**：
        #: `unresolved_report()` 报出来的必须就是真正生效的那份数值，
        #: 各存一份的代价是诊断与实际行为对不上（全局约束 7）。
        self.driver_policy = (
            driver_policy if driver_policy is not None else DriverPolicy()
        )
        self.medium_policy = (
            medium_policy if medium_policy is not None else MediumPolicy()
        )
        #: 驱动器状态集合与介质权威表。**由启动层持有并注入网络服务**
        #: （架构 §3）：驱动器与介质的权威状态不能只活在某个网络的存储服务里，
        #: 网络断开、主控移除、运行时重建都不等于设备和库存消失。
        #: 它在 `admission` 之后构造，因为槽位变化要能立刻通知负载重算。
        self.drivers = StorageDriverStore(
            self.definitions,
            self.registry,
            policy=self.driver_policy,
            medium_policy=self.medium_policy,
            on_load_changed=self._note_driver_load_changed,
        )
        #: 网络服务装配表。`ServiceFactory` 为**每个运行时**各造一套实例，
        #: 因此两个网络绝不共享库存或订单状态（03 验收 #4）。
        if service_factories is not None:
            self.service_factories = service_factories
        else:
            # 默认装配表 + 把上面那两份策略绑进调度与存储槽位。只覆盖这两个，
            # 其余槽位仍是默认替身（`ServiceFactory` 从默认表起步）。
            self.service_factories = ServiceFactory(
                {
                    ServiceId.SCHEDULER: _scheduler_builder(self.scheduler_policy),
                    ServiceId.STORAGE: _storage_builder(
                        self.drivers, self.medium_policy
                    ),
                }
            )
        # 运行时管理器**必须在 `AdmissionService` 之后构造**：两者都订阅
        # 拓扑发布，准入先订阅才能保证每次发布时带宽判定已按新版本刷新过，
        # 而运行时在同一帧内读到的就是刷新后的结论。
        self.runtimes = RuntimeManager(
            self.definitions,
            self.registry,
            self.catalog,
            self.topology,
            self.admission,
            profile=self.assembly_profile,
            factories=self.service_factories,
            ledger_policy=dedup_policy,
            session_check=session_check,
            repository=repository,
            scheduler_policy=self.scheduler_policy,
        )
        self.application = ApplicationService(
            self.runtimes,
            self.topology,
            self.admission,
            registry=self.registry,
            profile=self.assembly_profile,
        )
        #: 引擎事件 API。注入替身即可在无引擎环境跑通真实的 `bind()` 路径，
        #: 从而验证注册与注销，而不用进游戏试。
        self.event_binder = ServerEventBinder(
            self.queue,
            self.world,
            self.registry,
            self.definitions,
            clock=self.clock,
            event_api=event_api,
        )
        # 生命周期把变化发布到这里，这是**唯一**出口：
        # 事件处理与启动补查/手动重扫都走它，不会有一条路径绕过去。
        self.lifecycle.set_change_sink(self._emit)

        self.started = False
        self.tick_count = 0
        self.max_tick_errors = int(max_tick_errors)
        self.tick_errors = []
        self.tick_error_count = 0
        self._backfill_generator = None
        #: 一次性补查是否已跑完。区块发现主要靠 CHUNK_LOADED 事件，
        #: 补查只是兜底与手动重扫入口，因此跑完就不再重建。
        self._backfill_finished = False
        self._change_counts = {}
        self._change_overflow = 0
        if change_sink is None:
            self._recent_changes = []
        else:
            self._recent_changes = None

    # -- 生命周期 ------------------------------------------------------

    def start(self, bind_engine=True):
        # type: (bool) -> None
        """启动。

        `bind_engine=False` 时**完全不碰引擎**：不注册白名单、不注册监听器。
        离线环境下 `tooldelta.events` 无法导入（它依赖 mod.client.extraClientApi），
        因此这条路径必须在导入之前就分开。
        """
        if self.started:
            return
        if bind_engine:
            # 顺序是有意的：先把本模组方块加入移除事件白名单，再注册监听器，
            # 否则启动瞬间的拆除事件会被引擎直接丢掉。
            self.event_binder.register_remove_whitelist()
            self.event_binder.bind()
            self._bind_tick_driver()
        self._backfill_generator = None
        self._backfill_finished = False
        # 启动时全量标脏：注册表里可能已有记录（重启、手动重扫），
        # 图必须由它们重新推导，而不是沿用上一轮的内存状态。
        self.topology.mark_all_dirty()
        self.started = True

    def _bind_tick_driver(self):
        """把 `OnScriptTickServer` 接到 `tick()` 上。

        **没有这一步整条链路是死的**：事件会一直入队，但没人消费。
        （真机验证时就是这么发现的——事件计数一直在涨，而 `ticks` 恒为 0。）

        任务 01 只负责「入队 → 消费 → 产生设备变化」这一段。
        把 tick 变成有预算、可轮转、可休眠的调度属于任务 04：
        届时由调度器调用 `tick()`，这里改成不注册即可，领域代码不受影响。

        注册走 `event_binder.register()`，因此 `stop()` 时会和其他监听器
        一起被注销。早先这个回调**完全没被注销**——同进程内 stop 再 start
        会让每帧推进两次。
        """
        api = self.event_binder.require_api()

        def _drive(_event):
            self.drive_once()

        self.event_binder.register(api.OnScriptTickServer, _drive)

    def drive_once(self):
        # type: () -> None
        """推进一帧：**先走时钟，再走业务**。异常只记录不抛出。

        **时钟只在这里前进一步。** 引擎每帧调用一次 `OnScriptTickServer`，
        因此 `LogicalClock` 的读数就是「引擎帧序」。放在这个回调里而不是
        「消费队列里的 `SCRIPT_TICK` 事件」，是因为后者受三件事影响而这三件
        都不可靠：`drain_limit`（一次区块加载的积压就能让时钟停走，而
        `tick_count` 照走，于是有了两套「帧」）、队列空时仍会推进别的步骤、
        以及同帧两个 `OnScriptTickServer` 监听器的注册顺序。放在这里，
        无论谁先谁后，每帧都恰好推进一次。

        `tick()` **自己不动时钟**：离线测试直接调 `tick()` 就是为了精确
        控制时间（04 验收要的「假时钟」入口）。

        异常只记录不抛出：引擎会以每秒数十次的频率调用它，任何一次抛出都会
        变成刷屏日志，而且会让后续帧全部失效。诊断里能看到 `tick_errors`。
        """
        import traceback

        self.clock.advance()
        try:
            self.tick()
        except Exception:
            self._record_tick_error(traceback.format_exc())

    def _record_tick_error(self, formatted):
        # type: (str) -> None
        self.tick_error_count += 1
        # 只保留前若干条完整堆栈，避免一次持续故障把内存吃光。
        if len(self.tick_errors) < self.max_tick_errors:
            self.tick_errors.append(formatted)

    def stop(self):
        # type: () -> None
        """显式销毁。要求 hot reload 与退出时不残留监听器与待办。"""
        if not self.started:
            return
        self.event_binder.unbind()
        self.queue.clear()
        # 停机时把未处理的唤醒信号也丢掉：它们是「这一帧该醒醒了」，
        # 留着会在下次启动的第一帧集中投递一批早已过期的信号。
        self.event_binder.clear_wakes()
        # 驱动器与介质只标为未加载：**不写仓库、不清内容**。介质的内容是
        # 玩家资产，停机不是「把它们丢了」的时机（写盘由任务 12 决定）。
        self.drivers.mark_all_unloaded()
        self._backfill_generator = None
        self._backfill_finished = False
        self.started = False

    # -- 推进 ----------------------------------------------------------

    def tick(self):
        # type: () -> dict
        """推进一次：先补查，再消费事件。

        补查与事件消费都在**单帧内限量**：一次世界加载可能带来上千个区块，
        不做限量会直接卡住服务端主线程。
        """
        if not self.started:
            raise RuntimeError("bootstrap 未启动")

        self.tick_count += 1
        backfilled = self._advance_backfill()
        drained, changes = self._drain_events()
        # 事件消费完再重建：同帧的多次变化只算一次重建（02-02）。
        rebuild = self.topology.advance(self.rebuild_steps_per_tick)
        # 唤醒投递必须夹在**重建之后、推进之前**：放前面会丢掉「本帧刚放下、
        # 运行时还没同步成员」的设备（它的 lane 还不存在）；放后面则本帧
        # 白等一帧。集合为空时 `take_wakes()` 是 O(1)，空闲帧几乎不花钱。
        wakes = self.event_binder.take_wakes()
        delivered = self.runtimes.deliver_wakes(wakes) if wakes else 0
        # 重建之后才推进运行时：发布 → 运行时同步（含主控绑定与服务挂载）
        # → 服务推进。顺序不能倒——先在旧图上推进服务，等于让它们按已经
        # 不成立的归属动手。
        service_steps = self.runtimes.tick(self.runtime_budget, self._tick_context())

        return {
            "tick": self.tick_count,
            "backfilled_chunks": backfilled,
            #: 本帧消费的事件条数——它才是受 `drain_limit` 约束的量
            "events_drained": drained,
            #: 事件翻译出的设备变化条数。一次放置通常不止一条变化
            #: （放下本身 + 邻接设备需要标脏），因此两者不相等。
            "changes": len(changes),
            #: 本帧展开的图节点数与重建完成的分量数，供预算观测
            "topology_steps": rebuild.steps,
            "components_rebuilt": rebuild.components_rebuilt,
            "topology_version": self.topology.version,
            #: 本帧服务推进消耗的工作量（03）。受 `runtime_budget` 约束；
            #: 调度器落地后它不再恒为 0——有到期任务时才会有消耗。
            "service_steps": service_steps,
            #: 本帧送达的唤醒信号数（04-02）。未命中不报错，见 `deliver_wakes`。
            "wakes": delivered,
            "runtimes": len(self.runtimes.runtimes()),
            "queue": self.queue.stats(),
        }

    def _tick_context(self):
        # type: () -> dict
        """下传给服务的每帧上下文。只放两个**权威读数**，不放别的。

        - `tick`：逻辑时钟的帧序。服务不得自己数帧——那会在关网、跨帧重建、
          热重载时与它漂移（调度器曾经有一个 `_local_tick` 回退计数器，
          两者混用会让时间倒流，见交付说明）。
        - `frame`：引擎帧号。给「一帧只该发生一次」的东西去重用：
          公平轮转的余量轮会让同一个运行时在一帧里被访问两次，主控时钟
          与 `stats["ticks"]` 靠它按帧而不是按调用次数走。
        """
        return {
            "tick": self.clock.current_tick(),
            "frame": self.tick_count,
        }

    def _drain_events(self):
        # type: () -> tuple
        batch = self.queue.drain(limit=self.drain_limit)
        changes = []
        for event in batch:
            # `handle()` 内部已经经统一出口发布，这里不能再发一次。
            changes.extend(self.lifecycle.handle(event))
        return len(batch), changes

    def _advance_backfill(self):
        # type: () -> int
        """分帧补查已加载区块。

        **区块发现的主要路径是 `CHUNK_LOADED` 事件，不是这里。**
        世界在脚本初始化时尚未加载，之后每个区块加载都会触发事件并附带
        该区块的方块实体列表，因此不会漏。本方法只是兜底与手动重扫入口，
        跑完一次即停止，不每帧重建生成器——否则已知区块越多，
        每帧空转的代价越大。
        """
        if self._backfill_finished:
            return 0
        if self._backfill_generator is None:
            self._backfill_generator = self.lifecycle.plan_startup_backfill()
        done = 0
        while done < self.backfill_chunks_per_tick:
            try:
                next(self._backfill_generator)
            except StopIteration:
                self._backfill_generator = None
                self._backfill_finished = True
                break
            done += 1
        return done

    def reset_backfill(self):
        # type: () -> None
        """重新扫描全部已知区块。用于世界重载或手动重扫。

        两处状态都要清：本对象的分帧游标，以及生命周期里「该区块已补查过」
        的标记。只清前者不会真的重扫——后者会让生成器把每个区块都跳过，
        看起来像成功了其实什么都没做。
        重复扫描是安全的：注册表幂等，不会重复建实例。
        """
        self._backfill_generator = None
        self._backfill_finished = False
        self.lifecycle.forget_backfill()

    def _note_driver_load_changed(self, node_key):
        # type: (object) -> int
        """插拔介质后立刻重算该设备所在负载域的名额（05 要求）。

        **必须早于后续交易**：否则刚插上的一块盘还按旧准入放行，
        而旧准入不知道这台设备的需求变了。
        """
        return self.admission.note_device_load_changed(node_key)

    def _emit(self, changes):
        # type: (list) -> None
        for change in changes:
            self._change_counts[change.kind] = self._change_counts.get(change.kind, 0) + 1
            # 拓扑是设备变化的固定消费者：它只标脏，真正的重建在 `tick()` 里
            # 有限步完成，因此这里不会拖慢事件消费。
            self.topology.on_lifecycle_change(change)
            # 驱动器状态也从**同一批变化**推导，不另开观察者通道——
            # 两条路径分叉就会出「拓扑知道设备没了、驱动器还以为在」。
            self.drivers.observe(change)
        if self.change_sink is not None:
            for change in changes:
                self.change_sink(change)
        elif self._recent_changes is not None:
            # 没有额外观察者时只保留最近一批，避免无限堆积。
            self._recent_changes = changes

    # -- 测试与诊断 ----------------------------------------------------

    def push(self, event):
        # type: (object) -> bool
        return self.queue.push(event)

    def recent_changes(self):
        # type: () -> list
        """最近一批设备变化。仅在未注入 change_sink 时可用。"""
        if self._recent_changes is None:
            raise RuntimeError("已注入 change_sink，变化不再在此保留")
        return list(self._recent_changes)

    def force_backfill(self, chunk_keys):
        # type: (list) -> int
        """立即补查指定区块（测试用，不走分帧）。"""
        return sum(1 for _ in self.lifecycle.plan_startup_backfill(chunk_keys=chunk_keys))

    def diagnostics(self):
        # type: () -> dict
        return {
            "started": self.started,
            "ticks": self.tick_count,
            "tick_errors": {
                "count": self.tick_error_count,
                "samples": list(self.tick_errors),
            },
            "definitions": {
                "devices": len(self.definitions.devices),
                "mediums": len(self.definitions.mediums),
                "controllers": sorted(self.definitions.controller_ids),
            },
            "registry": self.registry.stats(),
            "lifecycle": self.lifecycle.diagnostics(),
            "events": self.event_binder.diagnostics(),
            "queue": self.queue.stats(),
            "changes": dict(self._change_counts),
            "topology": self.topology.diagnostics(),
            "admission": self.admission.diagnostics(),
            "assembly": self.assembly_diagnostics(),
            "runtimes": self.runtimes.diagnostics(),
            "application": self.application.diagnostics(),
            "scheduler": self.scheduler_diagnostics(),
            "storage": self.storage_diagnostics(),
            "world": self.world.diagnostics(),
            "scope_note": (
                "已装配任务 01 的设备层、任务 02 的拓扑/负载层、任务 03 的主控/运行时、"
                "任务 04 的调度核心（逻辑时钟、到期队列、跨网轮转与唤醒），"
                "以及任务 05 的存储领域层（介质、驱动器挂卸、汇总索引、预留、受控读写）；"
                "装配档位 %s。存储的**真实物品交接、存档后端与界面同步尚未接入**"
                "（任务 06/12/11），能源与工厂仍是替身（任务 04 的另一半/09），"
                "因此**网络业务命令仍会被拒绝并给出 service_absent**——"
                "拦住它们的是能源那一项网络级准入，不是存储没写。"
                "这是尚未实现的诚实结果，不是故障。" % (self.assembly_profile,)
            ),
        }

    def storage_diagnostics(self):
        # type: () -> dict
        """存储领域层的诊断（05）。

        分两块：驱动器与介质（启动层持有）与未定规则。两者都放在这里，
        是因为 `debug_query("assembly")` 与状态栏需要的正是这两个问题的答案：
        「设备上现在是什么状况」与「哪些规则还没定案」。
        """
        report = self.drivers.diagnostics()
        report["unresolved"] = (
            self.driver_policy.unresolved_report() + self.medium_policy.unresolved_report()
        )
        report["scope_note"] = (
            "介质挂载、汇总索引、预留与受控读写已实现并通过离线断言；"
            "真实玩家装卸（06）、存档后端（12）与界面同步（11）未接入，"
            "因此这些能力目前只有领域层的证据，没有游戏内的证据。"
        )
        return report

    def scheduler_diagnostics(self):
        # type: () -> dict
        """调度诊断（04-01/04-07）：时钟读数、脉冲计数、跨网轮转、唤醒与各网合计。

        `clock` 只有帧序（`tick`/`advances`）；**三种引擎脉冲的计数在
        `pulses` 里**，它们来自事件适配器那唯一一份 `counts`，
        供真机实测算比值——**代码里不出现任何频率换算**：
        `OnScriptTickServer`（实测约 30 Hz）与 `OnSimTickServerEvent`
        （实测约 17.3 Hz）是两个不同的时钟。
        """
        return {
            "clock": self.clock.describe(),
            "pulses": self.event_binder.pulse_counts(),
            "rotation": self.runtimes.rotation_view(),
            "wakes": self.event_binder.wakes.stats(),
            "totals": self.runtimes.scheduler_view(),
            "scope_note": (
                "调度核心已落地；能源服务仍是替身，因此设备状态里的能源判定"
                "一律是「尚未实现」而不是「没电」。"
            ),
        }

    def assembly_diagnostics(self):
        # type: () -> dict
        """装配档位、替身清单与未定规则报告。

        未定规则单独列出来，而不是折进「警告」里：R02/R03/R13 这些规则
        一旦定案，下游（尤其 05 的容量与 09 的槽位）要按新值重算，
        因此它们需要能被一条命令查出来。
        """
        pending = self.service_factories.pending_tasks()
        return {
            "profile": self.assembly_profile,
            "slots": list(self.service_factories.ids()),
            "stand_ins": [
                "替身服务 %s（待任务 %s 实现）" % (service_id, pending[service_id])
                for service_id in sorted(pending)
            ],
            # 三份策略一起报：只报带宽那一份会让 R07/R08/R10 在正式验收时漏网。
            "unresolved": self.policy_bundle().unresolved_report(),
            "storage_unresolved": (
                self.driver_policy.unresolved_report()
                + self.medium_policy.unresolved_report()
            ),
        }

    def policy_bundle(self):
        # type: () -> _PolicyBundle
        """全部未定案策略的合成视图（供诊断与正式验收一起检查）。"""
        return _PolicyBundle(
            [self.admission.policy, self.driver_policy, self.medium_policy]
        )

    def assert_assembly_ok(self):
        # type: () -> list
        """正式验收用的装配期检查。**不会被自动调用**（与 03 同一条理由：
        抛错会让正式档整个起不来，而运行期拦截给出的「待确认」更有用）。

        把三份策略一起交出去，否则 `PRODUCTION` 档会放行带未定案数值的存储，
        而「漏报」正是全局约束 7 最怕的形态。

        替身清单取自**实际装配**：有网络时用它的注册表，还没有网络时退回到
        一份裸注册表（它构造的就是默认装配表，与工厂的默认部分同源）。
        """
        policy = self.policy_bundle()
        registries = [runtime.services for runtime in self.runtimes.runtimes()]
        if not registries:
            registries = [ServiceRegistry()]
        problems = []
        for registry in registries:
            for item in registry.assert_profile_ok(self.assembly_profile, policy):
                if item not in problems:
                    problems.append(item)
        return problems

    def submit_command(self, command):
        # type: (object) -> object
        """业务命令入口。**永不抛异常**——这是引擎事件路径要调的那一个。

        领域层按纪律不吞异常（缺陷要在自己的层次炸出来），但这一层接着的
        是每帧数十次的引擎回调，抛回去会刷屏并让后续帧全废。因此异常在
        `ApplicationService.submit` 里被转成 `rejected INTERNAL`。
        """
        return self.application.submit(command)


#: 进程内的服务端单例
_SERVER = None


def get_server():
    # type: () -> ServerBootstrap
    return _SERVER


def start_server():
    # type: () -> ServerBootstrap
    """由 `modMain.OnServerInited` 调用。

    **显式使用 DEV 档，并把理由写在这里。** 04/05/09 尚未落地，网络服务
    全是替身；换成 `PRODUCTION` 档不会让它们开始工作，而是让每一条业务
    命令都被拒（`assembly_unresolved` + `service_absent`），并且把
    诊断里的「哪些数值还没定案」当成正常结论报出来。游戏内现在只能用
    DEV 档，而 DEV 档的代价同样是**业务命令一律被拒**——两种档位都不假装
    能用，区别只在说法。这一点在 `diagnostics()` 的 `scope_note` 里也写着。

    写成显式参数而不是依赖默认值：默认值将来若被改成别的档位，
    这里不会跟着悄悄改变游戏内的行为。
    """
    global _SERVER
    if _SERVER is not None:
        _SERVER.stop()
    _SERVER = ServerBootstrap(assembly_profile=AssemblyProfile.DEV)
    _SERVER.start()
    return _SERVER


def stop_server():
    # type: () -> None
    """显式销毁。热重载或退出时调用。"""
    global _SERVER
    if _SERVER is not None:
        _SERVER.stop()
        _SERVER = None
