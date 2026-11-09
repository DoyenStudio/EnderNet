# coding=utf-8
"""Desktop slot gestures. Collection indices are resolved before commands queue."""
import math
from collections import deque

from ..shared import slot_layout, stack_size
from .backpack_model import INTEGER_TYPES, engine_slot_of
from .hover import ItemDetails, TerminalHover
from .touch_input import TouchGesture
from ....common.protocol import Reason
from ....common.resources import ResourceKey


DOUBLE_CLICK_SECONDS = 0.25
#: 长按判定的门槛（秒）。**这是手感值，只由观感决定**，与 `flying_item` 的
#: `FLY_SECONDS` 同一类。触屏长按背包槽 = 渐进拆堆（User 2026-11-08 定，见
#: `touch_input`；10-01 定的「长按存半槽」随之取消）。
#: **按秒不按帧**：界面的渲染帧率与脚本 tick、模拟 tick 都不是一回事，
#: 按帧数计时会让同一段时长在低帧率下拖长一截。
LONG_PRESS_SECONDS = 0.4
#: 搬格子的命令。它们的失败（格子空了、目标满了、格子刚被别处改过）原版容器一句话都
#: 不说，格子自己会回到真实样子；只有 `LOUD_REASONS` 那种要玩家知道的才提示。
SLOT_ACTIONS = frozenset((
    "slot_click", "slot_batch", "cursor_drop", "cursor_return", "grid_move", "craft_take",
    "inv_put", "inv_take", "inv_split", "inv_merge", "inv_swap", "inv_quick_move",
    "template_cell_put", "template_cell_clear", "template_cell_count",
    "template_input_put", "template_input_take", "template_output_take",
))
LOUD_REASONS = frozenset((Reason.CONTAINER_UNCERTAIN,))
#: 键鼠长按拿取：过长按门槛后，拿取数量从 1 涨到整叠要多久（秒）。手感值，待真机对照原版调。
TAKE_FILL_SECONDS = 1.0
#: 输入模式（键鼠 / 触屏）多久查一次。每次查询都要新建一个 PlayerView 组件。
MODE_CHECK_SECONDS = 0.25
#: 收到回执后背包按帧重读多久。引擎的背包同步比回执晚到，按分频轮询会让格子
#: 在飞行动画落地后还停在旧样子。
BAG_WATCH_SECONDS = 0.5


def input_api():
    try:
        from .....tooldelta.api.client import ui
        return ui
    except ImportError:
        return None


def hold_count(total, held):
    # type: (int, float) -> int
    """键鼠空手按住一格 `held` 秒时拿多少个（原版旧式长按）；不到长按门槛返回 0。"""
    if total <= 0 or held < LONG_PRESS_SECONDS:
        return 0
    ratio = min(1.0, float(held - LONG_PRESS_SECONDS) / TAKE_FILL_SECONDS)
    return max(1, int(math.ceil(total * ratio)))


#: 持物图标照原版 `inventory_selected_icon`：21x21，中心相对鼠标偏 (-1, -2)。
CURSOR_ICON_SIZE = 21
CURSOR_ICON_OFFSET = (-1, -2)


def cursor_item_rect(point):
    # type: (object) -> object
    """Global bounds of the held item icon around the mouse hotspot."""
    if point is None:
        return None
    half = CURSOR_ICON_SIZE / 2.0
    return (point[0] + CURSOR_ICON_OFFSET[0] - half, point[1] + CURSOR_ICON_OFFSET[1] - half,
            CURSOR_ICON_SIZE, CURSOR_ICON_SIZE)


class TerminalInput(object):
    def __init__(self, screen):
        self.screen = screen
        self.shift = False
        self.space = False
        self.ctrl = False
        self.pending = deque()
        self.active = None
        self.drag = None
        self.settling_drag = None
        self.finished_drag_button = None
        self.editing = False
        self.pocket_tap = None
        self.last_quick = None
        self.last_left = None
        self.cursor_count = None
        self.cursor_icon = None
        self.cursor_signature = None
        #: 光标上画着的那一叠（耐久条读它）与长按拿取的进度，供进度条绑定读。
        self.cursor_key = None
        self.take_state = None
        #: 键鼠短按拿起后、回执到之前，光标上先显示的那一叠（原版按下即拿起）。
        self.lifted = None
        self.host_shown = False
        self.desktop = False
        self.mode_due = 0
        self.bag_watch_until = 0
        self.hover = TerminalHover(screen, self)
        self.details = ItemDetails(screen, self.hover)
        self.touch = TouchGesture(screen, LONG_PRESS_SECONDS)

    def reset(self):
        self.pending.clear()
        self.active = self.drag = self.settling_drag = None
        self.finished_drag_button = None
        self.shift = self.space = self.ctrl = self.editing = False
        self.pocket_tap = None
        self.last_quick = None
        self.last_left = None
        self.lifted = None
        for control in (self.cursor_icon, self.cursor_count):
            if control is not None:
                control.SetVisible(False)
        self.cursor_signature = None
        self.cursor_key = self.take_state = None
        self.bag_watch_until = 0
        self.details.reset()
        self.touch.reset()

    def watch_bag(self):
        # type: () -> None
        self.bag_watch_until = self.screen._clock() + BAG_WATCH_SECONDS

    def watching_bag(self):
        # type: () -> bool
        return self.screen._clock() < self.bag_watch_until

    def notice(self, result, action=None):
        # type: (dict, object) -> None
        """失败回执的原因进底部气泡；搬格子的失败不提示（见 `SLOT_ACTIONS`）。"""
        reasons = result.get("reasons") or ()
        if action in SLOT_ACTIONS and not (reasons and reasons[0].get("code") in LOUD_REASONS):
            return
        self.details.notice(result)

    def tick(self):
        # Only suppress duplicate Up callbacks for the release frame.
        self.finished_drag_button = None
        view = self.screen.list_view
        if view is None or not view.cache.client.has_session():
            if self.active or self.cursor_signature or self.pending or self.drag or self.settling_drag:
                self.reset()
            return
        api = input_api()
        desktop = self.desktop if api is not None else False
        now = self.screen._clock()
        if api is not None and now >= self.mode_due:
            self.mode_due = now + MODE_CHECK_SECONDS
            desktop = api.GetToggleMode() == 0
        if desktop != self.desktop:
            if self.desktop:
                view.cache.client.send_command("cursor_return")
            self.reset()
            self.screen.backpack.clear_selection()
            view.cache.model.clear_selection()
            self.desktop = desktop
        if not self.host_shown:
            # 跟随光标的宿主两种输入都开着：隐藏时不跟手，触屏第一次拖动的图标会停在左上角。
            host = slot_layout.control(self.screen, "/cursor_host")
            if host is not None:
                host.SetVisible(True)
                self.host_shown = True
        self.details.tick(*self.selected_target(view))
        if not desktop:
            self.touch.tick()
            self.retire_distribution()
            # 队列两种输入共用：触屏的丢出与滑动平分都是「拿到光标上」再发第二条，要在这里发。
            self.dispatch()
            return
        hand = getattr(view.cache.client, "cursor", None)
        if self.drag and self.screen.top_modal():
            self.end_drag()
        if self.drag:
            point = api.GetFocusPos()
            if point != self.drag["point"]:
                self.drag["point"] = point
                self.visit(slot_layout.slot_at(self.screen, point))
            if self.drag["preview"] and self.drag["revision"] != (hand or {}).get("revision"):
                self.end_drag()
        self.retire_distribution()
        hold = self.drag.get("hold") if self.drag else None
        if hold is not None and len(self.drag["slots"]) > 1:
            # 按住后划到了别的格子：那是拖动，不是长按拿取。
            hold = self.drag["hold"] = None
        if hold is not None:
            hold["count"] = hold_count(hold["total"], now - hold["at"])
        distribution = self.drag or self.settling_drag
        count = distribution["remainder"] if distribution and distribution["preview"] else (hand or {}).get("count", 0)
        if self.screen.top_modal():
            self.show_cursor(None, 0)
        elif hand and hand["key"] and count:
            self.show_cursor(ResourceKey.from_dict(hand["key"]), count, revision=hand["revision"])
        elif hold is not None and hold["count"]:
            # 按够久：物品回到格子里，光标上方出现原版旧式的灰底绿色进度条。
            self.show_cursor(None, 0, take=hold["count"], total=hold["total"])
        elif hold is not None or self.lifted is not None:
            lifted = hold or self.lifted
            self.show_cursor(lifted["key"], lifted["total"])
        else:
            self.show_cursor(None, 0)
        self.dispatch()

    def show_cursor(self, key, count, revision=None, take=0, total=0):
        # type: (object, int, object, int, int) -> None
        """光标上的持物图标与数量，或长按拿取的进度（`take` 非零）。

        两条进度条（耐久、长按拿取）照原版用绑定取数，见 `take_bar` / `durability_bar`；
        图标与数量直接写控件，状态没变就不碰引擎。
        """
        self.cursor_key = key if key is not None and count else None
        self.take_state = (take, total) if take else None
        signature = (revision, key, count)
        if signature == self.cursor_signature:
            return
        control = slot_layout.control
        if self.cursor_icon is None:
            self.cursor_icon = control(self.screen, "/cursor_host/cursor_item")
        if self.cursor_count is None:
            self.cursor_count = control(self.screen, "/cursor_host/cursor_count")
        if self.cursor_icon is None or self.cursor_count is None:
            return
        if key is not None and count:
            self.cursor_icon.asItemRenderer().SetUiItem(key.name, key.aux or 0)
            self.cursor_count.asLabel().SetText(str(count))
        self.cursor_icon.SetVisible(key is not None and count > 0)
        self.cursor_count.SetVisible(key is not None and count > 1)
        self.cursor_signature = signature

    def take_bar(self):
        # type: () -> object
        """键鼠长按拿取的进度 `(当前, 总量)`；没在拿时为 None。"""
        return self.take_state

    def durability_bar(self):
        # type: () -> object
        """光标上那一叠的耐久 `(当前, 总量)`（原版 `durability_bar_grabbed`）；不是受损工具时为 None。"""
        visible, total, current = stack_size.durability(self.cursor_key)
        return (current, total) if visible else None

    def selected_target(self, view):
        # type: (object) -> tuple
        """触屏当前选中的格子与标识这次选中的记号；键鼠没有选中说明，两个都是 None。"""
        if self.desktop:
            return None, None
        backpack = self.screen.backpack
        if backpack.selected is not None:
            entry = backpack.entry(backpack.selected)
            return ("bag", backpack.selected), ("bag", backpack.selected, entry.key if entry else None)
        key = view.cache.model.selected_key()
        if key is None:
            return None, None
        return ("network", view.cache.model.index_of(key)), ("network", key)

    def _hold_target(self, button, target):
        # type: (str, object) -> object
        """键鼠空手左键按住一格：记下长按拿取的物品与整叠数量；不能拿就返回 None。"""
        view = self.screen.list_view
        hand = view.cache.client.cursor or {}
        if button != "left" or self.shift or self.space or hand.get("key") or target is None:
            return None
        area, index = target
        key, total = None, 0
        if area == "bag":
            entry = self.screen.backpack.entry(index)
            if entry is not None:
                key, total = entry.key, entry.count()
        elif area == "network" and view.has_item(index):
            row = view.rows[index]
            key, total = row.key, min(row.stored - row.reserved, view._stack_limit(row.key.name))
        if key is None or total <= 0:
            return None
        return {"key": key, "total": total, "at": self.screen._clock(), "count": 0}

    def mapped_click(self, button, params, target=None):
        if not self.desktop or self.screen.top_modal():
            self.end_drag()
            return
        drag = self.drag
        if drag and drag["button"] == button and self.dragged(drag):
            # 拖动过的话，抬起回到最初按下的那个按钮，参数里的序号不是松手的地方。
            target = slot_layout.slot_at(self.screen, input_api().GetFocusPos())
        elif target is not None and target[0] == "network" and not self.screen.list_view.has_item(target[1]):
            target = ("network", -1)
        if target:
            area, index = target
            if button == "quick" and area == "bag":
                now = self.screen._clock()
                item = self.screen.backpack.entry(index)
                if self.last_quick and self.last_quick[0] == index and now - self.last_quick[1] <= DOUBLE_CLICK_SECONDS:
                    self.quick_region(area, index, self.last_quick[2])
                    self.last_quick = None
                    return
                self.last_quick = (index, now, item.key) if item else None
            self.finish(area, index, button)
        else:
            self.end_drag()

    def wheel(self, direction):
        if self.desktop and self.shift and not self.editing and not self.screen.top_modal():
            target = slot_layout.slot_at(self.screen, input_api().GetFocusPos())
            if target and target[0] == "network":
                self.click("network", target[1], "one_in" if direction else "one_out")

    def visit(self, target):
        if target is not None and target[0] in ("bag", "grid", "source", "outputs"):
            if target not in self.drag["slots"]:
                self.drag["slots"].append(target)
                self.plan_distribution()

    def plan_distribution(self):
        # type: () -> None
        """Rebuild both the rendered totals and the ordered release commands."""
        drag = self.drag
        drag["placements"], drag["preview"] = [], {}
        hand = self.screen.list_view.cache.client.cursor
        source = drag.get("source")
        if len(drag["slots"]) < 2 or self.active or self.pending:
            return
        if source is not None:
            key, total = source["key"], source["count"]
        elif not hand or not hand["key"] or drag["revision"] != hand["revision"]:
            return
        else:
            key, total = ResourceKey.from_dict(hand["key"]), hand["count"]
        limit = self.screen.list_view._stack_limit(key.name)
        eligible = []
        physical = 0
        for area, index in drag["slots"]:
            # 触屏平分：来源是选中的那一格，它自己不算目标；只分到实物格。
            if source is not None and (area not in ("bag", "grid")
                                       or (area == "bag" and index == source["index"])):
                continue
            existing = 0
            if area == "bag":
                entry = self.screen.backpack.entry(index)
                if entry and entry.key != key:
                    continue
                existing = entry.count() if entry else 0
            elif area == "grid":
                cell = self.screen.craft.cell(index)
                if cell and cell["canonical"] != key.canonical():
                    continue
                existing = cell["count"] if cell else 0
            elif self.screen.template.locked(area, index):
                continue
            if area in ("bag", "grid"):
                if existing >= limit:
                    continue
                physical += 1
            eligible.append((area, index, existing))

        remaining = total
        share = 1 if drag["button"] == "right" else max(1, remaining // max(1, physical))
        for area, index, existing in eligible:
            if area in ("bag", "grid"):
                amount = min(share, remaining, limit - existing)
                remaining -= amount
            else:
                # Template cells copy the configuration without consuming the cursor.
                amount = 1 if drag["button"] == "right" else total
            if amount:
                target = (area, index)
                drag["placements"].append((area, index, amount))
                drag["preview"][target] = {"name": key.name, "aux": key.aux,
                                           "count": existing + amount}
        drag["remainder"] = remaining
        if source is not None and remaining < total:
            drag["preview"][("bag", source["index"])] = {
                "name": key.name, "aux": key.aux, "count": source["stack"] - (total - remaining)}

    def begin_spread(self, start):
        # type: (tuple) -> bool
        """触屏：选中之后滑动，沿手指经过的格子平分选中的那一叠（User 2026-11-09）。

        平分的规则与键鼠左键拖动是同一份（`plan_distribution`），差别只在来源：
        键鼠分的是光标上的那一叠，这里分的是选中的那一格（长按拆出的就只分那些）。
        """
        backpack = self.screen.backpack
        hand = self.screen.list_view.cache.client.cursor
        entry = backpack.entry(backpack.selected) if backpack.selected is not None else None
        if (entry is None or not hand or hand["key"] or self.active or self.pending
                or self.screen.top_modal()):
            return False
        count = min(backpack.selected_count or entry.count(), entry.count())
        self.drag = {"slots": [], "button": "left", "point": None, "start_point": None,
                     "target": start, "row_key": None, "shift": False, "space": False,
                     "preview": {}, "placements": [], "revision": hand["revision"], "remainder": 0,
                     "source": {"index": backpack.selected, "key": entry.key, "count": count,
                                "stack": entry.count()}}
        self.visit(start)
        return True

    def finish_spread(self):
        # type: () -> None
        """松手：先把要分出去的数量 `take` 到光标上，再发与键鼠拖动同一条 `slot_batch`。

        只拿正好分出去的那么多，没分完的留在原格；批量没落完时把光标还回背包（`settle`）。
        """
        drag, self.drag = self.drag, None
        if not drag or not drag["placements"]:
            return
        source = drag["source"]
        self.screen.backpack.clear_selection()
        self.settling_drag = drag
        queued = len(self.pending) + (self.active is not None)
        self.click("bag", source["index"], "take", sum(amount for _, _, amount in drag["placements"]))
        if len(self.pending) + (self.active is not None) > queued:
            self.submit_distribution(drag)
        else:
            self.settling_drag = None

    def preview_cell(self, area, index):
        # type: (str, object) -> object
        distribution = self.drag or self.settling_drag
        if distribution and isinstance(index, INTEGER_TYPES) and not isinstance(index, bool):
            return distribution["preview"].get((area, index))
        return None

    def retire_distribution(self):
        # type: () -> None
        # Keep the released layout visible until the batch and device snapshots settle.
        if not self.settling_drag or self.active or self.pending:
            return
        template = getattr(self.screen, "template", None)
        if template and template.revision < self.settling_drag.get("template_revision", -1):
            return
        craft = getattr(self.screen, "craft", None)
        if craft and craft.revision < self.settling_drag.get("grid_revision", -1):
            return
        self.screen.backpack.replace_from_engine()
        self.settling_drag = None

    @staticmethod
    def dragged(drag):
        # type: (dict) -> bool
        """这次按住之后鼠标挪过或划过了别的格子。"""
        return len(drag["slots"]) > 1 or input_api().GetFocusPos() != drag["start_point"]

    def end_drag(self):
        # type: () -> None
        if self.drag:
            self.finished_drag_button = self.drag["button"]
            self.drag = None

    def pointer(self, button, down):
        if not self.desktop or self.screen.top_modal() or self.screen.list_view is None:
            return
        point = input_api().GetFocusPos()
        target = slot_layout.slot_at(self.screen, point)
        if button == "middle":
            if down and target and not self.editing:
                self.click(target[0], target[1], "middle")
            return
        if down:
            self.press(button, target, point)

    def press(self, button, target=None, point=None):
        if not self.desktop or self.screen.top_modal():
            return False
        point = point or input_api().GetFocusPos()
        target = target or slot_layout.slot_at(self.screen, point)
        self.editing = False
        view = self.screen.list_view
        key = view.rows[target[1]].key if target and target[0] == "network" and view.has_item(target[1]) else None
        self.finished_drag_button = None
        self.settling_drag = None
        self.drag = {"slots": [], "button": button, "point": point, "start_point": point,
                     "target": target, "row_key": key,
                     "shift": self.shift, "space": self.space, "preview": {}, "placements": [],
                     "revision": (view.cache.client.cursor or {}).get("revision"), "remainder": 0}
        self.drag["hold"] = self._hold_target(button, target)
        self.visit(target)
        return True

    def finish(self, area, index, button):
        if self.finished_drag_button == button:
            return
        drag = self.drag
        if not drag or drag["button"] != button:
            self.end_drag()
            self.click(area, index, button)
            return
        self.visit((area, index))
        if len(drag["slots"]) > 1:
            # Re-check the current slot snapshots in case another player edited the grid.
            self.plan_distribution()
            self.end_drag()
            if drag["placements"]:
                self.settling_drag = drag
                self.submit_distribution(drag)
            return
        self.drag = None
        if drag["target"] == (area, index):
            if area == "network" and drag["row_key"] is not None:
                view = self.screen.list_view
                if not view.has_item(index) or view.rows[index].key != drag["row_key"]:
                    return
            hold = drag.get("hold")
            if hold is not None and hold["count"]:
                # 拿玩家最后一帧看到的那个数（`tick` 里算的）。
                self.click(area, index, "take", hold["count"])
                return
            gesture = "quick" if drag["shift"] and button == "left" else button
            if drag["shift"] and button == "right" and area == "network":
                gesture = "one_out"
            if drag["space"] and button == "left" and area in ("bag", "network"):
                self.quick_region(area, index)
            else:
                queued = (self.active, len(self.pending))
                self.click(area, index, gesture)
                if hold is not None and (self.active, len(self.pending)) != queued:
                    self.lifted = hold

    def submit_distribution(self, drag):
        self.last_left = None
        client = self.screen.list_view.cache.client
        placements = []
        for area, index, amount in drag["placements"]:
            entry = {"target": area, "cell": index, "count": amount}
            if area == "bag":
                entry["cell"] = engine_slot_of(index)
                row = client.inventory_slots.get(entry["cell"])
                if row is None:
                    self.settling_drag = None
                    return
                entry.update(slot_version=row["slot_version"], fingerprint=row["canonical"])
            else:
                model = self.screen.craft if area == "grid" else self.screen.template
                entry["slot_version"] = model.revision
            placements.append(entry)
        source = drag.get("source")
        # 触屏的批量排在 `take` 后面，持物修订到发出时才知道（见 `dispatch`）。
        fields = {"config": {"revision": None if source else drag["revision"], "placements": placements}}
        self.pending.append((fields, "batch", source["index"] if source else None, None))
        self.dispatch()

    def quick_region(self, area, index, matching=None):
        if area == "bag":
            slots = range(27) if index < 27 else range(27, 36)
            for slot in slots:
                if matching is not None and slot == index:
                    continue
                item = self.screen.backpack.entry(slot)
                if item and (matching is None or item.key == matching):
                    self.click(area, slot, "quick")
        elif self.screen.list_view.has_item(index):
            row = self.screen.list_view.rows[index]
            limit = self.screen.list_view._stack_limit(row.key.name)
            for _ in range(min(36, (max(0, row.stored - row.reserved) + limit - 1) // limit)):
                self.click(area, index, "quick")

    def primary(self, area, index):
        if area != "bag":
            self.pocket_tap = None
        if self.screen.top_modal():
            self.end_drag()
            return True
        if not self.desktop:
            return False
        if self.drag and self.drag["button"] == "left":
            if self.dragged(self.drag):
                target = slot_layout.slot_at(self.screen, input_api().GetFocusPos())
                if target is None:
                    self.end_drag()
                    return True
                area, index = target
        self.finish(area, index, "left")
        return True

    def click(self, area, index, button, count=None):
        previous, self.last_left = self.last_left, None
        screen = self.screen
        view = screen.list_view
        client = view.cache.client
        hand = client.cursor
        if hand is None or not client.has_session():
            return
        row = view.rows[index] if area == "network" and view.has_item(index) else None
        key = row.key if row else None
        if row and row.has_pattern and (button == "middle" or (
                button == "left" and not hand["key"] and row.stored == 0)):
            view.open_quantity(key)
            return
        if area in ("source", "outputs"):
            screen.template.pc_cell(area, index, button, hand, count)
            return
        if button == "middle":
            return
        if area == "output":
            screen.craft.take_desktop(button, self.shift or button == "quick", self.space, hand)
            return
        gesture = button
        if self.shift and button == "left":
            gesture = "quick"
        elif self.shift and button == "right" and area == "network":
            gesture = "one_out"
        fields = {"target": area, "key": key, "cell": index if index >= 0 else None}
        options = {"gesture": gesture}
        if area == "bag":
            slot = engine_slot_of(index)
            row = client.inventory_slots.get(slot)
            if row is None:
                return
            fields.update(slot=slot, slot_version=row["slot_version"])
            options["fingerprint"] = row["canonical"]
            entry = screen.backpack.entry(index)
            key = entry.key if entry else None
            if gesture == "left" and not self.space:
                now = screen._clock()
                if (previous and previous["slot"] == slot and previous["session"] == client.session_id
                        and now - previous["time"] <= DOUBLE_CLICK_SECONDS):
                    fields["key"] = key = previous["key"]
                    options["gesture"] = "collect"
                    fields["slot_version"] = previous["version"]
                    options["fingerprint"] = previous["fingerprint"]
                elif key and key.canonical() == row["canonical"]:
                    self.last_left = {"slot": slot, "time": now, "key": key, "fields": fields, "session": client.session_id,
                                      "version": row["slot_version"], "fingerprint": row["canonical"]}
        elif area != "network":
            model = screen.craft if area == "grid" else screen.template
            fields["slot_version"] = model.revision
            cell = model.cell(index) if area == "grid" else model.cell(area, index)
            if cell and cell.get("name"):
                key = ResourceKey.item(cell["name"], cell.get("aux") or 0)
        fields.update(config=options, count=count)
        self.pending.append((fields, area, index, key))
        self.dispatch()

    def drop(self, button, params=None):
        # type: (str, object) -> None
        """点窗外：左键丢整叠、右键丢一个（原版 `cursor_drop_all` / `cursor_drop_one`）。"""
        api = input_api()
        view = self.screen.list_view
        if not self.desktop and api is not None and view is not None:
            self.drop_selection(params)
            return
        if (api is None or not self.desktop or view is None or self.drag or self.settling_drag
                or self.screen.top_modal()):
            return
        hand = view.cache.client.cursor
        window = slot_layout.window_rect(self.screen)
        if not hand or not hand["key"] or window is None or slot_layout.contains(window, api.GetFocusPos()):
            return
        self.pending.append(({"config": {"count": "one" if button == "right" else "all"}}, "drop", -1, None))
        self.dispatch()

    def drop_selection(self, params):
        # type: (object) -> None
        """触屏点窗外：丢出选中的背包格，长按拆出的就只丢那些（原版 Pocket）。"""
        backpack = self.screen.backpack
        if self.screen.top_modal() or backpack.selected is None or self.touch.source is not None:
            return
        rect = slot_layout.click_rect(params)
        point = rect[:2] if rect else self.touch.finger()
        window = slot_layout.window_rect(self.screen)
        if point is None or window is None or slot_layout.contains(window, point):
            return
        hand = self.screen.list_view.cache.client.cursor
        ui_index = backpack.selected
        take = backpack.pending_take(whole=True)
        if not hand or hand["key"] or take is None:
            return
        backpack.clear_selection()
        self.take_and_drop(ui_index, take[2])

    def drop_hovered(self, whole):
        # type: (bool) -> None
        """键鼠按 Q：丢出鼠标下那一格背包物品的一个，Ctrl+Q 丢整组（原版）。"""
        api = input_api()
        view = self.screen.list_view
        if (api is None or not self.desktop or view is None or self.editing or self.drag
                or self.settling_drag or self.screen.top_modal()):
            return
        target = slot_layout.slot_at(self.screen, api.GetFocusPos())
        hand = view.cache.client.cursor
        if target is None or target[0] != "bag" or not hand or hand["key"]:
            return
        entry = self.screen.backpack.entry(target[1])
        if entry is not None:
            self.take_and_drop(target[1], entry.count() if whole else 1)

    def take_and_drop(self, ui_index, count):
        # type: (int, int) -> None
        """把背包这一格的 `count` 个丢出去：先 `take` 拿到空的光标上，再 `cursor_drop` 抛出。

        两步都是已有的带日志事务，与键鼠持物点窗外丢出走同一条丢出命令。
        拿到了却没丢成时把光标还回背包（见 `settle`）。
        """
        queued = len(self.pending) + (self.active is not None)
        self.click("bag", ui_index, "take", count)
        if len(self.pending) + (self.active is not None) > queued:
            # 队列项的序号记下这一叠原本在哪一格（键鼠持物丢出时是 -1）。
            self.pending.append(({"config": {"count": "all"}}, "drop", ui_index, None))
            self.dispatch()

    def dispatch(self):
        if self.active is not None or not self.pending or self.screen.top_modal():
            return
        client = self.screen.list_view.cache.client
        hand = client.cursor
        if not hand or not client.has_session():
            return
        fields, area, index, key = self.pending.popleft()
        if area == "drop" and not hand["key"]:
            return self.dispatch()
        if area != "batch" or fields["config"]["revision"] is None:
            fields["config"]["revision"] = hand["revision"]
        action = {"batch": "slot_batch", "drop": "cursor_drop"}.get(area, "slot_click")
        request = client.send_command(action, **fields)
        point = input_api().GetFocusPos()
        self.active = (request, area, index, key, hand, point, fields)

    def settle(self, result):
        if self.active is None or self.active[0] != result.get("request_id"):
            return
        _, area, index, key, before, point, sent = self.active
        gesture, version = sent["config"].get("gesture"), sent.get("slot_version")
        self.active = None
        self.lifted = None
        payload = result.get("payload") or {}
        client = self.screen.list_view.cache.client
        hand = payload.get("cursor")
        if hand and (not client.cursor or hand["revision"] >= client.cursor["revision"]):
            client.cursor = hand
        if area == "drop":
            if result.get("status") != "ok":
                self.pending.clear()
                if index >= 0:
                    # 从格子丢出是先拿到光标上再丢；没丢成就还回去，那一叠本来不在手上。
                    client.send_command("cursor_return")
            return
        if area == "batch":
            if result.get("status") != "ok":
                self.pending.clear()
                self.settling_drag = None
                if index is not None:
                    # 触屏平分是先拿到光标上再落；没落完的还回背包，触屏看不见光标。
                    client.send_command("cursor_return")
            elif self.settling_drag:
                for name in ("grid_revision", "template_revision"):
                    if name in payload:
                        self.settling_drag[name] = payload[name]
            anchors = dict((entry["cell"], entry) for entry in sent["config"]["placements"]
                           if entry["target"] == "bag")
            after = dict((entry["slot"], entry) for entry in payload.get("bag_after", ()))
            grid_before = next((entry["slot_version"] for entry in sent["config"]["placements"]
                                if entry["target"] == "grid"), None)
            for fields, region, _, __ in self.pending:
                slot = fields.get("slot")
                if (region == "bag" and slot in after and slot in anchors
                        and fields.get("slot_version") == anchors[slot]["slot_version"]):
                    fields["slot_version"] = after[slot]["version"]
                    fields["config"]["fingerprint"] = after[slot]["canonical"]
                elif region == "grid" and fields.get("slot_version") == grid_before:
                    fields["slot_version"] = payload.get("grid_revision", grid_before)
            self.screen.backpack.replace_from_engine()
            self.retire_distribution()
            return
        if result.get("status") not in ("ok", "partial"):
            # The first click can try to place into a full matching stack. The second
            # click still has a valid source and unchanged cursor/slot anchors.
            reasons = result.get("reasons") or ()
            if (gesture == "left" and area == "bag" and reasons and reasons[0].get("code") == "no_capacity"
                    and ((self.last_left and self.last_left["fields"] is sent)
                         or (self.pending and self.pending[0][0]["config"]["gesture"] == "collect"))):
                return
            self.pending.clear()
            self.settling_drag = None
            self.last_left = None
            return
        if payload.get("key"):
            key = ResourceKey.from_dict(payload["key"])
        rect = (self.screen.list_view._network_rect(key) if area == "network" and key
                else slot_layout.slot_rect(self.screen, area, index))
        if area == "bag" and "bag_after" in payload:
            after = payload["bag_after"]
            if self.last_left and self.last_left["fields"] is sent:
                self.last_left.update(version=after["version"], fingerprint=after["canonical"])
            for fields, region, slot, _ in self.pending:
                if region == "bag" and slot == index and fields.get("slot_version") == version:
                    fields["slot_version"] = after["version"]
                    fields["config"]["fingerprint"] = after["canonical"]
        if gesture == "quick":
            if area == "bag":
                self.screen.fly.fly(key, rect, self.screen.list_view._network_rect(key))
            else:
                for slot in payload.get("bag_slots", ()):
                    self.screen.fly.fly(key, rect, slot_layout.backpack_slot_rect(
                        self.screen, (slot - 9) % 36))
            return
        if area == "grid" and "device_revision" in payload:
            for fields, region, _, __ in self.pending:
                if region == "grid" and fields.get("slot_version") == version:
                    fields["slot_version"] = payload["device_revision"]
        pointer = cursor_item_rect(point)
        if gesture == "collect":
            for slot in payload.get("bag_slots", ()):
                self.screen.fly.fly(key, slot_layout.backpack_slot_rect(self.screen, (slot - 9) % 36), pointer)
            if payload.get("network_moved"):
                self.screen.fly.fly(key, self.screen.list_view._network_rect(key), pointer)
            return
        if payload.get("putting") and before["key"]:
            self.screen.fly.fly(ResourceKey.from_dict(before["key"]), pointer, rect)
            if payload.get("reverse"):
                self.screen.fly.fly(ResourceKey.from_dict(payload["reverse"]), rect, pointer)
        elif key:
            self.screen.fly.fly(key, rect, pointer)
