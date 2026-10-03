# coding=utf-8
"""两个**控件 API 的形状坑**，收在一处（都是 2026-10-03 真机上撞出来的）。

## 背景：同一条路径上会拿到两种控件对象

本仓取控件的路有两条，**返回的对象不是一回事**：

| 取得方式 | 返回 | `.asButton()` 给什么 |
| --- | --- | --- |
| `screen.AddElement(...)` / 它的 `.GetElement(...)` | tooldelta 的 `UBaseCtrl` | tooldelta 的 `UButton`（有 `SetCallback`） |
| `client/ui/slot_layout.control(screen, ...)`（走 `GetBaseUIControl`） | **引擎自己的** `BaseUIControl` | **引擎的** `ButtonUIControl`（**没有 `SetCallback`**） |

于是「同一个写法在有的地方好用、在有的地方静默失效」——而失效的那一侧
**不抛到顶、不刷屏**，只是那一下点了没反应。`slot_layout` 那条路是取
**主面板上**的控件用的（两种屏幕挂法要按候选试），正是它踩到这个坑。

## 坑一：`SetUiItem` 收 `(name, aux)`，不收 `Item`

真机上 `asItemRenderer()` 给的是引擎的
`ItemRendererUIControl`（`mod.client.ui.controls.itemRendererUIControl`），
它的 `SetUiItem` **至少收两个参数**。传一个 `Item` 对象会抛：

    TypeError: SetUiItem() takes at least 3 arguments (2 given)

本仓有三处写成那个形状（`select_item_window`、`crafting_panel`、`port_panel`），
前两处**都在没上过机的路径上**（11-03 的名单窗口、11-04 的合成预览），
所以这个错一直没被发现——症状只是「那一格是空的」。唯一在真机验过的
调用点是 `flying_item`，它用的正是 `(name, aux)`。

## 坑二：`SetCallback` 只长在 tooldelta 的 `UButton` 上

引擎的 `ButtonUIControl` **没有** `SetCallback`。tooldelta 的
`UButton.SetCallback(cb)` 内部做的是两件事：

    self.base.AddTouchEventParams({"isSwallow": True})
    self.base.SetButtonTouchUpCallback(cb)

这两个方法引擎控件**都有**（真机 `dir()` 逐条看过），因此经
`slot_layout` 拿到的按钮要么显式转成 `UButton`、要么直接调这两个。
`on_click` 两种形状都认。
"""


def show_item(renderer, name, aux=0):
    # type: (object, str, int) -> None
    """把 `name`/`aux` 画进 `item_renderer` 控件。见模块 docstring 的坑一。

    **收名字与辅助值，不收 `Item`**：`Item` 也提供这两样
    （`newItemName` / `newAuxValue`），调用方自己取出来传进来即可。

    不吞异常：调用方各自决定要不要容忍（`port_panel` 那条会计数，
    渲染失败不该炸掉整帧）。正确的形状本身不会抛。
    """
    renderer.asItemRenderer().SetUiItem(name, aux)


def on_click(control, callback):
    # type: (object, object) -> None
    """把一个**控件级**点击回调接到按钮上。见模块 docstring 的坑二。

    控件级回调不经 `Binder` 注册表，因此**与两套注册机制都无关**——类体绑定
    与运行时注册（`DynaBinding`）**在这台引擎上都可用**了：后者的成因已于
    2026-10-03 修掉，见 `plans/tasks/11-交付说明.md` 第十二节的后续。

    `{"isSwallow": True}` 是照抄 tooldelta `UButton.SetCallback` 的取值：
    它让这次触摸不再往下传——不吞的话，点在容器屏幕的按钮上会同时落到
    底下的槽位上。
    """
    button = control.asButton()
    set_callback = getattr(button, "SetCallback", None)
    if set_callback is not None:
        # tooldelta 的 `UButton`（`GetElement` 那条路上拿到的就是它）。
        set_callback(callback)
        return
    # 引擎自己的 `ButtonUIControl`：做 `UButton.SetCallback` 内部那两件事。
    button.AddTouchEventParams({"isSwallow": True})
    button.SetButtonTouchUpCallback(callback)
