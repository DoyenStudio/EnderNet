# coding=utf-8
"""「一次按下」：右键按住时引擎每个游戏刻派一条使用事件，这里把它们归成一次按下。

**不 import 引擎**，离线可测。时钟是客户端帧（`FrameClock`，由引擎的 `OnScriptTickClient`
推，每秒 30 帧）；事件按游戏刻（每秒 20 刻）来，1 个游戏刻 = 1.5 帧，所以「相邻两条隔不超过
1 个游戏刻」在帧上是「≤ 2 帧」（`HOLD_GAP_FRAMES`），隔 2 个游戏刻是 3 帧、算新的一次。

## 判定（作者 2026-11-10：去掉秒级冷却，只按「一次按下」去重）

- **一次按下** = 一串相邻间隔不超过 `gap` 帧的使用事件（方块使用、物品使用都算，不论目标、
  手持物品、是否潜行、我们接不接管）。松手（断开超过 `gap` 帧）后再来的一条是新的一次；
- **接管**：这次按下里只要有一条被我们接管、发出了意图（`take`），直到这次按下结束，后面
  **所有**使用事件都拦掉原版交互、不再发意图（`owned`）。否则第一下把面板底座换成光缆后，
  玩家还按着右键，后续事件点在新光缆上，「手持光缆点光缆」原版照常，就会多放一格；
- 还没接管的按下，后面的事件照样逐条判定：方块使用事件不带面，普通方块要等同一刻随后的
  物品事件（带面）才接得下来。
"""

#: 客户端时钟（`OnScriptTickClient`）每秒几帧；游戏刻每秒几刻
SCRIPT_FRAMES_PER_SECOND = 30
GAME_TICKS_PER_SECOND = 20
#: 同一次按下里相邻两条事件最多隔几个游戏刻（作者 2026-11-10：≤ 1）
HOLD_GAP_GAME_TICKS = 1
#: 换算成帧（向上取整）：1 个游戏刻 = 1.5 帧 → 2 帧
HOLD_GAP_FRAMES = (
    HOLD_GAP_GAME_TICKS * SCRIPT_FRAMES_PER_SECOND + GAME_TICKS_PER_SECOND - 1
) // GAME_TICKS_PER_SECOND


class FrameClock(object):
    """客户端帧计数：每条 `OnScriptTickClient` 加一。"""

    def __init__(self):
        self.frame = 0

    def tick(self, _event=None):
        self.frame += 1

    def now(self):
        # type: () -> int
        return self.frame


class PressTracker(object):
    """把使用事件归成一次次按下，并记这次按下有没有被接管。

    `press(帧)`：每条使用事件都要报（不论接不接管），返回它是否开始了**新的**一次按下；
    `take()`：这次按下被接管了；`owned`：这次按下是否已被接管（新的一次按下清零）。
    时钟倒退（重启、换存档）也算新的一次。
    """

    def __init__(self, gap=HOLD_GAP_FRAMES):
        # type: (int) -> None
        self.gap = int(gap)
        self._frame = None
        self._owned = False

    def press(self, frame):
        # type: (int) -> bool
        held = self._frame is not None and 0 <= frame - self._frame <= self.gap
        self._frame = frame
        if not held:
            self._owned = False
        return not held

    def take(self):
        self._owned = True

    @property
    def owned(self):
        # type: () -> bool
        return self._owned

    def reset(self):
        self._frame = None
        self._owned = False
