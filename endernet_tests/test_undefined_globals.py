# coding=utf-8
"""静态守卫：业务代码里不许出现**读不到定义**的全局名。

## 为什么要有这一条

Python 的 `NameError` 只在**那一行真的执行到**的时候才抛。一个写错的全局名
因此可以一路通过语法检查、通过导入、通过全部离线用例，直到某个界面被打开
才炸——而它炸的是整块屏建不起来（`uiManager.CheckInit` → 屏的 `OnCreate`），
玩家看到的是「这个终端打不开了」，日志里才有一行 `NameError`。

2026-10-03 真机撞的正是这一条：`factoric_panel.py` 里写了
`NetworkClient(view_kind=VIEW_ORDER_KIND)`，而 `VIEW_ORDER_KIND` **全仓都
没有过**（同一个文件明明已经导入了 `VIEW_ORDER`）。工厂终端一打开就
`NameError`，而当时的离线用例一条都没照到它——因为那条路径要 `device_ref`
才走得进去。

## 它检查什么、不检查什么

**只查「模块级读得到吗」这一件事**：把每个文件里**任何位置**被赋值的名字
（含函数参数、`for` 目标、`except as`、`global` 声明、导入名）都算作「定义过」，
因此它**不会**报出「在函数 A 里定义、在函数 B 里用」这类跨作用域的错误——
那需要真正的静态分析，而本仓库的代价收益不划算。它抓的是**拼写级**的错：
名字压根不存在。

这套宽松是刻意的：**误报会把门禁变成噪声，而噪声会被绕过**。实测 103 个
文件零命中（唯一的候选是 `unichr`，py2 内建，已列入白名单）。

**不扫 `tooldelta/`**：那是 vendored 上游子模块（见 CLAUDE.md），风格与
可用名由上游决定，本仓库不改它、也不替它守门。
"""

import ast
import os
import unittest

import testsupport

#: **py2 有、py3 没有**的内建。守卫跑在 py3.11 上，而代码是给 py2.7 写的，
#: 不列出来的话 `unichr` / `unicode` 这类**正确的**写法会被报成未定义。
PY2_BUILTINS = frozenset([
    "basestring", "cmp", "execfile", "file", "long", "raw_input", "reduce",
    "reload", "unichr", "unicode", "xrange", "StandardError",
])

#: 解释器注入的名字。它们不是「定义过」，但也不是错。
DUNDERS = frozenset([
    "__file__", "__name__", "__doc__", "__package__", "__builtins__",
    "__spec__", "__loader__",
])


def _parse(path):
    # type: (str) -> object
    import io

    with io.open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read())


def module_names(tree):
    # type: (object) -> set
    """一棵语法树里**所有**被赋值过的名字，宽松到只漏不误报。"""
    defined = set(DUNDERS)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                defined.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, ast.Global):
            defined.update(node.names)
    return defined


def undefined_names(tree):
    # type: (object) -> list
    """`[(名字, 行号)]`，按行号排序。`tree` 见 `_parse`。"""
    known = module_names(tree)
    builtins_names = set(dir(__builtins__)) | set(PY2_BUILTINS)
    try:
        import builtins
        builtins_names |= set(dir(builtins))
    except ImportError:
        builtins_names |= set(dir(__builtin__))  # noqa: F821  (py2)
    known |= builtins_names
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in known:
                out.append((node.id, node.lineno))
    return sorted(set(out), key=lambda item: (item[1], item[0]))


def our_files():
    # type: () -> list
    """业务代码的全部 `.py`，**不含 vendored 的 `tooldelta/`**。"""
    root = testsupport.ENDERNET_ROOT
    out = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            if name.endswith(".py"):
                out.append(os.path.join(base, name))
    return sorted(out)


class UndefinedGlobalTests(unittest.TestCase):
    def test_the_scan_actually_finds_a_typo(self):
        """**反向自测**：守卫本身要能咬得住，否则它只是一段装饰。

        与 `test_import_boundaries.py` 的正反例同一个理由：一条永远为真的
        断言比没有断言更坏——它会让人以为这一类错已经被挡住了。
        """
        import io
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".py")
        try:
            with io.open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    u"# coding=utf-8\n\n\ndef f():\n    return SOME_TYPO_NAME\n"
                )
            hits = undefined_names(_parse(path))
            self.assertEqual([name for name, _line in hits], ["SOME_TYPO_NAME"])
        finally:
            os.close(fd)
            os.remove(path)

    def test_py2_builtins_are_not_reported(self):
        """`unichr` / `unicode` 这类 py2 内建**不能**被报成未定义。"""
        import io
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".py")
        try:
            with io.open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    u"# coding=utf-8\n\n\ndef f(text):\n"
                    u"    return unichr(65) + unicode(text) + long('1')\n"
                )
            self.assertEqual(undefined_names(_parse(path)), [])
        finally:
            os.close(fd)
            os.remove(path)

    def test_every_business_file_reads_only_defined_names(self):
        files = our_files()
        # 少扫了文件会让这条守卫悄悄失效（与 `source_files()` 同一条理由）。
        self.assertGreater(len(files), 90, "扫到的文件太少，守卫可能没走到")
        offenders = []
        for path in files:
            for name, line in undefined_names(_parse(path)):
                offenders.append("%s:%d  %s" % (path, line, name))
        self.assertEqual(
            offenders, [],
            "这些名字在任何作用域里都找不到定义，运行到那一行就是 NameError：\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
