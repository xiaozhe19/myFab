"""从已认证 checkpoint 续跑并记录训练素材；以脚本方式启动以隔离旧运行时。"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import heapq
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
DAY = 1440.0


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--strategy", required=True, choices=("FIFO", "SMT2020"))
    parser.add_argument("--duration-days", type=float, default=240)
    parser.add_argument("--sample-interval-minutes", type=float, default=360)
    parser.add_argument("--horizon-minutes", type=float, default=360)
    args = parser.parse_args()
    for name in ("duration_days", "sample_interval_minutes", "horizon_minutes"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(name + " must be finite and positive")
    args.database = args.database.resolve()
    args.checkpoint = args.checkpoint.resolve()
    args.runtime = args.runtime.resolve()
    if args.database.exists():
        raise FileExistsError(args.database)
    args.database.parent.mkdir(parents=True, exist_ok=True)
    progress_path = args.database.with_suffix(".progress.json")
    summary_path = args.database.with_suffix(".summary.json")
    metadata_path = args.database.with_suffix(".metadata.json")
    if any(p.exists() for p in (progress_path, summary_path, metadata_path)):
        raise FileExistsError("run metadata already exists")

    # 在导入任何 fab 模块前选择归档运行时；不修改其源码或恢复校验。
    sys.path[:0] = [str(args.runtime), str(ROOT)]
    from checkpoint_tools.runtime import load_checkpoint, source_hashes
    from checkpoint_tools.__main__ import find_certification
    from fab.engine.events import EventKind
    import fab.plugins as runtime_plugins
    plugin_path = str(ROOT / "fab/plugins")
    if plugin_path not in runtime_plugins.__path__:
        runtime_plugins.__path__.append(plugin_path)
    from fab.plugins.base import FabPlugin
    from fab.plugins.plugin_manager import PluginManager
    from fab.plugins.sqlite_storage import SQLiteStoragePlugin
    from fab.plugins.trace import CqtViolationPlugin, TracePlugin
    from fab.plugins.tokenizer import PERSISTENCE_BASELINE, TokenizerPlugin
    from fab.plugins.tokenizer_core import TokenizerConfig

    certification = find_certification(args.checkpoint)
    if not certification or not certification.get("certified"):
        raise ValueError("checkpoint has no completed steady-state certification")
    started = time.perf_counter()
    engine, manifest = load_checkpoint(args.checkpoint)
    start_time = engine.current_time
    end_time = engine._time(start_time + args.duration_days * DAY)
    if end_time >= engine.model.simulation.end_time:
        raise ValueError("requested run reaches the original checkpoint horizon")
    original_sources = source_hashes()
    initial_wip = sum(not lot.completed for lot in engine.lots)
    original_strategy = engine._strategy.name
    switch = {"changed": False}
    if args.strategy == "FIFO":
        from strategy.fifo import FIFOStrategy
        strategy = FIFOStrategy()
        strategy.initialize(engine.model)
        engine._strategy = strategy
        engine._release_on_arrival = False
        # 旧 SMT 决策尚未执行。FIFO 只在周期投料点准入，当前边界先执行派工。
        engine._pending = engine._decide(engine._state(False), "dispatch")
        interval = engine.model.simulation.release_interval
        origin = engine.model.simulation.start_time
        first_release = origin + (math.floor((start_time - origin) / interval) + 1) * interval
        engine.schedule(first_release, EventKind.LOT_RELEASE)
        before = len(engine._calendar)
        engine._calendar = [event for event in engine._calendar
                            if event.kind != EventKind.STRATEGY_WAKEUP]
        heapq.heapify(engine._calendar)
        switch = {"changed": True, "from": original_strategy, "to": strategy.name,
                  "first_periodic_release_time": first_release,
                  "release_interval_minutes": interval,
                  "removed_previous_strategy_wakeups": before - len(engine._calendar),
                  "initial_pending_decision": "FIFO dispatch only; wait for periodic release"}
    elif engine._strategy.name != "SMT2020_BASELINE":
        raise ValueError("SMT2020 continuation requires an SMT2020 checkpoint")

    # 两个分支共用特征定义，policy 使用固定两维词表，方便后续合并训练。
    config = TokenizerConfig((30, 60, 120), (.05, .10, .20),
                             ("FIFO", "SMT2020_BASELINE"), args.horizon_minutes, 100, 100)
    policy = (1., 0.) if args.strategy == "FIFO" else (0., 1.)
    recorder = TokenizerPlugin(config, policy, sample_interval_minutes=args.sample_interval_minutes,
                               forecast_mode=PERSISTENCE_BASELINE)
    storage = SQLiteStoragePlugin(args.database)

    class Progress(FabPlugin):
        def __init__(self):
            self.next_time = start_time + 5 * DAY

        def report(self, status="running"):
            info = {"status": status, "strategy": engine._strategy.name,
                    "start_day": start_time / DAY, "day": engine.current_time / DAY,
                    "end_day": end_time / DAY,
                    "elapsed_simulation_days": (engine.current_time - start_time) / DAY,
                    "snapshots": recorder._snapshot_count,
                    "wall_seconds": time.perf_counter() - started,
                    "database": str(args.database)}
            write_json(progress_path, info)
            print(json.dumps(info, ensure_ascii=False), flush=True)

        def on_time_advanced(self, current_time, lots):
            if current_time >= self.next_time:
                self.report()
                self.next_time += 5 * DAY

    progress = Progress()
    manager = PluginManager([storage, TracePlugin(), CqtViolationPlugin(), recorder, progress])
    engine._plugin_manager = manager
    recorder.bind_engine(engine, restored=True)
    metadata = {
        "checkpoint": str(args.checkpoint), "checkpoint_sha256": sha_file(args.checkpoint),
        "checkpoint_certified": True, "strict_restore_verified": True,
        "seed": manifest["seed"], "strategy": engine._strategy.name,
        "baseline_strategy": original_strategy, "strategy_switch": switch,
        "baseline_note": "FIFO branches from the SMT2020 steady-state checkpoint; FIFO steady state is not certified",
        "initial_wip": initial_wip, "initial_release_pool": len(engine.order_source.queue),
        "start_time_minutes": start_time, "end_time_minutes": end_time,
        "duration_days": args.duration_days, "start_day": start_time / DAY, "end_day": end_time / DAY,
        "original_horizon_days": engine.model.simulation.end_time / DAY,
        "sample_interval_minutes": args.sample_interval_minutes, "horizon_minutes": args.horizon_minutes,
        "policy_vocabulary": config.dispatch_rules, "policy": policy,
        "forecast_method": PERSISTENCE_BASELINE, "runtime": str(args.runtime),
        "python": sys.version, "source_hashes": original_sources,
        "collector_source_hashes": {str(path.relative_to(ROOT)): sha_file(path) for path in
                                    (Path(__file__), ROOT / "fab/plugins/tokenizer.py",
                                     ROOT / "fab/plugins/tokenizer_core.py")},
    }
    write_json(metadata_path, metadata)
    # 保留原 1080 日上限和预采样日历，只增加本次采集结束事件。
    engine.schedule(end_time, EventKind.SIMULATION_END)
    try:
        manager.on_simulation_started(engine.model)
        storage.append_extension_data("training_material", "run_metadata", metadata, start_time)
        progress.report()
        if engine._pending is not None:
            engine.apply_release_action(None)
        while engine.advance_until_release() is not None:
            engine.apply_release_action(None)
        result = engine.finish_episode()
        if source_hashes() != original_sources:
            raise RuntimeError("archived runtime source files changed")
        with closing(sqlite3.connect(args.database.as_uri() + "?mode=ro", uri=True)) as connection:
            times = [row[0] for row in connection.execute(
                "SELECT event_time FROM plugin_records WHERE plugin_name='tokenizer' "
                "AND record_type='snapshot' ORDER BY record_id")]
            expected = math.floor((end_time - start_time) / args.sample_interval_minutes + 1e-9) + 1
            if len(times) != expected or any(not math.isclose(
                t, start_time + i * args.sample_interval_minutes, rel_tol=0, abs_tol=1e-6)
                for i, t in enumerate(times)):
                raise RuntimeError("snapshot sampling grid is incomplete")
            first = json.loads(connection.execute(
                "SELECT payload_json FROM plugin_records WHERE plugin_name='tokenizer' "
                "AND record_type='snapshot' ORDER BY record_id LIMIT 1").fetchone()[0])
            counts = {name: connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                      for name in ("operations", "lot_events", "plugin_records", "runs")}
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError("database integrity check failed: " + integrity)
        summary = {**metadata, "status": "complete", "result": result, "row_counts": counts,
                   "snapshot_count": len(times), "snapshot_shape": [len(first["matrix"]), len(first["matrix"][0])],
                   "schema_id": first["schema_id"], "first_snapshot_time": times[0],
                   "last_snapshot_time": times[-1], "database_integrity": integrity,
                   "database_bytes": args.database.stat().st_size,
                   "wall_seconds": time.perf_counter() - started}
        write_json(summary_path, summary)
        progress.report("complete")
    except BaseException as error:
        if storage._worker is not None and storage._worker.is_alive():
            storage.finish_run(engine._strategy.name)
        write_json(summary_path, {**metadata, "status": "failed", "error": repr(error)})
        progress.report("failed")
        raise


if __name__ == "__main__":
    main()
