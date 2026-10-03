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

## 11-07：两台物品口的配置写入

它们多继承了 `ItemPortPanelBase`（本文件下方），把**权威配置画到控件上**、
把**玩家的意图发回去**。三条实现纪律写在那个基类里：控件全部命令式驱动
（`SetCallback` / `SetText` / `SetUiItem` / `SetUV` / `SetState`），因此
`bus_item_*.json` 一个字节都不用改；取不到控件时给安全值、不抛；服务端
推送即权威，本地只做乐观显示。
"""

from ....tooldelta.ui import RegistToolDeltaScreen

from .port_panel import (
    SETTINGS_CACHE_SWITCH,
    SETTINGS_FILTER_BUTTON,
    SETTINGS_MODE_BUTTON,
    SETTINGS_MODE_ICON,
    SETTINGS_MODE_TEXT,
    SUMMARY_BUTTON,
    ItemPortPanelBase,
)
from .proxy_panel import ProxyPanelBase


@RegistToolDeltaScreen("bus_item_output_io_ui.main", is_proxy=True)
class BusItemOutputPanel(ItemPortPanelBase):
    """总线物品取货口（`endernet:bus_item_output_io`）。

    5 格缓存由**引擎**按方块的 `netease_block_container` 填，脚本不碰那些
    格子。本屏做两件配置：**缓存开关**（`settings_window` 里那一个）与
    **输出名单**（`item_set_panel` 那一格 + 「编辑」按钮）。

    **缓存开关现在真的改变搬运行为**：「网络 → 缓存 → 朝向容器」这条中转
    路由已于 2026-10-03 交付（06-06，见
    `plans/tasks/06-06-交付说明-真实缓存.md`）：开启时走 5 格真实容器中转，
    关闭时直输且不触碰缓存。**那一半游戏内未验**，本屏只负责把开关的权威值
    读出来、把玩家的意图提交上去。
    """

    DEVICE_ID = "endernet:bus_item_output_io"
    SETTINGS_DEFINITION = "bus_item_output_io_ui.settings_window"
    #: 缓存开关（设置窗里那一个）。取货口独有——存货口没有它。
    CACHE_SWITCH = SETTINGS_CACHE_SWITCH
    #: 取货口**只接受白名单**（`ROLE_FILTER_MODES`），因此没有模式切换控件，
    #: `filter_modes` 下发的那一项也是空的切换目标（`next_mode` 原地不动）。
    #: 摘要与「编辑名单」按钮都在主面板的 `item_set_panel` 上。
    HAS_SUMMARY = True
    #: 取货口的「编辑名单」长在**主面板**的 `item_set_panel` 上（那台没有
    #: 名单按钮在设置窗里），因此填的是主面板那一条。
    FILTER_BUTTON_MAIN = SUMMARY_BUTTON
    FILTER_TITLE = "自动输出物品"


@RegistToolDeltaScreen("bus_item_input_io_ui.main", is_proxy=True)
class BusItemInputPanel(ItemPortPanelBase):
    """总线物品存货口（`endernet:bus_item_input_io`）。

    它的过滤面全在 `settings_window` 里：**模式按钮**（黑白名单）与
    **「修改」按钮**（打开名单窗口）。`content` 里没有 `item_set_panel`，
    因此本界面不画摘要图标——那是 UI 就有的差异，不是漏接。

    **名单上限由服务端下发**：UI 交接明写「本设备具体上限待定，不能套用
    缓冲槽数量或示例值 64」，而服务端已经有一个具名替身
    （`PortPolicy.intake_filter_limit`），因此界面照它显示与限制，不自己定。
    """

    DEVICE_ID = "endernet:bus_item_input_io"
    SETTINGS_DEFINITION = "bus_item_input_io_ui.settings_window"
    MODE_BUTTON = SETTINGS_MODE_BUTTON
    MODE_ICON = SETTINGS_MODE_ICON
    MODE_TEXT = SETTINGS_MODE_TEXT
    FILTER_BUTTON_SETTINGS = SETTINGS_FILTER_BUTTON
    FILTER_TITLE_FROM_MODE = True


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


@RegistToolDeltaScreen("storage_driver_ui.main", is_proxy=True)
class StorageDriverPanel(ProxyPanelBase):
    """存储驱动器（`endernet:storage_driver`）。**这是一张空屏，有意如此。**

    User 2026-10-03 裁定：介质的装卸与 Lore 刷新**暂不需要界面**，只保留
    服务端逻辑，因此本屏不接任何配置控件——它拿到的就是 `ProxyPanelBase`
    给的那两样（`stat_bar` 在线/离线与详情弹窗）。

    **这不是「配置写入漏了驱动器」**：它的 UI 文档
    `plans/ui/storage_driver.md` 第 2～8 节仍是空模板，JSON 里也没有任何
    槽位按钮或介质渲染器，而任务 11 第 36 行禁止对未交接 UI 写逻辑。等
    那份文档交接、控件到位之后，接法照 `port_panel.ItemPortPanelBase` 那
    一套（配置走 `port_configure` 的合并语义，或驱动器自己那几条命令）。

    驱动器的**服务端**半边早已存在且不受本屏影响：`DriverStore` 的槽位与
    介质表、`mount` / `unmount` / `driver_status` / `medium` 四条命令。
    """


#: `(方块 id, 屏类)`，顺序即注册顺序。**单一来源**：注册、反向断言、
#: 与 `CONTAINER_SCREENS` 的对账都从这里取。
PROXY_PANELS = (
    ("endernet:bus_item_output_io", BusItemOutputPanel),
    ("endernet:bus_item_input_io", BusItemInputPanel),
    ("endernet:bus_template_io", BusTemplatePanel),
    ("endernet:assemble_chamber", AssembleChamberPanel),
    ("endernet:storage_driver", StorageDriverPanel),
)
