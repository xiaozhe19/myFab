from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from pathlib import Path
from statistics import mean, stdev

from hyperopt import STATUS_OK, Trials, fmin, hp, tpe

from fab.random_run import extract_run_metrics
from fab.run_sim import (
    DEFAULT_FACTORY_PATH,
    DEFAULT_MACHINE_PATH,
    DEFAULT_ORDERS_PATH,
    DEFAULT_PRODUCT_PATH,
    DEFAULT_RESULT_DIR,
    DEFAULT_SEEDS_PATH,
    DEFAULT_SIMULATION_PATH,
    build_order_pool,
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
from strategy.dbr_v2 import DynamicDBR


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DBR_CONFIG = PROJECT_ROOT / "strategy" / "config" / "DBR_v2_config.json"
DEFAULT_OUTPUT = DEFAULT_RESULT_DIR / "dbr_v2_hyperopt_result.json"
DEFAULT_BEST_CONFIG = PROJECT_ROOT / "strategy" / "config" / "DBR_v2_hyperopt_best.json"

TRACKED_METRIC_KEYS = (
    "throughput",
    "average_fab_wip",
    "mct_average",
    "mct_p95",
)
SCORE_KEYS = ("throughput", "mct_p95", "average_fab_wip")
DEFAULT_SCORE_WEIGHTS = {
    "throughput": 0.5,
    "mct_p95": 0.3,
    "average_fab_wip": 0.2,
}


# Hyperopt 会从这个搜索空间里采样 DBR v2 参数。
# 这些范围不是最终定论，只是第一版比较保守的搜索范围。
SEARCH_SPACE = {
    "parameters": {
        "L0": hp.uniform("L0", 3.0, 12.0),
        "m0": hp.uniform("m0", 0.0, 0.05),
        "m1": hp.uniform("m1", 0.0, 2.0),
        "m2": hp.uniform("m2", -2.0, 0.5),
        "n0": hp.uniform("n0", 0.0, 0.05),
        "n1": hp.uniform("n1", 0.0, 2.0),
        "n2": hp.uniform("n2", 0.0, 0.05),
        "s1": hp.uniform("s1", -0.02, 0.0),
        "s2": hp.uniform("s2", 0.0, 0.02),
        "p1": hp.uniform("p1", 0.0, 0.6),
        "p2": hp.uniform("p2", 0.0, 0.3),
        "BL": hp.uniform("BL", 600.0, 1800.0),
        "r": hp.uniform("r", 0.5, 2.0),
        "setup_penalty_weight": hp.uniform("setup_penalty_weight", 0.0, 3.0),
    },
    "sub_bottleneck_window": hp.choice(
        "sub_bottleneck_window",
        [30.0, 60.0, 120.0, 180.0],
    ),
}


def average_metric(rows, key):
    values = [row["metrics"][key] for row in rows if key in row["metrics"]]
    return mean(values) if values else 0.0


def percentile(values, quantile):
    """Linear-interpolated percentile without adding a numerical dependency."""

    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def mct_p95_from_result(result):
    """Calculate one run's P95 MCT from completed wafers in the result."""

    mct_values = []
    for wafer in result.get("wafers", []):
        release_time = wafer.get("release_time")
        end_time = wafer.get("end_time")
        if (
            isinstance(release_time, (int, float))
            and isinstance(end_time, (int, float))
            and end_time >= release_time
        ):
            mct_values.append(end_time - release_time)
    return round(percentile(mct_values, 0.95), 6)


def run_dbr_on_seed(
    strategy_params,
    seed_set,
    base_factory_config,
    simulation_config,
    order_config,
):
    #用一组 DBR 参数和一组随机种子跑一次仿真。

    factory_config = apply_factory_seeds(
        deepcopy(base_factory_config),
        seed_set,
    )
    order_pool = build_order_pool(
        factory_config,
        deepcopy(order_config),
        seed_set["order_seed"],
    )
    strategy = DynamicDBR(**deepcopy(strategy_params))
    result = run_sim(
        strategy,
        factory_config,
        deepcopy(simulation_config),
        order_pool,
    )
    metrics = extract_run_metrics(result)
    metrics["mct_p95"] = mct_p95_from_result(result)
    return {
        "seed_set": deepcopy(seed_set),
        "metrics": metrics,
    }


def run_dbr_seed_task(task):
    """Pickle-friendly adapter for ProcessPoolExecutor on Windows."""

    return run_dbr_on_seed(*task)


def run_dbr_replicates(
    strategy_params,
    seed_sets,
    base_factory_config,
    simulation_config,
    order_config,
    executor=None,
):
    """同一组 DBR 参数跑多个 seed，然后返回每个 seed 的指标。"""

    tasks = [
        (
            deepcopy(strategy_params),
            deepcopy(seed_set),
            base_factory_config,
            simulation_config,
            order_config,
        )
        for seed_set in seed_sets
    ]
    if executor is None:
        return [run_dbr_on_seed(*task) for task in tasks]
    return list(executor.map(run_dbr_seed_task, tasks))


def normalize_weights(weights=None):
    selected = dict(DEFAULT_SCORE_WEIGHTS if weights is None else weights)
    if any(value < 0 for value in selected.values()):
        raise ValueError("Score weights must be non-negative.")
    total = sum(selected.values())
    if total <= 0:
        raise ValueError("At least one score weight must be positive.")
    return {key: value / total for key, value in selected.items()}


def score_against_baseline(summary, baseline, weights=None):
    """Return the scalar BO objective for the three selected metrics.

    Throughput is maximized; P95 MCT and average WIP are minimized. All three
    metrics are normalized against the same baseline before weighting.
    """

    weights = normalize_weights(weights)

    throughput_ratio = (
        summary["throughput"] / baseline["throughput"]
        if baseline["throughput"]
        else 0.0
    )
    wip_ratio = (
        summary["average_fab_wip"] / baseline["average_fab_wip"]
        if baseline["average_fab_wip"]
        else 1.0
    )
    p95_ratio = (
        summary["mct_p95"] / baseline["mct_p95"]
        if baseline["mct_p95"]
        else 1.0
    )
    return (
        weights["throughput"] * throughput_ratio
        - weights["mct_p95"] * p95_ratio
        - weights["average_fab_wip"] * wip_ratio
    )


def summarize_rows(rows):
    summary = {}
    for key in TRACKED_METRIC_KEYS:
        values = [row["metrics"][key] for row in rows if key in row["metrics"]]
        summary[key] = round(mean(values), 6) if values else 0.0
        summary[f"{key}_std"] = round(stdev(values), 6) if len(values) > 1 else 0.0
    for key in ("setup_count", "movements"):
        summary[key] = round(average_metric(rows, key), 6)
    return summary


def ensure_seed_count(seed_sets, required_count):
    """Extend the deterministic seed list when independent validation seeds are needed."""

    result = deepcopy(seed_sets)
    if len(result) >= required_count:
        return result
    next_seed = max(
        max(seed_set.values()) for seed_set in result
    ) + 1000
    while len(result) < required_count:
        result.append(
            {
                "order_seed": next_seed,
                "downtime_seed": next_seed + 1,
                "setup_seed": next_seed + 2,
            }
        )
        next_seed += 10
    return result


def save_json(payload, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, ensure_ascii=False)
        file.write("\n")


def optimize_dbr(
    max_evals=50,
    screening_seed_count=10,
    validation_seed_count=20,
    top_k=5,
    score_weights=None,
    max_workers=5,
):
    """Run DBR BO with parallel seed replications."""

    if max_workers <= 1:
        return _optimize_dbr(
            max_evals,
            screening_seed_count,
            validation_seed_count,
            top_k,
            score_weights,
            None,
        )
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        return _optimize_dbr(
            max_evals,
            screening_seed_count,
            validation_seed_count,
            top_k,
            score_weights,
            executor,
        )


def _optimize_dbr(
    max_evals=50,
    screening_seed_count=10,
    validation_seed_count=20,
    top_k=5,
    score_weights=None,
    executor=None,
):
    """Run scalar BO screening and independent validation.

    Screening evaluates max_evals parameter groups on the first seed block.
    The highest-scoring candidates are then evaluated on a separate seed block.
    """

    score_weights = normalize_weights(score_weights)
    requested_seed_count = screening_seed_count + validation_seed_count
    seed_sets = ensure_seed_count(
        load_seed_sets(DEFAULT_SEEDS_PATH),
        requested_seed_count,
    )
    screening_seeds = seed_sets[:screening_seed_count]
    validation_seeds = seed_sets[
        screening_seed_count:requested_seed_count
    ]
    default_params = load_strategy_parameters(DEFAULT_DBR_CONFIG)
    base_factory_config = build_factory_config(
        load_json(DEFAULT_FACTORY_PATH),
        load_json(DEFAULT_PRODUCT_PATH),
        load_machine_config(DEFAULT_MACHINE_PATH),
    )
    simulation_config = load_json(DEFAULT_SIMULATION_PATH)
    order_config = load_json(DEFAULT_ORDERS_PATH)

    print("Running default DBR v2 baseline...")
    baseline_screening_rows = run_dbr_replicates(
        default_params,
        screening_seeds,
        base_factory_config,
        simulation_config,
        order_config,
        executor=executor,
    )
    baseline_screening_summary = summarize_rows(baseline_screening_rows)
    baseline_validation_rows = run_dbr_replicates(
        default_params,
        validation_seeds,
        base_factory_config,
        simulation_config,
        order_config,
        executor=executor,
    )
    baseline_validation_summary = summarize_rows(baseline_validation_rows)
    print(f"Baseline screening: {baseline_screening_summary}")
    print(f"Baseline validation: {baseline_validation_summary}")

    trial_rows = []

    def objective(sampled_params):
        rows = run_dbr_replicates(
            sampled_params,
            screening_seeds,
            base_factory_config,
            simulation_config,
            order_config,
            executor=executor,
        )
        summary = summarize_rows(rows)
        score = score_against_baseline(
            summary,
            baseline_screening_summary,
            score_weights,
        )
        trial_index = len(trial_rows) + 1
        trial_rows.append(
            {
                "trial": trial_index,
                "loss": -score,
                "score": score,
                "params": sampled_params,
                "summary": summary,
                "runs": rows,
            }
        )
        print(
            f"Trial {trial_index}: "
            f"score {score:.4f}, "
            f"throughput {summary['throughput']:.2f}, "
            f"MCT {summary['mct_average']:.2f}, "
            f"P95 MCT {summary['mct_p95']:.2f}, "
            f"WIP {summary['average_fab_wip']:.2f}"
        )
        return {
            "loss": -score,
            "status": STATUS_OK,
        }

    trials = Trials()
    fmin(
        fn=objective,
        space=SEARCH_SPACE,
        algo=tpe.suggest,
        max_evals=max_evals,
        trials=trials,
    )

    selected_candidates = sorted(
        trial_rows,
        key=lambda row: row["score"],
        reverse=True,
    )[:top_k]

    validation_rows = []
    for candidate in selected_candidates:
        print(f"Validating trial {candidate['trial']}...")
        rows = run_dbr_replicates(
            candidate["params"],
            validation_seeds,
            base_factory_config,
            simulation_config,
            order_config,
            executor=executor,
        )
        validation_summary = summarize_rows(rows)
        validation_score = score_against_baseline(
            validation_summary,
            baseline_validation_summary,
            score_weights,
        )
        validation_rows.append(
            {
                "trial": candidate["trial"],
                "params": candidate["params"],
                "screening_score": candidate["score"],
                "screening_summary": candidate["summary"],
                "validation_score": validation_score,
                "validation_summary": validation_summary,
                "runs": rows,
            }
        )
        print(
            f"Validation trial {candidate['trial']}: "
            f"throughput {validation_summary['throughput']:.2f}, "
            f"MCT {validation_summary['mct_average']:.2f}, "
            f"P95 MCT {validation_summary['mct_p95']:.2f}, "
            f"WIP {validation_summary['average_fab_wip']:.2f}, "
            f"score {validation_score:.4f}"
        )

    recommended = max(
        validation_rows,
        key=lambda row: row["validation_score"],
        default=None,
    )

    best_params = (
        deepcopy(recommended["params"])
        if recommended is not None
        else deepcopy(selected_candidates[0]["params"])
        if selected_candidates
        else deepcopy(default_params)
    )

    result = {
        "result_kind": "dbr_v2_two_stage_bo_score_search",
        "tracked_metrics": list(TRACKED_METRIC_KEYS),
        "score_metrics": list(SCORE_KEYS),
        "score_weights": score_weights,
        "score_definition": (
            "w_throughput * throughput/baseline_throughput "
            "- w_p95 * p95_mct/baseline_p95_mct "
            "- w_wip * average_fab_wip/baseline_average_fab_wip"
        ),
        "max_evals": max_evals,
        "screening_seed_count": screening_seed_count,
        "validation_seed_count": validation_seed_count,
        "top_k": top_k,
        "screening_seeds": screening_seeds,
        "validation_seeds": validation_seeds,
        "baseline": {
            "screening": baseline_screening_summary,
            "validation": baseline_validation_summary,
            "screening_runs": baseline_screening_rows,
            "validation_runs": baseline_validation_rows,
        },
        "screening": {
            "trials": trial_rows,
            "selected_candidates": selected_candidates,
        },
        "validation": {
            "candidates": validation_rows,
            "recommended": recommended,
        },
    }
    save_json(result, DEFAULT_OUTPUT)
    save_json(best_params, DEFAULT_BEST_CONFIG)
    print(f"Saved search result to {DEFAULT_OUTPUT}")
    print(f"Saved best config to {DEFAULT_BEST_CONFIG}")
    return result


def main():
    parser = argparse.ArgumentParser(description="Optimize DBR v2 parameters with Hyperopt.")
    parser.add_argument("--max-evals", type=int, default=50)
    parser.add_argument("--screening-seed-count", type=int, default=10)
    parser.add_argument("--validation-seed-count", type=int, default=20)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--throughput-weight", type=float, default=0.5)
    parser.add_argument("--p95-mct-weight", type=float, default=0.3)
    parser.add_argument("--wip-weight", type=float, default=0.2)
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args()
    optimize_dbr(
        max_evals=args.max_evals,
        screening_seed_count=args.screening_seed_count,
        validation_seed_count=args.validation_seed_count,
        top_k=args.top_k,
        score_weights={
            "throughput": args.throughput_weight,
            "mct_p95": args.p95_mct_weight,
            "average_fab_wip": args.wip_weight,
        },
        max_workers=args.workers,
    )


if __name__ == "__main__":
    main()
