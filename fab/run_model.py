"""运行新 FabModel 的命令行入口。"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path

from fab.engine import FabEngine
from fab.model import load_fab_model
from strategy.fifo import FIFOStrategy


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_PATH = PROJECT_ROOT / "fab" / "config" / "fab_model.json"
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "result" / "fifo_v2_result.json"


def save_result(result: dict[str, object], path: Path) -> None:
    """原子写出新引擎结果，避免读取方读到半个 JSON 文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary_path.replace(path)


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


def run_simulation_from_model(
    strategy_spec: str,
    model_path: Path,
    order_seed: int,
    strategy_params_path: Path | None = None,
    diagnostic_mode: bool = False,
) -> dict[str, object]:
    """用新配置运行任一已接入策略。"""

    model = load_fab_model(model_path)
    strategy = build_strategy(strategy_spec, _load_json_object(strategy_params_path))
    return FabEngine(
        model, order_seed=order_seed, diagnostic_mode=diagnostic_mode
    ).run(strategy)


def run_fifo(model_path: Path, order_seed: int) -> dict[str, object]:
    """保留一个简短的 FIFO 便捷入口。"""

    return run_simulation_from_model("FIFO", model_path, order_seed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the v2 fab model.")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--strategy", default="FIFO")
    parser.add_argument("--strategy-params", type=Path)
    parser.add_argument("--order-seed", type=int, default=2026061700)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()

    result = run_simulation_from_model(
        args.strategy,
        args.model,
        args.order_seed,
        args.strategy_params,
    )
    save_result(result, args.output)
    measurement = result["measurement"]
    print(
        f"{result['strategy']} finished: "
        f"throughput {measurement['throughput']}, "
        f"movements {measurement['movements']}, "
        f"MCT P95 {measurement['mct_p95']}, "
        f"average WIP {measurement['average_fab_wip']}."
    )
    print(f"Saved result to {args.output}")


if __name__ == "__main__":
    main()
