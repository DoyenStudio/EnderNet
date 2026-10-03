# coding=utf-8
"""通用终端（11-03）：绑定接线、点击取物、搜索、排序与重锚。

这个文件守的是**界面与列表模型之间的那层缝**，而缝里的错在游戏里几乎都
看不出来：

- 绑定名少一个点、集合名对不上——格子全空，看着像「网络里没东西」。
- 排序图标与真实顺序分家——图标说降序而列表是升序，玩家不会怀疑图标。
- 重锚不生效——滚动之后列表跳走，但那可能被当成「本来就这样」。

因此下面每一条都对着一个**具体到能被看见的**后果，而不是「函数被调用了」。
界面的另一半（控件有没有画出来、`$` 变量能不能替换）离线测不了，属游戏内
实测项，见 `plans/tasks/11-交付说明.md`。
"""

import json
import os
import sys
import types
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import testsupport  # noqa: E402  设置 sys.path，并提供引擎物品字典的实测形状

PREFIX = "endernet_bp.endernet_scripts."


class BinderStub(object):
    """与 `test_item_list_window` 同一份标记值。**必须与真 `Binder` 的取值
    无关**：这里断言的是「注册时用的旗标与 JSON 里 `binding_type` 对得上」，
    对不上的表现是引擎按另一种类型解析同一份数据。

    两个装饰器**照真 `Binder` 的样子往函数上贴标记**并原样返回函数——
    `ToolDeltaScreen._get_tdscreen_bindings` 就是靠这三个属性在 `dir(cls)`
    里认出绑定方法的，贴不上就等于那条绑定没注册。
    """

    BF_BindString = 1
    BF_BindInt = 2
    BF_BindBool = 3
    BF_ButtonClickUp = 4
    BF_EditChanged = 5
    BF_ButtonClickDown = 6
    #: 11-06 起本仓库也用到这两个旗标（工厂终端的进度条与三色背景）。
    #: 取值与真引擎无关，只要求**各测试文件彼此一致**。
    BF_BindColor = 7
    BF_BindFloat = 8
    @staticmethod
    def binding(flag, name):
        def decorate(func):
            func.binding_flags = flag
            func.binding_name = name
            return func
        return decorate

    @staticmethod
    def binding_collection(flag, collection, name):
        def decorate(func):
            func.binding_flags = flag
            func.binding_name = name
            func.collection_name = collection
            return func
        return decorate


class ItemStub(object):
    #: 资源名 → `maxStackSize`，没登记的一律 64。**「取一组」的上限只有在
    #: 它小于可用数时才咬得住**；夹具里没登记时全是 64，而 64 比任何一行
    #: 的可用数都大——上限那条路会永远走不到，测试照样是绿的。要验它就得
    #: 在这里塞一条小堆叠（工具、桶都是 1）。
    stack_sizes = {}

    def __init__(self, item_id, count=1, orig=None):
        self.newItemName = item_id
        self.count = count
        #: 引擎给的原始字典。**只有从引擎读回来的物品才有**，手工构造的没有；
        #: `engine_item_key` 靠它算身份，手工物品请用 `item_key`。
        self._orig = orig if orig is not None else {}

    def GetBasicInfo(self):
        # 每件物品给一个**互不相同**的 id_aux：全都一样的话，「格子取错了
        # 行」在离线永远测不出来——而图标画错行是玩家第一眼就该看见的错。
        return types.SimpleNamespace(
            itemName=self.newItemName, id_aux=abs(hash(self.newItemName)) % 30000,
            maxStackSize=self.stack_sizes.get(self.newItemName, 64),
            maxDurability=0,
        )


class Control(object):
    """自动长出子控件的替身。记下 UV 与可见性，供断言读写。"""

    def __init__(self):
        self.children = {}
        self.uv = None
        self.uv_size = None
        self.uv_writes = 0
        self.visible = None
        self.text = None
        self.removed = False

    def GetElement(self, path):
        return self.children.setdefault(path, Control())

    def asButton(self):
        return self

    asImage = asGrid = asItemRenderer = asLabel = asButton

    def asScrollView(self):
        raise RuntimeError("not a scroll view")

    def SetUV(self, uv, uv_size):
        self.uv = uv
        self.uv_size = uv_size
        self.uv_writes += 1

    def SetVisible(self, visible):
        self.visible = visible

    def SetText(self, text):
        self.text = text

    def SetSpriteClipRatio(self, direction, ratio):
        pass

    def SetSpriteColor(self, rgb):
        pass

    def SetUiItem(self, item):
        return True

    def GetGridItem(self, x, y):
        return self.GetElement((x, y))

    def Remove(self):
        self.removed = True


class Scroll(object):
    """引擎 `ScrollViewUIControl` 的替身——**就是那个对象本身**。

    **2026-10-01 真机实测**：`screen.GetBaseUIControl(路径).asScrollView()`
    回的是引擎控件，两个百分比方法**长在它身上**（`dir()` 里就有），而
    `.base` 是 tooldelta `UBaseCtrl` 才有的东西，这里根本没有。

    原先这份替身照 tooldelta 包装层的形状写（方法挂在 `.base` 上），于是
    `terminal_panel` 里那两处 `self._scroll.base.X` 在离线**一路绿灯**，
    真机上却每次都 `AttributeError`，再被 `except Exception: return` 吞掉——
    **重锚整条静默失效**，表现只是「滚动位置偶尔会跳」。

    **`.base` 的缺席是有意的**：谁再写回 `.base`，本文件立刻 `AttributeError`，
    `TestRerenderAndAnchoring` 的两条锚点用例当场变红。
    """

    def __init__(self):
        self.percent = 0
        self.written = []

    def GetScrollViewPercentValue(self):
        return self.percent

    def SetScrollViewPercentValue(self, percent):
        self.written.append(percent)
        # 引擎写过之后读回来就是新值，替身照抄：不这么做的话
        # 「写完再读」这条路径测不出来。
        self.percent = percent

    def asScrollView(self):
        return self


class Screen(object):
    """引擎那个 UI 屏幕对象的替身：控件树、滚动条、绑定登记表。

    `bindings` / `collection_bindings` 里放的是**已经解析好的回调**，由
    `ScreenBase` 按引擎的方式填（见那里）。本模块的界面已经全部改成类体
    静态注册，因此这两个字典不再由 `DynaBinding*` 写入。
    """

    def __init__(self):
        self.bindings = {}
        self.collection_bindings = {}
        self.elements = {}
        self.scrolls = {}
        self._activated = True

    def _key(self, path):
        # type: (str) -> str
        """把两种屏幕挂法收成同一个键。

        真机上 `/terminal/x`（通用终端）与 `/x`（另外三台）是**两个命名空间**
        （见 `slot_layout._candidates`）。这份替身两种都得认，否则代码按候选
        顺序问到哪一条，测试就得跟着改读哪一条——而测试要钉的是「代码写进去
        的那一格，用本模块的常量名读得出来」。
        """
        prefix = slot_layout.CONTENT_ROOT + "/"
        if path.startswith(prefix):
            return path[len(slot_layout.CONTENT_ROOT):]
        return path

    def GetElement(self, path):
        return self.elements.setdefault(self._key(path), Control())

    def GetBaseUIControl(self, path):
        return self.scrolls.setdefault(self._key(path), Scroll())


class ScreenNodeStub(object):
    """引擎 `ScreenNode` 的替身。生成的 `_Base` 继承它。

    **用得上的是它没有的东西**：引擎取值时拿回调的名字在 `_Base` 上
    `getattr`，名字不在类上就是 `AttributeError`——游戏里那一行
    `'CommonTerminalPanel_Base' object has no attribute 'reader'` 正是这么来的。
    """


def td_screen_attrs(cls):
    """`ToolDeltaScreen._get_tdscreen_bindings` 的等价实现。

    与 `tooldelta/ui/general_screen.py` 的差别只有一处：那边会设 `__module__`。
    包装函数抄的是那边**一模一样的名字规则**——默认绑定挂成 `key`、集合绑定
    挂成 `key_bindwrapper`，两边的 `__name__` 也照抄。名字规则要是改了一边没
    改另一边，这里就会先炸。
    """
    attrs = {}
    for key in dir(cls):
        attr = getattr(cls, key)
        if hasattr(attr, "collection_name"):
            def wrap(_func):
                def _wrapper(self, *args):
                    return _func(self._super_screen_ins, *args)
                return _wrapper

            wrapper = wrap(attr)
            wrapper.__name__ = key + "_bindwrapper"
            attrs[key + "_bindwrapper"] = BinderStub.binding_collection(
                attr.binding_flags, attr.collection_name, attr.binding_name)(wrapper)
        elif hasattr(attr, "binding_flags"):
            def wrap(_func):
                def _wrapper(self, *args):
                    return _func(self._super_screen_ins, *args)
                return _wrapper

            wrapper = wrap(attr)
            wrapper.__name__ = key
            attrs[key] = BinderStub.binding(
                attr.binding_flags, attr.binding_name)(wrapper)
    return attrs


class ScreenBase(object):
    """`tooldelta.ui.ToolDeltaScreen` 的替身：只提供基类真正用到的那几样。

    **注册发生在这里**，与真 `ToolDeltaScreen._register_as_screen` 同一处、
    同一时机：造一个 `_Base` 类，把类体里那些带标记的方法包成它的属性，
    再按引擎的方式解析出回调填进屏幕的登记表。

    `DynaBinding*` 也照引擎的口径实现——**按回调自己的名字在 `_Base` 上
    取**。本模块的界面一条都不走这条路了，留着它是为了让「哪天有人又把绑定
    写回运行时注册」当场炸在这里（真 `_make_dyna_binding_func` 会把包装函数
    的名字抄成回调的名字，那个名字不在 `_Base` 上），而不是等进游戏每渲染
    一帧抛一次 `AttributeError`。
    """

    def __init__(self, screen_name, screen_instance, params=None):
        self.screen = screen_instance
        self._init_params = params if params is not None else {}
        self._activated = False
        base = type(type(self).__name__ + "_Base", (ScreenNodeStub,),
                    td_screen_attrs(type(self)))
        self._engine = base.__new__(base)
        self._engine._super_screen_ins = self
        for key in dir(self._engine):
            func = getattr(self._engine, key)
            flags = getattr(func, "binding_flags", None)
            if flags is None:
                continue
            collection = getattr(func, "collection_name", None)
            resolved = self._resolve(func.__name__)
            if collection is None:
                self.screen.bindings[(flags, func.binding_name)] = resolved
            else:
                self.screen.collection_bindings[
                    (flags, collection, func.binding_name)] = resolved

    def _resolve(self, func_name):
        # type: (str) -> object
        """引擎的那一步：按名字在 `_Base` 上取回调。取不到就是 `AttributeError`。"""
        return getattr(self._engine, func_name)

    def GetElement(self, path):
        return self.screen.GetElement(path)

    def GetBaseUIControl(self, path):
        return self.screen.GetBaseUIControl(path)

    def _dyna(self, func_name, flag, name, collection):
        registry = (self.screen.bindings if collection is None
                    else self.screen.collection_bindings)
        key = ((flag, name) if collection is None
               else (flag, collection, name))
        registry[key] = self._resolve(func_name)

    def DynaBinding(self, flag, name, callback):
        self._dyna(getattr(callback, "__name__", ""), flag, name, None)

    def DynaUnbinding(self, flag, name):
        self.screen.bindings.pop((flag, name), None)

    def DynaBindingCollection(self, flag, collection, name, callback):
        self._dyna(getattr(callback, "__name__", ""), flag, name, collection)

    def DynaUnbindingCollection(self, flag, collection):
        for key in list(self.screen.collection_bindings):
            if key[:2] == (flag, collection):
                del self.screen.collection_bindings[key]

    def OnCreate(self):
        pass

    def OnDestroy(self):
        pass

    def OnTicking(self):
        pass


def _registrable(key, is_proxy=False):
    """`RegistToolDeltaScreen` 的替身。**两个参数都要收**：11-07 起设备屏用
    `is_proxy=True` 注册，签名少一个就是导入期 `TypeError`（`register_screens`
    一调就炸）。这里顺带记下旗标，代理那一类的断言靠它。
    """

    def decorate(cls):
        cls._screen_key = key
        cls._td_is_proxy = is_proxy
        return cls

    return decorate


def _install_stubs():
    """把 tooldelta 的那两个模块替掉。

    **必须赶在导入 `terminal_panel` 之前**：真 `tooldelta/__init__.py` 在
    模块级 import 引擎（`mod.server.extraServerApi`），脱离游戏一导入就炸。
    替身注册在**游戏内的那个模块名**下，于是相对导入 `....tooldelta.ui`
    恰好命中它。
    """
    for suffix, attrs in (
        ("tooldelta.ui", {
            "Binder": BinderStub,
            "RegistToolDeltaScreen": _registrable,
            "ToolDeltaScreen": ScreenBase,
        }),
        ("tooldelta.define.item", {"Item": ItemStub}),
    ):
        module = types.ModuleType(PREFIX + suffix)
        module.__dict__.update(attrs)
        sys.modules[module.__name__] = module


_install_stubs()

from endernet_bp.endernet_scripts.endernet.client.network_client import (  # noqa: E402
    NetworkClient,
)
from endernet_bp.endernet_scripts.endernet.client.ui import (  # noqa: E402
    backpack_model, flying_item, slot_layout, stack_size, terminal_panel,
)
from endernet_bp.endernet_scripts.endernet.client.ui.item_list_model import (  # noqa: E402
    SORT_COUNT, SORT_NAME,
)
from endernet_bp.endernet_scripts.endernet.client.ui.terminal_cache import (  # noqa: E402
    TerminalCache,
)
from endernet_bp.endernet_scripts.endernet.client.ui.widgets import (  # noqa: E402
    device_status, order_preview, order_preview_bindings,
)
from endernet_bp.endernet_scripts.endernet.common.protocol import (  # noqa: E402
    STATUS_OK, STATUS_REJECTED, CommandResultMessage, ItemDelta, SessionReady,
    SnapshotBegin, SnapshotChunk, SnapshotEnd, SyncEnvelope, SyncError,
)
from endernet_bp.endernet_scripts.endernet.common.identity import (  # noqa: E402
    DeviceRef, NodeKey,
)
from endernet_bp.endernet_scripts.endernet.common.resource_key import (  # noqa: E402
    ResourceKey,
)

#: 一份**真的** `DeviceRef.to_dict()`。面板拿到的就是这个形状——它必须能还原
#: 成 `DeviceRef`，因为客户端查不到权威位置，猜一个出来只会换来一条被拒的
#: 会话请求。
REF = DeviceRef(NodeKey(0, 10, 64, 20), 3)

STONE = ResourceKey.item("minecraft:stone")
SAND = ResourceKey.item("minecraft:sand")
DIRT = ResourceKey.item("minecraft:dirt")
#: 11 增量用到的第四行：**没有样板**的那一种，用来钉「长按无 `+` 的行
#: 不触发下单入口」。
IRON = ResourceKey.item("minecraft:iron_ingot")
ZINC = ResourceKey.item("minecraft:zinc")

SNAPSHOT_VERSION = 10
DELTA_VERSION = 11


class RecordingPort(object):
    """记下发出去的包，并记下注册/注销。端口契约只有这四件事。"""

    def __init__(self):
        self.sent = []
        self.listens = 0
        self.unlistens = 0

    def send(self, payload):
        self.sent.append(payload)
        return True

    def listen(self, handler, priority=0):
        self.listens += 1
        return 1

    def unlisten(self):
        self.unlistens += 1

    def diagnostics(self):
        return {"stats": {}, "channels": [], "listening": False}


class PanelFixture(unittest.TestCase):
    def setUp(self):
        # `stack_size` 的缓存挂在**模块**上（不跟视图实例走），因此它会跨用例
        # 存活。不在这里清掉的话，某条用例压低的堆叠上限会漏给后面所有用例
        # ——而那种失败看起来像是「上限算错了」，不是「上一个用例没擦干净」。
        self.addCleanup(stack_size.reset)
        stack_size.reset()
        self.port = RecordingPort()
        self.client = NetworkClient(port=self.port)
        self.cache = TerminalCache(client=self.client)
        self.screen = Screen()
        # 三行的**可用数各占一种情形**：36（正常显示）、1（留空）、0（留空）。
        # 只放「正常显示」的话，`<= 1` 那一支在离线永远是绿的。
        self._batch_serial = 0
        self.feed(SessionReady(7, SNAPSHOT_VERSION))
        self.snapshot([(STONE, 40, 4, False), (SAND, 1, 0, False), (DIRT, 2, 2, False)])
        self.cache.tick()
        self.backpack = backpack_model.BackpackModel()
        #: 假的引擎背包读数，由 `engine_backpack()` 填。**引擎槽序**：0–8 是
        #: 快捷栏、9–35 是背包，与界面那两段的顺序正好相反。
        self.engine_items = [None] * backpack_model.SLOT_COUNT
        # 面板是**绑定的登记处**（十条列表绑定写在它的类体里），列表视图只是
        # 被转发的那一头。建立顺序与生产一致：先有面板，再把视图挂上去。
        # 本夹具不喂 `device_ref`，因此面板自己不会建视图——正是要手动挂。
        self.panel = terminal_panel.CommonTerminalPanel("main", self.screen, {})
        self.view = terminal_panel.TerminalListView(
            self.panel, cache=self.cache, device_ref=REF, dimension=0,
            backpack=self.backpack,
        )
        self.panel.list_view = self.view
        # **面板与列表视图必须共用同一个背包模型。** 生产就是这么接的
        # （`terminal_panel.py` 的 `TerminalListView(..., backpack=self.backpack)`），
        # 而夹具此前各建各的——那时看不出来，因为面板的点击路径只碰自己的那一份。
        # 2026-09-30 起 `_click` 会读背包（判断这一下是不是背包内动作），两份模型
        # 走岔的表现是「点了有反应，但反应在另一个对象上」，离线全绿、真机全错。
        self.panel.backpack = self.backpack
        self.addCleanup(self.view.close)

    # -- 引擎背包 ------------------------------------------------------

    def engine_backpack(self, *entries):
        # type: (tuple) -> None
        """装上一份假的引擎背包读数。`entries` 是 `(引擎槽号, 资源名, 数量)`。

        **一条用例只调一次**：每调一次就多叠一层替换，`self.engine_items`
        指向最后那一层，中间那层的内容再改也不会被读到。要改内容请用
        `put_engine` / `clear_engine` 改 `self.engine_items`。
        """
        patcher = patch.object(backpack_model, "_read_engine_items",
                               lambda: list(self.engine_items))
        patcher.start()
        self.addCleanup(patcher.stop)
        for slot, name, count in entries:
            self.put_engine(slot, name, count)
        self.backpack.replace_from_engine()

        from endernet.common.protocol import InventorySnapshot
        rows = []
        for slot, item in enumerate(self.engine_items):
            resource = backpack_model.engine_item_key(item) if item else None
            rows.append({"slot": slot,
                         "canonical": resource.canonical() if resource else "",
                         "count": item.count if item else 0, "slot_version": 7})
        self.feed(InventorySnapshot(7, rows))

    def put_engine(self, slot, name, count, **identity):
        # type: (int, str, int, object) -> None
        """往那一格放一件东西——**改的是引擎那一侧**，模型要重读才看得到。"""
        self.engine_items[slot] = ItemStub(
            name, count, orig=testsupport.engine_item(name, count, **identity))

    def clear_engine(self, slot):
        # type: (int) -> None
        self.engine_items[slot] = None

    def take_command(self):
        # type: () -> dict
        """最后一条发出去的命令。"""
        return self.commands()[-1]

    def in_flight(self):
        # type: () -> set
        """还没收到回执的命令 `request_id`。

        **是集合不是单值**：背包内的操作支持多轮，同时有几条在途是正常状态。
        单值会让后一条顶掉前一条，前一条的回执就认不出来了。
        """
        return set(self.view._action_requests)

    def sole_in_flight(self):
        # type: () -> str
        """恰好一条在途命令的 id。**多于一条时报错**——那种用例要显式说明
        自己为什么会有多条。"""
        pending = self.in_flight()
        self.assertEqual(len(pending), 1,
                         "期望恰好一条在途命令，得到 %r" % (pending,))
        return list(pending)[0]

    # -- 线上包 --------------------------------------------------------

    def feed(self, message, session_id="s1", network_id="n1", epoch="e1"):
        return self.client.receive(
            SyncEnvelope(message, session_id, network_id, epoch).to_dict()
        )

    def snapshot(self, records, version=SNAPSHOT_VERSION, snapshot_id="snap-1"):
        self.feed(SnapshotBegin(snapshot_id, version, len(records)))
        for index, record in enumerate(records):
            self.feed(SnapshotChunk(snapshot_id, index, (record,)))
        self.feed(SnapshotEnd(snapshot_id, len(records)))

    def delta(self, upserts=(), removes=(), base=SNAPSHOT_VERSION,
              new=DELTA_VERSION, batch_id=None):
        """推一批增量。**批次号默认自增**：`TerminalCache` 按 `batch_id` 去重
        （同一批重发是网络上的正常现象），两个不同的批次共用同一个号，第二
        批会被当成迟到包丢掉——那会让「这批不该重画」的用例拿到一个永远
        为零的计数，然后看起来像是绿了。"""
        if batch_id is None:
            self._batch_serial += 1
            batch_id = "b%d" % self._batch_serial
        self.feed(ItemDelta(
            batch_id, 0, 1, base, new, tuple(upserts), tuple(removes)
        ))

    def commands(self):
        return [
            item["message"] for item in self.port.sent
            if item["message"]["kind"] == "command"
        ]

    def result(self, request_id, status=STATUS_OK):
        # type: (str, str) -> dict
        """一条形状合法的命令回执（`CommandResult.to_dict()` 的样子）。"""
        return {"request_id": request_id, "status": status, "payload": {},
                "reasons": [], "topology_version": None}

    def rejected(self, request_id):
        # type: (str) -> dict
        """一条**被拒**的回执。放在夹具里：`inv_take` 与背包内动作两条路都要
        用它，各写一份的话两边的形状迟早分家。"""
        return self.result(request_id, status=STATUS_REJECTED)

    # -- 绑定回调 ------------------------------------------------------
    #
    # 两个取值口都走 `screen.bindings` / `screen.collection_bindings`，也就是
    # **引擎解析之后**的那一份（`ScreenBase` 填的），而不是直接拿着方法对象
    # 调。差别不是形式：绑定名、旗标、集合任一对不上，这里就取不到。

    def bind(self, flag, name, *args):
        name = terminal_panel.BINDING_PREFIX + name
        return self.screen.bindings[(flag, name)](*args)

    def cell(self, flag, name, index):
        name = terminal_panel.BINDING_PREFIX + name
        key = (flag, terminal_panel.COLLECTION, name)
        return self.screen.collection_bindings[key](index)

    def names(self):
        return [entry.key.name for entry in self.view.rows]


class TestBindings(PanelFixture):
    """绑定的名字、旗标与集合必须与 JSON 里写的完全一致。"""

    def test_scalar_bindings_are_registered_with_the_engine_flags(self):
        expected = (
            (BinderStub.BF_BindInt, "count"),
            (BinderStub.BF_BindBool, "unavailable"),
            (BinderStub.BF_EditChanged, "search"),
            (BinderStub.BF_ButtonClickUp, "close"),
            (BinderStub.BF_ButtonClickUp, "sort_method"),
            (BinderStub.BF_ButtonClickUp, "sort_order"),
            (BinderStub.BF_ButtonClickUp, "select"),
        )
        for flag, name in expected:
            self.assertIn((flag, terminal_panel.BINDING_PREFIX + name),
                          self.screen.bindings)

    def test_the_close_binding_sits_on_the_close_handler(self):
        """`#EnderNetTerminal.close` 必须挂在 `OnCloseClick` 上——**名字配名字**。

        2026-10-02 出过一次：加 ESC 那条时，`@Binder.binding(...close)`
        被留在了新方法 `OnEscapeKey` 头上，于是**点 ✕ 完全没反应**——那一下
        调到的是键盘事件的处理函数，它拿到按钮的 `params`，
        `getattr(event, "isDown", 0)` 取不到就当场返回；而 `OnCloseClick`
        成了没人绑的死方法。

        **装饰器与它修饰的函数之间没有任何语法约束**，插一个新方法进去就能
        悄悄拆散一对，而且两边都不报错。因此这里钉的不是「close 有没有绑定」
        （上面那条已经钉了），而是**它挂在哪个函数上**。
        """
        close_name = terminal_panel.BINDING_PREFIX + "close"
        holders = sorted(
            name for name, func
            in td_screen_attrs(terminal_panel.CommonTerminalPanel).items()
            if getattr(func, "binding_name", None) == close_name
        )
        self.assertEqual(["OnCloseClick"], holders)

    def test_cell_bindings_live_on_the_terminal_collection(self):
        expected = (
            (BinderStub.BF_BindBool, "has_item"),
            (BinderStub.BF_BindInt, "item_id_aux"),
            (BinderStub.BF_BindBool, "selected"),
            (BinderStub.BF_BindString, "item_count"),
        )
        for flag, name in expected:
            self.assertIn(
                (flag, terminal_panel.COLLECTION,
                 terminal_panel.BINDING_PREFIX + name),
                self.screen.collection_bindings)

    def test_count_and_cell_guards_follow_the_visible_rows(self):
        self.assertEqual(self.bind(BinderStub.BF_BindInt, "count"), 3)
        for index in range(3):
            self.assertTrue(self.cell(BinderStub.BF_BindBool, "has_item", index))
        # **越界的下标必须报假**，而不是当成第 0 行：引擎在列表变短的那一帧
        # 仍会按旧的行数回调一次，把那种回调当成有效数据就会画出幽灵格子。
        for invalid in (-1, 3, 99, None, True, "0", 1.0):
            self.assertFalse(self.cell(BinderStub.BF_BindBool, "has_item", invalid))
            self.assertEqual(self.cell(BinderStub.BF_BindInt, "item_id_aux", invalid), 0)
            self.assertEqual(self.cell(BinderStub.BF_BindString, "item_count", invalid), "")

    def test_each_cell_gets_the_icon_of_its_own_row(self):
        seen = [self.cell(BinderStub.BF_BindInt, "item_id_aux", index)
                for index in range(3)]
        self.assertEqual(len(set(seen)), 3)
        self.assertTrue(all(isinstance(value, int) and value > 0 for value in seen))

    def test_cell_count_shows_available_not_stored(self):
        """数量标签显示**可用数**（决策 D4）：`stored - reserved`。

        `minecraft:stone` 存 40 预留 4，因此格子上是 36 而不是 40——这正是
        「显示可用数与预留数的差别」这条验收的落点。**排序仍按总存量**
        （决策 D7），两者不是同一个量，差异记在待确认段。
        """
        stone = self.names().index("minecraft:stone")
        self.assertEqual(self.cell(BinderStub.BF_BindString, "item_count", stone), "36")
        # 可用数 ≤ 1 时留空：满屏的「1」只是噪声。
        sand = self.names().index("minecraft:sand")
        self.assertEqual(self.cell(BinderStub.BF_BindString, "item_count", sand), "")
        dirt = self.names().index("minecraft:dirt")
        self.assertEqual(self.cell(BinderStub.BF_BindString, "item_count", dirt), "")

    def test_unavailable_mask_follows_the_session(self):
        self.assertFalse(self.bind(BinderStub.BF_BindBool, "unavailable"))
        self.client.drop_session()
        self.assertTrue(self.bind(BinderStub.BF_BindBool, "unavailable"))

    def test_a_closed_view_still_answers_the_engine_with_an_empty_list(self):
        """绑定**不随会话摘除**，所以收尾靠的是「答案变空」。

        摘除是个错觉：绑定写在类体里、跟着界面走，而界面比会话活得久。关闭
        与最后一帧渲染之间有一段窗口，那时引擎照常来问——报一个还没清掉的
        行数就是让玩家看见一个已经不存在的列表。
        """
        self.view.close()
        self.assertEqual(self.bind(BinderStub.BF_BindInt, "count"), 0)
        self.assertFalse(self.cell(BinderStub.BF_BindBool, "has_item", 0))
        self.assertEqual(self.cell(BinderStub.BF_BindString, "item_count", 0), "")
        self.assertEqual(self.port.unlistens, 1)
        # 重复关是安全的（`OnDestroy` 与 `OnDeactive` 都可能走到这里）。
        self.view.close()
        self.assertEqual(self.port.unlistens, 1)


class TestPatternMarkedCounts(PanelFixture):
    """带样板的条目：`stored+` / `+`（契约第 1 节）。

    无样板的条目**一条都不改**——它们仍按可用数显示，可用数 ≤ 1 留空。
    两套口径混在一起是这一步最容易出的错：把 `+` 的口径套到所有行上，
    表现是满屏「0+」；把可用数的口径套到样板行上，表现是零库存的样板行
    看起来跟空行一样，玩家根本不知道能下单。
    """

    def setUp(self):
        PanelFixture.setUp(self)
        # 三行的**样板与库存各占一种情形**：
        # 有库存有样板（`256+`）、零库存有样板（`+`）、有库存没样板（可用数）。
        self.snapshot([
            (STONE, 256, 0, True),
            (SAND, 0, 0, True),
            (DIRT, 5, 0, False),
        ])
        self.cache.tick()
        # **视图的行是它自己刷新出来的**：`cache.tick()` 只动模型，
        # 集合绑定读的是 `self.rows`——不刷的话断言的还是上一次快照的行。
        self.view.refresh()

    def test_a_stock_row_with_a_pattern_keeps_the_stored_number(self):
        """用 `stored` 而不是可用数：预留不该把 `256+` 变成 `254+`。

        User 指定的是「库存数量」，而标签本来就不承诺可取量——
        取物仍走可用数（见 `on_select`）。
        """
        self.snapshot([(STONE, 256, 2, True)])
        self.cache.tick()
        self.view.refresh()
        stone = self.names().index("minecraft:stone")
        self.assertEqual(
            self.cell(BinderStub.BF_BindString, "item_count", stone), "256+"
        )

    def test_one_item_still_shows_a_plus(self):
        """1 个也要显示 `1+`——`<= 1 留空` 那条只属于无样板口径。"""
        self.snapshot([(STONE, 1, 0, True)])
        self.cache.tick()
        self.view.refresh()
        stone = self.names().index("minecraft:stone")
        self.assertEqual(
            self.cell(BinderStub.BF_BindString, "item_count", stone), "1+"
        )

    def test_a_pattern_only_row_shows_a_bare_plus(self):
        """零库存有样板：`+`，**不是 `0+`**，而且这一行必须还在。"""
        sand = self.names().index("minecraft:sand")
        self.assertEqual(
            self.cell(BinderStub.BF_BindString, "item_count", sand), "+"
        )
        self.assertTrue(self.cell(BinderStub.BF_BindBool, "has_item", sand))

    def test_rows_without_a_pattern_keep_the_available_number(self):
        """无样板的老口径一字未改：可用数 5 显示 5，可用数 ≤ 1 留空。"""
        dirt = self.names().index("minecraft:dirt")
        self.assertEqual(
            self.cell(BinderStub.BF_BindString, "item_count", dirt), "5"
        )
        # 夹具原有的三行里 SAND 可用数为 1、DIRT 可用数为 0，都留空。
        self.snapshot([(SAND, 1, 0, False), (DIRT, 2, 2, False)])
        self.cache.tick()
        self.view.refresh()
        sand = self.names().index("minecraft:sand")
        dirt = self.names().index("minecraft:dirt")
        self.assertEqual(self.cell(BinderStub.BF_BindString, "item_count", sand), "")
        self.assertEqual(self.cell(BinderStub.BF_BindString, "item_count", dirt), "")

    def test_a_pattern_only_row_sorts_as_zero_and_survives_a_counting_sort(self):
        """`+` 单独存在的行按 0 排序，不能因为没有数量就被排到最后之外。"""
        self.view.sort_field = "count"
        self.view.on_sort_method()
        self.cache.tick()
        self.view.refresh()
        self.assertIn("minecraft:sand", self.names(), "零库存的样板行要留在列表里")


class TestLongPressOrder(PanelFixture):
    """长按带 `+` 的网络条目 = 开数量窗下单（11 增量，契约第 2 节）。

    **数量窗本身被换成了替身**：它要 `screen.AddElement`，而这套夹具的屏
    是个只认得绑定表的替身。这一组要钉的是**手势那一半**——按下记了什么、
    抬起怎么判、短按有没有被吞掉；窗口怎么建在
    `test_number_set_window.py` 里各自有一份。
    """

    PATTERN = "minecraft:iron_ingot"

    def setUp(self):
        PanelFixture.setUp(self)
        # 时钟注入：用例不必真的等 0.4 秒（同 `inv_split` 的长按用例）。
        self.now = [1000.0]
        self.view._clock = lambda: self.now[0]
        self.opened = []
        patcher = patch.object(
            terminal_panel, "show_number_set_window", self._fake_window)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 一行有样板（`+`）、一行没有。
        self.snapshot([
            (STONE, 0, 0, True),
            (IRON, 5, 0, False),
        ])
        self.cache.tick()
        self.view.refresh()

    def _fake_window(self, screen, number, callback, **kwargs):
        self.opened.append({"screen": screen, "number": number,
                            "callback": callback, "kwargs": kwargs})
        return True

    #: 比门槛多留一截：`1000.0 + 0.4` 减 `1000.0` 是 `0.39999999999986`，
    #: 拿门槛当推进量会让用例测的是浮点舍入。
    HOLD = terminal_panel.LONG_PRESS_SECONDS + 0.1

    def index_of(self, name):
        return self.names().index(name)

    def press(self, ui_index, hold=None):
        # type: (int, object) -> None
        """按下再抬起同一行；`hold` 是两次之间流逝的秒数。"""
        params = {"#collection_index": ui_index}
        self.bind(BinderStub.BF_ButtonClickDown, "select", params)
        if hold is not None:
            self.now[0] += hold
        self.bind(BinderStub.BF_ButtonClickUp, "select", params)

    def test_a_long_press_on_a_plus_row_opens_the_quantity_window(self):
        index = self.index_of(STONE.name)
        self.press(index, hold=self.HOLD)
        self.assertEqual(len(self.opened), 1, "长按该开一次数量窗")
        self.assertEqual(self.opened[0]["number"], 1)
        self.assertEqual(self.opened[0]["kwargs"]["min_number"], 1)
        self.assertEqual(self.commands(), [], "开窗那一下不发任何命令")
        self.assertIsNone(self.backpack.selected, "也不能顺手选中")

    def test_the_confirmed_quantity_asks_for_a_preview(self):
        index = self.index_of(STONE.name)
        self.press(index, hold=self.HOLD)
        # 这是**数量窗确认之后**的那一步：开窗、玩家填数、点确认。
        self.opened[0]["callback"](7)
        command = self.take_command()
        self.assertEqual(command["action"], "preview_request")
        self.assertEqual(command["count"], 7)
        self.assertEqual(command["key"]["name"], STONE.name)
        self.assertTrue(self.view.preview.model.loading(), "预览窗立刻进入 loading")

    def test_a_short_press_on_a_plus_row_is_still_a_plain_click(self):
        """按一下就松开还是**选中**：长按是另一个手势，不能顺手接管短按。"""
        index = self.index_of(STONE.name)
        self.press(index, hold=terminal_panel.LONG_PRESS_SECONDS - 0.01)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.view.cache.model.selected_key(), STONE)

    def test_a_long_press_on_a_row_without_a_pattern_is_a_plain_click(self):
        """**不带 `+` 的条目不触发下单入口**（契约第 2 节）。"""
        index = self.index_of(IRON.name)
        self.press(index, hold=self.HOLD)
        self.assertEqual(self.opened, [])
        self.assertEqual(self.view.cache.model.selected_key(), IRON)

    def test_the_pressed_key_is_remembered_not_the_index(self):
        """**按下时记住完整资源键**（契约第 2 节明写）。

        中间夹一次重排：抬起时那一行已经换了人。按下时记行号的话，玩家
        对着 A 长按、下单买下的是 B——而界面上什么异常都没有。
        """
        index = self.index_of(STONE.name)
        params = {"#collection_index": index}
        self.bind(BinderStub.BF_ButtonClickDown, "select", params)
        # 重排之后同一个下标换成了另一行。
        self.view.sort_field = SORT_NAME
        self.view.sort_descending = not self.view.sort_descending
        self.view._apply_sort()
        self.assertNotEqual(self.names()[index], STONE.name, "这一行真的换了人")
        self.now[0] += self.HOLD
        self.bind(BinderStub.BF_ButtonClickUp, "select", params)
        self.assertEqual(len(self.opened), 1)
        self.opened[0]["callback"](1)
        self.assertEqual(self.take_command()["key"]["name"], STONE.name)

    def test_a_long_press_without_a_session_does_nothing(self):
        """没有会话时连数量窗都不该开：开了也只会转圈。"""
        self.client.drop_session()
        self.press(self.index_of(STONE.name), hold=self.HOLD)
        self.assertEqual(self.opened, [])

    def test_losing_the_list_view_drops_the_press(self):
        """视图拆了之后抬起不该再开窗（记录要跟着一起清）。"""
        self.view._row_press = (STONE, True, self.now[0])
        self.view.close()
        self.view.on_select({"#collection_index": 0})
        self.assertEqual(self.opened, [])


class TestBackpackCellBindings(PanelFixture):
    """下半屏 36 格的集合绑定：名字、旗标、集合三者错一个，格子就永远空着。

    **这里一格控件都不碰**：格子由引擎按集合生成、滚动时还会被回收换出，
    本类只是逐格回调的取值方。早先的实现按控件路径去摸格子
    （`grid.GetGridItem`），而格子要到**渲染**时才生成，`OnCreate` 第一步
    就抛——那正是整块改成绑定的原因。
    """

    #: 四个字段各自的引擎绑定类型。**必须与 JSON 里 `binding_type` 对得上**：
    #: 类型错了引擎会按另一种来解析同一份数据（数字当布尔之类），而界面上
    #: 只表现为「这一格看着不对」。
    FLAGS = {
        "visible": BinderStub.BF_BindBool,
        "item_id_aux": BinderStub.BF_BindInt,
        "selected": BinderStub.BF_BindBool,
        "item_count": BinderStub.BF_BindString,
    }

    def panel_with(self, *entries):
        # type: (tuple) -> object
        """按引擎槽序摆好背包，再建面板并跑 `OnCreate`。

        **先摆再建**：面板的 `OnCreate` 会先读一次背包，摆晚了第一帧全是空的
        （而格子是引擎每帧来取，之后自然会跟上）。
        """
        self.engine_backpack(*entries)
        self.panel_screen = Screen()
        panel = terminal_panel.CommonTerminalPanel("main", self.panel_screen, {})
        panel.OnCreate()
        self.addCleanup(panel.OnDestroy)
        return panel

    def cell(self, prefix, suffix, index):
        # type: (str, str, object) -> object
        collection = (terminal_panel.BACKPACK_COLLECTION if prefix == "backpack"
                      else terminal_panel.HOTBAR_COLLECTION)
        key = (self.FLAGS[suffix], collection,
               terminal_panel.slot_binding(prefix, suffix))
        return self.panel_screen.collection_bindings[key](index)

    def test_every_segment_registers_the_four_fields_of_the_shared_template(self):
        """八个字段一条都不能少：漏一条，那一格就永远空着。

        引擎查不到绑定**不报错**，只是安静地给假值——而 `#item_id_aux` 的假值
        是 0，正好是「空格子」的样子。因此这条只能在离线钉住。
        """
        self.panel_with()
        for prefix, collection in (("backpack", terminal_panel.BACKPACK_COLLECTION),
                                   ("hotbar", terminal_panel.HOTBAR_COLLECTION)):
            for suffix in terminal_panel.SLOT_FIELDS:
                self.assertIn((self.FLAGS[suffix], collection,
                               terminal_panel.slot_binding(prefix, suffix)),
                              self.panel_screen.collection_bindings)
        # 两段各一套名字，**一格不多一格不少**。登记表里还有上半屏那四条
        # （`terminal_slots` 集合），按集合名滤掉——不滤的话这条断言等于在数
        # 另一个集合的条数。
        cell_keys = [key for key in self.panel_screen.collection_bindings
                     if key[1] in (terminal_panel.BACKPACK_COLLECTION,
                                   terminal_panel.HOTBAR_COLLECTION)]
        self.assertEqual(len(cell_keys), len(terminal_panel.SLOT_FIELDS) * 2)

    def test_the_hotbar_cells_read_the_hotbar_not_the_backpack(self):
        """两段各自加上自己的首槽，而且**不能一起指向最后一段**。

        闭包写错（循环里定义的几个函数共享同一个变量）时两段都会按快捷栏去
        读：背包那 27 格显示的全是快捷栏的内容，而界面照常渲染、不报任何错。
        """
        self.panel_with((35, "minecraft:stone", 2), (8, "minecraft:apple", 7))
        # 两个 id_aux 必须不同，否则这条用例可以「都相等」地绿掉。
        self.assertNotEqual(stack_size.id_aux("minecraft:stone"),
                            stack_size.id_aux("minecraft:apple"))
        # 界面 26（背包最后一格）= 引擎 35 = 石头；界面 35（快捷栏最后一格）
        # = 引擎 8 = 苹果。串了段的话两个断言会互换。
        self.assertEqual(self.cell("backpack", "item_id_aux", 26),
                         stack_size.id_aux("minecraft:stone"))
        self.assertEqual(self.cell("hotbar", "item_id_aux", 8),
                         stack_size.id_aux("minecraft:apple"))
        self.assertEqual(self.cell("backpack", "item_id_aux", 25), 0)

    def test_selection_count_and_visibility_follow_the_panel_state(self):
        """选中、堆叠数、可见性都从面板那一份状态现读，不缓存第二份。"""
        panel = self.panel_with((35, "minecraft:stone", 2), (34, "minecraft:sand", 1))
        # 界面 24（引擎 33）是空的。空格子**也要画**：空是用
        # `#item_id_aux == 0` 表达的，不是靠隐藏。
        self.assertTrue(self.cell("backpack", "visible", 24))
        self.assertEqual(self.cell("backpack", "item_id_aux", 24), 0)
        # 越界的集合内下标报「没选中」——不能因为「两个都是 None」而报成选中。
        for bad in (9, 27, -1, "0", True):
            self.assertFalse(self.cell("hotbar", "selected", bad))
        # 数量 > 1 才显示数字（同上半屏）：满屏的「1」只是噪声。界面 25
        # （引擎 34）有东西但只有 1 个，因此留空——它同时钉住「留空不等于空格」。
        self.assertEqual(self.cell("backpack", "item_count", 26), "2")
        self.assertNotEqual(self.cell("backpack", "item_id_aux", 25), 0)
        self.assertEqual(self.cell("backpack", "item_count", 25), "")
        self.assertEqual(self.cell("backpack", "item_count", 24), "")
        panel.OnBackpackClick({"#collection_index": 26})
        self.assertTrue(self.cell("backpack", "selected", 26))
        self.assertFalse(self.cell("backpack", "selected", 25))
        # 两段共用一个槽位空间：界面 26 是背包的最后一格，快捷栏没有第 26 格。
        self.assertFalse(self.cell("hotbar", "selected", 26))

    def test_polling_hands_the_new_backpack_to_the_engine_by_itself(self):
        """轮询重读之后**不需要通知界面**：引擎每渲染帧自己来取。

        可见后果是「背包的变化在 N 帧内自己出现」。反过来，谁要是把重画写成
        「只重画变化的那几格」，就得重新持有格子控件——那正是被删掉的那套。
        """
        panel = self.panel_with((35, "minecraft:stone", 2))
        self.put_engine(34, "minecraft:sand", 5)
        self.assertEqual(self.cell("backpack", "item_id_aux", 25), 0)
        for _ in range(terminal_panel.BACKPACK_POLL_FRAMES):
            panel._poll_backpack()
        self.assertEqual(self.cell("backpack", "item_id_aux", 25),
                         stack_size.id_aux("minecraft:sand"))
        self.assertEqual(self.cell("backpack", "item_count", 25), "5")

    def test_destroy_still_leaves_a_binding_the_engine_can_call(self):
        """销毁**不摘绑定**：它写在类体里，跟着界面类走、不跟会话走。

        界面关掉之后引擎仍可能再问一帧，那时回调不能抛——抛出去就是游戏里
        那种「每渲染一帧一条 `AttributeError`」的刷屏。选中态倒是必须清掉：
        它是实例状态，留着只会在下次打开时冒出来指向一个早就不存在的东西。
        """
        panel = self.panel_with((35, "minecraft:stone", 2))
        panel.OnBackpackClick({"#collection_index": 26})
        self.assertEqual(panel.backpack.selected, 26)
        panel.OnDestroy()
        self.assertIsNone(panel.backpack.selected)
        self.assertTrue(self.cell("backpack", "visible", 26))
        self.assertFalse(self.cell("backpack", "selected", 26))
        # 重复销毁是安全的（`OnDestroy` 与 `OnDeactive` 都可能走到这里）。
        panel.OnDestroy()


class TestManualTransfer(PanelFixture):
    """点终端行**只选中**，不搬东西（2026-10-01 改定）。

    原先那条「一行点击 = 一条 `inv_put`，数量取一组」的默认**撤掉了**：
    现在两侧对称——**选中一侧、再点另一侧才搬**。取网络里东西的那半边走
    `TestBackpackPut`（点背包格），存进网络的那半边仍是 `TestBackpackHandoff`
    （点终端行，但前提是背包里已经选了一格）。

    手势口径改一次，这里的断言跟着改一次；这些用例就是那条口径的离线落点。
    """

    def test_clicking_a_row_only_selects_it(self):
        """**一条命令都不发。** 这是本次改定的全部要点：点行只是选中。"""
        before = len(self.commands())
        index = self.names().index("minecraft:stone")
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": index})
        self.assertEqual(len(self.commands()), before)
        self.assertEqual(self.cache.model.selected_key(), self.view.rows[index].key)

    def test_click_selects_the_row_and_the_selection_follows_the_cell_binding(self):
        index = self.names().index("minecraft:sand")
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": index})
        self.assertEqual(self.cache.model.selected_key(), self.view.rows[index].key)
        self.assertTrue(self.cell(BinderStub.BF_BindBool, "selected", index))
        self.assertFalse(self.cell(BinderStub.BF_BindBool, "selected", 1 - index))

    def test_clicking_the_same_row_again_clears_the_selection(self):
        """同一条再点 = 取消选中（与背包侧「同格再点取消」对称）。"""
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        self.assertIsNotNone(self.cache.model.selected_key())
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        self.assertIsNone(self.cache.model.selected_key())

    def test_a_bad_index_neither_selects_nor_sends(self):
        before = len(self.commands())
        for invalid in (-1, 3, 99, None, True, "0", 1.0):
            self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": invalid})
        self.assertEqual(len(self.commands()), before)
        self.assertIsNone(self.cache.model.selected_key())

    def test_clicking_a_row_while_a_backpack_slot_is_selected_still_takes(self):
        """背包里选了一格时，点终端行**仍然是 `inv_take`**——两半手势靠
        「背包有没有选中」分流，顺序反了就会把玩家的点击改判成「选中网络行」。"""
        self.engine_backpack((9, "minecraft:stone", 40))
        self.backpack.select(0)
        before = len(self.commands())
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        self.assertEqual(self.commands()[before:][0]["action"], "inv_take")


class TestBackpackPut(PanelFixture):
    """网络里选中一行 → 点背包某一格 = `inv_put` **带槽锚**（放进／合堆）。

    数量仍是**一组**：`min(可用数, maxStackSize)`，再由服务端按那一格装得
    下的量截一次（`probe_space`）。异种由服务端的锚核对拒掉（`SLOT_CHANGED`），
    客户端**不做本地判断**——那是两个真相源的老路。
    """

    def put(self, ui_index):
        # type: (int) -> None
        """替玩家点一下下半屏的某个格子（此时网络那边已经选好了一行）。"""
        self.view.on_backpack_put(ui_index)

    def select_row(self, name="minecraft:stone"):
        index = self.names().index(name)
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": index})
        return index

    def test_the_command_carries_the_target_slot_and_its_version(self):
        """带槽锚：`slot` 是**引擎槽号**（界面 0 号格 → 引擎槽 9），
        `slot_version` 取服务端那份快照——少一个就退回「随便找个格子放」。"""
        self.engine_backpack((9, "minecraft:stone", 40))
        index = self.select_row("minecraft:stone")
        before = len(self.commands())
        self.put(0)
        command = self.commands()[before:][0]
        self.assertEqual(command["action"], "inv_put")
        self.assertEqual(command["slot"], 9)
        self.assertEqual(command["slot_version"], 7)
        self.assertEqual(command["expected_index_version"], 7)
        self.assertEqual(command["key"], self.view.rows[index].key.to_dict())
        # **不发 `cursor`**：R12 未定，缺席不等于「手上没拿东西」。
        self.assertNotIn("cursor", command)

    def test_taking_one_stack_and_stopping_at_the_stack_size(self):
        """一组 = `min(可用数, maxStackSize)`，不是把这一种全拿走。

        STONE 可用 36；把它的堆叠上限压到 16，发出去的就该是 16。**必须
        在第一次用到这件物品之前设**：视图按资源名缓存 `GetBasicInfo()`，
        缓存一旦填上，同一条用例里再改 `stack_sizes` 就不生效了。
        """
        ItemStub.stack_sizes["minecraft:stone"] = 16
        self.addCleanup(ItemStub.stack_sizes.clear)
        self.engine_backpack((9, "minecraft:stone", 40))
        self.select_row("minecraft:stone")
        before = len(self.commands())
        self.put(0)
        self.assertEqual(self.commands()[before:][0]["count"], 16)

    def test_a_row_holding_less_than_a_stack_gives_what_it_has(self):
        """可用数不足一组时**能拿多少拿多少**：「取一组」的意思是「不超过
        一组」，不是「不够就一个都不给」。"""
        self.engine_backpack((9, "minecraft:stone", 40))
        self.select_row("minecraft:stone")
        before = len(self.commands())
        self.put(0)
        # STONE 可用 36 < 默认堆叠上限 64。
        self.assertEqual(self.commands()[before:][0]["count"], 36)

    def test_an_unusable_stack_size_falls_back_to_one(self):
        """引擎说不出堆叠上限时退成 **1**，**不是**可用数。

        退化的方向是刻意的：退成可用数的话，同一个点击会在引擎答不上来的
        时候变成「把这一种全抽干」——正是这个约定要避开的那件事。退成 1
        最多是少拿。
        """
        ItemStub.stack_sizes["minecraft:stone"] = None
        self.addCleanup(ItemStub.stack_sizes.clear)
        self.engine_backpack((9, "minecraft:stone", 40))
        self.select_row("minecraft:stone")
        before = len(self.commands())
        self.put(0)
        self.assertEqual(self.commands()[before:][0]["count"], 1)

    def test_a_selected_row_wins_over_backpack_internal_actions(self):
        """网络里选着一行时，点背包格走「放进这一格」，**不是背包内合并**。

        两条路都从「点背包格」进，靠 `_click` 的三选一优先级分流。顺序错了
        的表现很具体：**玩家想把东西放进去，结果两个背包格自己换了个位**。
        """
        self.engine_backpack((9, "minecraft:stone", 40), (10, "minecraft:dirt", 3))
        self.select_row("minecraft:stone")
        before = len(self.commands())
        # 走**真正的入口**（面板的 `_click` 三选一），不是直接调 `on_backpack_put`
        # ——分流顺序正是这条用例要钉的东西。
        self.panel.OnBackpackClick({"#collection_index": 1})
        self.assertEqual(self.commands()[before:][0]["action"], "inv_put")
        self.assertEqual(self.commands()[before:][0]["slot"], 10)

    def test_no_row_selected_means_nothing_happens(self):
        """没有选中任何行时点背包格**不发命令**——那一下是「选中这一格」，
        由面板的 `_click` 分流，不归这里。"""
        self.engine_backpack((9, "minecraft:stone", 40))
        before = len(self.commands())
        self.put(0)
        self.assertEqual(len(self.commands()), before)

    def test_no_server_snapshot_means_wait_for_sync(self):
        """拿不到那一槽的服务端快照就**等待同步**：槽版本是锚上的一关。"""
        self.engine_backpack((9, "minecraft:stone", 40))
        self.client.inventory_slots = {}
        self.select_row("minecraft:stone")
        before = len(self.commands())
        self.put(0)
        self.assertEqual(len(self.commands()), before)

    def test_a_row_with_nothing_available_sends_no_command(self):
        """可用数为 0 时**不发命令**：`inv_put` 的 `count` 必须为正，发一个 0
        只会换来一条「参数越界」，而玩家什么也没做错。"""
        self.engine_backpack((9, "minecraft:stone", 40))
        self.snapshot([(STONE, 40, 4, False), (SAND, 3, 3, False), (DIRT, 2, 0, False)],
                      version=SNAPSHOT_VERSION + 1, snapshot_id="snap-2")
        # **走 `view.tick()` 而不是 `cache.tick()`**：视图读的是自己那份行快照，
        # 只推缓存不重画的话，点击仍按旧数字去发命令。
        self.view.tick()
        sand = "minecraft:sand"
        self.select_row(sand)
        before = len(self.commands())
        self.put(0)
        self.assertEqual(len(self.commands()), before)

    def test_receipts_are_taken_off_the_client_every_frame(self):
        """回执**必须每帧取走**：不取的话 `NetworkClient._results` 只涨不落。

        回执目前没有显示位置（`plans/ui/common_terminal_panel.md` 的待确认段
        记了这一缺项），因此取走之后只留最近几条供诊断——留最近几条而不是
        全留，是因为「回执没有显示位置」不该顺便变成一个无上限的内存增长。
        """
        self.feed(CommandResultMessage("req-1", self.result("req-1")))
        self.assertEqual(self.client.pending_results(), 1)
        self.view.tick()
        self.assertEqual(self.client.pending_results(), 0)
        self.assertEqual(self.view.receipts[0]["request_id"], "req-1")
        for index in range(terminal_panel.RECEIPT_MEMORY + 4):
            request_id = "req-%d" % (index + 2)
            self.feed(CommandResultMessage(request_id, self.result(request_id)))
        self.view.tick()
        self.assertEqual(len(self.view.receipts), terminal_panel.RECEIPT_MEMORY)
        self.assertEqual(self.view.receipts[-1]["request_id"],
                         "req-%d" % (terminal_panel.RECEIPT_MEMORY + 5))


class TestBackpackHandoff(PanelFixture):
    """选中背包槽 → 点终端行 = `inv_take`（背包 → 网络），数量是**一组**。

    这条路的每一步都有一种「静默出错」的坏法，因此每条用例都对着一个具体
    后果：动作名错了是「越点东西越多」，槽号错了是**搬走玩家没点的那一格**
    （服务端指纹核对通过、离线全绿、什么都不报），把 key 冻进选中态是拿旧
    快照冒充真身，回执不认账则会让玩家在同一条死路上反复点。

    方向以网络为参照（`server/storage/service.py` 的动作表）：存进去是
    `inv_take`，取出来是 `inv_put`。
    """

    def select(self, ui_index):
        # type: (int) -> None
        """替玩家点一下下半屏的某个格子。"""
        self.assertEqual(self.backpack.select(ui_index), ui_index)

    def test_no_server_snapshot_does_not_bypass_version_check(self):
        self.engine_backpack((9, "minecraft:stone", 40))
        self.client.inventory_slots = {}
        self.select(0)
        before = len(self.commands())
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        self.assertEqual(len(self.commands()), before)

    def test_mismatched_server_fingerprint_waits_for_sync(self):
        self.engine_backpack((9, "minecraft:stone", 40))
        self.client.inventory_slots[9]["canonical"] = "different"
        self.select(0)
        before = len(self.commands())
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        self.assertEqual(len(self.commands()), before)

    def test_a_selected_slot_turns_the_row_click_into_an_inv_take(self):
        # 界面 0 号格 → 引擎槽 9（引擎的顺序是快捷栏在前）。这条换算错了，
        # 下面的 slot 断言是唯一的离线拦截点。
        self.engine_backpack((9, "minecraft:stone", 40))
        self.select(0)
        before = len(self.commands())
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        command = self.commands()[before:][0]
        self.assertEqual(command["action"], "inv_take")
        self.assertEqual(command["slot"], 9)
        self.assertEqual(command["count"], 40)
        self.assertEqual(command["expected_index_version"], 7)
        self.assertIsNotNone(command["key"])
        # **不发 `cursor`**：R12 未定，缺席不等于「手上没拿东西」。
        self.assertNotIn("cursor", command)
        self.assertEqual(command["slot_version"], 7)

    def test_the_deposit_is_capped_at_one_stack(self):
        """存一组 = `min(该槽数量, maxStackSize)`，不是把这一种全抽干。"""
        ItemStub.stack_sizes["minecraft:stone"] = 16
        self.addCleanup(ItemStub.stack_sizes.clear)
        stack_size.reset()
        self.engine_backpack((9, "minecraft:stone", 40))
        self.select(0)
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        self.assertEqual(self.take_command()["count"], 16)

    def test_a_selected_hotbar_slot_maps_to_the_low_engine_slots(self):
        """界面下方那一排（27–35）对应引擎的 **0–8**，不是接在上面后面。"""
        self.engine_backpack((0, "minecraft:apple", 5))
        self.select(27)
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        command = self.take_command()
        self.assertEqual(command["action"], "inv_take")
        self.assertEqual(command["slot"], 0)
        self.assertEqual(command["count"], 5)

    def test_without_a_selection_the_row_click_sends_nothing(self):
        """**没选中背包槽时点终端行不再搬东西**（2026-10-01 改定）。

        原先这一条断言的是「发 `inv_put`」——方向取网络 → 背包。那条默认
        撤掉之后，同一半手势改从**背包那一侧**发起：先选中网络里的一行，
        再点背包某一格。见 `TestBackpackPut`。
        """
        before = len(self.commands())
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        self.assertEqual(len(self.commands()), before)

    def test_a_slot_emptied_after_selection_sends_nothing(self):
        """选中之后东西被别处拿走了：**什么都不发**，并且清掉选中。

        绝不能落到底下的 `inv_put`——玩家选的是背包，从网络里取东西给他
        不是他要的那个动作。本地就发现，比发出去换一张 `SLOT_CHANGED`
        回执要好：那也是玩家的那一下点击被拒。
        """
        self.engine_backpack((9, "minecraft:stone", 40))
        self.select(0)
        self.clear_engine(9)
        before = len(self.commands())
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        self.assertEqual(len(self.commands()), before)
        self.assertIsNone(self.backpack.selected)

    def test_the_slot_is_reread_before_sending_not_frozen_at_selection(self):
        """发的是**重读之后**的那一格，不是选中那一刻的快照。

        选中 40 个，随后别处拿走一半：发出去的必须是 20，而不是 40——后者
        会被服务端按「来源没有可动的」拒掉，而玩家明明看得见东西在那里。
        """
        self.engine_backpack((9, "minecraft:stone", 40))
        self.select(0)
        self.put_engine(9, "minecraft:stone", 20)
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        self.assertEqual(self.take_command()["count"], 20)

    def test_the_selected_slot_being_replaced_drops_the_selection(self):
        """选中的那一格**换了东西**就把选中丢掉。

        否则下一次点击会拿一件玩家没选过的物品去交接——而这正是按槽取放
        要避免的那种错。
        """
        self.engine_backpack((9, "minecraft:stone", 40))
        self.select(0)
        self.put_engine(9, "minecraft:dirt", 3)
        self.assertEqual(self.backpack.replace_from_engine(), [0])
        self.assertIsNone(self.backpack.selected)

    def test_another_slot_changing_does_not_drop_the_selection(self):
        """别处抖一下就吞掉玩家的选中，玩家会觉得界面在自己乱动。"""
        self.engine_backpack((9, "minecraft:stone", 40), (10, "minecraft:dirt", 3))
        self.select(0)
        self.put_engine(10, "minecraft:dirt", 9)
        self.assertEqual(self.backpack.replace_from_engine(), [1])
        self.assertEqual(self.backpack.selected, 0)

    # -- 回执 ----------------------------------------------------------

    def send_a_take(self):
        # type: () -> str
        """走一遍「选中背包槽 + 点终端行」，返回那条命令的 `request_id`。"""
        self.engine_backpack((9, "minecraft:stone", 40))
        self.select(0)
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        return self.sole_in_flight()

    def test_an_ok_receipt_clears_the_selection(self):
        request_id = self.send_a_take()
        self.feed(CommandResultMessage(request_id, self.result(request_id)))
        self.view.tick()
        self.assertIsNone(self.backpack.selected)
        self.assertEqual(self.in_flight(), set())

    def test_a_rejected_receipt_clears_the_selection_and_rereads(self):
        """被拒时**也要清**，而且立刻重读。

        不清的话玩家会在同一条死路上反复点（每次都被同一道检查拒掉，而
        界面上看不出为什么）；不重读的话他会继续看着一个引擎里已经不存在
        的东西。
        """
        request_id = self.send_a_take()
        self.clear_engine(9)
        self.feed(CommandResultMessage(request_id, self.rejected(request_id)))
        self.view.tick()
        self.assertIsNone(self.backpack.selected)
        self.assertEqual(self.in_flight(), set())
        # 重读过了：那一格已经在本地空掉了。
        self.assertIsNone(self.backpack.entry(0))

    def test_a_late_receipt_does_not_wipe_a_selection_the_player_remade(self):
        """**F1 的回归。**

        回执只该清**发命令时**的那个选中。原来的写法是「回执一到就
        `clear_selection()`」——清的是**收到回执那一刻**的选中。玩家在在途
        窗口里改主意之后，那条迟到的回执会把**新选中**一并抹掉，而后果不是
        「高亮没了」这么轻：下一次点终端行会落到 `inv_put`（网络 → 背包），
        **方向与玩家的意图相反**。

        这条走的是最刁的那一种：取消之后**又选中同一格**。槽号与发命令时
        一模一样，因此拿槽号去比是拦不住的——必须比修订号。
        """
        request_id = self.send_a_take()
        self.backpack.clear_selection()
        self.backpack.select(0)               # 又选回同一格
        self.assertEqual(self.backpack.selected, 0)
        self.feed(CommandResultMessage(request_id, self.result(request_id)))
        self.view.tick()
        self.assertEqual(self.backpack.selected, 0, "新选中不能被迟到回执抹掉")

    def test_a_receipt_for_someone_elses_command_leaves_the_selection_alone(self):
        """回执队列里还有别的命令。按顺序猜会把别人的结果算到自己头上。"""
        request_id = self.send_a_take()
        self.feed(CommandResultMessage("c999", self.rejected("c999")))
        self.view.tick()
        self.assertEqual(self.backpack.selected, 0)
        self.assertIn(request_id, self.in_flight())

    def test_sending_alone_does_not_clear_the_selection(self):
        """**发送即清是不行的**：被拒时会把玩家的选中白白吞掉，而他什么都
        没做错。回执就是用来区分这两种结局的。

        **这条只对取放成立。** 背包内的交换/合并是另一套：它们的结果已经
        落在虚拟空间上了，因此发送时就清选中（见 `TestBackpackExchange`）。
        """
        request_id = self.send_a_take()
        self.assertIn(request_id, self.in_flight())
        self.assertEqual(self.backpack.selected, 0)


class TestBackpackExchange(PanelFixture):
    """背包槽之间：**合并（`inv_merge`）与交换（`inv_swap`），都不经过网络。**

    2026-09-30 定案：已经选中一个槽之后再点**另一个**槽，这一下是背包内部的
    动作，不再是改选中。三类坏法各有各的后果——

    - 动作名错了（发成 `inv_take`）是**把东西悄悄存进网络**；
    - 先跑 `select()` 再判断，会把选中挪到目标格上，于是拿它当源槽用，
      **搬走玩家没点的那一格**（服务端指纹核对通过、离线全绿、什么都不报）；
    - 合并的数量不封口，则每合并一次报一次「部分成功」。

    方向以网络为参照，与 `TestBackpackHandoff` 同一套：这两条**都不带方向**，
    它们只在背包内部发生。
    """

    def click(self, ui_index):
        # type: (int) -> None
        """替玩家点一下下半屏的某个格子——**走真正的入口**（`_click`）。"""
        if ui_index < backpack_model.BACKPACK_COUNT:
            self.panel.OnBackpackClick({"#collection_index": ui_index})
        else:
            self.panel.OnHotbarClick(
                {"#collection_index": ui_index - backpack_model.HOTBAR_START}
            )

    def exchange(self, source_ui, target_ui):
        # type: (int, int) -> dict
        """选中 `source_ui` 再点 `target_ui`，返回这一下发出去的那条命令。"""
        self.click(source_ui)
        self.assertEqual(self.backpack.selected, source_ui)
        before = len(self.commands())
        self.click(target_ui)
        sent = self.commands()[before:]
        self.assertEqual(len(sent), 1, "这一下应当恰好发一条命令")
        return sent[0]

    # -- 判定 ----------------------------------------------------------

    def test_a_second_click_on_a_different_item_swaps(self):
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3))
        command = self.exchange(0, 1)
        self.assertEqual(command["action"], "inv_swap")
        # 界面 0/1 → 引擎 9/10。这条换算错了就是「换了玩家没点的那两格」。
        self.assertEqual(command["slot"], 9)
        self.assertEqual(command["into_slot"], 10)
        self.assertEqual(command["slot_version"], 7)
        self.assertEqual(command["into_slot_version"], 7)
        self.assertIsNotNone(command["key"])
        # **不发 `cursor`**：这次定的手势不产生持有物（R12 的替身口径不变）。
        self.assertNotIn("cursor", command)

    def test_the_same_item_merges_instead_of_swapping(self):
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:stone", 3))
        command = self.exchange(0, 1)
        self.assertEqual(command["action"], "inv_merge")
        self.assertEqual(command["count"], 5)
        self.assertEqual(command["into_slot"], 10)

    def test_a_merge_gives_only_what_the_target_still_has_room_for(self):
        """**数量在这里就封好**，不是整槽发出去让服务端去截。

        不封口的表现是每合并一次报一次 `PARTIAL`（服务端先取后放、放不进的
        退回原槽）——而玩家看到的是东西都进对了。
        """
        ItemStub.stack_sizes["minecraft:stone"] = 8
        self.addCleanup(ItemStub.stack_sizes.clear)
        self.engine_backpack((9, "minecraft:stone", 6), (10, "minecraft:stone", 5))
        # 目标还剩 3 格空间，源有 6 个：这一下只搬得动 3 个。
        self.assertEqual(self.exchange(0, 1)["count"], 3)

    def test_an_empty_target_swaps_instead_of_needing_a_sixth_action(self):
        """与空槽交换**就是移动**——那是交换的一个特例，不是第六个动作。"""
        self.engine_backpack((9, "minecraft:stone", 5))    # 界面 1（引擎 10）是空的
        command = self.exchange(0, 1)
        self.assertEqual(command["action"], "inv_swap")
        self.assertEqual(command["into_slot"], 10)

    def test_an_exchange_never_goes_through_the_network(self):
        """这两条**不带方向**：发成 `inv_take` 就是把背包里的东西悄悄存进网络。"""
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3))
        command = self.exchange(0, 1)
        self.assertNotIn(command["action"], ("inv_take", "inv_put"))
        self.assertIsNone(command.get("target"))

    # -- 不该发命令的几种情形 ------------------------------------------

    def test_clicking_the_same_slot_cancels_instead_of_swapping(self):
        self.engine_backpack((9, "minecraft:stone", 5))
        self.click(0)
        before = len(self.commands())
        self.click(0)
        self.assertEqual(len(self.commands()), before)
        self.assertIsNone(self.backpack.selected)

    def test_a_full_target_of_the_same_item_sends_nothing(self):
        """同类但目标满了：搬不动。发出去只会换一张 `NO_CAPACITY` 回执，
        而玩家什么也没做错——**选中也要留着**，他可能想点的是别的格子。"""
        ItemStub.stack_sizes["minecraft:stone"] = 4
        self.addCleanup(ItemStub.stack_sizes.clear)
        self.engine_backpack((9, "minecraft:stone", 3), (10, "minecraft:stone", 4))
        self.click(0)
        before = len(self.commands())
        self.click(1)
        self.assertEqual(len(self.commands()), before)
        self.assertEqual(self.backpack.selected, 0)

    def test_without_a_server_snapshot_the_exchange_waits_for_sync(self):
        """槽版本没有权威来源时不发命令——**不得用本地计数或空值绕过校验**。"""
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3))
        self.client.inventory_slots = {}
        self.click(0)
        before = len(self.commands())
        self.click(1)
        self.assertEqual(len(self.commands()), before)

    def test_a_mismatched_source_fingerprint_waits_for_sync(self):
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3))
        self.client.inventory_slots[9]["canonical"] = "different"
        self.click(0)
        before = len(self.commands())
        self.click(1)
        self.assertEqual(len(self.commands()), before)

    # -- 回执 ----------------------------------------------------------

    def test_an_exchange_receipt_clears_the_selection(self):
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3))
        self.exchange(0, 1)
        request_id = self.sole_in_flight()
        self.feed(CommandResultMessage(request_id, self.result(request_id)))
        self.view.tick()
        self.assertIsNone(self.backpack.selected)
        self.assertEqual(self.in_flight(), set())

    def test_a_rejected_exchange_clears_the_selection_and_rereads(self):
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3))
        self.exchange(0, 1)
        request_id = self.sole_in_flight()
        self.clear_engine(9)
        self.feed(CommandResultMessage(request_id, self.rejected(request_id)))
        self.view.tick()
        self.assertIsNone(self.backpack.selected)
        self.assertIsNone(self.backpack.entry(0), "重读过了：那一格已经在本地空掉")

    # -- 虚拟背包空间（多轮） ------------------------------------------

    def test_a_second_round_is_allowed_before_the_receipt(self):
        """**不挡连点。** 玩家在等服务端回执的那段时间里可以接着点，每一轮
        都作用在虚拟空间上；真值沿队列从头兑现。"""
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3),
                             (11, "minecraft:apple", 2))
        self.exchange(0, 1)
        self.click(1)
        self.click(2)
        self.assertEqual(len(self.commands()), 2, "两条都发出去了")
        self.assertEqual(len(self.in_flight()), 2, "两条都在途")
        self.assertEqual(self.backpack.pending_slots(), [0, 1, 2])
        self.assertEqual(self.backpack.entry(0).key.name, "minecraft:dirt")
        self.assertEqual(self.backpack.entry(1).key.name, "minecraft:apple")
        self.assertEqual(self.backpack.entry(2).key.name, "minecraft:stone")

    def test_a_pending_slot_sends_no_slot_version(self):
        """**本地改过的槽不发版本**（`expected_version=None`，含义是「不校验
        版本」，是 `SlotAnchor` 有意留的选项）。

        那几格在服务端那边的版本客户端算不出来——服务端什么时候读到、读了
        几次取决于它自己的时序，而且交换与合并不一样（交换只复核目标槽，
        合并两格各复核一次）。发一个猜的只会换来一次 `stale_version` 拒绝，
        而玩家什么都没做错。

        **没改过的槽照旧发真实版本**：版本那一关对它们仍然有效。
        """
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3),
                             (11, "minecraft:apple", 2))
        first = self.exchange(0, 1)
        self.assertEqual(
            (first["slot_version"], first["into_slot_version"]), (7, 7),
            "谁都没改过，照旧发真实版本",
        )
        # 现在界面 1 那一格有了本地操作在身。
        self.click(1)
        self.click(2)
        second = self.commands()[-1]
        self.assertEqual(second["slot"], 10)
        self.assertEqual(second["into_slot"], 11)
        self.assertIsNone(second["slot_version"], "源槽是本地改过的")
        self.assertEqual(second["into_slot_version"], 7, "目标槽没改过")

    def test_the_receipt_rereads_the_real_backpack(self):
        """回执到达 = 「可以去看真值了」的信号：**立刻重读一遍真实背包**。

        真值追上之后本地操作才退场，画面因此不会先跳回去再跳过来。
        """
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3))
        self.exchange(0, 1)
        # 服务端做完了，真实背包也变了（引擎读数追上来）。
        self.put_engine(9, "minecraft:dirt", 3)
        self.put_engine(10, "minecraft:stone", 5)
        request_id = self.sole_in_flight()
        self.feed(CommandResultMessage(request_id, self.result(request_id)))
        self.view.tick()
        self.assertFalse(self.backpack.has_pending(), "真值追上 → 本地操作退场")
        self.assertEqual(self.backpack.entry(0).key.name, "minecraft:dirt")
        self.assertEqual(self.backpack.entry(1).key.name, "minecraft:stone")

    def test_a_late_exchange_receipt_leaves_a_remade_selection_alone(self):
        """交换那条路同样（F1）。

        交换在**发送时**就把选中清掉了，因此回执回来时什么都不该做——更不该
        把玩家在那之后新建立的选中一并抹掉。
        """
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3))
        self.exchange(0, 1)
        request_id = self.sole_in_flight()
        self.click(0)                          # 玩家又选了一格，准备下一轮
        self.assertEqual(self.backpack.selected, 0)
        self.feed(CommandResultMessage(request_id, self.result(request_id)))
        self.view.tick()
        self.assertEqual(self.backpack.selected, 0, "迟到回执不能抹掉它")

    # -- 会话没了 / 服务端拒绝（F2 的回归）----------------------------

    def test_a_refusal_that_kills_the_session_settles_the_stuck_action(self):
        """**F2 的回归。**（2026-09-30 修）

        服务端判死会话时只回一条 `error`（不是回执），回执那一关永远等不到
        ——原先 `_action_requests` 与选中会一起悬着，而遮罩又不出（客户端还
        以为自己有会话）：界面看起来完全正常，点什么都做不了，点别的背包槽
        只会再发一条没有下文的命令。

        判据是**「会话还在不在」**，不是「收到了哪条错误码」。因此服务端换
        一种理由判死也一样管用，而客户端不必再维护一份「哪些原因码算会话级」
        的表——那份表一旦漂移，坏法恰恰是这种「看起来正常、点不动」。
        """
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3))
        self.exchange(0, 1)
        self.assertTrue(self.in_flight())
        self.assertTrue(self.backpack.has_pending(), "本地操作也悬着")
        self.assertFalse(self.view.is_unavailable())
        # 空会话字段 = 服务端在说「那个会话已经没了」。
        self.client.receive(SyncEnvelope(SyncError("x", "session_invalid")).to_dict())
        self.assertTrue(self.view.is_unavailable(), "遮罩要出")
        self.view.tick()
        self.assertEqual(self.in_flight(), set(), "在途动作要收尾")
        self.assertFalse(self.backpack.has_pending(), "本地操作也要撤")
        self.assertIsNone(self.backpack.selected, "选中不能赖着不走")
        # 收尾之后玩家点终端行是「从头开始」的一下，不会拿着死选中去发命令。
        before = len(self.commands())
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        self.assertEqual(len(self.commands()), before, "会话都没了，一条都不该发")

    def test_an_in_flight_error_that_keeps_the_session_still_settles(self):
        """**会话还活着的那一支**：服务端执行命令时抛了异常（`error` 带会话
        id）。它同样不会有回执，同样得给这次请求一个结局。

        把它当成会话判死是更糟的处置——「服务端一次内部错误 = 玩家界面掉线」。
        所以这里断言：会话**留着**，但那条在途动作收尾了。
        """
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3))
        self.exchange(0, 1)
        request_id = self.sole_in_flight()
        # `feed` 默认就带 s1/n1/e1——**非空的会话字段**正是这一支的标记。
        self.feed(SyncError(request_id, "internal"))
        self.assertTrue(self.cache.client.has_session(), "会话要留着")
        self.view.tick()
        self.assertEqual(self.in_flight(), set())
        self.assertIsNone(self.backpack.selected)

    def test_every_command_the_panel_sends_is_msgpack_encodable(self):
        """**真机撞过（2026-09-30）。** C2S 的信封是引擎用 msgpack 编的，编不了
        自定义对象。

        现场是：`key` 一路以 `ResourceKey` 实例传下去，`TerminalCommand.
        to_dict()` 把它**原样写进消息**，于是发包那一刻炸
        `can not serialize 'ResourceKey' object`——界面上什么都看不到。

        这条走的是**真实面板路径**（点击 → `send_command` → 线上字典），因此
        它连同「面板传进去的东西是什么」一起钉住了；只测 `send_command` 的
        话，将来有人在上面再包一层、又塞个对象进去就漏了。
        """
        try:
            import msgpack
        except ImportError:      # 引擎自带；离线环境没装就跳过
            self.skipTest("没有 msgpack，跳过编码检查")
        self.engine_backpack((9, "minecraft:stone", 40), (10, "minecraft:stone", 3))
        # 存进网络：`inv_take`（背包选中 → 点终端行）
        self.click(0)
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        # 取出网络：`inv_put` **带槽锚**（点终端行只选中 → 再点背包格）
        self.backpack.clear_selection()
        self.cache.model.clear_selection()
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        self.view.on_backpack_put(1)
        # 背包内合并：`inv_merge`。**两条选中都要清掉**——网络那一行还选着
        # 的话，点背包格会继续走「放进这一格」，而不是背包内合并。
        self.cache.model.clear_selection()
        self.backpack.clear_selection()
        self.click(0)
        self.click(1)
        actions = [body["action"] for body in self.commands()]
        self.assertIn("inv_take", actions)
        self.assertIn("inv_put", actions)
        self.assertIn("inv_merge", actions)
        for body in self.commands():
            msgpack.packb(body, use_bin_type=True)

    def test_an_error_for_someone_elses_request_is_ignored(self):
        """凭空冒出来的 `request_id` 不进队列：进了，界面下一次收尾就会拿
        别人的拒绝去撤自己的本地操作、清自己的选中。"""
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3))
        self.exchange(0, 1)
        request_id = self.sole_in_flight()
        self.feed(SyncError("nobody-sent-this", "internal"))
        self.view.tick()
        self.assertIn(request_id, self.in_flight(), "在途动作不受影响")
        self.assertTrue(self.backpack.has_pending(), "本地操作也不受别人影响")

    def test_sending_an_exchange_clears_the_selection_on_purpose(self):
        """**交换这一条与取放相反：发送时就清选中。**

        取放那条写着「发送即清是不行的——被拒时会把玩家的选中白白吞掉」，
        这里**故意不照做**：交换的结果**已经预渲染在画面上了**，留着一个
        指向源槽的高亮会让玩家以为动作还没发生、还能接着点。被拒时预测会
        被撤掉、两格回到引擎真值，玩家重新点一下就是——**画面从不撒谎**
        这一点没有变，变的是「什么时候承认动作做完了」。

        取放没有预测，那条纪律原样保留
        （`TestBackpackHandoff::test_sending_alone_does_not_clear_the_selection`）。
        """
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3))
        self.exchange(0, 1)
        self.assertTrue(self.in_flight())
        self.assertIsNone(self.backpack.selected)
        self.assertTrue(self.backpack.has_pending())


class RecordingAnimator(object):
    """只记不画的动画器替身。

    **动画本身测不了**（要看屏幕），但「哪一条路会发起动画、用的是哪件物品」
    在这里全都能钉住——而那正是接线出错时会静默失效的一半：动画不动看起来
    和「没触发」一模一样。
    """

    def __init__(self):
        self.calls = []

    def fly(self, key, source, target):
        self.calls.append((key, source, target))
        return True


class TestQuickStore(PanelFixture):
    """选中背包槽 → 点上方列表**空白背景** = `inv_quick_move`（整槽进网络）。

    手势由 User 定（2026-10-01）。它与「点终端行 = 一组」是两条不同的命令，
    因此这里的判据不是「数量更大」而是**动作名与 target**：发错动作的表现是
    网络里多出/少掉一槽东西，而界面上那两种点击看起来都「成功了」。
    """

    def select(self, ui_index):
        self.assertEqual(self.backpack.select(ui_index), ui_index)

    def quick_store(self, params=None):
        return self.bind(BinderStub.BF_ButtonClickUp, "quick_store", params)

    def test_a_selected_slot_is_moved_whole_as_inv_quick_move(self):
        # 一组是 64（`ItemStub` 的默认），这里放 200：整槽搬**不截断**，
        # 截断成一组的话网络里只会多出 64 个，而界面上看不出差别。
        ItemStub.stack_sizes["minecraft:stone"] = 64
        self.engine_backpack((9, "minecraft:stone", 200))
        self.select(0)
        before = len(self.commands())
        self.quick_store()
        command = self.commands()[before:][0]
        self.assertEqual(command["action"], "inv_quick_move")
        self.assertEqual(command["target"], "network")
        self.assertEqual(command["count"], 200, "整槽，不是一组")
        self.assertEqual(command["slot"], 9)
        self.assertEqual(command["slot_version"], 7)
        self.assertEqual(command["expected_index_version"], 7)
        self.assertIsNotNone(command["key"])
        # **不发 `cursor`**：R12 未定，缺席不等于「手上没拿东西」。
        self.assertNotIn("cursor", command)

    def test_the_whole_stack_differs_from_the_row_click_on_the_same_slot(self):
        """同一次选中，两种点击发出**不同的命令**——把两者写成一个，整槽
        那一半就永远是「点一下拿一组」。"""
        ItemStub.stack_sizes["minecraft:stone"] = 16
        self.addCleanup(ItemStub.stack_sizes.clear)
        stack_size.reset()
        self.engine_backpack((9, "minecraft:stone", 40))
        self.select(0)
        self.quick_store()
        whole = self.take_command()
        self.assertEqual(whole["action"], "inv_quick_move")
        self.assertEqual(whole["count"], 40)
        self.backpack.clear_selection()
        self.select(0)
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        row = self.take_command()
        self.assertEqual(row["action"], "inv_take")
        self.assertEqual(row["count"], 16)

    def test_without_a_selection_nothing_is_sent_and_nothing_is_chosen(self):
        """没有选中时这一下**没有目标**：不发命令，也不借用别的默认值
        （挑一格最近的、挑第一格之类）。"""
        self.engine_backpack((9, "minecraft:stone", 40))
        before = len(self.commands())
        self.quick_store()
        self.assertEqual(len(self.commands()), before)
        self.assertIsNone(self.backpack.selected)

    def test_a_slot_emptied_after_selection_sends_nothing(self):
        self.engine_backpack((9, "minecraft:stone", 40))
        self.select(0)
        self.clear_engine(9)
        before = len(self.commands())
        self.quick_store()
        self.assertEqual(len(self.commands()), before)
        self.assertIsNone(self.backpack.selected)

    def test_a_mismatched_server_fingerprint_waits_for_sync(self):
        """发之前的核对与 `inv_take` 是**同一套**：指纹对不上就等同步，
        而不是换个动作名把命令发出去。"""
        self.engine_backpack((9, "minecraft:stone", 40))
        self.client.inventory_slots[9]["canonical"] = "different"
        self.select(0)
        before = len(self.commands())
        self.quick_store()
        self.assertEqual(len(self.commands()), before)

    def test_no_server_snapshot_does_not_bypass_version_check(self):
        self.engine_backpack((9, "minecraft:stone", 40))
        self.client.inventory_slots = {}
        self.select(0)
        before = len(self.commands())
        self.quick_store()
        self.assertEqual(len(self.commands()), before)

    def test_without_a_session_the_click_is_dropped(self):
        """没有会话时**不能往下走到 `send_command`**：那条路会抛 `ValueError`，
        而这是 UI 回调，异常会冒到引擎的派发里、每点一次报一条。"""
        self.engine_backpack((9, "minecraft:stone", 40))
        self.select(0)
        # 空会话字段 = 服务端在说「那个会话已经没了」。
        self.client.receive(SyncEnvelope(SyncError("x", "session_invalid")).to_dict())
        self.assertFalse(self.client.has_session())
        before = len(self.commands())
        self.quick_store()
        self.assertEqual(len(self.commands()), before)

    def test_an_ok_receipt_clears_the_selection(self):
        """`inv_quick_move` 的回执与 `inv_take` 走**同一条收尾**：东西已经进
        网络了，留着那个指向空槽的选中没有意义。"""
        self.engine_backpack((9, "minecraft:stone", 40))
        self.select(0)
        self.quick_store()
        request_id = self.sole_in_flight()
        self.feed(CommandResultMessage(request_id, self.result(request_id)))
        self.view.tick()
        self.assertIsNone(self.backpack.selected)
        self.assertEqual(self.in_flight(), set())


class TestTransferAnimation(PanelFixture):
    """搬运动画的**接线**：每一条真的把东西搬走的路径都要发起一次，而且
    「等待同步」的那几处 `return` 一次都不该发起。

    坐标与缓动不在这里测（那要屏幕）；这里测的是「有没有发起」「用的是哪件
    物品」——接线断了的表现与「动画没做」完全一样。
    """

    def animator(self):
        recorder = RecordingAnimator()
        self.panel.fly = recorder
        return recorder

    def select(self, ui_index):
        self.assertEqual(self.backpack.select(ui_index), ui_index)

    def click(self, ui_index):
        self.bind(BinderStub.BF_ButtonClickUp, "backpack_click",
                  {"#collection_index": ui_index})

    def test_storing_a_stack_flies_the_item_out(self):
        self.engine_backpack((9, "minecraft:stone", 40))
        recorder = self.animator()
        self.select(0)
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0][0].name, "minecraft:stone")

    def test_quick_storing_a_whole_slot_flies_the_item_out(self):
        self.engine_backpack((9, "minecraft:stone", 200))
        recorder = self.animator()
        self.select(0)
        self.bind(BinderStub.BF_ButtonClickUp, "quick_store", None)
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0][0].name, "minecraft:stone")

    def test_storing_a_stack_flies_to_where_the_player_clicked(self):
        """「存进网络」这一向的落点是**点击处**（User 口径，2026-10-01）。

        此前落到 `_network_rect(key)`——那是「这件东西在网络里的行」，而整槽
        存入那条路点的是列表**空白**处，那里根本没有行，动画于是朝一个与手势
        无关的位置飞。
        """
        self.engine_backpack((9, "minecraft:stone", 40))
        recorder = self.animator()
        self.select(0)
        self.bind(BinderStub.BF_ButtonClickUp, "select",
                  {"#collection_index": 0, "TouchPosX": 222.0, "TouchPosY": 111.5})
        self.assertEqual(recorder.calls[0][2], (222.0, 111.5, 0.0, 0.0))

    def test_quick_storing_a_whole_slot_flies_to_the_click_too(self):
        self.engine_backpack((9, "minecraft:stone", 200))
        recorder = self.animator()
        self.select(0)
        self.bind(BinderStub.BF_ButtonClickUp, "quick_store",
                  {"TouchPosX": 240.5, "TouchPosY": 66.25})
        self.assertEqual(recorder.calls[0][2], (240.5, 66.25, 0.0, 0.0))

    def test_putting_into_a_backpack_slot_flies_to_that_slot(self):
        """网络 → 背包那一下的落点是**玩家点的那一格**。

        它不再是「背包那一半的整块区域」：2026-10-01 起这一条走带槽锚的
        `inv_put`，客户端**真的知道**东西进了哪一格（是玩家点的）。夹具的
        屏幕替身取不到格子控件，因此坐标是 `None`；关键是它**不是**那个
        点击点——落点由 `slot_layout` 算，不由手指位置算。
        """
        self.engine_backpack((9, "minecraft:stone", 40))
        recorder = self.animator()
        self.bind(BinderStub.BF_ButtonClickUp, "select",
                  {"#collection_index": 0, "TouchPosX": 222.0, "TouchPosY": 111.5})
        self.view.on_backpack_put(0)
        self.assertEqual(len(recorder.calls), 1)
        self.assertNotEqual(recorder.calls[0][2], (222.0, 111.5, 0.0, 0.0))
        self.assertIsNone(recorder.calls[0][2])

    def test_without_a_click_position_it_falls_back_to_the_row(self):
        """键盘/手柄触发时 `params` 里没有那两个键，于是退回 `_network_rect`
        ——不是 `(0, 0)`（那会是一道横穿屏幕的假动画）。夹具的屏幕替身取不到
        控件，因此退回来仍是 `None`，但**走的确实是另一条分支**。"""
        self.engine_backpack((9, "minecraft:stone", 40))
        recorder = self.animator()
        self.select(0)
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        self.assertIsNone(recorder.calls[0][2])

    def test_taking_from_the_network_flies_the_item_into_the_target_slot(self):
        self.engine_backpack((9, "minecraft:stone", 40))
        recorder = self.animator()
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        self.view.on_backpack_put(0)
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0][0].name, "minecraft:stone")

    def test_a_backpack_exchange_flies_between_the_two_slots(self):
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3))
        recorder = self.animator()
        self.click(0)
        self.click(1)
        self.assertEqual(len(recorder.calls), 1)
        self.assertEqual(recorder.calls[0][0].name, "minecraft:stone")
        # 夹具的屏幕替身没有真的格子控件，因此两个坐标都是 `None`——**这不
        # 影响本用例**：要钉的是「这一下有没有发起动画」，坐标怎么算归
        # `TestSlotLayout`。真机上坐标取不到时动画器会自己不动。
        self.assertEqual(recorder.calls[0][1:], (None, None))

    def test_nothing_is_flown_when_the_command_was_not_sent(self):
        """**「等待同步」的那几次点击不改变画面。** 指纹对不上时命令根本
        没发出去，动画也不该动——否则玩家会以为东西已经开始搬了。"""
        self.engine_backpack((9, "minecraft:stone", 40))
        self.client.inventory_slots[9]["canonical"] = "different"
        recorder = self.animator()
        self.select(0)
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 0})
        self.assertEqual(recorder.calls, [])

    def test_a_row_with_nothing_available_neither_sends_nor_flies(self):
        recorder = self.animator()
        # 夹具的第 2 行（DIRT）可用数是 0（快照里它是 2/2）。
        self.bind(BinderStub.BF_ButtonClickUp, "select", {"#collection_index": 2})
        self.assertEqual(self.commands(), [])
        self.assertEqual(recorder.calls, [])


class FlyControl(object):
    """`content.flying_item` 的替身：记下被设成什么、摆在哪儿、可不可见。

    `size` 默认 **(0, 0)**，这是**游戏内实测的读数**：这个 `custom` 控件的
    `GetSize()` 回 0 而不是模板声明的 18×18。动画**不该依赖它**——早先那版
    拿它算居中，于是图标整整偏出半个物品（两个轴各约 9px）。这里照实读 0，
    所以「偷回去读 GetSize」会立刻让位置断言失败。
    """

    def __init__(self, size=(0.0, 0.0), origin=(100.0, 50.0), broken=False):
        self.size = size
        self.origin = origin
        self.broken = broken
        self.item = None
        self.position = None
        self.positions = []
        self.visible = None

    def GetSize(self):
        return self.size

    def GetGlobalPosition(self):
        return self.origin

    def asItemRenderer(self):
        if self.broken:
            raise RuntimeError("not an item renderer")
        return self

    def SetUiItem(self, name, aux):
        self.item = (name, aux)
        return True

    def SetPosition(self, position):
        self.position = position
        self.positions.append(position)

    def SetVisible(self, visible):
        self.visible = visible


class FlyScreen(object):
    """飞行动画要的那两个控件。别的路径一律 `None`（本界面上没有）。"""

    def __init__(self, control, origin):
        self.control = control
        self.origin = origin

    def GetBaseUIControl(self, path):
        if path == slot_layout.FLYING_ITEM_PATH:
            return self.control
        if path == slot_layout.CONTENT_PATH:
            return self.origin
        return None


class FlyClock(object):
    """可以精确推进的时钟。真机上是 `time.time`，这里由用例说了算。"""

    def __init__(self):
        self.seconds = 0.0

    def __call__(self):
        return self.seconds

    def advance(self, seconds):
        self.seconds += seconds


class TestFlyingItem(unittest.TestCase):
    """动画本身：起点、终点、先快后慢、按**秒**而不是按帧。

    **按秒是这里最容易写错的一条。** 界面渲染帧率与脚本 tick、模拟 tick 都
    不是一回事，按帧数计时会让同一个 0.5 秒在低帧率下变成一秒多——而那种
    偏差在开发机上根本看不出来。
    """

    def setUp(self):
        self.control = FlyControl()
        self.origin = FlyControl(size=(180.0, 220.0))
        self.clock = FlyClock()
        self.animator = flying_item.FlyingItemAnimator(
            FlyScreen(self.control, self.origin),
            slot_layout.FLYING_ITEM_PATH, slot_layout.CONTENT_PATH,
            now=self.clock,
        )
        #: 源槽在 (118, 68) 处、18x18；目标在 (218, 168) 处、18x18。
        self.source = (118.0, 68.0, 18.0, 18.0)
        self.target = (218.0, 168.0, 18.0, 18.0)
        #: 槽位 18、图标 16，居中 ⇒ 图标左上角比槽位左上角各多 1 像素。
        self.inset = (18.0 - flying_item.ICON_SIZE) / 2.0

    def test_nothing_happens_until_something_is_flown(self):
        self.animator.tick()
        self.assertIsNone(self.control.visible)
        self.assertFalse(self.animator.is_active())

    def test_the_icon_starts_on_the_source_and_is_shown(self):
        """起点是**槽里那张图标**的位置，不是槽位控件的左上角。

        槽位 18×18、图标 16×16 且居中 ⇒ 图标左上角 = 槽位左上角 + 1。
        源槽 (118, 68)、原点 (100, 50)、(1, 1) 内缩 ⇒ (19, 19)。
        """
        self.assertTrue(self.animator.fly(STONE, self.source, self.target))
        self.assertEqual(
            self.control.position, (18.0 + self.inset, 18.0 + self.inset)
        )
        self.assertEqual(self.control.item, ("minecraft:stone", 0))
        self.assertTrue(self.control.visible)

    def test_it_reaches_the_target_and_hides_itself(self):
        """最后一帧落在终点附近，时间一到就**收起来**。

        终点同样带那 1 像素内缩：目标 (218, 168)、原点 (100, 50) ⇒ (119, 119)。
        """
        self.animator.fly(STONE, self.source, self.target)
        self.clock.advance(flying_item.FLY_SECONDS * 0.999)
        self.animator.tick()
        self.assertAlmostEqual(self.control.position[0], 118.0 + self.inset, delta=0.01)
        self.assertAlmostEqual(self.control.position[1], 118.0 + self.inset, delta=0.01)
        writes = len(self.control.positions)
        self.clock.advance(flying_item.FLY_SECONDS)
        self.animator.tick()
        self.assertFalse(self.control.visible)
        self.assertFalse(self.animator.is_active())
        # 到点之后**不再写位置、也不回到起点**：引擎那一侧只有收起来这一下。
        self.assertEqual(len(self.control.positions), writes)

    def test_a_click_point_lands_on_the_point_itself(self):
        """「落到玩家点的那一下」是一条**退化矩形**（0×0），它比的不是图标。

        它要是也被当成格子去「居中」，就会偏到左上 8 像素——一个谁也没点过
        的位置。比图标还小的矩形同理，因此规则是「不比图标大就不动它」。
        """
        point = (140.0, 90.0, 0.0, 0.0)
        self.assertTrue(self.animator.fly(STONE, self.source, point))
        self.clock.advance(flying_item.FLY_SECONDS * 0.999)
        self.animator.tick()
        # 点 (140, 90) − 原点 (100, 50) = (40, 40)，不偏不倚。
        self.assertAlmostEqual(self.control.position[0], 40.0, delta=0.01)
        self.assertAlmostEqual(self.control.position[1], 40.0, delta=0.01)

    def test_a_whole_area_lands_in_its_middle(self):
        """背包那一半、列表区是**整块矩形**，落点取正中（文档就是这么说的）。

        此前取的是左上角，于是「落到列表区中心」这句注释与代码差了半个区域
        ——那种差只在屏幕上看得出来。
        """
        area = (100.0, 50.0, 180.0, 100.0)
        self.assertTrue(self.animator.fly(STONE, self.source, area))
        self.clock.advance(flying_item.FLY_SECONDS * 0.999)
        self.animator.tick()
        # **居中说的是图标中心，不是它的左上角**：区域中心在 (190, 100)，
        # 图标 16 见方 ⇒ 左上角 (182, 92)；减去原点 (100, 50) 得 (82, 42)。
        self.assertAlmostEqual(self.control.position[0], 82.0, delta=0.01)
        self.assertAlmostEqual(self.control.position[1], 42.0, delta=0.01)

    def test_the_motion_is_fast_first_and_slow_after(self):
        """**先快后慢**：前四分之一的时间要走完超过四分之一的距离。

        线性缓动恰好走四分之一，因此这条能区分「有缓动」与「没有缓动」；
        而方向反了（先慢后快）则小于四分之一。
        """
        self.animator.fly(STONE, self.source, self.target)
        self.clock.advance(flying_item.FLY_SECONDS * 0.25)
        self.animator.tick()
        start = 18.0 + self.inset
        end = 118.0 + self.inset
        travelled = (self.control.position[0] - start) / (end - start)
        self.assertGreater(travelled, 0.25)
        self.assertLess(travelled, 1.0)

    def test_the_position_follows_elapsed_time_not_the_frame_count(self):
        """同一个时刻，**分几帧推进都是同一个位置**——帧数与位置无关。

        分 5 帧走完半程与一步走完半程，末帧位置必须**逐位相同**：位置是由
        累计时间算出来的，不是由「走了几次」累加出来的。时长本身是手感值
        （会被调），因此这里一律用 `FLY_SECONDS` 折算，**不写死秒数**。
        """
        half = flying_item.FLY_SECONDS / 2.0
        self.animator.fly(STONE, self.source, self.target)
        self.clock.advance(half)
        self.animator.tick()
        once = self.control.position

        control = FlyControl()
        clock = FlyClock()
        animator = flying_item.FlyingItemAnimator(
            FlyScreen(control, self.origin),
            slot_layout.FLYING_ITEM_PATH, slot_layout.CONTENT_PATH, now=clock)
        animator.fly(STONE, self.source, self.target)
        for _ in range(5):
            clock.advance(half / 5.0)
            animator.tick()
        self.assertEqual(control.position, once)

    def test_a_new_flight_replaces_the_running_one(self):
        """连点两下看到的是最后那一下的方向，而不是两次叠加。"""
        self.animator.fly(STONE, self.source, self.target)
        self.clock.advance(0.25)
        self.animator.tick()
        other = (300.0, 300.0, 18.0, 18.0)
        self.animator.fly(DIRT, self.target, other)
        self.assertEqual(self.control.item, ("minecraft:dirt", 0))
        self.assertEqual(
            self.control.position, (118.0 + self.inset, 118.0 + self.inset),
            "重新从新起点开始",
        )

    def test_a_missing_control_degrades_to_flying_nothing(self):
        animator = flying_item.FlyingItemAnimator(
            FlyScreen(None, self.origin),
            slot_layout.FLYING_ITEM_PATH, slot_layout.CONTENT_PATH,
            now=self.clock,
        )
        self.assertFalse(animator.fly(STONE, self.source, self.target))
        animator.tick()          # 不抛
        self.assertFalse(animator.is_active())

    def test_a_rect_that_could_not_be_measured_does_not_fly(self):
        """坐标取不到就**不飞**：宁可不动，也不要动错地方。"""
        for broken in ((None, self.target), (self.source, None), (self.source, (1, 2))):
            self.assertFalse(self.animator.fly(STONE, broken[0], broken[1]))

    def test_an_engine_refusal_is_counted_not_raised(self):
        control = FlyControl(broken=True)
        animator = flying_item.FlyingItemAnimator(
            FlyScreen(control, self.origin),
            slot_layout.FLYING_ITEM_PATH, slot_layout.CONTENT_PATH,
            now=self.clock,
        )
        self.assertFalse(animator.fly(STONE, self.source, self.target))
        self.assertEqual(animator.diagnostics()["errors"], 1)

    def test_clear_stops_the_flight_and_hides_the_icon(self):
        self.animator.fly(STONE, self.source, self.target)
        self.animator.clear()
        self.assertFalse(self.control.visible)
        self.assertFalse(self.animator.is_active())
        self.assertEqual(self.animator.diagnostics()["flights"], 1)


class GridStub(object):
    """一张网格：按行列给出格子控件，**取不到的格子返回 `None`**（同引擎）。"""

    def __init__(self, origin, cell=(18.0, 18.0), columns=9):
        self.origin = origin
        self.cell = cell
        self.columns = columns
        self.missing = set()

    def asGrid(self):
        return self

    def GetGridItem(self, column, row):
        if (column, row) in self.missing:
            return None
        return GridItem((self.origin[0] + column * self.cell[0],
                         self.origin[1] + row * self.cell[1]), self.cell)


class GridItem(object):
    def __init__(self, origin, size):
        self.origin = origin
        self.size = size

    def GetGlobalPosition(self):
        return self.origin

    def GetSize(self):
        return self.size


class ScrollStub(object):
    """滚动控件：只有内容接口，没有百分比接口——**这正是要测的那个分叉**。"""

    def __init__(self, content):
        self.content = content

    def asScrollView(self):
        return self

    def GetScrollViewContentControl(self):
        return self.content


class LayoutScreen(object):
    """一棵只认得本界面几条路径的控件树。

    `scroll_path` 决定滚动区那一段长在哪儿（鼠标端还是触控端），用来钉住
    「不写死 `scroll_mouse`」这条。
    """

    def __init__(self, backpack=None, hotbar=None, scroll_path=None,
                 content=None, scroll_panel=None, inventory=None):
        self.tree = {}
        if backpack is not None:
            self.tree[slot_layout.BACKPACK_GRID_PATH] = backpack
        if hotbar is not None:
            self.tree[slot_layout.HOTBAR_GRID_PATH] = hotbar
        if scroll_panel is not None:
            self.tree[slot_layout.SCROLL_PATH] = scroll_panel
        if scroll_path is not None:
            self.tree[scroll_path] = ScrollStub(content)

    def GetBaseUIControl(self, path):
        return self.tree.get(path)


class TestSlotLayoutCandidates(unittest.TestCase):
    """控件路径的**两种屏幕挂法**（2026-10-02 真机实测，两边各开着直接问过）。

    四个终端面板不都用同一种挂法，同一个控件因此在两个命名空间里：

    - 通用终端：`main` 里显式挂了 `terminal@…content` → `/terminal/…` 命中；
    - 合成／工厂／样板：`$screen_content` 把内容直接铺在屏幕基底上 →
      只有 `SCREEN_BASE_PATH + /…` 命中。

    离线拿不到 `SCREEN_BASE_PATH`（`screen_base()` 返回空串），因此这几个
    用例把它换成一个假基底——**钉住的正是「两种挂法都要试」这条**：改回
    只发一种，这里的红就是游戏里「动画整个不见」的那个症状。
    """

    def setUp(self):
        self.base = slot_layout.screen_base
        slot_layout.screen_base = lambda: "/FAKE_BASE"
        self.addCleanup(setattr, slot_layout, "screen_base", self.base)

    def test_a_panel_that_only_has_the_absolute_path_still_resolves(self):
        screen = LayoutScreen()
        screen.tree["/FAKE_BASE" + slot_layout.BACKPACK_GRID_PATH] = GridStub((1.0, 2.0))
        self.assertEqual(slot_layout.backpack_slot_rect(screen, 0),
                         (1.0, 2.0, 18.0, 18.0))

    def test_a_panel_that_only_has_the_relative_path_still_resolves(self):
        """通用终端那一支：绝对路径取不到，`/terminal/…` 取得到。"""
        screen = LayoutScreen()
        screen.tree[slot_layout.CONTENT_ROOT + slot_layout.BACKPACK_GRID_PATH] = (
            GridStub((3.0, 4.0)))
        self.assertEqual(slot_layout.backpack_slot_rect(screen, 0),
                         (3.0, 4.0, 18.0, 18.0))

    def test_neither_convention_resolving_is_still_none(self):
        """两条都取不到就是「本界面上没有这个控件」，不发明第三种说法。"""
        self.assertIsNone(slot_layout.backpack_slot_rect(LayoutScreen(), 0))

    def test_the_flying_item_and_the_origin_resolve_through_the_same_rule(self):
        """图标与原点**必须走同一条候选**：各解各的会让图标整块偏移，而
        那个错的症状看起来像缓动写错了。"""
        screen = LayoutScreen()
        screen.tree["/FAKE_BASE" + slot_layout.FLYING_ITEM_PATH] = "fly"
        screen.tree["/FAKE_BASE" + slot_layout.CONTENT_PATH] = "origin"
        self.assertEqual("fly", slot_layout.control(
            screen, slot_layout.FLYING_ITEM_PATH))
        self.assertEqual("origin", slot_layout.control(
            screen, slot_layout.CONTENT_PATH))


class TestSlotLayout(unittest.TestCase):
    """槽位矩形：给飞行动画算起终点用。

    取不到的**一律 `None`**，绝不猜一个坐标出来——飞到一个不存在的位置，
    看起来和「动画位置算错了」一模一样，而那种错要盯着屏幕才看得出来。
    """

    def test_backpack_slots_are_read_row_major_from_the_backpack_grid(self):
        screen = LayoutScreen(backpack=GridStub((10.0, 20.0)))
        self.assertEqual(slot_layout.backpack_slot_rect(screen, 0),
                         (10.0, 20.0, 18.0, 18.0))
        # 界面 9 → 第二行第一列。
        self.assertEqual(slot_layout.backpack_slot_rect(screen, 9),
                         (10.0, 38.0, 18.0, 18.0))

    def test_the_hotbar_is_a_separate_grid_with_its_own_origin(self):
        """两段是**两张网格**，不是一张 9x4 的。按背包那套算快捷栏，物品会
        飞到背包的最后一行去。"""
        screen = LayoutScreen(backpack=GridStub((10.0, 20.0)),
                              hotbar=GridStub((10.0, 200.0)))
        self.assertEqual(slot_layout.backpack_slot_rect(screen, 27),
                         (10.0, 200.0, 18.0, 18.0))
        self.assertEqual(slot_layout.backpack_slot_rect(screen, 35),
                         (10.0 + 8 * 18.0, 200.0, 18.0, 18.0))

    def test_an_index_outside_the_two_segments_is_not_guessed(self):
        screen = LayoutScreen(backpack=GridStub((10.0, 20.0)))
        for bad in (-1, 36, None, True, "0", 1.5):
            self.assertIsNone(slot_layout.backpack_slot_rect(screen, bad))

    def test_a_grid_that_is_not_there_yields_nothing(self):
        self.assertIsNone(slot_layout.backpack_slot_rect(LayoutScreen(), 0))

    def test_the_click_position_comes_from_the_binding_params(self):
        """点击位置取自绑定回调的 `params`（`TouchPosX/TouchPosY`，**UI 坐标**），
        **不是现问鼠标**——要的是「点的那一下在哪儿」，动画开始时鼠标可能已挪走。

        坐标系与 `GetGlobalPosition()` 同一个，因此不需要任何缩放换算。
        """
        self.assertEqual(
            slot_layout.click_rect({"TouchPosX": 12, "TouchPosY": 3.5}),
            (12.0, 3.5, 0.0, 0.0))

    def test_a_missing_or_malformed_click_position_is_not_guessed(self):
        """取不到就 `None`，调用方据此退回原来的落点——**不飞向 `(0, 0)`**。"""
        for bad in (None, [], {}, {"TouchPosX": 1}, {"TouchPosY": 1},
                    {"TouchPosX": "1", "TouchPosY": 2},
                    {"TouchPosX": True, "TouchPosY": 2},
                    {"TouchPosX": None, "TouchPosY": 2}):
            self.assertIsNone(slot_layout.click_rect(bad), repr(bad))

    def test_the_network_row_is_found_on_both_the_mouse_and_the_touch_layout(self):
        """**`scroll_mouse` 与 `scroll_touch` 只能由引擎说了算。**

        原版模板里这两层靠 `ignored` 互斥，写死一条就会在另一种设备上取不到
        内容控件——而那不是报错，只是动画不动。
        """
        for suffix in ("/scroll_mouse/scroll_view", "/scroll_touch/scroll_view"):
            screen = LayoutScreen(
                scroll_path=slot_layout.SCROLL_PATH + suffix,
                content=GridStub((50.0, 60.0)),
            )
            self.assertEqual(slot_layout.network_row_rect(screen, 0),
                             (50.0, 60.0, 18.0, 18.0),
                             "在 %s 上取不到内容" % (suffix,))
            # 行也是行优先的，与网络列表的 9 列一致。
            self.assertEqual(slot_layout.network_row_rect(screen, 10),
                             (50.0 + 18.0, 60.0 + 18.0, 18.0, 18.0))

    def test_a_row_the_grid_does_not_have_yields_nothing(self):
        grid = GridStub((50.0, 60.0))
        grid.missing.add((0, 1))
        screen = LayoutScreen(
            scroll_path=slot_layout.SCROLL_PATH + "/scroll_mouse/scroll_view",
            content=grid)
        self.assertIsNone(slot_layout.network_row_rect(screen, 9))

    def test_the_area_rect_is_the_scroll_panel_not_a_row(self):
        """物品在网络里还没有行时落到整块列表区——那是屏幕上真实存在的地方。"""
        screen = LayoutScreen(scroll_panel=GridItem((30.0, 40.0), (162.0, 80.0)))
        self.assertEqual(slot_layout.network_area_rect(screen),
                         (30.0, 40.0, 162.0, 80.0))

    def test_the_backpack_area_covers_both_segments(self):
        """「网络 → 背包」的落点：服务端挑哪个格子客户端看不到，因此落**整块**
        区域（上 9x3 + 下 9x1），不假装知道是哪一格。"""
        screen = LayoutScreen()
        screen.tree[slot_layout.INVENTORY_PATH] = GridItem((5.0, 6.0), (162.0, 90.0))
        self.assertEqual(slot_layout.backpack_area_rect(screen),
                         (5.0, 6.0, 162.0, 90.0))
        self.assertIsNone(slot_layout.backpack_area_rect(LayoutScreen()))


class TestSearchAndSort(PanelFixture):
    def test_search_strips_and_ignores_a_repeat_of_the_same_text(self):
        generation = self.cache.model.query_generation()
        self.bind(BinderStub.BF_EditChanged, "search", {"Text": b"  stone  "})
        first = self.cache.model.query_generation()
        self.assertGreater(first, generation)
        self.assertEqual(self.view.search_text, "stone")
        # 同一个词再来一次**不重排**：输入框每个字符都会回调，而重排之后
        # 滚动位置与选中态都要重算，白算一次就是一次可见的抖动。
        self.bind(BinderStub.BF_EditChanged, "search", {"Text": "stone"})
        self.assertEqual(self.cache.model.query_generation(), first)
        self.assertEqual(self.names(), ["minecraft:stone"])
        self.assertEqual(self.bind(BinderStub.BF_BindInt, "count"), 1)

    def test_an_empty_search_shows_everything(self):
        self.bind(BinderStub.BF_EditChanged, "search", {"Text": "stone"})
        self.assertEqual(self.bind(BinderStub.BF_BindInt, "count"), 1)
        self.bind(BinderStub.BF_EditChanged, "search", {"Text": "   "})
        self.assertEqual(self.bind(BinderStub.BF_BindInt, "count"), 3)

    def test_sort_method_cycles_between_the_two_icons(self):
        self.assertEqual(self.view.sort_field, SORT_NAME)
        self.bind(BinderStub.BF_ButtonClickUp, "sort_method")
        self.assertEqual(self.view.sort_field, SORT_COUNT)
        self.bind(BinderStub.BF_ButtonClickUp, "sort_method")
        self.assertEqual(self.view.sort_field, SORT_NAME)

    def test_sort_order_toggles_without_touching_the_method(self):
        self.assertEqual(self.view.sort_descending, True)
        self.bind(BinderStub.BF_ButtonClickUp, "sort_order")
        self.assertFalse(self.view.sort_descending)
        self.assertEqual(self.view.sort_field, SORT_NAME)
        # 方式与方向是**两个独立状态**：来回切方向不该把方式带走。
        self.bind(BinderStub.BF_ButtonClickUp, "sort_method")
        self.bind(BinderStub.BF_ButtonClickUp, "sort_order")
        self.assertTrue(self.view.sort_descending)
        self.assertEqual(self.view.sort_field, SORT_COUNT)

    def test_the_initial_icons_match_the_initial_order(self):
        """开场图标与真实顺序必须一致：默认名称**降序**（决策 D6）。"""
        method = self.screen.elements[terminal_panel.SORT_METHOD_ICON_PATH]
        order = self.screen.elements[terminal_panel.SORT_ORDER_ICON_PATH]
        self.assertEqual(method.uv, terminal_panel.SORT_METHOD_UV[SORT_NAME])
        self.assertEqual(order.uv, terminal_panel.SORT_ORDER_UV[True])
        self.assertEqual(
            self.names(),
            sorted(["minecraft:stone", "minecraft:sand", "minecraft:dirt"],
                   reverse=True))

    def test_an_icon_is_only_rewritten_when_its_state_really_changed(self):
        """`SetUV` 每帧调用会让引擎每帧重新切图，因此只在真变了时写。"""
        order = self.screen.elements[terminal_panel.SORT_ORDER_ICON_PATH]
        method = self.screen.elements[terminal_panel.SORT_METHOD_ICON_PATH]
        before = order.uv_writes
        self.bind(BinderStub.BF_ButtonClickUp, "sort_method")
        self.assertEqual(order.uv_writes, before)
        self.assertEqual(method.uv, terminal_panel.SORT_METHOD_UV[SORT_COUNT])
        self.bind(BinderStub.BF_ButtonClickUp, "sort_order")
        self.assertEqual(order.uv_writes, before + 1)
        self.assertEqual(order.uv, terminal_panel.SORT_ORDER_UV[False])


class TestInteractionsNeedASession(PanelFixture):
    """**遮罩拦不住面板上的控件，所以拦截落在回调这一层。**

    2026-10-01 真机实测：`unavailable_mask` 提到 `layer: 39` 之后，两个排序
    按钮**仍然点得到**（它拦得住列表区，拦不住这两个）。把窗口抬得更高能拦
    住，但那样连关闭按钮一起盖掉了——所以判据放在回调里，遮罩只管表现。

    与 `on_select` 早就写着的那一条同源：**「碰巧不会炸」不算拦住了**
    （那里原来的靠山是「`inventory_slots` 随会话清空」，改动一多就失效）。

    每一条都先证一遍「有会话时有效」，再丢掉会话证「之后无效」——只写后半
    句的话，一个永远直接返回的函数也能把它测绿。
    """

    def drop_session(self):
        # type: () -> None
        self.client.drop_session()
        self.assertFalse(self.view.cache.client.has_session())

    def test_the_sort_method_button_does_nothing_without_a_session(self):
        self.bind(BinderStub.BF_ButtonClickUp, "sort_method")
        self.assertEqual(self.view.sort_field, SORT_COUNT)
        self.drop_session()
        self.bind(BinderStub.BF_ButtonClickUp, "sort_method")
        self.assertEqual(self.view.sort_field, SORT_COUNT)

    def test_the_sort_order_button_does_nothing_without_a_session(self):
        self.bind(BinderStub.BF_ButtonClickUp, "sort_order")
        self.assertFalse(self.view.sort_descending)
        self.drop_session()
        self.bind(BinderStub.BF_ButtonClickUp, "sort_order")
        self.assertFalse(self.view.sort_descending)

    def test_the_search_box_does_nothing_without_a_session(self):
        self.bind(BinderStub.BF_EditChanged, "search", {"Text": "stone"})
        self.assertEqual(self.view.search_text, "stone")
        self.drop_session()
        self.bind(BinderStub.BF_EditChanged, "search", {"Text": "dirt"})
        self.assertEqual(self.view.search_text, "stone")
        self.assertEqual(self.names(), ["minecraft:stone"])

    def test_a_backpack_exchange_sends_nothing_without_a_session(self):
        self.engine_backpack((9, "minecraft:stone", 5), (10, "minecraft:dirt", 3))
        self.panel.OnBackpackClick({"#collection_index": 0})
        self.assertEqual(self.backpack.selected, 0)
        self.drop_session()
        before = len(self.commands())
        self.panel.OnBackpackClick({"#collection_index": 1})
        self.assertEqual(len(self.commands()), before, "没有会话时不该发命令")
        self.assertEqual(self.in_flight(), set())


class TestLongPressSplit(PanelFixture):
    """长按背包格 = 把这一槽的**一半**存进网络（`inv_split`）。

    手势由 User 2026-10-01 定（长按，PC 与触控同一颗按钮）。**引擎没有
    「长按」这一档绑定**——只有按下与抬起两个相位，时长只能在脚本侧算
    （`LONG_PRESS_SECONDS`）。所以这里驱动的是**两个相位**，而不是一个
    「长按事件」：真机上到底收不收得到按下相位仍是未验证项，离线能验的
    只是「收到了的话，算得对不对」。
    """

    def setUp(self):
        PanelFixture.setUp(self)
        # 时钟注入：用例不必真的 sleep（同 `flying_item` 的理由与做法）。
        self.now = [1000.0]
        self.panel._clock = lambda: self.now[0]

    #: 「长按」在用例里的推进量。**比门槛多留一截是有意的**：`1000.0 + 0.4`
    #: 再减 `1000.0` 得到的是 `0.39999999999986`，正好卡在 `>=` 下面——拿
    #: 门槛当推进量会让这条用例测的是浮点舍入，而不是长按判定。
    HOLD = terminal_panel.LONG_PRESS_SECONDS + 0.1

    def press(self, ui_index, hold=None):
        # type: (int, object) -> None
        """按下再抬起同一格；`hold` 是两次之间流逝的秒数。"""
        params = {"#collection_index": ui_index}
        self.bind(BinderStub.BF_ButtonClickDown, "backpack_click", params)
        if hold is not None:
            self.now[0] += hold
        self.bind(BinderStub.BF_ButtonClickUp, "backpack_click", params)

    def test_a_long_press_splits_the_stack_in_half(self):
        self.engine_backpack((9, "minecraft:stone", 30))
        self.press(0, hold=self.HOLD)
        command = self.take_command()
        self.assertEqual(command["action"], "inv_split")
        # 界面 0 → 引擎 9（下半屏那两段的顺序与引擎相反）。
        self.assertEqual(command["slot"], 9)
        self.assertEqual(command["count"], 15)
        self.assertEqual(command["slot_version"], 7)
        # **不发 `cursor`**：与其余几条一样，这条手势不产生持有物。
        self.assertNotIn("cursor", command)

    def test_a_short_press_is_still_just_a_click(self):
        self.engine_backpack((9, "minecraft:stone", 30))
        self.press(0, hold=terminal_panel.LONG_PRESS_SECONDS - 0.01)
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.backpack.selected, 0)

    def test_a_press_that_ends_on_another_slot_does_not_arm_the_next_click(self):
        """**长按只该影响那一下。**

        按下落在 A、抬起落在 B 时，A 那一条记录必须**当场作废**。不作废的话，
        之后任何一次**只有抬起、没有按下**的 A（引擎没给 down 相位、或丢了一
        次）都会拿那条旧时间戳去比，凭空变成一次拆分。

        （写成「连续两次长按/短按」是测不到的：第二下的 down 会把记录覆盖
        掉，于是不管清不清都通过——那是**用例不咬**，不是行为正确。）
        """
        self.engine_backpack((9, "minecraft:stone", 30), (10, "minecraft:dirt", 30))
        self.bind(BinderStub.BF_ButtonClickDown, "backpack_click",
                  {"#collection_index": 0})
        self.now[0] += self.HOLD
        # 抬起落在**另一格**：这一下不是长按，A 的记录就此作废。
        self.bind(BinderStub.BF_ButtonClickUp, "backpack_click",
                  {"#collection_index": 1})
        self.backpack.clear_selection()
        before = len(self.commands())
        # 模拟「引擎没给 down 相位」：只有抬起，而且离那次按下已经很久。
        self.bind(BinderStub.BF_ButtonClickUp, "backpack_click",
                  {"#collection_index": 0})
        self.assertEqual(len(self.commands()), before, "A 的记录早该作废，不该拆分")
        self.assertEqual(self.backpack.selected, 0)

    def test_a_single_item_has_no_half_to_split(self):
        """`1 // 2 == 0`：一件东西拆不出两半，那一下什么都不发——
        而不是发一条 `count=0` 去换一张越界回执。"""
        self.engine_backpack((9, "minecraft:stone", 1))
        self.press(0, hold=self.HOLD)
        self.assertEqual(self.commands(), [])

    def test_a_long_press_without_a_session_sends_nothing(self):
        self.engine_backpack((9, "minecraft:stone", 30))
        self.client.drop_session()
        self.press(0, hold=self.HOLD)
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.in_flight(), set())

    def test_a_press_on_an_out_of_range_slot_does_not_leak_into_the_next_one(self):
        """按下落在算不出身份的位置时，记录要清掉，抬起不该拿它去比。"""
        self.engine_backpack((9, "minecraft:stone", 30))
        self.bind(BinderStub.BF_ButtonClickDown, "backpack_click",
                  {"#collection_index": 99})
        self.now[0] += terminal_panel.LONG_PRESS_SECONDS
        self.bind(BinderStub.BF_ButtonClickUp, "backpack_click",
                  {"#collection_index": 0})
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.backpack.selected, 0)


class TestRerenderAndAnchoring(PanelFixture):
    def test_a_delta_touching_no_visible_row_does_not_rerender(self):
        self.bind(BinderStub.BF_EditChanged, "search", {"Text": "stone"})
        self.assertEqual(self.names(), ["minecraft:stone"])
        calls = []
        original = self.view.refresh
        self.view.refresh = lambda: (calls.append(1), original())[1]
        self.delta(upserts=((DIRT, 9, 0, False),))
        self.view.tick()
        self.assertEqual(calls, [])
        self.delta(upserts=((STONE, 41, 4, False),), base=DELTA_VERSION, new=DELTA_VERSION + 1)
        self.view.tick()
        self.assertEqual(len(calls), 1)

    def test_the_anchored_row_keeps_its_place_across_a_reorder(self):
        """重排之后把玩家盯着的那一行拉回视野。

        锚点是**资源键**而不是行号：行号在一批增量之后指向的是别人。这里
        钉的是一串具体数字——`zinc` 插进来把 `stone` 从第 0 行挤到第 1 行，
        4 行里第 1 行就是 25%。
        """
        scroll = self.screen.scrolls[terminal_panel.SCROLL_PATH]
        scroll.percent = 33
        self.assertEqual(self.names()[0], "minecraft:stone")
        self.delta(upserts=((ZINC, 5, 0, False),))
        self.view.tick()
        self.assertEqual(self.names(), ["minecraft:zinc", "minecraft:stone",
                                        "minecraft:sand", "minecraft:dirt"])
        self.assertEqual(scroll.written, [25])

    def test_a_reorder_that_drops_the_anchor_falls_back_to_its_old_offset(self):
        """锚点那一行被删掉时退到「它原来占着第几行」，而不是跳回顶端。"""
        scroll = self.screen.scrolls[terminal_panel.SCROLL_PATH]
        scroll.percent = 0
        self.assertEqual(self.names()[0], "minecraft:stone")
        self.delta(removes=(STONE,))
        self.view.tick()
        self.assertEqual(self.names(), ["minecraft:sand", "minecraft:dirt"])
        # 锚点（`stone`）没了 → 退到它原来占着的第 0 行，也就是现在的 `sand`。
        # 换成「跳回顶端」在这里是同一个值，因此**再看一眼锚点挂在谁身上**。
        self.assertEqual(scroll.written, [0])
        self.assertEqual(self.cache.model.anchor()[0], SAND)

    def test_a_missing_scroll_control_degrades_to_no_anchoring(self):
        """取不到滚动条时列表照常工作，只是不重锚——不该因此整块不显示。

        这份用例用的是**另一份缓存**：`TerminalCache.close()` 不幂等（第二次
        `close_session` 会因为会话已经没了而抛），两个视图共用一份时先关的
        那个会把后关的带崩。面板自己不会走到那一步（`OnDestroy` 把
        `list_view` 置空），这里只是不去构造那个组合。
        """
        client = NetworkClient(port=RecordingPort())
        cache = TerminalCache(client=client)

        def feed(message):
            client.receive(SyncEnvelope(message, "s1", "n1", "e1").to_dict())

        for message in (SessionReady(7, SNAPSHOT_VERSION),
                        SnapshotBegin("snap-x", SNAPSHOT_VERSION, 1),
                        SnapshotChunk("snap-x", 0, ((STONE, 1, 0, False),)),
                        SnapshotEnd("snap-x", 1)):
            feed(message)
        cache.tick()
        screen = Screen()
        screen.GetBaseUIControl = lambda path: (_ for _ in ()).throw(RuntimeError("no"))
        view = terminal_panel.TerminalListView(screen, cache=cache)
        self.addCleanup(view.close)
        self.assertIsNone(view._scroll)
        feed(ItemDelta("b1", 0, 1, SNAPSHOT_VERSION, DELTA_VERSION, ((ZINC, 5, 0, False),), ()))
        view.tick()
        self.assertEqual(view.rows[0].key, ZINC)


class TestStatusSessionLifecycle(PanelFixture):
    def test_delayed_session_ready_preserves_status_model_and_handler(self):
        from endernet_bp.endernet_scripts.endernet.common.protocol import DeviceStatusMessage
        self.client.drop_session()
        model = device_status.DeviceStatusModel(REF)
        self.panel.status = model
        self.client.on_message(self.panel._make_status_handler(self.client))
        self.client.open_session(REF, 0)
        for unused in range(3):
            self.panel.OnTicking()
        self.assertIs(self.panel.status, model)
        self.assertFalse(self.panel.device_status_visible())
        self.feed(SessionReady(7, SNAPSHOT_VERSION))
        self.feed(DeviceStatusMessage(
            "endernet:common_terminal", REF.generation, True, "在线",
            "ok", "runnable", "可运行", 1))
        self.panel.OnTicking()
        self.assertEqual(self.panel.device_status_text(), "在线")
        self.screen.bindings[
            (BinderStub.BF_ButtonClickUp, device_status.OPEN_BINDING)]({})
        self.assertTrue(self.panel.device_status_popup_visible())

    def test_rejected_pending_session_clears_status_model(self):
        self.client.drop_session()
        self.panel.status = device_status.DeviceStatusModel(REF)
        self.client.open_session(REF, 0)
        self.client.receive(SyncEnvelope(SyncError("x", "session_invalid")).to_dict())
        self.panel.OnTicking()
        self.assertIsNone(self.panel.status)

    def online(self):
        from endernet_bp.endernet_scripts.endernet.common.protocol import DeviceStatusMessage
        model = device_status.DeviceStatusModel(REF)
        model.apply(DeviceStatusMessage(
            "endernet:common_terminal", REF.generation, True, "在线",
            "ok", "runnable", "可运行", 1))
        self.panel.status = model
        self.panel.OnDeviceStatusClick()
        self.assertEqual(self.panel.device_status_text(), "在线")
        self.assertTrue(self.panel.device_status_popup_visible())
        return model

    def test_idle_terminal_clears_status_and_popup_when_session_is_lost(self):
        self.online()
        self.assertEqual(self.view._action_requests, {})
        self.client.drop_session()
        self.panel.OnTicking()
        self.assertIsNone(self.panel.status)
        self.assertFalse(self.panel.device_status_visible())
        self.assertFalse(self.panel.device_status_popup_visible())
        self.assertEqual(self.panel.device_status_color(), device_status.COLOR_OFFLINE)
        self.panel.OnTicking()  # 重复收尾安全。

    def test_status_is_cleared_even_without_a_backpack(self):
        self.online()
        self.view.backpack = None
        self.client.drop_session()
        self.view.tick()
        self.assertIsNone(self.panel.status)

    def test_active_session_keeps_online_status_and_popup(self):
        model = self.online()
        self.panel.OnTicking()
        self.assertIs(self.panel.status, model)
        self.assertTrue(self.panel.device_status_visible())
        self.assertTrue(self.panel.device_status_popup_visible())

    def test_status_handler_rejects_another_device_generation(self):
        from endernet_bp.endernet_scripts.endernet.common.protocol import DeviceStatusMessage
        model = self.online()
        handler = self.panel._make_status_handler(self.client)
        message = DeviceStatusMessage(
            "endernet:common_terminal", REF.generation + 1, False, "离线",
            "ok", "master_absent", "无主控", 2)
        handler(types.SimpleNamespace(session_id=self.client.session_id), message)
        self.assertEqual(self.panel.device_status_text(), "在线")
        self.assertEqual(model.stats["dropped"], 1)

    def test_shared_handler_ignores_replaced_models_and_closed_sessions(self):
        from endernet_bp.endernet_scripts.endernet.common.protocol import MSG_DEVICE_STATUS
        applied = []
        self.panel.status = types.SimpleNamespace(apply=applied.append)
        handler = self.panel._make_status_handler(self.client)
        envelope = types.SimpleNamespace(session_id=self.client.session_id)
        message = types.SimpleNamespace(kind=MSG_DEVICE_STATUS)
        handler(envelope, message)
        self.assertEqual(applied, [message])
        handler(types.SimpleNamespace(session_id="s9"), message)
        handler(envelope, None)
        handler(envelope, types.SimpleNamespace(kind="craft_state"))
        self.assertEqual(applied, [message])
        self.client.drop_session()
        handler(types.SimpleNamespace(session_id=""), message)
        self.assertEqual(applied, [message])
        self.client.session_id = envelope.session_id
        replacement = []
        self.panel.status = types.SimpleNamespace(apply=replacement.append)
        handler(envelope, message)
        self.assertEqual(replacement, [])
        self.assertEqual(applied, [message])
        self.panel.status = None
        handler(envelope, message)


class TestPanelEntry(PanelFixture):
    def test_no_device_ref_means_no_session_and_no_list(self):
        """`OpenDemo()` 这类没有权威身份的打开方式**不占位、不伪造**。

        按 11.md 的 UI 前置约定，缺交付时相关逻辑保持暂停。编一个设备引用
        出来会让界面看着一切正常，而每一条命令都会打到别人身上。
        """
        screen = Screen()
        panel = terminal_panel.CommonTerminalPanel("main", screen, {"items": None})
        panel.OnCreate()
        self.assertIsNone(panel.list_view)
        # 十条列表绑定**照样在册**（类体注册，不跟会话走），但它们答的是空表
        # 而不是假数据：报一个「有 N 行」出去就会画出幽灵格子。
        self.assertEqual(
            screen.bindings[(BinderStub.BF_BindInt, terminal_panel.COUNT_BINDING)](),
            0)

    def test_a_device_ref_opens_the_session_and_ticks_the_view(self):
        """有权威身份时才建视图、开会话、挂收包。

        端口换成记账替身：真端口要去 `tooldelta` 里取那两个事件类，而那是
        引擎侧的东西（本文件的替身清单里没有它）。这里要验的是面板**什么时候
        建视图、什么时候挂收包**，不是引擎那一侧。
        """
        screen = Screen()
        port = RecordingPort()
        with patch.object(terminal_panel, "NetworkClient",
                          lambda: NetworkClient(port=port)):
            panel = terminal_panel.CommonTerminalPanel(
                "main", screen, {"device_ref": REF.to_dict(), "dimension": 0})
            panel.OnCreate()
        self.addCleanup(panel.OnDestroy)
        self.assertIsNotNone(panel.list_view)
        # 会话**已经请求出去**，但还没建成：`session_id` 要等 `session_ready`
        # 回来才有。这正是要盯的地方——视图必须在开会话之前就把收包挂上，
        # 否则那条回包没人接，界面停在「没有会话」直到下一次重开。
        self.assertEqual([item["message"]["kind"] for item in port.sent],
                         ["session_open"])
        self.assertFalse(panel.list_view.cache.client.has_session())
        # 收包回调在开会话**之前**就挂上了：`session_ready` 可能比 `OnCreate`
        # 返回还早，反过来的话首帧快照会整批丢掉。
        self.assertEqual(port.listens, 1)
        panel.OnTicking()
        panel.OnDestroy()
        self.assertIsNone(panel.list_view)
        # 视图拆了之后引擎仍可能再问一帧：答的必须是空表。
        self.assertEqual(
            screen.bindings[(BinderStub.BF_BindInt, terminal_panel.COUNT_BINDING)](),
            0)
        self.assertEqual(port.unlistens, 1)

    def test_a_broken_device_ref_pauses_instead_of_opening_a_wrong_session(self):
        """身份还原不了就**当没有身份**，不拿半个位置去开会话。

        半个位置能过 `open_session` 的类型检查（它只认 `DeviceRef`），炸的是
        更远处——服务端按 `matches` 一比就拒。玩家看到的都是「终端没反应」，
        但一个多发了一条注定被拒的包，另一个什么都没发。
        """
        for broken in ({"node_key": {"dimension": 0}}, "terminal-1", 7):
            screen = Screen()
            panel = terminal_panel.CommonTerminalPanel(
                "main", screen, {"device_ref": broken, "dimension": 0})
            panel.OnCreate()
            self.assertIsNone(panel.list_view)
            self.assertFalse(
                screen.bindings[
                    (BinderStub.BF_BindBool, terminal_panel.UNAVAILABLE_BINDING)]())


class TestEngineBindingResolution(unittest.TestCase):
    """引擎按**名字**取回调，所以绑定只能写在类体里。

    这是 2026-09-30 游戏内实测撞出来的：界面上每一条绑定都走运行时注册，
    打开终端面板时每渲染一帧抛一条

        'CommonTerminalPanel_Base' object has no attribute 'reader'

    ——`reader` 是给八个格子回调套的那个包装函数的名字。机制是：登记时引擎
    把**回调自己的名字**交给 C++ 那一侧，取值时拿这个名字在生成的 `_Base`
    上 `getattr`。类体里的绑定会被 `_get_tdscreen_bindings` 挂成 `_Base` 的
    属性（名字就是方法名），因此查得到；运行时注册那条路不会。

    这里的两条用例一条钉「类体这条路查得到」，一条钉「运行时注册那条路查
    不到」——**后一条是前一条的理由**，删掉它，下一个人就只会看到一堆莫名其
    妙的转发方法。
    """

    def panel(self):
        return terminal_panel.CommonTerminalPanel("main", Screen(), {})

    def test_every_class_body_binding_answers_to_the_name_the_engine_stores(self):
        panel = self.panel()
        registered = 0
        for key in dir(panel._engine):
            func = getattr(panel._engine, key)
            if getattr(func, "binding_flags", None) is None:
                continue
            registered += 1
            # 引擎就是这一步：按回调的名字在 `_Base` 上取。
            self.assertIsNotNone(getattr(type(panel._engine), func.__name__, None))
        # 十条列表 + 八条格子 + 四条点击（背包、快捷栏、关闭、列表空白处整槽存）
        # + 两条按下（背包、快捷栏；`inv_split` 的长按判定要按下那一刻的时刻）
        # = 24；再加设备状态十一条（文字、颜色、可见、图标、详情两行文字、
        # 两个行图标、弹窗显隐、打开、关闭）。
        #
        # **颜色那一条只在引擎提供 `BF_BindColor` 时才注册**（`terminal_panel.
        # _bind_color`）：11-06 起离线桩也带上了这个旗标（工厂终端的三色背景
        # 要用它），因此本地走的是「有」那一支；下面那句留的是「换一个没有这个
        # 标志的引擎时少一条、而其余照常」——那正是这条兜底存在的理由。
        # 11 增量再加两条路：
        #   - **网络列表的长按判定**（`BF_ButtonClickDown` + `select`，与抬起
        #     那条同名）：契约第 2 节要「按下时记住完整资源键」，因此必须有
        #     按下那一刻的那条绑定；
        #   - **下单预览窗十二项**：整窗显隐、loading、按钮可用、状态说明、
        #     行数（接 `#maximum_grid_items`），四行属性（图标、名字、数量、
        #     缺料颜色），三个按钮（关闭、取消、下单）。
        # 预览的缺料颜色与设备状态的颜色用同一个旗标，缺了它两边各少一条。
        #
        # 11-07 再加三条：挂在终端屏上的三个**辅助窗口**的输入框绑定
        # （物品选择器、流体选择器、数量窗）。它们原先由窗口对象自己用
        # 运行时注册声明——正是本条钉住的那条不通的路——因此搬到类体上
        # （`widgets/list_window_bindings.py`）。数在终端这一侧是因为
        # `TerminalPanelBase` 是它们的宿主。
        expected = 24 + 11 + 1 + 12 + 3
        if terminal_panel._COLOR_BINDING_FLAG is None:
            expected -= 1
        if order_preview_bindings.COLOR_BINDING_FLAG is None:
            expected -= 1
        # 少一条的表现是那一格／那一项／那一下点击永远是空的或没反应。
        self.assertEqual(registered, expected)

    def test_the_runtime_registration_route_cannot_resolve_at_all(self):
        panel = self.panel()

        def reader(index):
            return 0

        with self.assertRaises(AttributeError):
            panel.DynaBindingCollection(
                BinderStub.BF_BindInt, terminal_panel.COLLECTION,
                terminal_panel.BINDING_PREFIX + "item_id_aux", reader)


class TestJsonContract(unittest.TestCase):
    """界面 JSON 与上面这些常量的接线。**这里只验名字对得上**，控件画得对不对
    是游戏内实测项。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "endernet_rp", "ui",
                               "common_terminal_panel.json"), encoding="utf-8") as handle:
            cls.panel = json.load(handle)
        with open(os.path.join(ROOT, "endernet_rp", "ui",
                               "endernet_windows.json"), encoding="utf-8") as handle:
            cls.windows = json.load(handle)
        with open(os.path.join(ROOT, "endernet_rp", "ui",
                               "endernet_container_common.json"), encoding="utf-8") as handle:
            cls.container = json.load(handle)
        with open(os.path.join(ROOT, "endernet_rp", "ui",
                               "endernet_ui_common.json"), encoding="utf-8") as handle:
            cls.common = json.load(handle)

    def controls(self, node):
        return {key.split("@")[0]: value
                for entry in node["controls"] for key, value in entry.items()}

    def find_control(self, node, name):
        """按控件名在整棵树里找（`@模板` 后缀不算名字的一部分）。"""
        if isinstance(node, dict):
            for key, value in node.items():
                if key.split("@")[0] == name:
                    return value
                found = self.find_control(value, name)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = self.find_control(value, name)
                if found is not None:
                    return found
        return None

    def test_the_terminal_slot_overrides_every_variable_the_shared_template_uses(self):
        """终端侧的薄包装只覆盖变量，视觉结构一个字不动（决策 D3）。

        **六个都要覆盖**：漏一个就会静默沿用 11-09 的默认值（`endernet_item_list`
        与 `#EnderNetItemList.*`），而那种格子会去读另一个窗口的集合——格子在、
        图也有，只是永远为空。
        """
        shared = self.windows["item_can_be_selected"]
        declared = set(key.split("|")[0] for key in shared if key.startswith("$"))
        wrapper = self.panel["terminal_slot@endernet_windows.item_can_be_selected"]
        overridden = set(key.split("|")[0] for key in wrapper if key.startswith("$"))
        self.assertEqual(overridden, declared)
        self.assertEqual(wrapper["$item_collection_name"], terminal_panel.COLLECTION)
        for variable, suffix in (
            ("$item_visible_binding", "has_item"),
            ("$item_id_aux_binding", "item_id_aux"),
            ("$item_selected_binding", "selected"),
            ("$item_count_binding", "item_count"),
            ("$item_select_button", "select"),
        ):
            self.assertEqual(wrapper[variable], terminal_panel.BINDING_PREFIX + suffix)

    def test_the_grid_declares_the_collection_template_and_count_binding(self):
        grid = self.panel["terminal_items_grid"]
        self.assertEqual(grid["collection_name"], terminal_panel.COLLECTION)
        self.assertEqual(grid["grid_item_template"],
                         "common_terminal_panel_ui.terminal_slot")
        binding = grid["bindings"][0]
        self.assertEqual(binding["binding_name_override"], "#maximum_grid_items")
        self.assertEqual(binding["binding_type"], "global")
        self.assertEqual(binding["binding_name"], terminal_panel.BINDING_PREFIX + "count")

    def test_the_search_box_sort_buttons_and_mask_point_at_the_right_bindings(self):
        content = self.controls(self.panel["content"])
        self.assertEqual(content["item_search_editor"]["$text_box_name"],
                         terminal_panel.SEARCH_BINDING)
        for button, binding in (("sort_method_btn", terminal_panel.SORT_METHOD_BINDING),
                                ("sort_order_btn", terminal_panel.SORT_ORDER_BINDING)):
            self.assertEqual(content[button]["$pressed_button_name"], binding)
            self.assertEqual(
                content[button]["button_mappings"][0]["to_button_id"],
                "$pressed_button_name")
        mask = content["unavailable_mask"]
        self.assertEqual(mask["bindings"][0]["binding_name"],
                         terminal_panel.UNAVAILABLE_BINDING)
        self.assertEqual(mask["bindings"][0]["binding_name_override"], "#visible")

    def test_the_scroll_background_is_an_invisible_button_wired_to_quick_store(self):
        """上方列表的空白处那块**隐形按钮**：名字要与 Python 侧逐字对上。

        `common.scrolling_panel` 把 `$scroll_background_image_control` 实例化成
        `background`，而它在原版模板里**不接受外部传入的变量**——因此按钮名
        只能写在一个专用模板里。用一个默认名（`simple_invisible_button` 的
        `%netease_editor_template_namespace.click`）的话，这一下点击引擎查不到
        绑定，**安静地什么都不做**。
        """
        content = self.controls(self.panel["content"])
        scroll = content["terminal_items_scroll"]
        self.assertEqual(scroll["$scroll_background_image_control"],
                         "common_terminal_panel_ui.terminal_scroll_background")
        # 背景要显示出来，否则整块按钮根本不存在（`background` 挂着
        # `visible: $show_background`）。
        self.assertTrue(scroll["$show_background"])
        button = self.panel["terminal_scroll_background@"
                             "endernet_ui_common.simple_invisible_button"]
        self.assertEqual(button["$pressed_button_name"], terminal_panel.QUICK_STORE_BINDING)

    def test_the_flying_item_is_anchored_where_the_script_measures_it(self):
        """飞行动画按**父节点坐标**摆位（`SetPosition`），因此这个控件必须
        左上锚定：锚在中心的话，脚本算出来的每一个位置都会偏半格。"""
        content = self.controls(self.panel["content"])
        flying = content["flying_item"]
        self.assertEqual(flying["anchor_from"], "top_left")
        self.assertEqual(flying["anchor_to"], "top_left")
        # 初始不可见：没有搬运时它应当在画面上不存在。
        self.assertFalse(flying["visible"])

    def test_the_flying_icon_is_the_same_size_as_the_icons_in_the_slots(self):
        """飞行图标要与**槽里那张图标**同尺寸——**不是与槽位面板同尺寸**。

        2026-10-01 之前这一条比的是 `item_can_be_selected` 的**面板**尺寸
        （18×18）并要求 `flying_item_renderer` 也是 18，于是它永远是红的
        （那个 renderer 从建立起就是 16）。用户当日定「**按 16×16 权威算**」，
        这条随之改成比**同类**：

        - `flying_item_renderer` 的 `size` == `flying_item.ICON_SIZE`；
        - 槽位里那张图标的尺寸也 == 它——两份槽位模板各是一种写法，
          因此这里把两种写法都折成像素再比。

        比错了对象与比错了数值一样坏：前者会因为「两个都在、只是量的不是
        一回事」而长期红着，红久了就没人看了。
        """
        # 飞行动画那个（`custom`，脚本 `SetUiItem` 画的就是它）。
        flying = self.container["flying_item_renderer"]["size"]
        self.assertEqual(flying, [flying_item.ICON_SIZE, flying_item.ICON_SIZE])
        # 槽位模板之一：`custom_slot` 的图标是显式像素值 + 显式居中。
        renderer = self.custom_slot_renderer()
        self.assertEqual(renderer["size"], [16, 16])
        self.assertEqual(renderer["anchor_from"], "center")
        self.assertEqual(renderer["anchor_to"], "center")
        self.assertEqual(renderer["size"][0], flying_item.ICON_SIZE)
        # 槽位模板之二：`item_can_be_selected` 的图标写的是 `100%+-2px`
        # （相对 18 的槽位即 16），且**不写 anchor 就是居中**。
        slot = self.windows["item_can_be_selected"]
        self.assertEqual(slot["size"], [18.0, 18.0])
        self.assertEqual(self.slot_renderer_size(), "100%+-2px")
        # `100%+-2px` 相对这个 18 的槽位就是 16。-2 与 `ICON_SIZE` 必须
        # 说同一件事：槽位一改而图标尺寸没跟着改，这一条先红。
        self.assertEqual(slot["size"][0] - 2, flying_item.ICON_SIZE)

    def custom_slot_renderer(self):
        """`endernet_container_common.custom_slot` 里的 `item_renderer`。"""
        return self.controls(self.container["custom_slot"])["item_renderer"]

    def slot_renderer_size(self):
        """`item_can_be_selected` 里的 `item_renderer` 的 `size`（字符串写法）。"""
        return self.controls(self.windows["item_can_be_selected"])[
            "item_renderer"]["size"][0]

    def test_the_unavailable_mask_actually_paints(self):
        """遮罩里那层变暗图必须画得出来。

        **2026-10-01 游戏内实测撞出来的**：`unavailable_mask` 会按绑定变可见
        （那一半是好的），但它唯一的子控件 `mask_img` 当时写死 `visible: false`
        且没有 anchor——运行时 `GetSize()` / `GetMaxSize()` 都是 `0×0`，把
        它 `SetVisible(True)` 也什么都不画。**遮罩整块是空的。**

        后果不是「不好看」：会话掉线（默认 TTL）之后界面看起来完全正常、
        列表还挂着旧数据，而 `on_select` 开头那条「没有会话就什么都不做」
        会让**下一次点击安静地什么都不发生**。玩家看到的是「点什么都没反应」。

        两条断言缺一不可：`visible` 不写死假值，`anchor_from`/`anchor_to`
        必须显式给出——`size` 用百分比，没有 anchor 时实测解析成 0。
        """
        mask = self.find_control(self.panel["content"], "unavailable_mask")
        self.assertIsNotNone(mask)
        # 遮罩面板本身由绑定控制显隐，这两个名字必须与脚本常量一致。
        self.assertIn(terminal_panel.UNAVAILABLE_BINDING,
                      self.binding_names(mask))
        mask_img = self.controls(mask)["mask_img"]
        self.assertTrue(mask_img.get("visible", True))
        self.assertEqual(mask_img["anchor_from"], "top_left")
        self.assertEqual(mask_img["anchor_to"], "top_left")
        self.assertEqual(mask_img["size"], ["100%+0px", "100%+0px"])

    # -- 设备总体状态（11-13）------------------------------------------

    def binding_names(self, control):
        return [b["binding_name"] for b in control.get("bindings", ())]

    def test_the_stat_bar_reads_its_text_color_and_visibility(self):
        """状态栏那三个绑定**缺一不可**，而且 `text` 必须自带阅读器。

        只挂 `binding_name_override` 而控件自己不写 `text: "#text"` 的话，
        引擎把值写进属性之后**没有人读它**：界面一片空白，而绑定回调一直在
        跑——这条 2026-10-01 在游戏里实测撞过（见 `label` 那笔记录）。
        """
        bar = {key.split("@")[0]: value
               for entry in self.common["device_stat_bar"]["controls"]
               for key, value in entry.items()}
        text = bar["stat_text"]
        self.assertEqual(text["text"], "#text")
        self.assertEqual(
            self.binding_names(text),
            [device_status.TEXT_BINDING, device_status.COLOR_BINDING,
             device_status.VISIBLE_BINDING])
        self.assertEqual(
            [b["binding_name_override"] for b in text["bindings"]],
            ["#text", "#color", "#visible"])
        # 初值仍是不显示：另外十七个界面没接这套绑定，不能因为模板改了
        # 就先亮起一行黄字。
        self.assertFalse(text["visible"])

    def test_the_stat_bar_icon_texture_is_driven_by_the_binding(self):
        """纹理只能走 `#texture` 这个**规范名**，不能自造一个。

        2026-10-01 游戏内实测撞出来的：原先写成 `texture: "#stat_icon_texture"`
        ＋ `binding_name_override: "#stat_icon_texture"`，两边名字自洽，界面上
        却是一块「材质丢失」。原因是 `texture` 属性只在绑定上下文里查
        `#texture`，自造的名字它不看。原版 42 处绑定纹理**无一例外**都是
        `binding_name_override: "#texture"`（如 `resource_packs_screen.json`
        的 `icon_image`、`pause_screen.json` 的 `permission_icon_image`）。
        """
        bar = {key.split("@")[0]: value
               for entry in self.common["device_stat_bar"]["controls"]
               for key, value in entry.items()}
        icon = bar["stat_icon"]
        self.assertEqual(self.binding_names(icon), [device_status.ICON_BINDING])
        self.assertEqual(icon["texture"], "#texture")
        self.assertEqual(icon["bindings"][0]["binding_name_override"], "#texture")
        # 绑定没接上的界面（其余十七个）退回静态的离线图标，而不是紫黑块。
        self.assertEqual(icon["property_bag"]["#texture"],
                         device_status.ICON_OFFLINE)

    def test_the_stat_bar_button_is_a_real_button_mapping(self):
        """`interact_btn` 早先是个编辑器占位（`%netease_editor_template_...`
        + 空 `button_mappings`），名字对不上的表现是**这一下点击安静地什么
        都不做**。"""
        bar = {key.split("@")[0]: value
               for entry in self.common["device_stat_bar"]["controls"]
               for key, value in entry.items()}
        button = bar["interact_btn"]
        self.assertEqual(button["$pressed_button_name"], device_status.OPEN_BINDING)
        self.assertEqual(button["button_mappings"][0]["to_button_id"],
                         "$pressed_button_name")
        self.assertEqual(button["button_mappings"][0]["mapping_type"], "pressed")

    def test_the_popup_instance_sits_above_the_close_button_and_is_bound(self):
        content = self.controls(self.panel["content"])
        popup = content["device_status_popup"]
        self.assertEqual(popup["bindings"][0]["binding_name"],
                         device_status.POPUP_VISIBLE_BINDING)
        self.assertEqual(popup["bindings"][0]["binding_name_override"], "#visible")
        self.assertGreater(popup["layer"], content["close"]["layer"],
                           "详情要盖在关闭按钮之上，否则它会被压在下面")

    def test_the_popup_is_centred_not_parked_at_a_corner(self):
        """详情要**屏幕居中**（2026-10-01 User 的口径）。

        它的父节点 `content`（180×220）本身就是居中的，所以在父节点里
        `anchor_from/to: center` 就等于屏幕居中；改成写死 `offset` 会把它
        钉在面板左上角那一带，窗口一挪或内容一改就偏。
        """
        popup = self.controls(self.panel["content"])["device_status_popup"]
        self.assertEqual(popup["anchor_from"], "center")
        self.assertEqual(popup["anchor_to"], "center")
        self.assertNotIn("offset", popup)

    def test_the_popup_template_blocks_click_through_and_starts_hidden(self):
        """`input_panel` 而不是 `panel`：点详情不该穿透到下面的终端列表。

        初值 `visible: false` 也是必须的——绑定还没生效的那一帧里，一个默认
        可见的弹窗会先糊在屏幕中央。
        """
        window = self.windows["device_status_window_basic"]
        self.assertEqual(window["type"], "input_panel")
        self.assertFalse(window["visible"])

    def test_the_popup_rows_bind_their_text_and_mask(self):
        rows = {key.split("@")[0]: value
                for entry in self.windows["device_status_window_basic"]["controls"]
                for key, value in entry.items()}
        for name, text_binding, icon_binding in (
                ("controller_conn_stat", device_status.MASTER_TEXT_BINDING,
                 device_status.MASTER_ICON_BINDING),
                ("network_conn_stat", device_status.NET_TEXT_BINDING,
                 device_status.NET_ICON_BINDING)):
            row = rows[name]
            label = row["controls"][1]["text"]
            self.assertEqual(label["text"], "#text")
            self.assertEqual(self.binding_names(label), [text_binding])
            mask = row["controls"][0]["icon"]["controls"][0]["stat_mask"]
            self.assertEqual(self.binding_names(mask), [icon_binding])
            # 与状态栏图标同一条规矩：只能覆盖规范的 `#texture`。
            self.assertEqual(mask["texture"], "#texture")
            self.assertEqual(mask["bindings"][0]["binding_name_override"], "#texture")
            self.assertEqual(mask["property_bag"]["#texture"],
                             device_status.MASK_OFFLINE)

    def test_the_popup_close_button_uses_its_own_name(self):
        close = self.windows["device_status_window_basic"]["controls"][2]
        close = list(close.values())[0]
        self.assertEqual(close["$pressed_button_name"], device_status.CLOSE_BINDING)
        self.assertEqual(close["button_mappings"][0]["to_button_id"],
                         "$pressed_button_name")


class TestPressedNamesAreActuallyClickable(unittest.TestCase):
    """设了按下名、却没把它接上按钮映射的控件，**点下去是安静地什么都不做**。

    2026-10-01 撞出来的：`terminal_scroll_background` 只覆盖了
    `$pressed_button_name`，而模板 `simple_invisible_button` 的
    `button_mappings` 是空的——`button.menu_select` 从来没有被映射到那个
    名字上，于是「点列表空白处整槽存进网络」这一下永远不触发。**离线全绿**：
    Python 侧的 `@Binder.binding` 注册得好好的，JSON 里名字也逐字对得上，
    缺的是把输入事件接到名字上的那条映射。

    这一条扫**全部**界面 JSON：凡是把 `$pressed_button_name` 写成真实绑定名
    （`#EnderNet…`，不是编辑器占位）的节点，同一个节点必须自带把
    `button.menu_select` 映射到 `$pressed_button_name` 的 `button_mappings`。
    间接传参的（如 `$item_select_button` 交给模板去拼）不在此列。
    """

    def ui_files(self):
        ui_dir = os.path.join(ROOT, "endernet_rp", "ui")
        for name in sorted(os.listdir(ui_dir)):
            if name.endswith(".json") and name != "_ui_defs.json":
                yield name, os.path.join(ui_dir, name)

    def test_every_real_pressed_name_has_a_button_mapping(self):
        # 计数器与清单都放进列表：py2 没有 `nonlocal`，嵌套函数里 `+=` 会
        # 把它变成局部名并当场 `UnboundLocalError`。
        checked = [0]
        offenders = []

        def walk(node, filename, trail):
            if isinstance(node, dict):
                pressed = node.get("$pressed_button_name")
                if isinstance(pressed, str) and pressed.startswith("#EnderNet"):
                    checked[0] += 1
                    mappings = node.get("button_mappings")
                    mapped = isinstance(mappings, list) and any(
                        isinstance(m, dict)
                        and m.get("to_button_id") == "$pressed_button_name"
                        for m in mappings)
                    if not mapped:
                        offenders.append("%s: %s -> %s" % (filename, trail, pressed))
                for key, value in node.items():
                    walk(value, filename, trail + "/" + key)
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, filename, trail + "/" + str(index))

        for filename, path in self.ui_files():
            with open(path, encoding="utf-8") as handle:
                walk(json.load(handle), filename, "")
        self.assertGreater(checked[0], 0,
                           "一个真实按下名都没扫到，说明这个扫法已经失效")
        self.assertEqual(offenders, [])


class TestDeviceStatusBindingsTolerateNoSession(unittest.TestCase):
    """绑定比 `OnCreate` 早生效，因此**每条都必须在没有 `status` 时也能答**。

    `OpenDemo` 那条路永远不建状态模型，而引擎照样每渲染帧问一遍。抛出去的
    后果与列表那半边的历史故障一样：每帧一条 `AttributeError`，界面看着只是
    「什么都没有」。
    """

    def setUp(self):
        self.screen = Screen()
        self.panel = terminal_panel.CommonTerminalPanel("main", self.screen, {})

    def test_every_status_binding_returns_a_safe_value(self):
        panel = self.panel
        self.assertIsNone(panel.status)
        self.assertEqual(panel.device_status_text(), "")
        self.assertFalse(panel.device_status_visible())
        self.assertEqual(panel.device_status_icon(), device_status.ICON_OFFLINE)
        self.assertFalse(panel.device_status_popup_visible())
        self.assertIn(panel.device_status_color(),
                      (device_status.COLOR_ONLINE, device_status.COLOR_OFFLINE))
        for getter in (panel.device_status_master_text, panel.device_status_net_text,
                       panel.device_status_master_icon, panel.device_status_net_icon):
            self.assertTrue(getter())

    def test_the_click_handlers_do_nothing_without_a_status(self):
        self.panel.OnDeviceStatusClick(None)
        self.panel.OnDeviceStatusClose(None)
        self.assertIsNone(self.panel.status)

    def test_deactivating_without_a_status_does_not_raise(self):
        """界面隐藏时也要能收尾：弹窗收起来，而 `status` 可能压根没建。"""
        self.panel.OnDeactive()
        self.assertIsNone(self.panel.status)


class TestTemplateConfigHook(PanelFixture):
    """11-05 留下的那个协作口子（`TerminalPanelBase.config`）。

    两条判据，缺一不可：

    1. **`config is None` 时行为与 11-03 定下的一字不差**——这是给通用／
       合成终端的回归保证，不是顺手一提；
    2. **接上协作对象之后，那两条手势由它接管**，且「接管」意味着**什么都
       不发**，而不是「发了但服务端会拒」。后者在界面上看起来一样，
       但玩家点空格的每一次都会换来一条回执。

    还钉住它插在**长按之后**：`inv_split` 是另一条手势，不能被它吃掉。
    """

    class RecordingConfig(object):
        def __init__(self, slot_click=True, row_click=True):
            self.slot_click = slot_click
            self.row_click = row_click
            self.slot_calls = []
            self.row_calls = []

        def consumes_slot_click(self, ui_index):
            self.slot_calls.append(ui_index)
            return self.slot_click

        def consumes_row_click(self, row_index):
            self.row_calls.append(row_index)
            return self.row_click

    def click_backpack(self, index=0):
        self.panel._click({"#collection_index": index}, 0, 27)

    def test_the_hook_defaults_to_nothing(self):
        self.assertIsNone(self.panel.__class__.config)
        self.assertIsNone(self.panel.config)

    def test_without_a_config_a_backpack_click_still_selects(self):
        self.engine_backpack((9, "minecraft:stone", 40))
        self.click_backpack(0)
        self.assertEqual(0, self.backpack.selected)

    def test_a_consuming_config_takes_the_click_and_nothing_happens(self):
        self.engine_backpack((9, "minecraft:stone", 40))
        config = self.RecordingConfig()
        self.panel.config = config
        self.addCleanup(setattr, self.panel, "config", None)
        before = len(self.commands())
        self.click_backpack(3)
        self.assertEqual([3], config.slot_calls)
        self.assertIsNone(self.backpack.selected, "既没选中也没搬")
        self.assertEqual(before, len(self.commands()))

    def test_a_non_consuming_config_falls_through_untouched(self):
        self.engine_backpack((9, "minecraft:stone", 40))
        config = self.RecordingConfig(slot_click=False)
        self.panel.config = config
        self.addCleanup(setattr, self.panel, "config", None)
        self.click_backpack(0)
        self.assertEqual([0], config.slot_calls, "问了，但没接管")
        self.assertEqual(0, self.backpack.selected)

    def test_a_long_press_still_wins_over_the_config(self):
        """长按是另一条手势：协作对象在它**之后**才轮到。"""
        self.engine_backpack((9, "minecraft:stone", 40))
        self.backpack.select(0)
        config = self.RecordingConfig()
        self.panel.config = config
        self.addCleanup(setattr, self.panel, "config", None)
        self.panel._clock = lambda: 100.0
        self.panel._press_index = 0
        self.panel._press_at = 0.0
        self.click_backpack(0)
        self.assertEqual([], config.slot_calls, "长按没轮到它")

    def test_a_consuming_config_takes_the_row_click_and_sends_nothing(self):
        self.engine_backpack((9, "minecraft:stone", 40))
        self.backpack.select(0)
        config = self.RecordingConfig()
        self.panel.config = config
        self.addCleanup(setattr, self.panel, "config", None)
        before = len(self.commands())
        self.view.on_select({"#collection_index": 0})
        self.assertEqual([0], config.row_calls)
        self.assertEqual(before, len(self.commands()), "不发 inv_take/inv_put")

    def test_without_a_config_the_row_click_is_unchanged(self):
        self.engine_backpack((9, "minecraft:stone", 40))
        self.backpack.select(0)
        before = len(self.commands())
        self.view.on_select({"#collection_index": 0})
        self.assertEqual(before + 1, len(self.commands()))
        self.assertEqual("inv_take", self.commands()[-1]["action"])

    def test_the_list_view_does_not_import_the_panel_class(self):
        """列表视图只从**传进来的那个屏幕对象**上取口子，不认识面板类。

        传进来的正是面板（`TerminalListView(self.panel, ...)`），因此
        `getattr(screen, "config", None)` 取到的就是面板上那一个。
        这里顺手钉住另一半：控件桩（`self.screen`）上有没有这个属性
        **与它无关**——`_fly` 取 `screen.fly` 是同一手法。
        """
        self.assertIsNone(getattr(self.view.screen, "config", None))
        config = self.RecordingConfig()
        self.view.screen.config = config
        self.addCleanup(delattr, self.view.screen, "config")
        before = len(self.commands())
        self.view.on_select({"#collection_index": 0})
        self.assertEqual([0], config.row_calls)
        self.assertEqual(before, len(self.commands()))


class TestKeepalive(PanelFixture):
    """终端面板不能只靠「发命令」续命（2026-10-02 真机撞出来的）。

    服务端按**多久没有 C2S 消息**回收会话（600 个脚本 tick ≈20 秒），而
    界面开着不动时一条命令都不发——于是 20 秒后会话没了、遮罩起来，
    表现是「点哪儿都没反应」。修法就是按帧发一条 `keepalive`
    （11-07 已经给容器代理屏做过同一件事）。
    """

    def kinds(self, since):
        return [item["message"]["kind"] for item in self.port.sent[since:]]

    def test_the_interval_is_the_same_number_as_the_proxy_panel(self):
        from endernet_bp.endernet_scripts.endernet.client.ui import proxy_panel

        self.assertEqual(proxy_panel.KEEPALIVE_FRAMES,
                         terminal_panel.KEEPALIVE_FRAMES)

    def test_it_fires_once_per_interval_and_not_before(self):
        before = len(self.port.sent)
        for _ in range(terminal_panel.KEEPALIVE_FRAMES - 1):
            self.view.tick()
        self.assertEqual([], self.kinds(before), "还没到间隔")
        self.view.tick()
        self.assertEqual(["keepalive"], self.kinds(before))

    def test_the_next_interval_starts_over(self):
        before = len(self.port.sent)
        for _ in range(terminal_panel.KEEPALIVE_FRAMES * 2):
            self.view.tick()
        self.assertEqual(["keepalive", "keepalive"], self.kinds(before))

    def test_without_a_session_it_stays_silent(self):
        self.client.drop_session()
        before = len(self.port.sent)
        for _ in range(terminal_panel.KEEPALIVE_FRAMES + 3):
            self.view.tick()
        self.assertEqual([], self.kinds(before))

    def test_a_failing_keepalive_never_escapes_the_frame(self):
        """保活是保活，不是业务：一次抖动不该让 `OnTicking` 每帧报错。"""
        def broken():
            raise RuntimeError("bus down")

        self.client.keepalive = broken
        for _ in range(terminal_panel.KEEPALIVE_FRAMES + 1):
            self.view.tick()


class TestEscapeKey(PanelFixture):
    """ESC 关屏走 `OnKeyPressInGame`，**不走 JSON 的 `button_mappings`**
    （那条路在真机上会触发脚本层奇怪的报错，User 2026-10-02 定）。

    离线拿不到 `tooldelta.events.client.control`（它在模块级 import 引擎），
    因此这条监听**在离线是「压根没注册」**，而不是「注册了一个不来的回调」。
    下面钉的正是这两件事：退化是安全的，以及判据本身是对的。
    """

    class Event(object):
        def __init__(self, key, is_down=1, screen="main"):
            self.key = key
            self.isDown = is_down
            self.screenName = screen

    def setUp(self):
        PanelFixture.setUp(self)
        # 真机上 `_do_active` 会把它置真，而监听器也正是在那一刻注册的。
        self.panel._activated = True
        self.removed = []
        self.panel.RemoveUI = lambda: self.removed.append(True)
        self.addCleanup(setattr, terminal_panel, "ESCAPE_KEY",
                        terminal_panel.ESCAPE_KEY)

    def test_offline_the_key_is_unresolved_and_that_is_safe(self):
        """离线时 `ESCAPE_KEY` 是 `None`；监听器随之永不触发，不报错。"""
        self.assertIsNone(terminal_panel.ESCAPE_KEY)
        terminal_panel.ESCAPE_KEY = None
        self.panel.OnEscapeKey(self.Event(27))
        self.assertEqual([], self.removed)

    def test_a_press_of_escape_closes_the_screen(self):
        terminal_panel.ESCAPE_KEY = 27
        self.panel.OnEscapeKey(self.Event(27))
        self.assertEqual([True], self.removed)

    def test_release_does_not_close_it(self):
        """只认按下：抬起时再来一次会把刚开的屏又关掉。"""
        terminal_panel.ESCAPE_KEY = 27
        self.panel.OnEscapeKey(self.Event(27, is_down=0))
        self.assertEqual([], self.removed)

    def test_other_keys_do_not_close_it(self):
        terminal_panel.ESCAPE_KEY = 27
        for key in (48, 65, 0):
            self.panel.OnEscapeKey(self.Event(key))
        self.assertEqual([], self.removed)

    def test_a_deactivated_screen_ignores_the_key(self):
        """`_do_deactive` 与事件派发之间可能隔着一帧，这里再挡一次。"""
        terminal_panel.ESCAPE_KEY = 27
        self.panel._activated = False
        self.panel.OnEscapeKey(self.Event(27))
        self.assertEqual([], self.removed)

    def test_a_malformed_event_never_raises(self):
        terminal_panel.ESCAPE_KEY = 27
        self.panel.OnEscapeKey(object())
        self.assertEqual([], self.removed)


if __name__ == "__main__":
    unittest.main()
