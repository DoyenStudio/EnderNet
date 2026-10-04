# coding=utf-8
"""订单记录服务端确认的发起者身份。"""

import unittest

import testsupport  # noqa: F401
from endernet.common.protocol import STATUS_OK
from endernet.server.crafting.jobs import Order
from test_executor import OneLevel, BLOCK, IRON


class TestOrderPlayerId(unittest.TestCase):
    def test_identity_is_in_views_and_persistence_summary(self):
        order = Order("order@1/m1/1", 1, "m1", BLOCK, BLOCK.canonical(), 1,
                      player_id="player-initiator")
        self.assertEqual(order.player_id, "player-initiator")
        self.assertEqual(order.to_dict()["player_id"], "player-initiator")
        self.assertEqual(order.book_entry()["player_id"], "player-initiator")

    def test_existing_callers_default_to_unknown_initiator(self):
        order = Order("order@1/m1/1", 1, "m1", BLOCK, BLOCK.canonical(), 1)
        self.assertEqual(order.player_id, "")
        self.assertEqual(order.to_dict()["player_id"], "")
        self.assertEqual(order.book_entry()["player_id"], "")


class TestSubmitPlayerId(OneLevel):
    def test_submit_records_source_identity_instead_of_payload(self):
        self.seed(IRON, 4)
        command = self.order_command("submit", target=BLOCK.to_dict(), amount=1,
                                     player_id="spoofed-player")
        command.source.player_id = "actual-player"
        result = self.server.submit_command(command)
        self.assertEqual(result.status, STATUS_OK, result.to_dict())
        order = self.service.orders.get(result.payload["order_id"])
        self.assertEqual(order.player_id, "actual-player")
        self.assertEqual(result.payload["player_id"], "actual-player")
        self.server.tick()
        self.assertEqual(order.player_id, "actual-player")
