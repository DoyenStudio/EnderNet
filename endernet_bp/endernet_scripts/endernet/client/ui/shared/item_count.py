# coding=utf-8
"""Compact terminal slot quantities; integer truncation avoids overstating stock."""


COUNT_UNITS = ("", "k", "M", "G", "T", "P", "E")


def format_item_count(count, hide_one=True):
    # type: (int, bool) -> str
    """Show small counts verbatim; floor SI quantities to one decimal place."""
    if hide_one and count == 1:
        return ""
    unit = 0
    scale = 1
    while count >= scale * 1000 and unit < len(COUNT_UNITS) - 1:
        scale *= 1000
        unit += 1
    if unit == 0:
        return str(count)
    tenths = count * 10 // scale
    return "%d.%d%s" % (tenths // 10, tenths % 10, COUNT_UNITS[unit])
