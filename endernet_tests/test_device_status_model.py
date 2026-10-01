# coding=utf-8
"""客户端设备状态模型：代次隔离、两行分组、弹窗单例（11-13）。

**全部离线**：`client/ui/widgets/device_status.py` 不 import 引擎，只提供
绑定要读的值，因此这里直接构造模型、喂消息、断言读出来的东西。

三条判据：

- **代次对不上的消息一律丢**——「拆掉又在原地放一个」之后旧实例的响应
  不得污染新界面；
- **只有两种文本**——`validity="pending"` 照样是「离线」，不出现第三态；
- **弹窗只有一个**——连点不叠窗是结构性的（只有一个控件、重复打开幂等），
  不是靠计数去重。
"""

import json
import os
import unittest

import testsupport  # noqa: F401  (放进 sys.path)

from endernet.client.ui.widgets import device_status
from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.protocol import DeviceStatusMessage, Reason

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXTURES = os.path.join(ROOT, "endernet_rp", "textures", "ui", "endernet")

REF = DeviceRef(NodeKey(0, 10, 64, 20), 3)


def message(
    online=True,
    generation=3,
    validity="ok",
    reasons=(),
    state_text=None,
    master_instance_id="master-1",
    bandwidth_ok=True,
    power_ok=True,
    ui_applicable=True,
):
    # type: (...) -> DeviceStatusMessage
    return DeviceStatusMessage(
        "endernet:common_terminal_panel",
        generation,
        online,
        state_text if state_text is not None else ("在线" if online else "离线"),
        validity,
        "runnable" if online else "master_absent",
        "运行中" if online else "主控未接管",
        7,
        reasons=list(reasons),
        notes=[],
        master_instance_id=master_instance_id,
        bandwidth_ok=bandwidth_ok,
        power_ok=power_ok,
        bandwidth_domain_id="domain:0:0,64,0" if online else None,
        ui_applicable=ui_applicable,
    )


def reason(code, detail):
    return {"code": code, "detail": detail}


class TestStatusBarReads(unittest.TestCase):
    def setUp(self):
        self.model = device_status.DeviceStatusModel(REF)

    def test_nothing_shows_until_a_verdict_arrives(self):
        """没有结论时**不显示**，而不是先糊一个红色的「离线」。

        它是方向错误的结论：玩家会去查一个不存在的故障。
        """
        self.assertFalse(self.model.visible())
        self.assertEqual(self.model.text(), "")

    def test_an_online_verdict_is_green_and_an_offline_one_is_red(self):
        self.model.apply(message(online=True))
        self.assertTrue(self.model.visible())
        self.assertEqual(self.model.text(), "在线")
        self.assertEqual(self.model.color(), device_status.COLOR_ONLINE)
        self.assertEqual(self.model.icon(), device_status.ICON_ONLINE)

        self.model.apply(message(online=False, reasons=[reason("brownout", "能源不可用")]))
        self.assertEqual(self.model.text(), "离线")
        self.assertEqual(self.model.color(), device_status.COLOR_OFFLINE)
        self.assertEqual(self.model.icon(), device_status.ICON_OFFLINE)

    def test_pending_is_still_only_offline(self):
        self.model.apply(message(online=False, validity="pending",
                                 reasons=[reason(Reason.MASTER_RECOVERING, "主控恢复中")]))
        self.assertEqual(self.model.text(), "离线")
        self.assertNotIn("待确认", self.model.text())

    def test_a_controller_is_not_shown_by_the_client_either(self):
        """控制器由服务端不发；客户端收到也按「不该显示」处理。"""
        self.assertFalse(self.model.apply(message(ui_applicable=False)))
        self.assertFalse(self.model.visible())
        self.assertEqual(self.model.stats["dropped"], 1)


class TestGenerationIsolation(unittest.TestCase):
    def setUp(self):
        self.model = device_status.DeviceStatusModel(REF)

    def test_a_message_from_another_generation_is_dropped(self):
        self.model.apply(message(online=True, generation=3))
        self.assertFalse(self.model.apply(message(online=False, generation=9)))
        self.assertEqual(self.model.text(), "在线")
        self.assertEqual(self.model.stats["dropped"], 1)

    def test_an_identical_verdict_is_a_no_op(self):
        self.assertTrue(self.model.apply(message()))
        self.assertFalse(self.model.apply(message()))

    def test_a_model_without_a_generation_accepts_anything(self):
        loose = device_status.DeviceStatusModel(None)
        self.assertTrue(loose.apply(message(generation=42)))


class TestPopupRows(unittest.TestCase):
    def setUp(self):
        self.model = device_status.DeviceStatusModel(REF)

    def test_controller_reasons_go_to_the_first_row_and_network_ones_to_the_second(self):
        self.model.apply(message(online=False, reasons=[
            reason(Reason.MASTER_ABSENT, "该网络没有主控接管"),
            reason(Reason.BROWNOUT, "能源不可用"),
            reason(Reason.BANDWIDTH_DENIED, "负载域已满"),
        ]))
        master, master_mask, net, net_mask = self.model.popup_rows()
        self.assertEqual(master, "该网络没有主控接管")
        self.assertIn("能源不可用", net)
        self.assertIn("负载域已满", net)
        self.assertEqual(master_mask, device_status.MASK_OFFLINE)
        self.assertEqual(net_mask, device_status.MASK_OFFLINE)

    def test_multiple_reasons_are_all_listed_not_collapsed(self):
        """11.md 点名：多个原因不能塌成一句静态的「网络故障中」。"""
        self.model.apply(message(online=False, reasons=[
            reason(Reason.BROWNOUT, "能源不可用"),
            reason(Reason.REBUILDING, "网络重建中"),
        ]))
        net = self.model.net_text()
        self.assertIn("能源不可用", net)
        self.assertIn("网络重建中", net)
        self.assertNotEqual(net, "网络故障中")

    def test_an_online_device_explains_itself_from_the_tri_state(self):
        self.model.apply(message(online=True))
        master, master_mask, net, net_mask = self.model.popup_rows()
        self.assertEqual(master, device_status.MASTER_ROW_OK)
        self.assertEqual(master_mask, device_status.MASK_ONLINE)
        self.assertEqual(net, "带宽满足，能源满足")
        self.assertEqual(net_mask, device_status.MASK_ONLINE)

    def test_an_unconfirmed_figure_says_pending_not_short(self):
        """`None` 说「待确认」，**不说「不足」**——否则会指一个错的方向。"""
        self.model.apply(message(online=False, validity="pending",
                                 bandwidth_ok=None, power_ok=None,
                                 master_instance_id=None))
        net = self.model.net_text()
        self.assertEqual(net, "带宽待确认，能源待确认")
        self.assertEqual(self.model.master_text(), device_status.MASTER_ROW_MISSING)

    def test_rows_read_before_any_verdict_are_still_text(self):
        self.assertEqual(self.model.master_text(), device_status.MASTER_ROW_MISSING)
        self.assertEqual(self.model.net_text(), device_status.NET_ROW_UNKNOWN)


class TestPopupSingleInstance(unittest.TestCase):
    def setUp(self):
        self.model = device_status.DeviceStatusModel(REF)

    def test_repeated_clicks_do_not_stack_windows(self):
        self.assertFalse(self.model.popup_visible())
        self.model.open_popup()
        self.model.open_popup()
        self.model.open_popup()
        self.assertTrue(self.model.popup_visible())

    def test_closing_only_hides_the_popup(self):
        self.model.open_popup()
        self.model.close_popup()
        self.assertFalse(self.model.popup_visible())
        # 关闭详情不影响状态栏本身：它拿到的结论还在。
        self.model.apply(message())
        self.assertTrue(self.model.visible())

    def test_the_popup_can_be_opened_while_offline(self):
        """离线时照样要能打开看原因——这正是详情存在的理由。"""
        self.model.apply(message(online=False, reasons=[reason("brownout", "能源不可用")]))
        self.model.open_popup()
        self.assertTrue(self.model.popup_visible())


class TestTexturePaths(unittest.TestCase):
    def test_every_texture_the_model_can_return_exists_on_disk(self):
        """路径写错的表现是紫黑块，而它在离线与真机都不会报错。"""
        for path in (device_status.ICON_ONLINE, device_status.ICON_OFFLINE,
                     device_status.MASK_ONLINE, device_status.MASK_OFFLINE):
            self.assertTrue(path.startswith("textures/ui/endernet/"), path)
            name = path.rsplit("/", 1)[-1] + ".png"
            self.assertTrue(os.path.exists(os.path.join(TEXTURES, name)),
                            "缺少贴图 %s" % (name,))

    def test_binding_names_share_the_device_status_prefix(self):
        names = [value for key, value in vars(device_status).items()
                 if key.endswith("_BINDING") and isinstance(value, str)]
        self.assertGreaterEqual(len(names), 11)
        for name in names:
            self.assertTrue(name.startswith(device_status.BINDING_PREFIX), name)

    def test_the_master_reason_set_only_holds_controller_side_codes(self):
        self.assertIn(Reason.MASTER_ABSENT, device_status.MASTER_REASONS)
        self.assertNotIn(Reason.BROWNOUT, device_status.MASTER_REASONS)
        self.assertNotIn(Reason.REBUILDING, device_status.MASTER_REASONS)


class TestDiagnostics(unittest.TestCase):
    def test_the_report_is_json_serializable(self):
        model = device_status.DeviceStatusModel(REF)
        model.apply(message())
        model.open_popup()
        report = model.diagnostics()
        self.assertEqual(report["accepted"], 1)
        self.assertTrue(report["has_status"])
        self.assertTrue(report["popup_open"])
        json.dumps(report)


if __name__ == "__main__":
    unittest.main()
