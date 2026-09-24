# coding=utf-8
"""统一容器契约与假适配器。

对应任务 13-01。

**为什么需要这一层。** 架构规划 6.3 记录了当前 `tooldelta` 的高层封装问题：
`PutItemIntoContainer` 调用 `SetContainerItem` 却不看它的布尔返回值，
在空槽路径更是直接报告「全部放入」。因此**不能把这个返回值当作物品守恒依据**。
本模块把「外部容器」抽象成一个可验证的契约，并让调用方只看到一个
经过核对的结果。

**本模块刻意不 import 任何引擎模块**，因此可以脱离游戏测试。
真实引擎适配器（基于 tooldelta / mod.server）在后续任务里另建模块引入，
方向是：引擎适配器依赖本模块的契约，本模块不依赖引擎。

三条纪律：

1. **实际数量为准。** 对外只暴露 `insert` / `extract`，它们在内部核对
   真实变化量，子类无法把「声称放了多少」直接透传给调用方。
2. **不能核对时必须说不确定。** 容器没有读取能力时返回 `UNCERTAIN`，
   不得声称成功——否则恢复流程会以为已经落袋。
3. **不确定不重放。** `UNCERTAIN` 是给恢复流程的输入，不是重试许可。
"""

from ...common.protocol import (
    ContainerCapabilities,
    Reason,
    TransferReceipt,
    TransferStatus,
)
from ...common.resource_key import ResourceKey, _require_count
from ...common import settings


class ContainerEntry(object):
    """容器中的一个资源条目。数量不属于身份，因此分开存放。"""

    __slots__ = ("resource_key", "count")

    def __init__(self, resource_key, count):
        # type: (ResourceKey, int) -> None
        if settings.DEBUG:
            if not isinstance(resource_key, ResourceKey):
                raise TypeError("entries 需要 ResourceKey，得到 %r" % (type(resource_key),))
            _require_count(count, "container count")
        self.resource_key = resource_key
        self.count = count

    def to_dict(self):
        # type: () -> dict
        return {"resource_key": self.resource_key.to_dict(), "count": self.count}

    def __repr__(self):
        return "ContainerEntry(%s x%d)" % (self.resource_key.name, self.count)


#: 子类用它表达「已经动过，但无法确认结果」。
UNCERTAIN = TransferStatus.UNCERTAIN


class ContainerAdapter(object):
    """外部容器的统一契约。

    子类只实现 `_do_insert` / `_do_extract` / `_count_of` 三个原始操作，
    对外入口 `insert` / `extract` 由基类负责核对，避免「声称成功」流出去。
    """

    #: 诊断用的适配器标识
    adapter_id = "container"
    #: 该容器是否还有效（邻接方块被拆、维度变化等会失效）
    valid = True

    # -- 能力 ----------------------------------------------------------

    def capabilities(self):
        # type: () -> ContainerCapabilities
        """保守默认：能读能写，其余一律不支持。"""
        return ContainerCapabilities()

    def describe(self):
        # type: () -> dict
        return {"adapter_id": self.adapter_id, "valid": self.valid,
                "capabilities": self.capabilities().to_dict()}

    def notify_token(self):
        # type: () -> object
        """变化通知令牌。None 表示该容器没有通知，调用方需要退避轮询。"""
        return self.capabilities().notify_token

    def is_valid(self):
        # type: () -> bool
        return self.valid

    def invalidate(self):
        # type: () -> None
        """邻接变化、卸载或依赖关闭时调用（13-05）。"""
        self.valid = False

    # -- 只读 ----------------------------------------------------------

    def slots_count(self):
        # type: () -> object
        """槽位数。None 表示不可枚举（如流体罐）。"""
        return None

    def entries(self):
        # type: () -> list
        """全部非空条目 `[(slot, ContainerEntry)]`。只读容器也要实现它。"""
        raise NotImplementedError

    def _count_of(self, resource_key):
        # type: (ResourceKey) -> int
        """容器中某种资源的实际总数，用于核对。"""
        raise NotImplementedError

    def count_of(self, resource_key):
        # type: (ResourceKey) -> object
        """对外只读查询。没有读取能力时返回 None（未知，不是 0）。"""
        if not self.capabilities().can_read:
            return None
        return self._count_of(resource_key)

    # -- 模拟 ----------------------------------------------------------

    def can_accept(self, resource_key, count):
        # type: (ResourceKey, int) -> object
        """预判能接收多少。没有模拟能力时返回 None（未知），不得返回 0 冒充。"""
        caps = self.capabilities()
        if not caps.can_simulate:
            return None
        return self._do_simulate_insert(resource_key, count)

    def _do_simulate_insert(self, resource_key, count):
        # type: (ResourceKey, int) -> int
        raise NotImplementedError

    # -- 原始操作（子类实现，允许「说错话」）----------------------------

    def _do_insert(self, op_id, resource_key, count):
        # type: (str, ResourceKey, int) -> object
        """把 count 个资源放进容器。

        返回**声称**放入的数量（int），或返回 `UNCERTAIN` 表示结果不明。
        返回值会被基类核对，因此说谎只会导致调用方拿到实际值。
        """
        raise NotImplementedError

    def _do_extract(self, op_id, resource_key, count):
        # type: (str, ResourceKey, int) -> object
        """从容器取出 count 个资源。语义同 `_do_insert`。"""
        raise NotImplementedError

    # -- 对外入口：核对后返回 -------------------------------------------

    def insert(self, op_id, resource_key, count):
        # type: (str, ResourceKey, int) -> TransferReceipt
        """放入资源，返回**经过核对**的回执。"""
        if count < 0:
            raise ValueError("数量不能为负")
        caps = self.capabilities()
        if not caps.can_insert:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED, Reason.CONTAINER_REFUSED
            )

        before = self._count_of(resource_key) if caps.can_read else None
        claimed = self._do_insert(op_id, resource_key, count)

        if claimed is UNCERTAIN:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.UNCERTAIN, Reason.CONTAINER_UNCERTAIN
            )

        if before is None:
            # 无法核对：只能照实说结果不确定，不得声称守恒。
            return TransferReceipt(
                op_id,
                count,
                0,
                TransferStatus.UNCERTAIN,
                Reason.CONTAINER_UNCERTAIN,
                resource_key,
            )

        after = self._count_of(resource_key)
        actual = after - before
        if actual < 0:
            raise AssertionError(
                "容器在插入后该资源反而减少: %+d；适配器或容器语义有误" % (actual,)
            )
        actual = min(actual, count)

        if actual == count:
            return TransferReceipt(
                op_id, count, actual, TransferStatus.COMPLETE, "", resource_key
            )
        if actual == 0:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED, Reason.CONTAINER_REFUSED, resource_key
            )
        return TransferReceipt(
            op_id, count, actual, TransferStatus.PARTIAL, Reason.CONTAINER_PARTIAL, resource_key
        )

    def extract(self, op_id, resource_key, count):
        # type: (str, ResourceKey, int) -> TransferReceipt
        """取出资源，返回**经过核对**的回执。"""
        if count < 0:
            raise ValueError("数量不能为负")
        caps = self.capabilities()
        if not caps.can_read:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.UNCERTAIN, Reason.CONTAINER_UNCERTAIN
            )

        before = self._count_of(resource_key)
        claimed = self._do_extract(op_id, resource_key, count)

        if claimed is UNCERTAIN:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.UNCERTAIN, Reason.CONTAINER_UNCERTAIN,
                resource_key,
            )

        after = self._count_of(resource_key)
        actual = before - after
        if actual < 0:
            raise AssertionError("容器在取出后该资源反而增加: %+d" % (actual,))
        actual = min(actual, count)

        if actual == count:
            return TransferReceipt(
                op_id, count, actual, TransferStatus.COMPLETE, "", resource_key
            )
        if actual == 0:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED, Reason.INSUFFICIENT_STOCK,
                resource_key,
            )
        return TransferReceipt(
            op_id, count, actual, TransferStatus.PARTIAL, Reason.CONTAINER_PARTIAL,
            resource_key,
        )


# -- 假适配器（测试替身，也是 13-06 兼容矩阵的用例载体）----------------------


class FakeContainer(ContainerAdapter):
    """基础假容器：有槽位、按槽堆叠、可读可写可模拟。"""

    adapter_id = "fake"

    def __init__(self, slots=27, max_stack=64, notify_token="fake-notify"):
        # type: (int, int, object) -> None
        self._slots = [None] * int(slots)
        self.max_stack = int(max_stack)
        self._notify = notify_token

    def capabilities(self):
        return ContainerCapabilities(
            can_read=True,
            can_insert=True,
            can_simulate=True,
            supports_partial=True,
            is_atomic=False,
            notify_token=self._notify,
            max_slots=len(self._slots),
        )

    def slots_count(self):
        return len(self._slots)

    def entries(self):
        out = []
        for index, entry in enumerate(self._slots):
            if entry is not None:
                out.append((index, entry))
        return out

    def _count_of(self, resource_key):
        total = 0
        for entry in self._slots:
            if entry is not None and entry.resource_key == resource_key:
                total += entry.count
        return total

    def _do_simulate_insert(self, resource_key, count):
        room = 0
        for entry in self._slots:
            if entry is None:
                room += self.max_stack
            elif entry.resource_key == resource_key:
                room += max(0, self.max_stack - entry.count)
        return min(room, count)

    def _do_insert(self, op_id, resource_key, count):
        remaining = count
        for index, entry in enumerate(self._slots):
            if remaining <= 0:
                break
            if entry is not None and entry.resource_key == resource_key:
                room = self.max_stack - entry.count
                if room > 0:
                    take = min(room, remaining)
                    entry.count += take
                    remaining -= take
        for index, entry in enumerate(self._slots):
            if remaining <= 0:
                break
            if entry is None:
                take = min(self.max_stack, remaining)
                self._slots[index] = ContainerEntry(resource_key, take)
                remaining -= take
        return count - remaining

    def _do_extract(self, op_id, resource_key, count):
        remaining = count
        for index, entry in enumerate(self._slots):
            if remaining <= 0:
                break
            if entry is not None and entry.resource_key == resource_key:
                take = min(entry.count, remaining)
                entry.count -= take
                remaining -= take
                if entry.count == 0:
                    self._slots[index] = None
        return count - remaining

    def set_slot(self, index, entry):
        # type: (int, ContainerEntry) -> None
        """测试用：直接摆一个条目。"""
        if not isinstance(entry, ContainerEntry):
            raise TypeError("set_slot 需要 ContainerEntry")
        self._slots[index] = entry


class GhostContainer(FakeContainer):
    """复刻 `PutItemIntoContainer` 的行为：**声称全部放入，实际没有**。

    这是架构规划 6.3 点名的缺陷形态，用来证明契约层确实拦得住它：
    调用方拿到的是核对过的实际数量，而不是容器声称的数字。
    """

    adapter_id = "ghost"

    def __init__(self, accept_into_store=0, **kwargs):
        FakeContainer.__init__(self, **kwargs)
        #: 真正愿意接收的数量；其余部分被静默丢弃
        self.accept_into_store = accept_into_store
        self.ghost_reports = 0

    def _do_insert(self, op_id, resource_key, count):
        # 真的存一点，然后照旧报告「全部放入」
        FakeContainer._do_insert(self, op_id, resource_key, self.accept_into_store)
        self.ghost_reports += 1
        return count


class ReadOnlyContainer(FakeContainer):
    """只读容器。"""

    adapter_id = "read_only"

    def capabilities(self):
        caps = FakeContainer.capabilities(self)
        caps.can_insert = False
        caps.can_simulate = False
        return caps

    def _do_insert(self, op_id, resource_key, count):
        return 0


class RefusingContainer(FakeContainer):
    """永远拒收的容器（满、被锁定、类型不符）。"""

    adapter_id = "refusing"

    def _do_insert(self, op_id, resource_key, count):
        return 0

    def _do_simulate_insert(self, resource_key, count):
        return 0


class PartialContainer(FakeContainer):
    """每次最多只接收 `per_op_limit` 个。"""

    adapter_id = "partial"

    def __init__(self, per_op_limit=1, **kwargs):
        FakeContainer.__init__(self, **kwargs)
        self.per_op_limit = int(per_op_limit)

    def _do_insert(self, op_id, resource_key, count):
        return FakeContainer._do_insert(
            self, op_id, resource_key, min(count, self.per_op_limit)
        )

    def _do_extract(self, op_id, resource_key, count):
        return FakeContainer._do_extract(
            self, op_id, resource_key, min(count, self.per_op_limit)
        )


class NoSimulateContainer(FakeContainer):
    """没有模拟能力：`can_accept` 必须返回 None（未知），不得返回 0。"""

    adapter_id = "no_simulate"

    def capabilities(self):
        caps = FakeContainer.capabilities(self)
        caps.can_simulate = False
        return caps

    def _do_simulate_insert(self, resource_key, count):
        raise AssertionError("没有模拟能力时不应调用到模拟实现")


class NoNotificationContainer(FakeContainer):
    """没有变化事件：必须给出 None 令牌，使调用方退化到退避轮询。"""

    adapter_id = "no_notification"

    def __init__(self, **kwargs):
        FakeContainer.__init__(self, notify_token=None, **kwargs)


class UnverifiableContainer(FakeContainer):
    """写得了但读不了：无法核对，结果只能是不确定。"""

    adapter_id = "unverifiable"

    def capabilities(self):
        caps = FakeContainer.capabilities(self)
        caps.can_read = False
        return caps

    def _count_of(self, resource_key):
        raise AssertionError("没有读取能力时不应调用到读取实现")


class UncertainContainer(FakeContainer):
    """结果无法确认（外部 API 超时、返回语义不明）。"""

    adapter_id = "uncertain"

    def __init__(self, **kwargs):
        FakeContainer.__init__(self, **kwargs)
        self.uncertain_calls = []

    def _do_insert(self, op_id, resource_key, count):
        self.uncertain_calls.append(("insert", count))
        return UNCERTAIN

    def _do_extract(self, op_id, resource_key, count):
        self.uncertain_calls.append(("extract", count))
        return UNCERTAIN


class InvalidatableContainer(FakeContainer):
    """邻居拆除后失效的容器（13-05）。"""

    adapter_id = "invalidatable"

    def entries(self):
        if not self.valid:
            raise IOError("容器已失效，不应再被读取")
        return FakeContainer.entries(self)
