# 网易容器屏幕的打开机制

记录日期：2026-09-29。用途：为 [11-07 缺项清单](../tasks/11-07-UI缺项与等待交接.md)
的 A1 记录已定案的路线（§零）与三条候选的代价（§二～§四），**不回答任何界面语义问题**。

**状态**：§零 的分类与落地形态均已由 User 定案，容器也已按该分类**落到 9 台方块上**
（2026-09-29 第二轮，见 §二 与 [11-07 清单](../tasks/11-07-UI缺项与等待交接.md) §十）；
§五 有 2026-09-29 的真机核实记录。
「引擎按方块组件自动打开」这一条**仍未在真机上直接验证**（需要真人右键，见 §六）。

## 零、定案：哪些方块走网易容器（User，2026-09-29）

原话：「**只有终端类方块是使用自定义槽位逻辑；其他方块由于槽位物品没有超限，
直接走网易容器**」，理由是「终端展示网络聚合库存，槽位物品数量可以超限」。

按定义表切分（`common/definitions.py` 的 `CATEGORY_*`）：

| 走哪条 | 方块 | 依据 |
| --- | --- | --- |
| **自定义槽位控件 + 自定义渲染** | `CATEGORY_TERMINAL` 的 4 台：通用／合成／样板／工厂终端面板（`definitions.py:772-804`） | 展示网络聚合库存，单格数量可超过原版堆叠上限 |
| **网易容器（本文路径甲）** | 其余全部，含 `CATEGORY_BUS` 的物品存货口／取货口（`definitions.py:721`／`:737`）、`CATEGORY_MEMORY` 的内存槽（`:824`） | 槽位物品不超限 |

### 落地形态（User 同日补充，逐字）

「**走网易容器的设备的界面走代理(proxy=True)；那几个终端则检测点击方块然后 PushUI()**」

| | 容器类设备（非终端） | 终端类（那 4 台） |
| --- | --- | --- |
| 界面从哪来 | JSON UI **原生屏**，屏幕名即方块 `netease:block_container.screen_name` | 脚本自有屏（`RegisterUI` 建），由脚本推出 |
| 谁触发打开 | **引擎**按方块组件自动打开，**不拦右键** | 脚本**检测方块点击**（`ClientBlockUseEvent`，需先 `AddExtraBlocks` 进白名单）后 `PushUI()` |
| 脚本怎么挂 | `@RegistToolDeltaScreen(bound_ui_name, is_proxy=True)` → `NSManagerIns.RegisterScreenProxy`（§四 乙-A） | `@RegistToolDeltaScreen(bound_ui_name)`，即 `is_proxy=False` → `RegisterUI`（§四 乙-A） |
| 用哪个集合 | `netease_container`——**继承来的绑定保持不动** | 不用引擎集合；槽位是自定义控件 + 自定义渲染 |
| 为什么这么分 | 槽位物品**不超限**，引擎容器直接填得下 | 单格数量可超原版堆叠上限，引擎容器承载不了 |

**对 11-07 的推论**：`endernet:bus_item_output_io` 是 `CATEGORY_BUS`，不在终端类之内，
因此 **A1 定为路径甲**——给方块加 `netease:block_container` + `base_block: "netease_container"`，
界面里继承来的 `netease_container` 集合绑定**本来就是对的**，也**不需要拦右键**。
但**仍然需要一个 Screen 类与一次注册**：以 `is_proxy=True` 把设备的读取／按钮／开关逻辑
**挂到那张原生屏上**（`RegisterScreenProxy`），而不是另建一张屏。代价见 §三末
（方块实体从此有 `Items`，06／12 要同步）。

> 「终端类走自定义槽位、其余走网易容器」这一句的分类是 User 逐字给的；
> **它落到具体方块上是本文按定义表推的**——若取货口其实该归入终端类，本文与清单要一起改。
> 「容器类走代理、终端类 `PushUI`」这一句是 User 逐字的。

**与既有实测对得上**：§五 1 量出「画了 `netease_container` 集合」的 7 个界面是
`assemble_chamber_ui` / `bus_item_input_io_ui` / `bus_item_io_ui` / `bus_item_output_io_ui` /
`bus_template_io_ui` / `memory_slot_ui` / `storage_driver_ui`——**4 台终端一个都不在里面**，
7 个全部落在「容器类」那一侧（当时 7 个里只有 4 台已声明容器组件，3 台是待补的差异；
**那 3 台已于 2026-09-29 补上**，见 §五 1 与 §二）。
User 的分类规则独立预测出这个切分，实测恰好相符，两条线索互证。

## 一、问题

本模组有 7 个界面画了容器网格，但当时**只有 4 个方块声明了容器组件**。缺的那三台
（物品存货口、物品取货口、内存槽）的屏幕**当时怎么被打开，没有任何已定案的说法**：
`scripts/gen_blocks.py` 的声明注释（原文在 `:119-121`）明确把它们排除在容器方块之外，
理由是「blocks.md 说它们『自动从/向朝向的容器』搬运，动的是**邻居**，不是自身缓冲」。
而 2026-09-28 的取货口增量又给了它 5 个「内部真实缓存槽」——两者不能同时成立。
**这条张力已于 2026-09-29 由 User 裁决「格就是设备自身库存」而消解**
（那条排除注释同日被改写为「为什么是这几台」，见 `scripts/gen_blocks.py:103-139`）。

**§零 定案后，「怎么打开」这一半已解决**（取货口走路径甲 + 代理）；另一半
——「那 5 格是不是方块自身的库存」——也**已于 2026-09-29 由 User 答「是」并落地**
（加容器组件就是回答「是」）：本文件 §二 记的 4 台扩到 9 台，
见 [11-07 清单](../tasks/11-07-UI缺项与等待交接.md) §七 Q2 与 §十。
**注意这解决的是方块侧**：领域层 `PortStore` 对 `OUTPUT` 仍是 `buffer=None`，
两边尚未对齐（11-07 §四 C1）。

## 二、三种集合名，对应三条不同通路

**它们不是同一个东西，混用不会报错，只会得到一个空网格。**

| 集合名 | 谁填充它 | 在什么通路上用 | 本机可查的样例 |
| --- | --- | --- | --- |
| `netease_container` | 引擎，按方块实体的 `Items` | 方块容器（`netease:block_container` + `base_block: netease_container`） | **本仓自己**：`endernet_container_common.json:128` 硬编码 |
| `container_items` | 引擎，按方块容器 | 原版自定义箱子 | `vanilla_netease/ui/netease_custom_chest_screen.json:13`，且用 `grid_dimension_binding: "#netease_custom_chest_grid_dimension"`（`:12`）逐帧取维度 |
| `netease_ui_container` | **引擎为 `OpenNeteaseContainer` 打开的界面绑定** | 服务端脚本打开、不依赖方块 | **本仓自带范本**：`endernet_rp/ui/NeteaseContainerUI.json:197`（集合名）与 `:3`（`$item_collection_name`），已登记于 `_ui_defs.json:22` |

前两者的差别值得注意：本仓把 `collection_name` 写死在公共模板里，原版则用
`grid_dimension_binding` 让网格维度随界面变化。这只影响「同一模板给不同格数的设备复用」这一件事。

**命名空间与集合名是两回事，`NeteaseContainerUI.json` 是最好的例子**：它的
`namespace` 是 `netease_container`（`:5`），而它内部同时用了两个集合——
`netease_custom_container_grid` 用 `netease_container`（`:14`），
`netease_drop_container_grid` 用 `netease_ui_container`（`:197`），
两个网格被叠在同一个面板里
（`netease_custom_container_panel` 的 `:101` 与 `:109`）。
它的屏幕名是 `netease_custom_container_screen@common.inventory_screen_common`（`:173`），
**与引擎 `OpenNeteaseContainer` 注释里的示例 screenName
`"netease_container.netease_custom_container_screen"` 逐字相符**。
即：路径甲与路径丙的样例在同一份文件里，且这份文件就在本仓资源包内。

## 三、路径甲：引擎按方块组件自动打开

方块的 `minecraft:block.components` 里有 `netease:block_container`
（`{"custom_description": ..., "screen_name": "namespace.screen", "container_size": N}`），
且 `description.base_block` 为 `"netease_container"`，玩家右键就会打开该屏幕，
格子由引擎按方块实体数据填充。

- 本仓已走这条的**9 台**（声明处 `scripts/gen_blocks.py:140`，产物见
  `endernet_bp/blocks/*.json`）：`storage_driver`、`bus_item_io`、`bus_template_io`、
  `assemble_chamber`（原有 4 台），加 2026-09-29 补上的 `bus_item_input_io`、
  `bus_item_output_io`、`memory_slot`、`bus_fluid_input_io`、`bus_fluid_output_io`。
  后两台的格数是 **1 格占位**且**界面上刻意不画**那个集合
  （`unresolved="R07"`，真实流体适配属 13-03）。
- 离线已有断言把两者钉在一起：`endernet_tests/test_assets.py:376` 起
  （集合恰好等于生成器声明、`container_size` 等于领域层槽数、必须有 `base_block`、
  非容器方块两样都不得有）与 `:492`／`:517`／`:540` 三条界面侧断言
  （该画的必须画、该隐藏的必须隐藏、画了却没有容器方块的反向差异必须为空）。
- **同文件 `:377-380` 的 docstring 明写这条边界**：「只验证『资源声明正确』，**不验证它在
  游戏里真的能打开**——那件事只能在真机上定论」。所以「这 9 台能打开」至今仍是**未验证**项，
  2026-09-29 第二轮**一次都没上机**。

## 四、路径乙与丙：脚本侧的两条路

### 乙-A：ToolDeltaScreen 注册（`tooldelta/ui/reg.py`）

`RegistToolDeltaScreen(bound_ui_name, key=None, is_proxy=False)`（`:24`）有两个分支，
在 `UiInitFinishedEvent`（优先级 10，`:55-56`）里统一注册：

| `is_proxy` | 调用 | 用途 |
| --- | --- | --- |
| `False`（默认） | `RegisterUI(GetModName(), ui_key, path, bound_ui_name)`（`:71`） | 注册一个**自有**屏幕；本仓通用终端走这条（`client/ui/terminal_panel.py:127`） |
| `True` | `NSManagerIns.RegisterScreenProxy(bound_ui_name, path)`（`:67`） | 挂到**既有原生屏幕**上（`general_screen.py` 的 `_register_as_proxy`） |

两条分支各对应 §零 的一类方块：

- **`is_proxy=False`＝终端类。** 屏幕是本模组自有的，由 `RegisterUI` 建出来，
  **打开动作是 `ToolDeltaScreen.PushUI`**（`general_screen.py:89`，类方法），
  实例状态在 `_activated`（`:44`）。
- **`is_proxy=True`＝容器类。** 屏幕由资源包提供（`bus_item_output_io.json` 里那张原生屏
  本来就在 `_ui_defs.json` 里），脚本**不建屏、也不推屏**——只把逻辑挂上去，
  打开由引擎按方块组件完成（路径甲）。

本仓现状：全仓唯一一处 `@RegistToolDeltaScreen` 是通用终端（`is_proxy=False`）；
设备界面**一处都没有**，包括容器类该有的那些 proxy 注册。

### 乙-B：拦截方块的右键

`ClientBlockUseEvent`（`tooldelta/events/client/block.py:5-6`）带 `blockName` 与坐标，
且 `cancel()`（同文件末尾）能「拦截与方块交互的逻辑」——即可以阻止引擎走路径甲，
改由脚本 `PushUI`。要收到它必须先 `AddExtraBlocks(blocks)`（`block.py`）把方块加入白名单。

它与 11 文档设想的形态不同（11 文档第 90 行讲的是 `Binder` 静态绑定，
第 23 行禁止「先写脚本后适配」），因此若选这条，**11 文档与 11-07 实现形态都要改**。

### 丙：服务端 `OpenNeteaseContainer`

`mod/server/component/playerCompServer.py:1147-1184`（引擎源码在本机
`D:/gitclone/vanillaMCP_3.7.21.288355_server/`）：

```
OpenNeteaseContainer(self, screenName, customDescription, isCloseReturnItem=False)
```

引擎注释里与本项目相关的几条：

- 「打开自定义容器界面，**不依赖于方块**。该界面的物品数据**需由开发者自行维护**」；
- 「`collection_name` 需指定为 `netease_ui_container`」；
- 物品数据用 `GetPlayerUIItem` / `SetPlayerUIItem` 自行存取，关闭时**不自动返还**背包；
- 可用 `PushScreenEvent` 做格子初始化；会触发 `PlayerTry*CustomContainerItem*Event`；
- 「如出现物品无法放入容器界面、或者无法移动物品，请检查 ui json 的是否使用了
  `netease_ui_container`」。

**代价**：这条路绕开方块实体，与本模组「玩家右键方块、设备有位置与朝向、服务端按
`node_key` 认设备」的模型不一致；`collection_name` 必须是 `netease_ui_container`，
与本仓设备界面继承的 `netease_container` 不同，等于本设备的网格要单独写一套，
物品数据也要自行用 `GetPlayerUIItem` / `SetPlayerUIItem` 维护。

**样例是有的**：本仓 `endernet_rp/ui/NeteaseContainerUI.json` 就是这条路线的范本
（§二），且它的屏幕名与引擎注释里的示例逐字相符。真正缺的不是样例，而是
**它没在真机上跑过**，以及它与本模组设备模型如何对齐（方块没了，`node_key` 从哪来）。

## 五、核实记录

**静态已核实（2026-09-29，本机）**：

1. **本仓有 7 个界面画了 `netease_container` 集合**（按「实例化了
   `container_grid_items` 或逐槽 `container_grid_item`」判定，不是按界面里出现
   `collection_name` 字面量判定——见下一条）：`assemble_chamber_ui` /
   `bus_item_input_io_ui` / `bus_item_io_ui` / `bus_item_output_io_ui` /
   `bus_template_io_ui` / `memory_slot_ui` / `storage_driver_ui`。
   当时声明了容器组件的只有 4 台（`gen_blocks.py`），两边的差是：
   **画了却没有容器方块＝`bus_item_input_io_ui` / `bus_item_output_io_ui` /
   `memory_slot_ui` 三个；有容器方块却没画＝空集**
   （`assemble_chamber` 靠逐槽 `container_grid_item` 渲染，是画了的）。
   **2026-09-29 第二轮后这个差清零**：那三台补上了组件，两个流体口则是
   「有容器但界面刻意不画」的新形态（`endernet_tests/test_assets.py:517`）。
2. **不能靠字面量 `collection_name` 找这个集合。** 集合名只写死在公共模板
   `endernet_container_common.json:128` 一处，设备界面一律**继承**它，自己那一行
   不出现该字符串；逐槽写法（`slot@endernet_container_common.container_grid_item`）
   连模板都不写集合名，靠 `$item_collection_name` 的默认值（同文件 `:118`）绑定。
   全仓只有 2 个界面自己写了 `collection_name`：`bus_item_io.json:146`（编辑器残留的
   `test_grid`）与 `assemble_chamber.json:108`（`source_items`，3×3），
   **两个都不是** `netease_container`。只看字面量会得出完全错误的结论。
3. `netease_ui_container` 在 `D:/MCStudioDownload/game/MinecraftPE_Netease/3.9.0.401155/data/`
   下的**全部 JSON 中零出现**——即**游戏自带**资源包里没有这条通路的样例。
   但**本仓自己有一份**：`endernet_rp/ui/NeteaseContainerUI.json`（随 `87c4c2e`
   以「终端 UI 基座」名义入库，`_ui_defs.json:22` 已登记，`.mcs/editorSave.json`
   引用了它，Python 侧无人引用）。它是路径甲与路径丙共用的范本，见 §二。
4. 原版范本 `vanilla_netease/ui/netease_custom_chest_screen.json`：命名空间 `chest`，
   集合 `container_items`（`:13`），`netease_custom_chest_screen@common.inventory_screen_common`
   （`:112`）——**路径甲的可对照样例**。
5. `common.inventory_screen_common` 的定义在 `vanilla_netease/ui/ui_common.json:6697`
   （`inventory_screen_common@base_screen`）。
6. 本仓 `bus_item_io.json` 同一屏内既有编辑器残留 `test_grid`（`:146`）又有真实网格
   （`:155`），是路径甲那条屏的现状，可作为真机对照样本。
7. `tooldelta/ui/reg.py` 的 `is_proxy` 两分支与 `general_screen.py` 的 `PushUI` 已读源码确认。
8. 上面第 1、2 条的判定已固化为离线断言：
   `endernet_tests/test_assets.py::TestBlockContainers` 的
   `test_container_screens_actually_render_the_engine_collection`（真不变量，
   今天为绿）与 `test_screens_rendering_the_engine_collection_without_a_container_block`
   （把已知的那 3 台差异**具名钉住**，任何人改动都会在此被拦下）。

**仍未核实（需真机，见 §六）**：路径甲在真机上是否真的打开、格子是否被引擎填充、
运行时控件路径。

### 真机核实记录（2026-09-29，MCDK 启动的游戏内）

样本世界（2026-09-29 复核：**23 台**设备已注册）：
`endernet:bus_item_io` 在 (24, 68, -8) dim 0、`endernet:bus_item_output_io` 在
(24, 68, -7)、另有一台 `bus_item_io` 在 (-30, 70, -67)；
**注册表里没有任何 `endernet:bus_item_input_io`**（23 台，`unloaded = 0`；
即**已发现区块内**没有——未加载区块不在这个口径内）。
玩家当时在 (-39.95, 71.62, -67.17)，
离 (24, 68, -8) 约 87 格，**右键要真人先走过去**。
（样本资格见 §六：`bus_item_io` 已因「没做完」作废，端口才是样本。）

1. **路径乙-A 的注册环节成立**：`/mod-ui --include-registered` 里确有
   `EnderNet:common_terminal_panel_ui.main → common_terminal_panel_ui.main`，
   即 `RegistToolDeltaScreen` 的 `RegisterUI` 真机可用。
2. **路径乙-A 的创屏环节：屏出来了，Python 侧没接上。** `CommonTerminalPanel.OpenDemo()`
   调 `CreateUI` → `clientApi.CreateUI` 返回 `None` → 上游判 `isinstance` 失败抛
   `CreateUI failed: return None is not <class ...CommonTerminalPanel>`；
   但 `capture_game_window` 截图里**屏幕确实渲染出来了**（标题「通用终端面板」、
   搜索框、上下两片格），只是 `OpenDemo` 塞的演示物品没有出现。
   首调另有 `GetBaseUIControl('/terminal/inventory')` → `UBaseCtrl` 报
   `Can't initialize UBaseCtrl: comp is None`：**脚本路径与 JSON 对不上**——
   `common_terminal_panel.json` 里该控件实际是 `content/inventory`
   （`main/terminal` 是另一片空 panel，其下没有 `inventory`）。
3. **路径丙实测未成功**：`comp.OpenNeteaseContainer('netease_container.netease_custom_container_screen', ...)`
   在服务端返回 **`False`**（未打开），且日志里没有对应的引擎报错。范本屏幕就是本仓
   `NeteaseContainerUI.json`（§二）。**一次失败不足以判这条路不可用**（可能与调用时机、
   玩家状态有关），但它今天没有任何成功证据。
4. **ModSDK `CreateUI` 的屏不在原生 JSON UI 树里**：屏幕开着的时候，`/screens` 与
   `/overview` 都只看得到 `hud.hud_screen`。因此——**路径甲的容器屏可以用
   `jsonui_debugger` 查运行时控件路径，路径乙的脚本屏不能**，后者只能靠 ModSDK 的
   `GetBaseUIControl` 自查。这一条直接决定 §六 Q3 用什么工具。
5. **附带发现（不属于本文范围，但拦住了整场核实）**：模组原本在 `OnServerInited`
   就崩（`EventBus.AddEventListener` 的 `static=True` 给**绑定方法**挂 `_td_static`，
   py2 抛 `AttributeError`），见 [11-07 清单](../tasks/11-07-UI缺项与等待交接.md) §九。
   修掉之后又露出第二个：`GetLoadItems(True)` 会报上 `minecraft:air`，
   `filter_item_ids` 对它抛 `ValueError`，导致目录服务永远发不出包。
   **两处均已修复，且修复后已上机验证**：客户端 `_SERVICE.item_ids` 拿到 **2265 条**、
   服务端 `tick_errors.count = 0`、错误日志为空（11-07 清单 §九末有探针表）。
   这属于 11 的物品／流体选择链路，不属本文题目。
6. **方块实体侧的容器是真的（只读补测，不需要右键）**。用与
   `server/adapters/containers_engine.py:429-440` **完全相同的原语**
   （`f.CreateItem(GetLevelId()).GetContainerSize(pos, dim)` 与
   `GetContainerItem(pos, slot, dim, True)`）读同一维度里的相邻两台：

   | 方块 | 坐标（dim 0） | `GetContainerSize` | 逐槽读回 |
   | --- | --- | --- | --- |
   | `endernet:bus_item_io` | (24, 68, -8) | **9** | 9 槽全为 `None`（空容器） |
   | `endernet:bus_item_output_io` | (24, 68, -7) | **-1** | 未读（引擎答「没有容器」） |

   两条**必须一起看**才是结论：声明了容器组件的方块在真机上有**真实的 9 格容器**
   （与 `gen_blocks.py` 声明的 `container_size` 相符，且能逐槽读），
   而没声明的那台引擎直接答 `-1`。这既是「路径甲背后确实有一个真容器」的证据，
   也是 §一 「5 格凭空存在」在真机上的直接确认。
   **两条边界**：① 它证明的是**方块实体侧**，「右键之后界面里看得见这 9 格」
   仍属 §六 Q1／Q2，仍必须真人右键；② 提供 `= 9` 那台的样本资格已在 §六 被
   User 作废（`bus_item_io` 没做完），所以它**只用来证明机制成立，
   不能当任一端口的对照**。取货口那半（`-1`）不受影响——它量的本就是端口自己。
   **补记（同日稍后）**：取货口那 `-1` 是**加容器组件之前**的读数。
   组件已于 2026-09-29 第二轮补上（`container_size=5`），因此这个数今天**已经过期**，
   重测应得到 `5`——但**没有重测**，别把它当成已验证的新结论。

## 六、待运行时核实的问题

**样本改为两台端口：`endernet:bus_item_input_io`（存货口）与 `endernet:bus_item_output_io`（取货口）。**

原先选的是 `endernet:bus_item_io`（当时理由是「9 格是定案的真实库存、方块已有容器组件，
三者中最干净的一台」）。**2026-09-29 User 判定该设备「没做完」，样本资格作废**——
在定义表里它与两台端口是**三条彼此并列的定义**（`definitions.py:703` 对 `:721`／`:737`，
各有 id、能耗与 notes），不是端口的替身；拿它做对照会把「未完成设备的表现」
误当成端口的结论。§五 6 那条 `container_size = 9` 仍作为「引擎容器机制成立」的
证据保留，但**不得当端口的对照**。

**两台端口当时都没有可测对象**，理由各自不同：

| 设备 | 世界里有实例吗 | 有容器组件吗 | 结论 |
| --- | --- | --- | --- |
| `bus_item_output_io` | 有，(24, 68, -7) dim 0 | ~~没有~~ → **2026-09-29 已补**（`container_size=5`，未重测） | 组件已就位，Q1–Q3 只差真人右键 |
| `bus_item_input_io` | **注册表里没有**（23 台设备，`unloaded = 0`） | ~~没有~~ → **2026-09-29 已补** | 组件已就位，但**方块仍不在世界里**，要测先得真人放一台 |

因此 Q1–Q3 对端口而言**整体阻塞在前置决定上**，而不是「等真人右键」——
真人右键只是最后一步。**那个前置决定（= 给端口加容器组件，
即 [11-07 清单](../tasks/11-07-UI缺项与等待交接.md) §七 Q2）已于 2026-09-29 解开**
（同一份清单 §十）。今天剩下的只有两件事：真人右键，以及给存货口放一台方块。
**两者本轮都没做。**

| # | 问题 | 判据 |
| --- | --- | --- |
| Q1 | 右键端口方块，屏幕是否打开、是否是 `bus_item_output_io_ui.main` | `get_latest_logs` / `jsonui_debugger` 能看到屏幕与命名空间 |
| Q2 | 端口自己的格（取货口 5 格／存货口 9 格）是否被引擎填充（不是空网格） | 往格里放一样东西后，用 `execute_code` 读方块实体，读法与 `server/adapters/containers_engine.py:429-440` 一致 |
| Q3 | 槽位子控件的**运行时**路径（JSON 定义路径 ≠ 运行时路径） | `jsonui_debugger` 的节点查询。**只对路径甲有效**——脚本屏不进原生树（§五 4） |
| Q4 | 对一个本仓屏幕调 `OpenNeteaseContainer` 会怎样 | 预期因集合名不符而失败或网格为空。**已试一次：返回 `False`**，无引擎报错；不足以定论（§五 3） |
| Q5 | `PushUI` 打开一个已注册的脚本屏幕 | 代表 `is_proxy=False` 那条路，即终端类。**已试：屏渲染出来了，但 `CreateUI` 返回 `None`**，脚本路径与 JSON 不一致（§五 2） |
| Q6 | `is_proxy=True` 注册的代理能否挂在引擎打开的容器屏上 | 代表容器类由 §零 定下的形态。`RegisterScreenProxy` 在 `UiInitFinishedEvent` 执行（`reg.py:55-67`），逻辑是否有 `OnCreate` 回调要看 `_register_as_proxy` 生成的 `ScreenProxy` 子类。**前置：得先有一个代理 Screen 类**——而它属 11-07 的界面逻辑，卡在 §四 C1／C2（5 格无权威来源、开关无提交目标）。因此本轮**没有可测对象**，只登记问题 |

**证据必须分开记**：Q1–Q3 是「引擎自动打开」，Q4 是「服务端打开」，Q5／Q6 是「脚本侧」
（Q5 自有屏、Q6 代理）。**不得用脚本伪造「玩家右键」**；起不来就如实记
「引擎自动打开这条未验证」，不要用 Q5／Q6 的结果去替它。

## 七、三条界线（免得下一轮踩）

1. 本文只回答**机制**问题，不回答界面语义。不要在这里记录「这台设备的界面该怎么用」——
   那是 `plans/ui/*.md` 的事。
2. 观察结果**不写进任何 `plans/ui/*.md` 的第 2～6 节**。脚本侧可写的只有第 8 节，
   且要先与 User 的交接内容比对去重。
3. 三种打开路径的证据不得互相冒充。路径甲能用，不代表路径丙能用，反之亦然。
