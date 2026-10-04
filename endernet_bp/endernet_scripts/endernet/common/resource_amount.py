# coding=utf-8
"""物料数量的显示格式；单位物与资源原生数量由调用者明确区分。"""

from .resource_key import (
    KIND_ITEM, KIND_FLUID, MICRO_UNITS_PER_UNIT, FLUID_MICRO_UNITS_PER_MB,
    _require_count,
)


def format_resource_amount(kind, amount, native=False):
    # type: (str, int, bool) -> str
    """默认输入整数单位物；native=True 输入物品个数或流体 mB。"""
    _require_count(amount, "物料数量")
    if kind == KIND_ITEM:
        return "%d" % amount
    if kind == KIND_FLUID:
        millibuckets = amount if native else (
            amount * MICRO_UNITS_PER_UNIT // FLUID_MICRO_UNITS_PER_MB
        )
        return "%dmB" % millibuckets
    raise ValueError("未知物料类型：%r" % (kind,))
