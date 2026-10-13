# coding=utf-8
"""网络接口设备：总线接口、存货口、取货口与样板口。

对应任务 06-04 至 06-08。**本模块是领域层**，不 import 任何引擎模块
（`test_import_boundaries` 静态拦着）。引擎访问全部经注入的契约：

- 朝向容器的真实实现是 `adapters/containers_engine.py`，这里只认
  `ContainerProvider.container_at(node_key)` 这个鸭子类型；
- 真正的搬运由 `storage/transfer.py` 的 `TransferCoordinator` 执行，
  而且**经主控命令提交**，不在这里直连库存。

## 三条分工，别混

| 关注点 | 归属 | 理由 |
| --- | --- | --- |
| 接口什么时候尝试、退避到什么程度、缓冲里有什么 | 本模块的 `PortStore` / `PortRunner` | 设备私有状态；**活得比网络运行时久**（缓冲里的东西不因为断网消失） |
| 这次搬运到底动不动得成、动多少 | `TransferCoordinator` | 预检与受控提交只有一处 |
| 能不能开工（主控、能源、带宽、节点代次） | 主控准入链 | 全局约束 2；设备不得绕过主控 |

## 为什么轮询 handler 不直接搬东西

`network/ticking.py` 的模块 docstring 明令「不得在 handler 里直接改库存」，
架构 §7 也写明「到期任务仍须通过主控制器的运行准入」。因此 handler 只做一件事：
把一条**设备发起的**命令交给主控，由主控准入后落到存储服务上执行。

控制器注释里写明了这种情形是预期的（`controller.py` 的 `ledger_key`：
`source.device_ref` 缺失时回退到 `command.device_ref`，「正是到期任务的情形」）。
"""

from ...common.definitions import (
    ITEM_INTAKE_PORT_SLOT_COUNT,
    ITEM_OUTPUT_PORT_SLOT_COUNT,
    PASSIVE_PORT_SLOT_COUNT,
    PICKUP_FILTER_LIMIT,
    TEMPLATE_PATTERN_SLOT_COUNT,
    TEMPLATE_CONTAINER_SLOT_COUNT,
)
from ...common.identity import FACING_NAMES, FACING_OFFSETS, NodeKey, node_key_text
from ...common.protocol import (
    BlockReason,
    CommandKind,
    CommandResult,
    CommandSource,
    DeviceCommand,
    ORIGIN_EXTERNAL,
    ORIGIN_INTERNAL,
    Reason,
    TransferReceipt,
    TransferRequest,
    TransferStatus,
)
from ...common.resource_key import ResourceKey  # noqa: F401 (仅用于 # type: 注释)
from ..persistence.repository import Record
from .capabilities import (
    FLUID_PORT,
    ITEM_PORT,
    CapabilityRegistry,
)
from ..runtime.lifecycle import ChangeKind
from ..runtime.scheduler import PollOutcome
from ..storage.transfer import (
    BufferDisposition,
    ContainerEndpoint,
    SlotView,
    TransferBuffer,
    receipt_result,
)

# -- 设备与角色 ------------------------------------------------------------

#: 被动总线：外部物流写进它自己的容器，网络按配置与它交换（06-04）。
PASSIVE = "passive"
#: 存货口：读朝向容器的内容存进网络（06-05）。
INTAKE = "intake"
#: 取货口：按过滤从网络取货供给朝向容器（06-06）。
OUTPUT = "output"
#: 样板口：成批备料/投料入口与本地缓冲回执（06-07）。
TEMPLATE = "template"

#: 设备标识 -> 角色。**这是本模块认识这些方块的唯一依据**，不按名字猜。
ROLE_OF = {
    "endernet:bus_item_io": PASSIVE,
    "endernet:bus_fluid_io": PASSIVE,
    "endernet:bus_item_input_io": INTAKE,
    "endernet:bus_fluid_input_io": INTAKE,
    "endernet:bus_item_output_io": OUTPUT,
    "endernet:bus_fluid_output_io": OUTPUT,
    "endernet:bus_template_io": TEMPLATE,
}

#: 走流体路径的三台。**它们的引擎适配属 13-03，尚未实现**，
#: 因此容器提供者会返回 None，命令明确回 `no_adapter` 而不是假装能搬。
FLUID_DEVICES = frozenset(
    [
        "endernet:bus_fluid_io",
        "endernet:bus_fluid_input_io",
        "endernet:bus_fluid_output_io",
    ]
)

#: 需要朝向邻居才能工作的角色。被动总线**不在此列**：它交换的是自己的容器。
FACING_ROLES = frozenset([INTAKE, OUTPUT, TEMPLATE])

#: **有内部缓冲**的角色。四台都有，但存放形态不同（见 `BUFFER_*`）：
#: 被动接口的 9 格、存货口的 5 格与取货口的 5 格都是**方块容器**（真实槽位，
#: `blocks.md`），样板口的原料缓冲是**内存结构**（容量归 R05）。
INTERNAL_BUFFER_ROLES = frozenset([PASSIVE, INTAKE, OUTPUT, TEMPLATE])

#: **对外那一侧就是本地缓冲**（而不是朝向的容器）的角色。
#:
#: 这个集合与 `INTERNAL_BUFFER_ROLES` **回答的不是同一个问题**，2026-09-29
#: 之前它们共用一个名字 `BUFFERED_ROLES`，于是「取货口有没有缓存」这个问题
#: 一直答不上来——它对该集合的答案是「没有」（所以 `external_endpoint` 给它
#: 朝向容器，这是对的），对内部缓冲的答案却是「有 5 格」（所以它的 `buffer`
#: 一直是 `None`，这是错的）。取货口正是把两者分开的那台：缓存只是
#: 「网络 → 缓存 → 朝向的容器」这条路上的中间站，**外部物流的存取不该打到
#: 缓存格上**。把 OUTPUT 加进本集合会让 `PortItemPort.external_endpoint`
#: 把外部那双的手引到缓存里，而不是引到它真正该投递的容器里。
BUFFER_AS_EXTERNAL_SIDE = frozenset([PASSIVE, TEMPLATE])

# -- 配置方向 --------------------------------------------------------------

DIRECTION_IMPORT = "import"
DIRECTION_EXPORT = "export"
DIRECTION_BOTH = "both"
DIRECTIONS = frozenset([DIRECTION_IMPORT, DIRECTION_EXPORT, DIRECTION_BOTH])

#: 过滤模式。**取货只接受白名单**（`blocks.md` 对取货的原话是「设定 9 种」，
#: 那是个显式集合）；**存货口两种都支持**（「有黑名单/白名单模式」）。
MODE_WHITELIST = "whitelist"
MODE_BLACKLIST = "blacklist"
FILTER_MODES = frozenset([MODE_WHITELIST, MODE_BLACKLIST])

#: 各角色接受的过滤模式。
ROLE_FILTER_MODES = {
    PASSIVE: frozenset([MODE_WHITELIST]),
    INTAKE: frozenset([MODE_WHITELIST, MODE_BLACKLIST]),
    OUTPUT: frozenset([MODE_WHITELIST]),
    TEMPLATE: frozenset([MODE_WHITELIST]),
}

#: 空名单在替身口径下的含义（**未定案**，见 `PortPolicy.empty_filter_means`）。
EMPTY_MEANS_NOTHING = "nothing"
EMPTY_MEANS_EVERYTHING = "everything"

#: 默认的堆叠上限。**只在**适配器答不出 `stack_limit_of` 时兜底——
#: 真实上限由容器的 `maxStackSize` 决定，不能拿它当「每格 64 件」的策略。
DEFAULT_STACK_LIMIT = 64

#: 接口状态的存档结构版本。
#:
#: **v2 的改动是语义性的**：v1 把两种接口的方块容器都当作「原料/存取缓冲」，
#: v2 之后被动接口的 9 格是存取共用的**真实库存**、样板接口的 9 格是
#: **工厂样板专用槽**（原料缓冲改为独立的内存缓冲）。因此 v1 的样板口记录
#: **不能按新语义解释**——任务 12 要为它写迁移，在那之前只标记、保留原载荷。
PORT_STATE_SCHEMA_VERSION = 2

#: 接口状态的存档记录类型。**一条接口一条记录**，主键是位置文本
#: （`node_key_text`）——理由与驱动器槽位同：位置稳定，设备代次不持久。
PORT_RECORD_KIND = "endernet.port_state"

#: 拆除时余料的处置记录（12-06）。**与接口记录分开存**：接口记录在拆除时
#: 被删掉（同位置重放不继承），而「这里曾经有一台接口、缓冲里还剩 N 件」
#: 是必须留在档里的事实——R05 定案后要据此决定掉落还是保管。
#: 主键同样是位置文本，同一位置多次拆除时保留最新一条。
PORT_DISPOSAL_RECORD_KIND = "endernet.port_disposal"

#: 缓冲的存放形态。**「没有内部槽」与「有槽但是空的」必须分得开**——
#: 前者是还没有内部存放处，后者只是当前恰好为空。
#:
#: 2026-09-29：`BUFFER_NONE` **不再是任何角色的常态**。2026-09-25 的规格曾写
#: 「主动存取口无内部库存槽」，但 `blocks.md` 在 2026-09-28/29 给了存货口与
#: 取货口各 5 个真实槽位，四台接口现在都有内部缓冲，区别只在形态。
#: 保留这个取值，是因为它仍然要表达「这台设备的缓冲还没有存放处」。
BUFFER_NONE = None
#: 缓冲的实际存放处是方块自己的容器（被动接口 9 格、存货口 5 格、取货口 5 格）。
BUFFER_CONTAINER = "container"
#: 缓冲是独立的内存结构（样板接口的原料缓冲，容量归 R05）。
BUFFER_MEMORY = "memory"

#: 关掉缓存开关时，**已经在缓存格里的东西**怎么办。**未定案**。
#:
#: `blocks.md` 只说到「已有缓存物品的关闭处置仍待定，**不得直接清空**」，
#: 因此替身取保守的那一侧（`CACHE_KEEP`）。另外两种口径列在这里，是为了让
#: 「待定」有一个确切的名字，而不是一个空的 TODO——它们**没有实现**。
CACHE_KEEP = "keep"
CACHE_RETURN_TO_NETWORK = "return_to_network"
CACHE_DROP = "drop"
CACHE_ON_DISABLE = frozenset([CACHE_KEEP, CACHE_RETURN_TO_NETWORK, CACHE_DROP])


def role_of(device_id):
    # type: (str) -> str
    """该设备的接口角色；不是接口设备时返回空串。"""
    return ROLE_OF.get(device_id, "")


def is_port_device(device_id):
    # type: (str) -> bool
    return device_id in ROLE_OF


def is_fluid_port(device_id):
    # type: (str) -> bool
    return device_id in FLUID_DEVICES


def neighbor_key(node_key, facing):
    # type: (object, str) -> object
    """朝向那一面的位置。`facing` 未知或不是合法朝向时返回 None。

    朝向名字取自方块的 `minecraft:cardinal_direction`（`world.facing_at`
    读的就是它），与 `identity.FACING_NAMES` 一一对应。
    `adapters/world.py` 明确记着这条**未验证**：方块若缺该 trait，
    方向性端口会退化为无方向。这里对未知朝向**返回 None 而不猜一个方向**：
    猜错会把东西送进错误的容器，那是不可逆的。
    """
    if not facing:
        return None
    try:
        index = FACING_NAMES.index(facing)
    except ValueError:
        return None
    dx, dy, dz = FACING_OFFSETS[index]
    return node_key.offset(dx, dy, dz)


# -- 策略 ------------------------------------------------------------------


class PortPolicy(object):
    """接口的未定案旋钮。

    与 `MediumPolicy` / `DriverPolicy` / `TransferPolicy` 同形：做成具名策略
    而不是常数，因为这几条都还没定案（全局约束 7）。`assert_settled()`
    **只应在正式验收时调用**——开发与离线测试不应调用它。

    **接口槽数不在这里**：2026-09-25 的接口规格已经把被动接口共用 9 格、
    样板接口 9 个专用样板槽定案，它们进定义表（`PASSIVE_PORT_SLOT_COUNT` /
    `TEMPLATE_PATTERN_SLOT_COUNT` / `PICKUP_FILTER_LIMIT`），
    **不带 unresolved 标签**。把已定案的数值继续挂在替身规则上，
    会让正式验收为一条已经不存在的规则盖章。
    """

    __slots__ = (
        "template_buffer_capacity",
        "output_cache_capacity",
        "intake_buffer_capacity",
        "intake_filter_limit",
        "default_filter_mode",
        "empty_filter_means",
        "max_batch",
        "probe_unit",
        "cached_items_on_disable",
        "output_cache_strategy",
    )

    def __init__(
        self,
        template_buffer_capacity=None,
        intake_filter_limit=16,
        default_filter_mode=MODE_WHITELIST,
        empty_filter_means=EMPTY_MEANS_NOTHING,
        max_batch=64,
        probe_unit=1,
        output_cache_capacity=None,
        intake_buffer_capacity=None,
        cached_items_on_disable=CACHE_KEEP,
        output_cache_strategy="demand_first",
    ):
        # type: (int, int, str, str, int, int, int, int, str, str) -> None
        #: **R05**：样板接口的**原料缓冲**能装多少件。
        #: 与它那 9 个样板槽无关——规格明写「不能用 9 个样板槽推算」。
        #: 替身按「若干格 × 一个通用堆叠上限」估；真实值定案前不落进交付。
        self.template_buffer_capacity = int(
            template_buffer_capacity
            if template_buffer_capacity is not None
            else TEMPLATE_PATTERN_SLOT_COUNT.value * DEFAULT_STACK_LIMIT
        )
        #: 取货口 5 格缓存的**件数**上界。
        #:
        #: **这个数不是玩法数值，是从已定案的槽数推出来的护栏**：槽数是定案值
        #: （`blocks.md` 的 5 格，进定义表且不带 unresolved 标签），乘数用的是
        #: 「适配器答不出时的通用堆叠上限」。真实上界由容器与每件物品的
        #: `maxStackSize` 决定，与被动接口那条同口径：台账宣称的件数上限
        #: 只是保守护栏，不是容量规则。
        #:
        #: **刻意不复用 `template_buffer_capacity`**：那个值挂的是 R05，
        #: 拿它给一台槽数已定案的设备定容量，等于把 R05 的替身口径扩散到
        #: 一个不归它管的地方。
        self.output_cache_capacity = int(
            output_cache_capacity
            if output_cache_capacity is not None
            else ITEM_OUTPUT_PORT_SLOT_COUNT.value * DEFAULT_STACK_LIMIT
        )
        #: 存货口 5 格缓冲的**件数**上界。与 `output_cache_capacity` 同一条
        #: 口径（定案槽数 × 适配器答不出时的通用堆叠上限，是保守护栏而非
        #: 容量规则），**但刻意分成两个字段**：两台设备的槽数各自独立定案，
        #: 共用一个字段会让以后改其中一台时另一台跟着动，而那种联动在
        #: 诊断里看不出来。
        #:
        #: **不要与 `intake_filter_limit` 混起来**：那个是过滤条目数（R05
        #: 未定），这个是真实槽位的件数上界（已定案），两者恰好都在
        #: 「存货口」名下，但一个待定一个定案。
        self.intake_buffer_capacity = int(
            intake_buffer_capacity
            if intake_buffer_capacity is not None
            else ITEM_INTAKE_PORT_SLOT_COUNT.value * DEFAULT_STACK_LIMIT
        )
        #: **待确认**：存货口的过滤条目上限。`blocks.md` 只说了取货最多 9 种，
        #: 并明写**不得从取货的 9 种推导存货过滤上限**，因此这里的替身
        #: 刻意取一个**不等于 9** 的保守值。
        self.intake_filter_limit = int(intake_filter_limit)
        #: **待确认**：存货口的默认过滤模式（白名单 / 黑名单）。
        #: 替身取白名单——与 `PortConfig` 一直以来的语义一致，
        #: 因此不会因为换了策略对象而改变既有存档的读法。
        self.default_filter_mode = default_filter_mode
        #: **待确认**：空名单是什么意思。替身取「什么都不搬」，
        #: 因为它是唯一不会让一台刚放下的接口立刻动手的解释。
        self.empty_filter_means = empty_filter_means
        #: R05：单次尝试最多搬多少。**这是唯一真正的吞吐旋钮**——
        #: 一轮轮询只发一条命令、只搬一次，因此每轮的搬运量上界就是它
        #: （真实落地的量还要过介质额度那一关）。
        self.max_batch = int(max_batch)
        #: R05：目标端不支持模拟时，第一次试探搬多少。
        #: 取 1 是保守的：多搬的部分要回滚，而回滚本身可能失败。
        self.probe_unit = int(probe_unit)
        #: **未定案**：关掉取货口的缓存开关后，**已经在缓存格里的东西**怎么办。
        #:
        #: `blocks.md` 的原话是「已有缓存物品的关闭处置仍待定，**不得直接清空**」，
        #: 因此替身取 `CACHE_KEEP`——唯一一个不会造成损失的选项。
        #: 另两种口径（回网 / 掉落）**没有实现**，只留名字。
        self.cached_items_on_disable = cached_items_on_disable
        # R05 开发替身：先送已有缓存，按名单顺序补货；目标无空间不预取。
        # 只实现这一个口径，不能把未实现的策略作为可选行为放行。
        if output_cache_strategy != "demand_first":
            raise ValueError("未实现的取货缓存策略: %r" % (output_cache_strategy,))
        self.output_cache_strategy = output_cache_strategy

        if self.template_buffer_capacity <= 0:
            raise ValueError(
                "template_buffer_capacity 必须为正，得到 %r"
                % (self.template_buffer_capacity,)
            )
        if self.intake_filter_limit <= 0:
            raise ValueError(
                "intake_filter_limit 必须为正，得到 %r" % (self.intake_filter_limit,)
            )
        if self.default_filter_mode not in FILTER_MODES:
            raise ValueError("未知默认过滤模式: %r" % (self.default_filter_mode,))
        if self.empty_filter_means not in (EMPTY_MEANS_NOTHING, EMPTY_MEANS_EVERYTHING):
            raise ValueError("未知空名单口径: %r" % (self.empty_filter_means,))
        if self.max_batch <= 0:
            raise ValueError("max_batch 必须为正，得到 %r" % (self.max_batch,))
        if self.probe_unit <= 0:
            raise ValueError("probe_unit 必须为正，得到 %r" % (self.probe_unit,))
        if self.output_cache_capacity <= 0:
            raise ValueError(
                "output_cache_capacity 必须为正，得到 %r"
                % (self.output_cache_capacity,)
            )
        if self.intake_buffer_capacity <= 0:
            raise ValueError(
                "intake_buffer_capacity 必须为正，得到 %r"
                % (self.intake_buffer_capacity,)
            )
        if self.cached_items_on_disable not in CACHE_ON_DISABLE:
            raise ValueError(
                "未知的关闭缓存处置口径: %r" % (self.cached_items_on_disable,)
            )

    def filter_limit_for(self, role):
        # type: (str) -> int
        """该角色的过滤条目上限。

        **按角色分开**是有意的：取货的 9 种是定案值（`blocks.md`），
        存货的上限还没定——两处共用一个数就会把「取货 9 种」偷偷
        推广到存货上，而那正是文档禁止的。
        """
        if role in (PASSIVE, OUTPUT):
            return int(PICKUP_FILTER_LIMIT.value)
        if role == INTAKE:
            return self.intake_filter_limit
        # 样板口的过滤是「投料白名单」，规格未给它单独的上限，
        # 沿用取货那一档并在诊断里可查。
        return int(PICKUP_FILTER_LIMIT.value)

    def unresolved_report(self):
        # type: () -> list
        return [
            {
                "field": "port.output_cache_strategy",
                "rule": "R05",
                "value": self.output_cache_strategy,
                "note": "开发替身：先送缓存、按名单顺序补货、无目标空间不预取；"
                        "名单移除后的余货保留，预取与调度仍待定案",
            },
            {
                "field": "port.template_buffer_capacity",
                "rule": "R05",
                "value": self.template_buffer_capacity,
                "note": "样板接口原料缓冲的件数上限；不能用 9 个样板槽推算，替身值",
            },
            {
                "field": "port.intake_filter_limit",
                "rule": "R05",
                "value": self.intake_filter_limit,
                "note": "存货口过滤条目上限；尚未定案，且不得从取货的 9 种推导",
            },
            {
                "field": "port.default_filter_mode",
                "rule": "R05",
                "value": self.default_filter_mode,
                "note": "存货口默认过滤模式；替身取白名单",
            },
            {
                "field": "port.empty_filter_means",
                "rule": "R05",
                "value": self.empty_filter_means,
                "note": "空名单的含义；替身取「什么都不搬」",
            },
            {
                "field": "port.max_batch",
                "rule": "R05",
                "value": self.max_batch,
                "note": "单次尝试的搬运上限；替身取 64",
            },
            {
                "field": "port.probe_unit",
                "rule": "R05",
                "value": self.probe_unit,
                "note": "目标不支持模拟时的试探量；替身取 1",
            },
            {
                "field": "port.cached_items_on_disable",
                "rule": "R05",
                "value": self.cached_items_on_disable,
                "note": (
                    "关掉取货口缓存开关后，已在缓存格里的东西怎么处置；"
                    "`blocks.md` 明写「仍待定，不得直接清空」，替身取保留。"
                    "归 00 的「接口尚待确认的细则：配置变化后的余料处置」，"
                    "由 06 联合 11/12 明确"
                ),
            },
        ]

    def assert_settled(self):
        # type: () -> None
        """正式验收用：仍有未定项就报错。开发与离线测试不应调用它。"""
        pending = self.unresolved_report()
        if pending:
            raise AssertionError(
                "接口策略仍有 %d 项未定案，不能作为正式玩法交付：\n  "
                % (len(pending),)
                + "\n  ".join(
                    "%s（规则 %s，当前值 %r）"
                    % (item["field"], item["rule"], item["value"])
                    for item in pending
                )
            )

    def describe(self):
        # type: () -> dict
        return {
            "template_buffer_capacity": self.template_buffer_capacity,
            "output_cache_capacity": self.output_cache_capacity,
            "output_cache_strategy": self.output_cache_strategy,
            "intake_buffer_capacity": self.intake_buffer_capacity,
            "intake_filter_limit": self.intake_filter_limit,
            "default_filter_mode": self.default_filter_mode,
            "empty_filter_means": self.empty_filter_means,
            "cached_items_on_disable": self.cached_items_on_disable,
            "max_batch": self.max_batch,
            "probe_unit": self.probe_unit,
        }


# -- 配置 ------------------------------------------------------------------


class PortConfig(object):
    """玩家给一台接口设的配置。**落在 `DeviceRecord.config["port"]` 上。**

    `enabled` 默认 True——接口是「放下就开始工作」的方块。真正决定
    「能不能开工」的是 `is_ready_for(role, policy)`。

    ## 过滤模式（2026-09-25 接口规格）

    | 角色 | 模式 | 依据 |
    | --- | --- | --- |
    | 取货（被动取货 / 主动取货口） | **只白名单** | `blocks.md`：「允许设定 9 种」是个显式集合 |
    | 存货口 | 白名单 / 黑名单 | `blocks.md`：「有黑名单/白名单模式」 |

    **空名单的含义尚未定案**（文档明写：空名单行为、默认模式、存货过滤上限
    都待确认），因此它走 `PortPolicy.empty_filter_means` 的替身口径——
    替身取「什么都不搬」，因为那是唯一不会让一台刚放下的接口立刻动手的解释。

    ## 供货位（2026-09-25 接口规格）

    `supply_slots` 是**被动接口那 9 格里哪些用于供货**的下标集合；
    其余槽位都是存货位。规格把这件分工交给玩家逐槽指定（对照 AE2 的做法），
    因此它**是配置而不是待定的规则值**。

    **默认空 = 9 格全是存货位**：刚放下的被动接口只导入、不供货。
    这与「不替玩家猜一个供货策略」一致，也让「刚补入的供货资源不被自己
    存回网络」这条不变量成为**结构性**的——两个方向作用在不相交的槽上。
    """

    __slots__ = (
        "enabled",
        "direction",
        "filter_mode",
        "filter",
        "supply_slots",
        "limit",
        "cache_enabled",
    )

    def __init__(
        self,
        enabled=True,
        direction=DIRECTION_BOTH,
        filter=(),
        limit=0,
        filter_mode=MODE_WHITELIST,
        supply_slots=(),
        cache_enabled=True,
    ):
        # type: (bool, str, tuple, int, str, tuple, bool) -> None
        if direction not in DIRECTIONS:
            raise ValueError("未知方向: %r" % (direction,))
        if filter_mode not in FILTER_MODES:
            raise ValueError("未知过滤模式: %r" % (filter_mode,))
        self.enabled = bool(enabled)
        self.direction = direction
        self.filter_mode = filter_mode
        #: 过滤条目。白名单 = 只搬这些；黑名单 = 除这些之外都搬。
        self.filter = tuple(filter)
        #: 供货位下标。**只有被动接口用得上**；其余角色恒为空。
        self.supply_slots = tuple(sorted(set(int(slot) for slot in supply_slots)))
        #: 单次上限。0 = 用 `PortPolicy.max_batch`。
        self.limit = int(limit)
        if self.limit < 0:
            raise ValueError("limit 不能为负，得到 %r" % (self.limit,))
        #: 取货口的**缓存开关**（11-07 的 C2 把它叫「缓存模式」，界面语义是
        #: `settings_window` 里的一个开关）。
        #:
        #: `blocks.md`：启用时经缓存中转（网络 → 缓存 → 朝向容器），
        #: **关闭后关闭缓存槽，改由网络直接向朝向容器输入物品**。
        #: **默认启用**，而且「保存过的关闭状态重开/重载不得被默认值覆盖」——
        #: `from_dict` 因此是 `data.get(..., True)`：字段缺失取 True（旧存档
        #: 与新建都走这一支），字段存在则**原样读出**，`False` 不会被顶掉。
        #:
        #: **只有取货口读它**，其余角色拿到什么值都不影响行为（它们各自的中转
        #: 语义是别的东西，不由这个开关管）。**本字段目前只被记录与展示**——
        #: 「网络 → 缓存 → 朝向容器」这条中转路由已于 2026-10-03 交付
        #: （06-06；游戏内未验）。**存货口那一半（06-05）仍未实现**，
        #: `dispatch_intake` 至今是直输。
        #:
        #: **它与「有没有缓冲」是两件事**：关掉开关不撤缓冲，
        #: 见 `PortPolicy.cached_items_on_disable`。
        self.cache_enabled = bool(cache_enabled)

    # -- 判定 ----------------------------------------------------------

    def is_ready_for(self, role, policy=None):
        # type: (str, PortPolicy) -> bool
        """能不能开工。**「空名单」在这里是唯一依赖未定案口径的分支。**"""
        if not self.enabled:
            return False
        if role == TEMPLATE:
            # Template inputs are selected by the order batch.
            return True
        if self.filter:
            return True
        if role in (PASSIVE, OUTPUT):
            # 取货类：空名单就是没配置。与 `blocks.md` 的「设定 N 种」一致。
            return False
        means = (
            policy.empty_filter_means if policy is not None else EMPTY_MEANS_NOTHING
        )
        return means == EMPTY_MEANS_EVERYTHING

    def matches(self, key, policy=None):
        # type: (ResourceKey, PortPolicy) -> bool
        """这种资源是否在过滤范围内。"""
        if self.filter_mode == MODE_BLACKLIST:
            return key not in self.filter
        if not self.filter:
            means = (
                policy.empty_filter_means if policy is not None
                else EMPTY_MEANS_NOTHING
            )
            return means == EMPTY_MEANS_EVERYTHING
        return key in self.filter

    def allows(self, direction):
        # type: (str) -> bool
        return self.direction == DIRECTION_BOTH or self.direction == direction

    def effective_limit(self, policy):
        # type: (PortPolicy) -> int
        return self.limit if self.limit > 0 else policy.max_batch

    # -- 槽位 ----------------------------------------------------------

    def is_supply_slot(self, slot):
        # type: (int) -> bool
        return int(slot) in self.supply_slots

    def intake_slots(self, slot_count):
        # type: (int) -> list
        """存货位（供货位的补集）。**两个集合不相交，这就是不来回搬的根据。**"""
        return [slot for slot in range(slot_count) if not self.is_supply_slot(slot)]

    # -- 序列化 --------------------------------------------------------

    def to_dict(self):
        # type: () -> dict
        return {
            "enabled": self.enabled,
            "direction": self.direction,
            "filter_mode": self.filter_mode,
            "filter": [key.to_dict() for key in self.filter],
            "supply_slots": list(self.supply_slots),
            "limit": self.limit,
            "cache_enabled": self.cache_enabled,
        }

    @classmethod
    def from_dict(cls, data, policy=None):
        # type: (dict, PortPolicy) -> PortConfig
        """读回配置。**缺省值必须与构造器一致。**

        新字段（`filter_mode` / `supply_slots`）缺失时取策略口径：
        2026-09-25 之前的存档没有它们，而那时只有白名单、也没有供货位，
        因此默认值正好是旧语义，读进来不会改变既有设备的行为。
        """
        default_mode = (
            policy.default_filter_mode if policy is not None else MODE_WHITELIST
        )
        return cls(
            data.get("enabled", True),
            data.get("direction", DIRECTION_BOTH),
            tuple(ResourceKey.from_dict(raw) for raw in data.get("filter", ())),
            data.get("limit", 0),
            data.get("filter_mode", default_mode),
            tuple(data.get("supply_slots", ())),
            # 缓存开关：缺失即默认启用，存在则原样读出（`False` 不被顶掉）。
            data.get("cache_enabled", True),
        )

    def __repr__(self):
        return "PortConfig(%s, %s/%s, %d 项过滤, %d 个供货位)" % (
            "开" if self.enabled else "关",
            self.direction,
            self.filter_mode,
            len(self.filter),
            len(self.supply_slots),
        )


class BatchState(object):
    """样板口的一次投料批次（06-07）。

    「外部机器部分接收时保留批次状态，交由 09 决定后续推进」：本对象就是
    **被保留的那份状态**。它记着这一批要投什么、投了多少、还差多少，
    以及它当前卡在哪。**本模块不决定后续推进**——那是 09 的事。
    """

    __slots__ = ("op_id", "resource_key", "requested", "staged", "delivered", "reason")

    def __init__(self, op_id, resource_key, requested):
        # type: (str, object, int) -> None
        self.op_id = op_id
        self.resource_key = resource_key
        self.requested = int(requested)
        #: 已经从中转位备好、等着投出去的量。
        self.staged = 0
        #: 已经真的进了对面机器的量。
        self.delivered = 0
        self.reason = ""

    def remaining(self):
        # type: () -> int
        return max(0, self.requested - self.delivered)

    def is_complete(self):
        # type: () -> bool
        return self.delivered >= self.requested

    def to_dict(self):
        # type: () -> dict
        return {
            "op_id": self.op_id,
            "resource_key": (
                self.resource_key.to_dict() if self.resource_key is not None else None
            ),
            "requested": self.requested,
            "staged": self.staged,
            "delivered": self.delivered,
            "remaining": self.remaining(),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> BatchState
        state = cls(
            data["op_id"],
            ResourceKey.from_dict(data["resource_key"])
            if data.get("resource_key") is not None
            else None,
            data["requested"],
        )
        state.staged = int(data.get("staged", 0))
        state.delivered = int(data.get("delivered", 0))
        state.reason = data.get("reason", "")
        return state

    def __repr__(self):
        return "BatchState(%s, %d/%d)" % (self.op_id, self.delivered, self.requested)


# -- 对外端口（实际的能力对象）--------------------------------------------


class PortItemPort(object):
    """接口**「对外那一侧」**的实际能力对象（`capabilities.ItemPort` 契约）。

    架构 §4 要求「明确实际能力对象在哪里构造、保存和取得，**不能只返回标签**」：
    每个 `PortState` 在构造时建一个本对象并注册进它的 `CapabilityRegistry`，
    此后只取用不重建（与 `StorageDriverProvider` 同一分工）。

    「对外那一侧」按角色解释，两种情况统一成同一组方法，因为外部调用方
    只关心一件事——「往这台接口的对外一侧放/取」：

    | 角色 | 对外那一侧 | 依据 |
    | --- | --- | --- |
    | 被动总线 / 样板口 | **它自己的本地缓冲** | 06-04：外部物流访问本地缓冲 |
    | 存货口 / 取货口 | **朝向的容器** | blocks.md：自动从/向朝向的容器 |

    **不经过主控。** 外部物流往接口里塞东西发生在网络之外，网络只是事后
    把缓冲收进来（06-04 的「按配置与网络交换」）。因此这两个方法只动缓冲或
    朝向容器，**不动网络库存、不推进订单**——它们不是业务命令的旁路，
    因为网络存量根本不在这条路径上。
    """

    adapter_id = "endernet_item_port"

    def __init__(self, state, ports):
        # type: (PortState, PortStore) -> None
        self.state = state
        self.ports = ports

    def external_endpoint(self, op_id):
        # type: (str) -> tuple
        """对外那一侧的端点。返回 `(endpoint, detail)`，失败时端点为 None。"""
        if self.state.role in BUFFER_AS_EXTERNAL_SIDE:
            endpoint = self.ports.buffer_endpoint(self.state, op_id, ORIGIN_EXTERNAL)
            if endpoint is None:
                return None, "该接口没有可用的本地缓冲"
            return endpoint, ""
        endpoint, reason, detail = self.ports.facing_endpoint(self.state)
        if endpoint is None:
            return None, detail or "朝向那一侧没有可用的容器（%s）" % (reason,)
        return endpoint, ""

    def insert(self, op_id, resource_key, count):
        # type: (str, ResourceKey, int) -> TransferReceipt
        """外部把东西放进这台接口。**来源记为外部**，因此网络收走时算新输入。"""
        endpoint, detail = self.external_endpoint(op_id)
        if endpoint is None:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED, Reason.NO_CONTAINER,
                resource_key,
            )
        receipt = endpoint.commit_insert(
            op_id, resource_key, count, "", ORIGIN_EXTERNAL
        )
        self._note_side_effect(receipt)
        return receipt

    def extract(self, op_id, resource_key, count):
        # type: (str, ResourceKey, int) -> TransferReceipt
        """外部从这台接口把东西拿走。"""
        endpoint, detail = self.external_endpoint(op_id)
        if endpoint is None:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED, Reason.NO_CONTAINER,
                resource_key,
            )
        receipt = endpoint.commit_extract(op_id, resource_key, count)
        self._note_side_effect(receipt)
        return receipt

    def _note_side_effect(self, receipt):
        # type: (object) -> None
        """外部物流动了**本地缓冲**就标脏。

        只对「对外那一侧是缓冲」的角色标：其余角色（存货口/取货口）的对外
        动作打在朝向容器上，那是别人的箱子，与这台接口的存档无关。
        """
        if (
            self.state.role in BUFFER_AS_EXTERNAL_SIDE
            and receipt is not None
            and receipt.moved > 0
        ):
            self.ports.note_changed(self.state)

    def describe(self):
        # type: () -> dict
        return {
            "adapter_id": self.adapter_id,
            "role": self.state.role,
            "external_side": (
                "buffer" if self.state.role in BUFFER_AS_EXTERNAL_SIDE else "facing"
            ),
            "node_key": self.state.node_key.as_key(),
        }

    def __repr__(self):
        return "PortItemPort(%s @ %r)" % (self.state.role, self.state.node_key)


class PortFluidPort(object):
    """流体接口的**实际能力对象**。**它一律拒绝，而且说得出为什么。**

    13-03（真实流体适配）未做，因此这里不给一个「看起来能传 mB」的实现。
    返回 `no_adapter` 而不是「搬了 0」，理由是后者会让调用方以为
    「这台容器是空的」而不是「这条通路还没做」——那正是架构 §7 说的
    「尚不支持的命令明确拒绝，不能返回假成功」。
    """

    adapter_id = "endernet_fluid_port"

    def __init__(self, state):
        # type: (PortState) -> None
        self.state = state

    def _refuse(self, op_id, count, resource_key):
        # type: (str, int, ResourceKey) -> TransferReceipt
        return TransferReceipt(
            op_id, count, 0, TransferStatus.UNSUPPORTED, Reason.NO_ADAPTER,
            resource_key,
        )

    def insert(self, op_id, resource_key, count):
        # type: (str, ResourceKey, int) -> TransferReceipt
        return self._refuse(op_id, count, resource_key)

    def extract(self, op_id, resource_key, count):
        # type: (str, ResourceKey, int) -> TransferReceipt
        return self._refuse(op_id, count, resource_key)

    def describe(self):
        # type: () -> dict
        return {
            "adapter_id": self.adapter_id,
            "available": False,
            "pending_task": "13-03",
        }


# -- 设备状态 --------------------------------------------------------------


class PortState(object):
    """一台接口的权威状态。**由启动层持有**，不挂在网络运行时上。

    理由与 `DriverState` 一致（架构 §3）：接口缓冲里的东西不能因为网络断开、
    主控被移除或运行时重建而消失。06 验收明写「主控无效或断电不继续抽取源
    容器；**外部物流已送入缓冲的资源仍保留**」——把状态放进服务里就做不到。
    """

    __slots__ = (
        "ref",
        "device_id",
        "role",
        "config",
        "buffer",
        "loaded",
        "facing",
        "attempt_seq",
        "hold",
        "hold_detail",
        "batch",
        "buffer_kind",
        "capabilities",
        "item_port",
        "fluid_port",
        "stats",
    )

    def __init__(self, ref, device_id, role, config=None, buffer=None, facing=None):
        # type: (object, str, str, PortConfig, TransferBuffer, object) -> None
        self.ref = ref
        self.device_id = device_id
        self.role = role
        self.config = config if config is not None else PortConfig()
        self.buffer = buffer
        #: 缓冲的存放形态（见 `BUFFER_*`）。**不派生的**：它由「这台设备
        #: 该不该有内部槽」决定，而不是由 `buffer is None` 推断——
        #: 一个还没挂上存放处的缓冲与「本来就没有内部槽」是两回事。
        self.buffer_kind = BUFFER_NONE if buffer is None else BUFFER_MEMORY
        self.loaded = True
        self.facing = facing
        #: 挂在这台设备上的**实际能力对象**（架构 §4 要求可查）。
        #: 光有定义表里的能力标签不算数：`has_capability("item_port")` 为真
        #: 而这里什么都没有，正是「已声明能力不等于已实例化能力对象」的形态。
        self.capabilities = CapabilityRegistry()
        #: 对外那一侧的实际端口。由 `PortStore.ensure` 注册（它需要回指 store
        #: 才能解析缓冲与朝向），这里先留空。
        self.item_port = None
        self.fluid_port = None
        #: 本次尝试的序号。每发起一次搬运就加一，用来生成 request_id。
        #: **重试同一次搬运不换 op_id**——换了就等于允许再搬一遍。
        self.attempt_seq = 0
        #: 待核对标记。非空时**一律不再发起搬运**（06-02：不确定不重放）。
        #: 由 `port_clear` 命令人工解除，或由恢复流程（12-07）销账。
        self.hold = ""
        self.hold_detail = ""
        self.batch = None
        self.stats = {
            "polls": 0,
            "dispatches": 0,
            "moved": 0,
            "unchanged": 0,
            "refused": 0,
            "uncertain": 0,
            "holds": 0,
            "wakes": 0,
            "no_container": 0,
        }

    # -- 查询 ----------------------------------------------------------

    @property
    def node_key(self):
        return self.ref.node_key

    def is_usable(self):
        # type: () -> bool
        """能不能参与业务。**未加载不等于被拆掉**（与驱动器同一条纪律）。"""
        return self.loaded

    def is_held(self):
        # type: () -> bool
        return bool(self.hold)

    def target_key(self):
        # type: () -> object
        """朝向那一面的位置。朝向未知时返回 None。"""
        return neighbor_key(self.node_key, self.facing)

    def next_request_id(self):
        # type: () -> str
        """给下一次尝试发一个 request_id。**必须带设备身份。**

        主控的账本键是 `(request_id, 设备实例)`（`controller.ledger_key`），
        因此它天然分得开两台设备；但存储服务自己的账本键是
        `(op_id, 方向)`（`StorageService._commit`），**不含设备**。
        不带位置的话，两台同角色的接口会生成一模一样的 op_id，
        后一台的写入会命中前一台的去重记录而被当成「重发」——
        回执说成功、东西没进网络，而它自己那份已经扣掉了。
        """
        self.attempt_seq += 1
        return "port:%s:%d" % (self.node_key.as_key(), self.attempt_seq)

    def to_dict(self):
        # type: () -> dict
        """**状态视图**（界面与诊断）。比存档多了统计、能力清单与几何投影。

        真正的存档走 `archive_payload()`：那一份**不含统计计数**。计数每帧
        都在变，写进记录会让「这台接口的存档」每次都不一样，发布去重与
        「改动才写盘」两件事同时失效。
        """
        target = self.target_key()
        view = self.archive_payload()
        view.update({
            "node_key": self.node_key.to_dict(),
            "generation": self.ref.generation,
            "loaded": self.loaded,
            "target": target.to_dict() if target is not None else None,
            "buffer_summary": (
                self.buffer.describe() if self.buffer is not None else None
            ),
            # 实际挂上的能力对象（架构 §4）。界面与诊断靠它回答
            # 「这台设备到底提供了什么」，而不是看定义表里的标签。
            "capabilities": sorted(self.capabilities.names()),
            "stats": dict(self.stats),
        })
        return view

    def archive_payload(self):
        # type: () -> dict
        """**存档结构**（任务 12 的入口）。不含任何随帧变化的计数。

        包含缓冲的**完整批次表**而不是一份概览：批次上的 `origin` 与 `op_id`
        是「这份资源算不算外部输入」的唯一依据，丢了它就只能靠猜。

        `device_id` 必须存：同位置**换了方块**时，记录不得被新设备继承
        （那会把一台取货口的缓存配置装到一台存货口上）；`role` 由它推出，
        存下来只是为了恢复时不必再查定义表。
        """
        return {
            "schema_version": PORT_STATE_SCHEMA_VERSION,
            "device_id": self.device_id,
            "role": self.role,
            "facing": self.facing,
            "config": self.config.to_dict(),
            "hold": self.hold,
            "hold_detail": self.hold_detail,
            "batch": self.batch.to_dict() if self.batch is not None else None,
            "buffer_kind": self.buffer_kind,
            "buffer": self.buffer.to_dict() if self.buffer is not None else None,
        }

    def __repr__(self):
        return "PortState(%s @ %r)" % (self.device_id, self.node_key)


class PortStore(object):
    """接口状态集合。**由启动层持有并注入网络服务**（架构 §3）。

    与 `StorageDriverStore` 同地位：同时是接口状态的唯一权威，
    也是「设备配置」与「缓冲归属」的保存点。
    """

    def __init__(self, definitions, registry, policy=None, containers=None,
                 disposition=None):
        # type: (object, object, PortPolicy, object, object) -> None
        #: 定义表。**它被读**：`_register_capabilities` 按定义表声明的能力
        #: 决定注册哪些能力对象——注册多了会造出「声称能传物品、实际没有
        #: 对应实现」的端口（架构 §4 点名的形态）。
        self.definitions = definitions
        self.registry = registry
        self.policy = policy if policy is not None else PortPolicy()
        #: `ContainerProvider`：位置 -> ContainerAdapter。生产实现在
        #: `adapters/containers_engine.py`，测试传替身。**可为 None**，
        #: 此时所有需要容器的动作明确报 `no_adapter`。
        self.containers = containers
        #: R05 未定：缓冲里剩下的东西怎么处置。默认只保留并留痕。
        self.disposition = disposition if disposition is not None else BufferDisposition()
        self._states = {}
        #: 读不动的存档记录。它们**不参与业务**，只是原样留着等 12 处置。
        self._unrecovered = []
        #: 拆除时**缓冲里还有东西**的处置报告。R05 未定，因此只留痕不动作；
        #: 留痕必须留在一个比 `PortState` 活得久的地方（见 `drop`）。
        self._disposed = []
        self.tick = 0
        self.stats = {
            "created": 0,
            "dropped": 0,
            "load_marks": 0,
            "projections": 0,
            "buffers_created": 0,
            "unrecovered": 0,
            "disposed": 0,
        }
        # -- 存档（12-04）------------------------------------------------
        #: 还没写进存档的接口 `node_key` 文本。**谁改状态谁调
        #: `note_changed`**，不由扫描兜底：漏标一次的表现是「改了没存」，
        #: 而那种缺陷要等到重进世界才看得见。
        self._dirty_ports = set()
        #: 已拆除、**待从存档里删掉**的接口。
        self._removed_ports = set()
        #: 从存档读回、还没等到对应设备被区块发现的接口载荷。
        #: `node_key 文本 -> 载荷 dict`。位置稳定，设备代次不持久，
        #: 因此必须等 `ensure()` 拿到新实例时按位置装上。
        self._pending_states = {}
        #: 信封版本过新、只能原样保留的接口记录：槽位/配置照最好猜测应用，
        #: 但**不写回也不删除**——写回会把只存在于新版本里的字段抹掉。
        self._frozen_ports = set()
        #: 载荷读不动的接口记录（未知结构 / 超出容量）。原样留档，等人工处置。
        self._unrecovered_records = []
        self.archive_stats = {
            "port_writes": 0,
            "port_deletes": 0,
            "restored_ports": 0,
            "disposal_writes": 0,
        }
        #: 待写盘的处置记录位置（12-06）。**只增不减**：它是历史事实。
        self._dirty_disposals = set()
        self._disposals = {}

    # -- 状态 ----------------------------------------------------------

    def ensure(self, ref, device_id=None, facing=None):
        # type: (object, str, object) -> PortState
        """取或建一台接口的状态。同一代次重复调用是幂等的。"""
        existing = self.get_by_ref(ref)
        if existing is not None:
            return existing
        record = self.registry.get_by_ref(ref)
        if device_id is None:
            device_id = record.device_id if record is not None else ""
        role = role_of(device_id)
        if not role:
            return None
        if facing is None and record is not None:
            facing = record.facing
        state = PortState(ref, device_id, role, PortConfig(), None, facing)
        if role == PASSIVE:
            # 被动接口：9 格是存取共用的**真实库存**，实际存放处是它自己的
            # 方块容器（`buffer_endpoint` 惰性挂上）。容量上界由容器与每件
            # 物品的 maxStackSize 决定，台账不再宣称一个件数上限。
            state.buffer = TransferBuffer(
                self.policy.template_buffer_capacity, ref.node_key.as_key()
            )
            state.buffer_kind = BUFFER_CONTAINER
            self.stats["buffers_created"] += 1
        elif role == TEMPLATE:
            # 样板接口：9 格**专用于工厂样板**（归任务 07），原料缓冲是
            # **独立的内存缓冲**，容量归 R05。两者从此不共用任何结构，
            # 「投料不得占用样板槽」因此是结构性的，不靠自觉。
            state.buffer = TransferBuffer(
                self.policy.template_buffer_capacity, ref.node_key.as_key()
            )
            state.buffer_kind = BUFFER_MEMORY
            self.stats["buffers_created"] += 1
        elif role == INTAKE:
            # 存货口：5 格**内部真实物品缓冲槽**（`blocks.md` 2026-09-29 由 9
            # 更正为 5；方块侧同日挂上 `container_size=5` 的网易容器）。
            # 与取货口那 5 格是**对称的同一个缺口**，也在 11-07 的 C1 名下，
            # 于 2026-09-29 与取货口一并补上。
            #
            # 存放形态同取货口：**方块容器**，因此外部模组直接动那 5 格时
            # 记账看得见。规格要求的流向是「朝向容器 → 缓冲 → 网络」，
            # 而 `dispatch_intake` 目前走的是**朝向容器 → 网络直输**
            # （06-05 尚未实现中转），所以本分支同样只立权威、不改行为。
            #
            # **对外那一侧仍是朝向的容器**：存货口从外面**吸**东西，
            # 那件事发生在朝向容器上，不该打到缓冲格——因此本角色不在
            # `BUFFER_AS_EXTERNAL_SIDE` 里（与取货口同理，理由相反）。
            state.buffer = TransferBuffer(
                self.policy.intake_buffer_capacity, ref.node_key.as_key()
            )
            state.buffer_kind = BUFFER_CONTAINER
            self.stats["buffers_created"] += 1
        elif role == OUTPUT:
            # 取货口：5 格**内部真实物品缓存槽**（`blocks.md`；方块侧已于
            # 2026-09-29 挂上 `container_size=5` 的网易容器）。这 5 格此前在
            # 领域层**没有任何权威来源**（11-07 的 C1），这里把它补上。
            #
            # 06-06 搬运以方块自己的容器为唯一库存权威，逐次读真实槽。
            # 此 TransferBuffer 仅保留旧归属/恢复结构，不把它的批次表当库存；
            # 外部模组直接改槽不能变成账上多出来的第二份物品。
            #
            # **建缓冲与「缓存启不启用」是两件事**，见 `PortConfig.cache_enabled`：
            # 关掉开关不该让这 5 格里的东西从账上消失，那正是 `blocks.md` 的
            # 「已有缓存物品的关闭处置仍待定，不得直接清空」。
            #
            # **对外那一侧仍是朝向的容器**，缓存只是中转站——因此本角色不在
            # `BUFFER_AS_EXTERNAL_SIDE` 里。「网络 → 缓存 → 朝向容器」这条
            # 中转路由由 PortDispatcher._cached_output 实现。
            state.buffer = TransferBuffer(
                self.policy.output_cache_capacity, ref.node_key.as_key()
            )
            state.buffer_kind = BUFFER_CONTAINER
            self.stats["buffers_created"] += 1
        self._register_capabilities(state)
        self._states[ref.node_key.as_key()] = state
        self.stats["created"] += 1
        # 存档里等着这台设备的配置/缓冲/批次：**按位置装**，位置稳定而代次不。
        self._apply_pending_state(state)
        return state

    def _apply_pending_state(self, state):
        # type: (PortState) -> bool
        """把存档里等着这台设备的接口状态装上。**只认位置，不认代次。**

        两道拒绝：
        - **型号不同**（同位置换成了别的方块）不装——那不是同一台设备，
          把取货口的缓存配置装到存货口上会让它按错误的语义工作；
        - 载荷读不动（`_unrecovered`）不装——**不当空配置**，原样留在档里。

        装完之后**不标脏**：刚读回来的东西立刻写回去是纯浪费，而且会让
        「读不懂的记录永不写回」那条闸多一条绕过的路径。
        """
        key = node_key_text(state.node_key)
        raw = self._pending_states.get(key)
        if raw is None:
            return False
        if raw.get("device_id") != state.device_id:
            # 位置还在，但换成了别的方块：记录**不装也不删**，等下一次
            # 那台新设备写出自己的记录时自然覆盖（12-06 的替换处置）。
            return False
        self._apply_archive(state, raw)
        self._pending_states.pop(key, None)
        return True

    def _register_capabilities(self, state):
        # type: (PortState) -> None
        """把**实际的能力对象**挂到这台设备上（架构 §4）。

        **注册范围取自定义表**，不是「凡是接口就两样都挂」：声明了
        `item_port` 才建物品端口，声明了 `fluid_port` 才建流体端口。
        两样都无条件挂上的话，一台只声明了流体的方块也会有一个「能收物品」
        的端口对象，而它背后没有任何实现——架构 §4 要的正是
        「已声明能力 == 已实例化能力对象」这个双向的一致。
        """
        definition = self.definitions.get(state.device_id) if self.definitions else None
        declared = set(definition.capabilities) if definition is not None else set()
        if not declared:
            # 定义表里查不到（离线夹具常这样）：按角色把对应的那一样挂上，
            # 而不是两样都挂——多挂一样就多一个说谎的对象。
            declared = set(
                [FLUID_PORT] if is_fluid_port(state.device_id) else [ITEM_PORT]
            )
        if ITEM_PORT in declared:
            state.item_port = PortItemPort(state, self)
            state.capabilities.register(ITEM_PORT, state.item_port)
        if FLUID_PORT in declared:
            state.fluid_port = PortFluidPort(state)
            state.capabilities.register(FLUID_PORT, state.fluid_port)

    def get(self, node_key):
        # type: (object) -> PortState
        state = self._states.get(node_key.as_key())
        if state is None or not is_port_device(state.device_id):
            return None
        return state

    def get_by_ref(self, ref):
        # type: (object) -> PortState
        """按实例代次取。**代次不符即失效**——这是延迟任务的自毁检查。"""
        state = self._states.get(ref.node_key.as_key())
        if state is None or state.ref.generation != ref.generation:
            return None
        return state

    def drop(self, ref):
        # type: (object) -> PortState
        """拆除：移除状态，**但缓冲里的东西不丢**。

        本轮不自行决定掉落还是保管（R05 未定，与 12-06 联动），
        因此把处置交给注入的策略，并且**只报告、不动作**。
        """
        state = self.get_by_ref(ref)
        if state is None:
            return None
        if state.buffer is not None and state.buffer.total() > 0:
            # **处置报告必须留在一个活得比 state 久的地方。**
            # 原来写在 state 上，而下一行就把 state 摘掉了——那份
            # 「缓冲里还有 N 件、现在归谁」的记录跟着对象一起没了，
            # R05 的「取消/拆除后余料处置」就等于没实现。
            report = self.disposition.dispose(state.buffer, "接口方块已拆除")
            report["node_key"] = state.node_key.as_key()
            report["role"] = state.role
            report["tick"] = self.tick
            self._disposed.append(report)
            self.stats["disposed"] += report.get("held", 0)
        key = node_key_text(state.node_key)
        if state.buffer is not None and state.buffer.total() > 0:
            # 处置报告落盘：接口记录马上要被删掉，这份留痕是「东西还在」
            # 的唯一凭据（见 `PORT_DISPOSAL_RECORD_KIND`）。
            self._disposals[key] = self._disposed[-1]
            self._dirty_disposals.add(key)
        self._states.pop(ref.node_key.as_key(), None)
        # 接口记录**删掉而不是写空**：同位置重放的新接口不得继承旧的配置、
        # 缓冲归属与批次（12 验收「同一位置拆后重放……不会自动继承」）。
        # 缓冲里剩下的东西已经记进 `_disposed`（R05 未定，只报告不动作），
        # 那份**不随记录一起删**——它是「东西还在，归谁待定」的唯一凭据。
        self._dirty_ports.discard(key)
        self._pending_states.pop(key, None)
        # 信封读不懂的那些**连删都不删**：那份存档不是本构建写的，
        # 凭什么断定「这台接口现在该从档案里消失」。
        if key not in self._frozen_ports:
            self._removed_ports.add(key)
        self.stats["dropped"] += 1
        return state

    def mark_loaded(self, node_key, loaded):
        # type: (object, bool) -> bool
        """区块加载状态变化。**缓冲与配置一字不改**。"""
        state = self.get(node_key)
        if state is None or state.loaded == bool(loaded):
            return False
        state.loaded = bool(loaded)
        self.stats["load_marks"] += 1
        return True

    def mark_all_unloaded(self):
        # type: () -> int
        """停机：全部标为未加载。**不写仓库、不清缓冲**。"""
        count = 0
        for state in self._states.values():
            if state.loaded:
                state.loaded = False
                count += 1
        return count

    def states(self):
        # type: () -> list
        return [
            self._states[key]
            for key in sorted(self._states, key=lambda item: tuple(item))
        ]

    def advance_clock(self, tick):
        # type: (int) -> int
        """推进批次时间戳。**不参与任何判定**，只给诊断排序用。

        **早退是必要的**：每个网络的存储服务每帧都会调它一次，而这个
        `PortStore` 是全进程共享的——不做判断的话，同一帧里会把同一个
        常量值向所有接口各写一遍，次数 = 网络数 × 接口数。
        """
        tick = int(tick)
        if tick == self.tick:
            return self.tick
        self.tick = tick
        for state in self._states.values():
            if state.buffer is not None:
                state.buffer.tick = self.tick
        return self.tick

    # -- 生命周期 ------------------------------------------------------

    def observe(self, change):
        # type: (object) -> int
        """从设备生命周期变化推导接口状态。**唯一入口**，与拓扑同一批变化。

        由启动层的 `_emit` 调用，因此不会出现「拓扑知道设备没了、
        接口还以为在」的分叉。
        """
        kind = change.kind
        device_id = change.device_id
        if kind == ChangeKind.ADDED:
            if not is_port_device(device_id):
                return 0
            state = self.ensure(change.ref, device_id)
            if state is None:
                return 0
            self.project(state)
            return 1
        if kind == ChangeKind.REMOVED:
            if not is_port_device(device_id):
                return 0
            return 1 if self.drop(change.ref) is not None else 0
        if kind in (ChangeKind.LOADED, ChangeKind.UNLOADED):
            if not is_port_device(device_id):
                return 0
            if self.mark_loaded(change.node_key, kind == ChangeKind.LOADED):
                state = self.get(change.node_key)
                if state is not None:
                    self.project(state)
                return 1
            return 0
        # NEIGHBORS_AFFECTED 与 PLACEMENT_REVIEW 是拓扑与交互层的事。
        return 0

    def project(self, state):
        # type: (PortState) -> bool
        """把权威配置投影到 `DeviceRecord.config["port"]`。

        **这是唯一写该字段的地方**：命令载荷不得直接提交槽位号或计数
        （架构 §4 的同一条纪律，`disk_count` 那边照此办理）。
        """
        record = self.registry.get(state.node_key)
        if record is None:
            return False
        payload = state.config.to_dict()
        if record.config.get("port") == payload:
            return False
        record.config["port"] = payload
        self.stats["projections"] += 1
        return True

    def configure(self, ref, config):
        # type: (object, PortConfig) -> bool
        """写入新配置并投影。返回是否真的变了。"""
        state = self.get_by_ref(ref)
        if state is None:
            return False
        state.config = config
        self.project(state)
        self.note_changed(state)
        return True

    def note_changed(self, state):
        # type: (PortState) -> None
        """标脏一台接口。**谁改状态谁调**，不由扫描兜底。

        与 `StorageDriverStore.note_medium_changed` 同形：漏标一次的表现是
        「改了没存」，而那种缺陷要等到重进世界才看得见。信封版本过新、
        只能原样保留的记录**跳过**（见 `_frozen_ports`）。
        """
        if state is None:
            return
        key = node_key_text(state.node_key)
        if key in self._frozen_ports:
            return
        # `_states` 的键是 `as_key()` 元组，而存档键是位置文本——两套键
        # 不能混用（早先这里拿文本去 `in self._states`，于是**永远为假**，
        # 一条记录都标不脏）。
        if self._states.get(state.node_key.as_key()) is not None:
            self._dirty_ports.add(key)
            # **撤销待删除**：同一局里「拆掉 → 又放一台」时，删除意图必须
            # 作废，否则保存顺序（先写后删）会让这台新设备的记录刚写下就被删。
            self._removed_ports.discard(key)

    # -- 容器 ----------------------------------------------------------

    def container_at(self, node_key):
        # type: (object) -> object
        """取某个位置的容器适配器。没有提供者时返回 None（**不是空容器**）。"""
        if self.containers is None:
            return None
        return self.containers.container_at(node_key)

    def facing_endpoint(self, state):
        # type: (PortState) -> tuple
        """朝向容器的端点。返回 `(endpoint, reason, detail)`。

        三种失败分开报，因为它们要玩家做的事完全不同：
        朝向未知 → 重新放置；那一面没东西 → 放个容器；有东西但不认识 → 等适配。
        """
        if not state.facing:
            return (
                None,
                Reason.FACING_UNKNOWN,
                "方块没有朝向状态，无法确定要往哪一面搬运",
            )
        target = state.target_key()
        if target is None:
            return (None, Reason.FACING_UNKNOWN, "朝向 %r 无法解析" % (state.facing,))
        record = self.registry.get(target)
        if record is not None and role_of(record.device_id) == TEMPLATE:
            # 样板接口有真实容器，但不能把它当作外部加工机器。
            # 通用容器写入会绕过样板准入校验，污染隔壁接口的样板槽。
            return (None, Reason.NO_CONTAINER, "朝向位置是样板接口，不能作为投料容器")
        adapter = self.container_at(target)
        if adapter is None:
            return (
                None,
                Reason.NO_CONTAINER,
                "朝向位置 %r 没有可用的容器（不是容器，或该类型尚未登记适配器）"
                % (target,),
            )
        return (
            ContainerEndpoint(adapter, "container:%s" % (target.as_key(),)),
            "",
            "",
        )

    def adjacent_endpoints(self, state):
        # type: (PortState) -> list
        """样板投料查询六面外部容器，跳过样板接口和订单管理的装配仓。"""
        endpoints = []
        for facing in FACING_NAMES:
            target = neighbor_key(state.node_key, facing)
            record = self.registry.get(target)
            if record is not None and (
                role_of(record.device_id) == TEMPLATE
                or record.device_id == "endernet:assemble_chamber"
            ):
                continue
            adapter = self.container_at(target)
            if adapter is not None:
                endpoints.append((facing, ContainerEndpoint(
                    adapter, "container:%s" % (target.as_key(),))))
        return endpoints

    def buffer_endpoint(self, state, op_id, origin=ORIGIN_INTERNAL):
        # type: (PortState, str, str) -> object
        """缓冲端点。

        **只有被动接口把方块容器挂成实际存放处。** 样板接口的原料缓冲是
        纯内存的（它的 9 格归任务 07 的工厂样板），因此这里不替它挂容器——
        早先两者共用一条挂载路径，那正是「投料占用样板槽」的根源。
        """
        if state.buffer is None:
            return None
        if (
            state.buffer.backing is None
            and state.buffer_kind == BUFFER_CONTAINER
        ):
            # 惰性挂上实际存放处：容器组件由资源包声明，运行时才知道能不能读。
            adapter = self.container_at(state.node_key)
            if adapter is not None:
                state.buffer.backing = ContainerEndpoint(
                    adapter, "container:%s" % (state.node_key.as_key(),)
                )
        return state.buffer.for_op(op_id, origin)

    def supply_view(self, state, slot):
        # type: (PortState, int) -> object
        """供货位/存货位在容器上的**按槽端点**（被动接口专用）。

        返回 `SlotView`；拿不到容器时返回 None（**不是空容器**）。
        """
        adapter = self.container_at(state.node_key)
        if adapter is None:
            return None
        return SlotView(
            adapter, slot, "port:%s" % (state.node_key.as_key(),)
        )

    def supply_slot_count(self, state):
        # type: (PortState) -> int
        """这台接口自己的槽位数。被动接口取定义表那条已定案的 9。"""
        adapter = self.container_at(state.node_key)
        if adapter is not None:
            counter = getattr(adapter, "slot_count", None)
            if callable(counter):
                return int(counter())
        if role_of(state.device_id) == PASSIVE:
            return int(PASSIVE_PORT_SLOT_COUNT.value)
        return int(TEMPLATE_PATTERN_SLOT_COUNT.value)

    # -- 存档 ----------------------------------------------------------

    def save_all(self):
        # type: () -> dict
        """导出全部接口状态。**缓冲物品本身不在这里**——它们在方块容器里，
        `to_dict()` 只导出归属台账与批次记录。"""
        return {
            "states": [state.to_dict() for state in self.states()],
        }

    def restore(self, payload):
        # type: (dict) -> int
        """从存档恢复配置、批次与缓冲归属台账。返回恢复的接口数。

        **不重建设备**：只对注册表里当前存在、型号仍是接口的实例恢复状态。
        存档里的代次只在同一进程内有意义（与 `runtime_record_id` 同一条注释），
        因此这里取注册表里的**当前**代次——跨重启的稳定身份属任务 12。

        读不懂的记录（未知 schema、超出容量）**不按空缓冲处理**：原样收进
        `unrecovered`，那个接口留空但计入 `stats["unrecovered"]`，
        等 12 决定保保留还是限制交易。这正是 05 明令禁止的「读成空盘再覆盖」
        的反面。
        """
        restored = 0
        for raw in (payload or {}).get("states", ()):
            node_key = NodeKey.from_dict(raw["node_key"])
            record = self.registry.get(node_key)
            # 型号对不上（同位置换成了别的方块）就不恢复——那不是同一台设备。
            if record is None or not is_port_device(record.device_id):
                continue
            state = self.ensure(record.ref, record.device_id, record.facing)
            if state is None:
                continue
            self._apply_archive(state, raw)
            self.project(state)
            restored += 1
        return restored

    def _apply_archive(self, state, raw):
        # type: (PortState, dict) -> bool
        """把一份接口载荷装到一台**已经存在**的 state 上。

        读不动的子项**不装**、原样留痕：把一份读不懂的批次表当空批次，
        等于把「外部机器投了一半」这件事抹成「没投过」。
        """
        if raw.get("config"):
            try:
                state.config = PortConfig.from_dict(raw["config"])
            except (KeyError, TypeError, ValueError) as error:
                self._note_bad_record(state, "config", error)
                return False
        # **待核对标记必须存**（12-07）：不确定的外部投料重进世界后
        # 仍然要停在那条通路上，不得因为「重开了一次」而开始自动重试。
        hold = raw.get("hold") or ""
        state.hold = hold
        state.hold_detail = raw.get("hold_detail") or ""
        if raw.get("batch"):
            try:
                state.batch = BatchState.from_dict(raw["batch"])
            except (KeyError, TypeError, ValueError) as error:
                self._note_bad_record(state, "batch", error)
        if not state.facing:
            # 朝向以世界读数为准；读不到时才退回存档里那一份（存档只是兜底）。
            state.facing = raw.get("facing")
        self._restore_buffer(state, raw)
        return True

    def _restore_buffer(self, state, raw):
        # type: (PortState, dict) -> bool
        if state.buffer is None or not raw.get("buffer"):
            return False
        try:
            buffer = TransferBuffer.from_dict(raw["buffer"])
        except (KeyError, TypeError, ValueError) as error:
            self._note_bad_record(state, "buffer", error)
            return False
        # 物品的实际存放处不随存档走（它在方块容器里），重新挂上。
        buffer.backing = state.buffer.backing
        state.buffer = buffer
        return True

    def _note_bad_record(self, state, field, error):
        # type: (PortState, str, object) -> None
        """读不懂的载荷：计数、留痕、原样保留。**绝不静默替换成空。**"""
        self.stats["unrecovered"] += 1
        entry = {
            "node_key": state.node_key.as_key(),
            "field": field,
            "detail": str(error),
        }
        self._unrecovered.append(entry)
        self._unrecovered_records.append(entry)

    def unrecovered(self):
        # type: () -> list
        """读不动的存档记录。**原样保留**，供任务 12 决定处置。"""
        return list(self._unrecovered)

    def disposed(self):
        # type: () -> list
        """拆除时余料的处置报告。**每条记着它当时还有多少、归谁。**

        R05 未定，因此这里只保留事实；任务 12 与 R05 定案后据此决定
        掉落、保管还是转交。**没有这张表就等于那批东西从诊断里消失。**
        """
        return [dict(item) for item in self._disposed]

    # -- 真实后端（12-04）-----------------------------------------------

    def pending_archive_writes(self):
        # type: () -> int
        """还没写进存档的接口/处置记录数。诊断与预算用。"""
        return (
            len(self._dirty_ports)
            + len(self._removed_ports)
            + len(self._dirty_disposals)
        )

    def save_to_repository(self, repository, budget=None):
        # type: (object, int) -> dict
        """把脏接口与处置记录写进存档，把拆掉的接口从档里删掉。

        返回本批写了多少条。**写成功之后才摘脏**：`repository.write` 抛异常
        （故障注入、版本过新）时记录留在脏里等下一轮重试——先摘脏再写会让
        那次改动永远丢掉。
        """
        written = 0
        for key in sorted(self._dirty_ports):
            if budget is not None and written >= budget:
                break
            if key in self._frozen_ports:
                self._dirty_ports.discard(key)
                continue
            state = self._by_text(key)
            if state is None:
                # 设备已经不在了（拆掉而没走 `drop`）。不写空，留待删除分支。
                self._dirty_ports.discard(key)
                continue
            repository.write(Record(PORT_RECORD_KIND, key, state.archive_payload()))
            self.archive_stats["port_writes"] += 1
            self._dirty_ports.discard(key)
            written += 1

        for key in sorted(self._dirty_disposals):
            if budget is not None and written >= budget:
                break
            report = self._disposals.get(key)
            if report is None:
                self._dirty_disposals.discard(key)
                continue
            repository.write(
                Record(PORT_DISPOSAL_RECORD_KIND, key, self._disposal_payload(report))
            )
            self.archive_stats["disposal_writes"] += 1
            self._dirty_disposals.discard(key)
            written += 1

        for key in sorted(self._removed_ports):
            if budget is not None and written >= budget:
                break
            repository.delete(PORT_RECORD_KIND, key)
            self._removed_ports.discard(key)
            self.archive_stats["port_deletes"] += 1
            written += 1

        return written

    def _disposal_payload(self, report):
        # type: (dict) -> dict
        """处置报告转成**纯数据**。

        `BufferDisposition.dispose()` 已经把批次表转成 dict（含
        `resource_key.to_dict()`），因此这里只需浅拷一份，不要再动里面的键——
        再转一次会让「谁负责序列化」变成两处，而两处迟早会分叉。
        """
        return dict(report)

    def restore_from_repository(self, repository):
        # type: (object) -> dict
        """从存档读回接口状态。返回统计。

        **不立刻建接口**：位置稳定而设备代次不持久，因此载荷进
        `_pending_states`，等 `ensure()` 拿到新实例时按 `node_key` 装上
        （与驱动器槽位同一条纪律）。

        信封版本过新 → 冻结（照最好猜测应用但不写回）；
        载荷读不动 → 进 `_unrecovered` 并**原样留在档里**，不当空配置。
        """
        stats = {"ports": 0, "frozen": 0, "disposals": 0, "missing": 0}
        for key in repository.list_ids(PORT_RECORD_KIND):
            record = repository.load(PORT_RECORD_KIND, key)
            if record is None or not isinstance(record.payload, dict):
                stats["missing"] += 1
                continue
            if record.read_only:
                self._frozen_ports.add(key)
                stats["frozen"] += 1
            self._pending_states[key] = record.payload
            stats["ports"] += 1
        for key in repository.list_ids(PORT_DISPOSAL_RECORD_KIND):
            record = repository.load(PORT_DISPOSAL_RECORD_KIND, key)
            if record is None or not isinstance(record.payload, dict):
                stats["missing"] += 1
                continue
            self._disposals[key] = record.payload
            stats["disposals"] += 1
        self.archive_stats["restored_ports"] += stats["ports"]
        return stats

    def _by_text(self, key):
        # type: (str) -> PortState
        for state in self._states.values():
            if node_key_text(state.node_key) == key:
                return state
        return None

    def archived_states(self):
        # type: () -> dict
        """还没等到设备的存档载荷（诊断用）。**只读快照。**"""
        return dict(self._pending_states)

    def frozen_ports(self):
        # type: () -> list
        return sorted(self._frozen_ports)

    def diagnostics(self):
        # type: () -> dict
        by_role = {}
        held = 0
        loaded = 0
        buffered = 0
        for state in self._states.values():
            by_role[state.role] = by_role.get(state.role, 0) + 1
            if state.hold:
                held += 1
            if state.loaded:
                loaded += 1
            if state.buffer is not None:
                buffered += state.buffer.total()
        return {
            "ports": len(self._states),
            "by_role": by_role,
            "held": held,
            "loaded": loaded,
            "buffered": buffered,
            "tick": self.tick,
            "stats": dict(self.stats),
            "unrecovered": self.unrecovered(),
            "disposed": self.disposed(),
            "policy": self.policy.describe(),
            "unresolved": self.policy.unresolved_report(),
            "disposition": self.disposition.policy_id,
            # -- 存档（12-04）--------------------------------------------
            "pending_archive_writes": self.pending_archive_writes(),
            #: 存档里有、还没等到设备的接口载荷。
            "pending_states": sorted(self._pending_states),
            #: 信封版本过新、只能原样保留的接口记录。**它们不进任何写回路径。**
            "frozen_ports": sorted(self._frozen_ports),
            "archive": dict(self.archive_stats),
        }

    def __repr__(self):
        return "PortStore(%d 台接口)" % (len(self._states),)


# -- 主控通道 --------------------------------------------------------------


class PortGateway(object):
    """接口把意图交给主控的**唯一通道**。

    这里只做三件事：向不向、什么时候、交给谁。判不判得下来是准入链的事，
    怎么做是存储服务的事——**本类不做任何业务判定**。
    """

    __slots__ = ("runtime",)

    def __init__(self, runtime=None):
        # type: (object) -> None
        self.runtime = runtime

    def available(self):
        # type: () -> bool
        """现在能不能把命令交出去。

        三个条件缺一不可：有运行时、运行时已开放业务、有主控。
        `NetworkRuntime.service()` 本来就会拒绝关闭中的运行时，但那条拒绝
        是**异常**，会在 `SchedulerService.tick` 里冒到帧边界上去刷屏
        （与 `bootstrap.drive_once` 的纪律冲突）。因此在这里先问一次。
        """
        runtime = self.runtime
        if runtime is None or getattr(runtime, "master", None) is None:
            return False
        opener = getattr(runtime, "is_open", None)
        return True if opener is None else bool(opener())

    def topology_version(self):
        # type: () -> object
        topology = getattr(self.runtime, "topology", None)
        return getattr(topology, "version", None)

    def submit(self, command):
        # type: (object) -> object
        """提交一条设备发起的命令。不可用时返回 None。"""
        if not self.available():
            return None
        return self.runtime.master.submit(command)


# -- 驱动 ------------------------------------------------------------------


class PortRunner(object):
    """按轮询驱动接口的搬运尝试。

    **它自己不动库存。** 每次尝试都构造一条设备发起的 `DeviceCommand` 交给
    主控（见模块 docstring）。因此：

    - 网络不能工作时这里连命令都发不出去（`PortGateway.available()`），
      验收「主控无效或断电不继续抽取源容器」是结构性的；
    - 重复请求被主控账本挡住，接口不需要自己维护第二张去重表。

    **退避不靠轮询本身。** 没搬动时返回 `changed=False`，让
    `SchedulerService` 的退避接管；只有真的动了才 `changed=True`。
    """

    #: 注册轮询时用的 owner。必须是本服务自己的 `service_id`，
    #: 否则 `SchedulerService` 会以 `rejected_owner` 拒绝注册。
    owner = "storage"

    def __init__(self, ports, gateway, coordinator=None):
        # type: (PortStore, PortGateway, object) -> None
        self.ports = ports
        self.gateway = gateway
        #: 每网络的转移协调器。由 `StorageService` 注入——接口状态是全局的，
        #: 而转移归属是每网络的，两者不能合并成一份。
        self.coordinator = coordinator
        #: `(node_key.as_key(), generation) -> True`：已注册过轮询的实例。
        #: **没有这张表就是缺陷**：`set_poll_handler` 每次调用都会把退避打回
        #: 初值（`ticking.py`），而 `add_node` 对每个成员反复调用，
        #: 直接在里面注册会让退避永远失效。
        self._registered = {}
        self.stats = {
            "registered": 0,
            "unregistered": 0,
            "dispatched": 0,
            "unavailable": 0,
        }

    # -- 注册 ----------------------------------------------------------

    def attach(self, node, scheduler):
        # type: (object, object) -> bool
        """给一台接口注册轮询。**同一实例只注册一次**。"""
        state = self.ports.get_by_ref(node.ref)
        if state is None:
            return False
        key = (node.node_key.as_key(), node.ref.generation)
        if key in self._registered:
            return False
        ok = scheduler.set_poll_handler(node.ref, self._poll, owner=self.owner)
        if not ok:
            return False
        self._registered[key] = node.ref
        self.stats["registered"] += 1
        return True

    def detach(self, node, scheduler):
        # type: (object, object) -> bool
        """注销轮询并撤销该实例的任务。**保留缓冲与配置。**"""
        key = (node.node_key.as_key(), node.ref.generation)
        if key not in self._registered:
            return False
        del self._registered[key]
        scheduler.set_poll_handler(node.ref, None)
        scheduler.cancel_all(node.ref)
        self.stats["unregistered"] += 1
        return True

    def wake(self, ref, scheduler):
        # type: (object, object) -> bool
        """**把该接口的轮询提前到本帧**，用于「参数刚刚改过」这类确定的变化。

        与 `note_wake` 的分工：那个是方块实体 tick 的心跳（只把退避打回初值，
        不把轮询提前——04 明确警告过拿它当变化信号会让开 tick 的设备退化成
        每帧扫描）；这个是**知道发生了变化**时用的，`SchedulerService.wake`
        就是为它准备的原语。配置变化走这条，因为那是玩家的明确动作。
        """
        if scheduler is None:
            return False
        return bool(scheduler.wake(ref))

    def note_wake(self, node_key):
        # type: (object) -> bool
        """方块实体 tick 到达。**只记一笔**，不改退避也不发起搬运。

        退避本身由 `SchedulerService.on_wake` 处理（它才是轮询节奏的归属）。
        这里记数是为了让「唤醒到过但没搬动」与「压根没醒过」在诊断里分得开。
        """
        state = self.ports.get(node_key)
        if state is None:
            return False
        state.stats["wakes"] += 1
        return True

    # -- 轮询 ----------------------------------------------------------

    def _poll(self, task):
        # type: (object) -> PollOutcome
        state = self.ports.get_by_ref(task.ref)
        if state is None:
            return PollOutcome(False, 0)
        state.stats["polls"] += 1

        # **一份门，四处共用**（这里是轮询、命令处理器、只读诊断）。
        # 重写一遍的代价是加新门时漏掉其中一处，而症状是「界面说正常、
        # 接口就是不动」。
        if port_blocker(state, self.ports.policy) is not None:
            state.stats["unchanged"] += 1
            return PollOutcome(False, 0)
        if not self.gateway.available():
            self.stats["unavailable"] += 1
            state.stats["unchanged"] += 1
            return PollOutcome(False, 0)

        return self._dispatch(state)

    def _dispatch(self, state):
        # type: (PortState) -> PollOutcome
        """构造并提交一条搬运命令。"""
        role = state.role
        if role in FACING_ROLES and role != TEMPLATE:
            endpoint, reason, detail = self.ports.facing_endpoint(state)
            if endpoint is None:
                state.stats["no_container"] += 1
                # 朝向容器的缺失是**环境问题**，不是「这条通路坏了」：
                # 放一个容器上去就该好。因此只计数、不进待核对。
                state.hold_detail = detail
                return PollOutcome(False, 0)

        command = self._build_command(state)
        result = self.gateway.submit(command)
        if result is None:
            self.stats["unavailable"] += 1
            return PollOutcome(False, 0)
        self.stats["dispatched"] += 1
        state.stats["dispatches"] += 1
        return self._note_result(state, result)

    def _build_command(self, state):
        # type: (PortState) -> DeviceCommand
        """一条**设备发起**的命令。

        `CommandSource.player_id` 为 None 是合法的：接口不是玩家，
        没有身份可言，而 `CommandSource` 不校验它。归属（`owner_id`）
        由存储服务在记账时按端口位置给出。
        """
        return DeviceCommand(
            state.next_request_id(),
            CommandKind.STORAGE,
            CommandSource(
                None, state.ref, state.node_key.dimension, distance_ok=True
            ),
            payload={
                "action": "port_dispatch",
                "intent": state.role,
            },
            # 图版本取**提交这一刻**的读数：接口每轮都会重新取一次，
            # 因此不会拿着旧图去执行（准入链第 6 条会拦下过期的）。
            topology_version=self.gateway.topology_version(),
            device_ref=state.ref,
        )

    def _note_result(self, state, result):
        # type: (PortState, object) -> PollOutcome
        """把命令结果翻译成「有没有变化」以及待核对状态。

        **回执必须从 payload 里读**，不能凭 `CommandResult.status` 推断：
        `UNCERTAIN` 被降级成 `blocked`（它确实不该算成功），而
        「要不要停住这条通路」只能由回执自己的 `status` 回答。
        读不到回执就当作没动过——**沉默不等于安全，但也不等于可以重试**，
        因此这里同时把「读不到回执」这件事记进 hold_detail 供诊断。
        """
        payload = result.payload or {}
        receipt = payload.get("receipt") or {}
        moved = int(receipt.get("moved", 0) or 0)
        status = receipt.get("status", "")
        if moved > 0:
            state.stats["moved"] += moved

        if status == "uncertain":
            # 06-02：不确定不能当作成功，也不能盲目重试。**把这条通路停住**，
            # 等人工或恢复流程销账——自动重试正是这里要防的东西。
            state.hold = Reason.CONTAINER_UNCERTAIN
            # **写一句人能读的话，不是原因码。** `hold_detail` 会进诊断与
            # 界面弹窗（11-13），把 `container_uncertain` 原样贴上去等于
            # 让玩家看一个内部标识。
            state.hold_detail = (
                "结果不确定，已停止这条通路：余量位置 %s。核对之前不会重试"
                % (receipt.get("holder") or "无法确认",)
            )
            state.stats["uncertain"] += 1
            state.stats["holds"] += 1
            # **待核对标记落盘**（12-07）：重进世界后这条通路仍要停着，
            # 不得因为「重开了一次」就开始自动重试。
            self.ports.note_changed(state)
            return PollOutcome(False, 0)

        if moved > 0:
            state.hold_detail = ""
            return PollOutcome(True, 1)

        if not result.is_applied():
            state.stats["refused"] += 1
            detail = ",".join(result.reason_codes())
            state.hold_detail = detail or "命令未产生效果，且回执缺失"
        state.stats["unchanged"] += 1
        # 没搬动 → 让退避拉长间隔。**这就是「不会无限忙轮询」的落点**：
        # 返回 changed=False，`SchedulerService` 会把间隔逐级拉大。
        return PollOutcome(False, 0)

    # -- 人工干预 ------------------------------------------------------

    def clear_hold(self, ref):
        # type: (object) -> tuple
        """人工解除待核对标记。返回 `(ok, detail)`。

        它**不做任何搬运**，只是允许这条通路重新尝试。为什么需要它：
        不确定的状态只能由「有人核对过」来销账，而自动重试不能替代核对
        ——没有这个入口，一条不确定就永久停用该接口。
        """
        state = self.ports.get_by_ref(ref)
        if state is None:
            return False, "目标位置上没有接口实例"
        if not state.hold:
            return False, "该接口没有待核对标记"
        detail = "已解除待核对标记（原因：%s）" % (state.hold,)
        state.hold = ""
        state.hold_detail = ""
        self.ports.note_changed(state)
        return True, detail

    def status(self, ref):
        # type: (object) -> dict
        state = self.ports.get_by_ref(ref)
        if state is None:
            return {"found": False}
        view = state.to_dict()
        view["found"] = True
        view["gateway_available"] = self.gateway.available()
        return view

    def diagnostics(self):
        # type: () -> dict
        return {
            "registered": len(self._registered),
            "coordinator": self.coordinator is not None,
            "gateway_available": self.gateway.available(),
            "stats": dict(self.stats),
            "scope_note": (
                "接口的轮询、退避、待核对与配置已实现；真正的搬运由存储服务"
                "经主控准入后执行。接触真实容器的引擎适配在 "
                "adapters/containers_engine.py，**未经游戏内实测**；"
                "流体路径依赖 13-03，尚未接入。"
            ),
        }

    def __repr__(self):
        return "PortRunner(%d 台已注册)" % (len(self._registered),)


#: 接口「能不能开工」的**唯一判定处**。
#:
#: 轮询、命令处理器、只读诊断三处都调它。分开写的代价是会分叉：
#: 加一条新门（比如 R05 的断电暂停）时改了其中两处，第三处照旧放行，
#: 而 `port_status` 报的原因与实际结果对不上——玩家看到「一切正常」
#: 但接口就是不动。
def port_blocker(state, policy=None):
    # type: (PortState, PortPolicy) -> object
    """一台接口当前为什么不工作。诊断、轮询与命令处理共用这一份措辞。

    `policy` 只在「空名单是什么意思」这一条上用到——那一条尚未定案，
    必须由策略回答，不能在这里写死。
    """
    if state is None:
        return BlockReason(Reason.DEVICE_GONE, "目标位置上没有接口实例")
    if not state.loaded:
        return BlockReason(
            Reason.DRIVER_UNLOADED, "接口所在区块未加载；这不代表它已被拆除"
        )
    if state.hold:
        return BlockReason(
            state.hold, state.hold_detail or "接口处于待核对状态，不发起搬运"
        )
    if is_fluid_port(state.device_id):
        # **这是「功能没做」，不是「配置不对」**：放在 `is_ready_for` 之前，
        # 否则一台没配置的流体接口会先报「尚未配置过滤」。
        return BlockReason(Reason.NO_ADAPTER, "流体路径尚未接入（13-03）")
    if not state.config.is_ready_for(state.role, policy):
        return BlockReason(
            Reason.PORT_UNCONFIGURED,
            "接口尚未配置要搬运什么（该角色必须显式指定过滤，或接口被关掉了）",
        )
    return None


def config_view(state, policy):
    # type: (PortState, PortPolicy) -> dict
    """一台接口的**配置视图**：界面要显示与编辑的那一份（11-07）。

    与 `PortState.to_dict()` **刻意分开**。那一份是**存档结构**（任务 12 的
    入口），带着整张批次表、缓冲明细与统计计数；把它直接当界面载荷发出去，
    既是白发的带宽，也会在客户端留下一份没人看的存档副本——更要命的是，
    界面一旦照着它写回，就把「存档长什么样」变成了第二个写者。

    这里只挑界面真正用得上的：配置本身、**该角色的上限**、**该角色允许的
    过滤模式**，以及当前为什么不工作。

    **上限与模式由策略回答，界面不硬编码。** 取货的 9 种是定案值，而存货口
    那个上限尚未定案（`PortPolicy.intake_filter_limit`，替身口径）；界面若
    自己抄一个数字，就是把替身悄悄变成定案。`filter_modes` 同理：取货口
    只接受白名单（`ROLE_FILTER_MODES`），界面不该自己判断。

    **不含任何版本字段。** `PortState` 上没有配置版本，而 `schema_version`
    是存档结构版本、`expected_index_version` 是交易版本——按仓库「两个版本
    必须各有其名」的纪律，两个都不得挪用到配置上（11-07 的「期望配置版本」
    因此仍未实现，见交付说明）。

    `buffer_summary` 只是给界面看的一句话，**不是**可写回的数据。

    ## `buffer_summary` 为什么是一句话而不是 `describe()`

    `TransferBuffer.describe()` 给的是一份**带统计计数**的字典（`stats` 里
    那几个数随每一次搬运都在变）。把它整份塞进载荷有两个后果，而且都不会
    在离线用例里露出来（夹具的缓冲是 `None`）：

    1. **线上编不出去**——`PortConfigMessage` 的校验要求这一项是文本，
       于是 `build_config_message` 抛 `TypeError`，被 `_publish_device_config`
       吞掉并计进 `config_errors`。**表现是配置推送一条都到不了客户端**，
       而界面上只是「什么都不显示」。
    2. 就算放开校验也**不该发**：发布去重按整个载荷比相等，而那串计数每帧
       都可能变，于是**每帧一个包**。

    2026-10-03 在真机上撞到的是第 1 条。这里因此只取「占用了多少」这一句
    稳定的话，计数留在 `PortState.to_dict()`（存档与诊断）里。
    """
    blocker = port_blocker(state, policy)
    buffer = state.buffer
    summary = None
    if state.device_id == "endernet:bus_item_output_io":
        summary = "5 格真实缓存，库存以方块容器为准"
    elif buffer is not None:
        described = buffer.describe()
        summary = "占用 %d/%d 单位物，%d 组" % (
            described["total"], described["capacity"], described["lots"])
    return {
        "device_id": state.device_id,
        "generation": state.ref.generation,
        "role": state.role,
        "config": state.config.to_dict(),
        "filter_limit": policy.filter_limit_for(state.role),
        # `sorted` 是为了让它成为**确定的顺序**：发布去重按整个载荷比相等，
        # 集合的迭代顺序一变就会白发一份「内容其实没变」的包。
        "filter_modes": sorted(ROLE_FILTER_MODES.get(state.role, FILTER_MODES)),
        "blocker": blocker.to_dict() if blocker is not None else None,
        "buffer_summary": summary,
    }


# -- 命令处理 --------------------------------------------------------------


class PortDispatcher(object):
    """把一台接口的「该干活了」翻译成一次实际搬运。

    **它是命令处理器，不是轮询器。** 轮询由 `PortRunner` 发起、经主控准入
    之后才走到这里，因此这里看到的一切都已经过了准入链——主控、能源、带宽、
    节点代次、控制器支持范围都在那之前判过了。这里只补准入链答不了的那几条：
    朝向容器在不在、过滤配了没有、缓冲里有没有东西。

    **顺序上的两条约定：**

    - 被动总线先导入后导出。反过来的话，同一轮里刚搬进来的东西会被立刻
      搬回去，看起来像在抖动。
    - 存货口与取货口的余量都回原地：存货口搬不完的留在**朝向容器**里
      （它的老家），取货口送不进去的退回**网络**（06 验收：「不丢弃余量」）。
      两条都靠协调器的 `return_to` 默认值（默认退回来源端）成立，
      这里不需要额外写回滚代码。
    """

    def __init__(self, ports, coordinator, network_endpoint):
        # type: (PortStore, object, object) -> None
        self.ports = ports
        self.coordinator = coordinator
        #: 网络端点。它是每网络一份的（协调器也是），因此由服务注入。
        self.network = network_endpoint
        self.stats = {"dispatches": 0, "moved": 0, "no_source": 0, "no_space": 0}

    # -- 入口 ----------------------------------------------------------

    def dispatch(self, state, request_id, topology_version=None):
        # type: (PortState, str, object) -> CommandResult
        """执行一次该角色该做的事。**不越权**：角色决定走哪条分支。"""
        role = state.role
        if is_fluid_port(state.device_id):
            # 流体真实适配属 13-03。**明确拒绝而不是假装能搬**：
            # 返回「搬了 0 个」会让它安静地什么都不做，玩家看不出区别。
            return CommandResult.blocked(
                request_id,
                [BlockReason(Reason.NO_ADAPTER, "流体路径尚未接入（13-03）")],
            )
        handlers = {
            PASSIVE: self.dispatch_passive,
            INTAKE: self.dispatch_intake,
            OUTPUT: self.dispatch_output,
            TEMPLATE: self.dispatch_template,
        }
        handler = handlers.get(role)
        if handler is None:
            return CommandResult.rejected(
                request_id,
                [BlockReason(Reason.UNSUPPORTED, "未登记的接口角色 %r" % (role,))],
            )
        self.stats["dispatches"] += 1
        result = handler(state, request_id, topology_version)
        # **改动才标脏**（12-04）：模拟、被拒、去重命中都不该产生写盘工作。
        # 判据是「真搬动了」或「这次派发涉及投料批次」——后者即使没搬动
        # 也会改 `batch.reason`，那也是要落盘的状态。
        moved = int(((result.payload or {}).get("receipt") or {}).get("moved", 0) or 0)
        if moved > 0 or state.batch is not None:
            self.ports.note_changed(state)
        return result

    # -- 被动总线（06-04）----------------------------------------------

    def dispatch_passive(self, state, request_id, topology_version=None):
        # type: (PortState, str, object) -> CommandResult
        """被动接口：外部物流写进它的 9 格，网络按配置与它交换。

        **两个方向作用在不相交的槽上**，这就是「不来回搬」的根据：

        - 存货（外界 → 网络，目标「保持空」）只扫**存货位**（供货位的补集）；
        - 取货（网络 → 外界，目标「保持满」）只写**供货位**。

        刚补进去的供货物落在供货位上，而导入只看存货位——**它不可能被
        自己搬回网络**。这条不变量是结构性的，不是靠一层判断维持的。

        早先的实现两个方向共用一份过滤与一份缓冲，导出刚放进去的东西
        下一轮就被导入看见并搬回网络，在真机上表现为无休止的来回搬。
        """
        attempts = []
        if state.config.allows(DIRECTION_IMPORT):
            attempts.append(self._passive_import)
        if state.config.allows(DIRECTION_EXPORT):
            attempts.append(self._passive_export)
        for attempt in attempts:
            result = attempt(state, request_id, topology_version)
            if result is not None:
                return result
        return self._idle(request_id, PASSIVE, "配置的过滤里没有可搬运的资源")

    def _passive_import(self, state, request_id, topology_version):
        # type: (PortState, str, object) -> object
        """存货位 → 网络。**只扫存货位**（`supply_slots` 的补集）。"""
        slot_count = self.ports.supply_slot_count(state)
        limit = state.config.effective_limit(self.ports.policy)
        for slot in state.config.intake_slots(slot_count):
            endpoint = self.ports.supply_view(state, slot)
            if endpoint is None:
                return CommandResult.blocked(
                    request_id,
                    [BlockReason(Reason.NO_ADAPTER, "被动接口所在的方块不是容器")],
                )
            view = endpoint.slot_view()
            if view["key"] is None or not state.config.matches(
                view["key"], self.ports.policy
            ):
                continue
            take = min(view["count"], limit)
            if take <= 0:
                continue
            # **外部放进来的**，因此来源是 EXTERNAL：它进网络时要发一次回流。
            return self._pump(
                request_id, view["key"], take, endpoint, self.network,
                ORIGIN_EXTERNAL, topology_version,
            )
        self.stats["no_source"] += 1
        return None

    def _passive_export(self, state, request_id, topology_version):
        # type: (PortState, str, object) -> object
        """网络 → 供货位。**只写供货位**（`supply_slots`）。

        一格一格试：空槽或同种且还有余量才写。某一格放不下就换下一格，
        没有可用的供货位就退避——**不硬塞、不超额**。
        """
        limit = state.config.effective_limit(self.ports.policy)
        for slot in state.config.supply_slots:
            endpoint = self.ports.supply_view(state, slot)
            if endpoint is None:
                return CommandResult.blocked(
                    request_id,
                    [BlockReason(Reason.NO_ADAPTER, "被动接口所在的方块不是容器")],
                )
            view = endpoint.slot_view()
            # 这一格里如果是**外部放的东西**，不要覆盖它：那一格此刻是存货位。
            if view["key"] is not None and not state.config.matches(
                view["key"], self.ports.policy
            ):
                continue
            for key in state.config.filter:
                if view["key"] is not None and view["key"] != key:
                    continue
                available = self._available_of(key)
                if available <= 0:
                    continue
                take = min(available, limit)
                space = endpoint.probe_space(key, take)
                if space is not None:
                    take = min(take, int(space))
                if take <= 0:
                    continue
                # **网络供给的**，因此来源是 INTERNAL：不进网络，也就不发回流。
                return self._pump(
                    request_id, key, take, self.network, endpoint,
                    ORIGIN_INTERNAL, topology_version,
                )
        self.stats["no_space" if state.config.supply_slots else "no_source"] += 1
        return None

    # -- 存货口（06-05）------------------------------------------------

    def dispatch_intake(self, state, request_id, topology_version=None):
        # type: (PortState, str, object) -> CommandResult
        """读朝向容器的可提取内容，向主控请求存入网络。

        过滤为空时**取全部**——blocks.md 对存货口的原话是「可以存入**任意**
        物品/流体」，因此空过滤在这里是有意义的配置，而不是缺省
        （`PortConfig.is_ready_for` 按角色区分这件事）。
        """
        endpoint, reason, detail = self.ports.facing_endpoint(state)
        if endpoint is None:
            return CommandResult.blocked(request_id, [BlockReason(reason, detail)])
        candidates = self._intake_candidates(state, endpoint)
        if not candidates:
            self.stats["no_source"] += 1
            return self._idle(request_id, INTAKE, "朝向容器里没有可提取的内容")
        key, have = candidates[0]
        limit = min(have, state.config.effective_limit(self.ports.policy))
        return self._pump(
            request_id, key, limit, endpoint, self.network, ORIGIN_EXTERNAL,
            topology_version,
        )

    def _intake_candidates(self, state, endpoint):
        # type: (PortState, object) -> list
        """`[(key, 可提取量)]`，**按配置顺序**而不是容器顺序。

        顺序固定是有意的：容器里通常不止一种东西，每轮只搬一种意味着
        「先搬什么」会决定多轮之后谁先搬完。按玩家的配置顺序走，
        结果是可解释的；按容器槽位顺序走则取决于谁先被放进去。

        **白名单与黑名单在这里分开**：白名单按配置顺序走（那是玩家排的），
        黑名单没有顺序可言，按资源指纹的**稳定顺序**走——稳定即可复现，
        而不是「看容器里谁先被放进去」。
        """
        totals = {}
        for _, entry in endpoint.adapter.entries():
            totals[entry.resource_key] = (
                totals.get(entry.resource_key, 0) + entry.count
            )
        if state.config.filter_mode == MODE_BLACKLIST:
            # 黑名单没有「配置顺序」可言（名单里的是**不要**的），
            # 因此按资源指纹的稳定顺序走。
            return sorted(
                (
                    (key, count)
                    for key, count in totals.items()
                    if state.config.matches(key, self.ports.policy)
                ),
                key=lambda item: item[0].canonical(),
            )
        out = []
        for key in state.config.filter:
            count = totals.get(key, 0)
            if count > 0:
                out.append((key, count))
        return out

    # -- 取货口（06-06）------------------------------------------------

    def dispatch_output(self, state, request_id, topology_version=None):
        # type: (PortState, str, object) -> CommandResult
        """按过滤从网络请求资源，供给朝向容器。

        **先问目标还能放多少，再决定取多少。** 目标支持模拟时用它给的数字；
        不支持时退到 `PortPolicy.probe_unit`（保守的 1 个）——多搬的那部分
        虽然会被协调器退回网络，但那是一次真实的往返，
        在额度受限的介质上是要花代价的。
        """
        endpoint, reason, detail = self.ports.facing_endpoint(state)
        if endpoint is None:
            return CommandResult.blocked(request_id, [BlockReason(reason, detail)])
        if state.config.cache_enabled and state.device_id == "endernet:bus_item_output_io":
            return self._cached_output(state, request_id, endpoint, topology_version)
        limit = state.config.effective_limit(self.ports.policy)
        for key in state.config.filter:
            available = self._available_of(key)
            if available <= 0:
                continue
            wanted = min(available, limit)
            space = endpoint.probe_space(key, wanted)
            if space is None:
                # 目标不支持模拟：**只试探一个**。宁可慢，也不要搬一趟又退回。
                wanted = min(wanted, self.ports.policy.probe_unit)
            elif int(space) <= 0:
                self.stats["no_space"] += 1
                continue
            else:
                wanted = min(wanted, int(space))
            if wanted <= 0:
                continue
            return self._pump(
                request_id, key, wanted, self.network, endpoint,
                ORIGIN_INTERNAL, topology_version,
            )
        self.stats["no_source"] += 1
        return self._idle(request_id, OUTPUT, "网络里没有配置的过滤资源")

    def _cached_output(self, state, request_id, target, topology_version):
        # type: (PortState, str, object, object) -> CommandResult
        """一次只搬一段，复用主控准入和协调器的回执、回退与去重。

        真实容器是库存权威；不从 TransferBuffer 批次表推算可用量。
        玩家/外部模组可直接改槽，因此不能把批次表恢复成第二份物品。
        不在名单中的余货保留；开关关闭后本方法根本不调用。
        """
        if not state.config.filter:
            return self._idle(request_id, OUTPUT, "网络里没有配置的过滤资源")
        adapter = self.ports.container_at(state.node_key)
        if adapter is None or adapter.slots_count() != ITEM_OUTPUT_PORT_SLOT_COUNT.value:
            return CommandResult.blocked(
                request_id, [BlockReason(Reason.NO_ADAPTER, "取货口需要自身的 5 格真实容器")]
            )
        cache = ContainerEndpoint(adapter, "container:%s" % (state.node_key.as_key(),))
        if not cache.can_read() or not cache.can_write():
            return CommandResult.blocked(
                request_id, [BlockReason(Reason.NO_ADAPTER, "取货缓存不可读写")]
            )
        limit = state.config.effective_limit(self.ports.policy)
        totals = {}
        for _, entry in adapter.entries():
            totals[entry.resource_key] = totals.get(entry.resource_key, 0) + entry.count
        # 开发替身 demand_first：先送旧货，不在同一命令里再扣一次网络。
        for key in state.config.filter:
            wanted = self._output_space(target, key, min(totals.get(key, 0), limit))
            if wanted > 0:
                result = self._pump(
                    request_id, key, wanted, cache, target,
                    ORIGIN_INTERNAL, topology_version,
                )
                result.payload["route"] = "cache_to_target"
                return result
        for key in state.config.filter:
            # 同种旧货没送出去时不继续积压；目标满/缺失不预取。
            if totals.get(key, 0) > 0:
                continue
            wanted = self._output_space(target, key, min(self._available_of(key), limit))
            wanted = self._output_space(cache, key, wanted)
            if wanted > 0:
                result = self._pump(
                    request_id, key, wanted, self.network, cache,
                    ORIGIN_INTERNAL, topology_version,
                )
                result.payload["route"] = "network_to_cache"
                return result
        return self._idle(request_id, OUTPUT, "缓存或网络无可输出资源，或容器无空间")

    def _output_space(self, endpoint, key, wanted):
        # type: (object, ResourceKey, int) -> int
        if wanted <= 0:
            return 0
        space = endpoint.probe_space(key, wanted)
        if space is None:
            return min(wanted, self.ports.policy.probe_unit)
        return max(0, min(wanted, int(space)))

    # -- 样板口（06-07）------------------------------------------------

    def dispatch_template(self, state, request_id, topology_version=None):
        # type: (PortState, str, object) -> CommandResult
        """把已经备好的批次依次尝试投给六面相邻容器。

        **投不完就留着，不重来。** 外部机器只收了一部分时，剩余量留在
        中转位，批次状态记着「已投多少、还差多少」——06-07 明写后续推进
        交由 09 决定，本模块**不替它做决定**（不重投、不取消、不改配方）。
        """
        returned = self._template_return(state, request_id, topology_version)
        if returned is not None:
            return returned
        batch = state.batch
        if batch is None or batch.is_complete():
            return self._idle(request_id, TEMPLATE, "没有待投的样板批次")
        endpoints = self.ports.adjacent_endpoints(state)
        if not endpoints:
            return CommandResult.blocked(request_id, [BlockReason(
                Reason.NO_CONTAINER, "六个相邻面都没有可用的投料容器")])
        buffer_endpoint = self.ports.buffer_endpoint(state, batch.op_id, ORIGIN_INTERNAL)
        if buffer_endpoint is None:
            return CommandResult.blocked(
                request_id, [BlockReason(Reason.NO_ADAPTER, "该接口没有可用的中转位")]
            )
        staged = state.buffer.total_of(batch.resource_key)
        if staged <= 0:
            # 中转位空了但批次没完成：**不猜**。这可能意味着备料没成功、
            # 或者东西被别的路径搬走了，两种都要 09 重新决定。
            batch.reason = "中转位里已经没有这批资源，批次无法继续"
            return CommandResult.blocked(
                request_id, [BlockReason(Reason.INSUFFICIENT_STOCK, batch.reason)]
            )
        wanted = min(staged, batch.remaining(), state.config.effective_limit(self.ports.policy))
        for facing, endpoint in endpoints:
            # 各面的尝试必须有独立身份，否则拒收回执会被下一面重放。
            result = self._pump(
                "%s/face:%s" % (request_id, facing), batch.resource_key,
                wanted, buffer_endpoint, endpoint, ORIGIN_INTERNAL,
                topology_version,
            )
            receipt = (result.payload or {}).get("receipt", {})
            # 一面接收后结束本轮；部分接收的余量下轮继续尝试六面。
            # 不确定时立即停下，交由 PortRunner 设置待核对标记。
            if int(receipt.get("moved", 0) or 0) > 0 or receipt.get("status") == "uncertain":
                break
        result.request_id = request_id
        moved = int((result.payload or {}).get("receipt", {}).get("moved", 0) or 0)
        batch.delivered += moved
        if moved <= 0:
            batch.reason = "外部机器没有接收"
        elif not batch.is_complete():
            batch.reason = "外部机器只接收了一部分，余量留在中转位"
        else:
            batch.reason = ""
        result.payload["batch"] = batch.to_dict()
        return result

    def _template_return(self, state, request_id, topology_version):
        # type: (PortState, str, object) -> object
        """只从第二行回流槽搬入网络，并发布外部产物回流回执。"""
        adapter = self.ports.container_at(state.node_key)
        if adapter is None:
            return None
        slot_count = adapter.slots_count()
        if slot_count is None:
            return None
        for slot in range(TEMPLATE_PATTERN_SLOT_COUNT.value,
                          min(slot_count, TEMPLATE_CONTAINER_SLOT_COUNT.value)):
            endpoint = self.ports.supply_view(state, slot)
            if endpoint is None:
                return None
            view = endpoint.slot_view()
            if view["key"] is None or view["count"] <= 0:
                continue
            take = min(view["count"], state.config.effective_limit(self.ports.policy))
            return self._pump(request_id, view["key"], take, endpoint, self.network,
                              ORIGIN_EXTERNAL, topology_version)
        return None

    def stage_batch(self, state, key, count, op_id, request_id, topology_version=None):
        # type: (PortState, ResourceKey, int, str, str, object) -> CommandResult
        """成批备料：网络 -> 中转位（06-07 的投料入口，由 09 调用）。

        调用方给出 `op_id`，因此**同一次备料重发不会备第二遍**——
        主控账本与协调器账本各挡一次。
        """
        batch = BatchState(op_id, key, count)
        state.batch = batch
        buffer_endpoint = self.ports.buffer_endpoint(state, op_id, ORIGIN_INTERNAL)
        if buffer_endpoint is None:
            return CommandResult.blocked(
                request_id, [BlockReason(Reason.NO_ADAPTER, "该接口没有可用的中转位")]
            )
        result = self._pump(
            request_id, key, count, self.network, buffer_endpoint,
            ORIGIN_INTERNAL, topology_version,
        )
        moved = int((result.payload or {}).get("receipt", {}).get("moved", 0) or 0)
        batch.staged = moved
        if not batch.is_complete():
            batch.reason = "备料未完成，批次已保留"
        result.payload["batch"] = batch.to_dict()
        self.ports.note_changed(state)
        return result

    # -- 公共 ----------------------------------------------------------

    def _available_of(self, key):
        # type: (ResourceKey) -> int
        """网络里这种资源**可用**（扣掉别人的预留）的数量。

        走存储服务的查询面，不自己读 `item_totals`——预留的口径只有一处
        （05 的 `query()`），绕过它会让取货口把别人预留的东西搬走。
        """
        return int(self.network.storage.query(key)["available"])

    def _pump(self, request_id, key, count, source, target, origin, topology_version):
        # type: (str, ResourceKey, int, object, object, object, object) -> CommandResult
        """一次搬运：预检 + 提交，并把回执包成 `CommandResult`。

        `return_to` 用默认值（退回来源端）：
        - 存货口：网络拒收的部分退回**朝向容器**——它本来的家；
        - 取货口：直输/补货的余量回网络，缓存输出的余量回真实缓存。
        """
        request = TransferRequest(
            request_id,
            source.endpoint_id(),
            target.endpoint_id(),
            key,
            count,
            topology_version,
            owner_id="port:%s" % (self.network.network_id or "",),
            origin=origin,
        )
        receipt = self.coordinator.commit(request, source, target)
        if receipt.moved > 0:
            self.stats["moved"] += receipt.moved
        return self._receipt_result(request_id, receipt)

    def _receipt_result(self, request_id, receipt):
        # type: (str, object) -> CommandResult
        """回执 -> 命令结果。`stale` / `rejected` / `blocked` 的分流在
        `transfer.receipt_result()` 里，**接口与存储服务共用同一套口径**——
        各写一份的代价是同一个失败在两处给出不同的下一步提示。"""
        return receipt_result(request_id, receipt, "port_dispatch")

    def _idle(self, request_id, intent, note):
        # type: (str, str, str) -> CommandResult
        """没有可做的事。**这是一条成功结果**：没搬动不是错误，
        轮询会据此拉长退避（`changed=False`），这正是「不会无限忙轮询」的落点。"""
        return CommandResult.ok(
            request_id, {"action": "port_dispatch", "intent": intent, "moved": 0, "note": note}
        )

    def diagnostics(self):
        # type: () -> dict
        return dict(self.stats)
