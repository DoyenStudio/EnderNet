# coding=utf-8
"""资源身份、引擎转换、同步与存档恢复之间的回归。"""
import copy
import unittest

import testsupport  # noqa: F401
from endernet.common import protocol
from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.resource_key import ResourceKey, canonical_tree, tree_text
from endernet.common.item_codec import item_key, item_key_from_dict, item_dict_for_drop, item_dict_from_key, item_from_key, normalize_engine_item, unbox_engine_value
from endernet.common.protocol import StorageChanged
from endernet.common.resource_sync import ResourceDescriptions, ResourceResyncRequired
from endernet.server.adapters.containers import ContainerEntry
from endernet.server.persistence.resources import RestoredResourceEntry, UnavailableResourceError
from endernet.common import settings


def wire(value):
    # Simulate ownership separation, not a JSON codec or the actual ExtraData backend.
    return copy.deepcopy(value)


class ResourceContractTests(unittest.TestCase):
    def test_debug_off_preserves_conversion_and_sync(self):
        from unittest.mock import patch
        with patch.object(settings, "DEBUG", False):
            raw = {"nested": [u"中文", 3]}
            key = ResourceKey.item("test:item", user_data=raw, enchant_data=[(9, 5)])
            raw["nested"].append(4)
            self.assertEqual(item_key_from_dict(wire(item_dict_from_key(key))), key)
            sender = ResourceDescriptions("net", "epoch")
            receiver = ResourceDescriptions("net", "epoch")
            event = StorageChanged("net", key, 50, 5, 1)
            decoded = StorageChanged.from_dict(wire(event.to_dict(sender)), receiver)
            self.assertEqual(decoded.resource_key, key)
            entry = RestoredResourceEntry(wire(ContainerEntry(key, 50).to_dict()), lambda key: True)
            self.assertEqual(entry.require_key(), key)
            self.assertEqual(entry.count, 50)

    def test_debug_off_still_rejects_invalid_external_data(self):
        from unittest.mock import patch
        key = ResourceKey.item("test:item")
        with patch.object(settings, "DEBUG", False):
            for field, value in (("aux", 1.5), ("schema_version", 99), ("identity", [])):
                data = key.to_dict()
                data[field] = value
                with self.assertRaises((TypeError, ValueError)):
                    ResourceKey.from_dict(data)
            data = item_dict_from_key(key)
            data["count"] = True
            with self.assertRaises(TypeError):
                item_key_from_dict(data)
            receiver = ResourceDescriptions("net", "epoch")
            with self.assertRaises(ResourceResyncRequired):
                receiver.resolve("old-epoch", 1)

    def test_input_and_exports_cannot_mutate_key(self):
        for make in (ResourceKey.item, ResourceKey.fluid):
            raw = {"nested": [1, {"text": u"中文😀"}]}
            key = make("test:resource", user_data=raw)
            original = key.to_dict()
            inventory = {key: 5}
            raw["nested"].append(2)
            key.raw_identity["user_data"]["nested"].append(3)
            key.to_dict()["identity"]["user_data"]["nested"].append(4)
            self.assertEqual(key.to_dict(), original)
            self.assertEqual(inventory[ResourceKey.from_dict(wire(original))], 5)
            copied = copy.deepcopy(key)
            self.assertEqual(copied, key)
            self.assertIsNot(copied, key)
            copied._raw["user_data"]["nested"].append(5)
            self.assertEqual(key.to_dict(), original)

    def test_enchantment_source_is_detached(self):
        enchants = [[9, 5]]
        key = ResourceKey.item("test:sword", enchant_data=enchants)
        enchants[0][1] = 1
        self.assertEqual(key, ResourceKey.item("test:sword", enchant_data=[(9, 5)]))
        self.assertEqual(key, ResourceKey.from_dict(wire(key.to_dict())))

    def test_payload_roundtrip_preserves_types_and_empty_values(self):
        for raw in (None, {}, {"v": [True, 1, 1.0, -0.0, 0.3, 2 ** 65, u"中文😀"]}):
            key = ResourceKey.item("test:item", user_data=raw, enchant_data=[])
            restored = ResourceKey.from_dict(wire(key.to_dict()))
            self.assertEqual(key, restored)
            self.assertEqual(key.canonical(), restored.canonical())
            self.assertEqual(key.to_dict(), restored.to_dict())

    def test_metadata_is_preserved_without_json_coercion(self):
        raw = {1: (2, {"value": [3]}), "1": "text key"}
        key = ResourceKey.item("test:item", user_data=raw)
        restored = ResourceKey.from_dict(wire(key.to_dict()))
        self.assertEqual(restored, key)
        self.assertEqual(restored.raw_identity["user_data"], raw)
        reversed_raw = dict(reversed(list(raw.items())))
        self.assertEqual(ResourceKey.item("test:item", user_data=reversed_raw), key)
        nested = "x" * 262145
        for unused in range(40):
            nested = [nested]
        large = ResourceKey.item("test:item", user_data={"nested": nested})
        self.assertEqual(ResourceKey.from_dict(large.to_dict()), large)

    def test_strict_schema_and_legacy_loading(self):
        key = ResourceKey.item("test:item")
        legacy = key.to_dict()
        del legacy["schema_version"]
        self.assertEqual(ResourceKey.from_dict(legacy), key)
        for changes in ({"aux": 1.5}, {"aux": True}, {"aux": -1}, {"identity": []},
                        {"identity": None}, {"identity": {"future_field": 1}},
                        {"schema_version": 2}, {"schema_version": True}, {"future_field": 1}):
            payload = key.to_dict()
            payload.update(changes)
            with self.assertRaises((TypeError, ValueError)):
                ResourceKey.from_dict(payload)
        fluid = ResourceKey.fluid("test:fluid").to_dict()
        fluid["aux"] = 1
        with self.assertRaises(ValueError):
            ResourceKey.from_dict(fluid)

    def test_fixed_fingerprints(self):
        self.assertEqual(tree_text(canonical_tree(u"中文😀")), u"(1:s10:中文😀)")
        self.assertEqual(tree_text(canonical_tree(0.3)), "(1:f19:0.29999999999999999)")
        self.assertEqual(tree_text(canonical_tree({"b": 2, "a": 1})),
                         "(1:d22:(8:(1:s1:a)8:(1:i1:1))22:(8:(1:s1:b)8:(1:i1:2)))")
        node = canonical_tree({u"\U0001f600": 1, u"\ue000": 2})
        self.assertEqual(node[1][0][1], u"\ue000")


#: 2026-09-25 游戏内实测读数：引擎对一个**原版箱子**里的石头返回的 16 个字段。
#: 逐字段照抄自 `test_containers_engine.py` 的同一份夹具——两处同源是刻意的，
#: 这份形状正是「客户端与服务端各读一次同一个槽」时引擎给的那一份。
MEASURED_ENGINE_ITEM = {
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


def _load_real_item_module():
    """离线加载**真的** `tooldelta.define.item`。

    真 `tooldelta/__init__.py` 在模块级 import 引擎，脱离游戏一导入就炸；
    这里按文件路径单独加载 `item.py`，并给它一个假的 `internal` 包。
    """
    import importlib.util
    import os
    import sys
    import types
    from unittest.mock import patch

    path = os.path.join(testsupport.SCRIPTS_ROOT, "tooldelta", "define", "item.py")
    internal = types.ModuleType("resource_codec_test.internal")
    internal.InServerEnv = lambda: False
    spec = importlib.util.spec_from_file_location("resource_codec_test.define.item", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {internal.__name__: internal}):
        spec.loader.exec_module(module)
    return module


def _register_stack_size(module, key, size):
    """给 `GetBasicInfo()` 一个可查的堆叠上限，免得它去问引擎。"""
    basic = module.BasicItemInfo()
    basic.maxStackSize = size
    module.itemBasicInfoPool[(key.name, key.aux)] = basic


class ItemCodecTests(unittest.TestCase):
    def test_real_tooldelta_class_without_engine_bootstrap(self):
        module = _load_real_item_module()
        key = ResourceKey.item("test:item", aux=2, enchant_data=[],
                               mod_enchant_data=[("test:enchant", 3)], durability=None,
                               extra_id="unique", custom_tips=u"中文", user_data={"v": [1]})
        _register_stack_size(module, key, 16)
        item = item_from_key(key, 12, module.Item)
        self.assertEqual(item_key(item), key)
        self.assertEqual(item_key_from_dict(wire(item.marshal())), key)
        item.userData["v"].append(2)
        self.assertNotEqual(item_key(item), key)
        with self.assertRaises(ValueError):
            item_from_key(key, 17, module.Item)

    def test_the_client_key_matches_the_server_key_for_a_measured_item(self):
        """**界面按槽取放的身份回归位。**

        客户端读自己的背包拿到的是一份引擎物品字典，服务端读同一个槽拿到的
        是另一份；两边算出的 `ResourceKey` 必须**完全相同**，否则
        `SlotAnchor` 的指纹核对恒不通过，每一次带槽请求都被判成
        `SLOT_CHANGED`——玩家看到的是「点了没反应」，日志里只有一条回执。

        这条不是假想的：`Item.from_dict` 把缺席的 `durability` 补成 `0`，
        而服务端把 `0` 归一成 `None`，`ResourceKey` 又把这两者当成两个身份。
        因此客户端**必须**回到原始字典走归一化，不能按属性取身份。
        """
        from endernet.common.item_codec import (
            engine_item_key,
            normalize_engine_item,
        )
        module = _load_real_item_module()
        measured = dict(MEASURED_ENGINE_ITEM)
        item = module.Item.from_dict(measured)

        server_key = item_key_from_dict(normalize_engine_item(dict(measured)))
        self.assertEqual(server_key, ResourceKey.item("minecraft:stone"))
        self.assertEqual(engine_item_key(item), server_key)
        self.assertEqual(engine_item_key(item).canonical(), server_key.canonical())

    def test_a_hand_built_item_has_no_engine_dictionary(self):
        """手工构造的物品没有原始字典，`engine_item_key` 要**明确拒绝**。

        静默算一个键出来正是这条修法要消灭的失效模式：那种错不会报，
        只会让服务端一直说「槽里的东西不是你说的那件」。
        """
        from endernet.common.item_codec import engine_item_key
        module = _load_real_item_module()
        key = ResourceKey.item("minecraft:stone")
        _register_stack_size(module, key, 64)
        with self.assertRaises(ValueError):
            engine_item_key(item_from_key(key, 1, module.Item))

    def test_counts_empty_and_unknown_metadata(self):
        key = ResourceKey.item("test:item")
        for count in (1, 64, 2 ** 30):
            self.assertEqual(item_key_from_dict(item_dict_from_key(key, count)), key)
        data = item_dict_from_key(key)
        data["count"] = 0
        self.assertIsNone(item_key_from_dict(data))
        for count in (True, 1.1, -1):
            data["count"] = count
            with self.assertRaises((TypeError, ValueError)):
                item_key_from_dict(data)
        data["count"] = 1
        data["futureIdentity"] = 1
        with self.assertRaises(ValueError):
            item_key_from_dict(data)
        with self.assertRaises(TypeError):
            item_dict_from_key(ResourceKey.fluid("test:water"))


class DescriptionTests(unittest.TestCase):
    def setUp(self):
        self.sender = ResourceDescriptions("net", "session-A")
        self.receiver = ResourceDescriptions("net", "session-A")
        self.key = ResourceKey.item("test:item", user_data={"v": [1]})

    def test_new_description_then_quantity_only_and_click(self):
        first = StorageChanged("net", self.key, 2 ** 30, 3, 1)
        payload = wire(first.to_dict(self.sender))
        self.assertIn("description", payload["resource_ref"])
        restored = StorageChanged.from_dict(payload, self.receiver)
        self.assertEqual(restored.resource_key, self.key)
        self.assertEqual(restored.stored, 2 ** 30)
        second = wire(StorageChanged("net", self.key, 7, 0, 2).to_dict(self.sender))
        self.assertNotIn("description", second["resource_ref"])
        self.assertEqual(StorageChanged.from_dict(second, self.receiver).stored, 7)
        ref = second["resource_ref"]
        self.assertEqual(self.sender.resolve(ref["epoch"], ref["serial"]), self.key)
        self.assertEqual(StorageChanged.from_dict(wire(first.to_dict())).resource_key, self.key)

    def test_missing_description_requires_resync(self):
        event = StorageChanged("net", self.key, 10, 0, 1)
        event.to_dict(self.sender)
        with self.assertRaises(ResourceResyncRequired):
            StorageChanged.from_dict(wire(event.to_dict(self.sender)), self.receiver)
        payload = event.to_dict(self.sender, include_description=True)
        self.assertEqual(StorageChanged.from_dict(wire(payload), self.receiver).resource_key, self.key)

    def test_old_epoch_rebinding_and_wrong_network_are_rejected(self):
        payload = StorageChanged("net", self.key, 10, 0, 1).to_dict(self.sender)
        with self.assertRaises(ResourceResyncRequired):
            StorageChanged.from_dict(payload, ResourceDescriptions("net", "session-B"))
        with self.assertRaises(ValueError):
            StorageChanged.from_dict(payload, ResourceDescriptions("other-net", "session-A"))
        StorageChanged.from_dict(payload, self.receiver)
        payload["resource_ref"]["description"] = ResourceKey.item("test:other").to_dict()
        with self.assertRaises(ResourceResyncRequired):
            StorageChanged.from_dict(payload, self.receiver)
        self.assertEqual(self.receiver.resolve("session-A", 1), self.key)

    def test_invalid_counts_do_not_register_description(self):
        payload = StorageChanged("net", self.key, 10, 0, 1).to_dict(self.sender)
        payload["reserved"] = 11
        with self.assertRaises(ValueError):
            StorageChanged.from_dict(payload, self.receiver)
        with self.assertRaises(ResourceResyncRequired):
            self.receiver.resolve("session-A", 1)

    def test_bounded_mapping_does_not_reuse_serials(self):
        table = ResourceDescriptions("net", "session", max_entries=1)
        table.encode(self.key)
        with self.assertRaises(ResourceResyncRequired):
            table.encode(ResourceKey.item("test:other"))
        self.assertEqual(table.resolve("session", 1), self.key)


def wire_keys(value, out=None):
    """递归收集一个线格式载荷里出现过的全部字段名。"""
    if out is None:
        out = set()
    if isinstance(value, dict):
        for name, item in value.items():
            out.add(name)
            wire_keys(item, out)
    elif isinstance(value, list):
        for item in value:
            wire_keys(item, out)
    return out


class SyncMessageTests(unittest.TestCase):
    """11-01 / 11-02 的线格式：两个版本各有其名，边界检查无条件执行。"""

    def setUp(self):
        self.sender = ResourceDescriptions("net", "epoch-1")
        self.receiver = ResourceDescriptions("net", "epoch-1")
        self.stone = ResourceKey.item("minecraft:stone")
        self.dirt = ResourceKey.item("minecraft:dirt")
        self.ref = DeviceRef(NodeKey(0, 1, 2, 3), 4)

    def envelope_of(self, message):
        """按消息方向补上必要路由字段，返回一个可上线的信封。"""
        if message.kind in protocol.SERVER_TO_CLIENT_KINDS:
            return protocol.SyncEnvelope(message, "s1", "net", "epoch-1")
        session_id = "" if message.kind == protocol.MSG_SESSION_OPEN else "s1"
        return protocol.SyncEnvelope(message, session_id)

    def every_message(self):
        """每种消息各一条实例。**新增种类忘了加进来会让下面几条用例失效**，
        因此另有 `test_every_registered_kind_is_covered_here` 兜底。"""
        return (
            protocol.SessionOpen("r1", self.ref, 0),
            protocol.SessionClose("r2"),
            protocol.SnapshotRequest("r3", 7),
            protocol.Ack("r4", "transfer-1", 3),
            protocol.KeepaliveMessage("r4b"),
            protocol.SessionReady(11, 12),
            protocol.InventorySnapshot(36, [
                {"slot": slot, "canonical": "", "count": 0, "slot_version": 1}
                for slot in range(36)]),
            protocol.SnapshotBegin("snap-1", 12, 3),
            protocol.SnapshotChunk("snap-1", 0, [(self.stone, 5, 1)]),
            protocol.SnapshotEnd("snap-1", 2),
            protocol.ItemDelta("batch-1", 0, 2, 12, 13, [(self.stone, 5, 1)], [self.dirt]),
            protocol.ResyncRequired("stale_version", "缺口已回收"),
            protocol.SyncError("r5", "stale_version", "期望版本过期"),
            protocol.TerminalCommand(
                "r6", "inv_take", 11, self.stone.to_dict(), 32, 3, 9
            ),
            protocol.CommandResultMessage(
                "r6", protocol.CommandResult.ok("r6", {"action": "inv"}).to_dict()
            ),
            protocol.DeviceStatusMessage(
                "endernet:storage_driver",
                3,
                True,
                "在线",
                "ok",
                "runnable",
                "运行中",
                11,
                reasons=[{"code": "ok", "detail": ""}],
            ),
            protocol.CraftStateMessage(
                4,
                [{"slot": slot, "canonical": "", "count": 0, "name": "", "aux": 0}
                 for slot in range(protocol.CRAFT_SLOT_COUNT)],
                "deadbeef",
                {"name": "minecraft:stick", "aux": 0, "count": 4},
            ),
            template_message(),
        )

    def test_every_registered_kind_is_covered_here(self):
        covered = set(message.kind for message in self.every_message())
        self.assertEqual(covered, set(protocol.sync_message_kinds()))

    def test_inventory_snapshot_rejects_incomplete_and_invalid_versions(self):
        rows = [{"slot": slot, "canonical": "", "count": 0, "slot_version": 1}
                for slot in range(36)]
        with self.assertRaises(ValueError):
            protocol.InventorySnapshot(1, rows[:35])
        for invalid in (True, -1, 0, "1"):
            rows[0]["slot_version"] = invalid
            with self.assertRaises((ValueError, TypeError)):
                protocol.InventorySnapshot(1, rows)

    def test_message_kind_registry_matches_direction_sets(self):
        """方向表与注册表必须覆盖同一批种类，且互不重叠。"""
        self.assertEqual(
            protocol.CLIENT_TO_SERVER_KINDS | protocol.SERVER_TO_CLIENT_KINDS,
            set(protocol.sync_message_kinds()),
        )
        self.assertEqual(
            protocol.CLIENT_TO_SERVER_KINDS & protocol.SERVER_TO_CLIENT_KINDS, set()
        )

    def test_no_bare_version_field_on_the_wire(self):
        """**两个版本必须各有其名。**

        同名是「客户端拿 UI 发布版本当交易预期版本」唯一可能的入口：
        一旦线上有一个含义模糊的 `version`，两边各自往里面塞不同的东西
        也能收发自如，而这种错在离线与真机都不会报错，只会让命令偶尔
        被判成过期版本。

        注意 `storage_changed` 通知里的 `version`（提交版本）不在此列——
        它不是终端通道的一部分，也已有生产者之外的含义。
        """
        for message in self.every_message():
            for descriptions in (None, self.sender):
                payload = self.envelope_of(message).to_dict(descriptions)
                self.assertNotIn(
                    "version",
                    wire_keys(payload),
                    "%s 的载荷里出现了裸 version 字段" % (message.kind,),
                )

    def test_large_counts_survive_roundtrip_exactly(self):
        """大数量不得经浮点往返。2^30 是验收里点名的量级。"""
        for count in (0, 1, 64, 2 ** 30, 2 ** 30 + 1, protocol.PROTOCOL_MAX_COUNT):
            sender = ResourceDescriptions("net", "epoch-1")
            receiver = ResourceDescriptions("net", "epoch-1")
            delta = protocol.ItemDelta("b", 0, 1, 1, 2, [(self.stone, count, 0)])
            envelope = protocol.SyncEnvelope(delta, "s1", "net", "epoch-1")
            restored = protocol.SyncEnvelope.from_dict(
                envelope.to_dict(sender), receiver
            )
            self.assertEqual(restored.message.upserts[0][1], count)
            self.assertIsInstance(restored.message.upserts[0][1], int)

    def test_require_count_rejects_bool_negative_and_float(self):
        for bad in (True, False, -1, 1.0, "3", None):
            with self.assertRaises((TypeError, ValueError)):
                protocol.require_count(bad, "count")
        self.assertEqual(protocol.require_count(2 ** 30, "count"), 2 ** 30)
        with self.assertRaises(ValueError):
            protocol.require_count(protocol.PROTOCOL_MAX_COUNT + 1, "count")

    def test_unknown_message_kind_is_rejected_not_ignored(self):
        """静默忽略未知种类会把「协议版本不一致」表现成「界面不刷新」。"""
        with self.assertRaises(ValueError):
            protocol.decode_sync_message({"kind": "no_such_message"})
        with self.assertRaises(ValueError):
            protocol.decode_sync_message({"kind": None})

    def test_spoofed_identity_field_is_rejected(self):
        """消息体里自称的身份不是「被忽略」，而是**整条消息非法**。

        只忽略的话，注入的字段会静静躺在那里，而任何一次「顺手读一下」
        的改动都能让它生效。拒绝未知字段把这条路堵死在协议边界上。
        """
        payload = protocol.SessionOpen("r1", self.ref, 0).to_dict()
        payload["player_id"] = "attacker"
        with self.assertRaises(ValueError):
            protocol.decode_sync_message(payload)
        self.assertNotIn("player", protocol.SessionOpen("r1", self.ref, 0).to_dict())

    def test_schema_version_is_validated_before_anything_else(self):
        envelope = protocol.SyncEnvelope(
            protocol.SessionReady(1, 2), "s1", "net", "epoch-1"
        )
        payload = envelope.to_dict()
        payload["schema_version"] = protocol.SYNC_SCHEMA_VERSION + 1
        with self.assertRaises(ValueError):
            protocol.SyncEnvelope.from_dict(payload)

    def test_the_error_message_is_the_only_s2c_message_without_a_session(self):
        """`error` 是那条规则**唯一的**例外，且它自带 `request_id`。

        会话不存在、会话属于别人、协议版本对不上——这些情形下服务端根本
        没有可填的会话与网络。若也要求非空，就只剩两条路：不发（客户端只
        看到界面不响应），或者硬编一个假会话（客户端会以为那个会话存在）。
        带上 `request_id` 之后它仍能挂回对应的那次请求，不是无名广播。
        """
        envelope = protocol.SyncEnvelope(protocol.SyncError("r1", protocol.Reason.SESSION_INVALID))
        self.assertEqual(envelope.session_id, "")
        self.assertEqual(envelope.network_id, "")
        self.assertEqual(envelope.message.request_id, "r1")

        # 例外**只此一条**：把空会话下仍被放行的种类列出来，它必须恰好是
        # `error`。写成「排除 error 再检查其余」的话，将来多出第二个例外
        # 也不会有人发现。
        tolerated = []
        for message in self.every_message():
            if message.kind not in protocol.SERVER_TO_CLIENT_KINDS:
                continue
            try:
                protocol.SyncEnvelope._validate(message, "", "", "", protocol.VIEW_ITEM)
            except ValueError:
                continue
            tolerated.append(message.kind)
        self.assertEqual(tolerated, [protocol.MSG_ERROR])

    def test_s2c_messages_require_session_network_and_epoch(self):
        """每个服务端→客户端包都必须能回答「这是谁的、哪一代的」。

        `error` 不在此列（见上一条），其余一个都不能漏。
        """
        for message in self.every_message():
            if message.kind not in protocol.SERVER_TO_CLIENT_KINDS:
                continue
            if message.kind == protocol.MSG_ERROR:
                continue
            for field in ("session_id", "network_id", "epoch"):
                envelope = protocol.SyncEnvelope(
                    message, "s1", "net", "epoch-1", protocol.VIEW_ITEM
                )
                setattr(envelope, field, "")
                with self.assertRaises(ValueError, msg="%s 的 %s" % (message.kind, field)):
                    protocol.SyncEnvelope._validate(
                        envelope.message, envelope.session_id, envelope.network_id,
                        envelope.epoch, envelope.view_kind,
                    )

    def test_c2s_messages_require_a_session_except_open(self):
        self.assertIsNone(
            protocol.SyncEnvelope._validate(
                protocol.SessionOpen("r1", self.ref, 0), "", "", "", protocol.VIEW_ITEM
            ),
            "session_open 是唯一一条在会话存在之前发出的消息",
        )
        for message in (protocol.SessionClose("r2"), protocol.Ack("r3", "t", 0)):
            with self.assertRaises(ValueError):
                protocol.SyncEnvelope._validate(
                    message, "", "", "", protocol.VIEW_ITEM
                )

    def test_order_view_is_known_but_not_yet_produced(self):
        """`order` 是**已知**种类却没有生产者，与「发了个不认识的种类」不同。

        前者要回 `unsupported`（还没做），后者是协议错误（发错了）。
        把两者混成一条会让人以为订单视图只是坏了，而不是还没接。
        """
        self.assertIn(protocol.VIEW_ORDER, protocol.VIEW_KINDS)
        self.assertNotIn(protocol.VIEW_ORDER, protocol.VIEW_KINDS_WITH_PRODUCER)
        with self.assertRaises(ValueError):
            protocol.SyncEnvelope._validate(
                protocol.SessionReady(1, 2), "s1", "net", "epoch-1", "no_such_view"
            )

    def test_snapshot_and_delta_share_one_item_encoding(self):
        """快照与增量走同一套条目编码，两条路径不会各自漂移。"""
        for message in (
            protocol.SnapshotChunk("snap-1", 0, [(self.stone, 5, 1)]),
            protocol.ItemDelta("b", 0, 1, 1, 2, [(self.stone, 5, 1)]),
        ):
            payload = self.envelope_of(message).to_dict(self.sender)
            entries = payload["message"].get("items") or payload["message"]["upserts"]
            self.assertIn("resource_ref", entries[0])
            self.assertEqual(entries[0]["stored"], 5)

    def test_reserved_above_stored_is_rejected_on_the_wire(self):
        payload = self.envelope_of(
            protocol.SnapshotChunk("snap-1", 0, [(self.stone, 5, 1)])
        ).to_dict(self.sender)
        payload["message"]["items"][0]["reserved"] = 6
        with self.assertRaises(ValueError):
            protocol.SyncEnvelope.from_dict(payload, self.receiver)

    def test_delta_packet_index_must_be_inside_the_batch(self):
        with self.assertRaises(ValueError):
            protocol.ItemDelta("b", 2, 2, 1, 2)
        with self.assertRaises(ValueError):
            protocol.ItemDelta("b", 0, 0, 1, 2)

    def test_command_absent_optionals_are_still_written(self):
        """可选字段**必须显式写 `None`**，因为 `_require_fields` 也拒缺项。

        「可选」在协议这一层不表示「可以不写」：任何一条字段缺席都会让
        整条消息被判为缺项。这条用例把那个约定钉住——只拒缺项不拒多项
        的宽松实现对下面 `test_command_*` 那几条毫无意义。
        """
        payload = protocol.TerminalCommand("r1", "inv_take").to_dict()
        for field in protocol.TerminalCommand.FIELDS:
            self.assertIn(field, payload, "字段 %s 缺席了" % (field,))
        restored = protocol.decode_sync_message(payload)
        self.assertIsNone(restored.count)
        self.assertIsNone(restored.slot)
        self.assertIsNone(restored.into_slot)

    def test_command_rejects_injected_identity_and_cursor(self):
        """命令里塞身份或 `cursor` 是**整条消息非法**，不是「被忽略」。

        `cursor` 尤其重要：`StorageService._cursor_refusal` 只在
        `payload.get("cursor")` 为真时才拦，而 R12 尚未定案。不发它是
        「缺席」；能发它就成了「客户端可以自称手里有没有东西」，
        那等于用一个未定的规则做了个看着能用的替身。
        """
        base = protocol.TerminalCommand("r1", "inv_take", 11, None, 32).to_dict()
        self.assertNotIn("cursor", base)
        self.assertNotIn("player_id", base)
        # `target` 不在此列：它是**合法字段**（`inv_quick_move` 用），
        # 只是别的动作不读它。字段集是统一的，逐动作收窄等于让「这条命令
        # 该有哪些键」散到两处，而漏收的那一处不会报错。
        for injected in ({"cursor": {"item": "x"}}, {"player_id": "attacker"},
                         {"device_ref": {}}, {"op_id": "sneaky"}):
            payload = dict(base)
            payload.update(injected)
            with self.assertRaises(ValueError, msg="%r 未被拒绝" % (injected,)):
                protocol.decode_sync_message(payload)
        # 真正合法的 `target` 走构造函数而不是绕过字段检查。
        self.assertEqual(
            protocol.TerminalCommand("r1", "inv_quick_move", target="network")
            .payload()["target"],
            "network",
        )

    def test_command_payload_maps_to_what_the_storage_service_reads(self):
        """`payload()` 是线格式与 `StorageService` 之间**唯一**的字段名映射。

        服务端那一侧一律 `payload.get(...)` 读，所以缺席的键不写即可；
        而 `count` 总是写上——`inv_quick_move` 靠它的假值判断「整槽」。
        """
        command = protocol.TerminalCommand(
            "r1", "inv_merge", 11, self.stone.to_dict(), 4, 3, 9,
            into_slot=5, into_slot_version=2,
        )
        payload = command.payload()
        self.assertEqual(payload["action"], "inv_merge")
        self.assertEqual(payload["count"], 4)
        self.assertEqual(payload["slot"], 3)
        self.assertEqual(payload["slot_version"], 9)
        self.assertEqual(payload["into_slot"], 5)
        self.assertEqual(payload["into_slot_version"], 2)
        # 不发 `op_id`：服务端取 `payload.get("op_id") or request_id`，
        # 多发一个只会造出「账本按 A 去重、动作按 B 记账」的缝隙。
        self.assertNotIn("op_id", payload)
        # `expected_index_version` 属消息层，不是动作载荷。
        self.assertNotIn("expected_index_version", payload)

    def test_command_result_rejects_a_mismatched_request_id(self):
        """消息层的 `request_id` 与结果里的那一份**必须相等**（指纹式核对）。

        两份不一致说明消息在某一层被拼错了，此时按哪一份去挂回请求都是猜：
        按消息层挂，界面会把结果贴到另一次操作上；按结果挂，客户端的在途
        记录则永远释放不掉。
        """
        result = protocol.CommandResult.ok("r1", {}).to_dict()
        with self.assertRaises(ValueError):
            protocol.CommandResultMessage("OTHER", result)
        # 走一遍线也不行：解析路径的检查无条件执行。
        payload = protocol.CommandResultMessage("r1", result).to_dict()
        payload["request_id"] = "OTHER"
        with self.assertRaises(ValueError):
            protocol.decode_sync_message(payload)

    def test_command_result_rejects_a_malformed_result(self):
        """形状不对的结果**当场拒绝**：界面拿它做不了任何判断。"""
        good = protocol.CommandResult.rejected(
            "r1", [protocol.BlockReason(protocol.Reason.BAD_PARAMETER, "x")]
        ).to_dict()
        with self.assertRaises(ValueError):
            protocol.CommandResultMessage("r1", dict(good, status="no_such_status"))
        with self.assertRaises(ValueError):
            protocol.CommandResultMessage("r1", dict(good, reasons=[{"code": ""}]))
        with self.assertRaises((TypeError, ValueError)):
            protocol.CommandResultMessage("r1", dict(good, payload=[]))
        with self.assertRaises(ValueError):
            protocol.CommandResultMessage("r1", dict(good, extra=1))
        # 原因码原样过线，界面据此决定「重发有没有用」。
        restored = protocol.CommandResultMessage("r1", good)
        self.assertEqual(restored.result["status"], protocol.STATUS_REJECTED)
        self.assertEqual(
            restored.result["reasons"][0]["code"], protocol.Reason.BAD_PARAMETER
        )

    def test_full_roundtrip_carries_keys_and_versions(self):
        delta = protocol.ItemDelta(
            "b", 1, 2, 12, 13, [(self.stone, 2 ** 30, 0)], [self.dirt]
        )
        envelope = protocol.SyncEnvelope(delta, "s1", "net", "epoch-1")
        restored = protocol.SyncEnvelope.from_dict(
            envelope.to_dict(self.sender), self.receiver
        )
        self.assertEqual(restored.message.base_version, 12)
        self.assertEqual(restored.message.new_version, 13)
        self.assertEqual(restored.message.packet_count, 2)
        self.assertEqual(restored.message.upserts[0], (self.stone, 2 ** 30, 0))
        self.assertEqual(restored.message.removes, (self.dirt,))
        self.assertEqual(restored.epoch, "epoch-1")
        self.assertEqual(restored.direction(), "s2c")
        self.assertTrue(restored.message.is_last(), "1 是 2 个包里的最后一包")

    def test_a_delta_referencing_an_unknown_serial_asks_for_resync(self):
        """客户端解不出引用时必须走重同步，而不是猜一把键。

        复现的是真实断档：服务端在某条**客户端没收到**的消息里首次
        登记了这把键，于是之后的引用只剩序号、不带描述，客户端这时
        才发现自己对不上。
        """
        self.sender.encode(self.stone)  # 登记过了，后续引用只带序号
        payload = self.envelope_of(
            protocol.ItemDelta("b", 0, 1, 1, 2, [(self.stone, 5, 0)])
        ).to_dict(self.sender)
        self.assertNotIn("description", payload["message"]["upserts"][0]["resource_ref"])
        with self.assertRaises(ResourceResyncRequired):
            protocol.SyncEnvelope.from_dict(payload, self.receiver)


class RecoveryTests(unittest.TestCase):
    def test_missing_resource_is_preserved_and_can_recover(self):
        key = ResourceKey.item("missing:item", user_data={"v": [1]})
        original = wire(ContainerEntry(key, 2 ** 30).to_dict())
        entry = RestoredResourceEntry(original, lambda key: False)
        self.assertFalse(entry.available)
        with self.assertRaises(UnavailableResourceError):
            entry.require_key()
        with self.assertRaises(UnavailableResourceError):
            unused = entry.count
        with self.assertRaises(TypeError):
            ContainerEntry(entry, 1)
        original["count"] = 0
        entry.to_dict()["resource_key"]["identity"]["user_data"]["v"].append(2)
        recovered = entry.retry(lambda key: True)
        self.assertEqual(recovered.require_key(), key)
        self.assertEqual(recovered.count, 2 ** 30)
        self.assertEqual(wire(recovered.to_dict()), entry.to_dict())

    def test_unknown_version_and_malformed_entries_stay_opaque(self):
        key = ResourceKey.item("test:item").to_dict()
        key["schema_version"] = 99
        key["future"] = [1, {"x": True}]
        for payload in ({"resource_key": key, "count": 42},
                        {"resource_key": {}, "count": -1},
                        {"resource_key": key, "count": "unknown", "future": 1}):
            entry = RestoredResourceEntry(wire(payload), lambda key: True)
            self.assertFalse(entry.available)
            self.assertEqual(wire(entry.to_dict()), payload)
            self.assertTrue(entry.error)

    def test_registry_failure_preserves_record(self):
        payload = ContainerEntry(ResourceKey.fluid("test:fluid"), 25).to_dict()
        def unavailable(key):
            raise IOError("Registry offline")
        entry = RestoredResourceEntry(payload, unavailable)
        self.assertFalse(entry.available)
        self.assertEqual(entry.to_dict(), payload)


class EngineUserDataBoxingTests(unittest.TestCase):
    """自有样板载荷读回后带 ModSDK NBT 类型标签，按业务协议解包。

    2026-09-25 游戏内实测的形状，逐条钉在这里。少了这一层，
    样板解码会得到「格式不对」。
    **身份比较过去刻意保留原始 NBT 表示，那条取舍已于 2026-10-02 反转**
    ——见 `EngineUserDataIdentityTests` 与 `normalize_user_data` 的说明。
    离线测试此前照不到它：`FakeContainer` 原样存取，而真引擎会包。
    """

    def box(self, tag, value):
        return {"__type__": tag, "__value__": value}

    def test_scalars_are_unboxed(self):
        self.assertEqual(unbox_engine_value(self.box(3, 7)), 7)
        self.assertEqual(unbox_engine_value(self.box(3, -3)), -3)
        self.assertEqual(unbox_engine_value(self.box(3, 0)), 0)
        self.assertEqual(unbox_engine_value(self.box(5, 1.5)), 1.5)
        self.assertEqual(unbox_engine_value(self.box(8, "hi")), "hi")

    def test_the_byte_tag_carries_both_booleans_and_none(self):
        """实测：`True`→1、`False`→0、`None`→2，**共用同一个标签**。
        因此解包要看值，不能只看标签。"""
        self.assertIs(unbox_engine_value(self.box(1, 1)), True)
        self.assertIs(unbox_engine_value(self.box(1, 0)), False)
        self.assertIsNone(unbox_engine_value(self.box(1, 2)))

    def test_containers_are_walked(self):
        boxed = {
            "list": [self.box(3, 1), self.box(8, "a")],
            "nested": {"x": self.box(3, 5)},
            "empty_list": [],
            "empty_dict": {},
        }
        self.assertEqual(
            unbox_engine_value(boxed),
            {"list": [1, "a"], "nested": {"x": 5}, "empty_list": [], "empty_dict": {}},
        )

    def test_plain_values_pass_through(self):
        for value in (7, "x", None, True, [], {}, [1, 2]):
            self.assertEqual(unbox_engine_value(value), value)

    def test_a_dict_that_merely_looks_like_a_tag_is_not_unboxed(self):
        """外层 `__type__` 为字典时按普通字典遍历。
        这里只验证判别条件，不断言 SDK 会把同形业务字典再次包装。"""
        boxed = {
            "__type__": self.box(3, 3),
            "__value__": self.box(3, 7),
        }
        self.assertEqual(
            unbox_engine_value(boxed), {"__type__": 3, "__value__": 7}
        )

    def test_an_unknown_tag_is_left_alone(self):
        """未知标签的包装原样返回，由调用方校验；不据此推断引擎变更。"""
        odd = self.box(99, "?")
        self.assertEqual(unbox_engine_value(odd), odd)

    def test_a_boolean_is_not_mistaken_for_a_tag(self):
        """`True` 是 `int` 的子类，但它不是类型标签——不判这一条的话
        `{"__type__": True, "__value__": ...}` 会被当成 Byte 解掉。"""
        odd = {"__type__": True, "__value__": "x"}
        self.assertEqual(unbox_engine_value(odd), odd)


class EngineUserDataIdentityTests(unittest.TestCase):
    """**写进去的键必须等于读回来的键**——2026-10-02 实测缺陷的回归位。

    引擎对 `userData` 的往返是**变形**的（两条都来自实测，见
    `plans/technical_notes/resource_identity.md` §2）：

    1. 叶子标量被包成 `{"__type__": T, "__value__": V}`；
    2. 顶层 `customTips` 被**复制**一份进 `userData["ItemCustomTips"]`。

    过去身份刻意不解包（「解包会丢 NBT 类型信息」），于是「构造的键」与
    「引擎读回的键」恒不相等。2026-10-02 在样板终端上实测到的后果是
    **一次复制**：`slot_insert` 写完按资源键回读核对、判 `moved=0`，
    而东西已经写进那一格，调用方因此不扣来源格。带 `userData` 的样板
    是第一批撞上它的物品。

    **夹具必须自己造「引擎那份形状」**：喂 `ResourceKey.raw_identity`
    进去永远绿——离线看到的是裸的，线上看到的是包的。
    """

    def boxed(self, value):
        # type: (object) -> object
        """裸值 → 引擎读回来的形状（叶子标量带 NBT 标签，容器递归）。"""
        if isinstance(value, dict):
            return dict((key, self.boxed(item)) for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return [self.boxed(item) for item in value]
        if isinstance(value, bool):
            return {"__type__": 1, "__value__": 1 if value else 0}
        if value is None:
            return {"__type__": 1, "__value__": 2}
        if isinstance(value, int):
            return {"__type__": 3, "__value__": value}
        if isinstance(value, float):
            return {"__type__": 5, "__value__": value}
        return {"__type__": 8, "__value__": value}

    def engine_readback(self, key, count=1):
        # type: (object, int) -> dict
        """`key` 写进引擎、再读回来时的那份字典（两条变形都照实测做）。"""
        data = item_dict_from_key(key, count)
        payload = data.get("userData")
        tips = data.get("customTips")
        if isinstance(payload, dict) and payload:
            payload = self.boxed(payload)
            if tips:
                payload["ItemCustomTips"] = {"__type__": 8, "__value__": tips}
            data["userData"] = payload
        data["customTips"] = tips or ""
        return data

    def pattern_key(self):
        # type: () -> ResourceKey
        """一件**带 `userData` 载荷**的工厂样板（11-05 编出来的那种）。

        载荷里的字符串全部从这一份键自己派生，因此不引入
        「unicode 还是字节串」这一层无关变量。
        """
        identity = {"custom_tips": None, "durability": None,
                    "enchant_data": None, "extra_id": None,
                    "mod_enchant_data": None, "user_data": None}
        inner = {"aux": 0, "kind": "item", "name": "minecraft:oak_planks",
                 "schema_version": 1, "identity": identity}
        return ResourceKey.item(
            "endernet:factorical_template", 0,
            custom_tips="ordered 1x planks -> 1x button",
            user_data={"endernet:pattern": {
                "format": 1, "mode": "crafting", "returns": [],
                "inputs": [{"count": 1, "slot": 0, "key": inner}],
                "outputs": [{"count": 1, "key": inner}],
            }},
        )

    def test_the_engine_readback_has_the_same_identity(self):
        key = self.pattern_key()
        readback = self.engine_readback(key)
        self.assertEqual(
            item_key_from_dict(normalize_engine_item(readback)), key,
            "写进去的键与读回来的键必须是同一个身份",
        )

    def test_the_case_bites_without_the_normalizer(self):
        """**这条用例真的能红**：不走归一化时两个键不相等。

        钉住这一点，是因为身份归一一旦被绕过（或在别处又算一份），
        症状会以「转移层误报」的形式出现在很远的地方。
        """
        key = self.pattern_key()
        self.assertNotEqual(item_key_from_dict(self.engine_readback(key)), key)

    def test_the_engine_copy_of_custom_tips_is_dropped(self):
        key = self.pattern_key()
        normalized = normalize_engine_item(self.engine_readback(key))
        self.assertNotIn("ItemCustomTips", normalized["userData"])
        self.assertEqual(normalized["userData"], key.raw_identity["user_data"])

    def test_a_custom_tips_copy_that_differs_is_kept(self):
        """只丢「引擎复制的那一份」：与顶层 `customTips` 不同的副本留下。

        否则一个第三方模组真把 `ItemCustomTips` 当自己的载荷写，会被
        我们静默抹掉——那正是 `_check_fields` 那类严格性要防的事。
        """
        key = self.pattern_key()
        readback = self.engine_readback(key)
        readback["userData"]["ItemCustomTips"] = {"__type__": 8, "__value__": "另一份"}
        normalized = normalize_engine_item(readback)
        self.assertEqual(normalized["userData"]["ItemCustomTips"], "另一份")

    def test_an_empty_user_data_is_the_same_as_none(self):
        """引擎对空 `userData` 直接回 `None`；空字典必须归一到同一个身份。"""
        data = {"newItemName": "minecraft:stone", "newAuxValue": 0,
                "count": 1, "showInHand": True, "enchantData": [],
                "modEnchantData": [], "customTips": "", "extraId": "",
                "userData": {"ItemCustomTips": {"__type__": 8, "__value__": ""}},
                "durability": 0}
        self.assertIsNone(normalize_engine_item(data)["userData"])

    def test_a_plain_payload_passes_through_unchanged(self):
        """解包对**已经是裸的**载荷是恒等的，构造侧因此不需要第二份形状。"""
        key = self.pattern_key()
        normalized = normalize_engine_item(self.engine_readback(key))
        self.assertEqual(normalized["userData"],
                         item_key_from_dict(normalized).raw_identity["user_data"])


class DropItemShapeTests(unittest.TestCase):
    """`CreateEngineItemEntity` 要的物品字典形状（2026-10-02 真机逐字段二分）。

    三条都不是风格问题，**给错就一件也掉不出来**：引擎抛 `TypeError`，
    而 `TemplateStore._drop_real_slots` 把它接成「一件也没掉」——症状是
    拆了方块、格子里的东西不掉出来，日志干净、东西留在孤儿索引里。
    """

    def test_old_style_keys_are_used(self):
        data = item_dict_for_drop(ResourceKey.item("minecraft:stone"), 3)
        self.assertEqual("minecraft:stone", data["itemName"])
        self.assertEqual(0, data["auxValue"])
        self.assertEqual(3, data["count"])
        self.assertNotIn("newItemName", data)
        self.assertNotIn("newAuxValue", data)

    def test_the_three_fields_the_engine_refuses_none_are_filled(self):
        """`customTips` / `extraId` 要字符串、`durability` 要整数。

        真机逐字段二分：给 `None` 直接抛（前两个报
        「expected string or Unicode object」，最后一个报
        「an integer is required」）。
        """
        data = item_dict_for_drop(ResourceKey.item("minecraft:stone"), 1)
        self.assertEqual("", data["customTips"])
        self.assertEqual("", data["extraId"])
        self.assertEqual(0, data["durability"])
        # 其余身份字段给 `None` 也能生成实体，因此**不补**：补了反而多一处
        # 与读回归一不一致的地方。
        for engine in ("enchantData", "modEnchantData", "userData"):
            self.assertIsNone(data[engine], engine)

    def test_a_payload_survives_and_the_identity_round_trips(self):
        """掉落的字典里 `userData` 原样带着；捡回来仍是同一个身份。"""
        key = ResourceKey.item("endernet:factorical_template", 0,
                               custom_tips="配置", user_data={"endernet:pattern": {"format": 1}})
        data = item_dict_for_drop(key, 1)
        self.assertEqual(key.raw_identity["user_data"], data["userData"])
        self.assertEqual(key.raw_identity["custom_tips"], data["customTips"])

    def test_the_empty_writings_are_what_the_reader_normalizes_back(self):
        """补进去的 `""` / `0` 正是 `_EMPTY_IDENTITY` 归一回 `None` 的那两个写法。

        两边必须成对：改了一边，掉落物捡回来后身份就与放进去时不同。
        """
        data = item_dict_for_drop(ResourceKey.item("minecraft:stone"), 1)
        data["newItemName"] = data.pop("itemName")
        data["newAuxValue"] = data.pop("auxValue")
        self.assertEqual(ResourceKey.item("minecraft:stone"),
                         item_key_from_dict(normalize_engine_item(data)))


def craft_slots():
    return [{"slot": slot, "canonical": "", "count": 0, "name": "", "aux": 0}
            for slot in range(protocol.CRAFT_SLOT_COUNT)]


def template_rows(count, locked=False):
    """样板状态的一行（`locked` 每行都带，因此这里也要有）。"""
    return [{"slot": slot, "canonical": "", "count": 0, "name": "", "aux": 0,
             "locked": locked}
            for slot in range(count)]


def template_message(revision=5, mode="crafting"):
    return protocol.TemplateStateMessage(
        revision,
        mode,
        template_rows(protocol.TEMPLATE_SOURCE_CELLS),
        template_rows(protocol.TEMPLATE_OUTPUT_CELLS, locked=mode == "crafting"),
        template_rows(protocol.TEMPLATE_REAL_SLOT_ROWS),
        template_rows(protocol.TEMPLATE_REAL_SLOT_ROWS),
        {"max_count": 65535, "default_count": 1},
    )


class CraftStateTests(unittest.TestCase):
    """11-04 的工作台状态消息与命令上的 `recipe` 字段。

    这一条消息**不能复用 `InventorySnapshot`**（那个钉 36 格），因此它的
    边界检查要自己钉一遍：格数、槽号连续、空位写法、预览的形状。
    """

    def test_it_pins_nine_slots_not_thirty_six(self):
        with self.assertRaises(ValueError):
            protocol.CraftStateMessage(1, craft_slots()[:8])
        with self.assertRaises(ValueError):
            protocol.CraftStateMessage(1, craft_slots() + [
                {"slot": 9, "canonical": "", "count": 0, "name": "", "aux": 0}])
        self.assertEqual(9, protocol.CRAFT_SLOT_COUNT)

    def test_the_slot_indices_must_be_in_order(self):
        rows = craft_slots()
        rows[3] = {"slot": 7, "canonical": "", "count": 0, "name": "", "aux": 0}
        with self.assertRaises(ValueError):
            protocol.CraftStateMessage(1, rows)

    def test_an_empty_slot_is_an_empty_canonical(self):
        rows = craft_slots()
        rows[0] = {"slot": 0, "canonical": "item|minecraft:oak_planks|0|...",
                   "count": 3, "name": "minecraft:oak_planks", "aux": 0}
        message = protocol.CraftStateMessage(2, rows)
        self.assertEqual(3, message.slots[0]["count"])
        self.assertEqual("", message.slots[1]["canonical"])

    def test_a_bad_count_is_refused(self):
        for bad in (True, -1, "3"):
            rows = craft_slots()
            rows[0] = {"slot": 0, "canonical": "", "count": bad,
                       "name": "", "aux": 0}
            with self.assertRaises((ValueError, TypeError)):
                protocol.CraftStateMessage(1, rows)

    def test_the_recipe_note_must_be_text(self):
        with self.assertRaises(TypeError):
            protocol.CraftStateMessage(1, craft_slots(), recipe=None)

    def test_a_preview_carries_name_aux_and_count(self):
        message = protocol.CraftStateMessage(
            1, craft_slots(), "deadbeef", {"name": "minecraft:stick", "aux": 0, "count": 4})
        self.assertEqual(4, message.preview["count"])
        with self.assertRaises((ValueError, TypeError)):
            protocol.CraftStateMessage(
                1, craft_slots(), "", {"name": "minecraft:stick", "count": 4})

    def test_no_preview_is_none_and_survives_the_round_trip(self):
        message = protocol.CraftStateMessage(3, craft_slots(), "cafebabe", None)
        envelope = protocol.SyncEnvelope(message, "s1", "net", "epoch-1")
        restored = protocol.SyncEnvelope.from_dict(envelope.to_dict(), None)
        self.assertIsNone(restored.message.preview)
        self.assertEqual("cafebabe", restored.message.recipe)

    def test_the_envelope_needs_a_session_like_every_other_push(self):
        # 它是 S2C 消息，路由字段一个都不能少。
        with self.assertRaises((ValueError, TypeError)):
            protocol.SyncEnvelope(
                protocol.CraftStateMessage(1, craft_slots()), "", "net", "epoch-1")

    def test_unknown_fields_are_refused_outright(self):
        payload = protocol.CraftStateMessage(1, craft_slots()).to_dict()
        payload["extra"] = 1
        with self.assertRaises((ValueError, TypeError)):
            protocol.decode_sync_message(payload)


class TerminalCommandRecipeField(unittest.TestCase):
    """`recipe` 是 11-04 加到命令上的**唯一**新字段。

    加它的代价要说清楚：`TerminalCommand.FIELDS` 是**无条件严格**的
    （缺字段与多字段都拒），所以老客户端发的命令会被整个拒掉，
    而不是「少一个可选项」。客户端与服务端同批发布，因此这里要钉的是
    **往返不丢**，而不是兼容旧版。
    """

    def command(self, recipe=None):
        return protocol.TerminalCommand(
            "r1", "craft_import", None, {"kind": "item", "name": "minecraft:stick"},
            1, None, None, None, None, None, recipe,
        )

    def test_the_field_is_on_the_declared_list(self):
        self.assertIn("recipe", protocol.TerminalCommand.FIELDS)

    def test_it_survives_the_round_trip(self):
        envelope = protocol.SyncEnvelope(self.command("deadbeef"), "s1")
        restored = protocol.SyncEnvelope.from_dict(envelope.to_dict(), None)
        self.assertEqual("deadbeef", restored.message.recipe)

    def test_absent_is_none_and_still_serialised(self):
        envelope = protocol.SyncEnvelope(self.command(), "s1")
        payload = envelope.to_dict()
        self.assertIn("recipe", payload["message"])
        self.assertIsNone(payload["message"]["recipe"])
        restored = protocol.SyncEnvelope.from_dict(payload, None)
        self.assertIsNone(restored.message.recipe)

    def test_a_missing_field_is_refused_not_defaulted(self):
        # 严格是**有意的**：悄悄补一个缺席字段会让「客户端发的是新协议还是
        # 旧协议」在服务端看不出来。
        payload = protocol.SyncEnvelope(self.command(), "s1").to_dict()
        del payload["message"]["recipe"]
        with self.assertRaises((ValueError, TypeError)):
            protocol.SyncEnvelope.from_dict(payload, None)

    def test_a_non_text_recipe_is_refused(self):
        payload = protocol.SyncEnvelope(self.command(), "s1").to_dict()
        payload["message"]["recipe"] = 7
        with self.assertRaises((ValueError, TypeError)):
            protocol.SyncEnvelope.from_dict(payload, None)

    def test_it_reaches_the_service_payload(self):
        self.assertEqual("deadbeef", self.command("deadbeef").payload()["recipe"])
        self.assertNotIn("recipe", self.command().payload())


class TerminalCommandCellField(unittest.TestCase):
    """`cell` 是 11-05 加到命令上的**唯一**新字段（样板终端的虚格下标）。

    **与 `slot` 分开是刻意的**：交接明令「不将虚格下标当作玩家或设备真实槽号」，
    两个下标空间共用一个字段会让「这个数属于哪个空间」逐动作去猜。
    """

    def command(self, cell=None):
        return protocol.TerminalCommand(
            "r1", "template_cell_put", None,
            {"kind": "item", "name": "minecraft:stick"},
            1, None, None, None, None, None, None, cell,
        )

    def test_the_field_is_on_the_declared_list(self):
        self.assertIn("cell", protocol.TerminalCommand.FIELDS)
        self.assertIn("cell", protocol.TerminalCommand.__slots__)

    def test_it_survives_the_round_trip(self):
        envelope = protocol.SyncEnvelope(self.command(4), "s1")
        restored = protocol.SyncEnvelope.from_dict(envelope.to_dict(), None)
        self.assertEqual(4, restored.message.cell)

    def test_absent_is_none_and_still_serialised(self):
        payload = protocol.SyncEnvelope(self.command(), "s1").to_dict()
        self.assertIn("cell", payload["message"])
        self.assertIsNone(payload["message"]["cell"])
        restored = protocol.SyncEnvelope.from_dict(payload, None)
        self.assertIsNone(restored.message.cell)

    def test_a_missing_field_is_refused_not_defaulted(self):
        payload = protocol.SyncEnvelope(self.command(), "s1").to_dict()
        del payload["message"]["cell"]
        with self.assertRaises((ValueError, TypeError)):
            protocol.SyncEnvelope.from_dict(payload, None)

    def test_a_non_count_cell_is_refused(self):
        for bad in (True, -1, "1"):
            payload = protocol.SyncEnvelope(self.command(1), "s1").to_dict()
            payload["message"]["cell"] = bad
            with self.assertRaises((ValueError, TypeError)):
                protocol.SyncEnvelope.from_dict(payload, None)

    def test_it_reaches_the_service_payload(self):
        self.assertEqual(4, self.command(4).payload()["cell"])
        self.assertNotIn("cell", self.command().payload())

    def test_the_cell_never_lands_in_the_slot_field(self):
        """两个下标空间必须各走各的字段——串了就是搬错格子。"""
        payload = self.command(4).payload()
        self.assertEqual(4, payload["cell"])
        self.assertNotIn("slot", payload)


class TemplateStateTests(unittest.TestCase):
    """11-05 的样板终端状态消息。

    四组行同形，因此**边界检查只有一份**（与 `CraftStateMessage` 共用
    `_require_slot_row`）：格数、槽号连续、空位写法、`locked` 的类型、
    `limits` 的形状。
    """

    def test_it_pins_the_four_groups_to_their_own_sizes(self):
        self.assertEqual(9, protocol.TEMPLATE_SOURCE_CELLS)
        self.assertEqual(3, protocol.TEMPLATE_OUTPUT_CELLS)
        self.assertEqual(2, protocol.TEMPLATE_REAL_SLOTS)
        self.assertEqual(1, protocol.TEMPLATE_REAL_SLOT_ROWS)
        with self.assertRaises(ValueError):
            protocol.TemplateStateMessage(
                1, "crafting",
                template_rows(protocol.TEMPLATE_SOURCE_CELLS - 1),
                template_rows(protocol.TEMPLATE_OUTPUT_CELLS),
                template_rows(protocol.TEMPLATE_REAL_SLOT_ROWS),
                template_rows(protocol.TEMPLATE_REAL_SLOT_ROWS),
                {"max_count": 64, "default_count": 1})

    def test_the_slot_indices_must_be_in_order(self):
        rows = template_rows(protocol.TEMPLATE_SOURCE_CELLS)
        rows[3]["slot"] = 7
        with self.assertRaises(ValueError):
            protocol.TemplateStateMessage(
                1, "crafting", rows,
                template_rows(protocol.TEMPLATE_OUTPUT_CELLS),
                template_rows(protocol.TEMPLATE_REAL_SLOT_ROWS),
                template_rows(protocol.TEMPLATE_REAL_SLOT_ROWS),
                {"max_count": 64, "default_count": 1})

    def test_a_key_without_a_name_is_refused(self):
        rows = template_rows(protocol.TEMPLATE_SOURCE_CELLS)
        rows[0] = {"slot": 0, "canonical": "item|minecraft:stick|0|...",
                   "count": 1, "name": "", "aux": 0, "locked": False}
        with self.assertRaises(ValueError):
            protocol.TemplateStateMessage(
                1, "crafting", rows,
                template_rows(protocol.TEMPLATE_OUTPUT_CELLS),
                template_rows(protocol.TEMPLATE_REAL_SLOT_ROWS),
                template_rows(protocol.TEMPLATE_REAL_SLOT_ROWS),
                {"max_count": 64, "default_count": 1})

    def test_locked_must_be_a_bool_not_a_number(self):
        for bad in (0, 1, None, "true"):
            rows = template_rows(protocol.TEMPLATE_OUTPUT_CELLS)
            rows[0]["locked"] = bad
            with self.assertRaises((ValueError, TypeError)):
                protocol.TemplateStateMessage(
                    1, "crafting",
                    template_rows(protocol.TEMPLATE_SOURCE_CELLS), rows,
                    template_rows(protocol.TEMPLATE_REAL_SLOT_ROWS),
                    template_rows(protocol.TEMPLATE_REAL_SLOT_ROWS),
                    {"max_count": 64, "default_count": 1})

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            template_message(mode="smelting")

    def test_the_limits_must_be_sane(self):
        for limits in ({"max_count": 64}, {"max_count": 64, "default_count": 1,
                                           "extra": 1},
                       {"max_count": 0, "default_count": 1},
                       {"max_count": 4, "default_count": 9},
                       {"max_count": True, "default_count": 1}):
            with self.assertRaises((ValueError, TypeError)):
                protocol.TemplateStateMessage(
                    1, "crafting",
                    template_rows(protocol.TEMPLATE_SOURCE_CELLS),
                    template_rows(protocol.TEMPLATE_OUTPUT_CELLS),
                    template_rows(protocol.TEMPLATE_REAL_SLOT_ROWS),
                    template_rows(protocol.TEMPLATE_REAL_SLOT_ROWS), limits)

    def test_it_survives_the_round_trip_whole(self):
        envelope = protocol.SyncEnvelope(
            template_message(7, "factory"), "s1", "net", "epoch-1")
        restored = protocol.SyncEnvelope.from_dict(envelope.to_dict(), None)
        message = restored.message
        self.assertEqual(7, message.revision)
        self.assertEqual("factory", message.mode)
        self.assertEqual(protocol.TEMPLATE_SOURCE_CELLS, len(message.source))
        self.assertEqual(protocol.TEMPLATE_OUTPUT_CELLS, len(message.outputs))
        self.assertEqual(protocol.TEMPLATE_REAL_SLOT_ROWS, len(message.result))
        self.assertEqual(65535, message.limits["max_count"])

    def test_unknown_fields_are_refused_outright(self):
        payload = template_message().to_dict()
        payload["extra"] = 1
        with self.assertRaises((ValueError, TypeError)):
            protocol.decode_sync_message(payload)

    def test_it_needs_a_session_like_every_other_push(self):
        with self.assertRaises((ValueError, TypeError)):
            protocol.SyncEnvelope(template_message(), "", "net", "epoch-1")


if __name__ == "__main__":
    unittest.main()
