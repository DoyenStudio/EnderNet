# 通用物品 ID 选择窗口

入口：`endernet/client/ui/select_item_window.py` 的
`show_select_item_window(screen, network_item_ids, select_callback)`。
只在客户端、当前 `ToolDeltaScreen` 已创建后调用；不需要继承新的 Screen，
也不需要在调用方添加 Binder 装饰器。

```python
# 在 endernet/client/ui/ 下的 Screen 脚本中：
from .select_item_window import WINDOW_ATTRIBUTE, show_select_item_window

def open_item_picker(self):
    return show_select_item_window(
        self,
        ["minecraft:stone", "minecraft:iron_ingot"],
        self.on_item_selected,
    )

def on_item_selected(self, item_id):
    # 窗口已关闭；这里只回传 identifier，没有 aux、数量或 NBT。
    self.selected_item_id = item_id

def OnDestroy(self):
    picker = self._vars.get(WINDOW_ATTRIBUTE)
    if picker is not None:
        picker.close()
    # 保留调用方原有的销毁逻辑。
```

`screen` 是 ToolDeltaScreen 实例；`network_item_ids` 是调用方已有的网络物品
ID 列表，窗口复制并按首次出现顺序去重，不发送网络库存请求。
`select_callback(item_id)` 接收选中的字符串 ID，仅在选中后调用一次。
返回值为 `bool`：创建完成返回 `True`；若 `screen._vars` 已存在
`"_endernet_select_item_window"` 键（常量 `WINDOW_ATTRIBUTE`），直接返回 `False`，
保留原窗口、搜索状态、来源和回调。创建控件前写入该标记，值为窗口实例，
同时可作为主动关闭的句柄；创建异常会清理标记并继续抛出异常，允许后续重试。
`close()` 可重复调用，取消不调用选择回调；关闭时删除标记，之后可再次打开。
父 Screen 销毁时按示例从 `_vars` 取出句柄并调用 `close()`，释放缓存和标记；
父屏原生控件和 ToolDelta 动态绑定也随屏销毁。

## 数据来源与搜索

- 默认选中“全部”：使用本次游戏会话的已注册物品目录，包括模组物品。
- “背包”：读取本地玩家主背包和快捷栏，跳过空槽、零数量和空气，按 ID 去重；
  切换到背包或改变搜索词时重新读取，不包含盔甲栏、副手。
- “网络”：使用调用时传入的 ID 快照；更新快照可先关闭再打开窗口。
- 搜索词先 `strip()`，为空时不筛选；否则不区分大小写，按
  `Item(item_id).GetBasicInfo().itemName` 或完整 ID 做子串匹配。
  名称按窗口缓存，支持 UTF-8 中文与 Unicode 输入。
- 始终先搜索完整来源，再取前 40 个匹配项。网格为 8×5，没有翻页；
  超过 40 项时可用搜索缩小范围。未使用格子隐藏，无结果时全部隐藏。
- 窗口只选择物品类型；渲染使用 aux=0，不搬运、扣除或生成物品。

## 全部物品目录初始化

`modMain.py` 在两端初始化时分别启动 `client/item_catalog.py` 与
`server/adapters/item_catalog.py`，在两端卸载时注销监听并清空缓存。
事件载荷定义在不依赖引擎的 `common/item_catalog.py` 中。

1. 客户端收到 `UiInitFinishedEvent` 后，仅发送一次
   `EnderNet:ItemCatalogRequest`。
2. 服务端通过 `GetLoadItems(True)` 枚举已加载 ID。SDK 的实际接口名是
   **GetLoadItems**，对应本需求所称的 `GetLoadedItems()`。
3. 服务端按 `Item(id).GetBasicInfo().itemCategory` 过滤掉 `""`、
   `"command"`、`"none"`；`itemCategory` 是 ToolDelta 的实际类别字段名。
   目录按 ID 排序、去重，首次请求时构建，后续请求复用本次服务缓存。
4. 服务端仅向请求事件中引擎填入的 `__id__` 回传
   `EnderNet:ItemCatalogResponse`，物品目录字段为 `item_ids`。
   同一响应也包含 `fluid_ids` 和 `bucket_fluid_ids`，供
   [流体选择窗口](select_fluid_window.md) 使用；流体按原始注册目录独立识别。
5. 客户端缓存返回的 ID，并刷新已打开窗口。目录未到达时“全部”暂为空，
   背包与网络来源仍可使用。后续筛选与选中均为客户端本地操作。

## UI 接入约定

资源定义是 `endernet_windows.select_item_window`，由 `screen.AddElement`
创建根实例 `endernet_select_item_window`，随后调用 `SetLayer(50)`。
资源已在 `_ui_defs.json` 登记。使用的相对路径：

| 路径 | 接入方式 |
| --- | --- |
| `close` | `asButton().SetCallback` 取消关闭 |
| `select_from_backpack_btn` / `select_from_net_btn` / `select_from_all_btn` | 本地切换来源 |
| 各来源按钮的 `default` | 选中时 `SetSprite(button_pressed)`，其余恢复 `background_panel_opaque` |
| 各来源按钮的 `hover` | 选中时也保持 pressed，其他恢复 `button_hovered` |
| `item_search_editor` | `$text_box_name = #EnderNetSelectItem.search`，运行时注册 `BF_EditChanged` |
| `items/grid` | 固定 40 格，collection 为 `endernet_select_items` |
| 每格 `item_renderer` / `interact_btn` | `SetUiItem(Item(id))` 渲染，点击按当前可见列表取 ID |

搜索绑定仅在窗口打开期间存在，关闭时 `DynaUnbinding`；按钮按实例直接绑定，
不占用调用方静态 Binder 名称。关闭先移除控件、解除绑定、清空回调，再调用
选择回调；标记也在回调前删除，因此回调可以立即打开新窗口。

## 验证记录

2026-09-27：`python -B endernet_tests/run_tests.py` 通过 2167 项测试，
71 个源码文件通过 Python 2.7 兼容门禁。新增测试覆盖目录过滤与收发、
只请求一次、监听注销、三来源、中文/ID 搜索、搜索后截断 40 条、按钮材质、
空结果、先关闭后回调、重复点击、重复打开返回 False 且保留原状态、创建前标记、
创建失败后重试、销毁清理标记及目录晚到刷新。

尝试通过 MCDK 读取运行时模块和 UI 列表，两次均发生 IPC 超时，未完成实机验收。
后续按用户要求不再进行游戏内验证；离线替身不代表引擎验收。
`git diff --check` 通过；环境未安装 Ruff，未执行 Ruff 检查。
