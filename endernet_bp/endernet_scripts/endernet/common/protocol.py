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

from .definitions import PATTERN_MODES
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
    #: 会话已失效（终端关闭、玩家离开、会话被顶替、空闲超时回收）。
    #: 与「越权」分开：前者要重新开会话，后者是权限问题（架构规划 6.6）。
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
    #: 合成终端九宫格里还有真实原料，因此拒绝导入（11-04）。
    #: **不是 `SLOT_OCCUPIED`**：那说的是「某一格被占了」，这只说
    #: 「要先点清空」——文档第 8 节明确不许覆盖或删除已有实物，
    #: 而在没有规则说清旧料怎么处理之前，唯一的出路是把选择权交回玩家。
    CRAFT_GRID_OCCUPIED = "craft_grid_occupied"
    #: 按这个产物查不到可用配方（11-04）。**与 `PLAN_NO_RECIPE` 分开**：
    #: 那条是工厂规划里「这条样板没有配方」，这条是「这个物品在引擎的
    #: 工作台配方表里查不到」，界面要指的东西不一样。
    CRAFT_NO_RECIPE = "craft_no_recipe"
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

    # 样板终端（11-05）
    #: 这一格在当前模式下不能改。**与 `UNSUPPORTED` 分开**：那条说「这个动作
    #: 不存在」，这条说「动作存在、格子也在，只是这个模式下它是只读的」——
    #: 合成模式的三个输出格就是这一种（上下是固定图标，中间是自动预览）。
    #: 也不并进 `SLOT_OCCUPIED`：那条说「别人占着这个位置」。
    TEMPLATE_CELL_LOCKED = "template_cell_locked"
    #: 当前的虚拟配置还构不成一块合法样板（输入空、产物空、摆位与模式不符……），
    #: `detail` 里带 `patterns.PatternError` 的码。
    #: **与 `PATTERN_INVALID` 分开**：那条说的是「世界里那块样板物品的内容坏了」
    #: （读路径），这条说的是「你刚摆的这份配置还不成样板」（写路径）——
    #: 行为者与下一步动作都不一样，与 `CRAFT_GRID_OCCUPIED` / `SLOT_OCCUPIED`
    #: 的分家同一个理由。
    TEMPLATE_CONFIG_INVALID = "template_config_invalid"

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
    #: **没有空闲的配方处理槽位**，新任务这一帧派不出去。
    #: **与 `NO_CAPACITY`（介质容量）和 `BANDWIDTH_*`（负载域带宽）都分开**：
    #: 三者都是「放不下」，但一个要加盘、一个要加处理器、一个要扩基带，
    #: 合并等于把该做的事丢掉。
    #: **它也是可重试的**（在 `executor.RETRYABLE_CODES` 里）：槽位是竞争资源，
    #: 别人跑完就会空出来，订单因此留在运行中等，**不是**被判失败或拒绝。
    ORDER_NO_CAPACITY = "order_no_capacity"
    #: 订单要占的内存字节超过网络当前可用量，**请求被拒绝下达**（R04 已定案）。
    #: **必须与 `ORDER_NO_CAPACITY` 分开**：那条是「等一等就有」，这条是
    #: 「这单现在就不该接下来」——规则原文明确不要「已接受但缺内存、
    #: 自动等待」的那种订单，因此它落进拒绝记录而不是阻塞队列。
    ORDER_NO_MEMORY = "order_no_memory"
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
#
# 两个版本的**产生者不同**，这一点在 11-03 定下命令通道时补记：
# `expected_index_version` 对的是 `StorageService.index_version`（谁能动
# 库存，谁才推进它），`item_view_version` 对的是终端视图自己的发布版本。
# 库存动了视图迟早会动，但两者不是同一个计数器，也不能互相赋值——
# 因此 `TerminalCommand` 只出现前者，`ItemDelta` 只出现后者，没有一条
# 消息同时带两个。

SYNC_SCHEMA_VERSION = 1

#: 客户端→服务端与 服务端→客户端 的事件名（`CustomC2SEvent` / `CustomS2CEvent`）
SYNC_CHANNEL_C2S = "endernet:terminal_request"
SYNC_CHANNEL_S2C = "endernet:terminal_sync"

#: 「这台设备是哪个实例」的下行通道（11-07）。
#:
#: **不搭在同步通道上**，因为容器类设备（总线接口、装配仓一类）的界面由
#: **引擎**按方块组件自动打开，脚本侧的屏幕是挂上去的代理（`is_proxy=True`），
#: 它**拿不到任何参数**，也就不知道自己对应哪台方块。身份必须由服务端在
#: 玩家右键那一刻查到注册表后推过去，而那一刻**还没有会话**（会话要等屏
#: 挂上代理、客户端采纳身份之后才开），因此它不能是一条会话内的消息。
#:
#: 载荷形状见 `common/device_identity.py`；事件名两侧必须逐字相同。
DEVICE_IDENTITY_CHANNEL_S2C = "endernet:device_identity"

#: 视图种类。两种都**是已知种类**，且都已有生产者；与「客户端发来一个我们
#: 不认识的种类」必须给出不同的拒绝理由——前者是 `Reason.UNSUPPORTED`
#: （还没做），后者是协议错误（发错了）。
#:
#: `order` 的生产者由 11-06（工厂终端）接上：会话按 `VIEW_ORDER` 打开时，
#: 服务端推 `MSG_ORDER_STATE` 而不是物品快照，`_service_network` 按这个
#: 字段分流。**在 11-06 之前它只出现在 `VIEW_KINDS` 里**，开会话会被
#: `sessions.check` 以「尚未接入（依赖 11-06/11-09）」拒掉。
VIEW_ITEM = "item"
VIEW_ORDER = "order"
VIEW_KINDS = (VIEW_ITEM, VIEW_ORDER)
VIEW_KINDS_WITH_PRODUCER = (VIEW_ITEM, VIEW_ORDER)

MSG_SESSION_OPEN = "session_open"
MSG_SESSION_CLOSE = "session_close"
MSG_SNAPSHOT_REQUEST = "snapshot_request"
MSG_ACK = "ack"
#: 「我还开着，别回收」（11-07）。**只读的设备屏一条命令都不发**，而会话按
#: 「多久没有 C2S 消息」回收（`SessionPolicy.session_ttl_frames`，出厂 600 帧
#: ≈20 秒），于是状态栏会在 20 秒后失效——与 D-08 同一根因。这条消息只让
#: 服务端刷新该会话的 `last_seen_frame`，**不改 TTL、不产生任何业务**。
MSG_KEEPALIVE = "keepalive"
MSG_SESSION_READY = "session_ready"
MSG_INVENTORY_SNAPSHOT = "inventory_snapshot"
MSG_SNAPSHOT_BEGIN = "snapshot_begin"
MSG_SNAPSHOT_CHUNK = "snapshot_chunk"
MSG_SNAPSHOT_END = "snapshot_end"
MSG_ITEM_DELTA = "item_delta"
MSG_RESYNC_REQUIRED = "resync_required"
MSG_ERROR = "error"
MSG_COMMAND = "command"
MSG_COMMAND_RESULT = "command_result"
#: 设备总体状态（11-08/11-13）。服务端单向推给客户端，见 `DeviceStatusMessage`。
MSG_DEVICE_STATUS = "device_status"
#: 合成终端的工作台状态（11-04）。服务端单向推，见 `CraftStateMessage`。
MSG_CRAFT_STATE = "craft_state"
#: 样板终端的真实槽与虚拟配置（11-05）。服务端单向推，见 `TemplateStateMessage`。
MSG_TEMPLATE_STATE = "template_state"
#: 工厂终端的活跃订单、剩余内存与配方树进度（11-06）。服务端单向推，
#: 见 `OrderStateMessage`。**搭在已有的同步通道上**（`view_kind=VIEW_ORDER`），
#: 不新开通道，也不复用物品快照那套（订单不是库存）。
MSG_ORDER_STATE = "order_state"
#: 下单预览的**状态头**与**原料行分片**（11 增量）。服务端单向推。
#: 见 `PreviewStateMessage` / `PreviewChunkMessage` 与
#: `plans/technical_notes/terminal_pattern_order_preview.md`。
MSG_PREVIEW_STATE = "preview_state"
MSG_PREVIEW_CHUNK = "preview_chunk"
#: 一台**接口设备**的权威配置（11-07）。服务端单向推，见 `PortConfigMessage`。
#: **与 `MSG_DEVICE_STATUS` 各管一半**：那条说「这台设备现在能不能干活」，
#: 这条说「玩家把它配成了什么样」。两条都按会话的设备身份发，因此任何一台
#: 设备的屏都能同时收到；非接口设备收不到这一条（服务端读不到就不发）。
MSG_PORT_CONFIG = "port_config"

CLIENT_TO_SERVER_KINDS = frozenset(
    [
        MSG_SESSION_OPEN,
        MSG_SESSION_CLOSE,
        MSG_SNAPSHOT_REQUEST,
        MSG_ACK,
        MSG_KEEPALIVE,
        MSG_COMMAND,
    ]
)
SERVER_TO_CLIENT_KINDS = frozenset(
    [
        MSG_INVENTORY_SNAPSHOT,
        MSG_SESSION_READY,
        MSG_SNAPSHOT_BEGIN,
        MSG_SNAPSHOT_CHUNK,
        MSG_SNAPSHOT_END,
        MSG_ITEM_DELTA,
        MSG_RESYNC_REQUIRED,
        MSG_ERROR,
        MSG_COMMAND_RESULT,
        MSG_DEVICE_STATUS,
        MSG_CRAFT_STATE,
        MSG_TEMPLATE_STATE,
        MSG_ORDER_STATE,
        MSG_PREVIEW_STATE,
        MSG_PREVIEW_CHUNK,
        MSG_PORT_CONFIG,
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


def require_flag(value, name):
    # type: (object, str) -> bool
    """线格式布尔校验：**只收真正的 `bool`**。

    与 `require_count` 同一条取向：`1` / `"true"` / `[]` 都能在 `if` 下冒充
    真值，而它们落到线上之后，对面解出来的就是另一个类型——「快照说这个
    资源有样板、增量说没有」这类偏差会从一个类型错误开始，
    却在那条消息的**语义**上表现出来。
    """
    if not isinstance(value, bool):
        raise TypeError("%s 必须是布尔，得到 %r" % (name, value))
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


def _encode_item(descriptions, key, stored, reserved, has_pattern):
    # type: (object, ResourceKey, int, int, bool) -> dict
    """快照与增量**共用**的条目编码，防止两条路径漂移。

    `has_pattern` **刻意没有默认值**：它是「显示来源」而不是库存数量，
    任何一处调用点漏改都应当是 `TypeError` 而不是静默的 `False`——
    后者会让「快照带标记、增量丢标记」在线上表现为点击后 `+` 随机消失。
    """
    require_count(stored, "stored")
    require_count(reserved, "reserved")
    require_flag(has_pattern, "has_pattern")
    if reserved > stored:
        raise ValueError("Reserved count exceeds stored count")
    if descriptions is None:
        return {
            "resource_key": key.canonical(),
            "resource_description": key.to_dict(),
            "stored": stored,
            "reserved": reserved,
            "has_pattern": has_pattern,
        }
    return {
        "resource_ref": descriptions.encode(key),
        "stored": stored,
        "reserved": reserved,
        "has_pattern": has_pattern,
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
    """`_encode_item` 的逆。**所有检查通过后才认这把键。**

    返回四元组（末位是 `has_pattern`）。**它不接受缺项的 `has_pattern`**：
    与编码侧同一条取向——线上少一个字段就是协议错误，不是「默认没有样板」。
    """
    if "resource_ref" in data:
        _require_fields(
            data, ("resource_ref", "stored", "reserved", "has_pattern"), "item entry"
        )
        if descriptions is None:
            raise ValueError("收到资源引用，但当前没有会话描述表")
        key = descriptions.decode(data["resource_ref"])
    else:
        _require_fields(
            data,
            (
                "resource_key",
                "resource_description",
                "stored",
                "reserved",
                "has_pattern",
            ),
            "item entry",
        )
        key = ResourceKey.from_dict(data["resource_description"])
        if data["resource_key"] != key.canonical():
            raise ValueError("Resource fingerprint mismatch")
    stored = require_count(data["stored"], "stored")
    reserved = require_count(data["reserved"], "reserved")
    if reserved > stored:
        raise ValueError("Reserved count exceeds stored count")
    has_pattern = require_flag(data["has_pattern"], "has_pattern")
    return key, stored, reserved, has_pattern


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


class KeepaliveMessage(SyncMessage):
    """「这个会话还在被看着」——**唯一的用途是刷新服务端的空闲计时**。

    **不带任何业务字段**，也不带 `cursor` / 版本之类的可选位：它不表达
    「客户端手上有什么」，只表达「这一刻客户端还活着」。因此服务端收到它
    除了 `touch()` 之外什么都不做，也不回包——回一条 ack 只会让客户端的
    在途账本涨一条永远没人取走的记录。

    `request_id` 照其它 C2S 消息给：服务端在会话失效时用它回一条 `error`，
    没有它那条错误就成了一条无名无姓的通知。
    """

    kind = MSG_KEEPALIVE
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
        # type: (dict, object) -> KeepaliveMessage
        _require_fields(data, ("kind", "request_id"), "keepalive")
        require_text(data["request_id"], "request_id")
        return cls(data["request_id"])


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


class InventorySnapshot(SyncMessage):
    """当前玩家背包的服务端观察版本，槽号使用引擎顺序。"""

    kind = MSG_INVENTORY_SNAPSHOT
    __slots__ = ("revision", "slots")

    def __init__(self, revision, slots):
        require_count(revision, "revision")
        if not isinstance(slots, (list, tuple)) or len(slots) != 36:
            raise ValueError("inventory snapshot requires 36 slots")
        rows = []
        for index, row in enumerate(slots):
            _require_fields(row, ("slot", "canonical", "count", "slot_version"),
                            "inventory slot")
            require_count(row["slot"], "slot")
            require_count(row["count"], "count")
            require_count(row["slot_version"], "slot_version")
            if row["slot"] != index or row["slot_version"] < 1:
                raise ValueError("invalid inventory slot/version")
            if row["canonical"] != "":
                require_text(row["canonical"], "canonical")
            rows.append(dict(row))
        self.revision = revision
        self.slots = tuple(rows)

    def to_dict(self, descriptions=None):
        return {"kind": self.kind, "revision": self.revision,
                "slots": [dict(row) for row in self.slots]}

    @classmethod
    def from_dict(cls, data, descriptions=None):
        _require_fields(data, ("kind", "revision", "slots"), "inventory_snapshot")
        return cls(data["revision"], data["slots"])


def _require_slot_row(row, index, extra=()):
    # type: (object, int, object) -> dict
    """一行槽位状态的形状检查。**两条消息共用**（11-04 的九宫格与 11-05 的
    样板状态），免得两种行形状各自漂移——分叉的代价是「一边多了一个字段、
    另一边解析时炸」，而那只有跑起来才看得见。

    判据（与 `CraftStateMessage` 的文档同源）：

    - 槽号必须**按位置连续**（`row["slot"] == index`）；
    - `canonical` 为空即空位，此时 `name` / `aux` 允许一起为空；
    - **非空位必须给出 `name`**：客户端画一格靠 `#item_id_aux`，那是
      `Item(资源名).GetBasicInfo()`，从 `canonical()` 那个指纹里反解名字是
      拿格式当接口。取不到名字的键是**消息的 bug**，不是空位。
    - `extra` 里列出的字段一并要求存在（样板状态多一个 `locked`）。
    """
    fields = ("slot", "canonical", "count", "name", "aux") + tuple(extra)
    _require_fields(row, fields, "slot row")
    require_count(row["slot"], "slot")
    require_count(row["count"], "count")
    if row["slot"] != index:
        raise ValueError("invalid slot index")
    if row["canonical"] != "":
        require_text(row["canonical"], "canonical")
    if row["name"] != "":
        require_text(row["name"], "name")
    elif row["canonical"] != "":
        raise ValueError("slot row has a key but no item name")
    require_count(row["aux"], "aux")
    return dict(row)


#: 合成终端工作台的槽位数。与 `crafting_recipe.SLOT_COUNT` 是同一个数
#: （原版工作台九格）；这里再写一份是因为 `common/` 内部两模块各有各的
#: 事实来源，而同步层**必须**钉住线上槽数，不能跟着对面变。
CRAFT_SLOT_COUNT = 9


class CraftStateMessage(SyncMessage):
    """合成终端九宫格的状态（11-04）。服务端单向推。

    **不能复用 `InventorySnapshot`**：那个类把槽数钉死成 36（玩家背包），
    九宫格是 9 格，而且比它多两样东西——保留的配方，和产物预览。

    - `slots`：9 行 `{slot, canonical, count, name, aux}`。空位四条一起空。
      **`name` / `aux` 与 `canonical` 不是重复**：客户端画一格靠
      `#item_id_aux`，那是 `Item(资源名).GetBasicInfo().id_aux`，
      从 `canonical()` 那个指纹里反解名字是拿格式当接口。
      `InventorySnapshot` 只用 `canonical`，是因为玩家背包客户端本地就能读
      引擎、它拿那个字段做槽锚而不做渲染；九宫格客户端读不到，必须带上。
      **没有每槽版本**：本轮九宫格不接受客户端改格（手动摆放属 R12），
      客户端只读它，因此没有需要防的过期写。整台的版本由 `revision` 承担。
    - `recipe`：上次导入的配方标识（`PresetRecipe.digest()`），没有就是空串。
      **它只是备忘**，服务端做合成判定时看的是格子里实际有什么。
    - `preview`：当前摆位能合成出来的产物，`{name, aux, count}` 或 `None`。
      **`None` 不等于「不能合成」这句话的完整表述**——配方能对上但原料
      数量不足也算 `None`，因此界面据它显示的是「点了会怎样」，
      不是「配方存不存在」。
    """

    kind = MSG_CRAFT_STATE
    __slots__ = ("revision", "slots", "recipe", "preview")

    #: 线上字段的唯一清单，构造与解析共用。
    FIELDS = ("kind", "revision", "slots", "recipe", "preview")

    def __init__(self, revision, slots, recipe="", preview=None):
        require_count(revision, "revision")
        if not isinstance(slots, (list, tuple)) or len(slots) != CRAFT_SLOT_COUNT:
            raise ValueError("craft state requires %d slots" % (CRAFT_SLOT_COUNT,))
        rows = [_require_slot_row(row, index)
                for index, row in enumerate(slots)]
        if not isinstance(recipe, _TEXT_TYPES):
            raise TypeError("recipe 必须是文本，得到 %r" % (type(recipe),))
        if preview is not None:
            _require_fields(preview, ("name", "aux", "count"), "craft preview")
            require_text(preview["name"], "name")
            require_count(preview["aux"], "aux")
            require_count(preview["count"], "count")
            preview = dict(preview)
        self.revision = revision
        self.slots = tuple(rows)
        self.recipe = recipe
        self.preview = preview

    def to_dict(self, descriptions=None):
        return {
            "kind": self.kind,
            "revision": self.revision,
            "slots": [dict(row) for row in self.slots],
            "recipe": self.recipe,
            "preview": None if self.preview is None else dict(self.preview),
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        _require_fields(data, cls.FIELDS, "craft_state")
        return cls(data["revision"], data["slots"], data["recipe"], data["preview"])


#: 样板终端的两片虚格各有几格。**与 `template_terminal` 的同名常量是同一个
#: 数**，这里再写一份的理由与 `CRAFT_SLOT_COUNT` 相同：同步层必须钉住线上
#: 格数，不能跟着对面变。
TEMPLATE_SOURCE_CELLS = 9
TEMPLATE_OUTPUT_CELLS = 3
#: 设备一共有几个真实槽。**它不在这条消息的形状里**：两个槽各自成组
#: （`input` / `result`），每组一行，因此线上每组的行数是下面那个 1。
#: 两个常量分开写，是因为「设备有几个槽」与「一条消息几行」是两个问题——
#: 合成起来第一个改的人就会改错另一个。
TEMPLATE_REAL_SLOTS = 2
#: `input` / `result` 两组各自的行数。
TEMPLATE_REAL_SLOT_ROWS = 1


def _template_rows(rows, count, label):
    # type: (object, int, str) -> tuple
    """一组样板状态行：格数要对，每行的 `locked` 必须是真布尔。"""
    if not isinstance(rows, (list, tuple)) or len(rows) != count:
        raise ValueError("%s requires %d rows" % (label, count))
    out = []
    for index, row in enumerate(rows):
        checked = _require_slot_row(row, index, extra=("locked",))
        # `locked` 不接受 0/1/None：它决定界面上那一格能不能点，
        # 一个「看着像真」的值会让只读格变成可编辑的。
        if not isinstance(checked["locked"], bool):
            raise TypeError("%s 的 locked 必须是布尔，得到 %r"
                            % (label, type(checked["locked"])))
        out.append(checked)
    return tuple(out)


class TemplateStateMessage(SyncMessage):
    """样板终端的两个真实槽与一份虚拟配置（11-05）。服务端单向推。

    四组行同形（`{slot, canonical, count, name, aux, locked}`），因此共用
    同一个校验器：

    - `source`：9 格虚拟输入九宫格；
    - `outputs`：3 格虚拟输出格。合成模式下 `locked` 全真——上下两格不显示、
      中间那格是自动检测出来的预览，玩家都不能改；
    - `input` / `result`：两个真实样板槽，各 1 行，恒不锁。

    **`locked` 由服务端给，不由客户端按模式推。** 客户端自己推的话，「哪一格
    在什么模式下只读」就有两份，而两份迟早分叉（模式一改，客户端那份还在按
    旧的画），症状是「点了没反应」，看不出是服务端拒的还是本地压根没发。

    `limits` 带 `{"max_count": int, "default_count": int}`：数量的上下限是
    服务端的策略值，只有一处，客户端的数量窗照它开。

    **行里的 `name` / `aux` 与 `canonical` 不重复**，理由与
    `CraftStateMessage` 逐字相同：真实槽与虚格客户端都读不到引擎，画一格
    只能靠服务端把资源名一起带过来。装配处见
    `bootstrap._sync_template_of`。
    """

    kind = MSG_TEMPLATE_STATE
    __slots__ = ("revision", "mode", "source", "outputs", "input", "result",
                 "limits")

    #: 线上字段的唯一清单，构造与解析共用。
    FIELDS = ("kind", "revision", "mode", "source", "outputs", "input",
              "result", "limits")

    def __init__(self, revision, mode, source, outputs, input, result, limits):
        require_count(revision, "revision")
        if mode not in PATTERN_MODES:
            raise ValueError("未知样板模式 %r" % (mode,))
        if not isinstance(limits, dict):
            raise TypeError("limits 必须是字典，得到 %r" % (type(limits),))
        _require_fields(limits, ("max_count", "default_count"), "template limits")
        require_count(limits["max_count"], "max_count")
        require_count(limits["default_count"], "default_count")
        if limits["default_count"] > limits["max_count"]:
            raise ValueError("默认数量不能大于上限")
        if limits["max_count"] < 1:
            raise ValueError("数量上限必须是正数")
        self.revision = revision
        self.mode = mode
        self.source = _template_rows(source, TEMPLATE_SOURCE_CELLS, "source")
        self.outputs = _template_rows(outputs, TEMPLATE_OUTPUT_CELLS, "outputs")
        self.input = _template_rows(input, TEMPLATE_REAL_SLOT_ROWS, "input")
        self.result = _template_rows(result, TEMPLATE_REAL_SLOT_ROWS, "result")
        self.limits = dict(limits)

    def to_dict(self, descriptions=None):
        return {
            "kind": self.kind,
            "revision": self.revision,
            "mode": self.mode,
            "source": [dict(row) for row in self.source],
            "outputs": [dict(row) for row in self.outputs],
            "input": [dict(row) for row in self.input],
            "result": [dict(row) for row in self.result],
            "limits": dict(self.limits),
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        _require_fields(data, cls.FIELDS, "template_state")
        return cls(data["revision"], data["mode"], data["source"],
                   data["outputs"], data["input"], data["result"],
                   data["limits"])


#: 工厂终端配方行的**制作阶段**（11-06）。
#:
#: 走线上的是**整数**，中文只在客户端映射：协议层保持语言中立。这条与
#: `PATTERN_MODES` 同一个地位——两侧共用一份词汇，谁都不许各写一套字符串
#: 再去比。交接（`plans/ui/factoric_terminal_panel.md` §4）定的是三态：
#: 等待中 / 制作中 / 已完成。
ORDER_SOURCE_STAGE_WAITING = 0
ORDER_SOURCE_STAGE_RUNNING = 1
ORDER_SOURCE_STAGE_DONE = 2
ORDER_SOURCE_STAGES = (
    ORDER_SOURCE_STAGE_WAITING,
    ORDER_SOURCE_STAGE_RUNNING,
    ORDER_SOURCE_STAGE_DONE,
)

#: 配方行的**投料／产物状态**。与阶段分开：阶段说的是这条配方整体走到哪，
#: 这一说说的是它此刻卡在哪一头（等料下锅 / 等产物出锅）。同样是三态，
#: 但**不是同一条轴**——合成中且正在等产物的配方，阶段是「制作中」。
ORDER_SOURCE_FEED_WAITING_INPUTS = 0
ORDER_SOURCE_FEED_WAITING_OUTPUTS = 1
ORDER_SOURCE_FEED_DONE = 2
ORDER_SOURCE_FEEDS = (
    ORDER_SOURCE_FEED_WAITING_INPUTS,
    ORDER_SOURCE_FEED_WAITING_OUTPUTS,
    ORDER_SOURCE_FEED_DONE,
)

#: 一行订单摘要的字段。**没有 `steps`**：配方树是**另一张扁平表**，
#: 用 `order_id` 与订单行相认（见 `OrderStateMessage`）。
ORDER_ROW_FIELDS = (
    "order_id", "canonical", "name", "aux", "amount", "status",
    "tasks_completed", "tasks_total", "memory_bytes",
)

#: 一行配方树节点的字段。`done` / `total` 是**物品数**，不是执行次数——
#: 交接 §4 明写「两项使用同一物品数量单位，不混用执行次数与产物数量」，
#: 而订单整体进度那一侧用的才是配方任务数。两个口径各有其名，不许互相顶替。
SOURCE_ROW_FIELDS = (
    "order_id", "canonical", "name", "aux", "stage", "feed", "done", "total",
)


def _require_tristate(value, allowed, name):
    # type: (object, tuple, str) -> int
    """三态枚举校验。**拒 bool**：`True in (0, 1, 2)` 为真，放过去等于
    让一个布尔冒充「制作中」。"""
    if isinstance(value, bool) or value not in allowed:
        raise ValueError("%s 不在 %r 里，得到 %r" % (name, allowed, value))
    return value


def _order_rows(rows):
    # type: (object) -> tuple
    """订单摘要行。行数**不固定**（活跃订单有几条就几条），因此只逐行校验。"""
    if not isinstance(rows, (list, tuple)):
        raise TypeError("orders 必须是列表，得到 %r" % (type(rows),))
    out = []
    for row in rows:
        _require_fields(row, ORDER_ROW_FIELDS, "order row")
        require_text(row["order_id"], "order_id")
        require_text(row["canonical"], "canonical")
        # **名字必须给**：客户端的订单行要画主产物图标与名字，而设备侧读不到
        # 引擎。理由与 `_require_slot_row` 逐字相同，见那里的说明。
        require_text(row["name"], "name")
        require_count(row["aux"], "aux")
        require_count(row["amount"], "amount")
        require_text(row["status"], "status")
        require_count(row["tasks_completed"], "tasks_completed")
        require_count(row["tasks_total"], "tasks_total")
        if row["tasks_completed"] > row["tasks_total"]:
            raise ValueError("已完成任务数不能超过总数")
        require_count(row["memory_bytes"], "memory_bytes")
        out.append(dict(row))
    return tuple(out)


def _source_rows(rows):
    # type: (object) -> tuple
    """配方树行。**行数不固定**：一个订单的树有几条配方就几条。

    `order_id` 必须非空——它是与订单行相认的唯一依据，空串会让这一行
    落进「没有主人」的名单里，而界面上表现成「详情少了几行」。
    """
    if not isinstance(rows, (list, tuple)):
        raise TypeError("sources 必须是列表，得到 %r" % (type(rows),))
    out = []
    for row in rows:
        _require_fields(row, SOURCE_ROW_FIELDS, "source row")
        require_text(row["order_id"], "order_id")
        require_text(row["canonical"], "canonical")
        require_text(row["name"], "name")
        require_count(row["aux"], "aux")
        _require_tristate(row["stage"], ORDER_SOURCE_STAGES, "stage")
        _require_tristate(row["feed"], ORDER_SOURCE_FEEDS, "feed")
        require_count(row["done"], "done")
        require_count(row["total"], "total")
        if row["done"] > row["total"]:
            raise ValueError("已完成数量不能超过总数")
        out.append(dict(row))
    return tuple(out)


class OrderStateMessage(SyncMessage):
    """工厂终端的订单视图（11-06）。服务端单向推，`view_kind=VIEW_ORDER` 专属。

    **不是一条消息一个订单**：一次推本网络**全部活跃订单**，外加它们的
    配方树。理由是详情窗口要求「打开期间随事件更新」——若详情要靠一条
    按需请求，服务端就得为每条会话记一个「他正看着哪一单」，那是第二份
    状态；而活跃订单本来就受 `max_active_orders` 约束，张数很少。

    - `memory`：`{capacity, used, available, slots}`，**单位是字节（B）**。
      任务文档（[11. 终端与客户端同步.md]「11-06 的已确认显示需求」）写的就是
      字节，JSON 里那句静态「0KB」是占位文案，以这里为准。
      `slots` 是**插入的内存条根数**（2026-10-03 起；在那之前报的是内存槽的
      台数，而那时容量正是按台数换算的），与 `capacity` 不是一回事——
      后者是这些内存条的字节合计。
    - `orders`：活跃订单摘要，**非终态**（已完成的订单不再是「运行中」）。
      主产物身份四个字段 `canonical / name / aux / amount` 的理由同
      `_require_slot_row`：客户端画图标只能靠服务端把资源名带过来。
      `status` 是**文本**（`OrderStatus` 的字面值），协议层不为了校验它
      去 import 领域层的枚举——它是显示提示，不是控制输入。
    - `sources`：**全量配方树**，扁平一张表，用 `order_id` 认领。每行是
      订单里的一条配方任务，**已完成的也保留**（交接 §3：全配方树都要显示，
      不能只显示直接原料）。树形由客户端按 `canonical` / 依赖关系重建展示。

    **它不做去重、也不带版本号**：整条是全量快照，服务端在内容没变时不发
    （去重在 `TerminalSyncHub._publish_order_state`）。因此客户端拿到就整份
    替换，不比较新旧。
    """

    kind = MSG_ORDER_STATE
    __slots__ = ("memory", "orders", "sources")

    #: 线上字段的唯一清单，构造与解析共用。
    FIELDS = ("kind", "memory", "orders", "sources")

    #: `memory` 的字段。**四个都要**：界面上那根条的分子是 `available`、
    #: 分母是 `capacity`，少一个就得让客户端去猜另一端。
    MEMORY_FIELDS = ("capacity", "used", "available", "slots")

    def __init__(self, memory, orders, sources):
        if not isinstance(memory, dict):
            raise TypeError("memory 必须是字典，得到 %r" % (type(memory),))
        _require_fields(memory, self.MEMORY_FIELDS, "order memory")
        for name in self.MEMORY_FIELDS:
            require_count(memory[name], name)
        if memory["used"] > memory["capacity"]:
            raise ValueError("已用内存不能超过总量")
        if memory["available"] > memory["capacity"]:
            raise ValueError("可用内存不能超过总量")
        self.memory = dict(memory)
        self.orders = _order_rows(orders)
        self.sources = _source_rows(sources)

    def to_dict(self, descriptions=None):
        return {
            "kind": self.kind,
            "memory": dict(self.memory),
            "orders": [dict(row) for row in self.orders],
            "sources": [dict(row) for row in self.sources],
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        _require_fields(data, cls.FIELDS, "order_state")
        return cls(data["memory"], data["orders"], data["sources"])


#: 预览原料行的一行。`allocated` 是**本计划分配到的那份库存**（不是全网可用，
#: 也不允许同一份库存在多行里重复出现）；`planned` 是这一步要制作的量；
#: `missing` 是这一行最终还差不差料。四个数一个都不能省：少任何一个，
#: 界面上那三种情形（够 / 靠制作补齐 / 补不上）就有两种长得一样。
PREVIEW_ROW_FIELDS = (
    "canonical", "name", "aux", "demand", "allocated", "planned", "missing",
    "sources",
)

#: 整单缺料结论里的一条：哪个键缺、缺在哪。`detail` 是给人看的说明。
PREVIEW_BLOCK_FIELDS = ("canonical", "detail")


def _preview_rows(rows):
    # type: (object) -> tuple
    """预览原料行。行数**不固定**（计划的账本有几条就几条），只逐行校验。"""
    if not isinstance(rows, (list, tuple)):
        raise TypeError("rows 必须是列表，得到 %r" % (type(rows),))
    out = []
    for row in rows:
        _require_fields(row, PREVIEW_ROW_FIELDS, "preview row")
        require_text(row["canonical"], "canonical")
        # **名字必须给**：客户端要画图标与名字，而它读不到引擎。理由与
        # `_require_slot_row` / `_order_rows` 逐字相同。
        require_text(row["name"], "name")
        require_count(row["aux"], "aux")
        for field in ("demand", "allocated", "planned"):
            require_count(row[field], field)
        require_flag(row["missing"], "missing")
        sources = row["sources"]
        if not isinstance(sources, (list, tuple)):
            raise TypeError("sources 必须是列表")
        for canonical in sources:
            require_text(canonical, "sources 里的 canonical")
        out.append(dict(row))
    return tuple(out)


def _preview_blocks(blocks):
    # type: (object) -> tuple
    """整单缺料结论。**空列表表示「没有缺料」**，不是「算不出来」——
    后者走 `status`。"""
    if not isinstance(blocks, (list, tuple)):
        raise TypeError("blocked 必须是列表，得到 %r" % (type(blocks),))
    out = []
    for row in blocks:
        _require_fields(row, PREVIEW_BLOCK_FIELDS, "preview block")
        require_text(row["canonical"], "canonical")
        require_text(row["detail"], "detail")
        out.append(dict(row))
    return tuple(out)


class PreviewStateMessage(SyncMessage):
    """下单预览的状态头（11 增量）。**先于原料行到达。**

    **它到了不等于算完了。** 客户端要等**同一 `generation` 的全部分片**
    到齐才允许提交——契约第 3 节点名的坑正是「收到第一条进度就结束 loading」，
    而那条进度就是这条消息本身。

    - `status` 用**规划器自己的字符串**（`PlanStatus` 的字面值）加两个服务级
      取值 `expired` / `error`。不另造一套词汇：`submit_order` 就是按那几个
      字符串分支的，两边各写一套迟早对不上。
    - `generation` 每重算一次加一。分片带着它，因此**旧一轮的迟到分片
      不会被拼进新一轮**。
    - `submit_ok` 只是给按钮用的**建议**。服务端提交时会独立复核一遍
      （契约第 4 节末句）；`truncated` 为真时它必定为假，因为被截断的计划
      说不上「没缺料」。
    - `inventory_version` / `topology_version` / `plan_digest` 是这次预览
      **依据的是哪一份世界**。它们不参与准入判断，只回答「这份结论是什么
      时候算的」。
    """

    kind = MSG_PREVIEW_STATE
    __slots__ = (
        "request_id",
        "preview_id",
        "generation",
        "status",
        "detail",
        "submit_ok",
        "target",
        "count",
        "semantics",
        "inventory_version",
        "topology_version",
        "plan_digest",
        "row_count",
        "packet_count",
        "missing",
        "blocked",
        "truncated",
        "spent",
    )

    FIELDS = (
        "kind",
        "request_id",
        "preview_id",
        "generation",
        "status",
        "detail",
        "submit_ok",
        "target",
        "count",
        "semantics",
        "inventory_version",
        "topology_version",
        "plan_digest",
        "row_count",
        "packet_count",
        "missing",
        "blocked",
        "truncated",
        "spent",
    )

    #: 目标身份。三个都要：客户端画图标与名字只能靠服务端带过来。
    TARGET_FIELDS = ("canonical", "name", "aux")

    def __init__(self, request_id, preview_id, generation, status, detail,
                 submit_ok, target, count, semantics, inventory_version,
                 topology_version, plan_digest, row_count, packet_count,
                 missing, blocked, truncated, spent):
        require_text(request_id, "request_id")
        #: **空串是合法的**：`preview_request` 被拒时服务端没有预览号可给，
        #: 而客户端仍要收到一条「你那条请求没成」。
        if not isinstance(preview_id, _TEXT_TYPES):
            raise TypeError("preview_id 必须是文本，得到 %r" % (preview_id,))
        require_count(generation, "generation")
        require_text(status, "status")
        #: 说明文字**允许为空**：正常完成时没有话要说。
        if not isinstance(detail, _TEXT_TYPES):
            raise TypeError("detail 必须是文本")
        require_flag(submit_ok, "submit_ok")
        if not isinstance(target, dict):
            raise TypeError("target 必须是字典，得到 %r" % (type(target),))
        _require_fields(target, self.TARGET_FIELDS, "preview target")
        require_text(target["canonical"], "canonical")
        require_text(target["name"], "name")
        require_count(target["aux"], "aux")
        require_count(count, "count")
        if count <= 0:
            raise ValueError("预览数量必须是正整数，得到 %r" % (count,))
        require_text(semantics, "semantics")
        require_count(inventory_version, "inventory_version")
        require_count(topology_version, "topology_version")
        if not isinstance(plan_digest, _TEXT_TYPES):
            raise TypeError("plan_digest 必须是文本")
        require_count(row_count, "row_count")
        require_count(packet_count, "packet_count")
        require_flag(missing, "missing")
        require_flag(truncated, "truncated")
        require_count(spent, "spent")
        self.request_id = request_id
        self.preview_id = preview_id
        self.generation = generation
        self.status = status
        self.detail = detail
        self.submit_ok = submit_ok
        self.target = dict(target)
        self.count = count
        self.semantics = semantics
        self.inventory_version = inventory_version
        self.topology_version = topology_version
        self.plan_digest = plan_digest
        self.row_count = row_count
        self.packet_count = packet_count
        self.missing = missing
        self.blocked = _preview_blocks(blocked)
        self.truncated = truncated
        self.spent = spent

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {
            "kind": self.kind,
            "request_id": self.request_id,
            "preview_id": self.preview_id,
            "generation": self.generation,
            "status": self.status,
            "detail": self.detail,
            "submit_ok": self.submit_ok,
            "target": dict(self.target),
            "count": self.count,
            "semantics": self.semantics,
            "inventory_version": self.inventory_version,
            "topology_version": self.topology_version,
            "plan_digest": self.plan_digest,
            "row_count": self.row_count,
            "packet_count": self.packet_count,
            "missing": self.missing,
            "blocked": [dict(row) for row in self.blocked],
            "truncated": self.truncated,
            "spent": self.spent,
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> PreviewStateMessage
        _require_fields(data, cls.FIELDS, "preview_state")
        return cls(
            data["request_id"],
            data["preview_id"],
            data["generation"],
            data["status"],
            data["detail"],
            data["submit_ok"],
            data["target"],
            data["count"],
            data["semantics"],
            data["inventory_version"],
            data["topology_version"],
            data["plan_digest"],
            data["row_count"],
            data["packet_count"],
            data["missing"],
            data["blocked"],
            data["truncated"],
            data["spent"],
        )


class PreviewChunkMessage(SyncMessage):
    """预览原料行的一批（11 增量）。

    **收齐全部分片才算拿到结果**（契约第 3 节：「不能因收到第一条进度或
    部分行而提前结束 loading」）。`packet_count` 随每片发，因此客户端
    **不必等最后一片**就能知道还差几片——最后一片丢了也不会一直转圈。

    `generation` 与状态头对齐：旧一轮的迟到分片凭它被丢掉。
    """

    kind = MSG_PREVIEW_CHUNK
    __slots__ = ("preview_id", "generation", "packet_index", "packet_count", "rows")

    FIELDS = (
        "kind",
        "preview_id",
        "generation",
        "packet_index",
        "packet_count",
        "rows",
    )

    def __init__(self, preview_id, generation, packet_index, packet_count, rows):
        require_text(preview_id, "preview_id")
        require_count(generation, "generation")
        require_count(packet_index, "packet_index")
        require_count(packet_count, "packet_count")
        if packet_count < 1:
            raise ValueError("packet_count 至少为 1")
        if packet_index >= packet_count:
            raise ValueError("packet_index 越界")
        self.preview_id = preview_id
        self.generation = generation
        self.packet_index = packet_index
        self.packet_count = packet_count
        self.rows = _preview_rows(rows)

    def is_last(self):
        # type: () -> bool
        return self.packet_index == self.packet_count - 1

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {
            "kind": self.kind,
            "preview_id": self.preview_id,
            "generation": self.generation,
            "packet_index": self.packet_index,
            "packet_count": self.packet_count,
            "rows": [dict(row) for row in self.rows],
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> PreviewChunkMessage
        _require_fields(data, cls.FIELDS, "preview_chunk")
        return cls(
            data["preview_id"],
            data["generation"],
            data["packet_index"],
            data["packet_count"],
            data["rows"],
        )


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
                _encode_item(descriptions, key, stored, reserved, has_pattern)
                for key, stored, reserved, has_pattern in self.items
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
                _encode_item(descriptions, key, stored, reserved, has_pattern)
                for key, stored, reserved, has_pattern in self.upserts
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
    """一次请求被拒绝。`code` 用 `Reason` 里的原因码。

    **信封的 `session_id` 是有含义的，不是装饰**（2026-09-30 起）：

    - **空** ⇒ 「这个会话已经不在了」——会话不存在、被别人抢、或距离／维度／
      设备复核不过。客户端收到就**丢掉本会话**。
    - **非空** ⇒ 会话还活着，是**这一条请求自身**失败了（服务端执行命令时
      抛了异常之类）。丢掉它会变成「服务端一次内部错误 = 玩家界面掉线」。

    做成结构性的判据，而不是让客户端列一份「哪些原因码算会话级」的表：
    那份表一旦与服务端漂移，表现是「界面看起来还正常、点什么都没反应」，
    恰恰是最难查的一类。服务端那一侧见 `SyncTerminalHub._error`。

    `error` 也是唯一允许空 `session_id` 的 S2C 消息（会话根本不存在时服务端
    没有可填的），因此它与「S2C 必须带会话」那条规则不冲突。
    """

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


class TerminalCommand(SyncMessage):
    """终端发来的一条手动存取命令（11-03）。

    ## 为什么字段这么多

    这条消息要覆盖 `StorageService` 已实现的五个背包动作（`inv_take` /
    `inv_put` / `inv_split` / `inv_merge` / `inv_quick_move`）与样板终端的
    八条 `template_*`，每个动作各读几个不同的键（见 `payload()`）。
    **全部字段无条件序列化**，缺席写 `None`——`_require_fields` 既拒缺项也
    拒多项，所以「可选字段可以不写」在这里是不成立的。

    ## 它不是只管存取：`order_cancel` 也走这里

    工厂终端（11-06）的取消订单由 `TerminalSyncHub._on_command` 按动作名
    路由成 `CommandKind.ORDER`，交给网络服务而不是存储服务；因此本消息
    的**动作集合不限于存储动作**，`action` 也刻意保持自由字符串（客户端
    不校验动作名）。哪些动作走哪条路由，那张表在 `sync/terminal.py` 里，
    是**唯一**一处。

    这不是负担而是防线：客户端多塞一个字段会**当场**变成协议错误，
    而不是被静静忽略。

    ## `key` 是线上形状的字典

    **不是 `ResourceKey` 实例。** `from_dict` 原样转交、`payload()` 原样转给
    `StorageService`（后者的 `_key_from` 只认 `dict`），`__init__` 在
    `DEBUG` 下会拒掉非字典。塞一个领域对象进去的后果是 `to_dict()` 把它
    **原样写进消息**，引擎的 msgpack 编不了自定义对象——而**那要等发包那
    一刻才炸**，界面上什么都看不到（2026-09-30 真机撞过）。转换的位置在
    `NetworkClient.send_command`。

    ## `config` 是设备配置的**意图**，与 `key` 同样只收线上形状

    `port_configure` 要改哪几项就发哪几项——服务端**合并**而不是整体替换
    （`server/storage/service.py` 的 `_cmd_port_configure`），因此这里是一个
    小字典，不是一份领域对象，`filter` 那一项是资源键字典的列表。
    与 `key` 同一条纪律、同一个理由：塞 `PortConfig` 进去会被 `to_dict()`
    原样写进消息，而引擎的 msgpack 编不了自定义对象，**要到发包那一刻才炸**。
    `__init__` 因此在 `DEBUG` 下拒掉非字典。

    ## 为什么没有这些字段

    - **没有 `player` / `player_id`。** 身份只有引擎在 C2S 事件上附的
      `__id__` 一个起点，服务端从 `session.player_id` 取。定义了这个
      字段，就迟早有一处代码去读它（`SessionOpen` 的文档说得更细）。
    - **没有 `device_ref`。** 目标设备就是 `session.device_ref`，
      而 `sessions._check_target` **每条请求**都已经核对过一次。再带一份
      就是第二个真相源，且给伪造留了口子。
    - **`target` 不是资源键。** 它 11-05 起是**样板终端的虚格面名**
      （`source` / `outputs`），11-03 起是快捷转移的落点（`"network"`）。
      下单与预览要的资源键走 **`key`**（线上就是资源键的字典形式），
      由 `sync/terminal.py` 的动作表别名成工厂读的 `target`——
      **两件事不能共用一个字段名**，共用的表现是下单时 `_read_target`
      收到一个字符串并把它判成坏参数。
    - **没有 `cursor`。** `StorageService._cursor_refusal` 只在
      `payload.get("cursor")` 为真时才拦。不发它 = **没有这只手**，
      这是「缺席」，**不等于**客户端声称「手里没拿东西」——R12 尚未定案
      （见 `plans/tasks/11. 终端与客户端同步.md` 的待定段）。
    - **没有 `op_id`。** 服务端取 `payload.get("op_id") or request_id`；
      不发就自动落到 `request_id`，而去重账本用的正是 `request_id`。
      多发一个 `op_id` 只会造出「账本按 A 去重、动作按 B 记账」的缝隙。

    ## `expected_index_version` 目前**携带但不校验**

    它是 11-01/11-02 定下的词汇（`SessionReady.index_version` 就是把它
    交给客户端的），但本轮**没有任何消费者读它**，这是有意的：

    客户端只在 `session_ready` 那一次拿到 `index_version`，此后
    `_absorb` 不再更新它（`item_delta` 推的是 `item_view_version`，
    另一个版本）。若服务端拿它做全局限定，客户端**自己发的第一条命令**
    就会把 `index_version` 推高，第二条必然被判过期——而死锁：没有任何
    通道会把新值告诉它。

    真正在做过期防护的是**槽锚**（`slot_version` / `into_slot_version`，
    见 `transfer._version_verdict`）：它比全局版本更精确，且客户端能
    逐槽拿到新值。全局版本该不该管、由谁管，属未定规则。
    """

    kind = MSG_COMMAND
    __slots__ = (
        "request_id",
        "expected_index_version",
        "action",
        "key",
        "count",
        "slot",
        "slot_version",
        "into_slot",
        "into_slot_version",
        "target",
        "recipe",
        "cell",
        "order",
        "preview",
        "config",
    )

    #: 本条消息**允许**出现的字段。`from_dict` 用它做严格检查，
    #: `tests` 用它当「不许悄悄加字段」的回归位。
    FIELDS = (
        "kind",
        "request_id",
        "expected_index_version",
        "action",
        "key",
        "count",
        "slot",
        "slot_version",
        "into_slot",
        "into_slot_version",
        "target",
        "recipe",
        "cell",
        "order",
        "preview",
        "config",
    )

    def __init__(
        self,
        request_id,
        action,
        expected_index_version=None,
        key=None,
        count=None,
        slot=None,
        slot_version=None,
        into_slot=None,
        into_slot_version=None,
        target=None,
        recipe=None,
        cell=None,
        order=None,
        preview=None,
        config=None,
    ):
        if settings.DEBUG:
            require_text(request_id, "request_id")
            require_text(action, "action")
            if expected_index_version is not None:
                require_count(expected_index_version, "expected_index_version")
            if count is not None:
                require_count(count, "count")
            # **`key` 与线上同形：字典或 `None`，不是 `ResourceKey`。**
            # 这条检查必须在离线就炸。放过去的话 `to_dict()` 会把对象原样
            # 写进消息，引擎的 msgpack 编不了自定义对象——而那是**发包那一刻
            # 才炸**的，界面上什么都看不到，只有引擎日志里一条
            # `can not serialize 'ResourceKey' object`（2026-09-30 真机撞过）。
            # `payload()` 与 `from_dict` 都按字典转交，这条与它们同源。
            if key is not None and not isinstance(key, dict):
                raise ValueError(
                    "key 必须是字典（线上形状），得到 %r" % (type(key),)
                )
            if cell is not None:
                require_count(cell, "cell")
            if order is not None:
                require_text(order, "order")
            if preview is not None:
                require_text(preview, "preview")
            # **与 `key` 同形：字典或 `None`。** 理由同上（那段注释的
            # 「发包那一刻才炸」对 `config` 一字不差地成立）。
            if config is not None and not isinstance(config, dict):
                raise ValueError(
                    "config 必须是字典（线上形状），得到 %r" % (type(config),)
                )
        self.request_id = request_id
        self.expected_index_version = expected_index_version
        self.action = action
        self.key = key
        self.count = count
        self.slot = slot
        self.slot_version = slot_version
        self.into_slot = into_slot
        self.into_slot_version = into_slot_version
        self.target = target
        self.recipe = recipe
        #: **样板终端的虚格下标**（11-05）。与 `slot` **刻意分开**：
        #: 交接明令「不将虚格下标当作玩家或设备真实槽号」，两个下标空间
        #: 共用一个字段，会让「这个数属于哪个空间」变成逐动作去猜的事，
        #: 而猜错的表现是搬走玩家没点的那一格。
        self.cell = cell
        #: **订单标识**（11-06）。与 `slot` / `cell` 同样刻意独立：订单号
        #: 既不是背包槽号也不是虚格下标，它唯一的用途是「取消这一单」。
        #: 服务端在 `payload()` 里把它写成 `order_id`——`FactoryService`
        #: 读的就是那个键（`executor.py` 的 `cancel` 分支）。
        self.order = order
        #: **下单预览号**（11 增量）。与 `order` 同样刻意独立：预览号既不是
        #: 订单号也不是槽号，它唯一的用途是让「下单」这一下认领前面那次预览
        #: （`executor.py` 的 `_consume_preview` 按它校验并消费）。
        #: 服务端在 `payload()` 里写成 `preview_id`。
        self.preview = preview
        #: **设备配置的意图**（11-07）：`{"cache_enabled": False}` 或
        #: `{"filter": [资源键字典, ...]}` 这类**只写要改的那几项**的字典。
        #: 与 `cell` / `order` / `preview` 同样刻意独立——配置项既不是槽号
        #: 也不是虚格下标，共用一个字段会让「这个数属于哪个空间」变成逐动作
        #: 去猜的事。服务端读的键就叫 `config`，因此 `payload()` 是直传。
        self.config = config

    def payload(self):
        # type: () -> dict
        """交给 `StorageService` 的载荷。**字段名映射只此一处。**

        服务端那一侧一律用 `payload.get(...)` 读，因此缺席的键直接不写
        即可（写 `None` 与不写等价）。这不影响消息本身的严格检查——
        严格检查在 `to_dict`/`from_dict` 那一层，管的是线格式。

        `count` 例外地总是写上：`inv_quick_move` 靠它的**假值**判断
        「整槽搬走」，而 `None` 恰好是假值，两种写法在服务端等价，
        写成总是出现只是让「客户端到底说没说数量」在日志里看得见。
        """
        data = {"action": self.action, "count": self.count}
        if self.key is not None:
            data["key"] = self.key
        if self.slot is not None:
            data["slot"] = self.slot
        if self.slot_version is not None:
            data["slot_version"] = self.slot_version
        if self.into_slot is not None:
            data["into_slot"] = self.into_slot
        if self.into_slot_version is not None:
            data["into_slot_version"] = self.into_slot_version
        if self.target is not None:
            data["target"] = self.target
        if self.recipe is not None:
            data["recipe"] = self.recipe
        if self.cell is not None:
            data["cell"] = self.cell
        # 线上叫 `order`、服务端读 `order_id`：**映射只在这一处**（本方法
        # 的存在理由就是这个）。`FactoryService` 是按 `order_id` 读的。
        if self.order is not None:
            data["order_id"] = self.order
        # 线上叫 `preview`、服务端读 `preview_id`：与上面 `order` 同一条理由，
        # 映射仍然只在这一处。
        if self.preview is not None:
            data["preview_id"] = self.preview
        # `config` 的线上名与服务端读的键**同名**（`payload.get("config")`），
        # 因此这里是直传。写出来只为了让「哪些字段会上线」在这一处看得全。
        if self.config is not None:
            data["config"] = self.config
        return data

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {
            "kind": self.kind,
            "request_id": self.request_id,
            "expected_index_version": self.expected_index_version,
            "action": self.action,
            "key": self.key,
            "count": self.count,
            "slot": self.slot,
            "slot_version": self.slot_version,
            "into_slot": self.into_slot,
            "into_slot_version": self.into_slot_version,
            "target": self.target,
            "recipe": self.recipe,
            "cell": self.cell,
            "order": self.order,
            "preview": self.preview,
            "config": self.config,
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> TerminalCommand
        _require_fields(data, cls.FIELDS, "command")
        require_text(data["request_id"], "request_id")
        require_text(data["action"], "action")
        if data["expected_index_version"] is not None:
            require_count(data["expected_index_version"], "expected_index_version")
        if data["count"] is not None:
            require_count(data["count"], "count")
        if data["recipe"] is not None:
            require_text(data["recipe"], "recipe")
        if data["cell"] is not None:
            require_count(data["cell"], "cell")
        if data["order"] is not None:
            require_text(data["order"], "order")
        if data["preview"] is not None:
            require_text(data["preview"], "preview")
        # **无条件检查**（同 `CommandResultMessage.from_dict` 与
        # `SyncEnvelope.from_dict`）：这是来自对面的数据，本端没开 DEBUG
        # 不能成为关掉边界检查的理由。`key` 那一条只在 `__init__` 里查，
        # 这里补上是因为 `config` 里装着要写进权威状态的配置。
        if data["config"] is not None and not isinstance(data["config"], dict):
            raise ValueError(
                "config 必须是字典（线上形状），得到 %r" % (type(data["config"]),)
            )
        return cls(
            data["request_id"],
            data["action"],
            data["expected_index_version"],
            data["key"],
            data["count"],
            data["slot"],
            data["slot_version"],
            data["into_slot"],
            data["into_slot_version"],
            data["target"],
            data["recipe"],
            data["cell"],
            data["order"],
            data["preview"],
            data["config"],
        )


class CommandResultMessage(SyncMessage):
    """一条命令的结果回执（11-03）。

    `result` 就是 `CommandResult.to_dict()` 的原样字典。**这一层不重建
    领域对象**：`CommandResult` 与 `TransferReceipt` 都没有 `from_dict`，
    而回执是给界面看的展示数据（「成功/部分/过期/被拒/受阻」加上原因码），
    重建一份出来只会多一个会与真身漂移的副本。

    因此这里只验**形状**：`status` 是已知状态、`payload` 是字典、
    `reasons` 是 `[{code, detail}]`。形状不对就当协议错误拒绝整条消息——
    一个形状都不对的结果，界面拿它做不了任何判断。

    `request_id` 在消息层与 `result` 里各有一份，且**必须相等**。这是
    指纹式核对（同 `_decode_item` 的 `resource_key` 比对）：两份不一致
    说明消息在某一层被拼错了，此时按哪一份去挂回请求都是猜。
    """

    kind = MSG_COMMAND_RESULT
    __slots__ = ("request_id", "result")

    def __init__(self, request_id, result):
        if settings.DEBUG:
            require_text(request_id, "request_id")
            self._validate_result(request_id, result)
        self.request_id = request_id
        self.result = result

    @staticmethod
    def _validate_result(request_id, result):
        # type: (str, object) -> None
        if not isinstance(result, dict):
            raise TypeError("命令结果必须是字典，得到 %r" % (type(result),))
        _require_fields(
            result,
            ("request_id", "status", "payload", "reasons", "topology_version"),
            "command_result.result",
        )
        require_text(result["request_id"], "command_result.result.request_id")
        if result["request_id"] != request_id:
            raise ValueError(
                "命令结果的 request_id 与消息层不一致：%r != %r"
                % (result["request_id"], request_id)
            )
        if result["status"] not in ALL_STATUSES:
            raise ValueError("未知的命令结果状态 %r" % (result["status"],))
        if not isinstance(result["payload"], dict):
            raise TypeError("命令结果的 payload 必须是字典")
        reasons = result["reasons"]
        if not isinstance(reasons, (list, tuple)):
            raise TypeError("命令结果的 reasons 必须是列表")
        for reason in reasons:
            if isinstance(reason, BlockReason):
                continue
            _require_fields(reason, ("code", "detail"), "command_result.reason")
            require_text(reason["code"], "command_result.reason.code")

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {"kind": self.kind, "request_id": self.request_id, "result": self.result}

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> CommandResultMessage
        _require_fields(data, ("kind", "request_id", "result"), "command_result")
        require_text(data["request_id"], "request_id")
        # 解析路径的检查**无条件执行**（同 `SyncEnvelope.from_dict`）：
        # 这是来自对面的数据，本端没开 DEBUG 不能成为关掉边界检查的理由。
        cls._validate_result(data["request_id"], data["result"])
        return cls(data["request_id"], data["result"])


#: `DeviceStatusMessage.validity` 的取值。`pending` = 结论还没确认——界面
#: **照样显示「离线」**，只有 `reasons` 说明为什么还没确认（没有第三态）。
DEVICE_STATUS_VALIDITY = ("ok", "pending")


class DeviceStatusMessage(SyncMessage):
    """一台设备的**总体**在线/离线与原因（11-08 / 11-13）。

    **与进程内的 `DeviceStatusChanged` 不是一回事。** 那条只包带宽判定
    （`admission.BandwidthChange`），任务 11 明令不得把它当成完整在线判定
    （`plans/tasks/11. 终端与客户端同步.md` §「自动状态与详情」）。这里的
    载荷取自 `controller.DeviceNetworkStatus`：有效唯一主控、型号是否被该
    主控支持、网络是否允许工作、带宽与能源是否满足，合起来的那**一个**结论。

    字段全部具名：图版本叫 `status_version`，**不叫 `version`**——同步区
    头注那条「两个版本必须各有其名」在这里同样成立，一条裸 `version` 会让
    「客户端把哪个版本当哪个用」在代码层面根本看不出来。

    只有两种文本：`state_text` 是「在线」或「离线」；`validity` 为
    `pending` 时结论尚未确认，界面照样显示离线。`ui_applicable` 为假的是
    控制器本身（blocks.md 第 5 条不套用这套状态栏）：服务端不会为它发布，
    客户端收到也应当忽略。
    """

    kind = MSG_DEVICE_STATUS
    __slots__ = (
        "device_id",
        "generation",
        "online",
        "state_text",
        "validity",
        "run_state",
        "run_state_text",
        "master_instance_id",
        "bandwidth_ok",
        "power_ok",
        "bandwidth_domain_id",
        "ui_applicable",
        "status_version",
        "reasons",
        "notes",
    )

    #: 线上字段的**唯一清单**，构造与解析共用，防止两边漂移。
    FIELDS = (
        "kind",
        "device_id",
        "generation",
        "online",
        "state_text",
        "validity",
        "run_state",
        "run_state_text",
        "master_instance_id",
        "bandwidth_ok",
        "power_ok",
        "bandwidth_domain_id",
        "ui_applicable",
        "status_version",
        "reasons",
        "notes",
    )

    def __init__(
        self,
        device_id,
        generation,
        online,
        state_text,
        validity,
        run_state,
        run_state_text,
        status_version,
        reasons=(),
        notes=(),
        master_instance_id=None,
        bandwidth_ok=None,
        power_ok=None,
        bandwidth_domain_id=None,
        ui_applicable=True,
    ):
        self.device_id = device_id
        self.generation = generation
        self.online = online
        self.state_text = state_text
        self.validity = validity
        self.run_state = run_state
        self.run_state_text = run_state_text
        self.master_instance_id = master_instance_id
        #: True / False / None（待确认）。**None 不是「可以」。**
        self.bandwidth_ok = bandwidth_ok
        self.power_ok = power_ok
        self.bandwidth_domain_id = bandwidth_domain_id
        self.ui_applicable = ui_applicable
        #: 计算该结论时的图版本。**不是**交易版本，也不是物品视图版本。
        self.status_version = status_version
        self.reasons = tuple(reasons)
        self.notes = tuple(notes)
        if settings.DEBUG:
            self._validate(self.to_dict())

    @classmethod
    def _validate(cls, data):
        # type: (dict) -> None
        """形状校验。**不重建领域对象**，只验「界面拿它做得了判断吗」。

        与 `CommandResultMessage._validate_result` 同一取向：形状不对就当
        协议错误拒收整条消息，而不是让界面拿到半条能用的数据。
        """
        _require_count(data["generation"], "device_status.generation")
        _require_count(data["status_version"], "device_status.status_version")
        require_text(data["device_id"], "device_status.device_id")
        require_text(data["state_text"], "device_status.state_text")
        if data["validity"] not in DEVICE_STATUS_VALIDITY:
            raise ValueError("未知的设备状态确认度 %r" % (data["validity"],))
        for name in ("online", "ui_applicable"):
            if not isinstance(data[name], bool):
                raise TypeError("device_status.%s 必须是布尔值" % (name,))
        for name in ("bandwidth_ok", "power_ok"):
            value = data[name]
            if value is not None and not isinstance(value, bool):
                raise TypeError("device_status.%s 必须是布尔值或 None" % (name,))
        for name in ("run_state", "run_state_text"):
            if not isinstance(data[name], _TEXT_TYPES):
                raise TypeError("device_status.%s 必须是文本" % (name,))
        for name in ("master_instance_id", "bandwidth_domain_id"):
            value = data[name]
            if value is not None:
                require_text(value, "device_status." + name)
        reasons = data["reasons"]
        if not isinstance(reasons, (list, tuple)):
            raise TypeError("device_status.reasons 必须是列表")
        for reason in reasons:
            if isinstance(reason, BlockReason):
                continue
            _require_fields(reason, ("code", "detail"), "device_status.reason")
            require_text(reason["code"], "device_status.reason.code")
        notes = data["notes"]
        if not isinstance(notes, (list, tuple)):
            raise TypeError("device_status.notes 必须是列表")
        for note in notes:
            if not isinstance(note, _TEXT_TYPES):
                raise TypeError("device_status.notes 的项必须是文本")

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {
            "kind": self.kind,
            "device_id": self.device_id,
            "generation": self.generation,
            "online": self.online,
            "state_text": self.state_text,
            "validity": self.validity,
            "run_state": self.run_state,
            "run_state_text": self.run_state_text,
            "master_instance_id": self.master_instance_id,
            "bandwidth_ok": self.bandwidth_ok,
            "power_ok": self.power_ok,
            "bandwidth_domain_id": self.bandwidth_domain_id,
            "ui_applicable": self.ui_applicable,
            "status_version": self.status_version,
            "reasons": [
                reason.to_dict() if isinstance(reason, BlockReason) else dict(reason)
                for reason in self.reasons
            ],
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> DeviceStatusMessage
        _require_fields(data, cls.FIELDS, "device_status")
        # 解析路径的检查**无条件执行**（同 `SyncEnvelope.from_dict`）：
        # 这是来自对面的数据，本端没开 DEBUG 不是关掉边界检查的理由。
        cls._validate(data)
        return cls(
            data["device_id"],
            data["generation"],
            data["online"],
            data["state_text"],
            data["validity"],
            data["run_state"],
            data["run_state_text"],
            data["status_version"],
            reasons=[
                reason.to_dict() if isinstance(reason, BlockReason) else dict(reason)
                for reason in data["reasons"]
            ],
            notes=list(data["notes"]),
            master_instance_id=data["master_instance_id"],
            bandwidth_ok=data["bandwidth_ok"],
            power_ok=data["power_ok"],
            bandwidth_domain_id=data["bandwidth_domain_id"],
            ui_applicable=data["ui_applicable"],
        )


#: `PortConfigMessage.config` 必须在线的字段。**是一份镜像，不是 import**：
#: `common/` 不得 import `server/`（`test_import_boundaries.py` 静态强制），
#: 而配置的形状由领域层的 `PortConfig.to_dict()` 持有。因此这里放一份镜像，
#: 并用一条跨层用例钉住两者逐字相等（`test_io.py` 的 `TestPortConfigView`）。
#: 镜像与真身漂移的表现是「配置被静默丢掉一项」，那正好是那条用例要拦的。
PORT_CONFIG_FIELDS = (
    "enabled",
    "direction",
    "filter_mode",
    "filter",
    "supply_slots",
    "limit",
    "cache_enabled",
)

#: 过滤模式的**线上词汇**。同样是镜像：真身是 `server/devices/io.py` 的
#: `FILTER_MODES`，跨层用例钉住两者相等。下发与回传的 `filter_mode` 都只
#: 允许这两个词——写错了要当场拒收，而不是让服务端在 `PortConfig` 的
#: 构造器里抛一个「未知过滤模式」。
PORT_FILTER_MODES = ("whitelist", "blacklist")


class PortConfigMessage(SyncMessage):
    """一台**接口设备**的权威配置（11-07）。

    **它是全量快照，不是增量。** 一次发这台设备的整份配置，外加界面显示与
    限制所需的 `filter_limit` / `filter_modes` / `blocker`，客户端收到即整份
    替换。没有版本号、不做增量：配置只在玩家动手时变，整份也就几个字段，
    为它建一套版本协调，收益远小于它引入的第二份真相源。

    **不含任何版本字段**，理由见 `PORT_CONFIG_FIELDS` 上方与
    `server/devices/io.py` 的 `config_view`：配置版本今天不存在，而另外两个
    版本词汇（交易版本、存档结构版本）各有其名，挪用它们会让「按哪个版本
    判断过期」变成猜。

    `config` 是 `PortConfig.to_dict()`，与 `TerminalCommand.config` 完全同形
    ——**同一条形状从服务端推到客户端、再原样发回来**，中间不经转换，
    因此不存在「推的形状与收的形状不一致」这种漂移。
    """

    kind = MSG_PORT_CONFIG
    __slots__ = (
        "device_id",
        "generation",
        "role",
        "config",
        "filter_limit",
        "filter_modes",
        "blocker",
        "buffer_summary",
    )

    #: 线上字段的**唯一清单**，构造与解析共用，防止两边漂移。
    FIELDS = (
        "kind",
        "device_id",
        "generation",
        "role",
        "config",
        "filter_limit",
        "filter_modes",
        "blocker",
        "buffer_summary",
    )

    def __init__(
        self,
        device_id,
        generation,
        role,
        config,
        filter_limit,
        filter_modes=(),
        blocker=None,
        buffer_summary=None,
    ):
        self.device_id = device_id
        self.generation = generation
        self.role = role
        self.config = dict(config)
        self.filter_limit = filter_limit
        self.filter_modes = tuple(filter_modes)
        self.blocker = dict(blocker) if blocker is not None else None
        self.buffer_summary = buffer_summary
        if settings.DEBUG:
            self._validate(self.to_dict())

    @classmethod
    def _validate(cls, data):
        # type: (dict) -> None
        """形状校验。**不重建领域对象**，只验「界面拿它做得了判断吗」。

        与 `DeviceStatusMessage._validate` 同一取向。`config` 按
        `PORT_CONFIG_FIELDS` **逐字段**验：形状不对就拒收整条，而不是让界面
        拿到半份配置——半份配置最坏的表现是「界面上少了 3 条过滤」，而玩家
        看不出那是丢包还是本来就没配。
        """
        require_text(data["device_id"], "port_config.device_id")
        _require_count(data["generation"], "port_config.generation")
        require_text(data["role"], "port_config.role")
        _require_count(data["filter_limit"], "port_config.filter_limit")
        config = data["config"]
        if not isinstance(config, dict):
            raise TypeError("port_config.config 必须是字典")
        _require_fields(config, PORT_CONFIG_FIELDS, "port_config.config")
        if config["filter_mode"] not in PORT_FILTER_MODES:
            raise ValueError(
                "未知的过滤模式 %r" % (config["filter_mode"],)
            )
        entries = config["filter"]
        if not isinstance(entries, (list, tuple)):
            raise TypeError("port_config.config.filter 必须是资源键的列表")
        for entry in entries:
            # 线上形状就是资源键的字典（与 `TerminalCommand.key` 同一份）。
            # 这里只验「是个字典」：`ResourceKey.from_dict` 那一层的严格
            # 校验在领域侧，协议层重复一遍只会多一处会漂移的副本。
            if not isinstance(entry, dict):
                raise TypeError(
                    "port_config.config.filter 的项必须是字典，得到 %r"
                    % (type(entry),)
                )
        modes = data["filter_modes"]
        if not isinstance(modes, (list, tuple)):
            raise TypeError("port_config.filter_modes 必须是列表")
        for mode in modes:
            if mode not in PORT_FILTER_MODES:
                raise ValueError("未知的过滤模式 %r" % (mode,))
        blocker = data["blocker"]
        if blocker is not None:
            _require_fields(blocker, ("code", "detail"), "port_config.blocker")
            require_text(blocker["code"], "port_config.blocker.code")
        summary = data["buffer_summary"]
        if summary is not None and not isinstance(summary, _TEXT_TYPES):
            raise TypeError("port_config.buffer_summary 必须是文本或 None")

    def to_dict(self, descriptions=None):
        # type: (object) -> dict
        return {
            "kind": self.kind,
            "device_id": self.device_id,
            "generation": self.generation,
            "role": self.role,
            "config": dict(self.config),
            "filter_limit": self.filter_limit,
            "filter_modes": list(self.filter_modes),
            "blocker": dict(self.blocker) if self.blocker is not None else None,
            "buffer_summary": self.buffer_summary,
        }

    @classmethod
    def from_dict(cls, data, descriptions=None):
        # type: (dict, object) -> PortConfigMessage
        _require_fields(data, cls.FIELDS, "port_config")
        # 解析路径的检查**无条件执行**（同 `SyncEnvelope.from_dict`）：
        # 这是来自对面的数据，本端没开 DEBUG 不是关掉边界检查的理由。
        cls._validate(data)
        return cls(
            data["device_id"],
            data["generation"],
            data["role"],
            data["config"],
            data["filter_limit"],
            filter_modes=list(data["filter_modes"]),
            blocker=data["blocker"],
            buffer_summary=data["buffer_summary"],
        )


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
        InventorySnapshot,
        SessionOpen,
        SessionClose,
        SnapshotRequest,
        Ack,
        KeepaliveMessage,
        SessionReady,
        SnapshotBegin,
        SnapshotChunk,
        SnapshotEnd,
        ItemDelta,
        ResyncRequired,
        SyncError,
        TerminalCommand,
        CommandResultMessage,
        DeviceStatusMessage,
        CraftStateMessage,
        TemplateStateMessage,
        OrderStateMessage,
        PreviewStateMessage,
        PreviewChunkMessage,
        PortConfigMessage,
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

