# coding=utf-8
"""射线的起点：相机原点（2026-11-10 真机反馈后改）。**不 import 引擎**。

射线 = 相机原点 → 引擎给的命中点（`common/cable_shapes.click_ray`，与服务端右键开界面共用；
命中点的口径也写在那里）。起点取相机接口
`CreateCamera(levelId).GetWorldRayFromScreenPoint(视口中心)` 给的 `origin`（引擎读数在
`engine.EngineView.camera_origin`）。键鼠、触屏、第三人称走同一条路，与屏幕坐标的单位、
倍数、偏移都无关（此前换算触点的做法在触屏上整体偏左，已删，不留回退）。
"""


def view_center(view_info):
    # type: (object) -> tuple
    """`GetScreenViewInfo()`（宽、高、x 偏移、y 偏移）→ 视口中心（准星）的屏幕坐标。

    只拿来问相机原点：透视相机的每条屏幕射线都从同一个相机位置出发，取哪个屏幕点不影响
    起点，因此这里不管偏移与单位。
    """
    return (float(view_info[0]) / 2.0, float(view_info[1]) / 2.0)


def parse_ray(ray):
    # type: (object) -> object
    """`GetWorldRayFromScreenPoint` 的返回值 → `(起点, 方向)`；形状不对返回 None。"""
    if not isinstance(ray, dict):
        return None
    origin = ray.get("origin")
    direction = ray.get("direction")
    if not isinstance(origin, (list, tuple)) or not isinstance(direction, (list, tuple)):
        return None
    if len(origin) != 3 or len(direction) != 3:
        return None
    try:
        return tuple(float(value) for value in origin), tuple(float(value) for value in direction)
    except (TypeError, ValueError):
        return None
