from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from statistics import mean, pvariance
from typing import Any

from flask import Flask, jsonify, render_template, request
import json
from pathlib import Path


app = Flask(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent


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


def _pretty_label(stem: str) -> str:
    text = stem.replace("_result", "").replace("_", " ")
    return " ".join(part.upper() if part.isalpha() and len(part) <= 4 else part.title() for part in text.split())


def _discover_result_files() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [
        {
            "id": "current",
            "label": "当前运行结果",
            "source": "memory",
            "path": None,
        }
    ]
    for path in sorted(PROJECT_ROOT.glob("*_result.json")):
        payload = _load_json_file(path) or {}
        stem = path.stem.replace("_result", "")
        strategy = str(payload.get("strategy") or "").strip()
        label = strategy or _pretty_label(stem)
        items.append(
            {
                "id": stem,
                "label": label,
                "source": "file",
                "path": path.name,
            }
        )
    return items


def _load_initial_run() -> dict[str, Any]:
    for entry in _discover_result_files():
        if entry["id"] == "current":
            continue
        payload = _load_json_file(PROJECT_ROOT / str(entry["path"]))
        if payload:
            return payload
    return _sample_run()


CURRENT_RUN = _load_initial_run()


def _normalize_run(payload: dict[str, Any]) -> dict[str, Any]:
    run = deepcopy(payload)
    factory_config = _load_json_file(PROJECT_ROOT / "factory_config.json") or {}
    order_config = _load_json_file(PROJECT_ROOT / "order_config.json") or {}
    simulation_config = _load_json_file(PROJECT_ROOT / "simulation_config.json") or {}
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
            return _normalize_run(payload)
    raise KeyError(result_id)


def compute_dashboard(run: dict[str, Any]) -> dict[str, Any]:
    run = _normalize_run(run)
    wafers = {str(w["id"]): dict(w) for w in run["wafers"] if "id" in w}
    machines = {str(m["id"]): dict(m) for m in run["machines"] if "id" in m}
    operations = sorted(
        [dict(op) for op in run["operations"]],
        key=lambda op: (_num(op.get("start")), _num(op.get("end"))),
    )
    machine_events = sorted(
        [dict(event) for event in run.get("machine_events", [])],
        key=lambda event: (_num(event.get("start")), _num(event.get("end"))),
    )

    machine_processing: dict[str, float] = {machine_id: 0.0 for machine_id in machines}
    machine_setup: dict[str, float] = {machine_id: 0.0 for machine_id in machines}
    machine_down: dict[str, float] = {machine_id: 0.0 for machine_id in machines}
    machine_ops: dict[str, list[dict[str, Any]]] = {machine_id: [] for machine_id in machines}
    wafer_ops: dict[str, list[dict[str, Any]]] = {wafer_id: [] for wafer_id in wafers}
    process_busy: dict[str, float] = {}
    waits_by_process: dict[str, list[float]] = {}

    measurement = run.get("measurement", {})
    measurement_horizon = _num(measurement.get("measurement_horizon"))
    min_release = min((_num(w.get("release_time")) for w in wafers.values()), default=0.0)
    max_end = max(
        [_num(op.get("end")) for op in operations] + [_num(event.get("end")) for event in machine_events],
        default=0.0,
    )
    horizon = measurement_horizon or max(
        _num(run.get("current_time"), max_end) - min_release,
        0.0,
    )
    if horizon == 0 and max_end > 0:
        horizon = max_end

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

    utilization_values = [row["utilization"] for row in utilization_rows]
    queue_groups: dict[str, list[float]] = {}
    for sample in run.get("queue_samples", []):
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

    wait_rows = [
        {
            "process": process,
            "average_wait": round(_average(values), 3),
            "max_wait": round(max(values), 3) if values else 0,
        }
        for process, values in waits_by_process.items()
    ]

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
    measurement_start = _num(measurement.get("measurement_start_time"), min_release)
    measurement_end = _num(
        measurement.get("measurement_end_time"),
        measurement_start + horizon,
    )
    released_count = int(_num(measurement.get("released")))
    movement_count = int(_num(measurement.get("movements")))
    average_fab_wip = _num(measurement.get("average_fab_wip"))
    throughput_count = int(
        _num(measurement.get("throughput"), len(completed_wafers))
    )
    throughput_rate = throughput_count / horizon if horizon > 0 else 0.0

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
        event for event in run.get("order_events", []) if event.get("accepted")
    ]
    station_count = len(
        {
            str(machine.get("type", "Unknown"))
            for machine in machines.values()
        }
    )

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
        "raw": run,
    }


@app.get("/")
def dashboard_page():
    return render_template("fab_dashboard.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "fab-dashboard", "time": _now_iso()})


@app.get("/api/dashboard")
def dashboard_data():
    result_id = request.args.get("result")
    try:
        return jsonify(compute_dashboard(_load_run_by_id(result_id)))
    except KeyError:
        return jsonify({"error": f"Unknown result id: {result_id}"}), 404


@app.get("/api/sample-run")
def sample_run():
    return jsonify(_sample_run())


@app.get("/api/results")
def list_results():
    selected = request.args.get("result") or "current"
    return jsonify(
        {
            "selected": selected if selected in {item["id"] for item in _discover_result_files()} else "current",
            "results": _discover_result_files(),
        }
    )


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
    return jsonify({"ok": True, "dashboard": compute_dashboard(CURRENT_RUN)})


@app.get("/api/schema")
def api_schema():
    return jsonify(
        {
            "required_top_level": ["machines", "wafers", "operations"],
            "optional_top_level": [
                "run_id",
                "strategy",
                "time_unit",
                "generated_at",
                "current_time",
                "machine_events",
                "queue_samples",
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
