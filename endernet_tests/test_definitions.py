# coding=utf-8
"""01-03 验收：设备、介质与连接定义表逐项对应 blocks.md / items.md。

重点验证两件事：
1. 数值确实与文档一致（逐条核对，而不是「有个表就行」）。
2. 未定玩法没有被悄悄定案——`assert_no_silent_unresolved()` 真的会拦下。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import (
    BandwidthSpec,
    CAP_ITEM_PORT,
    CAP_PATTERN_PROVIDER,
    CAP_STORAGE_PROVIDER,
    CAP_TERMINAL_ENDPOINT,
    CATEGORY_BASEBAND,
    CATEGORY_BUS,
    CATEGORY_CONTROLLER,
    CATEGORY_ENERGY_INPUT,
    CATEGORY_PROCESSOR,
    CATEGORY_PROCESSOR_CONNECTOR,
    CATEGORY_STORAGE,
    CATEGORY_TERMINAL,
    DEVICES,
    DefinitionSet,
    DeviceDefinition,
    EnergySpec,
    MEDIUMS,
    PATTERN_MODES,
    PATTERN_TEMPLATES,
    PER_DISK_ENERGY,
    PER_MEMORY_ENERGY,
    UNCONFIGURED,
    Value,
    ConnectionRules,
    TemplateSpec,
    devices_for_mode,
)

#: blocks.md 中「网络控制器」分组下的全部方块
DOCUMENTED_CONTROLLERS = {
    "endernet:dos_network_endpoint",
    "endernet:tiny_network_endpoint",
}

#: blocks.md 列出的全部 23 个方块
DOCUMENTED_BLOCKS = {
    "endernet:dos_network_endpoint",
    "endernet:tiny_network_endpoint",
    "endernet:network_baseband_8",
    "endernet:network_baseband_16",
    "endernet:network_baseband_32",
    "endernet:bus_item_io",
    "endernet:bus_fluid_io",
    "endernet:bus_item_output_io",
    "endernet:bus_fluid_output_io",
    "endernet:bus_item_input_io",
    "endernet:bus_fluid_input_io",
    "endernet:bus_template_io",
    "endernet:storage_driver",
    "endernet:common_terminal_panel",
    "endernet:crafting_terminal_panel",
    "endernet:template_terminal_panel",
    "endernet:factoric_terminal_panel",
    "endernet:parallel_processor_connector",
    "endernet:parallel_processor",
    "endernet:memory_slot",
    "endernet:assemble_chamber",
    "endernet:energy_input_interface",
    "endernet:creative_energy_source",
}


class TestUnconfiguredSentinel(unittest.TestCase):
    """UNCONFIGURED 必须与 0 严格区分。"""

    def test_not_equal_to_zero(self):
        self.assertIsNot(UNCONFIGURED, 0)
        self.assertNotEqual(UNCONFIGURED, 0)

    def test_truthiness_raises(self):
        """禁止 `if spec.energy:` 这类写法把缺项当成免费。"""
        with self.assertRaises(TypeError):
            bool(UNCONFIGURED)


class TestValueDiscipline(unittest.TestCase):
    """未定数值的三种状态必须可区分。"""

    def test_concrete_value_is_settled(self):
        self.assertTrue(Value(4).is_settled())

    def test_unconfigured_value_is_not_settled(self):
        self.assertFalse(Value(UNCONFIGURED).is_settled())
        self.assertFalse(Value(UNCONFIGURED).is_configured())

    def test_marked_test_value_is_not_settled(self):
        """带未定标签的就是未定案，哪怕它是测试替身。"""
        test_value = Value(30, unresolved="R08", is_test_value=True)
        self.assertFalse(test_value.is_settled())

    def test_silent_resolution_detected(self):
        """带未定标签、却不是测试值的条目会被点名。"""
        defs = DefinitionSet(
            devices=DEVICES,
            config={"rf_per_en": Value(4, unresolved="R08", is_test_value=False)},
        )
        with self.assertRaises(AssertionError):
            defs.assert_no_silent_unresolved()

    def test_marked_test_config_passes_guard(self):
        defs = DefinitionSet(
            devices=DEVICES,
            config={"rf_per_en": Value(4, unresolved="R08", is_test_value=True)},
        )
        defs.assert_no_silent_unresolved()

    def test_default_definition_set_passes_guard(self):
        """当前定义表不得包含任何悄悄定案的玩法数值。"""
        DefinitionSet().assert_no_silent_unresolved()


class TestDeviceCoverage(unittest.TestCase):
    """23 个方块逐项对应，不多不少。"""

    def setUp(self):
        self.defs = DefinitionSet()

    def test_all_documented_blocks_present(self):
        self.assertEqual(set(self.defs.devices.keys()), DOCUMENTED_BLOCKS)

    def test_no_duplicate_ids(self):
        self.assertEqual(len(DEVICES), len(set(d.id for d in DEVICES)))

    def test_no_duplicate_medium_ids(self):
        self.assertEqual(len(MEDIUMS), len(set(m.id for m in MEDIUMS)))


class TestControllerTypeSet(unittest.TestCase):
    """控制器按显式分类计数，不按名称猜测。"""

    def test_controller_ids_match_document(self):
        self.assertEqual(DefinitionSet().controller_ids, DOCUMENTED_CONTROLLERS)

    def test_is_controller(self):
        defs = DefinitionSet()
        self.assertTrue(defs.is_controller("endernet:tiny_network_endpoint"))
        self.assertTrue(defs.is_controller("endernet:dos_network_endpoint"))
        self.assertFalse(defs.is_controller("endernet:network_baseband_8"))

    def test_new_controller_type_joins_group_automatically(self):
        """新增一个分类为 controller 的类型，自动参与分组计数校验。"""
        extra = type(DEVICES[0])(
            "endernet:test_endpoint",
            "测试控制器",
            CATEGORY_CONTROLLER,
        )
        defs = DefinitionSet(devices=DEVICES + (extra,))
        self.assertIn("endernet:test_endpoint", defs.controller_ids)
        self.assertEqual(len(defs.controller_ids), 3)

    def test_name_containing_controller_but_wrong_category_is_not_a_controller(self):
        """名字里有 controller 但分类不对，不得被算作控制器。"""
        impostor = type(DEVICES[0])(
            "endernet:not_a_controller_block",
            "冒名方块",
            CATEGORY_BASEBAND,
        )
        defs = DefinitionSet(devices=(impostor,))
        self.assertEqual(defs.controller_ids, frozenset())


class TestBandwidthNumbers(unittest.TestCase):
    """带宽数值逐条对应 blocks.md，以四分之一 u 整数记账。"""

    def setUp(self):
        self.defs = DefinitionSet()

    def demand(self, device_id, disks=0, memories=0):
        return self.defs.get(device_id).bandwidth.demand_quarters(disks, memories)

    def test_basebands_hold_8_16_32_units(self):
        self.assertEqual(self.defs.get("endernet:network_baseband_8").notes.count("8u"), 1)
        self.assertEqual(self.defs.get("endernet:network_baseband_16").notes.count("16u"), 1)
        self.assertEqual(self.defs.get("endernet:network_baseband_32").notes.count("32u"), 1)

    def test_bus_and_terminal_demand_one_unit(self):
        for device_id in (
            "endernet:bus_item_io",
            "endernet:bus_fluid_io",
            "endernet:bus_item_output_io",
            "endernet:bus_item_input_io",
            "endernet:bus_template_io",
            "endernet:common_terminal_panel",
            "endernet:crafting_terminal_panel",
            "endernet:template_terminal_panel",
            "endernet:factoric_terminal_panel",
            "endernet:assemble_chamber",
        ):
            self.assertEqual(self.demand(device_id), 4, device_id)

    def test_parallel_processor_demands_four_units(self):
        self.assertEqual(self.demand("endernet:parallel_processor"), 16)

    def test_memory_slot_demands_two_units(self):
        self.assertEqual(self.demand("endernet:memory_slot"), 8)

    def test_energy_input_does_not_use_bandwidth(self):
        self.assertEqual(self.demand("endernet:energy_input_interface"), 0)

    def test_creative_energy_source_does_not_use_bandwidth(self):
        """blocks.md：不占用带宽。0 是**已定案**的 0，不是缺项。"""
        self.assertEqual(self.demand("endernet:creative_energy_source"), 0)
        self.assertTrue(
            self.defs.get("endernet:creative_energy_source").bandwidth.base_quarters
            is not UNCONFIGURED
        )

    def test_storage_driver_bandwidth_is_disk_count_quarters(self):
        """(硬盘个数/4)u 精确等于「每盘 1 个四分之一 u」，无需取整。"""
        self.assertEqual(self.demand("endernet:storage_driver", disks=0), 0)
        self.assertEqual(self.demand("endernet:storage_driver", disks=1), 1)
        self.assertEqual(self.demand("endernet:storage_driver", disks=4), 4)
        self.assertEqual(self.demand("endernet:storage_driver", disks=4), 4 * 1)

    def test_storage_driver_four_disks_equals_one_unit(self):
        self.assertEqual(self.demand("endernet:storage_driver", disks=4), 4)

    def test_unlisted_bandwidth_is_unconfigured_not_zero(self):
        """控制器与连接器的带宽 blocks.md 未列，必须是缺项而不是 0。"""
        for device_id in (
            "endernet:dos_network_endpoint",
            "endernet:tiny_network_endpoint",
            "endernet:parallel_processor_connector",
        ):
            self.assertIs(self.demand(device_id), UNCONFIGURED, device_id)


class TestEnergyNumbers(unittest.TestCase):
    """能耗数值逐条对应 blocks.md。"""

    def setUp(self):
        self.defs = DefinitionSet()

    def energy(self, device_id, state="idle", units=0):
        return self.defs.get(device_id).energy.value_for_state(state, units)

    def test_controllers(self):
        self.assertEqual(self.energy("endernet:dos_network_endpoint"), 4)

    def test_tiny_controller_energy_follows_bandwidth(self):
        spec = self.defs.get("endernet:tiny_network_endpoint").energy
        self.assertTrue(spec.follows_bandwidth)

    def test_buses_that_document_energy(self):
        for device_id in (
            "endernet:bus_item_output_io",
            "endernet:bus_fluid_output_io",
            "endernet:bus_item_input_io",
            "endernet:bus_fluid_input_io",
            "endernet:bus_template_io",
        ):
            self.assertEqual(self.energy(device_id), 4, device_id)

    def test_terminals_draw_four(self):
        for device_id in (
            "endernet:common_terminal_panel",
            "endernet:crafting_terminal_panel",
            "endernet:template_terminal_panel",
            "endernet:factoric_terminal_panel",
        ):
            self.assertEqual(self.energy(device_id), 4, device_id)

    def test_parallel_processor_idle_and_work(self):
        self.assertEqual(self.energy("endernet:parallel_processor", "idle"), 1)
        self.assertEqual(self.energy("endernet:parallel_processor", "work"), 32)

    def test_assemble_chamber_idle_and_work(self):
        self.assertEqual(self.energy("endernet:assemble_chamber", "idle"), 1)
        self.assertEqual(self.energy("endernet:assemble_chamber", "work"), 8)

    def test_storage_driver_scales_with_disk_count(self):
        self.assertEqual(self.energy("endernet:storage_driver", units=0), 0)
        self.assertEqual(self.energy("endernet:storage_driver", units=1), 1)
        self.assertEqual(self.energy("endernet:storage_driver", units=4), 4)

    def test_memory_slot_scales_with_memory_count(self):
        self.assertEqual(self.energy("endernet:memory_slot", units=0), 0)
        self.assertEqual(self.energy("endernet:memory_slot", units=2), 16)

    def test_per_unit_rules_are_recorded(self):
        self.assertEqual(PER_DISK_ENERGY.value, 1)
        self.assertEqual(PER_MEMORY_ENERGY.value, 8)

    def test_buses_without_documented_energy_are_unconfigured(self):
        """blocks.md 只给了带宽没给耗能的接口，必须是缺项而不是免费。"""
        for device_id in ("endernet:bus_item_io", "endernet:bus_fluid_io"):
            self.assertIs(self.energy(device_id), UNCONFIGURED, device_id)

    def test_energy_input_interface_energy_unconfigured(self):
        """「不占带宽」不等于耗能为零，blocks.md 未列即缺项。"""
        self.assertIs(self.energy("endernet:energy_input_interface"), UNCONFIGURED)

    def test_creative_energy_source_energy_unconfigured(self):
        """blocks.md 只写了它**提供**多少，没写它**消耗**多少——那是缺项。"""
        self.assertIs(self.energy("endernet:creative_energy_source"), UNCONFIGURED)


class TestSupplyNumbers(unittest.TestCase):
    """供能数值逐条对应 blocks.md。

    **供能与耗能分开测**：`TestEnergyNumbers` 管「花多少」，这里管「给多少」。
    合成一个字段的话，「能耗未知」与「供能未知」就分不开了。
    """

    def setUp(self):
        self.defs = DefinitionSet()

    def test_creative_source_supplies_the_documented_rate(self):
        """blocks.md：为网络提供 2147483647 EN/t。这是定案值，不是替身。"""
        definition = self.defs.get("endernet:creative_energy_source")
        self.assertTrue(definition.supplies())
        self.assertEqual(definition.supply_rate(), 2147483647)
        self.assertTrue(definition.supply.is_settled())

    def test_energy_input_interface_supplies_but_rate_is_undecided(self):
        """它供能，但 RF→EN 的折算比例属 R08——**不能省略成「不供能」**。

        写成 `None` 会被读成「它不供能」，于是只接了能源接入口的网络会报
        「确认没电」，而正确答案是「供能速率未定」。两者对用户是两句完全不同
        的话，因此这里必须留着一个**带标记**的未定值。
        """
        definition = self.defs.get("endernet:energy_input_interface")
        self.assertTrue(definition.supplies())
        self.assertIs(definition.supply_rate(), UNCONFIGURED)
        self.assertEqual(definition.supply.unresolved, "R08")
        self.assertTrue(definition.supply.is_test_value)
        self.assertFalse(definition.supply.is_settled())

    def test_ordinary_devices_do_not_supply(self):
        """不供能是**结构性事实**，不是缺项：`supplies()` 假、`supply_rate()` None。"""
        for device_id in (
            "endernet:network_baseband_8",
            "endernet:storage_driver",
            "endernet:common_terminal_panel",
            "endernet:dos_network_endpoint",
        ):
            definition = self.defs.get(device_id)
            self.assertFalse(definition.supplies(), device_id)
            self.assertIsNone(definition.supply_rate(), device_id)

    def test_supply_is_registered_in_iter_values(self):
        """**新字段必须真的进 `iter_values()`**，否则守卫覆盖不到它。

        这条是防「加了字段忘了登记」：那样一个「带未定标签却不是测试值」的
        供能速率会**恰好**落在 `assert_no_silent_unresolved()` 的范围之外，
        而那正是这个守卫存在的唯一理由。
        """
        paths = dict(self.defs.iter_values())
        self.assertIn("endernet:creative_energy_source.supply", paths)
        self.assertIn("endernet:energy_input_interface.supply", paths)
        self.assertEqual(
            paths["endernet:creative_energy_source.supply"].value, 2147483647
        )

    def test_devices_without_supply_emit_no_supply_path(self):
        """不供能的设备不产出路径，否则 21 台设备的「不供能」会被报成缺项。"""
        paths = [path for path, _ in self.defs.iter_values()]
        self.assertNotIn("endernet:network_baseband_8.supply", paths)
        self.assertNotIn("endernet:storage_driver.supply", paths)
        self.assertEqual(
            sorted(path for path in paths if path.endswith(".supply")),
            [
                "endernet:creative_energy_source.supply",
                "endernet:energy_input_interface.supply",
            ],
        )

    def test_silent_supply_resolution_is_caught(self):
        """供能速率若被悄悄定案（带标记却不是测试值），装配期守卫必须报错。"""
        silent = DeviceDefinition(
            "endernet:test_supplier",
            "测试电源",
            CATEGORY_ENERGY_INPUT,
            bandwidth=BandwidthSpec(0),
            supply=Value(64, unresolved="R08", is_test_value=False),
        )
        defs = DefinitionSet(devices=(silent,), mediums=())
        with self.assertRaises(AssertionError):
            defs.assert_no_silent_unresolved()

    def test_tagged_supply_stand_in_passes_the_guard(self):
        """带标记的**测试替身**合法：它没有把未定规则定案。"""
        tagged = DeviceDefinition(
            "endernet:test_supplier",
            "测试电源",
            CATEGORY_ENERGY_INPUT,
            bandwidth=BandwidthSpec(0),
            supply=Value(UNCONFIGURED, unresolved="R08", is_test_value=True),
        )
        defs = DefinitionSet(devices=(tagged,), mediums=())
        defs.assert_no_silent_unresolved()


class TestMediums(unittest.TestCase):
    """介质规格逐项对应 items.md。"""

    EXPECTED = {
        "endernet:singleside_tape": (12, 1728, 4, 4),
        "endernet:doubleside_tape": (24, 3456, 4, 4),
        "endernet:database_compressed_tape": (64, 2 ** 30, 64, 64),
        "endernet:softdisk_1k": (32, 1728, 16, 16),
        "endernet:softdisk_4k": (32, 5120, 32, 32),
        "endernet:harddisk_1k": (64, 4096, 24, 16),
        "endernet:harddisk_4k": (64, 2 ** 16, 32, 24),
        "endernet:harddisk_16": (64, 2 ** 18, 48, 32),
    }

    def test_all_documented_mediums_present(self):
        self.assertEqual(set(m.id for m in MEDIUMS), set(self.EXPECTED))

    def test_each_spec_matches_document(self):
        defs = DefinitionSet()
        for medium_id, (types, capacity, read, write) in self.EXPECTED.items():
            spec = defs.get_medium(medium_id)
            self.assertEqual(spec.max_types, types, medium_id)
            self.assertEqual(spec.capacity_units, capacity, medium_id)
            self.assertEqual(spec.read_units_per_second, read, medium_id)
            self.assertEqual(spec.write_units_per_second, write, medium_id)

    def test_harddisk_16_keeps_documented_spelling(self):
        """items.md 的标识就是 harddisk_16，不得擅自改成 16k。"""
        defs = DefinitionSet()
        self.assertIn("endernet:harddisk_16", defs.mediums)
        self.assertNotIn("endernet:harddisk_16k", defs.mediums)

    def test_unknown_medium_rejected_loudly(self):
        with self.assertRaises(KeyError):
            DefinitionSet().get_medium("endernet:floppy_from_nowhere")


class TestPatternTemplates(unittest.TestCase):
    """两类样板逐项对应 items.md 的「样板」一节（任务 07-01/07-02）。"""

    #: items.md 给出的标识与中文名。**不擅自补全 items.md 之外的型号。**
    EXPECTED = {
        "endernet:crafting_template": ("合成样板", "crafting"),
        "endernet:factorical_template": ("工厂样板", "factory"),
    }

    def test_all_documented_templates_present(self):
        self.assertEqual(set(t.id for t in PATTERN_TEMPLATES), set(self.EXPECTED))

    def test_each_spec_matches_document(self):
        defs = DefinitionSet()
        for template_id, (label, mode) in self.EXPECTED.items():
            spec = defs.get_template(template_id)
            self.assertEqual(spec.label, label, template_id)
            self.assertEqual(spec.mode, mode, template_id)
            self.assertTrue(spec.usage, template_id)

    def test_the_documented_spelling_is_kept(self):
        """items.md 的标识就是 factorical_template（不是 factory_）。"""
        defs = DefinitionSet()
        self.assertIn("endernet:factorical_template", defs.templates)
        self.assertNotIn("endernet:factory_template", defs.templates)

    def test_both_templates_are_registered_as_items(self):
        defs = DefinitionSet()
        self.assertEqual(
            sorted(defs.item_keys()),
            [
                "endernet:crafting_template",
                "endernet:factorical_template",
                "endernet:singleside_tape",
                "endernet:softdisk_1k",
            ],
        )
        self.assertEqual(len(defs.item_specs()), 4)

    def test_unknown_template_rejected_loudly(self):
        with self.assertRaises(KeyError):
            DefinitionSet().get_template("endernet:ghost_template")

    def test_a_template_without_an_item_is_rejected(self):
        with self.assertRaises(KeyError):
            DefinitionSet().get_item_template("endernet:ghost_template")

    def test_mode_must_be_known(self):
        with self.assertRaises(ValueError):
            TemplateSpec("endernet:x", "x", "smelting", "")

    def test_item_id_is_found_by_mode(self):
        """写样板物品时按模式反查标识，因此「模式与物品类型不符」这条路径
        根本不存在——两者查的是同一个映射。"""
        defs = DefinitionSet()
        self.assertEqual(
            defs.item_id_for_template_mode("crafting"), "endernet:crafting_template"
        )
        self.assertEqual(
            defs.item_id_for_template_mode("factory"), "endernet:factorical_template"
        )
        self.assertEqual(defs.item_id_for_template_mode("smelting"), "")

    def test_template_mode_of_only_answers_for_templates(self):
        defs = DefinitionSet()
        self.assertEqual(
            defs.template_mode_of("endernet:crafting_template"), "crafting"
        )
        self.assertEqual(defs.template_mode_of("endernet:singleside_tape"), "")
        self.assertEqual(defs.template_mode_of("minecraft:stone"), "")

    def test_the_mode_mapping_points_at_real_devices(self):
        """`PATTERN_DEVICES` 里的每台设备都必须在设备表里，且**都声明了**
        `pattern_provider` 能力——否则「样板放进了收它的设备」之后没人会去读。"""
        defs = DefinitionSet()
        for mode in PATTERN_MODES:
            allowed = devices_for_mode(mode)
            self.assertTrue(allowed, mode)
            for device_id in allowed:
                self.assertTrue(defs.has(device_id), device_id)
                self.assertTrue(
                    defs.get(device_id).has_capability(CAP_PATTERN_PROVIDER),
                    device_id,
                )

    def test_every_pattern_provider_is_somewhere_in_the_mapping(self):
        """反过来也要成立：没有哪个提供者是谁都不收样板的。"""
        defs = DefinitionSet()
        mapped = set()
        for mode in PATTERN_MODES:
            mapped |= set(devices_for_mode(mode))
        providers = set(
            device_id
            for device_id in defs.devices
            if defs.get(device_id).has_capability(CAP_PATTERN_PROVIDER)
        )
        self.assertEqual(providers, mapped)

    def test_unknown_mode_has_no_devices(self):
        self.assertEqual(devices_for_mode("smelting"), frozenset())


class TestConnectionRules(unittest.TestCase):
    """相邻不等于连通：端口规则必须显式。"""

    def test_baseband_connects_to_devices(self):
        for category in (
            CATEGORY_CONTROLLER,
            "bus",
            "terminal",
            "storage",
            "memory",
            "assembly",
            "energy_input",
            CATEGORY_PROCESSOR_CONNECTOR,
        ):
            self.assertTrue(ConnectionRules.accepts(CATEGORY_BASEBAND, category), category)

    def test_baseband_connects_to_baseband(self):
        self.assertTrue(ConnectionRules.accepts(CATEGORY_BASEBAND, CATEGORY_BASEBAND))

    def test_connector_connects_to_processor_and_baseband(self):
        self.assertTrue(
            ConnectionRules.accepts(CATEGORY_PROCESSOR_CONNECTOR, CATEGORY_PROCESSOR)
        )
        self.assertTrue(
            ConnectionRules.accepts(CATEGORY_PROCESSOR_CONNECTOR, CATEGORY_BASEBAND)
        )

    def test_processor_connects_to_processor(self):
        self.assertTrue(ConnectionRules.accepts(CATEGORY_PROCESSOR, CATEGORY_PROCESSOR))

    def test_processor_must_not_connect_directly_to_baseband(self):
        """blocks.md：处理器不能直接连接网络基带或控制器。"""
        self.assertFalse(ConnectionRules.accepts(CATEGORY_PROCESSOR, CATEGORY_BASEBAND))
        self.assertEqual(
            ConnectionRules.reason_for_reject(CATEGORY_PROCESSOR, CATEGORY_BASEBAND),
            "processor_must_use_connector",
        )

    def test_processor_must_not_connect_directly_to_controller(self):
        self.assertFalse(ConnectionRules.accepts(CATEGORY_PROCESSOR, CATEGORY_CONTROLLER))

    def test_connector_rejects_terminals_and_storage(self):
        for category in ("terminal", "storage", "bus", "memory", "assembly"):
            self.assertFalse(
                ConnectionRules.accepts(CATEGORY_PROCESSOR_CONNECTOR, category), category
            )

    def test_devices_do_not_connect_to_each_other(self):
        self.assertFalse(ConnectionRules.accepts("bus", "terminal"))
        self.assertFalse(ConnectionRules.accepts("terminal", "terminal"))
        self.assertFalse(ConnectionRules.accepts("storage", "assembly"))

    def test_rules_are_symmetric(self):
        """白名单必须双向一致，否则会出现单向连通。"""
        categories = [
            CATEGORY_BASEBAND,
            CATEGORY_CONTROLLER,
            CATEGORY_BUS,
            CATEGORY_TERMINAL,
            CATEGORY_STORAGE,
            CATEGORY_PROCESSOR,
            CATEGORY_PROCESSOR_CONNECTOR,
        ]
        for first in categories:
            for second in categories:
                self.assertEqual(
                    ConnectionRules.accepts(first, second),
                    ConnectionRules.accepts(second, first),
                    "%s <-> %s 不对称" % (first, second),
                )

    def test_reject_reason_is_specific(self):
        """业务错误不得只显示为「离线」，要有可定位原因。"""
        reason = ConnectionRules.reason_for_reject(CATEGORY_PROCESSOR, CATEGORY_BASEBAND)
        self.assertNotEqual(reason, "")


class TestCapabilities(unittest.TestCase):
    def test_driver_exposes_storage_provider(self):
        defs = DefinitionSet()
        self.assertTrue(
            defs.get("endernet:storage_driver").has_capability(CAP_STORAGE_PROVIDER)
        )

    def test_terminals_expose_terminal_endpoint(self):
        defs = DefinitionSet()
        for device_id in defs.devices_in_category("terminal"):
            self.assertTrue(defs.get(device_id).has_capability(CAP_TERMINAL_ENDPOINT), device_id)

    def test_buses_expose_item_or_fluid_port(self):
        defs = DefinitionSet()
        for device_id in defs.devices_in_category("bus"):
            definition = defs.get(device_id)
            self.assertTrue(
                definition.has_capability(CAP_ITEM_PORT)
                or definition.has_capability("fluid_port"),
                device_id,
            )

    def test_baseband_has_no_capability(self):
        defs = DefinitionSet()
        for device_id in defs.devices_in_category(CATEGORY_BASEBAND):
            self.assertEqual(defs.get(device_id).capabilities, (), device_id)


class TestControllerAllowance(unittest.TestCase):
    """DOS 控制器只能控制 blocks.md 列举的方块。"""

    def setUp(self):
        self.defs = DefinitionSet()

    def test_dos_controller_allows_documented_targets(self):
        dos = self.defs.get("endernet:dos_network_endpoint")
        self.assertTrue(dos.allows("endernet:storage_driver"))
        self.assertTrue(dos.allows("endernet:common_terminal_panel"))
        self.assertTrue(dos.allows("endernet:crafting_terminal_panel"))

    def test_dos_controller_rejects_buses_and_processors(self):
        dos = self.defs.get("endernet:dos_network_endpoint")
        self.assertFalse(dos.allows("endernet:bus_item_io"))
        self.assertFalse(dos.allows("endernet:parallel_processor"))

    def test_tiny_controller_is_undocumented_unrestricted(self):
        """小型控制器的接入范围 blocks.md 未限定，保持 None 而不是编一个集合。"""
        tiny = self.defs.get("endernet:tiny_network_endpoint")
        self.assertIsNone(tiny.allowed_device_ids)
        self.assertTrue(tiny.allows("endernet:bus_item_io"))


class TestDiagnostics(unittest.TestCase):
    """缺项与未定项必须可被上层列举，而不是散落在各处。"""

    def setUp(self):
        self.defs = DefinitionSet()

    def test_unconfigured_report_is_not_empty(self):
        self.assertTrue(self.defs.unconfigured_report())

    def test_unconfigured_report_names_the_device(self):
        paths = [path for path, _ in self.defs.unconfigured_report()]
        self.assertIn("endernet:bus_item_io.energy.idle", paths)
        self.assertIn("endernet:dos_network_endpoint.bandwidth.base", paths)

    def test_unconfigured_report_excludes_settled_devices(self):
        """已定案的设备不得出现在缺项清单里，否则清单会失去意义。"""
        paths = [path for path, _ in self.defs.unconfigured_report()]
        self.assertNotIn("endernet:parallel_processor.energy.idle", paths)
        self.assertNotIn("endernet:common_terminal_panel.energy.idle", paths)
        self.assertNotIn("endernet:storage_driver.bandwidth.base", paths)

    def test_unresolved_report_captures_r08(self):
        unresolved = set(
            value.unresolved
            for _, value in self.defs.unresolved_report()
            if value is not None
        )
        self.assertIn("R08", unresolved)

    def test_silent_resolutions_empty_by_default(self):
        self.assertEqual(self.defs.silent_resolutions(), [])

    def test_get_unknown_device_raises(self):
        with self.assertRaises(KeyError):
            self.defs.get("endernet:nonexistent")


class TestEnergySpecUnitScaling(unittest.TestCase):
    def test_per_unit_accepts_plain_int(self):
        spec = EnergySpec(2, per_unit=3)
        self.assertEqual(spec.value_for_state("idle", unit_count=4), 14)

    def test_unconfigured_base_stays_unconfigured_regardless_of_units(self):
        spec = EnergySpec(UNCONFIGURED, per_unit=3)
        self.assertIs(spec.value_for_state("idle", unit_count=4), UNCONFIGURED)

    def test_unconfigured_per_unit_makes_result_unconfigured(self):
        spec = EnergySpec(0, per_unit=Value(UNCONFIGURED))
        self.assertIs(spec.value_for_state("idle", unit_count=4), UNCONFIGURED)

    def test_work_state_defaults_to_idle_when_not_given(self):
        spec = EnergySpec(5)
        self.assertEqual(spec.value_for_state("work"), 5)


if __name__ == "__main__":
    unittest.main()
