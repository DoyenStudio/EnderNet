# common_terminal_panel.json：通用终端

> **2026-10-03 新增需求（待实现）：** 网络列表新增样板产物行；有库存且有样板显示 stored+（如 256+），无库存有样板显示 +。带 + 的行长按打开数量窗口，确认数量后进入分帧订单预览，缺料标红并禁用下单。此规则覆盖本文旧的带样板条目数量口径；背包与短按存取仍复用共享终端组件。 完整规则见 [网络样板标记与下单预览契约](../technical_notes/terminal_pattern_order_preview.md)。

[返回目录](README.md) · [UI JSON](../../endernet_rp/ui/common_terminal_panel.json)

对应 namespace：`common_terminal_panel_ui`；屏幕入口：`common_terminal_panel_ui.main`。

UI 交接：2026-09-29 已确认本文中的控件用途与显示规则，2026-09-30 补齐下方真实背包的槽位映射与操作流程（第 4、5 节），2026-10-01 增加**整槽存入**的第三条手势（点列表空白处）与**槽位之间的搬运动画**；脚本接入：下方**真实**玩家背包、上方网络列表、搜索、排序、连接遮罩、空白处按钮与飞行动画均已接入（11-03）；游戏验证：待验证——本文件记录的一切渲染、事件与真实存取均**尚未在游戏中实测**。

## 1. 制作范围与要求

上方滚动区域是终端物品列表，使用自定义槽位网格显示终端内所有物品；搜索时显示筛选结果。下方复用已有玩家物品栏组件及自定义槽位渲染、交互逻辑。上方槽位同样支持类似普通物品槽的交互，不是仅供展示的图标。

网络列表首版只显示物品，不显示流体。同一 ResourceKey 汇总为一个条目，特殊元数据不同的物品不误合并；可用数量与预留数量须区分。网络库存范围和同步契约见 [物品列表实施说明](../technical_notes/terminal_item_index.md)，功能依据见 [Task 11](../tasks/11.%20终端与客户端同步.md)。

本文记录用户确认的 UI 规则及当前实现证据，不将已有槽位逻辑记为已完成真实网络存取。**2026-09-30 起下方背包渲染的是真实玩家背包**，因此本文里出现的「模拟」字样只在第 5 节关于旧口径的历史说明中还有意义；`InventoryModel` 已无生产消费者。

## 2. 资源与交接信息

| 项目 | 记录 |
| --- | --- |
| UI 制作者／交接日期／资源版本 | User／2026-09-29／本次工作区 JSON 与用户提供的“通用终端面板”截图 |
| 本次交接范围 | 上下两区用途、搜索条件、排序按钮图标与切换、上方槽位交互、连接遮罩显隐 |
| 根控件 | `main` 为独立 screen，实例 `terminal@common_terminal_panel_ui.content`；内容面板大小 180×220 |
| 适用设备／打开入口 | 通用终端；已接真实入口（方块交互 → `PushUIRequest` → 界面拿 `device_ref` 开会话），另有 `CommonTerminalPanel.OpenDemo()` 演示入口——它只开一个空会话，上方列表恒为空，下方显示的是**玩家自己的真实背包**（读引擎，不依赖会话） |
| 公共模板 | `common.scrolling_panel`、`endernet_container_common.custom_slot`、`endernet_terminal_inventory.inventory`、`endernet_ui_common.device_stat_bar`、`endernet_ui_common.close_btn` |
| 资源登记与文本 | 已列入 `_ui_defs.json`；标题为 `tile.endernet:common_terminal_panel.name`，背包标题为 `container.inventory`；搜索占位文本当前为“搜索物品..” |

## 3. 控件功能与路径

以下 `/terminal/...` 是按当前 JSON 与脚本核对的路径；仍须游戏内验证。滚动模板展开后的 grid 完整路径未核实，不猜测引擎内部层级。

| 控件／模板定义 | 实例路径 | 类型与功能 | 脚本读取／更新内容与初始状态 |
| --- | --- | --- | --- |
| `content.item_search_editor` | `/terminal/item_search_editor` | edit_box，上方搜索栏 | 读取输入文本；`max_length=512`，筛选规则见第 4 节 |
| `content.terminal_items_scroll` | `/terminal/terminal_items_scroll` | scrolling_panel，上方物品滚动区，162×80 | `$scrolling_content=common_terminal_panel_ui.terminal_items_grid`；`$scroll_background_image_control=common_terminal_panel_ui.terminal_scroll_background`（见下一条） |
| 上条展开后的 `background` | `/terminal/terminal_items_scroll/scroll_mouse/scroll_view/stack_panel/background_and_viewport/background`（触控端把 `scroll_mouse` 换成 `scroll_touch`） | **隐形按钮**（`endernet_ui_common.simple_invisible_button` 的薄包装，只覆盖 `$pressed_button_name`），覆盖整个列表区 | 点击 = `#EnderNetTerminal.quick_store`，行为见第 5 节；**格子画出来的地方归格子**，没有格子的地方（含空格子）才落到它 |
| `terminal_items_grid` | 上述 scroll 的 `scroll_view` 内容，**实例路径待核实**（在 `scroll_mouse` / `scroll_touch` 之下分叉） | grid，自定义网络物品槽网格 | collection 为 `terminal_slots`，条目由 `#EnderNetTerminal.count` 驱动（见第 4 节） |
| `content.flying_item` | `/terminal/flying_item` | custom（`endernet_container_common.flying_item_renderer`，`renderer=inventory_item_renderer`），槽位之间的**搬运动画** | 初始 `visible=false`；搬运时由脚本摆到源槽并逐帧飞向目标槽（`client/ui/flying_item.py`），先快后慢、到点收起。时长是手感值（`FLY_SECONDS`，初值 0.5 秒，2026-10-01 实测后按 User 要求调快 4 倍） |
| `content.terminal_items_scroll_frame` | `/terminal/terminal_items_scroll_frame` | image，滚动区边框 | 纯装饰，不作为交互槽 |
| `content.inventory` | `/terminal/inventory` | 复用终端玩家物品栏 | 下含 `backpack` 和 `hotbar`，由已有槽位逻辑渲染 |
| `content.sort_method_btn` | `/terminal/sort_method_btn`，图标为其 `/icon` | button，切换排序方式 | 默认图标 `uv=[0,0]`，按 identifier 排序；点击切换至数量排序及切回 |
| `content.sort_order_btn` | `/terminal/sort_order_btn`，图标为其 `/icon` | button，切换排序方向 | 默认 `uv=[16,0]`，降序；点击在升序与降序间切换 |
| `content.unavailable_mask` | `/terminal/unavailable_mask` | input_panel，未连接遮罩 | 连接网络时隐藏，未连接时显示；该 input_panel 下那层变暗图是 `mask_img`（半透明黑色 image） |
| `content.stat_bar` | `/terminal/stat_bar` | 公共设备状态栏 | 遵循 [公共控件文档](endernet_ui_common.md)；截图中的“未知状态”不是已完成的运行时状态绑定 |
| `content.close` | `/terminal/close` | 关闭按钮 | `#EnderNetTerminal.close`；已有关闭回调 |

遮罩当前 JSON 为 `visible=false`、layer 20，底部锚定，大小为 `100% × (100%-20px)`，含半透明黑色变暗图（子控件 `mask_img`）。静态 `false` 只是资源初值，接入后应在首次取得连接状态及连接变化时更新。关闭按钮 layer 30，应保持可关闭；左侧排序按钮位于面板外，不能仅凭遮罩层级认定所有输入已被拦截，须游戏验证。

## 4. 槽位、搜索与排序

### 槽位及组件复用

| 槽位组 | 模板／collection | 数据与索引映射 | 显示要求 |
| --- | --- | --- | --- |
| 上方终端物品 | `endernet_container_common.custom_slot`／`terminal_slots` | 网络物品汇总；显示索引映射到筛选排序后的稳定 ResourceKey | 能滚动浏览全部条目，不以真实物品堆叠上限拆成大量槽位；交易不能只发送显示序号 |
| 下方背包 | `endernet_terminal_inventory.backpack_slot`（实例化 `endernet_windows.item_can_be_selected`，覆盖六个 `$` 变量）／`endernet_terminal_backpack` | 9×3；局部索引 0～26 → **引擎槽 9～35**（背包 27 格） | 内容来自真实背包，只读镜像；格子由**集合绑定**逐格取值，无控件引用 |
| 下方快捷栏 | `endernet_terminal_inventory.hotbar_slot`（同上，另一套绑定名）／`endernet_terminal_hotbar` | 9×1；局部索引 0～8 → **引擎槽 0～8**（快捷栏） | 同上 |

**下方两套槽序正好相反**（2026-09-30 决策 D2）：引擎给的是**快捷栏在前**（0–8 快捷栏、9–35 背包），而界面是**背包在前**（上 9×3 是 0–26、下 9×1 是 27–35）。`#collection_index` 在**各自集合内**从 0 起，因此「界面 27～35」是界面自己的编号，不是引擎槽号。换算只有一处定义：`client/ui/backpack_model.py` 的 `engine_slot_of`；**写反了不会报错**——客户端用同一张表同时算槽号与指纹，服务端的锚只回答「这个槽里是不是这件东西」，自己和自己必然一致，症状是**搬走玩家没点的那一格**。见 [终端背包组件](endernet_terminal_inventory.md) 第 1 节。

当前上方 grid 声明 `grid_dimensions=[9,2]`、`maximum_grid_items=30`，这些静态值不是终端库存条目上限。11-03 起 grid 带 `property_bag={"#maximum_grid_items": 0}` 与 global 绑定 `#EnderNetTerminal.count → #maximum_grid_items`，条目数改由脚本报的**筛选后行数**驱动；静态的 `30` 只是资源初值。**引擎是否真的按这个绑定突破静态上限尚未实测**——若它被静态值卡住，超过 30 种物品时列表会静默截断，而界面上看不出少了东西。验收必须覆盖超过 30 种物品，并把这条件为游戏内实测项。

**上下两区的格子都不持有控件引用**，一律由引擎的集合绑定逐格回调（见第 6 节）。下方两段走同一个共享模板 `endernet_windows.item_can_be_selected`，靠六个 `$` 变量分成两套（`endernet_terminal_inventory.json` 里的 `backpack_slot` / `hotbar_slot`）：`$item_collection_name`、`$item_visible_binding`、`$item_id_aux_binding`、`$item_selected_binding`、`$item_count_binding`、`$item_select_button`。绑定名由 `terminal_panel.slot_binding(前缀, 后缀)` 生成，两段的前缀分别是 `backpack` / `hotbar`——**必须分段**：集合绑定的回调只拿得到集合内下标、拿不到集合名，共用一套名字会让快捷栏那 9 格按背包的下标去读。

早先的实现按控件路径逐格取格子（`grid.GetGridItem`）并缓存子控件（`CustomSlotView`），**已删除**：格子要到**渲染**时才生成、滚动时还会被回收换出，`OnCreate` 里第一步就 `ValueError: Can't initialize UBaseCtrl: comp is None`，整块界面建不起来（ModSDK 的网格文档也不建议按路径操作格子）。共享模板 `endernet_container_common.custom_slot` 因此**不再被本界面使用**（其余引用它的界面照旧，见 [custom_slot 说明](../technical_notes/custom_slot.md)）。

当前数量标签仅在数量大于 1 时显示十进制文本；网络大数量缩写、精确提示及可用／预留数量的具体承载位置尚待补齐，不能记为已有实现。**标签的 `text` 必须写成 `#text`**，与 `binding_name_override: "#text"` 配对：只写 override 时值确实被写进了 `#text` 属性，但标签渲染的是自己的 `text`，没有东西去读那个属性——界面上是空白，而绑定一直在被调用（2026-10-01 真机撞过，见第 8 节）。标签的 `layer` 同样要压在 `item_renderer` 之上，否则整块物品的图标会把数字盖掉。

滚动、搜索、排序或增量更新导致槽位重绑时，必须清理旧图标、数量、耐久、选中态及点击身份。无物品或无匹配结果时显示空列表，不保留旧物品；重新排序后的点击仍须指向当前槽位绑定的资源。

### 搜索

1. 从上方搜索栏读取文本，以 `text.strip()` 得到关键词。
2. 关键词为空（包括空串、纯空格等 strip 后为空的输入）时显示终端全部物品。
3. 关键词非空时按关键词筛选终端物品，仅显示匹配条目；清空输入恢复全量列表。
4. 筛选结果继续使用当前排序方式和方向；搜索与排序只影响上方列表，不重排玩家物品栏。

关键词匹配字段（显示名、identifier 或两者）、大小写及多关键词语法本次未指定，保留待确认，不自行视为已定接口。过滤针对完整库存摘要，不能仅过滤当前可见槽位。

### 排序按钮与图标

图标由按钮直接子控件 `icon` 的 `uv` 控制；两个图标均为 `uv_size=[16,16]`，切换时保持 y=0。

| 按钮／图标纹理 | `uv=[0,0]` | `uv=[16,0]` | 初始值与点击行为 |
| --- | --- | --- | --- |
| `sort_method_btn/icon`／`textures/ui/endernet/terminal_item_sort_methods_icon` | 按物品 identifier 排序 | 按物品数量排序 | 当前资源初值为 identifier；每次点击切换方式并刷新结果和图标 |
| `sort_order_btn/icon`／`textures/ui/endernet/sort_order_icon` | 升序 | 降序 | 默认降序，与当前界面一致；每次点击切换方向并刷新结果和图标 |

方式与方向是两个独立状态：切换方式保留方向，切换方向保留方式。数量排序比较数值而非标签文本。identifier 相同时仍按稳定资源身份保持确定次序，不合并元数据不同的条目；数量排序所用字段（总库存或可用数）及相同数量的次序细节待确认——**11-03 的实现按总库存 `stored` 排**，而上方的数量标签显示的是可用数 `stored - reserved`，因此「排序看着不对」在这个差异定案前属预期现象，见第 8 节。

## 5. 操作流程与事件

| 操作 | 约定／当前实现 |
| --- | --- |
| 打开界面 | 初始化下方 36 格；上方列表**只在拿到了权威终端身份时才接**（`PushUIRequest` 带来的 `device_ref`），没有身份就不建视图、不占位、不伪造设备。有身份时按空搜索、identifier 与默认降序生成列表并发起会话。**2026-09-30 起限速**：同一个人两次打开之间至少隔 `TERMINAL_OPEN_LIMIT_SECONDS`（0.2 秒），窗口内再右键**静默忽略**——引擎的方块使用回调不节流，连点几下会把同一次打开做几遍 |
| 编辑搜索文本 | 读 `params["Text"]`，`strip()` 后为空显示全部（第 4 节）；同一文本重复回调不重排 |
| 点击排序按钮 | 切换对应状态，同步修改该按钮 `/icon` 的 UV 和列表顺序，不修改库存；UV 只在状态真变了时写 |
| 操作上方物品槽 | 点击 = 选中该行并把该行**一组**取进背包（一条 `inv_put`），一组 = `min(可用数, maxStackSize)`；可用数不足一组时能拿多少拿多少。可用数为 0 时只选中、不发命令。**不做防抖／连击合并**——那属 R12，本轮不发明。**2026-09-30 起多一条前置**：下方有选中时，这一下改发 `inv_take`（见下一行），且**无论发没发出去都不落到 `inv_put`** |
| 操作下方物品槽 | 点击只**选中**（不搬任何东西）：空格子点不动，同格再点取消。**动作发生在接着点的那一下**，看目标是谁：点**终端行** → `inv_take`（背包 → 网络），数量取那一槽的**一组** = `min(该槽数量, maxStackSize)`；点**另一个背包槽** → 背包内动作（同种且可合并 → **合并**、异种 → **交换**、目标为空 → **交换**即在移动）。选中态只记一个槽号（不持有物品，因此关闭界面没有悬空义务）。**2026-09-30 已接**：第二条分支现在会发 `inv_merge` / `inv_swap`，**且在命令发出后立刻把结果显示在画面上**（虚拟背包空间，见第 6 节与 `terminal_inventory_ui.md`）——因此**连点是允许的**，多轮操作会依次作用在那份虚拟快照上（仍**只离线验证**，见第 8 节） |
| 点上方列表的**空白处** | **2026-10-01 由 User 定的第三条手势**：选中了某个背包槽之后，点列表里没有格子的地方（含空格子）→ `inv_quick_move`（`target="network"`），把那一槽**整槽**存进网络。与「点终端行 = 一组」是**两条不同的命令**：这一条不截断到 `maxStackSize`，装不下的部分由服务端回 `PARTIAL`——「尽可能多」由服务端裁，客户端看不到网络余量。**没有选中时什么也不做**（不发命令、也不借用任何默认目标）。核账与 `inv_take` **完全同一套**（发之前重读那一格、对服务端快照核指纹与槽版本） |
| 物品搬运时的动画 | 三种搬运各画一次：背包内交换/合并**两端都落到真实格子**；背包 → 网络落到**该物品在网络里的那一行**（它还没有行时落到列表区中心）；网络 → 背包落到**背包那一半的整块区域**（服务端挑哪个格子客户端看不到，不假装知道）。**只在命令真的发出去之后**才画——「等待同步」的那几次点击画面上不动。动画只是表现，不参与任何判定 |
| 连接状态变化 | 连接网络隐藏 `unavailable_mask`，未连接显示；网络存取资格仍由服务端校验。11-03 的判据是**没有会话**或**正等着重同步的快照** |
| 关闭／失活 | 关闭／失活／销毁都清掉下方选中；关闭移除 UI，销毁清理视图缓存；上方列表额外摘掉全部绑定、注销收包并关闭会话。**未决请求在途时也一并丢弃**（选中已清，回执回来时按 `request_id` 认不出就不再处理）。正式订阅与真实持有物处理待接入 |

**下方不再有本地合堆、拆分或交换**（2026-09-30）：那些是旧 `InventoryModel` 的行为，随模拟背包一起从面板上撤掉了——搬运改由服务端做，客户端的合堆规则与它迟早会分家。分堆、拖拽、快捷键与光标持有物仍属 R12，本轮一条都没做。R12 的具体键位、触控与持有物退出规则同样按正式契约补齐。

> **2026-09-30 更正（原文保留，不删改）**：上面这句撤掉的是**客户端自己算合堆规则**这件事，**不是规则本身**。User 同日补定：**玩家背包的物品槽可进行物品交换、合并**（这是自实现物品栏的通用做法），终端用**同样的方式**从网络中取出与存入。上游 `blocks.md` 与 `architecture_analysis.md` 一直要求「服务端处理拆分、合并、取放及快捷转移」，从未取消——2026-09-30 上午撤掉的只是那份**与它并行的客户端实现**。手势沿用既有的「点选中 → 再点目标」，**不引入光标持有物**。详见 [Task 11](../tasks/11.%20终端与客户端同步.md) 文末补充段。

**R12 的「持有物在关闭/断线时的处理」这一半本轮天然不触碰**：第一次点击不搬走任何东西，只记一个槽号，所以关闭界面时没有任何需要善后的东西。载荷里 `cursor` 照旧**不发**（11-03 的决策）。**这不是顺手定了 R12 的退出规则**，是这条交互本来就不产生悬空的持有物；等 R12 接进「按住取半 / 右键取一个」时要重新审这一条。

未连接时不能用缓存余额提交有效交易；已连接也不等于能源、权限等交易条件全部满足。交易成功、部分成功、失败、超时与重同步均遵循 Task 11，不以本地结果回写权威库存。

上方点击的**数量口径是本轮定下的默认，不是已定案的玩法**：界面上没有任何输入数量的控件，而「取一个」与「取一组」都要先有一个约定。本轮取**一组**——它比「全部可用数」更接近玩家在物品栏里的手感，也不会一次点击就把网络里这一种抽干。`maxStackSize` 由 `Item(item_id).GetBasicInfo()` 给出，按资源名缓存一次；**引擎给不出合法上限时退成 1，不是退成可用数**，因为退成可用数正好会让「取一组」变成它要避开的那件事，而退成 1 最多是少拿。R12 定稿后若改为按住取半、右键取一个，改的是这一条。回执目前**没有显示位置**（见第 8 节），因此本轮只把回执取走留作诊断，不以它刷新界面上的任何东西——服务端的权威结果通过随后的增量同步回来。

## 6. 数据与绑定现状

下列绑定**全部写在 `TerminalPanelBase` 的类体里**（`@Binder.binding` /
`@Binder.binding_collection`），当前终端保留静态实现。2026-09-30 曾因旧 ToolDelta
动态包装回调未安装到注册宿主上，发生按名称查找失败、格子全空；类体绑定规避了
当时的缺陷。框架已于 2026-10-03 修复，动态 API 不再受此限制，见
[修复记录](../technical_notes/tooldelta_dynamic_binding.md)。静态终端的既有真机验收
与修复后动态绑定的离线验证分别记录。

代价是**静态绑定不能按会话摘除**：没有会话时引擎照样来问，所以每条列表回调都先看
`self.list_view is None` 并返回安全值，`list_view` 也必须有类属性兜底（绑定解析早于
`OnCreate`）。`TerminalListView.close()` 因此只置 `closed`、不摘绑定。

| 控件／数据 | 现有 JSON 或脚本 | 接入状态 |
| --- | --- | --- |
| 搜索框 | `$text_box_name=#EnderNetTerminal.search`；内容名 `#EnderNetTerminal.search_content` | `on_search_changed` 已以 `BF_EditChanged` 绑定 `#EnderNetTerminal.search`。`search_content` **没有脚本侧注册**——它与 11-09 的 `#EnderNetSelectItem.search_content` 同一情形，只是资源里声明的名字 |
| 上方 grid | `collection_name=terminal_slots`；`grid_item_template=common_terminal_panel_ui.terminal_slot`（薄包装 `endernet_windows.item_can_be_selected`，只覆盖六个 `$` 变量）；global 绑定 `#EnderNetTerminal.count → #maximum_grid_items` | 整表交给引擎的集合绑定（决策 D2）：`count` 报行数，引擎逐格回调四个集合绑定 `#EnderNetTerminal.has_item`／`.item_id_aux`／`.selected`／`.item_count` |
| 两个排序按钮 | `$pressed_button_name=#EnderNetTerminal.sort_method`／`.sort_order`；`button_mappings` 为 `button.menu_select` pressed → `$pressed_button_name` | `on_sort_method`／`on_sort_order` 已以 `BF_ButtonClickUp` 绑定 |
| 两个排序图标 | 按钮 `/icon` 的静态 `uv`、`uv_size`（方式 `[0,0]`、方向 `[16,0]`） | 已按第 4 节动态更新：`on_sort_*` 后写 `SetUV`，**只在状态真变了时写**——每帧写会让引擎每帧重新切图 |
| 点击格子 | 格模板的 `$item_select_button=#EnderNetTerminal.select` | `on_select` 已以 `BF_ButtonClickUp` 绑定；索引取自 `#collection_index`，越界／非整数一律当无效 |
| 下方背包／快捷栏 | `$item_select_button` = `#EnderNetTerminal.backpack_click`／`.hotbar_click`，collection_details 提供 `#collection_index` | `OnBackpackClick`／`OnHotbarClick` 已以 `Binder.BF_ButtonClickUp` 绑定；点击前先重读引擎背包（本地读，不走协议），越界／缺项／非整数索引一律当无效 |
| 下方 36 格的数据 | 每段四个集合绑定：`#EnderNetTerminal.{backpack,hotbar}_visible`（bool→`#visible`）／`_item_id_aux`（int）／`_selected`（bool）／`_item_count`（string） | 八条都写在 `TerminalPanelBase` 的类体里（`backpack_*` / `hotbar_*` 八个具名方法，各转手调 `_slot_*(段, 下标)`；段常量写死在各自方法里），名字由 `terminal_panel.slot_binding(前缀, 后缀)` 拼出。**两段各一套名字**：集合绑定的回调拿不到集合名，共用一套会让快捷栏按背包的下标去读。`_visible` **恒真**——空格子也要画槽底，空是用 `#item_id_aux == 0` 表达的 |
| 列表空白处（滚动区背景） | `terminal_scroll_background` 的 `$pressed_button_name=#EnderNetTerminal.quick_store` **＋同节点的 `button_mappings`（`button.menu_select` pressed → `$pressed_button_name`）** | `OnQuickStoreClick` 已以 `BF_ButtonClickUp` 绑定，**转发**给 `TerminalListView.on_quick_store`（背包与命令的账在列表视图那边）。**按钮名只能写在专用模板里**：`common.scrolling_panel` 实例化 `background` 时不接受外部变量，用共享模板的默认名（`%netease_editor_template_namespace.click`）会让这一下查不到绑定、**安静地什么都不做**。<br>**2026-10-01 真机撞出来的后半条**：光有名字不够。`simple_invisible_button` 模板的 `button_mappings` 是空的，override 里只写了 `$pressed_button_name`，于是「名字有了、却没有一条映射把 `button.menu_select` 接到它上面」——**点下去同样安静地什么都不做**，而 Python 侧绑定注册得好好的、JSON 名字也逐字对得上。修法：实例里把 `button_mappings` 一起补上（与 `close`／两个排序按钮同一形状）。全仓扫描确认只有这一处，并由 `test_terminal_panel.TestPressedNamesAreActuallyClickable` 扫全部界面 JSON 兜住 |
| 关闭／系统返回 | `#EnderNetTerminal.close`，分别来自 menu_select／menu_cancel 的 pressed 映射 | `OnCloseClick` 已绑定 `BF_ButtonClickUp` |
| 搬运动画 | `content.flying_item`（`#item_id_aux` 由脚本按**资源身份**设，`name` + `aux`） | `client/ui/flying_item.py` 逐帧推；坐标由 `client/ui/slot_layout.py` 现算（**不缓存**：滚动、缩放、条目数一变坐标就变）。`TerminalPanelBase.fly` 的 `diagnostics()` 里 `errors` 是唯一能回答「动画为什么不动」的数。<br>**落点分两类**（2026-10-01）：**存入网络**那一向（`inv_take` / `inv_quick_move`）落到**玩家点的那一下在哪儿**（`slot_layout.click_rect(params)` 读绑定回调的 `TouchPosX/TouchPosY`，UI 坐标，与 `GetGlobalPosition()` 同一套；取不到才退回 `_network_rect`）；**从网络取出**那一向落背包整块区域（服务端把东西放进第一个装得下的格子，客户端看不到是哪一格，**不假装知道**）。落点一律是「把 16×16 的图标摆在目标矩形正中」——槽位因此内缩 1 像素，整块区域因此落在中心，见第 8 节 |
| 连接遮罩 | 绑定 `#EnderNetTerminal.unavailable → #visible`（global）；静态 `visible=false` | `is_unavailable` 已接：没有会话，或正等着重同步的快照时显示 |

真实物品身份使用 ResourceKey；终端实例、玩家、网络会话及交易预期版本由可信上下文取得，不使用显示索引替代。快照、增量、查询代次、过期响应丢弃和订阅清理按 [物品列表实施说明](../technical_notes/terminal_item_index.md) 实施；本文不另设虚构绑定名或 API。

命令通道本轮定稿：客户端发 `command`（`TerminalCommand`），服务端回 `command_result`（`CommandResultMessage`，载荷是 `CommandResult.to_dict()`）。**来源与目标不在消息里**——玩家、终端与维度都由会话结构性给出，再加一份就是第二个真相源；`cursor` 字段**不发**，因为 R12 未定，缺席不等于「客户端声称手上没拿东西」。详见 [Task 11](../tasks/11.%20终端与客户端同步.md) 的「接口与交付」。

## 7. 脚本如何使用本 UI

现有代码：[terminal_panel.py](../../endernet_bp/endernet_scripts/endernet/client/ui/terminal_panel.py)。`CommonTerminalPanel` 继承 `TerminalPanelBase`，注册为 `common_terminal_panel_ui.main`。分两半：

*上方列表* 由 `TerminalListView` 承担，它把 `client/ui/terminal_cache.py` 的 `TerminalCache` 接成一份可见列表。每帧在 `OnTicking()` 里推进一次（引擎的 `Update → ScreenNode.Update → _on_ticking → OnTicking`），顺序是「先记锚点、再收包、最后按需重画」——反过来的话，读到的是换过之后的顶行，锚点就锚到别人身上了。缓存与视图**一一对应**（`TerminalCache.close()` 不幂等，两个视图不能共用一份）。

*下方背包* 由 `client/ui/backpack_model.py` 的 `BackpackModel` 承担，读的是**真实玩家背包**（`GetLocalPlayerHotbarAndInvItems(get_user_data=True)`，显示本地读；S-15 另同步服务端槽版本）。视图每 12 帧重读一次（`BACKPACK_POLL_FRAMES`，**帧数不是秒**），点击时另外立刻重读一次；两者的分工是「平时跟上变化」与「发命令前把过期窗口压到最小」。**读失败时整块保持上一帧**——把玩家的背包画成空，比闪一下严重得多。

打开入口本轮接了最小真实链路：服务端方块交互 → 权威 `DeviceRef` → `PushUIRequest`，界面从 `params["device_ref"]` 拿到设备身份后开会话。**没有权威身份就没有上方列表**，不占位、不伪造。手工检查可用演示入口：

```python
# 导入路径按调用者所在包调整；本例为客户端包内调用。
from .ui.terminal_panel import CommonTerminalPanel

screen = CommonTerminalPanel.OpenDemo()   # 空参数开会话：上方恒空，下方是真实背包
```

此入口**不再注入假物品**：它曾用 `items` 参数演示模拟背包的合堆溢出与异类交换，那套语义随模拟模型一起没了；`items` 参数与 `SetInventoryItems` 写入口已删除（留一个没人用、又会诱导别人写本地假数据的写入口，比删掉更贵）。`GetBackpackItems()` 返回当前镜像的 `(资源名, 数量)` 快照，只作诊断。

**注意 2026-09-30 的语义变化**：这个界面现在会**真的把玩家的东西搬走**（一条 `inv_take` / `inv_put`，由服务端裁决并回执），不再只是演示。「客户端不持有权威状态」说的是**不做裁决**，不是「不改动世界」。

尚未接入：11-10 的虚拟槽位控件池（本轮是引擎集合绑定整表，控件数随条目数增长）、回执的显示位置、**背包槽之间的拆分**（`inv_split` 的服务端命令早已实现，客户端仍未接——它天然需要按住取半一类的组合手势，属 R12，见第 5 节与第 8 节）、R12 剩余的手势与持有物规则。**交换与合并已接**（2026-09-30）；**快捷转移已接**（2026-10-01，入口是「点列表空白处」，不是组合键）。动画一侧的模块分工见第 5 节与 [11-交付说明](../tasks/11-交付说明.md) 第十四节。具体复用说明见 [终端背包 UI 说明](../technical_notes/terminal_inventory_ui.md) 和 [终端背包组件](endernet_terminal_inventory.md)。

## 8. 待确认项与验证记录

| 缺项／问题 | 当前结论 |
| --- | --- |
| 关键词匹配范围与大小写规则 | 待确认；strip 后为空显示全部已确定。11-03 的实现是**显示名与资源名的子串匹配、大小写不敏感**（`ItemEntry.search_text` 把两者拼在一起），这是 `ItemListModel` 的既有口径，**不是定案** |
| 数量排序字段及同数量次序 | 待确认；identifier／数量两种方式及 UV 约定已确定。当前实现按**总库存** `stored` 排序，而数量标签显示**可用数** `stored - reserved`，两者不是同一个量 |
| 网络数量、预留量和精确提示布局 | 待补齐；当前公共渲染器只有基础数量标签。**可用数**复用既有数量标签（决策 D4），**预留数没有显示位置**——不新增控件、不拼字符串蒙混，记为缺项 |
| 滚动 grid 完整实例路径与动态条目承载 | 待接入并游戏验证；静态 30 不能成为列表上限，`#maximum_grid_items` 的运行时绑定能否突破它尚未实测 |
| 上方点击、上下区真实存取、引擎背包槽映射 | 均已接（2026-09-30）：上方一条 `inv_put`、下方有选中时一条 `inv_take`，两处的「一组」共用 `client/ui/stack_size.py` 一份定义；槽位映射见第 4 节。**均未游戏内实测** |
| 下方背包的数据来源与刷新时延 | 显示仍客户端本地读引擎背包；S-15 为版本校验新增服务端快照，替代原 D3 的“不新增协议”边界。界面打开期间本地那份是否仍更新、服务端写入后多久能看到，**未验证**；因此点击时会再重读一次，把过期窗口压到最小 |
| 槽版本校验 | **D-06 已修复（S-15）**：玩家端点缓存与同步共用台账，`inv_take` 回传服务端真实槽版本；无匹配快照不发命令。只能识别已观察到的变化，游戏内未验。见 [交付](../tasks/06-11-交付说明-背包槽版本.md) |
| 命令回执的显示位置 | 待补齐；11-03 只把回执取走留作诊断，界面上**没有任何控件显示它** |
| 缺包超时 | 未测量政策，留给 11-14；本轮不编毫秒数（全局约束 7），只把在途批次暴露给诊断 |
| R12 键鼠、触控及持有物退出规则 | **2026-09-30 部分已定**：网络存取与背包内交换/合并的手势＝「点选中 → 再点目标」；选中态只记槽号、**不产生持有物**，`cursor` 照旧**不发**。**仍待定**（不由本文擅自补设）：拖拽、长按、鼠标键位/组合键、触控对应操作，以及**将来若引入真实持有物**时的关闭/断线处置 |
| 背包槽之间的交换／合并 | **已接（2026-09-30）**。判定：同种且可合并 → 合并（数量在本地就封在「目标剩余空间」内）；异种或目标为空 → 交换（**与空槽交换即移动，不是第六个动作**）。服务端命令 `inv_merge` / `inv_swap` 同日补齐，协议零改动。**仅离线验证**：`SetInvItemExchange` 真机没跑过。见 [欠账台账](../tasks/00-欠账台账.md) **S-16** 与 [11-交付说明](../tasks/11-交付说明.md) 第十三节第 9 小节 |
| 背包槽之间的拆分与快捷转移 | **2026-10-01 部分已接**：`inv_quick_move` 接上了（点列表空白处整槽存进网络，见第 5 节），`inv_split` 仍**未接**——它天然需要组合键或长按，属 R12，`client/` 下没有生产调用点。**整槽搬的键位/触控映射仍属 R12**：这一轮只定了「点空白处」这一种入口，按住 Shift 点击之类的既有习惯没有实现 |
| 列表空白处那一下的具体边界 | **未验证**。已知的实现口径：格子画出来的地方归格子（`item_can_be_selected` 的 `interact_btn`），**空格子的整格不可见**、那一下因此落到背景按钮上；滚动条、列表区之外的点击不受影响。**但「空格子那一下到底落到谁身上」「背景按钮会不会吃掉滚轮/拖拽滚动」都只能在游戏里看**——按钮带 `is_handle_button_move_event`，与滚动共存的取舍没有实测过 |
| 搬运动画的落点与观感 | **2026-10-01 已实测观察到（部分）**：`asItemRenderer().SetUiItem()` 画得出来、`GetGridItem` 在点击那一刻取得到格子、`GetGlobalPosition()` 与 `SetPosition` 确实是同一套（相减即父节点内坐标）——三条原先列在这里的不确定处都成立。实测暴露两处偏差，均已修：①图标**整整偏出半个物品**（两轴各约 9px），根因是脚本拿 `GetSize()` 去算居中，而这个 `custom` 控件回的是 **0** 而不是模板声明的 18×18（已改成不读它，见下条）；②飞行**偏慢**，已按 User 要求调快 4 倍。**修复后未再验证** |
| 落点算的是**图标**还是**槽位控件** | **2026-10-01 User 定案：按 16×16 权威算。** 槽位控件 18×18，槽里那张图标 16×16 且在控件里**居中**（`custom_slot` 明写 `[16,16]` + `anchor: center`；`item_can_be_selected` 写 `100%+-2px` 且不写 anchor 即默认居中，见 [custom_slot 说明](../technical_notes/custom_slot.md) 第 1 节），因此两者差 `(18-16)/2 = 1` 像素。规则因此是「**把 16×16 的图标摆到目标矩形的正中**」：槽位 ⇒ 内缩 1；背包／列表**整块区域** ⇒ 落在区域中心（此前落在左上角，与本文档说的「落到列表区中心」是两回事）；**退化成一个点的点击处** ⇒ 原样用那一点（比图标还小的矩形一律不动它）。尺寸只认一份常量（`flying_item.ICON_SIZE`），JSON 契约用例把它与两份槽位模板一起钉住。**2026-10-01 整包重载后在运行实例上核对过三个分支，用户随后目测动画、回报「很准确」**——1 像素的落点差只能由眼睛定，因此这里记的是目测通过。仍待验：控件的 `GetSize()` 回 0 是不是普遍现象 |
| 滚动区内部的路径 | **两处待核实**：①`/terminal/terminal_items_scroll` 到底能不能 `asScrollView()`（它自己是 `common.scrolling_panel`，一个 panel；既有代码这么写、但滚动锚定从未验过）；②`scroll_view` 那一层是 `scroll_mouse` 还是 `scroll_touch`。`client/ui/slot_layout.py` 对这两条**都做了候选与回退**，因此取不到时不会抛——只会不画动画（`network_row_rect` 回 `None`，落点退到整块列表区） |
| 打开界面的限速窗口 | **已定**：0.2 秒，按玩家（User 2026-09-30）。写在 `common/definitions.py` 的 `TERMINAL_OPEN_LIMIT_SECONDS`。**游戏内未验证**——0.2 秒是拍的，要在真机上试「连点几下会不会真的只开一个」 |
| 遮罩输入拦截及状态详情可访问性 | 待游戏验证，特别是面板外的排序按钮 |

验收清单：

- [x] 已记录用户确认的控件用途、搜索规则、排序 UV、默认降序与遮罩显隐。
- [x] 已静态核对 JSON、公共槽位模板和现有 Python，实现与待接入部分分别记录。
- [x] 完成搜索、排序、连接遮罩和上方库存／槽位事件接入（11-03，离线测试通过）。
- [ ] 验证空输入、纯空白、前后空白关键词、无匹配及清空恢复。
- [ ] 验证两种排序方式与两个方向共四种组合，图标与顺序一致，下方背包顺序不变。
- [ ] 验证 0、1、超过一屏及超过 30 种物品均可正确浏览；滚动和重排后点击无旧身份残留。
- [ ] 验证初次未连接显示遮罩、连接隐藏、断开重新显示，关闭与状态查看可用。
- [ ] 验证上下区域槽位交互、真实存取回执、移动端、大数量、异常状态、关闭重开。
- [ ] 验证下方真实背包：格子画的是引擎说的那一格（**逐格实点，看背包少了哪一格，不看日志**）、两组槽序没有颠倒、点击只选中不搬运、有选中时终端行发的是 `inv_take`、被拒时选中被清且界面重读、关闭重开无残留选中。
- [ ] 验证**点列表空白处**：选中背包槽后点列表里没有格子的地方真的把整槽存进网络（不是一组）；点**空格子**那一下也落到同一个按钮上；空白处点击**不影响**滚轮/拖拽滚动。
      **2026-10-01 部分验证**：命令那一段是真的（直接调 `OnQuickStoreClick` 时 `inv_quick_move`
      发了出去）；**缺的那一环（`button_mappings`）也已补上并热重载**。但**这一下仍然没有用手
      真的点过**——本构建的 `click_game_window` 不可用。空格子归谁、以及会不会吃掉滚动，都还没验。
- [ ] 验证**搬运动画**：三种搬运各画一次且方向正确（背包内交换是源格 → 目标格，**别反向**）、**起终点与槽里那张图标对齐**（2026-10-01 报过「偏出半个物品」，改动见第 8 节）、飞行动画**只在命令真的发出去之后**才出现、关闭／失活时半空中的那一个会被收起、`diagnostics()["animation"]["errors"]` 为 0。
      **2026-10-01 部分验证**：「背包 → 网络区」这一向逐次采样过，起终点**与槽位控件的左上角**精确对齐
      （`(195.0, 149.5)` → `(159.0, 50.5)`，误差 < 0.001），中途位置在两点之间，`errors` 全程为 0，
      到点自行收起。**另外两向（背包内交换、网络 → 背包）未采**，方向正确性因此整条不勾。
      **同日稍后更正**：上面那个「与槽位**控件**左上角对齐」**正是当时量错的对象**——槽位控件是
      18×18，而槽里那张图标是 16×16、在控件里居中，两者差 1 像素。按 User 当日的口径
      「**按 16×16 权威算**」，落点改成「把 16×16 的图标摆到目标矩形的正中」，于是量出来的
      数字会比上面那两个各多 1（`.5` 也会变）。**改后没有重新采样**，但**用户目测了实际动画并
      回报「很准确」**（2026-10-01）——这一条本来就靠眼睛定，见第 8 节。
      **仍不勾**：三种搬运没有逐向驱动过一遍，「各画一次且方向正确」没有被覆盖；
      飞行动画的方向正确性同样未逐向确认。
      另：`FLY_SECONDS` 现为 **0.125 秒**（2026-10-01 按 User 要求从 0.5 调快 4 倍），
      与本文档别处引用的「0.5s 左右」不是同一个数——那是初值。
      **同日再改**：存入网络的落点改为**点击处**（见第 6 节那一行）。**那一处仍未在真机上
      用手点过**——`TouchPosX/TouchPosY` 是否真的出现在绑定 `params` 里，只能由一次真实
      点击来确认；离线用例覆盖的是「拿到了这两个键之后怎么走」以及「拿不到时怎么退」。

| 日期／验证人 | 环境与方式 | 结果／证据 | 未验证项 |
| --- | --- | --- | --- |
| 2026-09-29／Codex | 工作区文档、JSON 与 Python 静态核对 | 已核实控件名、模板引用、排序初始 UV、collection 与模拟背包实现范围 | 未运行游戏；渲染、事件、真实网络与背包存取均不因此视为验收通过 |
| 2026-09-29／Claude Code | `endernet_tests/test_terminal_panel.py`（29 项）与 `test_terminal_cache.py` 离线测试 | 绑定名／旗标／集合与 JSON 对得上；搜索 strip、排序四种组合与图标一致性、重锚、越界索引、关闭摘绑定均有断言 | **未运行游戏**。`$` 变量能否替换、格子是否真的画出来、`SetUV` 的运行时效果、`#maximum_grid_items` 能否突破静态 30、遮罩能否拦住面板外的按钮，全部未验证 |
| 2026-09-30／Claude Code | `endernet_tests/test_backpack_model.py`（15 项）与 `test_terminal_panel.py::TestBackpackHandoff`（12 项）离线测试；完整门禁 2406 项、79 个文件通过 Python 2.7 语法门禁 | 两组槽序的四个端点、空槽点不动、同格再点取消、选中格换了东西就丢选中（别格变化不丢）、读失败整块保持上一帧、`pending_take` 的 `(槽号, 身份, 数量)` 与一组上限、有选中发 `inv_take` 且 `slot_version is None`、无选中仍发 `inv_put`、回执按 `request_id` 清选中、被拒时清选中并重读 | **未运行游戏**。引擎那边真的怎么给（`GetPlayerAllItems(INVENTORY, True)` 的长度与字段形状、索引 0–8 是不是快捷栏）、界面画出来没有、特殊物品（附魔／`userData`）的身份往返、连点与触控——逐条见 [11-交付说明](../tasks/11-交付说明.md) 第十节 |
| 2026-09-30／Claude Code | `test_terminal_panel.py::TestBackpackCellBindings`（5 项）与 `test_terminal_inventory.py::ScreenTests` 的 JSON 契约（2 项）；完整门禁 **2419** 项 | 八个集合绑定的名字／旗标／集合与 JSON 逐字对上、两段不串段（串了会把快捷栏当背包读）、空格可见且 `#item_id_aux == 0`、数量 > 1 才显示、越界下标不报成选中、轮询重读后引擎自取、销毁摘干净绑定 | **未运行游戏**。本轮把「按控件路径逐格取格子」整块换成了集合绑定，因此**「格子到底画不画得出来」从「已知会抛异常」变成了「未验证」**——这是本轮改动里最需要实测的一条。另：共享模板无耐久条，`#item_id_aux` 按资源名查（带损耗的工具会画成完好），均为已知缺项 |
| 2026-10-01／Claude Code | **游戏内实测**（User 在真机上跑，回报两处偏差）+ `test_terminal_panel.py` 新增 `TestQuickStore`（8 项）、`TestTransferAnimation`（6 项）、`TestFlyingItem`（10 项）、`TestSlotLayout`（8 项）与 JSON 契约 3 项，共 35 条；完整门禁 **2547** 项、82 个文件通过 Python 2.7 语法门禁 | **动画确实在真机上跑起来了**，这三条因此从「未验证」变成「已观察到」：`SetUiItem` 画得出来、`GetGridItem` 在点击那一刻取得到格子（格子回收换出**没有**在这条路径上咬人）、`GetGlobalPosition()` 与 `SetPosition` 是同一套坐标。同时暴露两处偏差并已修：①图标偏出**半个物品**——`GetSize()` 对这个 `custom` 控件回的是 0，脚本却拿它算居中，于是加了半个槽位；改成左上角对齐、不再读它。②飞行偏慢，按 User 要求调快 4 倍。离线侧仍照旧钉着：整槽那条 `inv_quick_move` + `target="network"` 且数量不截断、五种「不发命令」的情形、回执同一条收尾；动画的起终点、先快后慢、按秒推进、新飞行顶掉旧的；几何的两张网格与 `scroll_mouse`/`scroll_touch` 两端 | **「点列表空白处」那一下仍未在真机上点过**（没有回报，不得读成已验）：空格子归谁、背景按钮会不会吃掉滚动，都还不知道。**动画的两处修复也没再验**——修完没有第二轮回放。另外 `GetSize()` 回 0 这一条只在这一个控件上量到，别处不要据此推断 |
| 2026-09-30／Claude Code | **游戏内实测**（MCDK，`reload_game(reload_addons=True)` 后 `OpenDemo()`；`get_latest_error_logs` 零条）+ `test_terminal_panel.py::TestEngineBindingResolution`（2 项）；完整门禁 **2431** 项 | 上一行为运行时注册的绑定（`DynaBinding`）报 `'CommonTerminalPanel_Base' object has no attribute 'reader'`，每渲染帧一条、格子全空；**绑定全部搬进类体后不再出现**。实测另确认：36 格槽底全部生成（27+9，各 18×18）、两个段的槽序映射与引擎实读在两个端点上都对上、集合下标 0 基（选中 5 → 只有 `backpack_slot6` 的 `selected_frame` 变可见）、8 个有物品的格子中 7 个画出图标与数量 | **上半屏（列表）仍未跑过**——`OpenDemo` 没有权威身份，10 条列表绑定只跑到「没有会话时返回安全值」那条分支；「界面关掉」「逐格实点看背包少了哪格」「`inv_take` 回执」也都还没做。另有**一格画不出图标**（引擎槽 14 的 `skybluetech:item_transport_cable_steel`：`item_id_aux` 由引擎给出且非 0，但格子里不画）——已排除绑定与映射，倾向于是该方块没有 item 图标，**未定案**。另：当时 `item_list_window.py` / `select_item_window.py` 使用旧动态注册，尚未修复且无生产调用点。**2026-10-03 更新**：框架故障已修复，列表保留静态 mixin，选择器网格已使用修复后的动态 API；本行的真机验收仍为历史版本 |
| 2026-10-01／Claude Code | **游戏内实测**（User 回报「物品栏不显示数量」；`jsonui_debugger` 读运行时节点 + `execute_code` 读客户端 Python 逐次核对，改动经热重载后由 User 复看）+ `test_terminal_inventory.py` 新增 JSON 契约 1 项；完整门禁 **2548** 项 | **「数量不显示」是真 bug，不是没数据**：Python 侧 `backpack_item_count(2)` 返回 `"64"`，而运行时节点的 `text` 是空串。根因是共享模板的 `item_count` 标签只写了 `binding_name_override: "#text"`、**没有 `text`**——override 只把值写进 `#text` 属性，标签渲染的是自己的 `text`，因此没人读那个属性。补上 `"text": "#text"` 后数字立刻出现（User 确认「数字出来了，大小合适」）。同轮另修：标签 `layer` 抬到 `item_renderer` 之上——整块物品的图标按 16×16 铺满，标签在它下面会被盖住 | **上半屏（网络列表）的数量这轮仍然没看到**：当时网络是空的（`terminal_count()` 为 0），只验了下半屏。共享的是同一个模板，但按本仓「实测才算数」的口径不记作已验证。另：`fluid_list_window` / `item_list_window` 的标题是同一处毛病的近亲（`text` 写字面量、绑定没人读），当时那两个窗口使用旧 `DynaBinding`，回调无法解析，因此本次验收未改它们。**2026-10-03 更新**：框架故障已修复，列表窗口当前使用静态 mixin；历史标题问题需按当前 JSON 与脚本单独核对，不能继续归因于动态 API 不可用 |
| 2026-10-01／Claude Code | **游戏内实测**（User 回报「点列表空白处没有效果」；`execute_code` 读运行时控件树与客户端 Python，改后热重载）+ 新增 `test_terminal_panel.py::TestPressedNamesAreActuallyClickable`（扫全部界面 JSON）；完整门禁 **2604** 项 | **「点击没效果」是两个独立问题叠在一起**。①`terminal_scroll_background` 只覆盖了 `$pressed_button_name`、**没有 `button_mappings`**，`button.menu_select` 从来没接到那个名字上——Python 侧的 `OnQuickStoreClick` 早就注册好了，**直接调它就是好的**（实测：`in_flight` 0→1，`inv_quick_move` + `target="network"` 真的发出去了），但真手点永远不触发。补上映射后修好；全仓扫描确认只此一处。②**飞行动画本身是好的**，此前「没效果」只是因为它挂在同一个不触发的按钮后面：临时把 `FLY_SECONDS` 拉长到 15s 后逐次采样，`flying_item` 从源槽左上角 `(195.0, 149.5)` 起步、中途 `(167.7, 74.4)`、终点精确落在目标左上角 `(159.0, 50.5)`，全程 `visible=True`、`errors=0`，到点自行收起（测完已恢复 0.125s）。另确认控件树与文档一致：`background` 确实是 `Button`，`scroll_touch` 在 PC 上不存在（走 `scroll_mouse`） | **仍然没有用手真的点过**：本构建的 `click_game_window` 不可用，上面两条都是调回调/直接采样，因此「空格子那一下归谁」「背景按钮会不会吃掉滚轮与拖拽」**依然未验**。飞行动画只在「背包→网络区」这一向采样过，另外两种搬运（背包内交换、网络→背包）未采。移动端路径（`scroll_touch`）同样未验 |
