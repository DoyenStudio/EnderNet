# 物品列表窗口

客户端入口：`endernet/client/ui/item_list_window.py`。

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

签名为 `show_item_list_window(screen, item_ids, max_items, title="物品列表")`。
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
- 此接口没有网络库存参数，打开选择窗口时传入空网络列表；
  可以使用“全部”和“背包”，“网络”来源为空。
- 选择窗口打开期间，列表窗口的选中、添加、删除暂停。关闭列表窗口会关闭
  它自己打开的选择窗口；不会关闭其他调用方已经打开的选择窗口。

只接受物品 identifier，渲染使用 aux=0；本组件不改变真实玩家库存。
调用方应在界面创建后调用入口，在父 Screen 销毁时按示例关闭窗口。

## 控件与绑定

使用 `screen.AddElement("endernet_windows.item_list_window", ...)` 创建窗口，
根层级为 30，为滚动容器的子层级留出空间，低于选择窗口的 50。资源中的 `scroll_view` 继续引用
`endernet_windows.item_list_grid`；格子模板为 `endernet_windows.item_can_be_selected`。

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
