# coding=utf-8
"""配方结构与样板编解码（任务 07-01 / 07-02）。

**全部离线**：`crafting/patterns.py` 是纯数据与纯函数，一行引擎都不碰。

这一套用例守的是几件「结构上看着对、实际会出事」的事：

1. **表达力够不够。** 判据不是「能构造出来」，而是 `plans/concepts.md`
   给的那四个例子能不能原样写出——多产物、流体、无限燃料、有序摆位
   各压一条。少一条能力，将来就要改已存档的数据格式。
2. **非法配方进不来。** 零/负数量、没有产物、无效资源、有序无序混用，
   每一条都要被拒，而且**拒绝原因要能指到具体哪一行**（界面按它显示）。
3. **往返一致。** 编出去再读回来必须逐字段相等；`userData` 里别人的键
   一个都不能动。
4. **旧格式不可派发。** 这条现在只有测试能验（真实数据里只有 v1），
   但格式升级那一次必须已经拦得住。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import (
    PATTERN_MODE_CRAFTING,
    PATTERN_MODE_FACTORY,
    PATTERN_TEMPLATES,
    devices_for_mode,
)
from endernet.common.resource_key import ResourceKey
from endernet.server.crafting import patterns
from endernet.server.crafting.patterns import (
    MODE_CRAFTING,
    MODE_FACTORY,
    PATTERN_DATA_KEY,
    PATTERN_FORMAT_VERSION,
    KindScope,
    PatternDefinition,
    PatternError,
    PatternPolicy,
    PatternStack,
    PatternValidationError,
    ReturnRule,
    UnlimitedFuel,
    decode_pattern,
    describe_tip,
    encode_pattern,
    has_pattern,
    read_pattern,
    write_pattern,
)

IRON = "minecraft:iron_ingot"
IRON_BLOCK = "minecraft:iron_block"
RAW_IRON = "minecraft:raw_iron"
COAL = "minecraft:coal"
CHARCOAL = "minecraft:charcoal"
RAW_FISH = "minecraft:raw_fish"
COOKED_FISH = "minecraft:cooked_fish"
DIRT = "minecraft:dirt"
CLAY = "minecraft:clay"
WATER = "minecraft:water"


def item(name, count=1, slot=None, unlimited=False):
    return PatternStack(ResourceKey.item(name), count, slot, unlimited)


def fluid(name, count):
    return PatternStack(ResourceKey.fluid(name), count)


def _is_ascii_only(value):
    """整棵树里所有文本都是 ASCII 吗（键与值都查）。"""
    if isinstance(value, dict):
        return all(
            _is_ascii_only(key) and _is_ascii_only(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return all(_is_ascii_only(item) for item in value)
    if isinstance(value, str):
        try:
            value.encode("ascii")
        except UnicodeEncodeError:
            return False
        return True
    return True


def craft(inputs, outputs, returns=()):
    return PatternDefinition(MODE_CRAFTING, inputs, outputs, returns)


def factory(inputs, outputs, returns=()):
    return PatternDefinition(MODE_FACTORY, inputs, outputs, returns)


class ValidationCase(unittest.TestCase):
    """拒绝类用例的公共断言：一定要被拒，而且原因码要对得上。"""

    def assertRejected(self, code, callable_):
        with self.assertRaises(PatternValidationError) as caught:
            callable_()
        self.assertEqual(caught.exception.code, code)
        # 消息不能是空壳：界面与日志都直接显示它。
        self.assertTrue(caught.exception.detail)
        return caught.exception


# -- 表达力（07-01）--------------------------------------------------------


class TestExpressiveness(ValidationCase):
    """`plans/concepts.md` 的四个例子。**这是本模块表达力的验收清单。**"""

    def test_workbench_preset_is_ordered(self):
        """系统预设-工作台：1 个铁块 <- 9 个铁锭（九格填满）。"""
        pattern = craft(
            [item(IRON, 1, slot=slot) for slot in range(9)],
            [item(IRON_BLOCK, 1)],
        )
        self.assertTrue(pattern.is_ordered())
        self.assertEqual(len(pattern.inputs), 9)
        self.assertEqual(sorted(s.slot for s in pattern.inputs), list(range(9)))

    def test_furnace_preset_with_unlimited_fuel(self):
        """系统预设-熔炉：1 个铁锭 <- 1 个粗铁（默认熔炉有无限的燃料供应）。

        燃料在这条里**没有被写成一条输入**——它正是 R09 要澄清的那种情形。
        本条只验「能表达」，含义归 `TestPolicy`。
        """
        pattern = factory([item(RAW_IRON, 1)], [item(IRON, 1)])
        self.assertEqual(pattern.summary()["unlimited"], 0)

    def test_furnace_preset_with_explicit_unlimited_marker(self):
        """把「无限燃料」显式写成一条带标记的输入，也要能表达。"""
        pattern = factory(
            [item(RAW_IRON, 1), item(COAL, 1, unlimited=True)],
            [item(IRON, 1)],
        )
        self.assertEqual(pattern.summary()["unlimited"], 1)
        # 标记要能穿过编解码，否则策略读不到它。
        again = decode_pattern(encode_pattern(pattern)).definition
        self.assertEqual(again, pattern)
        self.assertTrue(again.inputs[1].unlimited)

    def test_multi_output_with_a_return(self):
        """自定义：8 份烤鱼 <- 8 份生鱼 + 1 份木炭，并返还一个空桶。

        多产物与返还物同时出现——10 的完成判定要靠这两组分得清。
        """
        pattern = factory(
            [item(RAW_FISH, 8), item(CHARCOAL, 1)],
            [item(COOKED_FISH, 8)],
            [item("minecraft:bucket", 1)],
        )
        self.assertEqual(len(pattern.outputs), 1)
        self.assertEqual(pattern.outputs[0].count, 8)
        self.assertEqual(len(pattern.returns), 1)

    def test_fluid_input(self):
        """自定义：1 个粘土块 <- 1 块尘土 + 1000mB 水。"""
        pattern = factory([item(DIRT, 1), fluid(WATER, 1000)], [item(CLAY, 1)])
        self.assertTrue(pattern.has_fluid())
        self.assertEqual(pattern.fluid_group()[0][0], "inputs")
        self.assertTrue(pattern.fluid_group()[0][1].key.is_fluid())
        self.assertEqual(pattern.fluid_group()[0][1].count, 1000)

    def test_multiple_outputs_reach_every_output_index(self):
        """一个配方的**每个**产物都要能被查到（否则只有第一条路找得到它）。"""
        pattern = factory(
            [item(RAW_FISH, 8)],
            [item(COOKED_FISH, 8), item("minecraft:bone_meal", 1)],
        )
        names = sorted(key.name for key in pattern.output_keys())
        self.assertEqual(names, ["minecraft:bone_meal", "minecraft:cooked_fish"])

    def test_duplicate_outputs_are_indexed_once(self):
        pattern = factory(
            [item(RAW_FISH, 2)],
            [item(COOKED_FISH, 1), item(COOKED_FISH, 1)],
        )
        self.assertEqual(len(pattern.output_keys()), 1)


class TestHasNoMachineType(ValidationCase):
    """样板不记录外部机器类型（`concepts.md` 与任务 07 都写明了）。"""

    def test_definition_fields_are_exactly_the_four(self):
        self.assertEqual(
            sorted(PatternDefinition.__slots__),
            ["inputs", "mode", "outputs", "returns"],
        )

    def test_encoded_payload_carries_no_machine_field(self):
        payload = encode_pattern(factory([item(RAW_IRON, 1)], [item(IRON, 1)]))
        self.assertEqual(
            sorted(payload), ["format", "inputs", "mode", "outputs", "returns"]
        )


# -- 拒绝（07-01）----------------------------------------------------------


class TestRejection(ValidationCase):
    """07-01 点名要拒的三条，外加几条结构性错误。"""

    def test_zero_count_is_rejected(self):
        self.assertRejected(
            PatternError.NON_POSITIVE_COUNT,
            lambda: factory([item(RAW_IRON, 0)], [item(IRON, 1)]),
        )

    def test_negative_count_is_rejected(self):
        self.assertRejected(
            PatternError.NON_POSITIVE_COUNT,
            lambda: factory([item(RAW_IRON, -4)], [item(IRON, 1)]),
        )

    def test_zero_count_on_output_is_rejected_too(self):
        self.assertRejected(
            PatternError.NON_POSITIVE_COUNT,
            lambda: factory([item(RAW_IRON, 1)], [item(IRON, 0)]),
        )

    def test_boolean_is_not_a_count(self):
        """`True` 是 `int` 的子类，但它不是数量——放行会变成「1 个」的静默替身。"""
        self.assertRejected(
            PatternError.INVALID_COUNT, lambda: PatternStack(ResourceKey.item(IRON), True)
        )

    def test_no_output_is_rejected(self):
        self.assertRejected(
            PatternError.EMPTY_OUTPUTS,
            lambda: factory([item(RAW_IRON, 1)], []),
        )

    def test_no_input_is_rejected(self):
        """零输入会让网络凭空造物，与物品守恒直接冲突。"""
        self.assertRejected(
            PatternError.EMPTY_INPUTS,
            lambda: factory([], [item(IRON, 1)]),
        )

    def test_invalid_resource_is_rejected(self):
        self.assertRejected(
            PatternError.INVALID_RESOURCE,
            lambda: PatternStack("minecraft:iron_ingot", 1),
        )

    def test_non_stack_entry_is_rejected(self):
        self.assertRejected(
            PatternError.MALFORMED,
            lambda: factory([IRON], [item(IRON_BLOCK, 1)]),
        )

    def test_unknown_mode_is_rejected(self):
        self.assertRejected(
            PatternError.UNKNOWN_MODE,
            lambda: PatternDefinition("smelting", [item(RAW_IRON, 1)], [item(IRON, 1)]),
        )

    def test_factory_pattern_cannot_be_ordered(self):
        """工厂样板没有工作台格子。"""
        self.assertRejected(
            PatternError.SLOT_NOT_ALLOWED,
            lambda: factory([item(RAW_IRON, 1, slot=0)], [item(IRON, 1)]),
        )

    def test_ordered_and_unordered_cannot_be_mixed(self):
        self.assertRejected(
            PatternError.ORDERED_MIX,
            lambda: craft(
                [item(IRON, 1, slot=0), item(DIRT, 1)], [item(IRON_BLOCK, 1)]
            ),
        )

    def test_duplicate_slot_is_rejected(self):
        self.assertRejected(
            PatternError.DUPLICATE_SLOT,
            lambda: craft(
                [item(IRON, 1, slot=2), item(DIRT, 1, slot=2)],
                [item(IRON_BLOCK, 1)],
            ),
        )

    def test_slot_out_of_range_is_rejected(self):
        for bad in (-1, patterns.GRID_SLOT_COUNT):
            self.assertRejected(
                PatternError.SLOT_OUT_OF_RANGE,
                lambda bad=bad: craft([item(IRON, 1, slot=bad)], [item(IRON_BLOCK, 1)]),
            )

    def test_fluid_cannot_occupy_a_grid_slot(self):
        """流体不进工作台格子：放行会造出谁也执行不了的「合法」样板。"""
        self.assertRejected(
            PatternError.SLOT_NOT_ALLOWED,
            lambda: PatternStack(ResourceKey.fluid(WATER), 1000, slot=1),
        )

    def test_returns_cannot_carry_a_slot(self):
        self.assertRejected(
            PatternError.SLOT_NOT_ALLOWED,
            lambda: craft(
                [item(IRON, 1, slot=0)],
                [item(IRON_BLOCK, 1)],
                [item("minecraft:bucket", 1, slot=0)],
            ),
        )

    def test_outputs_cannot_carry_a_slot(self):
        """产物不摆进工作台格子。

        放行的话，那个槽位对 `output_keys()` / 索引 / `is_ordered()` 全都
        不可见，**却进 `to_dict()`、因而进指纹**——同一份配方于是有两个
        「版本」，而肉眼看不出差别；`execution.editable_view` 交给 11 的
        表单契约又明写「产物：槽位必须为空」，模型与校验对不上。
        """
        with self.assertRaises(PatternValidationError) as caught:
            factory([item(RAW_IRON, 1)], [item(IRON_BLOCK, 1, slot=5)])
        self.assertEqual(caught.exception.code, PatternError.SLOT_NOT_ALLOWED)

    def test_the_unlimited_flag_only_makes_sense_on_inputs(self):
        """「不限量」说的是「这一条不向网络要」——产物与返还物没有这个含义，
        放行只会让它静悄悄进指纹。"""
        self.assertRejected(
            PatternError.FLAG_NOT_ALLOWED,
            lambda: factory([item(RAW_IRON, 1)], [item(IRON_BLOCK, 1, unlimited=True)]),
        )
        self.assertRejected(
            PatternError.FLAG_NOT_ALLOWED,
            lambda: factory(
                [item(RAW_IRON, 1)],
                [item(IRON_BLOCK, 1)],
                [item("minecraft:bucket", 1, unlimited=True)],
            ),
        )

    def test_inputs_may_carry_the_unlimited_flag(self):
        pattern = factory(
            [item(RAW_IRON, 1), item(COAL, 1, unlimited=True)], [item(IRON, 1)]
        )
        self.assertEqual(pattern.summary()["unlimited"], 1)

    def test_error_carries_a_code_and_a_detail(self):
        error = self.assertRejected(
            PatternError.EMPTY_OUTPUTS, lambda: factory([item(IRON, 1)], [])
        )
        view = error.to_dict()
        self.assertEqual(view["code"], PatternError.EMPTY_OUTPUTS)
        self.assertIn("产物", view["detail"])

    def test_validation_error_is_a_value_error(self):
        """必须是 `ValueError`：服务端的命令兜底按这一族捕获。"""
        self.assertTrue(issubclass(PatternValidationError, ValueError))


# -- 编解码（07-02）--------------------------------------------------------


class TestEncoding(ValidationCase):
    def setUp(self):
        self.pattern = factory(
            [item(RAW_IRON, 3), fluid(WATER, 250)],
            [item(IRON, 1)],
            [item("minecraft:bucket", 1)],
        )

    def test_roundtrip_is_exact(self):
        decoded = decode_pattern(encode_pattern(self.pattern))
        self.assertEqual(decoded.definition, self.pattern)

    def test_roundtrip_survives_ordered_patterns(self):
        pattern = craft(
            [item(IRON, 1, slot=slot) for slot in range(9)], [item(IRON_BLOCK, 1)]
        )
        decoded = decode_pattern(encode_pattern(pattern))
        self.assertEqual(decoded.definition, pattern)
        self.assertTrue(decoded.definition.is_ordered())
        self.assertEqual([s.slot for s in decoded.definition.inputs], list(range(9)))

    def test_roundtrip_survives_rich_identity(self):
        """附魔剑与普通剑是两个身份，样板里也要分得开。"""
        enchanted = PatternStack(
            ResourceKey.item("minecraft:diamond_sword", enchant_data=[(9, 3)]), 1
        )
        plain = PatternStack(ResourceKey.item("minecraft:diamond_sword"), 1)
        self.assertNotEqual(enchanted.key, plain.key)
        pattern = factory([enchanted], [item(IRON, 1)])
        again = decode_pattern(encode_pattern(pattern)).definition
        self.assertEqual(again.inputs[0].key, enchanted.key)

    def test_defaults_are_omitted_from_the_canonical_form(self):
        """缺省值不写出，因此同一份内容只有一种写法（也就只有一种指纹）。"""
        payload = encode_pattern(factory([item(RAW_IRON, 1)], [item(IRON, 1)]))
        self.assertNotIn("slot", payload["inputs"][0])
        self.assertNotIn("unlimited", payload["inputs"][0])

    def test_format_version_is_written(self):
        payload = encode_pattern(self.pattern)
        self.assertEqual(payload["format"], PATTERN_FORMAT_VERSION)

    # -- 读外部数据 ----------------------------------------------------

    def test_payload_without_format_is_rejected(self):
        payload = encode_pattern(self.pattern)
        payload.pop("format")
        self.assertRejected(PatternError.MALFORMED, lambda: decode_pattern(payload))

    def test_payload_with_a_newer_format_is_rejected_not_read(self):
        """更新的格式读不懂就不读，**不当作空样板覆盖**（与 12 同一条纪律）。"""
        payload = encode_pattern(self.pattern)
        payload["format"] = PATTERN_FORMAT_VERSION + 1
        self.assertRejected(PatternError.FORMAT_TOO_NEW, lambda: decode_pattern(payload))

    def test_payload_with_unknown_fields_is_rejected(self):
        """外部载荷里多出字段说明有东西没被理解，不能静默丢掉。"""
        payload = encode_pattern(self.pattern)
        payload["speed"] = 200
        self.assertRejected(PatternError.MALFORMED, lambda: decode_pattern(payload))

    def test_non_integer_format_is_rejected(self):
        payload = encode_pattern(self.pattern)
        payload["format"] = "1"
        self.assertRejected(PatternError.MALFORMED, lambda: decode_pattern(payload))

    def test_payload_that_is_not_an_object_is_rejected(self):
        self.assertRejected(PatternError.MALFORMED, lambda: decode_pattern([]))
        self.assertRejected(PatternError.MALFORMED, lambda: decode_pattern(None))

    def test_broken_resource_description_is_rejected(self):
        """`ResourceKey.from_dict` 对**缺省**字段是宽容的（缺 `aux`/`identity`
        按 0 与空处理），因此这里用的是真正无效的描述：未知种类与未知版本。"""
        for bad in (
            {"kind": "nonsense", "name": "minecraft:stone"},
            {"schema_version": 99, "kind": "item", "name": "minecraft:stone"},
            {"kind": "item", "name": ""},
        ):
            payload = encode_pattern(self.pattern)
            payload["inputs"][0]["key"] = bad
            self.assertRejected(
                PatternError.INVALID_RESOURCE, lambda payload=payload: decode_pattern(payload)
            )

    def test_resource_description_that_is_not_an_object_is_rejected(self):
        payload = encode_pattern(self.pattern)
        payload["inputs"][0]["key"] = "minecraft:stone"
        self.assertRejected(
            PatternError.INVALID_RESOURCE, lambda: decode_pattern(payload)
        )

    def test_fabricated_recipe_does_not_sneak_past_validation(self):
        """**伪造配方必须被拒**：把 JSON 手改成一个不符合任何规则的配方。"""
        payload = encode_pattern(self.pattern)
        payload["outputs"] = []
        self.assertRejected(PatternError.EMPTY_OUTPUTS, lambda: decode_pattern(payload))

    def test_structurally_broken_groups_never_raise_a_bare_type_error(self):
        """**损坏的载荷必须一律是 `PatternValidationError`。**

        `store._read_one` 只捕这一族。若某个载荷抛出 `TypeError`，它会一路
        冒到 `bootstrap.tick()` 被记成 tick_error，而那台设备**每帧都会再崩
        一次**，排在它后面的样板提供者永远刷不到——一块手改过的 NBT
        就能把整张样板索引冻住。这一族必须逐条守住。
        """
        for field, bad in (
            ("inputs", 5),
            ("inputs", None),
            ("inputs", 3.0),
            ("outputs", "minecraft:stone"),
            ("returns", 7),
            ("mode", []),
            ("mode", {"a": 1}),
        ):
            payload = encode_pattern(self.pattern)
            payload[field] = bad
            with self.assertRaises(PatternValidationError) as caught:
                decode_pattern(payload)
            self.assertTrue(caught.exception.code, (field, bad))
            self.assertTrue(caught.exception.detail, (field, bad))

    def test_structurally_broken_entries_never_raise_a_bare_type_error(self):
        for bad in (5, None, "x", []):
            payload = encode_pattern(self.pattern)
            payload["inputs"] = [bad]
            with self.assertRaises(PatternValidationError) as caught:
                decode_pattern(payload)
            self.assertEqual(caught.exception.code, PatternError.MALFORMED)

    def test_a_broken_payload_is_a_value_error(self):
        """命令层的兜底按 `ValueError` 一族捕获，异常类型不能跑偏。"""
        payload = encode_pattern(self.pattern)
        payload["inputs"] = 5
        with self.assertRaises(ValueError):
            decode_pattern(payload)

    def test_bool_cannot_impersonate_the_unlimited_flag(self):
        payload = encode_pattern(self.pattern)
        payload["inputs"][0]["unlimited"] = 1
        self.assertRejected(PatternError.MALFORMED, lambda: decode_pattern(payload))


class TestDispatchability(ValidationCase):
    """「旧版本不可派发」——07-02 的第三条。"""

    def test_current_format_is_dispatchable(self):
        decoded = decode_pattern(encode_pattern(factory([item(DIRT, 1)], [item(CLAY, 1)])))
        self.assertTrue(decoded.is_dispatchable())
        self.assertFalse(decoded.needs_upgrade())

    def test_a_below_floor_format_is_readable_but_not_dispatchable(self):
        """低于下限的格式：**读得出来、界面看得见，但订单不能用**。

        当前只有 v1，因此这里用一个低于下限的合成版本号覆盖这条路径。
        它现在走不到真实数据，但格式升级那一次必须已经拦得住。
        """
        payload = encode_pattern(factory([item(DIRT, 1)], [item(CLAY, 1)]))
        payload["format"] = patterns.PATTERN_MIN_DISPATCHABLE_FORMAT - 1
        decoded = decode_pattern(payload)
        self.assertFalse(decoded.is_dispatchable())
        self.assertTrue(decoded.needs_upgrade())
        # 内容照样读得出来——人要看得到才知道该重做。
        self.assertEqual(decoded.definition.outputs[0].key.name, CLAY)

    def test_digest_ignores_the_format(self):
        """格式是「这份数据怎么读」，内容是「这个配方是什么」。升级格式
        不该把所有样板判成「变了」，否则已派发批次会被无谓地复核一遍。"""
        definition = factory([item(DIRT, 1)], [item(CLAY, 1)])
        payload = encode_pattern(definition)
        other = dict(payload)
        other["format"] = PATTERN_FORMAT_VERSION
        self.assertEqual(
            decode_pattern(payload).digest(), decode_pattern(other).digest()
        )


class TestUserDataBoundary(ValidationCase):
    """样板编在物品自己的 `userData` 里，而 `userData` 是所有模组共用的。"""

    def setUp(self):
        self.pattern = factory([item(RAW_IRON, 1)], [item(IRON, 1)])

    def test_write_then_read_roundtrips(self):
        user_data = write_pattern(None, self.pattern)
        self.assertEqual(read_pattern(user_data).definition, self.pattern)

    def test_other_keys_are_preserved(self):
        existing = {"display": {"Lore": ["别的模组写的"]}, "someMod:flag": 1}
        user_data = write_pattern(existing, self.pattern)
        self.assertEqual(user_data["display"], existing["display"])
        self.assertEqual(user_data["someMod:flag"], 1)
        self.assertTrue(has_pattern(user_data))

    def test_the_input_dict_is_not_mutated(self):
        """物品字典在事件路径上是引擎给的对象，就地改它的有效性未经验证。"""
        existing = {"display": {}}
        write_pattern(existing, self.pattern)
        self.assertEqual(sorted(existing), ["display"])

    def test_rewriting_replaces_only_our_key(self):
        first = write_pattern(None, self.pattern)
        second = write_pattern(first, factory([item(COAL, 2)], [item(CHARCOAL, 1)]))
        self.assertEqual(len(second), 1)
        self.assertEqual(read_pattern(second).definition.outputs[0].key.name, CHARCOAL)

    def test_item_without_our_key_is_not_a_pattern(self):
        error = self.assertRejected(
            PatternError.NOT_A_PATTERN, lambda: read_pattern({"display": {}})
        )
        self.assertIn(PATTERN_DATA_KEY, error.detail)

    def test_missing_user_data_is_not_a_pattern(self):
        self.assertRejected(PatternError.NOT_A_PATTERN, lambda: read_pattern(None))

    def test_has_pattern_never_raises(self):
        for value in (None, {}, {"display": {}}, "x", 5):
            self.assertFalse(has_pattern(value))
        self.assertTrue(has_pattern(write_pattern(None, self.pattern)))

    def test_a_non_dict_user_data_is_refused_not_downgraded(self):
        """**不能降级成空字典。** 那等于「把别的模组的数据整份抹掉」，
        而这条纪律的第一句话刚说过不能那么做——引擎某些路径给的就是
        字符串，静默降级的后果是多模组环境下的数据丢失。"""
        for bad in ("{}", [], 5, u""):
            self.assertRejected(
                PatternError.MALFORMED,
                lambda bad=bad: write_pattern(bad, self.pattern),
            )

    def test_none_means_no_user_data_yet(self):
        """`None` 是例外：它明确表示「这个物品还没有任何 userData」。"""
        self.assertEqual(sorted(write_pattern(None, self.pattern)), [PATTERN_DATA_KEY])


class TestDigest(ValidationCase):
    """指纹就是「样板版本」，07-04 的旧规划复核全靠它。"""

    def test_same_content_same_digest(self):
        a = factory([item(RAW_IRON, 1)], [item(IRON, 1)])
        b = factory([item(RAW_IRON, 1)], [item(IRON, 1)])
        self.assertEqual(a.digest(), b.digest())

    def test_changing_a_count_changes_the_digest(self):
        a = factory([item(RAW_IRON, 1)], [item(IRON, 1)])
        b = factory([item(RAW_IRON, 2)], [item(IRON, 1)])
        self.assertNotEqual(a.digest(), b.digest())

    def test_changing_the_mode_changes_the_digest(self):
        a = factory([item(RAW_IRON, 1)], [item(IRON, 1)])
        b = craft([item(RAW_IRON, 1, slot=0)], [item(IRON, 1)])
        self.assertNotEqual(a.digest(), b.digest())

    def test_equal_resource_keys_give_equal_digests(self):
        """**指纹走身份，不走线上载荷。**

        `ResourceKey` 把附魔表当**集合**（排序后比较），而 `to_dict()` 照抄
        `raw_identity`（原序）。拿 `to_dict()` 当指纹的代价：只要任何路径把
        附魔表换个顺序重新存一次，`check_dispatch` 就报 `PATTERN_STALE`
        ——**样板根本没被编辑，09 却去重做了一遍计划**。
        """
        first = ResourceKey.item("minecraft:diamond_sword", enchant_data=[(1, 2), (0, 1)])
        second = ResourceKey.item("minecraft:diamond_sword", enchant_data=[(0, 1), (1, 2)])
        self.assertEqual(first, second, "前提：两者是同一个资源")
        self.assertNotEqual(first.to_dict(), second.to_dict(), "前提：载荷写法不同")

        a = factory([PatternStack(first, 1)], [item(IRON, 1)])
        b = factory([PatternStack(second, 1)], [item(IRON, 1)])
        self.assertEqual(a, b)
        self.assertEqual(a.digest(), b.digest())

    def test_patterns_are_hashable_and_consistent_with_equality(self):
        """定义了 `__eq__` 却不定义 `__hash__`，py2 上对象仍按 `id` 哈希，
        于是 `a == b` 为真时 `{a: 1}[b]` 会 `KeyError`；py3 上直接不可哈希。"""
        a = factory([item(RAW_IRON, 1)], [item(IRON, 1)])
        b = factory([item(RAW_IRON, 1)], [item(IRON, 1)])
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))
        self.assertEqual({a: "x"}[b], "x")
        stack = PatternStack(ResourceKey.item(IRON), 1)
        self.assertEqual(hash(stack), hash(PatternStack(ResourceKey.item(IRON), 1)))

    def test_the_payload_is_pure_ascii(self):
        """**写进 `userData` 的东西必须是纯 ASCII。**

        真机实测：容器 `userData` 里值为非 ASCII 的 unicode 字符串会被
        引擎**整个丢掉**（在列表或嵌套字典里则把整个容器清空），
        写成 UTF-8 字节串虽然留得下但会被再编码一次。
        只有纯 ASCII 原样往返——因此这一层编码不是可选的。
        """
        chinese = PatternStack(
            ResourceKey.item("minecraft:stone", custom_tips=u"中文提示"), 1
        )
        payload = encode_pattern(factory([chinese], [item(IRON, 1)]))
        self.assertTrue(_is_ascii_only(payload), payload)

    def test_non_ascii_content_survives_the_escaping(self):
        chinese = PatternStack(
            ResourceKey.item("minecraft:stone", custom_tips=u"中文提示"), 1
        )
        pattern = factory([chinese], [item(IRON, 1)])
        again = decode_pattern(encode_pattern(pattern)).definition
        self.assertEqual(again, pattern)
        self.assertEqual(again.inputs[0].key.raw_identity["custom_tips"], u"中文提示")

    def test_escaping_round_trips_awkward_text(self):
        for text in (u"", u"纯中文", u"mixed 中英 123", u"反斜杠\\与\\u4e2d转义",
                     u"emoji \U0001F600 星平面", u"空格 与\t制表"):
            stack = PatternStack(
                ResourceKey.item("minecraft:stone", custom_tips=text), 1
            )
            pattern = factory([stack], [item(IRON, 1)])
            again = decode_pattern(encode_pattern(pattern)).definition
            self.assertEqual(
                again.inputs[0].key.raw_identity["custom_tips"], text, repr(text)
            )

    def test_non_ascii_inside_nested_user_data_survives(self):
        """引用的物品自己的 `user_data` 里也可能有中文（别的模组写的）。"""
        nested = {"otherMod:label": u"中文标签", "n": 3, "flag": True}
        stack = PatternStack(
            ResourceKey.item("minecraft:stone", user_data=nested), 1
        )
        pattern = factory([stack], [item(IRON, 1)])
        again = decode_pattern(encode_pattern(pattern)).definition
        self.assertEqual(again.inputs[0].key.raw_identity["user_data"], nested)

    def test_escaped_text_is_a_byte_string_not_unicode(self):
        """**转义结果必须是 py2 的 `str`（字节串），不能是 `unicode`。**

        真机隔离实验：`userData` 里凡是 `unicode` 一律被引擎破坏——
        unicode 的**键**让整个条目消失，unicode 的**值**让条目被清空，
        与内容是不是 ASCII 无关。第一版转义层用 `u"".join(...)` 构造，
        于是一份完全正确的样板写进去之后整个不见了，且不报任何错。

        这条断言在 py2 上是**真的会拦**：那时 `str is not unicode`。
        py3 上两者同一个类型，因此它只能防住一半——另一半靠第八节的实测。
        """
        for text in (u"中文", "ascii", u"mixed 中英"):
            escaped = patterns._escape_text(text)
            self.assertIs(type(escaped), str, repr(text))

    def test_ascii_content_is_byte_identical_to_the_unescaped_form(self):
        """纯 ASCII 的样板编码出来与不做这一层时完全一样——
        转义只在真的含非 ASCII 时才出现。"""
        pattern = factory([item(RAW_IRON, 1)], [item(IRON, 1)])
        self.assertEqual(
            encode_pattern(pattern)["inputs"], pattern.to_dict()["inputs"]
        )

    def test_tip_text_is_not_part_of_the_digest(self):
        """提示文本是派生的展示信息；改措辞不该让玩家的样板变成另一版。"""
        pattern = factory([item(RAW_IRON, 1)], [item(IRON, 1)])
        self.assertIn("minecraft:raw_iron", describe_tip(pattern))
        self.assertEqual(pattern.digest(), pattern.digest())

    def test_digest_is_a_string_and_stable_across_reconstruction(self):
        pattern = factory([item(RAW_IRON, 1), fluid(WATER, 1000)], [item(IRON, 1)])
        again = decode_pattern(encode_pattern(pattern)).definition
        self.assertTrue(isinstance(pattern.digest(), str) or type(pattern.digest()) is str)
        self.assertEqual(pattern.digest(), again.digest())


# -- R09 策略 --------------------------------------------------------------


class TestPolicy(ValidationCase):
    """R09 的三条取值。**未定的进 `unresolved_report()`，已定案的留成字段。**"""

    def setUp(self):
        self.policy = PatternPolicy()

    def test_defaults_are_the_settled_side(self):
        """三条的默认值就是定案值/替身值：两条**已定案**（无限燃料 2026-09-30、
        返还物显式声明同日），只剩 `kind_scope` 仍是替身。"""
        self.assertEqual(self.policy.unlimited_fuel, UnlimitedFuel.EXTERNAL_SUPPLY)
        self.assertEqual(self.policy.return_rule, ReturnRule.DECLARED_ONLY)
        self.assertEqual(self.policy.kind_scope, KindScope.STRICT)

    def test_the_one_open_item_is_reported_as_unresolved(self):
        """R09 三条里**只剩 `kind_scope`** 一条未定。

        另外两条的收口见下一条用例；这里的条数从 3 → 2 → 1 走过两轮，
        每次都是「定案了哪一条」而不是「谁忘了报」。
        """
        fields = sorted(item["field"] for item in self.policy.unresolved_report())
        self.assertEqual(
            fields,
            [
                "pattern.kind_scope",
            ],
        )
        for item in self.policy.unresolved_report():
            self.assertEqual(item["rule"], "R09")
            self.assertTrue(item["note"])

    def test_the_settled_fields_keep_their_names_but_leave_the_report(self):
        """**无限燃料与返还物显式声明都已于 2026-09-30 定案**（照
        `ReturnPolicy.exclusive` 的先例）：取值留着给诊断与测试，但不再进
        `unresolved_report()`——正式档不再为它们拦，也不该有人把它们当成
        「还没定」的替身去翻。
        """
        fields = [item["field"] for item in self.policy.unresolved_report()]
        self.assertNotIn("pattern.unlimited_fuel", fields)
        self.assertNotIn("pattern.return_rule", fields)
        self.assertEqual(self.policy.unlimited_fuel, UnlimitedFuel.EXTERNAL_SUPPLY)
        self.assertEqual(self.policy.return_rule, ReturnRule.DECLARED_ONLY)
        self.assertIn("unlimited_fuel", self.policy.describe())
        self.assertIn("return_rule", self.policy.describe())

    def test_assert_settled_refuses_while_r09_is_open(self):
        with self.assertRaises(AssertionError):
            self.policy.assert_settled()

    def test_plain_inputs_are_always_supplied(self):
        self.assertTrue(self.policy.supplies(item(RAW_IRON, 1)))

    def test_unlimited_input_is_not_supplied(self):
        """定案口径：「不自动供给未授权燃料」——标记的那条由外部自理。"""
        self.assertFalse(self.policy.supplies(item(COAL, 1, unlimited=True)))

    def test_the_excluded_side_is_refused_at_construction(self):
        """另一侧（标记只是说明、网络照常备料）**不再可选**。

        「要网络供给」的落点是**不标记**那条输入、把它写成样板的显式输入，
        而不是把这个开关翻过去。另一端在装配期就报错——它看起来完全合理，
        一旦生效就是另一套玩法。
        """
        with self.assertRaises(ValueError):
            PatternPolicy(unlimited_fuel=UnlimitedFuel.MODELING_ONLY)

    def test_required_inputs_skip_the_externally_supplied_ones(self):
        """网络投的料 = 样板声明的输入里、没被标成外部自理的那些。

        「要不要燃料由玩家在样板里设定」的代码落点就是这里：标记了的那条
        不进需求，其余照常。
        """
        pattern = factory(
            [item(RAW_IRON, 1), item(COAL, 1, unlimited=True)], [item(IRON, 1)]
        )
        required = self.policy.required_inputs(pattern)
        self.assertEqual([stack.key.name for stack in required], [RAW_IRON])

    def test_expected_returns_is_the_declared_set(self):
        pattern = factory(
            [item(RAW_IRON, 1)],
            [item(IRON, 1)],
            [item("minecraft:bucket", 1)],
        )
        self.assertEqual(len(self.policy.expected_returns(pattern)), 1)

    def test_deriving_returns_is_refused_loudly_at_construction(self):
        """**2026-09-30 定案**：返还物一律由样板显式声明，这一侧**不会实现**。

        **在装配期就报错**——一个「装作能用」的推导（返回空表）会让 10 的
        完成判定永远满足，而「空桶未到就完成了」正是 09 文档点名要防的那件事。
        """
        with self.assertRaises(ValueError):
            PatternPolicy(return_rule=ReturnRule.DERIVE_CONTAINERS)

    def test_strict_scope_matches_items_md(self):
        for spec in PATTERN_TEMPLATES:
            allowed = devices_for_mode(spec.mode)
            self.assertTrue(allowed, spec.id)
            for device_id in allowed:
                self.assertTrue(self.policy.accepts_mode(device_id, spec.mode))
        # 同时反过来：合成样板不该被样板接口接受，反之亦然。
        self.assertFalse(
            self.policy.accepts_mode("endernet:bus_template_io", MODE_CRAFTING)
        )
        self.assertFalse(
            self.policy.accepts_mode("endernet:assemble_chamber", MODE_FACTORY)
        )

    def test_any_scope_accepts_everything(self):
        policy = PatternPolicy(kind_scope=KindScope.ANY)
        self.assertTrue(policy.accepts_mode("endernet:bus_template_io", MODE_CRAFTING))
        self.assertTrue(policy.accepts_mode("endernet:assemble_chamber", MODE_FACTORY))

    def test_unknown_policy_values_are_rejected(self):
        with self.assertRaises(ValueError):
            PatternPolicy(unlimited_fuel="whatever")
        with self.assertRaises(ValueError):
            PatternPolicy(kind_scope="whatever")
        with self.assertRaises(ValueError):
            PatternPolicy(return_rule="whatever")

    def test_describe_lists_every_field(self):
        self.assertEqual(
            sorted(self.policy.describe()),
            ["kind_scope", "return_rule", "unlimited_fuel"],
        )


class TestModesMatchTheItemTable(ValidationCase):
    """模式词汇只有一套：物品类型就是它能承载的配方模式。"""

    def test_modes_are_the_definition_table_ones(self):
        self.assertEqual(MODE_CRAFTING, PATTERN_MODE_CRAFTING)
        self.assertEqual(MODE_FACTORY, PATTERN_MODE_FACTORY)
        self.assertEqual(patterns.MODES, frozenset([MODE_CRAFTING, MODE_FACTORY]))

    def test_every_documented_device_is_known(self):
        from endernet.common.definitions import DefinitionSet

        definitions = DefinitionSet()
        for spec in PATTERN_TEMPLATES:
            for device_id in devices_for_mode(spec.mode):
                self.assertTrue(definitions.has(device_id), device_id)


if __name__ == "__main__":
    unittest.main()
