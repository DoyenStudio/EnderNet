# coding=utf-8
"""资源身份与整数单位尺度。

对应任务 01-02。本模块是仓储、转移、样板与回流共用的资源键定义，
不依赖任何引擎接口，可脱离游戏单独验证。

三个必须成立的性质：

1. **数量不属于身份。** `ResourceKey` 只描述「是什么」，不描述「有多少」。
   两个同种物品的键恒等，无论各自身边有多少个。
2. **身份覆盖会影响还原/堆叠的内容。** 名称、附加值、附魔、模组附魔、耐久、
   自定义数据、`extraId`、`customTips` 全部参与比较；只拿物品 ID 作键会把
   附魔剑和普通剑合并成同一条库存记录。
3. **空值差异保留。** `None`（字段不存在）与 `[]`/`{}`（字段存在但为空）
   暂按不同身份处理。引擎默认值归一化必须经实测后在转换边界统一处理。

单位尺度（架构规划 6.2）：以 `1/50 单位物` 为整数记账尺度。
1 物品 = 50 微单位物，1 mB 流体 = 1 微单位物，介质容量按 `单位物 × 50` 记录。
这样 25 mB 不会被截断为 0，也不会引入浮点累计误差。
"""

from copy import deepcopy

from . import medium_item
from . import settings

try:  # pragma: no cover - 仅在 Python 2 下成立
    _TEXT_TYPES = (str, unicode)
    _UNICODE = unicode
except NameError:  # pragma: no cover - 仅在 Python 3 下成立
    _TEXT_TYPES = (str,)
    _UNICODE = str

try:  # pragma: no cover - 仅在 Python 2 下成立
    _INT_TYPES = (int, long)
except NameError:  # pragma: no cover - 仅在 Python 3 下成立
    _INT_TYPES = (int,)


# -- 种类与单位尺度 --------------------------------------------------------

KIND_ITEM = "item"
KIND_FLUID = "fluid"
RESOURCE_SCHEMA_VERSION = 1


#: 1 单位物 = 50 微单位物
MICRO_UNITS_PER_UNIT = 50
#: 1 物品 = 1 单位物
ITEM_MICRO_UNITS = MICRO_UNITS_PER_UNIT
#: 1 mB 流体 = 1/50 单位物
FLUID_MICRO_UNITS_PER_MB = 1

#: 参与物品身份的字段，顺序固定。新增字段时一并更新迁移规则（任务 12）。
ITEM_IDENTITY_FIELDS = (
    "enchant_data",
    "mod_enchant_data",
    "custom_tips",
    "extra_id",
    "user_data",
    "durability",
)


def item_micro_units(count):
    # type: (int) -> int
    """物品个数换算成微单位物。"""
    _require_count(count, "物品个数")
    return count * ITEM_MICRO_UNITS


def fluid_micro_units(millibuckets):
    # type: (int) -> int
    """流体 mB 换算成微单位物。"""
    _require_count(millibuckets, "流体 mB")
    return millibuckets * FLUID_MICRO_UNITS_PER_MB


def capacity_micro_units(units):
    # type: (int) -> int
    """介质容量从「单位物」换算成微单位物。"""
    _require_count(units, "容量单位物")
    return units * MICRO_UNITS_PER_UNIT


def _require_count(value, label):
    # type: (object, str) -> None
    if isinstance(value, bool) or not isinstance(value, _INT_TYPES):
        raise TypeError("%s 必须是整数，得到 %r" % (label, value))
    if value < 0:
        raise ValueError("%s 不能为负，得到 %r" % (label, value))


# -- 规范化 ----------------------------------------------------------------


def _float_text(value):
    # type: (float) -> str
    """跨 py2/py3 稳定的浮点文本。repr() 在两版之间可能不同，%.17g 不会。"""
    return "%.17g" % (value,)


def canonical_tree(value):
    # type: (object) -> tuple
    """把任意 NBT 风格的值转成可比较、可哈希的规范元组树。

    - 字典按键排序，因此 `{"a":1,"b":2}` 与 `{"b":2,"a":1}` 同身份。
    - 列表保持顺序，顺序不同即不同身份。
    - 类型标签保留 int 与 float 的差异，避免 NBT 还原时改变类型。
    """
    if value is None:
        return ("n", "")
    if isinstance(value, bool):
        return ("b", "1" if value else "0")
    if isinstance(value, _INT_TYPES):
        return ("i", str(value))
    if isinstance(value, float):
        return ("f", _float_text(value))
    if isinstance(value, _TEXT_TYPES):
        return ("s", value if isinstance(value, _UNICODE) else value.decode("utf-8"))
    if isinstance(value, (list, tuple)):
        return ("l",) + tuple(canonical_tree(v) for v in value)
    if isinstance(value, dict):
        pairs = []
        for key in value:
            pairs.append((canonical_tree(key), canonical_tree(value[key])))
        # UTF-8 order is identical on Python 2 narrow/wide and Python 3 Unicode.
        pairs.sort(key=lambda pair: (
            pair[0][0],
            (pair[0][1] if pair[0][0] == "s" else tree_text(pair[0])).encode("utf-8"),
        ))
        return ("d",) + tuple(pairs)
    raise TypeError("自定义数据中出现无法规范化的类型: %r" % (type(value),))


def tree_text(node):
    # type: (tuple) -> str
    """把规范元组树编成稳定的文本指纹。

    长度前缀保证拼接无歧义：`("s","ab")` 与 `("s","a")+("b",)` 不会撞车。
    输出只由结构决定，因此在 py2 与 py3 下一致，可安全写入存档。
    """
    parts = []
    for part in node:
        encoded = tree_text(part) if isinstance(part, tuple) else part
        parts.append(u"%d:%s" % (len(encoded.encode("utf-8")), encoded))
    return u"(" + u"".join(parts) + u")"


def _optional(node):
    # type: (object) -> tuple
    """区分「字段不存在」与「字段为空」。"""
    return ("none", "") if node is None else ("some", node)


def _enchant_tree(pairs, mod=False, validate=False):
    """附魔是集合语义：顺序不影响身份，排序后比较。"""
    if validate and not isinstance(pairs, (list, tuple)):
        raise TypeError("Enchantments must be a sequence of pairs")
    out = []
    for pair in pairs:
        if validate:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError("附魔条目应为 (id, level)，得到 %r" % (pair,))
            if mod:
                if not isinstance(pair[0], _TEXT_TYPES) or not pair[0]:
                    raise ValueError("Mod enchantment ID must be nonempty text")
            else:
                _require_count(pair[0], "enchantment ID")
            _require_count(pair[1], "enchantment level")
        out.append((canonical_tree(pair[0]), canonical_tree(pair[1])))
    out.sort(key=lambda pair: (pair[0][1].encode("utf-8"), pair[1][1]))
    return tuple(out)


def _item_identity_tree(
    enchant_data,
    mod_enchant_data,
    custom_tips,
    extra_id,
    user_data,
    durability,
    validate=False,
):
    """六个字段固定顺序，各自区分 None 与空值。"""
    return (
        ("enchant_data", _optional(None if enchant_data is None else _enchant_tree(enchant_data, validate=validate))),
        (
            "mod_enchant_data",
            _optional(
                None if mod_enchant_data is None else _enchant_tree(mod_enchant_data, mod=True, validate=validate)
            ),
        ),
        ("custom_tips", _optional(None if custom_tips is None else canonical_tree(custom_tips))),
        ("extra_id", _optional(None if extra_id is None else canonical_tree(extra_id))),
        # 身份投影落在这一处，因此**两条构造路径（`item()` 与 `from_dict()`）
        # 自动都走到它**。介质物品的动态容量 Lore 只在这里被剔除；
        # `_raw`（以及 `raw_identity` / `to_dict`）拿到的仍是完整载荷。
        (
            "user_data",
            _optional(
                None
                if user_data is None
                else canonical_tree(medium_item.project_identity_user_data(user_data))
            ),
        ),
        ("durability", _optional(None if durability is None else canonical_tree(durability))),
    )


# -- 资源键 ----------------------------------------------------------------


class ResourceKey(object):
    """一种资源（物品或流体）的身份。不含数量。

    请用 `ResourceKey.item(...)` / `ResourceKey.fluid(...)` 构造，
    不要直接调用 `__init__`。作为字典键使用期间，调用方不得修改身份字段。
    构造及导出时复制元数据；不通过属性拦截强制冻结对象。
    """

    __slots__ = ("_kind", "_name", "_aux", "_identity", "_raw")

    def __init__(self, kind, name, aux, identity, raw_identity):
        self._kind = kind
        self._name = name
        self._aux = aux
        self._identity = identity
        self._raw = raw_identity

    @classmethod
    def _create(cls, kind, name, aux, raw, validate=False):
        # 内部工厂只在 DEBUG 时检查；外部解码显式要求校验。
        validate = validate or settings.DEBUG
        if validate:
            if kind not in (KIND_ITEM, KIND_FLUID):
                raise ValueError("未知资源种类: %r" % (kind,))
            if not isinstance(name, _TEXT_TYPES) or not name:
                raise ValueError("资源名称必须是非空字符串，得到 %r" % (name,))
            _require_count(aux, "aux")
            if kind == KIND_FLUID and aux != 0:
                raise ValueError("Fluid aux must be zero")
            if not isinstance(raw, dict):
                raise TypeError("Resource identity must be an object")
        raw = deepcopy(raw)
        fields = ITEM_IDENTITY_FIELDS if kind == KIND_ITEM else ("user_data",)
        if validate and set(raw) - set(fields):
            raise ValueError("Unknown resource identity fields")
        raw = dict((field, raw.get(field)) for field in fields)
        if validate and raw.get("user_data") is not None and not isinstance(raw["user_data"], dict):
            raise TypeError("user_data must be an object or None")
        if kind == KIND_ITEM:
            if validate:
                for field in ("custom_tips", "extra_id"):
                    if raw[field] is not None and not isinstance(raw[field], _TEXT_TYPES):
                        raise TypeError("%s must be text or None" % field)
                if raw["durability"] is not None:
                    _require_count(raw["durability"], "durability")
            derived = _item_identity_tree(validate=validate, **raw)
        else:
            derived = (("user_data", _optional(
                None if raw["user_data"] is None else canonical_tree(raw["user_data"])
            )),)
        name = name if isinstance(name, _UNICODE) else name.decode("utf-8")
        return cls(kind, name, aux, derived, raw)

    # -- 构造 ----------------------------------------------------------

    @classmethod
    def item(
        cls,
        name,
        aux=0,
        enchant_data=None,
        mod_enchant_data=None,
        custom_tips=None,
        extra_id=None,
        user_data=None,
        durability=None,
    ):
        # type: (str, int, object, object, object, object, object, object) -> ResourceKey
        """构造物品键。参数与 `tooldelta.define.item.Item` 的字段对应。"""
        raw = {
            "enchant_data": enchant_data,
            "mod_enchant_data": mod_enchant_data,
            "custom_tips": custom_tips,
            "extra_id": extra_id,
            "user_data": user_data,
            "durability": durability,
        }
        return cls._create(KIND_ITEM, name, aux, raw)

    @classmethod
    def fluid(cls, name, user_data=None):
        # type: (str, object) -> ResourceKey
        """构造流体键。流体没有附加值；自定义数据仍参与身份。"""
        return cls._create(KIND_FLUID, name, 0, {"user_data": user_data})

    # -- 查询 ----------------------------------------------------------

    @property
    def kind(self):
        return self._kind

    @property
    def name(self):
        return self._name

    @property
    def aux(self):
        return self._aux

    @property
    def identity(self):
        """规范元组树，只读。"""
        return self._identity

    @property
    def raw_identity(self):
        """构造时元数据的独立副本，用于无损还原。"""
        return deepcopy(self._raw)

    def is_item(self):
        return self._kind == KIND_ITEM

    def is_fluid(self):
        return self._kind == KIND_FLUID

    def micro_units(self, count):
        # type: (int) -> int
        """把该资源的 count 个/mB 换算成微单位物。"""
        if self._kind == KIND_FLUID:
            return fluid_micro_units(count)
        return item_micro_units(count)

    # -- 身份 ----------------------------------------------------------

    def _identity_tuple(self):
        return (self._kind, self._name, self._aux, self._identity)

    def __eq__(self, other):
        if not isinstance(other, ResourceKey):
            return NotImplemented
        return self._identity_tuple() == other._identity_tuple()

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return hash(self._identity_tuple())

    def __repr__(self):
        text = u"ResourceKey(%s)" % (self.canonical(),)
        return text if _UNICODE is str else text.encode("utf-8")

    def canonical(self):
        # type: () -> str
        """稳定文本指纹，跨 py2/py3 一致，可用于存档与去重。"""
        return u"%s|%s|%d|%s" % (
            self._kind,
            self._name,
            self._aux,
            tree_text(self._identity),
        )

    # -- 序列化 --------------------------------------------------------

    def to_dict(self):
        # type: () -> dict
        """无损写出。`from_dict` 应当还原出相等的键。"""
        return {
            "schema_version": RESOURCE_SCHEMA_VERSION,
            "kind": self._kind,
            "name": self._name,
            "aux": self._aux,
            "identity": self.raw_identity,
        }

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> ResourceKey
        """从 `to_dict()` 的输出还原。"""
        if not isinstance(data, dict):
            raise TypeError("资源键反序列化需要 dict，得到 %r" % (type(data),))
        if set(data) - set(("schema_version", "kind", "name", "aux", "identity")):
            raise ValueError("Unknown resource key fields")
        version = data.get("schema_version", 1)  # 兼容首版无版本结构。
        _require_count(version, "resource schema version")
        if version != RESOURCE_SCHEMA_VERSION:
            raise ValueError("Unsupported resource schema version: %s" % version)
        kind = data.get("kind")
        name = data.get("name")
        aux = data.get("aux", 0)
        identity = data.get("identity", {})
        if not isinstance(identity, dict):
            raise TypeError("Resource identity must be an object")
        return cls._create(kind, name, aux, identity, validate=True)
