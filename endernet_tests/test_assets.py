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

from endernet.common.definitions import STORAGE_DRIVER_SLOT_COUNT, DefinitionSet

REPO = testsupport.REPO_ROOT
BP_BLOCKS = os.path.join(REPO, "endernet_bp", "blocks")
RP_ROOT = os.path.join(REPO, "endernet_rp")
RP_BLOCKS = os.path.join(RP_ROOT, "blocks.json")
RP_TERRAIN = os.path.join(RP_ROOT, "textures", "terrain_texture.json")
TEXTURE_DIR = os.path.join(RP_ROOT, "textures", "endernet", "devices")
BP_ITEMS = os.path.join(REPO, "endernet_bp", "netease_items_beh")
RP_ITEMS = os.path.join(RP_ROOT, "netease_items_res")
RP_ITEM_TEXTURE = os.path.join(RP_ROOT, "textures", "item_texture.json")
ITEM_TEXTURE_DIR = os.path.join(RP_ROOT, "textures", "items")
RP_UI = os.path.join(RP_ROOT, "ui")
DRIVER = "endernet:storage_driver"

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


def item_file_name(medium_id):
    """与 scripts/gen_items.py 保持一致。"""
    return medium_id.replace(":", "_") + ".json"


def item_texture_name(medium_id):
    """与 scripts/gen_items.py 保持一致：`endernet_<短名>`。"""
    return "endernet_" + medium_id.split(":", 1)[1]


class AssetFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definitions = DefinitionSet()
        cls.blocks_json = read_json(RP_BLOCKS)
        cls.terrain = read_json(RP_TERRAIN)
        cls.terrain_data = cls.terrain.get("texture_data", {})
        cls.block_ids = sorted(cls.definitions.devices)
        cls.item_texture = read_json(RP_ITEM_TEXTURE)
        cls.item_texture_data = cls.item_texture.get("texture_data", {})
        cls.item_ids = list(cls.definitions.item_mediums)

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


class TestStorageDriverContainer(AssetFixture):
    """驱动器是一个 10 格的原生容器：组件、槽数与屏幕名三者必须对得上。

    这里只验证「资源声明正确」，**不验证它在游戏里真的能打开**——那件事
    只能在真机上定论，属交付说明的未验证项。
    """

    def components(self, device_id=DRIVER):
        return self.read_block_json(device_id)["minecraft:block"]["components"]

    def test_storage_driver_declares_a_container(self):
        self.assertIn("netease:block_container", self.components())

    def test_container_size_matches_the_domain_definition(self):
        """界面格数、领域层槽数、方块容器格数必须是同一个数。

        分成三份的话会出现「界面上能放 10 格、领域层只认 8 格」，
        而那种错误在离线测试里不会有任何征兆。
        """
        container = self.components()["netease:block_container"]
        self.assertEqual(container["container_size"], STORAGE_DRIVER_SLOT_COUNT.value)

    def test_container_requires_the_netease_container_base_block(self):
        """声明了容器组件却不设 base_block，引擎不会按自定义容器处理它。"""
        description = self.read_block_json(DRIVER)["minecraft:block"]["description"]
        self.assertEqual(description.get("base_block"), "netease_container")

    def test_screen_name_resolves_to_a_real_screen(self):
        """`screen_name` 形如 `namespace.screenName`，两边都得真的存在。

        写错不会报错，只会让玩家右键时打开一个空界面。

        **不能假设界面文件名等于命名空间**：`storage_driver.json` 的命名空间是
        `storage_driver_ui`（原版同样如此，`NeteaseContainerUI.json` 的命名空间是
        `netease_container`）。因此按 `_ui_defs.json` 登记的清单逐个找命名空间。
        """
        screen_name = self.components()["netease:block_container"]["screen_name"]
        namespace, _, screen = screen_name.partition(".")
        self.assertTrue(
            namespace and screen, "screen_name 应形如 namespace.screen：%r" % screen_name
        )

        registry = read_json(os.path.join(RP_UI, "_ui_defs.json"))
        found = None
        for relative in registry["ui_defs"]:
            path = os.path.join(RP_ROOT, relative.replace("/", os.sep))
            payload = read_json(path)
            if payload.get("namespace") == namespace:
                found = (path, payload)
                break
        self.assertIsNotNone(found, "没有命名空间为 %s 的界面文件" % namespace)
        path, payload = found
        # 屏幕可以在定义处写 `main@common.inventory_screen_common` 继承一个原型，
        # 而外部引用只写 `namespace.main`。因此前缀匹配才是正确的判定。
        names = set(payload)
        self.assertTrue(
            screen in names or any(name.startswith(screen + "@") for name in names),
            "%s 里没有名为 %s 的屏幕（现有：%s）"
            % (path, screen, ", ".join(sorted(n for n in names if not n.startswith("_"))[:8])),
        )

    def test_only_the_storage_driver_is_a_container(self):
        """别的设备不该顺手获得容器组件——那会让它们变成可开箱的方块。"""
        others = [d for d in self.block_ids if d != DRIVER]
        for device_id in others:
            components = self.components(device_id)
            self.assertNotIn("netease:block_container", components, device_id)
            description = self.read_block_json(device_id)["minecraft:block"]["description"]
            self.assertNotIn("base_block", description, device_id)


class TestItemResources(AssetFixture):
    """介质物品的行为包定义、资源包定义、图集与贴图文件必须四者齐全。"""

    def test_every_item_medium_has_behavior_and_resource_json(self):
        missing = []
        for medium_id in self.item_ids:
            name = item_file_name(medium_id)
            for directory in (BP_ITEMS, RP_ITEMS):
                if not os.path.isfile(os.path.join(directory, name)):
                    missing.append(os.path.join(directory, name))
        self.assertEqual(missing, [], "缺少物品 JSON：%r" % (missing,))

    def test_no_orphan_item_json(self):
        known = set(item_file_name(m) for m in self.item_ids)
        for directory in (BP_ITEMS, RP_ITEMS):
            present = set(n for n in os.listdir(directory) if n.endswith(".json"))
            self.assertEqual(present - known, set(), "%s 存在定义表之外的物品 JSON" % directory)

    def test_item_identifiers_match_file_names(self):
        for medium_id in self.item_ids:
            for directory in (BP_ITEMS, RP_ITEMS):
                payload = read_json(os.path.join(directory, item_file_name(medium_id)))
                identifier = payload["minecraft:item"]["description"]["identifier"]
                self.assertEqual(identifier, medium_id)

    def test_medium_items_do_not_stack(self):
        """介质要携带自己的身份与内容，一叠 64 个会共享同一份数据。

        堆叠上限必须保持 1；改成别的值会让「拔出的是哪一块盘」无从确定。
        """
        for medium_id in self.item_ids:
            payload = read_json(os.path.join(BP_ITEMS, item_file_name(medium_id)))
            self.assertEqual(
                payload["minecraft:item"]["components"]["minecraft:max_stack_size"], 1
            )

    def test_resource_json_has_no_category(self):
        """资源侧不写 category，与行为侧的分工不同。"""
        for medium_id in self.item_ids:
            payload = read_json(os.path.join(RP_ITEMS, item_file_name(medium_id)))
            self.assertNotIn("category", payload["minecraft:item"]["description"])

    def test_item_icon_points_at_a_registered_atlas_key(self):
        for medium_id in self.item_ids:
            payload = read_json(os.path.join(RP_ITEMS, item_file_name(medium_id)))
            icon = payload["minecraft:item"]["components"]["minecraft:icon"]
            self.assertIn(icon, self.item_texture_data, medium_id)
            self.assertEqual(icon, medium_id)

    def test_atlas_has_no_orphan_entries(self):
        known = set(self.item_ids)
        self.assertEqual(set(self.item_texture_data) - known, set(), "物品图集存在多余条目")

    def test_every_atlas_texture_file_exists(self):
        missing = []
        for key, entry in self.item_texture_data.items():
            path = os.path.join(RP_ROOT, entry["textures"].replace("/", os.sep) + ".png")
            if not os.path.isfile(path):
                missing.append((key, entry["textures"]))
        self.assertEqual(missing, [], "物品图集缺少贴图文件：%r" % (missing,))

    def test_no_unreferenced_item_textures(self):
        used = set(
            os.path.basename(entry["textures"]) + ".png"
            for entry in self.item_texture_data.values()
        )
        present = set(n for n in os.listdir(ITEM_TEXTURE_DIR) if n.endswith(".png"))
        self.assertEqual(present - used, set(), "存在未被引用的物品贴图")

    def test_item_category_matches_creative_tab_config(self):
        tab_path = os.path.join(
            REPO, "endernet_bp", "netease_tab", "endernet_category_config.json"
        )
        names = set(category["name"] for category in read_json(tab_path)["category"])
        for medium_id in self.item_ids:
            payload = read_json(os.path.join(BP_ITEMS, item_file_name(medium_id)))
            category = payload["minecraft:item"]["description"]["category"]
            self.assertIn(category, names, "%s 的分类 %s 不在创造栏配置里" % (medium_id, category))


class TestItemLocalisation(AssetFixture):
    """介质物品在游戏里要有中文名。"""

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

    def test_every_item_medium_has_a_display_name(self):
        missing = [
            medium_id
            for medium_id in self.item_ids
            if ("item.%s.name" % medium_id) not in self.lang
        ]
        self.assertEqual(missing, [], "缺少中文名：%r" % (missing,))

    def test_display_name_matches_the_definition_label(self):
        """语言文件里的名字要与定义表的 label 一致，不另起一套叫法。"""
        for spec in self.definitions.item_medium_specs():
            self.assertEqual(self.lang["item.%s.name" % spec.id], spec.label, spec.id)

    def test_no_orphan_item_lang_entries(self):
        known = set("item.%s.name" % m for m in self.item_ids)
        orphans = set(
            key
            for key in self.lang
            if key.startswith("item.endernet:") and key not in known
        )
        self.assertEqual(orphans, set(), "存在已删除介质物品的语言条目")


if __name__ == "__main__":
    unittest.main()
