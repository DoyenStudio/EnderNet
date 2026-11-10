# coding=utf-8
"""样板、配方、合成规划与订单执行（任务 07、08、09）。

分包（2026-11 重组）。依赖方向：`orders → planning → patterns`，
`providers → orders、terminals → patterns`；包根只留四个子包共用的 `patterns.py`。

- `patterns.py` —— 纯数据与编解码。不认识设备，也不持有状态。（07）
- `providers/` —— 样板提供者（07）：
  - `store.py` —— 提供者状态与产物索引。由启动层持有，随设备生命周期变化。
  - `execution.py` —— 执行侧契约与尚未实现的桩（07-03/05/06/07）。
- `planning/` —— 合成规划（08）：
  - `planner.py` —— 把目标与数量算成批次、依赖步骤、缺料与复核凭据。
    **它是只读计算**：不扣库存、不预留、不占真实槽位；两份输入（库存快照、
    样板快照）由调用方采集后交进来，因此它不认识 05 与 07 的实现——
    两份快照都会被**深拷贝成纯值**，这样规划结果不会被世界的变化悄悄改动。
  - `preview.py` —— 下单预览：一次只读的分帧规划。
- `orders/` —— 订单与执行（09、10）：
  - `capacity.py` —— 处理器槽位与内存租约，以及订单的未定案旋钮。
    它**就是** `capabilities.ProcessingSlots` 与 `capabilities.Memory` 的实现：
    那两个契约的方法本来就是网络级语义（按执行实例领用/释放），
    因此不为每台处理器再建一个答不了领用问题的对象。
  - `jobs.py` —— 订单、步骤状态机、等待台账与订单集合。**纯领域**：
    不认识网络服务，一条命令都不发。
  - `executor.py` —— 分帧执行器与 `ServiceId.FACTORY` 的服务门面。
    本包**唯一会动真实资源**的那一层。
  - `returns.py`（产物回流与分配，10）、`archive.py`（订单恢复快照）、
    `assembly.py`（装配仓本地执行，07-06）。
- `terminals/` —— 合成终端工作台 `workbench.py`、样板终端的设备侧状态
  `template.py`、两类终端的实体槽原语 `slots.py`（11-04、11-05）。

**订单内的依赖方向是一条线**：`jobs` ← `executor`，`capacity` ← `executor`，
而 `executor` 反向依赖它们只通过构造注入的鸭子类型（规划器、样板索引、
接口状态集合、命令网关、回流接收侧）。因此 08 的规划器与 07 的样板索引
都不需要知道 09 的存在。

**本包是领域层**：不得 import 引擎（`test_import_boundaries.py` 静态拦着）。
引擎访问一律经注入的契约：容器走 `adapters/containers/engine.py` 的
`ContainerProvider.container_at(node_key)`，容器变更通知走
`adapters/events.py` 翻译进来的位置信号。09 还要一扇**设备发起命令**的门
（`port_stage` 必须经主控准入），那一扇同样是注入的（`PortGateway`），
因此本包不 import `devices/bus_io.py`。

门面：只导出包外生产代码在用的名字（见 `__all__`）；包内模块之间写模块路径，不经本门面（`test_package_layout` 守着）。
"""

from .patterns import PatternPolicy, PatternStack, PatternValidationError

__all__ = [
    "PatternPolicy",
    "PatternStack",
    "PatternValidationError",
]
