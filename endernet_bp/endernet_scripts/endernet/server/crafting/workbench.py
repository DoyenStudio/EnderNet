# coding=utf-8
"""合成终端的工作台：九宫格、配方保留与导入计划（11-04）。

## 这里放什么

**九宫格是设备自己的库存**（UI 文档第 1 节），不是网络的，也不是玩家背包的，
因此它必须自己有一份权威状态：

- `WorkbenchGrid`：9 格实物 + 一个「保留的配方」标识。退出界面不返还、
  不清空——这正是 UI 文档「重开界面须读取设备现存九宫格」那条；
- `WorkbenchStore`：按 `DeviceRef` 存这些格。**按代次存**，同一个坐标上
  先后两个方块各有一份，旧的不被新的覆写（见 `forget()`）；
- `WorkbenchContainer`：把九宫格包成 `ContainerAdapter`，于是它能直接被
  `storage/transfer.py` 的 `ContainerEndpoint` 用——「网络 → 九宫格」的补料、
  「九宫格 → 网络」的清空都不需要另写一套搬运，回执也照旧由基类核对；
- `plan_import` / `plan_refill`：**纯函数**，只回答「要往哪几格放什么」，
  不碰库存、不碰引擎。

## 这里不放什么

不碰引擎（本包在 `test_import_boundaries.py` 的禁入名单里），因此：

- 物品叠加上限由**注入**的 `limit_of` 给（引擎那边是
  `build_stack_limit(primitives)`），默认退成 **1**——正好就是导入要的
  「每格一个」，也是「引擎给不出合法上限时退成 1」这条既有约定；
- 真正的搬运（从玩家背包取、从网络取、往网络存）由
  `storage/service.py` 的三个处理器用现成的 `transfer().commit()` 做，
  本模块只提供端点与计划。

## 与 `crafting/` 其余模块的关系

`patterns.py` / `planner.py` / `executor.py` 那条链是**工厂**（样板 → 规划 →
订单执行），工作台是**玩家手动合成**，两者共用 `crafting_recipe.py` 的配方形状
与 07 的显式校验纪律，但不共用订单状态机。**不要把工作台塞进订单里**：
订单是有进度、可取消、要回流的东西，而这里点一次就是一次，没有进度可言。
"""

from ...common.crafting_recipe import GRID_SIZE, SLOT_COUNT, match
from ...common.protocol import (
    ContainerCapabilities,
    Reason,
    TransferReceipt,
    TransferStatus,
)
from ...common.resource_key import ResourceKey
from ..adapters.containers import ContainerAdapter

#: 网格自身的存档版本。形状变了就加一，恢复时按它决定读还是跳过。
WORKBENCH_SCHEMA_VERSION = 1

#: 工作台没有「输出槽库存」——输出槽是预览，不是第 10 格（UI 文档第 4 节）。
#: 因此可搬运的槽位就是这 9 个。
SLOT_INDICES = tuple(range(SLOT_COUNT))

# -- 策略（§8 未定项的口径）-------------------------------------------------

#: 产物交给发起合成的玩家，背包放不下就整次失败、原料原样留下。
OUTPUT_PLAYER_INVENTORY = "player_inventory"

#: 补料只从网络取，绝不从玩家背包取（UI 文档第 5.3 节已确认）。
REFILL_NETWORK_ONLY = "network_only"

#: 导入时九宫格已有实物怎么办。
#: `refuse` = 判死整次导入，要求玩家先点「清空」。
IMPORT_REFUSE = "refuse"
IMPORT_KEEP = "keep"

#: 清空时网络收不下怎么办。`keep` = 收不下的留在格子里，绝不删除。
CLEAR_KEEP_REMAINDER = "keep_remainder"


class CraftingPolicy(object):
    """未定规则的显式口径。

    与 `BandwidthPolicy` / `EnergyPolicy` / `TransferPolicy` 同一套路数：
    `plans/ui/crafting_terminal_panel.md` 第 8 节列了 11 条未确认项，
    它们**不能靠临时实现悄悄定案**。这里把与代码有关的那几条提成具名字段，
    默认一律取**不丢物、不静默**的那一侧；`unresolved()` 列出哪些还没有定论，
    便于验收时核对，也便于将来逐条换成定案值而不是满仓库找魔法数字。
    """

    #: 产物去向。**玩家 2026-10-01 裁定「进玩家背包」**，因此这一条
    #: 已经不是替身了——它是本轮的口径，但仍经 `describe()` 外露，
    #: 因为背包放不下时整次失败这条行为还带着一个未定的分支（见 `unresolved`）。
    output_destination = OUTPUT_PLAYER_INVENTORY
    #: 补料来源。UI 文档已确认只有网络这一条来源。
    refill_source = REFILL_NETWORK_ONLY
    #: 每次补料往每格放几个。**未定**——文档只说「补料目标数量…尚未指定」，
    #: 并明确要求不要把导入阶段的「每格一个」擅自扩成补料规则。取 1 是
    #: 保守那侧：补少了玩家再点一次，补多了会凭空占掉网络库存。
    refill_per_slot = 1
    #: 导入时九宫格非空怎么办。**未定**（文档第 8 节第一条）。
    #: 取 `refuse`：既不覆盖也不删除任何实物，且不是静默的部分成功。
    import_when_occupied = IMPORT_REFUSE
    #: 每个原料槽位放几个。**已确认**：导入「每格一个」，不是一组。
    import_per_slot = 1
    #: 清空时网络收不下的部分怎么办。**未定**（文档第 8 节）。
    #: 取「留在格子里」——文档那条硬约束是「不能删除尚未成功存入网络的物品」。
    clear_remainder = CLEAR_KEEP_REMAINDER

    #: 还没定案的条目，只列与代码行为有关的。
    UNRESOLVED = (
        "refill_per_slot",
        "import_when_occupied",
        "clear_remainder",
        "import_source_priority",
        "output_when_inventory_full",
        "device_removed_disposition",
        "grid_manual_interaction",
    )

    def describe(self):
        # type: () -> dict
        return {
            "output_destination": self.output_destination,
            "refill_source": self.refill_source,
            "refill_per_slot": self.refill_per_slot,
            "import_when_occupied": self.import_when_occupied,
            "import_per_slot": self.import_per_slot,
            "clear_remainder": self.clear_remainder,
        }

    def unresolved(self):
        # type: () -> tuple
        """仍未定案的条目名。验收时对着它逐条问，而不是对着代码猜。"""
        return self.UNRESOLVED

    def unresolved_report(self):
        # type: () -> dict
        return {
            "policy": self.describe(),
            "unresolved": list(self.UNRESOLVED),
        }

    def assert_settled(self):
        """**只在正式验收时调用**，开发与离线测试都不该调。

        与 `BandwidthPolicy` 的同名方法同一个理由：它问的是「上面这些替身
        口径有没有被正式裁定过」，而不是「代码对不对」。开发期调用它只会
        得到一条没有信息量的失败。`test_storage.py` 有一条静态守卫扫全仓，
        禁止这个调用形状出现在业务代码里——**因此本方法的文档也不会写出
        那个调用形状**，否则守卫扫到的是注释。
        """
        if self.UNRESOLVED:
            raise AssertionError(
                "合成终端仍有未定口径：%s（见 plans/ui/crafting_terminal_panel.md 第 8 节）"
                % ("、".join(self.UNRESOLVED),)
            )


# -- 九宫格 ----------------------------------------------------------------

class WorkbenchGrid(object):
    """设备自己那 9 格，外加一个保留的配方标识。

    `recipe` 是 `PresetRecipe.digest()` 的字符串（或 `None`）。它**只是备忘**：
    导入之后记住「上次摆的是哪条配方」，好让界面重开时能显示。它**不参与**
    合成判定——判定永远看格子里实际有什么（见 `match`），因为玩家可能把
    格子手动动过，而备忘不会知道。
    """

    __slots__ = ("slots", "recipe", "recipe_output", "revision", "generation")

    def __init__(self, slots=None, recipe=None, recipe_output=None,
                 revision=0, generation=0):
        #: 9 项，每项 `(ResourceKey, count)` 或 `None`。
        self.slots = list(slots) if slots is not None else [None] * SLOT_COUNT
        if len(self.slots) != SLOT_COUNT:
            raise ValueError("工作台必须是 %d 格，得到 %d" % (
                SLOT_COUNT, len(self.slots)))
        self.recipe = recipe
        #: 保留配方**产物的 `(名字, aux)`**，或 `None`。
        #:
        #: 没有它就找不回那条配方：引擎只能「按产物查配方」与「按原料查
        #: 配方」，而九宫格给不出产物是哪个（产物正是我们要算的东西）。
        #: 因此导入时顺手把它记下来，`craft` 时按它重查一遍、再用 digest
        #: 挑出同一条。**它同样只是索引，不是权威**——真正能不能合成
        #: 由格子里实际有什么决定。
        self.recipe_output = recipe_output
        #: 每次内容变化加一。同步层靠它去重，客户端靠它认新旧。
        self.revision = revision
        #: 方块实例代次。**只用于诊断**——真正区分两份格的是 store 的键。
        self.generation = generation

    # -- 读 ------------------------------------------------------------

    def entry(self, index):
        # type: (int) -> object
        return self.slots[index]

    def key_at(self, index):
        # type: (int) -> object
        entry = self.slots[index]
        return None if entry is None else entry[0]

    def count_at(self, index):
        # type: (int) -> int
        entry = self.slots[index]
        return 0 if entry is None else entry[1]

    def is_empty(self):
        # type: () -> bool
        return all(entry is None for entry in self.slots)

    def occupied_slots(self):
        # type: () -> tuple
        return tuple(index for index in SLOT_INDICES
                     if self.slots[index] is not None)

    def total_of(self, resource_key):
        # type: (ResourceKey) -> int
        total = 0
        for entry in self.slots:
            if entry is not None and entry[0] == resource_key:
                total += entry[1]
        return total

    def cells(self):
        # type: () -> tuple
        """给 `crafting_recipe` 的匹配函数用的 9 元组。

        每项 `(名字, aux, 数量)` 或 `None`。**这里才把 `ResourceKey` 摊成
        名字 + aux**：配方匹配只看这两样（原版也不看附魔与自定义数据），
        而格子里的东西带着完整身份。
        """
        result = []
        for entry in self.slots:
            if entry is None:
                result.append(None)
            else:
                result.append((entry[0].name, entry[0].aux, entry[1]))
        return tuple(result)

    # -- 写 ------------------------------------------------------------

    def set_slot(self, index, key, count):
        # type: (int, object, int) -> None
        if count <= 0 or key is None:
            self.slots[index] = None
        else:
            self.slots[index] = (key, count)
        self._touch()

    def add_at(self, index, key, count):
        # type: (int, object, int) -> int
        """往某一格加东西。**同种才叠，异种不挤**——返回实际加进去的数量。"""
        entry = self.slots[index]
        if entry is None:
            self.slots[index] = (key, count)
            self._touch()
            return count
        if entry[0] != key:
            return 0
        self.slots[index] = (key, entry[1] + count)
        self._touch()
        return count

    def take_at(self, index, count):
        # type: (int, int) -> int
        """从某一格取走。返回实际取走的数量（可能少于请求）。"""
        entry = self.slots[index]
        if entry is None or count <= 0:
            return 0
        moved = min(count, entry[1])
        left = entry[1] - moved
        self.slots[index] = None if left <= 0 else (entry[0], left)
        self._touch()
        return moved

    def clear(self):
        # type: () -> None
        """清空全部格子。**调用方负责先把东西安置好**——这里只管清。"""
        self.slots = [None] * SLOT_COUNT
        self._touch()

    def set_recipe(self, recipe, output=None):
        # type: (object, object) -> None
        if recipe != self.recipe or output != self.recipe_output:
            self.recipe = recipe
            self.recipe_output = output
            self._touch()

    def _touch(self):
        self.revision += 1

    # -- 序列化 --------------------------------------------------------

    def to_dict(self):
        # type: () -> dict
        return {
            "schema_version": WORKBENCH_SCHEMA_VERSION,
            "revision": self.revision,
            "recipe": self.recipe,
            "recipe_output": self.recipe_output,
            "slots": [
                None if entry is None else {
                    "key": entry[0].to_dict(), "count": entry[1],
                }
                for entry in self.slots
            ],
        }

    @classmethod
    def from_dict(cls, payload):
        # type: (dict) -> WorkbenchGrid
        slots = [None] * SLOT_COUNT
        for index, row in enumerate(payload.get("slots") or ()):
            if index >= SLOT_COUNT or row is None:
                continue
            slots[index] = (ResourceKey.from_dict(row["key"]), row["count"])
        return cls(slots, payload.get("recipe"), payload.get("recipe_output"),
                   payload.get("revision", 0))

    def __repr__(self):
        return "WorkbenchGrid(occupied=%r, recipe=%r)" % (
            self.occupied_slots(), self.recipe,
        )


# -- 存档 ------------------------------------------------------------------

class WorkbenchStore(object):
    """按设备实例存工作台格。

    **键是 `(node_key, generation)`，不是坐标。** 同一个坐标上先后两个方块
    是两份东西：新的那份是空的，旧的那份连同里面没取走的物品一起留着。
    用坐标当键会让「拆掉再放回去」把旧物品**继承**给新方块，而那是
    凭空多出来的物品；反过来，直接丢掉旧那份则是凭空少掉物品。
    两条都不该在一条未定的规则上悄悄发生，因此这里选**留着但不可达**，
    并把处置方式记进 `CraftingPolicy.UNRESOLVED`（文档第 8 节最后一条）。
    """

    def __init__(self, accepts=None):
        #: `accepts(device_ref) -> bool`。**只对合成终端开口**：
        #: 没有它就等于给每条命令都变出一台空工作台，而「这台方块
        #: 根本没有九宫格」和「它的九宫格是空的」在界面上是两回事。
        #: 为 `None` 时不设限（离线夹具与测试直接用它）。
        self._accepts = accepts
        #: 活着的设备。
        self._grids = {}
        #: **被拆掉／注销的设备，格子连同里面的东西一起留着。**
        #: 见 `forget()` 为什么不是直接丢引用。
        self._orphans = {}
        self.stats = {"created": 0, "forgotten": 0, "restored": 0, "orphaned": 0}

    # -- 取 ------------------------------------------------------------

    @staticmethod
    def _key_of(ref):
        node_key = getattr(ref, "node_key", None)
        if node_key is None:
            raise ValueError("工作台需要一个设备实例身份（DeviceRef）")
        return (getattr(node_key, "dimension", 0), getattr(node_key, "x", 0),
                getattr(node_key, "y", 0), getattr(node_key, "z", 0),
                getattr(ref, "generation", 0))

    def of(self, ref):
        # type: (object) -> WorkbenchGrid
        """取这台设备的工作台，没有就建一个空的。"""
        key = self._key_of(ref)
        grid = self._grids.get(key)
        if grid is None:
            grid = WorkbenchGrid(generation=getattr(ref, "generation", 0))
            self._grids[key] = grid
            self.stats["created"] += 1
        return grid

    def get(self, ref):
        # type: (object) -> object
        return self._grids.get(self._key_of(ref))

    def accepts(self, ref):
        # type: (object) -> bool
        if self._accepts is None:
            return True
        try:
            return bool(self._accepts(ref))
        except Exception:
            # 判不出型号时**拒绝**，不是放行：放行会给一个来路不明的
            # 位置凭空造出一台工作台。
            return False

    def grid_for(self, ref):
        # type: (object) -> object
        """命令入口用这个，不要用 `of()`。

        不接受的设备返回 `None`——**不建、不报错**，由调用方转成一条
        「这台方块没有工作台」的拒绝。
        """
        if not self.accepts(ref):
            return None
        return self.of(ref)

    def forget(self, ref):
        # type: (object) -> bool
        """设备注销：从活动索引移到**孤儿索引**，内容一个不删。

        「方块被拆掉之后格子里那几件东西归谁」是 UI 文档第 8 节最后一条
        未定项。两种实现都很容易写，而且都很危险：

        - **直接丢引用** = 替这条规则定了「丢掉」，而且是一件物品都追不回来；
        - **留在活动索引里** = 替它定了「归下一个住在这个坐标的方块」，
          于是「拆了重放」会凭空多出物品。

        因此这里选了第三条：**留着，但不再被 `of()` 取到**。物品在存档里
        还在（`save_all()` 把它们一起写出去），谁也拿不走，等这条规则定了
        再决定是还、是烧、还是随方块一起消失。代价是孤儿会攒着——所以
        `diagnostics()` 把它们报出来，别让它悄悄长。
        """
        key = self._key_of(ref)
        grid = self._grids.pop(key, None)
        if grid is None:
            return False
        self._orphans[key] = grid
        self.stats["forgotten"] += 1
        self.stats["orphaned"] = len(self._orphans)
        return True

    def grids(self):
        # type: () -> tuple
        return tuple(self._grids.values())

    def orphans(self):
        # type: () -> tuple
        """被拆设备的格子。**里面是真物品**，不是历史记录。"""
        return tuple(self._orphans.values())

    def adopt(self, ref):
        # type: (object) -> object
        """把某个孤儿格子重新挂回活动索引。返回它，没有就返回 `None`。

        留给「这条未定规则将来定成『还回去』」那一刻用，本轮没有任何生产
        调用点——写出来是为了让上面那段话里的「再决定」有落点，而不是
        只能重写一遍 `forget`。
        """
        key = self._key_of(ref)
        grid = self._orphans.pop(key, None)
        if grid is None:
            return None
        self._grids[key] = grid
        self.stats["orphaned"] = len(self._orphans)
        return grid

    # -- 存档 ----------------------------------------------------------

    def save_all(self):
        # type: () -> dict
        """整份存盘载荷。形状与 `StorageDriverStore.save_all` 一致。

        **真机上还没有人调它。** 存档接线是任务 12 的事
        （`persistence/repository.py` 只留了契约），因此这份数据目前在内存里
        活着——「关掉界面再打开还在」成立，「重启服务器还在」不成立。
        交付说明里按这个区分写，不写成已经持久化。
        """
        rows = [
            {"node": list(key[:4]), "generation": key[4],
             "detached": detached, "grid": grid.to_dict()}
            for detached, source in ((False, self._grids), (True, self._orphans))
            for key, grid in sorted(source.items())
        ]
        return {"schema_version": WORKBENCH_SCHEMA_VERSION, "grids": rows}

    def restore(self, payload):
        # type: (dict) -> int
        """从 `save_all()` 的输出恢复。返回恢复的台数。

        版本比当前**新**的原样跳过，不读成空——与 12 的存档纪律一致：
        未知的更新版本是「读不懂」，不是「没有东西」。
        """
        if payload.get("schema_version", WORKBENCH_SCHEMA_VERSION) > WORKBENCH_SCHEMA_VERSION:
            return 0
        restored = 0
        for row in payload.get("grids") or ():
            node = row.get("node") or ()
            if len(node) != 4:
                continue
            key = (node[0], node[1], node[2], node[3], row.get("generation", 0))
            grid = WorkbenchGrid.from_dict(row.get("grid") or {})
            # 孤儿也一起恢复：它们是**真物品**，丢了就是丢物。
            if row.get("detached"):
                self._orphans[key] = grid
            else:
                self._grids[key] = grid
            restored += 1
        self.stats["restored"] += restored
        self.stats["orphaned"] = len(self._orphans)
        return restored

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report["grids"] = len(self._grids)
        report["orphans"] = len(self._orphans)
        report["occupied"] = sum(
            1 for grid in self._grids.values() if not grid.is_empty())
        #: 孤儿里压着的物品件数。**这是「还没决定归属」的量**，
        #: 它只涨不落说明有设备的格子一直没被处理。
        report["orphan_items"] = sum(
            grid.count_at(index)
            for grid in self._orphans.values()
            for index in grid.occupied_slots()
        )
        return report


# -- 容器适配器 ------------------------------------------------------------

class WorkbenchContainer(ContainerAdapter):
    """把九宫格包成 `ContainerAdapter`，好让转移协调器直接搬。

    子类只实现 `_do_insert` / `_do_extract` / `_count_of`，**回执由基类核对**
    （读前后总数、自己算差值），因此这里说错话不会流出去。

    只暴露那 9 格：**输出槽不是库存**，它是产物预览（UI 文档第 4 节
    「预览不是已生成物品」），把它算进容器会让「九宫格里有多少东西」
    这个数包含一个根本还没做出来的物品。
    """

    adapter_id = "workbench"

    def __init__(self, grid, limit_of=None):
        # type: (WorkbenchGrid, object) -> None
        self.grid = grid
        #: `limit_of(resource_key) -> int`。引擎给不出时退成 1。
        self._limit_of = limit_of if limit_of is not None else _limit_one

    def capabilities(self):
        # type: () -> object
        return ContainerCapabilities(
            can_read=True,
            can_insert=True,
            # 有模拟能力：`probe_space` 才答得出「还能塞几个」，
            # 转移协调器据此决定要不要动手。
            can_simulate=True,
            # 一格一格地放，放不下会如实说部分完成（基类按读数差核对）。
            supports_partial=True,
        )

    def slots_count(self):
        # type: () -> int
        return SLOT_COUNT

    def entries(self):
        # type: () -> list
        return [(index, self.grid.entry(index)) for index in SLOT_INDICES
                if self.grid.entry(index) is not None]

    def _count_of(self, resource_key):
        # type: (ResourceKey) -> int
        return self.grid.total_of(resource_key)

    def limit_of(self, resource_key):
        # type: (ResourceKey) -> int
        limit = self._limit_of(resource_key)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            # 与客户端 `stack_size.py` 同一条退化方向：**退成 1，不是退成可用数**。
            # 退成可用数会让「放一个」悄悄变成「放一组」。
            return 1
        return limit

    def slot_room(self, index, resource_key):
        # type: (int, ResourceKey) -> int
        """某一格还能再放几个这种东西。异种占着就是 0。"""
        entry = self.grid.entry(index)
        if entry is None:
            return self.limit_of(resource_key)
        if entry[0] != resource_key:
            return 0
        return max(0, self.limit_of(resource_key) - entry[1])

    def _do_simulate_insert(self, resource_key, count):
        # type: (ResourceKey, int) -> int
        room = 0
        for index in SLOT_INDICES:
            room += self.slot_room(index, resource_key)
            if room >= count:
                return count
        return room

    def _do_insert(self, op_id, resource_key, count):
        # type: (str, ResourceKey, int) -> int
        remaining = count
        for index in self._insert_order(resource_key):
            if remaining <= 0:
                break
            room = self.slot_room(index, resource_key)
            if room <= 0:
                continue
            placed = min(room, remaining)
            self.grid.add_at(index, resource_key, placed)
            remaining -= placed
        return count - remaining

    def _insert_order(self, resource_key):
        # type: (ResourceKey) -> tuple
        """先补已有的半堆，再开空格。

        九宫格只有 9 格，先把同种的半堆叠满能省下一格——`minecraft:book`
        那种「3 张纸 + 1 张皮革」的配方正好占满两格，散开摆会很快不够用。
        **只影响先放哪儿，不影响能放多少。**
        """
        same = []
        empty = []
        for index in SLOT_INDICES:
            entry = self.grid.entry(index)
            if entry is None:
                empty.append(index)
            elif entry[0] == resource_key:
                same.append(index)
        return tuple(same + empty)

    def insert_at(self, op_id, index, resource_key, count):
        # type: (str, int, ResourceKey, int) -> TransferReceipt
        """往**指定的那一格**放。

        导入要的是「每个原料槽位摆一个、摆在哪一格由配方决定」，而基类的
        `insert` 只承诺「放进这个容器」——它按自己的顺序找空位，位置不由
        调用方定。**两件事都需要**：清空/补料用 `insert`（位置无所谓），
        导入用 `insert_at`（位置就是配方本身）。

        核对方式与基类一致：读这一格前后的数量，自己算差值，**不采信
        调用方的话**。
        """
        if not 0 <= index < SLOT_COUNT:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED,
                Reason.BAD_PARAMETER, resource_key,
            )
        if count <= 0:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED,
                Reason.BAD_PARAMETER, resource_key,
            )
        before = self.grid.count_at(index)
        room = self.slot_room(index, resource_key)
        if room <= 0:
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED,
                Reason.CONTAINER_REFUSED, resource_key,
            )
        placed = min(room, count)
        self.grid.add_at(index, resource_key, placed)
        after = self.grid.count_at(index)
        moved = after - before
        if moved <= 0:
            # 声称放了、实际没动：如实说失败，不假装成功。
            return TransferReceipt(
                op_id, count, 0, TransferStatus.REFUSED,
                Reason.CONTAINER_REFUSED, resource_key,
            )
        if moved == count:
            return TransferReceipt(
                op_id, count, moved, TransferStatus.COMPLETE, "", resource_key,
            )
        return TransferReceipt(
            op_id, count, moved, TransferStatus.PARTIAL,
            Reason.CONTAINER_PARTIAL, resource_key,
        )

    def _do_extract(self, op_id, resource_key, count):
        # type: (str, ResourceKey, int) -> int
        remaining = count
        for index in SLOT_INDICES:
            if remaining <= 0:
                break
            entry = self.grid.entry(index)
            if entry is None or entry[0] != resource_key:
                continue
            remaining -= self.grid.take_at(index, min(entry[1], remaining))
        return count - remaining

    def describe(self):
        # type: () -> dict
        report = ContainerAdapter.describe(self)
        report["occupied"] = list(self.grid.occupied_slots())
        report["revision"] = self.grid.revision
        return report


def _limit_one(resource_key):
    # type: (ResourceKey) -> int
    """引擎给不出叠加上限时的退化值。

    **1，不是「格子的容量」**：导入的规则本来就是「每个原料槽位只摆一个」，
    而补料的口径未定，退成 1 最多是少放；退成大数会让一次补料把网络里
    那一种抽干。
    """
    return 1


# -- 计划（纯函数）---------------------------------------------------------

def plan_import(recipe, per_slot=1):
    # type: (object, int) -> tuple
    """导入时要往哪几格放什么。

    返回 `((槽位, 候选原料元组, 数量), ...)`，**按槽位升序**。

    - 有序配方按 `slot_layout()` 的铺法（左上角对齐）；
    - 无序配方没有位置可言，依次占 0、1、2…格；
    - **配方没有要求的位置不在结果里**，调用方据此保证不会被清空或覆盖；
    - 候选原料是一个**元组**（标签展开的那种符号有十几个候选），
      由调用方按「哪个来源真的有」去挑——挑哪一个是未定规则
      （文档第 8 节「原料来源优先级及替代原料」），不在这个纯函数里定。
    """
    plan = []
    if recipe.ordered:
        for index, cell in enumerate(recipe.slot_layout()):
            if cell is None:
                continue
            plan.append((index, recipe.symbols[_symbol_at(recipe, index)], per_slot))
    else:
        for offset, ingredient in enumerate(recipe.ingredients):
            if offset >= SLOT_COUNT:
                break
            plan.append((offset, (ingredient,), ingredient.count))
    return tuple(plan)


def _symbol_at(recipe, index):
    # type: (object, int) -> str
    row = index // GRID_SIZE
    col = index % GRID_SIZE
    return recipe.rows[row][col]


def plan_refill(recipe, cells, per_slot=1):
    # type: (object, tuple, int) -> tuple
    """合成之后要给哪几格补料。

    只对**当前摆位真正用到**的格子补（`cells` 是合成前的摆位），
    因为有序配方允许整体平移，铺在左上角的那套坐标不一定就是玩家实际
    摆的那套。补料只认「这一格空了」，不认位置。

    返回 `((槽位, 候选原料元组, 数量), ...)`，数量按 `per_slot`。
    """
    plan = []
    for index in SLOT_INDICES:
        if cells[index] is None:
            continue
        ingredient = _ingredient_for(recipe, cells[index])
        if ingredient is None:
            continue
        plan.append((index, (ingredient,), per_slot))
    return tuple(plan)


def _ingredient_for(recipe, cell):
    # type: (object, tuple) -> object
    """摆位上的这一格对应配方里的哪条要求。找不到就返回 `None`。"""
    name, aux = cell[0], cell[1]
    if recipe.ordered:
        for ingredient in _all_symbols(recipe):
            if ingredient.matches(name, aux):
                return ingredient
        return None
    for ingredient in recipe.ingredients:
        if ingredient.matches(name, aux):
            return ingredient
    return None


def _all_symbols(recipe):
    # type: (object) -> tuple
    result = []
    for alternatives in recipe.symbols.values():
        result.extend(alternatives)
    return tuple(result)


def preview_of(grid, recipe_source):
    # type: (object, object) -> object
    """当前摆位能做出什么。返回 `{name, aux, count}` 或 `None`。

    **不做合成**，只回答界面「点了会怎样」。判定与合成同一套：
    先按保留的配方 `digest` 找回那一条，再拿**格子里实际有什么**去匹配。
    格子被玩家动过时匹配不上，于是预览消失——这是对的，因为点下去
    也会被同一条判定拒绝。

    取不回配方（引擎那边出问题）时**让异常穿出去**：同步层那里有
    兜底，会变成「这一帧不发」而不是「预览为空」。后者会被读成
    「原料不够」，而真相是查询坏了。
    """
    if not grid.recipe or not grid.recipe_output:
        return None
    name = grid.recipe_output.get("name")
    if not name:
        return None
    output = ResourceKey.item(name, aux=grid.recipe_output.get("aux", 0) or 0)
    for recipe in recipe_source.recipes_for(output):
        if recipe.digest() != grid.recipe:
            continue
        if match(recipe, grid.cells()) is None:
            return None
        item = recipe.output_preview()
        return {"name": item.name, "aux": item.aux or 0, "count": item.count}
    return None
