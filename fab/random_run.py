from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from fab.core import _build_operation_summary, now_iso
from fab.run_sim import (
    DEFAULT_FACTORY_PATH,
    DEFAULT_MACHINE_PATH,
    DEFAULT_ORDERS_PATH,
    DEFAULT_PRODUCT_PATH,
    DEFAULT_RESULT_DIR,
    DEFAULT_SEEDS_PATH,
    DEFAULT_SIMULATION_PATH,
    build_order_pool,
    build_strategy,
    load_strategy_parameters,
    run_sim,
)
from fab.settings import (
    apply_factory_seeds,
    build_factory_config,
    load_json,
    load_seed_sets,
)
from machine.settings import load_machine_config


DEFAULT_OUTPUT_PATH = DEFAULT_RESULT_DIR / "random_run_result.json"
DEFAULT_MAX_WORKERS = 5


def _numeric_items(payload: dict[str, Any]) -> dict[str, float]:
    return {
        key: float(value)
        for key, value in payload.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def extract_run_metrics(result: dict[str, Any]) -> dict[str, float]:
    """Extract compact numeric metrics from one full simulation result."""
    metrics: dict[str, float] = {}
    metrics.update(_numeric_items(result.get("measurement", {})))

    operation_summary = _build_operation_summary(result)
    totals = operation_summary.get("totals", {})
    for key, value in _numeric_items(totals).items():
        metrics[f"total_{key}"] = value

    machine_utilization = operation_summary.get("machine_utilization", {})
    for key in ("average", "min", "max"):
        value = machine_utilization.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metrics[f"machine_utilization_{key}"] = float(value)

    metrics["setup_count"] = float(
        sum(1 for event in result.get("operations", []) if event.get("kind") == "setup")
    )
    metrics["downtime_count"] = float(
        sum(
            1
            for event in result.get("machine_events", [])
            if event.get("kind") == "downtime"
        )
    )
    metrics["operation_count"] = float(
        sum(
            1
            for event in result.get("operations", [])
            if event.get("kind", "process") == "process"
        )
    )
    return {key: round(value, 6) for key, value in sorted(metrics.items())}


def average_metrics(runs: list[dict[str, Any]]) -> dict[str, float]:
    """Average same-name numeric metrics across all seed runs."""
    metric_names = sorted(
        {
            key
            for run in runs
            for key, value in run.get("metrics", {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
    )
    return {
        key: round(
            mean(float(run["metrics"][key]) for run in runs if key in run["metrics"]),
            6,
        )
        for key in metric_names
    }


def run_one_seed(task: dict[str, Any]) -> dict[str, Any]:
    """Run one seed in a worker process and return compact metrics only."""
    seed_index = int(task["seed_index"])
    seed_set = dict(task["seed_set"])
    strategy_spec = str(task["strategy_spec"])
    strategy_params = deepcopy(task["strategy_params"])
    factory_config = apply_factory_seeds(
        deepcopy(task["base_factory_config"]),
        seed_set,
    )
    order_pool = build_order_pool(
        factory_config,
        deepcopy(task["order_config"]),
        int(seed_set["order_seed"]),
    )
    result = run_sim(
        build_strategy(strategy_spec, strategy_params),
        factory_config,
        deepcopy(task["simulation_config"]),
        order_pool,
    )
    return {
        "seed_index": seed_index,
        "seed_set": deepcopy(seed_set),
        "strategy": result.get("strategy", strategy_spec),
        "metrics": extract_run_metrics(result),
    }


def build_random_run_result(
    strategy_spec: str,
    strategy_params_path: Path | None = None,
    factory_path: Path = DEFAULT_FACTORY_PATH,
    product_path: Path = DEFAULT_PRODUCT_PATH,
    machine_path: Path = DEFAULT_MACHINE_PATH,
    orders_path: Path = DEFAULT_ORDERS_PATH,
    simulation_path: Path = DEFAULT_SIMULATION_PATH,
    seeds_path: Path = DEFAULT_SEEDS_PATH,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, Any]:
    seed_sets = load_seed_sets(seeds_path)
    strategy_params = load_strategy_parameters(strategy_params_path)
    base_factory_config = build_factory_config(
        load_json(factory_path),
        load_json(product_path),
        load_machine_config(machine_path),
    )
    simulation_config = load_json(simulation_path)
    order_config = load_json(orders_path)

    worker_count = max(1, min(int(max_workers), len(seed_sets)))
    runs: list[dict[str, Any]] = []
    strategy_name = None
    active_seeds: set[int] = set()
    tasks = [
        {
            "seed_index": seed_index,
            "seed_set": deepcopy(seed_set),
            "strategy_spec": strategy_spec,
            "strategy_params": deepcopy(strategy_params),
            "base_factory_config": base_factory_config,
            "simulation_config": simulation_config,
            "order_config": order_config,
        }
        for seed_index, seed_set in enumerate(seed_sets, start=1)
    ]

    with ProcessPoolExecutor(max_workers=worker_count) as executor:
        future_to_task = {}
        for task in tasks:
            future = executor.submit(run_one_seed, task)
            future_to_task[future] = task
            active_seeds.add(int(task["seed_index"]))
            if len(active_seeds) > worker_count:
                active_seeds.remove(min(active_seeds))
            if progress_callback:
                progress_callback(
                    {
                        "status": "submitted",
                        "seed_index": task["seed_index"],
                        "seed_count": len(seed_sets),
                        "completed": len(runs),
                        "active_seeds": sorted(active_seeds),
                        "max_workers": worker_count,
                    }
                )

        active_seeds = {
            int(task["seed_index"])
            for task in tasks[:worker_count]
        }
        if progress_callback:
            progress_callback(
                {
                    "status": "running",
                    "seed_count": len(seed_sets),
                    "completed": 0,
                    "active_seeds": sorted(active_seeds),
                    "max_workers": worker_count,
                }
            )

        pending_seed_indices = [
            int(task["seed_index"])
            for task in tasks[worker_count:]
        ]
        for future in as_completed(future_to_task):
            task = future_to_task[future]
            seed_index = int(task["seed_index"])
            run = future.result()
            active_seeds.discard(seed_index)
            if pending_seed_indices:
                active_seeds.add(pending_seed_indices.pop(0))
            runs.append(
                {
                    "seed_index": run["seed_index"],
                    "seed_set": run["seed_set"],
                    "metrics": run["metrics"],
                }
            )
            strategy_name = strategy_name or run["strategy"]
            if progress_callback:
                progress_callback(
                    {
                        "status": "completed_seed",
                        "seed_index": seed_index,
                        "seed_count": len(seed_sets),
                        "completed": len(runs),
                        "active_seeds": sorted(active_seeds),
                        "max_workers": worker_count,
                    }
                )

    runs.sort(key=lambda item: item["seed_index"])

    return {
        "result_kind": "random_run_summary",
        "result_format_version": 1,
        "generated_at": now_iso(),
        "strategy": strategy_name or strategy_spec,
        "strategy_spec": strategy_spec,
        "strategy_params_path": (
            str(strategy_params_path) if strategy_params_path is not None else None
        ),
        "seed_count": len(seed_sets),
        "average": average_metrics(runs),
        "runs": runs,
    }


def save_random_run_result(result: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(result, file, indent=2, ensure_ascii=False)
        file.write("\n")
    tmp_path.replace(path)


def summarize_random_run(result: dict[str, Any]) -> str:
    average = result.get("average", {})
    return (
        f"{result['strategy']} random run finished across {result['seed_count']} seeds: "
        f"throughput {average.get('throughput', 0):g}, "
        f"movements {average.get('movements', 0):g}, "
        f"MCT {average.get('mct_average', 0):g}, "
        f"average WIP {average.get('average_fab_wip', 0):g}."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run one fab strategy across every configured seed set."
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
    parser.add_argument("--simulation", type=Path, default=DEFAULT_SIMULATION_PATH)
    parser.add_argument("--seeds", type=Path, default=DEFAULT_SEEDS_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--workers", type=int, default=DEFAULT_MAX_WORKERS)
    args = parser.parse_args()

    result = build_random_run_result(
        strategy_spec=args.strategy,
        strategy_params_path=args.strategy_params,
        factory_path=args.factory,
        product_path=args.products,
        machine_path=args.machine,
        orders_path=args.orders,
        simulation_path=args.simulation,
        seeds_path=args.seeds,
        max_workers=args.workers,
    )
    save_random_run_result(result, args.output)

    print(summarize_random_run(result))
    print(f"Saved random-run summary to {args.output}")


if __name__ == "__main__":
    main()
