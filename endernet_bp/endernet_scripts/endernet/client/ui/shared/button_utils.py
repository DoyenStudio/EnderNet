# coding=utf-8
"""客户端 Ore UI 按钮的触摸反馈。"""


def SetOreUiButtonCallback(button, callback):
    # type: (object, object) -> object
    """注册释放回调，并在按压期间将 button_label / icon 下移一像素。

    button 接受 ToolDelta 控件或原生 ButtonUIControl。callback 接收引擎
    传入的触摸事件参数；释放时先恢复位置再调用，取消触摸只恢复位置。
    返回传入的控件。此函数会替换按钮原有的触摸回调。
    """
    if not callable(callback):
        raise TypeError("callback must be callable")
    control = getattr(button, "base", button).asButton()
    shifted = []

    def restore():
        # type: () -> None
        for child in shifted:
            x, y = child.GetPosition()
            child.SetPosition((x, y - 1))
        del shifted[:]

    def on_down(args):
        # type: (dict) -> None
        if shifted:
            return
        for path in ("/button_label", "/icon"):
            child = control.GetChildByPath(path)
            if child is not None:
                x, y = child.GetPosition()
                child.SetPosition((x, y + 1))
                shifted.append(child)

    def on_up(args):
        # type: (dict) -> object
        restore()
        return callback(args)

    def on_cancel(args):
        # type: (dict) -> None
        restore()

    control.AddTouchEventParams({"isSwallow": True})
    control.SetButtonTouchDownCallback(on_down)
    control.SetButtonTouchUpCallback(on_up)
    set_cancel = getattr(control, "SetButtonTouchCancelCallback", None)
    if set_cancel is not None:
        set_cancel(on_cancel)
    return button
