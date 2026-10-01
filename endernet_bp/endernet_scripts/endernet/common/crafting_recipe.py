# coding=utf-8
"""工作台预设配方：规范化与摆位匹配（11-04）。

**本模块不 import 引擎，也不 import `server/`。** 它只做两件事：

- `normalize_recipe(raw)`：把引擎给的原始配方字典收成一个稳定形状；
- `find_match(recipes, cells)`：拿一份 3×3 摆位去匹配合法配方。

真正的引擎调用在 `server/adapters/recipes.py`。放在 `common/` 是因为
适配器与领域层都要用同一份形状，而这层形状是**纯数据**。

## 实测形状（2026-10-01，真机 `GetRecipesByResult`）

不是抄文档来的，是在运行中的游戏里逐条打出来的。两条必须记住的事实：

1. **配方标签是 `"crafting_table"`。** 这个值**不是从任何文档读来的，
   是量出来的**：第一次实现时按常识猜了 `"crafting"`（工作台就叫 crafting
   table，去掉 table 看着最自然），实测**返回空表**。空表看起来就像
   「这个产物没有配方」，于是「标签猜错了」会被读成「配方不存在」——
   这条错没有任何报错，只有一张永远空着的候选表。
   一并排除的候选还有 `shape`／`shaped`／`shapeless`／`workbench`／
   `furnace`／`stone_cutter`／`smithing_table`／`brewing_stand`／
   `campfire`：**全部返回空表**。`RECIPE_TAG` 就是这条的落点。
2. **有序与无序是两种字典，靠有没有 `pattern` 区分**：

   有序（`minecraft:golden_apple`）::

       {"pattern": ["AAA", "ABA", "AAA"],
        "key": {"A": {"item": "minecraft:gold_ingot"},
                "B": {"item": "minecraft:apple"}},
        "result": [{"item": "minecraft:golden_apple", "data": 0}]}

   无序（`minecraft:book`）::

       {"ingredients": [{"count": 3, "item": "minecraft:paper", "data": 0},
                        {"item": "minecraft:leather", "data": 0}],
        "result": [{"item": "minecraft:book", "data": 0}]}

其他实测细节：

- `count` **可以缺席**，缺席即 1（`minecraft:stick` 的空手配方就没有它）；
- `data` 同样可以缺席（`minecraft:oak_log`），实测出现时一律是 0；
- `key[sym]["item"]` 既可以是一个字符串，也可以是**一个列表**——
  列表是物品标签展开出来的候选（`minecraft:stick` 的 A 就是 12 种木板）；
- `result` **永远是列表**，实测到的配方都只有一项；
- 查不存在的物品返回 `[]`，不抛异常。

逐条证据见 `plans/tasks/11-交付说明.md` 第十九节。

## 摆位（`cells`）

匹配函数的第二个参数是一个 **9 元组**，下标 `行 * 3 + 列`，每项是
`(名字, aux, 数量)` 或 `None`（空位）。**带数量**是因为无序配方的一条可以要
3 个同种东西（`minecraft:book` 要 3 张纸），而工作台里那 3 张是堆在一个格子里的，
只看名字会让「只剩 1 张纸」的格子满足一条要 3 张的配方。
"""

import zlib

from .resource_key import canonical_tree, tree_text

try:  # pragma: no cover - 仅在 Python 2 下成立
    _TEXT_TYPES = (str, unicode)  # noqa: F821
    _UNICODE = unicode  # noqa: F821
    _INT_TYPES = (int, long)  # noqa: F821
except NameError:  # pragma: no cover - 仅在 Python 3 下成立
    _TEXT_TYPES = (str,)
    _UNICODE = str
    _INT_TYPES = (int,)

#: 实测出来的配方标签，见模块文档第 1 条。**不要改成 `"crafting"`。**
RECIPE_TAG = "crafting_table"

#: 工作台输入格是 3×3——**引擎事实**，与 `crafting/patterns.py` 的
#: `GRID_SLOT_COUNT` 同一个数。这里再写一份是因为 `common/` 不能 import
#: `server/`；两处都对应原版工作台的九格。
GRID_SIZE = 3
SLOT_COUNT = GRID_SIZE * GRID_SIZE

#: 有序配方里代表「这一格是空的」的符号（原版 pattern 用空格）。
EMPTY_SYMBOL = " "

# -- 错误码 ----------------------------------------------------------------

#: 既没有 `pattern` 也没有 `ingredients`，两种形状都不是。
BAD_SHAPE = "bad_shape"
#: `pattern` 是空的，或所有行都是空格。
EMPTY_PATTERN = "empty_pattern"
#: 摆位超过 3×3，工作台放不下。
PATTERN_TOO_BIG = "pattern_too_big"
#: `pattern` 里用了 `key` 没定义的符号。
UNKNOWN_SYMBOL = "unknown_symbol"
#: `key` 里某个符号没有任何可选物品。
EMPTY_SYMBOL_KEY = "empty_symbol_key"
#: 没有 `result`，或 `result` 里没有合法产物。
NO_RESULT = "no_result"


class RecipeError(ValueError):
    """规范化失败。带机器可读的 `code`，调用方按码判断，不解析文本。"""

    def __init__(self, code, detail=""):
        message = "%s: %s" % (code, detail) if detail else code
        ValueError.__init__(self, message)
        self.code = code
        self.detail = detail

    def to_dict(self):
        # type: () -> dict
        return {"code": self.code, "detail": self.detail}


class RecipePolicy(object):
    """未定／实测未穷尽处的显式口径。

    存在的理由与 `BandwidthPolicy` / `EnergyPolicy` 相同：**已实现不等于已定案**。
    这里放的两条都不是玩法定案，是「引擎给的数据在实测里没覆盖到的角落怎么读」，
    默认取**保守那一侧**——宁可少匹配一次（玩家看到「配方查得到但摆不进去」），
    也不要多匹配一次（悄悄吃掉一个不在配方里的物品）。
    """

    #: `key[sym]` 没有 `data` 字段时，是否把 aux 当通配。
    #:
    #: **默认 `False`（按 aux=0 严格比）**，依据是实测里凡是带 `data` 的一律是 0，
    #: 缺席的那些物品（`oak_log` 等）aux 本来就是 0，两种读法在实测样本上一致，
    #: 因此**没有样本能区分它们**。严格那侧只可能少匹配，不会误吃物品。
    #: 打开它（`True`）会让 `data` 缺席的条目匹配任意 aux。
    aux_wildcard_when_absent = False

    #: 同一次合成里，`result` 有多项时是否全部交付。
    #:
    #: 实测样本（stick / torch / crafting_table / golden_apple / book / wool）
    #: 全部只有一项，所以这一条**同样没有被实测穷尽**。默认 `True`：全交付，
    #: 因为少交付一件是静默丢物，而多交付一件至少是可见的。
    deliver_all_results = True

    def describe(self):
        # type: () -> dict
        return {
            "aux_wildcard_when_absent": self.aux_wildcard_when_absent,
            "deliver_all_results": self.deliver_all_results,
        }


DEFAULT_POLICY = RecipePolicy()


# -- 规范化 ----------------------------------------------------------------

def _text(value):
    # type: (object) -> str
    """引擎给的名字可能是 `unicode`，也可能是 `str`。统一成 `unicode`。

    py2 下 `u"a" == "a"` 成立，但把两者混进同一棵规范树会得到两个指纹，
    因此进哈希之前先统一。
    """
    if isinstance(value, _UNICODE):
        return value
    if isinstance(value, str):
        return value.decode("utf-8")
    return u"%s" % (value,)


def _count_of(entry, default=1):
    # type: (dict, int) -> int
    raw = entry.get("count", default)
    if isinstance(raw, bool) or not isinstance(raw, _INT_TYPES):
        raise RecipeError(BAD_SHAPE, "count 必须是整数，得到 %r" % (raw,))
    if raw <= 0:
        raise RecipeError(BAD_SHAPE, "count 必须为正，得到 %r" % (raw,))
    return raw


def _aux_of(entry, policy):
    # type: (dict, RecipePolicy) -> object
    """返回该条目要求的 aux；`None` 表示通配。"""
    if "data" not in entry:
        return None if policy.aux_wildcard_when_absent else 0
    data = entry["data"]
    if isinstance(data, bool) or not isinstance(data, _INT_TYPES):
        raise RecipeError(BAD_SHAPE, "data 必须是整数，得到 %r" % (data,))
    return data


class RecipeIngredient(object):
    """配方要的一种东西：名字 + aux + 数量。"""

    __slots__ = ("name", "aux", "count")

    def __init__(self, name, aux, count=1):
        self.name = _text(name)
        #: `None` = 通配任意 aux。
        self.aux = aux
        self.count = count

    def matches(self, name, aux):
        # type: (str, int) -> bool
        if name != self.name:
            return False
        if self.aux is None:
            return True
        return aux == self.aux

    def fingerprint(self):
        # type: () -> tuple
        return (self.name, self.aux, self.count)

    def to_dict(self):
        # type: () -> dict
        return {"name": self.name, "aux": self.aux, "count": self.count}

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> RecipeIngredient
        return cls(data["name"], data.get("aux"), data.get("count", 1))

    def __eq__(self, other):
        if not isinstance(other, RecipeIngredient):
            return NotImplemented
        return (self.name, self.aux, self.count) == (other.name, other.aux, other.count)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.name, self.aux, self.count))

    def __repr__(self):
        return "RecipeIngredient(%r, %r, %r)" % (self.name, self.aux, self.count)


class PresetRecipe(object):
    """一条规范化的预设配方。

    `ordered` 为真时用 `rows` + `symbols`（有序摆位）；为假时用 `ingredients`
    （无序，只看每种东西够不够）。两种都带 `outputs`。
    """

    __slots__ = ("ordered", "rows", "symbols", "ingredients", "outputs")

    def __init__(self, ordered, rows, symbols, ingredients, outputs):
        self.ordered = ordered
        #: 有序：每一行一个字符串，每个字符是一个符号或 `EMPTY_SYMBOL`。
        self.rows = rows
        #: 有序：`符号 -> (RecipeIngredient, ...)`，多项即标签展开的候选。
        self.symbols = symbols
        #: 无序：`(RecipeIngredient, ...)`。同名条目出现两次表示占两个格子。
        self.ingredients = ingredients
        #: `(RecipeIngredient, ...)`，一项一件产物。
        self.outputs = outputs

    # -- 形状查询 ------------------------------------------------------

    @property
    def height(self):
        return len(self.rows)

    @property
    def width(self):
        if not self.rows:
            return 0
        return max(len(row) for row in self.rows)

    def required_slots(self):
        # type: () -> int
        """有序配方占用的格子数（空格不算）。"""
        total = 0
        for row in self.rows:
            for char in row:
                if char != EMPTY_SYMBOL:
                    total += 1
        return total

    def slot_layout(self):
        # type: () -> tuple
        """给界面看的 9 格原料预览。

        返回 9 项，每项是 `RecipeIngredient` 或 `None`（该格不要求东西）。
        **按左上角对齐铺开**，不做居中——居中会让同一配方因为产物不同
        在界面上跳来跳去，而玩家看的是「我要摆哪几格」。
        """
        cells = [None] * SLOT_COUNT
        for row_index in range(len(self.rows)):
            row = self.rows[row_index]
            if row_index >= GRID_SIZE:
                break
            for col_index in range(len(row)):
                if col_index >= GRID_SIZE:
                    break
                char = row[col_index]
                if char == EMPTY_SYMBOL:
                    continue
                candidates = self.symbols[char]
                if not candidates:
                    continue
                cells[row_index * GRID_SIZE + col_index] = candidates[0]
        return tuple(cells)

    def output_preview(self):
        # type: () -> RecipeIngredient
        return self.outputs[0]

    # -- 指纹 ----------------------------------------------------------

    def fingerprint(self):
        # type: () -> tuple
        """内容指纹（不是线格式）。与 `crafting/patterns.py` 同样由内容派生。"""
        symbols = tuple(sorted(
            (symbol, tuple(item.fingerprint() for item in items))
            for symbol, items in self.symbols.items()
        ))
        return (
            "ordered" if self.ordered else "shapeless",
            self.rows,
            symbols,
            tuple(item.fingerprint() for item in self.ingredients),
            tuple(item.fingerprint() for item in self.outputs),
        )

    def digest(self):
        # type: () -> str
        """配方标识，客户端原样回传，用来指认「就是这一条配方」。

        取 `crc32` 而不是整段指纹文本：`minecraft:stick` 的指纹里带着
        12 种木板的完整候选表，**八百多个字符**——每条候选配方都要把它
        发给客户端一次，而客户端只是原样回传。8 个十六进制字符够用。

        **这不是安全边界。** 服务端收到回传后会重新查一遍该产物的候选表
        并按标识挑，挑不到就拒——因此伪造一个标识最多是「换一条同样能
        合成这个产物的配方」，而不是「执行任意配方」。同理，理论上存在
        的碰撞（同一产物的两条配方撞成同一个标识）后果也只是这个。

        用 `crc32` 是因为**实测 `hashlib` 在这个环境里没有 `md5`**
        （`'module' object has no attribute 'md5'`），而 `zlib` 有。
        """
        text = tree_text(canonical_tree(self.fingerprint())).encode("utf-8")
        return "%08x" % (zlib.crc32(text) & 0xffffffff)

    # -- 序列化 --------------------------------------------------------

    def to_dict(self):
        # type: () -> dict
        """给界面与线路用的紧凑形状。

        **不回传 `symbols` 的完整候选表**：那 12 种木板的列表对界面没用，
        玩家看到的是「这里要一块木板」。候选表只在服务端匹配时用。
        因此这个字典**不足以还原配方**（`from_dict` 会补一个单候选），
        它是**展示用的投影**，不是线格式的往返。
        """
        return {
            "ordered": self.ordered,
            "rows": list(self.rows),
            "ingredients": [item.to_dict() for item in self.ingredients],
            "slots": [
                None if cell is None else cell.to_dict()
                for cell in self.slot_layout()
            ],
            "outputs": [item.to_dict() for item in self.outputs],
            "digest": self.digest(),
        }

    def __repr__(self):
        return "PresetRecipe(ordered=%r, rows=%r, outputs=%r)" % (
            self.ordered, self.rows, self.outputs,
        )


def _ingredients_of(entries, policy, default_count=1):
    # type: (list, RecipePolicy, int) -> tuple
    if not isinstance(entries, list):
        raise RecipeError(BAD_SHAPE, "item 必须是字符串或列表，得到 %r" % (entries,))
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RecipeError(BAD_SHAPE, "候选条目必须是字典，得到 %r" % (entry,))
        name = entry.get("item")
        if not isinstance(name, _TEXT_TYPES):
            raise RecipeError(BAD_SHAPE, "候选缺少 item：%r" % (entry,))
        result.append(RecipeIngredient(
            name, _aux_of(entry, policy), _count_of(entry, default_count),
        ))
    return tuple(result)


def _symbol_alternatives(entry, policy):
    # type: (object, RecipePolicy) -> tuple
    """`key[sym]` 的一项，展开成候选表。

    实测两种写法：`{"item": "minecraft:oak_planks"}` 与
    `{"item": ["minecraft:acacia_planks", ...]}`（标签展开）。
    """
    if not isinstance(entry, dict):
        raise RecipeError(BAD_SHAPE, "key 的取值必须是字典，得到 %r" % (entry,))
    items = entry.get("item")
    if isinstance(items, _TEXT_TYPES):
        items = [entry]
    else:
        # 列表写法里每一项**只是一个字符串**（实测：`"item": ["minecraft:acacia_planks",
        # "minecraft:bamboo_planks", ...]`），`data` 才在外层。把外层 `data`
        # 并进每一项；`aux` 为 `None`（通配）时**不塞**——塞进去会得到一个
        # `data: None`，在 `_aux_of` 那里被判成「data 必须是整数」而拒掉整条配方。
        aux = _aux_of(entry, policy)
        merged = []
        for pair in items:
            if isinstance(pair, _TEXT_TYPES):
                pair = {"item": pair}
            if isinstance(pair, dict) and "data" not in pair and aux is not None:
                pair = dict(pair, data=aux)
            merged.append(pair)
        items = merged
    alternatives = _ingredients_of(items, policy)
    if not alternatives:
        raise RecipeError(EMPTY_SYMBOL_KEY, "符号没有任何可选物品")
    return alternatives


def _normalize_outputs(raw, policy):
    # type: (object, RecipePolicy) -> tuple
    if not isinstance(raw, list) or not raw:
        raise RecipeError(NO_RESULT, "result 必须是非空列表，得到 %r" % (raw,))
    outputs = _ingredients_of(raw, policy)
    if not policy.deliver_all_results:
        outputs = outputs[:1]
    return outputs


def normalize_recipe(raw, policy=DEFAULT_POLICY):
    # type: (object, RecipePolicy) -> PresetRecipe
    """把引擎的原始配方字典规范成 `PresetRecipe`。

    两条实测形状见模块文档。**不认识的一律抛 `RecipeError`，不猜、不补默认**：
    猜出来的配方会把一条「引擎加了新形状」静默读成「这条配方能合成」。
    """
    if not isinstance(raw, dict):
        raise RecipeError(BAD_SHAPE, "配方必须是字典，得到 %r" % (raw,))
    # 先判形状再判产物：`{"nonsense": true}` 两种都不像，该报的是
    # 「这压根不是配方」（BAD_SHAPE），而不是「它没有产物」——
    # 后者会把「引擎换了形状」读成「这条配方没有产物」。
    if "pattern" not in raw and "ingredients" not in raw:
        raise RecipeError(BAD_SHAPE, "既没有 pattern 也没有 ingredients：%r" % (
            sorted(raw.keys()),))

    outputs = _normalize_outputs(raw.get("result"), policy)
    if "pattern" in raw:
        return _normalize_ordered(raw, outputs, policy)
    ingredients = _ingredients_of(raw.get("ingredients"), policy)
    if not ingredients:
        raise RecipeError(BAD_SHAPE, "ingredients 不能为空")
    return PresetRecipe(False, (), {}, ingredients, outputs)


def _normalize_ordered(raw, outputs, policy):
    pattern = raw.get("pattern")
    if not isinstance(pattern, list) or not pattern:
        raise RecipeError(EMPTY_PATTERN, "pattern 必须是非空列表")
    rows = tuple(_text(row) for row in pattern)
    if len(rows) > GRID_SIZE:
        raise RecipeError(PATTERN_TOO_BIG, "配方有 %d 行，工作台只有 %d 行" % (
            len(rows), GRID_SIZE))
    for row in rows:
        if len(row) > GRID_SIZE:
            raise RecipeError(PATTERN_TOO_BIG, "某一行有 %d 列，工作台只有 %d 列" % (
                len(row), GRID_SIZE))

    key = raw.get("key")
    if not isinstance(key, dict):
        raise RecipeError(BAD_SHAPE, "有序配方缺少 key")
    symbols = {}
    for row in rows:
        for char in row:
            if char == EMPTY_SYMBOL or char in symbols:
                continue
            if char not in key:
                raise RecipeError(UNKNOWN_SYMBOL, "符号 %r 不在 key 里" % (char,))
            symbols[char] = _symbol_alternatives(key[char], policy)

    occupied = False
    for row in rows:
        if row.strip():
            occupied = True
            break
    if not occupied:
        raise RecipeError(EMPTY_PATTERN, "pattern 全是空格")
    return PresetRecipe(True, rows, symbols, (), outputs)


# -- 匹配 ------------------------------------------------------------------

class RecipeMatch(object):
    """一次匹配的结果：哪条配方、吃掉哪几格的几个。"""

    __slots__ = ("recipe", "consumed")

    def __init__(self, recipe, consumed):
        self.recipe = recipe
        #: `((槽位下标, 数量), ...)`，按槽位升序。
        self.consumed = consumed

    def slots(self):
        # type: () -> tuple
        return tuple(slot for slot, _count in self.consumed)

    def to_dict(self):
        # type: () -> dict
        return {
            "consumed": [{"slot": slot, "count": count} for slot, count in self.consumed],
        }


def _cell_matches(cell, ingredient):
    # type: (object, RecipeIngredient) -> bool
    """`cell` 是 `(name, aux, count)` 或 `None`。

    **数量要够**：无序配方的一条可以要 `count: 3`（`minecraft:book` 就是
    3 张纸），而工作台里那 3 张纸是**堆在一个格子里**的。所以「格子里的
    东西对」还不够，还得「这一格够数」——只看名字会让一个只剩 1 张纸的
    格子满足一条要 3 张的配方，预览亮着、点下去却做不出来。
    """
    if cell is None:
        return False
    name, aux, count = cell
    if not ingredient.matches(name, aux):
        return False
    return count >= ingredient.count


def match_ordered(recipe, cells):
    # type: (PresetRecipe, tuple) -> object
    """有序匹配：**允许整体平移**。

    原版工作台里把 2×2 的配方摆到右上角照样能合成——`pattern` 给的是
    最小包围盒，不是绝对坐标。因此这里枚举所有合法偏移。
    包围盒里凡是 `pattern` 标了空格的位置，摆位上也**必须是空的**；
    包围盒之外摆了什么一概不看（原版如此）。
    """
    height = recipe.height
    width = recipe.width
    if height > GRID_SIZE or width > GRID_SIZE:
        return None
    for offset_row in range(GRID_SIZE - height + 1):
        for offset_col in range(GRID_SIZE - width + 1):
            consumed = _try_offset(recipe, cells, offset_row, offset_col)
            if consumed is not None:
                return RecipeMatch(recipe, consumed)
    return None


def _try_offset(recipe, cells, offset_row, offset_col):
    # type: (PresetRecipe, tuple, int, int) -> object
    consumed = []
    for row_index in range(recipe.height):
        row = recipe.rows[row_index]
        for col_index in range(recipe.width):
            char = row[col_index] if col_index < len(row) else EMPTY_SYMBOL
            grid_slot = (offset_row + row_index) * GRID_SIZE + (offset_col + col_index)
            cell = cells[grid_slot]
            if char == EMPTY_SYMBOL:
                # 包围盒里的空位必须是真空位，否则等于「多摆了东西」。
                if cell is not None:
                    return None
                continue
            if not any(_cell_matches(cell, item) for item in recipe.symbols[char]):
                return None
            consumed.append((grid_slot, 1))
    consumed.sort()
    return tuple(consumed)


def match_shapeless(recipe, cells):
    # type: (PresetRecipe, tuple) -> object
    """无序匹配：只看「每种东西够不够」，不看位置。

    同名条目在 `ingredients` 里出现两次表示占**两个**格子，因此这里按条目
    逐条去要一格，不能先把同名条目合并（合并会把「2 个格子各 1 个」读成
    「1 个格子放 2 个」）。
    """
    needed = list(recipe.ingredients)
    taken = []
    used = set()
    for ingredient in needed:
        found = None
        for slot in range(SLOT_COUNT):
            if slot in used:
                continue
            if _cell_matches(cells[slot], ingredient):
                found = slot
                break
        if found is None:
            return None
        used.add(found)
        taken.append((found, ingredient.count))
    taken.sort()
    return RecipeMatch(recipe, tuple(taken))


def match(recipe, cells):
    # type: (PresetRecipe, tuple) -> object
    if recipe.ordered:
        return match_ordered(recipe, cells)
    return match_shapeless(recipe, cells)


def find_matches(recipes, cells):
    # type: (list, tuple) -> list
    """返回**全部**匹配到的配方，按传入顺序。

    返回全部而不是第一条：多条配方同时匹配时（实测里 `minecraft:white_wool`
    就有「四根线」与若干染色两条），「摆进去到底会做出哪一个」是一个
    需要被看见的事实，不是可以随便取一条的实现细节。
    """
    result = []
    for recipe in recipes:
        found = match(recipe, cells)
        if found is not None:
            result.append(found)
    return result


def find_match(recipes, cells):
    # type: (list, tuple) -> object
    """第一条匹配，没有则 `None`。预览槽用这个。"""
    for recipe in recipes:
        found = match(recipe, cells)
        if found is not None:
            return found
    return None
