# coding=utf-8
"""伪原版交互的服务端一半（`server/interaction/`，2026-11-10）：再拦一道原版物品使用的规则表、
扣 / 退 / 给物品、放成功后的放置音效，以及音效命令的引擎出口。

客户端那一半（一次按下、接管后整次按下都拦、射线、挥手）在 `test_cable_intents.TestTakeover`
与 `TestPressByPress`；扳手与面板经这一层的整条链路在 `test_cable_intents` / `test_cable_panels`。
"""

import unittest

import testsupport  # noqa: F401  (把脚本目录加进 sys.path)

from endernet.server.adapters.interaction import sound_command
from endernet.server.interaction.feedback import PLACE_SOUND, PlaceFeedback
from endernet.server.interaction.guard import UseGuard
from endernet.server.interaction.items import ItemExchange
from endernet.server.interaction.opening import BlockUse, Openers, OpenTarget
from test_cable_states import key

PLAYER = "-4294967295"


def use(item, block, click=None, eye=None):
    return BlockUse(PLAYER, 0, (1, 64, 2), block, item, click=click, eye=eye)


class Players(object):
    def __init__(self, creative=False, take_ok=True):
        self.creative = creative
        self.take_ok = take_ok
        self.taken = []

    def is_creative(self, player_id):
        if self.creative is None:
            raise RuntimeError("离线")
        return self.creative

    def take_held_item(self, player_id, item_id):
        if not self.take_ok:
            return False
        self.taken.append(item_id)
        return True


class World(object):
    def __init__(self, space=None):
        self.space = space
        self.given = []
        self.dropped = []

    def give_items(self, player_id, item_ids):
        given = list(item_ids) if self.space is None else list(item_ids)[:self.space]
        self.given.append((player_id, given))
        return list(item_ids)[len(given):]

    def drop_items_at(self, at, items):
        self.dropped.append((at, [(resource.name, count) for resource, count in items]))


class TestUseGuard(unittest.TestCase):
    def test_rules_match_block_and_item(self):
        guard = UseGuard()
        guard.forbid("a", ["x:block"], ["x:item"])
        guard.forbid("b", ["y:block"])
        never = lambda: self.fail("不该读潜行")  # noqa: E731
        self.assertEqual(guard.check(use("x:item", "x:block"), lambda: False), "a")
        self.assertIsNone(guard.check(use("x:other", "x:block"), never))
        self.assertIsNone(guard.check(use("x:item", "z:block"), never))
        self.assertEqual(guard.check(use("", "y:block"), lambda: False), "b",
                         "None = 任何物品（含空手）")
        self.assertEqual(guard.diagnostics()["cancelled"], {"a": 1, "b": 1})

    def test_any_block_and_a_last_question(self):
        """方块给 None = 任何方块；`when` 在方块与物品都对上之后才问，问过才读潜行。"""
        asked = []
        guard = UseGuard()
        guard.forbid("item", items=["x:item"])
        guard.forbid("when", when=lambda u: asked.append(u.block) or u.block == "y:block")
        never = lambda: self.fail("不该读潜行")  # noqa: E731
        self.assertEqual(guard.check(use("x:item", "anything"), lambda: False), "item")
        self.assertEqual(asked, [], "前一条已命中")
        self.assertIsNone(guard.check(use("", "z:block"), never))
        self.assertEqual(guard.check(use("", "y:block"), lambda: False), "when")
        self.assertEqual(asked, ["z:block", "y:block"])

    def test_sneaking_lets_it_through_unless_told_otherwise(self):
        guard = UseGuard()
        guard.forbid("soft", ["x:block"])
        guard.forbid("hard", ["y:block"], unless_sneaking=False)
        self.assertIsNone(guard.check(use("i", "x:block"), lambda: True))
        self.assertEqual(guard.check(use("i", "y:block"), lambda: self.fail("不该读")), "hard")

    def test_registering_the_same_name_replaces_it(self):
        guard = UseGuard()
        guard.forbid("a", ["x:block"])
        guard.forbid("b", ["y:block"])
        guard.forbid("a", ["z:block"])
        self.assertEqual(guard.rules(), ["b", "a"])
        self.assertIsNone(guard.check(use("i", "x:block"), lambda: False))
        self.assertEqual(guard.check(use("i", "z:block"), lambda: False), "a")


class TestOpening(unittest.TestCase):
    """右键开界面的唯一入口（`server/interaction/opening.py`）：一次右键的事实与入口表。"""

    def test_the_ray_runs_from_the_eye_through_the_click_point_and_is_read_once(self):
        reads = []

        def eye(player_id):
            reads.append(player_id)
            return (4.0, 64.5, 2.5)

        one = use("", "x:block", click=(1.0, 0.5, 0.5), eye=eye)
        origin, direction = one.ray()
        self.assertEqual(origin, (4.0, 64.5, 2.5))
        self.assertEqual(direction, (-1.0, 0.0, 0.0), "命中点 (2, 64.5, 2.5) 在眼睛正西")
        self.assertIs(one.ray(), one.ray())
        self.assertEqual(reads, [PLAYER])
        self.assertEqual(one.node_key(), key(1, 64, 2))

    def test_no_click_or_no_eye_means_no_ray(self):
        self.assertIsNone(use("", "x:block").ray(), "事件没带命中点")
        self.assertIsNone(use("", "x:block", click=(0.5, 0.5, 0.5)).ray(), "没有读眼睛的入口")
        self.assertIsNone(use("", "x:block", click=(0.5, 0.5, 0.5), eye=lambda p: None).ray())
        self.assertIsNone(use("", "x:block", click=(0.5, 0.5, 0.5), eye=lambda p: 1 / 0).ray())

    def test_the_first_opener_that_claims_wins(self):
        openers = Openers()
        openers.add("a", lambda u: OpenTarget("screen.a") if u.block == "a" else None)
        openers.add("b", lambda u: OpenTarget() if u.block in ("a", "b") else None)
        self.assertEqual(openers.claim(use("", "a")).ui_key, "screen.a")
        claimed = openers.claim(use("", "b"))
        self.assertFalse(claimed.opens, "认领了、这次打不开")
        self.assertIsNone(openers.claim(use("", "c")))
        openers.add("a", lambda u: None)
        self.assertEqual(openers.names(), ["b", "a"], "同名再登记就覆盖")
        self.assertFalse(openers.claim(use("", "a")).opens)

    def test_a_broken_opener_counts_as_not_claiming(self):
        openers = Openers()
        openers.add("boom", lambda u: 1 / 0)
        openers.add("ok", lambda u: OpenTarget("screen"))
        self.assertEqual(openers.claim(use("", "x")).ui_key, "screen")
        self.assertEqual(openers.diagnostics()["claim_errors"], 1)


class TestItemExchange(unittest.TestCase):
    def test_withdraw_and_refund_in_survival(self):
        players, world = Players(), World()
        items = ItemExchange(players, world)
        taken = items.withdraw(PLAYER, "endernet:ender_cable")
        self.assertTrue(taken.charged)
        self.assertEqual(players.taken, ["endernet:ender_cable"])
        items.refund(taken, 0, (3.7, 64.0, -0.2))
        self.assertEqual(world.given, [(PLAYER, ["endernet:ender_cable"])])
        self.assertEqual(items.stats["refunded"], 1)

    def test_creative_takes_nothing_and_refunds_nothing(self):
        players, world = Players(creative=True), World()
        items = ItemExchange(players, world)
        taken = items.withdraw(PLAYER, "endernet:ender_cable")
        self.assertFalse(taken.charged)
        items.refund(taken, 0, (0, 64, 0))
        self.assertEqual((players.taken, world.given), ([], []))

    def test_unknown_mode_is_survival_and_a_missing_item_is_refused(self):
        items = ItemExchange(Players(creative=None, take_ok=False), World())
        self.assertIsNone(items.withdraw(PLAYER, "endernet:ender_cable"))
        self.assertEqual(items.stats["take_failures"], 1)
        items.refund(None, 0, (0, 64, 0))

    def test_a_full_inventory_drops_at_the_feet(self):
        world = World(space=1)
        items = ItemExchange(Players(), world)
        items.hand_over(PLAYER, ["a:one", "a:two", "a:three"], 0, (3.7, 64.0, -0.2))
        self.assertEqual(world.given, [(PLAYER, ["a:one"])])
        self.assertEqual(world.dropped, [((0, 3, 64, -1), [("a:two", 1), ("a:three", 1)])])
        self.assertEqual((items.stats["given"], items.stats["dropped"]), (1, 2))


class TestPlaceFeedback(unittest.TestCase):
    def test_the_vanilla_stone_place_sound_at_the_cell_center(self):
        self.assertEqual(PLACE_SOUND, ("dig.stone", 1.0, 0.9))
        played = []
        feedback = PlaceFeedback(play_sound=lambda *args: played.append(args))
        self.assertTrue(feedback.placed(PLAYER, key(2, z=-3)))
        self.assertEqual(played, [(PLAYER, "dig.stone", (2.5, 64.5, -2.5), 1.0, 0.9)])

    def test_without_an_engine_it_only_counts(self):
        feedback = PlaceFeedback()
        self.assertFalse(feedback.placed(PLAYER, key(0)))
        self.assertEqual(feedback.stats, {"placed": 1, "played": 0, "play_failures": 0})
        failing = PlaceFeedback(play_sound=lambda *args: False)
        self.assertFalse(failing.placed(PLAYER, key(0)))
        self.assertEqual(failing.stats["play_failures"], 1)

    def test_the_sound_command(self):
        self.assertEqual(sound_command("dig.stone", (2.5, 64.5, -2.5), 1.0, 0.9),
                         "/playsound dig.stone @a 2.500 64.500 -2.500 1 0.9")


class TestClientEngineGlue(unittest.TestCase):
    def test_the_engine_view_imports_offline(self):
        from endernet.client.interaction.engine import EngineView

        view = EngineView()
        for name in ("camera_origin", "held_item_id", "sneaking", "swing"):
            self.assertTrue(callable(getattr(view, name)), name)


if __name__ == "__main__":
    unittest.main()
