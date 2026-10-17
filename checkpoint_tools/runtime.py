from __future__ import annotations

import dataclasses
import enum
import hashlib
import itertools
import json
import os
import pickle
import platform
import sys
import warnings
import zipfile
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from fab.engine import FabEngine
from fab.model.entities import LotState, ToolState
from fab.plugins import FabPlugin, PluginManager

ROOT = Path(__file__).resolve().parents[1]
DAY = 1440.0
FORMAT = "myfab-checkpoint-v1"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_hashes() -> dict[str, str]:
    return {
        str(p.relative_to(ROOT)).replace("\\", "/"): sha256(p.read_bytes())
        for directory in ("fab", "strategy", "checkpoint_tools")
        for p in sorted((ROOT / directory).rglob("*.py"))
    }


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def sequence_arguments(engine: FabEngine) -> tuple:
    # Serialize the numeric counter position, never the itertools object itself.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return engine._sequence.__reduce__()[1]


def normal(value):
    if isinstance(value, LotState):
        return ["LotRef", value.id]
    if isinstance(value, ToolState):
        return ["ToolRef", value.tool_id]
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value):
        return [type(value).__name__, [(f.name, normal(getattr(value, f.name)))
                                      for f in dataclasses.fields(value)]]
    if isinstance(value, dict):
        return [[normal(k), normal(v)] for k, v in sorted(value.items(), key=lambda x: repr(x[0]))]
    if isinstance(value, (list, tuple, deque)):
        return [normal(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((normal(v) for v in value), key=repr)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported fingerprint value: {type(value)!r}")


def digest(value) -> str:
    return sha256(json.dumps(normal(value), separators=(",", ":")).encode())


def lot_row(lot):
    return {f.name: getattr(lot, f.name) for f in dataclasses.fields(lot) if f.name != "route"}


def fingerprint(engine: FabEngine) -> dict[str, str]:
    fields = (
        "_calendar", "_tokens", "_active", "_active_by_tool", "_paused",
        "_scheduled_tool_available", "_pending_downtimes", "_pending_pms",
        "_last_operation_completion", "_pending", "_dispatch_needed", "_done",
        "_next_counter_pm", "_lot_order", "_admitted_count", "_ready_lots_by_group",
        "_ready_group_by_lot", "_free_ready_by_group", "_dedicated_ready_by_group",
        "_ready_pile_by_lot", "_ready_batch_class_by_lot", "_idle_tools_by_group",
        "_changed_lot_ids", "_candidate_groups_dirty", "_order_dirty_groups",
        "_dispatchable_candidates_cache", "_eligible_lots_by_group", "_entries_by_group",
        "_completed_pending", "_pending_batches",
    )
    parts = {name: getattr(engine, name) for name in fields}
    # This cache's per-group list order is immaterial: the kernel globally sorts it.
    parts["_entries_by_group"] = {
        g: sorted(rows, key=lambda row: (row[0], row[1]))
        for g, rows in engine._entries_by_group.items()
    }
    parts.update(
        time=engine.current_time,
        lots=[lot_row(lot) for lot in engine.lots],
        tools=[dataclasses.asdict(tool) for tool in engine.tool_states.values()],
        rng=engine.rng.getstate(), source_rng=engine.order_source.rng.getstate(),
        source_queue=[lot_row(lot) for lot in engine.order_source.queue],
        source_counts=(engine.order_source._release_counts, engine.order_source._generated_count),
        sequence=sequence_arguments(engine),
    )
    for name in ("_reservations", "_blocked_since", "_blocked_wakeup", "_now", "_wakeups"):
        parts["strategy" + name] = getattr(engine._strategy, name)
    parts["strategy_super_hot"] = [lot_row(lot) for lot in engine._strategy._super_hot.values()]
    return {name: digest(value) for name, value in parts.items()}


def aliases_valid(engine: FabEngine) -> bool:
    active = [lot for a in engine._active.values() for lot in a.get("batch_lots", [])]
    ready = [lot for lots in engine._ready_lots_by_group.values() for lot in lots]
    return all(engine._lots_by_id.get(lot.id) is lot for lot in active + ready)


def save_checkpoint(path: Path, engine: FabEngine, *, seed: int, requested_day: float,
                    model_database: Path, expected_sources: dict[str, str]) -> dict:
    if type(engine) is not FabEngine or engine._done or engine._pending is None:
        raise ValueError("Checkpoint requires an unfinished native engine at a pending-release boundary.")
    if source_hashes() != expected_sources:
        raise ValueError("Source files changed during the run.")
    state = engine.__dict__.copy()
    for name in ("_sequence", "_plugin_manager", "_progress_callback"):
        state.pop(name)
    payload = pickle.dumps({"engine_state": state, "sequence": sequence_arguments(engine)}, protocol=5)
    model_database = model_database.resolve()
    try:
        model_name = str(model_database.relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        model_name = str(model_database)
    manifest = {
        "format": FORMAT, "created_utc": datetime.now(timezone.utc).isoformat(),
        "python": list(sys.version_info[:3]), "implementation": sys.implementation.name,
        "system": platform.system(), "sources": expected_sources,
        "model_database": model_name, "model_sha256": sha256(model_database.read_bytes()),
        "seed": seed, "strategy": engine._strategy.name, "requested_day": requested_day,
        "time_minute": engine.current_time, "actual_day": engine.current_time / DAY,
        "horizon_minute": engine.model.simulation.end_time, "stage": "release_decision_pending",
        "wip": len(engine.lots), "release_pool": len(engine.order_source.queue),
        "calendar_events": len(engine._calendar), "active_activities": len(engine._active),
        "paused_tools": len(engine._paused), "payload_bytes": len(payload),
        "payload_sha256": sha256(payload), "fingerprint": fingerprint(engine),
        "certification": "See the completed experiment index.json; capture alone is not certification.",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    if path.exists() or partial.exists():
        raise FileExistsError(path)
    with partial.open("xb") as f:
        with zipfile.ZipFile(f, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
            z.writestr("engine.pkl", payload)
        f.flush()
        os.fsync(f.fileno())
    # Windows rename refuses to overwrite an existing destination.
    os.rename(partial, path)
    return manifest


def inspect_checkpoint(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read("manifest.json"))


def load_checkpoint(path: Path) -> tuple[FabEngine, dict]:
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
        if manifest["format"] != FORMAT:
            raise ValueError("Unsupported checkpoint format.")
        if (manifest["python"] != list(sys.version_info[:3])
                or manifest["implementation"] != sys.implementation.name
                or manifest["system"] != platform.system()):
            raise ValueError("Checkpoint runtime version/platform mismatch.")
        if manifest["sources"] != source_hashes():
            raise ValueError("Checkpoint source fingerprint mismatch.")
        model_path = ROOT / manifest["model_database"]
        if sha256(model_path.read_bytes()) != manifest["model_sha256"]:
            raise ValueError("Checkpoint model fingerprint mismatch.")
        payload = z.read("engine.pkl")
        if len(payload) != manifest["payload_bytes"] or sha256(payload) != manifest["payload_sha256"]:
            raise ValueError("Checkpoint payload integrity failure.")
    # Only load trusted, locally generated checkpoints. Pickle is executable data.
    saved = pickle.loads(payload)
    engine = FabEngine.__new__(FabEngine)
    engine.__dict__.update(saved["engine_state"])
    engine._sequence = itertools.count(*saved["sequence"])
    engine._plugin_manager = PluginManager()
    engine._progress_callback = None
    engine._strategy._expected_setup_cache.clear()  # Keys contain old process object IDs.
    if not aliases_valid(engine) or fingerprint(engine) != manifest["fingerprint"]:
        raise ValueError("Restored state/reference fingerprint mismatch.")
    return engine, manifest


def advance_to(engine: FabEngine, target: float) -> None:
    """Stop at the first safe pending-release boundary at or after target."""
    if target <= engine.current_time:
        return
    if target >= engine.model.simulation.end_time:
        raise ValueError("Requested continuation reaches/exceeds the saved simulation horizon.")
    if engine._pending is not None:
        engine.apply_release_action(None)
    while engine.current_time < target:
        if engine.advance_until_release() is None:
            raise RuntimeError("Simulation ended before the requested pause boundary.")
        if engine.current_time < target:
            engine.apply_release_action(None)


class MetricsObserver(FabPlugin):
    def __init__(self):
        self.start = self.last = 0.0
        self.area = 0.0
        self.rows = []
        self.wip = set()
        self.ins = Counter()
        self.outs = Counter()
        self.ct = defaultdict(list)
        self.probe = None
        self.probe_counts = Counter()

    def on_simulation_started(self, model):
        self.start = self.last = model.simulation.start_time

    def initialize_restored(self, engine):
        self.start = self.last = engine.current_time
        self.wip = {lot.id for lot in engine.lots if not lot.completed}

    def on_time_advanced(self, current_time, lots):
        while current_time >= self.start + (len(self.rows) + 1) * DAY:
            end = self.start + (len(self.rows) + 1) * DAY
            self.area += len(self.wip) * (end - self.last)
            self.last = end
            self.rows.append({"day": len(self.rows) + 1, "wip": self.area / DAY,
                              "end_wip": len(self.wip), "ins": dict(self.ins),
                              "outs": dict(self.outs), "ct": dict(self.ct)})
            self.area = 0.0
            self.ins = Counter()
            self.outs = Counter()
            self.ct = defaultdict(list)
        self.area += len(self.wip) * (current_time - self.last)
        self.last = current_time

    def _trace(self, value):
        if self.probe is not None:
            self.probe.update(repr(value).encode())
            self.probe.update(b"\n")
            self.probe_counts[value[0]] += 1

    def on_lot_released(self, current_time, lot):
        self.wip.add(lot.id)
        self.ins[lot.lot_type_id] += 1
        self._trace(("release", current_time, lot.id, lot.product_id))

    def on_lot_completed(self, current_time, lot):
        self.wip.discard(lot.id)
        self.outs[lot.lot_type_id] += 1
        self.ct[lot.lot_type_id].append((current_time - lot.release_time) / DAY)
        self._trace(("complete", current_time, lot.id, lot.product_id))

    def on_operation_completed(self, lot, tool, start, end, active_duration):
        if self.probe is not None:
            self._trace(("operation", lot.id, tool.tool_id, lot.operation_index,
                         start, end, active_duration))

    def start_probe(self):
        self.probe = hashlib.sha256()
        self.probe_counts = Counter()

    def finish_probe(self):
        value = {"sha256": self.probe.hexdigest(), "counts": dict(self.probe_counts)}
        self.probe = None
        return value


def attach_observer(engine: FabEngine) -> MetricsObserver:
    observer = MetricsObserver()
    observer.initialize_restored(engine)
    engine._plugin_manager = PluginManager([observer])
    return observer
