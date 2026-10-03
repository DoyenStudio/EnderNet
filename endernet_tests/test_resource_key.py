# coding=utf-8
"""01-02 验收：资源身份规范化、无损还原与整数单位尺度。"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.resource_key import (
    ITEM_MICRO_UNITS,
    ResourceKey,
    canonical_tree,
    capacity_micro_units,
    fluid_micro_units,
    item_micro_units,
    tree_text,
)

IRON = "minecraft:iron_ingot"
SWORD = "minecraft:diamond_sword"
WATER = "minecraft:water"


class TestIdentity(unittest.TestCase):
    """名称相同但附魔/耐久/自定义数据不同的物品不得合并。"""

    def test_same_name_same_identity(self):
        self.assertEqual(ResourceKey.item(IRON), ResourceKey.item(IRON))

    def test_aux_value_is_part_of_identity(self):
        self.assertNotEqual(ResourceKey.item(SWORD, aux=0), ResourceKey.item(SWORD, aux=1))

    def test_enchantment_changes_identity(self):
        plain = ResourceKey.item(SWORD)
        sharp = ResourceKey.item(SWORD, enchant_data=[(9, 5)])
        self.assertNotEqual(plain, sharp)

    def test_different_enchant_level_changes_identity(self):
        low = ResourceKey.item(SWORD, enchant_data=[(9, 1)])
        high = ResourceKey.item(SWORD, enchant_data=[(9, 5)])
        self.assertNotEqual(low, high)

    def test_enchant_order_does_not_change_identity(self):
        """附魔是集合语义，登记顺序不同仍是同一身份。"""
        first = ResourceKey.item(SWORD, enchant_data=[(9, 5), (34, 3)])
        second = ResourceKey.item(SWORD, enchant_data=[(34, 3), (9, 5)])
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))

    def test_mod_enchant_order_does_not_change_identity(self):
        first = ResourceKey.item(SWORD, mod_enchant_data=[("a:fire", 1), ("b:ice", 2)])
        second = ResourceKey.item(SWORD, mod_enchant_data=[("b:ice", 2), ("a:fire", 1)])
        self.assertEqual(first, second)

    def test_durability_changes_identity(self):
        fresh = ResourceKey.item(SWORD, durability=1561)
        worn = ResourceKey.item(SWORD, durability=100)
        self.assertNotEqual(fresh, worn)

    def test_user_data_key_order_does_not_change_identity(self):
        first = ResourceKey.item(SWORD, user_data={"a": 1, "b": 2})
        second = ResourceKey.item(SWORD, user_data={"b": 2, "a": 1})
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))

    def test_user_data_nested_value_change_is_detected(self):
        first = ResourceKey.item(SWORD, user_data={"nbt": {"charges": 1}})
        second = ResourceKey.item(SWORD, user_data={"nbt": {"charges": 2}})
        self.assertNotEqual(first, second)

    def test_extra_id_changes_identity(self):
        first = ResourceKey.item(SWORD, extra_id="machine-a")
        second = ResourceKey.item(SWORD, extra_id="machine-b")
        self.assertNotEqual(first, second)

    def test_custom_tips_changes_identity(self):
        first = ResourceKey.item(SWORD, custom_tips="tip one")
        second = ResourceKey.item(SWORD, custom_tips="tip two")
        self.assertNotEqual(first, second)

    def test_item_and_fluid_never_equal(self):
        """同名物品与流体不得因为名字相同而被视为同一资源。"""
        item = ResourceKey.item(WATER)
        fluid = ResourceKey.fluid(WATER)
        self.assertNotEqual(item, fluid)

    def test_different_fluid_names_differ(self):
        self.assertNotEqual(ResourceKey.fluid(WATER), ResourceKey.fluid("minecraft:lava"))


class TestEmptyVersusAbsent(unittest.TestCase):
    """None（字段不存在）与空列表/空字典（字段存在但为空）必须是不同身份。"""

    def test_enchant_none_vs_empty_list(self):
        absent = ResourceKey.item(SWORD, enchant_data=None)
        empty = ResourceKey.item(SWORD, enchant_data=[])
        self.assertNotEqual(absent, empty)

    def test_user_data_none_vs_empty_dict(self):
        absent = ResourceKey.item(SWORD, user_data=None)
        empty = ResourceKey.item(SWORD, user_data={})
        self.assertNotEqual(absent, empty)

    def test_durability_none_vs_zero(self):
        absent = ResourceKey.item(SWORD, durability=None)
        zero = ResourceKey.item(SWORD, durability=0)
        self.assertNotEqual(absent, zero)

    def test_custom_tips_none_vs_empty_string(self):
        absent = ResourceKey.item(SWORD, custom_tips=None)
        empty = ResourceKey.item(SWORD, custom_tips="")
        self.assertNotEqual(absent, empty)


class TestTypeFidelity(unittest.TestCase):
    """规范化不得抹掉 NBT 类型差异。"""

    def test_int_and_float_are_distinct(self):
        self.assertNotEqual(canonical_tree(1), canonical_tree(1.0))

    def test_bool_and_int_are_distinct(self):
        self.assertNotEqual(canonical_tree(True), canonical_tree(1))

    def test_list_order_is_significant(self):
        self.assertNotEqual(canonical_tree([1, 2]), canonical_tree([2, 1]))

    def test_unsupported_type_is_rejected_loudly(self):
        class Opaque(object):
            pass

        with self.assertRaises(TypeError):
            canonical_tree({"bad": Opaque()})

    def test_tree_text_is_unambiguous(self):
        """长度前缀必须防止相邻片段拼接歧义。"""
        self.assertNotEqual(tree_text(canonical_tree(["ab"])), tree_text(("s", "a", "b")))

    def test_tree_text_has_no_python_repr_dependency(self):
        """指纹文本必须自洽，不依赖 repr 在 py2/py3 之间的差异。"""
        self.assertEqual(tree_text(canonical_tree(1.5)), "(1:f3:1.5)")
        self.assertNotIn("0.30000000000000004", tree_text(canonical_tree(0.3)))


class TestRoundTrip(unittest.TestCase):
    """to_dict / from_dict 必须无损。"""

    def assert_round_trip(self, key):
        restored = ResourceKey.from_dict(key.to_dict())
        self.assertEqual(key, restored)
        self.assertEqual(hash(key), hash(restored))
        self.assertEqual(key.canonical(), restored.canonical())

    def test_plain_item(self):
        self.assert_round_trip(ResourceKey.item(IRON))

    def test_item_with_every_field(self):
        self.assert_round_trip(
            ResourceKey.item(
                SWORD,
                aux=3,
                enchant_data=[(9, 5), (34, 3)],
                mod_enchant_data=[("a:fire", 1)],
                custom_tips="shiny",
                extra_id="machine-a",
                user_data={"nbt": {"k": [1, 2, {"deep": True}]}},
                durability=1200,
            )
        )

    def test_item_with_empty_containers(self):
        self.assert_round_trip(
            ResourceKey.item(SWORD, enchant_data=[], user_data={})
        )

    def test_fluid(self):
        self.assert_round_trip(ResourceKey.fluid(WATER))

    def test_fluid_with_user_data(self):
        self.assert_round_trip(ResourceKey.fluid(WATER, user_data={"quality": 3}))

    def test_round_trip_preserves_empty_versus_absent(self):
        for kwargs in ({"enchant_data": None}, {"enchant_data": []}, {"user_data": {}}):
            key = ResourceKey.item(SWORD, **kwargs)
            restored = ResourceKey.from_dict(key.to_dict())
            self.assertEqual(key, restored, "%.20s 往返后身份改变" % (kwargs,))

    def test_to_dict_preserves_metadata_representation(self):
        """存档层接收原有数据类型，不为 JSON 强制转换。"""
        key = ResourceKey.item(SWORD, enchant_data=[(9, 5)], user_data={"a": [1, 2]})
        payload = key.to_dict()
        self.assertEqual(payload["identity"]["enchant_data"], [(9, 5)])
        self.assertEqual(payload["identity"]["user_data"], {"a": [1, 2]})

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            ResourceKey.from_dict({"kind": "plasma", "name": "x"})


class TestMediumIdentityProjection(unittest.TestCase):
    """介质物品的身份特例（12-02 定案、12-03 落地）。

    边界极窄，所以这里**每一侧都有反例**：只有「已识别的本模组介质 +
    带标记的管理行」这一种组合会被剔除，其余一律按原来的算法走。
    """

    TAPE = "endernet:singleside_tape"

    def _media_user_data(self, medium_id, lines):
        from endernet.common.medium_item import carrier_of, mark_managed_line

        user_data = carrier_of(_FakeMedium(medium_id))
        user_data["display"] = {
            "Name": "同一块盘",
            "Lore": ["玩家自己写的一行"] + [mark_managed_line(l) for l in lines],
        }
        return user_data

    def test_same_id_different_capacity_lines_are_the_same_key(self):
        """**核心性质**：同一块盘只因为剩余量变了就换身份，会让库存里
        凭空多出/少掉一条，并触发 remove/add 与预留重建。"""
        left = ResourceKey.item(self.TAPE, user_data=self._media_user_data("medium:1", ["12", "1728"]))
        right = ResourceKey.item(self.TAPE, user_data=self._media_user_data("medium:1", ["3", "40"]))
        self.assertEqual(left, right)
        self.assertEqual(hash(left), hash(right))

    def test_different_ids_are_different_keys(self):
        """同型号、同剩余量的**两块**盘不能被合并——稳定 ID 就是为此存在的。"""
        left = ResourceKey.item(self.TAPE, user_data=self._media_user_data("medium:1", ["12", "1728"]))
        right = ResourceKey.item(self.TAPE, user_data=self._media_user_data("medium:2", ["12", "1728"]))
        self.assertNotEqual(left, right)

    def test_other_lore_still_distinguishes(self):
        """只排除管理行——玩家自己写的那一行仍然参与身份。"""
        from endernet.common.medium_item import carrier_of

        base = carrier_of(_FakeMedium("medium:1"))
        left = dict(base, display={"Lore": ["玩家 A"]})
        right = dict(base, display={"Lore": ["玩家 B"]})
        self.assertNotEqual(
            ResourceKey.item(self.TAPE, user_data=left), ResourceKey.item(self.TAPE, user_data=right)
        )

    def test_custom_name_still_distinguishes(self):
        from endernet.common.medium_item import carrier_of

        base = carrier_of(_FakeMedium("medium:1"))
        left = dict(base, display={"Name": "甲"})
        right = dict(base, display={"Name": "乙"})
        self.assertNotEqual(
            ResourceKey.item(self.TAPE, user_data=left), ResourceKey.item(self.TAPE, user_data=right)
        )

    def test_unmanaged_item_is_completely_unaffected(self):
        """**平行对照**：没有载体的物品，Lore 里就算有一样的标记也照旧参与身份。"""
        from endernet.common.medium_item import mark_managed_line

        left = ResourceKey.item(
            IRON, user_data={"display": {"Lore": [mark_managed_line("一样的标记")]}}
        )
        right = ResourceKey.item(IRON, user_data={"display": {"Lore": []}})
        self.assertNotEqual(left, right)

    def test_other_user_data_keys_still_distinguish(self):
        """别的模组的键不归我们管，照旧参与身份。"""
        from endernet.common.medium_item import carrier_of

        base = carrier_of(_FakeMedium("medium:1"))
        left = dict(base, **{"别的模组": 1})
        right = dict(base, **{"别的模组": 2})
        self.assertNotEqual(
            ResourceKey.item(self.TAPE, user_data=left), ResourceKey.item(self.TAPE, user_data=right)
        )

    def test_raw_identity_keeps_the_managed_lines(self):
        """**投影不是清洗**：真实载荷里那两行必须原样留着。"""
        user_data = self._media_user_data("medium:1", ["12", "1728"])
        key = ResourceKey.item(self.TAPE, user_data=user_data)
        self.assertEqual(key.raw_identity["user_data"], user_data)

    def test_round_trip_preserves_identity(self):
        """`to_dict` / `from_dict` 之后身份不变——两条构造路径都要走投影。"""
        from endernet.common.resource_key import ResourceKey as RK

        key = RK.item(self.TAPE, user_data=self._media_user_data("medium:1", ["12", "1728"]))
        restored = RK.from_dict(key.to_dict())
        self.assertEqual(restored, key)
        self.assertEqual(restored.raw_identity, key.raw_identity)

    def test_a_capacity_refresh_does_not_change_the_key(self):
        """把「按最新权威摘要物化」接在身份上：刷新前后必须是同一个键。"""
        from endernet.common.medium_item import apply_capacity_lines, carrier_of

        user_data = carrier_of(_FakeMedium("medium:1"))
        before = ResourceKey.item(self.TAPE, user_data=user_data)
        after = ResourceKey.item(
            self.TAPE, user_data=apply_capacity_lines(user_data, ("12", "1728"))
        )
        self.assertEqual(before, after)


class _FakeMedium(object):
    """只需要 `medium_id`：`carrier_of` 用它发载体。"""

    __slots__ = ("medium_id",)

    def __init__(self, medium_id):
        self.medium_id = medium_id


class TestUnitScale(unittest.TestCase):
    """单位尺度：1 物品 = 50 微单位物，1 mB = 1 微单位物。"""

    def test_item_is_fifty_micro_units(self):
        self.assertEqual(item_micro_units(1), 50)
        self.assertEqual(item_micro_units(64), 3200)
        self.assertEqual(ITEM_MICRO_UNITS, 50)

    def test_fluid_mb_is_one_micro_unit(self):
        self.assertEqual(fluid_micro_units(1000), 1000)

    def test_twenty_five_mb_does_not_truncate_to_zero(self):
        """整数尺度必须保留半单位物，否则 25 mB 会被截断为 0。"""
        self.assertEqual(fluid_micro_units(25), 25)

    def test_capacity_scales_by_fifty(self):
        self.assertEqual(capacity_micro_units(1728), 86400)
        self.assertEqual(capacity_micro_units(2 ** 30), 2 ** 30 * 50)

    def test_mixed_capacity_preserves_remainder(self):
        total = item_micro_units(1728) + fluid_micro_units(25)
        self.assertEqual(divmod(total, ITEM_MICRO_UNITS), (1728, 25))
        self.assertLess(total, capacity_micro_units(1729))

    def test_resource_key_dispatch(self):
        self.assertEqual(ResourceKey.item(IRON).micro_units(3), 150)
        self.assertEqual(ResourceKey.fluid(WATER).micro_units(25), 25)

    def test_negative_and_fractional_counts_rejected(self):
        with self.assertRaises(ValueError):
            item_micro_units(-1)
        with self.assertRaises(TypeError):
            item_micro_units(1.5)
        with self.assertRaises(TypeError):
            item_micro_units(True)

    def test_accumulated_fluid_never_drifts(self):
        """逐次累加 25 mB 必须精确等于整数结果，不得出现浮点漂移。"""
        total = 0
        for _ in range(40):
            total += fluid_micro_units(25)
        self.assertEqual(total, 1000)
        self.assertEqual(total, capacity_micro_units(20))


class TestConstructionValidation(unittest.TestCase):
    def test_empty_name_rejected(self):
        with self.assertRaises(ValueError):
            ResourceKey.item("")

    def test_non_string_name_rejected(self):
        with self.assertRaises(ValueError):
            ResourceKey.item(123)

    def test_bad_enchant_entry_rejected(self):
        with self.assertRaises(ValueError):
            ResourceKey.item(SWORD, enchant_data=[(9, 5, 1)])


if __name__ == "__main__":
    unittest.main()
