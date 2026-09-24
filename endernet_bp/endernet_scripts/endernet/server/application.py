# coding=utf-8
"""应用服务：业务命令的**唯一**对外入口与只读查询门面。

对应任务 03-04。参照 AE2 的 `IStorageMonitorableAccessor`：外部拿到的不是
「某个控制器」，而是一个「能受理请求的口子」，由它决定这次请求落到谁身上。

**这一层不重复主控的准入判定。** 它只做三件主控做不了的事：

1. **路由**：把命令送到目标设备所属的那个运行时。主控管的是一个分量，
   不知道别的分量存不存在；只有运行时管理器同时看得见全部网络。
2. **无网络时的诚实回答**：目标设备不属于任何网络、或该网络还没有主控时，
   命令送不到主控手上。这时必须给出一条带原因的 `blocked`，
   而不是让调用方拿到 `None`。
3. **兜住异常**。主控内部按项目纪律**不吞异常**（业务实现抛错就是缺陷，
   该在自己的层次炸出来）；但这条链最终接着引擎事件回调，每帧调用数十次，
   异常一路抛回去会刷屏并让后续帧全废。因此在这里把异常转成
   `rejected INTERNAL`，缺陷仍然看得见，帧不会烂。

**只读路径没有门禁，也不经过主控**。没有主控、断电、超载、设备压根不属于
任何网络时，状态查询与必要的恢复诊断都要能答（03 验收末条：主控恢复之前
只允许状态视图与必要的恢复管理）。这不是降级，而是诊断本来就该在任何状态
下可读——否则设备离线时界面反而看不到原因。

**调试入口不能成为绕过口。** `debug_query` 只接受白名单里的只读操作，
未知操作一律拒绝；**不提供「强制写入」之类的后门**，因为那正是
「用调试入口绕过运行条件」的定义。要改库存只能走 `submit`。
"""

from ..common.protocol import (
    BlockReason,
    CommandKind,
    CommandResult,
    Reason,
    reason_code,
)
from .network.services import (
    AssemblyProfile,
    ServiceId,
    is_read_only,
    route_of,
)

#: `debug_query` 允许的只读操作。白名单而不是黑名单：新增查询要显式登记，
#: 而新增**写操作**永远不会因为「忘了加进黑名单」而漏出去。
DEBUG_OPS = frozenset(
    [
        #: 单台设备的总体状态（与 11-13 状态栏同一份数据）。
        "device",
        #: 一个网络或全部网络的运行状态。
        "network",
        #: 全部网络的概览。
        "snapshot",
        #: 一个网络的服务装配与替身清单。
        "services",
        #: 运行时管理器的计数与各运行时摘要。
        "runtimes",
        #: 一个网络当前主控的去重账本。
        "ledger",
        #: 本应用层的装配档位与统计。
        "assembly",
        #: 调度指标（04-07）：时钟、轮转、唤醒与队列合计。
        "scheduler",
    ]
)


class ApplicationService(object):
    """业务命令路由 + 只读查询门面。"""

    def __init__(
        self,
        runtimes,
        topology,
        admission,
        registry=None,
        profile=None,
        policy=None,
    ):
        # type: (object, object, object, object, str, object) -> None
        self.runtimes = runtimes
        self.topology = topology
        self.admission = admission
        self.registry = registry
        self.profile = profile if profile is not None else runtimes.profile
        self.policy = policy if policy is not None else admission.policy
        self.stats = {
            "submitted": 0,
            "routed": 0,
            "unrouted": 0,
            "read_only": 0,
            "errors": 0,
            "queries": 0,
            "queries_refused": 0,
        }

    # -- 业务命令 ------------------------------------------------------

    def submit(self, command):
        # type: (object) -> CommandResult
        """受理一条命令。**永不抛异常**（异常转成 `rejected INTERNAL`）。"""
        self.stats["submitted"] += 1
        route = route_of(command.kind)
        if route is None:
            return self._reject(
                command, Reason.BAD_PARAMETER, "未知命令类型: %r" % (command.kind,)
            )
        if command.device_ref is None:
            return self._reject(command, Reason.BAD_PARAMETER, "命令没有目标设备")

        read_only = is_read_only(command.kind)
        runtime = self.runtimes.runtime_for(command.device_ref)
        if runtime is None:
            self.stats["unrouted"] += 1
            return self._answer_without_runtime(command, read_only)
        if runtime.master is None:
            self.stats["unrouted"] += 1
            if read_only:
                self.stats["read_only"] += 1
                return CommandResult.ok(
                    command.request_id,
                    self._read_only_payload(runtime, command.kind),
                    self.topology.version,
                )
            return CommandResult.blocked(
                command.request_id, runtime.blockers(), self.topology.version
            )

        self.stats["routed"] += 1
        if read_only:
            self.stats["read_only"] += 1
        try:
            return runtime.master.submit(command)
        except Exception as error:
            # 缺陷在这里被挡下，但**不被掩埋**：原因码与异常类型都带上，
            # 详细堆栈由调用方（引擎事件路径）记进 tick_errors。
            self.stats["errors"] += 1
            return CommandResult.rejected(
                command.request_id,
                [
                    BlockReason(
                        Reason.INTERNAL,
                        "处理请求时抛出 %s: %s" % (type(error).__name__, error),
                    )
                ],
                self.topology.version,
            )

    def _answer_without_runtime(self, command, read_only):
        # type: (object, bool) -> CommandResult
        """目标设备不在任何网络里。

        只读命令照常回答——「它不属于任何网络」正是最需要能查到的一条结论。
        """
        if not read_only:
            return CommandResult.blocked(
                command.request_id,
                [
                    BlockReason(
                        Reason.MASTER_ABSENT,
                        "目标设备不属于任何网络，命令无法送达主控",
                    )
                ],
                self.topology.version,
            )
        self.stats["read_only"] += 1
        payload = {
            "component_id": None,
            "run_state": None,
            "note": "目标设备不属于任何网络（尚未归属，或已被拆除）",
            "device": self.device_status(command.device_ref).to_dict(),
        }
        if command.kind == CommandKind.RECOVERY:
            # 恢复管理要的是「现在发生了什么」，不是这台设备的状态。
            payload = {
                "component_id": None,
                "recovery": self.diagnostics(),
                "note": "目标设备不属于任何网络，没有可恢复的运行时",
            }
        return CommandResult.ok(command.request_id, payload, self.topology.version)

    def _read_only_payload(self, runtime, kind=None):
        # type: (object, str) -> dict
        """该网络还没有主控时的只读回答。"""
        if kind == CommandKind.RECOVERY:
            return {
                "component_id": runtime.component_id,
                "recovery": runtime.diagnostics(),
                "note": "该网络当前没有主控接管；这里只报告现状，不做任何改动",
            }
        payload = runtime.status_view()
        payload["note"] = "该网络当前没有主控接管，只能查看状态"
        return payload

    def _reject(self, command, code, detail):
        # type: (object, str, str) -> CommandResult
        return CommandResult.rejected(
            command.request_id,
            [BlockReason(code, detail)],
            self.topology.version,
        )

    # -- 只读查询 ------------------------------------------------------

    def device_status(self, ref, source=None):
        # type: (object, object) -> object
        """单台设备的总体状态（03-07）。任何状态下都可读。

        `source` 目前只用于将来的越权判定（谁能看谁的设备），本次不检查——
        给了参数却不检查，比不给参数更危险，因此在这里写明它还没有作用。
        """
        return self.runtimes.device_status(ref)

    def network_status(self, component_id=None):
        # type: (str) -> object
        """一个网络或全部网络的运行状态。"""
        return self.runtimes.status_view(component_id)

    def status_snapshot(self):
        # type: () -> dict
        """全部网络的概览。不含逐台设备状态（那要按需查）。"""
        views = self.runtimes.status_view()
        return {
            "profile": self.profile,
            "topology_version": self.topology.version,
            "admission_version": self.admission.version,
            "networks": views,
            "count": len(views),
            "runnable": sorted(
                component_id
                for component_id, view in views.items()
                if view.get("run_state") == "runnable"
            ),
        }

    def assembly_view(self):
        # type: () -> dict
        """装配档位、各槽位的替身清单与未定规则。"""
        problems = self.runtimes.factories.diagnostics()
        view = {
            "profile": self.profile,
            "slots": problems["slots"],
        }
        unresolved = self.policy.unresolved_report() if self.policy is not None else []
        view["unresolved"] = unresolved
        view["unresolved_count"] = len(unresolved)
        if self.profile == AssemblyProfile.PRODUCTION and unresolved:
            view["blocking"] = "正式装配档下准入策略未定案，业务命令会被拒绝"
        return view

    # -- 调试入口 ------------------------------------------------------

    def debug_query(self, op, request_id="debug", **kwargs):
        # type: (str, str, dict) -> CommandResult
        """只读诊断入口（03-07）。

        **只读，且只认白名单**。任何会改库存、推进订单或改装配的操作都不
        在这里，也不会因为「调试需要」被临时加上——主控恢复之前允许必要的
        恢复管理，指的是看，不是替它做决定。
        """
        if op not in DEBUG_OPS:
            self.stats["queries_refused"] += 1
            return CommandResult.rejected(
                request_id,
                [
                    BlockReason(
                        Reason.UNSUPPORTED,
                        "调试入口只提供只读操作，%r 不在其中（可用：%s）"
                        % (op, "、".join(sorted(DEBUG_OPS))),
                    )
                ],
                self.topology.version,
            )
        self.stats["queries"] += 1
        return CommandResult.ok(
            request_id, self._run_query(op, kwargs), self.topology.version
        )

    def _run_query(self, op, kwargs):
        # type: (str, dict) -> object
        if op == "device":
            ref = kwargs.get("ref")
            if ref is None:
                return {"error": "缺少 ref"}
            return self.device_status(ref).to_dict()
        if op == "network":
            return self.network_status(kwargs.get("component_id"))
        if op == "snapshot":
            return self.status_snapshot()
        if op == "services":
            runtime = self.runtimes.runtime(kwargs.get("component_id"))
            return runtime.service_view() if runtime is not None else []
        if op == "runtimes":
            return self.runtimes.diagnostics()
        if op == "ledger":
            runtime = self.runtimes.runtime(kwargs.get("component_id"))
            if runtime is None or runtime.master is None:
                return {"ledger": None, "note": "该网络当前没有主控"}
            return runtime.master.ledger.diagnostics()
        if op == "assembly":
            return self.assembly_view()
        if op == "scheduler":
            # 带 component_id 就看那一个网络的调度服务细节，否则给跨网合计。
            component_id = kwargs.get("component_id")
            if component_id is None:
                return self.runtimes.scheduler_view()
            runtime = self.runtimes.runtime(component_id)
            if runtime is None:
                return {"error": "没有这个网络", "component_id": component_id}
            service = runtime.services.get(ServiceId.SCHEDULER)
            if service is None:
                return {"error": "该网络没有装配调度服务"}
            return service.diagnostics()
        # 白名单与非白名单之间没有第三条路：走到这里说明 DEBUG_OPS 加了
        # 一项却没在 `_run_query` 里实现，那是个缺陷，不该静默返回空。
        raise NotImplementedError("调试操作 %r 已在白名单中但未实现" % (op,))

    # -- 推进与诊断 ----------------------------------------------------

    def tick(self, budget=64, context=None):
        # type: (int, dict) -> int
        return self.runtimes.tick(budget, context)

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report.update(
            {
                "profile": self.profile,
                "topology_version": self.topology.version,
                "admission_version": self.admission.version,
                "networks": len(self.runtimes.status_view()),
                "assembly": self.assembly_view(),
            }
        )
        return report

    def __repr__(self):
        return "ApplicationService(%s, %d 个网络)" % (
            self.profile,
            len(self.runtimes.status_view()),
        )


def summarize_result(result):
    # type: (CommandResult) -> str
    """一行摘要，供日志与 `execute_code` 使用。"""
    if result.is_applied():
        return "%s: %s" % (result.status, result.payload)
    codes = [reason_code(reason) for reason in result.reasons]
    return "%s: %s" % (result.status, ", ".join(codes) or "无原因")


#: `CommandKind.STATUS` 与 `CommandKind.RECOVERY` 是仅有的两种只读命令，
#: 供调用方判断「没有网络时能不能问」。
READ_ONLY_KINDS = frozenset([CommandKind.STATUS, CommandKind.RECOVERY])
