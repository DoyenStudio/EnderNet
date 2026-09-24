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

`common/item_codec.py` 不导入引擎。客户端背包模拟已复用 `item_key()`。

- `item_key(item)` / `item_key_from_dict(data)`：真实字段转为资源身份，空槽返回 None。
- `item_dict_from_key(key, count=1)`：独立引擎字段描述，默认可用作图标样本。
- `item_from_key(key, count, Item)`：端侧注入构造器，验证 maxStackSize 后返回物品。

保留 None 与空值，不经过 ToolDelta 的浅复制或带默认值的 from_dict。
原始字典入口拒绝未知引擎字段；已有 Item 对象按已知字段转换，DEBUG 下额外核对
其 `_orig`。若目标 SDK 返回额外字段，
应核实语义后补充映射。既有 Item 对象已经被上游填入的默认值无法反推为缺失字段，
后续真实容器应优先从原始引擎字典进入共享转换器。

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
