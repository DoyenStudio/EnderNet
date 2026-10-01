# coding=utf-8
"""引擎边界上的字符串转换：**传 `str`，不要传 `unicode`。**

## 为什么单独一个模块

因为这是**一条跨接口的习惯，不是某一处的修补**：本仓的资源名到了适配器
这一层几乎全是 `unicode`（`ResourceKey.name` 构造时就 `decode("utf-8")`，
引擎读回来的物品名也是），而引擎的绑定是 C++ 转发，收哪种字符串**各接口
并不一致**。与其在每个适配器里各写一遍 `encode`，不如把「过边界时统一
转换」这件事收在一处，谁过边界谁调它。

## 实测到什么程度（2026-10-01/02，运行中的游戏）

**不要把它读成「引擎一律拒绝 unicode」——实测不是这样：**

| 接口 | 传 `unicode` | 结论 |
| --- | --- | --- |
| `GetRecipesByResult(name, "crafting_table", aux, -1)` | 返回 **`False`** | **实测会被拒**，且不抛异常 |
| `GetItemBasicInfo(name, aux)` | 正常返回，`maxStackSize` 对得上 | **实测能接受** |

因此：

- `GetRecipesByResult` 那条是**确证的**，症状是静默失败（`False` 而不是空表），
  下游只要写 `raw or ()` 就会被读成「没有配方」；
- `GetItemBasicInfo` 那条**量过，是不成立的**——所以这里写的是「过边界统一转」
  这条习惯，**不是**「所有引擎接口都拒 unicode」这条过度概括。把没量过的
  接口说成会拒，代价是下一次有人照着改一处本来没坏的代码。
- 其余引擎接口**没有逐个量过**。统一在这一层转换的代价接近零，因此照转；
  但**别把「转过了」当成「原样传就一定坏」的证据**。

## 与 `tooldelta` 的关系

`tooldelta` 的封装不替你做这件事，也**不该**在这里改它——它是 vendored 的
上游代码（见 `CLAUDE.md`）。转换在**本仓库这一侧**的调用点上做。
"""

try:  # pragma: no cover - 仅在 Python 2 下成立
    _UNICODE = unicode  # noqa: F821
    _BYTES = str
except NameError:  # pragma: no cover - 仅在 Python 3 下成立
    _UNICODE = str
    _BYTES = bytes


def engine_text(value):
    # type: (object) -> object
    """把参数转成引擎接受的那一种字符串。

    py2 下 `str` 就是字节串；已经是字节串的原样返回（不重复编码），
    是 `unicode` 的按 UTF-8 编码。非字符串转成文本再走同一条路。

    **返回类型在 py2 与 py3 下不同**（py2 是 `str`／字节串，py3 是 `bytes`），
    这是有意的：这个函数的唯一用途就是喂给引擎，而引擎只在 py2 下存在。
    离线测试断言的是**编码后的字节内容**，不是类型名。
    """
    if isinstance(value, _BYTES):
        return value
    if isinstance(value, _UNICODE):
        return value.encode("utf-8")
    return engine_text(u"%s" % (value,))


__all__ = ["engine_text"]
