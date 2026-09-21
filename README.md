# myFab

这是一个用于研究**半导体晶圆厂调度问题**的离散事件仿真项目。项目通过构建一个简化版 Fab 生产系统，模拟多产品、多工序、重入式生产、设备故障、换型损失、WIP 控制和瓶颈调度等问题，并对各种调度策略进行对比分析。

> 说明：本项目是一个学习和研究性质的简化原型，并不是工业级 Fab 调度系统。  
> 项目的重点在于搭建一个可扩展的仿真框架，用于理解和实验半导体制造中的调度与瓶颈控制问题。

---

## 1. 项目背景

半导体晶圆厂是典型的复杂制造系统，具有以下特点：

- 产品路线长，工序数量多；
- 存在重入式流程，同一类设备可能被同一个 Lot 多次访问；
- 不同设备对同一工序的加工时间可能不同；
- 设备可能发生随机故障和维修；
- 不同产品之间切换可能产生 setup 损失；
- 高 WIP 会导致排队、堵塞和周期时间上升；
- 调度策略会直接影响产出、周期时间、设备利用率和交期表现。

本项目希望通过一个简化但结构清晰的仿真平台，探索以下问题：

- 不同调度规则在重入式生产系统中的表现如何？
- 批量加工是否能够减少换型损失？
- DBR 思想是否可以用于控制 Fab 的瓶颈和 WIP？
- 尝试编写以及训练各种强化学习模型，以学习在重入流生产调度方面的强化学习算法应用

---

## 2. 项目功能

### 2.1 离散事件仿真

项目使用离散事件仿真方式推进时间，而不是固定时间步长循环。

典型事件包括：

- 订单到达；
- Lot 释放；
- 设备空闲；
- 工序完成；
- 设备故障；
- 设备维修完成；
- 仿真结束。

这种方式更接近真实制造系统，因为真实 Fab 中的状态变化通常是由事件触发的，而不是每隔固定时间统一更新。

---

### 2.2 可配置的 Fab 系统

项目将 SMT Excel 原样导入 SQLite。SQLite 保存工作表表头、原始行及用户提供的仿真时间配置；
运行时由模型构造模块在内存中生成工厂环境，包括：

- 产品类型；
- 产品工艺路线；
- 每个工序可使用的设备；
- 不同设备的加工时间；
- 订单到达规律；
- 设备数量和设备状态；
- 仿真时间；

随机种子由每次运行命令提供；策略参数仍可使用独立 JSON 文件。

例如，同一个工序可以配置多个可用设备，并且每台设备可以有不同的加工时间。这可以模拟真实 Fab 中“同一 Recipe 在不同设备上耗时不同”的情况。

---

### 2.3 重入式生产路线

重入式流程会导致调度问题变得更加复杂，因为早期工序和后期工序可能竞争同一批设备。如果调度策略不合理，可能会造成瓶颈拥堵、WIP 堆积和周期时间上升。

---

### 2.4 多种调度策略

当前项目中包含多种调度策略：

- `FIFO`：先进先出基准策略；
- `Batching FIFO`：优先加工相同产品，减少换型损失；
- `DBR v1`：初版 Drum-Buffer-Rope 瓶颈控制策略；
- `Dynamic DBR v2`：动态识别瓶颈并计算优先级的 DBR 改进策略。

这些策略被设计成可插拔模块，因此后续可以继续加入新的调度规则，例如 EDD、SPT、CR、CONWIP 或强化学习调度策略。

---

## 3. 快速开始

### 3.1 克隆项目

```bash
git clone https://github.com/xiaozhe19/myFab.git
cd myFab
```

## 4. 运行仿真

正式运行从 SQLite 模型库读取。先将一个 SMT2020 工作簿导入模型库；SMT 文件
不包含仿真结束时刻和策略投料节拍，所以它们必须显式以 minute 提供：

```bash
python -m fab.importer \
  --input "smt2020/General Data/dataset 1/SMT_2020_Model_Data_-_HVLM.xlsx" \
  --database data/model/hvlm.sqlite \
  --end-time 18000 \
  --release-interval 30 \
  --warmup-time 3600
```

然后以 FIFO 策略运行一次仿真：

```bash
python -m fab.run_model \
  --model-database data/model/hvlm.sqlite \
  --strategy FIFO \
  --plugin 'fab.plugins.sqlite_storage:SQLiteStoragePlugin={"database_path":"data/result/simulation_results.sqlite"}' \
  --plugin fab.plugins.trace:TracePlugin \
  --plugin fab.plugins.results:OnlineMetricsPlugin \
  --plugin fab.plugins.results:FinalStateSnapshotPlugin
```

若策略需要自行决定批处理成员，可增加 `--batch-from-strategy`。此时策略必须在
`StrategyDecision.batches` 中给出每个开炉设备的 lot 列表；引擎会校验工具组、
批处理条件、最小/最大批量，并在设备 loading 前冻结该批次，避免成员被其他
派工抢走。未启用该选项时，引擎继续使用内置的“按顺序尽量装满”组批规则。

模型库不保存产品、路线、设备等派生对象，只保存原始 SMT 表；每次运行时在内存中构造
`FabModel`。启用的结果插件通过 `run_id` 追加保存 lot、设备、工序记录和聚合 metrics。
内核和结果库的规范时间单位均为 minute。运行时会显示进度条、百分比及模拟时间进度
（当前时间／总模拟时长）。

---

## 5. 训练最小 Release RL

当前 RL 第一版只接管投料动作：`0` 表示 hold，`1` 表示 release；设备派工仍由
Dynamic DBR 完成。实现只依赖 Python 标准库，不需要安装深度学习框架。

运行 100 个 episode 的 Q-learning：

```bash
python -m rl.train --episodes 100
```

默认输出：

- `result/rl_q_table.json`：可以继续加载或部署的 Q 表；
- `result/rl_training_history.json`：每轮 reward、throughput、WIP、epsilon；

使用未参与训练的一组随机种子进行纯 greedy 评估：

```bash
python -m rl.evaluate --q-table result/rl_q_table.json --episodes 10
```

运行 RL 回归测试：

```bash
python -m unittest tests.test_rl_training
```

---

## 6. 调度策略设计思路

本项目将“仿真引擎”和“调度策略”分开设计。

仿真引擎主要负责推进仿真时间、处理事件、更新 Lot 和设备状态、记录生产历史，并生成输出结果。

调度策略主要负责判断是否释放新的 Lot、设备空闲时选择加工对象，并根据当前系统状态计算任务优先级。

新事件内核的策略接口、两种投料源和手写策略示例见 [Fab 内核与策略接入指南](FAB_KERNEL_GUIDE_ZH.md)。

---

## 7. Dynamic DBR 策略说明

Dynamic DBR 是当前项目中较核心的策略。其思想和实现主要参考了 Cao 等人提出的半导体制造系统 DBR 调度方法，并在本项目中结合简化 Fab 仿真环境进行了重新实现和扩展。[^1]


在制造系统中：

- Drum 代表系统的主瓶颈；
- Buffer 代表瓶颈前的保护缓冲；
- Rope 代表根据瓶颈节奏控制前端释放。

本项目中的 Dynamic DBR 主要包括三个部分。

---

### 7.1 主瓶颈识别

策略会根据一段时间窗口内各工艺区域的负载和产能情况，估计当前系统中的主瓶颈。

简单来说，如果某个工艺区域的需求负载长期高于它的可用产能，那么它更可能成为系统瓶颈。

---

### 7.2 次级瓶颈跟踪

除了主瓶颈之外，Fab 中还可能出现短期局部拥堵。例如：

- 某类设备刚好发生故障；
- 某种产品突然集中到达；
- 某个中间工序的队列突然变长。

因此，Dynamic DBR 也会关注短期队列压力，用于识别可能出现的次级瓶颈。

---

### 7.3 综合优先级计算

在设备选择下一个 Lot 时，Dynamic DBR 会综合考虑多个因素，例如：

- Lot 是否位于主瓶颈工序；
- Lot 是否接近瓶颈工序；
- 瓶颈前缓冲是否不足；
- 当前工序队列压力；
- Lot 在工艺路线中的位置；
- 下游工序是否可能发生拥堵；
- 预计清队时间。

该策略的目标不是证明最优，而是构建一个更有结构的瓶颈感知调度基准，用于和 FIFO、批量 FIFO 等简单规则进行对比。

---



## 8. 输出指标

项目的正式 benchmark 输出五项紧凑指标：

- `throughput`：预热期后完成的 Lot 数；
- `average_fab_wip`：预热期后的平均产线内 WIP；
- `p95_end_to_end_cycle_time`：订单从到达 release pool 到完成的 P95 周期；
- `on_time_rate`：有交期的已完成 Lot 中按期完成的比例；
- `release_pool_lots_at_end`：仿真结束时仍未放行到产线的 Lot 数。

最后一项用来识别策略通过长期 hold 订单而掩盖 WIP 或交期问题的情况。

完整性能优化说明见 [仿真提速说明（第六轮）](docs/performance_optimization_round6.md)。

---

## 9. 可研究的问题与后续方向

通过这个仿真平台，可以继续研究以下问题：

- FIFO 在高 WIP 情况下是否会导致严重排队？
- 批量优先策略能否减少 setup 损失？
- DBR 是否能稳定瓶颈前缓冲？
- 动态瓶颈识别是否比固定瓶颈假设更合理？
- 随机故障会不会改变系统中的实际瓶颈？
- 不同产品组合会不会导致瓶颈位置转移？
- 限制 WIP 是否能在不显著降低产出的情况下减少周期时间？
- 强化学习是否可以学习到比规则策略更好的 release 或 dispatch policy？

目前项目仍然是简化模型，主要局限包括：规模远小于真实半导体 Fab，暂未接入真实生产数据，运输时间、人员约束和物料搬运系统仍然较简化，setup、故障和维修模型仍然是抽象设定，暂未加入良率、返工、报废和计量反馈。

后续可以进一步扩展更复杂的产品路线，增加 EDD、SPT、CR、CONWIP 等策略，加入 due date 和 tardiness 指标，增加 Gantt 图、WIP 曲线和瓶颈变化曲线，做多随机种子重复实验和敏感性分析，并尝试引入强化学习作为调度策略。

---

## 10. 作者

Created by [xiaozhe19](https://github.com/xiaozhe19)

[^1]: Z. Cao, Y. Peng, and Y. Wang, "A drum-buffer-rope based scheduling method for semiconductor manufacturing system," *2011 IEEE International Conference on Automation Science and Engineering (CASE)*, Trieste, Italy, 2011, pp. 120–125, doi: [10.1109/CASE.2011.6042397](https://doi.org/10.1109/CASE.2011.6042397).
