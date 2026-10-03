# coding=utf-8
"""Terminal UI package. Engine registration is explicit in register_screens."""


def register_screens():
    # type: () -> tuple
    """把本仓库的界面登记进引擎，返回登记过的类。

    **登记发生在导入期**：`@RegistToolDeltaScreen` 一执行就往
    `registeredToolDeltaScreenClasses` 里写一条，因此这个函数的作用就是
    把两个模块导进来。返回值只是给调用方与测试看的，`client/bootstrap.py`
    并不用它（那边 `register_screens()` 的返回值直接丢掉）。

    返回的是**元组**：界面多起来之后，返回单个类会让「还有没有别的界面」
    完全看不出来——而漏导入一个模块的后果不是报错，是**那个界面在客户端
    查不到**，`PushUIRequest` 一到就抛异常（右键合成终端什么都不开）。
    """
    from .crafting_panel import CraftingPanel
    from .device_panels import (
        AssembleChamberPanel,
        BusItemInputPanel,
        BusItemOutputPanel,
        BusTemplatePanel,
        StorageDriverPanel,
    )
    from .factoric_panel import FactoricPanel
    from .template_panel import TemplatePanel
    from .terminal_panel import CommonTerminalPanel

    return (
        CommonTerminalPanel,
        CraftingPanel,
        TemplatePanel,
        FactoricPanel,
        BusItemOutputPanel,
        BusItemInputPanel,
        BusTemplatePanel,
        AssembleChamberPanel,
        StorageDriverPanel,
    )
