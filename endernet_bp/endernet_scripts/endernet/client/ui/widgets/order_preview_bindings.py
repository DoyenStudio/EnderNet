# coding=utf-8
"""下单预览窗口的**类体绑定**（11 增量）。

与 `device_status_bindings.py` 同一个形状、同两条纪律：

1. **必须写在类体里。** 运行时注册（`DynaBinding`）在这台引擎上无效——
   引擎在渲染期按回调自己的名字在 `_Base` 上 `getattr`，运行时注册的包装
   函数名字不在那里（2026-09-30 真机撞出来的）。
2. **每个回调都要容忍 `self.preview` 还没建起来**，并给**安全值**。

安全值的方向：窗口是「没有结论就不显示」——`#visible` 回假、行数回 0，
绝不给一个看起来像「算完了、没有缺料」的中间态。

## 窗口是**实例化在宿主界面里**的，不是新开的界面

`order_preview@endernet_windows.order_preview_window` 与
`device_status_popup` 一样，是宿主面板 JSON 里的一个控件实例，靠 `#visible`
控制显隐。**不走 `AddElement` + `DynaBinding`**：那条路在本引擎上没跑通过
（见第 1 条），而 11-13 已经用「JSON 实例化 + 类体绑定」把详情弹窗验到真机上了。
"""

from .....tooldelta.ui import Binder

from .. import stack_size
from . import order_preview
from .order_preview import (
    BINDING_PREFIX,
    CANCEL_BINDING,
    CLOSE_BINDING,
    COLLECTION,
    CONFIRM_BINDING,
    COUNT_BINDING,
    DETAIL_BINDING,
    ITEM_AUX_BINDING,
    LOADING_BINDING,
    ROW_COUNT_BINDING,
    ROW_MISSING_BINDING,
    ROW_NAME_BINDING,
    SUBMIT_BINDING,
    VISIBLE_BINDING,
)

#: 颜色绑定的标志。与 `device_status_bindings.COLOR_BINDING_FLAG` 同一条
#: 理由：`Binder` 是引擎在运行期给的类，离线拿不到它的属性表；取不到就
#: **不注册这一条**，行颜色退回模板里的静态值，界面的其余部分照常工作。
COLOR_BINDING_FLAG = getattr(Binder, "BF_BindColor", None)


def bind_color(func):
    # type: (object) -> object
    """给行背景注册颜色绑定；引擎没有这个标志时原样返回（即不注册）。"""
    if COLOR_BINDING_FLAG is None:
        return func
    return Binder.binding_collection(
        COLOR_BINDING_FLAG, COLLECTION, ROW_MISSING_BINDING
    )(func)


class OrderPreviewBindingsMixin(object):
    """给一块终端屏接上下单预览窗口。混入者只需提供 `self.preview`。

    `preview` 的默认值必须是**类属性**：绑定比实例活得久（见模块 docstring）。
    宿主在 `OnCreate` 里把它换成 `OrderPreviewWindow`，收尾时置回 `None`。
    """

    #: `OrderPreviewWindow` / `OrderPreviewModel` / `None`。
    preview = None

    def _preview_model(self):
        # type: () -> object
        """当前该拿哪个模型回答绑定。窗口自己也是一种「模型」。"""
        preview = self.preview
        if preview is None:
            return None
        return getattr(preview, "model", preview)

    @Binder.binding(Binder.BF_BindBool, VISIBLE_BINDING)
    def order_preview_visible(self):
        # type: () -> bool
        model = self._preview_model()
        return bool(model is not None and model.visible)

    @Binder.binding(Binder.BF_BindBool, LOADING_BINDING)
    def order_preview_loading(self):
        # type: () -> bool
        model = self._preview_model()
        return bool(model is not None and model.loading())

    @Binder.binding(Binder.BF_BindBool, SUBMIT_BINDING)
    def order_preview_submit_enabled(self):
        # type: () -> bool
        """「下单」按钮的 `#enabled`。

        **服务端那句话是唯一判据**（`submit_ok`），客户端不去数红行——
        契约第 4 节：「颜色只是表现，服务端必须独立拒绝」。
        """
        model = self._preview_model()
        return bool(model is not None and model.can_submit())

    @Binder.binding(Binder.BF_BindString, DETAIL_BINDING)
    def order_preview_detail(self):
        # type: () -> str
        model = self._preview_model()
        return "" if model is None else (model.detail or "")

    @Binder.binding(Binder.BF_BindInt, COUNT_BINDING)
    def order_preview_count(self):
        # type: () -> int
        model = self._preview_model()
        if model is None:
            return 0
        return model.grid_count()

    @Binder.binding_collection(Binder.BF_BindInt, COLLECTION, ITEM_AUX_BINDING)
    def order_preview_row_id_aux(self, index):
        # type: (object) -> int
        """一行的图标。**用资源名现算 `id_aux`**，不用服务端那个 `aux`：
        后者是资源身份的一部分（附魔、附加值），而渲染要的是
        `Item(名字).GetBasicInfo().id_aux`——两者是两回事，混用会让附魔
        物品画成普通物品。同 `TerminalListView.get_item_id_aux` 的做法。
        """
        model = self._preview_model()
        if model is None:
            return 0
        row = model.row_of(index)
        if row is None:
            return 0
        return stack_size.basic_info(row["name"])[0]

    @Binder.binding_collection(Binder.BF_BindString, COLLECTION, ROW_NAME_BINDING)
    def order_preview_row_name(self, index):
        # type: (object) -> str
        model = self._preview_model()
        return "" if model is None else model.name_text(index)

    @Binder.binding_collection(Binder.BF_BindString, COLLECTION, ROW_COUNT_BINDING)
    def order_preview_row_count(self, index):
        # type: (object) -> str
        model = self._preview_model()
        return "" if model is None else model.count_text(index)

    @bind_color
    def order_preview_row_color(self, index):
        # type: (object) -> list
        """缺料行的背景色。**不缺的行也要给一个明确的颜色**：靠「不写」
        恢复会留着上一次预览的红色（契约第 4 节）。"""
        model = self._preview_model()
        if model is not None and model.row_missing(index):
            return order_preview.COLOR_MISSING
        return order_preview.COLOR_NORMAL

    @Binder.binding(Binder.BF_ButtonClickUp, CLOSE_BINDING)
    def OnOrderPreviewClose(self, params=None):
        # type: (object) -> None
        """关窗只收起预览：**不取消任何真实订单**（契约第 5 节）。"""
        if self.preview is not None:
            self.preview.dismiss()

    @Binder.binding(Binder.BF_ButtonClickUp, CANCEL_BINDING)
    def OnOrderPreviewCancel(self, params=None):
        # type: (object) -> None
        """取消预览**走独立事件**，不复用工厂终端的取消订单事件。"""
        if self.preview is not None:
            self.preview.cancel()

    @Binder.binding(Binder.BF_ButtonClickUp, CONFIRM_BINDING)
    def OnOrderPreviewConfirm(self, params=None):
        # type: (object) -> None
        """「下单」才发正式订单提交。

        **不可提交时这里必须挡住。** 按钮的 `#enabled` 在这台引擎上是否
        真的生效没有实测过（见交付说明的未验证项），而「按了没反应」与
        「按了但服务端拒绝」在玩家看来一样——所以真正的闸在服务端，
        这一层只是不发出那条注定被拒的请求。
        """
        if self.preview is not None:
            self.preview.submit()
