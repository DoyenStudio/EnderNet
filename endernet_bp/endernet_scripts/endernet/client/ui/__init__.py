# coding=utf-8
"""Terminal UI package. Engine registration is explicit in register_screens."""


def register_screens():
    # Import only on the client, before UiInitFinishedEvent.
    from .terminal_panel import CommonTerminalPanel

    return CommonTerminalPanel
