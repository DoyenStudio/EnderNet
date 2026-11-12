# coding=utf-8
"""触屏的长按拆堆与拖动，照原版 Pocket 容器（User 2026-11-08 定）。

- **长按背包格**：过长按门槛后格子上方出现大数字进度条（`split_overlay`，定位在
  格子上，原版 `inventory_hold_icon_holding`），默认拆出一半（向上取整）；手指右滑加、
  左滑减，滑过进度条的宽度正好是整叠，最少 1 个。松手（在哪松都一样）后大进度条
  消失，这一格以拆出的数量选中，格子上出现原版灰底绿色小进度条（`split_bar`，原版
  `inventory_hold_icon_release`，拆出数量 / 整叠），一直显示到取消选中或放下。
- **滑动平分**：已经选中了背包格时，按住格子滑过去 = 沿手指经过的格子平分选中的那一叠，
  规则与键鼠左键拖动相同（`TerminalInput.begin_spread`），松手才落（User 2026-11-09）。
- **拖动**：没有选中时，没到长按门槛就挪动 = 拖整格，在别处松手放下。背包格 → 背包格（合并、
  移到空格、整格交换）、网络区（存入）、合成格（整格放入）；网络行 → 背包格（取一组）。
  拖出窗口松手 = 取消。

拖动的判定只看按下与松手两处：位置取按钮回调参数里的 `TouchPosX/Y`
（`slot_layout.click_rect`），没挪动或在原格松手仍按点按处理。按住期间的手指位置取
跟随光标的 `cursor_host`（`follows_cursor`），只拿它与自己的读数比：判断挪没挪、
滑了多远，并摆跟手的图标，不和回调坐标混用。
"""

from ..shared.item_count import format_item_count

from ..shared import slot_layout

#: 大数字进度条里填充条的宽度（UI 像素），滑过这么远 = 整叠（原版 `stack_progress_bar_fill` 64×10）。
SPLIT_SLIDE_PIXELS = 64.0

#: 挪动超过这么多（UI 像素，取两轴较大者）才算拖动，与网络行点按的防误触同一个口径
#: （`TerminalListView.on_select`）。
DRAG_PIXELS = 5.0

#: 进度条在格子上方放不下时改放在下方，需要的高度（原版 up 变体：条高 26、离格子 7）。
SPLIT_ABOVE_PIXELS = 33.0

#: 选中格上那条小进度条：原版 `inventory_hold_icon_release` 的键鼠尺寸，中心比格子中心高 7。
#: 它在终端窗口里（与 `cursor_host` 同级），数据走面板的 `split_bar_*` 绑定。
BAR_SIZE = (15.0, 3.0)
BAR_RISE = 7.0
BAR_PATH = "/split_bar"

HOST_PATH = "/cursor_host"
ICON_PATH = HOST_PATH + "/cursor_item"
COUNT_PATH = HOST_PATH + "/cursor_count"
#: 大数字进度条挂在铺满屏幕的根面板上（`endernet_ui_common.screen_root`），按格子的位置摆。
OVERLAY_PATH = "/split_overlay"
VARIANTS = ("split_up", "split_down")


def half_up(total):
    # type: (int) -> int
    return (total + 1) // 2


def slide_count(total, dx):
    # type: (int, float) -> int
    """从一半起，手指横向挪了 `dx` 像素后拆出多少个（右正左负，1 到整叠）。"""
    count = half_up(total) + int(round(float(dx) / SPLIT_SLIDE_PIXELS * total))
    return max(1, min(total, count))


def _point(params):
    # type: (object) -> object
    rect = slot_layout.click_rect(params)
    return rect[:2] if rect else None


def _far(first, second):
    # type: (tuple, tuple) -> bool
    return max(abs(first[0] - second[0]), abs(first[1] - second[1])) > DRAG_PIXELS


class TouchGesture(object):
    def __init__(self, screen, hold_seconds):
        self.screen = screen
        self.hold_seconds = hold_seconds
        self.source = None
        #: 上一次摆出来的样子；JSON 里这些控件默认隐藏。
        self.drag_shown = (None, 0)
        self.split_shown = None
        self.bar_shown = None

    def reset(self):
        # type: () -> None
        self.source = None
        self._show_drag(None, 0)
        self._show_split(None)
        self._place_bar(None)

    def press(self, area, index, params):
        # type: (str, int, object) -> None
        """触屏按下背包格（`bag`）或网络行（`network`）。"""
        self.source = None
        self._show_drag(None, 0)
        self._show_split(None)
        screen = self.screen
        view = screen.list_view
        if area == "bag":
            entry = screen.backpack.entry(index)
            if entry is None and screen.backpack.selected is None:
                return
            # 空格只在已有选中时记下：那是滑动平分的起点。
            key, total = (entry.key, entry.count()) if entry is not None else (None, 0)
        elif view is not None and view.has_item(index):
            key, total = view.rows[index].key, 0
        else:
            return
        self.source = {"area": area, "index": index, "key": key, "total": total,
                       "point": _point(params), "origin": None, "at": screen._clock(),
                       "moved": False, "split": None,
                       "scroll": view._scroll_position() if area == "network" and view is not None else None}

    def tick(self):
        # type: () -> None
        backpack = self.screen.backpack
        self._place_bar(backpack.selected if self.selection_bar() else None)
        source = self.source
        if source is None:
            return
        if self.screen.top_modal():
            self.reset()
            return
        finger = self.finger()
        if finger is not None and source["origin"] is None:
            source["origin"] = finger
        if (source["split"] is None and not source["moved"] and source["key"] is not None
                and source["area"] == "bag" and self.screen._clock() - source["at"] >= self.hold_seconds):
            source["split"] = {"anchor": finger, "count": half_up(source["total"])}
        split = source["split"]
        if split is None:
            if finger is not None and _far(finger, source["origin"]):
                source["moved"] = True
            if source["moved"] and source["area"] == "bag" and self.screen.backpack.selected is not None:
                self._spread(finger)
                return
            self._show_drag(source["key"] if source["moved"] else None, source["total"])
            return
        if finger is not None:
            if split["anchor"] is None:
                split["anchor"] = finger
            split["count"] = slide_count(source["total"], finger[0] - split["anchor"][0])
        self._show_split((source["index"], split["count"], source["total"]))

    def release(self, params):
        # type: (object) -> bool
        """松手。返回 True 表示已按拆堆或拖动处理，调用方不再当点按处理。"""
        source = self.source
        self.source = None
        self._show_drag(None, 0)
        self._show_split(None)
        if source is None:
            return False
        if source.get("spread"):
            self.screen.slot_input.finish_spread()
            return True
        split = source["split"]
        if split is not None:
            self._select(source["index"], split["count"])
            return True
        point = _point(params)
        home = (source["area"], source["index"])
        target = slot_layout.slot_at(self.screen, point) if point else None
        moved = source["moved"] or bool(point and source["point"] and _far(point, source["point"]))
        if not moved or target == home:
            return False
        if target is not None:
            self._drop(source, target)
        return True

    def _spread(self, finger):
        # type: (object) -> None
        """选中之后滑动：交给路由按键鼠左键拖动的规则平分（`begin_spread` / `visit`）。"""
        source = self.source
        router = self.screen.slot_input
        if "spread" not in source:
            source["spread"] = router.begin_spread((source["area"], source["index"]))
        if source["spread"] and finger is not None:
            router.visit(slot_layout.slot_at(self.screen, finger))

    # -- 落点 ----------------------------------------------------------

    def _select(self, ui_index, count):
        view = self.screen.list_view
        if view is not None:
            view.cache.model.clear_selection()
        self.screen.backpack.select_partial(ui_index, count)

    def _drop(self, source, target):
        screen = self.screen
        view = screen.list_view
        if view is None or not view.cache.client.has_session():
            return
        area, index = target
        backpack = screen.backpack
        if source["area"] == "network":
            # 起点在列表里而列表滚动过，那是一次滚动，不是拖动。
            if area != "bag" or view._scroll_position() != source["scroll"]:
                return
            if view.cache.model.select(source["key"]):
                backpack.clear_selection()
                view.on_backpack_put(index)
            return
        entry = backpack.entry(source["index"])
        if entry is None or entry.key != source["key"]:
            return
        self._select(source["index"], None)
        if area == "bag":
            view.on_backpack_exchange(index)
        elif area == "network":
            view.on_quick_store()
        elif area == "grid" and getattr(screen, "craft", None):
            screen.craft.on_grid_click({"#collection_index": index})

    # -- 显示 ----------------------------------------------------------

    def finger(self):
        # type: () -> object
        """手指此刻的位置（跟随光标的 `cursor_host`）；取不到返回 None。"""
        host = slot_layout.control(self.screen, HOST_PATH)
        if host is None:
            return None
        x, y = host.GetGlobalPosition()
        return float(x), float(y)

    def _slot_offset(self, ui_index, root):
        # type: (int, object) -> object
        """背包格相对 `root` 控件的位置 `(x, y, 宽, 高)`；取不到返回 None。"""
        rect = slot_layout.backpack_slot_rect(self.screen, ui_index)
        if rect is None or root is None:
            return None
        x, y = root.GetGlobalPosition()
        return rect[0] - x, rect[1] - y, rect[2], rect[3]

    def selection_bar(self):
        # type: () -> object
        """长按拆出后选中格上的进度 `(拆出数量, 整叠)`；没有拆出时为 None（供绑定读）。"""
        backpack = self.screen.backpack
        if backpack.selected is None or backpack.selected_count is None:
            return None
        entry = backpack.entry(backpack.selected)
        if entry is None:
            return None
        return backpack.selected_count, entry.count()

    def _show_drag(self, key, count):
        # type: (object, int) -> None
        """拖动中跟手的图标与数量。"""
        state = (key, count if key is not None else 0)
        if state == self.drag_shown:
            return
        self.drag_shown = state
        icon = slot_layout.control(self.screen, ICON_PATH)
        label = slot_layout.control(self.screen, COUNT_PATH)
        if icon is not None:
            if key is not None:
                icon.asItemRenderer().SetUiItem(key.name, key.aux or 0)
            icon.SetVisible(key is not None)
        if label is not None:
            if key is not None and count > 1:
                label.asLabel().SetText(format_item_count(count, hide_one=False))
            label.SetVisible(key is not None and count > 1)

    def _show_split(self, state):
        # type: (object) -> None
        """格子上方的大数字进度条；`state` 是 `(背包格, 拆出数量, 整叠)`。"""
        if state == self.split_shown:
            return
        self.split_shown = state
        overlay = slot_layout.screen_control(self.screen, OVERLAY_PATH)
        if overlay is None:
            return
        place = self._slot_offset(state[0], slot_layout.screen_control(self.screen, "")) if state else None
        if place is None:
            overlay.SetVisible(False)
            return
        ui_index, count, total = state
        overlay.SetPosition((place[0], place[1]))
        shown = VARIANTS[0] if place[1] >= SPLIT_ABOVE_PIXELS else VARIANTS[1]
        for name in VARIANTS:
            variant = slot_layout.screen_control(self.screen, "%s/%s" % (OVERLAY_PATH, name))
            if variant is None:
                continue
            if name == shown:
                fill = slot_layout.screen_control(self.screen, "%s/%s/split_fill" % (OVERLAY_PATH, name))
                label = slot_layout.screen_control(self.screen, "%s/%s/split_count" % (OVERLAY_PATH, name))
                if fill is not None:
                    fill.asImage().SetSpriteClipRatio(1.0 - float(count) / total)
                if label is not None:
                    label.asLabel().SetText(format_item_count(count, hide_one=False))
            variant.SetVisible(name == shown)
        overlay.SetVisible(True)

    def _place_bar(self, ui_index):
        # type: (object) -> None
        """把选中格上的小进度条摆到那一格；显示与数值由绑定管（`selection_bar`）。"""
        if ui_index == self.bar_shown:
            return
        self.bar_shown = ui_index
        bar = slot_layout.control(self.screen, BAR_PATH)
        if ui_index is None or bar is None:
            return
        place = self._slot_offset(ui_index, slot_layout.control(self.screen, slot_layout.CONTENT_PATH))
        if place is None:
            return
        center = (place[0] + place[2] / 2.0, place[1] + place[3] / 2.0)
        bar.SetPosition((center[0] - BAR_SIZE[0] / 2.0, center[1] - BAR_RISE - BAR_SIZE[1] / 2.0))
