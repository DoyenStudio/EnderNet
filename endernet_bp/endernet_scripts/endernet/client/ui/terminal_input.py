# coding=utf-8
"""Desktop slot gestures. Collection indices are resolved before commands queue."""
from collections import deque

from . import slot_layout
from .backpack_model import INTEGER_TYPES, engine_slot_of
from .flying_item import ICON_SIZE
from ...common.resource_key import ResourceKey


DOUBLE_CLICK_SECONDS = 0.25


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
        self.settling_drag = None
        self.finished_drag_button = None
        self.editing = False
        self.pocket_tap = None
        self.last_quick = None
        self.last_left = None
        self.cursor_count = None
        self.cursor_icon = None
        self.cursor_signature = None
        self.info_target = self.info_point = self.info_label = self.info_text = None
        self.info_time = self.notice_until = 0
        self.desktop = False

    def reset(self):
        self.pending.clear()
        self.active = self.drag = self.settling_drag = None
        self.finished_drag_button = None
        self.shift = self.space = self.editing = False
        self.pocket_tap = None
        self.last_quick = None
        self.last_left = None
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
            preview = self.preview_cell(area, index)
            if preview:
                name, count = preview["name"], preview["count"]
            elif area == "network" and view.has_item(index):
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
        # Only suppress duplicate Up callbacks for the release frame.
        self.finished_drag_button = None
        view = self.screen.list_view
        if view is None or not view.cache.client.has_session():
            if self.active or self.cursor_signature or self.pending or self.drag or self.settling_drag:
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
            host = slot_layout.control(self.screen, "/cursor_host")
            if host is not None:
                host.SetVisible(desktop)
        if api is not None:
            self.update_info(api, view)
        if not desktop:
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
        distribution = self.drag or self.settling_drag
        count = distribution["remainder"] if distribution and distribution["preview"] else (hand or {}).get("count", 0)
        if hand and hand["key"] and count and not self.screen.top_modal():
            signature = (hand["revision"], count)
            if self.cursor_signature != signature:
                if self.cursor_icon is None:
                    self.cursor_icon = slot_layout.control(self.screen, "/cursor_host/cursor_item")
                if self.cursor_count is None:
                    self.cursor_count = slot_layout.control(self.screen, "/cursor_host/cursor_count")
                if self.cursor_icon is not None and self.cursor_count is not None:
                    key = ResourceKey.from_dict(hand["key"])
                    self.cursor_icon.asItemRenderer().SetUiItem(key.name, key.aux or 0)
                    self.cursor_icon.SetVisible(True)
                    self.cursor_count.asLabel().SetText(str(count))
                    self.cursor_count.SetVisible(count > 1)
                    self.cursor_signature = signature
        elif self.cursor_signature is not None:
            self.cursor_icon.SetVisible(False)
            if self.cursor_count is not None:
                self.cursor_count.SetVisible(False)
            self.cursor_signature = None
        self.dispatch()

    def mapped_click(self, button, params):
        if not self.desktop or self.screen.top_modal():
            self.end_drag()
            return
        point = input_api().GetFocusPos()
        target = slot_layout.slot_at(self.screen, point)
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
        if (len(drag["slots"]) < 2 or not hand or not hand["key"]
                or drag["revision"] != hand["revision"] or self.active or self.pending):
            return
        key = ResourceKey.from_dict(hand["key"])
        limit = self.screen.list_view._stack_limit(key.name)
        eligible = []
        physical = 0
        for area, index in drag["slots"]:
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

        remaining = hand["count"]
        share = 1 if drag["button"] == "right" else max(1, remaining // max(1, physical))
        for area, index, existing in eligible:
            if area in ("bag", "grid"):
                amount = min(share, remaining, limit - existing)
                remaining -= amount
            else:
                # Template cells copy the configuration without consuming the cursor.
                amount = 1 if drag["button"] == "right" else hand["count"]
            if amount:
                target = (area, index)
                drag["placements"].append((area, index, amount))
                drag["preview"][target] = {"name": key.name, "aux": key.aux,
                                           "count": existing + amount}
        drag["remainder"] = remaining

    def preview_cell(self, area, index):
        # type: (str, object) -> object
        distribution = self.drag or self.settling_drag
        if distribution and isinstance(index, INTEGER_TYPES) and not isinstance(index, bool):
            return distribution["preview"].get((area, index))
        return None

    def retire_distribution(self):
        # type: () -> None
        # Keep the released layout visible while its sequential placements settle.
        if not self.settling_drag or self.active or self.pending:
            return
        template = getattr(self.screen, "template", None)
        if template and (template.stamps or template.stamp_request or template.revision < template.stamp_revision):
            return
        craft = getattr(self.screen, "craft", None)
        if craft and craft.revision < self.settling_drag.get("grid_revision", -1):
            return
        self.screen.backpack.replace_from_engine()
        self.settling_drag = None

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
            for region, slot, amount in drag["placements"]:
                self.click(region, slot, "place", amount)
            return
        self.drag = None
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
        if area != "bag":
            self.pocket_tap = None
        if self.screen.top_modal():
            self.end_drag()
            return True
        if not self.desktop:
            return False
        if self.drag and self.drag["button"] == "left":
            point = input_api().GetFocusPos()
            if point != self.drag["start_point"] or len(self.drag["slots"]) > 1:
                target = slot_layout.slot_at(self.screen, point)
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
        self.active = (request, area, index, key, hand, point, fields)

    def settle(self, result):
        if self.active is None or self.active[0] != result.get("request_id"):
            return
        _, area, index, key, before, point, sent = self.active
        gesture, version = sent["config"]["gesture"], sent.get("slot_version")
        self.active = None
        payload = result.get("payload") or {}
        client = self.screen.list_view.cache.client
        hand = payload.get("cursor")
        if hand and (not client.cursor or hand["revision"] >= client.cursor["revision"]):
            client.cursor = hand
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
        if gesture == "place" and self.settling_drag:
            if payload.get("moved") != sent["count"]:
                self.settling_drag = None
            elif area == "grid" and "device_revision" in payload:
                self.settling_drag["grid_revision"] = payload["device_revision"]
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
