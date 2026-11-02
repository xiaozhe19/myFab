"""仿真运行时按配置间隔采集 Token 并写入数据库。"""

# TODO: Engine 整理后，用公开的只读状态接口替换私有字段访问及启动入口的绑定。
# TODO: 增加同刻事件及动作结算完成后的回调，替换当前的延迟采集。

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
import math
from types import SimpleNamespace
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

from fab.model.entities import FabModel, LotState
from fab.plugins.base import FabPlugin, PluginResultContext
from fab.plugins.storage import ResultStorage

if TYPE_CHECKING:
    from fab.engine.engine import FabEngine
    from fab.plugins.tokenizer_core import DowntimeForecast, TokenizerConfig


Policy = Mapping[str, Sequence[float]] | Sequence[float]
PolicyProvider = Callable[[SimpleNamespace], Policy]
ForecastProvider = Callable[[SimpleNamespace], "DowntimeForecast"]
PERSISTENCE_BASELINE = "current_status_persistence_baseline"


class TokenizerPlugin(FabPlugin):
    """保存完整 Token 和 schema；默认六小时间隔及当前停机状态持续基线。"""

    def __init__(
        self,
        config: TokenizerConfig | Mapping[str, object] | None = None,
        dispatch_policy: Policy | PolicyProvider | None = None,
        *,
        sample_interval_minutes: float = 360,
        horizon_minutes: float | None = None,
        start_time: float | None = None,
        forecast_mode: str | None = None,
        forecast_provider: ForecastProvider | None = None,
        recipe_by_step: Mapping[tuple[str, int], str] | None = None,
    ) -> None:
        # 延迟导入，避免插件包与 Engine 的循环导入。
        from fab.plugins.tokenizer_core import TokenizerConfig

        if not math.isfinite(sample_interval_minutes) or sample_interval_minutes <= 0:
            raise ValueError("sample_interval_minutes must be finite and positive")
        if start_time is not None and not math.isfinite(start_time):
            raise ValueError("start_time must be finite")
        if horizon_minutes is not None and (not math.isfinite(horizon_minutes) or horizon_minutes <= 0):
            raise ValueError("horizon_minutes must be finite and positive")
        if forecast_provider is None and forecast_mode is None:
            forecast_mode = PERSISTENCE_BASELINE
        if forecast_provider is None and forecast_mode != PERSISTENCE_BASELINE:
            raise ValueError("provide forecast_provider or explicitly select " + PERSISTENCE_BASELINE)
        if forecast_provider is not None and forecast_mode is not None:
            raise ValueError("choose forecast_provider or forecast_mode, not both")
        self.config = (None if config is None else
                       config if isinstance(config, TokenizerConfig) else TokenizerConfig(**config))
        if self.config is not None and horizon_minutes is not None:
            raise ValueError("set horizon_minutes inside config or pass it separately, not both")
        self._default_horizon_minutes = 360 if horizon_minutes is None else horizon_minutes
        self.sample_interval_minutes = float(sample_interval_minutes)
        self.start_time = start_time
        self.forecast_mode = forecast_mode
        self._forecast_provider = forecast_provider
        self._recipe_by_step = dict(recipe_by_step) if recipe_by_step is not None else None
        if dispatch_policy is None:
            self._policy = None
        elif callable(dispatch_policy):
            self._policy = dispatch_policy
        elif isinstance(dispatch_policy, Mapping):
            self._policy = {g: tuple(p) for g, p in dispatch_policy.items()}
        else:
            self._policy = tuple(dispatch_policy)
        self._engine: FabEngine | None = None
        self._storage: ResultStorage | None = None
        self._encoder = None
        self._first_sample_time = 0.0
        self._next_sample_time = 0.0
        self._last_callback_time = 0.0
        self._sample_index = 0
        self._snapshot_count = 0

    def required_services(self) -> tuple[str, ...]:
        return ("result_storage",)

    def bind_services(self, services: dict[str, object]) -> None:
        storage = services["result_storage"]
        if not isinstance(storage, ResultStorage):
            raise TypeError("result_storage must implement ResultStorage")
        self._storage = storage

    def bind_engine(self, engine: FabEngine, *, restored: bool = False) -> None:
        """显式注入只读使用的 Engine 引用，不改变引擎的插件管理器。"""
        if self._engine is not None or self._encoder is not None:
            raise RuntimeError("TokenizerPlugin can only be bound once")
        if restored and (engine._strategy is None or engine._done):
            raise RuntimeError("restored binding requires an unfinished episode")
        if engine._strategy is not None and not restored:
            raise RuntimeError("bind_engine must be called before begin_episode")
        self._engine = engine

    def on_simulation_started(self, model: FabModel) -> None:
        from fab.plugins.tokenizer_core import TokenizerConfig, ToolGroupTokenizer

        if self._engine is None:
            raise RuntimeError("call TokenizerPlugin.bind_engine(engine) before running")
        if self._storage is None:
            raise RuntimeError("TokenizerPlugin requires result_storage")
        if self._encoder is not None:
            raise RuntimeError("TokenizerPlugin cannot be reused across runs")
        if self._engine.model is not model:
            raise ValueError("bound engine does not match the running model")
        first = self._engine.current_time if self.start_time is None else self.start_time
        if not self._engine.current_time <= first <= model.simulation.end_time:
            raise ValueError("start_time must fall within the simulation")
        if first + self.sample_interval_minutes <= first:
            raise ValueError("sample interval is too small for the timestamp precision")
        if self.config is None:
            self.config = TokenizerConfig(
                processing_edges_minutes=(30, 60, 120),
                downtime_ratio_edges=(.05, .10, .20),
                dispatch_rules=(self._engine._strategy.name,),
                horizon_minutes=self._default_horizon_minutes,
                wip_scale=100, queue_scale=100,
            )
        if self._policy is None:
            if len(self.config.dispatch_rules) != 1:
                raise ValueError("dispatch_policy is required when config has multiple dispatch rules")
            self._policy = (1.0,)
        self._encoder = ToolGroupTokenizer(model, self.config, recipe_by_step=self._recipe_by_step)
        self._first_sample_time = first
        self._next_sample_time = first
        self._grid_start = Decimal(str(first))
        self._grid_interval = Decimal(str(self.sample_interval_minutes))
        self._last_callback_time = self._engine.current_time
        self._storage.append_extension_data(
            plugin_name="tokenizer", record_type="schema", event_time=first,
            payload={
                "format_version": 1,
                "schema_id": self._encoder.schema_id,
                "tokenizer_schema": self._encoder.schema(),
                "sample_interval_minutes": self.sample_interval_minutes,
                "first_sample_time": first,
                "simulation_end_time": model.simulation.end_time,
                "capture_semantics": "after_same_time_events_and_actions",
                "forecast_mode": self.forecast_mode or "custom_provider",
            },
        )

    def on_time_advanced(self, current_time: float, lots: Sequence[LotState]) -> None:
        self._require_started()
        if not math.isfinite(current_time) or current_time < self._last_callback_time:
            raise ValueError("callback time must be finite and nondecreasing")
        if current_time != self._engine.current_time:
            raise ValueError("callback time must match engine.current_time")
        # 相同时间可能被回调多次；严格小于确保同刻新产生的事件/动作均已执行。
        self._capture_until(current_time, include_boundary=False)
        self._last_callback_time = current_time

    def build_result(self, context: PluginResultContext) -> dict[str, object]:
        self._require_started()
        if context.model is not self._engine.model or not self._engine._done:
            raise RuntimeError("final capture requires the bound engine to be finished")
        self._capture_until(self._engine.current_time, include_boundary=True)
        return {"tokenizer_snapshots": self._snapshot_count,
                "tokenizer_schema_id": self._encoder.schema_id}

    def _require_started(self) -> None:
        if self._encoder is None or self._engine is None or self._storage is None:
            raise RuntimeError("TokenizerPlugin has not started")

    def _capture_until(self, boundary: float, *, include_boundary: bool) -> None:
        from fab.plugins.tokenizer_core import current_status_forecast

        while True:
            sample_time = self._next_sample_time
            if sample_time > boundary or (sample_time == boundary and not include_boundary):
                return
            engine = self._engine
            active = engine._active
            if active and isinstance(next(iter(active)), tuple):
                # 归档 checkpoint 的活动表使用 (tool_id, token) 键；只重组只读视图。
                by_tool = {}
                for (tool_id, activity_token), record in active.items():
                    by_tool.setdefault(tool_id, {})[activity_token] = record
                active = by_tool
            # 只暴露 Tokenizer 必需的状态，不提供事件日历、随机数或未来事件。
            # 它是同步使用的浅视图；编码结束后只把独立的数值矩阵交给写库线程。
            view = SimpleNamespace(
                current_time=sample_time, model=engine.model, lots=engine.lots,
                tool_states=engine.tool_states, _active=active,
                _paused=engine._paused, _pending_batches=engine._pending_batches,
                _ready_group_by_lot=engine._ready_group_by_lot,
            )
            policy = self._policy(view) if callable(self._policy) else self._policy
            if not isinstance(policy, Mapping):
                policy = {g: policy for g in self._encoder.group_ids}
            forecast = (
                self._forecast_provider(view) if self._forecast_provider is not None
                else current_status_forecast(
                    view, horizon_minutes=self.config.horizon_minutes,
                    ratio_edges=self.config.downtime_ratio_edges,
                )
            )
            token = self._encoder.encode(view, dispatch_policy=policy, forecast=forecast)
            self._storage.append_extension_data(
                plugin_name="tokenizer", record_type="snapshot", event_time=token.time,
                payload={
                    "time": token.time, "recorded_at": boundary,
                    "horizon_minutes": token.horizon_minutes, "group_ids": token.group_ids,
                    "matrix": token.matrix, "counts": [asdict(c) for c in token.counts],
                    "schema_id": token.schema_id, "forecast_method": token.forecast_method,
                },
            )
            self._snapshot_count += 1
            self._sample_index += 1
            # 十进制网格避免 0.1 * 3 > 0.3 导致恰好位于结束时间的点漏采。
            # 只在生成快照后计算下一点，普通事件回调仅进行浮点时间比较。
            next_time = float(self._grid_start + self._sample_index * self._grid_interval)
            if not math.isfinite(next_time) or next_time <= sample_time:
                raise ValueError("sampling timestamps must remain finite and strictly increasing")
            self._next_sample_time = next_time
