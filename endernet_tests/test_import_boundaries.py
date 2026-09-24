# coding=utf-8
"""分层守卫：领域层不得依赖引擎。

这条规则是离线测试能存在的前提。一旦某个服务里出现
`from tooldelta.api.server.container import GetContainerItem`，
整个领域层就再也无法脱离游戏导入，本仓库的测试会集体失效——
而**这个错误在游戏内是看不出来的**，所以必须由测试挡住。

允许出现引擎导入的位置只有两处：
  - `server/adapters/`（引擎边界，职责就是封装引擎）
  - `server/bootstrap.py`（装配点，负责把适配器注入领域层）
  - `client/`（客户端同样需要引擎；其权威状态约束由 6.6 另行规定）
"""

import ast
import io
import os
import unittest

import testsupport

ENDERNET_ROOT = testsupport.ENDERNET_ROOT

#: 允许导入引擎的路径前缀（相对 endernet/ 包目录，使用 / 分隔）
ENGINE_ALLOWED_PREFIXES = (
    "server/adapters/",
    "client/",
)
ENGINE_ALLOWED_FILES = (
    "server/bootstrap.py",
)

#: 被视为「引擎依赖」的顶层模块名
ENGINE_MODULES = ("tooldelta", "mod")

#: 领域层目录，必须保持纯净
DOMAIN_PREFIXES = (
    "common/",
    "server/devices/",
    "server/network/",
    "server/runtime/",
    "server/persistence/",
    "server/sync/",
    "server/storage/",
    "server/crafting/",
)


def relative_path(path):
    return os.path.relpath(path, ENDERNET_ROOT).replace(os.sep, "/")


def is_engine_module(module_name):
    if not module_name:
        return False
    root = module_name.split(".", 1)[0]
    return root in ENGINE_MODULES


def find_engine_imports(path):
    """返回该文件中所有引擎导入的 (行号, 模块名)。"""
    # 必须显式指定 UTF-8：Windows 上 open() 默认用 GBK，源码里的中文注释会炸。
    with io.open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return []

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if is_engine_module(alias.name):
                    found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            # 相对导入（level > 0）不可能是外部引擎模块
            if node.level:
                continue
            if is_engine_module(node.module):
                found.append((node.lineno, node.module or ""))
    return found


class TestDomainLayerIsEngineFree(unittest.TestCase):
    def test_domain_files_do_not_import_engine(self):
        violations = []
        for path in testsupport.source_files():
            relative = relative_path(path)
            if not relative.startswith(DOMAIN_PREFIXES):
                continue
            if relative in ENGINE_ALLOWED_FILES:
                continue
            for lineno, module in find_engine_imports(path):
                violations.append("%s:%d 导入了 %s" % (relative, lineno, module))

        self.assertEqual(
            violations,
            [],
            "领域层出现了引擎依赖，离线测试会因此失效：\n  " + "\n  ".join(violations),
        )

    def test_adapters_are_the_only_engine_boundary(self):
        """反过来说：引擎导入必须集中在适配器里，不能散落在别处。"""
        offenders = []
        for path in testsupport.source_files():
            relative = relative_path(path)
            if relative.startswith(ENGINE_ALLOWED_PREFIXES):
                continue
            if relative in ENGINE_ALLOWED_FILES:
                continue
            if find_engine_imports(path):
                offenders.append(relative)
        self.assertEqual(offenders, [], "引擎导入出现在非边界位置：%r" % (offenders,))

    def test_domain_packages_exist(self):
        """守卫本身要有东西可守；目录改名后这里会失败。"""
        present = set()
        for path in testsupport.source_files():
            relative = relative_path(path)
            for prefix in DOMAIN_PREFIXES:
                if relative.startswith(prefix):
                    present.add(prefix)
        self.assertIn("common/", present)
        self.assertIn("server/devices/", present)
        self.assertIn("server/runtime/", present)

    def test_adapters_directory_is_present(self):
        self.assertTrue(
            os.path.isdir(os.path.join(ENDERNET_ROOT, "server", "adapters"))
        )


class TestGuardItselfWorks(unittest.TestCase):
    """守卫必须真的能抓到违规，否则它只是装饰。"""

    def test_detects_forbidden_import(self):
        sample = "from tooldelta.api.server.block import GetBlockName\n"
        tree = ast.parse(sample)
        modules = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and not node.level:
                modules.append(node.module)
        self.assertTrue(any(is_engine_module(m) for m in modules))

    def test_detects_plain_import(self):
        tree = ast.parse("import mod.server.extraServerApi\n")
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
        self.assertTrue(any(is_engine_module(n) for n in names))

    def test_relative_import_is_not_flagged(self):
        """相对导入是包内引用，不得误判为引擎依赖。"""
        tree = ast.parse("from ...common.identity import NodeKey\n")
        node = tree.body[0]
        self.assertTrue(node.level)
        self.assertFalse(is_engine_module(node.module))

    def test_similar_module_names_are_not_flagged(self):
        """`modelled` 之类的名字不应当被当成 `mod`。"""
        self.assertFalse(is_engine_module("modelled"))
        self.assertFalse(is_engine_module("tooldelta_extra"))
        self.assertTrue(is_engine_module("mod.server"))
        self.assertTrue(is_engine_module("tooldelta"))


class TestToolDeltaMustBeImportedRelatively(unittest.TestCase):
    """`tooldelta` 必须用相对导入——它就在行为包里，顶层没有这个包。

    这一条是真机上撞出来的：适配器原来写 `from tooldelta.api.server.block import ...`，
    离线测试全过（它们从不导入适配器），进游戏后 `OnServerInited` 直接
    `ImportError: No module named tooldelta.api.server.block`，
    模组静默没有启动。脚本目录被引擎当作包挂载，完整名字是
    `endernet_scripts.tooldelta`，所以任何绝对 `tooldelta.*` 都必然失败。

    离线无法导入真引擎，但**静态扫描绝对导入**就能挡住这类错误。
    `mod.*` 是引擎提供的顶层包，保持绝对导入才是对的，因此不在本条规则内。
    """

    #: 该顶层包必须相对导入
    NESTED_MODULE = "tooldelta"

    @staticmethod
    def _required_level(relative):
        """从该文件回到 `endernet_scripts` 需要几级相对导入。

        脚本目录被引擎当作包挂载，因此完整模块名是
        `endernet_scripts.endernet.<子包...>.<模块>`。
        文件在 `endernet/` 下的目录层数每多一层，点号就要多一个。
        """
        directory = os.path.dirname(relative)
        if not directory:
            return 2  # endernet 包根下的模块
        # 目录层数 + 1（回到 endernet）+ 1（回到 endernet_scripts）
        return len(directory.split("/")) + 2

    def _offenders(self):
        out = []
        for path in testsupport.source_files():
            relative = relative_path(path)
            # 只查本模组自己的代码；tooldelta 自身的内部实现不归我们管
            if not relative.startswith(("common/", "server/", "client/")):
                continue
            required = self._required_level(relative)
            with io.open(path, "r", encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                module = node.module or ""
                if not (
                    module == self.NESTED_MODULE
                    or module.startswith(self.NESTED_MODULE + ".")
                ):
                    continue
                if node.level == 0:
                    out.append(
                        "%s:%d 绝对导入了 %s（应为 %d 级相对导入）"
                        % (relative, node.lineno, module, required)
                    )
                elif node.level != required:
                    out.append(
                        "%s:%d 相对层级为 %d，但该文件需要 %d 级"
                        "（%s）——层级过大会触发"
                        " 'Attempted relative import beyond toplevel package'"
                        % (relative, node.lineno, node.level, required, module)
                    )
        return out

    def test_no_absolute_tooldelta_imports(self):
        offenders = self._offenders()
        self.assertEqual(
            offenders,
            [],
            "tooldelta 位于行为包内，必须用**层级正确**的相对导入；"
            "以下写法在真机上会 ImportError：\n  " + "\n  ".join(offenders),
        )

    def test_required_level_formula(self):
        """层级公式本身要正确，否则守卫会给出错误的期望值。"""
        self.assertEqual(self._required_level("common/resource_key.py"), 3)
        self.assertEqual(self._required_level("server/bootstrap.py"), 3)
        self.assertEqual(self._required_level("server/adapters/world.py"), 4)
        self.assertEqual(self._required_level("server/network/node.py"), 4)
        self.assertEqual(self._required_level("client/bootstrap.py"), 3)

    def test_detects_one_too_many_dots(self):
        """多一个点同样会炸，且报错信息极具误导性（这回真机上遇到的就是它）。"""
        relative = "server/bootstrap.py"
        self.assertNotEqual(self._required_level(relative), 4)
        tree = ast.parse("from ....tooldelta.events import serverapi\n")
        level = tree.body[0].level
        self.assertNotEqual(level, self._required_level(relative))

    def test_detects_the_real_regression(self):
        """守卫本身要能抓到这次真机上的那种写法。"""
        tree = ast.parse("from tooldelta.api.server.block import GetBlockName\n")
        node = tree.body[0]
        self.assertEqual(node.level, 0)
        self.assertTrue(node.module.startswith("tooldelta"))

    def test_relative_tooldelta_form_is_accepted(self):
        tree = ast.parse("from ....tooldelta.api.server.block import X\n")
        node = tree.body[0]
        self.assertTrue(node.level)

    def test_engine_package_mod_stays_absolute(self):
        """`mod` 是引擎顶层包，绝对导入是唯一正确写法，不得被误判。"""
        tree = ast.parse("import mod.server.extraServerApi as serverApi\n")
        node = tree.body[0]
        self.assertEqual(node.names[0].name.split(".")[0], "mod")


class TestEventClassMapping(unittest.TestCase):
    """适配器引用的事件类必须与 tooldelta 当前提供的对得上。

    适配器是全项目唯一 import `tooldelta.events` 的地方，离线跑不到它，
    因此上游改名、或者类名拼错，都只有在游戏里才会炸。
    这几条静态检查把该风险搬到离线：
    「已声明」的类名直接从 `ToolDeltaEventApi.__init__` 的赋值里读，
    不另维护一份清单——两份清单迟早会对不上。
    """

    TOOLDELTA_EVENTS = os.path.join(
        testsupport.SCRIPTS_ROOT, "tooldelta", "events"
    )

    @staticmethod
    def _declared_classes():
        """收集 tooldelta.events 下全部类名。"""
        names = set()
        for dirpath, dirnames, filenames in os.walk(TestEventClassMapping.TOOLDELTA_EVENTS):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                with io.open(path, "r", encoding="utf-8") as handle:
                    tree = ast.parse(handle.read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef):
                        names.add(node.name)
        return names

    @staticmethod
    def _classes_used_in_bind():
        """`bind()` 里用到的所有 `api.<事件类名>`。

        `bind()` 现在直接引用事件类本身（`api.ChunkLoadedServerEvent`），
        不再用短槽位别名，因此这里收集到的应当是 tooldelta 的类名。
        """
        from endernet.server.adapters import events as adapter

        path = adapter.__file__
        if path.endswith(".pyc"):
            path = path[:-1]
        with io.open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)

        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "api":
                    used.add(node.attr)
        return used

    def test_declared_classes_still_exist_in_tooldelta(self):
        declared = self._declared_classes()
        self.assertTrue(declared, "没有解析到 tooldelta.events 的类，检查路径")

        missing = [
            name
            for name in testsupport.declared_event_class_names()
            if name not in declared
        ]
        self.assertEqual(
            missing, [], "tooldelta 里找不到这些事件类（可能已改名）：%r" % (missing,)
        )

    def test_assignment_target_matches_source(self):
        """`self.X = events_module.X` 两侧名字必须一致。

        只派生目标名的话，把右侧改成不存在的类（上游改名）**不会被发现**——
        离线不构造真实 API，而 `bind()` 引用的又是目标名，
        错误会一路溜到游戏里才炸。这条专门堵这个口子。
        """
        mismatched = [
            (target, source)
            for target, source in testsupport.event_class_assignments()
            if target != source
        ]
        self.assertEqual(
            mismatched,
            [],
            "事件类赋值两侧名字不一致（左侧或右侧被改错）：%r" % (mismatched,),
        )

    def test_bind_uses_only_declared_classes(self):
        declared = set(testsupport.declared_event_class_names())
        used = self._classes_used_in_bind()
        # `require_api` / `register` 之类的方法也在 api 上调用，排除掉方法名
        method_names = {"listen", "remove", "require_api"}
        unknown = used - declared - method_names
        self.assertEqual(
            unknown, set(),
            "bind() 用到了适配器没有挂载的事件类（拼写错误？）：%r"
            % (unknown,),
        )

    def test_declared_classes_are_all_used(self):
        """反向检查：挂了却没人用，说明赋值和 bind() 脱节了。"""
        declared = set(testsupport.declared_event_class_names())
        used = self._classes_used_in_bind()
        self.assertEqual(
            declared - used, set(), "有事件类声明了但 bind() 从未注册"
        )

    def test_declared_class_names_are_unique(self):
        names = list(testsupport.declared_event_class_names())
        self.assertEqual(len(names), len(set(names)))

    def test_project_uses_only_the_mc_event_bus(self):
        """本项目统一走 `GetMCServerEventBus`，自定义事件也不例外。

        `EventBus` 注册时用的是 `event.GetNamespace() or self.namespace`——
        **事件类自带的命名空间优先**，总线自身的只是回退值。
        自定义事件（`CustomC2SEvent` 之类）的 `GetNamespace()` 返回本模组名字，
        所以挂在 MC 总线上同样注册到正确位置，不需要第二条总线。

        这条检查把该约定固定下来，避免后续加自定义事件时又分出 Mod 总线。
        只在代码里查（AST 的 Name/Attribute），文档字符串里提到名字不算。
        """
        forbidden = ("GetModServerEventBus", "GetModClientEventBus")
        offenders = []
        for path in testsupport.source_files():
            relative = relative_path(path)
            if not relative.endswith(".py"):
                continue
            with io.open(path, "r", encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), filename=path)
            for node in ast.walk(tree):
                name = None
                if isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                if name in forbidden:
                    offenders.append("%s:%d %s" % (relative, node.lineno, name))
        self.assertEqual(
            offenders,
            [],
            "本项目约定原版与自定义事件全部走 GetMCServerEventBus：\n  "
            + "\n  ".join(offenders),
        )

    def test_no_module_level_engine_import_in_adapter(self):
        """适配器的 tooldelta 导入必须留在方法体内，模块顶层 import 会毁掉离线测试。"""
        from endernet.server.adapters import events as adapter

        path = adapter.__file__
        if path.endswith(".pyc"):
            path = path[:-1]
        with io.open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)

        offenders = []
        for node in tree.body:  # 只看模块顶层
            if isinstance(node, ast.ImportFrom) and node.module == "tooldelta":
                offenders.append((node.lineno, node.module))
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "tooldelta":
                        offenders.append((node.lineno, alias.name))
        self.assertEqual(offenders, [], "适配器顶层出现了 tooldelta 导入：%r" % (offenders,))


class TestSourceFilesAreFound(unittest.TestCase):
    def test_source_files_are_discovered(self):
        files = testsupport.source_files()
        self.assertGreater(len(files), 10, "没有发现源文件，守卫形同虚设")

    def test_mod_main_is_included(self):
        names = [os.path.basename(p) for p in testsupport.source_files()]
        self.assertIn("modMain.py", names)


if __name__ == "__main__":
    unittest.main()
