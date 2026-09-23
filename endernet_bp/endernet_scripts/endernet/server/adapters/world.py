# coding=utf-8
"""世界查询适配器：本模块是**引擎边界**，唯一允许 import tooldelta/mod.* 的地方之一。

对应任务 01-05/01-06。领域层只透过这里的方法拿到世界事实，
因此这些方法必须在脱离游戏时整体缺席——它们本来就不该被离线测试调用。

**关于区块枚举。** ModSDK 没有提供「枚举某区块内全部自定义方块实体」的接口，
但也不需要：**脚本初始化时世界尚未加载**，之后每个区块的加载都会触发
`ChunkLoadedServerEvent`，事件携带该区块的方块实体列表。
因此「缓存引擎告知过的事实」这一策略是完整的——

- 冷启动：初始化时没有任何区块已加载，所有区块都在脚本就位之后才加载，
  每一个都会发事件，不会漏；
- 因此不存在「脚本初始化之前就已加载、之后再未触发加载事件的区块」这种情况。

两个仍然成立的前提，需要实测确认但已不影响本策略的正确性：

1. `ChunkLoadedServerEvent.blockEntities` 必须完整列出该区块内**声明了
   `netease:block_entity` 的**方块实体（见交付说明未验证项 4）。
2. 区块在被卸载后重新加载时会再次发事件，从而重新挂载设备。
"""
from __future__ import absolute_import, division, print_function

from ...common.identity import ChunkKey, NodeKey


class ToolDeltaWorldAdapter(object):
    """基于 tooldelta 的世界适配器。"""

    def __init__(self):
        #: `chunk_key.as_key() -> [block_entity_dict]`，只保存引擎告知过的事实
        self._chunk_entities = {}
        self._loaded_chunks = set()
        self.query_count = 0

    # -- 区块缓存（由事件适配器喂入）-----------------------------------

    def note_chunk_loaded(self, chunk_key, block_entities=()):
        # type: (ChunkKey, list) -> None
        self._loaded_chunks.add(chunk_key.as_key())
        self._chunk_entities[chunk_key.as_key()] = list(block_entities or ())

    def note_chunk_unloaded(self, chunk_key):
        # type: (ChunkKey) -> None
        self._loaded_chunks.discard(chunk_key.as_key())
        self._chunk_entities.pop(chunk_key.as_key(), None)

    def forget_all_chunks(self):
        # type: () -> None
        """世界重载时调用。"""
        self._chunk_entities = {}
        self._loaded_chunks = set()

    def list_loaded_chunks(self):
        # type: () -> list
        """当前已知已加载的区块。来源是引擎的区块加载事件。"""
        out = []
        for key_tuple in sorted(self._loaded_chunks):
            out.append(ChunkKey(key_tuple[0], key_tuple[1], key_tuple[2]))
        return out

    def block_entities_in_chunk(self, chunk_key):
        # type: (ChunkKey) -> list
        """该区块上次加载时引擎报告的方块实体。"""
        return list(self._chunk_entities.get(chunk_key.as_key(), ()))

    # -- 世界查询 ------------------------------------------------------

    def block_name_at(self, node_key):
        # type: (NodeKey) -> str
        from ....tooldelta.api.server.block import GetBlockName

        self.query_count += 1
        return GetBlockName(node_key.dimension, node_key.as_tuple())

    def block_states_at(self, node_key):
        # type: (NodeKey) -> dict
        from ....tooldelta.api.server.block import GetBlockStates

        self.query_count += 1
        return GetBlockStates(node_key.dimension, node_key.as_tuple())

    def facing_at(self, node_key):
        # type: (NodeKey) -> object
        """方块的朝向。方块没有朝向状态时返回 None，而不是抛异常。

        未验证：endernet 方块是否真的带 `minecraft:cardinal_direction`。
        若实测缺失，方向性端口会退化为无方向，需要先给方块补 traits。
        """
        states = self.block_states_at(node_key) or {}
        if "minecraft:cardinal_direction" in states:
            return states["minecraft:cardinal_direction"]
        if "minecraft:facing_direction" in states:
            return states["minecraft:facing_direction"]
        return None

    def get_block_entity_data(self, node_key):
        # type: (NodeKey) -> dict
        """读取自定义方块实体数据。不可读取时返回 None。

        未验证：返回 None 的确切时机（区块正在卸载、方块实体尚未创建等）。
        调用方必须把 None 当作「暂时读不到」，不得当作「没有配置」。
        """
        from ....tooldelta.api.server.block import GetBlockEntityDataDict

        self.query_count += 1
        return GetBlockEntityDataDict(node_key.dimension, node_key.as_tuple())

    def set_block_entity_data(self, node_key, data):
        # type: (NodeKey, dict) -> None
        from ....tooldelta.api.server.block import SetBlockEntityData

        SetBlockEntityData(node_key.dimension, node_key.as_tuple(), data)

    def check_chunk_state(self, node_key):
        # type: (NodeKey) -> bool
        from ....tooldelta.api.server.world import CheckChunkState

        return bool(CheckChunkState(node_key.dimension, node_key.as_tuple()))

    def register_block_remove_listener(self, block_ids):
        # type: (set) -> None
        """把方块加入移除事件白名单。

        **不做这一步，引擎根本不会派发 `BlockRemoveServerEvent`**，
        设备拆除将完全无感。必须在启动时对全部本模组方块调用一次。
        """
        from ....tooldelta.api.server.block import AddBlocksToBlockRemoveListener

        for block_id in sorted(block_ids):
            AddBlocksToBlockRemoveListener(set([block_id]))

    def diagnostics(self):
        # type: () -> dict
        return {
            "known_chunks": len(self._loaded_chunks),
            "cached_block_entities": sum(
                len(v) for v in self._chunk_entities.values()
            ),
            "queries": self.query_count,
        }
