# coding=utf-8
"""客户端装配与启动/销毁。

对应任务 01-01 的客户端入口。

终端 UI 在 client/ui 中注册；物品与流体列表已经接上真实网络的会话同步
（任务 11），终端下半屏渲染的也是**真实玩家背包**（本地读引擎，见
`ui/backpack_model.py`）。这里保证：

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
        return {
            "started": self.started,
            "subscriptions": len(self.subscriptions),
            "scope_note": "客户端不持有权威状态；界面与订阅属于任务 11。",
        }


def get_client():
    # type: () -> ClientBootstrap
    return _CLIENT


def start_client():
    # type: () -> ClientBootstrap
    """由 `modMain.OnClientInited` 调用。"""
    global _CLIENT
    from .ui import register_screens

    register_screens()
    if _CLIENT is not None:
        _CLIENT.stop()
    _CLIENT = ClientBootstrap()
    _CLIENT.start()
    return _CLIENT


def stop_client():
    # type: () -> None
    global _CLIENT
    if _CLIENT is not None:
        _CLIENT.stop()
        _CLIENT = None
