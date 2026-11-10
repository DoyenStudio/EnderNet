# coding=utf-8
"""面板（`.claude/13` §7，阶段 D；2026-11-10 照 AE2 改放置、加面板底座与面板骨架）：
挂上、打开、底座换光缆、外观读数、整格被挖时的掉落，以及给以后的面板种类留的口子。

面板**不是**设备记录：它只存在于宿主记录的 `config["parts"]`（`{面名: 面板标识}`，同方块
实体 `endernet:parts`）。宿主是能挂面板的光缆（玻璃、普通）或面板底座。面板的身份是
`PartKey(宿主位置, 面名)`，实例引用是 `DeviceRef(宿主位置, 宿主代次, 面名)`（`part_ref`）；
会话、终端库存、带宽、能源与设备状态都按这一份记账，「这块面板是什么」一律经
`DeviceCatalog.resolve` / `parts_of` 回到宿主的记录。

## 朝向

每块面板占一格的某一面，**正面朝这一面的外侧**（挂在光缆上就是现在的样子，作者确认）。
正面所对的那一格是 `PartKey.front_key()`。

## 挂（意图 `place_panel`，手持面板物品；照 AE2 `PartPlacement`）

潜行与不潜行走同一套放置规则（2026-11-10 起潜行也挂，作者确认按推荐）：潜行右键能贴到终端、
箱子这类会开界面的方块上，不潜行点它们照旧开界面（客户端 `action_for` 不接管）。

| 被点的 | 结果 |
| --- | --- |
| 光缆或面板底座，点中臂 / 线芯，求出的那一面空着、这格能挂 | 挂在这一面（臂 → 那一面；线芯 → 射线进入线芯的那一面） |
| 光缆或面板底座，其余情况（那一面已占、点中的是面板、致密光缆、没点中部件） | **像火把**：相邻格 = 被点格 + 被点击的面，面 = 被点击面的反方向 |
| 别的方块 B，点的是面 F | 相邻格 = B + F，面 = F 的反方向（贴在 B 上、正面朝着 B） |

相邻格是空气、或是草 / 水 / 雪层这类**可替换方块**（世界的 `may_place`：引擎
`BlockInfoComponentServer.MayPlace` 判「面板底座能不能放进这一格」，2026-11-10）→ 新建面板底座
挂上（可替换方块被顶掉，同原版放方块）；是能挂面板的格子（玻璃、普通光缆、底座，已加载）且
那一面空着 → 加上；都不是 → 不放。**放不上一律静默**（不弹提示，作者 2026-11-10）。不检查
实体挡路（作者确认按推荐：底座可以新建在玩家站的格子里）。扣一个物品（创造模式不扣）→ 这一面
换新的库存标记、清空面板配置 → 写入 → 放置音效；后几步失败就把物品退回去、新建的底座拆掉。
物品进出与音效走伪原版交互那一层（`server/interaction`）。

## 底座换光缆（意图 `add_cable`，手持光缆物品、不潜行点面板底座）

玻璃或普通光缆：这一格原地换成那种光缆，面板与库存标记原样保留（同一实例，
`CableIntentService.convert` → `ChangeKind.HOST_CONVERTED`），扣一个物品（创造不扣），播放置
音效。致密光缆挂不了面板：静默不放。服务端另在 `ServerItemUseOnEvent` 再拦一道（`register` 给
`UseGuard` 登记 `add_cable` 规则）：手持光缆点面板底座，原版那次放方块一律取消。

## 开 / 拆 / 挖

| 流程 | 入口 | 做什么 |
| --- | --- | --- |
| 开 | 右键开界面的统一入口（`server/interaction/opening.py`，与整方块终端同一条）：`register` 把 `claim_open` 登记进 `Openers` | 空手或手持任何物品、不潜行点中面板 → 它那一种的界面（终端：对应整方块终端的屏，以面板引用开，四个终端屏不改），原版那次放方块 / 用物品一并取消；潜行交给原版；没点中面板不认领，原版照常 |
| 拆 | 扳手（`cable_intents.py`）→ `ChangeKind.PART_DETACHED` | 启动层关会话、处置终端库存（与整方块终端被拆一致） |
| 挖 | 移除事件 → 生命周期在宿主的 `REMOVED` 后逐块发 `PART_DETACHED` | 同「拆」，另外 `drop_if_orphaned`：宿主已经没了 → 面板物品掉在原地 |

## 面板骨架（给总线接口面板、P2P 面板等以后的种类；评估见 `.claude/13` 「面板框架」一节）

- 种类：定义表的 `part_kind`（`PART_KINDS`），`register_kind(种类, screen_of=, on_front_changed=)`
  登记各自的界面与「正面那一格变了」处理；终端是现成的一种；
- 正面：`front_of(PartKey)` 给正面所对的那一格（`PartKey.front_key()`）；那一格换了方块时，
  生命周期发 `ChangeKind.PART_FRONT_CHANGED`，启动层交给 `front_changed`，按种类分派；
- 配置：`part_config(PartKey)` / `set_part_config(PartKey, 字典)`，存在宿主方块实体里按面
  一份（`endernet:part_config_<面>`），挂上新面板时清空、原地换方块时随面板搬走。

**外观**：`display_values(node_key)` 给 `SlotDisplayTracker` 当面板读数（六面取值，顺序
`FACING_NAMES`，种类序号同 `cable_shapes.PANEL_KINDS`）。
"""

from ...common.cable_shapes import PANEL_PART_IDS, PART_PANEL
from ...common.channels import PANEL, kind_of, model_value
from ...common.channels.cable_intent import (
    ACTION_ADD_CABLE,
    ACTION_PLACE_PANEL,
    CABLE_IDS,
    HOLDER_IDS,
    PANEL_HOST_IDS,
    PANEL_ITEM_IDS,
)
from ...common.definitions import PART_HOLDER_ID, PART_KIND_TERMINAL
from ...common.identity import FACING_NAMES, DeviceRef, PartKey, opposite_face
from ...common.resources import ResourceKey
from ..interaction import ItemExchange, OpenTarget, PlaceFeedback
from .cable_intents import REASON_WRITE_FAILED, IntentResult
from .cables import is_pending
from .catalog import CableFaces

#: 处理结果（诊断按它计数；玩家看到的是 `IntentResult.message`）
REASON_PANEL_PLACED = "panel_placed"
REASON_CABLE_ADDED = "cable_added"
REASON_CANNOT_HOLD_PANELS = "cannot_hold_panels"
REASON_NO_ROOM = "no_room"
REASON_ITEM_GONE = "item_gone"
REASON_NOT_A_HOLDER = "not_a_holder"

#: 新建面板底座时补进事件队列的放置事件的来源
SOURCE_PANEL = "panel_placement"

#: 空气（相邻格是它就新建面板底座；别的方块再问世界 `may_place` 可不可替换）
AIR = "minecraft:air"

#: 服务端再拦一道原版物品使用的规则名（`UseGuard`）：手持光缆点面板底座
GUARD_ADD_CABLE = "add_cable"
#: 同上：手持终端面板（物品形态是同名的展示方块，2026-11-10 方案 B），对任何方块都放不下
GUARD_PANEL_ITEM = "panel_item"
#: 右键开界面的入口名（`Openers`）：面板
OPENER_PANEL = "panel"


def part_ref(record, face):
    # type: (object, str) -> DeviceRef
    """宿主记录 + 面名 → 这块面板的实例引用。"""
    return DeviceRef(record.node_key, record.generation, face)


class PartKindHooks(object):
    """一种面板的流程（面板骨架）：界面（右键点中它开什么）与正面那一格变了。都可缺。

    - `screen_of(record, face, part_id) -> OpenTarget`（`server/interaction/opening.py`）；
      缺了 = 这种面板没有界面：点中它照样只认领、不推屏，原版交互一样取消；
    - `on_front_changed(part_key, part_id, front_key) -> None`。
    """

    __slots__ = ("screen_of", "on_front_changed")

    def __init__(self, screen_of=None, on_front_changed=None):
        self.screen_of = screen_of
        self.on_front_changed = on_front_changed


class PanelService(object):
    """面板的挂、开、底座换光缆与外观读数。可以脱离引擎构造：世界与玩家都经注入的对象，推屏
    由右键开界面的统一入口做（`claim_open` 只回答开什么）。

    - `keeper`：`CableStateKeeper`（`write_faces` 写按面数据）；
    - `world`：`block_name_at`、`set_block` / `write_cable_entity_fields` / `remove_block`（新建
      面板底座）、`may_place`（可替换方块，可缺：缺了只认空气）、`renew_terminal_inventory_id` /
      `write_part_config` / `part_config`（可缺）、`drop_items_at`；
    - `players`：`is_creative` / `take_held_item`（适配器的 `CableIntentPort`）；
    - `items`：扣 / 退物品（`interaction.ItemExchange`，不给就用 `players` 与 `world` 现建一个）；
    - `feedback`：放成功后的放置音效（`interaction.PlaceFeedback`，不给就只计数）。
    """

    def __init__(self, definitions, registry, catalog, keeper, world, players, items=None,
                 feedback=None):
        self.definitions = definitions
        self.registry = registry
        self.catalog = catalog
        self.keeper = keeper
        self.world = world
        self.players = players
        self.items = items if items is not None else ItemExchange(players, world)
        self.feedback = feedback if feedback is not None else PlaceFeedback()
        #: 意图服务（`register` 时记下）：原地换方块、补放置事件、求交都用它
        self.intents = None
        #: 面板种类 → `PartKindHooks`
        self._kinds = {}
        self.register_kind(PART_KIND_TERMINAL, screen_of=self._terminal_screen)
        self.stats = dict.fromkeys(
            (
                "placed",
                "holders_created",
                "cables_added",
                "identity_failures",
                "config_failures",
                "dropped",
                "drop_failures",
                "front_changes",
                "front_handler_errors",
            ),
            0,
        )

    # -- 登记 ------------------------------------------------------------

    def register(self, intents, guard=None, openers=None):
        # type: (object, object, object) -> None
        """把「挂面板」「底座换光缆」登记进意图的分派表（`CableIntentService.register`），
        放不上就静默不放（`silent`）。

        - 给了 `openers`（`interaction.Openers`，挂在事件适配器上）：登记 `claim_open`——面板与
          整方块终端走同一个右键开界面入口；
        - 给了 `guard`（`interaction.UseGuard`，同上）：再登记两条服务端拦截——手持光缆（不潜行）
          点面板底座（与客户端 `action_for` 的 `add_cable` 同一条件，以服务端为准）；手持终端面板
          对任何方块（物品形态是展示方块，放不下）。
        """
        self.intents = intents
        if openers is not None:
            openers.add(OPENER_PANEL, self.claim_open)
        if guard is not None:
            guard.forbid(GUARD_ADD_CABLE, HOLDER_IDS, CABLE_IDS, unless_sneaking=True)
            guard.forbid(GUARD_PANEL_ITEM, items=PANEL_ITEM_IDS, unless_sneaking=False)
        intents.register(
            ACTION_PLACE_PANEL, self.place, self.is_panel_item, "要手持一块终端面板",
            any_block=True, needs_hit=False, silent=True,
        )
        intents.register(
            ACTION_ADD_CABLE, self.add_cable, self.definitions.is_cable, "",
            needs_hit=False, silent=True,
        )

    def register_kind(self, kind, screen_of=None, on_front_changed=None):
        # type: (str, object, object) -> None
        """登记一种面板的流程（面板骨架）。同名再登记就覆盖。"""
        self._kinds[kind] = PartKindHooks(screen_of=screen_of, on_front_changed=on_front_changed)

    def hooks_of(self, part_id):
        # type: (str) -> object
        return self._kinds.get(self.definitions.part_kind_of(part_id))

    def is_panel_item(self, item_id):
        # type: (str) -> bool
        return bool(item_id) and self.definitions.is_part(item_id)

    # -- 挂 --------------------------------------------------------------

    def place(self, context):
        # type: (object) -> IntentResult
        part_id = context.item_id
        target = self.placement_for(context)
        if target is None:
            return IntentResult(False, REASON_NO_ROOM)
        node_key, face, host = target
        withdrawal = self.items.withdraw(context.player_id, part_id)
        if withdrawal is None:
            return IntentResult(False, REASON_ITEM_GONE)
        if host is None:
            ok = self._create_holder(node_key, face, part_id)
        else:
            ok = self._attach(host, face, part_id)
        if not ok:
            self.items.refund(withdrawal, context.dimension, context.feet())
            return IntentResult(False, REASON_WRITE_FAILED)
        self.stats["placed"] += 1
        self.feedback.placed(context.player_id, node_key)
        return IntentResult(True, REASON_PANEL_PLACED)

    def placement_for(self, context):
        # type: (object) -> object
        """照 AE2 定这块面板放在哪：`(格子, 面, 宿主记录)`；宿主为 None 表示新建面板底座。
        放不了返回 None。规则见模块说明的表。"""
        record = context.record
        if record is not None and self.definitions.has_face_data(record.device_id):
            hit = context.hit
            if (
                hit is not None
                and hit.part != PART_PANEL
                and self.definitions.accepts_parts(record.device_id)
                and self.catalog.part_at(record, hit.face) is None
            ):
                return record.node_key, hit.face, record
        if context.face is None or context.node_key is None:
            return None
        # 像火把：被点格 + 被点击的面；面板贴在被点的方块上，正面朝着它
        node_key = context.node_key.neighbor(context.face)
        face = opposite_face(context.face)
        neighbor = self.registry.get(node_key)
        if neighbor is not None:
            if (
                self.definitions.accepts_parts(neighbor.device_id)
                and neighbor.loaded
                and not is_pending(neighbor)
                and self.catalog.part_at(neighbor, face) is None
            ):
                return node_key, face, neighbor
            return None
        if self._replaceable(node_key, context.face):
            return node_key, face, None
        return None

    def _replaceable(self, node_key, clicked_face):
        # type: (object, str) -> bool
        """这一格能不能新建面板底座：空气直接能；别的方块问世界 `may_place`（引擎 `MayPlace`：
        草、水、雪层这类可替换方块为真）。世界没有这个口子、或问不到，就只认空气。"""
        block = self._block_at(node_key)
        if block == AIR:
            return True
        if block is None:
            return False
        asker = getattr(self.world, "may_place", None)
        if asker is None:
            return False
        try:
            return asker(node_key, PART_HOLDER_ID, clicked_face) is True
        except Exception:
            return False

    def _attach(self, host, face, part_id):
        # type: (object, str, str) -> bool
        """挂到已有的宿主上：换这一面的库存标记、清配置，再写按面数据。"""
        parts = dict(CableFaces.from_record(host).parts)
        parts[face] = part_id
        if not self._fresh_face(PartKey(host.node_key, face)):
            return False
        return self.keeper.write_faces(host.node_key, parts=parts)

    def _create_holder(self, node_key, face, part_id):
        # type: (object, str, str) -> bool
        """空气格（或可替换方块）新建一个面板底座并挂上这一块：放方块 → 写面板表 → 换库存标记、清配置 →
        补一条放置事件（生命周期照常注册，注册时从方块实体读出面板）。中途失败就把方块拆回空气。
        """
        setter = getattr(self.world, "set_block", None)
        writer = getattr(self.world, "write_cable_entity_fields", None)
        if setter is None or writer is None:
            return False
        try:
            if not setter(node_key, PART_HOLDER_ID):
                return False
        except Exception:
            return False
        try:
            written = bool(writer(node_key, 0, {face: part_id}))
        except Exception:
            written = False
        if not written or not self._fresh_face(PartKey(node_key, face)):
            remover = getattr(self.world, "remove_block", None)
            if remover is not None:
                try:
                    remover(node_key)
                except Exception:
                    pass
            return False
        self.stats["holders_created"] += 1
        if self.intents is not None:
            self.intents.queue_place(node_key, PART_HOLDER_ID, SOURCE_PANEL)
        return True

    def _fresh_face(self, part_key):
        # type: (PartKey) -> bool
        """这一面要挂一块新面板：换一个新的终端库存实例标记、清空面板配置（世界没有这两个
        口子就跳过）。同一面拆下再挂一块，不能接着用上一块的库存与配置
        （见 `persistence/terminals.py`）。"""
        renew = getattr(self.world, "renew_terminal_inventory_id", None)
        if renew is not None:
            try:
                ok = bool(renew(part_key))
            except Exception:
                ok = False
            if not ok:
                self.stats["identity_failures"] += 1
                return False
        writer = getattr(self.world, "write_part_config", None)
        if writer is not None:
            try:
                ok = bool(writer(part_key, {}))
            except Exception:
                ok = False
            if not ok:
                self.stats["config_failures"] += 1
                return False
        return True

    def _block_at(self, node_key):
        # type: (object) -> object
        reader = getattr(self.world, "block_name_at", None)
        if reader is None:
            return None
        try:
            return reader(node_key)
        except Exception:
            return None

    # -- 底座换光缆 --------------------------------------------------------

    def add_cable(self, context):
        # type: (object) -> IntentResult
        """手持光缆点面板底座：换成那种光缆（面板与库存标记原样保留），扣一个物品。"""
        record = context.record
        cable_id = context.item_id
        if not self.definitions.is_part_holder(record.device_id):
            return IntentResult(False, REASON_NOT_A_HOLDER)
        if not self.definitions.accepts_parts(cable_id):
            # 致密光缆挂不了面板：不放
            return IntentResult(False, REASON_CANNOT_HOLD_PANELS)
        withdrawal = self.items.withdraw(context.player_id, cable_id)
        if withdrawal is None:
            return IntentResult(False, REASON_ITEM_GONE)
        if self.intents is None or not self.intents.convert(record, cable_id):
            self.items.refund(withdrawal, context.dimension, context.feet())
            return IntentResult(False, REASON_WRITE_FAILED)
        self.stats["cables_added"] += 1
        self.feedback.placed(context.player_id, record.node_key)
        return IntentResult(True, REASON_CABLE_ADDED)

    # -- 开 --------------------------------------------------------------

    def claim_open(self, use):
        # type: (object) -> object
        """右键开界面的面板一项（`server/interaction/opening.py` 的 `Openers`）：这一下（`BlockUse`）
        点中的是不是一块面板。

        只看能挂面板的方块（玻璃、普通光缆、面板底座）。射线是玩家眼睛 → 引擎给的命中点，与该格
        **当前的方块状态**求交，与意图同一个求交（`CableIntentService.pick_at`）。点中面板 → 它那
        一种的 `screen_of`（终端：`_terminal_screen`）；这种面板没有界面 → 认领、打不开。没点中
        面板、这格还没加载好、射线读不到 → 不认领，原版照常（贴着光缆放方块之类）。
        """
        if use.block not in PANEL_HOST_IDS or self.intents is None:
            return None
        record = self.registry.get(use.node_key())
        if record is None or record.device_id != use.block:
            return None
        if not record.loaded or is_pending(record):
            return None
        ray = use.ray()
        if ray is None:
            return None
        origin, direction = ray
        local = tuple(origin[axis] - use.pos[axis] for axis in range(3))
        hit, _entered = self.intents.pick_at(record, local, direction)
        if hit is None or hit.part != PART_PANEL:
            return None
        part_id = self.catalog.part_at(record, hit.face)
        if part_id is None:
            return None
        hooks = self.hooks_of(part_id)
        if hooks is None or hooks.screen_of is None:
            return OpenTarget()
        return hooks.screen_of(record, hit.face, part_id)

    def _terminal_screen(self, record, face, part_id):
        # type: (object, str, str) -> OpenTarget
        """终端面板：对应整方块终端的屏，以面板引用开（参数与整方块终端同形）。"""
        ui_key = self.definitions.terminal_screen_of(part_id)
        if ui_key is None:
            return OpenTarget()
        return OpenTarget(ui_key, {
            "device_ref": part_ref(record, face).to_dict(),
            "dimension": record.node_key.dimension,
        })

    # -- 外观 ------------------------------------------------------------

    def display_values(self, node_key):
        # type: (object) -> object
        """这一格六面的外观取值（`variable.p0`～`p5`）；不是显示面板的方块返回 None。"""
        record = self.registry.get(node_key)
        if record is None or kind_of(record.device_id) != PANEL:
            return None
        parts = dict(self.catalog.parts_of(record))
        return tuple(model_value(PANEL_PART_IDS, parts.get(face)) for face in FACING_NAMES)

    # -- 宿主上的面板 ----------------------------------------------------

    def ref_at(self, record, face):
        # type: (object, str) -> object
        """这一格 `face` 面**现在**那块面板的实例引用；那一面没有面板返回 None。"""
        if self.catalog.part_at(record, face) is None:
            return None
        return part_ref(record, face)

    def panels_of(self, record):
        # type: (object) -> list
        """这一格挂着的面板：`[(面板引用, 面板标识)]`，按 `FACING_NAMES` 顺序。"""
        return [(part_ref(record, face), part_id) for face, part_id in self.catalog.parts_of(record)]

    def drop_if_orphaned(self, change):
        # type: (object) -> bool
        """一条 `PART_DETACHED`：宿主已经不在（挖掉、炸掉）就把面板物品掉在宿主那一格。

        扳手拆的那几条路宿主要么还在、要么物品已经给了玩家（拆底座最后一块：面板先摘掉、
        补进来的移除事件不再发离开），这里什么也不做。返回是否掉了。
        """
        host = self.registry.get(change.node_key)
        if host is not None and change.ref is not None and host.ref.matches(change.ref):
            return False
        node_key = change.node_key
        at = (node_key.dimension, node_key.x, node_key.y, node_key.z)
        dropper = getattr(self.world, "drop_items_at", None)
        dropped = 0
        if dropper is not None:
            try:
                dropped = int(dropper(at, [(ResourceKey.item(change.device_id), 1)]) or 0)
            except Exception:
                dropped = 0
        if dropped:
            self.stats["dropped"] += 1
            return True
        self.stats["drop_failures"] += 1
        return False

    # -- 面板骨架：正面与配置 ----------------------------------------------

    @staticmethod
    def front_of(part_key):
        # type: (PartKey) -> object
        """面板正面所对的那一格（`NodeKey`）；center 槽返回 None。"""
        return PartKey.of(part_key).front_key()

    def front_changed(self, change):
        # type: (object) -> bool
        """`ChangeKind.PART_FRONT_CHANGED`：交给这一种面板的 `front_changed`（没登记就只计数）。
        处理函数抛的异常在这里接住并计数。返回是否交出去了。"""
        self.stats["front_changes"] += 1
        hooks = self.hooks_of(change.device_id)
        if hooks is None or hooks.on_front_changed is None:
            return False
        part_key = PartKey(change.node_key, change.detail)
        try:
            hooks.on_front_changed(part_key, change.device_id, part_key.front_key())
        except Exception:
            self.stats["front_handler_errors"] += 1
            return False
        return True

    def part_config(self, part_key):
        # type: (PartKey) -> object
        """这块面板自己的配置（字典）；读不到（方块实体拿不到、世界没有这个口子）返回 None。"""
        reader = getattr(self.world, "part_config", None)
        if reader is None:
            return None
        try:
            return reader(part_key)
        except Exception:
            return None

    def set_part_config(self, part_key, data):
        # type: (PartKey, dict) -> bool
        """整张写这块面板的配置。那一面没有面板、或写不进去返回 False。"""
        part_key = PartKey.of(part_key)
        record = self.registry.get(part_key.node_key)
        if record is None or part_key.is_center() or self.catalog.part_at(record, part_key.slot) is None:
            return False
        writer = getattr(self.world, "write_part_config", None)
        if writer is None:
            return False
        try:
            return bool(writer(part_key, dict(data)))
        except Exception:
            return False

    def diagnostics(self):
        # type: () -> dict
        return dict(self.stats)
