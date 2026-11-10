# coding=utf-8
"""光缆与面板的精确交互：扳手切断、拆面板、拆光缆（`.claude/13` §6；2026-11-10 照 AE2 改拆卸）。

客户端只提交意图（`common/channels/cable_intent.py`：被点的方块与那一面、动作、一条射线），
这里：

1. **校验**：发件人、动作登记过没有、同一玩家同一个服务端 tick 只处理第一条（轻量防刷，
   多出来的静默丢弃；没有秒级冷却——作者 2026-11-10 要求去掉原来照蔚蓝的 0.5 秒）、
   被点的方块与客户端看到的一致（注册表里有记录就对记录，否则问世界）、按面的方块
   （光缆、面板底座）的方块实体读到了没有、玩家够不够得着（与终端会话同一条距离口径，
   由装配层注入）、手持物品合不合这个动作。扳手类动作只认按面的方块；挂面板
   （`any_block`）对任何方块都收；
2. **求交**：按面的方块用这格**当前的方块状态**（`CableStateKeeper.current_sides`）与射线
   求交（`cable_shapes.pick`），判出点中的是线芯、哪一面的臂还是哪一面的面板；被点击的那一面
   优先取客户端事件带的，没带就按外包盒求（`cable_shapes.hull_entry`）。客户端拿不到方块
   状态，所以在这边做；求交函数两端是同一份；
3. **分派**：「动作 → 处理函数」表（`register`）。登记成 `silent` 的动作（挂面板、底座换
   光缆）不弹任何提示：放不上就静默不放（作者 2026-11-10）。

## 扳手（13 §6；拆卸 2026-11-10 照 AE2 `disassembleWithWrench`）

- `cut`（不潜行）：点中臂 → 那一面；点中线芯 → 射线进入线芯的那一面（「线芯朝向某面」）。
  切换该面的切断标记，对面也是光缆就对称写。点中面板不动，提示潜行可拆（面板的界面由
  不拿工具时的右键打开，`server/interaction/opening.py`）。
  这一面没有能连上的设备时不切（否则留下一个看不见的切断标记）。点的是面板底座（没有线芯、
  不连任何东西）：什么都不做，**静默**（作者 2026-11-10 确认按推荐）。
- `dismantle`（潜行）：
  - 点中面板 → 只拆这一块，物品进背包；是面板底座的最后一块，底座连同消失；
  - 点中线芯或臂 → **只拆光缆本身**，物品进背包：挂着面板就原地换成面板底座（面板、
    库存标记、会话都不动，`ChangeKind.HOST_CONVERTED`），没有面板就整格移除。
  背包满了掉在玩家脚下（物品进出走伪原版交互那一层的 `server/interaction.ItemExchange`）。
  挖掘照旧：整格连同全部面板一起掉。

## 给面板与之后的口子

- **加动作**：`service.register(动作, 处理函数, 物品要求, 物品不对时的提示, any_block=,
  needs_hit=, silent=)`。处理函数收 `IntentContext`（玩家、意图、被点那一格的记录、命中的
  部件、被点击的面、维度、位置、手持物品），返回 `IntentResult`；客户端在
  `common/channels/cable_intent.action_for` 加对应分支。
- **原地换方块**：`service.convert(宿主记录, 新方块)`（底座 ↔ 光缆，`panels.py` 的
  「先面板后光缆」也走它）。
- **面板离开**：拆单块与拆整格都经 `DeviceLifecycle.detach_part` 发一条
  `ChangeKind.PART_DETACHED`（宿主位置、面、面板标识），启动层在 `ServerBootstrap._emit` 里接它
  清会话与存档。发出时方块实体与记录里已经没有这块面板了。
"""

from ...common.cable_shapes import PART_PANEL, core_px_of, hull_entry, pick
from ...common.channels.cable_intent import (
    ACTION_CUT,
    ACTION_DISMANTLE,
    WRENCH_ID,
    parse_intent,
)
from ...common.definitions import PART_HOLDER_ID
from ...common.identity import FACING_NAMES, NodeKey
from ..interaction import ItemExchange
from ..runtime import NormalizedEvent
from .cables import is_pending
from .catalog import CableFaces

#: 面名 → 中文（提示用）
FACE_LABELS = {
    "up": "上",
    "down": "下",
    "north": "北",
    "south": "南",
    "west": "西",
    "east": "东",
}

#: 扳手拆整格时补进事件队列的那条移除事件的来源
SOURCE_WRENCH = "wrench"

#: 处理结果（诊断按它计数；玩家看到的是 `IntentResult.message`）
REASON_CUT = "cut"
REASON_JOINED = "joined"
REASON_PANEL_REMOVED = "panel_removed"
REASON_CABLE_REMOVED = "cable_removed"
REASON_BAD_REQUEST = "bad_request"
REASON_UNKNOWN_ACTION = "unknown_action"
#: 同一玩家同一个服务端 tick 里的第二条及以后（静默丢弃）
REASON_SAME_TICK = "same_tick"
REASON_NO_PLAYER = "no_player"
REASON_NOT_A_CABLE = "not_a_cable"
REASON_BLOCK_MISMATCH = "block_mismatch"
REASON_NOT_READY = "not_ready"
REASON_TOO_FAR = "too_far"
REASON_WRONG_ITEM = "wrong_item"
REASON_MISSED = "missed"
REASON_PANEL_TARGETED = "panel_targeted"
REASON_NOTHING_TO_CUT = "nothing_to_cut"
#: 扳手切断点的是面板底座（静默）
REASON_HOLDER_HAS_NO_LINKS = "holder_has_no_links"
REASON_NO_PANEL = "no_panel"
REASON_WRITE_FAILED = "write_failed"
REASON_REMOVE_FAILED = "remove_failed"


class IntentResult(object):
    """一条意图的处理结果。`message` 非空时由适配器弹给发件人看。"""

    __slots__ = ("ok", "reason", "message")

    def __init__(self, ok, reason, message=""):
        # type: (bool, str, str) -> None
        self.ok = bool(ok)
        self.reason = reason
        self.message = message

    def to_dict(self):
        # type: () -> dict
        return {"ok": self.ok, "reason": self.reason, "message": self.message}

    def __repr__(self):
        return "IntentResult(%s, %s)" % (self.ok, self.reason)


class IntentContext(object):
    """处理函数收到的全部事实：都已由服务端自己查过、校验过。"""

    __slots__ = (
        "player_id",
        "intent",
        "record",
        "hit",
        "dimension",
        "position",
        "item_id",
        "node_key",
        "face",
        "block",
    )

    def __init__(self, player_id, intent, record, hit, dimension, position, item_id,
                 node_key=None, face=None, block=None):
        self.player_id = player_id
        #: `cable_intent.CableIntent`
        self.intent = intent
        #: 被点那一格的设备记录：光缆或面板底座（宿主）；挂面板点普通方块时是那台设备的
        #: 记录，点的不是本模组方块则为 None
        self.record = record
        #: `cable_shapes.CableHit`：点中的部件与面；没点中（或点的不是按面的方块）为 None
        self.hit = hit
        self.dimension = dimension
        #: 玩家脚下坐标（引擎给的，不是客户端自报的）
        self.position = position
        #: 玩家主手物品标识（服务端读的；空手为 ""）
        self.item_id = item_id
        #: 被点的那一格
        self.node_key = node_key
        #: 被点击的那一面（面名）：客户端事件带的，没带就按外包盒求；求不出为 None
        self.face = face
        #: 被点的方块标识（已与客户端看到的核对过）
        self.block = block

    def feet(self):
        # type: () -> tuple
        """给玩家的东西背包放不下时掉在哪：玩家脚下；读不到玩家位置就掉在被点的那一格。"""
        return self.position or self.intent.pos


class _Action(object):
    """分派表的一项。"""

    __slots__ = ("handler", "accepts_item", "item_hint", "any_block", "needs_hit", "silent")

    def __init__(self, handler, accepts_item, item_hint, any_block, needs_hit, silent):
        self.handler = handler
        self.accepts_item = accepts_item
        self.item_hint = item_hint
        #: 被点的可以是任何方块（挂面板）；否则只认光缆与面板底座
        self.any_block = any_block
        #: 没点中任何部件就拒绝（「没有点中光缆」）
        self.needs_hit = needs_hit
        #: 不弹提示：结果照样计数，`message` 一律清空
        self.silent = silent


class CableIntentService(object):
    """意图的校验与分派。可以脱离引擎构造：玩家与世界的读写都经注入的对象。

    - `keeper`：`CableStateKeeper`（按面数据的读写、当前方块状态、原地换方块）；
    - `world`：`remove_block`，挂面板还要 `block_name_at`；
    - `players`：`dimension_of` / `position_of` / `held_item_of`（适配器的 `CableIntentPort`）；
    - `items`：物品进出（`interaction.ItemExchange`，不给就用 `players` 与 `world` 现建一个）；
    - `lifecycle`：`detach_part`（面板离开）、`convert`（原地换方块）；
    - `reach(方块中心, 玩家位置) -> bool`：距离口径，装配层注入终端会话那一条；
    - `push_event`：把扳手拆整格的移除事件、新建面板底座的放置事件补进事件队列
      （`ServerBootstrap.push`）。
    """

    def __init__(
        self,
        definitions,
        registry,
        catalog,
        keeper,
        world,
        players,
        lifecycle,
        reach,
        push_event=None,
        clock=None,
        items=None,
    ):
        self.definitions = definitions
        self.registry = registry
        self.catalog = catalog
        self.keeper = keeper
        self.world = world
        self.players = players
        self.lifecycle = lifecycle
        self.reach = reach
        self.push_event = push_event
        self.clock = clock
        #: 物品进出：拆下来的进背包，满了掉在脚下
        self.items = items if items is not None else ItemExchange(players, world)
        #: 玩家 → 最近处理过一条意图的服务端 tick（同一 tick 只处理第一条）
        self._handled_tick = {}
        #: 动作 → `_Action`
        self._actions = {}
        self.stats = {}
        wrench = definitions.get_tool(WRENCH_ID).label if definitions.has_tool(WRENCH_ID) else WRENCH_ID
        wrench_hint = "要手持%s" % (wrench,)
        # 切断自己判「没点中」：点面板底座要静默，不能先被「没有点中光缆」拦下
        self.register(ACTION_CUT, self._toggle_cut, self.is_wrench, wrench_hint, needs_hit=False)
        self.register(ACTION_DISMANTLE, self._dismantle, self.is_wrench, wrench_hint)

    # -- 分派表 ----------------------------------------------------------

    def register(self, action, handler, accepts_item, item_hint="", any_block=False,
                 needs_hit=True, silent=False):
        # type: (str, object, object, str, bool, bool, bool) -> None
        """登记一个动作：`handler(IntentContext) -> IntentResult`，`accepts_item(物品标识) -> bool`。

        - `any_block`：被点的可以是任何方块（挂面板），否则只认光缆与面板底座；
        - `needs_hit`：没点中任何部件就拒绝（「没有点中光缆」）；为假时 `context.hit` 可能是 None；
        - `silent`：这个动作不弹任何提示（校验不过也一样）。

        同名再登记就覆盖（换实现时不必先摘）。
        """
        self._actions[action] = _Action(
            handler, accepts_item, item_hint, bool(any_block), bool(needs_hit), bool(silent))

    def actions(self):
        # type: () -> list
        return sorted(self._actions)

    @staticmethod
    def is_wrench(item_id):
        # type: (str) -> bool
        return item_id == WRENCH_ID

    # -- 入口 ------------------------------------------------------------

    def handle(self, player_id, payload):
        # type: (str, dict) -> IntentResult
        """一条意图的全流程。**永不抛**：坏包与拒绝都落成 `IntentResult`。"""
        if not player_id:
            return self._done(None, IntentResult(False, REASON_BAD_REQUEST))
        try:
            intent = parse_intent(payload)
        except (KeyError, TypeError, ValueError):
            return self._done(None, IntentResult(False, REASON_BAD_REQUEST))
        spec = self._actions.get(intent.action)
        if spec is None:
            return self._done(None, IntentResult(False, REASON_UNKNOWN_ACTION))
        # 轻量防刷在查世界之前：同一玩家同一个 tick 只处理第一条，多出来的不提示。
        # 「按一次算一次」由客户端做（`client/interaction/press.PressTracker`），
        # 这里不设任何秒级冷却。
        if self.clock is not None:
            tick = self.clock.current_tick()
            if self._handled_tick.get(player_id) == tick:
                return self._done(spec, IntentResult(False, REASON_SAME_TICK))
            self._handled_tick[player_id] = tick
        dimension = self._ask("dimension_of", player_id)
        if dimension is None:
            return self._done(spec, IntentResult(False, REASON_NO_PLAYER))
        node_key = NodeKey(dimension, intent.pos[0], intent.pos[1], intent.pos[2])
        record = self.registry.get(node_key)
        faced = record is not None and self.definitions.has_face_data(record.device_id)
        if not faced and not spec.any_block:
            return self._done(spec, IntentResult(False, REASON_NOT_A_CABLE, "这里没有光缆"))
        block = record.device_id if record is not None else self._block_at(node_key)
        if block != intent.block:
            return self._done(spec, IntentResult(
                False, REASON_BLOCK_MISMATCH, "这一格已经换成别的方块了，请再点一次"))
        if faced and (not record.loaded or is_pending(record)):
            return self._done(spec, IntentResult(
                False, REASON_NOT_READY, "这格光缆还没加载好，请稍后再试"))
        position = self._ask("position_of", player_id)
        anchor = (intent.pos[0] + 0.5, intent.pos[1] + 0.5, intent.pos[2] + 0.5)
        if not self.reach(anchor, position):
            return self._done(spec, IntentResult(False, REASON_TOO_FAR, "离光缆太远了"))
        item_id = self._ask("held_item_of", player_id) or ""
        if not spec.accepts_item(item_id):
            return self._done(spec, IntentResult(False, REASON_WRONG_ITEM, spec.item_hint))
        hit = None
        face = intent.face or None
        if faced:
            hit, entered = self.pick_at(record, intent.local_origin(), intent.direction)
            if face is None:
                face = entered
        if hit is None and spec.needs_hit:
            return self._done(spec, IntentResult(False, REASON_MISSED, "没有点中光缆"))
        context = IntentContext(
            player_id, intent, record, hit, dimension, position, item_id,
            node_key=node_key, face=face, block=block,
        )
        return self._done(spec, spec.handler(context))

    def pick_at(self, record, origin, direction):
        # type: (object, tuple, tuple) -> tuple
        """射线（格内坐标：起点已减去这一格的最小角）与按面的方块**当前的方块状态**求交：
        `(点中的部件 cable_shapes.CableHit 或 None, 射线进外包盒的那一面)`。

        意图与右键开界面（`panels.claim_open`）共用这一个求交。
        """
        core = core_px_of(record.device_id)
        sides = self.keeper.current_sides(record)
        return pick(core, sides, origin, direction), hull_entry(core, sides, origin, direction)

    def _block_at(self, node_key):
        # type: (NodeKey) -> object
        """世界里这一格的方块标识（注册表里没有记录的格子用）；问不到返回 None。"""
        reader = getattr(self.world, "block_name_at", None)
        if reader is None:
            return None
        try:
            return reader(node_key)
        except Exception:
            return None

    def _ask(self, method, player_id):
        # type: (str, str) -> object
        """问一条玩家事实；拿不到（玩家刚离线之类）就是 None，由调用方按拒绝处理。"""
        try:
            return getattr(self.players, method)(player_id)
        except Exception:
            return None

    def _done(self, spec, result):
        # type: (object, IntentResult) -> IntentResult
        """计数；登记成 `silent` 的动作把提示清空（放不上就静默不放）。"""
        self.stats[result.reason] = self.stats.get(result.reason, 0) + 1
        if spec is not None and spec.silent and result.message:
            return IntentResult(result.ok, result.reason)
        return result

    # -- 切断 ------------------------------------------------------------

    def _toggle_cut(self, context):
        # type: (IntentContext) -> IntentResult
        record = context.record
        hit = context.hit
        if self.definitions.is_part_holder(record.device_id):
            # 面板底座没有线芯、不连任何东西：没有可切的，静默（作者 2026-11-10 确认按推荐）
            return IntentResult(False, REASON_HOLDER_HAS_NO_LINKS)
        if hit is None:
            return IntentResult(False, REASON_MISSED, "没有点中光缆")
        face = hit.face
        if hit.part == PART_PANEL or face in dict(self.catalog.parts_of(record)):
            return IntentResult(
                False, REASON_PANEL_TARGETED,
                "潜行使用扳手交互可拆下面板")
        index = FACING_NAMES.index(face)
        neighbor_key = record.node_key.neighbors()[index]
        neighbor = self.registry.get(neighbor_key)
        was_cut = CableFaces.from_record(record).is_cut(face)
        if not was_cut and not self._could_link(record, face, neighbor):
            return IntentResult(
                False, REASON_NOTHING_TO_CUT, "%s面没有能连上的设备" % (FACE_LABELS[face],))
        cut = not was_cut
        if not self.keeper.set_cut(record.node_key, face, cut):
            return IntentResult(False, REASON_WRITE_FAILED, "没有写进去，请再试一次")
        message = "已%s%s面的连接" % ("切断" if cut else "恢复", FACE_LABELS[face])
        if self._is_writable_cable(neighbor):
            # 两格光缆之间的切断标记两边各记一份，对称写（照蔚蓝管道）
            if not self.keeper.set_cut(neighbor_key, FACING_NAMES[index ^ 1], cut):
                message += "（相邻那格没有写进去，请对它再点一次）"
        return IntentResult(True, REASON_CUT if cut else REASON_JOINED, message)

    def _could_link(self, record, face, neighbor):
        # type: (object, str, object) -> bool
        """这一面要是不切断，能不能连上：邻格有设备、分类允许，且对面那一面没挂面板。"""
        if neighbor is None:
            return False
        if not self.catalog.can_connect(record.device_id, neighbor.device_id).allowed:
            return False
        if self.definitions.is_cable(neighbor.device_id):
            opposite = FACING_NAMES[FACING_NAMES.index(face) ^ 1]
            if opposite in dict(self.catalog.parts_of(neighbor)):
                return False
        return True

    def _is_writable_cable(self, record):
        # type: (object) -> bool
        return (
            record is not None
            and self.definitions.is_cable(record.device_id)
            and record.loaded
            and not is_pending(record)
        )

    # -- 拆卸 ------------------------------------------------------------

    def _dismantle(self, context):
        # type: (IntentContext) -> IntentResult
        """照 AE2：点中面板只拆那一块；点中线芯或臂只拆光缆本身（挂着面板就留下面板底座）。"""
        if context.hit.part == PART_PANEL:
            return self._remove_panel(context, context.hit.face)
        if self.catalog.parts_of(context.record):
            return self._strip_cable(context)
        return self._remove_cable(context)

    def _remove_panel(self, context, face):
        # type: (IntentContext, str) -> IntentResult
        """只拆这一块：先从方块实体与记录里去掉，再发「面板离开」，物品给玩家。

        面板底座上的最后一块：底座连同消失（方块换成空气，不掉落；拆除走与挖掉相同的流程，
        面板已先摘掉，不会再掉一遍）。底座拆不掉就把面板放回去。
        """
        record = context.record
        node_key = record.node_key
        part_id = dict(self.catalog.parts_of(record)).get(face)
        if part_id is None:
            return IntentResult(False, REASON_NO_PANEL, "这一面没有面板")
        original = dict(CableFaces.from_record(record).parts)
        remaining = dict(original)
        remaining.pop(face, None)
        if not self.keeper.write_faces(node_key, parts=remaining):
            return IntentResult(False, REASON_WRITE_FAILED, "没有写进去，请再试一次")
        if self.definitions.is_part_holder(record.device_id) and not remaining:
            if not self._remove_block(node_key):
                self.keeper.write_faces(node_key, parts=original)
                return IntentResult(False, REASON_REMOVE_FAILED, "没能拆下这块面板，请再试一次")
            self.queue_remove(node_key, record.device_id)
        self.lifecycle.detach_part(record, face, part_id)
        self.items.hand_over(context.player_id, [part_id], context.dimension, context.feet())
        return IntentResult(
            True, REASON_PANEL_REMOVED, "已拆下%s" % (self.definitions.get(part_id).label,))

    def _strip_cable(self, context):
        # type: (IntentContext) -> IntentResult
        """只拆光缆、面板留下（AE2）：这一格原地换成面板底座，光缆物品给玩家。

        面板、库存标记、会话都不动（同一个实例，`ChangeKind.HOST_CONVERTED`）；面板的状态栏
        随后变成「面板没有接在光缆上」。
        """
        record = context.record
        cable_id = record.device_id
        if not self.convert(record, PART_HOLDER_ID):
            return IntentResult(False, REASON_REMOVE_FAILED, "没能拆下这格光缆，请再试一次")
        self.items.hand_over(context.player_id, [cable_id], context.dimension, context.feet())
        return IntentResult(
            True, REASON_CABLE_REMOVED,
            "已拆下%s（面板留在原处）" % (self.definitions.get(cable_id).label,))

    def _remove_cable(self, context):
        # type: (IntentContext) -> IntentResult
        """拆整格（没有面板的光缆）：方块换成空气（不掉落），拆除走与挖掉相同的流程，物品给玩家。

        挂着面板的走 `_strip_cable`；这里仍防着「记录里有、却不算数」的面板表（先摘掉，
        接下来的拆除流程就看不到它们，不会再给一遍）。
        """
        record = context.record
        node_key = record.node_key
        panels = list(self.catalog.parts_of(record))
        original = dict(CableFaces.from_record(record).parts)
        if original and not self.keeper.write_faces(node_key, parts={}):
            return IntentResult(False, REASON_WRITE_FAILED, "没有写进去，请再试一次")
        if not self._remove_block(node_key):
            if original:
                self.keeper.write_faces(node_key, parts=original)
            return IntentResult(False, REASON_REMOVE_FAILED, "没能拆下这格光缆，请再试一次")
        self.queue_remove(node_key, record.device_id)
        for face, part_id in panels:
            self.lifecycle.detach_part(record, face, part_id)
        self.items.hand_over(context.player_id,
                             [record.device_id] + [part_id for _face, part_id in panels],
                             context.dimension, context.feet())
        message = "已拆下%s" % (self.definitions.get(record.device_id).label,)
        if panels:
            message += "（连同 %d 块面板）" % (len(panels),)
        return IntentResult(True, REASON_CABLE_REMOVED, message)

    def _remove_block(self, node_key):
        # type: (NodeKey) -> bool
        remover = getattr(self.world, "remove_block", None)
        if remover is None:
            return False
        try:
            return bool(remover(node_key))
        except Exception:
            return False

    # -- 给面板用的口子 ----------------------------------------------------

    def convert(self, record, device_id):
        # type: (object, str) -> bool
        """面板宿主原地换方块（面板底座 ↔ 光缆）：世界与方块实体（`keeper.swap_block`，面板与
        按面字段原样搬过去、切断面清零）→ 注册表型号与通知（`lifecycle.convert`，
        `ChangeKind.HOST_CONVERTED`）。世界那一步不成就什么都不改，返回 False。"""
        node_key = record.node_key
        if not self.keeper.swap_block(node_key, device_id, cuts=0):
            return False
        self.lifecycle.convert(node_key, device_id)
        return True

    def queue_remove(self, node_key, device_id):
        # type: (NodeKey, str) -> None
        """把「这一格的方块被拆了」补进事件队列。引擎的移除事件也会来：事件队列把背靠背的
        两条合并成一条，隔开了的那条到注册表按「这里已经没有」忽略——拆除只处理一次。补这一条
        是为了引擎不派发时也拆得掉。"""
        if self.push_event is not None:
            self.push_event(NormalizedEvent.remove(
                node_key, device_id, tick=self._now(), source=SOURCE_WRENCH))

    def queue_place(self, node_key, device_id, source):
        # type: (NodeKey, str, str) -> None
        """把「这一格放下了一个方块」补进事件队列（新建面板底座用）：生命周期照放置事件
        注册它、从方块实体读出面板。引擎的放置事件若也来，注册表按同位置同方块当重复。"""
        if self.push_event is not None:
            self.push_event(NormalizedEvent.place(
                node_key, device_id, tick=self._now(), source=source))

    def _now(self):
        # type: () -> int
        return self.clock.current_tick() if self.clock is not None else 0

    def diagnostics(self):
        # type: () -> dict
        return {"actions": self.actions(), "results": dict(self.stats),
                "items": self.items.diagnostics()}
