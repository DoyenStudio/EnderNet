# coding=utf-8
"""介质物品的载体与展示契约（12-02 定案、12-03 落地）。

对应 `plans/technical_notes/medium_lore.md` 第 2 节与任务 12-03 的验收：
「Lore 仅为权威介质内容的派生展示……刷新容量行不覆盖名称及无关元数据」。

**这里最容易写错的两件事**，各有一组用例钉着：

1. **识别只按标记串。** 玩家和其他模组完全可能拥有同样文本的 Lore，
   也可能插进或重排自己的行。按中文前缀或按行号认领，会在真机上
   悄悄吃掉别人的 Lore。
2. **投影不是清洗。** 身份投影剔除管理行之后，**真实物品载荷一个字都不能变**
   ——`raw_identity` 里那两行必须还在。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.medium_item import (
    CARRIER_VERSION,
    MEDIUM_CARRIER_KEY,
    MANAGED_LORE_MARK,
    apply_capacity_lines,
    carrier_of,
    describe_carrier,
    is_managed_line,
    mark_managed_line,
    materialize_item_user_data,
    medium_lore_lines,
    project_identity_user_data,
    read_carrier,
    write_carrier,
)


class FakeMedium(object):
    """只要能给 `medium_id` 与 `capacity_lines()` 就够了。"""

    __slots__ = ("medium_id", "lines")

    def __init__(self, medium_id, lines=("剩余种类数：12", "剩余总数量：1728 单位物")):
        self.medium_id = medium_id
        self.lines = lines

    def capacity_lines(self):
        return self.lines


def managed(line):
    """给一行普通文本加上归属标记（测试里只走公开入口）。"""
    return mark_managed_line(line)


class TestCarrier(unittest.TestCase):
    def test_round_trip(self):
        user_data = write_carrier({"display": {"Name": "我的盘"}}, "medium:7")
        self.assertEqual(read_carrier(user_data), ("medium:7", CARRIER_VERSION))

    def test_absent_carrier_is_not_managed(self):
        self.assertIsNone(read_carrier({"display": {"Lore": ["普通说明"]}}))
        self.assertIsNone(read_carrier(None))
        self.assertIsNone(read_carrier({}))

    def test_malformed_carrier_is_not_managed(self):
        """读不懂的载体按「不是本模组管理的介质」处理，**不猜**。"""
        for broken in (
            {MEDIUM_CARRIER_KEY: "medium:7"},
            {MEDIUM_CARRIER_KEY: {"id": "", "v": 1}},
            {MEDIUM_CARRIER_KEY: {"id": "medium:7"}},
            {MEDIUM_CARRIER_KEY: {"id": "medium:7", "v": "1"}},
            {MEDIUM_CARRIER_KEY: {"id": "medium:7", "v": True}},
        ):
            self.assertIsNone(read_carrier(broken), broken)

    def test_reading_never_invents_an_id(self):
        """**读取路径永不生成 ID**：空载体读出来是 None，不是新号。"""
        self.assertIsNone(read_carrier({"display": {}}))
        self.assertIsNone(read_carrier({MEDIUM_CARRIER_KEY: {}}))

    def test_non_ascii_id_is_refused(self):
        with self.assertRaises(ValueError):
            write_carrier({}, u"介质:1")

    def test_write_does_not_mutate_the_input(self):
        original = {"display": {"Name": "x"}}
        write_carrier(original, "medium:1")
        self.assertNotIn(MEDIUM_CARRIER_KEY, original)

    def test_write_keeps_other_fields(self):
        user_data = write_carrier({"display": {"Name": "x"}, "其他模组的键": 1}, "medium:3")
        self.assertEqual(user_data["display"], {"Name": "x"})
        self.assertEqual(user_data["其他模组的键"], 1)

    def test_carrier_of_uses_the_medium_id(self):
        carrier = carrier_of(FakeMedium("medium:42"))
        self.assertEqual(read_carrier(carrier)[0], "medium:42")

    def test_describe_reports_non_managed_honestly(self):
        self.assertEqual(
            describe_carrier({"display": {}}),
            {"managed": False, "medium_id": None, "version": None},
        )


class TestManagedLines(unittest.TestCase):
    def test_marker_is_recognised(self):
        self.assertTrue(is_managed_line(managed("剩余种类数：12")))
        self.assertFalse(is_managed_line("剩余种类数：12"))

    def test_recognition_is_by_marker_only(self):
        """判据**只有标记串**：同样文本不带标记的不是我们的（玩家可以照抄），
        而光秃秃一个标记串**是**我们的（标记本身就是全部判据，没有额外条件）。"""
        self.assertFalse(is_managed_line("剩余种类数：12"))
        self.assertFalse(is_managed_line(u"剩余种类数：12"))
        self.assertTrue(is_managed_line(MANAGED_LORE_MARK))

    def test_marker_must_be_at_the_end(self):
        """标记在行中间不算：那不可能是我们写的形状。"""
        text = MANAGED_LORE_MARK + "剩余种类数：12"
        self.assertFalse(is_managed_line(text))

    def test_non_text_is_not_managed(self):
        for value in (None, 12, [], {"a": 1}):
            self.assertFalse(is_managed_line(value))

    def test_marking_a_non_text_raises(self):
        with self.assertRaises(TypeError):
            mark_managed_line(None)

    def test_marked_line_keeps_its_text(self):
        line = "剩余总数量：1728 单位物"
        self.assertTrue(mark_managed_line(line).startswith(line))

    def test_lore_lines_carry_the_marker(self):
        lines = medium_lore_lines(FakeMedium("medium:1"))
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(is_managed_line(line) for line in lines))
        self.assertIn("1728", lines[1])


class TestIdentityProjection(unittest.TestCase):
    def _media_user_data(self, medium_id, lines, extra_lore=(), extra_display=None):
        user_data = carrier_of(FakeMedium(medium_id, lines))
        display = dict(extra_display or {})
        lore = list(extra_lore) + [managed(line) for line in lines]
        display["Lore"] = lore
        user_data["display"] = display
        return user_data

    def test_managed_lines_are_removed_from_the_projection(self):
        user_data = self._media_user_data(
            "medium:1", ("a", "b"), extra_lore=["玩家自己写的一行"]
        )
        projected = project_identity_user_data(user_data)
        self.assertEqual(projected["display"]["Lore"], ["玩家自己写的一行"])

    def test_other_display_fields_survive(self):
        user_data = self._media_user_data(
            "medium:1", ("a", "b"), extra_display={"Name": "我的盘"}
        )
        projected = project_identity_user_data(user_data)
        self.assertEqual(projected["display"]["Name"], "我的盘")

    def test_empty_lore_and_display_are_omitted(self):
        """剔除后为空 → 整个键省略，与「本来就没有」同形。"""
        user_data = self._media_user_data("medium:1", ("a", "b"))
        projected = project_identity_user_data(user_data)
        self.assertNotIn("display", projected)

    def test_empty_lore_but_other_display_fields_keeps_display(self):
        user_data = self._media_user_data(
            "medium:1", ("a", "b"), extra_display={"Name": "x"}
        )
        projected = project_identity_user_data(user_data)
        self.assertNotIn("Lore", projected["display"])
        self.assertEqual(projected["display"], {"Name": "x"})

    def test_carrier_stays_in_the_projection(self):
        """稳定 ID **参与**身份——正是它让两块同型号的盘不被合并。"""
        user_data = self._media_user_data("medium:1", ("a", "b"))
        self.assertEqual(read_carrier(project_identity_user_data(user_data))[0], "medium:1")

    def test_unmanaged_items_are_untouched(self):
        """**边界：只对已识别的本模组介质生效。** 别人的物品一个字节都不动。"""
        user_data = {"display": {"Lore": [managed("碰巧长得一样")]}}
        self.assertIs(project_identity_user_data(user_data), user_data)

    def test_item_without_managed_lines_is_returned_as_is(self):
        user_data = self._media_user_data("medium:1", ("a", "b"), extra_lore=["只此一行"])
        user_data["display"]["Lore"] = ["只此一行"]
        self.assertIs(project_identity_user_data(user_data), user_data)

    def test_projection_does_not_mutate_the_payload(self):
        """投影是**只读**的：真实载荷必须原样留着。"""
        user_data = self._media_user_data(
            "medium:1", ("a", "b"), extra_lore=["玩家自己写的一行"]
        )
        before = list(user_data["display"]["Lore"])
        project_identity_user_data(user_data)
        self.assertEqual(user_data["display"]["Lore"], before)


class TestCapacityLineWriteBack(unittest.TestCase):
    def test_writes_two_marked_lines(self):
        user_data = apply_capacity_lines({}, ("剩余种类数：12", "剩余总数量：1728 单位物"))
        lore = user_data["display"]["Lore"]
        self.assertEqual(len(lore), 2)
        self.assertTrue(all(is_managed_line(line) for line in lore))

    def test_preserves_other_lore_and_name(self):
        original = {"display": {"Name": "我的盘", "Lore": ["玩家自己写的一行"]}}
        user_data = apply_capacity_lines(original, ("a", "b"))
        self.assertEqual(user_data["display"]["Name"], "我的盘")
        self.assertIn("玩家自己写的一行", user_data["display"]["Lore"])
        self.assertEqual(len(user_data["display"]["Lore"]), 3)

    def test_replaces_previous_managed_lines(self):
        """重复刷新**幂等**：不会一路追加（验收明写）。"""
        first = apply_capacity_lines({}, ("旧的种类数", "旧的总数量"))
        second = apply_capacity_lines(first, ("新的种类数", "新的总数量"))
        lore = second["display"]["Lore"]
        self.assertEqual(len(lore), 2)
        self.assertNotIn(mark_managed_line("旧的种类数"), lore)
        self.assertEqual(second, apply_capacity_lines(second, ("新的种类数", "新的总数量")))

    def test_does_not_mutate_the_input(self):
        original = {"display": {"Name": "x"}}
        apply_capacity_lines(original, ("a", "b"))
        self.assertEqual(original, {"display": {"Name": "x"}})

    def test_materialize_writes_carrier_and_lines_together(self):
        medium = FakeMedium("medium:9")
        user_data = materialize_item_user_data(
            medium, {"display": {"Name": "我的盘", "Lore": ["玩家的一行"]}}
        )
        self.assertEqual(read_carrier(user_data)[0], "medium:9")
        lore = user_data["display"]["Lore"]
        self.assertEqual(len(lore), 3)
        self.assertTrue(is_managed_line(lore[2]))
        # 两行都来自介质自己的权威摘要，不是网络汇总值。
        self.assertIn("1728", lore[2])

    def test_materialize_is_idempotent(self):
        medium = FakeMedium("medium:9")
        once = materialize_item_user_data(medium, {})
        twice = materialize_item_user_data(medium, once)
        self.assertEqual(once, twice)


class TestProjectionAndWriteBackAgree(unittest.TestCase):
    """投影与写回必须是一对逆：物化之后身份不该再随着刷新改变。"""

    def test_capacity_only_change_does_not_change_the_projection(self):
        medium = FakeMedium("medium:5")
        user_data = materialize_item_user_data(medium, {"display": {"Name": "x"}})
        refreshed = apply_capacity_lines(user_data, ("换了数字", "也换了数字"))
        self.assertEqual(
            project_identity_user_data(user_data),
            project_identity_user_data(refreshed),
        )


if __name__ == "__main__":
    unittest.main()
