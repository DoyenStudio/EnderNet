# ToolDelta 动态绑定修复记录

日期：2026-10-03。范围：上游子模块 `tooldelta/ui/general_screen.py` 的通用框架修复。

## 根因与 SDK 依据

对照本机 `vanillaMCP_3.7.21.288355_server`：

- `mod/client/ui/viewBinder.py` 的装饰器只添加 `binding_flags`、`binding_name` 和可选的 `collection_name`。
- `mod/client/ui/screenBase.py` 的 `_process_default` / `_process_collection` 将绑定宿主和回调交给 `gui`。
- `minecraft/gui.py` 的注册函数继续向 `_gui` 传递 `func.__name__`，而非回调对象。

因此动态回调必须能通过 `getattr(绑定宿主, func.__name__)` 取得。原实现仅创建绑定方法并保存在 ToolDelta 的字典里；原生层无法在生成的屏幕实例上找到 `reader` 等属性。这与 [终端真机失败记录](../ui/common_terminal_panel.md) 相符。

## 修复

- 注册前，将包装方法安装到当前绑定宿主实例，使用递增的私有名称，不复制业务回调名称，不覆盖静态绑定。
- 不同绑定即使使用同一个回调，也有独立包装名称；不同屏幕实例互不影响。
- 替换、反绑定和销毁时移除包装属性；注册失败移除未完成的属性；反绑定失败保留记录，允许重试。
- 生成的代理屏幕把代理实例传给 ToolDeltaScreen，绑定宿主与静态绑定保持一致。底层控件访问仍通过代理的 ScreenNode；动态注册使用代理的 `GetScreenName()`。
- 选择器离线测试按注册宿主查找回调，符合 SDK 向原生层传递的实例语义。

保留原有 API 和回调参数约定。集合解绑仍按旗标与集合批量处理；SDK 支持的集合旗标仍为 Bool、Int、Float、String、Color。本次没有修改 SDK 的分发规则。

## 验证与边界

- `test_tooldelta_dyna_binding.py`：13 项框架回归通过，并单独检查该框架文件的 Python 2.7 语法。
- `test_select_item_window.py`：22 项通过。
- 临时离线核对直接执行本机 SDK 的 ViewBinder、ScreenBase 和 gui 注册函数，替换原生 `_gui` 为按名称查找的记录器：26 次注册均能找到回调，普通绑定、集合绑定、编辑框/滑块组合旗标及代理解绑通过。仅为在 Python 3.11 上读取 SDK，将 gui 文件的 Python 2 print/except 语法在内存中转换；未修改 SDK 文件。
- 框架修复时的全量检查曾有列表窗口、流体选择器相关失败：修复前基线同样有 2 个失败、15 个错误。这是当时业务测试尚未对齐的记录。
- 同日选择器改造完成后，业务全量 3544 项测试、112 个源码文件的 Python 2.7 语法门禁及生成资源检查通过，见 [物品选择器验证记录](select_item_window.md)。

未运行游戏，也未在真实 Python 2.7 或原生 `_gui` 上验证。本记录不替代历史真机验收，不代表已有动态绑定的真机成功案例。
