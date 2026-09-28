# coding=utf-8
"""样板提供者能力与预设配方契约（07）。

push_inputs 将单种工厂原料经 09 的 stage_inputs 交给 06 port_stage，
经过主控准入与去重；moved 表示进中转位，调用方不得重复累加重放回执。
它不创建订单或预留，多种原料由订单执行器逐种备料。

装配仓由 assembly.AssemblyChamberExecution 处理：09 持有逐实例槽位租约，
通过 STORAGE assembly_advance 把原料投入 0～8 槽，消费确认后产物进入
9 槽，再由 06 入库、10 分配。accept_returns 只能回收已经生成的实物，
不能凭 entries 增加库存。工厂外部产物仍走正常输入接口。

预设配方读取（07-03）仍明确拒绝；07-07 在本模块提供只读视图。
"""

from ...common.protocol import (
    BlockReason,
    Reason,
    TransferReceipt,
    TransferStatus,
)
from .assembly import AssemblyChamberExecution  # noqa: F401
from ...common.resource_key import ResourceKey

# -- 07-03 预设配方 --------------------------------------------------------


class PresetRecipeSource(object):
    """预设配方来源的鸭子类型（07-03 的契约，**尚未实现**）。

    目标形态：把一个**目标版本**（网易 ModSDK 的版本号）的工作台/熔炉等
    预设配方读成本模块能用的形状，并保留有序/无序规则与元数据。

    必须提供：

      - `recipes_for(output_key)` -> list   按产物查预设配方
      - `to_pattern(recipe, mode)` -> `PatternDefinition`
      - `version()` -> str                  数据来源的版本标识

    `to_pattern` **必须走 `PatternDefinition` 的构造器**，也就是必须过那一整套
    校验。任务 07 原文的「**不把客户端输入直接当作合法配方**」在这里的落点是：
    预设配方与玩家编辑走**同一条**校验路径，没有「引擎给的所以免检」这回事。
    """

    def recipes_for(self, output_key):
        # type: (ResourceKey) -> list
        raise NotImplementedError

    def to_pattern(self, recipe, mode):
        # type: (object, str) -> object
        raise NotImplementedError

    def version(self):
        # type: () -> str
        raise NotImplementedError


class NoPresetRecipeSource(object):
    """07-03 未接入时的**明确拒绝**实现。

    为什么拒绝而不是返回空表：空表与「这个产物没有预设配方」在调用方看来
    一模一样，于是「预设配方功能没做」会被读成「这个配方不存在」——
    玩家会去到处找配方，而真正的原因是这个功能整条没接。
    """

    source_id = "none"
    pending_task = "07-03"

    def recipes_for(self, output_key):
        # type: (ResourceKey) -> list
        """**明确拒绝，不返回空表。**

        返回空表看起来温和，但调用方拿到的 `[]` 与「查过了，这个产物确实
        没有预设配方」完全无法区分——于是「预设配方功能整条没接」会被读成
        「这个配方不存在」，让人去到处找配方。这条正是本类存在的理由，
        因此**查询入口也必须拒绝**，不能只有 `to_pattern` 拒绝。
        想知道为什么不能查，用 `blocker()`。
        """
        raise NotImplementedError(
            "预设配方适配尚未实现（07-03）：%s" % (self.blocker().detail,)
        )

    def to_pattern(self, recipe, mode):
        # type: (object, str) -> object
        raise NotImplementedError(
            "预设配方适配尚未实现（07-03）：%r" % (recipe,)
        )

    def version(self):
        # type: () -> str
        return ""

    def blocker(self):
        # type: () -> BlockReason
        return BlockReason(
            Reason.NO_ADAPTER,
            "预设配方适配尚未实现（07-03）：工作台等预设配方目前没有可用的"
            "读取入口，样板只能由玩家自行编辑",
        )

    def describe(self):
        # type: () -> dict
        return {
            "source_id": self.source_id,
            "available": False,
            "pending_task": self.pending_task,
            "preset_available": False,
        }


# -- 07-05 / 07-06 提供者的能力对象 ----------------------------------------


class PatternProviderPort(object):
    """一台样板提供者的**实际能力对象**（`capabilities.PatternProvider` 契约）。

    架构规划 §4 要求「明确实际能力对象在哪里构造、保存和取得，**不能只返回
    标签**」：每个 `PatternProviderState` 在构造时建一个本对象并注册进它的
    `CapabilityRegistry`，此后只取用不重建（与 `PortItemPort` 同一分工）。

    三个方法的分工：

    - `list_patterns()`：**这一半是能用的**——读的是 `store.py` 已经解出来的
      样板，08 的规划器要的就是它。返回的是 `patterns.py` 的纯数据对象，
      **不含任何引擎对象**（08 文档：「不向客户端暴露引擎对象」）。
    - `push_inputs()`：**07-05 已落地**，是 `port_stage` 的薄封装（见模块
      docstring）。它自己不做任何判定与搬运，把活交给 09 的服务门面。
    - `accept_returns()`：回收装配仓 9 槽已生成的实物，经 06 入库后交 10。
    """

    adapter_id = "endernet_pattern_provider"

    def __init__(self, state, store=None, factory_of=None):
        # type: (object, object, object) -> None
        self.state = state
        #: 反查用（拿产物索引、按产物找候选）。**可以为 None**：只读诊断与
        #: 离线夹具常只关心 `describe()`，不该被迫构造一整个 store。
        self.store = store
        #: `node_key -> FactoryService | None`（09 的工厂服务门面）。
        #: **是一个可调用对象而不是一个服务引用**：本对象在设备**放下**时
        #: 就建好了，而那时它所在网络的运行时可能还没装配（甚至还不存在），
        #: 设备也可能此后被拆下装到别的网络上。按位置现取，才不会拿着一个
        #: 属于旧网络的引用去备料。**可以为 None**，此时 `push_inputs`
        #: 明确拒绝（`service_not_ready`），而不是假装投了。
        self.factory_of = factory_of

    # -- 可用的一半 ----------------------------------------------------

    def list_patterns(self):
        # type: () -> list
        """本提供者当前可派的样板。

        **只列能派的**：格式过旧（`is_dispatchable()` 为假）的样板仍然在
        `state.entries` 里（人要看得见才知道该重做），但不出现在这里——
        列出来就会被 08 排进计划，然后卡在派发那一步。
        """
        return [entry.decoded.definition for entry in self.state.usable_entries()]

    def pattern_count(self):
        # type: () -> int
        return len(self.state.usable_entries())

    # -- 投料与实物回流-------------------------------

    def push_inputs(self, execution_id, entries):
        # type: (str, object) -> TransferReceipt
        """把一批原料备到这台提供者的**中转位**上（07-05）。

        `entries` 是 `(ResourceKey, count)` 或 `PatternStack` 一类的序列。
        `requested` 取整批的件数；`moved` 是**真的搬进去了多少**——
        注意它指的是**中转位**，不是机器（见模块 docstring）。

        **一次只收一种资源。** 样板口的批次槽只有一格（`BatchState` 是单个
        字段），`dispatch_template` 又只按 `batch.resource_key` 投料，
        因此投两种资源会有一种既不在批次记录里、也永远不会被投出去——
        **账外资产**。这里直接按 `unsupported` 拒绝并说清原因，而不是
        悄悄只投第一种。

        **绝不吞异常、绝不返回 `None`。** 「投不了」永远是一条说得清理由的
        回执：`moved=0` 让 `is_applied()` 为假，去重账本就不会把它记成
        「已完成」而挡掉真正的重试（`RequestLedger` 只收 `is_applied()` 的结果）。
        """
        requested = _entry_total(entries)
        groups, orphan = _group_entries(entries)
        if requested <= 0 or orphan > 0:
            # 读不出「投什么、投多少」就投不了。**不能只投读懂的那一部分**：
            # 那会让调用方以为整批都进去了，而余下的原料没有任何记录。
            return self._refuse(execution_id, requested, Reason.BAD_PARAMETER)
        if len(groups) > 1:
            # 一次投多种：批次槽只有一格，第二种既进不了批次记录、
            # 也永远不会被 `dispatch_template` 投出去。要多种原料请走订单
            # 执行器，它按步骤逐种备料。
            return self._refuse(execution_id, requested, Reason.UNSUPPORTED)
        key, count = groups[0]
        if self.store is None or self.factory_of is None:
            # 入口在，只是这一份装配没把工厂服务接进来（离线夹具、
            # 或装配层漏了 `factory_of`）。与「这个功能没做」是两回事。
            return self._refuse(
                execution_id, requested, Reason.SERVICE_NOT_READY
            )
        factory = self.factory_of(self.state.node_key)
        if factory is None:
            # 这台设备现在不属于任何网络，或者那张网络没有工厂服务。
            return self._refuse(execution_id, requested, Reason.MASTER_ABSENT)
        # **`op_id` 必须含数量。** 只按 `(execution_id, 资源)` 派生的话，
        # 「同一次执行、改了量再投一次」会命中去重账本的重放，拿回上一次
        # 那份回执——数字对不上，而两边都不觉得自己错。
        op_id = "push/%s/%s#%d" % (execution_id, key.canonical(), count)
        result = factory.stage_inputs(self.state.node_key, key, count, op_id)
        if result is None:
            # 命令网关不可用（没有主控 / 网络没开放），与 `_port_stage` 的
            # 约定一致：什么都没发出去。
            return self._refuse(execution_id, requested, Reason.MASTER_ABSENT)
        return self._receipt_from(execution_id, requested, key, result)

    def _receipt_from(self, execution_id, requested, key, result):
        # type: (str, int, object, object) -> TransferReceipt
        """把 09 的 `CommandResult` 翻译成回执。

        **三种载荷分别处理，而且不按数量反推状态。** 06 给的状态字照抄；
        只有「自己那一批的现成读数」这一种没有状态字，才由 `staged` 推。
        **不能统一按 `moved > 0` 推**——`uncertain` 的回执里 `moved` 完全
        可能大于 0（动作已经发出去了、只是没得到确认），按「搬动了多少」
        推就会在**不确定**上报出一个**假成功**，而 `TransferStatus.UNCERTAIN`
        的整条纪律正是「不得据此重发整批」。
        """
        payload = result.payload or {}
        replayed = Reason.DUPLICATE_REQUEST in result.reason_codes()
        receipt = payload.get("receipt")
        batch = payload.get("batch")
        if isinstance(receipt, dict):
            status = _RECEIPT_STATUS.get(receipt.get("status"))
            if status is None:
                # 认不出的状态字：**不猜**。宁可报「什么都没动」，
                # 也不能把一个未知状态当成完成。
                return self._refuse(
                    execution_id, requested,
                    result.reason_codes()[0] if result.reason_codes()
                    else Reason.UNSUPPORTED,
                    key,
                )
            moved = _as_count(receipt.get("moved"))
            reason = receipt.get("reason") or ""
            holder = receipt.get("holder") or ""
        elif isinstance(batch, dict):
            # 批次槽里**现成的**读数（自己那一批已经备过了，本次没发命令）。
            moved = min(_as_count(batch.get("staged")), requested)
            if moved >= requested:
                status = TransferStatus.COMPLETE
            elif moved > 0:
                status = TransferStatus.PARTIAL
            else:
                # 自家那一批一件都没备进去。这不是成功，也不是「正在投」。
                status = TransferStatus.REFUSED
            reason = ""
            holder = self._holder()
        else:
            # 没有回执也没有批次读数：09 在**动手之前**就拦下了这一次
            # （口不是样板口、被别家占着、装配仓需要订单租约……）。
            # 理由原样透传，一条都不吞——`moved=0` 已经诚实了，
            # 理由再丢了就没法查。
            codes = result.reason_codes()
            return self._refuse(
                execution_id, requested,
                codes[0] if codes else Reason.UNSUPPORTED,
                key,
            )
        if replayed and not reason:
            # **重放要标出来。** 见模块 docstring 第三条：重放的回执里
            # `moved` 是**那一次**搬的量，本次一件都没动。
            reason = Reason.DUPLICATE_REQUEST
        return TransferReceipt(
            execution_id,
            requested,
            moved,
            status,
            reason,
            key,
            holder=holder,
        )

    def _refuse(self, execution_id, requested, reason, key=None):
        # type: (str, int, str, object) -> TransferReceipt
        """一条说得清理由的拒绝。`moved=0`，因此 `is_applied()` 为假，
        去重账本不会把它记成「已完成」而挡掉真正的重试。

        **回执里能查的只有 `reason`**：`TransferReceipt` 没有「说明」这个
        字段，它是 06 的契约，加字段要动 `protocol.py` 并过那边的验收。
        因此每一条拒绝都必须给出**准确**的码，不能一律 `unsupported`——
        码选错了，调用方与诊断都会往错的方向查。措辞写在各自的调用点上。
        """
        return TransferReceipt(
            execution_id,
            requested,
            0,
            TransferStatus.REFUSED,
            reason,
            key,
            holder=self._holder(),
        )

    def _holder(self):
        # type: () -> str
        """本提供者的位置描述。**必须是字符串**——契约如此，空串意为「说不清」。

        这里曾经写成 `self.state.node_key.as_key()`（一个元组）：
        `TransferReceipt` 不校验 `holder` 的类型，因此它一路躺到诊断里，
        而按字符串读它的地方只会看到 `('%s' 之类)` 这种东西。
        """
        return "%s" % (self.state.node_key.as_key(),)

    def accept_returns(self, execution_id, entries):
        # type: (str, object) -> TransferReceipt
        """回收本次装配已在 9 槽中的实物；entries 不授予生成物品的权限。"""
        requested = _entry_total(entries)
        groups, orphan = _group_entries(entries)
        if requested <= 0 or orphan or len(groups) != 1:
            return self._refuse(execution_id, requested, Reason.BAD_PARAMETER)
        if self.state.device_id != "endernet:assemble_chamber":
            return self._refuse(execution_id, requested, Reason.UNSUPPORTED)
        if self.factory_of is None:
            return self._refuse(execution_id, requested, Reason.SERVICE_NOT_READY)
        factory = self.factory_of(self.state.node_key)
        if factory is None:
            return self._refuse(execution_id, requested, Reason.MASTER_ABSENT)
        key, count = groups[0]
        for order in factory.orders.live():
            instance = order.inflight.get(execution_id)
            if instance is None or instance.assembly is None:
                continue
            output = instance.assembly.get("output")
            if (instance.step.node_key != self.state.node_key.as_key()
                    or instance.step.generation != self.state.ref.generation
                    or not output or ResourceKey.from_dict(output["key"]) != key
                    or output["count"] != count):
                return self._refuse(execution_id, requested, Reason.BAD_PARAMETER)
            result = factory.executor._assembly_command(order, instance)
            moved = int((result.payload or {}).get("moved", 0))
            codes = result.reason_codes()
            status = (TransferStatus.COMPLETE if moved == requested else
                      TransferStatus.PARTIAL if moved else TransferStatus.REFUSED)
            if Reason.CONTAINER_UNCERTAIN in codes:
                status = TransferStatus.UNCERTAIN
            return TransferReceipt(execution_id, requested, moved, status,
                                   codes[0] if codes else "", key, holder=self._holder())
        return TransferReceipt(
            execution_id,
            requested,
            0,
            TransferStatus.REFUSED,
            Reason.BAD_PARAMETER,
            None,
            holder=self._holder(),
        )

    def blocker(self):
        # type: () -> object
        """功能已接入；每次请求分别校验设备类型、订单与实物。"""
        return None

    def describe(self):
        # type: () -> dict
        return {
            "adapter_id": self.adapter_id,
            "node_key": self.state.node_key.as_key(),
            "device_id": self.state.device_id,
            "patterns": self.pattern_count(),
            # **分开放**：投料与回流各自卡在自己的任务上，
            # 合成一句「不可用」会让诊断少掉一半信息。
            "push_inputs": True,
            "accept_returns": self.state.device_id == "endernet:assemble_chamber",
            "pending_task": "",
        }

    def __repr__(self):
        return "PatternProviderPort(%s @ %r)" % (
            self.state.device_id,
            self.state.node_key,
        )


#: 06 的回执状态 -> 本模块的状态。**恒等映射，不是按数量推断。**
#: 见 `PatternProviderPort._receipt_from` 的说明。
#: `refused` / `unsupported` 都归到 `REFUSED`：本模块只有这一个「什么都没动」
#: 的状态，而两者的区别在 `reason` 里（06 的码原样保留）。
_RECEIPT_STATUS = {
    TransferStatus.COMPLETE: TransferStatus.COMPLETE,
    TransferStatus.PARTIAL: TransferStatus.PARTIAL,
    TransferStatus.UNCERTAIN: TransferStatus.UNCERTAIN,
    TransferStatus.REFUSED: TransferStatus.REFUSED,
    TransferStatus.UNSUPPORTED: TransferStatus.REFUSED,
}

#: `(资源键, 数量)` 之外还认这两个属性名的堆叠对象（`patterns.PatternStack`）。
_STACK_ATTRS = ("key", "resource_key")


def _as_count(value):
    # type: (object) -> int
    """把一个读数变成非负整数。读不出或为负一律按 0——`moved` 不能凭空变大。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    value = int(value)
    return value if value > 0 else 0


def _iter_entries(entries):
    # type: (object) -> list
    """把一批条目拆成 `(资源键或 None, 件数)`。只收**正整数**件数。

    **`(资源键, 数量)` 那一路要单独判，不能靠 `.count` 兜底。**
    元组**也有** `.count`（内置方法），`getattr(t, "count")` 拿到的是它、
    永远不是 `None`，于是那条兜底分支根本走不到——`[(key, 3)]` 会被数成 0。
    （字符串同样有 `.count`，所以 `"junk"` 那条路也必须靠 `callable` 挡掉。）
    """
    out = []
    for entry in entries or ():
        if isinstance(entry, (list, tuple)):
            if len(entry) != 2:
                continue
            key, count = entry[0], entry[1]
        else:
            count = getattr(entry, "count", None)
            if count is None or callable(count):
                # 别的对象碰巧也有个叫 `count` 的方法（元组、字符串就是）。
                continue
            key = None
            for attr in _STACK_ATTRS:
                key = getattr(entry, attr, None)
                if key is not None:
                    break
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            continue
        out.append((key, count))
    return out


def _entry_total(entries):
    # type: (object) -> int
    """数一批条目里有多少件。用于回执的 `requested` 字段。

    容错而不是抛错：这是一个**为了填回执**的读数，一条读不懂的条目不该让
    「拒绝」这件事本身失败。读不懂就按 0 计。

    **它只回答「本来想投多少」，不回答「投什么」**——「投什么」由
    `_group_entries` 回答，两者必须用同一遍拆分（`_iter_entries`），
    否则 `requested` 与实际要投的量会对不上，而回执里没有任何东西
    能暴露这个不一致。
    """
    total = 0
    for _key, count in _iter_entries(entries):
        total += count
    return total


def _group_entries(entries):
    # type: (object) -> tuple
    """按资源键归并条目。返回 `(groups, orphan)`。

    `groups` 是 `[(key, 件数), ...]`，**按首次出现排序**（顺序稳定，
    拒绝文案里的资源名不会每次不一样）；`orphan` 是**数得出件数、
    却读不出资源键**的那些件数之和。

    `orphan > 0` 必须被当成错误，不能当没看见：那几件原料既进不了批次
    记录、也永远不会被投出去，是标准的**账外资产**。
    """
    groups = {}
    order = []
    orphan = 0
    for entry_key, count in _iter_entries(entries):
        canonical = None
        if entry_key is not None and hasattr(entry_key, "canonical"):
            canonical = entry_key.canonical()
        if canonical is None:
            orphan += count
            continue
        row = groups.get(canonical)
        if row is None:
            row = [entry_key, 0]
            groups[canonical] = row
            order.append(row)
        row[1] += count
    return order, orphan


# -- 07-06 装配仓执行 ------------------------------------------------------


# -- 07-07 只读视图 --------------------------------------------------------


def editable_view(state, policy=None):
    # type: (object, object) -> dict
    """给样板终端（11）看的**可编辑结构 + 验证错误 + 概要**（07-07）。

    **这是只读的**：真正写入走 `PatternStore.install` / `uninstall` /
    `clear_slot`，而那三个入口本次也只有领域层调用方——11 的样板终端落地时
    按「取放必须经过服务端交接」接上，本次**不开命令路径**，因为绕开主控的
    写入口（哪怕是调试用的）就是全局约束 2 说的那种旁路。

    错误分开三处报，因为玩家要做的事完全不同：

    | 字段 | 含义 | 玩家要做什么 |
    | --- | --- | --- |
    | `invalid_slots` | 槽里的东西解不成样板 | 把那件物品拿出来 |
    | `stale_slots` | 是样板，但格式过旧，不可派发 | 重新编辑并保存一次 |
    | `unreadable` | 读不到容器 | 靠近一点让区块加载 |
    """
    entries = []
    stale = []
    for slot in sorted(state.entries):
        entry = state.entries[slot]
        row = entry.decoded.definition.summary()
        row["slot"] = slot
        row["digest"] = entry.digest()
        row["format"] = entry.decoded.format
        row["dispatchable"] = entry.is_dispatchable()
        if not row["dispatchable"]:
            stale.append(slot)
        entries.append(row)
    return {
        "node_key": state.node_key.as_key(),
        "device_id": state.device_id,
        "loaded": state.loaded,
        "slot_count": state.slot_count,
        "patterns": entries,
        "invalid_slots": [
            {"slot": slot, "error": dict(state.invalid[slot])}
            for slot in sorted(state.invalid)
        ],
        "stale_slots": stale,
        "unreadable": state.unreadable,
        # 可编辑结构的**形状**（字段名与含义），供 11 渲染表单。
        # 不在这里塞一份完整 JSON Schema：那不是本项目的结构，
        # 而是一句「有哪些字段」，写清楚即可。
        "editable": {
            "mode": sorted(_modes()),
            "inputs": "条目列表，每条 {资源键, 数量, 槽位(可空), 不限量(可空)}",
            "outputs": "同 inputs，槽位必须为空",
            "returns": "同 inputs，槽位必须为空",
            "ordered": "合成样板可选；有序时每条输入都要给 0..8 的槽位",
        },
        "policy": policy.describe() if policy is not None else None,
    }


def _modes():
    # type: () -> frozenset
    """模式词汇。**延迟导入**：本模块只在 07-07 的视图里用到它，
    而那边是一个纯展示函数，不值得让整个模块在导入期就绑上 `patterns`。"""
    from . import patterns

    return patterns.MODES


def candidate_view(candidates):
    # type: (list) -> list
    """把 `PatternStore.candidates()` 的结果转成给 08 看的只读形状。

    **这是 07-07 交给 08 的那份契约的实体**（「只读配方索引与可用提供者」），
    因此它住在样板层而不是门面里：形状改了要改这里，而不是某个调用点。

    **只有顺序，没有优先级**——顺序是稳定的（见 `store.candidates` 的说明），
    但「哪个更优」是 08-04 的规则，这里不替它决定。
    `blocker` 也一并给出去：08 要能分辨「没有候选」与「候选都离线了」。
    """
    out = []
    for ordinal, item in enumerate(candidates):
        blocker = item.get("blocker")
        out.append(
            {
                "ordinal": ordinal,
                "node_key": item["provider"].node_key.as_key(),
                "device_id": item["provider"].device_id,
                "slot": item["entry"].slot,
                "digest": item["entry"].digest(),
                "mode": item["entry"].decoded.definition.mode,
                "summary": item["entry"].decoded.definition.summary(),
                "available": bool(item.get("available")),
                "blocker": blocker.to_dict() if blocker is not None else None,
            }
        )
    return out

