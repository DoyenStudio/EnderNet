# coding=utf-8
"""包结构守卫：包名、包门面与导入图（2026-11 目录重组）。

`endernet/` 按领域分包、包 `__init__.py` 做短路径门面之后，有几类错误
**离线全绿、进游戏才炸**，这里静态拦住：

- **与 Python 2 标准库同名的模块/包。** 本仓不写
  `from __future__ import absolute_import`，py2 下同包里 `import io`
  会先拿到同目录的 `io.py`。
- **门面急加载成环。** 导入任何子模块之前，Python 先执行它各级祖先包的
  `__init__.py`；门面急加载的模块若绕回来导入这个包，py2 报
  `ImportError: cannot import name ...`，而且只在某种导入顺序下出现。
- **门面把引擎拖进纯逻辑。** 模块顶层需要 tooldelta / `mod.*` 的模块一旦
  进了门面，只想用纯模型的离线测试就跟着导入失败。
- **经自己或祖先包的门面取名字。** 门面执行到一半时名字还没绑定。

规则原文见 `.claude/09-repo-restructure-plan.md` §4；这里只守可机械判定的部分。
解析函数与引擎导入的判定复用 `test_import_boundaries`，不另写一套。
"""

import ast
import io
import os
import unittest

import testsupport
from test_import_boundaries import find_module_level_engine_imports, relative_path

SCRIPTS_ROOT = testsupport.SCRIPTS_ROOT
ENDERNET_ROOT = testsupport.ENDERNET_ROOT
REPO_ROOT = testsupport.REPO_ROOT

#: 层根包：只放 docstring，**不 import 任何东西**（它们一旦急加载，
#: 导入任何一个模块都会把整层拖进来）。
LAYER_ROOTS = ("endernet", "endernet.common", "endernet.server", "endernet.client")

#: 只做界面登记的包：`register_screens()` 在函数体里延迟导入各屏，
#: 模块顶层不许有 import（屏模块导入即注册，时机必须由它控制）。
REGISTRATION_PACKAGES = ("endernet.client.ui",)

#: Python 2.7 标准库的顶层模块名，外加引擎与 vendored 包名。
#: 本仓的模块/包一律不许与之同名（不分大小写，Windows 文件系统不分）。
PY2_TOP_LEVEL_NAMES = frozenset(name.lower() for name in """
abc aifc anydbm argparse array ast asynchat asyncore atexit audioop base64 bdb binascii binhex bisect bsddb
bz2 calendar cgi cgitb chunk cmath cmd code codecs codeop collections colorsys commands compileall compiler
contextlib cookielib copy copy_reg cpickle cprofile crypt csv ctypes curses datetime dbhash dbm decimal difflib
dircache dis distutils doctest dumbdbm dummy_thread dummy_threading email encodings ensurepip errno exceptions
fcntl filecmp fileinput fnmatch formatter fpformat fractions ftplib functools future_builtins gc genericpath
getopt getpass gettext glob grp gzip hashlib heapq hmac hotshot htmlentitydefs htmllib httplib idlelib ihooks
imaplib imghdr imp importlib imputil inspect io itertools json keyword lib2to3 linecache locale logging macpath
mailbox mailcap markupbase marshal math md5 mhlib mimetools mimetypes mimify mmap modulefinder msilib msvcrt
multifile multiprocessing mutex netrc new nntplib ntpath nturl2path numbers opcode operator optparse os os2emxpath
parser pdb pickle pickletools pipes pkgutil platform plistlib popen2 poplib posix posixfile posixpath pprint
profile pstats pty pwd py_compile pyclbr pydoc pydoc_data pyexpat queue quopri random re repr resource rexec
rfc822 rlcompleter robotparser runpy sched select sets sgmllib sha shelve shlex shutil signal site smtpd smtplib
sndhdr socket socketserver sqlite3 sre sre_compile sre_constants sre_parse ssl stat statvfs string stringio
cstringio stringold stringprep strop struct subprocess sunau symbol symtable sys sysconfig syslog tabnanny
tarfile telnetlib tempfile termios test textwrap thread threading time timeit tkinter token tokenize trace
traceback ttk tty turtle types unicodedata unittest urllib urllib2 urlparse user userdict userlist userstring
uu uuid warnings wave weakref webbrowser whichdb winsound wsgiref xdrlib xml xmllib xmlrpclib zipfile zipimport
zlib configparser htmlparser __builtin__ __future__ __main__
typing mod mod_log tooldelta
""".split())

#: 仓库内路径上限（相对仓库根）。现状与重组后的最长都是 81；留余量，
#: 但不许继续加深——游戏目录与发布目录比仓库更深（MAX_PATH = 260）。
MAX_RELATIVE_PATH = 90


def module_name(path):
    # type: (str) -> str
    """`.../endernet_scripts/endernet/a/b.py` → `endernet.a.b`；包取目录名。"""
    rel = os.path.relpath(path, SCRIPTS_ROOT).replace(os.sep, "/")[:-3]
    parts = rel.split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def endernet_files():
    # type: () -> list
    """`endernet/` 下全部 `.py`（不含 modMain 与 tooldelta）。"""
    return [p for p in testsupport.source_files() if os.path.abspath(p).startswith(ENDERNET_ROOT + os.sep)]


class _Tree(object):
    """一次性解析 `endernet/` 全树：模块、包与模块顶层导入边。"""

    def __init__(self):
        self.paths = {}  # 模块名 -> 文件路径
        self.packages = set()
        for path in endernet_files():
            name = module_name(path)
            self.paths[name] = path
            if os.path.basename(path) == "__init__.py":
                self.packages.add(name)
        self.trees = {}
        for name, path in self.paths.items():
            with io.open(path, "r", encoding="utf-8") as handle:
                self.trees[name] = ast.parse(handle.read(), filename=path)

    def package_of(self, name):
        # type: (str) -> str
        return name if name in self.packages else name.rsplit(".", 1)[0]

    def resolve(self, name, node):
        # type: (str, ast.ImportFrom) -> object
        """相对导入 → `(基准, [(目标模块, 是否子模块, 名字)])`；越出 `endernet` 的返回 None。"""
        if not node.level:
            return None
        parts = self.package_of(name).split(".")
        if node.level - 1 >= len(parts):
            return None  # 越出 endernet（tooldelta 等），不在本图里
        if node.level > 1:
            parts = parts[: len(parts) - (node.level - 1)]
        base = ".".join(parts + ([node.module] if node.module else []))
        targets = []
        for alias in node.names:
            child = base + "." + alias.name
            if child in self.paths:
                targets.append((child, True, alias.name))
            else:
                targets.append((base, False, alias.name))
        return base, targets

    def top_level_imports(self, name):
        """模块导入期会执行的 `ImportFrom`（函数体里的不算，类体里的算）。"""
        found = []

        def visit(nodes):
            for node in nodes:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue
                if isinstance(node, ast.ImportFrom):
                    found.append(node)
                visit(ast.iter_child_nodes(node))

        visit(self.trees[name].body)
        return found

    def edges(self):
        # type: () -> dict
        """增广导入图：A 顶层导入 B ⇒ A→B，且 A→B 的每个（A 不在其中的）祖先包。"""
        graph = {}
        for name in self.paths:
            out = graph.setdefault(name, set())
            for node in self.top_level_imports(name):
                resolved = self.resolve(name, node)
                if resolved is None:
                    continue
                for target, _is_child, _alias in resolved[1]:
                    if target not in self.paths:
                        continue
                    if target != name:
                        out.add(target)
                    parts = target.split(".")
                    for i in range(1, len(parts)):
                        ancestor = ".".join(parts[:i])
                        if ancestor == name or name.startswith(ancestor + "."):
                            continue
                        if ancestor in self.paths:
                            out.add(ancestor)
        return graph


def strongly_connected(graph):
    # type: (dict) -> list
    """Tarjan（迭代写法），返回大小 > 1 的强连通分量与自环。"""
    index = {}
    low = {}
    on_stack = set()
    stack = []
    result = []
    counter = [0]
    for root in sorted(graph):
        if root in index:
            continue
        work = [(root, iter(sorted(graph.get(root, ()))))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter[0]
                    counter[0] += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    item = stack.pop()
                    on_stack.discard(item)
                    component.append(item)
                    if item == node:
                        break
                if len(component) > 1 or node in graph.get(node, ()):
                    result.append(sorted(component))
    return result


class TestNames(unittest.TestCase):
    def test_no_module_or_package_shadows_the_py2_stdlib(self):
        offenders = []
        for path in endernet_files():
            name = module_name(path).rsplit(".", 1)[-1]
            if name.lower() in PY2_TOP_LEVEL_NAMES:
                offenders.append(relative_path(path))
        self.assertEqual(offenders, [], "与 py2 标准库/引擎同名，py2 隐式相对导入会遮蔽它：%r" % (offenders,))

    def test_paths_stay_short(self):
        offenders = []
        for path in endernet_files():
            rel = os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")
            if len(rel) > MAX_RELATIVE_PATH:
                offenders.append("%d %s" % (len(rel), rel))
        self.assertEqual(offenders, [], "路径过长（游戏目录更深，MAX_PATH = 260）：%r" % (offenders,))

    def test_the_stdlib_list_catches_the_known_case(self):
        """反向自测：名单真能咬住 `io`（重组前 `server/devices/io.py` 就是它）。"""
        self.assertIn("io", PY2_TOP_LEVEL_NAMES)
        self.assertIn("types", PY2_TOP_LEVEL_NAMES)
        self.assertIn("tooldelta", PY2_TOP_LEVEL_NAMES)


class TestPackageInits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = _Tree()

    def _children(self, package):
        prefix = package + "."
        modules = set()
        subpackages = set()
        for name in self.tree.paths:
            if name.startswith(prefix) and "." not in name[len(prefix):]:
                leaf = name[len(prefix):]
                (subpackages if name in self.tree.packages else modules).add(leaf)
        return modules, subpackages

    def test_layer_roots_import_nothing(self):
        offenders = []
        for package in LAYER_ROOTS:
            for node in ast.walk(self.tree.trees[package]):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    offenders.append("%s:%d" % (package, node.lineno))
        self.assertEqual(offenders, [], "层根包不许 import：%r" % (offenders,))

    def test_registration_packages_have_no_module_level_imports(self):
        offenders = []
        for package in REGISTRATION_PACKAGES:
            for node in self.tree.trees[package].body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    offenders.append("%s:%d" % (package, node.lineno))
        self.assertEqual(offenders, [], "登记包只能在函数体里导入：%r" % (offenders,))

    def test_facades_are_explicit(self):
        """门面只许 `from .<直接子模块> import 名字` 与 `__all__`，二者一致。"""
        problems = []
        for package in sorted(self.tree.packages):
            if package in LAYER_ROOTS or package in REGISTRATION_PACKAGES:
                continue
            modules, subpackages = self._children(package)
            imported = []
            declared = None
            body = self.tree.trees[package].body
            for index, node in enumerate(body):
                if index == 0 and isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant):
                    continue
                if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module in modules:
                    for alias in node.names:
                        if alias.name == "*":
                            problems.append("%s: 不许 import *" % package)
                        imported.append(alias.asname or alias.name)
                    continue
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "__all__"
                        and isinstance(node.value, (ast.List, ast.Tuple))):
                    declared = [getattr(item, "value", None) for item in node.value.elts]
                    continue
                problems.append("%s:%d 门面里只能有对直接子模块的 from-import 与 __all__" % (package, node.lineno))
            if imported and declared is None:
                problems.append("%s: 有导出却没有 __all__" % package)
            if declared is not None and sorted(declared) != sorted(imported):
                problems.append("%s: __all__ 与导入的名字不一致" % package)
            if len(set(imported)) != len(imported):
                problems.append("%s: 同一个名字导出了两次" % package)
            for name in imported:
                if name in modules or name in subpackages:
                    problems.append("%s: 导出名 %s 与子模块同名" % (package, name))
        self.assertEqual(problems, [], "\n  ".join([""] + problems))

    def test_facades_do_not_drag_the_engine_in(self):
        """门面急加载的模块（取导入闭包）在导入期都不需要引擎。"""
        graph = self.tree.edges()
        offenders = []
        for package in sorted(self.tree.packages):
            seen = set()
            todo = [package]
            while todo:
                name = todo.pop()
                if name in seen:
                    continue
                seen.add(name)
                todo.extend(graph.get(name, ()))
            for name in sorted(seen):
                path = self.tree.paths[name]
                if find_module_level_engine_imports(path, relative_path(path)):
                    offenders.append("%s ← %s" % (name, package))
        self.assertEqual(offenders, [], "门面会把引擎拖进来：%r" % (offenders,))

    def test_import_graph_with_package_inits_is_acyclic(self):
        cycles = strongly_connected(self.tree.edges())
        self.assertEqual(cycles, [], "导入期成环（含包初始化边）：%r" % (cycles,))

    def test_no_names_from_own_or_ancestor_facade(self):
        """`from . import X` / `from .. import X` 指向自己的祖先包时，X 只能是子模块。"""
        offenders = []
        for name in sorted(self.tree.paths):
            for node in ast.walk(self.tree.trees[name]):
                if not isinstance(node, ast.ImportFrom):
                    continue
                resolved = self.tree.resolve(name, node)
                if resolved is None:
                    continue
                base, targets = resolved
                if base not in self.tree.packages:
                    continue
                if not (name == base or name.startswith(base + ".")):
                    continue
                for _target, is_child, alias in targets:
                    if not is_child:
                        offenders.append("%s:%d 从自己的祖先包 %s 取名字 %s" % (name, node.lineno, base, alias))
        self.assertEqual(offenders, [], "\n  ".join([""] + offenders))


class TestGuardItselfWorks(unittest.TestCase):
    def test_cycle_detection_finds_a_two_node_cycle(self):
        graph = {"a": {"b"}, "b": {"a"}, "c": {"a"}}
        self.assertEqual(strongly_connected(graph), [["a", "b"]])

    def test_cycle_detection_accepts_a_dag(self):
        graph = {"a": {"b", "c"}, "b": {"c"}, "c": set()}
        self.assertEqual(strongly_connected(graph), [])

    def test_the_tree_is_not_empty(self):
        tree = _Tree()
        self.assertGreater(len(tree.paths), 100)
        self.assertIn("endernet.server.storage", tree.packages)


if __name__ == "__main__":
    unittest.main()
