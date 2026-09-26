# coding=utf-8
"""请求、响应、回执与增量通知的共用词汇。

对应任务 03-01/03-05 的协议部分，以及 13-01 统一容器契约中**与引擎无关**的那一半。

放在 common 层是有意的：转移回执类型会被存储与工厂服务引用，
若把它写在 `server/adapters/containers.py` 里，领域层为了拿到回执类型
就得 import 一个拖进 `mod.server.*` 的模块，离线测试立刻失效。
适配器**实现**仍在 adapters 下。

约定：所有会改变网络资源的操作都返回 `CommandResult`，
「成功」不等于「全部完成」，部分完成必须显式表示为 `STATUS_PARTIAL`，
并按实际数量提交（架构规划 6.3）。
"""

from .resource_key import ResourceKey, _TEXT_TYPES, _require_count
from . import settings

# -- 结果状态 --------------------------------------------------------------

#: 完全按请求完成。
STATUS_OK = "ok"
#: 只完成了请求的一部分；`payload` 必须给出实际完成量。
STATUS_PARTIAL = "partial"
#: 运行条件不满足，稍后重试可能成功（无主控、超载、断电、重建中）。
STATUS_BLOCKED = "blocked"
#: 请求所依据的实例或拓扑版本已失效，必须重新取快照后重发。
STATUS_STALE = "stale"
#: 请求本身非法（越权、越界、参数错误、设备类型不支持）。
STATUS_REJECTED = "rejected"
#: 同一 request_id 的请求正在处理中。
STATUS_IN_FLIGHT = "in_flight"

ALL_STATUSES = frozenset(
    [
        STATUS_OK,
        STATUS_PARTIAL,
        STATUS_BLOCKED,
        STATUS_STALE,
        STATUS_REJECTED,
        STATUS_IN_FLIGHT,
    ]
)

#: 只有这两种状态表示请求被受理并产生了实际效果。
APPLIED_STATUSES = frozenset([STATUS_OK, STATUS_PARTIAL])


# -- 阻塞/拒绝原因码 -------------------------------------------------------


class Reason(object):
    """可定位的原因码。

    任务 02-07 要求「不把业务错误仅显示为离线」，因此每个不成功的
    结果都必须带上具体原因，而不是一个笼统的失败。
    """

    # 拓扑与主控（03）
    NO_CONTROLLER = "no_controller"
    CONTROLLER_CONFLICT = "controller_conflict"
    TOPOLOGY_UNKNOWN = "topology_unknown"
    REBUILDING = "rebuilding"
    NOT_A_CANDIDATE = "not_a_candidate"
    #: 控制器实例存在但没有主控运行时接管它（运行时尚未建立或刚失效）。
    MASTER_ABSENT = "master_absent"
    #: 主控实例的区块未加载。**不等于控制器已被拆除**（架构规划 6.1）：
    #: 图保留已知拓扑，但业务必须暂停，也不能因此绕过「最多 1 个」的限制。
    MASTER_UNLOADED = "master_unloaded"
    #: 主控恢复流程尚未完成（启动恢复 / 迁移校验中）。
    MASTER_RECOVERING = "master_recovering"
    #: 该设备型号不被当前主控支持。blocks.md 给了控制器各自能控制的方块集合，
    #: 「拥有主控」不等于「支持全部功能」。
    CONTROLLER_DOES_NOT_SERVE = "controller_does_not_serve"
    #: 命令的目标设备不属于本网络。请求被路由到了错误的运行时。
    WRONG_NETWORK = "wrong_network"

    # 拓扑结构变化（02-07）
    COMPONENT_SPLIT = "component_split"
    COMPONENT_MERGED = "component_merged"
    PROCESSOR_GROUP_UNATTACHED = "processor_group_unattached"
    PROCESSOR_GROUP_UNRESOLVED = "r03_processor_group_ownership"
    PORT_MISMATCH = "port_mismatch"
    UNLOADED_MEMBER = "unloaded_member"
    #: 异容量网络基带相邻。blocks.md 明确「8/16/32 位基带只与同容量基带
    #: 直接连通」，不同容量即使贴在一起也不建边、不合并网络与负载域。
    #: 注意这与「同一控制器分别接两条不同容量的独立分支」是两回事，后者允许。
    BASEBAND_CAPACITY_MISMATCH = "baseband_capacity_mismatch"

    # 带宽（02）
    BANDWIDTH_DENIED = "bandwidth_denied"
    BANDWIDTH_UNKNOWN = "bandwidth_unknown"
    BANDWIDTH_UNCONFIGURED = "bandwidth_unconfigured"
    BANDWIDTH_OVERLOADED = "bandwidth_overloaded"
    #: 单台设备的需求就超过整个负载域的总容量——域**还空着**也放不下它。
    #: 与 `BANDWIDTH_OVERLOADED`（被别的设备挤掉、域确实满了）是两回事：
    #: 都拒绝，但界面必须分开说，否则会告诉玩家「已满载」而他看到的是一个空域。
    BANDWIDTH_DEMAND_EXCEEDS_DOMAIN = "bandwidth_demand_exceeds_domain"
    NO_BANDWIDTH_DOMAIN = "no_bandwidth_domain"
    #: 多接入待定（R02）：同一设备同时落在多个互不相连的负载域上，
    #: 归属未定，不擅自选一个。
    MULTI_DOMAIN_DEVICE = "r02_multi_domain_device"

    # 能源（04）
    BROWNOUT = "brownout"
    ENERGY_UNAVAILABLE = "energy_unavailable"
    ENERGY_UNCONFIGURED = "energy_unconfigured"

    # 身份与版本（03-05）
    DEVICE_GONE = "device_gone"
    STALE_INSTANCE = "stale_instance"
    STALE_VERSION = "stale_version"
    DUPLICATE_REQUEST = "duplicate_request"

    # 请求校验（03-04）
    NOT_PERMITTED = "not_permitted"
    OUT_OF_RANGE = "out_of_range"
    BAD_PARAMETER = "bad_parameter"
    UNSUPPORTED = "unsupported"
    #: 会话已失效（终端关闭、玩家离开、会话被顶替）。与「越权」分开：
    #: 前者要重新开会话，后者是权限问题（架构规划 6.6）。
    SESSION_INVALID = "session_invalid"
    #: 处理过程中抛出了异常。只在 `ApplicationService` 这一层使用——
    #: 引擎每帧调用数十次，把异常抛回去会刷屏并让后续帧全废
    #: （与 `ServerBootstrap.tick()` 同一条纪律）。**主控内部不吞异常**：
    #: 缺陷要在自己的层次上炸出来，不能伪装成一条业务失败。
    INTERNAL = "internal_error"

    # 服务装配（03-01）
    #: 所需业务服务未装配（开发档下的替身、或该功能尚未实现）。
    #: **不是 0、也不是「没有需求」**——未实现与已实现但不满足是两回事。
    SERVICE_ABSENT = "service_absent"
    #: 服务已装配但尚未就绪（依赖未满足、初始化未完成）。
    SERVICE_NOT_READY = "service_not_ready"
    #: 该判定依赖尚未定案的玩法规则（R02/R03/R13 等），正式档拒绝用替身值代替。
    ASSEMBLY_UNRESOLVED = "assembly_unresolved"

    # 存储与转移（05/06）
    NO_CAPACITY = "no_capacity"
    TYPE_LIMIT_REACHED = "type_limit_reached"
    INSUFFICIENT_STOCK = "insufficient_stock"
    RATE_LIMITED = "rate_limited"

    # 转移与接口（06）
    #: 有界中转缓冲放不下。与 `NO_CAPACITY`（介质容量不足）分开：
    #: 前者说的是**接口这台设备的中转位**满了，后者说的是网络存不下。
    #: 两条都要能各自显示，否则玩家看到「网络没容量」而实际卡在接口上。
    BUFFER_FULL = "buffer_full"
    #: 接口还没配置过滤/方向，因此没有可执行的动作。**不是错误**，
    #: 是「还没告诉它要搬什么」——与「搬不动」是两回事。
    PORT_UNCONFIGURED = "port_unconfigured"
    #: 朝向未知或朝向那面没有方块。方块缺 `minecraft:cardinal_direction`
    #: 或该位置为空都归这里，界面要提示玩家重新放置/放置容器。
    FACING_UNKNOWN = "facing_unknown"
    #: 朝向位置有方块，但没有可用的容器适配器（不是容器、或适配器未登记）。
    #: 与 `NO_ADAPTER` 的区别：那个是「这个类型我们没做适配」，
    #: 这个是「那个位置根本不是容器」。
    NO_CONTAINER = "no_container"
    #: 请求依据的槽位内容已经变了（06-09 的槽锚失效）。
    #: **必须与 `STALE_VERSION` 分开**：后者是版本号过期，可以取新快照重发；
    #: 这里说的是「你看到的那个槽现在已经不是那样了」，重发等于误操作。
    SLOT_CHANGED = "slot_changed"
    #: R12 尚未定案：玩家手里还拿着东西时的退出/断线处置没有规则。
    #: 本轮**不接管光标持有物**，遇到就明确拒绝而不是猜一个行为。
    CURSOR_UNRESOLVED = "r12_cursor_held"
    #: 目标端点已失效（邻接容器被拆、维度变化、依赖关闭）。
    ENDPOINT_INVALID = "endpoint_invalid"
    #: 该方向/该资源在这个接口上没有配置，或配置与设备能力不匹配。
    PORT_DIRECTION_MISMATCH = "port_direction_mismatch"

    # 接口配置（06 / 11-07）
    #: 配置的过滤种类数超过该角色的上限（取货为 9 种）。
    #: 与 `OUT_OF_RANGE` 分开：界面要能直接说「最多 9 种」，
    #: 而不是让玩家自己去数。
    FILTER_LIMIT_REACHED = "filter_limit_reached"
    #: 该角色不接受这种过滤模式（取货只接受白名单，存货才支持黑白名单）。
    FILTER_MODE_MISMATCH = "filter_mode_mismatch"
    #: 供货位配置不合法（越界、重复，或该角色没有共享槽可分配）。
    SUPPLY_SLOTS_INVALID = "supply_slots_invalid"

    # 介质与驱动器（05）
    #: 目标物品不是已登记的介质型号（R10 决定最终接受范围）。
    #: 与 `UNSUPPORTED` 分开：这里说的是**这个物品不对**，不是这个功能没做。
    NOT_A_MEDIUM = "not_a_medium"
    #: 同一介质被要求挂到第二处。同一介质只有一份权威状态，不重复计入库存。
    MEDIUM_DUPLICATE_MOUNT = "duplicate_medium_mount"
    #: 该介质并不挂在当前驱动器上。与「物品不是介质」是两回事：
    #: 前者是挂载状态不对，后者是型号不对。
    MEDIUM_NOT_MOUNTED = "medium_not_mounted"
    #: 介质记录无法恢复（未知 schema、缺型号、损坏）。内容原样保留但禁止交易——
    #: **不得当作空盘覆盖**，也不得复制成两个新 ID。
    MEDIUM_UNRECOVERED = "medium_unrecovered"
    #: 驱动器已被拆除，介质失去归属。本轮不自行决定掉落或保管方式。
    MEDIUM_ORPHANED = "medium_orphaned"
    #: 目标槽位已被占用。
    SLOT_OCCUPIED = "slot_occupied"
    #: 驱动器所在区块未加载。**不等于它被拆掉了**（与 `MASTER_UNLOADED` 同一条纪律）。
    DRIVER_UNLOADED = "driver_unloaded"

    # 样板与配方（07）
    #: 样板结构本身不合法：数量为零或负、没有任何产物、资源键无效。
    #: **与 `BAD_PARAMETER` 分开**：那说的是「请求的写法不对」，
    #: 这一条说的是「样板的**内容**不是一个合法配方」，界面要指到具体哪一条。
    PATTERN_INVALID = "pattern_invalid"
    #: 样板编码的格式版本认不出来，或已经失效。**可读性判断在这里，
    #: 不在 `PATTERN_INVALID`**：内容可能完全合法，只是这个版本不再允许派发。
    PATTERN_FORMAT_UNSUPPORTED = "pattern_format_unsupported"
    #: 判断依据的样板版本已经变了（被编辑、被换成另一块、被拔出后重挂）。
    #: **必须与 `SLOT_CHANGED` 分开**：那是「槽里的东西换了」，这条是
    #: 「同一块样板的内容变了」；两者对旧规划的影响相同，但处置不同——
    #: 前者要重新找样板，后者只要重新核对。
    PATTERN_STALE = "pattern_stale"
    #: 样板类型与这台设备不匹配（合成样板插进了样板接口，或反过来）。
    PATTERN_KIND_MISMATCH = "pattern_kind_mismatch"
    #: 槽里的物品根本不是本模组的样板。与 `PATTERN_INVALID` 分开：
    #: 那是「是样板但内容坏了」，这是「压根不是样板」。
    PATTERN_NOT_A_TEMPLATE = "pattern_not_a_template"

    # 合成规划（08）
    #
    # **这一族刻意分成多个码，不合并成「没有配方」**（08 文档原文：无解、缺料与
    # 预算超限分别报告）。四种情形对玩家意味着完全不同的下一步动作：
    # 「去装一块样板」「去把离线的那台接口接回来」「去补基础原料」「再点一次
    # 让它接着算」，糊成一个码就等于把「我该做什么」这条信息丢掉。
    #: 样板索引里**根本没有**产出这个资源的样板。玩家要做的是准备一块样板。
    PLAN_NO_RECIPE = "plan_no_recipe"
    #: 有样板，但所有候选提供者现在都不能用（离线、容器读不到、被拆）。
    #: **与 `PLAN_NO_RECIPE` 分开**：这里玩家要做的是让那台设备回到线，不是去找样板。
    PLAN_NO_PROVIDER = "plan_no_available_provider"
    #: 配方找到了，但基础原料不够。这是**部分计划**（能算的部分照算），
    #: 与「压根做不出来」是两回事。
    PLAN_MISSING_MATERIALS = "plan_missing_materials"
    #: 某条路线出现循环依赖（A→B→A）。**规划器自 2026-09-26 起不再产出它**：
    #: 候选会成环时先排除该候选、继续试其他非循环候选，全部排除才折进
    #: `PLAN_MISSING_MATERIALS`（环路留在 `detail` 里作诊断）。
    #: 保留这个成员是给任务 12 用的——恢复一份旧存档里的计划时，
    #: `CraftPlan.reasons()` 仍应给得出原因码，而不是静默返回空。
    PLAN_CYCLE = "plan_cycle"
    #: 深度、步骤数或候选窗口触顶。**与 `PLAN_NO_RECIPE` 分开**：
    #: 那说的是「真的没有」，这条说的是「这次没往下看」，两者都不该被读成对方的结论。
    PLAN_LIMIT_REACHED = "plan_limit_reached"
    #: 本帧的推进预算用完了，任务仍可继续。**它不是失败**——
    #: 与调度器把未做完的任务留到下一帧同一语义。
    PLAN_BUDGET_EXHAUSTED = "plan_budget_exhausted"
    #: 任务被调用方取消。计划结果作废，但**任何真实库存都没被动过**。
    PLAN_CANCELLED = "plan_cancelled"
    #: 计划依据的那几个键的库存已经不够了（提交前复核）。
    #: **按计划实际声明过的键逐个比量**，因此无关的库存变动不会让计划作废。
    PLAN_STOCK_CHANGED = "plan_stock_changed"
    #: 计划依据的拓扑版本变了（拆线、合并、主控换人）——归属可能已经不同。
    PLAN_TOPOLOGY_CHANGED = "plan_topology_changed"

    # 订单与执行（09）
    #
    # 与 `PLAN_*` 同一条纪律：每一种「做不下去」都要能分辨，因为它们的下一步
    # 动作完全不同。**注意订单不重复定义计划的那一族码**——规划失败（缺料、
    # 无样板、成环）由 08 的 `PLAN_*` 原样透出，这里只加**执行侧**才有的事实。
    #: 订单 ID 在所属网络的台账里不存在（从没建过、已被清理、或不属于这张网络）。
    #: **与 `BAD_PARAMETER` 分开**：请求写法完全合法，是那个订单不在。
    ORDER_UNKNOWN = "order_unknown"
    #: 订单已经处于终态（已完成/已取消/失败），不能再取消或继续。
    #: **与 `ORDER_UNKNOWN` 分开**：「没有这个订单」与「这个订单已经结束了」
    #: 在界面上是两句话，混在一起会让玩家去查一个其实还在的订单。
    ORDER_TERMINAL = "order_terminal"
    #: 处理器槽位或内存租约不够，订单进不了运行。**与 `NO_CAPACITY`（介质容量）
    #: 和 `BANDWIDTH_*`（负载域带宽）都分开**：三者都是「放不下」，但一个要加盘、
    #: 一个要加处理器/内存槽、一个要扩基带，合并等于把该做的事丢掉。
    ORDER_NO_CAPACITY = "order_no_capacity"
    #: 计划声明要用的库存**有，但已经被别的订单锁住了**。
    #: **必须与 `INSUFFICIENT_STOCK` 分开**：那条是「网络里确实没有，去补料」，
    #: 这条是「等你前面那单」——两者对玩家是相反的动作。
    ORDER_STOCK_TAKEN = "order_stock_taken"
    #: 步骤已经派发出去、正等产物回流，但**这条装配没有注入 `returns_sink`**。
    #: 单列一个码的理由：这不是失败、也不是缺料，而是「这一半没接上」——
    #: 订单停在等待产物是对的，但必须让人一眼看出卡在哪，否则会被读成
    #: 「机器没出货」，然后去修一台没坏的机器。任务 10 的实现已落地，
    #: 库默认装配仍不给 `returns_sink`（游戏内由 `start_server()` 显式接上）。
    ORDER_RETURNS_ABSENT = "order_returns_absent"
    #: 订单被**显式**暂停（玩家或调用方要求）。与「被东西挡住的阻塞」分开：
    #: 一个是「我叫它停的」，一个是「有别的东西挡着」，恢复动作不同。
    ORDER_PAUSED = "order_paused"
    #: 样板口的批次槽正被**另一个批次**占着（`PortState.batch` 只有一格，
    #: `stage_batch` 每次覆盖它）。让位并报出来，而不是把别人的批次冲掉：
    #: 冲掉的表现是前一个订单的 `delivered` 被清零，然后原料被投第二遍。
    ORDER_PORT_BUSY = "order_port_busy"
    #: 批次推进不下去：备料没备完、外部机器不收、或只投了一部分就停住。
    #: 06 明写「后续推进交由 09 决定」，因此**这个判断只在这里**得出；
    #: 细节（备了多少、投了多少）在订单视图与批次状态里，玩家据此决定重试还是换机器。
    ORDER_BATCH_STALLED = "order_batch_stalled"

    # 外部容器（13）
    CONTAINER_REFUSED = "container_refused"
    CONTAINER_PARTIAL = "container_partial"
    CONTAINER_UNCERTAIN = "container_uncertain"
    NO_ADAPTER = "no_adapter"

    # 定义缺项（R10）
    DEFINITION_UNCONFIGURED = "r10_unconfigured"


class BlockReason(object):
    """一个原因及其补充说明。同一次失败可以携带多个原因。"""

    __slots__ = ("code", "detail")

    def __init__(self, code, detail=""):
        # type: (str, str) -> None
        self.code = code
        self.detail = detail

    def to_dict(self):
        # type: () -> dict
        return {"code": self.code, "detail": self.detail}

    def __eq__(self, other):
        if not isinstance(other, BlockReason):
            return NotImplemented
        return self.code == other.code and self.detail == other.detail

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return hash((self.code, self.detail))

    def __repr__(self):
        return "BlockReason(%s, %r)" % (self.code, self.detail)


def reason_code(reason):
    # type: (object) -> str
    """从 BlockReason / 字符串 / None 中取出原因码，便于断言与日志。"""
    if reason is None:
        return ""
    if isinstance(reason, BlockReason):
        return reason.code
    return str(reason)


# -- 命令 ------------------------------------------------------------------


class CommandKind(object):
    """命令的用途分类。

    主控按它决定路由到哪个网络服务（`network/services.py` 的 `COMMAND_ROUTES`），
    以及该命令是不是只读。分类必须显式：设备不得「顺便」改库存（全局约束 2），
    因此未知 kind 一律 `BAD_PARAMETER`，不做兜底推断。
    """

    #: 从网络存取资源（终端、取货口、存货口）。
    STORAGE = "storage"
    #: 修改设备配置（过滤、朝向相关设置等）。仍属写操作，走 storage 之外的
    #: 同一条准入链，但不改动库存。
    CONFIGURE = "configure"
    #: 提交/取消订单。
    ORDER = "order"
    #: 只读查询。走到运行条件检查为止，不要求带宽、能源与业务服务可用。
    STATUS = "status"
    #: 必要的恢复管理。**只读或受控的诊断动作**，不得用来绕过运行条件
    #: 改库存或推进订单（03 验收末条）。
    RECOVERY = "recovery"

    ALL = frozenset([STORAGE, CONFIGURE, ORDER, STATUS, RECOVERY])


class CommandSource(object):
    """请求来源。已经过身份校验，领域层不再关心玩家细节。"""

    __slots__ = (
        "player_id",
        "device_ref",
        "dimension",
        "distance_ok",
        "session_id",
        "received_tick",
    )

    def __init__(
        self,
        player_id,
        device_ref,
        dimension,
        distance_ok=True,
        session_id="",
        received_tick=0,
    ):
        self.player_id = player_id
        self.device_ref = device_ref
        self.dimension = dimension
        self.distance_ok = distance_ok
        self.session_id = session_id
        self.received_tick = received_tick

    def to_dict(self):
        # type: () -> dict
        return {
            "player_id": self.player_id,
            "device_ref": self.device_ref.to_dict() if self.device_ref else None,
            "dimension": self.dimension,
            "distance_ok": self.distance_ok,
            "session_id": self.session_id,
            "received_tick": self.received_tick,
        }


class DeviceCommand(object):
    """一条待准入的命令。

    携带 `topology_version` 是有意的：命令依据的图可能已经变化，
    主控必须能拒绝「基于旧图」的请求，而不是照着旧图去执行。
    """

    __slots__ = (
        "request_id",
        "kind",
        "source",
        "payload",
        "topology_version",
        "device_ref",
    )

    def __init__(
        self,
        request_id,
        kind,
        source,
        payload=None,
        topology_version=None,
        device_ref=None,
    ):
        # type: (str, str, CommandSource, dict, int, object) -> None
        if not request_id:
            raise ValueError("命令必须携带 request_id（用于去重）")
        self.request_id = request_id
        self.kind = kind
        self.source = source
        self.payload = dict(payload or {})
        self.topology_version = topology_version
        #: 发起命令的设备实例。缺省取来源设备。
        self.device_ref = device_ref if device_ref is not None else (
            source.device_ref if source is not None else None
        )

    def to_dict(self):
        # type: () -> dict
        return {
            "request_id": self.request_id,
            "kind": self.kind,
            "source": self.source.to_dict() if self.source else None,
            "payload": dict(self.payload),
            "topology_version": self.topology_version,
            "device_ref": self.device_ref.to_dict() if self.device_ref else None,
        }


class CommandResult(object):
    """一次命令的结果。"""

    __slots__ = ("request_id", "status", "payload", "reasons", "topology_version")

    def __init__(
        self,
        request_id,
        status,
        payload=None,
        reasons=(),
        topology_version=None,
    ):
        # type: (str, str, dict, tuple, int) -> None
        if status not in ALL_STATUSES:
            raise ValueError("未知结果状态: %r" % (status,))
        self.request_id = request_id
        self.status = status
        self.payload = dict(payload or {})
        self.reasons = tuple(reasons)
        self.topology_version = topology_version

    # -- 构造快捷方式 --------------------------------------------------

    @classmethod
    def ok(cls, request_id, payload=None, topology_version=None):
        return cls(request_id, STATUS_OK, payload, (), topology_version)

    @classmethod
    def partial(cls, request_id, payload=None, reasons=(), topology_version=None):
        return cls(request_id, STATUS_PARTIAL, payload, reasons, topology_version)

    @classmethod
    def blocked(cls, request_id, reasons, topology_version=None):
        return cls(request_id, STATUS_BLOCKED, None, reasons, topology_version)

    @classmethod
    def stale(cls, request_id, reasons, topology_version=None):
        return cls(request_id, STATUS_STALE, None, reasons, topology_version)

    @classmethod
    def rejected(cls, request_id, reasons, topology_version=None):
        return cls(request_id, STATUS_REJECTED, None, reasons, topology_version)

    @classmethod
    def in_flight(cls, request_id, topology_version=None):
        return cls(request_id, STATUS_IN_FLIGHT, None, (), topology_version)

    # -- 查询 ----------------------------------------------------------

    def is_applied(self):
        # type: () -> bool
        """是否产生了实际效果。只有它会改变库存或订单。"""
        return self.status in APPLIED_STATUSES

    def reason_codes(self):
        # type: () -> tuple
        return tuple(reason_code(r) for r in self.reasons)

    def first_reason(self):
        # type: () -> str
        codes = self.reason_codes()
        return codes[0] if codes else ""

    def to_dict(self):
        # type: () -> dict
        return {
            "request_id": self.request_id,
            "status": self.status,
            "payload": dict(self.payload),
            "reasons": [r.to_dict() if isinstance(r, BlockReason) else r for r in self.reasons],
            "topology_version": self.topology_version,
        }

    def __repr__(self):
        return "CommandResult(%s, %s, %r)" % (
            self.request_id,
            self.status,
            self.reason_codes(),
        )


# -- 转移回执 --------------------------------------------------------------


class TransferStatus(object):
    """一次与外部容器交接的结果。"""

    #: 请求的数量全部完成
    COMPLETE = "complete"
    #: 只完成了一部分，`moved` 为实际数量
    PARTIAL = "partial"
    #: 对方拒绝，什么都没动
    REFUSED = "refused"
    #: 无法确定是否成功。**不得据此重发整批**，交由恢复流程核对。
    UNCERTAIN = "uncertain"
    #: 适配器不支持该操作（例如只读容器）
    UNSUPPORTED = "unsupported"


class TransferReceipt(object):
    """一次转移的实际结果。

    核心纪律（架构规划 6.3）：**只按实际数量记账**。
    `moved` 是唯一可信的完成量，`requested` 只是意图。

    `holder` 是任务 06 加上的第三样东西（「实际量、余量、当前持有者」）：
    它回答的是**没搬成的那一部分现在在谁手上**。只报「成功了多少」而不报
    「剩下的在哪」，正是「先删源再假定目标接收」的温床——那批东西会变成
    账外资产。空串表示「说不清」（缺少端点身份时），**不是「没有主」**。
    """

    __slots__ = (
        "op_id",
        "requested",
        "moved",
        "status",
        "reason",
        "resource_key",
        "holder",
    )

    def __init__(
        self, op_id, requested, moved, status, reason="", resource_key=None, holder=""
    ):
        # type: (str, int, int, str, str, object, str) -> None
        if moved < 0 or requested < 0:
            raise ValueError("转移数量不能为负: requested=%r moved=%r" % (requested, moved))
        if moved > requested:
            raise ValueError(
                "实际转移量不得大于请求量: requested=%r moved=%r" % (requested, moved)
            )
        self.op_id = op_id
        self.requested = requested
        self.moved = moved
        self.status = status
        self.reason = reason
        self.resource_key = resource_key
        #: 余量当前的持有者标识。空串 = 无法确认（**不等于「没人持有」**）。
        self.holder = holder

    @property
    def remainder(self):
        # type: () -> int
        """未完成、仍归原主的部分。"""
        return self.requested - self.moved

    def is_complete(self):
        return self.status == TransferStatus.COMPLETE and self.moved == self.requested

    def is_applied(self):
        # type: () -> bool
        """是否真的动过东西。**去重账本只记这一类结果。**

        `COMPLETE` 与 `PARTIAL` 都算：两者都真的搬了东西，因此重发必须返回
        原回执而不是再搬一次。`UNCERTAIN` **不算**——但它的处理方式不是
        「当没发生」：调用方必须把它留在在途标记里（见 `TransferCoordinator.commit`），
        否则重发会再动一遍。
        """
        return self.status in (TransferStatus.COMPLETE, TransferStatus.PARTIAL)

    def to_dict(self):
        # type: () -> dict
        return {
            "op_id": self.op_id,
            "requested": self.requested,
            "moved": self.moved,
            "status": self.status,
            "reason": self.reason,
            "holder": self.holder,
            "resource_key": (
                self.resource_key.to_dict() if self.resource_key is not None else None
            ),
        }

    def __repr__(self):
        return "TransferReceipt(%s, %s, %d/%d)" % (
            self.op_id,
            self.status,
            self.moved,
            self.requested,
        )


# -- 转移请求 --------------------------------------------------------------

#: 这份资源是**外部**送进本模组的（其他模组的物流、玩家手动放入、漏斗）。
#: 只有它会为订单结果累加器贡献一次（`concepts.md`「无论产物对应的物品以任何
#: 方式输入网络…都会…累加器 +1」）。
ORIGIN_EXTERNAL = "external"
#: 本模组自己搬的（缓冲↔网络、介质间搬运、补偿、索引重建）。
#: **不算新回流**——把它记成输入会让一份资源被反复计入产物
#: （`terminal_item_index.md` §3 与全局约束 3）。
ORIGIN_INTERNAL = "internal"

ORIGINS = frozenset([ORIGIN_EXTERNAL, ORIGIN_INTERNAL])


class TransferRequest(object):
    """一次资源转移的意图。**纯数据**：可序列化、可比较，不含任何端点对象。

    端点对象由调用方按 `source_id` / `target_id` 解析后另行交给协调器。
    这样分开是有理由的：请求要能跨命令边界、进诊断、进存档，
    而端点可能持有容器句柄甚至引擎对象，两者不该被绑在同一个结构里。

    `topology_version` 与 `reservation_id` 都是**可选但要显式**的：
    `None` 表示「不依图版本」，空串表示「没有预留凭据」——而不是「随便挑一个」。
    """

    __slots__ = (
        "op_id",
        "source_id",
        "target_id",
        "resource_key",
        "count",
        "topology_version",
        "reservation_id",
        "owner_id",
        "origin",
    )

    def __init__(
        self,
        op_id,
        source_id,
        target_id,
        resource_key,
        count,
        topology_version=None,
        reservation_id="",
        owner_id="",
        origin=ORIGIN_INTERNAL,
    ):
        # type: (str, str, str, ResourceKey, int, int, str, str, str) -> None
        if not op_id:
            raise ValueError("转移请求必须携带 op_id（用于去重与恢复核对）")
        if settings.DEBUG and not isinstance(resource_key, ResourceKey):
            raise TypeError("转移请求需要 ResourceKey，得到 %r" % (type(resource_key),))
        _require_count(count, "转移数量")
        if origin not in ORIGINS:
            raise ValueError("未知来源标识: %r" % (origin,))
        self.op_id = op_id
        self.source_id = source_id
        self.target_id = target_id
        self.resource_key = resource_key
        self.count = int(count)
        self.topology_version = topology_version
        self.reservation_id = reservation_id
        self.owner_id = owner_id
        self.origin = origin

    def to_dict(self):
        # type: () -> dict
        return {
            "op_id": self.op_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "resource_key": self.resource_key.to_dict(),
            "count": self.count,
            "topology_version": self.topology_version,
            "reservation_id": self.reservation_id,
            "owner_id": self.owner_id,
            "origin": self.origin,
        }

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> TransferRequest
        return cls(
            data["op_id"],
            data["source_id"],
            data["target_id"],
            ResourceKey.from_dict(data["resource_key"]),
            data["count"],
            data.get("topology_version"),
            data.get("reservation_id", ""),
            data.get("owner_id", ""),
            data.get("origin", ORIGIN_INTERNAL),
        )

    def __repr__(self):
        return "TransferRequest(%s, %s->%s, %s x%d)" % (
            self.op_id,
            self.source_id,
            self.target_id,
            self.resource_key.name,
            self.count,
        )


class ContainerCapabilities(object):
    """外部容器的能力声明。

    默认全部为保守值（不支持），适配器必须**主动声明**它确实具备的能力。
    任务 13-01 明确要求覆盖：只读、拒收、部分接收、无模拟、
    无变化事件、返回不确定。
    """

    __slots__ = (
        "can_read",
        "can_insert",
        "can_simulate",
        "supports_partial",
        "is_atomic",
        "notify_token",
        "max_slots",
        "note",
    )

    def __init__(
        self,
        can_read=True,
        can_insert=True,
        can_simulate=False,
        supports_partial=False,
        is_atomic=False,
        notify_token=None,
        max_slots=None,
        note="",
    ):
        self.can_read = can_read
        self.can_insert = can_insert
        self.can_simulate = can_simulate
        self.supports_partial = supports_partial
        self.is_atomic = is_atomic
        #: None 表示该容器没有变化通知，调用方需要退避轮询。
        self.notify_token = notify_token
        self.max_slots = max_slots
        self.note = note

    def to_dict(self):
        # type: () -> dict
        return {
            "can_read": self.can_read,
            "can_insert": self.can_insert,
            "can_simulate": self.can_simulate,
            "supports_partial": self.supports_partial,
            "is_atomic": self.is_atomic,
            "has_notification": self.notify_token is not None,
            "max_slots": self.max_slots,
            "note": self.note,
        }


# -- 增量通知 --------------------------------------------------------------


class Notification(object):
    """提交完成后的派生通知。

    纪律（架构规划 5.3）：先提交，后通知。通知里带的是已提交的事实，
    订阅者不得据此再改一次库存。
    """

    kind = "notification"
    __slots__ = ()

    def to_dict(self):
        raise NotImplementedError


class StorageChanged(Notification):
    kind = "storage_changed"
    __slots__ = ("network_id", "resource_key", "stored", "reserved", "version")

    def __init__(self, network_id, resource_key, stored, reserved, version):
        if settings.DEBUG:
            if not isinstance(resource_key, ResourceKey):
                raise TypeError("StorageChanged requires an available ResourceKey")
            for label, value in (("stored", stored), ("reserved", reserved), ("version", version)):
                _require_count(value, label)
            if reserved > stored:
                raise ValueError("Reserved count exceeds stored count")
        self.network_id = network_id
        self.resource_key = resource_key
        self.stored = stored
        self.reserved = reserved
        self.version = version

    def to_dict(self, descriptions=None, include_description=False):
        """默认自包含；传入会话描述表后使用序号，首次或重同步附完整描述。"""
        result = {
            "kind": self.kind,
            "network_id": self.network_id,
            "stored": self.stored,
            "reserved": self.reserved,
            "available": self.stored - self.reserved,
            "version": self.version,
        }
        if descriptions is None:
            result["resource_key"] = self.resource_key.canonical()
            result["resource_description"] = self.resource_key.to_dict()
        else:
            if descriptions.network_id != self.network_id:
                raise ValueError("Resource description network mismatch")
            result["resource_ref"] = descriptions.encode(self.resource_key, include_description)
        return result

    @classmethod
    def from_dict(cls, data, descriptions=None):
        if not isinstance(data, dict) or data.get("kind") != cls.kind:
            raise ValueError("Expected a storage change notification")
        # 在注册资源描述之前校验数量，失败消息不能污染映射。
        for field in ("stored", "reserved", "version"):
            _require_count(data[field], field)
        if data["reserved"] > data["stored"]:
            raise ValueError("Reserved count exceeds stored count")
        if data.get("available") != data["stored"] - data["reserved"]:
            raise ValueError("Inconsistent available count")
        if "resource_ref" in data:
            if "resource_description" in data or "resource_key" in data:
                raise ValueError("Ambiguous resource representation")
            if descriptions is None or descriptions.network_id != data["network_id"]:
                raise ValueError("Resource description network mismatch")
            key = descriptions.decode(data["resource_ref"])
        else:
            key = ResourceKey.from_dict(data["resource_description"])
            if data["resource_key"] != key.canonical():
                raise ValueError("Resource fingerprint mismatch")
        return cls(data["network_id"], key, data["stored"], data["reserved"], data["version"])


class TopologyChanged(Notification):
    kind = "topology_changed"
    __slots__ = ("version", "network_id", "run_state", "reasons")

    def __init__(self, version, network_id, run_state, reasons=()):
        self.version = version
        self.network_id = network_id
        self.run_state = run_state
        self.reasons = tuple(reasons)

    def to_dict(self):
        return {
            "kind": self.kind,
            "version": self.version,
            "network_id": self.network_id,
            "run_state": self.run_state,
            "reasons": [
                r.to_dict() if isinstance(r, BlockReason) else r for r in self.reasons
            ],
        }


class DeviceStatusChanged(Notification):
    kind = "device_status_changed"
    __slots__ = ("status",)

    def __init__(self, status):
        self.status = status

    def to_dict(self):
        return {"kind": self.kind, "status": self.status.to_dict()}


class ExternalInput(Notification):
    """一份资源**真的**从外部进入了网络。任务 10 的合格回流入口。

    **它只发一次，而且只在真的进入之后发。** 三条纪律写在字段里：

    - `op_id` 是唯一操作身份，接收方据此去重。同一批被重发、被重放、
      或被缓冲往返搬了几次，都只有这一个 `op_id`。
    - `count` 是**实际进入**的数量（`TransferReceipt.moved`），不是请求量。
      部分接收时只报进来多少，剩下的还在外面。
    - `origin` 必须是 `ORIGIN_EXTERNAL`。补偿、介质间搬运、索引重建都不发这条
      通知——把它们记成回流会让同一份资源被反复计入订单产物
      （全局约束 3，`architecture_analysis.md` §6.4）。
    """

    kind = "external_input"
    __slots__ = ("op_id", "network_id", "resource_key", "count", "source_id", "tick")

    def __init__(self, op_id, network_id, resource_key, count, source_id="", tick=0):
        # type: (str, str, ResourceKey, int, str, int) -> None
        if settings.DEBUG:
            if not op_id:
                raise ValueError("外部输入通知必须携带 op_id（接收方据此去重）")
            if not isinstance(resource_key, ResourceKey):
                raise TypeError("ExternalInput requires an available ResourceKey")
            _require_count(count, "external input count")
        self.op_id = op_id
        self.network_id = network_id
        self.resource_key = resource_key
        self.count = int(count)
        self.source_id = source_id
        self.tick = int(tick)

    def to_dict(self):
        # type: () -> dict
        return {
            "kind": self.kind,
            "op_id": self.op_id,
            "network_id": self.network_id,
            "resource_key": self.resource_key.to_dict(),
            "count": self.count,
            "source_id": self.source_id,
            "tick": self.tick,
        }

    def __repr__(self):
        return "ExternalInput(%s, %s x%d)" % (
            self.op_id,
            self.resource_key.name,
            self.count,
        )


class CancelToken(object):
    """R01 的交互层挂钩：非法连接要「拦截」还是「停机」尚未定案。

    服务端只负责把最终结果告诉交互层，不替它决定策略。
    设置为取消时，交互层可以选择不出手（拦下放置），
    保持未取消则由拓扑进入控制器冲突状态。
    """

    __slots__ = ("_cancelled", "_reason")

    def __init__(self):
        self._cancelled = False
        self._reason = ""

    def cancel(self, reason=""):
        # type: (str) -> None
        self._cancelled = True
        self._reason = reason

    @property
    def cancelled(self):
        return self._cancelled

    @property
    def reason(self):
        return self._reason


# -- 终端同步通道（11-01 / 11-02）------------------------------------------
#
# 客户端与终端服务端之间的**线格式**。它不是 `StorageChanged` 那类进程内
# 通知：那里的订阅者是本进程的服务，这里的对端是客户端，字段一旦上线就
# 得长期兼容，因此边界检查（数量范围、未知字段、未知消息种类）全部写在
# 这里，且**解析时无条件执行**（`settings.DEBUG` 只管构造时的那一半，
# 与 `StorageChanged` 的分工一致）。
#
# **两个版本在这一层必须各有其名，不允许出现裸 `version`。** 交易预期
# 版本一律叫 `expected_index_version`，物品视图版本一律叫 `item_view_version`
# 或增量的 `base_version`/`new_version`。名字不同是唯一的防线：同名的话
# 「客户端拿 UI 发布版本当交易预期版本」在代码层面根本看不出来。

SYNC_SCHEMA_VERSION = 1

#: 客户端→服务端与 服务端→客户端 的事件名（`CustomC2SEvent` / `CustomS2CEvent`）
SYNC_CHANNEL_C2S = "endernet:terminal_request"
SYNC_CHANNEL_S2C = "endernet:terminal_sync"

#: 视图种类。`order` **是已知种类**（消息词汇里为它留了位置），但尚无生产者，
#: 因此与「客户端发来一个我们不认识的种类」必须给出不同的拒绝理由——
#: 前者是 `Reason.UNSUPPORTED`（还没做），后者是协议错误（发错了）。
VIEW_ITEM = "item"
VIEW_ORDER = "order"
VIEW_KINDS = (VIEW_ITEM, VIEW_ORDER)
VIEW_KINDS_WITH_PRODUCER = (VIEW_ITEM,)

MSG_SESSION_OPEN = "session_open"
MSG_SESSION_CLOSE = "session_close"
MSG_SNAPSHOT_REQUEST = "snapshot_request"
MSG_ACK = "ack"
MSG_SESSION_READY = "session_ready"
MSG_SNAPSHOT_BEGIN = "snapshot_begin"
MSG_SNAPSHOT_CHUNK = "snapshot_chunk"
MSG_SNAPSHOT_END = "snapshot_end"
MSG_ITEM_DELTA = "item_delta"
MSG_RESYNC_REQUIRED = "resync_required"
MSG_ERROR = "error"

CLIENT_TO_SERVER_KINDS = frozenset(
    [MSG_SESSION_OPEN, MSG_SESSION_CLOSE, MSG_SNAPSHOT_REQUEST, MSG_ACK]
)
SERVER_TO_CLIENT_KINDS = frozenset(
    [
        MSG_SESSION_READY,
        MSG_SNAPSHOT_BEGIN,
        MSG_SNAPSHOT_CHUNK,
        MSG_SNAPSHOT_END,
        MSG_ITEM_DELTA,
        MSG_RESYNC_REQUIRED,
        MSG_ERROR,
    ]
)

#: 线格式数量上限。取双精度能精确表示整数的上界：再大就不能保证
#: 经过任何一次 JSON 中转后仍与原值相等，而「大数量精确保留」是验收项。
#: 注意这比 `resource_key._require_count` 严——那条只管住本地库存，
#: 这条还要管住「对面解出来是不是同一个数」。
PROTOCOL_MAX_COUNT = 2 ** 53 - 1


def require_count(value, name):
    # type: (object, str) -> int
    """线格式数量校验：拒 bool / 非整数 / 负数 / 超出双精度整数范围。

    判据的前三条与 `resource_key._require_count` 相同，改动要同步；
    这里多出的第四条是线上特有的（见 `PROTOCOL_MAX_COUNT`）。
    """
    _require_count(value, name)
    if value > PROTOCOL_MAX_COUNT:
        raise ValueError(
            "%s 超出线格式上限 %d，得到 %r" % (name, PROTOCOL_MAX_COUNT, value)
        )
    return value


def require_text(value, name):
    # type: (object, str) -> str
    """非空文本校验。空串对身份类字段永远是错的（它等于「没说」）。"""
    if not isinstance(value, _TEXT_TYPES) or not value:
        raise ValueError("%s 必须是非空文本，得到 %r" % (name, value))
    return value


def _require_fields(data, required, label):
    # type: (dict, tuple, str) -> None
    """严格字段检查：**缺项与多项都拒绝**。

    多项也拒绝是有意的。它让「消息里塞一个自称的 `player_id`」变成协议
    错误而不是被静静忽略的字段——身份只能来自引擎附的 `__id__`，
    那么任何试图在消息体里声明身份的做法都应当当场失败。
    """
    if not isinstance(data, dict):
        raise TypeError("%s 必须是字典，得到 %r" % (label, type(data)))
    present = set(data)
    missing = [name for name in required if name not in present]
    if missing:
        raise ValueError("%s 缺少字段 %r" % (label, missing))
    unknown = present - set(required)
    if unknown:
        raise ValueError("%s 出现未知字段 %r" % (label, sorted(unknown)))


def _encode_item(descriptions, key, stored, reserved):
    # type: (object, ResourceKey, int, int) -> dict
    """快照与增量**共用**的条目编码，防止两条路径漂移。"""
    require_count(stored, "stored")
    require_count(reserved, "reserved")
    if reserved > stored:
        raise ValueError("Reserved count exceeds stored count")
    if descriptions is None:
        return {
            "resource_key": key.canonical(),
            "resource_description": key.to_dict(),
            "stored": stored,
            "reserved": reserved,
        }
    return {
        "resource_ref": descriptions.encode(key),
        "stored": stored,
        "reserved": reserved,
    }


def _encode_remove(descriptions, key):
    # type: (object, ResourceKey) -> dict
    """`remove` 只带身份，不带数量——带数量会诱使对端把它当成「减到某个数」。"""
    if descriptions is None:
        return {
            "resource_key": key.canonical(),
            "resource_description": key.to_dict(),
        }
    return {"resource_ref": descriptions.encode(key)}


def _decode_remove(data, descriptions=None):
    # type: (dict, object) -> ResourceKey
    if "resource_ref" in data:
        _require_fields(data, ("resource_ref",), "item remove")
        if descriptions is None:
            raise ValueError("收到资源引用，但当前没有会话描述表")
        return descriptions.decode(data["resource_ref"])
    _require_fields(
        data, ("resource_key", "resource_description"), "item remove"
    )
    key = ResourceKey.from_dict(data["resource_description"])
    if data["resource_key"] != key.canonical():
        raise ValueError("Resource fingerprint mismatch")
    return key


def _decode_item(data, descriptions=None):
    # type: (dict, object) -> tuple
    """`_encode_item` 的逆。**所有检查通过后才认这把键。**"""
    if "resource_ref" in data:
        _require_fields(data, ("resource_ref", "stored", "reserved"), "item entry")
        if descriptions is None:
            raise ValueError("收到资源引用，但当前没有会话描述表")
        key = descriptions.decode(data["resource_ref"])
    else:
        _require_fields(
            data, ("resource_key", "resource_description", "stored", "reserved"),
            "item entry",
        )
        key = ResourceKey.from_dict(data["resource_description"])
        if data["resource_key"] != key.canonical():
            raise ValueError("Resource fingerprint mismatch")
    stored = require_count(data["stored"], "stored")
    reserved = require_count(data["reserved"], "reserved")
    if reserved > stored:
        raise ValueError("Reserved count exceeds stored count")
    return key, stored, reserved


class SyncMessage(object):
    """同步消息的公共外层。**只放消息自己的字段**，路由字段在 `SyncEnvelope`。"""

    kind = ""
    __slots__ = ()

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        raise NotImplementedError

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> SyncMessage
        raise NotImplementedError


class SessionOpen(SyncMessage):
    """客户端请求打开一个终端会话。

    **消息体里没有玩家字段，这是有意的。** 服务端只认引擎在 C2S 事件上
    附的 `__id__`；不定义该字段，就没有一处代码会去读它，也就没有
    「忘了忽略它」的机会（`_require_fields` 还会把塞进来的这类字段
    判为未知字段而拒收整条消息）。
    """

    kind = MSG_SESSION_OPEN
    __slots__ = ("request_id", "device_ref", "dimension")

    def __init__(self, request_id, device_ref, dimension):
        if settings.DEBUG:
            require_text(request_id, "request_id")
            _require_count(dimension, "dimension")
        self.request_id = request_id
        self.device_ref = device_ref
        self.dimension = dimension

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {
            "kind": self.kind,
            "request_id": self.request_id,
            "device_ref": self.device_ref.to_dict(),
            "dimension": self.dimension,
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> SessionOpen
        from .identity import DeviceRef

        _require_fields(
            data, ("kind", "request_id", "device_ref", "dimension"), "session_open"
        )
        require_text(data["request_id"], "request_id")
        _require_fields(
            data["device_ref"], ("node_key", "generation"), "session_open.device_ref"
        )
        _require_count(data["dimension"], "dimension")
        return cls(
            data["request_id"],
            DeviceRef.from_dict(data["device_ref"]),
            data["dimension"],
        )


class SessionClose(SyncMessage):
    kind = MSG_SESSION_CLOSE
    __slots__ = ("request_id",)

    def __init__(self, request_id):
        if settings.DEBUG:
            require_text(request_id, "request_id")
        self.request_id = request_id

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {"kind": self.kind, "request_id": self.request_id}

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> SessionClose
        _require_fields(data, ("kind", "request_id"), "session_close")
        require_text(data["request_id"], "request_id")
        return cls(data["request_id"])


class SnapshotRequest(SyncMessage):
    """请求（重新）取一份快照。`from_version` 是客户端当前的物品视图版本。

    服务端据此判断能不能只补增量：缺口早于历史起点就只能重发快照。
    """

    kind = MSG_SNAPSHOT_REQUEST
    __slots__ = ("request_id", "from_version")

    def __init__(self, request_id, from_version):
        if settings.DEBUG:
            require_text(request_id, "request_id")
            require_count(from_version, "from_version")
        self.request_id = request_id
        self.from_version = from_version

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {
            "kind": self.kind,
            "request_id": self.request_id,
            "from_version": self.from_version,
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> SnapshotRequest
        _require_fields(data, ("kind", "request_id", "from_version"), "snapshot_request")
        require_text(data["request_id"], "request_id")
        require_count(data["from_version"], "from_version")
        return cls(data["request_id"], data["from_version"])


class Ack(SyncMessage):
    """确认某次传输里**连续到达**到的最大包序号。

    只报最高连续序号（而不是逐包报），是因为丢掉中间某一包时后面的包
    即使到了也不能算数——游标本来就只能按连续段推进。
    """

    kind = MSG_ACK
    __slots__ = ("request_id", "transfer_id", "sequence")

    def __init__(self, request_id, transfer_id, sequence):
        if settings.DEBUG:
            require_text(request_id, "request_id")
            require_text(transfer_id, "transfer_id")
            require_count(sequence, "sequence")
        self.request_id = request_id
        self.transfer_id = transfer_id
        self.sequence = sequence

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {
            "kind": self.kind,
            "request_id": self.request_id,
            "transfer_id": self.transfer_id,
            "sequence": self.sequence,
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> Ack
        _require_fields(
            data, ("kind", "request_id", "transfer_id", "sequence"), "ack"
        )
        require_text(data["request_id"], "request_id")
        require_text(data["transfer_id"], "transfer_id")
        require_count(data["sequence"], "sequence")
        return cls(data["request_id"], data["transfer_id"], data["sequence"])


class SessionReady(SyncMessage):
    """会话已建立，附带两个版本。

    **两个版本都在这里，且名字不同**：客户端把它们存成两个属性，
    提交命令时只能用 `index_version`。参见本节开头的说明。
    """

    kind = MSG_SESSION_READY
    __slots__ = ("index_version", "item_view_version")

    def __init__(self, index_version, item_view_version):
        if settings.DEBUG:
            require_count(index_version, "index_version")
            require_count(item_view_version, "item_view_version")
        self.index_version = index_version
        self.item_view_version = item_view_version

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {
            "kind": self.kind,
            "index_version": self.index_version,
            "item_view_version": self.item_view_version,
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> SessionReady
        _require_fields(
            data, ("kind", "index_version", "item_view_version"), "session_ready"
        )
        require_count(data["index_version"], "index_version")
        require_count(data["item_view_version"], "item_view_version")
        return cls(data["index_version"], data["item_view_version"])


class SnapshotBegin(SyncMessage):
    kind = MSG_SNAPSHOT_BEGIN
    __slots__ = ("snapshot_id", "item_view_version", "total_items")

    def __init__(self, snapshot_id, item_view_version, total_items):
        if settings.DEBUG:
            require_text(snapshot_id, "snapshot_id")
            require_count(item_view_version, "item_view_version")
            require_count(total_items, "total_items")
        self.snapshot_id = snapshot_id
        self.item_view_version = item_view_version
        self.total_items = total_items

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {
            "kind": self.kind,
            "snapshot_id": self.snapshot_id,
            "item_view_version": self.item_view_version,
            "total_items": self.total_items,
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> SnapshotBegin
        _require_fields(
            data,
            ("kind", "snapshot_id", "item_view_version", "total_items"),
            "snapshot_begin",
        )
        require_text(data["snapshot_id"], "snapshot_id")
        require_count(data["item_view_version"], "item_view_version")
        require_count(data["total_items"], "total_items")
        return cls(
            data["snapshot_id"], data["item_view_version"], data["total_items"]
        )


class SnapshotChunk(SyncMessage):
    kind = MSG_SNAPSHOT_CHUNK
    __slots__ = ("snapshot_id", "chunk_index", "items")

    def __init__(self, snapshot_id, chunk_index, items):
        if settings.DEBUG:
            require_text(snapshot_id, "snapshot_id")
            require_count(chunk_index, "chunk_index")
        self.snapshot_id = snapshot_id
        self.chunk_index = chunk_index
        self.items = tuple(items)

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {
            "kind": self.kind,
            "snapshot_id": self.snapshot_id,
            "chunk_index": self.chunk_index,
            "items": [
                _encode_item(descriptions, key, stored, reserved)
                for key, stored, reserved in self.items
            ],
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> SnapshotChunk
        _require_fields(
            data, ("kind", "snapshot_id", "chunk_index", "items"), "snapshot_chunk"
        )
        require_text(data["snapshot_id"], "snapshot_id")
        require_count(data["chunk_index"], "chunk_index")
        if not isinstance(data["items"], list):
            raise TypeError("snapshot_chunk.items 必须是列表")
        items = tuple(_decode_item(entry, descriptions) for entry in data["items"])
        return cls(data["snapshot_id"], data["chunk_index"], items)


class SnapshotEnd(SyncMessage):
    """快照传输结束。

    `chunk_count` 让客户端**不依赖「最后一个包一定到」**就能判断缺包：
    收齐 `chunk_count` 个分片才算完整，否则重取。
    """

    kind = MSG_SNAPSHOT_END
    __slots__ = ("snapshot_id", "chunk_count")

    def __init__(self, snapshot_id, chunk_count):
        if settings.DEBUG:
            require_text(snapshot_id, "snapshot_id")
            require_count(chunk_count, "chunk_count")
        self.snapshot_id = snapshot_id
        self.chunk_count = chunk_count

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {
            "kind": self.kind,
            "snapshot_id": self.snapshot_id,
            "chunk_count": self.chunk_count,
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> SnapshotEnd
        _require_fields(
            data, ("kind", "snapshot_id", "chunk_count"), "snapshot_end"
        )
        require_text(data["snapshot_id"], "snapshot_id")
        require_count(data["chunk_count"], "chunk_count")
        return cls(data["snapshot_id"], data["chunk_count"])


class ItemDelta(SyncMessage):
    """一个发布窗口的增量，可能拆成多包。

    **一次窗口 = 一次版本推进。** 整批的所有包共享同一对
    `base_version` / `new_version`，客户端**收齐 `packet_count` 个包才应用**，
    缺包即断档。不采用「V→V+1→…→V+N 链式推进」：那会把一个窗口变成 N 次
    版本推进，断档判定随之复杂化，而收益只是让单包能独立应用。
    """

    kind = MSG_ITEM_DELTA
    __slots__ = (
        "batch_id",
        "packet_index",
        "packet_count",
        "base_version",
        "new_version",
        "upserts",
        "removes",
    )

    def __init__(
        self,
        batch_id,
        packet_index,
        packet_count,
        base_version,
        new_version,
        upserts=(),
        removes=(),
    ):
        if settings.DEBUG:
            require_text(batch_id, "batch_id")
            require_count(packet_index, "packet_index")
            require_count(packet_count, "packet_count")
            require_count(base_version, "base_version")
            require_count(new_version, "new_version")
            if packet_count < 1:
                raise ValueError("packet_count 至少为 1")
            if packet_index >= packet_count:
                raise ValueError("packet_index 越界")
        self.batch_id = batch_id
        self.packet_index = packet_index
        self.packet_count = packet_count
        self.base_version = base_version
        self.new_version = new_version
        self.upserts = tuple(upserts)
        self.removes = tuple(removes)

    def is_last(self):
        # type: () -> bool
        return self.packet_index == self.packet_count - 1

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {
            "kind": self.kind,
            "batch_id": self.batch_id,
            "packet_index": self.packet_index,
            "packet_count": self.packet_count,
            "base_version": self.base_version,
            "new_version": self.new_version,
            "upserts": [
                _encode_item(descriptions, key, stored, reserved)
                for key, stored, reserved in self.upserts
            ],
            "removes": [
                _encode_remove(descriptions, key) for key in self.removes
            ],
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> ItemDelta
        _require_fields(
            data,
            (
                "kind",
                "batch_id",
                "packet_index",
                "packet_count",
                "base_version",
                "new_version",
                "upserts",
                "removes",
            ),
            "item_delta",
        )
        require_text(data["batch_id"], "batch_id")
        packet_index = require_count(data["packet_index"], "packet_index")
        packet_count = require_count(data["packet_count"], "packet_count")
        if packet_count < 1:
            raise ValueError("packet_count 至少为 1")
        if packet_index >= packet_count:
            raise ValueError("packet_index 越界")
        base_version = require_count(data["base_version"], "base_version")
        new_version = require_count(data["new_version"], "new_version")
        if not isinstance(data["upserts"], list) or not isinstance(data["removes"], list):
            raise TypeError("item_delta 的 upserts / removes 必须是列表")
        # **先全部解析再交出对象**：中途失败不能留下半批效果。
        upserts = tuple(
            _decode_item(entry, descriptions) for entry in data["upserts"]
        )
        removes = tuple(
            _decode_remove(entry, descriptions) for entry in data["removes"]
        )
        return cls(
            data["batch_id"],
            packet_index,
            packet_count,
            base_version,
            new_version,
            upserts,
            removes,
        )


class ResyncRequired(SyncMessage):
    """服务端判定客户端无法按增量追上，必须重取快照。

    出现即意味着**客户端要丢弃当前缓存**，因此它总是先于新的
    `snapshot_begin` 发出，不能与增量混在同一个批次里。
    """

    kind = MSG_RESYNC_REQUIRED
    __slots__ = ("code", "detail")

    def __init__(self, code, detail=""):
        if settings.DEBUG:
            require_text(code, "code")
        self.code = code
        self.detail = detail

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {"kind": self.kind, "code": self.code, "detail": self.detail}

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> ResyncRequired
        _require_fields(data, ("kind", "code", "detail"), "resync_required")
        require_text(data["code"], "code")
        return cls(data["code"], data["detail"])


class SyncError(SyncMessage):
    """一次请求被拒绝。`code` 用 `Reason` 里的原因码。"""

    kind = MSG_ERROR
    __slots__ = ("request_id", "code", "detail")

    def __init__(self, request_id, code, detail=""):
        if settings.DEBUG:
            require_text(code, "code")
        self.request_id = request_id
        self.code = code
        self.detail = detail

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {
            "kind": self.kind,
            "request_id": self.request_id,
            "code": self.code,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> SyncError
        _require_fields(data, ("kind", "request_id", "code", "detail"), "error")
        require_text(data["code"], "code")
        return cls(data["request_id"], data["code"], data["detail"])


def _build_sync_message_types(classes):
    # type: (tuple) -> dict
    """建立 `kind -> 类` 注册表，并**当场拒绝重复的种类名**。

    重复会让后一个类静静盖掉前一个，而两条消息的字段检查都还在——
    表现成「某个方向的消息总是解析成另一条」，很难从现象追回这里。
    """
    table = {}
    for message_class in classes:
        kind = message_class.kind
        if not kind:
            raise ValueError("%s 没有 kind" % (message_class.__name__,))
        if kind in table:
            raise ValueError("同步消息种类重复：%r" % (kind,))
        table[kind] = message_class
    return table


#: 消息种类注册表。**唯一事实来源**：`decode_sync_message` 用它解析，
#: `sync_message_kinds()` 把它交给测试去和方向表对账。
_SYNC_MESSAGE_TYPES = _build_sync_message_types(
    (
        SessionOpen,
        SessionClose,
        SnapshotRequest,
        Ack,
        SessionReady,
        SnapshotBegin,
        SnapshotChunk,
        SnapshotEnd,
        ItemDelta,
        ResyncRequired,
        SyncError,
    )
)


def sync_message_kinds():
    # type: () -> frozenset
    """已注册的种类集合。给测试与方向表对账用。"""
    return frozenset(_SYNC_MESSAGE_TYPES)


def decode_sync_message(data, descriptions=None):
    # type: (dict, object) -> SyncMessage
    """按 `kind` 解析一条同步消息。**未知种类一律拒绝，不静默忽略。**

    忽略未知种类会让「客户端用了新协议、服务端是旧的」表现成「界面一直
    不刷新」——最难查的那种故障。这里当场报错，故障就落在协议边界上。
    """
    if not isinstance(data, dict):
        raise TypeError("同步消息必须是字典，得到 %r" % (type(data),))
    kind = data.get("kind")
    message_class = _SYNC_MESSAGE_TYPES.get(kind)
    if message_class is None:
        raise ValueError("未知的同步消息种类 %r" % (kind,))
    return message_class.from_dict(data, descriptions)


class SyncEnvelope(object):
    """同步包的外层：**路由字段 + 一个消息体**。

    路由字段刻意**不放进消息体**（而是嵌套成 `message`），有两个好处：

    1. 「每个服务端→客户端包都带 session_id 与 epoch」成为结构保证，
       不必靠每条消息各自记得写一遍、也不会因为漏写而静默失效；
    2. 消息体的字段检查只看得到自己的字段，未知字段的判定不会被
       路由字段干扰。

    方向（谁发给谁）由消息种类决定，因此**不额外下发一个 direction 字段**：
    可推导的冗余字段迟早会和被推导的那个对不上。
    """

    __slots__ = ("message", "session_id", "network_id", "epoch", "view_kind")

    def __init__(self, message, session_id="", network_id="", epoch="", view_kind=VIEW_ITEM):
        if settings.DEBUG:
            self._validate(message, session_id, network_id, epoch, view_kind)
        self.message = message
        self.session_id = session_id
        self.network_id = network_id
        self.epoch = epoch
        self.view_kind = view_kind

    @staticmethod
    def _validate(message, session_id, network_id, epoch, view_kind):
        # type: (SyncMessage, str, str, str, str) -> None
        """方向相关的路由约束。构造与解析**共用**，两条路径不会分叉。"""
        if view_kind not in VIEW_KINDS:
            raise ValueError("未知的视图种类 %r" % (view_kind,))
        kind = message.kind
        if kind in SERVER_TO_CLIENT_KINDS:
            # `error` 是**唯一的例外**：会话不存在、会话属于别人、协议版本
            # 对不上，这些情形下服务端根本没有可填的会话与网络。它带
            # `request_id`，客户端按它把错误挂回对应的那次请求（因此不是
            # 「无名无姓的广播」）。若也要求非空，服务端就只能要么不发、
            # 要么硬编一个假会话——后者会让客户端以为那个会话存在。
            if kind == MSG_ERROR:
                return
            require_text(session_id, "session_id")
            require_text(network_id, "network_id")
            require_text(epoch, "epoch")
            return
        if kind not in CLIENT_TO_SERVER_KINDS:
            raise ValueError("未知的同步消息种类 %r" % (kind,))
        # `session_open` 是唯一一条在会话存在之前发出的消息，
        # 其余 C2S 消息必须带会话——否则服务端无从判断「这条是谁的」。
        if kind != MSG_SESSION_OPEN:
            require_text(session_id, "session_id")

    def direction(self):
        # type: () -> str
        return (
            "s2c" if self.message.kind in SERVER_TO_CLIENT_KINDS else "c2s"
        )

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {
            "schema_version": SYNC_SCHEMA_VERSION,
            "session_id": self.session_id,
            "network_id": self.network_id,
            "epoch": self.epoch,
            "view_kind": self.view_kind,
            "message": self.message.to_dict(descriptions),
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> SyncEnvelope
        _require_fields(
            data,
            (
                "schema_version",
                "session_id",
                "network_id",
                "epoch",
                "view_kind",
                "message",
            ),
            "sync envelope",
        )
        # 版本先于一切：拿旧解析器读新格式，最可能的后果是字段语义变了
        # 而字段名没变，那是最危险的一种「能解析但意思不对」。
        if data["schema_version"] != SYNC_SCHEMA_VERSION:
            raise ValueError(
                "不支持的同步协议版本 %r（本端为 %d）"
                % (data["schema_version"], SYNC_SCHEMA_VERSION)
            )
        message = decode_sync_message(data["message"], descriptions)
        # 解析路径的检查**无条件执行**：这是来自对面的数据，不能因为
        # 本端没开 DEBUG 就把边界检查关掉。
        cls._validate(
            message,
            data["session_id"],
            data["network_id"],
            data["epoch"],
            data["view_kind"],
        )
        return cls(
            message,
            data["session_id"],
            data["network_id"],
            data["epoch"],
            data["view_kind"],
        )

    def __repr__(self):
        return "SyncEnvelope(%s, session=%r)" % (
            self.message.kind,
            self.session_id,
        )

