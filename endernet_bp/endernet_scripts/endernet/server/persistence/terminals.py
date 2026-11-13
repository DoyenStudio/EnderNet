# coding=utf-8
"""在正常保存边界写出两类终端的设备库存。方块实体只携带实例标记。

## 键（阶段 D 起带上槽，13 §7）

- **实例键**（库存表里的键）：`(维度, x, y, z, 实例标记[, 面名])`。整方块终端没有面名，
  与改版前逐字相同；光缆面板（`DeviceRef.slot` 不是 center）多一个面名——同一格最多
  6 块面板各记各的。没接存档时实例标记就是代次（`instance_key`）。
- **位置**（等着重新发现的那张表）：`PartKey.as_key()`，即 `(维度, x, y, z[, 面名])`。
- **存档行**：`{"node": [维度, x, y, z], "generation": 实例标记, ...}`，面板多一个
  `"slot": 面名`（`saved_row` / `row_key` 是唯一的编解码点）。旧存档没有 `slot`，照旧读。

面板的实例标记存在宿主光缆的方块实体里、按面各一份（`world.terminal_inventory_id(PartKey)`），
挂上面板时换一个新的（`world.renew_terminal_inventory_id`），因此同一面拆下再挂一块，
不会继承上一块留下的东西。
"""

from ...common.identity import FACING_NAMES, is_part_ref
from .repository import Record


TERMINAL_RECORD_KIND = "endernet.terminal_inventories"
TERMINAL_RECORD_ID = "world"


def instance_key(ref):
    # type: (object) -> tuple
    """没接存档时的实例键：`(维度, x, y, z, 代次[, 面名])`。"""
    node_key = ref.node_key
    key = (getattr(node_key, "dimension", 0), getattr(node_key, "x", 0),
           getattr(node_key, "y", 0), getattr(node_key, "z", 0),
           getattr(ref, "generation", 0))
    if is_part_ref(ref):
        key += (ref.slot,)
    return key


def saved_row(key, fields):
    # type: (tuple, dict) -> dict
    """实例键 → 存档行（再并上 `fields`）。"""
    row = {"node": list(key[:4]), "generation": key[4]}
    if len(key) > 5:
        row["slot"] = key[5]
    row.update(fields)
    return row


def row_key(row):
    # type: (dict) -> object
    """存档行 → 实例键；位置不是四个数、或面名不认识，返回 None（这一行不读）。"""
    node = row.get("node") or ()
    if len(node) != 4:
        return None
    key = (node[0], node[1], node[2], node[3], row.get("generation", 0))
    slot = row.get("slot")
    if slot is None:
        return key
    if slot not in FACING_NAMES:
        return None
    return key + (slot,)


def row_position(row):
    # type: (dict) -> tuple
    """存档行 → 它等着被重新发现的位置（`PartKey.as_key()` 的形状）。"""
    position = tuple(row["node"])
    slot = row.get("slot")
    return position + (slot,) if slot is not None else position


class TerminalInventoryArchive(object):
    def __init__(self, world, registry, device_of=None):
        # type: (object, object, object) -> None
        self.read_id = getattr(world, "terminal_inventory_id", None)
        self.registry = registry
        #: `ref -> 这台终端（这块面板）记在哪张库存表上`（表按整方块终端的型号登记）；
        #: 对不上返回 None。不给就只认整方块设备（记录的型号）。
        self.device_of = device_of if device_of is not None else self._record_device
        self.bindings = {}
        #: `ref -> 记在哪张表上`：卸载时把面板连同宿主一起挪进待发现表要用到
        self.bound_ids = {}
        self.stores = {}
        self.identity_error = None
        # 尚未重新发现的活动记录；区块没有加载时原样保留。
        self.pending = {}

    def add_store(self, device_id, store, rows, field, version):
        self.stores[device_id] = (store, rows, field, version)

    def _record_device(self, ref):
        # type: (object) -> object
        if is_part_ref(ref):
            return None
        record = self.registry.get(ref.node_key)
        if record is None or not record.ref.matches(ref):
            return None
        return record.device_id

    def key_for(self, ref):
        # type: (object) -> object
        key = self.bindings.get(ref)
        if key is None and self.read_id is not None:
            record = self.registry.get(ref.node_key)
            if record is None or not record.loaded or not record.ref.matches(ref.host()):
                return None
            device_id = self.device_of(ref)
            if device_id is None:
                return None
            try:
                token = self.read_id(ref.part_key if is_part_ref(ref) else ref.node_key)
            except Exception as error:
                self.identity_error = "%r: %s" % (ref.part_key.as_key(), error)
                return None
            if token is None:
                return None
            key = ref.node_key.as_key() + (token,)
            if is_part_ref(ref):
                key += (ref.slot,)
            self.bindings[ref] = key
            self.bound_ids[ref] = device_id
            self._match(ref.part_key.as_key(), device_id, key)
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
            self._match(ref.part_key.as_key(), device_id, None)

    def unloaded(self, device_id, ref):
        doomed = [ref]
        if not is_part_ref(ref):
            # 宿主光缆卸载：挂在它上面的面板一起等重新发现
            doomed += [bound for bound in self.bindings
                       if is_part_ref(bound) and bound.host() == ref]
        for bound in doomed:
            key = self.bindings.pop(bound, None)
            store_id = self.bound_ids.pop(bound, device_id)
            if key is not None:
                self.pending[bound.part_key.as_key()] = (store_id, key)

    def removed(self, device_id, ref):
        if ref in self.bindings:
            self.stores[device_id][0].forget(ref)
            del self.bindings[ref]
            self.bound_ids.pop(ref, None)
        else:
            # 尚未核对实体身份，不能因同坐标的拆除事件发放旧样板。
            self._match(ref.part_key.as_key(), None, None)

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
                key = row_key(row)
                if key is None or row[field]["schema_version"] != version:
                    raise ValueError("invalid terminal inventory record")
                if key in seen:
                    raise ValueError("duplicate terminal inventory instance")
                seen.add(key)
                if not row["detached"]:
                    position = row_position(row)
                    if position in pending:
                        raise ValueError("duplicate terminal inventory at %r" % (position,))
                    pending[position] = (device_id, key)
            count += store.restore(snapshot)
        self.bindings.clear()
        self.bound_ids.clear()
        self.pending = pending
        return count

    def snapshot(self):
        # type: () -> dict
        """两类终端的库存载荷（纯数据）；`save` 与定期落盘的「变没变」比较共用。"""
        payload = {"schema_version": 1}
        for device_id, (store, _rows, _field, _version) in self.stores.items():
            payload[device_id] = store.save_all()
        return payload

    def save(self, repository):
        # type: (object) -> int
        payload = self.snapshot()
        has_state = any(payload[device_id][rows]
                        for device_id, (_store, rows, _field, _version) in self.stores.items())
        if not has_state:
            return 0
        repository.write(Record(TERMINAL_RECORD_KIND, TERMINAL_RECORD_ID, payload))
        return 1
