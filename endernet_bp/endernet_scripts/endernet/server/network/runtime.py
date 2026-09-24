# coding=utf-8
"""网络运行时：一个连通分量对应一份可运行的服务集合。

对应任务 03-02 与 03-06。参照 AE2 的 `init/internal/InitGridServices.java`：
网格在建立时把各项服务挂到节点上，节点离开时卸载，网格销毁时整体退役。

**为什么运行时按「分量」而不是按「控制器」组织。** 分量编号在成员增减时
会尽量沿用，而控制器方块可能被拆掉再在原位放一个新的。若把运行时绑在
控制器上，一次「拆掉再放回」就会把整网的服务状态（库存归属、订单归属）
一起清空。绑在分量上，控制器只是这个分量当前的**主控身份**，
位置被顶替时换的是身份而不是容器（03 文档：新放的控制器不得自动继承旧身份）。

**迁移顺序是固定的**，见 `RuntimeManager.on_publication`：
旧服务卸载 → 更新归属 → 新服务挂载 → 校验完成后开放业务。
顺序颠倒会让服务在「图已经变了、归属还没更新」的半更新状态下收到回调。

**合并与拆分。** 02 在合网时沿用覆盖成员最多的那个旧编号，其余编号在
归属完成时就被回收（`_FinishCursor`）。因此：

- 合并：被沿用的编号的运行时**活下来并保留全部服务状态**，另一个编号的
  运行时退役（它的编号已经不存在了）。合并后若出现控制器冲突，
  活下来的那个运行时**保留服务状态但没有主控**——不在这里发明替身服务的
  合并事务，那属于任务 09/10。
- 拆分：一半沿用旧编号，另一半拿到新编号并新建运行时、从空状态开始。
  离开旧运行时的成员会被逐个卸载。

**分量编号在进程重启后会重新分配**，因此 `save_node_data()` 写出的运行时记录
现在还不是合格的持久身份——见 `runtime_record_id()`。
"""

from ...common.protocol import BlockReason, Reason
from ..persistence.repository import Record
from .controller import (
    AccessGrant,  # noqa: F401（供 # type: 注释）
    MasterController,
    RequestLedger,
    RunState,
    component_blockers_by_id,
    compose_device_status,
    power_state_of,
    route_blocker,
    run_state_of,
    service_blockers,
)
from .node import NetworkNode
from .services import (
    AssemblyProfile,
    NetworkService,  # noqa: F401（供 # type: 注释）
    ServiceFactory,
    ServiceId,
)
from ..runtime.scheduler import SchedulerPolicy
from .ticking import NodeLocator, RotationCursor
from .topology import MigrationKind

#: 运行时记录与节点记录的存档类型。任务 12 会用它们组织存档。
RUNTIME_RECORD_KIND = "network_runtime"
NODE_RECORD_KIND = "network_node"


class AccessDenied(Exception):
    """取用服务的请求不合法。

    **抛异常而不是返回原因码**：走到这一步说明是编程错误（没拿许可、
    拿的是旧主控的许可、重复使用许可），把它变成一条业务失败只会让
    真正的缺陷藏进日志里。
    """


def _noop(change):
    return None


class RuntimeChange(object):
    """一次运行时状态变化。04 据此决定起停 ticking，11 据此刷新状态。"""

    #: 分量出现，运行时建立（服务已挂载，尚未开放）。
    CREATED = "created"
    #: 绑定了新的主控。
    MASTER_BOUND = "master_bound"
    #: 失去主控（拆掉、卸载、冲突）。**运行时本身还活着。**
    MASTER_LOST = "master_lost"
    #: 成员增减。
    MEMBERS_CHANGED = "members_changed"
    #: 校验通过，开始接受业务命令。
    OPENED = "opened"
    #: 停止接受业务命令（重建中、无主控、恢复中）。
    CLOSED = "closed"
    #: 分量消失，运行时退役。
    RETIRED = "retired"

    __slots__ = (
        "component_id",
        "kind",
        "before_state",
        "after_state",
        "master_instance_id",
        "reasons",
        "version",
    )

    def __init__(
        self,
        component_id,
        kind,
        before_state=None,
        after_state=None,
        master_instance_id=None,
        reasons=(),
        version=0,
    ):
        # type: (str, str, str, str, str, tuple, int) -> None
        self.component_id = component_id
        self.kind = kind
        self.before_state = before_state
        self.after_state = after_state
        self.master_instance_id = master_instance_id
        self.reasons = tuple(reasons)
        self.version = version

    def to_dict(self):
        # type: () -> dict
        return {
            "component_id": self.component_id,
            "kind": self.kind,
            "before_state": self.before_state,
            "after_state": self.after_state,
            "master_instance_id": self.master_instance_id,
            "reasons": [
                reason.to_dict() if isinstance(reason, BlockReason) else reason
                for reason in self.reasons
            ],
            "version": self.version,
        }

    def __repr__(self):
        return "RuntimeChange(%s, %s -> %s)" % (
            self.component_id,
            self.kind,
            self.after_state,
        )


class NetworkRuntime(object):
    """一个分量的可运行业务集合：主控 + 服务 + 成员 + 开关状态。"""

    def __init__(
        self,
        component_id,
        definitions,
        registry,
        catalog,
        topology,
        admission,
        factories=None,
        profile=AssemblyProfile.DEV,
        policy=None,
        ledger_policy=None,
        session_check=None,
        repository=None,
        emit=None,
    ):
        # type: (str, object, object, object, object, object, ServiceFactory, str, object, object, object, object, object) -> None
        self.component_id = component_id
        self.definitions = definitions
        self.registry = registry
        self.catalog = catalog
        self.topology = topology
        self.admission = admission
        self.profile = AssemblyProfile.validate(profile)
        self.policy = policy
        self.ledger_policy = ledger_policy
        self.session_check = session_check
        self.repository = repository

        #: **本运行时私有**的服务集合。工厂为每次创建调用一次，
        #: 因此两个独立网络绝不共享服务实例（验收 #4）。
        factory = factories if factories is not None else ServiceFactory()
        self.services = factory.create(self)
        for service in self.services.assembly_order():
            service.attach(self)

        self.master = None
        #: 图版本：最近一次同步到的快照版本。
        self.version = 0
        self.tick_count = 0
        #: 上一次推进时的帧号（`context["frame"]`）。公平轮转的余量轮会让
        #: 同一个运行时在一帧里被访问两次，靠它把「按帧走的东西」去重——
        #: 主控时钟与 `stats["ticks"]` 都在其中，见 `tick()`。
        self._frame_marker = None

        self._nodes = {}
        #: node_key -> 该节点的局部恢复数据（由 12 通过 `import_node_data` 注入）。
        self._saved = {}
        self._open = False
        self._close_reason = ""
        self._reported_state = None
        self._emit = emit if emit is not None else _noop
        self.stats = {
            "syncs": 0,
            "nodes_mounted": 0,
            "nodes_dropped": 0,
            "opened": False or 0,
            "ticks": 0,
            #: 服务返回负数 / 超出额度的次数。非零即实现缺陷的痕迹：
            #: 负数会反向放大同帧后续服务的额度，超支则让预算形同虚设。
            "negative_costs": 0,
            "budget_overruns": 0,
            #: **处理了唤醒的服务数**（不是信号数——一个信号可能被多个服务
            #: 处理）。管理器那层的 `wakes_delivered` 数的是送达的信号数。
            "wakes_handled": 0,
            #: 网络关闭时被拦下的唤醒。**分列出来**：它与「送到了但没人处理」
            #: （`deliver_wakes` 的未命中）是两件事，混在一起就查不出
            #: 「唤醒为什么没生效」。
            "wakes_withheld": 0,
        }

    # -- 成员 ----------------------------------------------------------

    def members(self):
        # type: () -> list
        return [self._nodes[key] for key in sorted(self._nodes, key=lambda k: k.as_key())]

    def member_keys(self):
        # type: () -> tuple
        return tuple(sorted(self._nodes, key=lambda key: key.as_key()))

    def contains_node(self, node_key):
        # type: (object) -> bool
        return node_key in self._nodes

    def sync_component(self, snapshot):
        # type: (object) -> int
        """把一个分量的当前成员同步进来，并重绑主控。

        先卸载后挂载：`sync_members` 内部就是这个顺序，因此服务在
        `add_node` 里看到的成员集合已经是最终的。
        """
        component = snapshot.components.get(self.component_id)
        if component is None:
            return 0
        nodes = []
        for node_key in sorted(component.members, key=lambda key: key.as_key()):
            record = self.registry.get(node_key)
            if record is None:
                continue
            nodes.append(NetworkNode.from_record(self.definitions, record))
        mounted = self.sync_members(nodes)
        self.rebind_master(component.master_candidate)
        self.version = snapshot.version
        self.stats["syncs"] += 1
        return mounted

    def sync_members(self, nodes):
        # type: (list) -> int
        """登记新来的、卸载已离开的。

        `add_node` 对**每个**成员都调用，包括上一帧就在的：契约要求实现
        自己保证幂等（`NetworkService.add_node` 的说明），这样服务能在
        配置变化时刷新，而不必再开一个「刷新」入口。
        """
        incoming = {}
        for node in nodes:
            incoming[node.node_key] = node
        for node_key in sorted(
            set(self._nodes) - set(incoming), key=lambda key: key.as_key()
        ):
            self.drop_node(node_key)
        mounted = 0
        for node_key in sorted(incoming, key=lambda key: key.as_key()):
            node = incoming[node_key]
            self._nodes[node_key] = node
            saved = self._saved.get(node_key)
            for service in self.services.assembly_order():
                if service.add_node(node, saved):
                    mounted += 1
        self.stats["nodes_mounted"] += mounted
        if mounted:
            self._notify(RuntimeChange.MEMBERS_CHANGED)
        return mounted

    def drop_node(self, node_key):
        # type: (object) -> bool
        """卸载一个成员。按依赖的**逆序**通知服务。"""
        node = self._nodes.pop(node_key, None)
        if node is None:
            return False
        order = self.services.assembly_order()
        for index in range(len(order) - 1, -1, -1):
            order[index].remove_node(node)
        self._saved.pop(node_key, None)
        self.stats["nodes_dropped"] += 1
        self._notify(RuntimeChange.MEMBERS_CHANGED)
        return True

    def note_domain_change(self, node_key, from_domain_ids, to_domain_ids):
        # type: (object, tuple, tuple) -> int
        """负载域归属变化。分量没变，因此不重挂节点，只让服务知道。"""
        node = self._nodes.get(node_key)
        if node is None:
            return 0
        count = 0
        for service in self.services.assembly_order():
            if service.note_domain_change(node, from_domain_ids, to_domain_ids):
                count += 1
        return count

    # -- 主控 ----------------------------------------------------------

    def rebind_master(self, master_ref):
        # type: (object) -> object
        """绑定或解除主控。返回是否发生了变化。

        **解绑不销毁运行时**：库存与订单归属属于运行时，不能因为一次
        控制器冲突或暂时卸载就被清掉。真正退役只发生在分量消失时。
        """
        if master_ref is None:
            if self.master is None:
                return False
            return self._drop_master("该网络当前没有唯一主控")
        if self.master is not None and self.master.ref == master_ref:
            return False
        if self.master is not None:
            self._drop_master("主控候选已改变")
        # 账本**随主控实例**：主控被顶替时旧的去重记录不该拦住新主控
        # （那是另一个实例的事），因此每次绑定都新建一本。
        self.master = MasterController(
            master_ref,
            self,
            self.registry,
            self.catalog,
            self.topology,
            self.admission,
            profile=self.profile,
            policy=self.policy,
            ledger=RequestLedger(self.ledger_policy),
            session_check=self.session_check,
        )
        # 新绑定的主控先进入恢复中：它的存档还没读回来（12 落地后由恢复
        # 流程结束这一步）。本次没有存档可读，因此在开放业务时立即完成。
        self.master.begin_recovery()
        self._notify(RuntimeChange.MASTER_BOUND)
        return True

    def _drop_master(self, detail):
        # type: (str) -> bool
        self.master = None
        self._notify(RuntimeChange.MASTER_LOST, [BlockReason(Reason.MASTER_ABSENT, detail)])
        return True

    # -- 开关 ----------------------------------------------------------

    def is_open(self):
        # type: () -> bool
        return self._open

    def close_reason(self):
        # type: () -> str
        return self._close_reason

    def open_if_ready(self, snapshot=None):
        # type: (object) -> bool
        """校验通过才开放业务。

        「校验」是结构性的：分量已归属且不在重建中、控制器恰好一个、
        已经绑定主控。**服务是否可用不在这里判**——那要按命令逐条算
        （见主控准入门链），在这里判会把「某个功能还没实现」误报成
        「整个网络不能开」。
        """
        snapshot = snapshot if snapshot is not None else self.topology.snapshot()
        component = snapshot.components.get(self.component_id)
        reason = ""
        if component is None:
            reason = "分量不存在"
        elif component.pending:
            reason = "分量正在重建"
        elif not component.is_runnable():
            reason = "拓扑条件不满足（%s）" % (
                component.state_code() or component.controller_state,
            )
        elif self.master is None:
            reason = "没有主控接管"
        if reason:
            self.close(reason)
            return False
        self.master.complete_recovery()
        if self._open:
            return True
        self._open = True
        self._close_reason = ""
        self.stats["opened"] += 1
        self._notify(RuntimeChange.OPENED)
        return True

    def close(self, reason=""):
        # type: (str) -> bool
        """停止接受业务命令。可重复调用。"""
        self._close_reason = reason
        if not self._open:
            return False
        self._open = False
        self._notify(RuntimeChange.CLOSED, [BlockReason(Reason.REBUILDING, reason)] if reason else ())
        return True

    def retire(self):
        # type: () -> None
        """分量消失：卸载全部节点后停止发通知。"""
        self.close("运行时退役")
        for node_key in sorted(self._nodes, key=lambda key: key.as_key()):
            self.drop_node(node_key)
        self._notify(RuntimeChange.RETIRED)
        self._emit = _noop

    # -- 服务取用 ------------------------------------------------------

    def service(self, service_id, grant):
        # type: (str, AccessGrant) -> NetworkService
        """取用一项服务。**必须持有当前主控签发的许可。**

        这是「设备不得绕过主控制器直接改库存或推进订单」的结构性保证：
        服务不对外暴露，只有主控在准入链走完之后才会发出许可。
        """
        if not self._open:
            raise AccessDenied(
                "运行时尚未开放业务（%s）" % (self._close_reason or "未校验",)
            )
        if self.master is None:
            raise AccessDenied("没有主控，不能取用服务")
        if grant is None:
            raise AccessDenied("缺少主控签发的许可")
        if grant.controller_instance_id != self.master.instance_id:
            raise AccessDenied(
                "许可由 %s 签发，当前主控是 %s"
                % (grant.controller_instance_id, self.master.instance_id)
            )
        if grant.topology_version != self.topology.version:
            raise AccessDenied(
                "许可依据的图版本 %r 已过期（当前 %r）"
                % (grant.topology_version, self.topology.version)
            )
        if grant.service_id is not None and grant.service_id != service_id:
            raise AccessDenied(
                "许可对应服务 %r，请求的是 %r" % (grant.service_id, service_id)
            )
        if not grant.consume():
            raise AccessDenied("许可已被使用（request_id=%s）" % (grant.request_id,))
        return self.services.require(service_id)

    def service_blockers(self):
        # type: () -> tuple
        return service_blockers(self.services, self.profile, self.policy)

    def route_blocker(self, kind):
        # type: (str) -> object
        return route_blocker(self.services, kind)

    def power_state(self, node_key):
        # type: (object) -> object
        return power_state_of(self.services, node_key)

    def service_view(self):
        # type: () -> list
        return [
            {
                "service_id": service.service_id,
                "available": not service.is_stand_in,
                "stand_in": service.is_stand_in,
                "pending_task": service.pending_task,
            }
            for service in self.services.assembly_order()
        ]

    # -- 状态 ----------------------------------------------------------

    def blockers(self):
        # type: () -> tuple
        if self.master is not None:
            return self.master.blockers()
        out = component_blockers_by_id(self.topology, self.component_id)
        if self.topology.snapshot().components.get(self.component_id) is not None:
            out.append(BlockReason(Reason.MASTER_ABSENT, "该网络没有主控接管"))
        return tuple(out)

    def state(self):
        # type: () -> str
        return run_state_of(self.blockers())

    def device_status(self, ref):
        # type: (object) -> object
        """只读。分量不存在、无主控、断电、超载时同样可答。"""
        master = self.master
        component_id = self.topology.snapshot().component_id_of(ref.node_key)
        if component_id != self.component_id:
            # 调用方路由错了。宁可少说主控，也不要报出别人的主控身份。
            master = None
        return compose_device_status(
            ref,
            self.definitions,
            self.registry,
            self.catalog,
            self.topology,
            self.admission,
            services=self.services,
            profile=self.profile,
            policy=self.policy,
            master=master,
        )

    def status_view(self):
        # type: () -> dict
        blockers = self.blockers()
        state = self.state()
        return {
            "component_id": self.component_id,
            "run_state": state,
            "run_state_text": RunState.text_of(state),
            "open": self._open,
            "close_reason": self._close_reason,
            "topology_version": self.version,
            "master_instance_id": self.master.instance_id if self.master is not None else None,
            "members": len(self._nodes),
            "services": self.service_view(),
            "blockers": [reason.to_dict() for reason in blockers],
        }

    # -- 推进与存档 ----------------------------------------------------

    def tick(self, context=None, budget=64):
        # type: (dict, int) -> int
        """推进本运行时。

        主控的时钟**无论开不开都走**（去重账本的保留期按它计算）；
        服务只在开放业务时推进——网络不能工作时还让服务跑，等于让它们
        绕过运行条件自己动起来。

        消耗量逐服务钳位：`int(x or 0)` 原样收下负值会让**后续服务**的额度
        反向变大（额度是 `budget - spent`），而症状是「某个服务偶尔多做一堆
        事」，几乎不可能从现象倒推回原因。

        **帧号由 `context["frame"]` 判定，同一帧里被访问两次只推进一步。**
        公平轮转的余量轮会让同一个运行时在一帧里被访问两次；若无条件
        `advance_tick()`，主控时钟会走两倍，而去重账本的保留期是按它算的
        ——「两次调用算两帧」会把保留窗口压缩一半，重复请求可能提前被遗忘。
        `stats["ticks"]` 同理，它记的是帧数而不是调用次数。
        没有 `frame`（直接调用、离线单测）时按每次调用算一帧，与引入轮转前一致。
        """
        context = context or {}
        frame = context.get("frame")
        if frame is None or frame != self._frame_marker:
            self._frame_marker = frame
            self.tick_count += 1
            self.stats["ticks"] += 1
            if self.master is not None:
                self.master.advance_tick()
        if not self._open:
            return 0
        spent = 0
        for service in self.services.assembly_order():
            remaining = budget - spent
            if remaining <= 0:
                break
            cost = int(service.tick(context, remaining) or 0)
            if cost < 0:
                cost = 0
                self.stats["negative_costs"] += 1
            elif cost > remaining:
                cost = remaining
                self.stats["budget_overruns"] += 1
            spent += cost
        return spent

    def wake_node(self, node_key):
        # type: (object) -> int
        """把唤醒信号转给各服务，返回处理了它的服务数。

        **不检查许可。** 唤醒不是命令：它与 `add_node` / `note_domain_change`
        同属内部信号路径，不产生任何业务副作用，因此不违反「设备不得绕过
        主控改库存」。

        **但检查 `_open`。** 理由与 `tick()` 里那道门一样：网络不能工作时
        服务不该自己动起来。唤醒会改服务状态（调度器会把退避打回初值），
        放它进去等于让关闭中的网络继续被外部脉冲驱动——而且那台网络重新
        开放的一刻，所有设备的退避都被重置过，会以最短间隔同时轮询。
        """
        if not self._open:
            self.stats["wakes_withheld"] += 1
            return 0
        hit = 0
        for service in self.services.assembly_order():
            if service.on_wake(node_key):
                hit += 1
        if hit:
            # 名字与管理器那层区分开：这里是**处理了它的服务数**（一个信号
            # 可能被多个服务处理），管理器那层数的是**送达的信号数**。
            # 三处报表用同一个名字却含义不同，对齐数字时会得到恒不相等的值。
            self.stats["wakes_handled"] += hit
        return hit

    def import_node_data(self, node_key, data):
        # type: (object, dict) -> None
        """注入某个节点的局部恢复数据。任务 12 的入口，本次只做记录。"""
        self._saved[node_key] = dict(data or {})

    def runtime_record_id(self):
        # type: () -> str
        """本运行时的存档 ID。

        **注意：分量编号在进程重启后会重新分配**（02 的口径），因此它现在
        还不是合格的持久身份。任务 12 需要把身份绑到位置集合或控制器实例上；
        在此之前这里写出的记录只能当作本进程内的检查点，不能作为「重进世界
        后还能找回同一个网络」的依据。写出来是为了让 12 有东西可以迁移，
        而不是假装身份问题已经解决。
        """
        return "runtime@%s" % (self.component_id,)

    def save_node_data(self):
        # type: () -> dict
        """导出本运行时及其成员的持久数据。

        节点数据按**位置**存档：位置是稳定的，而分量编号不是。
        """
        payload = {
            "component_id": self.component_id,
            "topology_version": self.version,
            "open": self._open,
            "master": self.master.save_node_data() if self.master is not None else None,
            "members": [key.as_key() for key in self.member_keys()],
        }
        if self.repository is None:
            return payload
        self.repository.write(
            Record(
                RUNTIME_RECORD_KIND,
                self.runtime_record_id(),
                payload,
                owner_id=self.master.instance_id if self.master is not None else None,
            )
        )
        for node_key in self.member_keys():
            node = self._nodes[node_key]
            data = {}
            for service in self.services.assembly_order():
                chunk = service.save_node_data(node)
                if chunk is not None:
                    data[service.service_id] = chunk
            if not data:
                continue
            self.repository.write(
                Record(NODE_RECORD_KIND, node_key.as_key(), {"data": data})
            )
        return payload

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report.update(
            {
                "component_id": self.component_id,
                "run_state": self.state(),
                "open": self._open,
                "close_reason": self._close_reason,
                "topology_version": self.version,
                "members": [key.as_key() for key in self.member_keys()],
                "master": self.master.diagnostics() if self.master is not None else None,
                "services": self.services.diagnostics(),
            }
        )
        return report

    # -- 通知 ----------------------------------------------------------

    def _notify(self, kind, reasons=()):
        # type: (str, tuple) -> RuntimeChange
        state = self.state()
        change = RuntimeChange(
            self.component_id,
            kind,
            self._reported_state,
            state,
            self.master.instance_id if self.master is not None else None,
            reasons,
            self.version,
        )
        self._reported_state = state
        self._emit(change)
        return change

    def __repr__(self):
        return "NetworkRuntime(%s, %s, %d 个成员)" % (
            self.component_id,
            self.state(),
            len(self._nodes),
        )


class RuntimeManager(object):
    """分量编号 → 运行时。**发布事件是唯一的迁移入口。**

    构造时订阅 `TopologyService`，因此**必须在 `AdmissionService` 之后构造**：
    准入服务在自己的 `__init__` 里订阅并立刻补算一次，运行时管理器晚一点
    订阅，才能保证每次发布时带宽判定已经按新版本刷新过。
    """

    def __init__(
        self,
        definitions,
        registry,
        catalog,
        topology,
        admission,
        profile=AssemblyProfile.DEV,
        factories=None,
        ledger_policy=None,
        session_check=None,
        repository=None,
        scheduler_policy=None,
    ):
        # type: (...) -> None
        self.definitions = definitions
        self.registry = registry
        self.catalog = catalog
        self.topology = topology
        self.admission = admission
        self.profile = AssemblyProfile.validate(profile)
        self.factories = factories if factories is not None else ServiceFactory()
        self.ledger_policy = ledger_policy
        self.session_check = session_check
        self.repository = repository

        self._runtimes = {}
        self._listeners = []
        self._buffer = None
        #: 调用方没给帧号时由本层补的帧序号（见 `tick()`）。
        self._frame_seq = 0
        #: 调度策略。**与各网络的 `SchedulerService` 是同一个对象**
        #: （由 `ServerBootstrap` 创建并分发）：轮转的轮数与保底额度既在这里
        #: 生效，也在这里被报告成「未定项」——两处若各存一份，
        #: `unresolved_report()` 报的就不是生效值了（全局约束 7 要防的正是这个）。
        self.scheduler_policy = (
            scheduler_policy if scheduler_policy is not None else SchedulerPolicy()
        )
        #: 跨网公平轮转的游标与统计（04-03）。此前这里什么都没有，
        #: 于是 `tick()` 是「按分量编号先到先得」——见 `tick()` 的说明。
        self._rotation = RotationCursor(
            max_passes=self.scheduler_policy.rotation_max_passes,
            min_share_floor=self.scheduler_policy.min_share_floor,
        )
        #: `NodeKey -> component_id`，供唤醒信号路由（04-02）。
        self._locator = NodeLocator()
        self.stats = {
            "publications": 0,
            "created": 0,
            "retired": 0,
            "handovers": 0,
            "changes_emitted": 0,
            "wakes_delivered": 0,
            #: 运行时（不是服务）自报负数消耗 / 超出额度的次数。
            #: 与 `NetworkRuntime.stats` 的同名键**分开**：一个是运行时违约，
            #: 一个是服务违约，混在一起就查不出是谁。
            "negative_costs": 0,
            "budget_overruns": 0,
        }
        # 订阅在最末：构造期间的任何发布都不该漏掉。
        topology.add_listener(self.on_publication)
        self.reconcile(topology.snapshot())

    # -- 迁移入口 ------------------------------------------------------

    def on_publication(self, publication):
        # type: (object) -> tuple
        """唯一入口。顺序**固定**：卸载 → 归属 → 挂载 → 校验后开放。"""
        self._buffer = []
        try:
            for notice in publication.migrations:
                self._apply_migration(notice)
            self.reconcile(publication.snapshot)
        finally:
            changes = tuple(self._buffer)
            self._buffer = None
        self.stats["publications"] += 1
        if changes:
            self.stats["changes_emitted"] += len(changes)
            self._emit(changes)
        return changes

    def reconcile(self, snapshot):
        # type: (object) -> int
        """把运行时集合对齐到一份快照。可重复调用（幂等）。"""
        for component_id in sorted(snapshot.components):
            if component_id not in self._runtimes:
                self._runtimes[component_id] = self._create_runtime(component_id)
        for component_id in sorted(snapshot.components):
            self._runtimes[component_id].sync_component(snapshot)
        for component_id in sorted(set(self._runtimes) - set(snapshot.components)):
            runtime = self._runtimes.pop(component_id)
            runtime.retire()
            self.stats["retired"] += 1
        for component_id in sorted(self._runtimes):
            self._runtimes[component_id].open_if_ready(snapshot)
        # 成员集合变了就得重建唤醒索引。放在最后：索引必须反映**本帧最终**
        # 的归属，否则本帧刚放下、又在本帧从 A 迁到 B 的设备会被唤醒到 A。
        self._locator.rebuild([self._runtimes[key] for key in sorted(self._runtimes)])
        return len(self._runtimes)

    def _apply_migration(self, notice):
        # type: (object) -> None
        """按 02 的迁移通知做**卸载**。挂载统一由 `sync_component` 负责。

        这样分工的理由：迁移通知只说「谁离开了哪里」，而挂载需要知道
        成员集合的最终形态；两件事分开做，就不会出现「先挂上去又被
        同一帧的另一条通知摘掉」的抖动。
        """
        if notice.kind == MigrationKind.DETACHED:
            runtime = self._runtimes.get(notice.from_component_id)
            if runtime is not None:
                runtime.drop_node(notice.node_key)
            return
        if notice.kind == MigrationKind.COMPONENT:
            if notice.to_component_id == notice.from_component_id:
                return
            old = self._runtimes.get(notice.from_component_id)
            if old is not None:
                old.drop_node(notice.node_key)
            if notice.from_component_id is not None and notice.to_component_id is not None:
                self.stats["handovers"] += 1
            return
        if notice.kind == MigrationKind.DOMAIN:
            runtime = self._runtimes.get(notice.to_component_id)
            if runtime is not None:
                runtime.note_domain_change(
                    notice.node_key, notice.from_domain_ids, notice.to_domain_ids
                )

    def _create_runtime(self, component_id):
        # type: (str) -> NetworkRuntime
        self.stats["created"] += 1
        runtime = NetworkRuntime(
            component_id,
            self.definitions,
            self.registry,
            self.catalog,
            self.topology,
            self.admission,
            factories=self.factories,
            profile=self.profile,
            policy=self.admission.policy,
            ledger_policy=self.ledger_policy,
            session_check=self.session_check,
            repository=self.repository,
            emit=self._buffer_change,
        )
        runtime._notify(RuntimeChange.CREATED)
        return runtime

    def _buffer_change(self, change):
        # type: (RuntimeChange) -> None
        if self._buffer is not None:
            self._buffer.append(change)

    # -- 查询 ----------------------------------------------------------

    def runtimes(self):
        # type: () -> list
        return [self._runtimes[key] for key in sorted(self._runtimes)]

    def runtime(self, component_id):
        # type: (str) -> NetworkRuntime
        return self._runtimes.get(component_id)

    def runtime_of(self, node_key):
        # type: (object) -> NetworkRuntime
        for component_id in sorted(self._runtimes):
            runtime = self._runtimes[component_id]
            if runtime.contains_node(node_key):
                return runtime
        return None

    def runtime_for(self, ref):
        # type: (object) -> NetworkRuntime
        """按设备实例找它所属的运行时。

        **只按位置找**：代次过期由主控在准入链里判（`DEVICE_GONE`），
        这里若也判一次，就会出现「运行时说没有、主控说代次过期」两套说法。
        """
        if ref is None:
            return None
        return self.runtime_of(ref.node_key)

    def device_status(self, ref):
        # type: (object) -> object
        """只读设备状态。没有运行时（分量不存在）时也给一份结论。"""
        runtime = self.runtime_for(ref)
        if runtime is not None:
            return runtime.device_status(ref)
        return compose_device_status(
            ref,
            self.definitions,
            self.registry,
            self.catalog,
            self.topology,
            self.admission,
            services=None,
            profile=self.profile,
            policy=self.admission.policy,
            master=None,
        )

    def add_listener(self, listener):
        # type: (object) -> None
        self._listeners.append(listener)

    def remove_listener(self, listener):
        # type: (object) -> None
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _emit(self, changes):
        # type: (tuple) -> None
        for listener in list(self._listeners):
            listener(changes)

    # -- 推进 ----------------------------------------------------------

    def tick(self, budget=64, context=None):
        # type: (int, dict) -> int
        """按预算推进各运行时。**公平轮转，不是先到先得。**

        此前这里是「按分量编号排序 + 全局预算递减」：排在字典序前面的分量
        把预算吃满，后面的每帧拿到 0，会被**永久饿死**——不是「慢」，是
        「不动」。而分量编号在进程重启后会重新分配，连「谁排在前面」都是
        碰运气。这是 04-03 要修的东西，分配算法在 `RotationCursor`。

        形状是两轮：**保底一轮**（按环序，每个运行时至多
        `share = max(保底, 预算 // n)`）+ **余量一轮**（没花完的额度按同一
        环序再分，直到预算用尽）。`remaining` 只按**实际消耗**递减，因此
        空闲网络不会浪费额度——它们没花掉的部分仍留给同帧的繁忙网络。

        能兑现的保证（不要吹成更强的）：`budget >= n` 时，每帧每个网络至少
        获得 1 单位额度；`budget < n` 时不保证人人每帧有份，但环序起点逐帧
        前移，连续 `n` 帧内人人当过一次首位。总消耗永不超过预算。
        """
        budget = int(budget)
        keys = sorted(self._runtimes)
        spent = 0
        if budget <= 0 or not keys:
            return 0
        # 帧号：调用方给了就用它的（`ServerBootstrap` 传引擎帧号），没给就
        # 由本层补一个——`ApplicationService.tick` 那条入口就没有帧号，而它
        # 同样会走余量轮、同样会把同一个运行时访问两次。**去重不能只在
        # bootstrap 这条路上成立**，否则换条入口主控时钟照样走两倍。
        if context is None or "frame" not in context:
            self._frame_seq += 1
            forwarded = dict(context or {})
            forwarded["frame"] = "manager#%d" % (self._frame_seq,)
        else:
            forwarded = context
        ring, share = RotationCursor.plan_frame(
            keys,
            budget,
            self._rotation.cursor(),
            self._rotation.min_share_floor,
        )
        allocations = []
        remaining = budget
        for pass_index in range(self._rotation.max_passes):
            progressed = False
            for index in ring:
                if remaining <= 0:
                    break
                component_id = keys[index]
                runtime = self._runtimes.get(component_id)
                if runtime is None:
                    # 同一帧内被退役（合网/拆网）。跳过即可，不是错误。
                    continue
                # 第一轮是额度上限，第二轮起是「剩下的都给你」。
                allowance = share if pass_index == 0 else remaining
                grant = min(allowance, remaining)
                if grant <= 0:
                    continue
                cost = int(runtime.tick(forwarded, grant) or 0)
                if cost < 0:
                    # 负的消耗量会反向放大同一帧里后续运行时的额度，
                    # 而且看上去什么错都没有——留痕，不静默吃下。
                    # 计数记在**管理器自己**的 stats 上：运行时内部那道钳位
                    # 也写同名键，两处混在一个 dict 里就分不清是哪一层拦下的。
                    cost = 0
                    self.stats["negative_costs"] += 1
                elif cost > grant:
                    cost = grant
                    self.stats["budget_overruns"] += 1
                allocations.append((component_id, grant, cost))
                if cost > 0:
                    progressed = True
                spent += cost
                remaining -= cost
            if not progressed:
                break
        self._rotation.note_frame(allocations)
        self._rotation.advance()
        return spent

    def deliver_wakes(self, signals):
        # type: (object) -> int
        """把唤醒信号按位置路由到所属运行时，返回送达条数。

        接受 `ServerEventBinder.take_wakes()` 的输出（`WakeSignal`），也接受
        裸的 `NodeKey`——离线测试里直接投递一个位置更省事。

        **未命中不是错误**：方块实体 tick 会在设备刚放下的那一帧到达，而那时
        运行时还没同步成员。丢一条唤醒不丢设备——注册与到期任务都不经过它，
        `NodeLocator` 里的 `unrouted` 记着数量。
        """
        delivered = 0
        for signal in signals:
            node_key = getattr(signal, "node_key", signal)
            component_id = self._locator.locate(node_key)
            if component_id is None:
                continue
            runtime = self._runtimes.get(component_id)
            if runtime is None:
                continue
            if runtime.wake_node(node_key):
                delivered += 1
        self.stats["wakes_delivered"] += delivered
        return delivered

    def rotation_view(self):
        # type: () -> dict
        report = self._rotation.describe()
        report["locator"] = self._locator.describe()
        return report

    def scheduler_view(self):
        # type: () -> dict
        """汇总各网络的调度指标（04-07），供诊断与任务 14 的验收读取。

        只统计**正式实现**的服务：替身槽位没有这些指标，把它算进来会让
        「队列长度 0」既可能是「真没有任务」也可能是「还没实现」——
        两者必须能分辨。
        """
        report = {
            "networks": 0,
            "queued": 0,
            "queued_worst": 0,
            "lanes": 0,
            "sleeping": 0,
            "executions": 0,
            "deferred_total": 0,
            "budget_spent": 0,
            #: **没有可汇总指标的**非替身服务数。必须与 0 分开：
            #: 「队列长度 0」既可能是真没有任务，也可能是那个实现没报，
            #: 两者在验收时是完全不同的结论（仓库口径：缺项不等于 0）。
            "unreported": 0,
        }
        for component_id in sorted(self._runtimes):
            service = self._runtimes[component_id].services.get(ServiceId.SCHEDULER)
            if service is None or service.is_stand_in:
                continue
            # 走**契约方法**而不是戳实现字段：`diagnostics()` 是
            # `NetworkService` 的一部分，而 `stats` 不是。
            detail = service.diagnostics()
            stats = detail.get("stats")
            if not isinstance(stats, dict):
                # 别的调度实现没有报指标。**不计入合计、也不折成 0**——
                # 折成 0 会让一整份全 0 的报表看起来像「一切正常」。
                report["unreported"] += 1
                continue
            report["networks"] += 1
            queued = int(detail.get("queued") or 0)
            report["queued"] += queued
            report["lanes"] += int(detail.get("lanes") or 0)
            report["sleeping"] += int(detail.get("sleeping") or 0)
            report["executions"] += int(stats.get("executions") or 0)
            report["deferred_total"] += int(stats.get("deferred_total") or 0)
            report["budget_spent"] += int(stats.get("budget_spent") or 0)
            if queued > report["queued_worst"]:
                report["queued_worst"] = queued
        return report

    # -- 诊断 ----------------------------------------------------------

    def status_view(self, component_id=None):
        # type: (str) -> dict
        if component_id is not None:
            runtime = self._runtimes.get(component_id)
            return runtime.status_view() if runtime is not None else {}
        return dict(
            (key, self._runtimes[key].status_view()) for key in sorted(self._runtimes)
        )

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report.update(
            {
                "count": len(self._runtimes),
                "rotation": self.rotation_view(),
                "runtimes": dict(
                    (key, self._runtimes[key].diagnostics())
                    for key in sorted(self._runtimes)
                ),
            }
        )
        return report
