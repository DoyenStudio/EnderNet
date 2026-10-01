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


#: ModSDK NBT 类型编号；这里只列出当前解包器支持的四种类型。
ENGINE_TAG_BYTE = 1
ENGINE_TAG_INT = 3
ENGINE_TAG_FLOAT = 5
ENGINE_TAG_TEXT = 8

#: 本项目容器往返实测：Byte 兼表布尔与 None；不是通用 NBT 的空值定义。
ENGINE_BYTE_NONE = 2


def unbox_engine_value(value):
    """递归解包本项目载荷使用的 ModSDK NBT 表示，非完整 NBT 解码器。

    依据 mcdk-assistant 教程《NBT数据格式》（检索命令：
    `netease NBT数据格式`，2026-09-28 核对）：物品 userData 与原版方块
    实体数据以 NBT 存取，Python 接口用 `{"__type__": T, "__value__": V}`
    表示类型和值。T 是 NBT 类型编号，不是 Python 类型编号；V 是载荷，
    不一定是标量。教程列出的类型如下：

    | T | NBT 类型 | V / 本项目实测补充 |
    | --- | --- | --- |
    | 1 | Byte，**布尔与 None 共用这一个标签** | 0=False，1=True，2=None |
    | 2 | Short | 短整数 |
    | 3 | Int | 普通整数 |
    | 4 | Int64 | 64 位整数 |
    | 5 | Float | 浮点 |
    | 6 | Double | 双精度浮点 |
    | 7 | ByteArray | 整数列表 |
    | 8 | String | 文本 |
    | 9 | List | 递归转换的列表 |
    | 10 | Compound | 递归转换的复合结构 |
    | 11 | IntArray | 整数列表 |

    教程对 Byte 只写了 True/False；表中的 0/1/2 映射来自 2026-09-25
    本项目方块容器的游戏内实测，保留此兼容约定。不能据此把任意物品的
    Byte 数值 2 都解释成 None（例如 item_lock 的 Byte 值另有业务含义）。
    同次实测中，写入普通值后标量带标签返回，空列表与空字典原样返回；
    这是该路径的观测，不代表 NBT 的 List/Compound 没有类型标签。

    当前仅解包 1/3/5/8，遍历未包装的 dict/list/tuple（tuple 转 list）。
    其他编号的包装原样保留，包括教程已定义但此处未支持的 2/4/6/7/9/10/11；
    不递归处理这些包装的载荷，也不在此抛错，由调用方按业务协议校验。
    解包会丢失 NBT 类型信息，仅用于样板等自有载荷读取，不能用于资源身份
    规范化或任意 NBT 的无损往返。

    包装判别要求字典恰好有这两个键，且 `__type__` 是 int 而非 bool。
    若 `__type__` 本身是字典，外层按普通字典遍历；但已经符合包装形状的
    字典无法与同形业务数据区分。SDK 本就允许用该形状显式指定 NBT 类型，
    不能假定引擎会再次包装这两个字段来自动避免冲突。
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
    # 未支持或未知的标签：保留整个包装，由调用方按业务协议校验。
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


#: 引擎返回、但**不属于身份**的派生字段。2026-09-25 游戏内实测：
#: `GetContainerItem` 对一个**原版箱子**返回 16 个字段，比本模块认识的
#: 10 个多出这 6 个。它们都是引擎自己从名称/附加值算出来的镜像
#: （`itemName`/`auxValue` 就是 `newItemName`/`newAuxValue` 的另一份写法；
#: `itemId`/`isDiggerItem`/`modId`/`modItemId` 是引擎内部的检索与分类字段）。
#:
#: **只放行这 6 个已知派生字段。** 再出现别的未知字段仍然让 `_check_fields`
#: 抛错——那条严格性是「不静默丢弃潜在身份数据」的守卫，不能因为
#: 「引擎字段多」就整体放开。
_DERIVED_FIELDS = (
    "itemId",
    "itemName",
    "auxValue",
    "isDiggerItem",
    "modId",
    "modItemId",
)

#: 参与身份的六个字段（引擎里的名字）。与 `ITEM_FIELDS` 的右侧一一对应；
#: 这里再列一遍是为了让「哪些字段要归一化」一眼可见。
_IDENTITY_FIELDS = (
    "enchantData",
    "modEnchantData",
    "customTips",
    "extraId",
    "userData",
    "durability",
)

#: 归一化允许出现的字段全集：4 个核心 + 6 个身份 + 6 个已知派生。
#: **多一个都不行**——见 `normalize_engine_item` 里的说明。
_KNOWN_FIELDS = frozenset(
    ("newItemName", "newAuxValue", "count", "showInHand")
    + _IDENTITY_FIELDS
    + _DERIVED_FIELDS
)

#: 身份的六个字段在引擎里的「空值」写法。实测：引擎读回来时不是 `None`，
#: 而是 `[]` / `""` / `0`。**必须归一成 `None`**，否则同一件物品
#: 「从网络写进去」与「从箱子读出来」会得到两个不同的 `ResourceKey`
#: （本模块按「空值与缺省是不同身份」处理），守恒核对立刻失效。
_EMPTY_IDENTITY = {
    "enchantData": (None, [], ()),
    "modEnchantData": (None, [], ()),
    "customTips": (None, ""),
    "extraId": (None, ""),
    "userData": (None,),
    # `durability` 的 0 与 None 在引擎里表达不了差别（`tooldelta` 自己的
    # `Item.from_dict` 也默认成 0）。真正的工具损伤记在 `newAuxValue` 里，
    # 那一位已经参与身份，因此这里把 0 归一成 None 是安全的。
    "durability": (None, 0),
}


def normalize_engine_item(data):
    # type: (object) -> object
    """把引擎读回来的物品字典**归一成 `item_codec` 认的形状**。

    两件事，都在**引擎边界**做（本模块其余部分保持严格不变）：

    1. 丢掉 6 个已知派生字段（见 `_DERIVED_FIELDS`）；
    2. 把身份的「空值」从引擎写法（`[]` / `""` / `0`）归一到 `None`。

    空槽（`None` 或 count≤0）原样返回，交给调用方判空。

    **这是实测出来的，不是推断的**：上面两条都来自 2026-09-25 的游戏内读数。

    **为什么在本模块而不是适配器里**：这条边界**服务端与客户端都要走**。
    客户端界面渲染玩家背包时读的是引擎给的那一份，而它必须与服务端算出
    同一个 `ResourceKey`——两处各归一一份的代价是「客户端算的指纹永远
    对不上服务端的」，表现是每次按槽取放都被判成 `SLOT_CHANGED`，而日志里
    只有一条回执。适配器 `containers_engine` 仍然从本模块转出这个名字。
    """
    if not data or is_empty_item(data):
        return data
    # **认不出的字段要炸，不能丢。** 「只保留我认识的」听起来温和，
    # 实际效果是把新出现的身份字段静默抹掉——那正是 `_check_fields`
    # 存在的理由。因此这里只放行**已知**的两类额外字段（引擎派生），
    # 其余一律拒绝。
    unknown = set(data) - _KNOWN_FIELDS
    if unknown:
        raise ValueError(
            "引擎返回了未登记的物品字段 %r；不确定它是否参与身份，"
            "请先实测再决定放行还是保留" % (sorted(unknown),)
        )
    out = {}
    for field in ("newItemName", "newAuxValue", "count", "showInHand"):
        if field in data:
            out[field] = data[field]
    for engine_name in _IDENTITY_FIELDS:
        value = data.get(engine_name)
        if value in _EMPTY_IDENTITY[engine_name]:
            value = None
        out[engine_name] = value
    return out


def engine_item_key(item):
    # type: (object) -> ResourceKey
    """**引擎读回来的** `Item` → `ResourceKey`。按槽取放的身份只能走这条。

    与 `item_key()` 的区别是**必须的一步**，不是风格差异：`item_key` 按
    `Item` 的**属性**取身份，而 `tooldelta` 的 `Item.from_dict` 把缺席的
    `durability` 补成 `0`（`define/item.py`：`data.get("durability", 0)`），
    永远给不出 `None`。服务端那条路却把 `0` 归一成 `None`，而
    `ResourceKey` 把「缺省」与「值为 0」当成**两个身份**——于是客户端算的
    指纹恒不等于服务端的，带槽请求全部被判 `SLOT_CHANGED`。

    因此这里回到**引擎给的原始字典**（`_orig`，`Item.from_dict` 原样存下）
    再走 `normalize_engine_item`，与服务端同一个入口。**不用 `marshal()`**：
    本模块开头就写明不使用它——它把 `_orig` 与属性混在一起，物品被就地
    改过之后算出来的就不是引擎说过的那个身份了。

    `_orig` 为空说明这个 `Item` 是手工构造的，那种情况请用 `item_key()`。
    """
    raw = getattr(item, "_orig", None)
    if not raw:
        raise ValueError(
            "该物品没有原始字典，不是从引擎读回来的；"
            "手工构造的物品请用 item_key()"
        )
    return item_key_from_dict(normalize_engine_item(dict(raw)))


def item_key(item):
    """从 ToolDelta Item 或具有相同字段的对象提取身份。

    **只适用于手工构造的物品**（`item_from_key` 造出来的、或端侧直接
    `Item(...)` 出来的）。**引擎读回来的一律用 `engine_item_key()`**：
    这个函数的属性路径给不出引擎的空值写法，见那里的说明。
    """
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
