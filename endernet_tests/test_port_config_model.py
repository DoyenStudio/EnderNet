# coding=utf-8
"""接口配置的客户端模型与两台物品口的接线（11-07）。**全部离线。**

这个文件守的是三件在游戏里几乎看不出来的事：

- **旧代次的推送不得喂进新界面**——拆掉又在原地放一台时，症状是界面画着
  上一台设备的配置，而玩家没有任何理由怀疑它；
- **被拒绝的提交必须退回权威值**——不退回的话界面永久停在错的状态上，
  那正是 11 反复记的「界面显示一次没发生的存取」；
- **意图的线上形状**——`filter` 那一项必须是**资源键字典**，塞字符串或
  `ResourceKey` 对象进去要到发包那一刻才炸（`can not serialize`）。

第三条尤其要离线钉住：真机上那一步什么都不显示，只有引擎日志里一行。
"""

import unittest

import testsupport  # noqa: F401  (放进 sys.path)

from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.protocol import PortConfigMessage
from endernet.common.resource_key import ResourceKey
from endernet.client.ui import port_config_model
from endernet.client.ui.port_config_model import (
    BARRIER_ITEM,
    MODE_ICON_SIZE,
    PortConfigModel,
)

OUTPUT = "endernet:bus_item_output_io"


def config(**overrides):
    """一份 `PortConfig.to_dict()` 形状的配置（照领域层的字段清单）。"""
    fields = dict(
        enabled=True,
        direction="both",
        filter_mode="whitelist",
        filter=[],
        supply_slots=[],
        limit=0,
        cache_enabled=True,
    )
    fields.update(overrides)
    return fields


def message(ref, config_overrides=None, **fields):
    """一条推送。**配置项与信封字段分开传**：前者进 `config` 那一层，
    后者是 `filter_limit` / `device_id` 这类窗口级字段——两者混在一个
    `**kwargs` 里时，写成 `filter=[...]` 会静默地什么也不改（那正是本文件
    第一版踩到的坑，两个用例因此测的是空配置）。
    """
    payload = config()
    if config_overrides:
        payload.update(config_overrides)
    defaults = dict(
        device_id=OUTPUT,
        generation=ref.generation,
        role="output",
        config=payload,
        filter_limit=9,
        filter_modes=["whitelist"],
        blocker=None,
        buffer_summary=None,
    )
    defaults.update(fields)
    return PortConfigMessage(
        defaults["device_id"], defaults["generation"], defaults["role"],
        defaults["config"], defaults["filter_limit"],
        filter_modes=defaults["filter_modes"], blocker=defaults["blocker"],
        buffer_summary=defaults["buffer_summary"],
    )


class ModelFixture(unittest.TestCase):
    def setUp(self):
        self.ref = DeviceRef(NodeKey(0, 10, 64, 0), 1)
        self.model = PortConfigModel(self.ref)

    def apply(self, config_overrides=None, **fields):
        self.assertTrue(self.model.apply(
            message(self.ref, config_overrides, **fields)))


class TestAuthority(ModelFixture):
    def test_nothing_is_shown_before_the_first_push(self):
        """没收到过权威值 ≠ 空配置。

        界面按「还不知道」处理（`ready()` 为假时那几条交互自己跳过），
        而不是画一份「什么都没配」——后者会让玩家去改一台他还没看过的设备。
        """
        self.assertFalse(self.model.ready())
        self.assertEqual(self.model.filter_names(), [])

    def test_the_first_push_fills_both_sides(self):
        self.apply(config_overrides={
            "filter": [ResourceKey.item("minecraft:stone").to_dict()]})
        self.assertTrue(self.model.ready())
        self.assertEqual(self.model.filter_names(), ["minecraft:stone"])
        self.assertTrue(self.model.cache_enabled())
        self.assertEqual(self.model.filter_limit, 9)
        self.assertEqual(self.model.filter_modes, ("whitelist",))

    def test_a_push_from_another_generation_is_refused(self):
        self.apply()
        other = DeviceRef(self.ref.node_key, 9)
        self.assertFalse(self.model.apply(message(other)))
        self.assertEqual(self.model.stats["stale"], 1)
        # 权威值仍是第一台那一份，没有被顶掉。
        self.assertEqual(self.model.stats["applied"], 1)

    def test_a_push_from_another_device_is_refused(self):
        self.apply()
        self.assertFalse(self.model.apply(
            message(self.ref, device_id="endernet:bus_item_input_io")))
        self.assertEqual(self.model.stats["stale"], 1)

    def test_the_filter_limit_comes_from_the_server(self):
        """界面不硬编码上限：存货口那个数尚未定案（服务端才是它的事实来源）。"""
        self.apply(filter_limit=16)
        self.assertEqual(self.model.filter_limit, 16)


class TestIntent(unittest.TestCase):
    def setUp(self):
        self.ref = DeviceRef(NodeKey(0, 10, 64, 0), 1)
        self.model = PortConfigModel(self.ref)
        self.model.apply(message(self.ref))

    def test_the_filter_intent_is_a_list_of_resource_key_dicts(self):
        """**线上形状在客户端一次成型**：服务端读的就是这个形状。"""
        intent = self.model.intent_filter(["minecraft:stone", "minecraft:dirt"])
        self.assertEqual(list(intent), ["filter"])
        self.assertEqual(len(intent["filter"]), 2)
        self.assertEqual(intent["filter"][0]["kind"], "item")
        self.assertEqual(intent["filter"][0]["name"], "minecraft:stone")
        # 与 `ResourceKey.to_dict()` 逐字相同——两边形状不同就得在服务端转换，
        # 而多一层转换就多一处会漂移的地方。
        self.assertEqual(intent["filter"][0], ResourceKey.item("minecraft:stone").to_dict())

    def test_the_other_intents_carry_only_the_field_they_change(self):
        """**只写要改的那一项**：服务端合并，整体替换会把没提到的打回默认值。"""
        self.assertEqual(self.model.intent_cache(False), {"cache_enabled": False})
        self.assertEqual(self.model.intent_mode("blacklist"), {"filter_mode": "blacklist"})


class TestPendingAndSettle(unittest.TestCase):
    def setUp(self):
        self.ref = DeviceRef(NodeKey(0, 10, 64, 0), 1)
        self.model = PortConfigModel(self.ref)
        self.model.apply(message(self.ref))

    def test_an_expectation_shows_immediately_and_gates_the_field(self):
        self.model.expect("c1", self.model.intent_cache(False))
        # 乐观值先生效（玩家拨一下就看得见），而权威值一个字没动。
        self.assertFalse(self.model.cache_enabled())
        self.assertTrue(self.model.applied["cache_enabled"])
        self.assertTrue(self.model.busy("cache_enabled"))
        self.assertFalse(self.model.busy("filter"))
        self.assertEqual(self.model.pending_count(), 1)

    def test_a_successful_receipt_takes_the_authority_from_the_payload(self):
        """回执里的配置是**服务端合并之后**的结果，客户端那份只是它的一部分。"""
        self.model.expect("c1", self.model.intent_cache(False))
        merged = config(cache_enabled=False, filter_mode="whitelist", limit=4)
        self.assertTrue(self.model.settle("c1", config=merged))
        self.assertEqual(self.model.cache_enabled(), False)
        self.assertEqual(self.model.value("limit"), 4)
        self.assertEqual(self.model.pending_count(), 0)
        self.assertEqual(self.model.notice, "")

    def test_a_refused_receipt_rolls_back_to_the_authority(self):
        self.model.expect("c1", self.model.intent_cache(False))
        self.assertTrue(self.model.settle("c1", config=None, detail="第 10 种被拒"))
        self.assertTrue(self.model.cache_enabled())
        self.assertEqual(self.model.notice, "第 10 种被拒")
        self.assertEqual(self.model.stats["rolled_back"], 1)
        self.assertFalse(self.model.busy("cache_enabled"))

    def test_an_unknown_receipt_is_ignored(self):
        """不是我们发的回执（迟到、重复、别的界面）不得改这一份状态。"""
        self.assertFalse(self.model.settle("c-never", config=config(cache_enabled=False)))
        self.assertEqual(self.model.pending_count(), 0)
        self.assertTrue(self.model.cache_enabled())


class TestPresentation(ModelFixture):
    def test_the_empty_filter_draws_a_barrier(self):
        self.apply()
        name, aux, text = self.model.filter_summary()
        self.assertEqual(name, BARRIER_ITEM)
        self.assertEqual(aux, 0)
        self.assertEqual(text, "未设定物品")

    def test_a_single_entry_shows_its_own_name(self):
        self.apply(config_overrides={
            "filter": [ResourceKey.item("minecraft:stone").to_dict()]})
        self.assertEqual(
            self.model.filter_summary(),
            ("minecraft:stone", 0, "minecraft:stone"))

    def test_several_entries_fall_back_to_the_stand_in_summary(self):
        """多项摘要策略**尚未定案**（UI 交接第 8 节），这里取的是替身口径。"""
        self.apply(config_overrides={
            "filter": [ResourceKey.item(name).to_dict()
                       for name in ("minecraft:stone", "minecraft:dirt")]})
        name, _aux, text = self.model.filter_summary()
        self.assertEqual(name, "minecraft:stone")
        self.assertTrue(text.startswith("已设定 2 "), text)

    def test_the_mode_visual_keeps_the_icon_and_the_text_together(self):
        """图标 uv 与文字必须同一次取——分开取就会图与字分家。"""
        uv, size, text = self.model.mode_visual()
        self.assertEqual((uv, text), ([0, 0], "白名单"))
        self.assertEqual(size, MODE_ICON_SIZE)
        self.apply(config_overrides={"filter_mode": "blacklist"})
        uv, _size, text = self.model.mode_visual()
        self.assertEqual((uv, text), ([16, 0], "黑名单"))

    def test_next_mode_only_rotates_inside_what_the_server_allows(self):
        """取货口只允许白名单（`ROLE_FILTER_MODES`），切换因此原地不动。"""
        self.apply(filter_modes=["whitelist"])
        self.assertEqual(self.model.next_mode(), "whitelist")
        self.apply(config_overrides={"filter_mode": "whitelist"},
                   filter_modes=["blacklist", "whitelist"])
        self.assertEqual(self.model.next_mode(), "blacklist")
        self.assertEqual(self.model.mode_visual()[2], "白名单")

    def test_a_mode_outside_the_allowed_set_falls_back_to_the_first(self):
        self.apply(config_overrides={"filter_mode": "whitelist"},
                   filter_modes=["blacklist", "whitelist"])
        self.model.desired["filter_mode"] = "either"
        self.assertEqual(self.model.next_mode(), "blacklist")


class TestDiagnostics(ModelFixture):
    def test_the_report_carries_what_the_screen_cannot_show(self):
        """失败原因今天**没有显示位置**（缺项），因此它至少要能在诊断里查到。"""
        self.apply()
        self.model.expect("c1", self.model.intent_cache(False))
        self.model.settle("c1", config=None, detail="配置内容不合法")
        report = self.model.diagnostics()
        self.assertEqual(report["notice"], "配置内容不合法")
        self.assertEqual(report["pending"], 0)
        self.assertEqual(report["device_id"], OUTPUT)
        self.assertTrue(report["ready"])
        self.assertEqual(report["cache_enabled"], True)
        self.assertEqual(report["rolled_back"], 1)


if __name__ == "__main__":
    unittest.main()
