# UI 制作与脚本接入文档

本目录按 UI JSON 分文件记录制作需求、控件功能、交互和脚本使用方法。

## 命名与填写规则

- 文档与 JSON **同名，仅扩展名不同**：例如 `endernet_rp/ui/common_terminal_panel.json` 对应 `plans/ui/common_terminal_panel.md`。保留大小写及现有拼写，包括 `factoric`。
- 一个 JSON 一份文档；其中多个面板或模板在该文档内分别说明。公共组件在其同名文档交接，使用方补充实例路径、参数和差异。
- 制作完成后，直接填写对应文档第 2～6 节；脚本接入方在核对明确后填写第 7 节。第 8 节分别记录交接、接入和验证。
- 不适用的栏目写“不适用”及原因。新增 JSON 时按任一同类文档结构建立同名 Markdown，并补入目录。
- 当前文档是待填写模板；资源文件存在、namespace 已记录或文档已建立，都不表示 UI 已交接或游戏验证通过。
- UI 创建、布局和新增控件由 User 完成；脚本接入方负责注册、绑定、事件、同步与服务端命令。依赖缺失 UI 约定的工作仍须等待，不能猜测控件行为或先写占位逻辑。
- 客户端只维护显示及交互状态，库存与配置以服务端为准；虚拟配置不得变成真实资源。

## 文件目录

覆盖当前 `endernet_rp/ui/` 中所有 JSON，包含设备、终端、公共模板及资源登记。列入目录不意味着新增必做界面或已确定业务功能。

| 同名文档 | 界面／资源用途 |
| --- | --- |
| [_ui_defs.md](_ui_defs.md) | UI 资源登记 |
| [assemble_chamber.md](assemble_chamber.md) | 装配仓 |
| [bus_fluid_input_io.md](bus_fluid_input_io.md) | 主动流体存货口 |
| [bus_fluid_io.md](bus_fluid_io.md) | 被动流体接口 |
| [bus_fluid_output_io.md](bus_fluid_output_io.md) | 主动流体取货口 |
| [bus_item_input_io.md](bus_item_input_io.md) | 主动物品存货口 |
| [bus_item_io.md](bus_item_io.md) | 被动物品接口 |
| [bus_item_output_io.md](bus_item_output_io.md) | 主动物品取货口 |
| [bus_template_io.md](bus_template_io.md) | 样板接口 |
| [common_terminal_panel.md](common_terminal_panel.md) | 通用终端 |
| [crafting_terminal_panel.md](crafting_terminal_panel.md) | 合成终端 |
| [creative_energy_source.md](creative_energy_source.md) | 创造能量源 |
| [dos_network_endpoint.md](dos_network_endpoint.md) | DOS 网络控制器 |
| [endernet_container_common.md](endernet_container_common.md) | 容器公共组件 |
| [endernet_terminal_inventory.md](endernet_terminal_inventory.md) | 终端背包公共组件 |
| [endernet_ui_common.md](endernet_ui_common.md) | 公共 UI 控件 |
| [endernet_windows.md](endernet_windows.md) | 公共弹窗 |
| [energy_input_interface.md](energy_input_interface.md) | 能源接入口 |
| [factoric_terminal_panel.md](factoric_terminal_panel.md) | 工厂终端 |
| [memory_slot.md](memory_slot.md) | 内存槽 |
| [NeteaseContainerUI.md](NeteaseContainerUI.md) | 网易容器 UI 资源 |
| [parallel_processor.md](parallel_processor.md) | 并行处理器 |
| [parallel_processor_connector.md](parallel_processor_connector.md) | 并行处理器连接器 |
| [storage_driver.md](storage_driver.md) | 存储驱动器 |
| [template_terminal_panel.md](template_terminal_panel.md) | 样板终端 |
| [tiny_network_endpoint.md](tiny_network_endpoint.md) | 小型网络控制器 |

## 共用规则与参考

- [Task 11](../tasks/11.%20终端与客户端同步.md) 与 [方块说明](../blocks.md) 是需求依据。
- 非控制器接网设备统一使用绿色“在线”／红色“离线”及详情弹窗；控制器不套用此流程。参见 [公共控件](endernet_ui_common.md) 和 [弹窗](endernet_windows.md)。
- 网络长列表的承载与复用模板在相关终端文档中注明；玩家背包公共组件见 [endernet_terminal_inventory.md](endernet_terminal_inventory.md)。
- R12 操作映射及持有物退出规则、黑白名单默认模式、空名单行为、过滤上限和共享槽分工，按相关规则最新定案填写，本目录不替代玩法决策。
- [模拟背包 UI 说明](../technical_notes/terminal_inventory_ui.md) 仅描述模拟行为，不能自动作为真实服务端交互约定。
