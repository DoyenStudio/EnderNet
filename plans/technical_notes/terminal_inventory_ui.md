# 通用终端模拟物品栏 UI

代码：`endernet_bp/endernet_scripts/endernet/client/ui/`。`TerminalPanelBase` 继承 ToolDeltaScreen，`CommonTerminalPanel` 注册为 `common_terminal_panel_ui.main`，由客户端 bootstrap 导入注册。当前是独立模拟数据，不读取或修改真实玩家背包，也未接入网络库存。

## 槽号与交互

- 背包 0～26（27 格，9×3）；快捷栏 27～35（9 格，9×1）。合计 36 格；这些是 UI 槽号，未来接入真实背包时仍需显式映射引擎槽号。
- 点击非空槽选择来源，再点击目标：空槽整堆移动，同 ResourceKey 合并到物品 maxStackSize，不同物品交换。合堆后的余量保留在来源并继续选中；目标同类且已满则不操作。
- 再点来源取消选择。空手点空槽无动作。关闭/失活只取消选择，没有光标持有物，也没有待返还物品。
- 不实现分堆、拖拽、快捷键或真实服务端存取。合堆身份包含附魔、耐久和自定义数据，不仅比较物品名称。

## 调用与复用

在客户端 UI 初始化完成之后调用，导入路径按调用者包位置调整：

```python
from .ui.terminal_panel import CommonTerminalPanel

screen = CommonTerminalPanel.OpenDemo()
# 或传入 36 个 Item / None，未传 items 时默认全空：
# screen = CommonTerminalPanel.CreateUI({"items": items})
items_after_interaction = screen.GetInventoryItems()
# screen.SetInventoryItems(items)  # 整体替换，取消旧选择
```

OpenDemo 在 0/1/2/26/27/35 放置示例物品，用于验证合堆溢出、异类交换、两区边界和空格移动；不自动弹窗，也不向真实玩家发放物品。

其他终端继承 TerminalPanelBase 并单独使用 RegistToolDeltaScreen 注册。复用 `endernet_terminal_inventory.inventory` JSON 模板，默认路径 `/terminal/inventory`；布局不同可覆写 INVENTORY_PATH。继承的静态 Binder 方法由 ToolDelta 注册器发现。完成内容变化后调用 OnInventoryChanged(indices)，子类可覆写；选择变化不调用该钩子。

模型保存数据副本；GetInventoryItems 返回副本，修改返回值不会暗中改变界面。外部需要保留模拟结果时显式取出该快照。后续接入真实背包时须另加服务端交易与确认流程，不能直接把本地模拟结果作为库存余额上传。

CustomSlotView 缓存子控件引用，资源身份未变时仅更新数量/选择状态；只刷新本次涉及的槽位，无每帧全表刷新。身份变化时重新设置图标、耐久并清理旧状态。

## 验证

离线入口：`python endernet_tests/run_tests.py`。用例覆盖移动、取消、完全/部分合堆、16 堆叠上限、异类/元数据交换、输入隔离、无效输入与随机操作数量守恒。

游戏内仍需验证：初始化后 OpenDemo 能打开、36 格完整显示、点击抬起只触发一次、鼠标/触控 collection_index、关闭按钮与系统返回、附魔图标及耐久显示。离线用例不代表 ModSDK 渲染和事件已实测通过。
