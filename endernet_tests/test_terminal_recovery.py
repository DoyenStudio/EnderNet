# coding=utf-8
import unittest

import testsupport
from test_bootstrap import FakeWorld
from test_terminal_cursor import CursorPrimitives
from endernet.common.identity import NodeKey
from endernet.common.item_codec import item_dict_from_key
from endernet.common.protocol import CommandKind, CommandSource, DeviceCommand
from endernet.common.resource_key import ResourceKey
from endernet.server.adapters.containers_engine import EngineInventoryProvider
from endernet.server.bootstrap import (
    CRAFTING_TERMINAL_DEVICE_ID, TEMPLATE_TERMINAL_DEVICE_ID, ServerBootstrap,
)
from endernet.server.crafting.template_terminal import GROUP_SOURCE, INPUT_SLOT, RESULT_SLOT
from endernet.server.network.energy import EnergyPolicy
from endernet.server.persistence.terminals import TERMINAL_RECORD_ID, TERMINAL_RECORD_KIND
from endernet.server.runtime.events import NormalizedEvent


CRAFT = NodeKey(0, 2, 65, 0)
TEMPLATE = NodeKey(0, 3, 65, 0)
IRON = ResourceKey.item("minecraft:iron_ingot", user_data={"label": "ore"})
BLANK = ResourceKey.item("endernet:factorical_template")


class TerminalWorld(FakeWorld):
    def __init__(self):
        super().__init__()
        self.markers = {CRAFT: "craft-1", TEMPLATE: "template-1"}
        self.dropped = []

    def terminal_inventory_id(self, node_key):
        return self.markers.get(node_key)


class TestTerminalRecovery(unittest.TestCase):
    def setUp(self):
        self.world = TerminalWorld()
        self.backend = testsupport.MemoryExtraDataBackend()
        self.primitives = CursorPrimitives()
        self.inventory = EngineInventoryProvider(self.primitives)
        self.start()

    def start(self, discover=True):
        self.server = ServerBootstrap(
            world=self.world, persistence_backend=self.backend,
            inventory_provider=self.inventory, energy_policy=EnergyPolicy.dev_stand_in(),
            session_check=lambda source: True,
        )
        self.server.start(bind_engine=False)
        if discover:
            for x in range(4):
                self.place(NodeKey(0, x, 64, 0), "endernet:network_baseband_32")
            self.place(NodeKey(0, 0, 65, 0), "endernet:tiny_network_endpoint")
            self.place(NodeKey(0, 1, 65, 0), "endernet:creative_energy_source")
            self.place(CRAFT, CRAFTING_TERMINAL_DEVICE_ID)
            self.place(TEMPLATE, TEMPLATE_TERMINAL_DEVICE_ID)
            self.server.tick()

    def place(self, pos, device):
        self.server.push(NormalizedEvent.place(pos, device))

    def ref(self, pos):
        return self.server.registry.get(pos).ref

    def send(self, pos, payload):
        ref = self.ref(pos)
        cmd = DeviceCommand(payload["action"], CommandKind.STORAGE, CommandSource("p1", ref, 0),
                            payload=payload, device_ref=ref)
        reply = self.server.submit_command(cmd)
        self.assertTrue(reply.is_applied(), reply.to_dict())

    def grid(self):
        return self.server.workbench.grid_for(self.ref(CRAFT))

    def template(self):
        return self.server.templates.state_for(self.ref(TEMPLATE))

    def test_bag_material_and_templates_survive_normal_save_and_late_discovery(self):
        self.primitives.slots[9] = item_dict_from_key(IRON, 12)
        self.primitives.slots[10] = item_dict_from_key(BLANK, 3)
        bag = self.inventory.endpoint_of("p1")
        slot = bag.slot_view(9)
        self.send(CRAFT, {"action": "grid_move", "target": "bag", "cell": 0,
                          "slot": 9, "slot_version": slot["version"], "key": IRON.to_dict(),
                          "config": {"placing": True, "grid_revision": self.grid().revision,
                                     "fingerprint": slot["canonical"]}})
        self.send(TEMPLATE, {"action": "template_input_put", "key": BLANK.to_dict(),
                             "slot": 10, "slot_version": bag.slot_view(10)["version"]})
        self.send(TEMPLATE, {"action": "template_cell_put", "target": GROUP_SOURCE,
                             "cell": 5, "key": IRON.to_dict(), "count": 4,
                             "slot_version": self.template().revision})
        encoded = ResourceKey.item("endernet:factorical_template", user_data={"pattern": "test"})
        self.template().slots.set_slot(RESULT_SLOT, encoded, 1)
        before = self.template().to_dict()
        self.assertEqual(self.grid().entry(0), (IRON, 12))
        self.assertEqual(self.template().slots.count_at(INPUT_SLOT), 3)
        self.assertIsNone(self.primitives.slots[9])
        self.assertIsNone(self.primitives.slots[10])
        self.server.stop()
        self.start(discover=False)
        # 第二次保存时区块尚未加载，旧记录也要保留。
        self.server.stop()
        self.start()
        self.assertEqual(self.grid().entry(0), (IRON, 12))
        self.assertEqual(self.template().to_dict(), before)
        self.assertIsNone(self.primitives.slots[9])
        self.assertIsNone(self.primitives.slots[10])

    def test_replaced_terminals_do_not_inherit_items_or_drop_offline_orphans(self):
        self.grid().set_slot(0, IRON, 12)
        self.template().slots.set_slot(INPUT_SLOT, BLANK, 3)
        self.server.stop()
        self.world.markers = {CRAFT: "craft-2", TEMPLATE: "template-2"}
        self.start()
        self.assertEqual(self.grid().count_at(0), 0)
        self.assertEqual(self.template().slots.count_at(INPUT_SLOT), 0)
        self.assertEqual(self.world.dropped, [])
        self.server.stop()
        self.start()
        self.assertEqual(self.server.workbench.diagnostics()["orphan_items"], 12)
        self.assertEqual(self.server.templates.diagnostics()["orphan_items"], 3)

    def test_remove_then_replace_and_restart_keeps_new_generation_only(self):
        self.grid().set_slot(0, IRON, 4)
        self.template().slots.set_slot(INPUT_SLOT, BLANK, 3)
        self.server.push(NormalizedEvent.remove(CRAFT, CRAFTING_TERMINAL_DEVICE_ID))
        self.server.push(NormalizedEvent.remove(TEMPLATE, TEMPLATE_TERMINAL_DEVICE_ID))
        self.server.tick()
        self.assertEqual(len(self.world.dropped), 1)
        self.assertEqual(self.world.dropped[0][1], [(BLANK, 3)])
        self.world.markers[CRAFT] = "craft-2"
        self.place(CRAFT, CRAFTING_TERMINAL_DEVICE_ID)
        self.server.tick()
        self.assertEqual(self.grid().count_at(0), 0)
        self.grid().set_slot(0, IRON, 7)
        self.server.stop()
        self.start()
        self.assertEqual(self.grid().count_at(0), 7)
        self.assertEqual(self.server.workbench.diagnostics()["orphan_items"], 4)
        self.assertEqual(len(self.world.dropped), 1)

    def test_unavailable_block_entity_does_not_revive_or_replace_old_inventory(self):
        self.grid().set_slot(0, IRON, 12)
        self.server.stop()
        self.world.markers[CRAFT] = None
        self.start()
        self.assertIsNone(self.grid())
        self.world.markers[CRAFT] = "craft-1"
        self.assertEqual(self.grid().entry(0), (IRON, 12))

    def test_chunk_reload_rechecks_the_actual_terminal_identity(self):
        self.grid().set_slot(0, IRON, 12)
        original_ref = self.ref(CRAFT)
        self.server.push(NormalizedEvent.chunk_unloaded(CRAFT.chunk_key()))
        self.server.tick()
        self.world.markers[CRAFT] = "replacement"
        self.place(CRAFT, CRAFTING_TERMINAL_DEVICE_ID)
        self.server.tick()
        self.assertEqual(self.ref(CRAFT), original_ref)
        self.assertEqual(self.grid().count_at(0), 0)
        self.assertEqual(self.server.workbench.diagnostics()["orphan_items"], 12)

    def test_removing_unidentified_terminal_preserves_previous_contents_without_dropping(self):
        self.template().slots.set_slot(INPUT_SLOT, BLANK, 3)
        self.server.stop()
        self.world.markers[TEMPLATE] = None
        self.start()
        self.server.push(NormalizedEvent.remove(TEMPLATE, TEMPLATE_TERMINAL_DEVICE_ID))
        self.server.tick()
        self.assertEqual(self.world.dropped, [])
        self.assertEqual(self.server.templates.diagnostics()["orphan_items"], 3)

    def test_unknown_nested_slot_version_is_not_rewritten_as_empty(self):
        self.template().slots.set_slot(INPUT_SLOT, BLANK, 3)
        self.server.stop()
        repo = self.server.repository
        record = repo.load(TERMINAL_RECORD_KIND, TERMINAL_RECORD_ID)
        saved = record.payload[TEMPLATE_TERMINAL_DEVICE_ID]["states"][0]["state"]
        saved["slots"] = {"schema_version": 999, "future_slots": saved["slots"]["slots"]}
        repo.write(record)
        repo.commit()
        self.start(discover=False)
        self.assertFalse(self.server.recovery.is_open())
        self.server.stop()
        self.assertEqual(repo.load(TERMINAL_RECORD_KIND, TERMINAL_RECORD_ID).payload, record.payload)

    def test_unknown_terminal_record_is_preserved_on_stop(self):
        self.grid().set_slot(0, IRON, 12)
        self.server.stop()
        repo = self.server.repository
        record = repo.load(TERMINAL_RECORD_KIND, TERMINAL_RECORD_ID)
        record.payload["schema_version"] = 999
        repo.write(record)
        repo.commit()
        self.start(discover=False)
        self.assertFalse(self.server.recovery.is_open())
        self.server.stop()
        self.assertEqual(repo.load(TERMINAL_RECORD_KIND, TERMINAL_RECORD_ID).payload, record.payload)
