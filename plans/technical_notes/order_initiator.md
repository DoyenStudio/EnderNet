# 订单发起者

订单保存 `player_id`，在受理下单命令时从服务端确认的
`command.source.player_id` 读取；客户端 payload 中同名字段不参与身份记录。
该字段仅记录发起者，不改变订单操作权限或共享网络规则。

`Order.to_dict()` 和订单簿摘要 `Order.book_entry()` 均包含 `player_id`。
完整订单快照按 `Order.__slots__` 导出时也会保存该字段。
未提供玩家来源的领域调用默认使用空字符串，不推断发起者。
