# 转录器 UI

资源：`endernet_rp/ui/endernet/storage/transcriber.json`，namespace `endernet_transcriber`，原生屏 `endernet_transcriber.main`。

- 方块 `endernet:transcriber` 声明 8 槽原生容器。左 0～3、右 4～7，分别按从左到右、从上到下排列；中间为向右箭头。槽位继承 `endernet_container_common.indexed_container_slot`，用 `$container_slot_index` 指定索引；模板由 `stack_panel` 提供 `netease_container` 集合上下文，内部 `slot` 用 `collection_index` 绑定真实槽位。普通 `panel` 不支持 `collection_name`，其直接子控件也不能使用 `collection_index`。
- 复用原生容器格、背包、鼠标/触屏交互以及 `ProxyPanelBase` 身份握手、保活、关闭清理。
- `TranscriberPanel` 在类体用 `Binder.BF_BindString` 绑定 `#EnderNetTranscriber.summary`；服务端设备状态 notes 最后一项提供可执行速度、耗能和业务状态，不接受客户端修改。
- `stat_bar` 和详情弹窗复用设备总体状态绑定。无任务、源空、目标满属于业务空闲；网络离线原因由原有状态模型解释。
- 介质放入只收注册介质，每格一件；拖拽、分配和取出由引擎负责，不复制 Python 槽位库存。取出/破坏前尝试刷新已提交内容对应的 Lore。
- 界面关闭不停止转写；离线仍可管理本地介质。

验证记录见 `../technical_notes/transcriber.md`。离线渲染只用于布局检查，不代替游戏验收。

2026-11-13 集合上下文修复：八槽索引唯一性及公共槽位模板上下文纳入回归，完整离线门禁通过 4881 项测试和 Python 2.7 兼容检查。同一公共模板已在存储转储器实机热重载中确认无上述未知属性报错；本轮未单独打开转录器复测。
