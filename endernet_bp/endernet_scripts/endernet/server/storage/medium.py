# coding=utf-8
"""单个存储介质的权威状态、容量核算与共享读写额度。

对应任务 05-01/05-02/05-03 的介质部分。参考 AE2 的
`me/storage/` 下的 cell 与 `IStorageCell`。

**同一介质只有一份权威状态**（架构规划 §3 的所有权表）。驱动器不复制它的内容，
网络服务的汇总索引是派生量，卸载只解除贡献、不清空介质。因此这一层不持有
任何网络、位置或会话概念：给它一个 `medium_id` 与一份规格，它就只是一块盘。

三条必须成立的性质：

1. **数量整数、无浮点累计。** 容量与用量都以「微单位物」记账
   （物品 1 个 = 50，流体 1 mB = 1，见 `common/resource_key.py`）。
   25 mB 因此不会被截断成 0，也不会攒出浮点误差。
2. **模拟不改任何东西。** `plan()` 是纯函数；只有 `commit(plan)` 落账。
   验收要求「模拟、失败、重复请求不消耗实际库存或预算」，靠的就是这条。
3. **额度是所有调用者共享的**（05-03）。多个接口、终端和订单从同一块盘取货时，
   谁也拿不到第二份额度。
"""

from ...common.definitions import MediumSpec  # noqa: F401 (仅用于 # type: 注释)
from ...common.protocol import Reason
from ...common.resource_key import (
    MICRO_UNITS_PER_UNIT,
    ResourceKey,
    _INT_TYPES,
    _require_count,
)


#: 介质存档结构版本。写入 `to_dict()`，由任务 12 的迁移器读懂。
MEDIUM_SCHEMA_VERSION = 1

#: 转移方向。写成常量而不是布尔，是因为调用点读起来要能自解释。
DIRECTION_READ = "read"
DIRECTION_WRITE = "write"

DIRECTIONS = (DIRECTION_READ, DIRECTION_WRITE)


class TransferBudget(object):
    """一次转移的**额度时基与记账上下文**。

    `devices/capabilities.py` 的 `StorageProvider.read(medium_id, resource_key,
    count, budget)` 只声明了有个 `budget` 参数，没有规定它的形状。本类就是
    那个形状的第一个实现：额度判定必须知道「现在是第几逻辑帧」，而这个读数
    只能由服务层从 `context["tick"]` 传下来——**介质自己不数帧**，
    否则会与逻辑时钟漂移。
    """

    __slots__ = ("now_tick", "op_id", "owner_id")

    def __init__(self, now_tick=0, op_id="", owner_id=None):
        # type: (int, str, str) -> None
        self.now_tick = int(now_tick)
        self.op_id = op_id
        self.owner_id = owner_id

    def to_dict(self):
        # type: () -> dict
        return {
            "now_tick": self.now_tick,
            "op_id": self.op_id,
            "owner_id": self.owner_id,
        }

    def __repr__(self):
        return "TransferBudget(tick=%d, op=%s)" % (self.now_tick, self.op_id or "-")


class MediumRecovery(object):
    """介质记录的恢复状态。

    **非 OK 不等于空盘。** 内容与原始载荷一律保留，只是禁止交易——
    把它当空盘覆盖掉，等于替玩家决定那些东西没了。
    """

    OK = "ok"
    #: 存档版本不是本进程能理解的那一版（过新，或缺少版本字段）。
    UNKNOWN_SCHEMA = "unknown_schema"
    #: 记录里写的型号不在定义表中（模组降级、内容包缺失）。
    MISSING_MODEL = "missing_model"
    #: 载荷结构本身无法解析，或内部读数互相矛盾。同样原样保留。
    CORRUPT = "corrupt"


#: 恢复状态 -> 交易被拒时的**具体**原因文本。`OK` 不在表里（它不会被拒）。
#: 把它与协议原因码分开：`MEDIUM_UNRECOVERED` 是给程序判断的，
#: 这里是给人看「到底是哪一种读不懂」。
RECOVERY_DETAILS = {
    MediumRecovery.UNKNOWN_SCHEMA: "介质记录版本无法识别",
    MediumRecovery.MISSING_MODEL: "介质型号未注册",
    MediumRecovery.CORRUPT: "介质记录损坏",
}


class MediumPolicy(object):
    """介质的未定案旋钮（R07 / R08，以及 05 与 12 的接缝）。

    **做成具名策略而不是常数**，因为这几条都还没有定案（总览全局约束 7）：
    写死一个值等于用临时实现把玩法定下来。离线测试可以显式注入别的值来
    验证两种取法；正式验收前必须先把规则定完。

    `assert_settled()` 只应在正式验收时调用——开发与离线测试**不应**调用它。
    """

    __slots__ = (
        "shares_read_write_budget",
        "counts_fluid_as_type",
        "bucket_seconds",
        "frames_per_second",
        "recharge_while_offline",
    )

    def __init__(
        self,
        shares_read_write_budget=False,
        counts_fluid_as_type=False,
        bucket_seconds=1,
        frames_per_second=20,
        recharge_while_offline=True,
    ):
        # type: (bool, bool, int, int, bool) -> None
        #: R07：读与写是共用一个额度桶，还是各自独立。
        #: 替身取「各自独立」——两种取法都合理，尚未定案。
        self.shares_read_write_budget = bool(shares_read_write_budget)
        #: R07：流体是否占用 `max_types` 配额。替身取「不占」。
        self.counts_fluid_as_type = bool(counts_fluid_as_type)
        #: 额度桶的容量，以「几秒的吞吐量」计。
        #: 它决定空转之后最多能一次性取走多少，因此是数值的一部分而不是实现细节。
        self.bucket_seconds = int(bucket_seconds)
        #: R08：逻辑帧与秒的换算。**实测两个引擎时钟分别是约 30 Hz 与约 17.3 Hz**，
        #: 因此这里不能写死任何一个——替身取 20，离线测试注入 1 就得到
        #: 「1 帧 = 1 秒」的假时钟。
        self.frames_per_second = int(frames_per_second)
        #: 05/12 接缝：两次访问之间隔了很久（离线、区块未加载）时，
        #: 额度是否按经过的逻辑帧惰性补发。**无论怎么取都不会超过一个桶**——
        #: 桶容量就是上限，所以它不构成刷额度路径。
        #: 取 False 时只有当前这一帧计入，长期空转不再补发。
        self.recharge_while_offline = bool(recharge_while_offline)

        if self.bucket_seconds <= 0:
            raise ValueError("bucket_seconds 必须为正，得到 %r" % (self.bucket_seconds,))
        if self.frames_per_second <= 0:
            raise ValueError(
                "frames_per_second 必须为正，得到 %r" % (self.frames_per_second,)
            )

    def unresolved_report(self):
        # type: () -> list
        """仍带未定标签的字段。与 `BandwidthPolicy` / `SchedulerPolicy` 同形。"""
        return [
            {
                "field": "medium.shares_read_write_budget",
                "rule": "R07",
                "value": self.shares_read_write_budget,
                "note": "读写额度是共用一桶还是各自独立；替身取各自独立",
            },
            {
                "field": "medium.counts_fluid_as_type",
                "rule": "R07",
                "value": self.counts_fluid_as_type,
                "note": "流体是否占用 max_types 配额；替身取不占",
            },
            {
                "field": "medium.bucket_seconds",
                "rule": "R07",
                "value": self.bucket_seconds,
                "note": "额度桶容量按几秒吞吐量计；替身取 1",
            },
            {
                "field": "medium.frames_per_second",
                "rule": "R08",
                "value": self.frames_per_second,
                "note": "逻辑帧与秒的换算；实测约 30 Hz 与约 17.3 Hz 两个时钟，替身取 20",
            },
            {
                "field": "medium.recharge_while_offline",
                "rule": "05/12",
                "value": self.recharge_while_offline,
                "note": "离线期间是否惰性补发额度；替身取是（上限为一个桶）",
            },
        ]

    def assert_settled(self):
        # type: () -> None
        """正式验收用：仍有未定项就报错。

        **开发与离线测试不应调用它**——它拒绝的是「现在就宣布这些数值是定案」，
        而不是「带着未定项跑起来」。
        """
        pending = self.unresolved_report()
        if pending:
            raise AssertionError(
                "介质策略仍有 %d 项未定案，不能作为正式玩法交付：\n  " % (len(pending),)
                + "\n  ".join(
                    "%s（规则 %s，当前值 %r）"
                    % (item["field"], item["rule"], item["value"])
                    for item in pending
                )
            )

    def describe(self):
        # type: () -> dict
        return {
            "shares_read_write_budget": self.shares_read_write_budget,
            "counts_fluid_as_type": self.counts_fluid_as_type,
            "bucket_seconds": self.bucket_seconds,
            "frames_per_second": self.frames_per_second,
            "recharge_while_offline": self.recharge_while_offline,
        }


class TokenBucket(object):
    """有限桶 + **保留不足一件物品的累积余数**（05-03）。

    整数记账，没有浮点：余数以分子形式跨帧累积，
    `credit = (numerator + elapsed * rate) // fps`，`numerator -= credit * fps`。
    因此 4 单位物/s 的介质在 3 帧/秒的假时钟下**每帧记 66 微单位而不是 0**，
    三帧后刚好凑成一件物品。写成 `int(rate / fps)` 会把这些余数丢掉，
    结果是低速介质永远取不出东西。
    """

    __slots__ = (
        "capacity_micro",
        "rate_micro_per_second",
        "balance_micro",
        "numerator",
        "last_tick",
        "granted_micro",
        "denied",
    )

    def __init__(self, capacity_micro, rate_micro_per_second, now_tick=0):
        # type: (int, int, int) -> None
        _require_count(capacity_micro, "bucket capacity")
        _require_count(rate_micro_per_second, "bucket rate")
        self.capacity_micro = capacity_micro
        self.rate_micro_per_second = rate_micro_per_second
        #: **初始额度为零**。05 验收要求在「初始额度为零的假时钟」中验证累积，
        #: 一开始就给满一桶会让那条验收无从复现。
        self.balance_micro = 0
        self.numerator = 0
        self.last_tick = int(now_tick)
        #: 累计放行过多少（诊断用，不参与任何判定）
        self.granted_micro = 0
        #: 因额度不足被拒的次数
        self.denied = 0

    def _accrual(self, now_tick, allow_lazy):
        # type: (int, bool) -> int
        """从 `last_tick` 到 `now_tick` 之间应计的分子量。"""
        if now_tick <= self.last_tick:
            # 时钟回退（退出世界再进来，逻辑帧从头数）**不发放任何额度**。
            # 否则「重进一次就把桶灌满」就是一条刷额度路径。
            return 0
        elapsed = now_tick - self.last_tick
        if not allow_lazy and elapsed > 1:
            # 不补发离线额度：只认当前这一帧。
            elapsed = 1
        return elapsed * self.rate_micro_per_second

    def peek(self, now_tick, frames_per_second, allow_lazy=True):
        # type: (int, int, bool) -> int
        """**纯函数**：算出到 `now_tick` 为止可用多少额度，不改任何状态。

        预演走它，因此「模拟不消耗预算」是结构性的，而不是靠调用方自觉。
        """
        _require_count(frames_per_second, "frames_per_second")
        total = self.numerator + self._accrual(now_tick, allow_lazy)
        return min(
            self.capacity_micro,
            self.balance_micro + total // int(frames_per_second),
        )

    def refill(self, now_tick, frames_per_second, allow_lazy=True):
        # type: (int, int, bool) -> int
        """推进到 `now_tick`，返回推进后的余额。"""
        _require_count(frames_per_second, "frames_per_second")
        if now_tick < self.last_tick:
            # 回退游标但**不发放**：等价于「这段时间不存在」。
            self.last_tick = int(now_tick)
            self.numerator = 0
            return self.balance_micro
        total = self.numerator + self._accrual(now_tick, allow_lazy)
        credit = total // int(frames_per_second)
        self.numerator = total - credit * int(frames_per_second)
        self.last_tick = int(now_tick)
        # 超出桶容量的部分直接丢弃：这是「有限桶」的含义。
        self.balance_micro = min(self.capacity_micro, self.balance_micro + credit)
        return self.balance_micro

    def take(self, amount_micro, now_tick, frames_per_second, allow_lazy=True):
        # type: (int, int, int, bool) -> int
        """取走额度。取不到就返回 0 且**不改余额**。"""
        _require_count(amount_micro, "bucket take")
        self.refill(now_tick, frames_per_second, allow_lazy)
        if amount_micro > self.balance_micro:
            self.denied += 1
            return 0
        self.balance_micro -= amount_micro
        self.granted_micro += amount_micro
        return amount_micro

    def to_dict(self):
        # type: () -> dict
        return {
            "capacity_micro": self.capacity_micro,
            "rate_micro_per_second": self.rate_micro_per_second,
            "balance_micro": self.balance_micro,
            "numerator": self.numerator,
            "last_tick": self.last_tick,
        }

    def load_state(self, data):
        # type: (dict) -> None
        """恢复余额与余数。**桶本身由规格决定，不从存档重建**——
        否则改一次定义表就能让老存档带着旧速率跑。
        """
        if not isinstance(data, dict):
            return
        balance = data.get("balance_micro", 0)
        if isinstance(balance, _INT_TYPES) and not isinstance(balance, bool):
            self.balance_micro = max(0, min(int(balance), self.capacity_micro))
        numerator = data.get("numerator", 0)
        if isinstance(numerator, _INT_TYPES) and not isinstance(numerator, bool):
            self.numerator = max(0, int(numerator))

    def __repr__(self):
        return "TokenBucket(%d/%d)" % (self.balance_micro, self.capacity_micro)


class MediumPlan(object):
    """一次介质内转移的**预演结果**。

    `plan()` 产出它，`commit()` 消费它。它带着做判定时的全部依据，
    因此 commit 不需要（也不应该）重新算一遍——重算会在两次计算之间
    引入一个「状态已经变了」的窗口。
    """

    __slots__ = (
        "ok",
        "reason",
        "detail",
        "key",
        "direction",
        "requested",
        "accepted",
        "granted_micro",
        "recovery",
    )

    def __init__(
        self,
        ok,
        reason="",
        key=None,
        direction="",
        requested=0,
        accepted=0,
        granted_micro=0,
        recovery=MediumRecovery.OK,
        detail="",
    ):
        # type: (bool, str, object, str, int, int, int, str, str) -> None
        self.ok = ok
        #: 协议原因码（`common.protocol.Reason`）；成功时为空串。
        self.reason = reason
        #: 给人看的补充说明。
        self.detail = detail
        self.key = key
        self.direction = direction
        self.requested = requested
        self.accepted = accepted
        self.granted_micro = granted_micro
        self.recovery = recovery

    def __repr__(self):
        return "MediumPlan(%s, %s, %d/%d)" % (
            self.direction,
            self.reason or "ok",
            self.accepted,
            self.requested,
        )


class Medium(object):
    """单个介质的权威状态。

    生命周期由持有者（驱动器状态集合）决定，本对象不感知网络与位置。
    """

    __slots__ = (
        "medium_id",
        "model_id",
        "spec",
        "revision",
        "recovery",
        "orphaned",
        "raw_payload",
        "notes",
        "entries",
        "used_micro",
        "type_count",
        "read_bucket",
        "write_bucket",
        "policy",
    )

    def __init__(self, medium_id, model_id, spec, policy, now_tick=0, entries=None):
        # type: (str, str, MediumSpec, MediumPolicy, int, dict) -> None
        if not medium_id:
            raise ValueError("介质必须有稳定 ID")
        self.medium_id = medium_id
        self.model_id = model_id
        self.spec = spec
        self.policy = policy
        self.revision = 0
        #: 见 `MediumRecovery`。非 OK 时禁止交易，但内容原样保留。
        self.recovery = MediumRecovery.OK
        #: 驱动器被拆掉、介质失去归属。本轮不自行决定掉落或保管方式。
        self.orphaned = False
        #: 无法理解时的原始载荷。**永不丢弃**，任务 12 的原样保留靠它。
        self.raw_payload = None
        self.notes = ""

        self.entries = {}
        self.used_micro = 0
        self.type_count = 0

        self.read_bucket = None
        self.write_bucket = None
        if spec is not None:
            self.read_bucket, self.write_bucket = self._make_buckets(now_tick)

        if entries:
            for key, count in entries.items():
                self._put(key, count)

    # -- 构造 ----------------------------------------------------------

    def capacity_micro(self):
        # type: () -> int
        if self.spec is None:
            return 0
        return self.spec.capacity_units * MICRO_UNITS_PER_UNIT

    def _make_buckets(self, now_tick):
        # type: (int) -> tuple
        """按策略建桶。

        `shares_read_write_budget` 为真时读与写**共用同一个桶**（容量与速率都取
        两者较大者）——这是 R07 的替身分支，两种取法都合理。共用时两个字段指向
        同一个对象，`refresh_budget` 重复推进它是幂等的。
        """
        read_rate = self.spec.read_units_per_second * MICRO_UNITS_PER_UNIT
        write_rate = self.spec.write_units_per_second * MICRO_UNITS_PER_UNIT
        if self.policy.shares_read_write_budget:
            shared = TokenBucket(
                max(read_rate, write_rate) * self.policy.bucket_seconds,
                max(read_rate, write_rate),
                now_tick=now_tick,
            )
            return shared, shared
        return (
            TokenBucket(
                read_rate * self.policy.bucket_seconds, read_rate, now_tick=now_tick
            ),
            TokenBucket(
                write_rate * self.policy.bucket_seconds, write_rate, now_tick=now_tick
            ),
        )

    @classmethod
    def create(cls, medium_id, model_id, definitions, policy, now_tick=0):
        # type: (str, str, object, MediumPolicy, int) -> Medium
        """按型号新建一块盘。型号未注册时**明确报错**，不建一块无规格的盘。"""
        return cls(
            medium_id, model_id, definitions.get_medium(model_id), policy, now_tick
        )

    # -- 恢复状态 ------------------------------------------------------

    def is_tradeable(self):
        # type: () -> bool
        """能否参与交易。恢复未完成或已失去归属时为 False。"""
        return self.recovery == MediumRecovery.OK and not self.orphaned

    def recovery_reason(self):
        # type: () -> str
        """交易被拒时的协议原因码。"""
        if self.orphaned:
            return Reason.MEDIUM_ORPHANED
        return Reason.MEDIUM_UNRECOVERED

    def recovery_detail(self):
        # type: () -> str
        if self.orphaned:
            return "介质已失去归属（驱动器被拆除）"
        return RECOVERY_DETAILS.get(self.recovery, "介质不可交易")

    def mark_orphaned(self, note=""):
        # type: (str) -> None
        """失去归属（驱动器被拆）。**内容不动**。"""
        self.orphaned = True
        if note:
            self.notes = note

    # -- 容量与类型 ----------------------------------------------------

    def free_micro(self):
        # type: () -> int
        return max(0, self.capacity_micro() - self.used_micro)

    def max_types(self):
        # type: () -> int
        return 0 if self.spec is None else self.spec.max_types

    def _counts_as_type(self, key):
        # type: (ResourceKey) -> bool
        return key.is_item() or self.policy.counts_fluid_as_type

    # -- 视图（不泄露内部字典）------------------------------------------

    def count_of(self, key):
        # type: (ResourceKey) -> int
        return self.entries.get(key, 0)

    def keys(self):
        # type: () -> tuple
        """稳定顺序的条目键。调用方遍历顺序不随 set 哈希变化。"""
        return tuple(sorted(self.entries.keys(), key=lambda item: item.canonical()))

    def stored_view(self):
        # type: () -> dict
        """全部条目的副本。"""
        return dict(self.entries)

    def mount_view(self):
        # type: () -> dict
        """挂载信息用的只读快照。**纯数据**，不含任何内部对象引用。"""
        return {
            "medium_id": self.medium_id,
            "model_id": self.model_id,
            "label": self.spec.label if self.spec is not None else self.model_id,
            "capacity_micro": self.capacity_micro(),
            "used_micro": self.used_micro,
            "free_micro": self.free_micro(),
            "types": self.type_count,
            "max_types": self.max_types(),
            "entries": len(self.entries),
            "revision": self.revision,
            "recovery": self.recovery,
            "tradeable": self.is_tradeable(),
            "orphaned": self.orphaned,
        }

    # -- 预算推进 ------------------------------------------------------

    def refresh_budget(self, now_tick):
        # type: (int) -> None
        """把桶推进到 `now_tick`。只影响额度，不影响库存。"""
        for bucket in (self.read_bucket, self.write_bucket):
            if bucket is not None:
                bucket.refill(
                    now_tick,
                    self.policy.frames_per_second,
                    self.policy.recharge_while_offline,
                )

    def _bucket_for(self, direction):
        # type: (str) -> TokenBucket
        if direction == DIRECTION_READ:
            return self.read_bucket
        if direction == DIRECTION_WRITE:
            return self.write_bucket
        raise ValueError("未知转移方向: %r" % (direction,))

    # -- 预演 / 提交 ---------------------------------------------------

    def plan(self, key, count, direction, now_tick):
        # type: (ResourceKey, int, str, int) -> MediumPlan
        """预演一次转移。**纯函数**：不改库存、不改计数器、不动额度、不动 revision。

        判定顺序固定为 **类型配额 → 容量 → 额度**。任一步不足都返回一个
        带具体原因的失败计划；部分可行时 `accepted` 是实际能动的数量。
        """
        _require_count(count, "transfer count")
        if direction not in DIRECTIONS:
            raise ValueError("未知转移方向: %r" % (direction,))

        if not self.is_tradeable():
            return MediumPlan(
                False,
                self.recovery_reason(),
                key,
                direction,
                count,
                0,
                0,
                self.recovery,
                self.recovery_detail(),
            )

        if direction == DIRECTION_WRITE:
            return self._plan_insert(key, count, now_tick)
        return self._plan_extract(key, count, now_tick)

    def _unit_micro(self, key):
        # type: (ResourceKey) -> int
        """一个该资源占多少微单位物。物品 50，流体 1。"""
        return key.micro_units(1)

    def _plan_insert(self, key, count, now_tick):
        # type: (ResourceKey, int, int) -> MediumPlan
        unit = self._unit_micro(key)
        accepted = count

        # 1 类型配额。同类型追加不占新配额；取空之后再加回来要重新占。
        if self.entries.get(key, 0) <= 0 and self._counts_as_type(key):
            if self.type_count >= self.max_types():
                return MediumPlan(
                    False, Reason.TYPE_LIMIT_REACHED, key, DIRECTION_WRITE, count, 0, 0
                )

        # 2 容量。先按整数件算能放多少，避免出现「放进去 0.4 件」。
        capacity_items = self.free_micro() // unit
        if capacity_items <= 0:
            return MediumPlan(
                False, Reason.NO_CAPACITY, key, DIRECTION_WRITE, count, 0, 0
            )
        accepted = min(accepted, capacity_items)

        # 3 额度。同一个桶由所有调用者共享。
        quota_items = self._available_items(DIRECTION_WRITE, unit, now_tick)
        if quota_items <= 0:
            return MediumPlan(
                False, Reason.RATE_LIMITED, key, DIRECTION_WRITE, count, 0, 0
            )
        accepted = min(accepted, quota_items)

        return MediumPlan(
            True, "", key, DIRECTION_WRITE, count, accepted, accepted * unit
        )

    def _plan_extract(self, key, count, now_tick):
        # type: (ResourceKey, int, int) -> MediumPlan
        unit = self._unit_micro(key)
        have = self.entries.get(key, 0)
        if have <= 0:
            return MediumPlan(
                False, Reason.INSUFFICIENT_STOCK, key, DIRECTION_READ, count, 0, 0
            )
        accepted = min(count, have)

        quota_items = self._available_items(DIRECTION_READ, unit, now_tick)
        if quota_items <= 0:
            return MediumPlan(
                False, Reason.RATE_LIMITED, key, DIRECTION_READ, count, 0, 0
            )
        accepted = min(accepted, quota_items)

        return MediumPlan(
            True, "", key, DIRECTION_READ, count, accepted, accepted * unit
        )

    def _available_items(self, direction, unit, now_tick):
        # type: (str, int, int) -> int
        """该方向当前额度能换几件该资源。"""
        bucket = self._bucket_for(direction)
        if bucket is None:
            return 0
        credit = bucket.peek(
            now_tick,
            self.policy.frames_per_second,
            self.policy.recharge_while_offline,
        )
        return credit // unit

    def commit(self, plan, now_tick):
        # type: (MediumPlan, int) -> int
        """落账。返回实际移动的数量。

        **只接受本对象产出的计划**，且不重新判定：单帧内没有插入点，
        因此 `take()` 的实得量必然够。万一不够（时钟被外部拨动等），
        返回 0 并**保持介质不变**——宁可拒绝，也不留一次单边更新。
        """
        if not plan.ok or plan.accepted <= 0:
            return 0
        if not self.is_tradeable():
            # 计划做出之后、提交之前介质被判为不可交易（例如刚失去归属）。
            return 0

        bucket = self._bucket_for(plan.direction)
        taken = bucket.take(
            plan.granted_micro,
            now_tick,
            self.policy.frames_per_second,
            self.policy.recharge_while_offline,
        )
        if taken < plan.granted_micro:
            return 0

        if plan.direction == DIRECTION_WRITE:
            self._put(plan.key, plan.accepted)
        else:
            self._remove(plan.key, plan.accepted)
        self.revision += 1
        return plan.accepted

    # -- 内部记账 ------------------------------------------------------

    def _put(self, key, count):
        # type: (ResourceKey, int) -> None
        """写入条目并维护两个缓存计数器。**唯一**改 `entries` 的入口之一。"""
        if count <= 0:
            return
        before = self.entries.get(key, 0)
        if before == 0 and self._counts_as_type(key):
            self.type_count += 1
        self.entries[key] = before + count
        self.used_micro += key.micro_units(count)

    def _remove(self, key, count):
        # type: (ResourceKey, int) -> None
        """扣减条目。**归零即删键并释放类型占用**（05-02）。"""
        before = self.entries.get(key, 0)
        if before <= 0:
            return
        take = min(before, count)
        after = before - take
        self.used_micro -= key.micro_units(take)
        if after == 0:
            del self.entries[key]
            if self._counts_as_type(key):
                self.type_count -= 1
        else:
            self.entries[key] = after

    # -- 不变式自检 ----------------------------------------------------

    def recompute_usage(self):
        # type: () -> tuple
        """**独立**全量求和，返回 `(used_micro, type_count)`。

        刻意不复用 `_put` / `_remove` 的增量逻辑：那样只是把同一套算法抄一遍，
        抄错了一起错。这里是测试与审计的独立校验依据。
        """
        used = 0
        types = 0
        for key, count in self.entries.items():
            if count <= 0:
                continue
            used += key.micro_units(count)
            if self._counts_as_type(key):
                types += 1
        return used, types

    def assert_consistent(self):
        # type: () -> None
        """不变式自检。开发期与测试用，生产 tick 不调用。"""
        for key, count in self.entries.items():
            if count <= 0:
                raise AssertionError(
                    "介质 %s 留有非正数条目 %s x%d"
                    % (self.medium_id, key.canonical(), count)
                )
        expected_used, expected_types = self.recompute_usage()
        if self.used_micro != expected_used:
            raise AssertionError(
                "介质 %s 的用量缓存 %d 与独立求和 %d 不一致"
                % (self.medium_id, self.used_micro, expected_used)
            )
        if self.type_count != expected_types:
            raise AssertionError(
                "介质 %s 的类型数缓存 %d 与独立求和 %d 不一致"
                % (self.medium_id, self.type_count, expected_types)
            )
        if self.used_micro > self.capacity_micro():
            raise AssertionError(
                "介质 %s 用量 %d 超过容量 %d"
                % (self.medium_id, self.used_micro, self.capacity_micro())
            )
        for bucket in (self.read_bucket, self.write_bucket):
            if bucket is None:
                continue
            if bucket.balance_micro < 0 or bucket.balance_micro > bucket.capacity_micro:
                raise AssertionError(
                    "介质 %s 的额度余额 %d 越界（0..%d）"
                    % (self.medium_id, bucket.balance_micro, bucket.capacity_micro)
                )

    # -- 序列化 --------------------------------------------------------

    def to_dict(self):
        # type: () -> dict
        payload = {
            "schema_version": MEDIUM_SCHEMA_VERSION,
            "medium_id": self.medium_id,
            "model_id": self.model_id,
            "revision": self.revision,
            "orphaned": self.orphaned,
            "entries": [
                {"key": key.to_dict(), "count": self.entries[key]}
                for key in self.keys()
            ],
            "read_bucket": self.read_bucket.to_dict() if self.read_bucket else None,
            "write_bucket": self.write_bucket.to_dict() if self.write_bucket else None,
        }
        if self.notes:
            payload["notes"] = self.notes
        return payload

    @classmethod
    def from_dict(cls, data, definitions, policy, now_tick=0):
        # type: (dict, object, MediumPolicy, int) -> Medium
        """从存档还原。

        **三种「读不懂」都不得当空盘处理**（05-04）：版本不认识、型号缺失、
        载荷损坏，一律返回一个带对应 `recovery` 标记的介质对象，
        `raw_payload` 保留原样，交易被拒。

        当前只有 v1，因此「版本不等于 1」就是读不懂。任务 12 接真实后端时
        在这里加迁移分支，迁移失败才落到 `UNKNOWN_SCHEMA`。
        """
        if not isinstance(data, dict):
            return cls._unreadable("", "", None, policy, data, MediumRecovery.CORRUPT)

        medium_id = data.get("medium_id", "")
        model_id = data.get("model_id", "")

        if not medium_id or not model_id:
            return cls._unreadable(
                medium_id, model_id, None, policy, data, MediumRecovery.CORRUPT
            )

        version = data.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, _INT_TYPES):
            return cls._unreadable(
                medium_id, model_id, None, policy, data, MediumRecovery.UNKNOWN_SCHEMA
            )
        if int(version) != MEDIUM_SCHEMA_VERSION:
            return cls._unreadable(
                medium_id, model_id, None, policy, data, MediumRecovery.UNKNOWN_SCHEMA
            )

        try:
            spec = definitions.get_medium(model_id)
        except KeyError:
            return cls._unreadable(
                medium_id, model_id, None, policy, data, MediumRecovery.MISSING_MODEL
            )

        try:
            entries = {}
            for item in data.get("entries", ()):
                key = ResourceKey.from_dict(item["key"])
                count = item["count"]
                if isinstance(count, bool) or not isinstance(count, _INT_TYPES):
                    raise TypeError("条目数量必须是整数")
                if count > 0:
                    entries[key] = entries.get(key, 0) + int(count)
        except (KeyError, TypeError, ValueError, AttributeError):
            return cls._unreadable(
                medium_id, model_id, spec, policy, data, MediumRecovery.CORRUPT
            )

        medium = cls(
            medium_id, model_id, spec, policy, now_tick=now_tick, entries=entries
        )
        medium.revision = int(data.get("revision", 0))
        medium.orphaned = bool(data.get("orphaned", False))
        medium.notes = data.get("notes", "")
        # 桶的容量与速率由**规格**决定，只从存档恢复余额与余数。
        # 连速率一起恢复的话，改一次定义表就会让老存档带着旧速率继续跑。
        if medium.read_bucket is not None:
            medium.read_bucket.load_state(data.get("read_bucket"))
        if medium.write_bucket is not None:
            medium.write_bucket.load_state(data.get("write_bucket"))

        try:
            medium.assert_consistent()
        except AssertionError:
            # 缓存与内容对不上：宁可判为损坏（保留载荷、禁交易），
            # 也不要把两个互相矛盾的读数放出去。
            return cls._unreadable(
                medium_id, model_id, spec, policy, data, MediumRecovery.CORRUPT
            )
        return medium

    @classmethod
    def _unreadable(cls, medium_id, model_id, spec, policy, raw, recovery):
        # type: (str, str, MediumSpec, MediumPolicy, object, str) -> Medium
        """造一个「保留载荷但不可交易」的介质。

        `medium_id` 可能为空（载荷里就没写），那时给一个明显的占位 ID——
        它不会被写回存档，因为 `read_only` 语义要求原样保留 `raw_payload`。
        """
        medium = cls(medium_id or "unreadable", model_id or "unknown", spec, policy)
        medium.recovery = recovery
        medium.raw_payload = raw
        return medium

    def __repr__(self):
        return "Medium(%s, %s, %d/%d, %d 类型)" % (
            self.medium_id,
            self.model_id,
            self.used_micro,
            self.capacity_micro(),
            self.type_count,
        )
