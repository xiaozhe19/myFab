from __future__ import annotations

import json
import random
import heapq
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib import request

from fab_orders import FabOrderPool


@dataclass
class MachineState:
    """
    单台机器的运行状态。

    这是通用仿真层的数据结构，所有策略共用。
    策略可以读取这些字段来做派工判断，但最好不要改变字段含义。
    """

    id: str
    name: str
    type: str
    available_time: float
    current_product_id: str | None = None
    downtime_model: dict[str, Any] | None = None
    downtime_rng: random.Random | None = None
    next_failure_time: float | None = None


@dataclass
class WaferState:
    """
    单片 wafer 的运行状态。

    """

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
    operation_history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def completed(self) -> bool:
        return self.step >= len(self.route)

    @property
    def current_process(self) -> str | None:
        if self.completed:
            return None
        return str(self.route[self.step]["process"])

    @property
    def current_process_time(self) -> float | None:
        """当前 route step 自己定义的加工时间。"""

        if self.completed:
            return None
        return float(self.route[self.step]["process_time"])

    @property
    def current_operation(self) -> dict[str, Any] | None:
        """当前 route step 的完整配置。"""

        if self.completed:
            return None
        return self.route[self.step]

    @property
    def current_operation_id(self) -> str | None:
        """区分同一设备被多次重入时的具体 route operation。"""

        if self.completed:
            return None
        return f"{self.product_id}:{self.step}"


@dataclass
class FactoryState:
    """供高级调度/投料策略读取的完整工厂快照。"""

    current_time: float
    machines: list[MachineState]
    wafers: list[WaferState]
    waiting_list: FabOrderPool
    products: dict[str, Any]
    factory_config: dict[str, Any]
    simulation_config: dict[str, Any]
    machine_events: list[dict[str, Any]]
    last_operation_completion: dict[str, float]


@dataclass(frozen=True)
class StrategyInitialization:
    """策略初始化时获得的静态仿真信息。"""

    factory_config: dict[str, Any]
    simulation_config: dict[str, Any]
    products: dict[str, Any]
    process_names: tuple[str, ...]


@dataclass(frozen=True)
class StrategyTrigger:
    """
    本轮 push_next 的触发信息。

    event_kinds 是当前时刻刚刚结算的事件类型。release_opportunity 表示
    当前时刻是否到达投料检查点；策略只能在这个机会决定是否投料。
    """

    event_kinds: tuple[str, ...]
    release_opportunity: bool = False
    strategy_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyWakeup:
    """
    策略请求 Engine 在指定仿真时间重新调用 push_next。

    reason 完全由策略定义；Engine 只负责准时保存和传回。
    """

    time: float
    reason: str


@dataclass
class StrategyDecision:
    """
    策略交给引擎执行的下一步动作。

    dispatches 的键是 machine.id，值是该机器要加工的 WaferState。
    release_lot=True 表示在本次投料机会释放 waiting list 队首 lot。
    """

    dispatches: dict[str, WaferState] = field(default_factory=dict)
    release_lot: bool = False
    wakeups: list[StrategyWakeup] = field(default_factory=list)


class FabStrategy(Protocol):
    """FabSimulationEngine 接受的统一策略对象接口。"""

    name: str
    run_id: str
    release_interval: float

    def initialize(self, context: StrategyInitialization) -> None:
        """仿真开始前调用一次，策略可在这里缓存静态信息。"""

    def push_next(
        self,
        state: FactoryState,
        trigger: StrategyTrigger,
    ) -> StrategyDecision:
        """事件结算后返回下一步投料和派工决策。"""

    def sample(self, state: FactoryState) -> dict[str, Any]:
        """返回策略自定义采样；没有额外采样时返回空字典。"""

    def result_fields(self) -> dict[str, Any]:
        """返回需要写入结果 JSON 的策略元数据。"""


@dataclass(order=True)
class SimulationEvent:
    """事件日历中的一条记录。"""

    time: float
    priority: int
    sequence: int
    kind: str = field(compare=False)
    payload: dict[str, Any] = field(default_factory=dict, compare=False)


EVENT_PRIORITY = {
    "OP_COMPLETE": 10,
    "ORDER_ARRIVAL": 20,
    "LOT_RELEASE": 30,
    "MACHINE_FAILURE": 40,
    "STRATEGY_WAKEUP": 45,
    "MACHINE_READY": 50,
    "WAFER_READY": 60,
    "SIM_END": 100,
}


def now_iso() -> str:
    """生成结果文件里的 UTC 时间戳。"""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def save_result(result: dict[str, Any], path: Path) -> None:
    """把仿真结果保存成 dashboard 可以读取的 JSON。"""

    with path.open("w", encoding="utf-8") as file:
        json.dump(result, file, indent=2, ensure_ascii=False)
        file.write("\n")


def post_result(result: dict[str, Any], url: str) -> None:
    """可选：把结果 POST 到本地 dashboard API。"""

    body = json.dumps(result).encode("utf-8")
    http_request = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(http_request, timeout=10) as response:
        if response.status >= 400:
            raise RuntimeError(f"Dashboard API returned HTTP {response.status}")


def random_duration(
    rng: random.Random, model: dict[str, Any], min_key: str, max_key: str
) -> float:
    """
    从配置的 [min, max] 区间采样时间。

    setup、repair 都复用这个函数。固定 seed 后可以复现实验。
    """

    low = float(model.get(min_key, 0))
    high = float(model.get(max_key, low))
    if high <= low:
        return low
    return round(rng.uniform(low, high), 3)


def build_machine_state(
    machine: dict[str, Any],
    downtime: dict[str, Any],
    downtime_seed: int,
    index: int,
) -> MachineState:
    """根据配置构造单台机器，并初始化它的随机故障时间。"""

    available_from = float(machine.get("available_from", 0))
    model = (
        downtime.get("models", {}).get(machine["id"])
        if downtime.get("enabled", False)
        else None
    )
    rng = random.Random(downtime_seed + index * 9973) if model else None
    next_failure_time = None
    if model and rng:
        next_failure_time = available_from + random_duration(
            rng, model, "mtbf_min", "mtbf_max"
        )

    return MachineState(
        id=machine["id"],
        name=machine.get("name", machine["id"]),
        type=machine["type"],
        available_time=available_from,
        downtime_model=model,
        downtime_rng=rng,
        next_failure_time=next_failure_time,
    )


def normalize_route(
    product: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    校验并复制产品路线。

    每一个 route step 必须自己包含：
    - process：需要的设备类型
    - process_time：该产品在该步骤的加工时间
    """

    product_id = str(product.get("id", "<unknown>"))
    route = product.get("route")
    if not isinstance(route, list) or not route:
        raise ValueError(f"Product {product_id} must contain a non-empty route.")

    normalized: list[dict[str, Any]] = []
    for step, operation in enumerate(route):
        if not isinstance(operation, dict):
            raise ValueError(
                f"Product {product_id} route step {step} must be an object "
                "with process and process_time."
            )
        if not operation.get("process"):
            raise ValueError(
                f"Product {product_id} route step {step} is missing process."
            )
        if "process_time" not in operation:
            raise ValueError(
                f"Product {product_id} route step {step} is missing process_time."
            )

        process_time = float(operation["process_time"])
        if process_time <= 0:
            raise ValueError(
                f"Product {product_id} route step {step} process_time "
                "must be greater than zero."
            )

        normalized.append(
            {
                **operation,
                "process": str(operation["process"]),
                "process_time": process_time,
            }
        )
    return normalized


def build_states(
    factory_config: dict[str, Any],
) -> tuple[list[MachineState], list[WaferState], dict[str, Any]]:
    """
    把 JSON 配置转换成内存里的 machine / wafer 对象。

    这一步所有策略都一样，所以放在 core 里。
    """

    products = {
        product["id"]: product for product in factory_config["products"]
    }
    downtime = factory_config.get("machine_downtime", {})
    downtime_seed = int(
        downtime.get("random_seed", 0)
    )

    machines = [
        build_machine_state(machine, downtime, downtime_seed, index)
        for index, machine in enumerate(factory_config["machines"])
    ]

    wafers: list[WaferState] = []
    for index, wafer in enumerate(factory_config.get("wafers", [])):
        product = products[wafer["product_id"]]
        release_time = float(wafer.get("release_time", 0))
        wafers.append(
            WaferState(
                id=wafer["id"],
                product_id=wafer["product_id"],
                product_name=product.get("name", wafer["product_id"]),
                route=normalize_route(product),
                generation_time=float(wafer.get("generation_time", release_time)),
                release_time=release_time,
                due_time=(
                    float(wafer["due_time"])
                    if wafer.get("due_time") is not None
                    else None
                ),
                priority=int(wafer.get("priority", 1)),
                input_order=index,
                record_result=bool(wafer.get("record_result", True)),
                ready_time=release_time,
            )
        )

    return machines, wafers, products


def create_order_lot(
    generation_time: float,
    product: dict[str, Any],
    order_id: str,
    lot_number: int,
) -> WaferState:
    """为订单模块创建一个尚未投料的生产 lot。"""

    return WaferState(
        id=f"W{lot_number:03d}",
        product_id=product["id"],
        product_name=product.get("name", product["id"]),
        route=normalize_route(product),
        generation_time=round(generation_time, 3),
        release_time=float("inf"),
        due_time=None,
        priority=1,
        input_order=lot_number - 1,
        order_id=order_id,
        ready_time=float("inf"),
    )


def ready_candidates(
    wafers: list[WaferState], machine_type: str, current_time: float
) -> list[WaferState]:
    """
    找出当前机器可加工的候选 wafer。

    策略文件通常第一步都会调用它，然后只负责排序。
    """

    return [
        wafer
        for wafer in wafers
        if not wafer.completed
        and not wafer.in_process
        and wafer.ready_time <= current_time
        and wafer.current_process == machine_type
    ]


class FabStrategyBase:
    """
    常规策略的基础类。

    子类通常只需要实现 select_job()；需要投料控制时重写 should_release()，
    需要监控数据时重写 sample()。push_next() 负责把这些局部规则组合成
    一次完整决策，并保证同一个 lot 不会同时分配给两台机器。
    """

    name = "Unnamed Strategy"
    run_id = "unnamed-strategy"
    release_interval = 38.0

    def __init__(self) -> None:
        self.context: StrategyInitialization | None = None

    def initialize(self, context: StrategyInitialization) -> None:
        self.context = context

    def select_job(
        self,
        state: FactoryState,
        machine: MachineState,
        reserved_wafer_ids: set[str],
    ) -> WaferState | None:
        raise NotImplementedError

    def should_release(self, state: FactoryState) -> bool:
        return True

    def push_next(
        self,
        state: FactoryState,
        trigger: StrategyTrigger,
    ) -> StrategyDecision:
        decision = StrategyDecision(
            release_lot=(
                trigger.release_opportunity
                and bool(state.waiting_list.queue)
                and self.should_release(state)
            )
        )
        reserved_wafer_ids: set[str] = set()
        for machine in sorted(
            state.machines,
            key=lambda item: (item.available_time, item.id),
        ):
            if machine.available_time > state.current_time:
                continue
            wafer = self.select_job(state, machine, reserved_wafer_ids)
            if wafer is None:
                continue
            if wafer.id in reserved_wafer_ids:
                raise ValueError(
                    f"Strategy {self.name} assigned wafer {wafer.id} more than once."
                )
            decision.dispatches[machine.id] = wafer
            reserved_wafer_ids.add(wafer.id)
        return decision

    def sample(self, state: FactoryState) -> dict[str, Any]:
        return {}

    def result_fields(self) -> dict[str, Any]:
        return {}


def sample_next_failure(machine: MachineState, from_time: float) -> None:
    """机器修复完成后，安排下一次随机故障。"""

    if not machine.downtime_model or not machine.downtime_rng:
        machine.next_failure_time = None
        return

    machine.next_failure_time = round(
        from_time
        + random_duration(
            machine.downtime_rng, machine.downtime_model, "mtbf_min", "mtbf_max"
        ),
        3,
    )


def repair_duration(machine: MachineState) -> float:
    """采样一次故障修复时长。"""

    if not machine.downtime_model or not machine.downtime_rng:
        return 0.0
    return random_duration(
        machine.downtime_rng, machine.downtime_model, "mttr_min", "mttr_max"
    )


def setup_duration(
    config: dict[str, Any],
    rng: random.Random,
    machine: MachineState,
    from_product_id: str | None,
    to_product_id: str,
) -> float:
    """计算机器从一个产品切换到另一个产品的 setup 时间。"""

    setup = config.get("setup", {})
    if not setup.get("enabled", False):
        return 0.0

    if from_product_id is None or from_product_id == to_product_id:
        model = setup.get("same_product_time", {"min": 0, "max": 0})
    else:
        key = f"{from_product_id}->{to_product_id}"
        model = setup.get("product_change_overrides", {}).get(
            key, setup.get("default_product_change_time", {})
        )

    base = random_duration(rng, model, "min", "max")
    multiplier = float(setup.get("machine_type_multiplier", {}).get(machine.type, 1.0))
    return round(base * multiplier, 3)


def register_downtime(
    machine: MachineState, start: float, machine_events: list[dict[str, Any]]
) -> float:
    """记录一次 downtime，并把机器时间推进到修复完成。"""

    repair = repair_duration(machine)
    end = round(start + repair, 3)
    machine_events.append(
        {
            "kind": "downtime",
            "machine_id": machine.id,
            "reason": "random_failure",
            "start": round(start, 3),
            "end": end,
            "duration": repair,
        }
    )
    sample_next_failure(machine, end)
    machine.available_time = max(machine.available_time, end)
    return end


def clear_idle_failures(
    machine: MachineState, current_time: float, machine_events: list[dict[str, Any]]
) -> bool:
    """处理机器空闲期间已经发生的故障。"""

    changed = False
    while (
        machine.next_failure_time is not None
        and machine.next_failure_time <= current_time
    ):
        current_time = register_downtime(
            machine, machine.next_failure_time, machine_events
        )
        changed = True
    return changed


def schedule_interruptible_segment(
    machine: MachineState,
    start: float,
    duration: float,
    machine_events: list[dict[str, Any]],
    kind: str,
    segment_meta: dict[str, Any],
) -> float:
    """
    安排一段可能被 downtime 打断的时间段。

    kind 可以是 setup 或 process。遇到故障时会自动拆段。
    """

    actual_start = round(start, 3)
    current = actual_start
    remaining = round(duration, 3)
    segment_index = 0

    while remaining > 0:
        failure_time = machine.next_failure_time
        if failure_time is None or current + remaining <= failure_time:
            end = round(current + remaining, 3)
            machine_events.append(
                {
                    "kind": kind,
                    "machine_id": machine.id,
                    "start": current,
                    "end": end,
                    "duration": round(end - current, 3),
                    "segment_index": segment_index,
                    "final_segment": True,
                    **segment_meta,
                }
            )
            machine.available_time = end
            return end

        worked = round(max(failure_time - current, 0.0), 3)
        if worked > 0:
            machine_events.append(
                {
                    "kind": kind,
                    "machine_id": machine.id,
                    "start": current,
                    "end": failure_time,
                    "duration": worked,
                    "segment_index": segment_index,
                    "final_segment": False,
                    **segment_meta,
                }
            )
            remaining = round(remaining - worked, 3)
            current = failure_time
            segment_index += 1

        current = register_downtime(machine, failure_time, machine_events)
        machine.available_time = current

    return current


def queue_snapshot(
    wafers: list[WaferState], process_names: list[str], current_time: float
) -> list[dict[str, Any]]:
    """记录当前时刻每个工序前 ready queue 的长度。"""

    samples = []
    for process_name in process_names:
        length = sum(
            1
            for wafer in wafers
            if not wafer.completed
            and wafer.ready_time <= current_time
            and wafer.current_process == process_name
            and wafer.record_result
        )
        samples.append({"time": current_time, "queue": process_name, "length": length})
    return samples


class FabSimulationEngine:
    """
    基于统一事件日历的通用 fab 离散事件仿真引擎。
    """

    def __init__(
        self,
        factory_config: dict[str, Any],
        simulation_config: dict[str, Any],
        order_pool: FabOrderPool,
        strategy: FabStrategy,
    ) -> None:
        if strategy.release_interval <= 0:
            raise ValueError("strategy.release_interval must be greater than zero.")
        self.factory_config = factory_config
        self.simulation_config = simulation_config
        self.order_pool = order_pool
        self.strategy = strategy
        self.release_interval = float(strategy.release_interval)
        self.run_id = strategy.run_id
        self.strategy_name = strategy.name

    def run(self) -> dict[str, Any]:
        machines, wafers, products = build_states(self.factory_config)
        simulation = self.simulation_config
        start_time = float(simulation.get("start_time", 0))
        warmup_time = float(simulation.get("warmup_time", 0))
        measurement_start_time = start_time + warmup_time
        measurement_duration = float(simulation.get("measurement_duration", 0))
        if measurement_duration <= 0:
            raise ValueError("simulation.measurement_duration must be greater than zero.")
        simulation_end_time = measurement_start_time + measurement_duration
        waiting_list = self.order_pool
        order_events: list[dict[str, Any]] = []
        release_events: list[dict[str, Any]] = []
        setup_seed = int(
            self.factory_config.get("setup", {}).get("random_seed", 0)
        )
        setup_rng = random.Random(setup_seed)
        process_names = list(dict.fromkeys(machine.type for machine in machines))
        current_time = start_time

        machine_events: list[dict[str, Any]] = []
        queue_samples: list[dict[str, Any]] = []
        extra_samples: dict[str, list[Any]] = {}
        sampled_times: set[float] = set()
        last_operation_completion: dict[str, float] = {}
        event_calendar: list[SimulationEvent] = []
        event_sequence = 0
        machines_by_id = {machine.id: machine for machine in machines}

        self.strategy.initialize(
            StrategyInitialization(
                factory_config=self.factory_config,
                simulation_config=self.simulation_config,
                products=products,
                process_names=tuple(process_names),
            )
        )

        def factory_state() -> FactoryState:
            return FactoryState(
                current_time=current_time,
                machines=machines,
                wafers=wafers,
                waiting_list=waiting_list,
                products=products,
                factory_config=self.factory_config,
                simulation_config=self.simulation_config,
                machine_events=machine_events,
                last_operation_completion=last_operation_completion,
            )

        def push_event(
            event_time: float,
            kind: str,
            payload: dict[str, Any] | None = None,
        ) -> None:
            nonlocal event_sequence
            event_time = round(float(event_time), 3)
            if event_time > simulation_end_time:
                return
            event_sequence += 1
            heapq.heappush(
                event_calendar,
                SimulationEvent(
                    time=event_time,
                    priority=EVENT_PRIORITY[kind],
                    sequence=event_sequence,
                    kind=kind,
                    payload=payload or {},
                ),
            )

        def schedule_idle_failure(machine: MachineState) -> None:
            failure_time = machine.next_failure_time
            if (
                failure_time is not None
                and failure_time >= current_time
                and machine.available_time <= failure_time
            ):
                push_event(
                    failure_time,
                    "MACHINE_FAILURE",
                    {
                        "machine_id": machine.id,
                        "failure_time": failure_time,
                    },
                )

        def prepare_idle_machines() -> None:
            """在策略观察状态前结算空闲期间已经发生的设备故障。"""
            for machine in machines:
                if machine.available_time > current_time:
                    continue
                if clear_idle_failures(machine, current_time, machine_events):
                    push_event(
                        machine.available_time,
                        "MACHINE_READY",
                        {"machine_id": machine.id},
                    )
                schedule_idle_failure(machine)

        def dispatch_decisions(decision: StrategyDecision) -> None:
            assigned_wafer_ids: set[str] = set()
            for machine_id, wafer in decision.dispatches.items():
                machine = machines_by_id.get(machine_id)
                if machine is None:
                    raise ValueError(
                        f"Strategy {self.strategy_name} selected unknown machine "
                        f"{machine_id}."
                    )
                if machine.available_time > current_time:
                    raise ValueError(
                        f"Strategy {self.strategy_name} selected busy machine "
                        f"{machine_id} at time {current_time}."
                    )
                if wafer.id in assigned_wafer_ids:
                    raise ValueError(
                        f"Strategy {self.strategy_name} assigned wafer {wafer.id} "
                        "to multiple machines."
                    )
                if (
                    wafer.completed
                    or wafer.in_process
                    or wafer.ready_time > current_time
                    or wafer.current_process != machine.type
                ):
                    raise ValueError(
                        f"Strategy {self.strategy_name} returned invalid assignment "
                        f"{wafer.id} -> {machine_id} at time {current_time}."
                    )
                assigned_wafer_ids.add(wafer.id)

                process_name = wafer.current_process
                operation_id = wafer.current_operation_id
                process_duration = wafer.current_process_time
                if (
                    process_name is None
                    or operation_id is None
                    or process_duration is None
                ):
                    continue

                setup_time = setup_duration(
                    self.factory_config,
                    setup_rng,
                    machine,
                    machine.current_product_id,
                    wafer.product_id,
                )
                setup_end = current_time
                if setup_time > 0:
                    setup_end = schedule_interruptible_segment(
                        machine,
                        current_time,
                        setup_time,
                        machine_events,
                        "setup",
                        {
                            "wafer_id": wafer.id,
                            "machine_type": machine.type,
                            "from_product_id": machine.current_product_id,
                            "to_product_id": wafer.product_id,
                        },
                    )

                process_start = setup_end
                completion_time = schedule_interruptible_segment(
                    machine,
                    setup_end,
                    process_duration,
                    machine_events,
                    "process",
                    {
                        "wafer_id": wafer.id,
                        "product_id": wafer.product_id,
                        "machine_type": machine.type,
                        "process": process_name,
                        "step": wafer.step,
                    },
                )

                if wafer.start_time is None:
                    wafer.start_time = process_start
                wafer.in_process = True
                wafer.processing_machine_id = machine.id
                wafer.processing_end_time = completion_time
                wafer.ready_time = completion_time
                machine.available_time = completion_time
                machine.current_product_id = wafer.product_id

                push_event(
                    completion_time,
                    "OP_COMPLETE",
                    {
                        "wafer": wafer,
                        "operation_id": operation_id,
                        "process_name": process_name,
                        "machine_id": machine.id,
                        "process_start": process_start,
                    },
                )

            for machine in machines:
                if machine.available_time <= current_time:
                    schedule_idle_failure(machine)

        def apply_strategy(
            event_kinds: tuple[str, ...],
            release_opportunity: bool,
            strategy_reasons: tuple[str, ...],
        ) -> None:
            """
            让策略通过 push_next 给出动作，并由引擎统一执行。

            若本轮释放了新 lot，会再调用一次 push_next，使刚进入 Fab 的 lot
            可以在同一仿真时刻参与派工。
            """
            decision = self.strategy.push_next(
                factory_state(),
                StrategyTrigger(
                    event_kinds=event_kinds,
                    release_opportunity=release_opportunity,
                    strategy_reasons=strategy_reasons,
                ),
            )
            for wakeup in decision.wakeups:
                if wakeup.time <= current_time:
                    raise ValueError(
                        f"Strategy {self.strategy_name} requested wakeup "
                        f"{wakeup.reason!r} at non-future time {wakeup.time}."
                    )
                push_event(
                    wakeup.time,
                    "STRATEGY_WAKEUP",
                    {"reason": wakeup.reason},
                )
            if decision.release_lot and not release_opportunity:
                raise ValueError(
                    f"Strategy {self.strategy_name} requested release without "
                    "a release opportunity."
                )
            released = False
            if release_opportunity and decision.release_lot:
                wafer = waiting_list.release_head(current_time)
                if wafer is not None:
                    wafers.append(wafer)
                    release_events.append(
                        {
                            "wafer_id": wafer.id,
                            "order_id": wafer.order_id,
                            "product_id": wafer.product_id,
                            "generation_time": wafer.generation_time,
                            "release_time": wafer.release_time,
                        }
                    )
                    released = True

            # 与旧引擎保持相同采样时点：事件和投料已经结算，但本轮派工尚未执行。
            if (
                current_time >= measurement_start_time
                and current_time not in sampled_times
            ):
                queue_samples.extend(
                    queue_snapshot(wafers, process_names, current_time)
                )
                self._record_extra_sample(extra_samples, factory_state())
                sampled_times.add(current_time)

            if released:
                follow_up = self.strategy.push_next(
                    factory_state(),
                    StrategyTrigger(event_kinds=("LOT_RELEASED",)),
                )
                for wakeup in follow_up.wakeups:
                    if wakeup.time <= current_time:
                        raise ValueError(
                            f"Strategy {self.strategy_name} requested wakeup "
                            f"{wakeup.reason!r} at non-future time {wakeup.time}."
                        )
                    push_event(
                        wakeup.time,
                        "STRATEGY_WAKEUP",
                        {"reason": wakeup.reason},
                    )
                if follow_up.release_lot:
                    raise ValueError(
                        f"Strategy {self.strategy_name} requested release without "
                        "a release opportunity."
                    )
                dispatch_decisions(follow_up)
            else:
                dispatch_decisions(decision)

        push_event(start_time, "ORDER_ARRIVAL")
        push_event(start_time, "LOT_RELEASE")
        push_event(simulation_end_time, "SIM_END")
        for machine in machines:
            if machine.available_time > start_time:
                push_event(
                    machine.available_time,
                    "MACHINE_READY",
                    {"machine_id": machine.id},
                )
            schedule_idle_failure(machine)
        for wafer in wafers:
            if wafer.ready_time > start_time:
                push_event(
                    wafer.ready_time,
                    "WAFER_READY",
                    {"wafer_id": wafer.id},
                )

        stop_simulation = False
        while event_calendar and not stop_simulation:
            current_time = event_calendar[0].time
            simultaneous_events: list[SimulationEvent] = []
            while event_calendar and event_calendar[0].time == current_time:
                simultaneous_events.append(heapq.heappop(event_calendar))

            meaningful_event = False
            release_opportunity = False
            handled_event_kinds: list[str] = []
            strategy_reasons: list[str] = []
            for event in simultaneous_events:
                if event.kind == "SIM_END":
                    stop_simulation = True
                    continue

                # 仿真终点只结算恰好在终点完成的工序，不再生成订单或投料。
                if current_time >= simulation_end_time and event.kind != "OP_COMPLETE":
                    continue

                if event.kind == "OP_COMPLETE":
                    wafer = event.payload["wafer"]
                    wafer.operation_history.append(
                        {
                            "operation_id": event.payload["operation_id"],
                            "step": wafer.step,
                            "process": event.payload["process_name"],
                            "machine_id": event.payload["machine_id"],
                            "start": event.payload["process_start"],
                            "end": current_time,
                        }
                    )
                    last_operation_completion[event.payload["operation_id"]] = (
                        current_time
                    )
                    wafer.step += 1
                    wafer.ready_time = current_time
                    wafer.in_process = False
                    wafer.processing_machine_id = None
                    wafer.processing_end_time = None
                    if wafer.completed:
                        wafer.end_time = current_time
                    meaningful_event = True
                    handled_event_kinds.append(event.kind)

                elif event.kind == "ORDER_ARRIVAL":
                    order_events.append(waiting_list.generate_order(current_time))
                    push_event(
                        current_time + waiting_list.order_interval,
                        "ORDER_ARRIVAL",
                    )
                    meaningful_event = True
                    handled_event_kinds.append(event.kind)

                elif event.kind == "LOT_RELEASE":
                    push_event(
                        current_time + self.release_interval,
                        "LOT_RELEASE",
                    )
                    release_opportunity = True
                    meaningful_event = True
                    handled_event_kinds.append(event.kind)

                elif event.kind == "MACHINE_FAILURE":
                    machine = machines_by_id[event.payload["machine_id"]]
                    expected_failure = event.payload["failure_time"]
                    if (
                        machine.available_time <= current_time
                        and machine.next_failure_time == expected_failure
                    ):
                        repair_end = register_downtime(
                            machine, current_time, machine_events
                        )
                        push_event(
                            repair_end,
                            "MACHINE_READY",
                            {"machine_id": machine.id},
                        )
                        schedule_idle_failure(machine)
                        meaningful_event = True
                        handled_event_kinds.append(event.kind)

                elif event.kind == "STRATEGY_WAKEUP":
                    strategy_reasons.append(str(event.payload["reason"]))
                    meaningful_event = True
                    handled_event_kinds.append(event.kind)

                elif event.kind in {"MACHINE_READY", "WAFER_READY"}:
                    meaningful_event = True
                    handled_event_kinds.append(event.kind)

            if current_time >= simulation_end_time:
                break
            if not meaningful_event:
                continue

            prepare_idle_machines()
            apply_strategy(
                tuple(handled_event_kinds),
                release_opportunity,
                tuple(strategy_reasons),
            )

        return self._build_result(
            machines,
            wafers,
            machine_events,
            queue_samples,
            extra_samples,
            order_events,
            release_events,
            waiting_list,
            last_operation_completion,
            simulation_end_time,
        )

    def _record_extra_sample(
        self,
        extra_samples: dict[str, list[Any]],
        state: FactoryState,
    ) -> None:
        sample_bundle = self.strategy.sample(state)
        for key, value in sample_bundle.items():
            extra_samples.setdefault(key, []).append(value)

    def _build_result(
        self,
        machines: list[MachineState],
        wafers: list[WaferState],
        machine_events: list[dict[str, Any]],
        queue_samples: list[dict[str, Any]],
        extra_samples: dict[str, list[Any]],
        order_events: list[dict[str, Any]],
        release_events: list[dict[str, Any]],
        waiting_list: FabOrderPool,
        last_operation_completion: dict[str, float],
        simulation_end_time: float,
    ) -> dict[str, Any]:
        end_time = simulation_end_time
        simulation = self.simulation_config
        start_time = float(simulation.get("start_time", 0))
        warmup_time = float(simulation.get("warmup_time", 0))
        measurement_start_time = start_time + warmup_time

        recordable_wafer_ids = {wafer.id for wafer in wafers if wafer.record_result}
        def overlaps_measurement(event: dict[str, Any]) -> bool:
            event_start = float(event.get("start", 0))
            event_end = float(event.get("end", event_start))
            return event_end > measurement_start_time and event_start < end_time

        def clip_to_measurement(event: dict[str, Any]) -> dict[str, Any]:
            clipped = dict(event)
            clipped["start"] = round(max(float(event["start"]), measurement_start_time), 3)
            clipped["end"] = round(min(float(event["end"]), end_time), 3)
            clipped["duration"] = round(clipped["end"] - clipped["start"], 3)
            return clipped

        setup_and_process_events = [
            clip_to_measurement(event)
            for event in machine_events
            if event["kind"] in {"setup", "process"}
            and event.get("wafer_id") in recordable_wafer_ids
            and overlaps_measurement(event)
        ]
        downtime_events = [
            clip_to_measurement(event)
            for event in machine_events
            if event["kind"] == "downtime" and overlaps_measurement(event)
        ]
        operations = sorted(
            setup_and_process_events,
            key=lambda event: (
                event["start"],
                event["machine_id"],
                event["kind"],
                event.get("segment_index", 0),
            ),
        )
        sorted_downtime_events = sorted(
            downtime_events,
            key=lambda event: (event["start"], event["machine_id"], event["kind"]),
        )

        completed_in_measurement = [
            wafer
            for wafer in wafers
            if wafer.record_result
            and wafer.end_time is not None
            and measurement_start_time <= wafer.end_time <= end_time
        ]
        completed_by_end = {
            wafer.id
            for wafer in wafers
            if wafer.end_time is not None and wafer.end_time <= end_time
        }
        movement_keys = {
            (event.get("wafer_id"), event.get("step"))
            for event in machine_events
            if event.get("kind") == "process"
            and event.get("final_segment", True)
            and measurement_start_time <= float(event.get("end", 0)) <= end_time
        }
        average_wip = self._time_weighted_wip(
            wafers, measurement_start_time, end_time
        )
        mct_values = [
            wafer.end_time - wafer.release_time
            for wafer in completed_in_measurement
            if wafer.end_time is not None
        ]

        result = {
            "run_id": self.run_id,
            "task_id": self.simulation_config.get(
                "simulation_id",
                self.factory_config.get("factory_id", "fab-simulation"),
            ),
            "factory_id": self.factory_config.get("factory_id"),
            "strategy": self.strategy_name,
            "time_unit": self.simulation_config.get("time_unit", "minute"),
            "generated_at": now_iso(),
            "current_time": end_time,
            "warmup_time": warmup_time,
            "measurement_start_time": measurement_start_time,
            "machines": [
                {
                    "id": machine["id"],
                    "name": machine.get("name", machine["id"]),
                    "type": machine["type"],
                }
                for machine in self.factory_config["machines"]
            ],
            "products": deepcopy(self.factory_config["products"]),
            "wafers": [
                {
                    "id": wafer.id,
                    "order_id": wafer.order_id,
                    "product_id": wafer.product_id,
                    "product_name": wafer.product_name,
                    "generation_time": wafer.generation_time,
                    "release_time": wafer.release_time,
                    "due_time": wafer.due_time,
                    "priority": wafer.priority,
                    "route": wafer.route,
                    "start_time": wafer.start_time,
                    "end_time": wafer.end_time if wafer.id in completed_by_end else None,
                    "completed": wafer.id in completed_by_end,
                    "step": wafer.step,
                    "in_process": wafer.in_process,
                    "processing_machine_id": wafer.processing_machine_id,
                    "processing_end_time": wafer.processing_end_time,
                    "operation_history": wafer.operation_history,
                }
                for wafer in wafers
                if wafer.record_result
                and (
                    wafer.end_time is None
                    or wafer.end_time >= measurement_start_time
                )
            ],
            "operations": operations,
            "machine_events": sorted_downtime_events,
            "fab_parameters": {
                "simulation": deepcopy(simulation),
                "release_interval": self.release_interval,
                "machine_downtime": deepcopy(
                    self.factory_config.get("machine_downtime", {})
                ),
                "setup": deepcopy(self.factory_config.get("setup", {})),
            },
            "measurement": {
                "warmup_time": warmup_time,
                "measurement_start_time": measurement_start_time,
                "measurement_end_time": end_time,
                "measurement_horizon": max(end_time - measurement_start_time, 0.0),
                "released": len(
                    [
                        event
                        for event in release_events
                        if measurement_start_time <= event["release_time"] < end_time
                    ]
                ),
                "completed_after_warmup": len(completed_in_measurement),
                "throughput": len(completed_in_measurement),
                "movements": len(movement_keys),
                "mct_average": (
                    round(sum(mct_values) / len(mct_values), 3)
                    if mct_values
                    else 0.0
                ),
                "average_fab_wip": round(average_wip, 3),
                "note": "Waiting-list time is excluded from MCT. Metrics cover only the fixed measurement window.",
            },
            "queue_samples": queue_samples,
            "order_events": order_events,
            "release_events": release_events,
            "last_operation_completion": {
                operation_id: round(completion_time, 3)
                for operation_id, completion_time in sorted(
                    last_operation_completion.items()
                )
            },
            "waiting_list": {
                "capacity": waiting_list.capacity,
                "arrival_type": waiting_list.arrival_type,
                "order_interval": waiting_list.order_interval,
                "lots_per_order": waiting_list.lots_per_order,
                "product_mix": waiting_list.product_mix,
                "orders_generated": waiting_list.next_order_number - 1,
                "generated_count": waiting_list.generated_count,
                "final_snapshot": waiting_list.snapshot(),
            },
        }
        result.update(self.strategy.result_fields())
        result.update(extra_samples)
        return result

    @staticmethod
    def _time_weighted_wip(
        wafers: list[WaferState], start_time: float, end_time: float
    ) -> float:
        if end_time <= start_time:
            return 0.0

        events: list[tuple[float, int]] = []
        for wafer in wafers:
            if wafer.release_time >= end_time:
                continue
            release_time = max(wafer.release_time, start_time)
            completion_time = (
                min(wafer.end_time, end_time)
                if wafer.end_time is not None
                else end_time
            )
            if completion_time <= start_time:
                continue
            events.append((release_time, 1))
            events.append((completion_time, -1))

        events.sort(key=lambda item: (item[0], item[1]))
        wip = 0
        area = 0.0
        previous_time = start_time
        for event_time, delta in events:
            event_time = min(max(event_time, start_time), end_time)
            area += wip * (event_time - previous_time)
            wip += delta
            previous_time = event_time
        area += wip * (end_time - previous_time)
        return area / (end_time - start_time)
