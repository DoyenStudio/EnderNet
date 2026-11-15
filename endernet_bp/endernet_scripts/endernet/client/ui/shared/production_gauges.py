# coding=utf-8
"""SkyBlueTech machine gauges adapted to EnderNet's server snapshots.

Adapted from SkyBlueTech client/ui/machinery/utils.py and define.py.
Copyright (C) 2025 SuperScript@Doyen-Studio, MIT; see
third_party/skybluetech-ui-LICENSE.txt for the complete notice.
"""

ENERGY_PURPLE = (0.52, 0.68, 1.0)
ENERGY_TEXTURES = tuple("textures/endernet/ui/production/skybluetech/" + path for path in (
    "energy/orig_fill_rose", "energy/orig_fill_amber", "energy/orig_fill_red"))
ENERGY_TIPS = ("", "\n§e电量偏低", "\n§c电量告急")


def fraction(value, capacity):
    # type: (float, float) -> float
    return max(0.0, min(1.0, float(value) / capacity)) if capacity > 0 else 0.0


def energy_display(amount, capacity, height, generator=False):
    # type: (int, int, float, bool) -> tuple[float, int]
    ratio = fraction(amount, capacity)
    height = int(round(height))
    segments = (height - 1) // 3 if 10 <= height <= 400 and (height - 1) % 3 == 0 else 0
    shown = ratio
    if segments:
        count = int(ratio * segments + 0.5)
        if ratio > 0 and count == 0:
            count = 1
        shown = 3.0 * count / (3 * segments + 1)
    level = 0 if generator else (2 if ratio < 0.05 else (1 if ratio < 0.15 else 0))
    return shown, level


def format_energy(amount):
    # type: (int) -> str
    if 0 <= amount < 1000:
        return "%d FE" % amount
    number = float(amount)
    suffixes = ("", "k", "M", "G", "T", "P", "E", "Z", "Y")
    index = 0
    while number >= 1000 and index < len(suffixes) - 1:
        number /= 1000
        index += 1
    return "%.2f %sFE" % (number, suffixes[index])


def set_hover_text(control, text):
    # type: (object, str) -> None
    cache = control._vars
    if cache.get("hover_text") == text:
        return
    cache["hover_text"] = text
    if "hover_targets" not in cache:
        cache["hover_targets"] = [control["info/hover/hover_text"], control["info/pressed/hover_text"]]
    for target in cache["hover_targets"]:
        target.SetPropertyBag({"#hover_text": text})


class ProductionGauges(object):
    def __init__(self, screen, path, generator=False, progress_direction="fromTopToBottom"):
        # type: (object, object, bool, str) -> None
        self.generator = generator
        self.progress_direction = progress_direction
        path = str(path)
        self.power = screen.GetElement(path + "/main/power_bar")
        self.bar = self.power["bar"]
        self.fill = self.power["bar/mask"].asImage()
        self.indicator = screen.GetElement(path + ("/main/flame" if generator else "/main/progress"))
        self.indicator_fill = self.indicator["mask"].asImage()
        self.previous_energy = None
        self.previous_progress = None

    def update(self, data):
        # type: (dict) -> None
        from .....tooldelta.define import UICtrlPosData

        amount, capacity = data.get("energy", 0), data.get("capacity", 0)
        shown, level = energy_display(amount, capacity, self.bar.GetSize()[1], self.generator)
        if self.previous_energy != (shown, level):
            # A tiled image ignores clip_ratio: resize the bottom-anchored fill,
            # keeping the same complete 2px segments and 1px gaps as SkyBlueTech.
            self.fill.SetFullSize("y", UICtrlPosData("parent", relative_value=shown))
            self.fill.SetSprite(ENERGY_TEXTURES[level])
            self.fill.SetSpriteColor(ENERGY_PURPLE if level == 0 else (1.0, 1.0, 1.0))
            self.previous_energy = (shown, level)
        hover = "§b储能： §f%s\n§7容量： §f%s%s" % (
            format_energy(amount), format_energy(capacity), ENERGY_TIPS[level])
        set_hover_text(self.power, hover if data else "等待能源数据")

        progress = fraction(data.get("burn", 0), data.get("burn_duration", 0)) if self.generator else fraction(
            data.get("progress", 0), data.get("duration", 0))
        if progress != self.previous_progress:
            self.indicator_fill.SetSpriteClipRatio(self.progress_direction, 1.0 - progress)
            self.previous_progress = progress
