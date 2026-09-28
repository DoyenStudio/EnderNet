# 通用流体选择窗口

客户端入口：`endernet/client/ui/select_fluid_window.py`。

```python
# 在 endernet/client/ui/ 下的 ToolDeltaScreen 脚本中：
from .select_fluid_window import WINDOW_ATTRIBUTE, show_select_fluid_window

def open_fluid_picker(self):
    return show_select_fluid_window(
        self,
        ["minecraft:water", "minecraft:lava"],  # 调用方已有的网络流体 ID 快照
        self.on_fluid_selected,
    )

def on_fluid_selected(self, fluid_id):
    # 回调前窗口已关闭，标记已删除。
    self.selected_fluid_id = fluid_id

def OnDestroy(self):
    picker = self._vars.get(WINDOW_ATTRIBUTE)
    if picker is not None:
        picker.close()
    # 保留父 Screen 已有的销毁逻辑。
```

签名：`show_select_fluid_window(screen, network_fluid_ids, select_callback) -> bool`。
参数依次是当前已创建的 `ToolDeltaScreen`、网络流体 ID 列表和接收流体 ID 的回调。
创建成功返回 `True`；同屏已有流体选择窗口时直接返回 `False`，保留原窗口及回调。
创建前在 `screen._vars["_endernet_select_fluid_window"]` 写入窗口实例作为标记，
关闭或创建失败时删除；也可从该键取出实例调用 `close()` 主动取消。

## 流体识别规则

前后缀仅作用于命名空间冒号后的名称部分，保留原命名空间，且只移除一次。
统一展示和回传移除前后缀后的**基础 ID**，不回传桶 ID 或 flowing ID。

| 来源 | 判定条件 | 示例及结果 |
| --- | --- | --- |
| 全部已注册物品 | 名称以 `flowing_` 开头，移除后基础 ID 满足 `ItemExists` | `minecraft:flowing_water` → `minecraft:water` |
| 玩家背包 | 非空且数量大于 0 的物品，名称以 `_bucket` 结尾，移除后基础 ID 满足 `ItemExists` | `mod:oil_bucket` → `mod:oil` |
| 网络 | 使用调用方传入的基础流体 ID 快照，按首次出现顺序去重 | `["mod:oil", "mod:oil"]` → `["mod:oil"]` |

两个识别条件相互独立：只要 `mod:milk` 存在，背包中的 `mod:milk_bucket`
就可识别为 `mod:milk`，不要求注册 `mod:flowing_milk`。
基础 ID 不存在、前后缀位置不正确、去掉后名称为空的候选均不接受。
例如 `mod:not_flowing_water`、`mod:water_bucket_extra` 不匹配。
不会硬编码水、岩浆，也不额外检查 fluid 标签或方块类别。

## 初始化与客户端数据

项目的 `ItemExists` 是 ToolDelta 服务端接口
`tooldelta.api.server.item.ItemExists`（封装 `LookupItemByName`）。为避免客户端
导入服务端 API，复用现有 `UiInitFinishedEvent` 后的一次目录请求：

1. 服务端 `server/adapters/item_catalog.py` 调用 `GetLoadItems(True)`。
2. 对原始目录调用 `common/fluid_catalog.py` 的 `build_fluid_catalog`，
   按上述规则调用 `ItemExists`，生成全部流体列表及桶到流体的映射。
3. 原有物品目录继续按 `GetBasicInfo().itemCategory` 过滤；流体识别在此过滤前
   完成，不受 `""`、`"command"`、`"none"` 分类影响。
4. `EnderNet:ItemCatalogResponse` 一并回传以下三个字段，服务端按会话缓存：
   `item_ids: list[str]`、`fluid_ids: list[str]`、`bucket_fluid_ids: dict[str, str]`。
5. 客户端缓存目录。背包来源读取本地玩家主背包与快捷栏，在已通过 `ItemExists`
   校验的桶映射中查找；不读取服务端背包，也不为每次搜索另发请求。

目录未到达时，“全部”和“背包”暂为空，“网络”仍可显示传入的快照。
目录到达后刷新已打开的选择窗口，包括当前正在显示的背包来源。
卸载时关闭选择窗口、注销目录监听并清空三份缓存。

## 窗口行为与 UI 绑定

- 定义：`endernet_windows.select_fluid_window`；实例名：`endernet_select_fluid_window`。
  在当前 Screen 中创建，调用 `SetLayer(50)`。
- 默认来源为“全部”。“背包”按槽位顺序去重，“全部”按基础 ID 排序去重，
  “网络”使用传入列表的复制快照。背包来源不包含盔甲栏、副手。
- 搜索词先 `strip()`，为空则不筛选；否则忽略大小写，匹配基础流体 ID 或
  `Item(fluid_id).GetBasicInfo().itemName`，支持中文。
- 先搜索完整来源，再截取前 40 项；沿用 8×5 网格，无翻页，空格隐藏。
  图标沿用控件的 `item_renderer`，渲染 `Item(fluid_id)`（aux=0）。
- 当前来源按钮的 `default` 和 `hover` 图片设为 pressed，其他按钮恢复各自材质。
- 选择后先关闭和清理，再调用一次 `select_callback(fluid_id)`；取消不回调。
  不消耗桶、不转移流体、不更改库存。

复用物品选择窗口的布局操作、搜索、回调与生命周期；流体窗口仅替换定义、
实例名、来源和绑定名：

| 项目 | 约定 |
| --- | --- |
| 来源按钮 | `select_from_backpack_btn` / `select_from_net_btn` / `select_from_all_btn` |
| 搜索框 | `fluid_search_editor` |
| 搜索事件 | `#EnderNetSelectFluid.search`，运行时 `Binder.BF_EditChanged` |
| 搜索内容变量 | `#EnderNetSelectFluid.search_content` |
| 网格路径 / collection | `fluids/grid` / `endernet_select_fluids` |
| 格子模板 | `endernet_windows.selected_item` |
| 关闭按钮 | `close` |

流体窗口和物品窗口的标记、根实例及搜索绑定互相独立，关闭流体窗口不会删除
物品窗口的绑定。父 Screen 销毁时仍需按示例调用各自窗口的 `close()`。

## 验证记录

离线测试覆盖命名空间、前后缀位置、基础 ID 不存在、只去掉一层前后缀、
目录分类过滤前识别、桶规则与 flowing 规则独立、背包去重、三来源、名称/ID
搜索、40 项上限、独立标记与绑定、关闭后回调、目录晚到刷新及卸载清理。
遵照用户要求不进行游戏内验证。

2026-09-28：`python -B endernet_tests/run_tests.py` 通过 2205 项测试，
74 个源码文件通过 Python 2.7 兼容门禁；本次涉及文件的 `git diff --check` 通过。
