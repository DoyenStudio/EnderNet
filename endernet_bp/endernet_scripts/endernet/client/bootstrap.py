# coding=utf-8
"""客户端装配与启动/销毁。

对应任务 01-01 的客户端入口。

**本批次刻意做得很薄。** 终端界面、网络槽与背包槽的自定义渲染属于任务 11，
在服务端还没有库存可显示之前实现它们没有意义。这里只保证：

- 模组在客户端能正常初始化与销毁，不会因为缺少客户端入口而报错；
- 客户端**不持有任何权威状态**（架构规划 6.6）：它不判断库存、订单完成或能量余额。

后续任务在这里挂 UI 注册与会话订阅即可，不需要改动服务端。
"""
from __future__ import absolute_import, division, print_function

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
