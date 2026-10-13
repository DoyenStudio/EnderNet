# coding=utf-8
"""订单恢复快照。保存真实资源键与逐次执行账，不从规范文本反解身份。"""

from ...common.identity import NodeKey
from ...common.protocol import BlockReason
from ...common.resource_key import ResourceKey
from .jobs import Order, OrderStep, ExecutionInstance, StepInput, StepOutput
from .planner import CraftPlan

VERSION = 1


def pack(value):
    if isinstance(value, ResourceKey):
        return {"_endernet_value": "resource", "data": value.to_dict()}
    if isinstance(value, NodeKey):
        return {"_endernet_value": "node", "data": list(value.as_key())}
    if isinstance(value, BlockReason):
        return {"_endernet_value": "reason", "data": value.to_dict()}
    if isinstance(value, tuple):
        return {"_endernet_value": "tuple", "data": [pack(item) for item in value]}
    if isinstance(value, list):
        return [pack(item) for item in value]
    if isinstance(value, dict):
        return dict((key, pack(item)) for key, item in value.items())
    return value


def unpack(value):
    if isinstance(value, list):
        return [unpack(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get("_endernet_value")
    if kind == "resource":
        return ResourceKey.from_dict(value["data"])
    if kind == "node":
        return NodeKey(*value["data"])
    if kind == "reason":
        data = value["data"]
        return BlockReason(data["code"], data.get("detail", ""))
    if kind == "tuple":
        return tuple(unpack(item) for item in value["data"])
    if kind is not None:
        raise ValueError("unknown order value: %s" % kind)
    return dict((key, unpack(item)) for key, item in value.items())


def fields(obj, excluded=()):
    return dict((name, pack(getattr(obj, name))) for name in obj.__slots__
                if name not in excluded)


def fill(obj, data, excluded=()):
    for name in obj.__slots__:
        if name not in excluded:
            setattr(obj, name, unpack(data[name]))
    return obj


ORDER_EXCLUDED = ("plan", "plan_job", "steps", "inflight", "memory_lease")
STEP_EXCLUDED = ("executions",)
INSTANCE_EXCLUDED = ("step", "inputs", "outputs", "returns")


def export_order(order):
    data = fields(order, ORDER_EXCLUDED)
    data["plan"] = (pack(order.plan.to_dict()) if order.plan is not None else None)
    data["inflight"] = sorted(order.inflight)
    data["steps"] = []
    for step in order.steps:
        row = fields(step, STEP_EXCLUDED)
        row["executions"] = []
        for instance in step.executions:
            execution = fields(instance, INSTANCE_EXCLUDED)
            for name in ("inputs", "outputs", "returns"):
                execution[name] = [fields(item) for item in getattr(instance, name)]
            row["executions"].append(execution)
        data["steps"].append(row)
    return data


def import_order(data, component_id, master_instance_id):
    order = Order(data["order_id"], component_id, master_instance_id, None, "", 1)
    # player_id 是后加的可选字段：旧快照没有可信发起者，保留为空。
    # 只补这一项，其余必需字段缺失仍应拒绝恢复，且不修改原存档。
    fill(order, dict(data, player_id=data.get("player_id", "")), ORDER_EXCLUDED)
    order.component_id = component_id
    order.master_instance_id = master_instance_id
    if order.amount <= 0 or order.canonical != order.target.canonical():
        raise ValueError("invalid order target")
    if order.status not in ("planning", "queued", "running", "blocked", "cancelling",
                            "completed", "cancelled", "failed"):
        raise ValueError("invalid order status")
    if order.memory_bytes < 0 or order.tasks_total < 0:
        raise ValueError("invalid order resource counters")
    if data["plan"] is not None:
        plan = CraftPlan.__new__(CraftPlan)
        plan.content = unpack(data["plan"])
        plan._digest = plan.content["seal"]["plan_digest"]
        order.plan = plan
    for raw in data["steps"]:
        step = fill(OrderStep(0, "", {}), raw, STEP_EXCLUDED)
        if not 0 <= step.completed <= step.created <= step.batches:
            raise ValueError("invalid execution counters")
        order.steps.append(step)
        for saved in raw["executions"]:
            instance = ExecutionInstance(order.order_id, step, saved["index"])
            if instance.execution_id != saved["execution_id"]:
                raise ValueError("invalid execution identity")
            fill(instance, saved, INSTANCE_EXCLUDED)
            if instance.status not in ("waiting_inputs", "staging", "dispatched",
                                       "waiting_outputs", "completed", "blocked", "cancelled"):
                raise ValueError("invalid execution status")
            for name in ("inputs", "outputs", "returns"):
                cls = StepInput if name == "inputs" else StepOutput
                setattr(instance, name, [fill(cls(None, "", 0), item)
                                         for item in saved[name]])
            for item in instance.inputs:
                if not 0 <= item.delivered_base <= item.delivered <= item.staged <= item.required:
                    raise ValueError("invalid input ledger")
            for item in instance.outputs + instance.returns:
                if not 0 <= item.received <= item.expected:
                    raise ValueError("invalid output ledger")
            step.executions.append(instance)
            if instance.execution_id in data["inflight"]:
                order.inflight[instance.execution_id] = instance
    if len(order.inflight) != len(data["inflight"]):
        raise ValueError("missing inflight execution")
    if order.adopted and order.plan is None and not order.is_terminal():
        raise ValueError("adopted order has no plan")
    order.recovery_pending = not order.is_terminal()
    return order
