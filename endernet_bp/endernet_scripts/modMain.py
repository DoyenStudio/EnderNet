# coding=utf-8
"""EnderNet 终界网络 —— 网易我的世界中国版行为包入口。

本文件只负责注册模组并在服务端/客户端初始化时启动各自的 bootstrap。
网络业务、设备、存储与工厂逻辑全部位于 `endernet` 包内，见 plans/。

导入一律使用相对导入：脚本目录本身是一个包（与 ModSDK 官方结构和同环境
可运行的 SkyblueTech 一致），相对导入不依赖该包最终被挂载成什么名字。
"""
from __future__ import absolute_import, division, print_function

from .tooldelta.mod_main import RegisterMod, ToolDeltaMod


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
