# 方块事件的引擎行为（拆除可靠性与数据可读窗口）

记录日期：2026-09-24。来源：**项目维护者本人在真机上反复验证得出的经验**，
另有一次同日的游戏内实测佐证（见下）。本文是跨任务的实现约束，
01/05/06/12 在读写方块时都受它约束。

## 一、两条事实

### 1. 拆除事件是完备的

**只要方块声明了 `netease:listen_block_remove`，那么它无论如何被破坏，
都会走 `BlockRemoveServerEvent`。**

「无论如何」包括：玩家正常破坏、脚本 `SetBlock` 覆盖、爆炸、其他模组清除等。
因此**拆除注销只需要挂在这一个事件上**，不需要 `ServerPlayerTryDestroyBlockEvent`
或 `DestroyBlockEvent` 兜底——后者只适合作诊断。

本模组的方块都声明了该组件（`endernet_bp/blocks/*.json`，由
`scripts/gen_blocks.py` 生成，见 `netease:listen_block_remove: {"value": true}`），
启动时还会对全部本模组方块调一次 `AddBlocksToBlockRemoveListener`
（`server/adapters/world.py::register_block_remove_listener`）。
两处冗余是有意的，实测一次移除只计一次。

### 2. 方块数据只在「事件触发的本 tick 内」可读

**在事件触发的那一 tick 内可以读到方块数据；到了下一 tick 就读不到了。**

推论：凡是「拆除时还需要知道的信息」，必须在**事件处理函数内部同步取**，
不能等到稍后再查。本模组是「事件入队 → 下一帧 `tick()` 才消费」的架构，
所以这条尤其要紧：**队列消费阶段不允许再去世界查这个位置。**

## 二、现有实现的对照（均已满足）

| 约束 | 代码位置 | 做法 |
| --- | --- | --- |
| 拆除只认 `BlockRemoveServerEvent` | `server/adapters/events.py::_on_remove` | 实际注销走它；`DestroyBlockEvent` 只 `_count("destroy_after")`，不注销（避免两条路径各注销一次） |
| 不在消费阶段回查世界 | `server/adapters/events.py::_on_remove` | 方块标识取自**事件载荷**（`event.fullName`），随 `NormalizedEvent` 一起入队 |
| 同上 | `server/runtime/lifecycle.py::_on_remove` | 只做 `registry.unregister_matching(node_key, device_id)` 与 `_neighbor_changes()`；两者都只读注册表，**不碰世界** |
| 同上 | `server/runtime/lifecycle.py::_neighbor_changes` | 邻居位置由坐标算出，再查注册表 |

世界查询（`world.facing_at`）只出现在 **`_resolve_facing`**，它的两个调用点
都在「方块还在」的路径上：

- `_on_place`（放置）——方块刚放下，可读；
- `_backfill_entry`（补查/手动重扫）——补查的是**存在**的方块。

因此当前没有一处会在被拆位置回查世界。

## 三、将来会咬人的地方（写给 05/06/12）

**「拆除时保存设备状态」必须同步做。** 一旦有任务需要在方块被拆掉的瞬间
读走方块实体数据（`GetBlockEntityDataDict` / `ExtraData`）——例如：

- 12 持久化：驱动器里的硬盘内容、终端会话、设备配置；
- 05 存储：介质在拆除时要不要连同内容一起保留；
- 06 转移：接口缓冲里没搬完的物品。

**这些读取必须放在事件处理函数内（适配器或紧随其后的同步路径），
不能放进队列消费阶段**——后者可能已经是下一 tick，那时数据已经读不到了。
需要的话，`NormalizedEvent` 要携带一份在事件期取好的快照。

当前 `NormalizedEvent.remove` 只带了方块标识，**没有带方块实体数据**。
真要做「拆下即保存」时，这是第一个要改的地方。

## 四、未验证

- 「无论如何被破坏都走同一事件」中的**爆炸**与其他模组清除两条，本项目尚未实测；
  玩家破坏与脚本路径已实测。
- 同日实测时 `ServerPlayerTryDestroyBlockEvent` **一次都没触发**，
  当时玩家手持的是基带方块而非镐子。基带声明了
  `netease:tier {destroy_special: true, digger: pickaxe, level: 1}`，
  因此**怀疑**拿错工具时方块根本不可破坏、该事件也就不发。
  这只是推测，**未验证**；但即便成立也不影响拆除注销——按事实 1，
  拿对工具破坏时仍然会走 `BlockRemoveServerEvent`。
  另需注意 `_on_try_destroy` 本身**不改注册表**，只发一条
  `PLACEMENT_REVIEW` 待裁决变化（R01 的钩子），因此「尝试破坏后放弃」
  不存在留下注册表状态的风险。
