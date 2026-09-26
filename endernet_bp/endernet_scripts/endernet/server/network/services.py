# coding=utf-8
"""网络服务的注册、装配表与替身。

对应任务 03-01。参照 AE2 的 `IGridServiceProvider`：每项网络服务有明确的
节点接入、离开、逻辑推进和保存入口（架构规划 7）。

**这一层的存在理由是三件事，不是「引入依赖注入框架」：**

1. **服务不能互相随意查找全局单例。** 一个服务只能从它所属的
   `NetworkRuntime` 拿到别的服务，而运行时是逐分量独立的，因此
   「每个独立网络拥有独立服务状态」是结构性的，不靠自觉。
2. **依赖关系要显式，成环或缺依赖在装配期就报错**，而不是等到某个
   只在特定玩法下才走到的分支里 `AttributeError`。
3. **替身与正式实现必须在装配期就能区分。** 工厂（09）尚未实现，开发档允许
   替身占位；正式档必须拒绝替身，也要拒绝「未定规则用替身值悄悄定案」
   （全局约束 7，01 交付说明第八节的第二项契约）。**落地了不等于定案**：
   能源（04）已从替身换成正式实现，但它的两条策略字段仍是未定规则的替身口径，
   因此「已实现」与「可交付」在这张表上依然是两件事。

**服务只能通过主控签发的许可取用。** 取服务要走
`NetworkRuntime.service(service_id, grant)`，那里会校验许可；本模块的
`ServiceRegistry` 本身不做准入，它只负责「谁装了什么、依赖顺序如何」。
"""

from ...common.protocol import BlockReason, CommandResult, Reason

# -- 词汇 ------------------------------------------------------------------


class ServiceId(object):
    """已登记的网络服务槽位。

    编号即契约：04/05/09 落地时把对应槽位换成正式实现即可，
    主控的路由表与装配表不用改。
    """

    #: 逻辑时钟、到期队列与执行预算（任务 04）。
    SCHEDULER = "scheduler"
    #: EN 供用能与能源预留（任务 04）。
    ENERGY = "energy"
    #: 介质、库存、预留、容量与限速（任务 05）。
    STORAGE = "storage"
    #: 样板、规划、订单与执行（任务 07/08/09）。
    FACTORY = "factory"

    ALL = frozenset([SCHEDULER, ENERGY, STORAGE, FACTORY])


class AssemblyProfile(object):
    """装配档位。开发档与正式档对「未完成的实现」态度不同。"""

    #: 允许替身服务与未定规则的替身数值。游戏内目前只能用这一档，
    #: 因为 04/05/09 还没有正式实现。
    DEV = "dev"
    #: 拒绝替身服务，也拒绝任何仍有未定项的准入策略。
    #: 正式验收时用（全局约束 7）。
    PRODUCTION = "production"

    ALL = frozenset([DEV, PRODUCTION])

    @staticmethod
    def validate(value):
        # type: (str) -> str
        if value not in AssemblyProfile.ALL:
            raise ValueError("未知装配档位: %r" % (value,))
        return value


class AssemblyError(AssertionError):
    """装配期的结构性错误：依赖成环、缺依赖、替身混进正式档。"""


class ServiceAbsentError(KeyError):
    """请求的服务没有装配。"""


# -- 服务契约 --------------------------------------------------------------


class NetworkService(object):
    """一项网络服务的契约（架构规划 7 的四入口 + 状态查询）。

    子类在 `__init__` 里设置 `service_id` 与 `requires`。
    全部默认实现都是安全的空操作，正式服务按需覆盖。
    """

    #: `ServiceId` 中的取值。由子类在 `__init__` 里赋值。
    service_id = ""
    #: 依赖的其他服务 id。装配时按它做拓扑排序。
    requires = ()
    #: 是否为替身。正式档拒绝替身。
    is_stand_in = False
    #: 尚未实现该服务时对应的任务编号，供诊断说明「为什么不能用」。
    pending_task = ""

    def attach(self, runtime):
        # type: (object) -> None
        """绑定到所属运行时。每个实例只调用一次。"""
        return None

    def detach(self):
        # type: () -> None
        """解除与运行时的绑定。"""
        return None

    def is_ready(self):
        # type: () -> bool
        """是否已就绪可以接业务。依赖未满足、初始化未完成时为 False。

        与 `is_stand_in` 分开：替身是「根本没写」，未就绪是「写了但还不能用」，
        两者给用户的信息不同（`SERVICE_ABSENT` vs `SERVICE_NOT_READY`）。
        """
        return True

    def power_ok(self, node_key):
        # type: (object) -> object
        """本设备当前是否有可用能源（04）。True / False / None（待确认）。

        只有能源服务覆盖它（`network/energy.py`），而且答的是**网络级**
        判定——能源按网络计。默认 None 表示「没人能回答」——**未确认
        不等于有电**，主控不会据此放行。
        """
        return None

    def handle(self, grant, command):
        # type: (object, object) -> object
        """执行一条已获准入的命令（03-04）。

        `grant` 是主控签发的一次性许可，`command` 是原始命令。
        实现**必须在提交完成后**才发通知（架构规划 5.3）。
        基类不给兜底实现：没写就是没写，返回 `UNSUPPORTED` 而不是空成功。
        """
        raise NotImplementedError(
            "服务 %s 没有实现 handle()" % (self.service_id,)
        )

    def add_node(self, node, saved_data=None):
        # type: (object, dict) -> bool
        """登记该节点的能力与局部恢复数据。

        **可重复调用而不重复挂载**：拓扑重建会对同一节点反复调用，
        实现必须自己保证幂等（架构规划 7）。
        返回是否真的挂载了新的东西。
        """
        return False

    def remove_node(self, node):
        # type: (object) -> bool
        """解除该节点的提供者、监听与到期任务。返回是否有东西被解除。"""
        return False

    def on_wake(self, node_key):
        # type: (object) -> bool
        """收到了该位置的唤醒信号（方块实体 tick）。返回是否处理了。

        **唤醒不是命令**：它与 `add_node` / `note_domain_change` 同属内部
        信号路径，因此不经主控签发许可，也不得产生业务副作用。默认不处理。
        目前只有调度服务覆盖它（把退避打回初值）。
        """
        return False

    def note_domain_change(self, node, from_domain_ids, to_domain_ids):
        # type: (object, tuple, tuple) -> bool
        """负载域归属变了，但分量没变（迁移通知的 `DOMAIN` 种类）。

        单独一个入口而不是复用 `add_node`：域变了不等于设备换了，
        服务可能要迁移的是「这个节点占用了哪个域的多少槽位/容量」，
        而那与节点本身的注册是两件事。返回是否真的处理了。
        **目前没有服务覆盖它**：存储的额度按介质记账、能源的负载跟随折算
        口径又未定，两者都恒返回 `False`，理由写在各自的实现里。
        """
        return False

    def tick(self, context, budget):
        # type: (dict, int) -> int
        """推进本服务的到期工作，返回本帧消耗的工作量。"""
        return 0

    def save_node_data(self, node):
        # type: (object) -> object
        """导出与该节点相关的持久数据；没有可存的返回 None。"""
        return None

    def status(self):
        # type: () -> dict
        """只读状态，供诊断与界面。不得改任何业务状态。"""
        return {"service_id": self.service_id, "available": not self.is_stand_in}

    def diagnostics(self):
        # type: () -> dict
        state = self.status()
        state["stand_in"] = self.is_stand_in
        state["pending_task"] = self.pending_task
        state["requires"] = list(self.requires)
        return state


class StandInService(NetworkService):
    """尚未实现的服务槽位。

    **当前没有任何槽位在用它**（任务 09 之后四个槽位都有了正式实现），
    但它是装配契约的一部分：新增一个槽位时，先让它以替身的样子出现，
    比让它以一个「看起来正常但什么都不做」的空实现出现安全得多。

    **它不假装能用。** 登记节点时什么都不挂载，业务调用一律返回
    `SERVICE_ABSENT`，只有状态查询是有意义的。这样下游拿到的是
    「这个功能还没做」，而不是一份看起来正常的空库存——
    后者会让替身数值被当成正式结果（全局约束 7）。
    """

    is_stand_in = True

    def __init__(self, service_id, pending_task, note=""):
        # type: (str, str, str) -> None
        if service_id not in ServiceId.ALL:
            raise ValueError("未知服务槽位: %r" % (service_id,))
        self.service_id = service_id
        self.pending_task = pending_task
        self.note = note
        self.attach_calls = 0
        self.tick_calls = 0
        self.nodes_seen = 0

    def attach(self, runtime):
        # type: (object) -> None
        self.attach_calls += 1

    def add_node(self, node, saved_data=None):
        # type: (object, dict) -> bool
        # 记一笔「见过这个节点」只为诊断，不挂载任何能力。
        self.nodes_seen += 1
        return False

    def tick(self, context, budget):
        # type: (dict, int) -> int
        self.tick_calls += 1
        return 0

    def is_ready(self):
        # type: () -> bool
        return False

    def refuse(self, action):
        # type: (str) -> BlockReason
        return BlockReason(
            Reason.SERVICE_ABSENT,
            "服务 %s 尚未实现（任务 %s）%s"
            % (self.service_id, self.pending_task, self.note and "：" + self.note or ""),
        )

    def handle(self, grant, command):
        # type: (object, object) -> object
        """业务一律拒绝。

        主控在准入链里就会挡下并给出更具体的说明；这个实现是第二道防线
        ——万一有别的路径直接调到服务，它同样不会假装做成了。
        """
        return CommandResult.blocked(command.request_id, [self.refuse(command.kind)])

    def status(self):
        # type: () -> dict
        return {
            "service_id": self.service_id,
            "available": False,
            "stand_in": True,
            "pending_task": self.pending_task,
            "note": self.note,
        }


def default_stand_ins():
    # type: () -> list
    """默认服务实例集合：**由 `default_builders()` 派生，不是另一份清单**。

    此前这里与 `default_builders()` 各写一份完整清单，靠一条测试拦着不让
    它们分叉。04 把 SCHEDULER 换成正式实现时必须同时改两处，于是索性只留
    一处：裸 `ServiceRegistry()`（离线测试直接构造时走这条）与工厂造的运行时
    从此**不可能**拿到不同的调度器。

    `builders[...](None)` 是合法的：服务的 `__init__` 不碰运行时，
    绑定发生在 `attach()`。
    """
    builders = default_builders()
    return [builders[service_id](None) for service_id in sorted(ServiceId.ALL)]


# -- 装配表 ----------------------------------------------------------------


class ServiceRegistry(object):
    """一个运行时私有的服务集合与依赖顺序。"""

    def __init__(self, services=()):
        # type: (object) -> None
        self._by_id = {}
        self._order = None
        for service in services:
            self.install(service)
        if not services:
            for service in default_stand_ins():
                self.install(service)

    def install(self, service):
        # type: (NetworkService) -> NetworkService
        service_id = getattr(service, "service_id", "")
        if not service_id:
            raise ValueError("服务必须声明 service_id: %r" % (service,))
        if service_id not in ServiceId.ALL:
            raise ValueError("未登记的服务槽位: %r" % (service_id,))
        if service_id in self._by_id:
            raise ValueError(
                "服务槽位 %s 已被 %r 占用，不允许覆盖"
                % (service_id, self._by_id[service_id])
            )
        for required in getattr(service, "requires", ()):
            if required not in ServiceId.ALL:
                raise ValueError("服务 %s 依赖了未登记的槽位 %r" % (service_id, required))
        self._by_id[service_id] = service
        self._order = None
        return service

    def get(self, service_id):
        # type: (str) -> NetworkService
        """缺失返回 None——调用方要区分「没装」与「装了但拒绝」。"""
        return self._by_id.get(service_id)

    def require(self, service_id):
        # type: (str) -> NetworkService
        service = self._by_id.get(service_id)
        if service is None:
            raise ServiceAbsentError("服务未装配: %s" % (service_id,))
        return service

    def ids(self):
        # type: () -> tuple
        return tuple(sorted(self._by_id))

    def services(self):
        # type: () -> list
        return [self._by_id[key] for key in sorted(self._by_id)]

    def stand_ins(self):
        # type: () -> list
        return [item for item in self.services() if item.is_stand_in]

    def assembly_order(self):
        # type: () -> list
        """按依赖拓扑排序。缺依赖或成环直接 `AssemblyError`。

        排序结果只用于 `add_node` / `tick` 的推进顺序：被依赖的服务
        必须先看到节点。**成环不静默放过**——它在运行期只会表现为
        「有时按这个顺序、有时按那个顺序」的幽灵 bug。
        """
        if self._order is not None:
            return list(self._order)
        pending = dict((key, set(self._by_id[key].requires)) for key in self._by_id)
        for key in list(pending):
            missing = sorted(pending[key] - set(self._by_id))
            if missing:
                raise AssemblyError(
                    "服务 %s 依赖了未装配的服务: %s" % (key, ", ".join(missing))
                )
        ordered = []
        while pending:
            ready = sorted(key for key, deps in pending.items() if not deps)
            if not ready:
                raise AssemblyError(
                    "服务依赖成环，无法确定装配顺序: %s"
                    % (", ".join(sorted(pending)),)
                )
            for key in ready:
                ordered.append(self._by_id[key])
                del pending[key]
            for deps in pending.values():
                deps.difference_update(ready)
        self._order = ordered
        return list(ordered)

    def assert_profile_ok(self, profile, policy=None):
        # type: (str, object) -> list
        """按档位检查装配是否可接受。返回被容忍的问题清单（开发档非空）。

        正式档发现问题**直接抛错**，不做「先跑起来再说」的妥协：
        一旦替身数值被下游吃进库存或订单，事后很难分辨哪一份是假的。

        **本检查不会自动运行**，调用它是一件显式的事。理由是抛错会让
        正式档整个起不来，而 `01-交付说明.md` §5.7-2 要的形态是运行期的
        「待确认」——起不来的档位容易被绕开，被拒的命令不会。运行期的
        把关在主控准入链的第 10 条（`assembly_unresolved`）与第 12 条
        （`service_absent`），它们只拦业务，不影响诊断与状态视图。
        这里留给任务 14 的发布验收显式调用。
        """
        AssemblyProfile.validate(profile)
        problems = []
        for service in self.stand_ins():
            problems.append(
                "替身服务 %s（待任务 %s 实现）" % (service.service_id, service.pending_task)
            )
        if policy is not None:
            for item in policy.unresolved_report():
                problems.append(
                    "%s 未定案（规则 %s，当前值 %r）"
                    % (item.get("field"), item.get("rule"), item.get("value"))
                )
        if profile == AssemblyProfile.PRODUCTION and problems:
            raise AssemblyError(
                "正式装配档不接受未完成的实现或未定规则：\n  " + "\n  ".join(problems)
            )
        return problems

    def diagnostics(self):
        # type: () -> dict
        return {
            "services": [service.diagnostics() for service in self.services()],
            "stand_ins": sorted(item.service_id for item in self.stand_ins()),
        }


class ServiceFactory(object):
    """按运行时创建服务实例。

    **每个运行时拿到的必须是全新实例**（验收 #4：每个独立网络拥有独立服务
    状态）。因此工厂而不是服务表才是装配的输入：把一份 `ServiceRegistry`
    分给两个运行时，会让两个网络共享库存归属，而那种错误在离线测试里
    几乎不会自己暴露。

    构造时**从默认替身起步**，部分覆盖的字典不会意外丢掉没提到的槽位——
    「只换了 storage，结果 factory 不见了」这类问题在运行期表现为神秘的
    `SERVICE_ABSENT`，不值得让人去查。
    """

    def __init__(self, builders=None):
        # type: (dict) -> None
        # 留一份默认表的引用：`pending_tasks()` 要靠**身份**区分「默认表里的
        # 替身」与「调用方自己塞的替身」，因此默认表只能生成一次。
        self._defaults = default_builders()
        self._builders = dict(self._defaults)
        for service_id, builder in (builders or {}).items():
            if service_id not in ServiceId.ALL:
                raise ValueError("未登记的服务槽位: %r" % (service_id,))
            if not callable(builder):
                raise TypeError("服务构造器必须可调用: %r -> %r" % (service_id, builder))
            self._builders[service_id] = builder

    def builder(self, service_id):
        # type: (str) -> object
        return self._builders.get(service_id)

    def replace(self, service_id, builder):
        # type: (str, object) -> None
        """换成另一份实现（04/05/09 落地时用）。"""
        if service_id not in ServiceId.ALL:
            raise ValueError("未登记的服务槽位: %r" % (service_id,))
        if not callable(builder):
            raise TypeError("服务构造器必须可调用: %r -> %r" % (service_id, builder))
        self._builders[service_id] = builder

    def ids(self):
        # type: () -> tuple
        return tuple(sorted(self._builders))

    def stand_in_slots(self):
        # type: () -> tuple
        """仍在使用**默认替身实现**的槽位。

        两个条件同时成立才算：构造器还是默认表里的那一个（说明没被换过），
        且该槽位的默认实现本来就是替身（`default_pending_tasks()` 就是
        「哪些槽位的默认实现仍是替身」的表）。

        只看身份曾经够用（04 之前默认表里全是替身，谁换了谁就是正式实现），
        但 04 落地后 SCHEDULER 的**默认构造器本身就是正式实现**——它仍然
        「是默认的那一个」，却显然不再是替身，于是被误报成「待任务 04 实现」。
        第二个条件就是为了把它排除掉。05 的 STORAGE 同理，因此它也从
        `default_pending_tasks()` 移出去了。

        **不构造实例**：构造器只被承诺可以接受一个运行时（`replace()` 只查
        callable），要求它必须能用 `None` 构造会给 05/09 的实现加一条没写进
        契约的隐含义务，而这里只是诊断路径——一个在 `__init__` 里读
        `runtime.registry` 的实现会让 `debug_query("assembly")` 直接炸。
        需要知道**某个实例**是不是替身时，读实例自己的 `is_stand_in`。
        """
        defaults = default_pending_tasks()
        return tuple(
            sorted(
                service_id
                for service_id in self._builders
                if service_id in defaults
                and self._builders[service_id] is self._defaults.get(service_id)
            )
        )

    def pending_tasks(self):
        # type: () -> dict
        """`service_id -> 尚未实现该服务的任务编号`，只列**默认表里的**替身槽位。

        只报默认表里的那一个：调用方自己塞一个替身（哪怕它挂在同一个槽位上）
        时，写这里的任务编号会对不上——那是它自己的替身，不是本项目的待办。
        """
        defaults = default_pending_tasks()
        return dict(
            (service_id, defaults[service_id])
            for service_id in self.stand_in_slots()
            if service_id in defaults
            and self._builders[service_id] is self._defaults.get(service_id)
        )

    def create(self, runtime):
        # type: (object) -> ServiceRegistry
        """为一个运行时构造服务集合。每个槽位调用一次构造器。"""
        services = []
        for service_id in sorted(self._builders):
            services.append(self._builders[service_id](runtime))
        return ServiceRegistry(services)

    def diagnostics(self):
        # type: () -> dict
        return {
            "slots": list(self.ids()),
            "stand_ins": list(self.stand_in_slots()),
            "pending_tasks": self.pending_tasks(),
        }


def default_pending_tasks():
    # type: () -> dict
    """`service_id -> 尚未实现该服务的任务编号`。**只列替身槽位。**

    **此刻是空的**——四个槽位都有正式实现了：SCHEDULER 在任务 04、
    STORAGE 在任务 05、ENERGY 在任务 04 的能源增量、FACTORY 在任务 09。
    表留着是因为它是一份**契约**（新增槽位时先在这里登记「为什么还不能用」），
    而不是因为它现在还有内容。

    **「有实现」不等于「能玩」**：能源的 RF 计量（04-05）、断电暂停（04-06）
    与设备级状态通知（04-08）仍未实现，写在 `network/energy.py` 的
    `scope_note` 里；DEV 档为了让网络真的能跑给能源选了替身口径
    （`EnergyPolicy.dev_stand_in()`），正式档照样拒绝。工厂侧的不完整更具体——
    **库默认装配不给 `returns_sink`**（那一份默认仍是 `AbsentReturns`，
    于是订单停在 `order_returns_absent`），10 的回流分配只在
    `start_server()` 显式装配 `ReturnsService` 之后才生效；装配仓执行是 07-06，
    仍未实现。两者都**不会**让订单看起来成功。
    """
    return {}


def default_builders():
    # type: () -> dict
    """`service_id -> callable(runtime) -> NetworkService` 的默认装配。

    返回的是**构造器**而不是实例：同一份装配表要能为多个运行时各造一套。
    四个槽位都是正式实现（SCHEDULER / STORAGE / ENERGY / FACTORY）。

    **每个槽位的 `(None)` 裸构造都是合法的**：`default_stand_ins()` 会用
    `None` 构造每个槽位，此时它们的 `is_ready()` 为假、业务给
    `SERVICE_NOT_READY`——那是「装了但还没接上运行时/驱动器」，
    不是「这个功能没写」。真正带有驱动器、接口集合与规划器的实例由启动层用
    `_storage_builder()` / `_factory_builder()` 注入。

    **导入写在函数体里是有意的。** 本模块是契约层，`ticking.py`、`energy.py`、
    `storage/service.py` 与 `crafting/executor.py` 是实现层，它们在模块级
    import 本模块；若在这里也模块级 import 回去就成环了。而
    `default_builders()` 只在构造工厂/注册表时被调用，不在导入期，
    因此函数内导入不引入环——**不要把它提到模块顶部**。
    """
    from ..crafting.executor import FactoryService
    from ..storage.service import StorageService
    from .energy import EnergyService
    from .ticking import SchedulerService

    pending = default_pending_tasks()
    notes = {
        ServiceId.FACTORY: "样板、规划、订单与执行",
    }
    out = {
        ServiceId.SCHEDULER: SchedulerService,
        ServiceId.STORAGE: StorageService,
        ServiceId.ENERGY: EnergyService,
        ServiceId.FACTORY: FactoryService,
    }
    for service_id in sorted(ServiceId.ALL - frozenset(out)):
        # 四个槽位都有实现时这一轮是空的；留着它是为了让**新增槽位**时
        # 忘记登记 pending 的代价是「说明为空」而不是装配直接 KeyError。
        out[service_id] = _stand_in_builder(
            service_id, pending.get(service_id, ""), notes.get(service_id, "")
        )
    return out


def _stand_in_builder(service_id, task, note):
    # type: (str, str, str) -> object
    """把槽位与说明绑定进闭包。

    写成显式工厂而不是循环里直接写 `lambda: StandInService(...)`：
    闭包捕获的是变量而不是当时的值，循环写法会让四个槽位都指向最后一个。
    """
    def build(runtime):
        # type: (object) -> NetworkService
        return StandInService(service_id, task, note)

    return build


# -- 命令路由 --------------------------------------------------------------

#: `kind -> (service_id | None, read_only)`。
#:
#: `service_id` 为 None 表示命令不落到业务服务上：
#: `STATUS` 由主控自己答快照，`RECOVERY` 由运行时答受控诊断。
COMMAND_ROUTES = {
    "storage": (ServiceId.STORAGE, False),
    "configure": (ServiceId.STORAGE, False),
    "order": (ServiceId.FACTORY, False),
    "status": (None, True),
    "recovery": (None, True),
}


def route_of(kind):
    # type: (str) -> tuple
    """命令类型 -> `(service_id, read_only)`。未知类型返回 None。

    未知类型**不做兜底推断**：猜一个服务出来就等于让设备绕过准入
    （全局约束 2）。
    """
    return COMMAND_ROUTES.get(kind)


def is_read_only(kind):
    # type: (str) -> bool
    route = route_of(kind)
    return bool(route) and route[1]
