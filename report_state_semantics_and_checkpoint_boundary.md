# myFab 补充审计：路线下标、物理阶段与无 Pending Action 保存边界

日期：2026-10-16。承接[前一轮状态审计](D:/Project/micron/myFab/report_transformer_state_audit.md)。本轮只新增研究脚本、证据与本报告；未修改 Engine、Checkpoint 工具、策略、模型数据库或既有 checkpoint。

## 1. 判断摘要

**`operation_index` 提前推进本身是既有阶段设计，不应整体认定为错误。**原生全厂 WIP 主统计、派工资格、运输起点和设备释放，在本轮检查及对照流程中正确保留了额外条件；没有发现这四条主链因下标提前而必然发生误计、提前派工、错误搬运或提前释放。

但消费这些字段的代码并非始终区分两种语义，本轮确认了三个实际问题：

| 编号 | 确认的问题 | 影响范围 | 证据 |
|---|---|---|---|
| B1 | Super Hot 预留使用 Tool 的 `active_step_id` 推断阶段，但卸载时该字段已被更新为下一工步；暂停时又被清空 | 实际派工、设备软预留和 Super Hot 等待时间 | 原生三工步例中，SH 应接着使用的设备被普通 Lot 抢占，SH 到下一工步的开工从规则预期的 t=15 推迟到实际 t=112；真实 Day240 续跑也观测到阶段误判 |
| B2 | CQT 以 `operation_index == target && in_process` 当作目标已开始，漏掉“仍在上一工序卸载”的情况 | CQT violation 统计；当前规则不直接改变物理路线 | 原生例中 deadline=t=12，目标 t=15 才开工，插件没有记录 violation |
| B3 | checkpoint 新观察器用 `not lot.completed` 初始化 WIP；该属性在最后工序加工完成、尚未卸载时已经为 True | 恢复后的 WIP 积分和初始 WIP；不改变保存的物理状态 | 在合法 pending-release 时刻 t=12，连续观察器 WIP=1，新观察器 WIP=0，而 Lot 的 `end_time=None`、卸载到 t=15 才结束 |

证据为未改业务代码、完全内存构造的原生 Engine 执行结果，见 [native_small_models.json](D:/Project/micron/myFab/research/state_boundary_audit_20261016/native_small_models.json)。B2/B3 的反例证明配置空间中存在真实缺陷，不代表已测得它们在 HVLM 长期运行中的发生率。现有三个 Day240 起点未捕获末工步卸载，未证明这些起点已受到 B3 影响。

**无 Pending Action 的一致边界可以建立。**这三个 Day240 快照在同一仿真时刻执行已有 SMT2020 动作后，恰好已经没有同刻事件，下一事件均严格晚于当前时刻；内存序列化往返及六小时续跑均与原流程一致。但现有 `save_checkpoint()` 明确拒绝 `_pending is None` 的状态，现有保存/加载摘要也绑定 SMT2020 策略，尚不能直接作为通用跨策略 checkpoint 接口。

## 2. 路线推进与物理状态的具体调用路径

### 2.1 正常阶段链

```text
_apply_decision
  → 校验 _is_eligible
  → 移出 ready，Lot.in_process=True，记录 processing_tool_id
  → _start_phase(LOAD_COMPLETE)
  → _after_load [必要时 SETUP_COMPLETE → _after_setup]
  → _start_process(OPERATION_COMPLETE)
  → _after_operation
       先通知 on_operation_completed（此时仍是刚完成的路线下标）
       记录 last_tool_group_id、processed_tool_by_step、LTL、CQT
       operation_index += 1
       _start_phase(UNLOAD_COMPLETE, finished_step=刚完成工步)
  → _after_unload
       删除该 Tool/token 活动
       Lot.in_process=False，processing_tool_id=None
       完工，或返工，或 _continue_route（抽检跳步）
       _schedule_transport_for_lot（ready_time=inf）
       刷新原 Tool 容量并处理待维护
  → _after_transport
       ready_time=current_time → _mark_ready
  → 当前目的组派工候选 → 策略选择
```

位置：[动作校验](D:/Project/micron/myFab/fab/engine/engine.py:518)、[装载/换型/加工](D:/Project/micron/myFab/fab/engine/engine.py:610)、[工序完成](D:/Project/micron/myFab/fab/engine/engine.py:968)、[卸载完成](D:/Project/micron/myFab/fab/engine/engine.py:1021)、[抽检与继续路线](D:/Project/micron/myFab/fab/engine/engine.py:1260)、[运输](D:/Project/micron/myFab/fab/engine/engine.py:1325)。

| 阶段 | `operation_index/current_step` | Lot 其他状态 | 物理位置/占用的可靠依据 |
|---|---|---|---|
| 装载、换型、加工 | 正在执行的工步 | `in_process=True`，有 `processing_tool_id` | `_active` 的 Tool/token、阶段及 batch 成员 |
| 工序完成后的卸载 | 已指向下一名义工步，末步时可等于路线长度 | 仍 `in_process=True`，仍有原 `processing_tool_id` | `_active.completion=UNLOAD_COMPLETE` 和 `finished_step`，而非 `current_step` |
| 暂停 | 路线下标不因暂停而改变；卸载暂停仍已指向下一步 | Lot 仍保留加工占用关联，Tool 转 down | `_paused` 的原阶段、成员、remaining；Tool 的代表性字段被清空，不能单靠它判定 Lot 阶段 |
| 运输 | 下一道实际需要执行的工步，可能已跳过抽检工步/退回返工目标 | `in_process=False`、`processing_tool_id=None`、`ready_time=inf` | `last_tool_group_id` 是起点，目标是当前路线工步；日历运输事件表示到达 |
| 到站排队 | 待加工工步 | `ready_time<=now`，位于 ready 索引 | 目标组 ready 索引及绑定资格 |

`Lot.completed` 仅表示 `operation_index >= len(route)`；`end_time` 才在整 Lot 最终卸载/完工结算时写入。[属性定义](D:/Project/micron/myFab/fab/model/entities.py:302)、[整 Lot 完成](D:/Project/micron/myFab/fab/engine/engine.py:1291)

### 2.2 四条主链的核对结论

| 检查对象 | 当前代码如何避免混淆 | 本轮判断 |
|---|---|---|
| 全厂 WIP 主指标 | `OnlineMetricsPlugin` 在准入时加入 ID，在 `on_lot_completed` 才移除，按实际事件间隔积分；不因 `operation_index` 变化移除 | 没有发现主指标在卸载期提前扣 WIP。checkpoint 新观察器初始化另有 B3 |
| 工序操作记录 | `_after_operation` 先调用插件，再加路线下标；`TracePlugin` 此时读到刚完成工步的 process/index | 没有发现操作记录整体偏移一工步 |
| 派工资格 | `_is_eligible` 同时检查 Tool 容量/停机、Lot `in_process/completed/ready_time`、当前 ready 组、目标工具组和绑定；不能仅因下一 `current_step` 匹配就派工 | 卸载中被 `in_process` 拦截；运输中被 `ready_time=inf` 和 ready 索引拦截；属于正确的多条件资格判断 |
| 运输起点 | `_after_operation` 保存刚实际加工的 `last_tool_group_id`；抽检/返工更新目的路线位置；`_transport_time` 读取保存起点和新目的地的 location 对 | 不使用 `route[index-1]` 冒充实际起点，跳步路径可正确运输 |
| 设备释放 | `_after_unload` 按原事件的 `tool_id/token` 删除 `_active/_active_by_tool`，再 `_refresh_tool_capacity`；`_current_activity` 不依赖 `Tool.active_step_id` | 没有发现下标提前导致原设备错误释放。级联设备有剩余槽位时可提前接下一 Lot，是容量语义，不是整个活动被提前删除 |
| 完工对象清理 | `_drop_completed_lots` 依 `_completed_pending` 与 `end_time` 删除，不是扫描 `Lot.completed` 一律删除 | 最后工序正在卸载的 Lot 不会因此提前移出业务容器 |

对应位置：[WIP 插件](D:/Project/micron/myFab/fab/plugins/results.py:43)、[操作 Trace](D:/Project/micron/myFab/fab/plugins/trace.py:38)、[资格](D:/Project/micron/myFab/fab/engine/engine.py:1728)、[运输起点](D:/Project/micron/myFab/fab/engine/engine.py:1873)、[活动查找及设备容量](D:/Project/micron/myFab/fab/engine/engine.py:1439)、[完成清理](D:/Project/micron/myFab/fab/engine/engine.py:1301)。

Tool Group 的物理 WIP 必须按活动的服务组与运输目的地区分。前一轮 G0 明确命名的是“路线目的组计数”，不是原生业务中的物理位置指标；这类研究特征的口径差异本身不是 Engine Bug。DynamicDBR 的 layer/剩余加工负荷使用路线位置，队列 buffer 另用 `in_process/ready_time` 过滤；它们不是同一口径的物理位置 WIP，不能仅因卸载时跨 layer 就判错。[DBR 索引说明](D:/Project/micron/myFab/strategy/dbr_v2.py:424)

### 2.3 正常流转的原生对照证据

内存模型：A 装载 2、加工 10、卸载 5；中间 B 抽检概率 0；C 装载 1、加工 20、卸载 3；A→C 运输 7，故意给 B→C 配置 99 来区分起点。

| 事件/结果 | 原生实际结果 |
|---|---|
| A 加工 | t=2→12，操作记录 step=0 |
| A 卸载、运输 | 卸载到 t=17；C 到达 t=24，证明运输采用 A→C 的 7，而不是跳过的 B→C 的 99 |
| C 加工 | t=25→45，操作记录 step=2；没有在卸载或运输期提前开工 |
| 整 Lot 完工 | t=48，最后卸载完成后结算 |
| 100 minute 区间平均 WIP | 0.48，包含前后两次卸载及运输 |
| B 设备加工量 | 0 wafer，抽检跳过后未误用 B |

这是具体对照路径的验证，不是对所有 batch/级联/故障组合的形式证明。[对照实现](D:/Project/micron/myFab/research/state_boundary_audit_20261016/probe_paths.py:202)、[结果](D:/Project/micron/myFab/research/state_boundary_audit_20261016/native_small_models.json)

## 3. 已确认的消费端业务问题

### 3.1 B1：卸载阶段的 Super Hot 预留多跳一步

触发路径：

```text
_after_operation: Lot.operation_index 从 i 增到 i+1
  → _start_phase(UNLOAD_COMPLETE)
      tool.active_step_id = lot.current_step.id   # 写成 i+1 的 ID
  → 同刻或后续时刻有策略请求
  → SMT2020.decide → _update_reservations → _reservation_step_index
      Lot.in_process=True，且 tool.active_step_id == current_step.id
      因此再次 index += 1，误判成 i+2
  → 取消原先的 i+1 设备预留，改留 i+2 或不再预留
```

关键冲突不是 `operation_index` 的语义，而是 `_reservation_step_index` 注释明确假设“卸载时设备活动工步与 current_step 不同”，实际写入却相同。[写入点](D:/Project/micron/myFab/fab/engine/engine.py:597)、[推断点与注释](D:/Project/micron/myFab/strategy/smt2020_baseline.py:430)、[预留更新](D:/Project/micron/myFab/strategy/smt2020_baseline.py:396)

原生反例：SH 路线 `UP(S0)→TARGET(S1)→NEXT(S2)`。UP 加工 t=0→10、卸载到 t=15；普通 Lot 在 t=12 到达 TARGET，需加工 100 minute。

- t=12，SH 仍在 UP 卸载：`operation_index=1`、`finished_step=S0`、`tool.active_step_id=S1`。
- 应继续保留 TARGET；策略实际得到 index=2，转留 NEXT。
- 同次 release 的 follow-up 把普通 Lot 派给 TARGET，t=12→112。
- SH 在 t=15 可到 TARGET，却只能在 t=112 开工。按预留规则，TARGET 应等 SH 到达；这里发生了可观测的业务行为变化。

没有运行修改后的策略来宣称修复效果；t=15 是规则和本模型时长给出的预期，不是修复版实测。[完整决策与操作轨迹](D:/Project/micron/myFab/research/state_boundary_audit_20261016/native_small_models.json)

**相关风险：暂停和级联。**`_begin_downtime` 清空 Tool 的 `active_step_id`，但暂停加工 Lot 的下一设备仍应基于其真实活动阶段决定；按代表 Tool 字段推断会退回当前工步。级联设备还可能同时有多个不同 Lot/工步，一组代表字段不能可靠表达每个 Lot 的阶段。本轮真实续跑观察到了暂停加工、装载和卸载时的预留下标差异；不应只修一处 unload 赋值就声称覆盖了暂停/并发。[暂停处理](D:/Project/micron/myFab/fab/engine/engine.py:1062)

Day240 后约六小时的原生阶段观测：

| seed | 卸载阶段的下标差异观测数 | 加工/装载阶段的差异观测数 |
|---|---:|---:|
| 2026061700 | 341 | 6319 |
| 2026061701 | 531 | 158 |
| 2026061702 | 240 | 136 |

这些是在 `on_time_advanced` 中、事件处理前对存活 SH Lot 做的重复阶段观测，**不是不同 Bug 次数、不是实际策略调用次数，也不是吞吐损失率**；加工差异包含暂停状态。例：seed1701 在 t=345671.550532，Lot E011974 卸载中，应预留 Litho_BE_110，helper 却指向 LithoMet_BE_18。seed1700 暂停加工例中 `active_step_id=None`，helper 返回当前工步而非下一工步。[真实样本](D:/Project/micron/myFab/research/state_boundary_audit_20261016/day240.json)

### 3.2 B2：CQT 把上一工步卸载误当成目标已开始

`_record_cqt_violation` 的当前判断是：路线下标未到 target，或下标等于 target 且 `not in_process`，才记录 violation。上一工序完成后，下标已到 target，但卸载中的 `in_process=True`，因此不满足记录条件。[CQT 判断](D:/Project/micron/myFab/fab/engine/engine.py:1309)

原生反例：A 加工到 t=10，要求目标 B 在 2 minute 内开始，CQT 事件在 t=12；A 卸载到 t=15，B 实际 t=15 才开始。启用了原生 `CqtViolationPlugin`，结果记录 0 次 violation。这里不存在 deadline 同刻事件先后或“装载是否算开始”的边界争议：t=12 时 B 连装载也没开始，Lot 仍在 A 卸载。

影响限于当前 CQT 统计通知链；不据此声称 Engine 会因此自动修改路线或 SMT2020 优先级。前一轮 G2 的 CQT “already_satisfied” 分类复用了原引擎判断，因此只是代码行为摘要，不能当作所有样本都已真实满足 CQT 的证明。[反例实现](D:/Project/micron/myFab/research/state_boundary_audit_20261016/probe_paths.py:242)

### 3.3 B3：恢复观察器漏算最后工序卸载中的 WIP

原生连续统计按完整生命周期移除 Lot，正确。问题出在 checkpoint adapter 的 `MetricsObserver.initialize_restored`：

```text
物理：末步加工结束 → operation_index=len(route) → 正在卸载 → end_time=None
Lot.completed：True
initialize_restored：筛掉 completed=True 的 Lot
结果：新观察器提前少计一个 WIP，直到真正卸载完成也无法补回这段积分
```

本轮在一个原生 external 到达模型里，t=12 恰好获得合法 `release_decision_pending` 边界：首 Lot 最后加工 t=10 结束，卸载到 t=15，第二 Lot 在 t=12 到达池中。连续观察器 WIP ID 集合含首 Lot，新建观察器为空。后续会漏掉首 Lot t=12→15 的 3 lot-minute WIP 积分。[恢复初始化](D:/Project/micron/myFab/checkpoint_tools/runtime.py:230)、[复现](D:/Project/micron/myFab/research/state_boundary_audit_20261016/probe_paths.py:257)

这是 adapter 统计问题，不是 payload 丢失或原生全厂 WIP 算法错误。既有跨进程哈希认证证明恢复忠实于旧实现，不等于证明每条业务规则无误。原 Day240 三个保存点没有这个末步卸载样本，不能将此反例解读成此前三份 Day240 的物理状态或历史预热结果已被判无效。

## 4. 无 Pending Action 的一致边界是否存在

### 4.1 当前 pending 的来源和动作执行顺序

`advance_until_release()` 先处理完当前日历堆里全部同刻事件，然后生成一次 release 决策写入 `_pending` 并返回。**这是“已有决策、尚未执行”的边界，不是尚未询问任何策略的中立物理快照。**策略的 blocked 计时、SH 预留等记忆可能已在 `decide` 中变化。

`apply_release_action(None)` 的顺序是：

1. 取出并清空 `_pending`。
2. 执行该决策的 dispatch，登记 wakeup。
3. 按该决策的 ID/布尔请求从 pool 准入。
4. 请求 follow-up 派工并执行，登记其 wakeup，然后返回。

因此该函数返回后，已有那份决策及 follow-up 动作已经执行；但这些动作可能新建 `time == current_time` 的 LOAD/SETUP/OPERATION/UNLOAD/TRANSPORT 事件。[事件循环](D:/Project/micron/myFab/fab/engine/engine.py:299)、[执行顺序](D:/Project/micron/myFab/fab/engine/engine.py:360)

### 4.2 “None” 不够，必须完成同刻闭包

建议定义的边界为：**当前时刻所有原生事件结算及其诱发的策略动作均完成，尚未开始下一严格更晚时刻的事件处理。**至少要求：

```text
_pending is None
不在 _handle_event / _decide / _apply_decision / apply_release_action 调用栈中
未结束；没有过去事件
calendar 为空，或 min(calendar).time > current_time
本时刻触发的原生策略请求已按原逻辑执行
业务对象、活动、暂停、索引/dirty 通知和 RNG 引用图完整
```

这里的“闭包”是执行现有引擎规定的同刻处理，**不是反复强行调用策略直到所有 idle 设备都开工**。策略可以合法地不派工，dirty 候选或 `changed_lots` 也不必全为空；保存它们等待原生下一次消费是有效状态。更不能为了看起来干净，先调用 `_state()` 清掉通知或 `_drop_completed_lots()` 丢掉新策略/原策略尚未消费的增量。

原生零时长反例：t=0 到达、装载/加工/卸载均为 0。`apply_release_action` 返回时 `_pending=None`，但日历头仍是 t=0 的 `LOAD_COMPLETE`；继续原生循环后才完成整 Lot，日历头变为 t=160 的结束事件。后者仍可有 `_completed_pending=1`、容器 `len(lots)=1`，但该 Lot 已 `end_time=0`，真实厂内 WIP=0。这说明一致性不等于所有内部容器都已经清理，manifest 的物理 WIP 也不能在任意新边界一律取 `len(lots)`。[零时长证据](D:/Project/micron/myFab/research/state_boundary_audit_20261016/native_small_models.json)

### 4.3 三个现有 Day240 文件的实际结果

在独立 RAM 实例中，按原生流程执行已有 pending 决策及 follow-up；不改动作、不重求旧决策，也不更改当前时钟。三个实例都已满足同刻闭包，无需再处理零时长事件：

| seed | 时刻 minute | 执行前 WIP / pool | 执行后 WIP / pool | 下一事件时刻 | pending | 当前时刻事件 |
|---|---:|---:|---:|---:|---|---:|
| 2026061700 | 345651.03 | 2102 / 2 | 2104 / 0 | 345651.056347 | None | 0 |
| 2026061701 | 345651.03 | 2245 / 2 | 2247 / 0 | 345651.042615 | None | 0 |
| 2026061702 | 345651.03 | 2130 / 2 | 2132 / 0 | 345651.072775 | None | 0 |

这是执行了旧策略动作后的物理状态；**它不等于原 checkpoint 中那份动作执行前的 Fab 状态**。三个 seed 均放行了 E013732/E013733；seed1702 还留下 5 个合法的 changed Lot 通知，不能因其非空认定状态不一致。

进一步在这个边界将业务对象图做 RAM pickle 往返（排除观察器/存储资源，保留别名、RNG、序号），指纹全部相同；原生继续约六小时，与从原 pending 文件正常执行所得末态的 44 组 fingerprint 全部相同。对齐记录起点后，投料、工序完成、整 Lot 完成轨迹哈希也全部相同：

| seed | 边界之后的新投料 | 工序完成 | 整 Lot 完成 | 轨迹及末态一致 |
|---|---:|---:|---:|---|
| 2026061700 | 12 | 5990 | 15 | 是 |
| 2026061701 | 12 | 6083 | 23 | 是 |
| 2026061702 | 12 | 5877 | 20 | 是 |

原 pending 边界起算的完整轨迹各多 2 次投料，因为那两次已发生在本次无 pending 边界之前；证据文件同时保存前缀完整轨迹和新边界之后的轨迹，未混用统计起点。[Day240 证据](D:/Project/micron/myFab/research/state_boundary_audit_20261016/day240.json)

**本轮没有写出或认证新的 `.fabcp`。**RAM 往返验证证明这个保存位置可成立，不等于现有格式已经支持它。

### 4.4 为什么当前接口仍不能直接通用保存

| 现有接口/实现 | 限制 | 结果 |
|---|---|---|
| `advance_until_release()` | 公开返回点主要是 `_pending` release 决策或结束，没有“处理完本 tick 即返回”的公共接口 | 对任意目标时间取得无 pending 闭包边界，需要新暂停入口或外部适配 |
| `apply_release_action()` | 返回后可能留下同刻事件 | 不能普遍将其返回点无条件标记为一致边界；本次三份恰好满足 |
| `save_checkpoint()` | 要求原生未结束 Engine 且 `_pending is not None`；manifest stage 固定 `release_decision_pending` | 现有函数明确拒绝这里测得的状态，不能仅在报告里换个 stage 名称 |
| `fingerprint()` | 无条件读取 `_reservations/_blocked_since/_blocked_wakeup/_now/_wakeups/_super_hot` | 绑定 SMT2020；换 FIFO 后会 AttributeError |
| `load_checkpoint()` | 无条件清理 `_strategy._expected_setup_cache` | 也包含 SMT2020 特定恢复假设 |
| `resume()/advance_to()` | 默认认证索引和停止语义仍是旧阶段；保存内容与 source hash/旧格式绑定 | 需要明确新阶段、旧格式兼容和认证/迁移语义 |

位置：[保存约束及 stage](D:/Project/micron/myFab/checkpoint_tools/runtime.py:121)、[指纹](D:/Project/micron/myFab/checkpoint_tools/runtime.py:82)、[加载](D:/Project/micron/myFab/checkpoint_tools/runtime.py:170)、[续跑](D:/Project/micron/myFab/checkpoint_tools/runtime.py:200)、[认证路径](D:/Project/micron/myFab/checkpoint_tools/__main__.py:172)。

本轮为观测零时长闭包，用 Python tracing 在**未修改的原生循环即将给下一严格更晚事件赋时钟之前**停止；同刻事件仍由原方法处理。它只是一种研究验证手段，不是建议的生产保存接口；不应复制一套私有事件循环长期维护。[观测门](D:/Project/micron/myFab/research/state_boundary_audit_20261016/probe_paths.py:284)

## 5. 无 Pending Action 还不等于可以直接切任意策略

### 5.1 哪些状态应该保留，哪些属于旧策略

| 状态 | 切策略建议 |
|---|---|
| 已准入 Lot、队列/在途、绑定/返工状态、设备 setup、加工/装卸/暂停、已冻结批次、维护队列 | 保留。这些是切换点已经形成的物理状态，不能撤销旧策略已执行的动作 |
| 加工/运输/维修完成、故障、PM、来源下一次到达、CQT、horizon、RNG 和事件排序 | 保留相应物理/外生事件。不要重新 `begin_episode` 或调用来源 `initial_events`，否则重复造事件、重置时钟/初态 |
| `_pending` | 新边界必须为空，不携带旧策略尚未执行的动作 |
| 旧策略 `_reservations/blocked timers/DBR bottleneck memory` | 与“相同物理 Fab 状态”分开。新策略需要规定自己的初始化语义；同策略精确续跑应保留旧记忆，跨策略冷启动不能随意套用旧内存 |
| 旧策略安排的 `STRATEGY_WAKEUP` | 应按所有权清理/替换；现有事件只有 reason，没有通用 policy epoch，需明确识别规则。不得统一删除 PM、维修或其他物理未来 |
| `_release_on_arrival` 和 `LOT_RELEASE` 节拍 | 必须按新策略重新接好触发协议；SMT2020 到达即投料，而 FIFO/EDD/CR/DBR 默认使用节拍 |
| 增量索引与新策略首个输入 | 保留 Engine 物理索引/dirty 状态，首个新策略快照需完整当前 Lot 上下文；例如新 SMT2020 若只看到最近 changed Lot，会漏掉已有 SH Lot 的登记 |
| 观察器 | 新建测量窗口并从真实 WIP 初始化；与物理/策略重置分开。B3 也需要修正，避免新边界复用错误过滤条件 |

原 Day240 seed1701/1702 在旧动作完成后仍各有 6/4 个旧 `STRATEGY_WAKEUP`。即使没有 pending，它们也可能在新策略下引发额外决策，不能把整个日历都叫作与策略无关的物理状态。[边界清单](D:/Project/micron/myFab/research/state_boundary_audit_20261016/day240.json)

### 5.2 已实测的不完整切换会发生什么

在 seed1700 的无 pending 边界，只在 RAM 中初始化 FIFO、替换 `_strategy`，并把 `_release_on_arrival=False`，但不安排 FIFO 的 `LOT_RELEASE` 节拍。约六小时后，**新增投料 0，release pool 积累 12 个 Lot**；外部订单仍在到达。这是触发协议未接好导致的业务断链，不是 FIFO 自身选择不投料。

同一副本调用既有 `fingerprint`，实际报错：`'FIFOStrategy' object has no attribute '_reservations'`。如果只替换策略而连 `_release_on_arrival` 都不改，则会反过来保留 SMT2020 的到达触发语义，也不能宣称是原定义的 FIFO 节拍实验。[切换反例](D:/Project/micron/myFab/research/state_boundary_audit_20261016/naive_policy_switch.json)

### 5.3 公平起点的两种语义必须选清楚

- **本题要求的旧动作完成后分叉**：所有新策略从同一份 `S(t, after SMT2020 actions)` 物理状态开始，各自建立策略内存、控制日历和投料协议。已经开始的活动继续执行，不回滚。这个方案可行，也正是本次 Day240 实测的边界。
- **希望在 t 对同一批刚到达订单重新决策**：需要“同刻物理事件完成、尚未调用旧策略”的另一种边界。当前文件已调用旧策略，不能简单 `_pending=None` 来还原此状态；旧策略内存已经变化，也不能把旧动作执行后的位置称为决策前的位置。

同一初始物理状态不表示不同策略会获得各自稳态。保留同一 RNG 起点也不保证每个 Lot 都获得相同随机时长，因为后续随机数消耗顺序会随派工变化。以上影响实验解释，尚不需要在本轮改造随机流或重新建立 checkpoint。

## 6. 最小修改范围建议（本轮均未实施）

### 6.1 业务语义问题应独立修复

| 项目 | 最小涉及位置 | 建议边界与回归检查 |
|---|---|---|
| B1 预留判阶段 | Engine 的阶段元数据/策略快照，`SMT2020._reservation_step_index` | 提供按 Lot 关联的真实阶段/执行工步；卸载读 finished_step，暂停读 paused，级联按具体 Lot 活动查。只修 `Tool.active_step_id` 一行不能覆盖暂停和并发。新增 unload/paused/cascading 预留回归 |
| B2 CQT | `Engine._record_cqt_violation` 与可复用的按 Lot 活动阶段查询 | 区分“上一工步卸载”和“目标工步确已开始”；明确开始口径后覆盖加载、setup、加工、暂停及 deadline 同刻次序。无需整体推迟路线下标 |
| B3 恢复 WIP | `checkpoint_tools.runtime.MetricsObserver.initialize_restored`，以及新边界 manifest 的 WIP 口径 | 依据已准入 Lot 的 `end_time is None` 等生命周期条件初始化，覆盖末工序卸载和 `_completed_pending` 两个相反边界。无需更改正常 Engine WIP 主算法 |

可以由 Engine 统一提供一个只读的“每 Lot 活动阶段/执行工步”投影给策略，复用 `_active/_paused`；不一定新增一套独立动态状态机。若改实体字段或快照协议，则要考虑旧 payload 兼容。不能通过整体把 `operation_index += 1` 移到运输之后来处理，后者会波及返工、抽检、CQT、候选与原轨迹，修改范围明显更大。

### 6.2 仅针对这三份 Day240 转换，最小可以不改 Engine

新增 adapter：使用旧 loader 读文件 → 原生 `apply_release_action(None)` → 验证 `pending=None` 且日历头严格晚于当前时刻 → 用新阶段格式保存。此三份文件已证明无需额外同刻结算，因此 **Engine 和策略可以保持原样**；保存器仍需新增支持无 pending stage 的入口、对应 WIP/指纹/兼容校验和恢复验证。

该适配器应对不满足条件的快照拒绝转换或交给新通用推进 API，不能用“Day240 恰好成立”推广到所有文件。本轮只进行了内存转换验证，没有实现/发布这一保存器。

### 6.3 要支持任意时刻、任意策略的通用方案

| 改动层 | 最小职责 | 可否保持现有默认行为 |
|---|---|---|
| Engine 新暂停入口/可选 hook | 在下一 tick 赋时钟前返回；完成此前同刻事件、release/follow-up/dispatch；检查闭包，不引入额外策略调用 | 可以。旧 `run/advance_until_release` 默认路径保持语义；新入口独立选择保存点 |
| checkpoint adapter 新阶段 | 例如显式 `after_tick_actions`；允许 pending=None；正确保存 dirty/changed/completed-pending、RNG/序号/别名，区分物理 WIP 与容器长度 | 可以版本化新增，不应硬改旧文件的 stage 标签 |
| 策略状态适配 | 将通用 Engine 指纹与策略专属状态分开；去掉加载器对 SMT2020 缓存属性的无条件假设；同策略重放与跨策略初始化两种模式分别定义 | 需要新增适配/注册逻辑，不能只放宽 pending 检查 |
| Handoff 协议 | 初始化新策略、完整首快照、旧控制事件所有权、投料触发重接、首次决策时点与观察器重置 | 可以在 adapter/service 层承担；无需重建物理 Fab 或重新投初始 WIP |
| 测试 | 零时长闭包、多轮同刻决策、暂停/批次/级联、completed-pending、fresh-process 往返、旧策略回放、新策略投料节拍/计时器 | 必须覆盖新边界；本轮 RAM 往返尚非新文件格式的跨进程认证 |

候选 hook 的关键位置是 `advance_until_release` 将 `self.current_time` 赋为下个事件时间之前，而不是 `on_decision_made` 插件回调（该时点动作还没执行），也不是任意事件回调栈中。当前 tick 所有原生调用已返回后才允许序列化。[循环位置](D:/Project/micron/myFab/fab/engine/engine.py:305)、[决策回调位置](D:/Project/micron/myFab/fab/engine/engine.py:421)

既有 checkpoint 对 Engine/策略/checkpoint_tools 源码做精确哈希校验。将来实施任一修复后，旧 loader 会拒绝来源不匹配的文件；应明确保留旧版本运行环境或做有验证的格式/状态迁移，不能直接绕过校验。修复策略行为后的稳态结论也需重新验证，不能仅靠旧版本恢复认证外推。

## 7. 本轮验证范围与产物

新增目录：[research/state_boundary_audit_20261016](D:/Project/micron/myFab/research/state_boundary_audit_20261016)。

- [probe_paths.py](D:/Project/micron/myFab/research/state_boundary_audit_20261016/probe_paths.py)：原生内存模型、边界观测、RAM 往返与不完整切换验证；没有猴子补丁或业务方法替换。
- [native_small_models.json](D:/Project/micron/myFab/research/state_boundary_audit_20261016/native_small_models.json)：正确流转对照、B1/B2/B3 和零时长边界的完整实测。
- [day240.json](D:/Project/micron/myFab/research/state_boundary_audit_20261016/day240.json)：三种子转换前后状态、SH 阶段样本、续跑哈希与末态一致性。
- [naive_policy_switch.json](D:/Project/micron/myFab/research/state_boundary_audit_20261016/naive_policy_switch.json)：FIFO 无投料节拍和旧指纹失败证据。
- [integrity.json](D:/Project/micron/myFab/research/state_boundary_audit_20261016/integrity.json)：105 个既有受保护文件 SHA-256 不变，含业务/测试/checkpoint 源、数据库、旧 checkpoint 实验和上一轮研究产物/报告。

额外运行了现有 `test_smt2020_semantics/test_smt2020_baseline/test_tool_capacity/test_event_ordering/test_smt2020_fidelity/test_overlapping_downtime` 六个模块，**45 项通过**。已有 SH 测试主要覆盖加工中预留与到站后的派工，没有覆盖本轮卸载阶段反例；现有测试通过不等于 B1/B2/B3 不存在。

本轮未进行吞吐改善量估计、没有运行修复版代码，没有生成新 checkpoint，也没有证明任意政策均能完成无缝切换。报告中的最小修改范围是后续实施建议。

复现使用 checkpoint 原 CPython 3.12.14，`-B` 避免更新既有字节码。脚本默认只创建新证据文件，拒绝覆盖已有输出；再次复现应复制到项目 `research/` 下另一新目录后运行：

```powershell
$python = 'C:\Users\ZZY\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $python -B 'research/state_boundary_audit_20261016/probe_paths.py'
```
