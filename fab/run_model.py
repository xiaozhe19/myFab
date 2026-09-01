"""运行新 FabModel 的命令行入口。"""

from __future__ import annotations

import argparse
import importlib
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from fab.engine import FabEngine
from fab.model import load_fab_model_from_sqlite
from strategy.fifo import FIFOStrategy
from fab.plugins import PluginManager,FabPlugin

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DATABASE = PROJECT_ROOT / "data" / "model" / "fab_model.sqlite"


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

def build_plugin(spec: str) -> FabPlugin:
    class_spec, separator, parameters_text = spec.partition("=")
    try:
        module_name, classname = class_spec.split(":", 1)
    except ValueError as error:
        raise ValueError(
            "插件格式必须是 module:class，"
            "或 module:class={JSON 参数}。"
        ) from error

    plugin_class = getattr(
        importlib.import_module(module_name),
        classname,
    )
    parameters: dict[str, object] = {}
    if separator:
        try:
            parameters = json.loads(parameters_text)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"插件 {class_spec} 的参数不是合法 JSON。"
            ) from error
        if not isinstance(parameters, dict):
            raise ValueError(
                f"插件 {class_spec} 的参数必须是 JSON 对象。"
            )

    plugin = plugin_class(**parameters)

    if not isinstance(plugin, FabPlugin):
        raise TypeError(
            f"插件 {class_spec} 必须继承 FabPlugin。"
        )

    return plugin

def build_plugin_manager(
        plugin_spec: list[str]
) -> PluginManager:
    plugins = [
        build_plugin(spec)
        for spec in plugin_spec
    ]
    return PluginManager(plugins)

def run_simulation_from_database(
    strategy_spec: str,
    model_database: Path,
    order_seed: int,
    plugin_manager:PluginManager,
    strategy_params_path: Path | None = None,
    release_interval: float | None = None,
    progress_callback: Callable[[float, float], None] | None = None,
) -> dict[str, object]:
    """从 SQLite 模型库运行任一已接入策略。"""

    model = load_fab_model_from_sqlite(model_database)
    if release_interval is not None:
        if release_interval <= 0:
            raise ValueError("release_interval 必须大于 0。")
        # 校准投料节拍时不改写原始模型库，保证每次试验的配置可追溯。
        model = replace(
            model,
            simulation=replace(
                model.simulation,
                release_interval=release_interval,
            ),
        )
    strategy = build_strategy(strategy_spec, _load_json_object(strategy_params_path))
    return FabEngine(
        model,
        order_seed=order_seed,
        progress_callback=progress_callback,
        plugin_manager=plugin_manager,
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


def _run(args: argparse.Namespace) -> None:
    plugin_manager = build_plugin_manager(args.plugin_specs)
    run_started_at = time.perf_counter()
    result = run_simulation_from_database(
    strategy_spec=args.strategy,
    model_database=args.model_database,
    order_seed=args.order_seed,
    plugin_manager=plugin_manager,
    strategy_params_path=args.strategy_params,
    release_interval=args.release_interval,
    progress_callback=_print_progress,
    )
    elapsed_seconds = time.perf_counter() - run_started_at
    print(f"{result['strategy']} finished in {elapsed_seconds:.1f}s.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the fab model from SQLite.")
    parser.add_argument("--model-database", type=Path, default=DEFAULT_MODEL_DATABASE)
    parser.add_argument("--strategy", default="FIFO")
    parser.add_argument("--strategy-params", type=Path)
    parser.add_argument("--order-seed", type=int, default=2026061700)
    parser.add_argument(
        "--release-interval",
        type=float,
        help="覆盖模型库中的投料决策间隔（minute）；用于校准，不改写模型库。",
    )
    parser.add_argument(
        "--plugin",
        dest="plugin_specs",
        action="append",
        default=[],
        metavar="MODULE:CLASS[=JSON]",
        help=(
            "启用一个插件，格式为 module:class 或 "
            "module:class={JSON 参数}；可重复指定。"
        ),
    )
    parser.add_argument(
        "--cprofile",
        action="store_true",
        help="启用 cProfile，并在运行结束后打印累计耗时统计。",
    )
    args = parser.parse_args()
    if not args.cprofile:
        _run(args)
        return

    import cProfile
    import pstats

    profiler = cProfile.Profile()
    profiler.enable()
    try:
        _run(args)
    finally:
        profiler.disable()
        pstats.Stats(profiler).sort_stats("cumulative").print_stats(50)


if __name__ == "__main__":
    main()
