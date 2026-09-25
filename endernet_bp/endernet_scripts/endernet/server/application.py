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
        #: 能源判定与策略（04-04）：各网络有电没电，以及替身口径忽略了什么。
        "energy",
        #: 接口设备的状态（06）：角色、朝向、配置、缓冲与待核对标记。
        #: **这一条存在的理由**是全局约束 9 与 03 验收末条：设备离线、网络
        #: 不能工作时仍要能查看诊断。接口的业务命令（`port_status` 等）走
        #: `CommandKind.STORAGE`，必须过主控准入，因此在没有主控的网络上是
        #: 拿不到答案的——那正是这里另开一条只读路径的原因。
        "port",
        #: 样板提供者的状态与产物索引（07）：9 格里有什么、哪些解不出来、
        #: 哪些格式过旧、哪个产物有哪几台候选。同上，只为「坏掉时也能查」
        #: 而存在——样板索引根本不经过网络，没有主控时它照样是准的。
        "pattern",
        #: 用**真库存与真样板索引**跑一次合成规划（08），返回批次、缺料清单
        #: 与候选三元组。存在的理由有两个：
        #: 1. 09（订单与执行）尚未实现，那之前规划没有别的调用方，而仓库纪律
        #:    明写「只有类文件、没有装配与调用路径的实现不算完整交付」；
        #: 2. 规划是**只读计算**（不扣库存、不预留、不占槽位），因此它可以
        #:    安全地做成一条查询——这也是它敢放在只读白名单里的唯一理由。
        #:    订单的提交与派发属 09，不在这里。
        "plan",
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
        ports=None,
        patterns=None,
        planner=None,
    ):
        # type: (object, object, object, object, str, object, object, object, object) -> None
        self.runtimes = runtimes
        self.topology = topology
        self.admission = admission
        self.registry = registry
        self.profile = profile if profile is not None else runtimes.profile
        self.policy = policy if policy is not None else admission.policy
        #: 接口状态集合（06）。**只为只读诊断而接**：`debug_query("port")`
        #: 要在没有主控、网络不能工作时照样能答（全局约束 9）。
        #: 业务命令仍然只能走主控，绝不从这里绕过去。
        self.ports = ports
        #: 样板提供者状态与产物索引（07）。同上，只为 `debug_query("pattern")`。
        #: **样板槽的写入没有走这里**：编辑入口是领域层 API，调用方是任务 11
        #: 的样板终端；在门面上开一个写入口就是全局约束 2 说的那种旁路。
        self.patterns = patterns
        #: 合成规划门面（08）。只为 `debug_query("plan")`。
        #: **它连写库存的接口都没有**：规划是只读计算（08 文档的边界），
        #: 因此这里不存在「调试入口变成旁路」的可能——不是靠自律，
        #: 而是因为那个能力根本没被实现出来。
        self.planner = planner
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
        if op == "energy":
            # 与 `scheduler` 同形，且**同样只走契约方法 `diagnostics()`**：
            # 伸手要能源服务专有的字段，会让任何自定义能源实现（测试双件）
            # 把这个只读入口直接打崩。
            component_id = kwargs.get("component_id")
            if component_id is None:
                return self.runtimes.energy_view()
            runtime = self.runtimes.runtime(component_id)
            if runtime is None:
                return {"error": "没有这个网络", "component_id": component_id}
            service = runtime.services.get(ServiceId.ENERGY)
            if service is None:
                return {"error": "该网络没有装配能源服务"}
            return service.diagnostics()
        if op == "port":
            return self._run_port_query(kwargs)
        if op == "pattern":
            return self._run_pattern_query(kwargs)
        if op == "plan":
            return self._run_plan_query(kwargs)
        # 白名单与非白名单之间没有第三条路：走到这里说明 DEBUG_OPS 加了
        # 一项却没在 `_run_query` 里实现，那是个缺陷，不该静默返回空。
        raise NotImplementedError("调试操作 %r 已在白名单中但未实现" % (op,))

    def _run_port_query(self, kwargs):
        # type: (dict) -> dict
        """接口状态的**只读**视图。没有主控、网络不能工作时照样能答。

        `node_key` 给位置、`ref` 给实例（带代次），都没有时列全部。
        **这不是业务入口**：它只读 `PortStore`，一条命令都不会发出去，
        因此在网络坏掉时它给出的恰恰是最有用的那部分信息——
        「这台接口为什么不动」。
        """
        if self.ports is None:
            return {"error": "本装配没有接入接口状态集合"}
        node_key = kwargs.get("node_key")
        ref = kwargs.get("ref")
        if ref is not None:
            state = self.ports.get_by_ref(ref)
            if state is None:
                return {"error": "该代次的接口实例不存在", "ref": str(ref)}
            return {"port": state.to_dict(), "blocker": _blocker_dict(state)}
        if node_key is not None:
            state = self.ports.get(node_key)
            if state is None:
                return {"error": "该位置上没有接口实例", "node_key": str(node_key)}
            return {"port": state.to_dict(), "blocker": _blocker_dict(state)}
        return {
            "ports": [state.to_dict() for state in self.ports.states()],
            "diagnostics": self.ports.diagnostics(),
        }

    def _run_pattern_query(self, kwargs):
        # type: (dict) -> dict
        """样板层的**只读**视图（07）。

        `ref` 给实例（带代次）、`node_key` 给位置、`output` 给产物，都没有时
        列全部。与 `port` 同形：**这不是业务入口**，一条命令都不会发出去，
        也不改任何样板——想改要调领域层的 `install` / `uninstall`，
        而那三个入口的调用方是任务 11 的样板终端。

        没有主控时它给出的恰恰是最有用的那部分信息：这台设备的 9 格里
        到底有什么。样板索引根本不经过网络，因此它在断电、断网、主控
        被移除时**照样是准的**。
        """
        if self.patterns is None:
            return {"error": "本装配没有接入样板提供者状态集合"}
        ref = kwargs.get("ref")
        node_key = kwargs.get("node_key")
        output = kwargs.get("output")
        # **参数类型先判。** 传进来一个字符串（`node_key="0,7,64,0"`）时，
        # 下面 `node_key.as_key()` 会抛 `AttributeError`，而 `debug_query`
        # 没有兜底——调用方拿到的是一次崩溃而不是一条诊断。
        # 只读入口最该在「什么都坏掉」时能用，因此这里必须答得出来。
        if ref is not None and not hasattr(ref, "node_key"):
            return {"error": "ref 必须是设备实例引用（DeviceRef）", "ref": str(ref)}
        if node_key is not None and not hasattr(node_key, "as_key"):
            return {"error": "node_key 必须是位置（NodeKey）", "node_key": str(node_key)}
        if output is not None and not hasattr(output, "name"):
            return {"error": "output 必须是资源键（ResourceKey）", "output": str(output)}
        if ref is not None or node_key is not None:
            return self.patterns.view(node_key=node_key, ref=ref)
        if output is not None:
            # **视图的拼装在 `crafting/execution.py` 里，不在这里。**
            # 那份形状是 07-07 交给 08 的契约（「只读配方索引与可用提供者」），
            # 归属在样板层；在门面里再抄一份的话，两处迟早对不上，
            # 而症状是「08 拿到的字段与 07 文档写的不一样」。
            from .crafting.execution import candidate_view

            return {
                "output": output.name,
                "candidates": candidate_view(
                    self.patterns.candidates(output, include_unavailable=True)
                ),
            }
        return self.patterns.view()

    def _run_plan_query(self, kwargs):
        # type: (dict) -> dict
        """跑一次**只读**合成规划（08）。

        参数：

        - `target`：要算的资源键（`ResourceKey`，必填）。缺了回错误载荷——
          `debug_query` 会对每个白名单操作空参调一次，因此这里必须答得出来。
        - `amount`：目标数量（正整数，缺省 1）。
        - `component_id`：用哪个网络的库存做规划。给了就在该网络的存储服务上
          采一份库存快照；没给就是**空库存**（规划照跑，只是「什么都要现做」）。
        - `topology_version`：可选，覆盖快照里记的拓扑版本（诊断用）。

        **它不改任何状态。** 不扣库存、不预留、不占槽位、不建订单——规划器本来
        就没有这些能力。返回的「槽位/内存估计」明确标着 `authoritative: False`：
        08-06 要求估算值不得被读成「资源已预留」。

        没有主控、网络没电、设备离线时它照样能答，而且答的正是最有用的那部分：
        「按现在网络里真实有的东西与真实装着的样板，这个目标要几批、缺什么」。
        """
        if self.planner is None:
            return {"error": "本装配没有接入合成规划器"}
        target = kwargs.get("target")
        if target is None:
            return {"error": "缺少 target（资源键）"}
        if not hasattr(target, "canonical"):
            return {"error": "target 必须是资源键（ResourceKey）", "target": str(target)}
        amount = kwargs.get("amount", 1)
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            return {"error": "amount 必须是正整数", "amount": amount}

        component_id = kwargs.get("component_id")
        runtime = self.runtimes.runtime(component_id) if component_id else None
        if component_id and runtime is None:
            return {"error": "没有这个网络", "component_id": component_id}
        inventory, inventory_note = self._plan_inventory(runtime)
        topology_version = kwargs.get("topology_version", self.topology.version)
        patterns = self.planner.capture_patterns(self.patterns, topology_version)
        plan = self.planner.solve(target, amount, inventory, patterns)
        if plan is None:
            return {
                "error": "这次规划没有算完（超出工作量上限或已被取消）",
                "target": target.name,
                "amount": amount,
            }
        return {
            "target": target.name,
            "amount": amount,
            "component_id": component_id,
            "summary": plan.summary(),
            "plan": plan.to_dict(),
            "inventory": {
                "keys": len(inventory),
                "version": inventory.version,
                "note": inventory_note,
            },
            "patterns": {
                "outputs": len(patterns),
                "candidates": patterns.candidate_count(),
            },
            "policy": self.planner.policy.describe(),
            "notice": (
                "这是只读预览：计划的槽位与内存是**估计值**（不是已预留），"
                "订单的提交、预留与派发属任务 09，本入口一个都不做。"
            ),
        }

    def _plan_inventory(self, runtime):
        # type: (object) -> tuple
        """取规划要用的库存快照。返回 `(快照, 说明)`。

        走存储服务的**公开只读入口** `keys()`/`query()`（`InventorySnapshot.capture`
        里），不伸手读它的内部表——与 `debug_query("port")` 那一条同一条纪律。
        拿不到存储服务时给一份空快照并把原因写在说明里：**空库存不等于没有库存**，
        读的人要能分辨「这个网络什么都没有」与「我根本没读到」。
        """
        if runtime is None:
            return (
                self.planner.capture_inventory(None, self.topology.version),
                "未指定网络，按空库存规划（只算配方与依赖，不考虑现有库存）",
            )
        service = runtime.services.get(ServiceId.STORAGE)
        if service is None:
            return (
                self.planner.capture_inventory(None, self.topology.version),
                "该网络没有装配存储服务，按空库存规划",
            )
        capture = getattr(service, "keys", None)
        query = getattr(service, "query", None)
        if not callable(capture) or not callable(query):
            return (
                self.planner.capture_inventory(None, self.topology.version),
                "该网络的存储服务没有提供只读库存查询，按空库存规划",
            )
        return (
            self.planner.capture_inventory(service, self.topology.version),
            "按该网络当前真实库存（可用量 = 库存 - 他人预留）规划",
        )

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


def _blocker_dict(state):
    # type: (object) -> object
    """接口为什么不工作。措辞与界面共用一份（`devices/io.py::port_blocker`）。

    延迟导入：`application.py` 是命令门面，为了一个诊断字段把它和
    `devices/io.py` 绑死在导入期不划算。
    """
    from .devices.io import port_blocker

    blocker = port_blocker(state)
    return blocker.to_dict() if blocker is not None else None


#: `CommandKind.STATUS` 与 `CommandKind.RECOVERY` 是仅有的两种只读命令，
#: 供调用方判断「没有网络时能不能问」。
READ_ONLY_KINDS = frozenset([CommandKind.STATUS, CommandKind.RECOVERY])
