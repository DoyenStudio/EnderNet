# coding=utf-8
"""Close prefab windows before dropping their controllers on screen exit."""

from .names import (
    FLUID_LIST_WINDOW_ATTRIBUTE,
    ITEM_LIST_WINDOW_ATTRIBUTE,
    NUMBER_SET_WINDOW_ATTRIBUTE,
    SELECT_FLUID_WINDOW_ATTRIBUTE,
    SELECT_ITEM_WINDOW_ATTRIBUTE,
)


WINDOW_ATTRIBUTES = (
    ITEM_LIST_WINDOW_ATTRIBUTE,
    FLUID_LIST_WINDOW_ATTRIBUTE,
    SELECT_ITEM_WINDOW_ATTRIBUTE,
    SELECT_FLUID_WINDOW_ATTRIBUTE,
    NUMBER_SET_WINDOW_ATTRIBUTE,
    "_endernet_recipe_choice_window",
)


def hide_window(screen, root):
    # type: (object, object) -> None
    if root is None:
        return
    try:
        # ToolDelta marks the host inactive BEFORE OnDeactive/OnDestroy.
        # Its prefab controls can still exist and retain visibility on reuse.
        root.SetVisible(False)
    except Exception:
        # A destroyed native control needs no hiding, but controller cleanup
        # must still finish. Active-screen failures must remain observable.
        if screen._activated:
            raise


def close_windows(screen):
    # type: (object) -> None
    windows = getattr(screen, "_vars", {})
    for attribute in WINDOW_ATTRIBUTES:
        window = windows.get(attribute)
        if window is not None:
            window.close()
