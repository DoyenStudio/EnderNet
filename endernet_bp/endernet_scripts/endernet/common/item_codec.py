# coding=utf-8
"""共享物品边界：只依赖 Item 的字段协议，构造器由端侧注入。

不使用 Item.copy/from_dict/marshal，以免浅复制或默认值改变身份。
保留 None/空值；未知引擎字段拒绝接收，不能静默丢弃潜在身份数据。
"""

from .resource_key import ResourceKey, _require_count
from . import settings


ITEM_FIELDS = (
    ("enchant_data", "enchantData"),
    ("mod_enchant_data", "modEnchantData"),
    ("custom_tips", "customTips"),
    ("extra_id", "extraId"),
    ("user_data", "userData"),
    ("durability", "durability"),
)
_ENGINE_FIELDS = set(("newItemName", "newAuxValue", "count", "showInHand"))
_ENGINE_FIELDS.update(engine for field, engine in ITEM_FIELDS)


def _check_fields(data):
    if not isinstance(data, dict):
        raise TypeError("Expected an item dictionary")
    if set(data) - _ENGINE_FIELDS:
        raise ValueError("Unsupported item fields: %r" % (sorted(set(data) - _ENGINE_FIELDS),))


def item_key_from_dict(data):
    """从引擎字段字典提取身份；空槽返回 None，数量不参与身份。"""
    _check_fields(data)
    _require_count(data["count"], "item count")
    if data["count"] == 0 or data["newItemName"] == "minecraft:air":
        return None
    identity = dict((field, data.get(engine)) for field, engine in ITEM_FIELDS)
    return ResourceKey.from_dict({"kind": "item", "name": data["newItemName"],
                                  "aux": data["newAuxValue"], "identity": identity})


def item_key(item):
    """从 ToolDelta Item 或具有相同字段的对象提取身份。"""
    if item is None:
        return None
    # _orig 中未建模的字段可能影响还原，不能绕过字典入口的拒绝规则。
    if settings.DEBUG:
        _check_fields(getattr(item, "_orig", {}))
        _require_count(item.count, "item count")
    if item.count == 0 or item.newItemName == "minecraft:air":
        return None
    identity = dict((field, getattr(item, engine, None)) for field, engine in ITEM_FIELDS)
    return ResourceKey.item(item.newItemName, item.newAuxValue, **identity)


def item_dict_from_key(key, count=1):
    """生成独立的引擎物品描述；默认数量 1 可作为图标样本。

    此函数不代表真实背包写入；调用方仍须按 maxStackSize 拆分。
    """
    if settings.DEBUG and not isinstance(key, ResourceKey):
        raise TypeError("Expected an available item ResourceKey")
    if not key.is_item():
        raise TypeError("Expected an available item ResourceKey")
    if settings.DEBUG:
        _require_count(count, "item count")
    if count <= 0 or key.name == "minecraft:air":
        raise ValueError("Cannot materialize an empty resource")
    raw = key.raw_identity
    data = dict((engine, raw[field]) for field, engine in ITEM_FIELDS)
    data.update(newItemName=key.name, newAuxValue=key.aux, count=count, showInHand=True)
    return data


def item_from_key(key, count, item_factory):
    """端侧传入 Item 构造器，返回独立且满足堆叠上限的真实物品对象。"""
    item = item_factory(**item_dict_from_key(key, count))
    if count > item.GetBasicInfo().maxStackSize:
        raise ValueError("Item count exceeds maxStackSize")
    return item
