# coding=utf-8
"""网络与外部容器之间的资源转移：端点、预检、受控提交与中转缓冲。

对应任务 06-01 至 06-03，以及 06-09 的转移半边。端点契约与回执字段与
13-01 的统一容器契约（`adapters/containers.py` / `common/protocol.py`）共用；
**本模块不 import 任何引擎模块**，因此可以脱离游戏测试。

参考 AE2 的 `IStorageMonitorableAccessor`——外部拿到的不是「某个控制器」，
而是一个能受理请求、并按实际数量回答的口子。

## 四条纪律（写在代码之前，因为每一条都有对应的失败形态）

1. **只按实际数量记账。** `TransferReceipt.moved` 是唯一可信的完成量。
   容器说放进去多少不算数：13-01 的 `GhostContainer` 就是
   `PutItemIntoContainer` 那个「声称全部放入、实际没有」的缺陷形态，
   契约层的读回核对专门为它存在。

2. **不确定不重放。** `UNCERTAIN` 是给恢复流程的输入，不是重试许可。
   重发同一批可能让外部机器收到两份原料，而账目上只多了一份。

3. **先入后出，不做「先删源再假定目标接收」。** 跨两个互不知情的存档系统
   本来就没有事务；中转缓冲是唯一能回答「东西现在在哪」的地方。
   `TransferReceipt.holder` 就是那个回答。

4. **外部进入与内部搬运分开。** 只有 `ORIGIN_EXTERNAL` 且真的进入网络的那一份
   才发 `ExternalInput`。把内部搬运也记成输入，会让同一份资源被反复计入
   订单产物（全局约束 3）。

## 端点为什么报 `holder_id`

06 验收要求「失败时可确认剩余资源的位置」。每次跨边界移动之后，没搬完的部分
必须能回答「现在在谁手上」——`plans/architecture_analysis.md` §6.3 第 4 条。
只报「成功了多少」而不报「剩下的在哪」，正是「先删源再假定目标接收」的温床。
"""

from ...common.item_codec import item_dict_from_key, item_key_from_dict
from ...common.protocol import (
    BlockReason,
    ORIGIN_EXTERNAL,
    ORIGIN_INTERNAL,
    Reason,
    TransferReceipt,
    TransferRequest,  # noqa: F401 (仅用于 # type: 注释)
    TransferStatus,
    reason_code,
)
from ...common.resource_key import ResourceKey  # noqa: F401 (仅用于 # type: 注释)
from ..network.controller import RequestLedger

#: 缓冲归属台账的存档结构版本。任务 12 用它判断能否读入。
TRANSFER_SCHEMA_VERSION = 1

#: `commit()` 的子操作后缀。**它们是稳定的**：重发整个操作时子操作必须落到
#: 同一把账本键上，否则「重试不会搬第二次」这条不成立。
_SUB_IN = ":in"
_SUB_OUT = ":out"
_SUB_UNDO = ":undo"

#: 光标持有物的处置口径（R12）。
#: `REFUSE` = 遇到客户端声称手里拿着东西就拒绝，**不接管也不猜**。
CURSOR_REFUSE = "refuse"
#: 明确表示「本模组不实现光标持有物」，此时带 cursor 的请求按普通命令处理。
CURSOR_IGNORE = "ignore"


class TransferPolicy(object):
    """转移的未定案旋钮（R05 / R12）。

    做成具名策略而不是常数，理由与 `MediumPolicy` / `DriverPolicy` 一致：
    这几条都还没定案（总览全局约束 7），写死一个值等于用临时实现把玩法定下来。

    `assert_settled()` **只应在正式验收时调用**——开发与离线测试不应调用它。
    """

    __slots__ = ("cursor_disposition", "reconcile_limit")

    def __init__(self, cursor_disposition=CURSOR_REFUSE, reconcile_limit=256):
        # type: (str, int) -> None
        #: R12：玩家界面关闭/断线时手里那件东西怎么办。**尚未定案**，
        #: 替身取「拒绝接管」——不猜一个行为，遇到就明确回 `r12_cursor_held`。
        self.cursor_disposition = cursor_disposition
        #: R05 接缝：待核对台账最多保留多少条。超出按最旧淘汰并计数，
        #: **不静默丢弃**（淘汰数在诊断里）。
        self.reconcile_limit = int(reconcile_limit)

        if self.cursor_disposition not in (CURSOR_REFUSE, CURSOR_IGNORE):
            raise ValueError("未知光标处置口径: %r" % (self.cursor_disposition,))
        if self.reconcile_limit <= 0:
            raise ValueError(
                "reconcile_limit 必须为正，得到 %r" % (self.reconcile_limit,)
            )

    def unresolved_report(self):
        # type: () -> list
        """仍带未定标签的字段。与 `MediumPolicy` / `DriverPolicy` 同形。"""
        return [
            {
                "field": "transfer.cursor_disposition",
                "rule": "R12",
                "value": self.cursor_disposition,
                "note": "光标持有物的退出/断线处置；替身取拒绝接管",
            },
            {
                "field": "transfer.reconcile_limit",
                "rule": "R05",
                "value": self.reconcile_limit,
                "note": "待核对台账条数上限；替身取 256",
            },
        ]

    def assert_settled(self):
        # type: () -> None
        """正式验收用：仍有未定项就报错。开发与离线测试不应调用它。"""
        pending = self.unresolved_report()
        if pending:
            raise AssertionError(
                "转移策略仍有 %d 项未定案，不能作为正式玩法交付：\n  " % (len(pending),)
                + "\n  ".join(
                    "%s（规则 %s，当前值 %r）"
                    % (item["field"], item["rule"], item["value"])
                    for item in pending
                )
            )

    def describe(self):
        # type: () -> dict
        return {
            "cursor_disposition": self.cursor_disposition,
            "reconcile_limit": self.reconcile_limit,
        }


# -- 端点 ------------------------------------------------------------------


class TransferEndpoint(object):
    """转移的一端。

    鸭子类型契约（与 `devices/capabilities.py` 同一风格，不用 ABC、不做
    isinstance 校验）：协调器只认下面这些方法，端点不需要 import 协调器。

    子类实现 `can_read` / `can_write` / `probe_*` / `commit_*`，
    基类给出保守默认——**默认答案是「不知道」，不是「可以」**。
    """

    #: 诊断用的端点种类
    kind = "endpoint"

    def endpoint_id(self):
        # type: () -> str
        """稳定身份，进回执、进诊断、进归属台账。"""
        raise NotImplementedError

    def holder_id(self):
        # type: () -> str
        """这批资源此刻归谁。缺失时返回空串（「不知道」，不是「没有主」）。"""
        return ""

    def holder_of(self, resource_key):
        # type: (ResourceKey) -> str
        """**指定资源**此刻归谁。默认与 `holder_id()` 同义。

        只有缓冲端点需要区分——它的箱子里可能同时躺着几批分属不同人的东西。
        基类给出这个默认实现，是为了让协调器可以一律调 `holder_of(key)`
        而不必先问「你是不是缓冲」。
        """
        return self.holder_id()

    def is_valid(self):
        # type: () -> bool
        """邻接容器被拆、维度变化、依赖关闭之后为假（13-05）。"""
        return True

    def invalidate(self):
        # type: () -> None
        """作废这一端。基类不动任何东西——**没有东西可作废的实现不该假装有**。

        `ContainerEndpoint` 覆盖它并把作废传给它包着的适配器。
        """
        return None

    def can_read(self):
        # type: () -> bool
        raise NotImplementedError

    def can_write(self):
        # type: () -> bool
        raise NotImplementedError

    def probe_supply(self, resource_key, count):
        # type: (ResourceKey, int) -> object
        """还能取出多少。`None` = 不知道（调用方按「全都要」尝试并核对）。"""
        return None

    def probe_space(self, resource_key, count):
        # type: (ResourceKey, int) -> object
        """还能放进多少。`None` = 不知道（同上）。"""
        return None

    def commit_extract(self, op_id, resource_key, count, owner_id=""):
        # type: (str, ResourceKey, int, str) -> TransferReceipt
        raise NotImplementedError

    def commit_insert(self, op_id, resource_key, count, owner_id="", origin=None):
        # type: (str, ResourceKey, int, str, str) -> TransferReceipt
        raise NotImplementedError

    def last_extract_origins(self):
        # type: () -> tuple
        """上一次取出**实际动了哪些来源**，返回 `((origin, count), ...)`。

        只有中转缓冲需要实现它：一份缓冲里可能同时躺着外部送进来的和本模组
        搬过来的，而两者的回流语义不同。直接对着容器或网络的端点没有这个
        问题（它们的 `origin` 由请求本身给出），返回空元组即可——
        空元组表示「按请求的来源标识算」，不是「没有来源」。
        """
        return ()

    def describe(self):
        # type: () -> dict
        return {
            "kind": self.kind,
            "endpoint_id": self.endpoint_id(),
            "holder_id": self.holder_id(),
            "valid": self.is_valid(),
            "can_read": self.can_read(),
            "can_write": self.can_write(),
        }

    def __repr__(self):
        return "%s(%s)" % (type(self).__name__, self.endpoint_id())


def _probe_answer(plan):
    # type: (dict) -> object
    """把存储服务的预演结果翻成 `probe_supply` / `probe_space` 的答案。

    **「额度用尽」必须回 `None`（不知道），不能回 0。** 协调器把 0 读成
    「来源端点没有可动的」并报 `insufficient_stock` —— 而玩家看到的是
    「网络里没有这件东西」，真相却是 `rate_limited`（这一秒的读写额度用完了，
    下一帧就有）。方向指错，界面就会把人往「去别处找货源」上引。

    回 `None` 之后协调器会照常发起转移，由 `commit_*` 给出真实的
    `rate_limited` 回执——这正是 `probe_*` 的既有契约：「`None` = 不知道，
    调用方按请求量去试，由 `commit` 的核对给实际值」。

    2026-10-01 真机实测到的那条：一次「取一组」正好抽干该秒的读取额度
    （磁带 `read_units_per_second` 是 64，桶容量 1 秒 = 64 件），同一秒内
    第二条命令必然失败——而它报的是 `insufficient_stock`。连点 4 行，
    第 1 行成功、后 3 行都是这条错码。

    **只有 `RATE_LIMITED` 这一种原因改判。** 真没货（`INSUFFICIENT_STOCK`）
    与真放不下（`NO_CAPACITY`）照旧回 0：那些答案是对的，回 `None` 只会让
    协调器白跑一趟再去核对一遍。
    """
    if plan.get("accepted", 0) > 0:
        return plan["accepted"]
    if plan.get("reason") == Reason.RATE_LIMITED:
        return None
    return 0


class NetworkEndpoint(TransferEndpoint):
    """网络这一端。包一个 `StorageService`。

    `probe_*` 走服务自己的 `simulate_*`——**额度与容量的判定归存储服务**，
    协调器不重算一遍。两处各算一次迟早在「跨盘凑额度」这类边界上分叉。
    """

    kind = "network"

    def __init__(self, storage, network_id=""):
        # type: (object, str) -> None
        self.storage = storage
        self.network_id = network_id

    def endpoint_id(self):
        # type: () -> str
        return "network:%s" % (self.network_id or "?",)

    def holder_id(self):
        # type: () -> str
        return self.endpoint_id()

    def is_valid(self):
        # type: () -> bool
        return self.storage is not None and self.storage.is_ready()

    def can_read(self):
        # type: () -> bool
        return self.is_valid()

    def can_write(self):
        # type: () -> bool
        return self.is_valid()

    def probe_supply(self, resource_key, count):
        # type: (ResourceKey, int) -> object
        if not self.can_read():
            return None
        plan = self.storage.simulate_extract(resource_key, count)
        return _probe_answer(plan)

    def probe_space(self, resource_key, count):
        # type: (ResourceKey, int) -> object
        if not self.can_write():
            return None
        plan = self.storage.simulate_insert(resource_key, count)
        return _probe_answer(plan)

    def commit_extract(self, op_id, resource_key, count, owner_id=""):
        # type: (str, ResourceKey, int, str) -> TransferReceipt
        if not self.can_read():
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED,
                Reason.SERVICE_NOT_READY, resource_key,
            )
        return self.storage.commit_extract(op_id, resource_key, count, owner_id)

    def commit_insert(self, op_id, resource_key, count, owner_id="", origin=None):
        # type: (str, ResourceKey, int, str, str) -> TransferReceipt
        if not self.can_write():
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED,
                Reason.SERVICE_NOT_READY, resource_key,
            )
        return self.storage.commit_insert(op_id, resource_key, count, owner_id)


class ContainerEndpoint(TransferEndpoint):
    """外部容器这一端。包一个 13-01 的 `ContainerAdapter`。

    **不复制核对逻辑**：`ContainerAdapter.insert` / `extract` 已经做了读回核对，
    这里只把它的能力映射成端点的三个问题（能不能读、能不能写、还能进出多少）。
    """

    kind = "container"

    def __init__(self, adapter, endpoint_id=""):
        # type: (object, str) -> None
        self.adapter = adapter
        self._endpoint_id = endpoint_id

    def endpoint_id(self):
        # type: () -> str
        if self._endpoint_id:
            return self._endpoint_id
        return "container:%s" % (getattr(self.adapter, "adapter_id", "?"),)

    def holder_id(self):
        # type: () -> str
        return self.endpoint_id()

    def is_valid(self):
        # type: () -> bool
        if self.adapter is None:
            return False
        checker = getattr(self.adapter, "is_valid", None)
        return True if checker is None else bool(checker())

    def invalidate(self):
        # type: () -> None
        """作废这一端，并把它传给包着的适配器（13-05 的失效入口）。"""
        if self.adapter is not None:
            self.adapter.invalidate()

    def can_read(self):
        # type: () -> bool
        if not self.is_valid():
            return False
        return bool(self.adapter.capabilities().can_read)

    def can_write(self):
        # type: () -> bool
        if not self.is_valid():
            return False
        return bool(self.adapter.capabilities().can_insert)

    def probe_supply(self, resource_key, count):
        # type: (ResourceKey, int) -> object
        if not self.can_read():
            return None
        total = self.adapter.count_of(resource_key)
        if total is None:
            return None
        return min(count, total)

    def probe_space(self, resource_key, count):
        # type: (ResourceKey, int) -> object
        if not self.can_write():
            return None
        return self.adapter.can_accept(resource_key, count)

    def commit_extract(self, op_id, resource_key, count, owner_id=""):
        # type: (str, ResourceKey, int, str) -> TransferReceipt
        if not self.is_valid():
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED,
                Reason.ENDPOINT_INVALID, resource_key,
            )
        return self.adapter.extract(op_id, resource_key, count)

    def commit_insert(self, op_id, resource_key, count, owner_id="", origin=None):
        # type: (str, ResourceKey, int, str, str) -> TransferReceipt
        if not self.is_valid():
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED,
                Reason.ENDPOINT_INVALID, resource_key,
            )
        return self.adapter.insert(op_id, resource_key, count)


class SlotAnchor(object):
    """背包槽的定位锚：槽号 + 期望指纹 + 期望槽版本。

    06 文档明写「使用玩家、槽号、槽版本和物品指纹定位，**不能以客户端显示索引
    代替**」，06 验收也明写「背包槽在点击后变更时不会误取替换后的物品」。
    三样缺一不可：

    - 只看槽号：槽里换了东西照样照单全收；
    - 只看指纹：玩家背包里另一种槽位也有同一件物品时，会从别处扣；
    - 加槽版本：客户端基于旧快照发来的请求能被明确拒绝（`stale_version`），
      而不是取错东西。

    `expected_version` 允许为 `None`，表示「不校验版本」——那是有意的选项，
    不是省略；旧调用方可显式不校验。终端按槽存入使用服务端同步的真实版本，
    没有对应快照时不发送，不再通过 None 绕过。
    """

    __slots__ = ("slot", "expected_key", "expected_version")

    def __init__(self, slot, expected_key, expected_version=None):
        # type: (int, object, object) -> None
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
            raise ValueError("槽号必须是非负整数，得到 %r" % (slot,))
        self.slot = slot
        self.expected_key = expected_key
        self.expected_version = expected_version

    def to_dict(self):
        # type: () -> dict
        return {
            "slot": self.slot,
            "expected_key": (
                self.expected_key.to_dict() if self.expected_key is not None else None
            ),
            "expected_version": self.expected_version,
        }

    def __repr__(self):
        return "SlotAnchor(槽%d, v%r)" % (self.slot, self.expected_version)


class InventoryEndpoint(ContainerEndpoint):
    """玩家背包这一端。在容器契约之上加**按槽定位**与槽版本。

    槽版本由本对象自己维护：每次 `slot_view()` 读到与上次不同的指纹就把版本加一。
    它因此是「服务端观察到的修订号」，不是引擎给的权威版本号——这一点写在
    `slot_view()` 的说明里，界面上说的「槽版本」指的是它。
    """

    kind = "inventory"

    def __init__(self, adapter, player_id, endpoint_id=""):
        # type: (object, str, str) -> None
        ContainerEndpoint.__init__(self, adapter, endpoint_id)
        self.player_id = player_id
        #: `slot -> [last_key_canonical, version]`
        self._versions = {}
        self._revision = 0

    def endpoint_id(self):
        # type: () -> str
        if self._endpoint_id:
            return self._endpoint_id
        return "player:%s" % (self.player_id,)

    def holder_id(self):
        # type: () -> str
        return self.endpoint_id()

    # -- 按槽读取 ------------------------------------------------------

    def _entry_at(self, slot):
        # type: (int) -> object
        """该槽当前的条目；空槽返回 None。

        容器已失效时 `entries()` 会抛 `IOError`（13-01 的
        `InvalidatableContainer` 就是这样），这里**不吞异常**——调用方
        先看 `can_read()`，读不到就是「读不到」，不该伪装成空槽。
        """
        for index, entry in self.adapter.entries():
            if index == slot:
                return entry
        return None

    def slot_view(self, slot):
        # type: (int) -> dict
        """一个槽的只读视图。**这是槽版本的唯一来源。**

        版本按「观察到指纹变化」递增：`last is None` 表示第一次看到这个槽，
        记为 1。它不能替代引擎的权威修订号（引擎没有提供），因此界面上
        必须把它当「本模组会话内的修订号」用——同一份快照里比较是有效的，
        跨会话比较没有意义。
        """
        entry = self._entry_at(slot)
        return self._observe_slot(slot, entry)

    def inventory_view(self):
        # type: () -> dict
        """一次读取整个背包，供同步与命令共用同一份版本台账。"""
        entries = dict(self.adapter.entries())
        slots = [self._observe_slot(slot, entries.get(slot))
                 for slot in range(self.adapter.slot_count())]
        return {"revision": self._revision, "slots": slots}

    def _observe_slot(self, slot, entry):
        # type: (int, object) -> dict
        key = entry.resource_key if entry is not None else None
        canonical = key.canonical() if key is not None else ""
        record = self._versions.get(slot)
        if record is None or record[0] != canonical:
            record = [canonical, (record[1] + 1) if record is not None else 1]
            self._versions[slot] = record
            self._revision += 1
        return {
            "slot": slot,
            "key": key.to_dict() if key is not None else None,
            "canonical": canonical,
            "count": entry.count if entry is not None else 0,
            "version": record[1],
        }

    def anchor_verdict(self, anchor, allow_empty=False):
        # type: (SlotAnchor, bool) -> tuple
        """核对一个槽锚，返回 `(ok, reason, detail)`。**只读，不改任何状态。**

        `allow_empty=True` 供**插入**方向用：往一个空槽里放东西是合法的，
        而取出方向遇到空槽必须拒绝。指纹那一关两个方向都过——它才是
        「不会误取替换后的物品」的落点。
        """
        if not self.can_read():
            return False, Reason.CONTAINER_UNCERTAIN, "背包不可读，无法核对槽锚"
        view = self.slot_view(anchor.slot)
        if anchor.expected_key is None:
            return False, Reason.BAD_PARAMETER, "请求没有给出期望物品指纹"
        if view["key"] is None:
            if allow_empty:
                return (
                    self._version_verdict(anchor, view)
                )
            return False, Reason.SLOT_CHANGED, "槽 %d 现在是空的" % (anchor.slot,)
        if view["canonical"] != anchor.expected_key.canonical():
            # **这一条就是「不会误取替换后的物品」**：槽里的东西已经不是
            # 请求所依据的那一件了，拒绝，而不是照单全收。
            return (
                False,
                Reason.SLOT_CHANGED,
                "槽 %d 已不是请求依据的物品" % (anchor.slot,),
            )
        return self._version_verdict(anchor, view)

    def _version_verdict(self, anchor, view):
        # type: (SlotAnchor, dict) -> tuple
        """槽版本核对。空槽在**插入**方向也要过这一关：客户端依据一份
        旧快照往一个已经变了又变回去的槽里放东西，是同一类过期请求。"""
        if (
            anchor.expected_version is not None
            and int(anchor.expected_version) != view["version"]
        ):
            return (
                False,
                Reason.STALE_VERSION,
                "槽 %d 的版本已由 %r 变为 %r"
                % (anchor.slot, anchor.expected_version, view["version"]),
            )
        return True, "", ""

    def _slot_primitive(self, name):
        # type: (str) -> object
        """取适配器的**按槽原语**。没有就返回 None。

        为什么必须有它：`ContainerAdapter.extract/insert` 按**资源键**工作，
        实现从 0 号槽起整表扫描。对「第 7 个槽」这种要求，那会把东西
        放进/取出别的槽——而总量守恒让这种错**对不上账也查不出来**。
        宁可明确说「这个适配器不支持按槽操作」，也不要静默挪位置。
        """
        method = getattr(self.adapter, name, None)
        return method if callable(method) else None

    def anchored_extract(self, op_id, anchor, count):
        # type: (str, SlotAnchor, int) -> TransferReceipt
        """从槽锚指定的槽取出。核对不过就拒绝，**不做「退而求其次从别处拿」**。"""
        ok, reason, detail = self.anchor_verdict(anchor)
        if not ok:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED, reason,
                anchor.expected_key, self.holder_id(),
            )
        primitive = self._slot_primitive("extract_slot")
        if primitive is None:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.UNSUPPORTED,
                Reason.UNSUPPORTED, anchor.expected_key, self.holder_id(),
            )
        receipt = primitive(op_id, anchor.slot, anchor.expected_key, count)
        return self._recheck_after(receipt, anchor)

    def anchored_insert(self, op_id, anchor, count):
        # type: (str, SlotAnchor, int) -> TransferReceipt
        """插进槽锚指定的槽。槽里是别的东西时拒绝（`slot_occupied`）。"""
        if not self.can_write():
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED,
                Reason.CONTAINER_REFUSED, anchor.expected_key, self.holder_id(),
            )
        if anchor.expected_key is None:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED,
                Reason.BAD_PARAMETER, None, self.holder_id(),
            )
        # **版本也核对**：只核指纹会让「基于旧快照的插入」通过，
        # 而版本的意义正是识别那一类请求。取出方向由 `anchor_verdict` 统一核对，
        # 这里补上同一件事，免得两个方向用两套严格程度。
        ok, reason, detail = self.anchor_verdict(anchor, allow_empty=True)
        if not ok:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED, reason,
                anchor.expected_key, self.holder_id(),
            )
        primitive = self._slot_primitive("insert_slot")
        if primitive is None:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.UNSUPPORTED,
                Reason.UNSUPPORTED, anchor.expected_key, self.holder_id(),
            )
        receipt = primitive(op_id, anchor.slot, anchor.expected_key, count)
        return self._recheck_after(receipt, anchor)

    def _recheck_after(self, receipt, anchor):
        # type: (TransferReceipt, SlotAnchor) -> TransferReceipt
        """操作后复核那个槽。**复核不过一律降级为 `UNCERTAIN`。**

        为什么不是「报成功」：我们只核对了操作**之前**的状态，而操作与复核之间
        存在一个窗口。窗口里发生的事我们看不见，因此只能说「动过，但没法确认
        最终状态」——这与 13-01 的 `UnverifiableContainer` 是同一条纪律。
        """
        if receipt.status == TransferStatus.UNCERTAIN:
            return receipt
        if not self.can_read():
            return TransferReceipt(
                receipt.op_id, receipt.requested, receipt.moved,
                TransferStatus.UNCERTAIN, Reason.CONTAINER_UNCERTAIN,
                receipt.resource_key, receipt.holder or self.holder_id(),
            )
        view = self.slot_view(anchor.slot)
        expected_canonical = (
            anchor.expected_key.canonical() if anchor.expected_key is not None else ""
        )
        if view["canonical"] not in (expected_canonical, ""):
            return TransferReceipt(
                receipt.op_id, receipt.requested, receipt.moved,
                TransferStatus.UNCERTAIN, Reason.SLOT_CHANGED,
                receipt.resource_key, self.holder_id(),
            )
        if not receipt.holder:
            receipt.holder = self.holder_id()
        return receipt

    def anchored_swap(self, op_id, source_anchor, target_anchor):
        # type: (str, SlotAnchor, SlotAnchor) -> TransferReceipt
        """交换两个槽锚指定的槽——**一次动两格**，与取/放那两个单格原语不同。

        **两个锚的严格程度不一样，这是有意的：**

        - **源槽一律严格**（`allow_empty=False`）。源槽空着就没有可交换的
          东西，拒绝。
        - **目标槽允许为空**（`allow_empty=True`）。与空槽交换**就是移动**，
          这是定案的行为（`plans/blocks.md` 的「背包槽之间的拆分、合并与交换」），
          不是特例。这一支只核版本、不要求指纹——空槽本来就没有指纹。

        `inv_merge` 在服务层用 `if not ok and target_view["key"] is not None`
        表达同一件事；交换把它放回端点这一层，因为判定本来就属于这里。
        **不要把这一条「补」成两个锚都严格**：那会把「移动到空格」判死。

        引擎侧是一个真正的交换原语（`SetInvItemExchange`），**不是**
        「读两格再写两格」——后者第二次写入失败会把源物品同时留在两处，
        而总量守恒查不出复制。
        """
        source_key = source_anchor.expected_key
        if source_anchor.slot == target_anchor.slot:
            return TransferReceipt(
                op_id, 0, 0, TransferStatus.REFUSED, Reason.BAD_PARAMETER,
                source_key, self.holder_id(),
            )
        ok, reason, detail = self.anchor_verdict(source_anchor)
        if not ok:
            return TransferReceipt(
                op_id, 0, 0, TransferStatus.REFUSED, reason,
                source_key, self.holder_id(),
            )
        # 数量取自**刚刚核对过的那一次读**，不另外再读一遍：两次读之间
        # 若发生变动，复核那一关会把它降级成 `UNCERTAIN`，而不是拿一个
        # 从没被核对过的数量去记账。
        count = int(self.slot_view(source_anchor.slot)["count"] or 0)
        if count <= 0:
            return TransferReceipt(
                op_id, 0, 0, TransferStatus.REFUSED,
                Reason.INSUFFICIENT_STOCK, source_key, self.holder_id(),
            )
        ok, reason, detail = self.anchor_verdict(target_anchor, allow_empty=True)
        if not ok:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED, reason,
                source_key, self.holder_id(),
            )
        primitive = self._slot_primitive("swap_slots")
        if primitive is None:
            # 容器没有这个原语（基类刻意不提供会复制物品的默认实现）。
            return TransferReceipt(
                op_id, count, 0, TransferStatus.UNSUPPORTED,
                Reason.UNSUPPORTED, source_key, self.holder_id(),
            )
        if not primitive(source_anchor.slot, target_anchor.slot):
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED,
                Reason.CONTAINER_REFUSED, source_key, self.holder_id(),
            )
        return self._recheck_swap(op_id, count, source_anchor, target_anchor)

    def _recheck_swap(self, op_id, count, source_anchor, target_anchor):
        # type: (str, int, SlotAnchor, SlotAnchor) -> TransferReceipt
        """交换之后复核。**复核不过一律降级为 `UNCERTAIN`。**

        判据与 `_recheck_after` 不同，不能复用：那边问的是「源槽空了没有」，
        而交换之后源槽里装着**对方的东西**，问「空没空」必然答错、每次都
        报不确定。这里换成正着问——**源物品现在是不是真的在目标槽里、
        数量对不对**，那正是这次操作声称做成的事。

        只查目标那一格：交换是双向的，目标对上了就说明两边都换了；
        再查源槽等于把同一个判据说两遍，而两遍里任何一遍的读取失败都会
        把一条成功的交换打成不确定。
        """
        if not self.can_read():
            return TransferReceipt(
                op_id, count, 0, TransferStatus.UNCERTAIN,
                Reason.CONTAINER_UNCERTAIN, source_anchor.expected_key,
                self.holder_id(),
            )
        expected = (
            source_anchor.expected_key.canonical()
            if source_anchor.expected_key is not None else ""
        )
        view = self.slot_view(target_anchor.slot)
        if view["canonical"] != expected or int(view["count"] or 0) != count:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.UNCERTAIN, Reason.SLOT_CHANGED,
                source_anchor.expected_key, self.holder_id(),
            )
        return TransferReceipt(
            op_id, count, count, TransferStatus.COMPLETE, "",
            source_anchor.expected_key, self.holder_id(),
        )


class AnchoredInventoryView(TransferEndpoint):
    """把背包端点与一个槽锚绑成**本次只能动这个槽**的端点视图（06-09）。

    **为什么需要它。** 按槽操作最初被写成了协调器之外的一条旁路：
    核对锚、调 `anchored_extract`、完事。那条路会漏掉三样东西——
    网络那一侧根本没人动（`inv_take` 于是把背包里的东西**凭空删掉**）、
    去重账本不参与（重发就再动一次）、外部输入通知不发（手动存入网络
    不算回流）。把锚做成端点视图之后，锚定路径与普通路径**走的是同一段
    代码**：预检、去重、取出、放入、余量回退全都一样，锚只影响「能进出多少」
    与「往哪个槽动」。

    它是**每次请求新建的一次性视图**，不持有长期状态。
    """

    kind = "inventory_view"

    def __init__(self, endpoint, anchor):
        # type: (InventoryEndpoint, SlotAnchor) -> None
        self.endpoint = endpoint
        self.anchor = anchor
        #: 取/放的方向。构造时定死，避免中途改向。
        self.taking = False

    def endpoint_id(self):
        # type: () -> str
        return "%s#slot%d" % (self.endpoint.endpoint_id(), self.anchor.slot)

    def holder_id(self):
        # type: () -> str
        return self.endpoint.holder_id()

    def is_valid(self):
        # type: () -> bool
        return self.endpoint.is_valid()

    def invalidate(self):
        # type: () -> None
        self.endpoint.invalidate()

    def can_read(self):
        # type: () -> bool
        return self.endpoint.can_read()

    def can_write(self):
        # type: () -> bool
        return self.endpoint.can_write()

    def anchor_verdict(self):
        # type: () -> tuple
        """锚是否仍然成立。预检与提交各查一次——**中间那个窗口是真实存在的**。"""
        return self.endpoint.anchor_verdict(self.anchor, allow_empty=not self.taking)

    def probe_supply(self, resource_key, count):
        # type: (ResourceKey, int) -> object
        """**只数这个槽**。锚不成立时返回 0，让预检给出 `slot_changed`。"""
        ok, _, _ = self.anchor_verdict()
        if not ok:
            return 0
        view = self.endpoint.slot_view(self.anchor.slot)
        if view["key"] is None:
            return 0
        return min(count, view["count"])

    def probe_space(self, resource_key, count):
        # type: (ResourceKey, int) -> object
        """**只算这个槽还能放多少。**"""
        ok, _, _ = self.anchor_verdict()
        if not ok:
            return 0
        view = self.endpoint.slot_view(self.anchor.slot)
        limit = self.endpoint.adapter.stack_limit_of(resource_key) if hasattr(
            self.endpoint.adapter, "stack_limit_of"
        ) else 64
        return min(count, max(0, limit - view["count"]))

    def commit_extract(self, op_id, resource_key, count, owner_id=""):
        # type: (str, ResourceKey, int, str) -> TransferReceipt
        return self.endpoint.anchored_extract(op_id, self.anchor, count)

    def commit_insert(self, op_id, resource_key, count, owner_id="", origin=None):
        # type: (str, ResourceKey, int, str, str) -> TransferReceipt
        return self.endpoint.anchored_insert(op_id, self.anchor, count)

    def describe(self):
        # type: () -> dict
        report = TransferEndpoint.describe(self)
        report["slot"] = self.anchor.slot
        return report

    def __repr__(self):
        return "AnchoredInventoryView(%s, 槽%d)" % (
            self.endpoint.endpoint_id(), self.anchor.slot
        )


def anchored_extract_endpoint(endpoint, anchor):
    # type: (InventoryEndpoint, SlotAnchor) -> AnchoredInventoryView
    """取出方向的锚定视图（从背包往网络搬）。"""
    view = AnchoredInventoryView(endpoint, anchor)
    view.taking = True
    return view


def anchored_insert_endpoint(endpoint, anchor):
    # type: (InventoryEndpoint, SlotAnchor) -> AnchoredInventoryView
    """放入方向的锚定视图（从网络往背包搬）。"""
    view = AnchoredInventoryView(endpoint, anchor)
    view.taking = False
    return view


# -- 按槽原语（引擎适配器与假容器共用同一份实现）---------------------------
#
# **为什么放在这里而不是各自的适配器里。** 按槽语义（堆叠上限、读回核对、
# 槽被占或装着别的东西时的处理）是**一条规则**，而它有两个载体：
# `adapters/containers_engine.py` 的 `SlotBackedContainer`（真实引擎）
# 与 `adapters/containers.py` 的 `FakeContainer`（测试替身，也是 13-06 的
# 用例载体）。各写一份的代价是「真机能放、假容器放不了」这类偏差只在
# 游戏里暴露。因此规则写在这里，两边只提供三个原始操作：
# `read_slot` / `write_slot` / `stack_limit_of`。


def slot_read(adapter, slot):
    # type: (object, int) -> dict
    """该槽现在是什么。返回 `{"key", "count"}`；空槽或读不到时 key 为 None。"""
    empty = {"key": None, "count": 0}
    reader = getattr(adapter, "read_slot", None)
    if not callable(reader):
        return empty
    data = reader(slot)
    if not data or data.get("count", 0) <= 0:
        return empty
    key = item_key_from_dict(data)
    if key is None:
        return empty
    return {"key": key, "count": int(data.get("count", 0))}


def slot_limit_of(adapter, resource_key):
    # type: (object, ResourceKey) -> int
    limit_of = getattr(adapter, "stack_limit_of", None)
    limit = int(limit_of(resource_key)) if callable(limit_of) else 64
    return limit if limit > 0 else 1


def slot_insert(adapter, op_id, slot, resource_key, count):
    # type: (object, str, int, ResourceKey, int) -> TransferReceipt
    """**只动这一格**地放入，上限是该物品的堆叠上限，写完读回核对。"""
    view = slot_read(adapter, slot)
    if view["key"] is not None and view["key"] != resource_key:
        return TransferReceipt(
            op_id, count, 0, TransferStatus.REFUSED, Reason.SLOT_OCCUPIED,
            resource_key,
        )
    limit = slot_limit_of(adapter, resource_key)
    take = min(max(0, limit - view["count"]), count)
    if take <= 0:
        return TransferReceipt(
            op_id, count, 0, TransferStatus.REFUSED, Reason.NO_CAPACITY,
            resource_key,
        )
    data = item_dict_from_key(resource_key, view["count"] + take)
    # 引擎可能写入成功却返回 False；必须读回，否则协调器会补偿出第二份。
    adapter.write_slot(slot, data)
    after = slot_read(adapter, slot)
    if after["key"] != resource_key:
        return TransferReceipt(
            op_id, count, 0, TransferStatus.REFUSED, Reason.CONTAINER_REFUSED,
            resource_key,
        )
    moved = after["count"] - view["count"]
    if moved >= count:
        return TransferReceipt(
            op_id, count, moved, TransferStatus.COMPLETE, "", resource_key
        )
    return TransferReceipt(
        op_id, count, moved, TransferStatus.PARTIAL, Reason.NO_CAPACITY,
        resource_key,
    )


def slot_extract(adapter, op_id, slot, resource_key, count):
    # type: (object, str, int, ResourceKey, int) -> TransferReceipt
    """**只动这一格**地取出，写完读回核对。"""
    view = slot_read(adapter, slot)
    if view["key"] is None or view["key"] != resource_key:
        return TransferReceipt(
            op_id, count, 0, TransferStatus.REFUSED, Reason.SLOT_CHANGED,
            resource_key,
        )
    take = min(view["count"], count)
    if take <= 0:
        return TransferReceipt(
            op_id, count, 0, TransferStatus.REFUSED, Reason.INSUFFICIENT_STOCK,
            resource_key,
        )
    left = view["count"] - take
    data = item_dict_from_key(resource_key, left) if left > 0 else None
    # 同插入：以读回的实际变化为准，不把布尔失败当作没有发生过取出。
    adapter.write_slot(slot, data)
    after = slot_read(adapter, slot)
    if after["key"] is not None and after["key"] != resource_key:
        return TransferReceipt(
            op_id, count, 0, TransferStatus.REFUSED, Reason.CONTAINER_REFUSED,
            resource_key,
        )
    moved = view["count"] - after["count"]
    if moved >= count:
        return TransferReceipt(
            op_id, count, moved, TransferStatus.COMPLETE, "", resource_key
        )
    return TransferReceipt(
        op_id, count, moved, TransferStatus.PARTIAL,
        Reason.INSUFFICIENT_STOCK, resource_key,
    )


class SlotView(TransferEndpoint):
    """**只动一个槽**的端点视图（被动接口的存取位就是它）。

    与 `AnchoredInventoryView` 的分工：那个是「按槽**校验**后再动」，
    面向玩家点击（要核对指纹与槽版本）；这个是「就动这一格」，
    面向接口自己的轮询——槽号由**玩家配置的供货位**给出，不需要锚。

    它要求背后的适配器提供按槽原语（`SlotBackedContainer.insert_slot` /
    `extract_slot`）。没有就明确回 `unsupported`，**不退化成按资源键的整表扫描**
    ——后者会把东西放进/取出别的槽，而总量守恒让这种错对不上账也查不出来。
    """

    kind = "slot"

    def __init__(self, adapter, slot, endpoint_id=""):
        # type: (object, int, str) -> None
        self.adapter = adapter
        self.slot = int(slot)
        self._endpoint_id = endpoint_id

    def endpoint_id(self):
        # type: () -> str
        if self._endpoint_id:
            return "%s#%d" % (self._endpoint_id, self.slot)
        return "slot:%d" % (self.slot,)

    def holder_id(self):
        # type: () -> str
        return self.endpoint_id()

    def is_valid(self):
        # type: () -> bool
        if self.adapter is None:
            return False
        checker = getattr(self.adapter, "is_valid", None)
        return True if checker is None else bool(checker())

    def can_read(self):
        # type: () -> bool
        """能不能读这一格。判据是**三个原始操作齐不齐**
        （`read_slot` / `write_slot` / `stack_limit_of`），而不是
        「有没有一个叫 insert_slot 的方法」——按槽语义只有一份实现
        （本模块的 `slot_insert` / `slot_extract`）。
        """
        if not self.is_valid():
            return False
        for name in ("read_slot", "write_slot"):
            if not callable(getattr(self.adapter, name, None)):
                return False
        return True

    def can_write(self):
        # type: () -> bool
        return self.can_read()

    def slot_view(self):
        # type: () -> dict
        """这一格现在是什么。读不到时返回空视图（**不是「空槽」**）。"""
        if not self.is_valid():
            return {"slot": self.slot, "key": None, "count": 0}
        view = slot_read(self.adapter, self.slot)
        view["slot"] = self.slot
        return view

    def probe_supply(self, resource_key, count):
        # type: (ResourceKey, int) -> object
        view = self.slot_view()
        if view["key"] is None:
            return 0
        return min(count, view["count"])

    def probe_space(self, resource_key, count):
        # type: (ResourceKey, int) -> object
        """这一格还能放多少。

        **按该物品的真实堆叠上限算**（`adapter.stack_limit_of`，引擎里就是
        `GetItemBasicInfo().maxStackSize`）。早先那个「9 格 × 64 件 = 576」
        的估计正是被这条规则否掉的——9 格不等于任何物品都能放 576 件。
        槽里是**别的**东西时返回 0：这一格被占了，换一格。
        """
        if not self.can_write():
            return None
        view = self.slot_view()
        limit = slot_limit_of(self.adapter, resource_key)
        if view["key"] is not None and view["key"] != resource_key:
            return 0
        return min(count, max(0, limit - view["count"]))

    def commit_extract(self, op_id, resource_key, count, owner_id=""):
        # type: (str, ResourceKey, int, str) -> TransferReceipt
        if not self.can_read():
            return TransferReceipt(
                op_id, count, 0, TransferStatus.UNSUPPORTED,
                Reason.UNSUPPORTED, resource_key, self.holder_id(),
            )
        return slot_extract(self.adapter, op_id, self.slot, resource_key, count)

    def commit_insert(self, op_id, resource_key, count, owner_id="", origin=None):
        # type: (str, ResourceKey, int, str, str) -> TransferReceipt
        if not self.can_write():
            return TransferReceipt(
                op_id, count, 0, TransferStatus.UNSUPPORTED,
                Reason.UNSUPPORTED, resource_key, self.holder_id(),
            )
        return slot_insert(self.adapter, op_id, self.slot, resource_key, count)

    def describe(self):
        # type: () -> dict
        report = TransferEndpoint.describe(self)
        report["slot"] = self.slot
        return report

    def __repr__(self):
        return "SlotView(%s)" % (self.endpoint_id(),)


# -- 中转缓冲 --------------------------------------------------------------


class BufferLot(object):
    """缓冲里的一批资源。**来源标识记在批次上，不记在缓冲上。**

    同一份缓冲里可能同时躺着「外部物流刚送进来的」和「本模组搬过来等着转手的」
    两类东西，它们的回流语义完全不同（前者算新输入，后者不算）。
    把来源记在缓冲级别，就必须在混杂时二选一，而两种选法都错。
    """

    __slots__ = ("resource_key", "count", "origin", "owner_id", "op_id", "tick")

    def __init__(self, resource_key, count, origin, owner_id="", op_id="", tick=0):
        # type: (ResourceKey, int, str, str, str, int) -> None
        if origin not in (ORIGIN_EXTERNAL, ORIGIN_INTERNAL):
            raise ValueError("未知来源标识: %r" % (origin,))
        if count <= 0:
            raise ValueError("批次数量必须为正，得到 %r" % (count,))
        self.resource_key = resource_key
        self.count = int(count)
        self.origin = origin
        self.owner_id = owner_id
        self.op_id = op_id
        self.tick = int(tick)

    def to_dict(self):
        # type: () -> dict
        return {
            "resource_key": self.resource_key.to_dict(),
            "count": self.count,
            "origin": self.origin,
            "owner_id": self.owner_id,
            "op_id": self.op_id,
            "tick": self.tick,
        }

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> BufferLot
        return cls(
            ResourceKey.from_dict(data["resource_key"]),
            data["count"],
            data["origin"],
            data.get("owner_id", ""),
            data.get("op_id", ""),
            data.get("tick", 0),
        )

    def __repr__(self):
        return "BufferLot(%s x%d, %s)" % (
            self.resource_key.name, self.count, self.origin
        )


class TransferBuffer(object):
    """有界中转缓冲 + 资源归属台账（06-03）。

    容量是**件数**上限，由具名策略给出（R05，测试替身）。放不下时
    **部分接受并明确说还差多少**，不丢东西、也不假装放下。

    缓冲只记归属元数据，不解析存储介质——真正的存放处由配对它的
    `ContainerEndpoint`（接口方块自己的方块容器）负责。
    `for_op()` 返回一个按操作固定的视图，供一次转移使用。
    """

    __slots__ = (
        "capacity",
        "port_id",
        "schema_version",
        "tick",
        "backing",
        "_lots",
        "_total",
        "stats",
    )

    def __init__(self, capacity, port_id="", schema_version=TRANSFER_SCHEMA_VERSION,
                 backing=None):
        # type: (int, str, int, object) -> None
        self.capacity = int(capacity)
        self.port_id = port_id
        self.schema_version = int(schema_version)
        #: 最近一次由调用方设定的逻辑帧号。批次的时间戳来自它，
        #: **缓冲自己不数帧**——与存储服务同一条纪律。
        self.tick = 0
        #: 物品的**实际存放处**：接口方块自己的 `netease:block_container`
        #: （一个 `TransferEndpoint`）。`None` = 纯记账（离线测试、
        #: 或还没有容器适配器的设备）。
        #:
        #: 有 backing 时，进出**以 backing 的实际结果为准**，批次表跟着它走。
        #: 这样一来「外部物流已送入缓冲的资源仍保留」是结构性的：
        #: 东西真的在那个方块里，网络断开、主控卸载都不影响它。
        self.backing = backing
        if self.capacity <= 0:
            raise ValueError("缓冲容量必须为正，得到 %r" % (self.capacity,))
        #: FIFO 批次表。退出的顺序决定「先搬哪一批」，因此必须有序，
        #: 不能是 set 或 dict 的遍历顺序。
        self._lots = []
        self._total = 0
        self.stats = {
            "deposits": 0,
            "withdrawals": 0,
            "refused": 0,
            "lots_created": 0,
            "lots_consumed": 0,
            #: 批次表与实际存放处对不上的累计件数。**非零即缺陷的痕迹**：
            #: 它意味着有人绕过缓冲直接动了方块容器（玩家手动取走、
            #: 其他模组），或者一次存放的结果无法确认。
            "desync": 0,
        }

    def note_desync(self, amount):
        # type: (int) -> int
        """记一次批次表与实际存放处的偏差。返回累计值。"""
        if amount > 0:
            self.stats["desync"] += int(amount)
        return self.stats["desync"]

    # -- 查询 ----------------------------------------------------------

    def total(self):
        # type: () -> int
        return self._total

    def free(self):
        # type: () -> int
        return self.capacity - self._total

    def total_of(self, resource_key):
        # type: (ResourceKey) -> int
        total = 0
        for lot in self._lots:
            if lot.resource_key == resource_key:
                total += lot.count
        return total

    def lots(self):
        # type: () -> list
        """不可变快照。**纯数据**：改动它不影响缓冲内部。"""
        return [lot.to_dict() for lot in self._lots]

    def for_op(self, op_id, origin=ORIGIN_INTERNAL):
        # type: (str, str) -> BufferEndpoint
        """给某次操作用的端点视图。`origin` 记在**这次存进来的批次**上。

        取出口径是整个缓冲（FIFO，跨批次）：缓冲是这台接口自己的箱子，
        里面有什么就先搬什么。批次上的 `op_id` / `origin` 是**归属记录**，
        不是访问控制——把取用限定在「本次操作的批次」会让上一次半途而废的
        余料永远搬不出去（它的 `op_id` 再也不会被用到）。
        """
        return BufferEndpoint(self, op_id, origin)

    def describe(self):
        # type: () -> dict
        return {
            "port_id": self.port_id,
            "capacity": self.capacity,
            "total": self._total,
            "free": self.free(),
            "lots": len(self._lots),
            "schema_version": self.schema_version,
            "stats": dict(self.stats),
        }

    # -- 提交 ----------------------------------------------------------

    def deposit(self, op_id, resource_key, count, origin, owner_id="", tick=0):
        # type: (str, ResourceKey, int, str, str, int) -> int
        """存入。返回**实际接下的数量**（可能少于请求，放不下时就是这么多）。"""
        if count <= 0:
            return 0
        accepted = min(count, self.free())
        if accepted <= 0:
            self.stats["refused"] += 1
            return 0
        self._lots.append(
            BufferLot(resource_key, accepted, origin, owner_id, op_id, tick)
        )
        self._total += accepted
        self.stats["deposits"] += 1
        self.stats["lots_created"] += 1
        return accepted

    def withdraw(self, op_id, resource_key, count):
        # type: (str, ResourceKey, int) -> tuple
        """取出。返回 `(实际数量, [(批次, 取走的数量)])`。

        只按**稳定顺序**取：先按批次先后（FIFO）。取走的批次可能跨多个来源，
        因此把明细一并交出去——调用方据此知道「这次搬走的东西里，
        有多少是外部送进来的」。
        """
        if count <= 0:
            return 0, []
        remaining = count
        taken = []
        for lot in list(self._lots):
            if remaining <= 0:
                break
            if lot.resource_key != resource_key:
                continue
            take = min(lot.count, remaining)
            lot.count -= take
            remaining -= take
            taken.append((lot, take))
            if lot.count <= 0:
                self._lots.remove(lot)
                self.stats["lots_consumed"] += 1
        moved = count - remaining
        if moved:
            self._total -= moved
            self.stats["withdrawals"] += 1
        return moved, taken

    def forget(self, resource_key, count):
        # type: (ResourceKey, int) -> int
        """**按批次表销掉 count 个，但不动实际存放处。**

        它只在一种情况下被调用：实际存放处比账上少（有人绕过缓冲直接动了
        那个方块容器）。此时账要是照旧记着，`total_of` / `free` /
        `probe_supply` 会**永远**报着不存在的余量，而那台接口再也收不进东西。
        把账压到现实上，下一次读取自然就一致了。

        返回实际销掉的件数。
        """
        if count <= 0:
            return 0
        remaining = count
        for lot in list(self._lots):
            if remaining <= 0:
                break
            if lot.resource_key != resource_key:
                continue
            drop = min(lot.count, remaining)
            lot.count -= drop
            remaining -= drop
            if lot.count <= 0:
                self._lots.remove(lot)
                self.stats["lots_consumed"] += 1
        dropped = count - remaining
        if dropped:
            self._total -= dropped
        return dropped

    def totals(self):
        # type: () -> dict
        """按资源键的合计。**诊断与守恒核对用**，不在提交路径上跑。"""
        out = {}
        for lot in self._lots:
            out[lot.resource_key] = out.get(lot.resource_key, 0) + lot.count
        return out

    # -- 存档 ----------------------------------------------------------

    def to_dict(self):
        # type: () -> dict
        return {
            "schema_version": self.schema_version,
            "port_id": self.port_id,
            "capacity": self.capacity,
            "lots": [lot.to_dict() for lot in self._lots],
        }

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> TransferBuffer
        """读回。

        **未知的更新版本不按空缓冲处理**：抛 `ValueError` 让调用方（任务 12）
        保留原载荷并限制交易——「读成空盘再覆盖」正是 05 明令禁止的形态。
        """
        version = int(data.get("schema_version", 0))
        if version != TRANSFER_SCHEMA_VERSION:
            raise ValueError(
                "缓冲存档版本 %r 无法识别（本实现只认 %r）"
                % (version, TRANSFER_SCHEMA_VERSION)
            )
        buffer = cls(data["capacity"], data.get("port_id", ""), version)
        for raw in data.get("lots", ()):
            lot = BufferLot.from_dict(raw)
            buffer._lots.append(lot)
            buffer._total += lot.count
        if buffer._total > buffer.capacity:
            raise ValueError(
                "缓冲存档超出容量：%d > %d" % (buffer._total, buffer.capacity)
            )
        return buffer

    def __repr__(self):
        return "TransferBuffer(%s, %d/%d)" % (
            self.port_id, self._total, self.capacity
        )


class BufferEndpoint(TransferEndpoint):
    """缓冲这一端。

    缓冲是这台接口自己的箱子，因此：

    - `probe_supply` 看**整个缓冲**里这种资源的合计（物理上就这么多）；
    - `commit_extract` 按 FIFO 跨批次取走，并把**实际动到的来源**记下来
      （`last_extract_origins`），供协调器判断这一批里有多少算外部输入；
    - `commit_insert` 把这次进来的记成一批，带着 `origin` 与 `op_id`。

    `holder_id` 回答的是「缓冲里的东西归谁」——**归原主，不归缓冲**。
    它是 `TransferReceipt.holder` 在分段搬运里的取值来源。
    """

    kind = "buffer"

    def __init__(self, buffer, op_id, origin=ORIGIN_INTERNAL):
        # type: (TransferBuffer, str, str) -> None
        if origin not in (ORIGIN_EXTERNAL, ORIGIN_INTERNAL):
            raise ValueError("未知来源标识: %r" % (origin,))
        self.buffer = buffer
        self.op_id = op_id
        self.origin = origin
        self._last_origins = ()

    def endpoint_id(self):
        # type: () -> str
        return "buffer:%s" % (self.buffer.port_id or "?",)

    def holder_id(self):
        # type: () -> str
        """缓冲里的东西归**原主**。没有原主时归缓冲位置本身。

        不带资源键时只看「有没有主」，因为调用方（`TransferEndpoint` 的契约）
        不一定知道键。知道键的走 `holder_of(key)`——那个才答得准。
        """
        for lot in self.buffer._lots:
            if lot.owner_id:
                return lot.owner_id
        return self.endpoint_id()

    def holder_of(self, resource_key):
        # type: (ResourceKey) -> str
        """**这种资源**的原主。

        缓冲里可能同时躺着几批不同资源、分属不同的人。不看键就返回
        「第一个有主人的批次」会让回执说「余量在某甲手上」，而某甲名下那份
        根本不是这次搬的资源——恢复流程会去问一个无关的人。
        **报错人比报「说不清」更糟。**
        """
        for lot in self.buffer._lots:
            if lot.resource_key != resource_key:
                continue
            if lot.owner_id:
                return lot.owner_id
        for lot in self.buffer._lots:
            if lot.resource_key == resource_key:
                return self.endpoint_id()
        return self.endpoint_id()

    def can_read(self):
        # type: () -> bool
        return True

    def can_write(self):
        # type: () -> bool
        return True

    def probe_supply(self, resource_key, count):
        # type: (ResourceKey, int) -> object
        return min(count, self.buffer.total_of(resource_key))

    def probe_space(self, resource_key, count):
        # type: (ResourceKey, int) -> object
        return min(count, self.buffer.free())

    def last_extract_origins(self):
        # type: () -> tuple
        return self._last_origins

    def commit_extract(self, op_id, resource_key, count, owner_id=""):
        # type: (str, ResourceKey, int, str) -> TransferReceipt
        """取出。**先动 backing，再改批次表**——顺序不能反。

        反过来的话，backing 取出失败（方块被拆、槽被玩家拿走）时批次表已经
        减过了，那批东西就只存在于账上。先动 backing 的代价是可能多留一批
        还没销掉的记录，而那是一个**能自己纠正**的偏差（下次取出会把它抹平，
        `desync` 计数会记下来）。
        """
        limit = min(count, self.buffer.total_of(resource_key))
        moved, uncertain = self._backing_extract(op_id, resource_key, limit)
        self._last_origins = ()
        if uncertain is not None:
            # **存放处说不清，整条取出就是说不清。** 把它降级成「库存不足」
            # 会让调用方以为「这里没东西了」而换个 op_id 再试一次——
            # 那正是「不确定不重放」要防的第二次动作。
            return TransferReceipt(
                op_id, count, uncertain.moved, TransferStatus.UNCERTAIN,
                Reason.CONTAINER_UNCERTAIN, resource_key, self.holder_id(),
            )
        if moved < limit:
            # 账上说有、实际拿不到：**有人绕过缓冲直接动了那个方块容器**
            # （玩家手动取走、其他模组）。这是「批次表不是权威」的直接证据，
            # 因此**把账压到现实上**：只留痕不修正的话，`total_of`/`free`/
            # `probe_supply` 会永远报着不存在的余量，这台接口再也收不进东西，
            # 而且每次轮询都会再记一笔偏差。
            self.buffer.note_desync(limit - moved)
            self.buffer.forget(resource_key, limit - moved)
        if moved <= 0:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED,
                Reason.INSUFFICIENT_STOCK, resource_key, self.holder_id(),
            )
        _, taken = self.buffer.withdraw(op_id, resource_key, moved)
        merged = {}
        for lot, amount in taken:
            merged[lot.origin] = merged.get(lot.origin, 0) + amount
        self._last_origins = tuple(sorted(merged.items()))

        if moved == count and count > 0:
            return TransferReceipt(
                op_id, count, moved, TransferStatus.COMPLETE, "", resource_key,
                self.holder_of(resource_key),
            )
        return TransferReceipt(
            op_id, count, moved, TransferStatus.PARTIAL,
            Reason.CONTAINER_PARTIAL, resource_key, self.holder_of(resource_key),
        )

    def commit_insert(self, op_id, resource_key, count, owner_id="", origin=None):
        # type: (str, ResourceKey, int, str, str) -> TransferReceipt
        """存入。**先动 backing，再按实际结果记批次。**

        放不下的部分既不在 backing 里、也没有批次记录，因此它仍归原主——
        回执里的 `moved` 就是真正落下的数量。
        """
        room = min(count, self.buffer.free())
        accepted, uncertain = self._backing_insert(op_id, resource_key, room)
        if uncertain is not None:
            # 同取出：存放处说不清就不能报「缓冲满了」。后者会让调用方
            # 换个地方再试，而东西可能已经在这个箱子里了。
            return TransferReceipt(
                op_id, count, 0, TransferStatus.UNCERTAIN,
                Reason.CONTAINER_UNCERTAIN, resource_key, self.endpoint_id(),
            )
        if accepted > 0:
            self.buffer.deposit(
                self.op_id, resource_key, accepted, origin or self.origin,
                owner_id, self.buffer.tick,
            )
        holder = self.endpoint_id()
        if accepted == count and count > 0:
            return TransferReceipt(
                op_id, count, accepted, TransferStatus.COMPLETE, "", resource_key, holder
            )
        if accepted > 0:
            # 两种「只收了一部分」要说清楚是哪一种：**缓冲满了**（还能腾位置）
            # 与**存放处不肯收**（换多少个位置都没用）对玩家的含义完全不同。
            reason = (
                Reason.CONTAINER_PARTIAL
                if accepted < room
                else Reason.BUFFER_FULL
            )
            return TransferReceipt(
                op_id, count, accepted, TransferStatus.PARTIAL,
                reason, resource_key, holder,
            )
        return TransferReceipt(
            op_id, count, 0, TransferStatus.REFUSED,
            Reason.BUFFER_FULL, resource_key, holder,
        )

    def _backing_insert(self, op_id, resource_key, count):
        # type: (str, ResourceKey, int) -> tuple
        """把东西放进实际存放处。返回 `(实际放入量, 不确定回执或 None)`。

        **不确定时一个批次也不记**：记了等于宣称东西在缓冲里，而它可能在、
        也可能不在。留痕让待核对流程去查——记的是**请求量**而不是
        `receipt.moved`（后者恒为 0，那样这条留痕就等于没留）。
        """
        if count <= 0:
            return 0, None
        if self.buffer.backing is None:
            return count, None
        receipt = self.buffer.backing.commit_insert(op_id, resource_key, count)
        if receipt.status == TransferStatus.UNCERTAIN:
            self.buffer.note_desync(count)
            return 0, receipt
        return receipt.moved, None

    def _backing_extract(self, op_id, resource_key, count):
        # type: (str, ResourceKey, int) -> tuple
        """同上，返回 `(实际取出量, 不确定回执或 None)`。"""
        if count <= 0:
            return 0, None
        if self.buffer.backing is None:
            return count, None
        receipt = self.buffer.backing.commit_extract(op_id, resource_key, count)
        if receipt.status == TransferStatus.UNCERTAIN:
            self.buffer.note_desync(count)
            return 0, receipt
        return receipt.moved, None


class BufferDisposition(object):
    """缓冲里剩下的东西在取消/拆除时怎么处置。**R05 未定，因此不自行决定。**

    默认实现只做一件事：**留着，并记下它还在那儿**。不掉落、不复制、
    不静默丢弃——17 行代码换来的是一条能对用户说清楚的话，而不是三种猜法之一。
    任务 12 接真实恢复时替换成带归属的协议实现。
    """

    #: 诊断里显示的处置口径
    policy_id = "retain"

    def dispose(self, buffer, reason=""):
        # type: (TransferBuffer, str) -> dict
        """返回 `{ok, policy, held, lots}`。**不修改缓冲。**"""
        return {
            "ok": False,
            "policy": self.policy_id,
            "reason": reason,
            "held": buffer.total(),
            "lots": buffer.lots(),
            "note": "R05 未定案：本轮保留在缓冲与方块容器里并标记待恢复，不掉落、不复制",
        }


# -- 预检与提交 ------------------------------------------------------------


class TransferPlan(object):
    """预检结果。**零副作用的产物**：它只说明「能搬多少、为什么不能更多」。"""

    __slots__ = (
        "op_id",
        "resource_key",
        "requested",
        "movable",
        "status",
        "reasons",
        "source_id",
        "target_id",
    )

    def __init__(self, op_id, resource_key, requested, movable, status, reasons,
                 source_id, target_id):
        # type: (str, ResourceKey, int, int, str, tuple, str, str) -> None
        self.op_id = op_id
        self.resource_key = resource_key
        self.requested = int(requested)
        self.movable = int(movable)
        self.status = status
        self.reasons = tuple(reasons)
        self.source_id = source_id
        self.target_id = target_id

    def is_ok(self):
        # type: () -> bool
        """能不能动手。**部分可动也算 ok**：只搬得动一部分是有意义的结果。"""
        return self.movable > 0

    def reason_codes(self):
        # type: () -> tuple
        return tuple(reason_code(item) for item in self.reasons)

    def first_reason(self):
        # type: () -> str
        codes = self.reason_codes()
        return codes[0] if codes else ""

    def to_dict(self):
        # type: () -> dict
        return {
            "op_id": self.op_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "resource_key": self.resource_key.to_dict(),
            "requested": self.requested,
            "movable": self.movable,
            "status": self.status,
            "reasons": [
                item.to_dict() if isinstance(item, BlockReason) else item
                for item in self.reasons
            ],
        }

    def __repr__(self):
        return "TransferPlan(%s, %d/%d, %s)" % (
            self.op_id, self.movable, self.requested, self.status
        )


class TransferCoordinator(object):
    """串行化服务端提交并按实际数量回答的转移协调器（06-01/06-02/06-03）。

    它**不复制主控准入**：主控存在的检查、`run_state`（含能源与带宽）、
    节点代次、控制器是否支持该设备，都在 `MasterController.admit` 的第 5/8/9/11 条
    里判过了。这一层只补准入链答不了的：源端与目标端的**端点级**事实
    （能不能读、能不能写、各自还能进出多少）。
    """

    def __init__(self, policy=None, ledger=None, sink=None, network_id=""):
        # type: (TransferPolicy, object, object, str) -> None
        self.policy = policy if policy is not None else TransferPolicy()
        #: 去重账本。与 `StorageService` 用**同一个类**、同一套 `BEGIN_*` 语义，
        #: 但**各自一份实例**：存储服务记的是「介质读写操作」，
        #: 这里记的是「端点之间的搬运」，两者的键空间与保留期都不同。
        self._ledger = ledger if ledger is not None else RequestLedger()
        #: 合格回流入口（任务 10）。`None` = 还没接线，**不是「不用发」**——
        #: 那种情况下诊断会明确说「外部输入通知无处可去」。
        self.sink = sink
        self.network_id = network_id
        self._clock = 0
        #: `op_id -> 待核对记录`。`UNCERTAIN` 与「补偿也失败」的落点。
        self._pending = {}
        self._pending_order = []
        self.stats = {
            "preflights": 0,
            "commits": 0,
            "moved": 0,
            "partial": 0,
            "refused": 0,
            "uncertain": 0,
            "compensations": 0,
            "stranded": 0,
            "replayed": 0,
            "in_flight": 0,
            "external_inputs": 0,
            "sink_missing": 0,
            "reconcile_evicted": 0,
        }

    # -- 时钟 ----------------------------------------------------------

    def advance_clock(self, tick):
        # type: (int) -> int
        """推进时基。与存储服务同一条纪律：**不用方块 tick，不用墙上时间**。"""
        self._clock = int(tick)
        return self._clock

    def _now(self):
        # type: () -> int
        return self._clock

    # -- 预检（06-01）--------------------------------------------------

    def preflight(self, request, source, target):
        # type: (TransferRequest, TransferEndpoint, TransferEndpoint) -> TransferPlan
        """**零副作用**：不改库存、不消耗额度、不推进任何计数。

        检查顺序是写死的，因为它决定了「玩家先看到哪一条原因」：
        先看这次请求本身合不合法，再看两端在不在，最后才看数量够不够。
        反过来的话，一台被拆掉的设备会先报「库存不足」。
        """
        self.stats["preflights"] += 1
        reasons = []

        if request.count <= 0:
            return self._plan(
                request, source, target, 0, TransferStatus.REFUSED,
                [BlockReason(Reason.OUT_OF_RANGE, "转移数量必须为正")],
            )

        for label, endpoint in (("来源", source), ("目标", target)):
            if endpoint is None:
                return self._plan(
                    request, source, target, 0, TransferStatus.REFUSED,
                    [BlockReason(Reason.BAD_PARAMETER, "%s端点缺失" % (label,))],
                )
            if not endpoint.is_valid():
                return self._plan(
                    request, source, target, 0, TransferStatus.REFUSED,
                    [
                        BlockReason(
                            Reason.ENDPOINT_INVALID,
                            "%s端点 %s 已失效（邻接容器被拆、维度变化或依赖关闭）"
                            % (label, endpoint.endpoint_id()),
                        )
                    ],
                )

        if not source.can_read():
            return self._plan(
                request, source, target, 0, TransferStatus.UNSUPPORTED,
                [
                    BlockReason(
                        Reason.UNSUPPORTED,
                        "来源端点 %s 不可读，无法核对实际数量"
                        % (source.endpoint_id(),),
                    )
                ],
            )
        if not target.can_write():
            return self._plan(
                request, source, target, 0, TransferStatus.UNSUPPORTED,
                [
                    BlockReason(
                        Reason.UNSUPPORTED,
                        "目标端点 %s 不接受写入" % (target.endpoint_id(),),
                    )
                ],
            )

        supply = source.probe_supply(request.resource_key, request.count)
        # `None` = 不知道。**不当作 0**（那会让「读不到」变成「没有」），
        # 也不当作无限；按请求量去试，由 `commit` 的核对给实际值。
        available = request.count if supply is None else int(supply)
        if available <= 0:
            return self._plan(
                request, source, target, 0, TransferStatus.REFUSED,
                [
                    BlockReason(
                        Reason.INSUFFICIENT_STOCK,
                        "来源端点 %s 没有可动的 %s"
                        % (source.endpoint_id(), request.resource_key.name),
                    )
                ],
            )

        wanted = min(request.count, available)
        space = target.probe_space(request.resource_key, wanted)
        capacity = wanted if space is None else int(space)
        movable = min(wanted, capacity)
        if movable <= 0:
            return self._plan(
                request, source, target, 0, TransferStatus.REFUSED,
                [
                    BlockReason(
                        Reason.NO_CAPACITY,
                        "目标端点 %s 放不下 %s"
                        % (target.endpoint_id(), request.resource_key.name),
                    )
                ],
            )

        status = (
            TransferStatus.COMPLETE
            if movable >= request.count
            else TransferStatus.PARTIAL
        )
        if status == TransferStatus.PARTIAL:
            reasons.append(
                BlockReason(
                    Reason.CONTAINER_PARTIAL,
                    "只能搬动 %d/%d（来源可动 %d，目标可容纳 %d）"
                    % (movable, request.count, available, capacity),
                )
            )
        return self._plan(request, source, target, movable, status, reasons)

    def _plan(self, request, source, target, movable, status, reasons):
        # type: (TransferRequest, object, object, int, str, list) -> TransferPlan
        return TransferPlan(
            request.op_id,
            request.resource_key,
            request.count,
            movable,
            status,
            reasons,
            source.endpoint_id() if source is not None else request.source_id,
            target.endpoint_id() if target is not None else request.target_id,
        )

    # -- 提交（06-02）--------------------------------------------------

    def commit(self, request, source, target, plan=None, return_to=None, tick=None):
        # type: (TransferRequest, TransferEndpoint, TransferEndpoint, TransferPlan, TransferEndpoint, int) -> TransferReceipt
        """受控提交。**只按实际数量记账**，并保证余量有明确归属。

        步骤与理由：

        1. 预检（或复核调用方给的 plan）；
        2. 去重：同一 `(op_id, 来源->目标)` 重发返回**原回执**，不搬第二次；
        3. 从源端取出 → 实际量 `moved1`；
        4. 放进目标 → 实际量 `placed`；
        5. `moved1 - placed` 这一份既不在源、也没进目标，**必须放回**
           （默认放回来源端；分段搬运时放回中转位）。放回也失败 → `UNCERTAIN`
           并记进待核对台账，**绝不报告成成功**。

        第 5 步就是「避免先删源再假定目标接收」：`TransferReceipt.holder`
        给出的永远是**余量所在的真实位置**。
        """
        source_id = source.endpoint_id() if source is not None else request.source_id
        target_id = target.endpoint_id() if target is not None else request.target_id
        holder = source.holder_id() if source is not None else ""
        now = self._now() if tick is None else int(tick)

        # **去重排在预检之前**，与主控准入链第 2 条同序。理由：去重问的是
        # 「这条请求做过没有」，与「此刻还做不做得成」无关。反过来的话，
        # 一条已经搬空了来源的请求重发会报「库存不足」——听起来像没搬过，
        # 而实际是搬过了，调用方据此重试就会重复搬。
        key = (request.op_id, "%s->%s" % (source_id, target_id))
        state = self._ledger.begin(key, now)
        if state == RequestLedger.BEGIN_REPLAYED:
            stored = self._ledger.replay(key)
            if stored is not None:
                self.stats["replayed"] += 1
                return stored
        if state == RequestLedger.BEGIN_IN_FLIGHT:
            # **不确定就不重放**（06-02）。在途的那一次可能已经动了，
            # 也可能还没动；两种情况下再动一次都是错的。
            self.stats["in_flight"] += 1
            return TransferReceipt(
                request.op_id, request.count, 0, TransferStatus.UNCERTAIN,
                Reason.DUPLICATE_REQUEST, request.resource_key, holder,
            )

        # **调用方给的 plan 一律用新预检夹一次。** 不夹的话，一份算好之后
        # 放了一会儿的 plan 会拿着过期的 `movable` 去动手——源被搬空了、
        # 目标被塞满了，它都不知道。夹一次是 O(一次预检)，而这条路径本来
        # 就要动真的资源。
        fresh = self.preflight(request, source, target)
        if plan is None:
            plan = fresh
        elif fresh.movable < plan.movable:
            plan = fresh
        if not plan.is_ok():
            # **没动过任何东西的请求不留在账本里**（与主控同一条纪律：
            # 账本防的是重复执行，不是缓存答案）。否则一次「暂时放不下」
            # 会让同一个 op_id 永远拿到同一条拒绝。
            self._ledger.abort(key)
            self.stats["refused"] += 1
            return TransferReceipt(
                request.op_id,
                request.count,
                0,
                plan.status,
                plan.first_reason(),
                request.resource_key,
                holder,
            )

        try:
            receipt = self._do_commit(request, source, target, plan, return_to, now)
        except Exception:
            # 抛错时**把在途标记清掉**，否则同一个 op_id 永远拿到 `in_flight`。
            # 缺陷仍然向上抛（项目纪律：主控内部不吞异常）。
            self._ledger.abort(key)
            raise
        if receipt.status == TransferStatus.UNCERTAIN:
            # **不结算，把在途标记留着。**（06-02：不确定不能作为成功，
            # 也不能盲目重试。）留着的效果正是这两条：同一个 op_id 重发会
            # 命中 `BEGIN_IN_FLIGHT` 拿到同一条不确定回执，而不是又搬一次。
            # 标记会随保留期被 `prune` 清掉——那之后由恢复流程（12-07）
            # 核对，不由重试逻辑补做。
            self.stats["uncertain"] += 1
        else:
            self._ledger.finish(key, receipt, now)
        return receipt

    def _do_commit(self, request, source, target, plan, return_to, now):
        # type: (TransferRequest, TransferEndpoint, TransferEndpoint, TransferPlan, TransferEndpoint, int) -> TransferReceipt
        key = request.resource_key
        count = plan.movable
        owner = request.owner_id

        extracted = source.commit_extract(request.op_id + _SUB_OUT, key, count, owner)
        if extracted.status == TransferStatus.UNCERTAIN:
            self._note_pending(
                request, extracted, target.endpoint_id(),
                "来源端已动作但无法确认结果，不得据此重发整批",
            )
            return self._receipt(
                request, 0, TransferStatus.UNCERTAIN,
                Reason.CONTAINER_UNCERTAIN,
                source.holder_id(), now,
            )
        moved1 = extracted.moved
        if moved1 <= 0:
            self.stats["refused"] += 1
            return self._receipt(
                request, 0, TransferStatus.REFUSED,
                extracted.reason or Reason.INSUFFICIENT_STOCK,
                source.holder_id(), now,
            )

        placed_receipt = target.commit_insert(
            request.op_id + _SUB_IN, key, moved1, owner, request.origin
        )
        if placed_receipt.status == TransferStatus.UNCERTAIN:
            # 目标端说不清楚。东西**可能**已经进去了，因此不能报成功，
            # 也不能把它算成还留在来源端。
            self._note_pending(
                request, placed_receipt, target.holder_id(),
                "目标端已动作但无法确认结果，余量归属待核对",
            )
            return self._receipt(
                request, 0, TransferStatus.UNCERTAIN,
                Reason.CONTAINER_UNCERTAIN,
                # `holder_id()` 而不是 `endpoint_id()`：回执里的 `holder`
                # 回答的是「东西现在归谁」，而不是「这是哪个端点」。
                # 两者对缓冲/背包端点并不相同（前者报原主）。
                target.holder_id(), now,
            )
        placed = placed_receipt.moved

        holder = target.holder_of(key)
        leftover = moved1 - placed
        if leftover > 0:
            holder = self._return_leftover(
                request, return_to if return_to is not None else source,
                key, leftover, owner, now, source.last_extract_origins(),
            )

        self.stats["commits"] += 1
        self.stats["moved"] += placed
        if placed < request.count:
            self.stats["partial"] += 1
        reason = ""
        if placed >= request.count:
            status = TransferStatus.COMPLETE
        elif placed > 0:
            status = TransferStatus.PARTIAL
            reason = Reason.CONTAINER_PARTIAL
        else:
            status = TransferStatus.REFUSED
            reason = placed_receipt.reason or Reason.CONTAINER_REFUSED

        receipt = self._receipt(request, placed, status, reason, holder, now)
        self._deliver_external_input(request, source, placed, now)
        return receipt

    def _return_leftover(self, request, back, key, leftover, owner, now, origins=()):
        # type: (TransferRequest, TransferEndpoint, ResourceKey, int, str, int, tuple) -> str
        """把没搬成的那一份放回一个可追踪的位置。返回它现在归谁（说不清时是空串）。

        `stats["stranded"]` 记的是**件数**而不是次数：诊断要回答的是
        「有多少件东西说不清在哪」，而不是「发生过几次回滚失败」。

        **来源标识要跟着回退走。** 从缓冲里搬出来的可能是混合来源（一部分
        外部送进来的、一部分本模组搬的），退回去时如果一律写成「内部」，
        那批外部东西以后再进网络就**再也不会**被算成回流。因此这里按原样
        拆成几笔退回——多写一两行，换的是归属不丢。
        """
        if back is None or not back.can_write():
            self.stats["stranded"] += leftover
            self._note_pending(
                request, None, "",
                "有 %d 个 %s 既未进入目标、也无处放回" % (leftover, key.name),
            )
            return ""
        self.stats["compensations"] += 1
        moved = 0
        for origin, amount in self._split_by_origin(origins, leftover):
            undo = back.commit_insert(
                request.op_id + _SUB_UNDO, key, amount, owner, origin
            )
            moved += undo.moved
            if undo.moved < amount:
                break
        if moved >= leftover:
            return back.holder_of(key)
        stranded = leftover - moved
        # 放回**也没成功**。这是三方都不确定的状态，必须留痕：
        # 报成功会让这批东西变成账外资产，静默丢弃更是直接违反守恒。
        # **`holder` 报空串**：东西到底在哪已经没人知道了，
        # 报一个位置等于让恢复流程去一个错的地方找。
        self.stats["stranded"] += stranded
        self._note_pending(
            request, None, "",
            "回滚也未完成，仍有 %d 个 %s 归属待核对" % (stranded, key.name),
        )
        return ""

    @staticmethod
    def _split_by_origin(origins, amount):
        # type: (tuple, int) -> tuple
        """把 `amount` 件按 `origins` 的比例拆成 `((origin, count), ...)`。

        没有来源信息时按**内部**处理：内部搬运不发回流通知，
        因此在信息不足时保守地不报，好过凭空报一次外部输入
        （那会让同一份资源被反复计入订单产物）。
        余数归第一个来源，保证各段之和恰好等于 `amount`——丢一件或多一件
        都违反守恒。
        """
        if amount <= 0:
            return ()
        total = sum(count for _, count in origins)
        if not origins or total <= 0:
            return ((ORIGIN_INTERNAL, amount),)
        out = []
        assigned = 0
        for index, (origin, count) in enumerate(origins):
            if index == len(origins) - 1:
                take = amount - assigned
            else:
                take = amount * count // total
            if take > 0:
                out.append((origin, take))
                assigned += take
        if assigned < amount:
            out.append((origins[0][0], amount - assigned))
        return tuple(out)

    def _receipt(self, request, moved, status, reason, holder, now):
        # type: (TransferRequest, int, str, str, str, int) -> TransferReceipt
        return TransferReceipt(
            request.op_id,
            request.count,
            moved,
            status,
            reason,
            request.resource_key,
            holder,
        )

    # -- 合格回流入口（06-08）------------------------------------------

    def _external_moved(self, request, source, moved):
        # type: (TransferRequest, TransferEndpoint, int) -> int
        """这次搬动里**有多少算外部送进网络的新输入**。

        判据优先取来源端点自己报的批次来源（中转缓冲里可能混着外部送进来的
        和本模组搬过来的），拿不到就退回请求上的 `origin`。
        结果按实际到达目标的量截断：没进网络的部分不算输入。
        """
        origins = source.last_extract_origins()
        if not origins:
            return moved if request.origin == ORIGIN_EXTERNAL else 0
        external = 0
        for origin, amount in origins:
            if origin == ORIGIN_EXTERNAL:
                external += amount
        return min(external, moved)

    def _deliver_external_input(self, request, source, moved, now):
        # type: (TransferRequest, TransferEndpoint, int, int) -> None
        """只有**外部来源、真的进入了网络**的那一份才发通知。

        内部搬运（缓冲↔网络、介质间移动、补偿）不发：把它记成输入会让同一份
        资源被反复计入订单产物（全局约束 3，`architecture_analysis.md` §6.4）。

        `moved` 已经是「到达目标的实际量」，但来源端可能是混合来源的缓冲，
        因此这里再按来源截一次。
        """
        count = self._external_moved(request, source, moved)
        if count <= 0:
            return
        if self.sink is None:
            # **不是「不用发」**，是「还没地方发」。计数并留痕，
            # 让 10 接线之前这件事在诊断里看得见。
            self.stats["sink_missing"] += 1
            return
        self.sink(request, count, now)
        self.stats["external_inputs"] += 1

    # -- 待核对台账（12-07 的入口）-------------------------------------

    def _note_pending(self, request, receipt, holder, detail):
        # type: (TransferRequest, object, str, str) -> None
        """记一条「动过但说不清」的记录。

        **不重放**是这里的全部意义：这张表是给恢复流程看的，
        不是给重试逻辑看的（06-02 明写「结果不确定不能作为成功，
        也不能盲目重试」）。
        """
        entry = {
            "op_id": request.op_id,
            "resource_key": request.resource_key,
            "count": request.count,
            "moved": receipt.moved if receipt is not None else 0,
            "source_id": request.source_id,
            "target_id": request.target_id,
            "holder": holder,
            "detail": detail,
            "tick": self._now(),
        }
        if request.op_id not in self._pending:
            self._pending_order.append(request.op_id)
        self._pending[request.op_id] = entry
        # 超出上限时按最旧淘汰，**并且计数**：静默丢弃待核对记录等于让
        # 「东西不知道去哪了」这件事从诊断里消失。
        while len(self._pending_order) > self.policy.reconcile_limit:
            oldest = self._pending_order.pop(0)
            self._pending.pop(oldest, None)
            self.stats["reconcile_evicted"] += 1

    def pending_reconcile(self):
        # type: () -> list
        """待核对记录的快照。**纯数据**。"""
        return [
            dict(self._pending[op_id])
            for op_id in self._pending_order
            if op_id in self._pending
        ]

    def clear_pending(self, op_id):
        # type: (str) -> bool
        """核对完成后销掉一条。返回是否真的销掉了。"""
        if op_id not in self._pending:
            return False
        self._pending.pop(op_id, None)
        try:
            self._pending_order.remove(op_id)
        except ValueError:
            pass
        return True

    # -- 推进与诊断 ----------------------------------------------------

    def tick(self, tick=None):
        # type: (int) -> int
        """推进时基与账本清理。**不做任何搬运**——搬运由接口按轮询驱动。"""
        now = self._now() if tick is None else int(tick)
        self._clock = now
        self._ledger.prune(now)
        return 0

    def diagnostics(self):
        # type: () -> dict
        policy = self.policy.describe()
        return {
            "network_id": self.network_id,
            "clock_tick": self._clock,
            "stats": dict(self.stats),
            "ledger": dict(self._ledger.stats),
            "policy": policy,
            "unresolved": self.policy.unresolved_report(),
            "pending": self.pending_reconcile(),
            "sink_wired": self.sink is not None,
            "scope_note": (
                "预检、受控提交、实际数量回执与有界中转缓冲已实现；"
                "端点的真实引擎实现（背包、外部容器）在 adapters/containers_engine.py，"
                "**未经游戏内实测**。"
                + (
                    "合格回流入口（任务 10）尚未接线，外部输入通知目前无处可去——"
                    "那是「还没接」而不是「不用发」。"
                    if self.sink is None
                    else ""
                )
            ),
        }

    def __repr__(self):
        return "TransferCoordinator(%s, pending=%d)" % (
            self.network_id or "?",
            len(self._pending),
        )


# -- 回执 -> 命令结果 ------------------------------------------------------

#: 请求依据的东西已经过期：**取新快照再发**就有机会成功。
#: 与「暂时不通」分开，因为前者重发同样的载荷永远不成功、后者才值得重试。
_STALE_CODES = frozenset(
    [
        Reason.STALE_VERSION,
        Reason.STALE_INSTANCE,
        # 槽里的东西已经不是请求依据的那一件了：重发等于误操作，
        # 玩家要看到的是「界面刷新一下」而不是「稍后再试」。
        Reason.SLOT_CHANGED,
    ]
)

#: 请求本身不合法：**重发无用**。
_REJECTED_CODES = frozenset(
    [
        Reason.BAD_PARAMETER,
        Reason.OUT_OF_RANGE,
        Reason.SLOT_OCCUPIED,
        Reason.NOT_A_MEDIUM,
        Reason.UNSUPPORTED,
        Reason.ENDPOINT_INVALID,
        Reason.PORT_DIRECTION_MISMATCH,
        Reason.CURSOR_UNRESOLVED,
    ]
)


def receipt_result(request_id, receipt, action="transfer"):
    # type: (str, TransferReceipt, str) -> object
    """把 `TransferReceipt` 翻译成 `CommandResult`。

    **四种归宿的划分是这一层最重要的一件事**，因为界面要据此给出不同的
    下一步提示（与 `StorageService._refuse_mount` 同一套口径）：

    | 状态 | 玩家的下一步 |
    | --- | --- |
    | `ok` / `partial` | 已完成或部分完成，实际量在 `receipt.moved` 里 |
    | `stale` | 取新快照、刷新界面，**重发同样的载荷没有用** |
    | `rejected` | 请求本身不合法，重发无用 |
    | `blocked` | 暂时不通（没空间、没电、结果不确定），稍后可能可以 |

    `UNCERTAIN` **永远落在 `blocked`**，并带上余量当前的位置——
    06-02 要求它既不算成功也不能盲目重试。
    """
    from ...common.protocol import STATUS_BLOCKED, BlockReason, CommandResult

    payload = {"action": action, "receipt": receipt.to_dict()}
    if receipt.is_complete():
        return CommandResult.ok(request_id, payload)
    if receipt.status == TransferStatus.UNCERTAIN:
        # **不确定也要把回执带上。** `CommandResult.blocked()` 会把 payload
        # 压成 `None`，而这条结果的接收方（接口的轮询）正是靠回执里的
        # `status` 判断「要不要把这条通路停住」的——丢掉它，接口就会
        # 换个 op_id 再试一次，那正是 06-02 禁止的盲目重发。
        # 因此这里显式构造，不用快捷方式。
        return CommandResult(
            request_id,
            STATUS_BLOCKED,
            payload,
            [
                BlockReason(
                    Reason.CONTAINER_UNCERTAIN,
                    "结果不确定，不得重发整批（余量位置：%s）"
                    % (receipt.holder or "无法确认",),
                )
            ],
        )
    if receipt.status == TransferStatus.PARTIAL:
        return CommandResult.partial(
            request_id,
            payload,
            [
                BlockReason(
                    receipt.reason or Reason.CONTAINER_PARTIAL,
                    "只完成了 %d/%d，余量仍在 %s"
                    % (receipt.moved, receipt.requested, receipt.holder or "来源端"),
                )
            ],
        )
    code = receipt.reason or Reason.INSUFFICIENT_STOCK
    detail = "这次没有搬动任何资源（%s）" % (code,)
    if code in _STALE_CODES:
        return CommandResult.stale(request_id, [BlockReason(code, detail)])
    if code in _REJECTED_CODES:
        return CommandResult.rejected(request_id, [BlockReason(code, detail)])
    return CommandResult.blocked(request_id, [BlockReason(code, detail)])
