# coding=utf-8
"""光缆的精确交互：客户端 → 服务端的「意图」通道（`.claude/13` §6）。**不 import 引擎**。

## 流程

1. 客户端点中方块（`ClientBlockUseEvent` / `ClientItemUseOnEvent`），按手持物品、是否潜行与
   被点的方块选出动作（`action_for`）；选不出就不管，原版交互照常（贴着光缆放方块之类）。
2. 选出了：拦掉原版交互，取一条射线，把意图发给服务端——被点的方块、客户端看到的方块与
   手持物品、动作、被点的那一面、射线起点与方向（世界坐标），并挥一下手。监听、「一次按下」、
   拦截、射线、挥手都在伪原版交互那一层（`client/interaction/`，2026-11-10 收拢）。
3. 服务端（`server/devices/cable_intents.py`）校验距离、方块、手持物品，
   **用该格当前的方块状态**与射线求交（`common/cable_shapes.pick`）判出点中的部件，
   再按动作分派。

## 按一次算一次，没有冷却（作者 2026-11-10 要求去掉冷却）

此前照蔚蓝管道两端各有一个 0.5 秒的时间窗，快速连点会被吞掉，作者认为像「内置 cd」，
要求去掉。现在：

- 客户端按「按下」算（`client/interaction/press.PressTracker`）：相邻使用事件间隔不超过
  1 个游戏刻的算同一次按下（不论目标、手持物品、潜行）。一次按下只要被接管过（发过意图），
  直到松手（断开超过 1 个游戏刻），这期间**所有**方块使用、物品使用事件都拦掉原版交互、
  不再发意图——第一下把面板底座换成光缆后玩家还按着右键，后续事件点在新光缆上也不会再让
  原版多放一格（2026-11-10 真机反馈）。一次点击的两条事件只发一次，按住不重复，快速连点
  每一下都生效；
- 服务端只防刷：同一玩家同一个服务端 tick 内只处理第一条，多出来的静默丢弃。没有秒级冷却；
  另以服务端为准再拦一道原版物品使用（`server/interaction/guard.py`：手持光缆点面板底座）。

## 射线怎么来（2026-11-10 真机反馈后改）

两条客户端事件本身就带被点的方块坐标与格内命中点 `clickX/Y/Z`：引擎沿真实的触点或准星
射线求出来的，落在该格 clip 盒（`netease:aabb` 的外包盒）的表面上。**口径**：文档原文
「点击点的 x 比例位置」，即命中点相对方块最小角的格内坐标（各轴 0～1，不是相对 clip 盒的
比例），蔚蓝管道的扳手（`transmitters/base/action.py::get_pick_facing`）正是这样拿它与
格内阈值比的。

射线 = 相机原点 → 命中点（方块坐标 + click）：起点取相机接口
`CreateCamera(levelId).GetWorldRayFromScreenPoint(视口中心)` 给的 `origin`，方向 = 命中点 −
起点（归一化，`client/interaction/ray.click_ray`）。键鼠、触屏、第三人称走同一条路，
与屏幕坐标的单位、倍数、偏移都无关（此前换算触点的做法在触屏上整体偏左，已删）。

**射线发给服务端、在那边求交**：客户端拿不到方块状态（`GetBlockStates` 只有服务端版，
客户端事件只给 aux 值，按位解码是蔚蓝踩过的坑），不知道哪几面有臂、哪几面挂着面板；
自己求交会把「这一面要是有臂就会点中臂」的位置当成点中。服务端读得到当前状态，
求交函数两端是同一份（`cable_shapes`）。

## 动作

| 动作 | 客户端什么时候发 | 服务端要求的手持物品 |
| --- | --- | --- |
| `cut`（切断 / 恢复某一面） | 手持扳手、不潜行，点光缆或面板底座 | 扳手 |
| `dismantle`（拆面板或光缆） | 手持扳手、潜行，点光缆或面板底座 | 扳手 |
| `place_panel`（挂一块面板） | 手持面板物品点**任何**方块：不潜行时右键会开界面的本模组设备除外；潜行时连它们（以及箱子等原版会开界面的方块）也挂（2026-11-10） | 那块面板物品 |
| `add_cable`（面板底座换成光缆） | 手持光缆物品、不潜行，点面板底座 | 光缆物品（致密的由服务端静默不放） |
| `tunnel_input`（隧道设为输入 / 清空配置卡） | 手持配置卡、潜行，点任何方块 | 配置卡 |
| `tunnel_output`（隧道设为输出） | 手持配置卡、不潜行，点光缆或面板底座 | 配置卡 |
| `wireless_link`（选中 / 配对无线连接器） | 手持无线连接工具、不潜行，点无线连接器 | 无线连接工具 |
| `wireless_unlink`（解除配对 / 清空工具） | 手持无线连接工具、潜行，点任何方块 | 无线连接工具 |

`place_panel` / `add_cable` 的处理函数在 `server/devices/panels.py`，配置卡两条在
`server/devices/tunnels.py`，无线连接工具两条在 `server/devices/wireless.py`，都经
`CableIntentService.register` 登记。挂面板的放置规则（照 AE2，2026-11-10）见 `panels.py`。
配置卡、无线连接工具潜行点到别处时清空自己记着的东西（点空气没有方块事件，不接管）。

**打开面板不是意图**（2026-11-10 收拢）：空手或手持别的东西右键面板，与右键整方块终端走同一个
服务端入口（`server/interaction/opening.py`），原版那次放方块一并取消。点没点中面板只有服务端
知道（客户端拿不到方块状态），客户端拦下来就没法再把这一下还给原版（没点中时该照常贴着光缆
放方块），所以客户端不接管，交给服务端的原版事件判。

## 载荷（C2S `endernet:cable_intent`，字段严格：缺项与多项都拒）

| 字段 | 内容 |
| --- | --- |
| `pos` | 被点的方块 `[x, y, z]` |
| `block` | 客户端看到的方块标识；服务端拿它和注册表（或世界）对，对不上就是点了个已经换掉的方块 |
| `item` | 客户端看到的手持物品标识，空手为 `""`；服务端自己再读一遍主手物品，以它为准 |
| `action` | 动作 |
| `face` | 被点击的那一面（面名，`FACING_NAMES`）；事件没带（`ClientBlockUseEvent`）为 `""`，服务端按该格的外包盒自己求 |
| `origin` | 射线起点 `[x, y, z]`（世界坐标） |
| `direction` | 射线方向 `[x, y, z]`（不必归一） |

身份只认引擎附的 `__id__`；维度由服务端按玩家查，不收。
"""

import math

from ..definitions import (
    CABLES,
    CONFIG_CARD_ID,
    CONTAINER_SCREENS,
    PART_HOLDERS,
    PARTS,
    TERMINAL_SCREENS,
    WIRELESS_CONNECTOR_ID,
    WIRELESS_LINK_TOOL_ID,
    WRENCH_ID,
)
from ..identity import FACING_NAMES
from ..protocol import _require_fields

#: 事件名
CABLE_INTENT = "endernet:cable_intent"
#: 线上字段
INTENT_FIELDS = ("pos", "block", "item", "action", "face", "origin", "direction")

#: 动作（13 §6、§7；`add_cable` 2026-11-10）
ACTION_CUT = "cut"
ACTION_DISMANTLE = "dismantle"
ACTION_PLACE_PANEL = "place_panel"
ACTION_ADD_CABLE = "add_cable"
#: 配置卡与无线连接工具（2026-11-13）
ACTION_TUNNEL_INPUT = "tunnel_input"
ACTION_TUNNEL_OUTPUT = "tunnel_output"
ACTION_WIRELESS_LINK = "wireless_link"
ACTION_WIRELESS_UNLINK = "wireless_unlink"

#: 引擎 `Facing` 枚举（Down 0、Up 1、North 2、South 3、West 4、East 5）→ 面名。
#: `ClientItemUseOnEvent.face` 是这个枚举，线上一律传面名。
ENGINE_FACING_NAMES = ("down", "up", "north", "south", "west", "east")


def face_of_engine(value):
    # type: (object) -> str
    """引擎 `Facing` 值 → 面名；不是 0～5 的整数（或没给）返回 `""`。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return ""
    if 0 <= value < len(ENGINE_FACING_NAMES):
        return ENGINE_FACING_NAMES[value]
    return ""


def engine_facing_of(face):
    # type: (str) -> int
    """面名 → 引擎 `Facing` 值（`face_of_engine` 的反方向；服务端问 `MayPlace` 用）。"""
    return ENGINE_FACING_NAMES.index(face)


try:  # pragma: no cover - 仅在 Python 2 下成立
    _TEXT_TYPES = (str, unicode)
    _INT_TYPES = (int, long)
except NameError:  # pragma: no cover - 仅在 Python 3 下成立
    _TEXT_TYPES = (str,)
    _INT_TYPES = (int,)


#: 面板物品（13 §2：`*_terminal_part`；2026-11-13 加 P2P 网络隧道）
PANEL_ITEM_IDS = frozenset(definition.id for definition in PARTS)
#: 无线连接器（工具只对它配对）
WIRELESS_CONNECTOR_IDS = frozenset([WIRELESS_CONNECTOR_ID])
#: 光缆（方块标识，也是它的物品标识）
CABLE_IDS = frozenset(definition.id for definition in CABLES)
#: 面板底座（2026-11-10）
HOLDER_IDS = frozenset(definition.id for definition in PART_HOLDERS)
#: 按面的方块：光缆与面板底座。扳手只对它们起作用；客户端的方块使用事件（不带面）也只
#: 对它们接管，别的方块要等同一刻随后带面的物品事件
FACED_BLOCK_IDS = CABLE_IDS | HOLDER_IDS
#: 能挂面板的方块（玻璃、普通光缆与面板底座；致密不挂，13 §0-8）。服务端右键开界面时只对
#: 它们求交找面板（`server/devices/panels.py::claim_open`）
PANEL_HOST_IDS = frozenset(
    definition.id for definition in CABLES + PART_HOLDERS if definition.accepts_parts
)
#: 右键会开界面的本模组设备（终端、容器类）。手持面板点它们照旧开界面、不挂面板——与
#: 「手持物品右键终端只开界面」（`adapters/events._on_item_use_on`）同一口径。
INTERACTIVE_BLOCK_IDS = frozenset(TERMINAL_SCREENS) | frozenset(CONTAINER_SCREENS)


def action_for(item_id, sneaking, block=None):
    # type: (str, bool, object) -> object
    """客户端：这一下按手持物品、潜行与被点的方块该发哪个动作；`None` 表示不管（原版照常）。

    `block` 是被点的方块标识；不给（C 阶段的调用形状）就只认扳手。

    - 扳手：只对光缆与面板底座；
    - 面板物品：任何方块都拦下来挂面板（放在哪由服务端照 AE2 定）。不潜行时右键会开界面的
      本模组设备除外（照旧开界面）；**潜行时也挂**（2026-11-10，作者确认按推荐），这样能贴到
      终端、箱子这类会开界面的方块上——AE2 同样潜行能挂；
    - 光缆物品（不潜行）点面板底座：换成光缆；
    - 配置卡：潜行点任何方块（点中隧道设为输入，别处清空卡）；不潜行只对光缆与面板底座
      （点中隧道设为输出）；
    - 无线连接工具：不潜行只对无线连接器（选中或配对）；潜行点任何方块（连接器解除配对，
      别处清空工具）。

    空手与别的物品一律不管：右键面板开界面由服务端的原版事件判（模块说明「打开面板不是意图」）。
    """
    if item_id == WRENCH_ID:
        if block is not None and block not in FACED_BLOCK_IDS:
            return None
        return ACTION_DISMANTLE if sneaking else ACTION_CUT
    if block is None:
        return None
    if item_id == CONFIG_CARD_ID:
        if sneaking:
            return ACTION_TUNNEL_INPUT
        return ACTION_TUNNEL_OUTPUT if block in FACED_BLOCK_IDS else None
    if item_id == WIRELESS_LINK_TOOL_ID:
        if sneaking:
            return ACTION_WIRELESS_UNLINK
        return ACTION_WIRELESS_LINK if block in WIRELESS_CONNECTOR_IDS else None
    if item_id in PANEL_ITEM_IDS:
        if sneaking:
            return ACTION_PLACE_PANEL
        return None if block in INTERACTIVE_BLOCK_IDS else ACTION_PLACE_PANEL
    if sneaking:
        return None
    if item_id in CABLE_IDS and block in HOLDER_IDS:
        return ACTION_ADD_CABLE
    return None


class CableIntent(object):
    """解析好的一条意图。值对象；`parse_intent` 是唯一的构造入口。"""

    __slots__ = ("pos", "block", "item", "action", "face", "origin", "direction")

    def __init__(self, pos, block, item, action, origin, direction, face=""):
        # type: (tuple, str, str, str, tuple, tuple, str) -> None
        self.pos = pos
        self.block = block
        self.item = item
        self.action = action
        #: 被点击的那一面（面名）；`""` = 事件没带，服务端自己求
        self.face = face
        self.origin = origin
        self.direction = direction

    def local_origin(self):
        # type: () -> tuple
        """射线起点换到被点那一格的格内坐标（方块最小角为原点）。"""
        return tuple(self.origin[axis] - self.pos[axis] for axis in range(3))

    def to_dict(self):
        # type: () -> dict
        return {
            "pos": list(self.pos),
            "block": self.block,
            "item": self.item,
            "action": self.action,
            "face": self.face,
            "origin": list(self.origin),
            "direction": list(self.direction),
        }

    def __repr__(self):
        return "CableIntent(%s @ %r)" % (self.action, self.pos)


def intent_payload(pos, block, item, action, origin, direction, face=""):
    # type: (tuple, str, str, str, tuple, tuple, str) -> dict
    """客户端侧：编成线上字典。**唯一的编码点**；编码前按同一套规则校验一遍。"""
    payload = {
        "pos": [int(value) for value in pos],
        "block": block,
        "item": item or "",
        "action": action,
        "face": face or "",
        "origin": [float(value) for value in origin],
        "direction": [float(value) for value in direction],
    }
    parse_intent(payload)
    return payload


def parse_intent(data):
    # type: (dict) -> CableIntent
    """服务端侧：解出一条意图。**形状不对就抛**（`KeyError` / `TypeError` / `ValueError`），
    由收件方接住并计数——那里才有「这条包不要了」的上下文。"""
    _require_fields(data, INTENT_FIELDS, "cable_intent")
    pos = _integers(data["pos"], "pos")
    block = _text(data["block"], "block", allow_empty=False)
    item = _text(data["item"], "item", allow_empty=True)
    action = _text(data["action"], "action", allow_empty=False)
    face = _text(data["face"], "face", allow_empty=True)
    if face and face not in FACING_NAMES:
        raise ValueError("face 必须是面名或空，得到 %r" % (face,))
    origin = _finite(data["origin"], "origin")
    direction = _finite(data["direction"], "direction")
    if not any(direction):
        raise ValueError("direction 不能是零向量")
    return CableIntent(pos, block, item, action, origin, direction, face)


def _text(value, label, allow_empty):
    if not isinstance(value, _TEXT_TYPES):
        raise TypeError("%s 必须是文本，得到 %r" % (label, value))
    if not value and not allow_empty:
        raise ValueError("%s 不能为空" % (label,))
    return value


def _triple(value, label):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("%s 必须是三个数，得到 %r" % (label, value))
    return value


def _integers(value, label):
    out = []
    for item in _triple(value, label):
        if isinstance(item, bool) or not isinstance(item, _INT_TYPES):
            raise TypeError("%s 必须是整数，得到 %r" % (label, item))
        out.append(int(item))
    return tuple(out)


def _finite(value, label):
    out = []
    for item in _triple(value, label):
        if isinstance(item, bool) or not isinstance(item, _INT_TYPES + (float,)):
            raise TypeError("%s 必须是数，得到 %r" % (label, item))
        number = float(item)
        if math.isnan(number) or math.isinf(number):
            raise ValueError("%s 必须是有限的数，得到 %r" % (label, item))
        out.append(number)
    return tuple(out)


def build_event_classes(events):
    # type: (object) -> tuple
    """以 `events` 模块为基类来源，构造意图的事件类 `(CableIntentRequest,)`。

    **一个进程里每侧只能建一份**，理由见 `common/channels/device_identity.py` 的同名函数；
    两侧适配器各自用 `shared_event_classes()` 缓存。原始字典原样带着，解析在收件方。
    """

    class CableIntentRequest(events.CustomC2SEvent):
        """客户端 → 服务端：「我对这格光缆做了这个动作，射线是这条」。"""

        name = CABLE_INTENT

        def __init__(self, payload=None, player_id=None):
            self.payload = payload if isinstance(payload, dict) else {}
            #: 引擎附的发送者（`__id__`）。**只认它**，不认消息体里自称的身份。
            self.player_id = player_id

        def marshal(self):
            # type: () -> dict
            return dict(self.payload)

        @classmethod
        def unmarshal(cls, data):
            payload = dict(data) if isinstance(data, dict) else {}
            player_id = payload.pop("__id__", None)
            return cls(payload, player_id)

    return (CableIntentRequest,)
