"""Plan or execute independent DES collection jobs in bounded spawned processes."""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from contextlib import redirect_stderr, redirect_stdout
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time
import traceback

from fabotron.data_collection.progress import ProgressDisplay, render_progress, watch_progress

from fabotron.data_collection.common import (
    DEFAULT_PLAN, batch_lock, collection_sources, file_hash, load_plan,
    prepare_collection, read_json, timestamp, validate_database, write_json,
)


_STOP = None


def initialize_worker(stop):
    global _STOP
    _STOP = stop
    # Parent handles Ctrl+C; children leave through the simulator's progress callback.
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def collect_one(request):
    """Each child owns its engine, strategy, SQLite writer and output files."""
    from fab.engine import FabEngine
    from fab.model.sqlite_loader import load_fab_model_from_sqlite
    from fab.plugins.plugin_manager import PluginManager
    from fab.plugins.results import OnlineMetricsPlugin
    from fab.plugins.sqlite_storage import SQLiteStoragePlugin
    from fab.plugins.tokenizer import PERSISTENCE_BASELINE, TokenizerPlugin
    from fab.plugins.tokenizer_core import TokenizerConfig
    from fab.run_model import build_plugin, build_strategy

    job, plan, contract = request["job"], request["plan"], request["contract"]
    database = Path(plan["output_dir"]) / job["database"]
    progress_path = database.with_suffix(".progress.json")
    log_path = database.with_suffix(".log")
    storage = None
    began = time.perf_counter()
    database.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("x", encoding="utf-8", buffering=1) as log, redirect_stdout(log), redirect_stderr(log):
        try:
            if database.exists():
                raise FileExistsError(database)
            if _STOP is not None and _STOP.is_set():
                raise InterruptedError("Collection cancelled before starting")
            if (file_hash(plan["model_database"]) != request["provenance"]["model_sha256"]
                    or collection_sources() != request["provenance"]["source_sha256"]):
                raise ValueError("Model database or collection/runtime source changed after preflight")
            model = load_fab_model_from_sqlite(Path(plan["model_database"]))
            strategy = build_strategy(job["strategy"]["factory"], job["strategy"].get("parameters"))
            if strategy.name != job["strategy"]["policy"]:
                raise ValueError("Strategy name differs from the fixed policy vocabulary")
            options = dict(plan["tokenizer"])
            interval = options.pop("sample_interval_minutes")
            vocabulary = contract["schema"]["dispatch_rules"]
            tokenizer = TokenizerPlugin(
                TokenizerConfig(dispatch_rules=vocabulary, **options),
                dispatch_policy=[float(name == strategy.name) for name in vocabulary],
                start_time=contract["capture_start_minutes"], sample_interval_minutes=interval,
                forecast_mode=PERSISTENCE_BASELINE,
            )
            storage = SQLiteStoragePlugin(database)
            # Summary metrics and daily records share the existing SQLite storage service.
            plugins = [storage, tokenizer, OnlineMetricsPlugin()]
            if plan.get("daily_metrics_plugin"):
                plugins.append(build_plugin(plan["daily_metrics_plugin"]))

            def progress(elapsed, total):
                if _STOP is not None and _STOP.is_set():
                    raise InterruptedError("Collection cancelled by parent")
                info = {"job_id": job["id"], "seed": job["seed"], "strategy_id": job["strategy"]["id"],
                        "split": job["split"], "status": "running", "elapsed_days": elapsed / 1440,
                        "simulation_days": total / 1440, "fraction": elapsed / total if total else 0,
                        "snapshots": tokenizer._snapshot_count, "updated_at": timestamp(),
                        "phase": "warmup" if elapsed < model.simulation.warmup_time else "collecting",
                        "wall_seconds": time.perf_counter() - began}
                write_json(progress_path, info)
                print(f"day={elapsed / 1440:.2f} snapshots={tokenizer._snapshot_count}", flush=True)

            engine = FabEngine(model, order_seed=job["seed"], plugin_manager=PluginManager(plugins),
                               progress_callback=progress)
            for plugin in plugins:
                bind = getattr(plugin, "bind_engine", None)
                if callable(bind):
                    bind(engine)
            metadata = {"seed": job["seed"], "split": job["split"], "strategy_id": job["strategy"]["id"],
                        "strategy_name": strategy.name, "strategy_factory": job["strategy"]["factory"],
                        "strategy_parameters": job["strategy"].get("parameters", {}),
                        "batch_fingerprint": request["batch_fingerprint"], "attempt": job["attempt"],
                        "started_at": timestamp(), "python": sys.version,
                        "simulation": plan["expected_simulation"], "tokenizer": plan["tokenizer"],
                        "policy_vocabulary": vocabulary, "schema_id": contract["schema_id"],
                        "model_sha256": request["provenance"]["model_sha256"],
                        "source_sha256": request["provenance"]["source_sha256"],
                        "capture_start_minutes": contract["capture_start_minutes"],
                        "capture_end_minutes": contract["capture_end_minutes"],
                        "daily_metrics_enabled": contract["daily_metrics_enabled"],
                        "raw_event_logs_enabled": False, "steady_state_certified": False}
            engine.begin_episode(strategy)
            storage.append_extension_data("training_material", "run_metadata", metadata, model.simulation.start_time)
            progress(0, model.simulation.end_time - model.simulation.start_time)
            while engine.advance_until_release() is not None:
                engine.apply_release_action(None)
            simulation_result = engine.finish_episode()
            write_json(progress_path, {"job_id": job["id"], "status": "running", "phase": "verifying",
                                       "fraction": 1.0, "elapsed_days": (model.simulation.end_time-model.simulation.start_time)/1440,
                                       "snapshots": tokenizer._snapshot_count, "updated_at": timestamp()})
            result = validate_database(database, contract, job)
            if (file_hash(plan["model_database"]) != request["provenance"]["model_sha256"]
                    or collection_sources() != request["provenance"]["source_sha256"]):
                raise ValueError("Model database or runtime source changed during collection")
            result.update({"simulation_result": simulation_result, "wall_seconds": time.perf_counter() - began,
                           "completed_at": timestamp()})
            write_json(progress_path, {"job_id": job["id"], "status": "complete", "phase": "complete", "fraction": 1,
                                       "snapshots": result["snapshots"], "updated_at": timestamp()})
            return {"status": "complete", "result": result}
        except BaseException as error:
            traceback.print_exc()
            status = "cancelled" if isinstance(error, (InterruptedError, KeyboardInterrupt)) else "failed"
            write_json(progress_path, {"job_id": job["id"], "status": status,
                                       "error": str(error), "updated_at": timestamp()})
            return {"status": status, "error": f"{type(error).__name__}: {error}",
                    "log": str(log_path), "wall_seconds": time.perf_counter() - began}
        finally:
            # On a simulation exception the engine does not reach its normal plugin finalizers.
            if storage is not None and storage._worker is not None and storage._worker.is_alive():
                storage.finish_run(job["strategy"]["policy"])


def select_jobs(manifest, strategies=None, seeds=None, limit=None):
    jobs = manifest["jobs"]
    for requested, available, label in (
        (strategies, {job["strategy"]["id"] for job in jobs}, "strategies"),
        (seeds, {job["seed"] for job in jobs}, "seeds"),
    ):
        if requested and set(requested) - available:
            raise ValueError(f"Unknown {label}: {set(requested) - available}")
    selected = [job for job in jobs if (not strategies or job["strategy"]["id"] in strategies)
                and (not seeds or job["seed"] in seeds)]
    return selected[:limit] if limit is not None else selected


def next_attempt(job, directory):
    """Preserve interrupted/failed artifacts; retries always get a fresh filename."""
    attempt = job["attempt"] + 1
    parent = Path(job["split"]) / job["strategy"]["id"]
    while True:
        suffix = "" if attempt == 1 else f".attempt{attempt}"
        relative = parent / f"seed_{job['seed']}{suffix}.sqlite"
        database = directory / relative
        if not any(p.exists() for p in (database, database.with_suffix(".log"), database.with_suffix(".progress.json"))):
            break
        attempt += 1
    job.update({"attempt": attempt, "database": relative.as_posix(), "status": "running",
                "started_at": timestamp()})
    job.pop("error", None)
    job.pop("result", None)


def run_collection(prepared, *, workers=2, resume=False, strategies=None, seeds=None, limit=None,
                   worker=collect_one, progress_mode="auto"):
    if prepared["preflight_errors"]:
        raise ValueError("Preflight blocked:\n" + "\n".join(prepared["preflight_errors"]))
    if workers < 1:
        raise ValueError("workers must be positive")
    directory = Path(prepared["plan"]["output_dir"])
    path = directory / "manifest.json"
    stop_path = directory / ".stop_requested.json"
    display = ProgressDisplay(progress_mode)
    with batch_lock(directory):
        if path.exists():
            if not resume:
                raise ValueError(f"Batch already exists: {path}. Use --resume to continue.")
            manifest = read_json(path)
            if manifest.get("batch_fingerprint") != prepared["batch_fingerprint"]:
                raise ValueError("Existing batch has a different plan, model database or runtime source")
        else:
            manifest = prepared
        selected = select_jobs(manifest, strategies, seeds, limit)
        queue = []
        for job in selected:
            if job["status"] == "complete":
                database = directory / job["database"]
                if not database.is_file() or file_hash(database) != job["result"]["database_sha256"]:
                    raise ValueError(f"Completed database is missing or changed: {database}")
            else:
                queue.append(job)
        now = timestamp()
        manifest["controller"] = {"pid": os.getpid(), "status": "running", "workers": workers,
                                  "started_at": now, "heartbeat_at": now}
        stop_path.unlink(missing_ok=True)
        write_json(path, manifest)
        if not queue:
            manifest["controller"]["status"] = "complete"
            write_json(path, manifest)
            display.update(manifest, selected, force=True)
            return manifest
        context = multiprocessing.get_context("spawn")
        stop = context.Event()
        executor = ProcessPoolExecutor(max_workers=min(workers, len(queue)), mp_context=context,
                                       initializer=initialize_worker, initargs=(stop,))
        active = {}
        interrupted = False
        requested_stop = False
        last_heartbeat = time.monotonic()

        def save():
            manifest["updated_at"] = timestamp()
            manifest["controller"]["heartbeat_at"] = manifest["updated_at"]
            write_json(path, manifest)

        def submit():
            job = queue.pop(0)
            next_attempt(job, directory)
            save()
            request = {"job": dict(job), "plan": manifest["plan"], "contract": manifest["contract"],
                       "provenance": manifest["provenance"], "batch_fingerprint": manifest["batch_fingerprint"]}
            active[executor.submit(worker, request)] = job
            display.event(f"Started {job['id']} [{job['split']}] attempt={job['attempt']}")

        try:
            while queue and len(active) < workers:
                submit()
            display.update(manifest, selected, force=True)
            while active:
                if stop_path.exists() and not requested_stop:
                    requested_stop = True
                    manifest["controller"]["status"] = "stopping"
                    stop.set()
                    display.event("Stop requested. Waiting for active simulators to close safely...")
                    save()
                if time.monotonic() - last_heartbeat >= 5:
                    save()
                    last_heartbeat = time.monotonic()
                display.update(manifest, selected)
                done, _ = wait(active, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    job = active.pop(future)
                    try:
                        job.update(future.result())
                    except Exception as error:
                        job.update({"status": "failed", "error": f"Worker process failed: {error}"})
                    job["finished_at"] = timestamp()
                    save()
                    display.event(f"{job['status'].upper()}: {job['id']} {job.get('error', '')}")
                    if queue and not requested_stop:
                        submit()
                    display.update(manifest, selected, force=True)
        except BaseException:
            # Cooperative cancellation avoids waiting for all 480 simulated days on Ctrl+C.
            interrupted = True
            stop.set()
            manifest["controller"]["status"] = "stopping"
            for future in active:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            if interrupted:
                for future, job in active.items():
                    try:
                        job.update(future.result())
                    except BaseException:
                        job.update({"status": "cancelled", "error": "Parent interrupted; safe to resume"})
            manifest["controller"]["status"] = (
                "stopped" if interrupted or requested_stop else
                "finished_with_errors" if any(job["status"] in ("failed", "cancelled") for job in selected)
                else "complete"
            )
            save()
            display.update(manifest, selected, force=True)
        return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "run", "status", "stop"))
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--workers", type=int, help="Independent simulator processes; default is 2")
    parser.add_argument("--strategies", nargs="+", help="Subset of configured strategy IDs")
    parser.add_argument("--seeds", nargs="+", type=int, help="Subset of configured simulation seeds")
    parser.add_argument("--limit", type=int, help="Run only the first N selected jobs for a pilot")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--progress", choices=("auto", "live", "plain", "off"), default="auto",
                        help="auto redraws progress in a terminal; redirected logs use periodic plain snapshots")
    parser.add_argument("--watch", action="store_true", help="Continuously display status without running a simulator")
    args = parser.parse_args(argv)
    if (args.workers is not None and args.workers < 1) or (args.limit is not None and args.limit < 1):
        parser.error("workers and limit must be positive")
    if args.watch and args.command != "status":
        parser.error("--watch is only supported with status")
    try:
        plan = load_plan(args.plan, args.output_dir)
        manifest_path = Path(plan["output_dir"]) / "manifest.json"
        if args.command == "stop":
            manifest = read_json(manifest_path)
            if manifest.get("controller", {}).get("status") not in ("running", "stopping"):
                raise ValueError("No active collector is registered for this batch")
            write_json(manifest_path.parent / ".stop_requested.json", {"requested_at": timestamp()})
            print("Stop requested. The collector will close active simulators at their next progress callback.")
            return 0
        if args.command == "status":
            if args.watch:
                watch_progress(manifest_path, mode=args.progress)
                return 0
            manifest = read_json(manifest_path)
            print("\n".join(render_progress(manifest)))
        else:
            manifest = prepare_collection(plan)
            selected = select_jobs(manifest, args.strategies, args.seeds, args.limit)
            print(f"Schema: {manifest['contract']['shape']}; snapshots/run: {manifest['contract']['snapshots']}")
            print(f"Jobs: {len(manifest['jobs'])}; selected: {len(selected)}; "
                  f"workers: {args.workers or plan['workers']}")
            print("Daily metrics: " + (f"enabled ({manifest['contract']['daily_metrics']['records']} records/run)"
                                      if plan.get("daily_metrics_plugin") else "disabled"))
            if manifest["preflight_errors"]:
                for error in manifest["preflight_errors"]:
                    print("BLOCKED: " + error)
                return 2
            if args.command == "plan":
                for job in selected:
                    print(f"{job['split']:10} {job['id']}: {job['database']}")
                print("Preflight passed. No files written and no simulation started.")
                return 0
            manifest = run_collection(manifest, workers=args.workers or plan["workers"], resume=args.resume,
                                      strategies=args.strategies, seeds=args.seeds, limit=args.limit,
                                      progress_mode=args.progress)
        counts = {}
        for job in manifest["jobs"]:
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        print("Job status: " + str(counts))
        print("Manifest: " + str(Path(plan["output_dir"]) / "manifest.json"))
        return int(any(job["status"] in ("failed", "cancelled") for job in manifest["jobs"]))
    except KeyboardInterrupt:
        if args.command == "run":
            print("Interrupted. Partial databases are preserved; use --resume.", file=sys.stderr)
        else:
            print("Status monitor closed; the collector continues independently.", file=sys.stderr)
        return 130
    except (ValueError, OSError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
