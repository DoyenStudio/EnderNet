# assemble_chamber.json：装配仓

[返回目录](README.md) · [UI JSON](../../endernet_rp/ui/assemble_chamber.json)

对应文件：`endernet_rp/ui/assemble_chamber.json`。namespace：`assemble_chamber_ui`（从当前资源读取，不表示已交接）。

UI 交接：待填写；脚本接入：待填写；游戏验证：未进行。

> 第 2～6 节中标注「（JSON 直读）」的内容是脚本接入方按当前 JSON **静态读出**的资源现状，只描述文件里写了什么，**不等于 User 已交接**，也不等于该写法可用——本文件当前有多处未接通，见第 8 节。标注「待确认」的条目一律不改动 JSON。

## 1. 制作范围与要求

按 blocks.md 的已确认规则，使用网易容器，共 19 槽：0～8 原料、9 输出、10～18 合成样板。记录 UI 对应槽位与容器接入方式，不将其套作终端虚拟配置格。

需求依据：[Task 11](../tasks/11.%20终端与客户端同步.md)、[方块说明](../blocks.md)。具体布局和控件用途由 User 制作后填写；以下空表不是已确认的 UI 约定。

当前资源现状（JSON 直读）：界面已按 19 槽搭出骨架——3×3 原料格、1 个输出槽、9 个样板槽排成一行，另有标题、状态条、一个箭头图标和玩家背包。**槽位与容器集合尚未接通**（第 4 节），且方块侧还缺容器组件（第 8 节），因此现在既打不开也取不到物品。按 blocks.md，装配仓直接使用样板记录的配方、不做配方校验；网络调用时把原料实际放入 0～8 槽、产物进 9 槽后送回网络——这三条都是容器槽位语义，UI 侧必须与之对齐。

## 2. 资源与交接信息

| 项目 | 记录 |
| --- | --- |
| UI 制作者／交接日期／资源版本 | 待填写 |
| 本次交接范围／未交接部分 | 待填写。就资源而言界面骨架已在仓库内，但**槽位集合、进度表现、按钮映射均未接通**，不能视为已交接 |
| 根控件；独立屏幕或嵌入 panel | 独立屏幕（JSON 直读）：`main@common.inventory_screen_common`，即 `assemble_chamber_ui.main`，`$screen_content = "assemble_chamber_ui.content"`。父模板 `common.inventory_screen_common` 来自原版 `vanilla_netease/ui/ui_common.json`。文件已在 `endernet_rp/ui/_ui_defs.json` 第 3 行登记 |
| 打开入口／适用设备／父界面 | 适用设备为方块 `endernet:assemble_chamber`（`endernet_bp/blocks/endernet_assemble_chamber.json`，`endernet_rp/blocks.json` 第 2 行）。方块已声明 `netease:block_container`：`screen_name` = `assemble_chamber_ui.main`、`container_size` = 19、`custom_description` = 装配仓，并在 `description` 里带 `base_block: netease_container`（2026-09-27 补齐）。这四项由 `scripts/gen_blocks.py` 从 `CONTAINERS` 与定义表的 `ASSEMBLE_CHAMBER_SLOT_COUNT` 生成——**格数只写一处**，界面画 19 格、方块容器开 19 格、领域层认 19 格三者同源 |
| 公共模板、纹理、本地化键与登记情况 | 见下方资源清单（JSON 直读） |
| 截图或预览路径（可选） | 待填写 |

资源清单（JSON 直读）：

| 类别 | 内容 |
| --- | --- |
| 公共模板 | 原版 `vanilla_netease/ui/ui_common.json`：`common.root_panel`、`common.dialog_background_opaque`、`common.close_button`、`common.inventory_screen_common`、`common.flying_item_renderer`、`common.container_gamepad_helpers`、`common.selected_item_details_factory`、`common.item_lock_notification_factory`。本仓库：`endernet_container_common.container_grid_item`、`endernet_container_common.inventory`（`endernet_rp/ui/endernet_container_common.json`）、`endernet_ui_common.device_stat_bar`（`endernet_rp/ui/endernet_ui_common.json`） |
| 纹理 | `textures/ui/endernet/device_title_background`、`textures/ui/endernet/background_panel_opaque`（本仓库，文件存在）；`textures/ui/arrow_active`、`textures/ui/arrow_inactive`（原版路径） |
| 本地化键 | `tile.endernet:assemble_chamber.name` = 装配仓（`endernet_rp/texts/zh_CN.lang` 第 21 行） |
| 登记 | `endernet_rp/ui/_ui_defs.json` 第 3 行 `"ui/assemble_chamber.json"` |

纹理冗余提示（JSON 直读）：`endernet_rp/textures/ui/` 下的 `arrow.png`、`arrow_active.png`、`arrow_inactive.png` 与 vanilla 原文件**逐字节相同**，属同名同内容的冗余副本（不改变外观，可删）。这是脚本侧静态比对结果，不是制作方的取舍记录。

## 3. 控件功能与路径

定义位置指 JSON 里的书写位置；运行时完整路径按屏幕根 `assemble_chamber_ui.main` 展开，**均未做游戏内核实**，统一标「待核实」。

| 控件／模板 | 定义位置 | 运行时完整路径（待核实） | 类型与功能 | 脚本读取／更新内容 | 初始状态及显隐／禁用条件 |
| --- | --- | --- | --- | --- | --- |
| `content` | 本文件顶层 | `main/content` | `panel`，layer 1，容器屏幕的实际内容根 | 无 | 无覆盖 |
| `flying_item_renderer` | 继承 `common.flying_item_renderer` | `main/content/flying_item_renderer` | `custom`，layer 11，size [0,0] | 原版接管，脚本不碰 | 原版默认 |
| `container_gamepad_helpers` | 继承 `common.container_gamepad_helpers` | `main/content/container_gamepad_helpers` | `stack_panel`，layer 3，原版手柄辅助 | 原版接管 | 原版默认 |
| `selected_item_details_factory` | 继承 `common.selected_item_details_factory` | `main/content/selected_item_details_factory` | `factory`，layer 4，size [0,0]，原版选中物详情 | 原版接管 | 原版默认 |
| `item_lock_notification_factory` | 继承 `common.item_lock_notification_factory` | `main/content/item_lock_notification_factory` | `factory`，size [0,0]，原版物品锁定提示 | 原版接管 | 原版默认 |
| `root_panel` | 继承 `common.root_panel` | `main/content/root_panel` | `input_panel`，layer 0，size [174,200] | 无 | 无覆盖 |
| `bg` | 继承 `common.dialog_background_opaque` | `main/content/root_panel/bg` | `image`，layer 0，texture 覆盖为 `background_panel_opaque`；对话框底板 | 无 | 无覆盖 |
| `close` | 继承 `common.close_button` | `main/content/root_panel/bg/close` | `button`，size [18,18]；关闭界面。按下名由原版模板提供 | 无 | 原版默认 |
| `main` | 本文件内联 | `main/content/root_panel/bg/main` | `panel`，无 anchor／size（按原版默认 `anchor_from`/`anchor_to` = `center`、size = `100%`，等效铺满 `bg`）；主内容区 | 无 | 无覆盖 |
| `title` | 本文件内联 | `.../bg/main/title` | `image`，top_left，size [50,16]，layer 1，texture `device_title_background`；标题底板 | 无 | 无覆盖 |
| `title_text` | 本文件内联 | `.../bg/main/title/title_text` | `label`，top_left，offset [3,3]，`font_scale_factor` 1.2，颜色 ≈ (0.306, 0.255, 0.314)，`text` = `tile.endernet:assemble_chamber.name`，居中；标题文字 | 无（文本写死为本地化键，未设 `localize`，按原版默认显示） | 无覆盖 |
| `stat_bar` | 继承 `endernet_ui_common.device_stat_bar` | `.../bg/main/stat_bar` | `panel`，只覆盖 `offset [84,1]`；设备状态条，模板内含 `stat_icon`(image) / `stat_text`(label，初值「未知状态」) / `interact_btn`(button，layer 2)；模板本身 size [64,16]、top_left | 状态与详情入口，字段见 [endernet_ui_common.md](endernet_ui_common.md) | 无覆盖；模板内 `interact_btn` 的 `$pressed_button_name` 仍是编辑器占位 `%netease_editor_template_namespace.click` |
| `source_items_grid` | 本文件内联 | `.../bg/main/source_items_grid` | `grid`，top_left，offset [32,24]，layer 1，`collection_name` = `source_items`，`grid_dimensions` [3,3]，`grid_item_template` = `endernet_container_common.container_grid_item`；原料 3×3 | 待确认（集合名无生产者，见第 4 节／第 8 节） | 无覆盖 |
| `output_item` | 本文件内联 | `.../bg/main/output_item` | `stack_panel`，top_left，offset [118,42]，size `100%cm`，layer 1，`collection_index` = 9，子控件 1 个 `slot@endernet_container_common.container_grid_item`；输出槽 | 待确认（无祖先声明 `collection_name`） | 无覆盖 |
| `progress` | 本文件内联 | `.../bg/main/progress` | `panel`，top_left，offset [88,40]，size [26,20]，layer 1；合成进度箭头，结构照原版熔炉（`vanilla/ui/furnace_screen.json` 的 `furnace_arrow_*_image`） | `mask` 的裁剪比例由脚本设置，见第 6 节 | 无覆盖 |
| `progress/bg` | 本文件内联 | `.../bg/main/progress/bg` | `image`，layer 1，texture `textures/ui/arrow_inactive`；未完成部分的底图 | 无 | 无覆盖 |
| `progress/mask` | 本文件内联 | `.../bg/main/progress/mask` | `image`，layer 1，texture `textures/ui/arrow_active`，`clip_direction` = `left`（2026-09-27 补）；已完成部分的覆盖层，靠裁剪比例表现进度。本文件**不写绑定**，比例由脚本在运行时设置 | `SetSpriteClipRatio`，见第 6 节 | 无覆盖 |
| `inventory` | 继承 `endernet_container_common.inventory` | `.../bg/inventory` | `panel`，bottom_middle，offset [0,-4]；玩家背包（模板内为 `inventory_panel` 的 9×3 `inventory_grid`，集合 `inventory_items`） | 原版／公共模板接管 | 无覆盖 |
| `templates` | 本文件内联 | `main/content/root_panel/templates` | `panel`，top_middle，offset [0,86]，size [162,18]，layer 1；9 个样板槽的容器面板 | 无 | 无覆盖 |
| `slot10`～`slot18` | 本文件内联（9 个同构控件） | `main/content/root_panel/templates/slot10` … `/slot18` | `stack_panel`，size `100%cm`，layer 1，`collection_index` = 10…18，各含 1 个 `slot@endernet_container_common.container_grid_item`；合成样板 9 格。**`slot10` 未写 `anchor_from`/`anchor_to`，`slot11`～`slot18` 写了 `top_left`** | 待确认（无祖先声明 `collection_name`） | 无覆盖 |
| `slot`（每槽子控件） | 继承 `endernet_container_common.container_grid_item` | `.../templates/slotNN/slot` | `input_panel`，物品格本体；模板只覆盖 `$item_collection_name = "netease_container"` | 由公共模板的 collection 绑定决定 | 见 [endernet_container_common.md](endernet_container_common.md) |

装饰性控件（`title`／`progress` 的两张图）已按上表合并记录，不再单列。

## 4. 槽位、列表及组件复用

| 槽位组／组件 | 实例路径及引用模板 | 性质与数据来源 | 数量／索引范围 | UI 索引到业务身份的映射 | 限制、单位和显示规则 |
| --- | --- | --- | --- | --- | --- |
| 原料槽 | `content/root_panel/bg/main/source_items_grid`（`grid`，`grid_item_template` = `endernet_container_common.container_grid_item`） | 取自集合 `source_items`；该集合名在本仓库内无生产者（唯一未决项，见第 8 节） | 3×3 = 9 格，索引由 grid 自动分配 0～8 | 按 blocks.md：容器槽 0～8 = 原料 | 单格尺寸由 `container_item`（18×18）决定；格内图标／数量表现沿用公共模板 |
| 输出槽 | `content/root_panel/bg/main/output_item`（`stack_panel`）→ 子 `slot@endernet_container_common.container_grid_item` | `collection_index` = 9。索引写在外层 `stack_panel` 上、不另写 `collection_name`——**该写法经 User 于 2026-09-27 确认为可用**（游戏内已验证） | 1 格，索引 9 | 按 blocks.md：容器槽 9 = 合成产物，产物直接送回网络 | 同上 |
| 合成样板槽 | `content/root_panel/templates/slot10` … `slot18`（9 个 `stack_panel`）→ 各含 1 个 `slot@…container_grid_item` | `collection_index` = 10…18，写法同上，已确认可用 | 9 格，索引 10～18 | 按 blocks.md：容器槽 10～18 = 合成样板 1～9 | 同上；横向排列，x 偏移 0/18/36/…/144 |
| 玩家背包 | `content/root_panel/bg/inventory@endernet_container_common.inventory` | 公共模板内的 `inventory_grid`，集合 `inventory_items` | 9×3 | 玩家背包槽位，非本设备业务槽 | 见 [endernet_container_common.md](endernet_container_common.md) |

- 模板参数、collection 名称及索引含义：`container_grid_item@endernet_container_common.container_item` 只覆盖一个变量 `$item_collection_name = "netease_container"`；`container_item` 本体把该变量用作 `bindings[].binding_collection_name`（`binding_type: "collection"`）。
- 索引写法（2026-09-27 澄清）：本文件把 `collection_index` 写在外层 `stack_panel` 上、**不写 `collection_name`**，这与原版参照 `endernet_rp/ui/NeteaseContainerUI.json` 的 `test_collection`（父 `row_1` 写 `collection_name`、子项 `a@…custom_grid_item` 写 `collection_index`）写法不同，但**该写法经 User 确认为可用且已在游戏内验证**，不是缺陷。输出槽与 9 个样板槽都走这一套。
- 唯一未决的是**原料格那一组**：`source_items_grid` 是 `grid`，必须有显式 `collection_name`，而它写的 `source_items` 在仓库内没有生产者（其余容器界面用的都是 `netease_container`）。若该名字是有意的，需说明由谁提供这份集合；若是笔误，则应改为 `netease_container` 并去掉一层 3×3 的网格维度假设。**在 User 确认前不改动 JSON**，见第 8 节。
- 图标、数量缩写、精确提示和选中态位置：本文件未覆盖，全部由 `container_item` 公共模板提供，见 [endernet_container_common.md](endernet_container_common.md)。
- 滚动复用、搜索／排序、重绑时旧状态清理：不适用——本界面是固定 19 格容器，没有长列表、搜索和排序。
- 空槽、空列表、加载中和数据失效的表现：本文件未定义，沿用 `container_item` 公共模板的默认表现；若按现状运行，由于上述集合问题，**所有格子都会是空的**，这正是第 8 节第 1 条要澄清的问题。
- 公共组件文档及本界面实例路径、覆盖参数：[endernet_container_common.md](endernet_container_common.md)（`container_grid_item`、`inventory`）、[endernet_ui_common.md](endernet_ui_common.md)（`device_stat_bar`）。本界面的覆盖仅两处：`stat_bar` 覆盖 `offset [84,1]`，`bg` 覆盖 `texture`。

## 5. 操作流程与事件

| 控件路径 | 鼠标／触控操作与触发时机 | 动作及参数含义 | 等待回执时的表现 | 成功／失败后的表现 |
| --- | --- | --- | --- | --- |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |

- 本文件**没有定义任何按钮映射或事件名**（静态检查：`button_mappings`／`$pressed_button_name`／`play_event` 在本文件内均无出现）。现有可交互控件只有两个，且都由公共模板提供：`bg/close@common.close_button`（关闭），以及状态条内的 `interact_btn@common.button`——后者在模板里的按下名仍是编辑器占位 `%netease_editor_template_namespace.click`，**其真实用途待 User 确认**。
- 初次打开、选择／取消、提交修改的流程：待填写。
- 弹窗层级、防穿透、重复点击和关闭范围：待填写。
- 离线、拆除／替换设备、交互距离失效的处理：待填写。
- 关闭、系统返回、失活、断线与持有物处理：待填写。
- R12 未定的键位、触控和持有物退出规则：注明待定，不自行补设。

## 6. 数据与绑定

| 控件路径 | 字段含义、类型、单位与范围 | 来源与刷新时机 | JSON 绑定名／事件名 | Python 方法 | 接入状态 |
| --- | --- | --- | --- | --- | --- |
| `content/root_panel/bg/main/progress/mask` | 合成进度，0.0～1.0 的**裁剪**比例。注意它是「裁掉多少」而不是「显示多少」：要显示进度 p 应传 `1 - p`（与 ModSDK `SetSpriteClipRatio` 文档示例一致） | 装配仓当前加工任务的进度 | 本文件不写绑定，比例由脚本在运行时直接设属性（2026-09-27 决定） | 待接入。机制是 `ImageUIControl.SetSpriteClipRatio`；本仓库既有用法见 [client/ui/terminal_panel.py](../../endernet_bp/endernet_scripts/endernet/client/ui/terminal_panel.py)（经 tooldelta 包装 [tooldelta/ui/elem_comp.py](../../endernet_bp/endernet_scripts/tooldelta/ui/elem_comp.py)，包装会一并设置裁剪方向）。装配仓侧尚未接线，**方向串待实测后填** | 待接入 |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |

- 本文件**自身没有任何 `bindings`**（静态检查：`bindings` 键在本文件内不出现）；现有绑定全部来自公共模板（`container_item` 的集合可见性／物品渲染、`device_stat_bar` 的 `interact_btn`）。
- 已有按钮映射及按下／抬起触发约定：本文件未定义，见第 5 节。
- 只读展示与提交服务端命令的操作区分：待填写。
- 请求使用的设备／资源／槽位身份和版本来源：待填写（原料格的集合归属还卡在第 8 节第 1 条）。
- 快照、增量、过期响应、失败及重新同步：待填写，按 Task 11 的同步通道填写。
- 本地化、长文本换行、数量精度：标题走本地化键 `tile.endernet:assemble_chamber.name`（`zh_CN.lang` 已有）；其余待填写。

## 7. 脚本如何使用本 UI

交接明确后由脚本接入方补充真实接口；未实现项写“待接入”，不提供虚构 API。

| 项目 | 使用说明 |
| --- | --- |
| Screen 文件、类名、注册入口，或公共组件的接入方式 | 待接入 |
| namespace／根控件、打开方法与调用时机 | 待接入 |
| 参数字段、类型、必填项及可信来源 | 待接入 |
| 初始化、读取快照及订阅入口 | 待接入 |
| 更新文本、图标、列表、槽位及状态的方法 | 待接入 |
| 事件回调与服务端命令的调用位置 | 待接入 |
| 关闭／销毁后的订阅、回调与缓存清理 | 待接入 |
| 公共组件复用方法与允许覆盖的配置 | 待接入 |

实际调用示例：待接入后填写执行端、导入路径、前置条件和真实参数。演示入口必须注明模拟用途。

## 8. 待确认项与验证记录

| 缺项／问题 | 影响范围 | 需求依据 | 负责人 | 结论／日期 |
| --- | --- | --- | --- | --- |
| `source_items_grid` 的 `collection_name` = `source_items`，该名字在仓库内没有任何生产者（其余容器界面一律用 `netease_container`）。它是 `grid`，必须有显式集合名，不能像 `collection_index` 那样省略 | 第 3／4／6 节，原料 9 格 | blocks.md「使用网易容器，共 19 槽（槽号从 0 开始）：0～8 合成原料槽…」 | User | **待定** |
| 索引写在外层 `stack_panel` 上、不另写 `collection_name`（`output_item` 与 `slot10`～`slot18`），与原版 `NeteaseContainerUI.json` 的把集合名写父级、索引写物品控件不同 | 第 3／4 节，输出槽与 9 个样板槽 | — | User | 2026-09-27 已确认：**可用写法，游戏内已验证**，不改动 |
| 方块缺 `netease:block_container`（`gen_blocks.CONTAINERS` 里也没有它），界面在游戏内无打开入口 | 第 2 节「打开入口」，及第 5／7 节 | 方块说明与设备容器接入方式 | 脚本接入方 | 2026-09-27 已补齐：`CONTAINERS` 增列 `endernet:assemble_chamber` → `assemble_chamber_ui.main`，槽数取自定义表新增的 `ASSEMBLE_CHAMBER_SLOT_COUNT`（19），并重新生成方块 JSON（`--check` 无差异） |
| `slot10` 未写 `anchor_from`/`anchor_to`，而 `slot11`～`slot18` 都写了 `top_left` | 第 3／4 节样板槽 | — | User | 2026-09-27：**不改**，User 确认当前显示正常 |
| `progress/mask` 缺 `clip_direction`，且没有绑定／比例，`bg` 被完整盖住，不构成进度条 | 第 3／6 节 | — | User／脚本接入方 | 2026-09-27 已定：JSON 侧补 `clip_direction: "left"`（对齐原版熔炉 `furnace_arrow_full_image`）；**裁剪比例由脚本层控制**，本文件不写绑定，接线状态见第 6 节 |
| `device_stat_bar` 内 `interact_btn` 的 `$pressed_button_name` 仍是编辑器占位 `%netease_editor_template_namespace.click`，未指定真实按钮名 | 第 5 节交互、第 7 节事件回调 | [公共控件](endernet_ui_common.md) | User | **待定** |
| `endernet_rp/textures/ui/` 下的 `arrow.png`／`arrow_active.png`／`arrow_inactive.png` 与 vanilla 逐字节相同（冗余副本，不改变外观） | 资源包体积 | — | User | **待定**：保留或删除 |

- [ ] User 已交接资源、控件用途、槽位映射和操作流程。
- [ ] 脚本接入方已核对路径和约定，记录所有缺项。
- [ ] 注册、绑定、事件、同步和实际调用说明已完成。
- [ ] 离线检查完成，结果已记录。
- [ ] 游戏内打开、操作、更新、关闭及重开已验证。
- [ ] 移动端、长文本、大数量和异常状态已验证（按适用范围）。

| 日期／验证人 | 环境与方式 | 覆盖范围 | 结果／证据 | 未验证项 |
| --- | --- | --- | --- | --- |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |

静态核对记录（非游戏验证，不计入上方复选框）：`endernet_rp/ui/assemble_chamber.json` 可解析，已在 `_ui_defs.json` 登记；所有继承引用均可解析到本仓库或原版 `ui_common.json`；4 个纹理所指文件存在；本地化键 `tile.endernet:assemble_chamber.name` 在 `zh_CN.lang` 存在。2026-09-27 的改动（补 `netease:block_container`、补 `clip_direction`）之后复跑 `python -B endernet_tests/run_tests.py` 全通过（2158 项，含 Python 2.7 语法门禁），`python -B scripts/gen_blocks.py --check` 无差异。以上仅为文件级检查，**未在游戏内打开过本界面**。
