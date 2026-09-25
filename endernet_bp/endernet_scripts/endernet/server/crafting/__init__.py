# coding=utf-8
"""样板与配方服务（任务 07）。

模块分工：

- `patterns.py` —— 纯数据与编解码。不认识设备，也不持有状态。
- `store.py` —— 提供者状态与产物索引。由启动层持有，随设备生命周期变化。
- `execution.py` —— 执行侧契约与尚未实现的桩（07-03/05/06/07）。

**本包是领域层**：不得 import 引擎（`test_import_boundaries.py` 静态拦着）。
引擎访问一律经注入的契约：容器走 `adapters/containers_engine.py` 的
`ContainerProvider.container_at(node_key)`，容器变更通知走
`adapters/events.py` 翻译进来的位置信号。
"""
