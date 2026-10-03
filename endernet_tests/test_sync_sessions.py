# coding=utf-8
"""终端会话的身份、时效与注销（11-01）。

这些用例测的都是**服务端自己算出来的结论**：玩家是谁、离得多远、终端还是
不是那一台。客户端能影响的只有「它请求了什么」，不能影响「它凭什么」——
凡是有客户端申报味儿的字段（消息体里的 player、客户端给的坐标）都不在这
条路径上，所以这里造 `SessionContext` 时一律当它是**已解析的外部事实**。
"""
import unittest

import testsupport  # noqa: F401
from endernet.common.identity import DeviceRef, NodeKey
from endernet.common.protocol import (
    CommandSource,
    SyncEnvelope,
    Reason,
    VIEW_ITEM,
    VIEW_ORDER,
    SessionOpen,
)
from endernet.common.resource_key import ResourceKey
from endernet.common.resource_sync import ResourceResyncRequired
from endernet.server.sync.sessions import (
    SESSION_ACTIVE,
    SESSION_CLOSED,
    SESSION_SNAPSHOTTING,
    SessionContext,
    SessionPolicy,
    SessionRegistry,
    within_reach,
)

STONE = ResourceKey.item("minecraft:stone")
DIRT = ResourceKey.item("minecraft:dirt")


def ref(x=0, y=64, z=0, generation=1, dimension=0):
    # type: (...) -> DeviceRef
    return DeviceRef(NodeKey(dimension, x, y, z), generation)


class Fixture(unittest.TestCase):
    """每个用例一套独立的注册表，避免用例之间靠执行顺序传递状态。"""

    network = "net-a"
    epoch = "net-a#1"
    master = "master-1"
    player = "player-7"

    def setUp(self):
        self.policy = SessionPolicy(max_interaction_distance=8.0)
        self.registry = SessionRegistry(self.policy)
        self.device = ref()
        self.frame = 100

    def context(self, **overrides):
        facts = {
            "player_id": self.player,
            "record_ref": self.device,
            "network_id": self.network,
            "epoch": self.epoch,
            "master_instance_id": self.master,
            "network_workable": True,
            "player_dimension": self.device.node_key.dimension,
            "player_position": (0.0, 64.0, 0.0),
        }
        facts.update(overrides)
        return SessionContext(**facts)

    def envelope(self, view_kind=VIEW_ITEM, device=None, dimension=None):
        """客户端发来的那条 `session_open`，外面裹成信封。

        `view_kind` 在信封上而不是消息体里——路由元数据与业务字段分开，
        `SessionOpen` 因此连「我是哪个视图」都不必知道。
        """
        device = self.device if device is None else device
        message = SessionOpen(
            "req-1",
            device,
            device.node_key.dimension if dimension is None else dimension,
        )
        return SyncEnvelope(message, view_kind=view_kind)

    def attempt(self, envelope=None, context=None, frame=None, **query):
        """跑一次 `open()`，返回 `(session, result)`。"""
        return self.registry.open(
            self.envelope(**query) if envelope is None else envelope,
            self.context() if context is None else context,
            self.frame if frame is None else frame,
        )

    def open(self, **query):
        """打开一个会话，返回 Session（失败即断言失败）。"""
        session, result = self.attempt(**query)
        self.assertTrue(result.ok, result.detail)
        return session

    def open_at(self, device, **query):
        """在另一台方块上开会话。

        请求里的 `device_ref` 与上下文里的 `record_ref` 必须指同一台——
        它们一个是客户端说的、一个是服务端查到的，指着两台不同的方块时
        本就不该通过（`test_generation_change_invalidates_the_block`）。
        """
        query.setdefault(
            "context",
            self.context(record_ref=device, player_position=device.node_key.as_tuple()),
        )
        return self.open(device=device, **query)

    def refusal(self, **query):
        """尝试打开并断言被拒，返回 `SessionCheckResult`。"""
        session, result = self.attempt(**query)
        self.assertIsNone(session, "被拒的请求不应产生会话")
        self.assertFalse(result.ok)
        return result


class SessionOpenTests(Fixture):
    def test_open_registers_a_live_session_carrying_the_resolved_facts(self):
        session = self.open()
        self.assertEqual(session.state, SESSION_ACTIVE)
        self.assertEqual(session.player_id, self.player)
        self.assertEqual(session.network_id, self.network)
        self.assertEqual(session.epoch, self.epoch)
        self.assertEqual(session.master_instance_id, self.master)
        self.assertEqual(session.view_kind, VIEW_ITEM)
        self.assertIs(self.registry.get(session.session_id), session)

    def test_identity_comes_from_the_resolved_context_not_the_message(self):
        """消息体里塞 `player_id` 会让整条消息被拒（未知字段），而不是被采用。

        身份只有一个来源：引擎附在 C2S 事件上的 `__id__`。协议层连这个字段
        都没定义，所以「忘了忽略它」这件事没有发生的机会。
        """
        payload = self.envelope().message.to_dict()
        payload["player_id"] = "somebody-else"
        with self.assertRaises(ValueError):
            SessionOpen.from_dict(payload)

    def test_unresolvable_identity_is_refused(self):
        result = self.refusal(context=self.context(player_id=""))
        self.assertEqual(result.reason, Reason.SESSION_INVALID)

    def test_unknown_view_kind_is_refused(self):
        raw = SyncEnvelope(SessionOpen("req-1", self.device, 0))
        raw.view_kind = "something_else"
        result = self.refusal(envelope=raw)
        # 未知种类连 `VIEW_KINDS` 都进不去，走的是「视图种类」那一支。
        self.assertEqual(result.reason, Reason.UNSUPPORTED)

    def test_order_view_opens_now_that_it_has_a_producer(self):
        """`order` 在 11-06 之前是「已知但没有产出方」，现在开得起来了。

        这条要钉的是**两件事合起来**：视图种类仍然只是「已知」的一个元素
        （不是随便什么字符串都能开），而它已经在产出方名单里——少了后者，
        工厂终端会在开会话这一步被拒，界面上表现成「右键没反应」。
        与 `test_unknown_view_kind_is_refused` 成对读：那条钉的是另一种
        「不支持」（发错了种类，协议错误）。
        """
        session = self.open(view_kind=VIEW_ORDER)
        self.assertEqual(session.view_kind, VIEW_ORDER)
        self.assertEqual(session.state, SESSION_ACTIVE)

    def test_dimension_mismatch_is_refused(self):
        result = self.refusal(dimension=1)
        self.assertEqual(result.reason, Reason.SESSION_INVALID)

    def test_player_in_another_dimension_than_the_block_is_refused(self):
        """玩家与终端不在同一维度——即使两边各自都对得上请求的维度。"""
        result = self.refusal(context=self.context(player_dimension=1))
        self.assertEqual(result.reason, Reason.SESSION_INVALID)

    def test_unresolvable_dimension_is_refused(self):
        result = self.refusal(context=self.context(player_dimension=None))
        self.assertEqual(result.reason, Reason.SESSION_INVALID)

    def test_generation_change_invalidates_the_block(self):
        """方块被拆掉又在原地放了一台：位置相同、代次不同，会话必须失效。"""
        replacement = ref(generation=2)
        result = self.refusal(context=self.context(record_ref=replacement))
        self.assertEqual(result.reason, Reason.STALE_INSTANCE)

    def test_missing_block_is_refused_not_treated_as_a_stale_one(self):
        result = self.refusal(context=self.context(record_ref=None))
        self.assertEqual(result.reason, Reason.DEVICE_GONE)

    def test_master_replacement_is_refused(self):
        result = self.refusal(context=self.context(master_instance_id=None))
        self.assertEqual(result.reason, Reason.MASTER_UNLOADED)

    def test_network_that_may_not_work_is_refused(self):
        result = self.refusal(context=self.context(network_workable=False))
        self.assertEqual(result.reason, Reason.WRONG_NETWORK)

    def test_block_outside_any_network_is_refused(self):
        result = self.refusal(context=self.context(network_id=None))
        self.assertEqual(result.reason, Reason.WRONG_NETWORK)


class DistanceTests(Fixture):
    """距离断言一律**对着 policy 字段**，不硬编码 8.0。

    这样调策略会改变行为，而用例仍然测的是「边界含端点」这条规则本身。
    """

    def setUp(self):
        Fixture.setUp(self)
        self.policy = SessionPolicy(max_interaction_distance=3.0)
        self.registry = SessionRegistry(self.policy)

    def at(self, x):
        return self.context(player_position=(x, 64.0, 0.0))

    def test_boundary_is_inclusive(self):
        limit = self.policy.max_interaction_distance
        self.assertTrue(self.registry.check(self.envelope(), self.at(limit)).ok)

    def test_just_past_the_boundary_is_refused(self):
        limit = self.policy.max_interaction_distance
        result = self.registry.check(self.envelope(), self.at(limit + 0.5))
        self.assertEqual(result.reason, Reason.OUT_OF_RANGE)

    def test_distance_is_measured_in_three_dimensions(self):
        """位移摊到三个轴上算的是平方和，不是最大的那一个。"""
        limit = self.policy.max_interaction_distance
        third = limit / 3.0
        inside = self.context(player_position=(third, 64.0 + third, third))
        self.assertTrue(self.registry.check(self.envelope(), inside).ok)
        outside = self.context(player_position=(limit, 64.0 + limit, limit))
        self.assertEqual(
            self.registry.check(self.envelope(), outside).reason, Reason.OUT_OF_RANGE
        )

    def test_vertical_offset_alone_can_exceed_the_range(self):
        limit = self.policy.max_interaction_distance
        result = self.registry.check(
            self.envelope(), self.context(player_position=(0.0, 64.0 + limit + 1.0, 0.0))
        )
        self.assertEqual(result.reason, Reason.OUT_OF_RANGE)

    def test_unknown_position_fails_closed(self):
        """查不到坐标时默认拒绝——放行等于把距离校验变成可选项。"""
        result = self.registry.check(self.envelope(), self.context(player_position=None))
        self.assertEqual(result.reason, Reason.OUT_OF_RANGE)

    def test_unknown_position_can_be_allowed_by_policy(self):
        self.policy.require_position = False
        self.assertTrue(
            self.registry.check(self.envelope(), self.context(player_position=None)).ok
        )

    def test_within_reach_reports_failure_when_both_sides_are_missing(self):
        self.assertFalse(within_reach(None, (0.0, 0.0, 0.0), self.policy))
        self.assertFalse(within_reach((0.0, 0.0, 0.0), None, self.policy))


class RevalidationTests(Fixture):
    """每请求复核（`revalidate`）：会话**开着**不等于一直成立。

    这一组的核心不是「复核会拒」，而是「复核拒的理由与开会话拒的理由
    是同一套」——判据一旦分成两份，迟早出现「开会话严、复核松」，而
    中间那条缝正好是「人走开了还在收库存」。
    """

    def test_a_still_valid_session_passes(self):
        session = self.open()
        self.assertTrue(self.registry.revalidate(session, self.context()).ok)

    def test_every_refusal_reason_matches_the_open_time_check(self):
        """同一份上下文，`check` 与 `revalidate` 必须给出**同一个**理由。

        逐条覆盖 `_check_target` 里的每一个分支。改判据时只改一处会让
        这条立刻红——这正是它存在的意义。
        """
        session = self.open()
        envelope = self.envelope()
        cases = [
            ("player_dimension=None", {"player_dimension": None}),
            ("player_dimension=1", {"player_dimension": 1}),
            ("player_position=None", {"player_position": None}),
            ("player_position=(99, 64, 0)", {"player_position": (99.0, 64.0, 0.0)}),
            ("record_ref=None", {"record_ref": None}),
            ("record_ref=2nd generation", {"record_ref": ref(generation=2)}),
            ("record_ref=other block", {"record_ref": ref(x=3)}),
            ("network_id=None", {"network_id": None}),
            ("master_instance_id=None", {"master_instance_id": None}),
            ("network_workable=False", {"network_workable": False}),
        ]
        for name, overrides in cases:
            context = self.context(**overrides)
            expected = self.registry.check(envelope, context)
            actual = self.registry.revalidate(session, context)
            self.assertFalse(expected.ok, "%s：这份上下文本就该校验不过" % (name,))
            self.assertEqual(
                actual.reason,
                expected.reason,
                "%s：复核与开会话给出了不同的理由" % (name,),
            )
            self.assertFalse(actual.ok, name)

    def test_an_unresolvable_context_is_refused_like_at_open_time(self):
        """连网络都查不到（`context is None`）时与开会话同一条：拒绝。

        `check()` 拿不到 `None`（开会话那条路径先挡过一轮），因此这一条
        只能单独钉——两条路径在这一点上必须**同向**，否则「设备刚被拆掉」
        这一瞬间复核会放行。
        """
        session = self.open()
        result = self.registry.revalidate(session, None)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, Reason.WRONG_NETWORK)

    def test_revalidation_reads_the_session_not_a_second_request(self):
        """复核比的是**会话存的那一份**，不是任何新来的说法。

        确认包里根本没有设备引用（客户端说的是「我收到第 3 批」），因此
        这里能比的只有会话自己——钉住这一点，将来给确认消息加字段时才不会
        顺手让它覆盖掉会话认准的那台设备。
        """
        session = self.open()
        # 会话认的是 self.device；上下文改成另一台方块 → 复核必须拒。
        elsewhere = ref(x=9)
        context = self.context(
            record_ref=elsewhere, player_position=elsewhere.node_key.as_tuple()
        )
        result = self.registry.revalidate(session, context)
        self.assertEqual(result.reason, Reason.STALE_INSTANCE)
        # 而把上下文换回原设备就通过——拒绝确实来自设备对不上，不是别的原因。
        self.assertTrue(self.registry.revalidate(session, self.context()).ok)

    def test_revalidation_is_a_read_only_probe(self):
        """复核**不动会话表、不动计数**：它只回答「还成立吗」。

        关会话是调用方的事（hub 在复核不过时才关）。复核自己顺手关掉的话，
        「查一下」这个动作就有了副作用，而调用方再也分不清「它拒了」与
        「它已经替我关了」。
        """
        session = self.open()
        self.registry.revalidate(session, self.context(player_dimension=None))
        self.assertIs(self.registry.get(session.session_id), session)
        self.assertTrue(session.is_open())


class SessionLifecycleTests(Fixture):
    def test_session_ids_are_not_reused_after_close(self):
        first = self.open()
        self.registry.close(first.session_id, "test")
        second = self.open()
        self.assertNotEqual(first.session_id, second.session_id)
        self.assertIsNone(self.registry.get(first.session_id))

    def test_closing_twice_is_harmless(self):
        session = self.open()
        self.assertIsNotNone(self.registry.close(session.session_id, "test"))
        self.assertIsNone(self.registry.close(session.session_id, "test"))

    def test_closed_session_is_no_longer_returned(self):
        session = self.open()
        self.registry.close(session.session_id, "test")
        self.assertEqual(session.state, SESSION_CLOSED)
        self.assertIsNone(self.registry.get(session.session_id))

    def test_snapshotting_session_is_still_a_subscriber(self):
        """快照在途**不影响它算订阅者**：增量照发，客户端收齐快照才应用。"""
        session = self.open()
        session.state = SESSION_SNAPSHOTTING
        self.assertIs(self.registry.get(session.session_id), session)
        self.assertEqual(self.registry.sessions_for_network(self.network), [session])

    def test_expiry_uses_the_policy_window(self):
        session = self.open()
        self.assertEqual(
            self.registry.evict_expired(self.frame + self.policy.session_ttl_frames), []
        )
        expired = self.registry.evict_expired(
            self.frame + self.policy.session_ttl_frames + 1
        )
        self.assertEqual([s.session_id for s in expired], [session.session_id])
        self.assertEqual(self.registry.count(), 0)

    def test_touch_pushes_the_deadline_out(self):
        session = self.open()
        session.touch(self.frame + self.policy.session_ttl_frames)
        self.assertEqual(
            self.registry.evict_expired(self.frame + self.policy.session_ttl_frames + 1), []
        )

    def test_expiry_can_be_disabled(self):
        self.policy.session_ttl_frames = 0
        self.open()
        self.assertEqual(self.registry.evict_expired(self.frame + 10 ** 6), [])

    def test_close_for_player_leaves_other_players_alone(self):
        mine = self.open()
        theirs = self.open(context=self.context(player_id="player-9"))
        closed = self.registry.close_for_player(self.player)
        self.assertEqual([s.session_id for s in closed], [mine.session_id])
        self.assertIs(self.registry.get(theirs.session_id), theirs)

    def test_close_for_device_leaves_other_blocks_alone(self):
        """**按位置比对**：同一台设备的会话关掉，别处的会话不受影响。"""
        here = self.open()
        elsewhere = self.open_at(ref(x=40))
        closed = self.registry.close_for_device(self.device)
        self.assertEqual([s.session_id for s in closed], [here.session_id])
        self.assertIs(self.registry.get(elsewhere.session_id), elsewhere)

    def test_close_for_device_matches_by_position_not_by_generation(self):
        """代次变了同样要关——界面上显示的已经不再是一台存在的方块。"""
        session = self.open()
        closed = self.registry.close_for_device(ref(generation=99))
        self.assertEqual([s.session_id for s in closed], [session.session_id])

    def test_close_for_device_ignores_a_different_position(self):
        session = self.open()
        self.assertEqual(self.registry.close_for_device(ref(x=1)), [])
        self.assertIs(self.registry.get(session.session_id), session)

    def test_close_for_epoch_drops_only_the_stale_generation(self):
        old = self.open()
        self.assertEqual(self.registry.close_for_epoch(self.network, self.epoch), [])
        closed = self.registry.close_for_epoch(self.network, "net-a#2")
        self.assertEqual([s.session_id for s in closed], [old.session_id])

    def test_close_for_epoch_is_scoped_to_one_network(self):
        """别的网络换了代次，不该动这个网络的会话。"""
        session = self.open()
        self.assertEqual(self.registry.close_for_epoch("net-b", "net-b#2"), [])
        self.assertIs(self.registry.get(session.session_id), session)

    def test_close_for_network_takes_every_view_of_it(self):
        first = self.open()
        second = self.open_at(ref(x=40))
        self.assertEqual(len(self.registry.sessions_for_network(self.network)), 2)
        closed = self.registry.close_for_network(self.network)
        self.assertEqual(
            sorted(s.session_id for s in closed),
            sorted([first.session_id, second.session_id]),
        )
        self.assertEqual(self.registry.sessions_for_network(self.network), [])

    def test_per_player_limit_evicts_the_oldest_not_the_newest(self):
        """重开界面本就该成功；卡住的是旧的那个会话。"""
        self.policy.max_sessions_per_player = 2
        self.registry = SessionRegistry(self.policy)
        first = self.open()
        second = self.open()
        third = self.open()
        self.assertIsNone(self.registry.get(first.session_id))
        self.assertIsNotNone(self.registry.get(second.session_id))
        self.assertIsNotNone(self.registry.get(third.session_id))
        self.assertEqual(self.registry.stats["evicted_by_limit"], 1)

    def test_per_network_limit_does_not_let_one_player_starve_the_network(self):
        self.policy.max_sessions_per_network = 1
        self.registry = SessionRegistry(self.policy)
        mine = self.open()
        theirs = self.open(context=self.context(player_id="player-9"))
        self.assertIsNone(self.registry.get(mine.session_id))
        self.assertIsNotNone(self.registry.get(theirs.session_id))

    def test_eviction_order_is_numeric_not_lexicographic(self):
        """「先开的先关」按数值序号判。

        `session_id` 是给人看的字符串，`"s10" < "s9"` 按字典序成立——真拿
        它排序，序号一旦过十就会把最新的那个当成最旧的关掉。
        """
        self.policy.max_sessions_per_player = 3
        self.registry = SessionRegistry(self.policy)
        opened = [self.open() for _ in range(10)]
        for session in opened[:8]:
            self.registry.close(session.session_id, "test")
        self.open()  # s11，此时只有 s9/s10 在世，还没到上限
        self.open()  # s12，满了，该关的是 s9（字典序会误判成 s10）
        self.assertIsNone(self.registry.get("s9"))
        self.assertIsNotNone(self.registry.get("s10"))
        self.assertIsNotNone(self.registry.get("s11"))

    def test_eviction_never_blocks_the_player_who_is_opening(self):
        self.policy.max_sessions_per_player = 1
        self.registry = SessionRegistry(self.policy)
        self.open()
        session = self.open()
        self.assertIsNotNone(self.registry.get(session.session_id))


class DescriptionsArePerSessionTests(Fixture):
    def test_serial_numbers_are_not_shared_between_sessions(self):
        """序号只在本会话内有效。两张表各发各的，客户端才不会拿错键。

        共享一张表看起来省带宽，代价是「谁先看到什么序号」变成会话间的
        耦合——一条会话重开就得连累另一条重新同步。
        """
        first = self.open()
        second = self.open()
        self.assertEqual(first.descriptions.encode(STONE)["serial"], 1)
        self.assertEqual(second.descriptions.encode(STONE)["serial"], 1)
        self.assertEqual(first.descriptions.encode(DIRT)["serial"], 2)

    def test_one_session_cannot_resolve_another_sessions_serials(self):
        first = self.open()
        second = self.open()
        first.descriptions.encode(STONE)
        with self.assertRaises(ResourceResyncRequired):
            second.descriptions.resolve(self.epoch, 1)

    def test_a_new_session_carries_the_networks_epoch(self):
        session = self.open()
        self.assertEqual(session.descriptions.epoch, self.epoch)
        self.assertEqual(session.descriptions.network_id, self.network)


class CommandAdmissionTests(Fixture):
    """`accepts()` 是 `MasterController.session_check` 的答案。"""

    def source(self, session_id="", player_id=None, device_ref=None):
        return CommandSource(
            self.player if player_id is None else player_id,
            self.device if device_ref is None else device_ref,
            0,
            session_id=session_id,
        )

    def test_a_command_without_a_session_is_still_allowed(self):
        """**05/08 的非回归要求**：既有调用方不带会话，不能被顺手拒掉。

        在 `accepts` 里对空会话返回 False，等于给所有既有命令凭空加了一道
        具名门禁——那不是 11-01 要改的东西。
        """
        self.assertTrue(self.registry.accepts(self.source()))

    def test_a_command_carrying_a_live_session_is_allowed(self):
        session = self.open()
        self.assertTrue(self.registry.accepts(self.source(session_id=session.session_id)))

    def test_a_command_carrying_a_closed_session_is_refused(self):
        session = self.open()
        self.registry.close(session.session_id, "test")
        self.assertFalse(self.registry.accepts(self.source(session_id=session.session_id)))

    def test_an_unknown_session_id_is_refused(self):
        self.assertFalse(self.registry.accepts(self.source(session_id="s999")))

    def test_a_session_belonging_to_another_player_is_refused(self):
        session = self.open()
        self.assertFalse(
            self.registry.accepts(
                self.source(session_id=session.session_id, player_id="player-9")
            )
        )

    def test_a_command_aimed_at_a_replaced_block_is_refused(self):
        session = self.open()
        self.assertFalse(
            self.registry.accepts(
                self.source(session_id=session.session_id, device_ref=ref(generation=2))
            )
        )

    def test_a_command_may_omit_the_device_ref_entirely(self):
        """命令不一定带方块——只带会话时按会话自己的设备算。"""
        session = self.open()
        source = CommandSource(self.player, None, 0, session_id=session.session_id)
        self.assertTrue(self.registry.accepts(source))


class DiagnosticsTests(Fixture):
    def test_diagnostics_counts_sessions_by_network(self):
        self.open()
        self.open_at(ref(x=40))
        report = self.registry.diagnostics()
        self.assertEqual(report["by_network"][self.network], 2)
        self.assertEqual(report["open"], 2)

    def test_diagnostics_survives_an_empty_registry(self):
        report = self.registry.diagnostics()
        self.assertEqual(report["open"], 0)
        self.assertEqual(report["by_network"], {})


if __name__ == "__main__":
    unittest.main()
