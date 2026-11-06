"""分支树采集（Fabotron 阶段 2）：每个种子只预热一次，从检查点长出一棵分支树。

流程：
1. **预热任务**（每个种子一个）：FIFO_SA（空时间表的 MixedRule）跑到预热结束，
   存成检查点 ``warm_cache/seed_<seed>_<key>.pkl``。key 由种子、预热设置、模型和
   源码 hash 决定；以后的采集计划只要这些不变就直接复用，不再预热。
2. **分支任务**（每条分支一个，全部进同一个进程池）：读出发点检查点，换一套仿真
   随机流，重置 PolicyController 的策略随机流，接上自己的 SQLite，跑 ``days``/``hours``。
   如果有子节点从它的中途分叉，跑到那个时刻先存一个检查点再继续。
3. 子节点在父分支完成后才开始。测试种子上的评估节点（``eval: true``）只跑几天，
   多份副本同策略随机流、不同仿真随机流，用来检查模型预测的分布。

计划里的分支树（``tree``）例子::

    {"name": "B", "from": "warm", "branches": 4, "days": 240}
    {"name": "C", "from": "B@60", "branches": 2, "days": 60}      # 每条 B 的第 60 天再分 2 条
    {"name": "E", "from": "warm", "branches": 16, "hours": 72, "eval": true, "policy": "shared"}

实例名：根节点 ``B0..B3``；子节点 ``B2.C1``。训练节点必须覆盖全部 split，
评估节点默认只在 test 种子上。``policy``：``fresh``（默认，每条分支独立的策略随机流）
或 ``shared``（同一父实例下的副本共用一条策略随机流）。分叉时都会立即重抽权重。

manifest 兼容 ``common.training_manifest``：每个训练实例是一个 "策略"。

用法：
  python -m fabotron.data_collection.collect_branches --plan <plan.json> [--workers N] [--resume]
  python -m fabotron.data_collection.collect_branches --plan <plan.json> --dry-run
"""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from contextlib import redirect_stderr, redirect_stdout
import json
import math
import multiprocessing
from pathlib import Path
import re
import sys
import time
import traceback

from fabotron.data_collection.common import (
    ROOT, SPLITS, batch_lock, canonical_hash, file_hash, project_path, read_json,
    timestamp, validate_database, write_json,
)

MANIFEST_VERSION = "fabotron-collection-v1"
POLICY_NAME = "MIXED"
DAY = 1440.0
# 预热模型的终点放得很远：预热状态与终点无关（终点之后的事件只是不进日历），
# 各分支再用 retarget_end 改成自己的终点。这样同一预热检查点能给任何计划复用。
WARM_HORIZON_DAYS = 3650
SOURCE_DIRS = ("fab", "strategy")
SOURCE_FILES = (
    "fabotron/data_collection/common.py",
    "fabotron/data_collection/collect_branches.py",
    "fabotron/data_collection/branching.py",
    "fabotron/data_collection/policy_control.py",
)
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


# ---------------------------------------------------------------- 计划与分支树


def load_plan(path: Path, output_dir: str | None = None) -> dict:
    plan = read_json(path)
    if plan.get("version") != 3:
        raise ValueError("Branch collection plan must have version 3")
    plan["model_database"] = str(project_path(plan["model_database"]))
    plan["output_dir"] = str(project_path(output_dir or plan["output_dir"]))
    plan["hot_groups_config"] = str(project_path(plan["hot_groups_config"]))
    plan["warm_cache_dir"] = str(project_path(plan.get("warm_cache_dir", "data/result/warm_cache")))
    splits = plan["seed_splits"]
    seeds = [s for name in SPLITS for s in splits[name]]
    if set(splits) != set(SPLITS) or any(not splits[n] for n in SPLITS):
        raise ValueError("Provide nonempty train, validation and test seed lists")
    if len(seeds) != len(set(seeds)) or any(type(s) is not int or s < 0 for s in seeds):
        raise ValueError("Seeds must be unique nonnegative integers")
    expand_tree(plan)  # 只为校验
    return plan


def expand_tree(plan: dict) -> list[dict]:
    """分支树 -> 实例列表（父实例在前）。时间都是绝对分钟数。"""

    warm_end = plan["simulation"]["start_time"] + plan["simulation"]["warmup_days"] * DAY
    nodes, instances = {}, []
    for node in plan["tree"]:
        name = node["name"]
        if not NAME.match(name) or name in nodes:
            raise ValueError(f"node name {name!r} must be unique and alphanumeric")
        if ("days" in node) == ("hours" in node):
            raise ValueError(f"{name}: give exactly one of days / hours")
        duration = node["days"] * DAY if "days" in node else node["hours"] * 60.0
        if duration <= 0 or type(node["branches"]) is not int or node["branches"] < 1:
            raise ValueError(f"{name}: duration and branches must be positive")
        if node.get("policy", "fresh") not in ("fresh", "shared"):
            raise ValueError(f"{name}: policy must be fresh or shared")
        is_eval = bool(node.get("eval", False))
        node_splits = list(node.get("splits", ["test"] if is_eval else SPLITS))
        if not set(node_splits) <= set(SPLITS):
            raise ValueError(f"{name}: unknown split")
        if not is_eval and sorted(node_splits) != sorted(SPLITS):
            raise ValueError(f"{name}: training nodes must cover every split; use eval for subsets")
        source = node["from"]
        if source == "warm":
            parents, offset = [None], 0.0
        else:
            parent, _, at = source.partition("@")
            if parent not in nodes or not at:
                raise ValueError(f"{name}: from must be 'warm' or '<earlier node>@<day>'")
            if nodes[parent]["eval"]:
                raise ValueError(f"{name}: cannot branch from an eval node")
            if float(at) != int(float(at)):
                raise ValueError(f"{name}: fork day must be a whole day")
            offset = float(at) * DAY
            if not 0 < offset < nodes[parent]["duration"]:
                raise ValueError(f"{name}: fork day must lie inside {parent}")
            if not set(node_splits) <= set(nodes[parent]["splits"]):
                raise ValueError(f"{name}: splits must be a subset of the parent's")
            parents = [i for i in instances if i["node"] == parent]
        nodes[name] = {"duration": duration, "eval": is_eval, "splits": node_splits}
        for parent in parents:
            start = warm_end + offset if parent is None else parent["start"] + offset
            if parent is not None:
                parent["saves"].append(start)
            group = "warm" if parent is None else parent["id"]
            for k in range(node["branches"]):
                instance = f"{name}{k}" if parent is None else f"{parent['id']}.{name}{k}"
                instances.append({
                    "id": instance, "node": name, "parent": None if parent is None else parent["id"],
                    "start": start, "end": start + duration, "eval": is_eval, "splits": node_splits,
                    "fab_tag": instance,
                    "policy_tag": instance if node.get("policy", "fresh") == "fresh" else f"{group}|{name}",
                    "saves": [],
                })
    for instance in instances:
        instance["saves"] = sorted(set(instance["saves"]))
    return instances


def sources_hash(plan: dict) -> dict[str, str]:
    paths = [p for d in SOURCE_DIRS for p in sorted((ROOT / d).rglob("*.py"))]
    paths += [ROOT / f for f in SOURCE_FILES]
    paths.append(Path(plan["hot_groups_config"]))
    return {p.resolve().relative_to(ROOT).as_posix(): file_hash(p) for p in paths}


def warm_model(model, plan: dict):
    from dataclasses import replace

    sim = plan["simulation"]
    start = sim["start_time"]
    return replace(model, simulation=replace(
        model.simulation, start_time=start, warmup_time=sim["warmup_days"] * DAY,
        end_time=start + (sim["warmup_days"] + WARM_HORIZON_DAYS) * DAY))


def tokenizer_options(plan: dict) -> tuple[dict, float]:
    options = dict(plan["tokenizer"])
    return options, float(options.pop("sample_interval_minutes"))


def build_contract(plan: dict, model) -> dict:
    from fab.plugins.tokenizer_core import TokenizerConfig, ToolGroupTokenizer
    from fabotron.data_collection.policy_control import VOCAB

    options, interval = tokenizer_options(plan)
    encoder = ToolGroupTokenizer(model, TokenizerConfig(dispatch_rules=VOCAB, **options))
    warm_end = plan["simulation"]["start_time"] + plan["simulation"]["warmup_days"] * DAY
    return {
        "schema": json.loads(json.dumps(encoder.schema())), "schema_id": encoder.schema_id,
        "capture_start_minutes": warm_end, "sample_interval_minutes": interval,
        "shape": encoder.shape, "policy_vocabulary": list(VOCAB),
        "daily_metrics_enabled": bool(plan.get("daily_metrics_plugin")), "raw_event_logs_enabled": False,
    }


def job_contract(contract: dict, job: dict) -> dict:
    """每条分支自己的起止时刻、快照数与每日指标范围。"""

    interval = contract["sample_interval_minutes"]
    steps = (job["end"] - job["start"]) / interval
    if not math.isclose(steps, round(steps), abs_tol=1e-8) or steps < 1:
        raise ValueError(f"{job['id']}: duration must be a whole number of sampling intervals")
    result = {**contract, "capture_start_minutes": job["start"], "snapshots": round(steps) + 1}
    if contract["daily_metrics_enabled"]:
        result["daily_metrics"] = {
            "day_minutes": DAY, "start_time": job["start"], "end_time": job["end"],
            "measurement_start_time": contract["capture_start_minutes"],
            "records": math.ceil((job["end"] - job["start"]) / DAY), "first_day": round(job["start"] / DAY) + 1,
        }
    return result


def warm_key(plan: dict, seed: int, provenance: dict) -> str:
    return canonical_hash({
        "seed": seed, "simulation": plan["simulation"], "tokenizer": plan["tokenizer"],
        "bd_lead_days": plan["bd_lead_days"], "daily_metrics_plugin": plan.get("daily_metrics_plugin"),
        "warm_horizon_days": WARM_HORIZON_DAYS, **provenance,
    })


def prepare(plan: dict) -> dict:
    from fab.model.sqlite_loader import load_fab_model_from_sqlite

    model = warm_model(load_fab_model_from_sqlite(Path(plan["model_database"])), plan)
    contract = build_contract(plan, model)
    provenance = {"model_sha256": file_hash(plan["model_database"]), "source_sha256": sources_hash(plan)}
    instances = expand_tree(plan)
    training = [i["id"] for i in instances if not i["eval"]]
    # training_manifest 把每个训练实例当作一种 "策略"。
    plan["strategies"] = [{"id": i, "policy": POLICY_NAME, "factory": "strategy.mixed:MixedRule"} for i in training]
    fingerprint = canonical_hash({"plan": {k: v for k, v in plan.items() if k != "workers"},
                                  "contract": contract, "provenance": provenance})
    jobs, eval_jobs, warm = [], [], []
    for split in SPLITS:
        for seed in plan["seed_splits"][split]:
            warm.append({"seed": seed, "key": warm_key(plan, seed, provenance)})
            for i in instances:
                if split not in i["splits"]:
                    continue
                job = {
                    "id": f"{i['id']}_seed{seed}", "seed": seed, "instance": i["id"], "node": i["node"],
                    "parent": None if i["parent"] is None else f"{i['parent']}_seed{seed}",
                    "start": i["start"], "end": i["end"], "saves": i["saves"],
                    "capture_start_minutes": i["start"], "fab_tag": i["fab_tag"], "policy_tag": i["policy_tag"],
                    "strategy": {"id": i["id"], "policy": POLICY_NAME}, "status": "pending", "attempt": 0,
                }
                job["snapshots"] = job_contract(contract, job)["snapshots"]
                if i["eval"]:
                    job.update(split="eval", database=f"eval/seed_{seed}/{i['id']}.sqlite")
                    eval_jobs.append(job)
                else:
                    job.update(split=split, database=f"{split}/{i['id']}/seed_{seed}.sqlite")
                    jobs.append(job)
    now = timestamp()
    return {"version": MANIFEST_VERSION, "kind": "branch-collection-v3", "created_at": now,
            "updated_at": now, "batch_fingerprint": fingerprint, "plan": plan, "contract": contract,
            "provenance": provenance, "warm": warm, "jobs": jobs, "eval_jobs": eval_jobs,
            "preflight_errors": []}


def policy_row_ok(row) -> bool:
    """策略槽：要么 one-hot FIFO_SA（没选中），要么 FIFO_SA=0 且其余和为 1（float32 存储）。"""

    if any(not 0.0 <= v <= 1.0 for v in row) or not math.isclose(math.fsum(row), 1.0, abs_tol=1e-5):
        return False
    return row[0] in (0.0, 1.0)


def checkpoint_path(out: Path, job: dict, time_minutes: float) -> Path:
    return out / "checkpoints" / f"seed_{job['seed']}" / f"{job['instance']}@{time_minutes / DAY:g}d.pkl"


def warm_paths(plan: dict, item: dict) -> tuple[Path, Path, Path]:
    base = Path(plan["warm_cache_dir"]) / f"seed_{item['seed']}_{item['key'][:16]}"
    return base.with_suffix(".pkl"), base.with_suffix(".sqlite"), base.with_suffix(".json")


def warm_ready(plan: dict, item: dict) -> bool:
    pkl, _db, meta = warm_paths(plan, item)
    return pkl.is_file() and meta.is_file() and read_json(meta).get("key") == item["key"]


# ---------------------------------------------------------------- 子进程任务


def _check_provenance(plan: dict, provenance: dict) -> None:
    if file_hash(plan["model_database"]) != provenance["model_sha256"] or sources_hash(plan) != provenance["source_sha256"]:
        raise ValueError("Model database or source changed after preflight")


def _run_logged(log_path: Path, body) -> dict:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    began = time.perf_counter()
    with log_path.open("w", encoding="utf-8", buffering=1) as log, redirect_stdout(log), redirect_stderr(log):
        try:
            result = body()
            return {"status": "complete", "result": result, "wall_seconds": time.perf_counter() - began}
        except BaseException as error:
            traceback.print_exc()
            return {"status": "failed", "error": f"{type(error).__name__}: {error}", "log": str(log_path)}


def warm_task(request: dict) -> dict:
    """一个种子的预热：FIFO_SA 跑到预热结束，存检查点和预热期的数据库。"""

    def body():
        from fab.engine import FabEngine
        from fab.model.sqlite_loader import load_fab_model_from_sqlite
        from fab.plugins.plugin_manager import PluginManager
        from fab.plugins.results import OnlineMetricsPlugin
        from fab.plugins.sqlite_storage import SQLiteStoragePlugin
        from fab.plugins.tokenizer import PERSISTENCE_BASELINE, TokenizerPlugin
        from fab.plugins.tokenizer_core import TokenizerConfig
        from fab.run_model import build_plugin
        from fabotron.data_collection.branching import RelayStorage, StopAt, StopAtFork, save_checkpoint
        from fabotron.data_collection.policy_control import VOCAB, PolicyController
        from strategy.mixed import MixedRule

        plan, item = request["plan"], request["warm"]
        _check_provenance(plan, request["provenance"])
        began = time.perf_counter()
        model = warm_model(load_fab_model_from_sqlite(Path(plan["model_database"])), plan)
        fork_time = model.simulation.start_time + model.simulation.warmup_time
        options, interval = tokenizer_options(plan)
        strategy = MixedRule()
        relay = RelayStorage()
        controller = PolicyController(
            model, strategy, interval_minutes=interval,
            bd_start_time=fork_time - plan["bd_lead_days"] * DAY, config_path=Path(plan["hot_groups_config"]),
        )
        tokenizer = TokenizerPlugin(
            TokenizerConfig(dispatch_rules=VOCAB, **options), dispatch_policy=controller.policy,
            start_time=fork_time, sample_interval_minutes=interval, forecast_mode=PERSISTENCE_BASELINE,
        )
        stopper = StopAt([fork_time])
        # 顺序：停点 -> 存储 -> 控制器 -> Tokenizer，见各类说明。
        plugins = [stopper, relay, controller, tokenizer, OnlineMetricsPlugin()]
        if plan.get("daily_metrics_plugin"):
            plugins.append(build_plugin(plan["daily_metrics_plugin"]))
        engine = FabEngine(model, order_seed=item["seed"], plugin_manager=PluginManager(plugins))
        for plugin in plugins:
            bind = getattr(plugin, "bind_engine", None)
            if callable(bind):
                bind(engine)
        engine.begin_episode(strategy)
        try:
            engine.advance()
            raise RuntimeError("simulation ended before the end of warm-up")
        except StopAtFork:
            pass
        pkl, db, meta = warm_paths(plan, item)
        for path in (pkl, db, meta):
            path.unlink(missing_ok=True)
        pkl.parent.mkdir(parents=True, exist_ok=True)
        relay.flush_to(SQLiteStoragePlugin(db), "WARMUP")
        size = save_checkpoint(pkl, (engine, relay, controller, stopper))
        result = {"key": item["key"], "seed": item["seed"], "checkpoint_bytes": size,
                  "warm_seconds": time.perf_counter() - began, "created_at": timestamp()}
        write_json(meta, result)
        print(f"seed {item['seed']} warm-up {result['warm_seconds']:.0f}s, checkpoint {size / 2**20:.0f} MB", flush=True)
        return result

    out = Path(request["plan"]["output_dir"])
    return _run_logged(out / "logs" / f"warm_seed_{request['warm']['seed']}.log", body)


def _metadata(job: dict, plan: dict, contract: dict, provenance: dict, fingerprint: str) -> dict:
    return {"seed": job["seed"], "split": job["split"], "strategy_id": job["strategy"]["id"],
            "strategy_name": POLICY_NAME, "strategy_factory": "strategy.mixed:MixedRule",
            "batch_fingerprint": fingerprint, "attempt": job.get("attempt", 0),
            "started_at": timestamp(), "python": sys.version, "simulation": plan["simulation"],
            "tokenizer": plan["tokenizer"], "policy_vocabulary": contract["policy_vocabulary"],
            "schema_id": contract["schema_id"], "model_sha256": provenance["model_sha256"],
            "capture_start_minutes": job["start"], "capture_end_minutes": job["end"],
            "instance": job["instance"], "parent": job["parent"], "fab_tag": job["fab_tag"],
            "policy_tag": job["policy_tag"], "daily_metrics_enabled": contract["daily_metrics_enabled"],
            "raw_event_logs_enabled": False, "steady_state_certified": False}


def branch_task(request: dict) -> dict:
    """一条分支：读出发点检查点 -> 换随机流 -> 跑到终点（途中按需存检查点）-> 校验。"""

    job = request["job"]

    def body():
        from fab.plugins.sqlite_storage import SQLiteStoragePlugin
        from fabotron.data_collection.branching import (
            StopAtFork, load_checkpoint, reseed_branch, retarget_end, save_checkpoint,
        )

        plan, contract, provenance = request["plan"], request["contract"], request["provenance"]
        _check_provenance(plan, provenance)
        out = Path(plan["output_dir"])
        database = out / job["database"]
        if database.exists():
            raise FileExistsError(database)
        database.parent.mkdir(parents=True, exist_ok=True)
        engine, relay, controller, stopper = load_checkpoint(request["source"])
        # 停点在时钟越过 start 的那一刻触发，start 时刻的快照与控制更新都还没做。
        if engine.current_time < job["start"] or controller.next_time != job["start"]:
            raise ValueError("checkpoint time does not match the branch start")
        reseed_branch(engine, job["fab_tag"])
        controller.activate(f"{job['seed']}|policy|{job['policy_tag']}", **plan.get("sampler", {}))
        controller.weights.clear()  # 分叉时立即按新随机流重抽
        controller.remaining.clear()
        retarget_end(engine, job["end"])
        stopper.set(job["saves"])
        storage = SQLiteStoragePlugin(database)
        relay.attach(storage, start_time=job["start"], end_time=job["end"])
        storage.append_extension_data("training_material", "run_metadata",
                                      _metadata(job, plan, contract, provenance, request["batch_fingerprint"]),
                                      job["start"])
        saved = {}
        try:
            while True:
                try:
                    engine.advance()
                    break
                except StopAtFork as stop:
                    path = checkpoint_path(out, job, stop.time)
                    saved[path.relative_to(out).as_posix()] = save_checkpoint(
                        path, (engine, relay, controller, stopper))
            engine.finish_episode()
        finally:
            if storage._worker is not None and storage._worker.is_alive():
                storage.finish_run(POLICY_NAME)
        checked = validate_database(database, job_contract(contract, job), job, policy_check=policy_row_ok)
        checked["checkpoints"] = saved
        checked["hot_groups_log"] = controller.log[:: 4 * 7]  # 每周一条，便于检查
        return checked

    out = Path(request["plan"]["output_dir"])
    return _run_logged(out / "logs" / f"{job['id']}.attempt{job['attempt']}.log", body)


# ---------------------------------------------------------------- 调度


def run(prepared: dict, workers: int, resume: bool) -> dict:
    """进程池里同时放预热任务和分支任务；每完成一个就提交新就绪的任务。"""

    directory = Path(prepared["plan"]["output_dir"])
    path = directory / "manifest.json"
    with batch_lock(directory):
        if path.exists():
            if not resume:
                raise ValueError(f"Batch exists: {path}; use --resume")
            manifest = read_json(path)
            if manifest["batch_fingerprint"] != prepared["batch_fingerprint"]:
                raise ValueError("Existing batch has a different plan, model or source")
        else:
            manifest = prepared
        plan = manifest["plan"]
        all_jobs = manifest["jobs"] + manifest["eval_jobs"]
        by_id = {j["id"]: j for j in all_jobs}
        warm_by_seed = {w["seed"]: w for w in manifest["warm"]}
        for j in all_jobs:
            if j["status"] != "complete":
                j.update(status="pending", error=None)

        def save():
            manifest["updated_at"] = timestamp()
            write_json(path, manifest)

        def source(job: dict) -> Path | None:
            """出发点检查点；还没就绪时返回 None。"""
            if job["parent"] is None:
                item = warm_by_seed[job["seed"]]
                return warm_paths(plan, item)[0] if warm_ready(plan, item) else None
            parent = by_id[job["parent"]]
            return checkpoint_path(directory, parent, job["start"]) if parent["status"] == "complete" else None

        common = {"plan": plan, "contract": manifest["contract"], "provenance": manifest["provenance"],
                  "batch_fingerprint": manifest["batch_fingerprint"]}
        pending: dict = {}
        warming: set[int] = set()
        failed_warm: set[int] = set()
        with ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn")) as pool:
            while True:
                for job in all_jobs:
                    if job["status"] != "pending":
                        continue
                    if job["parent"] is not None and by_id[job["parent"]]["status"] == "failed":
                        job.update(status="failed", error=f"parent {job['parent']} failed")
                        continue
                    src = source(job)
                    if src is None:
                        if job["parent"] is None and job["seed"] in failed_warm:
                            job.update(status="failed", error="warm-up failed")
                        elif job["parent"] is None and job["seed"] not in warming:
                            warming.add(job["seed"])
                            item = warm_by_seed[job["seed"]]
                            pending[pool.submit(warm_task, {**common, "warm": item})] = ("warm", item["seed"])
                        continue
                    attempt = job.get("attempt", 0) + 1
                    stem = Path(job["database"]).with_suffix("").as_posix().split(".attempt")[0]
                    # 失败重跑换新文件名，保留旧文件便于排查。
                    job.update(attempt=attempt, status="running",
                               database=f"{stem}.sqlite" if attempt == 1 else f"{stem}.attempt{attempt}.sqlite")
                    pending[pool.submit(branch_task, {**common, "job": job, "source": str(src)})] = ("job", job["id"])
                save()
                if not pending:
                    break
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    kind, key = pending.pop(future)
                    outcome = future.result()
                    if kind == "warm":
                        warming.discard(key)
                        if outcome["status"] != "complete":
                            failed_warm.add(key)
                        print(f"warm seed {key}: {outcome['status']} ({outcome.get('wall_seconds', 0):.0f}s) "
                              f"{outcome.get('error', '')}", flush=True)
                        continue
                    job = by_id[key]
                    if outcome["status"] == "complete":
                        result = outcome["result"]
                        result["wall_seconds"] = outcome["wall_seconds"]
                        job.update(status="complete", result=result, completed_at=timestamp())
                    else:
                        job.update(status="failed", error=outcome.get("error"), log=outcome.get("log"))
                    print(f"{key}: {job['status']} ({outcome.get('wall_seconds', 0):.0f}s) "
                          f"{outcome.get('error') or ''}", flush=True)
        if all(j["status"] == "complete" for j in all_jobs) and not plan.get("keep_checkpoints", False):
            # 中途检查点只给子分支用；全部完成后删掉（预热检查点保留，可跨计划复用）。
            for j in all_jobs:
                for relative in j["result"].get("checkpoints", {}):
                    (directory / relative).unlink(missing_ok=True)
        save()
    return manifest


def estimate(plan: dict, seconds_per_day: float, provenance: dict | None = None) -> str:
    instances = expand_tree(plan)
    seeds = {name: plan["seed_splits"][name] for name in SPLITS}
    total_seeds = sum(len(v) for v in seeds.values())
    reused = 0
    if provenance is not None:
        reused = sum(warm_ready(plan, {"seed": s, "key": warm_key(plan, s, provenance)})
                     for v in seeds.values() for s in v)
    branch_days, trajectories, evals, snapshots = 0.0, 0, 0, 0
    interval = plan["tokenizer"]["sample_interval_minutes"]
    for i in instances:
        n = sum(len(seeds[s]) for s in i["splits"])
        branch_days += n * (i["end"] - i["start"]) / DAY
        if i["eval"]:
            evals += n
        else:
            trajectories += n
            snapshots += n * (round((i["end"] - i["start"]) / interval) + 1)
    warm_days = (total_seeds - reused) * plan["simulation"]["warmup_days"]
    days = warm_days + branch_days
    return (f"{total_seeds} seeds x {len(instances)} instances -> {trajectories} training trajectories "
            f"({snapshots} snapshots) + {evals} eval copies\n"
            f"simulated days: warm-up {warm_days:.0f} ({reused} seeds reused from cache) + branches "
            f"{branch_days:.0f} = {days:.0f}\nCPU-h at {seconds_per_day} s/day: {days * seconds_per_day / 3600:.1f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--workers", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="only preflight and print the cost estimate")
    parser.add_argument("--seconds-per-day", type=float, default=3.5)
    args = parser.parse_args()
    plan = load_plan(args.plan, args.output_dir)
    prepared = prepare(plan)
    print(estimate(plan, args.seconds_per_day, prepared["provenance"]))
    contract = prepared["contract"]
    print(f"token shape {contract['shape']}, policy vocab {contract['policy_vocabulary']}")
    if args.dry_run:
        return
    run(prepared, args.workers or plan["workers"], args.resume)


if __name__ == "__main__":
    main()
