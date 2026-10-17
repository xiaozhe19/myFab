from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from fab.engine import FabEngine
from fab.model.sqlite_loader import _raw_workbook, _simulation
from fab.model.smt_model_builder import build_fab_model
from fab.plugins import PluginManager
from strategy.smt2020_baseline import SMT2020BaselineStrategy

from .analysis import assess_rows, summarize
from .runtime import (DAY, ROOT, MetricsObserver, advance_to, attach_observer,
                      fingerprint, inspect_checkpoint, load_checkpoint,
                      save_checkpoint, sha256, source_hashes, write_json)


def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)


def new_engine(database: Path, seed: int, observer=None, horizon_days=1080):
    database = database.resolve()
    with sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True) as c:
        c.row_factory = sqlite3.Row
        simulation, workbook = _simulation(c), _raw_workbook(c, database)
    model = build_fab_model(workbook, replace(simulation, start_time=0.0,
                           end_time=horizon_days * DAY, warmup_time=0.0), model_id=database.stem)
    engine = FabEngine(model, order_seed=seed,
                       plugin_manager=PluginManager([observer] if observer is not None else []))
    engine.begin_episode(SMT2020BaselineStrategy())
    return engine


def verify(args):
    expected = json.loads(args.expected.read_text(encoding="utf-8"))
    started = time.perf_counter()
    engine, manifest = load_checkpoint(args.checkpoint)
    loaded_seconds = time.perf_counter() - started
    start_equal = fingerprint(engine) == expected["start_fingerprint"]
    observer = attach_observer(engine)
    observer.start_probe()
    advance_to(engine, expected["until_minute"])
    trace = observer.finish_probe()
    after = fingerprint(engine)
    differing = [key for key in after if after[key] != expected["end_fingerprint"][key]]
    passed = start_equal and not differing and trace == expected["trace"]
    result = {
        "passed": passed, "checkpoint": str(args.checkpoint.resolve()),
        "payload_sha256": manifest["payload_sha256"],
        "start_state_equal": start_equal, "end_state_equal": not differing,
        "trace_equal": trace == expected["trace"], "trace": trace,
        "differing_sections": differing, "fingerprint_sections": len(after),
        "load_and_validation_seconds": loaded_seconds,
        "start_time_minute": manifest["time_minute"], "end_time_minute": engine.current_time,
        "measurement_start_wip": manifest["wip"], "end_wip": len(engine.lots),
        "complete_measurement_days": len(observer.rows),
        "metrics": summarize(observer.rows) if observer.rows else None,
        "wall_seconds": time.perf_counter() - started,
    }
    write_json(args.output, result)
    emit(result)
    if not passed:
        raise SystemExit(2)


def build(args):
    if args.evaluation_days < 360 or args.evaluation_days % 30:
        raise ValueError("Evaluation days must be a multiple of 30, at least 360.")
    if args.verify_days <= 0 or args.evaluation_days + args.verify_days >= 1080:
        raise ValueError("Invalid validation duration/horizon.")
    if any(day <= 0 or day > args.evaluation_days or day % 30 for day in args.checkpoint_days):
        raise ValueError("Checkpoint days must be evaluation milestones (multiples of 30).")
    if any(b - a <= args.verify_days for a, b in zip(sorted(args.checkpoint_days),
                                                    sorted(args.checkpoint_days)[1:])):
        raise ValueError("Checkpoint validation windows must not overlap the next capture.")
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    observer = MetricsObserver()
    frozen_sources = source_hashes()
    engine = new_engine(args.model_database, args.seed, observer)
    initial_empty = not engine.lots and not engine.order_source.queue
    captures = []
    for day in range(30, args.evaluation_days + 1, 30):
        advance_to(engine, day * DAY)
        emit({"progress": True, "seed": args.seed, "day": day,
              "actual_day": engine.current_time / DAY, "wip": len(engine.lots),
              "last_30_days": summarize(observer.rows[day - 30:day]),
              "wall_seconds": time.perf_counter() - started})
        if day not in args.checkpoint_days:
            continue
        path = args.output / f"day{day:04d}.fabcp"
        capture_started = time.perf_counter()
        manifest = save_checkpoint(path, engine, seed=args.seed, requested_day=day,
                                   model_database=args.model_database, expected_sources=frozen_sources)
        start_fingerprint = manifest["fingerprint"]
        observer.start_probe()
        advance_to(engine, engine.current_time + args.verify_days * DAY)
        expected = {"start_fingerprint": start_fingerprint,
                    "end_fingerprint": fingerprint(engine),
                    "until_minute": engine.current_time, "trace": observer.finish_probe()}
        expected_path = args.output / f"day{day:04d}_expected.json"
        result_path = args.output / f"day{day:04d}_verification.json"
        write_json(expected_path, expected)
        captures.append({"path": str(path.resolve()), "manifest": manifest,
                         "checkpoint_sha256": sha256(path.read_bytes()),
                         "checkpoint_bytes": path.stat().st_size,
                         "expected": str(expected_path.resolve()),
                         "verification": str(result_path.resolve())})
        emit({"captured": True, "seed": args.seed, "requested_day": day,
              "actual_day": manifest["actual_day"], "checkpoint": str(path),
              "bytes": path.stat().st_size,
              "capture_and_reference_seconds": time.perf_counter() - capture_started})
    for item in captures:
        command = [sys.executable, "-B", "-m", "checkpoint_tools", "verify",
                   "--checkpoint", item["path"], "--expected", item["expected"],
                   "--output", item["verification"]]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError(f"Fresh-process verification failed: {result.stdout}\n{result.stderr}")
        item["verification_result"] = json.loads(Path(item["verification"]).read_text(encoding="utf-8"))
        emit({"verified": True, "seed": args.seed, "day": item["manifest"]["requested_day"],
              "result": item["verification_result"]})
    rows = observer.rows[:args.evaluation_days]
    running_in = running_out = 0
    for row in rows:
        running_in += sum(row["ins"].values())
        running_out += sum(row["outs"].values())
        if running_in - running_out != row["end_wip"]:
            raise RuntimeError(f"WIP conservation failure on day {row['day']}")
        if any(len(row["ct"].get(k, [])) != n for k, n in row["outs"].items()):
            raise RuntimeError("Cycle-time sample count mismatch.")
    run = {"seed": args.seed, "initial_empty": initial_empty,
           "evaluation_days": args.evaluation_days, "horizon_days": 1080,
           "source_hashes": frozen_sources, "captures": captures, "rows": rows,
           "conservation_passed": True, "wall_seconds": time.perf_counter() - started}
    write_json(args.output / "run.json", run)
    emit({"done": True, "seed": args.seed, "run": str(args.output / "run.json"),
          "wall_seconds": run["wall_seconds"]})


def find_certification(checkpoint: Path):
    checkpoint = checkpoint.resolve()
    for directory in checkpoint.parents:
        index_path = directory / "index.json"
        if index_path.exists():
            index = json.loads(index_path.read_text(encoding="utf-8"))
            for item in index.get("checkpoints", []):
                if Path(item["path"]).resolve() == checkpoint:
                    if item["checkpoint_sha256"] != sha256(checkpoint.read_bytes()):
                        raise ValueError("Certified checkpoint file hash mismatch.")
                    return item
        if directory == ROOT:
            break
    return None


def resume(args):
    if args.output.exists():
        raise FileExistsError(args.output)
    certification = find_certification(args.checkpoint)
    if not args.allow_uncertified and not (certification and certification["certified"]):
        raise ValueError("No completed steady-state certification. Use a certified index/checkpoint.")
    if args.duration_days <= 0:
        raise ValueError("Duration must be positive.")
    started = time.perf_counter()
    engine, manifest = load_checkpoint(args.checkpoint)
    args.output.mkdir(parents=True, exist_ok=False)
    observer = attach_observer(engine)
    initial_wip = len(observer.wip)
    observer.start_probe()
    target = engine.current_time + args.duration_days * DAY
    advance_to(engine, target)
    trace = observer.finish_probe()
    result = {
        "checkpoint": str(args.checkpoint.resolve()), "seed": manifest["seed"],
        "certified": bool(certification and certification["certified"]),
        "start_time_minute": manifest["time_minute"], "end_time_minute": engine.current_time,
        "initial_wip": initial_wip, "end_wip": len(engine.lots),
        "release_pool_at_end": len(engine.order_source.queue),
        "requested_duration_days": args.duration_days, "complete_measurement_days": len(observer.rows),
        "metrics": summarize(observer.rows) if observer.rows else None,
        "trace": trace, "end_fingerprint": fingerprint(engine),
        "wall_seconds": time.perf_counter() - started,
    }
    write_json(args.output / "result.json", result)
    write_json(args.output / "daily_metrics.json", observer.rows)
    emit(result)


def assess(args):
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in args.runs]
    if len({run["seed"] for run in runs}) != len(runs):
        raise ValueError("Seeds must be distinct.")
    if len(runs) < 3:
        raise ValueError("At least three independent seeds are required for this certification.")
    if any(run["source_hashes"] != source_hashes() for run in runs):
        raise ValueError("Source fingerprints differ from the completed runs.")
    results = [{"seed": run["seed"], **assess_rows(run["rows"], args.tolerance)} for run in runs]
    accepted = all(r["candidate_day"] is not None and r["reference_stable"] for r in results)
    recommended = max(r["candidate_day"] for r in results) + 30 if accepted else None
    entries = []
    for run, result in zip(runs, results):
        for capture in run["captures"]:
            manifest = capture["manifest"]
            certified = bool(accepted and manifest["requested_day"] >= recommended
                             and capture["verification_result"]["passed"])
            entries.append({"seed": run["seed"], "path": capture["path"],
                            "requested_day": manifest["requested_day"],
                            "actual_day": manifest["actual_day"], "wip": manifest["wip"],
                            "bytes": capture["checkpoint_bytes"],
                            "checkpoint_sha256": capture["checkpoint_sha256"],
                            "certified": certified, "verification": capture["verification"]})
    defaults = {str(run["seed"]): min((e for e in entries if e["seed"] == run["seed"]
                                         and e["certified"]), key=lambda e: e["actual_day"])["path"]
                for run in runs if any(e["seed"] == run["seed"] and e["certified"] for e in entries)}
    index = {"accepted": accepted, "criterion_tolerance": args.tolerance,
             "p95_tolerance": args.tolerance * 2, "window_days": 90,
             "step_days": 30, "consecutive_windows": 3,
             "recommended_warmup_days": recommended, "results": results,
             "default_by_seed": defaults, "checkpoints": entries}
    write_json(args.output / "index.json", index)
    report = ["# SMT2020 稳态 checkpoint 实测与恢复报告", "",
              f"当前 HVLM、SMT2020_BASELINE，{len(runs)} 个独立种子，从空厂运行。", "",
              f"工程稳态容差：均值/投出差/90日趋势 ±{args.tolerance:.0%}，周期 P95 ±{args.tolerance*2:.0%}。",
              "90日滑动窗口、30日步长，连续3窗通过；后180日作参考，并检查其投出差与趋势。", "",
              f"推荐预热：**{recommended} 仿真日**。首次通过的窗口起点与完成确认的时刻分别列出。", "",
              "| 种子 | 稳态起点（日） | 确认完成（日） | 参考平均WIP | 参考完工/日 | 参考趋势/90日 |",
              "|---|---:|---:|---:|---:|---:|"]
    for r in results:
        report.append(f"| {r['seed']} | {r['candidate_day']} | {r['confirmed_day']} | "
                      f"{r['reference']['wip']:.3f} | {r['reference']['output_per_day']:.3f} | "
                      f"{r['reference']['drift_pct_per_90_days']:.3f}% |")
    report += ["", "## 已保存的完整状态", "",
               "checkpoint 保留绝对时钟，停在已生成、尚未应用的 release 决策边界；实际时刻略晚于整数目标日。",
               "保存业务对象完整引用图、事件堆、活动/暂停加工、lot/设备/来源/策略、RNG及事件序号；不保存线程或存储插件。", "",
               "| 种子 | 目标日 | 实际日 | WIP | 文件MiB | 已认证 | 文件 |",
               "|---|---:|---:|---:|---:|---|---|"]
    for e in entries:
        report.append(f"| {e['seed']} | {e['requested_day']} | {e['actual_day']:.8f} | {e['wip']} | "
                      f"{e['bytes']/1048576:.3f} | {e['certified']} | [{Path(e['path']).name}]({e['path']}) |")
    report += ["", "## 跨进程续跑验证", "",
               "每个 checkpoint 均由新 Python 进程读取，在相同未来时刻与未中断原进程比较。",
               "验证初态/末态44组语义状态摘要、两套RNG、未来事件堆和事件序号，以及期间每次投料、工序完成、整lot完成的顺序轨迹哈希。",
               "_entries_by_group 内部无业务意义的列表顺序规范化后比较；真正的全局候选顺序仍原样比较。", "",
               "| 种子 | 目标日 | 起点一致 | 末态一致 | 轨迹一致 | 工序完成数 | 完成lot数 | 续跑到（日） |",
               "|---|---:|---|---|---|---:|---:|---:|"]
    for run in runs:
        for c in run["captures"]:
            v = c["verification_result"]
            report.append(f"| {run['seed']} | {c['manifest']['requested_day']} | {v['start_state_equal']} | "
                          f"{v['end_state_equal']} | {v['trace_equal']} | {v['trace']['counts'].get('operation',0)} | "
                          f"{v['trace']['counts'].get('complete',0)} | {v['end_time_minute']/DAY:.8f} |")
    report += ["", "## 使用", "",
               "使用本次 CPython 和项目根目录，-B 避免写入既有字节码缓存；恢复命令不会重新预热或调用 begin_episode。", "",
               "```powershell",
               "$python = 'C:\\Users\\ZZY\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\python\\python.exe'",
               f"& $python -B -m checkpoint_tools inspect --checkpoint '{next(iter(defaults.values()), '')}'",
               f"& $python -B -m checkpoint_tools resume --checkpoint '{next(iter(defaults.values()), '')}' --duration-days 30 --output 'data/checkpoints/resumed_seed1700'",
               "```", "",
               "输出目录必须是新目录。恢复时为当前在制lot初始化新的WIP计量，不将它们当作空厂；CT统计为计量期完工lot的完整实际厂内周期。",
               "checkpoint 仍包含已采样的未来事件，保留原1080日上限；命令会拒绝超出该上限的续跑。", "",
               "## 适用范围与文件不变性", "",
               "只新增 checkpoint_tools 包和本轮输出；既有 fab/、strategy/、模型库及旧实验报告不需要改动。",
               "完整payload校验、代码/模型SHA-256和Python精确版本/平台校验均在加载时执行；旧进程id(step)性能缓存会清空后按需重算。",
               "只加载可信、自行生成的 checkpoint；pickle 是可执行数据。", "",
               "本认证是5%容差下、三个种子的工程稳态估计，不是严格平稳性或其他策略稳态的证明。",
               "同一checkpoint重复加载会重现同一随机轨迹；独立重复请使用不同seed的默认checkpoint。",
               "完整指标、逐日数据、每个滑动窗口和验证结果分别保存在 index.json、各seed/run.json和 *_verification.json。", ""]
    with args.report.open("x", encoding="utf-8") as f:
        f.write("\n".join(report))
    emit({"done": True, "accepted": accepted, "recommended_days": recommended,
          "index": str(args.output / "index.json"), "report": str(args.report)})
    if not accepted or len(defaults) != len(runs):
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(description="Additive SMT2020 checkpoint capture, assessment and resume.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("build")
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--model-database", type=Path, default=ROOT / "data/model/fab_model.sqlite")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--evaluation-days", type=int, default=360)
    p.add_argument("--checkpoint-days", type=int, nargs="+", default=[240, 360])
    p.add_argument("--verify-days", type=float, default=2)
    p.set_defaults(func=build)
    p = sub.add_parser("verify")
    p.add_argument("--checkpoint", type=Path, required=True)
    p.add_argument("--expected", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.set_defaults(func=verify)
    p = sub.add_parser("resume")
    p.add_argument("--checkpoint", type=Path, required=True)
    p.add_argument("--duration-days", type=float, default=30)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--allow-uncertified", action="store_true")
    p.set_defaults(func=resume)
    p = sub.add_parser("inspect")
    p.add_argument("--checkpoint", type=Path, required=True)
    p.set_defaults(func=lambda a: emit({"manifest": inspect_checkpoint(a.checkpoint),
                                        "certification": find_certification(a.checkpoint)}))
    p = sub.add_parser("assess")
    p.add_argument("--runs", type=Path, nargs="+", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--tolerance", type=float, default=0.05)
    p.set_defaults(func=assess)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
