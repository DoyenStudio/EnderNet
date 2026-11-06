# coding=utf-8
"""PC item tooltips, using the same native renderer and text API as MEI."""
from . import slot_layout, stack_size
from ...common.resource_key import ResourceKey


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


class TerminalHover(object):
    def __init__(self, screen):
        self.screen = screen
        self.control = None
        self.identity = None
        self.text = b""
        self.shown = None

    def hide(self):
        if self.control is not None and self.shown is not None:
            self.control.SetVisible(False)
        self.shown = None

    def update(self, target):
        key, craftable = hovered_stack(self.screen, target)
        if key is None:
            self.hide()
            return
        identity = key.to_dict()
        if identity != self.identity:
            self.text = formatted_hover(key)
            tips = key.raw_identity.get("custom_tips")
            if tips and native_text(tips) not in self.text:
                self.text += b"\n" + native_text(tips)
            self.identity = identity
        text = self.text + (native_text(u"\n§e[可制造]") if craftable else b"")
        if self.control is None:
            self.control = slot_layout.control(self.screen, "/cursor_host/hover_text")
        if self.control is not None and text != self.shown:
            self.control.SetPropertyBag({"#hover_text": text})
            self.control.SetVisible(True)
            self.shown = text
