# coding=utf-8
"""服务端装配与启动/销毁。

对应任务 01-01、01-06 的入口与分帧补查部分。

**本批次的范围。** 装配 01 的设备层与 02 的拓扑/负载层。主控、存储、工厂、
能源服务属于 03 及以后，本文件不提前把它们挂上去，也不假装网络已经可以工作。

**可以脱离游戏构造。** 适配器把引擎调用放在方法体内部而不是模块顶层，
所以本模块导入时不需要 `mod.server.*`；离线测试传 `bind_engine=False`
并自己注入假 world 即可驱动整条链路。
"""

from ..common.definitions import DefinitionSet
from ..common.identity import ChunkKey
from .adapters.events import ServerEventBinder
from .adapters.world import ToolDeltaWorldAdapter
from .devices.catalog import DeviceCatalog
from .devices.registry import DeviceRegistry
from .network.admission import AdmissionService
from .network.topology import TopologyService
from .runtime.events import EventKind, EventQueue
from .runtime.lifecycle import DeviceLifecycle


class ServerBootstrap(object):
    """服务端运行时装配。

    `bind_engine=False` 时不注册任何引擎监听器，也不会调用引擎接口，
    用于离线验证；游戏内由 `start_server()` 以默认参数启动。
    """

    def __init__(
        self,
        definitions=None,
        world=None,
        drain_limit=256,
        backfill_chunks_per_tick=4,
        rebuild_steps_per_tick=64,
        change_sink=None,
        max_tick_errors=8,
    ):
        # type: (DefinitionSet, object, int, int, int, object, int) -> None
        self.definitions = definitions if definitions is not None else DefinitionSet()
        self.world = world if world is not None else ToolDeltaWorldAdapter()
        self.drain_limit = int(drain_limit)
        self.backfill_chunks_per_tick = int(backfill_chunks_per_tick)
        #: 每帧允许展开多少个图节点。跨帧重建的预算（02-02）。
        self.rebuild_steps_per_tick = int(rebuild_steps_per_tick)
        #: **额外**的设备变化观察者。拓扑服务始终消费设备变化，
        #: 这个钩子只是让测试与后续服务也能看到同一批变化。
        self.change_sink = change_sink

        self.registry = DeviceRegistry(self.definitions)
        self.catalog = DeviceCatalog(self.definitions)
        self.queue = EventQueue()
        self.lifecycle = DeviceLifecycle(
            self.definitions, self.registry, self.catalog, world=self.world
        )
        self.topology = TopologyService(
            self.definitions, self.registry, self.catalog
        )
        self.admission = AdmissionService(self.definitions, self.registry, self.topology)
        self.event_binder = ServerEventBinder(
            self.queue, self.world, self.registry, self.definitions
        )

        self.started = False
        self.tick_count = 0
        self.max_tick_errors = int(max_tick_errors)
        self.tick_errors = []
        self.tick_error_count = 0
        self._backfill_generator = None
        #: 一次性补查是否已跑完。区块发现主要靠 CHUNK_LOADED 事件，
        #: 补查只是兜底与手动重扫入口，因此跑完就不再重建。
        self._backfill_finished = False
        self._change_counts = {}
        self._change_overflow = 0
        if change_sink is None:
            self._recent_changes = []
        else:
            self._recent_changes = None

    # -- 生命周期 ------------------------------------------------------

    def start(self, bind_engine=True):
        # type: (bool) -> None
        """启动。

        `bind_engine=False` 时**完全不碰引擎**：不注册白名单、不注册监听器。
        离线环境下 `tooldelta.events` 无法导入（它依赖 mod.client.extraClientApi），
        因此这条路径必须在导入之前就分开。
        """
        if self.started:
            return
        if bind_engine:
            # 顺序是有意的：先把本模组方块加入移除事件白名单，再注册监听器，
            # 否则启动瞬间的拆除事件会被引擎直接丢掉。
            self.event_binder.register_remove_whitelist()
            self.event_binder.bind()
            self._bind_tick_driver()
        self._backfill_generator = None
        self._backfill_finished = False
        # 启动时全量标脏：注册表里可能已有记录（重启、手动重扫），
        # 图必须由它们重新推导，而不是沿用上一轮的内存状态。
        self.topology.mark_all_dirty()
        self.started = True

    def _bind_tick_driver(self):
        """把 `OnScriptTickServer` 接到 `tick()` 上。

        **没有这一步整条链路是死的**：事件会一直入队，但没人消费。
        （真机验证时就是这么发现的——事件计数一直在涨，而 `ticks` 恒为 0。）

        任务 01 只负责「入队 → 消费 → 产生设备变化」这一段。
        把 tick 变成有预算、可轮转、可休眠的调度属于任务 04：
        届时由调度器调用 `tick()`，这里改成不注册即可，领域代码不受影响。
        """
        from ...tooldelta.events import OnScriptTickServer, serverapi

        @serverapi.ListenEvent(OnScriptTickServer)
        def _drive(_event):
            self.drive_once()

    def drive_once(self):
        # type: () -> None
        """推进一帧。异常只记录不抛出。

        引擎会以每秒数十次的频率调用它，任何一次抛出都会变成刷屏日志，
        而且会让后续帧全部失效。因此这里捕获并记下前若干条堆栈，
        既保证循环继续，又不把错误藏起来——诊断里能看到 `tick_errors`。
        """
        import traceback

        try:
            self.tick()
        except Exception:
            self._record_tick_error(traceback.format_exc())

    def _record_tick_error(self, formatted):
        # type: (str) -> None
        self.tick_error_count += 1
        # 只保留前若干条完整堆栈，避免一次持续故障把内存吃光。
        if len(self.tick_errors) < self.max_tick_errors:
            self.tick_errors.append(formatted)

    def stop(self):
        # type: () -> None
        """显式销毁。要求 hot reload 与退出时不残留监听器与待办。"""
        if not self.started:
            return
        self.event_binder.unbind()
        self.queue.clear()
        self._backfill_generator = None
        self._backfill_finished = False
        self.started = False

    # -- 推进 ----------------------------------------------------------

    def tick(self):
        # type: () -> dict
        """推进一次：先补查，再消费事件。

        补查与事件消费都在**单帧内限量**：一次世界加载可能带来上千个区块，
        不做限量会直接卡住服务端主线程。
        """
        if not self.started:
            raise RuntimeError("bootstrap 未启动")

        self.tick_count += 1
        backfilled = self._advance_backfill()
        drained, changes = self._drain_events()
        # 事件消费完再重建：同帧的多次变化只算一次重建（02-02）。
        rebuild = self.topology.advance(self.rebuild_steps_per_tick)

        return {
            "tick": self.tick_count,
            "backfilled_chunks": backfilled,
            #: 本帧消费的事件条数——它才是受 `drain_limit` 约束的量
            "events_drained": drained,
            #: 事件翻译出的设备变化条数。一次放置通常不止一条变化
            #: （放下本身 + 邻接设备需要标脏），因此两者不相等。
            "changes": len(changes),
            #: 本帧展开的图节点数与重建完成的分量数，供预算观测
            "topology_steps": rebuild.steps,
            "components_rebuilt": rebuild.components_rebuilt,
            "topology_version": self.topology.version,
            "queue": self.queue.stats(),
        }

    def _drain_events(self):
        # type: () -> tuple
        batch = self.queue.drain(limit=self.drain_limit)
        changes = []
        for event in batch:
            changes.extend(self.lifecycle.handle(event))
        self._emit(changes)
        return len(batch), changes

    def _advance_backfill(self):
        # type: () -> int
        """分帧补查已加载区块。

        **区块发现的主要路径是 `CHUNK_LOADED` 事件，不是这里。**
        世界在脚本初始化时尚未加载，之后每个区块加载都会触发事件并附带
        该区块的方块实体列表，因此不会漏。本方法只是兜底与手动重扫入口，
        跑完一次即停止，不每帧重建生成器——否则已知区块越多，
        每帧空转的代价越大。
        """
        if self._backfill_finished:
            return 0
        if self._backfill_generator is None:
            self._backfill_generator = self.lifecycle.plan_startup_backfill()
        done = 0
        while done < self.backfill_chunks_per_tick:
            try:
                next(self._backfill_generator)
            except StopIteration:
                self._backfill_generator = None
                self._backfill_finished = True
                break
            done += 1
        return done

    def reset_backfill(self):
        # type: () -> None
        """重新扫描全部已知区块。用于世界重载或手动重扫。

        两处状态都要清：本对象的分帧游标，以及生命周期里「该区块已补查过」
        的标记。只清前者不会真的重扫——后者会让生成器把每个区块都跳过，
        看起来像成功了其实什么都没做。
        重复扫描是安全的：注册表幂等，不会重复建实例。
        """
        self._backfill_generator = None
        self._backfill_finished = False
        self.lifecycle.forget_backfill()

    def _emit(self, changes):
        # type: (list) -> None
        for change in changes:
            self._change_counts[change.kind] = self._change_counts.get(change.kind, 0) + 1
            # 拓扑是设备变化的固定消费者：它只标脏，真正的重建在 `tick()` 里
            # 有限步完成，因此这里不会拖慢事件消费。
            self.topology.on_lifecycle_change(change)
        if self.change_sink is not None:
            for change in changes:
                self.change_sink(change)
        elif self._recent_changes is not None:
            # 没有额外观察者时只保留最近一批，避免无限堆积。
            self._recent_changes = changes

    # -- 测试与诊断 ----------------------------------------------------

    def push(self, event):
        # type: (object) -> bool
        return self.queue.push(event)

    def recent_changes(self):
        # type: () -> list
        """最近一批设备变化。仅在未注入 change_sink 时可用。"""
        if self._recent_changes is None:
            raise RuntimeError("已注入 change_sink，变化不再在此保留")
        return list(self._recent_changes)

    def force_backfill(self, chunk_keys):
        # type: (list) -> int
        """立即补查指定区块（测试用，不走分帧）。"""
        return sum(1 for _ in self.lifecycle.plan_startup_backfill(chunk_keys=chunk_keys))

    def diagnostics(self):
        # type: () -> dict
        return {
            "started": self.started,
            "ticks": self.tick_count,
            "tick_errors": {
                "count": self.tick_error_count,
                "samples": list(self.tick_errors),
            },
            "definitions": {
                "devices": len(self.definitions.devices),
                "mediums": len(self.definitions.mediums),
                "controllers": sorted(self.definitions.controller_ids),
            },
            "registry": self.registry.stats(),
            "lifecycle": self.lifecycle.diagnostics(),
            "events": self.event_binder.diagnostics(),
            "queue": self.queue.stats(),
            "changes": dict(self._change_counts),
            "topology": self.topology.diagnostics(),
            "admission": self.admission.diagnostics(),
            "world": self.world.diagnostics(),
            "scope_note": (
                "已装配任务 01 的设备层与任务 02 的拓扑/负载层；"
                "主控、存储、工厂与能源服务尚未接入。"
            ),
        }


#: 进程内的服务端单例
_SERVER = None


def get_server():
    # type: () -> ServerBootstrap
    return _SERVER


def start_server():
    # type: () -> ServerBootstrap
    """由 `modMain.OnServerInited` 调用。"""
    global _SERVER
    if _SERVER is not None:
        _SERVER.stop()
    _SERVER = ServerBootstrap()
    _SERVER.start()
    return _SERVER


def stop_server():
    # type: () -> None
    """显式销毁。热重载或退出时调用。"""
    global _SERVER
    if _SERVER is not None:
        _SERVER.stop()
        _SERVER = None
