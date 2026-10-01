# coding=utf-8
"""合成终端的三个命令：导入、合成、清空（11-04）。

**全部离线。** 走的是真实的 `StorageService.handle` 与真实的网络库存
（`testsupport.StorageStand`），只有背包与配方来源是替身——它们一个在引擎里、
一个是引擎查询，两条本来就有各自的适配器边界。

三条判据：

1. **不丢物。** 合成是「先把产物放进背包、再扣原料」；导入是「借来了放不
   进去就原路还回去」；清空是「按回执扣格」。任何一条路径都不许在没有
   回执的情况下让物品消失；
2. **不静默。** 「九宫格满了」「配方查不到」「背包放不下」各自有自己的
   原因码，不能都表现为「点了没反应」；
3. **不替玩家定未定项。** 多配方不自动挑一条；来源优先级写进策略而不是
   散在代码里。
"""

import unittest

import testsupport

from endernet.common.crafting_recipe import RecipeIngredient, PresetRecipe
from endernet.common.protocol import (
    BlockReason,
    CommandKind,
    CommandSource,
    DeviceCommand,
    Reason,
    TransferReceipt,
    TransferStatus,
)
from endernet.common.resource_key import ResourceKey
from endernet.server.crafting.workbench import (
    IMPORT_KEEP,
    CraftingPolicy,
    WorkbenchStore,
)

PLANKS = ResourceKey.item("minecraft:oak_planks")
BIRCH = ResourceKey.item("minecraft:birch_planks")
STICK = ResourceKey.item("minecraft:stick")
LOG = ResourceKey.item("minecraft:oak_log")

CRAFTING_TERMINAL = u"endernet:crafting_terminal_panel"


# -- 替身 ------------------------------------------------------------------

class FakeRecipes(object):
    """一个按产物返回固定配方的来源。**不查引擎**。"""

    def __init__(self, by_output):
        self.by_output = by_output
        self.calls = []

    def recipes_for(self, output_key):
        self.calls.append(output_key.name)
        return list(self.by_output.get(output_key.name, ()))


class FakeInventory(object):
    """一个够用的假背包：只实现命令路径真正碰的那几个方法。

    `commit_insert` 满时**只放一部分**并如实报 `moved`——这正是真背包
    在满了之后的样子，而用例要钉的就是那条路径。
    """

    SLOTS = 36

    def __init__(self, slots=None, capacity=None):
        #: `槽号 -> (ResourceKey, 数量)`
        self.slots = dict(slots or {})
        self.capacity = self.SLOTS if capacity is None else capacity
        self.calls = []

    def can_read(self):
        return True

    def inventory_view(self):
        rows = []
        for slot in range(self.SLOTS):
            entry = self.slots.get(slot)
            rows.append({
                "slot": slot,
                "key": None if entry is None else entry[0].to_dict(),
                "canonical": "",
                "count": 0 if entry is None else entry[1],
                "version": 1,
            })
        return {"revision": 1, "slots": rows}

    def count_of(self, key):
        return sum(count for entry_key, count in self.slots.values()
                   if entry_key == key)

    def commit_insert(self, op_id, key, count):
        self.calls.append(("insert", key.name, count))
        room = sum(1 for slot in range(self.capacity) if slot not in self.slots)
        placed = min(count, room)
        if placed > 0:
            for slot in range(self.capacity):
                if slot not in self.slots:
                    self.slots[slot] = (key, placed)
                    break
        return self._receipt(op_id, key, count, placed)

    def commit_extract(self, op_id, key, count):
        self.calls.append(("extract", key.name, count))
        remaining = count
        for slot, entry in sorted(self.slots.items()):
            if remaining <= 0:
                break
            if entry[0] != key:
                continue
            moved = min(entry[1], remaining)
            left = entry[1] - moved
            if left <= 0:
                del self.slots[slot]
            else:
                self.slots[slot] = (key, left)
            remaining -= moved
        return self._receipt(op_id, key, count, count - remaining)

    def anchored_extract(self, op_id, anchor, count):
        return self.commit_extract(op_id, anchor.expected_key, count)

    def anchored_insert(self, op_id, anchor, count):
        return self.commit_insert(op_id, anchor.expected_key, count)

    def _receipt(self, op_id, key, requested, moved):
        if moved >= requested:
            status, reason = TransferStatus.COMPLETE, ""
        elif moved > 0:
            status, reason = TransferStatus.PARTIAL, Reason.CONTAINER_PARTIAL
        else:
            status, reason = TransferStatus.REFUSED, Reason.CONTAINER_REFUSED
        return TransferReceipt(op_id, requested, moved, status, reason, key)


class FakeProviders(object):
    def __init__(self, inventory):
        self.inventory = inventory

    def endpoint_of(self, player_id):
        return self.inventory


# -- 夹具 ------------------------------------------------------------------

def recipe(rows, symbols, outputs, ordered=True, ingredients=()):
    return PresetRecipe(
        ordered, tuple(rows), dict(symbols), tuple(ingredients),
        tuple(outputs),
    )


def stack(name, count=1):
    return RecipeIngredient(name, 0, count)


#: 竖着的两根木板 → 4 根木棍。
STICK_RECIPE = recipe(["A", "A"], {"A": (stack("minecraft:oak_planks"),)},
                      (RecipeIngredient("minecraft:stick", 0, 4),))
#: 同一个产物的第二条配方（换成桦木），用来测「多配方不自动挑」。
STICK_BIRCH = recipe(["A", "A"], {"A": (stack("minecraft:birch_planks"),)},
                     (RecipeIngredient("minecraft:stick", 0, 4),))


class Fixture(unittest.TestCase):

    def setUp(self):
        from endernet.server.crafting.workbench import WorkbenchStore

        self.stand = testsupport.StorageStand()
        self.stand.place(0)
        # **先把盘装上再接入网络**：没有盘就没有容量，`seed` 会被
        # 容量拒掉；而 `attach()` 是服务认识这台节点的那一刻，
        # 盘必须在它之前就位。
        self.stand.new_medium()
        self.stand.attach()
        self.stand.seed(PLANKS, 16)
        self.stand.seed(BIRCH, 16)
        # **推进额度时基**：`seed` 是按显式帧号存的，而服务自己那个
        # 时钟还停在 0。不推的话后面每一次取出都会被判成「帧倒流」，
        # 而症状是「导入什么都没摆上、也不报错」——正是最难查的那种。
        self.stand.service.advance_clock(self.stand.next_tick + 10)

        self.store = WorkbenchStore()
        self.grid = self.store.of(self.stand.record.ref)
        self.inventory = FakeInventory()
        self.recipes = FakeRecipes({"minecraft:stick": [STICK_RECIPE]})
        self.service = self.stand.service
        self.service.workbench = self.store
        self.service.recipes = self.recipes
        self.service.inventories = FakeProviders(self.inventory)
        self.service.crafting_policy = CraftingPolicy()

    def command(self, payload, request_id="r1"):
        ref = self.stand.record.ref
        return DeviceCommand(
            request_id,
            CommandKind.STORAGE,
            CommandSource("p1", ref, 0),
            payload=payload,
            device_ref=ref,
        )

    def send(self, payload, request_id="r1"):
        return self.service.handle(None, self.command(payload, request_id))

    def import_stick(self, key=None, digest=None, request_id="r1"):
        payload = {"action": "craft_import", "key": (key or STICK).to_dict()}
        if digest:
            payload["recipe"] = digest
        return self.send(payload, request_id)

    def reasons(self, result):
        return [reason.code for reason in result.reasons]

    def import_and_check(self):
        """导入一次并确认两格各摆上了一件。后续用例的公共前缀。"""
        result = self.import_stick()
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual(1, self.grid.count_at(0))
        self.assertEqual(1, self.grid.count_at(3))


class TestImport(Fixture):

    def test_one_item_per_required_slot(self):
        result = self.import_stick()
        self.assertTrue(result.is_applied(), result.to_dict())
        # 两根木板各占一格，且**每格一个**——不是一组。
        self.assertEqual(1, self.grid.count_at(0))
        self.assertEqual(1, self.grid.count_at(3))
        self.assertEqual([0, 3], list(self.grid.occupied_slots()))

    def test_the_recipe_is_retained_for_the_next_open(self):
        self.import_stick()
        self.assertEqual(STICK_RECIPE.digest(), self.grid.recipe)
        self.assertEqual({"name": u"minecraft:stick", "aux": 0},
                         self.grid.recipe_output)

    def test_it_takes_from_the_network_when_the_backpack_is_empty(self):
        before = self.service.query(PLANKS)["stored"]
        self.import_stick()
        self.assertEqual(before - 2, self.service.query(PLANKS)["stored"])

    def test_the_backpack_is_used_first(self):
        # 来源优先级是**未定项**，本轮取「先背包、后网络」并写在策略里。
        self.inventory.slots[0] = (PLANKS, 5)
        before = self.service.query(PLANKS)["stored"]
        self.import_stick()
        self.assertEqual(before, self.service.query(PLANKS)["stored"])
        # 配方要两格，每格一件 ⇒ 从那一叠里拿两件。
        self.assertEqual(3, self.inventory.count_of(PLANKS))

    def test_a_missing_material_leaves_its_slot_empty(self):
        # 网络里只有一块木板、背包也空：一格摆上、一格留空，
        # **不因为缺料取消整次导入**。
        from endernet.server.crafting.workbench import WorkbenchStore as _S

        stand = testsupport.StorageStand()
        stand.place(90)
        stand.new_medium()
        stand.attach()
        stand.seed(PLANKS, 1)
        stand.service.advance_clock(stand.next_tick + 10)
        grid = _S().of(stand.record.ref)
        service = stand.service
        service.workbench = _S()
        service.workbench._grids[service.workbench._key_of(stand.record.ref)] = grid
        service.recipes = self.recipes
        service.inventories = FakeProviders(FakeInventory())
        service.crafting_policy = CraftingPolicy()

        ref = stand.record.ref
        result = service.handle(None, DeviceCommand(
            "r9", CommandKind.STORAGE, CommandSource("p1", ref, 0),
            payload={"action": "craft_import", "key": STICK.to_dict()},
            device_ref=ref,
        ))
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual(1, result.payload["imported"])
        self.assertEqual([3], result.payload["missing"])

    def test_multiple_recipes_are_handed_back_not_chosen_for_the_player(self):
        self.recipes.by_output["minecraft:stick"] = [STICK_RECIPE, STICK_BIRCH]
        result = self.import_stick()
        self.assertTrue(result.is_applied())
        self.assertTrue(result.payload["needs_choice"])
        self.assertEqual(2, len(result.payload["candidates"]))
        # **一格都没动**：挑哪条是玩家的决定。
        self.assertTrue(self.grid.is_empty())

    def test_the_chosen_digest_is_the_one_imported(self):
        self.recipes.by_output["minecraft:stick"] = [STICK_RECIPE, STICK_BIRCH]
        result = self.import_stick(digest=STICK_BIRCH.digest())
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual(STICK_BIRCH.digest(), self.grid.recipe)
        # 桦木那条要的是桦木。
        self.assertEqual(BIRCH, self.grid.key_at(0))

    def test_an_unknown_digest_is_refused(self):
        result = self.import_stick(digest="deadbeef")
        self.assertIn(Reason.CRAFT_NO_RECIPE, self.reasons(result))
        self.assertTrue(self.grid.is_empty())

    def test_an_occupied_bench_is_refused_not_overwritten(self):
        self.grid.set_slot(4, LOG, 3)
        result = self.import_stick()
        self.assertIn(Reason.CRAFT_GRID_OCCUPIED, self.reasons(result))
        # 原有的东西一个没动。
        self.assertEqual(3, self.grid.count_at(4))
        self.assertEqual((4,), self.grid.occupied_slots())

    def test_the_occupied_refusal_can_be_relaxed_by_policy(self):
        self.service.crafting_policy = CraftingPolicy()
        self.service.crafting_policy.import_when_occupied = IMPORT_KEEP
        self.grid.set_slot(4, LOG, 3)
        result = self.import_stick()
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual(3, self.grid.count_at(4))

    def test_an_output_with_no_recipe_is_refused(self):
        result = self.import_stick(key=LOG)
        self.assertIn(Reason.CRAFT_NO_RECIPE, self.reasons(result))

    def test_a_non_crafting_terminal_has_no_workbench(self):
        # 没有 `accepts` 时 store 对谁都开口，因此这里显式装一个只认
        # 合成终端的门——这条钉的是「别的方块不该有九宫格」。
        self.service.workbench = WorkbenchStore(
            accepts=lambda ref: False)
        result = self.import_stick()
        self.assertIn(Reason.UNSUPPORTED, self.reasons(result))

    def test_a_broken_recipe_source_is_not_reported_as_a_bad_parameter(self):
        class Broken(object):
            def recipes_for(self, key):
                raise TypeError("引擎返回了 bool")

        self.service.recipes = Broken()
        result = self.import_stick()
        self.assertFalse(result.is_applied())
        self.assertIn(Reason.NO_ADAPTER, self.reasons(result))


class TestCraft(Fixture):

    def test_crafting_consumes_the_inputs_and_delivers_to_the_backpack(self):
        self.import_and_check()
        result = self.send({"action": "craft_take"})
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertTrue(self.grid.is_empty())
        self.assertEqual(4, self.inventory.count_of(STICK))
        self.assertEqual(4, result.payload["outputs"][0]["count"])

    def test_crafting_needs_a_retained_recipe(self):
        result = self.send({"action": "craft_take"})
        self.assertIn(Reason.CRAFT_NO_RECIPE, self.reasons(result))

    def test_a_grid_that_does_not_match_the_recipe_is_refused(self):
        self.import_and_check()
        # 把其中一格换成别的东西：判定看的是格子实际有什么。
        self.grid.set_slot(0, LOG, 1)
        result = self.send({"action": "craft_take"})
        self.assertIn(Reason.INSUFFICIENT_STOCK, self.reasons(result))
        self.assertEqual(1, self.grid.count_at(3))

    def test_a_full_backpack_refuses_and_keeps_every_input(self):
        self.import_and_check()
        self.inventory.capacity = 0
        result = self.send({"action": "craft_take"})
        self.assertIn(Reason.NO_CAPACITY, self.reasons(result))
        # **一格原料都没动**：顺序是「先放产物、再扣原料」。
        self.assertEqual(1, self.grid.count_at(0))
        self.assertEqual(1, self.grid.count_at(3))

    def test_only_one_execution_per_click(self):
        # 一次 = 一次配方执行，不是自动连做。
        self.import_and_check()
        self.send({"action": "craft_take"})
        self.assertTrue(self.grid.is_empty())
        self.assertEqual(4, self.inventory.count_of(STICK))


class TestClear(Fixture):

    def test_clear_returns_everything_to_the_network(self):
        self.import_and_check()
        before = self.service.query(PLANKS)["stored"]
        result = self.send({"action": "craft_clear"})
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertTrue(self.grid.is_empty())
        self.assertEqual(before + 2, self.service.query(PLANKS)["stored"])

    def test_clear_also_drops_the_retained_recipe(self):
        self.import_and_check()
        self.send({"action": "craft_clear"})
        self.assertIsNone(self.grid.recipe)
        self.assertIsNone(self.grid.recipe_output)

    def test_items_the_network_refuses_stay_in_the_grid(self):
        self.import_and_check()
        # 网络存不下时**一件都不许丢**：它们留在原格。
        original = self.service.commit_insert
        self.service.commit_insert = lambda op_id, key, count, **kw: TransferReceipt(
            op_id, count, 0, TransferStatus.REFUSED, Reason.NO_CAPACITY, key)
        try:
            result = self.send({"action": "craft_clear"})
        finally:
            self.service.commit_insert = original
        self.assertTrue(result.is_applied())
        self.assertEqual(0, result.payload["returned"])
        self.assertEqual([0, 3], result.payload["kept"])
        self.assertEqual(1, self.grid.count_at(0))
        self.assertEqual(1, self.grid.count_at(3))
        # 清不干净时保留配方，免得界面显示一条与格子对不上的配方。
        self.assertIsNotNone(self.grid.recipe)

    def test_clearing_an_empty_bench_is_not_an_error(self):
        result = self.send({"action": "craft_clear"})
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual(0, result.payload["returned"])


class TestWiring(unittest.TestCase):

    def test_the_device_id_constant_matches_the_definition_table(self):
        from endernet.common.definitions import DefinitionSet
        from endernet.server.bootstrap import CRAFTING_TERMINAL_DEVICE_ID

        definitions = DefinitionSet()
        self.assertIn(
            CRAFTING_TERMINAL_DEVICE_ID, definitions.devices,
            "启动层那份方块型号常量与定义表对不上",
        )
        # 它必须是终端类，否则「合成终端走自定义槽位」那条分类就错了。
        self.assertIn(
            CRAFTING_TERMINAL_DEVICE_ID,
            definitions.devices_in_category("terminal"),
        )


if __name__ == "__main__":
    unittest.main()
