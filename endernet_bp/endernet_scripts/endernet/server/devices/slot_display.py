# coding=utf-8
"""槽位模块显示的服务端一侧：从驱动器 / 内存槽的读数算出每槽的模型取值，变了就发。

规则（取值、变量名、载荷）在 `common/slot_display.py`；本模块只做两件事：

1. **算**：驱动器看 `DriverState.occupied()` 里每块介质的型号
   （`StorageDriverStore.medium(id).model_id`），内存槽看 `MemorySlotState.entries`
   里每根内存条的型号，按 `model_value` 换成数。
2. **发**：`advance()` 每帧由启动层调一次，只对 `revision` 变了的设备重算，
   算出来与上次发的不同才发（`publish` 回调，生产实现是全服广播）。
   `answer()` 回答客户端的单点请求。

**不 import 引擎**：朝向与发送都由启动层注入（`facing_of` / `publish`）。

### 为什么挂在 `revision` 上而不是加回调

两份读数都有 `revision`（每次重读读出不同内容就 +1），而 `refresh()` 有两条
调用路径（帧尾 `advance` 与放置时的 `ensure`），在两处各插一个回调容易漏一处。
这里每帧逐台比一次整数，代价是「设备台数次属性读」——与两份 `advance()`
本来就要做的「遍历一遍状态表」同一量级。
"""

from ...common.slot_display import (
    DISK,
    EMPTY,
    MEMORY,
    UNKNOWN,
    model_items,
    model_value,
    slot_count,
    update_payload,
)


class SlotDisplayTracker(object):
    """槽位模块读数的发布者。由启动层持有。"""

    def __init__(self, definitions, drivers, memory_slots, facing_of=None, publish=None):
        # type: (object, object, object, object, object) -> None
        self.definitions = definitions
        self.drivers = drivers
        self.memory_slots = memory_slots
        #: `NodeKey -> 朝向名 | None`。生产实现是 `world.facing_at`。
        self.facing_of = facing_of
        #: `payload -> None`。生产实现是 `adapters/slot_display.publish_slot_display`；
        #: 为 None 时只记账不发（离线测试与未接引擎时）。
        self.publish = publish
        #: `node_key.as_key() -> {"generation", "revision", "slots", "facing"}`。
        #: `slots` 是**上一次发出去的**读数；新设备从全空开始，因此空设备不发。
        self._entries = {}
        self._items = {
            DISK: model_items(definitions, DISK),
            MEMORY: model_items(definitions, MEMORY),
        }
        self.stats = {"published": 0, "answered": 0, "unknown_requests": 0, "errors": 0}
        self.last_error = ""

    # -- 计算 ----------------------------------------------------------

    def _stores(self):
        return ((DISK, self.drivers), (MEMORY, self.memory_slots))

    def slots_of(self, kind, state):
        # type: (str, object) -> tuple
        """一台设备当前读数对应的每槽取值。"""
        values = [EMPTY] * slot_count(kind)
        items = self._items[kind]
        if kind == DISK:
            for assignment in state.occupied():
                if not 0 <= assignment.slot < len(values):
                    continue
                medium = self.drivers.medium(assignment.medium_id)
                # 载体记录缺失的孤儿介质：盘确实插着，只是认不出型号。
                model_id = getattr(medium, "model_id", None) if medium is not None else None
                values[assignment.slot] = (
                    model_value(items, model_id) if model_id else UNKNOWN
                )
        else:
            for slot, entry in state.entries.items():
                if 0 <= slot < len(values):
                    values[slot] = model_value(items, entry.memory_id)
        return tuple(values)

    def _empty(self, kind):
        return (EMPTY,) * slot_count(kind)

    def _facing(self, entry, node_key):
        # type: (dict, object) -> str
        """朝向只读一次：同一代次的方块不会转向（转向 = 换方块 = 换代次）。"""
        if entry.get("facing"):
            return entry["facing"]
        facing = None
        if self.facing_of is not None:
            try:
                facing = self.facing_of(node_key)
            except Exception:
                # 读不到朝向不是不发的理由：按 north 画，下次再读。
                self.stats["errors"] += 1
                facing = None
        if facing:
            entry["facing"] = facing
        return facing or ""

    def _entry_for(self, kind, state):
        # type: (str, object) -> dict
        key = state.node_key.as_key()
        entry = self._entries.get(key)
        if entry is None or entry["generation"] != state.ref.generation:
            entry = {
                "generation": state.ref.generation,
                "revision": None,
                "slots": self._empty(kind),
                "facing": None,
            }
            self._entries[key] = entry
        return entry

    # -- 推进 ----------------------------------------------------------

    def advance(self):
        # type: () -> int
        """逐台比 `revision`，读数真的变了才发。返回本帧发了几台。

        **保证不抛**：它排在帧尾、落盘之前（`bootstrap.tick`），抛出去会让本帧
        的落盘一起被跳过。一台设备算不出来只记 `errors`，其余照常。
        """
        seen = set()
        published = 0
        for kind, store in self._stores():
            for state in store.states():
                try:
                    key = state.node_key.as_key()
                    seen.add(key)
                    if self._advance_one(kind, state):
                        published += 1
                except Exception as error:
                    self.stats["errors"] += 1
                    self.last_error = "%s: %s" % (type(error).__name__, error)
        for key in [key for key in self._entries if key not in seen]:
            # 设备没了（拆除、换代次之前的那一份）：忘掉，免得表只增不减。
            del self._entries[key]
        return published

    def _advance_one(self, kind, state):
        # type: (str, object) -> bool
        entry = self._entry_for(kind, state)
        if entry["revision"] == state.revision:
            return False
        entry["revision"] = state.revision
        slots = self.slots_of(kind, state)
        if slots == entry["slots"]:
            return False
        entry["slots"] = slots
        return self._send(state.node_key, entry)

    def _send(self, node_key, entry):
        # type: (object, dict) -> bool
        if self.publish is None:
            return False
        payload = update_payload(node_key, self._facing(entry, node_key), entry["slots"])
        try:
            self.publish(payload)
        except Exception as error:
            # 发送失败不能打断帧尾：客户端下次加载这台设备时会再来要。
            self.stats["errors"] += 1
            self.last_error = "%s: %s" % (type(error).__name__, error)
            return False
        self.stats["published"] += 1
        return True

    def answer(self, node_key):
        # type: (object) -> dict
        """回答一次单点请求：这台设备当前的读数。不认识的位置返回 None。"""
        for kind, store in self._stores():
            state = store.get(node_key)
            if state is None:
                continue
            entry = self._entry_for(kind, state)
            # **就地算，不动「已发」的账**：要是这一刻的读数还没轮到 `advance()`，
            # 把它记成已发会让其他已加载这台设备的玩家错过这次变化。
            # 代价只是请求者稍后再收到一次同样的广播。
            self.stats["answered"] += 1
            return update_payload(
                node_key, self._facing(entry, node_key), self.slots_of(kind, state)
            )
        self.stats["unknown_requests"] += 1
        return None

    def diagnostics(self):
        # type: () -> dict
        return dict(self.stats, tracked=len(self._entries), last_error=self.last_error)
