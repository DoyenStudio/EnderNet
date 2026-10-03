# endernet_ui_common.json：公共 UI 控件

[返回目录](README.md) · [UI JSON](../../endernet_rp/ui/endernet_ui_common.json)

对应文件：`endernet_rp/ui/endernet_ui_common.json`。namespace：`endernet_ui_common`（从当前资源读取，不表示已交接）。

UI 交接：待填写；脚本接入：待填写；游戏验证：待填写。

## 1. 制作范围与要求

分别记录各公共模板的用途、参数与实例化方式。设备状态栏 stat_bar 对非控制器接网设备显示绿色在线／红色离线，点击打开 endernet_windows 中指定详情 panel；不以黄色未知状态作为正式第三态。

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
| `/stat_bar/stat_text` | 总体在线状态文本，**只有「在线」/「离线」两种**；颜色绿 `(0,1,0)`／红 `(1,0,0)`；结论未确认（`validity=pending`）**照样显示离线**，不设第三态 | 服务端 `DeviceNetworkStatus.state_text()`；会话开启即推首份，之后只在结论变化时重推 | `#EnderNetDeviceStatus.text` / `.color` / `.visible` | `TerminalPanelBase.device_status_text` / `device_status_color` / `device_status_visible` | **已接入**（通用终端面板）；其余界面未接 |
| `/stat_bar/stat_icon` | 状态图标纹理路径 | 同上，随在线/离线切换 | `#EnderNetDeviceStatus.icon` | `device_status_icon` | **已接入**（同上） |
| `/stat_bar/interact_btn` | 点开详情弹窗 | 无服务端往返 | `#EnderNetDeviceStatus.open` | `OnDeviceStatusClick` | **已接入**（同上） |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |

- 已有按钮映射及按下／抬起触发约定：待填写。
- 只读展示与提交服务端命令的操作区分：待填写。
- 请求使用的设备／资源／槽位身份和版本来源：待填写。
- 快照、增量、过期响应、失败及重新同步：待填写。
- 本地化、长文本换行、数量精度：待填写。

## 7. 脚本如何使用本 UI

**2026-10-01 起本节的 `device_stat_bar` 部分已由脚本接入方填写**（11-13，
界面范围只有通用终端面板）。其余模板仍待接入。

| 项目 | 使用说明 |
| --- | --- |
| Screen 文件、类名、注册入口，或公共组件的接入方式 | `device_stat_bar` 是**公共组件**，没有自己的 Screen。宿主界面在自己的类体里声明绑定，如 `endernet_bp/endernet_scripts/endernet/client/ui/terminal_panel.py` 的 `TerminalPanelBase`（`CommonTerminalPanel` 注册为 `common_terminal_panel_ui.main`）。当前组件使用宿主类体绑定。ToolDelta 动态回调名称解析缺陷已于 2026-10-03 修复，静态与动态 API 均可使用，见 [修复记录](../technical_notes/tooldelta_dynamic_binding.md)；此组件保留现有静态实现。 |
| namespace／根控件、打开方法与调用时机 | 由宿主界面实例化：`"stat_bar@endernet_ui_common.device_stat_bar"`，只覆盖 `offset`。它会**自动继承模板里的绑定**，不需要在实例上重复声明。数据不跟实例走，跟**会话**走。 |
| 参数字段、类型、必填项及可信来源 | 没有 `$` 参数。设备身份来自宿主界面的 `device_ref`（服务端 `PushUIRequest` 带来），**不接受客户端自报**。 |
| 初始化、读取快照及订阅入口 | 宿主界面 `OnCreate` 里先 `client.on_message(handler)` **再** `open_session`——服务端下一帧就会推第一份快照，晚一步就永远补不上。订阅表就是会话表，没有第二条通道。 |
| 更新文本、图标、列表、槽位及状态的方法 | 绑定名一律 `#EnderNetDeviceStatus.*`：`.text`（文字）、`.color`（绿/红）、`.visible`（拿不到结论时不显示）、`.icon`（在线/离线纹理）。回调返回安全值，且**在没有会话时也不抛**。 |
| 事件回调与服务端命令的调用位置 | `.open` → `TerminalPanelBase.OnDeviceStatusClick` → 打开详情弹窗（幂等，连点不叠窗，**不发任何服务端命令**）；详情关闭走 `endernet_windows.device_status_window_basic` 的 `.close`。 |
| 关闭／销毁后的订阅、回调与缓存清理 | 服务端一侧由 `TerminalSyncHub._forget`（会话关闭的唯一漏斗）丢掉每会话记账；界面一侧 `OnDestroy` 把 `status` 置空、`TerminalListView.close()` 调 `clear_handlers()`。绑定不摘——它们本来就不随会话生灭。 |
| 公共组件复用方法与允许覆盖的配置 | 任意**非控制器**设备界面都可以 `"stat_bar@endernet_ui_common.device_stat_bar"` 并覆盖 `offset`。控制器不套用（服务端 `ui_applicable` 判），排除**不写在 JSON 里**。 |

实际调用示例：见 `terminal_panel.py` 的 `TerminalPanelBase` 类体与
`CommonTerminalPanel.OnCreate`／`_make_status_handler`。绑定名与三份 JSON 的
一致性由 `endernet_tests/test_terminal_panel.py::TestJsonContract` 逐条钉住。

**仍未接入的**：`custom_slot`、`custom_inventory`、`custom_item_slots_grid`、
`endernet_image_button`、`endernet_text_button`、`simple_invisible_button`、
`simple_slotitem_renderer`、`close_btn`（后两个的按下名由各实例自己覆盖）。

## 8. 待确认项与验证记录

| 缺项／问题 | 影响范围 | 需求依据 | 负责人 | 结论／日期 |
| --- | --- | --- | --- | --- |
| 待填写；无则写“无” | 待填写 | 待填写 | 待填写 | 待填写 |

- [ ] User 已交接资源、控件用途、槽位映射和操作流程。
      **2026-10-01**：`device_stat_bar` 有**口头**确认「可对接」，但本节
      第 1～6 节仍是模板——**不勾**。第 1～6 节仍由 UI 归属方填写。
- [x] 脚本接入方已核对路径和约定，记录所有缺项。
      **2026-10-01**：逐条核对了 `endernet_ui_common.json`／
      `endernet_windows.json`／`common_terminal_panel.json` 的实际控件与
      绑定字段，并把三条**在引擎上没有先例**的绑定（`BF_BindColor`、用
      `binding_name_override` 驱动 `texture`、弹窗根改 `input_panel`）
      记进 [11-交付说明-设备状态](../tasks/11-交付说明-设备状态.md) 第七节。
- [x] 注册、绑定、事件、同步和实际调用说明已完成。
      **2026-10-01**：见本节第 7 节与第 6 节 `device_stat_bar` 各行；
      名称一致性由 `test_terminal_panel.TestJsonContract` 钉住。
- [x] 离线检查完成，结果已记录。
      **2026-10-01**：`python -B endernet_tests/run_tests.py` 2602 项、
      py2check 85 文件；本轮新增 53 项，分布见
      [11-交付说明-设备状态](../tasks/11-交付说明-设备状态.md) 第一节。
- [ ] 游戏内打开、操作、更新、关闭及重开已验证。
      **2026-10-01 部分验证**：状态栏文字与颜色（在线绿/离线红）、图标随
      状态切换、`interact_btn` 弹详情、详情关闭，都在 User 的测试世界里
      看过；本轮还修掉一个只在界面上看得见的缺陷（纹理只能覆盖规范的
      `#texture`，自造名会材质丢失）。**整条不勾**：点击穿透没真的用手点过、
      重开与移动端没验、另外十七个界面未接。
- [ ] 移动端、长文本、大数量和异常状态已验证（按适用范围）。

| 日期／验证人 | 环境与方式 | 覆盖范围 | 结果／证据 | 未验证项 |
| --- | --- | --- | --- | --- |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 |
| 2026-10-02 | 11-07 只读接入（离线） | `device_stat_bar` 的十一条绑定被提成共用 mixin（`client/ui/widgets/device_status_bindings.py`），**四台容器类设备的代理屏**（取货口／存货口／样板接口／装配仓）与终端屏共用同一份 | 覆盖集与既有断言逐条对得上；**离线 40 项新用例**（见 [11 交付说明](../tasks/11-交付说明.md) 第二十一节） | ~~**游戏内零**：代理屏拿不拿得到回调、状态栏在设备屏上真的画不画得出来，都没跑过~~ **当日稍后已上机**：`bus_template_io` 那台的 `stat_bar` **截图确认渲染出绿色信号图标 + 绿字「在线」**，点开详情两行绿勾；其余三台未逐台右键、移动端未验 |
| 2026-10-02（同日稍后） | 11-07 **游戏内实测**（真人右键 `bus_template_io` @ (20,68,−6)） | `device_stat_bar` 在**设备代理屏**上的完整链路：身份认领 → 会话 → `device_status` → 绑定取值 → 渲染 | 截图（界面右上绿字「在线」+ 信号图标）与绑定读数（`在线` / `[0,1,0]` / `network_ok_icon` / `已连接控制器` / `带宽满足，能源满足`）一致 | 四台里只打了这一台；移动端未验。见 [11 交付说明](../tasks/11-交付说明.md) 第二十三节 |
