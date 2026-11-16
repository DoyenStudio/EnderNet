# 物品列表窗口

客户端入口：`endernet/client/ui/windows/item_list.py`。

```python
# 在 endernet/client/ui/ 下的 ToolDeltaScreen 脚本中：
from .item_list_window import WINDOW_ATTRIBUTE, show_item_list_window

def open_item_list(self):
    # self.item_ids 是调用方保存的 list[str]。
    return show_item_list_window(self, self.item_ids, 64, "输入白名单")

def OnDestroy(self):
    window = self._vars.get(WINDOW_ATTRIBUTE)
    if window is not None:
        window.close()
    # 保留调用方已有的销毁逻辑。
```

签名为 `show_item_list_window(screen, item_ids, max_items, title="物品列表", on_selected=None, network_item_ids=None)`。
`screen` 是当前已创建的 `ToolDeltaScreen`；其余三个参数依次是已有物品
ID 列表、最多物品数和可选标题。创建成功返回 `True`；该 Screen 已有列表窗口
时直接返回 `False`，保留原窗口。使用 `_vars["_endernet_item_list_window"]`
保存窗口实例及打开标记，关闭或创建失败时删除。

## 列表与操作

- **直接修改传入的 list**，不复制、不提交服务端。添加或删除后调用方立即看到
  变化，关闭保留修改，没有取消回滚或结果回调。
- 初始列表按传入顺序显示，不自动选中。点击一格后单选该项；
  选中框的 `visible` 由 Binder 决定，不通过 `SetVisible` 手动切换。
- 添加会调用已有的 `show_select_item_window`，选择后将新 ID 追加到原 list
  并选中新项。若已有同 ID，则选中首次出现的那一项，不重复添加。
  既有列表中的重复项保持原样，删除按选中下标精确删除一项。
- 删除按钮仅在有有效选中项时可用；删除后清空选择，避免连续点击误删下一项。
- `max_items` 必须是非负整数（不接受 bool）；0 表示不可添加。
  初始列表超过上限时抛出 `ValueError`，不会截断用户数据。
  添加按钮和选择回调均检查容量，保证窗口不会追加超过上限。
- 列表网格动态显示全部已有项，沿用控件的滚动容器和每行 8 格布局，
  不受物品选择窗口每次显示 40 项的限制。
- 可选的 `network_item_ids` 提供网络库存 ID 快照，复制并去重后传给选择窗口；
  未提供时“网络”来源为空。“全部”和“背包”沿用选择器已有来源。
  宿主可调用 `set_network_item_ids(ids)` 更新快照及本窗口打开的选择器，
  保留当前来源与搜索词。
- 总线物品存货口、取货口使用当前设备会话的 `TerminalCache` 消费库存快照与
  增量，提供存量大于零的物品 ID；不把仅有合成样板的零库存物品列入网络来源。
  库存晚到或发生增删时，打开中的网络选择器随之刷新。
- 选择窗口打开期间，列表窗口的选中、添加、删除暂停。关闭列表窗口会关闭
  它自己打开的选择窗口；不会关闭其他调用方已经打开的选择窗口。

只接受物品 identifier，渲染使用 aux=0；本组件不改变真实玩家库存。
调用方应在界面创建后调用入口，在父 Screen 销毁时按示例关闭窗口。

## 控件与绑定

宿主内容区预制 `endernet_item_list_window@endernet_windows.item_list_window`，
控制器通过 `window_control` 获取并显示；宿主根层级为 55，选择窗口为 65。
`scroll_view` 的相对层级为 2，引用 `endernet_windows.item_list_grid`；
格子模板为 `endernet_windows.item_can_be_selected`。层级沿父子控件累加，
滚动视口额外增加 1 层；格子图标、数量、选中框和点击层均须低于选择窗的
`modal_backdrop`（相对窗口为 -1），防止穿透遮挡及点击。

当前实现由宿主的 `ItemListWindowBindingsMixin` 声明静态 Binder 绑定，
窗口对象提供数据与操作；关闭窗口清理实例，静态绑定保留并返回安全值。
绑定名与选择窗口独立，设备屏已有生产接入。

2026-09-30 曾因 ToolDelta 动态回调没有安装到注册宿主上，发生按名称查找失败。
该框架缺陷已于 2026-10-03 修复，见 [修复记录](tooldelta_dynamic_binding.md)。
`DynaBinding` / `DynaBindingCollection` 已可按窗口生命周期注册和注销；
本组件目前保留静态 mixin 实现，不再将动态绑定列为不可用方案。修复已做离线验证，
不代表本组件已完成动态绑定的真机验收。

| 控件 / 用途 | 绑定名 | 标志 / 目标 |
| --- | --- | --- |
| `title/title_tip` 标题 | `#EnderNetItemList.title` | `BF_BindString` → `#text` |
| `item_list_grid` 数量 | `#EnderNetItemList.count` | `BF_BindInt` → `#maximum_grid_items` |
| `add_btn` 可用状态 | `#EnderNetItemList.can_add` | `BF_BindBool` → `#enabled` |
| `del_btn` 可用状态 | `#EnderNetItemList.can_delete` | `BF_BindBool` → `#enabled` |
| 格子本体 | `#EnderNetItemList.has_item` | collection `BF_BindBool` → `#visible` |
| 格子 `item_renderer` | `#EnderNetItemList.item_id_aux` | collection `BF_BindInt` → `#item_id_aux` |
| 格子 `selected_frame` | `#EnderNetItemList.selected` | collection `BF_BindBool` → `#visible` |
| 格子 `interact_btn` | `#EnderNetItemList.select` | `BF_ButtonClickUp`，读取 `#collection_index` |

collection 名统一为 `endernet_item_list`。图标来自
`Item(id).GetBasicInfo().id_aux`，按窗口缓存；越界格子隐藏且返回 0。
`selected_frame` 使用 `binding_condition: "always"`，隐藏后仍可因选择变化重新显示。
添加、删除和关闭按钮按具体控件实例设置回调。

## 验证

新增离线测试覆盖绑定与 JSON 契约、列表原地增删、选中框状态、实际选择窗口
联动、去重添加、容量限制、空列表、超过 40 项的列表、重复打开、窗口归属、
创建失败清理及关闭时解绑。遵照用户要求不进行游戏内验证。

2026-09-27：`python -B endernet_tests/run_tests.py` 通过 2178 项测试，
72 个源码文件通过 Python 2.7 兼容门禁；本次 UI JSON 的 `git diff --check` 通过。

2026-11-15：修复总线物品取货口选择器的网络来源为空：接口屏消费既有物品
视图通道，并把库存快照传给名单窗口；存货口同步修复。新增回归覆盖真实协议
快照、增量删除、aux 去重、零库存样板过滤、库存晚到刷新及保留搜索词。
完整离线 4941 项测试、190 个文件的 Python 2.7 语法门禁、生成资产检查通过。
MCDK 实机重开取货口后，缓存收到 27 条库存记录，“网络”页正常显示其中
23 种有库存物品；点击物品可添加至名单草稿，验证后移除该草稿项，未提交配置。

2026-11-15（弹窗层级）：将物品及流体列表滚动区的相对层级从 9 降为 2。
新增回归检查覆盖五处列表／选择器宿主，旧值均触发失败；完整离线 4987 项测试、
190 个文件的 Python 2.7 语法门禁、生成资产检查通过。
MCDK 3.10 在原界面临时设置层级后确认图标穿透消失；随后原生重载 JSON UI，
用存货口资源及临时测试宿主重新创建名单和选择器，确认资源加载的滚动区层级为 2。
鼠标点击搜索框可输入并筛选，未选中下层格子；关闭选择器后下层格子恢复正常选择。
测试不连接设备配置会话。[选择器截图](../tasks/validation/ui_modal_layering_2026-11-15.jpg)。
