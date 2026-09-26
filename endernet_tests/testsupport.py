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


class StorageStand(object):
    """一套最小的驱动器 + 存储服务，供需要**真实库存**的用例复用。

    **不复制业务逻辑**：`place`/`seed` 走的是与运行时同一条入口，因此
    「库存有多少」「哪些键变脏」都是被测代码算出来的，不是替身编的。
    用假存储替身来测同步层，测的会是替身的语义而不是仓储的语义。

    只做搭台，不做断言：方法返回结果，由调用方决定怎么断言。
    """

    #: 小型单面磁带 4 单位物/s。夹具取 1 帧/秒、桶容量 1 秒 ⇒ 每帧 4 件。
    tape_quota = 4

    def __init__(self, definitions=None, frames_per_second=1, medium_policy=None):
        from endernet.common.definitions import DefinitionSet
        from endernet.server.devices.registry import DeviceRegistry
        from endernet.server.devices.storage_driver import DriverPolicy, StorageDriverStore
        from endernet.server.storage.medium import MediumPolicy
        from endernet.server.storage.service import StorageService

        self.definitions = definitions if definitions is not None else DefinitionSet()
        self.registry = DeviceRegistry(self.definitions)
        #: 负载变化通知的落点（挂卸盘会让 `disk_count` 变，需要重算负载）。
        self.loads = []
        self.medium_policy = (
            medium_policy
            if medium_policy is not None
            else MediumPolicy(frames_per_second=frames_per_second)
        )
        self.driver_policy = DriverPolicy()
        self.store = StorageDriverStore(
            self.definitions,
            self.registry,
            policy=self.driver_policy,
            medium_policy=self.medium_policy,
            on_load_changed=self.loads.append,
        )
        self.service = StorageService(drivers=self.store, policy=self.medium_policy)
        #: `seed()` 下一次要用的帧号。**由它自己往上走**，因为额度是按帧
        #: 发的：连着 `seed` 两块不同的物品却都记在同一帧上，第二次就会被
        #: 额度拒掉——而用例想表达的是「库存有这些」，不是「额度有多大」。
        self.next_tick = 10
        #: 最近一次 `place()` 的设备状态与网络节点。
        self.state = None
        self.node = None
        self.record = None
        self.medium = None

    def place(self, x, y=64, z=0, dimension=0, device_id=None):
        # type: (int, int, int, int, str) -> object
        """放置一台驱动器，走与运行时同一条生命周期入口。"""
        from endernet.common.identity import NodeKey
        from endernet.server.devices.storage_driver import DRIVER_DEVICE_ID
        from endernet.server.network.node import NetworkNode
        from endernet.server.runtime.lifecycle import ChangeKind, LifecycleChange

        device_id = DRIVER_DEVICE_ID if device_id is None else device_id
        outcome = self.registry.register(NodeKey(dimension, x, y, z), device_id)
        self.store.observe(
            LifecycleChange(
                ChangeKind.ADDED, outcome.record.node_key, device_id, outcome.record.ref
            )
        )
        self.record = outcome.record
        self.state = self.store.get_by_ref(self.record.ref)
        self.node = NetworkNode.from_record(self.definitions, self.record)
        return self.record

    def attach(self, mount_network=True):
        # type: (bool) -> object
        """把最近一次 `place()` 的设备接入存储服务。"""
        if mount_network:
            self.service.add_node(self.node)
        return self.node

    def new_medium(self, model="endernet:singleside_tape", slot=0):
        # type: (str, int) -> object
        """新建一块盘并装进槽位，返回介质。"""
        medium, reason, detail = self.store.create_medium(model)
        if medium is None:
            raise AssertionError("造不出介质 %s：%s / %s" % (model, reason, detail))
        ok, reason, detail = self.state.provider.mount(slot, medium.medium_id)
        if not ok:
            raise AssertionError("装不进槽位：%s / %s" % (reason, detail))
        self.medium = medium
        return medium

    def seed(self, resource, count, tick=None):
        # type: (object, int, int) -> int
        """按额度分帧存入 `count` 件，返回下一次可用的帧号。

        **不假装额度不存在**：一次请求放不下就分到下一帧去，这样用例
        表达的才是「库存有多少」，而不是「桶有多大」。`tick` 省略时接着
        上一次的位置往下走（见 `next_tick`）。
        """
        if tick is None:
            tick = self.next_tick
        remaining = count
        while remaining > 0:
            chunk = min(remaining, self.tape_quota)
            receipt = self.service.commit_insert(
                "seed:%s:%d" % (resource.name, tick), resource, chunk, now_tick=tick
            )
            if receipt.moved != chunk:
                raise AssertionError("存入被拒：%r" % (receipt,))
            remaining -= chunk
            tick += 1
        self.next_tick = tick
        return tick


#: 假引擎模块的命名前缀。**集中在这里**而不是散在各测试里：这个名字要
#: 与真引擎的顶层包名区分开，也要一眼看出「这是替身」，四处各写一份迟早
#: 会有一处写成真名——而那会让某个测试悄悄开始依赖真引擎。
FAKE_MODULE_PREFIX = "endernet_fake_engine_"


def install_fake_modules(names):
    # type: (list) -> dict
    """把 `names` 里的名字装成空的假模块，返回 `{名字: 模块}`。

    **调用方负责用完清理**（`tearDown` 里 `sys.modules.pop`）：测试之间
    泄漏假模块，会让后面的用例以为引擎在，于是本该失败的导入悄悄成功。
    一般直接用 `fake_modules()` 上下文管理器，别手写清理。
    """
    import sys
    import types

    installed = {}
    for name in names:
        module = types.ModuleType(name)
        module.__dict__["__endernet_fake__"] = True
        sys.modules[name] = module
        installed[name] = module
    return installed


class fake_modules(object):
    """`with fake_modules("mod.server.extraServerApi") as installed:`。

    退出时把**本次安装的**名字删掉；期间被替换掉的原有模块原样放回——
    直接 `del` 会把一个真实存在的模块也删掉，后续用例于是看到「引擎不在」。
    """

    def __init__(self, *names):
        self._names = list(names)
        self._saved = {}

    def __enter__(self):
        import sys

        self._saved = dict(
            (name, sys.modules[name]) for name in self._names if name in sys.modules
        )
        return install_fake_modules(self._names)

    def __exit__(self, exc_type, exc, tb):
        import sys

        for name in self._names:
            sys.modules.pop(name, None)
        sys.modules.update(self._saved)
        return False


def load_dotted_module(real_name, path, fake=None):
    # type: (str, str, object) -> object
    """按 `path` 加载源码，并**以 `real_name` 注册进 `sys.modules`**。

    引擎边界模块（`server/adapters/*`、`client/*`）在模块顶层
    `import tooldelta`，脱机加载必失败。做法是先装假模块再按真实模块名
    注册——注册这一步不是可有可无的：边界模块里的**相对导入**要靠
    `__package__` 推导，而 `__package__` 由注册名决定；只 exec 不注册的话
    相对导入会以 `__main__` 为基准，报出一个与真实原因毫不相干的错。

    `fake` 是 `fake_modules` 的返回或 `{名字: 模块}`，供调用方在加载前
    往假模块里塞属性；本函数不动它。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(real_name, path)
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[real_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(real_name, None)
        raise
    return module
