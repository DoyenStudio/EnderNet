# coding=utf-8
"""资源一致性：方块 JSON、资源包映射、贴图文件与设备定义表必须对得上。

这些检查在离线状态下完全可做，而且正是最容易悄悄坏掉的地方——
新增一个设备却忘了加方块 JSON，或者贴图键拼错导致游戏里显示紫黑块，
都不会有任何测试失败，直到进游戏才发现。
"""

import io
import json
import os
import sys
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


def item_file_name(item_id):
    """与 scripts/gen_items.py 保持一致。"""
    return item_id.replace(":", "_") + ".json"


def item_texture_name(item_id):
    """与 scripts/gen_items.py 保持一致：`endernet_<短名>`。"""
    return "endernet_" + item_id.split(":", 1)[1]


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
        # **两类物品一起查**（介质 + 样板）。分成两份清单的代价是新增一类
        # 时总有一处漏掉，而漏掉的表现是「游戏里有这件物品、但没人检查它的
        # 贴图与语言」——一个只在真机上才看得见的空洞。
        cls.item_ids = list(cls.definitions.item_keys())
        cls.item_specs = cls.definitions.item_specs()

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


def declared_containers():
    """`gen_blocks.CONTAINERS` 的读数。

    **不另抄一份清单。** 生成器里那张表是唯一的声明处，测试从它派生——
    两边各写一份的话，新增一个容器方块时测试会照样通过，而它本来要防的
    正是「声明与产物对不上」。
    """
    scripts_dir = os.path.join(REPO, "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import gen_blocks

    return gen_blocks.CONTAINERS


#: 引擎的容器集合名。**只写死在公共模板 `container_grid_items` 里**
#: （`endernet_container_common.json:128`），各设备界面靠继承拿到它。
ENGINE_CONTAINER_COLLECTION = "netease_container"

#: 渲染该集合的两种模板名：网格模板与逐槽模板。逐槽那种**连模板都不写集合名**，
#: 靠 `$item_collection_name` 的默认值绑定（同文件 `:118`）。
#:
#: 短名与全名都收：控件键写成 `X@ns.template`，两侧各自可能被省略。
ENGINE_CONTAINER_TEMPLATES = frozenset(
    [
        "container_grid_items",
        "container_grid_item",
        "endernet_container_common.container_grid_items",
        "endernet_container_common.container_grid_item",
    ]
)


def _iter_dicts(node, out):
    """收集 payload 中全部 dict 节点（键与列表都下探）。"""
    if isinstance(node, dict):
        out.append(node)
        for value in node.values():
            _iter_dicts(value, out)
    elif isinstance(node, list):
        for value in node:
            _iter_dicts(value, out)


def _instantiates_engine_container_template(key):
    """`X@ns.template` 形式的控件键是否指向引擎容器集合的模板。"""
    local, _, template = key.partition("@")
    return (
        local in ENGINE_CONTAINER_TEMPLATES or template in ENGINE_CONTAINER_TEMPLATES
    )


def screens_rendering_the_engine_container():
    """`命名空间 -> 界面文件名`：哪些界面画了引擎的 `netease_container` 集合。

    **不能靠 grep `collection_name`。** 集合名全仓只出现在公共模板
    `endernet_container_common.json:128` 一处，设备界面一律继承它；逐槽写法
    更是连模板都不写集合名。全仓只有 2 个界面自己写了 `collection_name`
    （`bus_item_io.json:146` 的编辑器残留 `test_grid` 与
    `assemble_chamber.json:108` 的 `source_items`），**两个都不是**这个集合——
    只看字面量会得出与事实相反的结论。

    只算「界面」：公共模板文件没有 `main` 屏幕，不计入。
    """
    drawn = {}
    for name in sorted(os.listdir(RP_UI)):
        if not name.endswith(".json"):
            continue
        payload = read_json(os.path.join(RP_UI, name))
        namespace = payload.get("namespace")
        if not namespace:
            continue
        if not any(key == "main" or key.startswith("main@") for key in payload):
            continue
        nodes = []
        _iter_dicts(payload, nodes)
        for node in nodes:
            if node.get("collection_name") == ENGINE_CONTAINER_COLLECTION:
                drawn[namespace] = name
                break
            if any(_instantiates_engine_container_template(key) for key in node):
                drawn[namespace] = name
                break
    return drawn


#: 声明了容器组件、但界面上**刻意不画**引擎集合的设备。
#:
#: 只有两个流体口。它们挂的是 User 2026-09-29 指定的**占位**容器：
#: 1 格，装的是**物品**，与流体无关（引擎根本没有流体容器，真实流体适配属
#: 13-03）。界面上不画那个集合是刻意的——画出来玩家会看见一个和流体毫无
#: 关系的物品格，比不画更糟。由
#: `test_fluid_port_containers_stay_hidden_from_the_screen` 单独钉住。
UI_HIDDEN_CONTAINERS = frozenset(
    ["endernet:bus_fluid_input_io", "endernet:bus_fluid_output_io"]
)


def declared_container_namespaces():
    """声明了容器组件的设备所用**全部**界面的命名空间（含刻意隐藏的两个）。"""
    return set(screen.partition(".")[0] for screen, _ in declared_containers().values())


def visible_container_namespaces():
    """声明了容器组件、且界面上**应该**画出集合的那些界面命名空间。

    即从 `declared_container_namespaces()` 里去掉 `UI_HIDDEN_CONTAINERS`。
    """
    return set(
        screen.partition(".")[0]
        for device_id, (screen, _) in declared_containers().items()
        if device_id not in UI_HIDDEN_CONTAINERS
    )


class TestBlockContainers(AssetFixture):
    """声明了 `netease:block_container` 的方块：组件、槽数与屏幕名三者必须对得上。

    这里只验证「资源声明正确」，**不验证它在游戏里真的能打开**——那件事
    只能在真机上定论，属交付说明的未验证项。
    """

    def components(self, device_id):
        return self.read_block_json(device_id)["minecraft:block"]["components"]

    def container_of(self, device_id):
        return self.components(device_id)["netease:block_container"]

    def test_container_set_matches_the_generator(self):
        """**容器方块的集合必须恰好是生成器声明的那几个。**

        两个方向都要查：产物多了一个（懒得改生成器、手改了 JSON）会让一台
        设备莫名其妙变成可开箱的方块；少了一个则是容器压进方块却没生效，
        表现为「外部物流放进去的东西不见了」。
        """
        declared = set(declared_containers())
        present = set(
            device_id
            for device_id in self.block_ids
            if "netease:block_container" in self.components(device_id)
        )
        self.assertEqual(present, declared)

    def test_container_size_matches_the_domain_definition(self):
        """界面格数、领域层槽数、方块容器格数必须是同一个数。

        分成三份的话会出现「界面上能放 10 格、领域层只认 8 格」，
        而那种错误在离线测试里不会有任何征兆。
        """
        for device_id, (_, size_value) in sorted(declared_containers().items()):
            self.assertEqual(
                self.container_of(device_id)["container_size"],
                size_value.value,
                device_id,
            )

    def test_container_requires_the_netease_container_base_block(self):
        """声明了容器组件却不设 base_block，引擎不会按自定义容器处理它。"""
        for device_id in sorted(declared_containers()):
            description = self.read_block_json(device_id)["minecraft:block"]["description"]
            self.assertEqual(description.get("base_block"), "netease_container", device_id)

    def test_the_storage_driver_is_still_a_container(self):
        """**独立于生成器的断言。** 其余几条都从 `gen_blocks.CONTAINERS`
        派生期望值，因此把驱动器那一项从生成器里删掉之后它们照样全绿——
        而驱动器必须是容器（05 的介质槽位就存在那个方块容器里）。
        这里把这条**钉死在测试里**，不让它跟着生成器一起漂走。
        """
        self.assertIn("netease:block_container", self.components(DRIVER))
        self.assertEqual(
            self.container_of(DRIVER)["container_size"],
            STORAGE_DRIVER_SLOT_COUNT.value,
        )

    def test_every_declared_container_is_a_known_device(self):
        for device_id in sorted(declared_containers()):
            self.assertIn(device_id, self.definitions.devices)

    def test_non_container_blocks_have_neither_component_nor_base_block(self):
        """没声明容器的设备不该顺手获得容器组件——那会让它们变成可开箱的方块。"""
        for device_id in self.block_ids:
            if device_id in declared_containers():
                continue
            self.assertNotIn(
                "netease:block_container", self.components(device_id), device_id
            )
            description = self.read_block_json(device_id)["minecraft:block"]["description"]
            self.assertNotIn("base_block", description, device_id)

    def test_screen_name_resolves_to_a_real_screen(self):
        """`screen_name` 形如 `namespace.screenName`，两边都得真的存在。

        写错不会报错，只会让玩家右键时打开一个空界面。

        **不能假设界面文件名等于命名空间**：`storage_driver.json` 的命名空间是
        `storage_driver_ui`（原版同样如此，`NeteaseContainerUI.json` 的命名空间是
        `netease_container`）。因此按 `_ui_defs.json` 登记的清单逐个找命名空间。
        """
        registry = read_json(os.path.join(RP_UI, "_ui_defs.json"))
        screens = {}
        for relative in registry["ui_defs"]:
            path = os.path.join(RP_ROOT, relative.replace("/", os.sep))
            payload = read_json(path)
            namespace = payload.get("namespace")
            if namespace:
                screens[namespace] = (path, payload)

        for device_id, (screen_name, _) in sorted(declared_containers().items()):
            namespace, _, screen = screen_name.partition(".")
            self.assertTrue(
                namespace and screen,
                "%s 的 screen_name 应形如 namespace.screen：%r" % (device_id, screen_name),
            )
            found = screens.get(namespace)
            self.assertIsNotNone(
                found, "没有命名空间为 %s 的界面文件（%s）" % (namespace, device_id)
            )
            path, payload = found
            # 屏幕可以在定义处写 `main@common.inventory_screen_common` 继承原型，
            # 而外部引用只写 `namespace.main`。因此前缀匹配才是正确的判定。
            names = set(payload)
            self.assertTrue(
                screen in names or any(name.startswith(screen + "@") for name in names),
                "%s 里没有名为 %s 的屏幕（现有：%s）"
                % (
                    path,
                    screen,
                    ", ".join(sorted(n for n in names if not n.startswith("_"))[:8]),
                ),
            )

    def test_container_screens_actually_render_the_engine_collection(self):
        """声明了容器组件的方块，其界面必须真的画了 `netease_container` 集合。

        这条是**真不变量**：容器组件让引擎把方块实体的数据填进那个集合，
        界面不画它，这批数据在界面上就没有出口——表现是「箱子里有东西，
        打开却是空界面」，而离线门禁本来对此毫无征兆。上面几条只比对
        组件与领域层的数字，管不到界面画了什么。

        **例外只有两个流体口**（`UI_HIDDEN_CONTAINERS`）：它们的容器是占位，
        界面上刻意不画；那两台由下面那条**反向**断言单独管，不在这里放行后
        就不管了。

        注意本断言**只查画没画，不查格数**。2026-09-29 修掉的那次分叉
        （存货口界面 5 格／文档 9 格）正是从这个缺口漏过去的。
        """
        missing = visible_container_namespaces() - set(
            screens_rendering_the_engine_container()
        )
        self.assertEqual(
            missing,
            set(),
            "以下界面声明了容器组件却没画 %s 集合：%s"
            % (ENGINE_CONTAINER_COLLECTION, sorted(missing)),
        )

    def test_fluid_port_containers_stay_hidden_from_the_screen(self):
        """两个流体口的占位容器**必须不出现在界面上**。

        这是 User 2026-09-29 明确要求的「界面内隐藏」。理由不是审美：那 1 格
        是**物品**容器（引擎没有流体容器），真画到流体界面上，玩家会看见一个
        与流体毫无关系、又能真放东西的物品格——比没有更糟。

        与上一条相反，这里钉的是**反向**不变量：谁把那两台界面的
        `netease_container` 集合画上去，都会在此被拦下，去面对「流体口的容器
        到底该不该可见」这个仍属 13-03 的问题。
        """
        drawn = screens_rendering_the_engine_container()
        self.assertTrue(UI_HIDDEN_CONTAINERS.issubset(set(declared_containers())))
        for device_id in sorted(UI_HIDDEN_CONTAINERS):
            screen_name, _ = declared_containers()[device_id]
            namespace = screen_name.partition(".")[0]
            self.assertNotIn(
                namespace,
                drawn,
                "%s 的容器是刻意隐藏的，界面 %s 不该画 %s 集合"
                % (device_id, namespace, ENGINE_CONTAINER_COLLECTION),
            )

    def test_screens_rendering_the_engine_collection_without_a_container_block(self):
        """**画了引擎集合却没有容器方块**的界面：今天必须是空的。

        2026-09-29 之前这里钉着三个界面（存货口／取货口／内存槽）——它们画了
        `netease_container` 集合却没有容器组件，那些格子（5／5／10 格）因此
        没有任何权威数据源。User 当天定案「不是终端类方块且能存储物品且有 UI
        有物品槽的都要有网易容器」，三台都补上了组件，差异清零。

        断言留在 `== set()` 而不是删掉：**下一次**再出现「界面画了集合、方块
        没有容器」时，这里会立刻拦下。给方块加容器会一并改变 06/12 的语义
        （方块实体从此有 `Items`、外部模组可存取），属玩法定案，不由测试替选。
        """
        self.assertEqual(
            set(screens_rendering_the_engine_container())
            - declared_container_namespaces(),
            set(),
        )


class TestItemResources(AssetFixture):
    """物品（介质与样板）的行为包定义、资源包定义、图集与贴图必须四者齐全。"""

    def test_every_item_has_behavior_and_resource_json(self):
        missing = []
        for item_id in self.item_ids:
            name = item_file_name(item_id)
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
        for item_id in self.item_ids:
            for directory in (BP_ITEMS, RP_ITEMS):
                payload = read_json(os.path.join(directory, item_file_name(item_id)))
                identifier = payload["minecraft:item"]["description"]["identifier"]
                self.assertEqual(identifier, item_id)

    def test_items_do_not_stack(self):
        """**堆叠上限必须是 1，两类物品各自的理由都成立。**

        - 介质要携带自己的身份与内容，一叠 64 个会共享同一份物品数据，
          拔出其中一个就无法确定它是哪一块盘。
        - 样板把配方编码在自己的 `userData` 里，一叠 64 块只会剩下其中
          一块的配方，而玩家看到的是一叠——**这一条比介质那条更硬**：
          样板没有世界侧的记录可以兜底。
        """
        for item_id in self.item_ids:
            payload = read_json(os.path.join(BP_ITEMS, item_file_name(item_id)))
            self.assertEqual(
                payload["minecraft:item"]["components"]["minecraft:max_stack_size"],
                1,
                item_id,
            )

    def test_items_carry_a_tip(self):
        """物品说明里要有内容：样板是塞在容器里的实物，玩家只能靠悬停分辨。"""
        for item_id in self.item_ids:
            payload = read_json(os.path.join(BP_ITEMS, item_file_name(item_id)))
            tip = payload["minecraft:item"]["components"]["netease:customtips"]["value"]
            self.assertTrue(tip.strip(), item_id)

    def test_resource_json_has_no_category(self):
        """资源侧不写 category，与行为侧的分工不同。"""
        for item_id in self.item_ids:
            payload = read_json(os.path.join(RP_ITEMS, item_file_name(item_id)))
            self.assertNotIn("category", payload["minecraft:item"]["description"])

    def test_item_icon_points_at_a_registered_atlas_key(self):
        for item_id in self.item_ids:
            payload = read_json(os.path.join(RP_ITEMS, item_file_name(item_id)))
            icon = payload["minecraft:item"]["components"]["minecraft:icon"]
            self.assertIn(icon, self.item_texture_data, item_id)
            self.assertEqual(icon, item_id)

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
        for item_id in self.item_ids:
            payload = read_json(os.path.join(BP_ITEMS, item_file_name(item_id)))
            category = payload["minecraft:item"]["description"]["category"]
            self.assertIn(category, names, "%s 的分类 %s 不在创造栏配置里" % (item_id, category))


class TestItemLocalisation(AssetFixture):
    """物品在游戏里要有中文名。"""

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

    def test_every_item_has_a_display_name(self):
        missing = [
            item_id
            for item_id in self.item_ids
            if ("item.%s.name" % item_id) not in self.lang
        ]
        self.assertEqual(missing, [], "缺少中文名：%r" % (missing,))

    def test_display_name_matches_the_definition_label(self):
        """语言文件里的名字要与定义表的 label 一致，不另起一套叫法。"""
        for spec in self.item_specs:
            self.assertEqual(self.lang["item.%s.name" % spec.id], spec.label, spec.id)

    def test_no_orphan_item_lang_entries(self):
        known = set("item.%s.name" % m for m in self.item_ids)
        orphans = set(
            key
            for key in self.lang
            if key.startswith("item.endernet:") and key not in known
        )
        self.assertEqual(orphans, set(), "存在已删除物品的语言条目")


if __name__ == "__main__":
    unittest.main()
