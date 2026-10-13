# coding=utf-8
"""样板终端（11-05）的客户端半边：四区绑定、双向模型、长按改数量与接线。

守的是**界面与协议之间的那层缝**，缝里的错在游戏里几乎都看不出来：

- 四个区共用一个集合名——后面的区按前面的区的下标去读，界面照常渲染；
- 空位与「有东西但没有名字」分不开——格子上有数量、没有图标；
- 选中虚格之后点背包槽**还去动背包**——玩家的配置没清掉，背包里的东西
  却被选中／搬走了；
- 配置编辑不带修订号——服务端没办法挡住一个过期界面的写入；
- `template_state` 的旧包盖回来——画面上是几帧前的配置，而没有任何迹象。

界面的另一半（控件画没画出来、`$` 变量能不能替换、长按到底会不会派发
`BF_ButtonClickDown`）离线测不了，属游戏内实测项。

**替身从 `test_crafting_panel` 借**（`test_select_item_window` 是被更多文件
借的那一份）：`tooldelta` 的三份替身必须与别的测试文件取值一致，各写一份
反而是漂移的来源。
"""

import json
import os
import unittest
from unittest.mock import patch

import testsupport  # noqa: F401  设置 sys.path

from test_crafting_panel import (
    BinderStub,
    ControlStub,
    ItemStub,
    RecordingPort,
    ROOT,
    Screen,
    REF,
    SNAPSHOT_VERSION,
)

from endernet_bp.endernet_scripts.endernet.client.network_client import (
    NetworkClient,
)
from endernet_bp.endernet_scripts.endernet.client.ui import (
    backpack_model,
    stack_size,
    template_panel,
)
from endernet_bp.endernet_scripts.endernet.client.ui.template_panel import (
    BINDING_PREFIX,
    COLLECTION_OF,
    MODE_CRAFTING,
    MODE_FACTORY,
    SURFACE_INPUT,
    SURFACE_OUTPUTS,
    SURFACE_RESULT,
    SURFACE_SOURCE,
    TemplateView,
)
from endernet_bp.endernet_scripts.endernet.client.ui.terminal_cache import (
    TerminalCache,
)
from endernet_bp.endernet_scripts.endernet.client.ui import terminal_panel
from endernet_bp.endernet_scripts.endernet.common.definitions import (
    TERMINAL_SCREENS,
)
from endernet_bp.endernet_scripts.endernet.common.identity import (
    DeviceRef,
    NodeKey,
)
from endernet_bp.endernet_scripts.endernet.common.protocol import (
    InventorySnapshot,
    MSG_TEMPLATE_STATE,
    STATUS_OK,
    STATUS_REJECTED,
    Reason,
    SessionReady,
    SyncEnvelope,
    TemplateStateMessage,
)
from endernet_bp.endernet_scripts.endernet.common.resource_key import (
    ResourceKey,
)

STONE = ResourceKey.item("minecraft:stone")
STICK = ResourceKey.item("minecraft:stick")
FACTORY = ResourceKey.item("endernet:factorical_template")

LIMITS = {"max_count": 65535, "default_count": 1}

SURFACE_CELLS = {
    SURFACE_SOURCE: 9,
    SURFACE_OUTPUTS: 3,
    SURFACE_INPUT: 1,
    SURFACE_RESULT: 1,
}


def rows(count, entries=(), locked=False):
    """一个区的线上形状。空位是**五条一起空**（+ `locked`）。"""
    out = [{"slot": index, "canonical": "", "count": 0, "name": "", "aux": 0,
            "locked": locked}
           for index in range(count)]
    for slot, key, count_value in entries:
        out[slot] = {"slot": slot, "canonical": key.canonical(),
                     "count": count_value, "name": key.name, "aux": key.aux,
                     "locked": locked}
    return out


class TemplateFixture(unittest.TestCase):

    def setUp(self):
        # `stack_size` 的缓存挂在模块上，跨用例存活，必须每条擦一次。
        self.addCleanup(stack_size.reset)
        stack_size.reset()
        self.port = RecordingPort()
        self.client = NetworkClient(port=self.port)
        self.screen = Screen()
        # 面板是**绑定的登记处**（二十二条写在类体里），四个区只是被转发的
        # 那一头。与 `test_crafting_panel.CraftingFixture` 同一个形状。
        self.panel = template_panel.TemplatePanel("main", self.screen, {})
        self.panel._activated = True
        self.view = TemplateView(self.panel, client=self.client)
        self.panel.template = self.view
        self.panel.config = self.view
        self.client.on_message(self.panel._make_template_handler(self.client))
        self.backpack = backpack_model.BackpackModel()
        self.panel.backpack = self.backpack
        #: 假的引擎背包读数，由 `engine_backpack()` 填。**引擎槽序**：
        #: 0–8 是快捷栏、9–35 是背包，与界面那两段的顺序正好相反。
        self.engine_items = [None] * backpack_model.SLOT_COUNT
        self.addCleanup(self.view.close)
        self.feed(SessionReady(7, SNAPSHOT_VERSION))

    # -- 收发 ----------------------------------------------------------

    def feed(self, message, session_id="s1", network_id="n1", epoch="e1"):
        return self.client.receive(
            SyncEnvelope(message, session_id, network_id, epoch).to_dict())

    def state(self, source=(), outputs=(), input_rows=(), result_rows=(),
              mode=MODE_CRAFTING, revision=1, limits=None,
              locked_outputs=None, session_id="s1"):
        self.feed(TemplateStateMessage(
            revision, mode,
            rows(9, source, False),
            rows(3, outputs,
                 mode == MODE_CRAFTING if locked_outputs is None
                 else locked_outputs),
            rows(1, input_rows, False),
            rows(1, result_rows, False),
            limits if limits is not None else LIMITS,
        ), session_id=session_id)
        return self.view

    def item_command(self, **overrides):
        """发一条命令的公共形状。"""
        defaults = {
            "expected_index_version": 7,
            "target": None, "cell": None, "key": None, "count": None,
            "slot": None, "slot_version": None,
        }
        defaults.update(overrides)
        return defaults

    def commands(self):
        return [item["message"] for item in self.port.sent
                if item["message"]["kind"] == "command"]

    def last_command(self):
        return self.commands()[-1]

    def deliver(self, result):
        """把一条回执交给视图——**走生产的那条路**：列表视图取走、转发过来。"""
        taken = [type("Envelope", (object,), {"result": result})()]
        self.view.on_results(taken)

    def result(self, request_id, status=STATUS_OK, reasons=None):
        return {"request_id": request_id, "status": status, "payload": {},
                "reasons": reasons if reasons is not None else [],
                "topology_version": None}

    # -- 引擎背包 ------------------------------------------------------

    def engine_backpack(self, *entries):
        """装上一份假的引擎背包读数。`entries` 是 `(引擎槽号, 资源名, 数量)`。"""
        patcher = patch.object(backpack_model, "_read_engine_items",
                               lambda: list(self.engine_items))
        patcher.start()
        self.addCleanup(patcher.stop)
        for slot, name, count in entries:
            self.engine_items[slot] = ItemStub(
                name, count, orig=testsupport.engine_item(name, count))
        self.backpack.replace_from_engine()
        rows_out = []
        for slot, item in enumerate(self.engine_items):
            resource = backpack_model.engine_item_key(item) if item else None
            rows_out.append({
                "slot": slot,
                "canonical": resource.canonical() if resource else "",
                "count": item.count if item else 0, "slot_version": 7,
            })
        self.feed(InventorySnapshot(7, rows_out))

    def select_backpack(self, ui_index):
        self.engine_backpack((9, "minecraft:stone", 40))
        self.assertEqual(self.backpack.select(ui_index), ui_index)

    # -- 绑定取值口 ----------------------------------------------------

    FLAGS = {
        "visible": BinderStub.BF_BindBool,
        "item_id_aux": BinderStub.BF_BindInt,
        "item_count": BinderStub.BF_BindString,
        "selected": BinderStub.BF_BindBool,
    }

    def cell(self, surface, field, index):
        key = (self.FLAGS[field], COLLECTION_OF[surface],
               BINDING_PREFIX + surface + "_" + field)
        return self.screen.collection_bindings[key](index)

    def binding(self, name, flag=BinderStub.BF_ButtonClickUp, *args):
        key = (flag, BINDING_PREFIX + name)
        return self.screen.bindings[key](*args)

    def click_cell(self, surface, index):
        self.binding(surface + "_click", BinderStub.BF_ButtonClickUp,
                     {"#collection_index": index})

    def press_cell(self, surface, index):
        self.binding(surface + "_click", BinderStub.BF_ButtonClickDown,
                     {"#collection_index": index})


class TestTemplateState(TemplateFixture):
    """`template_state` 进得来、存得对、旧包盖不回去。"""

    def test_the_message_lands_whole(self):
        self.state(source=((0, STONE, 4),), result_rows=((0, FACTORY, 1),),
                   mode=MODE_FACTORY, revision=3)
        self.assertEqual(3, self.view.revision)
        self.assertEqual(MODE_FACTORY, self.view.mode)
        self.assertEqual(4, self.view.cell(SURFACE_SOURCE, 0)["count"])
        self.assertEqual(FACTORY.name, self.view.cell(SURFACE_RESULT, 0)["name"])

    def test_an_empty_cell_is_empty_by_canonical(self):
        self.state(source=((0, STONE, 4),))
        self.assertIsNone(self.view.cell(SURFACE_SOURCE, 1))
        self.assertIsNotNone(self.view.cell(SURFACE_SOURCE, 0))

    def test_a_row_without_a_name_renders_nothing_rather_than_guessing(self):
        """协议层会拒这种行；真漏进来时也不许画出幽灵图标。"""
        broken = rows(9)
        broken[0] = {"slot": 0, "canonical": "item|x|0|...", "count": 1,
                     "name": "", "aux": 0, "locked": False}
        # 协议层会拒这种行；这里绕过它直接塞给视图，钉住视图那一侧也不许猜。
        self.view.rows[SURFACE_SOURCE] = tuple(broken)
        self.assertEqual(0, self.view.id_aux(SURFACE_SOURCE, 0))

    def test_an_older_revision_never_overwrites_the_newer_one(self):
        self.state(revision=5, source=((0, STONE, 4),))
        self.state(revision=4, source=((1, STICK, 9),))
        self.assertEqual(5, self.view.revision)
        self.assertIsNotNone(self.view.cell(SURFACE_SOURCE, 0))
        self.assertIsNone(self.view.cell(SURFACE_SOURCE, 1))

    def test_the_same_revision_is_applied_again(self):
        self.state(revision=2)
        self.assertTrue(self.view.apply(TemplateStateMessage(
            2, MODE_FACTORY, rows(9), rows(3), rows(1), rows(1), LIMITS)))
        self.assertEqual(MODE_FACTORY, self.view.mode)

    def test_an_unknown_mode_is_ignored_rather_than_stored(self):
        self.state(mode=MODE_CRAFTING)
        message = TemplateStateMessage(
            9, MODE_FACTORY, rows(9), rows(3), rows(1), rows(1), LIMITS)
        message.mode = "smelting"
        self.view.apply(message)
        self.assertEqual(MODE_CRAFTING, self.view.mode)

    def test_the_other_sessions_state_is_dropped_by_the_panel_handler(self):
        self.state(revision=4, source=((0, STONE, 4),), session_id="s9")
        self.assertEqual(-1, self.view.revision)

    def test_the_limits_come_off_the_wire(self):
        self.state(limits={"max_count": 64, "default_count": 3})
        self.assertEqual(64, self.view.limits["max_count"])
        self.assertEqual(3, self.view.limits["default_count"])

    def test_stale_selection_is_dropped_when_the_cell_empties(self):
        self.state(source=((0, STONE, 1),))
        self.view.selection = (SURFACE_SOURCE, 0)
        self.state(source=(), revision=2)
        self.assertIsNone(self.view.selection)


class TestCellAccessors(TemplateFixture):

    def test_locked_is_read_even_for_an_empty_cell(self):
        """合成模式中间那格没有配方时是空的，但一样不许玩家往里放东西。"""
        self.state()
        self.assertTrue(self.view.locked(SURFACE_OUTPUTS, 1))
        self.assertIsNone(self.view.cell(SURFACE_OUTPUTS, 1))

    def test_the_factory_outputs_are_unlocked(self):
        self.state(mode=MODE_FACTORY)
        self.assertFalse(self.view.locked(SURFACE_OUTPUTS, 0))

    def test_an_out_of_range_or_boolean_index_is_not_a_cell(self):
        self.state(source=((0, STONE, 1),))
        for bad in (-1, 9, True, "0", None):
            self.assertIsNone(self.view.cell(SURFACE_SOURCE, bad))
            self.assertTrue(self.view.locked(SURFACE_SOURCE, bad))

    def test_a_count_of_one_is_blank_and_more_is_shown(self):
        self.state(source=((0, STONE, 1), (1, STICK, 12)))
        self.assertEqual("", self.view.count_text(SURFACE_SOURCE, 0))
        self.assertEqual("12", self.view.count_text(SURFACE_SOURCE, 1))

    def test_a_byte_string_name_from_the_wire_still_draws(self):
        """**过线回来的名字是字节串，不是 unicode。**

        只认 `unicode` 的写法会把它当成非文本、换成空串，`id_aux` 随之恒为 0
        ——**格子里有配置、画面上没有图标**，而界面上看不出是没数据还是没渲染。
        2026-10-02 真机上正是这个症状（九宫格三格配好了、一个图标都没有）。

        **这一条必须自己造行**：别的用例喂的是 `ResourceKey.name`（unicode），
        而线上给的是 `str`（py2 的字节串）；夹具与真机在这一处的差别，就是
        这个 bug 能躲过全部离线用例的原因。
        """
        row = {"slot": 0, "canonical": "item|minecraft:dirt|0|...", "count": 1,
               "name": b"minecraft:dirt", "aux": 0, "locked": False}
        self.view.rows[SURFACE_SOURCE] = (row,)
        self.assertEqual(u"minecraft:dirt",
                         self.view.cell(SURFACE_SOURCE, 0)["name"])
        self.assertNotEqual(0, self.view.id_aux(SURFACE_SOURCE, 0))

    def test_a_non_text_name_is_blank_rather_than_guessed(self):
        row = {"slot": 0, "canonical": "item|x|0|...", "count": 1,
               "name": 7, "aux": 0, "locked": False}
        self.view.rows[SURFACE_SOURCE] = (row,)
        self.assertEqual(u"", self.view.cell(SURFACE_SOURCE, 0)["name"])
        self.assertEqual(0, self.view.id_aux(SURFACE_SOURCE, 0))

    def test_the_icon_comes_from_the_name_on_the_wire(self):
        self.state(source=((0, STONE, 1),))
        self.assertEqual(stack_size.id_aux(STONE.name),
                         self.view.id_aux(SURFACE_SOURCE, 0))

    def test_every_cell_is_visible_including_the_empty_ones(self):
        self.state()
        for index in range(9):
            self.assertTrue(self.cell(SURFACE_SOURCE, "visible", index))
        self.assertTrue(self.cell(SURFACE_RESULT, "visible", 0))

    def test_the_title_follows_the_mode(self):
        """**逐字钉住**（User 2026-10-02 定的措辞），不是只查包含关系：
        分隔符与括号差一个字符，眼睛在界面上看不出来，而这是交接单上写明的文案。"""
        self.state(mode=MODE_CRAFTING)
        self.assertEqual(u"样板配置 - 合成样板", self.view.title())
        self.state(mode=MODE_FACTORY, revision=2, locked_outputs=False)
        self.assertEqual(u"样板配置 - 工厂样板", self.view.title())

    def test_the_title_is_never_a_blank_string(self):
        """没有身份时也要给得出一个完整句子——空标题看起来像界面没建起来。"""
        self.assertEqual(u"样板配置 - 合成样板", self.view.title())

    def test_the_factory_mode_shows_all_three_output_cells(self):
        self.state(mode=MODE_FACTORY, locked_outputs=False)
        for index in range(3):
            self.assertTrue(self.cell(SURFACE_OUTPUTS, "visible", index), index)

    def test_the_crafting_mode_shows_only_the_middle_output_cell(self):
        """上下两格 User 2026-10-02 定：整个不画。"""
        self.state(mode=MODE_CRAFTING)
        self.assertFalse(self.cell(SURFACE_OUTPUTS, "visible", 0))
        self.assertFalse(self.cell(SURFACE_OUTPUTS, "visible", 2))

    def test_the_crafting_middle_cell_stays_visible_with_or_without_a_recipe(self):
        """**「没有产物」不靠隐藏表达**，靠 `id_aux` 回 0——与下半屏背包槽
        同一套做法（那边 `_slot_visible` 恒真、`_slot_item_id_aux` 空格回 0，
        已经跑通）。格子留着，槽底还在、里面没图标。"""
        self.state(mode=MODE_CRAFTING)
        self.assertTrue(self.cell(SURFACE_OUTPUTS, "visible", 1))
        self.assertEqual(0, self.cell(SURFACE_OUTPUTS, "item_id_aux", 1))
        self.state(mode=MODE_CRAFTING, outputs=((1, STICK, 4),), revision=2)
        self.assertTrue(self.cell(SURFACE_OUTPUTS, "visible", 1))
        self.assertNotEqual(0, self.cell(SURFACE_OUTPUTS, "item_id_aux", 1))

    def test_a_cleared_source_cell_goes_back_to_zero_not_hidden(self):
        """同一个机制在九宫格上：清空之后 `id_aux` 回 0、格子仍可见
        ——和背包槽把东西拿走之后的样子一致。"""
        self.state(source=((0, STONE, 3),))
        self.assertNotEqual(0, self.cell(SURFACE_SOURCE, "item_id_aux", 0))
        self.state(source=(), revision=2)
        self.assertTrue(self.cell(SURFACE_SOURCE, "visible", 0))
        self.assertEqual(0, self.cell(SURFACE_SOURCE, "item_id_aux", 0))

    def test_switching_modes_re_evaluates_the_output_cells(self):
        self.state(mode=MODE_CRAFTING)
        self.assertFalse(self.cell(SURFACE_OUTPUTS, "visible", 0))
        self.state(mode=MODE_FACTORY, revision=2, locked_outputs=False)
        self.assertTrue(self.cell(SURFACE_OUTPUTS, "visible", 0))

    def test_the_other_three_surfaces_are_always_visible(self):
        """空格子也要画槽底——那几处是固定结构（同 `visible` 的文档）。"""
        self.state()
        for index in range(9):
            self.assertTrue(self.cell(SURFACE_SOURCE, "visible", index), index)
        self.assertTrue(self.cell(SURFACE_INPUT, "visible", 0))
        self.assertTrue(self.cell(SURFACE_RESULT, "visible", 0))

    def test_the_two_mode_frames_are_mutually_exclusive(self):
        self.state(mode=MODE_FACTORY, locked_outputs=False)
        self.assertFalse(self.binding("mode_crafting_selected", BinderStub.BF_BindBool))
        self.assertTrue(self.binding("mode_factory_selected", BinderStub.BF_BindBool))

    def test_without_a_session_neither_frame_lights_up(self):
        """没有会话时不亮任何一个框，比亮错一个要好。"""
        self.state()
        self.view.client.drop_session()
        self.assertFalse(self.binding("mode_crafting_selected", BinderStub.BF_BindBool))
        self.assertFalse(self.binding("mode_factory_selected", BinderStub.BF_BindBool))


class TestTwoWayModel(TemplateFixture):
    """交接第 4 节的双向模型，逐条钉住。"""

    def select_network(self, stored=40, reserved=0, has_pattern=False):
        view = terminal_panel.TerminalListView(
            self.panel, cache=TerminalCache(client=self.client),
            device_ref=REF, dimension=0, scroll_path=None,
            backpack=self.backpack)
        self.addCleanup(view.close)
        self.panel.list_view = view
        view.cache.model.replace_snapshot(
            SNAPSHOT_VERSION, [(STONE, stored, reserved, has_pattern)])
        view.refresh()
        view.on_select({"#collection_index": 0})
        self.assertEqual(STONE, view.cache.model.selected_key())
        return view

    def test_a_selected_network_item_is_copied_into_each_source_cell(self):
        self.state(revision=6)
        view = self.select_network(stored=40, reserved=40)
        for index in range(9):
            self.click_cell(SURFACE_SOURCE, index)
            command = self.last_command()
            self.assertEqual("template_cell_put", command["action"])
            self.assertEqual(SURFACE_SOURCE, command["target"])
            self.assertEqual(index, command["cell"])
            self.assertEqual(STONE.to_dict(), command["key"])
            self.assertEqual(6, command["slot_version"])
            self.assertIsNone(command["count"])
            self.assertIsNone(command["slot"])
        self.assertEqual(9, len(self.commands()))
        self.assertIsNone(self.view.selection)
        self.assertEqual(STONE, view.cache.model.selected_key())
        self.deliver(self.result(self.last_command()["request_id"]))
        self.assertIsNone(view.cache.model.selected_key())

    def test_a_pattern_only_network_item_can_configure_factory_outputs(self):
        self.state(mode=MODE_FACTORY)
        view = self.select_network(stored=0, has_pattern=True)
        self.click_cell(SURFACE_OUTPUTS, 2)
        command = self.last_command()
        self.assertEqual("template_cell_put", command["action"])
        self.assertEqual(SURFACE_OUTPUTS, command["target"])
        self.assertEqual(2, command["cell"])
        self.assertEqual(STONE.to_dict(), command["key"])
        self.deliver(self.result(command["request_id"], status=STATUS_REJECTED))
        self.assertEqual(STONE, view.cache.model.selected_key())

    def test_network_selection_cannot_edit_locked_crafting_output(self):
        self.state()
        view = self.select_network()
        self.click_cell(SURFACE_OUTPUTS, 1)
        self.assertEqual([], self.commands())
        self.assertEqual(STONE, view.cache.model.selected_key())

    def test_cleared_network_selection_does_not_copy_the_previous_item(self):
        self.state()
        view = self.select_network()
        view.cache.model.clear()
        self.click_cell(SURFACE_SOURCE, 4)
        self.assertEqual([], self.commands())
        self.assertEqual((SURFACE_SOURCE, 4), self.view.selection)

    def test_a_selected_backpack_item_is_copied_into_the_cell(self):
        self.select_backpack(0)
        self.state(source=((0, STONE, 3),))
        self.click_cell(SURFACE_SOURCE, 4)
        command = self.last_command()
        self.assertEqual("template_cell_put", command["action"])
        self.assertEqual(SURFACE_SOURCE, command["target"])
        self.assertEqual(4, command["cell"])
        self.assertEqual(STONE.name, command["key"]["name"])
        # **不带数量**：虚格的数量是配方需求，由服务端的策略给默认值。
        self.assertIsNone(command["count"])
        # 源槽一件不动（配置而已）。
        self.assertEqual(40, self.engine_items[9].count)

    def test_clicking_a_cell_without_a_source_only_selects_it(self):
        self.state(source=((0, STONE, 1),))
        self.click_cell(SURFACE_SOURCE, 0)
        self.assertEqual((SURFACE_SOURCE, 0), self.view.selection)
        self.assertEqual([], self.commands())

    def test_clicking_the_selected_cell_again_cancels(self):
        self.state(source=((0, STONE, 1),))
        self.click_cell(SURFACE_SOURCE, 0)
        self.click_cell(SURFACE_SOURCE, 0)
        self.assertIsNone(self.view.selection)
        self.assertEqual([], self.commands())

    def test_clicking_another_cell_moves_the_selection(self):
        """选中只有一份，因此「点另一格就挪过去」是自然结果——包括跨区。"""
        self.state(source=((0, STONE, 1),), mode=MODE_FACTORY,
                   locked_outputs=False)
        self.click_cell(SURFACE_SOURCE, 0)
        self.click_cell(SURFACE_OUTPUTS, 2)
        self.assertEqual((SURFACE_OUTPUTS, 2), self.view.selection)

    def test_a_locked_cell_never_takes_a_click(self):
        self.select_backpack(0)
        self.state()
        self.click_cell(SURFACE_OUTPUTS, 1)
        self.assertEqual([], self.commands())
        self.assertIsNone(self.view.selection)

    def test_a_backpack_click_clears_the_selected_cell(self):
        self.state(source=((0, STONE, 1),))
        self.click_cell(SURFACE_SOURCE, 0)
        before = len(self.commands())
        # 走生产的路径：面板的 `_click` 先问协作对象。
        self.panel._click({"#collection_index": 2}, 0, 27)
        command = self.last_command()
        self.assertEqual(before + 1, len(self.commands()))
        self.assertEqual("template_cell_clear", command["action"])
        self.assertEqual(0, command["cell"])
        self.assertIsNone(self.view.selection)
        self.assertIsNone(self.backpack.selected, "背包那一格没被选中")

    def test_a_row_click_clears_the_selected_cell_and_takes_nothing(self):
        self.select_backpack(0)
        self.state(source=((0, STONE, 1),))
        self.view.selection = (SURFACE_SOURCE, 0)
        view = terminal_panel.TerminalListView(
            self.panel, cache=TerminalCache(client=self.client),
            device_ref=REF, dimension=0, scroll_path=None,
            backpack=self.backpack)
        self.addCleanup(view.close)
        self.panel.list_view = view
        before = len(self.commands())
        view.on_select({"#collection_index": 0})
        self.assertEqual(before + 1, len(self.commands()))
        self.assertEqual("template_cell_clear", self.last_command()["action"])
        # **背包的选中不动**：那一下只清虚格，玩家可以接着填下一格。
        self.assertEqual(0, self.backpack.selected)
        self.assertEqual(1, len(self.commands()), "只发了那一条清空")

    def test_an_empty_selected_cell_sends_nothing_on_the_outside_click(self):
        self.state()
        self.view.selection = (SURFACE_SOURCE, 3)
        self.assertTrue(self.view.consumes_slot_click(0))
        self.assertEqual([], self.commands())
        self.assertIsNone(self.view.selection)

    def test_without_a_selection_the_hook_stays_out_of_the_way(self):
        self.state(source=((0, STONE, 1),))
        self.assertFalse(self.view.consumes_slot_click(0))
        self.assertFalse(self.view.consumes_row_click(0))

    def test_without_a_session_a_cell_click_sends_nothing(self):
        self.select_backpack(0)
        self.state(source=((0, STONE, 1),))
        self.client.drop_session()
        self.click_cell(SURFACE_SOURCE, 4)
        self.assertEqual([], self.commands())


class TestLongPress(TemplateFixture):
    """长按 = 改数量。**按住够久当场弹窗，不等松手**（User 2026-10-02）。"""

    def clock(self, start=100.0):
        """可推进的时钟。返回 `advance(seconds)`。"""
        now = [start]
        self.panel._clock = lambda: now[0]

        def advance(seconds):
            now[0] += seconds
            return now[0]
        return advance

    def captured(self):
        """把数量窗换成记录器，返回 `(opened, advance)` 之外的记录表。"""
        opened = []

        def recorder(screen, number, cb, min_number=1, max_number=None):
            opened.append((screen, number, cb, min_number, max_number))
            return True

        return opened, recorder

    def hold(self, surface, index, seconds=1.0, frames=1):
        """按住 `seconds` 秒，每帧让视图 tick 一次，**全程不松手**。"""
        advance = self.clock()
        opened, recorder = self.captured()
        with patch.object(template_panel, "show_number_set_window", recorder):
            self.press_cell(surface, index)
            advance(seconds)
            for _ in range(frames):
                self.view.tick()
        return opened

    def test_a_short_press_is_still_a_click(self):
        self.select_backpack(0)
        self.state(source=((0, STONE, 3),))
        advance = self.clock()
        self.press_cell(SURFACE_SOURCE, 4)
        advance(0.01)
        self.view.tick()
        self.click_cell(SURFACE_SOURCE, 4)
        self.assertEqual("template_cell_put", self.last_command()["action"])

    def test_a_long_press_fires_while_still_holding(self):
        """**关键那一条**：不松手就该弹出来。"""
        self.state(source=((0, STONE, 3),),
                   limits={"max_count": 64, "default_count": 1})
        opened = self.hold(SURFACE_SOURCE, 0, seconds=1.0)
        self.assertEqual(1, len(opened), "按住够久当场就该弹")
        _screen, number, _cb, minimum, maximum = opened[0]
        self.assertEqual(3, number, "初值就是格子里的数量")
        self.assertEqual(1, minimum)
        self.assertEqual(64, maximum, "上限来自服务端推来的 limits")

    def test_it_fires_exactly_once_per_press(self):
        self.state(source=((0, STONE, 3),))
        opened = self.hold(SURFACE_SOURCE, 0, seconds=1.0, frames=5)
        self.assertEqual(1, len(opened), "按住不松不该反复弹")

    def test_nothing_fires_before_the_threshold(self):
        self.state(source=((0, STONE, 3),))
        self.assertEqual([], self.hold(SURFACE_SOURCE, 0, seconds=0.1,
                                       frames=3))

    def test_releasing_after_a_fired_long_press_sends_no_click(self):
        """长按已经处理过这一下，松手时**不许再当成一次普通点击**。"""
        self.select_backpack(0)
        self.state(source=((0, STONE, 3),))
        self.hold(SURFACE_SOURCE, 0, seconds=1.0)
        before = len(self.commands())
        self.click_cell(SURFACE_SOURCE, 0)
        self.assertEqual(before, len(self.commands()), "松手不该再发一条")

    def test_a_press_that_ends_on_another_cell_is_a_click_there(self):
        """按住 A 拖到 B 再松手：算点 B。按下记录属于 A，**不许拿来判 B 的时长**。"""
        self.state(source=((0, STONE, 1),))
        advance = self.clock()
        self.press_cell(SURFACE_SOURCE, 0)
        advance(1.0)
        self.click_cell(SURFACE_SOURCE, 4)
        self.assertEqual((SURFACE_SOURCE, 4), self.view.selection)

    def test_a_long_press_on_an_empty_cell_opens_nothing(self):
        self.state()
        self.assertEqual([], self.hold(SURFACE_SOURCE, 2))

    def test_a_long_press_on_a_locked_cell_opens_nothing(self):
        self.state()
        self.assertEqual([], self.hold(SURFACE_OUTPUTS, 1))

    def test_the_press_record_is_always_cleared_on_release(self):
        """一次偶然的长按不许让之后每一下短按都变成改数量。"""
        self.state(source=((0, STONE, 3),))
        self.hold(SURFACE_SOURCE, 0, seconds=1.0)
        self.click_cell(SURFACE_SOURCE, 0)
        self.assertEqual({}, self.view.presses)

    def test_a_confirm_sends_the_quantity(self):
        self.state(source=((0, STONE, 3),))
        opened = self.hold(SURFACE_SOURCE, 0, seconds=1.0)
        opened[0][2](8)
        command = self.last_command()
        self.assertEqual("template_cell_count", command["action"])
        self.assertEqual(8, command["count"])
        self.assertEqual(0, command["cell"])

    def test_the_press_records_are_per_surface(self):
        self.state(source=((0, STONE, 1),), mode=MODE_FACTORY,
                   locked_outputs=False)
        self.clock()
        self.press_cell(SURFACE_SOURCE, 0)
        self.press_cell(SURFACE_OUTPUTS, 2)
        self.assertEqual(2, len(self.view.presses))
        self.assertEqual(0, self.view.presses[SURFACE_SOURCE][0])
        self.assertEqual(2, self.view.presses[SURFACE_OUTPUTS][0])

    def test_the_release_path_still_covers_a_frame_that_never_ticked(self):
        """帧率极低时按下与抬起之间可能一帧都没跑——那一支要兜住。"""
        self.state(source=((0, STONE, 3),))
        advance = self.clock()
        opened, recorder = self.captured()
        with patch.object(template_panel, "show_number_set_window", recorder):
            self.press_cell(SURFACE_SOURCE, 0)
            advance(1.0)
            self.click_cell(SURFACE_SOURCE, 0)
        self.assertEqual(1, len(opened))


class TestOutputRecipeLongPress(TemplateFixture):
    clock = TestLongPress.clock

    def test_half_second_opens_picker_once_and_release_does_not_click(self):
        self.state()
        advance = self.clock()
        with patch.object(template_panel, "show_item_list_window", return_value=True) as picker:
            self.press_cell(SURFACE_OUTPUTS, 1)
            advance(0.49)
            self.view.tick()
            picker.assert_not_called()
            advance(0.01)
            self.view.tick()
            self.view.tick()
            picker.assert_called_once()
            picker.call_args.kwargs["on_selected"]("minecraft:stick")
            command = self.last_command()
            self.assertEqual("template_import", command["action"])
            self.assertEqual(1, command["slot_version"])
            self.click_cell(SURFACE_OUTPUTS, 1)
            self.assertEqual(1, len(self.commands()))

    def test_hidden_outputs_and_short_presses_do_not_open_picker(self):
        self.state()
        advance = self.clock()
        with patch.object(template_panel, "show_item_list_window") as picker:
            for index in (0, 2):
                self.press_cell(SURFACE_OUTPUTS, index)
                advance(0.5)
                self.click_cell(SURFACE_OUTPUTS, index)
            picker.assert_not_called()

    def test_release_without_tick_opens_picker(self):
        self.state()
        advance = self.clock()
        with patch.object(template_panel, "show_item_list_window", return_value=True) as picker:
            self.press_cell(SURFACE_OUTPUTS, 1)
            advance(0.5)
            self.click_cell(SURFACE_OUTPUTS, 1)
            picker.assert_called_once()

    def test_multiple_recipes_use_shared_window_and_send_chosen_digest(self):
        self.state()
        request = self.view._on_product(self.view.revision, "minecraft:stick")
        result = self.result(request)
        result["payload"] = {
            "needs_choice": True, "revision": self.view.revision,
            "output": {"name": "minecraft:stick", "aux": 0},
            "candidates": [{"digest": "a"}, {"digest": "b"}],
        }
        with patch.object(template_panel, "RecipeChoiceWindow") as window:
            self.deliver(result)
            window.assert_called_once()
            window.call_args.args[3]("b")
        self.assertEqual("b", self.last_command()["recipe"])

    def test_stale_picker_callback_and_lost_session_do_not_write(self):
        self.state()
        self.assertIsNone(self.view._on_product(0, "minecraft:stick"))
        self.view.close()
        self.assertIsNone(self.view._on_product(1, "minecraft:stick"))
        self.assertEqual([], self.commands())

    def test_factory_outputs_keep_quantity_window(self):
        self.state(mode=MODE_FACTORY, outputs=((2, STICK, 4),))
        advance = self.clock()
        with patch.object(template_panel, "show_number_set_window", return_value=True) as quantity:
            with patch.object(template_panel, "show_item_list_window") as picker:
                self.press_cell(SURFACE_OUTPUTS, 2)
                advance(0.5)
                self.view.tick()
                quantity.assert_called_once()
                self.assertEqual(4, quantity.call_args.args[1])
                picker.assert_not_called()

    def test_configuration_change_closes_recipe_window(self):
        self.state()
        window = type("Window", (object,), {"close": lambda obj: setattr(obj, "closed", True)})()
        self.panel._vars[template_panel.RECIPE_WINDOW_ATTRIBUTE] = window
        self.state(revision=2)
        self.assertTrue(window.closed)


class TestTitleIsWrittenByScript(TemplateFixture):
    """标题由脚本 `SetText` 写（User 2026-10-02 定），**不走绑定**。"""

    #: 假的屏幕基底。真机上是 `tooldelta.ui.SCREEN_BASE_PATH`，而离线导不进
    #: 那个包（`utils.py` 顶部就 import 引擎），所以这里替成同一串。
    BASE = ("/variables_button_mappings_and_controls/safezone_screen_matrix/"
            "inner_matrix/safezone_screen_panel/root_screen_panel")

    def setUp(self):
        TemplateFixture.setUp(self)
        self.written = []
        control = ControlStub()
        control.SetText = self.written.append
        self.path = self.BASE + template_panel.TITLE_PATH
        self.screen.controls[self.path] = control
        self.panel._title_path = None
        self.panel._title_text = None
        # 离线拿不到引擎的 `SCREEN_BASE_PATH`，这里把它装回去——被钉住的正是
        # 「基底 + 后缀拼出来的那条路径能不能取到控件」。
        patcher = patch.object(template_panel, "title_control_path",
                               lambda: self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_writes_the_current_mode(self):
        self.state(mode=MODE_CRAFTING)
        self.panel._sync_title()
        self.assertEqual([u"样板配置 - 合成样板"], self.written)

    def test_it_writes_only_when_the_text_changes(self):
        """每帧写一次会让引擎每帧重排这个 label——与排序图标那条同一个理由。"""
        self.state(mode=MODE_CRAFTING)
        self.panel._sync_title()
        self.panel._sync_title()
        self.assertEqual(1, len(self.written), "没变就不该再写")
        self.state(mode=MODE_FACTORY, revision=2, locked_outputs=False)
        self.panel._sync_title()
        self.assertEqual([u"样板配置 - 合成样板", u"样板配置 - 工厂样板"],
                         self.written)

    def test_it_remembers_the_path_once_it_worked(self):
        self.state(mode=MODE_CRAFTING)
        self.panel._sync_title()
        self.assertEqual(self.path, self.panel._title_path)

    def test_an_unresolvable_path_writes_nothing_and_never_raises(self):
        """取不到控件的代价是**静默不写**（界面上看不出与「没接」的区别），
        因此这条只能靠「不抛」来钉。**空串是「试过了、放弃」的哨兵**：
        留着 `None` 会每帧重试、每帧抛一次异常。"""
        self.screen.controls.clear()
        self.state(mode=MODE_CRAFTING)
        self.panel._sync_title()
        self.assertEqual([], self.written)
        self.assertEqual("", self.panel._title_path)

    def test_a_given_up_path_is_not_retried(self):
        self.screen.controls.clear()
        self.state(mode=MODE_CRAFTING)
        self.panel._sync_title()
        self.panel._sync_title()
        self.assertEqual("", self.panel._title_path)

    def test_without_a_view_nothing_happens(self):
        self.panel.template = None
        self.panel._sync_title()
        self.assertEqual([], self.written)


class TestSourceSelectionCleared(TemplateFixture):
    """一次放入 = 一次来源选择用完（User 2026-10-02）。"""

    class FakeListView(object):
        def __init__(self):
            self.cleared = 0
            self.cache = type("Cache", (object,), {"model": self})()

        def clear_selection(self):
            self.cleared += 1

    def put_once(self):
        """选中一个背包槽，点一格虚格，返回那条命令的 `request_id`。"""
        self.select_backpack(0)
        self.state(source=((0, STONE, 3),))
        self.view.on_cell_click(SURFACE_SOURCE, 4)
        return self.commands()[-1]["request_id"]

    def test_the_backpack_selection_goes_away_after_the_receipt(self):
        view = self.FakeListView()
        self.panel.list_view = view
        self.put_once()
        self.assertEqual(1, len(self.commands()))
        request_id = self.commands()[-1]["request_id"]
        self.deliver(self.result(request_id))
        self.assertIsNone(self.backpack.selected)
        self.assertEqual(1, view.cleared, "网络那一侧的选中也一起放掉")

    def test_a_rejected_put_keeps_the_selection(self):
        """被拒的那一下不该把玩家的来源选择吃掉。"""
        view = self.FakeListView()
        self.panel.list_view = view
        self.put_once()
        request_id = self.commands()[-1]["request_id"]
        self.deliver(self.result(request_id, status=STATUS_REJECTED,
                                 reasons=[{"code": Reason.SLOT_OCCUPIED,
                                           "detail": ""}]))
        self.assertEqual(0, self.backpack.selected, "选中还在")
        self.assertEqual(0, view.cleared)

    def test_successful_clear_removes_selection_and_pending_presses(self):
        view = self.FakeListView()
        self.panel.list_view = view
        self.select_backpack(0)
        self.state(source=((0, STONE, 3),))
        self.view.selection = (SURFACE_SOURCE, 0)
        self.view.presses[SURFACE_SOURCE] = (0, 0.0, False)
        request_id = self.view.on_clear()
        self.deliver(self.result(request_id))
        self.assertIsNone(self.view.selection)
        self.assertEqual({}, self.view.presses)
        self.assertIsNone(self.backpack.selected)
        self.assertEqual(1, view.cleared)

    def test_rejected_clear_keeps_selection(self):
        self.state(source=((0, STONE, 3),))
        self.view.selection = (SURFACE_SOURCE, 0)
        request_id = self.view.on_clear()
        self.deliver(self.result(request_id, status=STATUS_REJECTED,
                                 reasons=[{"code": Reason.STALE_VERSION,
                                           "detail": ""}]))
        self.assertEqual((SURFACE_SOURCE, 0), self.view.selection)
        self.assertIsNotNone(self.view.cell(SURFACE_SOURCE, 0))

    def test_other_actions_do_not_clear_it(self):
        view = self.FakeListView()
        self.panel.list_view = view
        self.select_backpack(0)
        self.state()
        request_id = self.view.on_make()
        self.assertIsNotNone(request_id)
        self.deliver(self.result(request_id))
        self.assertEqual(0, self.backpack.selected)
        self.assertEqual(0, view.cleared)


class TestRealSlots(TemplateFixture):

    def test_putting_a_backpack_item_into_the_input_slot(self):
        self.select_backpack(0)
        self.state()
        self.binding("input_click")
        command = self.last_command()
        self.assertEqual("template_input_put", command["action"])
        self.assertEqual(9, command["slot"], "引擎槽号，不是界面下标")
        self.assertEqual(7, command["slot_version"])
        self.assertEqual(STONE.name, command["key"]["name"])

    def test_without_a_server_snapshot_the_put_waits_for_sync(self):
        self.select_backpack(0)
        self.client.inventory_slots = {}
        self.state()
        self.binding("input_click")
        self.assertEqual([], self.commands())

    def test_clicking_the_input_slot_without_a_selection_takes_it_back(self):
        self.state(input_rows=((0, FACTORY, 2),))
        self.binding("input_click")
        self.assertEqual("template_input_take", self.last_command()["action"])

    def test_clicking_the_result_slot_takes_it_back(self):
        self.state(result_rows=((0, FACTORY, 1),))
        self.binding("result_click")
        self.assertEqual("template_output_take", self.last_command()["action"])

    def test_the_selected_cell_wins_over_the_slot(self):
        """选中虚格时点真实槽也是「清空那一格」，不是取放。"""
        self.state(source=((0, STONE, 1),), result_rows=((0, FACTORY, 1),))
        self.view.selection = (SURFACE_SOURCE, 0)
        self.binding("result_click")
        self.assertEqual("template_cell_clear", self.last_command()["action"])

    def test_an_empty_slot_with_no_selection_sends_nothing(self):
        self.state()
        self.binding("input_click")
        self.binding("result_click")
        self.assertEqual([], self.commands())

    def test_the_result_slot_can_target_the_selected_backpack_slot(self):
        """`slot` = 目标背包槽，`key` = **客户端看到那一格装着什么**。

        两个方向都按这条报（放：那一格是来源；取：那一格是目标）。
        **要放进去的那一件不在这里给**——取回方向它只能来自设备，服务端
        自己知道是哪一件；照 `key` 插入会把目标格里那件无关物品复制一份、
        同时把设备里的样板扣掉（见 `_template_slot_take` 的换锚）。
        """
        self.select_backpack(0)
        self.state(result_rows=((0, FACTORY, 1),))
        self.binding("result_click")
        command = self.last_command()
        self.assertEqual("template_output_take", command["action"])
        self.assertEqual(9, command["slot"])
        self.assertEqual(STONE.name, command["key"]["name"])

    def test_the_real_slot_cells_read_the_same_bindings(self):
        self.state(input_rows=((0, FACTORY, 3),),
                   result_rows=((0, FACTORY, 1),))
        self.assertEqual(FACTORY.name,
                         self.view.cell(SURFACE_INPUT, 0)["name"])
        self.assertEqual("3", self.cell(SURFACE_INPUT, "item_count", 0))
        self.assertEqual("", self.cell(SURFACE_RESULT, "item_count", 0))


class TestModeAndMake(TemplateFixture):

    def test_re_choosing_the_current_mode_sends_nothing(self):
        self.state(mode=MODE_CRAFTING)
        self.binding("mode_crafting")
        self.assertEqual([], self.commands())

    def test_switching_the_mode_sends_the_new_one(self):
        self.state(mode=MODE_CRAFTING)
        self.binding("mode_factory")
        command = self.last_command()
        self.assertEqual("template_mode", command["action"])
        self.assertEqual(MODE_FACTORY, command["target"])

    def test_the_clear_button_sends_one_atomic_command(self):
        self.state(source=((0, STONE, 1),), revision=6)
        self.binding("clear")
        self.assertEqual(1, len(self.commands()))
        self.assertEqual("template_clear", self.last_command()["action"])
        self.assertEqual(6, self.last_command()["slot_version"])
        self.assertIsNotNone(self.view.cell(SURFACE_SOURCE, 0))

    def test_the_make_button_sends_exactly_one_encode(self):
        self.state()
        self.binding("make")
        command = self.last_command()
        self.assertEqual("template_encode", command["action"])
        self.assertEqual(1, len(self.commands()))

    def test_without_a_session_nothing_is_sent(self):
        self.state()
        self.client.drop_session()
        self.binding("mode_factory")
        self.binding("make")
        self.binding("clear")
        self.assertEqual([], self.commands())


class TestConfigRevision(TemplateFixture):
    """配置编辑带的是**最后一次收到的修订号**，不做乐观推进。"""

    def test_every_config_action_carries_the_pushed_revision(self):
        self.select_backpack(0)
        self.state(source=((0, STONE, 1),), revision=6)
        for name in ("mode_factory", "make", "clear"):
            self.binding(name)
        self.click_cell(SURFACE_SOURCE, 4)
        for command in self.commands():
            self.assertEqual(6, command["slot_version"], command["action"])

    def test_the_real_slot_actions_carry_the_backpack_anchor_instead(self):
        self.select_backpack(0)
        self.state(revision=6, result_rows=((0, FACTORY, 1),))
        self.binding("result_click")
        self.assertEqual(7, self.last_command()["slot_version"],
                         "背包槽锚的版本，不是配置修订号")


class TestFailures(TemplateFixture):

    def test_the_known_reasons_are_distinct(self):
        codes = [Reason.TEMPLATE_CELL_LOCKED, Reason.TEMPLATE_CONFIG_INVALID,
                 Reason.SLOT_OCCUPIED, Reason.NO_CAPACITY, Reason.OUT_OF_RANGE,
                 Reason.STALE_VERSION, Reason.PATTERN_KIND_MISMATCH,
                 Reason.PATTERN_NOT_A_TEMPLATE, Reason.INSUFFICIENT_STOCK]
        texts = [template_panel.REASON_TEXTS[code] for code in codes]
        self.assertEqual(len(codes), len(set(texts)))

    def test_an_unknown_reason_says_nothing_rather_than_showing_a_code(self):
        self.assertEqual(u"", TemplateView._error_text(
            {"reasons": [{"code": "no_such_reason", "detail": ""}]}))
        self.assertEqual(u"", TemplateView._error_text({}))

    def test_a_receipt_we_did_not_send_is_ignored(self):
        self.state()
        self.view.last_error = None
        self.deliver(self.result("someone-else",
                                 status=STATUS_REJECTED,
                                 reasons=[{"code": Reason.SLOT_OCCUPIED,
                                           "detail": ""}]))
        self.assertIsNone(self.view.last_error)

    def test_a_rejection_lands_in_last_error(self):
        self.state()
        request = self.view.on_make()
        self.deliver(self.result(request, status=STATUS_REJECTED,
                                 reasons=[{"code": Reason.SLOT_OCCUPIED,
                                           "detail": ""}]))
        self.assertEqual(template_panel.REASON_TEXTS[Reason.SLOT_OCCUPIED],
                         self.view.last_error)

    def test_a_success_clears_it(self):
        self.state()
        first = self.view.on_make()
        self.deliver(self.result(first, status=STATUS_REJECTED,
                                 reasons=[{"code": Reason.OUT_OF_RANGE,
                                           "detail": ""}]))
        second = self.view.on_make()
        self.deliver(self.result(second))
        self.assertIsNone(self.view.last_error)

    def test_losing_the_session_drops_the_selection_and_the_requests(self):
        self.state(source=((0, STONE, 1),))
        self.view.on_make()
        self.view.selection = (SURFACE_SOURCE, 0)
        self.client.drop_session()
        self.view.tick()
        self.assertIsNone(self.view.selection)
        self.assertEqual(0, self.view.diagnostics()["in_flight"])


class TestPanelEntry(unittest.TestCase):

    def make(self, params):
        """建面板。**把 `NetworkClient` 换成带离线端口的那个**——端口要自己
        回 `session_ready`，而真端口的 `classes()` 会去摸引擎事件模块。"""
        screen = Screen()
        port = RecordingPort()
        with patch.object(template_panel, "NetworkClient",
                          lambda: NetworkClient(port=port)):
            panel = template_panel.TemplatePanel("main", screen, params)
            panel.OnCreate()
        return panel, screen, port

    @staticmethod
    def ready(panel):
        client = panel.template.client
        client.receive(SyncEnvelope(
            SessionReady(7, SNAPSHOT_VERSION), "s1", "n1", "e1").to_dict())
        return client

    def test_no_device_ref_means_no_session_and_no_view(self):
        panel, _screen, port = self.make({})
        self.assertIsNone(panel.template)
        self.assertIsNone(panel.config)
        self.assertIsNone(panel.list_view)
        self.assertEqual([], port.sent)

    def test_a_device_ref_opens_the_session_and_wires_both_halves(self):
        panel, _screen, port = self.make({"device_ref": REF.to_dict(),
                                          "dimension": 0})
        self.addCleanup(panel.OnDestroy)
        self.assertIsNotNone(panel.template)
        self.assertIs(panel.config, panel.template)
        self.assertIsNotNone(panel.list_view)
        # 收包在开会话**之前**就挂上了：`session_ready` 与第一份
        # `template_state` 都可能比 `OnCreate` 返回还早。
        self.assertEqual(1, port.listens)
        # 三条回调：设备状态一条、`template_state` 一条，外加**下单预览一条**
        # （11 增量：预览窗与列表同寿命，`TerminalListView.__init__` 里挂上）。
        self.assertEqual(3, len(panel.template.client._handlers))

    def test_a_broken_device_ref_pauses_instead_of_opening_a_wrong_session(self):
        panel, _screen, _port = self.make({"device_ref": {"nonsense": 1}})
        self.assertIsNone(panel.template)
        self.assertIsNone(panel.list_view)

    def test_destroy_closes_the_view_and_clears_the_hook(self):
        panel, _screen, _port = self.make({"device_ref": REF.to_dict(),
                                           "dimension": 0})
        panel.OnDestroy()
        self.assertIsNone(panel.template)
        self.assertIsNone(panel.config)
        self.assertIsNone(panel.list_view)

    def test_destroy_closes_an_open_quantity_window(self):
        panel, _screen, _port = self.make({"device_ref": REF.to_dict(),
                                           "dimension": 0})
        closed = []

        class Window(object):
            def close(self):
                closed.append(True)

        panel._vars["_endernet_number_set_window"] = Window()
        panel.OnDestroy()
        self.assertEqual([True], closed)

    def test_the_template_handler_drops_another_sessions_message(self):
        panel, _screen, _port = self.make({"device_ref": REF.to_dict(),
                                           "dimension": 0})
        client = self.ready(panel)
        envelope = SyncEnvelope(TemplateStateMessage(
            3, MODE_CRAFTING, rows(9), rows(3, locked=True), rows(1), rows(1),
            LIMITS), "other", "n1", "e1")
        panel._make_template_handler(client)(envelope, envelope.message)
        self.assertEqual(-1, panel.template.revision)

    def test_the_template_handler_applies_its_own_sessions_message(self):
        panel, _screen, _port = self.make({"device_ref": REF.to_dict(),
                                           "dimension": 0})
        client = self.ready(panel)
        envelope = SyncEnvelope(TemplateStateMessage(
            3, MODE_FACTORY, rows(9), rows(3), rows(1), rows(1), LIMITS),
            client.session_id, client.network_id, client.epoch)
        panel._make_template_handler(client)(envelope, envelope.message)
        self.assertEqual(3, panel.template.revision)
        self.assertEqual(MODE_FACTORY, panel.template.mode)

    def test_every_binding_answers_with_a_safe_value_before_oncreate(self):
        """绑定解析早于 `OnCreate`，而那时 `self.template` 还是 `None`。"""
        screen = Screen()
        panel = template_panel.TemplatePanel("main", screen, {})
        for surface in SURFACE_CELLS:
            for field in ("visible", "item_id_aux", "item_count", "selected"):
                key = (TemplateFixture.FLAGS[field], COLLECTION_OF[surface],
                       BINDING_PREFIX + surface + "_" + field)
                value = screen.collection_bindings[key](0)
                self.assertIn(value, (True, False, 0, ""))
        # 标题那条绑定已经撤了（改由 `_sync_title` 直接写文本），因此这里
        # 只剩两个模式选中框要答。
        self.assertNotIn((BinderStub.BF_BindString, BINDING_PREFIX + "title"),
                         screen.bindings)
        self.assertFalse(screen.bindings[
            (BinderStub.BF_BindBool,
             BINDING_PREFIX + "mode_crafting_selected")]())


class TestRegistration(unittest.TestCase):

    def test_the_decorator_key_matches_the_block_mapping(self):
        self.assertEqual(
            template_panel.TemplatePanel._screen_key,
            TERMINAL_SCREENS.get("endernet:template_terminal_panel"))

    def test_register_screens_includes_the_template_panel(self):
        from endernet_bp.endernet_scripts.endernet.client.ui import (
            register_screens,
        )
        registered = register_screens()
        self.assertIn(template_panel.TemplatePanel, registered)
        self.assertEqual(
            registered[:2],
            (terminal_panel.CommonTerminalPanel,
             __import__("endernet_bp.endernet_scripts.endernet.client.ui."
                        "crafting_panel", fromlist=["CraftingPanel"]
                        ).CraftingPanel),
            "前两个位置是钉住的，新的必须插在后面")


class TestJsonContract(unittest.TestCase):
    """界面 JSON 与面板常量的接线。**只验名字对得上**，控件画得对不对
    是游戏内实测项。"""

    @classmethod
    def setUpClass(cls):
        cls.panel = cls.load("template_terminal_panel.json")
        cls.common_panel = cls.load("common_terminal_panel.json")
        cls.windows = cls.load("endernet_windows.json")

    @staticmethod
    def load(name):
        path = os.path.join(ROOT, "endernet_rp", "ui", name)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def controls(self, node):
        return {key.split("@")[0]: value
                for entry in node["controls"] for key, value in entry.items()}

    def content(self):
        return self.controls(self.panel["content"])

    def wrapper_overrides(self, name):
        return set(key.split("|")[0] for key in self.panel[name]
                   if key.startswith("$"))

    def shared_variables(self):
        return set(key.split("|")[0]
                   for key in self.windows["item_can_be_selected"]
                   if key.startswith("$"))

    def every_control(self, node):
        """深度遍历，产出每一个控件节点。"""
        for entry in node.get("controls", []):
            for key, value in entry.items():
                yield key, value
                for item in self.every_control(value):
                    yield item

    def test_every_wrapper_overrides_every_variable_the_shared_template_uses(self):
        """**六个都要覆盖**：漏一个就会静默沿用 11-09 的默认值
        （`endernet_item_list` 与 `#EnderNetItemList.*`），而那种格子会去读
        另一个窗口的集合——格子在、图也有，只是永远为空。"""
        declared = self.shared_variables()
        for name in ("template_source_cell@endernet_windows.item_can_be_selected",
                     "template_outputs_cell@endernet_windows.item_can_be_selected",
                     "template_input_cell@endernet_windows.item_can_be_selected",
                     "template_result_cell@endernet_windows.item_can_be_selected",
                     "terminal_slot@endernet_windows.item_can_be_selected"):
            self.assertEqual(self.wrapper_overrides(name), declared, name)

    def test_the_four_wrappers_point_at_their_own_collection_and_bindings(self):
        for surface in (SURFACE_SOURCE, SURFACE_OUTPUTS, SURFACE_INPUT,
                        SURFACE_RESULT):
            wrapper = self.panel[
                "template_%s_cell@endernet_windows.item_can_be_selected" % surface]
            self.assertEqual(wrapper["$item_collection_name"],
                             COLLECTION_OF[surface], surface)
            for variable, suffix in (
                ("$item_visible_binding", "visible"),
                ("$item_id_aux_binding", "item_id_aux"),
                ("$item_selected_binding", "selected"),
                ("$item_count_binding", "item_count"),
                ("$item_select_button", "click"),
            ):
                self.assertEqual(
                    wrapper[variable],
                    template_panel.BINDING_PREFIX + surface + "_" + suffix,
                    "%s %s" % (surface, variable))

    def test_the_four_collection_names_match_the_python_constants(self):
        self.assertEqual(set(COLLECTION_OF.values()),
                         {"endernet_template_source", "endernet_template_outputs",
                          "endernet_template_input", "endernet_template_result"})

    def test_the_four_grid_controls_use_those_wrappers(self):
        content = self.content()
        panel = self.controls(content["template_panel"])
        self.assertEqual(panel["source_grid"]["collection_name"],
                         COLLECTION_OF[SURFACE_SOURCE])
        self.assertEqual(panel["source_grid"]["grid_item_template"],
                         "template_terminal_panel_ui.template_source_cell")
        self.assertEqual(panel["outputs_grid"]["collection_name"],
                         COLLECTION_OF[SURFACE_OUTPUTS])
        self.assertEqual(panel["outputs_grid"]["grid_item_template"],
                         "template_terminal_panel_ui.template_outputs_cell")
        for name, surface in (("template_input_slot", SURFACE_INPUT),
                              ("template_output_slot", SURFACE_RESULT)):
            grid = panel[name]
            self.assertEqual("grid", grid["type"], name)
            self.assertEqual([1, 1], grid["grid_dimensions"], name)
            self.assertEqual(grid["collection_name"], COLLECTION_OF[surface])
            self.assertEqual(
                grid["grid_item_template"],
                "template_terminal_panel_ui.template_%s_cell" % surface)
            # 几何必须原样保留（交接第 3 节记的偏移）。
            self.assertEqual("top_right", grid["anchor_from"], name)

    def test_the_network_list_is_the_same_wiring_as_the_common_terminal(self):
        """本面板持有**自己的一份**薄包装，不指向 `common_terminal_panel_ui.*`
        ——跨面板耦合会让改一个界面动到另一个。格子那一份要与通用终端逐字段
        相同，否则「同一个列表」在两个界面上会有两种行为。"""
        ours = self.content()
        self.assertEqual(
            ours["terminal_items_scroll"]["$scrolling_content"],
            "template_terminal_panel_ui.terminal_items_grid")
        self.assertEqual(
            ours["terminal_items_scroll"]["$scroll_background_image_control"],
            "template_terminal_panel_ui.terminal_scroll_background")
        common_grid = self.common_panel["terminal_items_grid"]
        ours_grid = dict(self.panel["terminal_items_grid"])
        ours_grid["grid_item_template"] = common_grid["grid_item_template"]
        self.assertEqual(common_grid, ours_grid)
        grid = self.panel["terminal_items_grid"]
        self.assertEqual("terminal_slots", grid["collection_name"])
        self.assertEqual([9, 2], grid["grid_dimensions"])
        self.assertEqual(grid["grid_item_template"],
                         "template_terminal_panel_ui.terminal_slot")
        self.assertEqual([{"binding_condition": "always",
                           "binding_name": "#EnderNetTerminal.count",
                           "binding_name_override": "#maximum_grid_items",
                           "binding_type": "global"}], grid["bindings"])

    def test_the_scroll_background_is_an_invisible_button_wired_to_quick_store(self):
        node = self.panel[
            "terminal_scroll_background@endernet_ui_common.simple_invisible_button"]
        self.assertEqual("#EnderNetTerminal.quick_store",
                         node["$pressed_button_name"])
        self.assertTrue(node["button_mappings"], "光有名字不够，要有映射")

    def test_the_search_box_sort_buttons_and_mask_point_at_the_right_bindings(self):
        content = self.content()
        mask = content["unavailable_mask"]
        self.assertEqual([{"binding_type": "global",
                           "binding_name": "#EnderNetTerminal.unavailable",
                           "binding_name_override": "#visible",
                           "binding_condition": "always"}], mask["bindings"])
        self.assertEqual({"#EnderNetTerminal.unavailable": False},
                         mask["property_bag"])
        mask_img = self.controls(mask)["mask_img"]
        self.assertEqual("top_left", mask_img["anchor_from"])
        self.assertEqual("top_left", mask_img["anchor_to"])
        self.assertEqual("#EnderNetTerminal.sort_method",
                         content["sort_method_btn"]["$pressed_button_name"])
        self.assertEqual("#EnderNetTerminal.sort_order",
                         content["sort_order_btn"]["$pressed_button_name"])
        self.assertEqual("#EnderNetTerminal.close",
                         content["close"]["$pressed_button_name"])

    def test_the_title_carries_a_literal_and_no_binding(self):
        """标题**不走绑定**，由脚本 `SetText` 写（User 2026-10-02 定）。

        这条钉的是「没有绑定」这件事本身：留着一条没人读的绑定，下一个人
        会以为它在起作用，而真机上文字是脚本写的那一份——两处都改才是对的，
        只改一处则哪一次都看不出问题。
        """
        panel = self.controls(self.content()["template_panel"])
        self.assertEqual(u"样板配置 - 合成样板", panel["title"]["text"])
        self.assertNotIn("bindings", panel["title"])
        self.assertNotIn("#EnderNetTemplate.title",
                         json.dumps(self.panel, ensure_ascii=False))

    def test_the_title_label_is_above_the_grid(self):
        """标题的 `layer` 要压过九宫格（`layer 1`）——不压住就会画在网格底下，
        那正是 2026-10-02 真机上「文字看不见」的直接原因之一。

        **颜色与 anchor 不在断言里**：那是 User 的设计取值，他们会按观感调
        （当天就调过一次）。把设计取值钉成正确性，只会让每次微调都红一次。
        """
        panel = self.controls(self.content()["template_panel"])
        self.assertGreater(panel["title"]["layer"], 1)

    def test_the_three_panel_buttons_are_wired_with_their_mappings(self):
        panel = self.controls(self.content()["template_panel"])
        for name, pressed in (("crafting_template_mode_radio_btn",
                               "#EnderNetTemplate.mode_crafting"),
                              ("factoric_template_mode_radio_btn",
                               "#EnderNetTemplate.mode_factory"),
                              ("make_template_btn", "#EnderNetTemplate.make"),
                              ("clear_btn", "#EnderNetTemplate.clear")):
            self.assertEqual(pressed, panel[name]["$pressed_button_name"], name)
            self.assertTrue(panel[name]["button_mappings"], name)

    def test_the_two_selected_frames_are_bound_not_static(self):
        panel = self.controls(self.content()["template_panel"])
        for name, binding in (("crafting_template_mode_radio_btn",
                               "#EnderNetTemplate.mode_crafting_selected"),
                              ("factoric_template_mode_radio_btn",
                               "#EnderNetTemplate.mode_factory_selected")):
            frame = self.controls(panel[name])["selected_frame"]
            self.assertNotIn("visible", frame, "显隐由推送决定，不留静态初值")
            self.assertEqual(binding, frame["bindings"][0]["binding_name"], name)

    def test_the_flying_item_is_anchored_where_the_script_measures_it(self):
        item = self.content()["flying_item"]
        self.assertEqual("top_left", item["anchor_from"])
        self.assertEqual("top_left", item["anchor_to"])

    def test_the_detail_popup_is_instantiated(self):
        popup = self.content()["device_status_popup"]
        self.assertEqual("#EnderNetDeviceStatus.popup_visible",
                         popup["bindings"][0]["binding_name"])
        self.assertEqual("#visible", popup["bindings"][0]["binding_name_override"])

    def test_every_local_template_reference_resolves(self):
        """本文件里每一处**指向本命名空间**的模板引用，都必须真的定义在这个文件里。

        **这一条是补的，而且是被真机打出来的**：上一版契约用例在两处各断言
        了一种拼法——一处说包装叫 `template_outputs_cell`、另一处说
        `outputs_grid` 指向 `template_output_cell`——**两个断言互相抵消**，
        于是谁也没拦住。真机上的症状是那一格报
        `UI control reference not found: 'template_output_cell'`：
        界面照常渲染，只有日志里有。

        现在从**引用**出发反查定义：拼法对不上就红。
        """
        defined = set(key.split("@")[0] for key in self.panel)
        fields = ("grid_item_template", "$scrolling_content",
                  "$scroll_background_image_control",
                  "$scroll_box_mouse_image_control",
                  "$scroll_box_touch_image_control",
                  "$scroll_track_image_control")
        prefix = "template_terminal_panel_ui."
        references = []
        missing = []
        for name, node in self.every_control(self.panel["content"]):
            for field in fields:
                value = node.get(field)
                if not isinstance(value, str) or not value.startswith(prefix):
                    continue
                references.append(value)
                if value[len(prefix):] not in defined:
                    missing.append((name, field, value))
        self.assertEqual([], missing, "引用了本文件里没有定义的模板")
        # 至少要扫到那几处，否则这条用例是一句空话。
        for expected in ("template_terminal_panel_ui.template_source_cell",
                         "template_terminal_panel_ui.template_outputs_cell",
                         "template_terminal_panel_ui.template_input_cell",
                         "template_terminal_panel_ui.template_result_cell",
                         "template_terminal_panel_ui.terminal_items_grid",
                         "template_terminal_panel_ui.terminal_scroll_background"):
            self.assertIn(expected, references, expected)

    def python_collection(self, surface):
        """Python 侧那条集合绑定登记的集合名。"""
        method = getattr(template_panel.TemplatePanel, surface + "_visible")
        return method.collection_name

    def test_every_grid_collection_agrees_with_its_wrapper_and_the_python_side(self):
        """**三处必须写同一个集合名。**

        1. grid 的 `collection_name`；
        2. 薄包装的 `$item_collection_name`；
        3. Python 侧 `binding_collection` 登记的集合名。

        对不上的症状是**九格一起亮**：引擎拿不到每格的 `#collection_index`，
        每一格于是读同一个下标——点哪一格都像点了第 0 格。

        **这一条是真机打出来的**（2026-10-02）：`source_grid` 的
        `collection_name` 当时写的还是交接文档给的 `crafting_grid`，另外两处
        已经是 `endernet_template_source`。集合名不是标签，它是引擎路由回调
        用的键，所以**文档给的名字也要按能不能接上去改**。
        """
        panel = self.controls(self.content()["template_panel"])
        virtual = (("source_grid", SURFACE_SOURCE, "template_source_cell"),
                   ("outputs_grid", SURFACE_OUTPUTS, "template_outputs_cell"))
        for grid_name, surface, wrapper_name in virtual:
            grid = panel[grid_name]
            wrapper = self.panel[
                wrapper_name + "@endernet_windows.item_can_be_selected"]
            self.assertEqual(COLLECTION_OF[surface],
                             grid["collection_name"], grid_name)
            self.assertEqual(COLLECTION_OF[surface],
                             wrapper["$item_collection_name"], grid_name)
            self.assertEqual(COLLECTION_OF[surface],
                             self.python_collection(surface), grid_name)
        for grid_name, surface in (("template_input_slot", SURFACE_INPUT),
                                   ("template_output_slot", SURFACE_RESULT)):
            grid = panel[grid_name]
            self.assertEqual(COLLECTION_OF[surface],
                             grid["collection_name"], grid_name)
            self.assertEqual(COLLECTION_OF[surface],
                             self.python_collection(surface), grid_name)

    def test_the_old_display_only_template_is_gone(self):
        self.assertNotIn("template_item_slot", self.panel)

    def test_the_network_collection_name_no_longer_collides(self):
        """`test_grid` 曾同时是网络列表与输出格的集合名——两张 grid 读同一份
        数据，而界面上完全看不出来。"""
        raw = json.dumps(self.panel)
        self.assertNotIn("test_grid", raw)

    def test_every_real_pressed_name_in_this_file_has_a_button_mapping(self):
        """只写名字不写映射时，点击是**安静地什么都不做**（2026-10-01 实测）。"""
        missing = []
        for name, node in self.every_control(self.panel["content"]):
            pressed = node.get("$pressed_button_name")
            if not isinstance(pressed, str) or not pressed.startswith("#EnderNet"):
                continue
            if "__" in name:
                continue
            mappings = node.get("button_mappings") or []
            if not any(m.get("to_button_id") == "$pressed_button_name"
                       for m in mappings):
                missing.append((name, pressed))
        self.assertEqual([], missing)


if __name__ == "__main__":
    unittest.main()
