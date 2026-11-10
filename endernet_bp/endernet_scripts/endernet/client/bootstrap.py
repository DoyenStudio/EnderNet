# coding=utf-8
"""客户端装配与启动/销毁。

对应任务 01-01 的客户端入口。

终端 UI 在 client/ui 中注册；物品与流体列表已经接上真实网络的会话同步
（任务 11），终端下半屏渲染的也是**真实玩家背包**（本地读引擎，见
`ui/terminal/backpack_model.py`）。这里保证：

- 模组在客户端能正常初始化与销毁，不会因为缺少客户端入口而报错；
- 客户端**不持有任何权威状态**（架构规划 6.6）：它不判断库存、订单完成或能量余额。

**界面上的操作会真的动东西**：取一组是把网络里的物品放进玩家背包，存一组
是把背包里的物品送进网络，两者都由服务端裁决并回执。客户端只负责发起与显示，
`inv_take` / `inv_put` 的实际搬运与账都在服务端（`server/storage/service.py`）。
"不持有权威状态"说的是**不做裁决**，不是"不改动世界"。
"""

#: 进程内的客户端单例
_CLIENT = None


class ClientBootstrap(object):
    """客户端运行时装配。"""

    def __init__(self):
        self.started = False
        #: 显示缓存与订阅（任务 11）。当前为空是刻意的。
        self.subscriptions = {}

    def start(self):
        # type: () -> None
        if self.started:
            return
        self.started = True

    def stop(self):
        # type: () -> None
        """显式销毁：解除全部订阅，避免设备拆除后仍在收增量。"""
        self.subscriptions = {}
        self.started = False

    def diagnostics(self):
        # type: () -> dict
        from .channels import get_cable_intent
        from .channels import get_device_identity
        from .channels import get_slot_display

        service = get_device_identity()
        slots = get_slot_display()
        cables = get_cable_intent()
        return {
            "started": self.started,
            "subscriptions": len(self.subscriptions),
            # 设备身份下行（11-07）。`rejected` 涨而 `accepted` 不涨 = 服务端
            # 推过来了但两边的形状已经分家；`accepted` 涨而 `taken` 不涨 =
            # 屏没认领（多半是屏幕名对不上）。**这是那条链路的第一个落点**，
            # 因此挂在这里而不是只留在模块里。
            "device_identity": (
                service.diagnostics() if service is not None else None
            ),
            # 槽位模块显示（2026-11-05）。`requested` 涨而 `applied` 不涨 =
            # 服务端没回（看服务端 `slot_displays.unknown_requests`）；
            # `set_failures` 涨 = 外观还没就绪就写了变量。
            "slot_display": slots.diagnostics() if slots is not None else None,
            # 光缆交互意图（阶段 C）。`intercepted` 涨而 `sent` 不涨：看 `ray_failures`
            # （引擎没有 3.10 的射线接口）与 `deduplicated`（按住时的重复）。
            "cable_intent": cables.diagnostics() if cables is not None else None,
            "scope_note": "客户端不持有权威状态；界面与订阅属于任务 11。",
        }


def get_client():
    # type: () -> ClientBootstrap
    return _CLIENT


def start_client():
    # type: () -> ClientBootstrap
    """由 `modMain.OnClientInited` 调用。"""
    global _CLIENT
    from .channels import start_cable_intent
    from .channels import start_device_identity
    from .channels import start_slot_display
    from .ui import register_screens

    register_screens()
    # 设备身份下行（11-07）：容器类设备的屏拿不到参数，身份由服务端推过来，
    # 这条监听必须在屏可能被打开之前就挂上——因此它在启动时起，而不是在
    # 某个屏的 `OnCreate` 里起（那时推送可能已经发过了）。
    start_device_identity()
    # 槽位模块显示（2026-11-05）：同理要在区块里的方块实体外观加载之前挂上，
    # 否则进世界头一批 `ModBlockEntityLoadedClientEvent` 没人接，那些设备
    # 要等下一次插拔才显示出来。
    start_slot_display()
    # 光缆交互意图（阶段 C）：点中光缆时取射线、发意图。点按要先记触点，所以与上面
    # 两条一样在启动时就挂上。
    start_cable_intent()
    if _CLIENT is not None:
        _CLIENT.stop()
    _CLIENT = ClientBootstrap()
    _CLIENT.start()
    return _CLIENT


def stop_client():
    # type: () -> None
    global _CLIENT
    from .channels import stop_cable_intent
    from .channels import stop_device_identity
    from .channels import stop_slot_display
    from .ui.universal_importer.screen import close_universal_importer

    close_universal_importer()
    stop_cable_intent()
    stop_slot_display()
    stop_device_identity()
    if _CLIENT is not None:
        _CLIENT.stop()
        _CLIENT = None
