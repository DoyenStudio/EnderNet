# coding=utf-8
"""下单预览窗：显示模型与控制器（11 增量，客户端半边）。

**全部离线**：模型是纯数据，控制器只对一个假客户端说话。窗口控件是宿主
面板 JSON 里的一个实例，绑定在类体里（`widgets/order_preview.py`），
因此这里碰不到引擎。

守的是契约里几条「错了也看不出来」的：

1. **收齐分片才换行表**：状态头到了不算完，中间任何一片缺失都不能让界面
   看起来像「算完了，就这几样」。
2. **陈旧包按三层丢弃**：会话、预览号、代次。
3. **提交闸在服务端**：`can_submit()` 照抄服务端那句 `submit_ok`，
   客户端不去数红行；`submit()` 在不可提交时**一条都不发**。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.client.ui import order_preview_window
from endernet.client.ui.widgets import order_preview
from endernet.common.protocol import (
    PreviewChunkMessage,
    PreviewStateMessage,
)

STONE = "minecraft:stone"
IRON = "minecraft:iron_ingot"


def row(canonical, missing=False, allocated=0, planned=0, demand=1):
    return {
        "canonical": canonical, "name": canonical, "aux": 0,
        "demand": demand, "allocated": allocated, "planned": planned,
        "missing": missing, "sources": [],
    }


def state(**overrides):
    fields = dict(
        request_id="r1", preview_id="pv1", generation=1, status="running",
        detail="", submit_ok=False,
        target={"canonical": STONE, "name": STONE, "aux": 0},
        count=4, semantics="hold", inventory_version=1, topology_version=1,
        plan_digest="", row_count=0, packet_count=0, missing=False,
        blocked=[], truncated=False, spent=0,
    )
    fields.update(overrides)
    return PreviewStateMessage(
        fields["request_id"], fields["preview_id"], fields["generation"],
        fields["status"], fields["detail"], fields["submit_ok"], fields["target"],
        fields["count"], fields["semantics"], fields["inventory_version"],
        fields["topology_version"], fields["plan_digest"], fields["row_count"],
        fields["packet_count"], fields["missing"], fields["blocked"],
        fields["truncated"], fields["spent"],
    )


def chunk(rows, index=0, count=1, preview_id="pv1", generation=1):
    return PreviewChunkMessage(preview_id, generation, index, count, rows)


class ModelCase(unittest.TestCase):
    def setUp(self):
        self.model = order_preview.OrderPreviewModel()

    def open(self):
        self.model.open(STONE, 4)


class TestStages(ModelCase):
    def test_open_shows_loading_before_anything_arrives(self):
        """**先显示 loading 再发请求**：等服务端回话才显示会让玩家先看到
        一个空窗口（契约第 3 节）。"""
        self.open()
        self.assertTrue(self.model.visible)
        self.assertTrue(self.model.loading())
        self.assertFalse(self.model.can_submit())
        self.assertEqual(self.model.grid_count(), 0)

    def test_a_running_header_keeps_loading(self):
        self.open()
        self.model.apply_state(state(status="running", row_count=0))
        self.assertTrue(self.model.loading(), "还在算就一直是 loading")
        self.assertFalse(self.model.can_submit())

    def test_a_complete_result_ends_loading_and_allows_submit(self):
        self.open()
        self.model.apply_state(state(
            status="complete", submit_ok=True, row_count=2, packet_count=1))
        # **状态头说算完了，但行还没到齐**——契约第 3 节要的是「收到完整匹配
        # 的结果后」才结束 loading，因此这时仍然在转圈。
        self.assertTrue(self.model.loading())
        self.model.apply_chunk(chunk([row(STONE, allocated=4)]))
        self.assertFalse(self.model.loading())
        self.assertEqual(self.model.grid_count(), 1)
        self.assertTrue(self.model.can_submit())

    def test_a_shortfall_result_ends_loading_but_blocks_submit(self):
        """缺料是**算完了**的结论，不是还在算——界面要给的原因，不是转圈。"""
        self.open()
        self.model.apply_state(state(
            status="missing_materials", submit_ok=False, missing=True,
            detail=u"有 1 种原料凑不齐，不能下单", row_count=1, packet_count=1))
        self.model.apply_chunk(chunk([row(IRON, missing=True, demand=9)]))
        self.assertFalse(self.model.loading(), "行到齐了才结束 loading")
        self.assertFalse(self.model.can_submit())
        self.assertEqual(self.model.detail, u"有 1 种原料凑不齐，不能下单")
        self.assertTrue(self.model.row_missing(0))

    def test_a_rejected_request_stops_spinning(self):
        """服务端拒了 `preview_request`：**空预览号的那一条**就是那句话。"""
        self.open()
        self.model.apply_state(state(
            preview_id="", status="error", detail=u"这张网络已经有 4 份预览在算"))
        self.assertFalse(self.model.loading())
        self.assertFalse(self.model.can_submit())
        self.assertIn(u"4 份预览", self.model.detail)

    def test_a_complete_header_with_no_rows_finishes_immediately(self):
        """零行也是**一份完整结果**（比如库里已经够了）：不能一直转圈。"""
        self.open()
        self.model.apply_state(state(
            status="complete", submit_ok=True, row_count=0, packet_count=0))
        self.assertFalse(self.model.loading())
        self.assertTrue(self.model.can_submit())
        self.assertEqual(self.model.grid_count(), 0)


class TestChunkAssembly(ModelCase):
    def test_rows_appear_only_when_every_packet_has_arrived(self):
        """**收齐才换。** 半份行表看起来与「就这几样」一模一样。"""
        self.open()
        self.model.apply_state(state(
            status="complete", submit_ok=True, row_count=3, packet_count=3))
        self.model.apply_chunk(chunk([row(STONE)], index=0, count=3))
        self.model.apply_chunk(chunk([row(IRON)], index=1, count=3))
        self.assertEqual(self.model.grid_count(), 0, "还差一片就不该显示")
        self.model.apply_chunk(chunk([row("minecraft:coal")], index=2, count=3))
        self.assertEqual(self.model.grid_count(), 3)
        self.assertEqual(
            [self.model.name_text(index) for index in range(3)],
            [STONE, IRON, "minecraft:coal"],
            "行按分片顺序拼，不按到达顺序",
        )

    def test_out_of_order_chunks_assemble_the_same(self):
        self.open()
        self.model.apply_state(state(
            status="complete", submit_ok=True, row_count=2, packet_count=2))
        self.model.apply_chunk(chunk([row(IRON)], index=1, count=2))
        self.model.apply_chunk(chunk([row(STONE)], index=0, count=2))
        self.assertEqual(
            [self.model.name_text(index) for index in range(2)], [STONE, IRON]
        )

    def test_a_duplicate_packet_is_ignored(self):
        self.open()
        self.model.apply_state(state(
            status="complete", submit_ok=True, row_count=2, packet_count=2))
        self.model.apply_chunk(chunk([row(STONE)], index=0, count=2))
        self.model.apply_chunk(chunk([row("minecraft:coal")], index=0, count=2))
        self.model.apply_chunk(chunk([row(IRON)], index=1, count=2))
        self.assertEqual(self.model.name_text(0), STONE, "重复的那一片保留先到的")

    def test_a_packet_from_an_older_generation_is_dropped(self):
        """换了一份预览之后，上一轮的分片不能落进来。"""
        self.open()
        self.model.apply_state(state(
            status="complete", submit_ok=True, row_count=1, packet_count=1))
        self.model.apply_chunk(chunk([row(STONE)]))
        self.assertEqual(self.model.grid_count(), 1)
        # 新一轮（服务端重算了）。
        self.model.apply_state(state(
            generation=2, status="complete", submit_ok=True,
            row_count=1, packet_count=1))
        self.model.apply_chunk(chunk([row(IRON)], generation=1))
        self.assertEqual(self.model.grid_count(), 1)
        self.assertEqual(self.model.name_text(0), STONE, "旧一代的行不覆盖")
        self.assertEqual(self.model.stats["stale_chunks"], 1)

    def test_a_packet_for_another_preview_is_dropped(self):
        self.open()
        self.model.apply_state(state(
            status="complete", submit_ok=True, row_count=1, packet_count=1))
        self.model.apply_chunk(chunk([row(STONE)], preview_id="pv-other"))
        self.assertEqual(self.model.grid_count(), 0)
        self.assertEqual(self.model.stats["stale_chunks"], 1)

    def test_a_packet_before_any_header_is_dropped(self):
        self.open()
        self.model.apply_chunk(chunk([row(STONE)]))
        self.assertEqual(self.model.grid_count(), 0)


class TestRowText(ModelCase):
    def setUp(self):
        ModelCase.setUp(self)
        self.open()
        self.model.apply_state(state(
            status="complete", submit_ok=True, row_count=1, packet_count=1))
        self.model.apply_chunk(chunk([
            row(STONE, missing=False, allocated=256, planned=0, demand=256),
        ]))

    def test_the_label_names_both_numbers(self):
        """契约第 4 节：库存栏是**本计划分配的份额**，不是全网可用。"""
        self.assertEqual(self.model.count_text(0), u"库存 256 计划制作 0")

    def test_an_out_of_range_index_is_answered_with_empty_text(self):
        for bad in (1, -1, True, "0", None):
            self.assertEqual(self.model.count_text(bad), "")
            self.assertIsNone(self.model.row_of(bad))
            self.assertFalse(self.model.row_missing(bad))

    def test_closing_hides_the_grid(self):
        self.model.close()
        self.assertFalse(self.model.visible)
        self.assertEqual(self.model.grid_count(), 0)
        self.assertFalse(self.model.can_submit())


# -- 控制器 ----------------------------------------------------------------


class FakeClient(object):
    """够用的网络客户端替身：只回答控制器问的那几件事。"""

    def __init__(self, session=True):
        self.session = session
        self.session_id = "s1"
        self.index_version = 7
        self.sent = []
        self.handlers = []

    def has_session(self):
        return self.session

    def send_command(self, action, **kwargs):
        self.sent.append((action, kwargs))
        return "req-%d" % (len(self.sent),)

    def on_message(self, handler):
        self.handlers.append(handler)


class TestController(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.window = order_preview_window.OrderPreviewWindow(
            screen=None, client=self.client)
        self.window.listen()

    def deliver(self, message, session_id="s1"):
        envelope = type("E", (), {"session_id": session_id})()
        for handler in self.client.handlers:
            handler(envelope, message)

    def test_a_request_opens_the_window_and_sends_one_command(self):
        self.assertTrue(self.window.request(STONE, 4))
        self.assertTrue(self.window.model.loading())
        action, kwargs = self.client.sent[0]
        self.assertEqual(action, "preview_request")
        self.assertEqual(kwargs["count"], 4)
        self.assertEqual(kwargs["key"], STONE)

    def test_no_session_means_no_request_and_no_window(self):
        """开一个会一直转圈的窗口比不开更糟。"""
        self.client.session = False
        self.assertFalse(self.window.request(STONE, 4))
        self.assertFalse(self.window.model.visible)
        self.assertEqual(self.client.sent, [])

    def test_results_reach_the_model(self):
        self.window.request(STONE, 4)
        self.deliver(state(status="complete", submit_ok=True,
                           row_count=1, packet_count=1))
        self.deliver(chunk([row(STONE, allocated=4)]))
        self.assertTrue(self.window.model.can_submit())
        self.assertEqual(self.window.model.grid_count(), 1)

    def test_a_message_for_another_session_is_ignored(self):
        self.window.request(STONE, 4)
        self.deliver(state(status="complete", submit_ok=True), session_id="s2")
        self.assertTrue(self.window.model.loading(), "别人的包不能推进我们的窗口")

    def test_a_closed_window_stops_hearing_results(self):
        self.window.request(STONE, 4)
        self.window.close()
        self.deliver(state(status="complete", submit_ok=True,
                           row_count=1, packet_count=1))
        self.assertFalse(self.window.model.visible)

    def test_closing_twice_is_safe(self):
        self.window.close()
        self.window.close()
        self.assertTrue(self.window.closed)

    def test_cancel_closes_and_tells_the_server(self):
        self.window.request(STONE, 4)
        self.deliver(state(status="complete", submit_ok=True,
                           row_count=0, packet_count=0))
        self.assertTrue(self.window.cancel())
        self.assertFalse(self.window.model.visible)
        action, kwargs = self.client.sent[-1]
        self.assertEqual(action, "preview_cancel")
        self.assertEqual(kwargs["preview"], "pv1")

    def test_cancelling_without_a_preview_only_closes(self):
        """预览号还没回来就关窗：**不发那条指名取消的命令**。"""
        self.window.request(STONE, 4)
        self.assertFalse(self.window.cancel())
        self.assertEqual([action for action, _ in self.client.sent],
                         ["preview_request"])

    def test_dismiss_is_the_same_as_cancel(self):
        """右上角关闭与「取消」都只收起预览——**都不是取消订单**。"""
        self.window.request(STONE, 4)
        self.deliver(state(status="complete", submit_ok=True,
                           row_count=0, packet_count=0))
        self.window.dismiss()
        self.assertEqual(self.client.sent[-1][0], "preview_cancel")

    def test_submit_sends_the_preview_id_and_the_count(self):
        self.window.request(STONE, 4)
        self.deliver(state(status="complete", submit_ok=True,
                           row_count=0, packet_count=0))
        self.assertTrue(self.window.submit())
        action, kwargs = self.client.sent[-1]
        self.assertEqual(action, "order_submit")
        self.assertEqual(kwargs["preview"], "pv1")
        self.assertEqual(kwargs["count"], 4)
        self.assertEqual(kwargs["key"], STONE)

    def test_submit_is_refused_while_the_result_is_not_in(self):
        """**分片没收齐就一条都不发**（契约第 3 节：未完整结果不得开启提交）。"""
        self.window.request(STONE, 4)
        self.deliver(state(status="complete", submit_ok=True,
                           row_count=2, packet_count=2))
        self.deliver(chunk([row(STONE)], index=0, count=2))
        self.assertFalse(self.window.submit())
        self.assertEqual([action for action, _ in self.client.sent],
                         ["preview_request"])

    def test_submit_is_refused_when_the_server_says_no(self):
        self.window.request(STONE, 4)
        self.deliver(state(status="missing_materials", submit_ok=False,
                           missing=True, row_count=0, packet_count=0))
        self.assertFalse(self.window.submit())
        self.assertEqual(self.window.stats["refused"], 1)

    def test_submitting_disables_the_button_until_the_answer_comes(self):
        self.window.request(STONE, 4)
        self.deliver(state(status="complete", submit_ok=True,
                           row_count=0, packet_count=0))
        self.window.submit()
        self.assertFalse(self.window.model.can_submit(), "在途时按钮要禁用")


# -- JSON 契约 --------------------------------------------------------------


def ui(name):
    """读一份资源包界面 JSON。**走真路径**：契约断言的对象就是上线的那一份。"""
    import io as _io
    import json
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "endernet_rp", "ui", name)
    return json.load(_io.open(path, encoding="utf-8"))


def control(controls, name):
    for item in controls:
        if isinstance(item, dict) and name in item:
            return item[name]
    raise AssertionError("控件 %s 不在这一层" % (name,))


def bindings_of(control_def):
    out = {}
    for item in control_def.get("bindings", ()):
        name = item.get("binding_name")
        if name:
            out[name] = item
    return out


class TestWindowJson(unittest.TestCase):
    """窗口 JSON 与 Python 常量的契约。

    **名字对不上时引擎不报错**，只是查不到绑定、安静地给假值——界面上看起来
    就是「这个功能没生效」。因此每一条绑定名都在这里与 Python 侧对账。
    """

    @classmethod
    def setUpClass(cls):
        cls.windows = ui("endernet_windows.json")

    def test_the_binding_names_match_the_python_constants(self):
        row = bindings_of(control(self.windows["order_preview_src_item"]["controls"],
                                  "bg"))
        self.assertIn(order_preview.ROW_MISSING_BINDING, row)
        window = self.windows["order_preview_window"]
        loading = bindings_of(control(window["controls"], "loading_panel"))
        self.assertIn(order_preview.LOADING_BINDING, loading)
        detail = bindings_of(control(window["controls"], "label"))
        self.assertIn(order_preview.DETAIL_BINDING, detail)
        grid = bindings_of(self.windows["order_preview_src_items"])
        self.assertIn(order_preview.COUNT_BINDING, grid)

    def test_the_row_icon_and_texts_are_bound(self):
        controls = self.windows["order_preview_src_item"]["controls"]
        icon = bindings_of(control(controls, "item_renderer"))
        self.assertEqual(
            icon[order_preview.ITEM_AUX_BINDING]["binding_name_override"],
            "#item_id_aux",
        )
        for name, binding in (("item_name", order_preview.ROW_NAME_BINDING),
                              ("label", order_preview.ROW_COUNT_BINDING)):
            item = control(controls, name)
            self.assertIn(binding, bindings_of(item))
            # **label 必须自己写 `text: "#text"`**：只挂绑定不写 text 时，
            # 值会被写进属性却没人读，界面空白而回调一直在跑（真机撞过）。
            self.assertEqual(item["text"], "#text")

    def test_every_row_binding_names_its_collection(self):
        """**集合绑定必须写明 `binding_collection_name`。**

        少了它引擎不会报错——它只是**解不出值**，于是行里的文字与图标
        全都静静地空着（图标会退成 `property_bag` 里的那个默认值，
        看起来「有图标」）。2026-10-03 上机就是这么撞出来的：
        三行都渲染出来了，图标是同一个，文字一个都没有。
        """
        grid = self.windows["order_preview_src_items"]
        expected = grid["collection_name"]
        seen = 0
        for control in self.windows["order_preview_src_item"]["controls"]:
            for name, body in control.items():
                for binding in body.get("bindings", ()):
                    if binding.get("binding_type") != "collection":
                        continue
                    seen += 1
                    self.assertEqual(
                        binding.get("binding_collection_name"), expected,
                        "%s 的绑定没写集合名" % (name,),
                    )
        self.assertEqual(
            seen, 5,
            "五条集合绑定：图标、名字、数量、缺料颜色、缺料遮罩的显隐")

    def test_the_missing_row_has_a_mask_that_actually_renders(self):
        """缺料行的红底**由一层遮罩实现**，不是靠 `bg` 的颜色绑定。

        2026-10-03 上机实测：`#color` 绑在 **image** 上不生效（同一个
        `BF_BindColor` 绑在 label 上是好的，11-13 的绿字在线验过）。
        遮罩用的是 `#visible`——那条路在本仓库里有先例
        （`item_can_be_selected/selected_frame`），而 `bg` 上那条颜色绑定
        按契约原文保留着：它不生效也不碍事，引擎哪天认了就自动生效。
        """
        controls = self.windows["order_preview_src_item"]["controls"]
        mask = control(controls, "missing_mask")
        self.assertEqual(mask["type"], "image")
        self.assertEqual(mask["color"], order_preview.COLOR_MISSING,
                         "遮罩的静态色就是契约要的那个 RGB")
        entry = bindings_of(mask)[order_preview.ROW_MISSING_BINDING]
        self.assertEqual(entry["binding_name_override"], "#visible")
        self.assertEqual(entry["binding_collection_name"],
                         self.windows["order_preview_src_items"]["collection_name"])
        # 遮罩要压在文字与图标**下面**，否则会把它们糊掉。
        for name in ("item_renderer", "item_name", "label"):
            self.assertLess(mask.get("layer", 0),
                            control(controls, name).get("layer", 0),
                            "%s 被遮罩糊住了" % (name,))

    def test_the_row_background_is_not_clipped_away(self):
        """**`clip_ratio: 0.0` 会让整块背景被裁掉**，于是缺料的红色永远看不见。

        这一条钉的是那个属性的**缺席**：它一旦回来，颜色绑定照样生效、
        日志里一个错都没有，而画面上什么都没有。
        """
        bg = control(self.windows["order_preview_src_item"]["controls"], "bg")
        self.assertNotIn("clip_ratio", bg)
        self.assertNotIn("clip_direction", bg)
        self.assertEqual(
            bindings_of(bg)[order_preview.ROW_MISSING_BINDING][
                "binding_name_override"],
            "#color",
        )

    def test_the_three_preview_buttons_have_their_own_events(self):
        """下单／取消／关闭**三个独立事件**。

        契约第 3 节明写：不能拿工厂终端的详情关闭冒充提交，也不能拿真实
        订单取消冒充预览取消——那两样点下去动的是别的东西。
        """
        controls = self.windows["order_preview_window"]["controls"]
        confirm = control(controls, "confirm_btn@common.button")
        cancel = control(controls, "cancel_order_btn@common.button")
        close = control(controls, "close@endernet_ui_common.close_btn")
        self.assertEqual(confirm["$pressed_button_name"],
                         order_preview.CONFIRM_BINDING)
        self.assertEqual(cancel["$pressed_button_name"],
                         order_preview.CANCEL_BINDING)
        self.assertEqual(close["$pressed_button_name"],
                         order_preview.CLOSE_BINDING)
        for control_def in (confirm, cancel, close):
            self.assertNotIn("Factoric", control_def["$pressed_button_name"])

    def test_the_submit_button_has_the_enabled_binding(self):
        confirm = control(self.windows["order_preview_window"]["controls"],
                          "confirm_btn@common.button")
        entry = bindings_of(confirm)[order_preview.SUBMIT_BINDING]
        self.assertEqual(entry["binding_name_override"], "#enabled")

    def test_the_grid_cap_matches_the_server_row_cap(self):
        """**服务端截断到几行，网格就得能显示几行。**

        两处各写一个数的话，服务端多给的行会被网格静默丢掉——而「只显示了
        一部分」与「就这么多」在界面上长得一样。
        """
        from endernet.server.crafting.preview import PreviewPolicy

        grid = self.windows["order_preview_src_items"]
        self.assertEqual(grid["maximum_grid_items"], PreviewPolicy().max_rows)
        self.assertEqual(grid["collection_name"], order_preview.COLLECTION)


class TestPanelInstances(unittest.TestCase):
    """三块终端屏都实例化了预览窗。**只实例化，不新建模板**（同 11-07）。"""

    PANELS = ("common_terminal_panel.json", "crafting_terminal_panel.json",
              "template_terminal_panel.json")

    #: 控件实例名。与 `plate` 一起进 JSON —— 模板仍然只有
    #: `endernet_windows` 里那一份。
    INSTANCE = "order_preview@endernet_windows.order_preview_window"

    def test_every_terminal_panel_carries_the_instance(self):
        for name in self.PANELS:
            data = ui(name)
            found = None
            for item in data["content"]["controls"]:
                if isinstance(item, dict) and self.INSTANCE in item:
                    found = item[self.INSTANCE]
            self.assertIsNotNone(found, "%s 少了预览窗实例" % (name,))
            entry = bindings_of(found)[order_preview.VISIBLE_BINDING]
            self.assertEqual(entry["binding_name_override"], "#visible")
            self.assertTrue(entry["binding_type"], "整窗显隐要挂全局绑定")

    def test_the_preview_sits_above_the_device_status_popup(self):
        """两层弹窗同屏时必须定得出先后：预览是玩家刚点开的那个。"""
        for name in self.PANELS:
            data = ui(name)
            layers = {}
            for item in data["content"]["controls"]:
                for key, value in item.items():
                    if key.endswith("device_status_window_basic") or                             key == self.INSTANCE:
                        layers[key] = value.get("layer", 0)
            self.assertEqual(len(layers), 2, "%s 两个弹窗实例都要在" % (name,))
            preview = layers[self.INSTANCE]
            popup = max(v for k, v in layers.items() if k != self.INSTANCE)
            self.assertGreater(preview, popup, "%s 的预览窗要盖在详情之上" % (name,))


if __name__ == "__main__":
    unittest.main()
