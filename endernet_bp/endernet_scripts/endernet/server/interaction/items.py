# coding=utf-8
"""伪原版交互里的物品进出：从主手扣一个（创造模式不扣）、放不成退回去、拆下来的东西进背包
（满了掉在玩家脚下）。**不 import 引擎**：玩家与世界的读写都经注入的对象。

- `players`：`is_creative(玩家) -> bool / None`、`take_held_item(玩家, 物品) -> bool`
  （生产实现 `adapters/channels/cable_intent.CableIntentPort`）；
- `world`：`give_items(玩家, [物品]) -> [没放进去的]`、`drop_items_at((维度, x, y, z),
  [(ResourceKey, 数量)])`（生产实现 `adapters/world.py`）。

放面板、往面板底座里放光缆（扣）与扳手拆面板 / 拆光缆（给）都走这里；以后的面板种类照用。
"""

import math

from ...common.resources import ResourceKey


class Withdrawal(object):
    """扣下的一件：`charged` 为假是创造模式（没扣，失败时也不退）。"""

    __slots__ = ("player_id", "item_id", "charged")

    def __init__(self, player_id, item_id, charged):
        self.player_id = player_id
        self.item_id = item_id
        self.charged = bool(charged)

    def __repr__(self):
        return "Withdrawal(%s, %s)" % (self.item_id, "charged" if self.charged else "free")


class ItemExchange(object):
    """玩家手里与背包里的物品进出。各项计数见 `stats`。"""

    def __init__(self, players, world):
        self.players = players
        self.world = world
        self.stats = dict.fromkeys(
            ("taken", "free", "take_failures", "refunded", "given", "dropped"), 0)

    def is_creative(self, player_id):
        # type: (str) -> bool
        """创造模式为真；查不到（玩家刚离线、引擎抛了）按生存模式处理。"""
        asker = getattr(self.players, "is_creative", None)
        if asker is None:
            return False
        try:
            return asker(player_id) is True
        except Exception:
            return False

    def withdraw(self, player_id, item_id):
        # type: (str, str) -> object
        """从主手扣一个 `item_id`（创造模式不扣）。扣不成（换了物品、空了）返回 None。"""
        if self.is_creative(player_id):
            self.stats["free"] += 1
            return Withdrawal(player_id, item_id, False)
        try:
            ok = bool(self.players.take_held_item(player_id, item_id))
        except Exception:
            ok = False
        if not ok:
            self.stats["take_failures"] += 1
            return None
        self.stats["taken"] += 1
        return Withdrawal(player_id, item_id, True)

    def refund(self, withdrawal, dimension, position):
        # type: (Withdrawal, int, tuple) -> None
        """放不成，把扣下的那一件还给玩家（创造模式没扣，不还）。"""
        if withdrawal is None or not withdrawal.charged:
            return
        self.stats["refunded"] += 1
        self.hand_over(withdrawal.player_id, [withdrawal.item_id], dimension, position)

    def hand_over(self, player_id, item_ids, dimension, position):
        # type: (str, list, int, tuple) -> None
        """几件物品（各 1 个）进玩家背包；放不下的掉在 `position`（玩家脚下）那一格。"""
        leftovers = list(item_ids)
        giver = getattr(self.world, "give_items", None)
        if giver is not None:
            try:
                leftovers = list(giver(player_id, list(item_ids)))
            except Exception:
                leftovers = list(item_ids)
        self.stats["given"] += len(item_ids) - len(leftovers)
        if not leftovers:
            return
        at = (dimension,) + tuple(int(math.floor(value)) for value in position[:3])
        dropper = getattr(self.world, "drop_items_at", None)
        if dropper is not None:
            dropper(at, [(ResourceKey.item(item_id), 1) for item_id in leftovers])
        self.stats["dropped"] += len(leftovers)

    def diagnostics(self):
        # type: () -> dict
        return dict(self.stats)
