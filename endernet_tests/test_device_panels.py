# coding=utf-8
"""容器类设备屏（11-07）的客户端半边：代理注册、身份认领、状态栏接线。

守的是**引擎开的屏**与**脚本挂上去的代理**之间那层缝。缝里的错在游戏里几乎
都看不出来——屏挂上了、格子照常渲染，只是脚本那一半永远不动：

- 屏幕名与 `CONTAINER_SCREENS` 对不上——服务端推的身份永远无人认领，
  状态栏一直是空的；
- 身份 latch 取走不消费——下一次开屏认到一台可能已经被拆掉的设备；
- 绑定写在方法里而不是类体里——引擎按名取不到回调，界面空白而没有任何报错；
- `keepalive` 不发——只读屏一条命令都不发，会话 20 秒后被回收，状态栏
  悄悄失效（与 D-08 同一根因）；
- 事件名两侧拼得不一样——总线照常派发、就是没人收到，同样没有任何报错。

**离线测不了**：代理的 `OnCreate`/`OnTick`/`OnDestroy` 到底会不会被引擎调用
（`netease_container_screens.md` 的 Q6）、`ServerBlockUseEvent` 对容器方块是否
派发、身份推送在真机上到不到。这些全部是游戏内实测项，见
`plans/tasks/11-交付说明.md` 的 11-07 一节。
"""

import json
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import testsupport  # noqa: E402  设置 sys.path

PREFIX = "endernet_bp.endernet_scripts."


class BinderStub(object):
    """与 `test_terminal_panel` / `test_crafting_panel` 同一份标记值。

    三个文件各装一次替身，而三个面板模块都在导入期把 `Binder` 绑成了自己
    见到的那一份。取值完全相同，因此「谁先导入」不影响任何断言。
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


class ScreenNodeStub(object):
    """引擎 `ScreenNode` 的替身。**用得上的是它没有的东西**：引擎取值时拿回调
    的名字在 `_Base` 上 `getattr`，名字不在类上就是 `AttributeError`。"""


class FakeScreen(object):
    """`ScreenBase` 眼里的「屏实例」：它只要求两本登记表。"""

    def __init__(self):
        self.bindings = {}
        self.collection_bindings = {}


class ItemStub(object):
    """`tooldelta.define.item.Item` 的替身（本文件只要求它导得进来——
    `network_client` 经 `crafting_panel` 那条链会摸到它）。"""


#: 导入期登记过的 `(key, is_proxy)`。
REGISTERED = []


def _registrable(key, is_proxy=False):
    def decorate(cls):
        cls._screen_key = key
        cls._td_is_proxy = is_proxy
        REGISTERED.append((key, is_proxy))
        return cls
    return decorate


def td_screen_attrs(cls):
    """`ToolDeltaScreen._get_tdscreen_bindings` 的等价实现（与
    `test_terminal_panel.td_screen_attrs` 同一份名字规则）。"""
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
    """`tooldelta.ui.ToolDeltaScreen` 的替身，**代理与自有屏两用**。

    比 `test_terminal_panel` 那一份多一样：设备屏的 `__init__` 在
    `_register_as_proxy` 里只收到 `(screenName, screenNode)` 两个参数
    （`general_screen.py` 就是这么调 `cls(...)` 的），而这里按同一个签名接。

    **是超集**：另两个测试模块的屏也共用这份替身（三家都往同一个
    `sys.modules['...tooldelta.ui']` 里写），少一样就会在导入期炸。
    """

    def __init__(self, screen_name, screen_instance, params=None):
        self.screen = screen_instance
        self._init_params = params if params is not None else {}
        self._activated = False
        self.bindings = {}
        self.collection_bindings = {}
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


def _install_stubs():
    """把 `tooldelta.ui` / `tooldelta.define.item` 换成替身。

    **必须赶在导入 `device_panels` 之前**：真 `tooldelta/__init__.py` 在模块级
    import 引擎，脱离游戏一导入就炸。
    """
    for suffix, attrs in (
        ("tooldelta.ui", {
            "Binder": BinderStub,
            "RegistToolDeltaScreen": _registrable,
            "ToolDeltaScreen": ScreenBase,
        }),
        ("tooldelta.define.item", {"Item": ItemStub}),
        # `register_screens()` 经 `crafting_panel` → `select_item_window` 摸到
        # 它；不补这一份，`register_screens()` 会把整个 vendored 包拖进来
        # （那条链最终炸在 `mod.client.*` 的循环导入上）。
        ("tooldelta.api.client.player",
         {"GetLocalPlayerHotbarAndInvItems": lambda *args, **kwargs: []}),
    ):
        module = sys.modules.get(PREFIX + suffix)
        if module is None:
            module = types.ModuleType(PREFIX + suffix)
            sys.modules[module.__name__] = module
        module.__dict__.update(attrs)


_install_stubs()

from endernet_bp.endernet_scripts.endernet.client import (  # noqa: E402
    device_identity as client_identity,
)
from endernet_bp.endernet_scripts.endernet.client.network_client import (  # noqa: E402
    NetworkClient,
)
from endernet_bp.endernet_scripts.endernet.client.ui import (  # noqa: E402
    device_panels, proxy_panel,
)
from endernet_bp.endernet_scripts.endernet.client.ui.widgets import (  # noqa: E402
    device_status, device_status_bindings,
)
from endernet_bp.endernet_scripts.endernet.common import device_identity  # noqa: E402
from endernet_bp.endernet_scripts.endernet.common import protocol  # noqa: E402
from endernet_bp.endernet_scripts.endernet.common.definitions import (  # noqa: E402
    CONTAINER_SCREENS, TERMINAL_SCREENS,
)
from endernet_bp.endernet_scripts.endernet.common.identity import (  # noqa: E402
    DeviceRef, NodeKey,
)

RP_UI = os.path.join(ROOT, "endernet_rp", "ui")


def _load_ui(name):
    with open(os.path.join(RP_UI, name), encoding="utf-8") as handle:
        return json.load(handle)


def ui_file_of(device_id):
    """方块 id → 资源包里的界面文件名。

    **不能用 namespace 拼**：namespace 是 `bus_item_output_io_ui`，而文件名是
    `bus_item_output_io.json`（`_ui` 后缀是资源那侧加的）。方块 id 冒号后半段
    才是文件名——这一条与 `gen_blocks` 的产物命名规则同源。
    """
    return device_id.split(":", 1)[1] + ".json"


def _declared_containers():
    """`gen_blocks.CONTAINERS` 的读数（与 `test_assets.declared_containers` 同一
    手法：从生成器派生，不另抄一份清单）。"""
    scripts_dir = os.path.join(ROOT, "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import gen_blocks

    return gen_blocks.CONTAINERS


#: 声明了容器组件、但**今天没有脚本屏**的设备。**具名而非空集**：多一台容器
#: 方块时要当场变红，而不是被一个「反向差异为空」的断言悄悄放过去。
PANEL_EXEMPTIONS = {
    "endernet:bus_item_io": "界面没做完，User 2026-09-29 判定不作样本",
    # `endernet:storage_driver` **2026-10-03 已移出本清单**：User 裁定介质管理
    # 暂不需要界面，于是给它挂了一张**空屏**（`StorageDriverPanel`，只有状态栏
    # 与详情弹窗）。它的配置面仍然没有——但「有没有屏幕类」这一问已经有了答案。
    "endernet:memory_slot": "内存槽界面未交接（plans/ui/memory_slot.md 仍是模板）",
    "endernet:bus_fluid_input_io": "流体口，R07 占位、真实适配属 13-03，界面刻意不画集合",
    "endernet:bus_fluid_output_io": "流体口，同上一行",
}


class FakeEvents(object):
    """`tooldelta.events` 的替身：只要那一个 `CustomS2CEvent` 基类。

    **`unmarshal` 必须有**：`testsupport.FakeEventsModule` 只给同步通道用
    （它有 `send` 没有 `unmarshal`），而身份事件两侧都要能走。
    """

    def __init__(self):
        outer = self
        self.sent = []

        class CustomS2CEvent(object):
            name = "fake_s2c"

            def __init__(self, payload=None):
                self.payload = payload if isinstance(payload, dict) else {}

            def marshal(self):
                return self.payload

            @classmethod
            def unmarshal(cls, data):
                return cls(data)

            def send(self, target_id):
                outer.sent.append((target_id, self.payload))

        self.CustomS2CEvent = CustomS2CEvent


class TestRegistration(unittest.TestCase):
    def test_every_panel_registers_under_the_definition_table_key(self):
        """注册键必须与 `CONTAINER_SCREENS` 的值逐字相同。

        这是两侧唯一的握手串：服务端按方块查表推身份，客户端按自己的
        `_screen_key` 认领。对不上的症状是「屏挂上了，但永远认不到身份」，
        界面上看不出任何异常。
        """
        for device_id, panel_cls in device_panels.PROXY_PANELS:
            self.assertIn(device_id, CONTAINER_SCREENS)
            self.assertEqual(panel_cls._screen_key, CONTAINER_SCREENS[device_id])
            self.assertIn((panel_cls._screen_key, True), REGISTERED)

    def test_device_panels_are_registered_as_proxies(self):
        for _, panel_cls in device_panels.PROXY_PANELS:
            self.assertTrue(getattr(panel_cls, "_td_is_proxy", None))

    def test_terminal_screens_are_not_proxies(self):
        """两条路不能混：终端屏是自有屏（`PushUI` 推），设备屏是代理（引擎开）。
        旗标反了的话，终端右键会去查一张 `_screen_cls` 为 `None` 的屏——
        直接抛异常。"""
        from endernet_bp.endernet_scripts.endernet.client.ui import (
            register_screens,
        )
        self.assertTrue(all(not getattr(cls, "_td_is_proxy", False)
                            for cls in register_screens()[:2]))

    def test_register_screens_includes_every_device_panel(self):
        from endernet_bp.endernet_scripts.endernet.client.ui import (
            register_screens,
        )
        registered = register_screens()
        for _, panel_cls in device_panels.PROXY_PANELS:
            self.assertIn(panel_cls, registered)


def _settings_controls(device_id):
    """界面 JSON 里 `settings_window` 模板的控件名集合（含嵌套路径）。

    **不去重、不归一**：这里要回答的是「Python 里写的那个名字在 JSON 里
    存不存在」，因此嵌套那一条按 `父/子` 的路径形式收进来，与
    `GetElement` 收路径的用法一致（`filter_mode_btn/icon` 就是这种）。
    """
    data = _load_ui(ui_file_of(device_id))
    template = data.get("settings_window")
    if template is None:
        return None
    found = set()

    def walk(node, prefix):
        for entry in node.get("controls") or ():
            for key, value in entry.items():
                name = key.split("@")[0]
                path = prefix + name
                found.add(path)
                if isinstance(value, dict):
                    walk(value, path + "/")

    walk(template, "")
    return found


class TestPortConfigPanels(unittest.TestCase):
    """两台物品口的配置接线（11-07）。

    守的是**「哪台有哪些控件」这件事有两份描述**：Python 的类属性与界面的
    JSON。两处对不上时界面不会报错——只是那一下点了没反应，或者开关拨了
    没人读。因此这里把它们对起来。
    """

    OUTPUT = device_panels.BusItemOutputPanel
    INPUT = device_panels.BusItemInputPanel

    def test_each_port_declares_only_the_controls_its_interface_has(self):
        # 取货口有缓存开关、没有过滤模式；存货口正好相反。
        self.assertIsNotNone(self.OUTPUT.CACHE_SWITCH)
        self.assertIsNone(self.OUTPUT.MODE_BUTTON)
        self.assertIsNone(self.OUTPUT.MODE_ICON)
        self.assertIsNone(self.OUTPUT.MODE_TEXT)
        self.assertIsNone(self.INPUT.CACHE_SWITCH)
        self.assertIsNotNone(self.INPUT.MODE_BUTTON)
        self.assertIsNotNone(self.INPUT.MODE_ICON)
        self.assertIsNotNone(self.INPUT.MODE_TEXT)

    def test_each_port_has_exactly_one_way_to_open_the_filter_list(self):
        """两条路（主面板／设置窗）**各用其中一条**。

        两条都填会让 `_ensure_controls` 与 `_wire_settings` 各接一次回调、
        后接的那次盖掉前面——表现是「点其中一个按钮没反应」。
        """
        self.assertIsNotNone(self.OUTPUT.FILTER_BUTTON_MAIN)
        self.assertIsNone(self.OUTPUT.FILTER_BUTTON_SETTINGS)
        self.assertIsNone(self.INPUT.FILTER_BUTTON_MAIN)
        self.assertIsNotNone(self.INPUT.FILTER_BUTTON_SETTINGS)

    def test_only_the_output_port_draws_a_summary(self):
        """存货口的 `content` 里没有 `item_set_panel`——那是界面的差异，
        不是漏接。填 `True` 只会让每次开屏多记一条 missing。"""
        self.assertTrue(self.OUTPUT.HAS_SUMMARY)
        self.assertFalse(self.INPUT.HAS_SUMMARY)

    def test_the_settings_definition_names_this_panel_s_own_json(self):
        for device_id, panel_cls in (
            ("endernet:bus_item_output_io", self.OUTPUT),
            ("endernet:bus_item_input_io", self.INPUT),
        ):
            self.assertEqual(
                panel_cls.SETTINGS_DEFINITION,
                ui_file_of(device_id)[:-5] + "_ui.settings_window",
            )
        # 取货口与存货口各是各的文件，串了就会去开另一台的设置窗。
        self.assertNotEqual(self.OUTPUT.SETTINGS_DEFINITION,
                            self.INPUT.SETTINGS_DEFINITION)

    def test_every_declared_control_name_exists_in_the_interface_json(self):
        """**Python 里的控件名必须能在 JSON 里找到。**

        找不到时 `_settings_control` 会记一条 missing 并跳过——界面不报错，
        只是那个开关永远画不出权威值。这条用例把那份沉默变成一次红灯。
        """
        for device_id, panel_cls in (
            ("endernet:bus_item_output_io", self.OUTPUT),
            ("endernet:bus_item_input_io", self.INPUT),
        ):
            controls = _settings_controls(device_id)
            self.assertIsNotNone(controls, "%s 没有 settings_window 模板" % (device_id,))
            declared = [
                ("SETTINGS_CACHE_SWITCH", panel_cls.CACHE_SWITCH),
                ("MODE_BUTTON", panel_cls.MODE_BUTTON),
                ("MODE_ICON", panel_cls.MODE_ICON),
                ("MODE_TEXT", panel_cls.MODE_TEXT),
                ("FILTER_BUTTON_SETTINGS", panel_cls.FILTER_BUTTON_SETTINGS),
            ]
            for label, name in declared:
                if name is None:
                    continue
                self.assertIn(
                    name, controls,
                    "%s 的 %s=%r 在界面 JSON 里找不到" % (device_id, label, name))

    def test_the_settings_window_is_not_mounted_in_the_content_tree(self):
        """它是一张**自由模板**，由脚本按需 `AddElement` 出来。

        本轮把它挂进 `content` 没有意义：那会让它每次开屏都在树上，而
        显隐还得另接一条绑定——命令式那边一个字节都不用改。
        """
        for device_id in ("endernet:bus_item_output_io",
                          "endernet:bus_item_input_io"):
            data = _load_ui(ui_file_of(device_id))
            content = data["content"]
            keys = [key for entry in content["controls"] for key in entry]
            self.assertFalse(
                [key for key in keys if "settings_window" in key],
                "%s 的 content 里不该挂 settings_window" % (device_id,))


class TestPortConfigPanelLifecycle(unittest.TestCase):
    """屏的模型装配与「没有控件也不炸」。"""

    def setUp(self):
        self.patcher = mock.patch.object(proxy_panel, "NetworkClient", _FakeClient)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        client_identity.latch().clear()
        self.addCleanup(client_identity.latch().clear)
        self.ref = DeviceRef(NodeKey(0, 4, 5, 6), 2)

    def panel(self, cls=None):
        cls = cls or device_panels.BusItemOutputPanel
        return cls("probe.main", FakeScreen())

    def offer(self, cls):
        return client_identity.latch().offer(
            device_identity.identity_payload(cls._screen_key, self.ref, 0))

    def test_the_config_model_is_built_on_adopt_and_dropped_on_release(self):
        self.offer(device_panels.BusItemOutputPanel)
        panel = self.panel()
        panel.OnCreate()
        self.assertIsNotNone(panel.port_config)
        self.assertIsNone(panel.port_config.device_id)   # 还没收到推送
        panel.OnDestroy()
        self.assertIsNone(panel.port_config)

    def test_a_tick_without_controls_degrades_instead_of_raising(self):
        """控件取不到（路径解析失败、这台设备的界面里没有那一格）时，
        `OnTicking` **不许抛**——引擎每帧调用它，抛出会刷屏并跳过本帧剩下的事。

        取不到的名字要被记下来（`missing`），否则「开关永远画不出权威值」
        在诊断里也看不见。
        """
        self.offer(device_panels.BusItemOutputPanel)
        panel = self.panel()
        panel.OnCreate()
        panel.client.establish()
        panel.OnTicking()
        panel.OnTicking()
        self.assertIsInstance(panel.diagnostics()["port_failures"], dict)
        # FakeScreen 没有控件树，因此取不到主面板那几个。
        self.assertTrue(panel.diagnostics()["port_missing"])

    def test_a_config_push_reaches_the_model_and_a_stale_one_does_not(self):
        self.offer(device_panels.BusItemOutputPanel)
        panel = self.panel()
        panel.OnCreate()
        panel.client.establish()
        session = panel.client.session_id
        panel.client.deliver(session, protocol.PortConfigMessage(
            "endernet:bus_item_output_io", self.ref.generation, "output",
            {"enabled": True, "direction": "both", "filter_mode": "whitelist",
             "filter": [], "supply_slots": [], "limit": 0, "cache_enabled": False},
            9, filter_modes=["whitelist"]))
        self.assertFalse(panel.port_config.cache_enabled())
        # 旧代次的推送一律丢掉：拆掉又在原地放一台时，界面不能画上一台的配置。
        panel.client.deliver(session, protocol.PortConfigMessage(
            "endernet:bus_item_output_io", self.ref.generation + 1, "output",
            {"enabled": True, "direction": "both", "filter_mode": "whitelist",
             "filter": [], "supply_slots": [], "limit": 0, "cache_enabled": True},
            9, filter_modes=["whitelist"]))
        self.assertFalse(panel.port_config.cache_enabled())
        self.assertEqual(panel.port_config.stats["stale"], 1)


class TestItemPickerSearch(unittest.TestCase):
    def test_port_search_binding_filters_localized_item_names(self):
        from endernet_bp.endernet_scripts.endernet.client.ui import select_item_window
        from test_select_item_window import ItemStub as SearchItem

        for panel_cls in (device_panels.BusItemInputPanel, device_panels.BusItemOutputPanel):
            with self.subTest(panel=panel_cls.__name__):
                engine = FakeScreen()
                panel = panel_cls("probe.main", engine)
                panel._activated = True
                panel._vars = {}
                # 仅构造选择器数据模型；事件仍通过实际设备屏生成的绑定入口派发。
                picker = select_item_window.SelectItemWindow.__new__(
                    select_item_window.SelectItemWindow)
                picker.screen = panel
                picker.closed = False
                picker.source = "all"
                picker.query = ""
                picker.search_names = {}
                picker.visible_ids = []
                picker.catalog = types.SimpleNamespace(
                    item_ids=["test:item_%02d" % i for i in range(55)] + ["minecraft:stone"])
                panel._vars[picker.WINDOW_ATTRIBUTE] = picker
                key = (BinderStub.BF_EditChanged, picker.SEARCH_BINDING)
                self.assertIn(key, engine.bindings)
                callback = engine.bindings[key]
                with mock.patch.object(select_item_window, "Item", SearchItem):
                    picker.refresh()
                    self.assertEqual(len(picker.visible_ids), 40)
                    # 名称命中，ID 不含关键字；目标位于最初 40 项之外。
                    for text in ("  石头  ", "石头".encode("utf-8")):
                        callback({"Text": text})
                        self.assertEqual(picker.visible_ids, ["minecraft:stone"])
                        self.assertTrue(picker.has_item(0))
                        self.assertFalse(picker.has_item(1))
                        self.assertEqual(picker.get_item_id_aux(0), 1234)
                    callback({"Text": "不存在"})
                    self.assertEqual(picker.visible_ids, [])
                    callback({"Text": ""})
                    self.assertEqual(len(picker.visible_ids), 40)
                    del panel._vars[picker.WINDOW_ATTRIBUTE]
                    callback({"Text": "石头"})
                    self.assertEqual(picker.query, "")


class TestContainerScreenTable(unittest.TestCase):
    def test_keys_are_declared_containers_with_matching_screen_names(self):
        """键必须在 `gen_blocks.CONTAINERS` 里，且屏幕名逐字相同。

        表格与生成器各写一份屏幕名是**唯一的第二事实来源**，而它俩一旦漂移，
        方块声明的容器屏幕与脚本挂的代理屏就是两张不同的屏。
        """
        declared = _declared_containers()
        for device_id, screen_name in CONTAINER_SCREENS.items():
            self.assertIn(device_id, declared)
            self.assertEqual(declared[device_id][0], screen_name)

    def test_terminal_and_container_tables_are_disjoint(self):
        self.assertFalse(set(CONTAINER_SCREENS) & set(TERMINAL_SCREENS))

    def test_panel_list_matches_the_table(self):
        self.assertEqual(
            set(device_id for device_id, _ in device_panels.PROXY_PANELS),
            set(CONTAINER_SCREENS),
        )

    def test_container_blocks_without_a_panel_are_named(self):
        """反向差异必须是**具名的**那一份，不是「恰好为空」。

        今天没有屏的容器方块各有各的理由（未交接 / 设备本身没做完 / 流体口
        刻意隐藏）。写成具名集合，多一台容器方块时这里当场变红；写成空集的话，
        同样的改动会被安静地放过去。
        """
        missing = set(_declared_containers()) - set(CONTAINER_SCREENS)
        self.assertEqual(missing, set(PANEL_EXEMPTIONS))


class TestStatusBindings(unittest.TestCase):
    """十一条状态绑定必须**在类体里**、且 `status is None` 时给安全值。"""

    def panel(self, panel_cls=None):
        panel_cls = panel_cls or device_panels.BusItemOutputPanel
        return panel_cls("probe.main", FakeScreen())

    def declared(self):
        """`_Base` 上真正带标记的绑定集合：`{(旗标, 绑定名), ...}`。"""
        return set(
            (func.binding_flags, func.binding_name)
            for func in td_screen_attrs(device_panels.BusItemOutputPanel).values()
        )

    def test_the_ten_always_registered_bindings_are_on_the_class_body(self):
        declared = self.declared()
        expected = (
            (device_status.TEXT_BINDING, BinderStub.BF_BindString),
            (device_status.VISIBLE_BINDING, BinderStub.BF_BindBool),
            (device_status.ICON_BINDING, BinderStub.BF_BindString),
            (device_status.MASTER_TEXT_BINDING, BinderStub.BF_BindString),
            (device_status.MASTER_ICON_BINDING, BinderStub.BF_BindString),
            (device_status.NET_TEXT_BINDING, BinderStub.BF_BindString),
            (device_status.NET_ICON_BINDING, BinderStub.BF_BindString),
            (device_status.POPUP_VISIBLE_BINDING, BinderStub.BF_BindBool),
            (device_status.OPEN_BINDING, BinderStub.BF_ButtonClickUp),
            (device_status.CLOSE_BINDING, BinderStub.BF_ButtonClickUp),
        )
        for name, flag in expected:
            self.assertIn((flag, name), declared, name)

    def test_the_color_binding_follows_the_engine_flag(self):
        """`BF_BindColor` 取不到时**不注册这一条**（颜色退回模板静态值），
        取得到时必须注册——11-13 已在真机上确认本引擎有这个旗标。

        **两支都要留**：11-06 起离线替身也带上了这个旗标（工厂终端的三色
        背景要用它），因此本地走的是下面那一支；而上面那一支押的是
        「换一个没有这个旗标的引擎时，客户端半边照样起得来」，那是
        `device_status_bindings.COLOR_BINDING_FLAG` 存在的全部理由。
        """
        declared = self.declared()
        if device_status_bindings.COLOR_BINDING_FLAG is None:
            self.assertFalse(
                any(name == device_status.COLOR_BINDING for _, name in declared))
        else:
            self.assertIn((device_status_bindings.COLOR_BINDING_FLAG,
                           device_status.COLOR_BINDING), declared)

    def test_safe_values_before_a_session_exists(self):
        """绑定比 `OnCreate` 早生效：没有状态时**不显示**，而不是先糊一个
        红色的「离线」——那是方向错误的结论，玩家会去查一个不存在的故障。"""
        panel = self.panel()
        self.assertIsNone(panel.status)
        self.assertEqual(panel.device_status_text(), "")
        self.assertFalse(panel.device_status_visible())
        self.assertEqual(panel.device_status_icon(), device_status.ICON_OFFLINE)
        self.assertEqual(panel.device_status_master_text(),
                         device_status.MASTER_ROW_MISSING)
        self.assertEqual(panel.device_status_net_text(),
                         device_status.NET_ROW_UNKNOWN)
        self.assertFalse(panel.device_status_popup_visible())
        # 点击类回调在没会话时必须安静：抛出去会一路回到引擎。
        self.assertIsNone(panel.OnDeviceStatusClick())
        self.assertIsNone(panel.OnDeviceStatusClose())


class TestIdentityLatch(unittest.TestCase):
    def setUp(self):
        self.ref = DeviceRef(NodeKey(0, 1, 2, 3), 7)
        self.latch = client_identity.DeviceIdentityLatch()

    def payload(self, key="bus_item_output_io_ui.main", ref=None):
        return device_identity.identity_payload(
            key, ref if ref is not None else self.ref, 0)

    def test_offer_then_take_consumes_the_entry(self):
        self.assertTrue(self.latch.offer(self.payload()))
        self.assertEqual(self.latch.take("bus_item_output_io_ui.main"),
                         (self.ref, 0))
        # **取出即消费**：留着它，下一次开屏会认到一台可能已被拆掉的设备。
        self.assertIsNone(self.latch.take("bus_item_output_io_ui.main"))
        # **空手而归不计数**：屏每帧来问一次，数它只会让诊断里出现一个
        # 「等了多久（帧）」的数，而不是「有几条身份没人接」。
        self.assertEqual(self.latch.stats["taken"], 1)

    def test_later_offer_overwrites_the_earlier_one(self):
        other = DeviceRef(NodeKey(0, 9, 9, 9), 3)
        self.latch.offer(self.payload())
        self.latch.offer(self.payload(ref=other))
        self.assertEqual(self.latch.take("bus_item_output_io_ui.main"), (other, 0))
        self.assertEqual(self.latch.stats["accepted"], 2)

    def test_unknown_key_takes_nothing(self):
        self.latch.offer(self.payload())
        self.assertIsNone(self.latch.take("bus_template_io_ui.main"))
        self.assertEqual(self.latch.pending(), ("bus_item_output_io_ui.main",))

    def test_malformed_payloads_are_counted_not_raised(self):
        broken = (
            {},
            None,
            [],
            {"key": "x", "dimension": 0},
            {"key": "", "device_ref": self.ref.to_dict(), "dimension": 0},
            {"key": "x", "device_ref": {"node_key": {}, "generation": 1},
             "dimension": 0},
            {"key": "x", "device_ref": self.ref.to_dict(), "dimension": -1},
            {"key": "x", "device_ref": self.ref.to_dict(), "dimension": True},
            {"key": "x", "device_ref": self.ref.to_dict(), "dimension": 0,
             "extra": 1},
        )
        for item in broken:
            self.assertFalse(self.latch.offer(item), repr(item))
        self.assertEqual(self.latch.stats["rejected"], len(broken))
        self.assertEqual(self.latch.pending(), ())

    def test_receive_never_raises_on_a_broken_event(self):
        """总线回调接到的可能是任何东西；抛出去会连累同一批里的其它消息。"""
        service = client_identity.DeviceIdentityClient(
            _NullBus(), FakeEvents(), latch=self.latch)

        class _Whatever(object):
            payload = "not-a-dict"

        self.assertFalse(service.receive(_Whatever()))
        self.assertEqual(self.latch.stats["rejected"], 1)

    def test_clear_and_diagnostics(self):
        self.latch.offer(self.payload())
        self.assertEqual(self.latch.pending(), ("bus_item_output_io_ui.main",))
        self.latch.clear()
        self.assertEqual(self.latch.pending(), ())
        report = self.latch.diagnostics()
        self.assertEqual(report["accepted"], 1)
        self.assertIn("pending", report)


class _NullBus(object):
    """只记注册与注销，不需要真的引擎总线。"""

    def __init__(self):
        self.handles = []

    def ListenEvent(self, event_cls, priority=0):
        def decorate(func):
            self.handles.append((event_cls, func, priority))
            return func
        return decorate

    def RemoveEventListener(self, event_cls, func, priority=0):
        self.handles.remove((event_cls, func, priority))


class TestIdentityChannel(unittest.TestCase):
    """两侧的事件类必须同名，载荷必须能原样往返。

    事件名拼错在真机上只表现为「什么都不发生」——没有回包、没有报错、
    屏就是认不到身份。
    """

    def test_both_sides_use_the_same_event_name(self):
        events = FakeEvents()
        (client_cls,) = device_identity.build_event_classes(events)
        self.assertEqual(client_cls.name, protocol.DEVICE_IDENTITY_CHANNEL_S2C)

    def test_payload_round_trips_through_the_wire(self):
        ref = DeviceRef(NodeKey(2, 10, 20, 30), 5)
        events = FakeEvents()
        payload = device_identity.identity_payload("some_ui.main", ref, 2)
        (event_cls,) = device_identity.build_event_classes(events)
        event = event_cls(payload)
        self.assertEqual(event.marshal(), payload)
        restored = event_cls.unmarshal(event.marshal())
        self.assertEqual(device_identity.parse_identity(restored.payload),
                         ("some_ui.main", ref, 2))

    def test_identity_payload_rejects_a_non_device_ref(self):
        with self.assertRaises(TypeError):
            device_identity.identity_payload("x", "not-a-ref", 0)

    def test_shared_classes_are_one_object_per_events_module(self):
        """同一份 `events` 模块只建一份类。

        每新建一个类就是引擎侧的一次新注册，而同名处理器会被后者覆盖，
        结果是**先注册的那个收不到任何消息**。
        """
        events = FakeEvents()
        first = client_identity.shared_event_classes(events)
        second = client_identity.shared_event_classes(events)
        self.assertIs(first, second)

    def test_the_listener_registers_once_and_unregisters_cleanly(self):
        bus = _NullBus()
        service = client_identity.DeviceIdentityClient(bus, FakeEvents())
        self.assertEqual(service.start(), 1)
        self.assertTrue(service.listening())
        # 重复注册被拒：再挂一次就是同一条消息被处理两遍。
        self.assertEqual(service.start(), 0)
        self.assertEqual(len(bus.handles), 1)
        self.assertEqual(service.stop(), 1)
        self.assertFalse(service.listening())
        self.assertEqual(bus.handles, [])


class _FakePort(object):
    def __init__(self):
        self.listeners = []
        self.unlistens = 0

    def listen(self, handler, priority=0):
        self.listeners.append(handler)
        return 1

    def unlisten(self):
        self.unlistens += 1
        return len(self.listeners)

    def send(self, payload):
        return True


class _FakeClient(object):
    """`NetworkClient` 的替身：只回答屏真正问的那几个问题。

    `opens` 记下每次开会话的 `(device_ref, dimension)`；`establish()`
    模拟服务端回 `session_ready`。
    """

    def __init__(self):
        self.port = _FakePort()
        self.session_id = ""
        self.handlers = []
        self.opens = []
        self.keepalives = 0
        self.closes = 0
        #: 11-07 起设备屏会发配置命令并排水，因此替身要回答这两条。
        self.commands = []
        self.results = []
        self.requests = 0

    def send_command(self, action, **kwargs):
        self.requests += 1
        request_id = "c%d" % (self.requests,)
        self.commands.append((action, kwargs, request_id))
        return request_id

    def take_command_results(self, limit=None):
        taken = self.results
        self.results = []
        return taken

    def deliver(self, session_id, message):
        """把一条 S2C 消息喂给所有回调（照 `NetworkClient.receive` 的做法）。"""

        class _Envelope(object):
            def __init__(self, session_id):
                self.session_id = session_id

        for handler in list(self.handlers):
            handler(_Envelope(session_id), message)

    def listen(self, handler):
        return 1

    def receive(self, value):
        return value

    def on_message(self, handler):
        self.handlers.append(handler)
        return len(self.handlers)

    def clear_handlers(self):
        count = len(self.handlers)
        self.handlers = []
        return count

    def open_session(self, device_ref, dimension):
        self.opens.append((device_ref, dimension))
        return "req-%d" % len(self.opens)

    def establish(self):
        self.session_id = "s-%d" % len(self.opens)

    def has_session(self):
        return bool(self.session_id)

    def keepalive(self):
        if not self.session_id:
            raise ValueError("还没有会话，不能保活")
        self.keepalives += 1
        return "k"

    def close_session(self):
        self.closes += 1
        self.session_id = ""

    def diagnostics(self):
        return {"session_id": self.session_id}


class TestProxyPanelLifecycle(unittest.TestCase):
    """屏的会话生命周期。这里守的是**屏会不会卡死**，不是状态栏好不好看。

    `_adopt` 一跑，`self.client` 就不再是 `None`，而认领身份的那条支路只在
    `self.client is None` 时才走。于是「开出去了但一直没建起来」如果被当成
    「还在等回包」，屏就永远回不到认领那一支——**下一次右键推来的身份也没人接**。
    """

    def setUp(self):
        self.patcher = mock.patch.object(proxy_panel, "NetworkClient", _FakeClient)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        # 进程级的兜底 latch 在测试之间是同一只，用例之间要清干净，
        # 否则上一条用例留下的身份会被下一条认领走。
        client_identity.latch().clear()
        self.addCleanup(client_identity.latch().clear)
        self.ref = DeviceRef(NodeKey(0, 4, 5, 6), 2)

    def offer(self):
        return client_identity.latch().offer(
            device_identity.identity_payload(
                device_panels.BusItemOutputPanel._screen_key, self.ref, 0))

    def panel(self):
        return device_panels.BusItemOutputPanel("probe.main", FakeScreen())

    def tick(self, panel, times):
        for _ in range(times):
            panel.OnTicking()

    def test_a_refused_open_does_not_wedge_the_panel(self):
        self.offer()
        panel = self.panel()
        panel.OnCreate()
        client = panel.client
        self.assertEqual(len(client.opens), 1)
        # 服务端拒了（设备不在网上 / 没有主控）：客户端丢掉会话，会话号仍是空。
        client.session_id = ""
        self.tick(panel, proxy_panel.SESSION_OPEN_GRACE_FRAMES + 1)
        self.assertIsNone(panel.client, "被拒之后必须收摊，否则屏再也认不到身份")
        self.assertIsNone(panel.status)
        self.assertEqual(client.port.unlistens, 1)
        self.assertEqual(client.closes, 1)
        # **收摊之后还能被下一次右键救回来**（latch 里那条已经被取走，
        # 所以要新推一条）——这正是收摊的意义。
        self.offer()
        self.tick(panel, 1)
        self.assertIsNotNone(panel.client)

    def test_a_session_that_establishes_is_kept_and_kept_alive(self):
        self.offer()
        panel = self.panel()
        panel.OnCreate()
        panel.client.establish()
        self.tick(panel, proxy_panel.KEEPALIVE_FRAMES)
        self.assertIsNotNone(panel.client)
        self.assertEqual(panel.client.keepalives, 1)
        # 再等一个周期又是一条：保活是**周期**的，不是只发一次。
        self.tick(panel, proxy_panel.KEEPALIVE_FRAMES)
        self.assertEqual(panel.client.keepalives, 2)

    def test_a_session_lost_after_it_worked_is_released_at_once(self):
        self.offer()
        panel = self.panel()
        panel.OnCreate()
        panel.client.establish()
        self.tick(panel, 1)
        panel.client.session_id = ""      # 服务端判死 / TTL 回收
        self.tick(panel, 1)
        self.assertIsNone(panel.client)
        self.assertIsNone(panel.status)
        # 收摊之后 latch 是空的，屏安静地停住（不自救、也不再发包）。
        self.tick(panel, 50)
        self.assertIsNone(panel.client)

    def test_nothing_happens_before_an_identity_arrives(self):
        """身份没到就什么都不做——引擎自己会把容器格子画出来，屏不能把它搞空。"""
        panel = self.panel()
        panel.OnCreate()
        self.tick(panel, 30)
        self.assertIsNone(panel.client)
        self.assertIsNone(panel.status)
        self.assertFalse(panel.device_status_visible())

    def test_destroy_releases_everything(self):
        self.offer()
        panel = self.panel()
        panel.OnCreate()
        panel.client.establish()
        panel.OnDestroy()
        self.assertIsNone(panel.client)
        self.assertIsNone(panel.status)
        # **幂等**：重复销毁是安全的。
        panel.OnDestroy()


class TestSharedStatusHandler(unittest.TestCase):
    def test_terminals_and_proxies_inherit_the_same_status_handler(self):
        from endernet_bp.endernet_scripts.endernet.client.ui import (
            terminal_panel, crafting_panel,
        )
        shared = device_status_bindings.DeviceStatusBindingsMixin._make_status_handler
        for panel_cls in (terminal_panel.CommonTerminalPanel,
                          crafting_panel.CraftingPanel, proxy_panel.ProxyPanelBase):
            self.assertIs(panel_cls._make_status_handler, shared)


class TestClientDiagnostics(unittest.TestCase):
    def test_the_identity_counters_are_reachable_from_the_client_bootstrap(self):
        """`rejected` / `accepted` / `taken` 必须能读到。

        它们被文档称为「这条链路的第一个落点」，而落不到任何地方就等于没有：
        实机上那条链路的失败症状只是「屏什么都没显示」。
        """
        from endernet_bp.endernet_scripts.endernet.client.bootstrap import (
            ClientBootstrap,
        )
        report = ClientBootstrap().diagnostics()
        self.assertIn("device_identity", report)
        # 监听没起来时是 None，但**这个键必须在**：否则实机上无从判断
        # 「是没起监听」还是「这个版本根本没有这一项」。
        service = client_identity.get_device_identity()
        if service is None:
            self.assertIsNone(report["device_identity"])
        else:
            self.assertIn("latch", report["device_identity"])
            self.assertIn("accepted", report["device_identity"]["latch"])


class TestServerSidePush(unittest.TestCase):
    """服务端那一半：右键容器方块 → 推一条身份；空闲保活刷新会话计时。"""

    def setUp(self):
        from endernet_bp.endernet_scripts.endernet.server.adapters import (
            events as server_events,
        )
        self.module = server_events
        self.ref = DeviceRef(NodeKey(0, 1, 2, 3), 4)

    def _binder(self, known=True):
        module = self.module

        class _Record(object):
            ref = self.ref

        class _Registry(object):
            def get(self, node_key):
                return _Record() if known else None

        class _Queue(object):
            def push(self, event):
                pass

        return module.ServerEventBinder(_Queue(), None, _Registry(), None)

    def _click(self, binder, block_name):
        class _Event(object):
            blockName = block_name
            playerId = "p1"
            dimensionId = 0
            x = 1
            y = 2
            z = 3

        binder._on_block_use(_Event())

    def _capture_push(self):
        """把推身份那一步换成记一笔，返回记录表。

        `patch.object` 而不是直接赋值：直接赋值之后 `del` 会把模块属性整个
        删掉，后一个用例就 AttributeError 了（而且失败路径上更乱）。
        """
        pushed = []
        patcher = mock.patch.object(
            self.module, "send_device_identity",
            lambda player_id, payload: pushed.append((player_id, payload)) or True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return pushed

    def test_container_block_use_pushes_the_authoritative_identity(self):
        binder = self._binder()
        pushed = self._capture_push()
        self._click(binder, "endernet:bus_item_output_io")
        self.assertEqual(len(pushed), 1)
        player_id, payload = pushed[0]
        self.assertEqual(player_id, "p1")
        self.assertEqual(payload["key"], CONTAINER_SCREENS[
            "endernet:bus_item_output_io"])
        self.assertEqual(payload["dimension"], 0)
        # 身份必须是**注册表里那一台的**，不是事件里报的。
        self.assertEqual(device_identity.parse_identity(payload)[1], self.ref)
        self.assertNotIn("block_use_opened", binder.counts)
        self.assertEqual(binder.counts["block_use_container"], 1)

    def test_container_push_is_not_rate_limited(self):
        """**不限速**：它幂等（客户端后写覆盖先写），不像 `PushUIRequest`
        会真的多开一个界面。限速会让「先点终端、0.2 秒内再点取货口」白丢
        一次身份。"""
        binder = self._binder()
        pushed = self._capture_push()
        for _ in range(5):
            self._click(binder, "endernet:bus_item_output_io")
        self.assertEqual(len(pushed), 5)

    def test_unknown_position_pushes_nothing(self):
        binder = self._binder(known=False)
        pushed = self._capture_push()
        self._click(binder, "endernet:bus_item_output_io")
        self.assertEqual(pushed, [])
        self.assertEqual(binder.counts["block_use_container_unknown"], 1)

    def test_send_device_identity_skips_a_missing_recipient(self):
        from endernet_bp.endernet_scripts.endernet.server.adapters import (
            device_identity as server_identity,
        )
        sent = []
        self.assertFalse(server_identity.send_device_identity(
            "", {}, events_module=FakeEvents(),
            notify=lambda target, event: sent.append(target)))
        self.assertEqual(sent, [])


class TestKeepalive(unittest.TestCase):
    def test_kind_is_a_client_to_server_message(self):
        self.assertIn(protocol.MSG_KEEPALIVE, protocol.CLIENT_TO_SERVER_KINDS)
        self.assertNotIn(protocol.MSG_KEEPALIVE, protocol.SERVER_TO_CLIENT_KINDS)
        self.assertIn("keepalive", protocol.sync_message_kinds())

    def test_wire_shape_is_exactly_kind_and_request_id(self):
        message = protocol.KeepaliveMessage("r1")
        self.assertEqual(message.to_dict(),
                         {"kind": "keepalive", "request_id": "r1"})
        self.assertEqual(
            protocol.decode_sync_message(message.to_dict()).request_id, "r1")
        with self.assertRaises(ValueError):
            protocol.KeepaliveMessage.from_dict(
                {"kind": "keepalive", "request_id": "r1", "count": 1})

    def test_client_sends_only_while_it_has_a_session(self):
        port = _RecordingPort()
        client = NetworkClient(port=port)
        with self.assertRaises(ValueError):
            client.keepalive()
        client.session_id = "s1"
        client.network_id = "net"
        client.epoch = "e1"
        self.assertTrue(client.keepalive())
        sent = port.sent[-1]
        self.assertEqual(sent["session_id"], "s1")
        self.assertEqual(sent["message"]["kind"], protocol.MSG_KEEPALIVE)
        # **不记在途**：服务端不回包，记了只会涨出一条永远取不走的记录。
        self.assertEqual(client.pending_commands(), 0)

    def test_the_server_touches_the_session_and_answers_nothing(self):
        """走真的 `TerminalSyncHub.handle`：保活刷新空闲计时，且**不回包**。"""
        from endernet_bp.endernet_scripts.endernet.server.sync.sessions import (
            SessionPolicy, SessionRegistry,
        )
        from endernet_bp.endernet_scripts.endernet.server.sync.terminal import (
            TerminalSyncHub,
        )

        policy = SessionPolicy()
        registry = SessionRegistry(SessionPolicy(session_ttl_frames=10))
        # 直接种一条会话：本用例只关心保活那一步，不重跑开会话的全链路。
        # `context_of` 仍要注入——`handle` 对每条非 `session_open` 的消息都
        # 走一次复核，不给它就会在复核那一关把会话判掉。
        from endernet_bp.endernet_scripts.endernet.server.sync.sessions import (
            SessionContext,
        )
        context = SessionContext(
            player_id="p1", record_ref=self._ref(), network_id="1",
            epoch="1#1", master_instance_id=1, network_workable=True,
            player_dimension=0, player_position=(0.0, 0.0, 0.0),
        )
        hub = TerminalSyncHub(
            registry=registry, policy=policy,
            context_of=lambda player_id, ref: context,
        )
        session = registry.open(
            protocol.SyncEnvelope(
                protocol.SessionOpen("r0", context.record_ref, 0), view_kind="item"),
            context, 0)[0]
        self.assertIsNotNone(session)
        session.touch(0)
        envelope = protocol.SyncEnvelope(
            protocol.KeepaliveMessage("r1"),
            session_id=session.session_id,
            network_id=session.network_id,
            epoch=session.epoch,
            view_kind=session.view_kind,
        )
        self.assertIsNone(hub.handle(envelope, "p1", frame=9))
        self.assertEqual(session.last_seen_frame, 9)
        self.assertEqual(hub.stats["keepalives"], 1)
        # 空闲计时真的被推掉了：第 11 帧不再算过期。
        ttl_policy = registry.policy
        self.assertFalse(session.is_expired(11, ttl_policy))
        self.assertTrue(session.is_expired(21, ttl_policy))

    def _ref(self):
        from endernet_bp.endernet_scripts.endernet.common.identity import (
            DeviceRef, NodeKey,
        )
        return DeviceRef(NodeKey(0, 1, 2, 3), 1)


class _RecordingPort(object):
    """只记下发出去的字典，不需要真的引擎。"""

    def __init__(self):
        self.sent = []

    def listen(self, handler, priority=0):
        return 0

    def unlisten(self):
        return 0

    def send(self, payload):
        self.sent.append(payload)
        return True


class TestJsonContract(unittest.TestCase):
    """界面 JSON 的接线。**只验名字与位置对得上**——控件画不画得出来是
    游戏内实测项。"""

    def test_assemble_chamber_inputs_use_first_nine_container_slots(self):
        document = _load_ui("assemble_chamber.json")
        grids = []

        def walk(node):
            if isinstance(node, dict):
                if "source_items_grid" in node:
                    grids.append(node["source_items_grid"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(document["content"])
        self.assertEqual(len(grids), 1)
        grid = grids[0]
        self.assertEqual(grid["collection_name"], "netease_container")
        self.assertEqual(grid["grid_dimensions"], [3, 3])
        self.assertEqual(grid["maximum_grid_items"], 9)
        self.assertEqual(grid["grid_item_template"],
                         "endernet_container_common.container_grid_item")

    def test_every_panel_json_instantiates_the_status_popup(self):
        for device_id, screen_name in CONTAINER_SCREENS.items():
            document = _load_ui(ui_file_of(device_id))
            controls = document["content"]["controls"]
            popups = [item for item in controls
                      if "device_status_popup@endernet_windows"
                      ".device_status_window_basic" in item]
            self.assertEqual(len(popups), 1, device_id)
            popup = popups[0][
                "device_status_popup@endernet_windows.device_status_window_basic"]
            self.assertEqual(popup["layer"], 40)
            self.assertEqual(popup["anchor_from"], "center")
            self.assertEqual(popup["anchor_to"], "center")
            self.assertEqual(popup["bindings"], [{
                "binding_condition": "always",
                "binding_name": device_status.POPUP_VISIBLE_BINDING,
                "binding_name_override": "#visible",
                "binding_type": "global",
            }])
            self.assertTrue(screen_name)

    def test_the_popup_sits_above_every_other_control_in_its_screen(self):
        """弹窗必须盖在这张屏的**所有**其它控件之上。

        比 `close` 是不够的：四台设备的 JSON 里根本没有顶层 `close@`（关闭
        按钮在 `device_status_window_basic` 里面），那条断言会退化成
        `40 > 0`，改坏了也照绿。这里取的是 `content` 里**除了弹窗自己之外**
        的最大层号——它才是「会不会被别的控件压住」的真判据。
        """
        for device_id in CONTAINER_SCREENS:
            document = _load_ui(ui_file_of(device_id))
            found = {"popup": None, "others": []}

            def walk(node):
                if isinstance(node, dict):
                    for key, value in node.items():
                        layer = value.get("layer") if isinstance(value, dict) else None
                        if key.startswith("device_status_popup@"):
                            found["popup"] = layer
                        elif isinstance(layer, int):
                            found["others"].append(layer)
                        walk(value)
                elif isinstance(node, list):
                    for item in node:
                        walk(item)

            walk(document["content"])
            self.assertIsNotNone(found["popup"], device_id)
            self.assertTrue(found["others"], device_id)
            self.assertGreater(found["popup"], max(found["others"]), device_id)

    def test_every_panel_json_declares_the_screen_name(self):
        """`namespace.main` 就是引擎开的屏幕名，必须与注册键逐字相同。"""
        for device_id, screen_name in CONTAINER_SCREENS.items():
            namespace, _, control = screen_name.partition(".")
            document = _load_ui(ui_file_of(device_id))
            self.assertEqual(document["namespace"], namespace, device_id)
            # 屏的键是 `main@common.inventory_screen_common`——带继承的写法，
            # 引擎认的是 `@` 前面那一段。
            self.assertTrue(
                any(key == control or key.split("@")[0] == control
                    for key in document), device_id)

    def test_the_shared_stat_bar_is_instantiated_by_every_panel(self):
        for device_id in CONTAINER_SCREENS:
            document = _load_ui(ui_file_of(device_id))
            text = json.dumps(document)
            self.assertIn("stat_bar@endernet_ui_common.device_stat_bar", text,
                          device_id)


if __name__ == "__main__":
    unittest.main()
