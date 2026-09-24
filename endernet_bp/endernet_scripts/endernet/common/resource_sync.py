# coding=utf-8
"""会话内资源描述映射，供服务端发送与客户端解码分别持有。

外层负责可靠有序传输、快照/查询版本与请求去重。未知序号必须重新同步，
不能猜测资源。重开/清空时使用全新的 epoch；同 epoch 内序号不回收。
"""

from .resource_key import ResourceKey, _TEXT_TYPES, _require_count
from . import settings


class ResourceResyncRequired(ValueError):
    """当前映射不能解释消息，需要获取当前会话的完整描述。"""


class ResourceDescriptions(object):
    def __init__(self, network_id, epoch, max_entries=65536):
        if settings.DEBUG:
            if not isinstance(epoch, _TEXT_TYPES) or not epoch:
                raise ValueError("A nonempty session epoch is required")
            _require_count(max_entries, "description capacity")
            if not 0 < max_entries <= 65536:
                raise ValueError("Description capacity must be in 1..65536")
        self.network_id = network_id
        self.epoch = epoch
        self.max_entries = max_entries
        self._by_key = {}
        self._by_serial = {}
        self._next_serial = 1

    def _check_serial(self, epoch, serial):
        if epoch != self.epoch:
            raise ResourceResyncRequired("Stale resource epoch")
        _require_count(serial, "resource serial")
        if not 0 < serial <= self.max_entries:
            raise ResourceResyncRequired("Invalid resource serial")

    def encode(self, key, include_description=False):
        if settings.DEBUG and not isinstance(key, ResourceKey):
            raise TypeError("Only available ResourceKeys can be synchronized")
        serial = self._by_key.get(key)
        is_new = serial is None
        if is_new:
            serial = self._next_serial
            self._check_serial(self.epoch, serial)
            self._by_key[key] = serial
            self._by_serial[serial] = key
            self._next_serial += 1
        result = {"epoch": self.epoch, "serial": serial}
        if is_new or include_description:
            result["description"] = key.to_dict()
        return result

    def resolve(self, epoch, serial):
        """服务端解析点击引用；仅查本会话映射，不接受客户端提供的新键。"""
        self._check_serial(epoch, serial)
        if serial not in self._by_serial:
            raise ResourceResyncRequired("Unknown resource serial")
        return self._by_serial[serial]

    def decode(self, reference):
        """客户端接收描述/数量引用。所有检查通过后才改变映射。"""
        if not isinstance(reference, dict):
            raise TypeError("Expected a resource reference")
        if set(reference) - set(("epoch", "serial", "description")):
            raise ValueError("Unknown resource reference fields")
        epoch, serial = reference["epoch"], reference["serial"]
        self._check_serial(epoch, serial)
        if "description" not in reference:
            return self.resolve(epoch, serial)
        key = ResourceKey.from_dict(reference["description"])
        previous = self._by_serial.get(serial)
        previous_serial = self._by_key.get(key)
        if ((previous is not None and previous != key)
                or (previous_serial is not None and previous_serial != serial)):
            raise ResourceResyncRequired("Resource serial was rebound")
        if previous is None and len(self._by_serial) >= self.max_entries:
            raise ResourceResyncRequired("Resource description capacity exceeded")
        self._by_serial[serial] = key
        self._by_key[key] = serial
        self._next_serial = max(self._next_serial, serial + 1)
        return key
