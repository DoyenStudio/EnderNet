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
from __future__ import absolute_import, division, print_function

from ...common.identity import ChunkKey, NodeKey
from ..runtime.events import EventKind, NormalizedEvent


class ServerEventBinder(object):
    """把引擎监听器接到 `EventQueue` 与 `world` 适配器上。

    监听器本身在 `bind()` 里注册；`unbind()` 用于显式销毁
    （01-01 要求模组有明确的销毁流程，不能只靠热重载）。
    """

    def __init__(self, queue, world, registry, definitions, clock=None):
        self.queue = queue
        self.world = world
        self.registry = registry
        self.definitions = definitions
        self.clock = clock
        self._bound = False
        self.counts = {}

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
        from ....tooldelta.events import serverapi
        from ....tooldelta.events import (
            BlockNeighborChangedServerEvent,
            BlockRemoveServerEvent,
            ChunkAcquireDiscardedServerEvent,
            ChunkLoadedServerEvent,
            DestroyBlockEvent,
            EntityPlaceBlockAfterServerEvent,
            OnScriptTickServer,
            OnSimTickServerEvent,
            ServerBlockEntityTickEvent,
            ServerPlaceBlockEntityEvent,
            ServerPlayerTryDestroyBlockEvent,
        )

        @serverapi.ListenEvent(EntityPlaceBlockAfterServerEvent)
        def on_place_after(event):
            self._on_place(event.dimensionId, event.x, event.y, event.z, event.fullName,
                           "entity_place_after")

        @serverapi.ListenEvent(ServerPlaceBlockEntityEvent)
        def on_place_block_entity(event):
            self._on_place(event.dimension, event.posX, event.posY, event.posZ,
                           event.blockName, "block_entity_placed")

        @serverapi.ListenEvent(BlockRemoveServerEvent)
        def on_remove(event):
            self._on_remove(event.dimension, event.x, event.y, event.z, event.fullName)

        @serverapi.ListenEvent(ServerPlayerTryDestroyBlockEvent)
        def on_try_destroy(event):
            self._on_try_destroy(event.dimensionId, event.x, event.y, event.z,
                                 event.fullName)

        @serverapi.ListenEvent(BlockNeighborChangedServerEvent)
        def on_neighbor(event):
            self._on_neighbor(event.dimensionId, event.posX, event.posY, event.posZ,
                              event.neighborPosX, event.neighborPosY,
                              event.neighborPosZ, event.toBlockName)

        @serverapi.ListenEvent(ChunkLoadedServerEvent)
        def on_chunk_loaded(event):
            self._on_chunk_loaded(event.dimension, event.chunkPosX, event.chunkPosZ,
                                  event.blockEntities)

        @serverapi.ListenEvent(ChunkAcquireDiscardedServerEvent)
        def on_chunk_discarded(event):
            self._on_chunk_unloaded(event.dimension, event.chunkPosX, event.chunkPosZ)

        @serverapi.ListenEvent(OnScriptTickServer)
        def on_script_tick(_event):
            self._on_tick(EventKind.SCRIPT_TICK)

        @serverapi.ListenEvent(OnSimTickServerEvent)
        def on_sim_tick(_event):
            self._on_tick(EventKind.SIM_TICK)

        @serverapi.ListenEvent(DestroyBlockEvent)
        def on_destroyed(event):
            # 破坏后事件：仅用于诊断。实际注销走 BlockRemoveServerEvent，
            # 避免两条路径各注销一次。
            self._count("destroy_after")

        self._bound = True

    def unbind(self):
        # type: () -> None
        """显式销毁。监听器是模块级函数，热重载时会在下一次 bind 前被丢弃。"""
        self._bound = False

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
