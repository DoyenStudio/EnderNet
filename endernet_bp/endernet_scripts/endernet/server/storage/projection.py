# coding=utf-8
"""终端网络列表的**显示投影**：真实库存与网络样板产物的并集。

权威规则见 `plans/technical_notes/terminal_pattern_order_preview.md` 第 1 节。
本模块只做一件事：把「这个网络此刻该显示哪些行」摊开给 11 的同步层。

三条不许含糊的取向：

1. **不写假库存。** 只有样板、没有库存的行是 `stored=0, reserved=0,
   has_pattern=True`——数量就是真实数量，`+` 是**客户端**由 `stored` 与
   `has_pattern` 一起推出来的表现。谁要是为了让 `+` 显示出来而把这里改成
   `stored=1`，那是在显示层伪造库存，下游任何按 `stored` 做的判断都会跟着错。
2. **交易版本与显示版本是两个属性。** `index_version` 原样转发真实存储服务
   的那个（会话握手要它）；`publish_version` 是投影自己的，只在投影自己
   排空脏键时推进。契约第 6 节「显示版本不能冒充实际交易的库存 index_version」
   在这里是结构性的，不是靠调用方自觉。
3. **样板标记按网络算，规划不按。** 哪些样板算「本网络的」由网络成员决定；
   而**规划**（预览与下单）读的仍是全进程一份的样板索引——这是 09 早就有的
   口径（见 `bootstrap._factory_builder` 的说明），本模块不替它改。

变更检测靠**轮询**：`PatternStore` 没有推送口，只有 `mark_dirty` + 每帧
`advance`。三个信号合流：真实库存的脏键、样板内容修订号、网络成员签名。
成员签名**不是多余的**——设备换网时样板内容一点没变，修订号不会动，
只有成员集合看得出来。

四个依赖全是**回调而不是对象**：`storage_of` / `members_of` 让运行时被退役
重建之后投影自动跟到新的那一份（不必有失效钩子），`products_of` /
`products_revision` 则把「样板索引」这条依赖挡在本模块之外——
`server/storage/` 不需要 import `server/crafting/`，离线用例塞两个纯函数即可。
"""


class NetworkItemProjection(object):
    """一个网络的显示投影。**只读口**，不持有引擎、不写任何业务状态。

    按鸭子类型满足 `TerminalSyncHub` 要的那组只读口（见 `terminal.py` 的
    `_STORAGE_READ_PORT`）：`has_dirty_items` / `take_dirty_keys` /
    `read_items` / `iter_item_records`，外加 `publish_version`。
    """

    __slots__ = (
        "network_id",
        "_storage_of",
        "_members_of",
        "_products_of",
        "_products_revision",
        "_marks",
        "_signature",
        "_revision",
        "_dirty",
        "publish_version",
        "stats",
    )

    def __init__(self, network_id, storage_of, members_of, products_of,
                 products_revision=None):
        # type: (str, object, object, object, object) -> None
        self.network_id = network_id
        #: `() -> StorageService | None`，**每次现取**。
        self._storage_of = storage_of
        #: `() -> tuple[NodeKey, ...]`，本网络当前成员。
        self._members_of = members_of
        #: `(frozenset[tuple]) -> iterable[ResourceKey]`：给一批成员位置
        #: （`NodeKey.as_key()`），返回它们的样板产物。
        self._products_of = products_of
        #: `() -> int | None`：样板内容的修订号。**可为 `None`**，那时每轮
        #: 都重算产物集合——退化方向是「多算几次」，不是「错过一次改动」。
        self._products_revision = products_revision
        #: 本网络的样板产物键集合。**只含物品键**（契约第 1 节：暂不扩展
        #: 网络列表的流体显示）。
        self._marks = frozenset()
        #: 上次算 `_marks` 时的依据 `(修订号, 成员签名)`。任一变即重算。
        self._signature = None
        self._revision = None
        self._dirty = set()
        #: 显示版本。**不是** `index_version`，见模块 docstring 第 2 条。
        self.publish_version = 0
        self.stats = {
            "storage_drains": 0,
            "mark_scans": 0,
            "rows": 0,
            "windows": 0,
            "published_keys": 0,
        }

    # -- 与库存同形的只读口 --------------------------------------------

    @property
    def index_version(self):
        # type: () -> int
        """真实存储服务的**交易**版本，原样转发。

        `TerminalSyncHub._open` 会读它填 `SessionReady.index_version`。
        取不到存储服务时回 0——那正是「这个网络现在读不到库存」，
        与 `_sync_storage_of` 返回 `None` 时同一句话。
        """
        storage = self._storage_of()
        if storage is None:
            return 0
        return int(getattr(storage, "index_version", 0) or 0)

    @property
    def inventories(self):
        # type: () -> object
        """玩家背包的提供者，**原样转发**（11-13 的背包快照走它）。

        它不是显示投影的一部分——背包属于玩家，不属于这张网络的库存。
        转发是因为 hub 只查一次「这个网络对应的存储件」（`_storage_of`），
        于是同一份对象上要能拿到它在用的**全部**东西；换成第二个查找口子，
        两处迟早会指向不同的服务实例。
        """
        storage = self._storage_of()
        if storage is None:
            return None
        return getattr(storage, "inventories", None)

    def has_dirty_items(self):
        # type: () -> bool
        """有没有待发布的显示变化。**不推进版本、不清空集合。**"""
        self._refresh()
        return bool(self._dirty)

    def take_dirty_keys(self):
        # type: () -> tuple
        """取出并清空脏键。**非空时才推进显示版本**（与存储服务同一条）。

        这一个调用就是发布窗口的原子点：排序、清空、推进版本在同一段语句里
        完成，因此窗口之后到达的改动必然落到下一个窗口。
        """
        self._refresh()
        if not self._dirty:
            return ()
        keys = tuple(sorted(self._dirty, key=lambda item: item.canonical()))
        self._dirty = set()
        self.publish_version += 1
        self.stats["windows"] += 1
        self.stats["published_keys"] += len(keys)
        return keys

    def read_items(self, keys):
        # type: (object) -> tuple
        """按 `keys` 顺序返回其中**仍然显示**的四元组行。

        与 `StorageService.read_items` 同一条契约：返回的元组里没有的键
        就是**该删掉的行**，调用方据此构造 remove，不必再猜。差别只有一个
        ——这里「还存在」的判据多了「有样板」那一项（契约第 1 节：
        「最后一份相关样板移除且库存为零时才删除显示条目」）。

        **读之前先自查一次**：样板标记是轮询来的，不先刷新的话，读到的
        会是「上一次问脏键时」的那份标记。发布窗口里两者恰好同帧，
        但开会话时拿快照的那条路没经过脏键查询——那条路上漏掉刷新，
        表现是新开的终端看不到纯样板行。
        """
        self._refresh()
        storage = self._storage_of()
        present = {}
        if storage is not None:
            for key, stored, reserved in storage.read_items(keys):
                present[key] = (stored, reserved)
        out = []
        for key in keys:
            stored, reserved = present.get(key, (0, 0))
            has_pattern = key in self._marks
            if stored <= 0 and reserved <= 0 and not has_pattern:
                continue
            out.append((key, stored, reserved, has_pattern))
        return tuple(out)

    def iter_item_records(self):
        # type: () -> tuple
        """整份显示行的冻结快照，canonical 序。

        库存那一半来自存储服务（**只含物品键**，流体不进物品视图），
        样板那一半补上零库存的行。两半合起来正是契约第 1 节的「并集」。

        与 `read_items` 一样**先自查一次**：快照那条路未必紧跟在一次
        脏键查询后面。
        """
        self._refresh()
        storage = self._storage_of()
        rows = {}
        if storage is not None:
            for key, stored, reserved in storage.iter_item_records():
                rows[key] = (stored, reserved)
        for key in self._marks:
            if key not in rows:
                rows[key] = (0, 0)
        records = tuple(
            (key, rows[key][0], rows[key][1], key in self._marks)
            for key in sorted(rows, key=lambda item: item.canonical())
        )
        self.stats["rows"] = len(records)
        return records

    def view_version(self):
        # type: () -> int
        """显示版本（= `publish_version`）。诊断用，与交易版本分列。"""
        return self.publish_version

    # -- 变更检测 ------------------------------------------------------

    def _refresh(self):
        # type: () -> None
        """把三个信号合流进脏集。**幂等**，一帧内被问几次都只算一次。"""
        self._drain_storage()
        self._scan_marks()

    def _drain_storage(self):
        # type: () -> None
        """把真实库存的脏键收进来。

        投影**成为存储服务 `take_dirty_keys()` 的唯一消费者**——「只有一个
        原子点」这条不变量因此保住：消费者换了位置，没有多出一个。
        代价是 `StorageService.publish_version` 从此只是它自己的排空计数，
        不再等于终端看到的物品视图版本；这句话也写在装配层的
        `_sync_storage_of` 旁边，免得别处误读它。
        """
        storage = self._storage_of()
        if storage is None:
            return
        if storage.has_dirty_items():
            self._dirty |= set(storage.take_dirty_keys())
        self.stats["storage_drains"] += 1

    def _scan_marks(self):
        # type: () -> None
        """样板产物集合变了才重算，并把**对称差**并入脏集。

        重算的依据是 `(修订号, 成员签名)` 这一对。用对称差而不是「全量重发」：
        重发整份快照会让每一次样板改动都推一遍全网络的行，而增量那条路
        本来就是逐键 upsert，把真的变了的键交出去就够了。
        """
        revision = self._pattern_revision()
        signature = self._member_signature()
        if revision is not None and revision == self._revision \
                and signature == self._signature:
            return
        marks = self._scan_products(signature)
        added = marks - self._marks
        removed = self._marks - marks
        self._marks = marks
        self._revision = revision
        self._signature = signature
        self.stats["mark_scans"] += 1
        if added or removed:
            self._dirty |= set(added) | set(removed)

    def _pattern_revision(self):
        # type: () -> object
        """样板内容的修订号；没有这个回调时回 `None`（每轮重算）。"""
        if self._products_revision is None:
            return None
        return self._products_revision()

    def _member_signature(self):
        # type: () -> tuple
        """成员签名，**排序后**：成员顺序不该让签名变。"""
        return tuple(sorted(key.as_key() for key in self._members_of()))

    def _scan_products(self, signature):
        # type: (tuple) -> frozenset
        """本网络的样板产物集合。流体剔除，契约第 1 节。"""
        members = frozenset(signature)
        out = set()
        for key in self._products_of(members):
            if key.is_item():
                out.add(key)
        return frozenset(out)

    # -- 诊断 ----------------------------------------------------------

    def diagnostics(self):
        # type: () -> dict
        return {
            "network_id": self.network_id,
            "publish_version": self.publish_version,
            "index_version": self.index_version,
            "marks": len(self._marks),
            "dirty": len(self._dirty),
            "members": len(self._signature or ()),
            "stats": dict(self.stats),
        }

    def __repr__(self):
        return "NetworkItemProjection(%s, v%d, %d 个样板产物)" % (
            self.network_id,
            self.publish_version,
            len(self._marks),
        )
