# coding=utf-8
"""设备状态栏与详情弹窗：纯模型、类体绑定。

这里放引擎无关的纯模型与类体绑定 Mixin；`@Binder.binding` 必须写在注册的
`ToolDeltaScreen` 子类**类体**里（见 `terminal/panel.py` 的长注释），所以本包不提供
「装上就生效」的控件类，只提供它们要用的数据与名字（原 `widgets/` 包的口径）。

本包不导出名字：`bindings` 导入期需要引擎；`model` 的包外消费者都按模块对象导入（`from ..device_status import model as device_status`）。
"""
