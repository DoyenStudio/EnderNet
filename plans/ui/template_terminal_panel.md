# template_terminal_panel.json：样板终端

> **2026-10-03 新增需求（待实现）：** 网络列表继承共享终端的样板标记与长按下单预览规则；长按识别后不得继续触发短按存取或虚格清空。配置区及真实样板槽的原行为继续有效，预览不会生成真实物品。 完整规则见 [网络样板标记与下单预览契约](../technical_notes/terminal_pattern_order_preview.md)。

[返回目录](README.md) · [UI JSON](../../endernet_rp/ui/template_terminal_panel.json) · [通用终端规则](common_terminal_panel.md)

对应 namespace：`template_terminal_panel_ui`；JSON 屏幕入口：`template_terminal_panel_ui.main`。本文按目录规则使用与 JSON 同名的 Markdown 文件名。

UI 交接：2026-10-02 已记录 User 本次确认的用途、流程与参考截图；脚本接入：**服务端与客户端均已接入（2026-10-02）**；游戏验证：待验证。

> **2026-10-02 状态补充。** 本文第 2～7 节记的是**交接那一刻**的现状；当天之后
> 做了一轮接入，**改过哪些控件**见 [11-交付说明](../tasks/11-交付说明.md) 第二十四节
> 第 5 节，**每一行的现状**以那一节为准。同一天 User 还逐条裁定了第 8 节里的
> 未定项（模式切换、制作门限、工厂输出手势、堆叠 64……），裁定的落点见那一节第 3 节。
>
> 因此：**右键样板终端现在会打开界面**（`TERMINAL_SCREENS` 已加这一条，
> `client/ui/__init__.py` 也注册了屏幕）。**但游戏内实测仍然是零**——界面画不画得出来、
> 长按会不会派发按下事件、两条新引擎面（配方按输入查、掉落）的真实形状，
> **一样都没有**。下方验收清单因此**只有一条勾上**。

## 1. 制作范围与要求

基础属性和通用终端功能继承 [common_terminal_panel](common_terminal_panel.md)：网络物品列表、搜索／排序、真实玩家背包及快捷栏、网络存取、连接状态与关闭规则；设备带宽负载 1u、耗能 4 EN/t，见 [方块说明](../blocks.md)。继承规则不代表当前 JSON 已具备全部控件或脚本已接通，差异见第 8 节。

样板配置区域新增两个互斥模式：合成样板、工厂样板。输入九宫格 `source_grid` 和输出配置 `outputs_grid` 都是虚槽位，物品仅代表配方配置；右侧 `template_input_slot`、`template_output_slot` 是真实样板物品槽。虚拟配置不能通过任何槽位交互变成真实物品。

本次确认：输入槽仅接受工厂样板物品；仅当输出槽为空时，点击 `make_template_btn` 才能将当前设定记录到样板物品中并输出至输出槽。实际库存与编码由服务端校验执行。

需求依据：User 2026-10-02 本次交接、[Task 11](../tasks/11.%20终端与客户端同步.md)、[方块说明](../blocks.md)。交接当时未说明的几项（工厂输出编辑、数量编辑、已有样板更新、模式切换配置保留）**当天稍后由 User 逐条裁定**，裁定结果与落点见 [交付说明](../tasks/11-交付说明.md) 第二十四节第 3 节；本文第 8 节按裁定后的状态重写。

## 2. 资源与交接信息

| 项目 | 记录 |
| --- | --- |
| 制作者／交接日期／资源版本 | User／2026-10-02／当前工作区 JSON 与本次参考截图 |
| 本次交接范围 | 通用属性继承、模式互斥、虚拟输入交互、合成输出自动检测、真实样板输入限制及制作输出条件 |
| 根控件 | `main@common.inventory_screen_common`；`$screen_content=template_terminal_panel_ui.content`；内容大小 180×220 |
| 适用设备／入口 | 样板终端面板 `endernet:template_terminal_panel`；真实打开流程和 Screen 注册待接入核实 |
| 本文件模板 | `content`、`template_item_slot`（18×18） |
| 公共组件 | `common.scrolling_panel`、`common.button`、`endernet_container_common.custom_slot`、`endernet_terminal_inventory.inventory`、`endernet_ui_common.device_stat_bar`、`endernet_ui_common.close_btn`、公共搬运图标及编辑器按钮子模板 |
| 资源登记／标题 | `_ui_defs.json` 已登记 `ui/template_terminal_panel.json`；主标题键 `tile.endernet:template_terminal_panel.name` |
| 纹理 | 当前 JSON 引用现有面板、槽位背景、选中框、箭头、工作台／熔炉模式图标和按钮纹理；具体路径以 JSON 为准 |
| 参考截图 | 本次附件展示上方网络列表、中间配置、右侧真实样板槽、下方背包和快捷栏；草方块和 99 为预览占位，不代表真实配方或库存 |

## 3. 控件功能与路径

用 `<主内容>` 表示 `content` 的实际实例，`<配置区>` 表示 `<主内容>/template_panel`，`<虚槽>` 表示一个 `template_item_slot` 实例。完整根路径、滚动模板内部层级和 grid 条目路径待运行时核实，不把模板名当作实例路径。

| JSON 定义位置 | 实例相对路径 | 功能及当前配置 |
| --- | --- | --- |
| `content.title/title_text` | `<主内容>/title/title_text` | 主标题，本地化键 |
| `content.close` | `<主内容>/close` | 主界面关闭；已有 `#EnderNetTerminal.close` 的 pressed 映射，真实回调待接入 |
| `content.stat_bar` | `<主内容>/stat_bar` | 公共设备连接状态条，规则继承通用终端 |
| `content.terminal_items_scroll` | `<主内容>/terminal_items_scroll` | 网络物品列表，162×44，偏移 `[0,20]`；滚动内容为 `endernet_ui_common.custom_item_slots_grid` |
| `content.inventory` | `<主内容>/inventory` | 真实玩家背包与快捷栏；`$display_inventory_label=false`，偏移 `[0,2]` |
| `content.sort_method_btn`、`sort_order_btn` | `<主内容>/sort_method_btn`、`sort_order_btn` | 排序规则继承通用终端；已有对应 pressed 映射及图标 |
| `content.unavailable_mask` | `<主内容>/unavailable_mask` | 离线输入遮罩，初始隐藏、layer 20；无实际状态绑定 |
| `content.flying_item` | `<主内容>/flying_item` | 公共搬运图标，初始隐藏；虚拟配置交互不代表真实搬运 |
| `content.template_panel` | `<配置区>` | 样板配置区域，偏移 `[0,76]` |
| `template_panel.title` | `<配置区>/title` | label，显示当前模式。**已接，但不走绑定**（User 2026-10-02 定）：`TemplatePanel._sync_title` 每帧对一次文本、变了才 `SetText`（`样板配置 - 合成样板` / `样板配置 - 工厂样板`）。label 的 `layer=2`、白色、`anchor_to=top_left`——深灰压在面板底色上、又被 `layer 1` 的九宫格压住，是真机上「文字看不见」的直接原因 |
| `template_panel.crafting_template_mode_radio_btn` | `<配置区>/crafting_template_mode_radio_btn` | 点击设为“合成样板”；工作台图标，12×12 |
| 上条的 `selected_frame` | `<配置区>/crafting_template_mode_radio_btn/selected_frame` | 合成模式选中框；JSON 未设隐藏，初值显示 |
| `template_panel.factoric_template_mode_radio_btn` | `<配置区>/factoric_template_mode_radio_btn` | 点击设为“工厂样板”；熔炉图标，12×12 |
| 上条的 `selected_frame` | `<配置区>/factoric_template_mode_radio_btn/selected_frame` | 工厂模式选中框；JSON 初值 `visible=false` |
| `template_panel.source_grid` | `<配置区>/source_grid` | 3×3 虚拟配方输入，collection **`endernet_template_source`**（接入时由 `crafting_grid` 改来，见下），引用 `template_source_cell` |
| `template_panel.outputs_grid` | `<配置区>/outputs_grid` | 虚拟输出配置／预览，JSON 为 `grid_dimensions=[1,3]`，collection `test_grid`，引用 `template_item_slot` |
| `template_panel.template_input_slot` | `<配置区>/template_input_slot` | 真实自定义槽，仅允许工厂样板物品；偏移 `[-9,0]` |
| `template_panel.template_output_slot` | `<配置区>/template_output_slot` | 真实编码结果槽；偏移 `[-9,36]`；不为空时不能制作覆盖 |
| `template_panel.make_template_btn` | `<配置区>/make_template_btn` | “OK”按钮，16×16；将设定写入样板物品并输出 |
| `template_item_slot.item_renderer` | `<虚槽>/item_renderer` | custom `inventory_item_renderer`；静态 `#item_id_aux=131072` |
| `template_item_slot.count` | `<虚槽>/count` | 虚拟配方数量文字；静态 `99`，不是库存余额 |

槽位背景、横向配方箭头、右侧向下箭头和按钮外观为装饰，不承担物品转移。当前虚槽模板只有背景、renderer、count，没有按钮或选中框，实际点选承载待接入核实。

## 4. 槽位、索引及模式规则

| 槽位组 | 性质与身份 | 数量／索引 | 规则 |
| --- | --- | --- | --- |
| 网络物品列表 | 真实网络汇总的展示与交易入口；稳定 ResourceKey | 当前公共 grid collection `test_grid`，静态上限 27 | 继承通用列表与存取规则，静态数值不是物品种类上限；绑定适配待接入 |
| 玩家背包／快捷栏 | 真实玩家库存镜像 | 背包局部 0～26 → 引擎 9～35；快捷栏局部 0～8 → 引擎 0～8 | 参见 [背包组件](endernet_terminal_inventory.md)，不与虚拟配方索引混用 |
| `source_grid` | 虚拟配方输入，不是设备真实库存 | 9 格，collection **`endernet_template_source`**；业务 0～8 按行从左到右、从上到下 | 放置只记录配置，不消耗被选源槽物品；清空只删除配置 |
| `outputs_grid` | 虚拟配方输出配置／预览 | JSON 为 1 列×3 行；collection_index 0 上、1 中、2 下 | 用户表述的输出“九宫格”在本 JSON 中实际为三个槽；合成模式规则如下 |
| `template_input_slot` | 真实样板物品槽，普通自定义槽位交互 | 1 格，设备真实槽号待接入 | 仅接受 `endernet:factorical_template`（工厂样板），不能按模式擅自改为接受合成样板 |
| `template_output_slot` | 真实编码结果槽 | 1 格，设备真实槽号待接入 | 输出为空才可制作；输出结果可按真实槽位规则取出，不能与虚拟 outputs_grid 混同 |

两个模式按钮使用同一个模式状态：点击合成按钮设为合成样板，点击工厂按钮设为工厂样板；对应 `selected_frame` 显示，另一个隐藏。始终只显示当前模式的一个框，不允许双选，也不将再次点击已选模式解释为取消全部选择。JSON 初始外观为合成模式，重开时是否恢复已存配置待确认。

`source_grid` 虚拟交互与普通自定义槽位不同：

- 先点击背包槽或网络列表行选择物品，再点击输入九宫格目标格：把物品配置放入该虚格，**源槽物品不消耗、不移动**。网络来源按选中的 ResourceKey 复制配置，不受可用库存数量限制（含零库存的样板条目）。不能复用普通存取／交换命令扣减真实库存。
- 先点击九宫格内有物品的虚格，再点击外界槽位：**仅清空被选九宫格格子的配置**，外界槽位不增加、不替换、不合并物品，网络库存也不增加。
- 虚格只定义样板所需物品，虚格到外界的这次操作应优先按清空处理，不能继续触发目标真实槽的普通交易。数量复制、数量编辑、虚格之间交互及覆盖已有虚格的具体行为待确认。

合成样板模式下，`outputs_grid` 三格全部不可手动更改：

| collection_index | 位置 | 合成模式内容与限制 |
| --- | --- | --- |
| 0 | 上 | **整个不显示**（User 2026-10-02 定） |
| 1 | 中 | 根据 `source_grid` 九宫格物品摆放方式检测合成配方并显示输出产物；**没有对应配方时整格置空**（不画任何占位图标），不可手动配置或清空 |
| 2 | 下 | **整个不显示**（同上） |

**「没有产物」就是空的**（User 2026-10-02 改）：服务端发**空行**，界面由
`#item_id_aux` 回 0 清掉图标——槽底还在，里面没有东西。

**曾经发过 `minecraft:barrier` 占位图标，现已撤掉。** 当时的依据是一条
「2026-10-02 真机实测：`id_aux` 回 0 盖不掉上一个渲染的物品」，**那条结论是
错的**：同日稍晚在真机上复验（界面开着，先让那一格画出 64 个橡木木板，再发
空行）——**图标干净地消失了，没有残留**。原先那次多半量在输出格的集合名还
没接对的时候（`test_grid` → `endernet_template_outputs` 的改名就在同一轮，
见 [11 交付说明](../tasks/11-交付说明.md) 第二十五节）。

> 真正会保留上一个渲染物品的是**另一条路**：`SetUiItem(Item("minecraft:air"))`
> （搬运动画在用）。两条路长得像，**别混**——集合绑定这条回 0 就清掉了。

服务端**不**往上下两格发任何图标：那两格在合成模式下根本不画
（`TemplateView.visible` 只放中间那格过），发什么都会在切到工厂模式、它们变
可见的那一刻露出来，因此同样是空行。

中间格是只读预览，不是配方产物，不编码进样板，也不能从 UI 取出。配方检测本身不执行合成，不消耗输入物品，也不生成真实产物。每次输入配置变化都需重新匹配；无匹配时清掉旧预览，不保留上一配方结果。

工厂模式下输出格仍为虚槽位；本次未指定其设置手势、数量、主副产物和清空规则，不将合成模式的自动检测规则直接套用到工厂模式。

## 5. 操作流程与事件

| 操作 | 动作与约束 | 成功／失败及待接入内容 |
| --- | --- | --- |
| 点击模式按钮 | 设置唯一模式，更新两个框及模式标题 | 切换时输入／输出配置保留或清空策略待确认 |
| 外界选物 → 点击 source_grid | 复制物品配置到虚格，不扣减真实源槽 | 数量策略与失败反馈待确认 |
| 选中 source_grid → 点击外界槽 | 清空被选虚格，不把物品转移到外界 | 清除虚拟选中态；不能连带执行真实槽交易 |
| 合成模式输入变化 | 按九宫格摆放检测配方，刷新中间输出预览 | 无配方则为空；多候选选择规则待确认 |
| 向 template_input_slot 放物 | 普通真实槽位存放，但只接受工厂样板 | 非工厂样板拒绝放入，不能吞物 |
| 点击 make_template_btn | 输出为空且输入存在合规样板、配置校验通过时，将设定编码到输入样板物品并输出至 template_output_slot | 输出占用则不能制作、覆盖或丢弃原物品；真实输入消耗／移动与输出写入由服务端统一完成，具体单次件数待确认 |
| 取走 template_output_slot 结果 | 真实样板槽的普通取出行为 | 输出恢复为空后才具备下一次制作的输出条件；仍需其他校验通过 |
| 关闭／失活 | 继承通用终端关闭及订阅清理 | 不把虚配置返还成真实物品；真实样板槽与配置重开留存策略待确认 |

输出为空是制作的必要条件，不代表缺输入、无有效配方也能成功。服务端须在执行时重新检查真实输入、输出空位和配置，避免并发／重复点击重复编码或复制真实样板。回执前客户端不能自行产出物品；等待表现、错误提示位置及重复点击策略待接入／确认。

本次输入物品限制按 User 指令执行：两种模式的输入槽都仅接受工厂样板物品。既有定义区分合成／工厂样板标识，合成模式最终输出物品标识是否转换待确认，不能据旧定义反向扩大输入白名单。

网络存取、背包交换／合并、排序、离线及真实搬运动画沿用通用终端规则；虚拟复制和清空不构成真实搬运，是否另做提示动画待确认。设备拆除／替换、距离失效、断线和 R12 未定手势／持有物退出规则不在本文补设。

## 6. 数据与绑定

下表是字段语义与当前资源，不是新协议字段名或已实现 Python 方法；真实绑定、回调及命令由接入方补齐。

| 控件／数据 | 含义及刷新时机 | JSON 配置／接入状态 |
| --- | --- | --- |
| 两个模式按钮及 selected_frame | 合成／工厂唯一模式；初始加载和切换时一起更新 | 三个专属按钮（含制作）均为编辑器占位事件、空 button_mappings、空 collection_details 集合名；两框无模式绑定 |
| template_panel/title | 当前模式文字 | 静态合成模式标题，待更新接入 |
| source_grid | 9 个虚拟输入的物品身份、数量、位置 | collection `endernet_template_source`；`template_source_cell` 的六个 `$` 变量指向 `#EnderNetTemplate.source_*` |
| outputs_grid | 模式相关虚拟输出；合成模式上下两格不显示、中间是自动检测结果 | collection `endernet_template_outputs`；模式禁改已接入，配方检测由服务端算 |
| template_input_slot／output_slot | 两个真实样板槽内容及版本 | 引用 custom_slot，无本界面真实槽号映射；过滤和同步待接入 |
| make_template_btn | 编码请求：当前模式与配置、真实输入及输出资格 | 显示“OK”；真实方法／协议／回执承载待接入 |
| terminal_items_scroll | 网络汇总列表 | 仍引用公共 custom_item_slots_grid，未采用通用终端专用集合绑定模板；且与 outputs_grid 同名 test_grid |
| close／排序按钮 | 通用关闭、排序方法、排序方向 | 已有 #EnderNetTerminal.close／sort_method／sort_order 的 pressed 映射；实际屏幕回调待核实 |
| unavailable_mask／stat_bar | 连接与交易可用状态 | 遮罩初始 false、无状态绑定；状态详情接入待核实 |

仅改事件名不能视为按钮可用，须补映射和实际回调。真实交易按稳定资源／槽身份及版本验证；虚格配置必须使用独立的数据和操作语义，不将虚格下标当作玩家或设备真实槽号。快照、增量、过期配方查询响应、重同步、编码回执处理待接入，不虚构接口名或超时数值。长文本、数量缩写／精确提示沿用通用规则中的已定部分，其余待确认。

## 7. 脚本如何使用本 UI

| 项目 | 使用说明／接入状态 |
| --- | --- |
| Screen 文件、类名、注册入口 | `client/ui/template_panel.py` 的 `TemplatePanel`，`@RegistToolDeltaScreen("template_terminal_panel_ui.main")`；服务端 `PushUIRequest` 推开（`TERMINAL_SCREENS` 已加这一条） |
| namespace／根控件 | JSON 入口 `template_terminal_panel_ui.main`；四个格子区的集合名分别是 `endernet_template_source` / `_outputs` / `_input` / `_result` |
| 参数与可信来源 | 服务端推 `{device_ref, dimension}`；玩家从 `session.player_id` 取；配置与真实槽全部来自 `template_state`，客户端不自算 |
| 初始化及同步 | 先挂收包再开会话；`TemplateView.apply` 挡旧修订号；四个区、模式、两个选中框与标题**一律来自推送** |
| 虚格事件分流 | 背包或网络列表有选中 → 点虚格 = `template_cell_put`（复制配置），**回执成功后两侧的选中一起清掉**（一次放入 = 一次来源选择用完）；选中虚格 → 点背包槽或网络行 = `template_cell_clear`；**按住虚格够久（0.4 秒）当场**弹数量窗 = `template_cell_count`（上限取自推送的 `limits`），不等松手 |
| 编码操作 | `template_encode`；服务端重新校验输出门限（空或同配置且有空间）与输入槽类型后写入 |
| 公共复用 | 通用列表、背包、状态与排序沿用相关组件；四条绑定名各有各的区，**不共用** |
| 关闭清理 | 关数量窗、清选中与在途；销毁时摘掉协作口子（`TerminalPanelBase.config`）；不生成虚拟返还物 |

实际调用示例：界面由服务端 `PushUIRequest` 打开，不需要调用方拼参数；
手工自查可用 `TemplatePanel.OpenDemo()`（**没有权威身份，四个区全空**）。

## 8. 待确认项与验证记录

旧缺项记录见 [11-05/11-06 UI 缺项与等待交接](../tasks/11-05-11-06-UI缺项与等待交接.md)。本次已确认模式、虚实槽分工、合成输出限制及编码条件；旧记录不能视为接入结论，当前 JSON 的变化以本次静态核对为准。

| 问题 | 当前结论／影响 |
| --- | --- |
| 工厂模式输出配置 | **已定**：与输入九宫格同一套手势（背包选中物放入／长按改数量／选中→点外界清空）；第一格主产物、其余副产物（**只是显示顺序**，不进数据） |
| 输入数量与虚格交互 | **已定**：按住虚格够久**当场**弹数量窗（1～65535，不等松手）；放入后清掉来源选中；覆盖=直接覆盖；虚格之间不支持复制／交换；再点同格=取消 |
| 合成配方匹配 | **已定**：沿用 11-04 的精确匹配口径（**九宫格里多摆的任何一件都让配方不成立**，一块木板加一颗钻石做不成按钮）；多候选**只取引擎给的第一条**（不重新排序）；无配方 → 中间格为空且拒绝制作 |
| 物品类型与编码件数 | **已定**：两种模式编出的物品**恒为工厂样板**（模式记在 `userData` 里）；输入槽空白与已编码都收；单次消耗 1、产出 1 |
| 生命周期 | **已定**：虚配置**服务端按设备存**（关界面/重开保留）；切模式保留输入九宫格、只清输出格；拆方块时真实槽的实物**掉落为掉落物** |
| 虚槽模板与事件 | **已接入**：`template_item_slot` 换成 `item_can_be_selected` 的薄包装，两个真实槽改成 1×1 的 grid；三按钮已有事件名与映射 |
| 网络集合冲突 | **已解决**：`outputs_grid` 的集合改名 `endernet_template_outputs`，本文件里不再出现 `test_grid` |
| 通用功能差异 | **已接入**：网络列表换成本面板自己的薄包装，遮罩补了绑定，两个排序按钮已有映射。**仍差一条**：没有搜索框（交接时就没画，本文 §1 那句「通用功能继承」按现状理解为不含搜索） |
| 制作回执及异常 | 输出门限（空或同配置且有空间）已定；**失败原因仍没有显示位置**、等待表现与并发处理待接入 |
| **编出的样板 `returns` 恒为空** | **本轮如实记为缺口**：R09 要求返还物显式声明，而本界面没有承载它的控件（交接未提）。不新增控件，也不在服务端按输入推导 |
| **两条新引擎面** | `GetRecipesByInput`（配方按输入查）与 `CreateEngineItemEntity`（掉落）**都没有真机证据**，见 [欠账台账](../tasks/00-欠账台账.md) D-09 / D-10 |
| **工厂样板堆叠 64** | User 2026-10-02 定的玩法数值；**引擎会不会把两块不同配置的样板并成一叠没有量过**，见 [欠账台账](../tasks/00-欠账台账.md) D-12 |

验收清单：

- [x] 记录 User 模式互斥、虚槽复制／清空、合成模式三个输出格与真实样板制作条件。
- [x] 静态核对模板、相对路径、collection、维度、初始框显隐与按钮映射。
- [x] 接入 Screen、独立虚拟配置、真实槽同步、配方检测、编码命令与事件。
      **2026-10-02：五样都做了**（服务端见交付说明第二十四节第 2～4 节，
      客户端见第 5～6 节）。**勾的是「接上没有」，不是「跑通了」**——这一条
      列的恰恰是离线可验的五样。
- [ ] 游戏验证两模式来回切换始终仅一个选中框，标题同步。
- [ ] 验证外界 → 虚格真实源数量不变，虚格 → 外界仅清空且目标／网络数量不变。
- [ ] 验证合成模式索引 0／2 不显示、1 自动匹配；无配方时中间格**置空且不留上一份产物**，三格均不能取出。
- [ ] 验证输入拒绝非工厂样板、输出占用不制作、合法制作后真实物品不复制／丢失。
- [ ] 验证搜索／排序／存取继承、集合隔离、离线遮罩、关闭重开、移动端及异常回执。

| 日期／验证人 | 环境与方式 | 结果／证据 | 未验证项 |
| --- | --- | --- | --- |
| 2026-10-02／Codex | 当前 JSON、公共列表模板、通用终端规则、定义与本次截图静态核对 | 完成同名文档；明确虚实分工、索引、模式约束和服务端制作条件；记录尚未交接及资源差异 | 本次仅修改 Markdown，未改 JSON／脚本、未运行离线代码门禁、未启动游戏；截图为交接参考，不计真实存取验收 |
| 2026-10-02／Claude Code | 脚本接入 + 离线用例（3195 项全绿、py2 门禁 99 文件）与本文件的静态核对 | 屏幕已注册、四区绑定与八条命令已接；JSON 的 22 处改动逐条见 [交付说明](../tasks/11-交付说明.md) 第二十四节第 5 节 | 当时未运行游戏 |
| 2026-10-02／Claude Code（**真机**，User 的测试世界） | 整包重载后真人右键 + `execute_code` 读服务端/运行时 + 截图，逐项对账 | **实测通过**：打开入口、服务端状态投影（9/3/1/1、屏障锁定、limits、堆叠 64）、四区渲染与图标、单格选中、放入配置（20 条命令零拒绝）、按住即弹数量窗、放入后清选中、保活（`keepalives: 11`）。**当场撞出并修掉四处**：拼法不一致、集合名三处不一致（九格一起亮）、过线名字被当非文本（无图标）、终端面无 keepalive（20 秒掉线）。逐条见 [交付说明](../tasks/11-交付说明.md) 第二十四节第 11～12 节 | **未测**：制作与两个真实槽、模式切换、合成配方自动检测、遮罩、关闭重开、拆方块掉落、堆叠 64 的两块样板是否串内容、移动端 |
