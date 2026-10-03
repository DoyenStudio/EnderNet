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


class FakeEventsModule(object):
    """假的 `tooldelta.events`：只提供同步通道要用的三个基类。

    **它的存在理由是离线装配。** `ServerBootstrap.start(bind_engine=True)`
    会注册同步通道的两个监听器，而真 `tooldelta.events` 在游戏外导不进来
    （`server/adapters/messages.py` 把那个 import 放在函数体内）。注入它之后
    「注册 → 派发 → 注销」三条路径都能离线跑——被测的是**接线**，不是引擎。

    **三个基类都是空的**，只实现同步通道真正会用到的那一点点：C2S 去掉
    `unmarshal` 后能装载原始字典，S2C 能 `marshal` 回字典、并把 `send`
    记进 `sent`（不真的发）。名字用 `fake_*` 前缀，避免与真类名混淆。

    契约的精确替身是 `test_sync_channels.FakeEventsModule`（它照搬了
    `ServerEvent.marshal` 抛 `NotImplementedError` 那一条）——那份测的是
    适配器的契约，这份只负责让装配跑起来，两处用途不同。

    **实例本身即解析器**（`__call__` 返回自己）：`AdapterSyncPort` 收的口子
    是「返回事件模块的可调用对象」（默认 `engine_events`），所以注入点写成
    `sync_events=FakeEventsModule()`——构造一次、拿到的那一个实例既是模块
    也是解析器。不做成「调用一下再产生一个新实例」：`classes()` 与
    `on_player_leave()` 各解析一次，两次拿到不同实例的话，`sent` 就会分裂成
    两份，而用例断言的正是「发出去的东西」。
    """

    def __call__(self):
        # type: () -> object
        """把自己当作「事件模块解析器」返回。理由见类 docstring。"""
        return self

    def __init__(self):
        #: 发出去的 `(client_id, payload)`。**每个模块实例一份**：共用一份
        #: 的话，前一个用例发出的包会漏进后一个用例的断言里。
        self.sent = []
        sent = self.sent

        class CustomC2SEvent(object):
            name = "fake_c2s"

            def __init__(self, data=None):
                self.data = data if isinstance(data, dict) else {}

            @classmethod
            def unmarshal(cls, data):
                return cls(data)

        class CustomS2CEvent(object):
            name = "fake_s2c"

            def __init__(self, payload=None):
                self.payload = payload if isinstance(payload, dict) else {}

            def marshal(self):
                return self.payload

            def send(self, client_id):
                sent.append((client_id, self.payload))

        class DelServerPlayerEvent(object):
            name = "fake_player_leave"

        self.CustomC2SEvent = CustomC2SEvent
        self.CustomS2CEvent = CustomS2CEvent
        self.DelServerPlayerEvent = DelServerPlayerEvent


class FakeSlotContainerProvider(object):
    """位置 -> 按槽可读写的假容器。**每个位置一个独立容器**。

    存储驱动器从 2026-10-04 起以**方块容器的读数为槽位权威**，因此凡是要
    装一块盘进驱动器的夹具都必须给它一个容器——本类就是那个容器。
    默认 10 格，与 `STORAGE_DRIVER_SLOT_COUNT` 和方块自己的
    `container_size` 同值。
    """

    def __init__(self, slots=10):
        self.slots = int(slots)
        self.by_position = {}

    def container_at(self, node_key):
        from endernet.server.adapters.containers import FakeContainer

        position = node_key.as_key()
        if position not in self.by_position:
            self.by_position[position] = FakeContainer(
                slots=self.slots, max_stack=1
            )
        return self.by_position[position]

    def diagnostics(self):
        return {"fake": True, "cached": len(self.by_position)}


class FakeInventoryEndpoint(object):
    """最小背包端点：只实现存储服务的卸载路径用到的四件事。"""

    def __init__(self, capacity=64):
        self.capacity = int(capacity)
        self.items = []

    def endpoint_id(self):
        return "fake-inventory"

    def holder_id(self):
        return "fake-player"

    def can_read(self):
        return True

    def probe_space(self, resource_key, count):
        return self.capacity

    def commit_insert(self, op_id, resource_key, count, owner_id="", origin=None):
        from endernet.common.protocol import TransferReceipt, TransferStatus

        moved = max(0, min(count, self.capacity))
        if moved <= 0:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED, "", resource_key
            )
        self.capacity -= moved
        self.items.append((resource_key, moved))
        if moved == count:
            return TransferReceipt(
                op_id, count, moved, TransferStatus.COMPLETE, "", resource_key
            )
        return TransferReceipt(
            op_id, count, moved, TransferStatus.PARTIAL, "", resource_key
        )


class FakePlayerInventoryProvider(object):
    """`位置 -> 背包` 的替身。**按玩家缓存**，与生产实现同一生命周期。"""

    def __init__(self, capacity=64):
        self.capacity = int(capacity)
        self.by_player = {}

    def endpoint_of(self, player_id):
        if player_id not in self.by_player:
            self.by_player[player_id] = FakeInventoryEndpoint(self.capacity)
        return self.by_player[player_id]

    def diagnostics(self):
        return {"fake": True, "players": len(self.by_player)}


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
        #: 驱动器那 10 格的假容器提供方。**槽位占用的权威就是它**
        #: （2026-10-04）：没有它，驱动器读不到任何槽位，`new_medium` 装不进去。
        self.containers = FakeSlotContainerProvider()
        self.store = StorageDriverStore(
            self.definitions,
            self.registry,
            policy=self.driver_policy,
            medium_policy=self.medium_policy,
            on_load_changed=self.loads.append,
            containers=self.containers,
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
        """新建一块盘并装进槽位，返回介质。

        **装 = 把介质物品写进方块容器**（2026-10-04）：驱动器随后重读，
        槽位读数是那次写入的结果。因此这里走 `place_medium` 而不是
        往内存里塞一个槽位。
        """
        medium, reason, detail = self.store.create_medium(model)
        if medium is None:
            raise AssertionError("造不出介质 %s：%s / %s" % (model, reason, detail))
        ok, reason, detail = self.store.place_medium(self.state, slot, medium.medium_id)
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


class MemoryExtraDataBackend(object):
    """`ExtraDataBackend` 的内存替身（12-03）。

    **刻意分成「内存区」与「已提交」两份**，因为真实引擎就是这么分的：
    `SetExtraData(k, v, auto_save=False)` 只改内存区，`SaveExtraData()` 才提交
    （`persistence_backend.md` §3.3）。合成一份的话，「提交前读得到、提交后
    还在」这条会被替身自己变成恒真，而它正是 `commit()` 语义要验的东西。

    真实引擎的**异步落盘窗口**不在这里模拟：那是引擎行为，不是本层的契约，
    12-05/12-06 才需要以它为前提。
    """

    def __init__(self):
        #: 已提交（相当于已进 leveldb）的键值。
        self.committed = {}
        #: 引擎内存区里还没提交的改动。
        self.pending = {}
        #: `save()` 被调了几次。**固定开销按调用次数付**，所以要数它。
        self.saves = 0

    def get(self, key):
        # type: (str) -> object
        if key in self.pending:
            return self.pending[key]
        return self.committed.get(key)

    def set(self, key, value, auto_save=True):
        # type: (str, str, bool) -> bool
        self.pending[key] = value
        if auto_save:
            self.committed[key] = value
        return True

    def delete(self, key):
        # type: (str) -> bool
        self.pending.pop(key, None)
        self.committed.pop(key, None)
        return True

    def save(self):
        # type: () -> bool
        self.saves += 1
        self.committed.update(self.pending)
        self.pending = {}
        return True


#: 假引擎模块的命名前缀。**集中在这里**而不是散在各测试里：这个名字要
#: 与真引擎的顶层包名区分开，也要一眼看出「这是替身」，四处各写一份迟早
#: 会有一处写成真名——而那会让某个测试悄悄开始依赖真引擎。
FAKE_MODULE_PREFIX = "endernet_fake_engine_"


class DisplayStand(object):
    """把真实 `StorageService` 包成终端要的**显示投影**，样板来源可控。

    11 的同步层拿到的是 `NetworkItemProjection`（多一列 `has_pattern`、
    版本也是显示版本），因此驱动 hub 的用例必须喂投影，而不是裸的存储服务。
    喂裸服务的话，行是三元组、版本是交易版本——测的就不再是线上那一条链。

    `mark` / `unmark` 模拟「本网络的样板提供者能做什么」，`join` / `leave`
    模拟设备进出这张网。**产物集合与成员集合都是可变的**：纯样板变化、
    设备换网这两条正是投影要靠轮询看出来的东西，写死了就测不到。
    """

    def __init__(self, service, network_id="net-1"):
        # type: (object, str) -> None
        from endernet.server.storage.projection import NetworkItemProjection

        self.service = service
        self.members = ()
        self.products = set()
        #: 样板内容的修订号。投影靠它判断「要不要重算产物集合」——只改
        #: 成员集合而修订号不变时，签名那条路仍然要能看出来。
        self.revision = 0
        self.projection = NetworkItemProjection(
            network_id,
            storage_of=lambda: self.service,
            members_of=lambda: self.members,
            products_of=lambda members: tuple(self.products),
            products_revision=lambda: self.revision,
        )

    def mark(self, *keys):
        # type: (object) -> None
        """让这些产物「本网络有样板」。"""
        for key in keys:
            self.products.add(key)
        self.revision += 1

    def unmark(self, *keys):
        # type: (object) -> None
        """撤掉这些产物的样板（库存为零时该行随之消失）。"""
        for key in keys:
            self.products.discard(key)
        self.revision += 1

    def join(self, *node_keys):
        # type: (object) -> None
        """设备进网。**不动修订号**：内容没变，只有成员集合变了。"""
        self.members = tuple(self.members) + tuple(node_keys)

    def leave(self, *node_keys):
        # type: (object) -> None
        """设备离网。理由同 `join`。"""
        drop = set(key.as_key() for key in node_keys)
        self.members = tuple(
            key for key in self.members if key.as_key() not in drop
        )


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


#: 2026-09-25 游戏内实测：引擎对一个**原版箱子**里的石头返回 16 个字段。
#: `test_containers_engine.py` 与 `test_resource_contracts.py` 各有一份冻结的
#: 逐字段副本（那两份是同源的、刻意不合并）；本函数是**可参数化**的那一份，
#: 给「要造好几件不同物品」的用例用（背包界面就是这种）。
ENGINE_ITEM_SHAPE = {
    "isDiggerItem": False,
    "enchantData": [],
    "durability": 0,
    "itemId": 1,
    "customTips": "",
    "extraId": "",
    "newAuxValue": 0,
    "modEnchantData": [],
    "modId": "",
    "userData": None,
    "modItemId": "",
    "auxValue": 0,
    "showInHand": True,
}


def item_dict_with_carrier(name, medium_id, count=1, version=None):
    # type: (str, str, int, object) -> dict
    """一件**带着载体**的介质物品字典——我方写进方块容器的那种形状。

    载体的格式版本默认取 `medium_item.CARRIER_VERSION`；显式给一个别的值
    用来测「版本不认识」那条分支。
    """
    from endernet.common import medium_item
    from endernet.common.item_codec import item_dict_from_key
    from endernet.common.resource_key import ResourceKey

    data = item_dict_from_key(ResourceKey.item(name, 0), count)
    if version is None:
        version = medium_item.CARRIER_VERSION
    data["userData"] = medium_item.write_carrier(None, medium_id, version)
    return data


def carrier_of_item(data):
    # type: (object) -> object
    """一件物品字典上的载体，等价于 `medium_item.read_carrier(userData)`。"""
    from endernet.common import medium_item

    if not isinstance(data, dict):
        return None
    return medium_item.read_carrier(data.get("userData"))


def managed_lines_of(data):
    # type: (object) -> tuple
    """一件物品的 `display.Lore` 里**本模组管理的**那些行。"""
    from endernet.common import medium_item

    if not isinstance(data, dict):
        return ()
    user_data = data.get("userData")
    if not isinstance(user_data, dict):
        return ()
    display = user_data.get(medium_item.DISPLAY_KEY)
    if not isinstance(display, dict):
        return ()
    lore = display.get(medium_item.LORE_KEY)
    if not isinstance(lore, (list, tuple)):
        return ()
    return tuple(line for line in lore if medium_item.is_managed_line(line))


def item_dict_without_carrier(name, count=1):
    # type: (str, int) -> dict
    """一件**没有载体**的介质物品字典 = 一块未初始化的空白介质。"""
    from endernet.common.item_codec import item_dict_from_key
    from endernet.common.resource_key import ResourceKey

    return item_dict_from_key(ResourceKey.item(name, 0), count)


def engine_item(name, count, **identity):
    # type: (str, int, object) -> dict
    """一份**引擎读回来**的物品字典。`identity` 覆盖上面那几个字段。

    **为什么测试要照抄引擎的 16 个字段**：客户端算身份走的是
    `engine_item_key`，它读 `Item._orig` 里这份原始字典；而
    `tooldelta.define.item.Item.from_dict` 把缺席的 `durability` 补成 `0`，
    服务端却把 `0` 归一成 `None`，`ResourceKey` 又认为两者是不同身份。少写
    几个字段、或者用属性拼一份「看起来一样」的，算出来的指纹都与服务端不同
    ——带槽请求会全部被判 `SLOT_CHANGED`，而离线测试照样是绿的。
    """
    data = dict(ENGINE_ITEM_SHAPE)
    data["newItemName"] = name
    data["itemName"] = name
    data["count"] = count
    data.update(identity)
    return data


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
