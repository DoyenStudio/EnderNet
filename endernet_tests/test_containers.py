# coding=utf-8
"""13-01 验收：统一容器契约与假适配器。

最重要的一条：**不能把容器声称的结果当作守恒依据**。
架构规划 6.3 点名 `PutItemIntoContainer` 在空槽路径会无视返回值和堆叠上限
直接报告「全部放入」；`GhostContainer` 复刻了这个形态，
用来证明契约层确实会以实际变化量为准。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.protocol import Reason, TransferStatus
from endernet.common.resource_key import ResourceKey
from endernet.server.adapters.containers import (
    ContainerEntry,
    FakeContainer,
    GhostContainer,
    InvalidatableContainer,
    NoNotificationContainer,
    NoSimulateContainer,
    PartialContainer,
    ReadOnlyContainer,
    RefusingContainer,
    UncertainContainer,
    UnverifiableContainer,
)

IRON = ResourceKey.item("minecraft:iron_ingot")
GOLD = ResourceKey.item("minecraft:gold_ingot")


class TestConservationByActualAmount(unittest.TestCase):
    """回执里的 moved 必须是真实发生了的数量。"""

    def test_full_insert_reports_complete(self):
        container = FakeContainer(slots=2, max_stack=64)
        receipt = container.insert("op-1", IRON, 10)
        self.assertEqual(receipt.status, TransferStatus.COMPLETE)
        self.assertEqual(receipt.moved, 10)
        self.assertEqual(container.count_of(IRON), 10)

    def test_partial_insert_reports_partial_with_actual(self):
        """容量不足时只能放入一部分，回执必须照实说。"""
        container = FakeContainer(slots=1, max_stack=8)
        receipt = container.insert("op-1", IRON, 20)
        self.assertEqual(receipt.status, TransferStatus.PARTIAL)
        self.assertEqual(receipt.moved, 8)
        self.assertEqual(receipt.remainder, 12)
        self.assertEqual(container.count_of(IRON), 8)

    def test_refused_insert_moves_nothing(self):
        container = RefusingContainer(slots=1)
        receipt = container.insert("op-1", IRON, 5)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(container.count_of(IRON), 0)

    def test_read_only_container_refuses_insert(self):
        container = ReadOnlyContainer(slots=1)
        receipt = container.insert("op-1", IRON, 5)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.reason, Reason.CONTAINER_REFUSED)

    def test_extract_more_than_available(self):
        container = FakeContainer(slots=1)
        container.insert("op-1", IRON, 3)
        receipt = container.extract("op-2", IRON, 10)
        self.assertEqual(receipt.status, TransferStatus.PARTIAL)
        self.assertEqual(receipt.moved, 3)
        self.assertEqual(container.count_of(IRON), 0)

    def test_extract_from_empty_container(self):
        container = FakeContainer(slots=1)
        receipt = container.extract("op-1", IRON, 4)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)


class TestLyingContainerIsCaught(unittest.TestCase):
    """声称成功但实际没放进去的容器，必须被契约层揭穿。"""

    def test_ghost_reports_success_but_adapter_reports_truth(self):
        ghost = GhostContainer(accept_into_store=2, slots=2, max_stack=64)
        receipt = ghost.insert("op-1", IRON, 10)
        # 容器内部确实只存了 2 个
        self.assertEqual(ghost.count_of(IRON), 2)
        # 契约层对外只报实际值，而不是容器声称的 10
        self.assertEqual(receipt.moved, 2)
        self.assertEqual(receipt.status, TransferStatus.PARTIAL)
        self.assertEqual(ghost.ghost_reports, 1)

    def test_ghost_storing_nothing_is_reported_as_refused(self):
        ghost = GhostContainer(accept_into_store=0, slots=2)
        receipt = ghost.insert("op-1", IRON, 10)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)

    def test_actual_amount_never_exceeds_request(self):
        ghost = GhostContainer(accept_into_store=64, slots=4, max_stack=64)
        receipt = ghost.insert("op-1", IRON, 4)
        self.assertLessEqual(receipt.moved, receipt.requested)


class TestUnverifiableAndUncertain(unittest.TestCase):
    """不能核对/结果不明时，必须说不确定，而不是声称成功。"""

    def test_unverifiable_insert_is_uncertain(self):
        container = UnverifiableContainer(slots=1)
        receipt = container.insert("op-1", IRON, 5)
        self.assertEqual(receipt.status, TransferStatus.UNCERTAIN)
        self.assertEqual(receipt.reason, Reason.CONTAINER_UNCERTAIN)

    def test_uncertain_insert_stays_uncertain(self):
        container = UncertainContainer(slots=1)
        receipt = container.insert("op-1", IRON, 5)
        self.assertEqual(receipt.status, TransferStatus.UNCERTAIN)

    def test_uncertain_extract_stays_uncertain(self):
        container = UncertainContainer(slots=1)
        receipt = container.extract("op-1", IRON, 5)
        self.assertEqual(receipt.status, TransferStatus.UNCERTAIN)

    def test_uncertain_does_not_claim_completion(self):
        """关键：不确定绝不能被当成完成，否则恢复流程会以为已落袋。"""
        container = UncertainContainer(slots=1)
        receipt = container.insert("op-1", IRON, 5)
        self.assertFalse(receipt.is_complete())

    def test_unverifiable_count_of_is_none_not_zero(self):
        """未知不是 0；返回 0 会被上层当成「确实没有」。"""
        container = UnverifiableContainer(slots=1)
        self.assertIsNone(container.count_of(IRON))


class TestCapabilities(unittest.TestCase):
    """能力声明必须被调用方真正尊重。"""

    def test_default_capabilities_are_conservative(self):
        from endernet.server.adapters.containers import ContainerAdapter

        caps = ContainerAdapter().capabilities()
        self.assertFalse(caps.can_simulate)
        self.assertFalse(caps.supports_partial)
        self.assertFalse(caps.is_atomic)
        self.assertIsNone(caps.notify_token)

    def test_no_simulate_returns_none_not_zero(self):
        """没有模拟能力时，「能收多少」是未知，不是 0。"""
        container = NoSimulateContainer(slots=1, max_stack=64)
        self.assertIsNone(container.can_accept(IRON, 10))

    def test_simulate_does_not_modify_container(self):
        container = FakeContainer(slots=1, max_stack=8)
        room = container.can_accept(IRON, 20)
        self.assertEqual(room, 8)
        self.assertEqual(container.count_of(IRON), 0)

    def test_partial_container_declares_partial_support(self):
        container = PartialContainer(per_op_limit=3, slots=4, max_stack=64)
        self.assertTrue(container.capabilities().supports_partial)
        receipt = container.insert("op-1", IRON, 10)
        self.assertEqual(receipt.moved, 3)

    def test_no_notification_container_exposes_none_token(self):
        container = NoNotificationContainer(slots=1)
        self.assertIsNone(container.notify_token())
        self.assertFalse(container.capabilities().to_dict()["has_notification"])

    def test_notifying_container_exposes_token(self):
        container = FakeContainer(slots=1)
        self.assertIsNotNone(container.notify_token())

    def test_read_only_capability(self):
        self.assertFalse(ReadOnlyContainer(slots=1).capabilities().can_insert)

    def test_describe_is_serializable(self):
        import json

        payload = json.dumps(FakeContainer(slots=1).describe())
        self.assertIn("fake", payload)


class TestLifecycle(unittest.TestCase):
    """13-05：邻接容器变化、卸载后缓存失效。"""

    def test_invalidate_marks_invalid(self):
        container = InvalidatableContainer(slots=1)
        self.assertTrue(container.is_valid())
        container.invalidate()
        self.assertFalse(container.is_valid())

    def test_invalidated_container_refuses_read(self):
        container = InvalidatableContainer(slots=1)
        container.insert("op-1", IRON, 1)
        container.invalidate()
        with self.assertRaises(IOError):
            container.entries()

    def test_entries_lists_only_non_empty_slots(self):
        container = FakeContainer(slots=3)
        container.set_slot(1, ContainerEntry(GOLD, 5))
        entries = container.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0], 1)
        self.assertEqual(entries[0][1].resource_key, GOLD)


class TestEntriesAndIdentity(unittest.TestCase):
    def test_entry_requires_resource_key(self):
        with self.assertRaises(TypeError):
            ContainerEntry("minecraft:iron_ingot", 1)

    def test_entry_rejects_negative_count(self):
        with self.assertRaises(ValueError):
            ContainerEntry(IRON, -1)

    def test_entries_with_different_identity_are_separate_slots(self):
        """同名不同附魔的物品不得被并成一格（复用 01-02 的身份规则）。"""
        plain = ResourceKey.item("minecraft:diamond_sword")
        enchanted = ResourceKey.item("minecraft:diamond_sword", enchant_data=[(9, 5)])
        container = FakeContainer(slots=4)
        container.insert("op-1", plain, 1)
        container.insert("op-2", enchanted, 1)
        self.assertEqual(len(container.entries()), 2)

    def test_negative_operation_rejected(self):
        with self.assertRaises(ValueError):
            FakeContainer(slots=1).insert("op-1", IRON, -1)
        with self.assertRaises(ValueError):
            FakeContainer(slots=1).extract("op-1", IRON, -1)


class TestReceiptShape(unittest.TestCase):
    """回执字段与 06 对齐，可直接参与库存记账。"""

    def test_receipt_carries_resource_and_counts(self):
        container = FakeContainer(slots=1)
        receipt = container.insert("op-1", IRON, 5)
        self.assertEqual(receipt.op_id, "op-1")
        self.assertEqual(receipt.requested, 5)
        self.assertEqual(receipt.moved, 5)
        self.assertEqual(receipt.resource_key, IRON)

    def test_receipt_serializes(self):
        import json

        receipt = FakeContainer(slots=1).insert("op-1", IRON, 5)
        payload = json.loads(json.dumps(receipt.to_dict()))
        self.assertEqual(payload["op_id"], "op-1")
        self.assertEqual(payload["resource_key"]["name"], "minecraft:iron_ingot")

    def test_moved_cannot_exceed_requested(self):
        from endernet.common.protocol import TransferReceipt

        with self.assertRaises(ValueError):
            TransferReceipt("op-1", 5, 6, TransferStatus.COMPLETE)


if __name__ == "__main__":
    unittest.main()
