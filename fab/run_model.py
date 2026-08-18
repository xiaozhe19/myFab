"""运行新 FabModel 的命令行入口。"""

from __future__ import annotations

import argparse
import importlib
import json
import time
from pathlib import Path
from typing import Callable

from fab.engine import FabEngine
from fab.metrics_sqlite import save_simulation_result
from fab.model import load_fab_model_from_sqlite
from strategy.fifo import FIFOStrategy

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DATABASE = PROJECT_ROOT / "data" / "model" / "fab_model.sqlite"
DEFAULT_RESULT_DATABASE = PROJECT_ROOT / "data" / "result" / "simulation_results.sqlite"


def _load_json_object(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError("策略参数文件必须是 JSON 对象。")
    return payload


def build_strategy(spec: str, parameters: dict[str, object] | None = None):
    """构造遵守新策略接口的原生策略。"""

    if spec == "FIFO":
        return FIFOStrategy()
    try:
        module_name, class_name = spec.split(":", 1)
    except ValueError as error:
        raise ValueError("策略格式必须是 module:class。") from error
    strategy_class = getattr(importlib.import_module(module_name), class_name)
    strategy = strategy_class(**(parameters or {}))
    if not hasattr(strategy, "decide"):
        raise TypeError(f"策略 {spec} 未实现新的 decide(state) 接口。")
    return strategy


def run_simulation_from_database(
    strategy_spec: str,
    model_database: Path,
    order_seed: int,
    strategy_params_path: Path | None = None,
    progress_callback: Callable[[float, float], None] | None = None,
) -> dict[str, object]:
    """从 SQLite 模型库运行任一已接入策略。"""

    model = load_fab_model_from_sqlite(model_database)
    strategy = build_strategy(strategy_spec, _load_json_object(strategy_params_path))
    return FabEngine(
        model,
        order_seed=order_seed,
        progress_callback=progress_callback,
    ).run(strategy)


# 优化：进度条计时起始时刻（首次调用时初始化）。
_PROGRESS_START_TIME: float | None = None


def _print_progress(current_time: float, total_time: float) -> None:
    global _PROGRESS_START_TIME
    # 优化：第一次显示进度时记录仿真墙钟起点，用于展示已运行时长。
    if _PROGRESS_START_TIME is None:
        _PROGRESS_START_TIME = time.perf_counter()
    elapsed = time.perf_counter() - _PROGRESS_START_TIME
    progress = current_time / total_time if total_time else 1.0
    width = 30
    completed = int(width * progress)
    bar = "=" * completed + ">" + " " * max(width - completed - 1, 0)
    print(
        f"\rProgress [{bar}] {progress:6.1%} "
        f"simulation time {current_time:.1f}/{total_time:.1f} min "
        f"elapsed {elapsed:6.1f}s",
        end="",
        flush=True,
    )
    if progress >= 1.0:
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the fab model from SQLite.")
    parser.add_argument("--model-database", type=Path, default=DEFAULT_MODEL_DATABASE)
    parser.add_argument("--strategy", default="FIFO")
    parser.add_argument("--strategy-params", type=Path)
    parser.add_argument("--order-seed", type=int, default=2026061700)
    parser.add_argument("--result-database", type=Path, default=DEFAULT_RESULT_DATABASE)
    args = parser.parse_args()

    # 优化：记录仿真的墙钟运行时长，供进度条与结果入库使用。
    run_started_at = time.perf_counter()
    result = run_simulation_from_database(
        args.strategy,
        args.model_database,
        args.order_seed,
        args.strategy_params,
        progress_callback=_print_progress,
    )
    elapsed_seconds = time.perf_counter() - run_started_at
    # 优化：把运行时长写入结果字典，metrics_sqlite 会一并存入 runs 表。
    result["elapsed_seconds"] = round(elapsed_seconds, 3)
    run_id = save_simulation_result(result, args.result_database)
    measurement = result["measurement"]
    print(
        f"{result['strategy']} finished: "
        f"throughput {measurement['throughput']}, "
        f"movements {measurement['movements']}, "
        f"MCT P95 {measurement['mct_p95']}, "
        f"average WIP {measurement['average_fab_wip']}, "
        f"elapsed {result['elapsed_seconds']:.1f}s."
    )
    print(f"Saved run {run_id} to {args.result_database}")


if __name__ == "__main__":
    # 优化：添加cProfile以便优化策略/内核性能
    import cProfile
    import pstats

    profiler = cProfile.Profile()
    profiler.enable()

    main()

    profiler.disable()

    stats = pstats.Stats(profiler)
    stats.sort_stats("cumulative")
    stats.print_stats(50)
