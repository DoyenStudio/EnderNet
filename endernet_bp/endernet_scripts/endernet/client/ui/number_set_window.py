# coding=utf-8
"""Reusable integer quantity dialog embedded in a ToolDeltaScreen."""

from functools import partial

from ....tooldelta.ui import Binder

if 0 > 1:
    import typing

    from ....tooldelta.ui import ToolDeltaScreen

WINDOW_ATTRIBUTE = "_endernet_number_set_window"
EDIT_BINDING = "#EnderNetNumberSet.number"
STEP_BUTTONS = (
    ("add_1x_btn", 1), ("add_10x_btn", 10), ("add_100x_btn", 100),
    ("sub_1x_btn", -1), ("sub_10x_btn", -10), ("sub_100x_btn", -100),
)

try:
    INTEGER_TYPES = (int, long)
except NameError:
    INTEGER_TYPES = (int,)


def show_number_set_window(screen, number, confirm_callback, min_number=1, max_number=None):
    # type: (ToolDeltaScreen, int, typing.Callable[[int], None], int, int | None) -> bool
    """Open once per screen; confirm returns an integer, close only cancels.

    Initial values and button steps are clamped to the supplied range. Empty,
    non-integer and out-of-range input cannot be confirmed. The host may call
    screen._vars[WINDOW_ATTRIBUTE].close() from OnDestroy for cleanup.
    """
    if WINDOW_ATTRIBUTE in screen._vars:
        return False
    values = (number, min_number) if max_number is None else (number, min_number, max_number)
    for value in values:
        if isinstance(value, bool) or not isinstance(value, INTEGER_TYPES):
            raise ValueError("quantity and bounds must be integers")
    if min_number < 0 or (max_number is not None and max_number < min_number):
        raise ValueError("invalid quantity range")
    if not callable(confirm_callback):
        raise TypeError("confirm_callback must be callable")
    NumberSetWindow(screen, number, confirm_callback, min_number, max_number)
    return True


class NumberSetWindow(object):
    WINDOW_ATTRIBUTE = WINDOW_ATTRIBUTE
    EDIT_BINDING = EDIT_BINDING
    CONTROL_DEFINITION = "endernet_windows.number_set_window"
    CONTROL_NAME = "endernet_number_set_window"

    def __init__(self, screen, number, confirm_callback, min_number=1, max_number=None):
        self.screen = screen
        self.confirm_callback = confirm_callback
        self.min_number = min_number
        self.max_number = max_number
        self.number = min_number
        self.closed = False
        self.root = None
        self.editor = None
        screen._vars[self.WINDOW_ATTRIBUTE] = self
        try:
            self.root = screen.AddElement(self.CONTROL_DEFINITION, self.CONTROL_NAME)
            self.root.SetLayer(50)
            self.editor = self.root.GetElement("num_editor").asTextEditBox()
            for path, step in STEP_BUTTONS:
                self.root.GetElement(path).asButton().SetCallback(partial(self._step, step))
            self.root.GetElement("close").asButton().SetCallback(self.close)
            self.root.GetElement("confirm_btn").asButton().SetCallback(self._confirm)
            # This helper is shared by arbitrary screens, so bind only while open.
            screen.DynaBinding(Binder.BF_EditChanged, self.EDIT_BINDING, self._edited)
            self.set_number(number)
        except Exception:
            self.close()
            raise

    def set_number(self, number):
        # type: (int) -> None
        """Update the displayed quantity, clamping it to the configured range."""
        if self.closed:
            return
        if isinstance(number, bool) or not isinstance(number, INTEGER_TYPES):
            raise ValueError("quantity must be an integer")
        self.number = max(self.min_number, number)
        if self.max_number is not None:
            self.number = min(self.max_number, self.number)
        self.editor.SetText(str(self.number))

    def _parse(self, text):
        text = (text or "").strip()
        if not text or any(char not in "0123456789" for char in text):
            return None
        try:
            number = int(text)
        except ValueError:
            return None
        if number < self.min_number or (self.max_number is not None and number > self.max_number):
            return None
        return number

    def _edited(self, params):
        if self.closed:
            return
        number = self._parse(params.get("Text"))
        if number is not None:
            self.number = number

    def _step(self, step, params=None):
        if self.closed or not self.screen._activated:
            return
        number = self._parse(self.editor.GetText())
        # Invalid/empty text recovers from the last valid quantity.
        self.set_number((self.number if number is None else number) + step)

    def _confirm(self, params=None):
        if self.closed or not self.screen._activated:
            return
        # Read the control directly in case its last edit event is still pending.
        number = self._parse(self.editor.GetText())
        if number is None:
            return
        callback = self.confirm_callback
        self.close()
        callback(number)

    def close(self, params=None):
        """Cancel safely, including after the host screen has been destroyed."""
        if self.closed:
            return
        self.closed = True
        self.screen.DynaUnbinding(Binder.BF_EditChanged, self.EDIT_BINDING)
        if self.screen._activated and self.root is not None:
            self.root.Remove()
        if self.screen._vars.get(self.WINDOW_ATTRIBUTE) is self:
            del self.screen._vars[self.WINDOW_ATTRIBUTE]
        self.confirm_callback = None
        self.editor = None
        self.root = None
