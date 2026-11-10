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

from ...common.identity import FACING_NAMES, ChunkKey, NodeKey, PartKey  # noqa: F401 (NodeKey 仅用于 # type: 注释)
from ...common.resources import ResourceKey, item_dict_for_drop, item_dict_from_key
from ...common.resources import item_codec
from .engine_text import engine_item_data, engine_text

#: 光缆方块实体里存按面数据的两个字段（`.claude/13` §2）：被扳手切断的面（6 位掩码，
#: 第 i 位 = `identity.FACING_NAMES[i]`）与挂着的面板（`{面名: 面板设备标识}`）。
#: 领域层只认记录的 `config`（`devices/catalog.py` 的 `CABLE_CUTS_KEY` / `CABLE_PARTS_KEY`），
#: 引擎侧的字段名只在这里出现。
CABLE_CUTS_FIELD = "endernet:cuts"
CABLE_PARTS_FIELD = "endernet:parts"

#: 终端库存的实例标记（方块实体字段）。光缆面板（阶段 D）按面各一份，存在宿主光缆的
#: 方块实体里：`endernet_terminal_inventory_id_<面名>`。
TERMINAL_INVENTORY_FIELD = "endernet_terminal_inventory_id"

#: 每块面板自己的配置（面板骨架，2026-11-10）：按面各一份，存在宿主的方块实体里，
#: `endernet:part_config_<面名>`，值是一张字符串键的字典。挂上一块新面板时清空。
PART_CONFIG_FIELD = "endernet:part_config"

#: 方块原地换型（面板底座 ↔ 光缆）时要搬到新方块实体里的「按面」字段前缀
_PER_FACE_FIELDS = (TERMINAL_INVENTORY_FIELD, PART_CONFIG_FIELD)

#: 空气
AIR = "minecraft:air"


def terminal_inventory_field(key):
    # type: (object) -> tuple
    """`NodeKey` / `PartKey` → `(方块位置, 方块实体字段名)`。"""
    part = PartKey.of(key)
    if part.is_center():
        return part.node_key, TERMINAL_INVENTORY_FIELD
    return part.node_key, TERMINAL_INVENTORY_FIELD + "_" + part.slot


def part_config_field(part_key):
    # type: (PartKey) -> tuple
    """面板的 `PartKey` → `(宿主位置, 方块实体字段名)`。center 槽没有面板配置，报错。"""
    part = PartKey.of(part_key)
    if part.is_center():
        raise ValueError("center 槽没有面板配置: %r" % (part_key,))
    return part.node_key, PART_CONFIG_FIELD + "_" + part.slot


def per_face_fields():
    # type: () -> list
    """一格里按面存的全部方块实体字段名（库存标记、面板配置），换方块时整批搬过去。"""
    return [prefix + "_" + face for prefix in _PER_FACE_FIELDS for face in FACING_NAMES]


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

        六向状态优先，兼容仍使用水平朝向的其他设备与旧方块。
        """
        states = self.block_states_at(node_key) or {}
        if "minecraft:facing_direction" in states:
            return states["minecraft:facing_direction"]
        if "minecraft:cardinal_direction" in states:
            return states["minecraft:cardinal_direction"]
        return None

    def migrate_item_port_facing(self, node_key, facing):
        # type: (NodeKey, str) -> bool
        """升级旧物品口时只改朝向状态，不替换方块或碰真实库存。"""
        from ....tooldelta.api.server.block import UpdateBlockStates

        if "minecraft:facing_direction" not in (self.block_states_at(node_key) or {}):
            return False
        UpdateBlockStates(node_key.dimension, node_key.as_tuple(),
                          {"minecraft:facing_direction": facing})
        return self.facing_at(node_key) == facing

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

    def energy_block_entity(self, node_key):
        from ....tooldelta.api.server.block import GetBlockEntityData
        from ...common.definitions import ENERGY_INPUT_ID
        if self.block_name_at(node_key) != ENERGY_INPUT_ID:
            return None
        return GetBlockEntityData(node_key.dimension, node_key.as_tuple())

    def terminal_inventory_id(self, node_key):
        # type: (object) -> object
        """旧方块重载仍携带同一标记；同位置新放的方块获得自己的库存。

        收 `NodeKey`（整方块终端）或 `PartKey`（光缆面板，标记在宿主光缆的方块实体里、
        按面各一份）。
        """
        import random
        from ...common.resources.key import _TEXT_TYPES
        from ....tooldelta.api.server.block import GetBlockEntityData

        node_key, field = terminal_inventory_field(node_key)
        entity = GetBlockEntityData(node_key.dimension, node_key.as_tuple())
        if entity is None:
            return None
        token = entity[field]
        if token is None:
            token = "%032x" % random.getrandbits(128)
            entity[field] = token
            if entity[field] != token:
                return None
        if not isinstance(token, _TEXT_TYPES) or len(token) != 32:
            raise ValueError("invalid terminal inventory identity")
        return token

    def renew_terminal_inventory_id(self, part_key):
        # type: (PartKey) -> bool
        """给光缆的这一面换一个新的库存实例标记（挂上面板时调，阶段 D）。写不进去返回 False。

        同一面拆下再挂一块，库存不能接着用上一块的：整方块终端靠「新方块 = 新方块实体」
        自然做到，面板共用宿主的方块实体，只能在挂上时换标记。
        """
        import random
        from ....tooldelta.api.server.block import GetBlockEntityData

        node_key, field = terminal_inventory_field(part_key)
        entity = GetBlockEntityData(node_key.dimension, node_key.as_tuple())
        if entity is None:
            return False
        token = "%032x" % random.getrandbits(128)
        entity[field] = token
        return entity[field] == token

    def check_chunk_state(self, node_key):
        # type: (NodeKey) -> bool
        from ....tooldelta.api.server.world import CheckChunkState

        return bool(CheckChunkState(node_key.dimension, node_key.as_tuple()))

    # -- 末影光缆（阶段 C，13 §5 / §6）-----------------------------------

    def cable_entity_fields(self, node_key):
        # type: (NodeKey) -> object
        """光缆方块实体里的 `(切断掩码, 面板表)` 原值；方块实体还拿不到时返回 None。

        字段不存在时引擎给 None（新放下的光缆就是这样），原样交出，由领域层按「没切断、
        没面板」解释（`devices/cables.config_from_entity`）。走自定义数据接口
        `CreateBlockEntityData`（同 `energy_block_entity`），不用会做 NBT 转换的
        `GetBlockEntityDataDict`。
        """
        from ....tooldelta.api.server.block import GetBlockEntityData

        self.query_count += 1
        entity = GetBlockEntityData(node_key.dimension, node_key.as_tuple())
        if entity is None:
            return None
        return entity[CABLE_CUTS_FIELD], entity[CABLE_PARTS_FIELD]

    def write_cable_entity_fields(self, node_key, cuts, parts):
        # type: (NodeKey, int, dict) -> bool
        """把切断掩码与面板表写进光缆方块实体。写不进去（方块实体拿不到）返回 False。

        引擎只认整键赋值（不支持嵌套改写），字典的键必须是字符串：面板表整张重写，
        键与值都过 `engine_text`。写完读回切断掩码核一遍——读回不一致也按失败算。
        """
        from ....tooldelta.api.server.block import GetBlockEntityData

        entity = GetBlockEntityData(node_key.dimension, node_key.as_tuple())
        if entity is None:
            return False
        entity[CABLE_CUTS_FIELD] = int(cuts)
        entity[CABLE_PARTS_FIELD] = dict(
            (engine_text(face), engine_text(part_id)) for face, part_id in parts.items()
        )
        return entity[CABLE_CUTS_FIELD] == int(cuts)

    def update_block_states(self, node_key, changes, current=None):
        # type: (NodeKey, dict, dict) -> bool
        """把几个方块状态改成新值，**一次** `SetBlockStates` 写完（没改的键照原值带上）。

        `current` 是调用方刚读到的那份完整状态，给了就不再读一遍。返回是否写成。
        """
        from ....tooldelta.api.server.block import UpdateBlockStates

        self.query_count += 1
        previous = dict(current) if current is not None else None
        return bool(UpdateBlockStates(
            node_key.dimension, node_key.as_tuple(), dict(changes), previous
        ))

    def remove_block(self, node_key):
        # type: (NodeKey) -> bool
        """把这一格换成空气，**不掉落**（`SetBlockNew` 的旧方块处理取 0「替换」）。

        扳手整格拆光缆时用：物品已经直接给了玩家。引擎随后照常派发
        `BlockRemoveServerEvent`，拆除走与挖掉相同的流程。返回是否成功。
        """
        from ....tooldelta.api.server.block import SetBlock

        return bool(SetBlock(node_key.dimension, node_key.as_tuple(), AIR, 0, 0))

    # -- 面板底座与面板（2026-11-10）-----------------------------------------

    def set_block(self, node_key, block_id):
        # type: (NodeKey, str) -> bool
        """在这一格放一个方块（`SetBlockNew`，旧方块处理取 0「替换」，不掉落）。新建面板底座
        用：调用方已确认这一格是空气或可替换方块（`may_place`）。返回是否成功。"""
        from ....tooldelta.api.server.block import SetBlock

        return bool(SetBlock(node_key.dimension, node_key.as_tuple(), engine_text(block_id), 0, 0))

    def may_place(self, node_key, block_id, face):
        # type: (NodeKey, str, str) -> bool
        """`block_id` 能不能放进这一格：引擎的原版可放置判定
        （`BlockInfoComponentServer.MayPlace(方块, 位置, 朝向, 维度)`），这一格是草、水、雪层这类
        可替换方块时为真。`face` 是放置时点中的那一面（面名，换成引擎 `Facing`）。

        网易没有单独的「方块可不可替换」接口（方块基本信息字典里也没有这一项），这是最接近的
        现成判定；不自己维护方块名单。2026-11-10 新建面板底座用（空气不问它，直接放）。
        """
        from mod.server.extraServerApi import GetEngineCompFactory, GetLevelId
        from ...common.channels.cable_intent import engine_facing_of

        comp = GetEngineCompFactory().CreateBlockInfo(GetLevelId())
        return bool(comp.MayPlace(
            engine_text(block_id), node_key.as_tuple(), engine_facing_of(face), node_key.dimension
        ))

    def swap_faced_block(self, node_key, block_id, cuts, parts):
        # type: (NodeKey, str, int, dict) -> bool
        """面板宿主原地换方块（面板底座 ↔ 光缆）：先把旧方块实体里按面存的字段（库存标记、
        面板配置）读出来，`SetBlockNew` 换成 `block_id`（不掉落），再把这些字段与新的切断
        掩码、面板表写进新方块的方块实体，读回核对。

        新方块实体写不进去就把旧方块换回来、旧字段写回去（尽力而为），返回 False；调用方
        据此退还物品、不改注册表。成功返回 True。
        """
        from ....tooldelta.api.server.block import GetBlockEntityData, GetBlockName, SetBlock

        dimension, position = node_key.dimension, node_key.as_tuple()
        entity = GetBlockEntityData(dimension, position)
        if entity is None:
            return False
        old_block = GetBlockName(dimension, position)
        if not old_block:
            return False
        kept = {}
        for field in per_face_fields():
            value = entity[field]
            if value is not None:
                kept[field] = value
        old_cuts = entity[CABLE_CUTS_FIELD]
        old_parts = entity[CABLE_PARTS_FIELD]
        if not SetBlock(dimension, position, engine_text(block_id), 0, 0):
            return False
        if self._write_faced_entity(node_key, cuts, parts, kept):
            return True
        # 新方块实体拿不到或写不进去：换回去，免得面板凭空没了
        if SetBlock(dimension, position, engine_text(old_block), 0, 0):
            self._write_faced_entity(
                node_key, 0 if old_cuts is None else old_cuts, old_parts or {}, kept)
        return False

    def _write_faced_entity(self, node_key, cuts, parts, kept):
        # type: (NodeKey, int, dict, dict) -> bool
        from ....tooldelta.api.server.block import GetBlockEntityData

        entity = GetBlockEntityData(node_key.dimension, node_key.as_tuple())
        if entity is None:
            return False
        for field, value in kept.items():
            entity[field] = value
        if not self.write_cable_entity_fields(node_key, cuts, parts):
            return False
        return all(entity[field] == value for field, value in kept.items())

    def part_config(self, part_key):
        # type: (PartKey) -> object
        """这块面板自己的配置（字典）；字段不存在为 `{}`，方块实体拿不到返回 None。"""
        from ....tooldelta.api.server.block import GetBlockEntityData

        node_key, field = part_config_field(part_key)
        entity = GetBlockEntityData(node_key.dimension, node_key.as_tuple())
        if entity is None:
            return None
        value = entity[field]
        return dict(value) if isinstance(value, dict) else {}

    def write_part_config(self, part_key, data):
        # type: (PartKey, dict) -> bool
        """整张写这块面板的配置（键过 `engine_text`；值由调用方保证是引擎收的简单类型）。
        写不进去返回 False。"""
        from ....tooldelta.api.server.block import GetBlockEntityData

        node_key, field = part_config_field(part_key)
        entity = GetBlockEntityData(node_key.dimension, node_key.as_tuple())
        if entity is None:
            return False
        payload = dict((engine_text(key), value) for key, value in dict(data).items())
        entity[field] = payload
        return entity[field] is not None

    def give_items(self, player_id, item_ids):
        # type: (str, list) -> list
        """几件物品（各 1 个）放进玩家背包，返回**没放进去**的那些（背包满了）。

        逐件放：`SpawnItemToPlayerInv` 一次放多个时会「放进去一部分却返回失败」，
        那样调用方再把整份掉在地上就多出了物品。字典形状同背包写入
        （`containers/engine.py` 的 `set_player_item`）。
        """
        from mod.server.extraServerApi import GetEngineCompFactory, GetLevelId

        comp = GetEngineCompFactory().CreateItem(GetLevelId())
        leftovers = []
        for item_id in item_ids:
            data = engine_item_data(item_codec.with_engine_empty_values(
                item_dict_from_key(ResourceKey.item(item_id), 1)))
            if not comp.SpawnItemToPlayerInv(data, engine_text(player_id)):
                leftovers.append(item_id)
        return leftovers

    def register_block_remove_listener(self, block_ids):
        # type: (set) -> None
        """把方块加入移除事件白名单。

        **不做这一步，引擎根本不会派发 `BlockRemoveServerEvent`**，
        设备拆除将完全无感。必须在启动时对全部本模组方块调用一次。
        """
        from ....tooldelta.api.server.block import AddBlocksToBlockRemoveListener

        for block_id in sorted(block_ids):
            AddBlocksToBlockRemoveListener(set([block_id]))

    def drop_items_at(self, node_key, items):
        # type: (tuple, list) -> int
        """把几件物品掉在一个位置上。返回**真的生成出实体**的件数。

        参数是 `(维度, x, y, z)`（**不是 `NodeKey`**）：调用方是设备被拆时
        的那条路径，它手上只有存档键的前四项，而引擎要的也正是这四个数。

        `items` 是 `((ResourceKey, 数量), ...)`——**不是引擎物品字典**。
        掉落的字典形状是这一层的事：`CreateEngineItemEntity` 要**旧式键名**
        `itemName` / `auxValue`，而且 `customTips` / `durability` **不接受
        `None`**（2026-10-02 真机逐字段二分；见 `item_dict_for_drop`）。
        形状不对时引擎抛 `TypeError`，而 `TemplateStore` 把异常接成
        「一件也没掉」——**症状是拆了方块东西不掉、日志干净**。
        把构造放在这里，领域层就没机会拼错。

        引擎面是 `ServerSystem.CreateEngineItemEntity(itemDict, dimensionId, pos)`：
        **没有 tooldelta 封装**（`tooldelta/api/server/` 下没有它），因此这里
        直接走 `tooldelta.internal.GetServer()`。**没有改 vendored 子模块**——
        那是上游的东西，本仓库不维护。

        **返回值是判据**：写出去了但没生成实体（位置被占、维度不存在）是真实
        存在的一支，把它当成成功就是凭空少物。因此调用方按这个数决定哪些
        格子算清空了。
        """
        from ....tooldelta.internal import GetServer

        dimension, x, y, z = node_key
        # 方块中心：落在整格坐标上会让掉落物嵌在方块里。
        position = (x + 0.5, y + 0.5, z + 0.5)
        server = GetServer()
        spawned = 0
        for key, count in items or ():
            entity_id = server.CreateEngineItemEntity(
                item_dict_for_drop(key, count), dimension, position)
            if entity_id is not None:
                spawned += 1
        return spawned

    def diagnostics(self):
        # type: () -> dict
        return {
            "known_chunks": len(self._loaded_chunks),
            "cached_block_entities": sum(
                len(v) for v in self._chunk_entities.values()
            ),
            "queries": self.query_count,
        }
