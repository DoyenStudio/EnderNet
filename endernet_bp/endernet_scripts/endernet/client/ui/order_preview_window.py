# coding=utf-8
"""下单预览窗口的**控制器**：收发、生命周期、与网络客户端的接线。

显示状态在 `widgets/order_preview.py`（纯模型），类体绑定在
`widgets/order_preview_bindings.py`。本模块只管三件事：

1. **发请求**：`request(target, count)` → 一条 `preview_request`；
   `cancel()` → 一条 `preview_cancel`；`submit()` → 一条 `order_submit`；
2. **收结果**：订阅 `NetworkClient.on_message`，把 `preview_state` 与
   `preview_chunk` 喂给模型，**按会话与预览号丢弃陈旧包**；
3. **收尾**：`close()` 解订阅；宿主屏销毁时调用它。

## 为什么不用 `AddElement` 现建一个窗口

窗口是**宿主面板 JSON 里的一个实例**（`#visible` 控制显隐），与
`device_status_window_basic` 同一个做法。`AddElement` + `DynaBinding` 那条路
在这台引擎上没跑通过（见 `widgets/order_preview_bindings.py` 的说明）。
因此本类**不持有任何控件引用**，只维护模型——和 `TerminalListView` 一样。

## 陈旧包靠三层丢掉

会话号（客户端 `receive` 已经挡过一道）、`preview_id`、`generation`。
少任何一层，一次「关掉再开、换个目标」都可能让上一轮的原料行落进新窗口，
而界面上那看起来就是「这次要这么多料」。
"""

from .widgets import order_preview
from ...common.protocol import MSG_COMMAND_RESULT, MSG_ERROR, STATUS_OK

#: 数量窗的标题。与 `number_set_window.DEFAULT_TITLE`（「设置数量」）不同：
#: 这一处的数量是**订单目标数量**，玩家按「设置数量」理解会以为是在改
#: 某种默认值。
QUANTITY_TITLE = u"设置订单数量"

#: 请求/取消/提交三条动作名（线上形状）。
ACTION_REQUEST = "preview_request"
ACTION_CANCEL = "preview_cancel"
ACTION_SUBMIT = "order_submit"


class OrderPreviewWindow(object):
    """一份预览的生命周期。**每个终端面板一个**，与列表视图同寿命。"""

    def __init__(self, screen, client, model=None, clock=None):
        # type: (object, object, object, object) -> None
        self.screen = screen
        self.client = client
        self.model = model if model is not None else order_preview.OrderPreviewModel()
        self.closed = False
        #: 最后一条在途请求的 `request_id`。**只记一条**：这个窗口同时
        #: 只有一份预览，而重开一次就是换一份。
        self.request_id = ""
        self._submitting = False
        self._accept_preview = False
        self.stats = {"requests": 0, "cancels": 0, "submits": 0, "refused": 0}

    # -- 生命周期 ------------------------------------------------------

    def listen(self):
        # type: () -> None
        """挂上收包回调。宿主在 `OnCreate` 里调一次。"""
        self.client.on_message(self._make_handler())

    def _make_handler(self):
        # type: () -> object
        """只更新**本窗口这一个模型**，并拒绝失效会话与旧屏的迟到响应。

        与 `device_status_bindings._make_status_handler` 同一条取向：
        捕获模型身份，防止同一屏关掉又开后，旧回调去动新模型。
        """
        client = self.client
        model = self.model

        def on_preview_message(envelope, message):
            # type: (object, object) -> None
            if message is None:
                return
            kind = message.kind
            if kind not in (order_preview.MSG_PREVIEW_STATE,
                            order_preview.MSG_PREVIEW_CHUNK,
                            MSG_COMMAND_RESULT, MSG_ERROR):
                return
            lost_session_error = (kind == MSG_ERROR and not envelope.session_id
                                  and bool(self.request_id)
                                  and message.request_id == self.request_id)
            if not lost_session_error and (
                    not client.has_session() or envelope.session_id != client.session_id):
                return
            if self.closed or self.model is not model:
                return
            if kind in (MSG_COMMAND_RESULT, MSG_ERROR):
                if not self.request_id or message.request_id != self.request_id:
                    return
                if kind == MSG_ERROR:
                    self.request_id = ""
                    self._submitting = False
                    self._accept_preview = False
                    model.fail(message.detail or order_preview.DETAIL_FAILED)
                    return
                result = message.result
                if result.get("status") == STATUS_OK:
                    if self._submitting:
                        model.close()
                else:
                    details = [reason.get("detail", "")
                               for reason in result.get("reasons", ())]
                    model.fail(u"\n".join(detail for detail in details if detail)
                               or order_preview.DETAIL_FAILED)
                    self._accept_preview = False
                self.request_id = ""
                self._submitting = False
                return
            if not self._accept_preview:
                return
            if kind == order_preview.MSG_PREVIEW_STATE:
                model.apply_state(message)
            else:
                model.apply_chunk(message)

        return on_preview_message

    def close(self):
        # type: () -> None
        """宿主屏销毁/失活时收尾。**可重复调用。**

        **不发 `preview_cancel`**：屏都没了再发一条命令也无人接收，而服务端
        那边有会话失效与 TTL 两条路回收（`PreviewService.forget_session`
        在会话关闭时就会清）。这里只把本地状态放下。
        """
        if self.closed:
            return
        self.closed = True
        self.model.close()
        self.client = None

    # -- 打开与取消 ----------------------------------------------------

    def request(self, target, count):
        # type: (object, int) -> bool
        """发一条预览请求。**先开窗口再发**（契约第 3 节：先显示 loading）。

        返回「发出去了没有」。没会话时返回 `False` 并**什么都不做**：
        开一个会一直转圈的窗口比不开更糟。
        """
        if self.closed or self.client is None or not self.client.has_session():
            self.stats["refused"] += 1
            return False
        self.model.open(target, count)
        self._submitting = False
        self._accept_preview = True
        self.request_id = self.client.send_command(
            ACTION_REQUEST,
            expected_index_version=self.client.index_version,
            key=target,
            count=count,
        )
        self.stats["requests"] += 1
        return True

    def cancel(self):
        # type: () -> bool
        """取消预览。**只关预览，不碰任何真实订单**（契约第 5 节）。"""
        if self.closed or self.client is None:
            return False
        preview_id = self.model.preview_id
        self.request_id = ""
        self._submitting = False
        self._accept_preview = False
        self.model.close()
        if not preview_id or not self.client.has_session():
            return False
        self.client.send_command(
            ACTION_CANCEL,
            expected_index_version=self.client.index_version,
            preview=preview_id,
        )
        self.stats["cancels"] += 1
        return True

    def dismiss(self):
        # type: () -> None
        """右上角关闭：**与「取消」同义**（都只收起预览）。

        分成两个方法是因为两条绑定是两个按钮，而它们将来未必一直同义——
        把它们合成一个入口会让「哪一条该发请求」变成按钮绑定里的事。
        """
        self.cancel()

    # -- 提交 ----------------------------------------------------------

    def submit(self):
        # type: (object) -> bool
        """发正式订单提交。**不可提交时一条都不发。**

        判据取模型那句 `can_submit()`（服务端的 `submit_ok` + 收齐分片），
        不在这里另算一遍——两处各算一次的代价是它们迟早分家，而分家的
        表现是「按钮看起来能点，点了没反应」。
        """
        if self.closed or self.client is None:
            return False
        if not self.model.can_submit():
            self.stats["refused"] += 1
            return False
        if not self.client.has_session():
            self.stats["refused"] += 1
            return False
        target = self.model.target
        if target is None:
            self.stats["refused"] += 1
            return False
        self.request_id = self.client.send_command(
            ACTION_SUBMIT,
            expected_index_version=self.client.index_version,
            key=target,
            count=self.model.count,
            preview=self.model.preview_id,
        )
        self.stats["submits"] += 1
        self._submitting = True
        self._accept_preview = False
        # **提交在途时禁用按钮**（契约第 5 节）：把它按下去之后先落到
        # loading，按当前提交请求的回执关闭窗口或展示拒绝原因。
        self.model.stage = order_preview.STAGE_LOADING
        self.model.detail = u"正在提交订单…"
        return True

    # -- 诊断 ----------------------------------------------------------

    def diagnostics(self):
        # type: () -> dict
        return {
            "closed": self.closed,
            "request_id": self.request_id,
            "stats": dict(self.stats),
            "model": self.model.diagnostics(),
        }

    def __repr__(self):
        return "OrderPreviewWindow(%s)" % (self.model.stage,)
