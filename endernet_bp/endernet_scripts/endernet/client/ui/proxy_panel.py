# coding=utf-8
"""容器类设备屏的基座（11-07）。

这些设备的界面**由引擎按方块组件自动打开**（`netease:block_container`），
脚本侧的屏幕是挂上去的**代理**（`@RegistToolDeltaScreen(..., is_proxy=True)`
→ `NSManagerIns.RegisterScreenProxy`）。因此本基类与终端那两个屏有三处
结构性差别，都不是风格问题：

1. **不能 `PushUI`。** 代理类没有 `_screen_cls`，`PushUI` / `CreateUI` 对它
   必抛（`tooldelta/ui/general_screen.py`）。屏由引擎开，脚本只管挂上去。
2. **拿不到参数。** 代理的 `__init__(screenName, screenNode)` 把
   `_initial_params` 置 `None`——它**不知道自己对应哪台方块**。身份只能由
   服务端推过来（`client/device_identity.py`），屏在 `OnCreate` 试一次、
   之后每帧再试，拿到才开会话。**没拿到就什么都不做**：引擎自己会把容器
   格子画出来，屏绝不能因为还没认到身份就把界面搞空。
3. **没有引擎的 `OnDeactive`。** `_register_as_proxy` 只生成
   `OnCreate` / `OnDestroy` / `OnTick` 三个回调，`OnActive`/`OnDeactive`
   只能通过 `RemoveUI()` 走到。因此收尾**只能**放 `OnDestroy`。

## 为什么还要会话

状态栏要的是**持续更新**的设备总体状态，而它搭在终端已有的同步通道上
（11-08 的 `MSG_DEVICE_STATUS`，见 `server/sync/device_status.py`）。会话
也是「玩家走远、设备被换、关了界面」这几条注销路径的既有落点——另建一套
订阅表一定会漏掉某一条。

会话按空闲回收（`session_ttl_frames`，出厂 600 帧 ≈20 秒），而只读屏一条
命令都不发，因此必须发**保活**（`KEEPALIVE_FRAMES`）。见 `_keepalive`。
"""

from ....tooldelta.ui import ToolDeltaScreen

from ..device_identity import latch as identity_latch
from ..network_client import NetworkClient
from .widgets import device_status
from .widgets.device_status_bindings import DeviceStatusBindingsMixin

#: 保活间隔：每这么多帧发一条 `keepalive`。
#:
#: **这是帧数，不是秒**——界面的渲染帧率与脚本 tick 都不是一回事，写死频率
#: 换算会随负载漂（CLAUDE.md 记了这条，`BACKPACK_POLL_FRAMES` 同一条口径）。
#:
#: 取值看的是**它要躲开什么**：服务端的空闲上限是
#: `SessionPolicy.session_ttl_frames`（出厂 **600 个脚本 tick** ≈20 秒）。
#: 两个计数器**不同源**，因此真正要比的是
#: `KEEPALIVE_FRAMES / 客户端渲染帧率 < 600 / 脚本 tick 频率`，也就是
#: `KEEPALIVE_FRAMES < 600 × 渲染帧率 / 30`。**客户端的渲染帧率越慢，余量越小**
#: （这一条容易读反）。取 100：60fps 时约 1.7 秒一条（够密），
#: 低到 8fps 时 12.5 秒，仍在 20 秒以内。若将来实测发现设备屏在低帧率下
#: 仍然掉线，这里就是第一个要降的数。
#:
#: **0 不是合法的「关闭」写法。** 下面那句 `not KEEPALIVE_FRAMES` 只是不让
#: `%` 除零，不是留了一个开关——只读屏没有「不需要保活」的合法场景，
#: 关掉它等于让状态栏在 20 秒后悄悄失效（D-08 同一症状）。
KEEPALIVE_FRAMES = 100

#: 开会话之后的等待上限（帧）。超了还没建起来就按**失败**收摊。
#:
#: 没有它，一次被拒的 `session_open`（设备不在网上、没有主控）会把屏卡死：
#: `open_session` 已经发出、`self.client` 不再是 `None`，于是 `OnTicking`
#: 再也不走认领那一条支路；而被拒时服务端回的是**空会话号**的 `error`，
#: 客户端 `drop_session()` 之后 `has_session()` 一直是假——「还没建起来」
#: 与「建好了又没了」就再也分不开了。
#:
#: 上限取 60 帧：服务端下一个 tick（≈33 毫秒）就会回，正常情况远够；
#: 而低帧率下它也只是让「屏安静下来」晚一点，不会误杀一个正在建的会话——
#: 建起来了 `_session_lost` 立刻转成「已建立」，与这个上限无关。
SESSION_OPEN_GRACE_FRAMES = 60


class ProxyPanelBase(DeviceStatusBindingsMixin, ToolDeltaScreen):
    """容器类设备屏的公共半边：认领身份、开会话、保活、收尾。

    `client` 与 `status` 必须是**类属性**：绑定写在类体里、跟着界面走，
    比 `OnCreate` 早生效也活得更久，因此每条绑定回调都要容忍它们还是
    `None`（安全值见 `DeviceStatusBindingsMixin`）。
    """

    #: 本屏的同步客户端；没认到身份之前是 `None`。
    client = None
    #: 本屏的设备状态模型；没开会话之前是 `None`。
    status = None

    def __init__(self, screen_name, screen_instance, params=None):
        # type: (str, object, object) -> None
        ToolDeltaScreen.__init__(self, screen_name, screen_instance, params)
        self._ticks = 0
        #: 曾经真的建起过会话（收到过 `session_ready`）。用来把「还没开好」
        #: 与「开好了又没了」分开——两者都是 `has_session()` 为假，但只有
        #: 后者要立刻收摊（见 `_session_lost`）。
        self._had_session = False
        #: 发出 `session_open` 的那一帧。给「还没建起来」一个等待窗口，
        #: 超时按失败收摊（理由见 `SESSION_OPEN_GRACE_FRAMES`）。
        self._opened_at = None

    # -- 生命周期 ------------------------------------------------------

    def OnCreate(self):
        self._adopt()

    def OnTicking(self):
        self._ticks += 1
        if self.client is None:
            # 身份还没到（或推送丢了）。**每帧只做一次字典查询**，很便宜；
            # 拿到之前屏完全不动，容器格子照常由引擎渲染。
            self._adopt()
            return
        if self._session_lost():
            return
        self._keepalive()

    def OnDestroy(self):
        self._release()

    # -- 认领身份与会话 ------------------------------------------------

    def screen_key(self):
        # type: () -> str
        """本屏注册的界面键，也就是 `common/definitions.py` 的
        `CONTAINER_SCREENS` 里那一串。身份推送按它认领。"""
        return type(self)._screen_key

    def _adopt(self):
        # type: () -> bool
        """认领一条待处理的身份并开会话；没有则什么都不做。

        **取出即消费**：latch 里那条被摘掉，因此它只会被认领一次——留着
        复用会让下一次开屏认到一台可能已经被拆掉的设备。
        """
        entry = identity_latch().take(self.screen_key())
        if entry is None:
            return False
        device_ref, dimension = entry
        client = NetworkClient()
        # 先挂收包再开会话：`session_ready` 可能比这个函数返回还早，晚一步
        # 那一份就永远补不上了（与终端的 `OnCreate` 同一条理由）。
        client.port.listen(client.receive)
        # 状态模型同样要在 `open_session` 之前建好：会话一开，服务端下一帧
        # 就会推一份总体状态。
        self.status = device_status.DeviceStatusModel(device_ref)
        client.on_message(self._make_status_handler(client))
        self.client = client
        self._opened_at = self._ticks
        client.open_session(device_ref, dimension)
        return True

    def _session_lost(self):
        # type: () -> bool
        """会话这一侧出了问题就收摊，返回是否已经收摊。三种情况：

        - **建起来了又没了**（服务端判死、TTL 回收）→ 立刻收摊；
        - **一直没建起来**（`session_open` 被拒，或回包没到）→ 等
          `SESSION_OPEN_GRACE_FRAMES` 帧，超了按失败收摊；
        - **还在正常用** → 什么都不做。

        第二种是必需的：被拒时服务端回的是**空会话号**的 `error`，客户端
        `drop_session()` 之后 `has_session()` 与「刚发出去还没回」一模一样。
        不收摊的话 `self.client` 永远不是 `None`，`OnTicking` 再也不去认领
        身份——屏就此卡死，连**下一次右键**推来的身份都没人接。

        **不自动重开**：那是一次新决定（可能立刻因距离/维度复核失败，次数
        也属未定的量），与 D-08 的「面板不自救」同一条口径。收摊之后
        `self.client` 归 `None`，`OnTicking` 回到「等身份」那一支；latch 里
        那条已经被取走，于是屏安静地停住——状态栏随之隐藏（`status` 置回
        `None`，`visible()` 因此为假），**不会残留一个绿色的「在线」**。
        玩家关掉重开（或再右键一次）就会拿到一条新身份。
        """
        if self.client is None:
            return True
        if self.client.has_session():
            self._had_session = True
            self._opened_at = None
            return False
        if self._had_session:
            self._release()
            return True
        if (self._opened_at is not None
                and self._ticks - self._opened_at > SESSION_OPEN_GRACE_FRAMES):
            self._release()
            return True
        return False

    def _keepalive(self):
        # type: () -> None
        """按帧数发一条保活，见 `KEEPALIVE_FRAMES`。

        会话还没建起来（`session_ready` 未到）时**不发**：`keepalive()` 在
        没有会话时会抛 `ValueError`，而这里抛出去会从 `OnTicking` 一路回到
        引擎。它下一帧还会再来，跳过这一次代价为零。

        下面第一句的 `not KEEPALIVE_FRAMES` **只是不让 `%` 除零**，
        不是留了一个可用的开关（见该常量的 docstring）。
        """
        if not KEEPALIVE_FRAMES or self._ticks % KEEPALIVE_FRAMES:
            return
        if not self.client.has_session():
            return
        self.client.keepalive()

    def _release(self):
        # type: () -> None
        """收尾：注销端口、丢回调、关会话、把两个类属性置回安全值。

        **幂等**：`OnDestroy` 与 `_session_lost` 都可能走到这里。
        """
        client = self.client
        self.client = None
        self.status = None
        self._had_session = False
        self._opened_at = None
        if client is None:
            return
        client.port.unlisten()
        client.clear_handlers()
        client.close_session()

    def diagnostics(self):
        # type: () -> dict
        client = self.client
        return {
            "screen_key": type(self)._screen_key,
            "adopted": client is not None,
            "ticks": self._ticks,
            "session": client.diagnostics() if client is not None else None,
        }
