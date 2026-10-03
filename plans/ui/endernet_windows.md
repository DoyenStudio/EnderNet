# endernet_windows.json：公共弹窗

> **2026-10-03 新增需求（待实现）：** order_preview_window 的用途与行为已由 User 确认：数量确认后显示；服务端分帧计算期间 loading_panel 可见，完整完成后隐藏；order_preview_src_item/bg 缺料为 RGB(255,200,200)，任一步缺料禁用 confirm_btn。现有模板尚待接线，下单与取消不得复用工厂详情关闭／真实订单取消事件。本文笼统“其他弹窗待交接”不再表示本预览的上述行为未确认。 完整规则见 [网络样板标记与下单预览契约](../technical_notes/terminal_pattern_order_preview.md)。
**2026-10-03 已接线**：模板四行绑定、窗口三个按钮事件、loading 与状态说明绑定、
网格上限 11→64；三块终端屏各实例化了一条（**只实例化、没新建模板**）。
逐条与实测见 [11 交付说明 §31／§32](../tasks/11-交付说明.md)。

[返回目录](README.md) · [UI JSON](../../endernet_rp/ui/endernet_windows.json)

对应文件：`endernet_rp/ui/endernet_windows.json`。namespace：`endernet_windows`（从当前资源读取，不表示已交接）。

UI 交接：待填写；脚本接入：待填写；游戏验证：待填写。

`select_item_window` 已接入通用客户端选择逻辑，接口与目录初始化流程见
[选择物品窗口技术说明](../technical_notes/select_item_window.md)。其他弹窗仍按下表待交接。

`item_list_window` 已接入 Binder 网格、单选、添加和删除，使用方式见
[物品列表窗口技术说明](../technical_notes/item_list_window.md)。

`select_fluid_window` 已接入流体识别、三来源选择与搜索，使用方式见
[流体选择窗口技术说明](../technical_notes/select_fluid_window.md)。

## 1. 制作范围与要求

device_status_window_basic 展示主控连接／冲突／支持情况，以及能源、带宽、恢复等原因。离线仍可打开；置顶防穿透、重复点击不叠窗、关闭仅隐藏详情；状态持续更新，切换设备或关闭主界面清理订阅。其他弹窗逐个填写用途。

需求依据：[Task 11](../tasks/11.%20终端与客户端同步.md)、[方块说明](../blocks.md)。具体布局和控件用途由 User 制作后填写；以下空表不是已确认的 UI 约定。

## 2. 资源与交接信息

| 项目 | 记录 |
| --- | --- |
| UI 制作者／交接日期／资源版本 | 待填写 |
| 本次交接范围／未交接部分 | 待填写 |
| 根控件；独立屏幕或嵌入 panel | 待填写 |
| 打开入口／适用设备／父界面 | 待填写 |
| 公共模板、纹理、本地化键与登记情况 | 待填写 |
| 截图或预览路径（可选） | 待填写 |

## 3. 控件功能与路径

模板定义位置与实际实例路径分开填写；运行时未核实的路径标注“待核实”。同一 JSON 中有多个模板时逐个说明，纯装饰可合并记录。

| 控件／模板 | 定义位置 | 运行时完整路径 | 类型与功能 | 脚本读取／更新内容 | 初始状态及显隐／禁用条件 |
| --- | --- | --- | --- | --- | --- |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |

## 4. 槽位、列表及组件复用

无槽位时填写“不适用”。真实槽、虚拟配置格及网络汇总条目分别记录；网络交易以稳定资源键定位，不使用显示序号。

| 槽位组／组件 | 实例路径及引用模板 | 性质与数据来源 | 数量／索引范围 | UI 索引到业务身份的映射 | 限制、单位和显示规则 |
| --- | --- | --- | --- | --- | --- |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |

- 模板参数、collection 名称及索引含义：待填写。
- 图标、数量缩写、精确提示和选中态位置：待填写。
- 滚动复用、搜索／排序、重绑时旧状态清理：待填写。
- 空槽、空列表、加载中和数据失效的表现：待填写。
- 公共组件文档及本界面实例路径、覆盖参数：待填写。

## 5. 操作流程与事件

| 控件路径 | 鼠标／触控操作与触发时机 | 动作及参数含义 | 等待回执时的表现 | 成功／失败后的表现 |
| --- | --- | --- | --- | --- |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |

- 初次打开、选择／取消、提交修改的流程：待填写。
- 弹窗层级、防穿透、重复点击和关闭范围：待填写。
- 离线、拆除／替换设备、交互距离失效的处理：待填写。
- 关闭、系统返回、失活、断线与持有物处理：待填写。
- R12 未定的键位、触控和持有物退出规则：注明待定，不自行补设。

## 6. 数据与绑定

User 先填写字段含义及已有 JSON 配置，脚本接入方核对后补充实际绑定。不得把建议名记为已实现接口。

| 控件路径 | 字段含义、类型、单位与范围 | 来源与刷新时机 | JSON 绑定名／事件名 | Python 方法 | 接入状态 |
| --- | --- | --- | --- | --- | --- |
| `/device_status_window_basic` | 详情弹窗整体显隐。根控件是 `input_panel`（阻止点击穿透），初值 `visible: false` | 由宿主界面的弹窗层实例化；`#visible` 跟随 `DeviceStatusModel.popup_visible()` | `#EnderNetDeviceStatus.popup_visible` | `device_status_popup_visible` | **已接入**（通用终端面板）；其余界面未接 |
| `.../controller_conn_stat/text` 与 `/icon/stat_mask` | 第一行：控制器连接/冲突/不支持。有原因就列**具体说明**，没有则按 `master_instance_id` 显示连接与否；遮罩绿色对勾／红色叉 | 与状态栏同一份快照（`reasons` 里 `MASTER_*` / `CONTROLLER_*` 那些码归这一行） | `#EnderNetDeviceStatus.master_text` / `.master_icon` | `device_status_master_text` / `device_status_master_icon` | **已接入**（同上） |
| `.../network_conn_stat/text` 与 `/icon/stat_mask` | 第二行：带宽、能源、重建等网络侧原因；无原因时按 `bandwidth_ok`／`power_ok` 三态说明（`None` 写「待确认」，**不写「不足」**） | 同上（其余原因码归这一行） | `#EnderNetDeviceStatus.net_text` / `.net_icon` | `device_status_net_text` / `device_status_net_icon` | **已接入**（同上） |
| `/close@endernet_ui_common.close_btn` | 只收起详情，**不关原设备界面** | 无服务端往返 | `#EnderNetDeviceStatus.close` | `OnDeviceStatusClose` | **已接入**（同上） |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |

- 已有按钮映射及按下／抬起触发约定：待填写。
- 只读展示与提交服务端命令的操作区分：待填写。
- 请求使用的设备／资源／槽位身份和版本来源：待填写。
- 快照、增量、过期响应、失败及重新同步：待填写。
- 本地化、长文本换行、数量精度：待填写。

## 7. 脚本如何使用本 UI

**2026-10-01 起 `device_status_window_basic` 部分已由脚本接入方填写**（11-13，
界面范围只有通用终端面板）；`item_list_window` / `select_item_window` /
`fluid_list_window` 系列另见各自的技术说明。

| 项目 | 使用说明 |
| --- | --- |
| Screen 文件、类名、注册入口，或公共组件的接入方式 | `device_status_window_basic` 是**模板**，没有自己的 Screen。宿主界面把它作为一个控件实例化，脚本一侧的绑定全部写在宿主类体里（如 `terminal_panel.py` 的 `TerminalPanelBase`）。 |
| namespace／根控件、打开方法与调用时机 | 实例化在 `common_terminal_panel.json` 的 `content` 里：`"device_status_popup@endernet_windows.device_status_window_basic"`，`layer: 40`（盖在关闭按钮的 30 之上），只覆盖 `offset` 与 `#visible` 绑定。**不新开界面**（不走 `PushUI`）。 |
| 参数字段、类型、必填项及可信来源 | 无 `$` 参数。两行内容来自宿主会话的 `device_status` 报文里的 `reasons`，设备身份来自宿主界面的 `device_ref`。 |
| 初始化、读取快照及订阅入口 | 与状态栏**同一个订阅**：会话开启即推首份快照，之后只在结论变化时重推。弹窗不是第二条订阅，它只是同一份数据的另一种显示。 |
| 更新文本、图标、列表、槽位及状态的方法 | 两行各一对绑定（文本 + 遮罩纹理）；整窗显隐一条 `.popup_visible`。有原因就列**每一条具体说明**（「；」连），不塌成静态的「网络故障中」。 |
| 事件回调与服务端命令的调用位置 | 打开：`#EnderNetDeviceStatus.open` → `OnDeviceStatusClick`（**幂等**，连点不叠窗，不发服务端命令）。关闭：本窗的 `#EnderNetDeviceStatus.close` → `OnDeviceStatusClose`。 |
| 关闭／销毁后的订阅、回调与缓存清理 | 弹窗关闭只把标志置假，**不动会话也不动状态栏**。宿主界面 `OnDestroy` 置空 `status`；`OnDeactive`（切到后台）收起弹窗，免得下次打开先显示一帧旧原因。 |
| 公共组件复用方法与允许覆盖的配置 | 允许覆盖 `offset`、`size`、`layer` 与 `#visible` 绑定；**不要在实例上重写控件内部结构**——两行的绑定来自模板。 |

实际调用示例：见 `terminal_panel.py` 的 `TerminalPanelBase` 类体、
`CommonTerminalPanel.OnCreate` 与 `endernet_bp/endernet_scripts/endernet/client/ui/widgets/device_status.py`。

**仍未接入**：本文件的其余模板（`item_list_window`、`select_item_window`、
`select_fluid_window`、`fluid_list_window` 及各自的 grid/can_be_selected）。

## 8. 待确认项与验证记录

| 缺项／问题 | 影响范围 | 需求依据 | 负责人 | 结论／日期 |
| --- | --- | --- | --- | --- |
| 待填写；无则写“无” | 待填写 | 待填写 | 待填写 | 待填写 |

- [ ] User 已交接资源、控件用途、槽位映射和操作流程。
      **2026-10-01**：`device_status_window_basic` 有**口头**确认「可对接」，
      但第 1～6 节仍是模板——**不勾**。
- [x] 脚本接入方已核对路径和约定，记录所有缺项。
      **2026-10-01**：核对了 `device_status_window_basic` 的实际控件树与
      绑定字段；把「根改 `input_panel`」「`binding_name_override` 驱动
      `texture`」两条**无先例**的改动记进
      [11-交付说明-设备状态](../tasks/11-交付说明-设备状态.md) 第七节。
- [x] 注册、绑定、事件、同步和实际调用说明已完成。
      **2026-10-01**：见本节第 7 节与第 6 节 `device_status_window_basic` 各行。
- [x] 离线检查完成，结果已记录。
      **2026-10-01**：`python -B endernet_tests/run_tests.py` 2602 项全绿
      （唯一红项是 `flying_item_renderer` 与槽位尺寸 16/18 的**既有**不一致，
      与本轮无关）、py2check 85 文件。
- [ ] 游戏内打开、操作、更新、关闭及重开已验证。
      **2026-10-01 部分验证**：弹窗**屏幕居中**、两行详情（主控/网络各一行，
      有原因时逐条列出）、遮罩随状态在红叉/绿勾间切换、关闭只收起弹窗，
      均在 User 的测试世界里看过。**整条不勾**：`input_panel` 拦点击穿透
      没有真的用手点过（本次是代码调回调）、重开与移动端没验、
      其余界面未接。
- [ ] 移动端、长文本、大数量和异常状态已验证（按适用范围）。

| 日期／验证人 | 环境与方式 | 覆盖范围 | 结果／证据 | 未验证项 |
| --- | --- | --- | --- | --- |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |

## 流体列表窗口补充（2026-09-28）

新增 `fluid_list_window`、`fluid_list_grid`、`fluid_can_be_selected`，沿用物品列表布局，独立使用 `endernet_fluid_list` collection 和 `#EnderNetFluidList.*` 绑定。调用、关闭、参数及验证记录见 [流体列表窗口](../technical_notes/fluid_list_window.md)。

## 设备状态弹窗在四台设备屏上的实例化（2026-10-02）

11-07 给四台容器类设备的界面（`bus_item_output_io` / `bus_item_input_io` / `bus_template_io` / `assemble_chamber`）各补了一条 `device_status_popup@endernet_windows.device_status_window_basic` 实例，参数与 `common_terminal_panel.json` 里那一条**逐字相同**（层号 40、居中、`#EnderNetDeviceStatus.popup_visible → #visible`）——**只实例化、没有新建模板**，本文件一个字节没改。离线断言钉住两件事：四份实例都在且层号高于**该屏其它所有控件**；模板本身的绑定名不变。

**2026-10-02 当日稍后已上机**（`bus_template_io` 那台，真人右键）：点状态栏那一行**真的弹出了「设备状态」详情窗**，两行各带绿勾——`已连接控制器` / `带宽满足，能源满足`，即 `interact_btn` → `device_status_window_basic` 这条交互也是通的（截图见 [11 交付说明](../tasks/11-交付说明.md) 第二十三节）。**仍有未验**：弹窗开着时点面板其它按钮会不会穿透（本轮点开弹的是真实点击，但没专门试穿透）、另外三台设备、移动端。
