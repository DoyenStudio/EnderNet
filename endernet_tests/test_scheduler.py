# coding=utf-8
"""任务 04-01 / 04-02 / 04-03 / 04-07 的离线验收：逻辑时钟、到期队列与调度服务。

覆盖 04 验收清单里「什么时候推进、谁先推进、卡顿了怎么办」的部分：

- **逻辑时钟是帧序，不是秒**（04-01）。时钟里不出现任何频率换算，
  三种引擎脉冲只记计数供真机算比值。
- **同一任务重排不会重复执行**（04-02 验收）。靠「活体表里同键只有一条」
  这个结构性质保证，而不是靠调用方记得先 `cancel`。
- **设备替换、卸载后旧任务失效**（04-02 验收）。三道检查：换 ref、移除、
  以及执行时按代次回查注册表。
- **空闲网络不高频扫描设备；有变化可唤醒；无事件容器最终被轮询发现**（04-02 验收）。
- **多网络同时繁忙时都有进展，大网络不长期饿死小网络**（04-03 验收）。
  `RuntimeManager.tick` 此前的「按分量编号先到先得」会永久饿死排在后面的
  分量，本文件里有一条用例专门钉住修复后的分配形状。
- **单次推进不无界处理积压任务**（04-03 验收）。
- **假时钟可精确重现低速累积、卡顿和恢复**（04 验收）——本文件全部用例
  都靠显式 `clock.advance()` 控制时间，没有一处`sleep`。

假服务沿用 `test_runtime.py` 的记录型双件形态：只数调用、不假装有库存。
"""

import unittest

import testsupport  # noqa: F401  设置 sys.path

from endernet.common.definitions import DefinitionSet
from endernet.common.identity import DeviceRef, NodeKey
from endernet.server.adapters.events import ServerEventBinder
from endernet.server.bootstrap import ServerBootstrap
from endernet.server.devices.registry import DeviceRegistry
from endernet.server.network.node import NetworkNode
from endernet.server.network.services import (
    NetworkService,
    ServiceFactory,
    ServiceId,
)
from endernet.server.network.ticking import (
    NodeLocator,
    RotationCursor,
    SchedulerService,
)
from endernet.server.runtime.events import (
    EventKind,
    EventQueue,
    NormalizedEvent,
    WakeSet,
)
from endernet.server.runtime.scheduler import (
    UNRESOLVED_FIELDS,
    DeviceLane,
    DueQueue,
    LogicalClock,
    PollBackoff,
    PollOutcome,
    ScheduledTask,
    SchedulerPolicy,
    TaskKey,
    TaskKind,
    default_timer,
)

TINY = "endernet:tiny_network_endpoint"
DRIVER = "endernet:storage_driver"


def key(x, y=64, z=0, dimension=0):
    # type: (int, int, int, int) -> NodeKey
    return NodeKey(dimension, x, y, z)


def ref(x, generation=1, y=64, z=0, dimension=0):
    # type: (int, int, int, int, int) -> DeviceRef
    return DeviceRef(key(x, y, z, dimension), generation)


class TestLogicalClock(unittest.TestCase):
    """04-01：时钟是**调用计数**，不是秒。"""

    def test_counts_calls_not_seconds(self):
        clock = LogicalClock()
        clock.advance(7)
        clock.advance(23)
        # 30 是位移总量，不是「秒」——时钟根本不知道一帧多长。
        self.assertEqual(clock.current_tick(), 30)
        # 两次调用 ⇒ 两次推进。位移与推进次数分开记，是因为诊断里
        # 「走了几帧」和「被推进了几次」是两个问题。
        self.assertEqual(clock.advances, 2)

    def test_the_clock_does_not_store_pulse_counts(self):
        """脉冲计数只有一份，在事件适配器里（`ServerEventBinder.counts`）。

        时钟里再存一份必然分叉：曾经 `LogicalClock.pulses` 就是第二份，
        而适配器只更新自己的那份，于是 `pulses["block_entity_tick"]` 在真机上
        永远是 0——04-01 要量的比值会变成 0，看起来像「引擎根本没派发」。
        """
        clock = LogicalClock()
        clock.advance()
        self.assertEqual(sorted(clock.describe()), ["advances", "tick"])
        self.assertFalse(hasattr(clock, "note_pulse"))

    def test_refuses_to_go_backwards(self):
        """倒退会让已排定的任务全部变成「过去」，一次退回触发一整批。"""
        clock = LogicalClock()
        clock.advance(5)
        with self.assertRaises(ValueError):
            clock.advance(-1)
        self.assertEqual(clock.current_tick(), 5)

    def test_start_offset_is_honoured(self):
        clock = LogicalClock(start=100)
        self.assertEqual(clock.current_tick(), 100)
        clock.advance()
        self.assertEqual(clock.current_tick(), 101)

    def test_default_timer_is_monotonic_enough_for_diagnostics(self):
        """计时器只用于诊断，但至少要能用且不倒流。"""
        first = default_timer()
        second = default_timer()
        self.assertIsInstance(first, float)
        self.assertGreaterEqual(second, first)


class TestTaskKey(unittest.TestCase):
    """代次参与身份：同一坐标的两个方块是**两个键**。"""

    def test_keys_differ_by_generation(self):
        first = TaskKey.of(ref(10, generation=1), "storage:io")
        second = TaskKey.of(ref(10, generation=2), "storage:io")
        self.assertNotEqual(first, second)
        self.assertNotEqual(first.as_key(), second.as_key())
        self.assertTrue(first.matches_ref(ref(10, generation=1)))
        self.assertFalse(first.matches_ref(ref(10, generation=2)))

    def test_keys_differ_by_kind(self):
        first = TaskKey.of(ref(10), "storage:io")
        second = TaskKey.of(ref(10), "transfer:pull")
        self.assertNotEqual(first, second)

    def test_keys_are_hashable_and_dict_safe(self):
        first = TaskKey.of(ref(10), "storage:io")
        second = TaskKey.of(ref(10), "storage:io")
        self.assertEqual(first, second)
        self.assertEqual(len({first: 1, second: 2}), 1)

    def test_empty_kind_is_refused(self):
        """空 kind 会让两个不同服务的任务撞成同一个键、互相顶替。"""
        with self.assertRaises(ValueError):
            TaskKey.of(ref(10), "")


class TestDueQueue(unittest.TestCase):
    """到期队列：堆序、懒删除与「重排 = 替换」。"""

    def _task(self, x, kind, due, period=None, handler=None, generation=1):
        target = ref(x, generation=generation)
        return ScheduledTask(
            TaskKey.of(target, kind), target, due, period=period, handler=handler
        )

    def test_pops_in_due_order(self):
        queue = DueQueue()
        queue.put(self._task(1, "a", 30))
        queue.put(self._task(1, "b", 10))
        queue.put(self._task(1, "c", 20))
        self.assertEqual(queue.peek_due(), 10)
        self.assertEqual([queue.pop_due(100).key.kind for _ in range(3)], ["b", "c", "a"])

    def test_same_due_is_ordered_by_arrival(self):
        """同刻到期的两个任务也必须可比较。

        堆元组是 `(due, sequence, key)`——把 `key` 或 `NodeKey` 提到前面，
        这里就会在 Python 3 下抛 `TypeError: '<' not supported between
        instances of 'NodeKey'`，而那只在「恰好同刻到期」时出现。
        """
        queue = DueQueue()
        queue.put(self._task(1, "a", 10))
        queue.put(self._task(2, "b", 10))
        self.assertEqual([queue.pop_due(10).key.kind for _ in range(2)], ["a", "b"])

    def test_pop_stops_at_the_due_horizon(self):
        queue = DueQueue()
        queue.put(self._task(1, "a", 10))
        self.assertIsNone(queue.pop_due(9))
        self.assertEqual(len(queue), 1)

    def test_peek_of_an_empty_queue_is_none(self):
        """`None` = 没有任务，不是「到期时刻是 0」。"""
        queue = DueQueue()
        self.assertIsNone(queue.peek_due())
        self.assertIsNone(queue.pop_due(1000))

    def test_reput_replaces_the_live_entry(self):
        queue = DueQueue()
        first = self._task(1, "a", 10)
        queue.put(first)
        second = self._task(1, "a", 50)
        queue.put(second)
        # 活体只有一条：旧的那条成了墓碑。
        self.assertEqual(len(queue), 1)
        self.assertIs(queue.get(first.key), second)
        # 旧 due 已过去，但活体是 50，因此 10 时刻没有东西可弹。
        self.assertIsNone(queue.pop_due(10))
        self.assertEqual(queue.pop_due(50), second)

    def test_drop_removes_only_the_live_entry(self):
        queue = DueQueue()
        task = self._task(1, "a", 10)
        queue.put(task)
        self.assertTrue(queue.drop(task.key))
        self.assertFalse(queue.drop(task.key))
        self.assertEqual(len(queue), 0)
        self.assertIsNone(queue.peek_due())

    def test_tombstones_are_reclaimed_at_the_top(self):
        """墓碑只在弹到时清掉：堆只增不减是有意的，但 peek 不能卡在尸体上。"""
        queue = DueQueue()
        queue.put(self._task(1, "a", 10))
        queue.put(self._task(1, "a", 50))  # 替换掉上面那条，它成了墓碑
        queue.put(self._task(2, "b", 30))
        self.assertEqual(queue.stats()["tombstones"], 1)
        # 堆顶是 due=10 的墓碑；peek 必须跳过它，而不是报「最早到期是 10」。
        self.assertEqual(queue.peek_due(), 30)
        self.assertEqual(queue.pop_due(30).key.kind, "b")

    def test_periodic_requeue_does_not_resurrect_the_old_slot(self):
        queue = DueQueue()
        task = self._task(1, "a", 10, period=5)
        queue.put(task)
        popped = queue.pop_due(10)
        self.assertIs(popped, task)
        # 执行后 due 前移并重新入队——堆里那条旧条目必须被认成墓碑，
        # 否则同一任务会在同一帧被弹两次。
        task.due = 15
        queue.put(task)
        self.assertEqual(len(queue), 1)
        self.assertIsNone(queue.pop_due(14))
        self.assertIs(queue.pop_due(15), task)

    def test_count_due_is_bounded_and_flags_truncation(self):
        queue = DueQueue()
        for index in range(10):
            queue.put(self._task(1, "t%d" % (index,), 5))
        count, capped = queue.count_due(10, 4)
        self.assertEqual(count, 4)
        self.assertTrue(capped, "还有第 5 条没数到，说明真实积压更多")
        count, capped = queue.count_due(10, 100)
        self.assertEqual(count, 10)
        self.assertFalse(capped)

    def test_count_due_at_exactly_the_limit_is_not_truncation(self):
        """恰好等于上限时**不是**被截断——虚报会让人得出错误的积压结论。"""
        queue = DueQueue()
        for index in range(4):
            queue.put(self._task(1, "t%d" % (index,), 5))
        self.assertEqual(queue.count_due(10, 4), (4, False))
        # 第 5 条一来才算截断。
        queue.put(self._task(1, "t4", 5))
        self.assertEqual(queue.count_due(10, 4), (4, True))

    def test_count_due_ignores_the_future(self):
        queue = DueQueue()
        queue.put(self._task(1, "a", 10))
        queue.put(self._task(1, "b", 99))
        self.assertEqual(queue.count_due(10, 100), (1, False))

    def test_tasks_view_is_stable_and_does_not_mutate(self):
        queue = DueQueue()
        queue.put(self._task(1, "b", 20))
        queue.put(self._task(1, "a", 10))
        listed = queue.tasks()
        self.assertEqual([task.key.kind for task in listed], ["a", "b"])
        self.assertEqual(len(queue), 2, "查询不得消费任务")


class TestPollBackoff(unittest.TestCase):
    """04-02：低频退避轮询。"""

    def test_grows_exponentially_then_caps(self):
        backoff = PollBackoff(base_ticks=1, max_ticks=8, factor=2)
        seen = [backoff.delay()]
        for _ in range(6):
            seen.append(backoff.note_unchanged())
        self.assertEqual(seen[:4], [1, 2, 4, 8])
        self.assertEqual(seen[-1], 8, "封顶之后不再增长")
        self.assertEqual(backoff.steps(), 6, "档位照记，只是间隔不再变大")

    def test_change_resets_to_the_initial_interval(self):
        backoff = PollBackoff(base_ticks=2, max_ticks=64, factor=2)
        backoff.note_unchanged()
        backoff.note_unchanged()
        self.assertEqual(backoff.delay(), 8)
        self.assertEqual(backoff.note_changed(), 2)
        self.assertEqual(backoff.steps(), 0)
        self.assertEqual(backoff.changed, 1)

    def test_refuses_parameters_that_cannot_work(self):
        with self.assertRaises(ValueError):
            PollBackoff(0, 10)
        with self.assertRaises(ValueError):
            PollBackoff(10, 5)
        with self.assertRaises(ValueError):
            PollBackoff(1, 10, factor=1)

    def test_a_huge_step_count_does_not_overflow(self):
        """不用 `**`：循环带提前退出，档位再大也只是撞上限。"""
        backoff = PollBackoff(base_ticks=3, max_ticks=97, factor=3)
        for _ in range(200):
            backoff.note_unchanged()
        self.assertEqual(backoff.delay(), 97)

    def test_describe_reports_the_current_interval(self):
        backoff = PollBackoff(base_ticks=1, max_ticks=4)
        backoff.note_unchanged()
        described = backoff.describe()
        self.assertEqual(described["steps"], 1)
        self.assertEqual(described["delay"], 2)


class TestPollOutcome(unittest.TestCase):
    """轮询回调的返回值归一化。"""

    def test_none_means_no_change_and_no_cost(self):
        outcome = PollOutcome.of(None)
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.cost, 0)

    def test_tuple_is_unpacked(self):
        outcome = PollOutcome.of((True, 3))
        self.assertTrue(outcome.changed)
        self.assertEqual(outcome.cost, 3)

    def test_outcome_passes_through(self):
        original = PollOutcome(False, 7)
        self.assertIs(PollOutcome.of(original), original)


class TestDeviceLane(unittest.TestCase):
    """lane 只承载一台设备；没有消费者也是一个被记录的事实。"""

    def test_keeps_position_and_generation_together(self):
        backoff = PollBackoff(1, 4)
        lane = DeviceLane(ref(3, generation=7), TINY, backoff)
        self.assertEqual(lane.node_key, key(3))
        self.assertEqual(lane.ref.generation, 7)

    def test_it_does_not_hold_a_second_copy_of_loaded(self):
        """「是否加载」在注册表里。lane 上再存一份永远不会被更新
        （区块卸载的事件路径只到注册表为止），只会让诊断撒谎。"""
        lane = DeviceLane(ref(3), TINY, PollBackoff(1, 4))
        self.assertFalse(hasattr(lane, "loaded"))
        self.assertNotIn("loaded", lane.to_dict())

    def test_no_consumer_is_distinguishable_from_no_polling(self):
        lane = DeviceLane(ref(3), TINY, PollBackoff(1, 4))
        self.assertFalse(lane.has_consumer())
        described = lane.to_dict()
        self.assertFalse(described["has_consumer"])
        # 轮询本身照跑，并计入指标——「没注册回调」不等于「没在轮询」。
        self.assertIn("poll_due", described)


class TestSchedulerPolicy(unittest.TestCase):
    """未定规则一律留成具名配置点（全局约束 7）。"""

    def test_every_unresolved_field_is_reportable(self):
        reported = SchedulerPolicy().unresolved_report()
        self.assertEqual(
            sorted(item["field"] for item in reported),
            sorted(field for field, _, _ in UNRESOLVED_FIELDS),
        )
        for item in reported:
            # 每条都要写清「依据哪份还没定」，否则读者无从跟进。
            self.assertTrue(item["rule"])
            self.assertTrue(item["note"])

    def test_settled_guard_refuses_while_rules_are_open(self):
        """它只在正式验收时被调用；开发与离线测试不应调用。"""
        with self.assertRaises(AssertionError):
            SchedulerPolicy().assert_settled()

    def test_catch_up_is_not_advertised_as_unresolved(self):
        """「卡顿后不集中补做」是 04 文档明写的要求，不是待定项。"""
        fields = [item["field"] for item in SchedulerPolicy().unresolved_report()]
        self.assertNotIn("catch_up", fields)

    def test_backoff_is_built_from_the_policy(self):
        policy = SchedulerPolicy(
            backoff_base_ticks=3, backoff_max_ticks=12, backoff_factor=2
        )
        backoff = policy.make_backoff()
        self.assertEqual(backoff.delay(), 3)
        self.assertEqual(backoff.max_ticks, 12)

    def test_describe_carries_the_unresolved_report(self):
        described = SchedulerPolicy().describe()
        self.assertEqual(
            len(described["unresolved"]), len(UNRESOLVED_FIELDS)
        )


class TestWakeSet(unittest.TestCase):
    """方块实体 tick 的唤醒集合：同位置同类只留一条，顺序可复现。"""

    def test_dedups_by_position_and_kind(self):
        wakes = WakeSet()
        self.assertTrue(wakes.add(EventKind.BLOCK_ENTITY_TICK, key(1)))
        self.assertTrue(wakes.add(EventKind.BLOCK_ENTITY_TICK, key(1)))
        self.assertEqual(len(wakes), 1)
        self.assertEqual(wakes.stats()["coalesced"], 1)
        self.assertEqual(len(wakes.take()), 1)

    def test_two_positions_both_survive(self):
        wakes = WakeSet()
        wakes.add(EventKind.BLOCK_ENTITY_TICK, key(1))
        wakes.add(EventKind.BLOCK_ENTITY_TICK, key(2))
        self.assertEqual(len(wakes.take()), 2)

    def test_take_is_sorted_and_empties(self):
        """集合的遍历顺序不该决定谁先被唤醒，否则问题不可复现。"""
        wakes = WakeSet()
        for x in (9, 3, 7):
            wakes.add(EventKind.BLOCK_ENTITY_TICK, key(x))
        self.assertEqual([signal.node_key.x for signal in wakes.take()], [3, 7, 9])
        self.assertEqual(len(wakes), 0)
        self.assertEqual(wakes.take(), ())

    def test_overflow_is_dropped_but_counted(self):
        """丢一条唤醒不会丢设备（注册与到期任务都不经过它），但要有痕迹。"""
        wakes = WakeSet(max_pending=2)
        for x in range(5):
            wakes.add(EventKind.BLOCK_ENTITY_TICK, key(x))
        self.assertEqual(len(wakes), 2)
        self.assertEqual(wakes.stats()["dropped"], 3)

    def test_clear_drops_everything(self):
        wakes = WakeSet()
        wakes.add(EventKind.BLOCK_ENTITY_TICK, key(1))
        wakes.add(EventKind.BLOCK_ENTITY_TICK, key(2))
        self.assertEqual(wakes.clear(), 2)
        self.assertEqual(len(wakes), 0)


class QuietWorld(object):
    """离线用的最小世界替身：不查引擎，也不假装有区块。"""

    def __init__(self):
        self.whitelisted = []

    def list_loaded_chunks(self):
        return []

    def block_entities_in_chunk(self, chunk_key):
        return []

    def register_block_remove_listener(self, block_ids):
        self.whitelisted.append(block_ids)

    def facing_at(self, node_key):
        return None

    def note_chunk_loaded(self, chunk_key, entries=()):
        pass

    def note_chunk_unloaded(self, chunk_key):
        pass

    def diagnostics(self):
        return {"fake": True}


class TestBlockEntityTickTranslation(unittest.TestCase):
    """适配器把方块实体 tick 翻译成**唤醒信号**，不占事件队列的名额。

    这条区分的实际后果：开 `tick: true` 的设备每帧都在心跳，
    若把它塞进 `EventQueue`，一次区块加载的积压会让脉冲吃掉 `drain_limit`，
    而队列满时丢的是新来的放置事件——设备会凭空消失。
    """

    def setUp(self):
        self.definitions = DefinitionSet()
        self.binder = ServerEventBinder(
            EventQueue(),
            QuietWorld(),
            DeviceRegistry(self.definitions),
            self.definitions,
        )

    def test_foreign_block_is_counted_but_not_recorded(self):
        self.binder._on_block_entity_tick(0, 1, 64, 0, "minecraft:stone")
        self.assertEqual(len(self.binder.wakes), 0)
        self.assertEqual(self.binder.counts.get("block_entity_tick"), 1)

    def test_our_block_becomes_a_wake_and_not_an_event(self):
        self.binder._on_block_entity_tick(0, 1, 64, 0, TINY)
        self.assertEqual(len(self.binder.queue), 0, "唤醒不该占用事件队列的名额")
        taken = self.binder.take_wakes()
        self.assertEqual(len(taken), 1)
        self.assertEqual(taken[0].kind, EventKind.BLOCK_ENTITY_TICK)
        self.assertEqual(taken[0].node_key, key(1))
        self.assertEqual(len(self.binder.wakes), 0, "取走之后要清空")

    def test_repeated_pulses_at_one_position_collapse(self):
        for _ in range(20):
            self.binder._on_block_entity_tick(0, 1, 64, 0, TINY)
        self.assertEqual(len(self.binder.take_wakes()), 1)

    def test_pulse_counts_include_block_entity_ticks(self):
        """04-01 要在真机上量「方块实体 tick / 脚本 tick」的比值。

        **计数只能有一份。** 曾经时钟那边另存过一份 `pulses`，而适配器只
        更新自己的 `counts`，于是 `pulses["block_entity_tick"]` 在真机上
        永远是 0——量出来的比值会是 0，看起来像「引擎根本没派发这个事件」。
        """
        self.binder._on_block_entity_tick(0, 1, 64, 0, TINY)
        self.binder._on_tick(EventKind.SCRIPT_TICK)
        counts = self.binder.pulse_counts()
        self.assertEqual(counts[EventKind.BLOCK_ENTITY_TICK], 1)
        self.assertEqual(counts[EventKind.SCRIPT_TICK], 1)
        self.assertEqual(counts[EventKind.SIM_TICK], 0, "没发生的脉冲是真实的 0")

    def test_stopping_clears_pending_wakes(self):
        self.binder._on_block_entity_tick(0, 1, 64, 0, TINY)
        self.assertEqual(self.binder.clear_wakes(), 1)
        self.assertEqual(self.binder.take_wakes(), ())


class TestRotationCursor(unittest.TestCase):
    """跨网轮转的纯函数部分。"""

    def test_ring_rotates_the_starting_slot(self):
        keys = ["a", "b", "c"]
        self.assertEqual(RotationCursor.plan_frame(keys, 9, 0)[0], [0, 1, 2])
        self.assertEqual(RotationCursor.plan_frame(keys, 9, 1)[0], [1, 2, 0])
        self.assertEqual(RotationCursor.plan_frame(keys, 9, 2)[0], [2, 0, 1])
        # 游标越界也要回到环上，不能抛。
        self.assertEqual(RotationCursor.plan_frame(keys, 9, 7)[0], [1, 2, 0])

    def test_share_is_the_budget_split_evenly(self):
        self.assertEqual(RotationCursor.plan_frame(["a", "b", "c", "d"], 10)[1], 2)
        self.assertEqual(RotationCursor.plan_frame(["a", "b"], 64)[1], 32)

    def test_share_never_exceeds_the_budget(self):
        """保底不能凭空放大总预算。

        要验的是那一句 `if share > budget: share = budget`，因此必须构造
        「保底下限比预算还大」：否则 `budget // n < budget` 永远成立，
        钳位那行删掉也不会失败。
        """
        ring, share = RotationCursor.plan_frame(["a"] * 5, 2)
        self.assertEqual(len(ring), 5)
        self.assertEqual(share, 1)
        self.assertEqual(
            RotationCursor.plan_frame(["a"] * 5, 2, min_share_floor=10)[1], 2,
            "保底大于预算时必须截到预算",
        )
        self.assertEqual(RotationCursor.plan_frame(["a"] * 5, 1)[1], 1)

    def test_empty_inputs_are_not_an_error(self):
        self.assertEqual(RotationCursor.plan_frame([], 64), ([], 0))
        self.assertEqual(RotationCursor.plan_frame(["a"], 0), ([], 0))
        self.assertEqual(RotationCursor.plan_frame(["a"], -5), ([], 0))

    def test_plan_is_deterministic(self):
        first = RotationCursor.plan_frame(["a", "b", "c"], 10, cursor=2)
        second = RotationCursor.plan_frame(["a", "b", "c"], 10, cursor=2)
        self.assertEqual(first, second)

    def test_first_slot_counts_only_when_something_was_granted(self):
        """首位统计要按「谁真的拿到了额度」算，否则空闲网络会污染统计。"""
        cursor = RotationCursor()
        cursor.note_frame([("a", 0, 0), ("b", 4, 4)])
        cursor.note_frame([("a", 4, 4), ("b", 0, 0)])
        self.assertEqual(cursor.describe()["firsts"], {"b": 1, "a": 1})

    def test_locator_routes_by_position_and_counts_misses(self):
        class Runtime(object):
            def __init__(self, component_id, keys):
                self.component_id = component_id
                self._keys = keys

            def member_keys(self):
                return tuple(self._keys)

        locator = NodeLocator()
        self.assertEqual(locator.rebuild([Runtime("c1", [key(1), key(2)])]), 2)
        self.assertEqual(locator.locate(key(2)), "c1")
        # 不认识的位置返回 None 而不是抛错：设备刚放下的那一帧就是这种情形。
        self.assertIsNone(locator.locate(key(99)))
        self.assertEqual(locator.describe()["unrouted"], 1)


class HungryService(NetworkService):
    """每次 `tick` 都报一个远大于额度的成本——用来观察轮转怎么分额度。"""

    COST = 100

    def __init__(self, service_id, log):
        # type: (str, list) -> None
        self.service_id = service_id
        self.requires = ()
        self.log = log
        self.calls = 0

    def tick(self, context, budget):
        # type: (dict, int) -> int
        self.calls += 1
        self.log.append((self.service_id, budget))
        return self.COST

    def on_wake(self, node_key):
        # type: (object) -> bool
        # 本组用例只关心**路由**，因此任何位置都算「处理了」。
        self.wakes_seen = getattr(self, "wakes_seen", 0) + 1
        return True


class TestFairRotation(unittest.TestCase):
    """04-03：多网络同时繁忙时都有进展，大网络不长期饿死小网络。

    **这一组用例钉住的是一个真实缺陷。** `RuntimeManager.tick` 此前按
    `sorted(component_id)` 先到先得地花全局预算：排在前面的分量把预算吃满，
    后面的每帧拿到 0，被**永久饿死**。把 `tick()` 换回那个写法，
    `test_every_network_gets_a_share_every_frame` 会立刻失败。
    """

    #: 两台网络相距很远，保证是互不相连的两个分量。
    def setUp(self):
        self.world = QuietWorld()
        self.created = {}
        self.server = ServerBootstrap(
            world=self.world, service_factories=ServiceFactory(self._builders())
        )
        self.server.start(bind_engine=False)

    def _builders(self):
        builders = {}
        for service_id in ServiceId.ALL:
            builders[service_id] = self._builder_for(service_id)
        return builders

    def _builder_for(self, service_id):
        def build(runtime):
            service = HungryService(service_id, [])
            # 构造器必须接受 `None` 运行时：`stand_in_slots()` 会为了回答
            # 「这个槽位还是替身吗」而空造一个实例（`attach()` 才绑定）。
            component_id = getattr(runtime, "component_id", None)
            if component_id is not None:
                self.created.setdefault(component_id, {})[service_id] = service
            return service

        return build

    def build_networks(self, count=2):
        """摆 `count` 台互不相连的网络：控制器与基带都按 x 相邻配对，
        各对之间相距 100 格，保证不会连成一片。"""
        for index in range(count):
            base_z = index * 100
            self.server.push(NormalizedEvent.place(key(1, 65, base_z), TINY))
            self.server.push(
                NormalizedEvent.place(
                    key(0, 65, base_z), "endernet:network_baseband_8"
                )
            )
        self.server.tick()
        self.assertEqual(
            len(self.server.runtimes.runtimes()), count,
            "应当是 %d 台互不相连的网络" % (count,),
        )

    def build_two_networks(self):
        self.build_networks(2)

    def calls_by_component(self):
        return dict(
            (component_id, sum(item.calls for item in services.values()))
            for component_id, services in self.created.items()
        )

    def test_every_network_gets_a_share_every_frame(self):
        self.build_two_networks()
        for _ in range(20):
            self.server.clock.advance()
            self.server.tick()
        calls = self.calls_by_component()
        self.assertEqual(len(calls), 2)
        # 每台网络每帧至少被服务推进一次。旧写法下排头的那台会独占预算，
        # 另一台的 calls 恒为 0。
        self.assertGreaterEqual(
            min(calls.values()), 20, "每台网络每帧都该拿到额度：%r" % (calls,)
        )
        # 两台拿到的份额相同——轮转不偏向任何一台。
        self.assertEqual(len(set(calls.values())), 1, "轮转应当均分：%r" % (calls,))

    def test_the_budget_is_never_exceeded(self):
        self.build_two_networks()
        for _ in range(5):
            self.server.clock.advance()
            report = self.server.tick()
            self.assertLessEqual(report["service_steps"], self.server.runtime_budget)
            self.assertGreaterEqual(report["service_steps"], 0)

    def test_the_starting_slot_rotates_across_frames(self):
        """`budget < n` 时不能保证人人每帧有份，但首位必须轮换。"""
        self.build_two_networks()
        firsts = {}
        for _ in range(6):
            self.server.clock.advance()
            self.server.tick()
            frame = self.server.runtimes.rotation_view()["last_frame"]
            for item in frame:
                if item["granted"] > 0:
                    firsts[item["component_id"]] = (
                        firsts.get(item["component_id"], 0) + 1
                    )
                    break
        self.assertEqual(sorted(firsts.values()), [3, 3], "首位应当轮流坐：%r" % (firsts,))

    def test_a_closed_network_withholds_wakes(self):
        """关闭中的网络不该被外部脉冲驱动——与 `tick()` 那道门同一个理由。

        放唤醒进去不只是「白白改了状态」：网络重新开放的一刻，所有设备的
        退避都被重置过，会以最短间隔同时轮询。
        """
        self.build_two_networks()
        self.server.clock.advance()
        self.server.tick()
        runtime = self.server.runtimes.runtime_of(key(0, 65))
        self.assertTrue(runtime.is_open())
        # 拆掉控制器：运行时保留，但没有主控，业务不再开放。
        self.server.push(NormalizedEvent.remove(key(1, 65), TINY))
        self.server.clock.advance()
        self.server.tick()
        self.assertFalse(runtime.is_open())
        # 唤醒要打给**仍然属于这台关闭网络**的位置（基带），否则它是
        # 「路由不到」（unrouted）而不是「送到了但网络关着」（withheld）。
        self.assertIn(key(0, 65), runtime.member_keys())
        self.assertEqual(
            self.server.runtimes.deliver_wakes([key(0, 65)]), 0,
            "关闭中的网络不该收到唤醒",
        )
        self.assertGreaterEqual(runtime.stats["wakes_withheld"], 1)

    def test_wakes_reach_the_owning_network(self):
        """唤醒按位置路由；未命中的位置只记数，不报错。"""
        self.build_two_networks()
        self.server.clock.advance()
        self.server.tick()
        delivered = self.server.runtimes.deliver_wakes([key(1, 65)])
        self.assertEqual(delivered, 1)
        self.assertEqual(self.server.runtimes.stats["wakes_delivered"], 1)
        # 不认识的位置（设备刚放下那一帧的常见情形）：丢掉，但留痕。
        self.assertEqual(self.server.runtimes.deliver_wakes([key(999)]), 0)
        self.assertEqual(
            self.server.runtimes.rotation_view()["locator"]["unrouted"], 1
        )

    def test_drive_once_advances_the_clock_exactly_once(self):
        """时钟只在这个回调里走一步，`tick()` 自己不动它。

        两个方向都要钉住：多推一步会让业务时间跑得比引擎快，少推一步
        （或让 tick 也推）会让「假时钟精确控制时间」这个入口失效。
        """
        self.build_two_networks()
        before = self.server.clock.current_tick()
        self.server.drive_once()
        self.assertEqual(self.server.clock.current_tick(), before + 1)
        self.server.drive_once()
        self.assertEqual(self.server.clock.current_tick(), before + 2)
        self.server.tick()
        self.assertEqual(
            self.server.clock.current_tick(),
            before + 2,
            "直接调 tick() 不推进时钟——离线测试靠它控制时间",
        )

    def test_diagnostics_report_the_clock_and_the_rotation(self):
        self.build_two_networks()
        self.server.clock.advance()
        self.server.tick()
        report = self.server.diagnostics()
        self.assertEqual(report["scheduler"]["clock"]["tick"], 1)
        self.assertIn("rotation", report["scheduler"])
        # 本夹具把调度槽位也换成了记录型双件，它没有可汇总的指标。
        # **它不该被折成 0**：那样「队列长度 0」就既可能是真没有任务，
        # 也可能是这个实现没报（仓库口径：缺项不等于 0）。
        totals = report["scheduler"]["totals"]
        self.assertEqual(totals["networks"], 0)
        self.assertEqual(totals["unreported"], 2)

    def test_a_runtime_visited_twice_in_one_frame_advances_once(self):
        """余量轮会让同一个运行时在一帧里被访问两次。

        主控时钟（去重账本的保留期按它算）与 `stats["ticks"]` 必须按**帧**
        走：走成两倍会把保留窗口压缩一半，重复请求可能提前被遗忘。
        3 台网络 + 预算 64 正好构出「保底 21×3=63，余 1 再访环首」的形状。
        """
        self.build_networks(3)
        # 摆网络本身也走了一帧，因此按增量断言而不是绝对值。
        baseline = dict(
            (runtime.component_id, runtime.tick_count)
            for runtime in self.server.runtimes.runtimes()
        )
        calls_before = self.calls_by_component()
        frames = 6
        for _ in range(frames):
            self.server.clock.advance()
            self.server.tick()
        for runtime in self.server.runtimes.runtimes():
            self.assertEqual(
                runtime.tick_count - baseline[runtime.component_id], frames,
                "主控时钟必须按帧走",
            )
        calls_after = self.calls_by_component()
        visits = max(
            calls_after[key] - calls_before[key] for key in calls_after
        )
        self.assertGreater(
            visits, frames,
            "本用例必须先真的出现过「一帧访问两次」，否则它什么都没测到",
        )

    def test_a_wake_survives_the_same_frame_as_the_placement(self):
        """唤醒投递的时点必须夹在「拓扑重建之后、服务推进之前」。

        放前面会丢掉本帧刚放下的设备——它还没有 lane，而且丢掉是**静默**的
        （只多一个 `unrouted` 计数）。这条用例把那个时点钉住。
        """
        self.server.push(NormalizedEvent.place(key(1, 65), TINY))
        self.server.push(
            NormalizedEvent.place(key(0, 65), "endernet:network_baseband_8")
        )
        # 引擎在本帧内就已经派发了该设备的方块实体 tick。
        self.server.event_binder.wakes.add(
            EventKind.BLOCK_ENTITY_TICK, key(1, 65)
        )
        report = self.server.tick()
        self.assertEqual(report["wakes"], 1, "本帧刚放下的设备也要收得到唤醒")


class TestIdleCheapness(unittest.TestCase):
    """空闲帧确实什么都不做。

    这一层对外的承诺是「空闲网络每帧只做两次比较」——它值得被断言，
    而不是只写在注释里。用**真实装配**（默认工厂，即真正的 `SchedulerService`），
    因为被验的就是它。
    """

    def setUp(self):
        self.server = ServerBootstrap(world=QuietWorld())
        self.server.start(bind_engine=False)
        self.server.push(NormalizedEvent.place(key(1, 65), TINY))
        self.server.push(
            NormalizedEvent.place(key(0, 65), "endernet:network_baseband_8")
        )
        self.server.tick()

    def scheduler(self):
        runtime = self.server.runtimes.runtimes()[0]
        return runtime.services.get(ServiceId.SCHEDULER)

    def test_a_real_scheduler_is_installed_by_default(self):
        """默认装配表里调度槽位已经是正式实现，不是替身。"""
        service = self.scheduler()
        self.assertFalse(service.is_stand_in)
        self.assertTrue(service.is_ready())
        self.assertIsNone(service.power_ok(key(1, 65)))

    def test_idle_frames_are_counted_apart_from_working_ones(self):
        service = self.scheduler()
        before = dict(service.stats)
        for _ in range(10):
            # 既不推时钟也不放事件：没有任何任务到期。
            self.server.tick()
        stats = service.stats
        self.assertEqual(stats["ticks"], before["ticks"], "没干活就不该记「干活」")
        self.assertEqual(stats["idle_ticks"] - before["idle_ticks"], 10)
        self.assertEqual(
            stats["budget_granted"], before["budget_granted"],
            "空闲帧连预算都不该记——记了那句「两次比较」就不成立",
        )
        self.assertEqual(stats["executions"], before["executions"])

    def test_the_manager_can_summarize_the_real_scheduler(self):
        """跨网合计走契约方法，且**真实实现**的指标键要被数出来。

        这条同时是那道守卫：谁把 `diagnostics()` 里的 `queued`/`lanes`
        改了名，这里立刻会从 1 掉到 0，而不是在验收时悄悄报一份全 0 的
        「一切正常」。
        """
        report = self.server.runtimes.scheduler_view()
        self.assertEqual(report["networks"], 1)
        self.assertEqual(report["unreported"], 0)
        # 夹具摆了两台设备（控制器 + 基带），因此两条 lane。
        self.assertEqual(report["lanes"], 2, "指标键改了名就会掉成 0")

    def test_the_clock_is_shared_by_both_consumers(self):
        """跨网轮转与调度服务读的是**同一份** `SchedulerPolicy`。

        各存一份的代价是 `unresolved_report()` 报的数字与真正生效的不是
        同一个——那正是全局约束 7 要防的「用替身值悄悄定案」。
        """
        self.assertIs(
            self.server.runtimes.scheduler_policy, self.server.scheduler_policy
        )
        service = self.scheduler()
        self.assertIs(service.policy, self.server.scheduler_policy)
        # 改一处，两边的报告都跟着变。
        self.server.scheduler_policy.min_share_floor = 3
        self.assertEqual(
            self.scheduler().policy.describe()["min_share_floor"], 3
        )


class SteppingTimer(object):
    """每次调用前进固定步长的假计时器，让时长断言精确到整数纳秒。"""

    def __init__(self, step_ns=1000000, start=100.0):
        # type: (int, float) -> None
        self.value = float(start)
        self.step = step_ns / 1000000000.0

    def __call__(self):
        self.value += self.step
        return self.value


class FakeServices(object):
    """只回答「这个槽位装了吗」。"""

    def __init__(self, ids=()):
        # type: (tuple) -> None
        self._ids = frozenset(ids)

    def get(self, service_id):
        # type: (str) -> object
        return object() if service_id in self._ids else None


class FakeRuntime(object):
    """`SchedulerService` 实际用到的东西只有这几样。"""

    def __init__(self, registry, component_id="c0", service_ids=()):
        # type: (object, str, tuple) -> None
        self.registry = registry
        self.component_id = component_id
        self.services = FakeServices(service_ids)
        self.stats = {}

    def member_keys(self):
        # type: () -> tuple
        return ()


class SchedulerServiceFixture(unittest.TestCase):
    """把 `SchedulerService` 单独拿出来验：它不依赖运行时的其余部分。"""

    def setUp(self):
        self.definitions = DefinitionSet()
        self.registry = DeviceRegistry(self.definitions)
        self.runtime = FakeRuntime(
            self.registry, service_ids=(ServiceId.STORAGE,)
        )
        self.service = SchedulerService(self.runtime)

    def mount(self, node_key=None, device_id=TINY):
        """注册一台设备并挂进调度器，返回它的节点。"""
        node_key = node_key if node_key is not None else key(1)
        self.registry.register(node_key, device_id)
        node = NetworkNode.from_record(
            self.definitions, self.registry.get(node_key)
        )
        self.service.add_node(node)
        return node

    def tick(self, now, budget=64):
        return self.service.tick({"tick": now, "budget": budget}, budget)

    # -- 契约 ----------------------------------------------------------

    def test_it_does_not_answer_power_ok(self):
        """能源是 04 的另一半。给 True 或 False 都是撒谎，`None` 才诚实。"""
        node = self.mount()
        self.assertIsNone(self.service.power_ok(node.ref.node_key))
        self.assertIsNone(self.service.power_ok(node.node_key))

    def test_it_does_not_pretend_to_handle_commands(self):
        """命令路由表里没有调度器这一格；真调到这里就是编程错误。"""
        with self.assertRaises(NotImplementedError):
            self.service.handle(None, None)

    def test_it_is_a_ready_non_stand_in(self):
        self.assertTrue(self.service.is_ready())
        self.assertFalse(self.service.is_stand_in)
        self.assertEqual(self.service.pending_task, "")

    def test_it_stores_nothing(self):
        """到期时刻是帧序，跨重启没有意义——不留半个假实现。"""
        node = self.mount()
        self.assertIsNone(self.service.save_node_data(node))

    def test_it_does_not_care_about_load_domains(self):
        node = self.mount()
        self.assertFalse(
            self.service.note_domain_change(node, ("d0",), ("d1",))
        )

    # -- 成员与幂等 ----------------------------------------------------

    def test_mounting_is_idempotent(self):
        node = self.mount()
        self.assertFalse(
            self.service.add_node(node), "重复广播不该再算一次挂载"
        )
        self.assertEqual(self.service.lane_count(), 1)
        # 一台设备一条轮询任务，重复挂载不会多出一条。
        self.assertEqual(len(self.service.queue_view()), 1)

    def test_a_new_lane_has_no_consumer_yet(self):
        """没有消费者是个**被记录的事实**，不是被隐藏的空白。"""
        self.mount()
        self.assertFalse(self.service.lane_view()[0]["has_consumer"])
        self.assertEqual(self.service.diagnostics()["poll_handlers"], 0)

    def test_poll_handler_must_belong_to_this_network(self):
        node = self.mount()
        self.assertFalse(
            self.service.set_poll_handler(node.ref, lambda task: None, owner="nope")
        )
        self.assertEqual(self.service.stats["rejected_owner"], 1)
        self.assertTrue(
            self.service.set_poll_handler(
                node.ref, lambda task: None, owner=ServiceId.STORAGE
            )
        )
        self.assertTrue(self.service.lane_view()[0]["has_consumer"])

    def test_registering_a_consumer_resets_the_backoff(self):
        """新消费者不该等「没人看」期间攒下的最疏间隔。"""
        node = self.mount_with_fast_backoff()
        for now in (0, 2, 6, 14):        # 无人消费 → 一路退避到上限
            self.tick(now)
        self.assertEqual(self.service.lane_view()[0]["backoff"]["steps"], 4)
        self.assertTrue(
            self.service.set_poll_handler(
                node.ref, lambda task: (False, 0), owner=ServiceId.STORAGE
            )
        )
        lane = self.service.lane_view()[0]
        self.assertEqual(lane["backoff"]["steps"], 0)
        self.assertEqual(lane["poll_due"], 14 + 1, "按初值重新排一次")

    def test_scheduling_for_a_foreign_owner_is_rejected(self):
        node = self.mount()
        self.assertIsNone(
            self.service.schedule(node.ref, "x:y", owner="not_a_service")
        )
        self.assertEqual(self.service.stats["rejected_owner"], 1)

    def test_unmounting_drops_the_lane_and_its_tasks(self):
        node = self.mount()
        self.service.schedule(node.ref, "test:work", handler=lambda task: 1)
        self.assertTrue(self.service.remove_node(node))
        self.assertFalse(self.service.remove_node(node))
        self.assertEqual(self.service.lane_count(), 0)
        self.assertEqual(len(self.service.queue_view()), 0)

    # -- 重排与失效 ----------------------------------------------------

    def test_rescheduling_replaces_the_same_task(self):
        """同一 `(ref, kind)` 只有一条活体——不要求调用方先 cancel。"""
        node = self.mount()
        calls = []
        self.service.schedule(node.ref, "test:work", delay=0,
                              handler=lambda task: calls.append(task) or 1)
        second = self.service.schedule(node.ref, "test:work", delay=0,
                                       handler=lambda task: calls.append(task) or 1)
        self.tick(0)
        self.assertEqual(len(calls), 1, "重排不是加一条，是换掉那一条")
        self.assertEqual(self.service.stats["replaced_total"], 1)
        self.assertEqual(second.runs, 1)
        # 活体表里已经没有这个 key 的一次性任务了（执行后就地摘除）。
        self.assertNotIn(
            TaskKey.of(node.ref, "test:work"),
            [task["key"]["kind"] for task in self.service.queue_view()],
        )

    def replace_at(self, node, device_id=DRIVER):
        """在同一位置换一个**型号**（同型号重复注册只是 DUPLICATE，
        代次不会变，因此构不出「替换」）。"""
        outcome = self.registry.register(node.node_key, device_id)
        self.assertEqual(outcome.kind, "replaced")
        replacement = NetworkNode.from_record(
            self.definitions, self.registry.get(node.node_key)
        )
        self.assertNotEqual(replacement.ref, node.ref)
        return replacement

    def test_a_replaced_device_invalidates_the_old_tasks(self):
        """同位置换了方块：旧代次的任务**永不执行**。"""
        node = self.mount()
        calls = []
        self.service.schedule(node.ref, "test:work", delay=0,
                              handler=lambda task: calls.append(task) or 1)
        replacement = self.replace_at(node)
        self.assertTrue(self.service.add_node(replacement))
        self.assertEqual(self.service.stats["replaced_total"], 1)
        self.tick(0)
        self.assertEqual(calls, [], "旧实例的任务不该在新方块上执行")
        self.assertEqual(self.service.lane_count(), 1)
        self.assertEqual(self.service.lane_view()[0]["device_id"], DRIVER)

    def test_a_stale_removal_does_not_wipe_the_live_lane(self):
        """迟到的卸载通知不能把新实例的 lane 一起删掉。"""
        node = self.mount()
        replacement = self.replace_at(node)
        self.service.add_node(replacement)
        # 拿旧节点来卸载：应当被认出来，只清它自己那个代次。
        self.assertFalse(self.service.remove_node(node))
        self.assertEqual(self.service.lane_count(), 1)
        self.assertEqual(self.service.lane_view()[0]["device_id"], DRIVER)

    def test_unloaded_device_freezes_then_resumes_without_catch_up(self):
        """「不集中补做无限历史 tick」在这里是可判定的。

        冻结期间按**任务自己的周期**（5）重新计时，因此解除冻结后的下一个
        到期时刻是 105 而不是「追赶」到 100——时间线是写死的，不依赖任何
        策略默认值。
        """
        node = self.mount()
        counter = []
        self.service.schedule(node.ref, "test:work", period=5,
                              handler=lambda task: counter.append(task) or 1)
        self.tick(0)
        self.assertEqual(len(counter), 1)

        self.registry.mark_unloaded(node.node_key)      # 卸载不是拆除
        self.tick(100)
        self.assertEqual(len(counter), 1, "冻结期间不得执行")
        self.assertGreaterEqual(self.service.stats["deferred_unloaded"], 1)
        # 补齐窗口内多次推进，一条都不能补做。
        self.tick(101)
        self.tick(102)
        self.assertEqual(len(counter), 1)

        self.registry.mark_loaded(node.node_key)
        self.tick(105)                                   # 到点才恢复执行
        self.assertEqual(len(counter), 2)
        self.assertEqual(self.service.queue_view()[0]["deferrals"], 1)

    def test_invalidate_on_unload_is_a_named_policy_point(self):
        """两种读法都保留成配置点，不在这里替玩法定案。"""
        self.service.policy = SchedulerPolicy(invalidate_on_unload=True)
        node = self.mount()
        calls = []
        self.service.schedule(node.ref, "test:work", period=5,
                              handler=lambda task: calls.append(task) or 1)
        self.registry.mark_unloaded(node.node_key)
        self.tick(100)
        self.assertEqual(calls, [], "作废口径下连一次都不该执行")
        self.assertGreaterEqual(self.service.stats["stale_dropped"], 1)

    # -- 预算与退避 ----------------------------------------------------

    def test_the_frame_budget_bounds_executions(self):
        node = self.mount()
        calls = []
        for index in range(10):
            self.service.schedule(
                node.ref, "test:work%d" % (index,),
                handler=lambda task: calls.append(task) or 1,
            )
        spent = self.service.tick({"tick": 0, "budget": 3}, 3)
        self.assertEqual(len(calls), 3, "单帧执行数受预算约束")
        self.assertEqual(spent, 3)
        diagnostics = self.service.diagnostics()
        self.assertEqual(diagnostics["stats"]["deferred_now"], 7)
        self.assertEqual(diagnostics["queue"]["queued"], 8, "7 条待办 + 1 条轮询")
        # 下一帧继续推进，不会把积压一次做完。
        self.service.tick({"tick": 0, "budget": 3}, 3)
        self.assertEqual(len(calls), 6)

    def test_a_zero_cost_task_cannot_spin_the_frame(self):
        """预算按 handler 自报的工作量计，恒 0 又重排会自旋——上限兜住它。"""
        self.service.policy = SchedulerPolicy(max_tasks_per_frame=4)
        node = self.mount()
        runs = []
        self.service.schedule(node.ref, "test:work", period=1,
                              handler=lambda task: runs.append(task) or 0)
        self.service.tick({"tick": 0, "budget": 64}, 64)
        self.assertLessEqual(len(runs), 4 + 1, "必须有执行条数上限")

    def test_a_handler_that_raises_does_not_lose_the_poll_timer(self):
        """异常不能让定时器消失。

        `pop_due` 已经把任务从活体表里拿走了；若 handler 抛异常时跳过重排，
        那台设备就**再也不被轮询**——而异常只在 `drive_once` 里记进
        `tick_errors`，没人会把它和「轮询停了」联系起来。
        """
        node = self.mount()
        self.service.schedule(
            node.ref, "test:boom",
            handler=lambda task: (_ for _ in ()).throw(ValueError("炸了")),
        )
        with self.assertRaises(ValueError):
            self.tick(0)
        kinds = [item["key"]["kind"] for item in self.service.queue_view()]
        self.assertIn(
            TaskKind.POLL, kinds, "抛异常之后轮询任务必须还在队列里"
        )
        # 而且下一次真的还能到期（无消费者时退避到 base×2 = 40）。
        self.tick(40)
        self.assertGreaterEqual(self.service.stats["poll_executions"], 2)

    def test_a_lane_without_a_consumer_backs_off(self):
        """没有消费者时应当**逐步退避**，而不是每 `base` 帧白扫一次。

        本增量 100% 的 lane 都是这种状态，因此这正是「空闲网络不高频扫描
        设备」在真实装配下的行为。
        """
        self.service.policy = SchedulerPolicy(
            backoff_base_ticks=1, backoff_max_ticks=8, backoff_factor=2
        )
        self.mount()
        intervals = []
        now = 0
        for _ in range(4):
            self.tick(now)
            due = self.service.lane_view()[0]["poll_due"]
            intervals.append(due - now)
            now = due
        self.assertEqual(intervals, [2, 4, 8, 8], "无人消费时也要退避到上限")

    def test_created_tick_is_the_creation_frame_not_the_due_frame(self):
        """`created_tick` 与 `due` 的差是「等了多久」，方向不能反。"""
        node = self.mount()
        self.tick(500)
        task = self.service.schedule(node.ref, "test:work", delay=100)
        self.assertEqual(task.created_tick, 500)
        self.assertEqual(task.due, 600)

    def test_a_negative_cost_is_clamped_and_counted(self):
        node = self.mount()
        self.service.schedule(node.ref, "test:work", handler=lambda task: -5)
        spent = self.tick(0)
        self.assertEqual(self.service.stats["negative_costs"], 1)
        self.assertEqual(spent, 0, "负的消耗量不该让本帧多出预算")
        self.assertEqual(
            self.service.queue_view()[0]["key"]["kind"], TaskKind.POLL,
            "一次性任务执行完就地摘除，只剩轮询",
        )

    def mount_with_fast_backoff(self):
        """退避调小到可读的档位，让用例可以精确落在到期时刻。

        默认值（base 20 / max 200）是给真机用的测试替身，断言里写死它
        等于把替身数值当成了契约。
        """
        self.service.policy = SchedulerPolicy(
            backoff_base_ticks=1, backoff_max_ticks=8, backoff_factor=2
        )
        return self.mount()

    def test_the_poll_interval_doubles_until_it_hits_the_ceiling(self):
        node = self.mount_with_fast_backoff()
        self.service.set_poll_handler(
            node.ref, lambda task: (False, 0), owner=ServiceId.STORAGE
        )
        intervals = []
        now = 0
        for _ in range(4):
            self.tick(now)
            due = self.service.lane_view()[0]["poll_due"]
            intervals.append(due - now)
            now = due
        # 注册消费者时按初值重排了一次（见 `set_poll_handler`），因此第一档
        # 就是 base=1，之后翻倍到上限。
        self.assertEqual(intervals, [1, 2, 4, 8], "翻倍到上限后不再变大")

    def test_a_change_resets_the_backoff(self):
        """还在变的容器值得继续低频探测；已经静止的不该每帧问一次。"""
        node = self.mount_with_fast_backoff()
        state = {"changed": False}
        self.service.set_poll_handler(
            node.ref,
            lambda task: (state["changed"], 0),
            owner=ServiceId.STORAGE,
        )
        # 注册消费者时按初值重排：due = 0 + 1。
        self.tick(1)
        self.tick(3)                      # 1 + 2
        self.assertEqual(self.service.lane_view()[0]["backoff"]["steps"], 2)
        state["changed"] = True
        self.tick(7)                      # 3 + 4
        lane = self.service.lane_view()[0]
        self.assertEqual(lane["backoff"]["steps"], 0)
        self.assertEqual(lane["poll_due"], 8, "下一轮回到初值间隔")

    def test_wake_pulls_the_poll_forward(self):
        node = self.mount_with_fast_backoff()
        self.tick(0)
        # 首次轮询之后退避已经走了一档（无消费者），下一次排在 0+2。
        self.assertEqual(self.service.lane_view()[0]["poll_due"], 2)
        self.assertTrue(self.service.wake(node.ref))
        self.assertEqual(self.service.lane_view()[0]["poll_due"], 0)
        # 同一帧就能被看见——这是唤醒的意义所在。
        self.tick(0)
        self.assertEqual(self.service.stats["poll_executions"], 2)

    def test_waking_a_foreign_or_unknown_ref_is_refused(self):
        node = self.mount()
        self.assertFalse(self.service.wake(ref(999)))
        self.assertFalse(self.service.wake(ref(1, generation=99)))

    def test_block_entity_tick_keeps_the_backoff_from_growing(self):
        """它**不**把轮询提前——那是 `wake()` 的活。

        引擎每帧都在心跳（开 tick 的设备约 20 Hz），拿心跳当「变化」去立刻
        轮询，开 tick 的设备就退化成每帧一次扫描。
        """
        node = self.mount_with_fast_backoff()
        self.service.set_poll_handler(
            node.ref, lambda task: (False, 0), owner=ServiceId.STORAGE
        )
        self.tick(0)
        self.tick(2)
        before = self.service.lane_view()[0]["poll_due"]
        self.assertGreater(self.service.lane_view()[0]["backoff"]["steps"], 0)

        self.assertTrue(self.service.on_wake(node.node_key))
        self.assertEqual(self.service.stats["wakes"], 1)
        lane = self.service.lane_view()[0]
        self.assertEqual(lane["backoff"]["steps"], 0, "还在心跳就不该退到最疏")
        self.assertEqual(lane["poll_due"], before, "但不把轮询提前到本帧")

    def test_sleep_cancels_work_and_slows_the_poll_to_the_ceiling(self):
        node = self.mount()
        calls = []
        self.service.schedule(node.ref, "test:work", handler=lambda task: calls.append(task) or 1)
        self.assertTrue(self.service.sleep(node.ref))
        lane = self.service.lane_view()[0]
        self.assertEqual(lane["poll_due"], lane["backoff"]["max_ticks"])
        self.tick(1000)
        self.assertEqual(calls, [], "睡着之后业务任务不再执行")

    # -- 诊断 ----------------------------------------------------------

    def test_status_stays_small(self):
        self.mount()
        state = self.service.status()
        self.assertEqual(state["service_id"], ServiceId.SCHEDULER)
        self.assertEqual(state["lanes"], 1)
        self.assertEqual(state["queued"], 1)
        self.assertNotIn("stats", state, "status 是给界面看的，别把全量统计塞进去")

    def test_diagnostics_carry_the_metrics_04_07_asks_for(self):
        self.mount()
        self.tick(0)
        report = self.service.diagnostics()
        for field in (
            "queued_peak", "scheduled_total", "replaced_total", "cancelled_total",
            "deferred_total", "deferred_unloaded", "budget_spent",
            "executions", "poll_executions", "wakes", "duration_ns_total",
        ):
            self.assertIn(field, report["stats"], field)
        self.assertIn("sleep_permille", report)
        self.assertIn("unresolved", report)
        self.assertEqual(
            len(report["unresolved"]), len(UNRESOLVED_FIELDS)
        )

    def test_duration_is_measured_only_when_a_timer_is_injected(self):
        """离线默认不测时长：确定性比多一个诊断数字重要。"""
        node = self.mount()
        self.service.schedule(node.ref, "test:work", handler=lambda task: 1)
        self.tick(0)
        self.assertEqual(self.service.stats["duration_ns_total"], 0)
        self.assertEqual(self.service.stats["duration_samples"], 0)
        self.assertGreater(
            self.service.stats["duration_unsampled"], 0,
            "没计时也要说清楚「有几笔没测」",
        )

        self.service.timer = SteppingTimer(step_ns=1000000)
        self.service.schedule(node.ref, "test:work2", handler=lambda task: 1)
        self.tick(1)
        stats = self.service.stats
        self.assertGreaterEqual(stats["duration_samples"], 1)
        # 每次计时恰好 1ms，因此总量必然是样本数的整数倍——这条断言把
        # 「时长是诊断量、不参与判定」也钉住了。
        self.assertEqual(
            stats["duration_ns_total"], 1000000 * stats["duration_samples"]
        )


if __name__ == "__main__":
    unittest.main()
