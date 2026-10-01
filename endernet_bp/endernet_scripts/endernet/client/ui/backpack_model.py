# coding=utf-8
"""下半屏的真实玩家背包：读取、选中、以及「这一点击要存什么」。

取代了原先的本地模拟 `InventoryModel`。两者的差别不是数据来源，而是
**语义**：模拟背包里点击即搬运，而真实背包里点击只是选中——真正动东西
的是接着点的那一下终端行，而且动作发生在服务端（`inv_take`）。本地
**不做任何乐观写入**，屏幕上看到的就是引擎说的那一份。

**两套槽序，别混。** 引擎给的列表是**快捷栏在前**（0–8 快捷栏、9–35 背包），
而界面与 `#collection_index` 是**背包在前**（上方 9×3 是 0–26，下方 9×1 是
27–35）。本模块内部一律用**界面序**（`self._slots`、`selected` 都是），两个
方向的换算只经 `engine_slot_of`——它是这条边界上唯一的一张表。写反的表现是
**背包上下颠倒**，而服务端的锚会照单全收：客户端自己算的槽号与指纹当然互相
一致，离线用例也全绿，只有逐格实点才看得出来。

三条纪律写在这里，因为它们是这个模块存在的理由：

1. **身份走 `engine_item_key`，不走 `item_key`。** 按属性算出来的指纹与
   服务端算的永远不同，带槽请求会全部被判 `SLOT_CHANGED`。详见
   `common/item_codec.engine_item_key` 的说明。
2. **绝不把 key 冻结进选中态。** 选中只记一个**槽号**；发命令之前重读那一格，
   用新鲜读到的物品去发。冻一个 key 下来等于用旧快照代替真身，那正是 06
   文档明禁的「以客户端显示索引代替」的另一种写法。
3. **本地不做任何乐观写入。** 本模块不写引擎背包、不写网络。

## 虚拟背包空间（2026-09-30 起）

**数据流是「真实背包 → 虚拟空间 → UI」**：`_slots` 是那个虚拟空间，界面
只读它；引擎读数往它里灌，本地操作在它上面做。

**为什么必须有它**：背包内的交换/合并，玩家点完要立刻看出结果，而**客户端
根本写不了自己的背包**——这台引擎的背包写接口全在服务端
（`SetInvItemExchange` / `SetInvItemNum` / `SpawnItemToPlayerInv` 都是
`mod.server.*`），客户端只有 `Get*`。于是本地只能维护一份**自己的**背包。
有了它，玩家在等服务端回执的那段时间里可以做**任意多轮**操作。

**它与真值的关系由 `_pending` 说清**：每一次本地操作都记一条，直到**真值
兑现了它**才退场（`_retire`）。因此：

- `_base` = 上一次引擎读数（真值）
- `_pending` = 还没被真值兑现的本地操作，**按发生顺序**
- `_slots` = `_base` 上按序重放 `_pending`（`_replay`）——**这就是 UI 看到的**

**退场判据是「真值追上了没有」，不是「回执到了没有」**：回执到达只是去重读
一次真实背包的信号，操作本身要等 `_base` 里那两格真的变成它的结果才退。差别
在时序上看得见——回执往往比引擎读数先到，**按回执退会让画面先跳回操作前的
样子、再跳过来**。真值走到别处去了（两格既不是操作前、也不是操作后）就
**丢弃该操作**，硬撑着会盖掉别人的改动。

**取放（网络 ↔ 背包）不进虚拟空间**：那两格以外还有网络库存，本地看不到
结果，预测不了——那两条路照旧「发命令 → 等回执 → 重读」。
"""

from ...common.item_codec import engine_item_key
from . import stack_size

#: 界面槽号的两个区段。与 `common_terminal_panel.json` 的两个 collection
#: 一一对应：上方 `endernet_terminal_backpack` 是 9×3，下方
#: `endernet_terminal_hotbar` 是 9×1。`#collection_index` 在**各自集合内**
#: 从 0 起，所以这两段是界面自己的编号，不是引擎槽号。
BACKPACK_COUNT = 27
HOTBAR_START = BACKPACK_COUNT
HOTBAR_COUNT = 9
SLOT_COUNT = BACKPACK_COUNT + HOTBAR_COUNT

#: 背包内部两个动作的名字。**与 `StorageService.handle` 认识的 action 字符串
#: 逐字相同**——这里不另立一套词汇再映射一次：映射写错了的表现是命令被服务端
#: 回一句「不认识」，而那只有真机上看得见。
#:
#: 交换是 2026-09-30 补的第五个动作（见 `plans/blocks.md` 的「背包槽之间的
#: 拆分、合并与交换」）：两者都**只在背包内部发生，不经过网络**。
ACTION_MERGE = "inv_merge"
ACTION_SWAP = "inv_swap"

try:
    INTEGER_TYPES = (int, long)
except NameError:
    INTEGER_TYPES = (int,)


def engine_slot_of(ui_index):
    # type: (int) -> int
    """界面槽号 → 引擎槽号。**这个映射只有这一处。**

    引擎的顺序是**快捷栏在前**：0–8 是快捷栏，9–35 是背包（27 格）。界面
    的顺序相反，上方的 9×3 是背包、下方的 9×1 是快捷栏。于是：

    ```
    UI  0–26（上方 9×3）→ 引擎 9–35
    UI 27–35（下方 9×1）→ 引擎  0–8
    ```

    为什么必须只有一个定义：客户端用**同一张表**同时算出 `slot` 和 `key`，
    而服务端的锚只回答「这个槽里是不是这件东西」——自己和自己必然一致。
    映射错的表现是**搬走玩家没点的那一格**，服务端指纹核对通过、离线用例
    也全绿，什么都不会报。游戏内逐格实测是唯一的判别手段。
    """
    if isinstance(ui_index, bool) or not isinstance(ui_index, INTEGER_TYPES):
        raise ValueError("界面槽号必须是整数，得到 %r" % (ui_index,))
    if not 0 <= ui_index < SLOT_COUNT:
        raise ValueError("界面槽号越界：%r" % (ui_index,))
    if ui_index < BACKPACK_COUNT:
        return ui_index + HOTBAR_COUNT
    return ui_index - BACKPACK_COUNT


class _PredictedStack(object):
    """预测出来的那一堆：**只有一个 `count`**。

    `BackpackEntry.item` 唯一被用到的地方就是 `.count()`——渲染走的是
    `entry.key`（`#item_id_aux` 按资源名查，数量另外绑定）。所以预测不必
    伪造一个完整的引擎物品字典。

    **不要去改引擎读回来的那个对象。** 它是引擎给的，同一帧里可能还有别人
    在读（`replace_from_engine` 的 `previous` 与 `_slots` 就指着它），改了会
    让「有没有变化」的比较凭空出错。
    """

    __slots__ = ("count",)

    def __init__(self, count):
        # type: (int) -> None
        self.count = count


class _PendingOp(object):
    """一次**已经作用在虚拟空间上、还没被真值兑现**的本地操作。

    除了「改哪两格、怎么改」，它还记下**动手前后那两格的样子**——退场判据
    全靠这一对：

    - 真值变成了 `after` → **兑现了**，退场；
    - 真值还是 `before` → 还没轮到它，继续预测；
    - 两个都不是 → 真值已经走到别处去了，**丢弃**（硬撑着会盖掉别人的改动）。

    `before` / `after` 每次重放时重算（`transform`）：它们相对的是「排在它
    前面那些操作都做完之后」的状态，而那个状态会随真值推进而变化。
    """

    __slots__ = ("request_id", "action", "source", "target", "count",
                 "before", "after")

    def __init__(self, request_id, action, source, target, count):
        # type: (str, str, int, int, object) -> None
        self.request_id = request_id
        self.action = action
        self.source = int(source)
        self.target = int(target)
        self.count = count
        self.before = None
        self.after = None

    def transform(self, before):
        # type: (tuple) -> object
        """把这一条作用在 `before` 那两格上，返回 `(源, 目标)` 的新样子。

        **算不出来返回 `None`**（源槽空了、或合并的目标不再是同类）——
        调用方据此丢弃它：真值已经走到它作用不了的地方了。
        """
        source, target = before
        if source is None:
            return None
        if self.action == ACTION_SWAP:
            return (target, source)
        if self.action == ACTION_MERGE:
            if target is None or target.key != source.key:
                return None
            moved = self.count
            if moved is None or moved <= 0 or moved > source.count():
                return None
            left = source.count() - moved
            return (
                None if left <= 0
                else BackpackEntry(_PredictedStack(left), source.key),
                BackpackEntry(_PredictedStack(target.count() + moved), target.key),
            )
        return None


class BackpackEntry(object):
    """一个槽里的东西：`item` 供渲染，`key` 供身份。

    两者分开是刻意的——渲染要的是 `SetUiItem` 能吃的对象，身份要的是
    **引擎原始字典**归一化之后的结果，而后者不能从前者推出来。

    **两个字段都不为 `None`**：`key` 是 `None` 意味着那是个空格子，而空格
    在模型里由「这一槽就是 `None`」表达，不造 `BackpackEntry`。
    """

    __slots__ = ("item", "key")

    def __init__(self, item, key):
        # type: (object, object) -> None
        self.item = item
        self.key = key

    def count(self):
        # type: () -> int
        count = getattr(self.item, "count", 0)
        if isinstance(count, bool) or not isinstance(count, INTEGER_TYPES):
            return 0
        return int(count)

    def __repr__(self):
        return "BackpackEntry(%s x%d)" % (self.key.name, self.count())


def _read_engine_items():
    # type: () -> list
    """读本机玩家的 36 个引擎槽，引擎槽序：0–8 快捷栏、9–35 背包。

    **引擎导入放在函数体内**：本模块因此仍可离线导入，测试直接替换这个
    函数即可（`patch.object(backpack_model, "_read_engine_items", ...)`），
    不必去动 `sys.modules`。这与适配器那条「引擎调用不进模块顶层」是
    同一条纪律。

    `get_user_data=True` **不是可选项**：默认的 `False` 会把 NBT 丢掉，
    于是带 userData 的物品（潜影盒、旗帜）身份与网络里的那一件不同，
    每存一次都被判 `SLOT_CHANGED`。
    """
    from ....tooldelta.api.client.player import GetLocalPlayerHotbarAndInvItems
    return GetLocalPlayerHotbarAndInvItems(get_user_data=True)


class BackpackModel(object):
    """36 个界面槽的**虚拟背包空间** + 一个选中槽号。

    界面只读 `_slots`（虚拟空间）；`_base` 是上一次引擎读数（真值），
    `_pending` 是还没被真值兑现的本地操作。三者关系见模块头。

    它仍然**不写引擎背包、不写网络**——本地操作只作用在 `_slots` 上，
    真正动东西的永远是服务端。
    """

    def __init__(self):
        #: 虚拟空间。**界面读的就是它**，`_replay()` 是它唯一的写入口。
        self._slots = [None] * SLOT_COUNT
        #: 上一次引擎读数，即真值。
        self._base = [None] * SLOT_COUNT
        #: 还没被真值兑现的本地操作，**按发生顺序**（`_PendingOp`）。
        self._pending = []
        #: 选中的槽号。**只由 `_set_selected` 改**——它同时记修订号。
        self.selected = None
        #: `selected` 的修订号：**每一次真的改变选中都 +1**。面板在发命令时
        #: 把它连同 `request_id` 一起记下，回执回来时比一下：**修订号没变才清
        #: 选中**。
        #:
        #: 为什么不能拿槽号去比：玩家在在途窗口里「取消选中 → 又选中同一格」
        #: 之后，槽号与发命令时**一模一样**，而那是两次不同的意图。比槽号会
        #: 把这次新选中当成旧的清掉，之后点终端行就落到 `inv_put`——
        #: **方向与玩家的意图相反**。
        self._selection_revision = 0
        self.errors = 0

    def _set_selected(self, value):
        # type: (object) -> None
        """改选中并记修订号。**值没变就不记**——否则一次无谓的
        `clear_selection()`（比如关界面那一下）会把别人的回执判成「玩家动过
        选中」，该清的就不清了。"""
        if self.selected == value:
            return
        self.selected = value
        self._selection_revision += 1

    def selection_revision(self):
        # type: () -> int
        """选中态的修订号。见 `_selection_revision` 的说明。"""
        return self._selection_revision

    # -- 读取 ----------------------------------------------------------

    @staticmethod
    def _checked(ui_index):
        # type: (object) -> bool
        """槽号合法吗。越界与非法类型一律不合法——**不能被当成第 0 格**，
        也不能被 `self._slots[-1]` 悄悄读成最后一格。"""
        if isinstance(ui_index, bool) or not isinstance(ui_index, INTEGER_TYPES):
            return False
        return 0 <= ui_index < SLOT_COUNT

    def entry(self, ui_index):
        # type: (int) -> object
        """这一格现在是什么——**虚拟空间**，也就是界面上画的那一份。

        渲染与点击判定都走它，因此玩家看到的与点到的永远是同一个东西。
        """
        if not self._checked(ui_index):
            return None
        return self._slots[ui_index]

    def truth(self, ui_index):
        # type: (int) -> object
        """这一格的**引擎真值**，不含任何本地操作。

        给诊断与对比用——**渲染不要走它**：那会让玩家看到的与点到的不是
        同一个东西，而这类不一致只在真机上以「点了没反应/换错格」的形式
        露头。
        """
        if not self._checked(ui_index):
            return None
        return self._base[ui_index]

    def replace_from_engine(self):
        # type: () -> list
        """重读引擎背包，返回**内容有变化**的界面槽号。

        两条失败路径都**整块保持上一帧**（返回空表，只计数）：

        - 读本身炸了或没给出 36 格。条数不对尤其不能当成「后面那些格子空了」
          ——那会把玩家的背包显示成空的，而引擎只是这一次没给全。
        - 单格的身份归一化抛错。那是有意的严格性（「不静默丢弃潜在身份数据」
          的守卫），但那个守卫该炸的是日志，不是玩家的背包面板，所以**只跳过
          那一格**、保留它上一帧的样子。

        「宁可显示上一次读到的，也不要闪成空的」是这里的取舍：背包面板闪一下
        只是难看，但**把它显示成空的**会让玩家以为东西没了。
        """
        previous = self._slots
        try:
            read = _read_engine_items()
            if read is None or len(read) != SLOT_COUNT:
                raise ValueError("引擎背包不是 %d 格" % (SLOT_COUNT,))
        except Exception:
            self.errors += 1
            return []
        base = [None] * SLOT_COUNT
        for ui_index in range(SLOT_COUNT):
            # **这里必须换算**：`read` 是引擎给的，顺序是快捷栏在前；而
            # `#collection_index` 一样是界面序。少这一步——或者写反——的表现
            # 是**整块背包上下颠倒**，而服务端的锚会照单全收（客户端自己算的
            # slot 与 key 当然对得上）。
            raw = read[engine_slot_of(ui_index)]
            if raw is None:
                entry = None
            else:
                try:
                    key = engine_item_key(raw)
                except Exception:
                    self.errors += 1
                    entry = self._base[ui_index]
                else:
                    # `item_key_from_dict` 返回 `None` 就是**这一格是空的**
                    # （空气、或 count≤0）。留着这个 entry 下去，`key` 为
                    # `None` 会变成一颗地雷：`pending_take` 里那句
                    # `entry.key.name` 谁也看不出它会炸。
                    entry = None if key is None else BackpackEntry(raw, key)
            base[ui_index] = entry
        # **写的是 `_base`，不是 `_slots`。** 引擎读数只更新真值；画面上那一份
        # 要由 `_replay` 在真值之上重放还没兑现的本地操作才算得出来——直接
        # 写 `_slots` 会把玩家刚做的多轮操作一笔抹掉。
        self._base = base
        self._retire()
        self._replay()
        changed = [index for index in range(SLOT_COUNT)
                   if not _same_entry(previous[index], self._slots[index])]
        self._drop_stale_selection(previous)
        return changed

    def snapshot(self):
        # type: () -> list
        """诊断用：每槽一个 `(资源名, 数量)` 或 `None`。

        **报的是画面上那一份**（虚拟空间）——`GetBackpackItems()` 回答的是
        「界面上现在显示什么」。想知道哪几格与真值不一致，看
        `diagnostics()["pending"]`；真值本身走 `truth()`。
        """
        out = []
        for index in range(SLOT_COUNT):
            entry = self.entry(index)
            out.append(None if entry is None else (entry.key.name, entry.count()))
        return out

    # -- 选中 ----------------------------------------------------------

    def select(self, ui_index):
        # type: (int) -> object
        """选中一个槽，返回**处理之后的选中下标**（`None` = 现在没有选中）。

        **空格子点不动**：返回原样（也就是原来那个选中，或者 `None`）。玩家
        点的是一种没东西的格子，不是要撤销上一次选择——旧实现里这一下会把
        东西搬过去，那个语义随模拟背包一起没了。同格再点 = 取消。

        界面**不要**拿这个返回值去画高亮：选中态有两个来源（本模块的点击、
        列表视图在回执回来后清掉的），各画各的迟早有一边漏画。实际的做法是
        `selected` 挂在面板的集合绑定上、由引擎每渲染帧自己来取一次
        （`TerminalPanelBase._slot_selected`），谁也无需通知谁。
        """
        if self.entry(ui_index) is None:
            return self.selected
        if self.selected == ui_index:
            self._set_selected(None)
        else:
            self._set_selected(ui_index)
        return self.selected

    def clear_selection(self):
        # type: () -> None
        """清掉选中。选中态**不持有物品**，所以丢弃它不欠任何东西。"""
        self._set_selected(None)

    def _drop_stale_selection(self, previous):
        # type: (list) -> None
        """选中的那一格**换了东西**或空了就把选中丢掉。

        判据是「那一格的 key 变了」而**不是**「背包有任何变化」：别处抖动一下
        就吞掉玩家的选中，玩家会觉得界面在自己乱动。反过来说，选中的那格真的
        被换掉了还留着选中，下一次点击就会拿一件玩家没选过的物品去交接——所以
        这一条不能省。
        """
        if self.selected is None:
            return
        before = previous[self.selected]
        after = self._slots[self.selected]
        if after is None or before is None or before.key != after.key:
            self.clear_selection()

    # -- 虚拟空间（背包内交换/合并） -----------------------------------

    def apply_exchange(self, request_id, action, source_index, target_index, count):
        # type: (str, str, int, int, object) -> bool
        """把一次**已经发出去**的背包内操作作用到虚拟空间上。

        调用方必须**只在命令真的发出去之后**调：没发出去的点击不该改变
        画面（那条路上每一处 `return` 都是「等待同步」的意思）。

        **多轮是支持的**，不设「已有操作在途就拒绝」那种栅栏——玩家在等
        服务端回执的那段时间里可以做任意多轮。叠加的正确性由 `_retire` /
        `_replay` 一起保证：真值沿队列**从头**兑现，没兑现的一直重放着。
        """
        if action not in (ACTION_MERGE, ACTION_SWAP):
            # 取放（网络 ↔ 背包）不进虚拟空间：那两格以外还有网络库存，
            # 本地看不到结果。**认不出来就不装**，而不是猜一个。
            return False
        self._pending.append(
            _PendingOp(request_id, action, source_index, target_index, count)
        )
        self._replay()
        return True

    def drop_pending(self, request_id):
        # type: (str) -> bool
        """撤掉某一条还没兑现的操作（回执说它失败了）。返回是否撤到。"""
        before = len(self._pending)
        self._pending = [op for op in self._pending
                         if op.request_id != request_id]
        self._replay()
        return len(self._pending) != before

    def clear_pending(self):
        # type: () -> None
        """丢掉全部本地操作，画面立刻回到真值。

        **会话丢掉、界面关掉时都要调。** 这些操作是「服务端答应过但还没
        兑现」的，会话都没了，那份答应已经作废——留着它，界面会一直显示
        一个不存在的交换结果。
        """
        self._pending = []
        self._replay()

    def has_pending(self):
        # type: () -> bool
        return bool(self._pending)

    def pending_slots(self):
        # type: () -> list
        """还没被真值兑现的界面槽号（排序，诊断与版本判定用）。

        **面板据此决定 `slot_version` 发什么**：这几格在服务端那边的版本
        客户端算不出来（服务端什么时候读到、读了几次，取决于它自己的时序，
        而且交换与合并不一样）。因此这几格发 `None`（含义是「不校验版本」，
        是 `SlotAnchor` 有意留的选项）——**指纹那一关照旧**，而它才是
        「不会误取替换后的物品」的落点。
        """
        slots = set()
        for op in self._pending:
            slots.add(op.source)
            slots.add(op.target)
        return sorted(slots)

    def _retire(self):
        # type: () -> None
        """从队首起，退掉真值已经兑现（或已经证伪）的操作。

        **按序、只从队首**：服务端也是按序执行这些命令的，因此真值只会沿着
        队列从头兑现。中间某一环还没兑现，后面就算「看起来对上了」也不退
        ——那多半是巧合（换出去又换回来）。

        **退场看的是真值，不是回执。** 回执到达只是去重读一次的**信号**；
        操作本身要等 `_base` 里那两格真的变成它的结果才退。差别在时序上
        看得见：回执往往比引擎读数先到，**按回执退会让画面先跳回操作前的
        样子、再跳过来**。
        """
        while self._pending:
            op = self._pending[0]
            if op.after is None:
                return
            if (_same_entry(self._base[op.source], op.after[0])
                    and _same_entry(self._base[op.target], op.after[1])):
                self._pending.pop(0)        # 兑现了
                continue
            if (_same_entry(self._base[op.source], op.before[0])
                    and _same_entry(self._base[op.target], op.before[1])):
                return                      # 还没轮到它，继续预测
            # 既不是操作前、也不是操作后：真值走到别处去了。**丢弃**——
            # 硬撑着预测会把别人的改动盖掉，那比不显示严重得多。
            self._pending.pop(0)

    def _replay(self):
        # type: () -> None
        """重建成「真值 + 还没兑现的本地操作」。**`_slots` 的唯一写入口。**"""
        slots = list(self._base)
        kept = []
        for op in self._pending:
            before = (slots[op.source], slots[op.target])
            after = op.transform(before)
            if after is None:
                # 真值已经走到它作用不了的地方了（源槽空了、合并的目标
                # 不再是同类）。丢弃，而不是硬套。
                continue
            op.before, op.after = before, after
            slots[op.source], slots[op.target] = after
            kept.append(op)
        self._pending = kept
        self._slots = slots

    # -- 背包内部的动作 ------------------------------------------------

    def intends_exchange(self, ui_index):
        # type: (int) -> bool
        """这一点击是不是「在背包内部动一下」，而不是改选中。

        判据只有一条：**已经有选中，且点的是另一个槽**。空格子也算——与空槽
        交换就是移动（`plans/blocks.md`）。同格再点是**取消**，不是交换。

        **界面必须先问这个，再决定要不要调 `select()`。** 两条路的语义不同：
        `select()` 只改选中、什么也不搬；这里要发一条命令。反过来调用——先
        `select()` 再判断——会把选中挪到目标格上，然后拿它当源槽用：那正是
        「搬走玩家没点的那一格」的另一种写法。
        """
        if self.selected is None:
            return False
        if isinstance(ui_index, bool) or not isinstance(ui_index, INTEGER_TYPES):
            return False
        if not 0 <= ui_index < SLOT_COUNT:
            return False
        return ui_index != self.selected

    def pending_exchange(self, ui_index):
        # type: (int) -> object
        """选中槽 → `ui_index` 这一下该在背包内部做什么。

        返回 `(动作名, 源界面槽, 数量)`，动作名是 `ACTION_MERGE` /
        `ACTION_SWAP`。**数量只在合并时有意义**（交换传 `None`，服务端不读
        它）。返回 `None` 表示**这一下不该发命令**：

        - 没有选中、或点的是同一格——那两下归 `select()`，不归这里；
        - 选中的那一格已经空了（`replace_from_engine` 会顺手把选中清掉）；
        - **同类而且目标已经满了**：搬不动。发出去只会换一张 `NO_CAPACITY`
          回执，而玩家什么也没做错。

        判定读的是**重读之后的**两格，不是选中那一刻的快照——理由与
        `pending_take` 相同：冻一份旧数据下来等于用旧快照代替真身。

        **合并的数量在这里就封好**（`min(源数量, 目标剩余空间)`），不是把源
        整槽发出去让服务端去截。差别落在回执上：发整槽而目标只装得下一半时，
        服务端会报 `PARTIAL`（它先取后放、放不进的退回原槽），界面于是每合并
        一次就报一次「部分成功」——而玩家看到的是东西都进对了。**这是本轮定
        的默认，不是玩法定案**：R12 定稿后若加上「按住取半」，改的是这一处。
        """
        # **判据只此一处。** `intends_exchange` 同时管住了「有没有选中」「点的
        # 是不是另一格」和下标合法性，这里复用它而不是重写一遍：两处各写一份
        # 的话，越界下标会在一边被拦下、在另一边被当成空格子——而
        # `self._slots[-1]` 在 Python 里**不报错**，它取的是最后一格。
        if not self.intends_exchange(ui_index):
            return None
        self.replace_from_engine()
        source_index = self.selected
        if source_index is None:
            return None
        source = self.entry(source_index)
        target = self.entry(ui_index)
        if source is None:
            return None
        count = source.count()
        if count <= 0:
            return None
        if target is not None and target.key == source.key:
            room = stack_size.stack_limit(source.key.name) - target.count()
            if room <= 0:
                return None
            if room < count:
                count = room
            return (ACTION_MERGE, source_index, count)
        # 异种、或目标为空：都是**交换**。与空槽交换就是移动，因此移动不需要
        # 单独一个动作——那是交换的一个特例，不是第六个动作。
        return (ACTION_SWAP, source_index, None)

    # -- 这一点击要做什么 ----------------------------------------------

    def pending_take(self, whole=False, half=False):
        # type: (bool, bool) -> tuple
        """选中的槽 → `(引擎槽号, key, 数量)`；没有可存的返回 `None`。

        数量有三种口径：

        - 默认：**一组** `min(该槽数量, maxStackSize)`，与上半屏终端行的
          「取一组」互为镜像；
        - `whole=True`：**整槽**、不截断——「点列表空白处整槽存进网络」那条
          路（`inv_quick_move`）要的就是它；
        - `half=True`：**一半** `该槽数量 // 2`——长按背包槽那条路
          （`inv_split`，2026-10-01 User 定的手势）。

        三种共用这一处，是因为**发命令前的那几道核对完全相同**：重读那一格、
        确认它还在、确认它不是空的。分成两三份的话，迟早有一份忘了重读，而
        表现是「发了一条注定被拒的命令」——那要真机上才看得出来。

        **`whole` 与默认这两个口径仍是本轮定的默认、不是已定案的玩法**
        （R12 的手势映射定稿后若改成右键取一个，改的是这一处）；`half` 那
        一条的手势已经定了（长按），但**长按本身在真机上还没验过**。

        这里**重读引擎**再给结果，用的不是选中那一刻的快照。选中之后物品
        被别的途径拿走了，在这一步就会发现（返回 `None`、清掉选中），而不是
        发一条注定被拒的命令出去换一张 `SLOT_CHANGED` 回执。

        **`half` 在一格的堆叠是 1 时给不出正整数**：`1 // 2 == 0`，于是走
        下面 `count <= 0` 那一支返回 `None`——「拆一半」在一件东西上没有
        意义，调用方据此什么都不发，而不是发一条 `count=0` 去换越界拒绝。
        """
        if self.selected is None:
            return None
        self.replace_from_engine()
        ui_index = self.selected
        if ui_index is None:
            return None
        entry = self._slots[ui_index]
        if entry is None:
            return None
        count = entry.count()
        if half:
            count = count // 2
        elif not whole:
            limit = stack_size.stack_limit(entry.key.name)
            if count > limit:
                count = limit
        if count <= 0:
            return None
        return (engine_slot_of(ui_index), entry.key, count)

    def diagnostics(self):
        # type: () -> dict
        return {
            "selected": self.selected,
            # 按**画面**数，与 `snapshot()` 同一份；`predicted` 非空时它可能
            # 与引擎真值不同。
            "filled": sum(1 for index in range(SLOT_COUNT)
                          if self.entry(index) is not None),
            "errors": self.errors,
            #: 还没被真值兑现的界面槽号。**非空表示画面上有格子不是引擎说的
            #: 那一份**——它们正等着服务端追上。
            "pending": self.pending_slots(),
        }


def _same_entry(before, after):
    # type: (object, object) -> bool
    """两格是不是同一件东西。**比较身份与数量，不比较对象**——每次读都会
    造一批新的 `Item`，按对象比会让「有没有变化」恒为真、每帧全量重画。"""
    if before is None or after is None:
        return before is None and after is None
    if before.key != after.key:
        return False
    return before.count() == after.count()
