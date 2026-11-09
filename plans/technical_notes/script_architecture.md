# 设备脚本架构与存储驱动器实施交接

更新日期：2026-09-25。状态：**目标架构已确认，业务实现待开发**。示例为存储驱动器与通用终端面板；本文件不表示任务 05/06/11/12 已完成。

本文细化文件归属与设备实施边界，不替代 [方块规则](../blocks.md)、[介质规格](../items.md) 和任务契约。现有脚本根目录 `ROOT = endernet_bp/endernet_scripts/endernet/`。后续实现、交付说明和脚本评审以此为入口。

## 1. 复审结论

沿用 common / server / client 分层，设备负责自身状态，网络服务负责共享业务，适配器负责引擎访问。复审补齐以下容易遗漏的接缝：

1. 驱动器与介质的权威状态不能只存在于每网络的 StorageService；网络断开、主控移除、运行时重建不等于设备和库存消失。
2. 装卸介质是**真实物品归属交接**，包含玩家背包、驱动器槽位、介质身份、存档和负载变化，不只是修改一个列表。
3. 当前 `adapters/containers/contract.py` 是纯契约与测试替身，真实引擎代码必须另放，保持离线可导入。
4. 驱动器与终端共享会话校验；驱动器还需要自己的槽位快照与命令响应，不能只有终端库存推送。
5. 已有能力标签不等于已实例化能力对象；设备构造、生命周期接线和服务装配必须实际完成。
6. 拆除数据读取窗口、重复介质身份、未知存档版本、卸载恢复和动态带宽必须有明确处理。
7. 离线装卸权限、槽数、介质兼容范围和掉落规则仍需决策；架构不能用 UI 格数或测试默认值替玩法定案。

## 2. 目标目录

> **2026-11 目录重组**（作者要求「归为模块文件夹，并走 `__init__` 短路径导出」；规划与执行记录在 `.claude/09-repo-restructure-plan.md`，属导读层）。本节替换旧口径：旧口径「客户端统一使用现有 `ui/`，不再并列新建 `screens/`、`slots/`、`render/` 或另一个顶层 `widgets/`」作废——`client/ui/` 内按功能分子包，`widgets/` 拆散归位；旧的 `.gitignore` `dev*` 提醒随该规则删除而失效。重组前的目录树见 `git show aa37329:plans/technical_notes/script_architecture.md`。
> **状态：已完成**（2026-11-09，批次 0～5：`286412e` `28edbc8` `be4a9ea` `e3cd976` `b37c3ae` `277867f` 与本条文档提交），下面的树就是现状。

纪律沿用：不要预建空目录、空类或转发包装层；只在有实际功能时创建模块。新增文件后 `git status --untracked-files=all` 核验（`.gitignore` 的 `test*` 仍按基名命中）。

### 2.1 分包与导入规则

- **三层不动**：`common/`（引擎无关的共享词汇）、`server/`（领域包 + `adapters/` 引擎边界 + 根上的装配 `bootstrap.py` / `application.py`）、`client/`。导入边界由 `test_import_boundaries.py` 守着，口径不变。
- **层内按领域分包**；旁路通道（设备身份下行、槽位模块显示、选择器目录）在三层都叫 `channels/`；主同步通道两端留在根（`server/adapters/messages.py` ↔ `client/network_client.py`）。
- **命名**：snake_case；文件名去掉与所在目录同义的前缀/后缀（`terminal/panel.py`、`windows/item_list.py`、`orders/archive.py`、`resources/key.py`）；不与 py2 标准库同名（`devices/bus_io.py` → `devices/bus_io.py`）。
- **包门面**：层根包 `endernet/` `common/` `server/` `client/` 与 `client/ui/` 不导出、模块顶层不 import；其余包的 `__init__.py` 只用 `from .<直接子模块> import 名字` 导出**包外生产代码在用**的名字，并列入 `__all__`。导入期需要引擎的模块（client 的屏、窗口、类体绑定）与 `storage/service.py`（集成模块，进门面会成环，待拆分）不进门面。守卫：`test_package_layout.py`。
- **导入写法**：包内写模块路径，不经自己或祖先包的门面取名字；包间优先走门面短路径，门面没导出的名字写模块路径；取模块对象写 `from ..shared import slot_layout`；测试与脚本写模块路径（`patch` 的目标必须是查名字的那个模块）。

### 2.2 目录

```text
endernet_scripts/
├── modMain.py                                   模组注册入口
└── endernet/
    ├── client/                                   客户端（可依赖引擎）
    │   ├── bootstrap.py                          客户端装配与启动/销毁。
    │   ├── network_client.py                     终端同步通道的客户端侧（任务 11-01 / 11-02 的客户端…
    │   ├── channels/                             旁路通道的客户端接收侧
    │   │   ├── device_identity.py                设备界面的身份认领（11-07）。
    │   │   ├── item_catalog.py                   Request the selector catalogue on…
    │   │   └── slot_display.py                   槽位模块显示的客户端一侧（2026-11-05）：把服务端的读数写…
    │   └── ui/                                   界面登记 `register_screens()`（延迟导入各屏）
    │       ├── device_status/                    设备状态栏与详情弹窗：纯模型、类体绑定
    │       │   ├── bindings.py                   设备状态栏与详情弹窗的类体绑定（11-13 / 11-07）。
    │       │   └── model.py                      设备状态栏与详情弹窗的离线可测那一半（11-13）。
    │       ├── devices/                          容器类设备的代理屏与接口配置
    │       │   ├── panels.py                     容器类设备的屏（11-07）。
    │       │   ├── port_config_model.py          接口配置的客户端模型（11-07）：权威值、乐观值与在途意图。
    │       │   ├── port_panel.py                 接口设备屏的配置写入半边（11-07）：读权威值、发意图、画控件。
    │       │   └── proxy_panel.py                容器类设备屏的基座（11-07）。
    │       ├── order_preview/                    下单预览：窗口控制器、纯模型、类体绑定
    │       │   ├── bindings.py                   下单预览窗口的类体绑定（11 增量）。
    │       │   ├── model.py                      下单预览窗口的显示状态（纯模型，不认识引擎、不发命令）。
    │       │   └── window.py                     下单预览窗口的控制器：收发、生命周期、与网络客户端的接线。
    │       ├── shared/                           跨屏共用、引擎无关的界面工具
    │       │   ├── button_utils.py               客户端 Ore UI 按钮的触摸反馈。
    │       │   ├── control_api.py                两个控件 API 的形状坑，收在一处（都是 2026-10-03 …
    │       │   ├── flying_item.py                槽位之间的飞行动画：把 content.flying_items/…
    │       │   ├── slot_layout.py                本界面各槽位在屏幕上的全局矩形，给飞行动画算起终点。
    │       │   └── stack_size.py                 一个物品的堆叠上限与 id_aux，按资源名缓存。
    │       ├── terminal/                         四个终端屏与终端专用逻辑
    │       │   ├── backpack_model.py             下半屏的真实玩家背包：读取、选中、以及「这一点击要存什么」。
    │       │   ├── cache.py                      终端界面的显示缓存：把线上的快照分片与增量批组装成一份列表。
    │       │   ├── crafting_panel.py             合成终端界面（11-04）：通用终端的一切 + 九宫格工作台。
    │       │   ├── factoric_panel.py             工厂终端界面（11-06）：一条网络的活跃订单、剩余内存与配方树进…
    │       │   ├── hover.py                      PC item tooltips, using the same …
    │       │   ├── input.py                      Desktop slot gestures. Collection…
    │       │   ├── inventory_model.py            本地模拟背包：已无生产消费者，只作测试夹具保留。
    │       │   ├── item_list_model.py            通用终端长列表的客户端模型（任务 11-09）。
    │       │   ├── panel.py                      Reusable terminal screen and the …
    │       │   ├── template_panel.py             样板终端界面（11-05）：通用终端的一切 + 一份样板配置区。
    │       │   └── touch_input.py                触屏的长按拆堆与拖动，照原版 Pocket 容器（User 202…
    │       ├── universal_importer/               万用样板采集器
    │       │   ├── config.py                     万用样板采集器的进程内结果；只有显式保存才替换内容。
    │       │   └── screen.py                     跨界面悬浮的工厂样板物品采集器。
    │       └── windows/                          辅助窗口（名单、选择器、数量窗）
    │           ├── fluid_list.py                 Fluid identifier list editor shar…
    │           ├── item_list.py                  物品名单编辑器：就地编辑一份物品标识列表（11-07 起）。
    │           ├── list_bindings.py              三个辅助窗口的类体绑定：物品名单／流体名单／物品选择器（11-07…
    │           ├── names.py                      辅助窗口的身份常量：_vars 键、collection 名、绑定…
    │           ├── number_set.py                 数量窗：嵌在任意 ToolDeltaScreen 里的一个整数输入…
    │           ├── select_fluid.py               Fluid picker using the item picke…
    │           └── select_item.py                物品选择器：宿主静态搜索绑定，网格按窗口生命周期动态绑定。
    ├── common/                                   引擎无关的共享词汇
    │   ├── crafting_recipe.py                    工作台预设配方：规范化与摆位匹配（11-04）。
    │   ├── definitions.py                        设备、介质、连接与数值配置表。
    │   ├── identity.py                           位置、区块与设备实例身份。
    │   ├── protocol.py                           请求、响应、回执与增量通知的共用词汇。
    │   ├── rate_limit.py                         按玩家限速：一个窗口内只放行一次。
    │   ├── settings.py                           全局开发检查开关；外部数据解码仍执行格式与业务校验。
    │   ├── channels/                             旁路通道的协议半边：事件类与载荷编解码
    │   │   ├── device_identity.py                设备界面的身份下发（11-07）。不 import 引擎。
    │   │   ├── fluid_catalog.py                  Fluid naming rules, with item-exi…
    │   │   ├── item_catalog.py                   Item selector catalogue protocol;…
    │   │   └── slot_display.py                   槽位模块显示：驱动器里的硬盘、内存槽里的内存条（2026-11-0…
    │   └── resources/                            资源身份、整数数量、物品载体与会话内资源描述（纯值）
    │       ├── amount.py                         物料数量的显示格式；单位物与资源原生数量由调用者明确区分。
    │       ├── item_codec.py                     共享物品边界：只依赖 Item 的字段协议，构造器由端侧注入。
    │       ├── key.py                            资源身份与整数单位尺度。
    │       ├── medium_item.py                    存储介质物品的载体与展示契约（引擎无关）。
    │       └── sync.py                           会话内资源描述映射，供服务端发送与客户端解码分别持有。
    └── server/                                   服务端：领域包 + 引擎边界 + 装配
        ├── application.py                        应用服务：业务命令的唯一对外入口与只读查询门面。
        ├── bootstrap.py                          服务端装配与启动/销毁。
        ├── adapters/                             引擎边界（唯一可 import tooldelta/mod.* 的服务端目录，且只在函数体内）
        │   ├── engine_text.py                    引擎边界上的字符串转换：传 str，不要传 unicode。
        │   ├── events.py                         引擎事件到归一化事件的绑定。本模块是引擎边界。
        │   ├── messages.py                       终端同步通道的引擎适配器（任务 11）。本模块是引擎边界。
        │   ├── order_notifications.py            订单完成提示与声音的引擎边界。
        │   ├── persistence_engine.py             真实存档后端：世界 ExtraData（任务 12-02 选定的后…
        │   ├── recipes.py                        工作台预设配方的引擎适配（11-04）。
        │   ├── template_tips.py                  样板提示的资源显示名查询。
        │   ├── world.py                          世界查询适配器：本模块是引擎边界，唯一允许 import tool…
        │   ├── channels/                         旁路通道的引擎发送侧
        │   │   ├── device_identity.py            设备身份下行的引擎侧实现（11-07）。
        │   │   ├── item_catalog.py               Read-only catalogues for the clie…
        │   │   └── slot_display.py               槽位模块显示的引擎出口（2026-11-05）。
        │   ├── compat/                           跨模组兼容（transferlib/radium 的流体与 FE）
        │   │   ├── energy.py                     Six-sided radium FE receiver. The…
        │   │   └── fluids.py                     Bridge radium's public fluid tran…
        │   └── containers/                       容器契约与真实容器/背包的引擎实现
        │       ├── contract.py                   统一容器契约与假适配器。
        │       └── engine.py                     真实容器与玩家背包的引擎实现。
        ├── crafting/                             样板、规划、订单与终端设备侧（07–10、11）
        │   ├── patterns.py                       配方结构与样板编解码（任务 07-01 / 07-02）。
        │   ├── orders/                           订单、执行器、槽位/内存租约、回流、恢复快照、装配仓执行
        │   │   ├── archive.py                    订单恢复快照。保存真实资源键与逐次执行账，不从规范文本反解身份。
        │   │   ├── assembly.py                   07-06 本地装配。09 持有租约，06 管理跨容器转移，10 …
        │   │   ├── capacity.py                   订单的处理器槽位与内存租约（任务 09-03）。
        │   │   ├── executor.py                   订单执行器与工厂服务门面（任务 09-02 / 09-04 / 0…
        │   │   ├── jobs.py                       订单、步骤状态机、等待台账与订单集合（任务 09-01 / 09-…
        │   │   └── returns.py                    产物回流与分配（任务 10-01 … 10-08）。
        │   ├── planning/                         只读规划与下单预览
        │   │   ├── planner.py                    合成规划服务（任务 08-01 至 08-07）。
        │   │   └── preview.py                    下单预览：一次只读的分帧规划。
        │   ├── providers/                        样板提供者能力契约与提供者状态/产物索引
        │   │   ├── execution.py                  样板提供者能力与预设配方契约（07）。
        │   │   └── store.py                      样板提供者状态与产物索引（任务 07-04）。
        │   └── terminals/                        合成终端工作台、样板终端设备侧状态、终端实体槽原语
        │       ├── slots.py                      Slot primitives for the physical …
        │       ├── template.py                   样板终端（11-05）的设备侧状态。
        │       └── workbench.py                  合成终端的工作台：九宫格、配方保留与导入计划（11-04）。
        ├── devices/                              设备注册、能力契约与各设备状态
        │   ├── bus_io.py                         网络接口设备：总线接口、存货口、取货口与样板口。
        │   ├── capabilities.py                   设备通过节点暴露的能力接口。
        │   ├── catalog.py                        方块种类到设备定义与端口规则的注册表。
        │   ├── memory_slot.py                    内存槽状态集合：网络的内存容量与内存槽能耗的唯一权威。
        │   ├── registry.py                       设备注册表：位置、区块与实例代次索引。
        │   ├── slot_display.py                   槽位模块显示的服务端一侧：从驱动器 / 内存槽的读数算出每槽的模型…
        │   └── storage_driver.py                 存储驱动器设备：槽位状态、介质归属与真实的存储提供者实现。
        ├── network/                              拓扑、准入、主控、服务装配与运行时
        │   ├── admission.py                      带宽负载准入：负载域容量、设备获准与 bandwidth_ok。
        │   ├── controller.py                     主控制器：唯一准入出口、网络运行状态与只读设备状态聚合。
        │   ├── energy.py                         网络能源服务：按定义表结算耗能，按供能设备判定供电。
        │   ├── node.py                           网络节点、连接端口与端口规则。
        │   ├── runtime.py                        网络运行时：一个连通分量对应一份可运行的服务集合。
        │   ├── services.py                       网络服务的注册、装配表与替身。
        │   ├── ticking.py                        网络调度：跨网公平轮转、唤醒路由与 SchedulerServic…
        │   └── topology.py                       拓扑服务：连通分量、控制器计数、负载域结构与图版本发布。
        ├── persistence/                          存档契约、恢复闸门与迁移
        │   ├── migrations.py                     存档版本迁移。
        │   ├── recovery.py                       分阶段恢复的闸门与一致性检查（12-05 / 12-08）。
        │   ├── repository.py                     存档仓库契约与内存替身。
        │   ├── resources.py                      资源条目的恢复边界；尚不接入真实介质后端。
        │   └── terminals.py                      在正常保存边界写出两类终端的设备库存。方块实体只携带实例标记。
        ├── runtime/                              生命周期、事件队列与调度原语
        │   ├── events.py                         归一化事件与事件队列。
        │   ├── lifecycle.py                      设备生命周期：放置、邻接、拆除、加载、卸载与启动补查。
        │   └── scheduler.py                      逻辑时钟、到期队列与任务代次（任务 04-01 / 04-02）。
        ├── storage/                              介质、转移端点、终端显示投影与每网络存储服务
        │   ├── medium.py                         单个存储介质的权威状态、容量核算与共享读写额度。
        │   ├── projection.py                     终端网络列表的显示投影：真实库存与网络样板产物的并集。
        │   ├── service.py                        每网络一份的存储服务：介质挂载、库存汇总、预留与受控读写。
        │   └── transfer.py                       网络与外部容器之间的资源转移：端点、预检、受控提交与中转缓冲。
        └── sync/                                 终端会话与同步发布
            ├── device_config.py                  接口配置的上行编码与订阅记账（11-07）。
            ├── device_status.py                  设备总体状态的上行编码与订阅记账（11-08 / 11-13）。
            ├── sessions.py                       终端会话：身份、有效期、校验与注销（11-01）。
            ├── terminal.py                       终端同步中枢：订阅、发布窗口、快照与增量（11-02）。
            └── transfer.py                       发布窗口的纯计算部分：分片、冻结快照、增量批次。
```

UI JSON 继续位于 `endernet_rp/ui/`（2026-11-09 起分组在 `endernet_rp/ui/endernet/<分组>/`），不移入脚本目录。旧规划的 `view_models.py` 职责分配到具体模型，不保留第二份缓存；库存发布职责在 `sync/terminal.py`，不要为同一批数据再建一套发布器。

暂不新增服务端 `devices/common_terminal.py`：通用终端没有独立库存，其身份由通用注册表管理，会话由 sync 管理。若需要向节点注册 `TerminalEndpoint` 能力，由装配层注入 sync 提供的实际实现；将来有终端专属持久配置或升级槽，再增加设备模块。

### 2.3 旧路径 → 新路径（2026-11 重组）

文档、交付说明与台账里的旧路径已按此表改写；`plans/ui/` 的交接文档未改（作者另行处理）。

| 旧路径（相对 `endernet/`） | 新路径 |
|---|---|
| `client/device_identity.py` | `client/channels/device_identity.py` |
| `client/item_catalog.py` | `client/channels/item_catalog.py` |
| `client/slot_display.py` | `client/channels/slot_display.py` |
| `client/ui/backpack_model.py` | `client/ui/terminal/backpack_model.py` |
| `client/ui/button_utils.py` | `client/ui/shared/button_utils.py` |
| `client/ui/crafting_panel.py` | `client/ui/terminal/crafting_panel.py` |
| `client/ui/device_panels.py` | `client/ui/devices/panels.py` |
| `client/ui/factoric_panel.py` | `client/ui/terminal/factoric_panel.py` |
| `client/ui/fluid_list_window.py` | `client/ui/windows/fluid_list.py` |
| `client/ui/flying_item.py` | `client/ui/shared/flying_item.py` |
| `client/ui/inventory_model.py` | `client/ui/terminal/inventory_model.py` |
| `client/ui/item_list_model.py` | `client/ui/terminal/item_list_model.py` |
| `client/ui/item_list_window.py` | `client/ui/windows/item_list.py` |
| `client/ui/number_set_window.py` | `client/ui/windows/number_set.py` |
| `client/ui/order_preview_window.py` | `client/ui/order_preview/window.py` |
| `client/ui/port_config_model.py` | `client/ui/devices/port_config_model.py` |
| `client/ui/port_panel.py` | `client/ui/devices/port_panel.py` |
| `client/ui/proxy_panel.py` | `client/ui/devices/proxy_panel.py` |
| `client/ui/select_fluid_window.py` | `client/ui/windows/select_fluid.py` |
| `client/ui/select_item_window.py` | `client/ui/windows/select_item.py` |
| `client/ui/slot_layout.py` | `client/ui/shared/slot_layout.py` |
| `client/ui/stack_size.py` | `client/ui/shared/stack_size.py` |
| `client/ui/template_panel.py` | `client/ui/terminal/template_panel.py` |
| `client/ui/terminal_cache.py` | `client/ui/terminal/cache.py` |
| `client/ui/terminal_hover.py` | `client/ui/terminal/hover.py` |
| `client/ui/terminal_input.py` | `client/ui/terminal/input.py` |
| `client/ui/terminal_panel.py` | `client/ui/terminal/panel.py` |
| `client/ui/touch_input.py` | `client/ui/terminal/touch_input.py` |
| `client/ui/universal_importer.py` | `client/ui/universal_importer/screen.py` |
| `client/ui/universal_importer_config.py` | `client/ui/universal_importer/config.py` |
| `client/ui/widgets/control_api.py` | `client/ui/shared/control_api.py` |
| `client/ui/widgets/device_status.py` | `client/ui/device_status/model.py` |
| `client/ui/widgets/device_status_bindings.py` | `client/ui/device_status/bindings.py` |
| `client/ui/widgets/list_window_bindings.py` | `client/ui/windows/list_bindings.py` |
| `client/ui/widgets/order_preview.py` | `client/ui/order_preview/model.py` |
| `client/ui/widgets/order_preview_bindings.py` | `client/ui/order_preview/bindings.py` |
| `client/ui/widgets/window_identity.py` | `client/ui/windows/names.py` |
| `common/device_identity.py` | `common/channels/device_identity.py` |
| `common/fluid_catalog.py` | `common/channels/fluid_catalog.py` |
| `common/item_catalog.py` | `common/channels/item_catalog.py` |
| `common/item_codec.py` | `common/resources/item_codec.py` |
| `common/medium_item.py` | `common/resources/medium_item.py` |
| `common/resource_amount.py` | `common/resources/amount.py` |
| `common/resource_key.py` | `common/resources/key.py` |
| `common/resource_sync.py` | `common/resources/sync.py` |
| `common/slot_display.py` | `common/channels/slot_display.py` |
| `server/adapters/containers.py` | `server/adapters/containers/contract.py` |
| `server/adapters/containers_engine.py` | `server/adapters/containers/engine.py` |
| `server/adapters/device_identity.py` | `server/adapters/channels/device_identity.py` |
| `server/adapters/energy.py` | `server/adapters/compat/energy.py` |
| `server/adapters/fluids.py` | `server/adapters/compat/fluids.py` |
| `server/adapters/item_catalog.py` | `server/adapters/channels/item_catalog.py` |
| `server/adapters/slot_display.py` | `server/adapters/channels/slot_display.py` |
| `server/crafting/assembly.py` | `server/crafting/orders/assembly.py` |
| `server/crafting/capacity.py` | `server/crafting/orders/capacity.py` |
| `server/crafting/execution.py` | `server/crafting/providers/execution.py` |
| `server/crafting/executor.py` | `server/crafting/orders/executor.py` |
| `server/crafting/jobs.py` | `server/crafting/orders/jobs.py` |
| `server/crafting/order_archive.py` | `server/crafting/orders/archive.py` |
| `server/crafting/planner.py` | `server/crafting/planning/planner.py` |
| `server/crafting/preview.py` | `server/crafting/planning/preview.py` |
| `server/crafting/returns.py` | `server/crafting/orders/returns.py` |
| `server/crafting/store.py` | `server/crafting/providers/store.py` |
| `server/crafting/template_terminal.py` | `server/crafting/terminals/template.py` |
| `server/crafting/workbench.py` | `server/crafting/terminals/workbench.py` |
| `server/devices/io.py` | `server/devices/bus_io.py` |
| `server/storage/terminal_slots.py` | `server/crafting/terminals/slots.py` |
| `client/ui/widgets/__init__.py` | 删除（`widgets/` 的内容分到 `shared/` `windows/` `order_preview/` `device_status/`） |

## 3. 数据所有权与依赖

| 数据／行为 | 唯一职责方 | 约束 |
| --- | --- | --- |
| 世界设备身份、加载状态、朝向 | DeviceRegistry | 不在驱动器另建一套位置／区块索引 |
| 驱动器槽位、配置版本、介质引用 | storage_driver.py | 不复制介质库存；状态不依赖某个网络实例存活 |
| 介质稳定 ID、型号、资源内容、额度 | medium.py 与其持久记录 | 同一介质只有一份权威状态，跨网或拔插不重置额度获利 |
| 介质当前可交易挂载、库存汇总、预留 | 每网络 StorageService | 汇总是派生索引；卸载只解除贡献，不清空介质 |
| 玩家背包及介质物品的实际交接 | transfer.py + 容器适配器 | 同一时刻只有一处拥有真实物品，无法核对时返回不确定 |
| 样板槽的实际内容（样板接口 9 格） | **方块容器本身**（`store.py` 只读它） | 样板是自包含物品，配方编码在自己的 `userData` 里；权威在世界侧，不在内存 |
| 产物 → 样板／提供者的索引、样板版本（内容指纹） | crafting/providers/store.py | **派生缓存**：每次从容器重读重建，不存档、不迁移、不构成第二份真相 |
| 配方结构、编码格式版本、R09 策略（无限燃料 **2026-09-30 已定案**；返还物显式声明 **同日已定案**；两类样板适用范围未定） | crafting/patterns.py | 不记录外部机器类型；可用性活在提供者上（架构规划「配方定义与当前可用的提供者分离」） |
| 规划结果（批次、依赖步骤、缺口、复核凭据） | crafting/planning/planner.py | **只读计算**：不扣库存、不预留、不占真实槽位。两份输入（库存、样板索引）由调用方采集并**深拷贝成纯值**，因此规划结果不随世界变化而变；**计划不是预留**——09 提交前必须 `revalidate()` 再按 `claims` 逐个 `reserve()` |
| 样板提供者的执行半边（投料与回流） | **09 的订单执行器** | 07 只提供只读的样板列表；`push_inputs` 必须是 06 的 `port_stage` 命令的薄封装，不另开绕过主控账本的投料路径。**那条路已经跑通**：`executor.OrderExecutor._port_stage()` 就是它。**07-05 已按此落地**（2026-09-27，台账 S-07）：`push_inputs` → `FactoryService.stage_inputs` → 同一个 `_port_stage`，命令与请求号形状只有一个来源；**装配回流已接线**（07-06 / 台账 S-11）：`assembly_advance` 经 06 按槽转移，真实回库后通知 10；离线完成、游戏内未验 |
| 订单状态、预留凭据、执行批次与等待台账 | crafting/orders/jobs.py（领域）+ executor.py（推进） | **每网络一份**（服务槽位逐运行时各造一套），两张网络不共享订单。容量从 02 的处理器群与网络成员**每帧重算**，不另建设备索引。**这条是「拉」不是「推」**（2026-09-28 台账 D-03 定案）：02 曾经发过一份「槽位来源变化」清单，正因为与本行相抵而无人消费，已连同 `ProcessorGroup.slot_count()` 一并删除；读法是 `ProcessorGroup.contributes_slots()` + `.slots`，去重由 `crafting/orders/capacity.py::slot_capacity` 自己做 |
| 每执行实例每产物的剩余需求与 `op_id` 去重 | crafting/orders/jobs.py 的 `WaitingLedger` | 全局的「资源键 → 等待实例」索引与分配顺序（R06）**归 10**；接缝见 [订单与回流的执行台账契约](order_returns_contract.md) |
| 设备槽位快照、终端库存快照、在线状态 | 对应 sync 模块 | 读业务结果，不维护第二套库存或准入规则 |
| 物品图标、选择、排序、数量文本 | client/ui | 仅显示与交互；不能上传本地余额当作提交结果 |

`bootstrap` 持有驱动器状态集合并注入相关服务；初期可由 `storage_driver.py` 内的小型集合管理，按设备实例引用索引。这是专属状态集合，不是第二个通用设备注册表。网络服务通过提供者获取介质，不能直接篡改其他服务的内部表。

common 和 server 领域模块不导入 ModSDK 或 ToolDelta 引擎模块。sync 也通过注入的收发函数和资格查询访问引擎；真实引擎依赖集中在 adapters、启动层和 client。依赖方向为装配层 → 领域／适配器，适配器 → 契约；避免设备类反向 import StorageService 后再互相调用。

## 4. 必须接上的现有代码入口

以下名字是已存在的接口，实施时仍应检查最新代码；新增内部方法名可调整，语义不可省略。

| 现有入口 | 驱动器实现要求 |
| --- | --- |
| `DeviceRegistry` / `DeviceRecord.ref` | 创建、加载、替换均检查设备代次；专属状态跟随设备生命周期 |
| `StorageProvider` / `CapabilityRegistry` | 实现介质枚举与受控读写；明确实际能力对象在哪里构造、保存和取得，不能只返回标签 |
| `NetworkService.add_node/remove_node/note_domain_change/save_node_data` | 幂等挂卸与迁移接入；`remove_node` 不等于世界方块被拆除 |
| `ServiceFactory` / `default_builders()` | 正式存储服务通过现有装配接入；构造、就绪、恢复、缺依赖都要测试 |
| `ApplicationService` / 主控命令路由 | `configure` 当前路由到 STORAGE；sync 校验会话后仍须走主控，不能直接改槽位 |
| `DeviceRecord.config["disk_count"]` | 当前负载读取点。由权威槽位派生并投影到这里，拒绝客户端直接提交计数 |
| `AdmissionService.note_device_load_changed(node_key)` | 装卸成功后更新计数并通知负载重算；必须早于后续交易使用旧准入 |
| `LogicalClock` / 调度器 | 所有访问共享介质额度；不能每请求、每终端单独获得额度，也不能用方块 tick 当计时基准 |
| `Record` / `WriteCoordinator` / `resources.py` | 复用版本、归属、脏写、故障与未知资源保留机制 |

现有 `runtime@component_id` 只适合进程内检查点，不能作为介质持久 ID。单独实现 StorageService 不会使整个网络自动可用：ENERGY 等替身和未定规则仍可能拦截主控准入。离线测试可显式注入替身，不得把真实装配改成无条件放行。

## 5. 存储驱动器的实施顺序与提交边界

### 5.1 先做设备与介质模型

实现驱动器槽位、允许的介质型号、槽位版本、稳定介质 ID 和可序列化状态。单介质库存复用 ResourceKey 和整数单位，规格取 definitions，不从 UI 标签解析，不把硬盘内容复制到驱动器或控制器。有内容的介质必须保持身份；首次初始化 ID 只能在服务端受控交接中进行。

驱动器支持哪些磁带／软盘／硬盘、槽位数以及带宽与能耗中“硬盘个数”的具体计数范围尚需定稿（R10）。定义表中有某型号不自动表示这个驱动器接受它。

### 5.2 接生命周期与网络挂载

| 事件 | 必须执行 |
| --- | --- |
| 新放置／首次恢复 | 核对设备身份，读取或建立专属状态；恢复校验完成前禁止业务 |
| 加入有效网络 | 根据最终可用提供者集合挂载介质，防止同 ID 双挂载 |
| 失去准入／断电 | 禁止交易，保留权威内容；缓存保留不等于可交易 |
| 拆线／跨网迁移 | 冻结旧交易与旧会话，解除旧贡献后挂到新网；旧代次许可无效 |
| 区块卸载 | 取消可交易挂载与相关订阅，按保存边界保存／保留数据，不生成掉落 |
| 区块重载 | 按持久身份恢复与幂等挂载，不能再分配一个新介质 ID |
| 方块拆除／同位置替换 | 停止旧实例写入，完成拆除归属协议；新实例不继承旧引用 |
| 世界退出 | 关闭交易和会话，完成约定的脏写与监听清理 |

真实需要读取的拆除方块数据必须在 `BlockRemoveServerEvent` 的处理当帧取得，随事件携带不可变快照；不能到下一帧队列消费时回查世界。现有 remove 载荷尚无方块实体数据，接线须补齐，详见 [引擎事件说明](engine_block_events.md)。

### 5.3 装入／取出介质

容量 Lore 的身份投影、按需合并刷新和交接时一致性详见 [专门实施契约](medium_lore.md)。内容提交只维护摘要/脏版本，真实物品写回不进入每笔存取的热路径；身份迁移先完成 12-02，纯领域摘要可独立交付。

介质物品详情要求见 `items.md`：`medium.py` 从单介质权威内容导出剩余类型/数量摘要；驱动器与交接适配层负责将摘要写入 `item.userData["display"]["Lore"]` 并同步真实物品。首次初始化、内容实际提交和恢复后标记刷新，拔出或生成既定掉落物前使用最新内容版本。Lore 只作展示，不承担介质身份或内容持久化；容量行更新保留其他元数据，校验槽位/身份/版本，避免覆盖并发换入物品。

1. 请求包含操作 ID、设备实例、期望槽位版本、目标槽，以及玩家源／目标真实背包槽与指纹；玩家身份从可信消息上下文取得。
2. 会话校验玩家、维度、距离和设备；主控检查当前网络、设备代次与执行许可。校验输入边界、介质类型、重复 ID、目标空间和恢复状态。
3. 在服务端主线程冻结受影响介质的并发存取，形成交接方案：真实介质物品、槽位引用、库存记录和拥有者必须一起考虑。
4. 通过已核对的容器能力完成交接并记录确认结果。具体写盘／物品写入先后由真实后端能力决定；必须记录各失败点的当前持有者与恢复动作。不能用“先删除源再假定目标成功”实现事务。
5. 只有确认成功的部分更新槽位、挂载集合、存档版本及派生计数；重算负载并使旧许可失效后再允许后续交易。无介质可用或失去准入时，库存可以保留但不可继续访问。
6. 提交完成后发槽位快照、状态变化和库存变化。重发已确认的操作只返回原结果，不再次拔盘或扣物品；不确定操作保留可核对记录，禁止盲目重放。

装卸介质不是新物品产出，不能送入订单产物回流计数。拔盘引起预留失效时按 05 契约撤销有效性并通知订单重验；不提前实现订单调度算法。

### 5.4 持久化与恢复

驱动器记录保存槽位引用与版本；介质记录保存稳定 ID、型号、内容和所需恢复信息；介质物品如何携带身份、内容究竟存在物品还是世界记录中，由 12-02 验证后选择**一套权威方案**。容量大小必须实测，不能假定全部库存适合塞进物品自定义数据。

重复 ID、缺失记录、未知 schema、损坏数据或缺失资源注册项必须保留原载荷、给出诊断并限制交易，不得当成空盘覆盖，也不得自动复制为两个新 ID。插拔、换网、重启时的读写额度恢复不能成为刷额度路径，具体恢复策略须记录。

恢复顺序为读取记录 → 核对加载设备与介质归属 → 重建网络与准入 → 挂载与重建索引 → 核对预留 → 开放业务。不能单靠运行时序列化就宣称真实存档已接通。

### 5.5 界面与同步

界面创建由 User 全权完成，包括物品槽位、按钮、开关及其他控件、布局和视觉资源。User 完成相应界面并交接后，脚本实现方才对接绑定、事件和同步，并按需要微调现有 UI；缺失界面或新增控件仍交由 User 创建。缺少 UI 时，涉及该 UI 逻辑的工作必须暂停，不得假设任何 UI 逻辑或通过占位、模拟实现绕过；依赖该 UI 交互约定的服务端、协议及数据模型也在暂停范围内。仅可继续无需任何 UI 假设的独立工作，待 User 完成并交接相应 UI 后再恢复对接。具体执行顺序遵循 [11 的 UI 分工与实施前置](<../tasks/11. 终端与客户端同步.md#ui-分工与实施前置>)。

复用 `storage_driver.json` 和公共状态控件。驱动器同步自己的槽位、介质型号、容量／类型使用量、版本和阻塞原因；不持续订阅整个网络物品列表。槽位的真实物品交接与配置展示分开，客户端只提交意图。

会话公共身份与关闭清理放 sessions；设备槽位同步放 devices；库存长列表发布放 terminal；设备总体状态**聚合**放 `network/controller` 的 `compose_device_status`（只读），上行编码与订阅记账放 `sync/device_status`。状态的订阅表**就是会话表**（不开第二条会话），清理点只有 `TerminalSyncHub._forget` 一处。业务写请求复用主控去重账本，不在每个 sync 模块另建一份互不相知的交易去重表。界面关闭不应销毁权威驱动器状态或未确认交接记录。

通用终端按 [增量索引说明](terminal_item_index.md) 实现。现有模拟 UI 仅作组件起点，不直接上传本地模拟结果；驱动器不要求先完成整个终端。静态 UI 绑定实施时读取 `tooldelta-ui-binding` 技能并核对实际 JSON 与 Screen，不能把占位按钮当作已接线。

## 6. 待定规则与可先实施的范围

| 项目 | 当前边界 | 未定时如何推进 |
| --- | --- | --- |
| 驱动器槽数、介质兼容范围与计数范围 | R10；UI 格数不代表已定规则 | 注入具名策略做离线验证；正式交付报告未定值 |
| 带宽取整、恰好满载语义 | R02 / R13 | 复用现有策略，不在驱动器再写一套 |
| 无主控／断电／超载时装卸介质 | 现有 configure 受主控门控；仅离线诊断已明确允许 | 暂遵守现有门控；若需离线维护，先明确权限、归属和专门命令契约，不绕过主控 |
| 拆除后介质掉落、保管及写入失败处置 | 与 R05、12-03/06 联动 | 先实现可注入的归属协议与故障测试，不自行选择丢弃或复制掉落 |
| 真实物品与存档后端 | 12-02 / 13 的实际 SDK 验证 | 内存后端只能交付领域层，真实恢复与游戏可用保持未完成 |
| 容量单位、流体与混合预算 | 05 与 R07 | 首阶段可实现物品路径；保留不支持的数据，不能清空流体 |
| 秒与逻辑帧换算、供能和额度恢复策略 | 04 / R08 与 05/12 接缝 | 注入明确测试时基，不硬编码方块 tick 频率或无限能源 |

以上不要求先停止所有开发：设备模型、接口接线、内存后端、错误语义与离线测试可以先完成。受未定规则影响的功能不能标记正式验收。

## 7. 给存储驱动器实现者的交付范围

本轮以存储驱动器为主，优先完成 05-01 至 05-04 中相关部分、满足挂载与查询的最小 05-05、12-03 所需数据结构和恢复接线。涉及真实装卸时必须一并接入 06 的物品交接、11-07/11-13 的界面与状态、12 的真实后端；不能把这些标为别人的任务后宣称设备已可玩。

可以分两阶段交付，但必须明确标记：

- **脚本领域层交付**：纯模型、挂卸、查询、生命周期、受控服务入口、故障注入和离线测试完整；引擎后端／UI／真实能源未接入时逐项记录，保留准入拦截。
- **游戏设备交付**：真实玩家交互、物品装卸、持久化、状态界面和游戏回归均走通；用实际记录说明范围，不以替身或测试全绿代替。

不要求本轮实现订单、合成、完整通用终端或流体接入。不为未来功能创建空实现；尚不支持的命令明确拒绝，不能返回假成功。只有类文件、没有 bootstrap 与主控调用路径的实现，不算脚本层完整交付。

## 8. 脚本评审与验证清单

- [ ] 目录遵守上述归属；Python 2.7 兼容；内部相对导入；未修改 ToolDelta 子模块；领域导入不依赖引擎。
  （**09 的增量已离线核过这一条**：`test_import_boundaries.py` 与 py2 门禁都在跑，
  60 个文件 0 违规；但「目录归属」里 `capacity.py` / `jobs.py` / `executor.py`
  三份新文件尚未经人评审。）
- [ ] 设备、介质、网络索引各有唯一数据源；快照不泄露可直接修改的内部对象；状态生命周期独立于网络运行时。
- [ ] 介质规格与允许类型来自明确配置；计数、容量、类型数、负数量、非法槽号和大整数都有边界校验。
- [ ] 覆盖空驱动器、合法装卸、非介质物品、槽满、背包满、同 ID 双挂载、特殊元数据与旧槽版本。
- [ ] 覆盖重复加载、卸载重载、拆线合网、主控消失、同位置替换、旧许可和旧回调；重复事件不产生第二份库存。
- [ ] 装卸后 `disk_count` 与权威槽位一致，负载刷新实际发生；满载／失去准入不会继续用旧许可写入。
- [ ] 两玩家并发、重复请求、部分失败、写入失败与结果不确定均能说明资源当前归属；恢复不盲目重发。
- [ ] 读写额度由所有调用者共享；挂卸和换网不会重新获得整桶额度；模拟与拒绝不消耗库存或额度。
- [ ] 恢复保留未知版本与缺失资源；真实后端与稳定介质 ID 已验证，或明确标记为未接入。
- [ ] 能力对象、ServiceFactory、主控命令、生命周期与保存入口有集成测试，不只单测新类。
- [ ] 若交付 UI，验证会话权限、旧响应隔离、关闭清理、离线诊断、状态栏／弹窗实际绑定与移动端交互。

运行 `python -B endernet_tests/run_tests.py` 完整门禁。若改 definitions 或生成资源，运行生成脚本及 `python -B scripts/gen_blocks.py --check`。测试应验证资源守恒、独立求和与失败行为，不以复制实现逻辑的断言充数。

游戏验证至少区分：真实装卸与背包核对、区块重载、拆除、保存退出重进、失去网络资格、UI 鼠标／触控；未测项保持空白。交付说明列出修改文件、实际调用链、协议／存档版本、已通过测试、未定规则与剩余缺口，供后续脚本层评审使用。
