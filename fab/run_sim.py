from __future__ import annotations

import importlib
import json
import argparse
from pathlib import Path
from typing import Any

from fab.settings import (
    apply_factory_seeds,
    build_factory_config,
    load_json,
    load_seed_set,
)
from fab.core import (
    FabSimulationEngine,
    FabStrategy,
    create_order_lot,
    _result_detail_path,
    post_result,
    save_result,
)
from order.pool import FabOrderPool
from machine.settings import load_machine_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FACTORY_PATH = PROJECT_ROOT / "fab" / "config" / "factory_config.json"
DEFAULT_PRODUCT_PATH = PROJECT_ROOT / "fab" / "config" / "product_config.json"
DEFAULT_MACHINE_PATH = PROJECT_ROOT / "machine" / "config" / "machine_config.json"
DEFAULT_ORDERS_PATH = PROJECT_ROOT / "order" / "config" / "order_config.json"
DEFAULT_SIMULATION_PATH = PROJECT_ROOT / "fab" / "config" / "simulation_config.json"
DEFAULT_SEEDS_PATH = PROJECT_ROOT / "fab" / "config" / "simulation_seeds.json"
DEFAULT_RESULT_DIR = PROJECT_ROOT / "result"

STRATEGY_ALIASES = {
    "FIFO": "strategy.fifo",
    "BATCHING_FIFO": "strategy.batching_fifo",
    "DBR_v1": "strategy.dbr_v1",
    "DBR_v2": "strategy.dbr_v2",
}


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

    module = importlib.import_module(STRATEGY_ALIASES.get(module_name, module_name))
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


def build_strategy(spec: str, strategy_params: dict[str, Any] | None = None) -> FabStrategy:
    strategy_class = load_strategy_class(spec)
    return strategy_class(**(strategy_params or {}))


def run_simulation_from_files(
    strategy_spec: str,
    strategy_params_path: Path | None = None,
    factory_path: Path = DEFAULT_FACTORY_PATH,
    product_path: Path = DEFAULT_PRODUCT_PATH,
    machine_path: Path = DEFAULT_MACHINE_PATH,
    orders_path: Path = DEFAULT_ORDERS_PATH,
    simulation_path: Path = DEFAULT_SIMULATION_PATH,
    seeds_path: Path = DEFAULT_SEEDS_PATH,
    seed_index: int = 1,
) -> dict[str, Any]:
    seed_set = load_seed_set(seeds_path, seed_index)
    factory_config = apply_factory_seeds(
        build_factory_config(
            load_json(factory_path),
            load_json(product_path),
            load_machine_config(machine_path),
        ),
        seed_set,
    )
    simulation_config = load_json(simulation_path)
    order_pool = build_order_pool(
        factory_config,
        load_json(orders_path),
        seed_set["order_seed"],
    )
    return run_sim(
        build_strategy(strategy_spec, load_strategy_parameters(strategy_params_path)),
        factory_config,
        simulation_config,
        order_pool,
    )


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
    parser.add_argument("--factory", type=Path, default=DEFAULT_FACTORY_PATH)
    parser.add_argument("--products", type=Path, default=DEFAULT_PRODUCT_PATH)
    parser.add_argument("--machine", type=Path, default=DEFAULT_MACHINE_PATH)
    parser.add_argument("--orders", type=Path, default=DEFAULT_ORDERS_PATH)
    parser.add_argument(
        "--simulation",
        type=Path,
        default=DEFAULT_SIMULATION_PATH,
    )
    parser.add_argument("--seeds", type=Path, default=DEFAULT_SEEDS_PATH)
    parser.add_argument("--seed-index", type=int, default=1)
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULT_DIR / "simulation_result.json")
    parser.add_argument(
        "--post",
        help="Optional dashboard API URL, e.g. http://127.0.0.1:5000/api/runs.",
    )
    args = parser.parse_args()

    result = run_simulation_from_files(
        strategy_spec=args.strategy,
        strategy_params_path=args.strategy_params,
        factory_path=args.factory,
        product_path=args.products,
        machine_path=args.machine,
        orders_path=args.orders,
        simulation_path=args.simulation,
        seeds_path=args.seeds,
        seed_index=args.seed_index,
    )

    save_result(result, args.output)
    if args.post:
        post_result(result, args.post)

    print(summarize(result))
    print(f"Saved result overview to {args.output}")
    print(f"Saved result detail to {_result_detail_path(args.output)}")
    if args.post:
        print(f"Posted result to {args.post}")


if __name__ == "__main__":
    main()
