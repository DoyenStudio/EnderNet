# coding=utf-8
"""执行侧契约与尚未实现的桩（任务 07-03 / 07-05 / 07-06 / 07-07）。

**本模块里每一个「做不了」都明确说出来，没有一个假成功。** 这是
`script_architecture.md` 的原话：不为未来功能创建空实现，尚不支持的命令
明确拒绝，不能返回假成功；只有类文件、没有 bootstrap 与主控调用路径的实现，
不算脚本层完整交付。

四项各自卡在不同的地方，因此**拒绝理由也不一样**——玩家与下一个接手的人
要能一眼看出「还差什么」：

| 项 | 卡在哪 | 拒绝理由 |
| --- | --- | --- |
| 07-03 预设配方 | 网易引擎的工作台配方数据没有可用的读取入口（待核实） | `no_adapter` |
| 07-05 工厂样板执行 | 09 未实现：备料与派发归订单执行器 | `service_absent` |
| 07-06 装配仓执行 | 同上，且槽位租约的所有权明确属 09 | `service_absent` |
| 07-07 界面 | 11 未实现（样板终端）；**只读的那半边已经在 `store.py` 里可用** | —— |

## 07-05 的投料入口定为 `port_stage` 的薄封装

`capabilities.PatternProvider` 声明了 `push_inputs(execution_id, entries)`，
而 06 已经把成批备料做成了 STORAGE 命令 `port_stage`（`storage/service.py`
的 `_cmd_port_stage` → `devices/io.py` 的 `stage_batch`），并明确交接给 09。
**两个入口同时存在是危险的**：`io.py:1911` 记着 `stage_batch` 靠「主控账本 +
协调器账本」双重去重，另开一条不经过账本的投料路径就会让同一批原料备两遍，
而两侧各自的账本都显示正常。因此这里把方向钉死：

    `push_inputs` **必须**是 `port_stage` 的薄封装（经主控准入），
    不得自己碰容器、不得自己写中转位。

实现它属于 07-05，本次不做，接口按 `service_absent` 拒绝。

**那条路已经有一条活的实现，07-05 落地时应当复用它**：
`executor.OrderExecutor._port_stage()` 就是「构造一条设备发起的
`CommandKind.STORAGE` 命令（`op_id` 稳定、经注入的 `PortGateway` 交出去）」，
订单执行器现在正是靠它备料的。`push_inputs` 要做的是**同一件事**——
因此它应当把 `(ref, key, count, op_id)` 交给同一个门面，
而不是在 `PatternProviderPort` 里另写一份命令构造。多出来的那一份
一旦与这份在 `op_id` 的形状上不一致，去重就会失效，而两边的账本
各自都显示正常。

## 07-06 的槽位租约所有权属 09

任务 07 原文与 09 文档都写明：执行器所有权属于 09，装配仓**不独立领取或释放
同一槽位**。因此 `AssemblyChamberExecution` 只声明「我要接受什么样的租约」，
不接受任何东西，也不自己找槽位。
"""

from ...common.protocol import (
    BlockReason,
    Reason,
    TransferReceipt,
    TransferStatus,
)
from ...common.resource_key import ResourceKey  # noqa: F401 (仅用于 # type: 注释)

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
    - `push_inputs()`：归 07-05，是 `port_stage` 的薄封装，本次按
      `service_absent` 拒绝（09 未实现）。
    - `accept_returns()`：归 07-06/10 的回流入口，同样拒绝。
    """

    adapter_id = "endernet_pattern_provider"

    def __init__(self, state, store=None):
        # type: (object, object) -> None
        self.state = state
        #: 反查用（拿产物索引、按产物找候选）。**可以为 None**：只读诊断与
        #: 离线夹具常只关心 `describe()`，不该被迫构造一整个 store。
        self.store = store

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

    # -- 尚未实现的两半 ------------------------------------------------

    def push_inputs(self, execution_id, entries):
        # type: (str, object) -> TransferReceipt
        """把一批原料投给这台提供者。**07-05 未实现。**

        `requested` 取 `entries` 的件数（`entries` 是 `(ResourceKey, count)`
        或 `PatternStack` 的序列），**只用于回执的对照**，不代表搬动过。
        返回 `moved=0`，因此 `is_applied()` 为假，去重账本不会把它记成
        「已完成」而挡掉真正的重试。
        """
        return TransferReceipt(
            execution_id,
            _entry_total(entries),
            0,
            TransferStatus.UNSUPPORTED,
            Reason.SERVICE_ABSENT,
            None,
            holder=self.state.node_key.as_key(),
        )

    def accept_returns(self, execution_id, entries):
        # type: (str, object) -> TransferReceipt
        """收回一批产物/返还物。**归 10 的回流入口，尚未接线。**"""
        return TransferReceipt(
            execution_id,
            _entry_total(entries),
            0,
            TransferStatus.UNSUPPORTED,
            Reason.SERVICE_ABSENT,
            None,
            holder=self.state.node_key.as_key(),
        )

    def blocker(self):
        # type: () -> BlockReason
        return BlockReason(
            Reason.SERVICE_ABSENT,
            "样板提供者的执行半边尚未实现（07-05）：投料走 06 的 port_stage 命令、"
            "由订单执行器驱动——订单那条路已经通了"
            "（`crafting/executor.py` 的 `_port_stage`），本对象只提供只读的样板列表",
        )

    def describe(self):
        # type: () -> dict
        return {
            "adapter_id": self.adapter_id,
            "node_key": self.state.node_key.as_key(),
            "device_id": self.state.device_id,
            "patterns": self.pattern_count(),
            # **分开放**：投料与回流各自卡在自己的任务上，
            # 合成一句「不可用」会让诊断少掉一半信息。
            "push_inputs": False,
            "accept_returns": False,
            "pending_task": "07-05",
        }

    def __repr__(self):
        return "PatternProviderPort(%s @ %r)" % (
            self.state.device_id,
            self.state.node_key,
        )


def _entry_total(entries):
    # type: (object) -> int
    """数一批条目里有多少件。只用于回执的 `requested` 字段。

    容错而不是抛错：这是一个**只为了填回执**的读数，一条读不懂的条目
    不该让「拒绝」这件事本身失败。读不懂就按 0 计，而 `moved` 本来就是 0。

    **`(资源键, 数量)` 那一路要单独判，不能靠 `.count` 兜底。**
    元组**也有** `.count`（内置方法），`getattr(t, "count")` 拿到的是它、
    永远不是 `None`，于是那条兜底分支根本走不到——`[(key, 3)]` 会被数成 0，
    而回执里的 `requested` 是 09 与诊断唯一能看到「本来想投多少」的地方，
    报 0 会让重试与余量核对全部失去依据。
    """
    total = 0
    for entry in entries or ():
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            count = entry[1]
        else:
            count = getattr(entry, "count", None)
            if callable(count):
                # 别的对象碰巧也有个叫 `count` 的方法（元组就是）。
                count = None
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            total += count
    return total


# -- 07-06 装配仓执行 ------------------------------------------------------


class AssemblyChamberExecution(object):
    """装配仓的本地执行适配（07-06）。**未实现，且不做任何事。**

    目标形态（07-06 原文）：接受 09 分配的槽位租约与已备齐材料，校验配方后
    生成**唯一产出回执**；材料、产物、返还物都经 06 管理。

    **所有权归 09**：装配仓不独立领取或释放槽位（07 文档与 09 文档同一条）。
    因此本类**没有** `claim_slot()` 之类的入口——多一个入口就会被当成
    「装配仓自己也能领槽位」，而两处各领一次是重复占用的最常见来源。
    """

    pending_task = "07-06"

    def __init__(self):
        self.leases = {}

    def accepts_lease(self, lease):
        # type: (object) -> bool
        """本装配仓收不收这份租约。**未实现，一律返回 False。**

        返回 False 而不是抛错：租约是 09 按它的表分配的，一台设备说
        「我不接」是正常答复；抛错会把 09 的分配循环整个打断。
        """
        return False

    def execute(self, lease, materials):
        # type: (object, object) -> object
        """执行一次合成，产出唯一回执。**未实现，明确拒绝。**"""
        return BlockReason(
            Reason.SERVICE_ABSENT,
            "装配仓执行尚未实现（07-06）：需要 09 的槽位租约与备齐材料，"
            "而订单执行器（09）尚未落地",
        )

    def describe(self):
        # type: () -> dict
        return {
            "pending_task": self.pending_task,
            "available": False,
            "leases": len(self.leases),
            "note": "不自行领取或释放槽位：执行器所有权属于 09",
        }


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

