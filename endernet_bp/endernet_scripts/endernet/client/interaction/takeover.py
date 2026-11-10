# coding=utf-8
"""伪原版交互的客户端一半：把右键（方块使用 / 物品使用）接管成我们自己的动作。

光缆的扳手、挂面板、往面板底座里放光缆都走这里（都是「手持我们的工具做我们的动作」）；以后的
总线面板、P2P 面板同样只写一条「规则」（`wants` / `decide`）和一个「发出去」函数（`send`），
其余都由这一层做。**右键开界面不走这里**：整方块终端与面板终端都由服务端的原版事件打开
（`server/interaction/opening.py`，手持任何物品都开、原版放置随之取消，潜行交给原版）——点没点中
面板只有服务端知道，客户端拦下来就没法再还给原版。

1. **听**：`ClientBlockUseEvent`（右键 / 点按自定义方块，不带物品也不带面）、
   `ClientItemUseOnEvent`（手持物品对方块用，潜行右键只走这一条，按住时每个游戏刻一条，
   带被点的面）、`OnScriptTickClient`（推帧时钟）。
2. **一次按下**（`press.PressTracker`）：每条事件都先报给它。这次按下已被接管 → 拦掉原版
   交互（`ClientBlockUseEvent` 置 cancel、`ClientItemUseOnEvent` 置 ret，服务端的同名事件随之
   不再触发），不再发，也不挥手——不管点的是什么、手里拿的是什么。
3. **判定**：规则的 `wants(use)` 先筛（不读引擎，按住时每刻一条的事件大多到这里结束），
   再读潜行，`decide(use, 潜行)` 给出动作；给不出就不碰，原版照常。
4. **接管**：拦掉原版交互、记这次按下已接管 → 组射线（`cable_shapes.click_ray`；相机接口取不到就
   照拦、不发，计 `ray_failures`）→ `send(use, 动作, 射线)` → **挥一次手**（`EngineView.swing`，
   本地玩家的原版攻击动作，跟手、不等服务端回包）。同一次按下只挥一次；被按住吞掉的事件、
   引擎照常处理的交互都不挥（原版自己会挥，免得挥两次）。

放置音效在服务端放成功后播（`server/interaction/feedback.py`），这里不播：放不上是静默的，
客户端不知道成没成。

**不持有任何权威状态**：点中的是哪个部件、放在哪由服务端按方块状态求（客户端拿不到方块状态）。
引擎读数都经注入的 `view`（生产用 `engine.EngineView`），本模块离线可测。
"""

from ...common.cable_shapes import click_ray
from .press import FrameClock, PressTracker


class BlockUse(object):
    """一条使用事件里规则要看的事实（值对象）。

    - `pos`：被点的方块坐标；`block`：被点的方块标识；
    - `item_id`：手持物品（空手 `""`）；方块使用事件不带，从引擎读；
    - `face`：被点的面（引擎 `Facing` 值，0～5）；方块使用事件不带，为 None；
    - `click`：格内命中点（`clickX/Y/Z`，0～1）；
    - `via_item`：来自物品使用事件（带面）还是方块使用事件。
    """

    __slots__ = ("pos", "block", "item_id", "face", "click", "via_item")

    def __init__(self, pos, block, item_id, face, click, via_item):
        self.pos = pos
        self.block = block
        self.item_id = item_id
        self.face = face
        self.click = click
        self.via_item = via_item

    def __repr__(self):
        return "BlockUse(%s @ %r, %r)" % (self.block, self.pos, self.item_id)


class UseTakeover(object):
    """一层可复用的「伪原版交互」接管。可以脱离引擎构造：总线、事件类、引擎读数都由调用方注入。

    - `rule`：`wants(BlockUse) -> bool`（只看事件事实，不读引擎）与
      `decide(BlockUse, 潜行) -> 动作或 None`；
    - `send(BlockUse, 动作, (起点, 单位方向))`：把意图发给服务端；
    - `view`：`camera_origin()` / `held_item_id()` / `sneaking()` / `swing()`；
    - `tick_event`：引擎的帧事件（`OnScriptTickClient`），给了就由它推 `clock`；测试不给，
      直接拨 `clock.frame`。
    """

    def __init__(self, bus, block_use_event, item_use_on_event, view, rule, send,
                 tick_event=None, clock=None, presses=None):
        self.bus = bus
        self.block_use_event = block_use_event
        self.item_use_on_event = item_use_on_event
        self.tick_event = tick_event
        self.view = view
        self.rule = rule
        self.send = send
        self.clock = clock if clock is not None else FrameClock()
        self.presses = presses if presses is not None else PressTracker()
        self._listening = False
        self.stats = {
            "intercepted": 0,
            "sent": 0,
            "held": 0,
            "swung": 0,
            "swing_failures": 0,
            "ray_failures": 0,
            "read_failures": 0,
            "send_failures": 0,
        }

    def _listeners(self):
        listeners = [
            (self.block_use_event, self.on_block_use),
            (self.item_use_on_event, self.on_item_use_on),
        ]
        if self.tick_event is not None:
            listeners.append((self.tick_event, self.clock.tick))
        return listeners

    def start(self):
        if self._listening:
            return
        # 不带 `static=True`，理由同 `client/channels/device_identity.py`。
        for event_cls, handler in self._listeners():
            self.bus.ListenEvent(event_cls, 0)(handler)
        self._listening = True

    def stop(self):
        if not self._listening:
            return
        for event_cls, handler in self._listeners():
            self.bus.RemoveEventListener(event_cls, handler, 0)
        self._listening = False
        self.presses.reset()

    @property
    def listening(self):
        # type: () -> bool
        return self._listening

    # -- 引擎事件 --------------------------------------------------------

    def on_block_use(self, event):
        """右键 / 点按自定义方块。这条事件不带物品，也不带面。"""
        return self._handle(event, via_item=False)

    def on_item_use_on(self, event):
        """手持物品对方块用（潜行右键只走这一条）。物品与被点的面都在事件里。"""
        return self._handle(event, via_item=True)

    def _handle(self, event, via_item):
        fresh = self.presses.press(self.clock.now())
        if not fresh and self.presses.owned:
            # 这次按下已被接管：后面的事件一律拦掉原版交互，不发、不挥
            event.cancel()
            self.stats["held"] += 1
            return True
        use = self._facts(event, via_item)
        if use is None or not self.rule.wants(use):
            return False
        try:
            sneaking = bool(self.view.sneaking())
        except Exception:
            self.stats["read_failures"] += 1
            return False
        action = self.rule.decide(use, sneaking)
        if action is None:
            return False
        # 先拦、先记接管：后面哪一步失败，这一下与这次按下的后续事件都不该落成原版交互
        event.cancel()
        self.presses.take()
        self.stats["intercepted"] += 1
        ray = self.ray(use)
        if ray is None:
            self.stats["ray_failures"] += 1
            return True
        try:
            self.send(use, action, ray)
        except Exception:
            self.stats["send_failures"] += 1
            return True
        self.stats["sent"] += 1
        self.swing()
        return True

    def _facts(self, event, via_item):
        # type: (object, bool) -> object
        if via_item:
            item = getattr(event, "item", None)
            item_id = getattr(item, "newItemName", None) or ""
            face = getattr(event, "face", None)
        else:
            try:
                item_id = self.view.held_item_id() or ""
            except Exception:
                self.stats["read_failures"] += 1
                return None
            face = None
        return BlockUse(
            (event.x, event.y, event.z),
            getattr(event, "blockName", None),
            item_id,
            face,
            (getattr(event, "clickX", None), getattr(event, "clickY", None),
             getattr(event, "clickZ", None)),
            via_item,
        )

    def ray(self, use):
        # type: (BlockUse) -> object
        """这一下的射线 `(相机原点, 指向命中点的单位方向)`（世界坐标）；取不到返回 None。"""
        try:
            origin = self.view.camera_origin()
            if origin is None:
                return None
            return click_ray(origin, use.pos, use.click)
        except Exception:
            return None

    def swing(self):
        """挥一次手。失败只计数：挥不挥都不影响意图。"""
        try:
            self.view.swing()
        except Exception:
            self.stats["swing_failures"] += 1
            return
        self.stats["swung"] += 1

    def diagnostics(self):
        # type: () -> dict
        return dict(self.stats, listening=self._listening)
