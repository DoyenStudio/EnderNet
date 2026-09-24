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

from ...common.identity import ChunkKey, NodeKey
from ..runtime.events import EventKind, NormalizedEvent


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
    # 注意**没有** `ServerBlockEntityTickEvent`：设备级 tick 是任务 04
    # 的休眠/唤醒机制要用的唤醒信号，本批次没有任何监听器消费它。
    # 04 接入时加上赋值即可，测试的反向检查会一并覆盖。

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

    def __init__(self, queue, world, registry, definitions, clock=None, event_api=None):
        self.queue = queue
        self.world = world
        self.registry = registry
        self.definitions = definitions
        self.clock = clock
        #: 引擎事件 API。None 表示首次 bind 时惰性构造真实实现。
        self.event_api = event_api
        self._bound = False
        #: `[(event_cls, func, priority)]`，unbind 时逐一注销
        self._registrations = []
        self.unregistered = 0
        #: 注销失败的条目（名字 + 异常文本）。非空说明有监听器没摘干净。
        self.unregister_failures = []
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
        self._count(kind)
        self.queue.push(NormalizedEvent(kind, tick=self._tick()))

    def diagnostics(self):
        # type: () -> dict
        return {
            "bound": self._bound,
            "event_counts": dict(self.counts),
        }
