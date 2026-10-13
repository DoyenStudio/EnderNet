# coding=utf-8
"""接口配置的上行编码与订阅记账（11-07）。

对应 `11. 终端与客户端同步.md` 的 11-07 与「接口配置界面补充」一节。
本模块**不 import 引擎**（`server/sync/` 是域目录，由
`test_import_boundaries.py` 静态强制），也不直接 import `server/devices/`——
视图由注入的 `port_of` 拿（`ServerBootstrap._sync_port_of` →
`PortStore.get_by_ref` → `io.config_view`），于是整条上行路径可以离线驱动。

## 为什么不叫 `devices.py`

11 文档的「新增交付」那一行点名了 `server/sync/devices.py`（驱动器槽位／
配置同步）。**那个名字留给驱动器那一半**：驱动器的介质管理与 Lore 刷新
今天仍被 UI 交接阻塞（`plans/ui/storage_driver.md` 还是空模板），占掉
`devices.py` 会让后来的人以为驱动器同步已经在这里了。本模块只管**接口**
（端口的过滤与缓存开关），因此按用途命名——`device_status.py` 立的就是
这个先例。

## 三件套与 `device_status.py` 逐件同形

1. `build_config_message()`：把视图编成线上消息（纯函数）；
2. `read_config()`：守卫式读取——读不到、不是接口设备、代次对不上、
   或读的过程中出任何错，一律 `None`（**绝不从 tick 路径抛出**）；
3. `PortConfigSubscriptions`：每会话「上一次发出去的配置」，用来做变化去重。

## 订阅表就是会话表

没有独立的订阅注册表。一条会话存在 ⇒ 它的配置订阅就存在；会话关闭
（拆设备、换网络、玩家离开、TTL 过期、停机）走的是 `TerminalSyncHub` 既有的
`_forget` 单一漏斗，本模块只在那一处 `drop()`。与 `device_status.py` 同一条
理由：另建一套生命周期一定会漏掉某条关闭路径，而症状是往一个已经没人的
会话发包。
"""

from ...common.identity import DeviceRef
from ...common.protocol import PortConfigMessage


def build_config_message(view):
    # type: (dict) -> PortConfigMessage
    """纯函数：`config_view()` 的视图 → 线上消息。

    逐字段转交而不是原样塞进去：改名的位置只有这一处，而线上字段的清单由
    `PortConfigMessage.FIELDS` 持有——视图那边加了字段而这里忘了转，构造器
    的 DEBUG 校验会当场把不一致报出来（缺项会被 `_require_fields` 拒）。
    """
    return PortConfigMessage(
        view["device_id"],
        view["generation"],
        view["role"],
        view["config"],
        view["filter_limit"],
        filter_modes=view["filter_modes"],
        blocker=view["blocker"],
        buffer_summary=view["buffer_summary"],
    )


def read_config(device_ref, port_of):
    # type: (DeviceRef, object) -> dict
    """守卫式读取。读不到就是 `None`，**不是**「发一份空配置」。

    与 `read_status` 同一条纪律：「读不到」与「确认没有配置」是两件事。
    前者这一帧不发包（客户端保留上一份），后者才是「这台设备刚放下、
    还没配过」。混起来的表现是：服务端某次读表失败，玩家的界面**自己**
    变回空配置，而他并没有改过任何东西。

    四道门：

    - `port_of` 抛异常 ⇒ `None`（一次读取失败不该让整帧作废）；
    - 返回 `None` ⇒ `None`（**这台设备不是接口**，或者它还没进 store——
      非接口设备本来就该收不到这条消息）；
    - 视图不是字典 / 身份字段缺失 ⇒ `None`（回调接错时的兜底）；
    - 代次对不上 ⇒ `None`（拿会话的设备身份复核一遍，别让旧实例的配置
      喂进新界面）。

    **位置不用再核**：`port_of` 是 `PortStore.get_by_ref(device_ref)`，
    它的键就是 `(位置, 代次)`，查得到即位置已经对上了。因此这里只补代次
    这一条显式比对，好让「拿别台的配置糊到自己界面上」在两条路径上都
    拦得住。
    """
    try:
        view = port_of(device_ref)
    except Exception:
        return None
    if not isinstance(view, dict):
        return None
    if not view.get("device_id"):
        return None
    reported = DeviceRef(device_ref.node_key, view["generation"])
    if not device_ref.matches(reported):
        return None
    return view


class PortConfigSubscriptions(object):
    """每会话「上一次发出去的配置」，用来做变化去重。

    与 `DeviceStatusSubscriptions` 逐行同形（值相同就不重发）。配置比状态
    更静态——绝大多数帧一个字都不变，没有这层去重就是每帧一个包。
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
        """会话没了就把它的记账丢掉。会话 ID 不复用（进程级序号），因此漏掉
        一条只会多占一点内存；但漏掉会让 `count()` 说谎，进而让「取消订阅」
        看起来没生效。"""
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
