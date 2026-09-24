# coding=utf-8
"""扫描 EnderNet 源码，拒绝 Python 3 专有语法与 py2/py3 行为差异。

游戏运行时是网易 ModSDK 的 Python 2.7，本机却只有 Python 3.11，
因此离线测试必须在 3.11 上导入同一份源码。本脚本是两者之间的语法门禁。

除了语法，它还兜住两类**静默**行为差异（因为项目约定不写 `__future__`，
这两条失去了未来导入的保护，必须由检查顶上）：

- PY2022：未保护的 `/` 除法——py2 对两个整数执行整除，`4 / 3` 得 1 而非 1.333；
- PY2021：多参数 `print`——py2 会打印成元组。

**仍不在检查范围内**：dict 视图对象、`str`/`unicode` 语义、异常链、排序稳定性。
「在真实 py2.7 上运行」依旧是未验证项，不能凭本脚本通过就说已经兼容。
"""

import ast
import io
import os
import sys


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_ROOT = os.path.join(REPO_ROOT, "endernet_bp", "endernet_scripts")

#: 需要检查的源码目录（递归）
SOURCE_DIRS = (
    os.path.join(SCRIPTS_ROOT, "endernet"),
)

#: 需要检查的单个源码文件
#:
#: `__init__.py` 也要查：它是脚本目录的包标记，同样是本仓库的代码。
#: 早先只查 `modMain.py`，于是它被漏掉——移除 `__future__` 时整仓库
#: 都清干净了，唯独这一行留下（连带的核对脚本用了同一份目录清单，也没发现）。
SOURCE_FILES = (
    os.path.join(SCRIPTS_ROOT, "modMain.py"),
    os.path.join(SCRIPTS_ROOT, "__init__.py"),
)

#: 项目约定：源文件不使用 `__future__`。
#:
#: 早先每个文件都写 `from __future__ import absolute_import, division, print_function`，
#: 现在改为不写，代价是那三项保护必须由别的手段顶上——
#: 前两项（absolute_import、division）在本项目里本来就是多余的：
#: 包内一律用显式相对导入；容量以整数微单位计算，整除使用 `//`。
#: 但这两条一旦破了就会静默出错，所以下面各有一条检查兜着。
NO_FUTURE_MODULE = "__future__"


def _node_names(*names):
    # type: (*str) -> tuple
    """把节点名解析成 ast 里的类，缺失的（旧/新版本）忽略。"""
    out = []
    for name in names:
        node_cls = getattr(ast, name, None)
        if node_cls is not None:
            out.append(node_cls)
    return tuple(out)


class Py2SyntaxVisitor(ast.NodeVisitor):
    """收集所有 Python 3 专有语法。"""

    def __init__(self, filename):
        self.filename = filename
        self.violations = []
        # type: list

    # -- 上报 --------------------------------------------------------------

    def report(self, node, code, message):
        line = getattr(node, "lineno", 0)
        self.violations.append((self.filename, line, code, message))

    # -- 通用分派 ----------------------------------------------------------

    def generic_visit(self, node):
        self._check_node(node)
        ast.NodeVisitor.generic_visit(self, node)

    def visit_ImportFrom(self, node):
        if node.module == NO_FUTURE_MODULE:
            self.report(
                node,
                "PY2019",
                "项目约定不使用 __future__；它提供的三项保护已由 "
                "PY2021（多参数 print）、PY2022（未保护的除法）"
                "与显式相对导入分别顶上",
            )
        self.generic_visit(node)

    def _check_node(self, node):
        type_name = type(node).__name__

        if isinstance(node, _node_names("JoinedStr", "FormattedValue")):
            self.report(node, "PY2001", "f-string 是 Python 3.6+ 语法，py2.7 无此语法")
        elif isinstance(node, _node_names("Nonlocal")):
            self.report(node, "PY2002", "nonlocal 是 Python 3 语法")
        elif isinstance(
            node,
            _node_names("AsyncFunctionDef", "AsyncFor", "AsyncWith", "Await"),
        ):
            self.report(node, "PY2003", "async/await 是 Python 3.5+ 语法")
        elif isinstance(node, _node_names("AnnAssign")):
            self.report(node, "PY2004", "变量注解是 Python 3.6+ 语法，请改用 # type: 注释")
        elif isinstance(node, _node_names("YieldFrom")):
            self.report(node, "PY2005", "yield from 是 Python 3.3+ 语法")
        elif isinstance(node, _node_names("MatMult")):
            self.report(node, "PY2006", "矩阵乘法运算符 @ 是 Python 3.5+ 语法")
        elif isinstance(node, _node_names("NamedExpr")):
            self.report(node, "PY2007", "海象运算符 := 是 Python 3.8+ 语法")
        elif isinstance(node, _node_names("MatchValue", "MatchAs", "MatchClass")):
            self.report(node, "PY2008", "match 语句是 Python 3.10+ 语法")

        # raise ... from ...
        if isinstance(node, ast.Raise) and getattr(node, "cause", None) is not None:
            self.report(node, "PY2009", "raise ... from ... 是 Python 3 语法")

        # 函数签名：注解与仅关键字/仅位置参数
        if isinstance(node, _node_names("FunctionDef", "AsyncFunctionDef")):
            self._check_signature(node)

        # 调用：仅限 py3 的解包形式
        if isinstance(node, ast.Call):
            self._check_call(node)

        # 字面量与赋值：{**a}、x, *y = ...
        if isinstance(node, ast.Dict):
            if any(k is None for k in node.keys):
                self.report(node, "PY2010", "字典字面量解包 {**a} 是 Python 3.5+ 语法")

        if isinstance(node, _node_names("Starred")):
            if isinstance(node.ctx, ast.Store):
                self.report(node, "PY2011", "赋值目标解包 *x 是 Python 3 语法")

        # --- 不使用 __future__ 之后必须自己兜住的两件事 ---
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            self._check_division(node)
        if isinstance(node, ast.Call):
            self._check_print(node)

    @staticmethod
    def _is_floaty(operand):
        """该操作数是否明显不是整数。

        识别：浮点字面量、`float(...)` / `Fraction(...)` 调用、它们的取负，
        以及**含浮点的乘积**（`1.0 * n` 这种把整数拉成浮点的惯用写法）。
        """
        if isinstance(operand, _node_names("Constant")):
            return isinstance(getattr(operand, "value", None), float)
        if isinstance(operand, _node_names("UnaryOp")):
            return Py2SyntaxVisitor._is_floaty(operand.operand)
        if isinstance(operand, ast.BinOp) and isinstance(operand.op, ast.Mult):
            return Py2SyntaxVisitor._is_floaty(
                operand.left
            ) or Py2SyntaxVisitor._is_floaty(operand.right)
        if isinstance(operand, ast.Call):
            func = operand.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            return name in ("float", "Fraction")
        return False

    def _check_division(self, node):
        """`/` 必须是安全的除法。

        没有 `from __future__ import division` 之后，py2 的 `/` 对两个整数
        是**整除**：`4 / 3 == 1`，而 py3 得到 1.333。这类差异不会报错，
        只会让数值悄悄变小，所以宁可要求写清楚：

        - 要整除就用 `//`（显式）
        - 要真除法就让一边明显是浮点：`float(n) / m`、`1.0 * n / m`、`Fraction(a, b)`
        """
        if self._is_floaty(node.left) or self._is_floaty(node.right):
            return
        self.report(
            node,
            "PY2022",
            "未保护的 `/` 除法：没有 division 未来导入时，py2 对两个整数执行整除"
            "（4/3 == 1）。要整除写 `//`，要真除法让一边是 float(...)/Fraction(...) 或浮点字面量",
        )

    def _check_print(self, node):
        """`print` 必须是单参数形式。

        没有 `print_function` 之后，py2 里 `print(a, b)` 打印的是元组 `(a, b)`，
        py3 打印 `a b`；带关键字参数（`print(x, file=f)`）在 py2 直接是语法错误。
        """
        if not (isinstance(node.func, ast.Name) and node.func.id == "print"):
            return
        if len(node.args) > 1:
            self.report(
                node,
                "PY2021",
                "多参数 print：py2 会打印成元组，请改用 `print(\"...\" %% (a, b))`",
            )
        elif node.keywords:
            self.report(
                node, "PY2021", "print 带关键字参数在 py2 无 print_function 时是语法错误"
            )

    def _check_signature(self, node):
        args = node.args
        if getattr(node, "returns", None) is not None:
            self.report(node, "PY2012", "函数返回注解是 Python 3 语法，请改用 # type: 注释")

        all_args = list(getattr(args, "posonlyargs", [])) + list(args.args)
        for arg in all_args:
            if getattr(arg, "annotation", None) is not None:
                self.report(
                    node, "PY2013", "参数注解是 Python 3 语法，请改用 # type: 注释"
                )
                break
        for arg in list(args.kwonlyargs):
            self.report(node, "PY2014", "仅关键字参数（* 之后）是 Python 3 语法")

        if getattr(args, "posonlyargs", None):
            self.report(node, "PY2015", "仅位置参数（/ 之前）是 Python 3.8+ 语法")

    def _check_call(self, node):
        star_count = sum(1 for a in node.args if isinstance(a, _node_names("Starred")))
        extra_star = star_count - 1 if star_count else 0
        if extra_star > 0:
            self.report(node, "PY2016", "调用中出现多个 * 解包是 Python 3.5+ 语法")

        keyword_stars = [k for k in node.keywords if k.arg is None]
        if len(keyword_stars) > 1:
            self.report(node, "PY2017", "调用中出现多个 ** 解包是 Python 3.5+ 语法")

        # super() 无参形式依赖 Python 3 的 __class__ 单元格
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "super"
            and not node.args
            and not node.keywords
        ):
            self.report(
                node, "PY2018", "无参 super() 在 py2 不工作，请写 super(Cls, self)"
            )

def check_source(source, filename):
    # type: (str, str) -> list
    """检查一段源码，返回违规列表 [(filename, line, code, message)]。"""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return [(filename, exc.lineno or 0, "PY2999", "无法解析: %s" % exc.msg)]

    visitor = Py2SyntaxVisitor(filename)
    visitor.visit(tree)
    return visitor.violations


def iter_source_files():
    # type: () -> list
    """列出所有需要检查的 .py 文件。"""
    out = []
    for path in SOURCE_FILES:
        if os.path.isfile(path):
            out.append(path)
    for root_dir in SOURCE_DIRS:
        if not os.path.isdir(root_dir):
            continue
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
            for name in sorted(filenames):
                if name.endswith(".py"):
                    out.append(os.path.join(dirpath, name))
    return sorted(set(out))


def check_paths(paths):
    # type: (list) -> list
    """检查给定文件列表，返回全部违规。"""
    violations = []
    for path in paths:
        with io.open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        violations.extend(check_source(source, path))
    return violations


def _relative(path):
    try:
        return os.path.relpath(path, REPO_ROOT)
    except ValueError:
        return path


def use_utf8_stdout():
    # type: () -> None
    """Windows 控制台默认 GBK，中文诊断信息会变成乱码。强制 UTF-8 输出。

    unittest 的失败信息走 stderr，因此两个流都要处理。
    errors='replace' 保证即使外层仍是 GBK 也只损失个别字符，不会抛
    UnicodeEncodeError 把整个检查打断。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, IOError):
            pass


def main(argv=None):
    # type: (list) -> int
    use_utf8_stdout()
    argv = sys.argv[1:] if argv is None else argv
    paths = [os.path.abspath(p) for p in argv] if argv else iter_source_files()

    if not paths:
        print("py2check: 没有找到任何源文件（源码目录尚未建立？）")
        return 0

    violations = check_paths(paths)
    if not violations:
        print("py2check: %d 个文件全部通过 Python 2.7 语法门禁" % len(paths))
        return 0

    for filename, line, code, message in sorted(violations):
        print("%s:%s: %s: %s" % (_relative(filename), line, code, message))
    print("py2check: %d 个文件中有 %d 处违规" % (len(paths), len(violations)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
