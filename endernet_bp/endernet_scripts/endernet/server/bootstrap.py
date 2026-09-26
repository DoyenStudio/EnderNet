# coding=utf-8
"""服务端装配与启动/销毁。

对应任务 01-01、01-06 的入口与分帧补查部分，03 起接入主控与网络运行时。

**本批次的范围。** 装配 01 的设备层、02 的拓扑/负载层、03 的主控与运行时、
04 的调度核心与能源服务、05 的存储领域层、06 的转移与接口层、07 的样板层、
08 的合成规划与 09 的订单与执行。**四个网络服务槽位都是正式实现**，
替身已经撤干净了。默认装配档仍是 `AssemblyProfile.DEV`：它本来是为
替身服务留的，现在留着是因为**能源的替身口径**——库默认取保守的那一侧，
而 DEV 档允许显式换成能跑得起来的那一侧。

**「不是替身」不等于「能玩」。** 09 的产物回流入口还是任务 10 的具名替身
（`AbsentReturns`），装配仓执行是 07-06，两者都**明确拒绝**而不是假装成功：
订单会一路推进到「等待产物」然后如实阻塞并给出 `order_returns_absent`。

**「已实现」不等于「已定案」。** 能源是正式实现，但它的两条策略字段仍是
未定规则的替身口径，而库默认取的是保守的那一侧；游戏内由 `start_server()`
显式选用替身口径，理由写在那个函数里。

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
from .adapters.containers_engine import (
    EngineContainerProvider,
    EngineInventoryProvider,
    EngineItemPrimitives,
    build_stack_limit,
)
from .adapters.events import ServerEventBinder
from .adapters.world import ToolDeltaWorldAdapter
from .application import ApplicationService
from .devices.catalog import DeviceCatalog
from .crafting.capacity import OrderPolicy
from .crafting.executor import AbsentReturns, FactoryService
from .crafting.patterns import PatternPolicy
from .crafting.planner import PlanPolicy, Planner
from .crafting.store import PatternStore
from .devices.io import (
    TEMPLATE as PORT_TEMPLATE_ROLE,
    PortGateway,
    PortPolicy,
    PortStore,
    port_blocker,
)
from .devices.registry import DeviceRegistry
from .devices.storage_driver import DriverPolicy, StorageDriverStore
from .network.admission import AdmissionService
from .network.energy import EnergyPolicy, EnergyService
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
from .storage.transfer import TransferPolicy


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


def _energy_builder(policy):
    # type: (object) -> object
    """把能源策略绑进能源服务的构造器（供 `ServiceFactory` 使用）。

    与 `_scheduler_builder` 同形，走显式工厂而不是闭包 lambda：这里绑的是
    **全进程唯一**的那份策略对象，而 `unresolved_report()` 报出来的必须就是
    真正生效的那份数值——各存一份的代价是诊断与实际行为对不上。
    """
    def build(runtime):
        # type: (object) -> EnergyService
        return EnergyService(runtime, policy=policy)

    return build


def _storage_builder(drivers, medium_policy, ports, transfer_policy, inventories):
    # type: (object, object, object, object, object) -> object
    """把**驱动器状态集合**与**接口状态集合**绑进存储服务的构造器。

    服务槽位是逐运行时各造一份的，而驱动器、介质与接口是**跨网络共享**的
    （架构 §3：这些状态不能依赖某个网络实例存活）。因此注入的不是
    「每个网络一份的状态」，而是同一个集合——两个网络同时挂同一块盘时
    由服务侧的重复挂载检测拦住。

    转移策略与背包提供者也是全局各一份：策略里的未定案数值必须与实际
    生效的是同一份（否则 `unresolved_report()` 报的数字与真正用的对不上），
    而背包是玩家级资源，与网络归属无关。
    """
    def build(runtime):
        # type: (object) -> StorageService
        return StorageService(
            runtime,
            drivers=drivers,
            policy=medium_policy,
            ports=ports,
            transfer_policy=transfer_policy,
            inventories=inventories,
        )

    return build


def _factory_builder(policy, planner, patterns, ports, returns=None):
    # type: (object, object, object, object, object) -> object
    """把规划器、样板索引与接口状态集合绑进工厂服务的构造器。

    这三样都是**全进程一份**的（它们本来就跨网络：样板装在方块里、
    规划器没有节点、接口缓冲不能因为网络断开而消失），而服务槽位是逐运行时
    各造一份的，因此注入的是同一批对象。**订单状态不在这里**——它在
    `FactoryService.attach()` 里按 `component_id` 新建，两张网络绝不共享。

    网关（`PortGateway`）**逐运行时新建**：它要绑的就是那一个运行时。
    设备发起的命令因此走的还是主控那条路（准入 + 去重账本），
    与 06 的接口轮询同一条。

    `port_blocker` 与 `TEMPLATE` 也从这里注入：`crafting/` 是领域层，
    不该 import 设备模块；而装配层两侧都看得见，正是接这根线的地方。
    """
    def build(runtime):
        # type: (object) -> FactoryService
        return FactoryService(
            runtime,
            policy=policy,
            planner=planner,
            patterns=patterns,
            ports=ports,
            gateway=PortGateway(runtime),
            returns=returns if returns is not None else AbsentReturns(),
            port_blocker=port_blocker,
            template_role=PORT_TEMPLATE_ROLE,
        )

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
        energy_policy=None,
        port_policy=None,
        transfer_policy=None,
        pattern_policy=None,
        plan_policy=None,
        order_policy=None,
        returns_sink=None,
        container_primitives=None,
        container_provider=None,
        inventory_provider=None,
        player_dimension=None,
    ):
        # type: (DefinitionSet, object, int, int, int, object, int, object, str, object, object, object, int, object, object, object, object, object, object) -> None
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
        #: 接口策略（R05/R04）与转移策略（R04/R05/R12）。同样**全进程各一份**：
        #: `unresolved_report()` 报出来的必须就是真正生效的那份数值。
        #: R05 是 06、09、12 共同的上游（输出缓冲容量、取消与拆除处置），
        #: 因此它们进 `policy_bundle()`，正式验收时不会漏网。
        self.port_policy = port_policy if port_policy is not None else PortPolicy()
        self.transfer_policy = (
            transfer_policy if transfer_policy is not None else TransferPolicy()
        )
        #: 样板策略（R09）。同样**全进程一份**：`unresolved_report()` 报出来的
        #: 必须就是真正生效的那份数值。
        self.pattern_policy = (
            pattern_policy if pattern_policy is not None else PatternPolicy()
        )
        #: 规划策略（R11/R04/08-04）。同样**全进程一份**。
        #: **它与样板策略是两份**：样板策略回答「这份配方里哪些输入要备料」
        #: （R09），规划策略回答「要几个批次、缺什么、按哪个口径算目标量」
        #: （R11/R04）。规划器同时持有两者，但它们的未定项各自报各自的，
        #: 合在一起会让「哪条规则在拦我」查不出来。
        self.plan_policy = (
            plan_policy if plan_policy is not None else PlanPolicy()
        )
        #: 订单策略（R04/R09 的执行侧旋钮）。同样**全进程一份**：
        #: `unresolved_report()` 报出来的必须就是真正生效的那份数值。
        #: 它与规划策略是两份——规划回答「要几个批次、缺什么」，
        #: 订单回答「一个订单占几个槽位、几个订单一块推进、返还物算不算完成」。
        self.order_policy = (
            order_policy if order_policy is not None else OrderPolicy()
        )
        #: 产物回流的接收侧（任务 10）。**默认是明确报「没接上」的替身**：
        #: 订单因此会停在等待产物并给出 `ORDER_RETURNS_ABSENT`，而不是
        #: 假装收到。离线测试注入具名假回流来驱动状态机（09 文档允许）；
        #: 生产装配**不接受**一个什么都不做的假实现。
        #: 这里把替身**当成默认值真的造出来**，而不是留一个 `None`：
        #: `None` 在诊断里与「接了但对象是空的」看起来一样。
        self.returns_sink = (
            returns_sink if returns_sink is not None else AbsentReturns()
        )
        #: 容器与背包的引擎原语。**构造是安全的、调用必须在游戏里**——
        #: `EngineItemPrimitives` 把引擎 import 放在方法体内，因此离线也能
        #: 构造整条链路，只是真的去读一个箱子时会失败（那正是它该失败的时候）。
        primitives = (
            container_primitives
            if container_primitives is not None
            else EngineItemPrimitives()
        )
        stack_limit = build_stack_limit(primitives)
        #: `位置 -> 容器` 与 `玩家 -> 背包` 两个提供者。注入替身即可离线驱动
        #: 存货口/取货口的完整路径，不必进游戏。
        self.containers = (
            container_provider
            if container_provider is not None
            else EngineContainerProvider(primitives, stack_limit)
        )
        self.inventories = (
            inventory_provider
            if inventory_provider is not None
            else EngineInventoryProvider(primitives, stack_limit)
        )
        #: 能源策略（04 的供需判定口径）。同样**全进程一份**。
        #: **库默认是保守的那一侧**：任何算不出的需求都让判定停在
        #: 「待确认」，因此默认装配下真实网络永远是 `ENERGY_UNAVAILABLE`。
        #: 游戏内 DEV 档要的是「能看出没电、能跑起来」，由 `start_server()`
        #: 显式改用 `EnergyPolicy.dev_stand_in()`——替身口径只出现在那一个
        #: 写了理由的地方，而不是默认值里。
        self.energy_policy = (
            energy_policy if energy_policy is not None else EnergyPolicy()
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
        #: 接口状态集合（06）。与 `drivers` 同一地位：接口缓冲里的东西不能
        #: 因为网络断开、主控被移除或运行时重建而消失（06 验收明写
        #: 「外部物流已送入缓冲的资源仍保留」）。
        #: 它同样在 `admission` 之后构造，但**不**通知负载重算——接口的带宽
        #: 与能耗是定义表里的固定值，不随缓冲内容变化。
        self.ports = PortStore(
            self.definitions,
            self.registry,
            policy=self.port_policy,
            containers=self.containers,
        )
        #: 样板提供者状态与产物索引（07）。与 `drivers` / `ports` 同一地位：
        #: 样板装在方块容器里，它不能因为网络断开、主控被移除或运行时重建
        #: 而消失。**索引是派生的**——它每次从容器重读，因此不存档、
        #: 也不需要迁移（12 的「派生样板索引在恢复后重建」）。
        self.patterns = PatternStore(
            self.definitions,
            self.registry,
            policy=self.pattern_policy,
            containers=self.containers,
        )
        #: 合成规划（08）。**它不是一个网络服务槽位**：规划是只读计算，
        #: 没有节点、没有生命周期、也不需要主控签发的许可。真正的调用方是
        #: 任务的 09（订单与执行），它会拿到同一个对象；在那之前，
        #: `debug_query("plan")` 是它唯一的调用路径——**只读**，不写任何状态。
        self.planner = Planner(
            self.definitions,
            policy=self.plan_policy,
            pattern_policy=self.pattern_policy,
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
                        self.drivers,
                        self.medium_policy,
                        self.ports,
                        self.transfer_policy,
                        self.inventories,
                    ),
                    ServiceId.ENERGY: _energy_builder(self.energy_policy),
                    ServiceId.FACTORY: _factory_builder(
                        self.order_policy,
                        self.planner,
                        self.patterns,
                        self.ports,
                        self.returns_sink,
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
            # 接口状态与样板状态只用于 `debug_query("port"/"pattern")`
            # 这两条只读诊断路径（全局约束 9：设备离线时仍可查看诊断）。
            # 业务命令照样只能走主控——门面这一侧没有任何写入入口。
            ports=self.ports,
            patterns=self.patterns,
            # 规划只读入口（08）。**它连写入的能力都没有**：规划器没有改库存
            # 的接口，因此「调试入口变成旁路」这件事在这里是结构性的不可能。
            planner=self.planner,
        )
        #: 引擎事件 API。注入替身即可在无引擎环境跑通真实的 `bind()` 路径，
        #: 从而验证注册与注销，而不用进游戏试。
        #:
        #: `patterns` 作为容器变更的接收方传进去（鸭子类型，见适配器的说明）。
        #: **样板状态必须早于这里构造**：绑定期间就会注册容器监听器，
        #: 而它们一触发就要找得到接收方。
        self.event_binder = ServerEventBinder(
            self.queue,
            self.world,
            self.registry,
            self.definitions,
            clock=self.clock,
            event_api=event_api,
            patterns=self.patterns,
            # 玩家维度解析同样可注入：自定义容器的玩家事件不带维度，
            # 而插槽拒收挂在那条路径上——不注入的话它只能进游戏试。
            player_dimension=player_dimension,
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
        # 接口同理：缓冲里的东西是**外部物流送进来的**，停机更不能丢。
        self.drivers.mark_all_unloaded()
        self.ports.mark_all_unloaded()
        self.patterns.mark_all_unloaded()
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
        # 样板槽的重读放在最后：它读的是方块容器，与网络服务不相干。
        # **注意容器事件不走这里的队列**：它们是引擎监听器，由适配器在
        # 引擎派发时直接 `mark_dirty`，因此到达时刻与这一帧的推进无关。
        # `advance` 只负责「够了静默期就重读」，放在帧尾是为了让它成为
        # 本帧最后一个动作——语义上「本帧的账都算完了，再去看看容器」。
        pattern_reads = self.patterns.advance(self.clock.current_tick())

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
            #: 本帧重读了几台样板提供者（07）。恒为 0 是正常的——
            #: 只有收到容器变更且过了静默期才会读。
            "pattern_reads": pattern_reads,
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
            # 驱动器状态与接口状态也从**同一批变化**推导，不另开观察者通道——
            # 两条路径分叉就会出「拓扑知道设备没了、驱动器/接口还以为在」。
            self.drivers.observe(change)
            self.ports.observe(change)
            # 样板提供者状态也从**同一批变化**推导（07）。它按能力判定
            # 谁是提供者，因此非提供者的变化在这里是空操作。
            self.patterns.observe(change)
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
            "io": self.io_diagnostics(),
            "pattern": self.pattern_diagnostics(),
            "plan": self.plan_diagnostics(),
            "order": self.order_diagnostics(),
            "energy": self.energy_diagnostics(),
            "world": self.world.diagnostics(),
            "scope_note": (
                "已装配任务 01 的设备层、任务 02 的拓扑/负载层、任务 03 的主控/运行时、"
                "任务 04 的调度核心（逻辑时钟、到期队列、跨网轮转与唤醒）与能源服务"
                "（固定耗能结算、供能判定）、"
                "任务 05 的存储领域层（介质、驱动器挂卸、汇总索引、预留、受控读写），"
                "任务 06 的转移与接口层（端点预检与受控提交、有界中转缓冲、"
                "四类接口设备、真实的容器/背包引擎适配），"
                "任务 07 的样板层（配方结构与编解码、9 格读写、"
                "产物到提供者的索引、样板槽拒收），"
                "以及任务 08 的合成规划（批次与余量、库存优先、多级依赖与共享"
                "子需求、环检测与有界失败、可分帧推进与提交前复核凭据）、"
                "任务 09 的订单与执行（状态机、提交时复核与独占预留、"
                "槽位与内存租约、分帧备料与派发、暂停取消与重验）；"
                "装配档位 %s。**接口接触真实引擎的那一段（箱子、背包、其他模组的"
                "物流）没有在游戏内实测过**，流体路径则整条未接入（13-03）。"
                "**四个服务槽位都已是正式实现**（工厂的替身已随任务 09 撤掉），"
                "但「不是替身」不等于「能玩」："
                "产物回流入口还是任务 10 的具名替身（`AbsentReturns`），"
                "因此订单会一路推进到「等待产物」然后**如实阻塞**并给出"
                "order_returns_absent——那不是缺料、也不是机器坏了，"
                "是那一半还没写；装配仓执行是 07-06，同样明确拒绝。"
                "存档后端与界面同步尚未接入（任务 12/11）。"
                "预设配方适配（07-03）与样板编辑界面（11）都未开始，"
                "因此样板目前只能看不能用——`debug_query(\"pattern\")`"
                "查得到 9 格里有什么。"
                "因此网络能不能开工，取决于当地那台网络的能源判定与带宽判定："
                "判定为「待确认」时业务命令被拒并给出"
                "energy_unavailable / bandwidth_unknown——**那是「算不出来」，"
                "不是「没电」**，详情见 energy 那一节。"
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
            "存档后端（12）与界面同步（11）未接入，因此这些能力目前只有"
            "领域层的证据，没有游戏内的证据。"
            "**06 的转移协调器已经可用，但介质装卸还没有改走它**："
            "`mount` 仍按 05 的路径（`create_medium(model_id)` + `provider.mount`），"
            "因为「介质物品如何携带身份」是 12-02 尚未做出的决定——"
            "在没有那个决定之前把装卸接到转移层，等于悄悄定案。"
            "接口侧的状态见 diagnostics()[\"io\"]。"
        )
        return report

    def io_diagnostics(self):
        # type: () -> dict
        """接口层诊断（06）。

        分三块：接口状态与其策略（启动层持有）、容器与背包提供者，
        以及各网络接口驱动的合计（逐接口明细在 `debug_query("port")` 里，
        这里不再抄一份——抄一份的代价是两处迟早对不上）。
        """
        report = self.ports.diagnostics()
        report["containers"] = self.containers.diagnostics()
        report["inventories"] = self.inventories.diagnostics()
        report["transfer_policy"] = self.transfer_policy.describe()
        report["transfer_unresolved"] = self.transfer_policy.unresolved_report()
        # **不伸手读服务私有字段。** 与 `debug_query` 那条只读入口同一条纪律：
        # 戳一个实现的内部名字，会让任何自定义存储服务（离线替身、将来把
        # 接口驱动拆出去的服务）把整块诊断打崩——而诊断正是「网络坏掉时
        # 最该能答」的那条路径（全局约束 9）。
        # 因此走公开访问器，并把「没建起来」与「没有接口」分开报。
        runners = []
        for runtime in self.runtimes.runtimes():
            service = runtime.services.get(ServiceId.STORAGE)
            if service is None:
                continue
            accessor = getattr(service, "ports_runner_view", None)
            if accessor is None:
                continue
            view = accessor()
            view["component_id"] = runtime.component_id
            runners.append(view)
        report["runners"] = runners
        report["scope_note"] = (
            "端点的预检与受控提交、有界中转缓冲、四类接口设备与待核对语义"
            "已实现并通过离线断言；**接触真实引擎的那一段（箱子读写、玩家背包、"
            "外部模组物流）没有游戏内实测**，因此它目前只有领域层的证据。"
            "流体接口整条未接入（13-03），会明确回 no_adapter。"
            "样板口的成批投料入口已备好（`port_stage`），但**没有调用方**"
            "——工厂（09）尚未实现。"
        )
        return report

    def pattern_diagnostics(self):
        # type: () -> dict
        """样板层诊断（07）。

        逐台明细在 `debug_query("pattern")` 里，这里只给合计与策略——
        抄一份逐台明细的代价是两处迟早对不上。
        """
        view = self.patterns.diagnostics()
        view["container_events"] = {
            key: self.event_binder.counts.get(key, 0)
            for key in (
                "container_changed",
                "custom_container_put",
                "custom_container_remove",
                "custom_container_push_in",
                "custom_container_pull_out",
                "pattern_slot_rejected",
            )
        }
        return view

    def plan_diagnostics(self):
        # type: () -> dict
        """规划层诊断（08）。

        只有策略与合计：**逐次计划的内容属于订单**（09），不该在启动层留一份
        缓存——那会变成第二份真相。想看一次真实的规划走
        `debug_query("plan", target=..., amount=...)`。
        """
        report = self.planner.diagnostics()
        report["pattern_policy"] = self.pattern_policy.describe()
        return report

    def order_diagnostics(self):
        # type: () -> dict
        """订单层诊断（09）。

        **只给策略、替身口径与各网络的合计**：逐张订单的内容属于那台网络的
        工厂服务（订单是每网络一份的状态），不该在启动层再存一份。
        想看某张订单走 `debug_query("order", component_id=..., order_id=...)`。

        这里最要紧的一条是 `returns_sink`：它把「回流入口是替身」这件事
        摆在启动层的诊断里，而不是只藏在某个网络的服务里——否则
        「订单为什么不动」在最外层看起来像什么都没发生。
        """
        return {
            "policy": self.order_policy.describe(),
            "unresolved": self.order_policy.unresolved_report(),
            "returns_sink": self.returns_sink.describe(),
            "runtimes": self.runtimes.order_view(),
        }

    def energy_diagnostics(self):
        # type: () -> dict
        """能源诊断（04-04）。

        分两块：各网络的判定（运行时侧）与策略（启动层持有）。判定的**逐设备
        明细**在 `diagnostics()["runtimes"][分量]["services"]` 里，这里不再抄
        一份——抄一份的代价是两处迟早对不上。
        """
        view = self.runtimes.energy_view()
        view["policy"] = self.energy_policy.describe()
        view["unresolved"] = self.energy_policy.unresolved_report()
        view["scope_note"] = (
            "固定耗能结算与供能判定已实现；RF 计量（04-05/13-02）、断电暂停"
            "（04-06）与设备级能源状态通知（04-08）未实现。装置数量与工作态"
            "还没有来源、基带等设备的耗能在 blocks.md 里未列（R10），因此库默认"
            "口径下真实网络一律报「待确认」；DEV 档用替身口径把算不出的需求"
            "忽略掉（见 ignored_demand），那是「能跑起来」而不是「已定案」。"
        )
        return view

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
                "调度核心已落地。能源判定不在本节，看 diagnostics()[\"energy\"]："
                "能源已是正式实现，因此设备状态里的能源判定是**真结论**"
                "（有电/没电/待确认），不再是「服务尚未实现」。"
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
            # 全部策略一起报：只报带宽那一份会让 R04/R05/R07/R08/R09/R10/R11
            # 在正式验收时漏网。
            "unresolved": self.policy_bundle().unresolved_report(),
            "storage_unresolved": (
                self.driver_policy.unresolved_report()
                + self.medium_policy.unresolved_report()
            ),
        }

    def policy_bundle(self):
        # type: () -> _PolicyBundle
        """全部未定案策略的合成视图（供诊断与正式验收一起检查）。

        **`scheduler_policy` 刻意不在里面**：`ServiceRegistry.assert_profile_ok`
        的签名只接一个策略对象，而调度策略的未定项是 04 交付说明明确交接给
        任务 14 的一件事（要么额外传，要么把签名扩成 `policies=(...)`）。
        本增量没有偷改那个签名，也没有把调度策略混进来冒充「已经检查过」。

        **06 的两份策略在里面**：`port_policy` 与 `transfer_policy` 带的是
        R05（输出缓冲容量、取消与拆除处置）与 R12（光标持有物）。
        少放一份的代价是正式验收时那两条规则漏网，而它们正是全局约束 7
        最怕的形态。
        """
        return _PolicyBundle(
            [
                self.admission.policy,
                self.driver_policy,
                self.medium_policy,
                self.energy_policy,
                self.port_policy,
                self.transfer_policy,
                # 09 的订单策略带的是 R04（槽位/内存计费）与 R09 的执行侧
                # （返还物算不算完成）。**它与样板策略是两份**：样板策略回答
                # 「这份配方里哪些输入要备料」，订单策略回答「一个订单占几个
                # 槽位、计多少内存、返还物到齐才算完成」。
                self.order_policy,
                # 07 的样板策略带的是 R09（无限燃料、返还物规则、两类样板
                # 适用范围）。少放一份的代价是正式验收时那三条漏网，
                # 而它们正是全局约束 7 最怕的形态。
                self.pattern_policy,
                # 08 的规划策略带的是 R11（目标量是生产量还是保有量）、
                # R04（内存估算口径）与 08-04（配方优先级、替代配方范围、
                # 四个搜索上限）。**它与样板策略是两份，都要在**：
                # 前者说的是「这份配方要备哪些料」，后者说的是「要几个批次」。
                self.plan_policy,
            ]
        )

    def assert_assembly_ok(self):
        # type: () -> list
        """正式验收用的装配期检查。**不会被自动调用**（与 03 同一条理由：
        抛错会让正式档整个起不来，而运行期拦截给出的「待确认」更有用）。

        把全部策略一起交出去，否则 `PRODUCTION` 档会放行带未定案数值的存储
        或能源，而「漏报」正是全局约束 7 最怕的形态。**替身服务**这一项同理：
        能源虽然是正式实现，但它仍是替身口径（`dev_stand_in()`）时会被策略
        那一路拦下，而不是靠「不是替身所以放行」。

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

    **两件事都在这里显式写出来，理由也写在这里。**

    1. **DEV 档。** 工厂（09）还是替身，PRODUCTION 档不会让它开始工作，
       只会让每一条业务命令都被拒（`assembly_unresolved` + `service_absent`），
       并且把诊断里的「哪些数值还没定案」当成正常结论报出来。游戏内现在
       只能用 DEV 档，而 DEV 档的代价是**涉及工厂的命令被拒**——两种档位都
       不假装能用，区别只在说法。这一点 `diagnostics()` 的 `scope_note` 里也写着。

    2. **能源取替身口径（`EnergyPolicy.dev_stand_in()`）。** 库默认口径下，
       基带的耗能（blocks.md 未列，R10）与 `per_unit` 的装置数量都算不出来，
       于是**任何**真实网络的能源判定都会停在「待确认」——放了创造能量源也
       一样。游戏内要的是「放上电源就能开工、拆掉就显示没电」，因此这里显式
       选择替身口径：有供能设备就算有电，一台都没有就报没电。

       **这不是「悄悄定案」。** 被忽略的需求逐个列在
       `diagnostics()["energy"]["ignored_demand"]` 里，三个策略字段照旧进
       `unresolved_report()`，`assert_assembly_ok()` 与 PRODUCTION 档照旧拒绝。
       等 R10 补齐、05/08/09 把装置数量与工作态接过来之后，这里改回默认口径
       即可——那时判定会自己变准，不需要动能源服务的代码。

    写成显式参数而不是依赖默认值：默认值将来若被改动，这里不会跟着悄悄
    改变游戏内的行为。
    """
    global _SERVER
    if _SERVER is not None:
        _SERVER.stop()
    _SERVER = ServerBootstrap(
        assembly_profile=AssemblyProfile.DEV,
        energy_policy=EnergyPolicy.dev_stand_in(),
    )
    _SERVER.start()
    return _SERVER


def stop_server():
    # type: () -> None
    """显式销毁。热重载或退出时调用。"""
    global _SERVER
    if _SERVER is not None:
        _SERVER.stop()
        _SERVER = None
