# 数量设置窗口脚本接口

> **2026-10-03 新增需求（待实现）：** 新增共享网络列表调用方：长按带 + 的条目使用 show_number_set_window 选择订单数量，确认回调打开 order_preview_window 并请求服务端预览；取消不发请求。通用数量组件接口不变，业务范围由调用方和服务端校验。 完整规则见 [网络样板标记与下单预览契约](../technical_notes/terminal_pattern_order_preview.md)。

`client/ui/number_set_window.py` 为已有的 `endernet_windows.number_set_window`
提供可嵌入任意 `ToolDeltaScreen` 的数量编辑窗口，与物品选择窗口使用方式一致。

```python
from .number_set_window import WINDOW_ATTRIBUTE, show_number_set_window

def on_confirm(number):
    # number 是玩家确认的整数；此时窗口已经关闭。
    pass

show_number_set_window(screen, 64, on_confirm, min_number=1, max_number=4096)
show_number_set_window(screen, 1, on_confirm, title="设置流体数量")
```

- `show_number_set_window(screen, number, confirm_callback, min_number=1, max_number=None, title="设置数量")`：
  返回是否成功打开；同一屏幕重复打开返回 `False`，保留已有窗口及其回调。
- `title` 覆盖窗口标题 `number_set_window/title/title_tip` 的文本，默认与 JSON 里的
  `"设置数量"` 一致。该 label 走 `SetText`（与合成面板标题同一写法），标题类型非字符串
  时抛 `TypeError`，不创建控件。
- 初始数量和 `screen._vars[WINDOW_ATTRIBUTE].set_number(number)` 都按范围截取。
  默认允许正整数，不设置上限；调用方可显式设置最小值为 `0`。
- 六个按钮分别增减 1、10、100，到达范围边界后停在边界值。
  空白或非法输入时，从最近一次有效数量继续增减。
- 手动输入仅接受十进制整数，可带首尾空白。空值、负数、小数、非数字及越界数量
  点击确认时不提交，窗口保持打开。确认直接读取输入框，回调只调用一次。
- 点右上角关闭按钮仅取消，不调用确认回调。宿主的 `OnDestroy` 可取出
  `screen._vars.get(WINDOW_ATTRIBUTE)` 并调用其 `close()`；该方法可重复调用。
- 数量输入使用独立的 `#EnderNetNumberSet.number` 动态绑定，关闭时解绑，
  不复用物品选择窗口的搜索绑定。

这是通用 UI 组件，具体业务数量范围由调用方提供，服务端仍需校验提交数量。
未改变终端手势映射或其他未定玩法规则。

离线回归覆盖打开、手动输入、六种步进、范围、确认/取消、重复打开、销毁清理、
创建失败、标题默认值与自定义及 JSON 绑定名。实际输入法、按钮事件和窗口显示
仍需在游戏内验证。
