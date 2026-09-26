# coding=utf-8
"""资源身份、引擎转换、同步与存档恢复之间的回归。"""
import copy
import unittest

import testsupport  # noqa: F401
from endernet.common import protocol
from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.resource_key import ResourceKey, canonical_tree, tree_text
from endernet.common.item_codec import item_key, item_key_from_dict, item_dict_from_key, item_from_key, unbox_engine_value
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


class ItemCodecTests(unittest.TestCase):
    def test_real_tooldelta_class_without_engine_bootstrap(self):
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
        key = ResourceKey.item("test:item", aux=2, enchant_data=[],
                               mod_enchant_data=[("test:enchant", 3)], durability=None,
                               extra_id="unique", custom_tips=u"中文", user_data={"v": [1]})
        basic = module.BasicItemInfo()
        basic.maxStackSize = 16
        module.itemBasicInfoPool[(key.name, key.aux)] = basic
        item = item_from_key(key, 12, module.Item)
        self.assertEqual(item_key(item), key)
        self.assertEqual(item_key_from_dict(wire(item.marshal())), key)
        item.userData["v"].append(2)
        self.assertNotEqual(item_key(item), key)
        with self.assertRaises(ValueError):
            item_from_key(key, 17, module.Item)

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
            protocol.SessionReady(11, 12),
            protocol.SnapshotBegin("snap-1", 12, 3),
            protocol.SnapshotChunk("snap-1", 0, [(self.stone, 5, 1)]),
            protocol.SnapshotEnd("snap-1", 2),
            protocol.ItemDelta("batch-1", 0, 2, 12, 13, [(self.stone, 5, 1)], [self.dirt]),
            protocol.ResyncRequired("stale_version", "缺口已回收"),
            protocol.SyncError("r5", "stale_version", "期望版本过期"),
        )

    def test_every_registered_kind_is_covered_here(self):
        covered = set(message.kind for message in self.every_message())
        self.assertEqual(covered, set(protocol.sync_message_kinds()))

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

    def test_s2c_messages_require_session_network_and_epoch(self):
        """每个服务端→客户端包都必须能回答「这是谁的、哪一代的」。"""
        for message in self.every_message():
            if message.kind not in protocol.SERVER_TO_CLIENT_KINDS:
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
    """引擎读回来的 `userData` 是**带类型标签**的，必须先解包。

    2026-09-25 游戏内实测的形状，逐条钉在这里。少了这一层，
    任何读 `userData` 的功能都会得到「格式不对」——而写进去时明明是对的。
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
        """**这条防的是误伤。** 别的模组的数据里可能真有
        `{"__type__": 3, "__value__": 7}` 这种形状；引擎把它包过之后，
        外层的 `__type__` 是**字典**而不是整数，因此不该被当成标签解掉。"""
        boxed = {
            "__type__": self.box(3, 3),
            "__value__": self.box(3, 7),
        }
        self.assertEqual(
            unbox_engine_value(boxed), {"__type__": 3, "__value__": 7}
        )

    def test_an_unknown_tag_is_left_alone(self):
        """认不出的标签**原样返回**：那说明引擎换了表示法，
        把整棵树交回去让上层报「读不出来」，好过在这里猜一个值。"""
        odd = self.box(99, "?")
        self.assertEqual(unbox_engine_value(odd), odd)

    def test_a_boolean_is_not_mistaken_for_a_tag(self):
        """`True` 是 `int` 的子类，但它不是类型标签——不判这一条的话
        `{"__type__": True, "__value__": ...}` 会被当成 Byte 解掉。"""
        odd = {"__type__": True, "__value__": "x"}
        self.assertEqual(unbox_engine_value(odd), odd)


if __name__ == "__main__":
    unittest.main()
