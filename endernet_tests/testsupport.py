# coding=utf-8
"""离线测试的公共入口：把行为包脚本根加入 sys.path，并提供共用替身。

导入本模块即完成路径设置，测试模块只需 `import testsupport`。

刻意**不**导入 tooldelta：该包在模块级 import `mod.server.extraServerApi`，
脱离游戏会直接失败。领域层不得依赖它，测试也不应把它拉进来。
"""

import io
import os
import sys

#: 本文件所在目录（endernet_tests/）
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
#: 仓库根
REPO_ROOT = os.path.dirname(TESTS_DIR)
#: 行为包脚本根——与游戏内 import 根一致
SCRIPTS_ROOT = os.path.join(REPO_ROOT, "endernet_bp", "endernet_scripts")
#: 业务代码包目录
ENDERNET_ROOT = os.path.join(SCRIPTS_ROOT, "endernet")


def ensure_import_path():
    # type: () -> None
    """把行为包脚本根放到 sys.path 最前，使 `import endernet.x` 可用。"""
    if SCRIPTS_ROOT not in sys.path:
        sys.path.insert(0, SCRIPTS_ROOT)


ensure_import_path()


def source_files():
    # type: () -> list
    """列出业务代码的全部 .py 文件，供导入边界与静态检查使用。

    除 `endernet/` 包外，脚本根目录下的 `modMain.py` 与 `__init__.py`
    也属于本仓库的代码，必须一并纳入——漏掉它们会让静态守卫出现盲区。
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(ENDERNET_ROOT):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(".py"):
                out.append(os.path.join(dirpath, name))
    for name in ("modMain.py", "__init__.py"):
        path = os.path.join(SCRIPTS_ROOT, name)
        if os.path.isfile(path):
            out.append(path)
    return sorted(out)


#: 事件适配器模块，供下面几个静态检查解析
EVENTS_ADAPTER = os.path.join(
    ENDERNET_ROOT, "server", "adapters", "events.py"
)


def event_class_assignments():
    # type: () -> list
    """从适配器代码里读出它挂载的事件类赋值，返回 `[(属性名, 源类名)]`。

    **不维护第二份清单。** `ToolDeltaEventApi.__init__` 里
    `self.X = events_module.X` 的赋值就是唯一事实来源——
    再写一份类名清单迟早会和实际赋值对不上，而它在运行时根本用不到。

    两侧名字都返回，是因为它们各自会被改错：
    改左边，`bind()` 引用不到；改右边，取到的是不存在的类。
    只派生一侧的话，另一侧出错会一路溜到游戏里才炸。
    """
    import ast

    with io.open(EVENTS_ADAPTER, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=EVENTS_ADAPTER)

    pairs = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        # 只看 `self.X = events_module.Y` 这一种形态；
        # `self._bus = GetMCServerEventBus()` 不满足。
        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)):
            continue
        if target.value.id != "self":
            continue
        if not (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)):
            continue
        if value.value.id != "events_module":
            continue
        pairs.append((target.attr, value.attr))
    return pairs


def declared_event_class_names():
    # type: () -> list
    """适配器实际挂载的事件类名，按出现顺序。"""
    return [target for target, _ in event_class_assignments()]
