# coding=utf-8
"""终端会话：身份、有效期、校验与注销（11-01）。

对应任务 11 的「会话绑定玩家、终端位置/代次、网络/主控实例」一句。
本模块是**纯领域**的：它不知道事件总线、不知道玩家怎么查坐标，只拿着一份
由同步层解析好的 `SessionContext` 做判定。这样它既能离线测试，也不会因为
「顺手查一下引擎」而让整个 `server/sync/` 失去脱机可测性。

**身份只有一个来源：引擎附在 C2S 事件上的 `__id__`。** 那条路径由
`server/adapters/messages.py` 的 `source_player_id()` 提供，本模块只接收
结论；协议层则连「消息体里写玩家」这个字段都没定义（见
`common/protocol.py` 的 `SessionOpen`）。

**会话与 `MasterController.session_check` 是两个东西。** 后者是命令准入时
的一次性探针（`callable(source) -> bool`），本模块提供 `accepts()` 去回答它；
判定依据仍是同一张会话表，但**无会话的命令必须继续放行**——05/08 的既有
调用方不带会话，把它们一并拒掉等于凭空加了一道门禁。
"""

from ...common.identity import DeviceRef  # noqa: F401 (仅用于 # type: 注释)
from ...common.protocol import (
    BlockReason,
    Reason,
    VIEW_KINDS,
    VIEW_KINDS_WITH_PRODUCER,
)
from ...common.resource_sync import ResourceDescriptions

#: 会话状态。`snapshotting` 表示快照还在途，**它仍然算订阅者**
#: （`terminal_item_index.md` §5），因此增量照发，只是客户端要收齐快照
#: 之后才能应用。
SESSION_ACTIVE = "active"
SESSION_SNAPSHOTTING = "snapshotting"
SESSION_CLOSED = "closed"

SESSION_STATES = (SESSION_ACTIVE, SESSION_SNAPSHOTTING, SESSION_CLOSED)


class SessionPolicy(object):
    """会话的边界参数。**逐项待定**，来源见各字段注释。

    测试一律**对着字段**断言（例如「恰好等于 `max_interaction_distance`
    时通过」），而不是硬编码 8.0 这类数值——这样调整策略会改变行为，
    而测试仍然有效。
    """

    __slots__ = (
        "max_interaction_distance",
        "max_interaction_distance_sq",
        "session_ttl_frames",
        "max_sessions_per_player",
        "max_sessions_per_network",
        "require_position",
    )

    def __init__(
        self,
        max_interaction_distance=8.0,
        session_ttl_frames=600,
        max_sessions_per_player=4,
        max_sessions_per_network=32,
        require_position=True,
    ):
        # 待定：11.md 页尾「请求大小、会话过期和分页上限由目标环境测量后确定」。
        self.max_interaction_distance = float(max_interaction_distance)
        # 预先平方，比较时不出现除法，也就不存在 py2 整除的坑。
        self.max_interaction_distance_sq = (
            self.max_interaction_distance * self.max_interaction_distance
        )
        # 待定：同上。
        self.session_ttl_frames = int(session_ttl_frames)
        # 待定：资源上限同样要测量后再定。
        self.max_sessions_per_player = int(max_sessions_per_player)
        self.max_sessions_per_network = int(max_sessions_per_network)
        #: 查不到玩家坐标时是否拒绝。**默认拒绝（fail closed）**：
        #: 距离校验存在的意义就是挡住远处的伪造请求，而「查不到坐标」
        #: 恰恰是它最该挡住的情形。放行等于把这道校验变成可选项。
        #:
        #: **它不进 `unresolved_report()`**：这是一条有取向的安全默认，
        #: 不是「等测量」的数值。混进去会让真正待定的那四项变得不起眼。
        self.require_position = bool(require_position)

    def unresolved_report(self):
        # type: () -> list
        """仍带未定标签的字段。与 `BandwidthPolicy` / `MediumPolicy` 同形。

        四项全部来自 11.md 页尾「请求大小、**会话过期**和分页上限由目标环境
        测量后确定」。前两项直接就是那句话里的东西；后两项（每玩家/每网络的
        会话数上限）是同一类资源上限，同批测量。
        """
        measured = "11-14 实测后确定；当前值是首轮测试配置，不是验收指标"
        return [
            {
                "field": "session.max_interaction_distance",
                "rule": "11-14",
                "value": self.max_interaction_distance,
                "note": "玩家与终端的交互距离上限；" + measured,
            },
            {
                "field": "session.session_ttl_frames",
                "rule": "11-14",
                "value": self.session_ttl_frames,
                "note": "多久没消息就回收会话（按逻辑帧）；" + measured,
            },
            {
                "field": "session.max_sessions_per_player",
                "rule": "11-14",
                "value": self.max_sessions_per_player,
                "note": "单个玩家的会话数上限，超限先关最旧的；" + measured,
            },
            {
                "field": "session.max_sessions_per_network",
                "rule": "11-14",
                "value": self.max_sessions_per_network,
                "note": "单个网络的会话数上限，超限先关最旧的；" + measured,
            },
        ]


class SessionCheckResult(object):
    """一次会话校验的结论。`ok` 为假时 `reason` 一定非空。"""

    __slots__ = ("ok", "reason", "detail")

    def __init__(self, ok, reason=None, detail=""):
        self.ok = bool(ok)
        self.reason = reason
        self.detail = detail
        if not self.ok and reason is None:
            raise ValueError("校验失败必须给出原因")

    def to_block_reason(self):
        # type: () -> BlockReason
        if self.reason is None:
            raise ValueError("校验通过时没有阻塞原因")
        return BlockReason(self.reason, self.detail)

    @classmethod
    def accepted(cls):
        # type: () -> SessionCheckResult
        return cls(True)

    @classmethod
    def refused(cls, reason, detail=""):
        # type: (str, str) -> SessionCheckResult
        return cls(False, reason, detail)

    def __repr__(self):
        if self.ok:
            return "SessionCheckResult(ok)"
        return "SessionCheckResult(%s, %r)" % (self.reason, self.detail)


class SessionContext(object):
    """打开会话所需的外部事实，**已由同步层解析完毕**。

    把「查注册表 / 查拓扑 / 查主控 / 查玩家坐标」的结果压成这几个字段，
    是为了让会话校验既可以离线造出来测试，又不必 import 拓扑、主控与
    注册表——那三样任意一个变了都不会牵动本模块的规则。
    """

    __slots__ = (
        "player_id",
        "record_ref",
        "network_id",
        "epoch",
        "master_instance_id",
        "network_workable",
        "player_dimension",
        "player_position",
    )

    def __init__(
        self,
        player_id,
        record_ref,
        network_id,
        epoch,
        master_instance_id,
        network_workable=True,
        player_dimension=None,
        player_position=None,
    ):
        self.player_id = player_id
        #: 该位置上**当前**存在的那一次实例；设备已不在时为 None。
        self.record_ref = record_ref
        self.network_id = network_id
        self.epoch = epoch
        self.master_instance_id = master_instance_id
        self.network_workable = bool(network_workable)
        self.player_dimension = player_dimension
        self.player_position = player_position


def within_reach(anchor, position, policy):
    # type: (tuple, tuple, SessionPolicy) -> bool
    """两个坐标是否在交互距离内。

    **平方距离比较，不出现开方与除法**——既省掉一次浮点运算，也就没有
    py2 整除的坑。任一方查不到坐标时按 `policy.require_position` 处理：
    默认拒绝（fail closed），因为「查不到坐标」恰恰是距离校验最该挡住的
    情形，放行等于把这道校验变成可选项。
    """
    if anchor is None or position is None:
        return not policy.require_position
    dx = position[0] - anchor[0]
    dy = position[1] - anchor[1]
    dz = position[2] - anchor[2]
    return (dx * dx + dy * dy + dz * dz) <= policy.max_interaction_distance_sq


class Session(object):
    """一次终端查看的会话状态。

    界面关闭、玩家离线、设备被替换或网络换代次都会让它作废（见
    `SessionRegistry` 的注销入口）。快照与增量的传输进度也挂在这里，
    因为这两件事的**生命周期与会话完全重合**——分开维护迟早对不上。
    """

    __slots__ = (
        "session_id",
        #: 数值序号，只用于排序（evict 时「先开的先关」）。
        #: `session_id` 是给人看的字符串，`"s10" < "s9"` 按字典序判就反了。
        "serial",
        "player_id",
        "device_ref",
        "network_id",
        "epoch",
        "master_instance_id",
        "view_kind",
        "dimension",
        "anchor_position",
        "state",
        "opened_frame",
        "last_seen_frame",
        "descriptions",
        "item_view_version",
        "snapshot_id",
        "transfer_cursor",
        "pending_deltas",
        "pending_delta_bytes",
        #: `transfer_id -> {"packets": [(条目数, 字节数), ...], "acked": int}`。
        #: 确认按**传输**而不是按会话记账：一次快照与一批增量同时在途时，
        #: 只按会话记总量就分不清哪个包还没到。
        "outstanding",
        "backlog_items",
        "backlog_bytes",
        "fail_count",
        "backoff_until_frame",
        #: 服务端已判定本会话的缓存不可信，等着一份新快照。**置位期间不发
        #: 增量**——发了也只会被客户端丢掉（它正要丢弃缓存）。
        "must_resync",
        "close_reason",
    )

    def __init__(
        self,
        session_id,
        serial,
        player_id,
        device_ref,
        network_id,
        epoch,
        master_instance_id,
        view_kind,
        dimension,
        anchor_position,
        frame,
    ):
        self.session_id = session_id
        self.serial = serial
        self.player_id = player_id
        self.device_ref = device_ref
        self.network_id = network_id
        self.epoch = epoch
        self.master_instance_id = master_instance_id
        self.view_kind = view_kind
        self.dimension = dimension
        #: 服务端解析出的终端坐标（不是客户端给的），距离校验的基准。
        self.anchor_position = anchor_position
        self.state = SESSION_ACTIVE
        self.opened_frame = frame
        self.last_seen_frame = frame
        #: 本会话的资源描述映射。序号只在本会话内有效（见 `accepts` 的说明）。
        self.descriptions = ResourceDescriptions(network_id, epoch)
        #: 物品视图版本：本会话已经发出过的那个版本。
        self.item_view_version = 0
        #: 在途快照的标识与已确认到的分片序号；没有在途快照时为空。
        self.snapshot_id = ""
        self.transfer_cursor = -1
        #: 快照期间到达的增量（有界暂存，见 `SyncPolicy`）。
        self.pending_deltas = []
        self.pending_delta_bytes = 0
        self.outstanding = {}
        #: 慢客户端的积压计数（已发出但未确认的部分）。
        self.backlog_items = 0
        self.backlog_bytes = 0
        self.fail_count = 0
        self.backoff_until_frame = 0
        self.must_resync = False
        self.close_reason = ""

    def touch(self, frame):
        # type: (int) -> None
        self.last_seen_frame = frame

    def is_expired(self, frame, policy):
        # type: (int, SessionPolicy) -> bool
        """超时会话。`session_ttl_frames <= 0` 表示不按时间回收。"""
        if policy.session_ttl_frames <= 0:
            return False
        return frame - self.last_seen_frame > policy.session_ttl_frames

    def reaches(self, position, policy):
        # type: (tuple, SessionPolicy) -> bool
        """玩家是否还在交互距离内。"""
        return within_reach(self.anchor_position, position, policy)

    def is_open(self):
        # type: () -> bool
        return self.state != SESSION_CLOSED

    def record_published(self, version):
        # type: (int) -> None
        self.item_view_version = version

    def to_dict(self):
        # type: () -> dict
        return {
            "session_id": self.session_id,
            "player_id": self.player_id,
            "network_id": self.network_id,
            "epoch": self.epoch,
            "view_kind": self.view_kind,
            "state": self.state,
            "device_ref": self.device_ref.to_dict(),
            "master_instance_id": self.master_instance_id,
            "item_view_version": self.item_view_version,
            "snapshot_id": self.snapshot_id,
            "transfer_cursor": self.transfer_cursor,
            "backlog_items": self.backlog_items,
            "fail_count": self.fail_count,
        }

    def __repr__(self):
        return "Session(%s, player=%s, %s)" % (
            self.session_id,
            self.player_id,
            self.state,
        )


class SessionRegistry(object):
    """活会话表。**唯一**的会话生命周期出入口。

    注销有六个触发点，全部收敛到这里，避免「某条路径忘了清理」——那种
    泄漏不会报错，只会让服务端持续给一个已经关掉的界面推库存。
    """

    def __init__(self, policy=None):
        self.policy = policy if policy is not None else SessionPolicy()
        self._sessions = {}
        #: 会话 ID 只增不减：同一个 epoch 内不会复用（复用的 ID 会让
        #: 「迟到的旧包」被当成新会话的消息）。
        self._next_serial = 1
        self.stats = {
            "opened": 0,
            "refused": 0,
            "closed": 0,
            "expired": 0,
            "evicted_by_limit": 0,
        }

    # -- 打开 ----------------------------------------------------------

    def open(self, envelope, context, frame):
        # type: (object, SessionContext, int) -> tuple
        """校验并登记一个会话，返回 `(Session, SessionCheckResult)`。

        失败时 Session 为 None。**所有校验都在这里做**，之后的每条请求
        只需 `get()` 之后复核距离与时效，不必重跑全套。

        收的是**信封**而不是消息体：`view_kind` 与 `session_id` 都是路由
        元数据，挂在信封上（`SyncEnvelope`）。这样一来「客户端说的」全在
        信封里，「服务端查到的事实」全在 `SessionContext` 里，两边的边界
        在调用点就是可见的。
        """
        result = self.check(envelope, context)
        if not result.ok:
            self.stats["refused"] += 1
            return None, result

        request = envelope.message

        # 数量上限：同一玩家/同一网络各有一条。超限时**先关最旧的**，
        # 而不是拒绝新请求——玩家重开界面本就该成功，卡住的是旧的那个。
        self._enforce_limits(context, frame)

        session_id = "s%d" % (self._next_serial,)
        self._next_serial += 1
        session = Session(
            session_id,
            self._next_serial - 1,
            context.player_id,
            context.record_ref,
            context.network_id,
            context.epoch,
            context.master_instance_id,
            envelope.view_kind,
            request.dimension,
            None if context.record_ref is None else context.record_ref.node_key.as_tuple(),
            frame,
        )
        self._sessions[session_id] = session
        self.stats["opened"] += 1
        return session, SessionCheckResult.accepted()

    def check(self, envelope, context):
        # type: (object, SessionContext) -> SessionCheckResult
        """逐项校验。顺序有意：先挡掉「根本不是这个设备/这个人」的请求，
        再谈网络资格——后者要查更多东西，而前者的失败原因对排查更关键。

        `SyncEnvelope` 构造时已经挡过一轮「未知视图种类」，这里再判一次是
        有意的：那一轮在 `settings.DEBUG` 关闭时不执行，而这条路径上的数据
        来自对面，检查必须无条件跑。
        """
        request = envelope.message
        view_kind = envelope.view_kind

        if not context.player_id:
            return SessionCheckResult.refused(
                Reason.SESSION_INVALID, "无法确认玩家身份（缺少可信来源）"
            )

        if view_kind not in VIEW_KINDS:
            return SessionCheckResult.refused(
                Reason.UNSUPPORTED, "未知的视图种类 %r" % (view_kind,)
            )
        if view_kind not in VIEW_KINDS_WITH_PRODUCER:
            return SessionCheckResult.refused(
                Reason.UNSUPPORTED,
                "%s 视图尚未接入（依赖 11-06/11-09）" % (view_kind,),
            )

        if context.record_ref is None:
            return SessionCheckResult.refused(
                Reason.DEVICE_GONE, "该位置上没有本模组的方块实例"
            )
        if not request.device_ref.matches(context.record_ref):
            # 位置相同、代次不同 = 方块被拆掉又在原地放了一个。
            return SessionCheckResult.refused(
                Reason.STALE_INSTANCE,
                "终端实例已被替换（请求 %d，当前 %d）"
                % (request.device_ref.generation, context.record_ref.generation),
            )

        if context.player_dimension is None:
            return SessionCheckResult.refused(
                Reason.SESSION_INVALID, "无法确认玩家所在维度"
            )
        if context.player_dimension != request.dimension:
            return SessionCheckResult.refused(
                Reason.SESSION_INVALID,
                "维度不符（玩家 %r，请求 %r）"
                % (context.player_dimension, request.dimension),
            )
        if context.player_dimension != context.record_ref.node_key.dimension:
            return SessionCheckResult.refused(
                Reason.SESSION_INVALID,
                "玩家与终端不在同一维度（%r / %r）"
                % (context.player_dimension, context.record_ref.node_key.dimension),
            )

        if not self._position_ok(context):
            return SessionCheckResult.refused(
                Reason.OUT_OF_RANGE,
                "超出交互距离 %g" % (self.policy.max_interaction_distance,),
            )

        if context.network_id is None:
            return SessionCheckResult.refused(
                Reason.WRONG_NETWORK, "该终端当前不属于任何网络"
            )
        if context.master_instance_id is None:
            return SessionCheckResult.refused(
                Reason.MASTER_UNLOADED, "该网络当前没有主控接管"
            )
        if not context.network_workable:
            return SessionCheckResult.refused(
                Reason.WRONG_NETWORK, "该网络当前不允许工作"
            )
        return SessionCheckResult.accepted()

    def _position_ok(self, context):
        # type: (SessionContext) -> bool
        if context.record_ref is None:
            return False
        return within_reach(
            context.record_ref.node_key.as_tuple(),
            context.player_position,
            self.policy,
        )

    def _enforce_limits(self, context, frame):
        # type: (SessionContext, int) -> None
        limit = self.policy.max_sessions_per_player
        if limit > 0:
            mine = [
                s
                for s in self._sessions.values()
                if s.player_id == context.player_id and s.is_open()
            ]
            while len(mine) >= limit:
                oldest = min(mine, key=lambda item: item.serial)
                self.close(oldest.session_id, "player_session_limit")
                mine.remove(oldest)
                self.stats["evicted_by_limit"] += 1
        limit = self.policy.max_sessions_per_network
        if limit > 0 and context.network_id is not None:
            theirs = self.sessions_for_network(context.network_id)
            while len(theirs) >= limit:
                oldest = min(theirs, key=lambda item: item.serial)
                self.close(oldest.session_id, "network_session_limit")
                theirs.remove(oldest)
                self.stats["evicted_by_limit"] += 1

    # -- 查询与注销 ----------------------------------------------------

    def get(self, session_id):
        # type: (str) -> Session
        session = self._sessions.get(session_id)
        if session is None or not session.is_open():
            return None
        return session

    def count(self):
        # type: () -> int
        return len(self._sessions)

    def sessions_for_network(self, network_id):
        # type: (str) -> list
        return [
            session
            for session in self._sessions.values()
            if session.network_id == network_id and session.is_open()
        ]

    def sessions_for_player(self, player_id):
        # type: (str) -> list
        return [
            session
            for session in self._sessions.values()
            if session.player_id == player_id and session.is_open()
        ]

    def close(self, session_id, reason):
        # type: (str, str) -> Session
        """关掉一个会话并**从表中移除**。重复关闭返回 None，不报错。"""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return None
        session.state = SESSION_CLOSED
        session.close_reason = reason
        self.stats["closed"] += 1
        return session

    def close_for_player(self, player_id, reason="player_left"):
        # type: (str, str) -> list
        return self._close_matching(
            lambda session: session.player_id == player_id, reason
        )

    def close_for_device(self, ref, reason="device_replaced"):
        # type: (DeviceRef, str) -> list
        """设备被拆除或替换。**按位置比对**：代次变了同样要关，
        因为界面上显示的已经不再是一台存在的方块。"""
        return self._close_matching(
            lambda session: session.device_ref is not None
            and session.device_ref.node_key.as_key() == ref.node_key.as_key(),
            reason,
        )

    def close_for_network(self, network_id, reason="network_changed"):
        # type: (str, str) -> list
        return self._close_matching(
            lambda session: session.network_id == network_id, reason
        )

    def close_all(self, reason="shutdown"):
        # type: (str) -> list
        """关掉**全部**会话。停机与热重载用。

        **它必须是这里的一个方法，而不是 `for session_id in ids(): close(...)`。**
        调用方自己遍历就得先拿到一份 ID 快照——那份快照的取得与使用之间
        隔着整个循环，中间新建的会话会**漏掉**（它不在快照里），于是停机后
        仍有一张订阅表活着。放在这里就没有那个窗口。
        """
        return self._close_matching(lambda session: True, reason)

    def close_for_epoch(self, network_id, epoch, reason="epoch_rotated"):
        # type: (str, str, str) -> list
        """网络换了代次：**所有旧代次的会话一律作废**。

        只比对 epoch 会漏掉「同一网络、代次没变但会话记的是上一任主控」，
        因此 `close_for_network` 仍要单独调用（见 `terminal.py` 的接线）。
        """
        return self._close_matching(
            lambda session: session.network_id == network_id and session.epoch != epoch,
            reason,
        )

    def _close_matching(self, predicate, reason):
        # type: (object, str) -> list
        doomed = [
            session_id
            for session_id, session in self._sessions.items()
            if session.is_open() and predicate(session)
        ]
        closed = []
        for session_id in sorted(doomed):
            session = self.close(session_id, reason)
            if session is not None:
                closed.append(session)
        return closed

    def evict_expired(self, frame):
        # type: (int) -> list
        closed = self._close_matching(
            lambda session: session.is_expired(frame, self.policy), "expired"
        )
        self.stats["expired"] += len(closed)
        return closed

    # -- 命令准入的探针 -------------------------------------------------

    def accepts(self, source):
        # type: (object) -> bool
        """`MasterController.session_check` 的答案。

        **无会话的命令一律放行。** 05/08 的既有调用方不带 `session_id`，
        若在这里把它们一并拒掉，等于给所有既有命令凭空加了一道具名门禁，
        而那不是 11-01 要改的东西。带会话的命令才走校验。
        """
        session_id = getattr(source, "session_id", "")
        if not session_id:
            return True
        session = self.get(session_id)
        if session is None:
            return False
        if session.player_id != getattr(source, "player_id", None):
            return False
        device_ref = getattr(source, "device_ref", None)
        if device_ref is not None and not device_ref.matches(session.device_ref):
            return False
        return True

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report.update(
            {
                "open": self.count(),
                "by_network": {},
                "next_serial": self._next_serial,
            }
        )
        for session in self._sessions.values():
            bucket = report["by_network"]
            bucket[session.network_id] = bucket.get(session.network_id, 0) + 1
        return report
