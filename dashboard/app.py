from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from statistics import mean, pvariance
import threading
from typing import Any
from uuid import uuid4

from flask import Flask, jsonify, render_template, request
import json
from pathlib import Path

from fab.random_run import build_random_run_result, save_random_run_result
from fab.run_sim import run_simulation_from_files, save_result
from fab.settings import build_factory_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULT_DIR = PROJECT_ROOT / "result"
FAB_CONFIG_DIR = PROJECT_ROOT / "fab" / "config"
MACHINE_CONFIG_DIR = PROJECT_ROOT / "machine" / "config"
ORDER_CONFIG_DIR = PROJECT_ROOT / "order" / "config"
STRATEGY_CONFIG_DIR = PROJECT_ROOT / "strategy" / "config"

app = Flask(
    __name__,
    template_folder=str(Path(__file__).resolve().parent / "templates"),
    static_folder=str(Path(__file__).resolve().parent / "static"),
)

STRATEGIES: list[dict[str, Any]] = [
    {
        "id": "fifo",
        "label": "FIFO",
        "spec": "strategy.fifo:FIFOStrategy",
        "spec_aliases": ["FIFO:FIFOStrategy"],
        "params_path": None,
        "output": "fifo_result.json",
    },
    {
        "id": "batching_fifo",
        "label": "Batching FIFO",
        "spec": "strategy.batching_fifo:BatchingFIFOStrategy",
        "spec_aliases": ["BATCHING_FIFO:BatchingFIFOStrategy"],
        "params_path": None,
        "output": "batching_fifo_result.json",
    },
    {
        "id": "dbr_v1",
        "label": "DBR v1",
        "spec": "strategy.dbr_v1:DBRV1Strategy",
        "spec_aliases": ["DBR_v1:DBRV1Strategy"],
        "params_path": None,
        "output": "dbr_v1_result.json",
    },
    {
        "id": "dbr_v2",
        "label": "Dynamic DBR v2",
        "spec": "strategy.dbr_v2:DynamicDBR",
        "spec_aliases": ["DBR_v2:DynamicDBR"],
        "params_path": STRATEGY_CONFIG_DIR / "DBR_v2_config.json",
        "output": "dbr_v2_result.json",
    },
    {
        "id": "rl_v1",
        "label": "RL Release + DBR v2 Dispatch",
        "spec": "strategy.rl_v1:RLDBRMixStrategy",
        "spec_aliases": ["RL_v1:RLDBRMixStrategy"],
        "params_path": STRATEGY_CONFIG_DIR / "DBR_v2_config.json",
        "output": "rl_v1_result.json",
    },
]


def _random_summary_output(strategy: dict[str, Any]) -> Path:
    return RESULT_DIR / f"{strategy['id']}_random_run_result.json"


def _load_random_summary(strategy: dict[str, Any]) -> dict[str, Any] | None:
    preferred = _load_json_file(_random_summary_output(strategy))
    if preferred and preferred.get("result_kind") == "random_run_summary":
        return preferred

    accepted_specs = {strategy["spec"], *strategy.get("spec_aliases", [])}
    for path in RESULT_DIR.glob("*random_run_result.json"):
        payload = _load_json_file(path)
        if (
            payload
            and payload.get("result_kind") == "random_run_summary"
            and payload.get("strategy_spec") in accepted_specs
        ):
            return payload
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _average(values: list[float]) -> float:
    return mean(values) if values else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = (len(ordered) - 1) * percentile
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    weight = pos - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _sample_run() -> dict[str, Any]:
    machines = [
        {"id": "M1-A", "name": "Litho A", "type": "Litho"},
        {"id": "M1-B", "name": "Litho B", "type": "Litho"},
        {"id": "M2-A", "name": "Etch A", "type": "Etch"},
        {"id": "M3-A", "name": "Clean A", "type": "Clean"},
    ]
    wafers = [
        {"id": "W01", "release_time": 0, "due_time": 34, "route": ["Litho", "Etch", "Clean"]},
        {"id": "W02", "release_time": 1, "due_time": 36, "route": ["Litho", "Etch", "Clean"]},
        {"id": "W03", "release_time": 3, "due_time": 39, "route": ["Litho", "Etch", "Clean"]},
        {"id": "W04", "release_time": 5, "due_time": 43, "route": ["Litho", "Etch", "Clean"]},
        {"id": "W05", "release_time": 8, "due_time": 50, "route": ["Litho", "Etch", "Clean"]},
    ]
    operations = [
        {"wafer_id": "W01", "machine_id": "M1-A", "process": "Litho", "step": 0, "start": 0, "end": 7},
        {"wafer_id": "W02", "machine_id": "M1-B", "process": "Litho", "step": 0, "start": 1, "end": 9},
        {"wafer_id": "W03", "machine_id": "M1-A", "process": "Litho", "step": 0, "start": 7, "end": 15},
        {"wafer_id": "W01", "machine_id": "M2-A", "process": "Etch", "step": 1, "start": 8, "end": 19},
        {"wafer_id": "W04", "machine_id": "M1-B", "process": "Litho", "step": 0, "start": 9, "end": 18},
        {"wafer_id": "W02", "machine_id": "M2-A", "process": "Etch", "step": 1, "start": 19, "end": 30},
        {"wafer_id": "W01", "machine_id": "M3-A", "process": "Clean", "step": 2, "start": 20, "end": 25},
        {"wafer_id": "W05", "machine_id": "M1-A", "process": "Litho", "step": 0, "start": 18, "end": 26},
        {"wafer_id": "W03", "machine_id": "M2-A", "process": "Etch", "step": 1, "start": 30, "end": 42},
        {"wafer_id": "W02", "machine_id": "M3-A", "process": "Clean", "step": 2, "start": 31, "end": 37},
        {"wafer_id": "W04", "machine_id": "M2-A", "process": "Etch", "step": 1, "start": 42, "end": 55},
        {"wafer_id": "W03", "machine_id": "M3-A", "process": "Clean", "step": 2, "start": 43, "end": 49},
        {"wafer_id": "W05", "machine_id": "M2-A", "process": "Etch", "step": 1, "start": 55, "end": 66},
        {"wafer_id": "W04", "machine_id": "M3-A", "process": "Clean", "step": 2, "start": 56, "end": 63},
        {"wafer_id": "W05", "machine_id": "M3-A", "process": "Clean", "step": 2, "start": 67, "end": 73},
    ]
    queue_samples = [
        {"time": 0, "queue": "Litho", "length": 1},
        {"time": 8, "queue": "Litho", "length": 2},
        {"time": 16, "queue": "Litho", "length": 1},
        {"time": 24, "queue": "Litho", "length": 0},
        {"time": 8, "queue": "Etch", "length": 1},
        {"time": 24, "queue": "Etch", "length": 3},
        {"time": 40, "queue": "Etch", "length": 2},
        {"time": 56, "queue": "Etch", "length": 1},
        {"time": 20, "queue": "Clean", "length": 1},
        {"time": 44, "queue": "Clean", "length": 1},
        {"time": 68, "queue": "Clean", "length": 0},
    ]
    return {
        "run_id": "sample-fifo",
        "strategy": "FIFO sample",
        "time_unit": "minute",
        "generated_at": _now_iso(),
        "machines": machines,
        "wafers": wafers,
        "operations": operations,
        "queue_samples": queue_samples,
    }


def _load_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _strategy_by_id(strategy_id: str) -> dict[str, Any] | None:
    return next((strategy for strategy in STRATEGIES if strategy["id"] == strategy_id), None)


def _safe_result_name(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return stem or f"simulation_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _pretty_label(stem: str) -> str:
    text = stem.replace("_result", "").replace("_", " ")
    return " ".join(part.upper() if part.isalpha() and len(part) <= 4 else part.title() for part in text.split())


def _read_json_string_field_prefix(path: Path, field: str) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as file:
            prefix = file.read(65536)
    except OSError:
        return None
    match = re.search(rf'"{re.escape(field)}"\s*:\s*"([^"]*)"', prefix)
    return match.group(1) if match else None


def _result_file_metadata(path: Path) -> dict[str, Any]:
    stem = path.stem.replace("_result", "")
    strategy = (_read_json_string_field_prefix(path, "strategy") or "").strip()
    modified_at = datetime.fromtimestamp(
        path.stat().st_mtime,
        timezone.utc,
    ).replace(microsecond=0).isoformat()
    return {
        "id": stem,
        "label": strategy or _pretty_label(stem),
        "source": "file",
        "path": str(path.relative_to(PROJECT_ROOT)),
        "generated_at": _read_json_string_field_prefix(path, "generated_at"),
        "modified_at": modified_at,
    }


def _result_sort_key(item: dict[str, Any]) -> str:
    generated_at = str(item.get("generated_at") or "")
    if generated_at:
        return generated_at
    return str(item.get("modified_at") or "")


def _default_result_id(items: list[dict[str, Any]]) -> str:
    if not items:
        return "current"
    return max(items, key=_result_sort_key)["id"]


def _discover_result_files() -> list[dict[str, Any]]:
    file_items = [_result_file_metadata(path) for path in RESULT_DIR.glob("*_result.json")]
    return sorted(file_items, key=_result_sort_key, reverse=True)


def _load_initial_run() -> dict[str, Any]:
    for entry in _discover_result_files():
        payload = _load_json_file(PROJECT_ROOT / str(entry["path"]))
        if payload:
            return payload
    return _sample_run()


CURRENT_RUN = _load_initial_run()
DASHBOARD_CACHE: dict[str, tuple[str, dict[str, Any]]] = {}
RANDOM_SUMMARY_JOBS: dict[str, dict[str, Any]] = {}
RANDOM_SUMMARY_JOBS_LOCK = threading.Lock()


def _normalize_run(payload: dict[str, Any]) -> dict[str, Any]:
    run = deepcopy(payload)
    factory_config = build_factory_config(
        _load_json_file(FAB_CONFIG_DIR / "factory_config.json") or {},
        _load_json_file(FAB_CONFIG_DIR / "product_config.json") or {},
        _load_json_file(MACHINE_CONFIG_DIR / "machine_config.json") or {},
    )
    order_config = _load_json_file(ORDER_CONFIG_DIR / "order_config.json") or {}
    simulation_config = _load_json_file(FAB_CONFIG_DIR / "simulation_config.json") or {}
    run.setdefault("run_id", f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    run.setdefault("strategy", "unknown")
    run.setdefault("time_unit", simulation_config.get("time_unit", "time"))
    run.setdefault("generated_at", _now_iso())
    run.setdefault("factory_id", factory_config.get("factory_id"))
    run.setdefault("machines", deepcopy(factory_config.get("machines", [])))
    run.setdefault("products", deepcopy(factory_config.get("products", [])))
    run.setdefault("wafers", [])
    run.setdefault("operations", [])
    run.setdefault("machine_events", [])
    run.setdefault("queue_samples", [])
    run.setdefault("order_events", [])
    run.setdefault("release_events", [])
    run.setdefault("waiting_list", {})
    waiting = run["waiting_list"]
    arrival = order_config.get("arrival", {})
    waiting.setdefault("arrival_type", arrival.get("type", "fixed_interval"))
    waiting.setdefault("order_interval", arrival.get("interval", 0))
    waiting.setdefault("lots_per_order", order_config.get("lots_per_order", 0))
    waiting.setdefault(
        "capacity",
        order_config.get("waiting_list", {}).get("max_size", 0),
    )
    waiting.setdefault("product_mix", deepcopy(order_config.get("product_mix", {})))
    fab_parameters = run.setdefault("fab_parameters", {})
    if "release_interval" not in fab_parameters:
        fab_parameters["release_interval"] = _num(
            fab_parameters.get("release", {}).get("interval")
        )
    return run


def _load_run_by_id(result_id: str | None) -> dict[str, Any]:
    if not result_id or result_id == "current":
        return CURRENT_RUN

    for entry in _discover_result_files():
        if entry["id"] != result_id or entry["source"] != "file":
            continue
        payload = _load_json_file(PROJECT_ROOT / str(entry["path"]))
        if payload:
            detail_path = payload.get("detail_path")
            if detail_path:
                detail_payload = _load_json_file((PROJECT_ROOT / str(entry["path"])).parent / str(detail_path))
                if detail_payload:
                    return _normalize_run(detail_payload)
            return _normalize_run(payload)
    raise KeyError(result_id)


def _result_path_by_id(result_id: str) -> Path:
    for entry in _discover_result_files():
        if entry["id"] == result_id and entry["source"] == "file":
            return PROJECT_ROOT / str(entry["path"])
    raise KeyError(result_id)


def _load_or_build_random_summary(strategy: dict[str, Any]) -> dict[str, Any]:
    payload = _load_random_summary(strategy)
    if payload:
        return payload

    output_path = _random_summary_output(strategy)
    result = build_random_run_result(
        strategy_spec=strategy["spec"],
        strategy_params_path=strategy["params_path"],
        factory_path=FAB_CONFIG_DIR / "factory_config.json",
        product_path=FAB_CONFIG_DIR / "product_config.json",
        machine_path=MACHINE_CONFIG_DIR / "machine_config.json",
        orders_path=ORDER_CONFIG_DIR / "order_config.json",
        simulation_path=FAB_CONFIG_DIR / "simulation_config.json",
        seeds_path=FAB_CONFIG_DIR / "simulation_seeds.json",
    )
    save_random_run_result(result, output_path)
    return result


def _strategy_summary_card(strategy: dict[str, Any]) -> dict[str, Any]:
    summary = _load_random_summary(strategy)
    return {
        "strategy_id": strategy["id"],
        "label": strategy["label"],
        "result_id": _random_summary_output(strategy).stem.replace("_result", ""),
        "result_path": str(_random_summary_output(strategy).relative_to(PROJECT_ROOT)),
        "generated": bool(summary),
        "generated_at": summary.get("generated_at") if summary else None,
        "seed_count": summary.get("seed_count", 0) if summary else 0,
        "average": summary.get("average", {}) if summary else {},
        "runs": summary.get("runs", []) if summary else [],
    }


def _set_random_job(job_id: str, **updates: Any) -> None:
    with RANDOM_SUMMARY_JOBS_LOCK:
        job = RANDOM_SUMMARY_JOBS.setdefault(job_id, {})
        job.update(updates)
        job["updated_at"] = _now_iso()


def _get_random_job(job_id: str) -> dict[str, Any] | None:
    with RANDOM_SUMMARY_JOBS_LOCK:
        job = RANDOM_SUMMARY_JOBS.get(job_id)
        return deepcopy(job) if job else None


def _run_random_summary_job(job_id: str, strategy: dict[str, Any]) -> None:
    output_path = _random_summary_output(strategy)

    def progress(update: dict[str, Any]) -> None:
        _set_random_job(
            job_id,
            state=update.get("status", "running"),
            current_seed=update.get("seed_index"),
            seed_count=update.get("seed_count"),
            completed=update.get("completed", 0),
            active_seeds=update.get("active_seeds", []),
            max_workers=update.get("max_workers"),
            seed_set=update.get("seed_set"),
        )

    try:
        _set_random_job(
            job_id,
            state="queued",
            strategy_id=strategy["id"],
            label=strategy["label"],
            current_seed=None,
            seed_count=0,
            completed=0,
            active_seeds=[],
            max_workers=None,
            result_path=None,
            error=None,
        )
        result = build_random_run_result(
            strategy_spec=strategy["spec"],
            strategy_params_path=strategy["params_path"],
            factory_path=FAB_CONFIG_DIR / "factory_config.json",
            product_path=FAB_CONFIG_DIR / "product_config.json",
            machine_path=MACHINE_CONFIG_DIR / "machine_config.json",
            orders_path=ORDER_CONFIG_DIR / "order_config.json",
            simulation_path=FAB_CONFIG_DIR / "simulation_config.json",
            seeds_path=FAB_CONFIG_DIR / "simulation_seeds.json",
            progress_callback=progress,
        )
        save_random_run_result(result, output_path)
        _set_random_job(
            job_id,
            state="done",
            current_seed=None,
            seed_count=result.get("seed_count", 0),
            completed=result.get("seed_count", 0),
            active_seeds=[],
            result_path=str(output_path.relative_to(PROJECT_ROOT)),
            strategy=_strategy_summary_card(strategy),
        )
    except Exception as error:
        _set_random_job(job_id, state="error", error=str(error))


def _dashboard_cache_token(result_id: str | None) -> str:
    if not result_id or result_id == "current":
        return f"memory:{CURRENT_RUN.get('run_id')}:{CURRENT_RUN.get('generated_at')}"

    for entry in _discover_result_files():
        if entry["id"] == result_id and entry["source"] == "file":
            return f"file:{entry['path']}:{entry.get('modified_at')}"
    raise KeyError(result_id)


def _compute_dashboard_by_id(result_id: str | None) -> dict[str, Any]:
    cache_id = result_id or "current"
    token = _dashboard_cache_token(result_id)
    cached = DASHBOARD_CACHE.get(cache_id)
    if cached and cached[0] == token:
        return cached[1]

    payload = _load_run_by_id(result_id)
    dashboard = (
        compute_random_run_dashboard(payload)
        if payload.get("result_kind") == "random_run_summary"
        else compute_dashboard(payload)
    )
    DASHBOARD_CACHE[cache_id] = (token, dashboard)
    return dashboard


def compute_random_run_dashboard(summary: dict[str, Any]) -> dict[str, Any]:
    summary = deepcopy(summary)
    factory_config = build_factory_config(
        _load_json_file(FAB_CONFIG_DIR / "factory_config.json") or {},
        _load_json_file(FAB_CONFIG_DIR / "product_config.json") or {},
        _load_json_file(MACHINE_CONFIG_DIR / "machine_config.json") or {},
    )
    order_config = _load_json_file(ORDER_CONFIG_DIR / "order_config.json") or {}
    simulation_config = _load_json_file(FAB_CONFIG_DIR / "simulation_config.json") or {}
    average = summary.get("average", {})
    runs = summary.get("runs", [])
    horizon = _num(average.get("measurement_horizon"))
    released = _num(average.get("released"))
    throughput = _num(average.get("throughput"))
    movements = _num(average.get("movements"))
    process_time = _num(average.get("total_process_time"))
    setup_time = _num(average.get("total_setup_time"))
    products = factory_config.get("products", [])
    product_mix = order_config.get("product_mix", {})
    product_performance = []
    for product in products:
        product_id = product.get("id")
        product_performance.append(
            {
                "id": product_id,
                "name": product.get("name", product_id),
                "mix_weight": _num(product_mix.get(product_id), 1),
                "target_share": 0,
                "released": 0,
                "release_share": 0,
                "completed": 0,
                "mct_average": 0,
            }
        )
    total_weight = sum(item["mix_weight"] for item in product_performance)
    if total_weight > 0:
        for item in product_performance:
            item["target_share"] = item["mix_weight"] / total_weight

    return {
        "run": {
            "run_id": summary.get("strategy_spec", summary.get("strategy", "random-run")),
            "strategy": f"{summary.get('strategy', 'Strategy')} average",
            "time_unit": simulation_config.get("time_unit", "minute"),
            "generated_at": summary.get("generated_at", _now_iso()),
            "horizon": round(horizon, 3),
            "window_start": _num(average.get("measurement_start_time")),
            "window_end": _num(average.get("measurement_end_time")),
            "warmup_time": _num(average.get("warmup_time")),
            "wafer_count": 0,
            "completed_count": round(throughput, 3),
            "factory_id": factory_config.get("factory_id", "-"),
        },
        "structure": {
            "factory": {
                "machines": len(factory_config.get("machines", [])),
                "stations": len({machine.get("type") for machine in factory_config.get("machines", [])}),
                "products": len(products),
            },
            "orders": {
                "arrival_type": order_config.get("arrival", {}).get("type", "unknown"),
                "interval": _num(order_config.get("arrival", {}).get("interval")),
                "lots_per_order": int(_num(order_config.get("lots_per_order"))),
                "orders_generated": 0,
                "lots_generated": 0,
                "final_backlog": 0,
                "capacity": int(_num(order_config.get("waiting_list", {}).get("max_size"))),
            },
            "policy": {
                "release_interval": 0,
            },
        },
        "business": {
            "mct": {
                "average": _num(average.get("mct_average")),
                "p50": 0,
                "p90": 0,
                "max": 0,
            },
            "movement": {
                "count": round(movements, 3),
                "rate": round(movements / horizon, 4) if horizon > 0 else 0.0,
            },
            "throughput": {
                "completed": round(throughput, 3),
                "rate": round(throughput / horizon, 4) if horizon > 0 else 0.0,
                "completion_ratio": round(throughput / released, 4) if released > 0 else 0.0,
            },
            "wip": {
                "average": _num(average.get("average_fab_wip")),
            },
            "release": {
                "count": round(released, 3),
                "rate": round(released / horizon, 4) if horizon > 0 else 0.0,
                "waiting_capacity": int(_num(order_config.get("waiting_list", {}).get("max_size"))),
                "waiting_backlog": 0,
            },
            "efficiency": {
                "process_time": round(process_time, 3),
                "setup_time": round(setup_time, 3),
                "setup_to_process_ratio": (
                    round(setup_time / process_time, 4)
                    if process_time > 0
                    else 0.0
                ),
            },
            "products": product_performance,
        },
        "health": {
            "machine_utilization": {
                "average": round(_num(average.get("machine_utilization_average")), 4),
                "variance": 0,
                "min": round(_num(average.get("machine_utilization_min")), 4),
                "max": round(_num(average.get("machine_utilization_max")), 4),
                "machines": [],
            },
            "queue_length": [],
            "wait_by_process": [],
            "bottlenecks": [],
        },
        "timeline": {
            "machines": [
                {
                    "id": machine.get("id"),
                    "name": machine.get("name", machine.get("id")),
                    "type": machine.get("type", "Unknown"),
                    "operations": [],
                }
                for machine in factory_config.get("machines", [])
            ]
        },
        "random_run": {
            "strategy": summary.get("strategy"),
            "strategy_spec": summary.get("strategy_spec"),
            "strategy_params_path": summary.get("strategy_params_path"),
            "seed_count": summary.get("seed_count", len(runs)),
            "average": average,
            "runs": runs,
        },
        "raw": {
            "products": products,
            "wafers": [],
            "operations": [],
            "machine_events": [],
            "queue_samples": [],
            "order_events": [],
            "release_events": [],
            "waiting_list": {},
        },
    }


def compute_dashboard(run: dict[str, Any]) -> dict[str, Any]:
    run = _normalize_run(run)
    wafers = {str(w["id"]): dict(w) for w in run["wafers"] if "id" in w}
    machines = {str(m["id"]): dict(m) for m in run["machines"] if "id" in m}
    measurement = run.get("measurement", {})
    measurement_horizon = _num(measurement.get("measurement_horizon"))
    min_release = min((_num(w.get("release_time")) for w in wafers.values()), default=0.0)
    raw_max_end = max(
        [_num(op.get("end")) for op in run["operations"]]
        + [_num(event.get("end")) for event in run.get("machine_events", [])],
        default=0.0,
    )
    measurement_start = _num(
        measurement.get("measurement_start_time"),
        _num(run.get("measurement_start_time"), min_release),
    )
    horizon = measurement_horizon or max(
        _num(run.get("current_time"), raw_max_end) - measurement_start,
        0.0,
    )
    if horizon == 0 and raw_max_end > measurement_start:
        horizon = raw_max_end - measurement_start
    measurement_end = _num(
        measurement.get("measurement_end_time"),
        measurement_start + horizon,
    )

    def overlaps_window(event: dict[str, Any]) -> bool:
        start = _num(event.get("start"))
        end = _num(event.get("end"), start)
        return end > measurement_start and start < measurement_end

    def clip_event(event: dict[str, Any]) -> dict[str, Any]:
        clipped = dict(event)
        start = max(_num(event.get("start")), measurement_start)
        end = min(_num(event.get("end"), start), measurement_end)
        clipped["start"] = round(start, 3)
        clipped["end"] = round(end, 3)
        clipped["duration"] = round(max(end - start, 0.0), 3)
        return clipped

    operations = sorted(
        [clip_event(op) for op in run["operations"] if overlaps_window(op)],
        key=lambda op: (_num(op.get("start")), _num(op.get("end"))),
    )
    machine_events = sorted(
        [
            clip_event(event)
            for event in run.get("machine_events", [])
            if overlaps_window(event)
        ],
        key=lambda event: (_num(event.get("start")), _num(event.get("end"))),
    )

    machine_processing: dict[str, float] = {machine_id: 0.0 for machine_id in machines}
    machine_setup: dict[str, float] = {machine_id: 0.0 for machine_id in machines}
    machine_down: dict[str, float] = {machine_id: 0.0 for machine_id in machines}
    machine_ops: dict[str, list[dict[str, Any]]] = {machine_id: [] for machine_id in machines}
    wafer_ops: dict[str, list[dict[str, Any]]] = {wafer_id: [] for wafer_id in wafers}
    process_busy: dict[str, float] = {}
    waits_by_process: dict[str, list[float]] = {}

    for op in operations:
        machine_id = str(op.get("machine_id", ""))
        wafer_id = str(op.get("wafer_id", ""))
        process_name = str(op.get("process") or machines.get(machine_id, {}).get("type") or "Unknown")
        start = _num(op.get("start"))
        end = _num(op.get("end"), start)
        duration = max(end - start, 0.0)
        op["duration"] = duration
        op["process"] = process_name
        op["kind"] = op.get("kind", "process")

        if op["kind"] == "process":
            machine_processing[machine_id] = machine_processing.get(machine_id, 0.0) + duration
            process_busy[process_name] = process_busy.get(process_name, 0.0) + duration
            wafer_ops.setdefault(wafer_id, []).append(op)
        elif op["kind"] == "setup":
            machine_setup[machine_id] = machine_setup.get(machine_id, 0.0) + duration
        machine_ops.setdefault(machine_id, []).append(op)

    for event in machine_events:
        machine_id = str(event.get("machine_id", ""))
        start = _num(event.get("start"))
        end = _num(event.get("end"), start)
        duration = max(end - start, 0.0)
        kind = str(event.get("kind", "event"))
        event["duration"] = duration
        if kind == "downtime":
            machine_down[machine_id] = machine_down.get(machine_id, 0.0) + duration
        machine_ops.setdefault(machine_id, []).append(event)

    completed_wafers = []
    cycle_times = []

    for wafer_id, wafer in wafers.items():
        ops = sorted(wafer_ops.get(wafer_id, []), key=lambda op: _num(op.get("start")))
        if ops:
            wafer.setdefault("start_time", ops[0]["start"])
        explicit_end_time = wafer.get("end_time")
        route_len = len(wafer.get("route") or [])
        has_explicit_completed = "completed" in wafer
        completed = bool(wafer.get("completed", False))
        if not has_explicit_completed and route_len and len(ops) >= route_len:
            completed = True
        if not has_explicit_completed and explicit_end_time is not None:
            completed = True
        if completed and ops and wafer.get("end_time") is None:
            wafer["end_time"] = ops[-1]["end"]

        last_ready = _num(wafer.get("release_time"))
        index = 0
        while index < len(ops):
            op = ops[index]
            process_name = str(op.get("process", "Unknown"))
            step_key = op.get("step")
            segment_start = _num(op.get("start"))
            segment_end = _num(op.get("end"), segment_start)
            group_end = segment_end
            group_last = index
            probe = index + 1
            while probe < len(ops) and ops[probe].get("step") == step_key and str(ops[probe].get("process", "Unknown")) == process_name:
                group_end = max(group_end, _num(ops[probe].get("end"), group_end))
                group_last = probe
                probe += 1

            wait = max(segment_start - last_ready, 0.0)
            waits_by_process.setdefault(process_name, []).append(wait)
            last_ready = group_end
            index = group_last + 1

        if completed and wafer.get("end_time") is not None:
            release = _num(wafer.get("release_time"))
            end = _num(wafer.get("end_time"))
            cycle = max(end - release, 0.0)
            cycle_times.append(cycle)
            completed_wafers.append({**wafer, "cycle_time": cycle})

    utilization_rows = []
    for machine_id, machine in machines.items():
        processing = machine_processing.get(machine_id, 0.0)
        setup = machine_setup.get(machine_id, 0.0)
        downtime = machine_down.get(machine_id, 0.0)
        busy = processing
        utilization = busy / horizon if horizon > 0 else 0.0
        utilization_rows.append(
            {
                "id": machine_id,
                "name": machine.get("name", machine_id),
                "type": machine.get("type", "Unknown"),
                "busy_time": round(busy, 3),
                "processing_time": round(processing, 3),
                "setup_time": round(setup, 3),
                "downtime": round(downtime, 3),
                "utilization": round(utilization, 4),
                "downtime_ratio": round(downtime / horizon, 4) if horizon > 0 else 0,
                "operation_count": len([op for op in machine_ops.get(machine_id, []) if op.get("kind") == "process"]),
            }
        )

    operation_summary = run.get("operation_summary", {})
    if operation_summary.get("machine_utilization"):
        utilization_rows = [
            dict(row)
            for row in operation_summary["machine_utilization"].get("machines", [])
        ]
    utilization_values = [row["utilization"] for row in utilization_rows]
    queue_groups: dict[str, list[float]] = {}
    queue_samples = [
        dict(sample)
        for sample in run.get("queue_samples", [])
        if measurement_start <= _num(sample.get("time")) <= measurement_end
    ]
    for sample in queue_samples:
        queue_groups.setdefault(str(sample.get("queue", "Unknown")), []).append(_num(sample.get("length")))

    queue_rows = [
        {
            "queue": queue,
            "average_length": round(_average(values), 3),
            "max_length": max(values) if values else 0,
            "sample_count": len(values),
        }
        for queue, values in queue_groups.items()
    ]
    if run.get("queue_summary"):
        queue_rows = [dict(row) for row in run.get("queue_summary", [])]

    wait_rows = [
        {
            "process": process,
            "average_wait": round(_average(values), 3),
            "max_wait": round(max(values), 3) if values else 0,
        }
        for process, values in waits_by_process.items()
    ]
    if run.get("wait_by_process_summary"):
        wait_rows = [dict(row) for row in run.get("wait_by_process_summary", [])]

    bottlenecks = [dict(row) for row in run.get("bottleneck_summary", [])]
    if not bottlenecks:
        bottlenecks = []
        for row in utilization_rows:
            bottlenecks.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "kind": "machine",
                    "score": round(row["utilization"], 4),
                    "reason": f"utilization {row['utilization'] * 100:.1f}%",
                }
            )
        for row in queue_rows:
            bottlenecks.append(
                {
                    "id": row["queue"],
                    "name": row["queue"],
                    "kind": "queue",
                    "score": round(row["average_length"] + row["max_length"], 4),
                    "reason": f"avg queue {row['average_length']}, max {row['max_length']}",
                }
            )
        for row in wait_rows:
            bottlenecks.append(
                {
                    "id": row["process"],
                    "name": row["process"],
                    "kind": "process",
                    "score": round(row["average_wait"] + row["max_wait"], 4),
                    "reason": f"avg wait {row['average_wait']}, max {row['max_wait']}",
                }
            )
        bottlenecks = sorted(bottlenecks, key=lambda item: item["score"], reverse=True)[:8]
    released_count = int(_num(measurement.get("released")))
    movement_count = int(_num(measurement.get("movements")))
    average_fab_wip = _num(measurement.get("average_fab_wip"))
    throughput_count = int(
        _num(measurement.get("throughput"), len(completed_wafers))
    )
    throughput_rate = throughput_count / horizon if horizon > 0 else 0.0
    operation_totals = operation_summary.get("totals", {})
    total_processing_time = _num(
        operation_totals.get("process_time"),
        sum(machine_processing.values()),
    )
    total_setup_time = _num(
        operation_totals.get("setup_time"),
        sum(machine_setup.values()),
    )

    products = {
        str(product.get("id")): dict(product)
        for product in run.get("products", [])
        if product.get("id") is not None
    }
    release_events = [
        event
        for event in run.get("release_events", [])
        if measurement_start
        <= _num(event.get("release_time"))
        < measurement_end
    ]
    released_by_product: dict[str, int] = {}
    for event in release_events:
        product_id = str(event.get("product_id", "Unknown"))
        released_by_product[product_id] = released_by_product.get(product_id, 0) + 1

    completed_by_product: dict[str, list[float]] = {}
    for wafer in completed_wafers:
        product_id = str(wafer.get("product_id", "Unknown"))
        completed_by_product.setdefault(product_id, []).append(
            _num(wafer.get("cycle_time"))
        )

    product_ids = list(
        dict.fromkeys(
            [*products.keys(), *released_by_product.keys(), *completed_by_product.keys()]
        )
    )
    product_mix = run.get("waiting_list", {}).get("product_mix", {})
    if not product_mix and product_ids:
        product_mix = {product_id: 1.0 for product_id in product_ids}
    total_weight = sum(
        max(_num(product_mix.get(product_id)), 0.0)
        for product_id in product_ids
    )
    product_performance = []
    for product_id in product_ids:
        product = products.get(product_id, {})
        weight = max(_num(product_mix.get(product_id)), 0.0)
        product_cycles = completed_by_product.get(product_id, [])
        product_performance.append(
            {
                "id": product_id,
                "name": product.get("name", product_id),
                "mix_weight": weight,
                "target_share": weight / total_weight if total_weight > 0 else 0.0,
                "released": released_by_product.get(product_id, 0),
                "release_share": (
                    released_by_product.get(product_id, 0) / released_count
                    if released_count > 0
                    else 0.0
                ),
                "completed": len(product_cycles),
                "mct_average": round(_average(product_cycles), 3),
            }
        )

    waiting_list = run.get("waiting_list", {})
    accepted_orders = [
        event
        for event in run.get("order_events", [])
        if event.get("accepted") and measurement_start <= _num(event.get("time")) <= measurement_end
    ]
    station_count = len(
        {
            str(machine.get("type", "Unknown"))
            for machine in machines.values()
        }
    )

    display_raw = deepcopy(run)
    display_raw["operations"] = operations
    display_raw["machine_events"] = machine_events
    display_raw["queue_samples"] = queue_samples
    display_raw["order_events"] = [
        dict(event)
        for event in run.get("order_events", [])
        if measurement_start <= _num(event.get("time")) <= measurement_end
    ]
    display_raw["release_events"] = [
        dict(event)
        for event in run.get("release_events", [])
        if measurement_start <= _num(event.get("release_time")) <= measurement_end
    ]
    display_raw["buffer_samples"] = [
        dict(sample)
        for sample in run.get("buffer_samples", [])
        if measurement_start <= _num(sample.get("time")) <= measurement_end
    ]

    return {
        "run": {
            "run_id": run["run_id"],
            "strategy": run["strategy"],
            "time_unit": run["time_unit"],
            "generated_at": run["generated_at"],
            "horizon": round(horizon, 3),
            "window_start": round(measurement_start, 3),
            "window_end": round(measurement_end, 3),
            "warmup_time": _num(measurement.get("warmup_time")),
            "wafer_count": len(wafers),
            "completed_count": len(completed_wafers),
            "factory_id": run.get("factory_id", "-"),
        },
        "structure": {
            "factory": {
                "machines": len(machines),
                "stations": station_count,
                "products": len(products),
            },
            "orders": {
                "arrival_type": waiting_list.get("arrival_type", "unknown"),
                "interval": _num(waiting_list.get("order_interval")),
                "lots_per_order": int(_num(waiting_list.get("lots_per_order"))),
                "orders_generated": int(
                    _num(waiting_list.get("orders_generated"), len(accepted_orders))
                ),
                "lots_generated": int(
                    _num(waiting_list.get("generated_count"))
                ),
                "final_backlog": len(waiting_list.get("final_snapshot", [])),
                "capacity": int(_num(waiting_list.get("capacity"))),
            },
            "policy": {
                "release_interval": _num(
                    run.get("fab_parameters", {}).get("release_interval")
                ),
            },
        },
        "business": {
            "mct": {
                "average": round(_average(cycle_times), 3),
                "p50": round(_percentile(cycle_times, 0.5), 3),
                "p90": round(_percentile(cycle_times, 0.9), 3),
                "max": round(max(cycle_times), 3) if cycle_times else 0,
            },
            "movement": {
                "count": movement_count,
                "rate": round(movement_count / horizon, 4) if horizon > 0 else 0.0,
            },
            "throughput": {
                "completed": throughput_count,
                "rate": round(throughput_rate, 4),
                "completion_ratio": (
                    round(throughput_count / released_count, 4)
                    if released_count > 0
                    else 0.0
                ),
            },
            "wip": {
                "average": round(average_fab_wip, 3),
            },
            "release": {
                "count": released_count,
                "rate": round(released_count / horizon, 4) if horizon > 0 else 0.0,
                "waiting_capacity": int(
                    _num(run.get("waiting_list", {}).get("capacity"))
                ),
                "waiting_backlog": len(
                    run.get("waiting_list", {}).get("final_snapshot", [])
                ),
            },
            "efficiency": {
                "process_time": round(total_processing_time, 3),
                "setup_time": round(total_setup_time, 3),
                "setup_to_process_ratio": (
                    round(total_setup_time / total_processing_time, 4)
                    if total_processing_time > 0
                    else 0.0
                ),
            },
            "products": product_performance,
        },
        "health": {
            "machine_utilization": {
                "average": round(_average(utilization_values), 4),
                "variance": round(pvariance(utilization_values), 6) if utilization_values else 0,
                "min": round(min(utilization_values), 4) if utilization_values else 0,
                "max": round(max(utilization_values), 4) if utilization_values else 0,
                "machines": utilization_rows,
            },
            "queue_length": queue_rows,
            "wait_by_process": wait_rows,
            "bottlenecks": bottlenecks,
        },
        "timeline": {
            "machines": [
                {
                    **machines.get(machine_id, {"id": machine_id, "name": machine_id, "type": "Unknown"}),
                    "operations": sorted(machine_ops.get(machine_id, []), key=lambda op: _num(op.get("start"))),
                }
                for machine_id in sorted(machine_ops)
            ]
        },
        "raw": display_raw,
    }


@app.get("/")
def dashboard_page():
    return render_template("fab_dashboard.html")


@app.get("/details/<result_id>")
def detail_dashboard_page(result_id: str):
    return render_template(
        "fab_dashboard.html",
        initial_result_id=result_id,
        embedded=bool(request.args.get("embedded")),
    )


@app.get("/seed-detail/<strategy_id>/<int:seed_index>")
def seed_detail_page(strategy_id: str, seed_index: int):
    return render_template(
        "seed_detail.html",
        strategy_id=strategy_id,
        seed_index=seed_index,
    )


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "fab-dashboard", "time": _now_iso()})


@app.get("/api/dashboard")
def dashboard_data():
    result_id = request.args.get("result")
    try:
        return jsonify(_compute_dashboard_by_id(result_id))
    except KeyError:
        return jsonify({"error": f"Unknown result id: {result_id}"}), 404


@app.get("/api/sample-run")
def sample_run():
    return jsonify(_sample_run())


@app.get("/api/results")
def list_results():
    results = _discover_result_files()
    valid_ids = {item["id"] for item in results}
    selected = request.args.get("result") or _default_result_id(results)
    return jsonify(
        {
            "selected": selected if selected in valid_ids else _default_result_id(results),
            "results": results,
        }
    )


@app.get("/api/strategies")
def list_strategies():
    return jsonify(
        {
            "strategies": [
                {
                    "id": strategy["id"],
                    "label": strategy["label"],
                    "has_params": strategy["params_path"] is not None,
                }
                for strategy in STRATEGIES
            ]
        }
    )


@app.get("/api/strategy-random-summaries")
def strategy_random_summaries():
    try:
        summaries = [_strategy_summary_card(strategy) for strategy in STRATEGIES]
    except Exception as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"strategies": summaries})


@app.post("/api/strategies/<strategy_id>/random-summary")
def build_strategy_random_summary(strategy_id: str):
    strategy = _strategy_by_id(strategy_id)
    if strategy is None:
        return jsonify({"error": f"Unknown strategy id: {strategy_id}"}), 404
    try:
        summary = _load_or_build_random_summary(strategy)
    except Exception as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"ok": True, "strategy": _strategy_summary_card(strategy), "summary": summary})


@app.post("/api/strategies/<strategy_id>/random-summary-job")
def start_strategy_random_summary_job(strategy_id: str):
    strategy = _strategy_by_id(strategy_id)
    if strategy is None:
        return jsonify({"error": f"Unknown strategy id: {strategy_id}"}), 404
    job_id = uuid4().hex
    _set_random_job(
        job_id,
        state="starting",
        strategy_id=strategy["id"],
        label=strategy["label"],
        current_seed=None,
        seed_count=0,
        completed=0,
        result_path=None,
        error=None,
    )
    thread = threading.Thread(
        target=_run_random_summary_job,
        args=(job_id, strategy),
        daemon=True,
    )
    thread.start()
    return jsonify({"ok": True, "job_id": job_id, "job": _get_random_job(job_id)})


@app.get("/api/random-summary-jobs/<job_id>")
def get_random_summary_job(job_id: str):
    job = _get_random_job(job_id)
    if job is None:
        return jsonify({"error": f"Unknown job id: {job_id}"}), 404
    return jsonify({"job": job})


@app.get("/api/results/<result_id>")
def get_result(result_id: str):
    try:
        run = _load_run_by_id(result_id)
    except KeyError:
        return jsonify({"error": f"Unknown result id: {result_id}"}), 404
    return jsonify(run)


@app.post("/api/runs")
def update_run():
    global CURRENT_RUN
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    CURRENT_RUN = _normalize_run(payload)
    dashboard = compute_dashboard(CURRENT_RUN)
    DASHBOARD_CACHE["current"] = (
        _dashboard_cache_token("current"),
        dashboard,
    )
    return jsonify({"ok": True, "dashboard": dashboard})


@app.post("/api/simulations")
def run_strategy_simulation():
    global CURRENT_RUN
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    strategy_id = str(payload.get("strategy_id") or "fifo")
    strategy = _strategy_by_id(strategy_id)
    if strategy is None:
        return jsonify({"error": f"Unknown strategy id: {strategy_id}"}), 400

    try:
        seed_index = int(payload.get("seed_index") or 1)
    except (TypeError, ValueError):
        return jsonify({"error": "seed_index must be an integer."}), 400

    output_name = str(payload.get("output_name") or strategy["output"])
    if not output_name.endswith(".json"):
        output_name = f"{output_name}.json"
    output_path = RESULT_DIR / _safe_result_name(output_name)
    RESULT_DIR.mkdir(exist_ok=True)

    try:
        result = run_simulation_from_files(
            strategy_spec=strategy["spec"],
            strategy_params_path=strategy["params_path"],
            factory_path=FAB_CONFIG_DIR / "factory_config.json",
            product_path=FAB_CONFIG_DIR / "product_config.json",
            machine_path=MACHINE_CONFIG_DIR / "machine_config.json",
            orders_path=ORDER_CONFIG_DIR / "order_config.json",
            simulation_path=FAB_CONFIG_DIR / "simulation_config.json",
            seeds_path=FAB_CONFIG_DIR / "simulation_seeds.json",
            seed_index=seed_index,
        )
        save_result(result, output_path)
    except Exception as error:
        return jsonify({"error": str(error)}), 400

    CURRENT_RUN = _normalize_run(result)
    result_id = output_path.stem.replace("_result", "")
    dashboard = compute_dashboard(CURRENT_RUN)
    DASHBOARD_CACHE[result_id] = (
        _dashboard_cache_token(result_id),
        dashboard,
    )
    return jsonify(
        {
            "ok": True,
            "result_id": result_id,
            "result_path": str(output_path.relative_to(PROJECT_ROOT)),
            "dashboard": dashboard,
            "results": _discover_result_files(),
        }
    )


@app.post("/api/random-runs")
def run_random_summary():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    strategy_id = str(payload.get("strategy_id") or "fifo")
    strategy = _strategy_by_id(strategy_id)
    if strategy is None:
        return jsonify({"error": f"Unknown strategy id: {strategy_id}"}), 400

    output_name = str(payload.get("output_name") or f"{strategy_id}_random_run_result.json")
    if not output_name.endswith(".json"):
        output_name = f"{output_name}.json"
    output_path = RESULT_DIR / _safe_result_name(output_name)
    RESULT_DIR.mkdir(exist_ok=True)

    try:
        result = build_random_run_result(
            strategy_spec=strategy["spec"],
            strategy_params_path=strategy["params_path"],
            factory_path=FAB_CONFIG_DIR / "factory_config.json",
            product_path=FAB_CONFIG_DIR / "product_config.json",
            machine_path=MACHINE_CONFIG_DIR / "machine_config.json",
            orders_path=ORDER_CONFIG_DIR / "order_config.json",
            simulation_path=FAB_CONFIG_DIR / "simulation_config.json",
            seeds_path=FAB_CONFIG_DIR / "simulation_seeds.json",
        )
        save_random_run_result(result, output_path)
    except Exception as error:
        return jsonify({"error": str(error)}), 400

    result_id = output_path.stem.replace("_result", "")
    dashboard = compute_random_run_dashboard(result)
    DASHBOARD_CACHE[result_id] = (
        _dashboard_cache_token(result_id),
        dashboard,
    )
    return jsonify(
        {
            "ok": True,
            "result_id": result_id,
            "result_path": str(output_path.relative_to(PROJECT_ROOT)),
            "dashboard": dashboard,
            "results": _discover_result_files(),
        }
    )


@app.post("/api/random-runs/<result_id>/seeds/<int:seed_index>/detail")
def run_random_seed_detail(result_id: str, seed_index: int):
    try:
        summary_path = _result_path_by_id(result_id)
        summary = _load_json_file(summary_path)
    except KeyError:
        return jsonify({"error": f"Unknown result id: {result_id}"}), 404
    if not summary or summary.get("result_kind") != "random_run_summary":
        return jsonify({"error": f"Result {result_id} is not a random-run summary."}), 400
    if seed_index < 1 or seed_index > int(_num(summary.get("seed_count"))):
        return jsonify({"error": f"seed_index must be between 1 and {summary.get('seed_count')}."}), 400

    strategy_spec = str(summary.get("strategy_spec") or "")
    if not strategy_spec:
        return jsonify({"error": "Random-run summary does not contain strategy_spec."}), 400
    params_value = summary.get("strategy_params_path")
    params_path = Path(params_value) if params_value else None
    if params_path is not None and not params_path.is_absolute():
        params_path = PROJECT_ROOT / params_path

    output_path = RESULT_DIR / f"{result_id}_seed_{seed_index:02d}_result.json"
    try:
        result = run_simulation_from_files(
            strategy_spec=strategy_spec,
            strategy_params_path=params_path,
            factory_path=FAB_CONFIG_DIR / "factory_config.json",
            product_path=FAB_CONFIG_DIR / "product_config.json",
            machine_path=MACHINE_CONFIG_DIR / "machine_config.json",
            orders_path=ORDER_CONFIG_DIR / "order_config.json",
            simulation_path=FAB_CONFIG_DIR / "simulation_config.json",
            seeds_path=FAB_CONFIG_DIR / "simulation_seeds.json",
            seed_index=seed_index,
        )
        save_result(result, output_path)
    except Exception as error:
        return jsonify({"error": str(error)}), 400

    detail_id = output_path.stem.replace("_result", "")
    dashboard = compute_dashboard(_normalize_run(result))
    DASHBOARD_CACHE[detail_id] = (
        _dashboard_cache_token(detail_id),
        dashboard,
    )
    return jsonify(
        {
            "ok": True,
            "result_id": detail_id,
            "result_path": str(output_path.relative_to(PROJECT_ROOT)),
            "dashboard": dashboard,
            "results": _discover_result_files(),
        }
    )


@app.post("/api/strategies/<strategy_id>/seeds/<int:seed_index>/detail")
def run_strategy_seed_detail(strategy_id: str, seed_index: int):
    strategy = _strategy_by_id(strategy_id)
    if strategy is None:
        return jsonify({"error": f"Unknown strategy id: {strategy_id}"}), 404

    try:
        summary = _load_or_build_random_summary(strategy)
    except Exception as error:
        return jsonify({"error": str(error)}), 400
    if seed_index < 1 or seed_index > int(_num(summary.get("seed_count"))):
        return jsonify({"error": f"seed_index must be between 1 and {summary.get('seed_count')}."}), 400

    output_path = RESULT_DIR / f"{strategy_id}_seed_{seed_index:02d}_result.json"
    try:
        result = run_simulation_from_files(
            strategy_spec=strategy["spec"],
            strategy_params_path=strategy["params_path"],
            factory_path=FAB_CONFIG_DIR / "factory_config.json",
            product_path=FAB_CONFIG_DIR / "product_config.json",
            machine_path=MACHINE_CONFIG_DIR / "machine_config.json",
            orders_path=ORDER_CONFIG_DIR / "order_config.json",
            simulation_path=FAB_CONFIG_DIR / "simulation_config.json",
            seeds_path=FAB_CONFIG_DIR / "simulation_seeds.json",
            seed_index=seed_index,
        )
        save_result(result, output_path)
    except Exception as error:
        return jsonify({"error": str(error)}), 400

    detail_id = output_path.stem.replace("_result", "")
    dashboard = compute_dashboard(_normalize_run(result))
    DASHBOARD_CACHE[detail_id] = (
        _dashboard_cache_token(detail_id),
        dashboard,
    )
    return jsonify(
        {
            "ok": True,
            "result_id": detail_id,
            "result_path": str(output_path.relative_to(PROJECT_ROOT)),
            "dashboard": dashboard,
        }
    )


@app.get("/api/strategies/<strategy_id>/seeds/<int:seed_index>/summary")
def get_strategy_seed_summary(strategy_id: str, seed_index: int):
    strategy = _strategy_by_id(strategy_id)
    if strategy is None:
        return jsonify({"error": f"Unknown strategy id: {strategy_id}"}), 404
    summary = _load_random_summary(strategy)
    if not summary:
        return jsonify({"error": f"{strategy['label']} has no random-run summary yet."}), 404
    seed_runs = summary.get("runs", [])
    run = next(
        (item for item in seed_runs if int(_num(item.get("seed_index"))) == seed_index),
        None,
    )
    if run is None:
        return jsonify({"error": f"Seed {seed_index} was not found."}), 404
    return jsonify(
        {
            "strategy_id": strategy_id,
            "strategy_label": strategy["label"],
            "seed_index": seed_index,
            "seed_count": summary.get("seed_count", len(seed_runs)),
            "seed_set": run.get("seed_set", {}),
            "metrics": run.get("metrics", {}),
        }
    )


@app.get("/api/schema")
def api_schema():
    return jsonify(
        {
            "result_package": {
                "overview": "Lightweight *_result.json for dashboard loading.",
                "detail": "Full *_detail.json for manual debug and AI analysis.",
            },
            "required_top_level": ["machines", "wafers", "measurement"],
            "optional_top_level": [
                "run_id",
                "strategy",
                "time_unit",
                "generated_at",
                "current_time",
                "operations",
                "machine_events",
                "queue_samples",
                "operation_summary",
                "queue_summary",
                "wait_by_process_summary",
                "bottleneck_summary",
            ],
            "machine": {"id": "M1-A", "name": "Litho A", "type": "Litho"},
            "wafer": {
                "id": "W01",
                "release_time": 0,
                "due_time": 120,
                "route": ["Litho", "Etch", "Clean"],
                "completed": True,
            },
            "operation": {
                "kind": "process",
                "wafer_id": "W01",
                "machine_id": "M1-A",
                "process": "Litho",
                "step": 0,
                "start": 0,
                "end": 12,
            },
            "machine_event": {
                "kind": "setup",
                "machine_id": "M1-A",
                "from_product_id": "P01",
                "to_product_id": "P02",
                "start": 12,
                "end": 21,
            },
            "queue_sample": {"time": 10, "queue": "Litho", "length": 3},
        }
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
