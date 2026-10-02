# bus_item_output_io.json：总线物品取货口

[返回目录](README.md) · [UI JSON](../../endernet_rp/ui/bus_item_output_io.json)

namespace：`bus_item_output_io_ui`。依据 2026-09-28 当前 JSON、公共组件、User 截图及功能说明编写。UI 功能交接已填写；设备专用脚本、服务端新增缓存模式及游戏验证待完成。

## 1. 制作范围与要求

总线物品取货口（`endernet:bus_item_output_io`）按设定物品从网络向朝向的外部容器自动供货。

- **5 个真实缓存槽**：`cached_slots/container_grid` 是本设备库存，承接网络 → 外部容器的物品，不是虚拟配置格或外部容器槽。
- **缓存默认启用**：启用时走网络 → 本设备 5 格缓存 → 朝向容器；关闭时关闭缓存槽，改为网络 → 朝向容器直接输入。
- **物品配置独立于库存**：沿用既有最多 9 种待取物品的上限，5 格缓存不意味着只能配置 5 种。设置／删除条目不消耗或生成物品。
- 齿轮按钮打开同 JSON 的 `settings_window`，其中“是否启用缓存槽”控制缓存模式。
- `item_set_panel` 展示已设定物品；空配置时其 `selected_item_renderer` 显示 `minecraft:barrier`，有配置时显示对应物品。`interact_btn` 打开 `item_list_window` 编辑允许自动输出的物品。
- 无设定时没有自动取货目标，不应视作允许输出任意物品；配置清空后已有缓存物品的处置仍待确认。

依据：[方块说明](../blocks.md)、[Task 06](../tasks/06.%20资源转移与自动输入输出服务.md)、[Task 11](../tasks/11.%20终端与客户端同步.md)、[Task 12](../tasks/12.%20持久化与恢复服务.md)。此次只明确物品取货口，不把 5 格与缓存开关推广到流体取货口。

## 2. 资源与架构

| 项目 | 当前结构 |
| --- | --- |
| 屏幕入口 | `main@common.inventory_screen_common`，`$screen_content` 为 `bus_item_output_io_ui.content` |
| 内容根 | `content`，layer=1；包含容器辅助控件和 `root_panel@common.root_panel` |
| 主面板 | `root_panel` 为 174×200，`bg@common.dialog_background_opaque` 使用不透明背景 |
| 设置窗口 | 独立模板 `bus_item_output_io_ui.settings_window`，160×100，未声明根 layer，也未挂入 `content.controls`；需脚本创建／显示 |
| 打开入口 | 玩家与该方块交互；Screen 注册、打开方法及交互校验待接入 |
| 登记与依赖 | `_ui_defs.json` 已登记；依赖 `common`、`common_toggles`、`netease_editor_template_namespace`、`endernet_ui_common`、`endernet_container_common`；列表复用 `endernet_windows` |
| 本地化 | 主标题 `tile.endernet:bus_item_output_io.name`；背包继承 `container.inventory`；设置与状态文字目前为中文直写 |
| 参考图 | User 本轮提供的主界面截图；“未设定物品”下的草方块为占位展示，验收以 barrier 规则为准 |

```text
main → bus_item_output_io_ui.content
└─ root_panel/bg
   ├─ close
   ├─ main
   │  ├─ title/title_text
   │  ├─ stat_bar
   │  ├─ outer_container_panel/{bg_frame, stat_text}
   │  ├─ cached_slots/{bg, container_grid}
   │  ├─ item_set_panel/{bg, selected_item_renderer, item_set_tip_text, interact_btn}
   │  ├─ conn_band_img/arrow_img
   │  ├─ baseband_img/{bg, frame}
   │  └─ settings_btn/icon
   └─ inventory
settings_window（独立模板）
├─ bg
├─ title/device_status_tip
├─ close
├─ cache_slots_enable_mode_tip_text
└─ cache_slots_enable_mode_switch
```

## 3. 控件功能与路径

`M` = `content/root_panel/bg/main`，`B` = `content/root_panel/bg`，`S` = 独立模板 `settings_window`。以下为 JSON 定义路径；继承屏幕的实际运行时前缀、动态窗口实例路径待接入核实。

| 控件路径 | 功能及当前配置 | 脚本职责 |
| --- | --- | --- |
| `B/close` | `common.close_button`，18×18 | 关闭主界面并清理所属子窗口 |
| `M/title/title_text` | 方块名称 label | 显示本地化名称 |
| `M/stat_bar` | `endernet_ui_common.device_stat_bar`，偏移 [84, 1]；继承状态图标、文字和交互按钮 | 用实际状态替换“未知状态”；状态按钮用途待接入 |
| `M/outer_container_panel/stat_text` | 100×20 面板内的 label，初值“外部容器： 未接入” | 按朝向容器连接状态刷新 |
| `M/cached_slots` | 100×22，顶部偏移 [0, 56]，含背景与缓存网格 | 随模式控制可用性；关闭后的隐藏／置灰样式待确认 |
| `M/cached_slots/container_grid` | `endernet_container_common.container_grid_items`，grid_dimensions=[5, 1] | 展示 5 格真实缓存及数量 |
| `M/item_set_panel` | 左上偏移 [8, 57]，20×20 | 虚拟配置摘要及编辑入口 |
| `M/item_set_panel/selected_item_renderer` | `custom` + `inventory_item_renderer`，静态 `#item_id_aux=131072` | 未设定渲染 barrier，设定后渲染对应物品；不能保留静态占位值 |
| `M/item_set_panel/item_set_tip_text` | 初值“未设定物品” | 与配置状态同步；已设定文案待确定，不能仍称未设定 |
| `M/item_set_panel/interact_btn` | 覆盖图标的透明按钮，layer=2 | 打开物品列表编辑窗口 |
| `M/settings_btn` | 20×20，右上偏移 [-8, 86]，齿轮图标 `settings_icon_light` | 打开本文件设置窗口 |
| `M/conn_band_img/arrow_img` | 向上箭头平铺，clip_ratio=0，父层灰色背景 | 表示网络向容器方向；无已声明的传输动画绑定 |
| `M/baseband_img` | 网络基带纹理及边框 | 仅示意，不存储资源 |
| `B/inventory` | 玩家背包及快捷栏，底部偏移 [0, -4] | 与设备缓存采用不同库存身份 |
| `S/title/device_status_tip` | “取货口设置” | 设置标题 |
| `S/close` | `endernet_ui_common.close_btn`，16×16 | 仅关闭设置窗口，返回主界面 |
| `S/cache_slots_enable_mode_tip_text` | “是否启用缓存槽” | 固定提示 |
| `S/cache_slots_enable_mode_switch` | `common_toggles.switch_toggle_collection`，20×20，偏移 [74, 19] | 读取并提交缓存模式，新设备默认 true |

## 4. 槽位、列表及组件复用

| 组件 | 模板／集合 | 数量与身份 | 规则 |
| --- | --- | --- | --- |
| 缓存网格 | `endernet_container_common.container_grid_items` / `netease_container` | 业务槽号 0–4，共 5 格；集合映射待核实 | 真库存；遵守物品堆叠上限与元数据，不固定换算为 320 件；手动存取权限待定 |
| 玩家背包 | `endernet_container_common.inventory` | 背包 9×3 与快捷栏 9×1 | 引擎背包身份独立于缓存槽号 |
| 配置摘要 | `item_set_panel/selected_item_renderer` | 单个图标 | 不存放实物，不等于只能配置一种；多项时显示哪项或是否轮播待确认 |
| 输出物品列表 | `endernet_windows.item_list_window` / `endernet_item_list` | 最多 9 个 identifier | 虚拟配置，不占缓存格；服务端须校验上限 |

公共网格继承 `maximum_grid_items=27`，本文件仅覆盖布局为 [5, 1]。接入时需使最大项数、集合和业务槽号严格对应 5 格，不能将 27 解释为设备容量。

[物品列表窗口](../technical_notes/item_list_window.md) 每行 8 格，按传入顺序显示，可滚动展示第 9 项。初始无选中项；添加同 identifier 不重复追加，已有重复项不会自动清理；删除按选中下标进行，删除后清空选择。超过上限的初始列表报错，不截断。

组件只接受 identifier，图标 aux=0，不代表已支持 aux／NBT 精确过滤。选择来源支持“全部”“背包”；当前接口传空网络列表，因此“网络”来源为空。barrier 只是未配置展示标志，不能自动加入输出名单或库存。

## 5. 操作流程与事件

1. 打开时取得服务端设备身份、代次、配置版本、输出名单、缓存模式、5 格库存、设备及朝向容器状态。新设备默认启用缓存；重开恢复保存值，不能每次重置为 true。
2. 空配置显示 barrier 与“未设定物品”；有配置显示对应物品，摘要不得由缓存槽内容反推。多项摘要策略见待确认表。
3. 点击 `item_set_panel/interact_btn` 打开 `item_list_window`，传入配置草稿及上限 9。添加／删除只修改本地列表；设备提交链路负责校验、保存和回执后刷新摘要。
4. 点击齿轮，创建或显示 `bus_item_output_io_ui.settings_window`，按已确认配置初始化开关。
5. 切换开关提交模式变更：启用后经 5 格缓存中转；关闭后不再用缓存承接新取货，直接向朝向容器输入。带货关闭的切换时机与旧货处置未定，不可先清空槽或假定已回网。
6. 关闭设置返回主界面；关闭列表保留其本地修改。组件没有取消回滚，也不提交服务端，关闭弹窗不等于保存成功。
7. 主界面销毁时关闭所属子窗口，清理订阅、回调与草稿，不能清空设备库存。

缓存关闭的具体隐藏／置灰表现待定，但业务必须禁用缓存路线及槽操作。缓存开启时外部容器满、未接入或拒收，已有缓存资源必须保持可追踪；是否继续预取直到满槽、补货顺序及单次数量仍待定。缺货、断电、主控无效或额度不足不得伪造成功；两种模式均遵守统一转移准入、实际数量回执、退避及不确定结果核对规则。

设置窗口需补层级、遮罩、防穿透和防重复打开。已有列表同一 Screen 拒绝重复打开，layer=30；选择窗口 layer=50，打开期间暂停列表选中／添加／删除。设置窗口的运行时层级需与它们协调。

两个业务按钮当前 `button_mappings=[]`，`$pressed_button_name` 为通用 `%netease_editor_template_namespace.click`，不是已注册的设备事件。触发边沿、触屏、系统返回、离线／距离失效、拆除／替换处理待接入；R12 持有物退出规则保持待定。

## 6. 数据与绑定

下表为数据语义，不声明新的既有 API 或字段名。

| 数据 → 控件 | 类型及刷新 | 当前状态 |
| --- | --- | --- |
| 设备状态 → `M/stat_bar` | 图标、文字；打开及状态变化 | 公共模板已有，设备源待接入 |
| 朝向容器 → `M/outer_container_panel/stat_text` | 连接信息；容器、朝向、加载状态变化 | 静态 text |
| 缓存库存 → `M/cached_slots/container_grid` | 5 格完整物品身份与数量；快照／实际转移后 | 槽映射与同步待接入 |
| 输出名单 → 列表与摘要 | `list[str]`，0–9 项；打开及配置确认后 | 公共列表已有，摘要仍为静态值 |
| 缓存模式 → 设置开关与缓存区 | bool，默认 true；打开及变更确认后 | 使用 `#netease_editor_template_namespace.toggle_name`、`#netease_editor_template_namespace.toggle_state` 占位，未绑定真实配置，未声明默认选中值 |

开关使用 `switch_on_default`、`switch_off_default` 及 hovered 纹理，纹理不等于实际状态。按钮与开关的 collection_details 仍为空集合名，不是业务绑定。容器集合与 `endernet_item_list` 独立，不混用下标。

配置命令携带可信设备身份、代次、请求 ID、期望配置版本；服务端校验资源和 9 种上限，处理重复请求与多人并发。等待回执避免重复提交；失败显示原因并恢复权威快照，旧会话／旧代次响应不得覆盖当前设备。具体命令名、提交时机、等待状态及错误文案待实现。

保存缓存模式、输出配置、5 格完整库存及转移归属，重载后不能被 UI 默认值覆盖。配置 identifier 与真实库存完整元数据分开。旧存档缺失模式字段的迁移、旧转移台账兼容需在 Task 12 明确。

## 7. 脚本如何使用本 UI

当前客户端及注册入口未检索到本设备专用 Screen 接入。已有公共入口位于 `endernet/client/ui/item_list_window.py`：

```python
# 宿主 Screen 位于 endernet/client/ui/ 时的接入示意。
from .item_list_window import WINDOW_ATTRIBUTE, show_item_list_window

# configured_item_ids 由设备权威快照获得；先创建可编辑草稿。
# 此调用只编辑本地列表，不提交服务端。
draft_item_ids = list(configured_item_ids)
show_item_list_window(self, draft_item_ids, 9, "自动输出物品")
```

签名为 `show_item_list_window(screen, item_ids, max_items, title="物品列表")`。创建成功返回 True，已有窗口返回 False。直接修改传入 list，无结果回调或自动保存；宿主需另接提交时机，不能把示意视为完整实现。

销毁时读取 `self._vars.get(WINDOW_ATTRIBUTE)`，存在则调用 `close()`；组件关闭自己创建的选择窗口并注销动态绑定。公共列表绑定使用 `#EnderNetItemList.*`，详见组件文档。

待接入：Screen 类及注册、方块交互打开、设置窗口挂载、toggle 事件、barrier／已设定图标、5 格槽映射、配置提交与同步、缓存／直输服务端路线、持久化迁移及关闭清理。本轮只编写文档，未修改 UI JSON 或运行时代码。

## 8. 缺项与验证记录

| 缺项／待确认规则 | 影响与处理边界 |
| --- | --- |
| ~~**打开入口未定，5 格身份随之未定**~~ **2026-09-29 已定：路径甲 + 5 格即设备自身库存** | 本屏是容器屏幕（根 `main@common.inventory_screen_common`），网格用引擎集合 `netease_container`。User 定「容器类设备走引擎自动打开、脚本以 `is_proxy=True` 挂代理」，取货口方块当日补上了 `netease:block_container`（`container_size=5`）与 `base_block`，**三条候选路线中选了第一条**。因此 5 格是**方块自身的容器**，外部模组也能存取。**领域层半边已于当日稍后补上权威**（`PortStore` 对 `OUTPUT` 建 `BUFFER_CONTAINER`，容量 `PortPolicy.output_cache_capacity`、开关 `PortConfig.cache_enabled`）；**仍未打通的**是方块那 5 格与这份记账之间的中转路由（「网络 → 缓存 → 朝向容器」归 06-06），因此两者目前只是各自存在、尚未对齐。依据见 [11-07 缺项清单](../tasks/11-07-UI缺项与等待交接.md) §一／§十、[网易容器屏幕的打开机制](../technical_notes/netease_container_screens.md) §零 |
| 缓存有货时关闭／修改或清空名单 | 排空后切换、拒绝切换、回网等方案未定；不自动删除、掉落或重新解释余货，需 06/11/12 联合确认 |
| 多项配置的单图标摘要 | 代表项、轮播及提示文案未定；不缩减 9 种配置能力 |
| 缓存关闭视觉与手动存取 | 隐藏／置灰及玩家权限待定；继承原生槽模板不等于授予权限 |
| 预取与调度 | 目标满／缺失时是否预取、补货顺序及槽位分配未定；保持资源守恒和预算约束 |
| 精确物品匹配 | 列表仅 identifier，aux／NBT 口径需对齐服务端身份契约 |
| 静态占位与容量继承 | renderer=131072（草方块，应为 barrier）、toggle 编辑器绑定串、maximum_grid_items=27 待接入时修正。开关另缺 `$toggle_binding_type`（原版默认 `"none"`），只换 `$toggle_name` 仍是空转，两者须一起改 |
| 脚本、同步与恢复 | 专用 Screen、新模式、5 格库存及迁移待实现；旧搬运验证不覆盖本轮缓存规则。~~服务端侧取货口**无缓冲**（`io.py:101` 的 `BUFFERED_ROLES` 不含 OUTPUT）、`PortConfig` 无缓存模式字段~~ **这两条 2026-09-29 已补**（`PortStore` 对 `OUTPUT` 建 `BUFFER_CONTAINER`、`PortConfig.cache_enabled`）；**仍未补的**是无设备视图通道与 `Migrator.register` 全仓零调用点，以及「网络 → 缓存 → 朝向容器」这条中转路由（06-06）。逐条见 [11-07 缺项清单](../tasks/11-07-UI缺项与等待交接.md) 第四节 |
| 引擎交互 | 层级、触控、防穿透、距离／拆除失效、R12 及异常状态待验证 |
| 公共组件未交接 | 本文件依赖的 `endernet_container_common.md`、`endernet_ui_common.md`、`endernet_windows.md` 三份文档仍为空模板（各 27 处「待填写」）。列表窗口可被 §7 引用，但组件文档未交接时接入示意无法核对 |
| ~~同类问题（非本设备）~~ **2026-09-29 已一并解决** | 物品存货口（`bus_item_input_io.json:331`）与内存槽（`memory_slot.json:102-106`）原先同样画了 `netease_container` 网格却无容器组件；当日连同本设备一起补上了组件。**附带的规格更正**：存货口格数由 9 改为 **5**（以 JSON 的 `[5,1]` 为准），[bus_item_input_io.md](bus_item_input_io.md) 与 [blocks.md](../blocks.md) 已同步 |

- [x] 核对 JSON、公共列表入口、5 格容量和缓存开关规则，完成文档交接。
- [x] 同步方块说明与 06/11/12 契约，保留未定项。
- [ ] 完成专用脚本、业务绑定、服务端缓存模式及恢复。
- [ ] 游戏验证：默认启用、重开保留关闭值、两种路线真实转移守恒。
- [ ] 游戏验证：空配置 barrier、设定与清空、多项摘要、第 9／10 项上限。
- [ ] 游戏验证：满容器、缺货、断电、部分写入、方向改变、带货切换、多人编辑、卸载恢复与拆除。
- [ ] 游戏验证：弹窗关闭范围、防穿透、触控、长文本及持有物退出。

2026-09-28：完成资源和代码入口的静态核对及文档检查。截图仅作为布局参考，不作为脚本功能验收证据；本轮未进行游戏验证。

本轮验证：文档相对链接和所列 JSON 控件检查通过；关联文档 diff 空白检查通过；python -B endernet_tests/run_tests.py 通过 2240 项测试，76 个源码文件通过 Python 2.7 兼容门禁。以上为现有工作区离线结果，不代表新增缓存行为已实现或通过游戏验收。

2026-09-29（11-07 开工评估，**无代码交付**）：复核本文件后确认**界面逻辑不具备开工条件**，原因是上表的「打开入口未定」「公共组件未交接」与服务端缺口，而非界面绘制未完成。缺项、依据与待 User 决定的问题记于 [11-07 UI 缺项与等待交接](../tasks/11-07-UI缺项与等待交接.md)；A1 三条打开路线的机制核实与真机待验问题记于 [网易容器屏幕的打开机制](../technical_notes/netease_container_screens.md)。本轮未写 Screen 类、未注册、未改任何 `plans/ui/*.md` 的第 2～7 节。

2026-09-29（同日第二轮，**有代码交付，仅方块侧**）：User 定案「不是终端类方块且能存储物品且有 UI 有物品槽的都要有网易容器」，取货口因此拿到 `netease:block_container`（`container_size=5`、`screen_name=bus_item_output_io_ui.main`、`base_block=netease_container`），方块 JSON 已重新生成。**仍未做**：专用 Screen 与 `is_proxy=True` 注册、领域层缓冲、缓存模式字段、任何游戏内验证。交付记录见 [11-07 UI 缺项与等待交接](../tasks/11-07-UI缺项与等待交接.md) §十。

本轮验证（同日第二轮）：`python -B scripts/gen_blocks.py --check` 无差异；`python -B endernet_tests/run_tests.py` 通过 **2244 项**，76 个源码文件通过 Python 2.7 门禁。**以上全部是离线结果，「右键能打开、5 格能被引擎填充」仍未验证。**

2026-10-02（11-07 **只读接入**）：本界面的**代理 Screen 已挂上**——`client/ui/device_panels.py` 的 `@RegistToolDeltaScreen("bus_item_output_io_ui.main", is_proxy=True)`，屏由引擎开、脚本挂代理（**不 `PushUI`**）。因此 `stat_bar` 的在线/离线与详情弹窗接上了，`content` 里也补了一条 `device_status_popup@endernet_windows.device_status_window_basic` 实例（层号 40、居中）。**代理拿不到构造参数**，设备身份由服务端在右键那一刻推来（`common/device_identity.py` 与 `CONTAINER_SCREENS`）。**仍未做**：缓存开关的提交（`TerminalCommand` 今天没有 `config` 字段，`port_configure` 从客户端够不着）、`settings_window` 的挂载、两个业务按钮的事件与映射、静态占位与 `maximum_grid_items` 继承——逐条见 [11-07 缺项清单](../tasks/11-07-UI缺项与等待交接.md) §三 B 组。~~**游戏内一次都没跑过**：代理会不会被引擎回调、容器方块的右键服务端收不收得到，都只能上机定论。~~ **2026-10-02 当日稍后：这两条已在真机上验掉了，而且都成立**——代理的四个回调都会被引擎调用，容器方块的右键服务端确实收得到（实测见 [11 交付说明](../tasks/11-交付说明.md) 第二十三节）。**但实测打的是 `bus_template_io` 那一台**：本界面共用同一份注册代码与同一份绑定，**没有逐台右键过**，所以本行其余各项（缓存开关、`settings_window`、两个业务按钮、barrier、`maximum_grid_items`）仍然全部未做。
