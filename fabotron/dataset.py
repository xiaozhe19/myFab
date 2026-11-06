"""Read Tokenizer V2 SQLite snapshots and build non-overlapping splits.

每条轨迹在内存里保留两张紧凑的表：工步表 ``step_counts`` [帧, 926, 7]（int16）
和组表 ``group_features`` [帧, 106, 19]（float32）。样本直接给这两张表，
W、Q、格子、设备个数等都由模型 / 目标编码按 ``fabotron.layout`` 现算。
旧的 V1（801 维 JSON）数据不再支持。
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import numpy as np
import torch
from torch.utils.data import Dataset

from fabotron.layout import V2
from fabotron.token_view import decode_snapshot


def validate_schema(schema):
    if schema.get("version") != V2:
        raise ValueError(f"Only {V2} data is supported; recollect older V1 data")
    groups = schema["group_ids"]
    if not groups or len(groups) != len(set(groups)):
        raise ValueError("Schema must contain unique tool groups")
    if (schema["step_counts"]["shape"] != [len(schema["steps"]), len(schema["stages"])]
            or schema["group_features"]["shape"][0] != len(groups)):
        raise ValueError("V2 table shapes do not match steps/stages/groups")
    if "Ready" not in schema["stages"] or "C" not in schema["group_features"]["slices"]:
        raise ValueError("Schema needs a Ready stage and a C block")


@dataclass
class TokenTrajectory:
    database: Path
    run_id: int
    schema_id: str
    schema: dict
    step_counts: torch.Tensor        # [N, steps, stages] int16
    group_features: torch.Tensor     # [N, groups, features] float32
    times: np.ndarray
    interval_minutes: float
    simulation_seed: int | None = None
    strategy_id: str | None = None

    def __len__(self):
        return len(self.step_counts)

    @property
    def name(self):
        if self.strategy_id is not None:
            return f"{self.strategy_id}/seed_{self.simulation_seed}/run_{self.run_id}"
        return f"{self.database.stem}/run_{self.run_id}"

    def manifest(self):
        return {
            "name": self.name, "database": str(self.database), "run_id": self.run_id,
            "schema_id": self.schema_id, "schema_version": self.schema["version"], "snapshots": len(self),
            "step_counts_shape": list(self.step_counts.shape), "group_features_shape": list(self.group_features.shape),
            "first_time_minutes": float(self.times[0]), "last_time_minutes": float(self.times[-1]),
            "sample_interval_minutes": self.interval_minutes,
            "simulation_seed": self.simulation_seed, "strategy_id": self.strategy_id,
        }


def _load_run(connection, path, run, record, interval, max_snapshots):
    """解码每张快照的两张表，保持紧凑（工步表 int16，组表 float32）。"""
    schema = record["tokenizer_schema"]
    rows = connection.execute(
        """SELECT event_time, payload_json FROM plugin_records WHERE run_id=?
           AND plugin_name='tokenizer' AND record_type='snapshot'
           ORDER BY event_time, record_id""" + ("" if max_snapshots is None else " LIMIT ?"),
        (run,) if max_snapshots is None else (run, max_snapshots),
    ).fetchall()
    if len(rows) < 2:
        raise ValueError(f"{path}/run_{run}: fewer than two snapshots")
    steps, stages = schema["step_counts"]["shape"]
    groups, features = schema["group_features"]["shape"]
    counts = np.empty((len(rows), steps, stages), dtype=np.int16)
    values = np.empty((len(rows), groups, features), dtype=np.float32)
    times = np.empty(len(rows), dtype=np.float64)
    for index, (event_time, raw) in enumerate(rows):
        snapshot = json.loads(raw)
        if snapshot["schema_id"] != record["schema_id"]:
            raise ValueError(f"{path}: snapshot schema changed")
        step_counts, group_features = decode_snapshot(snapshot, schema)
        if step_counts.max(initial=0) > np.iinfo(np.int16).max or not np.isfinite(group_features).all():
            raise ValueError(f"{path}: snapshot values out of range")
        timestamp = float(snapshot["time"])
        if not math.isfinite(timestamp) or event_time is None or abs(timestamp - event_time) > 1e-6:
            raise ValueError(f"{path}: time does not match event_time")
        counts[index], values[index], times[index] = step_counts, group_features, timestamp
    if not np.allclose(np.diff(times), interval, atol=1e-6, rtol=0):
        raise ValueError(f"{path}: missing, duplicated, or irregular snapshot times")
    print(f"Loaded {path.name}/run_{run}: {len(rows)} x (steps {steps}x{stages}, groups {groups}x{features})", flush=True)
    return TokenTrajectory(path, run, record["schema_id"], schema, torch.from_numpy(counts),
                           torch.from_numpy(values), times, interval)


def load_trajectories(databases, *, run_id=None, max_snapshots=None):
    """Load each database/run separately."""
    if max_snapshots is not None and max_snapshots < 2:
        raise ValueError("max_snapshots must be at least 2")
    paths = [Path(path).resolve() for path in databases]
    if not paths or len(set(paths)) != len(paths):
        raise ValueError("Supply at least one database, without duplicate paths")
    trajectories = []
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(path)
        original_stat = path.stat()
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            query = """SELECT run_id, payload_json FROM plugin_records
                       WHERE plugin_name='tokenizer' AND record_type='schema'"""
            parameters = ()
            if run_id is not None:
                query += " AND run_id=?"
                parameters = (run_id,)
            rows = connection.execute(query + " ORDER BY run_id, record_id", parameters).fetchall()
            if not rows:
                raise ValueError(f"{path}: no matching Tokenizer schema records")
            seen_runs = set()
            for current_run, payload in rows:
                if current_run in seen_runs:
                    raise ValueError(f"{path}: multiple schemas for run_id={current_run}")
                seen_runs.add(current_run)
                record = json.loads(payload)
                schema = record["tokenizer_schema"]
                validate_schema(schema)
                canonical = json.dumps(schema, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != record["schema_id"]:
                    raise ValueError(f"{path}: schema_id does not match schema")
                interval = float(record["sample_interval_minutes"])
                if not math.isfinite(interval) or interval <= 0:
                    raise ValueError("Sampling interval must be finite and positive")
                trajectories.append(_load_run(connection, path, current_run, record, interval, max_snapshots))
        final_stat = path.stat()
        if (original_stat.st_size, original_stat.st_mtime_ns) != (final_stat.st_size, final_stat.st_mtime_ns):
            raise ValueError(f"{path}: database changed during reading; wait for capture to finish")
    first = trajectories[0]
    for trajectory in trajectories[1:]:
        if trajectory.schema_id != first.schema_id or trajectory.schema != first.schema:
            raise ValueError("All trajectories must use the same Tokenizer schema")
        if abs(trajectory.interval_minutes - first.interval_minutes) > 1e-6:
            raise ValueError("All trajectories must use the same sampling interval")
    return trajectories


class TokenSequenceDataset(Dataset):
    """一个样本 = (历史工步表 [H,S,K], 历史组表 [H,G,F], 下一帧工步表 [S,K], 下一帧组表 [G,F], 轨迹编号)。"""

    def __init__(self, trajectories, segments, history_len, *, trajectory_ids=None):
        if history_len < 1:
            raise ValueError("history_len must be positive")
        if len(segments) != len(trajectories):
            raise ValueError("Each trajectory needs its own split segment")
        self.trajectories, self.history_len = trajectories, history_len
        self.trajectory_ids = list(range(len(trajectories))) if trajectory_ids is None else list(trajectory_ids)
        if len(self.trajectory_ids) != len(trajectories) or len(set(self.trajectory_ids)) != len(trajectories):
            raise ValueError("Each trajectory needs a unique evaluation ID")
        self.samples, self.segments = [], []
        for index, (start, stop) in enumerate(segments):
            if not 0 <= start < stop <= len(trajectories[index]):
                raise ValueError("Invalid snapshot segment")
            count = stop - start - history_len
            if count <= 0:
                raise ValueError(f"{trajectories[index].name}: split has {stop-start} snapshots; needs {history_len+1}")
            self.samples.extend((index, first) for first in range(start, stop - history_len))
            self.segments.append({"trajectory": trajectories[index].name, "snapshot_start": start,
                                  "snapshot_stop_exclusive": stop, "samples": count})

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        trajectory_index, start = self.samples[index]
        trajectory = self.trajectories[trajectory_index]
        stop = start + self.history_len
        return (trajectory.step_counts[start:stop], trajectory.group_features[start:stop],
                trajectory.step_counts[stop], trajectory.group_features[stop],
                self.trajectory_ids[trajectory_index])


def make_datasets(trajectories, history_len=8, train_fraction=0.7, val_fraction=0.15):
    """Split snapshots before windowing; no raw snapshot is shared between splits."""
    if not (0 < train_fraction < 1 and 0 < val_fraction < 1 and train_fraction + val_fraction < 1):
        raise ValueError("Train/validation fractions must be positive and sum to less than one")
    segments = {name: [] for name in ("train", "validation", "test")}
    for trajectory in trajectories:
        total = len(trajectory)
        train_stop = int(total * train_fraction)
        val_stop = train_stop + int(total * val_fraction)
        for name, bounds in zip(segments, [(0, train_stop), (train_stop, val_stop), (val_stop, total)]):
            segments[name].append(bounds)
    return {name: TokenSequenceDataset(trajectories, bounds, history_len) for name, bounds in segments.items()}


def load_collection_datasets(manifest_path, history_len=8, max_snapshots=None):
    """Hold out whole seeds；数据紧凑，整份读进内存。每条轨迹按自己的起点核对时间。"""
    from fabotron.data_collection.common import canonical_hash, training_manifest

    manifest_path = Path(manifest_path).resolve()
    manifest, splits = training_manifest(manifest_path)
    contract = manifest["contract"]
    validate_schema(contract["schema"])
    if canonical_hash(contract["schema"]) != contract["schema_id"]:
        raise ValueError("Collection schema hash mismatch")
    if max_snapshots is not None and max_snapshots < 2:
        raise ValueError("max_snapshots must be at least 2")
    jobs = {(manifest_path.parent / job["database"]).resolve(): job for job in manifest["jobs"]}
    interval = contract["sample_interval_minutes"]
    trajectories, datasets = [], {}
    for split, inputs in splits.items():
        current = []
        for item in inputs:
            job = jobs[item["database"]]
            count = job["result"]["snapshots"] if max_snapshots is None else min(job["result"]["snapshots"], max_snapshots)
            trajectory = load_trajectories([item["database"]], run_id=item["run_id"], max_snapshots=max_snapshots)[0]
            start = job.get("capture_start_minutes", contract["capture_start_minutes"])
            if (trajectory.schema != contract["schema"] or len(trajectory) != count
                    or not np.allclose(trajectory.times, start + np.arange(count) * interval, rtol=0, atol=1e-6)):
                raise ValueError(f"{item['database']}: data differs from its collection manifest")
            trajectory.simulation_seed, trajectory.strategy_id = item["seed"], item["strategy_id"]
            current.append(trajectory)
        offset = len(trajectories)
        datasets[split] = TokenSequenceDataset(
            current, [(0, len(t)) for t in current], history_len,
            trajectory_ids=range(offset, offset + len(current)),
        )
        trajectories.extend(current)
    return trajectories, datasets
