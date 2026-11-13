# 存储转储器 UI

2026-11-13：按用户批准的完整实现计划制作并接入；本次授权包含界面和贴图。

## 资源与布局

- 文件：`endernet_rp/ui/endernet/storage/storage_transfer.json`。
- namespace / 原生代理屏幕：`endernet_storage_transfer.main`。
- 原生四槽采用 2×2 布局，索引从左上到右下为 0、1、2、3，继承 `endernet_container_common.container_grid_item`，集合为 `netease_container`。
- 174×200 主面板复用公共背景、标题、关闭按钮、玩家背包、状态栏及详情弹窗；保留 PC、触屏与手柄的原生容器输入。
- 标题使用 `tile.endernet:storage_transfer.name`。界面上部是四槽，下方为三行摘要，再下方是玩家物品栏。

## 绑定与数据

| 控件 | 契约 |
| --- | --- |
| `window/root_panel/bg/main/medium_0`～`medium_3` | 原生真实介质槽，脚本不伪造物品；服务端校验非空身份。 |
| `window/root_panel/bg/main/summary` | `#EnderNetStorageTransfer.summary` 静态字符串绑定，最近一秒单位物/s、EN/t、工作状态、四槽剩余单位物。 |
| `window/root_panel/bg/main/stat_bar` | 公共总体在线状态，点击 `interact_btn` 打开详情。 |
| `window/device_status_popup` | 公共控制器、带宽、能源详情；关闭沿用公共事件绑定。 |

`StorageTransferPanel` 注册为 `is_proxy=True`，继承 `ProxyPanelBase`。服务端 `_sync_status_of` 将摘要加入状态 notes，经既有设备状态订阅同步；绑定只读服务端状态。未确认身份时显示等待提示，关闭由基类释放订阅。

## 操作与边界

放入立即开始自动尝试转储，不设开始按钮。空介质不能新放入，清空后的介质保留原槽；可随时拿走。窗口关闭不停止服务端工作。网络离线与业务空闲分开显示，满盘、等待额度、完成都有摘要提示。物品详情仍由共享介质 Lore 提供。

## 验证

实机原生四槽、在线状态、三行摘要、详情弹窗已验证；截图及离线/实机结果见 [验收记录](../tasks/validation/storage_transfer/README.md)。移动设备触屏和手柄未在对应硬件上验证。
