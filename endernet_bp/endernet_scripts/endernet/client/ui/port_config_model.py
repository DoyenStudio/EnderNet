# coding=utf-8
"""接口配置的客户端模型（11-07）：权威值、乐观值与在途意图。

**纯模型，不 import 引擎**，因此可以在离线测试里直接构造与断言。

## 它持有三份东西，以及为什么不能少一份

- `applied`：服务端推来的**权威**配置。客户端从不自己算它，收到即整份替换。
- `desired`：界面上**该显示什么**。玩家刚拨了开关、刚改了名单时它先动，
  等回执或推送把它拉回权威值。
- `pending`：还没回执的意图，按 `request_id` 记账。

只有 `applied` 的话，玩家拨一下开关要等一个来回才有反馈；只有 `desired`
的话，被拒绝的提交会把界面永久停在错的状态上（那正是 11 反复记的
「界面显示一次没发生的存取」）。两份一起，才有「先动、错了退回」。

## 过期防护靠什么

**不靠配置版本**——`PortState` 上没有那个字段，而另外两个版本词汇
（交易版本、存档结构版本）各有其名，挪用它们会让「按哪个版本判断过期」
变成猜（见 `common/protocol.PortConfigMessage` 与交付说明）。这里靠三件
已经存在的事：

1. **服务端推送即权威**：收到就覆盖，客户端没有第二条真相源；
2. **回执**：成功时以回执载荷里的配置为权威，失败时退回 `applied`；
3. **在途闸**（`busy`）：同一字段还有在途提交时不重复提交——它挡的是
   「拨一下开关发出两条命令」，而那两条的到达顺序不保证。

三者合起来挡不住的只有一件事：**两个玩家同时改同一台设备**——那种情况
按到达顺序最后写入生效。这是未实现项，见交付说明，不是这里漏了。
"""

from ...common.resource_key import ResourceKey

#: 空配置在界面上画什么。**不是**「没有配置」这个状态本身——那由摘要文案
#: 说，图标只是图标。
BARRIER_ITEM = "minecraft:barrier"

#: 多项目录的摘要口径（**替身，未定案**）。UI 交接第 8 节把「多项摘要策略」
#: 标着待确认，因此这里取最保守的一种：首项图标 + 一句「已设定 N 项」，
#: 不做轮播也不做堆叠。等 User 定案后改这一处即可。
MULTI_SUMMARY_FORMAT = "已设定 %d 项"

#: 过滤模式的图标 uv 与文字。**照抄 UI 交接第 5 节的表格**（那是对接口的
#: 明确定义，不是本文件发明的），两边都从这一处取，免得图与字分家。
MODE_VISUALS = {
    "blacklist": ([16, 0], "黑名单"),
    "whitelist": ([0, 0], "白名单"),
}
MODE_ICON_SIZE = (16, 16)


class PortConfigModel(object):
    """一台接口的配置在客户端的那一份。"""

    __slots__ = (
        "ref",
        "device_id",
        "role",
        "applied",
        "desired",
        "filter_limit",
        "filter_modes",
        "blocker",
        "buffer_summary",
        "notice",
        "pending",
        "stats",
    )

    def __init__(self, device_ref):
        #: 会话认领的那台设备。**代次就在它里面**，因此不需要另存一份。
        self.ref = device_ref
        self.device_id = None
        self.role = None
        self.applied = {}
        self.desired = {}
        #: 该角色的过滤条目上限与允许的模式。**由服务端下发**，界面不硬编码
        #: ——存货口那个上限尚未定案（`PortPolicy.intake_filter_limit`）。
        self.filter_limit = 0
        self.filter_modes = ()
        self.blocker = None
        self.buffer_summary = None
        #: 上一条失败的文案。**目前没有显示位置**（缺项，见交付说明），
        #: 因此它先落在 `diagnostics()` 里。
        self.notice = ""
        self.pending = {}
        self.stats = {"applied": 0, "stale": 0, "settled": 0, "rolled_back": 0}

    # -- 收权威值 ------------------------------------------------------

    def apply(self, message):
        # type: (object) -> bool
        """收一份权威配置，返回是否采纳。

        **旧代次一律丢掉**：拆掉又在原地放一台时，旧设备那一份不能喂进新
        界面（与 `DeviceStatusModel` 同一条守卫）。设备身份一旦记下也不再
        接受别的设备——一块屏只对着它认领的那一台。
        """
        if message is None:
            return False
        if self.ref is not None and message.generation != self.ref.generation:
            self.stats["stale"] += 1
            return False
        if self.device_id is not None and message.device_id != self.device_id:
            self.stats["stale"] += 1
            return False
        self.device_id = message.device_id
        self.role = message.role
        self.applied = dict(message.config)
        self.desired = dict(message.config)
        self.filter_limit = message.filter_limit
        self.filter_modes = tuple(message.filter_modes)
        self.blocker = dict(message.blocker) if message.blocker is not None else None
        self.buffer_summary = message.buffer_summary
        self.stats["applied"] += 1
        return True

    # -- 读 ------------------------------------------------------------

    def ready(self):
        # type: () -> bool
        """收到过权威配置没有。没收到时界面按「还不知道」处理，不按空配置。"""
        return bool(self.applied)

    def value(self, field, default=None):
        # type: (str, object) -> object
        return self.desired.get(field, self.applied.get(field, default))

    def cache_enabled(self):
        # type: () -> bool
        """取货口的缓存开关。默认 `True`——与 `PortConfig` 的出厂口径一致。"""
        return bool(self.value("cache_enabled", True))

    def filter_mode(self):
        # type: () -> str
        return self.value("filter_mode", "whitelist")

    def filter_entries(self):
        # type: () -> list
        """过滤名单的 `(名字, 辅助值)` 列表。

        线上那一项是**资源键字典的列表**，这里只取界面要的两项：画图标要用
        `name` + `aux`（`SetUiItem` 收的就是这两个，见
        `widgets/control_api.py`），改名单要用 `name`。
        """
        entries = self.value("filter", ()) or ()
        out = []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("name"):
                out.append((entry["name"], entry.get("aux") or 0))
        return out

    def filter_names(self):
        # type: () -> list
        """过滤名单里的物品 identifier 列表（给列表窗口用）。"""
        return [name for name, _aux in self.filter_entries()]

    def filter_summary(self):
        # type: () -> tuple
        """`(名字, 辅助值, 摘要文案)`。空配置画 barrier 并写「未设定物品」。

        **带 `aux`**：染色羊毛这类物品只靠名字画不出正确的图标，而
        `SetUiItem` 要的正是 `(name, aux)` 两个参数。
        """
        entries = self.filter_entries()
        if not entries:
            return (BARRIER_ITEM, 0, "未设定物品")
        name, aux = entries[0]
        if len(entries) == 1:
            return (name, aux, name)
        return (name, aux, MULTI_SUMMARY_FORMAT % (len(entries),))

    def mode_visual(self):
        # type: () -> tuple
        """`(uv, uv_size, 文案)`——过滤模式的图标与文字，两者永远一起变。"""
        uv, text = MODE_VISUALS.get(self.filter_mode(), MODE_VISUALS["whitelist"])
        return (list(uv), MODE_ICON_SIZE, text)

    def next_mode(self):
        # type: () -> str
        """下一个模式。只在服务端说该角色**允许**的那几个之间轮转。

        取货口只允许白名单（`ROLE_FILTER_MODES`），因此它的「切换」是原地
        不动——这正是服务端下发的 `filter_modes` 的用处：界面不自己判断
        哪个角色能用哪种模式。
        """
        modes = list(self.filter_modes)
        if not modes:
            return self.filter_mode()
        current = self.filter_mode()
        if current not in modes:
            return modes[0]
        return modes[(modes.index(current) + 1) % len(modes)]

    # -- 意图（线上形状，直接进 `send_command(config=...)`）-------------

    def intent_cache(self, enabled):
        # type: (bool) -> dict
        return {"cache_enabled": bool(enabled)}

    def intent_filter(self, names):
        # type: (list) -> dict
        """名单 → `{"filter": [资源键字典, ...]}`。

        **在客户端一次成型**：资源键的线上形状（`ResourceKey.to_dict()`）与
        `PortConfig.to_dict()` 里那一项逐字相同，因此服务端读进来不需要任何
        转换。塞字符串或 `ResourceKey` 对象进去都会在边界上炸。
        """
        return {
            "filter": [ResourceKey.item(name).to_dict() for name in names]
        }

    def intent_mode(self, mode):
        # type: (str) -> dict
        return {"filter_mode": mode}

    # -- 在途与回执 ----------------------------------------------------

    def expect(self, request_id, intent):
        # type: (str, dict) -> None
        """登记一次在途提交，并把乐观值先写进 `desired`。"""
        self.pending[request_id] = dict(intent)
        merged = dict(self.desired)
        for field, value in intent.items():
            merged[field] = value
        self.desired = merged

    def busy(self, field):
        # type: (str) -> bool
        """这个字段还有在途提交吗。有就不该再发一条——两条的到达顺序不保证。"""
        for intent in self.pending.values():
            if field in intent:
                return True
        return False

    def pending_count(self):
        # type: () -> int
        # 只剩「界面又发了一次同一个 request_id」这一种可能，但计数要让
        # 上机判据落得下来（`pending_commands()` 回到 0）。
        return len(self.pending)

    def settle(self, request_id, config=None, detail=""):
        # type: (str, dict, str) -> bool
        """回执到了，返回它是不是我们发出去的那一条。

        成功（`config` 不是 `None`）时**以回执载荷为权威**——服务端可能是
        合并之后的结果，客户端那份乐观值只是它的一部分。失败时退回
        `applied`，并把原因记进 `notice`（今天没有显示位置，见模块
        docstring 的缺项说明）。
        """
        intent = self.pending.pop(request_id, None)
        if intent is None:
            return False
        if config is not None:
            self.applied = dict(config)
            self.stats["settled"] += 1
        else:
            self.stats["rolled_back"] += 1
            self.notice = detail or "配置未被接受"
        self.desired = dict(self.applied)
        return True

    # -- 诊断 ----------------------------------------------------------

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report.update({
            "device_id": self.device_id,
            "role": self.role,
            "ready": self.ready(),
            "pending": self.pending_count(),
            "notice": self.notice,
            "filter_count": len(self.filter_names()),
            "cache_enabled": self.cache_enabled(),
        })
        return report
