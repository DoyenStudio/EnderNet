# coding=utf-8
"""工厂样板的配方提示格式与引擎物品名查询。"""

import types
import unittest

import testsupport  # noqa: F401

from endernet.common.definitions import TEMPLATE_PANEL_ITEM_ID
from endernet.common.resource_key import ResourceKey
from endernet.server.adapters.template_tips import resource_display_name
from endernet.server.crafting.patterns import (
    MODE_CRAFTING, MODE_FACTORY, PatternDefinition, PatternStack,
)
from endernet.server.crafting.template_terminal import encode_template_item


class TestTemplateTips(unittest.TestCase):
    def test_both_modes_use_localized_names_and_exact_multiline_format(self):
        source = ResourceKey.item("minecraft:coal", 1)
        second = ResourceKey.item("minecraft:iron_ingot")
        target = ResourceKey.item("minecraft:iron_block")
        names = {source: "煤炭", second: "铁锭", target: "铁块"}
        for mode in (MODE_CRAFTING, MODE_FACTORY):
            definition = PatternDefinition(
                mode,
                [PatternStack(source, 2, slot=0 if mode == MODE_CRAFTING else None),
                 PatternStack(second, 9, slot=1 if mode == MODE_CRAFTING else None)],
                [PatternStack(target, 1)],
            )
            key, data = encode_template_item(definition, name_of=names.__getitem__)
            self.assertEqual(TEMPLATE_PANEL_ITEM_ID, key.name)
            self.assertFalse(data.get("customTips"))
            self.assertNotIn("Name", data["userData"]["display"])
            self.assertEqual("§r§f制作： 铁块\n§r§f使用：\n§r§7 - 煤炭x2\n§r§7 - 铁锭x9",
                             "\n".join(data["userData"]["display"]["Lore"]))
            self.assertEqual(key, encode_template_item(
                definition, name_of=names.__getitem__)[0])

    def test_repeated_inputs_merge_counts_in_first_seen_order(self):
        copper = ResourceKey.item("minecraft:copper_ingot")
        coal = ResourceKey.item("minecraft:coal")
        target = ResourceKey.item("minecraft:copper_block")
        names = {copper: "铜锭", coal: "煤炭", target: "铜块"}
        for mode in (MODE_CRAFTING, MODE_FACTORY):
            inputs = [PatternStack(key, count,
                                   slot=index if mode == MODE_CRAFTING else None)
                      for index, (key, count) in enumerate(
                          [(copper, 4), (coal, 1), (copper, 5)])]
            definition = PatternDefinition(mode, inputs, [PatternStack(target, 1)])
            _key, data = encode_template_item(definition, name_of=names.__getitem__)
            self.assertEqual("§r§f制作： 铜块\n§r§f使用：\n§r§7 - 铜锭x9\n§r§7 - 煤炭x1",
                             "\n".join(data["userData"]["display"]["Lore"]))
            self.assertEqual([4, 1, 5], [stack.count for stack in definition.inputs])

    def test_different_aux_values_stay_separate(self):
        definition = PatternDefinition(MODE_FACTORY, [
            PatternStack(ResourceKey.item("minecraft:stone", 0), 2),
            PatternStack(ResourceKey.item("minecraft:stone", 1), 3),
        ], [PatternStack(ResourceKey.item("minecraft:iron_block"), 1)])
        _key, data = encode_template_item(definition, name_of=lambda key: "同名")
        self.assertEqual("§r§f制作： 同名\n§r§f使用：\n§r§7 - 同名x2\n§r§7 - 同名x3",
                         "\n".join(data["userData"]["display"]["Lore"]))

    def test_engine_lookup_uses_aux_and_basic_item_name(self):
        calls = []

        class FakeItem(object):
            def __init__(self, name, aux):
                calls.append((name, aux))

            def GetBasicInfo(self):
                return types.SimpleNamespace(itemName="花岗岩")

        self.assertEqual("花岗岩", resource_display_name(
            ResourceKey.item("minecraft:stone", 1), item_factory=FakeItem))
        self.assertEqual([("minecraft:stone", 1)], calls)
