# coding=utf-8
"""设备、介质、连接与数值配置表。

对应任务 01-03。逐项对应 `plans/blocks.md` 与 `plans/items.md`。

两条纪律（来自总览的全局约束 7）：

1. **未定的玩法不得被临时实现悄悄定案。** 凡是 `plans/` 尚未确定的数值，
   一律用 `Value(..., unresolved='R0x', is_test_value=True)` 标注；
   `DefinitionSet.assert_no_silent_unresolved()` 会在构建时拒绝
   「带着未定标签、却不是测试值」的条目——也就是拒绝一个悄悄定案的数值。
2. **`plans/` 没写的数值留 `UNCONFIGURED`，不得当成 0。** 能耗为 0 与能耗未知
   是两回事：前者是免费设备，后者是定义缺项（R10）。上层看到 `UNCONFIGURED`
   必须报「未配置」，而不是静默按 0 计费。

规则编号含义见 `plans/tasks/00. 开发流程与任务总览.md` 的决策台账。

**2026-11-09 网络改版**（`.claude/13` §0 的作者定案）：基带收成 AE2 控制器式的
紧凑核心，每个外露面输出本档带宽；延长与分发交给三种末影光缆；设备透传 8u；
终端多了挂在光缆上的面板形态；另有网络扳手。表的分工：

- `DEVICES`：整方块设备（`scripts/gen_blocks.py` 按它逐台生成方块，含义不变）；
- `CABLES`：三种光缆。也是方块，但状态、骨骼可见性与碰撞盒按面生成，
  归阶段 C 的生成器，因此单列一张表，不混进 `DEVICES`；
- `TERMINAL_PARTS`：四种终端的面板形态，挂在光缆的一个面上。它的物品形态是同名的展示方块
  （2026-11-10 方案 B，`scripts/gen_blocks.py` 的 `build_part_item_json`：物品栏显示 3D 面板），
  玩家放不下它（`server/interaction/guard.py` 的 `panel_item` 规则拦掉放置），挂面板走客户端
  接管的意图；
- `TUNNEL_PARTS`：P2P 网络隧道（2026-11-13），只有面板形态、没有对应的整方块；物品形态与终端
  面板一样是同名的展示方块（屏幕取默认面板那一块），手持点光缆挂上去，走同一条挂面板的意图。
  与 `TERMINAL_PARTS` 合起来是 `PARTS`；
- `PART_HOLDERS`：面板底座（2026-11-10 真机反馈后照 AE2 补上）。面板对着普通方块或
  空气放时由它承载：只装面板、没有线芯、不入网，最后一块面板拆掉就消失；按面数据与
  方块状态的维护同光缆一套，因此也不进 `DEVICES`；
- `TOOLS`：工具物品（网络扳手、配置卡、无线连接工具）。

`DefinitionSet.get()` / `has()` 跨这四张设备表查；`devices` 仍只是整方块那一张。
"""

import math


# -- 未配置哨兵 ------------------------------------------------------------


class _Unconfigured(object):
    """`plans/` 中缺失的数值。必须与整数 0 区分开。"""

    __slots__ = ()

    def __repr__(self):
        return "UNCONFIGURED"

    def __bool__(self):
        raise TypeError("UNCONFIGURED 不能当布尔值使用，请显式判断")

    __nonzero__ = __bool__


UNCONFIGURED = _Unconfigured()


class Value(object):
    """一个可能尚未定案的数值。

    - `unresolved is None`：已是确定值。
    - `unresolved = 'R08'` 且 `is_test_value=True`：该规则未定，
      这里只是**标注过的**开发替身，不得作为生产数值交付。
    - `unresolved = 'R08'` 且 `is_test_value=False`：静默定案，构建时会被拒绝。
    """

    __slots__ = ("value", "unresolved", "is_test_value", "note")

    def __init__(self, value, unresolved=None, is_test_value=False, note=""):
        # type: (object, str, bool, str) -> None
        self.value = value
        self.unresolved = unresolved
        self.is_test_value = is_test_value
        self.note = note

    def is_configured(self):
        return self.value is not UNCONFIGURED

    def is_settled(self):
        """该数值是否可以作为已确定的玩法交付。"""
        return self.unresolved is None and self.value is not UNCONFIGURED

    def __repr__(self):
        return "Value(%r%s%s)" % (
            self.value,
            ", unresolved=%r" % self.unresolved if self.unresolved else "",
            ", test" if self.is_test_value else "",
        )


# 带宽一律以四分之一 u 的整数记账、不取整（2026-11-09 作者定案，13 §0.2）。
# 此前的 `BandwidthRounding`（R02「负载取整」的替身）随负载域一起退役。


# -- 设备分类 --------------------------------------------------------------

CATEGORY_CONTROLLER = "controller"
CATEGORY_BASEBAND = "baseband"
#: 末影光缆（13 §2）：载体，按本档容量承载线路，自身不占带宽。
CATEGORY_CABLE = "cable"
#: 面板底座（2026-11-10）：只承载面板，不与任何设备相连（连接表里没有它），不入网。
CATEGORY_PART_HOLDER = "part_holder"
CATEGORY_BUS = "bus"
CATEGORY_TERMINAL = "terminal"
CATEGORY_STORAGE = "storage"
CATEGORY_PROCESSOR_CONNECTOR = "processor_connector"
CATEGORY_PROCESSOR = "processor"
CATEGORY_MEMORY = "memory"
CATEGORY_ASSEMBLY = "assembly"
ENERGY_INPUT_ID = "endernet:energy_input_interface"
FE_PER_EN = 2
# Native signed-int block entity field bounds the committed FE balance.
ENERGY_INPUT_CAPACITY_FE = 2147483647

CATEGORY_ENERGY_INPUT = "energy_input"
#: P2P 网络隧道（2026-11-13）：只有面板形态，挂在光缆的一个面上；同频率的几块隧道把各自
#: 正面那一格连成一张网（拓扑的远程连边）。面板不在连接表里，这个分类只给准入与诊断认。
CATEGORY_TUNNEL = "tunnel"
#: 无线连接器（2026-11-13）：整方块载体，六面像致密光缆一样连；用无线连接工具与另一台配对后，
#: 两台之间多一条远程连边（同维度、限距离）。
CATEGORY_WIRELESS = "wireless"

#: 13 §3 连接表里的「设备」：整方块的总线、终端、驱动器、内存槽、装配仓与能源类。
#: 它们彼此透传（13 §0.5），也都能接基带与光缆。处理器与处理器连接器各有
#: 自己的一行，**不在其中**。
DEVICE_CATEGORIES = frozenset(
    [
        CATEGORY_BUS,
        CATEGORY_TERMINAL,
        CATEGORY_STORAGE,
        CATEGORY_MEMORY,
        CATEGORY_ASSEMBLY,
        CATEGORY_ENERGY_INPUT,
    ]
)

#: 设备的形态（13 §2）：整方块，或挂在光缆一个面上的面板。
FORM_BLOCK = "block"
FORM_PART = "part"
FORMS = frozenset([FORM_BLOCK, FORM_PART])

#: 面板的种类（面板骨架，2026-11-10）：决定一块面板挂上以后走哪几条流程。
#: 终端（右键开终端屏、库存按整方块终端记账）与 P2P 隧道（配置卡设频率、正面经隧道连网）；
#: 往后的总线接口面板再加一种，各自的流程按种类分派，不默认「面板就是终端」。
PART_KIND_TERMINAL = "terminal"
PART_KIND_TUNNEL = "tunnel"
PART_KINDS = frozenset([PART_KIND_TERMINAL, PART_KIND_TUNNEL])

# -- 能力名（与 devices/capabilities.py 的协议一一对应）--------------------

CAP_STORAGE_PROVIDER = "storage_provider"
CAP_PATTERN_PROVIDER = "pattern_provider"
CAP_ITEM_PORT = "item_port"
CAP_FLUID_PORT = "fluid_port"
CAP_ENERGY_PORT = "energy_port"
CAP_TERMINAL_ENDPOINT = "terminal_endpoint"
CAP_PROCESSING_SLOTS = "processing_slots"
CAP_MEMORY = "memory"

# -- 样板模式（07）---------------------------------------------------------
#
# 词汇放在这里而不是 `server/crafting/`：`common` 不得反向 import `server`，
# 而样板物品表（下面）必须能校验自己的 mode 取值合法。`crafting/patterns.py`
# 从这里 import，因此「物品类型」与「配方模式」共用同一套字符串，
# 不可能分叉成两套写法。

#: 合成样板：指定工作台合成配方，放入**装配仓**使用（items.md）。
PATTERN_MODE_CRAFTING = "crafting"
#: 工厂样板：指定自定义配方，放入**总线样板接口**使用（items.md）。
PATTERN_MODE_FACTORY = "factory"
PATTERN_MODES = frozenset([PATTERN_MODE_CRAFTING, PATTERN_MODE_FACTORY])

#: 1 单位带宽记为 4 个「四分之一 u」。
QUARTERS_PER_UNIT = 4


def units_to_quarters(units):
    # type: (int) -> int
    """带宽单位 u 换算成四分之一 u。"""
    return int(units) * QUARTERS_PER_UNIT


# -- 连接规则 --------------------------------------------------------------


def _connection_pairs():
    # type: () -> frozenset
    """13 §3 的连接表，逐行展开成分类对（「设备」一词展开成 `DEVICE_CATEGORIES`）。

    | 一方 | 另一方 |
    | --- | --- |
    | 主控 | 基带（只接基带） |
    | 基带 | 基带（异档也连：R14-b，2026-11-09 作者改定允许混搭）、光缆、设备、处理器连接器 |
    | 光缆 | 光缆（三种互连）、设备、处理器连接器 |
    | 设备 | 设备（透传）、处理器连接器 |
    | 处理器连接器 | 处理器 |
    | 处理器 | 处理器 |
    | 无线连接器 | 基带、光缆、设备、处理器连接器、无线连接器（照致密光缆，不接主控） |

    面板不在表里：它不是方块，只经同格线芯挂在宿主光缆上，不对外连（拓扑负责）。
    光缆按面的限制（被切断、被面板占住）是第二层判定，见
    `server/devices/catalog.py` 的 `DeviceCatalog.face_verdict`。
    """
    pairs = set(
        [
            (CATEGORY_BASEBAND, CATEGORY_CONTROLLER),
            (CATEGORY_BASEBAND, CATEGORY_BASEBAND),
            (CATEGORY_BASEBAND, CATEGORY_CABLE),
            (CATEGORY_BASEBAND, CATEGORY_PROCESSOR_CONNECTOR),
            (CATEGORY_CABLE, CATEGORY_CABLE),
            (CATEGORY_CABLE, CATEGORY_PROCESSOR_CONNECTOR),
            (CATEGORY_PROCESSOR_CONNECTOR, CATEGORY_PROCESSOR),
            (CATEGORY_PROCESSOR, CATEGORY_PROCESSOR),
            (CATEGORY_BASEBAND, CATEGORY_WIRELESS),
            (CATEGORY_CABLE, CATEGORY_WIRELESS),
            (CATEGORY_WIRELESS, CATEGORY_PROCESSOR_CONNECTOR),
            (CATEGORY_WIRELESS, CATEGORY_WIRELESS),
        ]
    )
    devices = sorted(DEVICE_CATEGORIES)
    for index, device in enumerate(devices):
        pairs.add((CATEGORY_BASEBAND, device))
        pairs.add((CATEGORY_CABLE, device))
        pairs.add((CATEGORY_WIRELESS, device))
        pairs.add((device, CATEGORY_PROCESSOR_CONNECTOR))
        for other in devices[index:]:
            pairs.add((device, other))
    return frozenset(pairs)


class ConnectionRules(object):
    """显式的分类白名单（13 §3，2026-11-09 改版）。

    相邻**不等于**连通：只有双方类别都在白名单里才建立图边。
    这条规则直接决定拓扑服务的建图，不能靠「是不是贴在一起」推断。
    表外的一律不连，例如主控贴着光缆或设备、处理器贴着基带。
    """

    #: 允许互相连接的分类对。意图上是对称的，校验时双向都要成立。
    ALLOWED_PAIRS = _connection_pairs()

    @classmethod
    def accepts(cls, category_a, category_b):
        # type: (str, str) -> bool
        """两类设备之间是否允许建立连接边。"""
        return (
            (category_a, category_b) in cls.ALLOWED_PAIRS
            or (category_b, category_a) in cls.ALLOWED_PAIRS
        )

    @classmethod
    def partners_of(cls, category):
        # type: (str) -> frozenset
        """某分类允许连接的分类集合。"""
        out = set()
        for first, second in cls.ALLOWED_PAIRS:
            if first == category:
                out.add(second)
            elif second == category:
                out.add(first)
        return frozenset(out)

    @classmethod
    def reason_for_reject(cls, category_a, category_b):
        # type: (str, str) -> str
        """给出可定位的拒绝原因，而不是笼统的「不连通」。"""
        for mine, other in ((category_a, category_b), (category_b, category_a)):
            if mine == CATEGORY_PROCESSOR and other not in (
                CATEGORY_PROCESSOR,
                CATEGORY_PROCESSOR_CONNECTOR,
            ):
                return "processor_must_use_connector"
        for mine, other in ((category_a, category_b), (category_b, category_a)):
            if mine == CATEGORY_CONTROLLER and other != CATEGORY_BASEBAND:
                return "controller_only_accepts_baseband"
        return "categories_not_connectable"


# -- 数值规格 --------------------------------------------------------------


class EnergySpec(object):
    """设备能耗（EN/t）。

    - `idle` / `work`：空闲与工作态的固定能耗。
    - `per_unit`：每装一个外部单元的额外能耗，用于
      「存储驱动器 = 硬盘个数 EN/t」「内存槽 = 内存条个数 × 8 EN/t」。
      这类设备的固定部分确实是 0（没有硬盘就真的不耗能），
      不是缺项，因此基础值写 0 是对的。
    - `follows_bandwidth=True`：该设备的能耗等于它所在负载域的负载，
      见 blocks.md 的小型网络控制器「耗能：(基带负载)EN/t」。
    """

    __slots__ = ("idle", "work", "follows_bandwidth", "per_unit")

    def __init__(self, idle=UNCONFIGURED, work=None, follows_bandwidth=False, per_unit=None):
        # type: (object, object, bool, object) -> None
        self.idle = idle
        #: None 表示该设备没有独立的工作态，能耗恒为 idle。
        self.work = idle if work is None else work
        self.follows_bandwidth = follows_bandwidth
        self.per_unit = per_unit

    def value_for_state(self, state, unit_count=0):
        # type: (str, int) -> object
        """`state` 取 'idle' 或 'work'。返回 int 或 UNCONFIGURED。

        `unit_count` 是该设备的装置数量（硬盘个数 / 内存条个数）。
        """
        base = self.work if state == "work" else self.idle
        if isinstance(base, Value):
            base = base.value
        if base is UNCONFIGURED:
            return UNCONFIGURED
        total = base

        per_unit = self.per_unit
        if isinstance(per_unit, Value):
            per_unit = per_unit.value
        if per_unit is UNCONFIGURED:
            return UNCONFIGURED
        if per_unit is not None:
            total = total + per_unit * int(unit_count)
        return total

    def per_unit_value(self):
        # type: () -> object
        """每单元能耗。返回 int、Value 或 None。"""
        return self.per_unit


class BandwidthSpec(object):
    """设备带宽需求与（可选）承载容量，以**四分之一 u** 为整数记账。

    - `base_quarters`：固定需求。1u = 4。
    - `per_disk_quarters`：存储驱动器按硬盘个数计费，每块硬盘 1 个四分之一 u，
      即 blocks.md 的 `(硬盘个数/4)u`。
    - `per_memory_quarters`：内存槽按内存条个数计费（当前 blocks.md 给的是
      固定 2u，因此为 None）。
    - `capacity_quarters`：**载体的容量**，与设备自身的需求量是两回事，
      因此单独一个字段而不是复用 `base_quarters`。只有两类设备填它
      （2026-11-09 改版，13 §0）：
      - 网络基带：**每个外露面**输出的带宽，即本档 8/16/32u。一块基带的
        几个外露面各出一份，不相加成一个池；
      - 末影光缆：一格光缆能让多少带宽经过（玻璃 8u、普通 16u、致密 32u）。
      为 None 表示该设备不是载体。整方块设备的透传容量（R14-a，8u）是全体设备
      共用的一个数，记在 `DEVICE_PASSTHROUGH_QUARTERS`，不在这里逐台写。
    """

    __slots__ = (
        "base_quarters",
        "per_disk_quarters",
        "per_memory_quarters",
        "capacity_quarters",
    )

    def __init__(
        self,
        base_quarters,
        per_disk_quarters=None,
        per_memory_quarters=None,
        capacity_quarters=None,
    ):
        # type: (object, object, object, object) -> None
        self.base_quarters = base_quarters
        self.per_disk_quarters = per_disk_quarters
        self.per_memory_quarters = per_memory_quarters
        self.capacity_quarters = capacity_quarters

    def is_carrier(self):
        # type: () -> bool
        """该设备是否是载体（网络基带或末影光缆）。"""
        return self.capacity_quarters is not None

    def demand_quarters(self, disk_count=0, memory_count=0):
        # type: (int, int) -> object
        """当前装置配置下的带宽需求。返回 int 或 UNCONFIGURED。"""
        if self.base_quarters is UNCONFIGURED:
            return UNCONFIGURED
        total = self.base_quarters
        if self.per_disk_quarters:
            total += self.per_disk_quarters * int(disk_count)
        if self.per_memory_quarters:
            total += self.per_memory_quarters * int(memory_count)
        return total


class DeviceDefinition(object):
    """一种接入设备的静态定义。

    **`energy` 与 `supply` 是两件事，刻意不合成一个字段。** `energy` 是这台设备
    「花多少」（`EnergySpec`），`supply` 是它「给多少」。合成一个字段的话，
    「能耗未知」与「供能未知」就分不开了——而这两种未知要报给用户的是两句
    完全不同的话（「算不出这台设备要多少电」vs「算不出它能给多少电」）。

    `supply` 的三种取值：

    - `None`：该设备**不供能**。这是结构性事实，不是缺项——绝大多数设备都是这样。
    - `Value(n)`：已定案的供能速率（EN/t）。目前只有创造能量源。
    - `Value(UNCONFIGURED, unresolved=..., is_test_value=True)`：它供能，但速率未定。
      能源接入口就是这一种（2 FE = 1 EN，实际供能随余额变化）。
      **不能省略成 `None`**：那会被读成「它不供能」，于是只接了能源接入口的网络
      会报「确认没电」，而正确的答案是「供能速率未定」。

    供能设备由「`supply is not None`」这一个事实认定，**不再额外挂一个能力名**：
    能力 `energy_port` 对应可由线缆写入的 radium FE 端口，
    而创造能量源是**推**式静态电源，两者不是同一个能力；`supply` 已经足以分类，
    再存一份只会分叉。能源服务因此像 `admission` 读带宽那样直接读定义表。

    2026-11-09 网络改版加了三个字段（13 §2）：

    - `form`：`FORM_BLOCK` 整方块，或 `FORM_PART` 面板形态（挂在光缆一个面上）；
    - `whole_block_id`：面板形态对应的整方块设备。终端面板必填：带宽、耗能、界面都与它
      相同，界面由 `DefinitionSet.terminal_screen_of` 据此查。P2P 隧道（2026-11-13）只有面板
      形态，没有对应的整方块，为 None；整方块设备一律为 None；
    - `accepts_parts`：光缆能不能挂面板。致密光缆不能，玻璃与普通能；面板底座也能。

    2026-11-10 面板骨架加了一个：

    - `part_kind`：面板的种类（`PART_KINDS`）。面板形态必填，整方块设备必须为 None。
    """

    __slots__ = (
        "id",
        "label",
        "category",
        "bandwidth",
        "energy",
        "supply",
        "capabilities",
        "allowed_device_ids",
        "max_stack_slots",
        "disabled_reason",
        "notes",
        "form",
        "whole_block_id",
        "accepts_parts",
        "part_kind",
    )

    def __init__(
        self,
        device_id,
        label,
        category,
        bandwidth=None,
        energy=None,
        supply=None,
        capabilities=(),
        allowed_device_ids=None,
        max_stack_slots=None,
        disabled_reason=None,
        notes="",
        form=FORM_BLOCK,
        whole_block_id=None,
        accepts_parts=False,
        part_kind=None,
    ):
        if form not in FORMS:
            raise ValueError("未知的设备形态: %r" % (form,))
        if form != FORM_PART and whole_block_id is not None:
            raise ValueError("整方块设备不得指明对应的整方块设备: %s" % (device_id,))
        if (form == FORM_PART) != (part_kind is not None):
            raise ValueError("面板形态必须指明种类，整方块设备不得指明: %s" % (device_id,))
        if part_kind is not None and part_kind not in PART_KINDS:
            raise ValueError("未知的面板种类 %r: %s" % (part_kind, device_id))
        if part_kind == PART_KIND_TERMINAL and whole_block_id is None:
            raise ValueError("终端面板必须指明对应的整方块终端: %s" % (device_id,))
        if accepts_parts and category not in (CATEGORY_CABLE, CATEGORY_PART_HOLDER):
            raise ValueError("只有光缆与面板底座能挂面板: %s" % (device_id,))
        self.id = device_id
        self.label = label
        self.category = category
        self.bandwidth = bandwidth if bandwidth is not None else BandwidthSpec(UNCONFIGURED)
        self.energy = energy if energy is not None else EnergySpec()
        #: 见类 docstring。None = 不供能；Value = 供能速率（可能未定）。
        self.supply = supply
        self.capabilities = tuple(capabilities)
        #: 仅控制器使用：允许接入的设备 id 集合。None 表示 blocks.md 未作限定。
        self.allowed_device_ids = (
            None if allowed_device_ids is None else frozenset(allowed_device_ids)
        )
        self.max_stack_slots = max_stack_slots
        self.disabled_reason = disabled_reason
        self.notes = notes
        self.form = form
        #: 面板形态对应的整方块设备（带宽、耗能、界面与它相同）；整方块为 None。
        self.whole_block_id = whole_block_id
        #: 光缆能否挂面板（13 §0.8：致密不能，玻璃与普通能，一格最多 6 块）。
        self.accepts_parts = bool(accepts_parts)
        #: 面板的种类（`PART_KINDS`）；整方块设备为 None。
        self.part_kind = part_kind

    def is_part(self):
        # type: () -> bool
        """是否是面板形态（挂在光缆上，不是方块）。"""
        return self.form == FORM_PART

    def has_capability(self, name):
        # type: (str) -> bool
        return name in self.capabilities

    def supplies(self):
        # type: () -> bool
        """该设备是否向网络供能。**只认 `supply is not None` 这一个事实。**"""
        return self.supply is not None

    def supply_rate(self):
        # type: () -> object
        """供能速率（EN/t）。返回 `int` 或 `UNCONFIGURED`；不供能返回 `None`。

        未解包 `Value` 的话调用方会拿到 `Value` 对象，做算术时才炸——
        那时已经离出错的地方很远了。
        """
        if self.supply is None:
            return None
        if isinstance(self.supply, Value):
            return self.supply.value
        return self.supply

    def allows(self, other_device_id):
        # type: (str) -> bool
        """该控制器是否允许接入指定设备。未限定时一律允许。"""
        if self.allowed_device_ids is None:
            return True
        return other_device_id in self.allowed_device_ids

    def __repr__(self):
        return "DeviceDefinition(%s, %s)" % (self.id, self.category)


class TemplateSpec(object):
    """一种样板的静态定义，逐项取自 items.md 的「样板」一节。

    **除堆叠上限外没有数值字段，这是有意的。** items.md 对两类样板只给了标识、
    用途与堆叠上限，没给容量、耐久之类的数值——它不是存储介质，而是一件纯
    资源产物。凭空补一个 `capacity` 字段就等于替 R09 定案了一条从未被决定的
    玩法规则。

    `max_stack_size` 是 2026-10-02 由 User 明确给出的**玩法数值**（工厂样板 64，
    样板终端的输入槽要能一次放一叠），因此记在这里、由 `gen_items.py` 读走；
    不是替身、也不进 `unresolved_report()`。

    `mode` 的取值域与 `server/crafting/patterns.py` 是同一套词汇（常量在本文件，
    那边 import 过去）。`mode` 只是规格默认模式；工厂样板可承载两种模式，
    设备适用性按物品 userData 中的编码模式判定。
    """

    __slots__ = ("id", "label", "mode", "usage", "max_stack_size")

    def __init__(self, template_id, label, mode, usage, max_stack_size=1):
        # type: (str, str, str, str, int) -> None
        if mode not in PATTERN_MODES:
            raise ValueError("未知样板模式: %r" % (mode,))
        if isinstance(max_stack_size, bool) or not isinstance(max_stack_size, int):
            raise ValueError("堆叠上限必须是整数: %r" % (max_stack_size,))
        if max_stack_size < 1:
            raise ValueError("堆叠上限必须为正: %r" % (max_stack_size,))
        self.id = template_id
        self.label = label
        self.mode = mode
        #: 堆叠上限。见类 docstring：这是 User 给的玩法数值，不是替身。
        self.max_stack_size = max_stack_size
        #: 用途说明，进物品说明与诊断，不参与任何判定。
        #:
        #: **只写「指定什么配方」那半句**：items.md 的原句还有后半段
        #: 「放入某台设备使用」，而那半句由 `PATTERN_DEVICES` 提供、
        #: 由 `gen_items.template_tip` 拼上去。两处各写一遍的代价是
        #: 设备表改了名字，物品提示还说着旧设备。
        self.usage = usage

    def __repr__(self):
        return "TemplateSpec(%s, %s)" % (self.id, self.mode)


class ToolSpec(object):
    """一件工具物品的静态定义（13 §2：网络扳手；2026-11-13 加配置卡、无线连接工具）。

    **只有标识、名字与物品说明，没有数值字段，这是有意的。** 工具的行为——切断光缆的
    某一面、拆单块面板、潜行拆卸（13 §6），设隧道频率、配对无线连接器——全在交互层，作者
    没给任何数值；堆叠上限这类物品属性归物品生成器，与介质、内存条同理。

    `tip` 是物品说明的正文（不含开头的换行与颜色码）：物品 JSON 由 `scripts/gen_items.py`
    按它写；配置卡与无线连接工具把自己记着的东西写进物品实例的说明时，也以它开头
    （实例说明会盖掉物品 JSON 的那一份），两处同源。
    """

    __slots__ = ("id", "label", "tip")

    def __init__(self, tool_id, label, tip=""):
        # type: (str, str, str) -> None
        self.id = tool_id
        self.label = label
        self.tip = tip

    def __repr__(self):
        return "ToolSpec(%s)" % (self.id,)


class MediumSpec(object):
    """存储介质规格，逐项取自 items.md。"""

    __slots__ = (
        "id",
        "label",
        "max_types",
        "capacity_units",
        "read_units_per_second",
        "write_units_per_second",
    )

    def __init__(
        self,
        medium_id,
        label,
        max_types,
        capacity_units,
        read_units_per_second,
        write_units_per_second,
    ):
        self.id = medium_id
        self.label = label
        self.max_types = max_types
        self.capacity_units = capacity_units
        self.read_units_per_second = read_units_per_second
        self.write_units_per_second = write_units_per_second

    def __repr__(self):
        return "MediumSpec(%s)" % (self.id,)


class MemorySpec(object):
    """一种内存条的静态定义，取自 items.md 的「内存」一节。

    **只有一个数值字段，而且它是已定案值，不是替身。**（User 2026-10-03 定，
    见 `plans/items.md` 的内存一节。）在它出现之前，内存容量算不出来：
    R04 记着「内存条容量仍待定」，`capacity.OrderPolicy.bytes_per_memory_slot`
    只能拿 1024 B 当替身、按内存槽**方块个数**估。现在容量有了权威来源，
    替身随之下线。

    **不带 `unresolved` 标签、也不进 `unresolved_report()`**：items.md 是玩法的
    单一事实来源，写进那里的型号与容量就是定案，不需要再挂一个未定标记。
    这与 `MediumSpec` 不同——介质那份表在 items.md 里带省略号（R10 未列全），
    所以「规格存在」与「已注册为物品」是两张表；内存这一节没有省略号。

    堆叠上限**不在这里**，走 `gen_items.ITEM_MAX_STACK_SIZE`（= 1，与介质同档）：
    内存条不携带 `userData`，取 1 的理由是「与同类小件物品一致」，
    不是「怕一叠共享同一份物品数据」。
    """

    __slots__ = ("id", "label", "capacity_bytes")

    def __init__(self, memory_id, label, capacity_bytes):
        # type: (str, str, int) -> None
        if isinstance(capacity_bytes, bool) or not isinstance(capacity_bytes, int):
            raise ValueError("内存容量必须是整数: %r" % (capacity_bytes,))
        if capacity_bytes <= 0:
            raise ValueError("内存容量必须为正: %r" % (capacity_bytes,))
        self.id = memory_id
        self.label = label
        #: 该型号一根提供多少字节（items.md 的「内存容量 N 字节」）。
        self.capacity_bytes = capacity_bytes

    def __repr__(self):
        return "MemorySpec(%s, %d B)" % (self.id, self.capacity_bytes)


# -- 设备的在线 / 离线外观（2026-11-13，作者画的离线材质）---------------------------

#: 整方块设备的外观状态（方块状态）：0 在线、1 离线、2 冲突（只有控制器有：多台控制器连在一张网，
#: 照 AE2 控制器冲突变红）。默认是 0：物品栏图标与刚放下的样子都是在线外观，服务端随即按设备
#: 状态写（`server/devices/status_looks.py`）。生成器按它给方块加状态与贴图切换。
STATUS_STATE = "endernet:status"
STATUS_ONLINE = 0
STATUS_OFFLINE = 1
STATUS_CONFLICT = 2

#: 没有离线外观的整方块设备：创造能量源是电源（作者画的离线图与在线图一样）。
NO_STATUS_LOOK = frozenset([
    "endernet:creative_energy_source",
])


#: 整方块设备的**透传容量**（13 §0.5「设备透传带宽（AE2 式）」，R14-a）：
#: 线路经过一台设备继续往下走时，这台设备这一段最多能让多少带宽经过。
#: 2026-11-09 作者定案 **8u**（与 AE2 整方块设备透传 8 频道一致），已定数值，
#: 不带 `unresolved` 标签、不进 `unresolved_report()`。所有整方块设备共用这一个数；
#: 面板不透传（它只挂在宿主光缆上，不对外连）。
DEVICE_PASSTHROUGH_QUARTERS = Value(
    units_to_quarters(8),
    note="整方块设备透传容量 8u（R14-a，2026-11-09 作者定案）",
)

#: 按装置数量计费的规则。基础能耗为 0 是确定的（没有硬盘就不耗能），
#: 这里给出的是每增加一个单元的增量，供定义表引用与诊断。
PER_DISK_ENERGY = Value(1, note="存储驱动器：每块硬盘 1 EN/t（blocks.md）")
PER_MEMORY_ENERGY = Value(8, note="内存槽：每根内存条 8 EN/t（blocks.md）")


# -- P2P 隧道与无线连接器（2026-11-13 作者认可 `.claude/17` 的推荐值）---------------

#: 一个频率组（一个输入、若干输出）总共能让多少带宽经过：32u，同 AE2 ME 隧道外侧的致密容量。
#: 拓扑在线路树上把它记成隧道段的容量，经过这条隧道的各个出口分摊这一份。
TUNNEL_CAPACITY_QUARTERS = Value(
    units_to_quarters(32),
    note="P2P 网络隧道每个频率组 32u（2026-11-13 作者认可）",
)

TUNNEL_PART_ID = "endernet:p2p_network_tunnel"
WIRELESS_CONNECTOR_ID = "endernet:wireless_connector"

#: 无线连接器配对的最远距离（格，两台方块中心的直线距离）。只连同一维度。
WIRELESS_MAX_DISTANCE = Value(256, note="无线连接器配对上限 256 格（2026-11-13 作者认可）")

#: 无线连接器按配对距离多耗的能：每个「距离单位」1 EN/t，单位数 = ceil(0.025 × 距离^1.5)
#: （`wireless_distance_units`）。每台连接器各记一份，加上自身固定的 4 EN/t，一对合计约
#: `8 + 0.05 × 距离^1.5`：64 格约 34 EN/t、256 格约 213 EN/t。
WIRELESS_DISTANCE_ENERGY = Value(1, note="无线连接器：每个距离单位 1 EN/t（2026-11-13 作者认可）")
WIRELESS_DISTANCE_FACTOR = 0.025
WIRELESS_DISTANCE_EXPONENT = 1.5


def wireless_distance_units(distance):
    # type: (float) -> int
    """配对距离（格）→ 计耗能的单位数。没配对（距离 0）为 0。"""
    if distance <= 0:
        return 0
    return int(math.ceil(WIRELESS_DISTANCE_FACTOR * float(distance) ** WIRELESS_DISTANCE_EXPONENT))


# -- 存储驱动器配置 --------------------------------------------------------

#: 存储驱动器槽数。**这一份同时是两处的取值来源**：
#: `server/devices/storage_driver.py` 的 `DriverPolicy`，以及方块的
#: `netease:block_container.container_size`（由 `scripts/gen_blocks.py` 读出）。
#: 写在一处是刻意的：两边各写一个 10 迟早会分叉成「界面 10 格、领域层认 8 格」，
#: 而那种错误在离线测试里看不出来。
#:
#: 槽数本身是 R10 未定案的玩法（UI 格数不代表已定规则），因此这里是标注过的测试替身。
STORAGE_DRIVER_SLOT_COUNT = Value(
    10,
    unresolved="R10",
    is_test_value=True,
    note="驱动器槽数；也是方块容器格数（gen_blocks.py 从同一条读出）",
)

# -- 接口槽位（2026-09-25 接口规格）----------------------------------------
#
# 这三条是**已定案**的（`blocks.md` 的「网络接口」一节逐条写明），因此
# **不带 `unresolved` 标签**——它们不是替身值。
#
# 早先这里只有一条 `BUS_BUFFER_SLOT_COUNT`，把被动接口与样板接口当成同一种
# 「R05 测试缓冲」。规格同步之后两者的槽位**用途完全不同**：
# 被动接口的 9 格是存取共用的**真实库存**，样板接口的 9 格**专用于工厂样板**。
# 合成一条的代价是「9 格 × 64 件 = 576」这个估计被套到两种设备上，
# 而它既不成立（真实容量要按每件物品的 maxStackSize 算），
# 也让 R05 的诊断多报一条已经定案的规则。


#: 被动总线接口的槽数：**存货与取货共用这 9 格**（`blocks.md`）。
#: 同时是方块容器的格数（`scripts/gen_blocks.py` 从这一条读出），
#: 因此界面格数与领域层槽数不可能分叉。
PASSIVE_PORT_SLOT_COUNT = Value(
    9,
    note="被动接口存取共用的真实槽位数；也是方块容器格数（blocks.md 已确定）",
)

#: 样板接口的槽数：**专用于存放工厂样板**，与原料缓冲分离（`blocks.md`）。
#: 同样由生成器读作方块容器格数——那个容器归任务 07 使用，
#: **任务 06 的投料不碰它**。
TEMPLATE_PATTERN_SLOT_COUNT = Value(
    9,
    note="样板接口的工厂样板专用槽数；也是方块容器格数（blocks.md 已确定）",
)

TEMPLATE_RETURN_SLOT_COUNT = Value(9, note="样板接口产物回流槽数")
TEMPLATE_CONTAINER_SLOT_COUNT = Value(
    TEMPLATE_PATTERN_SLOT_COUNT.value + TEMPLATE_RETURN_SLOT_COUNT.value,
    note="样板接口样板槽与产物回流槽合计",
)

#: 装配仓的槽数：**19**，槽号从 0 开始（`blocks.md` 2026-09-27 确认）。
#: 0～8 合成原料、9 输出、10～18 合成模式的工厂样板。
#:
#: 与上面两条同理，`scripts/gen_blocks.py` 从这一条读作方块的
#: `netease:block_container.container_size`。**格数只写一处**：界面画 19 格、
#: 方块容器开 19 格、领域层认 19 格，三者同源才不会分叉成「界面上能放、
#: 领域层不认」——而那种错误在离线测试里没有任何征兆。
ASSEMBLE_CHAMBER_SLOT_COUNT = Value(
    19,
    note="装配仓容器槽数（0～8 原料 / 9 输出 / 10～18 样板）；也是方块容器格数",
)

# -- 存货口／取货口／内存槽／流体口的容器格数（2026-09-29 User 定案）---------
#: 主动物品口按视线六向放置；只连接朝向的一面，不扫描其他邻面。
SIX_WAY_ITEM_PORTS = frozenset((
    "endernet:bus_item_input_io",
    "endernet:bus_item_output_io",
))

#
# 背景：底下这几台设备的界面**本来就画了**引擎的 `netease_container` 集合
# （靠继承 `endernet_container_common.container_grid_items`），但方块上
# **没有**容器组件，于是那些格子没有任何权威数据源——表现是「方块里有东西，
# 打开界面却看不见」，而离线断言只查「界面画没画这个集合」，查不到这一层。
#
# User 2026-09-29 定案的原话：「不是终端类方块且能存储物品且有 UI 有物品槽的
# 都要有网易容器」。四条格数由此而来。
#
# **终端类（`CATEGORY_TERMINAL` 那四台）不在此列**：它们的槽位物品数量可以
# 超限，一律走自定义槽位控件与自定义渲染，不能由 `NeteaseContainer` 打开。
#
# 声明了容器组件就**必须**同时把 `description.base_block` 设为
# `netease_container`（`scripts/gen_blocks.py` 的 `build_block_json` 负责）。

#: 物品存货口的缓冲槽数。
#:
#: **2026-09-29 由 9 改为 5。** 改前三处不一致：`blocks.md` 写 9、
#: `plans/ui/bus_item_input_io.md` 写「9×1」「业务槽号 0–8」，
#: 而界面 JSON 的 `cached_slots/container_grid` 是 `grid_dimensions = [5, 1]`。
#: User 定**以界面为准**，两份文档随之改成 5。
#:
#: 这个分叉当时在门禁里**没有任何征兆**：`test_assets.py` 只比对「界面画没画
#: 这个集合」，不比对格数。改完之后它也不会被自动拦住——见 11-07 的 B 组。
#:
#: **两端现在都认这 5 格。** 领域层那半边于 2026-09-29 补上（与取货口一并，
#: 11-07 的 C1）：`server/devices/bus_io.py` 的 `PortStore.ensure` 为 `INTAKE` 建了
#: `BUFFER_CONTAINER` 形态的 `TransferBuffer`，件数上界由
#: `PortPolicy.intake_buffer_capacity` 从本值推出。
#:
#: 但**规格要求的流向「朝向容器 → 缓冲 → 网络」尚未实现**：`dispatch_intake`
#: 至今走的是朝向容器 → 网络直输（06-05）。「这 5 格有权威来源」与
#: 「缓冲已打通」仍是两件事，不要读成后者。
ITEM_INTAKE_PORT_SLOT_COUNT = Value(
    5,
    note="物品存货口的缓冲槽数；也是方块容器格数（User 2026-09-29 定，以界面为准）",
)

#: 物品取货口的缓存槽数。**三处本来就一致**（`blocks.md`「5 个内部真实物品
#: 缓存槽」、`plans/ui/bus_item_output_io.md`「业务槽号 0–4」、
#: 界面 JSON `grid_dimensions = [5, 1]`），这条只是把它收进单一来源表。
#:
#: **两端现在都认这 5 格。** 领域层那半边于 2026-09-29 补上（11-07 的 C1）：
#: `server/devices/bus_io.py` 的 `PortStore.ensure` 为 `OUTPUT` 建了
#: `BUFFER_CONTAINER` 形态的 `TransferBuffer`，件数上界由
#: `PortPolicy.output_cache_capacity` 从本值推出。
#:
#: 而**「网络 → 缓存 → 朝向容器」这条中转路由已于 2026-10-03 交付**（06-06），
#: 缓存开关（`PortConfig.cache_enabled`）目前只被记录与展示。
#: 「这 5 格有权威来源」与「缓存已打通」仍是两件事，不要读成后者。
ITEM_OUTPUT_PORT_SLOT_COUNT = Value(
    5,
    note="物品取货口的缓存槽数；也是方块容器格数（blocks.md 已确定）",
)

#: 内存槽的内存条槽数：**10**。
#:
#: User 2026-09-29 定案，口径是**照界面**：界面 JSON 是
#: `grid_dimensions = [5, 2]` 配 `maximum_grid_items = 10`。
#: `blocks.md` 的内存槽一节原先**完全没写槽数**，这次一并补上。
MEMORY_SLOT_COUNT = Value(
    10,
    note="内存槽可插的内存条数；也是方块容器格数（User 2026-09-29 定，照界面）",
)

#: 两个流体口的方块容器格数：**各 1 格**。
#:
#: **这是标注过的占位，不是玩法定案。** User 2026-09-29 的原话是「这些不是
#: 物品容器，所以**先设置**为仅有 1 个槽位（界面内隐藏）的网易容器」。
#: 真实流体适配属 13-03、尚未实现；而引擎**根本没有流体容器**
#: （`netease:block_container` 只装物品，整个 ModSDK 里 "fluid" 与容器无关），
#: 也就是说这 1 格现在装的是**物品**、与流体无关。之所以仍然要挂，是因为
#: 一台有界面、界面里又有格子的设备空着容器，是另一种不一致。
#:
#: 界面上**刻意不画**这个集合——免得玩家看见一个和流体毫无关系的物品格。
#: 那条「刻意不画」由 `endernet_tests/test_assets.py` 里的独立断言钉住，
#: 不是靠这条注释自觉。
#:
#: 归入 R07（「接口流体存取」写在它的影响栏里），因此会出现在
#: `DefinitionSet.unresolved_report()` 里，不会被当成已定案。
FLUID_PORT_SLOT_COUNT = Value(
    1,
    unresolved="R07",
    is_test_value=True,
    note="流体口的方块容器格数；占位值，真实流体适配属 13-03（User 2026-09-29 指定）",
)


#: 取货（被动接口的取货与主动取货口）**最多能配置多少种资源**。
#: `blocks.md`：「对于取货，允许设定 9 种物品/流体」/「只能设定至多 9 种待取物」。
#:
#: **它只管取货。** 主动物品存货口已独立定案为16项；其他存货角色不据此推导。
#: 旧文曾把存货过滤条目上限记为尚未定案，历史说明如下：
#: 「不得从取货 9 种上限推导存货过滤上限」——那一条在 `PortPolicy` 里，
#: 不在定义表里。
PICKUP_FILTER_LIMIT = Value(
    9,
    note="取货最多可配置的资源种类数（blocks.md 已确定；不适用于存货过滤）",
)


# -- 介质表（items.md）-----------------------------------------------------

MEDIUMS = (
    MediumSpec("endernet:singleside_tape", "小型单面磁带", 12, 1728, 4, 4),
    MediumSpec("endernet:doubleside_tape", "小型双面磁带", 24, 3456, 4, 4),
    MediumSpec("endernet:database_compressed_tape", "数据库高密磁带", 64, 2 ** 30, 64, 64),
    MediumSpec("endernet:softdisk_1k", "1K软盘", 32, 1728, 16, 16),
    MediumSpec("endernet:softdisk_4k", "4K软盘", 32, 5120, 32, 32),
    MediumSpec("endernet:harddisk_1k", "1K硬盘", 64, 4096, 24, 16),
    MediumSpec("endernet:harddisk_4k", "4K硬盘", 64, 2 ** 16, 32, 24),
    # items.md 中标识即为 harddisk_16（不是 harddisk_16k），保持原样不改拼写。
    MediumSpec("endernet:harddisk_16", "16K硬盘", 64, 2 ** 18, 48, 32),
)

#: **已注册为游戏物品**的介质型号。
#:
#: 与 `MEDIUMS` 分开是有意的：`MEDIUMS` 是规格表（介质有哪些、多大容量），
#: 这一份是**内容注册的分阶段清单**——哪些型号已经真的做成了游戏物品
#: （`endernet_bp/netease_items_beh/` 里有条目、资源包里有贴图、语言文件里有名字）。
#:
#: R10 的型号表尚未列全（items.md 里省略号代表的型号不算已定），因此
#: 这里先创建**容量并列最小的两个**，其余型号目前只有规格、不能作为物品获得。
#: 领域层不读这一份：驱动器接受哪些型号由 `DriverPolicy.accepted_models` 决定，
#: 两者是不同的规则。
ITEM_MEDIUMS = (
    "endernet:singleside_tape",
    "endernet:softdisk_1k",
)


# -- 内存表（items.md）-----------------------------------------------------

#: 内存条型号，逐项取自 items.md 的「内存」一节（User 2026-10-03 定）。
#:
#: **容量值就是 items.md 上写的那个数，不在这里另算一套。** 标识与中文名
#: 逐字照抄——`zh_CN.lang` 里的名字与 `MemorySpec.label` 必须一致，由
#: `test_assets.TestItemLocalisation` 钉住；物品 JSON 的提示文本由
#: `scripts/gen_items.py` 的 `memory_tip` 从 `capacity_bytes` 读出。
#:
#: 16KB／64KB／256KB 三条在 items.md 里原先都写成「4K 字节」（与型号名矛盾），
#: 2026-10-03 由 User 确认按型号名修正为 16384／65536／262144，原文已同步。
MEMORIES = (
    MemorySpec("endernet:memory_128b", "128B 感应式内存块", 128),
    MemorySpec("endernet:memory_256b", "256B 感应式内存块", 256),
    MemorySpec("endernet:memory_512b", "512B 感应式内存块", 512),
    MemorySpec("endernet:memory_1k", "1KB 集成式内存卡", 1024),
    MemorySpec("endernet:memory_4k", "4KB 集成式内存卡", 4096),
    MemorySpec("endernet:memory_16k", "16KB 集成式内存卡", 16384),
    MemorySpec("endernet:memory_64k", "64KB 集成式内存卡", 65536),
    MemorySpec("endernet:memory_256k", "256KB 内存条", 262144),
)

#: **已注册为游戏物品**的内存条型号。与 `ITEM_MEDIUMS` / `ITEM_TEMPLATES`
#: 同一地位、同一理由：规格表与「已经做出来了」是两件事。
#:
#: 内存这一节在 items.md 里**没有省略号**，八条型号是列全的，因此这里八条全注册
#: （介质那边相反：表里有省略号，只注册了容量最小的两个）。
ITEM_MEMORIES = (
    "endernet:memory_128b",
    "endernet:memory_256b",
    "endernet:memory_512b",
    "endernet:memory_1k",
    "endernet:memory_4k",
    "endernet:memory_16k",
    "endernet:memory_64k",
    "endernet:memory_256k",
)


# -- 样板表（items.md）-----------------------------------------------------

#: 唯一工厂样板物品；两种模式均保存在编码中。
#:
#: **除堆叠上限外没有数值字段，这是有意的**：items.md 只给了标识、用途与堆叠
#: 上限，凭空补别的（容量、耐久）就等于替 R09 定案。这里只有足以做出物品、
#: 做出版本校验、以及判「这块样板该进哪台设备」的信息。
PATTERN_TEMPLATES = (
    TemplateSpec(
        "endernet:factorical_template",
        "工厂样板",
        PATTERN_MODE_FACTORY,
        "记录合成或工厂模式配方；模式保存在物品编码中",
        max_stack_size=64,
    ),
)

#: 两种模式均使用工厂样板物品，具体模式读取 userData。
TEMPLATE_PANEL_ITEM_ID = "endernet:factorical_template"
ITEM_TEMPLATES = (TEMPLATE_PANEL_ITEM_ID,)

#: 装配仓的实体槽位分区，UI 与执行层须遵守同一映射。
ASSEMBLE_PATTERN_SLOTS = tuple(range(10, 19))

#: 每种样板模式**在文档里指定**的设备（items.md 的用途、blocks.md 的样板接口一节）。
#:
#: 这张表本身是已定案的事实，**不带 unresolved 标签**；它是否被**强制执行**
#: 取决于 `PatternPolicy.kind_scope`——R09 要澄清的正是「两类样板的适用范围」，
#: 所以规则在策略里，事实在这张表里。这样策略换成宽松口径时不需要改数据。
#:
#: 装配仓与样板接口都在设备表里（`CATEGORY_ASSEMBLY` / `endernet:bus_template_io`）；
#: 后者恰好也是**装配仓之外唯一**声明 `CAP_PATTERN_PROVIDER` 的方块。
PATTERN_DEVICES = {
    PATTERN_MODE_CRAFTING: frozenset(["endernet:assemble_chamber"]),
    PATTERN_MODE_FACTORY: frozenset(["endernet:bus_template_io"]),
}


def devices_for_mode(mode):
    # type: (str) -> frozenset
    """该模式的样板在文档里指定给哪些设备。未知模式返回空集。"""
    return PATTERN_DEVICES.get(mode, frozenset())


#: 右键哪个终端方块打开哪个界面（11-03 / 11-04 / 11-05 / 11-06）。
#:
#: **四个终端方块都在 `DEVICES` 里、都带 `CAP_TERMINAL_ENDPOINT`，现在四个
#: 都映射上了**：工厂终端那一行由 11-06 补上，与它的客户端屏同批落地。
#: 在此之前它**不映射、不打开、也不回退到通用终端**——把它接到通用终端
#: 就是替那个子任务悄悄定案（全局约束 7），当时右键它静默无反应。
#:
#: **只有屏幕真的注册了才写进来**：`PushUIRequest` 拿这个值在客户端查屏幕，
#: 查不到时 `tooldelta/ui/event_handler.py` 会**抛异常**——先加映射只会让
#: 右键炸掉（11-04 的客户端接上之前，合成终端那一行就是这么被压着的）。
#: 因此这几条与 `client/ui/__init__.py` 的 `register_screens()` 必须同步增删。
#:
#: 值必须与 `@RegistToolDeltaScreen(...)` 注册的键**逐字相同**。
TERMINAL_SCREENS = {
    "endernet:common_terminal_panel": "endernet_common_terminal_panel.main",
    "endernet:crafting_terminal_panel": "endernet_crafting_terminal_panel.main",
    "endernet:template_terminal_panel": "endernet_template_terminal_panel.main",
    "endernet:factoric_terminal_panel": "endernet_factoric_terminal_panel.main",
}

#: 哪些容器类方块**有挂上去的脚本屏**（11-07）。键是方块 id，值是屏幕名。
#:
#: **与 `TERMINAL_SCREENS` 是两条不同的路**，不要合并：
#:
#: - 终端类：屏幕是本模组自有的，服务端要 `PushUIRequest` 把它**推**开；
#: - 这一张：屏幕由**引擎**按方块组件自动打开，服务端只推一条**身份**
#:   （「你打开的这个界面是这台设备」），由屏上的代理自己认领。
#:   详见 `common/channels/device_identity.py` 与 `plans/technical_notes/netease_container_screens.md`。
#:
#: 三条纪律：
#:
#: 1. 值必须与 `@RegistToolDeltaScreen(...)` 注册的键**逐字相同**（它是
#:    `namespace.main` 的屏幕名，也是两侧比对的握手串）；
#: 2. 与 `TERMINAL_SCREENS` **不相交**——同一台方块不可能两条路都走；
#: 3. 必须与 `scripts/gen_blocks.py` 的 `CONTAINERS` 对得上（方块确实声明了
#:    容器组件）。三条都由 `endernet_tests/test_device_panels.py` 钉住。
#:
#: 所有已有容器界面的设备都接入状态订阅；配置交互按各自交付范围接入。
CONTAINER_SCREENS = {
    "endernet:bus_item_io": "endernet_bus_item_io.main",
    "endernet:memory_slot": "endernet_memory_slot.main",
    "endernet:bus_fluid_input_io": "endernet_bus_fluid_input_io.main",
    "endernet:bus_fluid_output_io": "endernet_bus_fluid_output_io.main",
    "endernet:bus_item_output_io": "endernet_bus_item_output_io.main",
    "endernet:bus_item_input_io": "endernet_bus_item_input_io.main",
    "endernet:bus_template_io": "endernet_bus_template_io.main",
    "endernet:assemble_chamber": "endernet_assemble_chamber.main",
    # 存储驱动器（11-07）。**屏幕是空的**——User 2026-10-03 裁定：介质的
    # 装卸与 Lore 刷新**暂不需要界面**，只保留服务端逻辑，因此本屏只有
    # 状态栏与详情弹窗（`ProxyPanelBase` 给的那两样），不接任何配置控件。
    # 它的 UI 文档 `plans/ui/storage_driver.md` 第 2～8 节仍是空模板，
    # 所以**不能**给它写配置面；这一行登记的是「引擎开着的那张屏由谁接管」。
    "endernet:storage_driver": "endernet_storage_driver.main",
}

#: 同一个人两次打开终端界面的最小间隔（秒）。窗口内再右键**静默忽略**。
#:
#: User 2026-09-30 定：**0.2 秒**。它只挡高频连点——正常走过去右键一次感觉
#: 不到，而连点几下不会开出好几个界面。**不是 0 也不是负值**：0 的含义是
#: 「窗口为零秒」，两种读法会给出相反的默认，因此本仓 `PlayerRateLimiter`
#: 只把 `<= 0` 读作「不限速」。
#:
#: 放在定义表里而不是写死在适配器里：它是**玩家看得见的行为参数**（点了不
#: 开界面），而定义表是本仓数值的单一事实来源。改它要连带看一眼
#: `server/adapters/events.py` 的 `_on_block_use` 与
#: `endernet_tests/test_rate_limit.py` 里的窗口断言。
TERMINAL_OPEN_LIMIT_SECONDS = 0.2


# -- 设备表（blocks.md）----------------------------------------------------

DEVICES = (
    # --- 网络控制器分组：全组实例合计最多 1 个 ---
    DeviceDefinition(
        "endernet:dos_network_endpoint",
        "DOS网络控制器",
        CATEGORY_CONTROLLER,
        bandwidth=BandwidthSpec(UNCONFIGURED),
        energy=EnergySpec(Value(4)),
        # blocks.md 明确列举了它能控制的方块，按显式集合处理，不按名称推断。
        allowed_device_ids=(
            "endernet:dos_network_endpoint",
            "endernet:network_baseband_8",
            "endernet:network_baseband_16",
            "endernet:network_baseband_32",
            "endernet:storage_driver",
            "endernet:common_terminal_panel",
            "endernet:crafting_terminal_panel",
        ),
        notes="只接网络基带，须贴着基带核心；不允许自动存货/取货；不支持订单处理。",
    ),
    DeviceDefinition(
        "endernet:tiny_network_endpoint",
        "小型网络控制器",
        CATEGORY_CONTROLLER,
        bandwidth=BandwidthSpec(UNCONFIGURED),
        # 耗能跟随基带负载；EN/t 的口径本身待定（R08）。
        energy=EnergySpec(
            Value(
                UNCONFIGURED,
                unresolved="R08",
                is_test_value=True,
                note="blocks.md 记为「(基带负载)EN/t」，折算口径待定",
            ),
            follows_bandwidth=True,
        ),
        allowed_device_ids=None,
        notes="只接网络基带，须贴着基带核心；允许接入总线类设备；不支持订单处理。",
    ),
    # --- 网络传输介质 ---
    DeviceDefinition(
        "endernet:network_baseband_8",
        "8位网络基带",
        CATEGORY_BASEBAND,
        # 基带自身不消耗带宽，而是提供容量；`base_quarters` 保持未配置
        # 以免被误读成「基带要占带宽」，容量写在 capacity_quarters。
        # 2026-11-09 起容量的含义是**每个外露面**输出本档带宽（13 §0.1）。
        bandwidth=BandwidthSpec(UNCONFIGURED, capacity_quarters=units_to_quarters(8)),
        energy=EnergySpec(),
        notes="每个外露面输出 8u 带宽。",
    ),
    DeviceDefinition(
        "endernet:network_baseband_16",
        "16位网络基带",
        CATEGORY_BASEBAND,
        bandwidth=BandwidthSpec(UNCONFIGURED, capacity_quarters=units_to_quarters(16)),
        energy=EnergySpec(),
        notes="每个外露面输出 16u 带宽。",
    ),
    DeviceDefinition(
        "endernet:network_baseband_32",
        "32位网络基带",
        CATEGORY_BASEBAND,
        bandwidth=BandwidthSpec(UNCONFIGURED, capacity_quarters=units_to_quarters(32)),
        energy=EnergySpec(),
        notes="每个外露面输出 32u 带宽。",
    ),
    # --- 网络接口 ---
    DeviceDefinition(
        "endernet:bus_item_io",
        "总线物品接口",
        CATEGORY_BUS,
        bandwidth=BandwidthSpec(units_to_quarters(1)),
        energy=EnergySpec(),
        capabilities=(CAP_ITEM_PORT,),
        notes="blocks.md 未列耗能，归入 R10。",
    ),
    DeviceDefinition(
        "endernet:bus_fluid_io",
        "总线流体接口",
        CATEGORY_BUS,
        bandwidth=BandwidthSpec(units_to_quarters(1)),
        energy=EnergySpec(),
        capabilities=(CAP_FLUID_PORT,),
        notes="blocks.md 未列耗能，归入 R10。",
    ),
    DeviceDefinition(
        "endernet:bus_item_output_io",
        "总线物品取货口",
        CATEGORY_BUS,
        bandwidth=BandwidthSpec(units_to_quarters(1)),
        energy=EnergySpec(Value(4)),
        capabilities=(CAP_ITEM_PORT,),
    ),
    DeviceDefinition(
        "endernet:bus_fluid_output_io",
        "总线流体取货口",
        CATEGORY_BUS,
        bandwidth=BandwidthSpec(units_to_quarters(1)),
        energy=EnergySpec(Value(4)),
        capabilities=(CAP_FLUID_PORT,),
    ),
    DeviceDefinition(
        "endernet:bus_item_input_io",
        "总线物品存货口",
        CATEGORY_BUS,
        bandwidth=BandwidthSpec(units_to_quarters(1)),
        energy=EnergySpec(Value(4)),
        capabilities=(CAP_ITEM_PORT,),
    ),
    DeviceDefinition(
        "endernet:bus_fluid_input_io",
        "总线流体存货口",
        CATEGORY_BUS,
        bandwidth=BandwidthSpec(units_to_quarters(1)),
        energy=EnergySpec(Value(4)),
        capabilities=(CAP_FLUID_PORT,),
    ),
    DeviceDefinition(
        "endernet:bus_template_io",
        "总线样板接口",
        CATEGORY_BUS,
        bandwidth=BandwidthSpec(units_to_quarters(1)),
        energy=EnergySpec(Value(4)),
        capabilities=(CAP_PATTERN_PROVIDER, CAP_ITEM_PORT, CAP_FLUID_PORT),
    ),
    # --- 存储器 ---
    DeviceDefinition(
        "endernet:storage_driver",
        "存储驱动器",
        CATEGORY_STORAGE,
        # (硬盘个数/4)u —— 每块硬盘 1 个四分之一 u。
        bandwidth=BandwidthSpec(0, per_disk_quarters=1),
        # (硬盘个数)EN/t —— 每块硬盘 1 EN/t。
        energy=EnergySpec(Value(0), per_unit=PER_DISK_ENERGY),
        capabilities=(CAP_STORAGE_PROVIDER,),
        notes="耗能与带宽均按硬盘个数计（PER_DISK_ENERGY / per_disk_quarters）。",
    ),
    # --- 终端 ---
    DeviceDefinition(
        "endernet:common_terminal_panel",
        "通用终端面板",
        CATEGORY_TERMINAL,
        bandwidth=BandwidthSpec(units_to_quarters(1)),
        energy=EnergySpec(Value(4)),
        capabilities=(CAP_TERMINAL_ENDPOINT,),
    ),
    DeviceDefinition(
        "endernet:crafting_terminal_panel",
        "合成终端面板",
        CATEGORY_TERMINAL,
        bandwidth=BandwidthSpec(units_to_quarters(1)),
        energy=EnergySpec(Value(4)),
        capabilities=(CAP_TERMINAL_ENDPOINT,),
    ),
    DeviceDefinition(
        "endernet:template_terminal_panel",
        "样板终端面板",
        CATEGORY_TERMINAL,
        bandwidth=BandwidthSpec(units_to_quarters(1)),
        energy=EnergySpec(Value(4)),
        capabilities=(CAP_TERMINAL_ENDPOINT,),
    ),
    DeviceDefinition(
        "endernet:factoric_terminal_panel",
        "工厂终端面板",
        CATEGORY_TERMINAL,
        bandwidth=BandwidthSpec(units_to_quarters(1)),
        energy=EnergySpec(Value(4)),
        capabilities=(CAP_TERMINAL_ENDPOINT,),
    ),
    # --- 工厂相关 ---
    DeviceDefinition(
        "endernet:parallel_processor_connector",
        "并行处理器连接器",
        CATEGORY_PROCESSOR_CONNECTOR,
        bandwidth=BandwidthSpec(UNCONFIGURED),
        energy=EnergySpec(),
        notes="blocks.md 未列带宽与耗能，归入 R10。",
    ),
    DeviceDefinition(
        "endernet:parallel_processor",
        "并行处理器",
        CATEGORY_PROCESSOR,
        bandwidth=BandwidthSpec(units_to_quarters(4)),
        energy=EnergySpec(Value(1), Value(32)),
        capabilities=(CAP_PROCESSING_SLOTS,),
        notes="单个处理器提供 1 个配方处理槽位。",
    ),
    DeviceDefinition(
        "endernet:memory_slot",
        "内存槽",
        CATEGORY_MEMORY,
        bandwidth=BandwidthSpec(units_to_quarters(2)),
        energy=EnergySpec(Value(0), per_unit=PER_MEMORY_ENERGY),
        capabilities=(CAP_MEMORY,),
        notes="耗能按内存条个数 × 8 EN/t（PER_MEMORY_ENERGY）。",
    ),
    # --- 工厂方块 ---
    DeviceDefinition(
        "endernet:assemble_chamber",
        "装配仓",
        CATEGORY_ASSEMBLY,
        bandwidth=BandwidthSpec(units_to_quarters(1)),
        energy=EnergySpec(Value(1), Value(8)),
        capabilities=(CAP_PATTERN_PROVIDER,),
        notes="占用 1 个配方处理槽位。",
    ),
    # --- 其他 ---
    # 这一组两台都是「能源类设备」：不占带宽。2026-11-09 起与其他设备一样
    # 接基带、光缆或别的设备（透传），不再「只连基带」。
    # 分类复用 CATEGORY_ENERGY_INPUT 而不是再开一个：blocks.md 把它们同放在
    # 「其他」下，连接规则也完全一样；新开分类要同时改 ConnectionRules 与
    # PortRuleSet 两张表（`node.py` 的守卫会因半张表报错），还要补
    # gen_placeholder_textures 的调色板与图形表——那两张表对未知分类是**静默
    # 回退**（灰块 + 默认挖掘等级），没有测试拦得住。一个分类里住着「转换」与
    # 「发电」两种角色，由 `supply` 区分。
    DeviceDefinition(
        "endernet:energy_input_interface",
        "能源接入口",
        CATEGORY_ENERGY_INPUT,
        bandwidth=BandwidthSpec(0),  # blocks.md：不占用带宽
        energy=EnergySpec(),
        # 它**供能**，每 2 FE 可用 1 EN，定义表不预设固定速率。
        # 写成 None 会被读成「它不供能」，于是只接它的网络误报「确认没电」。
        supply=Value(
            UNCONFIGURED,
            unresolved="R08",
            is_test_value=True,
            note="2 FE = 1 EN，实际供能由接入口余量和本次结算决定",
        ),
        capabilities=(CAP_ENERGY_PORT,),
        notes="不占带宽不代表耗能为零；耗能未列，归入 R10。",
    ),
    DeviceDefinition(
        "endernet:creative_energy_source",
        "创造能量源",
        CATEGORY_ENERGY_INPUT,
        bandwidth=BandwidthSpec(0),  # blocks.md：不占用带宽
        # blocks.md 未列它自身的耗能，归入 R10。不供能设备才写 EnergySpec(Value(0))。
        energy=EnergySpec(),
        # 数值取自 blocks.md，已是定案值，不是替身。
        supply=Value(2147483647, note="blocks.md：为网络提供 2147483647 EN/t"),
        # 不声明任何能力：它没有运行期接口（静态电源，由能源服务读定义表得知）。
        capabilities=(),
        notes=(
            "供能设备：为网络提供 2147483647 EN/t。不占带宽，"
            "因此不影响线路分配与带宽准入。"
        ),
    ),
    # --- 无线连接（2026-11-13）---
    # 载体：容量照致密光缆 32u，自身不占带宽；R14-a 的设备透传 8u 不适用于它。
    # 耗能 = 固定 4 EN/t + 每个距离单位 1 EN/t（单位数由配对距离算，没配对为 0），
    # 单位数由装配层的 `_energy_inputs_of` 按配对数据给出，与驱动器按硬盘个数计同一条路。
    DeviceDefinition(
        WIRELESS_CONNECTOR_ID,
        "无线连接器",
        CATEGORY_WIRELESS,
        bandwidth=BandwidthSpec(0, capacity_quarters=units_to_quarters(32)),
        energy=EnergySpec(Value(4), per_unit=WIRELESS_DISTANCE_ENERGY),
        notes=(
            "载体 32u，自身不占带宽；用无线连接工具与另一台配对后两台合成一张网"
            "（同维度、256 格内）。耗能 4 EN/t 加上按配对距离的部分。"
        ),
    ),
)


# -- 末影光缆（13 §2）--------------------------------------------------------
#
# 三种光缆互相能连（13 §0.9）；载体容量就是 `capacity_quarters`，自身不占带宽
# （`base_quarters = 0` 是已定事实：13 §2「载体 8u」，不是缺项）。耗能 blocks.md
# 没列，按全局约束 7 留 `UNCONFIGURED`（R10），与基带同一处理。
#
# 方块本身（状态 `endernet:side_<面>`、骨骼可见性、按排列的碰撞盒、方块实体）
# 由阶段 C 的生成器出，**不进 `DEVICES`**：`gen_blocks.py` 按 `DEVICES` 逐台生成
# 标准方块，光缆混进去会被生成成整方块。

CABLES = (
    DeviceDefinition(
        "endernet:ender_glass_cable",
        "末影玻璃光缆",
        CATEGORY_CABLE,
        bandwidth=BandwidthSpec(0, capacity_quarters=units_to_quarters(8)),
        energy=EnergySpec(),
        accepts_parts=True,
        notes="载体 8u，自身不占带宽；可挂面板，一格最多 6 块。",
    ),
    DeviceDefinition(
        "endernet:ender_cable",
        "末影光缆",
        CATEGORY_CABLE,
        bandwidth=BandwidthSpec(0, capacity_quarters=units_to_quarters(16)),
        energy=EnergySpec(),
        accepts_parts=True,
        notes="载体 16u，自身不占带宽；可挂面板，一格最多 6 块。",
    ),
    DeviceDefinition(
        "endernet:ender_dense_cable",
        "致密末影光缆",
        CATEGORY_CABLE,
        bandwidth=BandwidthSpec(0, capacity_quarters=units_to_quarters(32)),
        energy=EnergySpec(),
        accepts_parts=False,
        notes="载体 32u，自身不占带宽；不挂面板。",
    ),
)


# -- 面板底座（2026-11-10 作者定案，照 AE2 的 cable bus 只挂面板）------------------
#
# 面板对着普通方块放、或像火把一样放到相邻的空格时，那一格新建一个面板底座：只装面板，
# 没有线芯与连接臂，不与任何设备相连（连接表里没有这一类），所以不入网；面板的状态栏
# 原因是「面板没有接在光缆上」。最后一块面板拆掉它就消失；手持玻璃或普通光缆点它，
# 换成那种光缆（面板与库存标记原样保留）。没有对应物品，自身不掉落。
#
# 方块实体数据、方块状态 `endernet:side_<面>`（每面 0 / 2）与光缆同一套，状态维护也是
# 同一个 `CableStateKeeper`，因此与光缆一样不进 `DEVICES`，由生成器另走一条路出方块。
# 带宽与耗能都是 0：它不在任何网络里，这是结构性事实，不是缺项。

PART_HOLDER_ID = "endernet:part_holder"

PART_HOLDERS = (
    DeviceDefinition(
        PART_HOLDER_ID,
        "面板底座",
        CATEGORY_PART_HOLDER,
        bandwidth=BandwidthSpec(0),
        energy=EnergySpec(idle=0),
        accepts_parts=True,
        notes="只承载面板，不入网；最后一块面板拆掉即消失，手持玻璃或普通光缆点它换成光缆。",
    ),
)


def _terminal_part(part_id, label, panel_id):
    # type: (str, str, str) -> DeviceDefinition
    """按整方块终端生成它的面板形态：带宽、耗能、能力**直接取同一份对象**。

    逐项抄一遍的代价是哪天改了终端的耗能，面板还是旧值——两者在 13 §2 里
    写明「相同」，那就只能有一份。
    """
    panel = None
    for definition in DEVICES:
        if definition.id == panel_id:
            panel = definition
    if panel is None:
        raise ValueError("面板形态指向的整方块终端不存在: %s" % (panel_id,))
    return DeviceDefinition(
        part_id,
        label,
        panel.category,
        bandwidth=panel.bandwidth,
        energy=panel.energy,
        capabilities=panel.capabilities,
        form=FORM_PART,
        whole_block_id=panel_id,
        part_kind=PART_KIND_TERMINAL,
        notes="面板形态：挂在末影玻璃光缆或末影光缆的一个面上；带宽、耗能、界面与整方块终端相同。",
    )


#: 四种终端的面板形态（13 §2）。与整方块终端 `*_terminal_panel` **并存**
#: （13 §0.10），显示名 2026-11-10 随作者改的语言文件（与整方块终端同名，不再加「（光缆）」）。面板挂在光缆的一个面上，经同格线芯接入
#: 网络，拓扑上只连宿主光缆；它的物品形态是同名的展示方块（2026-11-10 方案 B，放不下）。
TERMINAL_PARTS = (
    _terminal_part(
        "endernet:common_terminal_part",
        "通用终端面板",
        "endernet:common_terminal_panel",
    ),
    _terminal_part(
        "endernet:crafting_terminal_part",
        "合成终端面板",
        "endernet:crafting_terminal_panel",
    ),
    _terminal_part(
        "endernet:template_terminal_part",
        "样板终端面板",
        "endernet:template_terminal_panel",
    ),
    _terminal_part(
        "endernet:factoric_terminal_part",
        "工厂终端面板",
        "endernet:factoric_terminal_panel",
    ),
)


#: P2P 隧道（2026-11-13，`.claude/17`）。只有面板形态，没有对应的整方块（`whole_block_id`
#: 为 None）。挂在玻璃或普通光缆的一个面上，占宿主线路 1u、2 EN/t；用配置卡设成某个频率的
#: 输入或输出，同频率恰好一个输入时，各端正面那一格经隧道连成一张网（拓扑的远程连边，
#: 每组最多让 `TUNNEL_CAPACITY_QUARTERS` 经过）。目前只有运带宽的网络隧道。
TUNNEL_PARTS = (
    DeviceDefinition(
        TUNNEL_PART_ID,
        "P2P 网络隧道",
        CATEGORY_TUNNEL,
        bandwidth=BandwidthSpec(units_to_quarters(1)),
        energy=EnergySpec(Value(2)),
        form=FORM_PART,
        part_kind=PART_KIND_TUNNEL,
        notes=(
            "面板形态：挂在末影玻璃光缆或末影光缆的一个面上，占宿主线路 1u、2 EN/t；"
            "同频率一入多出，各端正面那一格经隧道连通，每组最多让 32u 经过；不能嵌套。"
        ),
    ),
)

#: 全部面板形态：终端与隧道。物品形态都是同名的展示方块（`gen_blocks.build_part_item_json`）。
PARTS = TERMINAL_PARTS + TUNNEL_PARTS


# -- 工具（13 §2）------------------------------------------------------------

WRENCH_ID = "endernet:network_wrench"
#: 配置卡（2026-11-13，对应 AE2 内存卡；「内存卡」已被内存条系列占用）：潜行右键 P2P 隧道
#: 设为输入并记下频率，右键别的隧道设为同频率的输出，潜行右键别处清空。只存频率。
CONFIG_CARD_ID = "endernet:config_card"
#: 无线连接工具（2026-11-13，对应 ExtendedAE 的无线设置工具）：右键两台无线连接器配对，
#: 潜行右键连接器解除配对，潜行右键别处清空待配对的那一台。
WIRELESS_LINK_TOOL_ID = "endernet:wireless_link_tool"

#: 工具物品规格。网络扳手：切断光缆的某一面、拆单块面板、潜行拆卸（13 §6）；配置卡与
#: 无线连接工具见上。贴图为占位，作者后补。
TOOLS = (
    # 扳手的说明是作者 2026-11-10 在物品 JSON 里定的原文
    ToolSpec(WRENCH_ID, "网络扳手", "用于对终界网络设备进行维护"),
    ToolSpec(
        CONFIG_CARD_ID,
        "配置卡",
        "潜行右键 P2P 隧道设为输入并记下频率，右键其他隧道设为同频率的输出；潜行右键别处清空",
    ),
    ToolSpec(
        WIRELESS_LINK_TOOL_ID,
        "无线连接工具",
        "右键两台无线连接器完成配对；潜行右键连接器解除配对，潜行右键别处清空",
    ),
)

#: **已注册为游戏物品**的工具。与 `ITEM_MEDIUMS` 同一地位、同一理由：规格表与
#: 「已经做成了物品」是两件事。物品 JSON、图标与语言条目由 `scripts/gen_items.py` 生成。
ITEM_TOOLS = (WRENCH_ID, CONFIG_CARD_ID, WIRELESS_LINK_TOOL_ID)


class DefinitionSet(object):
    """设备与介质的运行时索引，外加未定案数值的守卫。

    设备分四张表（见模块说明）：`devices` 整方块、`cables` 光缆、`parts` 面板形态、
    `part_holders` 面板底座。`get` / `has` / `category_of` 跨四张表查——拓扑与准入不关心
    一台设备是哪张表里来的；`devices` 只是整方块那一张，生成器与资产检查按它逐台出方块。
    """

    #: 非设备、非介质的配置项。`iter_values()` 会把它们与设备数值一起报出来，
    #: 因此未定案的配置同样躲不过 `assert_no_silent_unresolved()`——
    #: 不必在别处再维护一份未定项清单。
    DEFAULT_CONFIG = {
        "storage_driver.slot_count": STORAGE_DRIVER_SLOT_COUNT,
        "port.passive_slot_count": PASSIVE_PORT_SLOT_COUNT,
        "port.template_pattern_slot_count": TEMPLATE_PATTERN_SLOT_COUNT,
        "assemble_chamber.slot_count": ASSEMBLE_CHAMBER_SLOT_COUNT,
        "port.item_intake_slot_count": ITEM_INTAKE_PORT_SLOT_COUNT,
        "port.item_output_slot_count": ITEM_OUTPUT_PORT_SLOT_COUNT,
        "port.fluid_slot_count": FLUID_PORT_SLOT_COUNT,
        "memory_slot.slot_count": MEMORY_SLOT_COUNT,
        "port.pickup_filter_limit": PICKUP_FILTER_LIMIT,
        "network.device_passthrough_quarters": DEVICE_PASSTHROUGH_QUARTERS,
        "network.tunnel_capacity_quarters": TUNNEL_CAPACITY_QUARTERS,
        "wireless.max_distance": WIRELESS_MAX_DISTANCE,
    }

    def __init__(
        self,
        devices=None,
        mediums=None,
        config=None,
        item_mediums=None,
        templates=None,
        item_templates=None,
        memories=None,
        item_memories=None,
        cables=None,
        parts=None,
        tools=None,
        item_tools=None,
        part_holders=None,
    ):
        # type: (tuple, tuple, dict, tuple, tuple, tuple, tuple, tuple, tuple, tuple, tuple, tuple) -> None
        self.devices = {}
        #: 末影光缆（方块，由阶段 C 的生成器按面出状态）。
        self.cables = {}
        #: 面板形态的设备（不是方块）。
        self.parts = {}
        #: 面板底座（方块，按面数据与方块状态同光缆一套；2026-11-10）。
        self.part_holders = {}
        #: 三张设备表合起来的索引：`get` / `has` 走它，跨表标识也在这里查重。
        self._by_id = {}
        self.mediums = {}
        self.templates = {}
        self.memories = {}
        self.tools = {}
        self.item_tools = tuple(ITEM_TOOLS if item_tools is None else item_tools)
        self.item_mediums = tuple(
            ITEM_MEDIUMS if item_mediums is None else item_mediums
        )
        self.item_templates = tuple(
            ITEM_TEMPLATES if item_templates is None else item_templates
        )
        self.item_memories = tuple(
            ITEM_MEMORIES if item_memories is None else item_memories
        )
        self.config = dict(self.DEFAULT_CONFIG)
        self.config.update(config or {})
        for definition in devices if devices is not None else DEVICES:
            self._add_unique(self.devices, definition.id, definition, "设备")
            self._add_unique(self._by_id, definition.id, definition, "设备")
        for definition in cables if cables is not None else CABLES:
            if definition.category != CATEGORY_CABLE:
                raise ValueError("光缆表里只能放光缆: %s" % (definition.id,))
            self._add_unique(self.cables, definition.id, definition, "光缆")
            self._add_unique(self._by_id, definition.id, definition, "设备")
        for definition in parts if parts is not None else PARTS:
            if not definition.is_part():
                raise ValueError("面板表里只能放面板形态的设备: %s" % (definition.id,))
            self._add_unique(self.parts, definition.id, definition, "面板")
            self._add_unique(self._by_id, definition.id, definition, "设备")
        for definition in part_holders if part_holders is not None else PART_HOLDERS:
            if definition.category != CATEGORY_PART_HOLDER:
                raise ValueError("面板底座表里只能放面板底座: %s" % (definition.id,))
            self._add_unique(self.part_holders, definition.id, definition, "面板底座")
            self._add_unique(self._by_id, definition.id, definition, "设备")
        for tool in tools if tools is not None else TOOLS:
            self._add_unique(self.tools, tool.id, tool, "工具")
        for spec in mediums if mediums is not None else MEDIUMS:
            self._add_unique(self.mediums, spec.id, spec, "介质")
        for template in templates if templates is not None else PATTERN_TEMPLATES:
            self._add_unique(self.templates, template.id, template, "样板")
        for memory in memories if memories is not None else MEMORIES:
            self._add_unique(self.memories, memory.id, memory, "内存条")
        # 只在用默认表时校验：测试常传自制的子集来构造夹具，那时
        # 「物品注册表里的型号必须存在」这条约束并不适用（它约束的是
        # 交付出去的这份真实定义表）。
        if mediums is None:
            for medium_id in self.item_mediums:
                if medium_id not in self.mediums:
                    raise ValueError(
                        "已注册为物品的介质型号不在介质表中: %s" % (medium_id,)
                    )
        if templates is None:
            for template_id in self.item_templates:
                if template_id not in self.templates:
                    raise ValueError(
                        "已注册为物品的样板不在样板表中: %s" % (template_id,)
                    )
        if memories is None:
            for memory_id in self.item_memories:
                if memory_id not in self.memories:
                    raise ValueError(
                        "已注册为物品的内存条型号不在内存表中: %s" % (memory_id,)
                    )
        if tools is None:
            for tool_id in self.item_tools:
                if tool_id not in self.tools:
                    raise ValueError("已注册为物品的工具不在工具表中: %s" % (tool_id,))
        # 指明了整方块对应物的面板（终端面板），必须指向一台真实的整方块终端。只在两张表都用
        # 默认值时校验，理由同上：测试常拿一小份自制设备表构造夹具。
        if devices is None and parts is None:
            for definition in self.parts.values():
                if definition.whole_block_id is None:
                    continue
                if definition.whole_block_id not in self.devices:
                    raise ValueError(
                        "面板 %s 指向的整方块设备不存在: %s"
                        % (definition.id, definition.whole_block_id)
                    )

    @staticmethod
    def _add_unique(table, key, item, label):
        if key in table:
            raise ValueError("%s标识重复: %s" % (label, key))
        table[key] = item

    # -- 查询 ----------------------------------------------------------

    def get(self, device_id):
        # type: (str) -> DeviceDefinition
        """任一张设备表里的定义（整方块、光缆、面板）。"""
        try:
            return self._by_id[device_id]
        except KeyError:
            raise KeyError("未注册的设备标识: %s" % (device_id,))

    def has(self, device_id):
        # type: (str) -> bool
        return device_id in self._by_id

    def all_definitions(self):
        # type: () -> list
        """四张设备表的全部定义，按标识排序。"""
        return [self._by_id[device_id] for device_id in sorted(self._by_id)]

    def is_cable(self, device_id):
        # type: (str) -> bool
        return device_id in self.cables

    def is_part(self, device_id):
        # type: (str) -> bool
        return device_id in self.parts

    def is_part_holder(self, device_id):
        # type: (str) -> bool
        return device_id in self.part_holders

    def has_face_data(self, device_id):
        # type: (str) -> bool
        """按面的方块：光缆与面板底座。方块实体带 `endernet:cuts` / `endernet:parts`、方块
        状态是 `endernet:side_<面>`，读写与状态维护都归 `server/devices/cables.py`。"""
        return device_id in self.cables or device_id in self.part_holders

    def accepts_parts(self, device_id):
        # type: (str) -> bool
        """这一格能不能挂面板：玻璃与普通光缆、面板底座能；致密光缆与其它设备一律不能。"""
        definition = self.cables.get(device_id) or self.part_holders.get(device_id)
        return definition is not None and definition.accepts_parts

    def part_kind_of(self, device_id):
        # type: (str) -> object
        """面板的种类（`PART_KINDS`）；不是面板返回 None。"""
        definition = self.parts.get(device_id)
        return definition.part_kind if definition is not None else None

    def terminal_screen_of(self, device_id):
        # type: (str) -> object
        """这台终端打开哪个屏。面板形态用它对应的整方块终端的屏（13 §2）。

        没有屏的设备返回 None。屏幕名的唯一来源仍是 `TERMINAL_SCREENS`。
        """
        definition = self._by_id.get(device_id)
        if definition is None:
            return None
        if definition.is_part():
            return TERMINAL_SCREENS.get(definition.whole_block_id)
        return TERMINAL_SCREENS.get(device_id)

    def device_passthrough_quarters(self):
        # type: () -> object
        """整方块设备的透传容量（四分之一 u），见 `DEVICE_PASSTHROUGH_QUARTERS`。

        缺这一项时返回 `UNCONFIGURED`：未知不等于 0，也不等于不限。
        """
        value = self.config_value("network.device_passthrough_quarters")
        if value is None or value is UNCONFIGURED:
            return UNCONFIGURED
        return int(value)

    def status_values_of(self, device_id):
        # type: (str) -> tuple
        """这台整方块设备的外观状态能取哪几个值：控制器 `(0, 1, 2)`，其余有离线外观的 `(0, 1)`，
        没有离线外观的（`NO_STATUS_LOOK`、光缆、面板、面板底座）为空。面板的离线走面板外观读数，
        不在这里。"""
        if device_id not in self.devices or device_id in NO_STATUS_LOOK:
            return ()
        if self.devices[device_id].category == CATEGORY_CONTROLLER:
            return (STATUS_ONLINE, STATUS_OFFLINE, STATUS_CONFLICT)
        return (STATUS_ONLINE, STATUS_OFFLINE)

    def tunnel_capacity_quarters(self):
        # type: () -> object
        """一个 P2P 隧道频率组能让多少带宽经过（四分之一 u），见 `TUNNEL_CAPACITY_QUARTERS`。"""
        value = self.config_value("network.tunnel_capacity_quarters")
        if value is None or value is UNCONFIGURED:
            return UNCONFIGURED
        return int(value)

    def wireless_max_distance(self):
        # type: () -> float
        """无线连接器配对的最远距离（格），见 `WIRELESS_MAX_DISTANCE`；缺项按 0（谁都连不上）。"""
        value = self.config_value("wireless.max_distance")
        if value is None or value is UNCONFIGURED:
            return 0.0
        return float(value)

    # -- 工具 ----------------------------------------------------------

    def has_tool(self, tool_id):
        # type: (str) -> bool
        return tool_id in self.tools

    def get_tool(self, tool_id):
        # type: (str) -> ToolSpec
        try:
            return self.tools[tool_id]
        except KeyError:
            raise KeyError("未注册的工具标识: %s" % (tool_id,))

    def get_item_tool(self, tool_id):
        # type: (str) -> ToolSpec
        """取**已注册为游戏物品**的工具规格。与 `get_item_medium` 同一分工。"""
        if tool_id not in self.item_tools:
            raise KeyError("工具 %s 尚未注册为游戏物品" % (tool_id,))
        return self.get_tool(tool_id)

    def item_tool_specs(self):
        # type: () -> list
        """已注册为物品的工具规格，顺序与 `item_tools` 一致。"""
        return [self.get_tool(tool_id) for tool_id in self.item_tools]

    def has_medium(self, medium_id):
        # type: (str) -> bool
        return medium_id in self.mediums

    def get_medium(self, medium_id):
        # type: (str) -> MediumSpec
        try:
            return self.mediums[medium_id]
        except KeyError:
            raise KeyError("未注册的介质标识: %s" % (medium_id,))

    def get_item_medium(self, medium_id):
        # type: (str) -> MediumSpec
        """取**已注册为游戏物品**的介质规格。

        与 `get_medium` 分开：规格存在不等于玩家能拿到它（见 `ITEM_MEDIUMS`）。
        """
        if medium_id not in self.item_mediums:
            raise KeyError("介质 %s 尚未注册为游戏物品" % (medium_id,))
        return self.get_medium(medium_id)

    def item_medium_specs(self):
        # type: () -> list
        """已注册为物品的介质规格，顺序与 `item_mediums` 一致。"""
        return [self.get_medium(medium_id) for medium_id in self.item_mediums]

    # -- 样板 ----------------------------------------------------------

    def has_template(self, template_id):
        # type: (str) -> bool
        return template_id in self.templates

    def get_template(self, template_id):
        # type: (str) -> TemplateSpec
        try:
            return self.templates[template_id]
        except KeyError:
            raise KeyError("未注册的样板标识: %s" % (template_id,))

    def get_item_template(self, template_id):
        # type: (str) -> TemplateSpec
        """取**已注册为游戏物品**的样板规格。与 `get_item_medium` 同一分工。"""
        if template_id not in self.item_templates:
            raise KeyError("样板 %s 尚未注册为游戏物品" % (template_id,))
        return self.get_template(template_id)

    def item_template_specs(self):
        # type: () -> list
        """已注册为物品的样板规格，顺序与 `item_templates` 一致。"""
        return [self.get_template(item) for item in self.item_templates]

    # -- 内存条 --------------------------------------------------------

    def has_memory(self, memory_id):
        # type: (str) -> bool
        """这个标识是不是一种**已注册为游戏物品**的内存条。

        领域层（`server/devices/memory_slot.py`）用它判「槽里这根算不算内存」，
        因此**判的是物品在游戏里真的存在**，不是「规格表里提过一嘴」——
        与 `get_item_medium` 的口径一致。写内存条时也按它反查，两处同源。
        """
        return memory_id in self.item_memories

    def get_memory(self, memory_id):
        # type: (str) -> MemorySpec
        try:
            return self.memories[memory_id]
        except KeyError:
            raise KeyError("未注册的内存条标识: %s" % (memory_id,))

    def get_item_memory(self, memory_id):
        # type: (str) -> MemorySpec
        """取**已注册为游戏物品**的内存条规格。与 `get_item_medium` 同一分工。"""
        if memory_id not in self.item_memories:
            raise KeyError("内存条 %s 尚未注册为游戏物品" % (memory_id,))
        return self.get_memory(memory_id)

    def item_memory_specs(self):
        # type: () -> list
        """已注册为物品的内存条规格，顺序与 `item_memories` 一致。"""
        return [self.get_memory(memory_id) for memory_id in self.item_memories]

    def memory_bytes(self, memory_id):
        # type: (str) -> int
        """一根该型号内存条的字节数。**不是内存条型号时返回 0**。

        返回 0 而不是抛异常，是给读容器的那条路径用的：槽里可能是任何东西，
        「不是内存条」是常规情形（拒收只管新放进去的），调用方不该为此写
        try/except。要区分「不存在」请用 `has_memory`。
        """
        spec = self.memories.get(memory_id)
        return 0 if spec is None else spec.capacity_bytes

    def item_specs(self):
        # type: () -> list
        """**全部已注册为游戏物品的规格**（介质 + 样板 + 内存条）。

        资产检查与物品生成器读这一份：两边各维护一张清单的话，新增一类物品
        时总有一处会漏，而漏掉的表现是「游戏里有这件物品、但没人检查它的贴图」
        或者反过来「检查报缺文件、其实这件物品还没做」。
        几种规格都带 `id` 与 `label`，这正是那两处用到的全部字段。面板是展示方块，不在内。
        """
        return (
            self.item_medium_specs()
            + self.item_template_specs()
            + self.item_memory_specs()
            + self.item_tool_specs()
        )

    def item_keys(self):
        # type: () -> tuple
        """全部已注册为游戏物品的标识。"""
        return (
            tuple(self.item_mediums)
            + tuple(self.item_templates)
            + tuple(self.item_memories)
            + tuple(self.item_tools)
        )

    def item_id_for_template_mode(self, mode):
        # type: (str) -> str
        """两种配方模式共用已注册的工厂样板物品。"""
        if mode in PATTERN_MODES and TEMPLATE_PANEL_ITEM_ID in self.item_templates:
            return TEMPLATE_PANEL_ITEM_ID
        return ""

    def template_mode_of(self, template_id):
        # type: (str) -> str
        """样板规格的默认模式；编码件的实际模式必须读取 userData。"""
        spec = self.templates.get(template_id)
        return spec.mode if spec is not None else ""

    def config_value(self, key, default=None):
        # type: (str, object) -> object
        """取配置项的**裸值**（`Value` 解包）。缺失返回 `default`。

        未解包的话调用方会拿到 `Value` 对象，`int()` 时才炸——那时已经离
        出错的地方很远了。
        """
        item = self.config.get(key)
        if item is None:
            return default
        if isinstance(item, Value):
            return item.value
        return item

    def category_of(self, device_id):
        # type: (str) -> str
        return self.get(device_id).category

    @property
    def controller_ids(self):
        # type: () -> frozenset
        """「网络控制器」分组的全部方块类型。

        按显式分类取，**不按名称含 controller 猜测**，也不只认当前的小型型号。
        新增控制器型号时只要分类正确就自动纳入计数校验。
        """
        return frozenset(
            device_id
            for device_id, definition in self.devices.items()
            if definition.category == CATEGORY_CONTROLLER
        )

    def is_controller(self, device_id):
        # type: (str) -> bool
        return self.get(device_id).category == CATEGORY_CONTROLLER

    def devices_in_category(self, category):
        # type: (str) -> frozenset
        """该分类的全部设备标识，三张设备表一起算（终端分类含面板形态）。"""
        return frozenset(
            device_id
            for device_id, definition in self._by_id.items()
            if definition.category == category
        )

    # -- 未定案守卫 ----------------------------------------------------

    @staticmethod
    def _as_value(raw):
        # type: (object) -> Value
        """统一成 Value，便于诊断层区分「未配置」与「已定案」。"""
        if isinstance(raw, Value):
            return raw
        return Value(raw)

    def iter_values(self):
        # type: () -> list
        """产出 (路径, Value) 供守卫与诊断使用。"""
        out = []
        for device_id in sorted(self._by_id):
            definition = self._by_id[device_id]
            energy = definition.energy
            for state, raw in (("idle", energy.idle), ("work", energy.work)):
                out.append(
                    ("%s.energy.%s" % (device_id, state), self._as_value(raw))
                )
            if energy.follows_bandwidth:
                # 能耗由负载域推导，不是一个可直接交付的常数。
                out.append(
                    (
                        "%s.energy.follows_bandwidth" % device_id,
                        Value(
                            UNCONFIGURED,
                            unresolved="R08",
                            is_test_value=True,
                            note="能耗跟随基带负载，口径待定",
                        ),
                    )
                )
            if energy.per_unit is not None:
                out.append(
                    (
                        "%s.energy.per_unit" % device_id,
                        self._as_value(energy.per_unit),
                    )
                )
            if definition.supply is not None:
                # 供能速率同样要过守卫：没登记的话，一个「带未定标签却不是测试值」
                # 的供能速率会**恰好**落在 assert_no_silent_unresolved() 的覆盖
                # 范围之外——那正是这个守卫存在的理由。不供能的设备（supply is None）
                # 不产出路径，免得把 23 台设备的「不供能」都报成缺项。
                out.append(
                    (
                        "%s.supply" % device_id,
                        self._as_value(definition.supply),
                    )
                )
            out.append(
                (
                    "%s.bandwidth.base" % device_id,
                    self._as_value(definition.bandwidth.base_quarters),
                )
            )
            if definition.bandwidth.capacity_quarters is not None:
                # 载体容量（基带的外露面、光缆）与需求量分开登记，缺项同样要能被诊断看见。
                out.append(
                    (
                        "%s.bandwidth.capacity" % device_id,
                        self._as_value(definition.bandwidth.capacity_quarters),
                    )
                )
        for key in sorted(self.config):
            if isinstance(self.config[key], Value):
                out.append(("config.%s" % key, self.config[key]))
        return out

    def unresolved_report(self):
        # type: () -> list
        """列出所有仍带未定标签的数值。这是诊断，不是错误。"""
        return [
            (path, value)
            for path, value in self.iter_values()
            if value is not None and value.unresolved is not None
        ]

    def silent_resolutions(self):
        # type: () -> list
        """列出「带着未定标签、却不是测试值」的数值——即悄悄定案的玩法。"""
        return [
            (path, value)
            for path, value in self.unresolved_report()
            if not value.is_test_value
        ]

    def unconfigured_report(self):
        # type: () -> list
        """列出 `plans/` 中缺失的数值（R10）。"""
        out = []
        for path, value in self.iter_values():
            if value is not None and value.value is UNCONFIGURED:
                out.append((path, value))
        return out

    def assert_no_silent_unresolved(self):
        # type: () -> None
        """构建时守卫：存在静默定案的玩法数值就报错。

        这不是「必须没有未定项」——规划阶段本来就该有未定项；
        它拒绝的是「未定项被填了一个具体的生产值，却没有标注是测试替身」。
        """
        bad = self.silent_resolutions()
        if bad:
            raise AssertionError(
                "以下数值带着未定规则标签，却未被标记为测试替身，"
                "属于悄悄定案：\n  "
                + "\n  ".join("%s -> %r" % (path, value) for path, value in bad)
            )
