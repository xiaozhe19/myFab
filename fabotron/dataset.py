"""Read Tokenizer SQLite snapshots and build non-overlapping chronological splits."""

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


TOKEN_BLOCKS = {"W", "P_WIP", "Q", "P_Queue", "C", "M", "B", "policy"}


def validate_schema(schema):
    groups = schema["group_ids"]
    if not groups or len(groups) != len(set(groups)):
        raise ValueError("Schema must contain unique tool groups")
    if set(schema["slices"]) != TOKEN_BLOCKS:
        raise ValueError("Unsupported Tokenizer feature blocks")
    offset = 0
    for start, end in sorted(schema["slices"].values()):
        if start != offset or end <= start:
            raise ValueError("Schema slices must cover all features exactly once")
        offset = end
    if offset != schema["input_dim"]:
        raise ValueError("Schema slices do not match input_dim")


@dataclass
class TokenTrajectory:
    database: Path
    run_id: int
    schema_id: str
    schema: dict
    tokens: torch.Tensor
    times: np.ndarray
    interval_minutes: float

    @property
    def name(self):
        return f"{self.database.stem}/run_{self.run_id}"

    def manifest(self):
        return {
            "name": self.name, "database": str(self.database), "run_id": self.run_id,
            "schema_id": self.schema_id, "snapshots": len(self.tokens),
            "shape": list(self.tokens.shape), "first_time_minutes": float(self.times[0]),
            "last_time_minutes": float(self.times[-1]), "sample_interval_minutes": self.interval_minutes,
        }


def load_trajectories(databases, *, run_id=None, max_snapshots=None):
    """Stream JSON into float32 buffers, keeping each database/run separate."""
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
                count = connection.execute(
                    """SELECT count(*) FROM plugin_records WHERE run_id=?
                       AND plugin_name='tokenizer' AND record_type='snapshot'""", (current_run,),
                ).fetchone()[0]
                if max_snapshots is not None:
                    count = min(count, max_snapshots)
                if count < 2:
                    raise ValueError(f"{path}/run_{current_run}: fewer than two snapshots")
                values = np.empty((count, len(schema["group_ids"]), schema["input_dim"]), dtype=np.float32)
                times = np.empty(count, dtype=np.float64)
                snapshots = connection.execute(
                    """SELECT event_time, payload_json FROM plugin_records WHERE run_id=?
                       AND plugin_name='tokenizer' AND record_type='snapshot'
                       ORDER BY event_time, record_id LIMIT ?""", (current_run, count),
                )
                loaded = 0
                for loaded, (event_time, raw) in enumerate(snapshots, start=1):
                    snapshot = json.loads(raw)
                    if snapshot["schema_id"] != record["schema_id"] or snapshot["group_ids"] != schema["group_ids"]:
                        raise ValueError(f"{path}: snapshot schema/group order changed")
                    matrix = np.asarray(snapshot["matrix"], dtype=np.float32)
                    if matrix.shape != values.shape[1:] or not np.isfinite(matrix).all():
                        raise ValueError(f"{path}: invalid snapshot matrix")
                    for name in ("W", "Q"):
                        start, end = schema["slices"][name]
                        if (matrix[..., start:end] < 0).any():
                            raise ValueError(f"{path}: negative target count in {name}")
                    timestamp = float(snapshot["time"])
                    if not math.isfinite(timestamp) or event_time is None or abs(timestamp - event_time) > 1e-6:
                        raise ValueError(f"{path}: time does not match event_time")
                    values[loaded - 1], times[loaded - 1] = matrix, timestamp
                if loaded != count:
                    raise ValueError(f"{path}: snapshot count changed while reading")
                if not np.allclose(np.diff(times), interval, atol=1e-6, rtol=0):
                    raise ValueError(f"{path}: missing, duplicated, or irregular snapshot times")
                trajectories.append(TokenTrajectory(
                    path, current_run, record["schema_id"], schema, torch.from_numpy(values), times, interval,
                ))
                print(f"Loaded {path.name}/run_{current_run}: {values.shape}", flush=True)
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
    """An item is (history, next complete token matrix, trajectory index)."""

    def __init__(self, trajectories, segments, history_len):
        if history_len < 1:
            raise ValueError("history_len must be positive")
        if len(segments) != len(trajectories):
            raise ValueError("Each trajectory needs its own split segment")
        self.trajectories, self.history_len = trajectories, history_len
        self.samples, self.segments = [], []
        for index, (start, stop) in enumerate(segments):
            if not 0 <= start < stop <= len(trajectories[index].tokens):
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
        tokens = self.trajectories[trajectory_index].tokens
        target_index = start + self.history_len
        return tokens[start:target_index], tokens[target_index], trajectory_index


def make_datasets(trajectories, history_len=8, train_fraction=0.7, val_fraction=0.15):
    """Split snapshots before windowing; no raw snapshot is shared between splits."""
    if not (0 < train_fraction < 1 and 0 < val_fraction < 1 and train_fraction + val_fraction < 1):
        raise ValueError("Train/validation fractions must be positive and sum to less than one")
    segments = {name: [] for name in ("train", "validation", "test")}
    for trajectory in trajectories:
        total = len(trajectory.tokens)
        train_stop = int(total * train_fraction)
        val_stop = train_stop + int(total * val_fraction)
        for name, bounds in zip(segments, [(0, train_stop), (train_stop, val_stop), (val_stop, total)]):
            segments[name].append(bounds)
    return {name: TokenSequenceDataset(trajectories, bounds, history_len) for name, bounds in segments.items()}
