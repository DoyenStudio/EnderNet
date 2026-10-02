# coding=utf-8
"""容器类设备的屏（11-07）。

每台设备一行 `@RegistToolDeltaScreen(<屏幕名>, is_proxy=True)`，逻辑全在
`ProxyPanelBase`（见该模块的 docstring：为什么不 `PushUI`、为什么拿不到
参数、为什么收尾只能在 `OnDestroy`）。

**屏幕名与 `common/definitions.py` 的 `CONTAINER_SCREENS` 必须逐字相同**，
那是服务端推身份时带的握手串，也是两侧比对的唯一依据。对不上时症状是
「屏挂上了，但永远认不到身份」——界面上看不出任何异常。这条由
`endernet_tests/test_device_panels.py` 钉住。

`PROXY_PANELS` 是「哪些方块有屏」的**单一来源**：既是注册清单，也是
「引擎画了容器集合、脚本有没有接」那条反向断言的依据。加第五台设备就是
三步——`CONTAINER_SCREENS` 加一行、这里加三行类并进 `PROXY_PANELS`、
设备 JSON 加一条详情弹窗实例。
"""

from ....tooldelta.ui import RegistToolDeltaScreen

from .proxy_panel import ProxyPanelBase


@RegistToolDeltaScreen("bus_item_output_io_ui.main", is_proxy=True)
class BusItemOutputPanel(ProxyPanelBase):
    """总线物品取货口（`endernet:bus_item_output_io`）。

    5 格缓存由**引擎**按方块的 `netease:block_container` 填，脚本不碰那些
    格子。本屏今天只做只读接入：状态栏 + 详情弹窗。缓存开关的提交属下一阶段
    （`PortConfig.cache_enabled` 已有权威字段，但「网络 → 缓存 → 朝向容器」
    这条中转路由未实现，开关目前只被记录与展示）。
    """


@RegistToolDeltaScreen("bus_item_input_io_ui.main", is_proxy=True)
class BusItemInputPanel(ProxyPanelBase):
    """总线物品存货口（`endernet:bus_item_input_io`）。"""


@RegistToolDeltaScreen("bus_template_io_ui.main", is_proxy=True)
class BusTemplatePanel(ProxyPanelBase):
    """总线样板接口（`endernet:bus_template_io`）。

    界面画了 9×2 的格，而方块容器是 9——这条差异记在该界面的 UI 文档里，
    **本轮不碰**（改格数属界面结构，不属脚本侧微调）。
    """


@RegistToolDeltaScreen("assemble_chamber_ui.main", is_proxy=True)
class AssembleChamberPanel(ProxyPanelBase):
    """装配仓（`endernet:assemble_chamber`）。

    它的 UI 文档目前只有「JSON 直读」那半填写过，但**本轮只用到 `stat_bar`
    与公共详情弹窗**——两者的交接在 `endernet_ui_common.md` /
    `endernet_windows.md`，与装配仓自己的 §5～§7 无关。
    """


#: `(方块 id, 屏类)`，顺序即注册顺序。**单一来源**：注册、反向断言、
#: 与 `CONTAINER_SCREENS` 的对账都从这里取。
PROXY_PANELS = (
    ("endernet:bus_item_output_io", BusItemOutputPanel),
    ("endernet:bus_item_input_io", BusItemInputPanel),
    ("endernet:bus_template_io", BusTemplatePanel),
    ("endernet:assemble_chamber", AssembleChamberPanel),
)
