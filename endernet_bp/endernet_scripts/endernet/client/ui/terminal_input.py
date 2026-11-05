# coding=utf-8
"""Desktop slot gestures. Collection indices are resolved before commands queue."""
from collections import deque

from . import slot_layout
from .backpack_model import engine_slot_of
from .flying_item import ICON_SIZE
from ...common.resource_key import ResourceKey


def input_api():
    try:
        from ....tooldelta.api.client import ui
        return ui
    except ImportError:
        return None


def cursor_item_rect(point):
    # type: (object) -> object
    """Global bounds of the 16px held item, centered on the mouse hotspot."""
    if point is None:
        return None
    half = ICON_SIZE / 2.0
    return (point[0] - half, point[1] - half, ICON_SIZE, ICON_SIZE)


class TerminalInput(object):
    def __init__(self, screen):
        self.screen = screen
        self.shift = False
        self.space = False
        self.pending = deque()
        self.active = None
        self.drag = None
        self.editing = False
        self.last_quick = None
        self.cursor_count = None
        self.cursor_icon = None
        self.cursor_signature = None
        self.info_target = self.info_point = self.info_label = self.info_text = None
        self.info_time = self.notice_until = 0
        self.desktop = False

    def reset(self):
        self.pending.clear()
        self.active = self.drag = None
        self.shift = self.space = self.editing = False
        self.last_quick = None
        if self.cursor_icon is not None:
            self.cursor_icon.SetVisible(False)
        if self.cursor_count is not None:
            self.cursor_count.SetVisible(False)
        self.cursor_signature = None
        if self.info_label is not None:
            self.info_label.SetVisible(False)
        self.info_text = self.info_point = self.info_target = None
        self.notice_until = 0

    def show_info(self, text):
        if self.info_text == text:
            return
        if self.info_label is None:
            self.info_label = slot_layout.control(self.screen, "/slot_info")
        if self.info_label is not None:
            if text:
                self.info_label.asLabel().SetText(text)
            self.info_label.SetVisible(bool(text))
            self.info_text = text

    def notice(self, result):
        reasons = result.get("reasons") or ()
        if reasons and reasons[0].get("detail"):
            self.show_info(reasons[0]["detail"])
            self.notice_until = self.screen._clock() + 2

    def update_info(self, api, view):
        now = self.screen._clock()
        if now < self.notice_until or now < self.info_time:
            return
        self.info_time = now + 0.12
        if self.screen.top_modal():
            self.show_info("")
            return
        target = None
        if self.desktop:
            point = api.GetFocusPos()
            if point != self.info_point:
                self.info_target = slot_layout.slot_at(self.screen, point) if point else None
                self.info_point = point
            target = self.info_target
        elif self.screen.backpack.selected is not None:
            target = ("bag", self.screen.backpack.selected)
        else:
            key = view.cache.model.selected_key()
            if key is not None:
                target = ("network", view.cache.model.index_of(key))
        name, count = None, 0
        if target is not None:
            area, index = target
            if area == "network" and view.has_item(index):
                row = view.rows[index]
                name, count = row.name, row.stored
            elif area == "bag":
                item = self.screen.backpack.entry(index)
                if item:
                    name, count = item.key.name, item.count()
            elif area in ("grid", "output", "source", "outputs", "input", "result"):
                model = getattr(self.screen, "craft", None) if area in ("grid", "output") else getattr(self.screen, "template", None)
                if model is not None:
                    if area == "output":
                        row = model.preview
                    elif area == "grid":
                        row = model.cell(index)
                    else:
                        row = model.cell(area, index)
                    if row:
                        name, count = row.get("name"), row.get("count", 0)
        if name and isinstance(name, bytes):
            name = name.decode("utf-8")
        if name and ":" in name:
            from .stack_size import display_name
            name = display_name(name)
        self.show_info(u"%s × %d" % (name, count) if name else "")

    def tick(self):
        view = self.screen.list_view
        if view is None or not view.cache.client.has_session():
            if self.active or self.cursor_signature or self.pending or self.drag:
                self.reset()
            return
        api = input_api()
        desktop = api is not None and api.GetToggleMode() == 0
        if desktop != self.desktop:
            if self.desktop:
                view.cache.client.send_command("cursor_return")
            self.reset()
            self.screen.backpack.clear_selection()
            view.cache.model.clear_selection()
            self.desktop = desktop
        if api is not None:
            self.update_info(api, view)
        if not desktop:
            return
        hand = getattr(view.cache.client, "cursor", None)
        if self.drag:
            point = api.GetFocusPos()
            if point != self.drag["point"]:
                self.drag["point"] = point
                self.visit(slot_layout.slot_at(self.screen, point))
        if hand and hand["key"] and not self.screen.top_modal():
            if self.cursor_icon is None:
                self.cursor_icon = slot_layout.control(self.screen, "/cursor_item")
            origin = slot_layout.control(self.screen, "")
            if self.cursor_icon is not None and origin is not None:
                rect = cursor_item_rect(api.GetFocusPos())
                if rect is not None:
                    ox, oy = origin.GetGlobalPosition()
                    left, top = rect[0] - ox, rect[1] - oy
                    self.cursor_icon.SetPosition((left, top))
                    if self.cursor_count is None:
                        self.cursor_count = slot_layout.control(self.screen, "/cursor_count")
                    if self.cursor_count is not None:
                        self.cursor_count.SetPosition((left + 2, top + 9))
                    if self.cursor_signature != hand["revision"]:
                        key = ResourceKey.from_dict(hand["key"])
                        self.cursor_icon.asItemRenderer().SetUiItem(key.name, key.aux or 0)
                        self.cursor_icon.SetVisible(True)
                        if self.cursor_count is not None:
                            self.cursor_count.asLabel().SetText(str(hand["count"]))
                            self.cursor_count.SetVisible(hand["count"] > 1)
                        self.cursor_signature = hand["revision"]
        elif self.cursor_signature is not None:
            self.cursor_icon.SetVisible(False)
            if self.cursor_count is not None:
                self.cursor_count.SetVisible(False)
            self.cursor_signature = None
        self.dispatch()

    def mapped_click(self, button, params):
        if not self.desktop or self.screen.top_modal():
            return
        point = input_api().GetFocusPos()
        target = slot_layout.slot_at(self.screen, point)
        if target:
            area, index = target
            if button == "quick" and area == "bag":
                now = self.screen._clock()
                item = self.screen.backpack.entry(index)
                if self.last_quick and self.last_quick[0] == index and now - self.last_quick[1] <= 0.25:
                    self.quick_region(area, index, self.last_quick[2])
                    self.last_quick = None
                    return
                self.last_quick = (index, now, item.key) if item else None
            self.finish(area, index, button)

    def wheel(self, direction):
        if self.desktop and self.shift and not self.editing and not self.screen.top_modal():
            target = slot_layout.slot_at(self.screen, input_api().GetFocusPos())
            if target and target[0] == "network":
                self.click("network", target[1], "one_in" if direction else "one_out")

    def visit(self, target):
        if target is not None and target[0] in ("bag", "grid", "source", "outputs"):
            if target not in self.drag["slots"]:
                self.drag["slots"].append(target)

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
        self.drag = {"slots": [], "button": button, "point": point, "target": target, "row_key": key,
                     "shift": self.shift, "space": self.space}
        self.visit(target)
        return True

    def finish(self, area, index, button):
        drag, self.drag = self.drag, None
        if not drag or drag["button"] != button:
            self.click(area, index, button)
            return
        self.drag = drag
        self.visit((area, index))
        self.drag = None
        targets = drag["slots"]
        hand = self.screen.list_view.cache.client.cursor
        if len(targets) > 1 and hand and hand["key"]:
            key = ResourceKey.from_dict(hand["key"])
            eligible = []
            for region, slot in targets:
                if region == "bag":
                    item = self.screen.backpack.entry(slot)
                    if item and (item.key != key or item.count() >= self.screen.list_view._stack_limit(key.name)):
                        continue
                elif region == "grid":
                    item = self.screen.craft.cell(slot)
                    if item and (item["canonical"] != key.canonical() or item["count"] >= self.screen.list_view._stack_limit(key.name)):
                        continue
                elif self.screen.template.locked(region, slot):
                    continue
                eligible.append((region, slot))
            remaining = hand["count"]
            physical = min(remaining, sum(region in ("grid", "bag") for region, _ in eligible))
            each = 1 if button == "right" else hand["count"] // max(1, physical)
            for region, slot in eligible:
                if region in ("bag", "grid"):
                    count = min(each, remaining)
                    remaining -= count
                else:
                    count = 1 if button == "right" else hand["count"]
                if count:
                    self.click(region, slot, "place", count)
            return
        if drag["target"] == (area, index):
            if area == "network" and drag["row_key"] is not None:
                view = self.screen.list_view
                if not view.has_item(index) or view.rows[index].key != drag["row_key"]:
                    return
            gesture = "quick" if drag["shift"] and button == "left" else button
            if drag["shift"] and button == "right" and area == "network":
                gesture = "one_out"
            if drag["space"] and button == "left" and area in ("bag", "network"):
                self.quick_region(area, index)
            else:
                self.click(area, index, gesture)

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
        if self.screen.top_modal():
            return True
        if not self.desktop:
            return False
        self.finish(area, index, "left")
        return True

    def click(self, area, index, button, count=None):
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
        elif area != "network":
            model = screen.craft if area == "grid" else screen.template
            fields["slot_version"] = model.revision
            cell = model.cell(index) if area == "grid" else model.cell(area, index)
            if cell and cell.get("name"):
                key = ResourceKey.item(cell["name"], cell.get("aux") or 0)
        fields.update(config=options, count=count)
        self.pending.append((fields, area, index, key))
        self.dispatch()

    def dispatch(self):
        if self.active is not None or not self.pending or self.screen.top_modal():
            return
        client = self.screen.list_view.cache.client
        hand = client.cursor
        if not hand or not client.has_session():
            return
        fields, area, index, key = self.pending.popleft()
        fields["config"]["revision"] = hand["revision"]
        request = client.send_command("slot_click", **fields)
        point = input_api().GetFocusPos()
        self.active = (request, area, index, key, hand, point, fields["config"]["gesture"], fields.get("slot_version"))

    def settle(self, result):
        if self.active is None or self.active[0] != result.get("request_id"):
            return
        _, area, index, key, before, point, gesture, version = self.active
        self.active = None
        payload = result.get("payload") or {}
        client = self.screen.list_view.cache.client
        hand = payload.get("cursor")
        if hand and (not client.cursor or hand["revision"] >= client.cursor["revision"]):
            client.cursor = hand
        if result.get("status") not in ("ok", "partial"):
            self.pending.clear()
            return
        if payload.get("key"):
            key = ResourceKey.from_dict(payload["key"])
        rect = (self.screen.list_view._network_rect(key) if area == "network" and key
                else slot_layout.slot_rect(self.screen, area, index))
        if area == "bag" and "bag_after" in payload:
            after = payload["bag_after"]
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
        if payload.get("putting") and before["key"]:
            self.screen.fly.fly(ResourceKey.from_dict(before["key"]), pointer, rect)
            if payload.get("reverse"):
                self.screen.fly.fly(ResourceKey.from_dict(payload["reverse"]), rect, pointer)
        elif key:
            self.screen.fly.fly(key, rect, pointer)
