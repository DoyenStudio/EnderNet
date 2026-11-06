# coding=utf-8
"""在正常保存边界写出两类终端的设备库存。方块实体只携带实例标记。"""

from .repository import Record


TERMINAL_RECORD_KIND = "endernet.terminal_inventories"
TERMINAL_RECORD_ID = "world"


class TerminalInventoryArchive(object):
    def __init__(self, world, registry):
        # type: (object, object) -> None
        self.read_id = getattr(world, "terminal_inventory_id", None)
        self.registry = registry
        self.bindings = {}
        self.stores = {}
        self.identity_error = None
        # 尚未重新发现的活动记录；区块没有加载时原样保留。
        self.pending = {}

    def add_store(self, device_id, store, rows, field, version):
        self.stores[device_id] = (store, rows, field, version)

    def key_for(self, ref):
        # type: (object) -> object
        key = self.bindings.get(ref)
        if key is None and self.read_id is not None:
            record = self.registry.get(ref.node_key)
            if record is None or not record.loaded or not record.ref.matches(ref):
                return None
            try:
                token = self.read_id(ref.node_key)
            except Exception as error:
                self.identity_error = "%r: %s" % (ref.node_key.as_key(), error)
                return None
            if token is None:
                return None
            key = ref.node_key.as_key() + (token,)
            self.bindings[ref] = key
            self._match(ref.node_key.as_key(), record.device_id, key)
        return key

    def _match(self, position, device_id, key):
        previous = self.pending.pop(position, None)
        if previous is not None and previous != (device_id, key):
            old_device, old_key = previous
            self.stores[old_device][0].detach_saved(old_key)

    def discovered(self, device_id, ref):
        if device_id in self.stores:
            self.key_for(ref)
        else:
            self._match(ref.node_key.as_key(), device_id, None)

    def unloaded(self, device_id, ref):
        key = self.bindings.pop(ref, None)
        if key is not None:
            self.pending[ref.node_key.as_key()] = (device_id, key)

    def removed(self, device_id, ref):
        if ref in self.bindings:
            self.stores[device_id][0].forget(ref)
            del self.bindings[ref]
        else:
            # 尚未核对实体身份，不能因同坐标的拆除事件发放旧样板。
            self._match(ref.node_key.as_key(), None, None)

    def restore(self, repository):
        # type: (object) -> int
        record = repository.load(TERMINAL_RECORD_KIND, TERMINAL_RECORD_ID)
        if record is None:
            return 0
        if record.read_only or record.payload.get("schema_version") != 1:
            raise ValueError("unsupported terminal inventory archive")
        pending = {}
        count = 0
        for device_id, (store, rows, field, version) in self.stores.items():
            snapshot = record.payload[device_id]
            if snapshot["schema_version"] != version:
                raise ValueError("unsupported terminal store version")
            seen = set()
            for row in snapshot[rows]:
                position = tuple(row["node"])
                if len(position) != 4 or row[field]["schema_version"] != version:
                    raise ValueError("invalid terminal inventory record")
                key = position + (row["generation"],)
                if key in seen:
                    raise ValueError("duplicate terminal inventory instance")
                seen.add(key)
                if not row["detached"]:
                    if position in pending:
                        raise ValueError("duplicate terminal inventory at %r" % (position,))
                    pending[position] = (device_id, key)
            count += store.restore(snapshot)
        self.bindings.clear()
        self.pending = pending
        return count

    def save(self, repository):
        # type: (object) -> int
        payload = {"schema_version": 1}
        has_state = False
        for device_id, (store, rows, field, version) in self.stores.items():
            snapshot = store.save_all()
            payload[device_id] = snapshot
            has_state = has_state or bool(snapshot[rows])
        if not has_state:
            return 0
        repository.write(Record(TERMINAL_RECORD_KIND, TERMINAL_RECORD_ID, payload))
        return 1
