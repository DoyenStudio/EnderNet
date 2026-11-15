# coding=utf-8
"""Independent machines use native slots and server-owned FE/progress snapshots."""
import time

from .....tooldelta.ui import Binder, RegistToolDeltaScreen, ToolDeltaScreen, SCREEN_BASE_PATH
from ...channels.production import get_production_client
from ..shared.production_gauges import ProductionGauges

MODE_NAMES = {"grind": "粉碎", "press": "压印", "assemble": "封装"}
STATE_NAMES = {
    "idle": "等待原料", "working": "加工中", "running": "加工中", "charging": "充能中",
    "no_energy": "电力不足", "no_recipe": "等待匹配的原料", "output_full": "成品槽已满",
    "no_fuel": "等待煤炭或木炭", "full": "能量缓存已满", "generating": "发电中",
    "paused": "已暂停", "blocked": "输出受阻", "invalid": "机器不可用",
    "complete": "加工完成", "waiting_input": "等待原料", "no_power": "电力不足",
    "waiting_fuel": "等待煤炭或木炭", "buffer_full": "能量缓存已满",
    "recovery_error": "存档恢复失败", "write_failed": "容器写入失败", "error": "机器异常",
}


class ProductionPanel(ToolDeltaScreen):
    DEVICE_ID = None
    USE_GAUGES = False
    IS_GENERATOR = False
    PROGRESS_DIRECTION = "fromTopToBottom"

    def __init__(self, screen_name, screen_instance, params=None):
        ToolDeltaScreen.__init__(self, screen_name, screen_instance, params)
        self.session = None
        self.last_poll = 0.0
        self.previous = None
        self.gauges = None

    def OnCreate(self):
        if self.USE_GAUGES:
            self.gauges = ProductionGauges(
                self, str(SCREEN_BASE_PATH) + "/window/root_panel/bg",
                self.IS_GENERATOR, self.PROGRESS_DIRECTION)
        self.OnTicking()

    def OnTicking(self):
        client = get_production_client()
        if client is None:
            return
        if self.session is None:
            self.session = client.cache.take(self.DEVICE_ID)
        if self.session is None:
            return
        now = time.time()
        if now - self.last_poll >= 1.0:
            client.send(self.session, "poll")
            self.last_poll = now
        current = client.cache.current.get(self.session)
        if current != self.previous:
            self.previous = dict(current) if current is not None else None
            if self.gauges is not None:
                self.gauges.update(current or {})
            self._screen_node.UpdateScreen(True)

    def OnDestroy(self):
        client = get_production_client()
        if client is not None and self.session is not None:
            client.close(self.session)
        self.session = None
        self.gauges = None

    def snapshot(self):
        client = get_production_client()
        return client.cache.current.get(self.session, {}) if client is not None else {}

    def change_mode(self, mode):
        client = get_production_client()
        if client is not None and self.session is not None:
            client.send(self.session, "mode", mode)

    @Binder.binding(Binder.BF_BindString, "#EnderNetProduction.energy")
    def energy_text(self):
        data = self.snapshot()
        if not data:
            return "等待能源数据"
        return "能量：%s / %s FE" % (data.get("energy", 0), data.get("capacity", 0))

    @Binder.binding(Binder.BF_BindString, "#EnderNetProduction.progress")
    def progress_text(self):
        data = self.snapshot()
        if not data:
            return "等待机器"
        if self.DEVICE_ID == "endernet:resonance_generator":
            return "剩余燃烧：%s tick" % data.get("burn", 0)
        duration = max(1, int(data.get("duration", 80)))
        return "加工进度：%s%%" % min(100, int(data.get("progress", 0)) * 100 // duration)

    @Binder.binding(Binder.BF_BindString, "#EnderNetProduction.mode")
    def mode_text(self):
        data = self.snapshot()
        return "%s · %s" % (MODE_NAMES.get(data.get("mode"), "等待模式"), self.progress_text())

    @Binder.binding(Binder.BF_BindString, "#EnderNetProduction.status")
    def status_text(self):
        data = self.snapshot()
        if not data:
            return "等待机器"
        return data.get("message") or STATE_NAMES.get(data.get("status"), "等待机器状态")

    @Binder.binding(Binder.BF_ButtonClickUp, "#EnderNetProduction.grind")
    def on_grind(self, params):
        self.change_mode("grind")

    @Binder.binding(Binder.BF_ButtonClickUp, "#EnderNetProduction.press")
    def on_press(self, params):
        self.change_mode("press")

    @Binder.binding(Binder.BF_ButtonClickUp, "#EnderNetProduction.assemble")
    def on_assemble(self, params):
        self.change_mode("assemble")


@RegistToolDeltaScreen("endernet_resonance_charger.main", is_proxy=True)
class ResonanceChargerPanel(ProductionPanel):
    DEVICE_ID = "endernet:resonance_charger"
    USE_GAUGES = True


@RegistToolDeltaScreen("endernet_resonance_inscriber.main", is_proxy=True)
class ResonanceInscriberPanel(ProductionPanel):
    DEVICE_ID = "endernet:resonance_inscriber"
    USE_GAUGES = True
    PROGRESS_DIRECTION = "fromRightToLeft"

    @Binder.binding(Binder.BF_BindBool, "#EnderNetProduction.grind_selected")
    def grind_selected(self):
        return self.snapshot().get("mode") == "grind"

    @Binder.binding(Binder.BF_BindBool, "#EnderNetProduction.press_selected")
    def press_selected(self):
        return self.snapshot().get("mode") == "press"

    @Binder.binding(Binder.BF_BindBool, "#EnderNetProduction.assemble_selected")
    def assemble_selected(self):
        return self.snapshot().get("mode") == "assemble"


@RegistToolDeltaScreen("endernet_resonance_generator.main", is_proxy=True)
class ResonanceGeneratorPanel(ProductionPanel):
    DEVICE_ID = "endernet:resonance_generator"
    USE_GAUGES = True
    IS_GENERATOR = True


PRODUCTION_PANELS = (ResonanceChargerPanel, ResonanceInscriberPanel, ResonanceGeneratorPanel)
