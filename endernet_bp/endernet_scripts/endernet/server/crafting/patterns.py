# coding=utf-8
"""配方结构与样板编解码（任务 07-01 / 07-02）。

**本模块是纯数据与纯函数**：不持有状态、不 import 引擎、不认识任何设备。
「哪台设备上有哪些样板、现在可不可用」在 `store.py`；执行侧在 `execution.py`。
这条分工直接落在架构规划的原文上：

    架构规划 §5.2：`PatternDefinition`：输入、输出和配方模式；
    **配方定义与当前可用的提供者分离**。

## 一条样板不记录外部机器类型

    概念页：单个样板记录一个合成方式，告诉网络系统：合成一个物品需要的原料，
    **不会记录合成该物品需要什么设备**。

设备只由「这块样板所在的那台接口接了什么」决定。因此 `PatternDefinition`
里**没有**机器种类字段——加一个就等于把一条已经写明的规则改掉，而且会把
「换一台机器就得重做样板」这种负担凭空塞给玩家。

## 必须能表达的东西

`plans/concepts.md` 给了四个例子，它们就是本模块表达力的验收清单：

    系统预设-工作台   1 个铁块   ← 9 个铁锭
    系统预设-熔炉     1 个铁锭   ← 1 个粗铁        （默认熔炉有无限的燃料供应）
    自定义            8 份烤鱼   ← 8 份生鱼 + 1 份木炭
    自定义            1 个粘土块 ← 1 块尘土 + 1000mB 水

四者分别压住「多产物」「流体」「有限/无限燃料」「有序工作台摆位」。
`tests/test_patterns.py` 直接拿这四条当用例。

## 身份与编码

样板是**自包含物品**：内容写在物品自己的 `userData` 里，读容器即可解出，
不需要任何「物品 ↔ 世界记录」的对应。这是 07 不卡在 12-02 上的原因——
介质那一条卡住，正是因为它的内容是世界侧权威、物品只是引子。

**内容指纹（`digest()`）就是「样板版本」。** 它由内容派生，因此「编辑之后
旧规划必须复核」是结构性的，不靠某处记得递增一个计数器。指纹算法直接复用
`common/resource_key.py` 的 `canonical_tree` + `tree_text`——那套设施已经
承担了「跨 py2 窄/宽 Unicode 与 py3 稳定」的责任，另造一套只会多一处要验。

## 校验为什么抛异常而不是返回错误列表

编辑样板是**提交式**的：一份结构非法的配方根本构造不出来，因此构造器直接
抛 `PatternValidationError`（带 `code`，界面按它显示具体哪一条不合法）。
**读外部数据时同一个异常由调用方捕获**——`store.py` 逐槽捕获并把原因记进
`invalid_slots`，绝不因为一块坏样板让整帧或整台设备失效，也绝不把它读成空值
再覆盖（与 12 的「原样保留」同一条纪律）。
"""

try:  # pragma: no cover - 仅在 Python 2 下成立
    _TEXT_TYPES = (str, unicode)
    _UNICODE = unicode
    _UNICHAR = unichr
except NameError:  # pragma: no cover - 仅在 Python 3 下成立
    _TEXT_TYPES = (str,)
    _UNICODE = str
    _UNICHAR = chr

from ...common.definitions import (
    PATTERN_MODE_CRAFTING,
    PATTERN_MODE_FACTORY,
    PATTERN_MODES,
    devices_for_mode,
)
from ...common.resource_key import (
    ResourceKey,
    _require_count,
    canonical_tree,
    tree_text,
)

# -- 编码格式 --------------------------------------------------------------

#: 当前写出的样板编码格式版本。
#:
#: 与 `ResourceKey` 的 `schema_version` 同一条纪律：**每份权威数据带自己的
#: 版本**。它进物品的 `userData`，因此会跟着存档走。
PATTERN_FORMAT_VERSION = 1

#: 低于这个格式版本的样板**可读但不可派发**（07-02：「旧版本不可派发」）。
#:
#: 目前只有 v1，因此这条路径在真实数据上走不到——它由测试用合成版本号覆盖。
#: 之所以现在就写出来而不是等有了 v2 再说：**格式升级的那一次必须能拒绝
#: 旧数据**，而那时再补这条判断，已经发出去的旧样板早就被派发过一轮了。
PATTERN_MIN_DISPATCHABLE_FORMAT = 1

#: 编码写在物品 `userData` 下的哪个键。
#:
#: 带命名空间前缀是必须的：`userData` 是**所有模组共用**的一块存储，
#: 本模组只拥有自己这个键，读写都不得碰别人的（见 `write_pattern`）。
PATTERN_DATA_KEY = "endernet:pattern"

#: 模式词汇。与 `TemplateSpec.mode` 是同一套字符串——物品类型就是模式。
MODE_CRAFTING = PATTERN_MODE_CRAFTING
MODE_FACTORY = PATTERN_MODE_FACTORY
MODES = PATTERN_MODES

#: 合成样板的有序摆位是 3×3 工作台。
#:
#: 这是**引擎事实**（原版工作台输入格就是 9 个），不是本模组待定的玩法。
#: 有序槽位的下标与工作台输入格一一对应。
GRID_SLOT_COUNT = 9


# -- 错误词汇 --------------------------------------------------------------


class PatternError(object):
    """样板不合法或读不出来的原因码。

    用途与 `common/protocol.py` 的 `Reason` 不同：那些是**命令**的拒绝原因，
    这些是**一份样板自身**的问题，界面要按它指到具体哪一行。两者在
    `execution.py` 里相会——那里把这里的码翻译成命令原因码。
    """

    #: 数量为零或负数。07-01 点名要拒绝的两条之一。
    NON_POSITIVE_COUNT = "non_positive_count"
    #: 数量根本不是整数（含布尔：`True` 是 `int` 的子类，但它不是数量）。
    INVALID_COUNT = "invalid_count"
    #: 资源键不是 `ResourceKey`，或描述解不出来。
    INVALID_RESOURCE = "invalid_resource"
    #: 一个产物都没有。07-01 点名要拒绝的两条之一。
    #:
    #: 「一个输入都没有」也走这一族（`EMPTY_INPUTS`）：它不是文档点名的两条，
    #: 但零输入的样板会让网络**凭空生成物品**，与全局约束 3
    #: （物品守恒）和 14 验收的「不凭空生成物品」直接冲突。
    EMPTY_OUTPUTS = "empty_outputs"
    EMPTY_INPUTS = "empty_inputs"
    #: 模式不是 `crafting` / `factory`。
    UNKNOWN_MODE = "unknown_mode"
    #: 有序与无序混在一份样板里（一部分输入带槽位、一部分不带）。
    ORDERED_MIX = "ordered_mix"
    #: 两个输入占了同一个槽位。
    DUPLICATE_SLOT = "duplicate_slot"
    #: 槽位越界或不是整数。
    SLOT_OUT_OF_RANGE = "slot_out_of_range"
    #: 这个位置不该有槽位（工厂样板没有有序摆位；流体不进工作台格子；
    #: 产物与返还物不摆进工作台格子）。
    SLOT_NOT_ALLOWED = "slot_not_allowed"
    #: 这个位置不该有 `unlimited` 标记。它只对**输入**有意义（「这一条不向
    #: 网络要」）；放在产物或返还物上既读不出含义，又会静悄悄进指纹——
    #: 同一份配方因此有两个「版本」，而肉眼看不出差别。
    FLAG_NOT_ALLOWED = "flag_not_allowed"
    #: 样板**内容合法**，但它这种模式不该放在这台设备上（合成样板插进了
    #: 样板接口，或反过来）。与 `MALFORMED` 分开：内容一个字都没错，
    #: 错的只是位置，换一台设备就能用。
    KIND_MISMATCH = "kind_mismatch"
    #: 结构与预期不符：缺字段、多出未知字段、类型不对。
    MALFORMED = "malformed"
    #: 物品的 `userData` 里根本没有本模组的键——这不是「坏样板」，是「不是样板」。
    NOT_A_PATTERN = "not_a_pattern"
    #: 格式版本比本版本**更新**。按「未知的更新版本原样保留」处理：
    #: 读不懂就不读，**不当作空样板覆盖**（与任务 12 的纪律一致）。
    FORMAT_TOO_NEW = "format_too_new"


class PatternValidationError(ValueError):
    """样板不合法或读不出来。`code` 取自 `PatternError`。

    继承 `ValueError`：与 `ResourceKey.from_dict` / `PortConfig` 一样，
    「外部数据不合法」在本项目里属值错误。同时它能被
    `storage/service.py::handle` 那圈 `except (KeyError, TypeError, ValueError)`
    兜住，因此一条坏样板既不会变成假成功，也不会变成崩帧。
    """

    def __init__(self, code, detail=""):
        # type: (str, str) -> None
        ValueError.__init__(self, detail or code)
        self.code = code
        self.detail = detail or code

    def to_dict(self):
        # type: () -> dict
        return {"code": self.code, "detail": self.detail}

    def __repr__(self):
        return "PatternValidationError(%s, %s)" % (self.code, self.detail)


def _fail(code, detail):
    # type: (str, str) -> PatternValidationError
    """构造一个待抛出的异常。

    **返回而不是就地 `raise`**：调用处统一写 `raise _fail(...)`，
    静态读代码时「这里一定会中断」是显式的。写成「`_fail` 内部抛、
    调用处之后的代码不可达」的话，一个把 `_fail` 改成记录日志的人
    就会让后面那些「已经假定校验通过」的语句拿到未绑定的局部变量。
    """
    return PatternValidationError(code, detail)


def _positive_count(value, label):
    # type: (object, str) -> int
    """校验一个正整数数量。

    **复用了 `resource_key._require_count`** 的整数判定而不是重写一遍：
    那一份已经处理了「布尔不是数量」「py2 的 long」这两件事，各写一份
    迟早会分叉成「某个入口接受 `True` 当 1」。
    """
    try:
        _require_count(value, label)
    except TypeError:
        raise _fail(PatternError.INVALID_COUNT, "%s 必须是整数，得到 %r" % (label, value))
    except ValueError:
        raise _fail(PatternError.NON_POSITIVE_COUNT, "%s 不能为负，得到 %r" % (label, value))
    if value <= 0:
        raise _fail(
            PatternError.NON_POSITIVE_COUNT,
            "%s 必须为正，得到 %r（零数量的原料等于没写，零数量的产物是空配方）"
            % (label, value),
        )
    return int(value)


def _grid_slot(value):
    # type: (object) -> int
    """校验一个工作台格子下标。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(PatternError.SLOT_OUT_OF_RANGE, "槽位必须是整数，得到 %r" % (value,))
    if value < 0 or value >= GRID_SLOT_COUNT:
        raise _fail(
            PatternError.SLOT_OUT_OF_RANGE,
            "槽位 %d 越界；工作台摆位是 0..%d（共 %d 格）"
            % (value, GRID_SLOT_COUNT - 1, GRID_SLOT_COUNT),
        )
    return int(value)


# -- 一条输入/输出 ---------------------------------------------------------


class PatternStack(object):
    """样板里的一条资源及数量。

    - `slot`：有序摆位的格子下标（0..8）；`None` 表示**无序**。
      只有合成样板可以有序（工厂样板没有工作台格子），液体也不能占格子。
    - `unlimited`：这条输入被标记为「不限量/不消耗」，对应
      `plans/concepts.md` 里熔炉预设的「默认熔炉有无限的燃料供应」。

      **标记的含义已于 2026-09-30 由 R09 定案**：这条输入**由外部自理**，
      网络不为它备料。放在数据里、判定放在策略里，是为了让已存档的样板不因
      玩法收口而被重写（`PatternPolicy.supplies()` 是唯一读它的地方）。
    """

    __slots__ = ("key", "count", "slot", "unlimited")

    def __init__(self, key, count, slot=None, unlimited=False):
        # type: (ResourceKey, int, object, bool) -> None
        if not isinstance(key, ResourceKey):
            raise _fail(
                PatternError.INVALID_RESOURCE,
                "样板条目需要 ResourceKey，得到 %r" % (type(key),),
            )
        self.key = key
        self.count = _positive_count(count, "样板条目数量")
        if slot is None:
            self.slot = None
        else:
            if key.is_fluid():
                # 流体不进工作台格子。允许的话会造出「1000mB 水占第 3 格」
                # 这种谁也执行不了、但结构上「合法」的样板。
                raise _fail(
                    PatternError.SLOT_NOT_ALLOWED,
                    u"流体不能占有序槽位（%s）" % (key.name,),
                )
            self.slot = _grid_slot(slot)
        self.unlimited = bool(unlimited)

    def is_fluid(self):
        # type: () -> bool
        return self.key.is_fluid()

    def to_dict(self):
        # type: () -> dict
        """**规范形状**：缺省值不写出，因此同一份内容只有一种写法。

        这同时让往后的编码升级可以「加字段、旧数据照读」：少写的字段
        按缺省解释，而缺省是唯一无歧义的解释（`slot` 缺失只可能是无序）。
        """
        out = {"key": self.key.to_dict(), "count": self.count}
        if self.slot is not None:
            out["slot"] = self.slot
        if self.unlimited:
            out["unlimited"] = True
        return out

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> PatternStack
        if not isinstance(data, dict):
            raise _fail(PatternError.MALFORMED, "样板条目必须是对象，得到 %r" % (data,))
        unknown = set(data) - set(("key", "count", "slot", "unlimited"))
        if unknown:
            raise _fail(PatternError.MALFORMED, "样板条目出现未知字段 %r" % (sorted(unknown),))
        if "key" not in data or "count" not in data:
            raise _fail(PatternError.MALFORMED, "样板条目缺少 key 或 count")
        try:
            key = ResourceKey.from_dict(data["key"])
        except (TypeError, ValueError) as error:
            raise _fail(
                PatternError.INVALID_RESOURCE,
                "样板条目里的资源描述解不出来：%s" % (error,),
            )
        unlimited = data.get("unlimited", False)
        if not isinstance(unlimited, bool):
            raise _fail(
                PatternError.MALFORMED,
                "unlimited 必须是布尔值，得到 %r" % (unlimited,),
            )
        return cls(key, data["count"], data.get("slot"), unlimited)

    def fingerprint(self):
        # type: () -> tuple
        """比较与指纹用的规范形态。**按身份，不按线上载荷。**

        `to_dict()` 是**无损的线上格式**，它照抄 `raw_identity`；而
        `ResourceKey` 的相等与哈希是按**身份**算的（附魔表是集合语义，
        排序后比较）。两者在「同一件东西写法不同」时会分叉，例如
        `enchant_data=[(1,2),(0,1)]` 与 `[(0,1),(1,2)]` 是**同一个键**
        （`==` 与 `hash` 都相等），但 `raw_identity` 不同。

        拿 `to_dict()` 当指纹的代价：一份计划记下 digest 之后，只要任何
        路径把附魔表换个顺序重新存一次，`check_dispatch` 就报
        `PATTERN_STALE`——**样板根本没被编辑，09 却去重做了一遍计划**。
        因此指纹走 `key.canonical()`（身份派生），线上格式仍走 `to_dict()`。
        """
        return (
            self.key.canonical(),
            self.count,
            self.slot,
            self.unlimited,
        )

    def __eq__(self, other):
        if not isinstance(other, PatternStack):
            return NotImplemented
        return self.fingerprint() == other.fingerprint()

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        """**与 `__eq__` 一致。** 定义了 `__eq__` 却不定义它，在 py2 上
        对象仍是按 `id` 哈希的，于是 `d[a]` 与 `d[b]` 在 `a == b` 为真时
        也会 `KeyError`；py3 上则直接不可哈希。目前没有地方拿它当字典键，
        但那是「还没人踩到」，不是「踩不到」。"""
        return hash(self.fingerprint())

    def __repr__(self):
        marks = ""
        if self.slot is not None:
            marks += " @%d" % (self.slot,)
        if self.unlimited:
            marks += " [不限量]"
        return "PatternStack(%s x%d%s)" % (self.key.name, self.count, marks)


# -- 一份配方 --------------------------------------------------------------


class PatternDefinition(object):
    """一份配方的内容。**不含机器类型，也不含版本**——版本在编码外层。

    三组条目各有明确分工：

    | 组 | 含义 | 谁用 |
    | --- | --- | --- |
    | `inputs` | 合成/加工需要投入什么 | 08 算料、09 备料 |
    | `outputs` | 这次合成**产出**什么 | 08 的产物索引、10 的完成判定 |
    | `returns` | 加工**返还**什么（如空桶） | 10 的完成判定 |

    `returns` 单独一组而不是混进 `outputs`，是因为它们在 10 那边的语义不同：
    「8 份烤鱼 ← 8 份生鱼 + 1 份木炭」只回鱼时不完成，而「鱼到了、空桶没到」
    也不完成（09 文档原文）。混在一起就分不清「副产物」与「返还容器」。

    ## 三组条目是谁写进来的（**三条已定案，完整声明见
    `PatternPolicy.unresolved_report`**）

    - **合成样板**：在**样板终端**配置配方时，**按合成配方自动推导产物**。
      样板一次记全**原料与产物**，此后**任何其他地方都不再推导、不再变更**
      ——下游（08 算料 / 装配仓执行 / 10 判定完成）只读这一份。
    - **工厂样板**：由**用户手动配置**所需原料与**期望的产物**。
    - **返还物**（三组里的第三组）：**一律由样板显式声明**——R09 于
      2026-09-30 定案，模组**不按输入推导空容器**（见 `ReturnRule`）。
      这一条与上面两条是同一条纪律的第三面：样板是完整记录。

    两者落到本类上的**形状完全一样**（就是 `inputs` / `outputs`），因此这里
    不加字段、不设标志：**差别在写入的那一刻，不在数据里**。这也正是
    `PatternDefinition` 只需要「输入/输出/返还物」三组条目的原因。
    """

    __slots__ = ("mode", "inputs", "outputs", "returns")

    def __init__(self, mode, inputs, outputs, returns=()):
        # type: (str, tuple, tuple, tuple) -> None
        if not isinstance(mode, _TEXT_TYPES):
            # 类型先于取值检查：`mode not in MODES` 对**不可哈希**的值
            # （列表、字典）会抛 `TypeError`，而它逃不出 `store._read_one`
            # 那圈捕获——一块伪造的样板能让整帧崩掉（见模块 docstring 的承诺）。
            raise _fail(
                PatternError.UNKNOWN_MODE,
                "样板模式必须是字符串，得到 %r" % (type(mode),),
            )
        if mode not in MODES:
            raise _fail(
                PatternError.UNKNOWN_MODE,
                "未知样板模式 %r（可用：%s）" % (mode, "、".join(sorted(MODES))),
            )
        self.mode = mode
        self.inputs = self._stacks(inputs, "输入")
        self.outputs = self._stacks(outputs, "产物")
        self.returns = self._stacks(returns, "返还物")
        if not self.inputs:
            raise _fail(
                PatternError.EMPTY_INPUTS,
                "样板至少要有一条输入：零输入的配方会让网络凭空生成物品"
                "（全局约束 3：物品守恒）",
            )
        if not self.outputs:
            raise _fail(PatternError.EMPTY_OUTPUTS, "样板至少要有一个产物（07-01）")
        self._check_layout()
        self._check_returns()

    @staticmethod
    def _stacks(items, label):
        # type: (object, str) -> tuple
        if items is None:
            return ()
        # **要求真的是一组**（列表/元组），而不是「可迭代就行」。
        # 只判可迭代的话，一份写成字符串的 payload 会被逐字符拆成
        # 「非样板条目」报错，报错信息完全指不到真正的问题。
        if not isinstance(items, (list, tuple)):
            raise _fail(
                PatternError.MALFORMED,
                "%s必须是一组条目（元组/列表），得到 %r；单个条目要写成 (条目,)"
                % (label, type(items)),
            )
        out = []
        for item in items:
            if not isinstance(item, PatternStack):
                raise _fail(
                    PatternError.MALFORMED,
                    "%s里出现非样板条目 %r" % (label, item),
                )
            out.append(item)
        return tuple(out)

    def _check_layout(self):
        # type: () -> None
        """有序摆位的三条一致性规则。

        它们只对**输入**成立：产物与返还物不摆进工作台格子。
        """
        ordered = [stack for stack in self.inputs if stack.slot is not None]
        if ordered and self.mode != MODE_CRAFTING:
            raise _fail(
                PatternError.SLOT_NOT_ALLOWED,
                "工厂样板没有有序摆位；带槽位的输入只属于合成样板",
            )
        if ordered and len(ordered) != len(self.inputs):
            raise _fail(
                PatternError.ORDERED_MIX,
                "有序与无序不能混用：%d 条输入里只有 %d 条指定了槽位"
                % (len(self.inputs), len(ordered)),
            )
        seen = set()
        for stack in ordered:
            if stack.slot in seen:
                raise _fail(
                    PatternError.DUPLICATE_SLOT,
                    "槽位 %d 被两条输入同时占用" % (stack.slot,),
                )
            seen.add(stack.slot)

    def _check_returns(self):
        # type: () -> None
        """产物与返还物**都不摆进工作台格子**，因此都不接受 `slot` 与 `unlimited`。

        产物那条是补上的：早先只查了返还物，于是
        `PatternStack(key, 1, slot=5)` 放进 `outputs` 会被接受。那个槽位
        对 `output_keys()` / 索引 / `is_ordered()` 全都不可见，
        **却进 `to_dict()`、因而进指纹**——同一份配方于是有两个「版本」，
        而肉眼看不出差别；`execution.editable_view` 交给 11 的表单契约
        又明写「产物：槽位必须为空」，模型与校验对不上。
        """
        for group, label in ((self.outputs, "产物"), (self.returns, "返还物")):
            for stack in group:
                if stack.slot is not None:
                    raise _fail(
                        PatternError.SLOT_NOT_ALLOWED,
                        u"%s不摆进工作台格子，因此不能带槽位（%s）"
                        % (label, stack.key.name),
                    )
                if stack.unlimited:
                    raise _fail(
                        PatternError.FLAG_NOT_ALLOWED,
                        u"%s不能带「不限量」标记（%s）：那个标记说的是"
                        u"「这一条不向网络要」，只对输入有意义"
                        % (label, stack.key.name),
                    )

    # -- 查询 ----------------------------------------------------------

    def is_ordered(self):
        # type: () -> bool
        """是否是有序（工作台摆位）配方。"""
        return bool(self.inputs) and self.inputs[0].slot is not None

    def output_keys(self):
        # type: () -> tuple
        """全部产物的资源键，**去重且保持首次出现的顺序**。

        索引按它建：一份配方有多个产物时，每一个产物都要能查到这块样板
        （否则「8 份烤鱼 + 1 份空桶」只能从鱼那条路查到，从桶就查不到）。
        去重是因为同一种产物在 outputs 里出现两次时，索引里只该有一条。
        """
        out = []
        seen = set()
        for stack in self.outputs:
            if stack.key in seen:
                continue
            seen.add(stack.key)
            out.append(stack.key)
        return tuple(out)

    def has_fluid(self):
        # type: () -> bool
        for group in (self.inputs, self.outputs, self.returns):
            for stack in group:
                if stack.is_fluid():
                    return True
        return False

    def fluid_group(self):
        # type: () -> tuple
        """`("inputs"|"outputs"|"returns", 条目)` 里含流体的那些。

        诊断与将来的流体路径（13-03）要靠它回答「这份配方到底卡在哪」。
        """
        out = []
        for name, group in (
            ("inputs", self.inputs),
            ("outputs", self.outputs),
            ("returns", self.returns),
        ):
            for stack in group:
                if stack.is_fluid():
                    out.append((name, stack))
        return tuple(out)

    def fingerprint(self):
        # type: () -> tuple
        """比较与指纹用的规范形态：**按资源身份，不按线上载荷**。

        理由与 `PatternStack.fingerprint` 同：`to_dict()` 是无损线上格式
        （照抄 `raw_identity`），而 `ResourceKey` 的相等按身份算。拿
        `to_dict()` 做指纹会让「同一份配方的两种等价写法」得到两个版本号，
        旧规划因此被无谓地判成 stale。
        """
        return (
            self.mode,
            tuple(stack.fingerprint() for stack in self.inputs),
            tuple(stack.fingerprint() for stack in self.outputs),
            tuple(stack.fingerprint() for stack in self.returns),
        )

    def digest(self):
        # type: () -> str
        """内容指纹，**就是「样板版本」**。

        由内容派生：改了任何一个字，指纹就变，于是旧规划在派发前复核时
        拿到 `PATTERN_STALE`。不需要任何地方记得递增计数器——那种做法
        漏掉一处就等于旧规划能直接派发。
        """
        return tree_text(canonical_tree(self.fingerprint()))

    def summary(self):
        # type: () -> dict
        """给界面与诊断看的概要（07-07 的「样板概要」）。

        **不含完整内容**：界面列 9 格样板时不需要把每条原料都传一遍，
        点开某一块再要详情。传全量的代价是每帧几十 KB 的无关数据。
        """
        return {
            "mode": self.mode,
            "ordered": self.is_ordered(),
            "inputs": len(self.inputs),
            "outputs": len(self.outputs),
            "returns": len(self.returns),
            "has_fluid": self.has_fluid(),
            #: 标记了「不限量」的输入条数。含义**已于 2026-09-30 由 R09
            #: 定案**（由外部自理，见 `UnlimitedFuel`），这里仍只报事实、
            #: 不报解释——`summary()` 是给界面数格子的，解释归策略。
            "unlimited": len([s for s in self.inputs if s.unlimited]),
        }

    # -- 序列化 --------------------------------------------------------

    def to_dict(self):
        # type: () -> dict
        """**不含格式版本**：版本是编码层的事（见 `encode_pattern`）。

        分开的理由是 `digest()`：把版本算进指纹的话，一次格式升级会把
        所有样板都判成「变了」，而已派发的批次其实什么都没改。
        """
        return {
            "mode": self.mode,
            "inputs": [stack.to_dict() for stack in self.inputs],
            "outputs": [stack.to_dict() for stack in self.outputs],
            "returns": [stack.to_dict() for stack in self.returns],
        }

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> PatternDefinition
        if not isinstance(data, dict):
            raise _fail(PatternError.MALFORMED, "样板内容必须是对象，得到 %r" % (data,))
        unknown = set(data) - set(("mode", "inputs", "outputs", "returns"))
        if unknown:
            raise _fail(PatternError.MALFORMED, "样板出现未知字段 %r" % (sorted(unknown),))
        for field in ("mode", "inputs", "outputs"):
            if field not in data:
                raise _fail(PatternError.MALFORMED, "样板缺少 %s 字段" % (field,))
        return cls(
            data["mode"],
            cls._stacks_from_payload(data["inputs"], "输入"),
            cls._stacks_from_payload(data["outputs"], "产物"),
            cls._stacks_from_payload(data.get("returns", ()), "返还物"),
        )

    @staticmethod
    def _stacks_from_payload(items, label):
        # type: (object, str) -> list
        """从载荷里读一组条目。**先判类型，再逐条转换。**

        **顺序不能反。** 写成一句列表推导的话，`"inputs": 5` 会在任何校验
        之前先迭代，抛出 `TypeError: 'int' object is not iterable`——而
        `store._read_one` 只捕 `PatternValidationError`：一块伪造的样板能让
        异常冒到 `bootstrap.tick()` 变成 tick_error，**那台设备每帧都会再崩
        一次**，排在它后面的提供者永远刷不到。非列表要得到一条能读的
        `MALFORMED`，而不是一个逃出捕获网的 `TypeError`。
        """
        if not isinstance(items, (list, tuple)):
            raise _fail(
                PatternError.MALFORMED,
                "%s必须是一组条目（元组/列表），得到 %r" % (label, type(items)),
            )
        return [PatternStack.from_dict(item) for item in items]

    def __eq__(self, other):
        if not isinstance(other, PatternDefinition):
            return NotImplemented
        return self.fingerprint() == other.fingerprint()

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        #: 与 `__eq__` 一致，理由见 `PatternStack.__hash__`。
        return hash(self.fingerprint())

    def __repr__(self):
        return "PatternDefinition(%s, %d 入 / %d 出%s)" % (
            self.mode,
            len(self.inputs),
            len(self.outputs),
            "，有序" if self.is_ordered() else "",
        )


# -- 编解码 ----------------------------------------------------------------


class DecodedPattern(object):
    """一次成功解码的结果：内容 + 编码格式 + 「能不能直接派发」。

    **格式与内容分开是有意的**：格式是「这份数据怎么读」，内容是「这个配方
    是什么」。升级格式不该改变内容指纹，因此 `digest()` 走内容、
    `is_dispatchable()` 走格式。
    """

    __slots__ = ("definition", "format")

    def __init__(self, definition, format_version):
        # type: (PatternDefinition, int) -> None
        self.definition = definition
        self.format = int(format_version)

    def is_dispatchable(self):
        # type: () -> bool
        """这一份现在能不能拿去派发。

        `PATTERN_MIN_DISPATCHABLE_FORMAT` 以下的**读得出来但不可派发**：
        界面照样能显示它的内容（人要看得见才知道该重做），但订单不能用它。
        """
        return (
            PATTERN_MIN_DISPATCHABLE_FORMAT
            <= self.format
            <= PATTERN_FORMAT_VERSION
        )

    def needs_upgrade(self):
        # type: () -> bool
        return self.format < PATTERN_FORMAT_VERSION

    def digest(self):
        # type: () -> str
        return self.definition.digest()

    def to_dict(self):
        # type: () -> dict
        return {
            "format": self.format,
            "dispatchable": self.is_dispatchable(),
            "pattern": self.definition.to_dict(),
        }

    def __repr__(self):
        return "DecodedPattern(v%d, %r)" % (self.format, self.definition)


def encode_pattern(definition):
    # type: (PatternDefinition) -> dict
    """把一份配方编成可写进 `userData` 的描述。**输出保证是纯 ASCII。**

    **只在写入时加上格式版本**，因此这个函数与 `decode_pattern` 是一对，
    而 `PatternDefinition.to_dict()` 保持与版本无关。
    """
    if not isinstance(definition, PatternDefinition):
        raise _fail(
            PatternError.MALFORMED,
            "只能编码 PatternDefinition，得到 %r" % (type(definition),),
        )
    payload = {"format": PATTERN_FORMAT_VERSION}
    payload.update(_escape_tree(definition.to_dict()))
    return payload


def decode_pattern(payload):
    # type: (object) -> DecodedPattern
    """把描述解成 `DecodedPattern`。**不信任外部载荷**。

    三件事分开：格式读不出来（`MALFORMED`）、格式比本版本新
    （`FORMAT_TOO_NEW`，原样保留）、内容不合法（各自的码）。
    """
    if not isinstance(payload, dict):
        raise _fail(PatternError.MALFORMED, "样板描述必须是对象，得到 %r" % (payload,))
    if "format" not in payload:
        raise _fail(PatternError.MALFORMED, "样板描述缺少 format 字段")
    version = payload["format"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise _fail(
            PatternError.MALFORMED,
            "样板格式版本必须是整数，得到 %r" % (version,),
        )
    if version > PATTERN_FORMAT_VERSION:
        raise _fail(
            PatternError.FORMAT_TOO_NEW,
            "样板格式版本 %d 比本版本（%d）新；读不懂就不读，原样保留"
            % (version, PATTERN_FORMAT_VERSION),
        )
    # **`format` 要先摘掉再交给内容解析**：`PatternDefinition.from_dict`
    # 拒绝未知字段（那是对外部载荷的守卫），而 `format` 属于编码层、
    # 不属于内容——不摘掉的话每一份**合法**样板都会被判成 MALFORMED。
    body = dict(payload)
    body.pop("format", None)
    body = _unescape_tree(body)
    return DecodedPattern(PatternDefinition.from_dict(body), version)


# -- ASCII 编码 ------------------------------------------------------------
#
# **为什么必须在写之前把文本转成纯 ASCII**（2026-09-25 游戏内实测）：
# 方块容器的 `userData` 对非 ASCII 文本是**破坏性**的：
#
# | 写进去 | 读回来 |
# | --- | --- |
# | 非 ASCII 的 `unicode` | **整个键消失**；在列表或嵌套字典里则整个容器被清空 |
# | 非 ASCII 的 UTF-8 字节串 | 留得下，但被再编码一次（`中文` 读回来是 `ä¸æ`） |
# | 纯 ASCII | 原样往返 |
#
# 样板引用的物品可以带中文（自定义提示、别的模组写进 `user_data` 的文本），
# 因此这一层不是可选的：少了它，那种样板存进去之后会变成**另一块**
# ——而且是一块再也读不出来的。用户可见的后果是「样板莫名其妙失效了」，
# 而没有任何地方报错。


def _escape_text(text):
    # type: (object) -> str
    """把一个文本值转成纯 ASCII 的**字节串**：反斜杠与所有非 ASCII 写成转义。

    **返回的必须是 py2 的 `str`（字节串），不能是 `unicode`。**
    这是实测出来的第二条约束（2026-09-25，隔离实验）：

    | 写进 `userData` 的 | 读回来 |
    | --- | --- |
    | `{"k": "v"}`（字节串） | 原样往返 |
    | `{u"k": u"v"}` 或 `{u"k": "v"}` | **整个条目消失** |
    | `{"k": u"v"}` | 条目还在，但被**清空成 `{}`** |

    也就是说引擎对 `userData` 里的 **`unicode` 一律破坏**，与内容是不是
    ASCII 无关。第一版转义层用 `u"".join(...)` 构造，于是一份**完全正确**
    的样板写进去之后就整个不见了——而且不报任何错。
    因此这里逐字符拼**字节串**，非 ASCII 走 `\\uXXXX`。
    """
    if not isinstance(text, _UNICODE):
        # py2 的字节串：先按 UTF-8 解成文本。解不开就**让它抛**
        # （`UnicodeDecodeError` 是 `ValueError` 的子类，命令层兜得住），
        # 而不是用 `replace` 把它悄悄改成问号。
        text = text.decode("utf-8")
    pieces = []
    for char in text:
        code = ord(char)
        if char == u"\\":
            pieces.append(u"\\\\")
        elif code < 128:
            pieces.append(char)
        elif code <= 0xFFFF:
            pieces.append(u"\\u%04x" % (code,))
        else:
            # 星平面。窄版 py2 上这类字符本来就已经是两个代理字符，
            # 各自走上面的 `\\u` 分支，因此这里只会在宽版/py3 上出现。
            pieces.append(u"\\U%08x" % (code,))
    escaped = u"".join(pieces)
    # py2 上编回字节串（内容已是纯 ASCII，因此这一步不会丢东西）；
    # py3 上 `str` 就是 `_UNICODE`，原样返回。
    return escaped if _UNICODE is str else escaped.encode("ascii")


def _unescape_text(text):
    # type: (object) -> _UNICODE
    """`_escape_text` 的逆。**认不出的转义原样保留**，不抛。

    读的这一侧返回 `unicode` 没关系——约束只在**写进 `userData`** 的方向上
    （引擎破坏 unicode），而解码结果本来就该是文本。
    """
    if not isinstance(text, _UNICODE):
        text = text.decode("ascii")
    out = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != u"\\" or index + 1 >= length:
            out.append(char)
            index += 1
            continue
        marker = text[index + 1]
        if marker == u"\\":
            out.append(u"\\")
            index += 2
            continue
        size = 4 if marker == u"u" else (8 if marker == u"U" else 0)
        if size and index + 2 + size <= length:
            try:
                code = int(text[index + 2:index + 2 + size], 16)
            except ValueError:
                code = -1
            if 0 <= code <= 0x10FFFF:
                out.append(_UNICHAR(code))
                index += 2 + size
                continue
        out.append(char)
        index += 1
    return u"".join(out)


def _escape_tree(value):
    # type: (object) -> object
    """递归地把整棵描述里的文本转成纯 ASCII。键与值都转。

    键也转是保险：实测非 ASCII 的**键**能活下来，但那是这一版引擎的行为，
    而这里多转一次几乎不花钱（纯 ASCII 逐字节不变）。
    """
    if isinstance(value, _TEXT_TYPES):
        return _escape_text(value)
    if isinstance(value, dict):
        return dict(
            (_escape_text(key), _escape_tree(item)) for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return [_escape_tree(item) for item in value]
    return value


def _unescape_tree(value):
    # type: (object) -> object
    """`_escape_tree` 的逆。"""
    if isinstance(value, _TEXT_TYPES):
        return _unescape_text(value)
    if isinstance(value, dict):
        return dict(
            (_unescape_text(key), _unescape_tree(item))
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return [_unescape_tree(item) for item in value]
    return value


def describe_tip(definition):
    # type: (PatternDefinition) -> str
    """一行人类可读的摘要，写进样板物品的 `customTips`。

    存在的理由很实际：样板是**塞在容器里的实物物品**，玩家在容器界面里
    除了名字什么都看不到，两块同名样板长得一模一样。有了这一行，
    「哪块是哪块」至少能靠悬停分辨。

    **它是派生的展示文本，不是身份**——`digest()` 不看它。反过来说，
    改这一行的措辞**不会**让玩家的样板变成另一个版本（不会触发旧规划复核），
    因为内容一个字节都没变。
    """
    parts = [u"%d×%s" % (stack.count, stack.key.name) for stack in definition.inputs]
    out = ["、".join(parts)]
    oparts = [u"%d×%s" % (stack.count, stack.key.name) for stack in definition.outputs]
    out.append("、".join(oparts))
    text = u"%s → %s" % (out[0], out[1])
    if definition.returns:
        text += u"（返还 %s）" % (
            "、".join(
                u"%d×%s" % (stack.count, stack.key.name)
                for stack in definition.returns
            ),
        )
    if definition.is_ordered():
        text = u"[有序] " + text
    return text


def has_pattern(user_data):
    # type: (object) -> bool
    """这份 `userData` 里有没有本模组的样板键。**不解码**，因此不会抛错。"""
    return isinstance(user_data, dict) and PATTERN_DATA_KEY in user_data


def read_pattern(user_data):
    # type: (object) -> DecodedPattern
    """从物品的 `userData` 里读出样板。

    「不是样板」（没有本模组的键）与「是样板但内容坏了」分开报：
    前者是 `NOT_A_PATTERN`，后者是各自的码。界面要能区分，
    因为对玩家来说一句是「这不是样板」，另一句是「这块样板做坏了」。
    """
    if not isinstance(user_data, dict):
        raise _fail(
            PatternError.NOT_A_PATTERN,
            "物品没有 userData，因而不是样板",
        )
    if PATTERN_DATA_KEY not in user_data:
        raise _fail(
            PatternError.NOT_A_PATTERN,
            "userData 里没有 %s 键，因而不是样板" % (PATTERN_DATA_KEY,),
        )
    return decode_pattern(user_data[PATTERN_DATA_KEY])


def write_pattern(user_data, definition):
    # type: (object, PatternDefinition) -> dict
    """返回一份**新的** `userData`：本模组的键被替换，其余键原样保留。

    两条纪律：

    - **不改传入的字典**。物品字典在事件路径上是引擎给的对象，就地改它
      的有效性未经验证；更重要的是调用方可能还要拿原值比对。
    - **不碰别人的键**。`userData` 是所有模组共用的一块存储，本模组只拥有
      `PATTERN_DATA_KEY` 这一个；整份覆盖会把别的模组的数据抹掉。

    **`userData` 不是字典时明确拒绝，不降级成空字典。** 「不是字典」
    意味着调用方手里那份数据是另一种表示（引擎某些路径给的是字符串），
    此时返回一份只带本模组键的新字典、再被写回物品，等于**把别的模组
    的数据整份抹掉**——而这条纪律的第一句话刚说过不能那么做。
    读的那一侧（`read_pattern`）也是拒绝，两边一致。
    `None` 是例外：它明确表示「这个物品还没有任何 userData」，等价于空。
    """
    if user_data is None:
        out = {}
    elif isinstance(user_data, dict):
        out = dict(user_data)
    else:
        raise _fail(
            PatternError.MALFORMED,
            "userData 必须是字典或 None，得到 %r；"
            "不能当成空字典写入，那会抹掉别的模组的数据" % (type(user_data),),
        )
    out[PATTERN_DATA_KEY] = encode_pattern(definition)
    return out


# -- R09 的策略取值 --------------------------------------------------------


class UnlimitedFuel(object):
    """「无限燃料」的含义。**R09 的这几条里，这一条已于 2026-09-30 定案。**

    两处文档的话各说一侧，合起来就是这条规则：

        概念页：默认熔炉有无限的燃料供应  -> 配方里不记燃料，它不进网络的账
        任务 07：**不自动供给未授权燃料**  -> 不替玩家供一条他没写进来的料

    于是样板里的三种写法，行为都是定的：

    | 样板里怎么写        | 网络做什么             |
    | ------------------- | ---------------------- |
    | 不写燃料（系统预设）| 不备——这条根本不进账   |
    | 写了并标 `unlimited`| 不备——那条由外部自理   |
    | 写了、没标          | 照常备料               |

    **要不要燃料由玩家在工厂样板里设定**。网络那一侧只做两件事：按样板声明的
    输入向对应样板接口投料、接收成品——它不判断这台机器该不该烧燃料，也不去
    猜哪条输入其实是燃料。

    「要全由网络控制」因此不是把这个标记解释成别的意思，而是**别标记它**：
    工厂样板记成 `8 原料 + 1 燃料 -> 8 产物`，网络连同原料一起投入样板接口的
    独立原料缓冲，再由**外部分类器**把两者分流进熔炉的原料口与燃料口。
    """

    #: **定案取值**：那条输入由**外部机器自理**（熔炉的燃料来自不属于网络的
    #: 外界环境），网络不为它备料——不会凭空造出玩家没准备的燃料。
    EXTERNAL_SUPPLY = "external_supply"
    #: **已排除的另一侧**（标记只是玩家对配方的说明、派发时照常备齐）：
    #: 它与上面的定案相反，`PatternPolicy(...)` **构造期直接报错**。
    #: 留着这个名字是为了报错时说得清「排除了哪一侧」。
    MODELING_ONLY = "modeling_only"


class ReturnRule(object):
    """R09：返还物规则。**2026-09-30 定案：返还物必须由样板显式声明。**

    概念例子里「8 份生鱼 + 1 份木炭」不返还什么，而 09 文档里出现了
    「鱼已回流但**空桶未到**」——那个空桶由谁写进配方？定案：**由样板自己写**
    （`returns` 一组），模组**不做任何推导**。

    这与「样板内容由谁写」那两条声明是同一条纪律的另一半：样板是**完整记录**，
    原料、产物、**返还物**三组都在里面，**任何其他地方都不再推导、不再变更**。
    空桶因此是 `returns` 里的一行数据，而不是某处按输入现算出来的东西。

    代价是玩家（或样板终端的编辑界面）得自己把空桶写上。收益是「该不该回、
    回几个」不再由一张跨模组的「容器物品 → 空容器」映射表在背后替玩家决定——
    那张表要穷举引擎物品、模组物品与流体容器，本身就不可能有权威版本。

    另一个被排除的理由在 10：一个「装作能用」的推导返回空表，会让完成判定
    永远满足，而「空桶未到就完成了」正是 09 文档点名要防的那件事。
    """

    #: **定案取值**：返还物必须由样板**显式声明**（写在 `returns` 里），
    #: 模组不推导。也是 `PatternPolicy` 唯一接受的取值。
    DECLARED_ONLY = "declared_only"
    #: **已排除的另一侧**（由模组按输入里的容器类物品推导空容器）：
    #: 它与上面的定案相反，`PatternPolicy(...)` **构造期直接报错**。
    #: 留着这个名字是为了报错时说得清「排除了哪一侧」——照
    #: `UnlimitedFuel.MODELING_ONLY` / `ReturnPolicy.exclusive` 的先例。
    #:
    #: （2026-09-30 之前这里写的是「尚未实现：需要一张容器映射表，属 13」。
    #: 定案之后它不再是「待实现」，而是**不会实现**——要找的是「空桶该不该
    #: 写进样板」，不是「谁来生成空桶」。）
    DERIVE_CONTAINERS = "derive_containers"


class KindScope(object):
    """R09：两类样板的适用范围。"""

    #: 合成样板只进装配仓、工厂样板只进总线样板接口（items.md 逐条写明）。
    #: **替身取值**。
    STRICT = "strict"
    #: 不按设备区分，任何样板提供者都接受任何模式。
    ANY = "any"


class PatternPolicy(object):
    """样板侧的旋钮（R09）。

    与 `PortPolicy` / `MediumPolicy` / `DriverPolicy` 同形：做成具名策略而不是
    常数，因为 R09 还没整条定案（全局约束 7）。`assert_settled()`
    **只应在正式验收时调用**——开发与离线测试不应调用它。

    三条里已经有两条于 **2026-09-30 由 User 定案**：

    - `unlimited_fuel`：标记 `unlimited` 的输入一律**由外部自理**；
    - `return_rule`：返还物一律**由样板显式声明**，模组不推导。

    两条都照 `ReturnPolicy.exclusive` 的先例**留成字段**——构造期只接受定案的
    那一侧（另一侧直接报错），但**不进 `unresolved_report()`**。

    只剩 `kind_scope`（两类样板的适用范围）一条仍进 `unresolved_report()`，
    并由 `bootstrap.policy_bundle()` 交给装配检查；少合一份的代价是正式验收
    时 R09 漏网。
    """

    __slots__ = ("unlimited_fuel", "return_rule", "kind_scope")

    def __init__(
        self,
        unlimited_fuel=UnlimitedFuel.EXTERNAL_SUPPLY,
        return_rule=ReturnRule.DECLARED_ONLY,
        kind_scope=KindScope.STRICT,
    ):
        # type: (str, str, str) -> None
        if unlimited_fuel != UnlimitedFuel.EXTERNAL_SUPPLY:
            # **2026-09-30 定案**：标记 `unlimited` 的输入由外部自理。
            # 另一侧（标记只是说明、网络照常备齐）与定案相反，因此在装配期
            # 就报错——它看起来完全合理，一旦生效就是另一套玩法。
            # 要让网络供给就**别标记**那条输入，见 `UnlimitedFuel`。
            raise ValueError(
                u"无限燃料口径 %r 已被 R09 排除（2026-09-30 定案）："
                u"标记 `unlimited` 的输入一律由外部自理，网络不备料。"
                u"要由网络供给，就把燃料写成普通输入。目前只能用 %r"
                % (unlimited_fuel, UnlimitedFuel.EXTERNAL_SUPPLY)
            )
        if return_rule != ReturnRule.DECLARED_ONLY:
            # **2026-09-30 定案**：返还物一律由样板显式声明，模组不推导。
            # 另一侧（按输入推导空容器）与定案相反，因此在装配期就报错：
            # 它需要一张跨模组的「容器物品 → 空容器」映射表（那张表不可能
            # 有权威版本），而更要紧的是，一个「装作能用」的空表会让 10 的
            # 完成判定永远满足——「空桶未到就完成了」正是 09 文档点名要防的
            # 那件事。要空桶回流，就在样板的 `returns` 里写一行。
            raise ValueError(
                u"返还物规则 %r 已被 R09 排除（2026-09-30 定案）："
                u"返还物一律由样板显式声明（写在 `returns` 里），"
                u"模组不按输入推导空容器。目前只能用 %r"
                % (return_rule, ReturnRule.DECLARED_ONLY)
            )
        if kind_scope not in (KindScope.STRICT, KindScope.ANY):
            raise ValueError("未知的样板适用范围口径: %r" % (kind_scope,))
        self.unlimited_fuel = unlimited_fuel
        self.return_rule = return_rule
        self.kind_scope = kind_scope

    # -- 判定 ----------------------------------------------------------

    def supplies(self, stack):
        # type: (PatternStack) -> bool
        """这次派发要不要为这一条输入备料。

        **这是「无限燃料」的唯一落点，R09 已于 2026-09-30 定案**：未标记
        `unlimited` 的条目一律要备；标记了的那条由**外部自理**——熔炉的燃料
        来自不属于网络的外界环境，网络不为它备料。
        """
        if not stack.unlimited:
            return True
        if self.unlimited_fuel != UnlimitedFuel.EXTERNAL_SUPPLY:
            # 构造期已经挡掉了另一侧；走到这里说明有人绕过了 `__init__`。
            raise ValueError(
                u"无限燃料口径 %r 已被 R09 排除" % (self.unlimited_fuel,)
            )
        return False

    def required_inputs(self, definition):
        # type: (PatternDefinition) -> tuple
        """按当前口径，本次派发真正要向网络要的那些输入。"""
        return tuple(
            stack for stack in definition.inputs if self.supplies(stack)
        )

    def expected_returns(self, definition):
        # type: (PatternDefinition) -> tuple
        """本次派发应当期待回流的返还物（10 的完成判定读它）。

        **R09 已于 2026-09-30 定案：返还物一律由样板显式声明**，因此这里
        除了 `definition.returns` 没有第二个来源，也没有「推导出来的空容器」
        这一路。10 那边不必再区分「声明来的」与「算出来的」——只有前者。
        """
        return tuple(definition.returns)

    def accepts_mode(self, device_id, mode):
        # type: (str, str) -> bool
        """这台设备收不收这种模式的样板。

        表在 `common/definitions.py` 的 `PATTERN_DEVICES`（**已定案的事实**），
        是否执行由本字段决定（**未定案的规则**）。分开的理由是 R09 定案时
        只改策略即可，不必动数据表，也不会让已存档的判断换一套依据。
        """
        if self.kind_scope == KindScope.ANY:
            return True
        return device_id in devices_for_mode(mode)

    # -- 未定案守卫 ----------------------------------------------------

    def unresolved_report(self):
        # type: () -> list
        """仍未定案的字段。**R09 三条里只剩 `kind_scope` 一条在里面。**

        `unlimited_fuel`（2026-09-30 定案：标记 `unlimited` 的输入由外部自理）与
        `return_rule`（同日定案：返还物一律由样板显式声明，模组不推导）都已收口，
        照 `ReturnPolicy.exclusive` 的先例留成字段、不进本列表。

        ## 先排掉两件**已经定案**的事：样板内容由谁写

        本列表只收「还没定」的旋钮。**样板的内容是怎么来的不在其中**，
        两条早已定案；写在这里是因为翻到本函数的人最容易把它们误当成待定项
        （毕竟「产物是谁定的」看起来正该由策略决定）：

        | 模式 | 内容来源 | 样板里记什么 |
        | --- | --- | --- |
        | 合成样板 `crafting` | 在**样板终端**配置配方时，**按合成配方自动推导产物** | **完整的原料与产物**；此后**任何其他地方都不再推导、不再变更** |
        | 工厂样板 `factory` | **用户手动配置** | 用户写下的所需原料与**期望的产物** |

        为什么这两条是「已定案」而不是「待定」：

        - **合成样板那一侧，配方是引擎给的既有事实**（工作台的预设合成配方），
          推导只发生在**配置的那一次**，推导结果**整份写进样板**。于是下游
          根本没有第二次推导的机会：08 按样板记的原料算料、装配仓按样板记的
          产物执行、10 按样板记的产物收回流——读的都是同一份内容，谁也不去
          查配方表。「样板编辑后旧规划必须复核」也落在同一条上：内容变了
          `digest()` 就变（见 `PatternDefinition.digest`），而不是靠某处记得
          重算产物。
        - **工厂样板那一侧，「期望的产物」本来就是玩家的意思表示**
          （`concepts.md`：「在工厂样板中记录的期望的产物**全部**输送回网络时，
          才判定此样板已完成一次合成」）。网络没有第二处能推出它，因此连
          「谁来推导产物」这个待定项都不存在。

        **返还物是同一条纪律的第三组**（2026-09-30 一并定案，见 `ReturnRule`）：
        样板记全原料、产物、返还物三组，空桶是 `returns` 里的一行数据，不是
        谁按输入现算出来的东西。

        剩下这一条与上面那些都无关：`kind_scope` 说的是**这模式能插到哪台
        设备上**（位置），不是「内容从哪来」。别把它们读成一件事。
        """
        return [
            {
                "field": "pattern.kind_scope",
                "rule": "R09",
                "value": self.kind_scope,
                "note": (
                    "两类样板的适用范围；替身按 items.md 严格区分"
                    "（合成样板 -> 装配仓，工厂样板 -> 总线样板接口）"
                ),
            },
        ]

    def assert_settled(self):
        # type: () -> None
        """正式验收用：仍有未定项就报错。开发与离线测试不应调用它。"""
        pending = self.unresolved_report()
        if pending:
            raise AssertionError(
                "样板策略仍有 %d 项未定案（R09），不能作为正式玩法交付：\n  "
                % (len(pending),)
                + "\n  ".join(
                    "%s（当前值 %r）" % (item["field"], item["value"])
                    for item in pending
                )
            )

    def describe(self):
        # type: () -> dict
        return {
            "unlimited_fuel": self.unlimited_fuel,
            "return_rule": self.return_rule,
            "kind_scope": self.kind_scope,
        }

    def __repr__(self):
        return "PatternPolicy(%s, %s, %s)" % (
            self.unlimited_fuel,
            self.return_rule,
            self.kind_scope,
        )
