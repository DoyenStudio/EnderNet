# coding=utf-8
"""本地模拟背包：**已无生产消费者，只作测试夹具保留**。

界面早在终端下半屏接真实玩家背包时就改用 `backpack_model.BackpackModel` 了
（点击=选中、搬运在服务端）。这个类留着的理由不是"以后可能用得上"，而是
它守着一批**搬运语义**的边界用例（`test_terminal_inventory.py` 的
`InventoryTests`）：同格再点取消、部分合堆留余、特殊元数据永不合并、随机
搬运守恒。这些口径在搬运改由服务端做之后**仍然成立**，是后续手势扩展
（R12：拖拽、按住取半、右键取一个）现成的回归位。

**不要再把它接回界面**：它按属性算身份（`item_key`），而真实物品必须走
`engine_item_key`——两套算法算出来的指纹不同，接回去的表现是每一次取放都
被判 `SLOT_CHANGED`（客户端与网络算的不是同一个身份）。
"""

from copy import deepcopy

from ...common.item_codec import item_key 


BACKPACK_COUNT = 27
HOTBAR_START = BACKPACK_COUNT
HOTBAR_COUNT = 9
SLOT_COUNT = BACKPACK_COUNT + HOTBAR_COUNT

try:
    INTEGER_TYPES = (int, long)
except NameError:
    INTEGER_TYPES = (int,)


class InventoryModel(object):
    """Select a source, then move/merge/swap with a destination; no cursor stack.

    Item instances supplied by callers are copied, including nested metadata.
    A partial merge leaves the remainder selected in its original slot.
    """

    def __init__(self, items=None):
        self._slots = [None] * SLOT_COUNT
        self.selected = None
        if items is not None:
            self.replace(items)

    def replace(self, items):
        items = list(items)
        if len(items) != SLOT_COUNT:
            raise ValueError("Expected exactly 36 simulated slots")
        slots = []
        for item in items:
            if item is None or item.newItemName == "minecraft:air" or item.count == 0:
                slots.append(None)
                continue
            limit = item.GetBasicInfo().maxStackSize
            if (isinstance(item.count, bool) or not isinstance(item.count, INTEGER_TYPES)
                    or item.count < 0 or item.count > limit):
                raise ValueError("Invalid simulated stack count")
            item_key(item)  # Validate identity before committing the replacement.
            slots.append(deepcopy(item))
        # Validate everything before replacing the current inventory.
        self._slots = slots
        self.selected = None

    @staticmethod
    def _check_slot(index):
        if (isinstance(index, bool) or not isinstance(index, INTEGER_TYPES)
                or not 0 <= index < SLOT_COUNT):
            raise ValueError("Invalid simulated slot index")

    def get_item(self, index):
        self._check_slot(index)
        return deepcopy(self._slots[index])

    def snapshot(self):
        return deepcopy(self._slots)

    def cancel(self):
        old = self.selected
        self.selected = None
        return () if old is None else (old,)

    def click(self, index):
        """Return only the slot indices whose content/selection needs repainting."""
        self._check_slot(index)
        if self.selected is None:
            if self._slots[index] is None:
                return ()
            self.selected = index
            return (index,)
        if self.selected == index:
            return self.cancel()

        source_index = self.selected
        source, target = self._slots[source_index], self._slots[index]
        if target is None:
            self._slots[index], self._slots[source_index] = source, None
            self.selected = None
        elif item_key(source) == item_key(target):
            moved = min(source.count, max(0, target.GetBasicInfo().maxStackSize - target.count))
            if moved == 0:
                return ()
            target.count += moved
            source.count -= moved
            if source.count == 0:
                self._slots[source_index] = None
                self.selected = None
        else:
            self._slots[index], self._slots[source_index] = source, target
            self.selected = None
        return (source_index, index)
