# coding=utf-8
"""资源条目的恢复边界；尚不接入真实介质后端。

缺失注册项、未知版本和损坏条目保留原载荷，不生成可交易的 ResourceKey。
这不是 ResourceKey 的子类，避免占位资源被正常转移路径误接受。
"""

from copy import deepcopy

from ...common.resource_key import ResourceKey, _require_count
from ...common import settings


class UnavailableResourceError(ValueError):
    pass


class RestoredResourceEntry(object):
    """读取 ContainerEntry.to_dict() 格式，保留整个条目（含数量及未知字段）。

    is_available(key) 必须由装配层提供，核实目标游戏/模组注册表；
    不根据 namespace 猜测模组存在。恢复对象只读，修改库存须先 require_key。
    """

    __slots__ = ("_payload", "_key", "_error")

    def __init__(self, payload, is_available):
        self._payload = deepcopy(payload)
        self._key = None
        self._error = None
        if settings.DEBUG and not callable(is_available):
            raise TypeError("A resource availability resolver is required")
        try:
            if not isinstance(payload, dict):
                raise TypeError("Resource entry must be an object")
            if set(payload) != set(("resource_key", "count")):
                raise ValueError("Unknown or missing resource entry fields")
            _require_count(payload["count"], "resource count")
            key = ResourceKey.from_dict(payload["resource_key"])
        except (KeyError, TypeError, ValueError) as error:
            self._error = "invalid_resource: %s" % error
            return
        try:
            available = is_available(key)
        except Exception as error:
            # 注册表暂不可读也不能被误认为资源不存在，原始数据保留以便重试。
            self._error = "availability_unconfirmed: %s" % error
            return
        if available is not True:
            self._error = "missing_resource"
            return
        self._key = key

    @property
    def available(self):
        return self._key is not None

    @property
    def error(self):
        return self._error

    @property
    def count(self):
        """仅已核验条目可用于数量记账；异常原值通过 to_dict 保留。"""
        self.require_key()
        return self._payload["count"]

    def require_key(self):
        if not self.available:
            raise UnavailableResourceError(self._error)
        return self._key

    def to_dict(self):
        return deepcopy(self._payload)

    def retry(self, is_available):
        return type(self)(self._payload, is_available)
