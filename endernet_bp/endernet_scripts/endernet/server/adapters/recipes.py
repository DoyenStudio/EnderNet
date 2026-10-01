# coding=utf-8
"""工作台预设配方的引擎适配（11-04）。

把 tooldelta 的 `GetRecipesByResult` 包成 `crafting/execution.py` 里那个
`PresetRecipeSource` 鸭子类型（`recipes_for` / `version`），供合成终端
「导入」与产物预览使用。

**引擎调用写在方法体里，不写在模块顶层。** 理由与 `adapters/` 其余模块相同：
这样本模块在脱离游戏时仍可导入，`EngineRecipeSource(fetch=...)` 也能在离线
测试里喂假数据。厂商层的 `tooldelta` 在模块级就 import 了
`mod.server.extraServerApi`，顶层引用会让整个测试目录报废。

## 实测（2026-10-01，运行中的游戏）

- 标签必须是 **`"crafting_table"`**。写成 `"crafting"`、`"shaped"`、
  `"workbench"`、`"furnace"` 等一律返回空表——空表与「这个产物确实没有
  配方」在调用方看来一模一样，会把「标签写错了」读成「配方不存在」。
  该常量落在 `common/crafting_recipe.py` 的 `RECIPE_TAG`。
- 查不存在的物品返回 `[]`，**不抛异常**，因此「查不到」与「没有」在这里
  是同一件事，都是空表。
- 原始字典的两种形状与全部字段细节见 `common/crafting_recipe.py` 的模块文档。
"""

from .engine_text import engine_text
from ...common.crafting_recipe import (
    DEFAULT_POLICY,
    RECIPE_TAG,
    RecipeError,
    normalize_recipe,
)

#: 一次查询最多要多少条。`-1` 在实测里就是不限。
MAX_RESULTS = -1

#: 规范化失败的原始配方留几条供诊断。留证的代价很小，而「有几条被跳过了」
#: 是排查「为什么这个产物查不到配方」时唯一有用的线索。
SKIPPED_MEMORY = 8


def _fetch_from_engine(name, tag, aux, max_results):
    # type: (str, str, int, int) -> list
    """真机上的取数路径。**只在被调用时才 import 引擎。**

    两个参数都过 `engine_text`：这条查询**实测会拒 `unicode`**
    （返回 `False`，不抛异常）。转换的理由与实测证据见 `engine_text`。
    """
    from ....tooldelta.api.server.world import GetRecipesByResult

    return GetRecipesByResult(
        engine_text(name), engine_text(tag), aux, max_results,
    )


class EngineRecipeSource(object):
    """引擎预设配方来源。

    `recipes_for(output_key)` 按**产物**查配方，返回规范化后的
    `PresetRecipe` 列表。**查不到就是空表**——那是引擎的回答，不是错误。

    单条配方规范化失败时**跳过它而不是抛**：一条脏配方不该让整个产物的
    配方表消失。但跳过会被记进 `skipped` 与 `stats`，**不静默**——静默跳过
    与「这条配方不存在」在界面上无法区分。
    """

    source_id = "engine.workbench"

    def __init__(self, fetch=None, policy=DEFAULT_POLICY, cache_size=64):
        self._fetch = _fetch_from_engine if fetch is None else fetch
        self.policy = policy
        #: 产物指纹 -> `(PresetRecipe, ...)`。按产物缓存：同一件物品在一次
        #: 会话里会被反复预览，而这条查询在真机上是引擎侧的字典查找。
        self._cache = {}
        self._cache_size = cache_size
        #: `(name, code, raw)`，最近 `SKIPPED_MEMORY` 条。
        self.skipped = []
        self.stats = {"calls": 0, "results": 0, "skipped": 0, "cache_hits": 0}

    # -- PresetRecipeSource 契约 --------------------------------------

    def recipes_for(self, output_key):
        # type: (object) -> list
        """按产物查预设配方。`output_key` 是 `ResourceKey`。"""
        cache_key = (output_key.name, output_key.aux)
        if cache_key in self._cache:
            self.stats["cache_hits"] += 1
            # 缓存里放元组（调用方改不动），**返回列表**——契约写的是
            # `recipes_for(output_key) -> list`，返回元组会让调用方那句
            # `== []` 悄悄变成假，而「查不到配方」正是最常见的分支。
            return list(self._cache[cache_key])

        self.stats["calls"] += 1
        raw = self._fetch(output_key.name, RECIPE_TAG, output_key.aux, MAX_RESULTS)
        # **不能把非列表读成「没有配方」。** 实测里引擎在参数类型不对时
        # 返回的是 `False`——写成 `raw or ()` 会让它变成一张空表，于是
        # 「这条查询整个坏了」与「这个产物确实没有配方」在界面上一模一样，
        # 玩家只会到处找配方。空表是引擎的回答，`False` 不是。
        if not isinstance(raw, (list, tuple)):
            raise TypeError(
                "配方查询返回了 %s，不是列表（产物 %r）"
                % (type(raw).__name__, output_key.name)
            )
        recipes = []
        for entry in raw:
            try:
                recipes.append(normalize_recipe(entry, self.policy))
            except RecipeError as error:
                self._remember_skip(output_key.name, error, entry)
        recipes = tuple(recipes)
        self.stats["results"] += len(recipes)
        self._remember(cache_key, recipes)
        return list(recipes)

    def version(self):
        # type: () -> str
        """数据来源标识。

        **不是内容版本**：引擎没给「配方表变没变」的号，这里只能报
        「查的是哪张表」。因此它适合放进诊断，不适合当缓存失效依据——
        缓存按会话活一次，见 `forget()`。
        """
        return "engine:%s" % (RECIPE_TAG,)

    # -- 缓存与诊断 ----------------------------------------------------

    def forget(self):
        # type: () -> None
        """丢掉缓存。会话关闭、设备被替换、版本换代时都该调一次。"""
        self._cache = {}

    def _remember(self, key, value):
        if len(self._cache) >= self._cache_size and key not in self._cache:
            # 不引 collections.OrderedDict：py2 下它存在，但这里只要
            # 「别无限涨」，丢掉重新查即可——引擎侧查询本来就是字典查找。
            self._cache.clear()
        self._cache[key] = value

    def _remember_skip(self, name, error, raw):
        self.stats["skipped"] += 1
        self.skipped.append((name, error.code, raw))
        if len(self.skipped) > SKIPPED_MEMORY:
            del self.skipped[0]

    def describe(self):
        # type: () -> dict
        report = dict(self.stats)
        report["source_id"] = self.source_id
        report["available"] = True
        report["version"] = self.version()
        report["cached"] = len(self._cache)
        #: 规范化失败的那几条。**留着原始字典**：没有它就只能知道
        #: 「有几条被跳过了」，看不出是引擎换了形状还是我们收错了。
        report["last_skipped"] = [
            {"name": name, "code": code} for name, code, _raw in self.skipped
        ]
        report["policy"] = self.policy.describe()
        return report
