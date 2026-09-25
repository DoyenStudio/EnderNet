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


#: 引擎在 `userData` 里给标量加的类型标签。**实测值**（2026-09-25，游戏内）。
ENGINE_TAG_BYTE = 1
ENGINE_TAG_INT = 3
ENGINE_TAG_FLOAT = 5
ENGINE_TAG_TEXT = 8

#: Byte 这一个标签**兼表布尔与 None**，靠值区分。
ENGINE_BYTE_NONE = 2


def unbox_engine_value(value):
    """把引擎 `userData` 里带类型标签的值解回普通值。**递归。**

    2026-09-25 游戏内实测：往方块容器写 `userData` 时给的是普通值，
    读回来**每个标量都被包成** `{"__type__": T, "__value__": V}`：

    | T | 含义 | 值 |
    | --- | --- | --- |
    | 1 | Byte，**布尔与 None 共用这一个标签** | 0=False，1=True，2=None |
    | 3 | Int | 普通整数 |
    | 5 | Float | 浮点 |
    | 8 | String | 文本 |

    空列表与空字典**不加标签**，原样回来。

    **判别要求 `__type__` 是普通整数。** 一份内容恰好长成
    `{"__type__": 3, "__value__": 7}` 的 `userData`（别的模组的数据）
    被引擎包过之后是 `{"__type__": {"__type__": 3, ...}, "__value__": {...}}`
    ——外层字典的 `__type__` 是字典而不是整数，因此不会被误当标签解掉。

    认不出的标签**原样返回**：那说明引擎换了表示法，把整棵树交回给调用方
    去报「读不出来」，好过在这里猜一个值。
    """
    if isinstance(value, dict):
        if set(value) == set(("__type__", "__value__")):
            tag = value["__type__"]
            if isinstance(tag, int) and not isinstance(tag, bool):
                return _unbox_scalar(tag, value["__value__"])
        return dict((key, unbox_engine_value(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return [unbox_engine_value(item) for item in value]
    return value


def _unbox_scalar(tag, raw):
    # type: (int, object) -> object
    if tag == ENGINE_TAG_BYTE:
        if raw == ENGINE_BYTE_NONE:
            return None
        return bool(raw)
    if tag == ENGINE_TAG_INT:
        return int(raw)
    if tag == ENGINE_TAG_FLOAT:
        return float(raw)
    if tag == ENGINE_TAG_TEXT:
        return raw
    # 认不出的标签：把原样交回去，让上层报「读不出来」。
    return {"__type__": tag, "__value__": raw}


def is_empty_item(data):
    """空槽判定：`None`、缺 `count`、数量 ≤ 0、或就是空气。

    **放在这里而不是各层自己写**：这条判定同时被引擎适配器
    （`adapters/containers_engine.py` 的读槽路径）与领域层（`crafting/store.py`
    读样板槽）需要，而两处各写一份的代价是「某一处忘了判空气」——
    那种错的表现是空槽被当成一件叫 `minecraft:air` 的物品读出来，
    然后在下游某个无关的地方炸掉。`item_key_from_dict` 是严格的、
    遇到空槽会抛错，所以判空必须在它之前，且只有一个定义。
    """
    if not data:
        return True
    if data.get("count", 0) <= 0:
        return True
    return data.get("newItemName") == "minecraft:air"


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
