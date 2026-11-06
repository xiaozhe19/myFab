"""Tool Group Token V2 的特征计算，由 TokenizerPlugin 调用。

一张快照 = 工步表 [工步数, 阶段数]（每个路线工步上各阶段的 lot 数）+ 组表
[组数, 组特征维数]（设备状态 C、PM 预报 M、故障预报 B、策略 policy）。组级的
WIP/Queue 与组内分布都可由工步表按静态路线求和得到
（见 fabotron/layout.py），因此不重复存储。

Only current activities, paused activities, frozen batches and ready indexes
are inspected. The event calendar and all random generators are untouched.
"""

from __future__ import annotations

from array import array
import base64
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
import sys
from typing import Mapping, Sequence
import zlib

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
class TokenizedFab:
    """一张快照：工步表（每个工步各阶段的 lot 数）+ 组表（设备、预报、策略）。

    两张表都是行优先展平的一维数组：step_counts 为 uint16，长度 = 工步数 × 阶段数；
    group_features 为 float32，长度 = 组数 × 组特征维数。
    """

    time: float
    horizon_minutes: float
    step_counts: array
    group_features: array
    schema_id: str
    forecast_method: str

    def packed(self) -> dict[str, str]:
        return {"step_counts": pack_array(self.step_counts),
                "group_features": pack_array(self.group_features)}


def pack_array(values: array) -> str:
    """小端字节 -> zlib -> base64 文本（SQLite 的扩展记录只收 JSON）。"""

    if sys.byteorder != "little":
        values = array(values.typecode, values)
        values.byteswap()
    return base64.b64encode(zlib.compress(values.tobytes(), 6)).decode("ascii")


def unpack_array(text: str, typecode: str, length: int) -> array:
    values = array(typecode)
    values.frombytes(zlib.decompress(base64.b64decode(text)))
    if sys.byteorder != "little":
        values.byteswap()
    if len(values) != length:
        raise ValueError(f"packed array has {len(values)} values, expected {length}")
    return values


# 组表 C 块的列（都按台数，分母 = 总台数）
C_COLUMNS = ("total_tools", "idle_tools", "busy_tools", "down_tools")


class ToolGroupTokenizer:
    """按静态路线把当前时刻编码成工步表 + 组表（Tool Group Token V2）。

    工步 = (产品, 路线下标)，按产品名、下标排序，所有快照顺序相同。每个工步固定属于
    一个设备组；组的 WIP/Queue 等于其工步之和，所以不再单独存。schema 里的
    ``steps`` 记录每个工步的 [产品, 下标, 组, recipe, 第几次到访该组]，训练端据此
    还原旧的 (Recipe, Re-entry) 视图。recipe_by_step 可覆盖默认 recipe（= 产品名）。

    阶段：Ready（在队列）、Transport、Setup、Processing[剩余时长分档]。引擎已把
    装卸合并进加工，不存在 Loading/Unloading 活动；遇到未知活动阶段直接报错。
    """

    _STAGES = ("Ready", "Transport", "Setup")
    _PHASES = {EventKind.SETUP_COMPLETE: "Setup"}
    VERSION = "tool-group-token-v2"

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
        for group in self.group_ids:
            if self._total_tools[group] != model.tool_groups[group].number_of_tools:
                raise ValueError(f"{group}: tool specs do not match number_of_tools")

        coordinates = {(p, i) for p, route in self._routes.items() for i in range(len(route))}
        if recipe_by_step is not None and set(recipe_by_step) != coordinates:
            raise ValueError("recipe_by_step must cover every static route coordinate exactly")
        steps = []
        for product_id in sorted(self._routes):
            visits: Counter[str] = Counter()
            for index, step in enumerate(self._routes[product_id]):
                if step.tool_group_id not in self._groups:
                    raise ValueError(f"{product_id}/{index}: missing/unknown tool_group_id")
                visits[step.tool_group_id] += 1
                recipe = product_id if recipe_by_step is None else recipe_by_step[(product_id, index)]
                if not isinstance(recipe, str) or not recipe:
                    raise ValueError("Recipe IDs must be nonempty strings")
                steps.append((product_id, index, step.tool_group_id, recipe, visits[step.tool_group_id]))
        self.steps = tuple(steps)
        self._step_index = {(step[0], step[1]): k for k, step in enumerate(self.steps)}
        self.recipe_source = "product_id" if recipe_by_step is None else "explicit_static_mapping"
        self.stages = self._STAGES + tuple(
            f"Processing[{i}]" for i in range(len(config.processing_edges_minutes) + 1))
        self._stages = {stage: i for i, stage in enumerate(self.stages)}
        sizes = (
            ("C", 4),
            ("M", len(config.downtime_ratio_edges) + 1),
            ("B", len(config.downtime_ratio_edges) + 1),
            ("policy", len(config.dispatch_rules)),
        )
        offset = 0
        self._slices = {}
        for name, size in sizes:
            self._slices[name] = slice(offset, offset + size)
            offset += size
        self.group_dim = offset
        self.schema_id = hashlib.sha256(json.dumps(
            self.schema(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest()

    @property
    def slices(self) -> dict[str, slice]:
        """组表各块在组特征维上的位置。"""
        return dict(self._slices)

    @property
    def shape(self) -> dict[str, list[int]]:
        return {"step_counts": [len(self.steps), len(self.stages)],
                "group_features": [len(self.group_ids), self.group_dim]}

    def schema(self) -> dict:
        """JSON-serializable column contract to save next to training data."""
        return {
            "version": self.VERSION,
            "group_ids": self.group_ids,
            "recipe_source": self.recipe_source,
            "steps": self.steps,
            "step_columns": ("product_id", "route_index", "tool_group_id", "recipe", "visit"),
            "stages": self.stages,
            "step_counts": {"dtype": "uint16", "shape": self.shape["step_counts"],
                            "layout": "step-major, stage-minor", "unit": "lots"},
            "group_features": {"dtype": "float32", "shape": self.shape["group_features"],
                               "layout": "group-major, feature-minor",
                               "slices": {k: [v.start, v.stop] for k, v in self._slices.items()}},
            "packing": "little-endian bytes, zlib, base64",
            "processing_edges_minutes": self.config.processing_edges_minutes,
            "downtime_ratio_edges": self.config.downtime_ratio_edges,
            "intervals": "left-closed, right-open; last includes upper endpoint",
            "dispatch_rules": self.config.dispatch_rules,
            "horizon_minutes": self.config.horizon_minutes,
            "normalization": {
                "W": self.config.wip_scale, "Q": self.config.queue_scale,
                "C": [[g, *([self._total_tools[g]] * 4)] for g in self.group_ids],
                "zero_capacity_divisor": 1,
                "clipping": False,
            },
            "C_columns": C_COLUMNS,
            "C_semantics": ("tool counts / total tools; idle + busy + down = total. idle = can accept "
                            "a new lot now (eligibility.tool_is_available); down = is_down (a tool paused "
                            "mid-process counts as down only); busy = neither"),
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

        def assign(lot: LotState, group: str, index: int, stage: str) -> None:
            if lot.id not in lots or lots[lot.id] is not lot:
                raise ValueError(f"activity/queue references unknown lot {lot.id}")
            if lot.route != self._routes.get(lot.product_id) or not 0 <= index < len(lot.route):
                raise ValueError(f"{lot.id}: route position does not match static schema")
            if lot.route[index].tool_group_id != group:
                raise ValueError(f"{lot.id}: service/route group mismatch at operation {index}")
            if lot.id in assignments:
                raise ValueError(f"{lot.id}: duplicate activity/queue membership")
            assignments[lot.id] = (lot.product_id, index, stage)

        def activity(tool_id: str, record: dict, paused: bool) -> None:
            group = self._tool_groups[tool_id]
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
            # Frozen members have not yet been marked in_process during setup.
            if completion == EventKind.SETUP_COMPLETE:
                members = (engine._pending_batches or {}).get(tool_id) or members
            for lot in members:
                assign(lot, group, lot.operation_index, stage)

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

        width = len(self.stages)
        counts = array("H", bytes(2 * len(self.steps) * width))
        for product_id, index, stage in assignments.values():
            position = self._step_index[(product_id, index)] * width + self._stages[stage]
            if counts[position] == 0xFFFF:
                raise OverflowError("more than 65535 lots in one step/stage cell")
            counts[position] += 1

        # 三种状态互斥、按台数计：空闲（能马上接新 lot）/ 停机 / 忙（其余）
        idle: Counter[str] = Counter()
        busy: Counter[str] = Counter()
        down: Counter[str] = Counter()
        for tool_id, tool in engine.tool_states.items():
            group = self._tool_groups[tool_id]
            if tool.is_down:
                down[group] += 1
            elif tool_is_available(tool, now):
                idle[group] += 1
            else:
                busy[group] += 1
        features = array("f")
        for group in self.group_ids:
            total = self._total_tools[group]
            features.extend((total / max(1, total), idle[group] / max(1, total),
                             busy[group] / max(1, total), down[group] / max(1, total),
                             *pm[group], *bd[group], *policies[group]))
        return TokenizedFab(now, self.config.horizon_minutes, counts, features,
                            self.schema_id, forecast.method)


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
