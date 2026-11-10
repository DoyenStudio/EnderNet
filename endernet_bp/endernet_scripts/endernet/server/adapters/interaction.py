# coding=utf-8
"""伪原版交互的服务端引擎出口（`server/interaction/` 的引擎胶水）。本模块是**引擎边界**，
引擎调用都在函数体内。

- 放置音效：`/playsound`，命令以放的那个玩家为执行者（坐标就落在他所在的维度），`@a` 让附近
  的玩家都听得到——与原版放方块一样。命令出口复用订单提示的那一个（`execute_sound_command`）。

服务端再拦一道原版物品使用的规则表（`interaction.UseGuard`）挂在 `events.ServerEventBinder`
上；可替换方块的判定（`MayPlace`）在 `world.ToolDeltaWorldAdapter.may_place`；扣 / 给物品的
引擎口子在 `channels/cable_intent.CableIntentPort` 与 `world.py`。
"""

from .order_notifications import execute_sound_command


def sound_command(name, position, volume, pitch):
    # type: (str, tuple, float, float) -> str
    """`/playsound <音效> @a <x y z> <音量> <音调>`。"""
    x, y, z = [float(value) for value in position]
    return "/playsound %s @a %.3f %.3f %.3f %.3g %.3g" % (
        name, x, y, z, float(volume), float(pitch))


def engine_play_sound(player_id, name, position, volume, pitch):
    # type: (str, str, tuple, float, float) -> bool
    """在 `position` 播一个原版音效（`feedback.PlaceFeedback` 的 `play_sound`）。"""
    if not player_id:
        return False
    return bool(execute_sound_command(sound_command(name, position, volume, pitch), player_id))
