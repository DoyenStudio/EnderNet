# coding=utf-8
"""引擎事件到归一化事件的绑定。本模块是**引擎边界**。

对应任务 01-05。这里只做翻译与入队，不做任何业务判断。

**事件的语义注记（都是实测后才能确认的点）：**

- `BlockRemoveServerEvent` 必须先经 `AddBlocksToBlockRemoveListener` 白名单，
  否则引擎不会派发。启动时由 `register_remove_whitelist` 统一登记。
- `ServerPlayerTryDestroyBlockEvent` 是**破坏前**事件，可取消；
  `DestroyBlockEvent` 是**破坏后**事件。前者绝不能当作「已拆除」。
- `ServerPlaceBlockEntityEvent` 与 `EntityPlaceBlockAfterServerEvent`
  对同一次放置可能都触发。队列按位置合并，注册表再加一道幂等，
  两道都过不去才会重复建实例。
- `ChunkAcquireDiscardedServerEvent` 的文档明确说触发时实体已无法查询，
  只能用于资源回收，因此这里**只**用来做区块卸载标记。
"""

from ...common.definitions import (
    CONTAINER_SCREENS,
    TERMINAL_OPEN_LIMIT_SECONDS,
    TERMINAL_SCREENS,
)
from ...common.device_identity import identity_payload
from ...common.rate_limit import PlayerRateLimiter
from ...common.identity import ChunkKey, NodeKey
from .device_identity import send_device_identity
from ..runtime.events import EventKind, NormalizedEvent, WakeSet


def engine_player_dimension(player_id):
    # type: (str) -> int
    """玩家维度的**引擎实现**。

    导入留在函数体内：`tooldelta.api.server` 在模块级依赖
    `mod.server.extraServerApi`，顶层导入会让整条离线链路失效。

    **它可以被注入替换**（`ServerEventBinder(player_dimension=...)`），
    理由与 `event_api` 完全一样：自定义容器的两条玩家事件不带维度，
    而「插槽拒收」这条业务规则正挂在那条路径上——不注入的话，
    这条规则**在离线测试里根本走不到**，只能进游戏试。
    """
    from ....tooldelta.api.server import GetPlayerDimensionId

    return int(GetPlayerDimensionId(player_id))


def _item_name(item):
    # type: (object) -> str
    """容器事件里那件物品的标识；读不出返回空串。

    **只读名字，不碰 `userData`。** 事件里的 `itemDict` 带不带 `userData`
    取决于是否调过引擎的 `GetUserDataInEvent`（tooldelta 的
    `ListenWithUserData()` 就是干这个的），那是一条额外的不确定项；
    而插槽拒收只需要知道**是不是本模组的样板、是哪一种**——
    这两件事物品标识就答得了（物品类型就是配方模式）。
    内容是否合法在重读路径上判，那条路走 `GetContainerItem(..., True)`，
    已经在真机上验过它带 `userData`。
    """
    name = getattr(item, "newItemName", None)
    return name if name else ""


class ToolDeltaEventApi(object):
    """tooldelta 事件 API 的薄封装。

    **引擎边界**：只有这里 import `tooldelta.events`。

    新版 tooldelta 改用 `EventBus` 实例管理事件：模块级的 `serverapi`
    已经没有 `ListenEvent` / `RemoveEventListener` 这两个属性，
    注册与注销变成总线实例上的方法。

    **本模组统一使用 `GetMCServerEventBus()`，原版事件与自定义事件都走它。**

    原因是注册时用的是
    `event.GetNamespace() or self.namespace` 与
    `event.GetSystemName() or self.system_name`——**事件类自带的命名空间优先**，
    总线自身的只在事件没有时作回退。自定义事件（`CustomC2SEvent` 之类）
    的 `GetNamespace()` / `GetSystemName()` 返回本模组的名字，
    所以挂在 MC 总线上同样注册到正确的位置。
    结论：`GetModServerEventBus()` 那一支对本项目是多余的，新代码不要用它——
    单一总线少一个「这个事件该挂哪条」的判断，也就少一处会挂错的地方。

    抽成对象是为了让 `ServerEventBinder` 可以注入替身——
    注册与注销必须能脱离引擎验证，否则「stop 之后回调还在不在」
    就只能靠进游戏试，而这正是出过问题的地方。
    """

    # 这里**不再单列一份类名清单**：下面 `__init__` 里的显式赋值就是
    # 唯一事实来源，离线测试直接从这段代码里读（见
    # `endernet_tests/testsupport.py` 的 `declared_event_class_names`）。
    # 单列一份清单会多一处需要同步的地方，而它在运行时毫无用处。
    #
    # 声明了就必须在 `bind()` 里真的注册——反向检查
    # （`test_import_boundaries.py` 的 `test_declared_classes_are_all_used`）
    # 会立刻发现「声明了没用」，不必等到真机。

    def __init__(self):
        from ....tooldelta import events as events_module
        from ....tooldelta.events.event_bus import GetMCServerEventBus

        #: 引擎事件总线。它内部依赖 `tooldelta.internal.GetServer()`，
        #: 因此必须在服务端系统初始化之后取——`bind()` 只在
        #: `OnServerInited` 之后被调用，这里是安全的。
        self._bus = GetMCServerEventBus()

        self.EntityPlaceBlockAfterServerEvent = (
            events_module.EntityPlaceBlockAfterServerEvent
        )
        self.ServerPlaceBlockEntityEvent = events_module.ServerPlaceBlockEntityEvent
        self.BlockRemoveServerEvent = events_module.BlockRemoveServerEvent
        self.ServerPlayerTryDestroyBlockEvent = (
            events_module.ServerPlayerTryDestroyBlockEvent
        )
        self.BlockNeighborChangedServerEvent = (
            events_module.BlockNeighborChangedServerEvent
        )
        self.ChunkLoadedServerEvent = events_module.ChunkLoadedServerEvent
        self.ChunkAcquireDiscardedServerEvent = (
            events_module.ChunkAcquireDiscardedServerEvent
        )
        self.OnScriptTickServer = events_module.OnScriptTickServer
        self.OnSimTickServerEvent = events_module.OnSimTickServerEvent
        self.DestroyBlockEvent = events_module.DestroyBlockEvent
        # 设备级 tick（任务 04-02 的唤醒信号）。**它在游戏里本来就会触发**：
        # `scripts/gen_blocks.py` 的 TICKING 让两类控制器、装配仓、并行处理器
        # 与能源接入口声明了 `netease:block_entity.tick = true`，
        # `test_assets.py` 正断言这一点。
        self.ServerBlockEntityTickEvent = events_module.ServerBlockEntityTickEvent
        # 玩家右键方块（11-03 的终端打开入口）。**只用于终端**：其余方块
        # 的交互都要么没有、要么有专门的容器事件（07 那四条）。
        self.ServerBlockUseEvent = events_module.ServerBlockUseEvent

        # --- 容器变更（任务 07 的样板槽发现）---------------------------
        #
        # 本模组的方块容器是 `netease:block_container`（`base_block` 为
        # `netease_container`）。**玩家往里面放东西没有任何事件队列能告诉我们
        # 「放了什么」的顺序**，因此下面这几条只当**脏标记**用，实际状态由
        # `PatternStore` 重读那 9 格得出。
        #
        # 覆盖范围是**查出来的，不是猜的**：同框架（tooldelta）的另一份可用
        # 实现 `skybluetech/server/machinery/basic/register.py` 对自定义容器
        # 同时用了 `ContainerItemChangedServerEvent`（带 `@Delay(0)` 推到下一帧）
        # 与 `PlayerTryPut/RemoveCustomContainerItem*`、`ItemPush/PullOutCustom
        # Container*` —— 说明这四条对 `netease_container` 都会派发。
        # 本项目的做法比 `@Delay(0)` 更保守：标脏后由 `PatternStore.advance()`
        # 静默若干帧再读，因此不需要引入 `Delay`。
        #
        # 四条各自的覆盖范围不同，缺一条就会有「某些改法不生效」：
        # 玩家放入 / 玩家取出 / 漏斗漏入 / 漏斗漏出。
        self.PlayerTryPutCustomContainerItemServerEvent = (
            events_module.PlayerTryPutCustomContainerItemServerEvent
        )
        self.PlayerTryRemoveCustomContainerItemServerEvent = (
            events_module.PlayerTryRemoveCustomContainerItemServerEvent
        )
        self.ContainerItemChangedServerEvent = (
            events_module.ContainerItemChangedServerEvent
        )
        self.ItemPushInCustomContainerServerEvent = (
            events_module.ItemPushInCustomContainerServerEvent
        )
        self.ItemPullOutCustomContainerServerEvent = (
            events_module.ItemPullOutCustomContainerServerEvent
        )

    # 下面两个方法刻意**不写 `# type:` 注释**：它们的返回值是引擎的装饰器，
    # 而 `tooldelta` 的类型信息在本仓库无法解析。写成 `-> object` 会让
    # `@binder.listen(...)` 被判成「object 不可调用」，整片报错；
    # 交给推断反而干净。
    def listen(self, event_cls, priority=0):
        """返回注册装饰器。

        与 `ServerEvent.Listen(priority)` 等价（同样 `static=True`），
        区别只是保留总线句柄以便注销。注意 `EventBus.ListenEvent` 的
        `priority` 是**必填位置参数**，没有默认值。
        """
        return self._bus.ListenEvent(event_cls, priority, static=True)

    def remove(self, event_cls, func, priority=0):
        self._bus.RemoveEventListener(event_cls, func, priority)
        return True


class ServerEventBinder(object):
    """把引擎监听器接到 `EventQueue` 与 `world` 适配器上。

    监听器在 `bind()` 里注册，句柄全部记下来；`unbind()` 逐一注销
    （01-01 要求模组有明确的销毁流程，不能只靠热重载）。

    **注销必须真的调用引擎的 RemoveEventListener。** 只翻一个布尔值
    不会让回调消失：监听器注册在进程级表里，同进程内 stop 再 start
    会留下旧回调，而重新 bind 创建的是新闭包，靠函数对象去重也拦不住。
    """

    def __init__(
        self,
        queue,
        world,
        registry,
        definitions,
        clock=None,
        event_api=None,
        patterns=None,
        player_dimension=None,
    ):
        self.queue = queue
        self.world = world
        self.registry = registry
        self.definitions = definitions
        self.clock = clock
        #: 容器变更的接收方（样板层）。**鸭子类型，只要求两个方法**：
        #:
        #: - `mark_dirty(node_key)` —— 这个位置的容器变了，稍后重读；
        #: - `accepts_put(device_id, item_name)` —— 这件东西该不该放进去。
        #:
        #: 做成鸭子类型而不是直接依赖 `PatternStore`：适配器是引擎边界，
        #: 它不该知道什么是样板。判定规则留在领域层，这里只负责把
        #: 引擎给的位置与物品名翻译过去。**可以为 None**（不接样板层的装配）。
        self.patterns = patterns
        #: 玩家维度解析。默认是引擎实现；注入替身即可在离线测试里走通
        #: 「自定义容器事件没有维度」那条路径——而插槽拒收正挂在它上面。
        self.player_dimension = (
            player_dimension
            if player_dimension is not None
            else engine_player_dimension
        )
        #: 引擎事件 API。None 表示首次 bind 时惰性构造真实实现。
        self.event_api = event_api
        self._bound = False
        #: 方块实体 tick 的唤醒信号。**不进 `EventQueue`**，理由见 `WakeSet`。
        self.wakes = WakeSet()
        #: `[(event_cls, func, priority)]`，unbind 时逐一注销
        self._registrations = []
        self.unregistered = 0
        #: 注销失败的条目（名字 + 异常文本）。非空说明有监听器没摘干净。
        self.unregister_failures = []
        #: 终端打开入口的限速器（`_on_block_use`）。**按玩家记**，窗口见
        #: `TERMINAL_OPEN_LIMIT_SECONDS`。放在这里而不是模块级：一个进程装了
        #: 几套适配器就各限各的，且 `stop()` 之后不留跨会话的残留计数。
        self._open_limiter = PlayerRateLimiter(TERMINAL_OPEN_LIMIT_SECONDS)
        self.counts = {}

    # -- 注册 ----------------------------------------------------------

    def require_api(self):
        # type: () -> ToolDeltaEventApi
        if self.event_api is None:
            self.event_api = ToolDeltaEventApi()
        return self.event_api

    # 这三个方法刻意**不写 `# type:` 注释**。它们的参数与返回值都是
    # 「引擎事件类」与「回调函数」，而 tooldelta 的类型在本仓库解析不出来；
    # 一旦写成 `-> object`，`@binder.listen(api.X)` 会被判成
    # 「object 不可调用」，IDE 里整片报错。交给推断反而干净。
    def _remember(self, event_cls, func, priority):
        """实际注册并记下句柄，供 `listen` 与 `register` 共用。"""
        self.require_api().listen(event_cls, priority)(func)
        self._registrations.append((event_cls, func, priority))
        return func

    def listen(self, event_cls, priority=0):
        """返回注册装饰器：`@binder.listen(api.XxxEvent)`。

        **与 `register` 分开是有意的。** 合成一个「给了 func 就注册、
        没给就返回装饰器」的函数，静态分析只能推成一个宽泛的返回类型，
        装饰器位置就会报错。拆开后两边的签名各自清楚。
        """
        return lambda func: self._remember(event_cls, func, priority)

    def register(self, event_cls, func, priority=0):
        """直接注册一个监听器（不走装饰器语法时用）。"""
        return self._remember(event_cls, func, priority)

    def registered_count(self):
        # type: () -> int
        return len(self._registrations)

    # -- 计数（诊断用：能看出哪类事件真的在触发）------------------------

    def _count(self, kind):
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def _tick(self):
        # 权威时钟由调度器掌握；这里只记录入队时刻，不做频率假设。
        if self.clock is None:
            return 0
        return self.clock.current_tick()

    # -- 绑定 ----------------------------------------------------------

    def register_remove_whitelist(self):
        """把全部本模组方块加入移除事件白名单。启动时必须调用一次。"""
        self.world.register_block_remove_listener(set(self.definitions.devices.keys()))

    def bind(self):
        # type: () -> None
        if self._bound:
            return
        api = self.require_api()

        @self.listen(api.EntityPlaceBlockAfterServerEvent)
        def on_place_after(event):
            self._on_place(event.dimensionId, event.x, event.y, event.z, event.fullName,
                           "entity_place_after")

        @self.listen(api.ServerPlaceBlockEntityEvent)
        def on_place_block_entity(event):
            self._on_place(event.dimension, event.posX, event.posY, event.posZ,
                           event.blockName, "block_entity_placed")

        @self.listen(api.BlockRemoveServerEvent)
        def on_remove(event):
            self._on_remove(event.dimension, event.x, event.y, event.z, event.fullName)

        @self.listen(api.ServerPlayerTryDestroyBlockEvent)
        def on_try_destroy(event):
            self._on_try_destroy(event.dimensionId, event.x, event.y, event.z,
                                 event.fullName)

        @self.listen(api.BlockNeighborChangedServerEvent)
        def on_neighbor(event):
            self._on_neighbor(event.dimensionId, event.posX, event.posY, event.posZ,
                              event.neighborPosX, event.neighborPosY,
                              event.neighborPosZ, event.toBlockName)

        @self.listen(api.ChunkLoadedServerEvent)
        def on_chunk_loaded(event):
            self._on_chunk_loaded(event.dimension, event.chunkPosX, event.chunkPosZ,
                                  event.blockEntities)

        @self.listen(api.ChunkAcquireDiscardedServerEvent)
        def on_chunk_discarded(event):
            self._on_chunk_unloaded(event.dimension, event.chunkPosX, event.chunkPosZ)

        @self.listen(api.OnScriptTickServer)
        def on_script_tick(_):
            self._on_tick(EventKind.SCRIPT_TICK)

        @self.listen(api.OnSimTickServerEvent)
        def on_sim_tick(_):
            self._on_tick(EventKind.SIM_TICK)

        @self.listen(api.DestroyBlockEvent)
        def on_destroyed(_):
            # 破坏后事件：仅用于诊断。实际注销走 BlockRemoveServerEvent，
            # 避免两条路径各注销一次。
            self._count("destroy_after")

        @self.listen(api.ServerBlockEntityTickEvent)
        def on_block_entity_tick(event):
            self._on_block_entity_tick(event.dimension, event.posX, event.posY,
                                       event.posZ, event.blockName)

        # --- 终端打开入口（11-03）--------------------------------------

        @self.listen(api.ServerBlockUseEvent)
        def on_block_use(event):
            self._on_block_use(event)

        # --- 容器变更（07）--------------------------------------------
        # 四条都只做一件事：**把位置标脏**。顺序不定、一条动作可能派发多条，
        # 因此这里一条都不拿来推断状态（见 `ToolDeltaEventApi.__init__` 的说明）。

        @self.listen(api.ContainerItemChangedServerEvent)
        def on_container_changed(event):
            self._count("container_changed")
            self._mark_container_dirty(event.dimensionId, event.pos)

        @self.listen(api.PlayerTryPutCustomContainerItemServerEvent)
        def on_custom_put(event):
            self._count("custom_container_put")
            self._on_custom_put(event)

        @self.listen(api.PlayerTryRemoveCustomContainerItemServerEvent)
        def on_custom_remove(event):
            # **取出永远不拦。** 拒收只针对「不该进来的东西」；
            # 连拿都拿不出来会让玩家对着一块卡住的方块无计可施。
            self._count("custom_container_remove")
            self._mark_container_dirty(
                self._player_dimension(event.playerId),
                (event.x, event.y, event.z),
            )

        @self.listen(api.ItemPushInCustomContainerServerEvent)
        def on_hopper_in(event):
            # **漏斗是第二条放入路径，拒收规则必须同样生效。**
            # 少了这一条，一台对着样板接口的漏斗能把任意物品塞进那 9 格
            # ——`blocks.md` 明写它们「专用于存放工厂样板」，而
            # 14 的验收表要求「样板槽拒收普通原料」。
            # 被动接口不受影响：`accepts_put` 对非提供者一律放行，
            # 而那正是 06「外部物流往本地缓冲里塞东西」的通道。
            self._count("custom_container_push_in")
            if self.patterns is None:
                return
            # `getattr` 而不是直接取：引擎回调里抛异常会一路回到引擎，
            # 而这里离 bootstrap 的兜底很远。字段缺失时按「读不出物品名」
            # 处理（`_item_name` 返回空串，于是只标脏、不拦）。
            self._veto_or_mark(
                NodeKey(event.dimension, event.x, event.y, event.z),
                getattr(event, "item", None),
                event,
            )

        @self.listen(api.ItemPullOutCustomContainerServerEvent)
        def on_hopper_out(event):
            # 漏出同样是「取出」，只标脏、不拦。
            self._count("custom_container_pull_out")
            self._mark_container_dirty(
                event.dimension, (event.x, event.y, event.z)
            )

        self._bound = True

    def unbind(self):
        # type: () -> int
        """显式销毁：逐一注销已注册的监听器，返回成功注销的数量。

        这样同进程内 stop 再 start 才不会留下旧回调。

        单个注销失败**不中断其余**：引擎可能已经自行清理过该监听器，
        或者它压根没注册成功。但失败会被记进 `unregister_failures`，
        **不是静默吞掉**——「注销没生效」的表现是热重载后回调翻倍，
        那种症状极难定位，必须留下痕迹。
        """
        api = self.event_api
        removed = 0
        failures = []
        if api is not None:
            for event_cls, func, priority in self._registrations:
                try:
                    if api.remove(event_cls, func, priority):
                        removed += 1
                except Exception as exc:  # noqa: BLE001
                    # 引擎可能抛什么类型无从预判（tooldelta 的
                    # `_dyn_unlisten` 在监听器不在表里时会 `list.remove` 抛
                    # ValueError）。这里按名字逐条记录，便于对照。
                    failures.append(
                        "%s: %s" % (getattr(event_cls, "name", event_cls), exc)
                    )
        self._registrations = []
        self._bound = False
        self.unregistered += removed
        self.unregister_failures.extend(failures)
        return removed

    def is_bound(self):
        return self._bound

    # -- 翻译 ----------------------------------------------------------

    def _is_ours(self, block_name):
        return bool(block_name) and self.definitions.has(block_name)

    def _on_place(self, dimension, x, y, z, block_name, source):
        self._count("place")
        if not self._is_ours(block_name):
            return
        # 朝向不在这里读：补查路径（区块加载）也需要同一个朝向，
        # 两处各读一次迟早会漏掉一边。统一由生命周期解析。
        self.queue.push(
            NormalizedEvent.place(NodeKey(dimension, x, y, z), block_name,
                                  tick=self._tick(), source=source)
        )

    def _on_remove(self, dimension, x, y, z, block_name):
        self._count("remove")
        if not self._is_ours(block_name):
            return
        self.queue.push(
            NormalizedEvent.remove(NodeKey(dimension, x, y, z), block_name,
                                   tick=self._tick(), source="block_remove")
        )

    def _on_try_destroy(self, dimension, x, y, z, block_name):
        self._count("try_destroy")
        if not self._is_ours(block_name):
            return
        # 只是「尝试」，方块还在：交给生命周期发一条待裁决的变化。
        self.queue.push(
            NormalizedEvent(
                EventKind.TRY_DESTROY,
                tick=self._tick(),
                data={"node_key": NodeKey(dimension, x, y, z), "device_id": block_name},
            )
        )

    def _on_neighbor(self, dimension, x, y, z, nx, ny, nz, to_block_name):
        self._count("neighbor")
        node_key = NodeKey(dimension, x, y, z)
        neighbor_key = NodeKey(dimension, nx, ny, nz)
        if not (self.registry.matches_key(node_key) or self.registry.matches_key(neighbor_key)):
            return
        self.queue.push(
            NormalizedEvent.neighbor_changed(node_key, neighbor_key,
                                             to_device_id=to_block_name,
                                             tick=self._tick())
        )

    def _on_chunk_loaded(self, dimension, chunk_x, chunk_z, block_entities):
        self._count("chunk_loaded")
        chunk_key = ChunkKey(dimension, chunk_x, chunk_z)
        entries = [
            entry for entry in (block_entities or ())
            if self._is_ours(entry.get("blockName"))
        ]
        self.world.note_chunk_loaded(chunk_key, entries)
        self.queue.push(
            NormalizedEvent.chunk_loaded(chunk_key, entries, tick=self._tick())
        )

    def _on_chunk_unloaded(self, dimension, chunk_x, chunk_z):
        self._count("chunk_unloaded")
        chunk_key = ChunkKey(dimension, chunk_x, chunk_z)
        self.world.note_chunk_unloaded(chunk_key)
        self.queue.push(NormalizedEvent.chunk_unloaded(chunk_key, tick=self._tick()))

    def _on_tick(self, kind):
        # 计数只走 `counts` 一份（见 `pulse_counts`）。时钟只由 `drive_once()`
        # 推进，与这里无关。
        self._count(kind)
        self.queue.push(NormalizedEvent(kind, tick=self._tick()))

    # -- 容器变更（07）------------------------------------------------
    #
    # 这一组只做两件事：**把位置标脏**，以及**把不该在这里的东西挡回去**。
    # 一条都不拿来推断状态——ModAPI 明说容器内移动、合堆、分堆会「分多次
    # 事件触发并且顺序不定」，且放入事件只在**目标槽位为空**时触发。
    # 真正发生了什么，由 `PatternStore` 重读那 9 格回答。

    def _player_dimension(self, player_id):
        # type: (str) -> object
        """玩家所在的维度。**自定义容器的两条玩家事件不带维度**，只能这么推。

        可靠的理由：方块容器只能在同一个维度里交互到，因此「玩家刚才在哪个
        维度」就是他刚才操作的那台容器所在的维度。拿不到（玩家已离线、
        接口异常）返回 `None`，调用方据此**放弃这一次标脏**——
        不猜一个维度：猜错会去读另一个维度同坐标的方块，而那可能是别人的设备。

        导入留在方法体内：`tooldelta.api.server` 在模块级依赖
        `mod.server.extraServerApi`，顶层导入会让整条离线链路失效。
        """
        if not player_id:
            return None
        try:
            return int(self.player_dimension(player_id))
        except Exception:
            # 引擎接口在离线替身里通常不存在。**不记 tick_error**：
            # 这是「这条信息这次拿不到」，不是缺陷。拿不到就放弃这一次
            # 标脏/拒收，**不猜一个维度**。
            return None

    def _mark_container_dirty(self, dimension, position):
        # type: (object, object) -> bool
        """把一个容器位置标脏。维度或坐标拿不到就什么都不做。"""
        if self.patterns is None or dimension is None or position is None:
            return False
        try:
            x, y, z = position
        except (TypeError, ValueError):
            return False
        return bool(self.patterns.mark_dirty(NodeKey(dimension, x, y, z)))

    def _on_custom_put(self, event):
        # type: (object) -> None
        """玩家往自定义容器里放东西。

        自定义容器的两条玩家事件**都不带维度**，只能从交互的玩家推出来；
        推不出来就放弃这一次（不猜一个维度，见 `_player_dimension`）。
        """
        if self.patterns is None:
            return
        dimension = self._player_dimension(event.playerId)
        if dimension is None:
            return
        self._veto_or_mark(
            NodeKey(dimension, event.x, event.y, event.z),
            getattr(event, "item", None),
            event,
        )

    def _veto_or_mark(self, node_key, item, event):
        # type: (object, object, object) -> bool
        """按领域规则决定「拒收」还是「放行并标脏」。返回是否拒收。

        **两条放入路径（玩家拖入、漏斗漏入）共用这一份。** 分开写的代价是
        加一条新规则（比如将来 R09 定案后放宽适用范围）时只改了其中一处，
        而另一处照旧放行——症状是「手动放不进去、用漏斗能放进去」，
        玩家会以为那是漏洞，而它确实是。

        判定在领域层（`PatternStore.accepts_put`），这里只做翻译：
        拒收走 `event.cancel()`，放行则标脏等重读。
        """
        record = self.registry.get(node_key)
        if record is None:
            return False
        item_name = _item_name(item)
        # **读不出物品名时仍然标脏。** 事件本身已经说明「这个容器变了」，
        # 而那与能否读出物品名无关；判不了该不该拦只是不拦而已。
        # 漏掉这一支的后果是「某些改法不进索引」，而它只在物品字典
        # 形状意外时才出现——最难复现的那一类。
        if item_name and not self.patterns.accepts_put(record.device_id, item_name):
            event.cancel()
            self._count("pattern_slot_rejected")
            return True
        self.patterns.mark_dirty(node_key)
        return False

    def _on_block_entity_tick(self, dimension, x, y, z, block_name):
        """方块实体 tick → 唤醒信号，**不入 `EventQueue`**（理由见 `WakeSet`）。

        只翻译与记录，不做任何业务判断：真正决定「醒来之后干什么」的是
        调度服务，而它按位置路由（实测坐标与放置事件同一坐标系）。

        **它不是时钟。** 这是逐方块实体派发的，引擎不保证同一时间窗口内
        各方块实体的 tick 次数一致，也不保证与全局 game tick 一一对应。
        所以它只被当作「该醒醒了」的提示，**不参与任何计时或判定**——
        到期与退避走的是逻辑帧（见 `EventKind.BLOCK_ENTITY_TICK`）。

        另一个实测限制：它只在**模拟距离内**的区块里派发。玩家走远时这里
        一条都不会来，因此唤醒不能是唯一的发现手段——低频退避轮询是必需的
        兜底（`SchedulerService` 的 `PollBackoff`）。
        """
        self._count("block_entity_tick")
        if not self._is_ours(block_name):
            return
        self.wakes.add(EventKind.BLOCK_ENTITY_TICK, NodeKey(dimension, x, y, z))

    #: 04-01 要在真机上算比值的那三种引擎脉冲。
    #:
    #: **计数只有一份**：`counts` 里本来就有它们（`_count` 对每种事件都调）。
    #: 曾经时钟那边另存过一份 `pulses`，而这里只更新自己的，于是
    #: `pulses["block_entity_tick"]` 永远是 0——真机量出来的比值会是 0，
    #: 看起来像「引擎根本没派发这个事件」。
    PULSE_KINDS = (
        EventKind.SCRIPT_TICK,
        EventKind.SIM_TICK,
        EventKind.BLOCK_ENTITY_TICK,
    )

    def pulse_counts(self):
        # type: () -> dict
        """三种引擎脉冲各自发生了多少次。`0` 是真实的 0，不是「没记」。"""
        return dict(
            (kind, self.counts.get(kind, 0)) for kind in self.PULSE_KINDS
        )

    def take_wakes(self):
        # type: () -> tuple
        """取走本帧累计的唤醒信号。调用方负责按位置路由。"""
        return self.wakes.take()

    def clear_wakes(self):
        # type: () -> int
        """丢弃未处理的唤醒信号（停机时用）。"""
        return self.wakes.clear()

    def diagnostics(self):
        # type: () -> dict
        return {
            "bound": self._bound,
            "event_counts": dict(self.counts),
            "wakes": self.wakes.stats(),
        }

    # -- 终端打开入口（11-03）--------------------------------------------

    def _on_block_use(self, event):
        """玩家右键终端方块 → 推给客户端一条「打开这个界面」。

        **设备的权威身份只能从注册表取。** 事件给的是方块名与坐标，而客户端
        开会话时要带一个 `DeviceRef`（位置 + 实例代次）。代次不能由客户端
        编——它得是注册表里那台**当前**设备的代次，否则 `SessionRegistry`
        每一帧的 `_check_target` 都会判它过期。按坐标查注册表，查到的
        `record.ref` 就是唯一答案。

        查不到记录 = 这台方块还没被发现（区块刚加载），或它压根不是我们认的
        设备。此时**什么都不做**：编一个代次为 0 的 ref 发过去，客户端会开出
        一个立刻被服务端关掉的会话，玩家看到的是「界面一闪」。

        见 `plans/tasks/11-03` 的打开入口决策（D5）。
        """
        self._count("block_use")
        block_name = getattr(event, "blockName", "")
        if not block_name:
            return
        container_key = CONTAINER_SCREENS.get(block_name)
        if container_key is not None:
            self._on_container_block_use(event, container_key)
            return
        ui_key = TERMINAL_SCREENS.get(block_name)
        if ui_key is None:
            # 本模组的其它方块或原版方块被右键：正常现象，不计入诊断，
            # 否则 `block_use` 的计数会被无关右键淹没。
            #
            # 另三个终端方块（合成/样板/工厂）有意**不在这里**——它们的界面
            # 是 11-04/11-05 的事，退回通用终端就是替那两项悄悄定案。
            return
        dimension = getattr(event, "dimensionId", 0)
        node_key = NodeKey(dimension, event.x, event.y, event.z)
        record = self.registry.get(node_key)
        if record is None:
            self._count("block_use_unknown")
            return
        # **限速在推之前。** 引擎的方块使用回调不节流，连点几下就是几条
        # `PushUIRequest`，客户端会把同一次打开做几遍（真的开出好几个界面）。
        # 窗口见 `TERMINAL_OPEN_LIMIT_SECONDS`（0.2 秒），按玩家记。
        #
        # 位置在**注册表查出记录之后**：查不到记录的那条路本来就什么都不做，
        # 拿它去消耗窗口只会让「方块刚加载、玩家连点两下」的第二下被吞。
        # 被限速的那一击单独计数，否则诊断里只看得到 `block_use` 涨、
        # `block_use_opened` 不涨，分不清是限速还是别的原因。
        player_id = getattr(event, "playerId", "")
        if not self._open_limiter.allow(player_id):
            self._count("block_use_limited")
            return
        self._count("block_use_opened")
        self._notify_ui(
            player_id,
            ui_key,
            {"device_ref": record.ref.to_dict(), "dimension": dimension},
        )

    def _on_container_block_use(self, event, ui_key):
        """玩家右键一台**容器类**设备 → 推一条「这个界面是哪台设备」。

        与终端那一条的分工只有一个区别：容器类的屏幕由**引擎**按方块组件
        （`netease:block_container`）自动打开，脚本侧只是挂在它上面的代理，
        因此这里**不 `PushUI`**——代理类没有可推的屏（`_screen_cls` 为
        `None`，`PushUI` 必抛），只把权威身份推过去让屏幕自己认领。

        身份仍然**只从注册表取**，理由与终端那一条完全相同：代次不能由
        客户端编，否则每一帧的 `_check_target` 都会判它过期。

        **不限速。** 这条推送是幂等的（客户端 latch 后写覆盖先写），不像
        `PushUIRequest` 会真的多开一个界面，因此不该消耗 `_open_limiter`
        的窗口——否则「先点终端、0.2 秒内再点取货口」会白丢一次身份。
        代价是自己一组计数桶，实机上分开看。
        """
        self._count("block_use_container")
        dimension = getattr(event, "dimensionId", 0)
        node_key = NodeKey(dimension, event.x, event.y, event.z)
        record = self.registry.get(node_key)
        if record is None:
            self._count("block_use_container_unknown")
            return
        player_id = getattr(event, "playerId", "")
        if not player_id:
            return
        payload = identity_payload(ui_key, record.ref, dimension)
        if send_device_identity(player_id, payload):
            self._count("container_identity_pushed")

    def _notify_ui(self, player_id, ui_key, params):
        # type: (str, str, dict) -> None
        """把「打开某个界面」推给一个玩家。

        引擎调用写在**方法体内**，与 `engine_player_dimension` 同一手法：
        适配器模块本身必须能离线导入，否则整条链路在没有 `mod.server.*`
        的环境里连构造都构造不了。

        客户端侧链路是现成的：`tooldelta/ui/event_handler.py` 已在监听
        `PushUIRequest`，收到后 `GetScreen(ui_key).PushUI(params=...)`。
        因此 `ui_key` 必须与 `@RegistToolDeltaScreen` 注册的键逐字相同，
        对不上时那边会抛异常（见 `common/definitions.py` 的 `TERMINAL_SCREENS`）。
        """
        if not player_id:
            return
        from ....tooldelta.events.server.ui import PushUIRequest

        PushUIRequest(ui_key=ui_key, params=params).send(player_id)
