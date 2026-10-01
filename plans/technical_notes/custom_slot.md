# custom_slot 脚本层使用说明

> **本模板已不再被终端面板使用**（2026-09-30）。通用终端上下两区的格子都改由
> 引擎的集合绑定驱动，走共享模板 `endernet_windows.item_can_be_selected`
> （见 [终端背包 UI](terminal_inventory_ui.md)）。原因不是风格：grid 的格子要到
> **渲染**时才生成、滚动时还会被回收换出，按控件路径取到的引用会过期。
> 文中那个示例类 `CustomSlotView` **曾是**生产代码（`client/ui/terminal_panel.py`），
> 现已删除——**它在这里只是写法示例，不要当成现成接口**。
> 本模板仍被 `crafting_terminal_panel.json`、`template_terminal_panel.json` 与
> `endernet_ui_common.json` 引用，下面的控件接口说明仍然有效。

## 定义与职责

控件定义：`endernet_rp/ui/endernet_container_common.json` 中的 `endernet_container_common.custom_slot`。该文件已在 `_ui_defs.json` 注册。

槽位大小为 18×18，物品渲染区域为 16×16。布局参考原版 `common.grid_item_for_inventory`，使用模组槽位底图和选中框，但没有原版背包集合绑定。所有显示状态由客户端 Python 主动更新。

它是展示用 `panel`，不会自动同步背包、处理点击、拖拽或物品转移。锁图标只是显示状态，不会阻止操作；交互和服务端校验需要调用方实现。

## 子控件路径

以下路径均相对于槽位实例，不含 JSON 定义中的 `@继承名`。

| 路径 | ToolDelta 类型转换 | 用途与默认状态 |
| --- | --- | --- |
| `background` | `asImage()` | 槽位底图 |
| `item_renderer` | `asItemRenderer()` | 物品渲染，初始物品 ID 为 0 |
| `stack_count_label` | `asLabel()` | 数量文字，初始为空 |
| `durability_bar` | 无需转换 | 耐久条整体，默认隐藏 |
| `durability_bar/background` | `asImage()` | 13×2 黑色底图 |
| `durability_bar/fill` | `asImage()` | 12×1 彩色填充，初始绿色、无裁切 |
| `item_lock` | `asImage()` | 原版黄色锁标志，默认隐藏 |
| `item_selected_image` | `asImage()` | 选中框，默认隐藏 |

## 创建或获取槽位

以下 `self` 均指已经完成 UI 创建、可以访问控件的 `ToolDeltaScreen` 实例。示例中的 `/main_panel/slots` 是占位路径，需替换为实际父容器路径。

动态创建：

```python
parent = self.GetElement("/main_panel/slots")
slot = parent.AddElement(
    "endernet_container_common.custom_slot", "slot_0"
)
slot.SetAnchorFrom("top_left")
slot.SetAnchorTo("top_left")
slot.SetPos((0, 0))
```

也可以将下面的条目放进父容器的 JSON `controls` 数组：

```json
{
    "slot_0@endernet_container_common.custom_slot": {
        "anchor_from": "top_left",
        "anchor_to": "top_left",
        "offset": [0, 0]
    }
}
```

随后获取已有实例，不要再次动态创建同名控件：

```python
slot = self.GetElement("/main_panel/slots/slot_0")
```

## 完整刷新示例

下面的辅助类是可复制到客户端 UI 代码中的示例，尚未作为项目运行时代码提供。它缓存子控件引用，并在每次刷新时覆盖物品、数量、耐久、锁定及选中状态，避免复用槽位后残留旧状态。

```python
# coding=utf-8
from tooldelta.define import Item


class CustomSlotView(object):
    def __init__(self, slot):
        self.slot = slot
        self.item = slot.GetElement("item_renderer").asItemRenderer()
        self.count = slot.GetElement("stack_count_label").asLabel()
        self.bar = slot.GetElement("durability_bar")
        self.fill = slot.GetElement("durability_bar/fill").asImage()
        self.lock = slot.GetElement("item_lock").asImage()
        self.selected = slot.GetElement("item_selected_image")
        self.refresh(None)

    def refresh(self, item, remaining=None, maximum=None,
                locked=False, selected=False):
        # item: ToolDelta Item；None 或数量 <= 0 表示空槽。
        # remaining: 剩余耐久；maximum: 最大耐久。
        has_item = (
            item is not None
            and item.count > 0
            and item.newItemName != "minecraft:air"
        )
        self.item.SetVisible(False)
        self.count.SetText("")
        self.bar.SetVisible(False)
        self.fill.SetSpriteClipRatio("fromRightToLeft", 0.0)
        self.fill.SetSpriteColor((0.0, 1.0, 0.0))
        self.lock.SetVisible(bool(locked))
        self.selected.SetVisible(bool(selected))

        if not has_item:
            return

        if not self.item.SetUiItem(item):
            return
        self.item.SetVisible(True)
        self.count.SetText(str(item.count) if item.count > 1 else "")

        if remaining is None or maximum is None or maximum <= 0:
            return
        ratio = max(0.0, min(1.0, float(remaining) / float(maximum)))
        # 裁切比例是被隐藏的比例，从右侧裁切，保留左侧填充。
        self.fill.SetSpriteClipRatio("fromRightToLeft", 1.0 - ratio)
        # 示例配色：红 -> 黄 -> 绿，并非原版渲染器的精确配色公式。
        self.fill.SetSpriteColor((
            min(1.0, 2.0 * (1.0 - ratio)),
            min(1.0, 2.0 * ratio),
            0.0,
        ))
        # 本示例采用满耐久隐藏、受损后显示的策略。
        self.bar.SetVisible(ratio < 1.0)


view = CustomSlotView(slot)

# 普通堆叠物品：显示物品与数量，不显示耐久。
view.refresh(Item("minecraft:stone", count=64))

# 耐久值由业务层提供，这里使用演示数值。
view.refresh(
    Item("minecraft:diamond_pickaxe"),
    remaining=750, maximum=1500,
    locked=True, selected=True,
)

# 清空显示，同时取消锁定与选中。
view.refresh(None)
```

`UItemRenderer.SetUiItem` 接收 `Item` 对象，内部传递物品名、附加值、附魔状态和 `userData`。它不会设置数量标签或本控件的耐久条；只修改 `Item.count`、`Item.durability` 也不会自动刷新 UI，需要再次调用刷新函数。

`remaining` 必须是剩余耐久。如果数据源提供的是已损耗值，应先计算 `maximum - damage`。无耐久物品不传这两个参数。不要将 `newAuxValue` 直接当成剩余耐久。

## 独立修改状态

```python
# 锁定与选中可以独立于物品显示控制，包括空槽。
slot.GetElement("item_lock").SetVisible(True)
slot.GetElement("item_selected_image").SetVisible(True)

# 如需红锁，修改贴图；恢复黄色时也应显式设置贴图。
lock = slot.GetElement("item_lock").asImage()
lock.SetSprite("textures/ui/item_lock_red")
lock.SetSprite("textures/ui/item_lock_yellow")

# 手动显示半条耐久；颜色需按业务需求另行设置。
slot.GetElement("durability_bar/fill").asImage().SetSpriteClipRatio(
    "fromRightToLeft", 0.5
)
slot.GetElement("durability_bar").SetVisible(True)
```

JSON 的 `clip_direction` 使用 `left` 等 JSON UI 值；Python 的 `SetSpriteClipRatio` 使用 `fromRightToLeft` 等接口枚举，两者不要混用。此处 ToolDelta 包装函数接受两个参数：方向、裁切比例。

## 生命周期与验收

在 UI 创建完成后缓存 `CustomSlotView`，收到业务数据更新时调用 `refresh`，无需每帧重复查找控件。动态创建时，多个槽位使用不同的实例名，并以 18 像素为基础间隔排列。移除动态槽位可调用 `slot.Remove()`；控件或界面销毁后应丢弃旧引用，重新创建界面时重新获取。

本说明依据项目的 `tooldelta/ui/elem_comp.py`、`tooldelta/ui/general_screen.py` 和 `tooldelta/define/item.py` 编写。尚未完成游戏内视觉验收，接入后应检查：空槽、数量 1/64、附魔物品、满/半/零耐久、黄/红锁、选中框，以及从有物品切换为空槽时是否残留显示。
