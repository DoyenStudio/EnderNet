# coding=utf-8
"""真实容器与玩家背包的引擎实现。

对应任务 06 的 13/06 接缝（06 文档把这一份归在 `server/adapters/`）。

**本模块是引擎边界**，允许 import `tooldelta` / `mod.*`——但**导入必须留在
方法体内**：`bootstrap.py` 在模块级 import 本模块，顶层 import 引擎会让整条
离线链路失效（`test_import_boundaries` 静态拦着，而那个错误在游戏里看不出来）。

## 三条不能省的纪律

1. **不看 `PutItemIntoContainer` 的返回值。** 架构规划 §6.3 点名的缺陷形态：
   它调用 `SetContainerItem` 却不看布尔返回值，在空槽路径更是直接报告
   「全部放入」。因此本模块**自己逐槽写入并逐槽读回核对**，
   13-01 的 `GhostContainer` 就是它的行为替身。
2. **实际读写不经 `tooldelta.define.item.Item` 包装。** 那一层把 `durability` 的
   `None` 归一成 `0`，而 `common/item_codec.py` 按「空值与缺省是不同身份」
   处理——包装一次就会让同一件物品在两条路径上得到不同的 `ResourceKey`。
   所以实际读写一律走 `mod.server.extraServerApi` 的**原始字典**；
   存取验证事件单独包装副本，监听者的物品修改不用于写回。
3. **写不进去就说写不进去。** 逐槽写入之后立刻读回，只把真的变了的部分算数；
   整段无法核对时把回执降级成 `UNCERTAIN`（基类已经这么做了，子类只要
   别假装成功）。

## 游戏内实测到的事实（2026-09-25）

**已确认**：

- `GetContainerSize` 对一个**原版箱子**返回 27，调用形状（
  `CreateItem(GetLevelId())` + `GetContainerSize(pos, dim)`）正确；
- `SpawnItemToContainer(data, slot, pos, dim)` 能把网络里的物品**真的写进
  原版箱子**——取货口的整条链路（轮询 → 主控 → 服务 → 协调器 → 引擎写）
  在真机上跑通，箱子里读回来 12 个石头；
- `GetContainerItem(pos, slot, dim, True)` 对原版箱子返回 **16 个字段**，
  身份字段的「空值」是 `[]` / `""` / `0` 而不是 `None`——见
  `normalize_engine_item`。

**仍未验证**：玩家背包的三个原语（`GetPlayerItem` /
`SpawnItemToPlayerInv` / `SetInvItemNum`）一次都没在真机上跑过；
`GetContainerItem` 对**本模组自己的** `netease:block_container` 是否与
原版箱子同形，也只测过驱动器那一种（05 §8.2），介质之外没试。

离线可验证的部分（槽位语义、拆分、合并、部分接收、读回核对、归一化）
由注入的替身原语覆盖，见 `endernet_tests/test_containers_engine.py`。
"""

from copy import deepcopy

from ...common.item_codec import (
    is_empty_item,
    item_dict_from_key,
    item_key_from_dict,
)
from ...common.item_codec import normalize_engine_item  # noqa: F401 (转出给服务端调用点)
from ...common.protocol import (
    ContainerCapabilities,
    Reason,
    TransferReceipt,
    TransferStatus,
)
from ...common.resource_key import ResourceKey  # noqa: F401 (仅用于 # type: 注释)
from ..storage.transfer import InventoryEndpoint, slot_extract, slot_insert
from .containers import ContainerAdapter, ContainerEntry
from .engine_text import engine_text

#: 玩家背包的槽位数：27 格背包 + 9 格快捷栏。
#: 与 `client/ui/inventory_model.py` 的 `SLOT_COUNT` 是同一个数，
#: 但两处各自成立——那边是界面布局，这边是服务端寻址。
PLAYER_SLOT_COUNT = 36

#: 空槽的规范表示。写回整表时用它占位，而不是 `None`：
#: 引擎的 `SetPlayerAllItems` 对 `None` 的行为未验证，用空气字典更保守。
EMPTY_ITEM = {
    "newItemName": "minecraft:air",
    "newAuxValue": 0,
    "count": 0,
    "showInHand": True,
}


def _is_empty(data):
    # type: (object) -> bool
    """空槽判定。**实现只有一处**：`common/item_codec.is_empty_item`。

    07 读样板槽时也要判空，而它不该 import 本模块（领域层不依赖适配器）。
    两份实现分叉的表现是「某一处没判空气」，于是空槽被当成一件
    `minecraft:air` 物品喂给严格转换器，在一个毫不相干的地方炸掉。
    """
    return is_empty_item(data)


# 引擎物品的归一化（`normalize_engine_item` 及其四张表）已移到
# `common/item_codec.py`——**服务端与客户端必须走同一个入口**，
# 否则两边算出的 `ResourceKey` 不同，带槽请求会全部被判 `SLOT_CHANGED`。
# `_is_empty` / `normalize_engine_item` 仍从本模块名字可用（见上方 import）。


# -- 按槽读写的容器基类 ----------------------------------------------------


class SlotBackedContainer(ContainerAdapter):
    """按槽读写的容器。三个原语由子类（或测试）提供。

    **基类负责核对，子类负责说真话。** 与 13-01 的 `ContainerAdapter` 同一
    分工：`_do_insert` / `_do_extract` 可以「说错话」，对外的
    `insert` / `extract` 会按读回的实际变化量给出回执。

    子类必须提供：

    - `slot_count()` -> int
    - `read_slot(slot)` -> dict | None     该槽的原始物品字典
    - `_write_slot(slot, data)` -> bool    **按原始数据写回**（`data` 是字典）
    - `stack_limit_of(key)` -> int         该资源的堆叠上限

    **`None` 到空气字典的翻译放在基类**（`write_slot`），子类只实现
    `_write_slot`。这不只是省事：清空槽位在容器上是写一个空气物品、
    在背包上是 `SetInvItemNum(slot, 0)`——**两条路径的「空」不是同一个
    表达**，让每个子类自己记得翻译必然会有一处漏掉，而漏掉的表现是
    「取不空」这种看起来像额度问题的症状。
    """

    adapter_id = "slot_backed"

    def __init__(self, stack_limit=None):
        # type: (object) -> None
        #: `ResourceKey -> 最大堆叠`。默认 64；真实实现按 `GetItemBasicInfo` 查。
        self._stack_limit = stack_limit if stack_limit is not None else (lambda key: 64)
        self.valid = True
        self.last_write_failures = 0

    # -- 子类契约 ------------------------------------------------------

    def slot_count(self):
        # type: () -> int
        raise NotImplementedError

    def read_slot(self, slot):
        # type: (int) -> object
        raise NotImplementedError

    def _write_slot(self, slot, data):
        # type: (int, dict) -> bool
        raise NotImplementedError

    def write_slot(self, slot, data):
        # type: (int, object) -> bool
        """写回一个槽。`data is None` 表示清空。"""
        return self._write_slot(
            slot, dict(EMPTY_ITEM) if data is None else dict(data)
        )

    def stack_limit_of(self, resource_key):
        # type: (ResourceKey) -> int
        limit = int(self._stack_limit(resource_key))
        return limit if limit > 0 else 1

    # -- 只读 ----------------------------------------------------------

    def capabilities(self):
        return ContainerCapabilities(
            can_read=True,
            can_insert=True,
            can_simulate=True,
            # 引擎没有事务：「放进去」是一槽一槽写的，中途可以停。
            supports_partial=True,
            is_atomic=False,
            # 容器没有变化通知（13-05）：调用方退化到退避轮询。
            notify_token=None,
            max_slots=self.slot_count(),
        )

    def slots_count(self):
        return self.slot_count()

    def entries(self):
        # type: () -> list
        out = []
        for slot in range(self.slot_count()):
            data = self.read_slot(slot)
            if _is_empty(data):
                continue
            key = item_key_from_dict(data)
            if key is None:
                continue
            out.append((slot, ContainerEntry(key, int(data.get("count", 0)))))
        return out

    def _count_of(self, resource_key):
        # type: (ResourceKey) -> int
        total = 0
        for _, entry in self.entries():
            if entry.resource_key == resource_key:
                total += entry.count
        return total

    # -- 模拟 ----------------------------------------------------------

    def _do_simulate_insert(self, resource_key, count):
        # type: (ResourceKey, int) -> int
        limit = self.stack_limit_of(resource_key)
        room = 0
        for slot in range(self.slot_count()):
            data = self.read_slot(slot)
            if _is_empty(data):
                room += limit
                continue
            if item_key_from_dict(data) == resource_key:
                room += max(0, limit - int(data.get("count", 0)))
        return min(room, count)

    # -- 提交 ----------------------------------------------------------

    def _do_insert(self, op_id, resource_key, count):
        # type: (str, ResourceKey, int) -> int
        """逐槽写入并**逐槽读回**。返回真的落下的数量。

        为什么逐槽读回而不是写完再整体数一遍：整体数一遍会把「本来就在
        槽里的同类物品」算成本次放进去的。逐槽读回还能定位到具体是哪一槽
        没写进去——那正是 `SetContainerItem` 返回 False 时最需要的信息。
        """
        limit = self.stack_limit_of(resource_key)
        remaining = count
        placed = 0
        for slot in range(self.slot_count()):
            if remaining <= 0:
                break
            data = self.read_slot(slot)
            if not _is_empty(data) and item_key_from_dict(data) != resource_key:
                continue
            if _is_empty(data):
                before = 0
                target = dict(item_dict_from_key(resource_key, 1))
            else:
                before = int(data.get("count", 0))
                target = dict(data)
            room = limit - before
            if room <= 0:
                continue
            take = min(room, remaining)
            target["count"] = before + take
            if not self.write_slot(slot, target):
                self.last_write_failures += 1
                continue
            after = self.read_slot(slot)
            # 空槽不是「写进了别的东西」，是「什么都没写进去」——两种情况
            # 都按 0 算，但**不能对 None 调 `item_key_from_dict`**（它会抛）。
            after_key = None if _is_empty(after) else item_key_from_dict(after)
            if after_key != resource_key:
                self.last_write_failures += 1
                continue
            got = int(after.get("count", 0))
            placed += max(0, got - before)
            remaining -= max(0, got - before)
        return placed

    def _do_extract(self, op_id, resource_key, count):
        # type: (str, ResourceKey, int) -> int
        remaining = count
        taken = 0
        for slot in range(self.slot_count()):
            if remaining <= 0:
                break
            data = self.read_slot(slot)
            if _is_empty(data) or item_key_from_dict(data) != resource_key:
                continue
            before = int(data.get("count", 0))
            take = min(before, remaining)
            if take <= 0:
                continue
            left = before - take
            if left > 0:
                target = dict(data)
                target["count"] = left
            else:
                target = None
            if not self.write_slot(slot, target):
                self.last_write_failures += 1
                continue
            after = self.read_slot(slot)
            # 同 `_do_insert`：空槽合法，但**不能对 None 取身份**。
            after_key = None if _is_empty(after) else item_key_from_dict(after)
            if after_key is not None and after_key != resource_key:
                self.last_write_failures += 1
                continue
            got = 0 if _is_empty(after) else int(after.get("count", 0))
            moved = before - got
            if moved <= 0:
                continue
            taken += moved
            remaining -= moved
        return taken

    # -- 按槽操作（06-09 的槽锚要的就是这个）---------------------------

    def insert_slot(self, op_id, slot, resource_key, count):
        # type: (str, int, ResourceKey, int) -> TransferReceipt
        """**只动这一个槽**地放入。

        语义（堆叠上限、读回核对、槽被占时的处理）只有一份实现，在
        `storage/transfer.slot_insert`；这里只是把它接到本适配器的三个
        原始操作上。与 `FakeContainer` 的分工完全相同——**真机能做的，
        替身也要能做**。
        """
        return slot_insert(self, op_id, slot, resource_key, count)

    def extract_slot(self, op_id, slot, resource_key, count):
        # type: (str, int, ResourceKey, int) -> TransferReceipt
        """**只动这一个槽**地取出。语义同 `insert_slot`。"""
        return slot_extract(self, op_id, slot, resource_key, count)

    def describe(self):
        # type: () -> dict
        report = ContainerAdapter.describe(self)
        report["slots"] = self.slot_count()
        report["write_failures"] = self.last_write_failures
        return report


# -- 引擎原语 --------------------------------------------------------------


class EngineItemPrimitives(object):
    """`mod.server.extraServerApi` 的物品容器原语。

    **每个方法体内才 import 引擎**，因此本模块可以被 `bootstrap.py` 在
    模块级 import 而不会破坏离线链路。返回值一律是**原始字典**，理由见
    模块 docstring 第 2 条。

    调用形状照着 `tooldelta/api/server/{container,player}.py` 抄：
    容器三类用 `CreateItem(GetLevelId())`，玩家三类用
    `CreateItem(player_id)`——**两者不是同一个组合**，写错在真机上
    只会表现为「返回 None」或「没反应」。
    """

    def _container_comp(self):
        from mod.server.extraServerApi import GetEngineCompFactory, GetLevelId

        return GetEngineCompFactory().CreateItem(GetLevelId())

    def _player_comp(self, player_id):
        from mod.server.extraServerApi import GetEngineCompFactory

        return GetEngineCompFactory().CreateItem(player_id)

    def container_size(self, dimension, position):
        # type: (int, tuple) -> int
        size = self._container_comp().GetContainerSize(tuple(position), dimension)
        return int(size or 0)

    def container_item(self, dimension, position, slot):
        # type: (int, tuple, int) -> object
        # `getUserData=True`：旗帜一类物品的身份在 userData 里，
        # 不取它会让两件不同的东西得到同一个 `ResourceKey`。
        return self._container_comp().GetContainerItem(
            tuple(position), slot, dimension, True
        )

    def set_container_item(self, dimension, position, slot, data):
        # type: (int, tuple, int, dict) -> bool
        # 所有外部容器写入（含指定槽位、清空和补偿）共用这一道检查。
        # 事件只承载本次增减的物品，不能把合并后的整堆数量当成投递量。
        before = normalize_engine_item(self.container_item(dimension, position, slot))
        after = normalize_engine_item(data)
        before_key = None if _is_empty(before) else item_key_from_dict(before)
        after_key = None if _is_empty(after) else item_key_from_dict(after)
        before_count = 0 if before_key is None else int(before.get("count", 0))
        after_count = 0 if after_key is None else int(after.get("count", 0))
        removed = before_count - (after_count if before_key == after_key else 0)
        added = after_count - (before_count if before_key == after_key else 0)
        for inserting, source, count in ((False, before, removed), (True, after, added)):
            if count <= 0:
                continue
            piece = deepcopy(source)
            piece["count"] = count
            if not self._broadcast_container_transfer(
                dimension, position, slot, piece, inserting
            ):
                return False
        # 广播同步执行第三方代码；槽位被监听者改动时不能覆盖新内容。
        current = normalize_engine_item(self.container_item(dimension, position, slot))
        if current != before:
            return False
        # 内部身份把空值归一为 None，但写入引擎需要恢复这三个字段。
        # 2026-10-04 红石熔炉实测：任一个传 None 都会写入成功，却不派发
        # ContainerItemChangedServerEvent；传 0 / 空字符串后事件恢复。
        item = dict(data)
        for field, empty in (("durability", 0), ("customTips", ""), ("extraId", "")):
            if item.get(field) is None:
                item[field] = empty
        # **返回值必须看。** `tooldelta` 的 `PutItemIntoContainer` 正是在这里
        # 丢掉了布尔值，才出现「空槽路径报告全部放入」的缺陷。
        # 参数顺序照 `tooldelta/api/server/container.py`：`(item, slot, pos, dim)`。
        return bool(
            self._container_comp().SpawnItemToContainer(
                item, slot, tuple(position), dimension
            )
        )

    def _broadcast_container_transfer(self, dimension, position, slot, data, inserting):
        # type: (int, tuple, int, dict, bool) -> bool
        """广播引擎存取事件；仅用 Item 包装事件副本，不改变实际写入身份。"""
        from ....tooldelta.api.server import GetBlockName
        from ....tooldelta.define.item import Item
        from ....tooldelta.events.server import (
            ItemPullOutCustomContainerServerEvent,
            ItemPushInCustomContainerServerEvent,
        )

        event_class = (ItemPushInCustomContainerServerEvent if inserting
                       else ItemPullOutCustomContainerServerEvent)
        piece = deepcopy(data)
        piece.setdefault("newAuxValue", 0)
        piece.setdefault("showInHand", True)
        event = event_class(
            item=Item.from_dict(piece),
            collectionName=GetBlockName(dimension, tuple(position)) or "",
            collectionIndex=slot,
            x=position[0],
            y=position[1],
            z=position[2],
            dimension=dimension,
            _orig={},
        )
        return not event.broadcast()._orig.get("cancel", False)

    def player_slot_count(self, player_id):
        # type: (str) -> int
        return PLAYER_SLOT_COUNT

    def player_item(self, player_id, slot):
        # type: (str, int) -> object
        from mod.server.extraServerApi import GetMinecraftEnum

        return self._player_comp(player_id).GetPlayerItem(
            GetMinecraftEnum().ItemPosType.INVENTORY, slot, True
        )

    def set_player_item(self, player_id, slot, data):
        # type: (str, int, dict) -> bool
        """写一个背包槽。

        两条路径分开：放东西用 `SpawnItemToPlayerInv`（单槽、不动别人），
        清空用 `SetInvItemNum(slot, 0)`（同样是单槽）。**不使用
        `SetPlayerAllItems` 整表替换**——那会覆盖同一帧内其它槽的改动，
        而改动就发生在我们读完那 36 个槽之后。
        """
        comp = self._player_comp(player_id)
        if not _is_empty(data):
            return bool(comp.SpawnItemToPlayerInv(dict(data), player_id, slot))
        return bool(comp.SetInvItemNum(slot, 0))

    def swap_player_items(self, player_id, slot_a, slot_b):
        # type: (str, int, int) -> bool
        """交换玩家背包的两个槽。

        引擎自带这个原语（`SetInvItemExchange`），因此**不要用「读两格再
        写两格」实现**：那是两次非原子写入，第二次失败会把 A 的内容同时留在
        两处——**复制**比丢失更难收拾，而总量守恒正好查不出来。引擎这一条
        是一次调用，成败就是一个布尔值。

        它也是本模块唯一一个「一次动两格」的原语，因此没有对应的容器版本：
        `SpawnItemToContainer` 只有单槽写入，容器上要做同样的事就得回到
        那两次写入。**要支持容器交换时另说，别顺手拿它拼一个。**

        `slot_a` / `slot_b` 与 `set_player_item` 的 `slot` 同一口径
        （物品栏槽位），不是客户端那两段界面槽号。
        """
        return bool(self._player_comp(player_id).SetInvItemExchange(slot_a, slot_b))

    def stack_limit(self, name, aux):
        # type: (str, int) -> int
        # 过边界统一转 `str`（见 `engine_text` 的说明）。**`GetItemBasicInfo`
        # 实测是接受 `unicode` 的**，因此这一处不是修 bug，是把跨接口的
        # 那条习惯落到每一处调用点上——只做一半的代价是下一个人看不出
        # 哪些接口是「必须转」、哪些是「顺手转」，于是每一处都要重新查一遍。
        info = self._container_comp().GetItemBasicInfo(
            engine_text(name), aux)
        if not info:
            return 1
        limit = int(info.get("maxStackSize", 1) or 1)
        return limit if limit > 0 else 1


# -- 具体容器 --------------------------------------------------------------


class EngineItemContainer(SlotBackedContainer):
    """一个真实方块容器（箱子、熔炉、本模组自己的 `netease:block_container`）。

    按 `(dimension, position)` 定位。写出时用的是 `SpawnItemToContainer`
    并**检查它的布尔返回值**，写完逐槽读回——架构 §6.3 与 06 验收
    「当前 ToolDelta 高层插入方法的失败路径单独验证」都要求这一条。
    """

    adapter_id = "engine_container"

    def __init__(self, primitives, dimension, position, stack_limit=None):
        # type: (object, int, tuple, object) -> None
        SlotBackedContainer.__init__(self, stack_limit)
        self.primitives = primitives
        self.dimension = int(dimension)
        self.position = tuple(position)
        self._size = None

    def slot_count(self):
        # type: () -> int
        if self._size is None:
            self._size = int(self.primitives.container_size(self.dimension, self.position))
        return self._size

    def read_slot(self, slot):
        # type: (int) -> object
        if not self.valid:
            raise IOError("容器已失效，不应再被读取")
        # **归一化在引擎边界做**，因此 `entries` / `_count_of` / 逐槽读回
        # 三条路径看到的都是同一种形状。
        return normalize_engine_item(
            self.primitives.container_item(self.dimension, self.position, slot)
        )

    def _write_slot(self, slot, data):
        # type: (int, dict) -> bool
        if not self.valid:
            return False
        return self.primitives.set_container_item(
            self.dimension, self.position, slot, data
        )


class EnginePlayerInventory(SlotBackedContainer):
    """一个玩家的背包（36 槽）。

    06 文档明写「玩家背包作为实际转移端点，使用玩家、槽号、槽版本和物品指纹
    定位，**不能以客户端显示索引代替**」：槽号就是这里的 `slot`，
    槽版本与指纹锚定由 `InventoryEndpoint` 负责（`slot_view` / `SlotAnchor`），
    两者组合起来才是完整的定位。
    """

    adapter_id = "engine_player_inventory"

    def __init__(self, primitives, player_id, stack_limit=None):
        # type: (object, str, object) -> None
        SlotBackedContainer.__init__(self, stack_limit)
        self.primitives = primitives
        self.player_id = player_id

    def slot_count(self):
        # type: () -> int
        return int(self.primitives.player_slot_count(self.player_id))

    def read_slot(self, slot):
        # type: (int) -> object
        if not self.valid:
            raise IOError("背包已失效，不应再被读取")
        return normalize_engine_item(self.primitives.player_item(self.player_id, slot))

    def _write_slot(self, slot, data):
        # type: (int, dict) -> bool
        if not self.valid:
            return False
        return self.primitives.set_player_item(self.player_id, slot, data)

    def swap_slots(self, slot_a, slot_b):
        # type: (int, int) -> bool
        """交换两个槽（06-09 的第五个动作）。

        **只有背包有它。** 基类 `SlotBackedContainer` 刻意**不**提供一个
        「读两格写两格」的默认实现：那会把复制风险铺到每个容器上，而
        `_slot_primitive("swap_slots")` 取不到时端点本来就会答
        `unsupported`——**明确说不支持，好过悄悄给一个会复制物品的实现**。
        """
        if not self.valid:
            return False
        return self.primitives.swap_player_items(self.player_id, slot_a, slot_b)


# -- 提供者 -----------------------------------------------------------------


class EngineContainerProvider(object):
    """位置 -> 容器适配器。

    按位置缓存，**缓存要能被作废**（13-05：邻接容器改变、维度变化、卸载
    或依赖关闭时失效）。本轮提供 `invalidate` / `invalidate_all` 与
    `capabilities` 的 `notify_token=None`（没有变化通知，调用方退避轮询），
    但**没有把作废接到生命周期事件上**——那是 13-05 的接线。
    """

    def __init__(self, primitives, stack_limit=None):
        # type: (object, object) -> None
        self.primitives = primitives
        self._stack_limit = stack_limit
        self._cache = {}
        self.stats = {"probes": 0, "misses": 0, "invalidations": 0}

    def container_at(self, node_key):
        # type: (object) -> object
        """该位置的容器适配器；**那里不是容器时返回 None**。"""
        key = node_key.as_key()
        self.stats["probes"] += 1
        cached = self._cache.get(key)
        if cached is not None:
            return cached if cached.is_valid() else None
        try:
            size = self.primitives.container_size(
                node_key.dimension, node_key.as_tuple()
            )
        except Exception:
            # 区块没加载、位置没有方块实体等都会抛。**返回 None 而不是
            # 造一个空容器**——空容器会被当成「有这个容器但它是空的」，
            # 于是取货口会一直往一个不存在的地方送货。
            self.stats["misses"] += 1
            return None
        if size <= 0:
            self.stats["misses"] += 1
            return None
        adapter = EngineItemContainer(
            self.primitives, node_key.dimension, node_key.as_tuple(), self._stack_limit
        )
        self._cache[key] = adapter
        return adapter

    def invalidate(self, node_key):
        # type: (object) -> bool
        adapter = self._cache.pop(node_key.as_key(), None)
        if adapter is None:
            return False
        adapter.invalidate()
        self.stats["invalidations"] += 1
        return True

    def invalidate_all(self):
        # type: () -> int
        count = len(self._cache)
        for adapter in self._cache.values():
            adapter.invalidate()
        self._cache = {}
        self.stats["invalidations"] += count
        return count

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report["cached"] = len(self._cache)
        return report


class EngineInventoryProvider(object):
    """玩家 ID -> 背包适配器 / 端点。"""

    def __init__(self, primitives, stack_limit=None):
        # type: (object, object) -> None
        self.primitives = primitives
        self._stack_limit = stack_limit
        self._cache = {}
        self._endpoints = {}

    def inventory_of(self, player_id):
        # type: (str) -> object
        if not player_id:
            return None
        adapter = self._cache.get(player_id)
        if adapter is None:
            adapter = EnginePlayerInventory(
                self.primitives, player_id, self._stack_limit
            )
            self._cache[player_id] = adapter
        return adapter

    def endpoint_of(self, player_id):
        # type: (str) -> object
        """背包端点。带槽版本与指纹锚定，供 06-09 的按槽命令使用。"""
        adapter = self.inventory_of(player_id)
        if adapter is None:
            return None
        endpoint = self._endpoints.get(player_id)
        if endpoint is None:
            endpoint = InventoryEndpoint(adapter, player_id)
            self._endpoints[player_id] = endpoint
        return endpoint

    def forget(self, player_id):
        # type: (str) -> bool
        """玩家退出时丢弃缓存。**不写盘**（退出恢复规则属 R12/12）。"""
        adapter = self._cache.pop(player_id, None)
        self._endpoints.pop(player_id, None)
        if adapter is None:
            return False
        adapter.invalidate()
        return True

    def diagnostics(self):
        # type: () -> dict
        return {"cached_players": len(self._cache)}


def build_stack_limit(primitives):
    # type: (object) -> object
    """按 `GetItemBasicInfo` 查堆叠上限的回调。

    查不到、或查到非正数时返回 1 而不是 64：**按 1 处理最多是多写几次**，
    按 64 处理会造出超堆叠的物品——而超堆叠在背包里是会被引擎拆掉或吞掉的，
    那是真的丢东西。夹在这里而不是 `SlotBackedContainer` 里：夹两处迟早
    有一处被改掉，而那种分叉只在真机上表现出来。
    """
    def limit_of(resource_key):
        # type: (ResourceKey) -> int
        try:
            limit = int(primitives.stack_limit(resource_key.name, resource_key.aux))
        except Exception:
            return 1
        return limit if limit > 0 else 1

    return limit_of
