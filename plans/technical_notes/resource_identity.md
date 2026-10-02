# ResourceKey 实现与接入说明

更新日期：2026-09-29。对应任务 01-02，以及 05/06/11/12/13 的资源边界。
参考 AE2 `45f315517ea346efc0babd02c85c6b9d32dc8acf` 的 AEItemKey、GenericStack、
IncrementalUpdateHelper 与缺失内容保留机制。

## 1. 身份与复制边界

`common/resource_key.py` 保持普通 Python 对象，不拦截属性赋值，不强制 immutable。
构造时复制并校验元数据，`raw_identity` / `to_dict()` 返回独立深层副本。
调用方不得在键进入库存字典后修改其内部身份字段；身份变化应构造新键。
数量不属于身份，精确身份比较与未来的配方/过滤器模糊匹配分开。
容量全程使用整数微单位，已删除无业务调用的 `micro_units_to_units()` 及 Fraction
依赖。展示若需拆分整数部分与余量，可直接使用 `divmod(value, 50)`。

`common/settings.py` 统一设置 `DEBUG = True`。`ResourceKey.__init__` 只赋值，
内部工厂及轻量对象构造的开发期检查由 `if settings.DEBUG` 控制。
关闭 DEBUG 后仍执行数据复制和身份规范化；ResourceKey.from_dict、引擎字典解码、
同步解码和存档恢复是外部数据边界，继续检查格式、数量及版本，不信任外部载荷。

按当前项目约定，载荷来自可信 s2s/s2c 路径，调用方保证数据可存入 ExtraData。
不设 JSON 可逆约束，不执行额外的递归数据清洗或深度、节点数、文本大小限制。
构造和导出均使用 deepcopy 保留原始数据类型。canonical_tree 只负责生成可比较、
可哈希的身份；其中 tuple/list 使用相同序列语义，但不会改变保存的数据。

资源描述写入 `schema_version: 1`，兼容最初未带版本的字典结构。
未知版本、未知字段和非法字段类型拒绝正常解码。文本指纹长度按 UTF-8 字节数计算，
字符串键按 UTF-8 排序，避免 Python 2 窄 Unicode 构建与 Python 3 的差异。
ASCII 指纹维持原形式；旧非 ASCII 文本指纹应由完整描述重新生成。
存档使用结构化描述，不依赖 Python hash 或文本指纹反解析。

## 2. 共享物品转换

### 介质动态容量 Lore 的新增边界（待实施）

当前实现仍将整个 `user_data` 纳入身份；本段为新增契约，不代表已改变现有算法。仅对 EnderNet 存储介质，在身份投影中排除本模组可靠识别的动态容量行和纯展示管理信息，保留自定义名称、其他 Lore 和其他身份字段；稳定介质 ID 参与实例区分。完整物品载荷仍保留展示数据，实际还原按最新权威摘要物化，不从旧键复制过期 Lore。不得全局剔除 `display` 或原地修改字典中的键。

ID 携带、行归属标记、空结构归一化、版本迁移及描述会话失效须先完成 12-02 契约；详见 [介质 Lore 实施契约](medium_lore.md)。动态容量变动不要求每笔交易重建介质物品键；资源身份、介质实例、盘内资源与展示缓存分别管理。现有“同键完整载荷往返”的假设须在介质特例下补测最新展示还原，不能只改哈希比较而遗漏实际输出路径。

### 现有转换入口

`common/item_codec.py` 不导入引擎。客户端背包模拟已复用 `item_key()`。

- `item_key(item)` / `item_key_from_dict(data)`：真实字段转为资源身份，空槽返回 None。
- `item_dict_from_key(key, count=1)`：独立引擎字段描述，默认可用作图标样本。
- `item_from_key(key, count, Item)`：端侧注入构造器，验证 maxStackSize 后返回物品。

保留 None 与空值，不经过 ToolDelta 的浅复制或带默认值的 from_dict。
原始字典入口拒绝未知引擎字段；已有 Item 对象按已知字段转换，DEBUG 下额外核对
其 `_orig`。若目标 SDK 返回额外字段，
应核实语义后补充映射。既有 Item 对象已经被上游填入的默认值无法反推为缺失字段，
后续真实容器应优先从原始引擎字典进入共享转换器。

### 容器 userData 的三条实测事实（2026-09-25，任务 07 真机，py2.7.13）

**「同一份物品字典写进容器、读回来得到同一个 `ResourceKey`」这个假设在
`userData` 上不成立。** 三条都是实测，不是推断；任何读 `userData` 的功能
（介质 Lore、样板编码、别的模组的载荷）都要按它们设计。

1. **该容器路径中，写入的普通标量以 ModSDK NBT 表示返回。** 读回来是
   `{"__type__": T, "__value__": V}`：`1`=Byte（**兼表布尔与 None**，
   值 0=False、1=True、2=None）、`3`=Int、`5`=Float、`8`=String；本次实测
   空列表与空字典原样返回。Byte 的 None 映射是实测补充，教程没有说明，
   不应推广成任意 Byte 字段的语义。

   2026-09-28 按 mcdk-assistant 教程《NBT数据格式》（命令
   `netease NBT数据格式`）复核：`__type__` 是 NBT 类型编号，`__value__`
   是对应载荷。完整教程表还包括 `2`=Short、`4`=Int64、`6`=Double、
   `7`=ByteArray、`9`=List、`10`=Compound、`11`=IntArray；两种数组的
   载荷为整数列表，List/Compound 的载荷递归转换。因此不能从空容器的
   局部观测推导出「只有标量带标签」。教程 `netease:initial_user_data`
   还展示了写入时用这两个键显式指定类型；不能假定同形业务字典会自动
   被引擎再次包装以避免歧义。

   解包实现见 `common/item_codec.py::unbox_engine_value`：当前仅支持
   1/3/5/8，递归遍历未包装容器；其他包装连同载荷原样保留，由调用方
   校验。它用于自有样板载荷读取，会丢失类型信息，不是完整 NBT 解码器，
   也不能用于资源身份规范化；资源身份仍保留原始 `userData`。

2. **引擎对 `userData` 里的 py2 `unicode` 是破坏性的。真正的分界是
   `str` / `unicode`，不是 ASCII / 非 ASCII。**

   2026-09-29 复核（12-02 探针，走引擎直连路径：
   容器侧 `SpawnItemToContainer` + `GetContainerItem(pos, slot, dim, True)`，
   背包侧 `SpawnItemToPlayerInv` + `GetPlayerItem(INVENTORY, slot, True)`）：

   | 写进去的 | 读回来 |
   | --- | --- |
   | `{"k": "v"}`（字节串） | 原样往返 |
   | `{"a": "keepme", "b": u"poison"}`（unicode **值**） | `{"a": "keepme"}`——**只丢那一条，兄弟项完好** |
   | `{"lst": ["keepme", u"poison"]}` | `["keepme"]`——列表里被丢掉一个元素 |
   | `{u"k": "v"}`（unicode **键**） | `{"k": "v"}`——**键正常**，被转成字节串 |
   | 非 ASCII 的 UTF-8 字节串（`中文`、`§6剩余种类数：3`） | **字节级原样**，未被再编码 |

   **规则**：`unicode` 值被静默丢弃（连同它的键）；值的字典被清空到只剩
   合法项，全空时引擎对 `userData` 返回 `None`。`str` 字节串无论内容是
   否 ASCII 都安全。

   **与本节 2026-09-25 的读数不一致的地方，见下面的路径说明。**
   写进 `userData` 的文本仍建议用 py2 `str`；样板层现有的 `\uXXXX`
   转义（`crafting/patterns.py` 的 `_escape_text`）**继续有效**，
   不因为本条而需要改。

   **背包路径复核（同上 2026-09-29）**：`SpawnItemToPlayerInv` 写
   `{"keepme": "ok", "poison": u"中文", "lst": ["ok", u"x"]}` 进玩家背包，
   `GetPlayerItem` 读回 `{"keepme": "ok", "lst": ["ok"]}`——**规则逐条复现**：
   `unicode` 值连同它的键消失，列表里只掉那一个元素，兄弟项完好。
   所以这条规则**不是容器路径的孤例**，它是引擎对 `userData` 的一致行为。

   读回形状与容器路径一致：嵌套容器不包装，叶子标量包装成
   `{"__type__": T, "__value__": V}`；空槽读回 `None`（不是空气字典）。

   **路径范围（重要）**：以上两条都是引擎直连路径。
   2026-09-25 那三条记录的是任务 07 的路径（经 tooldelta 物品包装层），
   两者的差异（"整个条目消失" vs "unicode 键正常"、"再编码成 `ä¸æ`" vs
   字节原样）**很可能是包装层而非引擎造成的**。本文件不据此宣布旧读数为错；
   **经 tooldelta 包装层写 `userData` 的路径没有重测**，走那条路的功能
   仍应假定旧读数成立。

3. **`SpawnItemToContainer` 的布尔返回值不可全信。** 实测到「返回 `False`、
   物品其实写进去了」。判据只能是**写回之后逐槽读回**（架构规划 §6.3 的
   那条纪律反过来同样成立：不看返回值会漏报，全信返回值会误报）。

**2026-09-29 更新**：玩家背包路径已于当日实测。第 1 条（叶子标量带 NBT 包装、
嵌套容器不带、空槽读回 `None`）与第 2 条（`unicode` 被丢弃、非 ASCII 字节串
原样、Lore 字节级往返）在背包路径上**逐条复现**，与容器路径一致。
三个背包原语 `GetPlayerItem` / `SpawnItemToPlayerInv` / `SetInvItemNum` 都跑通了
（`SetInvItemNum(slot, 0)` 返回 `True` 且该槽随后读为 `None`）。

**仍未确认**：第 3 条（`SpawnItemToContainer` 布尔返回值不可全信）只在容器侧
观察到，背包侧没有做「返回 `False` 但实际写入」的对照；本模组自己的
`netease:block_container` 与原版箱子之外的容器类型也没试。
`EngineContainerProvider` 目前把 `userData` 原样交给 `item_codec`，
因此**带 `customTips` 的物品在容器里往返会得到不同的 `ResourceKey`**
（写进去是顶层 `customTips`，读回来引擎把它搬进了
`userData["ItemCustomTips"]`）——这一条影响 05/06 的守恒核对与 12-02 的
介质身份，**修不修、在哪里修由它们决定**，本文件只记录事实。

> **2026-10-02 已修，方向是「读取侧解包」。** 上面这条从「记录事实」升级为
> **已实测的缺陷**：11-05 的样板终端是第一批把 `userData` 承载物送过传输层的
> 物品，`template_output_take`（产物槽 → 背包）当场**复制了一份**——
> `slot_insert` 写完按资源键回读核对、判 `after["key"] != resource_key`，
> 于是报 `moved=0`，而东西**已经写进那一格**；调用方（`_template_slot_take`）
> 按 `moved` 决定扣不扣来源格，于是设备那一格没扣 → **净 +1 份**。
> 同一根因还有一条副作用：「同一份配置编两次得到同一个键」这条性质，
> 中间经一次背包往返就不成立了（设备里的键是构造出来的裸形态，
> 背包里的键是引擎读回来的带标签形态）。
>
> **修法与取舍**：`common/item_codec.normalize_user_data` 在
> `normalize_engine_item` 里对 `userData` 解包（复用既有的
> `unbox_engine_value`）并丢掉引擎复制的那份 `ItemCustomTips`（只在它与顶层
> `customTips` 相同时丢——第三方真把它当载荷写的话要留下）。这**推翻**了
> 本节原先「解包会丢 NBT 类型信息，不能用于资源身份规范化」的取舍：
> 同形歧义只是理论上的，而身份不相等已经造成了复制。载荷侧的解码
> （`crafting/store.py` 的 `unbox_engine_value`）不变，两处看到的是同一份
> 形状了。
>
> 另外补了一条**不依赖身份**的安全带（`storage/transfer.py` 的
> `slot_insert` / `slot_extract`）：写完读回核对判不等时，按**读回的实际
> 增量**如实报 `moved`（`PARTIAL` + `slot_changed`），不再报 `moved=0`。
> 理由是「报了没动就必须真的没动」——那条不变量被破坏的表现正是复制。
> 回归位：`test_resource_contracts.EngineUserDataIdentityTests`（夹具自己造
> 带标签的引擎形状）与 `test_transfer.SlotPrimitiveIsHonestTests`。

**同一天同一次实测还撞出第二条**（与身份无关，属 11-05 的取值方向）：
`_template_slot_take` 把锚上的 `key` 当插入物用，而客户端在「选中背包槽 →
点产物槽」时报的是**目标格里现在装着的东西**。不换锚的话，插进去的是目标格
那件无关物品、同时把设备里的样板扣掉。修法是服务端换锚（插入物恒为设备
这一格的 resource key），槽号与版本锚不变。见 `_template_slot_take` 的说明。

### 掉落物 API 的字典形状（2026-10-02 真机逐字段二分）

`CreateEngineItemEntity(itemDict, dimensionId, pos)`（`ServerSystem`，**没有
tooldelta 封装**）与写容器／写背包那两个口子**不吃同一种字典**：

| 要点 | 依据 |
| --- | --- |
| 名字与附加值要**旧式键名** `itemName` / `auxValue` | 给 `newItemName` / `newAuxValue` 时报 `expected string or Unicode object, NoneType found`——它读的是旧名，读不到就是 `None` |
| `customTips` / `extraId` **不接受 `None`**（要字符串） | 给 `None` 报同一条 `TypeError` |
| `durability` **不接受 `None`**（要整数） | 给 `None` 报 `an integer is required` |
| `enchantData` / `modEnchantData` / `userData` 给 `None` 也能生成 | 逐项验证过 |
| 返回值是**实体 id 字符串**，失败才是 `None` | 成功实测形如 `'-824633720831'` |

构造它的入口是 `common/item_codec.item_dict_for_drop`（补 `""` / `0`，
与读回时的 `_EMPTY_IDENTITY` 归一正好相消）。**给错形状不会报错到看得见的地方**：
`TemplateStore._drop_real_slots` 把异常接成「一件也没掉」，症状是拆了方块、
格子里的东西不掉出来，而日志干净。因此掉落回调的契约是
`((ResourceKey, 数量), ...)`，字典由引擎边界（`adapters/world.py`）自己构造。

## 3. 终端描述映射

`common/resource_sync.py` 的 `ResourceDescriptions(network_id, epoch)` 由服务端和
客户端分别持有。epoch 由外层会话生成，重开或重同步须使用新值。

`StorageChanged.to_dict()` 默认包含指纹和完整描述；传入描述表后，首次发送
`resource_ref = {epoch, serial, description}`，后续仅发送 epoch、serial 与数量。
`StorageChanged.from_dict()` 校验数量及网络后解码，未知序号、旧 epoch、序号重绑
触发拒绝；描述丢失可用 `include_description=True` 重发。
服务端点击使用 `resolve(epoch, serial)`，不能用客户端描述注册新的服务端身份。

映射默认上限 65536 个条目，同 epoch 内不回收序号。到达上限应结束该描述会话并
重新建立映射。外层仍须实现可靠传输、快照分页、库存/查询版本、会话权限和请求去重；
本模块不承担这些职责，尚未连接真实终端网络事件。

## 4. 缺失资源保留

`server/persistence/resources.py` 的 `RestoredResourceEntry(payload, is_available)`
读取 `ContainerEntry.to_dict()` 格式。注册表可用性由装配层回调明确检查，
未知版本、损坏结构、缺失模组或注册表不可读时保留整个原始条目及数量。
`to_dict()` 返回原载荷副本；`retry()` 可在依赖恢复后重新解析。
不可用条目通过 `require_key()` 拒绝交易，不作为 ResourceKey 子类参与正常库存。
尚未接入真实介质后端，不据此勾选任务 12-03 的完整验收。

## 5. 验证

`python endernet_tests/run_tests.py`：**2249** 项测试通过，**76** 个源码文件通过 Python 2.7
语法门禁（2026-09-29 复核；本节此前记的是 547 项 / 38 个文件，是本文档成文时的数字，
此后仓库整体增长，不是本节所验事项的回归）。新增回归覆盖嵌套引用隔离、描述载荷往返、
固定 Unicode 指纹、实际 ToolDelta Item 类的转换、描述首次/增量/丢失/旧会话、缺失资源
原样保留和恢复。
ToolDelta 测试替换引擎初始化依赖，未运行游戏；未实测 Python 2 解释器、引擎物品
堆叠语义、真实存档和网络传输。新增测试也覆盖 DEBUG 关闭时的正常链路与外部数据
拒绝。哈希缓存和模糊索引留待测量及后续业务需要。
