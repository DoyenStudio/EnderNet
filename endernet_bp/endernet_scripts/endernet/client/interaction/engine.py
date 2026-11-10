# coding=utf-8
"""伪原版交互的客户端引擎胶水：相机原点、主手物品、潜行、挥手。

**每个方法体内才导入引擎**，本模块能离线导入；离线测试注入替身（只要同名方法）代替它。
相机接口只在 3.10 以后的引擎上有，本机接口桩只到 3.4；取不到时由调用方照拦、不发。
"""

from .ray import parse_ray, view_center


class EngineView(object):
    """`UseTakeover` 要的引擎读数与动作。"""

    def camera_origin(self):
        # type: () -> object
        """相机原点（世界坐标）：问视口中心那条屏幕射线的起点。取不到返回 None。"""
        from mod.client.extraClientApi import GetEngineCompFactory, GetLevelId

        factory = GetEngineCompFactory()
        center = view_center(factory.CreateGame(GetLevelId()).GetScreenViewInfo())
        ray = parse_ray(factory.CreateCamera(GetLevelId()).GetWorldRayFromScreenPoint(
            center[0], center[1]))
        return ray[0] if ray is not None else None

    def held_item_id(self):
        # type: () -> str
        from ....tooldelta.api.client.player import GetLocalPlayerMainhandItem

        item = GetLocalPlayerMainhandItem()
        return item.newItemName if item is not None else ""

    def sneaking(self):
        # type: () -> bool
        from mod.client.extraClientApi import GetEngineCompFactory, GetLocalPlayerId

        return bool(GetEngineCompFactory().CreatePlayer(GetLocalPlayerId()).isSneaking())

    def swing(self):
        # type: () -> bool
        """本地玩家挥一下手（原版攻击动作，`PlayerCompClient.Swing`）。写法照 mcdk 文档示例：
        组件用 levelId 建。"""
        from mod.client.extraClientApi import GetEngineCompFactory, GetLevelId

        return bool(GetEngineCompFactory().CreatePlayer(GetLevelId()).Swing())
