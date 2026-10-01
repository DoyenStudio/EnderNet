# coding=utf-8
"""通用终端长列表的客户端模型（任务 11-09）。

对应 `plans/technical_notes/terminal_item_index.md` 第 7 节（11-03、11-09）
与 11.md 的「建立 entries 与 ordered_keys，缓存搜索文本；名称排序下纯数量
变化不重排，结构变化/数量排序按批合并，查询代次隔离旧结果」。11-10 的虚拟
槽位（同文件第 8 节）是它的消费者，因此这里**不持有任何控件引用**。

关于代价：普通列表插入会移动元素，这里**不宣称**任何排序模式下单次更新都
是 O(1)。首版按结构变化批次重建/排序，测量后再决定是否引入更复杂的结构
（技术说明第 7 节末段）。

**引擎无关**，与 `inventory_model.py` 同一地位：不 import `tooldelta`、
不 import `server/`，因此可以直接 import 进测试跑，不需要假引擎。它的输入
只有一种——已经解码好的 `(资源键, 已存, 已预留)`，也就是线上 `item_delta`
的 `upserts` 与 `snapshot_chunk` 的 `items`。

三条不变量，每一条都对应一类实机上查不出来的症状：

1. **纯数量变化在名称排序下不重排。** 一条增量把某物品从 3 改成 4，行不该
   跳位置；跳了的表现是「手里的东西突然换了一行」，而玩家点下去取到的是
   另一件——这正是 11.md 里「快速滚动、排序和收到增量后点击，不会误取旧
   控件」要挡的。数量排序下**重排推迟到批次结束**（一批至多重排一次）：
   边收包边重排会让整表在几十毫秒里反复洗牌。
2. **旧查询不覆盖新查询。** `query_page` 只认代次，代次不符**抛
   `StaleQuery`**。模型**不提供**「那就返回当前结果」的便利口——只要存在，
   调用方迟早绕开代次，而绕开之后「筛选刚改完，旧结果盖回来」这件事就没有
   任何一处代码能发现。
3. **点击身份恒为资源键。** 公开接口只收发 `ResourceKey`，**没有任何以显示
   下标为身份的口**。下标是这一帧的布局，收包与筛选都会让它变；拿它当身份
   就是「点了这一格，取到了那一格」。

与 `server/sync/` 的关系是**同一条协议的两端**：服务端保证「一个发布窗口
= 一次版本推进」，这里靠 `begin_batch(base, new)` 的基准版本对上号；对不上
即断档，抛 `ListResyncRequired` 让上层重取快照——**不猜、不补**。版本语义见
11.md 第 119 行：客户端**不能**拿 UI 发布版本当交易的预期版本。
"""

from ...common.protocol import require_count
from ...common.resource_key import ResourceKey

#: 排序字段。三个都是**确定性**的：同名物品（附加值不同、附魔不同）靠资源
#: 键指纹兜底，因此同样的输入永远产出同样的顺序。
SORT_NAME = "name"
SORT_COUNT = "count"
SORT_KIND = "kind"
SORT_FIELDS = (SORT_NAME, SORT_COUNT, SORT_KIND)

try:  # pragma: no cover - 仅在 Python 2 下成立
    _UNICODE = unicode
except NameError:  # pragma: no cover - 仅在 Python 3 下成立
    _UNICODE = str


def _text(value):
    # type: (object) -> object
    """统一成 unicode。py2 下 `str` 是字节串，`u"%s" % <utf-8 字节>` 会按
    ASCII 解码而抛 `UnicodeDecodeError`——而显示名正是最可能出现非 ASCII 的
    地方（本地化名、玩家自定义名）。与 `resource_key._create` 同一处理。
    """
    if isinstance(value, _UNICODE):
        return value
    return value.decode("utf-8")


def _fingerprint(key):
    # type: (ResourceKey) -> object
    """排序与去重的兜底判据。见 `ItemListModel._sort_key`。"""
    return key.canonical()


class ListResyncRequired(Exception):
    """这一批增量的基准版本与本地对不上：**必须重取快照**。

    「没收到的那一条」与「版本对不上」是两件事：前者等一等就好，后者等多久
    都不会好——中间缺的那一批里若有删除，那是永远补不回来的，列表会一直
    显示一件早就没了的东西。因此这里不做任何补救（不缓存、不排队），
    直接抛给上层。
    """


class StaleQuery(Exception):
    """拿着旧代次来取结果：那次查询的条件（筛选、排序）早已变了。

    **它是异常而不是一个空结果**：空结果与「这次筛选真的没有匹配项」长得
    一模一样，而两者的处理完全相反——前者要重查，后者要显示「没有找到」。
    """


class ItemEntry(object):
    """一行。**身份是 `key`**，其余都是它的投影或当前数量。

    `name` / `kind` 在插入时定下：资源键一旦确定，这两项不会再变（`upsert`
    只改数量）。因此重排的判据里它们可以当稳定值用——「纯数量变化不重排」
    这条不变量就建立在这一点上。
    """

    __slots__ = (
        "key",
        "name",
        "kind",
        "stored",
        "reserved",
        "sort_name_key",
        "search_text",
    )

    def __init__(self, key, name):
        # type: (ResourceKey, object) -> None
        self.key = key
        self.name = name
        self.kind = key.kind
        self.stored = 0
        self.reserved = 0
        #: 排序用的名称。**小写缓存一次**：每次比较都调 `lower()` 会把
        #: O(n log n) 次比较变成 O(n log n) 次字符串分配（长列表上这是主要
        #: 开销之一）。py2 的 `lower()` 对非 ASCII 是逐字节的，但两侧都过
        #: 同一个函数，顺序仍然稳定。
        self.sort_name_key = name.lower()
        #: 搜索用的文本。**连资源名一起收**：显示名是本地化的，而玩家想搜的
        #: 常常是 `stone` 这类资源名——只收显示名的话，中文界面里搜英文名
        #: 一条都搜不到，而那种「搜了没反应」没人会想到是搜索文本的问题。
        if name == key.name:
            self.search_text = name.lower()
        else:
            self.search_text = ("%s %s" % (name, key.name)).lower()

    def __repr__(self):
        # type: () -> str
        text = u"ItemEntry(%s, stored=%d, reserved=%d)" % (
            self.key.canonical(),
            self.stored,
            self.reserved,
        )
        return text if _UNICODE is str else text.encode("utf-8")


class ApplyResult(object):
    """一个批次（或一次快照替换）对**界面**的影响。

    分成「重排了没有」与「哪些行变了」两件事：前者决定要不要重排控件，后者
    决定要重画哪几行。合成一个布尔的话，收到一条新物品就得整表重画——而长
    列表的成本几乎全在重画上（11-10 的控件池就建在这个区分上）。
    """

    __slots__ = (
        "reordered",
        "changed_keys",
        "removed_keys",
        "anchor_key",
        "anchor_offset",
        "selection_cleared",
        "version",
    )

    def __init__(self, reordered, changed_keys, removed_keys, anchor_key,
                 anchor_offset, selection_cleared, version):
        # type: (bool, tuple, tuple, object, int, bool, int) -> None
        #: 可见顺序**真的**变了——逐项比对得出的，不是「算过一遍」。
        self.reordered = reordered
        #: 本批动过的键（新增 / 改量 / 删除），按资源键指纹排序因而可复现。
        #: **含当前不可见的那些**：模型不知道视口在哪（那是 11-10 的事），
        #: 调用方自己与它正在显示的那几行求交集。
        self.changed_keys = changed_keys
        #: 其中已经消失的（渲染时要腾出这些行）。
        self.removed_keys = removed_keys
        #: 重排后锚点落在哪一行。没设过锚点时是当前首行（列表空则为 None）。
        #: 调用方按住它滚动，就能让玩家盯着的那一行留在原处。
        self.anchor_key = anchor_key
        self.anchor_offset = anchor_offset
        #: 选中的那一行没了，选择已被清掉。**要说出来**：不说的话界面会继续
        #: 画一个早已不存在的选中态，而玩家接着点下去什么也不会发生。
        self.selection_cleared = selection_cleared
        self.version = version


class ItemListModel(object):
    """长列表的 entries、可见顺序、筛选排序、查询代次与批次合并。"""

    def __init__(self, name_resolver=None):
        # type: (object) -> None
        #: 显示名解析器：`key -> 文本`。不给就用资源名（`key.name`）。
        #:
        #: **默认用资源名而不是 `canonical()`**：后者是给存档与去重用的指纹
        #: （`item|minecraft:stone|0|...`），显示出来对玩家毫无意义。资源名
        #: 不唯一（附加值不同的同种物品同名），因此**排序的兜底一定得是资源
        #: 键指纹**，见 `_sort_key`。
        self._name_of = name_resolver
        self._entries = {}
        #: 当前可见顺序（资源键列表）。`None` = 还没建过，与「建出来是空的」
        #: 是两件事：前者要立刻建，后者是「真的没有匹配项」。
        self._order = None
        #: 顺序需要重算。批次进行中时**只置位不重算**（见 `_ensure_order`）。
        self._order_dirty = False
        self._sort = SORT_NAME
        #: 默认**降序**（`plans/ui/common_terminal_panel.md` 第 79 行、
        #: `plans/technical_notes/terminal_item_index.md` 第 120 行）。名字大的
        #: 在前，玩家一进来看到的不是一堆 `a` 开头的东西。方向是模型的初始
        #: 状态，与 `set_sort` 的形参默认值各管各的——后者只在调用方**显式
        #: 省略**方向时生效，界面永远两个都传。
        self._descending = True
        self._filter = ""
        self._generation = 0
        self._version = 0
        self._batch = None
        self._batch_serial = 0
        self._selected = None
        self._anchor_key = None
        #: 锚点**当时**所在的行号。它在锚点还活着时只是个备忘，一旦那一行
        #: 消失（删掉、被筛掉、快照里没了）就是唯一能定位「玩家刚才在看
        #: 哪儿」的东西——所以它必须在**每次消失之前**就写下来，见 `remove`。
        self._anchor_offset = 0
        self._selection_cleared = False
        self._rebuilds = 0

    # -- 查询条件与代次 ------------------------------------------------

    def set_sort(self, field, descending=False):
        # type: (str, bool) -> int
        """改排序，返回新的查询代次。

        **立即生效**（重排不推迟）：这是玩家的操作，不是数据流的抖动。
        「重排推迟到批次结束」针对的是后者——把玩家刚点的排序压到下一批
        之后，表现就是「点了没反应」。
        """
        if field not in SORT_FIELDS:
            raise ValueError("未知的排序字段 %r" % (field,))
        self._sort = field
        self._descending = bool(descending)
        return self._invalidate()

    def set_filter_text(self, text):
        # type: (object) -> int
        """改筛选文本，返回新的查询代次。空串 / None = 不过滤。"""
        self._filter = _text(text if text is not None else "").lower()
        return self._invalidate()

    def _invalidate(self):
        # type: () -> int
        """条件变了：推进代次并强制重算顺序。

        推进代次是**唯一**能让在途的旧结果作废的办法；`_order = None` 则
        让下一次取值立刻重算，不受「批次进行中不重排」的推迟约束——推迟是
        为了不跟着数据流洗牌，不是为了不给玩家改条件。
        """
        self._generation += 1
        self._order = None
        self._order_dirty = False
        return self._generation

    def query_generation(self):
        # type: () -> int
        return self._generation

    def is_stale_generation(self, generation):
        # type: (int) -> bool
        """代次过期了没有。**这里不抛异常**：调用方常要在「我就是想问过没
        过期」的地方用它（比如一个滚动回调），在那种地方抛只会被吞掉。"""
        return generation != self._generation

    def query(self):
        # type: () -> tuple
        """按当前条件求一次结果，返回 `(代次, 可见键元组)`。

        **代次与结果在同一个返回值的两半**，调用方没法只拿结果、忘了代次
        ——那正是 `query_page` 那道门的钥匙。
        """
        self._ensure_order()
        return self._generation, tuple(self._order)

    def query_page(self, generation, offset, limit):
        # type: (int, int, int) -> tuple
        """取一页 `ItemEntry`。**代次不符即抛 `StaleQuery`**。

        这里的分页是**渲染切片**：`snapshot_chunk` 的那个「批」是传输分批，
        两者不要混。服务端筛选分页按 11.md 的待定项「测量后决定」，因此本轮
        没有 `server_side_query` 这类死配置——将来加它是一条新的消息种类，
        不是返工这一处。
        """
        if generation != self._generation:
            raise StaleQuery(
                "查询代次 %r 已过期（当前 %r）" % (generation, self._generation)
            )
        require_count(offset, "offset")
        require_count(limit, "limit")
        self._ensure_order()
        window = self._order[offset:offset + limit]
        return tuple(self._entries[key] for key in window)

    # -- 顺序 ----------------------------------------------------------

    def _ensure_order(self):
        # type: () -> None
        """按需重算可见顺序。

        **批次进行中不重排**，除非顺序还没建过：一批增量会连着几十次改动
        顺序判据，每次都重排等于把整表洗上几十遍，玩家看到的是表格在抖。
        批次结束时算一次就够——那正是 `ApplyResult.reordered` 要回答的时刻。

        「还没建过」是例外：新会话的第一批增量可能早于第一次渲染（分片还在
        路上），这时不建就等于列表空着。那不是「推迟」，是「没有」。
        """
        if self._order is None:
            self._rebuild()
            return
        if self._order_dirty and self._batch is None:
            self._rebuild()

    def _rebuild(self):
        # type: () -> None
        entries = self._entries
        order = [key for key in entries if self._matches(entries[key])]
        order.sort(key=self._sort_key, reverse=self._descending)
        self._order = order
        self._order_dirty = False
        self._rebuilds += 1

    def _sort_key(self, key):
        # type: (ResourceKey) -> tuple
        """排序键。**每一支都以资源键指纹收尾**。

        没有这个兜底，两件同名物品（附魔不同、附加值不同）的先后就取决于
        字典序——而那是**每次运行都可能不同**的顺序。列表于是会在两次进游戏
        之间自己换位置，且没有任何一处代码有错。
        """
        entry = self._entries[key]
        tiebreak = (entry.sort_name_key, _fingerprint(key))
        if self._sort == SORT_COUNT:
            return (entry.stored,) + tiebreak
        if self._sort == SORT_KIND:
            return (entry.kind,) + tiebreak
        return tiebreak

    def _matches(self, entry):
        # type: (ItemEntry) -> bool
        if not self._filter:
            return True
        return self._filter in entry.search_text

    def rebuilds(self):
        # type: () -> int
        """重排过多少次。**只为可观测**：11.md 要求「数量排序按批合并」，
        而「合并没有」从结果上看不出来——同样的最终顺序，重排一次与一百次
        长得一模一样，只有这个计数能把它们分开。"""
        return self._rebuilds

    def index_of(self, key):
        # type: (ResourceKey) -> int
        """这个键在**当前可见顺序**里的下标；不在（或被筛选掉）返回 -1。

        返回值只用来定位，**不是身份**：存下来当点击目标的话，下一次收包
        之后它指的可能是另一件物品。
        """
        self._ensure_order()
        return self._live_index(key)

    def _live_index(self, key):
        # type: (ResourceKey) -> int
        """在**已经建好的**顺序表里查，不触发重算。

        单独一个口是因为 `remove` 要用它：删除发生在写入路径上，那里
        `_ensure_order` 会（在批次之外）顺手重排一次，等于把刚拿到手的位置
        又算旧了。
        """
        if self._order is None:
            return -1
        try:
            return self._order.index(key)
        except ValueError:
            return -1

    def count(self):
        # type: () -> int
        """当前可见的行数（受筛选影响）。"""
        self._ensure_order()
        return len(self._order)

    def total_quantity(self):
        # type: () -> int
        """当前可见行的已存数量之和。

        跟着筛选走：筛选本来就是玩家在问「这一类一共有多少」，而筛完之后
        还报全局总量，那个数字与他正在看的东西对不上。
        """
        self._ensure_order()
        return sum(self._entries[key].stored for key in self._order)

    def version(self):
        # type: () -> int
        """物品视图版本（最后一次快照或批次的结果版本）。

        **它不是交易的预期版本**（11.md 第 119 行）。发给服务端的命令带的是
        存储索引版本，那一个由传输层自己记——两个版本各有其名，不共用。
        """
        return self._version

    # -- 数据写入 ------------------------------------------------------

    def begin_batch(self, base_version, new_version):
        # type: (int, int) -> int
        """开一批增量，返回批次令牌。基准版本对不上即抛 `ListResyncRequired`。

        「一个发布窗口 = 一次版本推进」是服务端的承诺，客户端这一侧的对应
        动作就是这句比对。
        """
        require_count(base_version, "base_version")
        require_count(new_version, "new_version")
        if self._batch is not None:
            raise ValueError("上一批还没结束（令牌 %r）" % (self._batch["token"],))
        if base_version != self._version:
            raise ListResyncRequired(
                "增量基准版本 %d 与本地 %d 对不上" % (base_version, self._version)
            )
        self._batch_serial += 1
        self._batch = {
            "token": self._batch_serial,
            "new_version": new_version,
            "upserts": [],
            "removes": [],
        }
        return self._batch_serial

    def upsert(self, key, stored, reserved):
        # type: (ResourceKey, int, int) -> bool
        """写入一条**绝对量**，返回这一行是否是新出现的。

        绝对量而不是增量：增量语义要求「一条都没丢」，而这是网络上的承诺，
        做不到（11.md 的验收里专门有「增量丢失/重复/乱序」一条）。绝对量
        重复到达只是白算一遍，缺一条也只是那一行旧一会儿。

        **命中已有键、且排序不是按数量时不置脏**——这就是「纯数量变化不
        重排」的落点。放在这里是因为只有这里知道是新增还是改量。
        """
        self._require_key(key)
        require_count(stored, "stored")
        require_count(reserved, "reserved")
        entry = self._entries.get(key)
        fresh = entry is None
        if fresh:
            entry = self._entry_for(key)
            self._entries[key] = entry
            # 新键要插进去：任何排序下顺序都会变。
            self._order_dirty = True
        entry.stored = stored
        entry.reserved = reserved
        if not fresh and self._sort == SORT_COUNT and self._order is not None:
            # 数量排序下改量会换位置。**只置位不重排**：批次结束时算一次。
            self._order_dirty = True
        self._remember(key, "upserts")
        return fresh

    def remove(self, key):
        # type: (ResourceKey) -> bool
        """删掉一条，返回它原本在不在。

        **删除不置脏**：拿走一行不改变其余行的相对顺序，任何排序下都是。
        但顺序表里的那一个要**当场**摘掉（就地 O(n)，而删除本来就少），
        否则在下一批结束前它是一个指向已删条目的悬空键，一取就 KeyError。
        """
        self._require_key(key)
        if key not in self._entries:
            return False
        # **摘掉之前先记下它当时在第几行**——只有这一刻还问得出来。等批次
        # 结束再问，它就和其他已经消失的键一样无从定位，锚点只能退回顶部。
        if self._anchor_key == key:
            index = self._live_index(key)
            if index >= 0:
                self._anchor_offset = index
        del self._entries[key]
        if self._order is not None:
            try:
                self._order.remove(key)
            except ValueError:
                pass
        self._remember(key, "removes")
        if self._selected == key:
            # 选中项被删：**这里就清**，不等批次结束。留着它，界面会在下一帧
            # 拿着一个不存在的键去问数量，或者更糟——把这个键写进命令里。
            self._selected = None
            self._selection_cleared = True
        return True

    def end_batch(self, token):
        # type: (int) -> ApplyResult
        """结束一批，返回它对界面的影响。**这是数据路径上唯一的重排点。**"""
        batch = self._batch
        if batch is None:
            raise ValueError("没有打开中的批次")
        if token != batch["token"]:
            raise ValueError(
                "批次令牌不符（拿到 %r，当前 %r）" % (token, batch["token"])
            )
        self._batch = None
        self._version = batch["new_version"]

        before = None if self._order is None else list(self._order)
        self._ensure_order()
        # **顺序比对而不是「算过一遍」**：数量排序下一批增量可能重排完还是
        # 原样（没有一条越过邻居），这时让界面重排控件纯属白费。
        reordered = before != self._order

        upserts = batch["upserts"]
        removes = batch["removes"]
        anchor_key, anchor_offset = self._reanchor()
        return self._finish(
            reordered, upserts + removes, removes, anchor_key, anchor_offset
        )

    def replace_snapshot(self, version, records):
        # type: (int, object) -> ApplyResult
        """用一份快照**整体替换**当前列表，返回它对界面的影响。

        **先全部校验再换**：中途失败不能留下半份列表——那会让「哪一半是新的」
        无从判断，而列表显示的错误数字看起来与服务端没什么两样。

        重复键是拒收而不是「后者覆盖前者」：服务端按资源键聚合成一条，
        出现两条意味着它的索引坏了；静默取最后一条会把那个缺陷藏起来。
        """
        require_count(version, "version")
        entries = {}
        for key, stored, reserved in records:
            self._require_key(key)
            require_count(stored, "stored")
            require_count(reserved, "reserved")
            if key in entries:
                raise ValueError("快照里出现重复资源键：%s" % (key.canonical(),))
            entry = self._entry_for(key)
            entry.stored = stored
            entry.reserved = reserved
            entries[key] = entry

        # 换掉之前先记下「刚才看得见的是哪些」：`removed_keys` 只能从这里来，
        # 换完之后旧键已经无处可查。没有建过顺序时按当前筛选条件现算一份，
        # 结论一样（条目还没换）。
        if self._order is not None:
            before_keys = list(self._order)
        else:
            before_keys = [
                key for key in self._entries if self._matches(self._entries[key])
            ]

        self._entries = entries
        self._order = None
        self._order_dirty = False
        self._version = version
        # 快照把在途批次一并作废：它是从这一刻重来，不是接着上一批往下走。
        self._batch = None
        if self._selected is not None and self._selected not in entries:
            self._selected = None
            self._selection_cleared = True
        # **整份列表都变了，因此推进代次**：在途的旧查询（页偏移按旧表算的）
        # 必须作废，否则它会拿着旧偏移去切新表，切出来的行看着没错、其实错位。
        self._invalidate()

        self._ensure_order()
        # 快照后锚点还在的话按它的新位置走；没了就用 `_anchor_offset`——
        # 那正是「上一次重排时它占着第几行」。
        anchor_key, anchor_offset = self._reanchor()
        removed = [old for old in before_keys if old not in entries]
        return self._finish(
            True, list(entries.keys()) + removed, removed, anchor_key, anchor_offset
        )

    def clear(self):
        # type: () -> None
        """丢掉全部内容（界面关闭 / 断线重来）。版本**不重置**。

        版本留着：下一次快照的 `replace_snapshot` 会重新定下它。这里把它
        归零的话，一个迟到的增量批次会以为自己接得上（`base_version=0` 与
        本地 0 相等），于是把几条绝对量写进一张本就该由快照重建的表里。
        """
        self._entries = {}
        self._order = None
        self._order_dirty = False
        self._batch = None
        self._selected = None
        self._anchor_key = None
        self._anchor_offset = 0
        self._selection_cleared = False
        self._invalidate()

    # -- 选择与锚点 ----------------------------------------------------

    def select(self, key):
        # type: (ResourceKey) -> bool
        """选中一行，返回是否选上了。

        参数只能是**资源键**。传一个已经不存在的键（那一行刚被删掉）返回
        False 而不是抛异常：点击与收包本来就是竞态，玩家点在一行刚消失的
        格子上不是错误，界面什么都不做即可。**也不要拿下标来兜底**——
        「那一片区域现在是谁」与「玩家看见的是谁」是两回事。
        """
        self._require_key(key)
        if key not in self._entries:
            return False
        self._selected = key
        return True

    def selected_key(self):
        # type: () -> object
        """当前选中的资源键；没选中返回 None。"""
        return self._selected

    def clear_selection(self):
        # type: () -> None
        self._selected = None

    def set_anchor(self, key, offset):
        # type: (object, int) -> None
        """记住「玩家正盯着哪一行」。`key` 传 None 即取消锚点。

        锚点不是选中项：滚动不改选中，选中也不该让列表跳走。两个状态分开，
        是因为它们由不同的事件驱动（滚动 vs 点击），合起来就会出现「滚了
        一下把选中的东西丢了」。

        这里**不校验这一行当前可见**：滚动回调报上来的行可能刚刚被筛掉或
        删掉，那不是调用方的错。落不到实处时，下一次重排会把它退回到
        `offset` 那一行（见 `_reanchor`）。
        """
        if key is None:
            self._anchor_key = None
            self._anchor_offset = 0
            return
        self._require_key(key)
        require_count(offset, "offset")
        self._anchor_key = key
        self._anchor_offset = offset

    def anchor(self):
        # type: () -> tuple
        """`(锚点键, 它在当前顺序里的下标)`；没有锚点时是 `(None, 0)`。

        重排之后报的是**新的下标**，因此调用方只要盯着这个键，行就不会在
        玩家眼皮底下换人。
        """
        if self._anchor_key is None:
            return None, 0
        index = self.index_of(self._anchor_key)
        if index >= 0:
            # 顺手刷新备忘：两次重排之间玩家可能滚过（`set_anchor` 会更新），
            # 也可能这一行被别的行挤动过（只有这里能发现）。
            self._anchor_offset = index
        return self._anchor_key, self._anchor_offset

    def _reanchor(self):
        # type: () -> tuple
        """重排之后锚点落到哪儿。返回 `(键, 下标)`。

        锚点自己没了（被删掉 / 被筛掉 / 快照里没了）时退到它原来的位置：
        玩家盯着第 12 行，第 12 行没了，那就还是第 12 行——把他甩回列表顶部
        是更糟的答案。越界就贴到末尾。

        **落定之后锚点重新挂到那一行上**（`_anchor_key` 一起改），因此
        `anchor()` 报的键永远是当前列表里真实存在的一行。挂回原来那个已经
        消失的键的话，它会一直「不存在」，下一次重排又要从头算一遍回退位置，
        而回退位置会随着列表缩短一路往前漂。
        """
        anchor = self._anchor_key
        if anchor is not None:
            index = self.index_of(anchor)
            if index >= 0:
                self._anchor_offset = index
                return anchor, index
        if not self._order:
            self._anchor_key = None
            self._anchor_offset = 0
            return None, 0
        index = max(0, min(self._anchor_offset, len(self._order) - 1))
        self._anchor_key = self._order[index]
        self._anchor_offset = index
        return self._anchor_key, index

    def _finish(self, reordered, touched, removes, anchor_key, anchor_offset):
        # type: (bool, list, list, object, int) -> ApplyResult
        """包一个 `ApplyResult`。**键一律按指纹排序**：批次里的键来自字典与
        集合，顺序随运行而变，而调用方会拿它去遍历重画——不排序的话，
        「同一批增量画出来的顺序」两次运行可以不同。"""
        cleared = self._selection_cleared
        self._selection_cleared = False
        return ApplyResult(
            reordered,
            tuple(sorted(set(touched), key=_fingerprint)),
            tuple(sorted(set(removes), key=_fingerprint)),
            anchor_key,
            anchor_offset,
            cleared,
            self._version,
        )

    # -- 内部 ----------------------------------------------------------

    def _require_key(self, key):
        # type: (object) -> None
        """只收物品键。流体出现在物品视图里是**发布端**的问题，
        在这里静默跳过会把那个缺陷藏到玩家看见为止。"""
        if not isinstance(key, ResourceKey):
            raise TypeError("列表身份必须是 ResourceKey，得到 %r" % (key,))
        if not key.is_item():
            raise ValueError("物品视图不接受非物品键：%s" % (key.canonical(),))

    def _entry_for(self, key):
        # type: (ResourceKey) -> ItemEntry
        name = key.name if self._name_of is None else _text(self._name_of(key))
        return ItemEntry(key, name)

    def _remember(self, key, bucket):
        # type: (ResourceKey, str) -> None
        """记下本批动过的键。没有打开批次时（单条写入）什么也不记。"""
        if self._batch is not None:
            self._batch[bucket].append(key)
