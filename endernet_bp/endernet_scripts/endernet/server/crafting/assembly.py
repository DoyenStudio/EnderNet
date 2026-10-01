# coding=utf-8
"""07-06 本地装配。09 持有租约，06 管理跨容器转移，10 只接真实入库量。

实体槽只允许一份执行占用。逐槽消费和逐份产出保留游标；不确定写入
锁住该执行而不重放。R10 耗时未定，目前按调度工作预算推进，不声称游戏耗时。
"""

from ...common.item_codec import item_dict_from_key, item_key_from_dict
from ...common.protocol import BlockReason, Reason, TransferRequest, TransferStatus
from ...common.resource_key import ResourceKey
from ..storage.transfer import NetworkEndpoint, SlotView, slot_read, slot_limit_of
from ..network.controller import power_blocker
from .patterns import MODE_CRAFTING
from .jobs import StepStatus


class AssemblyChamberExecution(object):
    def __init__(self, on_changed=None):
        # type: (object) -> None
        # 跨网络共用 PatternStore 上的这份所有权，防止拓扑迁移后混用槽位。
        self.leases = {}
        self.on_changed = on_changed

    def is_working(self, ref):
        # type: (object) -> bool
        return "%s" % (ref.as_key(),) in self.leases

    def accepts_lease(self, lease):
        # type: (object) -> bool
        return bool(lease is not None and getattr(lease, "active", False)
                    and getattr(lease, "units", 0) == 1)

    def execute(self, lease, materials):
        # type: (object, object) -> BlockReason
        # 旧能力入口不允许绕过主控和订单上下文。
        return BlockReason(Reason.BAD_PARAMETER, u"装配须由订单经主控派发")

    def describe(self):
        # type: () -> dict
        return {"available": True, "leases": len(self.leases),
                "timing": "R10 未定：按调度预算推进"}

    def release(self, instance):
        # type: (object) -> None
        state = instance.assembly
        if state and self.leases.get(state["holder"]) == instance.execution_id:
            self.leases.pop(state["holder"], None)
            if self.on_changed is not None:
                self.on_changed(state["holder"])

    def advance(self, executor, order, instance, tick):
        # type: (object, object, object, int) -> tuple
        """只从 STORAGE assembly_advance 的准入后处理器调用。"""
        try:
            return self._checked_advance(executor, order, instance, tick)
        except Exception as error:
            detail = u"装配容器访问待核对：%s: %s" % (type(error).__name__, error)
            if instance.assembly is not None:
                # 已开始执行时，引擎可能先写后抛，不自动重放。
                instance.assembly["uncertain"] = detail
                return self._blocked(Reason.CONTAINER_UNCERTAIN, detail)
            return self._blocked(Reason.NO_ADAPTER, detail)

    def _checked_advance(self, executor, order, instance, tick):
        # type: (object, object, object, int) -> tuple
        service = executor.service
        store = service.patterns
        lease = service.slots.lease_of(instance.execution_id)
        if not self.accepts_lease(lease):
            return self._blocked(Reason.ORDER_NO_CAPACITY, u"装配没有有效处理槽位租约")
        if not callable(getattr(service.returns, "accept_assembled", None)):
            return self._blocked(Reason.ORDER_RETURNS_ABSENT, u"装配产物回流入口未接入")
        ref = executor._step_ref(instance)
        if ref is None or ref.generation != instance.step.generation:
            return self._blocked(Reason.DEVICE_GONE, u"装配仓实例已被移除或替换")
        provider = store.get_by_ref(ref)
        if provider is None:
            return self._blocked(Reason.DEVICE_GONE, u"装配仓已离线")
        blocker = store.provider_blocker(provider)
        if blocker is not None:
            return 0, blocker, 0
        adapter = store.container_at(ref.node_key)
        if adapter is None or not adapter.is_valid():
            return self._blocked(Reason.NO_ADAPTER, u"装配仓容器不可读写")
        holder = "%s" % (ref.as_key(),)
        owner = self.leases.get(holder)
        if owner is not None and owner != instance.execution_id:
            return self._blocked(Reason.ORDER_PORT_BUSY, u"装配仓正在执行另一批配方")
        state = instance.assembly
        if state is None:
            entry, blocker = store.check_dispatch(
                ref, instance.step.slot, instance.step.digest, MODE_CRAFTING)
            if blocker is not None:
                return 0, blocker, 0
            state, blocker = self._prepare(adapter, entry.definition, holder)
            if blocker is not None:
                return 0, blocker, 0
            instance.assembly = state
            self.leases[holder] = instance.execution_id
            instance.status = StepStatus.STAGING
            if self.on_changed is not None:
                self.on_changed(holder)
        if state["uncertain"]:
            return self._blocked(Reason.CONTAINER_UNCERTAIN, state["uncertain"])
        if state["phase"] == "done":
            return executor._track_outputs(order, instance, tick), None, 0
        # 取得使用权会从空闲切为工作；必须按新需求再检查，不能用旧准入投料。
        blocker = power_blocker(executor.runtime.services, ref.node_key)
        if blocker is not None:
            return 0, blocker, 0
        return self._advance(executor, order, instance, adapter, tick)

    def _prepare(self, adapter, definition, holder):
        # type: (object, object, str) -> tuple
        rows = []
        ordered = definition.is_ordered()
        for item in definition.inputs:
            if item.key.kind != "item" or item.unlimited:
                return None, BlockReason(Reason.UNSUPPORTED, u"实体装配槽不支持流体或不限量输入")
            left = item.count
            while left:
                slot = item.slot if ordered else len(rows)
                limit = slot_limit_of(adapter, item.key)
                if slot is None or slot > 8 or (ordered and left > limit):
                    return None, BlockReason(Reason.NO_CAPACITY, u"配方输入超出九格实体槽容量")
                take = min(left, limit)
                rows.append({"slot": slot, "key": item.key.to_dict(),
                             "count": take, "staged": 0, "consumed": False})
                left -= take
        products = []
        for item in tuple(definition.outputs) + tuple(definition.returns):
            if item.key.kind != "item":
                return None, BlockReason(Reason.UNSUPPORTED, u"装配输出槽只支持物品")
            products.append({"key": item.key.to_dict(), "remaining": item.count})
        for slot in range(10):
            if slot_read(adapter, slot)["count"]:
                return None, BlockReason(Reason.SLOT_OCCUPIED, u"装配原料或输出槽有未处置物品")
        return {"holder": holder, "phase": "staging", "inputs": rows,
                "products": products, "output": None, "uncertain": "",
                "operation": None}, None

    def _advance(self, executor, order, instance, adapter, tick):
        # type: (object, object, object, object, int) -> tuple
        state = instance.assembly
        if state["phase"] == "staging":
            # 每次实际投料前复核样板，已消费之后保留原配方产出账。
            _entry, blocker = executor.service.patterns.check_dispatch(
                executor._step_ref(instance), instance.step.slot,
                instance.step.digest, MODE_CRAFTING)
            if blocker is not None:
                return 0, blocker, 0
            for row in state["inputs"]:
                key = ResourceKey.from_dict(row["key"])
                view = slot_read(adapter, row["slot"])
                if view["count"] != row["staged"] or (view["count"] and view["key"] != key):
                    return self._blocked(Reason.SLOT_CHANGED, u"已投入的装配原料被改动")
                need = row["count"] - row["staged"]
                if not need:
                    continue
                item = next(item for item in instance.inputs if item.key == key)
                blocker = executor._stock_blocker_for_staging(order, item, need)
                if blocker is not None:
                    return 0, blocker, 0
                released = executor._release_for_staging(order, item, need)
                receipt = self._move(executor, instance, adapter, row["slot"], key, need, False)
                row["staged"] += receipt.moved
                item.staged += receipt.moved
                item.delivered += receipt.moved
                if released > receipt.moved:
                    executor._restore_reservation(order, item, released - receipt.moved)
                return self._moved(state, receipt)
            if slot_read(adapter, 9)["count"]:
                return self._blocked(Reason.SLOT_OCCUPIED, u"输出槽被占用，尚未消费原料")
            state["phase"] = "consuming"
            return 1, None, 0
        if state["phase"] == "consuming":
            if slot_read(adapter, 9)["count"]:
                return self._blocked(Reason.SLOT_OCCUPIED, u"输出槽被占用，暂停消费原料")
            for row in state["inputs"]:
                if row["consumed"]:
                    continue
                key = ResourceKey.from_dict(row["key"])
                view = slot_read(adapter, row["slot"])
                if view["key"] != key or view["count"] != row["count"]:
                    return self._blocked(Reason.SLOT_CHANGED, u"待消费原料与本批次不一致")
                self._write(adapter, row["slot"], None)
                row["consumed"] = True
                return 1, None, 0
            # 全部消费确认后才产生输出权限；登记等待早于任何产物回流。
            executor._finish_inputs(order, instance, tick)
            state["phase"] = "outputs"
            return 1, None, 0
        output = state["output"]
        if output is not None:
            key = ResourceKey.from_dict(output["key"])
            view = slot_read(adapter, 9)
            if view["key"] != key or view["count"] != output["count"]:
                return self._blocked(Reason.SLOT_CHANGED, u"装配输出被改动，不能重新生成")
            receipt = self._move(executor, instance, adapter, 9, key, output["count"], True)
            output["count"] -= receipt.moved
            if not output["count"]:
                state["output"] = None
            if receipt.moved and receipt.status != TransferStatus.UNCERTAIN:
                executor.service.returns.accept_assembled(receipt.op_id, key, receipt.moved, tick)
            work, blocker, moved = self._moved(state, receipt)
            return work, blocker, moved
        for product in state["products"]:
            if not product["remaining"]:
                continue
            if slot_read(adapter, 9)["count"]:
                return self._blocked(Reason.SLOT_OCCUPIED, u"输出槽没有空间")
            key = ResourceKey.from_dict(product["key"])
            count = min(product["remaining"], slot_limit_of(adapter, key))
            self._write(adapter, 9, item_dict_from_key(key, count))
            product["remaining"] -= count
            state["output"] = {"key": product["key"], "count": count}
            return 1, None, 0
        state["phase"] = "done"
        self.release(instance)
        executor._track_outputs(order, instance, tick)
        return 1, None, 0

    def _move(self, executor, instance, adapter, slot, key, count, returning):
        # type: (object, object, object, int, object, int, bool) -> object
        storage = executor._storage()
        network = NetworkEndpoint(storage, executor.runtime.component_id)
        endpoint = SlotView(adapter, slot, instance.assembly["holder"])
        source, target = (endpoint, network) if returning else (network, endpoint)
        instance.op_attempt += 1
        op_id = "assembly/%s/%d" % (instance.execution_id, instance.op_attempt)
        request = TransferRequest(op_id, source.endpoint_id(), target.endpoint_id(),
                                  key, count, executor._topology_version(),
                                  owner_id=instance.execution_id)
        instance.assembly["operation"] = {"op_id": op_id, "slot": slot,
                                          "key": key.to_dict(), "count": count,
                                          "returning": returning}
        receipt = storage.transfer().commit(request, source, target)
        instance.assembly["operation"]["receipt"] = receipt.to_dict()
        return receipt

    def _write(self, adapter, slot, data):
        # type: (object, int, object) -> None
        # 不信任布尔返回值；读回不一致/异常后锁住执行，绝不重放整批。
        adapter.write_slot(slot, data)
        actual = slot_read(adapter, slot)
        if data is None:
            matches = actual["count"] == 0
        else:
            matches = actual["key"] == item_key_from_dict(data) and actual["count"] == data["count"]
        if not matches:
            raise ValueError("装配槽写入读回不一致")

    def _moved(self, state, receipt):
        # type: (dict, object) -> tuple
        if receipt.status == TransferStatus.UNCERTAIN:
            state["uncertain"] = u"资源转移结果不确定，保留账本等待核对"
            return self._blocked(Reason.CONTAINER_UNCERTAIN, state["uncertain"])
        if not receipt.moved:
            return self._blocked(receipt.reason or Reason.NO_CAPACITY, u"装配资源转移暂未完成")
        return 1, None, receipt.moved

    def _blocked(self, code, detail):
        # type: (str, str) -> tuple
        return 0, BlockReason(code, detail), 0
