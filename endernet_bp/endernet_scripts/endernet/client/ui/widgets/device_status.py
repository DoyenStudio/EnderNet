# coding=utf-8
"""设备状态栏与详情弹窗的**离线可测**那一半（11-13）。

对应 `plans/tasks/11. 终端与客户端同步.md` 的 11-13 与「总体状态与详情」
一节，以及 `script_architecture.md` 里 `client/ui/widgets/device_status.py`
那一格。

本模块**不 import 引擎、也不 import `tooldelta`**：这里只有绑定名、显示
常量与一个纯模型。真正的 `@Binder.binding` 必须写在注册的 `ToolDeltaScreen`
子类的**类体**里（运行时注册在这个引擎上不生效，见 `terminal_panel.py` 的
长注释），因此本模块只提供那些绑定要读的数据与名字。

## 为什么状态栏的文字是服务端给的，颜色图标是本地定的

`state_text`（「在线」/「离线」）与每条原因的 `detail` 都由服务端的
`DeviceNetworkStatus` / `BlockReason` 产出，是唯一真源；本地再维护一张
「原因码 → 中文」的表，两张表迟早漂移，而症状是「界面说的原因与实际拦截
的原因不一致」——比不显示更糟。颜色与图标相反：它们是**本界面的外观**，
服务端不该知道 `stat_text` 用哪种绿，因此留在这里。

## 只有两种状态

`validity="pending"`（结论还没确认）在界面上**照样是「离线」**，不是第三
种颜色；为什么还没确认写在详情的原因里。做成第三种状态会让玩家去分辨一个
他无从处置的差别。
"""

from ....common.protocol import Reason

# -- 绑定名 ----------------------------------------------------------------
#
# 名字要与资源包里的 `binding_name` **逐字相同**——对不上时引擎只是查不到
# 绑定、安静地什么都不做。`test_terminal_panel.TestJsonContract` 逐条钉住
# 这里与三份 JSON 的一致性。

BINDING_PREFIX = "#EnderNetDeviceStatus."
#: 状态栏文字（`stat_bar/stat_text` 的 `#text`）。
TEXT_BINDING = BINDING_PREFIX + "text"
#: 状态栏文字颜色（绿 / 红）。
COLOR_BINDING = BINDING_PREFIX + "color"
#: 状态栏是否显示。**没拿到结论时不显示**，而不是先糊一个红色。
VISIBLE_BINDING = BINDING_PREFIX + "visible"
#: 状态栏图标（在线 / 离线两种纹理）。
ICON_BINDING = BINDING_PREFIX + "icon"
#: 详情弹窗第一行：控制器连接/冲突/支持情况。
MASTER_TEXT_BINDING = BINDING_PREFIX + "master_text"
MASTER_ICON_BINDING = BINDING_PREFIX + "master_icon"
#: 详情弹窗第二行：带宽、能源与重建等网络侧原因。
NET_TEXT_BINDING = BINDING_PREFIX + "net_text"
NET_ICON_BINDING = BINDING_PREFIX + "net_icon"
#: 详情弹窗整体显隐。
POPUP_VISIBLE_BINDING = BINDING_PREFIX + "popup_visible"
#: 点 `stat_bar/interact_btn` 打开详情。
OPEN_BINDING = BINDING_PREFIX + "open"
#: 详情弹窗自己的关闭按钮。
CLOSE_BINDING = BINDING_PREFIX + "close"

# -- 显示常量 --------------------------------------------------------------

#: 11.md「总体状态与详情」建议的绿/红。属**显示值**，不是玩法数值。
COLOR_ONLINE = [0.0, 1.0, 0.0]
COLOR_OFFLINE = [1.0, 0.0, 0.0]

ICON_ONLINE = "textures/ui/endernet/network_ok_icon"
ICON_OFFLINE = "textures/ui/endernet/network_error_icon"
MASK_ONLINE = "textures/ui/endernet/corner_ok_icon"
MASK_OFFLINE = "textures/ui/endernet/corner_error_icon"

#: 详情弹窗两行的兜底文案。**只在那一行确实没有原因时使用**；有原因时
#: 显示的是服务端给的具体说明，不是这里这几句。
MASTER_ROW_OK = "已连接控制器"
MASTER_ROW_MISSING = "未连接到控制器"
NET_ROW_UNKNOWN = "网络状态未知"

#: 归到**第一行**（控制器侧）的原因码。其余一律进第二行（网络侧）。
#: 分组的判据是「该找谁」：控制器不在/冲突/不支持，与网络能不能工作，
#: 是两件要查不同东西的事。
MASTER_REASONS = frozenset(
    (
        Reason.NO_CONTROLLER,
        Reason.MASTER_ABSENT,
        Reason.MASTER_UNLOADED,
        Reason.MASTER_RECOVERING,
        Reason.CONTROLLER_CONFLICT,
        Reason.CONTROLLER_DOES_NOT_SERVE,
        Reason.NOT_A_CANDIDATE,
    )
)


def _reason_pair(reason):
    # type: (object) -> tuple
    """`{"code","detail"}` 字典或同形对象都收。"""
    if isinstance(reason, dict):
        return reason.get("code", ""), reason.get("detail", "")
    return getattr(reason, "code", ""), getattr(reason, "detail", "")


def _tri_text(value, label):
    # type: (object, str) -> str
    """三态字段的文案。**`None` 说「待确认」，不说「不足」**——与状态栏
    只有两种文本的口径一致：结论没确认时不该指一个方向。"""
    if value is True:
        return label + "满足"
    if value is False:
        return label + "不足"
    return label + "待确认"


class DeviceStatusModel(object):
    """一台设备的总体状态在本界面的本地副本。

    纯模型：不持有引擎控件、不发包，只回答「这几个绑定现在该读什么」。
    因此可以在离线测试里直接构造、喂消息、断言读出来的值。
    """

    __slots__ = ("device_ref", "generation", "status", "_payload", "popup_open", "stats")

    def __init__(self, device_ref=None):
        self.device_ref = device_ref
        #: 本界面认的实例代次。拆掉又在原地放一个会变大，于是旧实例的
        #: 响应在这里被丢掉——「旧设备响应不能污染新界面」的落点。
        self.generation = getattr(device_ref, "generation", None)
        #: 最近一条被接受的结论（`DeviceStatusMessage` 或 None）。
        self.status = None
        self._payload = None
        self.popup_open = False
        self.stats = {"accepted": 0, "dropped": 0, "popup_opened": 0}

    # -- 接收 ----------------------------------------------------------

    def accepts(self, message):
        # type: (object) -> bool
        """这条消息是不是给本界面的。**代次是唯一判据。**

        会话号对不上由调用方（面板的 `on_message`）先筛一遍：那属于路由，
        不是本模型的事。这里只管「同一条会话里，还是不是原来那一台方块」。
        """
        if message is None:
            return False
        # 控制器本身不套用这套状态栏（blocks.md 第 5 条）。服务端不会为它
        # 发布，但客户端也不该因为「服务端换了实现」就先显示起来——这条
        # 判据在两边各留一份是**有意的重复**，代价只有一个布尔值。
        if not getattr(message, "ui_applicable", True):
            return False
        if self.generation is None:
            return True
        return message.generation == self.generation

    def apply(self, message):
        # type: (object) -> bool
        """接受一条结论。返回**内容是否变化**（没变就是一次幂等刷新）。"""
        if not self.accepts(message):
            self.stats["dropped"] += 1
            return False
        payload = message.to_dict()
        if payload == self._payload:
            return False
        self._payload = payload
        self.status = message
        self.stats["accepted"] += 1
        return True

    # -- 状态栏 --------------------------------------------------------

    def visible(self):
        # type: () -> bool
        """**没拿到结论就整个不显示**：宁可空着，也不先给一个假的「离线」。"""
        return self.status is not None

    def text(self):
        # type: () -> str
        if self.status is None:
            return ""
        return self.status.state_text

    def color(self):
        # type: () -> list
        if self.status is not None and self.status.online:
            return COLOR_ONLINE
        return COLOR_OFFLINE

    def icon(self):
        # type: () -> str
        if self.status is not None and self.status.online:
            return ICON_ONLINE
        return ICON_OFFLINE

    # -- 详情弹窗 ------------------------------------------------------

    def popup_visible(self):
        # type: () -> bool
        return self.popup_open

    def open_popup(self):
        # type: () -> bool
        """打开详情。**幂等**：连点不会开出第二个弹窗——只有一个控件，
        重复打开就是重复把它设为显示，结构上不可能叠窗。"""
        self.popup_open = True
        self.stats["popup_opened"] += 1
        return True

    def close_popup(self):
        # type: () -> bool
        """只收起详情，**不关原设备界面**。"""
        self.popup_open = False
        return False

    def popup_rows(self):
        # type: () -> tuple
        """`(master_text, master_mask, net_text, net_mask)`。

        有原因就把**具体说明**逐条列出来（用「；」连），而不是塌成一句
        静态的「网络故障中」——11.md 点名了这一点。那一行没有原因时，用
        状态自己带的字段兜底：控制器行看 `master_instance_id`，网络行看
        `bandwidth_ok` / `power_ok` 的三态。
        """
        master = []
        network = []
        reasons = () if self.status is None else self.status.reasons
        for reason in reasons:
            code, detail = _reason_pair(reason)
            if not detail:
                continue
            if code in MASTER_REASONS:
                master.append(detail)
            else:
                network.append(detail)
        return (
            "；".join(master) if master else self._master_default(),
            MASK_OFFLINE if master else MASK_ONLINE,
            "；".join(network) if network else self._net_default(),
            MASK_OFFLINE if network else MASK_ONLINE,
        )

    def master_text(self):
        # type: () -> str
        return self.popup_rows()[0]

    def master_mask(self):
        # type: () -> str
        return self.popup_rows()[1]

    def net_text(self):
        # type: () -> str
        return self.popup_rows()[2]

    def net_mask(self):
        # type: () -> str
        return self.popup_rows()[3]

    def _master_default(self):
        # type: () -> str
        if self.status is None:
            return MASTER_ROW_MISSING
        if self.status.master_instance_id:
            return MASTER_ROW_OK
        return MASTER_ROW_MISSING

    def _net_default(self):
        # type: () -> str
        if self.status is None:
            return NET_ROW_UNKNOWN
        return "%s，%s" % (
            _tri_text(self.status.bandwidth_ok, "带宽"),
            _tri_text(self.status.power_ok, "能源"),
        )

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report["has_status"] = self.status is not None
        report["popup_open"] = self.popup_open
        return report
