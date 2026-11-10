# coding=utf-8
"""伪原版交互放成功之后的反馈：原版方块放置音效。**不 import 引擎**：播放经注入的
`play_sound(玩家, 音效名, 位置, 音量, 音调) -> bool`（生产实现
`adapters/interaction.py::engine_play_sound`，`/playsound` 命令）。

**音效取原版的**：本模组方块在 RP `blocks.json` 里的 `sound` 都是 `stone`，原版
`sounds.json` 的 `block_sounds.stone.events.place` 是 `dig.stone`（音量 1.0、音调 0.8～1.0，
这里取 0.9）。放成功才播（服务端判定放不放得下，放不上是静默的），对附近所有玩家播，
与原版放方块一样——所以在服务端、不在客户端（客户端那一下只挥手）。

挥手在客户端（`client/interaction/takeover.py`），不在这里。
"""

#: 方块放置音效：(音效名, 音量, 音调)。见模块说明
PLACE_SOUND = ("dig.stone", 1.0, 0.9)


class PlaceFeedback(object):
    """放成功的那一下：在那一格中心播放置音效。没给 `play_sound` 就只计数（离线、测试）。"""

    def __init__(self, play_sound=None, sound=PLACE_SOUND):
        self.play_sound = play_sound
        self.sound = sound
        self.stats = {"placed": 0, "played": 0, "play_failures": 0}

    def placed(self, player_id, node_key):
        # type: (str, object) -> bool
        """`node_key` 那一格放下了东西（面板、换上的光缆、新建的面板底座）。返回播没播。"""
        self.stats["placed"] += 1
        if self.play_sound is None:
            return False
        name, volume, pitch = self.sound
        center = (node_key.x + 0.5, node_key.y + 0.5, node_key.z + 0.5)
        try:
            ok = self.play_sound(player_id, name, center, volume, pitch) is not False
        except Exception:
            ok = False
        self.stats["played" if ok else "play_failures"] += 1
        return ok

    def diagnostics(self):
        # type: () -> dict
        return dict(self.stats)
