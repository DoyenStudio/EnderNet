# 持久化 JSON 编码诊断

2026-10-04，通过 Tracy 原生桥接与运行时探针排查订单执行期间的序列化尖峰。

## 观测结果

- 初始 trace `1a106e0b7fa-1-fb671c69`：接口设备保存最大 404.40 ms，其下 `dumps_payload -> json.dumps` 最大 403.59 ms。该尖峰与最慢的订单推进 handler 不在同一时刻。
- 确认再次下发订单后的 trace `1a106fc7bf0-3-196cc6ba`：`json.dumps` 最大 5.71 ms，接口设备保存最大 5.23 ms，驱动器保存最大 8.86 ms。
- 同次运行时探针从重置到关闭，共记录 1,527 次成功编码，平均 0.540 ms，最大约 6 ms；最慢记录为 `endernet.storage_medium/medium:1`，4,188 字节；最大记录 4,193 字节。探针窗口略长于 Tracy 窗口，次数和累计耗时不能直接比较。
- 同次 handler 仍出现 485.04 ms、451.49 ms；订单 `_recheck_if_stale` 最大 343.97 ms。

结论：本次未复现 JSON 的 403.59 ms 尖峰，不能将不同采样的最大值差当作优化收益。正常载荷约 4 KB，本次证据不支持为 JSON 改存档结构。初始异常可能包含线程调度停顿、GC 或其他瞬时因素，尚未确认。

## 探针使用

`server/persistence/repository.py` 中的 `_payload_encoding_probe` 默认是 `None`。需要时，在游戏服务端执行：

```python
import sys
repo = sys.modules['endernet_scripts.endernet.server.persistence.repository']
repo._payload_encoding_probe = repo.PayloadEncodingProbe(threshold_ms=20.0, sample_limit=16)
```

然后开启 Tracy，触发同规模订单。结束时读取并关闭：

```python
result = repo._payload_encoding_probe.diagnostics()
repo._payload_encoding_probe = None
```

仅记录类型、记录 ID、ASCII 编码字节数、开始时间和 JSON 编码墙钟耗时，不保存载荷内容。慢样本最多保留 16 条，另留最慢和最大载荷样本。墙钟耗时包含调度和运行时暂停，无法单独证明 GC；`time.time()` 在游戏中的分辨率约为毫秒，亚毫秒数字不宜过度解读。失败编码保持原异常传播，不计入成功样本。

本次通过 MCP 将探针类与编码函数加载到原模块命名空间，没有重载世界或替换存档仓库对象。复测结束后已关闭运行中的探针。源码只增加诊断，JSON 参数、存档格式与提交语义不变。

## 验证

- 持久化引擎测试：41 项通过，包含编码等价、默认关闭无计时、有限样本与报告隔离、失败传播四项回归。
- Python 2.7 门禁：119 个运行时文件通过。
- 全量：3,876 项，3 个失败、1 个错误，涉及订单提交、空接口名单、订单音效、预览 UI 文本。替换回 HEAD 中原始 `dumps_payload` 后，四项均原样复现。
- 当前没有声称获得性能优化收益；需要再次捕获异常才能进一步归因。
