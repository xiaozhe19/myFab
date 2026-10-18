"""Tool Group Token V1 的特征计算，由 TokenizerPlugin 调用。

Recipe defaults to product_id (the product's processing recipe). Re-entry is
the static route visit ordinal of the exact tool_group_id, not process/area.
Only current activities, paused activities, frozen batches and ready indexes
are inspected. The event calendar and all random generators are untouched.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Mapping, Sequence

from fab.engine.eligibility import tool_is_available
from fab.engine.engine import FabEngine
from fab.engine.events import EventKind
from fab.model.entities import FabModel, LotState


def _positive(value: float, name: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def _edges(values: Sequence[float], name: str, upper: float = math.inf) -> tuple[float, ...]:
    result = tuple(float(x) for x in values)
    if any(not math.isfinite(x) or not 0 < x < upper for x in result):
        raise ValueError(f"{name} must contain finite interior edges in (0, {upper})")
    if any(a >= b for a, b in zip(result, result[1:])):
        raise ValueError(f"{name} must be strictly increasing")
    return result


def _probabilities(values: Sequence[float], size: int, name: str) -> tuple[float, ...]:
    result = tuple(float(x) for x in values)
    if len(result) != size or any(not math.isfinite(x) or not 0 <= x <= 1 for x in result):
        raise ValueError(f"{name}: expected {size} probabilities in [0, 1]")
    if not math.isclose(math.fsum(result), 1.0, rel_tol=0, abs_tol=1e-6):
        raise ValueError(f"{name}: probabilities must sum to 1")
    return result


@dataclass(frozen=True)
class TokenizerConfig:
    """Explicit experiment settings; no bins/scales are fitted at runtime.

    Edges are interior boundaries: (30, 60, 120) gives [0,30), [30,60),
    [60,120), [120,+inf). Counts are divided by fixed positive scales without
    clipping. Equipment counts use static per-group tool/slot capacities.
    """

    processing_edges_minutes: tuple[float, ...]
    downtime_ratio_edges: tuple[float, ...]
    dispatch_rules: tuple[str, ...]
    horizon_minutes: float
    wip_scale: float
    queue_scale: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "processing_edges_minutes", _edges(
            self.processing_edges_minutes, "processing_edges_minutes"))
        object.__setattr__(self, "downtime_ratio_edges", _edges(
            self.downtime_ratio_edges, "downtime_ratio_edges", 1.0))
        rules = tuple(self.dispatch_rules)
        if not rules or any(not isinstance(x, str) or not x for x in rules) or len(set(rules)) != len(rules):
            raise ValueError("dispatch_rules must contain unique nonempty names")
        object.__setattr__(self, "dispatch_rules", rules)
        for name in ("horizon_minutes", "wip_scale", "queue_scale"):
            _positive(getattr(self, name), name)


@dataclass(frozen=True)
class DowntimeForecast:
    """Caller-supplied marginals for the SAME [window_start, start+horizon).

    as_of is the last observation time used to produce these probabilities.
    The caller is responsible for causal provenance; timestamps alone cannot
    prove absence of future leakage. PM and breakdown are separate marginals,
    with no independence assumption. Both mappings must cover every group.
    """

    as_of: float
    window_start: float
    horizon_minutes: float
    ratio_edges: tuple[float, ...]
    pm: Mapping[str, Sequence[float]]
    breakdown: Mapping[str, Sequence[float]]
    method: str


@dataclass(frozen=True)
class GroupCounts:
    """Unnormalized counts, for auditing and lossless count recovery."""

    wip: int
    queue: int
    total_tools: int
    available_tools: int
    occupied_slots: int
    down_tools: int


@dataclass(frozen=True)
class TokenizedFab:
    time: float
    horizon_minutes: float
    group_ids: tuple[str, ...]
    matrix: tuple[tuple[float, ...], ...]
    counts: tuple[GroupCounts, ...]
    schema_id: str
    forecast_method: str

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.group_ids), len(self.matrix[0])

    def as_numpy(self):
        """Optional NumPy bridge; core tokenizer has no third-party dependency."""
        import numpy as np

        return np.asarray(self.matrix, dtype=np.float32)


class ToolGroupTokenizer:
    """Encode engine.current_time using frozen static category indexes.

    All groups share the same global (Recipe, Re-entry) vocabulary, including
    zeros for inapplicable categories. recipe_by_step optionally overrides the
    default product recipes with an exhaustive {(product_id, route_index): id}
    mapping. Route indexes are zero-based; re-entry ordinals start at one.
    """

    _STAGES = ("Ready", "Transport", "Loading", "Setup", "Unloading")
    _PHASES = {
        EventKind.LOAD_COMPLETE: "Loading",
        EventKind.SETUP_COMPLETE: "Setup",
        EventKind.UNLOAD_COMPLETE: "Unloading",
    }

    def __init__(
        self,
        model: FabModel,
        config: TokenizerConfig,
        *,
        recipe_by_step: Mapping[tuple[str, int], str] | None = None,
    ) -> None:
        if model.time_unit != "minute":
            raise ValueError("tokenizer requires model.time_unit == 'minute'")
        self.config = config
        self.group_ids = tuple(sorted(model.tool_groups))
        if not self.group_ids:
            raise ValueError("explicit tool_groups are required")
        self._groups = {g: i for i, g in enumerate(self.group_ids)}
        self._routes = {p: product.route for p, product in model.products.items()}
        self._tool_groups = {t: spec.tool_group_id for t, spec in model.tools.items()}
        self._tool_capacities = {}
        for tool_id, group in self._tool_groups.items():
            if group not in self._groups:
                raise ValueError(f"tool {tool_id}: missing/unknown tool_group_id")
            spec = model.tool_groups[group]
            self._tool_capacities[tool_id] = max(1, spec.cascading_capacity) if spec.cascading_tool else 1
        self._total_tools = Counter(self._tool_groups.values())
        self._total_slots = Counter()
        for tool_id, group in self._tool_groups.items():
            self._total_slots[group] += self._tool_capacities[tool_id]
        for group in self.group_ids:
            if self._total_tools[group] != model.tool_groups[group].number_of_tools:
                raise ValueError(f"{group}: tool specs do not match number_of_tools")

        coordinates = {(p, i) for p, route in self._routes.items() for i in range(len(route))}
        if recipe_by_step is not None and set(recipe_by_step) != coordinates:
            raise ValueError("recipe_by_step must cover every static route coordinate exactly")
        self._route_categories = {}
        self._recipes = {}
        for product_id in sorted(self._routes):
            visits: Counter[str] = Counter()
            for index, step in enumerate(self._routes[product_id]):
                if step.tool_group_id not in self._groups:
                    raise ValueError(f"{product_id}/{index}: missing/unknown tool_group_id")
                visits[step.tool_group_id] += 1
                recipe = product_id if recipe_by_step is None else recipe_by_step[(product_id, index)]
                if not isinstance(recipe, str) or not recipe:
                    raise ValueError("Recipe IDs must be nonempty strings")
                self._recipes[(product_id, index)] = recipe
                self._route_categories[(product_id, index)] = (recipe, visits[step.tool_group_id])
        self.recipe_source = "product_id" if recipe_by_step is None else "explicit_static_mapping"
        self.pair_categories = tuple(sorted(set(self._route_categories.values())))
        self._pairs = {pair: i for i, pair in enumerate(self.pair_categories)}
        self.stages = self._STAGES + tuple(
            f"Processing[{i}]" for i in range(len(config.processing_edges_minutes) + 1))
        self._stages = {stage: i for i, stage in enumerate(self.stages)}
        sizes = (
            ("W", 1), ("P_WIP", len(self.pair_categories) * len(self.stages)),
            ("Q", 1), ("P_Queue", len(self.pair_categories)), ("C", 4),
            ("M", len(config.downtime_ratio_edges) + 1),
            ("B", len(config.downtime_ratio_edges) + 1),
            ("policy", len(config.dispatch_rules)),
        )
        offset = 0
        self._slices = {}
        for name, size in sizes:
            self._slices[name] = slice(offset, offset + size)
            offset += size
        self.input_dim = offset
        self.schema_id = hashlib.sha256(json.dumps(
            self.schema(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest()

    @property
    def slices(self) -> dict[str, slice]:
        return dict(self._slices)

    def schema(self) -> dict:
        """JSON-serializable column contract to save next to training data."""
        return {
            "version": "tool-group-token-v1",
            "group_ids": self.group_ids,
            "recipe_source": self.recipe_source,
            "route_categories": [
                [p, i, self._routes[p][i].tool_group_id, *self._route_categories[(p, i)]]
                for p, i in sorted(self._route_categories)
            ],
            "pair_categories": self.pair_categories,
            "stages": self.stages,
            "wip_flattening": "pair-major, stage-minor",
            "processing_edges_minutes": self.config.processing_edges_minutes,
            "downtime_ratio_edges": self.config.downtime_ratio_edges,
            "intervals": "left-closed, right-open; last includes upper endpoint",
            "dispatch_rules": self.config.dispatch_rules,
            "horizon_minutes": self.config.horizon_minutes,
            "normalization": {
                "W": self.config.wip_scale, "Q": self.config.queue_scale,
                "C": [[g, self._total_tools[g], self._total_tools[g],
                       self._total_slots[g], self._total_tools[g]] for g in self.group_ids],
                "zero_capacity_divisor": 1,
                "clipping": False,
            },
            "C_columns": ("total_tools", "available_tools", "occupied_slots", "down_tools"),
            "occupied_slots": "active plus paused activities; one slot per batch/cascade activity",
            "slices": {k: [v.start, v.stop] for k, v in self._slices.items()},
            "input_dim": self.input_dim,
        }

    def _check_engine(self, engine: FabEngine) -> None:
        if (engine.model.time_unit != "minute"
                or set(engine.model.tool_groups) != set(self.group_ids)
                or {p: x.route for p, x in engine.model.products.items()} != self._routes
                or {t: x.tool_group_id for t, x in engine.model.tools.items()} != self._tool_groups
                or set(engine.tool_states) != set(self._tool_groups)):
            raise ValueError("engine static model does not match tokenizer schema")
        for tool_id, state in engine.tool_states.items():
            if state.capacity != self._tool_capacities[tool_id]:
                raise ValueError(f"{tool_id}: runtime capacity differs from static schema")

    def encode(
        self,
        engine: FabEngine,
        *,
        dispatch_policy: Mapping[str, Sequence[float]],
        forecast: DowntimeForecast,
        t: float | None = None,
    ) -> TokenizedFab:
        """Snapshot the current DES time. Does not advance to past/future t.

        Call at a settled event boundary, with no concurrent DES mutation.
        dispatch_policy is the policy intended for the NEXT control window;
        this function encodes it and does not execute a dispatch strategy.
        """
        self._check_engine(engine)
        now = engine.current_time
        if not math.isfinite(now) or (t is not None and t != now):
            raise ValueError("t must equal the finite engine.current_time; restore/advance separately")
        if (not math.isfinite(forecast.as_of) or forecast.as_of > now
                or forecast.window_start != now
                or forecast.horizon_minutes != self.config.horizon_minutes
                or tuple(forecast.ratio_edges) != self.config.downtime_ratio_edges
                or not isinstance(forecast.method, str) or not forecast.method.strip()):
            raise ValueError("forecast must be causal and match current time, horizon, bins and method")
        for name, mapping in (("dispatch_policy", dispatch_policy), ("PM", forecast.pm),
                              ("breakdown", forecast.breakdown)):
            if set(mapping) != set(self.group_ids):
                raise ValueError(f"{name} must cover all tool groups exactly")
        policies = {g: _probabilities(dispatch_policy[g], len(self.config.dispatch_rules), f"policy/{g}")
                    for g in self.group_ids}
        bins = len(self.config.downtime_ratio_edges) + 1
        pm = {g: _probabilities(forecast.pm[g], bins, f"PM/{g}") for g in self.group_ids}
        bd = {g: _probabilities(forecast.breakdown[g], bins, f"breakdown/{g}") for g in self.group_ids}

        lots = {lot.id: lot for lot in engine.lots}
        if len(lots) != len(engine.lots):
            raise ValueError("duplicate lot IDs in engine.lots")
        assignments: dict[str, tuple[str, int, str]] = {}
        occupied: Counter[str] = Counter()

        def assign(lot: LotState, group: str, index: int, stage: str) -> None:
            if lot.id not in lots or lots[lot.id] is not lot:
                raise ValueError(f"activity/queue references unknown lot {lot.id}")
            if lot.route != self._routes.get(lot.product_id) or not 0 <= index < len(lot.route):
                raise ValueError(f"{lot.id}: route position does not match static schema")
            if lot.route[index].tool_group_id != group:
                raise ValueError(f"{lot.id}: service/route group mismatch at operation {index}")
            value = (group, index, stage)
            if lot.id in assignments:
                raise ValueError(f"{lot.id}: duplicate activity/queue membership")
            assignments[lot.id] = value

        def activity(tool_id: str, record: dict, paused: bool) -> None:
            group = self._tool_groups[tool_id]
            occupied[group] += 1
            completion = record["completion"]
            if completion == EventKind.OPERATION_COMPLETE:
                remaining = float(record["remaining"]) if paused else float(record["finish"]) - now
                if not math.isfinite(remaining) or remaining < -1e-6:
                    raise ValueError("invalid remaining processing time; use a settled snapshot")
                stage = f"Processing[{bisect_right(self.config.processing_edges_minutes, max(0, remaining))}]"
            else:
                stage = self._PHASES.get(completion)
                if stage is None:
                    raise ValueError(f"unsupported activity phase {completion}")
            members = record.get("batch_lots") or [lots[str(record["lot_id"])]]
            # Frozen members have not yet been marked in_process during loading/setup.
            if completion in (EventKind.LOAD_COMPLETE, EventKind.SETUP_COMPLETE):
                members = (engine._pending_batches or {}).get(tool_id) or members
            for lot in members:
                # Engine advances each member's route BEFORE unloading, including final unload.
                index = lot.operation_index - (completion == EventKind.UNLOAD_COMPLETE)
                assign(lot, group, index, stage)

        for tool_id, records in engine._active.items():
            for record in records.values():
                activity(tool_id, record, False)
        for tool_id, records in engine._paused.items():
            for record in records:
                activity(tool_id, record, True)

        for lot in engine.lots:
            if lot.id in assignments or lot.completed:
                continue
            if lot.in_process or lot.processing_tool_id is not None:
                raise ValueError(f"{lot.id}: in-process lot has no active/paused activity")
            step = lot.current_step
            group = step.tool_group_id
            if lot.id in engine._ready_group_by_lot:
                if engine._ready_group_by_lot[lot.id] != group or lot.ready_time > now:
                    raise ValueError(f"{lot.id}: inconsistent Ready Queue index")
                stage = "Ready"
            elif lot.ready_time > now:
                stage = "Transport"
            else:
                raise ValueError(f"{lot.id}: unresolved route transition; use a settled event boundary")
            assign(lot, group, lot.operation_index, stage)

        wip_hist = [[0] * (len(self.pair_categories) * len(self.stages)) for _ in self.group_ids]
        queue_hist = [[0] * len(self.pair_categories) for _ in self.group_ids]
        for lot_id, (group, index, stage) in assignments.items():
            lot = lots[lot_id]
            row = self._groups[group]
            pair = self._pairs[self._route_categories[(lot.product_id, index)]]
            wip_hist[row][pair * len(self.stages) + self._stages[stage]] += 1
            if stage == "Ready":
                queue_hist[row][pair] += 1

        available: Counter[str] = Counter()
        down: Counter[str] = Counter()
        for tool_id, tool in engine.tool_states.items():
            group = self._tool_groups[tool_id]
            available[group] += tool_is_available(tool, now)
            down[group] += tool.is_down
        rows, counts = [], []
        for row, group in enumerate(self.group_ids):
            w, q = sum(wip_hist[row]), sum(queue_hist[row])
            total = self._total_tools[group]
            counts.append(GroupCounts(w, q, total, available[group], occupied[group], down[group]))
            c = (total / max(1, total), available[group] / max(1, total),
                 occupied[group] / max(1, self._total_slots[group]), down[group] / max(1, total))
            rows.append((w / self.config.wip_scale,
                         *(n / w if w else 0.0 for n in wip_hist[row]),
                         q / self.config.queue_scale,
                         *(n / q if q else 0.0 for n in queue_hist[row]),
                         *c, *pm[group], *bd[group], *policies[group]))
        return TokenizedFab(now, self.config.horizon_minutes, self.group_ids,
                            tuple(rows), tuple(counts), self.schema_id, forecast.method)


def current_status_forecast(
    engine: FabEngine, *, horizon_minutes: float, ratio_edges: Sequence[float],
) -> DowntimeForecast:
    """Explicit persistence BASELINE, not an estimated failure/PM hazard model.

    Assumes each tool's current downtime status stays unchanged throughout the
    entire requested horizon. Thus the average ratio is a point mass. This
    uses no calendar, sampled future repair durations, RNG, or future records.
    It deliberately ignores new outages and recoveries; opt in explicitly.
    """
    _positive(horizon_minutes, "horizon_minutes")
    edges = _edges(ratio_edges, "ratio_edges", 1.0)
    totals: Counter[str] = Counter()
    pm_down: Counter[str] = Counter()
    bd_down: Counter[str] = Counter()
    for tool_id, tool in engine.tool_states.items():
        group = engine.model.tools[tool_id].tool_group_id
        if group not in engine.model.tool_groups:
            raise ValueError(f"{tool_id}: missing/unknown tool_group_id")
        totals[group] += 1
        if tool.is_down:
            reason = tool.down_reason or ""
            if reason.startswith("pm:"):
                pm_down[group] += 1
            elif reason.startswith("breakdown:"):
                bd_down[group] += 1
            else:
                raise ValueError(f"{tool_id}: cannot classify down_reason={reason!r}")

    def distribution(down: Counter[str]) -> dict[str, tuple[float, ...]]:
        result = {}
        for group in sorted(engine.model.tool_groups):
            ratio = down[group] / max(1, totals[group])
            index = bisect_right(edges, ratio)
            result[group] = tuple(float(i == index) for i in range(len(edges) + 1))
        return result

    return DowntimeForecast(
        as_of=engine.current_time, window_start=engine.current_time,
        horizon_minutes=horizon_minutes, ratio_edges=edges,
        pm=distribution(pm_down), breakdown=distribution(bd_down),
        method="current_status_persistence_baseline",
    )
