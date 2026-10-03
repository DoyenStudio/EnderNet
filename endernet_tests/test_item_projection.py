# coding=utf-8
"""终端网络列表的显示投影：真实库存 ∪ 网络样板产物（任务 11 增量）。

权威规则见 `plans/technical_notes/terminal_pattern_order_preview.md` 第 1 节。
**全部离线**：投影只读口不含引擎，夹具用真实的 `StorageService`。

这一套用例守的是几件「看着对、真机上会出事」的事：

1. **纯样板行写的是真实的 0。** 只有 `+` 的行 `stored` 必须是 0——
   写成 1 就等于在显示层伪造库存，取物与容量判断都会跟着错。
2. **删行的判据是「库存与样板都没有」。** 库存取空但样板还在时要留着，
   最后一份样板撤掉才删；多个样板产出同一个键时删一个不能误清。
3. **纯样板变化要能开一个发布窗口。** 库存一点没动，界面也得知道多了一行。
4. **设备换网不是样板变化。** 内容修订号不动，只有成员签名看得出来。
5. **显示版本不等于交易版本。** 会话握手读的是后者，发布窗口推的是前者。

行是四元组 `(key, stored, reserved, has_pattern)`，与线上 `_encode_item` 同形。
"""

import unittest

import testsupport

from endernet.common.identity import NodeKey
from endernet.common.resource_key import ResourceKey

IRON_BLOCK = ResourceKey.item("minecraft:iron_block")
IRON = ResourceKey.item("minecraft:iron_ingot")
STONE = ResourceKey.item("minecraft:stone")
WATER = ResourceKey.fluid("minecraft:water")


def key(x):
    return NodeKey(0, x, 64, 0)


class ProjectionCase(unittest.TestCase):
    def setUp(self):
        self.stand = testsupport.StorageStand()
        self.stand.place(0)
        self.stand.new_medium()
        self.stand.attach()
        self.service = self.stand.service
        self.display = testsupport.DisplayStand(self.service, "net-1")
        self.view = self.display.projection

    def row_of(self, key_):
        # type: (object) -> object
        for row in self.view.read_items((key_,)):
            return row
        return None

    def all_rows(self):
        # type: () -> tuple
        return self.view.iter_item_records()

    # -- 并集与删除判据 ------------------------------------------------

    def test_a_pattern_only_row_is_a_real_zero(self):
        """只样板没库存的行：数量是 0，标记是真的。**不是 1，也不是没有。**"""
        self.display.mark(IRON_BLOCK)
        self.assertEqual(self.row_of(IRON_BLOCK), (IRON_BLOCK, 0, 0, True))

    def test_stock_without_a_pattern_keeps_the_old_row(self):
        self.stand.seed(STONE, 5)
        self.assertEqual(self.row_of(STONE), (STONE, 5, 0, False))

    def test_stock_and_pattern_together_carry_both(self):
        self.stand.seed(IRON_BLOCK, 7)
        self.display.mark(IRON_BLOCK)
        self.assertEqual(self.row_of(IRON_BLOCK), (IRON_BLOCK, 7, 0, True))

    def test_emptying_the_stock_keeps_the_row_while_the_pattern_stays(self):
        """库存取空 → 行变成 `0/0/True`，**不是**消失（契约第 1 节）。"""
        self.stand.seed(IRON_BLOCK, 3)
        self.display.mark(IRON_BLOCK)
        self.service.commit_extract("take", IRON_BLOCK, 3, now_tick=99)
        self.view.take_dirty_keys()
        self.assertEqual(self.row_of(IRON_BLOCK), (IRON_BLOCK, 0, 0, True))

    def test_the_row_goes_away_only_when_both_stock_and_pattern_are_gone(self):
        self.stand.seed(IRON_BLOCK, 3)
        self.display.mark(IRON_BLOCK)
        self.service.commit_extract("take", IRON_BLOCK, 3, now_tick=99)
        self.assertIsNotNone(self.row_of(IRON_BLOCK), "样板还在，行要留着")
        self.display.unmark(IRON_BLOCK)
        self.view.take_dirty_keys()
        self.assertIsNone(self.row_of(IRON_BLOCK), "两样都没有了才删")

    def test_a_key_with_neither_is_not_a_row_even_if_asked_for(self):
        """发布窗口会把「消失的脏键」拿来问值；这里必须回「没有」。"""
        self.assertEqual(self.view.read_items((IRON_BLOCK,)), ())
        self.assertEqual(self.all_rows(), ())

    def test_read_items_keeps_the_order_it_was_asked(self):
        self.stand.seed(STONE, 1)
        self.stand.seed(IRON, 2)
        self.display.mark(IRON_BLOCK)
        rows = self.view.read_items((IRON_BLOCK, STONE, IRON))
        self.assertEqual(
            [row[0] for row in rows], [IRON_BLOCK, STONE, IRON]
        )

    def test_the_snapshot_holds_the_union_in_canonical_order(self):
        self.stand.seed(STONE, 4)
        self.display.mark(IRON_BLOCK)
        self.assertEqual(
            self.all_rows(),
            tuple(
                sorted(
                    ((STONE, 4, 0, False), (IRON_BLOCK, 0, 0, True)),
                    key=lambda row: row[0].canonical(),
                )
            ),
        )

    def test_a_fluid_product_is_not_shown(self):
        """契约第 1 节：网络列表暂不扩展流体显示。"""
        self.display.mark(WATER)
        self.assertEqual(self.all_rows(), ())

    # -- 变更检测 ------------------------------------------------------

    def test_a_pure_pattern_change_opens_a_window(self):
        """库存一点没动，只多了样板——界面也得知道。"""
        self.assertEqual(self.view.take_dirty_keys(), ())
        self.display.mark(IRON_BLOCK)
        self.assertTrue(self.view.has_dirty_items())
        self.assertEqual(self.view.take_dirty_keys(), (IRON_BLOCK,))
        self.assertEqual(self.view.publish_version, 1)

    def test_no_change_means_no_window_and_no_version_bump(self):
        self.stand.seed(STONE, 1)
        self.view.take_dirty_keys()
        version = self.view.publish_version
        for _ in range(3):
            self.assertFalse(self.view.has_dirty_items())
            self.assertEqual(self.view.take_dirty_keys(), ())
        self.assertEqual(self.view.publish_version, version)

    def test_taking_a_pattern_away_is_a_dirty_key_too(self):
        self.display.mark(IRON_BLOCK)
        self.view.take_dirty_keys()
        self.display.unmark(IRON_BLOCK)
        self.assertEqual(self.view.take_dirty_keys(), (IRON_BLOCK,))

    def test_a_second_request_for_a_key_is_deduplicated(self):
        self.display.mark(IRON_BLOCK)
        self.display.mark(IRON_BLOCK)
        self.assertEqual(self.view.take_dirty_keys(), (IRON_BLOCK,))

    def test_membership_changes_are_seen_without_a_revision_bump(self):
        """设备换网时样板内容一字未变，只有成员签名变了。

        `products_of` 在这里按**成员**回答，因此搬走最后一台提供者
        就等于这个产物不再属于本网络。
        """
        provider = key(10)
        self.display.products.add(IRON_BLOCK)
        self.display.join(provider)
        # 修订号此时还是上一次的值：投影必须先靠签名看出来。
        self.display.revision += 1
        self.assertEqual(self.view.take_dirty_keys(), (IRON_BLOCK,))
        self.assertEqual(self.row_of(IRON_BLOCK), (IRON_BLOCK, 0, 0, True))

        # 搬走：内容没变（修订号不动），成员签名的变化要能看见。
        self.display.products.discard(IRON_BLOCK)
        self.display.leave(provider)
        self.assertEqual(self.view.take_dirty_keys(), (IRON_BLOCK,))
        self.assertIsNone(self.row_of(IRON_BLOCK))

    # -- 两个版本 ------------------------------------------------------

    def test_the_display_version_is_not_the_transaction_version(self):
        """契约第 6 节：显示版本不能冒充交易版本。

        会话握手读 `index_version`，发布窗口推 `publish_version`——
        两者是两个属性，且各自独立推进。
        """
        self.stand.seed(STONE, 1)
        self.view.take_dirty_keys()
        self.assertEqual(self.view.publish_version, 1)
        index_before = self.view.index_version
        self.assertGreaterEqual(index_before, 0)

        # 只改样板：显示版本走一格，交易版本一动不动。
        self.display.mark(IRON_BLOCK)
        self.view.take_dirty_keys()
        self.assertEqual(self.view.publish_version, 2)
        self.assertEqual(self.view.index_version, index_before)

    def test_the_transaction_version_still_follows_the_storage_service(self):
        before = self.view.index_version
        self.stand.seed(STONE, 1)
        self.assertGreater(self.view.index_version, before)

    def test_without_a_storage_service_the_display_still_works(self):
        """运行时还没装配好时：`index_version` 回 0，样板行照常显示。"""
        from endernet.server.storage.projection import NetworkItemProjection

        view = NetworkItemProjection(
            "net-x",
            storage_of=lambda: None,
            members_of=lambda: (),
            products_of=lambda members: (IRON_BLOCK,),
        )
        self.assertEqual(view.index_version, 0)
        self.assertEqual(view.read_items((IRON_BLOCK,)), ((IRON_BLOCK, 0, 0, True),))


if __name__ == "__main__":
    unittest.main()
