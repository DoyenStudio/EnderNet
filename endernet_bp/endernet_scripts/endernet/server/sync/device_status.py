# coding=utf-8
"""设备总体状态的上行编码与订阅记账（11-08 / 11-13）。

对应 `11. 终端与客户端同步.md` 的 11-08 与「总体状态与详情」一节，以及
`script_architecture.md` 里 `server/sync/device_status.py` 那一格。本模块
**不 import 引擎**（`server/sync/` 是域目录，由 `test_import_boundaries.py`
静态强制），也不直接 import `server/network/`——判定通过注入的 `status_of`
拿（`ServerBootstrap._sync_status_of` → `RuntimeManager.device_status`），
于是整条上行路径可以离线驱动。

## 这里**不做**「聚合」，聚合已经交付了

11-08 的字面要求是「汇总断电、无主控、冲突、超载、型号不支持及恢复中
原因」。但把带宽、能源、主控与型号合成**一个**在线结论的那一步，早在
任务 03 就落在 `server/network/controller.py` 的 `compose_device_status()`
里了（`DeviceNetworkStatus`，只读、无副作用）。因此本模块**不重算一遍**、
也不复制一份原因表——复制出来的那份迟早会与真身漂移，而「界面显示的
离线原因与真实拦截原因不一致」正是 02-07 明令要避免的那类指错方向。

本模块只做三件事：

1. `build_status_message()`：把 `DeviceNetworkStatus` 编成线上消息（纯函数）；
2. `read_status()`：守卫式读取——读不到、读到控制器、读到过期代次、或
   读的过程中出任何错，一律 `None`（**绝不从 tick 路径抛出**）；
3. `DeviceStatusSubscriptions`：每会话「上一次发出去的状态」，用来做
   变化去重。

## 订阅表就是会话表

没有独立的订阅注册表。一条会话存在 ⇒ 它的设备状态订阅就存在；会话关闭
（拆设备、换网络、玩家离开、TTL 过期、停机）走的是 `TerminalSyncHub` 既有
的 `_forget` 单一漏斗，本模块只在那一处 `drop()`。这样「打开时读快照、
持续更新、关闭时清理订阅」三条都成为**结构性**的，不需要另建一套生命周期
（另建的那套一定会漏掉某条关闭路径，症状是往一个已经没人的会话发包）。
"""

from ...common.identity import DeviceRef
from ...common.protocol import DeviceStatusMessage

#: 原因清单**不截断**。诊断清单「不截断」是仓库既定口径（见 03/06 交付
#: 说明：具体原因看 `blockers()`，是清单不是摘要），界面拿它做两行说明。
#: 因此这里没有 `max_reasons` 之类的策略字段——加一个就是凭空发明一条
#: 未定规则，而 `DeviceNetworkStatus.reasons` 本身已经是有限且有序的。


def build_status_message(status):
    # type: (object) -> DeviceStatusMessage
    """纯函数：`DeviceNetworkStatus` → 线上消息。

    走 `status.to_dict()` 而不是逐个读属性：序列化口径由领域层**自己**
    持有，这里只做改名（`version` → `status_version`，见协议头注那条
    「两个版本必须各有其名」）。领域层加字段时这里不会静默漏掉——
    `DeviceStatusMessage` 的字段检查会当场把不一致报出来。
    """
    source = status.to_dict()
    return DeviceStatusMessage(
        source["device_id"],
        source["generation"],
        source["online"],
        source["state_text"],
        source["validity"],
        source["run_state"],
        source["run_state_text"],
        source["version"],
        reasons=source["reasons"],
        notes=source["notes"],
        master_instance_id=source["master_instance_id"],
        bandwidth_ok=source["bandwidth_ok"],
        power_ok=source["power_ok"],
        bandwidth_domain_id=source["bandwidth_domain_id"],
        ui_applicable=source["ui_applicable"],
    )


def read_status(device_ref, status_of):
    # type: (DeviceRef, object) -> object
    """守卫式读取。读不到就是 `None`，**不是**「离线」。

    「读不到」与「确认离线」是两件事：前者这一帧不发包（客户端保留上一
    份），后者发一份 `online=False`。把它们混起来，会在读取路径出问题的
    时候让界面显示一个**方向错误**的结论——而 `DeviceNetworkStatus` 本身
    已经用 `validity="pending"` 表达了「离线但还没确认」，那才是该显示的
    那一种。

    四道门：

    - `status_of` 抛异常 ⇒ `None`（同 `_publish_inventory` 的
      `except Exception: return`：一次读取失败不该让整帧作废）；
    - 返回 `None` ⇒ `None`（设备不存在、网络还没建起来）；
    - `ui_applicable` 为假 ⇒ `None`（控制器本身不套用这套状态栏，
      blocks.md 第 5 条）；
    - 代次/位置对不上 ⇒ `None`（拿会话的设备身份复核一遍，别让旧实例的
      状态喂进新界面——`RuntimeManager.runtime_for` 只按位置路由，代次
      要在这里补上）。
    """
    try:
        status = status_of(device_ref)
    except Exception:
        return None
    if status is None:
        return None
    if not getattr(status, "ui_applicable", False):
        return None
    # 「实例已不存在」那一份结论里 `device_id` 是空的（`compose_device_status`
    # 的 DEVICE_GONE 分支没有设备定义可报）。身份缺失的消息线上过不了校验，
    # 而那种设备本来就该由 `_drop_stale_devices` 关掉会话——这里跳过即可。
    if not getattr(status, "device_id", None):
        return None
    node_key = getattr(status, "node_key", None)
    if node_key is None:
        return None
    reported = DeviceRef(node_key, status.generation)
    if not device_ref.matches(reported):
        return None
    return status


class DeviceStatusSubscriptions(object):
    """每会话「上一次发出去的状态」，用来做变化去重。

    与 `TerminalSyncHub._inventory_sent` 同一手法：**值相同就不重发**。
    设备状态每帧判一次（读是纯的、便宜），但绝大多数帧结论不变，没有这层
    去重就是每帧一个包。

    `stats` 只加不减（除 `dropped`），与同步中枢其余计数器一致：它们给
    `diagnostics()` 看，不是滑窗。
    """

    __slots__ = ("_sent", "stats")

    def __init__(self):
        self._sent = {}
        self.stats = {"published": 0, "skipped": 0, "dropped": 0}

    def unchanged(self, session_id, payload):
        # type: (str, dict) -> bool
        """这一份与上一次发出去的**逐字段相同**，不必重发。"""
        if self._sent.get(session_id) == payload:
            self.stats["skipped"] += 1
            return True
        return False

    def remember(self, session_id, payload):
        # type: (str, dict) -> None
        self._sent[session_id] = payload
        self.stats["published"] += 1

    def drop(self, session_id):
        # type: (str) -> None
        """会话没了就把它的记账丢掉。**会话 ID 不复用**（进程级序号），
        所以这里漏掉一条只会多占一点内存，不会串台；但漏掉会让
        `count()` 说谎，进而让「取消订阅」看起来没生效。"""
        if self._sent.pop(session_id, None) is not None:
            self.stats["dropped"] += 1

    def count(self):
        # type: () -> int
        return len(self._sent)

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report["sessions"] = len(self._sent)
        return report
