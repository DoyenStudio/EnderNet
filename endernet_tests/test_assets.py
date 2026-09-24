# coding=utf-8
"""资源一致性：方块 JSON、资源包映射、贴图文件与设备定义表必须对得上。

这些检查在离线状态下完全可做，而且正是最容易悄悄坏掉的地方——
新增一个设备却忘了加方块 JSON，或者贴图键拼错导致游戏里显示紫黑块，
都不会有任何测试失败，直到进游戏才发现。
"""

import io
import json
import os
import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import DefinitionSet

REPO = testsupport.REPO_ROOT
BP_BLOCKS = os.path.join(REPO, "endernet_bp", "blocks")
RP_ROOT = os.path.join(REPO, "endernet_rp")
RP_BLOCKS = os.path.join(RP_ROOT, "blocks.json")
RP_TERRAIN = os.path.join(RP_ROOT, "textures", "terrain_texture.json")
TEXTURE_DIR = os.path.join(RP_ROOT, "textures", "endernet", "devices")

#: 运行时销毁流程依赖它；缺了引擎不会派发 BlockRemoveServerEvent。
REQUIRED_COMPONENTS = ("netease:block_entity", "netease:listen_block_remove")


def read_json(path):
    with io.open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def short_name(device_id):
    return device_id.split(":", 1)[1]


def block_file_name(device_id):
    """与 scripts/gen_blocks.py 保持一致：命名空间作前缀，冒号换成下划线。"""
    return device_id.replace(":", "_") + ".json"


class AssetFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definitions = DefinitionSet()
        cls.blocks_json = read_json(RP_BLOCKS)
        cls.terrain = read_json(RP_TERRAIN)
        cls.terrain_data = cls.terrain.get("texture_data", {})
        cls.block_ids = sorted(cls.definitions.devices)

    def block_json_path(self, device_id):
        return os.path.join(BP_BLOCKS, block_file_name(device_id))

    def read_block_json(self, device_id):
        return read_json(self.block_json_path(device_id))


class TestBlockJsonCoverage(AssetFixture):
    def test_every_device_has_a_block_json(self):
        missing = [
            device_id
            for device_id in self.block_ids
            if not os.path.isfile(self.block_json_path(device_id))
        ]
        self.assertEqual(missing, [], "缺少方块 JSON：%r" % (missing,))

    def test_no_orphan_block_json(self):
        """方块目录里不该有定义表之外的方块。"""
        known = set(block_file_name(d) for d in self.block_ids)
        present = set(
            name for name in os.listdir(BP_BLOCKS) if name.endswith(".json")
        )
        self.assertEqual(present - known, set(), "存在定义表之外的方块 JSON")

    def test_identifier_matches_file_name(self):
        for device_id in self.block_ids:
            payload = self.read_block_json(device_id)
            identifier = payload["minecraft:block"]["description"]["identifier"]
            self.assertEqual(identifier, device_id, block_file_name(device_id))

    def test_required_components_present(self):
        """缺少 block_entity 会没有方块实体数据；缺 listen_block_remove 会收不到拆除事件。"""
        for device_id in self.block_ids:
            components = self.read_block_json(device_id)["minecraft:block"]["components"]
            for required in REQUIRED_COMPONENTS:
                self.assertIn(required, components, "%s 缺少 %s" % (device_id, required))

    def test_block_remove_listener_is_enabled(self):
        for device_id in self.block_ids:
            components = self.read_block_json(device_id)["minecraft:block"]["components"]
            self.assertTrue(
                components["netease:listen_block_remove"].get("value"),
                "%s 的移除事件白名单未开启" % (device_id,),
            )

    def test_ticking_matches_definition_expectations(self):
        """需要被动推进的设备必须开 tick，否则它永远不会被推进。"""
        expected_ticking = (
            "endernet:tiny_network_endpoint",
            "endernet:dos_network_endpoint",
            "endernet:assemble_chamber",
            "endernet:parallel_processor",
            "endernet:energy_input_interface",
        )
        for device_id in expected_ticking:
            components = self.read_block_json(device_id)["minecraft:block"]["components"]
            self.assertTrue(
                components["netease:block_entity"].get("tick"),
                "%s 应该开启 tick" % (device_id,),
            )

    def test_baseband_does_not_tick(self):
        """基带靠事件驱动，不该逐 tick 扫描。"""
        for device_id in (
            "endernet:network_baseband_8",
            "endernet:network_baseband_16",
            "endernet:network_baseband_32",
        ):
            components = self.read_block_json(device_id)["minecraft:block"]["components"]
            self.assertFalse(components["netease:block_entity"].get("tick"), device_id)

    def test_textures_component_points_at_terrain_key(self):
        for device_id in self.block_ids:
            components = self.read_block_json(device_id)["minecraft:block"]["components"]
            texture = components["minecraft:material_instances"]["*"]["texture"]
            self.assertIn(
                texture,
                self.terrain_data,
                "%s 引用了未登记的地形贴图 %s" % (device_id, texture),
            )


class TestResourcePackMapping(AssetFixture):
    def test_blocks_json_covers_every_device(self):
        missing = [d for d in self.block_ids if d not in self.blocks_json]
        self.assertEqual(missing, [], "blocks.json 缺少：%r" % (missing,))

    def test_blocks_json_texture_keys_are_registered(self):
        for device_id in self.block_ids:
            key = self.blocks_json[device_id]["textures"]
            self.assertIsNotNone(key, "%s 的贴图为 null，游戏内会显示为缺失贴图" % device_id)
            self.assertIn(key, self.terrain_data, "%s -> %s 未登记" % (device_id, key))

    def test_terrain_entries_have_texture_paths(self):
        for key, entry in self.terrain_data.items():
            self.assertIn("textures", entry, key)
            self.assertTrue(entry["textures"], key)

    def test_every_referenced_texture_file_exists(self):
        """贴图路径必须真的落到文件上，否则游戏里是紫黑块。"""
        missing = []
        for key, entry in self.terrain_data.items():
            value = entry["textures"]
            paths = value if isinstance(value, list) else [value]
            for path in paths:
                full = os.path.join(RP_ROOT, path.replace("/", os.sep) + ".png")
                if not os.path.isfile(full):
                    missing.append((key, path))
        self.assertEqual(missing, [], "地形贴图缺少文件：%r" % (missing,))

    def test_no_unreferenced_device_textures(self):
        """设备贴图目录里不应有没人引用的散图。"""
        used = set()
        for entry in self.terrain_data.values():
            value = entry["textures"]
            paths = value if isinstance(value, list) else [value]
            for path in paths:
                if path.startswith("textures/endernet/devices/"):
                    used.add(os.path.basename(path) + ".png")
        present = set(
            name for name in os.listdir(TEXTURE_DIR) if name.endswith(".png")
        )
        self.assertEqual(present - used, set(), "存在未被引用的设备贴图")

    def test_json_files_are_valid_utf8(self):
        for path in (RP_BLOCKS, RP_TERRAIN):
            with io.open(path, "r", encoding="utf-8") as handle:
                json.load(handle)


class TestLocalisation(AssetFixture):
    """方块在游戏里要有中文名，否则创造栏里显示的是标识符。"""

    @classmethod
    def setUpClass(cls):
        AssetFixture.setUpClass()
        path = os.path.join(RP_ROOT, "texts", "zh_CN.lang")
        cls.lang = {}
        with io.open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                cls.lang[key.strip()] = value.strip()

    def test_every_block_has_a_display_name(self):
        missing = [
            device_id
            for device_id in self.block_ids
            if ("tile.%s.name" % device_id) not in self.lang
        ]
        self.assertEqual(missing, [], "缺少中文名：%r" % (missing,))

    def test_display_names_are_not_identifiers(self):
        for device_id in self.block_ids:
            name = self.lang["tile.%s.name" % device_id]
            self.assertTrue(name, device_id)
            self.assertNotEqual(name, device_id, "%s 的名字没有本地化" % device_id)

    def test_no_orphan_lang_entries(self):
        known = set("tile.%s.name" % d for d in self.block_ids)
        orphans = set(
            key for key in self.lang
            if key.startswith("tile.endernet:") and key not in known
        )
        self.assertEqual(orphans, set(), "存在已删除方块的语言条目")


class TestCreativeTab(AssetFixture):
    def test_category_matches_creative_tab_config(self):
        """方块分类必须与创造栏配置的 name 一致，否则方块不会出现在该分页下。"""
        tab_path = os.path.join(REPO, "endernet_bp", "netease_tab",
                                "endernet_category_config.json")
        tab = read_json(tab_path)
        names = set(category["name"] for category in tab["category"])
        for device_id in self.block_ids:
            payload = self.read_block_json(device_id)
            category = payload["minecraft:block"]["description"]["category"]
            self.assertIn(
                category, names,
                "%s 的分类 %s 不在创造栏配置里" % (device_id, category),
            )


if __name__ == "__main__":
    unittest.main()
