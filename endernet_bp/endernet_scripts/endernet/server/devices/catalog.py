# coding=utf-8
"""方块种类到设备定义与端口规则的注册表。

对应任务 02-01。这里是**连接判定的唯一入口**：相邻不等于连通，
必须双方都在 `ConnectionRules` 的白名单里才建立图边。

分类→定义映射来自 `common/definitions.py`，本模块只负责
「两个具体设备能不能连」以及「控制器是否允许接入对方」这两层判断，
并把拒绝原因一并给出（02-07 要求业务错误可定位，不能只显示「离线」）。
"""

from ...common.definitions import (
    CATEGORY_BASEBAND,
    QUARTERS_PER_UNIT,
    ConnectionRules,
    UNCONFIGURED,
)
from ...common.protocol import Reason


def _format_quarters(quarters):
    # type: (int) -> str
    """把四分之一 u 的整数还原成可读容量：整单位写 `8u`，否则写 `26/4u`。

    只用于诊断文案。容量一律以四分之一 u 的整数记账，显示时不做四舍五入。
    """
    if quarters % QUARTERS_PER_UNIT == 0:
        return "%du" % (quarters // QUARTERS_PER_UNIT,)
    return "%d/4u" % (quarters,)


class ConnectionVerdict(object):
    """一次连接判定的结果。"""

    __slots__ = ("allowed", "reason", "detail")

    def __init__(self, allowed, reason="", detail=""):
        # type: (bool, str, str) -> None
        self.allowed = allowed
        self.reason = reason
        self.detail = detail

    def __bool__(self):
        return self.allowed

    __nonzero__ = __bool__

    def to_dict(self):
        # type: () -> dict
        return {"allowed": self.allowed, "reason": self.reason, "detail": self.detail}

    def __repr__(self):
        return "ConnectionVerdict(%s, %s)" % (self.allowed, self.reason or "ok")


class DeviceCatalog(object):
    """设备定义索引与连接规则判定。"""

    def __init__(self, definitions):
        # type: (object) -> None
        self.definitions = definitions

    # -- 定义查询 ------------------------------------------------------

    def definition_of(self, device_id):
        # type: (str) -> object
        return self.definitions.get(device_id)

    def category_of(self, device_id):
        # type: (str) -> str
        return self.definitions.category_of(device_id)

    def is_controller(self, device_id):
        # type: (str) -> bool
        return self.definitions.is_controller(device_id)

    def capabilities_of(self, device_id):
        # type: (str) -> tuple
        return self.definitions.get(device_id).capabilities

    # -- 连接判定 ------------------------------------------------------

    def can_connect(self, device_id_a, device_id_b):
        # type: (str, str) -> ConnectionVerdict
        """两个设备标识之间是否允许建立**物理连接边**。

        blocks.md 里控制器「可以直接与基带进行连接」，其余设备也都挂在基带上；
        控制器并不直接贴着驱动器或终端。因此这里的判定只看分类白名单，
        **不**看控制器的功能范围——那是 `controller_serves` 的事。

        把两者混在一起会让「DOS 控制器旁边能不能放总线」这类问题
        得到一个错误答案：它们物理上本来就不相邻。

        判定分两层，顺序固定：

        1. **分类白名单**（`ConnectionRules`）：相邻不等于连通。
        2. **基带容量一致**：8/16/32 位基带只与同容量基带直接连通，
           异容量即使相邻也不建边。这一条需要设备标识才判得了，因此落在
           这里而不是分类表里（见 `_baseband_capacity_mismatch`）。
        """
        if not self.definitions.has(device_id_a):
            return ConnectionVerdict(False, "unknown_device", device_id_a)
        if not self.definitions.has(device_id_b):
            return ConnectionVerdict(False, "unknown_device", device_id_b)

        category_a = self.category_of(device_id_a)
        category_b = self.category_of(device_id_b)

        if not ConnectionRules.accepts(category_a, category_b):
            return ConnectionVerdict(
                False,
                ConnectionRules.reason_for_reject(category_a, category_b),
                "%s(%s) <-> %s(%s)" % (device_id_a, category_a, device_id_b, category_b),
            )

        if category_a == CATEGORY_BASEBAND and category_b == CATEGORY_BASEBAND:
            mismatch = self._baseband_capacity_mismatch(device_id_a, device_id_b)
            if mismatch is not None:
                return mismatch

        return ConnectionVerdict(True)

    def _baseband_capacity_mismatch(self, device_id_a, device_id_b):
        # type: (str, str) -> object
        """两段网络基带容量不同时给出拒绝理由，容量相同或无法比较时返回 None。

        blocks.md 规定 8/16/32 位基带**只与同容量基带直接连通**：不同容量
        即使相邻也不建立连接、不因此合并网络或负载域。这是一条**连接规则**，
        因此在建边这一层就拦掉，而不是等合网之后再想办法拆——后者会让
        「已经合并过一次」的中间状态被下游看见。

        注意与「同一控制器分别接两条不同容量的独立基带分支」区分：那条是
        允许的，两段基带彼此不相邻，本来就不会走到这里；即使经控制器桥接，
        `_build_domains` 只沿基带邻接扩展，两个分支仍是各自独立的负载域。

        **容量未知时不拦。** 未知不等于 0，也不等于「与对方不同」；把缺项
        判成「异容量」等于用一个没定过的数值定了案。这类缺项由
        `unconfigured_report()` 负责报出来，而准入层遇见未知容量会把整个域
        报成待确认（`bandwidth_ok=None`，主控不放行），因此放行不会造成
        「悄悄按一个错的容量合并」。
        """
        capacity_a = self._baseband_capacity(device_id_a)
        capacity_b = self._baseband_capacity(device_id_b)
        if capacity_a is None or capacity_b is None or capacity_a == capacity_b:
            return None
        return ConnectionVerdict(
            False,
            Reason.BASEBAND_CAPACITY_MISMATCH,
            "%s(%s) <-> %s(%s)：不同容量的网络基带不直接连通"
            % (
                device_id_a,
                _format_quarters(capacity_a),
                device_id_b,
                _format_quarters(capacity_b),
            ),
        )

    def _baseband_capacity(self, device_id):
        # type: (str) -> object
        """基带承载容量（四分之一 u）。非基带或未配置时返回 None。

        `capacity_quarters` 为 None 表示「该设备不承载负载」（定义表给
        非基带设备的写法），`UNCONFIGURED` 表示基带漏配了容量——两者都
        无法与对方比较，因此统一收敛成 None。
        """
        definition = self.definitions.get(device_id)
        capacity = getattr(getattr(definition, "bandwidth", None), "capacity_quarters", None)
        if capacity is None or capacity is UNCONFIGURED:
            return None
        return int(capacity)

    def controller_serves(self, controller_device_id, device_id):
        # type: (str, str) -> ConnectionVerdict
        """该型号控制器是否被允许控制这台设备。

        这是**功能范围**判定，与物理连接无关。DOS 控制器
        「仅能控制存储控制器、通用终端模版和合成终端面板」，
        因此它所在的网络上出现总线时，设备本身连得上，
        但这个控制器不认它——两件事要分开报。
        """
        if not self.definitions.has(controller_device_id):
            return ConnectionVerdict(False, "unknown_device", controller_device_id)
        if not self.definitions.has(device_id):
            return ConnectionVerdict(False, "unknown_device", device_id)

        definition = self.definitions.get(controller_device_id)
        if definition.category != "controller":
            return ConnectionVerdict(
                False,
                "not_a_controller",
                "%s 不是控制器" % (controller_device_id,),
            )
        if definition.allows(device_id):
            return ConnectionVerdict(True)
        return ConnectionVerdict(
            False,
            "controller_does_not_accept_device",
            "%s 不支持控制 %s" % (controller_device_id, device_id),
        )

    def connected_neighbors(self, registry, node_key):
        # type: (object, object) -> list
        """列出 `node_key` 处设备**按连接规则**真正相邻的设备记录。

        基带之间也要走这条规则（基带连基带是允许的），
        但处理器与基带相邻不会产生边。
        """
        record = registry.get(node_key)
        if record is None:
            return []

        out = []
        for neighbor_key in node_key.neighbors():
            neighbor = registry.get(neighbor_key)
            if neighbor is None:
                continue
            verdict = self.can_connect(record.device_id, neighbor.device_id)
            if verdict.allowed:
                out.append((neighbor_key, neighbor))
        return out

    def rejection_reason(self, device_id_a, device_id_b):
        # type: (str, str) -> str
        return self.can_connect(device_id_a, device_id_b).reason
