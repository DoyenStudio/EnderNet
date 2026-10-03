# coding=utf-8
"""存储驱动器设备：槽位状态、介质归属与真实的存储提供者实现。

对应任务 05-04 的驱动器侧，以及 12-03 需要的可序列化结构。
架构要求见 `plans/technical_notes/script_architecture.md` §3（数据所有权）、
§4（必须接上的现有入口）、§5.1–5.2（设备与介质模型、生命周期）。

**这一层不是第二个设备注册表。** 位置、区块、朝向、加载状态全部归
`DeviceRegistry` 管；这里只保存「这台驱动器装了哪几块盘」这一件
注册表不知道的事，索引键是 `DeviceRef`（带实例代次），因此同位置换方块
之后旧状态自然失效。

三条必须成立的性质：

1. **状态跟随设备生命周期，但不跟随网络。** 驱动器状态由启动层持有，
   网络断开、主控移除、运行时重建都不等于设备和介质消失。
2. **槽位是权威，`disk_count` 是投影。** 带宽准入读的是
   `DeviceRecord.config["disk_count"]`，它**只能**由 `project()` 写入；
   任何命令载荷都改不了它。
3. **介质对象从不被删除。** 卸载只清槽位引用，拆除只标 `orphaned`。
   因此网络侧的索引不可能指向一个已经被释放的对象。
"""

from ...common.definitions import UNCONFIGURED
from ...common.identity import DeviceRef, NodeKey, node_key_text  # noqa: F401
from ...common.protocol import (
    ContainerCapabilities,
    Reason,
    TransferReceipt,
    TransferStatus,
)
from ...common.resource_key import ResourceKey  # noqa: F401 (仅用于 # type: 注释)
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

#: 驱动器存档结构版本。
DRIVER_SCHEMA_VERSION = 1

#: 存档记录类型。任务 12 接真实后端时按它取记录。
DRIVER_RECORD_KIND = "endernet.storage_driver"
MEDIUM_RECORD_KIND = "endernet.storage_medium"

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
                "note": "区块重载后是否按持久身份恢复槽位；替身取是",
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
    """一个槽位里装着什么。空槽用 `None` 表示，不用一个空对象。"""

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
    """

    __slots__ = (
        "ref",
        "node_key",
        "device_id",
        "slot_count",
        "slots",
        "config_version",
        "loaded",
        "recovery",
        "recovery_detail",
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
        #: 槽位配置版本。任何一次装入/取出都推进它，供请求做旧版本拒绝。
        self.config_version = 0
        self.loaded = True
        #: 恢复状态。`UNCONFIGURED` 槽数也记在这里。
        self.recovery = "ok"
        self.recovery_detail = ""
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
        """槽位配置读得出来才谈得上装载。"""
        return self.recovery == "ok"

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

    # -- 槽位变更 ------------------------------------------------------

    def assign(self, slot, medium_id, expected_slot_version=None, tick=0):
        # type: (int, str, int, int) -> tuple
        """把一个介质放进槽位。返回 `(ok, reason, detail)`。

        **不查介质是否存在**：那是持有者（`StorageDriverStore`）的事，
        本对象只管槽位。校验顺序：范围 → 同盘重复 → 旧版本 → 占用。
        """
        if not self.is_usable():
            return False, self.recovery, self.recovery_detail
        if isinstance(slot, bool) or not isinstance(slot, int):
            return False, Reason.OUT_OF_RANGE, "槽号必须是整数"
        if slot < 0 or slot >= len(self.slots):
            return False, Reason.OUT_OF_RANGE, "槽号 %r 超出 0..%d" % (
                slot,
                len(self.slots) - 1,
            )
        if self.slot_of(medium_id) is not None:
            return False, Reason.MEDIUM_DUPLICATE_MOUNT, "该介质已经装在本驱动器上"

        current = self.slots[slot]
        current_version = current.slot_version if current is not None else 0
        if expected_slot_version is not None and int(expected_slot_version) != current_version:
            return (
                False,
                Reason.STALE_VERSION,
                "槽位版本为 %d，请求依据的是 %r" % (current_version, expected_slot_version),
            )
        if current is not None:
            return False, Reason.SLOT_OCCUPIED, "槽位 %d 已被 %s 占用" % (
                slot,
                current.medium_id,
            )

        self.slots[slot] = SlotAssignment(slot, medium_id, current_version + 1, tick)
        self.config_version += 1
        return True, "", ""

    def clear(self, slot, expected_slot_version=None):
        # type: (int, int) -> tuple
        """清空槽位。返回 `(ok, reason, detail)`；被清掉的分配从 detail 透出。"""
        if not self.is_usable():
            return False, self.recovery, self.recovery_detail
        if isinstance(slot, bool) or not isinstance(slot, int):
            return False, Reason.OUT_OF_RANGE, "槽号必须是整数"
        if slot < 0 or slot >= len(self.slots):
            return False, Reason.OUT_OF_RANGE, "槽号 %r 超出 0..%d" % (
                slot,
                len(self.slots) - 1,
            )
        current = self.slots[slot]
        if current is None:
            return False, Reason.BAD_PARAMETER, "槽位 %d 本来就是空的" % (slot,)
        if expected_slot_version is not None and int(expected_slot_version) != current.slot_version:
            return (
                False,
                Reason.STALE_VERSION,
                "槽位版本为 %d，请求依据的是 %r"
                % (current.slot_version, expected_slot_version),
            )
        self.slots[slot] = None
        self.config_version += 1
        return True, "", current.medium_id

    def clear_all(self):
        # type: () -> tuple
        """清空全部槽位（拆除用）。返回被清掉的介质 ID。"""
        out = []
        for index in range(len(self.slots)):
            if self.slots[index] is not None:
                out.append(self.slots[index].medium_id)
                self.slots[index] = None
        if out:
            self.config_version += 1
        return tuple(out)

    # -- 序列化 --------------------------------------------------------

    def to_dict(self):
        # type: () -> dict
        return {
            "schema_version": DRIVER_SCHEMA_VERSION,
            "node_key": self.node_key.to_dict(),
            "generation": self.ref.generation,
            "device_id": self.device_id,
            "slot_count": (
                self.slot_count if isinstance(self.slot_count, int) else None
            ),
            "config_version": self.config_version,
            "slots": [item.to_dict() for item in self.occupied()],
        }

    @classmethod
    def from_dict(cls, data, policy, store, device_id=DRIVER_DEVICE_ID):
        # type: (dict, DriverPolicy, object, str) -> DriverState
        """从存档还原槽位。

        占位介质**不在这里恢复**：本对象只管槽位引用，介质内容由
        `StorageDriverStore` 的介质表负责。因此这里只重建引用，
        调用方随后要核对每一块盘都存在（缺失的保持引用但不参与交易）。
        """
        ref = DeviceRef(NodeKey.from_dict(data["node_key"]), data["generation"])
        state = cls(ref, policy, store, device_id=device_id, slot_count=data["slot_count"])
        state.config_version = int(data.get("config_version", 0))
        for item in data.get("slots", ()):
            assignment = SlotAssignment.from_dict(item)
            if 0 <= assignment.slot < len(state.slots):
                state.slots[assignment.slot] = assignment
        return state

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

    def mount(self, slot, medium_id, expected_slot_version=None, tick=0):
        # type: (int, str, object, int) -> tuple
        """装入。返回 `(ok, reason, detail)`。

        **成功后立刻投影并通知负载重算**。放在这里而不是交给调用方，
        是因为「先改计数、后交易」是准入的前提：一旦有一条调用路径漏了这步，
        后续交易就会按旧准入放行。
        """
        # **跨驱动器**同 ID 去重。本机重复由 `DriverState.assign` 自己拦
        # （`slot_of`），但两台驱动器各插一块同 ID 的盘在它那里是合法的——
        # 那会让一份内容被记两次，而且记账本身不会报错。
        owner = self.store.medium_slot_owner(medium_id)
        if owner is not None:
            other_state, assignment = owner
            if other_state is not self.state or assignment.slot != slot:
                return (
                    False,
                    Reason.MEDIUM_DUPLICATE_MOUNT,
                    "介质 %s 已装在 %s 的 %d 号槽；同一块盘不能挂两处"
                    % (medium_id, node_key_text(other_state.node_key), assignment.slot),
                )
        ok, reason, detail = self.state.assign(
            slot, medium_id, expected_slot_version, tick
        )
        if ok:
            self.store.project(self.state)
            self.store.note_slots_changed(self.state)
        return ok, reason, detail

    def unmount(self, slot, expected_slot_version=None):
        # type: (int, object) -> tuple
        """取出。返回 `(ok, reason, 被取出的介质 ID)`。"""
        ok, reason, detail = self.state.clear(slot, expected_slot_version)
        if ok:
            self.store.project(self.state)
            self.store.note_slots_changed(self.state)
        return ok, reason, detail

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
    ):
        # type: (object, object, DriverPolicy, MediumPolicy, object, object) -> None
        self.definitions = definitions
        self.registry = registry
        self.policy = policy if policy is not None else DriverPolicy()
        self.medium_policy = (
            medium_policy if medium_policy is not None else MediumPolicy()
        )
        #: 槽位变化后通知带宽重算的钩子（`AdmissionService.note_device_load_changed`）。
        self.on_load_changed = on_load_changed
        #: 介质 ID 的分配器。**默认实现只保证进程内单调**——它不是持久身份，
        #: 任务 12 接真实后端时必须换成从存档推导的来源（架构 §4）。
        self.id_source = id_source if id_source is not None else self._next_medium_id

        self._states = {}
        self._mediums = {}
        self._id_counter = 0
        self._orphans = []
        #: 还没写进存档的介质 ID 与驱动器 `node_key` 文本（12-03）。
        #: **谁改状态谁标脏**，不由调用方补——漏标一次的表现是「改了没存」，
        #: 而那种缺陷要等到重进世界才看得见。
        self._dirty_mediums = set()
        self._dirty_drivers = set()
        #: 已拆除、**待从存档里删掉**的驱动器。
        self._removed_drivers = set()
        #: 从存档读回、还没等到对应设备被区块发现时重挂的槽位。
        #: `node_key 文本 -> [SlotAssignment, ...]`
        self._pending_slots = {}
        #: **读不懂**、只能原样保留的介质 ID。两个来源：记录信封版本过新
        #: （`Record.read_only`），或载荷本身读不出（`MediumRecovery` 非 OK，
        #: 含型号缺失与损坏）。它们照样接管进内存（内容不能丢），但
        #: **永远不标脏、不写回**。
        #:
        #: 第二条是**必须的**，不能只看 `read_only`：信封版本按
        #: `CURRENT_SCHEMA_VERSION` 写，而载荷版本是 `MEDIUM_SCHEMA_VERSION`，
        #: 两者可以不同步；`Medium.to_dict()` 又**没有 `raw_payload` 回退**。
        #: 只按信封判的话，一块「载荷读不懂」的盘一旦被 `drop()` 标脏，
        #: 写回去的就是一份 `entries: []` 的**空盘**——内容直接没了。
        self._frozen_mediums = set()
        #: 同上，但对象是驱动器记录：信封版本过新时槽位照常按最好猜测应用，
        #: 但**不写回**——写回会把只存在于新版本里的字段抹掉。
        self._frozen_drivers = set()
        #: 存档里有、但按当前槽数放不下的槽位（R10 槽数改小、或槽数未配置）。
        #: **保留而不截断**，逐条报在诊断里。
        self._unplaced_slots = {}
        #: 介质 ID 是不是「从存档推导」的持久身份（12-02 定案）。
        #: 默认是进程内替身，`use_durable_ids()` 之后才为真。
        self._durable_ids = False
        self.archive_stats = {
            "medium_writes": 0,
            "driver_writes": 0,
            "driver_deletes": 0,
            "restored_mediums": 0,
            "restored_drivers": 0,
        }
        self.stats = {
            "created": 0,
            "dropped": 0,
            "load_marks": 0,
            "mediums_created": 0,
            "mediums_adopted": 0,
            "projections": 0,
        }

    # -- 驱动器状态 ----------------------------------------------------

    def slot_count(self):
        # type: () -> object
        return self.policy.resolve_slot_count(self.definitions)

    def ensure(self, ref):
        # type: (object) -> DriverState
        """取或建一台驱动器的状态。同一代次重复调用是幂等的。"""
        existing = self.get_by_ref(ref)
        if existing is not None:
            return existing
        record = self.registry.get_by_ref(ref)
        device_id = record.device_id if record is not None else DRIVER_DEVICE_ID
        state = DriverState(ref, self.policy, self, device_id, self.slot_count())
        self._states[ref.node_key.as_key()] = state
        self.stats["created"] += 1
        self._apply_pending_slots(state)
        return state

    def _apply_pending_slots(self, state):
        # type: (DriverState) -> int
        """把存档里等着这台设备的槽位装上。**只认位置，不认代次。**

        代次不持久（`GenerationAllocator` 重启后从 0 重来），所以按
        `DeviceRef` 存槽位的话，重进世界时查不到、槽位被静默丢掉——
        「盘还在驱动器里、东西却没了」正是这样发生的。

        **放不下的槽位保留、不截断、也不写回**（12 验收对样板槽的同一条纪律：
        「容量或类型不兼容时保留原资源并进入待处理状态，不能静默截断、丢弃」）。
        这里有两种放不下：槽数被改小（R10 未定案），或槽数本身没配置
        （`UNCONFIGURED` 时 `slots` 是空表）。留在 `_pending_slots` 里等
        下次设备被重新发现时再试，并计入 `unplaced_slots` 诊断。

        **不标脏**：标脏会让下一次 `save_to_repository` 把「当前装得下的
        那几个槽」写回去，档案里那条放不下的引用就**永久没了**。
        档案维持原样，冲突如实报出来等人处置。
        """
        key = node_key_text(state.node_key)
        assignments = self._pending_slots.get(key)
        if not assignments:
            return 0
        applied = 0
        leftover = []
        for assignment in assignments:
            if 0 <= assignment.slot < len(state.slots) and state.slots[assignment.slot] is None:
                state.slots[assignment.slot] = assignment
                applied += 1
            else:
                leftover.append(assignment)
        if applied:
            self.project(state)
        if leftover:
            self._pending_slots[key] = leftover
            self._unplaced_slots[key] = len(leftover)
        else:
            self._pending_slots.pop(key, None)
            self._unplaced_slots.pop(key, None)
        return applied

    def get(self, node_key):
        # type: (object) -> DriverState
        state = self._states.get(node_key.as_key())
        return state if state is not None and state.device_id == DRIVER_DEVICE_ID else None

    def get_by_ref(self, ref):
        # type: (object) -> DriverState
        """按实例代次取。**代次不符即失效**——这是延迟任务的自毁检查。"""
        state = self._states.get(ref.node_key.as_key())
        if state is None or state.ref.generation != ref.generation:
            return None
        return state

    def drop(self, ref):
        # type: (object) -> DriverState
        """拆除：清空槽位、把介质标为失去归属。**不删除介质**。

        本轮**不自行决定**掉落还是保管（架构 §6：与 R05、12-03/06 联动），
        只把它们标成 `orphaned` 并留在介质表里，等后续任务定案。
        """
        state = self.get_by_ref(ref)
        if state is None:
            return None
        key = node_key_text(state.node_key)
        for medium_id in state.clear_all():
            medium = self._mediums.get(medium_id)
            if medium is not None:
                medium.mark_orphaned("驱动器已拆除")
                self._orphans.append(medium_id)
                # 失去归属是内容之外的**状态变化**，同样要落盘：
                # 不然重进世界后它会以「还挂在槽里」的样子回来。
                # 版本过新的那些照旧不写（见 `_frozen_mediums`）。
                self.note_medium_changed(medium_id)
        self._states.pop(ref.node_key.as_key(), None)
        # 驱动器记录**删掉而不是写空**：同位置重放的新实例不得继承旧引用
        # （12 验收「同一位置拆后重放……不会自动继承」）。介质记录留着，
        # 它们已经标了孤儿——「拆除后介质怎么处置」是 R05/12-06，不在本轮。
        self._dirty_drivers.discard(key)
        self._pending_slots.pop(key, None)
        self._unplaced_slots.pop(key, None)
        # 信封读不懂的那些**连删都不删**：那份存档不是本构建写的，
        # 凭什么断定「这台驱动器现在该从档案里消失」。
        if key not in self._frozen_drivers:
            self._removed_drivers.add(key)
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
            if change.device_id != DRIVER_DEVICE_ID:
                return 0
            state = self.ensure(change.ref)
            self.project(state)
            return 1
        if kind == ChangeKind.REMOVED:
            if change.device_id != DRIVER_DEVICE_ID:
                return 0
            state = self.drop(change.ref)
            if state is None:
                return 0
            return 1
        if kind in (ChangeKind.LOADED, ChangeKind.UNLOADED):
            if change.device_id != DRIVER_DEVICE_ID:
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

        **跨驱动器去重的唯一依据。** `DriverState.assign` 只查本机
        （`slot_of` 看的是自己的 `slots`），两台驱动器各插一块同 ID 的盘
        在它那里是合法的——那会让一份内容被记两次，而记账本身不会报错。
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

    # -- 存档 ----------------------------------------------------------

    def save_all(self):
        # type: () -> dict
        """导出全部驱动器状态与介质载荷。**纯数据**，不含对象引用。"""
        return {
            "schema_version": DRIVER_SCHEMA_VERSION,
            "drivers": [state.to_dict() for state in self.states()],
            "mediums": [
                self._mediums[key].to_dict() for key in sorted(self._mediums)
            ],
        }

    def restore(self, payload):
        # type: (dict) -> dict
        """从 `save_all()` 的输出恢复。返回统计。

        **不重建位置索引**：位置由注册表的区块发现路径恢复，
        这里只把槽位引用与介质内容装回去，随后由 `add_node` 幂等挂载。
        """
        if not isinstance(payload, dict):
            return {"drivers": 0, "mediums": 0}
        mediums = 0
        for item in payload.get("mediums", ()):
            self.load_medium(item)
            mediums += 1
        drivers = 0
        for item in payload.get("drivers", ()):
            state = DriverState.from_dict(item, self.policy, self)
            self._states[state.node_key.as_key()] = state
            drivers += 1
        return {"drivers": drivers, "mediums": mediums}

    # -- 真实后端（12-03）-----------------------------------------------

    def note_medium_changed(self, medium_id):
        # type: (str) -> None
        """标脏一块介质。**谁改内容谁调**，不由扫描兜底。

        版本过新的记录**跳过**：它只能原样保留（见 `_frozen_mediums`）。
        """
        if medium_id in self._mediums and medium_id not in self._frozen_mediums:
            self._dirty_mediums.add(medium_id)

    def note_slots_changed(self, state):
        # type: (DriverState) -> None
        """标脏一台驱动器的槽位。信封版本过新的那些**跳过**（见 `_frozen_drivers`）。"""
        key = node_key_text(state.node_key)
        if key not in self._frozen_drivers:
            self._dirty_drivers.add(key)

    def pending_archive_writes(self):
        # type: () -> int
        """还没写进存档的记录数。诊断与预算用。"""
        return (
            len(self._dirty_mediums)
            + len(self._dirty_drivers)
            + len(self._removed_drivers)
        )

    def save_to_repository(self, repository, budget=None):
        # type: (object, int) -> dict
        """把脏的介质与驱动器写进存档。返回本批写了多少条。

        **`disk_count` 不写**：它是权威槽位的投影（`project()` 是唯一写入点），
        存下来再读回去等于给投影造了第二份真相。

        读取器那边只认 `Record.payload`，所以这里写的就是 `to_dict()` 的原样。
        """
        written = 0
        work = [(MEDIUM_RECORD_KIND, rid) for rid in sorted(self._dirty_mediums)]
        work += [(DRIVER_RECORD_KIND, rid) for rid in sorted(self._dirty_drivers)]
        # 驱动器按位置文本建一次索引。逐条线性扫 `_states` 的话，
        # 一批 n 台就是 O(n²)——驱动器台数上去之后这里是纯浪费。
        drivers_by_text = dict(
            (node_key_text(state.node_key), state) for state in self._states.values()
        )

        for kind, record_id in work:
            if budget is not None and written >= budget:
                break
            if kind == MEDIUM_RECORD_KIND:
                if record_id in self._frozen_mediums:
                    # 第二道闸：冻结判断在恢复期就做完了，这里再挡一次，
                    # 免得将来多出一条标脏路径就绕过去了。
                    self._dirty_mediums.discard(record_id)
                    continue
                medium = self._mediums.get(record_id)
                payload = None if medium is None else medium.to_dict()
                dirty = self._dirty_mediums
            else:
                if record_id in self._frozen_drivers:
                    self._dirty_drivers.discard(record_id)
                    continue
                state = drivers_by_text.get(record_id)
                payload = None if state is None else state.to_dict()
                dirty = self._dirty_drivers
            if payload is not None:
                repository.write(Record(kind, record_id, payload))
                if kind == MEDIUM_RECORD_KIND:
                    self.archive_stats["medium_writes"] += 1
                else:
                    self.archive_stats["driver_writes"] += 1
            # **写成功之后才摘脏。** `repository.write` 抛异常（故障注入、
            # 版本过新）时这一行走不到，记录留在脏里等下一轮重试——
            # 先摘脏再写会让那次改动永远丢掉。
            dirty.discard(record_id)
            written += 1

        for record_id in sorted(self._removed_drivers):
            if budget is not None and written >= budget:
                break
            repository.delete(DRIVER_RECORD_KIND, record_id)
            self._removed_drivers.discard(record_id)
            self.archive_stats["driver_deletes"] += 1
            written += 1

        return written

    def restore_from_repository(self, repository):
        # type: (object) -> dict
        """从存档读回介质与驱动器槽位。返回统计。

        **介质立刻接管，槽位延后到设备被区块发现时再挂**：位置是稳定的，
        而设备代次不是（`GenerationAllocator` 重启后从 0 重来），所以
        槽位进 `_pending_slots`，等 `ensure()` 拿到新代次时按 `node_key` 装上。

        介质记录读不懂照样接管（内容与原始载荷都保留），只是不可交易——
        `Medium.from_dict` 已经给出那个语义，这里不重复判断。

        **没有预算参数：恢复是一次读完的。** 分帧恢复属 12-05
        （「阶段未完成不接受新存取」那套闸门），本轮不做。因此**大批量存档
        会在启动那一帧堆出一次尖峰**——本轮只验过 1~2 块介质，规模没有读数。
        """
        stats = {"mediums": 0, "drivers": 0, "missing": 0, "reused_ids": 0, "frozen": 0}

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
            # 恢复循环里逃出去，后面**连槽位都恢复不了**。
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

        restored_slots = []
        for record_id in repository.list_ids(DRIVER_RECORD_KIND):
            record = repository.load(DRIVER_RECORD_KIND, record_id)
            if record is None or not isinstance(record.payload, dict):
                stats["missing"] += 1
                continue
            if record.read_only:
                self._frozen_drivers.add(record_id)
                stats["frozen"] += 1
            assignments = []
            for item in record.payload.get("slots", ()):
                try:
                    assignments.append(SlotAssignment.from_dict(item))
                except (KeyError, TypeError, ValueError):
                    continue
            if assignments:
                self._pending_slots[record_id] = assignments
                restored_slots.extend(assignments)
            stats["drivers"] += 1

        # 槽位引用到的介质**没有记录**：那只可能是存档不完整，不是「空盘」。
        # 这里只如实计数——引用本身保留在 `_pending_slots` 里，装进驱动器后
        # 那块介质查不到，`StorageDriverProvider._transfer` 会以
        # `MEDIUM_UNRECOVERED` 拒绝交易（`medium is None` 那条分支）。
        # `orphaned` 标记**不在这里改**：它来自记录，是拆除时写下的结论。
        #
        # 只数**本次**读回来的那批：把 `_pending_slots` 整个扫一遍会把
        # 之前几次恢复留下的、还没等到设备被发现的引用重复计进去。
        for assignment in restored_slots:
            if assignment.medium_id not in self._mediums:
                stats["missing"] += 1

        self.archive_stats["restored_mediums"] += stats["mediums"]
        self.archive_stats["restored_drivers"] += stats["drivers"]
        return stats

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report.update(
            {
                "policy": self.policy.describe(),
                "medium_policy": self.medium_policy.describe(),
                "slot_count": self.slot_count(),
                "drivers": len(self._states),
                "mediums": len(self._mediums),
                "orphans": list(self._orphans),
                "pending_slots": sorted(self._pending_slots),
                #: 读不懂、只能原样保留的介质与驱动器。**它们不进任何写回路径。**
                "frozen_mediums": sorted(self._frozen_mediums),
                "frozen_drivers": sorted(self._frozen_drivers),
                #: 存档里有、但按当前槽数放不下的槽位。**保留未截断**，等人处置。
                "unplaced_slots": dict(self._unplaced_slots),
                "pending_archive_writes": self.pending_archive_writes(),
                #: 介质 ID 是不是持久的。`use_durable_ids()` 之后为真——
                #: 在此之前它只保证进程内单调，不能当「重进世界找回同一块盘」的依据。
                "durable_medium_ids": self._durable_ids,
            }
        )
        report.update(dict(("archive_" + key, value) for key, value in self.archive_stats.items()))
        return report

    def __repr__(self):
        return "StorageDriverStore(%d 台驱动器, %d 块介质)" % (
            len(self._states),
            len(self._mediums),
        )
