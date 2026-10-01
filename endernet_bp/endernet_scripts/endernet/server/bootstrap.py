# coding=utf-8
"""服务端装配与启动/销毁。

对应任务 01-01、01-06 的入口与分帧补查部分，03 起接入主控与网络运行时。

**本批次的范围。** 装配 01 的设备层、02 的拓扑/负载层、03 的主控与运行时、
04 的调度核心与能源服务、05 的存储领域层、06 的转移与接口层、07 的样板层、
08 的合成规划与 09 的订单与执行。**四个网络服务槽位都是正式实现**，
替身已经撤干净了。默认装配档仍是 `AssemblyProfile.DEV`：它本来是为
替身服务留的，现在留着是因为**能源的替身口径**——库默认取保守的那一侧，
而 DEV 档允许显式换成能跑得起来的那一侧。

**「不是替身」不等于「能玩」。** 10 的回流分配已落地（`crafting/returns.py`），
装配仓 07-06 已经经 06 将 9 槽产物回网，再调用 `ReturnsPort.accept_assembled`。
库默认装配**不给** `returns_sink`，理由是 09 的
`returns_absent` 计数与一整套用例都钉在「库默认不假装能用」上；
游戏内由 `start_server()` 显式装配 `ReturnsService`，那时订单才真的能走完。

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

from ..common.crafting_recipe import SLOT_COUNT
from ..common.definitions import CATEGORY_PROCESSOR, CATEGORY_ASSEMBLY, DefinitionSet
from .adapters.containers_engine import (
    EngineContainerProvider,
    EngineInventoryProvider,
    EngineItemPrimitives,
    build_stack_limit,
)
from .adapters.events import ServerEventBinder
from .adapters.messages import AdapterSyncPort
from .adapters.recipes import EngineRecipeSource
from .adapters.world import ToolDeltaWorldAdapter
from .application import ApplicationService
from .devices.catalog import DeviceCatalog
from .crafting.capacity import OrderPolicy
from .crafting.executor import AbsentReturns, FactoryService
from .crafting.patterns import PatternPolicy
from .crafting.planner import PlanPolicy, Planner
from .crafting.returns import ReturnPolicy, ReturnsService
from .crafting.store import PatternStore
from .crafting.workbench import CraftingPolicy, WorkbenchStore, preview_of
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
from .network.energy import DEVICE_IDLE, DEVICE_WORK, EnergyPolicy, EnergyService
from .network.runtime import RuntimeChange, RuntimeManager
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
from .sync.sessions import SessionContext, SessionPolicy, SessionRegistry
from .sync.terminal import SyncPolicy, TerminalSyncHub


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


def _energy_builder(policy, inputs_of=None):
    # type: (object, object) -> object
    """把能源策略绑进能源服务的构造器（供 `ServiceFactory` 使用）。

    与 `_scheduler_builder` 同形，走显式工厂而不是闭包 lambda：这里绑的是
    **全进程唯一**的那份策略对象，而 `unresolved_report()` 报出来的必须就是
    真正生效的那份数值——各存一份的代价是诊断与实际行为对不上。
    """
    def build(runtime):
        # type: (object) -> EnergyService
        def read_inputs(ref, device_id):
            return inputs_of(runtime, ref, device_id)
        return EnergyService(runtime, policy=policy,
                             inputs_of=read_inputs if inputs_of is not None else None)

    return build


def _storage_builder(drivers, medium_policy, ports, transfer_policy, inventories,
                     returns=None, workbench=None, recipes=None,
                     crafting_policy=None, grid_stack_limit=None):
    # type: (object, object, object, object, object, object, object, object,
    #        object, object) -> object
    """把**驱动器状态集合**与**接口状态集合**绑进存储服务的构造器。

    服务槽位是逐运行时各造一份的，而驱动器、介质与接口是**跨网络共享**的
    （架构 §3：这些状态不能依赖某个网络实例存活）。因此注入的不是
    「每个网络一份的状态」，而是同一个集合——两个网络同时挂同一块盘时
    由服务侧的重复挂载检测拦住。

    转移策略与背包提供者也是全局各一份：策略里的未定案数值必须与实际
    生效的是同一份（否则 `unresolved_report()` 报的数字与真正用的对不上），
    而背包是玩家级资源，与网络归属无关。

    `returns`（任务 10）是**第二条接缝**：`TransferCoordinator` 只在
    `commit()` 成功之后才把「一份外部资源真的进来了」通知给 `reflow_sink`，
    通知里带的是**实际到达量**。`inbox_for(runtime)` 给出的是与 09 那边
    **同一个** `ReturnsPort`——两条线共用一份索引，才不会出现「订单在等什么」
    与「东西给了谁」对不上的形状。接不上时保持 `None`：那种情况下诊断里
    写的是「还没接，通知无处可去」，而不是「不用发」。
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
            workbench=workbench,
            recipes=recipes,
            crafting_policy=crafting_policy,
            grid_stack_limit=grid_stack_limit,
            reflow_sink=_reflow_sink_for(returns, runtime),
        )

    return build


#: 合成终端的方块型号。与 `definitions.py` 的 `DEVICES` 是同一个字符串，
#: 这里再写一份是因为启动层要在不 import 定义表条目的前提下判型号；
#: `test_crafting_workbench` 有一条用例把它与定义表钉在一起。
CRAFTING_TERMINAL_DEVICE_ID = "endernet:crafting_terminal_panel"


def _craft_slot_row(index, entry):
    # type: (int, object) -> dict
    """九宫格里的一格转成线上形状。

    **`name` / `aux` 不是冗余的 `canonical` 的另一种写法，是客户端画图标
    唯一能用的那两样。** 客户端渲染一个格要靠 `#item_id_aux`，而它是
    `Item(资源名).GetBasicInfo().id_aux`——`canonical()` 那个指纹里虽然
    也含着名字，但从指纹里反解名字是拿格式当接口，改一次编码就悄悄失效。
    `InventorySnapshot` 只用 `canonical` 是因为玩家背包客户端**本地就能读
    引擎**，它拿那个字段做槽锚而不是做渲染；九宫格是服务端权威、客户端
    本地读不到，因此必须把能画的字段一起带上。

    空位的写法与 `InventorySnapshot` 一致：`canonical: ""`、`count: 0`，
    另外 `name: ""`、`aux: 0`——**四条都在，不用缺字段表达空**。
    """
    if entry is None:
        return {"slot": index, "canonical": "", "count": 0, "name": "", "aux": 0}
    return {
        "slot": index,
        "canonical": entry[0].canonical(),
        "count": entry[1],
        "name": entry[0].name,
        "aux": entry[0].aux,
    }


def _reflow_sink_for(returns, runtime):
    # type: (object, object) -> object
    """把全局的回流接收侧落到某一个分量上。**鸭子类型**：

    - 真实现（`returns.ReturnsService`）有 `inbox_for`；
    - 测试里注入的具名假回流（`test_executor.FakeReturns`）没有，它本身
      就是个可调用对象，原样返回即可；
    - `None`（没接线）保持 `None`。

    **可调用性是这个入口的准入条件**：库的默认口径 `AbsentReturns` 没有
    `inbox_for`，但它也**不是**可调用的——原样交给存储服务只会在第一次
    回流时炸成 `'AbsentReturns' object is not callable`。它要的是「没接上」，
    而那正是 `None` 的含义，因此这里如实给 `None`（09 那一侧另走
    工厂服务，拿到的仍是 `AbsentReturns`，`available: False` 由它报）。

    写成函数而不是在构造器里 `getattr` 内联：两个构造器（存储与工厂）
    要的是**同一个**对象的两副面孔，把「怎么取」收在一处，
    两边的取法就不会分叉。
    """
    if returns is None:
        return None
    inbox_for = getattr(returns, "inbox_for", None)
    if callable(inbox_for):
        return inbox_for(runtime)
    return returns if callable(returns) else None


def _returns_port_for(returns, runtime):
    # type: (object, object) -> object
    """把全局的回流索引落到 09 需要的那副面孔上（见 `_reflow_sink_for`）。"""
    if returns is None:
        return None
    port_for = getattr(returns, "port_for", None)
    if callable(port_for):
        return port_for(runtime)
    return returns


def _factory_builder(policy, planner, patterns, ports, returns=None, on_slots_changed=None):
    # type: (object, object, object, object, object, object) -> object
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

    `returns` 接上时交给 09 的是**本分量的端口**（`port_for(runtime)`），
    与 `_storage_builder` 给 06 的是同一个对象；没接上时仍是
    `AbsentReturns`——它明确回答「我没接上」，订单因此如实停在等待产物，
    而不是假装收到（09 的 `TestReturnsNotWired` 钉着这条）。
    """
    def build(runtime):
        # type: (object) -> FactoryService
        port = _returns_port_for(returns, runtime)
        def notify_slots():
            on_slots_changed(runtime)
        return FactoryService(
            runtime,
            policy=policy,
            planner=planner,
            patterns=patterns,
            ports=ports,
            gateway=PortGateway(runtime),
            returns=port if port is not None else AbsentReturns(),
            port_blocker=port_blocker,
            template_role=PORT_TEMPLATE_ROLE,
            on_slots_changed=notify_slots if on_slots_changed is not None else None,
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
        return_policy=None,
        returns_sink=None,
        container_primitives=None,
        container_provider=None,
        inventory_provider=None,
        player_dimension=None,
        player_position=None,
        sync_policy=None,
        session_policy=None,
        sync_events=None,
    ):
        # type: (DefinitionSet, object, int, int, int, object, int, object, str, object, object, object, int, object, object, object, object, object, object, object, object, object, object) -> None
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
        #: 回流策略（R06）。**全进程一份**，理由同上面各份。
        #: **它与订单策略是两份**：订单回答「一张订单占几个槽位、几张一块推进、
        #: 返还物算不算完成」，回流回答「同一份资源同时有多个等待者时先给谁」。
        #: 两者都涉及返还物，但一个问的是「算不算数」、另一个问的是「先给谁」。
        #:
        #: **库默认是那侧的替身口径**（登记先后），于是接上 `returns_sink`
        #: 之后 DEV 档能真的走完；正式档由 `assert_assembly_ok()` 报出
        #: `return.priority` / `return.starvation` 两条未定项。取向与
        #: `BandwidthPolicy` 相同，理由写在 `ReturnPolicy` 的 docstring 里。
        if return_policy is None and returns_sink is not None:
            # 注入的接收侧自带策略时**采纳它那一份**：`returns_sink` 是
            # 真正干活的实现，诊断报出来的必须就是它生效的那个口径。
            return_policy = getattr(returns_sink, "policy", None)
        self.return_policy = (
            return_policy if return_policy is not None else ReturnPolicy()
        )
        #: 产物回流的接收侧（任务 10）。**默认是明确报「没接上」的替身**：
        #: 订单因此会停在等待产物并给出 `ORDER_RETURNS_ABSENT`，而不是
        #: 假装收到。离线测试注入具名假回流来驱动状态机（09 文档允许）；
        #: 生产装配**不接受**一个什么都不做的假实现。
        #: 这里把替身**当成默认值真的造出来**，而不是留一个 `None`：
        #: `None` 在诊断里与「接了但对象是空的」看起来一样。
        #:
        #: **默认不接线是有意的**（与 R06 的替身默认是两件事）：09 的
        #: `returns_absent` 计数、`order_view()` 与一整套用例都钉在
        #: 「库默认不假装能用」上，改它属于动 09 的可见行为。游戏内要能玩，
        #: 由 `start_server()` 显式装配 `ReturnsService`——与
        #: `EnergyPolicy.dev_stand_in()` 只出现在那一个写了理由的地方同例。
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
        #: 合成终端的工作台集合（11-04）。**只对合成终端开口**：
        #: `accepts` 按方块型号判，别的方块拿不到工作台，于是命令侧
        #: 「这台方块没有九宫格」是一条明确拒绝，而不是凭空造一台空的。
        self.workbench = WorkbenchStore(accepts=self._is_crafting_terminal)
        #: 工作台配方来源。引擎调用写在方法体里，构造它是安全的。
        self.recipes = EngineRecipeSource()
        #: 合成终端的未定口径（文档第 8 节）。**全进程一份**：策略里的
        #: 替身数值必须与实际生效的是同一份，否则诊断报的与真正用的对不上。
        self.crafting_policy = CraftingPolicy()
        #: 九宫格的叠加上限来源，与容器那边同一份 `stack_limit`。
        self._craft_stack_limit = stack_limit
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
            # 07-05 的投料入口要按位置找那一份工厂服务。**传绑定方法**，
            # 因为 `self.runtimes` 这时还没构造（见 `_factory_of`）。
            factory_of=self._factory_of,
        )
        self.patterns.assembly.on_changed = self._note_assembly_energy_changed
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
                        self.returns_sink,
                        workbench=self.workbench,
                        recipes=self.recipes,
                        crafting_policy=self.crafting_policy,
                        grid_stack_limit=self._craft_stack_limit,
                    ),
                    ServiceId.ENERGY: _energy_builder(
                        self.energy_policy, self._energy_inputs_of),
                    ServiceId.FACTORY: _factory_builder(
                        self.order_policy,
                        self.planner,
                        self.patterns,
                        self.ports,
                        self.returns_sink,
                        self._note_factory_energy_changed,
                    ),
                }
            )
        #: 终端同步中枢（任务 11）。**它必须早于 `RuntimeManager` 构造**：
        #: 主控的命令准入探针（`session_check`）就是它的 `accepts()`，而那个
        #: 参数在下面那次构造里就要用。反过来先建运行时再回填，主控手里拿到的
        #: 会是 `None`——命令于是**全部放行**，而那是「没有这道门禁」的形状，
        #: 不是「暂时没接上」。
        #:
        #: 两个存储/上下文口子现在指向还不存在的东西（`self.runtimes`），
        #: 但它们是**绑定方法**，真正被调到时运行时早就装配好了。
        self.sync_policy = sync_policy if sync_policy is not None else SyncPolicy()
        self.session_policy = (
            session_policy if session_policy is not None else SessionPolicy()
        )
        #: 同步通道的**引擎事件模块**。注入替身即可在无引擎环境下跑通
        #: `start(bind_engine=True)`——「真机才试得出来」这件事在 01 已经吃过
        #: 一次亏（注册与注销必须能离线验证）。不注入时由适配器取真模块。
        #:
        #: 与 `player_dimension` 同一地位：都是**给适配器用的引擎口子**，
        #: 存在的理由也一样——注册路径本身要在游戏外可测。
        self.sync_events = sync_events
        self.sync = TerminalSyncHub(
            policy=self.sync_policy,
            registry=SessionRegistry(self.session_policy),
            storage_of=self._sync_storage_of,
            context_of=self._sync_context_of,
            # 手动存取命令的出口（11-03）。`submit_command` 只是
            # `application.submit` 的转发，注释写明**永不抛异常**——
            # 与 `_on_command` 对执行方的要求正好吻合，因此不必另写一层。
            submit=self.submit_command,
            # 设备总体状态（11-08/11-13）。同样是**绑定方法**，指向此刻还
            # 不存在的 `self.runtimes`——与上面两个口子同一个道理。
            status_of=self._sync_status_of,
            # 合成终端的工作台（11-04）。同样指向此刻还不存在的
            # `self.workbench`——与上面三个口子同一个道理：绑定方法
            # 在调用时才查属性。
            craft_of=self._sync_craft_of,
        )
        if session_check is None:
            # 注入值优先：离线测试与 05/08 的既有用例会自己传探针，
            # 覆盖它属于改别人的可见行为。没传时接上会话表——这就是
            # 计划里「组合根注入 `lambda source: hub.accepts(source)`」那一步，
            # 写成绑定方法而不是 lambda：两者的可调用性相同，但绑定方法在
            # 诊断里认得出是谁。
            session_check = self.sync.accepts
        #: `publish_budget_keys` 这类同步预算**不另开一个构造参数**：
        #: `SyncPolicy` 已经带着它们，注入 `sync_policy` 就是唯一的旋钮。
        #: 同一个数字有两个入口时，`unresolved_report()` 报出来的与真正
        #: 生效的迟早不是同一个（全局约束 7 要防的正是这个）。
        self._epochs = {}
        self._next_epoch = 1

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
            # 同一个解析器也交给同步端口（见下面 `AdapterSyncPort` 那一段）。
            player_dimension=player_dimension,
        )
        # 同步端口在这里才建：它要拿 `event_binder` 去注册监听器（注册本身
        # 发生在 `start()`，这里只组装）。**构造端口不碰引擎**——引擎 import
        # 全在方法体内，因此离线装配照样能走到这一行。
        #:
        #: **维度解析与 `ServerEventBinder` 共用同一份**（同一个构造参数接两处）。
        #: 「这个玩家在哪个维度」只有一个答案，两处各接一个的话，注入替身时
        #: 只换其中一个，就会出现「容器事件认为他在 A、会话校验认为他在 B」——
        #: 而两个症状都长成「有时不好使」，没法查。位置只此一处用，因此它是
        #: 独立参数；不注入时取引擎实现。
        self.sync_port = AdapterSyncPort(
            self.event_binder,
            events=self.sync_events,
            player_dimension=player_dimension,
            player_position=player_position,
        )
        self.sync.attach(self.sync_port)
        # 运行时状态变化的消费者（任务 11）。挂在这里而不是让 sync 自己订阅：
        # `_emit` 的订阅者顺序按注册先后走，而这一步在**装配期**完成，
        # 与 04/05 那些消费者一样是固定的、可复现的顺序。
        self.runtimes.add_listener(self._on_runtime_change)
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
            # 同步通道的两个监听器（任务 11）。**与上面同一批注册**：
            # 走 `event_binder` 的账本，`stop()` 才会把它们一并注销；
            # 另开一份账本的代价是热重载后同一个包被处理两次。
            self.sync_port.listen(self._on_sync_request)
            self.sync_port.on_player_leave(self._on_player_leave)
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
        # 会话与订阅缓存一并丢掉。**监听器已经注销了，但订阅表还活着**——
        # 留着它的症状不是报错，而是下一次 `start()` 时发布窗口以为还有人在看，
        # 于是对着一批早已离线的玩家组包。
        self.sync.shutdown()
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
        # 发布窗口**紧跟运行时推进**（任务 11-02）：提交发生在 `runtimes.tick`
        # 内，取脏键放其后，窗口才看得见本帧提交。放前面会让本帧的提交等到
        # 下一帧才发布——那不是错，只是白慢一拍；放后面则更快。位置不能挪。
        #
        # 没有订阅者的网络连碰都不碰（`tick()` 只遍历有会话的网络），
        # 因此那句「无人订阅时 `take_dirty_keys()` 不会被调用」继续成立。
        published = self.sync.tick()
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
            #: 本帧同步窗口取走的脏物品键数（11-02）。恒为 0 是正常的——
            #: 没有终端订阅、或本帧没有任何物品提交时都是 0。**它不等于
            #: 「没发包」**：快照分片与暂存增量补发不经过窗口，走的是会话
            #: 自己的传输进度（见 `sync.diagnostics()`）。
            "sync_published": published,
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
        """插拔介质后立刻重算负载名额，并回填能源需求（04/05）。

        **必须早于后续交易**：否则刚插上的一块盘还按旧准入放行，
        而旧准入不知道这台设备的需求变了。
        """
        changed = self.admission.note_device_load_changed(node_key)
        runtimes = getattr(self, "runtimes", None)
        runtime = runtimes.runtime_of(node_key) if runtimes is not None else None
        state = self.drivers.get(node_key)
        if runtime is not None and state is not None:
            energy = runtime.services.get(ServiceId.ENERGY)
            if isinstance(energy, EnergyService):
                energy.set_device_state(
                    state.ref, unit_count=state.disk_count(self.drivers.medium))
        return changed

    def _energy_inputs_of(self, runtime, ref, device_id):
        # type: (object, object, str) -> dict
        """只读权威设备状态，不用可被配置命令改写的数量投影。"""
        if device_id == "endernet:storage_driver":
            state = self.drivers.get_by_ref(ref)
            return {"unit_count": (
                state.disk_count(self.drivers.medium) if state is not None else None)}
        category = self.definitions.get(device_id).category
        if category == CATEGORY_ASSEMBLY:
            return {"state": DEVICE_WORK if self.patterns.assembly.is_working(ref) else DEVICE_IDLE}
        if category == CATEGORY_PROCESSOR:
            factory = runtime.services.get(ServiceId.FACTORY)
            if not isinstance(factory, FactoryService):
                return {"state": None}
            working = ref.node_key in factory.working_processors()
            return {"state": DEVICE_WORK if working else DEVICE_IDLE}
        return {}

    def _note_factory_energy_changed(self, runtime):
        # type: (object) -> None
        """租约先记账，再更新能源；同帧下一次主控准入可见新需求。"""
        energy = runtime.services.get(ServiceId.ENERGY)
        if not isinstance(energy, EnergyService):
            return
        for node in runtime.members():
            if node.category == CATEGORY_PROCESSOR:
                inputs = self._energy_inputs_of(runtime, node.ref, node.device_id)
                energy.set_device_state(node.ref, state=inputs["state"])

    def _note_assembly_energy_changed(self, holder):
        # type: (str) -> None
        """全局实体槽使用权只通知同代设备当前所属网络。"""
        for runtime in self.runtimes.runtimes():
            for node in runtime.members():
                if node.category != CATEGORY_ASSEMBLY or "%s" % (node.ref.as_key(),) != holder:
                    continue
                energy = runtime.services.get(ServiceId.ENERGY)
                if isinstance(energy, EnergyService):
                    inputs = self._energy_inputs_of(runtime, node.ref, node.device_id)
                    energy.set_device_state(node.ref, state=inputs["state"])

    # -- 终端同步的接线（任务 11） --------------------------------------

    def _on_sync_request(self, event):
        # type: (object) -> object
        """C2S 请求的引擎入口：取身份 → hub 收发 → 把回包发回给同一个玩家。

        **身份只从事件上取**（`__id__`），不从消息体里读——协议层连那个
        字段都没定义，因此这里没有取错的机会。

        **解析与编码都在 hub 里**，这里只做两件适配器做不了的事：从事件
        上取玩家，以及把回包发回给**同一个**玩家。编码不在适配器里做，
        是因为它要用该会话的资源描述表，而那张表按会话逐份发。
        """
        player_id = self.sync_port.source_player_id(event)
        # `payload_of` 摘掉引擎附的身份字段：协议解析器对未知字段拒收整条，
        # 不摘的话每一条请求都会在解析处变成坏包（见该方法的说明）。
        payload = self.sync.receive(player_id, self.sync_port.payload_of(event))
        if payload is None:
            return None
        # 发送失败的计数在 `AdapterSyncPort.listen` 的 `dispatch` 里
        # （`handler_errors`）：这里是引擎回调，异常抛出去由它接住。
        # 在这一层再包一层 try 只会让「引擎坏了」与「这条包坏了」变成
        # 同一个样子，而两者的查法完全不同。
        self.sync_port.send(player_id, payload)
        return payload

    def _on_player_leave(self, player_id):
        # type: (str) -> int
        """玩家离线：关掉他的全部会话，返回关掉的数量。

        **不注册它的后果不是报错，是会话泄漏**：人走了，订阅表里还留着
        那个会话，发布窗口从此每帧都以为有人在看，而包发给一个已经不在的
        玩家——那正是 11-02 要防的「无人订阅时的空转」。
        """
        closed = len(self.sync.close_for_player(player_id, "player_left"))
        forget = getattr(self.inventories, "forget", None)
        if callable(forget):
            forget(player_id)
        return closed

    def _sync_runtime(self, network_id):
        # type: (str) -> object
        """按**线上网络标识**找运行时，找不到返回 None。

        `RuntimeManager` 只提供「按分量编号取」，而这里要的是反方向。线性
        扫一遍，而不是把编号从字符串解析回来：解析要假定 `network_id_of`
        的写法可逆，而它正是项目里唯一做这个转换的地方——反过来依赖它，
        等于把「转换可逆」变成一条没人维护的隐含约定。网络数量是个位数，
        这一层的代价可以忽略（同形的只读查找先例见 `application.py`）。
        """
        for runtime in self.runtimes.runtimes():
            if TerminalSyncHub.network_id_of(runtime.component_id) == network_id:
                return runtime
        return None

    def _factory_of(self, node_key):
        # type: (object) -> object
        """某台设备所在网络的工厂服务（09），没有就返回 None。

        给 07-05 的契约入口 `PatternProviderPort.push_inputs` 用：它要把料
        备到那台设备的口上，而「哪台设备属于哪张网络、那张网络的工厂服务
        是哪一份」是装配层才知道的事实。

        **必须是绑定方法、不能在这里当场求值**：`self.patterns` 在
        `self.runtimes` **之前**构造（`docstring` 里那句「样板装在方块容器里，
        不能因为运行时重建而消失」正说明了这个顺序），构造那一刻
        `self.runtimes` 还不存在。绑定方法把求值推迟到真正被调到时，
        而那时装配早就完成了——与 `_sync_storage_of` 同一个道理。

        按位置**现取**还有第二个理由：设备会被拆下来装到别的网络上，
        而提供者状态是跟着方块容器走的。存一个服务引用就会拿着旧网络的
        那一份去备料。

        **这条门面不绕过准入**：`stage_inputs` 内部走的仍是
        `OrderExecutor._port_stage` → 注入的 `PortGateway` → 主控的
        去重账本与准入链。这里只是把「哪份服务」查出来。
        """
        runtime = self.runtimes.runtime_of(node_key)
        if runtime is None:
            return None
        return runtime.services.get(ServiceId.FACTORY)

    def _sync_storage_of(self, network_id):
        # type: (str) -> object
        """某个网络的存储服务，没有就返回 None。

        **只读路径：不订阅服务、不碰 `ServiceId.ALL`**（先例见
        `application.py` 的 `debug_query`）。`ServiceRegistry.get` 在缺失时
        返回 `None`，正好是 hub 要的「这个网络现在读不到库存」；真正的结论
        （`RESYNC_NO_STORAGE`）由 hub 下，这里不替它判。
        """
        runtime = self._sync_runtime(network_id)
        if runtime is None:
            return None
        return runtime.services.get(ServiceId.STORAGE)

    def _is_crafting_terminal(self, device_ref):
        # type: (object) -> bool
        """这台设备是不是合成终端。

        判据是**注册表里的方块型号**，不是「能不能拿到工作台」——反过来的
        话，任何一次误调用都会给一个不存在的设备造出一台空工作台，
        而「这台方块没有九宫格」与「它的九宫格是空的」在界面上是两回事。

        查不到记录时返回假（**拒绝**，不是放行）：代次对不上、方块已被拆、
        区块未加载都会走到这里，那时候放行等于让一条陈旧请求动到
        某个来路不明的工作台。
        """
        if device_ref is None:
            return False
        record = self.registry.get(device_ref.node_key)
        if record is None or not record.ref.matches(device_ref):
            return False
        return record.device_id == CRAFTING_TERMINAL_DEVICE_ID

    def _sync_craft_of(self, device_ref):
        # type: (object) -> object
        """合成终端的九宫格状态（11-04），非合成终端返回 `None`。

        **返回 `None` 与「返回一个空格子」是两回事**：前者是「这台设备没有
        工作台」，后者是「你的原料没了」。同步层据此决定发不发。

        预览（`preview`）在这里算：它要查引擎配方表，而 `sync/terminal.py`
        不许 import 引擎。判定照旧是**格子里实际有什么**，不是保留的配方
        备忘——玩家可能把格子动过。
        """
        if not self._is_crafting_terminal(device_ref):
            return None
        grid = self.workbench.get(device_ref)
        if grid is None:
            return None
        return {
            "revision": grid.revision,
            "slots": [_craft_slot_row(index, grid.entry(index))
                      for index in range(SLOT_COUNT)],
            "recipe": grid.recipe or "",
            "preview": self._craft_preview(grid),
        }

    def _craft_preview(self, grid):
        # type: (object) -> object
        """当前摆位能做出什么。**不做合成**，只回答界面「点了会怎样」。"""
        if self.recipes is None:
            return None
        return preview_of(grid, self.recipes)

    def _sync_status_of(self, device_ref):
        # type: (object) -> object
        """一台设备的总体在线/离线（11-08/11-13），没有运行时也照样回答。

        **只读路径**：`RuntimeManager.device_status` 在没有运行时、没有
        主控、断电、超载、分量不存在时都给结论（`compose_device_status`
        合成），不订阅服务、不预留、不写任何东西——这正是 11-08 那句
        「状态查询不触发业务写入」所依赖的性质。缺代次/位置的复核在
        `sync/device_status.read_status` 里做，这里只负责把设备身份换成
        结论。
        """
        return self.runtimes.device_status(device_ref)

    def _sync_context_of(self, player_id, device_ref):
        # type: (str, object) -> object
        """把一个会话请求需要的外部事实解析成 `SessionContext`。

        **全部事实都由服务端自己查**：设备还在不在（注册表）、属于哪个网络
        与哪一任主控（运行时）、玩家在哪个维度、离得多远（端口）。客户端
        申报的只有「我想开哪台设备的界面」，那是请求，不是依据。

        任何一项查不到都不在这里拒绝，而是如实填 `None`——拒绝的判据集中在
        `SessionRegistry.check()` 一处（各条理由还分先后）。分两处判迟早会
        出现「这里放行、那里拒绝」的形状。
        """
        if device_ref is None:
            return None
        runtime = self.runtimes.runtime_of(device_ref.node_key)
        if runtime is None:
            return None
        record = self.registry.get(device_ref.node_key)
        master = runtime.master
        dimension = None
        position = None
        if self.sync_port is not None:
            # 查不到是常态（玩家刚离线、实体刚卸载），端口把这类归为 None，
            # 会话校验按 `require_position` 处理——默认是拒绝。
            dimension = self.sync_port.dimension_of(player_id)
            position = self.sync_port.position_of(player_id)
        return SessionContext(
            player_id,
            # **传 `record.ref` 而不是 `record`**：`record_ref` 的类型是
            # `DeviceRef`（`check()` 要拿它与请求里的那个 `matches`），而
            # `DeviceRecord` 只是持有 `ref` 的一个记录，不是 `DeviceRef` 的
            # 子类。传错的表现不是报错——`matches` 对非 `DeviceRef` 一律返回
            # False，于是**每一个** `session_open` 都被判成 `STALE_INSTANCE`，
            # 症状是「终端一台都打不开」。
            None if record is None else record.ref,
            TerminalSyncHub.network_id_of(runtime.component_id),
            self._epoch_of(runtime.component_id),
            master.instance_id if master is not None else None,
            network_workable=runtime.is_open(),
            player_dimension=dimension,
            player_position=position,
        )

    def _epoch_of(self, component_id):
        # type: (object) -> str
        """某个运行时实例的代次。**同一实例恒定，退役后作废。**

        代次进会话表，也进 `ResourceDescriptions`。两者必须是同一个值，
        否则每一条 S2C 都会因资源序号对不上而解码失败——因此它必须**在实例
        存活期间恒定**，不能是个每帧变的计数。

        **编号回收不构成复用风险**：分量退役时 `_on_runtime_change` 会丢掉
        这个条目的映射，而计数器只增不减，于是同一编号上重建出来的新实例
        拿到的是**新的**代次。少丢那一次正是最坏的一种——旧会话在新的网络
        实例上被当成有效。
        """
        epoch = self._epochs.get(component_id)
        if epoch is None:
            epoch = "%s#%d" % (component_id, self._next_epoch)
            self._next_epoch += 1
            self._epochs[component_id] = epoch
        return epoch

    def _on_runtime_change(self, changes):
        # type: (tuple) -> None
        """`RuntimeManager` 的变化广播 → 注销失效会话。

        **异常在这里接住。** 这条回调挂在拓扑发布路径上
        （`topology.advance()` → `on_publication` → `_emit`），抛出去打挂的是
        本帧后续的全部工作，远比少关一个会话严重。记进 `tick_errors` 而不是
        静默：静默的代价是「界面为什么自己关了」在实机上无从查起。
        """
        import traceback

        for change in changes:
            try:
                self.sync.on_runtime_change(change)
            except Exception:
                self._record_tick_error(traceback.format_exc())
            if change.kind == RuntimeChange.RETIRED:
                # 分量没了，它那一代的代次随之作废。**放在 try 外面**：
                # 上面那条回调失败也不该让一个已经退役的编号继续占着旧代次。
                self._epochs.pop(change.component_id, None)

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
            "sync": self.sync_diagnostics(),
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
                "槽位与内存租约、分帧备料与派发、暂停取消与重验）、"
                "任务 10 的产物回流与分配（资源键索引、按真实到达量分配、"
                "归属与计数同步、回执去重、迟到产物走普通库存）；"
                "装配档位 %s。**接口接触真实引擎的那一段（箱子、背包、其他模组的"
                "物流）没有在游戏内实测过**，流体路径则整条未接入（13-03）。"
                "**四个服务槽位都已是正式实现**（工厂的替身已随任务 09 撤掉），"
                "但「不是替身」不等于「能玩」："
                "本装配若没显式注入 `returns_sink`，产物回流入口仍是任务 10 的"
                "具名替身（`AbsentReturns`），订单会一路推进到「等待产物」然后"
                "**如实阻塞**并给出 order_returns_absent——那不是缺料、"
                "也不是机器坏了，是**这条装配没有把 10 接上**"
                "（游戏内由 `start_server()` 接上）；"
                "装配仓执行是 07-06，它的回流入口已备好但仍无调用方。"
                "存档后端尚未接入（任务 12）；界面同步**只接了底座**——"
                "会话与消息协议、快照与增量、长列表模型已完成，但没有任何一面"
                "终端界面接上它（11-03 及以后等待 UI 交接），"
                "因此 `sync` 那一节的 `networks` 为空是正常的。"
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
            "样板口的成批投料入口（`port_stage`）由工厂（09）的备料调用，"
            "这条路已通；而**回流那一侧**（`TransferCoordinator.sink`）"
            "要装配层显式注入 `returns_sink` 才发得出去，默认装配下它还是"
            "「还没接」——`diagnostics()` 里看得见 `sink_wired` 与 "
            "`sink_missing`。"
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
        """订单层诊断（09）与回流层诊断（10）。

        **只给策略、替身口径与各网络的合计**：逐张订单的内容属于那台网络的
        工厂服务（订单是每网络一份的状态），不该在启动层再存一份。
        想看某张订单走 `debug_query("order", component_id=..., order_id=...)`，
        想看逐条等待记录走 `debug_query("returns", component_id=..., key=...)`。

        这里最要紧的一条是 `returns_sink`：无论接没接上，它都把答案摆在
        启动层的诊断里，而不是只藏在某个网络的服务里——否则「订单为什么
        不动」在最外层看起来像什么都没发生：接上之前是 `available: False`
        （那一半还没写），接上之后是 `available: True` 而 `waiting_*` 为 0
        （没人等产物，那是正常的）。
        """
        return {
            "policy": self.order_policy.describe(),
            "unresolved": self.order_policy.unresolved_report(),
            "return_policy": self.return_policy.describe(),
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

    def sync_diagnostics(self):
        # type: () -> dict
        """终端同步诊断（11-01/11-02）。

        **只有会话表、订阅表与计数，没有任何库存内容**：库存的真相在存储
        服务里，在启动层再抄一份就是第二份真相。想看某个会话发了什么走
        `hub.diagnostics()["networks"]`，逐条包不在这里留。

        `scope_note` 要说清现在到哪一步了：**通道与底座在，界面不在**。
        读的人该去查的是「UI 有没有交接」，不是「同步层是不是坏了」。
        """
        report = self.sync.diagnostics()
        report["port"] = self.sync_port.diagnostics()
        report["unresolved"] = (
            self.sync_policy.unresolved_report()
            + self.session_policy.unresolved_report()
        )
        report["scope_note"] = (
            "同步通道已接线：会话与消息协议（11-01）、快照/分批/版本增量/"
            "断档重取/订阅清理（11-02）、长列表模型（11-09）已在离线层面完成。"
            "**没有一面终端界面接上它**（11-03～11-07、11-10～11-14 未开始），"
            "因此现在没有任何客户端在订阅——`networks` 为空是正常的，"
            "不是「同步挂了」。11.md 页尾的请求大小、会话过期与分页上限"
            "仍未测量（11-14），相关字段见 `unresolved`。"
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
                # 10 的回流策略带的是 R06（分配优先级与防饥饿）。
                # **它与订单策略是两份**：订单回答「一张订单占几个槽位、
                # 返还物算不算完成」，回流回答「同一份资源同时有多个等待者时
                # 先给谁」。两者都碰返还物，但问的不是同一件事。
                #
                # 注意**这条只在这里生效**：主控准入链第 10 条读的是
                # `runtime.py` / `application.py` 传下去的那一份
                # `admission.policy`（`BandwidthPolicy`），`_PolicyBundle`
                # **从不进入主控**。所以 R06 的正式档拦截走的是
                # `assert_assembly_ok()`，与 R04/R09/R11 现状一致。
                self.return_policy,
                # 07 的样板策略带的是 R09（两类样板适用范围）。「无限燃料」
                # 与「返还物显式声明」两侧已于 2026-09-30 定案、因此都不再进
                # 报告；少放一份的代价是正式验收时剩下那一条漏网，而它正是
                # 全局约束 7 最怕的形态。
                self.pattern_policy,
                # 08 的规划策略带的是 R11（目标量是生产量还是保有量）、
                # R04（内存估算口径）与 08-04（配方优先级、替代配方范围、
                # 四个搜索上限）。**它与样板策略是两份，都要在**：
                # 前者说的是「这份配方要备哪些料」，后者说的是「要几个批次」。
                self.plan_policy,
                # 11 的两份策略：`sync_policy` 带的是传输与缓存边界，
                # `session_policy` 带的是会话的时效与交互距离。11.md 页尾
                # 明写这些「由目标环境测量后确定」，因此它们现在全是
                # **待定值**——不进这份清单就等于正式验收时整块漏网。
                self.sync_policy,
                self.session_policy,
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

    **三件事都在这里显式写出来，理由也写在这里。**

    1. **DEV 档。** PRODUCTION 档不会让未完成的实现开始工作，只会让每一条
       业务命令都被拒（`assembly_unresolved`），并且把诊断里的「哪些数值
       还没定案」当成正常结论报出来。游戏内现在只能用 DEV 档。

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

    3. **回流接上真实现（任务 10 的 `ReturnsService`）。** 库默认给的是
       `AbsentReturns`（明确报「没接上」），因此默认装配下订单**永远到不了
       completed**——那是 09 交付时刻意的形状，用来把「这一半还没写」摆在
       诊断里。10 落地之后游戏内要的是能玩：产物真的回流进来，订单真的结单。

       **R06 仍是替身口径**（登记先后）。与第 2 条同理：替身取值只出现在
       这一个写了理由的地方，策略字段照旧进 `unresolved_report()`，
       `assert_assembly_ok()` 照旧拒绝。

    写成显式参数而不是依赖默认值：默认值将来若被改动，这里不会跟着悄悄
    改变游戏内的行为。
    """
    global _SERVER
    if _SERVER is not None:
        _SERVER.stop()
    # 两份引用同一个策略对象：`unresolved_report()` 报出来的必须就是
    # 服务真正按它分配的那一份（装配层也会从 `returns_sink.policy` 采纳它）。
    return_policy = ReturnPolicy()
    returns = ReturnsService(policy=return_policy)
    _SERVER = ServerBootstrap(
        assembly_profile=AssemblyProfile.DEV,
        energy_policy=EnergyPolicy.dev_stand_in(),
        return_policy=return_policy,
        returns_sink=returns,
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
