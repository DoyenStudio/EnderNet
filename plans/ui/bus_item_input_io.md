# bus_item_input_io.json：主动物品存货口

[返回目录](README.md) · [UI JSON](../../endernet_rp/ui/bus_item_input_io.json)

namespace：`bus_item_input_io_ui`。本文依据 2026-09-27 当前 JSON、公共模板及 User 补充说明编写。UI 功能说明已填写；设备专用脚本接入及游戏验证待完成。

## 1. 制作范围与要求

主动物品存货口保留 **5 个内部真实物品槽作为缓冲槽**。物流过程为：**朝向的外部容器 → 内部缓冲槽 → 仓储网络**。设备先主动吸取符合过滤规则的物品到缓冲槽，再将缓冲物品输入网络；不能将界面中的 5 格视为虚拟过滤格或外部容器槽。

主界面展示设备状态、外部容器连接状态、缓冲库存和玩家背包；设置窗口提供黑白名单模式切换及名单编辑。

- 默认黑名单模式：名单用于排除主动吸取的物品；白名单用于限定允许主动吸取的物品。
- 设置按钮打开本文件的 `settings_window`。
- 模式按钮每次点击切换黑／白名单，同时更新图标与文字。
- “修改”按钮调用 `show_item_list_window` 编辑当前过滤模式的物品列表。
- 名单条目上限与缓冲槽数量是两个独立概念，不从 5 格推导名单上限。
- 空名单行为、两种模式共用还是分别保存名单、缓冲槽手动存取权限，以及修改过滤规则对已缓冲物品的影响仍待确认。

依据：[方块说明](../blocks.md)、[Task 06](../tasks/06.%20资源转移与自动输入输出服务.md)、[Task 11](../tasks/11.%20终端与客户端同步.md)。本次确认的是物品存货口的缓冲规则，不据此改变流体存货口规则。

## 2. 资源与架构

| 项目 | 当前结构 |
| --- | --- |
| 屏幕入口 | `main@common.inventory_screen_common`，`$screen_content` 指向 `bus_item_input_io_ui.content` |
| 内容根 | `content` 为 layer 1 的 panel，包含容器辅助控件和 `root_panel@common.root_panel` |
| 主面板 | `root_panel` 为 174×200；`bg` 继承 `common.dialog_background_opaque`，背景为 `textures/ui/endernet/background_panel_opaque` |
| 设置窗口 | 独立模板 `bus_item_input_io_ui.settings_window`，140×100，JSON layer=-2；未挂入 `content.controls`，需脚本创建或挂载 |
| 打开入口 | 与主动物品存货口交互时打开；具体 Screen 注册及打开方法待接入 |
| 资源登记 | `_ui_defs.json` 已登记 `ui/bus_item_input_io.json`；不代表 Python Screen 已注册 |
| 公共依赖 | `common`、`netease_editor_template_namespace`、`endernet_ui_common`、`endernet_container_common`；名单编辑复用 `endernet_windows` 和已有列表组件 |
| 本地化 | 主标题为 `tile.endernet:bus_item_input_io.name`；背包标题继承 `container.inventory`；设置及状态存在直接写入的中文 |
| 交接信息 | 本次完成结构和功能说明；资源制作者、版本及截图未提供 |

```text
main@common.inventory_screen_common
└─ $screen_content → bus_item_input_io_ui.content
   ├─ flying_item_renderer / container_gamepad_helpers
   ├─ selected_item_details_factory / item_lock_notification_factory
   └─ root_panel/bg
      ├─ close
      ├─ main
      │  ├─ title/title_text
      │  ├─ stat_bar
      │  ├─ container_grid（5 个内部缓冲槽）
      │  ├─ baseband_img/{bg, frame}
      │  ├─ conn_band_img
      │  ├─ outer_container_panel/{bg_frame, stat_text}
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

下表 `M` 表示定义路径 `content/root_panel/bg/main`，`B` 表示 `content/root_panel/bg`，`S` 表示独立模板 `settings_window`。这些是 JSON 定义路径缩写；继承屏幕生成的运行时前缀及动态窗口完整路径待接入核实，不能直接当作已经验证的绝对路径。

| 控件／定义路径 | 类型与功能 | 初始值及脚本职责 |
| --- | --- | --- |
| `B/close` | `common.close_button`，18×18 | 关闭设备主界面；继承事件与销毁行为待核对 |
| `M/title/title_text` | 标题 label，背景 82×16 | 显示方块本地化名称 |
| `M/stat_bar` | `endernet_ui_common.device_stat_bar`，偏移 [84, 1] | 继承 `stat_icon`、`stat_text`、`interact_btn`；默认错误图标及“未知状态”，状态刷新及点击用途待接入 |
| `M/container_grid` | 物品 grid，5×1，中心偏移 [0, -32] | 显示本设备真实缓冲库存；吸取和输入网络后刷新 |
| `M/outer_container_panel/stat_text` | 外部容器状态 label，面板 100×20，顶部偏移 [0, 24] | 初始“外部容器： 未接入”；按朝向容器接入状态刷新，其他文案待明确 |
| `M/baseband_img` | 网络基带示意 panel，100×10，顶部偏移 [0, 90] | `bg` 为 `network_baseband_8` 平铺纹理，`frame` 为装饰；无动态绑定 |
| `M/conn_band_img` | 连接示意 image，40×50，顶部偏移 [0, 42] | 灰色 `white_background`；未声明动态颜色或动画 |
| `M/settings_btn` | 设置按钮，20×20，右上偏移 [-8, 86] | 图标为 `settings_icon_light`；点击打开本 UI 的设置窗口 |
| `B/inventory` | 玩家背包组件，底部偏移 [0, -4] | 展示背包及快捷栏，与设备缓冲库存分开 |
| `S/bg`、`S/title` | 设置背景及标题条 | 标题子控件 `device_status_tip` 的文字为“存货口设置” |
| `S/close` | `endernet_ui_common.close_btn`，16×16 | 关闭设置窗口，返回主界面；实例回调待接入 |
| `S/filter_mode_tip_text` | 固定 label，偏移 [8, 24] | “过滤模式” |
| `S/filter_mode_btn` | 模式按钮，16×16，偏移 [52, 22] | 每次点击切换黑／白名单 |
| `S/filter_mode_btn/icon` | 模式图标 image | 纹理 `textures/ui/endernet/whitelist_or_blacklist_icon`；初始 uv=[16, 0]，uv_size=[16, 16] |
| `S/filter_mode_text` | 模式 label，偏移 [72, 24] | 初始“黑名单”；切换时同步文字 |
| `S/filter_modification_btn` | “修改”按钮，36×16，右上偏移 [-4, 22] | 调用 `show_item_list_window`，编辑当前模式对应的名单 |

补充说明中的 `setting_btn` 对应当前 JSON 的 `settings_btn@common.button`。重新保存的 JSON 已将“修改”按钮命名为 `filter_modification_btn@common.button`，接入应使用上述最新名称。

## 4. 槽位、列表及组件复用

| 组件 | 模板／collection | 数量与身份 | 显示与操作约定 |
| --- | --- | --- | --- |
| `M/container_grid` | `endernet_container_common.container_grid_items`；`netease_container` | 5 个内部真实缓冲槽；业务槽号 0–4，UI 集合映射待接入验证 | 展示吸取后、尚未输入网络的实际物品；不是过滤条目，手动存取权限待定 |
| `B/inventory/inventory/inventory_grid` | `endernet_container_common.inventory_panel` 内的 grid；`inventory_items` | 9×3，集合内布局索引 0–26 | 玩家背包，实际槽身份按引擎集合处理 |
| `B/inventory/hotbar` | `endernet_container_common.hotbar_grid_template`；`hotbar_items` | 9×1，集合内布局索引 0–8 | 玩家快捷栏，不与设备缓冲槽共用编号语义 |
| 过滤名单 | `endernet_windows.item_list_window`；`endernet_item_list` | 数量由传入 list 和独立上限决定 | 虚拟配置列表，仅保存物品 identifier；不消耗、存储或移动真实物品 |

**资源接入注意：**当前 `container_grid` 的 grid_dimensions 是 [5, 1]，仍继承公共模板的 maximum_grid_items=27。业务容量已明确为 **5**，接入时需使网格数量、集合数据和槽位映射都严格对应 5 格，不能将继承值解释为 27 格缓冲容量。

> **2026-09-29 更正。** 本节原先写 [9, 1] 与「业务容量已明确为 9」，而 JSON 里一直是 [5, 1]——**文档与 JSON 分叉了，且当时的离线断言只查「界面画没画这个集合」、不查格数，所以门禁没有拦住**。User 当天定以界面为准，本文与 [blocks.md](../blocks.md) 一并改为 5；方块侧也补上了网易容器（`container_size` 与领域层槽数同源于 `common/definitions.py` 的 `ITEM_INTAKE_PORT_SLOT_COUNT`）。

缓冲槽图标、数量、选中态、提示及容器交互继承公共物品槽模板；实际库存应由权威快照和更新驱动，不用过滤名单渲染缓冲槽。不得仅因渲染模板具有容器操作能力就视为已确定手动存取规则。

名单组件详见 [物品列表窗口](../technical_notes/item_list_window.md)：每行 8 格，可滚动展示全部已有项；初始无选中项。添加调用物品选择窗口，相同 identifier 不重复追加；删除按选中下标删除，完成后清空选择。既有重复项不会自动清理。图标使用 aux=0，不支持通过此入口编辑 aux／NBT 过滤条件。

空名单可添加；max_items=0 时禁止添加；初始列表超出上限会报错，不截断数据。选择来源支持“全部”“背包”，现有组件传入空网络列表，因此“网络”来源为空。主界面不另设名单搜索或排序控件。

## 5. 操作流程与事件

1. 打开设备界面，读取设备状态、朝向容器状态、5 格缓冲库存及过滤配置。没有已有模式配置时采用默认黑名单；重开应按保存配置恢复，不应无条件覆盖为默认模式。
2. 自动吸取时，符合过滤规则的物品先进入缓冲槽，UI 更新相应真实物品与数量；缓冲物品随后输入网络，UI 再按实际剩余量刷新。吞吐、满槽和网络无法接收时的调度属于物流规则，不能由界面模拟成功或自行清空库存。
3. 点击 `M/settings_btn`，创建或显示 `bus_item_input_io_ui.settings_window`，按当前配置初始化模式图标和文字。JSON 模板本身不会自动弹出。
4. 点击 `S/filter_mode_btn`，每次切换一次模式，并同步以下两个显示值：

   | 模式 | `filter_mode_btn/icon.uv` | `filter_mode_text.text` |
   | --- | --- | --- |
   | 黑名单（默认） | [16, 0] | 黑名单 |
   | 白名单 | [0, 0] | 白名单 |

   两种模式的 uv_size 均为 [16, 16]，uv 的 y 起点保持 0。图标、文字和实际过滤模式必须一致。
5. 点击 `S/filter_modification_btn`，调用 `show_item_list_window`，传入当前模式要编辑的物品 ID 列表。添加、删除立即修改该本地 list；关闭列表保留修改，没有取消回滚。
6. 关闭设置窗口返回主界面；关闭主界面时清理其子窗口与订阅。设备配置提交和保存回执尚待接入，关闭列表不等同于服务端保存成功。

列表组件在同一 Screen 上拒绝重复打开；物品选择窗口打开期间，暂停列表选择、添加和删除。设置窗口自身的防重复打开、遮罩、防穿透及与名单窗口的操作互斥待接入。

设置模板 layer=-2，需运行时协调覆盖层级；现有名单窗口 layer=30、物品选择窗口 layer=50。不能把模板静态层级当作已验证的弹窗效果。

当前业务按钮的 `button_mappings` 为空，`$pressed_button_name` 为编辑器通用 `%netease_editor_template_namespace.click`；它们尚不构成本设备业务事件。按下／抬起、触控、系统返回及 R12 持有物退出规则待按实际接入约定核实。离线、设备拆除／替换和交互距离失效的关闭与同步处理也待实现。

## 6. 数据与绑定

| 数据／目标控件 | 类型与刷新要求 | 当前接入状态 |
| --- | --- | --- |
| 设备状态 → `M/stat_bar` | 状态文字及图标；打开和状态变化时刷新 | 公共模板初值已有，本设备数据源及回调待接入 |
| 外部容器状态 → `M/outer_container_panel/stat_text` | 朝向容器接入信息 | 当前为静态 text，无业务绑定 |
| 缓冲库存 → `M/container_grid` | 5 个真实物品槽，包含物品身份与数量 | 继承 `netease_container`；设备身份、快照和槽号映射待接入 |
| 过滤模式 → `S/filter_mode_btn/icon`、`S/filter_mode_text` | 黑／白两态；打开及切换后同步刷新 | JSON 只有黑名单初值，无模式字段绑定或业务事件 |
| 过滤物品 → 物品列表窗口 | `list[str]`，元素为物品 identifier | 组件已有 `#EnderNetItemList.*` 动态绑定；设备存储及同步待接入 |
| 名单上限 → 列表参数 | 非负整数，不接受 bool | 组件已有校验；本设备具体上限待定，不能套用缓冲槽数量或示例值 64 |

设置相关按钮的 `collection_details` 使用空 `binding_collection_name` 和 `always_when_visible`，不是可用的过滤数据绑定。引擎容器 collection 与名单配置 collection 独立，不能混用下标。

连接状态属于只读展示；模式和名单修改属于设备配置操作。已有列表组件只修改调用方内存中的 list，不发送服务端命令。设备身份、配置版本、提交时机、等待回执的禁用状态、过期响应与失败重同步都待接入。缓冲槽的增减必须与真实转移结果一致。

## 7. 脚本如何使用本 UI

当前客户端及 `modMain.py` 未检索到本设备专用 Screen 接入。屏幕注册类、主界面运行时完整路径和设置窗口动态实例路径待实现／核实。

已有接口位于 `endernet/client/ui/item_list_window.py`：

```python
# 在 endernet/client/ui/ 下的宿主 Screen 模块导入。
from .item_list_window import WINDOW_ATTRIBUTE, show_item_list_window

# 接入示意：变量由调用方取得，不是已实现的设备字段。
show_item_list_window(self, current_filter_item_ids, max_filter_items, list_title)
```

- 签名为 `show_item_list_window(screen, item_ids, max_items, title="物品列表")`。screen 必须是已创建的 `ToolDeltaScreen`；其余参数为当前名单 list、规则上限和标题，可按当前模式显示“黑名单”或“白名单”。
- 成功返回 True；同一 Screen 已有名单窗口时返回 False，保留原窗口。
- 组件原地修改传入 list，不复制、不提交服务端，没有结果回调或取消回滚。若业务需要草稿与提交，应由宿主另行管理。
- 组件以 `screen._vars[WINDOW_ATTRIBUTE]` 保存实例。父 Screen 销毁时取得该实例并调用 `close()`，保留宿主原有销毁逻辑。组件清理自身动态绑定、根控件及自己打开的物品选择窗口。
- 设置窗口需另行创建／挂载并绑定 `settings_btn`、`filter_mode_btn`、`filter_modification_btn` 和关闭回调；通用编辑器 click 名不是设备业务接口。
- 缓冲槽数据源及两阶段物流接入需按 5 格真实库存实现；本文不提供未实现的吸取、入网或保存 API。

## 8. 待确认项与验证记录

| 待确认／接入项 | 影响范围 |
| --- | --- |
| 5 格缓冲的数量与数据接入 | 覆盖继承的 maximum_grid_items=27，核实 UI 下标到真实槽号 0–4 的映射 |
| 缓冲槽操作权限 | 是否允许玩家手动存取；过滤改变后已缓冲物品的处理 |
| 名单规则 | 空名单行为、条目上限、切换模式时共用还是分开保存列表 |
| 设置窗口生命周期 | 完整路径、layer=-2 的运行时调整、遮罩、防穿透、重复点击和返回键 |
| 脚本与服务端同步 | 注册、快照、两阶段物流、配置保存、回执与失效处理 |
| 游戏验证 | 触控、长文本、层级遮挡、库存更新、关闭及重开 |

- [x] 已核对当前 JSON、公共模板及用户补充的交互规则。
- [x] 已明确 5 格真实缓冲槽及“外部容器 → 缓冲 → 网络”的顺序。
- [x] 已记录默认黑名单、图标 UV、模式切换和物品列表入口。
- [x] 已区分资源初始值、已有公共接口与待接入行为。
- [ ] 本设备脚本、绑定、同步及完整运行时路径已验证。
- [ ] 游戏内打开、吸取、入网、过滤修改、关闭及重开已验证。
- [ ] 移动端、异常状态及持有物退出已验证。

| 日期 | 环境与方式 | 结果 | 未验证项 |
| --- | --- | --- | --- |
| 2026-09-27 | 离线解析 JSON，核对公共模板、玩法文档和列表窗口接口 | 文档按重新保存的控件名及最新缓冲规则填写；记录继承网格上限与业务容量的差异 | 仅修改文档，未运行全量代码测试；未进行游戏内验证 |
| 2026-09-29 | User 定案 + 离线核对 JSON 与 `blocks.md` | 缓冲槽数由 9 更正为 **5**（以 JSON 的 `grid_dimensions=[5,1]` 为准）；方块侧补网易容器，与领域层槽数同源于 `ITEM_INTAKE_PORT_SLOT_COUNT` | 仍**未做游戏内验证**：右键打开、5 格填充、吸取与入网都未在真机上跑过 |
