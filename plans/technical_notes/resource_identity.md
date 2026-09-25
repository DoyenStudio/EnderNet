# ResourceKey 实现与接入说明

更新日期：2026-09-24。对应任务 01-02，以及 05/06/11/12/13 的资源边界。
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

1. **引擎给 `userData` 里的每个标量加类型标签。** 写普通值，读回来是
   `{"__type__": T, "__value__": V}`：`1`=Byte（**兼表布尔与 None**，
   值 0/1/2）、`3`=Int、`5`=Float、`8`=String；空列表与空字典**不加**标签。
   解包实现见 `common/item_codec.py::unbox_engine_value`。

2. **引擎对 `userData` 里的 py2 `unicode` 是破坏性的**，与内容是不是 ASCII
   无关：

   | 写进去的 | 读回来 |
   | --- | --- |
   | `{"k": "v"}`（字节串） | 原样往返 |
   | `{u"k": u"v"}` 或 `{u"k": "v"}`（unicode **键**） | **整个条目消失** |
   | `{"k": u"v"}`（unicode **值**） | 条目在，但被**清空成 `{}`** |
   | 非 ASCII 的 UTF-8 字节串 | 留得下，但被再编码一次（`中文` → `ä¸æ`） |

   因此**写进 `userData` 的文本必须是纯 ASCII 的 py2 `str`**；
   需要携带非 ASCII 时要自己转义（样板层用 `\uXXXX`，见
   `crafting/patterns.py` 的 `_escape_text`）。

3. **`SpawnItemToContainer` 的布尔返回值不可全信。** 实测到「返回 `False`、
   物品其实写进去了」。判据只能是**写回之后逐槽读回**（架构规划 §6.3 的
   那条纪律反过来同样成立：不看返回值会漏报，全信返回值会误报）。

**尚未确认**：这三条只在本模组的 `netease:block_container` 与原版箱子上
验证过；玩家背包路径（`SpawnItemToPlayerInv` / `GetPlayerItem`）没有实测。
`EngineContainerProvider` 目前把 `userData` 原样交给 `item_codec`，
因此**带 `customTips` 的物品在容器里往返会得到不同的 `ResourceKey`**
（写进去是顶层 `customTips`，读回来引擎把它搬进了
`userData["ItemCustomTips"]`）——这一条影响 05/06 的守恒核对与 12-02 的
介质身份，**修不修、在哪里修由它们决定**，本文件只记录事实。

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

`python endernet_tests/run_tests.py`：547 项测试通过，38 个源码文件通过 Python 2.7
语法门禁。新增回归覆盖嵌套引用隔离、描述载荷往返、固定 Unicode 指纹、实际 ToolDelta
Item 类的转换、描述首次/增量/丢失/旧会话、缺失资源原样保留和恢复。
ToolDelta 测试替换引擎初始化依赖，未运行游戏；未实测 Python 2 解释器、引擎物品
堆叠语义、真实存档和网络传输。新增测试也覆盖 DEBUG 关闭时的正常链路与外部数据
拒绝。哈希缓存和模糊索引留待测量及后续业务需要。
