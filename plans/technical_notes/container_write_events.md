# 容器 API 写入与变更事件

2026-10-04，通过 Bridge MCP 在主世界 `(28, 68, -4)` 的
`skybluetech:redstone_furnace`（6 格自定义容器）实测。

## 问题与修复

EnderNet 的 `EngineItemPrimitives.set_container_item` 直接调用 ModSDK
`SpawnItemToContainer`，不经过 ToolDelta 的 `SetContainerItem`，也不直接改 NBT。
内部资源身份把空字符串、零归一成 `None`；此前适配器将这种字典原样写回引擎。

逐字段测试发现：`durability`、`customTips`、`extraId` 任意一个显式传 `None`，
都能使写入返回 `True`、实物数量增加，却收不到 `ContainerItemChangedServerEvent`。
最小字典、去掉空字段的字典，以及把上述三个值改为 `0`、`""`、`""` 的字典，
均收到事件。`enchantData`、`modEnchantData`、`userData` 单独传 `None` 时仍收到事件。
结论限于当前游戏版本与本次实测容器，不据此断言所有引擎版本都一样。

修复在容器 API 写入边界复制字典，将上述三个缺省或 `None` 值转回引擎空值；
其余字段、非空元数据、API 返回值保持原语义。不改变内部 ResourceKey 的归一化规则，
不修改上游 ToolDelta 子模块，也不人工派发变更事件。

## 验证

- 离线容器测试 **56 项通过**，新增 3 项覆盖空槽插入、堆叠、清空、身份往返、
  非空元数据保留、输入字典不变及写入失败返回值。
- **119 个文件通过 Python 2.7 门禁**；生成资源检查通过（23 个方块）。
- 完整门禁运行 **3846 项**，3 项失败、1 项错误。将运行时适配器方法换回 HEAD
  版本后，以下四项仍以相同原因失败，属于本次修改之外的现有工作区问题：
  `test_executor.TestSubmit.test_production_order_is_created_even_when_target_is_already_stored`、
  `test_io.TestPortConfig.test_an_empty_filter_is_not_ready_under_the_stand_in`、
  `test_order_notifications.TestNotificationAdapter.test_message_and_sound_target_only_initiator`、
  `test_order_preview_window.TestWindowJson.test_the_row_icon_and_texts_are_bound`。
- 游戏内从已保存源文件提取修复后的方法，应用到运行中的适配器，调用正常
  `EngineItemContainer.insert`：空槽插入 1 张纸、再堆叠 1 张纸，回执各搬动 1 件，
  收到槽 0 的 `0→1`、`1→2` 事件。通过 `write_slot(0, None)` 清空测试物品，
  并移除临时监听。此为适配器方法实测，未执行完整工厂订单的端到端验收。
