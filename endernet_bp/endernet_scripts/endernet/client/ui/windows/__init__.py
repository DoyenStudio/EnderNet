# coding=utf-8
"""辅助窗口：物品/流体名单、物品/流体选择器、数量窗，及其身份常量与类体绑定。

窗口的类体绑定 Mixin（`list_bindings`）由宿主屏混入——`@Binder.binding` 必须写在
注册的 `ToolDeltaScreen` 子类类体里（原 `widgets/` 包的口径）。

门面：只导出包外生产代码在用的名字（见 `__all__`）；包内模块之间写模块路径，不经本门面（`test_package_layout` 守着）。
"""

from .names import ITEM_LIST_WINDOW_ATTRIBUTE

__all__ = [
    "ITEM_LIST_WINDOW_ATTRIBUTE",
]
