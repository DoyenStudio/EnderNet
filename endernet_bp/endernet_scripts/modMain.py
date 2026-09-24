# coding=utf-8
"""EnderNet 终界网络 —— 网易我的世界中国版行为包入口。

本文件只负责注册模组并在服务端/客户端初始化时启动各自的 bootstrap。
网络业务、设备、存储与工厂逻辑全部位于 `endernet` 包内，见 plans/。

导入一律使用相对导入：脚本目录本身是一个包（与 ModSDK 官方结构和同环境
可运行的 SkyblueTech 一致），相对导入不依赖该包最终被挂载成什么名字。
"""

from .tooldelta.general import ClientUninitCallback, ServerUninitCallback
from .tooldelta.mod_main import RegisterMod, ToolDeltaMod


@ServerUninitCallback()
def _on_server_uninit():
    """服务端系统销毁时显式停机。

    没有这一步，`ServerEventBinder.unbind()` 永远不会被调用，
    监听器会一直留在事件总线的表里。新版 tooldelta 的 `_dyn_listen`
    **不再按函数对象去重**（旧版有 `if listener not in listeners`），
    因此热重载后新闭包会被追加进去，同一次事件触发多次回调。
    销毁钩子是把「显式注销」真正兑现的那个环节。
    """
    from .endernet.server.bootstrap import stop_server

    stop_server()


@ClientUninitCallback()
def _on_client_uninit():
    from .endernet.client.bootstrap import stop_client

    stop_client()


@RegisterMod()
class EnderNet(ToolDeltaMod):
    name = "EnderNet"
    version = (0, 0, 1)

    def OnServerInited(self):
        # 延迟导入：bootstrap 会拉起适配层，适配层依赖 mod.server.*，
        # 只在服务端系统真正初始化后才可用。
        from .endernet.server.bootstrap import start_server

        start_server()

    def OnClientInited(self):
        from .endernet.client.bootstrap import start_client

        start_client()
