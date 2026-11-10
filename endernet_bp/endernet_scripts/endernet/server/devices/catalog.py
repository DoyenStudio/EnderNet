# coding=utf-8
"""方块种类到设备定义与端口规则的注册表。

对应任务 02-01。这里是**连接判定的唯一入口**：相邻不等于连通，
必须双方都在 `ConnectionRules` 的白名单里才建立图边。

分类→定义映射来自 `common/definitions.py`。本模块回答三件事，并把拒绝原因
一并给出（02-07 要求业务错误可定位，不能只显示「离线」）：

1. 两种设备能不能连（分类白名单，`can_connect`）；
2. 一格光缆的某一面能不能对外连（`face_verdict`，2026-11-09 网络改版）：
   被扳手切断、或挂着面板占住的那一面不连，两格之间要**双方这一面都开着**；
3. 控制器是否允许接入对方（功能范围，`controller_serves`）。

**光缆按面的数据：本阶段的离线替身与阶段 C 的接口约定。** 方块实体上有两份
数据（`.claude/13` §2）：`endernet:cuts`（6 位整数，被扳手切断的面）与
`endernet:parts`（面 → 面板记录）。拓扑只经本模块读它们，读的是光缆
`DeviceRecord.config` 里的两个键：

- `config[CABLE_CUTS_KEY]`：与 `endernet:cuts` 同一个 6 位掩码，第 i 位 =
  `identity.FACING_NAMES[i]`，用 `identity.face_mask` / `faces_of_mask` 换算；
- `config[CABLE_PARTS_KEY]`：`{面名: 面板设备标识}`，面板设备标识取
  `definitions.TERMINAL_PARTS`（`*_terminal_part`）。

阶段 C 在放置、区块加载、扳手切断、面板接入与拆下时，从方块实体读出这两份写进
光缆记录的 `config`，再对该格 `topology.mark_dirty`（切断对面也是光缆时是对称写的，
两格都要标脏）。换存放位置时只改 `CableFaces.from_record` 一处。
"""

from ...common.definitions import CATEGORY_CABLE, ConnectionRules
from ...common.identity import FACING_NAMES, faces_of_mask, is_part_ref
from ...common.protocol import Reason

#: 光缆记录 `config` 里被扳手切断的面（6 位掩码，同 `endernet:cuts`）。
CABLE_CUTS_KEY = "cuts"
#: 光缆记录 `config` 里挂着的面板（`{面名: 面板设备标识}`，同 `endernet:parts`）。
CABLE_PARTS_KEY = "parts"

#: 六面全切断的掩码：读不懂的切断数据按它处理（见 `CableFaces.from_record`）。
_ALL_FACES_MASK = (1 << len(FACING_NAMES)) - 1


class CableFaces(object):
    """一格光缆的按面状态：哪些面被切断、哪些面挂着面板。纯值对象。"""

    __slots__ = ("cut", "parts")

    def __init__(self, cut=(), parts=None):
        # type: (object, dict) -> None
        self.cut = frozenset(cut)
        #: 面名 -> 面板设备标识（未过滤：能不能挂由 `DeviceCatalog.parts_of` 判）。
        self.parts = dict(parts or {})

    @classmethod
    def from_record(cls, record):
        # type: (object) -> CableFaces
        """从光缆记录的 `config` 读出（离线替身，阶段 C 往这里填）。

        **读不懂的切断数据按「六面全切断」处理**，不按「没切断」：把坏数据读成
        「连着」，玩家切断的那一面会悄悄连回去；读成全切断，这格光缆只是
        孤立出来，状态栏照样说得清。面板表不是字典、或面名写错的条目一律忽略——
        方块上不可能有它们。这里不抛错：本函数在拓扑重建里被调用，抛出去会让
        重建每帧卡在同一处。
        """
        config = getattr(record, "config", None) or {}
        raw_mask = config.get(CABLE_CUTS_KEY, 0)
        try:
            cut = faces_of_mask(raw_mask if raw_mask is not None else 0)
        except (TypeError, ValueError):
            cut = faces_of_mask(_ALL_FACES_MASK)
        raw_parts = config.get(CABLE_PARTS_KEY)
        parts = {}
        if isinstance(raw_parts, dict):
            for face, part_id in raw_parts.items():
                if face in FACING_NAMES and part_id:
                    parts[face] = part_id
        return cls(cut, parts)

    def is_cut(self, face):
        # type: (str) -> bool
        return face in self.cut

    def part_at(self, face):
        # type: (str) -> object
        return self.parts.get(face)

    def __repr__(self):
        return "CableFaces(cut=%s, parts=%s)" % (sorted(self.cut), sorted(self.parts.items()))


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

    def whole_block_of(self, device_id):
        # type: (str) -> str
        """面板形态 → 它对应的整方块终端（13 §2）；其余设备原样返回。

        「面板的行为与对应整方块终端完全相同」（全局约束 9）的判据都按它：工作台、样板槽、
        终端库存表、控制器支持范围。
        """
        definition = self.definitions.get(device_id)
        if definition.is_part():
            return definition.whole_block_id
        return device_id

    # -- 实例解析（阶段 D，13 §7）--------------------------------------

    def part_at(self, record, face):
        # type: (object, str) -> object
        """这一格 `face` 面挂着的面板型号；没有（或这种方块不挂面板）返回 None。"""
        for part_face, part_id in self.parts_of(record):
            if part_face == face:
                return part_id
        return None

    def resolve(self, registry, ref):
        # type: (object, object) -> tuple
        """设备实例引用 → `(记录, 型号)`；对不上是 `(None, None)`。

        - 整方块设备：`registry.get_by_ref`（位置与代次都对上）与记录的型号；
        - 面板引用（`DeviceRef.slot` 不是 center）：宿主光缆的记录（按宿主的位置与代次）
          与那一面现在挂着的面板型号。宿主不在、代次不对、那一面已经没有面板，都是对不上。

        面板没有自己的设备记录（13 §7），会话、准入、设备状态要「这台设备是什么」时都经这里。
        """
        if ref is None:
            return None, None
        if not is_part_ref(ref):
            record = registry.get_by_ref(ref)
            return record, (record.device_id if record is not None else None)
        record = registry.get_by_ref(ref.host())
        if record is None:
            return None, None
        part_id = self.part_at(record, ref.slot)
        if part_id is None:
            return None, None
        return record, part_id

    # -- 连接判定 ------------------------------------------------------

    def can_connect(self, device_id_a, device_id_b):
        # type: (str, str) -> ConnectionVerdict
        """两种设备之间是否允许建立**物理连接边**：只看分类白名单。

        主控只接基带、设备与光缆互连、处理器只经连接器入网……都写在
        `ConnectionRules`（13 §3）里。**不**看控制器的功能范围——那是
        `controller_serves` 的事；也**不**看某一面的状态——那是 `face_verdict`。

        2026-11-09 之前这里还有第二层「异容量基带不直接相连」（R02 曾验收）。
        作者当天改定允许混搭（决策台账 R14）：8/16/32 位基带相邻即相连，
        组成同一个核心，每块的外露面各出本档带宽，这一层随之删除。
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
        return ConnectionVerdict(True)

    def face_verdict(self, record, face):
        # type: (object, str) -> ConnectionVerdict
        """这一格的这一面能不能对外连（按面的判定接口）。

        只有光缆有按面的限制：被扳手切断的面、挂着面板的面都不连
        （13 §3「双方这一面都没被切断，且没被面板占住」）。其余设备六面皆可。
        数据来源见模块说明。
        """
        definition = self.definitions.get(record.device_id)
        if definition.category != CATEGORY_CABLE:
            return ConnectionVerdict(True)
        faces = CableFaces.from_record(record)
        if faces.is_cut(face):
            return ConnectionVerdict(
                False,
                Reason.CABLE_FACE_CUT,
                "%s 的 %s 面被扳手切断" % (record.device_id, face),
            )
        part_id = faces.part_at(face)
        if part_id is not None and self._holds_part(definition, part_id):
            return ConnectionVerdict(
                False,
                Reason.CABLE_FACE_OCCUPIED,
                "%s 的 %s 面挂着 %s" % (record.device_id, face, part_id),
            )
        return ConnectionVerdict(True)

    def link_verdict(self, record_a, face_a, record_b, face_b):
        # type: (object, str, object, str) -> ConnectionVerdict
        """两格相邻设备能不能经这一对面相连：分类白名单 + 双方这一面都开着。

        `face_a` 是从 A 指向 B 的面，`face_b` 是从 B 指向 A 的面。端口种类与
        方向另由拓扑的 `PortRuleSet` 判。
        """
        verdict = self.can_connect(record_a.device_id, record_b.device_id)
        if not verdict.allowed:
            return verdict
        for record, face in ((record_a, face_a), (record_b, face_b)):
            verdict = self.face_verdict(record, face)
            if not verdict.allowed:
                return verdict
        return ConnectionVerdict(True)

    def parts_of(self, record):
        # type: (object) -> list
        """这一格挂着的面板：`[(面名, 面板设备标识), ...]`，按 `FACING_NAMES` 顺序。

        宿主是能挂面板的光缆或面板底座（2026-11-10）。不能挂面板的方块（致密光缆等）上的、
        以及未登记为面板的标识一律不算：方块上不可能有它们，读到了是写入方的错，不能让它
        占住一面或进入线路。面板底座上的面板不进线路——拓扑只从光缆节点挂面板。
        """
        definition = self.definitions.get(record.device_id)
        if not definition.accepts_parts:
            return []
        faces = CableFaces.from_record(record)
        out = []
        for face in FACING_NAMES:
            part_id = faces.part_at(face)
            if part_id is not None and self._holds_part(definition, part_id):
                out.append((face, part_id))
        return out

    def _holds_part(self, definition, part_id):
        # type: (object, str) -> bool
        """这种光缆上能不能有这块面板：光缆可挂面板，且标识是登记过的面板。"""
        return bool(definition.accepts_parts) and self.definitions.is_part(part_id)

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
        # 面板与对应整方块终端同一口径（全局约束 9）：DOS 控制器认通用 / 合成终端，
        # 也就认它们的面板形态。
        if definition.allows(device_id) or definition.allows(self.whole_block_of(device_id)):
            return ConnectionVerdict(True)
        return ConnectionVerdict(
            False,
            "controller_does_not_accept_device",
            "%s 不支持控制 %s" % (controller_device_id, device_id),
        )

    def connected_neighbors(self, registry, node_key):
        # type: (object, object) -> list
        """列出 `node_key` 处设备**按连接规则**真正相邻的设备记录。

        分类白名单与光缆按面的状态都要过；处理器与基带相邻不会产生边。
        端口种类与方向不在这里判（那是拓扑的 `PortRuleSet`）。
        """
        record = registry.get(node_key)
        if record is None:
            return []

        out = []
        for index, neighbor_key in enumerate(node_key.neighbors()):
            neighbor = registry.get(neighbor_key)
            if neighbor is None:
                continue
            # 六个面两两相对：0<->1、2<->3、4<->5。
            verdict = self.link_verdict(
                record, FACING_NAMES[index], neighbor, FACING_NAMES[index ^ 1]
            )
            if verdict.allowed:
                out.append((neighbor_key, neighbor))
        return out

    def rejection_reason(self, device_id_a, device_id_b):
        # type: (str, str) -> str
        return self.can_connect(device_id_a, device_id_b).reason
