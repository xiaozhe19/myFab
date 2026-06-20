from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
from typing import Any

from fab_config import apply_factory_seeds, load_json, load_seed_set
from fab_orders import FabOrderPool
from fab_sim_core import (
    FabSimulationEngine,
    FabStrategy,
    create_order_lot,
    post_result,
    save_result,
)


def load_strategy_class(spec: str):
    """
    从“模块名:类名”加载策略类。

    示例：
        FIFO:FIFOStrategy
        BATCHING_FIFO:BatchingFIFOStrategy
        DBR_v1:DBRV1Strategy
        DBR_v2:DynamicDBR
    """
    try:
        module_name, class_name = spec.split(":", 1)
    except ValueError as error:
        raise ValueError(
            "--strategy must use the format module:class."
        ) from error

    module = importlib.import_module(module_name)
    try:
        return getattr(module, class_name)
    except AttributeError as error:
        raise ValueError(
            f"Strategy class {class_name!r} was not found in {module_name!r}."
        ) from error


def load_strategy_parameters(path: Path | None) -> dict[str, Any]:
    """读取传给策略 __init__ 的关键字参数。"""
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError("Strategy parameters JSON must contain an object.")
    return payload


def build_order_pool(
    factory_config: dict[str, Any],
    order_config: dict[str, Any],
    order_seed: int,
) -> FabOrderPool:
    """根据配置构建 Fab 外部订单池。"""
    products = {
        product["id"]: product
        for product in factory_config["products"]
    }
    return FabOrderPool(
        order_config,
        products,
        create_order_lot,
        order_seed,
    )


def run_sim(
    strategy: FabStrategy,
    factory_config: dict[str, Any],
    simulation_config: dict[str, Any],
    order_pool: FabOrderPool,
) -> dict[str, Any]:
    """
    运行一次仿真。

    运行模块只负责构造并启动 Engine；所有派工和投料逻辑都属于 strategy。
    """
    engine = FabSimulationEngine(
        factory_config=factory_config,
        simulation_config=simulation_config,
        order_pool=order_pool,
        strategy=strategy,
    )
    return engine.run()


def summarize(result: dict[str, Any]) -> str:
    """生成所有策略通用的命令行摘要。"""
    measurement = result["measurement"]
    setup_count = sum(
        1
        for event in result.get("operations", [])
        if event["kind"] == "setup"
    )
    downtime_count = sum(
        1
        for event in result.get("machine_events", [])
        if event["kind"] == "downtime"
    )
    return (
        f"{result['strategy']} finished: "
        f"throughput {measurement['throughput']}, "
        f"movements {measurement['movements']}, "
        f"MCT {measurement['mct_average']:g}, "
        f"average WIP {measurement['average_fab_wip']:g}, "
        f"setups {setup_count}, downtimes {downtime_count}."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the fab simulator with a strategy object."
    )
    parser.add_argument(
        "--strategy",
        default="FIFO:FIFOStrategy",
        help="Strategy class in module:class format.",
    )
    parser.add_argument(
        "--strategy-params",
        type=Path,
        help="Optional JSON object passed to the strategy constructor.",
    )
    parser.add_argument("--factory", type=Path, default=Path("factory_config.json"))
    parser.add_argument("--orders", type=Path, default=Path("order_config.json"))
    parser.add_argument(
        "--simulation",
        type=Path,
        default=Path("simulation_config.json"),
    )
    parser.add_argument("--seeds", type=Path, default=Path("simulation_seeds.json"))
    parser.add_argument("--seed-index", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("simulation_result.json"))
    parser.add_argument(
        "--post",
        help="Optional dashboard API URL, e.g. http://127.0.0.1:5000/api/runs.",
    )
    args = parser.parse_args()

    seed_set = load_seed_set(args.seeds, args.seed_index)
    factory_config = apply_factory_seeds(
        load_json(args.factory),
        seed_set,
    )
    simulation_config = load_json(args.simulation)
    order_pool = build_order_pool(
        factory_config,
        load_json(args.orders),
        seed_set["order_seed"],
    )

    strategy_class = load_strategy_class(args.strategy)
    strategy = strategy_class(
        **load_strategy_parameters(args.strategy_params)
    )
    result = run_sim(
        strategy,
        factory_config,
        simulation_config,
        order_pool,
    )

    save_result(result, args.output)
    if args.post:
        post_result(result, args.post)

    print(summarize(result))
    print(f"Saved result to {args.output}")
    if args.post:
        print(f"Posted result to {args.post}")


if __name__ == "__main__":
    main()
