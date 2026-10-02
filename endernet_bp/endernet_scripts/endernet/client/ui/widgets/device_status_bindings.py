# coding=utf-8
"""设备状态栏与详情弹窗的**类体绑定**（11-13 / 11-07）。

这一组绑定原先长在 `client/ui/terminal_panel.py` 的 `TerminalPanelBase` 里，
11-07 给容器类设备加代理屏时被提出来：终端的屏与设备的屏用的是**同一份
JSON 模板**（`endernet_ui_common.device_stat_bar` 与
`endernet_windows.device_status_window_basic`），绑定名也本来就相同
（`#EnderNetDeviceStatus.*`），没有理由留两份。

## 两条不能忘的纪律

1. **必须写在类体里。** 运行时注册（`DynaBinding`）在这台引擎上无效——
   引擎是在**渲染期**按回调自己的名字在 `_Base` 上 `getattr`，运行时注册的
   包装函数名字不在那里，于是每渲染一帧报一次
   `no attribute 'reader'`，而界面只是空白（2026-09-30 真机撞出来的）。
2. **每个回调都要容忍 `self.status` 还没建起来**，并给**安全值**。绑定写在
   类体里、跟着界面走，而界面比会话活得久：`OnCreate` 之前、收尾之后引擎
   照常来问。

   安全值有一处取向要说清：格子是「空格子也要画」，状态栏是「**没拿到结论
   就不显示**」——宁可空着，也不先糊一个红色的「离线」上去。那是**方向
   错误**的结论：玩家会去查一个不存在的故障。
"""

from .....tooldelta.ui import Binder

from ....common.protocol import MSG_DEVICE_STATUS
from . import device_status

#: 颜色绑定的标志。`Binder` 是**引擎在运行期给的类**
#: （`clientApi.GetViewBinderCls()`），本仓库此前从没用过 `BF_BindColor`
#: （只用了 Bool/Int/String/ButtonClickUp/EditChanged），离线也拿不到它的
#: 属性表。取不到就**不注册这一条**：颜色退回模板里的静态值，界面的其余
#: 部分照常工作。
#:
#: 直接写 `Binder.BF_BindColor` 的代价不对称：引擎一旦没有这个标志，它在
#: **导入期**就 `AttributeError`，`register_screens()` 失败，客户端半边整个
#: 起不来——为了一个颜色把模组拖下水，不值。
COLOR_BINDING_FLAG = getattr(Binder, "BF_BindColor", None)


def bind_color(func):
    # type: (object) -> object
    """给状态文字注册颜色绑定；引擎没有这个标志时原样返回（即不注册）。"""
    if COLOR_BINDING_FLAG is None:
        return func
    return Binder.binding(COLOR_BINDING_FLAG, device_status.COLOR_BINDING)(func)


class DeviceStatusBindingsMixin(object):
    """给一块屏接上 `stat_bar` 与详情弹窗。混入者只需提供 `self.status`。

    `status` 的默认值必须是**类属性**：绑定比实例活得久（见模块 docstring）。
    子类通常在 `OnCreate` 里把它换成 `DeviceStatusModel`，收尾时置回 `None`。
    """

    #: `DeviceStatusModel` 或 `None`。给安全值的那几条都先判它。
    status = None

    def _make_status_handler(self, client):
        # type: (object) -> object
        """只更新创建回调时的状态模型，拒绝失效会话与旧屏的迟到响应。

        会话号由客户端校验，设备代次由模型校验。捕获模型身份，
        防止同一屏重新认领设备之后，旧回调更新新模型。
        """
        status = self.status

        def on_status_message(envelope, message):
            # type: (object, object) -> None
            if message is None or message.kind != MSG_DEVICE_STATUS:
                return
            if not client.has_session() or envelope.session_id != client.session_id:
                return
            if status is not None and self.status is status:
                status.apply(message)

        return on_status_message

    @Binder.binding(Binder.BF_BindString, device_status.TEXT_BINDING)
    def device_status_text(self):
        # type: () -> str
        if self.status is None:
            return ""
        return self.status.text()

    @bind_color
    def device_status_color(self):
        # type: () -> list
        if self.status is None:
            return device_status.COLOR_OFFLINE
        return self.status.color()

    @Binder.binding(Binder.BF_BindBool, device_status.VISIBLE_BINDING)
    def device_status_visible(self):
        # type: () -> bool
        if self.status is None:
            return False
        return self.status.visible()

    @Binder.binding(Binder.BF_BindString, device_status.ICON_BINDING)
    def device_status_icon(self):
        # type: () -> str
        if self.status is None:
            return device_status.ICON_OFFLINE
        return self.status.icon()

    @Binder.binding(Binder.BF_BindString, device_status.MASTER_TEXT_BINDING)
    def device_status_master_text(self):
        # type: () -> str
        if self.status is None:
            return device_status.MASTER_ROW_MISSING
        return self.status.master_text()

    @Binder.binding(Binder.BF_BindString, device_status.MASTER_ICON_BINDING)
    def device_status_master_icon(self):
        # type: () -> str
        if self.status is None:
            return device_status.MASK_OFFLINE
        return self.status.master_mask()

    @Binder.binding(Binder.BF_BindString, device_status.NET_TEXT_BINDING)
    def device_status_net_text(self):
        # type: () -> str
        if self.status is None:
            return device_status.NET_ROW_UNKNOWN
        return self.status.net_text()

    @Binder.binding(Binder.BF_BindString, device_status.NET_ICON_BINDING)
    def device_status_net_icon(self):
        # type: () -> str
        if self.status is None:
            return device_status.MASK_OFFLINE
        return self.status.net_mask()

    @Binder.binding(Binder.BF_BindBool, device_status.POPUP_VISIBLE_BINDING)
    def device_status_popup_visible(self):
        # type: () -> bool
        if self.status is None:
            return False
        return self.status.popup_visible()

    @Binder.binding(Binder.BF_ButtonClickUp, device_status.OPEN_BINDING)
    def OnDeviceStatusClick(self, params=None):
        # type: (object) -> None
        """点状态栏弹出详情。**幂等**：连点只是重复设为显示，开不出第二个。"""
        if self.status is not None:
            self.status.open_popup()

    @Binder.binding(Binder.BF_ButtonClickUp, device_status.CLOSE_BINDING)
    def OnDeviceStatusClose(self, params=None):
        # type: (object) -> None
        """只收起详情，**不关原设备界面**。"""
        if self.status is not None:
            self.status.close_popup()
