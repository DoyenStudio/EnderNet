# coding=utf-8
"""按玩家限速，以及它在「右键终端方块打开界面」那条路上的接线。

限速器的正确性**在界面上看不出来**：被挡下的那一下什么都不发生，
与「玩家没点」长得一模一样。因此这里的用例对着的都是具体的错法——
把不限速读成全挡掉、把窗口按全局而不是按玩家记、或者干脆忘了接上去。

下半部分（`TestBlockUseRateLimit`）补的是另一件事：`_on_block_use`
在这之前**一条用例都没有**。它是 11-03 的打开入口，而离线链路里
引擎事件是喂进来的替身，所以「右键一下到底推没推 UI」此前没人钉过。
"""

import unittest
from unittest.mock import patch

import testsupport  # noqa: F401  设置 sys.path

from endernet.common import rate_limit


class _Clock(object):
    """可推进的假时钟。**不要在用例里 `sleep`**：0.2 秒的窗口乘上几十条
    用例就是几秒的墙钟，而那种慢会被读成「测试本来就慢」。"""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _FakeTime(object):
    """只替 `rate_limit` 模块里的 `time` **这一个引用**。

    **不要直接改 `rate_limit.time.time`**：`rate_limit.time` 就是 stdlib 的
    `time` 模块，改它等于把整个进程的 `time.time()` 都换成假时钟——别的
    模块在同一个用例里读时间就会读到 1000.0，而那种故障看起来毫不相干。
    """

    def __init__(self, clock):
        self.time = clock


class _FakeClockFixture(object):
    def install_clock(self):
        self.clock = _Clock()
        patcher = patch.object(rate_limit, "time", _FakeTime(self.clock))
        patcher.start()
        self.addCleanup(patcher.stop)


class RateLimiterTests(unittest.TestCase, _FakeClockFixture):
    def setUp(self):
        self.install_clock()
        self.limiter = rate_limit.PlayerRateLimiter(0.2)

    def test_the_first_click_is_always_allowed(self):
        self.assertTrue(self.limiter.allow("p1"))

    def test_a_second_click_inside_the_window_is_refused(self):
        self.assertTrue(self.limiter.allow("p1"))
        self.clock.advance(0.19)
        self.assertFalse(self.limiter.allow("p1"))

    def test_the_click_that_lands_exactly_on_the_window_is_allowed(self):
        """边界取**闭区间外侧**：恰好等于窗口算放行。

        这一条要钉的是 `delta < interval` 与 `delta <= interval` 的区别——
        写反了的表现是「窗口永远是 0.2 秒 + 一点点」，肉眼看不出，只在
        「按住不放到刚好卡点」时偶尔吞掉一次。
        """
        self.assertTrue(self.limiter.allow("p1"))
        self.clock.advance(0.2)
        self.assertTrue(self.limiter.allow("p1"))

    def test_the_window_is_per_player_not_global(self):
        """**按人记**。记成全局的话，服务器上只要有一个人在连点，所有人
        都打不开终端——而界面上表现为「有时候点了没反应」。"""
        self.assertTrue(self.limiter.allow("p1"))
        self.assertTrue(self.limiter.allow("p2"))
        self.assertFalse(self.limiter.allow("p1"))
        self.assertFalse(self.limiter.allow("p2"))

    def test_an_unknown_player_is_never_allowed(self):
        """身份不明时**不放行**：不知道是谁就没法按人限速，放行等于把限速
        悄悄关掉。"""
        for bad in ("", None):
            self.assertFalse(self.limiter.allow(bad))

    def test_a_zero_or_negative_window_means_no_limit_at_all(self):
        """**`<= 0` 读作「不限速」，不是「全挡掉」。** 两种读法会给出相反的
        默认，而这里的取舍是：没配这个值就该什么都不挡。"""
        for interval in (0, -1, 0.0):
            limiter = rate_limit.PlayerRateLimiter(interval)
            for _ in range(5):
                self.assertTrue(limiter.allow("p1"))

    def test_forget_lets_the_next_click_through_immediately(self):
        """忘掉 = 下次立即放行。**不是「继续挡着」**：玩家退出后那个记录
        没有留着的理由，而留成「挡着」会在重连后白吞一次点击。"""
        self.assertTrue(self.limiter.allow("p1"))
        self.limiter.forget("p1")
        self.assertTrue(self.limiter.allow("p1"))

    def test_stale_entries_are_reclaimed(self):
        """**字典不能无界增长。** 上游那版靠 `ExecLater` 挂定时器逐个删；
        移植时丢掉回收，就会每个右键过的 ID 留一条永不释放的记录。"""
        for index in range(50):
            self.assertTrue(self.limiter.allow("p%d" % (index,)))
        self.assertEqual(self.limiter.diagnostics()["tracked"], 50)
        self.clock.advance(1.0)
        # 任何一次放行都会顺手清掉出窗口的条目。
        self.assertTrue(self.limiter.allow("fresh"))
        self.assertEqual(self.limiter.diagnostics()["tracked"], 1)

    def test_diagnostics_report_the_window(self):
        report = self.limiter.diagnostics()
        self.assertEqual(report["interval_seconds"], 0.2)
        self.assertEqual(report["tracked"], 0)


# -- 接线：右键终端方块 ------------------------------------------------


class _FakeRef(object):
    def to_dict(self):
        return {"dimension": 0, "position": [1, 2, 3], "generation": 1}


class _FakeRecord(object):
    ref = _FakeRef()


class _FakeRegistry(object):
    """只回答「这个位置有没有设备」。**够 `_on_block_use` 用就行**——
    多实现一分就多一分与真注册表分家的机会。"""

    def __init__(self, known=True):
        self.known = known
        self.asked = []

    def get(self, node_key):
        self.asked.append(node_key)
        return _FakeRecord() if self.known else None


class _FakeEvent(object):
    def __init__(self, block_name, player_id="p1"):
        self.blockName = block_name
        self.playerId = player_id
        self.dimensionId = 0
        self.x = 1
        self.y = 2
        self.z = 3


class _FakeQueue(object):
    def push(self, event):
        pass


class BlockUseFixture(unittest.TestCase, _FakeClockFixture):
    BLOCK = "endernet:common_terminal_panel"

    def setUp(self):
        from endernet.server.adapters.events import ServerEventBinder

        self.install_clock()
        self.registry = _FakeRegistry()
        self.binder = ServerEventBinder(
            _FakeQueue(), None, self.registry, definitions=None
        )
        #: `_notify_ui` 真的推包要走引擎事件类（导入在方法体内）。这里换掉它，
        #: 只数「推了几次」——本文件要验的是限速，不是 `PushUIRequest` 本身。
        self.pushed = []
        self.binder._notify_ui = (
            lambda player_id, ui_key, params: self.pushed.append((player_id, ui_key))
        )

    def click(self, player_id="p1", block=None):
        self.binder._on_block_use(_FakeEvent(block or self.BLOCK, player_id))

    def test_a_single_click_pushes_the_ui(self):
        self.click()
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.pushed[0], ("p1", "common_terminal_panel_ui.main"))
        self.assertEqual(self.binder.counts["block_use_opened"], 1)

    def test_a_burst_of_clicks_pushes_only_once(self):
        """**这一条就是这次改动的理由。** 引擎的方块使用回调不节流，连点几下
        就是几条 `PushUIRequest`，客户端会把同一次打开做几遍。"""
        for _ in range(5):
            self.click()
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.binder.counts["block_use"], 5)
        self.assertEqual(self.binder.counts["block_use_opened"], 1)
        # 被挡下的那几下单独计数：不记的话诊断里只看得到 `block_use` 涨、
        # `block_use_opened` 不涨，分不清是限速还是别的原因。
        self.assertEqual(self.binder.counts["block_use_limited"], 4)

    def test_the_limit_is_per_player(self):
        self.click("p1")
        self.click("p2")
        self.assertEqual(len(self.pushed), 2)

    def test_after_the_window_the_next_click_goes_through(self):
        self.click()
        self.clock.advance(0.2)
        self.click()
        self.assertEqual(len(self.pushed), 2)

    def test_an_unmapped_block_is_not_counted_as_a_limited_click(self):
        """右键别的方块是正常现象，**不该消耗窗口**，也不该进限速计数——
        否则「站着连点脚下草方块」会给诊断刷出满屏的 `block_use_limited`。"""
        for _ in range(3):
            self.click(block="minecraft:dirt")
        self.assertEqual(len(self.pushed), 0)
        self.assertNotIn("block_use_limited", self.binder.counts)
        # 窗口没被消耗：紧接着右键真方块照样开。
        self.click()
        self.assertEqual(len(self.pushed), 1)

    def test_an_unknown_position_does_not_consume_the_window(self):
        """方块刚加载、注册表里还没有它时那条路本来就什么都不做。

        拿它去消耗窗口，表现是「走过去连点两下，第二下被吞」——而玩家
        根本没做错什么。
        """
        self.registry.known = False
        self.click()
        self.assertEqual(len(self.pushed), 0)
        self.assertEqual(self.binder.counts["block_use_unknown"], 1)
        # 设备随后被发现了：紧接着那一下必须能开。
        self.registry.known = True
        self.click()
        self.assertEqual(len(self.pushed), 1)


if __name__ == "__main__":
    unittest.main()
