# bus_fluid_input_io.json：主动流体存货口

[返回目录](README.md) · [UI JSON](../../endernet_rp/ui/bus_fluid_input_io.json)

namespace：`bus_fluid_input_io_ui`。本文依据 2026-09-28 当前 JSON、继承的公共模板及 User 补充说明编写。UI 功能说明已填写；流体名单公共组件已实现；本设备脚本接入及游戏内验证待完成。

## 1. 制作范围与要求

主动流体存货口与 [主动物品存货口](bus_item_input_io.md) 的主界面和设置交互类似，使用一个特殊的**真实流体缓冲槽**展示设备暂存的流体。物流顺序为：**朝向的外部容器 → 本设备流体缓冲槽 → 仓储网络**。

流体槽通过重复绘制同一流体对应的物品渲染内容，再裁切可见区域表现液面高低。平铺格子不是独立业务槽，不对应多个流体或黑白名单条目。

设置窗口沿用默认黑名单及点击切换黑／白名单的交互；“修改”按钮打开 `fluid_list_window`，供玩家编辑当前模式的流体名单。不能调用 `item_list_window` 将物品名单当作流体名单。

流体容量、计量单位、转移粒度、空名单行为、名单上限、两种模式是否共享名单以及已缓冲流体受过滤变更影响的规则仍待明确，不从 UI 尺寸或平铺数量推导。

依据：[方块说明](../blocks.md)、[Task 06](../tasks/06.%20资源转移与自动输入输出服务.md)、[Task 11](../tasks/11.%20终端与客户端同步.md)。

## 2. 资源与架构

| 项目 | 当前资源 |
| --- | --- |
| 屏幕入口 | `main@common.inventory_screen_common`；`$screen_content` 为 `bus_fluid_input_io_ui.content` |
| 内容根 | `content` 为 layer 1 的 panel；含容器辅助控件及 `root_panel@common.root_panel` |
| 主面板 | `root_panel` 为 174×200；`bg` 继承 `common.dialog_background_opaque`，使用 `background_panel_opaque` 纹理 |
| 流体槽 | `fluid_slot@endernet_container_common.fluid_displayer`，本界面覆盖为 100×32，顶部偏移 [0, 50] |
| 设置窗口 | 独立模板 `bus_fluid_input_io_ui.settings_window`，140×100；当前未显式设置根 layer，且未挂入 content，需脚本创建／挂载 |
| 资源登记 | `_ui_defs.json` 已包含 `ui/bus_fluid_input_io.json`；不代表设备 Screen 已注册 |
| 公共依赖 | `common`、`netease_editor_template_namespace`、`endernet_ui_common`、`endernet_container_common` |
| 本地化 | 主标题 `tile.endernet:bus_fluid_input_io.name`，背包标题 `container.inventory`；设置及状态文字目前含直接写入的中文 |
| 交接信息 | 本次完成控件用途及交互说明；制作者、资源版本和截图未提供 |

```text
main@common.inventory_screen_common
└─ $screen_content → bus_fluid_input_io_ui.content
   ├─ flying_item_renderer / container_gamepad_helpers
   ├─ selected_item_details_factory / item_lock_notification_factory
   └─ root_panel/bg
      ├─ close
      ├─ main
      │  ├─ title/title_text
      │  ├─ stat_bar
      │  ├─ conn_band_img
      │  ├─ fluid_slot（继承 fluid_displayer）
      │  │  ├─ bg
      │  │  └─ clip_panel/grid
      │  │     └─ 重复实例：fluid_renderer
      │  ├─ outer_container_panel/{bg_frame, stat_text}
      │  ├─ baseband_img/{bg, frame}
      │  └─ settings_btn/icon
      └─ inventory

settings_window（独立模板）
├─ bg
├─ title/device_status_tip
├─ close
├─ filter_mode_tip_text
├─ filter_mode_btn/icon
├─ filter_mode_text
└─ filter_modification_btn/button_label
```

## 3. 控件功能与路径

下表 `M` 表示 `/content/root_panel/bg/main`，`B` 表示 `/content/root_panel/bg`，`S` 表示独立模板 `settings_window`。主界面按 User 提供的路径记录；继承屏幕前缀和动态设置窗口的运行时完整路径仍需接入核实。

| 控件路径 | 功能及初始状态 | 脚本职责 |
| --- | --- | --- |
| `B/close` | 18×18 的 `common.close_button` | 关闭设备界面并清理子窗口、订阅 |
| `M/title/title_text` | 显示方块本地化名称，标题背景 82×16 | 只读展示 |
| `M/stat_bar` | 公共设备状态栏，偏移 [84, 1]；继承 `stat_icon`、`stat_text`、`interact_btn` | 更新状态图标和文本；默认错误图标、“未知状态”，点击用途待接入 |
| `M/outer_container_panel/stat_text` | 初始“外部容器： 未接入”，顶部 100×20 面板 | 根据朝向外部容器接入状态刷新 |
| `M/fluid_slot` | 100×32 的内部流体缓冲槽 | 根据同一份缓冲状态刷新流体外观与液面 |
| `M/fluid_slot/bg` | `container_fluid_slot_background` 槽背景 | 空槽时仍可保留背景 |
| `M/fluid_slot/clip_panel` | clips_children=true，裁切子控件 | 控制液面高低，保持液体从槽底向上填充的效果 |
| `M/fluid_slot/clip_panel/grid` | 6×2 平铺 grid，继承 collection_name=`test_grid` | 所有渲染实例使用同一缓冲流体 ID 对应的渲染内容 |
| `M/conn_band_img` | 40×50 灰色连接示意，顶部偏移 [0, 42] | 静态装饰，尚无状态颜色／动画约定 |
| `M/baseband_img` | 100×10 网络基带示意，顶部偏移 [0, 90] | `bg` 使用 `network_baseband_8` 平铺，`frame` 为装饰 |
| `M/settings_btn` | 20×20 设置图标按钮，右上偏移 [-8, 86] | 打开本 UI 的 `settings_window` |
| `B/inventory` | 公共玩家背包及快捷栏，底部偏移 [0, -4] | 展示玩家库存，不作为设备流体存储 |
| `S/title/device_status_tip` | “存货口设置” | 静态标题 |
| `S/close` | 16×16 公共关闭按钮 | 关闭设置窗口，返回主界面 |
| `S/filter_mode_tip_text` | “过滤模式” | 固定说明文字 |
| `S/filter_mode_btn`、`S/filter_mode_btn/icon` | 16×16 模式按钮；初始 uv=[16, 0] | 切换黑／白名单，同步更新图标 |
| `S/filter_mode_text` | 初始“黑名单” | 与实际模式及图标同步 |
| `S/filter_modification_btn` | 36×16 的“修改”按钮 | 打开 `fluid_list_window` 编辑当前模式的流体名单 |

## 4. 流体缓冲槽与液面渲染

完整网格路径为 **`/content/root_panel/bg/main/fluid_slot/clip_panel/grid`**。该路径的后两级来自 `endernet_container_common.fluid_displayer`，不是本文件内重复定义的子控件。

| 模板／控件 | 当前 JSON 配置 | 含义 |
| --- | --- | --- |
| `fluid_displayer/clip_panel` | 顶部居中；offset=[0, 1]；size=["100%+-2px", "100%+-2px"]；clip_offset=[0, 0]；clips_children=true | 在本实例中形成约 98×30 的内部裁切区域，液面由裁切控制 |
| `clip_panel/grid` | 左上锚点；grid_dimensions=[6, 2]；collection_name=`test_grid` | 12 个平铺渲染位置，用于覆盖显示区域，边缘由父 panel 裁去 |
| `grid_item_template` | `endernet_container_common.fluid_renderer` | 每格复用同一渲染模板 |
| `fluid_renderer` | type=custom；renderer=`inventory_item_renderer`；size=[20, 20] | 用户所述每个 item_renderer 对应此模板的渲染实例；模板自身就是 renderer，没有额外的 `item_renderer` 子控件 |
| `fluid_renderer.property_bag` | `#item_id_aux` 初始为 131072 | 静态预览值，不是已接入的缓冲流体 ID |

渲染约定：

1. 从本设备缓冲快照取得一个当前流体 ID、当前数量和容量。流体 ID 是业务身份；用于渲染的 `#item_id_aux` 是对应的显示值，两者需要明确映射，不能假定数值相同。
2. 同一次刷新中，**grid 的所有渲染实例必须使用同一个流体 ID 对应的显示内容**。集合下标只表示平铺位置，不选择不同流体，也不映射为业务槽号。
3. 根据数量／容量确定液面比例；显示比例限制在 0–1。通过 `clip_panel` 的裁切控制可见高度，空时无液体、半满时呈下半部液体、满时填满内部可见区域。保留平铺尺寸，不通过逐格隐藏或减少网格元素来近似液面。
4. 当前模板使用顶部锚点，仅缩小 panel 高度未必得到从底部上升的液面；具体裁切 API、偏移和锚点配合需接入并验证，不能把静态 clip_offset=[0, 0] 视为已经实现液面控制。
5. 缓冲变空时清除液体显示；流体种类变化时统一刷新全部实例，避免部分格子残留旧流体。容量未知、为零、数据失效时不进行除法或保留误导性的旧液面，具体占位文案待接入确定。

本界面没有独立的流体数量 label 或精确容量 tooltip 定义，不能把图形高度当作精确数量文本。容量、单位、混合规则、手工灌入／取出权限待玩法和脚本约定。流体名单是独立配置，不占用缓冲槽；玩家背包仍继承 `inventory_items`（9×3）和 `hotbar_items`（9×1），不映射到该流体槽。

## 5. 设置与操作流程

1. 打开界面，读取设备状态、外部容器状态、流体缓冲和过滤配置；刷新所有平铺实例及裁切液面。
2. 主动吸取符合规则的流体进入本设备缓冲，再向网络输入；按实际缓冲剩余量刷新液面，不以播放效果代替真实转移结果。
3. 点击 `settings_btn` 打开本文件的设置窗口。无已有模式配置时默认黑名单；重开按保存配置恢复。
4. 点击 `filter_mode_btn` 切换模式，同时更新业务模式、图标和文字：

   | 模式 | icon 的 uv | uv_size | 模式文字 |
   | --- | --- | --- | --- |
   | 黑名单（默认） | [16, 0] | [16, 16] | 黑名单 |
   | 白名单 | [0, 0] | [16, 16] | 白名单 |

   图标纹理为 `textures/ui/endernet/whitelist_or_blacklist_icon`。黑名单排除所列流体，白名单限定允许吸取的流体；空名单语义另行确认。
5. 点击 `filter_modification_btn` 唤醒 **`fluid_list_window`**，编辑当前模式下的流体名单。列表中的身份应为流体 ID，不能用渲染格下标或物品 ID 代替。
6. 关闭名单／设置窗口返回相应父界面；名单原地修改传入 list，关闭保留修改，无取消回滚；同一 Screen 上重复打开返回 False。服务端提交时机和失败反馈仍由宿主接入确定。

设置按钮当前 `button_mappings` 为空，`$pressed_button_name` 是编辑器通用 `%netease_editor_template_namespace.click`；空 collection 的 `collection_details` 不是业务事件。按下／抬起的触发方式和触控处理待绑定时核实。

设置窗口未挂入主界面，弹窗层级、遮罩、防穿透、返回键、设备失效时的关闭、父 Screen 销毁时的清理均待实现。R12 持有物退出规则沿用项目待定约定，不自行补设。

## 6. 数据与绑定

| 数据／控件 | 来源与刷新要求 | 当前状态 |
| --- | --- | --- |
| 设备状态栏 | 设备权威状态，打开及状态变化时刷新 | 公共模板初值已有，设备绑定待接入 |
| 外部容器状态文本 | 设备朝向及容器连接信息 | 当前静态文本 |
| 全部 `fluid_renderer` 实例 | 同一缓冲流体 ID 映射到同一 `#item_id_aux` | 当前所有格继承静态 131072，无动态流体映射 |
| `clip_panel` 裁切 | 缓冲数量及容量，吸取／入网后刷新 | clips_children 已开启，液面逻辑待实现 |
| 模式文字和图标 | 保存的模式与用户切换操作 | 当前仅有默认黑名单显示 |
| `fluid_list_window` | 当前模式的流体 ID 名单与独立条目上限 | 已实现公共组件，设备调用及保存待接入 |

`test_grid` 是当前公共模板的集合名，不是已约定的设备库存接口；接入时需核对集合数量与实例绑定，并确保每个索引得到相同流体外观。设备身份、版本、快照／增量、过期响应和失败重同步尚待接入；本文不虚构实际字段名或服务端命令。

## 7. 脚本如何使用本 UI

当前客户端及 `modMain.py` 未检索到 `bus_fluid_input_io` 专用 Screen 接入。应注册主屏幕、取得流体显示路径，并统一管理缓冲渲染、设置窗口与流体名单编辑生命周期。

已有 [流体列表窗口](../technical_notes/fluid_list_window.md)，入口为 `show_fluid_list_window(screen, fluid_ids, max_fluids, title="流体列表")`，资源定义为 `endernet_windows.fluid_list_window`。`filter_modification_btn` 应传入当前模式的流体名单及已确认上限；设备按钮回调仍待接入。

组件原地增删传入 list，关闭保留修改，无取消回滚、结果回调或服务端提交；同一 Screen 重复打开返回 False。列表的添加操作调用已有 [流体选择窗口](../technical_notes/select_fluid_window.md)，背包中的桶映射为流体 ID，网络来源当前为空。名单 layer=30，选择器 layer=50，动态绑定和窗口标记独立于物品名单。

关闭设备 Screen 时应清理其拥有的弹窗、回调及订阅；名单编辑不能改变实际缓冲量。公共名单脚本与资源已完成，设备专用脚本及缓冲液面逻辑仍待实现。

## 8. 待确认项与验证记录

| 待确认／接入项 | 影响 |
| --- | --- |
| 流体容量、单位与转移粒度 | 液面比例、数量精度和物流规则 |
| 流体 ID 到 renderer 的映射 | 全部实例一致显示，移除静态预览值的业务依赖 |
| 裁切实现及重绑清理 | 液面从底部升降，空槽和流体切换无残影 |
| `fluid_list_window` 宿主接入 | 组件已实现，设备仍需接入按钮、规则上限、配置保存和销毁清理 |
| 名单及缓冲规则 | 上限、空名单、共用／分开保存、过滤变更对缓冲流体的影响、手动操作权限 |
| 宿主接入 | 注册、运行时路径、业务事件、弹窗层级与数据同步 |

- [x] 已核对 UI JSON、公共流体显示模板及用户交接的功能。
- [x] 已记录一个流体缓冲槽、统一流体 ID 的平铺渲染和裁切液面。
- [x] 已记录设置交互及 `fluid_list_window` 与已有选择器的区别。
- [ ] 脚本注册、动态绑定、液面算法和服务端同步已完成。
- [ ] 游戏内空槽、半满、满槽、流体切换及全部平铺实例一致性已验证。
- [ ] 名单修改、模式切换、关闭重开、移动端和异常状态已验证。

| 日期 | 环境与方式 | 结果 | 未验证项 |
| --- | --- | --- | --- |
| 2026-09-28 | 离线解析本 UI 和公共模板，检索流体窗口资源及客户端实现 | 已核对路径、6×2 网格、renderer 模板、裁切属性和模式初值；名单窗口缺项已记录 | 本次仅修改文档，未运行全量代码测试，未进行游戏验证 |

2026-09-28 补充：流体名单公共组件已实现，全量 2218 项离线测试及 75 文件 Python 2.7 门禁通过；设备 Screen 和游戏内验证仍待完成。
