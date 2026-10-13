# coding=utf-8
"""下单预览窗口的显示状态（**纯模型**，不认识引擎、不发命令）。

规则见 `plans/technical_notes/terminal_pattern_order_preview.md` 第 3、4 节。
本模块只回答「现在窗口该显示什么」，交互与收发在
`client/ui/order_preview_window.py`。

## 两条不许含糊的

1. **收齐分片才算拿到结果。** 状态头（`preview_state`）到了只说明服务端开始
   回答了，它本身**不是完成信号**——契约第 3 节点名了「收到第一条进度或
   部分行就提前结束 loading」这个坑。因此模型收着一份待拼的行表，只有
   `packet_count` 片全到齐才**原子换掉**当前行。
2. **缺料是整单的结论，不是逐行数出来的。** 行上的 `missing` 与服务端禁不禁
   下单各说各的：服务端给 `submit_ok=False` 时按钮就是禁用的，客户端不去
   「看看有没有哪一行红了」再自己判一次——那是拿表现当判据。
"""

#: 窗口当前处在哪一段。四段而不是三态：`blocked` 与 `loading` 在界面上长得
#: 完全不同（一个在转圈、一个是「算完了但下不了单」），混成「不可提交」
#: 会让玩家一直等一个已经结束的计算。
STAGE_IDLE = "idle"
STAGE_LOADING = "loading"
STAGE_READY = "ready"
STAGE_BLOCKED = "blocked"

#: 原料行背景色，RGB 归一化到 0..1。
COLOR_MISSING = (1.0, 200.0 / 255.0, 200.0 / 255.0)
COLOR_NORMAL = (200.0 / 255.0, 1.0, 200.0 / 255.0)
COLOR_CRAFTING = (200.0 / 255.0, 200.0 / 255.0, 1.0)

#: 「还在算」的判据。**它是规划器自己的 `PlanStatus.RUNNING`**，
#: 而 `client/` 不 import 领域层（分层由 `test_import_boundaries.py` 强制），
#: 因此在这里如实抄一份字面量，并靠服务端与客户端两侧的用例钉住它。
RUNNING_STATUS = "running"

#: 本模块认识的两条 S2C 种类。**在这里写一次**，窗口控制器与绑定都从这里
#: 取——写两遍的代价是其中一遍漏改时，收包回调静默地什么都不做。
MSG_PREVIEW_STATE = "preview_state"
MSG_PREVIEW_CHUNK = "preview_chunk"

#: 状态说明的兜底文案。
DETAIL_IDLE = ""
DETAIL_LOADING = u"正在计算订单所需的原料"
DETAIL_FAILED = u"这次预览没能算出一份完整的计划，请重新预览"


# -- 绑定名 ---------------------------------------------------------------
#
# 名字与 JSON 逐字相同，而且要在**离线**（不装引擎桩）就能对着 JSON 对账；
# `order_preview_bindings.py` 那边有 `tooldelta` 的导入，装不了桩就进不来，
# 因此这些常量住在这里，那一份 import 它们。

#: 绑定名前缀。**与 JSON 里逐字相同**：名字对不上时引擎只是查不到绑定、
#: 安静地给假值，界面看起来「什么都没生效」而一个错都不报。
BINDING_PREFIX = "#EnderNetPreview."

VISIBLE_BINDING = BINDING_PREFIX + "visible"
LOADING_BINDING = BINDING_PREFIX + "loading"
SUBMIT_BINDING = BINDING_PREFIX + "submit_enabled"
DETAIL_BINDING = BINDING_PREFIX + "detail"
COUNT_BINDING = BINDING_PREFIX + "count"
CLOSE_BINDING = BINDING_PREFIX + "close"
CANCEL_BINDING = BINDING_PREFIX + "cancel"
CONFIRM_BINDING = BINDING_PREFIX + "confirm"

#: 原料行的集合名。与 `endernet_windows.order_preview_src_items` 的
#: `collection_name` 逐字相同。
COLLECTION = "order_preview_src_items"

ITEM_AUX_BINDING = BINDING_PREFIX + "row_id_aux"
ROW_NAME_BINDING = BINDING_PREFIX + "row_name"
ROW_COUNT_BINDING = BINDING_PREFIX + "row_count"
ROW_COLOR_BINDING = BINDING_PREFIX + "row_color"

class OrderPreviewModel(object):
    """窗口的显示状态与分片拼装。

    **行表是「收齐才换」的**：`rows` 只在完整收齐一份结果的那一刻被替换，
    因此界面不会出现半份原料表——那看起来与「算完了，就这几样」一模一样。
    """

    def __init__(self):
        self.visible = False
        self.stage = STAGE_IDLE
        self.detail = DETAIL_IDLE
        #: 服务端给的预览号与代次。陈旧的包靠它们被丢掉。
        self.preview_id = ""
        self.generation = 0
        #: 目标（显示用）。`count` 是玩家选的订单数量。
        self.target = None
        self.count = 0
        #: 收齐之后的原料行。
        self.rows = ()
        #: 有没有缺料（服务端的整单结论，不是逐行数出来的）。
        self.missing = False
        #: 服务端说的「能不能下单」。**只是建议**：真正的准入在服务端，
        #: 客户端用它来禁用按钮与提示。
        self.submit_ok = False
        self.row_count = 0
        self.packet_count = 0
        #: 待拼装的分片 `{packet_index: rows}` 与它们属于哪一代。
        self._pending = {}
        self._pending_key = None
        self.stats = {"states": 0, "chunks": 0, "stale_chunks": 0, "completed": 0}

    # -- 生命周期 ------------------------------------------------------

    def open(self, target=None, count=0):
        # type: (object, int) -> None
        """打开窗口并进入 loading。**先显示 loading 再发请求**（契约第 3 节：
        「打开预览立即保持 loading 可见」），因此这个动作不等服务端。"""
        self.visible = True
        self.stage = STAGE_LOADING
        self.detail = DETAIL_LOADING
        self.preview_id = ""
        self.generation = 0
        self.target = target
        self.count = int(count or 0)
        self.rows = ()
        self.missing = False
        self.submit_ok = False
        self.row_count = 0
        self.packet_count = 0
        self._pending = {}
        self._pending_key = None

    def close(self):
        # type: () -> None
        """收起窗口。**保留 `rows`**：重开时先看到上一次的结果，比看到
        一片空白更接近「刚刚发生过什么」；而 `open()` 会把它清掉。"""
        self.visible = False
        self.stage = STAGE_IDLE
        self._pending = {}
        self._pending_key = None

    def is_open(self):
        # type: () -> bool
        return self.visible

    # -- 收包 ----------------------------------------------------------

    def apply_state(self, message):
        # type: (object) -> bool
        """收一条状态头。返回「这一条是不是关于当前这份预览的」。

        判据是预览号：**没有预览号的那一条是「你的请求被拒了」**，
        它是服务端对 `preview_request` 的答复，此时窗口停在 loading 没有
        意义——要把它变成一句原因（见 `fail`）。
        """
        if not self.visible:
            return False
        preview_id = message.preview_id
        if not preview_id:
            self.fail(message.detail or DETAIL_FAILED)
            return True
        if self.preview_id and preview_id != self.preview_id:
            # 换了一份预览（玩家改了目标）：旧的连同它收了一半的分片一起作废。
            self.preview_id = preview_id
            self.generation = 0
            self._pending = {}
            self._pending_key = None
        self.preview_id = preview_id
        if message.generation < self.generation:
            return False
        if message.generation > self.generation:
            # 新一轮：上一轮收到的分片全部作废（契约第 3 节的「查询代次」）。
            self.generation = message.generation
            self._pending = {}
            self._pending_key = None
        self.stats["states"] += 1
        self.detail = message.detail or ""
        self.count = message.count
        self.packet_count = message.packet_count
        self.row_count = message.row_count
        self.missing = message.missing
        self.submit_ok = message.submit_ok
        if message.status == RUNNING_STATUS:
            # **还在算**：保持 loading，且**不要**因为收到「有零行」就以为
            # 算完了——那正是提前结束 loading 的另一条路。
            self.stage = STAGE_LOADING
            self.detail = message.detail or DETAIL_LOADING
            return True
        # 算完了（含缺料这份「算完了但下不了单」的结论）。
        if self.packet_count <= 0:
            self.rows = ()
            self._settle(message)
        return True

    def apply_chunk(self, message):
        # type: (object) -> bool
        """收一批原料行。**收齐才换 `rows`**，返回「这一批用上了没有」。"""
        if not self.visible or not self.preview_id:
            return False
        if message.preview_id != self.preview_id:
            self.stats["stale_chunks"] += 1
            return False
        if message.generation != self.generation:
            self.stats["stale_chunks"] += 1
            return False
        key = message.packet_count
        if self._pending_key != key:
            # 片数变了（服务端重算了）：已收的作废，从头拼。
            self._pending_key = key
            self._pending = {}
        if message.packet_index in self._pending:
            return False
        self._pending[message.packet_index] = message.rows
        self.stats["chunks"] += 1
        if len(self._pending) < key:
            return True
        rows = []
        for index in range(key):
            rows.extend(self._pending[index])
        self.rows = tuple(rows)
        self._pending = {}
        self._settle(None)
        return True

    def fail(self, detail):
        # type: (str) -> None
        """把窗口停在一句原因上。**不假装「没有缺料」**（契约第 3 节）。"""
        self.stage = STAGE_BLOCKED
        self.detail = detail or DETAIL_FAILED
        self.submit_ok = False
        self.rows = ()
        self._pending = {}
        self._pending_key = None

    def _settle(self, message):
        # type: (object) -> None
        """一份完整结果到手：定下这一段是什么。"""
        self.stats["completed"] += 1
        # **可提交的判据是服务端那句话**，不是「有没有红行」。
        if self.submit_ok:
            self.stage = STAGE_READY
        else:
            self.stage = STAGE_BLOCKED
        if message is not None:
            self.detail = message.detail or self.detail

    # -- 给绑定的只读口 ------------------------------------------------

    def loading(self):
        # type: () -> bool
        return self.visible and self.stage == STAGE_LOADING

    def can_submit(self):
        # type: () -> bool
        """按钮该不该可用。**服务端说了算**；客户端只是照着画。"""
        return (self.visible and self.stage == STAGE_READY
                and bool(self.preview_id) and self.submit_ok)

    def grid_count(self):
        # type: () -> int
        """`#maximum_grid_items`：窗口关着时报 0（否则会画出幽灵格子）。"""
        if not self.visible:
            return 0
        return len(self.rows)

    def row_of(self, index):
        # type: (object) -> object
        if isinstance(index, bool) or not isinstance(index, int):
            return None
        if index < 0 or index >= len(self.rows):
            return None
        return self.rows[index]

    def count_text(self, index):
        # type: (object) -> str
        """数量来自服务端账本；缺口使用包含子树影响的 shortfall。"""
        row = self.row_of(index)
        if row is None:
            return ""
        demand = row["demand"]
        if row["missing"]:
            shortfall = row.get("shortfall", max(0, demand - row["allocated"]))
            return u"缺失 %d/%d" % (shortfall, demand)
        if row["allocated"] >= demand:
            return u"库存充足"
        return u"需要制作 %d/%d" % (max(0, demand - row["allocated"]), demand)

    def row_color(self, index):
        # type: (object) -> tuple
        row = self.row_of(index)
        if row is None:
            return (1.0, 1.0, 1.0)
        if row["missing"]:
            return COLOR_MISSING
        if row["allocated"] >= row["demand"]:
            return COLOR_NORMAL
        return COLOR_CRAFTING

    def name_text(self, index):
        # type: (object) -> str
        row = self.row_of(index)
        return "" if row is None else row["name"]

    def row_missing(self, index):
        # type: (object) -> bool
        """这一行缺不缺料。

        **不叫 `missing`**：那个名字已经是「整单缺不缺」那个布尔属性，
        同名的方法会被那个属性遮住（`self.missing(0)` 会变成「拿布尔当
        函数调」），而报出来的是一句与原料毫无关系的 `TypeError`。
        """
        row = self.row_of(index)
        return bool(row is not None and row["missing"])

    def diagnostics(self):
        # type: () -> dict
        return {
            "visible": self.visible,
            "stage": self.stage,
            "preview_id": self.preview_id,
            "generation": self.generation,
            "rows": len(self.rows),
            "submit_ok": self.submit_ok,
            "stats": dict(self.stats),
        }
