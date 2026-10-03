# coding=utf-8
"""分阶段恢复的闸门与一致性检查（12-05 / 12-08）。

对应任务 12 的两条要求：

- **12-05**：读持久数据 → 核对已加载设备 → 重建拓扑 → 校验唯一主控 →
  挂载存储 → 核对预留和执行 → 开放业务；**阶段未完成不接受新存取**。
- **12-08**：提供一致性检查与可复现恢复记录。

## 为什么闸门是一个对象而不是几个布尔

阶段名要能**报出来**（诊断、14 的故障测试都要读它），而几个散落的布尔
拼不出「现在卡在哪一阶段」。闸门只做一件事：记住当前阶段、拒绝未完成的
业务，并把每一步的读数留成一份可复现的记录。

## 它与主控准入链的分工

主控准入链回答的是「这张网络此刻能不能干活」（主控在不在、能源、带宽、
节点代次）。本闸门回答的是**时间上更早的那个问题**：「存档读完了没有、
设备对上了没有」。两者都要过，业务才能动——闸门放行业务不等于准入通过。

## 阶段边界与「区块发现」的关系

设备是按区块加载陆续发现的，因此「核对已加载设备」不可能等到「所有设备
都到齐」（那会永远等不到）。这里的口径是：**恢复阶段在第一次完整推进
之后开放**——那一帧里拓扑已按当时已知的设备重建、存储已按权威槽位挂载、
预留与执行台账已核对过。此后新发现的设备走常规的增量路径，不再受闸门
约束（它们本来也不需要在启动那一刻就在）。
"""

#: 恢复阶段。顺序即执行顺序，`READY` 是唯一开放业务的那一档。
PHASE_LOADING = "loading"
PHASE_RECONCILING = "reconciling"
PHASE_READY = "ready"

#: 阶段顺序表。诊断里按它展示「走过了哪些」。
PHASE_ORDER = (PHASE_LOADING, PHASE_RECONCILING, PHASE_READY)

#: 每个阶段在做的事（诊断里给人看的一句）。
PHASE_TEXT = {
    PHASE_LOADING: "读持久数据",
    PHASE_RECONCILING: "核对已加载设备、重建拓扑、校验主控、挂载存储、核对预留与执行",
    PHASE_READY: "开放业务",
}


class RecoveryGate(object):
    """分阶段恢复的闸门。**未就绪时拒绝新存取。**

    生命周期：
    - 构造时是 `loading`（存档还没读）；
    - `note_restored()` 之后进 `reconciling`（记录已读，等设备与拓扑）；
    - `mark_ready()` 之后进 `ready`（第一次完整推进之后调用）。

    `note_restored(ok=False)` 时**停在 `loading`**：读存档失败不等于
    「没有存档」，把它当成空存档继续是 12 验收明令禁止的那条。此时
    `is_open()` 恒假，业务一律被拒。
    """

    __slots__ = ("phase", "steps", "detail", "restore_ok", "ready_tick")

    def __init__(self):
        #: 当前阶段。
        self.phase = PHASE_LOADING
        #: 走过的阶段与各阶段的一行读数。**可复现恢复记录**的载体。
        self.steps = []
        self.detail = ""
        #: 存档读取是否成功。`None` = 还没试过。
        self.restore_ok = None
        self.ready_tick = None

    # -- 推进 ----------------------------------------------------------

    def begin(self):
        # type: () -> None
        """恢复开始（`start()` 的最开头调用）。"""
        self.phase = PHASE_LOADING
        self.steps = []
        self.detail = ""
        self.restore_ok = None
        self.ready_tick = None
        self._note(PHASE_LOADING, "开始读持久数据")

    def note_restored(self, ok, detail=""):
        # type: (bool, str) -> None
        """存档读取结束。成功则进 `reconciling`，失败则**停在 loading**。"""
        self.restore_ok = bool(ok)
        if not ok:
            self.phase = PHASE_LOADING
            self.detail = detail or "存档读取失败"
            self._note(PHASE_LOADING, "存档读取失败：%s" % (self.detail,))
            return
        self.phase = PHASE_RECONCILING
        self._note(PHASE_RECONCILING, detail or "记录已读，等待设备与拓扑就绪")

    def mark_ready(self, tick):
        # type: (int) -> None
        """第一次完整推进之后开放业务。重复调用是幂等的。"""
        if self.phase == PHASE_READY:
            return
        self.phase = PHASE_READY
        self.ready_tick = int(tick)
        self._note(PHASE_READY, "第 %d 帧开放业务" % (int(tick),))

    def _note(self, phase, detail):
        # type: (str, str) -> None
        self.steps.append({"phase": phase, "detail": detail})

    # -- 查询 ----------------------------------------------------------

    def is_open(self):
        # type: () -> bool
        """业务能不能动。**只有 `ready` 才是真。**"""
        return self.phase == PHASE_READY

    def blocker(self):
        # type: () -> str
        """没开放时的一句话原因。开放时为空串。"""
        if self.phase == PHASE_READY:
            return ""
        if self.restore_ok is False:
            return "存档读取失败，已拒绝新存取（不把读不出的存档当成空存档）：%s" % (
                self.detail,)
        return "恢复尚未完成（当前阶段：%s）" % (PHASE_TEXT.get(self.phase, self.phase),)

    def diagnostics(self):
        # type: () -> dict
        return {
            "phase": self.phase,
            "phase_text": PHASE_TEXT.get(self.phase, self.phase),
            "open": self.is_open(),
            "restore_ok": self.restore_ok,
            "ready_tick": self.ready_tick,
            "detail": self.detail,
            "steps": [dict(item) for item in self.steps],
            "blocker": self.blocker(),
        }


def consistency_report(server):
    # type: (object) -> dict
    """跨三层（驱动器 / 接口 / 网络台账）的一致性检查（12-08）。

    **它只读、不改。** 查出问题不自行修复：修复要让权威层去修，而这里看
    不到「谁是对的」——比如「槽位引用的介质没有记录」，那可能是存档不完整，
    也可能是介质记录本该删而没删，两条的处置完全不同。

    返回 `{"problems": [...], "notes": [...], "checked": {...}}`。
    `problems` 非空即不一致（`assert_consistent` 据此报错）；
    `notes` 是**如实报告、不算故障**的读数（待复核记录、放不下的槽位等），
    它们的处置归 R05 / 12-05 之后的人工流程，不该让启动失败。
    """
    problems = []
    notes = []
    checked = {}

    drivers = getattr(server, "drivers", None)
    ports = getattr(server, "ports", None)
    runtimes = getattr(server, "runtimes", None)

    if drivers is not None:
        report = drivers.diagnostics()
        checked["mediums"] = report.get("mediums", 0)
        checked["drivers"] = report.get("drivers", 0)
        # 一块介质装在两个槽位上：跨驱动器去重已经拦了「装」，但存档读回
        # 的那一份不经过 `mount`，因此这里再核一次。
        seen = {}
        for state in drivers.states():
            for assignment in state.occupied():
                other = seen.get(assignment.medium_id)
                if other is not None and other != state.node_key.as_key():
                    problems.append({
                        "where": "drivers",
                        "detail": "介质 %s 同时挂在 %r 与 %r"
                        % (assignment.medium_id, other, state.node_key.as_key()),
                    })
                else:
                    seen[assignment.medium_id] = state.node_key.as_key()
        # 槽位引用到的介质没有记录：如实计数（可能是存档不完整）。
        missing = 0
        for state in drivers.states():
            for assignment in state.occupied():
                if drivers.medium(assignment.medium_id) is None:
                    missing += 1
        if missing:
            notes.append({
                "where": "drivers",
                "detail": "%d 个槽位引用的介质没有记录（引用保留，不可交易）" % (missing,),
            })
        for key, count in (report.get("unplaced_slots") or {}).items():
            notes.append({
                "where": "drivers",
                "field": "unplaced_slots",
                "detail": "%s 有 %d 个槽位按当前槽数放不下（保留未截断，等 R10）"
                % (key, count),
            })
        for medium_id in report.get("frozen_mediums", ()):
            notes.append({
                "where": "drivers",
                "field": "frozen_mediums",
                "detail": "介质 %s 的记录读不懂，原样保留、不参与写回" % (medium_id,),
            })

    if ports is not None:
        report = ports.diagnostics()
        checked["ports"] = report.get("ports", 0)
        for item in report.get("unrecovered", ()):
            notes.append({
                "where": "ports",
                "field": "unrecovered",
                "detail": "接口 %r 的 %s 读不动：%s"
                % (item.get("node_key"), item.get("field"), item.get("detail")),
            })
        for key in report.get("pending_states", ()):
            notes.append({
                "where": "ports",
                "field": "pending_states",
                "detail": "位置 %s 的接口记录还没等到设备（等区块发现）" % (key,),
            })
        for key in report.get("frozen_ports", ()):
            notes.append({
                "where": "ports",
                "field": "frozen_ports",
                "detail": "位置 %s 的接口记录读不懂，原样保留、不参与写回" % (key,),
            })

    if runtimes is not None:
        for runtime in runtimes.runtimes():
            storage = runtime.services.get("storage")
            if storage is None:
                continue
            # `reserved > stored` 是记账错误（预留台账说压着的东西比盘上多）。
            # 这不是「读不懂」，是**真的不一致**，必须报出来。
            for key, entry in getattr(storage, "item_totals", {}).items():
                stored, reserved = entry[0], entry[1]
                if reserved > stored:
                    problems.append({
                        "where": "storage",
                        "detail": "%s 的预留 %d 超过存量 %d"
                        % (key.canonical(), reserved, stored),
                    })
    return {"problems": problems, "notes": notes, "checked": checked}


def assert_consistent(server):
    # type: (object) -> dict
    """`consistency_report` 的问题非空时抛错。返回报告本身。

    **只在显式调用时抛**（同 `assert_profile_ok` / `assert_settled`）：
    启动路径不该因为一份待复核记录就失败——`notes` 里的东西是**如实报告**，
    不是故障。
    """
    report = consistency_report(server)
    if report["problems"]:
        raise AssertionError(
            "恢复一致性检查发现 %d 处问题：\n  " % (len(report["problems"]),)
            + "\n  ".join(
                "[%s] %s" % (item.get("where"), item.get("detail"))
                for item in report["problems"]
            )
        )
    return report
