# Fab 仿真开发手册（中文）

本文档面向需要编写 FIFO、Batching、DBR、动态瓶颈识别、投料控制等策略的开发者。

如果你只想快速开始，请先看：

1. [最重要的对象关系](#一最重要的对象关系)
2. [写一个普通派工策略](#八写一个普通派工策略)
3. [写一个需要全厂状态的高级策略](#九写一个需要全厂状态的高级策略)
4. [常见需求代码速查](#十二常见需求代码速查)
5. [常见错误](#十三常见错误)
6. [run_sim 运行模块](#十七run_sim-运行模块)
7. [策略对象完整接口](#十八策略对象完整接口)
8. [DynamicDBR API 参考](#十九dynamicdbr-api-参考)

---

## 一、最重要的对象关系

仿真中的 lot 分为两个区域：

```text
Fab 外部 waiting list                  Fab 内部
FabOrderPool.queue                     FactoryState.wafers
尚未投料的 lot                         已经投料的 lot
        │
        │ release_head()
        └────────────────────────────> wafers.append(lot)
```

需要牢记：

- `state.waiting_list.queue`：尚未进入产线的 lot。
- `state.wafers`：已经进入产线的所有 lot，包括等待加工、正在加工和已经完工的 lot。
- `lot.in_process`：是否正在某台机器上加工。
- `lot.completed`：是否已经走完整条 route。
- `lot.current_process`：当前需要加工的工序。
- `machine.type`：机器能加工的工序类型，例如 `Litho`。
- `machine.id`：具体机器编号，例如 `M01`。

机器与工序通过类型匹配：

```python
lot.current_process == machine.type
```

加工时间直接属于当前 route step：

```python
duration = lot.current_process_time
```

机器只负责说明自己能加工哪种 `process`；机器本身不定义产品工时。

---

## 二、代码模块分工

| 文件 | 作用 | 写策略时是否常用 |
| --- | --- | --- |
| `fab_sim_core.py` | 仿真引擎、机器状态、lot 状态、全厂状态、派工辅助函数 | 是 |
| `fab_orders.py` | 订单生成器和 Fab 外部 waiting list | 是 |
| `fab_config.py` | 读取 JSON 配置、读取随机种子 | 是 |
| `run_sim.py` | 统一加载策略、配置和订单池，并启动仿真 | 是 |
| `factory_config.json` | 机器、产品路线、加工时间、故障、换型配置 | 是 |
| `order_config.json` | 订单到达间隔、每单 lot 数、产品组合 | 是 |
| `simulation_config.json` | 预热时间和正式统计时长 | 是 |
| `simulation_seeds.json` | 订单、故障和换型的随机种子 | 通常直接复用 |
| `FIFO.py` | 最简单的策略范例 | 强烈建议参考 |
| `BATCHING_FIFO.py` | 同产品批量加工范例 | 建议参考 |
| `DBR_v1.py` | Buffer 计算、DBR 派工和自定义采样范例 | 写 DBR 时参考 |
| `fab_dashboard_app.py` | 结果展示服务 | 写核心策略时通常不用改 |

所有策略现在都是对象，并统一传给 `FabSimulationEngine`。策略对象通过：

- `initialize(context)`：仿真开始时获取产品、工序和配置等静态信息；
- `push_next(state, trigger)`：每批事件结算后返回投料和派工决策；
- `sample(state)`：可选，记录策略自己的监控数据；
- `result_fields()`：可选，把策略参数写入结果 JSON。

`FabSimulationEngine` 已经负责：

- 生成订单；
- 按周期尝试投料；
- 推进仿真时钟；
- 判断机器何时空闲；
- 处理 setup；
- 处理随机故障和维修；
- 开始、结束工序；
- 更新 lot 的 `step`；
- 记录生产事件；
- 生成结果 JSON。

策略模块通常只需要负责：

- 当前机器应该选择哪个候选 lot；
- 当前是否允许投料；
- 可选：记录策略自己的指标，例如 DBR buffer；
- 可选：根据全厂状态识别瓶颈。

---

## 三、核心数据结构

### 3.1 `MachineState`

定义位置：`fab_sim_core.py`

```python
@dataclass
class MachineState:
    id: str
    name: str
    type: str
    available_time: float
    current_product_id: str | None = None
    downtime_model: dict | None = None
    downtime_rng: random.Random | None = None
    next_failure_time: float | None = None
```

字段说明：

| 字段 | 示例 | 含义 |
| --- | --- | --- |
| `id` | `"M01"` | 具体机器编号 |
| `name` | `"Litho-01"` | 显示名称 |
| `type` | `"Litho"` | 机器能加工的工序类型 |
| `available_time` | `286.0` | 机器下一次可用的仿真时间 |
| `current_product_id` | `"P01"` | 机器上一次/当前加工的产品，用于计算 setup |
| `next_failure_time` | `520.3` | 下一次计划故障时间 |

常用判断：

```python
# 当前机器是否空闲
is_idle = machine.available_time <= state.current_time

# 是否延续加工相同产品
same_product = lot.product_id == machine.current_product_id
```

策略应主要读取这些字段。机器时间、故障和当前产品由仿真引擎维护，不建议策略直接修改。

### 3.2 `WaferState`

虽然类名叫 `WaferState`，在当前模型中一个对象代表一个生产 lot。

```python
@dataclass
class WaferState:
    id: str
    product_id: str
    product_name: str
    route: list[dict[str, Any]]
    generation_time: float
    release_time: float
    due_time: float | None
    priority: int
    input_order: int
    order_id: str | None = None
    record_result: bool = True
    step: int = 0
    ready_time: float = 0.0
    start_time: float | None = None
    end_time: float | None = None
    in_process: bool = False
    processing_machine_id: str | None = None
    processing_end_time: float | None = None
    operation_history: list[dict] = []
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `id` | lot ID，例如 `W001` |
| `order_id` | 所属订单，例如 `O0001` |
| `product_id` | 产品 ID，例如 `P01` |
| `route` | 完整工艺路线 |
| `step` | 当前工序在 `route` 中的下标，从 0 开始 |
| `generation_time` | 订单生成、lot 进入外部 waiting list 的时间 |
| `release_time` | lot 正式进入 Fab 的时间；未投料时为无穷大 |
| `ready_time` | lot 可以开始当前工序的最早时间 |
| `start_time` | lot 第一次开始加工的时间 |
| `end_time` | lot 完成全部工序的时间 |
| `in_process` | 当前是否正在机器上加工 |
| `processing_machine_id` | 当前加工机器 ID |
| `processing_end_time` | 当前工序预计结束时间 |
| `operation_history` | 已完成工序的历史记录 |

几个最重要的只读属性：

```python
lot.completed
```

当 `step >= len(route)` 时为 `True`。

```python
lot.current_process
```

返回当前工序名称，例如 `"Litho"`；已完工时返回 `None`。

```python
lot.current_process_time
```

返回当前 route step 的加工时间；已完工时返回 `None`。

```python
lot.current_operation
```

返回当前 step 的完整配置字典。

```python
lot.current_operation_id
```

返回类似 `"P01:4"` 的标识，用于区分同一产品路线中的不同 step。

例如：

```python
route = [
    {"process": "Litho", "process_time": 18},
    {"process": "Etch", "process_time": 14},
    {"process": "Clean", "process_time": 8},
    {"process": "Litho", "process_time": 30},
]
step = 3

lot.current_process       # "Litho"
lot.current_process_time  # 30.0
lot.current_operation_id  # "P01:3"
```

### 3.3 `FactoryState`

高级策略最应该使用的数据结构。它是某一仿真时刻的完整工厂快照。

```python
@dataclass
class FactoryState:
    current_time: float
    machines: list[MachineState]
    wafers: list[WaferState]
    waiting_list: FabOrderPool
    products: dict[str, dict]
    factory_config: dict
    simulation_config: dict
    machine_events: list[dict]
    last_operation_completion: dict[str, float]
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `current_time` | 当前仿真时间 |
| `machines` | 所有机器的实时状态 |
| `wafers` | 已经进入 Fab 的所有 lot |
| `waiting_list` | Fab 外部订单池 |
| `products` | 产品信息，以产品 ID 为键 |
| `factory_config` | 完整工厂配置 |
| `simulation_config` | 完整仿真配置 |
| `machine_events` | 到当前为止的 setup、process、downtime 事件 |
| `last_operation_completion` | 各 route operation 最近一次完成时间 |

典型用法：

```python
def inspect_factory(state: FactoryState) -> None:
    print("当前时间：", state.current_time)
    print("Fab 内 lot 数：", len(state.wafers))
    print("Fab 外 lot 数：", len(state.waiting_list.queue))
```

### 3.4 `FabOrderPool`

它既是订单生成器，也是 Fab 外部 waiting list。

主要字段：

| 字段 | 含义 |
| --- | --- |
| `queue` | 尚未投料的 lot，类型为 `deque` |
| `order_interval` | 新订单生成间隔 |
| `lots_per_order` | 每张订单包含多少 lot |
| `capacity` | waiting list 容量；0 表示不限 |
| `product_mix` | 产品随机生成权重 |
| `generated_count` | 已生成的 lot 总数 |

正确遍历方式：

```python
for lot in state.waiting_list.queue:
    print(lot.id, lot.product_id)
```

只读取快照：

```python
snapshot = state.waiting_list.snapshot()
```

`snapshot()` 返回字典列表，适合输出和保存，但不包含完整的 `route`、`step` 等运行对象字段。

投料：

```python
lot = state.waiting_list.release_head(state.current_time)
```

一般不要在策略中直接调用这个函数。引擎会根据 `release_policy` 的返回值决定是否调用它。

---

## 四、配置数据结构

### 4.1 机器配置

来自 `factory_config.json`：

```json
{
  "id": "M01",
  "name": "Litho-01",
  "type": "Litho",
  "available_from": 0
}
```

当前配置每种机器类型只有一台，但代码支持多台相同类型机器。

设备类型数量：

```python
from collections import Counter

machine_count = Counter(machine.type for machine in state.machines)
```

### 4.2 产品配置

运行时的 `state.products` 是以产品 ID 为键的字典：

```python
{
    "P01": {
        "id": "P01",
        "name": "Logic-A",
        "route": [
            {"process": "Litho", "process_time": 18},
            {"process": "Etch", "process_time": 14},
            {"process": "Clean", "process_time": 8},
            {"process": "Deposition", "process_time": 22},
            {"process": "Litho", "process_time": 30},
            {"process": "Etch", "process_time": 20},
            {"process": "Metrology", "process_time": 6},
        ],
        "reentry_steps": [...],
    }
}
```

读取产品路线：

```python
route = state.products["P01"]["route"]
```

注意：route 允许重入。同一个工序可以出现多次，而且每次重入可以设置不同工时。

### 4.3 加工时间

加工时间保存在 route 的每一步中：

```python
operation = {
    "process": "Litho",
    "process_time": 18,
}
```

因此既可以表达不同产品工时不同，也可以表达同一产品两次经过 Litho 时工时不同。

```python
first_litho = product["route"][0]["process_time"]
second_litho = product["route"][4]["process_time"]
```

查询某个 lot 当前工序的加工时间：

```python
duration = lot.current_process_time
```

`factory_config.json` 不再包含 `process_time_by_product`。

### 4.4 订单配置

`order_config.json`：

```json
{
  "arrival": {
    "type": "fixed_interval",
    "interval": 720
  },
  "lots_per_order": 19,
  "waiting_list": {
    "max_size": 0
  },
  "product_mix": {
    "P01": 1,
    "P02": 1,
    "P03": 1,
    "P04": 1,
    "P05": 1
  }
}
```

含义：

- 每 720 分钟生成一张订单；
- 每张订单生成 19 个独立 lot；
- 每个 lot 根据 `product_mix` 随机选择产品；
- `max_size = 0` 表示 waiting list 不限制容量。

waiting list 中每个元素已经是一个 lot，不是整张订单，因此没有：

```python
lot.quantity  # 不存在
```

如果 waiting list 有 19 个元素，就代表 19 个 lot。

### 4.5 仿真时间配置

`simulation_config.json`：

```json
{
  "simulation_id": "small-fab-run",
  "time_unit": "minute",
  "start_time": 0,
  "warmup_time": 720,
  "measurement_duration": 4320
}
```

正式仿真结束时间：

```python
measurement_start = start_time + warmup_time
simulation_end = measurement_start + measurement_duration
```

预热期间会正常生产，但正式指标只统计 measurement window。

---

## 五、仿真运行流程

引擎使用统一的优先队列（事件日历）推进时间，不再通过主循环扫描
`next_order_time`、`next_release_time` 和机器时间。当前事件类型包括：

- `OP_COMPLETE`：工序完成；
- `ORDER_ARRIVAL`：订单到达；
- `LOT_RELEASE`：投料检查；
- `MACHINE_FAILURE`：空闲设备发生故障；
- `MACHINE_READY`：设备初始可用或维修完成；
- `WAFER_READY`：配置中的初始 lot 到达可加工时间；
- `SIM_END`：仿真结束。

同一时刻按“工序完成 → 订单到达 → 投料 → 故障/恢复”处理，然后统一采样并
对所有空闲设备派工。整体流程如下：

```text
1. 从事件日历弹出最早时刻的全部事件
2. 按优先级更新 lot、订单池和机器状态
3. 记录队列和自定义采样
4. 对每台空闲机器调用派工策略
5. 引擎计算 setup 和 process time，并处理可能发生的故障
6. 为加工任务创建 OP_COMPLETE 事件
7. 重复执行，直到 SIM_END
```

派工策略不需要自己：

- 增加 `lot.step`；
- 设置 `lot.in_process`；
- 计算 setup；
- 修改 `machine.available_time`；
- 模拟故障；
- 推进 `current_time`。

策略只返回选中的 lot，剩下的由引擎处理。

---

## 六、核心辅助函数

### 6.1 `ready_candidates`

最常用的派工辅助函数：

```python
from fab_sim_core import ready_candidates

candidates = ready_candidates(
    state.wafers,
    machine.type,
    state.current_time,
)
```

它自动筛选：

- 未完工；
- 当前不在加工；
- 已到 `ready_time`；
- 当前工序等于机器类型。

等价条件：

```python
candidates = [
    lot
    for lot in state.wafers
    if not lot.completed
    and not lot.in_process
    and lot.ready_time <= state.current_time
    and lot.current_process == machine.type
]
```

### 6.2 `create_order_lot`

创建订单池时作为 lot 工厂传入：

```python
from fab_sim_core import create_order_lot

order_pool = FabOrderPool(
    order_config,
    products,
    create_order_lot,
    order_seed,
)
```

策略代码通常不直接调用它。

### 6.3 `save_result`

保存结果 JSON：

```python
from pathlib import Path
from fab_sim_core import save_result

save_result(result, Path("my_strategy_result.json"))
```

### 6.4 `post_result`

把结果发送到正在运行的 dashboard：

```python
from fab_sim_core import post_result

post_result(result, "http://127.0.0.1:5000/api/runs")
```

### 6.5 配置读取

```python
from pathlib import Path
from fab_config import load_json, load_seed_set, apply_factory_seeds

seed_set = load_seed_set(Path("simulation_seeds.json"), 1)
factory_config = apply_factory_seeds(
    load_json(Path("factory_config.json")),
    seed_set,
)
order_config = load_json(Path("order_config.json"))
simulation_config = load_json(Path("simulation_config.json"))
```

---

## 七、统一策略对象接口

推荐继承 `FabStrategyBase`。普通策略实现 `select_job()` 即可；基础类会在
`push_next()` 中遍历空闲机器、避免同一个 lot 被重复分配，并生成
`StrategyDecision`。

```python
class MyStrategy(FabStrategyBase):
    name = "My Strategy"
    run_id = "my-strategy"
    release_interval = 38.0

    def initialize(self, context):
        super().initialize(context)
        # 这里只执行一次，适合缓存产品路线、机器组和策略参数。

    def select_job(self, state, machine, reserved_wafer_ids):
        candidates = [
            lot
            for lot in ready_candidates(
                state.wafers,
                machine.type,
                state.current_time,
            )
            if lot.id not in reserved_wafer_ids
        ]
        return min(candidates, key=lambda lot: lot.ready_time) if candidates else None
```

需要统一控制投料和全厂派工的高级策略，可以直接重写：

```python
def push_next(self, state, trigger):
    decision = StrategyDecision()
    if trigger.release_opportunity:
        decision.release_lot = self.should_release_now(state)
    decision.dispatches = self.build_dispatch_plan(state)
    return decision
```

`trigger.event_kinds` 表示本轮刚结算的事件；`trigger.release_opportunity`
表示当前是否允许释放 waiting list 队首 lot。Engine 负责验证并执行决策，
策略不要直接修改机器、lot 或仿真时钟。

策略接入 Engine：

```python
engine = FabSimulationEngine(
    factory_config=factory_config,
    simulation_config=simulation_config,
    order_pool=order_pool,
    strategy=MyStrategy(),
)
```

---

## 八、写一个普通派工策略

下面是一个可直接复制的最小策略文件。

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from fab_config import apply_factory_seeds, load_json, load_seed_set
from fab_orders import FabOrderPool
from fab_sim_core import (
    FabSimulationEngine,
    FabStrategyBase,
    FactoryState,
    MachineState,
    WaferState,
    create_order_lot,
    ready_candidates,
    save_result,
)


class MyStrategy(FabStrategyBase):
    name = "My Strategy"
    run_id = "my-strategy-run"
    release_interval = 38.0

    def select_job(
        self,
        state: FactoryState,
        machine: MachineState,
        reserved_wafer_ids: set[str],
    ) -> WaferState | None:
        candidates = [
            lot
            for lot in ready_candidates(
                state.wafers,
                machine.type,
                state.current_time,
            )
            if lot.id not in reserved_wafer_ids
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda lot: (
                lot.current_process_time,
                lot.ready_time,
                lot.input_order,
                lot.id,
            ),
        )


def run_my_strategy(
    factory_config: dict[str, Any],
    simulation_config: dict[str, Any],
    order_pool: FabOrderPool,
) -> dict[str, Any]:
    engine = FabSimulationEngine(
        factory_config=factory_config,
        simulation_config=simulation_config,
        order_pool=order_pool,
        strategy=MyStrategy(),
    )
    return engine.run()


def main() -> None:
    seed_set = load_seed_set(Path("simulation_seeds.json"), 1)
    factory_config = apply_factory_seeds(
        load_json(Path("factory_config.json")),
        seed_set,
    )
    simulation_config = load_json(Path("simulation_config.json"))
    order_config = load_json(Path("order_config.json"))

    products = {
        product["id"]: product
        for product in factory_config["products"]
    }

    order_pool = FabOrderPool(
        order_config,
        products,
        create_order_lot,
        seed_set["order_seed"],
    )

    result = run_my_strategy(
        factory_config,
        simulation_config,
        order_pool,
    )
    save_result(result, Path("my_strategy_result.json"))


if __name__ == "__main__":
    main()
```

---

## 九、写一个需要全厂状态的高级策略

动态瓶颈和 DBR 推荐使用 `FactoryState`。

```python
from collections import Counter, defaultdict

from fab_sim_core import (
    FactoryState,
    MachineState,
    WaferState,
    ready_candidates,
)


def calculate_process_load(
    state: FactoryState,
    window: float = 720.0,
) -> list[dict]:
    machine_count = Counter(
        machine.type for machine in state.machines
    )
    demand = defaultdict(float)

    # Fab 外：每个 lot 的完整路线都还没有加工。
    for lot in state.waiting_list.queue:
        for operation in lot.route:
            demand[operation["process"]] += float(
                operation["process_time"]
            )

    # Fab 内：统计当前及后续尚未完成的路线。
    for lot in state.wafers:
        if lot.completed:
            continue

        if lot.in_process:
            # 当前工序已经开工，用预计结束时间减当前时间近似剩余占用；
            # 后续工序仍按标准加工时间累计。
            remaining_current = max(
                float(lot.processing_end_time or state.current_time)
                - state.current_time,
                0.0,
            )
            demand[lot.current_process] += remaining_current
            remaining_route = lot.route[lot.step + 1:]
        else:
            remaining_route = lot.route[lot.step:]

        for operation in remaining_route:
            demand[operation["process"]] += float(
                operation["process_time"]
            )

    ranking = []
    for process_name, count in machine_count.items():
        capacity = window * count
        load_ratio = (
            demand[process_name] / capacity
            if capacity > 0
            else float("inf")
        )
        ranking.append(
            {
                "process": process_name,
                "demand_minutes": round(demand[process_name], 3),
                "capacity_minutes": round(capacity, 3),
                "load_ratio": round(load_ratio, 4),
            }
        )

    return sorted(
        ranking,
        key=lambda item: item["load_ratio"],
        reverse=True,
    )


def select_advanced_job(
    state: FactoryState,
    machine: MachineState,
) -> WaferState | None:
    candidates = ready_candidates(
        state.wafers,
        machine.type,
        state.current_time,
    )
    if not candidates:
        return None

    ranking = calculate_process_load(state)
    main_bottleneck = ranking[0]["process"]

    # 示例：瓶颈机器保持 FIFO。
    if machine.type == main_bottleneck:
        return min(
            candidates,
            key=lambda lot: (
                lot.ready_time,
                lot.release_time,
                lot.input_order,
                lot.id,
            ),
        )

    # 这里只是示例，实际 DBR 可以按“距离下一次瓶颈的时间”排序。
    return min(
        candidates,
        key=lambda lot: (
            lot.ready_time,
            lot.input_order,
            lot.id,
        ),
    )
```

上例中，正在加工工序的剩余时间使用
`processing_end_time - current_time` 近似。这个值可能包含未来故障造成的中断，
因此它更接近“设备还会被占用多久”，不一定等于纯加工分钟数。当前运行时状态没有单独保存
“本工序剩余的纯加工时间”。

高级策略同样应封装为对象，并在 `select_job()` 或 `push_next()` 中调用上述计算：

```python
class AdvancedStrategy(FabStrategyBase):
    name = "Advanced Strategy"
    run_id = "advanced-strategy"
    release_interval = 38.0

    def select_job(self, state, machine, reserved_wafer_ids):
        selected = select_advanced_job(state, machine)
        if selected and selected.id not in reserved_wafer_ids:
            return selected
        return None

engine = FabSimulationEngine(
    factory_config=factory_config,
    simulation_config=simulation_config,
    order_pool=order_pool,
    strategy=AdvancedStrategy(),
)
```

### 关于“每 720 分钟重新计算一次瓶颈”

`push_next()` 会在事件结算后调用。动态瓶颈计算仍应按时间周期缓存，不要在每台机器选片时重复计算。

可以使用策略对象缓存：

```python
class DynamicBottleneckStrategy(FabStrategyBase):
    def __init__(self, interval: float = 720.0):
        self.interval = interval
        self.last_calculation_time: float | None = None
        self.ranking: list[dict] = []

    def update_if_needed(self, state: FactoryState) -> None:
        should_update = (
            self.last_calculation_time is None
            or state.current_time
            >= self.last_calculation_time + self.interval
        )
        if not should_update:
            return

        self.ranking = calculate_process_load(
            state,
            window=self.interval,
        )
        self.last_calculation_time = state.current_time

    def select_job(
        self,
        state: FactoryState,
        machine: MachineState,
        reserved_wafer_ids: set[str],
    ) -> WaferState | None:
        self.update_if_needed(state)

        candidates = [
            lot
            for lot in ready_candidates(
                state.wafers,
                machine.type,
                state.current_time,
            )
            if lot.id not in reserved_wafer_ids
        ]
        if not candidates:
            return None

        return min(
            candidates,
            key=lambda lot: (
                lot.ready_time,
                lot.input_order,
                lot.id,
            ),
        )


engine = FabSimulationEngine(
    ...,
    strategy=DynamicBottleneckStrategy(interval=720.0),
)
```

---

## 十、投料控制示例

例如限制 Fab 内未完工 WIP 不超过 50：

```python
from fab_sim_core import FactoryState


class WipLimitedStrategy(FabStrategyBase):
    def should_release(self, state: FactoryState) -> bool:
        active_wip = sum(
            1
            for lot in state.wafers
            if not lot.completed
        )
        return active_wip < 50

    def select_job(self, state, machine, reserved_wafer_ids):
        ...
```

接入：

```python
engine = FabSimulationEngine(
    ...,
    strategy=WipLimitedStrategy(),
)
```

如果 waiting list 为空，即使返回 `True` 也不会释放任何 lot。

---

## 十一、加工状态的准确含义

“Fab 中的 lot”和“正在加工的 lot”不是同一件事。

### Fab 内所有未完工 lot

包括排队和正在加工：

```python
active_lots = [
    lot
    for lot in state.wafers
    if not lot.completed
]
```

### 当前正在机器上加工

```python
processing_lots = [
    lot
    for lot in state.wafers
    if not lot.completed and lot.in_process
]
```

### 当前已经 ready、正在排队

```python
queued_lots = [
    lot
    for lot in state.wafers
    if not lot.completed
    and not lot.in_process
    and lot.ready_time <= state.current_time
]
```

### Fab 内已完工 lot

```python
completed_lots = [
    lot
    for lot in state.wafers
    if lot.completed
]
```

### Fab 外尚未投料

```python
unreleased_lots = list(state.waiting_list.queue)
```

---

## 十二、常见需求代码速查

### 12.1 获取当前机器的候选 lot

```python
candidates = ready_candidates(
    state.wafers,
    machine.type,
    state.current_time,
)
```

### 12.2 获取某工序前的排队 lot

```python
litho_queue = [
    lot
    for lot in state.wafers
    if not lot.completed
    and not lot.in_process
    and lot.ready_time <= state.current_time
    and lot.current_process == "Litho"
]
```

### 12.3 获取每种产品的 Fab 内 WIP

```python
from collections import Counter

wip_by_product = Counter(
    lot.product_id
    for lot in state.wafers
    if not lot.completed
)
```

### 12.4 获取正在加工的机器与 lot

```python
processing = [
    {
        "lot_id": lot.id,
        "product_id": lot.product_id,
        "process": lot.current_process,
        "machine_id": lot.processing_machine_id,
        "end_time": lot.processing_end_time,
    }
    for lot in state.wafers
    if lot.in_process and not lot.completed
]
```

### 12.5 获取一个 Fab 内 lot 的剩余路线

```python
remaining_route = lot.route[lot.step:]
```

如果当前工序也需要计入剩余工作量，使用上面的切片。

### 12.6 计算一个 lot 的剩余纯加工时间

```python
remaining_work = sum(
    float(operation["process_time"])
    for operation in lot.route[lot.step:]
)
```

这不包含：

- 排队时间；
- setup；
- downtime；
- 搬运时间。

### 12.7 计算某产品对某设备类型的完整需求

```python
product_id = "P01"
process_name = "Litho"

demand = sum(
    float(operation["process_time"])
    for operation in state.products[product_id]["route"]
    if operation["process"] == process_name
)
```

P01 的 route 如果出现两次 Litho，就会累计两次。

### 12.8 计算 720 分钟设备理论产能

```python
from collections import Counter

machine_count = Counter(
    machine.type for machine in state.machines
)

capacity = {
    process_name: 720.0 * count
    for process_name, count in machine_count.items()
}
```

当前配置每种设备一台，所以每种设备为 720 machine-minutes。以后增加同类型设备后会自动扩大。

### 12.9 判断某 lot 后续是否还会经过瓶颈

```python
def steps_until_process(lot, target_process):
    for offset, operation in enumerate(
        lot.route[lot.step:]
    ):
        if operation["process"] == target_process:
            return offset
    return None
```

返回：

- `0`：当前工序就是目标工序；
- `1`：做完当前工序后到目标工序；
- `None`：剩余路线不再经过目标工序。

### 12.10 查看某个 route operation 最近完成时间

```python
operation_id = "P01:4"
last_time = state.last_operation_completion.get(operation_id)
```

注意它按“产品 + step”聚合，不是按 lot ID 聚合。

### 12.11 查看历史事件

```python
process_events = [
    event
    for event in state.machine_events
    if event["kind"] == "process"
]

setup_events = [
    event
    for event in state.machine_events
    if event["kind"] == "setup"
]

downtime_events = [
    event
    for event in state.machine_events
    if event["kind"] == "downtime"
]
```

加工和 setup 可能被故障拆成多个 segment。判断一个工序是否为最后一段时，可读取：

```python
event.get("final_segment", True)
```

---

## 十三、常见错误

### 错误 1：仍然从机器或旧工时表查询加工时间

错误：

```python
process_times[product_id][machine.type]
```

正确：

```python
lot.current_process_time
```

设备只通过 `machine.type` 和 `lot.current_process` 判断能否加工。加工时间属于 route step。

### 错误 2：直接遍历 `waiting_list`

错误：

```python
for lot in waiting_list:
    ...
```

正确：

```python
for lot in waiting_list.queue:
    ...
```

`FabOrderPool` 本身没有实现迭代协议。

### 错误 3：读取 `lot.quantity`

当前 waiting list 中每个元素已经是一个 lot：

```python
demand = process_time
```

不是：

```python
demand = lot.quantity * process_time
```

如果有 19 个 lot，就遍历 19 次。

### 错误 4：把 `state.wafers` 当成全部未完工 lot

`state.wafers` 中也保留已完工 lot。必须筛选：

```python
active_lots = [
    lot for lot in state.wafers
    if not lot.completed
]
```

### 错误 5：把 `in_process` 当成“已进入 Fab”

`in_process` 只表示当前正在机器上加工。

Fab 内 WIP：

```python
not lot.completed
```

正在加工：

```python
lot.in_process
```

### 错误 6：忽略 route 重入

错误：

```python
unique_processes = {
    operation["process"]
    for operation in lot.route
}
for process_name in unique_processes:
    ...
```

正确：

```python
for operation in lot.route:
    demand[operation["process"]] += operation["process_time"]
```

同一工序出现两次，就代表需要加工两次，而且两次的 `process_time` 可以不同。

### 错误 7：策略手动修改状态

通常不要在派工函数中执行：

```python
lot.step += 1
lot.in_process = True
machine.available_time = ...
```

派工函数只返回 lot：

```python
return selected_lot
```

### 错误 8：在每次派工回调中重复做昂贵的全厂计算

同一时刻可能为多台机器调用策略。动态瓶颈计算应缓存，并按 720 分钟等周期更新。

### 错误 9：把 result JSON 当成运行时对象

仿真运行时：

```python
lot.current_process
lot.completed
```

仿真结束后的结果 JSON：

```python
wafer_dict["step"]
wafer_dict["completed"]
```

前者是对象属性，后者是普通字典，不能混用。

---

## 十四、结果 JSON

`engine.run()` 返回字典，主要字段包括：

| 字段 | 含义 |
| --- | --- |
| `run_id` | 本次运行 ID |
| `strategy` | 策略名称 |
| `current_time` | 仿真结束时间 |
| `machines` | 机器静态信息 |
| `products` | 产品配置 |
| `wafers` | lot 最终状态 |
| `operations` | measurement window 内的 setup/process 时间线 |
| `machine_events` | measurement window 内的 downtime |
| `queue_samples` | 各工序排队长度采样 |
| `order_events` | 订单生成事件 |
| `release_events` | 投料事件 |
| `waiting_list.final_snapshot` | 仿真结束时仍未投料的 lot |
| `measurement` | throughput、MCT、WIP 等指标 |

常用指标：

```python
result["measurement"]["throughput"]
result["measurement"]["mct_average"]
result["measurement"]["average_fab_wip"]
result["measurement"]["movements"]
```

注意：结果中的 `operations` 不只是加工，也可能包含 setup：

```python
process_operations = [
    event
    for event in result["operations"]
    if event["kind"] == "process"
]
```

---

## 十五、运行现有策略

FIFO：

```powershell
python run_sim.py --strategy FIFO:FIFOStrategy --output fifo_baseline_result.json
```

Batching FIFO：

```powershell
python run_sim.py --strategy BATCHING_FIFO:BatchingFIFOStrategy --output batching_fifo_baseline_result.json
```

DBR v1：

```powershell
python run_sim.py --strategy DBR_v1:DBRV1Strategy --output dbr_v1_result.json
```

指定随机种子组和输出：

```powershell
python run_sim.py --strategy FIFO:FIFOStrategy --seed-index 2 --output fifo_seed_2_result.json
```

启动 dashboard：

```powershell
python fab_dashboard_app.py
```

运行策略并发送结果：

```powershell
python run_sim.py --strategy FIFO:FIFOStrategy --post http://127.0.0.1:5000/api/runs
```

---

## 十六、推荐的开发顺序

编写新策略时，建议按这个顺序：

1. 复制 `FIFO.py` 作为新文件。
2. 新建 `FabStrategyBase` 子类，先只实现 `select_job()`。
3. 需要缓存路线、机器组或参数时，重写 `initialize()`。
4. 需要统一控制投料和多机器派工时，重写 `push_next()`。
5. 需要限制投料时，重写 `should_release()`。
6. 需要画策略指标时，重写 `sample()` 或 `result_fields()`。
7. 使用不同 `--seed-index` 比较策略，避免只看单次随机结果。

如果策略开始自己处理时间推进、故障、setup 或工序完成，通常说明逻辑放错层了：这些工作应该继续交给 `FabSimulationEngine`。

---

## 十七、`run_sim` 运行模块

`run_sim.py` 是统一的仿真入口。策略文件只保存策略类，不再负责读取配置、
创建订单池、保存结果或解析命令行参数。

### 17.1 命令行基本格式

```powershell
python run_sim.py `
  --strategy 模块名:策略类名 `
  --output 结果文件.json
```

例如：

```powershell
python run_sim.py `
  --strategy FIFO:FIFOStrategy `
  --output fifo_result.json
```

`--strategy` 使用 Python 模块名和类名，不包含 `.py`：

```text
FIFO.py 中的 FIFOStrategy
    → FIFO:FIFOStrategy

DBR_v2.py 中的 DynamicDBR
    → DBR_v2:DynamicDBR
```

### 17.2 全部命令行参数

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `--strategy` | `FIFO:FIFOStrategy` | 策略类，格式为 `module:class` |
| `--strategy-params` | 无 | 传给策略构造函数的 JSON 文件 |
| `--factory` | `factory_config.json` | 工厂、机器、产品路线配置 |
| `--orders` | `order_config.json` | 订单到达和产品组合配置 |
| `--simulation` | `simulation_config.json` | 仿真起点、预热和统计时长 |
| `--seeds` | `simulation_seeds.json` | 随机种子组文件 |
| `--seed-index` | `1` | 使用第几组随机种子，从 1 开始 |
| `--output` | `simulation_result.json` | 结果 JSON 输出路径 |
| `--post` | 无 | 可选的 dashboard API 地址 |

查看当前程序实际支持的参数：

```powershell
python run_sim.py --help
```

### 17.3 运行 DBR v2

`DynamicDBR` 的构造函数要求 `parameters`，因此需要准备参数文件：

```json
{
  "parameters": {
    "L0": 1.0,
    "m0": 0.01,
    "m1": 1.0,
    "m2": 1.0,
    "n0": 0.01,
    "n1": 1.0,
    "n2": 0.01,
    "s1": -0.01,
    "s2": 0.01,
    "p1": 1.0,
    "p2": 0.1,
    "BL": 720.0,
    "r": 1.0
  },
  "release_interval": 38.0,
  "main_bottleneck_window": 720.0,
  "sub_bottleneck_window": 5.0,
  "alpha": 0.5,
  "lam": 0.5
}
```

运行：

```powershell
python run_sim.py `
  --strategy DBR_v2:DynamicDBR `
  --strategy-params dbr_v2_parameters.json `
  --output dbr_v2_result.json
```

JSON 顶层字段会作为关键字参数传给策略：

```python
DynamicDBR(
    parameters={...},
    release_interval=38.0,
    main_bottleneck_window=720.0,
    sub_bottleneck_window=5.0,
    alpha=0.5,
    lam=0.5,
)
```

### 17.4 `run_sim.py` 函数参考

#### `load_strategy_class(spec)`

根据 `module:class` 动态导入策略类。

```python
strategy_class = load_strategy_class("DBR_v2:DynamicDBR")
strategy = strategy_class(parameters=my_parameters)
```

输入：

- `spec: str`：例如 `"FIFO:FIFOStrategy"`。

输出：

- 策略类本身，还没有实例化。

常见错误：

- 写成 `DBR_v2.py:DynamicDBR`；
- 模块不在当前 Python 搜索路径；
- 类名大小写错误。

#### `load_strategy_parameters(path)`

读取策略参数 JSON。

```python
kwargs = load_strategy_parameters(Path("dbr_v2_parameters.json"))
strategy = DynamicDBR(**kwargs)
```

输出必须是字典。文件内容不能是列表或单个数字。

#### `build_order_pool(factory_config, order_config, order_seed)`

创建 `FabOrderPool`。它会从工厂配置取得产品列表，并使用
`create_order_lot()` 作为 lot 创建器。

#### `run_sim(strategy, factory_config, simulation_config, order_pool)`

程序化运行一次仿真：

```python
result = run_sim(
    strategy=FIFOStrategy(),
    factory_config=factory_config,
    simulation_config=simulation_config,
    order_pool=order_pool,
)
```

返回 dashboard 兼容的结果字典，不会自动保存文件。

#### `summarize(result)`

从结果中生成一行摘要，包括 throughput、movements、MCT、WIP、setup 和
downtime 数量。

#### `main()`

命令行入口。负责：

1. 解析参数；
2. 读取随机种子；
3. 读取配置；
4. 动态创建策略；
5. 运行仿真；
6. 保存和可选 POST 结果。

---

## 十八、策略对象完整接口

### 18.1 策略生命周期

```text
创建策略对象
    ↓
Engine 调用 initialize(context)
    ↓
事件日历结算一批同一时刻事件
    ↓
Engine 调用 push_next(state, trigger)
    ↓
策略返回 StrategyDecision
    ↓
Engine 执行投料、setup、加工和故障处理
    ↓
重复直到 SIM_END
```

策略负责决定“做什么”，Engine 负责执行“如何做”。

### 18.2 `StrategyInitialization`

只在仿真开始时传给策略一次：

```python
context.factory_config
context.simulation_config
context.products
context.process_names
```

适合缓存：

- 产品数量；
- 各产品 recipe；
- 机器静态配置；
- 工序到机器的分组；
- 策略常量和预计算索引。

不适合缓存：

- 当前 WIP；
- 当前队列；
- 机器当前是否空闲；
- 当前时间。

这些实时数据应从 `FactoryState` 读取。

### 18.3 `StrategyTrigger`

每次调用 `push_next()` 时提供触发原因：

```python
trigger.event_kinds
trigger.release_opportunity
trigger.strategy_reasons
```

#### `event_kinds`

当前时刻刚结算的事件类型，可能包括：

| 事件 | 含义 |
| --- | --- |
| `OP_COMPLETE` | 某道工序完成 |
| `ORDER_ARRIVAL` | 新订单进入 waiting list |
| `LOT_RELEASE` | 到达投料检查时刻 |
| `MACHINE_FAILURE` | 空闲设备发生故障 |
| `MACHINE_READY` | 设备初始可用或维修完成 |
| `WAFER_READY` | 初始 lot 到达 ready 时间 |
| `STRATEGY_WAKEUP` | 策略自己安排的定时事件 |
| `LOT_RELEASED` | Engine 投料后立即进行的第二次决策 |

#### `release_opportunity`

只有它为 `True` 时，策略才能返回：

```python
decision.release_lot = True
```

否则 Engine 会抛出异常，防止策略绕开投料周期。

#### `strategy_reasons`

策略定时事件的原因。例如 DBR v2：

```python
("sub_bottleneck_update",)
```

或：

```python
("main_bottleneck_update",)
```

### 18.4 `StrategyWakeup`

让策略把自己的定时任务注入事件日历：

```python
StrategyWakeup(
    time=state.current_time + 5,
    reason="sub_bottleneck_update",
)
```

规则：

- `time` 必须严格大于当前仿真时间；
- `reason` 由策略自行解释；
- Engine 不理解 reason，只负责准时传回；
- 超过仿真终点的事件不会进入日历。

### 18.5 `StrategyDecision`

`push_next()` 的返回值：

```python
decision = StrategyDecision()
decision.release_lot = True
decision.dispatches["M01"] = wafer
decision.wakeups.append(
    StrategyWakeup(
        time=state.current_time + 5,
        reason="update",
    )
)
```

字段：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `release_lot` | `bool` | 是否在本次投料机会释放队首 lot |
| `dispatches` | `dict[machine_id, WaferState]` | 各机器下一步加工哪个 lot |
| `wakeups` | `list[StrategyWakeup]` | 注入未来事件日历的策略定时事件 |

Engine 会验证派工决策：

- machine ID 必须存在；
- 机器当前必须空闲；
- lot 必须 ready；
- lot 不能正在加工或已经完工；
- lot 当前工序必须等于 `machine.type`；
- 同一个 lot 不能同时分给多台机器。

### 18.6 `FabStrategy`

策略对象协议要求以下属性和方法：

```python
name: str
run_id: str
release_interval: float

initialize(context)
push_next(state, trigger)
sample(state)
result_fields()
```

#### `initialize(context)`

每轮仿真开始调用一次。复用同一个策略对象运行多轮仿真时，应在这里清空
上一轮的动态状态。

#### `push_next(state, trigger)`

策略主入口。返回 `StrategyDecision`。

不要在其中直接执行：

```python
wafer.step += 1
machine.available_time = ...
state.waiting_list.release_head(...)
```

#### `sample(state)`

返回自定义采样字典：

```python
def sample(self, state):
    return {
        "my_samples": {
            "time": state.current_time,
            "value": 123,
        }
    }
```

结果会追加到：

```python
result["my_samples"]
```

#### `result_fields()`

返回只写入一次的策略元数据：

```python
def result_fields(self):
    return {
        "strategy_parameters": self.parameters,
    }
```

### 18.7 `FabStrategyBase`

适合 FIFO、SPT、Batching 等“逐台机器选一个 lot”的策略。

通常只需重写：

```python
select_job(state, machine, reserved_wafer_ids)
```

可选重写：

```python
should_release(state)
sample(state)
result_fields()
initialize(context)
```

基础类的 `push_next()` 会：

1. 判断是否处于投料机会；
2. 调用 `should_release()`；
3. 遍历空闲机器；
4. 调用 `select_job()`；
5. 使用 `reserved_wafer_ids` 防止重复分配；
6. 返回完整 `StrategyDecision`。

`reserved_wafer_ids` 必须用于过滤候选：

```python
candidates = [
    lot
    for lot in ready_candidates(...)
    if lot.id not in reserved_wafer_ids
]
```

### 18.8 `FabSimulationEngine`

构造：

```python
engine = FabSimulationEngine(
    factory_config=factory_config,
    simulation_config=simulation_config,
    order_pool=order_pool,
    strategy=strategy,
)
```

#### `run()`

运行到固定仿真终点并返回结果字典。主要工作：

- 构造机器和 lot 状态；
- 调用策略初始化；
- 维护事件优先队列；
- 生成订单和处理投料；
- 验证并执行策略决策；
- 处理 setup、加工、故障和维修；
- 调用策略采样；
- 构造最终指标。

### 18.9 核心辅助函数

#### `ready_candidates(wafers, machine_type, current_time)`

返回某机器类型当前可加工的全部 lot。

#### `create_order_lot(generation_time, product, order_id, lot_number)`

为订单池创建尚未投料的 `WaferState`。其 `release_time` 和 `ready_time`
初始化为无穷大，直到 Engine 正式投料。

#### `build_states(factory_config)`

根据工厂配置创建：

```python
machines, wafers, products
```

主要由 Engine 使用，策略通常不直接调用。

#### `normalize_route(product)`

校验 route 中每一步包含有效的 `process` 和正数 `process_time`，并返回复制后的标准路线。

#### `save_result(result, path)`

将结果保存为 UTF-8 JSON。

#### `post_result(result, url)`

将结果通过 HTTP POST 发送给 dashboard。

### 18.10 `FabOrderPool`

#### `generate_order(generation_time)`

按产品权重生成一张订单及其多个 lot，并放入 waiting list。返回订单事件记录。

#### `release_head(release_time)`

释放 waiting list 队首 lot，设置其 `release_time` 和 `ready_time`。
策略不要直接调用，由 Engine 根据 `StrategyDecision.release_lot` 调用。

#### `snapshot()`

返回 waiting list 的可序列化快照，不包含完整运行时状态。

### 18.11 配置函数

#### `load_json(path)`

读取 UTF-8 JSON。

#### `load_seed_sets(path)`

读取并校验所有随机种子组。

#### `load_seed_set(path, seed_index)`

按 1-based 下标读取一组种子。

#### `apply_factory_seeds(factory_config, seed_set)`

复制工厂配置，并注入 downtime 与 setup 随机种子，不修改原始字典。

---

## 十九、`DynamicDBR` API 参考

### 19.1 构造函数

```python
DynamicDBR(
    parameters,
    release_interval=38.0,
    main_bottleneck_window=720.0,
    sub_bottleneck_window=5.0,
    alpha=0.5,
    lam=0.5,
)
```

参数：

| 参数 | 单位 | 含义 |
| --- | --- | --- |
| `parameters` | - | 复合优先级权重和目标值 |
| `release_interval` | minute | 投料检查间隔 |
| `main_bottleneck_window` | minute | 主瓶颈更新周期和负荷窗口 |
| `sub_bottleneck_window` | minute | 次瓶颈更新周期和短期产能窗口 |
| `alpha` | - | BD 指数平滑当前值权重，范围 `[0,1]` |
| `lam` | - | IBD 指数映射系数，必须大于 0 |

`parameters` 必须包含：

```text
L0, BL,
m0, m1, m2,
n0, n1, n2,
s1, s2,
p1, p2,
r
```

### 19.2 `initialize(context)`

缓存：

- `products` 和 `product_count`；
- 每个产品的完整 `recipes`；
- `machine_configs`；
- 按 process 分组的 `machine_groups`；
- 合并故障模型后的 `machine_parameters`；
- `process_names`。

同时重置：

```python
main_bottleneck
sub_bottleneck
previous_BDm
last_main_bottleneck_update
last_sub_bottleneck_update
timers_started
```

### 19.3 产能方法

#### `cal_machine_availability(machine)`

\[
A_m=\frac{MTBF}{MTBF+MTTR}
\]

无故障模型时返回 `1.0`。

#### `cal_capacity_rate(machines)`

将同一工作站的并行机器可用率相加：

\[
R_m=\sum_k A_{mk}
\]

单位为 `machine-minute/minute`。

#### `cal_Cm(machine, window=720)`

单台机器在窗口中的有效产能：

\[
C_m(T)=T\cdot A_m
\]

单位为 `machine-minute`。

### 19.4 主瓶颈识别

#### `get_mbottolneck(machines, waiting_list, products, window=720)`

注意：当前函数名沿用代码中的 `get_mbottolneck` 拼写。

计算 Fab 外 waiting list 中各产品对各工作站的完整路线需求：

\[
D_m=\sum_n \omega_{nm}Q_n
\]

并计算：

\[
b_m=D_m/C_m
\]

返回：

```python
(
    main_bottleneck,
    max_load_ratio,
    Dm_list,
    Cm_list,
)
```

当前实现只使用 Fab 外 waiting list 的订单需求，不包含 Fab 内剩余路线负荷。

### 19.5 次瓶颈识别

#### `cal_IBD(state, process, lam=None, window=None)`

先累计工作站前 ready 队列的加工工作量：

\[
QL_m=\sum_{h\in queue_m}PT_h
\]

然后：

\[
IBD_m=1-e^{-\lambda QL_m/C_m}
\]

输入 `process` 是工作站类型，例如 `"Litho"`，不是机器 ID。

#### `get_sbottleneck(...)`

指数平滑：

\[
BD_m(t)=\alpha IBD_m(t)+(1-\alpha)BD_m(t-T)
\]

排除当前主瓶颈后，选择 BD 最大的 process。

返回：

```python
(
    sub_bottleneck,
    sub_bottleneck_degree,
    current_BDm_list,
)
```

### 19.6 Layer 方法

#### `build_layer_list(mbottleneck, wafer)`

按主瓶颈访问顺序划分路线：

```text
Layer 1：投料到第一次主瓶颈结束
Layer 2：第一次主瓶颈结束后到第二次结束
...
```

返回：

```python
[
    {
        "layer": 1,
        "start_step": 0,
        "end_step": 2,
        "PT": 20.0,
        "FT": 45.0,
    }
]
```

#### `cal_PT(layer_list)`

返回：

```python
{1: 20.0, 2: 30.0}
```

#### `cal_FT(layer_list)`

返回每层理想流动时间：

```python
{1: 45.0, 2: 62.0}
```

#### `get_layer(layer_list, wafer)`

根据 `wafer.step` 判断当前 Layer。通过最后一次主瓶颈后返回 `None`。

#### `get_layer_load(mbottleneck, wafers)`

计算：

\[
L_j=\sum_{h\in Layer_j}\frac{PT_{hj}}{FT_{hj}}
\]

不同产品不分开编号，均按“第几次主瓶颈访问”汇总。

#### `cal_bottleneck_heartbeat(state, mbottleneck, layer_list)`

计算 Drum 节拍：

\[
D_t=\frac{\sum_j PT_j}{capacity\_rate}
\]

返回单位为 minute。

### 19.7 Operation Buffer 方法

#### `get_operation_key(wafer, step=None)`

将操作表示成：

```python
(process, visit_number)
```

例如：

```python
("Litho", 2)
```

这样不同产品绝对 step 不同，也能按第二次 Litho 统一统计。

#### `wafer_is_waiting_for_operation(wafer, operation_key, current_time)`

判断 lot 是否：

- 未完工；
- 不在加工；
- 已经 ready；
- 当前操作键等于目标键。

#### `cal_Bi(state, operation_key)`

先累计等待操作 i 的 machine-minute，再除以对应工作站有效产能率：

\[
B_i=\frac{\sum_{h\in queue_i}PT_{hi}}{capacity\_rate_i}
\]

返回 Buffer 清空时间，单位 minute。

#### `cal_BNj(state, mbottleneck, j)`

将第 j 次主瓶颈转换成：

```python
(mbottleneck, j)
```

然后复用 `cal_Bi()`。

#### `get_bottleneck_visit(wafer, mbottleneck, step=None)`

若指定 step 是主瓶颈，返回这是第几次访问；否则返回 `None`。

### 19.8 优先级方法

#### `cal_Pmi(...)`

仅对主瓶颈操作生效。使用：

- 当前操作 Buffer `Bi`；
- 当前 Layer Load `Lj`；
- 下一 Layer Load `Lj+1`；
- `L0` 和 `m0/m1/m2`。

最后一次主瓶颈没有下一 Layer 项。

#### `cal_Pni(...)`

仅对非主瓶颈操作生效。使用：

- 当前操作 Buffer `Bi`；
- 当前 Layer Load `Lj`；
- 当前 Layer 末尾主瓶颈 Buffer `B_BNj`。

通过最后一次主瓶颈后的收尾操作只保留 `n0 * Bi`。

#### `cal_Psi(state, wafer, sbottleneck, s1, s2)`

检查当前操作的直接上游或下游是否为次瓶颈：

- 下一步是次瓶颈：使用 `s1 * B_next`；
- 上一步是次瓶颈：使用 `s2 * B_previous`。

按照论文文字，“抑制上游、提高下游”时通常设置：

```python
s1 < 0
s2 > 0
```

#### `cal_Poi(state, wafer, mbottleneck, p1, p2)`

\[
P_{Oi}=p_1\left(\frac{t-t_i}{D_t}-1\right)+p_2\frac{i}{I}
\]

`ti` 来自：

```python
state.last_operation_completion[wafer.current_operation_id]
```

从未完成过的操作将节拍偏差项初始化为 0。

#### `get_remaining_bottleneck_load(state, mbottleneck)`

累计 Fab 内所有未完工 lot 从当前 step 开始的剩余主瓶颈工作量。

#### `cal_Pri(state, mbottleneck, BL, r)`

\[
P_{Ri}=r\left(1-\frac{remaining\_load}{BL}\right)
\]

当前 `push_next()` 使用 `PRi > 0` 作为是否投料的门槛。

#### `cal_compound_priority(...)`

统一计算并返回：

```python
{
    "PMi": ...,
    "PNi": ...,
    "PSi": ...,
    "POi": ...,
    "PRi": ...,
    "total": ...,
}
```

普通 Fab 内派工时 `release_operation=False`，因此 `PRi=0`。

### 19.9 `push_next(state, trigger)`

DBR v2 的完整事件决策入口：

1. 首次调用时计算初始主/次瓶颈；
2. 注入 5 分钟次瓶颈定时事件；
3. 注入 720 分钟主瓶颈定时事件；
4. 收到定时 reason 后更新并预约下一次；
5. 计算本时刻 Layer Load；
6. 在投料机会计算 `PRi`；
7. 对每台空闲机器收集候选 lot；
8. 计算复合优先级；
9. 选择最高优先级 lot；
10. 返回 `StrategyDecision`。

优先级相同时按 FIFO 字段稳定选择。

### 19.10 瓶颈更新时间方法

#### `update_main_bottleneck(state, force=False)`

默认根据 `main_bottleneck_window` 判断是否到期。DBR 的策略定时事件调用时使用：

```python
self.update_main_bottleneck(state, force=True)
```

当前默认周期为 720 分钟。

#### `update_sub_bottleneck(state, force=False)`

更新次瓶颈并覆盖：

```python
self.previous_BDm
```

供下一周期指数平滑。当前默认周期为 5 分钟。

### 19.11 `sample(state)`

记录：

```python
{
    "time": ...,
    "main_bottleneck": ...,
    "sub_bottleneck": ...,
    "layer_loads": ...,
    "BDm": ...,
}
```

最终保存到：

```python
result["dbr_v2_samples"]
```

### 19.12 `result_fields()`

将优先级参数和更新周期保存到结果 JSON：

```python
result["dbr_v2_parameters"]
result["dbr_v2_update_policy"]
```

---

## 二十、Engine 内部函数参考

以下函数通常不由策略直接调用，但理解它们有助于定位仿真问题。

| 函数 | 作用 |
| --- | --- |
| `random_duration()` | 从配置区间采样 setup、MTBF 或 MTTR 时长 |
| `build_machine_state()` | 创建机器运行状态并安排首次故障 |
| `sample_next_failure()` | 维修完成后安排下一次故障 |
| `repair_duration()` | 采样维修时长 |
| `setup_duration()` | 根据产品切换和机器类型计算 setup |
| `register_downtime()` | 记录故障并推进到维修完成 |
| `clear_idle_failures()` | 清理机器空闲期间已经发生的故障 |
| `schedule_interruptible_segment()` | 安排可被故障中断的 setup/process 段 |
| `queue_snapshot()` | 记录各工序当前 ready queue 长度 |
| `now_iso()` | 生成结果 UTC 时间戳 |

`schedule_interruptible_segment()` 可能把一次加工拆成多个 segment。策略不要假设
一个 route operation 在结果中只对应一个 process event。

---

## 二十一、策略开发检查表

提交或比较一个新策略前，检查：

- [ ] 策略类具有 `name`、`run_id`、`release_interval`；
- [ ] `initialize()` 会清空上一轮动态状态；
- [ ] `push_next()` 总是返回 `StrategyDecision`；
- [ ] 只在 `trigger.release_opportunity` 时请求投料；
- [ ] 定时 wakeup 的时间严格大于当前时间；
- [ ] 不把同一 lot 分配给多台机器；
- [ ] 不直接修改 lot、machine 或 waiting list；
- [ ] 候选 lot 由 `ready_candidates()` 或等价条件筛选；
- [ ] 所有时间量明确使用 minute；
- [ ] 重入操作不能只按绝对 step 在多产品间汇总；
- [ ] 结果中保存了策略参数；
- [ ] 使用多组随机种子进行比较；
- [ ] 与 FIFO 基线比较 throughput、MCT、WIP 和 movements；
- [ ] 检查 `dbr_v2_samples` 中瓶颈是否按 5/720 分钟变化。
