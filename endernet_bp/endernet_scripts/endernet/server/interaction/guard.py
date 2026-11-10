# coding=utf-8
"""服务端再拦一道原版交互：`ServerItemUseOnEvent` 命中规则就取消这次物品使用（放方块、用物品）。

客户端接管时已经拦过（`client/interaction/takeover.py`：`ClientItemUseOnEvent` 置 ret，服务端
同名事件随之不再触发）；这里以服务端为准再拦一道，客户端那一下没拦住（读引擎失败、事件
先到了服务端）也不会落成原版放方块。右键开界面（`opening.py`）的「手里的方块不放下」也是这里
的一条规则。**不 import 引擎**：规则是纯判定，引擎事件由
`server/adapters/events.py::ServerEventBinder._on_item_use_on` 翻译成 `opening.BlockUse` 再问这里。

一条规则 = 被点的方块（集合，None 表示任何方块）+ 手持物品（集合，None 表示任何物品）+ 再问
一句（`when(BlockUse)`，读世界、求交之类，方块与物品都对上之后才问）+ 潜行时放不放行。
潜行要读引擎，只在某条规则其余条件都对上时才问（`is_sneaking` 是零参可调用对象）。现有三条：

| 规则 | 方块 | 物品 | 再问 | 潜行 |
| --- | --- | --- | --- | --- |
| `open`（`ServerEventBinder` 登记） | 任何 | 任何 | `Openers.claim`：点中的是会开界面的东西（整方块终端、面板） | 放行（贴着它放方块，同蔚蓝） |
| `add_cable`（`PanelService.register` 登记） | 面板底座 | 三种光缆 | — | 放行（潜行照原版放方块，同客户端 `action_for`） |
| `panel_item`（`PanelService.register` 登记） | 任何 | 四种终端面板 | — | 不放行：面板的物品形态是展示方块，放不下；挂面板走客户端接管的意图 |
"""


class GuardRule(object):
    """一条「取消原版物品使用」的规则。"""

    __slots__ = ("name", "blocks", "items", "when", "unless_sneaking")

    def __init__(self, name, blocks=None, items=None, when=None, unless_sneaking=True):
        # type: (str, object, object, object, bool) -> None
        self.name = name
        #: None = 任何方块
        self.blocks = frozenset(blocks) if blocks is not None else None
        #: None = 任何物品（含空手）
        self.items = frozenset(items) if items is not None else None
        #: `when(BlockUse) -> bool`；None = 不再问
        self.when = when
        #: 潜行时放行（交给原版）
        self.unless_sneaking = bool(unless_sneaking)

    def matches(self, use):
        # type: (object) -> bool
        if self.blocks is not None and use.block not in self.blocks:
            return False
        if self.items is not None and use.item_id not in self.items:
            return False
        return self.when is None or bool(self.when(use))

    def __repr__(self):
        return "GuardRule(%s)" % (self.name,)


class UseGuard(object):
    """规则表。同名再登记就覆盖（换实现时不必先摘）。"""

    def __init__(self):
        self._rules = []
        self.stats = {}

    def forbid(self, name, blocks=None, items=None, when=None, unless_sneaking=True):
        # type: (str, object, object, object, bool) -> None
        rule = GuardRule(name, blocks, items, when, unless_sneaking)
        self._rules = [old for old in self._rules if old.name != name] + [rule]

    def rules(self):
        # type: () -> list
        return [rule.name for rule in self._rules]

    def check(self, use, is_sneaking):
        # type: (object, object) -> object
        """这次物品使用（`opening.BlockUse`）该不该取消：命中的规则名，不该取消返回 None。
        `is_sneaking()` 只在某条规则其余条件都对上、且它潜行放行时才调用（读引擎）。"""
        sneaking = None
        for rule in self._rules:
            if not rule.matches(use):
                continue
            if rule.unless_sneaking:
                if sneaking is None:
                    sneaking = bool(is_sneaking())
                if sneaking:
                    continue
            self.stats[rule.name] = self.stats.get(rule.name, 0) + 1
            return rule.name
        return None

    def diagnostics(self):
        # type: () -> dict
        return {"rules": self.rules(), "cancelled": dict(self.stats)}
