# 网络可视化查看器

2026-11-13 实现，依据作者确认的当前带宽线路、实际路由、手持期间显示与灰色未知状态方案。

## 数据与生命周期

`common/definitions.py` 注册 `NETWORK_VISUALIZER_ID`，沿用 `ToolSpec`、物品生成器和中文语言表。查看不改变拓扑、准入、库存、能耗或其他玩家状态。

`server/network/view_graph.py` 将 `LineTree` 投影为节点与边：基带按命中面选根；面板使用 `PartKey` 与面中心；`TunnelSegment` 经隧道服务的 `endpoints_at()` 展开为实际端点。跨维度边不连接两个不同坐标空间，消息提示存在跨维度连接。没有线路只显示选中节点的灰色方片；重建中撤下旧边，保留仍存在的灰色节点。

稳定线路中的节点先读准入：`ok is False` 显示红色，未确认显示灰色；带宽满足时复用 `RuntimeManager.device_status(ref)` 的总体状态，`validity != "ok"` 为灰色，已确认在线为绿色、离线为橙色（包括缺电）。未加载、重建和没有线路仍为灰色。面板按自身槽位查询，普通光缆仅作连线中继。

`server/network/viewer.py` 管理按玩家隔离的临时订阅。选择复用 `CableIntentService` 的身份、真实主手、距离、方块一致性、部件射线命中及同刻去重，再校验整方块射线。保存宿主实例引用；面板拆除事件立即终止选择，即使同刻重新装上同型号面板也不继承。

拓扑与准入通知标记待刷新，每 0.2 秒最多合并构图一次，内容未变不下发；无订阅时不构图。稳定线路每 0.2 秒只复查当前维度可见方片的颜色，以覆盖不产生拓扑通知的供电变化，不重新构造路由；仅颜色变化时复用模型更新 uniform。客户端每秒保活，服务端租约 5 秒。手持变化、死亡、维度变化、目标代次变化、退出和停机关闭订阅。构图和引擎渲染分离，未修改 ToolDelta。

## 协议

独立 MC 事件总线通道：`endernet:network_view_request` / `endernet:network_view_update`。事件类各端缓存；服务端只认引擎附加的 `__id__`，下行只发给请求者。

- 请求：`session`、`action`（`select` / `close` / `keepalive`）、`intent`。选择沿用严格的光缆意图载荷；其他动作 `intent=null`。
- 更新：`session`、`revision`、`dimension`、`clear`、`message`、`nodes`、`edges`。
- 节点：`id`、`pos`、`color`、`marker`；边：两个节点 ID。
- 每次选择新 UUID 会话，修订号递增。客户端拒收旧会话、旧修订及错误维度；关闭后迟到的更新不能重新创建模型。

## 渲染与资源

客户端 `network_overlay.py` 对目标几何做差异更新，每帧最多创建／更新 32 个自由模型；已有模型复用，删除立即释放。修改相机不需要 Python 重算顶点。

单骨骼四顶点平面由 `scripts/gen_network_visualizer.py` 生成。顶点着色器在观察空间生成线带和始终朝向镜头的方片；远程长线在投影前处理相机近平面。`EXTRA_VECTOR1` 为颜色，2／3 为相对模型中心的线段端点，4 为宽度及方片标志。包围盒覆盖整段，避免中心离开画面后整条线被剔除。

`endernet_network_overlay` 材质关闭深度测试、深度写入及背面剔除；片元直接输出颜色，不使用世界光照或雾。默认方片 0.36 格、线宽 0.05 格（作者后续要求）。此优先级针对世界渲染，游戏界面仍在其上显示。

生成器同时保存可复现的 16×16 像素检测仪图标与白色纹理，检查命令：

```powershell
python -B scripts/gen_network_visualizer.py --check
python -B scripts/gen_items.py --check
python -B scripts/gen_blocks.py --check
python -B endernet_tests/run_tests.py
```

离线与实机结果、截图及测试边界见 [验收记录](../tasks/validation/network_visualizer/README.md)。
