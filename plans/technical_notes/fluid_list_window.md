# 流体列表窗口

客户端入口：`endernet/client/ui/fluid_list_window.py`。参照物品列表窗口，复用其编辑生命周期，使用独立资源、绑定和流体选择器。

```python
# 在 endernet/client/ui/ 下已创建的 ToolDeltaScreen 中：
from .fluid_list_window import WINDOW_ATTRIBUTE, show_fluid_list_window

# fluid_ids 为调用方持有的 list[str]；上限来自设备规则。
show_fluid_list_window(self, fluid_ids, max_fluids, "流体黑名单")

# 合入宿主已有 OnDestroy 清理逻辑：
window = self._vars.get(WINDOW_ATTRIBUTE)
if window is not None:
    window.close()
```

签名为 `show_fluid_list_window(screen, fluid_ids, max_fluids, title="流体列表")`。
成功返回 True；同一 Screen 已有流体名单窗口时返回 False 并保留原窗口。
`fluid_ids` 必须是 list；上限必须是非负整数，不接受 bool。初始列表超过上限会报错，不会截断数据。

## 编辑行为

- 直接修改传入的流体 ID list，关闭保留修改；没有自动提交服务端、取消回滚或结果回调。
- 初始按传入顺序展示，不自动选中。点击单选；删除仅对有效选中项生效，删除后清空选择。
- 添加调用 `show_select_fluid_window`，选择后追加流体 ID 并选中新项。重复 ID 只选中首次出现的已有项，不重复追加；初始重复项不自动清理。
- 添加按钮和选择回调均检查上限；上限为 0 时不能添加。列表每行 8 格，滚动展示全部已有项，不受选择器单页 40 项限制。
- 选择器使用流体目录；“背包”把支持的桶映射成流体 ID，过滤普通物品。当前接口没有网络快照参数，“网络”来源为空。
- 图标使用流体 identifier 对应的 `Item(...).GetBasicInfo().id_aux`，按窗口缓存。列表仅配置 ID，不存储流体数量，不改变背包或设备缓冲。
- 流体选择器打开期间暂停名单的选择、添加和删除。关闭名单会关闭它自己打开的流体选择器，不关闭其他调用方的选择器。

## 资源与绑定

窗口定义为 `endernet_windows.fluid_list_window`，实例名为 `endernet_fluid_list_window`，运行时 layer=30，低于流体选择器的 50。
滚动内容引用 `endernet_windows.fluid_list_grid`，格子模板为 `endernet_windows.fluid_can_be_selected`。

collection 为 `endernet_fluid_list`；前缀为 `#EnderNetFluidList.`。

| 绑定后缀 | 类型／用途 |
| --- | --- |
| title | BF_BindString → 标题 #text |
| count | BF_BindInt → #maximum_grid_items |
| can_add、can_delete | BF_BindBool → 按钮 #enabled |
| has_item | collection BF_BindBool → 格子 #visible |
| item_id_aux | collection BF_BindInt → 渲染器 #item_id_aux |
| selected | collection BF_BindBool → 选中框 #visible |
| select | BF_ButtonClickUp，读取 #collection_index |

`has_item`、`item_id_aux` 沿用公共渲染语义命名，实际数据是流体 ID。
所有动态绑定仅在窗口打开期间注册；关闭时注销。窗口保存在 `_vars["_endernet_fluid_list_window"]`，关闭或创建失败时移除。
物品名单与流体名单的 collection、绑定前缀、窗口标记均独立，关闭其中之一不移除另一方绑定。

## 验证与范围

2026-09-28：`python -B endernet_tests/run_tests.py` 通过 2218 项测试，75 个源码文件通过 Python 2.7 兼容门禁。
新增测试覆盖 JSON／Binder 契约、增删、去重、容量边界、长列表、重复打开、创建失败清理、选择器归属、真实流体选择器联动、背包桶映射及两类名单共存。
尚未进行游戏内验证；本次提供公共组件，不代表流体输入总线 Screen 和服务端配置保存已经接入。
