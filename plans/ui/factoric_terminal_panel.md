# factoric_terminal_panel.json：工厂终端

[返回目录](README.md) · [UI JSON](../../endernet_rp/ui/factoric_terminal_panel.json)

对应 namespace：`factoric_terminal_panel_ui`；JSON 屏幕入口：`factoric_terminal_panel_ui.main`。按本目录命名规则，同名文档为 `factoric_terminal_panel.md`。

UI 交接：2026-10-02 已记录 User 本次确认的控件用途和两张参考截图；脚本接入：待接入；游戏验证：待验证。本文区分用户要求、当前 JSON 配置和待确认项，不把静态资源当作已实现功能。

## 1. 制作范围与要求

本次交接范围为当前网络正在运行的订单列表、网络剩余可用内存、单个订单详情、完整配方树的制作进度，以及直接取消（中止）订单。

- `orders_view` 显示当前网络所有正在运行的订单。按既有订单规则，已准入但排队、等待或阻塞的未结束订单仍属活跃订单；规划请求和拒绝记录不是运行订单。
- `memory_left_bar` 展示网络剩余可用内存，以服务端可用值为准。订单固定内存不会随部分进度完成而释放，完成或手动中止时按订单规则释放。
- `order/interact_btn` 点击后打开 `order_description_window`，显示被点击订单的当前状态。
- `order_sources_scroll` 展示该订单所有需求物品的制作进度等信息：订单实际配方树中**涉及的所有配方都要显示**，包括主产物配方和各层中间需求物配方，以及尚未开始、正在制作、已经完成的配方，不能只显示直接原料或当前制作项。
- `cancel_order_btn` 点击后直接请求取消当前订单，不增加二次确认；详情的 `confirm_btn` 和 `close` 按下后都关闭该窗口，不改变订单运行状态。

订单整体执行进度沿用 `已完成配方任务数 / 订单树配方任务总数`，同配方执行 10 次计 10 个任务。配方条目的物品数量进度见第 4 节，不能用配方种类数替代整体任务数。

需求依据：User 2026-10-02 本次交接、[Task 11](../tasks/11.%20终端与客户端同步.md)、[订单规则](../technical_notes/order_planning_execution.md)、[方块说明](../blocks.md)。规划预览、材料缺口和提交仍属既有任务范围，但当前 JSON 没有对应控件，本次未补设其布局或流程；规划预览不代表资源已锁定。

## 2. 资源与交接信息

| 项目 | 记录 |
| --- | --- |
| UI 制作者／交接日期／资源版本 | User／2026-10-02／当前工作区 JSON 与本次两张参考截图 |
| 本次交接范围 | 运行订单、剩余内存、详情、全部配方进度与状态、直接取消及详情关闭 |
| 根控件 | `main@common.inventory_screen_common`；`$screen_content=factoric_terminal_panel_ui.content`；`content` 为 180×210 |
| 适用设备／打开入口 | 工厂终端面板 `endernet:factoric_terminal_panel`；实际打开流程与 Screen 注册待接入核实 |
| 本文件模板 | `content`、`orders`、`order`（80×20）、`order_description_window`（180×200）、`order_sources_grid`、`order_source_status`（80×20） |
| 公共组件 | `common.inventory_screen_common`、`common.dialog_background_opaque`、`common.scrolling_panel`、`common.button`、`endernet_ui_common.device_stat_bar`、`endernet_ui_common.close_btn` 及 JSON 引用的编辑器子模板 |
| 登记与本地化 | `_ui_defs.json` 已登记 `ui/factoric_terminal_panel.json`；主标题键 `tile.endernet:factoric_terminal_panel.name` |
| 纹理 | 使用现有 `textures/ui/endernet/` 面板和按钮纹理、`textures/ui/white_background` 及引擎滚动条；具体引用以 JSON 为准 |
| 参考截图 | 本次附件一为订单列表与内存条，附件二为两列配方详情与底部按钮；草方块、999、100%、“未知”等为预览占位，不是业务数据 |

## 3. 控件功能与路径

主界面以 `<主内容>` 表示 `content` 实例；详情以 `<详情>` 表示动态创建的窗口实例；条目以 `<订单项>`、`<配方项>` 表示模板实例。以下只确认定义及条目内相对路径，完整运行路径和 scrolling_panel 展开层级待游戏核实，不把模板名当作固定实例路径。

| JSON 定义位置 | 实例相对路径 | 类型与用途／当前配置 |
| --- | --- | --- |
| `content.title/title_text` | `<主内容>/title/title_text` | label，本地化主标题 |
| `content.stat_bar` | `<主内容>/stat_bar` | 公共设备状态条；详情弹窗接入待核实 |
| `content.close` | `<主内容>/close` | 公共主界面关闭按钮，事件待接入 |
| `content.active_orders_tip` | `<主内容>/active_orders_tip` | label，静态“活跃订单列表 0/0”，数字含义待确认 |
| `content.orders_view` | `<主内容>/orders_view` | scrolling_panel，162×152，偏移 `[0,32]`，显示全部运行订单；滚动内容 `factoric_terminal_panel_ui.orders` |
| `orders.grid` | `<订单滚动内容>/grid` | grid，collection `orders`，模板 `factoric_terminal_panel_ui.order` |
| `content.memory_left_bar` | `<主内容>/memory_left_bar` | panel，剩余可用内存区域，偏移 `[0,188]`，大小 `100%-18px × 12` |
| `memory_left_bar.mask` | `<主内容>/memory_left_bar/mask` | image，静态宽度 `50%+-4px`；条形比例分母待确认 |
| `memory_left_bar.memory_left_label` | `<主内容>/memory_left_bar/memory_left_label` | label，展示剩余可用内存；静态“剩余可用内存 0KB”，单位差异见第 8 节 |
| `order.res_item_name` | `<订单项>/res_item_name` | label，静态“999 x 某某某”；主产物数量含义与文本格式待确认 |
| `order.progress_text` | `<订单项>/progress_text` | label，整体进度文字，静态 `100.0%%`；接入时核对百分号格式 |
| `order.progress_bar`、`progress_bar/mask` | `<订单项>/progress_bar/mask` | panel 内 image 填充，以 relative x 尺寸显示订单整体完成比例；外层保持布局尺寸 |
| `order.res_item_renderer` | `<订单项>/res_item_renderer` | custom `inventory_item_renderer`，12×12，显示订单结果（主）物品；静态 `#item_id_aux=131072` |
| `order.interact_btn` | `<订单项>/interact_btn` | button，点击打开当前订单详情；事件仍为占位 |
| `order_description_window.title/title_text` | `<详情>/title/title_text` | label，静态“当前正在制作： 未知”；展示订单状态的具体标题格式待确认 |
| `order_description_window.close` | `<详情>/close` | 公共按钮，按下关闭详情 |
| `order_description_window.order_sources_scroll` | `<详情>/order_sources_scroll` | scrolling_panel，160×144，偏移 `[0,24]`；滚动内容 `factoric_terminal_panel_ui.order_sources_grid`；展示全配方树 |
| `order_description_window.cancel_order_btn` | `<详情>/cancel_order_btn` | 48×20 红色“取消订单”按钮，直接中止订单 |
| `order_description_window.confirm_btn` | `<详情>/confirm_btn` | 48×20“确认”按钮，按下只关闭详情 |
| `order_sources_grid` | `<配方滚动内容>` | grid，collection `order_sources`，模板 `factoric_terminal_panel_ui.order_source_status` |
| `order_source_status` | `<配方项>` | image，整项背景按制作阶段变色 |
| `order_source_status.progress_bar` | `<配方项>/progress_bar` | image，高 2px，静态宽度 `50%+-2px`，以 relative x 尺寸显示本种源物品制作进度 |
| `order_source_status.item_renderer` | `<配方项>/item_renderer` | custom `inventory_item_renderer`，16×16，显示当前需求物，即当前配方产物；静态 `#item_id_aux=131072` |
| `order_source_status.item_name_text` | `<配方项>/item_name_text` | label，显示该物品 `GetBasicInfo().itemName` 提供的中文名 |
| `order_source_status.process_stat_text` | `<配方项>/process_stat_text` | label，展示制作阶段、数量进度、投料／产物状态三要素 |

背景、标题图片、按钮 default/hover/pressed 子控件为装饰或交互外观，不承担订单身份或库存功能。

## 4. 列表、进度及组件复用

本界面没有真实物品槽或玩家背包，两个 renderer 都是只读展示，不持有实物，不允许存取物品。

| 组件 | 数据来源与身份 | 数量及显示规则 |
| --- | --- | --- |
| `orders` 集合 | 当前网络权威运行订单；显示下标映射到稳定订单 ID | 两列，`grid_dimensions=[2,2]`、`maximum_grid_items=8`；8 是当前静态配置，不能截断所有运行订单 |
| `order_sources` 集合 | 所选订单实际配方树；保留订单 ID 与配方／步骤身份 | 两列，`grid_dimensions=[2,2]`、`maximum_grid_items=14`；必须支持超过 14 项的完整滚动展示 |
| 订单整体进度 | 已完成配方任务数／订单树配方任务总数 | 包含重复执行次数；相同配方完成 3 次／共 10 次为 30% |
| 配方需求物进度 | 本种源物品已完成制作数量／总制作数量 | 两项使用同一物品数量单位，进度条与文本采用同一计数，不混用执行次数与产物数量 |

全量列表覆盖主产物与各层中间产物的所有涉及配方，已经完成的配方也保留展示。仅由库存满足、未展开配方的叶子原料不虚构制作配方。相同产物可能涉及不同配方或树分支，不能仅按物品名合并而漏掉配方；重复执行的展示聚合粒度待确认。

进度比例 `p=已完成数/总数`，relative x 限制在 `[0,1]`。订单实际填充更新 `<订单项>/progress_bar/mask`；配方填充更新 `<配方项>/progress_bar`；保留高度、锚点与边距。不能把订单外层 `progress_bar` panel 当作填充缩放，也不能以 `clip_ratio` 代替本次要求的 relative x 尺寸。总数为零或未确定时不得除零或显示虚假的 100%，具体文字待确认。

`process_stat_text` 为一个包含以下三要素的文本，按此顺序展示：

| 要素 | 允许内容 |
| --- | --- |
| 制作阶段 | `等待中`／`制作中`／`已完成` |
| 数量进度 | `已完成数/总数` |
| 投料／产物状态 | `等待投料`／`等待产物`／`已完成` |

完整格式示例：`等待中 0/10 等待投料`、`制作中 3/10 等待产物`、`已完成 10/10 已完成`。状态来自权威执行数据，不能仅凭比例猜测“等待投料”或“等待产物”；三要素不自动等同于完整异常阻塞原因。

按第一要素设置 **`order_source_status` 图片控件本身**的 `color`，不是只改变进度条或文字颜色。颜色传入 JSON／脚本时归一化为 0～1：

| 制作阶段 | User 指定 RGB | 归一化 RGB |
| --- | --- | --- |
| 等待中 | `(200,200,255)` | `(200/255.0,200/255.0,1.0)` |
| 制作中 | `(255,255,200)` | `(1.0,1.0,200/255.0)` |
| 已完成 | `(200,255,200)` | `(200/255.0,1.0,200/255.0)` |

JSON 背景初值为制作中的黄色，不代表真实阶段。初始化、刷新和滚动复用时一并覆盖身份、图标、中文名、数量、状态文本、进度宽度与背景色，不能残留上一项状态。空列表不生成占位订单，切换详情不能保留上一订单的配方。

## 5. 操作流程与事件

| 控件 | 操作与动作 | 参数及结果 |
| --- | --- | --- |
| `<订单项>/interact_btn` | 点击打开详情 | 绑定被点击项的稳定订单 ID，不能只传显示下标 |
| `<详情>/cancel_order_btn` | 点击直接请求中止，无二次确认 | 发送当前订单 ID，由服务端执行；客户端不能自行宣告成功或释放内存。回执显示及取消后窗口是否关闭待确认 |
| `<详情>/confirm_btn` | 按下关闭详情 | 只关闭当前窗口，不提交、完成或取消订单 |
| `<详情>/close` | 按下关闭详情 | 与确认按钮关闭范围相同 |
| `<主内容>/close` | 主界面关闭入口，具体事件待接入 | 清理本界面订阅及详情实例，不因关闭界面中止后台订单 |

初次展示读取网络订单与可用内存，打开详情读取所选订单的全量配方状态，打开期间随事件更新。取消为服务端写操作，其余数据展示为只读。直接中止不意味着外部机器投料可回滚，在途和迟到产物按既有订单规则处理。

详情模板未在 `content.controls` 中实例化，动态创建、层级、输入拦截及关闭清理待接入。防穿透、重复取消、订单自然结束或数据失效时的窗口表现待确认。离线、设备拆除／替换、距离失效、系统返回、断线及 R12 键鼠／触控映射沿用既有已定规则，未定部分不自行补设。

## 6. 数据与绑定

以下为数据语义，不是新协议字段名或已实现 Python 接口；实际绑定、回调与同步消息由接入方填写。

| 控件／集合 | 数据与刷新来源 | JSON 现状／接入状态 |
| --- | --- | --- |
| `orders` | 当前网络全量运行订单及稳定 ID；快照／状态变化 | collection 已命名，实际数量和条目绑定待接入 |
| `res_item_renderer` | 订单主产物身份；初始化／重绑 | 静态 `#item_id_aux=131072`，待替换真实物品 |
| 订单进度填充与文本 | 整体任务完成／总计数；服务端完成事件及重同步 | 静态宽度和文字，待接入 |
| `memory_left_label` | 网络可用内存；内存租约取得／释放及快照 | 静态 `0KB`；既有规则 B，未确认单位改动 |
| `order_sources` | 所选订单全部配方身份、产物、数量、阶段、投料状态；打开及更新 | collection 已命名，实际条目绑定待接入 |
| 配方背景、进度与状态文本 | 同一份配方状态；一起刷新 | 静态颜色、宽度、文字，待接入 |
| `item_renderer`、`item_name_text` | 当前配方产物与 `GetBasicInfo().itemName` 中文名；初始化／重绑 | 图标名称均为占位，真实信息读取入口待接入 |
| `interact_btn`、`cancel_order_btn`、`confirm_btn` | 打开详情／取消／关闭 | `$pressed_button_name=%netease_editor_template_namespace.click`、`button_mappings=[]`，collection_details 集合名为空 |
| 两处 `close` | 分别关闭主界面、详情 | 未覆盖公共模板事件，继承编辑器占位与空映射 |

接入时须同时补齐事件名、按钮映射与真实回调，仅填 `$pressed_button_name` 不能视为点击已接通。详情确认与关闭均按 User 要求为按下触发，其他点击实际映射待接入。快照版本、过期响应、断档重取、取消在途和失败回执承载待接入，不虚构超时数值。

名称按指定 `GetBasicInfo().itemName` 显示中文名；当前名称标签宽 56px，长文本处理、名称查询失败回退及数量精度待确认／验证。

## 7. 脚本如何使用本 UI

| 项目 | 使用说明／接入状态 |
| --- | --- |
| Screen 文件、类名、注册入口 | 待接入；本次未新增 Screen 或设备打开登记 |
| namespace／根控件 | JSON 入口 `factoric_terminal_panel_ui.main`、内容 `content`；实际根路径待核实 |
| 参数及可信来源 | 网络／设备会话身份及详情稳定订单 ID；真实协议参数名待接入 |
| 初始化与订阅 | 读取订单、可用内存及详情全配方树；服务端权威状态决定显示 |
| 列表与更新 | 动态支持所有订单与配方，不以静态 8／14 为业务上限；按第 4 节一起更新条目状态 |
| 事件与命令 | 打开详情、发送当前订单中止请求、关闭详情；真实 Python 方法和服务端命令待接入 |
| 清理 | 关闭／销毁移除订阅与回调，清理详情、身份及缓存，不取消后台订单 |
| 公共组件 | 参见 [公共 UI 控件](endernet_ui_common.md)；滚动展开路径、按钮覆盖参数和输入行为待核实 |

实际调用示例待接入后填写执行端、导入路径、前置会话及真实参数，不提供占位 API。

## 8. 待确认项与验证记录

此前静态缺项见 [11-05/11-06 UI 缺项与等待交接](../tasks/11-05-11-06-UI缺项与等待交接.md)。本次已交接列表、详情、进度、三要素、三色及直接取消用途；旧记录不能据此视为脚本已接入，其余缺项保留。

| 问题 | 当前结论／影响 |
| --- | --- |
| 进度数据映射 | 整体为重复执行任务计数，条目为源物品制作数量；多件产出时不得混用，实际数量映射待核实 |
| 配方条目粒度 | 全部涉及配方必须可见；重复执行、同产物多配方／多分支的分项或聚合方式待确认，不能丢配方或计数 |
| 混合执行阶段 | 同条目部分等待、部分制作、部分完成时的第一和第三要素取值待确认 |
| 内存单位及 mask | 本次未指定单位或比例分母；既有要求 B，JSON／截图 KB。本次不把预览视为单位改定，不补设换算与总量 |
| 其他文本口径 | 活跃订单提示 `0/0`、结果数量 `999 x`、百分比格式、并行配方时详情标题待确认，不将整体任务算法套到活跃订单数标题 |
| 空态与异常名称 | 不除零、不伪造完成；空态文字、名称查询回退、长文本待确认／验证 |
| 取消与订单结束 | 直接取消、无二次确认已定；回执显示、取消后关闭、自然结束、重复点击策略待确认／接入 |
| 规划、提交、缺料、完整阻塞原因 | 当前无完整控件约定；三要素仅覆盖阶段／数量／投料产物状态，不虚构新 UI |
| 通用终端能力 | 无设备详情弹窗实例、离线遮罩、玩家背包／网络存取区；本次未确定补建 |
| 注册、事件与同步 | JSON 已登记；业务 Screen、事件、集合绑定、动态承载及真实命令／消息待接入 |

验收清单：

- [x] 已记录本次控件用途、全配方树、三要素、背景色及按钮流程。
- [x] 已静态核对模板、相对路径、集合、尺寸、占位字段和公共关闭模板。
- [ ] 已接入 Screen、订单／内存同步、动态列表、物品信息及按钮事件。
- [ ] 游戏验证 0、1、超过 8 个订单和超过 14 项配方、多层树均可完整浏览。
- [ ] 验证重复执行、多件产出、同产物多配方、混合阶段、三色及进度一致。
- [ ] 验证中文名、图标、长文本、大数量、滚动重绑与详情实时刷新。
- [ ] 验证确认／关闭仅关窗口、取消无二次确认且中止目标正确、回执及内存释放。
- [ ] 验证自然结束、离线与重开、移动端输入及异常状态。

| 日期／验证人 | 环境与方式 | 结果／证据 | 未验证项 |
| --- | --- | --- | --- |
| 2026-10-02／Codex | 当前 UI JSON、公共关闭模板、资源登记、订单规则与两张交接截图静态核对 | 完成同名文档；明确实际填充控件、全量列表、三要素及归一化颜色；列明占位事件、上限和单位差异 | 仅编写文档，未修改 JSON 或运行代码、未运行离线代码门禁、未启动游戏；参考截图不计为真实订单／事件验收 |
