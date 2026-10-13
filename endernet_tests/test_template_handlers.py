# coding=utf-8
"""样板终端的八条命令（11-05）。

**全部离线。** 走的是真实的 `StorageService.handle`、真实的
`TemplateStore`，只有背包与配方来源是替身——它们一个在引擎里、一个是引擎
查询，两条本来就有各自的适配器边界。

四条判据：

1. **虚配置不产生实物。** 虚格的放入／清空／改数量只改一份配置：不进网络、
   不进背包、不生成物品。真的东西只在两个真实槽里。
2. **不丢物。** 制作是「先把产物放进格子、再扣输入」；放样板是「借来了放不
   进去就原路还回去」；取回是「先放进背包、再扣格子」。
3. **不静默。** 「这一格在当前模式下不能改」「配置还构不成样板」「输入槽
   不是工厂样板」各有各的原因码。
4. **两种模式编出的都是工厂样板**（User 2026-10-02）——模式记在 `userData`
   里，不记在物品类型上。
"""

import unittest

import testsupport

from endernet.common.crafting_recipe import PresetRecipe, RecipeIngredient
from endernet.common.definitions import TEMPLATE_PANEL_ITEM_ID
from endernet.common.protocol import (
    CommandKind,
    CommandSource,
    DeviceCommand,
    Reason,
    TransferReceipt,
    TransferStatus,
)
from endernet.common.resource_key import ResourceKey
from endernet.server.crafting import patterns
from endernet.server.crafting.template_terminal import (
    GROUP_OUTPUTS,
    GROUP_SOURCE,
    INPUT_SLOT,
    PUT_COUNT_ONE,
    RESULT_SLOT,
    TEMPLATE_SCHEMA_VERSION,
    TemplatePolicy,
    TemplateStore,
)

PLANKS = ResourceKey.item("minecraft:oak_planks")
LOG = ResourceKey.item("minecraft:oak_log")
STICK = ResourceKey.item("minecraft:stick")
FACTORY = ResourceKey.item(TEMPLATE_PANEL_ITEM_ID)
CRAFTING = ResourceKey.item("endernet:crafting_template")

STACK_LIMIT = 64


# -- 替身 ------------------------------------------------------------------

class FakeRecipes(object):
    """按产物与按原料两个方向都答得出来的假来源。**不查引擎**。"""

    def __init__(self, by_output=None, by_input=None):
        self.by_output = by_output or {}
        self.by_input = by_input or {}
        self.input_calls = []

    def recipes_for(self, output_key):
        return list(self.by_output.get(output_key.name, ()))

    def recipes_for_input(self, key):
        self.input_calls.append(key.name)
        return list(self.by_input.get(key.name, ()))


class FakeInventory(object):
    """够用的假背包。满时**只放一部分**并如实报 `moved`。"""

    SLOTS = 36

    def __init__(self, slots=None, capacity=None, readable=True):
        #: `槽号 -> (ResourceKey, 数量)`
        self.slots = dict(slots or {})
        self.capacity = self.SLOTS if capacity is None else capacity
        self.readable = readable
        self.calls = []

    def can_read(self):
        return self.readable

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
        return self._receipt(op_id, key, count, placed,
                             empty=Reason.NO_CAPACITY, partial=Reason.NO_CAPACITY)

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
        return self._receipt(op_id, key, count, count - remaining,
                             empty=Reason.INSUFFICIENT_STOCK,
                             partial=Reason.INSUFFICIENT_STOCK)

    def anchored_extract(self, op_id, anchor, count):
        return self.commit_extract(op_id, anchor.expected_key, count)

    def anchored_insert(self, op_id, anchor, count):
        """锚定了槽号就放进那一格。

        **那一格装着别的东西时拒绝**——照真实原语（`transfer.slot_insert`
        先读回、异种即 `slot_occupied`），也照 `anchored_insert` 的
        `anchor_verdict`。原先是「占着就退回去自己找位置」，那比真机宽松，
        会让「目标格被占」这条路径在离线测不出来。
        """
        slot = anchor.slot
        if slot is not None and slot < self.capacity:
            current = self.slots.get(slot)
            if current is None:
                self.calls.append(("anchored_insert", anchor.expected_key.name, count))
                self.slots[slot] = (anchor.expected_key, count)
                return self._receipt(op_id, anchor.expected_key, count, count)
            if current[0] != anchor.expected_key:
                return TransferReceipt(
                    op_id, count, 0, TransferStatus.REFUSED,
                    Reason.SLOT_OCCUPIED, anchor.expected_key,
                )
            taken = min(count, self.capacity - current[1])
            self.slots[slot] = (current[0], current[1] + taken)
            return self._receipt(op_id, anchor.expected_key, count, taken)
        return self.commit_insert(op_id, anchor.expected_key, count)

    def _receipt(self, op_id, key, requested, moved,
                 empty=Reason.CONTAINER_REFUSED,
                 partial=Reason.CONTAINER_PARTIAL):
        """按「实际搬了多少」给状态与理由。

        `empty` / `partial` 由调用方给，理由与**真实原语**保持一致
        （`transfer.slot_extract` 取不到东西时报 `insufficient_stock`、
        `slot_insert` 放不下时报 `no_capacity`）。替身一律报
        `container_refused` 会把「端点的真实理由」这一条盖掉，而
        `_template_slot_take` / `_cmd_template_input_put` 正是要把那个理由
        原样回给玩家的——2026-10-02 的实测里，一句「背包放不下」掩盖了
        「槽里的东西换了」，排障因此多绕了一圈。
        """
        if moved >= requested:
            status, reason = TransferStatus.COMPLETE, ""
        elif moved > 0:
            status, reason = TransferStatus.PARTIAL, partial
        else:
            status, reason = TransferStatus.REFUSED, empty
        return TransferReceipt(op_id, requested, moved, status, reason, key)


class FakeProviders(object):
    def __init__(self, inventory):
        self.inventory = inventory

    def endpoint_of(self, player_id):
        return self.inventory


def stack(name, count=1):
    return RecipeIngredient(name, 0, count)


#: 竖着的两根木板 → 4 根木棍（与 11-04 的夹具同源）。
STICK_RECIPE = PresetRecipe(
    True,
    ("A", "A"),
    {"A": (stack("minecraft:oak_planks"),)},
    (),
    (RecipeIngredient("minecraft:stick", 0, 4),),
)


# -- 夹具 ------------------------------------------------------------------

class Fixture(unittest.TestCase):

    #: 配置编辑要带的那道闸。`Fixture.send` 默认按当前修订号填。
    CONFIG_ACTIONS = frozenset([
        "template_mode", "template_cell_put", "template_cell_clear",
        "template_cell_count", "template_encode", "template_clear", "template_import",
    ])

    def setUp(self):
        self.stand = testsupport.StorageStand()
        self.stand.place(0)
        self.stand.new_medium()
        self.stand.attach()
        self.stand.service.advance_clock(self.stand.next_tick + 10)

        self.store = TemplateStore(limit_of=lambda key: STACK_LIMIT)
        self.state = self.store.of(self.stand.record.ref)
        self.inventory = FakeInventory()
        self.recipes = FakeRecipes(
            by_output={"minecraft:stick": [STICK_RECIPE]},
            by_input={"minecraft:oak_planks": [STICK_RECIPE]},
        )
        self.service = self.stand.service
        self.service.templates = self.store
        self.service.recipes = self.recipes
        self.service.inventories = FakeProviders(self.inventory)
        self.service.template_policy = TemplatePolicy()

    # -- 公共前缀 ------------------------------------------------------

    @property
    def config(self):
        return self.state.config

    @property
    def slots(self):
        return self.state.slots

    def command(self, payload, request_id="r1"):
        ref = self.stand.record.ref
        return DeviceCommand(
            request_id,
            CommandKind.STORAGE,
            CommandSource("p1", ref, 0),
            payload=payload,
            device_ref=ref,
        )

    def send_raw(self, payload, request_id="r1"):
        return self.service.handle(None, self.command(payload, request_id))

    def send(self, payload, request_id="r1"):
        """发一条命令。配置编辑自动带上当前修订号（模拟客户端的行为）。"""
        payload = dict(payload)
        if (payload.get("action") in self.CONFIG_ACTIONS
                and "slot_version" not in payload):
            payload["slot_version"] = self.state.revision
        return self.send_raw(payload, request_id)

    def reasons(self, result):
        return [reason.code for reason in result.reasons]

    def fill_source(self, *entries):
        """往输入九宫格摆几格，走**真正的命令**而不是直接改模型。"""
        for index, key, count in entries:
            result = self.send({
                "action": "template_cell_put", "target": GROUP_SOURCE,
                "cell": index, "key": key.to_dict(), "count": count,
            })
            self.assertTrue(result.is_applied(), result.to_dict())

    def put_factory(self, count=3, slot=0):
        self.inventory.slots[slot] = (FACTORY, count)
        result = self.send({
            "action": "template_input_put", "key": FACTORY.to_dict(),
            "slot": slot, "slot_version": 1,
        })
        self.assertTrue(result.is_applied(), result.to_dict())
        return result

    def encoded(self):
        """产物槽里那块样板解出来的内容。没有则 `None`。"""
        key = self.slots.key_at(RESULT_SLOT)
        if key is None:
            return None
        return patterns.read_pattern(key.to_dict()["identity"]["user_data"])


class TestClear(Fixture):

    def test_clear_both_groups_in_one_revision_and_keep_real_slots(self):
        self.send({"action": "template_mode", "target": "factory"})
        self.fill_source((0, LOG, 1), (8, PLANKS, 2))
        self.config.set_cell(GROUP_OUTPUTS, 2, LOG, 3)
        self.put_factory()
        slots_before = self.slots.to_dict()
        before = self.state.revision
        result = self.send({"action": "template_clear"})
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual((), self.config.occupied(GROUP_SOURCE))
        self.assertEqual((), self.config.occupied(GROUP_OUTPUTS))
        self.assertEqual("factory", self.config.mode)
        self.assertEqual(slots_before, self.slots.to_dict())
        self.assertEqual(before + 1, self.state.revision)

    def test_empty_clear_succeeds_without_new_revision(self):
        before = self.state.revision
        self.assertTrue(self.send({"action": "template_clear"}).is_applied())
        self.assertEqual(before, self.state.revision)

    def test_crafting_clear_removes_inputs_and_detected_output(self):
        self.fill_source((0, PLANKS, 1), (3, PLANKS, 1))
        self.assertIsNotNone(self.service._template_detect(self.state)[0])
        self.assertTrue(self.send({"action": "template_clear"}).is_applied())
        self.assertEqual((None, None), self.service._template_detect(self.state))

    def test_stale_clear_keeps_config(self):
        self.fill_source((0, LOG, 1))
        result = self.send({"action": "template_clear", "slot_version": 99})
        self.assertEqual([Reason.STALE_VERSION], self.reasons(result))
        self.assertEqual((0,), self.config.occupied(GROUP_SOURCE))


class TestMode(Fixture):

    def test_switching_keeps_the_source_and_clears_the_outputs(self):
        self.fill_source((0, LOG, 1))
        self.send({"action": "template_mode", "target": "factory"})
        self.assertEqual("factory", self.config.mode)
        self.assertEqual((0,), self.config.occupied(GROUP_SOURCE))
        self.assertEqual((), self.config.occupied(GROUP_OUTPUTS))

    def test_re_choosing_the_current_mode_changes_nothing(self):
        before = self.state.revision
        result = self.send({"action": "template_mode", "target": "crafting"})
        self.assertTrue(result.is_applied())
        self.assertEqual(before, self.state.revision)

    def test_an_unknown_mode_is_a_bad_parameter(self):
        result = self.send({"action": "template_mode", "target": "smelting"})
        self.assertEqual([Reason.BAD_PARAMETER], self.reasons(result))

    def test_a_stale_revision_is_refused(self):
        result = self.send_raw({
            "action": "template_mode", "target": "factory", "slot_version": 99,
        })
        self.assertEqual([Reason.STALE_VERSION], self.reasons(result))
        self.assertEqual("crafting", self.config.mode)

    def test_a_missing_revision_is_refused(self):
        result = self.send_raw({"action": "template_mode", "target": "factory"})
        self.assertEqual([Reason.BAD_PARAMETER], self.reasons(result))


class TestCellEditing(Fixture):

    def test_putting_an_item_in_a_cell_produces_nothing_real(self):
        before = self.service.query(PLANKS)["stored"]
        self.fill_source((0, PLANKS, 1))
        self.assertEqual(PLANKS, self.config.cell(GROUP_SOURCE, 0)[0])
        self.assertEqual(before, self.service.query(PLANKS)["stored"])
        self.assertEqual({}, self.inventory.slots)

    def test_the_default_count_comes_from_the_policy(self):
        self.send({"action": "template_cell_put", "target": GROUP_SOURCE,
                   "cell": 0, "key": PLANKS.to_dict()})
        self.assertEqual(TemplatePolicy.encode_count_per_cell,
                         self.config.count_of(GROUP_SOURCE, 0))

    def test_overwriting_an_occupied_cell_is_allowed(self):
        self.fill_source((0, LOG, 1))
        self.fill_source((0, PLANKS, 5))
        self.assertEqual(PLANKS, self.config.cell(GROUP_SOURCE, 0)[0])
        self.assertEqual(5, self.config.count_of(GROUP_SOURCE, 0))

    def test_clearing_an_already_empty_cell_succeeds(self):
        result = self.send({"action": "template_cell_clear",
                            "target": GROUP_SOURCE, "cell": 4})
        self.assertTrue(result.is_applied(), result.to_dict())

    def test_a_cell_index_out_of_range_is_refused(self):
        for cell in (9, -1, True, "0"):
            result = self.send({"action": "template_cell_clear",
                                "target": GROUP_SOURCE, "cell": cell})
            self.assertEqual([Reason.OUT_OF_RANGE], self.reasons(result))

    def test_an_unknown_group_is_refused(self):
        result = self.send({"action": "template_cell_clear",
                            "target": "inputs", "cell": 0})
        self.assertEqual([Reason.BAD_PARAMETER], self.reasons(result))

    def test_the_count_can_be_set_within_the_policy_range(self):
        self.fill_source((0, PLANKS, 1))
        result = self.send({"action": "template_cell_count",
                            "target": GROUP_SOURCE, "cell": 0, "count": 4096})
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual(4096, self.config.count_of(GROUP_SOURCE, 0))

    def test_a_count_outside_the_policy_range_is_refused(self):
        self.fill_source((0, PLANKS, 1))
        for count in (0, -3, TemplatePolicy.max_config_count + 1, True):
            result = self.send({"action": "template_cell_count",
                                "target": GROUP_SOURCE, "cell": 0,
                                "count": count})
            self.assertEqual([Reason.OUT_OF_RANGE], self.reasons(result))

    def test_setting_a_count_on_an_empty_cell_is_refused(self):
        result = self.send({"action": "template_cell_count",
                            "target": GROUP_SOURCE, "cell": 7, "count": 3})
        self.assertEqual([Reason.BAD_PARAMETER], self.reasons(result))

    def test_a_bad_key_is_refused(self):
        result = self.send({"action": "template_cell_put",
                            "target": GROUP_SOURCE, "cell": 0, "key": "planks"})
        self.assertEqual([Reason.BAD_PARAMETER], self.reasons(result))

    def test_a_fluid_cannot_sit_in_a_slotted_crafting_cell(self):
        water = ResourceKey.fluid("minecraft:water")
        result = self.send({"action": "template_cell_put",
                            "target": GROUP_SOURCE, "cell": 0,
                            "key": water.to_dict()})
        self.assertEqual([Reason.PATTERN_INVALID], self.reasons(result))

    def test_the_crafting_outputs_are_locked(self):
        for cell in range(3):
            result = self.send({"action": "template_cell_clear",
                                "target": GROUP_OUTPUTS, "cell": cell})
            self.assertEqual([Reason.TEMPLATE_CELL_LOCKED], self.reasons(result))

    def test_the_factory_outputs_are_editable(self):
        self.send({"action": "template_mode", "target": "factory"})
        self.fill_outputs((0, STICK, 8))
        self.assertEqual(STICK, self.config.cell(GROUP_OUTPUTS, 0)[0])

    def fill_outputs(self, *entries):
        for index, key, count in entries:
            result = self.send({
                "action": "template_cell_put", "target": GROUP_OUTPUTS,
                "cell": index, "key": key.to_dict(), "count": count,
            })
            self.assertTrue(result.is_applied(), result.to_dict())


class TestEncode(Fixture):

    def craftable(self, mode="crafting", input_count=1):
        """摆一份能编出东西的配置，并在输入槽放 `input_count` 块工厂样板。"""
        if mode != "crafting":
            self.send({"action": "template_mode", "target": mode})
        self.fill_source((0, PLANKS, 1), (3, PLANKS, 1))
        if mode != "crafting":
            self.fill_outputs((0, STICK, 4))
        self.put_factory(input_count)

    def fill_outputs(self, *entries):
        for index, key, count in entries:
            self.send({"action": "template_cell_put", "target": GROUP_OUTPUTS,
                       "cell": index, "key": key.to_dict(), "count": count})

    def test_it_consumes_one_input_and_produces_one_template(self):
        self.craftable(input_count=3)
        result = self.send({"action": "template_encode"})
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual(2, self.slots.count_at(INPUT_SLOT))
        self.assertEqual(1, self.slots.count_at(RESULT_SLOT))

    def test_the_produced_item_is_always_the_factory_template(self):
        """两种模式都是它——模式记在 `userData` 里，不记在物品类型上。"""
        for mode in ("crafting", "factory"):
            with self.subTest(mode=mode):
                self.setUp()
                self.craftable(mode)
                self.send({"action": "template_encode"})
                self.assertEqual(
                    TEMPLATE_PANEL_ITEM_ID, self.slots.key_at(RESULT_SLOT).name)

    def test_the_encoded_pattern_carries_the_configured_inputs(self):
        self.craftable()
        self.send({"action": "template_encode"})
        definition = self.encoded().definition
        self.assertEqual("crafting", definition.mode)
        self.assertTrue(definition.is_ordered())
        self.assertEqual((0, 3), tuple(s.slot for s in definition.inputs))
        self.assertEqual("minecraft:stick", definition.outputs[0].key.name)
        self.assertEqual(4, definition.outputs[0].count)

    def test_the_crafting_layout_uses_the_cells_the_player_placed(self):
        """User 2026-10-02：所见即所编，不换成配方的规范原料。"""
        self.fill_source((1, PLANKS, 3), (4, PLANKS, 1))
        self.put_factory(1)
        self.send({"action": "template_encode"})
        definition = self.encoded().definition
        self.assertEqual((1, 4), tuple(s.slot for s in definition.inputs))
        self.assertEqual(3, definition.inputs[0].count)

    def test_a_factory_pattern_is_unordered_and_keeps_the_display_order(self):
        self.craftable("factory")
        self.fill_outputs((2, LOG, 1))
        self.send({"action": "template_encode"})
        definition = self.encoded().definition
        self.assertEqual("factory", definition.mode)
        self.assertFalse(definition.is_ordered())
        self.assertEqual(["minecraft:stick", "minecraft:oak_log"],
                         [s.key.name for s in definition.outputs])

    def test_no_recipe_is_refused_rather_than_encoded(self):
        self.fill_source((0, LOG, 1))
        self.put_factory(1)
        result = self.send({"action": "template_encode"})
        self.assertEqual([Reason.TEMPLATE_CONFIG_INVALID], self.reasons(result))
        self.assertEqual(1, self.slots.count_at(INPUT_SLOT))

    def test_an_empty_source_is_refused(self):
        self.put_factory(1)
        result = self.send({"action": "template_encode"})
        self.assertEqual([Reason.TEMPLATE_CONFIG_INVALID], self.reasons(result))

    def test_an_empty_input_slot_is_refused(self):
        self.fill_source((0, PLANKS, 1), (3, PLANKS, 1))
        result = self.send({"action": "template_encode"})
        self.assertEqual([Reason.OUT_OF_RANGE], self.reasons(result))

    def test_a_crafting_template_in_the_input_slot_is_refused(self):
        self.fill_source((0, PLANKS, 1), (3, PLANKS, 1))
        self.slots.set_slot(INPUT_SLOT, CRAFTING, 1)
        result = self.send({"action": "template_encode"})
        self.assertEqual([Reason.PATTERN_KIND_MISMATCH], self.reasons(result))

    def test_an_identical_result_merges_into_the_same_stack(self):
        self.craftable(input_count=2)
        self.send({"action": "template_encode"})
        self.send({"action": "template_encode"})
        self.assertEqual(2, self.slots.count_at(RESULT_SLOT))
        self.assertEqual(0, self.slots.count_at(INPUT_SLOT))

    def test_a_different_result_in_the_output_slot_blocks_the_make(self):
        self.craftable(input_count=2)
        self.send({"action": "template_encode"})
        # 改一格的数量：这条配方只要一块木板，格里有 2 块照样匹配，
        # 但「所见即所编」记的是格里的数——编出来就是另一份配置，
        # 于是产物槽里那块不同配置的样板挡路。
        #
        # **这里不能用「多摆一格」来改配置**：九宫格里多出来的东西会让
        # 配方整个不成立（原版如此，见 `crafting_recipe.match_ordered`），
        # 那样先撞上的是配置无效，验不到产物槽这一条。
        self.fill_source((0, PLANKS, 2))
        result = self.send({"action": "template_encode"})
        self.assertEqual([Reason.SLOT_OCCUPIED], self.reasons(result))
        self.assertEqual(1, self.slots.count_at(INPUT_SLOT))

    def test_a_full_result_stack_is_refused(self):
        self.craftable(input_count=2)
        self.send({"action": "template_encode"})
        key = self.slots.key_at(RESULT_SLOT)
        self.slots.set_slot(RESULT_SLOT, key, STACK_LIMIT)
        result = self.send({"action": "template_encode"})
        self.assertEqual([Reason.NO_CAPACITY], self.reasons(result))
        self.assertEqual(1, self.slots.count_at(INPUT_SLOT))

    def test_the_receipt_carries_a_read_only_state_projection(self):
        self.craftable(input_count=3)
        result = self.send({"action": "template_encode"})
        state = result.to_dict()["payload"]["state"]
        self.assertEqual("crafting", state["mode"])
        self.assertEqual([0, 3], state["source"])
        self.assertEqual(1, state["result"])
        self.assertEqual(2, state["input"])


class TestRealSlots(Fixture):

    def test_a_factory_template_goes_from_the_backpack_into_the_input_slot(self):
        self.put_factory(5)
        self.assertEqual(5, self.slots.count_at(INPUT_SLOT))
        self.assertEqual(0, self.inventory.count_of(FACTORY))

    def test_a_non_template_from_the_backpack_is_refused(self):
        self.inventory.slots[0] = (STICK, 1)
        result = self.send({"action": "template_input_put",
                            "key": STICK.to_dict(), "slot": 0})
        self.assertEqual([Reason.PATTERN_NOT_A_TEMPLATE], self.reasons(result))

    def test_the_removed_crafting_template_is_not_a_template(self):
        self.inventory.slots[0] = (CRAFTING, 1)
        result = self.send({"action": "template_input_put",
                            "key": CRAFTING.to_dict(), "slot": 0})
        self.assertEqual([Reason.PATTERN_NOT_A_TEMPLATE], self.reasons(result))

    def test_an_encoded_factory_template_is_accepted(self):
        """User 2026-10-02：空白与已编码的都收（放进去就是「打算更新它」）。"""
        self.craftable()
        self.send({"action": "template_encode"})
        encoded_key = self.slots.key_at(RESULT_SLOT)
        self.slots.set_slot(RESULT_SLOT, None, 0)
        self.slots.set_slot(INPUT_SLOT, None, 0)
        self.inventory.slots[0] = (encoded_key, 1)
        result = self.send({"action": "template_input_put",
                            "key": encoded_key.to_dict(), "slot": 0})
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual(encoded_key, self.slots.key_at(INPUT_SLOT))

    def test_a_missing_anchor_is_refused(self):
        self.inventory.slots[0] = (FACTORY, 1)
        result = self.send({"action": "template_input_put",
                            "key": FACTORY.to_dict()})
        self.assertEqual([Reason.BAD_PARAMETER], self.reasons(result))

    def test_an_empty_backpack_slot_is_refused(self):
        result = self.send({"action": "template_input_put",
                            "key": FACTORY.to_dict(), "slot": 0})
        self.assertEqual([Reason.INSUFFICIENT_STOCK], self.reasons(result))

    def test_a_different_template_already_in_the_slot_blocks_the_put(self):
        self.slots.set_slot(INPUT_SLOT, CRAFTING, 1)
        self.inventory.slots[0] = (FACTORY, 1)
        result = self.send({"action": "template_input_put",
                            "key": FACTORY.to_dict(), "slot": 0})
        self.assertEqual([Reason.SLOT_OCCUPIED], self.reasons(result))

    def test_a_full_input_slot_blocks_the_put(self):
        self.slots.set_slot(INPUT_SLOT, FACTORY, STACK_LIMIT)
        self.inventory.slots[0] = (FACTORY, 1)
        result = self.send({"action": "template_input_put",
                            "key": FACTORY.to_dict(), "slot": 0})
        self.assertEqual([Reason.SLOT_OCCUPIED], self.reasons(result))

    def test_the_policy_can_limit_the_put_to_one(self):
        class OneAtATime(TemplatePolicy):
            device_slot_put_count = PUT_COUNT_ONE
        self.service.template_policy = OneAtATime()
        self.put_factory(5)
        self.assertEqual(1, self.slots.count_at(INPUT_SLOT))
        self.assertEqual(4, self.inventory.count_of(FACTORY))

    def test_taking_the_result_back_empties_the_slot(self):
        self.put_factory(1)
        self.fill_source((0, PLANKS, 1), (3, PLANKS, 1))
        self.send({"action": "template_encode"})
        result = self.send({"action": "template_output_take"})
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertIsNone(self.slots.entry(RESULT_SLOT))
        # 取回来的那块**带着配置**，所以它的键不等于空白的工厂样板。
        self.assertEqual(
            [TEMPLATE_PANEL_ITEM_ID],
            [entry[0].name for entry in self.inventory.slots.values()],
        )

    def test_taking_from_an_empty_slot_is_refused(self):
        for action in ("template_input_take", "template_output_take"):
            result = self.send({"action": action})
            self.assertEqual([Reason.OUT_OF_RANGE], self.reasons(result))

    def test_a_full_backpack_refuses_the_take_without_losing_anything(self):
        self.put_factory(2)
        self.inventory.capacity = 1
        self.inventory.slots[0] = (STICK, 1)
        result = self.send({"action": "template_input_take"})
        self.assertEqual([Reason.NO_CAPACITY], self.reasons(result))
        self.assertEqual(2, self.slots.count_at(INPUT_SLOT))

    def test_the_take_can_target_a_named_backpack_slot(self):
        self.put_factory(2)
        result = self.send({"action": "template_input_take", "slot": 7,
                            "key": FACTORY.to_dict(), "slot_version": 1})
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual((FACTORY, 2), self.inventory.slots[7])

    def test_targeting_a_slot_holding_another_item_neither_copies_nor_loses(self):
        """**锚上给的 key 不是「要放进去的那一件」。**

        客户端在「选中背包槽 → 点产物槽」时报的是**目标格里现在装着的东西**
        （它同时充当那一格的指纹与版本锚）——而取回方向的插入物只能来自设备
        这一格。服务端不把它换掉的话，插进去的就成了目标格里那件无关物品：
        玩家的东西被白白复制一份，设备里的样板同时被扣掉（`_template_slot_take`
        紧接着就按 `receipt.moved` 扣槽）。2026-10-02 读代码发现，未上机复现。
        """
        self.put_factory(1)
        self.fill_source((0, PLANKS, 1), (3, PLANKS, 1))
        self.send({"action": "template_encode"})
        encoded = self.slots.key_at(RESULT_SLOT)
        self.inventory.slots[7] = (STICK, 1)
        result = self.send({"action": "template_output_take", "slot": 7,
                            "key": STICK.to_dict(), "slot_version": 1})
        self.assertFalse(result.is_applied(), result.to_dict())
        self.assertEqual((STICK, 1), self.inventory.slots[7])       # 没有被复制
        self.assertEqual(encoded, self.slots.key_at(RESULT_SLOT))   # 也没有被扣掉

    def fill_source(self, *entries):
        for index, key, count in entries:
            self.send({"action": "template_cell_put", "target": GROUP_SOURCE,
                       "cell": index, "key": key.to_dict(), "count": count})

    def craftable(self):
        self.fill_source((0, PLANKS, 1), (3, PLANKS, 1))
        self.put_factory(2)


class TestWiring(Fixture):

    def test_an_unwired_service_is_not_ready_rather_than_empty(self):
        self.service.templates = None
        result = self.send({"action": "template_mode", "target": "factory"})
        self.assertEqual([Reason.SERVICE_NOT_READY], self.reasons(result))

    def test_a_block_without_template_slots_is_unsupported(self):
        self.store = TemplateStore(accepts=lambda ref: False)
        self.service.templates = self.store
        result = self.send({"action": "template_mode", "target": "factory"})
        self.assertEqual([Reason.UNSUPPORTED], self.reasons(result))

    def test_the_actions_are_on_the_handler_table(self):
        result = self.send({"action": "template_nonsense"})
        self.assertEqual([Reason.UNSUPPORTED], self.reasons(result))
        self.assertIn("template_encode", result.reasons[0].detail)

    def test_the_schema_version_is_the_one_the_model_writes(self):
        self.assertEqual(TEMPLATE_SCHEMA_VERSION,
                         self.state.to_dict()["schema_version"])


class TestRecipeImport(Fixture):
    def import_recipe(self, **fields):
        payload = {"action": "template_import", "key": STICK.to_dict()}
        payload.update(fields)
        return self.send(payload)

    def test_single_recipe_replaces_grid_without_moving_items(self):
        self.fill_source((8, LOG, 3))
        revision = self.state.revision
        result = self.import_recipe()
        self.assertTrue(result.is_applied(), result.to_dict())
        self.assertEqual((PLANKS, 1), self.config.cell(GROUP_SOURCE, 0))
        self.assertEqual((PLANKS, 1), self.config.cell(GROUP_SOURCE, 3))
        self.assertIsNone(self.config.cell(GROUP_SOURCE, 8))
        self.assertEqual(revision + 1, self.state.revision)
        self.assertEqual([], self.inventory.calls)

    def test_no_recipe_preserves_config_and_revision(self):
        self.fill_source((8, LOG, 3))
        before = self.config.to_dict()
        revision = self.state.revision
        self.assertTrue(self.import_recipe(key=LOG.to_dict()).is_applied())
        self.assertEqual(before, self.config.to_dict())
        self.assertEqual(revision, self.state.revision)

    def test_multiple_recipes_wait_for_valid_choice(self):
        other = PresetRecipe(False, (), {}, (stack("minecraft:oak_log"),),
                             (stack("minecraft:stick", 4),))
        self.recipes.by_output[STICK.name].append(other)
        revision = self.state.revision
        result = self.import_recipe()
        self.assertTrue(result.payload["needs_choice"])
        self.assertEqual(2, len(result.payload["candidates"]))
        self.assertEqual("minecraft:oak_log",
                         result.payload["candidates"][1]["slots"][0]["name"])
        self.assertEqual(revision, self.state.revision)
        self.assertTrue(self.import_recipe(recipe=other.digest()).is_applied())
        self.assertEqual((LOG, 1), self.config.cell(GROUP_SOURCE, 0))
        self.assertIsNone(self.config.cell(GROUP_SOURCE, 3))

    def test_stale_revision_wrong_mode_and_invalid_digest_do_not_write(self):
        self.assertIn(Reason.STALE_VERSION,
                      self.reasons(self.import_recipe(slot_version=99)))
        self.assertIn(Reason.CRAFT_NO_RECIPE,
                      self.reasons(self.import_recipe(recipe="missing")))
        self.config.set_mode(patterns.PATTERN_MODE_FACTORY, self.service.template_policy)
        self.assertIn(Reason.TEMPLATE_CELL_LOCKED, self.reasons(self.import_recipe()))


if __name__ == "__main__":
    unittest.main()
