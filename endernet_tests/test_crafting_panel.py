# coding=utf-8
"""合成终端（11-04）的客户端半边：九宫格绑定、导入流程、多配方选择与界面接线。

守的是**界面与协议之间的那层缝**，缝里的错在游戏里几乎都看不出来：

- 九宫格绑定名对不上——九格全空，看着像「设备的格子里没东西」；
- 输出槽与九宫格共用一个集合——输出槽按九宫格的下标去读，界面照常渲染；
- 导入没带来玩家的选择（或者带了错的 digest）——命令发出去、回执回来，
  玩家却不知道东西为什么没进格子；
- `craft_state` 的旧包盖回来——画面上是几帧前的九宫格，而没有任何迹象。

界面的另一半（控件画没画出来、`$` 变量能不能替换、`load_recipe_window`
动态实例长什么样）离线测不了，属游戏内实测项，见
`plans/tasks/11-交付说明.md` 第二十节。

**本文件不测产物选择窗**（`select_item_window`）：那是既有组件、另有归属，
这里只钉「导入时交给它的是哪些候选、回调回来之后发了什么」。
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
    """与 `test_terminal_panel` 同一份标记值，取值必须一致。

    **两个文件各装一次替身**，而 `terminal_panel` / `crafting_panel` 都在导入期
    把 `Binder` 绑成了自己见到的那一份。两份替身的取值完全相同，因此
    「谁先导入」不影响任何断言——这里唯一要的是「旗标与 JSON 的
    `binding_type` 对得上」。
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
    stack_sizes = {}

    def __init__(self, item_id, count=1, orig=None):
        self.newItemName = item_id
        self.count = count
        self.newAuxValue = 0
        self._orig = orig if orig is not None else {}

    def GetBasicInfo(self):
        return types.SimpleNamespace(
            itemName=self.newItemName,
            id_aux=abs(hash(self.newItemName)) % 30000,
            maxStackSize=self.stack_sizes.get(self.newItemName, 64),
            maxDurability=0,
        )


class ControlStub(object):
    """控件替身：记下被写进去的每一件事，长出子控件。

    `AddElement` 是给动态窗口用的（候选配方一条一个 section）——真引擎里它是
    `CreateChildControl`，这里只要能按名字取回来就够断言了。
    """

    def __init__(self, definition=None, name=None):
        self.definition = definition
        self.name = name
        self.children = {}
        self.visible = None
        self.layer = None
        self.pos = None
        self.anchor_from = None
        self.anchor_to = None
        self.text = None
        self.item = None
        self.callback = None
        self.removed = False
        self.added = []

    def GetElement(self, path):
        # **按 `/` 逐层下钻**，不是把整条路径当一个键：真引擎解析的就是层级，
        # 两种写法在 `a/b` 上一致、在 `a/b/c` 上就会拿到两个不同的控件——
        # 而那种错的表现是「脚本写进去的值没有出现」，看起来像渲染问题。
        node = self
        for part in str(path).split("/"):
            node = node.children.setdefault(part, ControlStub())
        return node

    def AddElement(self, definition, name):
        child = ControlStub(definition, name)
        self.added.append((definition, name))
        self.children[name] = child
        return child

    def SetLayer(self, layer):
        self.layer = layer
        return self

    def SetVisible(self, visible):
        self.visible = visible
        return self

    def SetAnchorFrom(self, anchor):
        self.anchor_from = anchor
        return self

    def SetAnchorTo(self, anchor):
        self.anchor_to = anchor
        return self

    def SetPos(self, xy):
        self.pos = xy
        return self

    def SetCallback(self, callback):
        self.callback = callback
        return self

    def SetUiItem(self, name, aux):
        """**真引擎的形状是 `(name, aux)`，且两个都必填**（2026-10-03 真机实测：
        少传一个抛 `TypeError: SetUiItem() takes at least 3 arguments`）。

        替身以前写成单参 `SetUiItem(item)`，于是「传一个 `Item` 对象」那条
        **错的**调用形状在离线一路绿灯——而真机上它每次都抛，症状是**原料格
        全空、日志干净**。不设默认值是有意的：这样错形状会当场在离线变红。
        """
        self.item = types.SimpleNamespace(newItemName=name, newAuxValue=aux)
        return True

    def SetText(self, text):
        self.text = text
        return self

    def SetSprite(self, sprite):
        self.sprite = sprite

    asButton = asImage = asLabel = asItemRenderer = asGrid = asScrollView = (
        lambda self: self)

    def GetGridItem(self, x, y):
        return self.GetElement((x, y))

    def Remove(self):
        self.removed = True


class ScrollStub(object):
    """引擎 `ScrollViewUIControl` 的替身。**百分比方法长在它身上**（真机实测），
    `.base` 是 tooldelta 包装层才有的东西——本文件不依赖它。"""

    def __init__(self, content=None):
        self.percent = 0
        self.content = content

    def GetScrollViewPercentValue(self):
        return self.percent

    def SetScrollViewPercentValue(self, percent):
        self.percent = percent

    def asScrollView(self):
        return self

    def GetScrollViewContentControl(self):
        return self.content


class Screen(object):
    """引擎那个 UI 屏幕对象的替身：控件树、滚动条、绑定登记表、`_vars`。"""

    def __init__(self):
        self.bindings = {}
        self.collection_bindings = {}
        self.elements = {}
        self.scrolls = {}
        self.controls = {}
        self._activated = True
        self._vars = {}

    def GetElement(self, path):
        return self.elements.setdefault(path, ControlStub())

    def GetBaseUIControl(self, path):
        if path not in self.controls:
            raise ValueError("no control at %s" % (path,))
        return self.controls[path]

    def AddElement(self, definition, name):
        control = ControlStub(definition, name)
        self.controls[name] = control
        return control


class ScreenNodeStub(object):
    """引擎 `ScreenNode` 的替身。**用得上的是它没有的东西**：引擎按回调的
    名字在 `_Base` 上 `getattr`，名字不在类上就是 `AttributeError`。"""


def td_screen_attrs(cls):
    """`ToolDeltaScreen._get_tdscreen_bindings` 的等价实现（名字规则照抄）。"""
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
    """`tooldelta.ui.ToolDeltaScreen` 的替身：注册、控件树与 `_vars`。"""

    def __init__(self, screen_name, screen_instance, params=None):
        self.screen = screen_instance
        self._init_params = params if params is not None else {}
        self._activated = False
        self._vars = {}
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
            resolved = getattr(self._engine, func.__name__)
            if collection is None:
                self.screen.bindings[(flags, func.binding_name)] = resolved
            else:
                self.screen.collection_bindings[
                    (flags, collection, func.binding_name)] = resolved

    def GetElement(self, path):
        return self.screen.GetElement(path)

    def GetBaseUIControl(self, path):
        return self.screen.GetBaseUIControl(path)

    def AddElement(self, definition, name, force_update=True, _parent=None):
        return self.screen.AddElement(definition, name)

    def DynaBinding(self, flag, name, callback):
        raise AttributeError(getattr(callback, "__name__", ""))

    def DynaBindingCollection(self, flag, collection, name, callback):
        raise AttributeError(getattr(callback, "__name__", ""))

    def OnCreate(self):
        pass

    def OnDestroy(self):
        pass

    def OnTicking(self):
        pass


#: 导入期登记过的界面键。真 `RegistToolDeltaScreen` 会把它们写进
#: `registeredToolDeltaScreenClasses`；这里记下来，好钉住**注册键与定义表
#: 里的那串必须逐字相同**——对不上时右键是抛异常，不是开界面。
REGISTERED_KEYS = []


def _registrable(key, is_proxy=False):
    def decorate(cls):
        cls._screen_key = key
        #: 真 `RegistToolDeltaScreen` 的第二个开关（11-07）。设备屏走
        #: `is_proxy=True`（引擎开的原生屏，脚本挂代理），终端屏走默认。
        cls._td_is_proxy = is_proxy
        REGISTERED_KEYS.append(key)
        return cls
    return decorate


def _install_stubs():
    """把 tooldelta 的三个模块替掉。

    **必须赶在导入 `crafting_panel` 之前**：`crafting_panel` 经
    `select_item_window` 摸到了 `tooldelta.api.client.player`，而那一路会把
    vendored 的 `tooldelta` 包整个导进来——它在脱离游戏时必然失败
    （`tooldelta/events/client/control.py` 上那条 `mod.client.extraClientApi`，
    见 `network_client` 的模块文档）。

    **写进 `sys.modules` 而不是 `patch.dict`**：`crafting_panel` 的导入发生在
    本模块导入期，那时没有任何 patch 上下文。`tooldelta.ui` 与
    `tooldelta.define.item` 两份替身与 `test_terminal_panel` 完全一致（取值
    也一样），因此谁先导入都不影响断言。
    """
    for suffix, attrs in (
        ("tooldelta.ui", {
            "Binder": BinderStub,
            "RegistToolDeltaScreen": _registrable,
            "ToolDeltaScreen": ScreenBase,
        }),
        ("tooldelta.define.item", {"Item": ItemStub}),
        ("tooldelta.api.client.player",
         {"GetLocalPlayerHotbarAndInvItems": lambda: []}),
    ):
        module = sys.modules.get(PREFIX + suffix)
        if module is None:
            module = types.ModuleType(PREFIX + suffix)
            sys.modules[module.__name__] = module
        module.__dict__.update(attrs)


_install_stubs()

from endernet_bp.endernet_scripts.endernet.client.network_client import (  # noqa: E402
    NetworkClient,
)
from endernet_bp.endernet_scripts.endernet.client.ui import (  # noqa: E402
    crafting_panel, slot_layout, stack_size, terminal_panel,
)
from endernet_bp.endernet_scripts.endernet.client.ui.terminal_cache import (  # noqa: E402
    TerminalCache,
)
from endernet_bp.endernet_scripts.endernet.common.definitions import (  # noqa: E402
    TERMINAL_SCREENS,
)
from endernet_bp.endernet_scripts.endernet.common.identity import (  # noqa: E402
    DeviceRef, NodeKey,
)
from endernet_bp.endernet_scripts.endernet.common.protocol import (  # noqa: E402
    CRAFT_SLOT_COUNT,
    STATUS_OK,
    STATUS_REJECTED,
    CraftStateMessage,
    Reason,
    MSG_DEVICE_STATUS,
    SessionReady,
    SnapshotBegin,
    SnapshotChunk,
    SnapshotEnd,
    SyncEnvelope,
)
from endernet_bp.endernet_scripts.endernet.common.resource_key import (  # noqa: E402
    ResourceKey,
)

REF = DeviceRef(NodeKey(0, 10, 64, 20), 3)

STONE = ResourceKey.item("minecraft:stone")
SAND = ResourceKey.item("minecraft:sand")

SNAPSHOT_VERSION = 10

#: 一条**真的**候选配方投影（`PresetRecipe.to_dict()` 的形状，见
#: `common/crafting_recipe.py`）。`name` 是普通物品 ID 字符串，不是 ResourceKey。
RECIPE_A = {
    "ordered": True,
    "rows": ["PP", "PP"],
    "ingredients": [],
    "slots": [{"name": "minecraft:oak_planks", "aux": 0, "count": 1},
              {"name": "minecraft:oak_planks", "aux": 0, "count": 1},
              {"name": "minecraft:oak_planks", "aux": 0, "count": 1},
              {"name": "minecraft:oak_planks", "aux": 0, "count": 1},
              None, None, None, None, None],
    "outputs": [{"name": "minecraft:crafting_table", "aux": 0, "count": 1}],
    "digest": "aaaa1111",
}
RECIPE_B = {
    "ordered": False,
    "rows": [],
    "ingredients": [],
    "slots": [{"name": "minecraft:birch_planks", "aux": 0, "count": 1},
              None, None, None, None, None, None, None, None],
    "outputs": [{"name": "minecraft:crafting_table", "aux": 0, "count": 1}],
    "digest": "bbbb2222",
}


class RecordingPort(object):
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


def craft_slots(*entries):
    # type: (tuple) -> list
    """九宫格的线上形状。`entries` 是 `(槽号, 资源键, 数量)`；没给的格子留空。

    **五个键一个不能少**（`protocol.py` 的 `_require_fields` 既拒缺项也拒多项），
    空位是**四条一起空**而不是字段缺席。`name` / `aux` 是 2026-10-02 起随行带的
    ——客户端画一格靠 `#item_id_aux`，从指纹里反解名字是拿格式当接口。
    """
    rows = []
    for index in range(CRAFT_SLOT_COUNT):
        rows.append({"slot": index, "canonical": "", "count": 0,
                     "name": "", "aux": 0})
    for slot, key, count in entries:
        rows[slot] = {"slot": slot, "canonical": key.canonical(), "count": count,
                      "name": key.name, "aux": key.aux}
    return rows


class CraftingFixture(unittest.TestCase):
    def setUp(self):
        # `stack_size` 的缓存挂在模块上，跨用例存活，必须每条擦一次。
        self.addCleanup(stack_size.reset)
        stack_size.reset()
        self.port = RecordingPort()
        self.client = NetworkClient(port=self.port)
        self.screen = Screen()
        # 面板是**绑定的登记处**（十二条写作类体里：九宫格四条集合、产物槽四条集合、
        # 四个按钮），工作台只是被转发的那一头；
        # 收包回调也从面板那一侧拿——那两条接线一起被验到，而不是在夹具里
        # 另写一份。与 `test_terminal_panel.PanelFixture` 同一个形状。
        self.panel = crafting_panel.CraftingPanel("main", self.screen, {})
        # 生产里界面建起来之后 `_activated` 是真：`Remove()` 那一类收尾
        # 才真的执行（离线替身默认是假，不设的话「关窗」只关一半）。
        self.panel._activated = True
        self.craft = crafting_panel.CraftingView(self.panel, client=self.client)
        self.panel.craft = self.craft
        self.client.on_message(self.panel._make_craft_handler(self.client))
        self.addCleanup(self.craft.close)
        self.feed(SessionReady(7, SNAPSHOT_VERSION))

    def feed(self, message, session_id="s1", network_id="n1", epoch="e1"):
        return self.client.receive(
            SyncEnvelope(message, session_id, network_id, epoch).to_dict()
        )

    def state(self, slots=None, recipe="", preview=None, revision=1):
        self.feed(CraftStateMessage(
            revision, slots if slots is not None else craft_slots(),
            recipe, preview))
        return self.craft

    def snapshot(self, records, version=SNAPSHOT_VERSION, snapshot_id="snap-1"):
        """喂一份**网络列表**的快照。

        **不是 `InventorySnapshot`**：那个是**玩家背包**的快照（钉死 36 格），
        由 `NetworkClient` 直接收下、不入队；网络列表吃的是
        `snapshot_begin/chunk/end` 那三件。喂错了的表现是列表永远是空的
        ——而「网络里没东西」看起来完全正常。
        """
        self.feed(SnapshotBegin(snapshot_id, version, len(records)))
        for index, record in enumerate(records):
            self.feed(SnapshotChunk(snapshot_id, index, (record,)))
        self.feed(SnapshotEnd(snapshot_id, len(records)))

    def list_view(self):
        """给面板挂一个网络列表视图（生产接线就是从 `OnCreate` 里挂的）。"""
        view = terminal_panel.TerminalListView(
            self.panel, cache=TerminalCache(client=self.client),
            device_ref=REF, dimension=0, scroll_path=None)
        self.panel.list_view = view
        return view

    def commands(self):
        return [item["message"] for item in self.port.sent
                if item["message"]["kind"] == "command"]

    def result(self, request_id, status=STATUS_OK, payload=None, reasons=None):
        return {"request_id": request_id, "status": status,
                "payload": payload if payload is not None else {},
                "reasons": reasons if reasons is not None else [],
                "topology_version": None}

    def deliver(self, result):
        """把一条回执交给工作台——**走生产的那条路**：列表视图取走、转发过来。

        直接调 `_settle` 是不行的：取回执那条队列是「取走即出队」，谁先取谁
        拿到，而生产里取的是列表视图。夹具照那条路走，两边的接线才一起被验到。
        """
        self.client._results.append(types.SimpleNamespace(result=result))
        taken = self.client.take_command_results()
        self.craft.on_results(taken)

    # -- 绑定取值口 ----------------------------------------------------

    def cell(self, flag, name, index):
        key = (flag, crafting_panel.GRID_COLLECTION,
               crafting_panel.BINDING_PREFIX + name)
        return self.screen.collection_bindings[key](index)

    def output(self, flag, name):
        key = (flag, crafting_panel.OUTPUT_COLLECTION,
               crafting_panel.BINDING_PREFIX + name)
        return self.screen.collection_bindings[key](0)

    def button(self, name, *args):
        key = (BinderStub.BF_ButtonClickUp, crafting_panel.BINDING_PREFIX + name)
        return self.screen.bindings[key](*args)


class TestCraftState(CraftingFixture):
    """`craft_state` 进得来、存得对、旧包盖不回去。"""

    def test_the_message_lands_in_the_fixture_and_lands_whole(self):
        self.state(
            craft_slots((0, STONE, 1), (4, SAND, 3)),
            recipe="aaaa1111",
            preview={"name": "minecraft:stick", "aux": 0, "count": 4},
            revision=5)
        craft = self.craft
        self.assertEqual(craft.revision, 5)
        self.assertEqual(craft.recipe, "aaaa1111")
        self.assertEqual(craft.preview,
                         {"name": "minecraft:stick", "aux": 0, "count": 4})
        self.assertIsNotNone(craft.cell(0))
        self.assertIsNotNone(craft.cell(4))
        self.assertIsNone(craft.cell(1))

    def test_an_empty_slot_is_canonical_empty_and_count_zero_together(self):
        """**两个条件都看**：只看一个会把另一类行读成空位或读成实物。"""
        slots = craft_slots()
        slots[3] = {"slot": 3, "canonical": "", "count": 7, "name": "", "aux": 0}
        slots[5] = {"slot": 5, "canonical": STONE.canonical(), "count": 0,
                    "name": "minecraft:stone", "aux": 0}
        self.state(slots)
        self.assertIsNone(self.craft.cell(3))
        self.assertIsNone(self.craft.cell(5))

    def test_the_item_name_comes_straight_off_the_wire(self):
        """**不建客户端索引**：名字由服务端随行带来，客户端照用。

        这条钉住「不再需要别的来源」——格子里的东西是服务端从背包与网络借来
        的，客户端看得见的那一份未必覆盖它；索引漏一次就是「只有数量、没有
        图标」，而那看起来像引擎没渲染。
        """
        self.state(craft_slots((0, STONE, 6)))
        cell = self.craft.cell(0)
        self.assertEqual(cell["name"], "minecraft:stone")
        self.assertNotEqual(self.craft.cell_id_aux(0), 0)
        # 名字与数量是两件事：名字来自线上，数量也来自线上。
        self.assertEqual(self.craft.cell_count(0), "6")

    def test_a_row_without_a_name_renders_nothing_rather_than_guessing(self):
        """线上没有名字时**不猜**：`id_aux` 给 0，画面留白。

        **走 `_cell_of` 而不是走消息**：`protocol.py` 已经拒了「有 key 却没有
        名字」的行（`craft slot has a key but no item name`），因此那种形状
        根本到不了这里。这一条守的是读取那一层的**退化方向**——猜一个名字出来
        会画出**另一件**物品的图标，那比留白严重得多。
        """
        row = craft_slots((0, STONE, 1))[0]
        row["name"] = ""
        cell = crafting_panel._cell_of(row)
        self.assertEqual(cell["name"], "")
        self.assertEqual(crafting_panel._cell_of({"slot": 0}) , None)
        # 空位整行缺字段也一样读成空位，而不是造一个「有数量没名字」的格子。
        self.assertIsNone(crafting_panel._cell_of(
            {"slot": 0, "canonical": "", "count": 0, "name": "", "aux": 0}))

    def test_an_older_revision_never_overwrites_the_newer_one(self):
        """乱序到达时**旧画面不能盖回来**。

        症状是「格子里突然少了一件东西」，几帧后又自己回来——而玩家多半会
        以为自己看花了眼。
        """
        self.state(craft_slots((0, STONE, 1)), revision=9)
        self.state(craft_slots(), revision=4)
        self.assertIsNotNone(self.craft.cell(0))
        self.assertEqual(self.craft.revision, 9)

    def test_the_same_revision_is_applied_again(self):
        """同版本重推是幂等的，不算「旧包」——服务端按载荷去重，重推是允许的。"""
        self.state(craft_slots((0, STONE, 1)), revision=3)
        self.state(craft_slots(), revision=3)
        self.assertIsNone(self.craft.cell(0))

    def test_the_other_sessions_state_is_dropped_by_the_panel_handler(self):
        """会话号对不上就丢。**这一条在面板那侧**，夹具这里用真面板再验一次，
        见 `TestPanelEntry`。"""
        self.feed(CraftStateMessage(1, craft_slots((0, STONE, 1)), "", None),
                  session_id="s9")
        self.assertIsNone(self.craft.cell(0))


class TestGridBindings(CraftingFixture):
    """九宫格与产物槽的绑定：名字、集合、取值。"""

    def test_the_grid_bindings_are_registered_under_their_own_collection(self):
        for flag, name in (
            (BinderStub.BF_BindBool, "grid_visible"),
            (BinderStub.BF_BindInt, "grid_item_id_aux"),
            (BinderStub.BF_BindString, "grid_item_count"),
            (BinderStub.BF_BindBool, "grid_selected"),
        ):
            key = (flag, crafting_panel.GRID_COLLECTION,
                   crafting_panel.BINDING_PREFIX + name)
            self.assertIn(key, self.screen.collection_bindings)

    def test_the_output_bindings_use_a_different_collection(self):
        """**输出槽与九宫格必须各用一套集合。**

        共用一套的话，输出槽会按九宫格的下标（0）去读——读到的正好是第一格
        原料，于是产物槽里画着第一格原料、界面照常渲染、不报任何错。
        """
        self.assertNotEqual(crafting_panel.GRID_COLLECTION,
                            crafting_panel.OUTPUT_COLLECTION)
        for flag, name in (
            (BinderStub.BF_BindBool, "output_visible"),
            (BinderStub.BF_BindInt, "output_item_id_aux"),
            (BinderStub.BF_BindString, "output_item_count"),
        ):
            key = (flag, crafting_panel.OUTPUT_COLLECTION,
                   crafting_panel.BINDING_PREFIX + name)
            self.assertIn(key, self.screen.collection_bindings)

    def test_the_grid_draws_every_cell_including_the_empty_ones(self):
        """空格子也要画槽底：`#visible` 恒真，空是用 `#item_id_aux == 0` 表达的。"""
        self.state(craft_slots((0, STONE, 1)))
        for index in range(CRAFT_SLOT_COUNT):
            self.assertTrue(self.cell(BinderStub.BF_BindBool, "grid_visible", index))
        self.assertNotEqual(
            self.cell(BinderStub.BF_BindInt, "grid_item_id_aux", 0), 0)
        self.assertEqual(
            self.cell(BinderStub.BF_BindInt, "grid_item_id_aux", 1), 0)
        self.assertFalse(self.cell(BinderStub.BF_BindBool, "grid_selected", 0))

    def test_a_count_of_one_is_blank_and_more_is_shown(self):
        self.state(craft_slots((0, STONE, 1), (1, SAND, 12)))
        self.assertEqual(
            self.cell(BinderStub.BF_BindString, "grid_item_count", 0), "")
        self.assertEqual(
            self.cell(BinderStub.BF_BindString, "grid_item_count", 1), "12")

    def test_an_out_of_range_index_is_not_read_as_a_cell(self):
        """集合回调的下标是外部输入：`True` 是 `int` 的子类，`[-1]` 不报错。"""
        self.state(craft_slots((8, STONE, 1)))
        for bad in (True, False, -1, CRAFT_SLOT_COUNT, "0", None):
            self.assertIsNone(self.craft.cell(bad))
            self.assertFalse(self.craft.has_cell(bad))
            self.assertEqual(self.craft.cell_id_aux(bad), 0)

    def test_the_output_slot_only_shows_when_there_is_something_to_make(self):
        self.state()
        self.assertFalse(self.output(BinderStub.BF_BindBool, "output_visible"))
        self.assertEqual(
            self.output(BinderStub.BF_BindInt, "output_item_id_aux"), 0)
        self.state(preview={"name": "minecraft:stick", "aux": 0, "count": 4},
                   revision=2)
        self.assertTrue(self.output(BinderStub.BF_BindBool, "output_visible"))
        self.assertNotEqual(
            self.output(BinderStub.BF_BindInt, "output_item_id_aux"), 0)
        self.assertEqual(
            self.output(BinderStub.BF_BindString, "output_item_count"), "4")

    def test_the_cell_icon_does_not_depend_on_the_network_list(self):
        """**图标只认线上带来的名字**，与网络列表里有什么无关。

        这条是那个被删掉的索引的**反面**：曾经有一版从网络列表与玩家背包
        灌 `canonical -> 资源名` 的索引，于是「列表里没有这一种」就退化成
        「格子里只有数量、没有图标」——而九宫格里的东西是服务端借来的，
        客户端看得见的那一份未必覆盖它。
        """
        self.state(craft_slots((0, STONE, 1)))
        self.assertNotEqual(
            self.cell(BinderStub.BF_BindInt, "grid_item_id_aux", 0), 0)

    def test_crafting_does_not_touch_the_backpack_poll(self):
        """`OnTicking` 里那三件事（动画、背包轮询、列表推进）**一件都不能少**：
        本类没有 `super()`，基类的 `_poll_backpack` 只有显式调用才会跑，
        漏掉的表现是「背包一直不更新」（同 `CommonTerminalPanel`）。
        """
        self.list_view()
        # `_poll_frames` 是那条轮询自己的计数器：它涨了就说明 `_poll_backpack`
        # 真的被调到了。（离线读不到 36 格，`replace_from_engine` 把它收成空表
        # 并计数——那是它自己的退化路径，与本条无关。）
        self.panel._poll_frames = 0
        self.panel.OnTicking()
        self.assertEqual(self.panel._poll_frames, 1)

    def test_no_session_masks_the_whole_workbench(self):
        self.assertFalse(self.craft.is_unavailable())
        self.client.drop_session()
        self.assertTrue(self.craft.is_unavailable())


class TestImportFlow(CraftingFixture):
    """导入：选产物 → 一条配方直接发、多条走候选窗口。"""

    def setUp(self):
        CraftingFixture.setUp(self)
        self.picker_calls = []
        patcher = patch.object(
            crafting_panel, "show_select_item_window",
            lambda screen, ids, callback: self._show(screen, ids, callback))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _show(self, screen, ids, callback):
        self.picker_calls.append((ids, callback))
        return True

    def test_import_hands_the_network_rows_to_the_picker(self):
        """候选来自**网络列表当前可见的行**，不是整份库存，也不是空表。"""
        view = self.list_view()
        self.cache_feed(view)
        self.assertTrue(self.craft.on_import())
        self.assertEqual(len(self.picker_calls), 1)
        ids, callback = self.picker_calls[0]
        self.assertEqual(sorted(ids), ["minecraft:sand", "minecraft:stone"])
        self.assertIsNotNone(callback)

    def cache_feed(self, view):
        self.snapshot([(STONE, 40, 4, False), (SAND, 2, 0, False)])
        view.cache.tick()
        view.refresh()

    def test_a_chosen_product_sends_the_import_with_its_key(self):
        self.craft.on_product_chosen("minecraft:stick")
        command = self.commands()[-1]
        self.assertEqual(command["action"], "craft_import")
        self.assertEqual(command["key"]["name"], "minecraft:stick")
        self.assertIsNone(command["recipe"])

    def test_import_needs_a_session(self):
        self.client.drop_session()
        self.assertFalse(self.craft.on_import())
        self.assertEqual(self.picker_calls, [])

    def test_a_chosen_product_without_a_session_sends_nothing(self):
        self.client.drop_session()
        self.assertIsNone(self.craft.on_product_chosen("minecraft:stick"))
        self.assertEqual(self.commands(), [])

    def test_several_recipes_open_the_choice_window_and_keep_the_candidates(self):
        self.craft.on_product_chosen("minecraft:crafting_table")
        request_id = self.commands()[-1]["request_id"]
        self.deliver(self.result(request_id, payload={
            "action": "craft_import",
            "needs_choice": True,
            "output": {"name": "minecraft:crafting_table", "aux": 0},
            "candidates": [RECIPE_A, RECIPE_B],
        }))
        self.assertIsNotNone(self.craft.choice)
        self.assertEqual(len(self.craft.choice["candidates"]), 2)
        self.assertEqual(self.craft.choice["output"]["name"],
                         "minecraft:crafting_table")
        self.assertIsNotNone(self.craft.window)
        self.assertEqual(self.craft.window.shown, 2)

    def test_choosing_a_candidate_resends_with_its_digest(self):
        self.craft.on_product_chosen("minecraft:crafting_table")
        request_id = self.commands()[-1]["request_id"]
        self.deliver(self.result(request_id, payload={
            "action": "craft_import", "needs_choice": True,
            "output": {"name": "minecraft:crafting_table", "aux": 0},
            "candidates": [RECIPE_A, RECIPE_B],
        }))
        self.craft.on_choice("bbbb2222")
        command = self.commands()[-1]
        self.assertEqual(command["action"], "craft_import")
        self.assertEqual(command["recipe"], "bbbb2222")
        self.assertEqual(command["key"]["name"], "minecraft:crafting_table")
        self.assertIsNone(self.craft.choice)
        self.assertIsNone(self.craft.window)

    def test_a_digest_that_is_not_in_the_candidates_sends_nothing(self):
        """本地的候选表先比一遍：不比对的话，一个来路不明的 digest 会换回一条
        `craft_no_recipe`，而玩家只是点了一下界面。"""
        self.craft.on_product_chosen("minecraft:crafting_table")
        request_id = self.commands()[-1]["request_id"]
        self.deliver(self.result(request_id, payload={
            "action": "craft_import", "needs_choice": True,
            "output": {"name": "minecraft:crafting_table", "aux": 0},
            "candidates": [RECIPE_A],
        }))
        before = len(self.commands())
        self.assertIsNone(self.craft.on_choice("deadbeef"))
        self.assertEqual(len(self.commands()), before)

    def test_a_single_recipe_needs_no_choice_window(self):
        self.craft.on_product_chosen("minecraft:stick")
        request_id = self.commands()[-1]["request_id"]
        self.deliver(self.result(request_id, payload={
            "action": "craft_import", "recipe": "aaaa1111", "imported": 2,
            "missing": [3], "grid": {"revision": 4, "recipe": "aaaa1111",
                                     "occupied": [0, 1]},
        }))
        self.assertIsNone(self.craft.choice)
        self.assertIsNone(self.craft.window)
        # **保留的配方以 `craft_state` 为准**，不是以回执为准：回执里的
        # `grid` 只有 revision / recipe / occupied，没有格子内容（服务端那一侧
        # 的形状），而画格子要的是内容。回执之后服务端会推一份新的 `craft_state`。
        self.assertEqual(self.craft.recipe, "")

    def test_cancelling_the_choice_sends_nothing(self):
        self.craft.on_product_chosen("minecraft:crafting_table")
        request_id = self.commands()[-1]["request_id"]
        self.deliver(self.result(request_id, payload={
            "action": "craft_import", "needs_choice": True,
            "output": {"name": "minecraft:crafting_table", "aux": 0},
            "candidates": [RECIPE_A, RECIPE_B],
        }))
        before = len(self.commands())
        self.craft.window.close()
        self.assertEqual(len(self.commands()), before)
        self.assertIsNone(self.craft.choice)

    def test_a_needs_choice_without_candidates_is_not_a_window(self):
        self.craft.on_product_chosen("minecraft:crafting_table")
        request_id = self.commands()[-1]["request_id"]
        self.deliver(self.result(request_id, payload={
            "action": "craft_import", "needs_choice": True,
            "output": {"name": "minecraft:crafting_table", "aux": 0},
            "candidates": [],
        }))
        self.assertIsNone(self.craft.choice)
        self.assertIsNone(self.craft.window)


class TestTakeAndClear(CraftingFixture):
    """点输出槽与清空：各一条命令，不多不少。"""

    def test_take_sends_exactly_one_command_without_a_key(self):
        self.craft.on_take()
        command = self.commands()[-1]
        self.assertEqual(command["action"], "craft_take")
        self.assertIsNone(command["key"])
        self.assertEqual(len(self.commands()), 1)

    def test_clear_sends_exactly_one_command(self):
        self.craft.on_clear()
        self.assertEqual(self.commands()[-1]["action"], "craft_clear")

    def test_both_need_a_session(self):
        self.client.drop_session()
        self.assertIsNone(self.craft.on_take())
        self.assertIsNone(self.craft.on_clear())
        self.assertEqual(self.commands(), [])

    def test_clicking_a_grid_cell_does_nothing_at_all(self):
        """R12 未定案：放入／取出／拖拽本轮一格都不接。**这一下什么都不发。**"""
        self.state(craft_slots((0, STONE, 1)))
        self.assertIsNone(
            self.button("grid_click", {"#collection_index": 0}))
        self.assertEqual(self.commands(), [])

    def test_the_take_button_binding_is_wired_to_the_take_action(self):
        self.button("take")
        self.assertEqual(self.commands()[-1]["action"], "craft_take")


class TestFailures(CraftingFixture):
    """失败原因要**分得清**：玩家能不能看懂「下一步该点哪儿」全靠它。"""

    def reasons(self, code):
        return [{"code": code, "detail": ""}]

    def run_rejected(self, code):
        self.craft.on_clear()
        request_id = self.commands()[-1]["request_id"]
        self.deliver(self.result(request_id, status=STATUS_REJECTED,
                                 reasons=self.reasons(code)))
        return self.craft.last_error

    def test_the_occupied_grid_points_at_clear(self):
        text = self.run_rejected(Reason.CRAFT_GRID_OCCUPIED)
        self.assertIn("清空", text)

    def test_no_capacity_says_the_backpack_is_full(self):
        self.assertEqual(self.run_rejected(Reason.NO_CAPACITY), "背包放不下")

    def test_no_recipe_and_insufficient_stock_are_distinct(self):
        no_recipe = self.run_rejected(Reason.CRAFT_NO_RECIPE)
        short = self.run_rejected(Reason.INSUFFICIENT_STOCK)
        self.assertTrue(no_recipe)
        self.assertTrue(short)
        self.assertNotEqual(no_recipe, short)

    def test_an_unknown_reason_says_nothing_rather_than_showing_a_code(self):
        """`no_capacity` 这种词对玩家什么也没说；把它塞进提示里只会让人以为
        界面出了别的毛病。"""
        self.assertEqual(self.run_rejected("something_new"), "")

    def test_a_successful_command_clears_the_last_error(self):
        self.run_rejected(Reason.NO_CAPACITY)
        self.assertTrue(self.craft.last_error)
        self.craft.on_clear()
        request_id = self.commands()[-1]["request_id"]
        self.deliver(self.result(request_id, payload={
            "action": "craft_clear", "returned": 3, "kept": [],
            "grid": {"revision": 2, "recipe": "", "occupied": []},
        }))
        self.assertIsNone(self.craft.last_error)

    def test_a_receipt_we_did_not_send_is_ignored(self):
        self.deliver(self.result("c999", status=STATUS_REJECTED,
                                 reasons=self.reasons(Reason.NO_CAPACITY)))
        self.assertIsNone(self.craft.last_error)

    def test_losing_the_session_drops_pending_work_and_the_window(self):
        self.craft.on_product_chosen("minecraft:crafting_table")
        request_id = self.commands()[-1]["request_id"]
        self.deliver(self.result(request_id, payload={
            "action": "craft_import", "needs_choice": True,
            "output": {"name": "minecraft:crafting_table", "aux": 0},
            "candidates": [RECIPE_A],
        }))
        self.assertIsNotNone(self.craft.window)
        self.client.drop_session()
        self.craft.tick()
        self.assertIsNone(self.craft.choice)
        self.assertIsNone(self.craft.window)
        self.assertEqual(self.craft.diagnostics()["in_flight"], 0)


class TestRecipeChoiceWindow(CraftingFixture):
    """候选窗口：铺几条、填什么、点哪条回哪条。"""

    def open_window(self, candidates):
        self.craft.choice = {
            "output": {"name": "minecraft:crafting_table", "aux": 0},
            "candidates": list(candidates),
        }
        self.assertTrue(self.craft.open_window())
        return self.craft.window

    def test_it_creates_one_section_per_candidate_up_to_the_cap(self):
        window = self.open_window([RECIPE_A, RECIPE_B])
        self.assertEqual(window.shown, 2)
        # 窗口根注册在屏幕 `_vars` 上：**它就是「已经开着一个」的判据**，
        # 不记的话下一次导入会再开一个，两个窗口叠在一起。
        self.assertIs(self.panel._vars[window.WINDOW_ATTRIBUTE], window)
        self.assertEqual(window.root.definition,
                         crafting_panel.RECIPE_WINDOW_DEFINITION)

    def test_it_never_lays_out_more_rows_than_the_cap(self):
        many = [dict(RECIPE_A, digest="d%04d" % (index,))
                for index in range(crafting_panel.RECIPE_MAX_ROWS + 4)]
        window = self.open_window(many)
        self.assertEqual(window.shown, crafting_panel.RECIPE_MAX_ROWS)

    def test_each_row_shows_its_ingredients_and_output(self):
        window = self.open_window([RECIPE_A])
        section = window.root.GetElement("candidate_0")
        slot0 = section.GetElement("input_slots/slot0")
        self.assertTrue(slot0.visible)
        self.assertEqual(slot0.GetElement("item_renderer").item.newItemName,
                         "minecraft:oak_planks")
        # 配方没有原料的位置留空：藏起来，而不是留一张空槽底图。
        self.assertFalse(section.GetElement("input_slots/slot4").visible)
        output = section.GetElement("output_slot")
        self.assertTrue(output.visible)
        self.assertEqual(output.GetElement("item_renderer").item.newItemName,
                         "minecraft:crafting_table")

    def test_the_count_label_only_shows_above_one(self):
        window = self.open_window([RECIPE_A])
        section = window.root.GetElement("candidate_0")
        self.assertEqual(
            section.GetElement("input_slots/slot0/item_count").text, "")

    def test_the_confirm_button_reports_its_own_recipe(self):
        chosen = []
        self.craft.choice = {
            "output": {"name": "minecraft:crafting_table", "aux": 0},
            "candidates": [RECIPE_A, RECIPE_B],
        }
        window = crafting_panel.RecipeChoiceWindow(
            self.screen, self.craft.choice["output"],
            self.craft.choice["candidates"], chosen.append)
        second = window.root.GetElement("candidate_1")
        root = window.root
        callback = second.GetElement("confirm_btn").callback
        self.assertIsNotNone(callback)
        callback(None)
        self.assertEqual(chosen, ["bbbb2222"])
        # 点完就关：窗口留着的话玩家会以为还能再点一次，而那条命令已经发出去了。
        self.assertTrue(window.closed)
        self.assertTrue(root.removed)

    def test_opening_without_a_session_is_refused(self):
        self.client.drop_session()
        self.craft.choice = {"output": {}, "candidates": [RECIPE_A]}
        self.assertFalse(self.craft.open_window())
        self.assertIsNone(self.craft.window)

    def test_a_window_that_cannot_be_created_only_counts(self):
        """引擎拒绝建控件时**只计数**：抛出会把一次界面故障变成整个面板的故障，
        而这两条路的风险完全不对等。"""
        self.craft.choice = {"output": {}, "candidates": [RECIPE_A]}
        with patch.object(self.screen, "AddElement",
                          side_effect=ValueError("no such control")):
            self.assertFalse(self.craft.open_window())
        self.assertEqual(self.craft.errors, 1)
        self.assertIsNone(self.craft.window)
        self.assertEqual(self.panel._vars, {})

    def test_the_window_prefers_the_scroll_content_and_falls_back_to_the_root(self):
        """内容控件由引擎回答，不猜路径；取不到就退到窗口根（**可见的**故障，
        比静默什么都不显示好查）。"""
        content = ControlStub()
        self.screen.controls[
            crafting_panel.RECIPE_SCROLL_PATH + "/scroll_mouse/scroll_view"] = (
                ScrollStub(content))
        window = self.open_window([RECIPE_A])
        self.assertIs(window._content(), content)
        self.assertEqual(content.added,
                         [(crafting_panel.RECIPE_SECTION_DEFINITION,
                           "candidate_0")])


class TestPanelEntry(unittest.TestCase):
    """面板的装配：拿到权威身份才建会话，销毁要把两半都收干净。"""

    def setUp(self):
        self.addCleanup(stack_size.reset)
        stack_size.reset()

    def make(self, params):
        screen = Screen()
        port = RecordingPort()
        with patch.object(crafting_panel, "NetworkClient",
                          lambda: NetworkClient(port=port)):
            panel = crafting_panel.CraftingPanel("main", screen, params)
            panel.OnCreate()
        return panel, screen, port

    @staticmethod
    def ready(panel):
        """把一个会话装上。离线端口不会自己回 `session_ready`。"""
        client = panel.craft.client
        client.receive(SyncEnvelope(
            SessionReady(7, SNAPSHOT_VERSION), "s1", "n1", "e1").to_dict())
        return client

    def test_no_device_ref_means_no_session_no_list_and_no_workbench(self):
        panel, screen, port = self.make({"items": None})
        self.assertIsNone(panel.craft)
        self.assertIsNone(panel.list_view)
        self.assertEqual(port.sent, [])

    def test_a_device_ref_opens_the_session_and_wires_both_halves(self):
        panel, screen, port = self.make(
            {"device_ref": REF.to_dict(), "dimension": 0})
        self.addCleanup(panel.OnDestroy)
        self.assertIsNotNone(panel.craft)
        self.assertIsNotNone(panel.list_view)
        self.assertEqual([item["message"]["kind"] for item in port.sent],
                         ["session_open"])
        # 收包在开会话**之前**就挂上了：`session_ready` 与第一份 `craft_state`
        # 都可能比 `OnCreate` 返回还早，晚一步那两份就永远补不上。
        self.assertEqual(port.listens, 1)
        # 三条回调：设备状态一条、`craft_state` 一条，外加**下单预览一条**
        # （11 增量：预览窗与列表同寿命，`TerminalListView.__init__` 里挂上）。
        self.assertEqual(len(panel.craft.client._handlers), 3)

    def test_a_broken_device_ref_pauses_instead_of_opening_a_wrong_session(self):
        for broken in ({"node_key": {"dimension": 0}}, "terminal-1", 7):
            panel, screen, port = self.make(
                {"device_ref": broken, "dimension": 0})
            self.assertIsNone(panel.craft)
            self.assertEqual(port.sent, [])

    def test_the_craft_handler_drops_another_sessions_message(self):
        panel, screen, port = self.make(
            {"device_ref": REF.to_dict(), "dimension": 0})
        self.addCleanup(panel.OnDestroy)
        self.ready(panel)
        envelope = SyncEnvelope(CraftStateMessage(
            1, craft_slots((0, STONE, 1)), "", None), "s9", "n1", "e1")
        panel.craft.client.receive(envelope.to_dict())
        self.assertIsNone(panel.craft.cell(0))

    def test_the_panel_handler_applies_its_own_sessions_message(self):
        panel, screen, port = self.make(
            {"device_ref": REF.to_dict(), "dimension": 0})
        self.addCleanup(panel.OnDestroy)
        client = self.ready(panel)
        envelope = SyncEnvelope(CraftStateMessage(
            1, craft_slots((0, STONE, 1)), "", None),
            client.session_id, "n1", "e1")
        client.receive(envelope.to_dict())
        self.assertIsNotNone(panel.craft.cell(0))

    def test_the_status_handler_applies_a_device_status_message(self):
        """设备状态那条回调与工作台那条**各管各的**：它读的是
        `MSG_DEVICE_STATUS`，不认识 `craft_state`。

        这条同时钉住那个常量真的导进来了——只在收到消息时才求值的名字，
        离线不喂消息就永远不炸，而真机上是**每收到一次状态推就报一条**。
        """
        panel, screen, port = self.make(
            {"device_ref": REF.to_dict(), "dimension": 0})
        self.addCleanup(panel.OnDestroy)
        client = self.ready(panel)
        applied = []
        panel.status = types.SimpleNamespace(apply=applied.append)
        handler = panel._make_status_handler(client)
        envelope = types.SimpleNamespace(session_id=client.session_id)

        handler(envelope, types.SimpleNamespace(kind=MSG_DEVICE_STATUS))
        self.assertEqual(len(applied), 1)
        # 另一种消息一概不碰状态模型。
        handler(envelope, types.SimpleNamespace(kind="craft_state"))
        self.assertEqual(len(applied), 1)
        # 别人的会话也不碰。
        handler(types.SimpleNamespace(session_id="s9"),
                types.SimpleNamespace(kind=MSG_DEVICE_STATUS))
        self.assertEqual(len(applied), 1)

    def test_losing_an_idle_session_hides_online_status_and_popup(self):
        from endernet_bp.endernet_scripts.endernet.common.protocol import DeviceStatusMessage
        panel, screen, port = self.make(
            {"device_ref": REF.to_dict(), "dimension": 0})
        self.addCleanup(panel.OnDestroy)
        client = self.ready(panel)
        message = DeviceStatusMessage(
            "endernet:crafting_terminal", REF.generation, True, "在线",
            "ok", "runnable", "可运行", 1)
        client.receive(SyncEnvelope(message, client.session_id, "n1", "e1").to_dict())
        panel.OnDeviceStatusClick()
        self.assertEqual(panel.device_status_text(), "在线")
        self.assertTrue(panel.device_status_popup_visible())
        panel.OnTicking()
        self.assertTrue(panel.device_status_visible())
        client.drop_session()
        panel.OnTicking()
        self.assertIsNone(panel.status)
        self.assertFalse(panel.device_status_visible())
        self.assertFalse(panel.device_status_popup_visible())
        panel.OnTicking()

    def test_destroy_closes_both_halves_and_clears_the_handlers(self):
        panel, screen, port = self.make(
            {"device_ref": REF.to_dict(), "dimension": 0})
        self.ready(panel)
        panel.OnTicking()
        panel.OnDestroy()
        self.assertIsNone(panel.craft)
        self.assertIsNone(panel.list_view)
        # 端口注销、会话关掉——留任何一条，下一个界面都会被上一个界面的
        # 回调一起处理（`client.clear_handlers` 由 `cache.close` 调）。
        self.assertEqual(port.unlistens, 1)
        self.assertEqual(port.sent[-1]["message"]["kind"], "session_close")

    def test_destroy_closes_an_open_picker(self):
        """选择窗挂在屏幕的 `_vars` 上，不关的话下一次打开会被当成「已经开着」，
        而那一下导入就永远弹不出来。"""
        panel, screen, port = self.make(
            {"device_ref": REF.to_dict(), "dimension": 0})
        closed = []

        class Picker(object):
            """照 `SelectItemWindow.close` 的形状：**自己把自己的登记删掉**。
            控件层那边不代它删——真实现就是这么收尾的。"""

            def close(self):
                closed.append(True)
                panel._vars.pop("_endernet_select_item_window", None)

        panel._vars["_endernet_select_item_window"] = Picker()
        panel.OnDestroy()
        self.assertEqual(closed, [True])
        self.assertNotIn("_endernet_select_item_window", panel._vars)

    def test_every_binding_answers_with_a_safe_value_before_oncreate(self):
        """绑定比 `OnCreate` 早生效。**没有面板数据时不能抛**：抛出去引擎会
        每渲染一帧报一条，界面看着只是「什么都没有」。"""
        screen = Screen()
        panel = crafting_panel.CraftingPanel("main", screen, {})
        self.assertIsNone(panel.craft)
        self.assertEqual(
            screen.collection_bindings[
                (BinderStub.BF_BindInt, crafting_panel.GRID_COLLECTION,
                 crafting_panel.GRID_ITEM_ID_AUX_BINDING)](0), 0)
        self.assertFalse(
            screen.collection_bindings[
                (BinderStub.BF_BindBool, crafting_panel.OUTPUT_COLLECTION,
                 crafting_panel.OUTPUT_VISIBLE_BINDING)](0))
        self.assertEqual(
            screen.collection_bindings[
                (BinderStub.BF_BindString, crafting_panel.GRID_COLLECTION,
                 crafting_panel.GRID_ITEM_COUNT_BINDING)](0), "")


class TestRegistration(unittest.TestCase):
    """注册与定义表必须**逐字**对得上，否则右键是抛异常而不是开界面。"""

    def test_the_decorator_key_matches_the_block_mapping(self):
        """**逐字相同**是硬要求：`PushUIRequest` 拿定义表里的值在客户端查屏幕，
        查不到时 `tooldelta/ui/event_handler.py` 会抛异常——玩家看到的是
        「右键没反应」，而日志里是一条与界面无关的异常。
        """
        self.assertEqual(
            crafting_panel.CraftingPanel._screen_key,
            TERMINAL_SCREENS.get("endernet:crafting_terminal_panel"))
        self.assertIn(crafting_panel.CraftingPanel._screen_key, REGISTERED_KEYS)
        # 通用终端也在册：两条都注册了，定义表才有资格列两条。
        from endernet_bp.endernet_scripts.endernet.client.ui.terminal_panel import (
            CommonTerminalPanel,
        )
        self.assertEqual(
            CommonTerminalPanel._screen_key,
            TERMINAL_SCREENS.get("endernet:common_terminal_panel"))

    def test_register_screens_returns_both_panels(self):
        from endernet_bp.endernet_scripts.endernet.client.ui import (
            register_screens,
        )
        from endernet_bp.endernet_scripts.endernet.client.ui.terminal_panel import (
            CommonTerminalPanel,
        )
        # 11-07 起元组里还跟着四个容器类设备的代理屏。它们的键、旗标与
        # `CONTAINER_SCREENS` 的对账归 `test_device_panels.py`；这里只钉住
        # **两个终端屏在最前面且顺序不变**（调用方与既有用例都按位置读）。
        registered = register_screens()
        self.assertEqual(registered[:2],
                         (CommonTerminalPanel, crafting_panel.CraftingPanel))
        self.assertTrue(all(cls._td_is_proxy is False for cls in registered[:2]))


class TestJsonContract(unittest.TestCase):
    """界面 JSON 与上面这些常量的接线。**只验名字对得上**，控件画得对不对
    是游戏内实测项。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "endernet_rp", "ui",
                               "crafting_terminal_panel.json"),
                  encoding="utf-8") as handle:
            cls.panel = json.load(handle)
        with open(os.path.join(ROOT, "endernet_rp", "ui",
                               "common_terminal_panel.json"),
                  encoding="utf-8") as handle:
            cls.common_panel = json.load(handle)
        with open(os.path.join(ROOT, "endernet_rp", "ui",
                               "endernet_windows.json"),
                  encoding="utf-8") as handle:
            cls.windows = json.load(handle)

    def controls(self, node):
        return {key.split("@")[0]: value
                for entry in node["controls"] for key, value in entry.items()}

    def content(self):
        return self.controls(self.panel["content"])

    def wrapper_overrides(self, name):
        """薄包装覆盖了哪些 `$` 变量。"""
        wrapper = self.panel[name]
        return set(key.split("|")[0] for key in wrapper if key.startswith("$"))

    def shared_variables(self):
        """共享模板声明了哪些 `$` 变量。"""
        return set(key.split("|")[0]
                   for key in self.windows["item_can_be_selected"]
                   if key.startswith("$"))

    def test_both_slot_wrappers_override_every_variable_the_shared_template_uses(self):
        """**六个都要覆盖**：漏一个就会静默沿用 11-09 的默认值
        （`endernet_item_list` 与 `#EnderNetItemList.*`），而那种格子会去读
        另一个窗口的集合——格子在、图也有，只是永远为空。"""
        declared = self.shared_variables()
        for name in ("craft_slot@endernet_windows.item_can_be_selected",
                     "craft_output_slot@endernet_windows.item_can_be_selected",
                     "terminal_slot@endernet_windows.item_can_be_selected"):
            self.assertEqual(self.wrapper_overrides(name), declared, name)

    def test_the_craft_wrappers_point_at_the_craft_bindings(self):
        grid = self.panel["craft_slot@endernet_windows.item_can_be_selected"]
        self.assertEqual(grid["$item_collection_name"],
                         crafting_panel.GRID_COLLECTION)
        for variable, suffix in (
            ("$item_visible_binding", "grid_visible"),
            ("$item_id_aux_binding", "grid_item_id_aux"),
            ("$item_selected_binding", "grid_selected"),
            ("$item_count_binding", "grid_item_count"),
            ("$item_select_button", "grid_click"),
        ):
            self.assertEqual(variable in grid, True)
            self.assertEqual(grid[variable],
                             crafting_panel.BINDING_PREFIX + suffix)
        output = self.panel[
            "craft_output_slot@endernet_windows.item_can_be_selected"]
        self.assertEqual(output["$item_collection_name"],
                         crafting_panel.OUTPUT_COLLECTION)
        for variable, suffix in (
            ("$item_visible_binding", "output_visible"),
            ("$item_id_aux_binding", "output_item_id_aux"),
            ("$item_selected_binding", "output_selected"),
            ("$item_count_binding", "output_item_count"),
            ("$item_select_button", "take"),
        ):
            self.assertEqual(output[variable],
                             crafting_panel.BINDING_PREFIX + suffix)

    def test_the_workbench_controls_use_those_wrappers(self):
        panel = self.controls(self.content()["crafting_panel"])
        self.assertEqual(panel["crafting_grid"]["collection_name"],
                         crafting_panel.GRID_COLLECTION)
        self.assertEqual(panel["crafting_grid"]["grid_item_template"],
                         "crafting_terminal_panel_ui.craft_slot")
        self.assertEqual(panel["output_slot"]["grid_item_template"],
                         "crafting_terminal_panel_ui.craft_output_slot")
        # 输出槽是 1×1 的 grid：集合绑定只在集合上下文里解析得到，单放一个
        # 实例在面板上，引擎查不到集合，那一格永远空白。
        self.assertEqual(panel["output_slot"]["grid_dimensions"], [1, 1])
        self.assertEqual(panel["output_slot"]["collection_name"],
                         crafting_panel.OUTPUT_COLLECTION)

    def test_the_network_list_is_the_same_wiring_as_the_common_terminal(self):
        """**同一个终端，同一套绑定名**：合成终端不是另一个终端。"""
        content = self.content()
        scroll = content["terminal_items_scroll"]
        self.assertEqual(scroll["$scrolling_content"],
                         "crafting_terminal_panel_ui.terminal_items_grid")
        self.assertEqual(scroll["$scroll_background_image_control"],
                         "crafting_terminal_panel_ui.terminal_scroll_background")
        self.assertTrue(scroll["$show_background"])
        grid = self.panel["terminal_items_grid"]
        self.assertEqual(grid["collection_name"], terminal_panel.COLLECTION)
        self.assertEqual(grid["grid_item_template"],
                         "crafting_terminal_panel_ui.terminal_slot")
        binding = grid["bindings"][0]
        self.assertEqual(binding["binding_name_override"], "#maximum_grid_items")
        self.assertEqual(binding["binding_type"], "global")
        self.assertEqual(binding["binding_name"],
                         terminal_panel.BINDING_PREFIX + "count")
        # 与通用终端逐项对齐：两边走的是同一份表。
        common = self.common_panel["terminal_items_grid"]
        self.assertEqual(grid["collection_name"], common["collection_name"])
        self.assertEqual(grid["grid_dimensions"], common["grid_dimensions"])

    def test_the_terminal_wrapper_points_at_the_terminal_bindings(self):
        wrapper = self.panel["terminal_slot@endernet_windows.item_can_be_selected"]
        self.assertEqual(wrapper["$item_collection_name"],
                         terminal_panel.COLLECTION)
        for variable, suffix in (
            ("$item_visible_binding", "has_item"),
            ("$item_id_aux_binding", "item_id_aux"),
            ("$item_selected_binding", "selected"),
            ("$item_count_binding", "item_count"),
            ("$item_select_button", "select"),
        ):
            self.assertEqual(wrapper[variable],
                             terminal_panel.BINDING_PREFIX + suffix)

    def test_the_scroll_background_is_an_invisible_button_wired_to_quick_store(self):
        """上方列表的空白处那块**隐形按钮**：只覆盖 `$pressed_button_name`
        是不够的——模板的 `button_mappings` 是空的，那一下点击永远不会被接到
        名字上（2026-10-01 撞出来的）。"""
        button = self.panel["terminal_scroll_background@"
                            "endernet_ui_common.simple_invisible_button"]
        self.assertEqual(button["$pressed_button_name"],
                         terminal_panel.QUICK_STORE_BINDING)
        self.assertEqual(button["button_mappings"][0]["to_button_id"],
                         "$pressed_button_name")

    def test_the_search_box_sort_buttons_and_mask_point_at_the_right_bindings(self):
        content = self.content()
        self.assertEqual(content["item_search_editor"]["$text_box_name"],
                         terminal_panel.SEARCH_BINDING)
        for button, binding in (
                ("sort_method_btn", terminal_panel.SORT_METHOD_BINDING),
                ("sort_order_btn", terminal_panel.SORT_ORDER_BINDING)):
            self.assertEqual(content[button]["$pressed_button_name"], binding)
            self.assertEqual(
                content[button]["button_mappings"][0]["to_button_id"],
                "$pressed_button_name")
        mask = content["unavailable_mask"]
        self.assertEqual(mask["bindings"][0]["binding_name"],
                         terminal_panel.UNAVAILABLE_BINDING)
        self.assertEqual(mask["bindings"][0]["binding_name_override"], "#visible")
        # 遮罩里那层变暗图必须画得出来：没有 anchor 时运行时尺寸解析成 0，
        # `SetVisible(True)` 也什么都不画，整块遮罩是空的（真机实测）。
        self.assertEqual(mask["controls"][0]["mask_img"]["anchor_from"],
                         "top_left")

    def test_the_two_action_buttons_are_wired(self):
        content = self.content()
        for button, binding in (("load_recipe_btn", crafting_panel.IMPORT_BINDING),
                                ("clear_items_btn", crafting_panel.CLEAR_BINDING)):
            self.assertEqual(content[button]["$pressed_button_name"], binding)
            self.assertEqual(
                content[button]["button_mappings"][0]["to_button_id"],
                "$pressed_button_name")
        # 「摆满」本次不处理：**不接任何绑定**，它的动作由 User 定（UI 文档 1 节）。
        self.assertEqual(content["put_items_btn"]["$pressed_button_name"],
                         "%netease_editor_template_namespace.click")
        self.assertEqual(content["put_items_btn"]["button_mappings"], [])

    def test_every_real_pressed_name_in_this_file_has_a_button_mapping(self):
        """只在本文件里再扫一遍：**新加的那两个名字**是本轮引入的，出问题时
        第一个要看它们（全仓库那一遍在 `test_terminal_panel` 里）。"""
        checked = [0]
        offenders = []

        def walk(node, trail):
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
                        offenders.append("%s -> %s" % (trail, pressed))
                for key, value in node.items():
                    walk(value, trail + "/" + key)
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, trail + "/" + str(index))

        walk(self.panel, "")
        self.assertGreater(checked[0], 0, "一个真实按下名都没扫到，扫法已失效")
        self.assertEqual(offenders, [])

    def test_the_recipe_window_lists_into_its_own_container(self):
        """候选逐条铺进 `recipe_list`，而不是原来那个占位的 `recipe_section`：
        脚本要往内容控件里 `AddElement`，而内容控件就是 `$scrolling_content`
        指名的那个。"""
        window = self.controls(self.panel["load_recipe_window"])
        scroll = window["scroll_view"]
        self.assertEqual(scroll["$scrolling_content"],
                         "crafting_terminal_panel_ui.recipe_list")
        self.assertEqual(self.panel["recipe_list"]["type"], "panel")
        # `recipe_section` 的定义还在（脚本按名字实例化它）。
        self.assertIn("recipe_section", self.panel)

    def test_the_flying_item_is_anchored_where_the_script_measures_it(self):
        flying = self.content()["flying_item"]
        self.assertEqual(flying["anchor_from"], "top_left")
        self.assertEqual(flying["anchor_to"], "top_left")
        self.assertFalse(flying["visible"])


class TestSlotLayoutPaths(unittest.TestCase):
    """滚动控件那三条候选后缀：合成终端的候选窗口与终端列表**共用同一套**。"""

    def setUp(self):
        self.screen = Screen()

    def test_the_common_terminal_paths_are_the_same_three_candidates(self):
        self.assertEqual(
            slot_layout.SCROLL_VIEW_PATHS,
            tuple(slot_layout.SCROLL_PATH + suffix
                  for suffix in slot_layout.SCROLL_VIEW_SUFFIXES))

    def test_scroll_view_at_finds_the_touch_branch_too(self):
        content = ControlStub()
        path = crafting_panel.RECIPE_SCROLL_PATH + "/scroll_touch/scroll_view"
        self.screen.controls[path] = ScrollStub(content)
        self.assertIs(
            slot_layout.scroll_content_at(
                self.screen, crafting_panel.RECIPE_SCROLL_PATH), content)

    def test_scroll_content_at_returns_none_for_an_unknown_panel(self):
        self.assertIsNone(
            slot_layout.scroll_content_at(self.screen, "/nope/scroll_view"))


class TestCellNameFromTheWire(unittest.TestCase):
    """九宫格那一行的 `name` **过线回来是字节串**。

    2026-10-02 在样板终端上真机撞到同一个写法：只认 `unicode` 时
    `isinstance(name, unicode)` 判假、名字被换成空串，`id_aux` 恒为 0——
    格子里有原料、画面上没有图标。11-04 这两行是同一份代码，但它从没上过机，
    所以一直没暴露；离线夹具喂的是 `ResourceKey.name`（unicode），照不出来。
    """

    def row(self, name):
        return {"canonical": "item|minecraft:dirt|0|...", "count": 1,
                "name": name, "aux": 0}

    def test_a_byte_string_name_is_decoded_not_dropped(self):
        cell = crafting_panel._cell_of(self.row(b"minecraft:dirt"))
        self.assertEqual(u"minecraft:dirt", cell["name"])

    def test_a_unicode_name_still_works(self):
        cell = crafting_panel._cell_of(self.row(u"minecraft:dirt"))
        self.assertEqual(u"minecraft:dirt", cell["name"])

    def test_a_non_text_name_is_blank_rather_than_guessed(self):
        self.assertEqual(u"", crafting_panel._cell_of(self.row(7))["name"])
        self.assertEqual(u"", crafting_panel._cell_of(self.row(None))["name"])


if __name__ == "__main__":
    unittest.main()
