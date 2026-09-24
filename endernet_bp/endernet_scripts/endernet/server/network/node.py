# coding=utf-8
"""网络节点、连接端口与端口规则。

对应任务 02-01：**把允许连接的类型、方向和端口做成显式规则**，
并校验双方的端口。相邻不等于连通——两个方块贴在一起只是「相邻」，
要成为图里的一条边，必须同时通过三道判定：

1. 分类白名单（`common/definitions.py` 的 `ConnectionRules`）；
2. 端口种类：双方必须各有一个可对接的端口（本模块 `PortRuleSet`）；
3. 方向：两个端口所在的面必须互相朝向（本模块 `PORT_FACE_POLICY`）。

**关于方向。** `plans/blocks.md` 只规定了「谁能连谁」，没有规定
「哪一面能连」。因此 `PORT_FACE_POLICY` 里所有条目的默认值是
**显式声明的「六面皆可」**，而不是「因为是相邻所以默认连通」——
差别在于前者是一张可以逐条收紧的数据表，任何收紧都是改这张表，
不需要动遍历逻辑；后者则永远不会拒绝任何一边。逐面限制一旦由规划确定，
就在 `PORT_FACE_POLICY` 里按 (分类, 端口种类) 改成具体的面集合即可。

`assert_consistent_with()` 是这张表的守卫：任何新增的分类连接对
如果没有配套的端口种类，构建期就会报错，避免出现「规则说能连、
端口表却无从下手」的半张表。
"""

from ...common.definitions import (
    CATEGORY_ASSEMBLY,
    CATEGORY_BASEBAND,
    CATEGORY_BUS,
    CATEGORY_CONTROLLER,
    CATEGORY_ENERGY_INPUT,
    CATEGORY_MEMORY,
    CATEGORY_PROCESSOR,
    CATEGORY_PROCESSOR_CONNECTOR,
    CATEGORY_STORAGE,
    CATEGORY_TERMINAL,
)
from ...common.identity import FACING_NAMES


# -- 端口种类 --------------------------------------------------------------


class PortKind(object):
    """端口种类。种类决定了「能不能对上」，分类决定「允不允许对上」。"""

    #: 网络侧端口：基带、控制器，以及一切直接挂在基带上的设备。
    NETWORK = "network"
    #: 连接器的网络侧端口。与 `NETWORK` 成对，
    #: 但它只出现在并行处理器连接器上，因此「连接器—总线」这种
    #: 端口上说得通、分类上不允许的组合仍会被分类规则挡下。
    DEVICE_LINK = "device_link"
    #: 处理器群内部端口：处理器与处理器、处理器与连接器之间。
    PROCESSOR = "processor"


#: 六个面的朝向名（索引与 `FACING_OFFSETS` 一致）。
ALL_FACES = frozenset(FACING_NAMES)

#: 相邻位置在两侧各自对应的面。位置偏移的顺序是
#: up, down, north, south, west, east，因此 0<->1、2<->3、4<->5 互为对面。
_OPPOSITE_INDEX = {0: 1, 1: 0, 2: 3, 3: 2, 4: 5, 5: 4}


def opposite_face(face):
    # type: (str) -> str
    """与 `face` 相对的那一面。

    A 在 B 的北侧时，从 A 看是「北面」，从 B 看是「南面」。
    两侧都要各自同意，才能算这一对端口真的对上了。
    """
    return FACING_NAMES[_OPPOSITE_INDEX[FACING_NAMES.index(face)]]


class Port(object):
    """一个节点上的一个连接端口。"""

    __slots__ = ("kind", "faces")

    def __init__(self, kind, faces):
        # type: (str, object) -> None
        self.kind = kind
        self.faces = frozenset(faces)

    def accepts_face(self, face):
        # type: (str) -> bool
        return face in self.faces

    def to_dict(self):
        # type: () -> dict
        return {"kind": self.kind, "faces": sorted(self.faces)}

    def __repr__(self):
        return "Port(%s, %s)" % (self.kind, ",".join(sorted(self.faces)))


class PortVerdict(object):
    """端口层面的判定结果。理由要能定位，不能只说「不连通」（02-07）。"""

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
        return "PortVerdict(%s, %s)" % (self.allowed, self.reason or "ok")


class PortRuleSet(object):
    """显式的「分类 -> 端口种类」表与「端口种类 -> 可否对接」表。

    `face_policy` 可以按 (分类, 端口种类) 覆盖允许的面；默认六面皆可。
    这个覆盖点是**唯一**的方向来源：遍历逻辑不自己判断方向，
    只问这里。
    """

    #: 各分类暴露的端口种类。逐条对应 blocks.md 的「可以连接：」小节。
    CATEGORY_PORTS = {
        CATEGORY_BASEBAND: frozenset([PortKind.NETWORK]),
        CATEGORY_CONTROLLER: frozenset([PortKind.NETWORK]),
        CATEGORY_BUS: frozenset([PortKind.NETWORK]),
        CATEGORY_TERMINAL: frozenset([PortKind.NETWORK]),
        CATEGORY_STORAGE: frozenset([PortKind.NETWORK]),
        CATEGORY_MEMORY: frozenset([PortKind.NETWORK]),
        CATEGORY_ASSEMBLY: frozenset([PortKind.NETWORK]),
        CATEGORY_ENERGY_INPUT: frozenset([PortKind.NETWORK]),
        CATEGORY_PROCESSOR_CONNECTOR: frozenset(
            [PortKind.DEVICE_LINK, PortKind.PROCESSOR]
        ),
        CATEGORY_PROCESSOR: frozenset([PortKind.PROCESSOR]),
    }

    #: 可以对接的端口种类对。对称。
    KIND_PAIRS = frozenset(
        [
            (PortKind.NETWORK, PortKind.NETWORK),
            (PortKind.NETWORK, PortKind.DEVICE_LINK),
            (PortKind.PROCESSOR, PortKind.PROCESSOR),
        ]
    )

    #: `(分类, 端口种类) -> frozenset(面名)`。空表示「六面皆可」，
    #: 即规划未作限制时**显式声明**的不限制，而不是跳过校验。
    PORT_FACE_POLICY = {}

    def __init__(self, face_policy=None):
        # type: (dict) -> None
        self.face_policy = dict(self.PORT_FACE_POLICY)
        if face_policy:
            self.face_policy.update(face_policy)
        #: 分类 -> 端口元组。端口是纯值对象，按分类缓存后建图时无需反复构造。
        self._ports_cache = {}

    # -- 表查询 --------------------------------------------------------

    @classmethod
    def kinds_of(cls, category):
        # type: (str) -> frozenset
        """该分类暴露的端口种类。未登记的分类返回空集。"""
        return cls.CATEGORY_PORTS.get(category, frozenset())

    @classmethod
    def accepts_kinds(cls, kind_a, kind_b):
        # type: (str, str) -> bool
        return (kind_a, kind_b) in cls.KIND_PAIRS or (kind_b, kind_a) in cls.KIND_PAIRS

    def faces_of(self, category, kind):
        # type: (str, str) -> frozenset
        """该分类该种端口允许出现的面。"""
        declared = self.face_policy.get((category, kind))
        if declared is None:
            return ALL_FACES
        return frozenset(declared)

    def ports_of(self, category):
        # type: (str) -> tuple
        cached = self._ports_cache.get(category)
        if cached is None:
            cached = tuple(
                Port(kind, self.faces_of(category, kind))
                for kind in sorted(self.kinds_of(category))
            )
            self._ports_cache[category] = cached
        return cached

    # -- 判定 ----------------------------------------------------------

    def link_verdict(self, category_a, category_b, face_a=None, face_b=None):
        # type: (str, str, str, str) -> PortVerdict
        """双方端口能否对上。

        `face_a` 是从 A 指向 B 的那一面，`face_b` 是从 B 指向 A 的那一面
        （调用方已按 `opposite_face` 折算）。任一为 None 表示不检查该侧的面。
        """
        kinds_a = self.kinds_of(category_a)
        kinds_b = self.kinds_of(category_b)
        if not kinds_a or not kinds_b:
            missing = category_a if not kinds_a else category_b
            return PortVerdict(
                False, "no_ports_declared", "分类 %s 未登记任何端口" % (missing,)
            )

        pairs = [
            (kind_a, kind_b)
            for kind_a in sorted(kinds_a)
            for kind_b in sorted(kinds_b)
            if self.accepts_kinds(kind_a, kind_b)
        ]
        if not pairs:
            return PortVerdict(
                False,
                "no_shared_port_kind",
                "%s%s 与 %s%s 没有可对接的端口种类"
                % (category_a, sorted(kinds_a), category_b, sorted(kinds_b)),
            )

        for kind_a, kind_b in pairs:
            faces_a = self.faces_of(category_a, kind_a)
            faces_b = self.faces_of(category_b, kind_b)
            if face_a is not None and face_a not in faces_a:
                continue
            if face_b is not None and face_b not in faces_b:
                continue
            return PortVerdict(
                True,
                "",
                "%s@%s <-> %s@%s" % (kind_a, face_a or "*", kind_b, face_b or "*"),
            )

        return PortVerdict(
            False,
            "faces_do_not_meet",
            "%s 的 %s 面 与 %s 的 %s 面 不允许对接"
            % (category_a, face_a, category_b, face_b),
        )

    # -- 守卫 ----------------------------------------------------------

    @classmethod
    def assert_consistent_with(cls, connection_rules):
        # type: (object) -> None
        """分类白名单里的每一对，都必须有配套的端口种类。

        这条守卫防的是「半张表」：有人给 `ConnectionRules` 加了一对
        可连接分类，却忘了登记端口，于是规则说能连、建图时却永远连不上，
        而且失败得毫无提示。反向不检查——连接器与总线在**端口上**
        可以对接、在**分类上**不允许，这是有意的两级判定。
        """
        problems = []
        for category_a, category_b in sorted(connection_rules.ALLOWED_PAIRS):
            kinds_a = cls.kinds_of(category_a)
            kinds_b = cls.kinds_of(category_b)
            if not kinds_a or not kinds_b:
                problems.append(
                    "%s <-> %s：分类 %s 未登记端口"
                    % (category_a, category_b, category_a if not kinds_a else category_b)
                )
                continue
            if not any(
                cls.accepts_kinds(kind_a, kind_b)
                for kind_a in kinds_a
                for kind_b in kinds_b
            ):
                problems.append(
                    "%s <-> %s：端口种类无法对接（%s / %s）"
                    % (category_a, category_b, sorted(kinds_a), sorted(kinds_b))
                )
        if problems:
            raise AssertionError(
                "连接规则与端口规则表不一致，这些组合永远建不出边：\n  "
                + "\n  ".join(problems)
            )


#: 默认端口规则表（六面皆可）。
DEFAULT_PORTS = PortRuleSet()


# -- 节点 ------------------------------------------------------------------


class NetworkNode(object):
    """一个已注册设备在网络图里的表示。

    这是**只读快照**：登记变化由 01 的注册表负责，本对象只把
    「这个位置是什么设备、它有哪些端口、挂了哪些能力」摊开给图服务。
    """

    __slots__ = (
        "node_key",
        "ref",
        "device_id",
        "category",
        "capabilities",
        "loaded",
        "facing",
        "ports",
    )

    def __init__(self, node_key, ref, device_id, category, capabilities=(), loaded=True, facing=None, ports=()):
        # type: (object, object, str, str, tuple, bool, object, tuple) -> None
        self.node_key = node_key
        self.ref = ref
        self.device_id = device_id
        self.category = category
        self.capabilities = tuple(capabilities)
        self.loaded = bool(loaded)
        self.facing = facing
        self.ports = tuple(ports)

    @classmethod
    def from_record(cls, definitions, record, rule_set=None):
        # type: (object, object, PortRuleSet) -> NetworkNode
        rules = rule_set if rule_set is not None else DEFAULT_PORTS
        definition = definitions.get(record.device_id)
        return cls(
            record.node_key,
            record.ref,
            record.device_id,
            definition.category,
            capabilities=definition.capabilities,
            loaded=record.loaded,
            facing=record.facing,
            ports=rules.ports_of(definition.category),
        )

    # -- 查询 ----------------------------------------------------------

    @property
    def generation(self):
        return self.ref.generation

    def port_kinds(self):
        # type: () -> frozenset
        return frozenset(port.kind for port in self.ports)

    def port_of_kind(self, kind):
        # type: (str) -> Port
        for port in self.ports:
            if port.kind == kind:
                return port
        return None

    def has_capability(self, name):
        # type: (str) -> bool
        return name in self.capabilities

    def is_baseband(self):
        return self.category == CATEGORY_BASEBAND

    def is_controller(self):
        return self.category == CATEGORY_CONTROLLER

    def is_processor(self):
        return self.category == CATEGORY_PROCESSOR

    def is_connector(self):
        return self.category == CATEGORY_PROCESSOR_CONNECTOR

    def to_dict(self):
        # type: () -> dict
        return {
            "node_key": self.node_key.to_dict(),
            "device_id": self.device_id,
            "category": self.category,
            "generation": self.ref.generation,
            "loaded": self.loaded,
            "ports": [port.to_dict() for port in self.ports],
        }

    def __repr__(self):
        return "NetworkNode(%s @ %r)" % (self.device_id, self.node_key)
