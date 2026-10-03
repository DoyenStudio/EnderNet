# coding=utf-8
"""工厂终端（11-06）的客户端半边：订单视图模型、绑定与接线。

守的是**界面与协议之间的那层缝**，缝里的错在游戏里几乎都看不出来：

- 订单行按**下标**去认单——列表中途增删（一单结掉、又来一单）之后，
  「取消」打到的是另一个订单；
- 详情窗在那一单消失之后还开着——停在一份永远不会再更新的旧画面上，
  看起来像界面卡住；
- 总量为 0 时算比例——除零，或者显示一个虚假的 100%；
- 回执没人取——`_in_flight` 只涨不落，表现为「取消按过一次之后再也发不出
  命令」；
- 遮罩判据写成另外三台终端那一支（列表缺失就不遮）——本界面没有本地内容
  可画，会话没了就是一屏死数据。

界面的另一半（控件画没画出来、`#clip_ratio` / `#color` 绑在集合生成的
行上能不能生效、右键能否打开）离线测不了，属游戏内实测项。

**替身从 `test_crafting_panel` 借**（与 `test_template_panel` 同一条理由：
`tooldelta` 的三份替身必须与别的测试文件取值一致）。
"""

import json
import os
import unittest

import testsupport  # noqa: F401  设置 sys.path

from test_crafting_panel import (  # noqa: F401
    BinderStub,
    ControlStub,
    RecordingPort,
    ROOT,
    Screen,
)

from endernet_bp.endernet_scripts.endernet.client.network_client import (
    NetworkClient,
)
from endernet_bp.endernet_scripts.endernet.client.ui import (
    factoric_panel,
    stack_size,
)
from endernet_bp.endernet_scripts.endernet.client.ui.factoric_panel import (
    ACTION_CANCEL,
    BINDING_PREFIX,
    COLOR_DONE,
    COLOR_RUNNING,
    COLOR_WAITING,
    ORDER_COLLECTION,
    SOURCE_COLLECTION,
    FactoricView,
)
from endernet_bp.endernet_scripts.endernet.common.definitions import (
    TERMINAL_SCREENS,
)
from endernet_bp.endernet_scripts.endernet.common.protocol import (
    MSG_ORDER_STATE,
    ORDER_SOURCE_FEED_DONE,
    ORDER_SOURCE_FEED_WAITING_INPUTS,
    ORDER_SOURCE_FEED_WAITING_OUTPUTS,
    ORDER_SOURCE_STAGE_DONE,
    ORDER_SOURCE_STAGE_RUNNING,
    ORDER_SOURCE_STAGE_WAITING,
    STATUS_OK,
    STATUS_REJECTED,
    BlockReason,
    CommandResult,
    CommandResultMessage,
    OrderStateMessage,
    Reason,
    SessionReady,
    SyncEnvelope,
    VIEW_ORDER,
)

SCREEN_NAME = "factoric_terminal_panel_ui.main"
DEVICE_ID = "endernet:factoric_terminal_panel"

ORDER_ID = "order@net-1/master-1/1"
OTHER_ID = "order@net-1/master-1/2"


def order_row(order_id=ORDER_ID, name="minecraft:stone", aux=0, amount=64,
              status="running", completed=1, total=4, memory=1024):
    return {
        "order_id": order_id,
        "canonical": "minecraft:stone|0",
        "name": name,
        "aux": aux,
        "amount": amount,
        "status": status,
        "tasks_completed": completed,
        "tasks_total": total,
        "memory_bytes": memory,
    }


def source_row(order_id=ORDER_ID, name="minecraft:stone", aux=0, stage=1,
               feed=1, done=16, total=64):
    return {
        "order_id": order_id,
        "canonical": "minecraft:stone|0",
        "name": name,
        "aux": aux,
        "stage": stage,
        "feed": feed,
        "done": done,
        "total": total,
    }


def state(orders=None, sources=None, capacity=4096, available=3072, used=1024,
          slots=4):
    if orders is None:
        orders = [order_row()]
    if sources is None:
        sources = [source_row()]
    return OrderStateMessage(
        {"capacity": capacity, "used": used, "available": available,
         "slots": slots},
        orders,
        sources,
    )


class FactoricFixture(unittest.TestCase):
    """一块屏 + 一个视图 + 一条真会话。**没有背包、没有列表视图**——
    那正是本界面与另外三台终端的不同之处。"""

    def setUp(self):
        # `stack_size` 的缓存挂在模块上，跨用例存活，必须每条擦一次。
        self.addCleanup(stack_size.reset)
        stack_size.reset()
        factoric_panel.reset_label_cache()
        self.port = RecordingPort()
        self.client = NetworkClient(port=self.port, view_kind=VIEW_ORDER)
        self.screen = Screen()
        # 面板是**绑定的登记处**（写在类体里），视图只是被转发的那一头。
        self.panel = factoric_panel.FactoricPanel("main", self.screen, {})
        self.panel._activated = True
        self.view = FactoricView(self.panel, client=self.client)
        self.panel.factoric = self.view
        self.client.on_message(self.panel._make_order_handler(self.client))
        self.addCleanup(self.view.close)
        self.feed(SessionReady(7, 1))

    # -- 收发 ----------------------------------------------------------

    def feed(self, message, session_id="s1", network_id="n1", epoch="e1"):
        return self.client.receive(
            SyncEnvelope(message, session_id, network_id, epoch).to_dict())

    def apply(self, message=None, **kwargs):
        message = state(**kwargs) if message is None else message
        self.feed(message)
        return message

    def receipt(self, request_id, status=STATUS_OK):
        """一条回执。**形状照 `CommandResult.to_dict()` 原样**——协议层只验
        形状、不重建领域对象（`CommandResult` 没有 `from_dict`）。"""
        if status == STATUS_OK:
            result = CommandResult.ok(request_id, {"action": "order_cancel"})
        else:
            result = CommandResult.rejected(
                request_id, [BlockReason(Reason.ORDER_UNKNOWN, u"没有这一单")])
        return CommandResultMessage(request_id, result.to_dict())


class TestApply(FactoricFixture):
    """`apply` 是**整份替换**：服务端发来的每一条都是「现在的真相」。"""

    def test_a_state_replaces_every_table_at_once(self):
        self.apply()
        self.assertEqual(1, self.view.order_count())
        self.assertEqual(3072, self.view.memory["available"])
        self.assertEqual(4, self.view.memory["slots"])

    def test_a_later_state_replaces_the_earlier_one_wholesale(self):
        """没有版本号可比：后到的那一份**整个**盖掉前一份。

        与物品增量不同——那边两份要按版本合并，这边收到即是全量，
        因此「旧包乱序到达」不会留下一半旧一半新的画面。
        """
        self.apply()
        self.apply(orders=[order_row(order_id=OTHER_ID, amount=8)],
                   sources=[source_row(order_id=OTHER_ID)])
        self.assertEqual(1, self.view.order_count())
        self.assertEqual(OTHER_ID, self.view.order_id(0))
        self.assertEqual(8, self.view.order_row(0)["amount"])

    def test_an_empty_state_is_a_real_answer_not_a_missing_one(self):
        """「一单都没有」与「还没收到」是两份不同的画面：前者内存照常显示。"""
        self.apply(orders=[], sources=[])
        self.assertEqual(0, self.view.order_count())
        self.assertEqual(u"剩余可用内存 3072 B", self.view.memory_text())

    def test_a_bad_row_shape_is_ignored_not_fatal(self):
        """坏行只会少画一行，不会把整块界面炸掉（协议层已经校验过形状，
        这是第二道）。"""
        self.view.apply(_loose({"memory": {}, "orders": [None, 3], "sources": []}))
        self.assertEqual(0, self.view.order_count())


class TestOrderRows(FactoricFixture):

    def test_the_row_carries_the_stable_order_id_not_the_index(self):
        """**点的是哪一单，绑的就是哪一单的 ID**，不是显示下标。"""
        self.apply(orders=[order_row(order_id=OTHER_ID)])
        self.assertEqual(OTHER_ID, self.view.order_id(0))
        self.assertEqual("", self.view.order_id(7))

    def test_a_boolean_index_is_not_an_index(self):
        self.apply()
        for bad in (True, False, -1, 9, "0"):
            self.assertIsNone(self.view.order_row(bad))

    def test_the_name_label_is_amount_times_display_name(self):
        self.apply(orders=[order_row(amount=999, name="minecraft:stone")])
        self.assertEqual(u"999 x minecraft:stone", self.view.order_name(0))

    def test_a_long_name_is_shown_as_is(self):
        """**本轮不截断**：截到几个字是玩法口径，交接第 8 节把「长文本处理」
        明确标着待确认（见 `factoric_panel` 模块里那段说明）。"""
        long_name = "minecraft:" + "x" * 40
        self.apply(orders=[order_row(name=long_name)])
        self.assertEqual(u"64 x " + long_name, self.view.order_name(0))

    def test_the_overall_progress_is_recipe_task_counts(self):
        """委托第 4 节：**已完成配方任务数 / 订单树配方任务总数**。"""
        self.apply(orders=[order_row(completed=3, total=10)])
        self.assertEqual(u"3/10", self.view.order_progress_text(0))
        self.assertAlmostEqual(0.3, self.view.order_progress(0))

    def test_the_icon_is_the_ones_the_engine_would_draw(self):
        self.apply(orders=[order_row(name="minecraft:stone", aux=0)])
        self.assertEqual(stack_size.id_aux("minecraft:stone"),
                         self.view.order_item_id_aux(0))

    def test_an_empty_table_paints_nothing(self):
        self.apply(orders=[])
        self.assertFalse(self.view.order_visible(0))
        self.assertEqual(u"", self.view.order_name(0))
        self.assertEqual(0, self.view.order_item_id_aux(0))


class TestSources(FactoricFixture):

    def test_the_tree_is_filtered_to_the_open_order(self):
        """详情窗只画**开着的那一单**的配方树；两张订单的树不能混在一起。"""
        self.apply(
            orders=[order_row(ORDER_ID), order_row(OTHER_ID)],
            sources=[source_row(ORDER_ID), source_row(OTHER_ID),
                     source_row(ORDER_ID, name="minecraft:stick")],
        )
        self.view.on_order_click(1)
        self.assertEqual(OTHER_ID, self.view.selected)
        self.assertEqual(1, self.view.source_count())

    def test_the_three_faces_of_the_status_text(self):
        """交接第 4 节的三要素：`阶段 已完成数/总数 投料/产物状态`。"""
        self.apply(sources=[
            source_row(stage=ORDER_SOURCE_STAGE_WAITING,
                       feed=ORDER_SOURCE_FEED_WAITING_INPUTS, done=0, total=10),
            source_row(stage=ORDER_SOURCE_STAGE_RUNNING,
                       feed=ORDER_SOURCE_FEED_WAITING_OUTPUTS, done=3, total=10),
            source_row(stage=ORDER_SOURCE_STAGE_DONE,
                       feed=ORDER_SOURCE_FEED_DONE, done=10, total=10),
        ])
        self.view.on_order_click(0)
        self.assertEqual(u"等待中 0/10 等待投料", self.view.source_stat(0))
        self.assertEqual(u"制作中 3/10 等待产物", self.view.source_stat(1))
        self.assertEqual(u"已完成 10/10 已完成", self.view.source_stat(2))

    def test_each_stage_gets_its_own_background_colour(self):
        self.apply(sources=[
            source_row(stage=ORDER_SOURCE_STAGE_WAITING),
            source_row(stage=ORDER_SOURCE_STAGE_RUNNING),
            source_row(stage=ORDER_SOURCE_STAGE_DONE),
        ])
        self.view.on_order_click(0)
        self.assertEqual(list(COLOR_WAITING), self.view.source_color(0))
        self.assertEqual(list(COLOR_RUNNING), self.view.source_color(1))
        self.assertEqual(list(COLOR_DONE), self.view.source_color(2))

    def test_a_zero_total_never_divides_by_zero(self):
        """交接第 4 节：总数为零时**不得除零，也不得显示虚假的 100%**。"""
        self.apply(orders=[order_row(completed=0, total=0)],
                   sources=[source_row(done=0, total=0)])
        self.view.on_order_click(0)
        self.assertEqual(0.0, self.view.source_progress(0))
        self.assertEqual(0.0, self.view.order_progress(0))
        self.assertEqual(u"0/0", self.view.order_progress_text(0))

    def test_the_tree_is_empty_without_an_open_order(self):
        self.apply()
        self.assertEqual(0, self.view.source_count())
        self.assertFalse(self.view.source_visible(0))


class TestDetailWindow(FactoricFixture):
    """详情窗**按需创建**（User 2026-10-03 定），建好留着、靠显隐开关。"""

    def window(self):
        return self.screen.controls.get(factoric_panel.DETAIL_NAME)

    def test_the_window_is_not_built_until_the_first_open(self):
        """不点开就不该有任何控件树——主面板上那个 180×200 的常驻实例已经去掉。"""
        self.apply()
        self.assertIsNone(self.view.detail_window())
        self.assertIsNone(self.window())

    def test_the_first_open_builds_it_hidden_then_shows_it(self):
        self.apply()
        self.view.on_order_click(0)
        window = self.window()
        self.assertIsNotNone(window)
        self.assertEqual(factoric_panel.DETAIL_DEFINITION, window.definition)
        self.assertEqual(factoric_panel.DETAIL_LAYER, window.layer)
        self.assertTrue(window.visible, "点开了就要看得见")

    def test_closing_hides_it_and_reopening_restores_visibility(self):
        """**不销毁重建**：再打开只是把可见性恢复回去（User 的原话）。"""
        self.apply()
        self.view.on_order_click(0)
        window = self.window()
        self.view.on_detail_close()
        self.assertFalse(window.visible)
        self.view.on_order_click(0)
        self.assertTrue(window.visible)
        self.assertIs(window, self.window(), "第二次打开不该再 AddElement")

    def test_closing_without_ever_opening_builds_nothing(self):
        """关一扇从没建过的窗不该为它造一棵控件树。"""
        self.apply()
        self.view.on_detail_close()
        self.assertIsNone(self.window())

    def test_clicking_a_row_opens_its_detail_and_the_close_button_only_closes(self):
        self.apply()
        self.assertFalse(self.view.detail_visible())
        self.view.on_order_click(0)
        self.assertTrue(self.view.detail_visible())
        self.assertEqual(ORDER_ID, self.view.detail_order_id())
        self.view.on_detail_close()
        self.assertFalse(self.view.detail_visible())
        self.assertEqual(1, self.view.order_count(), "关窗不动订单")

    def test_clicking_the_same_row_twice_does_not_close_it(self):
        self.apply()
        self.view.on_order_click(0)
        self.view.on_order_click(0)
        self.assertTrue(self.view.detail_visible())

    def test_the_detail_closes_when_its_order_disappears(self):
        """那一单结掉/被取消之后必须自动关窗：留着的话画面永远不再更新，
        看起来像界面卡住了。"""
        self.apply(orders=[order_row(ORDER_ID), order_row(OTHER_ID)],
                   sources=[source_row(ORDER_ID)])
        self.view.on_order_click(0)
        window = self.window()
        self.assertTrue(self.view.detail_visible())
        self.apply(orders=[order_row(OTHER_ID)], sources=[])
        self.assertFalse(self.view.detail_visible())
        self.assertFalse(window.visible, "自动关窗也要真的藏起来")

    def test_a_broken_add_element_does_not_break_the_order_list(self):
        """建不出来时**订单列表照常工作**，只是点不开详情——而且要只失败一次，
        不然每次点击都会再抛一遍。"""
        attempts = []

        def broken(definition, name):
            attempts.append(name)
            raise ValueError("no such control definition")

        self.screen.AddElement = broken
        self.apply()
        self.view.on_order_click(0)
        self.view.on_order_click(0)
        self.assertEqual(1, len(attempts), "失败过就不再重试")
        self.assertIsNone(self.view.detail_window())
        self.assertEqual(1, self.view.order_count())

    def test_the_title_names_the_order(self):
        self.apply()
        self.view.on_order_click(0)
        self.assertIn(u"minecraft:stone", self.view.detail_title())

    def test_closing_the_panel_takes_the_detail_window_down_first(self):
        self.apply()
        self.view.on_order_click(0)
        window = self.window()
        self.view.close()
        self.assertFalse(window.visible)
        self.assertIsNone(self.view.detail_window())


class TestCancel(FactoricFixture):
    """取消订单：**一条 `order_cancel` 命令，带那一单的稳定 ID**。"""

    def test_cancel_sends_the_order_id_not_the_index(self):
        self.apply(orders=[order_row(order_id=OTHER_ID)])
        self.view.on_order_click(0)
        self.view.on_cancel()
        self.assertEqual(1, len(self.port.sent))
        message = self.port.sent[0]["message"]
        self.assertEqual(MSG_ORDER_STATE and "command", message["kind"])
        self.assertEqual(ACTION_CANCEL, message["action"])
        self.assertEqual(OTHER_ID, message["order"])

    def test_cancel_without_an_open_order_sends_nothing(self):
        self.apply()
        self.view.on_cancel()
        self.assertEqual([], self.port.sent)

    def test_cancel_without_a_session_sends_nothing(self):
        self.apply()
        self.view.on_order_click(0)
        self.client.drop_session()
        self.view.on_cancel()
        self.assertEqual([], self.port.sent)

    def test_the_client_never_announces_the_cancellation_itself(self):
        """取消是**服务端写操作**：客户端只发命令，订单那一行在收到下一次
        推送之前纹丝不动（交接第 5 节：客户端不能自行宣告成功）。"""
        self.apply()
        self.view.on_order_click(0)
        self.view.on_cancel()
        self.assertEqual(1, self.view.order_count())
        self.assertEqual(u"1/4", self.view.order_progress_text(0))


class TestAvailability(FactoricFixture):

    def test_no_session_means_masked(self):
        self.assertFalse(self.view.is_unavailable())
        self.client.drop_session()
        self.assertTrue(self.view.is_unavailable())

    def test_a_closed_view_is_masked_even_with_a_session(self):
        self.view.close()
        self.assertTrue(self.view.is_unavailable())

    def test_the_mask_binding_does_not_mask_before_the_model_exists(self):
        """绑定比 `OnCreate` 早生效：那时遮上会在开屏第一帧闪一下全黑。"""
        panel = factoric_panel.FactoricPanel("main", Screen(), {})
        self.assertIsNone(panel.factoric)
        self.assertFalse(panel.unavailable())
        self.assertEqual(0, panel.order_count())
        self.assertEqual(u"", panel.detail_title())

    def test_the_safe_values_are_not_fake_numbers(self):
        panel = factoric_panel.FactoricPanel("main", Screen(), {})
        self.assertEqual(0, panel.order_item_id_aux(0))
        self.assertEqual(0.0, panel.memory_ratio())
        self.assertEqual(0.0, panel.source_progress(0))
        self.assertFalse(panel.order_visible(0))
        self.assertFalse(panel.source_visible(0))
        self.assertEqual(u"0/0", panel.active_tip().split(u" ")[-1])


class TestReceipts(FactoricFixture):
    """本界面**没有列表视图**，回执得自己取——不取就是「只能按一次」。"""

    def test_tick_drains_the_command_results(self):
        self.apply()
        self.view.on_order_click(0)
        self.view.on_cancel()
        request_id = list(self.view._requests)[0]
        self.feed(self.receipt(request_id))
        self.assertEqual(1, self.client.pending_results())
        self.view.tick()
        self.assertEqual(0, self.client.pending_results())
        self.assertEqual(1, len(self.view.receipts))

    def test_a_failed_receipt_is_kept_for_diagnostics(self):
        self.apply()
        self.view.on_order_click(0)
        self.view.on_cancel()
        request_id = list(self.view._requests)[0]
        self.feed(self.receipt(request_id, status=STATUS_REJECTED))
        self.view.tick()
        self.assertIsNotNone(self.view.last_error)
        self.assertEqual(0, self.view.errors, "被拒不是异常")

    def test_the_receipt_memory_is_bounded(self):
        for index in range(factoric_panel.RECEIPT_MEMORY + 4):
            self.feed(self.receipt("r%d" % index))
        self.view.tick()
        self.assertEqual(factoric_panel.RECEIPT_MEMORY, len(self.view.receipts))


class TestWire(FactoricFixture):
    """收包的两道过滤：消息种类与会话号，缺一不可。"""

    def test_a_message_from_another_session_is_dropped(self):
        self.feed(state(orders=[order_row(OTHER_ID)]), session_id="other")
        self.assertEqual(0, self.view.order_count())

    def test_another_kind_is_dropped(self):
        other = self.apply()
        before = self.view.order_count()
        self.feed(SessionReady(9, 9))
        self.assertEqual(before, self.view.order_count())
        self.assertTrue(other is not None)

    def test_the_client_opened_the_order_view(self):
        self.assertEqual(VIEW_ORDER, self.client.view_kind)


class TestRegistration(unittest.TestCase):

    def test_the_screen_key_is_the_one_the_definitions_map_to(self):
        self.assertEqual(SCREEN_NAME, factoric_panel.FactoricPanel._screen_key)
        self.assertEqual(SCREEN_NAME, TERMINAL_SCREENS[DEVICE_ID])

    def test_register_screens_includes_the_panel(self):
        from endernet_bp.endernet_scripts.endernet.client.ui import (
            register_screens,
        )
        registered = register_screens()
        self.assertIn(factoric_panel.FactoricPanel, registered)
        self.assertEqual(
            (factoric_panel.FactoricPanel,),
            tuple(cls for cls in registered
                  if cls is factoric_panel.FactoricPanel))

    def test_the_panel_is_not_a_container_proxy(self):
        """它走的是终端那条路（服务端 `PushUIRequest` 推开），不是 11-07 的
        容器代理那条——两者由定义表里两张不相交的表分开。"""
        self.assertFalse(getattr(factoric_panel.FactoricPanel,
                                 "_td_is_proxy", False))


class TestJsonContract(unittest.TestCase):
    """界面 JSON 与面板常量的接线。**只验名字对得上**，控件画得对不对、
    `#clip_ratio` / `#color` 绑在集合生成的行上能不能生效，都是游戏内实测项。"""

    @classmethod
    def setUpClass(cls):
        cls.panel = cls.load("factoric_terminal_panel.json")

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

    def every_control(self, node):
        """**含节点自己**：模板根上的绑定（`order` / `order_source_status` /
        `order_description_window` 那几条）也在里面，只遍历 children 会漏掉
        它们，而漏掉的表现是「断言说这条绑定没人用」——恰好把对的说成错的。"""
        yield node
        for entry in node.get("controls", []):
            for _key, value in entry.items():
                for item in self.every_control(value):
                    yield item

    def all_nodes(self):
        for value in self.panel.values():
            if not isinstance(value, dict):
                continue
            for node in self.every_control(value):
                yield node

    def all_bindings(self):
        """本文件里所有 `bindings` 条目（含模板定义里的）。"""
        for node in self.all_nodes():
            for binding in node.get("bindings", ()):
                yield node, binding

    def every_binding_name(self):
        """本文件里出现的**所有**绑定名：`bindings` 条目加上按钮的
        `$pressed_button_name`（后者的回调同样登记在类体里，但它不是一条
        `bindings` 条目——只看前者会把它当成「声明了却没人用」）。"""
        names = set()
        for node in self.all_nodes():
            for binding in node.get("bindings", ()):
                name = binding.get("binding_name")
                if isinstance(name, str):
                    names.add(name)
            pressed = node.get("$pressed_button_name")
            if isinstance(pressed, str) and pressed.startswith("#"):
                names.add(pressed)
        return names

    def declared_names(self):
        """面板类体里声明的**本界面自己的**绑定名（前缀筛），以及它们的旗标。"""
        out = {}
        for name in dir(factoric_panel.FactoricPanel):
            attr = getattr(factoric_panel.FactoricPanel, name)
            binding_name = getattr(attr, "binding_name", None)
            if binding_name and binding_name.startswith(BINDING_PREFIX):
                out[binding_name] = getattr(attr, "binding_flags", None)
        return out

    def test_every_declared_binding_appears_in_the_json(self):
        """**声明的每一条都要在 JSON 里找得到**：找不到的那条永远不会被问，
        界面上表现为「这一项一直空着」，而日志干净。"""
        in_json = set(
            name for name in self.every_binding_name()
            if name.startswith(BINDING_PREFIX))
        declared = set(self.declared_names())
        self.assertTrue(declared, "一条绑定都没声明，说明接线断了")
        self.assertEqual(set(), declared - in_json)

    def test_the_float_and_colour_bindings_follow_the_engine_flags(self):
        """进度条与三色背景各要一条浮点／颜色绑定。**引擎没有那个旗标时
        不注册**（`_bind` 的兜底），此时这一条在 JSON 里是无主的——但它
        本来也只在有旗标时才有用，因此在哪一支都自洽。"""
        declared = self.declared_names()
        for name in (BINDING_PREFIX + "memory_ratio",
                     BINDING_PREFIX + "order_progress",
                     BINDING_PREFIX + "source_progress"):
            if factoric_panel.FLOAT_BINDING_FLAG is None:
                self.assertNotIn(name, declared)
            else:
                self.assertEqual(factoric_panel.FLOAT_BINDING_FLAG,
                                 declared.get(name), name)
        if factoric_panel.COLOR_BINDING_FLAG is not None:
            self.assertEqual(factoric_panel.COLOR_BINDING_FLAG,
                             declared.get(BINDING_PREFIX + "source_color"))

    def test_the_two_collection_names_agree_everywhere(self):
        """三处必须一致：grid 的 `collection_name`、绑定条目的
        `binding_collection_name`、Python 的常量。对不上的表现是**每一格都读
        下标 0**（`template_panel` 记过同一个坑）。"""
        seen = set()
        for _node, binding in self.all_bindings():
            if binding.get("binding_type") != "collection":
                continue
            seen.add(binding.get("binding_collection_name"))
        self.assertEqual(set([ORDER_COLLECTION, SOURCE_COLLECTION]), seen)
        self.assertEqual(
            ORDER_COLLECTION,
            self.panel["orders"]["controls"][0]["grid"]["collection_name"])
        self.assertEqual(SOURCE_COLLECTION,
                         self.panel["order_sources_grid"]["collection_name"])

    def test_the_orders_grid_count_comes_from_the_panel(self):
        """静态 8 只是资源初值：真正的行数是**当前活跃订单数**
        （交接第 4 节：不能截断所有运行订单）。"""
        grid = self.panel["orders"]["controls"][0]["grid"]
        self.assertEqual([{"binding_condition": "always",
                           "binding_name": BINDING_PREFIX + "order_count",
                           "binding_name_override": "#maximum_grid_items",
                           "binding_type": "global"}], grid["bindings"])
        self.assertEqual([{"binding_condition": "always",
                           "binding_name": BINDING_PREFIX + "source_count",
                           "binding_name_override": "#maximum_grid_items",
                           "binding_type": "global"}],
                         self.panel["order_sources_grid"]["bindings"])

    def test_every_button_has_a_mapping_as_well_as_a_name(self):
        """只有 `$pressed_button_name`、没有 `button_mappings` 时点击**安静地
        什么都不做**（2026-10-02 真机撞出来的那一类）。"""
        nodes = [node for key, value in self.panel.items()
                 if isinstance(value, dict)
                 for node in self.every_control(value)]
        named = [node for node in nodes if "$pressed_button_name" in node]
        self.assertTrue(named, "一个按钮都没接")
        for node in named:
            self.assertNotEqual(
                "%netease_editor_template_namespace.click",
                node["$pressed_button_name"])
            self.assertTrue(node.get("button_mappings"), node.get("type"))

    def test_the_mask_and_the_device_popup_are_instances_of_the_screen(self):
        content = self.content()
        self.assertIn("unavailable_mask", content)
        self.assertIn("device_status_popup", content)

    def test_the_detail_window_is_not_placed_on_the_main_panel(self):
        """**它是按需建的，不挂在 `content` 上**（User 2026-10-03 定）。

        挂在主面板上就意味着一棵 200 行的控件树每次开界面都白建一遍，而绝大多数
        人根本不会点开某一单。
        """
        self.assertNotIn("order_detail", self.content())
        self.assertIn("order_description_window", self.panel)

    def test_the_detail_template_is_hidden_centred_and_unbound(self):
        """模板根上三样：默认藏、居中、**没有 `#visible` 绑定**。

        最后一条是刻意的：显隐由脚本写 `SetVisible`（唯一那个写者），再挂一条
        每帧求值的绑定就会互相盖，表现是「窗口闪一下又没了」。
        """
        window = self.panel["order_description_window"]
        self.assertFalse(window["visible"])
        self.assertEqual("center", window["anchor_from"])
        self.assertEqual("center", window["anchor_to"])
        self.assertNotIn(
            "#visible",
            [binding["binding_name_override"]
             for binding in window.get("bindings", ())])

    def test_the_definition_the_panel_builds_at_runtime_really_exists(self):
        """`AddElement` 用的那串**必须能在本文件里找到**：查不到就是抛异常，
        而表现是「点了没反应」——`factoric_panel` 里那三个常量的存在理由。"""
        namespace, _, name = factoric_panel.DETAIL_DEFINITION.partition(".")
        self.assertEqual(namespace, self.panel["namespace"])
        self.assertIn(name, self.panel)
        self.assertTrue(factoric_panel.DETAIL_NAME)

    def test_the_item_templates_are_wired_to_their_own_collection(self):
        """两张行模板各自绑到自己那一片集合上：绑错的表现是配方行按订单
        的下标去读（`template_panel` 那条「每个区一套方法」记的就是它）。"""
        order = self.panel["order"]
        self.assertEqual(
            [ORDER_COLLECTION],
            [binding["binding_collection_name"]
             for binding in order["bindings"]
             if binding["binding_type"] == "collection"])
        source = self.panel["order_source_status"]
        self.assertEqual(
            [SOURCE_COLLECTION, SOURCE_COLLECTION],
            [binding["binding_collection_name"]
             for binding in source["bindings"]
             if binding["binding_type"] == "collection"])
        # 阶段背景色改的是**这一项图片本身**，不是只改进度条或文字。
        self.assertIn(BINDING_PREFIX + "source_color",
                      [binding["binding_name"] for binding in source["bindings"]])


def _loose(payload):
    """一个只有 `memory / orders / sources` 三个属性的最小对象。

    `FactoricView.apply` 是按属性读的（协议层已经校验过形状），
    这里用它钉住「坏形状不炸」。
    """
    class Loose(object):
        pass

    loose = Loose()
    for key, value in payload.items():
        setattr(loose, key, value)
    return loose


if __name__ == "__main__":
    unittest.main()
