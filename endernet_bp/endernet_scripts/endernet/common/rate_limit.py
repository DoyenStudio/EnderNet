# coding=utf-8
"""按玩家限速：一个窗口内只放行一次。

给「右键终端方块打开界面」这类入口用——它**由玩家动作触发、又有可见的
副作用**（客户端真的会把界面开起来）。引擎的方块使用回调本身不节流，
连点几下就是几条 `PushUIRequest`，客户端那边会把同一次打开做几遍。

**移植自 skybluetech 的 `tooldelta/extensions/rate_limiter.py`**（2026-09-30
由 User 指定），三处刻意的改动：

1. **不依赖 `ExecLater`，因此不 import 引擎。** 上游那版每记一次就挂一个
   定时器，到点把条目删掉；这里改成记时间戳、在读取时判过期、顺手回收。
   对外行为相同（窗口内为真、窗口过后自动为假），但不引入引擎依赖——
   本模块因此能放在 `common/`（那一层不许 import tooldelta），也能离线测。
2. **放在本仓自己的树里，不放进 `tooldelta/`。** 那是个 git 子模块：
   往里写的文件父仓跟踪不到，`git submodule update` 或重新克隆之后会
   **静默消失**（不是报错，是没了）。而且 `extensions/` 根本不在上游
   `origin/main` 上——它在 skybluetech 自己的分支里。搬进来顺带把这份
   依赖关系写明白。
3. **只留 `allow()` 一个判定口。** 上游的 `record()` 同时兼做「登记」与
   「查询」，签名返回 bool 但看不出是哪件事；调用方要的其实只有
   「这一下放不放行」。

**它不是一个通用限流器**：没有令牌桶、没有按接口分桶、窗口内容量恒为 1。
够用就好——多一个可调参数就多一个没人定的玩法数值（全局约束 7）。
"""

import time

#: 「从没放过行」时 `delta_since` 的返回值。取正无穷，任何窗口都比它小，
#: 因此不必为这个分支单写一条判据。
_NEVER = float("inf")


class PlayerRateLimiter(object):
    """按 `player_id` 记时间戳的限速器。**窗口内只放行一次。**

    `interval_seconds <= 0` 表示**不限速**——那是有意的：没配这个值就该是
    「不挡」，而不是「全挡掉」。反过来要是取 `0` 当「窗口是 0 秒所以永远
    放行」，两种读法会给出相反的默认，所以这里只认前者。
    """

    def __init__(self, interval_seconds=0.0):
        # type: (float) -> None
        self.interval_seconds = interval_seconds
        #: `player_id -> 上次放行的时间戳`
        self._last = {}

    def allow(self, player_id):
        # type: (str) -> bool
        """这一下放不放行。放行则记下当前时间，否则原样返回 `False`。

        **身份不明时不放行。** 不知道是谁就没法按人限速，而放行等于把限速
        悄悄关掉——宁可什么都不做。调用方（`_notify_ui`）本来就先拒空
        `player_id`，这里只是不让「上游少了一道检查」变成「限速失效」。
        """
        if not player_id:
            return False
        now = time.time()
        self._prune(now)
        if self.delta_since(player_id) < self.interval_seconds:
            return False
        self._last[player_id] = now
        return True

    def delta_since(self, player_id):
        # type: (str) -> float
        """距上次放行过了多久；从没放过行时返回正无穷。"""
        last = self._last.get(player_id)
        if last is None:
            return _NEVER
        return time.time() - last

    def forget(self, player_id):
        # type: (str) -> None
        """忘掉一个玩家。**忘掉等于下次立即放行**，不是「继续挡着」。

        玩家退出时调用。真正的回收靠 `_prune`（那也是上游用 `ExecLater`
        想达到的效果），这个口只用于「这个人已经不在了，别留记录」。
        """
        self._last.pop(player_id, None)

    def _prune(self, now):
        # type: (float) -> None
        """丢掉已经出窗口的条目。

        上游靠 `ExecLater` 给每次放行挂一个定时器逐个删。这里在每次放行时
        顺手清一遍：代价是 O(在场玩家数)，而那个数就是「窗口内点过的玩家」
        ——恒小。**不回收的话字典会随玩家总数无界增长**（每个右键过的 ID
        一条永不释放的记录），那是上游特意用定时器避开的事，不能移植时丢掉。
        """
        deadline = now - self.interval_seconds
        stale = [pid for pid, stamp in self._last.items() if stamp < deadline]
        for pid in stale:
            del self._last[pid]

    def diagnostics(self):
        # type: () -> dict
        return {
            "interval_seconds": self.interval_seconds,
            "tracked": len(self._last),
        }
