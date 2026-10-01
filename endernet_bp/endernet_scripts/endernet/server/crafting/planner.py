# coding=utf-8
"""合成规划服务（任务 08-01 至 08-07）。

把「我要 N 个某物」变成**材料需求、配方批次、依赖步骤、缺口清单与预算估算**。
参考 AE2 的规划与执行分离（`me/service/CraftingService.java` 的 plan/execute 两半）。

**规划是只读计算。** 本模块没有任何写入口：不扣库存、不调世界 API、不占真实槽位、
不预留资源。它的产物是一份**可复核的计划**，09 拿到之后在提交时自己复核并预留
（08 文档原文：「09 在提交时复核并预留」）。

## 与 07 / 05 的分工

| 关注点 | 归属 |
| --- | --- |
| 一份配方是什么、怎么编解码 | `patterns.py`（07） |
| 哪台设备上有哪些样板、现在能不能派发 | `store.py`（07） |
| 库存里现在有什么、别人预留了多少 | `storage/service.py`（05） |
| **要几个批次、缺什么、先做哪一步** | 本模块（08） |

本模块**不 import `store.py` 也不 import `storage/`**：两份输入都由调用方
capture 成纯数据快照交进来（`InventorySnapshot` / `PatternSnapshot`），
读的是它们的公开访问器（`StorageService.keys()/query()`、
`PatternStore.output_keys()/candidates()`）。因此 08 可以被离线驱动，
也能被 09 用**别的**后端喂（12 的存档恢复、11 的预览）。

## 快照必须深拷贝成纯值（这条是确定性的头号前提）

`PatternStore.advance()` **每帧**都会对到期的提供者调 `refresh()`，而那会整体
重写 `state.entries` 与 `state.loaded`。规划任务要跨帧推进（08-05），因此只要
快照里留着一个 `PatternProviderState`/`PatternEntry` 的引用，第 7 帧读到的可用性
就与第 1 帧不同——于是 `step(1)` 调用七次与 `step(7)` 调用一次会得到**两份不同的
计划**，而验收第 4 条要求的正是它们逐字节相同。所以 `capture()` 只拷纯值：
位置元组、代次、槽位、指纹、模式、`PatternDefinition`（本身是纯数据且不可变）、
可用性与阻塞原因的字典。

## 三条账本恒等式（守恒的全部内容）

每个资源键 K 只有四个**单调不减**的原始量，其余全是派生量：

    pools(K)       = from_stock + own_output + byproduct_output
    covered(K)     = min(demand(K), pools(K))
    outstanding(K) = demand(K) - covered(K)          # 还要生产多少
    surplus(K)     = pools(K) - covered(K)           # 批次取整多出来的
    from_stock(K)  = min(available(K), max(0, demand(K) - exempt(K)))

**为什么是池子而不是「分配」。** 早先的写法是「规划一个批次时把产物按需分配给
等着它的那些需求」——那就需要在需求后来变大时**反分配**，而漏掉任何一条反分配路径
的表现都是「同一份预计产出被许给了两个分支」（08-03 点名要避免的）。改成
单调池子之后分配这件事根本不存在：每个消费者读的是同一个 `demand`，
`covered`/`outstanding`/`surplus` 由三个池子算出来，因此

- 同一份库存不会被两个分支各用一次（每个键只有一个 `from_stock` 池）；
- 同一份预计产出不会被承诺两次（`own_output`/`byproduct_output` 每条增量只记一次）；
- 批次取整的余量不丢（它自动落在 `surplus` 里），而且**后来**才出现的需求
  会自动用上它——不需要任何人回头改账。

## 环检测必须在**边图**上做，不能在「祖先路径」上做

需求是按资源键**聚合**的（同一个键被两个分支要，只留一条需求）。聚合会丢掉
逐分支的祖先路径，于是「这个输入键在不在当前路径上」这种判据会漏掉真实的环。
反例：目标 `T` 需要 `A` 和 `B`，配方是 `A ← B`、`B ← A`。按路径检查时 `B` 的路径是
`[T, B]`，看不到已经存在的 `A→B` 这条边，于是两条边互相长大，只能等上限截断，
报出来是「超限」而不是「循环」。

因此本模块维护一张 `edges[父键][子键] = 毛需求量` 的边图，插入一条边之前先问
**「子键是不是已经能到达父键」**（`_reachable`）。这样一来边图**按构造是 DAG**，
终止性因此可证；`max_depth`/`max_steps`/`max_candidates` 只是实用安全阀，
不是「靠它才停得下来」。

## 成环的候选是**被排除**，不是被拒绝整单（2026-09-26 规则）

同一问还要**用在选候选之前**：一个键有若干候选，凡是「它的输入会绕回这个键」的
那些**根本不建步骤**（`_screen_inputs`），剩下的候选照常挑一条走。只有一条可用
路线都不剩时，才按**原料不足**拒绝这个键——原因是 `GapCause.RECURSIVE_CANDIDATES`，
环路路径作为缺料诊断写进 `detail`。规则原话：「先满足材料库存需求，再排除当前
祖先链上的递归候选并尝试其他非循环路线；只有库存及可用路线均无法满足时，才按
剩余缺料拒绝下单」——**不能因为一条候选成环就拒绝整单**。

**为什么是预筛而不是「试算—回滚」。** 一条候选会不会绕回这个键，由它**自己的
输入清单**在当前边图上的可达性就能答出来，不需要先提交再撤销。被排除的候选
**根本不碰账本**（不改批次、不记产出、不建边、不记需求），因此不存在残留污染
——这比「回滚到干净状态」更强。而且预筛是**唯一**守得住规则的手段：等边被拒绝
时，那个步骤的批次与预计产出**早已提交**，那时再撤销就等于往一个单调账本里
塞一套回滚机制。

自引用配方（`A ← A + B`，催化剂/回收类）落在这条规则的同一个位置：它的候选会
绕回自己，因此被排除。结论不再是「整单环」——这个键还有别的非循环路线时那条
路线照走，都没有才报缺料。「首版不支持循环生产」这句仍然成立，它现在说的是
**环里的那份产出不作为可用的原料**，而不是「见到环就整单拒绝」。

`_expand_next_input` 里那条「边成环就拒绝」的分支保留为**防御网**：按上面的构造
它在正常路径上不可达（预筛与走查在同一张冻结的边图上做同一个判断，且光标在
同一个节点内走完，两者之间没有别的键被执行），一旦有人改掉这个结构，它保守地
兜住（判做不到）而不是产出一份环状的计划。`counters["cycles_refused"]` 是它
**唯一**的观测点——它不为 0 就说明这套构造有洞，要去修设计而不是改断言。

## 几种失败分开报（08 文档原文要求）

    无解      样板索引里根本没有产出这个键的样板        → GapCause.NO_RECIPE
    无提供者  有样板，但候选全离线/读不到             → GapCause.NO_PROVIDER
    缺料      配方找到了，基础原料不够（**部分计划**） → 账本里的 outstanding
    递归候选  候选都在、但每条都会绕回自己            → GapCause.RECURSIVE_CANDIDATES
    超限 / 批次上限 / 候选窗口用完                    → 各自的 GapCause

`plan.status` 是**总判决**，`plan.gaps` 是**逐键原因**，两者分开：铁镐那个例子
报的是 `MISSING_MATERIALS`（还有两个基础原料要补），而不是「没有配方」。
`NO_RECIPE` 这个总判决只留给**目标本身**没有样板的情形。
`limits["truncated"]` 独立于总判决——**被截断这件事永远不折进判决里**，
少看一眼不等于做不出来。

## 预算耗尽不是失败

`step(budget)` 的预算是「这次最多干多少活」，不是「总共能干多少活」。
没干完就停在 `RUNNING`，下一帧接着走（与 04 的调度器把到期任务留到下一帧同一语义）。
因此**预算大小不影响任何判定**：它只决定这次推进到哪，绝不参与「该做什么」。
`step(0)` 是合法的空操作。

## 返还物（returns）不记账，这是有意的不对称

`outputs` 参与账本（它们是这次派发**会**产出的东西），而 `returns`（空桶之类）
只作为 `expected_returns` 报给 10 的完成判定，**不进池子**。理由：10 文档明写
「鱼已回流但**空桶未到**」是一种真实状态——把它记进池子等于替 10 认定那份回流
一定会到，而计划据此少算一个要生产的数量时，玩家会得到一份永远做不完的计划。

## 单位与数值

数量一律整数（与全局约束 4 一致）：批次取整用整数向上取整 `(n + y - 1) // y`，
**全程不出现除法 `/` 与浮点**（py2 的整除与 py3 不同，py2check 会拦）。
"""

from ...common.identity import DeviceRef, NodeKey
from ...common.protocol import BlockReason, Reason
from ...common.resource_key import (
    MICRO_UNITS_PER_UNIT,
    ResourceKey,
    canonical_tree,
    tree_text,
)
from .patterns import PatternPolicy

#: 计划结构的格式版本。与 `ResourceKey.schema_version`、`PATTERN_FORMAT_VERSION`
#: 同一条纪律：**每份权威数据带自己的版本**。09 的订单、11 的预览与 12 的存档
#: 都会存下计划，因此它的形状变化必须能被认出来。
PLAN_FORMAT_VERSION = 1

#: 一次性求解（`solve`）默认允许的总工作量。防的是「调用方忘了循环 step」时
#: 在一帧里把整张图跑爆；它不是玩法数值，也不进策略的未定清单。
DEFAULT_SOLVE_WORK_LIMIT = 100000


def _canon(key):
    # type: (object) -> str
    """资源键的稳定全序文本。

    **所有需要排序的地方都走它**：`set`/`dict` 的遍历顺序随哈希变化，
    同一份输入两次查询可能给出不同的候选顺序，而那种不确定性在离线测试里
    几乎不会自己暴露。`canonical()` 由身份派生，py2 窄/宽 Unicode 与 py3 一致。
    """
    return key.canonical()


def _name(key):
    # type: (object) -> object
    """资源键的可读名。**py2 上它是 `unicode`**，因此插值它的模板一律写 `u"..."`
    （与 `patterns.py` 同一写法）：非 ASCII 的字节串模板插值 unicode 值会让 py2
    按 ASCII 解码模板并抛 `UnicodeDecodeError`，而 `py2check` 查不出这一类。
    """
    return key.name


# -- 词汇 ------------------------------------------------------------------


class TargetSemantics(object):
    """R11：`amount` 是「目标保有量」还是「要生产多少」。

    两处文档的话各自都对，合起来才需要一条规则：

        概念页：玩家索求一个铁镐 → 系统用库存的材料把它做出来
        任务 08-01：区分生产量与目标保有量，确定已有最终产物是否抵扣

    **替身取 `HOLD`（目标保有量，已有成品抵扣）**，2026-09-25 选定。选它的理由
    两条：一是概念页对订单完成的措辞是「**所需产物数量均已满足**」，那说的是
    「手里有那么多」，不是「这一单新做了那么多」；二是抵扣的这一侧**不会多生产**
    ——多产出的东西要真花掉原料，而少了的那部分玩家一眼就能看见（库里本来就有的
    东西不会因为计划少做一件而消失）。

    另一侧照旧具名可选，两个值都进 `PlanPolicy.unresolved_report()`——
    **替身是替身，不等于定案**：R11 一天没写进上级玩法文档，08/09/11 就一天
    不能宣布验收。
    """
    #: 目标保有量：网络里已经有 N 个就只补差额。**替身取值。**
    HOLD = "hold"
    #: 生产量：要 N 个就规划产 N 个，库里的成品**不抵扣**。
    PRODUCE = "produce"


class CandidateOrder(object):
    """08-04：同一条产物有多个候选时，先看哪一个。

    07 交付说明第六节第 2 条把口径钉死了：`PatternStore.candidates()` 给的是
    **稳定顺序，不是优先级**（排序键是「设备标识、位置、槽位」），
    「哪个更优」是 08 的规则。默认值刻意就是「照 07 给的顺序」——
    任何更聪明的规则都是一条玩法决定，不该由替身悄悄定下来。
    """
    #: 直接用快照的顺序。**替身取值。**
    STABLE = "stable"
    #: 优先选「现有材料就够」的那条配方。**尚未实现**（构造策略时直接报错）。
    LEAST_MISSING = "least_missing"


class AlternativeRecipes(object):
    """08-04 / 08 待定：替代配方的搜索范围。

    同一产物有多块样板时，首版只按 `candidate_order` 挑一条走，
    **不把其余候选当备选方案去展开**（那会把搜索空间变成候选数的乘积）。
    """
    #: 取第一个可用候选的配方；其余候选只记录在步骤里，不展开。**替身取值。**
    FIRST_MATCH = "first_match"
    #: 把多个候选都当备选方案搜索。**尚未实现**（构造策略时直接报错）。
    SEARCH_MULTIPLE = "search_multiple"


class GapCause(object):
    """一个资源键为什么没被满足。**八种，各自对应玩家不同的下一步动作。**

    合并成一个「没有配方」是 08 文档点名要防的：那会让「去装一块样板」
    「去把那台离线接口接回来」「去补基础原料」「少要一点」变成同一句话。
    """
    #: 样板索引里根本没有产出这个键的样板（`store._ensure_index` 里没有它）。
    NO_RECIPE = "no_recipe"
    #: 有样板，但候选提供者现在都不能用（离线、容器读不到、被拆）。
    NO_PROVIDER = "no_provider"
    #: **这个键的每一条可用候选都会把依赖绕回它自己**，因此全部被候选预筛
    #: 排除（2026-09-26 规则）。这是**看完候选之后**的结论，不是「这次没看完」
    #: ——所以它归 `ABSENCE_CAUSES` 而不是 `LIMIT_CAUSES`。环路路径写在
    #: `detail` 里作为缺料诊断：玩家要看的是「哪条路线在打转」。
    #: 旧口径下这个位置报的是整单的 `CYCLE`；那个常量已随规则更新删除。
    RECURSIVE_CANDIDATES = "recursive_candidates"
    #: 依赖深度超过 `max_depth`。
    DEPTH_LIMIT = "depth_limit"
    #: 步骤数超过 `max_steps`。
    NODE_LIMIT = "node_limit"
    #: 可用候选落在候选窗口（`max_candidates`）之外。
    CANDIDATE_LIMIT = "candidate_limit"
    #: 单个步骤的批次数超过 `max_batches_per_step`（一个离谱的数量）。
    BATCH_LIMIT = "batch_limit"
    #: **这个键自己有配方，断的是它的上游。** 收尾的可达量传播发现它的某个输入
    #: 凑不齐，于是这一条链整体执行不了。与 `NO_RECIPE` 分开是必须的：
    #: 报「铁锭没有配方」会让玩家去到处找一份明明已经装好的样板，
    #: 而他要做的是去看 `detail` 里点名的那个上游键。
    MISSING_INPUTS = "missing_inputs"

    #: 因为「没往下看」而产生的四类。**它们不构成「做不出来」的结论**，
    #: 只是「这次没看到那么远」——总判决里它们排在真实原因之后。
    LIMIT_CAUSES = frozenset([DEPTH_LIMIT, NODE_LIMIT, CANDIDATE_LIMIT, BATCH_LIMIT])
    #: 因为「确实没有」而产生的三类。
    ABSENCE_CAUSES = frozenset([NO_RECIPE, NO_PROVIDER, RECURSIVE_CANDIDATES])
    #: 由上游传导而来的那一类（只出现在收尾的可达量传播里，不在 `_gaps` 里）。
    DERIVED_CAUSES = frozenset([MISSING_INPUTS])

    ALL = frozenset(
        [
            NO_RECIPE,
            NO_PROVIDER,
            RECURSIVE_CANDIDATES,
            DEPTH_LIMIT,
            NODE_LIMIT,
            CANDIDATE_LIMIT,
            BATCH_LIMIT,
            MISSING_INPUTS,
        ]
    )


class PlanStatus(object):
    """规划任务的**总判决**。

    `RUNNING` 是任务状态，其余是判决。两者放在同一个字段里是因为调用方要的
    就是一句「现在到哪了」——分成两个字段会让 09 每处都写两遍判断。
    """
    #: 还没算完（预算用完，或刚建好还没推进）。**不是失败。**
    RUNNING = "running"
    #: 算完了，目标能满足。
    COMPLETE = "complete"
    #: 算完了，但基础原料不够——能算的部分照算，缺的列在 `gaps` 里。
    MISSING_MATERIALS = "missing_materials"
    #: **目标本身**在样板索引里没有产出者。与上一项分开：这是「做不出来」。
    NO_RECIPE = "no_recipe"
    #: 有样板但候选全不可用。
    NO_PROVIDER = "no_provider"
    #: **历史状态：规划器不再产出它。** 2026-09-26 的规则把环改成「排除递归候选」
    #: （逐键的 `GapCause.RECURSIVE_CANDIDATES`），总判决落 `MISSING_MATERIALS`。
    #: 保留这个常量是为了 12 从存档里恢复一份**旧计划**时
    #: `CraftPlan.reasons()` 仍给得出原因码，而不是静默返回空。
    CYCLE = "cycle"
    #: 有截断，且没有更具体的失败原因（多半是深度或步骤数触顶）。
    LIMITED = "limited"
    #: 被调用方取消。计划作废，**但任何真实库存都没被动过**。
    CANCELLED = "cancelled"

    TERMINAL = frozenset(
        [
            COMPLETE,
            MISSING_MATERIALS,
            NO_RECIPE,
            NO_PROVIDER,
            CYCLE,
            LIMITED,
            CANCELLED,
        ]
    )


#: `GapCause` -> 命令层的 `Reason`。两套词汇的会合点（与 `store.reason_for_pattern_error`
#: 同一分工）：前者描述「这个资源为什么没有」，后者描述「这条命令为什么被拒」。
#:
#: **`RECURSIVE_CANDIDATES` 映射到 `PLAN_CYCLE` 是有意的**：命令层那一侧
#: 「取消一条做不到的订单」与原因码都是逐订单的，而 `PLAN_CYCLE` 是它唯一
#: 表达「打转了」的码；换个新码会让 09/11 多一个必须认的值。
#: 区别在**作用域**：从 2026-09-26 起拒绝是**逐路线**的（这个键的那几条候选
#: 都在打转），不是逐订单的（整单碰到一个环就死）；订单层的结论去看
#: `plan.status`——它现在是 `MISSING_MATERIALS`，环路径在 `status_detail` 里。
_GAP_REASONS = {
    GapCause.NO_RECIPE: Reason.PLAN_NO_RECIPE,
    GapCause.NO_PROVIDER: Reason.PLAN_NO_PROVIDER,
    GapCause.RECURSIVE_CANDIDATES: Reason.PLAN_CYCLE,
    GapCause.DEPTH_LIMIT: Reason.PLAN_LIMIT_REACHED,
    GapCause.NODE_LIMIT: Reason.PLAN_LIMIT_REACHED,
    GapCause.CANDIDATE_LIMIT: Reason.PLAN_LIMIT_REACHED,
    GapCause.BATCH_LIMIT: Reason.PLAN_LIMIT_REACHED,
}

#: 目标**自己**的缺口原因 -> 总判决。两者当下字面相同，但**这不构成理由**：
#: 写成显式映射，是为了让「改其中一个名字」不会悄悄改掉另一边。
#:
#: **这里不再有 `CYCLE` 一行**，那正是新规则要的行为：目标自己的配方自引用时
#: 落到通用分支，总判决是 `MISSING_MATERIALS`（做不出来是因为料不够，
#: 而不是因为「有个环」），环路径仍写在 `status_detail` 里。
_TARGET_CAUSE_STATUS = {
    GapCause.NO_RECIPE: PlanStatus.NO_RECIPE,
    GapCause.NO_PROVIDER: PlanStatus.NO_PROVIDER,
}

#: `PlanStatus` -> 命令层的 `Reason`。`COMPLETE` 与 `RUNNING` 没有对应码
#: （它们不是失败），因此不在表里。
_STATUS_REASONS = {
    PlanStatus.MISSING_MATERIALS: Reason.PLAN_MISSING_MATERIALS,
    PlanStatus.NO_RECIPE: Reason.PLAN_NO_RECIPE,
    PlanStatus.NO_PROVIDER: Reason.PLAN_NO_PROVIDER,
    #: 规划器**不再产出**这个状态（见 `PlanStatus.CYCLE`），保留仅供 12 恢复旧计划。
    PlanStatus.CYCLE: Reason.PLAN_CYCLE,
    PlanStatus.LIMITED: Reason.PLAN_LIMIT_REACHED,
    PlanStatus.CANCELLED: Reason.PLAN_CANCELLED,
}


# -- 策略 ------------------------------------------------------------------

#: 策略字段到**待定依据**的映射。第二项是决策台账编号或任务编号——
#: 哪个是真正待定的那一份就写哪一个（与 `SchedulerPolicy.UNRESOLVED_FIELDS` 同格式）。
UNRESOLVED_FIELDS = (
    ("target_semantics", "R11", "目标量是生产量还是目标保有量，以及已有成品是否抵扣"),
    ("candidate_order", "08-04", "默认配方优先级（07 只给稳定顺序，优先级是 08 的规则）"),
    ("alternative_recipes", "08-04",
     "替代配方的支持范围。「排除递归候选后试其他候选」**已定**（2026-09-26），"
     "它不是这个字段：那是逐候选的**过滤器**（只问候选自己的输入在当前边图上"
     "会不会绕回本键，**不展开候选子树**，每步仍只提交一条候选）。"
     "这个字段问的是「要不要把多个候选当备选方案**搜索**」——那才是乘积级搜索，仍待定"),
    ("max_depth", "08-04", "依赖深度上限（帧安全阀，不是玩法数值）"),
    ("max_steps", "08-04", "单次计划的步骤数上限（帧安全阀）"),
    ("max_candidates", "08-04", "每个资源检查的候选数上限（帧安全阀）"),
    ("max_batches_per_step", "08-04", "单个步骤的批次数上限（明显的误输入安全阀）"),
    ("byte_rounding", "R07", "根原料不足一个单位物时的字节取整口径；替身取向上取整"),
)


class ByteRounding(object):
    """`required_bytes` 在「根原料微单位不是 50 的整数倍」时怎么取整。

    **这是一条尚未定案的边界**（R07 的流体系列）：1 个物品恰好 50 微单位、
    1 mB 恰好 1 微单位，因此 1 mB 的原料会算出 0.02 字节。
    规则原文：「流体不足 50 mB 的精确计费及字节取整边界仍须随 R07 确认，
    **不擅自舍入**」——所以这里不做算术上的隐式处理，而是把它做成一个具名
    策略字段，让替身取值在诊断里看得见。
    """

    #: 替身：**向上取整**。宁可多收一点内存，也不放一个装不下的订单过去
    #: ——与「宁可说做不到，也不说做得到」同一条取向。
    CEIL = "ceil"
    #: 另一侧：向下取整（少收），已实现但**默认不是它**。
    FLOOR = "floor"

    ALL = frozenset([CEIL, FLOOR])


class PlanPolicy(object):
    """规划侧的未定案旋钮。

    与 `BandwidthPolicy` / `PortPolicy` / `PatternPolicy` 同形：做成具名策略而不是
    常数，因为这几条都还没定案（全局约束 7）。`assert_settled()` **只应在正式
    验收时调用**——开发与离线测试不应调用它。

    **全部八个字段都进 `unresolved_report()`**，并且由 `bootstrap.policy_bundle()`
    一起交给装配检查；少合一份的代价是正式验收时 R11/R04/08-04 漏网。
    """

    __slots__ = (
        "target_semantics",
        "candidate_order",
        "alternative_recipes",
        "max_depth",
        "max_steps",
        "max_candidates",
        "max_batches_per_step",
        "byte_rounding",
    )

    def __init__(
        self,
        target_semantics=TargetSemantics.HOLD,
        candidate_order=CandidateOrder.STABLE,
        alternative_recipes=AlternativeRecipes.FIRST_MATCH,
        max_depth=16,
        max_steps=64,
        max_candidates=8,
        max_batches_per_step=1000000,
        byte_rounding=ByteRounding.CEIL,
    ):
        # type: (str, str, str, int, int, int, int, str) -> None
        if target_semantics not in (TargetSemantics.HOLD, TargetSemantics.PRODUCE):
            raise ValueError(u"未知的目标量口径: %r" % (target_semantics,))
        if candidate_order != CandidateOrder.STABLE:
            # **在装配期就报错，而不是等到某次规划给出一个「更优」的答案。**
            # 优先级是一条玩法规则，替身实现出来的顺序会被当成规则生效
            # （与 `ReturnRule.DERIVE_CONTAINERS` 同一处置）。
            raise ValueError(
                u"配方候选优先级 %r 尚未实现：07 只提供稳定顺序，"
                u"「哪个更优」需要先定案（08-04）。目前只能用 %r"
                % (candidate_order, CandidateOrder.STABLE)
            )
        if alternative_recipes != AlternativeRecipes.FIRST_MATCH:
            raise ValueError(
                u"替代配方搜索 %r 尚未实现：把每个候选都当备选方案展开会让"
                u"搜索空间变成候选数的乘积，需要先定案（08-04）。目前只能用 %r。"
                u"注意「排除会成环的候选、继续试其他候选」是**已定案**的规则，"
                u"它由候选预筛（`PlanJob._screen_inputs`）实现，与这个字段无关"
                % (alternative_recipes, AlternativeRecipes.FIRST_MATCH)
            )
        self.target_semantics = target_semantics
        self.candidate_order = candidate_order
        self.alternative_recipes = alternative_recipes
        #: 四个上限都是**安全阀**，不是玩法数值：它们保证一帧的工作量有界。
        #: 默认值取「正常玩法够用、离奇输入不会把服务端拖住」的量级。
        self.max_depth = max(0, int(max_depth))
        self.max_steps = max(1, int(max_steps))
        self.max_candidates = max(1, int(max_candidates))
        self.max_batches_per_step = max(1, int(max_batches_per_step))
        #: **R07**：`required_bytes` 的取整边界。见 `ByteRounding`。
        #: 字节计费本身（根原料单位物 × 1 B）已由 R04 定案，
        #: **只有「不是整单位物」的取整**还悬着。
        if byte_rounding not in ByteRounding.ALL:
            raise ValueError(u"未知的字节取整口径: %r" % (byte_rounding,))
        self.byte_rounding = byte_rounding

    # -- 判定 ----------------------------------------------------------

    def root_exempt(self, amount):
        # type: (int) -> int
        """根需求里**不允许被库存抵扣**的那部分。

        这是 R11 两种口径的**唯一**差别：`HOLD` 下库存一律参与抵扣（返回 0，
        目标量是「保有」），`PRODUCE` 下根目标那一条需求整份不许抵扣——
        否则「要 3 个」会被库里的 3 个悄悄吃掉，两种口径退化成同一种行为。

        **它只可能作用在根需求上**，这一点是结构性的、不是约定：
        一个步骤要消耗目标键，就意味着存在一条边 `某步 -> 目标`，而那个步骤
        本身是从目标出发可达的（每个步骤的需求都最终源自根），于是
        `目标 -> … -> 某步 -> 目标` 成环——环在插入边时就被拒绝了。
        所以「嵌套需求上的目标」这种形状在无环的计划里不存在，
        `HOLD` 与 `PRODUCE` 的差别也因此完全是「根那一条需不需要自己生产」。
        用例 `test_no_step_can_consume_the_target` 钉着这条。
        """
        if self.target_semantics == TargetSemantics.PRODUCE:
            return int(amount)
        return 0

    def pick_candidate(self, usable):
        # type: (list) -> object
        """从可用候选里挑一个。替身口径就是取第一个（快照顺序即 07 的稳定顺序）。

        **会成环的候选根本送不到这里**：`PlanJob._select` 先用候选预筛
        （`_screen_inputs`）把「输入会绕回本键」的那些摘掉，剩下的才进这个函数。
        因此这里只需回答「哪个更优」，不必再自查递归性——两处各判一次的话，
        两边判据一旦分叉就会出现「预筛放行、走查拒绝」的已提交步骤。
        """
        return usable[0]

    # -- 未定案守卫 ----------------------------------------------------

    def unresolved_report(self):
        # type: () -> list
        return [
            {
                "field": "plan." + field,
                "rule": rule,
                "value": getattr(self, field, None),
                "note": note,
            }
            for field, rule, note in UNRESOLVED_FIELDS
        ]

    def assert_settled(self):
        # type: () -> None
        """正式验收用：仍有未定项就报错。开发与离线测试不应调用它。"""
        pending = self.unresolved_report()
        if pending:
            raise AssertionError(
                u"规划策略仍有 %d 项未定案（R11/R04/08-04），不能作为正式玩法交付：\n  "
                % (len(pending),)
                + "\n  ".join(
                    u"%s（当前值 %r）" % (item["field"], item["value"])
                    for item in pending
                )
            )

    def describe(self):
        # type: () -> dict
        return {
            "target_semantics": self.target_semantics,
            "candidate_order": self.candidate_order,
            "alternative_recipes": self.alternative_recipes,
            "max_depth": self.max_depth,
            "max_steps": self.max_steps,
            "max_candidates": self.max_candidates,
            "max_batches_per_step": self.max_batches_per_step,
            "byte_rounding": self.byte_rounding,
            "unresolved": self.unresolved_report(),
        }

    def __repr__(self):
        return "PlanPolicy(%s, %s)" % (self.target_semantics, self.candidate_order)


# -- 快照 ------------------------------------------------------------------


class InventorySnapshot(object):
    """库存的**不可变纯数据**视图。

    只回答三个数：存了多少、别人预留了多少、因此我能用多少。
    `available = stored - reserved` 是 05 的口径（「普通取货看不到他人预留物品」），
    因此规划不会把别人订下的料算成自己的。

    **它不持有 `StorageService`。** 快照一旦建立就与世界脱钩，规划期间库存怎么变
    都与它无关；「变了怎么办」由提交前复核回答（`CraftPlan.revalidate`）。
    """

    __slots__ = ("version", "topology_version", "_rows")

    def __init__(self, rows=None, version=0, topology_version=None):
        # type: (dict, object, object) -> None
        #: `ResourceKey -> (stored, reserved, available)`。
        self._rows = dict(rows or {})
        #: 来源的库存索引版本。**只是提示**：复核时按量比，不按版本（见 `revalidate`）。
        self.version = version
        #: 建立快照时的拓扑版本。`None` = 没捕获（复核时按「未检查」报，不是「通过」）。
        self.topology_version = topology_version

    @classmethod
    def capture(cls, storage, topology_version=None):
        # type: (object, object) -> InventorySnapshot
        """从存储服务读一份快照。`storage` 为 `None` 时得到空快照。

        走 `keys()` + `query()` 这两个**公开**查询入口，而不是 `snapshot()`：
        后者的 `entries[i]["key"]` 是 `to_dict()` 的线上形状，再 `from_dict` 回来
        会绕一趟身份归一化（附魔表排序等），而快照要的是**当前这份身份本身**。
        """
        if storage is None:
            return cls({}, 0, topology_version)
        rows = {}
        for key in storage.keys():
            entry = storage.query(key)
            rows[key] = (
                int(entry["stored"]),
                int(entry["reserved"]),
                int(entry["available"]),
            )
        return cls(
            rows,
            getattr(storage, "index_version", 0),
            topology_version,
        )

    @classmethod
    def from_payload(cls, payload, topology_version=None):
        # type: (dict, object) -> InventorySnapshot
        """从纯字典建快照（12 的存档恢复、11 的预览、离线夹具走这条）。

        接受 `StorageService.snapshot()` 的形状：`keys` 与 `entries` 两条等长列表，
        `entries[i]["key"]` 可以是资源键对象，也可以是它的 `to_dict()` 形状。
        """
        if not isinstance(payload, dict):
            raise TypeError(u"库存快照必须是字典，得到 %r" % (type(payload),))
        rows = {}
        raw_keys = payload.get("keys") or []
        entries = payload.get("entries") or []
        for index, key in enumerate(raw_keys):
            if not isinstance(key, ResourceKey):
                key = ResourceKey.from_dict(key)
            if index < len(entries):
                entry = entries[index]
                stored = int(entry.get("stored", 0))
                reserved = int(entry.get("reserved", 0))
                available = entry.get("available")
                if available is None:
                    available = stored - reserved
            else:
                stored = reserved = available = 0
            rows[key] = (stored, reserved, int(available))
        return cls(rows, payload.get("version", 0), topology_version)

    # -- 查询 ----------------------------------------------------------

    def has(self, key):
        # type: (object) -> bool
        return key in self._rows

    def row(self, key):
        # type: (object) -> tuple
        """`(stored, reserved, available)`。**不存在的键是三个 0**，不是 `None`：
        规划要的是「有多少可以用」，而「这个键从来没出现过」与「它现在归零」
        对算料是同一件事。区分它们属于 05 的诊断（`get(...) is None` 那条纪律）。"""
        return self._rows.get(key, (0, 0, 0))

    def stored(self, key):
        # type: (object) -> int
        return self.row(key)[0]

    def reserved(self, key):
        # type: (object) -> int
        return self.row(key)[1]

    def available(self, key):
        # type: (object) -> int
        return self.row(key)[2]

    def keys(self):
        # type: () -> tuple
        """按资源身份排序的键。**不用 `dict` 的遍历顺序**（它随哈希变化）。"""
        return tuple(sorted(self._rows, key=_canon))

    def to_dict(self):
        # type: () -> dict
        return {
            "version": self.version,
            "topology_version": self.topology_version,
            "rows": [
                {
                    "key": _name(key),
                    "canonical": _canon(key),
                    "stored": self._rows[key][0],
                    "reserved": self._rows[key][1],
                    "available": self._rows[key][2],
                }
                for key in self.keys()
            ],
        }

    def digest(self):
        # type: () -> str
        """内容指纹。复核时与 `version` 一起给出，但**判据是逐键的量**。"""
        return tree_text(canonical_tree(self.to_dict()["rows"]))

    def __len__(self):
        return len(self._rows)

    def __repr__(self):
        return u"InventorySnapshot(%d 个键, v%s)" % (len(self._rows), self.version)


class PatternCandidate(object):
    """一份样板在索引里的一席之地。**纯数据，而且规划期间不会被改动。**

    字段名与 `execution.candidate_view()` 对齐（07-07 交给 08 的那份契约）。

    **「这一批用了哪个候选」不在候选上标**（曾经有一个 `selected` 字段，后来删了）：
    候选属于**快照**，而快照是可复用的——一份快照上的标记会被别的规划任务看到，
    而且 `PatternSnapshot.digest()` 包含了候选，于是「规划一次」会把快照的指纹
    改掉。选择的结果记在**步骤**上（`CraftStep.chosen` 与计划里的
    `provider` 字段），那才是它该在的地方。
    """

    __slots__ = (
        "ordinal",
        "node_key",
        "generation",
        "device_id",
        "slot",
        "digest",
        "mode",
        "definition",
        "available",
        "blocker",
    )

    def __init__(
        self,
        ordinal,
        node_key,
        generation,
        device_id,
        slot,
        digest,
        mode,
        definition,
        available=True,
        blocker=None,
    ):
        # type: (int, tuple, int, str, int, str, str, object, bool, dict) -> None
        self.ordinal = int(ordinal)
        #: `(dimension, x, y, z)`——与 `candidate_view` 同形，且它是**纯元组**。
        self.node_key = tuple(node_key)
        self.generation = int(generation)
        self.device_id = device_id
        self.slot = int(slot)
        #: 样板版本（内容指纹）。计划据此在派发前复核（07-04 的三元组）。
        self.digest = digest
        self.mode = mode
        #: `PatternDefinition`——**纯数据且不可变**，因此直接引用不再拷一份。
        self.definition = definition
        self.available = bool(available)
        self.blocker = blocker

    def ref(self):
        # type: () -> DeviceRef
        """重建设备实例引用（带代次），供 09 拿去调 `PatternStore.check_dispatch`。"""
        return DeviceRef(NodeKey.from_tuple(self.node_key[0], self.node_key[1:]), self.generation)

    def triple(self):
        # type: () -> tuple
        """07 要求计划记住的那个三元组 `(设备实例引用, 槽位, 指纹)`。"""
        return (self.ref(), self.slot, self.digest)

    def to_dict(self):
        # type: () -> dict
        return {
            "ordinal": self.ordinal,
            "node_key": self.node_key,
            "device_id": self.device_id,
            "slot": self.slot,
            "digest": self.digest,
            "mode": self.mode,
            "summary": self.definition.summary(),
            "available": self.available,
            "blocker": self.blocker,
        }

    def __repr__(self):
        return u"PatternCandidate(%s @ %r 槽%d)" % (self.device_id, self.node_key, self.slot)


class PatternSnapshot(object):
    """样板索引的**不可变纯数据**视图：产物 -> 候选提供者。

    见模块 docstring 的深拷贝那一段——**这是确定性最容易破的一处**。
    """

    __slots__ = ("topology_version", "_index")

    def __init__(self, index=None, topology_version=None):
        # type: (dict, object) -> None
        #: `ResourceKey -> tuple(PatternCandidate)`，**保持 07 给的顺序**。
        self._index = dict(index or {})
        self.topology_version = topology_version

    @classmethod
    def capture(cls, store, topology_version=None):
        # type: (object, object) -> PatternSnapshot
        """从 `PatternStore` 读一份快照。`store` 为 `None` 时得到空快照。

        走 07 交付说明第九节点名交给 08 的两个入口：`output_keys()` 与
        `candidates(key, include_unavailable=True)`。**带上不可用的候选**是必须的：
        只拿可用的会让「这个产物有样板但都在离线」与「根本没有样板」变成同一个答案，
        而那正是 08 文档要求分开报的两件事。

        **过旧的样板不进索引**（07 的 `_ensure_index` 就把它排除了），
        因此对规划而言它与「没有样板」同形。要查过时的样板走
        `debug_query("pattern")`（那里报 `stale_slots`）——这条限制写在交付说明里。
        """
        if store is None:
            return cls({}, topology_version)
        index = {}
        for key in store.output_keys():
            rows = []
            for ordinal, item in enumerate(
                store.candidates(key, include_unavailable=True)
            ):
                provider = item["provider"]
                entry = item["entry"]
                blocker = item.get("blocker")
                rows.append(
                    PatternCandidate(
                        ordinal,
                        provider.node_key.as_key(),
                        provider.ref.generation,
                        provider.device_id,
                        entry.slot,
                        entry.digest(),
                        entry.definition.mode,
                        entry.definition,
                        available=bool(item.get("available")),
                        blocker=blocker.to_dict() if blocker is not None else None,
                    )
                )
            index[key] = tuple(rows)
        return cls(index, topology_version)

    # -- 查询 ----------------------------------------------------------

    def candidates(self, key):
        # type: (object) -> tuple
        """某个产物的候选。**没有就是空元组**——与「有候选但都不可用」不同，
        调用方按空/非空分流，两件事因此不可能被读成同一件。"""
        return self._index.get(key, ())

    def covers(self, key):
        # type: (object) -> bool
        """索引里有没有产出这个键的样板（不看可用性）。"""
        return bool(self._index.get(key))

    def output_keys(self):
        # type: () -> tuple
        return tuple(sorted(self._index, key=_canon))

    def candidate_count(self):
        # type: () -> int
        return sum(len(rows) for rows in self._index.values())

    def to_dict(self):
        # type: () -> dict
        return {
            "topology_version": self.topology_version,
            "outputs": [
                {
                    "key": _name(key),
                    "canonical": _canon(key),
                    "candidates": [row.to_dict() for row in self._index[key]],
                }
                for key in self.output_keys()
            ],
        }

    def digest(self):
        # type: () -> str
        return tree_text(canonical_tree(self.to_dict()["outputs"]))

    def __len__(self):
        return len(self._index)

    def __repr__(self):
        return u"PatternSnapshot(%d 种产物, %d 个候选)" % (
            len(self._index),
            self.candidate_count(),
        )


# -- 账本 ------------------------------------------------------------------


class LedgerRow(object):
    """一个资源键的账。**原始量单调不减，其余全是派生量**（见模块 docstring）。

    没有「分配」这一步，因此也没有「反分配」：`covered`/`outstanding`/`surplus`
    每次读的时候由池子算出来，任何时刻都自洽。
    """

    __slots__ = (
        "key",
        "demand",
        "exempt",
        "from_stock",
        "own_output",
        "byproduct_output",
        "depth",
    )

    def __init__(self, key):
        # type: (object) -> None
        self.key = key
        #: 全网对这个键的总需求（根贡献 + 所有入边的毛量之和）。
        self.demand = 0
        #: 其中**不允许**被库存抵扣的部分（R11 的 `PRODUCE` 根目标）。
        self.exempt = 0
        #: 从库存划过来的量。`min(available, max(0, demand - exempt))`。
        self.from_stock = 0
        #: 本次计划里，**为这个键安排的生产**（把批次数乘上每批产量）。
        self.own_output = 0
        #: 本次计划里，别的配方把它当**副产物**产出的量。
        self.byproduct_output = 0
        #: 见到它的最浅深度（深度越浅越接近目标，用于排序与上限判定）。
        #: `None` = **这个键只被当副产物记过、还没有过需求**（与「深度 0」
        #: 是两回事：0 是根目标自己）。用 `None` 而不是 0 当哨兵，是因为
        #: 0 是一个真实取值，拿它当哨兵会把根的深度判成「没设过」。
        self.depth = None

    # -- 派生量 --------------------------------------------------------

    def pools(self):
        # type: () -> int
        return self.from_stock + self.own_output + self.byproduct_output

    def covered(self):
        # type: () -> int
        return min(self.demand, self.pools())

    def outstanding(self):
        # type: () -> int
        """还差多少。**这是「缺料」的真身**：> 0 且没有配方就是基础原料不够。"""
        return self.demand - self.covered()

    def surplus(self):
        # type: () -> int
        """批次取整多出来的量（08 验收：「产出大于目标时正确保留批次余量」）。"""
        return self.pools() - self.covered()

    def to_dict(self, snapshot=None):
        # type: (object) -> dict
        stored = reserved = available = 0
        if snapshot is not None:
            stored, reserved, available = snapshot.row(self.key)
        return {
            "key": _name(self.key),
            "canonical": _canon(self.key),
            "kind": self.key.kind,
            "depth": self.depth if self.depth is not None else 0,
            "demand": self.demand,
            "exempt": self.exempt,
            "from_stock": self.from_stock,
            "own_output": self.own_output,
            "byproduct_output": self.byproduct_output,
            "covered": self.covered(),
            "outstanding": self.outstanding(),
            "surplus": self.surplus(),
            # 库存侧的原值一并给出：否则读的人没法分辨「缺料」是因为
            # 库里没有，还是因为那份料被别人预留了。
            "stored": stored,
            "reserved": reserved,
            "available": available,
        }

    def __repr__(self):
        return u"LedgerRow(%s 需求%d 覆盖%d)" % (_name(self.key), self.demand, self.covered())


# -- 步骤与缺口 ------------------------------------------------------------


class CraftStep(object):
    """一个依赖步骤：为某个键安排多少个批次、用哪一块样板。

    `batches` 在规划过程中**只增不减**：需求会因为别的分支而变大，
    而缩小批次会把已经记进账本的产出收回去——那正是「同一份预计产出被
    承诺两次」的另一面。因此这里只做加法，账本因此永远自洽。
    """

    __slots__ = (
        "key",
        "depth",
        "definition",
        "chosen",
        "candidates",
        "batches",
        "blocked",
        "blocked_by",
        "required_inputs",
        "external_inputs",
        "runnable",
    )

    def __init__(self, key, depth, chosen, candidates):
        # type: (object, int, object, tuple) -> None
        self.key = key
        self.depth = int(depth)
        self.definition = chosen.definition
        self.chosen = chosen
        self.candidates = tuple(candidates)
        self.batches = 0
        #: 这个步骤有没有因为环而接不上（它的某个输入被判成环）。
        self.blocked = False
        self.blocked_by = []
        #: 本次派发**真要向网络要**的输入（R09 的 `unlimited` 已被策略过滤掉）。
        self.required_inputs = ()
        #: 被策略判为「外部机器自理」的输入：不产生需求，但要报出来
        #: （09 据此知道不用备料，玩家知道那台机器自己供）。
        self.external_inputs = ()
        #: 收尾时按「输入是否真的凑得齐」标出来。**唯一的写者**是 `_finalize`。
        self.runnable = True

    # -- 派生量 --------------------------------------------------------

    def yield_per_batch(self):
        # type: () -> int
        """这个步骤每批次产出多少个**本键**。同一键在 `outputs` 里出现多行时相加。

        （`outputs = [(A,2),(A,3)]` 是合法样板，每批产 5 个 A。把重复行当成
        两条各 2、各 3 的独立产出去记账，会让 `own_output` 少算一半。）
        """
        return _yield_for(self.definition, self.key)

    def output_pairs(self):
        # type: () -> tuple
        """本步骤每批产出的**全部**键与数量，同一键相加，按身份排序。

        含本键自己（它是这个步骤存在的理由），也含副产物——收尾时的
        可达量计算要用完整的一份。
        """
        totals = {}
        for stack in self.definition.outputs:
            totals[stack.key] = totals.get(stack.key, 0) + stack.count
        return tuple((key, totals[key]) for key in sorted(totals, key=_canon))

    def byproduct_pairs(self):
        # type: () -> tuple
        """除本键以外的产出。它们进账本的 `byproduct_output` 池。"""
        return tuple(
            (key, count) for key, count in self.output_pairs() if key != self.key
        )

    def input_pairs(self):
        # type: () -> tuple
        """本次派发要备的输入，**按资源键合并**：`((键, 每批数量), ...)`，按身份排序。

        **必须合并，这不是优化。** 一份合成样板把「3 个铁锭」写成三条带槽位的
        输入（`patterns._check_layout` 要求有序输入每条占一个槽位、且槽位不重复），
        工厂样板也可能把同一种料写两遍——两种都是**合法样板**。
        逐条算需求时，第二条算出的增量是负的（第一条已经把那部分记进去了），
        于是账本只记下一条的量：`steps[].inputs` 说要 5 个、`materials[].demand`
        只写 3 个，而计划照旧报 `COMPLETE`。09 按 `demand`/`claims` 预留就会少扣，
        按 `steps[].inputs` 派发又会多要——**同一份计划里的两个数互相矛盾**。

        合并之后，每条输入在每个步骤里只有一条，需求与派发不可能分叉。

        **候选预筛必须走同一个函数**（`_merge_input_stacks`）：预筛问的是
        「这条候选展开后会不会绕回本键」，它问的输入集合一旦与真正展开的那个
        集合不一致，就会有输入在**已经提交之后**才被判成环，而那时没有回退。
        """
        return _merge_input_stacks(self.required_inputs)

    def input_totals(self):
        # type: () -> tuple
        """本次派发要备的输入合计：`(键, 每批, 合计)`，按身份排序。"""
        return tuple(
            (key, per_batch, per_batch * self.batches)
            for key, per_batch in self.input_pairs()
        )

    def external_totals(self):
        # type: () -> tuple
        """外部自理的输入合计，形状与 `input_totals` 同。"""
        totals = {}
        for stack in self.external_inputs:
            per_batch, total = totals.get(stack.key, (0, 0))
            totals[stack.key] = (per_batch + stack.count, total + stack.count * self.batches)
        return tuple(
            (key, totals[key][0], totals[key][1]) for key in sorted(totals, key=_canon)
        )

    def return_totals(self):
        # type: () -> tuple
        """应当期待回流的返还物合计。**只报不记账**（见模块 docstring）。"""
        totals = {}
        for stack in self.definition.returns:
            totals[stack.key] = totals.get(stack.key, 0) + stack.count
        return tuple(
            (key, totals[key], totals[key] * self.batches)
            for key in sorted(totals, key=_canon)
        )

    def to_dict(self):
        # type: () -> dict
        return {
            "key": _name(self.key),
            "canonical": _canon(self.key),
            "depth": self.depth,
            "mode": self.definition.mode,
            "batches": self.batches,
            "yield_per_batch": self.yield_per_batch(),
            "runnable": self.runnable,
            "blocked": self.blocked,
            "blocked_by": [_name(key) for key in self.blocked_by],
            "provider": self.chosen.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
            "inputs": [
                {"key": _name(key), "canonical": _canon(key), "per_batch": per, "total": total}
                for key, per, total in self.input_totals()
            ],
            "outputs": [
                {"key": _name(key), "canonical": _canon(key), "per_batch": per, "total": per * self.batches}
                for key, per in self.output_pairs()
            ],
            "external_inputs": [
                {"key": _name(key), "canonical": _canon(key), "per_batch": per, "total": total}
                for key, per, total in self.external_totals()
            ],
            "returns": [
                {"key": _name(key), "canonical": _canon(key), "per_batch": per, "total": total}
                for key, per, total in self.return_totals()
            ],
        }

    def __repr__(self):
        return u"CraftStep(%s x%d 批 @深度%d)" % (_name(self.key), self.batches, self.depth)


class GapRecord(object):
    """一个资源键为什么没被满足。

    **缺口数量刻意不存。** 它随时能从账本读出来，存一份就有两处会不同步——
    而不同步的表现是「报告说缺 3 个，账本说缺 5 个」，读的人只能猜哪个对。
    """

    __slots__ = ("key", "cause", "detail", "depth")

    def __init__(self, key, cause, detail, depth):
        # type: (object, str, object, int) -> None
        self.key = key
        self.cause = cause
        self.detail = detail
        self.depth = int(depth)

    def reason(self):
        # type: () -> str
        return _GAP_REASONS.get(self.cause, Reason.PLAN_LIMIT_REACHED)

    def to_dict(self):
        # type: () -> dict
        """**不含缺口数量**：那一个数在组装计划时从账本现读（见类 docstring）。"""
        return {
            "key": _name(self.key),
            "canonical": _canon(self.key),
            "kind": self.key.kind,
            "cause": self.cause,
            "reason": self.reason(),
            "detail": self.detail,
            "depth": self.depth,
            "is_limit": self.cause in GapCause.LIMIT_CAUSES,
        }

    def __repr__(self):
        # 模板虽然是纯 ASCII（py2 上解 ASCII 不会炸），但插值的是 unicode 的名字，
        # 与文件里那条规则保持一致：**插值可能为 unicode 的值的模板一律写 u**。
        return u"GapRecord(%s, %s)" % (_name(self.key), self.cause)


# -- 计划 ------------------------------------------------------------------


class PlanSeal(object):
    """计划的**依据凭据**：09 提交前拿它复核（08-07）。

    它同时记下三样东西，因为它们是三类不同的失效：

    - `inventory_version` / `inventory_digest` / `claims`：库存变了没有。
      **判据是逐键的量**（`claims`），版本号只是提示——版本变了但计划声明过的
      那几个键的量没少，计划照样成立（无关物品的进出不该让计划作废）。
    - `topology_version`：归属可能变了（拆线、合并、主控换人）。
    - `dispatch`：每个步骤用的是哪一块样板的哪个版本，逐条交给 07 的
      `check_dispatch` 复核。
    """

    __slots__ = (
        "plan_digest",
        "inventory_version",
        "inventory_digest",
        "topology_version",
        "dispatch",
        "claims",
    )

    def __init__(
        self,
        plan_digest,
        inventory_version,
        inventory_digest,
        topology_version,
        dispatch,
        claims,
    ):
        # type: (str, object, str, object, tuple, tuple) -> None
        self.plan_digest = plan_digest
        self.inventory_version = inventory_version
        self.inventory_digest = inventory_digest
        self.topology_version = topology_version
        self.dispatch = tuple(dispatch)
        self.claims = tuple(claims)

    def to_dict(self):
        # type: () -> dict
        return {
            "plan_digest": self.plan_digest,
            "inventory_version": self.inventory_version,
            "inventory_digest": self.inventory_digest,
            "topology_version": self.topology_version,
            "dispatch": [
                {
                    "step": item["step"],
                    "key": _name(item["key"]),
                    # 复核要按规范文本回查材料清单，因此这里必须带上它——
                    # 只有名字的话，两种写法不同的同一个键会对不上。
                    "canonical": _canon(item["key"]),
                    "node_key": item["node_key"],
                    "generation": item["generation"],
                    "slot": item["slot"],
                    "digest": item["digest"],
                    "mode": item["mode"],
                    "batches": item["batches"],
                    "runnable": item["runnable"],
                }
                for item in self.dispatch
            ],
            "claims": [
                {"key": _name(key), "canonical": _canon(key), "count": count}
                for key, count in self.claims
            ],
        }

    def __repr__(self):
        return u"PlanSeal(%s, %d 条派发)" % (
            (self.plan_digest or "")[:16],
            len(self.dispatch),
        )


class RevalidationReport(object):
    """提交前复核的结果。**三条腿各自出结论，绝不合并成一句「过期了」。**

    `ok` 的口径是**保守的**：它要求三条腿都检查过且都通过
    （`ok = not reasons and all(checked.values())`）。没传库存快照、没传
    `check_dispatch`、没捕获过拓扑版本，都会让 `ok` 为假——**未检查不等于通过**。
    只想看某一条腿的调用方直接读 `stale_stock` / `stale_dispatch` / `topology_changed`。
    """

    __slots__ = ("reasons", "checked", "stale_stock", "stale_dispatch", "topology_changed")

    def __init__(self, checked, stale_stock, stale_dispatch, topology_changed, reasons):
        # type: (dict, tuple, tuple, bool, list) -> None
        self.checked = checked
        self.stale_stock = tuple(stale_stock)
        self.stale_dispatch = tuple(stale_dispatch)
        self.topology_changed = bool(topology_changed)
        self.reasons = list(reasons)

    def ok(self):
        # type: () -> bool
        return not self.reasons and all(self.checked.values())

    def fully_checked(self):
        # type: () -> bool
        return all(self.checked.values())

    def codes(self):
        # type: () -> list
        return [reason.code for reason in self.reasons]

    def to_dict(self):
        # type: () -> dict
        return {
            "ok": self.ok(),
            "fully_checked": self.fully_checked(),
            "checked": dict(self.checked),
            "stale_stock": [
                {"canonical": canonical, "claimed": claimed, "available": available}
                for canonical, claimed, available in self.stale_stock
            ],
            "stale_dispatch": [
                {
                    "step": step,
                    "canonical": canonical,
                    "code": reason.code,
                    "detail": reason.detail,
                }
                for step, canonical, reason in self.stale_dispatch
            ],
            "topology_changed": self.topology_changed,
            "reasons": [reason.to_dict() for reason in self.reasons],
        }

    def __repr__(self):
        return "RevalidationReport(ok=%r, %s)" % (self.ok(), self.codes())


class CraftPlan(object):
    """一份规划结果。**纯数据**：`to_dict()` 可以直接 `json.dumps`
    （08-07 的「不向客户端暴露引擎对象」）。"""

    __slots__ = ("content", "_digest")

    def __init__(self, content, seal):
        # type: (dict, PlanSeal) -> None
        self.content = content
        self.content["seal"] = seal.to_dict()
        self._digest = seal.plan_digest

    # -- 基本字段 ------------------------------------------------------

    @property
    def version(self):
        return self.content["version"]

    @property
    def status(self):
        return self.content["status"]

    @property
    def target(self):
        return self.content["target"]["canonical"]

    @property
    def amount(self):
        return self.content["amount"]

    @property
    def steps(self):
        return self.content["steps"]

    @property
    def gaps(self):
        return self.content["gaps"]

    @property
    def materials(self):
        return self.content["materials"]

    @property
    def blocked_keys(self):
        return self.content["blocked_keys"]

    @property
    def limits(self):
        return self.content["limits"]

    @property
    def estimates(self):
        return self.content["estimates"]

    @property
    def required_bytes(self):
        """这份计划要占的内存**字节数**（R04 已定案的口径）。

        **这是 09 准入时唯一该用的那个数**，不是 `estimates` 里的任何一项：
        规则原文「接受后持有至完成或玩家手动中止」，因此它必须是一个
        一次算定、此后不因进度或活跃配方数变化的量。`estimates` 明写着
        `authoritative: False`，那是搜索用的估计。
        """
        return int(self.content["required_bytes"])

    @property
    def total_recipe_tasks(self):
        """配方任务总数（各步骤批次数之和）。**进度的分母**，不是步骤数。"""
        return int(self.content["total_recipe_tasks"])

    @property
    def seal(self):
        return self.content["seal"]

    def digest(self):
        # type: () -> str
        """计划内容的指纹（**不含依据**：库存与拓扑版本在 `seal` 里）。

        这样「同一份计划、不同的库存版本」不会被算成两份计划——09 的去重与
        11 的「预览有没有变」要的正是这个语义。
        """
        return self._digest

    def is_satisfied(self):
        # type: () -> bool
        """目标能不能满足。**与「有没有步骤」不是一回事**：
        `HOLD` 口径下库里已经够了，就会是一份零步骤但 `COMPLETE` 的计划。"""
        return self.status == PlanStatus.COMPLETE

    def dispatch_triples(self):
        # type: () -> list
        """`[(设备实例引用, 槽位, 指纹, 模式)]`，交给 07 的 `check_dispatch` 逐个复核。"""
        out = []
        for item in self.content["seal"]["dispatch"]:
            node = NodeKey.from_tuple(item["node_key"][0], tuple(item["node_key"][1:]))
            out.append(
                (
                    DeviceRef(node, item["generation"]),
                    item["slot"],
                    item["digest"],
                    item["mode"],
                )
            )
        return out

    def reasons(self):
        # type: () -> list
        """总判决 -> 命令原因码。`COMPLETE` / `RUNNING` 返回空表。"""
        code = _STATUS_REASONS.get(self.status)
        if code is None:
            return []
        detail = self.content.get("status_detail") or ""
        return [BlockReason(code, detail)]

    def execution_order(self):
        # type: () -> list
        """按依赖顺序排列的步骤下标：**子键（依赖）排在父键（消费者）之前**。

        这是由 `execution_ranks()` 保证的**合法拓扑序**，而不是「按 `depth`
        从大到小」——`depth` 会被更浅的路径调小，因此它不满足拓扑序
        （共享子需求的形状里就会失序，见那边的说明）。
        09 按这个顺序派发，原料总是先于消费者就位。
        """
        return [step["index"] for step in self.content["steps"]]

    def _label(self, canonical):
        # type: (str) -> object
        """规范文本 -> 给人看的名字。**只查计划自己记着的那份材料清单。**

        复核报告要能在「那个键已经从库存里消失」时照样写得出来，
        因此名字的来源必须是计划自己（它当初算的时候就记下了），
        而不是现场那份可能已经没有这个键的快照。查不到就退回规范文本，
        **绝不抛异常**——复核本身失败会让提交端失去唯一的判据。
        """
        for row in self.content["materials"]:
            if row["canonical"] == canonical:
                return row["key"]
        return canonical

    # -- 复核 ----------------------------------------------------------

    def revalidate(self, check_dispatch=None, inventory_snapshot=None, topology_version=None):
        # type: (object, object, object) -> RevalidationReport
        """提交前复核（08-07 交给 09 的那一份条件）。

        **它只回答「依据还成立吗」，不替 09 做任何处置**：不重规划、不扣料、
        不释放预留。09 拿到 `ok()` 为假时的正确动作是「用新快照重规划，或拒绝
        并报告」——**绝不能拿着旧计划去扣料**（08 验收第 5 条）。

        `check_dispatch` 是 07 的 `PatternStore.check_dispatch` 的鸭子类型，
        签名 `(ref, slot, digest, expected_mode) -> (entry, blocker)`；
        注入它而不是 import `store.py`，是为了让 08 不依赖 07 的实现。
        """
        checked = {"inventory": False, "patterns": False, "topology": False}
        reasons = []
        stale_stock = []
        stale_dispatch = []

        if inventory_snapshot is not None:
            checked["inventory"] = True
            # **逐键比量，不比版本。** 版本变了但计划声明过的量没少时计划照样成立，
            # 而无关物品的进出更不该让一份计划作废——那会让 09 反复重规划。
            for claim in self.content["seal"]["claims"]:
                canonical, claimed = claim["canonical"], claim["count"]
                if claimed <= 0:
                    continue
                # **键整个消失了要按「现有 0」处理，不能当成「没这条」跳过。**
                # 05 的索引在存量归零时会把整行删掉（`_set_stored`），因此
                # 「被拿光了」与「从来没见过」在新快照里是同一个样子——
                # 而那正是最需要拦下来的一种变化。同理，报告里要用**计划自己
                # 记的名字**（`_label`），不能拿现场找不到的键去取名字。
                fresh = _find_key(inventory_snapshot, canonical)
                available = inventory_snapshot.available(fresh) if fresh is not None else 0
                if available < claimed:
                    stale_stock.append((canonical, claimed, available))
            if stale_stock:
                reasons.append(
                    BlockReason(
                        Reason.PLAN_STOCK_CHANGED,
                        u"计划声明要用的 %d 种材料现在不够了（%s）；"
                        u"按旧计划扣料会少扣"
                        % (
                            len(stale_stock),
                            u"、".join(
                                u"%s：声明 %d，现有 %d"
                                % (self._label(key), claimed, available)
                                for key, claimed, available in stale_stock[:4]
                            ),
                        ),
                    )
                )

        if check_dispatch is not None:
            checked["patterns"] = True
            for item in self.content["seal"]["dispatch"]:
                node = NodeKey.from_tuple(item["node_key"][0], tuple(item["node_key"][1:]))
                ref = DeviceRef(node, item["generation"])
                entry, blocker = check_dispatch(
                    ref, item["slot"], item["digest"], item["mode"]
                )
                if blocker is not None:
                    # 只记**规范文本**，显示用的名字由 `_label` 现查计划自己的
                    # 材料清单——复核报告不该依赖「现场还找得到那个键」。
                    stale_dispatch.append((item["step"], item["canonical"], blocker))
            # **07 的原因码原样透出，且按码分组**，不合并成一句「样板过期了」。
            # 「提供者被拆了」「设备离线」「样板被编辑」「模式不符」在界面上是
            # 四句不同的话，压成一个码就等于把「该做什么」丢掉一半。
            grouped = {}
            for step, key, blocker in stale_dispatch:
                grouped.setdefault(blocker.code, []).append((step, key, blocker))
            for code in sorted(grouped):
                rows = grouped[code]
                reasons.append(
                    BlockReason(
                        code,
                        u"计划引用的 %d 处样板不能用了（第 %s 步：%s）"
                        % (len(rows), rows[0][0], rows[0][2].detail),
                    )
                )

        topology_changed = False
        sealed_topology = self.content["seal"]["topology_version"]
        if sealed_topology is not None and topology_version is not None:
            checked["topology"] = True
            if sealed_topology != topology_version:
                topology_changed = True
                reasons.append(
                    BlockReason(
                        Reason.PLAN_TOPOLOGY_CHANGED,
                        u"计划是在拓扑版本 %s 上算的，现在是 %s；"
                        u"网络归属可能已经不同，不按旧计划扣料"
                        % (sealed_topology, topology_version),
                    )
                )
        return RevalidationReport(
            checked, stale_stock, stale_dispatch, topology_changed, reasons
        )

    # -- 序列化 --------------------------------------------------------

    def summary(self):
        # type: () -> dict
        """一行能读的摘要（终端预览、日志、诊断共用）。

        **`produced` 不等于「一定会产出」。** 它记的是「本次计划为此安排了
        多少生产」，而一个步骤可能因为上游断料/成环而**执行不了**
        （`runnable` 为假）。那种情况下 `produced` 仍然非零，同时
        `blocked` 也非零——两个数必须一起读。09 提交前要复核的正是
        `blocked_keys`（非空就不能提交）。
        """
        by_cause = {}
        for gap in self.content["gaps"]:
            by_cause[gap["cause"]] = by_cause.get(gap["cause"], 0) + 1
        return {
            "target": self.content["target"]["key"],
            "amount": self.content["amount"],
            "semantics": self.content["semantics"],
            "status": self.content["status"],
            # 三个数**同时给出**，09 与 11 因此读的是同一口径（08-01）：
            # 「玩家要多少 / 库里已经有多少被算进来 / 这次要产多少」。
            # `HOLD` 下 `held` 是抵扣掉的部分；`PRODUCE` 下它恒为 0。
            "requested": self.content["requested"],
            "held": self.content["held"],
            "produced": self.content["produced"],
            "steps": len(self.content["steps"]),
            "gaps": by_cause,
            "missing": [gap["key"] for gap in self.content["gaps"]],
            # 有几个键**最终拿不到**（含被上游连累的）。与 `produced` 一起读。
            "blocked": len(self.content["blocked_keys"]),
            "truncated": self.content["limits"]["truncated"],
            # **内存不再是一个估计值**（2026-09-26 规则更新）：字节数是 09
            # 准入时要用的那个数，因此单独给、且与 `estimates` 分开放——
            # 混在 `authoritative: False` 里会让 09 顺手把它当参考值用。
            "required_bytes": self.content["required_bytes"],
            "total_recipe_tasks": self.content["total_recipe_tasks"],
            "estimates": {
                "slots": self.content["estimates"]["slots"],
                "authoritative": False,
            },
        }

    def to_dict(self):
        # type: () -> dict
        return self.content

    def __repr__(self):
        return u"CraftPlan(%s, %s, %d 步)" % (
            self.content["status"],
            self.content["target"]["key"],
            len(self.content["steps"]),
        )


def _find_key(snapshot, canonical):
    # type: (InventorySnapshot, str) -> object
    """在库存快照里按规范文本找回**活的**资源键对象；没有就返回 `None`。

    `None` 的含义是「这份快照里没有这个键」——**它不等于「量是 0」**，
    但对规划来说两者是同一件事（`available` 都是 0），因此调用方按 0 处理。
    之所以只返回键对象而不返回「名字」：名字的来源是计划自己
    （`CraftPlan._label`），现场找不到的键在报告里照样写得出名字。
    """
    for key in snapshot.keys():
        if _canon(key) == canonical:
            return key
    return None


# -- 任务 ------------------------------------------------------------------


class PlanJob(object):
    """一次规划的**可分帧状态机**（08-05）。

    它同时支持三件事，而这三件事各自都有明确的语义：

    - **预算耗尽**：`step(budget)` 没干完就停在 `RUNNING`，下一帧接着走（不是失败）。
    - **取消**：`cancel()` 之后结果是 `None`，而**任何真实库存都没被动过**
      ——本模块根本没有写入口，这条是结构性的，不靠自觉。
    - **版本失效**：任务不自己监测；`CraftPlan.revalidate()` 把「依据还成立吗」
      交给提交方回答。理由是监测需要读活的 store，而那正是确定性的敌人。

    **一切状态都由两份冻结快照 + 策略派生**：不读时钟、不读帧号、不读随机数。
    因此 `step(1)` 调用 n 次与 `step(n)` 调用一次给出**逐字节相同**的计划。
    """

    __slots__ = (
        "target",
        "amount",
        "inventory",
        "patterns",
        "policy",
        "pattern_policy",
        "_ledger",
        "_pending",
        "_steps",
        "_gaps",
        "_edges",
        "_refused",
        "_cursor",
        "_status",
        "_status_detail",
        "_plan",
        "_spent_total",
        "_frame",
        "counters",
    )

    def __init__(self, target, amount, inventory, patterns, policy=None, pattern_policy=None):
        # type: (object, int, object, object, object, object) -> None
        if not isinstance(target, ResourceKey):
            raise TypeError(u"规划目标必须是 ResourceKey，得到 %r" % (type(target),))
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise TypeError(u"目标数量必须是整数，得到 %r" % (amount,))
        if amount <= 0:
            raise ValueError(u"目标数量必须为正，得到 %r" % (amount,))
        if not isinstance(inventory, InventorySnapshot):
            raise TypeError("库存快照必须是 InventorySnapshot")
        if not isinstance(patterns, PatternSnapshot):
            raise TypeError("样板快照必须是 PatternSnapshot")
        self.target = target
        self.amount = int(amount)
        self.inventory = inventory
        self.patterns = patterns
        self.policy = policy if policy is not None else PlanPolicy()
        #: R09 的口径（`unlimited` 输入要不要向网络要）。**已于 2026-09-30
        #: 定案**：`PatternPolicy.unlimited_fuel` 只有「外部机器自理」一侧，
        #: 因此标了不限量的输入**不会**变成需求——它不是「宁可少报一份要备的
        #: 料」，而是规则本身：那条由外界环境供应，要网络供给就别标记它。
        self.pattern_policy = (
            pattern_policy if pattern_policy is not None else PatternPolicy()
        )
        self._ledger = {}
        self._pending = {}
        self._steps = {}
        self._gaps = {}
        #: `父键 -> {子键: 毛量}`。边图（见模块 docstring）。
        self._edges = {}
        #: 因为成环而被拒绝的依赖边。**它们的需求记进了账本，但没有建立依赖**
        #: ——差额就在这里，因此账本仍然可解释（`refused_edges()`）。
        self._refused = {}
        self._cursor = None
        self._status = PlanStatus.RUNNING
        self._status_detail = ""
        self._plan = None
        #: 已经花掉的**配方节点**数（不是通用工作量）。
        self._spent_total = 0
        #: 上一次推进用的帧号。见 `step()`：同帧重入不加算。
        self._frame = None
        self.counters = {
            "selects": 0,
            "expansions": 0,
            "dfs_visits": 0,
            "steps": 0,
            "demands": 0,
            "nodes": 0,
            "rounds": 0,
            #: 同一帧里被重复调度的次数。**它随调用方式变化，因此不进指纹**
            #: （与 `rounds` 同一条理由，见 `plan_counters`）。
            "frame_repeats": 0,
            #: 候选预筛试过的候选数、其中被递归排除的数。
            "candidate_trials": 0,
            "recursive_candidates": 0,
            #: 防御网（`_expand_next_input` 的拒绝分支）触发的次数。
            #: **正常路径下它恒为 0**：预筛与走查在同一张冻结边图上做同一个判断。
            #: 它是这套构造**唯一**的观测点——不为 0 就说明设计有洞。
            "cycles_refused": 0,
        }
        self._note_root()

    # -- 推进 ----------------------------------------------------------

    def step(self, nodes, frame=None):
        # type: (int, object) -> str
        """推进 `nodes` 个**配方节点**，返回当前状态。

        ## 「一个节点」是什么

        **配方树上的一个键**：选定它的配方、展开它的输入。展开出来的子树
        **本帧不再继续**——它们进待办，由下一次调用去算。这正是验收那句
        「父节点与子节点不会在同 tick 计算」。

        **单位就是节点，不是「通用工作量」。** 规则原话：「不能简单把通用
        工作量预算设成 1 就宣称符合，工作单位必须与配方节点对应」。加批次、
        环检测 DFS 这些都在**一个节点之内**完成，它们有界（受 `max_candidates`
        与样板的输入条数约束），因此不是藏在一个节点里的无界递归。

        `_select` 一次就是「算一个节点」：它只记这个键的需求、把子键排进
        待办；真正的子树展开发生在**后面的**调用里。

        ## `frame`：同 tick 重入不多算

        调度器为了让每个网络都拿到份额，会在同一帧里把同一个运行时访问两次。
        调用方把帧号传进来，同一个帧的第二次调用**什么都不做**（只计数）。
        `frame=None`（离线直接调用）时不判重，每次调用算一帧——与引入这个
        参数之前的行为一致。

        预算耗尽**不是失败**：状态仍是 `RUNNING`，下一帧接着来。
        `nodes == 0` 是合法的空操作。
        """
        if isinstance(nodes, bool) or not isinstance(nodes, int):
            raise TypeError(u"节点预算必须是整数，得到 %r" % (nodes,))
        if nodes < 0:
            raise ValueError(u"节点预算不能为负，得到 %r" % (nodes,))
        if frame is not None and frame == self._frame:
            self.counters["frame_repeats"] += 1
            return self._status
        self._frame = frame
        if self._status != PlanStatus.RUNNING:
            return self._status
        self.counters["rounds"] += 1
        done = 0
        while done < nodes:
            if self._cursor is not None:
                # 光标还在这一个节点的输入上：**它属于同一个节点**，不额外
                # 计费。一个节点的输入条数由样板决定，是有界的。
                self._expand_next_input()
                if self._cursor is None:
                    done += 1
                continue
            key = self._next_pending()
            if key is None:
                # 没有待办了 → 收尾。`_finalize` 自己幂等，**它不是配方节点**，
                # 因此不占这次配额。
                self._finalize()
                break
            self._select(key)
            self.counters["nodes"] += 1
            if self._cursor is None:
                # 叶子、被库存满足、没有候选、被环拒绝……这条节点当场算完。
                done += 1
        self._spent_total += done
        return self._status

    def cancel(self, detail=""):
        # type: (str) -> bool
        """取消。返回是否真的从「还在算」变成了「已取消」。

        **它不改任何真实库存**（本模块没有写入口），也不保留半份结果：
        `result()` 之后返回 `None`。半份计划比没有计划更危险——
        它会看起来像一份完整的计划。
        """
        if self._status != PlanStatus.RUNNING:
            return False
        self._status = PlanStatus.CANCELLED
        self._status_detail = detail or "调用方取消了这次规划"
        self._plan = None
        self._pending = {}
        self._cursor = None
        return True

    def result(self):
        # type: () -> CraftPlan
        """算完之后的计划；还在算或已取消时返回 `None`。"""
        return self._plan

    def status(self):
        # type: () -> str
        return self._status

    def spent(self):
        # type: () -> int
        """已经花掉的**配方节点**数。"""
        return self._spent_total

    def progress(self):
        # type: () -> dict
        """给 11（与 09 的进度显示）的纯数据进度。"""
        reason = ""
        detail = self._status_detail
        if self._status == PlanStatus.RUNNING:
            # **「还没开始」与「预算用完了」是两句话。** 刚建好的任务
            # （`spent == 0`）报 `plan_budget_exhausted` 会让人以为推进过一轮，
            # 而去查一个根本不存在的预算问题。
            if self._spent_total > 0:
                reason = Reason.PLAN_BUDGET_EXHAUSTED
                detail = u"本帧预算用完了，任务可以继续推进"
            else:
                detail = u"任务刚建立，还没有推进过；给它一个非零预算即可开始"
        elif self._status == PlanStatus.CANCELLED:
            reason = Reason.PLAN_CANCELLED
        return {
            "status": self._status,
            "target": _name(self.target),
            "canonical": _canon(self.target),
            "amount": self.amount,
            "semantics": self.policy.target_semantics,
            "pending": len(self._pending),
            "steps": len(self._steps),
            "gaps": len(self._gaps),
            "keys": len(self._ledger),
            "spent": self._spent_total,
            "reason": reason,
            "detail": detail,
            "counters": dict(self.counters),
        }

    # -- 账本 ----------------------------------------------------------

    def ledger(self):
        # type: () -> tuple
        """按资源身份排序的账本行。**报告与诊断共用一份**，不各算一遍。"""
        return tuple(self._ledger[key] for key in sorted(self._ledger, key=_canon))

    def edges(self):
        # type: () -> tuple
        """已建立的依赖边 `(父键, 子键, 毛量)`，按身份排序。

        毛量是**本次派发要备的总量**（每条输入 `每批数量 × 批次数`），不是净增量。
        """
        out = []
        for parent in sorted(self._edges, key=_canon):
            for child in sorted(self._edges[parent], key=_canon):
                out.append((parent, child, self._edges[parent][child]))
        return tuple(out)

    def refused_edges(self):
        # type: () -> tuple
        """因为成环而被拒绝的依赖边 `(父键, 子键, 毛量)`，按身份排序。

        这些边**不建立依赖**（因此不会被继续展开、也不会无界长大），但它们的
        需求照记进账本——不记的话，那份明明要不到的东西会从缺口清单里消失。
        差额就落在这些边上，所以「每个键的需求 = 根贡献 + 入边 + 被拒的入边」
        这条恒等式仍然成立。
        """
        out = []
        for parent in sorted(self._refused, key=_canon):
            for child in sorted(self._refused[parent], key=_canon):
                out.append((parent, child, self._refused[parent][child]))
        return tuple(out)

    def _row(self, key):
        # type: (object) -> LedgerRow
        row = self._ledger.get(key)
        if row is None:
            row = LedgerRow(key)
            self._ledger[key] = row
        return row

    @staticmethod
    def _set_depth(row, depth):
        # type: (LedgerRow, int) -> None
        """记下见到这个键的**最浅**深度。深度只是排序与上限判定的依据，
        而它取最浅的那一次：一条 5 层的链与一条 2 层的链同时需要它时，
        「2 层那条能算出来」是更值得报告的事实。"""
        if row.depth is None or depth < row.depth:
            row.depth = depth

    def _note_root(self):
        # type: () -> None
        """记下根需求。`HOLD` 口径下库存参与抵扣，`PRODUCE` 下不许抵扣。"""
        row = self._row(self.target)
        row.demand += self.amount
        row.exempt += self.policy.root_exempt(self.amount)
        self._set_depth(row, 0)
        self._recompute_from_stock(row)
        self._queue(self.target, 0)
        self.counters["demands"] += 1

    def _recompute_from_stock(self, row):
        # type: (LedgerRow) -> None
        """重算这个键从库存划过来的量。

        **单调不减**：`demand` 只会变大、`available` 是快照里的定值，
        因此划过来的量只会变多——不会出现「需求变大反而少用库存」这种怪事。
        """
        creditable = row.demand - row.exempt
        if creditable <= 0:
            row.from_stock = 0
            return
        row.from_stock = min(self.inventory.available(row.key), creditable)

    def _note_demand(self, key, count, depth):
        # type: (object, int, int) -> None
        """记一笔需求，并按需排队。

        **已经记过缺口的键默认不再展开**，只有一种例外：缺口原因是
        `DEPTH_LIMIT` 且这次来的路径更浅。理由逐个说清楚，因为它们不一样：

        - `NO_RECIPE` / `NO_PROVIDER`：只取决于冻结的快照，与走哪条路径无关，
          重开也是同一个答案。
        - `RECURSIVE_CANDIDATES`：重开同样没有意义，但理由与旧口径不同，
          而且**更强**——它不再依赖「路径」这个概念，只依赖两条单调性：
          ①**候选的递归性是单调的**。边图只增不减（全文件没有一处 `-=`），
          而候选来自**冻结快照**，所以「某条候选的输入绕得回本键」这件事
          只会由否变是，不会由是变否。于是一条被排除的候选永远被排除，
          `viable` 一旦为空就永远为空，重开只会得到同一个空集。
          ②**这个缺口自己会消失**，当且仅当别的路子把需求满足了——
          收尾的 `_finalize` 用 `demand - achievable <= 0` 删掉它（副产物或
          库存覆盖了它时）。那时不需要「重开」，需求已经不缺了。
          两条合起来：要么缺口消失、要么答案不变，中间没有第三种可能。
        - 各类上限：重开会让一次截断变成反复重试，而该看的是上限本身。

        （旧口径下这里论证的是 `CYCLE`「与路径有关但重开无意义」；那个常量
        已随 2026-09-26 的规则删除，理由也随之换成上面这条不依赖路径的版本。）
        """
        row = self._row(key)
        row.demand += count
        self._set_depth(row, depth)
        self._recompute_from_stock(row)
        self.counters["demands"] += 1
        gap = self._gaps.get(key)
        if gap is not None:
            if gap.cause == GapCause.DEPTH_LIMIT and depth < gap.depth:
                del self._gaps[key]
            else:
                return
        self._queue(key, depth)

    def _queue(self, key, depth):
        # type: (object, int) -> None
        """把键排进待办。深度取**最浅的那一次**（越浅越接近目标）。"""
        current = self._pending.get(key)
        if current is None or depth < current:
            self._pending[key] = depth

    def _next_pending(self):
        # type: () -> object
        """下一个要展开的键：先浅后深，同深度按资源身份排序。

        **顺序完全由内容决定**，因此预算怎么切都不改变处理顺序——
        这是「一次运行与分帧运行结果一致」的根。
        """
        if not self._pending:
            return None
        return sorted(
            self._pending, key=lambda key: (self._pending[key], _canon(key))
        )[0]

    # -- 展开 ----------------------------------------------------------

    def _select(self, key):
        # type: (object) -> int
        """为一个键选配方并安排批次。代价 = 1 + 检查过的候选数。

        **预筛在选候选之前**：会绕回本键的候选不参与挑选（`_screen_inputs`）。
        预筛的遍历**不动 tick 预算**——`step()` 丢掉这里返回的 `cost`，
        配额只按「算完一个节点」计（`done += 1`）。把 DFS 计进配额的话，
        「每 tick 一个逻辑配方节点」这条规则就破了（08-05）。
        它仍然**有界**：每个节点最多 `max_candidates` 次 `_reachable`，
        每次的范围是边图（键数 ≤ `max_steps`），次数记在 `counters` 里。
        """
        cost = 1
        depth = self._pending.pop(key, None)
        if depth is None:
            return cost
        row = self._ledger[key]
        if row.outstanding() <= 0:
            # 排队期间被副产物覆盖了（或需求本来就被库存满足）。
            return cost
        self.counters["selects"] += 1
        if depth > self.policy.max_depth:
            self._record_gap(
                key,
                GapCause.DEPTH_LIMIT,
                u"依赖深度超过上限 %d：这条链在第 %d 层被截断，"
                u"因此它「可能」还能做，只是这次没往下看"
                % (self.policy.max_depth, depth),
                depth,
            )
            return cost

        candidates = self.patterns.candidates(key)
        if not candidates:
            self._record_gap(
                key,
                GapCause.NO_RECIPE,
                u"样板索引里没有任何样板产出 %s；先给一台样板接口装上对应的样板"
                % (_name(key),),
                depth,
            )
            return cost

        # **先截断再按可用性过滤**，顺序不能反：反过来的话窗口宽度会随
        # 「恰好有几台离线」变化，于是「候选取前 N 个」这件事变得不可复现，
        # 而更糟的是截断会被误报成「没有提供者」。
        window = tuple(candidates[: self.policy.max_candidates])
        cost += len(window)
        usable = [item for item in window if item.available]
        if not usable:
            if len(candidates) > len(window):
                self._record_gap(
                    key,
                    GapCause.CANDIDATE_LIMIT,
                    u"前 %d 个候选提供者都不可用，而后面还有 %d 个没看；"
                    u"这是「没看完」，不是「没有提供者」"
                    % (len(window), len(candidates) - len(window)),
                    depth,
                )
            else:
                self._record_gap(
                    key,
                    GapCause.NO_PROVIDER,
                    u"产出 %s 的样板有 %d 块，但提供者现在都不能用（%s）"
                    % (
                        _name(key),
                        len(candidates),
                        _blocker_text(window),
                    ),
                    depth,
                )
            return cost

        # **挑候选之前先排除递归候选**（2026-09-26 规则）。预筛必须**紧挨着**
        # 建步骤，中间不能插进别的 `_select`：走查（`_expand_next_input`）
        # 在同一张边图上做同一个判断，而光标在同一个节点内一次走完
        # （见 `step()` 的 `continue`），因此两者之间**零窗口**。
        step = self._steps.get(key)
        if step is not None:
            # 已经有步骤：沿用原来那条候选，**预筛整段跳过**。
            #
            # 这不是省事，是正确性。`step.chosen` 的每条输入都已经过了预筛、
            # 又逐条拿到了被接受的边，因此它不可能变成递归的——候选的递归性
            # 是**单调**谓词（边图只增不减、候选集来自冻结快照，论证见
            # `_note_demand`）。反过来，若在这里按当下的 `usable` 重新挑，
            # 就会从一个**已经提交了批次与预计产出**的步骤底下把候选抽掉，
            # 而账本是单调的、没有回退。
            chosen = step.chosen
        else:
            viable = []
            first_cycle = None
            for item in usable:
                self.counters["candidate_trials"] += 1
                path = self._screen_inputs(key, item.definition)
                if path is None:
                    viable.append(item)
                    continue
                self.counters["recursive_candidates"] += 1
                if first_cycle is None:
                    first_cycle = path
            if not viable:
                # 候选都在、一条能走通的都没有。三种情形的区分见那个函数。
                self._record_recursion_gap(
                    key, candidates, window, first_cycle, depth
                )
                return cost
            # `pick_candidate` 拿到的是**筛过之后**的整份可用集合，
            # 而不是「筛出来的一条」：优先级规则（08-04 待定）要在所有
            # 可行候选之间排序，只给它一条的话那条规则就没有意义了。
            chosen = self.policy.pick_candidate(viable)
        # **建步骤之前先问产出**：索引说它有这个产物、内容里却没有时
        # （手工构造或从 JSON 恢复的快照可能与内容不一致），那一步压根不该
        # 被建出来——建完再判会留下一张 0 批次的空步骤，而它会进计划、
        # 进 `dispatch`，09 拿到一条「派 0 批」的凭据。
        per_batch = _yield_for(chosen.definition, key)
        if per_batch <= 0:
            self._record_gap(
                key,
                GapCause.NO_RECIPE,
                u"样板 %r 的产物里没有 %s（索引与内容不一致，按没有配方处理）"
                % (chosen.device_id, _name(key)),
                depth,
            )
            return cost

        # **按 outstanding 算增量批次**，不是按 demand 算总批次：
        # 已经安排的批次与副产物都已经在池子里了，再按 demand 算会重复生产。
        extra = (row.outstanding() + per_batch - 1) // per_batch
        if extra <= 0:
            return cost
        already = step.batches if step is not None else 0
        if already + extra > self.policy.max_batches_per_step:
            self._record_gap(
                key,
                GapCause.BATCH_LIMIT,
                u"%s 需要 %d 个批次，超过单步上限 %d；数量可能填错了"
                % (_name(key), already + extra, self.policy.max_batches_per_step),
                depth,
            )
            return cost
        if step is None:
            if len(self._steps) >= self.policy.max_steps:
                self._record_gap(
                    key,
                    GapCause.NODE_LIMIT,
                    u"计划的步骤数已达上限 %d；这是「没算完」，不是「做不出来」"
                    % (self.policy.max_steps,),
                    depth,
                )
                return cost
            step = CraftStep(key, depth, chosen, window)
            step.required_inputs = self.pattern_policy.required_inputs(chosen.definition)
            step.external_inputs = tuple(
                stack
                for stack in chosen.definition.inputs
                if not self.pattern_policy.supplies(stack)
            )
            self._steps[key] = step
            self.counters["steps"] += 1
        if depth < step.depth:
            step.depth = depth
        step.batches += extra
        row.own_output += extra * per_batch
        for other_key, per in step.byproduct_pairs():
            other = self._row(other_key)
            other.byproduct_output += extra * per
            self._set_depth(other, depth + 1)
            if other.outstanding() <= 0:
                # 副产物把它喂饱了：不必再为它排一次生产。
                # 这是**优化不是正确性**——不弹掉它，`_select` 也会在
                # 「outstanding <= 0」那一行直接返回。
                self._pending.pop(other_key, None)
        # 重新走一遍这个步骤的输入：批次变大了，每条边的毛量要跟着涨。
        self._cursor = (key, 0)
        return cost

    def _expand_next_input(self):
        # type: () -> int
        """展开一个步骤的下一条输入。代价 = 1 + 环检测 DFS 访问的节点数。

        走的是**合并后**的输入（`CraftStep.input_pairs`）而不是原始条目：
        同一种料的重复条目必须在记账前就并成一条，否则第二条的增量算成负数，
        需求会被少记（见 `input_pairs` 的说明）。
        """
        cost = 1
        key, index = self._cursor
        step = self._steps[key]
        inputs = step.input_pairs()
        if index >= len(inputs):
            self._cursor = None
            return cost
        child, per_batch = inputs[index]
        self._cursor = (key, index + 1)
        edge = self._edges.setdefault(key, {})
        refused = self._refused.setdefault(key, {})
        # **两条账都要减掉**：批次变大时重走一遍，增量必须相对「已经记过的总量」算
        # （含被拒绝的那部分）。只减 `edge` 的话，被拒绝的边会每重走一次就再记一遍，
        # 于是那个键的需求凭空膨胀——而膨胀出来的需求谁也满足不了，
        # 报告里就成了一个查不出原因的缺口。
        already = edge.get(child, 0) + refused.get(child, 0)
        need = per_batch * step.batches - already
        if need <= 0:
            return cost
        self.counters["expansions"] += 1

        path, visits = self._reachable(child, key)
        self.counters["dfs_visits"] += visits
        cost += visits
        if path is not None:
            # **防御网。** 按 2026-09-26 的设计它在正常路径上**不可达**：
            # 预筛（`_screen_inputs`）在建步骤之前就用同一个判据问过同一条输入，
            # 而光标在同一个节点内一次走完（`step()` 的 `continue`），两者之间
            # 没有别的键被执行。四条腿的论证见模块 docstring。
            #
            # 保留它是为了让**改动这个结构的人**得到保守的结果：一旦预筛与走查
            # 分叉（比如把预筛挪到别的调用里、或改用 `definition.inputs`），
            # 这里会把那份已经提交的步骤判成做不到，而不是让它带着一条环状依赖
            # 进计划。`counters["cycles_refused"]` 是它唯一的观测点。
            #
            # 这里**刻意不记需求**（与「缺口照记」相反）。被拒绝的那份既不会有
            # 任何步骤去产出、也不会由库存来补，把它记成需求就等于凭空造出一笔
            # 永远满足不了的需求：那个键的 `outstanding`、`materials` 与
            # `claims` 都会被撑大，而 `PRODUCE` 口径下还会让它误以为自己用上了
            # 库存（根需求之外多出来的那一份被算成了可抵扣的）。
            self.counters["cycles_refused"] += 1
            step.blocked = True
            if child not in step.blocked_by:
                step.blocked_by.append(child)
            refused[child] = refused.get(child, 0) + need
            self._record_gap(
                child,
                GapCause.RECURSIVE_CANDIDATES,
                u"循环依赖：%s（这条链按做不到处理）"
                % (u" -> ".join([_name(item) for item in path + [child]]),),
                step.depth + 1,
            )
            return cost

        edge[child] = edge.get(child, 0) + need
        self._note_demand(child, need, step.depth + 1)
        return cost

    def _screen_inputs(self, key, definition):
        # type: (object, object) -> object
        """这条候选展开后会不会把依赖绕回 `key`：会就返回环路路径，不会返回 `None`。

        判据与走查用的是**同一个**：`_reachable(子键, key)`——「子键是不是已经
        能回到 key」，也就是「子键是不是 key 的祖先」。见模块 docstring 里
        「环检测必须在边图上做」那一段（纯路径判据会漏掉兄弟成环）。

        **输入集合必须走 `pattern_policy.required_inputs`**
        （与 `CraftStep.input_pairs()` 同源，见 `_merge_input_stacks`），
        **不能**用 `definition.inputs`。不限量燃料那类输入走查时根本不建边
        （`PatternPolicy.supplies` 把它划给外部自理），按原始输入预筛会看到
        它「能回到 key」，于是排掉一条本来可行的候选、报出一个并不存在的缺料
        ——那是**反方向的谎话**，与谎报「做得到」一样不能接受。

        路径的写法与走查一致（`path + [子键]`，首尾同一个键），因此诊断里
        读到的永远是「A 要去 B，B 又回到 A」这种可读的形状。
        """
        for child, _per_batch in _merge_input_stacks(
            self.pattern_policy.required_inputs(definition)
        ):
            path, visits = self._reachable(child, key)
            self.counters["dfs_visits"] += visits
            if path is not None:
                return path + [child]
        return None

    def _record_recursion_gap(self, key, candidates, window, first_cycle, depth):
        # type: (object, tuple, tuple, object, int) -> None
        """一个键的候选全被排除时，说清**是哪一种「没有路线」**。

        三种情形，按「玩家下一步该做什么」分开报。顺序与上面 `not usable`
        那一段**刻意一致**（都是先问「是不是没看完」），理由也一样：
        **没看完的时候不能报成「确实没有」**——窗口外还有候选没看时，
        我们并不知道那里有没有非循环路线，而 `RECURSIVE_CANDIDATES` 与
        `NO_PROVIDER` 都属于「确实没有」那一侧。报成上限还保证
        `limits["truncated"]` 是诚实的（否则截断会被一次「确实没有」盖掉）。
        """
        unseen = len(candidates) - len(window)
        if unseen > 0:
            self._record_gap(
                key,
                GapCause.CANDIDATE_LIMIT,
                u"前 %d 个候选里可用的那些都会成环，而后面还有 %d 个没看；"
                u"这是「没看完」，不是「没有非循环路线」"
                % (len(window), unseen),
                depth,
            )
            return
        # **有路线，只是那台设备现在不能派发。** 不能报成「只能靠循环配方」：
        # 玩家要做的是把接口接回来，而不是回去改配方。这里要再筛一次
        # 不可用的那些候选——只有「筛掉它们之后还剩路线」才说明问题在可用性上。
        offline = []
        for item in window:
            if item.available:
                continue
            self.counters["candidate_trials"] += 1
            if self._screen_inputs(key, item.definition) is None:
                offline.append(item)
        if offline:
            self._record_gap(
                key,
                GapCause.NO_PROVIDER,
                u"产出 %s 的样板里，能走通非循环路线的那些提供者现在都不能用"
                u"（%s）；其余候选都会成环"
                % (_name(key), _blocker_text(tuple(offline))),
                depth,
            )
            return
        if first_cycle:
            cycle = u" -> ".join([_name(item) for item in first_cycle])
        else:  # pragma: no cover - 防御性：`usable` 非空时至少算过一条路径
            cycle = u"（环路路径未记录）"
        self._record_gap(
            key,
            GapCause.RECURSIVE_CANDIDATES,
            u"循环依赖：%s。%s 的每一条可用候选都会把依赖绕回它自己，"
            u"因此全部被排除；没有别的路线，按原料不足处理"
            % (cycle, _name(key)),
            depth,
        )

    def _reachable(self, source, target):
        # type: (object, object) -> tuple
        """在**边图**上问「`source` 能不能到达 `target`」，返回 `(路径, 访问节点数)`。

        路径为 `None` 表示到不了。这就是环检测：插入一条 `target -> source` 的边
        之前先问「`source` 是不是已经能回到 `target`」，能就说明这条边会成环。
        因为**每条被接受的边都过这一问**，边图按构造是 DAG——终止性因此
        可证，而不必靠上限去兜（见模块 docstring）。

        访问节点数一并返回而不是记在实例上：代价记账要按**这一次**的访问量算，
        而写在计数器里就得在调用前后各读一次、还要防着别处也动它。

        遍历顺序按资源身份排序，因此**路径是可复现的**，报出来的环路
        永远是同一条（否则同一个环有时写 `A -> B -> A`、有时写 `B -> A -> B`，
        诊断就没法比对了）。
        """
        visits = 0
        target_mark = _canon(target)
        if _canon(source) == target_mark:
            # 自环：`A ← A + B` 这类配方（催化剂、回收）。首版一律按环拒绝。
            return [source], 1
        path = [source]
        stack = [self._children_of(source)]
        seen = set([_canon(source)])
        visits += 1
        while stack:
            kids = stack[-1]
            if not kids:
                stack.pop()
                path.pop()
                continue
            nxt = kids.pop()
            mark = _canon(nxt)
            if mark in seen:
                continue
            seen.add(mark)
            visits += 1
            if mark == target_mark:
                return path + [nxt], visits
            path.append(nxt)
            stack.append(self._children_of(nxt))
        return None, visits

    def _children_of(self, key):
        # type: (object) -> list
        """该键的出边，**按资源身份倒序**（因为调用方用 `pop()` 取，倒序才是正序访问）。"""
        return sorted(self._edges.get(key, {}), key=_canon, reverse=True)

    def _record_gap(self, key, cause, detail, depth):
        # type: (object, str, object, int) -> object
        """记一条缺口。**同一个键只留第一条原因**。

        原因之间不会互相冒充：`NO_RECIPE`/`NO_PROVIDER` 只取决于快照（与路径无关），
        `RECURSIVE_CANDIDATES` 只取决于**冻结的候选集在当前边图上的可达性**
        ——边图只增不减，因此它一旦成立就永久成立（见 `_note_demand` 的第①条）；
        各种上限则取决于处理顺序——而处理顺序是确定性的。
        因此「先记哪一条」是确定的，不需要更复杂的优先级规则。
        `DEPTH_LIMIT` 例外：更浅的路径到来时它会被清掉（见 `_note_demand`）。
        """
        existing = self._gaps.get(key)
        if existing is not None:
            return existing
        gap = GapRecord(key, cause, detail, depth)
        self._gaps[key] = gap
        return gap

    # -- 收尾 ----------------------------------------------------------

    def _finalize(self):
        # type: () -> None
        """把这次搜索变成一份计划。**纯后置 pass，不反馈进扩展过程。**

        「不反馈」是确定性的要求：收尾只看已经算出来的账本与边，
        因此 `_finalize` 在什么预算下被调用，结果都一样。
        """
        if self._plan is not None or self._status != PlanStatus.RUNNING:
            return
        # 1. 可达量：输入凑不齐的步骤产不出东西，而**它的下游因此也不可达**。
        achievable = self._achievable()
        # 2. 被喂饱的缺口不算缺口。**判据用 `achievable` 而不是账本池子**：
        #    池子把「一个执行不了的步骤的产出」也算进去了，于是某个键可能
        #    「账面上够了、实际拿不到」——那时缺口若被丢掉，`blocked_keys` 里
        #    还会出现它，两个字段就对同一件事给出相反的说法。
        #    两者的判据一致之后，「有缺口」与「执行不了」是同一件事。
        for key in sorted(self._gaps, key=_canon):
            if self._ledger[key].demand - achievable.get(key, 0) <= 0:
                del self._gaps[key]
        # 3. 总判决与逐键原因。
        blocked = self._blocked_keys(achievable)
        status, detail = self._verdict(blocked)
        self._status = status
        self._status_detail = detail
        self._plan = self._build_plan(blocked, achievable)

    def _achievable(self):
        # type: () -> dict
        """`键 -> 真正能拿到多少`（**最小不动点**），并顺手标出哪些步骤真的能跑。

        没有这一步的话，一条深链上的死路（环 / 没样板 / 超限）会让**祖先步骤的
        `own_output` 照记**，于是「铁镐」看起来有货、而「铁锭」那一步永远执行不了
        ——那正是「报告说能做、实际做不出来」的谎。

        **方向是从「一步都跑不了」往上加，不是从「全都能跑」往下减。**
        往下减算的是**最大**不动点，它会把互相依赖的两步都判成能跑：判 A 时
        拿 B 的产出当供给，判 B 时又拿 A 的产出当供给——`A ← B`、`B ← A`
        于是双双「凑得齐」，而环在 `_gaps` 被清掉之后就会伪装成一份能做完的计划。
        从零开始加则相反：每一步都必须由**已经证明拿得到**的东西
        （库存 + 已确认可执行的步骤）支撑，结论只少不多——这正是「不谎报」
        要的那一侧，与 `_verdict` 把真实原因排在截断之前是同一条取向。

        复杂度是 `O(步骤数² × 输入数)`，而步骤数被 `max_steps` 兜着（默认 64），
        因此这是收尾时一次有界的计算，不是每帧的常驻开销。
        """
        steps = self._steps
        totals = {}
        for key in sorted(self._ledger, key=_canon):
            row = self._ledger[key]
            if row.from_stock:
                totals[key] = totals.get(key, 0) + row.from_stock
        # 尝试顺序只影响「哪一轮先加进来」，不影响最终集合（最小不动点与顺序无关）；
        # 固定按 (深度, 身份) 走是为了可复现。
        order = sorted(steps, key=lambda key: (steps[key].depth, _canon(key)))
        runnable = {}
        for _ in range(len(steps) + 1):
            changed = False
            for key in order:
                if key in runnable:
                    continue
                step = steps[key]
                if step.batches <= 0:  # pragma: no cover - 防御性：没有批次的步骤不产出
                    continue
                # 按**合并后**的每条输入比：逐条比会让「同一种料写了两遍」的
                # 步骤被误判成凑得齐（每一条单独看都够，加起来不够）。
                ok = True
                for child, per_batch in step.input_pairs():
                    if per_batch * step.batches > totals.get(child, 0):
                        ok = False
                        break
                if not ok:
                    continue
                # 它的输入都拿得到 → 这一步能跑，于是它的产出也算数。
                runnable[key] = True
                for out_key, per in step.output_pairs():
                    totals[out_key] = totals.get(out_key, 0) + per * step.batches
                changed = True
            if not changed:
                break
        for key in steps:
            steps[key].runnable = key in runnable
        return totals

    def _blocked_keys(self, achievable):
        # type: (dict) -> list
        """哪些键最终拿不到。原因取它自己的直接缺口，没有就继承子树里最深的那条。

        「最深」是刻意的：越深越接近基础原料，也就是真正该去补的东西。
        同深度时按资源身份排序，因此结论可复现。
        """
        out = []
        for key in sorted(self._ledger, key=_canon):
            row = self._ledger[key]
            shortfall = row.demand - achievable.get(key, 0)
            if shortfall <= 0:
                continue
            gap = self._gaps.get(key)
            if gap is not None:
                # 它自己就是断点：原因、说明都用它自己的。
                cause = gap.cause
                detail = gap.detail
                inherited = False
            else:
                # 它自己有配方，断在上游。原因**不能**照抄上游那条
                # （报「铁锭没有配方」会指向一份明明存在的样板），
                # 但仍要把上游是谁、为什么断说清楚。
                upstream = self._inherited_gap(key)
                cause = GapCause.MISSING_INPUTS
                if upstream is None:
                    detail = u"上游有材料凑不齐，但缺口记录已不在（计划可能被截断）"
                else:
                    detail = u"上游缺料：%s（%s）——%s" % (
                        _name(upstream.key),
                        upstream.cause,
                        upstream.detail,
                    )
                inherited = True
            out.append(
                {
                    "key": _name(key),
                    "canonical": _canon(key),
                    "kind": key.kind,
                    "cause": cause,
                    "reason": _GAP_REASONS.get(cause, Reason.PLAN_LIMIT_REACHED),
                    "detail": detail,
                    "shortfall": shortfall,
                    "demand": row.demand,
                    "achievable": achievable.get(key, 0),
                    "inherited": inherited,
                }
            )
        return out

    def _inherited_gap(self, key):
        # type: (object) -> object
        """在 `key` 的依赖子树里找**最深**的一条直接缺口。找不到返回 `None`。

        **被拒绝的边也要走**（`_refused`）：环上那条边不记需求、也不进 `_edges`，
        但它是这条链断掉的真实位置——只看 `_edges` 的话，一个「输入全在环上」
        的键会找不到任何上游缺口，报告里只剩一句「缺口记录已不在」，
        而真正要告诉玩家的是「它上游那个键成环了」。

        「最深」是刻意的：越深越接近基础原料，也就是真正该去补的东西。
        同深度时按资源身份排序，因此结论可复现。
        """
        best = None
        seen = set()
        stack = [key]
        while stack:
            current = stack.pop()
            if _canon(current) in seen:
                continue
            seen.add(_canon(current))
            gap = self._gaps.get(current)
            if gap is not None:
                if best is None or (gap.depth, _canon(gap.key)) > (best.depth, _canon(best.key)):
                    best = gap
            children = set(self._edges.get(current, {}))
            children.update(self._refused.get(current, {}))
            for child in sorted(children, key=_canon):
                stack.append(child)
        return best

    def _verdict(self, blocked):
        # type: (list) -> tuple
        """总判决与一句能读的说明。

        优先级是有意的：**真实原因压过上限**（材料真的缺就报缺料，哪怕顺带
        截断了），而 `NO_RECIPE` 只留给目标本身——否则「铁镐的原料缺了」会被
        报成「没有铁镐的配方」，玩家会去到处找一份其实已经有的样板。

        **2026-09-26 起这里不再有「有环就报环」那一条。** 环现在是逐候选被排除的
        （`GapCause.RECURSIVE_CANDIDATES`），它不在 `DERIVED_CAUSES` 里，
        因此会作为「叶子原因」被 `sorted(leaf or real)[0]` 选中——总判决是
        `MISSING_MATERIALS`（原料拿不到），而说明文字就是那条环路。
        规则要的正是这个：**把「打转了」当缺料的诊断来说，而不是当整单的判决。**
        """
        if not self._gaps and not blocked:
            return PlanStatus.COMPLETE, ""
        causes = set(gap.cause for gap in self._gaps.values())
        target_gap = self._gaps.get(self.target)
        # 目标**自己**的问题优先于别处的：玩家问的是「我要的东西能不能做出来」。
        if target_gap is not None and target_gap.cause in _TARGET_CAUSE_STATUS:
            return _TARGET_CAUSE_STATUS[target_gap.cause], target_gap.detail
        if GapCause.NO_PROVIDER in causes:
            return PlanStatus.NO_PROVIDER, self._first_detail(GapCause.NO_PROVIDER)
        real = causes - GapCause.LIMIT_CAUSES
        if real:
            # 说明文字优先给**叶子**原因（真的断了的那一处），而不是传导而来的
            # 「上游缺料」——后者只是转发，读它等于多绕一层。
            leaf = [cause for cause in real if cause not in GapCause.DERIVED_CAUSES]
            return PlanStatus.MISSING_MATERIALS, self._first_detail(
                sorted(leaf or real)[0]
            )
        if causes:
            return PlanStatus.LIMITED, self._first_detail(sorted(causes)[0])
        # 走到这里说明有键拿不到、却一条缺口都没记下来。理论上到不了
        # （拿不到的那些键必然有一条被留住的缺口），但**不返回 COMPLETE**：
        # 「有东西缺着」绝不能因为某个分支没覆盖到而被报成「都齐了」。
        first = blocked[0]
        return PlanStatus.MISSING_MATERIALS, first["detail"] or u"%s 缺 %d 个" % (
            first["key"],
            first["shortfall"],
        )

    def _first_detail(self, cause):
        # type: (str) -> object
        """某个原因的第一条说明（按缺口深度与身份排序，可复现）。"""
        rows = [
            gap for gap in self._gaps.values() if gap.cause == cause
        ]
        if not rows:
            return ""
        rows.sort(key=lambda gap: (-gap.depth, _canon(gap.key)))
        return rows[0].detail

    def leaf_units(self):
        # type: () -> dict
        """**叶子折算**：`{键: 微单位}`，每个键「不由本计划产出」的那部分需求。

        **账本记的是件数/mB，这里折成微单位物**（1 个物品 = 50、1 mB = 1，
        见 `resource_key`）。折算放在这一处，而不是让每个调用方自己乘：
        乘错一次就是 50 倍的差额，而那个数会直接变成订单占的内存。

        规则原文：`required_bytes = 根原料总单位物 × 1 B/单位物`，
        并且「库存已抵扣而不再展开的中间品」**也要折算**（2026-09-26 User 确认）。

            demand - own_output - byproduct_output

        逐项都有理由：

        - **根原料**（没有任何步骤产出它）：`own_output = 0`，于是整份需求都
          计入——不管它是从库存来的还是从外部输入来的，它都是这个订单要占住的
          那一份。
        - **被库存抵扣、因而不再展开的中间品**：那份抵扣量就是这里的差额，
          计入。**这正是「把抵扣物折算成内存」。**
        - **由本计划产出的中间品**：`demand == own_output`，差额 0，不计入
          ——它的原料在下一层已经计过了。
        - **副产物**：从别的步骤顺带出来的，那份产出同样有自己的原料在账上，
          再计一次就是重复收费。

        结果是**每一单位物正好被计一次**：要么作为要生产的根原料，要么作为
        从库存/外部拿的叶子。这也是验收「中间产物不重复收费」的判据。
        """
        out = {}
        for key, row in self._ledger.items():
            left = row.demand - row.own_output - row.byproduct_output
            if left > 0:
                out[key] = key.micro_units(left)
        return out

    def required_micro_units(self):
        # type: () -> int
        """叶子折算的合计（微单位）。**取整之前**的那个数。"""
        total = 0
        for value in self.leaf_units().values():
            total += value
        return total

    def required_bytes(self):
        # type: () -> int
        """这个订单要占住的**字节数**（R04 已定案的口径）。

        1 单位物 = 1 B；50 mB 流体 = 1 单位物。取整口径见 `ByteRounding`
        ——它仍是 R07 的未定边界，替身取向上取整。
        """
        micro = self.required_micro_units()
        unit = MICRO_UNITS_PER_UNIT
        if self.policy.byte_rounding == ByteRounding.CEIL:
            # py2 里 `-(-a // b)` 就是向上取整（两边都是整数，`//` 是整除）。
            return -(-micro // unit)
        return micro // unit

    def total_recipe_tasks(self):
        # type: () -> int
        """订单配方树里的**配方任务总数**（含同配方的重复执行次数）。

        压缩表示下就是各步骤的批次数之和：`batches` 数的是「这个键要生产几批」，
        而一批就是一次配方执行。规则原文：「同配方执行 10 次计 10 个任务，
        每次占 1 槽、完成一次进度加 1…不得因内部批次合并变成整批占 1 槽、
        整批计 1 个任务」。

        **进度与槽位按这个数计**，不是按步骤数。
        """
        total = 0
        for step in self._steps.values():
            total += step.batches
        return total

    def plan_counters(self):
        # type: () -> dict
        """**与预算无关**的那部分计数（进计划内容，因此进指纹）。

        见 `_build_plan` 里那段说明：`rounds` 必须排除在外。
        """
        return dict(
            (name, self.counters[name])
            for name in
            ("selects", "expansions", "dfs_visits", "steps", "demands", "nodes")
        )

    def execution_ranks(self):
        # type: () -> dict
        """`键 -> 执行层级`：叶子（没有依赖的步骤）为 0，父键为其子键的最大值 +1。

        **为什么不用步骤的 `depth` 排序**：`depth` 是「这个键被见到的最浅深度」，
        它会在依赖边建立之后被更浅的路径**调小**，因此不再满足「每条边都从
        depth 指向 depth+1」。一个键既被目标直接需要、又是另一条链上的子需求时
        （共享子需求，08-03 的核心形状），按 `depth` 排会把依赖排在消费者之后
        ——09 按计划顺序派发就会先派发一个原料还没到位的步骤。

        `rank` 由边图算出，因此 `rank(父) >= rank(子) + 1` 恒成立，
        按 `rank` **升序**排列**必然是合法拓扑序**（依赖在前、消费者在后）。
        同层之间没有边，用资源身份打破并列即可复现。
        """
        ranks = {}
        for start in sorted(self._steps, key=_canon):
            if start in ranks:
                continue
            # 迭代式后序：边图是 DAG（每条被接受的边都过了 `_reachable`），
            # 因此不会重复展开；用显式栈是为了不让 `max_depth` 的取值影响递归深度。
            stack = [(start, False)]
            while stack:
                key, expanded = stack.pop()
                if key in ranks:
                    continue
                kids = [
                    child
                    for child in self._edges.get(key, {})
                    if child in self._steps
                ]
                if not expanded:
                    stack.append((key, True))
                    for child in sorted(kids, key=_canon, reverse=True):
                        stack.append((child, False))
                    continue
                rank = 0
                for child in kids:
                    rank = max(rank, ranks.get(child, 0) + 1)
                ranks[key] = rank
        return ranks

    def _build_plan(self, blocked, achievable):
        # type: (list) -> CraftPlan
        """组装 `CraftPlan`。**顺序全部显式排序**（`dict`/`set` 的遍历顺序不可依赖）。"""
        # 步骤顺序 = **执行顺序**：依赖（子键）排在消费者（父键）之前。
        # 用 `execution_ranks()` 而不是 `depth`——理由见那边的说明。
        ranks = self.execution_ranks()
        # **升序**：`rank` 小的（叶子、依赖）排前面，消费者的 `rank` 更大。
        # 降序会得到一份「反拓扑序」——看着像拓扑序（每条边都跨层），
        # 实际上正好把消费者排在了它的原料前面。
        ordered = sorted(
            self._steps,
            key=lambda key: (ranks.get(key, 0), _canon(key)),
        )
        step_index = {}
        steps = []
        for index, key in enumerate(ordered):
            step_index[key] = index
            data = self._steps[key].to_dict()
            data["index"] = index
            data["rank"] = ranks.get(key, 0)
            steps.append(data)

        materials = [row.to_dict(self.inventory) for row in self.ledger()]
        gaps = []
        for gap in sorted(
            self._gaps.values(), key=lambda item: (-item.depth, _canon(item.key))
        ):
            data = gap.to_dict()
            # 缺口数量**在这里现读**，而且用的是与 `blocked_keys` 同一把尺子
            # （`demand - achievable`）：用账本的 `outstanding` 的话，
            # 同一个键在两个字段里会给出两个不同的数，读的人只能猜哪个对。
            data["shortfall"] = self._ledger[gap.key].demand - achievable.get(gap.key, 0)
            gaps.append(data)

        truncated = bool(
            set(gap.cause for gap in self._gaps.values()) & GapCause.LIMIT_CAUSES
        )
        limits = {
            "truncated": truncated,
            "max_depth": self.policy.max_depth,
            "max_steps": self.policy.max_steps,
            "max_candidates": self.policy.max_candidates,
            "max_batches_per_step": self.policy.max_batches_per_step,
            # 三条上限各自有没有真的触顶，分开报——「被哪条截断的」决定了
            # 玩家该少要一点、还是该去查候选。
            "hit": sorted(
                set(gap.cause for gap in self._gaps.values()) & GapCause.LIMIT_CAUSES
            ),
        }
        estimates = {
            # 「槽位」这里算的是**派发位**（每个步骤一个）——它是给 08-04
            # 的搜索上限看的估计，**不是**并行槽位的计费口径：并行槽位是
            # 「每次配方执行占 1 槽」，由 `total_recipe_tasks` 那个数量级决定，
            # 而且真正领取它的是 09（见 `capacity.py`）。
            "slots": len(steps),
            "basis": u"每个步骤占一个派发位；内存见顶层的 required_bytes",
            # **估计值不是预留证明**（08-06 原文）。09 拿到计划之后要自己
            # 按 R04 复核槽位与内存，本模块的估算不构成任何占用。
            "authoritative": False,
            "policy": self.policy.describe(),
        }

        target_row = self._ledger[self.target]
        content = {
            "version": PLAN_FORMAT_VERSION,
            "target": {"key": _name(self.target), "canonical": _canon(self.target)},
            "amount": self.amount,
            "semantics": self.policy.target_semantics,
            # 三个数一起给（08-01 的「同一口径」）。
            "requested": self.amount,
            "held": target_row.from_stock,
            "produced": target_row.own_output,
            "status": self._status,
            "status_detail": self._status_detail,
            "steps": steps,
            "materials": materials,
            "gaps": gaps,
            "blocked_keys": blocked,
            "limits": limits,
            # --- 交给 09 的两个数（2026-09-26 新增）----------------------
            #: 这个订单要占住的字节数。**它是「按快照算的账」，不是预留**：
            #: 规则原文要求「准入时重新核对网络当前可用字节」，因此 09 不能
            #: 拿这个数直接扣，而是要用它去问当时的网络。
            "required_bytes": self.required_bytes(),
            #: 取整之前的那个数。留给诊断——「为什么是 41 不是 40」要答得出来。
            "required_micro_units": self.required_micro_units(),
            #: 叶子折算的逐键明细（`[{key, canonical, micro_units}]`）。
            #: 与 `materials` 是**两个口径**：那里记的是全量需求与产出，
            #: 这里只记「不由本计划产出、因此要占内存」的那部分。
            "leaf_materials": [
                {
                    "key": _name(key),
                    "canonical": _canon(key),
                    "micro_units": units,
                }
                for key, units in sorted(
                    self.leaf_units().items(), key=lambda item: _canon(item[0])
                )
            ],
            #: 配方任务总数（含重复执行次数）。**进度与槽位按它计**，
            #: 不是按 `len(steps)`——同配方跑 10 次要算 10 个任务。
            "total_recipe_tasks": self.total_recipe_tasks(),
            "estimates": estimates,
            # `counters["rounds"]` **刻意不进计划**：它数的是 `step()` 被调了几次，
            # 而预算怎么切正是验收第 4 条允许自由变化的那一维。把它放进内容里，
            # 一份同样的计划会因为「分两帧跑还是三帧跑」得到两个指纹——
            # 而那会让 09 的去重与 11 的「预览变了吗」全部失效。
            # 其余四个计数都是**按工作量**记的，与预算无关，留在计划里可供 14 核对。
            "counters": self.plan_counters(),
        }
        seal = PlanSeal(
            tree_text(canonical_tree(content)),
            self.inventory.version,
            self.inventory.digest(),
            self.patterns.topology_version,
            tuple(
                {
                    "step": step_index[key],
                    "key": key,
                    "node_key": self._steps[key].chosen.node_key,
                    "generation": self._steps[key].chosen.generation,
                    "slot": self._steps[key].chosen.slot,
                    "digest": self._steps[key].chosen.digest,
                    "mode": self._steps[key].chosen.mode,
                    "batches": self._steps[key].batches,
                    "runnable": self._steps[key].runnable,
                }
                for key in ordered
            ),
            tuple(
                (row.key, row.from_stock)
                for row in self.ledger()
                if row.from_stock > 0
            ),
        )
        return CraftPlan(content, seal)


def _merge_input_stacks(stacks):
    # type: (tuple) -> tuple
    """把输入条目按资源键合并成 `((键, 每批数量), ...)`，按资源身份排序。

    **`CraftStep.input_pairs()` 与候选递归预筛必须走这一个函数。** 合并本身
    不是优化（理由见 `input_pairs` 的说明）；对预筛来说它还多一层意义：
    预筛与走查问的必须是**同一个输入集合**，否则会出现「预筛放行、走查拒绝」
    ——而走查拒绝时那个步骤的批次与预计产出早已提交，无法撤销。
    """
    totals = {}
    for stack in stacks:
        totals[stack.key] = totals.get(stack.key, 0) + stack.count
    return tuple((key, totals[key]) for key in sorted(totals, key=_canon))


def _yield_for(definition, key):
    # type: (object, object) -> int
    """一份配方每批次产出多少个 `key`。同一键在 `outputs` 里出现多行时**相加**。

    做成模块级函数（`CraftStep.yield_per_batch` 委托它）是因为 `_select`
    要在**建步骤之前**问这个问题：一份候选说它产出某个键、内容里却没有时，
    那一步压根不该被建出来——建完再判会留下一张 0 批次的空步骤，
    而它会进计划、进 `dispatch`，09 拿到一条「派 0 批」的凭据。
    """
    total = 0
    for stack in definition.outputs:
        if stack.key == key:
            total += stack.count
    return total


def _blocker_text(candidates):
    # type: (tuple) -> object
    """把一组候选的阻塞原因并成一句能读的话。"""
    parts = []
    for item in candidates:
        blocker = item.blocker or {}
        detail = blocker.get("detail") or blocker.get("code") or "原因未知"
        text = u"%s：%s" % (item.device_id, detail)
        if text not in parts:
            parts.append(text)
    if not parts:
        return u"没有可用的提供者"
    return u"；".join(parts[:3])


# -- 门面 ------------------------------------------------------------------


class Planner(object):
    """规划服务的门面：把两份快照的采集与策略绑在一起。

    启动层持有它的实例并交给 09；09 不需要知道快照怎么采集。
    写成门面而不是一堆自由函数，是因为**策略必须与实际生效的那一份是同一个对象**
    （与 `SchedulerPolicy`/`EnergyPolicy` 同一条纪律）：各存一份的代价是
    `unresolved_report()` 报出来的数值与真正用的对不上。
    """

    def __init__(self, definitions=None, policy=None, pattern_policy=None, work_limit=None):
        # type: (object, object, object, int) -> None
        self.definitions = definitions
        self.policy = policy if policy is not None else PlanPolicy()
        self.pattern_policy = (
            pattern_policy if pattern_policy is not None else PatternPolicy()
        )
        #: `solve()` 的总工作量上限。它是**防呆**（调用方忘了循环 step 时
        #: 别在一帧里跑爆），不是玩法数值。
        self.work_limit = (
            int(work_limit) if work_limit is not None else DEFAULT_SOLVE_WORK_LIMIT
        )
        self.stats = {"jobs": 0, "solved": 0, "cancelled": 0, "work": 0}

    def capture_inventory(self, storage, topology_version=None):
        # type: (object, object) -> InventorySnapshot
        return InventorySnapshot.capture(storage, topology_version)

    def capture_patterns(self, store, topology_version=None):
        # type: (object, object) -> PatternSnapshot
        return PatternSnapshot.capture(store, topology_version)

    def plan(self, target, amount, inventory, patterns):
        # type: (object, int, object, object) -> PlanJob
        """建一个规划任务（**不推进**）。分帧推进由调用方 `step()`。"""
        self.stats["jobs"] += 1
        return PlanJob(
            target,
            amount,
            inventory,
            patterns,
            policy=self.policy,
            pattern_policy=self.pattern_policy,
        )

    def solve(self, target, amount, inventory, patterns, work_limit=None):
        # type: (object, int, object, object, int) -> CraftPlan
        """一次性算完，返回计划；**被取消或超出工作量上限时返回 `None`**。

        `None` 与「计划本身是空的」是两件事：`HOLD` 口径下库里已经够了会给出
        一份**零步骤但 `COMPLETE`** 的计划，那不是失败。因此调用方拿到 `None`
        要去看 `job.status()`——这也是这里返回计划而不是任务的原因：
        想要进度就自己建任务。
        """
        job = self.plan(target, amount, inventory, patterns)
        limit = self.work_limit if work_limit is None else int(work_limit)
        while job.status() == PlanStatus.RUNNING:
            # **每次把剩余额度算进这一块的预算里**，而不是先跑 64 再看超没超：
            # 单项的代价可以大于 1（一次 `_select` 就要 1 + 候选窗口那么大），
            # 因此「先跑再检查」会超出上限一个项的量。超出一点点无害，
            # 但会超到什么程度取决于图，而这里的语义应当是「大致有界」。
            remaining = limit - job.spent()
            if remaining <= 0:
                job.cancel(u"一次性求解超出工作量上限 %d" % (limit,))
                break
            job.step(64 if remaining > 64 else remaining)
        self.stats["work"] += job.spent()
        if job.status() == PlanStatus.CANCELLED:
            self.stats["cancelled"] += 1
            return None
        if job.status() == PlanStatus.RUNNING:  # pragma: no cover - 防御性
            job.cancel("一次性求解没有收敛")
            self.stats["cancelled"] += 1
            return None
        self.stats["solved"] += 1
        return job.result()

    def diagnostics(self):
        # type: () -> dict
        return {
            "jobs": self.stats["jobs"],
            "solved": self.stats["solved"],
            "cancelled": self.stats["cancelled"],
            "work": self.stats["work"],
            "work_limit": self.work_limit,
            "policy": self.policy.describe(),
            "unresolved": self.policy.unresolved_report(),
            "scope_note": (
                "合成规划已实现：批次取整与余量、库存优先、多级依赖与共享子需求、"
                "环检测与有界失败、可分帧推进与取消、以及提交前的复核凭据。"
                "**它是只读的**：不扣库存、不预留、不占真实槽位——"
                "估算出的槽位与内存不是占用证明（08-06）。"
                "真正的订单提交与派发属任务 09，不在这里。"
            ),
        }

    def __repr__(self):
        return "Planner(%r)" % (self.policy,)


def plan(target, amount, inventory, patterns, policy=None, pattern_policy=None):
    # type: (object, int, object, object, object, object) -> PlanJob
    """模块级快捷入口：建一个规划任务（与任务文档里的签名一致）。"""
    return PlanJob(
        target,
        amount,
        inventory,
        patterns,
        policy=policy,
        pattern_policy=pattern_policy,
    )
