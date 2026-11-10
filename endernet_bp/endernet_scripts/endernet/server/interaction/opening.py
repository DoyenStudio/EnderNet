# coding=utf-8
"""右键开界面的唯一入口（2026-11-10 收拢）：整方块终端与面板终端走同一条路。**不 import 引擎**。

此前两条路各写一套：整方块终端在服务端 `ServerBlockUseEvent` 里开、`UseGuard` 拦掉手持物品
那一半；面板终端由客户端接管「空手点光缆」发意图再开——手持方块点面板时客户端不接管，原版
照常把方块放下（2026-11-10 真机反馈）。现在只剩一条：

1. 服务端两条原版事件都带被点的方块与格内命中点，适配器
   （`server/adapters/events.py::ServerEventBinder`）把它们翻译成同一个 `BlockUse`；
2. `Openers.claim(use)` 挨个问登记过的入口「这一下点中的是不是你会开的东西」：整方块终端按
   方块认（适配器登记），面板按射线求交认（`server/devices/panels.py::claim_open`，装配层登记）。
   以后的总线面板、P2P 面板各登记一个，不另起监听；
3. 有入口认领：
   - `ServerBlockUseEvent`（空手或手持任何物品都有）：潜行交给原版（贴着它放方块，同蔚蓝）；
     否则取消这次方块交互，按玩家限速（`TERMINAL_OPEN_LIMIT_SECONDS`），推屏；
   - `ServerItemUseOnEvent`（手持物品；放方块、用物品都在它之后）：`UseGuard` 的 `open` 规则
     问同一个 `claim`，认领了且不潜行就取消这次物品使用——手里的方块不会被放下。

手持我们自己的工具（扳手、面板、光缆点面板底座）由客户端接管成意图（`client/interaction`），
这两条服务端事件随之不来；它们点中面板时做什么见各自的处理函数。

**射线**：服务端的观察点是玩家眼睛（引擎 `GetPos`，玩家取到的就是眼睛高度），方向指向引擎给的
命中点（`cable_shapes.click_ray`，客户端意图同一个函数）。只有要求交的入口（面板）才读，一次
右键最多读一次。
"""

from ...common.cable_shapes import click_ray
from ...common.identity import NodeKey

#: `BlockUse.ray` 还没读过
_UNREAD = object()


class BlockUse(object):
    """服务端的一次右键方块：两条原版事件翻译成的同一份事实。

    - `player_id`、`dimension`、`pos`（被点的方块坐标）、`block`（被点的方块标识）；
    - `item_id`：手持物品标识，空手 `""`；
    - `click`：格内命中点（`clickX/Y/Z`，0～1）；事件没带为 None；
    - `ray()`：玩家眼睛 → 命中点（世界坐标，`(起点, 单位方向)`），读不到为 None。眼睛位置经
      注入的 `eye(player_id)` 读，第一次调用时读，之后复用。
    """

    __slots__ = ("player_id", "dimension", "pos", "block", "item_id", "click", "_eye", "_ray")

    def __init__(self, player_id, dimension, pos, block, item_id="", click=None, eye=None):
        # type: (str, int, tuple, str, str, object, object) -> None
        self.player_id = player_id
        self.dimension = dimension
        self.pos = tuple(pos)
        self.block = block
        self.item_id = item_id or ""
        self.click = tuple(click) if click is not None else None
        self._eye = eye
        self._ray = _UNREAD

    def node_key(self):
        # type: () -> NodeKey
        return NodeKey(self.dimension, self.pos[0], self.pos[1], self.pos[2])

    def ray(self):
        # type: () -> object
        if self._ray is _UNREAD:
            self._ray = self._read_ray()
        return self._ray

    def _read_ray(self):
        if self.click is None or self._eye is None:
            return None
        try:
            origin = self._eye(self.player_id)
        except Exception:
            return None
        if origin is None:
            return None
        return click_ray(origin, self.pos, self.click)

    def __repr__(self):
        return "BlockUse(%s @ %r, %r)" % (self.block, self.pos, self.item_id)


class OpenTarget(object):
    """入口认领一次右键的结果：开哪个界面（`ui_key`，与 `@RegistToolDeltaScreen` 的键逐字相同）、
    带什么参数（`params`，整方块终端与面板同形：`device_ref` + `dimension`）。

    `ui_key` 为 None = 归它管、这次打不开（设备还没被发现、这种面板没有界面）：原版交互照样
    取消，不推屏。
    """

    __slots__ = ("ui_key", "params")

    def __init__(self, ui_key=None, params=None):
        # type: (object, object) -> None
        self.ui_key = ui_key
        self.params = params if params is not None else {}

    @property
    def opens(self):
        # type: () -> bool
        return self.ui_key is not None

    def __repr__(self):
        return "OpenTarget(%s)" % (self.ui_key,)


class Openers(object):
    """入口表：`add(名字, claim)`，`claim(BlockUse) -> OpenTarget 或 None`（None = 不归它管）。

    按登记顺序问，第一个认领的算；同名再登记就覆盖（换实现时不必先摘）。某个入口抛异常只
    计数、当它没认领：总线回调里抛出去会打断同一批派发。
    """

    def __init__(self):
        self._entries = []
        self.stats = {"claimed": 0, "claim_errors": 0}

    def add(self, name, claim):
        # type: (str, object) -> None
        self._entries = [entry for entry in self._entries if entry[0] != name] + [(name, claim)]

    def names(self):
        # type: () -> list
        return [name for name, _claim in self._entries]

    def claim(self, use):
        # type: (BlockUse) -> object
        for _name, claim in self._entries:
            try:
                target = claim(use)
            except Exception:
                self.stats["claim_errors"] += 1
                continue
            if target is not None:
                self.stats["claimed"] += 1
                return target
        return None

    def diagnostics(self):
        # type: () -> dict
        return dict(self.stats, openers=self.names())
