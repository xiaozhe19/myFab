"""Collection plans and dataset checks; importing this module never runs DES."""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLAN = Path(__file__).with_name("collection_plan.json")
MANIFEST_VERSION = "fabotron-collection-v1"
SPLITS = ("train", "validation", "test")


def timestamp():
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                         encoding="utf-8")
    # Windows can briefly deny replacement while another terminal reads progress.
    for attempt in range(6):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.01 * (attempt + 1))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def project_path(value):
    path = Path(value)
    return (path if path.is_absolute() else ROOT / path).resolve()


def load_plan(path=DEFAULT_PLAN, output_dir=None):
    plan = read_json(path)
    if plan.get("version") != 1:
        raise ValueError("Unsupported collection plan version")
    plan["model_database"] = str(project_path(plan["model_database"]))
    plan["output_dir"] = str(project_path(output_dir or plan["output_dir"]))
    splits = plan["seed_splits"]
    if set(splits) != set(SPLITS) or any(not splits[name] for name in SPLITS):
        raise ValueError("Provide nonempty train, validation and test seed lists")
    seeds = [seed for name in SPLITS for seed in splits[name]]
    if any(type(seed) is not int or seed < 0 for seed in seeds) or len(seeds) != len(set(seeds)):
        raise ValueError("Simulation seeds must be nonnegative integers, unique across splits")
    strategies = plan["strategies"]
    if not strategies:
        raise ValueError("No strategies configured")
    ids = [item["id"] for item in strategies]
    policies = [item["policy"] for item in strategies]
    if (len(ids) != len(set(ids)) or len(policies) != len(set(policies))
            or any(not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in ids)):
        raise ValueError("Strategy IDs and policy names must be unique; IDs must be safe directory names")
    expected = plan["expected_simulation"]
    if set(expected) != {"start_time", "end_time", "warmup_time"}:
        raise ValueError("Expected simulation must specify start_time, end_time and warmup_time")
    if (not all(type(v) in (int, float) and math.isfinite(v) for v in expected.values())
            or expected["warmup_time"] < 0
            or expected["end_time"] <= expected["start_time"] + expected["warmup_time"]):
        raise ValueError("Invalid expected simulation times (minutes)")
    if type(plan["workers"]) is not int or plan["workers"] < 1:
        raise ValueError("workers must be a positive integer")
    if plan.get("daily_metrics_plugin") is not None and not isinstance(plan["daily_metrics_plugin"], str):
        raise ValueError("daily_metrics_plugin must be null or a MODULE:CLASS plugin specification")
    return plan


def collection_sources():
    paths = [*sorted((ROOT / "fab").rglob("*.py")), *sorted((ROOT / "strategy").rglob("*.py")),
             Path(__file__), Path(__file__).with_name("collect.py")]
    return {path.relative_to(ROOT).as_posix(): file_hash(path) for path in paths}


def prepare_collection(plan):
    """Read configuration and instantiate factories, without advancing the simulator."""
    from fab.model.sqlite_loader import load_fab_model_from_sqlite
    from fab.plugins.tokenizer_core import TokenizerConfig, ToolGroupTokenizer
    from fab.run_model import build_plugin, build_strategy

    database = Path(plan["model_database"])
    if not database.is_file():
        raise FileNotFoundError(database)
    before = file_hash(database)
    model = load_fab_model_from_sqlite(database)
    actual = {key: getattr(model.simulation, key) for key in plan["expected_simulation"]}
    errors = [f"simulation_config.{key}={actual[key]}; expected {value} minutes. Update the model database first."
              for key, value in plan["expected_simulation"].items()
              if not math.isclose(actual[key], value, abs_tol=1e-6, rel_tol=0)]
    policies = [item["policy"] for item in plan["strategies"]]
    for item in plan["strategies"]:
        strategy = build_strategy(item["factory"], item.get("parameters"))
        if strategy.name != item["policy"] or not callable(getattr(strategy, "initialize", None)):
            raise ValueError(f"{item['id']}: strategy name/interface differs from the plan")
    daily_plugin = build_plugin(plan["daily_metrics_plugin"]) if plan.get("daily_metrics_plugin") else None
    options = dict(plan["tokenizer"])
    interval = float(options.pop("sample_interval_minutes"))
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("sample_interval_minutes must be positive")
    encoder = ToolGroupTokenizer(model, TokenizerConfig(dispatch_rules=policies, **options))
    schema = json.loads(json.dumps(encoder.schema()))
    expected = plan["expected_simulation"]
    start = expected["start_time"] + expected["warmup_time"]
    steps = (expected["end_time"] - start) / interval
    if not math.isclose(steps, round(steps), abs_tol=1e-8, rel_tol=0) or steps < 1:
        raise ValueError("Collection duration must contain a whole number of sampling intervals")
    contract = {"schema": schema, "schema_id": encoder.schema_id, "capture_start_minutes": start,
                "capture_end_minutes": expected["end_time"], "sample_interval_minutes": interval,
                "snapshots": round(steps) + 1, "shape": encoder.shape,
                "daily_metrics_enabled": bool(plan.get("daily_metrics_plugin")),
                "raw_event_logs_enabled": False}
    if daily_plugin is not None:
        day_minutes = float(daily_plugin.day_minutes)
        if not math.isfinite(day_minutes) or day_minutes <= 0:
            raise ValueError("The daily plugin must define a positive day_minutes")
        contract["daily_metrics"] = {
            "day_minutes": day_minutes, "start_time": expected["start_time"],
            "end_time": expected["end_time"], "measurement_start_time": start,
            "records": math.ceil((expected["end_time"] - expected["start_time"]) / day_minutes),
        }
    if file_hash(database) != before:
        raise ValueError("Model database changed during preflight")
    provenance = {"model_sha256": before, "source_sha256": collection_sources()}
    fingerprint = canonical_hash({"plan": {k: v for k, v in plan.items() if k != "workers"},
                                  "contract": contract, "provenance": provenance})
    jobs = []
    for split in SPLITS:
        for seed in plan["seed_splits"][split]:
            for item in plan["strategies"]:
                jobs.append({"id": f"{item['id']}_seed{seed}", "strategy": item, "seed": seed,
                             "split": split, "status": "pending", "attempt": 0,
                             "database": f"{split}/{item['id']}/seed_{seed}.sqlite"})
    now = timestamp()
    return {"version": MANIFEST_VERSION, "created_at": now, "updated_at": now,
            "batch_fingerprint": fingerprint, "plan": plan, "contract": contract,
            "provenance": provenance, "jobs": jobs, "preflight_errors": errors}


@contextmanager
def batch_lock(directory):
    """OS-managed lock: a terminated parent does not leave a stale locked batch."""
    path = Path(directory) / ".collector.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise ValueError(f"Another collector is using {directory}") from None
    try:
        yield
    finally:
        stream.seek(0)
        if os.name == "nt":
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def validate_daily_metrics(connection, contract):
    """Verify complete daily coverage and balance, not merely an enabled flag."""
    records = connection.execute(
        "SELECT event_time,payload_json FROM plugin_records WHERE run_id=1 "
        "AND plugin_name='daily_metrics' AND record_type='daily_summary' ORDER BY event_time,record_id"
    ).fetchall()
    if not contract["daily_metrics_enabled"]:
        if records:
            raise ValueError("Daily records found while the daily plugin is disabled")
        return {"records": 0}
    daily = contract["daily_metrics"]
    if len(records) != daily["records"]:
        raise ValueError(f"Incomplete daily summaries: {len(records)}, expected {daily['records']}")
    schemas = connection.execute(
        "SELECT payload_json FROM plugin_records WHERE run_id=1 "
        "AND plugin_name='daily_metrics' AND record_type='schema'"
    ).fetchall()
    if len(schemas) != 1:
        raise ValueError("Expected one daily-metrics schema")
    metadata = json.loads(schemas[0][0])
    if metadata["version"] != 1 or any(not math.isclose(metadata[key], daily[key], rel_tol=0, abs_tol=1e-6)
                                       for key in ("day_minutes", "start_time", "end_time", "measurement_start_time")):
        raise ValueError("Daily-metrics schema differs from the collection contract")
    result = {"records": len(records), "warmup_records": 0, "collection_records": 0, "mixed_records": 0}
    previous = None
    for index, (event_time, raw) in enumerate(records):
        record = json.loads(raw)
        start = daily["start_time"] + index * daily["day_minutes"]
        end = min(start + daily["day_minutes"], daily["end_time"])
        duration = end - start
        if (record["day"] != daily.get("first_day", 1) + index or event_time is None
                or not math.isclose(event_time, end, abs_tol=1e-6, rel_tol=0)
                or any(not math.isclose(record[key], value, abs_tol=1e-6, rel_tol=0)
                       for key, value in (("start_time", start), ("end_time", end), ("duration_minutes", duration)))):
            raise ValueError("Daily summary timestamps contain a gap, duplicate or invalid boundary")
        if record["end_inclusive"] != (index == len(records) - 1):
            raise ValueError("Invalid final-day endpoint convention")
        if record["partial_day"] != (not math.isclose(duration, daily["day_minutes"])):
            raise ValueError("Invalid partial-day flag")
        phase = ("warmup" if end <= daily["measurement_start_time"] else
                 "collection" if start >= daily["measurement_start_time"] else "mixed")
        if record["phase"] != phase:
            raise ValueError("Daily warmup/collection phase mismatch")
        result[f"{phase}_records"] += 1
        integer_fields = ("wip_start", "wip_end", "queue_start", "queue_end", "released", "completed")
        if any(type(record[key]) is not int or record[key] < 0 for key in integer_fields):
            raise ValueError("Daily inventory and flow counts must be nonnegative integers")
        if record["wip_end"] != record["wip_start"] + record["released"] - record["completed"]:
            raise ValueError("Daily WIP conservation failed")
        if previous is not None and any(record[f"{name}_start"] != previous[f"{name}_end"] for name in ("wip", "queue")):
            raise ValueError("Daily inventories are discontinuous")
        if (any(not math.isfinite(record[key]) or record[key] < 0 for key in ("average_wip", "average_queue"))
                or record["average_queue"] > record["average_wip"] + 1e-8
                or record["queue_start"] > record["wip_start"] or record["queue_end"] > record["wip_end"]):
            raise ValueError("Invalid time-weighted WIP/Queue values")
        for key in ("mean_fab_cycle_time_minutes", "p95_fab_cycle_time_minutes", "mean_end_to_end_cycle_time_minutes"):
            value = record[key]
            if ((record["completed"] == 0 and value is not None)
                    or (record["completed"] > 0 and (value is None or not math.isfinite(value) or value < 0))):
                raise ValueError("Invalid daily completed-Lot cycle times")
        previous = record
    return result


def validate_database(database, contract, job, policy_check=None):
    """Check every snapshot after its writer has finished; no model inference.

    ``policy_check(row_policy) -> bool`` replaces the fixed one-hot check for runs whose
    per-group policy changes over time (branch collection).
    """
    database = Path(database).resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    from fab.plugins.tokenizer_core import unpack_array

    schema = contract["schema"]
    steps, stages = contract["shape"]["step_counts"]
    groups, features = contract["shape"]["group_features"]
    start = contract["capture_start_minutes"]
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite quick_check failed")
        runs = connection.execute("SELECT run_id,strategy FROM runs").fetchall()
        if runs != [(1, job["strategy"]["policy"])]:
            raise ValueError("Expected one complete run with the configured strategy")
        records = connection.execute("SELECT payload_json FROM plugin_records WHERE run_id=1 "
                                     "AND plugin_name='tokenizer' AND record_type='schema'").fetchall()
        if len(records) != 1:
            raise ValueError("Expected exactly one Tokenizer schema")
        record = json.loads(records[0][0])
        if record["schema_id"] != contract["schema_id"] or record["tokenizer_schema"] != schema:
            raise ValueError("Snapshot schema differs from the common collection schema")
        if canonical_hash(schema) != contract["schema_id"]:
            raise ValueError("Schema hash mismatch")
        count = connection.execute("SELECT count(*) FROM plugin_records WHERE run_id=1 "
                                   "AND plugin_name='tokenizer' AND record_type='snapshot'").fetchone()[0]
        if count != contract["snapshots"]:
            raise ValueError(f"Incomplete sampling grid: {count} snapshots, expected {contract['snapshots']}")
        policy_start, policy_stop = schema["group_features"]["slices"]["policy"]
        policy = [float(name == job["strategy"]["policy"]) for name in schema["dispatch_rules"]]
        for index, (event_time, payload) in enumerate(connection.execute(
                "SELECT event_time,payload_json FROM plugin_records WHERE run_id=1 "
                "AND plugin_name='tokenizer' AND record_type='snapshot' ORDER BY event_time,record_id")):
            snapshot = json.loads(payload)
            expected_time = start + index * contract["sample_interval_minutes"]
            if (event_time is None or not math.isclose(event_time, expected_time, abs_tol=1e-6, rel_tol=0)
                    or not math.isclose(snapshot["time"], expected_time, abs_tol=1e-6, rel_tol=0)):
                raise ValueError(f"Invalid sampling timestamp at index {index}")
            if snapshot["schema_id"] != contract["schema_id"]:
                raise ValueError("Schema changed within a run")
            # 解包本身就检查了长度；计数是 uint16，天然非负。
            unpack_array(snapshot["step_counts"], "H", steps * stages)
            values = unpack_array(snapshot["group_features"], "f", groups * features)
            for g in range(groups):
                row = values[g * features:(g + 1) * features]
                if any(not math.isfinite(value) for value in row):
                    raise ValueError("Non-finite group features")
                row_policy = list(row[policy_start:policy_stop])
                if (any(not math.isclose(a, b, abs_tol=1e-6) for a, b in zip(row_policy, policy))
                        if policy_check is None else not policy_check(row_policy)):
                    raise ValueError("Incorrect strategy one-hot encoding")
        raw_counts = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("operations", "lot_events")}
        if any(raw_counts.values()):
            raise ValueError("Detailed event logging was unexpectedly enabled")
        metadata = connection.execute("SELECT payload_json FROM plugin_records WHERE run_id=1 "
                                      "AND plugin_name='training_material' AND record_type='run_metadata'").fetchall()
        if len(metadata) != 1:
            raise ValueError("Missing collection metadata")
        metadata = json.loads(metadata[0][0])
        if any(metadata[key] != job[key] for key in ("seed", "split")) or metadata["strategy_id"] != job["strategy"]["id"]:
            raise ValueError("Seed, split or strategy metadata mismatch")
        daily_result = validate_daily_metrics(connection, contract)
    return {"run_id": 1, "snapshots": count, "shape": contract["shape"], "sqlite_quick_check": "ok",
            "database_bytes": database.stat().st_size, "database_sha256": file_hash(database),
            "raw_event_rows": raw_counts, "daily_metrics_enabled": contract["daily_metrics_enabled"],
            "daily_metrics": daily_result}


def training_manifest(path):
    """Resolve complete collection jobs into seed-disjoint training inputs."""
    path = Path(path).resolve()
    manifest = read_json(path)
    if manifest.get("version") != MANIFEST_VERSION:
        raise ValueError("Unsupported collection manifest")
    expected = {(item["id"], seed, split)
                for split in SPLITS for seed in manifest["plan"]["seed_splits"][split]
                for item in manifest["plan"]["strategies"]}
    actual = [(job["strategy"]["id"], job["seed"], job["split"]) for job in manifest["jobs"]]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("Manifest jobs do not match the complete strategy/seed plan")
    seed_sets = [set(manifest["plan"]["seed_splits"][name]) for name in SPLITS]
    if any(not seeds for seeds in seed_sets):
        raise ValueError("Collection requires nonempty train, validation and test seed groups")
    if any(seed_sets[i] & seed_sets[j] for i in range(3) for j in range(i)):
        raise ValueError("Simulation seed appears in more than one split")
    result = {name: [] for name in SPLITS}
    seen_databases = set()
    for job in manifest["jobs"]:
        if job["status"] != "complete":
            raise ValueError(f"Collection is incomplete: {job['id']} is {job['status']}")
        expected_snapshots = job.get("snapshots", manifest["contract"].get("snapshots"))
        if (job["result"]["run_id"] != 1 or job["result"]["snapshots"] != expected_snapshots
                or job["result"]["shape"] != manifest["contract"]["shape"]):
            raise ValueError(f"Inconsistent completion result: {job['id']}")
        database = (path.parent / job["database"]).resolve()
        if not database.is_relative_to(path.parent):
            raise ValueError("Dataset path escapes the collection directory")
        if database in seen_databases:
            raise ValueError("One database is assigned to multiple trajectories or splits")
        seen_databases.add(database)
        if not database.is_file() or file_hash(database) != job["result"]["database_sha256"]:
            raise ValueError(f"Missing or changed collected database: {database}")
        result[job["split"]].append({"database": database, "run_id": job["result"]["run_id"],
                                     "seed": job["seed"], "strategy_id": job["strategy"]["id"]})
    return manifest, result
