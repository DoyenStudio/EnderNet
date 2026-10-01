# coding=utf-8
"""真实玩家背包模型：槽序换算、选中语义、以及「这一点击要存什么」。

面板那一半在 `test_terminal_panel.py`（走到发命令），这里只守着模型自己。
三条边界值得单独有个文件，因为它们**在游戏里都不会报错**：

- **槽序**（`engine_slot_of`）。引擎给的是快捷栏在前，界面是背包在前。写反
  了服务端照单全收——客户端自己算的 slot 与指纹当然一致——界面也照样画，
  只有逐格实点才看得出东西搬错了格。这里钉住四个端点。
- **选中态不冻结物品**。`pending_take` 重读引擎再给结果，于是「选中之后那
  一格被换掉/被拿走」在本地就能发现，而不是发一条注定被拒的命令出去。
- **读失败时保持上一帧**。读不到 36 格时宁可显示旧的，也不能把玩家的背包
  画成空的——那会让玩家以为东西没了。
- **背包内动作的判定**（2026-09-30 起）。选中一个槽之后再点另一个槽，这一下
  是合并／交换，不再是改选中。判据写错的表现是**发错动作**：合并发成交换、
  或者干脆发成 `inv_take` 把东西悄悄存进网络。而越界下标尤其阴——`self._slots[-1]`
  在 Python 里取的是**最后一格**，不报错。

引擎那一侧的替身是 `_read_engine_items`（模块里的函数，导入在函数体内），
因此本文件不必装 `tooldelta` 的替身也能跑；唯一要装的是
`tooldelta.define.item.Item`，因为「一组是多少」由 `stack_size` 问它。
"""

import os
import sys
import types
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import testsupport  # noqa: E402  设置 sys.path，并提供引擎物品字典的实测形状

from endernet_bp.endernet_scripts.endernet.client.ui import (  # noqa: E402
    backpack_model, stack_size,
)

#: `stack_size.basic_info` 里那句 `from ....tooldelta.define.item import Item`
#: 解析出来的模块名。**必须与那四个点算出来的完全一致**，少一层就会落回
#: 「导入失败 → 上限退成 1」，而上限退成 1 的用例看起来只是断言写错了。
ITEM_MODULE = "endernet_bp.endernet_scripts.tooldelta.define.item"


class ItemStub(object):
    """只实现 `stack_size` 真正会碰到的那一项。"""

    #: 资源名 → `maxStackSize`，没登记的一律 64。**默认值 64 比用例里的数量
    #: 都大，所以上限那一支只有自己登记一条小堆叠时才走得到**（工具、桶
    #: 都是 1）——不登记就永远测不到上限，而用例照样是绿的。
    stack_sizes = {}

    def __init__(self, item_id, count=1, orig=None):
        self.newItemName = item_id
        self.count = count
        #: 引擎给的原始字典。身份靠它算（`engine_item_key`），所以替身必须
        #: 原样收下——用属性拼一份「看起来一样」的算不出同一个指纹。
        self._orig = orig if orig is not None else {}

    def GetBasicInfo(self):
        return types.SimpleNamespace(
            itemName=self.newItemName, id_aux=0,
            maxStackSize=self.stack_sizes.get(self.newItemName, 64),
        )


class BackpackModelTests(unittest.TestCase):
    def setUp(self):
        # `stack_size` 的缓存挂在**模块**上（不跟模型走），会跨用例存活：
        # 不在这里清掉的话，某条用例压低的堆叠上限会漏给后面所有用例，而
        # 那种失败看起来像「上限算错了」。
        self.addCleanup(stack_size.reset)
        stack_size.reset()
        self.addCleanup(ItemStub.stack_sizes.clear)
        # 用 `fake_modules` 而**不是** `install_fake_modules`：这个名字
        # `test_terminal_panel` 也装着，而且那一份是常驻的（模块导入时装一次）。
        # 用完直接 pop 会把它的替身一并删掉，于是那个文件里所有用例都开始
        # 「导入失败 → 堆叠上限退成 1」——看起来像是上限算错了。
        # `fake_modules` 退出时会把原来那一份放回去。
        stubs = testsupport.fake_modules(ITEM_MODULE)
        installed = stubs.__enter__()
        installed[ITEM_MODULE].Item = ItemStub
        self.addCleanup(stubs.__exit__, None, None, None)
        #: 假的引擎读数。**引擎槽序**：0–8 是快捷栏、9–35 是背包，与界面
        #: 那两段的顺序正好相反。
        self.engine = [None] * backpack_model.SLOT_COUNT
        #: 置真后 `_fake_read` 抛，模拟引擎调用本身失败。
        self.broken = False
        patcher = patch.object(backpack_model, "_read_engine_items", self._fake_read)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.model = backpack_model.BackpackModel()

    def _fake_read(self):
        if self.broken:
            raise RuntimeError("engine call failed")
        return self.engine

    def put(self, slot, name, count=1, **identity):
        # type: (int, str, int, object) -> None
        """往**引擎那一侧**放一件东西。模型要重读才看得到。"""
        self.engine[slot] = ItemStub(
            name, count, orig=testsupport.engine_item(name, count, **identity))

    def clear(self, slot):
        # type: (int) -> None
        self.engine[slot] = None

    def read(self):
        # type: () -> list
        return self.model.replace_from_engine()

    # -- 槽序 ----------------------------------------------------------

    def test_ui_slots_map_onto_the_engine_slots_players_actually_have(self):
        """四个端点。**这是会静默出错的那条换算。**

        客户端用同一张表同时算出 `slot` 与 `key`，而服务端的锚只回答「这个
        槽里是不是这件东西」——自己和自己必然一致。于是映射错了：服务端
        核对通过、离线用例全绿、界面也照画，只有**东西搬错了格**。
        """
        self.assertEqual(backpack_model.engine_slot_of(0), 9)
        self.assertEqual(backpack_model.engine_slot_of(26), 35)
        self.assertEqual(backpack_model.engine_slot_of(27), 0)
        self.assertEqual(backpack_model.engine_slot_of(35), 8)
        # `True` 是 `int` 的子类：不显式挡掉的话它会当成界面 1 号槽。
        for bad in (-1, 36, True, False, "0", None, 1.0):
            with self.assertRaises(ValueError):
                backpack_model.engine_slot_of(bad)

    def test_the_two_slot_orders_are_not_confused(self):
        """引擎的 0–8 是**快捷栏**，界面的 0–26 才是背包。

        读的时候少一次换算、或者换算写反，表现都是**整块背包上下颠倒**：
        上面三排显示的是快捷栏，下面那一排显示的是背包的头九格。玩家第一
        眼就该看出不对——但没有任何日志会提示。
        """
        self.put(0, "minecraft:apple")     # 引擎 0 = 快捷栏第 1 格 → 界面 27
        self.put(8, "minecraft:bread")     # 引擎 8 = 快捷栏最后 1 格 → 界面 35
        self.put(9, "minecraft:stone")     # 引擎 9 = 背包第 1 格 → 界面 0
        self.put(35, "minecraft:dirt")     # 引擎 35 = 背包最后 1 格 → 界面 26
        self.read()
        slots = self.model.snapshot()
        self.assertEqual(slots[27], ("minecraft:apple", 1))
        self.assertEqual(slots[35], ("minecraft:bread", 1))
        self.assertEqual(slots[0], ("minecraft:stone", 1))
        self.assertEqual(slots[26], ("minecraft:dirt", 1))
        # 四个端点之外全是空的：颠倒过来写的话这里每一格都会错位。
        for index in (1, 25, 28, 34):
            self.assertIsNone(slots[index])

    # -- 读取 ----------------------------------------------------------

    def test_only_real_changes_are_reported(self):
        """「有没有变」按**身份与数量**比，不按对象比。

        每次读都会造一批新的 `Item`，按对象比会让这个函数恒为「全变了」。
        那不会报错，只会让背包每帧全量重画（每个格子一次 `SetUiItem`）。
        """
        self.put(0, "minecraft:apple", 5)
        self.assertEqual(self.read(), [27])
        self.assertEqual(self.read(), [])
        self.put(0, "minecraft:apple", 4)
        self.assertEqual(self.read(), [27])
        self.clear(0)
        self.assertEqual(self.read(), [27])
        self.assertEqual(self.read(), [])

    def test_a_read_that_is_not_thirty_six_slots_is_refused_whole(self):
        """条数或读数本身不对时**整块保持上一帧**，只计数。

        条数不对尤其不能当成「后面那些格子空了」——那正是把玩家的背包画成
        空的那条路，而引擎只是这一次没给全。
        """
        self.put(9, "minecraft:stone", 3)
        self.read()
        for broken in ("short", "failed"):
            if broken == "short":
                self.engine = [None] * (backpack_model.SLOT_COUNT - 1)
            else:
                self.broken = True
            self.assertEqual(self.read(), [])
            self.assertEqual(self.model.snapshot()[0], ("minecraft:stone", 3))
        self.assertEqual(self.model.errors, 2)

    def test_one_unreadable_slot_does_not_cost_the_others(self):
        """单格身份算不出来时**只跳过那一格**，保留它上一帧的样子。

        `normalize_engine_item` 对未登记的字段会抛，那是有意的严格性（不静默
        丢弃潜在身份数据）。但那个守卫该炸的是日志，不是玩家的背包面板：
        整块不画会让玩家以为背包空了。计数留给 `diagnostics()`。
        """
        self.put(9, "minecraft:stone", 3)
        self.put(10, "minecraft:dirt", 4)
        self.read()
        self.engine[9] = ItemStub("minecraft:stone", 3, orig={
            "newItemName": "minecraft:stone", "newAuxValue": 0, "count": 3,
            "showInHand": True, "unknownField": 1,
        })
        self.put(10, "minecraft:dirt", 7)
        self.assertEqual(self.read(), [1])
        slots = self.model.snapshot()
        self.assertEqual(slots[0], ("minecraft:stone", 3))
        self.assertEqual(slots[1], ("minecraft:dirt", 7))
        self.assertEqual(self.model.errors, 1)

    def test_an_empty_stack_reads_as_an_empty_slot(self):
        """`item_key_from_dict` 说「这是空的」，这一格就该是空的。

        留一个 `key` 为 `None` 的 entry 下去是颗地雷：`pending_take` 里那句
        `entry.key.name` 谁也看不出它会炸，而且它要等玩家点击时才炸。所以
        「空」在模型里只有一种表达——这一槽就是 `None`。

        **实测的 16 字段字典走不到这一支**：`is_empty_item` 判空之后
        `normalize_engine_item` 原样转出，那 6 个派生字段会让 `_check_fields`
        抛，于是落进「保留上一帧」那条路（见上一条用例）。这里造的是**正好
        十个字段**的形状，也就是 `item_codec` 自己认得全的那一种。
        """
        self.put(9, "minecraft:stone", 3)
        self.read()
        self.engine[9] = ItemStub("minecraft:air", 1, orig={
            "newItemName": "minecraft:air", "newAuxValue": 0, "count": 1,
            "showInHand": True, "enchantData": [], "modEnchantData": [],
            "customTips": "", "extraId": "", "userData": None, "durability": 0,
        })
        self.assertEqual(self.read(), [0])
        self.assertIsNone(self.model.snapshot()[0])
        self.assertIsNone(self.model.entry(0))

    # -- 选中 ----------------------------------------------------------

    def test_an_empty_slot_does_not_move_the_selection(self):
        """点空格子**不是**撤销上一次选择，也不能把选中挪到空格上。

        选中一个空格意味着下一次点终端行会去搬一格空气；而「同格再点 = 取消」
        是取消，不是「选中这一格第二次」。
        """
        self.put(9, "minecraft:stone", 3)
        self.read()
        self.assertIsNone(self.model.selected)
        self.assertEqual(self.model.select(0), 0)
        self.assertEqual(self.model.select(1), 0)       # 界面 1 = 引擎 10，空的
        self.assertIsNone(self.model.select(0))         # 同格再点 = 取消
        self.assertEqual(self.model.select(0), 0)
        self.model.clear_selection()
        self.assertIsNone(self.model.selected)

    def test_a_bad_index_leaves_the_selection_alone(self):
        """越界与非法类型既不改选中，也不能被当成第 0 格。

        `#collection_index` 缺失时是 `None`——`None` 一旦被当成 0，玩家松开
        鼠标的那一下就会选中背包第一格。
        """
        self.put(9, "minecraft:stone", 3)
        self.read()
        self.model.select(0)
        for bad in (-1, 36, None, True, "0"):
            self.assertIsNone(self.model.entry(bad))
            self.assertEqual(self.model.select(bad), 0)
        self.assertEqual(self.model.selected, 0)

    def test_replacing_the_selected_slot_drops_the_selection(self):
        """选中的那一格换了东西就得清掉选中。

        不清的话，下一次点击会拿一件玩家**没选过**的物品去交接——而且本地
        看起来完全正常，只有服务端的锚会拒绝。
        """
        self.put(9, "minecraft:stone", 3)
        self.read()
        self.model.select(0)
        self.put(9, "minecraft:dirt", 1)
        self.read()
        self.assertIsNone(self.model.selected)

    def test_another_slot_changing_does_not_drop_the_selection(self):
        """别处抖动一下就吞掉玩家的选中，玩家会觉得界面在自己乱动。

        判据是「**那一格**的 key 变了」，不是「背包有任何变化」。
        """
        self.put(9, "minecraft:stone", 3)
        self.put(10, "minecraft:dirt", 1)
        self.read()
        self.model.select(0)
        self.put(10, "minecraft:dirt", 9)
        self.clear(11)
        self.read()
        self.assertEqual(self.model.selected, 0)

    def test_the_selection_revision_moves_only_on_real_changes(self):
        """修订号是给回执用的：它回答「这条命令发出去之后，玩家还动过选中
        没有」。因此**值没变就不该记**——否则一次无谓的 `clear_selection()`
        会把别人的回执判成「玩家动过选中」，该清的就不清了。

        为什么不能拿槽号去比：玩家「取消选中 → 又选中同一格」之后，槽号与
        发命令时一模一样，而那是两次不同的意图。
        """
        self.put(9, "minecraft:stone", 3)
        self.read()
        start = self.model.selection_revision()
        self.model.clear_selection()              # 本来就没选中
        self.assertEqual(self.model.selection_revision(), start, "值没变不记")
        self.model.select(0)
        picked = self.model.selection_revision()
        self.assertGreater(picked, start, "真的改了要记")
        self.model.select(0)                      # 同格再点 = 取消
        self.assertGreater(self.model.selection_revision(), picked)
        settled = self.model.selection_revision()
        self.model.clear_selection()              # 已经是 None 了
        self.assertEqual(self.model.selection_revision(), settled)

    def test_remaking_the_same_selection_still_moves_the_revision(self):
        """**「取消又选回同一格」必须算两次变化。** 这正是拿槽号比对拦不住的
        那一种：比出来的值完全相同。"""
        self.put(9, "minecraft:stone", 3)
        self.read()
        self.model.select(0)
        before = self.model.selection_revision()
        self.model.clear_selection()
        self.model.select(0)
        self.assertEqual(self.model.selected, 0)
        self.assertGreater(self.model.selection_revision(), before)

    # -- 背包内部的动作 ------------------------------------------------

    def test_a_second_click_before_selecting_is_not_an_exchange(self):
        """没有选中时点任何格子都不是背包内动作——那一下是**选中**。"""
        self.put(9, "minecraft:stone", 3)
        self.read()
        self.assertFalse(self.model.intends_exchange(0))
        self.assertIsNone(self.model.pending_exchange(0))

    def test_clicking_the_selected_slot_again_is_a_cancel_not_an_exchange(self):
        self.put(9, "minecraft:stone", 3)
        self.put(10, "minecraft:dirt", 1)
        self.read()
        self.model.select(0)
        self.assertFalse(self.model.intends_exchange(0))
        self.assertIsNone(self.model.pending_exchange(0))

    def test_a_bad_target_index_is_not_an_exchange(self):
        """越界与非法类型不能被当成槽 0：`self._slots[-1]` 在 Python 里**不报错**。"""
        self.put(9, "minecraft:stone", 3)
        self.read()
        self.model.select(0)
        for bad in (-1, 36, None, True, "0"):
            self.assertFalse(self.model.intends_exchange(bad))
            self.assertIsNone(self.model.pending_exchange(bad))

    def test_different_items_swap_and_the_source_is_reported(self):
        self.put(9, "minecraft:stone", 5)
        self.put(10, "minecraft:dirt", 3)
        self.read()
        self.model.select(0)
        self.assertTrue(self.model.intends_exchange(1))
        self.assertEqual(
            self.model.pending_exchange(1),
            (backpack_model.ACTION_SWAP, 0, None),
        )

    def test_an_empty_target_swaps_too_because_a_move_is_a_swap(self):
        """**移动不是第六个动作**：与空槽交换就是移动。"""
        self.put(9, "minecraft:stone", 5)
        self.read()
        self.model.select(0)
        self.assertEqual(
            self.model.pending_exchange(1),
            (backpack_model.ACTION_SWAP, 0, None),
        )

    def test_the_same_item_merges_with_the_sources_count(self):
        self.put(9, "minecraft:stone", 5)
        self.put(10, "minecraft:stone", 3)
        self.read()
        self.model.select(0)
        self.assertEqual(
            self.model.pending_exchange(1),
            (backpack_model.ACTION_MERGE, 0, 5),
        )

    def test_a_merge_is_capped_by_the_targets_room(self):
        """数量在这里就封好：不封口会让界面每合并一次报一次「部分成功」。"""
        ItemStub.stack_sizes["minecraft:stone"] = 8
        self.put(9, "minecraft:stone", 6)
        self.put(10, "minecraft:stone", 5)
        self.read()
        self.model.select(0)
        self.assertEqual(
            self.model.pending_exchange(1),
            (backpack_model.ACTION_MERGE, 0, 3),
        )

    def test_a_full_target_of_the_same_item_is_not_an_action_at_all(self):
        """同类但目标满了：搬不动。发出去只会换一张 `NO_CAPACITY` 回执，
        而玩家什么也没做错——**更不能改选中**。"""
        ItemStub.stack_sizes["minecraft:stone"] = 4
        self.put(9, "minecraft:stone", 3)
        self.put(10, "minecraft:stone", 4)
        self.read()
        self.model.select(0)
        self.assertTrue(self.model.intends_exchange(1), "这一下确实点了另一个槽")
        self.assertIsNone(self.model.pending_exchange(1))
        self.assertEqual(self.model.selected, 0, "什么都没发生，选中留着")

    def test_the_source_is_reread_not_frozen_at_selection_time(self):
        """选中只记槽号。选中之后源槽换了东西，这里发的是**新读到的**那一件。

        冻一份快照下来等于用旧数据代替真身，而那正是 06 禁止的「以客户端
        显示索引代替」的另一种写法。
        """
        self.put(9, "minecraft:stone", 5)
        self.put(10, "minecraft:dirt", 1)
        self.read()
        self.model.select(0)
        self.put(9, "minecraft:stone", 2)
        action, source, count = self.model.pending_exchange(1)
        self.assertEqual((action, source, count), (backpack_model.ACTION_SWAP, 0, None))
        # 目标也重读了：泥巴从 1 变成 7，仍然是异种 → 交换。
        self.put(10, "minecraft:dirt", 7)
        self.assertEqual(self.model.pending_exchange(1)[0], backpack_model.ACTION_SWAP)

    def test_a_source_emptied_after_selection_yields_nothing(self):
        """选中之后那一格空了：本地就该发现，不发命令、也不留一个死选中。"""
        self.put(9, "minecraft:stone", 5)
        self.put(10, "minecraft:dirt", 1)
        self.read()
        self.model.select(0)
        self.clear(9)
        self.assertIsNone(self.model.pending_exchange(1))
        self.assertIsNone(self.model.selected)

    # -- 虚拟背包空间（背包内交换/合并） --------------------------------

    def test_the_virtual_space_starts_as_the_engine_truth(self):
        self.put(9, "minecraft:stone", 5)
        self.read()
        self.assertEqual(self.model.entry(0).key.name, "minecraft:stone")
        self.assertEqual(self.model.truth(0).key.name, "minecraft:stone")
        self.assertFalse(self.model.has_pending())
        self.assertEqual(self.model.pending_slots(), [])

    def test_a_local_swap_shows_up_before_the_server_says_anything(self):
        """**这就是虚拟空间存在的理由。** 客户端写不了自己的背包（背包写接口
        全在服务端），所以「点完立刻看出结果」只能是本地那份画出来的。"""
        self.put(9, "minecraft:stone", 5)
        self.put(10, "minecraft:dirt", 3)
        self.read()
        self.assertTrue(
            self.model.apply_exchange("r1", backpack_model.ACTION_SWAP, 0, 1, None)
        )
        self.assertEqual(self.model.entry(0).key.name, "minecraft:dirt")
        self.assertEqual(self.model.entry(0).count(), 3)
        self.assertEqual(self.model.entry(1).key.name, "minecraft:stone")
        # **真值一点没动**——这正是「虚拟」的意思，而它也是能安全丢掉的原因。
        self.assertEqual(self.model.truth(0).key.name, "minecraft:stone")
        self.assertEqual(self.model.pending_slots(), [0, 1])

    def test_a_local_merge_splits_and_joins(self):
        self.put(9, "minecraft:stone", 5)
        self.put(10, "minecraft:stone", 3)
        self.read()
        self.model.apply_exchange("r1", backpack_model.ACTION_MERGE, 0, 1, 2)
        self.assertEqual(self.model.entry(0).count(), 3)
        self.assertEqual(self.model.entry(1).count(), 5)

    def test_a_merge_that_empties_the_source_empties_it_on_screen(self):
        self.put(9, "minecraft:stone", 3)
        self.put(10, "minecraft:stone", 3)
        self.read()
        self.model.apply_exchange("r1", backpack_model.ACTION_MERGE, 0, 1, 3)
        self.assertIsNone(self.model.entry(0))
        self.assertEqual(self.model.entry(1).count(), 6)

    def test_multiple_rounds_stack_in_order(self):
        """**多轮是这一套东西存在的理由。** 玩家在等服务端回执的那段时间里
        可以接着点，每一轮都作用在前一轮的结果上。"""
        self.put(9, "minecraft:stone", 5)
        self.put(10, "minecraft:dirt", 3)
        self.put(11, "minecraft:apple", 2)
        self.read()
        self.model.apply_exchange("r1", backpack_model.ACTION_SWAP, 0, 1, None)
        self.model.apply_exchange("r2", backpack_model.ACTION_SWAP, 1, 2, None)
        self.assertEqual(self.model.entry(0).key.name, "minecraft:dirt")
        self.assertEqual(self.model.entry(1).key.name, "minecraft:apple")
        self.assertEqual(self.model.entry(2).key.name, "minecraft:stone")
        self.assertEqual(self.model.pending_slots(), [0, 1, 2])

    def test_the_screen_does_not_flicker_when_the_receipt_comes_first(self):
        """**回执往往比引擎读数先到。** 那时真值还没变，画面必须继续显示操作
        后的结果——切回真值会先跳回操作前的样子、再跳过来。"""
        self.put(9, "minecraft:stone", 5)
        self.put(10, "minecraft:dirt", 3)
        self.read()
        self.model.apply_exchange("r1", backpack_model.ACTION_SWAP, 0, 1, None)
        self.read()                       # 回执到达时的那次重读
        self.assertEqual(self.model.entry(0).key.name, "minecraft:dirt")
        self.assertTrue(self.model.has_pending())

    def test_operations_retire_from_the_front_as_the_truth_catches_up(self):
        """**退场看的是真值，不是回执。** 真值沿队列从头兑现，没兑现的一直
        重放着——因此第一轮兑现不会把第二轮一起抹掉。"""
        self.put(9, "minecraft:stone", 5)
        self.put(10, "minecraft:dirt", 3)
        self.put(11, "minecraft:apple", 2)
        self.read()
        self.model.apply_exchange("r1", backpack_model.ACTION_SWAP, 0, 1, None)
        self.model.apply_exchange("r2", backpack_model.ACTION_SWAP, 1, 2, None)
        # 服务端做完了第一轮：真值追到第一轮的结果
        self.put(9, "minecraft:dirt", 3)
        self.put(10, "minecraft:stone", 5)
        self.read()
        self.assertEqual(self.model.pending_slots(), [1, 2], "第一轮退场了")
        self.assertEqual(self.model.entry(1).key.name, "minecraft:apple")
        self.assertEqual(self.model.entry(2).key.name, "minecraft:stone")
        # 第二轮兑现之后，画面与真值完全一致
        self.put(10, "minecraft:apple", 2)
        self.put(11, "minecraft:stone", 5)
        self.read()
        self.assertFalse(self.model.has_pending())
        self.assertEqual(self.model.entry(1).key.name, "minecraft:apple")
        self.assertEqual(self.model.entry(2).key.name, "minecraft:stone")

    def test_a_contradicted_operation_is_dropped_not_forced(self):
        """真值走到别处去了 → **丢弃**，而不是硬撑着盖掉别人的改动。

        这一个取舍比「闪一下」重要得多：硬撑着预测会把玩家（或其他途径）
        真正做出来的改动从画面上抹掉，而玩家会据此继续操作。
        """
        self.put(9, "minecraft:stone", 5)
        self.put(10, "minecraft:dirt", 3)
        self.read()
        self.model.apply_exchange("r1", backpack_model.ACTION_SWAP, 0, 1, None)
        self.put(9, "minecraft:apple", 7)     # 别处动过这一格
        self.read()
        self.assertFalse(self.model.has_pending(), "整份丢弃")
        self.assertEqual(self.model.entry(0).key.name, "minecraft:apple")
        self.assertEqual(self.model.entry(1).key.name, "minecraft:dirt")

    def test_dropping_a_rejected_operation_reverts_the_screen(self):
        """回执说失败 → **只撤那一条**，画面上那两格回到它依据的样子。"""
        self.put(9, "minecraft:stone", 5)
        self.put(10, "minecraft:dirt", 3)
        self.read()
        self.model.apply_exchange("r1", backpack_model.ACTION_SWAP, 0, 1, None)
        self.model.apply_exchange("r2", backpack_model.ACTION_SWAP, 1, 2, None)
        self.assertTrue(self.model.drop_pending("r1"))
        self.assertEqual(self.model.pending_slots(), [1, 2], "只撤被拒的那条")
        self.assertEqual(self.model.entry(0).key.name, "minecraft:stone")

    def test_dropping_an_unknown_request_changes_nothing(self):
        """别人的 `request_id` 撤不动我们的东西。"""
        self.put(9, "minecraft:stone", 5)
        self.put(10, "minecraft:dirt", 3)
        self.read()
        self.model.apply_exchange("r1", backpack_model.ACTION_SWAP, 0, 1, None)
        self.assertFalse(self.model.drop_pending("nobody-sent-this"))
        self.assertEqual(self.model.pending_slots(), [0, 1])

    def test_clear_pending_reverts_everything(self):
        """会话丢掉时用：本地操作是「服务端答应过但还没兑现」的，会话都没了，
        那份答应已经作废。"""
        self.put(9, "minecraft:stone", 5)
        self.put(10, "minecraft:dirt", 3)
        self.read()
        self.model.apply_exchange("r1", backpack_model.ACTION_SWAP, 0, 1, None)
        self.model.clear_pending()
        self.assertFalse(self.model.has_pending())
        self.assertEqual(self.model.entry(0).key.name, "minecraft:stone")
        self.assertEqual(self.model.entry(1).key.name, "minecraft:dirt")

    def test_a_take_is_not_applied_to_the_virtual_space(self):
        """取放（网络 ↔ 背包）本地看不到结果：**认不出来就不装**，而不是猜。"""
        self.put(9, "minecraft:stone", 5)
        self.read()
        self.assertFalse(self.model.apply_exchange("r1", "inv_take", 0, 1, None))
        self.assertFalse(self.model.has_pending())

    def test_a_snapshot_reports_what_the_screen_shows(self):
        """`GetBackpackItems()` 回答的是「界面上现在显示什么」，因此含本地
        操作；真值另走 `truth()`。"""
        self.put(9, "minecraft:stone", 5)
        self.put(10, "minecraft:dirt", 3)
        self.read()
        self.model.apply_exchange("r1", backpack_model.ACTION_SWAP, 0, 1, None)
        self.assertEqual(self.model.snapshot()[0], ("minecraft:dirt", 3))
        self.assertEqual(self.model.truth(0).key.name, "minecraft:stone")
        self.assertEqual(self.model.diagnostics()["pending"], [0, 1])

    # -- 这一点击要存什么 ----------------------------------------------

    def test_pending_take_names_the_engine_slot_and_the_fresh_item(self):
        """`pending_take` 给的是 `(引擎槽号, 身份, 数量)`，而且**重读引擎**。

        选中记的只是槽号，不是那一刻的物品与数量。冻一份快照下来等于用旧
        数据代替真身：这一格在选中之后少了几件（别的途径拿走的），发出去的
        就不是现在这一格的真实状况，服务端会判 `SLOT_CHANGED`，而玩家看到
        的只是「点了没反应」。
        """
        self.assertIsNone(self.model.pending_take())
        self.put(9, "minecraft:stone", 3)
        self.read()
        self.model.select(0)
        slot, key, count = self.model.pending_take()
        self.assertEqual(slot, 9)
        self.assertEqual(key.name, "minecraft:stone")
        self.assertEqual(count, 3)
        self.put(9, "minecraft:stone", 2)
        self.assertEqual(self.model.pending_take()[2], 2)

    def test_the_deposit_is_capped_at_one_stack(self):
        """存的是一**组**，与上半屏的「取一组」互为镜像。

        上限只有小于槽内数量时才咬得住，所以这里登记的是一条小堆叠——不
        登记的话默认 64 比槽里的数量都大，上限那一支永远走不到。登记必须
        在**第一次问上限之前**：`stack_size` 是按资源名缓存的。
        """
        ItemStub.stack_sizes["minecraft:stone"] = 16
        self.put(9, "minecraft:stone", 40)
        self.read()
        self.model.select(0)
        self.assertEqual(self.model.pending_take()[2], 16)
        self.put(9, "minecraft:stone", 7)
        self.assertEqual(self.model.pending_take()[2], 7)    # 不满一组就全存

    def test_a_limit_larger_than_the_stack_does_not_truncate_the_deposit(self):
        """上限比槽里多时**不该**削。

        削了的表现是「点一下只进去 16 个，槽里还剩 24」——玩家会以为漏了。
        """
        self.put(9, "minecraft:stone", 40)
        self.read()
        self.model.select(0)
        self.assertEqual(self.model.pending_take()[2], 40)

    def test_a_slot_emptied_after_selection_yields_nothing(self):
        """选中之后那一格空了：本地就该发现，不发命令、也不留一个死选中。

        留着选中，玩家会在同一条死路上反复点：每一次都发一条注定被拒的命令
        出去换一张回执。
        """
        self.put(9, "minecraft:stone", 3)
        self.read()
        self.model.select(0)
        self.clear(9)
        self.assertIsNone(self.model.pending_take())
        self.assertIsNone(self.model.selected)

    def test_diagnostics_counts_failures_without_naming_items(self):
        """诊断只报计数：它是给日志看的，不该把玩家的背包内容抄一份出去。

        `predicted` 是**槽号**不是内容，因此不违反上面这条——而它必须
        报出来：它非空就表示画面上有几格不是引擎说的那一份。
        """
        self.put(9, "minecraft:stone", 3)
        self.read()
        self.broken = True
        self.read()
        self.assertEqual(
            self.model.diagnostics(),
            {"selected": None, "filled": 1, "errors": 1, "pending": []},
        )


if __name__ == "__main__":
    unittest.main()
