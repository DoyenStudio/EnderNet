# 订单发起者

订单保存 `player_id`，在受理下单命令时从服务端确认的
`command.source.player_id` 读取；客户端 payload 中同名字段不参与身份记录。
该字段仅记录发起者，不改变订单操作权限或共享网络规则。

`Order.to_dict()` 和订单簿摘要 `Order.book_entry()` 均包含 `player_id`。
完整订单快照按 `Order.__slots__` 导出时也会保存该字段。
未提供玩家来源的领域调用默认使用空字符串，不推断发起者。

订单进入完成状态后，只向在线发起者发送一次聊天提示
`§7[§ai§7] §a订单已完成： 数量x物品名`，并播放
`note.endernet.order_finished`。生产完成与库存已满足的零步骤完成均通知；
取消、失败、重新载入已完成订单不通知。无发起者或发起者离线时跳过，
提示失败不回滚订单，工厂的 `notification_failed` 计数用于排障。

公共数量格式化函数位于 `common/resource_amount.py`。
`format_resource_amount(kind, amount)` 接收整数单位物：物品直接显示数量，
流体按 `1 单位物 = 50 mB` 显示，例如 20 单位物显示为 `1000mB`。
订单数量沿用原生单位（物品个数 / 流体 mB），通知调用时显式传
`native=True`，避免重复换算；25 mB 会保持为 `25mB`。
