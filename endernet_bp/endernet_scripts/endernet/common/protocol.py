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

from .resource_key import ResourceKey, _require_count
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
    """

    __slots__ = ("op_id", "requested", "moved", "status", "reason", "resource_key")

    def __init__(self, op_id, requested, moved, status, reason="", resource_key=None):
        # type: (str, int, int, str, str, object) -> None
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

    @property
    def remainder(self):
        # type: () -> int
        """未完成、仍归原主的部分。"""
        return self.requested - self.moved

    def is_complete(self):
        return self.status == TransferStatus.COMPLETE and self.moved == self.requested

    def to_dict(self):
        # type: () -> dict
        return {
            "op_id": self.op_id,
            "requested": self.requested,
            "moved": self.moved,
            "status": self.status,
            "reason": self.reason,
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
