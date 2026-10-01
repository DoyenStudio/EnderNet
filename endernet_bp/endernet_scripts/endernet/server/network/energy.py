# coding=utf-8
"""网络能源服务：按定义表结算耗能，按供能设备判定供电。

对应任务 04 的能源半边里的**固定耗能结算**（04-04 的一部分）。参照 AE2 的
`EnergyService`：每个网络算一份供需账，主控凭它回答 `power_ok`。

## 这一层只做三件事

登记成员、把定义表里的供需数值加起来、给出一个三态判定。它**不做** RF 计量
（04-05/13-02）、**不管**断电暂停与逐步恢复（04-06）、**不发布**设备级能源
状态与变化通知（04-08）。

## 供能设备由 `supply` 认定，不新开能力名

能力 `energy_port` 的协议是 `accept_rf`（**拉**式：向外部机器要 RF 再折算），
而创造能量源是**推**式静态电源，两者不是同一个能力。`definition.supply` 已经
足以分类（`supply is not None` 就是供能设备），再挂一个能力名等于把同一件事
存两处，迟早分叉——本仓库为这种分叉付过代价（见 04 交付说明第七节第 3 条）。
因此本模块像一个 `AdmissionService` 读带宽那样**直接读定义表**。

## 判定的四条次序（顺序本身就是契约）

1. 网络里**一台供能设备都没有** → 由 `EnergyPolicy.no_source` 决定说「待确认」
   还是「确认没电」。这是 04 文档记着的待决问题，库默认取保守的 `PENDING`，
   DEV 档显式改成 `BROWNOUT`。
2. **有供能设备的速率算不出**（只接了能源接入口，RF→EN 折算属 R08）→ `None`。
   这一条必须排在「够不够」之前：供能总量这时只是**下界**，拿它去比大小会得出
   一个不成立的「没电」。
3. **已知供能 < 已知需求** → `False`。到这里供能总量是**精确的**（上一条已经
   排除未知供能），而需求总量至多是下界，因此「精确值 < 下界 ≤ 真需求」成立，
   结论确定——哪怕还有需求算不出。
4. 有需求算不出（定义没写 / 输入没接线）→ 由策略决定「待确认」还是忽略；
   都没有 → `True`。

**库默认下真实网络的判定几乎总是 `None`**：基带（每张网络都少不了）的耗能
blocks.md 没列（R10），内存条数量也还没接线；驱动器数量、处理器与装配仓
工作态已由组合根接入。这是如实回答「算不出需求」。其余输入接上之后**不需要改
这里的代码**，判定会自己变成 `True`。DEV 档要的是「现在就能跑起来」，
因此显式选择替身口径（`EnergyPolicy.dev_stand_in()`），并把被忽略的需求
逐个报在 `diagnostics()["ignored_demand"]` 里。

## 读取无副作用

`power_ok` 只读缓存：判定在成员或输入变化时就算好，读取不重算、不预留、不
消耗能源，也不依赖 `context["tick"]`。因此 `clock.advance(n); server.tick()`
仍可精确复现，诊断查询也不会因为「没有新的 IO 事务」而失去能源状态（04 接口
要求）。`tick()` 因此恒返回 0——本服务没有到期任务。
"""

from ...common.definitions import UNCONFIGURED
from ...common.protocol import Reason
from .services import NetworkService, ServiceId

#: 设备工作态。**只有这两个取值**，与 `EnergySpec.value_for_state` 一致。
DEVICE_IDLE = "idle"
DEVICE_WORK = "work"

DEVICE_STATES = (DEVICE_IDLE, DEVICE_WORK)

#: 一件需求算不出数值的两种原因。诊断里逐设备给出。分开是因为两者
#: **关闭路径完全不同**：一个要改 `plans/`（定义缺项），一个要接线
#: （运行期输入还没有来源）。
GAP_MISSING_SPEC = "missing_spec"
GAP_UNWIRED_INPUTS = "unwired_inputs"


class EnergyMember(object):
    """一台成员设备在能源账里的那一行。

    `state` / `unit_count` 取 `None` 表示**这个输入还没有来源**（无人告诉过
    本服务），而不是「等于 0」——两者的区别正是判定能不能算的依据：
    没有来源就是算不出需求，等于 0 才是真的不耗能。
    """

    __slots__ = ("node_key", "ref", "device_id", "supply", "demand", "gap", "state", "unit_count")

    def __init__(self, node_key, ref, device_id, supply, demand, gap, state, unit_count):
        # type: (object, object, str, object, object, str, object, object) -> None
        self.node_key = node_key
        self.ref = ref
        self.device_id = device_id
        #: 供能速率（EN/t）：int、UNCONFIGURED，或 None（该设备不供能）。
        self.supply = supply
        #: 需求（EN/t）：int 或 UNCONFIGURED。
        self.demand = demand
        #: `demand` 算不出时的原因（`GAP_*`），算得出时为空串。
        self.gap = gap
        self.state = state
        self.unit_count = unit_count

    def signature(self):
        # type: () -> tuple
        """要不要重算这一行的判据。代次参与：同位置换了方块就是换了台设备。"""
        return (
            self.device_id,
            self.ref.generation,
            self.supply,
            self.state,
            self.unit_count,
        )

    def is_supplier(self):
        # type: () -> bool
        return self.supply is not None

    def to_dict(self):
        # type: () -> dict
        return {
            "device_id": self.device_id,
            "generation": self.ref.generation,
            "supply": self._show(self.supply),
            "demand": self._show(self.demand),
            "gap": self.gap,
            "state": self.state,
            "unit_count": self.unit_count,
        }

    @staticmethod
    def _show(value):
        """诊断里的人读形式。`UNCONFIGURED` 原样写出来，不当成 0 也不当成 None。"""
        return "UNCONFIGURED" if value is UNCONFIGURED else value

    def __repr__(self):
        return "EnergyMember(%s @ %r)" % (self.device_id, self.node_key)


#: 尚未定案的策略字段。三段式的第一段（与 `BandwidthPolicy`/`SchedulerPolicy`
#: 同形），`unresolved_report()` 逐条报出来。
UNRESOLVED_FIELDS = (
    ("no_source", "R08", "网络里一台供能设备都没有时：报「待确认」还是「确认没电」"),
    ("missing_spec", "R10", "定义表未列耗能的设备（基带、总线接口、连接器）怎么算"),
    (
        "unwired_inputs",
        "R08",
        "耗能要由装置数量/工作态/负载推导，而这些运行期输入尚未接线",
    ),
)


class EnergyPolicy(object):
    """能源策略的配置点。

    **两个默认值都是保守的那一侧**，因为待定规则尚未定案（全局约束 7）。
    游戏内 DEV 档要的是「能看出没电、能跑起来」，因此由
    `EnergyPolicy.dev_stand_in()` 显式选择替身口径，而不是把替身值写进默认值——
    默认值一旦宽松，离线测试与诊断里就再也看不出「这条结论建立在替身上」。

    `assert_settled()` 供正式验收使用，**只在被显式调用时才报错**：开发与
    离线测试不应调用（与 `BandwidthPolicy`/`SchedulerPolicy` 同一条纪律）。
    """

    #: 一台供能设备都没有时：报「待确认」（保守，默认）。
    PENDING = "pending"
    #: 一台供能设备都没有时：报「确认没电」。
    BROWNOUT = "brownout"

    #: 有算不出的数值时：**忽略它**（替身口径，必须报出来）。
    IGNORE = "ignore"

    NO_SOURCE_MODES = frozenset([PENDING, BROWNOUT])
    GAP_MODES = frozenset([PENDING, IGNORE])

    def __init__(
        self,
        no_source=PENDING,
        missing_spec=PENDING,
        unwired_inputs=PENDING,
    ):
        # type: (str, str, str) -> None
        self.no_source = self._validate(no_source, self.NO_SOURCE_MODES, "no_source")
        self.missing_spec = self._validate(
            missing_spec, self.GAP_MODES, "missing_spec"
        )
        self.unwired_inputs = self._validate(
            unwired_inputs, self.GAP_MODES, "unwired_inputs"
        )

    @staticmethod
    def _validate(value, allowed, field):
        # type: (str, frozenset, str) -> str
        if value not in allowed:
            raise ValueError(
                "能源策略 %s 取值非法: %r（可用：%s）"
                % (field, value, "、".join(sorted(allowed)))
            )
        return value

    @classmethod
    def dev_stand_in(cls):
        # type: () -> EnergyPolicy
        """游戏内 DEV 档用的**替身口径**：有供能设备就算有电，没有就说没电。

        为什么需要它：基带的耗能 blocks.md 没列（R10），`per_unit` 的装置数量
        与工作态也还没接线，因此**任何**真实网络在保守口径下都只能报「待确认」，
        放了创造能量源也一样——那样「现在就能用的能量源」就不成立。这个预置把
        那三类算不出的需求按替身口径忽略掉，从而让「有电源 → 有电」这条链路
        现在就走得通。

        **代价必须被看见，不能被藏起来**：被忽略的需求逐个列在
        `diagnostics()["ignored_demand"]` 里，三个策略字段照旧进
        `unresolved_report()`，正式档照样拒绝。它不是「可以交付的口径」。
        """
        return cls(
            no_source=cls.BROWNOUT,
            missing_spec=cls.IGNORE,
            unwired_inputs=cls.IGNORE,
        )

    def gap_mode(self, gap):
        # type: (str) -> str
        """某类缺口对应的处置口径。"""
        if gap == GAP_MISSING_SPEC:
            return self.missing_spec
        if gap == GAP_UNWIRED_INPUTS:
            return self.unwired_inputs
        return self.PENDING

    def unresolved_report(self):
        # type: () -> list
        """仍是测试替身的策略字段。这是诊断，不是错误。"""
        return [
            {
                "field": field,
                "rule": rule,
                "value": getattr(self, field, None),
                "note": note,
            }
            for field, rule, note in UNRESOLVED_FIELDS
        ]

    def assert_settled(self):
        # type: () -> None
        """正式验收前的守卫：策略字段没有对应的决策就不算定案。

        调用它意味着「现在要宣布能源数值可以验收了」。
        开发与离线集成测试**不应**调用它。
        """
        raise AssertionError(
            "能源策略尚未定案，不能宣布数值已验收。待定项：\n  "
            + "\n  ".join(
                "%s（%s）：%s" % (field, rule, note)
                for field, rule, note in UNRESOLVED_FIELDS
            )
        )

    def describe(self):
        # type: () -> dict
        return {
            "no_source": self.no_source,
            "missing_spec": self.missing_spec,
            "unwired_inputs": self.unwired_inputs,
            "unresolved": self.unresolved_report(),
        }


class EnergyService(NetworkService):
    """能源服务。每个网络运行时各有一份实例。"""

    service_id = ServiceId.ENERGY
    #: 不依赖别的服务：供需账只读定义表与自己的成员表。
    requires = ()
    is_stand_in = False
    pending_task = ""

    def __init__(self, runtime=None, policy=None, inputs_of=None):
        # type: (object, object, object) -> None
        #: **裸构造是合法的**（`default_stand_ins()` 会用 `None` 构造每个槽位），
        #: 此时 `is_ready()` 为假、`power_ok()` 为 `None`——那是「装了但还没接上
        #: 运行时」，不是「没有需求」。
        self.runtime = runtime
        self.policy = policy if policy is not None else EnergyPolicy()
        # 组合根提供权威输入；重挂网络时重读，不能继承另一实例的缓存。
        self.inputs_of = inputs_of
        #: 位置 -> `EnergyMember`。**只含本网络的成员**。
        self._members = {}
        #: 判定缓存。成员或输入变化时立刻重算，读取只取它。
        self._ok = None
        self._reason_code = Reason.ENERGY_UNAVAILABLE
        self._totals = {
            "suppliers": 0,
            "supply_total": 0,
            "demand_total": 0,
            "unknown_supply": [],
            "missing_spec": [],
            "unwired_inputs": [],
        }
        self.stats = {
            "settles": 0,
            "added": 0,
            "removed": 0,
            "state_updates": 0,
            "refused": 0,
        }

    # -- NetworkService 入口 -------------------------------------------

    def attach(self, runtime):
        # type: (object) -> None
        self.runtime = runtime

    def detach(self):
        # type: () -> None
        self.runtime = None

    def is_ready(self):
        # type: () -> bool
        """接上运行时才算就绪：判定必须有成员集合才算得出来。"""
        return self.runtime is not None

    def add_node(self, node, saved_data=None):
        # type: (object, object) -> bool
        """把一台设备记进供需账。**对每个成员每帧都会被调用**，必须幂等。

        判据是整行的 `signature()`（含代次），不是「位置在不在表里」：
        同位置换了方块要重算，同一台设备则一行都不动——否则每次拓扑重建都会
        对全部成员各来一次 O(n) 重算，那是 O(n²)。

        `saved_data` **刻意不用**：供能速率与耗能都来自定义表，而工作态与
        装置数量的权威在各自的设备/服务侧（见 `set_device_state`）。让存档
        盖过定义表，就是「缓存变成第二份权威」的经典形态。
        """
        definition = self._definition_of(node.device_id)
        if definition is None:
            self.stats["refused"] += 1
            return False
        current = self._members.get(node.node_key)
        built = self._build_member(node, definition, current)
        if current is not None and current.signature() == built.signature():
            return False
        self._members[node.node_key] = built
        self.stats["added"] += 1
        self._settle()
        return True

    def remove_node(self, node):
        # type: (object) -> bool
        """节点离开网络：把它那一行撤掉。**不等于世界里的方块被拆了。**"""
        if self._members.pop(node.node_key, None) is None:
            return False
        self.stats["removed"] += 1
        self._settle()
        return True

    def note_domain_change(self, node, from_domain_ids, to_domain_ids):
        # type: (object, tuple, tuple) -> bool
        """**有意不处理。**

        唯一与负载域有关的是 `follows_bandwidth` 的能耗，而它的折算口径属 R08、
        至今未定，因此本服务无论如何都算不出那个数（它恒落在
        `unwired_inputs` 里）。域变了不改判定，就不该声称「处理了」——
        `NetworkService.note_domain_change` 的返回值语义是「是否真的处理了」，
        不是「我知道这件事」。
        """
        return False

    def tick(self, context, budget):
        # type: (dict, int) -> int
        """**没有到期任务**：判定是成员与输入的纯函数，已在它们变化时算好。

        恒返回 0 是有意的：本服务不占用帧预算，也不参与 `context["tick"]`
        的时间推导，因此假时钟仍可精确复现。
        """
        return 0

    def save_node_data(self, node):
        # type: (object) -> object
        """**本服务在该节点上没有持久数据**，因此返回 None。

        供需两边的权威都在定义表与设备自身：供能速率与耗能是定义表的静态值，
        工作态与装置数量属于各自的设备。这里**不返回一份看起来像存档的空字典**
        ——那会让恢复流程以为有东西要读（任务 12）。
        """
        return None

    # -- 判定 ----------------------------------------------------------

    def power_ok(self, node_key):
        # type: (object) -> object
        """本网络当前是否有电。`True` / `False` / `None`（待确认）。

        **这是网络级判定，不是逐设备判定**：一个分量共用一份供需账，因此同一
        网络里任何一台设备拿到的都是同一个答案（能源按网络计，blocks.md 的
        创造能量源写的就是「为网络提供…」）。逐设备的差异要等 04-06 的断电
        结算口径定案。

        **非成员返回 `None`**：设备刚放下、拓扑还没把它同步进来的那一帧就是
        这种情形（与 01 记的「刚放下那一帧」同一类），说「有电」或「没电」
        都是无根据的。
        """
        if self.runtime is None or node_key not in self._members:
            return None
        return self._ok

    def set_device_state(self, ref, state=None, unit_count=None):
        # type: (object, str, int) -> bool
        """把一台设备的**工作态 / 装置数量**告诉能源账。

        这是 `unwired_inputs` 那类缺口的接线入口：`per_unit` 设备（存储驱动器、
        内存槽）要有人告诉它装了几块盘/几根条，工作态可变的设备（装配仓、
        并行处理器）要有人告诉它在不在加工，它们的耗能才算得出来。

        驱动器槽位、处理器租约与装配仓使用权变化由组合根同步调用；
        初次挂载或网络迁移由 `inputs_of` 重读。内存条数量仍待接入。
        更新立即参与供需判定，不只是改变诊断明细。

        只接受**当前这一代**的引用：换过实例的设备，旧的调用方不该还能改它。
        """
        member = self._members.get(ref.node_key)
        if member is None or not member.ref.matches(ref):
            self.stats["refused"] += 1
            return False
        if state is not None and state not in DEVICE_STATES:
            raise ValueError("未知的设备工作态: %r" % (state,))
        new_state = member.state if state is None else state
        new_count = member.unit_count if unit_count is None else int(unit_count)
        if new_state == member.state and new_count == member.unit_count:
            return False
        member.state = new_state
        member.unit_count = new_count
        member.demand, member.gap = self._demand_of(
            self._definition_of(member.device_id), new_state, new_count
        )
        self.stats["state_updates"] += 1
        self._settle()
        return True

    # -- 查询 ----------------------------------------------------------

    def members_of_kind(self, gap):
        # type: (str) -> list
        """落在某类缺口里的设备标识（诊断用）。"""
        return sorted(
            member.device_id
            for member in self._members.values()
            if member.gap == gap
        )

    def member_view(self):
        # type: () -> list
        """逐设备的供需明细。"""
        return [
            self._members[key].to_dict()
            for key in sorted(self._members, key=lambda item: item.as_key())
        ]

    def totals(self):
        # type: () -> dict
        return dict(self._totals)

    def status(self):
        # type: () -> dict
        return {
            "service_id": self.service_id,
            "available": self.is_ready(),
            "stand_in": False,
            "ready": self.is_ready(),
            "members": len(self._members),
            "suppliers": self._totals["suppliers"],
            "power_ok": self._ok,
            "reason": "" if self._ok is True else self._reason_code,
        }

    def diagnostics(self):
        # type: () -> dict
        """只读。**不重算、不预留、不消耗能源。**"""
        report = NetworkService.diagnostics(self)
        report.update(
            {
                "policy": self.policy.describe(),
                "unresolved": self.policy.unresolved_report(),
                "power_ok": self._ok,
                "reason": "" if self._ok is True else self._reason_code,
                "totals": dict(self._totals),
                "members": self.member_view(),
                # 被替身口径忽略掉的需求。**必须逐个列出来**：只报一个策略字段名，
                # 读的人无从知道到底丢了哪几台设备的需求。
                "ignored_demand": self._ignored_demand(),
                "stats": dict(self.stats),
                "scope_note": (
                    "固定耗能结算与供能判定已实现；RF 计量（04-05/13-02）、"
                    "断电暂停与逐步恢复（04-06）、设备级能源状态与变化通知（04-08）"
                    "未实现。驱动器数量、处理器与装配仓工作态已接入；内存条数量仍待接入，"
                    "负载跟随的耗能折算口径未定（R08）。"
                ),
            }
        )
        return report

    # -- 内部 ----------------------------------------------------------

    def _definition_of(self, device_id):
        # type: (str) -> object
        """定义表里的设备定义。**未登记返回 None，不抛**。

        未登记的标识在注册表那一层就会报错（`DeviceRegistry.register`），走到
        这里说明定义表与注册表不一致；那不该让每帧一次的服务推进炸掉。
        """
        if self.runtime is None:
            return None
        try:
            return self.runtime.definitions.get(device_id)
        except KeyError:
            return None

    def _build_member(self, node, definition, previous):
        # type: (object, object, object) -> EnergyMember
        """算出这一行。

        工作态与装置数量**从旧行继承**（同位置同一代才是同一台设备）：它们
        不属于定义表，是别人告诉本服务的。已注入 `inputs_of` 的字段以
        当前权威值覆盖；没有来源的字段才继承。换代次后不继承旧设备缓存。
        """
        state = None
        unit_count = None
        if previous is not None and previous.ref.matches(node.ref):
            state = previous.state
            unit_count = previous.unit_count
        if self.inputs_of is not None:
            inputs = self.inputs_of(node.ref, node.device_id)
            if "state" in inputs:
                state = inputs["state"]
            if "unit_count" in inputs:
                unit_count = inputs["unit_count"]
        demand, gap = self._demand_of(definition, state, unit_count)
        return EnergyMember(
            node.node_key,
            node.ref,
            node.device_id,
            definition.supply_rate(),
            demand,
            gap,
            state,
            unit_count,
        )

    @staticmethod
    def _demand_of(definition, state, unit_count):
        # type: (object, object, object) -> tuple
        """这台设备的耗能（EN/t）与算不出时的原因。

        四种情况都返回 `UNCONFIGURED`，但**原因分成两类**——它们的关闭路径
        不同（改文档 vs 接线），因此不能合并成一句「未知」。
        """
        energy = definition.energy
        if energy.follows_bandwidth:
            # 能耗等于所在负载域的负载。折算口径属 R08，不是「没人告诉它」，
            # 因此哪怕域已经同步过来也照样算不出。
            return UNCONFIGURED, GAP_UNWIRED_INPUTS
        if energy.per_unit is not None and unit_count is None:
            return UNCONFIGURED, GAP_UNWIRED_INPUTS
        if energy.work != energy.idle and state is None:
            # 工作态还没人告诉它。**不能默认按空闲算**：那是一个偏小的数，
            # 会让「供能够不够」判成「够」——错在乐观那一侧。
            return UNCONFIGURED, GAP_UNWIRED_INPUTS
        value = energy.value_for_state(state or DEVICE_IDLE, unit_count or 0)
        if value is UNCONFIGURED:
            return UNCONFIGURED, GAP_MISSING_SPEC
        return int(value), ""

    def _settle(self):
        # type: () -> None
        """重算供需合计与判定。**四条件一起算完再落缓存**，不留半套结论。"""
        suppliers = 0
        supply_total = 0
        demand_total = 0
        unknown_supply = []
        missing_spec = []
        unwired = []
        for key in sorted(self._members, key=lambda item: item.as_key()):
            member = self._members[key]
            if member.is_supplier():
                suppliers += 1
                if member.supply is UNCONFIGURED:
                    unknown_supply.append(member.device_id)
                else:
                    supply_total += int(member.supply)
            if member.gap == GAP_MISSING_SPEC:
                missing_spec.append(member.device_id)
            elif member.gap == GAP_UNWIRED_INPUTS:
                unwired.append(member.device_id)
            else:
                demand_total += int(member.demand)

        if suppliers == 0:
            # 一条供能设备都没有。**说「没电」是一个断言**，而 04 文档把
            # 「报没电还是报待确认」记为待决问题，因此库默认不下这个断言。
            if self.policy.no_source == EnergyPolicy.BROWNOUT:
                ok, code = False, Reason.BROWNOUT
            else:
                ok, code = None, Reason.ENERGY_UNAVAILABLE
        elif unknown_supply:
            # 供能总量这时只是下界，拿它比大小会得出不成立的结论。
            ok, code = None, Reason.ENERGY_UNAVAILABLE
        elif supply_total < demand_total:
            # 供能精确、需求至多是下界，因此「精确 < 下界 ≤ 真需求」成立。
            ok, code = False, Reason.BROWNOUT
        elif self._has_unsettled_demand(missing_spec, unwired):
            ok, code = None, Reason.ENERGY_UNAVAILABLE
        else:
            ok, code = True, ""

        self._ok = ok
        self._reason_code = code
        self._totals = {
            "suppliers": suppliers,
            "supply_total": supply_total,
            "demand_total": demand_total,
            "unknown_supply": sorted(unknown_supply),
            "missing_spec": sorted(missing_spec),
            "unwired_inputs": sorted(unwired),
        }
        self.stats["settles"] += 1

    def _has_unsettled_demand(self, missing_spec, unwired):
        # type: (list, list) -> bool
        """还有算不出的需求，且策略要求把它当成「说不准」吗。"""
        if missing_spec and self.policy.missing_spec != EnergyPolicy.IGNORE:
            return True
        if unwired and self.policy.unwired_inputs != EnergyPolicy.IGNORE:
            return True
        return False

    def _ignored_demand(self):
        # type: () -> dict
        """被替身口径忽略掉的需求，按原因分组逐个列出。

        只列**真的被忽略**的那些：策略取 `PENDING` 时它们让判定停在「待确认」，
        并没有被丢掉，列进这里反而会让人以为漏算了。
        """
        return {
            GAP_MISSING_SPEC: (
                self._totals["missing_spec"]
                if self.policy.missing_spec == EnergyPolicy.IGNORE
                else []
            ),
            GAP_UNWIRED_INPUTS: (
                self._totals["unwired_inputs"]
                if self.policy.unwired_inputs == EnergyPolicy.IGNORE
                else []
            ),
        }
