# coding=utf-8
"""样板提示的资源显示名查询。"""

from ...common.resource_key import KIND_ITEM


def resource_display_name(key, item_factory=None):
    # type: (object, object) -> str
    if key.kind != KIND_ITEM:
        return key.name
    if item_factory is None:
        try:
            from ....tooldelta.define.item import Item
        except ImportError:
            # 离线领域测试以 endernet 为顶层包，不加载 ToolDelta/引擎。
            return key.name
        item_factory = Item
    return item_factory(key.name, key.aux).GetBasicInfo().itemName
