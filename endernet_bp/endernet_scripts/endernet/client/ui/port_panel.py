# coding=utf-8
"""接口设备屏的**配置写入**半边（11-07）：读权威值、发意图、画控件。

两台物品接口（取货口／存货口）共用这一份，差异只用**类属性**描述
（哪台有缓存开关、哪台有过滤模式、摘要画在哪一格）——**不用方法重写**：
这些差异在真机上都是「那个控件在不在」的问题，写成重写会变成「读完两个
类才知道哪台少一个控件」，而漏读的代价是「界面看着正常、那一下点了没反应」。

## 为什么控件全部命令式驱动，而不新增一批 JSON 绑定

点击与图标这两笔走**控件级** API，不经 `Binder` 注册表——因此不受那个
`Binder` 注册机制的影响（运行时注册那条路的成因已于 2026-10-03 修掉，见
`plans/tasks/11-交付说明.md` 第十二节的后续），而且绕开了开关那对 `$toggle_*` 编辑器占位参数（UI 交接第 B4 条）。
**但控件 API 的形状本身有两个坑**（`SetUiItem` 收 `(name, aux)` 而非 `Item`；
`slot_layout` 那条路拿到的是引擎控件、没有 `SetCallback`），两者都收在
`widgets/control_api.py` 里，**别在调用点上各写一遍**。三样合起来的效果是：
**`bus_item_*.json` 一个字节都不用改**，连开关那对 `$toggle_*` 编辑器
占位参数也整个绕开了（UI 交接第 B4 条要的正是「要么两者一起改、要么别接」）。

**图标与点击都必须走 `widgets/control_api`**：`SetUiItem` 收的是
`(name, aux)` 而不是 `Item` 对象，而主面板那条路上的按钮是**引擎控件**、
没有 `SetCallback`。两条都是 2026-10-03 真机上撞到的，症状都只是
「那一格空着」或「点了没反应」。

## 三条纪律

1. **每个控件都可能不在**。代理屏拿不到构造参数、路径解析有两种挂法
   （`slot_layout.control` 按序试），而且存货口与取货口的界面形状本就不同。
   取不到就**跳过这一项**，并把名字记进 `missing`（只记一次，别每帧刷）。
2. **服务端推送即权威**。`_refresh_controls` 只把 `PortConfigModel` 的
   `desired` 画出来，本地绝不自己算配置。
3. **代理屏第一次消费回执**。`ProxyPanelBase` 今天只发保活、不排水，
   `_drain_receipts` 是这条通道的第一个消费者；不排水的话 `_in_flight`
   只涨不落，而症状是「改过一次之后再也发不出命令」。
"""

from ...common.protocol import MSG_PORT_CONFIG
from . import port_config_model, slot_layout
from .item_list_window import WINDOW_ATTRIBUTE as ITEM_LIST_WINDOW_ATTRIBUTE
from .item_list_window import show_item_list_window
from .proxy_panel import ProxyPanelBase
from .widgets.control_api import on_click, show_item
from .widgets.list_window_bindings import (
    ItemListWindowBindingsMixin, SelectItemWindowBindingsMixin,
)

#: 设置窗在宿主里建出来的实例名。**只建一次并复用**（`SetVisible` 控显隐），
#: 反复 `AddElement` 会叠出第二个控件，而表现是「关掉它，下次点齿轮不出来」。
SETTINGS_INSTANCE = "endernet_port_settings"

#: 设置窗的层级。比详情弹窗（40）高一档：那一条是只读的，这一条要操作。
SETTINGS_LAYER = 45

#: 内容区相对路径（两种屏幕挂法由 `slot_layout.control` 补前缀）。
MAIN = "/root_panel/bg/main"
SETTINGS_BTN = MAIN + "/settings_btn"
SUMMARY_ICON = MAIN + "/item_set_panel/selected_item_renderer"
SUMMARY_TEXT = MAIN + "/item_set_panel/item_set_tip_text"
SUMMARY_BUTTON = MAIN + "/item_set_panel/interact_btn"

#: 设置窗内部的控件名（相对于窗口根）。
SETTINGS_CLOSE = "close"
SETTINGS_CACHE_SWITCH = "cache_slots_enable_mode_switch"
SETTINGS_MODE_BUTTON = "filter_mode_btn"
SETTINGS_MODE_ICON = "filter_mode_btn/icon"
SETTINGS_MODE_TEXT = "filter_mode_text"
SETTINGS_FILTER_BUTTON = "filter_modification_btn"


def _reasons_text(result):
    # type: (dict) -> str
    """把回执里的原因拼成一句话。**没有显示位置**，先落在诊断里。"""
    parts = []
    for reason in result.get("reasons") or ():
        if isinstance(reason, dict):
            parts.append(reason.get("detail") or reason.get("code") or "")
    return "；".join(part for part in parts if part)


class ItemPortPanelBase(ItemListWindowBindingsMixin, SelectItemWindowBindingsMixin,
                        ProxyPanelBase):
    """物品接口屏的配置半边。子类用类属性描述差异，**不重写方法**。"""

    #: 本屏对应的方块 id（诊断用）。
    DEVICE_ID = ""

    #: 设置窗的**完整定义名**（`<namespace>.settings_window`）。
    SETTINGS_DEFINITION = ""

    #: 设置窗里的缓存开关控件名；`None` = 本设备没有这个开关。
    CACHE_SWITCH = None
    #: 设置窗里的过滤模式三件套；`None` = 本设备没有模式切换。
    MODE_BUTTON = None
    MODE_ICON = None
    MODE_TEXT = None
    #: 打开名单窗口的按钮。两处**分开写**而不是「一个名字 + 一个布尔」：
    #: 一个名字在两种宿主里的解析方式不同（主面板走内容区相对路径、设置窗
    #: 走窗口内相对名），共用一个字段会让「它在哪一层」变成猜。
    #: 两台各用其中一条，另一条留 `None`。
    FILTER_BUTTON_MAIN = None
    FILTER_BUTTON_SETTINGS = None
    #: 主面板上的摘要图标／文案控件；`None` = 本设备不画摘要。
    #: （存货口就是这样：它的 `content` 里没有 `item_set_panel`。）
    HAS_SUMMARY = False
    #: 名单窗口的标题。
    FILTER_TITLE = "物品列表"
    #: 标题随过滤模式变（存货口的交接要求：「可按当前模式显示黑名单或白名单」）。
    FILTER_TITLE_FROM_MODE = False

    def __init__(self, screen_name, screen_instance, params=None):
        ProxyPanelBase.__init__(self, screen_name, screen_instance, params)
        #: 设置窗控件（按需建、建好留着）。类属性之外再放一份实例状态。
        self.settings = None
        #: 已解析到的控件缓存；每帧只解析一次。
        self.controls = {}
        #: `名字 -> 次数`：取不到的控件。**只记一次**（见 `_missing`）。
        self.failures = {"missing": 0, "errors": 0}
        self.missing = set()
        #: 正在编辑的名单窗口：`(窗口对象, 列表对象)`。列表是**同一个对象**，
        #: 窗口就地改它，因此收尾时读到的就是玩家编辑后的那份。
        self.draft = None

    # -- 模型装配 ------------------------------------------------------

    def _attach_models(self, device_ref, client):
        self.port_config = port_config_model.PortConfigModel(device_ref)
        self.settings = None
        self.controls = {}
        self.missing = set()
        self.draft = None
        client.on_message(self._make_config_handler(client))

    def _detach_models(self):
        self.port_config = None
        self.settings = None
        self.controls = {}
        self.draft = None

    def _make_config_handler(self, client):
        # type: (object) -> object
        """只喂给创建回调时那个模型，拒绝失效会话与旧屏的迟到推送。

        与 `DeviceStatusBindingsMixin._make_status_handler` 逐条同形：会话号
        由客户端校验，设备代次由模型校验，模型身份按引用比对。
        """
        model = self.port_config

        def on_config_message(envelope, message):
            # type: (object, object) -> None
            if message is None or message.kind != MSG_PORT_CONFIG:
                return
            if not client.has_session() or envelope.session_id != client.session_id:
                return
            if model is not None and self.port_config is model:
                if model.apply(message):
                    self._refresh_controls()

        return on_config_message

    # -- 每帧 ----------------------------------------------------------

    def _tick_extra(self):
        for step in (self._ensure_controls, self._drain_receipts,
                     self._watch_filter_window, self._poll_switch):
            try:
                step()
            except Exception:
                # **一步炸了不能连累后面几步**，也不能让异常从 tick 路径
                # 回到引擎（那会刷屏并跳过本帧剩下的事）。计数留给诊断。
                self.failures["errors"] += 1

    def _missing(self, name):
        # type: (str) -> None
        """记一次「这个控件没找到」。**只记第一次**——每帧记一次会把诊断淹掉。"""
        if name not in self.missing:
            self.missing.add(name)
            self.failures["missing"] += 1

    # -- 控件解析 ------------------------------------------------------

    def _ensure_controls(self):
        if self.controls:
            return
        main = {}
        for name, path in (("settings_btn", SETTINGS_BTN),):
            found = slot_layout.control(self, path)
            if found is None:
                self._missing(name)
            else:
                main[name] = found
        summary_icon = slot_layout.control(self, SUMMARY_ICON)
        summary_text = slot_layout.control(self, SUMMARY_TEXT)
        summary_button = slot_layout.control(self, SUMMARY_BUTTON)
        self.controls = main
        if self.HAS_SUMMARY and summary_icon is not None:
            self.controls["summary_icon"] = summary_icon
        elif self.HAS_SUMMARY:
            self._missing("summary_icon")
        if self.HAS_SUMMARY and summary_text is not None:
            self.controls["summary_text"] = summary_text
        elif self.HAS_SUMMARY:
            self._missing("summary_text")
        if self.FILTER_BUTTON_MAIN is not None:
            if summary_button is None:
                self._missing("filter_button")
            else:
                self.controls["filter_button"] = summary_button
        # **必须走 `on_click`**：`slot_layout` 那条路拿到的是**引擎自己的**
        # `BaseUIControl`，它的 `asButton()` 给的是引擎的 `ButtonUIControl`,
        # 而那个类**没有 `SetCallback`**——直接写 `.asButton().SetCallback(...)`
        # 会抛，被上面的 `_tick_extra` 吞掉，表现是**点了没反应**（2026-10-03
        # 真机撞到）。`on_click` 两种形状都认，见 `widgets/control_api.py`。
        settings_btn = self.controls.get("settings_btn")
        if settings_btn is not None:
            on_click(settings_btn, self._open_settings)
        if "filter_button" in self.controls:
            on_click(self.controls["filter_button"], self._edit_filter)
        self._refresh_controls()

    def _settings(self):
        """设置窗控件：按需建、建好留着（`SetVisible` 控显隐）。"""
        if self.settings is not None:
            return self.settings
        if not self.SETTINGS_DEFINITION:
            self._missing("settings_window")
            return None
        try:
            created = self.AddElement(self.SETTINGS_DEFINITION, SETTINGS_INSTANCE)
        except Exception:
            self._missing("settings_window")
            return None
        try:
            created.SetLayer(SETTINGS_LAYER)
            created.SetVisible(False)
            close = created.GetElement(SETTINGS_CLOSE)
            if close is not None:
                close.asButton().SetCallback(self._close_settings)
        except Exception:
            self.failures["errors"] += 1
        self.settings = created
        return created

    def _settings_control(self, path):
        window = self._settings()
        if window is None:
            return None
        try:
            found = window.GetElement(path)
        except Exception:
            found = None
        if found is None:
            self._missing("settings/" + path)
        return found

    # -- 交互 ----------------------------------------------------------

    def _open_settings(self, params=None):
        window = self._settings()
        if window is None:
            return
        self._wire_settings()
        try:
            window.SetVisible(True)
        except Exception:
            self.failures["errors"] += 1
        self._refresh_controls()

    def _close_settings(self, params=None):
        if self.settings is not None:
            try:
                self.settings.SetVisible(False)
            except Exception:
                self.failures["errors"] += 1

    def _wire_settings(self):
        """把设置窗里的控件接上（幂等：接过的标记在 `controls` 里）。"""
        if self.controls.get("settings_wired"):
            return
        switch = None
        if self.CACHE_SWITCH is not None:
            switch = self._settings_control(self.CACHE_SWITCH)
            if switch is not None:
                self.controls["cache_switch"] = switch
        if self.MODE_BUTTON is not None:
            button = self._settings_control(self.MODE_BUTTON)
            if button is not None:
                self.controls["mode_button"] = button
                button.asButton().SetCallback(self._switch_mode)
            if self.MODE_ICON is not None:
                icon = self._settings_control(self.MODE_ICON)
                if icon is not None:
                    self.controls["mode_icon"] = icon
            if self.MODE_TEXT is not None:
                label = self._settings_control(self.MODE_TEXT)
                if label is not None:
                    self.controls["mode_text"] = label
        if self.FILTER_BUTTON_SETTINGS is not None:
            button = self._settings_control(self.FILTER_BUTTON_SETTINGS)
            if button is not None:
                self.controls["filter_button"] = button
                button.asButton().SetCallback(self._edit_filter)
        self.controls["settings_wired"] = True

    def _edit_filter(self, params=None):
        """打开名单窗口编辑过滤名单。

        **窗口只改传进去的那个 list**（组件契约），因此这里把**权威名单的
        一个副本**交给它，自己留着引用等它关掉——`_watch_filter_window`
        按「它还在不在 `_vars` 里」判定关闭，然后提交。组件没有关闭回调，
        加一个就等于给所有宿主改契约。
        """
        model = self.port_config
        if model is None or not model.ready():
            return
        if self.draft is not None:
            return
        names = list(model.filter_names())
        screen_vars = getattr(self, "_vars", {})
        if ITEM_LIST_WINDOW_ATTRIBUTE in screen_vars:
            return
        title = (
            model.mode_visual()[2] if self.FILTER_TITLE_FROM_MODE
            else self.FILTER_TITLE
        )
        opened = show_item_list_window(self, names, model.filter_limit, title)
        if not opened:
            return
        window = screen_vars.get(ITEM_LIST_WINDOW_ATTRIBUTE)
        if window is None:
            return
        self.draft = (window, names)

    def _watch_filter_window(self):
        draft = self.draft
        if draft is None:
            return
        window, names = draft
        screen_vars = getattr(self, "_vars", {})
        if screen_vars.get(ITEM_LIST_WINDOW_ATTRIBUTE) is window:
            return
        # 窗口关了（或被杀掉）：把编辑后的名单提交上去。
        self.draft = None
        model = self.port_config
        if model is None:
            return
        if list(names) == model.filter_names():
            return
        self._submit(model.intent_filter(list(names)))

    def _switch_mode(self, params=None):
        model = self.port_config
        if model is None or not model.ready() or model.busy("filter_mode"):
            return
        self._submit(model.intent_mode(model.next_mode()))

    def _poll_switch(self):
        """缓存开关：**读控件的状态**，与权威值不同就提交。

        开关是控件级状态（引擎自己在拨），而 `PortConfig` 是权威值，两者
        必须由这一刻的读数对齐——没有这一步，玩家拨一下没有任何反应。
        `GetState` 只在真引擎上存在，取不到时这一条静默跳过（见 `_switch_state`）。
        """
        model = self.port_config
        switch = self.controls.get("cache_switch")
        if model is None or switch is None or not model.ready():
            return
        if model.busy("cache_enabled"):
            return
        state = _switch_state(switch)
        if state is None or bool(state) == model.cache_enabled():
            return
        self._submit(model.intent_cache(bool(state)))

    # -- 提交与回执 ----------------------------------------------------

    def _submit(self, intent):
        # type: (dict) -> object
        """发一条 `port_configure`，返回 `request_id`（发不出去时 `None`）。

        **服务端只合并载荷里出现的那几项**，因此这里写多少项就改多少项。
        在途闸按**字段**判（`busy`）：同一字段还有一条没回来时不重复提交，
        两条的到达顺序不保证。
        """
        client = self.client
        model = self.port_config
        if client is None or model is None or not client.has_session():
            return None
        for field in intent:
            if model.busy(field):
                return None
        request_id = client.send_command("port_configure", config=intent)
        model.expect(request_id, intent)
        self._refresh_controls()
        return request_id

    def _drain_receipts(self):
        """收命令回执。**代理屏第一次消费这条通道**（见模块 docstring）。"""
        client = self.client
        model = self.port_config
        if client is None or model is None:
            return
        changed = False
        for message in client.take_command_results():
            result = getattr(message, "result", None)
            if not isinstance(result, dict):
                continue
            request_id = result.get("request_id")
            if not request_id:
                continue
            payload = result.get("payload")
            config = None
            if isinstance(payload, dict) and isinstance(payload.get("config"), dict):
                config = payload["config"]
            if model.settle(request_id, config=config,
                            detail=_reasons_text(result)):
                changed = True
        if changed:
            self._refresh_controls()

    # -- 画控件 --------------------------------------------------------

    def _refresh_controls(self):
        """把模型的 `desired` 画到控件上。**只画拿得到的那些。**"""
        model = self.port_config
        if model is None:
            return
        self._refresh_summary(model)
        self._refresh_mode(model)
        self._refresh_switch(model)

    def _refresh_summary(self, model):
        icon = self.controls.get("summary_icon")
        text = self.controls.get("summary_text")
        if icon is None and text is None:
            return
        name, aux, summary = model.filter_summary()
        if icon is not None:
            try:
                show_item(icon, name, aux)
            except Exception:
                self.failures["errors"] += 1
        if text is not None:
            try:
                text.asLabel().SetText(summary)
            except Exception:
                self.failures["errors"] += 1

    def _refresh_mode(self, model):
        icon = self.controls.get("mode_icon")
        label = self.controls.get("mode_text")
        if icon is None and label is None:
            return
        uv, size, text = model.mode_visual()
        if icon is not None:
            try:
                icon.asImage().SetUV(uv, size)
            except Exception:
                self.failures["errors"] += 1
        if label is not None:
            try:
                label.asLabel().SetText(text)
            except Exception:
                self.failures["errors"] += 1

    def _refresh_switch(self, model):
        switch = self.controls.get("cache_switch")
        if switch is None:
            return
        state = _switch_state(switch)
        if state is not None and bool(state) == model.cache_enabled():
            return
        try:
            switch.asSwitch().SetState(model.cache_enabled())
        except Exception:
            self.failures["errors"] += 1

    # -- 诊断 ----------------------------------------------------------

    def diagnostics(self):
        # type: () -> dict
        report = ProxyPanelBase.diagnostics(self)
        report["port_failures"] = dict(self.failures)
        report["port_missing"] = sorted(self.missing)
        return report


def _switch_state(switch):
    """读开关的当前状态；读不到返回 `None`。

    `USwitch.GetState` 直接调引擎的 `GetToggleState`，控件不是开关形状时
    抛 `AttributeError`——**那是「这台设备的这个控件不是开关」，不是故障**，
    因此这里吞掉并返回 `None`，让调用方跳过这一项。
    """
    try:
        return bool(switch.asSwitch().GetState())
    except Exception:
        return None
