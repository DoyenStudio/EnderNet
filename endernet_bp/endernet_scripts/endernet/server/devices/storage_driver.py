# coding=utf-8
"""存储驱动器设备：槽位状态、介质归属与真实的存储提供者实现。

对应任务 05-04 的驱动器侧。

## 「哪个介质在哪个槽」的唯一权威是方块容器

驱动器方块有 10 格 `netease:block_container`，玩家把介质**物品**放进去、
拿出来。本模块按位置读那个容器，把认出来的物品挂到槽位上——
**不再有一份写进存档的槽位表**。

这在 2026-10-04 之前是相反的：那时 `SlotAssignment` 随存档走（12-03），
因为当时挂载只能由 `mount` 命令凭空发号。真机读出「方块容器内容随方块
实体落盘」（`persistence_backend.md` §4.1）之后，再存一份就是两份真相——
玩家在区块没加载时拔走一块盘，存档还挂着它、网络照旧记账。

介质**内容**仍然不在物品里（`persistence_backend.md` §2.2）：物品只携带
载体 `userData["endernet:medium"] = {"id", "v"}`（`common/medium_item.py`），
内容按介质 ID 存世界 ExtraData。因此本层的记录**只剩介质表一份**。

读不到容器时**保留上一次的读数**（区块卸载不等于盘被拔了）。变更检测与
`memory_slot.py` / `crafting/store.py` 同一套：容器事件只当脏标记，去抖后重读。

三条仍然成立的性质：

1. **状态跟随设备生命周期，但不跟随网络。** 驱动器状态由启动层持有，
   网络断开、主控移除、运行时重建都不等于设备和介质消失。
2. **槽位（读数）是权威，`disk_count` 是投影。** 带宽准入读的是
   `DeviceRecord.config["disk_count"]`，它**只能**由 `project()` 写入；
   任何命令载荷都改不了它。
3. **介质对象从不被删除。** 拔盘只让引用从容器读数里消失；驱动器被拆
   也**不**把介质标成孤儿——归属是那件物品本身（见 `StorageDriverStore.drop`）。
"""

from ...common import medium_item
from ...common.definitions import (
    CATEGORY_STORAGE,
    STORAGE_DRIVER_SLOT_COUNT,
    UNCONFIGURED,
)
from ...common.identity import DeviceRef, NodeKey, node_key_text  # noqa: F401
from ...common.item_codec import (
    is_empty_item,
    item_dict_from_key,
    item_key_from_dict,
)
from ...common.protocol import (
    ContainerCapabilities,
    Reason,
    TransferReceipt,
    TransferStatus,
)
from ...common.resource_key import ResourceKey
from ..persistence.repository import Record
from ..runtime.lifecycle import ChangeKind
from ..storage.medium import (
    DIRECTION_READ,
    DIRECTION_WRITE,
    Medium,
    MediumPolicy,
    MediumRecovery,
    TransferBudget,
)
from .capabilities import STORAGE_PROVIDER, CapabilityRegistry  # noqa: F401


#: 本模块负责的设备标识。
DRIVER_DEVICE_ID = "endernet:storage_driver"

#: 驱动器存档结构版本。**已停止使用**：槽位不再进存档（见模块 docstring）。
DRIVER_SCHEMA_VERSION = 1

#: 驱动器记录类型。**只用于读取与删除旧存档**——本轮起不再写出。
#: 旧记录里是「存档时期的槽位引用」，读回来只报进诊断并删掉，
#: 不据此造物品（那些介质本来就没有对应物品，见 `restore_from_repository`）。
DRIVER_RECORD_KIND = "endernet.storage_driver"
MEDIUM_RECORD_KIND = "endernet.storage_medium"

# -- 重读节拍 --------------------------------------------------------------

#: 收到变更之后再静默这么多帧才重读（一次拖入会派发多条事件、顺序不定）。
#: 与 `memory_slot.MEMORY_SETTLE_FRAMES`、`crafting/store.PATTERN_SETTLE_FRAMES`
#: **是三条独立的节拍**：常量各写一份，不跨层 import（会成环）。
DRIVER_SETTLE_FRAMES = 2

#: 一台驱动器最多这么久没被重读过，**即使变更一直不停**。
#: 与内存槽同一量级（40 帧 ≈ 1.3 秒 @30Hz）。**不是玩法数值。**
DRIVER_MAX_STALE_FRAMES = 40

#: 每帧最多重读几台驱动器。重读要读满 10 个槽，是真实的引擎调用。
#: **照内存槽量级取的未测量值**（规模验收归 14）。
DRIVER_REFRESH_BUDGET = 4

#: 每帧最多刷新几件介质物品的展示行（容量 Lore）。写回是写引擎容器，
#: 比读贵，因此预算比重读还小。
DRIVER_DISPLAY_BUDGET = 2

#: 同一 (位置, 槽) 上「载体写回」最多重试几次。超过只报诊断，
#: 不再对一块写不进去的槽每帧重试。
DRIVER_PENDING_MAX_ATTEMPTS = 8

#: 「硬盘个数」（blocks.md 的带宽与能耗计费依据）的计入口径（R10）。
#: 取「装了东西的槽位数」——驱动器装的是磁带/软盘/硬盘，把软盘排除在外
#: 会让带宽与能耗被低估。
DISK_SCOPE_OCCUPIED = "occupied_slots"
DISK_SCOPE_HARDDISKS_ONLY = "harddisk_models_only"

#: `DISK_SCOPE_HARDDISKS_ONLY` 时认作硬盘的型号前缀。
HARDDISK_PREFIX = "endernet:harddisk"

#: 介质 ID 的形状。**改它等于改存档**：ID 是介质记录的主键，
#: 也是物品载体 `userData["endernet:medium"]` 里携带的那一串。
MEDIUM_ID_PREFIX = "medium:"

try:  # pragma: no cover - 仅在 Python 2 下成立
    _TEXT_TYPES = (str, unicode)
except NameError:  # pragma: no cover - 仅在 Python 3 下成立
    _TEXT_TYPES = (str,)


def medium_sequence(medium_id):
    # type: (object) -> int
    """从 `medium:<n>` 里取出序号。形状不符返回 0。

    「从存档推导持久 ID」靠的就是它：已接管的最大序号 + 1 就是下一个号。
    读不出的（别的模组写的、人工改过的）一律算 0，不参与抬高水位。
    """
    if not isinstance(medium_id, _TEXT_TYPES) or not medium_id.startswith(MEDIUM_ID_PREFIX):
        return 0
    try:
        return int(medium_id[len(MEDIUM_ID_PREFIX):])
    except (TypeError, ValueError):
        return 0


def is_storage_driver(device_id, definitions=None):
    # type: (str, object) -> bool
    """这台设备是不是存储驱动器。

    **按分类判定，不按设备名**：分类的真相只有定义表一处。定义表缺失
    （离线夹具）时退到「标识就叫 `endernet:storage_driver`」——
    与 `memory_slot.is_memory_slot` 同一形状。
    """
    if definitions is not None:
        definition = definitions.devices.get(device_id)
        if definition is not None:
            return definition.category == CATEGORY_STORAGE
    return device_id == DRIVER_DEVICE_ID


def _slot_count_of(adapter, fallback):
    # type: (object, int) -> int
    """一个容器适配器有多少格。

    **两个方法名都要试**：`ContainerAdapter.slots_count()` 是通用只读面
    （假容器实现它），`SlotBackedContainer.slot_count()` 是按槽读写面
    （引擎容器实现它）。拿不到时退回定义表那条已定案的 10——它同时就是
    `gen_blocks.py` 写进方块容器的 `container_size`，因此不可能与真实格数分叉。
    （与 `memory_slot._slot_count_of` 是同一条规则的两份副本，跨层 import 会成环。）
    """
    for name in ("slot_count", "slots_count"):
        counter = getattr(adapter, name, None)
        if not callable(counter):
            continue
        size = counter()
        if size is None:
            continue
        try:
            size = int(size)
        except (TypeError, ValueError):
            continue
        if size > 0:
            return size
    return int(fallback)


class DriverPolicy(object):
    """驱动器的未定案旋钮（R10）。

    与 `MediumPolicy` 分开：那边管一块盘自身的读写规则，这边管
    「这台设备接受什么、怎么计数」。两条规则的定案时间与归属任务都不同。

    **定义表里有某个型号，不等于这个驱动器接受它**（架构 §5.1）——
    因此 `accepted_models` 是独立的一份，不直接读 `MEDIUMS`。

    `assert_settled()` 只应在正式验收时调用。
    """

    __slots__ = (
        "explicit_slot_count",
        "accepted_models",
        "disk_count_scope",
        "recover_slots_on_reload",
    )

    def __init__(
        self,
        slot_count=None,
        accepted_models=None,
        disk_count_scope=DISK_SCOPE_OCCUPIED,
        recover_slots_on_reload=True,
    ):
        # type: (int, tuple, str, bool) -> None
        #: 显式覆盖槽数（测试用）。None 表示取定义表里的那一份。
        self.explicit_slot_count = None if slot_count is None else int(slot_count)
        if self.explicit_slot_count is not None and self.explicit_slot_count <= 0:
            raise ValueError("槽数必须为正，得到 %r" % (slot_count,))
        #: 接受的介质型号。None 表示「全部已注册型号」——这是替身取值，
        #: 真值要等 R10 定案。
        self.accepted_models = (
            None if accepted_models is None else frozenset(accepted_models)
        )
        self.disk_count_scope = disk_count_scope
        #: **结构性作废（2026-10-04），字段保留、无代码路径。**
        #: 槽位权威改成方块容器读数之后，「区块重载后要不要按持久身份恢复槽位」
        #: 这个问题不再存在——重读容器就是恢复，没有第二份可选的来源。
        #: 按 S-09 的先例**保留字段并加注**，不静默删除：删掉它是玩法层面的
        #: 表态，而 R10 是否还需要一个「重载后怎么读」的旋钮没有定案。
        self.recover_slots_on_reload = bool(recover_slots_on_reload)

        if disk_count_scope not in (DISK_SCOPE_OCCUPIED, DISK_SCOPE_HARDDISKS_ONLY):
            raise ValueError("未知的硬盘计数口径: %r" % (disk_count_scope,))

    def resolve_slot_count(self, definitions):
        # type: (object) -> object
        """算出实际槽数。返回 int，或 `UNCONFIGURED`（定义缺项）。

        **缺项不当作 0。** 返回 `UNCONFIGURED` 让上层报「未配置」——
        悄悄按 0 处理会让一台没配置槽数的驱动器看起来只是「装不下东西」。
        """
        if self.explicit_slot_count is not None:
            return self.explicit_slot_count
        value = definitions.config_value("storage_driver.slot_count")
        if value is None or value is UNCONFIGURED:
            return UNCONFIGURED
        return int(value)

    def accepts_model(self, model_id):
        # type: (str) -> bool
        if self.accepted_models is None:
            return True
        return model_id in self.accepted_models

    def counts_as_disk(self, spec):
        # type: (object) -> bool
        """该介质的规格是否计入「硬盘个数」。

        `spec` 为 None（介质型号查不到）时**计入**：宁可多算需求，
        也不要在信息不全时少算带宽。
        """
        if self.disk_count_scope == DISK_SCOPE_HARDDISKS_ONLY:
            return spec is None or spec.id.startswith(HARDDISK_PREFIX)
        return True

    def rejected_model_reason(self, model_id):
        # type: (str) -> object
        """型号不被接受时的原因（R10 未定案时说清楚是替身规则拒的）。"""
        return Reason.NOT_A_MEDIUM, "本驱动器不接受介质型号 %s（R10 接受范围未定案）" % (
            model_id,
        )

    def unresolved_report(self):
        # type: () -> list
        return [
            {
                "field": "driver.accepted_models",
                "rule": "R10",
                "value": (
                    "全部已注册型号"
                    if self.accepted_models is None
                    else sorted(self.accepted_models)
                ),
                "note": "驱动器接受哪些介质型号；定义表里有型号不等于它接受",
            },
            {
                "field": "driver.disk_count_scope",
                "rule": "R10",
                "value": self.disk_count_scope,
                "note": "blocks.md 的「硬盘个数」计入哪些槽位；替身取全部已占用槽位",
            },
            {
                "field": "driver.recover_slots_on_reload",
                "rule": "05/12",
                "value": self.recover_slots_on_reload,
                "note": "**结构性作废**：槽位权威已是方块容器读数，重读即恢复，"
                        "没有第二份来源可选（2026-10-04）",
            },
        ]

    def assert_settled(self):
        # type: () -> None
        pending = self.unresolved_report()
        if pending:
            raise AssertionError(
                "驱动器策略仍有 %d 项未定案，不能作为正式玩法交付：\n  " % (len(pending),)
                + "\n  ".join(
                    "%s（规则 %s，当前值 %r）"
                    % (item["field"], item["rule"], item["value"])
                    for item in pending
                )
            )

    def describe(self):
        # type: () -> dict
        return {
            "explicit_slot_count": self.explicit_slot_count,
            "accepted_models": (
                None if self.accepted_models is None else sorted(self.accepted_models)
            ),
            "disk_count_scope": self.disk_count_scope,
            "recover_slots_on_reload": self.recover_slots_on_reload,
        }


class SlotAssignment(object):
    """一个槽位里装着什么。空槽用 `None` 表示，不用一个空对象。

    **这是派生值，不是存档**：每次重读方块容器重建一份。`from_dict` 只为
    解析**上一版留下的旧存档记录**（读回来只报进诊断，见 `restore_from_repository`）。
    """

    __slots__ = ("slot", "medium_id", "slot_version", "bound_tick")

    def __init__(self, slot, medium_id, slot_version=1, bound_tick=0):
        # type: (int, str, int, int) -> None
        self.slot = int(slot)
        self.medium_id = medium_id
        #: 该槽位第几次被占用。用于拒绝「基于旧槽位状态」的请求。
        self.slot_version = int(slot_version)
        self.bound_tick = int(bound_tick)

    def to_dict(self):
        # type: () -> dict
        return {
            "slot": self.slot,
            "medium_id": self.medium_id,
            "slot_version": self.slot_version,
            "bound_tick": self.bound_tick,
        }

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> SlotAssignment
        return cls(
            data["slot"],
            data["medium_id"],
            data.get("slot_version", 1),
            data.get("bound_tick", 0),
        )

    def __repr__(self):
        return "SlotAssignment(%d, %s, v%d)" % (
            self.slot,
            self.medium_id,
            self.slot_version,
        )


class DriverState(object):
    """一台驱动器的专属状态。

    刻意**不含**位置索引、区块索引或朝向：那些是 `DeviceRegistry` 的事，
    在这里再存一份就是第二个设备注册表。

    `slots` 是**方块容器读数的派生表**（见模块 docstring），因此它由
    `StorageDriverStore._read_slots` 整体重建，没有任何写槽方法。
    """

    __slots__ = (
        "ref",
        "node_key",
        "device_id",
        "slot_count",
        "slots",
        "invalid",
        "config_version",
        "loaded",
        "recovery",
        "recovery_detail",
        "unreadable",
        "dirty_tick",
        "refresh_tick",
        "readings",
        "revision",
        "stats",
        "capabilities",
        "provider",
        "policy",
    )

    def __init__(self, ref, policy, store, device_id=DRIVER_DEVICE_ID, slot_count=0):
        # type: (object, DriverPolicy, object, str, object) -> None
        self.ref = ref
        self.node_key = ref.node_key
        self.device_id = device_id
        self.slot_count = slot_count
        self.slots = [None] * (slot_count if isinstance(slot_count, int) else 0)
        #: `slot -> {"code", "detail"}`。槽里有东西但**挂不上**：不是介质、
        #: 载体读不动、载体版本不认识、写回没核对上。**与「空槽」分开**。
        self.invalid = {}
        #: 槽位配置版本。每次读数里有净变化就推进它，供请求做旧版本拒绝。
        self.config_version = 0
        self.loaded = True
        #: 恢复状态。`UNCONFIGURED` 槽数也记在这里。
        self.recovery = "ok"
        self.recovery_detail = ""
        #: 读不到容器的原因（空串表示读得到）。**不清空 `slots`**。
        self.unreadable = ""
        #: 最后一次收到变更的帧号。**-1 表示从没收到过**（逻辑时钟可以是 0）。
        self.dirty_tick = -1
        #: 最后一次尝试重读的帧号。-1 表示从没试过。**它是「试过」不是「读到」**。
        self.refresh_tick = -1
        #: **成功读到过几次。** 与 `refresh_tick` 分开——「从没读到过」与
        #: 「读到过 0 块盘」是两件事（同 `MemorySlotState`）。
        self.readings = 0
        #: 内容版本，每次重读发现变化就 +1。
        self.revision = 0
        self.stats = {
            "refreshes": 0,
            "changes": 0,
            "unreadable": 0,
            "invalid_slots": 0,
            "forced_refreshes": 0,
            "errors": 0,
        }
        #: 挂在这台设备上的**实际能力对象**（架构 §4 要求可查）。
        self.capabilities = CapabilityRegistry()
        self.provider = StorageDriverProvider(self, store)
        self.capabilities.register(STORAGE_PROVIDER, self.provider)
        self.policy = policy
        if slot_count is UNCONFIGURED:
            self.recovery = Reason.DEFINITION_UNCONFIGURED
            self.recovery_detail = "定义表未给出存储驱动器的槽数（R10）"

    # -- 查询 ----------------------------------------------------------

    def is_usable(self):
        # type: () -> bool
        """定义表配得出槽数才谈得上装载。"""
        return self.recovery == "ok"

    def has_reading(self):
        # type: () -> bool
        """**成功**读到过方块容器没有。

        不等于 `refresh_tick >= 0`（那是「试过」），也不等于当前读得到。
        """
        return self.readings > 0

    def content_fingerprint(self):
        # type: () -> tuple
        """内容指纹，用于判断「这次重读有没有读出不一样的东西」。

        不含 `loaded` / `unreadable` / 帧号：那些是设备状态，不是容器内容。
        """
        return (
            tuple(
                (item.slot, item.medium_id, item.slot_version)
                for item in self.occupied()
            ),
            tuple(
                (slot, self.invalid[slot]["code"]) for slot in sorted(self.invalid)
            ),
        )

    def occupied(self):
        # type: () -> list
        return [item for item in self.slots if item is not None]

    def occupied_count(self):
        # type: () -> int
        return len(self.occupied())

    def medium_ids(self):
        # type: () -> tuple
        return tuple(item.medium_id for item in self.occupied())

    def slot_of(self, medium_id):
        # type: (str) -> object
        """该介质在哪个槽位。不在返回 None。"""
        for item in self.occupied():
            if item.medium_id == medium_id:
                return item.slot
        return None

    def assignment(self, slot):
        # type: (int) -> SlotAssignment
        if not isinstance(slot, int) or isinstance(slot, bool):
            return None
        if slot < 0 or slot >= len(self.slots):
            return None
        return self.slots[slot]

    def slot_view(self, slot):
        # type: (int) -> dict
        item = self.assignment(slot)
        if item is None:
            return {
                "slot": slot,
                "medium_id": None,
                "slot_version": 0,
                "bound_tick": 0,
                "state": "empty" if self.is_usable() else self.recovery,
            }
        view = item.to_dict()
        view["state"] = "occupied"
        return view

    def slot_views(self):
        # type: () -> list
        return [self.slot_view(index) for index in range(len(self.slots))]

    def disk_count(self, medium_of):
        # type: (object) -> int
        """按 `DriverPolicy.disk_count_scope` 计出的「硬盘个数」。"""
        count = 0
        for item in self.occupied():
            medium = medium_of(item.medium_id)
            spec = medium.spec if medium is not None else None
            if self.policy.counts_as_disk(spec):
                count += 1
        return count

    def find(self, medium_id):
        # type: (str) -> object
        """该介质在本驱动器上的槽位分配，不在则 None。"""
        for item in self.occupied():
            if item.medium_id == medium_id:
                return item
        return None

    # -- 读数重建（由 StorageDriverStore 调用）-------------------------

    def replace_slots(self, slots, invalid, slot_count=None, tick=0):
        # type: (dict, dict, int, int) -> bool
        """用一份新的容器读数替换槽位表。返回**是否有净变化**。

        `slots` 是 `slot -> medium_id`，`invalid` 是 `slot -> {"code","detail"}`。
        `slot_version` 在这里维护：**同槽同盘沿用旧版本号**（不因为多读一次
        就推进版本），换了盘或由空变占用则 +1。空槽对外报 0——与旧行为一致，
        且让「基于占用快照的过期请求」在多一次重读之后依然对得上。

        整体替换而不是原地改：半份读数会让剩下的槽看起来是空的
        （`_read_slots` 中途失败时已经整体作废，这里是最后一道）。
        """
        before = self.content_fingerprint()
        size = slot_count if isinstance(slot_count, int) and slot_count > 0 else len(self.slots)
        previous = self.slots
        new_slots = [None] * size
        for slot in sorted(slots):
            if slot < 0 or slot >= size:
                continue
            old = previous[slot] if slot < len(previous) else None
            if old is not None and old.medium_id == slots[slot]:
                new_slots[slot] = old
            else:
                version = old.slot_version if old is not None else 0
                new_slots[slot] = SlotAssignment(slot, slots[slot], version + 1, tick)
        self.slot_count = size
        self.slots = new_slots
        self.invalid = dict(invalid)
        self.stats["invalid_slots"] = len(self.invalid)
        changed = size != len(previous) or self.content_fingerprint() != before
        if changed:
            self.config_version += 1
        return changed

    def evict_slot(self, slot, code, detail):
        # type: (int, str, str) -> bool
        """把一个槽位从**读数**里摘掉，并记下原因。**容器里那件物品不碰。**

        只有跨驱动器去重用它：那块盘还在别人的驱动器里，玩家自己取出来。
        """
        if slot < 0 or slot >= len(self.slots) or self.slots[slot] is None:
            return False
        self.slots[slot] = None
        self.invalid[slot] = {"code": code, "detail": detail}
        self.stats["invalid_slots"] = len(self.invalid)
        self.config_version += 1
        return True

    # -- 序列化（仅供诊断；槽位不再进存档）-------------------------------

    def to_dict(self):
        # type: () -> dict
        return {
            "node_key": self.node_key.as_key(),
            "generation": self.ref.generation,
            "device_id": self.device_id,
            "slot_count": (
                self.slot_count if isinstance(self.slot_count, int) else None
            ),
            "config_version": self.config_version,
            "loaded": self.loaded,
            "readings": self.readings,
            "revision": self.revision,
            "unreadable": self.unreadable,
            "slots": [item.to_dict() for item in self.occupied()],
            "invalid": [
                {"slot": slot, "error": dict(self.invalid[slot])}
                for slot in sorted(self.invalid)
            ],
        }

    def diagnostics(self):
        # type: () -> dict
        return {
            "node_key": self.node_key.as_key(),
            "generation": self.ref.generation,
            "loaded": self.loaded,
            "recovery": self.recovery,
            "slot_count": self.slot_count,
            "occupied": self.occupied_count(),
            "config_version": self.config_version,
            "mediums": list(self.medium_ids()),
            "unreadable": self.unreadable,
            "readings": self.readings,
            "revision": self.revision,
            "invalid": [
                {"slot": slot, "error": dict(self.invalid[slot])}
                for slot in sorted(self.invalid)
            ],
            "stats": dict(self.stats),
            "capabilities": list(self.capabilities.names()),
        }

    def __repr__(self):
        return "DriverState(%r, %d/%s 槽)" % (
            self.node_key,
            self.occupied_count(),
            self.slot_count,
        )


class StorageDriverProvider(object):
    """挂在驱动器节点上的**实际存储提供者对象**。

    架构 §4 明确要求「明确实际能力对象在哪里构造、保存和取得，不能只返回标签」：
    每个 `DriverState` 在构造时建一个本对象并注册进它的 `CapabilityRegistry`，
    此后只取用不重建。

    鸭子类型契约来自 `devices/capabilities.py` 的 `StorageProvider`：
    `list_mediums` / `read` / `write` / `save_node_data`。
    """

    adapter_id = "endernet_storage_driver"

    def __init__(self, state, store):
        # type: (DriverState, StorageDriverStore) -> None
        self.state = state
        self.store = store

    # -- StorageProvider 契约 ------------------------------------------

    def list_mediums(self):
        # type: () -> list
        """已装载的介质句柄（稳定 ID 列表）。"""
        return list(self.state.medium_ids())

    def read(self, medium_id, resource_key, count, budget=None):
        # type: (str, ResourceKey, int, TransferBudget) -> TransferReceipt
        """从介质取出资源。走与写入**共用**的额度判定（05-03）。"""
        return self._transfer(medium_id, resource_key, count, DIRECTION_READ, budget)

    def write(self, medium_id, resource_key, count, budget=None):
        # type: (str, ResourceKey, int, TransferBudget) -> TransferReceipt
        return self._transfer(medium_id, resource_key, count, DIRECTION_WRITE, budget)

    def save_node_data(self):
        # type: () -> dict
        return self.state.to_dict()

    # -- 驱动器侧入口（供 StorageService 使用）--------------------------

    def mounts(self):
        # type: () -> list
        """`[(medium_id, slot, slot_version, medium), ...]`。

        介质对象可能是 None（引用在、记录缺）——调用方必须处理，
        那种情况下槽位仍然占着，但**不可交易**。
        """
        out = []
        for item in self.state.occupied():
            out.append(
                (item.medium_id, item.slot, item.slot_version, self.store.medium(item.medium_id))
            )
        return out

    # **`mount` / `unmount` 已删除**（2026-10-04）：它们直接改内存里的槽位表，
    # 在「槽位以方块容器为权威」之后那是一次会被下一次重读静默回退的写入。
    # 装/卸现在只有两条路：玩家动容器（读路径看到），或
    # `StorageDriverStore.place_medium` / `take_medium`（命令层动容器）。

    def slot_views(self):
        # type: () -> list
        return self.state.slot_views()

    def config_version(self):
        # type: () -> int
        return self.state.config_version

    def disk_count(self):
        # type: () -> int
        return self.state.disk_count(self.store.medium)

    def plan_transfer(self, medium_id, resource_key, count, direction, now_tick):
        # type: (str, ResourceKey, int, str, int) -> object
        """预演。**纯函数**，不改库存也不消耗额度。"""
        medium = self.store.medium(medium_id)
        if medium is None:
            return None
        return medium.plan(resource_key, count, direction, now_tick)

    def capabilities(self):
        # type: () -> ContainerCapabilities
        """能力声明。**主动声明**，不靠默认值——默认全是「不支持」。"""
        return ContainerCapabilities(
            can_read=True,
            can_insert=True,
            can_simulate=True,
            supports_partial=True,
            is_atomic=False,
            notify_token=None,
            max_slots=(
                self.state.slot_count
                if isinstance(self.state.slot_count, int)
                else None
            ),
            note="驱动器槽位；槽内介质由本模组记账，不是原版容器",
        )

    # -- 内部 ----------------------------------------------------------

    def _transfer(self, medium_id, resource_key, count, direction, budget):
        # type: (str, ResourceKey, int, str, TransferBudget) -> TransferReceipt
        budget = budget if budget is not None else TransferBudget()
        op_id = budget.op_id or ""
        if self.state.find(medium_id) is None:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED, Reason.MEDIUM_NOT_MOUNTED
            )
        medium = self.store.medium(medium_id)
        if medium is None:
            # 槽位引用在、记录缺：保留引用但不可交易，不当成空盘。
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED, Reason.MEDIUM_UNRECOVERED
            )
        plan = medium.plan(resource_key, count, direction, budget.now_tick)
        if not plan.ok:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED, plan.reason, resource_key
            )
        moved = medium.commit(plan, budget.now_tick)
        if moved > 0:
            # 内容真的动了才标脏：模拟、被拒、去重命中都不该产生写盘工作
            # （`medium_lore.md` §3 的「预留/释放、模拟、失败、去重命中不增加
            # 容量展示工作」在存档这一侧的对应物）。
            self.store.note_medium_changed(medium_id)
            # 展示行（容量 Lore）同批标脏：内容变了，那件物品上的摘要就旧了。
            self.store.note_display_dirty(medium_id)
        if moved == 0:
            # 计划成立却提交不动，只有两种可能：额度在两次计算之间被吃掉了
            # （`take` 拒绝），或介质在这中间失去了交易资格。分开报，别混成一个原因。
            reason = Reason.RATE_LIMITED
            if not medium.is_tradeable():
                reason = medium.recovery_reason()
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED, reason, resource_key
            )
        return TransferReceipt(
            op_id,
            count,
            moved,
            TransferStatus.COMPLETE if moved == count else TransferStatus.PARTIAL,
            "" if moved == count else Reason.CONTAINER_PARTIAL,
            resource_key,
        )


class StorageDriverStore(object):
    """驱动器状态集合与介质权威表。

    **由启动层持有并注入网络服务**（架构 §3）：驱动器与介质的权威状态
    不能只存在于某个网络的存储服务里——网络断开、主控移除、运行时重建
    都不等于设备和库存消失。

    同时是**介质稳定 ID 的唯一分配点**：`create_medium` 只在这里分配，
    `adopt_medium` 接管恢复出来的介质且**不换 ID**。
    """

    def __init__(
        self,
        definitions,
        registry,
        policy=None,
        medium_policy=None,
        on_load_changed=None,
        id_source=None,
        containers=None,
    ):
        # type: (object, object, DriverPolicy, MediumPolicy, object, object, object) -> None
        self.definitions = definitions
        self.registry = registry
        self.policy = policy if policy is not None else DriverPolicy()
        self.medium_policy = (
            medium_policy if medium_policy is not None else MediumPolicy()
        )
        #: 槽位读数变化后通知带宽重算的钩子（`AdmissionService.note_device_load_changed`）。
        self.on_load_changed = on_load_changed
        #: 介质 ID 的分配器。**默认实现只保证进程内单调**——它不是持久身份，
        #: 任务 12 接真实后端时必须换成从存档推导的来源（架构 §4）。
        self.id_source = id_source if id_source is not None else self._next_medium_id
        #: `ContainerProvider`：位置 -> ContainerAdapter。**槽位占用的唯一权威**
        #: 就是它读到的方块容器。可为 None，此时一切需要容器的动作明确报
        #: 「读不到」，而不是当成空槽（同 `MemorySlotStore`）。
        self.containers = containers

        self._states = {}
        self._mediums = {}
        self._id_counter = 0
        self._orphans = []
        #: 还没写进存档的**介质 ID**。**谁改内容谁标脏**，不由扫描兜底——
        #: 漏标一次的表现是「改了没存」，而那种缺陷要等到重进世界才看得见。
        #: （**驱动器本身不再有脏集**：它不再进存档。）
        self._dirty_mediums = set()
        #: 旧存档留下的驱动器记录：`记录 ID -> [SlotAssignment, ...]`。
        #: 读回来**只报进诊断**、不据此造物品，处置见 `restore_from_repository`。
        self._legacy_slots = {}
        #: 待从存档删掉的旧驱动器记录 ID。
        self._retired_driver_records = set()
        #: **信封读不懂**的旧驱动器记录：连删都不删——那份存档不是本构建
        #: 写的，凭什么断定「这台驱动器现在该从档案里消失」（同 12-03 的纪律）。
        self._kept_driver_records = set()
        #: **读不懂**、只能原样保留的介质 ID。两个来源：记录信封版本过新
        #: （`Record.read_only`），或载荷本身读不出（`MediumRecovery` 非 OK，
        #: 含型号缺失与损坏）。它们照样接管进内存（内容不能丢），但
        #: **永远不标脏、不写回**。
        #:
        #: 第二条是**必须的**，不能只看 `read_only`：信封版本按
        #: `CURRENT_SCHEMA_VERSION` 写，而载荷版本是 `MEDIUM_SCHEMA_VERSION`，
        #: 两者可以不同步；`Medium.to_dict()` 又**没有 `raw_payload` 回退**。
        #: 只按信封判的话，一块「载荷读不懂」的盘一旦被标脏，写回去的
        #: 就是一份 `entries: []` 的**空盘**——内容直接没了。
        self._frozen_mediums = set()
        #: 载体还没写回成功、**等下一帧复用同一个号重试**的槽位。
        #: 键 `(位置文本, 槽)`，值 `{"medium_id", "model_id", "attempts"}`。
        #: **它在发号之前就落账**，因此重复重读不会重复发号（见 `_materialize_blank`）。
        self._pending_carriers = {}
        #: 待刷新展示行（容量 Lore）的介质 ID：`medium_id -> 标脏帧号`。
        #: 写回是写引擎容器，比读贵，因此单独排队、单独预算（12-04 的
        #: 「不每 tick 扫描全部介质、显示不变跳过写回」）。
        self._display_dirty = {}
        self._display_seen = set()
        #: 介质 ID 是不是「从存档推导」的持久身份（12-02 定案）。
        #: 默认是进程内替身，`use_durable_ids()` 之后才为真。
        self._durable_ids = False
        self.tick = 0
        self.archive_stats = {
            "medium_writes": 0,
            "driver_deletes": 0,
            "restored_mediums": 0,
            "legacy_driver_records": 0,
            "legacy_driver_records_retired": 0,
        }
        self.stats = {
            "created": 0,
            "dropped": 0,
            "load_marks": 0,
            "marks": 0,
            "refreshes": 0,
            "mediums_created": 0,
            "mediums_adopted": 0,
            "projections": 0,
            "carriers_written": 0,
            "display_writes": 0,
            "display_skipped": 0,
        }

    # -- 驱动器状态 ----------------------------------------------------

    def slot_count(self):
        # type: () -> object
        return self.policy.resolve_slot_count(self.definitions)

    def ensure(self, ref, device_id=None):
        # type: (object, str) -> DriverState
        """取或建一台驱动器的状态。同一代次重复调用是幂等的。

        **建好立刻读一次**（同 `MemorySlotStore.ensure`）：槽位就是方块容器
        的读数，不读的话这台设备在下次重读之前一直是空的，刚插进去的盘
        会像没生效一样。
        """
        existing = self.get_by_ref(ref)
        if existing is not None:
            return existing
        if device_id is None:
            record = self.registry.get_by_ref(ref)
            device_id = record.device_id if record is not None else DRIVER_DEVICE_ID
        if not is_storage_driver(device_id, self.definitions):
            return None
        state = DriverState(ref, self.policy, self, device_id, self.slot_count())
        self._states[ref.node_key.as_key()] = state
        self.stats["created"] += 1
        self.refresh(ref)
        return state

    def get(self, node_key):
        # type: (object) -> DriverState
        state = self._states.get(node_key.as_key())
        if state is None or not is_storage_driver(state.device_id, self.definitions):
            return None
        return state

    def get_by_ref(self, ref):
        # type: (object) -> DriverState
        """按实例代次取。**代次不符即失效**——这是延迟任务的自毁检查。"""
        state = self._states.get(ref.node_key.as_key())
        if state is None or state.ref.generation != ref.generation:
            return None
        return state

    def drop(self, ref):
        # type: (object) -> DriverState
        """拆除：移除读数。**不删介质、也不标孤儿。**

        容器权威下介质的归属是**那件物品本身**：方块被拆时容器里的物品
        按游戏规则处理（掉落或留存），载体还在物品上，插到哪台驱动器都能
        认回来。把介质标成 `orphaned` 会让一块完好的盘变成不可交易
        （`is_tradeable()` 为假），方向是错的。

        **拆除时容器里的物品会掉出来**（User 2026-10-04 提供的引擎语义：
        带 `netease:block_container` 的方块，容器行为直接继承箱子，方块被破坏时
        内容物照常掉落。**本轮没有在真机上亲自复现过这一条**）。因此这里的
        「不 orphan」是对的：那件物品掉在地上、载体还在它身上，玩家捡回来插到
        任何一台驱动器都能认回同一块盘——把介质标成孤儿反而会让一块完好的盘
        变成不可交易。**本层不另发一份掉落物**（那会与引擎的掉落重复）。
        """
        state = self.get_by_ref(ref)
        if state is None:
            return None
        key = node_key_text(state.node_key)
        self._states.pop(ref.node_key.as_key(), None)
        # 这个位置上还没写回成功的载体请求作废：槽已经不在了。
        for pending_key in [item for item in self._pending_carriers if item[0] == key]:
            self._pending_carriers.pop(pending_key, None)
        if key not in self._kept_driver_records:
            # 旧存档里那个位置的驱动器记录**删掉而不是留着**：同位置重放的
            # 新实例不得继承旧引用（12 验收「同一位置拆后重放……不会自动继承」）。
            self._retired_driver_records.add(key)
        self.stats["dropped"] += 1
        return state

    def mark_loaded(self, node_key, loaded):
        # type: (object, bool) -> bool
        """区块加载状态变化。**槽位与介质 ID 一字不改**——

        区块重载不得重新分配介质 ID，也不得让内容看起来消失过。
        """
        state = self.get(node_key)
        if state is None or state.loaded == bool(loaded):
            return False
        state.loaded = bool(loaded)
        self.stats["load_marks"] += 1
        return True

    def mark_all_unloaded(self):
        # type: () -> int
        """停机：把所有驱动器标为未加载。**不写仓库、不清内容**。"""
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

    # -- 生命周期 ------------------------------------------------------

    def observe(self, change):
        # type: (object) -> int
        """从设备生命周期变化推导驱动器状态。**唯一入口**。

        由启动层的 `_emit` 调用，因此与拓扑消费的是同一批变化，
        不会出现「拓扑知道设备没了、驱动器还以为在」的分叉。
        返回是否真的处理了。
        """
        kind = change.kind
        if kind == ChangeKind.ADDED:
            if not is_storage_driver(change.device_id, self.definitions):
                return 0
            state = self.ensure(change.ref, change.device_id)
            if state is None:
                return 0
            # `ensure` 已经读了一次；这里再投影一次是为了「读不到容器」的设备
            # 也留下一个明确的 0（与改版前一致，准入读的就是这个字段）。
            self.project(state)
            return 1
        if kind == ChangeKind.REMOVED:
            if not is_storage_driver(change.device_id, self.definitions):
                return 0
            state = self.drop(change.ref)
            if state is None:
                return 0
            return 1
        if kind in (ChangeKind.LOADED, ChangeKind.UNLOADED):
            if not is_storage_driver(change.device_id, self.definitions):
                return 0
            if not self.mark_loaded(change.node_key, kind == ChangeKind.LOADED):
                return 0
            # 重新加载之后容器内容可能已经变了（退出前的手工调整、别的模组），
            # 因此标脏等下一次重读，而不是信任内存里那一份（同内存槽）。
            self.mark_dirty(change.node_key)
            return 1
        # NEIGHBORS_AFFECTED 与 PLACEMENT_REVIEW 是拓扑与交互层的事。
        return 0

    def project(self, state):
        # type: (DriverState) -> bool
        """把权威槽位投影到 `DeviceRecord.config["disk_count"]`。

        **这是唯一写该字段的地方**（架构 §4）。带宽准入读的就是它，
        因此任何命令载荷都不得直接提交计数——设备不得绕过权威槽位改负载。
        """
        record = self.registry.get(state.node_key)
        if record is None:
            # 设备已经被移除（REMOVED 时注册表先删了记录）。
            # 此时负载由拓扑重建统一处理，不需要投影。
            return False
        count = state.disk_count(self.medium)
        if record.config.get("disk_count") == count:
            return False
        record.config["disk_count"] = count
        self.stats["projections"] += 1
        if self.on_load_changed is not None:
            self.on_load_changed(state.node_key)
        return True

    # -- 介质权威表 ----------------------------------------------------

    def mediums(self):
        # type: () -> dict
        return dict(self._mediums)

    def medium(self, medium_id):
        # type: (str) -> Medium
        return self._mediums.get(medium_id)

    def create_medium(self, model_id, tick=0):
        # type: (str, int) -> tuple
        """新建一块空盘。返回 `(medium, reason, detail)`。

        型号未注册、或本驱动器策略不接受它，都明确拒绝——**不建一块无规格的盘**。
        """
        if not self.definitions.has_medium(model_id):
            return (
                None,
                Reason.NOT_A_MEDIUM,
                "未注册的介质型号 %s" % (model_id,),
            )
        if not self.policy.accepts_model(model_id):
            reason, detail = self.policy.rejected_model_reason(model_id)
            return None, reason, detail
        medium_id = self.id_source()
        if not medium_id or medium_id in self._mediums:
            raise AssertionError("介质 ID 分配器给了重复或空 ID: %r" % (medium_id,))
        medium = Medium.create(
            medium_id, model_id, self.definitions, self.medium_policy, now_tick=tick
        )
        self._mediums[medium_id] = medium
        self.stats["mediums_created"] += 1
        # 新盘是**唯一**在这里发号的地方（12-02：只在首次挂载/进网时分配）。
        # 立刻标脏，否则「造出来还没装就被存档」会漏掉它。
        self._dirty_mediums.add(medium_id)
        return medium, "", ""

    def adopt_medium(self, medium):
        # type: (Medium) -> Medium
        """接管一块恢复出来的介质。**绝不新分配 ID**（05-04）。

        重复 ID 不允许悄悄覆盖：那会把两块盘的内容并成一块。
        """
        if medium.medium_id in self._mediums:
            raise ValueError("介质 %s 已被接管，不能重复接管" % (medium.medium_id,))
        self._mediums[medium.medium_id] = medium
        self.stats["mediums_adopted"] += 1
        return medium

    def load_medium(self, payload, tick=0):
        # type: (dict, int) -> Medium
        """从存档载荷恢复一块介质并接管。

        读不懂的载荷**照样接管**（内容与原始载荷都保留），只是不可交易。
        """
        medium = Medium.from_dict(
            payload, self.definitions, self.medium_policy, now_tick=tick
        )
        return self.adopt_medium(medium)

    def orphans(self):
        # type: () -> tuple
        """失去归属、等待后续任务决定处置方式的介质 ID。"""
        return tuple(self._orphans)

    def use_durable_ids(self):
        # type: () -> int
        """切到**从存档推导**的介质 ID 来源（12-02 定案、12-03 落地）。

        **必须在恢复之后调用**：它把发号水位抬到已接管的最大序号之上。

        分配时机只有一处——介质**首次挂载／进网**时由 `create_medium` 发号。
        读取路径永不生成 ID：`load_medium` / `adopt_medium` 用的永远是
        记录里那一个。

        为什么不能沿用进程内那个计数器：它重启后从 0 重新数，于是
        「重进世界找回同一块盘」变成「拿到一个新 ID」，而新 ID 指向的记录
        不存在——物品上带着的旧 ID 就成了**孤儿**。序号来自存档本身，
        这条链才接得上。
        """
        self._id_counter = self._highest_medium_sequence()
        self.id_source = self._next_medium_id
        self._durable_ids = True
        return self._id_counter

    def _highest_medium_sequence(self):
        # type: () -> int
        highest = 0
        for medium_id in self._mediums:
            sequence = medium_sequence(medium_id)
            if sequence > highest:
                highest = sequence
        return highest

    def medium_slot_owner(self, medium_id):
        # type: (str) -> object
        """这块介质装在哪台驱动器的哪个槽位。不在任何槽位返回 None。

        **跨驱动器去重的依据。** `DriverState.slot_of` 只看本机，两台驱动器
        各插一块同 ID 的盘在它那里是合法的——那会让一份内容被记两次。
        读路径的 `_resolve_cross_driver_duplicates` 用本方法做确定性裁决，
        命令层也用它拒绝「已挂在别处」的请求。
        """
        for state in self._states.values():
            assignment = state.find(medium_id)
            if assignment is not None:
                return (state, assignment)
        return None

    def _next_medium_id(self):
        # type: () -> str
        """介质 ID 分配器。

        缺省（`use_durable_ids()` 之前）它**不是持久身份**：计数器从 0 起，
        重启后重数，因此不能作为「重进世界后找回同一块盘」的依据。
        `use_durable_ids()` 把水位抬到存档里已有的最大序号之上，同一个
        函数从此变成持久的——**这也是为什么它只改水位、不另写一份实现**：
        两条实现迟早会分叉，而分叉的那一半会安静地发出重复 ID。

        无论哪种模式都跳过已被接管的 ID，保证不复用。
        """
        while True:
            self._id_counter += 1
            candidate = "%s%d" % (MEDIUM_ID_PREFIX, self._id_counter)
            if candidate not in self._mediums:
                return candidate

    # -- 容器读数（槽位的唯一权威）---------------------------------------

    def container_at(self, node_key):
        # type: (object) -> object
        """这台设备的容器适配器；拿不到时返回 `None`（**不是空容器**）。"""
        if self.containers is None:
            return None
        return self.containers.container_at(node_key)

    def accepts_put(self, device_id, item_name):
        # type: (str, str) -> bool
        """这个物品能不能放进这台设备的容器。**只答驱动器那一部分。**

        不是驱动器一律放行——别家容器各有各的规则，`events._veto_or_mark`
        会逐个 store 问。

        **只能按名字判**：放入事件里读不出 `userData`（`events._item_name`
        只看 `newItemName`），因此这里问不了型号粒度——
        `DriverPolicy.accepted_models` 那个 R10 替身留在重读路径上。
        """
        if not is_storage_driver(device_id, self.definitions):
            return True
        if not item_name:
            # 读不出物品名就不拦（与 `_veto_or_mark` 同一口径）。
            return True
        return bool(self.definitions.has_medium(item_name))

    def disk_count_of(self, ref):
        # type: (object) -> object
        """能源要的 `unit_count`：硬盘个数；**从没读到过则是 `None`（待确认）**。

        与 `MemorySlotStore.unit_count_of` 同形。返回 `None` 而不是 0 是有意的：
        判定停在待确认，而不是伪造一个「这台驱动器一块盘都没有、所以不耗电」
        的结论。读得到时按上一次成功读数给（读不到不清零）。
        """
        state = self.get_by_ref(ref)
        if state is None or not state.has_reading():
            return None
        return state.disk_count(self.medium)

    # -- 变更信号 ------------------------------------------------------

    def mark_dirty(self, node_key, tick=None):
        # type: (object, int) -> bool
        """这台驱动器的容器变了。**只记一笔**，不在这里读容器。

        引擎无关的入口：适配器把容器事件翻译成位置后调它。不立刻读的两个
        理由同 `PatternStore.mark_dirty`：事件顺序不可依赖；事件触发时
        容器内容可能还没落定。
        """
        state = self.get(node_key)
        if state is None:
            return False
        state.dirty_tick = self.tick if tick is None else int(tick)
        self.stats["marks"] += 1
        return True

    def mark_all_dirty(self):
        # type: () -> int
        """把全部驱动器标脏。恢复流程与手动重扫用它重建读数。"""
        now = self.tick
        count = 0
        for state in self._states.values():
            state.dirty_tick = now
            count += 1
        return count

    # -- 重读 ----------------------------------------------------------

    def advance(self, tick):
        # type: (int) -> int
        """推进节拍并重读到期的驱动器。返回本帧重读了几台。

        由 `bootstrap.tick()` 每帧调用一次。**没到期的一台都不碰。**
        """
        self.tick = int(tick)
        pending = [
            state
            for state in self.states()
            if state.loaded and self._is_due(state)
        ]
        pending = pending[:DRIVER_REFRESH_BUDGET]
        refreshed = 0
        for state in pending:
            # 一台出问题不能连累其余：少了这层兜底，一次意外会让排在后面的
            # 驱动器永远刷不到——症状是「有的设备同步、有的从不同步」。
            try:
                if self.refresh(state.ref):
                    refreshed += 1
            except Exception as error:  # pragma: no cover - 防御性
                state.stats["errors"] = state.stats.get("errors", 0) + 1
                state.unreadable = u"重读时出错：%s: %s" % (
                    type(error).__name__,
                    error,
                )
        return refreshed

    def _is_due(self, state):
        # type: (DriverState) -> bool
        """这台驱动器现在该不该重读。规则同 `MemorySlotStore._is_due`。"""
        if state.refresh_tick < 0:
            # 从没读到过（`ensure` 那一次被拒了，比如当时还没加载）。
            return True
        if state.unreadable:
            # 读不到的要定期重试：一次瞬时失败之后**不会有任何变更事件再来**。
            return self.tick - state.refresh_tick >= DRIVER_MAX_STALE_FRAMES
        if state.dirty_tick < 0:
            # 从没收到过变更，也读得到：没有理由再读一遍。
            return False
        if state.refresh_tick > state.dirty_tick:
            # 这一次变更已被之后的某次重读覆盖过了。**必须是严格大于**：
            # 同一帧里「先刷新、后收到变更」是常规情形。
            return False
        if self.tick - state.dirty_tick >= DRIVER_SETTLE_FRAMES:
            return True
        return self.tick - state.refresh_tick >= DRIVER_MAX_STALE_FRAMES

    def refresh(self, ref, force=False):
        # type: (object, bool) -> bool
        """立刻重读一台驱动器的容器并重建槽位表。返回是否读到了。

        `force=True` 用于人工重扫与测试：跳过时机判断，直接读。
        """
        state = self.get_by_ref(ref)
        if state is None:
            return False
        if not state.loaded and not force:
            return False
        if force:
            state.stats["forced_refreshes"] += 1
        ok = False
        try:
            # **整段都在 `try` 里**，包括最开始那次指纹与最后那几步落地。
            # `refresh` 的调用方分布在引擎事件与每帧推进两条路径上，任何一处
            # 抛出去都会作废本帧剩余的事件批次——因此它**保证不抛**。
            # 这不是「吞异常」：类型与文本记进 `unreadable`，且计数进 `errors`。
            #
            # 2026-10-04 真机补：这几步原先在 `try` 外面（指纹在那之前、
            # 去重/投影/首次读到在那之后），一次偶发失败就真的逃了出去。
            # 那次的现场是 MCDK 探针（见 05-04 交付说明 §5.5），模组自身路径
            # 没有发作过，但契约是契约。
            before = state.content_fingerprint()
            ok = self._read_slots(state)
            if ok:
                state.readings += 1
                if state.content_fingerprint() != before:
                    state.revision += 1
                    state.stats["changes"] += 1
            if ok:
                # 跨驱动器去重与投影都放在读数成立之后：`_read_slots` 失败时
                # 旧读数原样保留，不能拿半份结果去动别人的槽位。
                self._resolve_cross_driver_duplicates()
                self.project(state)
                self._note_first_seen(state)
        except Exception as error:
            state.unreadable = u"重读时出错：%s: %s" % (
                type(error).__name__,
                error,
            )
            state.stats["errors"] += 1
            ok = False
        finally:
            # **帧号用 `finally` 记**：写在 `return` 之前的话，一次抛错就会
            # 让它停在 -1，于是 `_is_due` 判它「从没读过」、每帧重试。
            state.refresh_tick = self.tick
            state.stats["refreshes"] += 1
            self.stats["refreshes"] += 1
        return ok

    def _note_first_seen(self, state):
        # type: (DriverState) -> None
        """**头一次读到**的介质把展示行标脏（05 的详情增量「恢复核对后更新摘要」）。

        之后内容每变一次也会再标（见 `StorageDriverProvider._transfer`）。
        只看「读到过没有」，因此一台驱动器每帧重读不会反复标脏。
        """
        for medium_id in state.medium_ids():
            if medium_id in self._display_seen:
                continue
            self._display_seen.add(medium_id)
            self.note_display_dirty(medium_id)

    def refresh_all(self, force=True):
        # type: (bool) -> int
        """重读全部驱动器。恢复流程与手动重扫的入口。"""
        return sum(
            1 for state in self.states() if self.refresh(state.ref, force=force)
        )

    def _resolve_cross_driver_duplicates(self):
        # type: () -> int
        """同一块介质出现在多台驱动器上：按 `(位置文本, 槽号)` 字典序保留最小的
        一个，其余就地标成 `duplicate_across_drivers` 并从槽位表里摘掉。

        **这是改版新引入的确定性裁决。** 改版前这件事靠「物理上不可能同时插在
        两台里」保证（槽位只在命令里写入，`mount` 拒第二个）。改成容器读数之后，
        两台驱动器可以各插一块同 ID 的盘（创造模式复制、别的模组），
        必须有确定性的规则，否则同一份内容被记两次、而且结果随读序变化。

        **不碰容器里那件物品**：它还在那儿，玩家自己取出来即可。
        """
        entries = []
        for state in self._states.values():
            for item in state.occupied():
                entries.append((node_key_text(state.node_key), item.slot, state, item))
        entries.sort(key=lambda entry: (entry[0], entry[1]))
        claimed = {}
        touched = []
        evicted = 0
        for text, slot, state, item in entries:
            owner = claimed.get(item.medium_id)
            if owner is None:
                claimed[item.medium_id] = (text, slot)
                continue
            if state.evict_slot(
                slot,
                "duplicate_across_drivers",
                u"介质 %s 已在 %s 的 %d 号槽；同一块盘不能挂两处"
                % (item.medium_id, owner[0], owner[1]),
            ):
                evicted += 1
                if state not in touched:
                    touched.append(state)
        for state in touched:
            self.project(state)
        return evicted

    def _read_slots(self, state):
        # type: (DriverState) -> bool
        """读一台驱动器的全部格。返回是否真的读到了容器。

        **读不到时保留上一次的槽位、只标 `unreadable`**（区块卸载不等于盘被拔了）。
        """
        adapter = self.container_at(state.node_key)
        if adapter is None:
            state.unreadable = (
                "读不到这台设备的方块容器（区块未加载、位置已无方块、"
                "或该类型尚未登记适配器）"
            )
            state.stats["unreadable"] += 1
            return False
        if not callable(getattr(adapter, "read_slot", None)):
            # 没有按槽读取能力就不退化成整表扫描：槽位是**逐槽**的，
            # 扫描读不出「哪一格插着什么」，会张冠李戴。
            state.unreadable = "这台设备的容器适配器不支持按槽读取，读不出槽里的介质"
            state.stats["unreadable"] += 1
            return False
        state.unreadable = ""
        state.slot_count = _slot_count_of(
            adapter, state.slot_count or STORAGE_DRIVER_SLOT_COUNT.value
        )
        slots = {}
        invalid = {}
        for slot in range(state.slot_count):
            medium_id, error = self._read_one(state, adapter, slot)
            if state.unreadable:
                # 中途读失败，整次作废：半份结果会让剩下的槽看起来是空的。
                state.stats["unreadable"] += 1
                return False
            if medium_id is None:
                if error is not None:
                    invalid[slot] = error
                continue
            if medium_id in slots.values():
                # 本机重复：槽号小的先读到，因此它赢；后到的报重复。
                invalid[slot] = {
                    "code": "duplicate_in_driver",
                    "detail": u"介质 %s 已经在本驱动器的 %d 号槽" % (
                        medium_id,
                        min(
                            index
                            for index, value in slots.items()
                            if value == medium_id
                        ),
                    ),
                }
                continue
            slots[slot] = medium_id
        state.replace_slots(slots, invalid, state.slot_count, self.tick)
        return True

    def _read_one(self, state, adapter, slot):
        # type: (DriverState, object, int) -> tuple
        """读一个槽。返回 `(medium_id | None, error | None)`。

        分支与它们的后果（每一条都要有回归位）：

        | 读到的东西 | 处置 |
        | --- | --- |
        | 空槽 | 什么都不算 |
        | 无载体 + 名字是介质 | **空白介质**：发号 + 写回载体（`_materialize_blank`）|
        | 无载体 + 名字不是介质 | `not_a_medium`（不搬动、不占槽）|
        | 有载体键但读不出 | `malformed_carrier`（**绝不当空白盘再发号**）|
        | 载体版本不认识 | `unknown_carrier_version`（不猜新版本语义）|
        | 载体命中但记录缺失 | 占槽（孤儿）：不发号、不删引用、不当空盘 |
        | 一格叠了多块 | `stacked_media`（拆开是一道未定规则，本轮不动它）|
        """
        try:
            data = adapter.read_slot(slot)
        except Exception as error:
            # **当成读不到容器**，而不是当成空槽——空槽会让这台设备看起来
            # 「盘被拔了」，而库存会据此归零。
            state.unreadable = u"读取槽位 %d 时容器读不出来：%s: %s" % (
                slot,
                type(error).__name__,
                error,
            )
            return None, None
        if is_empty_item(data):
            return None, None
        try:
            item_key = item_key_from_dict(data)
        except (TypeError, ValueError) as error:
            # 这里**不能 `%s` 那个异常对象**：`str(exc)` 在 py2 上对含
            # 非 ASCII 的 `args[0]` 会按 ASCII 编码并抛 `UnicodeEncodeError`。
            return None, {
                "code": "malformed",
                "detail": u"槽里的物品字典读不动：%s: %s"
                % (type(error).__name__, error),
            }
        if item_key is None:
            return None, None
        name = item_key.name
        if not self.definitions.has_medium(name):
            return None, {
                "code": "not_a_medium",
                "detail": u"%s 不是本模组的介质（放进来的那一刻就该被拒收，"
                          u"这是规则生效之前留下的、或由别的模组塞进来的）" % (name,),
            }
        count = _item_count(data)
        if count != 1:
            # **一格只挂一块。** 空白介质是可以堆叠的（`medium_lore.md` 第 2 节），
            # 因此「一格 64 块空白磁带」是真会发生的事；要挂载就得把其中一块
            # 拆出来、并决定剩下那些放哪——那是一道未定规则（R05/R10）。
            # 本轮**不改动它**，如实报出来等人处置。
            return None, {
                "code": "stacked_media",
                "detail": u"%s × %d 叠在一格：驱动器一格只挂一块，"
                          u"拆开是一道未定规则（R05/R10）" % (name, count),
            }
        user_data = _user_data_of(data)
        if isinstance(user_data, dict) and medium_item.MEDIUM_CARRIER_KEY in user_data:
            carrier = medium_item.read_carrier(user_data)
            if carrier is None:
                return None, {
                    "code": "malformed_carrier",
                    "detail": u"载体读不动（ID 不是纯 ASCII、或格式版本不是整数）；"
                              u"**不当空白盘处理**——那会把一块有内容的盘重识别成新盘",
                }
            medium_id, version = carrier
            if version != medium_item.CARRIER_VERSION:
                return None, {
                    "code": "unknown_carrier_version",
                    "detail": u"载体格式版本 %r（本版本认得 %d）：不猜新版本的语义"
                    % (version, medium_item.CARRIER_VERSION),
                }
            # 记录缺失 → 孤儿。**占着槽**（`disk_count` 计入、`_transfer` 会以
            # `MEDIUM_UNRECOVERED` 拒绝交易），但不发号、不当空盘。
            return medium_id, None
        return self._materialize_blank(state, adapter, slot, name, self.tick)

    def _materialize_blank(self, state, adapter, slot, model_id, tick):
        # type: (DriverState, object, int, str, int) -> tuple
        """给一块**未初始化的空白介质**发号，把载体写进那件物品，再挂上。

        三步都必需，顺序也不能换：

        1. **先在 `_pending_carriers` 落账、再发号**。这是防重复发号的关键：
           `create_medium` 之后介质立刻进 `_mediums`，但写回要是失败，
           下一帧重读看到的**还是一块没有载体的空白物品**——没有这条账，
           每帧都会发一个新号，一口气造出一堆幽灵盘。
        2. **写回载体，忽略返回值**。真机实测 `SpawnItemToContainer`
           会写成功却返回 `False`（`resource_identity.md` §2.3）。
        3. **只认读回核对**。写回的判据是「再读一次，载体是不是我要的那个」。
        """
        key = (node_key_text(state.node_key), slot)
        pending = self._pending_carriers.get(key)
        if pending is not None and pending.get("model_id") == model_id:
            medium_id = pending["medium_id"]
        else:
            if pending is not None:
                # 槽里换了型号：旧的待写回作废（那块介质记录留着，不标孤儿）。
                self._pending_carriers.pop(key, None)
            medium, reason, detail = self.create_medium(model_id, tick)
            if medium is None:
                return None, {"code": "not_a_medium", "detail": detail}
            medium_id = medium.medium_id
            pending = {
                "medium_id": medium_id,
                "model_id": model_id,
                "attempts": 0,
            }
            self._pending_carriers[key] = pending
        pending["attempts"] = pending.get("attempts", 0) + 1
        if pending["attempts"] > DRIVER_PENDING_MAX_ATTEMPTS:
            # 一块永远写不进去的槽不能每帧重试。**报「写不进」不报「空盘」。**
            return None, {
                "code": "carrier_write_unverified",
                "detail": u"载体写入 %s 号槽连续失败 %d 次，已停止重试；"
                          u"盘已发号但读不回来" % (slot, pending["attempts"] - 1),
            }
        try:
            writable = item_dict_from_key(ResourceKey.item(model_id, 0), 1)
            writable["userData"] = medium_item.write_carrier(
                None, medium_id, medium_item.CARRIER_VERSION
            )
            adapter.write_slot(slot, writable)
        except Exception as error:
            return None, {
                "code": "carrier_write_unverified",
                "detail": u"写入 %s 号槽时出错：%s: %s"
                % (slot, type(error).__name__, error),
            }
        try:
            after = adapter.read_slot(slot)
        except Exception as error:
            return None, {
                "code": "carrier_write_unverified",
                "detail": u"写回后读不出 %s 号槽：%s: %s"
                % (slot, type(error).__name__, error),
            }
        carrier = medium_item.read_carrier(_user_data_of(after))
        if carrier is None or carrier[0] != medium_id:
            # 写下去了但读回来不是它——**保留 pending**，下一帧用同一个号重试。
            return None, {
                "code": "carrier_write_unverified",
                "detail": u"%s 号槽的载体写回没有核对上（读回来是 %r）"
                % (slot, carrier),
            }
        self._pending_carriers.pop(key, None)
        self.stats["carriers_written"] += 1
        self.note_medium_changed(medium_id)
        self.note_display_dirty(medium_id)
        return medium_id, None

    # -- 存档 ----------------------------------------------------------

    # -- 展示写回（介质物品的容量 Lore）-----------------------------------

    def note_display_dirty(self, medium_id):
        # type: (str) -> None
        """这件介质物品的展示行该刷新了。**只记一笔**，不在这里写引擎。

        写回是写方块容器，比读贵，因此单独排队、单独预算：
        05 的详情增量明写「不每 tick 扫描全部介质」「显示不变跳过写回」。
        """
        if medium_id in self._mediums and medium_id not in self._frozen_mediums:
            self._display_dirty[medium_id] = self.tick

    def advance_display(self, budget=None):
        # type: (int) -> int
        """把到期的展示行写回槽里的介质物品。返回写了几件。

        写入前的三道闸（缺一不可，理由见 05 的详情增量与 `medium_lore.md` §4）：

        1. **去抖**：标脏之后静默 `DRIVER_SETTLE_FRAMES` 帧再写，免得一次
           大额提交过程中反复重写。
        2. **显示不变跳过**：只比较**管理行**。只比内容版本是不够的——
           先取后存、取放相抵时内容回原样，而 Lore 本来就没变，不必写。
        3. **写前核对**：那块盘可能已经被拔走了（那就不写，物品已经离开我们的容器）。

        写回失败**不回滚任何已成功的库存交易**，只保留待刷新状态等下次。
        """
        if budget is None:
            budget = DRIVER_DISPLAY_BUDGET
        written = 0
        for medium_id in sorted(self._display_dirty):
            if written >= budget:
                break
            dirty_tick = self._display_dirty.get(medium_id, self.tick)
            if self.tick - dirty_tick < DRIVER_SETTLE_FRAMES:
                continue
            medium = self._mediums.get(medium_id)
            if medium is None or medium_id in self._frozen_mediums:
                self._display_dirty.pop(medium_id, None)
                continue
            owner = self.medium_slot_owner(medium_id)
            if owner is None:
                # 不在任何槽里：物品已经离开我们的容器，没地方写。
                self._display_dirty.pop(medium_id, None)
                self.stats["display_skipped"] += 1
                continue
            state, assignment = owner
            adapter = self.container_at(state.node_key)
            if adapter is None or not callable(getattr(adapter, "read_slot", None)):
                continue
            try:
                data = adapter.read_slot(assignment.slot)
            except Exception:
                continue
            if is_empty_item(data):
                self._display_dirty.pop(medium_id, None)
                continue
            carrier = medium_item.read_carrier(_user_data_of(data))
            if carrier is None or carrier[0] != medium_id:
                # 槽里已经不是这块盘了：这次待办作废。
                self._display_dirty.pop(medium_id, None)
                continue
            desired_lines = medium_item.medium_lore_lines(medium)
            current_lines = _managed_lines_of(data)
            if tuple(current_lines) == tuple(desired_lines):
                # **显示不变就跳过写回。**
                self._display_dirty.pop(medium_id, None)
                self.stats["display_skipped"] += 1
                continue
            writable = dict(data)
            writable["userData"] = medium_item.materialize_item_user_data(
                medium, _user_data_of(data)
            )
            try:
                adapter.write_slot(assignment.slot, writable)
                after = adapter.read_slot(assignment.slot)
            except Exception:
                continue
            if tuple(_managed_lines_of(after)) != tuple(desired_lines):
                # 没写进去：**保留待刷新状态**，不把显示成功当成完成。
                continue
            self._display_dirty.pop(medium_id, None)
            self.stats["display_writes"] += 1
            written += 1
        return written

    def pending_display_writes(self):
        # type: () -> int
        return len(self._display_dirty)

    # -- 存档：只剩介质这一半（12-03 接的）--------------------------------

    def note_medium_changed(self, medium_id):
        # type: (str) -> None
        """标脏一块介质的内容。**谁改内容谁调**，不由扫描兜底。

        版本过新的记录**跳过**：它只能原样保留（见 `_frozen_mediums`）。
        驱动器**不再有脏集**——槽位是容器读数，不进存档。
        """
        if medium_id in self._mediums and medium_id not in self._frozen_mediums:
            self._dirty_mediums.add(medium_id)

    def pending_archive_writes(self):
        # type: () -> int
        """还没落盘的记录数（脏介质 + 待删的旧驱动器记录）。诊断与预算用。"""
        return len(self._dirty_mediums) + len(self._retired_driver_records)

    def save_to_repository(self, repository, budget=None):
        # type: (object, int) -> int
        """把脏的**介质**写进存档，并把退役的旧驱动器记录删掉。返回写了几条。

        **只写介质**：槽位是方块容器的读数，存下来再读回去等于给读数造
        第二份真相（`persistence_backend.md` §4.1）。
        """
        written = 0
        for medium_id in sorted(self._dirty_mediums):
            if budget is not None and written >= budget:
                break
            if medium_id in self._frozen_mediums:
                # 第二道闸：冻结判断在恢复期就做完了，这里再挡一次，
                # 免得将来多出一条标脏路径就绕过去了。
                self._dirty_mediums.discard(medium_id)
                continue
            medium = self._mediums.get(medium_id)
            payload = None if medium is None else medium.to_dict()
            if payload is not None:
                repository.write(Record(MEDIUM_RECORD_KIND, medium_id, payload))
                self.archive_stats["medium_writes"] += 1
            # **写成功之后才摘脏。** `repository.write` 抛异常（故障注入、
            # 版本过新）时这一行走不到，记录留在脏里等下一轮重试——
            # 先摘脏再写会让那次改动永远丢掉。
            self._dirty_mediums.discard(medium_id)
            written += 1

        for record_id in sorted(self._retired_driver_records):
            if record_id in self._kept_driver_records:
                # 信封读不懂的旧记录**连删都不删**（见 `restore_from_repository`）。
                self._retired_driver_records.discard(record_id)
                continue
            if budget is not None and written >= budget:
                break
            repository.delete(DRIVER_RECORD_KIND, record_id)
            self._retired_driver_records.discard(record_id)
            self.archive_stats["driver_deletes"] += 1
            written += 1
        return written

    def restore_from_repository(self, repository):
        # type: (object) -> dict
        """恢复介质记录；旧驱动器记录**只报进诊断并退役**。

        **介质立刻接管**（内容不能丢；读不懂的照样接管，但冻结、永不写回）。
        槽位不恢复——它由方块容器的读数决定，设备被区块发现时读一次就有。

        **旧驱动器记录不物化。** 上一版把槽位存在 `endernet.storage_driver/<位置>`
        里，可那时挂载只能由 `mount` 命令凭空发号——那些介质**本来就没有对应
        物品**。凭空在世界里造一批物品就是替未定玩法定案（全局约束 7），
        因此这里只把内容报进 `diagnostics()["legacy_driver_records"]`，
        记录本身删掉。那些介质记录留在 ExtraData 里，处置归 12-06 / R05。

        **没有预算参数：恢复是一次读完的**（12-05 的分帧恢复不在此列）。
        """
        stats = {
            "mediums": 0,
            "legacy_drivers": 0,
            "missing": 0,
            "reused_ids": 0,
            "frozen": 0,
        }

        for medium_id in repository.list_ids(MEDIUM_RECORD_KIND):
            record = repository.load(MEDIUM_RECORD_KIND, medium_id)
            if record is None:
                stats["missing"] += 1
                continue
            if medium_id in self._mediums:
                # 同一块盘被恢复了两次。**不合并、不覆盖**——两份内容
                # 合成一份是最坏的结果（`medium_lore.md` 第 2 节第 2 条）。
                stats["reused_ids"] += 1
                continue
            medium = Medium.from_dict(
                record.payload, self.definitions, self.medium_policy
            )
            # **记录的键是权威**，载荷里那个 `medium_id` 只是副本。
            # 不改的话，读不懂的载荷会全用 `Medium._unreadable` 的占位 ID
            # `"unreadable"` 去接管——第二条就撞上「已被接管」，异常从
            # 恢复循环里逃出去，后面连介质都恢复不了。
            medium.medium_id = medium_id
            self.adopt_medium(medium)
            if record.read_only or medium.recovery != MediumRecovery.OK:
                # **接管但不拥有。** 内容原样留在内存里（不能丢），
                # 但从此不标脏、不写回。两条判据缺一不可：
                # 记录信封的版本与载荷自己的版本是**两套**，只看前者时
                # 一块「载荷读不懂」的盘会被 `to_dict()` 写成一枚**空盘**。
                self._frozen_mediums.add(medium_id)
                stats["frozen"] += 1
            stats["mediums"] += 1

        for record_id in repository.list_ids(DRIVER_RECORD_KIND):
            record = repository.load(DRIVER_RECORD_KIND, record_id)
            if record is None:
                stats["missing"] += 1
                continue
            stats["legacy_drivers"] += 1
            self.archive_stats["legacy_driver_records"] += 1
            payload = record.payload if isinstance(record.payload, dict) else {}
            assignments = []
            for item in payload.get("slots", ()):
                try:
                    assignments.append(SlotAssignment.from_dict(item))
                except (KeyError, TypeError, ValueError):
                    continue
            self._legacy_slots[record_id] = assignments
            if record.read_only:
                # 那份存档不是本构建写的，凭什么断定它该消失。
                self._kept_driver_records.add(record_id)
                stats["frozen"] += 1
                continue
            self._retired_driver_records.add(record_id)
            self.archive_stats["legacy_driver_records_retired"] += 1

        self.archive_stats["restored_mediums"] += stats["mediums"]
        return stats

    # -- 装/卸（命令层动容器）-------------------------------------------

    def place_medium(self, state, slot, medium_id, expected_slot_version=None):
        # type: (DriverState, int, str, object) -> tuple
        """把一块**已有**介质放进某个槽。返回 `(ok, reason, detail)`。

        它做的就是把那件物品写进方块容器——**槽位读数是容器的结果，不是原因**。
        写完立刻重读，因此调用方拿到 `ok` 时权威槽位表已经对上了。
        """
        if not state.is_usable():
            return False, state.recovery, state.recovery_detail
        if isinstance(slot, bool) or not isinstance(slot, int):
            return False, Reason.OUT_OF_RANGE, "槽号必须是整数"
        if slot < 0 or slot >= state.slot_count:
            return False, Reason.OUT_OF_RANGE, "槽号 %r 超出 0..%d" % (
                slot,
                state.slot_count - 1,
            )
        medium = self.medium(medium_id)
        if medium is None:
            return False, Reason.MEDIUM_NOT_MOUNTED, "没有 %s 这块介质的记录" % (
                medium_id,
            )
        owner = self.medium_slot_owner(medium_id)
        if owner is not None:
            other_state, assignment = owner
            if other_state is not state or assignment.slot != slot:
                return (
                    False,
                    Reason.MEDIUM_DUPLICATE_MOUNT,
                    "介质 %s 已装在 %s 的 %d 号槽；同一块盘不能挂两处"
                    % (medium_id, node_key_text(other_state.node_key), assignment.slot),
                )
        current = state.assignment(slot)
        current_version = current.slot_version if current is not None else 0
        if expected_slot_version is not None and int(expected_slot_version) != current_version:
            return (
                False,
                Reason.STALE_VERSION,
                "槽位版本为 %d，请求依据的是 %r" % (current_version, expected_slot_version),
            )
        if current is not None and current.medium_id != medium_id:
            return False, Reason.SLOT_OCCUPIED, "槽位 %d 已被 %s 占用" % (
                slot,
                current.medium_id,
            )
        adapter = self.container_at(state.node_key)
        if adapter is None:
            return False, Reason.DRIVER_UNLOADED, "读不到这台驱动器的方块容器"
        if not callable(getattr(adapter, "write_slot", None)):
            return False, Reason.UNSUPPORTED, "这台设备的容器适配器不支持按槽写入"
        try:
            writable = item_dict_from_key(ResourceKey.item(medium.model_id, 0), 1)
            writable["userData"] = medium_item.materialize_item_user_data(
                medium, writable.get("userData")
            )
            adapter.write_slot(slot, writable)
        except Exception as error:
            return False, Reason.CONTAINER_REFUSED, u"写入 %s 号槽时出错：%s: %s" % (
                slot,
                type(error).__name__,
                error,
            )
        try:
            after = adapter.read_slot(slot)
        except Exception as error:
            return False, Reason.CONTAINER_UNCERTAIN, u"写回后读不出 %s 号槽：%s: %s" % (
                slot,
                type(error).__name__,
                error,
            )
        carrier = medium_item.read_carrier(_user_data_of(after))
        if carrier is None or carrier[0] != medium_id:
            # 写下去了但读回来不是它——**什么都不回滚**，如实报不确定。
            return False, Reason.CONTAINER_UNCERTAIN, u"写回没有核对上（读回来是 %r）" % (
                carrier,
            )
        self.refresh(state.ref, force=True)
        if state.slot_of(medium_id) is None:
            return False, Reason.CONTAINER_UNCERTAIN, "写进去了，但重读后槽位里不是它"
        return True, "", ""

    def inspect_slot(self, state, slot):
        # type: (DriverState, int) -> object
        """**只读**地看一眼槽里那件物品的原始字典。读不到或空槽返回 `None`。

        命令层在「取出来」之前用它算去向（背包放不放得下）——
        没有这一步就得先取后算，而取出来没地方放就等于销毁那件物品。
        """
        adapter = self.container_at(state.node_key)
        if adapter is None or not callable(getattr(adapter, "read_slot", None)):
            return None
        try:
            data = adapter.read_slot(slot)
        except Exception:
            return None
        if is_empty_item(data):
            return None
        return data

    def take_medium(self, state, slot, expected_slot_version=None):
        # type: (DriverState, int, object) -> tuple
        """把某个槽里的介质物品**取出来**。返回 `(ok, reason, 物品字典)`。

        **取出来必须有去向**：本方法只把物品从容器里摘掉并原样交出来，
        调用方若不落地它（塞进背包或掉在原地），那件物品就没了。
        因此调用方必须自己保证「有一个能放下它的地方」再来取。
        """
        if not state.is_usable():
            return False, state.recovery, state.recovery_detail
        if isinstance(slot, bool) or not isinstance(slot, int):
            return False, Reason.OUT_OF_RANGE, "槽号必须是整数"
        if slot < 0 or slot >= state.slot_count:
            return False, Reason.OUT_OF_RANGE, "槽号 %r 超出 0..%d" % (
                slot,
                state.slot_count - 1,
            )
        current = state.assignment(slot)
        current_version = current.slot_version if current is not None else 0
        if expected_slot_version is not None and int(expected_slot_version) != current_version:
            return (
                False,
                Reason.STALE_VERSION,
                "槽位版本为 %d，请求依据的是 %r" % (current_version, expected_slot_version),
            )
        if current is None:
            return False, Reason.BAD_PARAMETER, "槽位 %d 本来就是空的" % (slot,)
        adapter = self.container_at(state.node_key)
        if adapter is None:
            return False, Reason.DRIVER_UNLOADED, "读不到这台驱动器的方块容器"
        try:
            data = adapter.read_slot(slot)
        except Exception as error:
            return False, Reason.CONTAINER_UNCERTAIN, u"读不出 %s 号槽：%s: %s" % (
                slot,
                type(error).__name__,
                error,
            )
        if is_empty_item(data):
            # 读数说它在这儿、容器说它是空的：以容器为准，**不当成取到了**。
            self.refresh(state.ref, force=True)
            return False, Reason.CONTAINER_UNCERTAIN, "容器里那一格已经是空的"
        try:
            adapter.write_slot(slot, None)
            after = adapter.read_slot(slot)
        except Exception as error:
            return False, Reason.CONTAINER_UNCERTAIN, u"清空 %s 号槽时出错：%s: %s" % (
                slot,
                type(error).__name__,
                error,
            )
        if not is_empty_item(after):
            return False, Reason.CONTAINER_UNCERTAIN, "取出后那一格仍然有东西"
        self.refresh(state.ref, force=True)
        return True, "", data

    def diagnostics(self):
        # type: () -> dict
        claimed = set()
        for state in self._states.values():
            claimed.update(state.medium_ids())
        report = dict(self.stats)
        report.update(
            {
                "policy": self.policy.describe(),
                "medium_policy": self.medium_policy.describe(),
                "slot_count": self.slot_count(),
                "drivers": len(self._states),
                "mediums": len(self._mediums),
                "orphans": list(self._orphans),
                #: 读不懂容器、只能沿用上一次读数的驱动器（区块未加载等）。
                "unreadable": [
                    state.node_key.as_key()
                    for state in self.states()
                    if state.unreadable
                ],
                #: **从没成功读到过**容器的驱动器：槽位与 `disk_count` 都不作数。
                "never_read": [
                    state.node_key.as_key()
                    for state in self.states()
                    if not state.has_reading()
                ],
                #: 槽里有东西但挂不上（不是介质 / 载体读不动 / 版本不认识 / 叠了多块）。
                "invalid_slots": dict(
                    (node_key_text(state.node_key), state.diagnostics()["invalid"])
                    for state in self.states()
                    if state.invalid
                ),
                #: 占了槽、但存档里没有对应记录（载体有 ID、记录缺失）。
                "orphan_slots": dict(
                    (node_key_text(state.node_key), [
                        medium_id
                        for medium_id in state.medium_ids()
                        if self.medium(medium_id) is None
                    ])
                    for state in self.states()
                    if any(
                        self.medium(medium_id) is None
                        for medium_id in state.medium_ids()
                    )
                ),
                #: 存档里有、但没有任何槽引用的介质（旧 `mount` 造的无物品介质、
                #: 被拔走之后记录还留着的盘）。**处置归 12-06 / R05**，这里只报。
                "unclaimed_mediums": sorted(
                    medium_id for medium_id in self._mediums
                    if medium_id not in claimed
                ),
                #: 读不懂、只能原样保留的介质。**它们不进任何写回路径。**
                "frozen_mediums": sorted(self._frozen_mediums),
                #: 旧存档留下的驱动器记录：**只报不物化**，见 `restore_from_repository`。
                "legacy_driver_records": dict(
                    (record_id, [item.to_dict() for item in assignments])
                    for record_id, assignments in self._legacy_slots.items()
                ),
                #: 信封读不懂、连删都不删的旧驱动器记录。
                "kept_driver_records": sorted(self._kept_driver_records),
                #: 载体还没写回成功、等下一帧重试的槽位（`(位置, 槽)` 文本）。
                "pending_carriers": dict(
                    ("%s#%d" % (key[0], key[1]), value["medium_id"])
                    for key, value in self._pending_carriers.items()
                ),
                "pending_display_writes": self.pending_display_writes(),
                "pending_archive_writes": self.pending_archive_writes(),
                #: 介质 ID 是不是持久的。`use_durable_ids()` 之后为真——
                #: 在此之前它只保证进程内单调，不能当「重进世界找回同一块盘」的依据。
                "durable_medium_ids": self._durable_ids,
            }
        )
        report.update(
            dict(("archive_" + key, value) for key, value in self.archive_stats.items())
        )
        return report

    def __repr__(self):
        return "StorageDriverStore(%d 台驱动器, %d 块介质)" % (
            len(self._states),
            len(self._mediums),
        )


def _user_data_of(data):
    # type: (object) -> object
    """引擎物品字典里的 `userData`；不是字典就返回 `None`。"""
    if not isinstance(data, dict):
        return None
    return data.get("userData")


def _item_count(data):
    # type: (object) -> int
    """引擎物品字典里的个数。读不出或不是正数时按 1。

    （与 `memory_slot._item_count` 同一份逻辑的两份副本——跨层 import 会成环。
    这里还多一个用途：**一格叠了多块介质**要被读路径认出来，见 `_read_one`。）
    """
    if not isinstance(data, dict):
        return 1
    count = data.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        return 1
    return count if count > 0 else 1


def _managed_lines_of(data):
    # type: (object) -> tuple
    """一件物品的 `display.Lore` 里**本模组管理的**那些行。

    只用于「显示有没有变」的比较——**只比管理行**，玩家自己的 Lore 一行都不看，
    否则每加一句自己的说明就会触发一次写回。
    """
    user_data = _user_data_of(data)
    if not isinstance(user_data, dict):
        return ()
    display = user_data.get(medium_item.DISPLAY_KEY)
    if not isinstance(display, dict):
        return ()
    lore = display.get(medium_item.LORE_KEY)
    if not isinstance(lore, (list, tuple)):
        return ()
    return tuple(line for line in lore if medium_item.is_managed_line(line))
