# coding=utf-8
"""跨界面悬浮的工厂样板物品采集器。

UI 初始化完成后调用 open_universal_importer()。CreateUI 仅持有生命周期，
面板挂在当前栈顶界面内；关闭采集器不会弹出玩家的容器。
"""

from collections import deque
from copy import deepcopy
from functools import partial

import mod.client.extraClientApi as clientApi

from ....tooldelta.api.client.tips import SetPopupNotice
from ....tooldelta.api.common import ExecLater
from ....tooldelta.events.basic import ClientEvent
from ....tooldelta.events.client.ui import ScreenSizeChangedClientEvent
from ....tooldelta.ui import RegistToolDeltaScreen, SCREEN_BASE_PATH, ToolDeltaScreen
from .universal_importer_config import save_config


UI_DEFINITION = "endernet_universal_importer_ui.main"
PANEL_DEFINITION = "endernet_universal_importer_ui.overlay_panel"
SLOT_DEFINITION = "endernet_universal_importer_ui.slot_section"
OVERLAY_LAYER = 100000
MAX_MOUNT_ATTEMPTS = 5
IMMEDIATE_SECTION_LIMIT = 16
SECTION_BATCH_SIZE = 8
COLORS = {
    None: (1.0, 1.0, 1.0),
    "inputs": (1.0, 0.6, 0.15),
    "outputs": (0.5, 0.8, 1.0),
}
_OPEN_SCREEN = None


class _ImporterScreenHost(object):
    """统一包装两种 SDK ScreenNode，避免 ToolDelta 误判为界面代理。"""

    def __init__(self, screen_node):
        self.screenNode = screen_node


class ImporterPushScreenEvent(ClientEvent):
    name = "PushScreenEvent"


class ImporterPopScreenAfterEvent(ClientEvent):
    name = "PopScreenAfterClientEvent"


def _on_screen_changed(event):
    # type: (ImporterPushScreenEvent | ImporterPopScreenAfterEvent | ScreenSizeChangedClientEvent) -> None
    # 静态监听不随 ToolDeltaScreen.OnDeactive 暂停；仅维护悬浮层，不遍历控件。
    if _OPEN_SCREEN is not None and not _OPEN_SCREEN.closed:
        _OPEN_SCREEN._on_screen_changed()


@ImporterPushScreenEvent.Listen()
def _on_push_screen(event):
    _on_screen_changed(event)


@ImporterPopScreenAfterEvent.Listen()
def _on_pop_screen(event):
    _on_screen_changed(event)


@ScreenSizeChangedClientEvent.Listen()
def _on_screen_size_changed(event):
    _on_screen_changed(event)


def open_universal_importer():
    # type: () -> UniversalImporterScreen
    """打开一份空白采集草稿；重复调用返回当前采集器。"""
    if _OPEN_SCREEN is not None and not _OPEN_SCREEN.closed:
        return _OPEN_SCREEN
    return UniversalImporterScreen.CreateUI({"isHud": 1})


def close_universal_importer():
    # type: () -> None
    """客户端停机或调用方取消时关闭，不改动已经保存的配置。"""
    if _OPEN_SCREEN is not None:
        _OPEN_SCREEN.close()


def _item_key(item):
    # type: (dict) -> tuple
    return (item.get("itemName"), item.get("auxValue", 0),
            bool(item.get("isEnchanted", False)))


@RegistToolDeltaScreen(UI_DEFINITION)
class UniversalImporterScreen(ToolDeltaScreen):
    def __init__(self, screen_name, screen_instance, params=None):
        ToolDeltaScreen.__init__(self, screen_name, screen_instance, params)
        self.closed = False
        self.selecting = False
        self.mode = "inputs"
        self._raise_pending = False
        self._mount_attempts = 0
        self._source = None
        self._source_name = None
        self._sections = {}
        self._pending_sections = deque()
        self._section_generation = 0
        self._sequence = 0
        self._draft = {}
        self._selection_order = []
        self._view_host = None
        self._view_root = None
        self._view_path = None
        self._view_bound = False
        self._board_position = None

    def OnCreate(self):
        global _OPEN_SCREEN
        _OPEN_SCREEN = self
        self.base.SetIsHud(1)
        # CreateUI 挂在 HUD 下，推入普通屏后可能不绘制；实际面板挂到栈顶屏。
        self.base.SetScreenVisible(False)
        self._on_screen_changed()

    def GetElement(self, path):
        return self._view_root.GetElement(str(path).lstrip("/"))

    def _bind_board(self):
        board = self.GetElement("/control_board")
        # 操作板必须盖住 slot_section，否则覆盖到按钮上的物品框会截走点击。
        board.SetLayer(100)
        for name, callback in (
            ("sample_btn", self._toggle_sampling),
            ("mode_btn", self._toggle_mode),
            ("save_btn", self._save),
            ("exit_btn", self.close),
        ):
            board.GetElement(name).asButton().SetCallback(callback)
        self._update_labels()

    def _on_screen_changed(self):
        if self.closed:
            return
        # 切屏或缩放后旧坐标不能继续使用；保留草稿，等待再次点击开始选择。
        self.selecting = False
        self._clear_sections()
        self._detach_view()
        self._source = None
        self._source_name = None
        self._mount_attempts = 0
        if not self._raise_pending:
            self._raise_pending = True
            ExecLater(0, self._raise_after_screen_change)

    def _raise_after_screen_change(self):
        self._raise_pending = False
        if self.closed:
            return
        self._mount_attempts += 1
        if not self._raise_overlay() and self._mount_attempts < MAX_MOUNT_ATTEMPTS:
            # 仅有限重试创建/绑定固定控件，不扫描物品或控件树。
            self._raise_pending = True
            ExecLater(0.05, self._raise_after_screen_change)

    def _raise_overlay(self):
        source = clientApi.GetTopUINode()
        if source is None or source is self.base:
            return False
        if self._view_host is not None and self._view_host.base is source and self._view_alive():
            self._view_root.SetLayer(OVERLAY_LAYER)
            return self._finish_mount()
        self._detach_view()
        # 其他模组可能直接继承 mod.client.ui.screenNode.ScreenNode，
        # 不属于 GetScreenNodeCls() 的子类；通过代理协议显式提供真实节点。
        host = ToolDeltaScreen(source.GetScreenName(), _ImporterScreenHost(source))
        # 使用原生屏全屏容器，避免面板被书页/容器内容区域裁剪。
        parent_path = "/variables_button_mappings_and_controls"
        if source.GetBaseUIControl(parent_path) is None:
            parent_path = ""
        parent = (host.GetElement(parent_path)
                  if source.GetBaseUIControl(parent_path) is not None else None)
        # 空白自定义屏可能没有可查询的根控件，None 表示直接挂到画布根。
        root = host.AddElement(PANEL_DEFINITION, "endernet_importer_overlay",
                               _parent=parent)
        self._view_host = host
        self._view_root = root
        self._view_path = root.getFullPath()
        root.SetLayer(OVERLAY_LAYER)
        return self._finish_mount()

    def _finish_mount(self):
        if self._view_bound:
            return True
        # 引擎允许下一帧刷新；不要把存在的根节点当成子控件已就绪。
        paths = ["control_board"]
        for name in ("sample_btn", "mode_btn", "save_btn", "exit_btn"):
            paths.append("control_board/" + name)
        paths.extend(("control_board/sample_btn/button_label",
                      "control_board/mode_btn/button_label"))
        for path in paths:
            if self._view_host.base.GetBaseUIControl(self._view_path + "/" + path) is None:
                return False
        if self._board_position is not None:
            self.GetElement("/control_board").SetPos(self._board_position)
        self._bind_board()
        self._view_bound = True
        return True

    def _view_alive(self):
        return (self._view_host is not None and self._view_root is not None
                and self._view_host.base.GetBaseUIControl(
                    self._view_path) is not None)

    def _detach_view(self):
        if self._view_alive():
            if self._view_bound:
                self._board_position = self.GetElement("/control_board").GetPos()
            self._view_root.Remove()
        self._view_root = None
        self._view_path = None
        self._view_host = None
        self._view_bound = False

    def _update_labels(self):
        if (not self._view_alive() or self._view_host.base.GetBaseUIControl(
                self._view_path + "/control_board/sample_btn/button_label") is None
                or self._view_host.base.GetBaseUIControl(
                    self._view_path + "/control_board/mode_btn/button_label") is None):
            return
        board = self.GetElement("/control_board")
        board.GetElement("sample_btn/button_label").asLabel().SetText(
            "关闭选择" if self.selecting else "开始选择"
        )
        board.GetElement("mode_btn/button_label").asLabel().SetText(
            "输入模式" if self.mode == "inputs" else "输出模式"
        )

    def _toggle_sampling(self, params=None):
        if self.closed:
            return
        self.selecting = not self.selecting
        self._update_labels()
        if self.selecting:
            self._scan_once()
        else:
            self._clear_sections()

    def _toggle_mode(self, params=None):
        if self.closed:
            return
        self.mode = "outputs" if self.mode == "inputs" else "inputs"
        self._update_labels()

    def _scan_once(self):
        """仅在玩家开始选择时遍历一次；平时及格子点击均不扫描控件树。"""
        if self.closed or not self.selecting or not self._view_alive():
            return
        self._clear_sections()
        source = clientApi.GetTopUINode()
        source_name = clientApi.GetTopUI()
        if source is self.base:
            source = None
        if source is not None:
            # 多个自定义屏的 GetTopUI 都可能是 main，优先用带命名空间的定义名。
            source_name = source.GetScreenName() or source_name
        self._source = source
        self._source_name = source_name
        if source is None:
            return
        origin = self._view_root.GetRootPos()
        overlay_path = self._view_root.getFullPath()
        # 显式栈进行深度遍历，避免复杂原生界面触及 Python 递归深度限制。
        # 自定义 CreateUI 有空路径根节点；原生 HUD / 容器的空路径不可枚举。
        # 使用 ToolDelta 提供的原生内容根，不能因根节点不存在而漏掉整屏。
        root_path = "" if source.GetBaseUIControl("") is not None else str(SCREEN_BASE_PATH)
        pending = [root_path]
        while pending:
            path = pending.pop()
            if path.lstrip("/") == overlay_path.lstrip("/"):
                continue
            control = source.GetBaseUIControl(path)
            if control is None or not control.GetVisible():
                continue
            renderer = control.asItemRenderer()
            item = renderer.GetUiItem() if renderer is not None else None
            if item and item.get("itemName") not in (None, "", "minecraft:air"):
                size = control.GetSize()
                if size and size[0] > 0 and size[1] > 0:
                    position = control.GetGlobalPosition()
                    if len(self._sections) < IMMEDIATE_SECTION_LIMIT:
                        self._sync_section(path, item, position, size, origin)
                    else:
                        # 保留单次扫描快照，不持有可能被引擎销毁的原生控件。
                        self._pending_sections.append((path, deepcopy(item), position, size))
            for child in reversed(source.GetChildrenName(path) or []):
                pending.append(path + "/" + child)
        if self._pending_sections:
            ExecLater(0, self._create_section_batch, self._section_generation)

    def _create_section_batch(self, generation):
        if generation != self._section_generation or self.closed or not self.selecting:
            return
        if clientApi.GetTopUINode() is not self._source or not self._view_alive():
            self._on_screen_changed()
            return
        origin = self._view_root.GetRootPos()
        for _ in range(min(SECTION_BATCH_SIZE, len(self._pending_sections))):
            path, item, position, size = self._pending_sections.popleft()
            # 只复核该批快照对应的控件；界面内容变化时跳过过期位置和物品。
            control = self._source.GetBaseUIControl(path)
            renderer = control.asItemRenderer() if control is not None else None
            current_item = renderer.GetUiItem() if renderer is not None else None
            if (not current_item or not control.GetVisible()
                    or _item_key(current_item) != _item_key(item)
                    or control.GetGlobalPosition() != position or control.GetSize() != size):
                continue
            self._sync_section(path, item, position, size, origin)
        if self._pending_sections:
            ExecLater(0, self._create_section_batch, generation)

    def _sync_section(self, path, item, position, size, origin):
        key = (self._source_name, path, _item_key(item))
        entry = self._sections.get(path)
        if entry is None:
            self._sequence += 1
            section = self._view_root.AddElement(SLOT_DEFINITION, "slot_%s" % self._sequence)
            section.SetAnchorFrom("top_left")
            section.SetAnchorTo("top_left")
            section.GetElement("img").asImage().SetSpriteColor(COLORS[None])
            section.GetElement("interact_btn").SetLayer(50).asButton().SetCallback(
                partial(self._select_slot, path)
            )
        else:
            section = entry[0]
        self._sections[path] = (section, key, deepcopy(item))
        # 全局位置转换到本悬浮层根节点；大小是已布局后的实际像素尺寸。
        section.SetPos((position[0] - origin[0], position[1] - origin[1]))
        section.SetSize(size, resize_children=True)
        selected = self._draft.get(key)
        section.GetElement("img").asImage().SetSpriteColor(
            COLORS[selected[0] if selected else None]
        )

    def _select_slot(self, path, params=None):
        if self.closed or not self.selecting:
            return
        # 点击只复核对应的单个控件，不重新遍历整屏。
        if clientApi.GetTopUINode() is not self._source:
            self._on_screen_changed()
            return
        entry = self._sections.get(path)
        if entry is None:
            return
        section, key, item = entry
        control = self._source.GetBaseUIControl(path)
        renderer = control.asItemRenderer() if control is not None else None
        current_item = renderer.GetUiItem() if renderer is not None else None
        if (not current_item or not control.GetVisible()
                or _item_key(current_item) != key[2]
                or control.GetGlobalPosition() != section.GetRootPos()
                or control.GetSize() != section.GetSize()):
            self._sections.pop(path)[0].Remove()
            return
        selected = self._draft.get(key)
        if selected is not None and selected[0] == self.mode:
            del self._draft[key]
            self._selection_order.remove(key)
            mode = None
        else:
            if selected is None:
                self._selection_order.append(key)
            self._draft[key] = (self.mode, deepcopy(item))
            mode = self.mode
        section.GetElement("img").asImage().SetSpriteColor(COLORS[mode])

    def _clear_sections(self):
        self._cancel_section_batches()
        if self._view_alive():
            for section, _, _ in self._sections.values():
                section.Remove()
        self._sections.clear()

    def _cancel_section_batches(self):
        self._section_generation += 1
        self._pending_sections.clear()

    def _save(self, params=None):
        if self.closed:
            return
        result = {"inputs": [], "outputs": []}
        for key in self._selection_order:
            mode, item = self._draft[key]
            result[mode].append(item)
        save_config(result["inputs"], result["outputs"])
        self.close()
        SetPopupNotice("配置已保存")

    def close(self, params=None):
        if self.closed:
            return
        self._stop()
        self._clear_sections()
        self._detach_view()
        self.RemoveUI()

    def _stop(self):
        global _OPEN_SCREEN
        self.closed = True
        self._cancel_section_batches()
        if _OPEN_SCREEN is self:
            _OPEN_SCREEN = None
        self._draft.clear()
        self._selection_order = []
        self._source = None

    def OnDestroy(self):
        self._stop()
        # 实际面板在其他界面中，必须随所有者一起清理。
        self._detach_view()
        self._sections.clear()
