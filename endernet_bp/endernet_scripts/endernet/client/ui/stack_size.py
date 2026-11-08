# coding=utf-8
"""一个物品的堆叠上限与 `id_aux`，按资源名缓存。

**「一组是多少」只能有一份定义。** 界面上下两半都要用它：上半屏的终端行
发 `inv_put`（网络 → 背包），下半屏选中的背包槽发 `inv_take`（背包 → 网络）。
两处各写一份的代价不是重复代码，而是**两个方向的天花板会悄悄分家**——
同一个键在「取」的时候按 64 算、在「存」的时候按 1 算，而界面上没有任何
东西看得出来。

缓存挂在模块上而不是实例上：`maxStackSize` 是物品的常量，不随会话变化，
也没有「这个会话的石头堆 64、那个会话堆 16」这回事。按资源名索引，所以
条目数被游戏里的物品种类数封顶。
"""

try:
    INTEGER_TYPES = (int, long)
except NameError:
    INTEGER_TYPES = (int,)

#: 资源名 → `(id_aux, maxStackSize, maxDurability)`。都来自同一次 `GetBasicInfo()`，
#: 而 `id_aux` 在渲染路径上逐格要，每格新建一个 `Item` 只为读两个常量
#: 没有道理。
_CACHE = {}


def reset():
    # type: () -> None
    """清缓存。给测试用——游戏里没有需要清的时刻。"""
    _CACHE.clear()


def basic_info(item_id, aux=0):
    # type: (str, int) -> tuple
    """`(id_aux, maxStackSize, maxDurability)`。查不到时退成 `(0, None, 0)`。

    **不抛**：拿不到堆叠上限是要退化处理的事，不是错误——玩家仍然应该
    能点，只是按最保守的数量走（见 `stack_limit`）。

    **引擎导入在函数体内**，与适配器同一条纪律：本模块因此可以离线导入，
    而测试把 `tooldelta.define.item` 换成替身时也**一定命中**——模块级的
    `from ... import Item` 会把导入那一刻的那个类钉死，后面再换替身就不
    生效了（那种错的表现是「测试改的堆叠上限没起作用」，而它看起来只是
    断言写错了）。
    """
    cache_key = (item_id, aux or 0)
    info = _CACHE.get(cache_key)
    if info is None:
        try:
            from ....tooldelta.define.item import Item
            basic = (Item(item_id, aux) if aux else Item(item_id)).GetBasicInfo()
            info = (basic.id_aux, basic.maxStackSize, getattr(basic, "maxDurability", 0))
        except Exception:
            info = (0, None, 0)
        _CACHE[cache_key] = info
    return info


def id_aux(item_id, aux=0):
    # type: (str, int) -> int
    """物品的数字 id，接集合绑定的 `#item_id_aux`。"""
    value = basic_info(item_id, aux)[0]
    if isinstance(value, bool) or not isinstance(value, INTEGER_TYPES):
        return 0
    return int(value)


def stack_limit(item_id):
    # type: (str) -> int
    """一组是多少。取不到 `maxStackSize` 时退成 **1**，不是「全部可用数」。

    退化的方向是**刻意的**：玩家按的是「一组」，引擎没给出堆叠上限时若
    退成可用数，同一个键就从「拿一组」变成「把这一种全抽干」——正是这个
    约定要避开的那件事。退成 1 最多是少拿，方向安全。
    """
    limit = basic_info(item_id)[1]
    if isinstance(limit, bool) or not isinstance(limit, INTEGER_TYPES) or limit < 1:
        return 1
    return int(limit)


def max_durability(item_id):
    # type: (str) -> int
    """最大耐久；不是工具（或查不到）为 0。与损伤值无关，按资源名查。"""
    value = basic_info(item_id)[2]
    if isinstance(value, bool) or not isinstance(value, INTEGER_TYPES) or value < 0:
        return 0
    return int(value)


def durability(key):
    # type: (object) -> tuple
    """耐久条 `(显示, 总量, 当前)`。满耐久或不是工具时不显示（同原版）。

    当前耐久优先取物品身份里的 `durability`；没有时按「最大耐久 - 附加值」算
    （工具的损伤记在附加值里，见 `item_codec` 的 `_EMPTY_IDENTITY`）。
    """
    if key is None:
        return False, 0, 0
    total = max_durability(key.name)
    if total <= 0:
        return False, 0, 0
    current = key.raw_identity.get("durability")
    if isinstance(current, bool) or not isinstance(current, INTEGER_TYPES):
        current = total - (key.aux or 0)
    current = max(0, min(total, int(current)))
    return current < total, total, current


def display_name(item_id):
    # type: (object) -> object
    try:
        from ....tooldelta.define.item import Item
        name = Item(item_id).GetBasicInfo().itemName
        return name.decode("utf-8") if isinstance(name, bytes) else name
    except (AttributeError, ImportError, ValueError, UnicodeError):
        return item_id
