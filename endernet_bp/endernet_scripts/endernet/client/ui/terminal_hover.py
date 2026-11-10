# coding=utf-8
"""PC item tooltips, using the same native renderer and text API as MEI.

The slot button's hover state shows `common.hover_text` (see `item_can_be_selected`);
its collection binding asks `TerminalHover.text` for the hovered cell only.
On touch the same text goes to the bottom bubble (`ItemDetails`), as vanilla Pocket does.
"""
from . import slot_layout, stack_size
from ...common.resource_key import ResourceKey

#: 提示文本按物品身份缓存，最多这么多种。
CACHE_LIMIT = 256

#: 底部说明气泡（原版 `selected_item_details`），挂在铺满屏幕的根面板上。
DETAILS_PATH = "/item_details"
DETAILS_IMAGE_PATH = DETAILS_PATH + "/item_panel_image"
DETAILS_LABEL_PATH = DETAILS_IMAGE_PATH + "/item_text_label"
#: 出现后多久淡出完（秒），照原版 `anim_item_details_alpha`：2 秒 in_quint。
DETAILS_FADE_SECONDS = 2.0


def native_text(value):
    # type: (object) -> str
    return value.encode("utf-8") if not isinstance(value, bytes) else value


def formatted_hover(key):
    # type: (ResourceKey) -> str
    try:
        import mod.client.extraClientApi as client_api
    except ImportError:
        return native_text(stack_size.display_name(key.name))
    comp = client_api.GetEngineCompFactory().CreateItem(client_api.GetLevelId())
    text = comp.GetItemFormattedHoverText(
        native_text(key.name), key.aux or 0, False, key.raw_identity.get("user_data"))
    return native_text(text or stack_size.display_name(key.name))


def hovered_stack(screen, target):
    # type: (object, object) -> tuple
    """Return the item key and whether this network row has a crafting pattern."""
    if target is None:
        return None, False
    area, index = target
    view = screen.list_view
    if area == "network":
        if view.has_item(index):
            row = view.rows[index]
            return row.key, row.has_pattern
        return None, False
    if area == "bag":
        entry = screen.backpack.entry(index)
        return (entry.key, False) if entry else (None, False)
    if area in ("grid", "output"):
        model = getattr(screen, "craft", None)
        row = (model.preview if area == "output" else model.cell(index)) if model else None
    else:
        model = getattr(screen, "template", None)
        row = model.cell(area, index) if model else None
    # Device cell messages only contain name/aux; never parse their fingerprints.
    if row and row.get("name"):
        return ResourceKey.item(row["name"], row.get("aux") or 0), False
    return None, False


def slot_durability(screen, target):
    # type: (object, tuple) -> tuple
    """格子里那件物品的耐久条 `(显示, 总量, 当前)`。"""
    if target[0] == "network" and screen.list_view is None:
        return False, 0, 0
    return stack_size.durability(hovered_stack(screen, target)[0])


class TerminalHover(object):
    def __init__(self, screen, router):
        self.screen = screen
        self.router = router
        self.cache = {}

    def base_text(self, key):
        # type: (ResourceKey) -> bytes
        text = self.cache.get(key)
        if text is None:
            text = formatted_hover(key)
            tips = key.raw_identity.get("custom_tips")
            if tips and native_text(tips) not in text:
                text += b"\n" + native_text(tips)
            if len(self.cache) >= CACHE_LIMIT:
                self.cache.clear()
            self.cache[key] = text
        return text

    def text(self, target):
        # type: (tuple) -> bytes
        """被悬停那一格的提示；触屏、拖拽分配、模态窗与空格都不显示。"""
        router = self.router
        if (not router.desktop or router.drag or router.settling_drag
                or self.screen.list_view is None or self.screen.top_modal()):
            return b""
        return self.describe(target)

    def describe(self, target):
        # type: (tuple) -> bytes
        """这一格物品的提示文本；空格返回空串。"""
        if target[0] == "network" and self.screen.list_view is None:
            return b""
        key, craftable = hovered_stack(self.screen, target)
        if key is None:
            return b""
        text = self.base_text(key)
        return text + native_text(u"\n§e[可制造]") if craftable else text


class ItemDetails(object):
    """底部说明气泡：触屏选中物品时显示它的提示，回执失败时（两种输入）显示原因。

    照原版每次出现都从不透明开始，2 秒内按 in_quint 淡出后收起。
    """

    def __init__(self, screen, hover):
        self.screen = screen
        self.hover = hover
        self.selection = None
        self.shown_at = None
        self.controls = None

    def _controls(self):
        # type: () -> object
        if self.controls is None:
            found = [slot_layout.screen_control(self.screen, path)
                     for path in (DETAILS_PATH, DETAILS_IMAGE_PATH, DETAILS_LABEL_PATH)]
            if None not in found:
                self.controls = found
        return self.controls

    def show(self, text):
        # type: (object) -> None
        controls = self._controls()
        if not text or controls is None:
            self.hide()
            return
        panel, image, label = controls
        label.asLabel().SetText(native_text(text))
        image.SetAlpha(1.0)
        label.SetAlpha(1.0)
        panel.SetVisible(True)
        self.shown_at = self.screen._clock()

    def hide(self):
        # type: () -> None
        if self.shown_at is not None and self.controls is not None:
            self.controls[0].SetVisible(False)
        self.shown_at = None

    def reset(self):
        # type: () -> None
        self.hide()
        self.selection = None

    def notice(self, result):
        # type: (dict) -> None
        reasons = result.get("reasons") or ()
        if reasons and reasons[0].get("detail"):
            self.show(reasons[0]["detail"])

    def tick(self, target, token):
        # type: (object, object) -> None
        """`target` 是触屏当前选中的格子、`token` 标识这次选中（键鼠两个都传 None）。
        选中换了就换成新物品的提示并重新淡出；取消选中不收起，让它照常淡完。"""
        if self.screen.top_modal():
            self.hide()
            return
        if token != self.selection:
            self.selection = token
            if target is not None:
                self.show(self.hover.describe(target))
        if self.shown_at is None:
            return
        progress = float(self.screen._clock() - self.shown_at) / DETAILS_FADE_SECONDS
        if progress >= 1.0:
            self.hide()
            return
        alpha = 1.0 - progress ** 5
        self.controls[1].SetAlpha(alpha)
        self.controls[2].SetAlpha(alpha)
