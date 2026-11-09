# coding=utf-8
"""下单预览：窗口控制器、纯模型、类体绑定。

纯模型不认识引擎、不发命令；类体绑定 Mixin 由宿主屏混入——`@Binder.binding`
必须写在注册的 `ToolDeltaScreen` 子类类体里（原 `widgets/` 包的口径）。

本包不导出名字：`bindings` 导入期需要引擎；`window` 的包外消费者按模块对象导入，`model` 只有包内消费者。
"""
