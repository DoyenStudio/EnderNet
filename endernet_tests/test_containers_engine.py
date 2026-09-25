# coding=utf-8
"""真实容器的引擎适配：逐槽写入、逐槽读回、按 maxStackSize 拆分。

对应任务 06 的 13/06 接缝。**全部离线**：引擎原语是注入的，
真的去读箱子、读背包的那一层（`mod.server.extraServerApi`）不在这一套里，
只能在游戏内验证——那是交付说明的「未验证」项。

这一套用例守的是三件真机上才会咬人的事：

1. **不看 `PutItemIntoContainer` 的返回值。** 架构规划 §6.3 点名的缺陷形态
   在这里被逐槽读回挡住：原语说「放进去了」不算数，槽里真的变了才算。
2. **绝不生成超堆叠物品。** 按 `maxStackSize` 拆分，放不下就报部分成功。
3. **引擎导入必须在方法体内。** 顶层 import 会让 `bootstrap.py` 无法离线
   导入本模块，整条测试链路会静默失效。
"""

import ast
import io
import os
import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.protocol import TransferStatus
from endernet.common.resource_key import ResourceKey
from endernet.server.adapters.containers_engine import (
    EMPTY_ITEM,
    PLAYER_SLOT_COUNT,
    EngineContainerProvider,
    EngineInventoryProvider,
    EngineItemContainer,
    EngineItemPrimitives,
    EnginePlayerInventory,
    SlotBackedContainer,
    build_stack_limit,
)
from endernet.server.storage.transfer import SlotAnchor

STONE = ResourceKey.item("minecraft:stone")
DIRT = ResourceKey.item("minecraft:dirt")
NETHERITE = ResourceKey.item("minecraft:netherite_ingot")


def item_dict(key, count):
    """一件物品的原始字典。**不经 `tooldelta` 的 `Item` 包装**——
    那一层会把 `durability` 的 `None` 归一成 `0`，而 `item_codec` 按
    「空值与缺省是不同身份」处理。"""
    return {
        "newItemName": key.name,
        "newAuxValue": key.aux,
        "count": count,
        "showInHand": True,
    }


class FakeContainerPrimitives(object):
    """按 `tooldelta/api/server/container.py` 的**调用形状**实现的假引擎。

    与 `test_transfer.py` 里那份分开写：两个文件各自成立，不互相 import。
    """

    def __init__(self, sizes=None, default_size=3, stack_limit=64):
        self.boxes = {}
        self.sizes = dict(sizes or {})
        self.default_size = default_size
        self.stack_limit_value = stack_limit
        self.write_calls = []
        self.fail_writes = False
        self.lying_writes = False
        self.missing_positions = set()

    def _box(self, dimension, position):
        return self.boxes.setdefault((dimension, tuple(position)),
                                     [None] * self.size_of(dimension, position))

    def size_of(self, dimension, position):
        return self.sizes.get(tuple(position), self.default_size)

    def container_size(self, dimension, position):
        if tuple(position) in self.missing_positions:
            raise ValueError("该位置没有方块实体")
        return self.size_of(dimension, position)

    def container_item(self, dimension, position, slot):
        return self._box(dimension, position)[slot]

    def set_container_item(self, dimension, position, slot, data):
        self.write_calls.append((tuple(position), slot, data.get("count", 0)))
        if self.fail_writes:
            return False
        box = self._box(dimension, position)
        if self.lying_writes:
            # 声称写成功，实际什么都不做——`PutItemIntoContainer` 的形态。
            return True
        if data.get("count", 0) <= 0 or data.get("newItemName") == "minecraft:air":
            box[slot] = None
        else:
            box[slot] = dict(data)
        return True

    def stack_limit(self, name, aux):
        return self.stack_limit_value


class FakePlayerPrimitives(object):
    """按 `tooldelta/api/server/player.py` 的调用形状实现的假引擎。

    玩家那几个方法用的是 `CreateItem(player_id)` 组合，与容器的
    `CreateItem(GetLevelId())` **不是同一个组合**——这里保留同样的形状，
    免得测试通过而真机上「没反应」。
    """

    def __init__(self, stack_limit=64):
        self.slots = [None] * PLAYER_SLOT_COUNT
        self.stack_limit_value = stack_limit
        self.player_ids = []

    def player_slot_count(self, player_id):
        self.player_ids.append(player_id)
        return PLAYER_SLOT_COUNT

    def player_item(self, player_id, slot):
        return self.slots[slot]

    def set_player_item(self, player_id, slot, data):
        self.player_ids.append(player_id)
        if data.get("count", 0) <= 0 or data.get("newItemName") == "minecraft:air":
            self.slots[slot] = None
        else:
            self.slots[slot] = dict(data)
        return True

    def stack_limit(self, name, aux):
        return self.stack_limit_value


class ContainerFixture(unittest.TestCase):
    def setUp(self):
        self.primitives = FakeContainerPrimitives(default_size=2, stack_limit=10)
        self.limit = build_stack_limit(self.primitives)
        self.container = EngineItemContainer(
            self.primitives, 0, (1, 2, 3), self.limit
        )


class TestSlotBackedSemantics(ContainerFixture):
    def test_capabilities_are_declared_honestly(self):
        caps = self.container.capabilities()
        self.assertTrue(caps.can_read)
        self.assertTrue(caps.can_insert)
        self.assertTrue(caps.can_simulate)
        self.assertTrue(caps.supports_partial)
        # 引擎没有事务：一槽一槽写的，中途可以停。
        self.assertFalse(caps.is_atomic)
        # 容器没有变化通知（13-05）：调用方退化到退避轮询。
        self.assertIsNone(caps.notify_token)
        self.assertEqual(caps.max_slots, 2)

    def test_insert_respects_the_stack_limit(self):
        """**绝不生成超堆叠物品。** 两槽 × 10 = 20，多的部分放不下。"""
        receipt = self.container.insert("op1", STONE, 25)
        self.assertEqual(receipt.status, TransferStatus.PARTIAL)
        self.assertEqual(receipt.moved, 20)
        self.assertEqual(receipt.remainder, 5)
        for _, slot, count in self.primitives.write_calls:
            self.assertLessEqual(count, 10)

    def test_insert_fills_partial_stacks_first(self):
        self.container.insert("seed", STONE, 4)
        self.primitives.write_calls = []
        self.container.insert("op1", STONE, 3)
        # 第一槽补到 7（4+3），没有新开槽。
        self.assertEqual(self.primitives.write_calls, [((1, 2, 3), 0, 7)])

    def test_extract_reports_the_verified_amount(self):
        self.container.insert("seed", STONE, 12)
        receipt = self.container.extract("op1", STONE, 5)
        self.assertEqual(receipt.status, TransferStatus.COMPLETE)
        self.assertEqual(receipt.moved, 5)
        self.assertEqual(self.container.count_of(STONE), 7)

    def test_extracting_more_than_there_is_reports_partial(self):
        self.container.insert("seed", STONE, 4)
        receipt = self.container.extract("op1", STONE, 9)
        self.assertEqual(receipt.status, TransferStatus.PARTIAL)
        self.assertEqual(receipt.moved, 4)

    def test_an_empty_container_refuses_extraction(self):
        receipt = self.container.extract("op1", STONE, 1)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.moved, 0)

    def test_simulation_matches_what_a_commit_actually_moves(self):
        """模拟不能只是「看起来合理」：它得和真的提交给出同一个数。"""
        predicted = self.container.can_accept(STONE, 25)
        self.assertEqual(predicted, 20)
        self.assertEqual(self.container.insert("op1", STONE, 25).moved, predicted)

    def test_identity_is_not_merged_across_metadata(self):
        """同名的两件东西如果元数据不同，**不能合并成一个堆**。"""
        enchanted = ResourceKey.item(
            "minecraft:stone", enchant_data=((9, 1),)
        )
        self.container.insert("a", STONE, 5)
        self.container.insert("b", enchanted, 5)
        self.assertEqual(len(self.container.entries()), 2)
        self.assertEqual(self.container.count_of(STONE), 5)
        self.assertEqual(self.container.count_of(enchanted), 5)


class TestWriteFailureIsNeverClaimedAsSuccess(ContainerFixture):
    """**「当前 ToolDelta 高层插入方法的失败路径单独验证」**（06 验收末条）。

    不能只凭「没有余量」推断引擎写入成功——`PutItemIntoContainer` 正是
    在空槽路径凭这一点报告「全部放入」的。
    """

    def test_a_refused_write_reports_nothing_moved(self):
        self.primitives.fail_writes = True
        receipt = self.container.insert("op1", STONE, 5)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(self.container.count_of(STONE), 0)
        self.assertGreater(self.container.last_write_failures, 0)

    def test_a_lying_write_is_caught_by_the_read_back(self):
        """原语说写成功了，槽里其实没变——**逐槽读回就是为了这个**。"""
        self.primitives.lying_writes = True
        receipt = self.container.insert("op1", STONE, 5)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.moved, 0)

    def test_a_partial_write_failure_only_counts_what_stuck(self):
        """一半的槽写得进去、一半写不进去：回执只算真的落下的。"""
        primitives = _FlakyWritePrimitives(default_size=3, stack_limit=10)
        container = EngineItemContainer(primitives, 0, (0, 0, 0), self.limit)
        receipt = container.insert("op1", STONE, 25)
        self.assertEqual(receipt.moved, 20, "第 3 槽写不进去，只算前两槽")
        self.assertEqual(primitives.written_slots, [0, 1])

    def test_a_refused_extract_write_leaves_the_stack_alone(self):
        self.container.insert("seed", STONE, 8)
        self.primitives.fail_writes = True
        receipt = self.container.extract("op1", STONE, 8)
        self.assertEqual(receipt.moved, 0)
        self.assertEqual(self.container.count_of(STONE), 8)


class _FlakyWritePrimitives(FakeContainerPrimitives):
    """第 3 个槽（下标 2）永远写不进去。"""

    def __init__(self, **kwargs):
        FakeContainerPrimitives.__init__(self, **kwargs)
        self.written_slots = []

    def set_container_item(self, dimension, position, slot, data):
        if slot == 2:
            return False
        self.written_slots.append(slot)
        return FakeContainerPrimitives.set_container_item(
            self, dimension, position, slot, data
        )


class TestInvalidation(ContainerFixture):
    def test_an_invalidated_container_refuses_to_be_read(self):
        """13-05：邻接容器被拆、维度变化之后必须失效。"""
        self.container.invalidate()
        self.assertFalse(self.container.is_valid())
        with self.assertRaises(IOError):
            self.container.entries()

    def test_writes_to_an_invalidated_container_fail(self):
        self.container.invalidate()
        self.assertFalse(self.container.write_slot(0, item_dict(STONE, 1)))


class TestPlayerInventory(unittest.TestCase):
    """玩家背包：36 槽，按 maxStackSize 拆分。"""

    def setUp(self):
        self.primitives = FakePlayerPrimitives(stack_limit=10)
        self.limit = build_stack_limit(self.primitives)
        self.inventory = EnginePlayerInventory(self.primitives, "steve", self.limit)

    def test_it_has_thirty_six_slots(self):
        self.assertEqual(self.inventory.slot_count(), PLAYER_SLOT_COUNT)
        self.assertEqual(self.inventory.capabilities().max_slots, PLAYER_SLOT_COUNT)

    def test_a_large_insert_is_split_across_slots(self):
        """**大于 maxStackSize 的提取不会生成超堆叠背包物品。**"""
        receipt = self.inventory.insert("op1", STONE, 70)
        self.assertEqual(receipt.status, TransferStatus.COMPLETE)
        self.assertEqual(receipt.moved, 70)
        for slot, entry in self.inventory.entries():
            self.assertLessEqual(entry.count, 10, "第 %d 槽超堆叠了" % (slot,))

    def test_a_full_backpack_only_takes_what_fits(self):
        """**背包容量不足只转移实际数量。**"""
        self.inventory.insert("filler", DIRT, PLAYER_SLOT_COUNT * 10)
        receipt = self.inventory.insert("op1", STONE, 5)
        self.assertEqual(receipt.status, TransferStatus.REFUSED)
        self.assertEqual(receipt.moved, 0)

    def test_a_partially_full_backpack_reports_partial(self):
        """只空出 1 个槽（装得下 10 个）时请求 25 个：**只转移实际数量**。"""
        self.inventory.insert("filler", DIRT, (PLAYER_SLOT_COUNT - 1) * 10)
        receipt = self.inventory.insert("op1", STONE, 25)
        self.assertEqual(receipt.status, TransferStatus.PARTIAL)
        self.assertEqual(receipt.moved, 10)
        self.assertEqual(receipt.remainder, 15)

    def test_a_slot_clearing_write_reaches_the_engine(self):
        self.inventory.insert("op1", STONE, 6)
        self.inventory.extract("op2", STONE, 6)
        self.assertEqual(self.inventory.count_of(STONE), 0)
        self.assertIsNone(self.primitives.slots[0])

    def test_the_engine_is_addressed_by_player_id_every_time(self):
        self.inventory.insert("op1", STONE, 1)
        self.assertTrue(self.primitives.player_ids)
        self.assertEqual(set(self.primitives.player_ids), set(["steve"]))


class TestStackLimitResolution(unittest.TestCase):
    def test_the_limit_comes_from_the_engine(self):
        primitives = FakePlayerPrimitives(stack_limit=16)
        self.assertEqual(build_stack_limit(primitives)(STONE), 16)

    def test_an_unknown_item_falls_back_to_one_not_sixty_four(self):
        """**查不到就按 1 处理**：按 64 处理会造出超堆叠物品，
        而超堆叠在背包里会被引擎拆掉或吞掉——那是真的丢东西。"""

        class Broken(object):
            def stack_limit(self, name, aux):
                raise ValueError("no such item")

        self.assertEqual(build_stack_limit(Broken())(STONE), 1)

    def test_a_non_positive_limit_is_treated_as_one(self):
        primitives = FakeContainerPrimitives(stack_limit=0)
        self.assertEqual(build_stack_limit(primitives)(STONE), 1)


class TestEngineItemNormalization(unittest.TestCase):
    """引擎读回来的物品字典要归一成 `item_codec` 认的形状。

    **这些期望值来自 2026-09-25 的游戏内实测**，不是推断的：对一个
    **原版箱子**调 `GetContainerItem` 返回 16 个字段，身份的六个字段
    用的是 `[]` / `""` / `0` 而不是 `None`。
    不归一的话，同一件物品「从网络写进去」与「从箱子读出来」会得到
    两个不同的 `ResourceKey`（`item_codec` 按「空值与缺省是不同身份」
    处理），守恒核对立刻失效，而症状是「读回来的东西对不上」。

    `_do_insert` 的逐槽读回就在这条路径上：不归一的话，**写入成功之后
    读回会抛 `ValueError`**，整条转移被 `ApplicationService` 兜成
    `rejected internal_error`——东西进去了、回执说失败。
    """

    #: 实测读数（原版箱子，石头），逐字段照抄。
    MEASURED = {
        "count": 12,
        "newItemName": "minecraft:stone",
        "isDiggerItem": False,
        "enchantData": [],
        "durability": 0,
        "itemId": 1,
        "customTips": "",
        "extraId": "",
        "newAuxValue": 0,
        "modEnchantData": [],
        "modId": "",
        "userData": None,
        "modItemId": "",
        "itemName": "minecraft:stone",
        "auxValue": 0,
        "showInHand": True,
    }

    def test_the_measured_shape_is_normalized(self):
        from endernet.server.adapters.containers_engine import normalize_engine_item

        out = normalize_engine_item(dict(self.MEASURED))
        self.assertEqual(sorted(out), [
            "count", "customTips", "durability", "enchantData", "extraId",
            "modEnchantData", "newAuxValue", "newItemName", "showInHand",
            "userData",
        ])
        for field in ("enchantData", "modEnchantData", "customTips", "extraId",
                      "durability"):
            self.assertIsNone(out[field], field)
        self.assertEqual(out["count"], 12)
        self.assertEqual(out["newItemName"], "minecraft:stone")

    def test_the_normalized_shape_is_accepted_by_the_codec(self):
        from endernet.common.item_codec import item_key_from_dict
        from endernet.server.adapters.containers_engine import normalize_engine_item

        key = item_key_from_dict(normalize_engine_item(dict(self.MEASURED)))
        self.assertEqual(key, ResourceKey.item("minecraft:stone"))

    def test_a_round_trip_key_matches_the_written_key(self):
        """**这才是归一化的目的**：写进去的键与读回来的键必须是同一个。"""
        from endernet.common.item_codec import (
            item_dict_from_key,
            item_key_from_dict,
        )
        from endernet.server.adapters.containers_engine import normalize_engine_item

        written = item_dict_from_key(STONE, 12)
        read_back = dict(self.MEASURED)
        read_back.update(newItemName=written["newItemName"],
                         newAuxValue=written["newAuxValue"])
        self.assertEqual(
            item_key_from_dict(normalize_engine_item(read_back)),
            item_key_from_dict(written),
        )

    def test_an_unknown_field_still_raises(self):
        """**只放行已知派生字段。** 再出现别的未知字段仍然要炸——
        「不静默丢弃潜在身份数据」这条守卫不能因为「引擎字段多」就放开。"""
        from endernet.common.item_codec import item_key_from_dict
        from endernet.server.adapters.containers_engine import normalize_engine_item

        weird = dict(self.MEASURED)
        weird["someNewIdentityField"] = "?"
        with self.assertRaises(ValueError):
            item_key_from_dict(normalize_engine_item(weird))

    def test_an_empty_slot_passes_through(self):
        from endernet.server.adapters.containers_engine import normalize_engine_item

        self.assertIsNone(normalize_engine_item(None))
        air = {"newItemName": "minecraft:air", "newAuxValue": 0, "count": 0,
               "showInHand": True}
        self.assertIs(normalize_engine_item(air), air)

    def test_an_enchanted_item_keeps_its_identity(self):
        """归一化只碰「空值」，不碰真正的身份数据。"""
        from endernet.server.adapters.containers_engine import normalize_engine_item

        enchanted = dict(self.MEASURED)
        enchanted["enchantData"] = [(9, 1)]
        out = normalize_engine_item(enchanted)
        self.assertEqual(out["enchantData"], [(9, 1)])


class TestContainerProvider(unittest.TestCase):
    def setUp(self):
        self.primitives = FakeContainerPrimitives(default_size=3)
        self.provider = EngineContainerProvider(self.primitives)

    def test_it_returns_an_adapter_for_a_real_container(self):
        from endernet.common.identity import NodeKey

        adapter = self.provider.container_at(NodeKey(0, 1, 2, 3))
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.slot_count(), 3)

    def test_a_position_that_is_not_a_container_yields_none(self):
        """**不能造一个空容器**：那会被当成「有这个容器但是空的」，
        于是取货口会一直往一个不存在的地方送货。"""
        from endernet.common.identity import NodeKey

        self.primitives.sizes[(9, 9, 9)] = 0
        self.assertIsNone(self.provider.container_at(NodeKey(0, 9, 9, 9)))

    def test_an_engine_error_yields_none_not_an_exception(self):
        from endernet.common.identity import NodeKey

        self.primitives.missing_positions.add((9, 9, 9))
        self.assertIsNone(self.provider.container_at(NodeKey(0, 9, 9, 9)))
        self.assertEqual(self.provider.stats["misses"], 1)

    def test_adapters_are_cached_per_position(self):
        from endernet.common.identity import NodeKey

        first = self.provider.container_at(NodeKey(0, 1, 2, 3))
        second = self.provider.container_at(NodeKey(0, 1, 2, 3))
        self.assertIs(first, second)
        self.assertEqual(self.provider.diagnostics()["cached"], 1)

    def test_invalidate_drops_and_disables_the_cached_adapter(self):
        """作废之后**旧适配器不能再用**；下一次探测会重新建一个。

        旧句柄留在别处（例如一次还没跑完的搬运）时，它必须自己失效，
        而不是继续对着一个已经被拆掉的位置读写。
        """
        from endernet.common.identity import NodeKey

        node_key = NodeKey(0, 1, 2, 3)
        adapter = self.provider.container_at(node_key)
        self.assertTrue(self.provider.invalidate(node_key))
        self.assertFalse(adapter.is_valid())
        fresh = self.provider.container_at(node_key)
        self.assertIsNotNone(fresh)
        self.assertIsNot(fresh, adapter, "作废后必须重建，不能复用旧句柄")
        self.assertTrue(self.provider.invalidate(node_key))
        self.assertFalse(fresh.is_valid())

    def test_invalidate_all_clears_everything(self):
        from endernet.common.identity import NodeKey

        self.provider.container_at(NodeKey(0, 1, 2, 3))
        self.provider.container_at(NodeKey(0, 4, 5, 6))
        self.assertEqual(self.provider.invalidate_all(), 2)
        self.assertEqual(self.provider.diagnostics()["cached"], 0)


class TestInventoryProvider(unittest.TestCase):
    def setUp(self):
        self.primitives = FakePlayerPrimitives(stack_limit=10)
        self.provider = EngineInventoryProvider(
            self.primitives, build_stack_limit(self.primitives)
        )

    def test_it_builds_endpoints_with_slot_versions(self):
        endpoint = self.provider.endpoint_of("steve")
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.endpoint_id(), "player:steve")
        self.assertEqual(endpoint.slot_view(0)["version"], 1)

    def test_an_empty_player_id_yields_nothing(self):
        self.assertIsNone(self.provider.inventory_of(""))
        self.assertIsNone(self.provider.endpoint_of(""))

    def test_slot_views_carry_the_fingerprint_and_version(self):
        endpoint = self.provider.endpoint_of("steve")
        endpoint.adapter.insert("seed", STONE, 4)
        view = endpoint.slot_view(0)
        self.assertEqual(view["count"], 4)
        self.assertEqual(view["key"], STONE.to_dict())
        self.assertTrue(endpoint.anchor_verdict(SlotAnchor(0, STONE, view["version"]))[0])

    def test_forgetting_a_player_invalidates_their_adapter(self):
        """玩家退出时丢弃缓存。**不写盘**（退出恢复规则属 R12/12）。"""
        adapter = self.provider.inventory_of("steve")
        self.assertTrue(self.provider.forget("steve"))
        self.assertFalse(adapter.is_valid())
        self.assertFalse(self.provider.forget("steve"))
        self.assertEqual(self.provider.diagnostics()["cached_players"], 0)

    def test_the_endpoint_shares_the_cached_adapter(self):
        first = self.provider.endpoint_of("steve").adapter
        second = self.provider.endpoint_of("steve").adapter
        self.assertIs(first, second)


class _MissingEngine(object):
    """一个没有引擎的替身：任何真实调用都炸。"""

    def container_size(self, dimension, position):
        raise AssertionError("离线环境不该真的去读容器")

    def container_item(self, dimension, position, slot):
        raise AssertionError("离线环境不该真的去读容器")

    def set_container_item(self, dimension, position, slot, data):
        raise AssertionError("离线环境不该真的去写容器")


class TestEngineBoundary(unittest.TestCase):
    """`EngineItemPrimitives` 的引擎调用必须留在**方法体内**。

    顶层 import 会让 `bootstrap.py` 无法离线导入本模块，整条测试链路会
    静默失效——而这个错误**在游戏里看不出来**（游戏里当然导得进来）。
    `test_import_boundaries.py` 只对 `events.py` 查了这一条，本文件补上
    新适配器的同一条。
    """

    ADAPTER = os.path.join(
        testsupport.ENDERNET_ROOT, "server", "adapters", "containers_engine.py"
    )

    def test_no_module_level_engine_import(self):
        with io.open(self.ADAPTER, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=self.ADAPTER)
        offenders = []
        for node in tree.body:  # 只看模块顶层
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in (
                "tooldelta", "mod"
            ):
                offenders.append((node.lineno, node.module))
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in ("tooldelta", "mod"):
                        offenders.append((node.lineno, alias.name))
        self.assertEqual(
            offenders, [], "适配器顶层出现了引擎导入，离线链路会失效：%r" % (offenders,)
        )

    def test_the_module_imports_without_an_engine(self):
        """模块本身必须能在一个没有 `mod.server.*` 的环境里导入成功。"""
        import endernet.server.adapters.containers_engine as module

        self.assertTrue(hasattr(module, "EngineItemPrimitives"))

    def test_the_primitives_only_touch_the_engine_when_called(self):
        """构造原语**不碰引擎**；真的去读的时候才碰。"""
        primitives = EngineItemPrimitives()
        self.assertTrue(callable(primitives.container_size))
        # 只验证「构造成功」，不调用——调用会真的去 import 引擎。

    def test_an_engine_free_provider_still_constructs(self):
        """注入一个「一碰就炸」的原语：只要没人调用，整条链路照常构造。"""
        provider = EngineContainerProvider(_MissingEngine())
        self.assertEqual(provider.diagnostics()["cached"], 0)


class TestEmptyItemTranslation(unittest.TestCase):
    """`None` 到空气字典的翻译放在**基类**，子类只实现 `_write_slot`。

    清空槽位在容器上是写一个空气物品、在背包上是 `SetInvItemNum(slot, 0)`
    ——**两条路径的「空」不是同一个表达**。让每个子类自己记得翻译必然会
    有一处漏掉，而漏掉的表现是「取不空」这种看起来像额度问题的症状。
    """

    def test_the_base_class_translates_none(self):
        class Recording(SlotBackedContainer):
            def __init__(self):
                SlotBackedContainer.__init__(self)
                self.written = []

            def slot_count(self):
                return 1

            def read_slot(self, slot):
                return None

            def _write_slot(self, slot, data):
                self.written.append((slot, data))
                return True

        recorder = Recording()
        recorder.write_slot(0, None)
        self.assertEqual(recorder.written[0][1], dict(EMPTY_ITEM))

    def test_the_air_dictionary_is_a_valid_empty_item(self):
        self.assertEqual(EMPTY_ITEM["count"], 0)
        self.assertEqual(EMPTY_ITEM["newItemName"], "minecraft:air")


if __name__ == "__main__":
    unittest.main()
