# coding=utf-8
"""完成通知只发送一次，数量遵循物料单位。"""

import unittest

import testsupport  # noqa: F401
from endernet.common.resource_amount import format_resource_amount
from endernet.common.resource_key import ResourceKey
from endernet.server.adapters.order_notifications import notify_order_completed
from endernet.server.crafting.jobs import Order, OrderStatus
from test_executor import OneLevel, BLOCK, IRON


class TestResourceAmount(unittest.TestCase):
    def test_item_count(self):
        self.assertEqual(format_resource_amount("item", 64), "64")

    def test_fluid_units_become_millibuckets(self):
        self.assertEqual(format_resource_amount("fluid", 20), "1000mB")

    def test_native_fluid_count_is_not_scaled_again(self):
        self.assertEqual(format_resource_amount("fluid", 25, native=True), "25mB")

    def test_invalid_amount_and_kind(self):
        for amount in (True, 1.5):
            with self.assertRaises(TypeError):
                format_resource_amount("item", amount)
        with self.assertRaises(ValueError):
            format_resource_amount("item", -1)
        with self.assertRaises(ValueError):
            format_resource_amount("unknown", 1)


class TestNotificationAdapter(unittest.TestCase):
    def order(self, key=BLOCK, amount=3, player_id="p1"):
        return Order("order@1/m1/1", 1, "m1", key, key.canonical(), amount,
                     player_id=player_id)

    def notify(self, order, online=True):
        messages, commands = [], []
        result = notify_order_completed(
            order, message=lambda *args: messages.append(args),
            command=lambda *args: commands.append(args),
            name_of=lambda key: u"水" if key.is_fluid() else u"铁块",
            online=lambda player_id: online,
        )
        return result, messages, commands

    def test_message_and_sound_target_only_initiator(self):
        result, messages, commands = self.notify(self.order())
        self.assertTrue(result)
        self.assertEqual(messages, [("p1", "§7[§ai§7] §a订单已完成： 3x铁块".encode("utf-8"))])
        self.assertEqual(commands, [("/playsound note.endernet.order_finished", "p1")])

    def test_fluid_message_uses_native_millibuckets(self):
        result, messages, commands = self.notify(
            self.order(ResourceKey.fluid("minecraft:water"), 1000))
        self.assertTrue(result)
        self.assertEqual(messages[0][1], "§7[§ai§7] §a订单已完成： 1000mBx水".encode("utf-8"))

    def test_missing_or_offline_initiator_is_skipped(self):
        self.assertEqual(self.notify(self.order(player_id="")), (False, [], []))
        self.assertEqual(self.notify(self.order(), online=False), (False, [], []))

    def test_utf8_name_is_decoded_before_message_formatting(self):
        messages = []
        notify_order_completed(
            self.order(), message=lambda *args: messages.append(args),
            command=lambda *args: None, name_of=lambda key: u"铁块".encode("utf-8"),
            online=lambda player_id: True,
        )
        self.assertEqual(messages[0][1], "§7[§ai§7] §a订单已完成： 3x铁块".encode("utf-8"))

    def test_failed_sound_command_is_reported(self):
        with self.assertRaises(RuntimeError):
            notify_order_completed(
                self.order(), message=lambda *args: None,
                command=lambda *args: False, name_of=lambda key: "gold",
                online=lambda player_id: True,
            )


class TestCompletionNotification(OneLevel):
    def complete(self, callback):
        self.service.on_order_completed = callback
        self.seed(IRON, 4)
        order_id = self.submit_order().payload["order_id"]
        self.settle()
        self.drive(3)
        self.sink.flush(self.service.executor)
        self.drive(3)
        return self.service.orders.get(order_id)

    def test_actual_completion_notifies_once_and_releases_resources(self):
        completed = []
        order = self.complete(lambda row: completed.append(row.order_id))
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(completed, [order.order_id])
        self.assertEqual(self.service.executor._complete_if_done(order, 99), 0)
        self.drive(3)
        self.assertEqual(completed, [order.order_id])
        self.assertEqual(self.service.memory.used(), 0)
        self.assertEqual(self.service.slots.used(), 0)

    def test_notification_failure_does_not_undo_completion(self):
        def fail(order):
            raise RuntimeError("notification failed")
        order = self.complete(fail)
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(self.service.stats["notification_failed"], 1)

    def test_inventory_satisfied_order_also_notifies(self):
        from endernet.server.crafting.planner import TargetSemantics

        self.server.plan_policy.target_semantics = TargetSemantics.HOLD
        completed = []
        self.service.on_order_completed = lambda row: completed.append(row.order_id)
        self.seed(BLOCK, 1)
        order_id = self.submit_order().payload["order_id"]
        self.drive(5)
        order = self.service.orders.get(order_id)
        self.assertEqual(order.status, OrderStatus.COMPLETED)
        self.assertEqual(completed, [order.order_id])
