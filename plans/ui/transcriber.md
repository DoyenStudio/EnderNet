# 转录器 UI

资源：`endernet_rp/ui/endernet/storage/transcriber.json`，namespace `endernet_transcriber`，原生屏 `endernet_transcriber.main`。

- 方块 `endernet:transcriber` 声明 8 槽原生容器。左 0～3、右 4～7，分别按从左到右、从上到下排列；中间为向右箭头。所有槽位使用 `netease_container` 集合和显式 `collection_index`。
- 复用原生容器格、背包、鼠标/触屏交互以及 `ProxyPanelBase` 身份握手、保活、关闭清理。
- `TranscriberPanel` 在类体用 `Binder.BF_BindString` 绑定 `#EnderNetTranscriber.summary`；服务端设备状态 notes 最后一项提供可执行速度、耗能和业务状态，不接受客户端修改。
- `stat_bar` 和详情弹窗复用设备总体状态绑定。无任务、源空、目标满属于业务空闲；网络离线原因由原有状态模型解释。
- 介质放入只收注册介质，每格一件；拖拽、分配和取出由引擎负责，不复制 Python 槽位库存。取出/破坏前尝试刷新已提交内容对应的 Lore。
- 界面关闭不停止转写；离线仍可管理本地介质。

验证记录见 `../technical_notes/transcriber.md`。离线渲染只用于布局检查，不代替游戏验收。
