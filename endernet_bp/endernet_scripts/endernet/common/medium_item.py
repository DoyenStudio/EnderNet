# coding=utf-8
"""存储介质物品的**载体与展示契约**（引擎无关）。

对应任务 12-02 的定案与 12-03 的落地，契约原文见
`plans/technical_notes/medium_lore.md` 第 2 节。

本模块只做三件事，全是纯函数：

1. **载体**：介质物品用 `userData["endernet:medium"] = {"id": <ASCII>, "v": <int>}`
   携带自己的持久身份。ID **只在首次挂载/进网时**由持久化发号器分配，
   读取路径永不生成（`read_carrier` 只认不造）。
2. **展示**：容量那两行由 `medium_lore.md` 认证的**不可见标记**认领，
   识别**只按标记串**——不按中文前缀、不按行号。玩家和其他模组完全可能
   拥有同样文本，也可能插进或重排自己的 Lore。
3. **身份投影**：`ResourceKey` 比较时，仅对本模组管理的介质剔除那两行动态
   容量行。**真实物品载荷一个字都不改**——投影产出的字典只用于比较。

## 为什么到处都在与 py2/py3 的类型分裂搏斗

`userData` 是**引擎**的存储，而引擎跑 py2：它会把 `unicode` **值**连同它的键
静默丢掉，非 ASCII 的字节串却字节级原样（`resource_identity.md` §2.2，容器与
背包两条路径都实测过）。因此：

- **写进去的东西必须是 py2 的 `str`（字节串）**。离线测试跑在 py3 上，
  那里的 `str` 就是文本——同一份代码在两边要落成不同的类型。
- 标记 `§1§2§3` 在 py2 下是 UTF-8 的 9 个字节 `\\xc2\\xa71\\xc2\\xa72\\xc2\\xa73`，
  在 py3 下是 9 个字符。**同一串文本，两种落盘形态。**

`common/` 里的其他模块（`resource_key.py`）没有这个问题，因为它们从不
自己拼要写进 `userData` 的串。这里是第一处，所以这段说明留在这里。
"""

try:  # pragma: no cover - 仅在 Python 2 下成立
    _PY2 = True
    _UNICODE = unicode
    _TEXT_TYPES = (str, unicode)
    _INT_TYPES = (int, long)
    #: py2 的 `userData` 是字节世界：标记就是那 9 个字节本身。
    #: 拼接必须走字节串——`str + unicode` 会升格成 unicode，而引擎会把它丢掉。
    MANAGED_LORE_MARK = "\xc2\xa71\xc2\xa72\xc2\xa73"
except NameError:  # pragma: no cover - 仅在 Python 3 下成立
    _PY2 = False
    _UNICODE = str
    _TEXT_TYPES = (str,)
    _INT_TYPES = (int,)
    #: py3 下同一个标记是文本：上面那串 UTF-8 字节解码回来就是这些字符。
    MANAGED_LORE_MARK = u"\xa71\xa72\xa73"


#: 载体键。带命名空间前缀是必须的：`userData` 是**所有模组共用**的一块存储，
#: 本模组只拥有自己这个键（与 `crafting/patterns.py` 的 `"endernet:pattern"`
#: 同一纪律）。
MEDIUM_CARRIER_KEY = "endernet:medium"

#: 载体自身的格式版本。**不写进 Lore**，由它给出单一事实来源——否则会出现
#: 「Lore 说 v1、载体说 v2」这种两处不同步的状态（`medium_lore.md` 第 2 节第 3 条）。
CARRIER_VERSION = 1

DISPLAY_KEY = "display"
LORE_KEY = "Lore"


# -- 类型形态 --------------------------------------------------------------


def _to_mark_domain(text):
    # type: (object) -> object
    """把一行 Lore 转成与 `MANAGED_LORE_MARK` **同一形态**的串。

    py2 下把 `unicode` 编成 UTF-8 字节串（与引擎落盘的一致），py3 下原样。
    非文本一律返回 None——调用方据此判「这行不是我们管的」。
    """
    if not isinstance(text, _TEXT_TYPES):
        return None
    if _PY2 and isinstance(text, _UNICODE):
        return text.encode("utf-8")
    return text


def _to_carrier_text(text):
    # type: (object) -> object
    """把一个**已知是纯 ASCII 的**文本转成写进 `userData` 的形态。

    py2 → 字节串（引擎只留得住字节串）；py3 → 原样。非 ASCII 返回 None，
    调用方负责报错或忽略——ID 是纯 ASCII 的，出现别的就是数据有问题。
    """
    if not isinstance(text, _TEXT_TYPES) or not text:
        return None
    if isinstance(text, str) and not isinstance(text, _UNICODE):
        # py2 的字节串：确认它确实是 ASCII（非 ASCII 的 ID 不是我们写的）。
        try:
            decoded = text.decode("ascii")
        except UnicodeDecodeError:
            return None
        if _PY2:
            return text
        return decoded
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        return None
    if _PY2:
        return text.encode("ascii")
    return text


# -- 载体 ------------------------------------------------------------------


def read_carrier(user_data):
    # type: (object) -> object
    """从 `userData` 读出介质身份，返回 `(medium_id, version)`；不是本模组的介质返回 None。

    **读取路径永不生成 ID。** 这里只认已经写在物品上的那一个。

    py2 的 `unicode` ID 直接判为「不是本模组管理的介质」：引擎根本留不住
    unicode 值，认了等于自欺——那个 ID 下次读回来就不见了。
    """
    if not isinstance(user_data, dict):
        return None
    carrier = user_data.get(MEDIUM_CARRIER_KEY)
    if not isinstance(carrier, dict):
        return None
    raw_id = carrier.get("id")
    if _PY2 and isinstance(raw_id, _UNICODE):
        return None
    medium_id = _to_carrier_text(raw_id)
    if medium_id is None:
        return None
    version = carrier.get("v")
    if isinstance(version, bool) or not isinstance(version, _INT_TYPES):
        return None
    return (medium_id, int(version))


def write_carrier(user_data, medium_id, version=CARRIER_VERSION):
    # type: (object, object, int) -> dict
    """把介质身份写进 `userData`，返回**新字典**（不改入参）。

    `medium_id` 必须是纯 ASCII——它由本模组的发号器产出，形状是 `medium:<n>`。
    """
    encoded = _to_carrier_text(medium_id)
    if encoded is None:
        raise ValueError("介质 ID 必须是纯 ASCII 文本，得到 %r" % (medium_id,))
    if isinstance(version, bool) or not isinstance(version, _INT_TYPES):
        raise TypeError("载体格式版本必须是整数，得到 %r" % (version,))
    out = dict(user_data) if isinstance(user_data, dict) else {}
    out[MEDIUM_CARRIER_KEY] = {"id": encoded, "v": int(version)}
    return out


def carrier_of(medium, user_data=None):
    # type: (object, object) -> dict
    """按一块介质造出它的载体字典。`medium` 只要有 `medium_id` 即可。"""
    return write_carrier(user_data, medium.medium_id, CARRIER_VERSION)


# -- 管理行 ----------------------------------------------------------------


def is_managed_line(line):
    # type: (object) -> bool
    """这一行 Lore 是不是本模组管理的动态容量行。

    **只按标记串判定。** 不按中文前缀、不按行号——玩家和其他模组完全可能
    拥有同样文本，也可能插入或重排自己的 Lore。
    """
    prepared = _to_mark_domain(line)
    if prepared is None:
        return False
    try:
        return prepared.endswith(MANAGED_LORE_MARK)
    except UnicodeDecodeError:  # pragma: no cover - py2 混型时的兜底
        return False


def mark_managed_line(line):
    # type: (object) -> object
    """给一行加归属标记；形态与 `MANAGED_LORE_MARK` 一致（py2 字节 / py3 文本）。"""
    prepared = _to_mark_domain(line)
    if prepared is None:
        raise TypeError("Lore 行必须是文本，得到 %r" % (line,))
    return prepared + MANAGED_LORE_MARK


def medium_lore_lines(medium):
    # type: (object) -> tuple
    """一块介质该显示的两行（**已带标记**）。

    渲染只有一处实现：`Medium.capacity_lines()`。这里只负责加归属标记。
    """
    return tuple(mark_managed_line(line) for line in medium.capacity_lines())


# -- 身份投影 --------------------------------------------------------------


def project_identity_user_data(user_data):
    # type: (object) -> object
    """身份投影：剔除管理行，供 `ResourceKey` 比较用。

    **这不是清洗，是投影。** 返回值只用于算身份；真实物品载荷仍走
    `raw_identity`，一个字都不改。

    边界（`medium_lore.md` 第 2 节第 1、4 条）：

    - **只对已识别的本模组介质生效**（有合法载体）。别人的物品即使碰巧有
      同样文本的 Lore，也一个字节都不动。
    - 剔除后 `Lore` 为空则省略 `Lore`，`display` 为空则省略 `display`——
      与「本来就没有」同形，这样「添加/删除受管理展示信息」不改变身份。
    - 载体键本身**保留在投影里**：稳定 ID 参与实例区分，正是它让
      「同型号、同剩余量的两块不同介质」不被合并。

    没有可剔除的行时**原样返回入参**（不复制），调用方可以据此判「没变」。
    """
    if not isinstance(user_data, dict):
        return user_data
    if read_carrier(user_data) is None:
        return user_data
    display = user_data.get(DISPLAY_KEY)
    if not isinstance(display, dict):
        return user_data
    lore = display.get(LORE_KEY)
    if not isinstance(lore, (list, tuple)):
        return user_data
    kept = [line for line in lore if not is_managed_line(line)]
    if len(kept) == len(lore):
        return user_data

    out = dict(user_data)
    new_display = dict(display)
    if kept:
        new_display[LORE_KEY] = kept
    else:
        del new_display[LORE_KEY]
    if new_display:
        out[DISPLAY_KEY] = new_display
    else:
        del out[DISPLAY_KEY]
    return out


# -- 展示写回 --------------------------------------------------------------


def apply_capacity_lines(user_data, lines):
    # type: (object, tuple) -> dict
    """把容量行写进 `display.Lore`，返回**新字典**。

    规则：**替换**已有的管理行，**保留**其余 Lore、自定义名称与一切无关
    元数据；没有管理行时追加。重复调用是幂等的——不会重复追加（验收里
    「刷新容量行不覆盖名称及无关元数据」与「无内容变化的重复刷新应幂等」）。

    `lines` 是已经渲染好的行（普通文本，不带标记），本函数负责加标记。
    """
    out = dict(user_data) if isinstance(user_data, dict) else {}
    display = out.get(DISPLAY_KEY)
    display = dict(display) if isinstance(display, dict) else {}
    lore = display.get(LORE_KEY)
    lore = list(lore) if isinstance(lore, (list, tuple)) else []

    kept = [line for line in lore if not is_managed_line(line)]
    for line in lines:
        kept.append(mark_managed_line(line))
    display[LORE_KEY] = kept
    out[DISPLAY_KEY] = display
    return out


def materialize_item_user_data(medium, user_data):
    # type: (object, object) -> dict
    """按权威内容把一件介质物品的 `userData` 物化到最新。

    = 写回载体 + 用最新权威摘要替换容量行。**保留其他一切字段**（名称、
    其他 Lore、`customTips` 与非本模组的 `userData` 键）。

    **给 `apply_capacity_lines` 的必须是没带标记的原文**（`capacity_lines()`），
    不是 `medium_lore_lines()`——后者已经加过标记，再传进去会被
    `apply_capacity_lines` **再加一次**，行尾变成 `§1§2§3§1§2§3`。
    这个重复直到 2026-10-04 驱动器接上展示写回、第一次真的调用本函数才现形：
    在那之前它一个调用方都没有（`medium_lore.md` §6 一直写着「没有任何代码
    在真机上把载体写进物品」）。
    """
    return apply_capacity_lines(
        carrier_of(medium, user_data), medium.capacity_lines()
    )


def describe_carrier(user_data):
    # type: (object) -> dict
    """只读诊断视图。读不出载体时如实报出来，不猜。"""
    carrier = read_carrier(user_data)
    if carrier is None:
        return {"managed": False, "medium_id": None, "version": None}
    return {"managed": True, "medium_id": carrier[0], "version": carrier[1]}
