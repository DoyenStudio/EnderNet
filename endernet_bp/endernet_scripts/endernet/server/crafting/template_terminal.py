# coding=utf-8
"""样板终端（11-05）的设备侧状态。

三处分工，与 `workbench.py` 开头那段话同形：

- `patterns.py`：一块样板**是什么**（结构、编解码、校验）；
- `store.py`：**哪台样板提供者**的哪一格里是哪块样板；
- 本模块：**样板终端这台设备**自己有什么——两个真实样板槽（输入／输出）
  与一份**服务端权威的虚拟配置**（模式 + 输入九宫格 + 输出格）。

**本模块是领域层**：不 import 引擎（`test_import_boundaries` 静态拦着）。
需要引擎的两件事都靠回调注入：拆方块时的掉落（`drop=`），以及某个资源键的
堆叠上限（`limit_of=`）。

## 为什么真实槽不是引擎容器

`endernet:template_terminal_panel` **没有方块容器组件**（不在
`scripts/gen_blocks.py` 的 `CONTAINERS` 里），因此那两个「真实样板槽」在引擎
侧根本不存在——`plans/ui/template_terminal_panel.md` 第 5 节写的「普通真实槽位
存放」落不到引擎的容器交互上。它们由本模块按设备实例保存，一切进出都走命令
（`template_input_put` / `template_input_take` / `template_output_take`）。

这也是这里**没有** `ContainerAdapter` 的原因：加一个容器会让转移协调器能够
绕开「输入槽只收工厂样板」那道闸，而那道闸是本次交接明写的规则。

## 修订号为什么两条状态共用一个计数

客户端拿最后一次收到的修订号回填 `slot_version`。真实槽与虚拟配置都能变，
若各记各的号，「只动过真实槽」与「只动过配置」会让客户端手里的号与其中一条
撞上，于是**一次陈旧编辑会被当成新的**。共用同一个计数就不会：任何一处变化
都让号往前走，客户端手里的号只有「一模一样」时才相等。
"""

from ...common.crafting_recipe import SLOT_COUNT, match
from ...common.definitions import (
    PATTERN_MODE_CRAFTING,
    PATTERN_MODE_FACTORY,
    PATTERN_MODES,
    TEMPLATE_PANEL_ITEM_ID,
)
from ...common.item_codec import item_dict_from_key
from ...common.resource_key import ResourceKey
from ..runtime.lifecycle import ChangeKind
from . import patterns
from .patterns import PatternStack, PatternValidationError

try:
    INTEGER_TYPES = (int, long)
except NameError:
    INTEGER_TYPES = (int,)

#: 本模块存档结构的版本。形状变了就加一，恢复时按它决定读还是跳过。
TEMPLATE_SCHEMA_VERSION = 1

#: 真实槽的下标。命令层的 `slot` 字段用的就是这两个。
INPUT_SLOT = 0
RESULT_SLOT = 1
REAL_SLOT_COUNT = 2

#: 虚拟输入九宫格与虚拟输出格。前者与工作台同形（3×3），后者是界面上的 1×3。
SOURCE_CELL_COUNT = SLOT_COUNT
OUTPUT_CELL_COUNT = 3

#: 合成模式下**唯一有内容**的输出格：中间那一格，检测到的产物放在这里。
#: 上下两格在合成模式下空着且锁定，界面按模式把它们整个藏掉
#: （User 2026-10-02 定）。客户端那边另有一份同名常量——两层不互相 import，
#: 与 `OUTPUT_CELL_COUNT` 在 `protocol.py` 里再写一份同一个理由。
CRAFT_PREVIEW_CELL = 1

#: 虚拟配置的两组。**用名字而不是下标区分**：两组的下标空间不同
#: （0～8 与 0～2），一个整数说不清它属于哪一组。
GROUP_SOURCE = "source"
GROUP_OUTPUTS = "outputs"
GROUPS = (GROUP_SOURCE, GROUP_OUTPUTS)
CELL_COUNT_OF = {
    GROUP_SOURCE: SOURCE_CELL_COUNT,
    GROUP_OUTPUTS: OUTPUT_CELL_COUNT,
}

# -- 策略取值 --------------------------------------------------------------

#: 产物槽的门限：空，或已有样板与本次配置**完全相同**且有空间。
OUTPUT_GATE_EMPTY_OR_SAME = "empty_or_same"
#: 只看有没有空间。
OUTPUT_GATE_ROOM_ONLY = "room_only"

#: 输入槽接受哪些工厂样板物品。`any` = 空白与已编码都收。
INPUT_ACCEPTS_ANY = "any"
INPUT_ACCEPTS_BLANK_ONLY = "blank_only"

#: 背包 → 真实槽一次搬几个。
PUT_COUNT_STACK = "stack"
PUT_COUNT_ONE = "one"

#: 拆方块时两个真实槽的实物怎么处置。
BREAK_DROP = "drop"
BREAK_KEEP = "keep"

#: 合成模式多条候选时取哪一条。
PREVIEW_PICK_FIRST = "first"

#: 编出的样板声明的返还物。**本界面没有承载返还物的控件**（交接未提），
#: 因此只能是「不声明」。R09 的定案是「返还物一律由样板显式声明」——
#: 这一条因此是一个**如实记录的缺口**，不是一个口径选择。
RETURNS_NONE = "none"

#: 没有注入 `limit_of` 时的堆叠上限。取 1 是保守那侧：搬少了玩家再点一次，
#: 搬多了会凭空占掉格子。生产装配一律注入定义表那一份，见
#: `bootstrap._template_stack_limit`。
DEFAULT_STACK_LIMIT = 1


class TemplatePolicy(object):
    """11-05 未定规则的显式口径。

    与 `BandwidthPolicy` / `EnergyPolicy` / `CraftingPolicy` 同一套路数：
    `plans/ui/template_terminal_panel.md` 第 8 节列了若干未确认项，它们
    **不能靠临时实现悄悄定案**。这里把与代码有关的那几条提成具名字段，
    并在 `UNRESOLVED` 里列出哪些还没有定论。

    2026-10-02 User 已逐条裁定的那些（数量范围、编码默认数量、制作门限、
    模式切换行为、拆方块处置、多候选取第一条……）**留成字段但不进
    `UNRESOLVED`**：它们是本轮的口径，不是替身。留成字段是为了将来改玩法时
    改一处、而不是满仓库找魔法数字。
    """

    #: 虚拟格数量的上限。User 2026-10-02 定：1～65535。
    max_config_count = 65535
    #: 往虚格放入时的初始数量。User 2026-10-02 定：每格 1。
    encode_count_per_cell = 1
    #: 一次制作消耗几个输入槽样板。User 2026-10-02 定：1。
    encode_input_cost = 1
    #: 一次制作产出几块。User 2026-10-02 定：1。
    encode_output_count = 1
    #: 模式切换时保留输入九宫格、只清输出格。User 2026-10-02 定。
    mode_switch_keeps_source = True
    mode_switch_clears_outputs = True
    #: 拆方块时两个真实槽的实物掉落为掉落物。User 2026-10-02 定。
    break_disposition = BREAK_DROP
    #: 合成模式多候选只取引擎给的第一条。User 2026-10-02 定，
    #: 理由：同样匹配的配方在引擎层会被更高优先级的模组盖掉，不必自作聪明。
    preview_pick = PREVIEW_PICK_FIRST

    #: 产物槽门限。User 2026-10-02 定为 `empty_or_same`；另一侧 `room_only`
    #: 只是留在字段里的备选，不是本轮口径。
    encode_output_gate = OUTPUT_GATE_EMPTY_OR_SAME
    #: 输入槽接受范围。User 2026-10-02 定为 `any`。
    input_accepts = INPUT_ACCEPTS_ANY
    #: 背包 → 真实输入槽一次搬几个。取「一组」与通用终端 `inv_put` 的既有
    #: 默认同一口径；**未与 User 单独确认过**，因此进 `UNRESOLVED`。
    device_slot_put_count = PUT_COUNT_STACK
    #: 工厂模式下「第一格空、后两格非空」该不该拒绝。**未定**。
    factory_output_requires_primary = False
    #: 编出的样板声明的返还物。**未定且本界面无承载**，见 `RETURNS_NONE`。
    returns_source = RETURNS_NONE

    #: 还没定案的条目，只列与代码行为有关的。
    UNRESOLVED = (
        "device_slot_put_count",
        "factory_output_requires_primary",
        "returns_source",
    )

    def describe(self):
        # type: () -> dict
        return {
            "max_config_count": self.max_config_count,
            "encode_count_per_cell": self.encode_count_per_cell,
            "encode_input_cost": self.encode_input_cost,
            "encode_output_count": self.encode_output_count,
            "mode_switch_keeps_source": self.mode_switch_keeps_source,
            "mode_switch_clears_outputs": self.mode_switch_clears_outputs,
            "break_disposition": self.break_disposition,
            "preview_pick": self.preview_pick,
            "encode_output_gate": self.encode_output_gate,
            "input_accepts": self.input_accepts,
            "device_slot_put_count": self.device_slot_put_count,
            "factory_output_requires_primary": self.factory_output_requires_primary,
            "returns_source": self.returns_source,
        }

    def unresolved(self):
        # type: () -> tuple
        """仍未定案的条目名。验收时对着它逐条问，而不是对着代码猜。"""
        return self.UNRESOLVED

    def unresolved_report(self):
        # type: () -> dict
        return {
            "policy": self.describe(),
            "unresolved": list(self.UNRESOLVED),
            "scope_note": (
                "R09「两类样板适用范围」仍未定（`PatternPolicy.kind_scope`）；"
                "本界面两种模式编出的物品恒为工厂样板，不替它定案。"
            ),
        }

    def assert_settled(self):
        """**只在正式验收时调用**，开发与离线测试都不该调。

        与 `BandwidthPolicy` 的同名方法同一个理由：它问的是「上面这些口径
        有没有被正式裁定过」，而不是「代码对不对」。`test_storage.py` 有一条
        静态守卫扫全仓，禁止这个调用形状出现在业务代码里——**因此本方法的
        文档也不会写出那个调用形状**，否则守卫扫到的是注释。
        """
        if self.UNRESOLVED:
            raise AssertionError(
                "样板终端仍有未定口径：%s（见 plans/ui/template_terminal_panel.md 第 8 节）"
                % ("、".join(self.UNRESOLVED),)
            )


class Revision(object):
    """一次变更加一。**真实槽与虚拟配置共用同一个计数**（见模块文档）。"""

    __slots__ = ("value",)

    def __init__(self, value=0):
        # type: (int) -> None
        self.value = value

    def bump(self):
        # type: () -> int
        self.value += 1
        return self.value

    def __repr__(self):
        return "Revision(%d)" % (self.value,)


def _cell_count(group):
    # type: (str) -> int
    try:
        return CELL_COUNT_OF[group]
    except KeyError:
        raise ValueError("未知的虚拟格分组 %r（可用：%s）"
                         % (group, "、".join(GROUPS)))


def _check_index(index, count, label):
    # type: (object, int, str) -> int
    """下标必须是 `0 <= index < count` 的整数。`bool` 挡掉——
    `isinstance(True, int)` 为真，而 `True` 当 1 用是一个安静的越界。
    `long` 也要收：命令载荷是 JSON，py2 下大一点的整数解析出来就是 `long`。"""
    if isinstance(index, bool) or not isinstance(index, INTEGER_TYPES):
        raise ValueError("%s下标必须是整数，得到 %r" % (label, index))
    if not 0 <= index < count:
        raise ValueError("%s下标 %d 越界（共 %d 格）" % (label, index, count))
    return index


def _cells_from(slots, count, label):
    # type: (object, int, str) -> list
    if slots is None:
        return [None] * count
    rows = list(slots)
    if len(rows) != count:
        raise ValueError("%s必须是 %d 格，得到 %d" % (label, count, len(rows)))
    return rows


def _entry_dict(entry):
    # type: (object) -> object
    if entry is None:
        return None
    return {"key": entry[0].to_dict(), "count": entry[1]}


def _entry_from(row):
    # type: (object) -> object
    if row is None:
        return None
    return (ResourceKey.from_dict(row["key"]), row["count"])


# -- 两个真实样板槽 --------------------------------------------------------

class TemplateSlots(object):
    """设备自己的两个真实样板槽：`INPUT_SLOT`（待编码／待更新）与 `RESULT_SLOT`。

    每格是 `(ResourceKey, count)` 或 `None`。**容量不是这里管的**——某个资源键
    能放几个由 `TemplateStore.limit_of` 回答，而那是注入的引擎面（物品的堆叠
    上限取自定义表）。模型只保证「放进去的东西是什么、有几个」。
    """

    __slots__ = ("slots", "revision", "generation")

    def __init__(self, slots=None, revision=None, generation=0):
        # type: (object, object, int) -> None
        self.slots = _cells_from(slots, REAL_SLOT_COUNT, "样板槽")
        self.revision = revision if revision is not None else Revision()
        self.generation = generation

    # -- 读 ------------------------------------------------------------

    def entry(self, index):
        # type: (int) -> object
        return self.slots[index]

    def key_at(self, index):
        # type: (int) -> object
        entry = self.slots[index]
        return None if entry is None else entry[0]

    def count_at(self, index):
        # type: (int) -> int
        entry = self.slots[index]
        return 0 if entry is None else entry[1]

    def is_empty(self):
        # type: () -> bool
        return all(entry is None for entry in self.slots)

    def occupied_slots(self):
        # type: () -> tuple
        return tuple(index for index in range(REAL_SLOT_COUNT)
                     if self.slots[index] is not None)

    def total_items(self):
        # type: () -> int
        return sum(entry[1] for entry in self.slots if entry is not None)

    # -- 写 ------------------------------------------------------------

    def set_slot(self, index, key, count):
        # type: (int, object, int) -> None
        _check_index(index, REAL_SLOT_COUNT, "样板槽")
        if key is None or count <= 0:
            self.slots[index] = None
        else:
            self.slots[index] = (key, count)
        self.revision.bump()

    def add_at(self, index, key, count):
        # type: (int, object, int) -> int
        """往某一格加东西。**同种才叠，异种不挤**——返回实际加进去的数量。"""
        _check_index(index, REAL_SLOT_COUNT, "样板槽")
        entry = self.slots[index]
        if entry is None:
            self.slots[index] = (key, count)
            self.revision.bump()
            return count
        if entry[0] != key:
            return 0
        self.slots[index] = (entry[0], entry[1] + count)
        self.revision.bump()
        return count

    def take_at(self, index, count):
        # type: (int, int) -> int
        """从某一格取走。返回实际取走的数量（可能少于请求）。"""
        _check_index(index, REAL_SLOT_COUNT, "样板槽")
        entry = self.slots[index]
        if entry is None or count <= 0:
            return 0
        moved = min(count, entry[1])
        left = entry[1] - moved
        self.slots[index] = None if left <= 0 else (entry[0], left)
        self.revision.bump()
        return moved

    # -- 序列化 --------------------------------------------------------

    def to_dict(self):
        # type: () -> dict
        return {
            "schema_version": TEMPLATE_SCHEMA_VERSION,
            "slots": [_entry_dict(entry) for entry in self.slots],
        }

    @classmethod
    def from_dict(cls, payload, revision=None):
        # type: (dict, object) -> TemplateSlots
        rows = _cells_from(payload.get("slots"), REAL_SLOT_COUNT, "样板槽")
        return cls([_entry_from(row) for row in rows], revision=revision)

    def __repr__(self):
        return "TemplateSlots(occupied=%r)" % (self.occupied_slots(),)


# -- 虚拟配置 --------------------------------------------------------------

class TemplateConfig(object):
    """服务端权威的虚拟配置：模式 + 输入九宫格 + 输出格。

    **虚格里没有实物**（交接第 1 节：「物品仅代表配方配置」）。因此这里
    存的是资源键与数量，不接任何转移服务；一次 `set_cell` 不会动网络库存，
    也不会动玩家背包——它只是一条配置。真物品只在两个真实槽里。

    **它按设备存**（User 2026-10-02 裁定）：关掉界面再打开还在。
    """

    __slots__ = ("mode", "source", "outputs", "revision")

    def __init__(self, mode=PATTERN_MODE_CRAFTING, source=None, outputs=None,
                 revision=None):
        # type: (str, object, object, object) -> None
        if mode not in PATTERN_MODES:
            raise ValueError("未知样板模式: %r" % (mode,))
        self.mode = mode
        self.source = _cells_from(source, SOURCE_CELL_COUNT, "输入九宫格")
        self.outputs = _cells_from(outputs, OUTPUT_CELL_COUNT, "输出格")
        self.revision = revision if revision is not None else Revision()

    # -- 读 ------------------------------------------------------------

    def cells_of(self, group):
        # type: (str) -> list
        return self.source if group == GROUP_SOURCE else self.outputs

    def cell(self, group, index):
        # type: (str, int) -> object
        _check_index(index, _cell_count(group), "虚拟格")
        return self.cells_of(group)[index]

    def count_of(self, group, index):
        # type: (str, int) -> int
        entry = self.cell(group, index)
        return 0 if entry is None else entry[1]

    def occupied(self, group):
        # type: (str) -> tuple
        cells = self.cells_of(group)
        return tuple(index for index in range(len(cells))
                     if cells[index] is not None)

    def is_empty(self, group):
        # type: (str) -> bool
        return not self.occupied(group)

    def cells(self, group):
        # type: (str) -> tuple
        """给 `crafting_recipe.match` 用的形状：`(名字, aux, 数量)` 或 `None`。

        **这里才把 `ResourceKey` 摊成名字 + aux**：配方匹配只看这两样
        （原版也不看附魔与自定义数据），与 `WorkbenchGrid.cells()` 同一口径。
        """
        result = []
        for entry in self.cells_of(group):
            if entry is None:
                result.append(None)
            else:
                result.append((entry[0].name, entry[0].aux, entry[1]))
        return tuple(result)

    def locked(self, group, index):
        # type: (str, int) -> bool
        """这一格在当前模式下能不能改。

        **合成模式的三个输出格全部锁定**：上下两格不显示、中间那格是服务端
        自动检测出来的预览，玩家都不能改（交接第 4 节）。工厂模式的输出格与
        输入九宫格一样可编辑，因此不锁。
        """
        _check_index(index, _cell_count(group), "虚拟格")
        if group == GROUP_OUTPUTS:
            return self.mode == PATTERN_MODE_CRAFTING
        return False

    # -- 写 ------------------------------------------------------------

    def set_mode(self, mode, policy=None):
        # type: (str, object) -> bool
        """切模式。**保留输入九宫格，只清输出格**（User 2026-10-02 定）。

        返回是否真的变了——没变就不加修订号，免得客户端白收一条推送。
        """
        if mode not in PATTERN_MODES:
            raise ValueError("未知样板模式: %r" % (mode,))
        if mode == self.mode:
            return False
        policy = policy if policy is not None else TemplatePolicy()
        self.mode = mode
        if not policy.mode_switch_keeps_source:
            self.source = [None] * SOURCE_CELL_COUNT
        if policy.mode_switch_clears_outputs:
            self.outputs = [None] * OUTPUT_CELL_COUNT
        self.revision.bump()
        return True

    def set_cell(self, group, index, key, count):
        # type: (str, int, object, int) -> None
        """放入或覆盖。**允许覆盖**（User 2026-10-02 定：直接覆盖）。"""
        _check_index(index, _cell_count(group), "虚拟格")
        if key is None or count <= 0:
            self.cells_of(group)[index] = None
        else:
            self.cells_of(group)[index] = (key, count)
        self.revision.bump()

    def replace_source(self, source):
        # type: (list) -> None
        """一次替换虚拟九宫格，未用槽清空，整份配置只递增一次修订号。"""
        self.source = _cells_from(source, SOURCE_CELL_COUNT, "输入九宫格")
        self.revision.bump()

    def clear(self):
        # type: () -> bool
        """清空全部虚拟配置；一次修改只推进一次修订号。"""
        if not any(self.source) and not any(self.outputs):
            return False
        self.source = [None] * SOURCE_CELL_COUNT
        self.outputs = [None] * OUTPUT_CELL_COUNT
        self.revision.bump()
        return True

    def replace_factory(self, source, outputs):
        # type: (list, list) -> None
        """原子替换工厂输入与输出；两组全部合法后才改动现有配置。"""
        if self.mode != PATTERN_MODE_FACTORY:
            raise ValueError("只有工厂模式支持批量导入")
        source = _cells_from(source, SOURCE_CELL_COUNT, "输入九宫格")
        outputs = _cells_from(outputs, OUTPUT_CELL_COUNT, "输出格")
        self.source = source
        self.outputs = outputs
        self.revision.bump()

    def clear_cell(self, group, index):
        # type: (str, int) -> bool
        """清空一格。**空格子返回假**——调用方据此决定要不要发通知。"""
        _check_index(index, _cell_count(group), "虚拟格")
        cells = self.cells_of(group)
        if cells[index] is None:
            return False
        cells[index] = None
        self.revision.bump()
        return True

    def set_count(self, group, index, count):
        # type: (str, int, int) -> bool
        """改一格的数量。空格子返回假（没有东西可以改数量）。"""
        _check_index(index, _cell_count(group), "虚拟格")
        cells = self.cells_of(group)
        entry = cells[index]
        if entry is None or count <= 0 or count == entry[1]:
            return False
        cells[index] = (entry[0], count)
        self.revision.bump()
        return True

    # -- 序列化 --------------------------------------------------------

    def to_dict(self):
        # type: () -> dict
        return {
            "schema_version": TEMPLATE_SCHEMA_VERSION,
            "mode": self.mode,
            "source": [_entry_dict(entry) for entry in self.source],
            "outputs": [_entry_dict(entry) for entry in self.outputs],
        }

    @classmethod
    def from_dict(cls, payload, revision=None):
        # type: (dict, object) -> TemplateConfig
        source = _cells_from(payload.get("source"), SOURCE_CELL_COUNT, "输入九宫格")
        outputs = _cells_from(payload.get("outputs"), OUTPUT_CELL_COUNT, "输出格")
        return cls(payload.get("mode", PATTERN_MODE_CRAFTING),
                   [_entry_from(row) for row in source],
                   [_entry_from(row) for row in outputs],
                   revision=revision)

    def __repr__(self):
        return "TemplateConfig(mode=%s, source=%r, outputs=%r)" % (
            self.mode, self.occupied(GROUP_SOURCE), self.occupied(GROUP_OUTPUTS),
        )


class TemplateDeviceState(object):
    """一台样板终端的全部状态：两个真实槽 + 一份虚拟配置 + 一个共用修订号。"""

    __slots__ = ("slots", "config", "generation")

    def __init__(self, slots=None, config=None, generation=0):
        # type: (object, object, int) -> None
        revision = Revision()
        self.slots = slots if slots is not None else TemplateSlots(revision=revision)
        self.config = config if config is not None else TemplateConfig(revision=revision)
        self.generation = generation

    @property
    def revision(self):
        # type: () -> int
        return self.slots.revision.value

    def to_dict(self):
        # type: () -> dict
        return {
            "schema_version": TEMPLATE_SCHEMA_VERSION,
            "revision": self.revision,
            "slots": self.slots.to_dict(),
            "config": self.config.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload):
        # type: (dict) -> TemplateDeviceState
        revision = Revision(payload.get("revision", 0))
        slots = TemplateSlots.from_dict(payload.get("slots") or {}, revision)
        config = TemplateConfig.from_dict(payload.get("config") or {}, revision)
        return cls(slots, config)

    def __repr__(self):
        return "TemplateDeviceState(revision=%d, occupied=%r, mode=%s)" % (
            self.revision, self.slots.occupied_slots(), self.config.mode,
        )


# -- 存档 ------------------------------------------------------------------

class TemplateStore(object):
    """按设备实例存样板终端的状态。

    **键是 `(node_key, generation)`，不是坐标**，理由与 `WorkbenchStore`
    逐字相同：同一个坐标上先后两个方块是两份东西，用坐标当键会让「拆掉再放回」
    把旧物品**继承**给新方块。

    ## 与 `WorkbenchStore` 的唯一分歧：拆方块时掉落实物

    User 2026-10-02 裁定：方块被拆时，两个真实槽里的实物**掉落为掉落物**。
    因此 `forget()` 会先调注入的 `drop(node_key, item_dicts)`，**没掉成功的
    留在孤儿索引里**——掉落是主路径，孤儿索引是失败桶，两者都不是「删掉」。
    这条也与事件面有关：`BlockRemoveServerEvent` **不带 `playerId`**，
    所以「还给拆方块的玩家」在那条路径上根本做不到，而掉落到方块位置只需要
    一个维度与一个坐标。
    """

    def __init__(self, accepts=None, drop=None, limit_of=None, policy=None,
                 name_of=None):
        #: `accepts(device_ref) -> bool`。**只对样板终端开口**：没有它就等于
        #: 给每条命令都变出一台空设备，而「这台方块根本没有样板槽」与
        #: 「它的样板槽是空的」在界面上是两回事。为 `None` 时不设限。
        self._accepts = accepts
        #: `drop(node_key, item_dicts) -> int`（返回真的掉出来几件）。
        #: 引擎面，因此是回调而不是服务引用。为 `None` 时**不掉**，
        #: 内容原样进孤儿索引——离线夹具与测试走这一支。
        self._drop = drop
        #: `limit_of(resource_key) -> int`。堆叠上限来自定义表，因此注入。
        self._limit_of = limit_of
        #: 资源显示名由引擎边界提供；离线未注入时使用资源标识。
        self.name_of = name_of
        self.policy = policy if policy is not None else TemplatePolicy()
        self._states = {}
        #: 被拆掉／注销的设备，状态连同**没掉成功的**实物一起留着。
        self._orphans = {}
        self.stats = {
            "created": 0, "forgotten": 0, "restored": 0, "orphaned": 0,
            "dropped_items": 0, "dropped_stacks": 0, "dropped_failed": 0,
        }

    # -- 取 ------------------------------------------------------------

    @staticmethod
    def _key_of(ref):
        node_key = getattr(ref, "node_key", None)
        if node_key is None:
            raise ValueError("样板终端状态需要一个设备实例身份（DeviceRef）")
        return (getattr(node_key, "dimension", 0), getattr(node_key, "x", 0),
                getattr(node_key, "y", 0), getattr(node_key, "z", 0),
                getattr(ref, "generation", 0))

    def of(self, ref):
        # type: (object) -> TemplateDeviceState
        """取这台设备的状态，没有就建一个空的。"""
        key = self._key_of(ref)
        state = self._states.get(key)
        if state is None:
            state = TemplateDeviceState(generation=getattr(ref, "generation", 0))
            self._states[key] = state
            self.stats["created"] += 1
        return state

    def get(self, ref):
        # type: (object) -> object
        return self._states.get(self._key_of(ref))

    def accepts(self, ref):
        # type: (object) -> bool
        if self._accepts is None:
            return True
        try:
            return bool(self._accepts(ref))
        except Exception:
            # 判不出型号时**拒绝**，不是放行：放行会给一个来路不明的位置
            # 凭空造出一台样板终端。
            return False

    def state_for(self, ref):
        # type: (object) -> object
        """命令入口用这个。不接受的设备返回 `None`——**不建、不报错**，
        由调用方转成一条「这台方块没有样板槽」的拒绝。"""
        if not self.accepts(ref):
            return None
        return self.of(ref)

    def limit_of(self, resource_key):
        # type: (object) -> int
        if self._limit_of is None:
            return DEFAULT_STACK_LIMIT
        try:
            limit = int(self._limit_of(resource_key))
        except Exception:
            return DEFAULT_STACK_LIMIT
        return limit if limit >= 1 else DEFAULT_STACK_LIMIT

    def room_for(self, state, index, resource_key):
        # type: (object, int, object) -> int
        """某格还能放几个这种资源。异种占着就是 0（本仓不做混堆）。"""
        entry = state.slots.entry(index)
        limit = self.limit_of(resource_key)
        if entry is None:
            return limit
        if entry[0] != resource_key:
            return 0
        return max(0, limit - entry[1])

    # -- 设备生命周期 --------------------------------------------------

    def observe(self, change):
        # type: (object) -> int
        """从设备生命周期变化推导状态。由启动层的 `_emit` 调用。

        **只关心移除**：这台设备被拆了，两个真实槽里的实物要掉出来。
        新增与挂载变化都不需要建状态——状态是按需建的（`of()`），
        没有界面打开、没有命令进来时建一份空状态只是噪声。
        """
        if getattr(change, "kind", None) != ChangeKind.REMOVED:
            return 0
        return 1 if self.forget(getattr(change, "ref", None)) else 0

    def forget(self, ref):
        # type: (object) -> bool
        """设备注销：先掉落实物，剩下的进**孤儿索引**，一个不删。"""
        if ref is None:
            return False
        key = self._key_of(ref)
        state = self._states.pop(key, None)
        if state is None:
            return False
        if self.policy.break_disposition == BREAK_DROP:
            self._drop_real_slots(key, state)
        self._orphans[key] = state
        self.stats["forgotten"] += 1
        self.stats["orphaned"] = len(self._orphans)
        return True

    def _drop_real_slots(self, key, state):
        # type: (tuple, object) -> None
        """把两个真实槽里的实物掉到方块位置。**掉不掉的留在格子里。**

        判据是 `drop` 回调的返回值，不是「我调过了」——引擎那边写出去了
        但没生成实体（位置被占、维度不存在）是真实存在的一支，把它当成
        成功就是**凭空少物**。

        **传给回调的是 `(维度, x, y, z)` 四元组，不含代次**：掉落落在一个
        位置上，与「这台方块是第几代」无关，而引擎面要的也只有那四个数。
        回调按传入顺序掉落，返回值是**真的生成出实体的那几件的下标前缀**。

        **要掉的是 `(ResourceKey, 数量)`，不是引擎物品字典**：掉落那一侧的
        形状（`CreateEngineItemEntity` 要旧式键名、且 `customTips` /
        `durability` 不接受 `None`）是**引擎边界**的事，构造它的人是
        `adapters/world.py`（走 `item_codec.item_dict_for_drop`）。
        领域层在这里拼字典，就多了一个「拼错了照样能跑」的地方——
        2026-10-02 真机上那条正是这么来的：字典拼错 → 引擎抛异常 →
        回调被接成「一件也没掉」，而东西留在格子里、日志干净。
        """
        if self._drop is None:
            return
        items = []
        indexes = []
        for index in state.slots.occupied_slots():
            entry = state.slots.entry(index)
            items.append((entry[0], entry[1]))
            indexes.append(index)
        if not items:
            return
        try:
            dropped = self._drop(key[:4], items)
        except Exception:
            dropped = 0
        dropped = max(0, min(int(dropped or 0), len(items)))
        for offset in range(dropped):
            index = indexes[offset]
            moved = state.slots.count_at(index)
            state.slots.take_at(index, moved)
            self.stats["dropped_items"] += moved
            self.stats["dropped_stacks"] += 1
        self.stats["dropped_failed"] += len(items) - dropped

    def states(self):
        # type: () -> tuple
        return tuple(self._states.values())

    def orphans(self):
        # type: () -> tuple
        """被拆设备的状态。**没掉成功的实物还在里面**，不是历史记录。"""
        return tuple(self._orphans.values())

    def adopt(self, ref):
        # type: (object) -> object
        """把某个孤儿状态重新挂回活动索引。返回它，没有就返回 `None`。

        留给「将来改口径成『还回去』」那一刻用，本轮没有生产调用点。
        """
        key = self._key_of(ref)
        state = self._orphans.pop(key, None)
        if state is None:
            return None
        self._states[key] = state
        self.stats["orphaned"] = len(self._orphans)
        return state

    # -- 存档 ----------------------------------------------------------

    def save_all(self):
        # type: () -> dict
        """整份存盘载荷。**真机上还没有人调它**（存档接线属任务 12）。"""
        rows = [
            {"node": list(key[:4]), "generation": key[4],
             "detached": detached, "state": state.to_dict()}
            for detached, source in ((False, self._states), (True, self._orphans))
            for key, state in sorted(source.items())
        ]
        return {"schema_version": TEMPLATE_SCHEMA_VERSION, "states": rows}

    def restore(self, payload):
        # type: (dict) -> int
        """从 `save_all()` 的输出恢复。返回恢复的台数。

        版本比当前**新**的原样跳过，不读成空——与 12 的存档纪律一致。
        """
        if payload.get("schema_version", TEMPLATE_SCHEMA_VERSION) > TEMPLATE_SCHEMA_VERSION:
            return 0
        restored = 0
        for row in payload.get("states") or ():
            node = row.get("node") or ()
            if len(node) != 4:
                continue
            key = (node[0], node[1], node[2], node[3], row.get("generation", 0))
            state = TemplateDeviceState.from_dict(row.get("state") or {})
            # 孤儿也一起恢复：它们是**真物品**，丢了就是丢物。
            if row.get("detached"):
                self._orphans[key] = state
            else:
                self._states[key] = state
            restored += 1
        self.stats["restored"] += restored
        self.stats["orphaned"] = len(self._orphans)
        return restored

    def diagnostics(self):
        # type: () -> dict
        report = dict(self.stats)
        report["devices"] = len(self._states)
        report["sources"] = sum(
            1 for state in self._states.values()
            if not state.config.is_empty(GROUP_SOURCE))
        #: 孤儿里压着的物品件数。**这是「没掉成功、还没决定归属」的量**，
        #: 它只涨不落说明有设备的实物一直没被处理。
        report["orphan_items"] = sum(
            state.slots.total_items() for state in self._orphans.values())
        report["policy"] = self.policy.describe()
        report["unresolved"] = list(self.policy.unresolved())
        return report

    def __repr__(self):
        return "TemplateStore(%d 台设备)" % (len(self._states),)


# -- 样板 → 物品 ------------------------------------------------------------

def encode_template_item(definition, item_id=None, name_of=None):
    # type: (object, str, object) -> tuple
    """把一份样板编成一件引擎物品。返回 `(ResourceKey, 引擎物品字典)`。

    **默认的物品标识恒为工厂样板**（`definitions.TEMPLATE_PANEL_ITEM_ID`），
    不按模式反查——模式记在 `userData` 的 pattern 里，不记在物品类型上
    （User 2026-10-02；见那个常量的文档）。样板提供者那 9 格仍然按模式反查
    （`PatternStore.template_item`），两处的差别是有意的，因此 `item_id`
    留成参数、由调用方决定；`PatternStore.template_item` 也调这里，
    免得「一件样板物品长什么样」有两个写法。

    **配方 Lore 随 `userData` 一起进资源键**（`item_codec` 将其算作
    身份）：写进去的和读回来的必须是同一个键，否则「刚放进去就被判成换了
    另一块」。由此得到一个很有用的性质——**同一份配置编两次得到同一个键**，
    产物槽那条「配置相同才可继续制作」的门限就是靠它比出来的，不必去解
    `userData` 再比 digest。
    """
    if item_id is None:
        item_id = TEMPLATE_PANEL_ITEM_ID
    user_data = patterns.write_pattern(None, definition)
    # 配方说明只写 Lore，不覆盖引擎原生物品名称/提示。
    tips = patterns.describe_tip(definition, name_of)
    user_data["display"] = {"Lore": [
        line.encode("utf-8") if bytes is str else line
        for line in tips.split(u"\n")
    ]}
    key = ResourceKey.item(
        item_id,
        0,
        user_data=user_data,
    )
    return key, item_dict_from_key(key, 1)


# -- 配置 → 样板 ------------------------------------------------------------

def pattern_from_config(config, preview, policy=None):
    # type: (object, object, object) -> PatternDefinition
    """虚拟配置 → 一份样板。**配置到样板的映射只此一处。**

    `preview` 是合成模式服务端检测出来的产物（`{name, aux, count}`）或 `None`；
    工厂模式下不看它，产物直接取输出格里玩家配的那几格。

    抛 `PatternValidationError`：空输入、空产物、槽位冲突、合成样板里放了流体
    ——都由 `PatternDefinition` 自己判，这里不重写一套校验。
    """
    policy = policy if policy is not None else TemplatePolicy()
    if config.mode == PATTERN_MODE_CRAFTING:
        return _crafting_pattern(config, preview)
    return _factory_pattern(config, preview, policy)


def _crafting_pattern(config, preview):
    # type: (object, object) -> PatternDefinition
    """合成样板：输入**有序**（九宫格的摆放就是配方），产物是检测出来的那一个。

    上下两格的**界面占位永不编码**（交接第 4 节：「不是配方产物，不编码进
    样板」）；它们本来就不在 `config` 里——那两格是界面按模式画出来的，
    配置里只有真正被配置过的东西。中间那格的产物由 `preview` 给。
    """
    inputs = []
    for index in range(SOURCE_CELL_COUNT):
        entry = config.cell(GROUP_SOURCE, index)
        if entry is None:
            continue
        inputs.append(PatternStack(entry[0], entry[1], slot=index))
    outputs = []
    if preview is not None and preview.get("name"):
        outputs.append(PatternStack(
            ResourceKey.item(preview["name"], preview.get("aux") or 0),
            preview.get("count") or 1,
        ))
    return patterns.PatternDefinition(
        PATTERN_MODE_CRAFTING, tuple(inputs), tuple(outputs))


def _factory_pattern(config, preview, policy):
    # type: (object, object, object) -> PatternDefinition
    """工厂样板：输入无序，产物取输出格按显示顺序（第一格主产物，其余副产物）。

    「主产物／副产物」**只是界面上的显示顺序**，不写进数据：`outputs` 是一个
    扁平列表，`output_keys()` 按首次出现的顺序给索引，再多一个标志就是同一件
    事的第二个真相源。
    """
    inputs = []
    for index in range(SOURCE_CELL_COUNT):
        entry = config.cell(GROUP_SOURCE, index)
        if entry is None:
            continue
        inputs.append(PatternStack(entry[0], entry[1]))
    outputs = []
    occupied = config.occupied(GROUP_OUTPUTS)
    if policy.factory_output_requires_primary and occupied and occupied[0] != 0:
        raise _invalid("工厂样板的第一格是主产物，不能空着而后面有东西")
    for index in occupied:
        entry = config.cell(GROUP_OUTPUTS, index)
        outputs.append(PatternStack(entry[0], entry[1]))
    return patterns.PatternDefinition(
        PATTERN_MODE_FACTORY, tuple(inputs), tuple(outputs))


def _invalid(detail):
    # type: (str) -> PatternValidationError
    return PatternValidationError(patterns.PatternError.MALFORMED, detail)


def detect_crafting_output(cells, recipe_source, policy=None):
    # type: (tuple, object, object) -> object
    """合成模式：按输入查配方，取**引擎给的顺序里的第一条**匹配。

    返回 `{name, aux, count}` 或 `None`（没有匹配）。

    `recipe_source` 是鸭子类型，只需要 `recipes_for_input(key) -> list` 一个
    方法；领域层因此不依赖配方的适配器。`cells` 是 `config.cells(GROUP_SOURCE)`
    的形状（`(名字, aux, 数量)` 或 `None`）。

    **查询用哪一个输入**：按显示顺序取第一个非空格，查不到再顺次试其余占位格。
    只用一格去查是因为引擎的接口就是「按某个原料查会用到它的配方」；一个都
    查不到时返回 `None`，界面上的中间预览格就是空的。

    **不重新排序**（User 2026-10-02 的理由：同样匹配的配方在引擎层会被更高
    优先级的模组盖掉，取引擎给的第一条就是与游戏内一致的那一条）。
    """
    if recipe_source is None:
        return None
    seen = set()
    for cell in cells:
        if cell is None:
            continue
        name = cell[0]
        if name in seen:
            continue
        seen.add(name)
        recipes = recipe_source.recipes_for_input(
            ResourceKey.item(name, cell[1] or 0)) or ()
        for recipe in recipes:
            if match(recipe, cells) is None:
                continue
            item = recipe.output_preview()
            return {
                "name": item.name,
                "aux": item.aux or 0,
                "count": item.count,
            }
    return None
