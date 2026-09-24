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
"""


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


# 带宽取整策略：`(硬盘个数/4)u` 到底进到多少（R02）
class BandwidthRounding(object):
    EXACT_QUARTERS = "exact_quarters"  # 默认：以 1/4u 整数记账，无需取整
    CEIL_TO_U = "ceil_to_u"
    FLOOR_TO_U = "floor_to_u"


# -- 设备分类 --------------------------------------------------------------

CATEGORY_CONTROLLER = "controller"
CATEGORY_BASEBAND = "baseband"
CATEGORY_BUS = "bus"
CATEGORY_TERMINAL = "terminal"
CATEGORY_STORAGE = "storage"
CATEGORY_PROCESSOR_CONNECTOR = "processor_connector"
CATEGORY_PROCESSOR = "processor"
CATEGORY_MEMORY = "memory"
CATEGORY_ASSEMBLY = "assembly"
CATEGORY_ENERGY_INPUT = "energy_input"

# -- 能力名（与 devices/capabilities.py 的协议一一对应）--------------------

CAP_STORAGE_PROVIDER = "storage_provider"
CAP_PATTERN_PROVIDER = "pattern_provider"
CAP_ITEM_PORT = "item_port"
CAP_FLUID_PORT = "fluid_port"
CAP_ENERGY_PORT = "energy_port"
CAP_TERMINAL_ENDPOINT = "terminal_endpoint"
CAP_PROCESSING_SLOTS = "processing_slots"
CAP_MEMORY = "memory"

#: 1 单位带宽记为 4 个「四分之一 u」。
QUARTERS_PER_UNIT = 4


def units_to_quarters(units):
    # type: (int) -> int
    """带宽单位 u 换算成四分之一 u。"""
    return int(units) * QUARTERS_PER_UNIT


# -- 连接规则 --------------------------------------------------------------


class ConnectionRules(object):
    """显式的端口白名单。

    相邻**不等于**连通：只有双方类别都在白名单里才建立图边。
    这条规则直接决定拓扑服务的建图，不能靠「是不是贴在一起」推断。
    """

    #: 允许互相连接的分类对。意图上是对称的，校验时双向都要成立。
    ALLOWED_PAIRS = frozenset(
        [
            (CATEGORY_BASEBAND, CATEGORY_CONTROLLER),
            (CATEGORY_BASEBAND, CATEGORY_BASEBAND),
            (CATEGORY_BASEBAND, CATEGORY_BUS),
            (CATEGORY_BASEBAND, CATEGORY_TERMINAL),
            (CATEGORY_BASEBAND, CATEGORY_STORAGE),
            (CATEGORY_BASEBAND, CATEGORY_MEMORY),
            (CATEGORY_BASEBAND, CATEGORY_ASSEMBLY),
            (CATEGORY_BASEBAND, CATEGORY_ENERGY_INPUT),
            (CATEGORY_BASEBAND, CATEGORY_PROCESSOR_CONNECTOR),
            (CATEGORY_PROCESSOR_CONNECTOR, CATEGORY_PROCESSOR),
            (CATEGORY_PROCESSOR, CATEGORY_PROCESSOR),
        ]
    )

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
        if category_a == CATEGORY_PROCESSOR and category_b in (
            CATEGORY_BASEBAND,
            CATEGORY_CONTROLLER,
        ):
            return "processor_must_use_connector"
        if category_b == CATEGORY_PROCESSOR and category_a in (
            CATEGORY_BASEBAND,
            CATEGORY_CONTROLLER,
        ):
            return "processor_must_use_connector"
        if category_a == CATEGORY_PROCESSOR_CONNECTOR and category_b not in (
            CATEGORY_PROCESSOR,
            CATEGORY_BASEBAND,
        ):
            return "connector_only_accepts_processor_or_baseband"
        if category_b == CATEGORY_PROCESSOR_CONNECTOR and category_a not in (
            CATEGORY_PROCESSOR,
            CATEGORY_BASEBAND,
        ):
            return "connector_only_accepts_processor_or_baseband"
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
    - `capacity_quarters`：**只有网络基带使用**。blocks.md 写明
      「可以接受最多 8/16/32u 的网络负载」，这是基带的承载容量，
      与设备自身的需求量是两回事，因此单独一个字段而不是复用 `base_quarters`。
      为 None 表示该设备不承载负载（全部非基带设备）。

    混合 8/16/32 位基带的容量合成、环路与多接入的路径选择仍待定（R02），
    由 `rounding` 与准入策略承担，不在这里悄悄选一个。
    """

    __slots__ = (
        "base_quarters",
        "per_disk_quarters",
        "per_memory_quarters",
        "capacity_quarters",
        "rounding",
    )

    def __init__(
        self,
        base_quarters,
        per_disk_quarters=None,
        per_memory_quarters=None,
        capacity_quarters=None,
        rounding=BandwidthRounding.EXACT_QUARTERS,
    ):
        # type: (object, object, object, object, str) -> None
        self.base_quarters = base_quarters
        self.per_disk_quarters = per_disk_quarters
        self.per_memory_quarters = per_memory_quarters
        self.capacity_quarters = capacity_quarters
        self.rounding = rounding

    def is_carrier(self):
        # type: () -> bool
        """该设备是否承载网络负载（即是否为网络基带）。"""
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
    """一种接入设备的静态定义。"""

    __slots__ = (
        "id",
        "label",
        "category",
        "bandwidth",
        "energy",
        "capabilities",
        "allowed_device_ids",
        "max_stack_slots",
        "disabled_reason",
        "notes",
    )

    def __init__(
        self,
        device_id,
        label,
        category,
        bandwidth=None,
        energy=None,
        capabilities=(),
        allowed_device_ids=None,
        max_stack_slots=None,
        disabled_reason=None,
        notes="",
    ):
        self.id = device_id
        self.label = label
        self.category = category
        self.bandwidth = bandwidth if bandwidth is not None else BandwidthSpec(UNCONFIGURED)
        self.energy = energy if energy is not None else EnergySpec()
        self.capabilities = tuple(capabilities)
        #: 仅控制器使用：允许接入的设备 id 集合。None 表示 blocks.md 未作限定。
        self.allowed_device_ids = (
            None if allowed_device_ids is None else frozenset(allowed_device_ids)
        )
        self.max_stack_slots = max_stack_slots
        self.disabled_reason = disabled_reason
        self.notes = notes

    def has_capability(self, name):
        # type: (str) -> bool
        return name in self.capabilities

    def allows(self, other_device_id):
        # type: (str) -> bool
        """该控制器是否允许接入指定设备。未限定时一律允许。"""
        if self.allowed_device_ids is None:
            return True
        return other_device_id in self.allowed_device_ids

    def __repr__(self):
        return "DeviceDefinition(%s, %s)" % (self.id, self.category)


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


#: 按装置数量计费的规则。基础能耗为 0 是确定的（没有硬盘就不耗能），
#: 这里给出的是每增加一个单元的增量，供定义表引用与诊断。
PER_DISK_ENERGY = Value(1, note="存储驱动器：每块硬盘 1 EN/t（blocks.md）")
PER_MEMORY_ENERGY = Value(8, note="内存槽：每根内存条 8 EN/t（blocks.md）")


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
        notes="不允许自动存货/取货；不支持订单处理。",
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
        notes="允许接入总线类设备；不支持订单处理。",
    ),
    # --- 网络传输介质 ---
    DeviceDefinition(
        "endernet:network_baseband_8",
        "8位网络基带",
        CATEGORY_BASEBAND,
        # 基带自身不消耗带宽，而是提供容量；`base_quarters` 保持未配置
        # 以免被误读成「基带要占带宽」，容量写在 capacity_quarters。
        bandwidth=BandwidthSpec(UNCONFIGURED, capacity_quarters=units_to_quarters(8)),
        energy=EnergySpec(),
        notes="可接受最多 8u 网络负载。",
    ),
    DeviceDefinition(
        "endernet:network_baseband_16",
        "16位网络基带",
        CATEGORY_BASEBAND,
        bandwidth=BandwidthSpec(UNCONFIGURED, capacity_quarters=units_to_quarters(16)),
        energy=EnergySpec(),
        notes="可接受最多 16u 网络负载。",
    ),
    DeviceDefinition(
        "endernet:network_baseband_32",
        "32位网络基带",
        CATEGORY_BASEBAND,
        bandwidth=BandwidthSpec(UNCONFIGURED, capacity_quarters=units_to_quarters(32)),
        energy=EnergySpec(),
        notes="可接受最多 32u 网络负载。",
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
    DeviceDefinition(
        "endernet:energy_input_interface",
        "能源接入口",
        CATEGORY_ENERGY_INPUT,
        bandwidth=BandwidthSpec(0),  # blocks.md：不占用带宽
        energy=EnergySpec(),
        capabilities=(CAP_ENERGY_PORT,),
        notes="不占带宽不代表耗能为零；耗能未列，归入 R10。",
    ),
)


class DefinitionSet(object):
    """设备与介质的运行时索引，外加未定案数值的守卫。"""

    def __init__(self, devices=None, mediums=None, config=None):
        # type: (tuple, tuple, dict) -> None
        self.devices = {}
        self.mediums = {}
        self.config = dict(config or {})
        for definition in devices if devices is not None else DEVICES:
            self._add_unique(self.devices, definition.id, definition, "设备")
        for spec in mediums if mediums is not None else MEDIUMS:
            self._add_unique(self.mediums, spec.id, spec, "介质")

    @staticmethod
    def _add_unique(table, key, item, label):
        if key in table:
            raise ValueError("%s标识重复: %s" % (label, key))
        table[key] = item

    # -- 查询 ----------------------------------------------------------

    def get(self, device_id):
        # type: (str) -> DeviceDefinition
        try:
            return self.devices[device_id]
        except KeyError:
            raise KeyError("未注册的设备标识: %s" % (device_id,))

    def has(self, device_id):
        # type: (str) -> bool
        return device_id in self.devices

    def get_medium(self, medium_id):
        # type: (str) -> MediumSpec
        try:
            return self.mediums[medium_id]
        except KeyError:
            raise KeyError("未注册的介质标识: %s" % (medium_id,))

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
        return frozenset(
            device_id
            for device_id, definition in self.devices.items()
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
        for device_id in sorted(self.devices):
            definition = self.devices[device_id]
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
            out.append(
                (
                    "%s.bandwidth.base" % device_id,
                    self._as_value(definition.bandwidth.base_quarters),
                )
            )
            if definition.bandwidth.capacity_quarters is not None:
                # 承载容量（基带）与需求量分开登记，缺项同样要能被诊断看见。
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
