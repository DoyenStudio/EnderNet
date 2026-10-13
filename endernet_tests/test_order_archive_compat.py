# coding=utf-8
"""新增发起者字段不阻断旧订单存档与网络恢复。"""

import json
import unittest

import testsupport  # noqa: F401
from endernet.server.crafting.jobs import Order
from endernet.server.crafting.order_archive import export_order, import_order
from test_executor import OneLevel, BLOCK, IRON
from test_order_recovery import RestartMixin


class TestOrderArchivePlayerId(unittest.TestCase):
    def snapshot(self):
        order = Order("order@1/m1/1", 1, "m1", BLOCK, BLOCK.canonical(), 3,
                      player_id="p1")
        return json.loads(json.dumps(export_order(order)))

    def test_current_snapshot_preserves_initiator(self):
        order = import_order(self.snapshot(), 2, "m2")
        self.assertEqual(order.player_id, "p1")
        self.assertEqual(order.book_entry()["player_id"], "p1")

    def test_legacy_snapshot_defaults_to_unknown_without_mutating_archive(self):
        data = self.snapshot()
        del data["player_id"]
        order = import_order(data, 2, "m2")
        self.assertEqual(order.player_id, "")
        self.assertEqual(order.amount, 3)
        self.assertEqual(order.target, BLOCK)
        self.assertNotIn("player_id", data)

    def test_other_missing_required_fields_still_fail(self):
        data = self.snapshot()
        del data["amount"]
        with self.assertRaises(KeyError):
            import_order(data, 2, "m2")


class TestLegacyOrderRecovery(RestartMixin, OneLevel):
    def test_legacy_initiator_does_not_keep_recovery_gate_closed(self):
        self.layout = [(record.ref.node_key, record.device_id)
                       for record in self.server.registry.all_records()]
        self.seed(IRON, 20)
        order_id = self.submit_order(amount=3).payload["order_id"]

        def legacy(repo):
            for kind in list(repo._store):
                for record_id in repo.list_ids(kind):
                    record = repo.load(kind, record_id)
                    book = record.payload.get("orders", {})
                    for row in book.get("resume_orders", ()):
                        row.pop("player_id", None)
                    repo.write(record)

        self.restart(mutate=legacy)
        self.assertTrue(self.server.recovery.is_open(),
                        self.server.recovery.diagnostics())
        order = self.service.orders.get(order_id)
        self.assertEqual(order.player_id, "")
        self.settle(order_id)
        self.assertTrue(order.adopted)
