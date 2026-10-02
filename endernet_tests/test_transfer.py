# coding=utf-8
"""转移协调器：端点、两段式提交、实际数量回执与有界中转缓冲。

对应任务 06-01 至 06-03 的离线验证。**全部离线**：转移层不 import 任何引擎
模块，容器一侧用的是 13-01 的假适配器（`adapters/containers.py`）。

**这些用例刻意不抄被测算法。** 凡是「守恒」「不重复计入」「不确定不算成功」
这类性质，都用**独立求和**去验证：把所有端点上的数量各数一遍再加起来，
和转移前比。抄一遍实现只会一起错。

排在前面的三条是这一层的全部意义所在，改坏了它们等于这一层不存在：

1. 容器声称放进去多少不算数，`moved` 必须是真实变化量；
2. 没搬成的那部分**必须回到一个说得出名字的地方**（`holder`）；
3. `UNCERTAIN` 既不算成功，也不许重发。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.protocol import (
    ORIGIN_EXTERNAL,
    ORIGIN_INTERNAL,
    Reason,
    TransferRequest,
    TransferStatus,
)
from endernet.common.item_codec import item_dict_from_key, item_key_from_dict
from endernet.common.resource_key import ResourceKey
from endernet.server.adapters.containers_engine import SlotBackedContainer
from endernet.server.adapters.containers import (
    ContainerEntry,
    FakeContainer,
    GhostContainer,
    NoSimulateContainer,
    PartialContainer,
    ReadOnlyContainer,
    RefusingContainer,
    UncertainContainer,
    UnverifiableContainer,
)
from endernet.server.storage.transfer import (
    BufferDisposition,
    BufferEndpoint,
    ContainerEndpoint,
    NetworkEndpoint,
    SlotAnchor,
    TRANSFER_SCHEMA_VERSION,
    TransferBuffer,
    TransferCoordinator,
    TransferPolicy,
    TransferReceipt,
    TransferStatus,
    slot_extract,
    slot_insert,
)

STONE = ResourceKey.item("minecraft:stone")
DIRT = ResourceKey.item("minecraft:dirt")
WATER = ResourceKey.fluid("minecraft:water")


def fill(container, key, count, slot=0):
    """把一个假容器摆成「槽 slot 里有 count 个 key」。返回容器本身。"""
    container.set_slot(slot, ContainerEntry(key, count))
    return container


def total_everywhere(key, *endpoints):
    """**独立求和**：每个端点各数一遍再相加。

    它就是守恒判据本身——不是把协调器的记账再算一次，而是直接问每个
    真实存放处「你这里现在有多少」。缓冲用 `totals()` 也是独立的
    （它按批次累加，而不是读协调器的任何字段）。
    """
    total = 0
    for endpoint in endpoints:
        if isinstance(endpoint, ContainerEndpoint):
            total += endpoint.adapter.count_of(key) or 0
        elif isinstance(endpoint, BufferEndpoint):
            total += endpoint.buffer.total_of(key)
        else:
            raise AssertionError("独立的守恒求和认不出这种端点: %r" % (endpoint,))
    return total


class EndpointFixture(unittest.TestCase):
    """两端点 + 一个协调器的公共夹具。"""

    def setUp(self):
        self.coordinator = TransferCoordinator()
        self.source_box = FakeContainer(slots=2, max_stack=64)
        self.target_box = FakeContainer(slots=2, max_stack=64)
        self.source = ContainerEndpoint(self.source_box, "src")
        self.target = ContainerEndpoint(self.target_box, "dst")

    def request(self, count, key=STONE, op_id="op1", **kwargs):
        source_id = kwargs.pop("source_id", self.source.endpoint_id())
        target_id = kwargs.pop("target_id", self.target.endpoint_id())
        return TransferRequest(
            op_id, source_id, target_id, key, count, **kwargs
        )


# -- 06-01：预检 -----------------------------------------------------------


class TestPreflightHasNoSideEffects(EndpointFixture):
    """预检必须**什么都不改**——它是「先看看行不行」，不是「先做一半」。"""

    def test_preflight_reports_movable_without_moving(self):
        fill(self.source_box, STONE, 10)
        plan = self.coordinator.preflight(self.request(6), self.source, self.target)
        self.assertTrue(plan.is_ok())
        self.assertEqual(plan.movable, 6)
        self.assertEqual(self.source_box.count_of(STONE), 10)
        self.assertEqual(self.target_box.count_of(STONE), 0)

    def test_repeated_preflight_does_not_consume_anything(self):
        """重复预检的结果必须完全一致：它不消耗额度、不占位置。"""
        fill(self.source_box, STONE, 10)
        first = self.coordinator.preflight(self.request(6), self.source, self.target)
        second = self.coordinator.preflight(self.request(6), self.source, self.target)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_preflight_clamps_to_available_stock(self):
        fill(self.source_box, STONE, 4)
        plan = self.coordinator.preflight(self.request(9), self.source, self.target)
        self.assertEqual(plan.movable, 4)
        self.assertEqual(plan.status, TransferStatus.PARTIAL)

    def test_preflight_clamps_to_target_space(self):
        fill(self.source_box, STONE, 10)
        target = ContainerEndpoint(FakeContainer(slots=1, max_stack=8), "tiny")
        plan = self.coordinator.preflight(self.request(9), self.source, target)
        self.assertEqual(plan.movable, 8)

    def test_empty_source_is_refused_with_a_locating_reason(self):
        plan = self.coordinator.preflight(self.request(1), self.source, self.target)
        self.assertFalse(plan.is_ok())
        self.assertEqual(plan.first_reason(), Reason.INSUFFICIENT_STOCK)

    def test_full_target_is_refused_with_no_capacity(self):
        fill(self.source_box, STONE, 5)
        target = ContainerEndpoint(FakeContainer(slots=0), "full")
        plan = self.coordinator.preflight(self.request(5), self.source, target)
        self.assertFalse(plan.is_ok())
        self.assertEqual(plan.first_reason(), Reason.NO_CAPACITY)

    def test_a_read_only_target_is_unsupported(self):
        """**「不能写」与「满了」是两回事**：前者换多少个目标都没用。"""
        fill(self.source_box, STONE, 5)
        target = ContainerEndpoint(ReadOnlyContainer(slots=2), "ro")
        plan = self.coordinator.preflight(self.request(5), self.source, target)
        self.assertEqual(plan.first_reason(), Reason.UNSUPPORTED)

    def test_an_unreadable_source_is_unsupported_not_empty(self):
        """**「读不了」与「没有」是两回事。** 把不可读当成空会让玩家以为东西没了。"""
        source = ContainerEndpoint(UnverifiableContainer(slots=1), "u")
        plan = self.coordinator.preflight(self.request(1), source, self.target)
        self.assertEqual(plan.first_reason(), Reason.UNSUPPORTED)

    def test_invalid_endpoint_blocks_before_stock_is_even_considered(self):
        """端点失效排在库存之前判：一台被拆掉的设备不该先报「库存不足」。"""
        self.source.invalidate()
        plan = self.coordinator.preflight(self.request(1), self.source, self.target)
        self.assertEqual(plan.first_reason(), Reason.ENDPOINT_INVALID)

    def test_zero_count_is_out_of_range(self):
        plan = self.coordinator.preflight(
            TransferRequest("op0", "src", "dst", STONE, 0), self.source, self.target
        )
        self.assertEqual(plan.first_reason(), Reason.OUT_OF_RANGE)

    def test_unknown_space_still_plans_the_full_amount(self):
        """目标不能模拟时**不当作 0**（那会让取货口永远不动），按请求量去试。"""
        fill(self.source_box, STONE, 5)
        target = ContainerEndpoint(NoSimulateContainer(slots=1), "n")
        plan = self.coordinator.preflight(self.request(5), self.source, target)
        self.assertTrue(plan.is_ok())
        self.assertEqual(plan.movable, 5)


# -- 06-02：实际数量回执 ---------------------------------------------------


class TestAStalePlanIsClamped(EndpointFixture):
    """调用方给的 plan **一定要用新预检夹一次**。

    不夹的话，一份算好之后放了一会儿的 plan 会拿着过期的 `movable` 去动手
    ——源被搬空了、目标被塞满了，它都不知道。
    """

    def test_a_plan_that_has_gone_stale_is_reclamped(self):
        fill(self.source_box, STONE, 6)
        plan = self.coordinator.preflight(self.request(6), self.source, self.target)
        self.assertEqual(plan.movable, 6)
        # plan 算完之后源被抽干了：动 6 个是不可能的。
        self.source_box._do_extract("drain", STONE, 6)
        receipt = self.coordinator.commit(
            self.request(6), self.source, self.target, plan=plan
        )
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(self.target_box.count_of(STONE), 0)

    def test_a_plan_smaller_than_reality_is_respected(self):
        """夹只往小里夹：调用方主动要少搬一点是合法的意图。"""
        fill(self.source_box, STONE, 6)
        plan = self.coordinator.preflight(self.request(3), self.source, self.target)
        receipt = self.coordinator.commit(
            self.request(6), self.source, self.target, plan=plan
        )
        self.assertEqual(receipt.moved, 3)
        self.assertEqual(self.target_box.count_of(STONE), 3)


class TestActualAmountIsTheOnlyTruth(EndpointFixture):
    """`moved` 必须是**真实变化量**，不是请求量、也不是容器声称的量。"""

    def test_complete_transfer(self):
        fill(self.source_box, STONE, 10)
        before = total_everywhere(STONE, self.source, self.target)
        receipt = self.coordinator.commit(self.request(6), self.source, self.target)
        self.assertEqual(receipt.status, TransferStatus.COMPLETE)
        self.assertEqual(receipt.moved, 6)
        self.assertEqual(receipt.remainder, 0)
        self.assertEqual(receipt.holder, "dst")
        self.assertEqual(total_everywhere(STONE, self.source, self.target), before)

    def test_partial_transfer_reports_the_real_amount(self):
        fill(self.source_box, STONE, 10)
        target = ContainerEndpoint(PartialContainer(per_op_limit=3, slots=2), "p")
        receipt = self.coordinator.commit(self.request(10), self.source, target)
        self.assertEqual(receipt.status, TransferStatus.PARTIAL)
        self.assertEqual(receipt.moved, 3)
        self.assertEqual(receipt.remainder, 7)

    def test_refused_transfer_moves_nothing(self):
        fill(self.source_box, STONE, 10)
        target = ContainerEndpoint(RefusingContainer(slots=2), "r")
        receipt = self.coordinator.commit(self.request(10), self.source, target)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(self.source_box.count_of(STONE), 10)

    def test_a_lying_container_is_caught_and_the_leftover_goes_back(self):
        """**这一条是 13-01 的 `GhostContainer` 存在的理由。**

        它复制了 `PutItemIntoContainer` 的缺陷形态：声称全部放入、实际只收
        一部分。契约层核对出真实数量 3，协调器把剩下的 7 **退回来源端**，
        并在回执里指出余量现在归谁。
        """
        fill(self.source_box, STONE, 10)
        ghost = GhostContainer(accept_into_store=3, slots=1)
        target = ContainerEndpoint(ghost, "ghost")
        receipt = self.coordinator.commit(self.request(10), self.source, target)
        self.assertEqual(receipt.moved, 3, "容器声称 10，回执必须是真实的 3")
        self.assertEqual(receipt.status, TransferStatus.PARTIAL)
        # 7 个退回了来源：来源从 10 变成 7，目标 3，总数仍是 10。
        self.assertEqual(self.source_box.count_of(STONE), 7)
        self.assertEqual(ghost.count_of(STONE), 3)
        self.assertEqual(receipt.holder, "src", "余量必须说得清在谁手上")

    def test_an_uncertain_source_reports_uncertain_not_success(self):
        """来源端「动过但说不清」时，**整条转移降级为不确定**。"""
        source = ContainerEndpoint(UncertainContainer(slots=1), "unc")
        fill(source.adapter, STONE, 4)
        receipt = self.coordinator.commit(self.request(4), source, self.target)
        self.assertEqual(receipt.status, TransferStatus.UNCERTAIN)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(self.target_box.count_of(STONE), 0, "不确定时不该往目标放东西")


class TestUncertainIsNeverReplayed(EndpointFixture):
    """06-02：**不确定不能作为成功，也不能盲目重试。**"""

    def test_uncertain_result_is_not_treated_as_success(self):
        fill(self.source_box, STONE, 5)
        target = ContainerEndpoint(UncertainContainer(slots=1), "unc")
        receipt = self.coordinator.commit(self.request(5), self.source, target)
        self.assertEqual(receipt.status, TransferStatus.UNCERTAIN)
        self.assertFalse(receipt.is_complete())
        self.assertEqual(receipt.moved, 0)

    def test_resending_the_same_op_does_not_act_twice(self):
        """重发拿到**同一条**不确定回执，而不是又动一次。"""
        fill(self.source_box, STONE, 5)
        uncertain = UncertainContainer(slots=1)
        target = ContainerEndpoint(uncertain, "unc")
        first = self.coordinator.commit(self.request(5), self.source, target)
        second = self.coordinator.commit(self.request(5), self.source, target)
        self.assertEqual(first.status, TransferStatus.UNCERTAIN)
        self.assertEqual(second.status, TransferStatus.UNCERTAIN)
        self.assertEqual(
            len(uncertain.uncertain_calls), 1,
            "同一个 op_id 重发时不该再动一次",
        )

    def test_uncertain_is_recorded_for_reconciliation(self):
        fill(self.source_box, STONE, 5)
        target = ContainerEndpoint(UncertainContainer(slots=1), "unc")
        self.coordinator.commit(self.request(5), self.source, target)
        pending = self.coordinator.pending_reconcile()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["op_id"], "op1")
        self.assertEqual(pending[0]["holder"], "unc")
        self.assertTrue(self.coordinator.clear_pending("op1"))
        self.assertEqual(self.coordinator.pending_reconcile(), [])


class TestDeduplication(EndpointFixture):
    """同一条请求只执行一次。**去重排在预检之前**，与主控准入链同序。"""

    def test_a_completed_request_replays_its_original_receipt(self):
        fill(self.source_box, STONE, 10)
        first = self.coordinator.commit(self.request(6), self.source, self.target)
        second = self.coordinator.commit(self.request(6), self.source, self.target)
        self.assertEqual(first.moved, second.moved)
        self.assertEqual(self.source_box.count_of(STONE), 4, "重发不该再搬一次")
        self.assertEqual(self.target_box.count_of(STONE), 6)

    def test_a_completed_request_replays_even_after_the_source_is_emptied(self):
        """**去重问的是「做过没有」，不是「现在还成不成」。**

        去重排在预检之后的话，一条已经搬空了来源的请求重发会报「库存不足」
        ——听起来像没搬过，而实际是搬过了，调用方据此重试就会重复搬。
        """
        fill(self.source_box, STONE, 6)
        first = self.coordinator.commit(self.request(6), self.source, self.target)
        self.assertEqual(first.status, TransferStatus.COMPLETE)
        second = self.coordinator.commit(self.request(6), self.source, self.target)
        self.assertEqual(second.status, TransferStatus.COMPLETE)
        self.assertEqual(second.moved, 6)
        self.assertEqual(self.source_box.count_of(STONE), 0)

    def test_a_refused_request_leaves_no_trace_so_it_can_be_retried(self):
        """被拒的请求没有副作用，**不该留在账本里**——留了就是永久拒绝。"""
        self.assertEqual(
            self.coordinator.commit(self.request(1), self.source, self.target).status,
            TransferStatus.REFUSED,
        )
        fill(self.source_box, STONE, 5)
        receipt = self.coordinator.commit(self.request(1), self.source, self.target)
        self.assertEqual(receipt.status, TransferStatus.COMPLETE)

    def test_a_partial_request_is_recorded_so_it_cannot_over_move(self):
        """部分成功**要入账**：不入账的话反复重发能把总量搬到超过请求量。

        **判据不能是「回执里的 moved 加起来」**：重放返回的是同一条旧回执，
        把它的 `moved` 加四遍当然得四倍。要看的是**真实存放处的数量**。
        """
        fill(self.source_box, STONE, 10)
        target = ContainerEndpoint(PartialContainer(per_op_limit=3, slots=4), "p")
        request = self.request(5)
        for _ in range(4):
            self.coordinator.commit(request, self.source, target)
        self.assertEqual(target.adapter.count_of(STONE), 3, "重发不该把余量也搬走")
        self.assertEqual(self.source_box.count_of(STONE), 7)
        self.assertEqual(
            total_everywhere(STONE, self.source, target), 10, "守恒"
        )

    def test_different_targets_do_not_collide(self):
        """账本键带方向：同一 op_id 发往不同目标算两条。"""
        fill(self.source_box, STONE, 10)
        other = ContainerEndpoint(FakeContainer(slots=1), "other")
        first = self.coordinator.commit(self.request(4), self.source, self.target)
        second = self.coordinator.commit(self.request(4), self.source, other)
        self.assertEqual(first.moved, 4)
        self.assertEqual(second.moved, 4)


class TestLeftoverAlwaysHasALocation(EndpointFixture):
    """06-03：**避免先删源再假定目标接收。**"""

    def test_a_full_target_is_caught_by_preflight_so_nothing_is_extracted(self):
        """能在预检阶段看出放不下时，**一个都不要取出来**——省掉一次往返。"""
        fill(self.source_box, STONE, 8)
        target = ContainerEndpoint(RefusingContainer(slots=1), "r")
        receipt = self.coordinator.commit(self.request(8), self.source, target)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(self.source_box.count_of(STONE), 8)

    def test_leftover_returns_to_the_source_when_the_target_refuses_it(self):
        """目标**在放进的那一刻**才拒收（预检看不出来）时，余量退回来源。

        `GhostContainer(accept_into_store=0)` 正是这种形态：它声称能收
        （`can_accept` 说有位置），实际一个都没收进去。
        """
        fill(self.source_box, STONE, 8)
        target = ContainerEndpoint(GhostContainer(accept_into_store=0, slots=1), "g")
        receipt = self.coordinator.commit(self.request(8), self.source, target)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(self.source_box.count_of(STONE), 8, "余量必须退回来源")
        self.assertEqual(receipt.holder, "src")

    def test_leftover_can_be_routed_to_an_explicit_return_endpoint(self):
        """分段搬运时余量回中转位——那是**比「来源」更近的可追踪位置**。"""
        fill(self.source_box, STONE, 8)
        buffer = TransferBuffer(100, "port")
        staging = buffer.for_op("op1")
        target = ContainerEndpoint(GhostContainer(accept_into_store=0, slots=1), "g")
        receipt = self.coordinator.commit(
            self.request(8), self.source, target, return_to=staging
        )
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(receipt.holder, "buffer:port")
        self.assertEqual(buffer.total_of(STONE), 8, "余量必须停在中转位里")
        self.assertEqual(self.source_box.count_of(STONE), 0)

    def test_a_return_endpoint_that_cannot_write_is_recorded(self):
        """回滚目标**根本不能写**：那批东西既没进目标、也没处放回。"""
        source = ContainerEndpoint(ReadOnlyContainer(slots=1), "ro")
        source.adapter._slots[0] = ContainerEntry(STONE, 5)
        target = ContainerEndpoint(GhostContainer(accept_into_store=0, slots=1), "g")
        receipt = self.coordinator.commit(self.request(5), source, target)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.holder, "", "无处可放时 holder 是「说不清」")
        self.assertEqual(self.coordinator.stats["stranded"], 5)
        pending = self.coordinator.pending_reconcile()
        self.assertEqual(len(pending), 1)
        self.assertIn("无处放回", pending[0]["detail"])

    def test_a_failed_rollback_is_recorded_in_the_reconcile_ledger(self):
        """回滚目标能写、但**在写的那一刻**被拒：同样必须留痕。

        报成功会让这批东西变成账外资产，静默丢弃更是直接违反守恒。
        """
        fill(self.source_box, STONE, 5)
        back = ContainerEndpoint(GhostContainer(accept_into_store=0, slots=1), "back")
        target = ContainerEndpoint(GhostContainer(accept_into_store=0, slots=1), "g")
        receipt = self.coordinator.commit(
            self.request(5), self.source, target, return_to=back
        )
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(self.source_box.count_of(STONE), 0, "取出来了")
        self.assertEqual(self.coordinator.stats["stranded"], 5)
        pending = self.coordinator.pending_reconcile()
        self.assertEqual(len(pending), 1)
        self.assertIn("回滚也未完成", pending[0]["detail"])
        self.assertEqual(pending[0]["count"], 5)


# -- 06-03：中转缓冲 -------------------------------------------------------


class TestTransferBuffer(unittest.TestCase):
    """有界缓冲与资源归属台账。**缓冲只记归属，物品在它背后的容器里。**"""

    def test_capacity_is_enforced_and_partially_accepted(self):
        buffer = TransferBuffer(10, "p")
        self.assertEqual(buffer.deposit("op", STONE, 7, ORIGIN_INTERNAL), 7)
        self.assertEqual(buffer.free(), 3)
        self.assertEqual(buffer.deposit("op", STONE, 5, ORIGIN_INTERNAL), 3,
                         "放不下时**部分接受**，不是拒绝也不是硬塞")
        self.assertEqual(buffer.total(), 10)
        self.assertEqual(buffer.deposit("op", STONE, 1, ORIGIN_INTERNAL), 0)

    def test_origin_is_recorded_per_lot_not_per_buffer(self):
        """来源标识记在**批次**上。记在缓冲上就没有正确答案可言：
        同一份缓冲里可能同时躺着外部送进来的和本模组搬过来的。"""
        buffer = TransferBuffer(100, "p")
        buffer.deposit("opA", STONE, 4, ORIGIN_EXTERNAL, owner_id="ext")
        buffer.deposit("opB", STONE, 6, ORIGIN_INTERNAL)
        origins = dict((lot["origin"], lot["count"]) for lot in buffer.lots())
        self.assertEqual(origins, {ORIGIN_EXTERNAL: 4, ORIGIN_INTERNAL: 6})

    def test_withdraw_is_fifo_across_lots(self):
        buffer = TransferBuffer(100, "p")
        buffer.deposit("opA", STONE, 4, ORIGIN_EXTERNAL)
        buffer.deposit("opB", STONE, 6, ORIGIN_INTERNAL)
        moved, taken = buffer.withdraw("opC", STONE, 7)
        self.assertEqual(moved, 7)
        # 先进先出：先拿光外部那 4 个，再从内部那批拿 3 个。
        self.assertEqual(
            [(lot.origin, amount) for lot, amount in taken],
            [(ORIGIN_EXTERNAL, 4), (ORIGIN_INTERNAL, 3)],
        )
        self.assertEqual(buffer.total(), 3)

    def test_withdraw_only_touches_the_requested_resource(self):
        buffer = TransferBuffer(100, "p")
        buffer.deposit("opA", STONE, 4, ORIGIN_INTERNAL)
        buffer.deposit("opA", DIRT, 4, ORIGIN_INTERNAL)
        moved, _ = buffer.withdraw("opB", STONE, 10)
        self.assertEqual(moved, 4)
        self.assertEqual(buffer.total_of(DIRT), 4)

    def test_round_trip_through_the_save_structure(self):
        buffer = TransferBuffer(100, "p")
        buffer.deposit("opA", STONE, 4, ORIGIN_EXTERNAL, owner_id="ext", tick=17)
        buffer.deposit("opB", WATER, 25, ORIGIN_INTERNAL)
        restored = TransferBuffer.from_dict(buffer.to_dict())
        self.assertEqual(restored.to_dict(), buffer.to_dict())
        self.assertEqual(restored.total(), 29)
        self.assertEqual(restored.totals(), buffer.totals())

    def test_an_unknown_newer_schema_is_rejected_not_read_as_empty(self):
        """05 明令：未知的**更新**版本原样保留并标记只读，
        **不得读成空值再覆盖**。这里抛错就是那条纪律的落点。"""
        payload = {"schema_version": TRANSFER_SCHEMA_VERSION + 1, "capacity": 10,
                   "lots": []}
        with self.assertRaises(ValueError):
            TransferBuffer.from_dict(payload)

    def test_a_saved_overflow_is_rejected(self):
        buffer = TransferBuffer(4, "p")
        payload = buffer.to_dict()
        payload["lots"] = [{"resource_key": STONE.to_dict(), "count": 9,
                            "origin": ORIGIN_INTERNAL, "owner_id": "", "op_id": "x",
                            "tick": 0}]
        with self.assertRaises(ValueError):
            TransferBuffer.from_dict(payload)

    def test_invalid_origin_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            TransferBuffer(4, "p").deposit("op", STONE, 1, "whatever")

    def test_disposition_retains_and_reports_instead_of_dropping(self):
        """R05 未定：**不自行决定**掉落还是保管，只报告「它还在那儿」。"""
        buffer = TransferBuffer(10, "p")
        buffer.deposit("op", STONE, 3, ORIGIN_EXTERNAL)
        outcome = BufferDisposition().dispose(buffer, "接口方块已拆除")
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["held"], 3)
        self.assertEqual(outcome["lots"][0]["count"], 3)
        self.assertEqual(buffer.total(), 3, "处置**不能**改动缓冲")


class TestBufferBacking(unittest.TestCase):
    """缓冲背后的实际存放处。**先动 backing、再改批次表**——顺序反了会丢东西。"""

    def setUp(self):
        self.box = FakeContainer(slots=2, max_stack=64)
        self.buffer = TransferBuffer(
            100, "p", backing=ContainerEndpoint(self.box, "own")
        )
        self.endpoint = self.buffer.for_op("op1")

    def test_insert_goes_into_the_backing_container(self):
        receipt = self.endpoint.commit_insert("op1", STONE, 12, "ext", ORIGIN_EXTERNAL)
        self.assertEqual(receipt.status, TransferStatus.COMPLETE)
        self.assertEqual(self.box.count_of(STONE), 12)
        self.assertEqual(self.buffer.total(), 12)

    def test_extract_takes_from_the_backing_first(self):
        self.endpoint.commit_insert("op1", STONE, 12, "ext", ORIGIN_EXTERNAL)
        receipt = self.endpoint.commit_extract("op2", STONE, 5)
        self.assertEqual(receipt.moved, 5)
        self.assertEqual(self.box.count_of(STONE), 7)
        self.assertEqual(self.buffer.total(), 7)
        self.assertEqual(self.endpoint.last_extract_origins(),
                         ((ORIGIN_EXTERNAL, 5),))

    def test_a_player_emptying_the_box_is_recorded_as_a_desync(self):
        """玩家手动掏空方块容器 → 账上说有、实际拿不到。**留痕，且把账压到现实上。**

        **回归**：早先只留痕不修正，于是 `total_of` / `free` /
        `probe_supply` 永远报着不存在的余量——那台接口再也收不进东西，
        而且每次轮询都会再记一笔偏差（`desync` 无界增长）。
        """
        self.endpoint.commit_insert("op1", STONE, 12, "ext", ORIGIN_EXTERNAL)
        self.box._slots[0] = None
        receipt = self.endpoint.commit_extract("op2", STONE, 12)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(self.buffer.stats["desync"], 12)
        self.assertEqual(self.buffer.total_of(STONE), 0, "账必须压到现实上")
        self.assertEqual(self.buffer.free(), self.buffer.capacity)
        # 再轮询一次不该再记一笔偏差：账已经与现实一致了。
        self.endpoint.commit_extract("op3", STONE, 12)
        self.assertEqual(self.buffer.stats["desync"], 12)

    def test_forget_only_drops_the_named_resource(self):
        self.endpoint.commit_insert("op1", STONE, 5, "ext", ORIGIN_EXTERNAL)
        self.endpoint.commit_insert("op1", DIRT, 5, "ext", ORIGIN_EXTERNAL)
        self.assertEqual(self.buffer.forget(STONE, 3), 3)
        self.assertEqual(self.buffer.total_of(STONE), 2)
        self.assertEqual(self.buffer.total_of(DIRT), 5)
        self.assertEqual(self.buffer.forget(STONE, 99), 2)

    def test_backing_refusal_limits_what_is_recorded(self):
        """backing 放不下时**不记批次**——记了等于宣称东西在缓冲里。"""
        tight = TransferBuffer(100, "p", backing=ContainerEndpoint(
            FakeContainer(slots=1, max_stack=4), "own"
        ))
        endpoint = tight.for_op("op1")
        receipt = endpoint.commit_insert("op1", STONE, 10)
        self.assertEqual(receipt.moved, 4)
        self.assertEqual(tight.total(), 4)
        self.assertEqual(receipt.reason, Reason.CONTAINER_PARTIAL)

    def test_an_uncertain_backing_stays_uncertain(self):
        """**回归**：存放处说不清时，回执曾经被降级成「缓冲满了 / 库存不足」。

        那会让调用方换个 op_id 再试一次——而东西可能已经进去了。
        不确定就是不确定，它对外的语义只有一种。
        """
        loose = TransferBuffer(100, "p", backing=ContainerEndpoint(
            UnverifiableContainer(slots=1), "own"
        ))
        receipt = loose.for_op("op1").commit_insert("op1", STONE, 5)
        self.assertEqual(receipt.status, TransferStatus.UNCERTAIN)
        self.assertEqual(receipt.reason, Reason.CONTAINER_UNCERTAIN)
        self.assertEqual(loose.total(), 0, "不确定就不记批次")
        self.assertEqual(loose.stats["desync"], 5)

    def test_holder_is_the_original_owner_not_the_buffer(self):
        """缓冲里的东西**归原主，不归缓冲**。"""
        self.endpoint.commit_insert("op1", STONE, 5, "player-7", ORIGIN_EXTERNAL)
        self.assertEqual(self.endpoint.holder_id(), "player-7")


class TestExternalInputNotification(unittest.TestCase):
    """只有**外部来源、真的进入了网络**的那一份才发通知（06-08）。"""

    def setUp(self):
        self.coordinator = TransferCoordinator()
        self.delivered = []
        self.coordinator.sink = self._sink
        self.network_box = FakeContainer(slots=2, max_stack=64)
        self.source_box = FakeContainer(slots=2, max_stack=64)

    def _sink(self, request, count, tick):
        self.delivered.append((request.op_id, count, request.origin))

    def test_an_external_deposit_emits_exactly_one_notification(self):
        fill(self.source_box, STONE, 6)
        request = TransferRequest("op1", "src", "net", STONE, 6,
                                  origin=ORIGIN_EXTERNAL)
        self.coordinator.commit(
            request, ContainerEndpoint(self.source_box, "src"),
            ContainerEndpoint(self.network_box, "net"),
        )
        self.assertEqual(self.delivered, [("op1", 6, ORIGIN_EXTERNAL)])

    def test_an_internal_move_emits_nothing(self):
        """内部搬运发了通知，同一份资源会被反复计入订单产物（全局约束 3）。"""
        fill(self.source_box, STONE, 6)
        request = TransferRequest("op1", "src", "net", STONE, 6,
                                  origin=ORIGIN_INTERNAL)
        self.coordinator.commit(
            request, ContainerEndpoint(self.source_box, "src"),
            ContainerEndpoint(self.network_box, "net"),
        )
        self.assertEqual(self.delivered, [])

    def test_a_partial_move_reports_only_what_actually_entered(self):
        fill(self.source_box, STONE, 6)
        target = ContainerEndpoint(PartialContainer(per_op_limit=2, slots=2), "net")
        request = TransferRequest("op1", "src", "net", STONE, 6,
                                  origin=ORIGIN_EXTERNAL)
        receipt = self.coordinator.commit(
            request, ContainerEndpoint(self.source_box, "src"), target
        )
        self.assertEqual(receipt.moved, 2)
        self.assertEqual(self.delivered, [("op1", 2, ORIGIN_EXTERNAL)])

    def test_a_replayed_request_does_not_emit_a_second_notification(self):
        fill(self.source_box, STONE, 6)
        request = TransferRequest("op1", "src", "net", STONE, 6,
                                  origin=ORIGIN_EXTERNAL)
        endpoints = (
            ContainerEndpoint(self.source_box, "src"),
            ContainerEndpoint(self.network_box, "net"),
        )
        self.coordinator.commit(request, *endpoints)
        self.coordinator.commit(request, *endpoints)
        self.assertEqual(len(self.delivered), 1)

    def test_a_missing_sink_is_counted_not_silently_skipped(self):
        """**「还没接」不等于「不用发」。** 没有回流入口时必须留痕。"""
        coordinator = TransferCoordinator()
        fill(self.source_box, STONE, 3)
        coordinator.commit(
            TransferRequest("op1", "src", "net", STONE, 3, origin=ORIGIN_EXTERNAL),
            ContainerEndpoint(self.source_box, "src"),
            ContainerEndpoint(self.network_box, "net"),
        )
        self.assertEqual(coordinator.stats["external_inputs"], 0)
        self.assertEqual(coordinator.stats["sink_missing"], 1)
        self.assertFalse(coordinator.diagnostics()["sink_wired"])
        self.assertIn("还没接", coordinator.diagnostics()["scope_note"])

    def test_buffer_lots_decide_the_origin_not_the_request(self):
        """从缓冲里搬的时候，来源由**实际取走的批次**决定。"""
        fill(self.source_box, STONE, 4)
        buffer = TransferBuffer(100, "p")
        # 先让外部把东西放进缓冲，再让网络从缓冲里取。
        ext = buffer.for_op("ext-op", ORIGIN_EXTERNAL)
        ext.commit_insert("ext-op", STONE, 4, "外界", ORIGIN_EXTERNAL)
        drain = buffer.for_op("drain-op")
        request = TransferRequest("drain-op", "buf", "net", STONE, 4,
                                  origin=ORIGIN_INTERNAL)
        self.coordinator.commit(
            request, drain, ContainerEndpoint(self.network_box, "net")
        )
        self.assertEqual(self.delivered, [("drain-op", 4, ORIGIN_INTERNAL)])
        self.assertEqual(self.network_box.count_of(STONE), 4)


# -- 06-09：槽锚 -----------------------------------------------------------


class TestSlotAnchor(unittest.TestCase):
    """背包槽的定位：**槽号 + 指纹 + 槽版本**，三样缺一不可。"""

    def setUp(self):
        from endernet.server.adapters.containers_engine import (
            EnginePlayerInventory,
            build_stack_limit,
        )

        self.primitives = _FakePlayerPrimitives()
        limit = build_stack_limit(self.primitives)
        self.inventory = EnginePlayerInventory(self.primitives, "steve", limit)

    def endpoint(self):
        from endernet.server.storage.transfer import InventoryEndpoint

        return InventoryEndpoint(self.inventory, "steve")

    def test_anchor_rejects_a_slot_holding_a_different_item(self):
        """**「不会误取替换后的物品」就是这一条。**"""
        endpoint = self.endpoint()
        self.inventory.insert("seed", STONE, 5)
        verdict = endpoint.anchor_verdict(SlotAnchor(0, DIRT))
        self.assertFalse(verdict[0])
        self.assertEqual(verdict[1], Reason.SLOT_CHANGED)

    def test_anchor_rejects_an_empty_slot(self):
        verdict = self.endpoint().anchor_verdict(SlotAnchor(35, STONE))
        self.assertFalse(verdict[0])
        self.assertEqual(verdict[1], Reason.SLOT_CHANGED)

    def test_anchor_rejects_a_stale_slot_version(self):
        """版本对不上说明请求依据的是一份**旧快照**，取新快照再发才有意义。"""
        endpoint = self.endpoint()
        self.inventory.insert("seed", STONE, 5)
        current = endpoint.slot_view(0)["version"]
        verdict = endpoint.anchor_verdict(SlotAnchor(0, STONE, current + 99))
        self.assertFalse(verdict[0])
        self.assertEqual(verdict[1], Reason.STALE_VERSION)

    def test_a_matching_anchor_passes(self):
        endpoint = self.endpoint()
        self.inventory.insert("seed", STONE, 5)
        view = endpoint.slot_view(0)
        verdict = endpoint.anchor_verdict(SlotAnchor(0, STONE, view["version"]))
        self.assertTrue(verdict[0], verdict[2])

    def test_anchored_extract_is_capped_by_that_slots_count(self):
        """**上限是这个槽里有几个**，不是整个背包里该物品的总量。"""
        endpoint = self.endpoint()
        self.inventory.insert("seed", STONE, 30)
        view = endpoint.slot_view(0)
        before_total = self.inventory.count_of(STONE)
        receipt = endpoint.anchored_extract("op", SlotAnchor(0, STONE, view["version"]), 999)
        self.assertTrue(receipt.moved <= view["count"])
        self.assertEqual(self.inventory.count_of(STONE), before_total - receipt.moved)

    def test_anchored_extract_refuses_instead_of_taking_from_elsewhere(self):
        endpoint = self.endpoint()
        self.inventory.insert("seed", STONE, 30)
        # 槽 0 换成泥土，石头还在槽 1 之后。锚定槽 0 必须拒绝，而不是从别处凑。
        self.inventory.write_slot(0, _dirt_dict(3))
        view = endpoint.slot_view(0)
        stone_before = self.inventory.count_of(STONE)
        receipt = endpoint.anchored_extract("op", SlotAnchor(0, STONE, view["version"]), 5)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.reason, Reason.SLOT_CHANGED)
        self.assertEqual(self.inventory.count_of(STONE), stone_before)

    # -- 交换（06-09 的第五个动作，2026-09-30 补）---------------------------

    def _two_slots(self, endpoint):
        """槽 0 放 5 个石头、槽 1 放 3 个泥土。"""
        self.inventory.insert("seed", STONE, 5)
        self.inventory.insert("seed", DIRT, 3)
        return endpoint, endpoint.slot_view(0), endpoint.slot_view(1)

    def test_anchored_swap_exchanges_both_slots(self):
        """**一次动两格**，与取/放那两个单格原语不同。

        `moved` 报的是**源物品落到对面几个**（与 `inv_merge` 报「并进去几个」
        同一口径），不是两格之和——交换没有「总量」可言，报和会随对调反转。
        """
        endpoint = self.endpoint()
        endpoint, _, _ = self._two_slots(endpoint)
        receipt = endpoint.anchored_swap(
            "op", SlotAnchor(0, STONE), SlotAnchor(1, DIRT)
        )
        self.assertEqual(receipt.status, TransferStatus.COMPLETE)
        self.assertEqual(receipt.moved, 5)
        self.assertEqual(receipt.resource_key, STONE)
        # **两边都换了**，不是把 A 复制到 B。
        self.assertEqual(self.inventory.read_slot(0)["newItemName"], DIRT.name)
        self.assertEqual(self.inventory.read_slot(1)["newItemName"], STONE.name)
        self.assertEqual(self.inventory.count_of(STONE), 5)
        self.assertEqual(self.inventory.count_of(DIRT), 3)

    def test_anchored_swap_with_an_empty_target_is_a_move(self):
        """**目标槽为空是合法的**——与空槽交换就是移动，是定案的行为。

        这条是端点层最容易写错的地方：把两个锚都做成严格的，第一次
        「把石头挪到空格」就会被拒。目标锚因此在空槽上只核版本。
        载荷里那个占位指纹（这里传的 `STONE`）**不被读取**。
        """
        endpoint = self.endpoint()
        self.inventory.insert("seed", STONE, 5)
        receipt = endpoint.anchored_swap(
            "op", SlotAnchor(0, STONE), SlotAnchor(20, STONE)
        )
        self.assertEqual(receipt.status, TransferStatus.COMPLETE, receipt.to_dict())
        self.assertIsNone(self.inventory.read_slot(0))
        self.assertEqual(self.inventory.read_slot(20)["newItemName"], STONE.name)
        self.assertEqual(self.inventory.read_slot(20)["count"], 5)

    def test_anchored_swap_refuses_an_empty_source(self):
        """源槽空着就没有可交换的东西。**目标允许为空，源槽不允许**——
        这一条不对称是判据本身，不是遗漏。"""
        endpoint = self.endpoint()
        receipt = endpoint.anchored_swap(
            "op", SlotAnchor(0, STONE), SlotAnchor(1, DIRT)
        )
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.reason, Reason.SLOT_CHANGED)

    def test_anchored_swap_refuses_a_source_holding_something_else(self):
        endpoint = self.endpoint()
        self.inventory.insert("seed", DIRT, 3)
        before = self.inventory.count_of(DIRT)
        receipt = endpoint.anchored_swap(
            "op", SlotAnchor(0, STONE), SlotAnchor(1, DIRT)
        )
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.reason, Reason.SLOT_CHANGED)
        self.assertEqual(self.inventory.count_of(DIRT), before, "一个都没动")

    def test_anchored_swap_refuses_a_stale_target_version(self):
        """**目标槽的版本也要核**：依据旧快照去换一个已经变过的槽是同类过期请求。"""
        endpoint = self.endpoint()
        endpoint, _, target = self._two_slots(endpoint)
        receipt = endpoint.anchored_swap(
            "op", SlotAnchor(0, STONE),
            SlotAnchor(1, DIRT, target["version"] + 99),
        )
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.reason, Reason.STALE_VERSION)

    def test_anchored_swap_is_unsupported_without_the_primitive(self):
        """**容器没有交换原语**——基类刻意不给默认实现（会复制物品）。

        取不到原语时必须明确答 `unsupported`，而不是退化成「读两格写两格」：
        后者第二次写入失败会把源物品同时留在两处，而总量守恒查不出来。
        """
        adapter = _NoSwapInventory([
            {"newItemName": STONE.name, "newAuxValue": 0, "count": 5, "showInHand": True},
            {"newItemName": DIRT.name, "newAuxValue": 0, "count": 3, "showInHand": True},
        ])
        from endernet.server.storage.transfer import InventoryEndpoint

        receipt = InventoryEndpoint(adapter, "steve").anchored_swap(
            "op", SlotAnchor(0, STONE), SlotAnchor(1, DIRT)
        )
        self.assertEqual(receipt.status, TransferStatus.UNSUPPORTED)
        self.assertEqual(receipt.moved, 0)

    def test_anchored_swap_reports_uncertain_when_the_engine_did_nothing(self):
        """**引擎说成功不算数，复核才算。**（架构 §6.3 / 06 验收首条）

        复核判据与 `_recheck_after` 不同：那边问「源槽空了没有」，而交换后
        源槽里装着对方的东西，问「空没空」必然答错，每次都会误报不确定。
        这里问的是「源物品现在在对面没有」。
        """
        from endernet.server.adapters.containers_engine import (
            EnginePlayerInventory,
            build_stack_limit,
        )

        primitives = _LyingSwapPrimitives()
        inventory = EnginePlayerInventory(
            primitives, "steve", build_stack_limit(primitives)
        )
        inventory.insert("seed", STONE, 5)
        inventory.insert("seed", DIRT, 3)
        from endernet.server.storage.transfer import InventoryEndpoint

        receipt = InventoryEndpoint(inventory, "steve").anchored_swap(
            "op", SlotAnchor(0, STONE), SlotAnchor(1, DIRT)
        )
        self.assertEqual(receipt.status, TransferStatus.UNCERTAIN)
        self.assertEqual(receipt.reason, Reason.SLOT_CHANGED)


def _dirt_dict(count):
    return {"newItemName": "minecraft:dirt", "newAuxValue": 0, "count": count,
            "showInHand": True}


class _FakePlayerPrimitives(object):
    """按 `tooldelta` 的调用形状实现的假引擎，**只服务本文件**。

    与 `test_containers_engine.py` 里那份分开写：两个文件各自成立，
    不互相 import——那会让这套用例的成败取决于另一个模块的导入顺序
    （`test_energy.py` 里有同样的注释）。
    """

    PLAYER_SLOTS = 36

    def __init__(self):
        self.slots = [None] * self.PLAYER_SLOTS

    def player_slot_count(self, player_id):
        return self.PLAYER_SLOTS

    def player_item(self, player_id, slot):
        data = self.slots[slot]
        return dict(data) if data else None

    def set_player_item(self, player_id, slot, data):
        if data.get("count", 0) <= 0 or data.get("newItemName") == "minecraft:air":
            self.slots[slot] = None
        else:
            self.slots[slot] = dict(data)
        return True

    def swap_player_items(self, player_id, slot_a, slot_b):
        """交换两个槽。**一次调用、一个布尔值**，与引擎同形（见
        `test_containers_engine.py` 同名方法的说明）。"""
        self.slots[slot_a], self.slots[slot_b] = self.slots[slot_b], self.slots[slot_a]
        return True

    def stack_limit(self, name, aux):
        return 64


class _LyingSwapPrimitives(_FakePlayerPrimitives):
    """引擎**说成功、其实没动**。复核那一关就是为它存在的。"""

    def swap_player_items(self, player_id, slot_a, slot_b):
        return True


class _NoSwapInventory(SlotBackedContainer):
    """一个**没有交换原语**的按槽容器。

    它继承 `SlotBackedContainer` 而不实现 `swap_slots`——这正是容器那一侧
    的真实形态（基类刻意不提供，理由见 `test_containers_engine.py` 的
    `test_the_backpack_is_the_only_thing_that_can_swap`）。用来看端点
    是否老实答 `unsupported`。
    """

    adapter_id = "no_swap_inventory"

    def __init__(self, slots):
        SlotBackedContainer.__init__(self, lambda key: 64)
        self._slots = list(slots)

    def slot_count(self):
        return len(self._slots)

    def read_slot(self, slot):
        data = self._slots[slot]
        return dict(data) if data else None

    def _write_slot(self, slot, data):
        return True


# -- 策略 ------------------------------------------------------------------


class TestTransferPolicy(unittest.TestCase):
    def test_unresolved_fields_are_reported_with_their_rule_numbers(self):
        report = TransferPolicy().unresolved_report()
        rules = set(item["rule"] for item in report)
        self.assertIn("R05", rules)
        self.assertIn("R12", rules)
        for item in report:
            self.assertTrue(item["field"].startswith("transfer."))
            self.assertTrue(item["note"])

    def test_assert_settled_refuses_to_declare_victory_early(self):
        with self.assertRaises(AssertionError):
            TransferPolicy().assert_settled()

    def test_cursor_disposition_rejects_unknown_values(self):
        with self.assertRaises(ValueError):
            TransferPolicy(cursor_disposition="whatever")

    def test_bad_numbers_are_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            TransferPolicy(reconcile_limit=0)
        with self.assertRaises(ValueError):
            TransferPolicy(cursor_disposition="whatever")


class TestRequestAndReceiptShape(unittest.TestCase):
    def test_request_round_trips_through_its_dict(self):
        request = TransferRequest("op", "a", "b", STONE, 3, 7, "res-1", "p1",
                                  ORIGIN_EXTERNAL)
        restored = TransferRequest.from_dict(request.to_dict())
        self.assertEqual(restored.to_dict(), request.to_dict())
        self.assertEqual(restored.origin, ORIGIN_EXTERNAL)

    def test_request_requires_an_op_id(self):
        with self.assertRaises(ValueError):
            TransferRequest("", "a", "b", STONE, 1)

    def test_request_rejects_an_unknown_origin(self):
        with self.assertRaises(ValueError):
            TransferRequest("op", "a", "b", STONE, 1, origin="guess")

    def test_receipt_carries_the_holder(self):
        request = TransferRequest("op", "a", "b", STONE, 3)
        coordinator = TransferCoordinator()
        source = ContainerEndpoint(fill(FakeContainer(slots=1), STONE, 3), "a")
        target = ContainerEndpoint(RefusingContainer(slots=1), "b")
        receipt = coordinator.commit(request, source, target)
        self.assertEqual(receipt.holder, "a")
        self.assertEqual(receipt.to_dict()["holder"], "a")


class TestNetworkEndpoint(unittest.TestCase):
    """网络端点。它把额度与容量的判定**交给存储服务**，不自己重算。"""

    class _Storage(object):
        def __init__(self):
            self.calls = []

        def is_ready(self):
            return True

        def simulate_insert(self, key, count):
            self.calls.append(("insert", count))
            return {"accepted": min(count, 4)}

        def simulate_extract(self, key, count):
            self.calls.append(("extract", count))
            return {"accepted": min(count, 2)}

        def commit_insert(self, op_id, key, count, owner_id=""):
            raise AssertionError("不该走到这里")

        def commit_extract(self, op_id, key, count, owner_id=""):
            raise AssertionError("不该走到这里")

    def test_probes_delegate_to_the_storage_service(self):
        storage = self._Storage()
        endpoint = NetworkEndpoint(storage, "c1")
        self.assertEqual(endpoint.probe_space(STONE, 10), 4)
        self.assertEqual(endpoint.probe_supply(STONE, 10), 2)
        self.assertEqual(storage.calls, [("insert", 10), ("extract", 10)])

    def test_an_unready_service_is_invalid(self):
        class Down(object):
            def is_ready(self):
                return False

        endpoint = NetworkEndpoint(Down(), "c1")
        self.assertFalse(endpoint.is_valid())
        self.assertFalse(endpoint.can_read())
        self.assertFalse(endpoint.can_write())
        self.assertIsNone(endpoint.probe_space(STONE, 1))

    def test_a_quota_shortage_is_unknown_not_empty(self):
        """**额度用尽回 `None`，不回 0。**

        2026-10-01 真机实测：一次「取一组」正好抽干该秒的读取额度（磁带
        `read_units_per_second` 是 64，桶容量 1 秒 = 64 件），同一秒内的
        第二条命令必然失败——而它报的是 **`insufficient_stock`**，玩家看到
        「网络里没有这件东西」。真相是 `rate_limited`：下一帧就有。

        回 `None`（不知道）之后，协调器会照常发起转移，由 `commit_*` 给出
        真实的 `rate_limited`——`probe_*` 的契约本来就是这么写的。
        """
        class QuotaOut(TestNetworkEndpoint._Storage):
            def simulate_insert(self, key, count):
                return {"accepted": 0, "reason": Reason.RATE_LIMITED}

            def simulate_extract(self, key, count):
                return {"accepted": 0, "reason": Reason.RATE_LIMITED}

        endpoint = NetworkEndpoint(QuotaOut(), "c1")
        self.assertIsNone(endpoint.probe_supply(STONE, 2))
        self.assertIsNone(endpoint.probe_space(STONE, 2))

    def test_a_genuinely_empty_side_still_answers_zero(self):
        """真没货 / 真放不下照旧回 0——那些答案是对的，不该被改成 `None`。"""
        class Empty(TestNetworkEndpoint._Storage):
            def simulate_insert(self, key, count):
                return {"accepted": 0, "reason": Reason.NO_CAPACITY}

            def simulate_extract(self, key, count):
                return {"accepted": 0, "reason": Reason.INSUFFICIENT_STOCK}

        endpoint = NetworkEndpoint(Empty(), "c1")
        self.assertEqual(endpoint.probe_supply(STONE, 2), 0)
        self.assertEqual(endpoint.probe_space(STONE, 2), 0)


class TestRateLimitedTransfer(unittest.TestCase):
    """额度用尽时报 `rate_limited`，**不是 `insufficient_stock`**。

    这是 2026-10-01 真机实测那条的端到端回归：网络里有货、只是这一帧的
    额度用完了，而玩家看到的是「网络里没有这件东西」。判据落在**回执的
    原因码**上——`probe_supply` 回 `None`（不知道）之后由 `commit_extract`
    给出真实原因，正是 `probe_*` 的契约。
    """

    class _Storage(object):
        """有货，但这一帧的额度是 0：预演说 `rate_limited`，提交也这么说。"""

        def is_ready(self):
            return True

        def simulate_extract(self, key, count):
            return {"accepted": 0, "reason": Reason.RATE_LIMITED}

        def commit_extract(self, op_id, key, count, owner_id=""):
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED, Reason.RATE_LIMITED, key
            )

        def commit_insert(self, op_id, key, count, owner_id="", origin=None):
            raise AssertionError("这一条不该写进网络")

    def test_the_real_reason_survives_to_the_receipt(self):
        source = NetworkEndpoint(self._Storage(), "c1")
        target = ContainerEndpoint(FakeContainer(slots=1, max_stack=64), "player")
        receipt = TransferCoordinator().commit(
            TransferRequest("op-rl", source.endpoint_id(), target.endpoint_id(),
                            STONE, 2),
            source, target,
        )
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(receipt.reason, Reason.RATE_LIMITED)
        self.assertNotEqual(receipt.reason, Reason.INSUFFICIENT_STOCK)


class ShiftingContainer(object):
    """写下去一份、**读回来换了身份**——模拟引擎对 `userData` 的变形。

    2026-10-02 真机上那条复制就是它造成的：原语写下去之后回读核对判不等，
    于是报 `moved=0`，而东西已经进了那一格。本类的职责只是**把那个形状
    造出来**，让「写了却报没写」这条纪律有用例咬着。

    注意它**故意不实现** `normalize_engine_item`（真引擎适配器会做）。
    身份归一修好之后，真机上的键不会再变；这一层守的是**别的原因**造成
    的不一致（第三方模组、引擎行为再变、别的容器类型）。
    """

    def __init__(self, max_stack=64, shift=True):
        self._slots = {}
        self.max_stack = max_stack
        self.shift = shift
        #: 变形**只在写过一次之后**出现。写入前就变形的话，撞到的是原语
        #: **写入前**那道「槽里的东西换了」的检查——那条分支不吃 `moved`，
        #: 也从来不会复制，测不到本次要守的东西。
        self._written = False
        self.writes = []

    def can_read(self):
        return True

    def can_write(self):
        return True

    def stack_limit_of(self, key):
        return self.max_stack

    def read_slot(self, slot):
        entry = self._slots.get(slot)
        if entry is None:
            return None
        key, count = entry
        data = item_dict_from_key(key, count)
        if self.shift and self._written:
            user_data = dict(key.raw_identity.get("user_data") or {})
            user_data["shifted"] = 1
            data["userData"] = user_data
        return data

    def write_slot(self, slot, data):
        self.writes.append((slot, data))
        self._written = True
        if data is None or not data.get("count"):
            self._slots.pop(slot, None)
            return True
        self._slots[slot] = (item_key_from_dict(dict(data)), data["count"])
        return True


def carrier():
    """一件带 `userData` 载荷的物品（变形只对它有意义）。"""
    return ResourceKey.item("endernet:factorical_template", 0,
                            user_data={"endernet:pattern": {"format": 1}})


class SlotPrimitiveIsHonestTests(unittest.TestCase):
    """**报了 `moved=0` 就必须真的没动过东西。**

    写入是「先写、再读回核对」，因为引擎可能写成功却返回 `False`。但核对
    不过时的出路不是「当没写过」——那样调用方会不扣来源格，而东西已经在
    目标格里：**一次复制**。这里把两边都钉住。
    """

    def test_insert_reports_moved_when_the_readback_key_differs(self):
        box = ShiftingContainer()
        key = carrier()
        receipt = slot_insert(box, "op1", 0, key, 3)
        self.assertEqual(receipt.moved, 3, "东西真的写进去了，不能报 0")
        self.assertEqual(receipt.status, TransferStatus.PARTIAL)
        self.assertEqual(receipt.reason, Reason.SLOT_CHANGED)
        self.assertTrue(receipt.is_applied(), "重发不能再写一遍")
        self.assertEqual(box.writes[0][0], 0)

    def test_insert_still_refuses_when_nothing_landed(self):
        box = ShiftingContainer(shift=False)
        box._slots[0] = (key_other(), 1)
        receipt = slot_insert(box, "op1", 0, carrier(), 1)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.reason, Reason.SLOT_OCCUPIED)

    def test_extract_reports_moved_when_the_readback_key_differs(self):
        box = ShiftingContainer()
        key = carrier()
        box._slots[0] = (key, 5)
        receipt = slot_extract(box, "op1", 0, key, 2)
        self.assertEqual(receipt.moved, 2, "东西真的取出来了，不能报 0")
        self.assertEqual(receipt.status, TransferStatus.PARTIAL)
        self.assertEqual(receipt.reason, Reason.SLOT_CHANGED)

    def test_extract_still_refuses_when_the_slot_holds_another_item(self):
        box = ShiftingContainer(shift=False)
        box._slots[0] = (key_other(), 1)
        receipt = slot_extract(box, "op1", 0, carrier(), 1)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.reason, Reason.SLOT_CHANGED)

    def test_the_same_item_round_trips_normally(self):
        """不变形时行为一字不改：写多少就是多少。"""
        box = ShiftingContainer(shift=False)
        receipt = slot_insert(box, "op1", 0, carrier(), 3)
        self.assertEqual(receipt.moved, 3)
        self.assertEqual(receipt.status, TransferStatus.COMPLETE)


def key_other():
    return ResourceKey.item("minecraft:stone")


if __name__ == "__main__":
    unittest.main()
