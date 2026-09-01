# 记录最后结果
from __future__ import annotations

from fab.model.entities import FabModel, LotState
from fab.plugins.base import FabPlugin, PluginResultContext
from fab.plugins.storage import ResultStorage

class OnlineMetricsPlugin(FabPlugin):
    def __init__(self) -> None:
        self.model: FabModel | None=None
        self._wip_area = 0.0
        self._last_time = 0.0
        self._wip_lot_ids: set[str] = set()
        self._storage: ResultStorage | None = None

    def required_services(self) -> tuple[str, ...]:
        return ("result_storage",)

    def bind_services(self, services: dict[str, object]) -> None:
        storage = services["result_storage"]
        if not isinstance(storage, ResultStorage):
            raise TypeError("result_storage 必须实现 ResultStorage。")
        self._storage = storage

    def on_simulation_started(
            self,
            model:FabModel,
    ) -> None:
        self.model = model
        self._wip_area = 0.0
        self._last_time = model.simulation.start_time
        self._wip_lot_ids.clear()

    def on_time_advanced(
            self,
            current_time:float,
            lots: list[LotState],
    ) -> None:
        model = self._require_model()

        measurement_start = (
            model.simulation.start_time
            + model.simulation.warmup_time
        )

        interval_start = max(
            self._last_time,
            measurement_start
        )

        interval_end = min(
            current_time,
            model.simulation.end_time
        )

        if interval_end > interval_start:
            current_wip  = len(self._wip_lot_ids)
            elapsed = interval_end - interval_start

            self._wip_area +=current_wip * elapsed

        self._last_time = current_time

    def on_lot_released(self, current_time:float, lot:LotState) -> None:
        self._wip_lot_ids.add(lot.id)

    def on_lot_completed(self, current_time:float, lot:LotState) -> None:
        self._wip_lot_ids.discard(lot.id)

    def build_result(self, context:PluginResultContext) -> dict[str,object]:
        model = self._require_model()

        self.on_time_advanced(
            model.simulation.end_time,
            context.lots
        )

        measurement_start = (
            model.simulation.start_time
            + model.simulation.warmup_time
        )
        measurement_horizon = (
            model.simulation.end_time
            - measurement_start
        )

        completed_lots = [
            lot
            for lot in context.lots
            if (
                lot.end_time is not None
                and lot.end_time >= measurement_start
            )
        ]
        cycle_times = [
            lot.end_time - lot.generation_time
            for lot in completed_lots
            if lot.end_time is not None
        ]

        completed_with_due_date = [
            lot
            for lot in completed_lots
            if lot.due_time is not None
        ]

        on_time_count = sum(
            lot.end_time <= lot.due_time
            for lot in completed_with_due_date
            if (
                lot.end_time is not None
                and lot.due_time is not None
            )
        )
        measurement = {
            "throughput": len(completed_lots),
            "average_fab_wip": (
                round(
                    self._wip_area / measurement_horizon,
                    3,
                )
                if measurement_horizon > 0.0
                else 0.0
            ),
            "p95_end_to_end_cycle_time": (
                round(
                    self._percentile(cycle_times, 0.95),
                    3,
                )
                if cycle_times
                else 0.0
            ),
            "on_time_rate": (
                round(
                    on_time_count
                    / len(completed_with_due_date),
                    6,
                )
                if completed_with_due_date
                else 0.0
            ),
            "release_pool_lots_at_end": (
                context.release_pool_lots_at_end
            ),
        }

        self._require_storage().append_metrics(measurement)
        return {}

    def _require_storage(self) -> ResultStorage:
        if self._storage is None:
            raise RuntimeError("OnlineMetricsPlugin 尚未获得 result_storage。")
        return self._storage

    def _require_model(self) -> FabModel:
        if self.model is None:
            raise RuntimeError(
                "OnlineMetricsPlugin 尚未收到仿真开始通知。"
            )

        return self.model

    @staticmethod
    def _percentile(
        values: list[float],
        ratio: float,
    ) -> float:
        if not values:
            return 0.0

        sorted_values = sorted(values)
        index = (len(sorted_values) - 1) * ratio
        lower = int(index)
        upper = min(
            lower + 1,
            len(sorted_values) - 1,
        )

        return (
            sorted_values[lower]
            + (
                sorted_values[upper]
                - sorted_values[lower]
            )
            * (index - lower)
        )

class FinalStateSnapshotPlugin(FabPlugin):
    def __init__(self) -> None:
        self._storage: ResultStorage | None = None

    def required_services(self) -> tuple[str, ...]:
        return ("result_storage",)

    def bind_services(self, services: dict[str, object]) -> None:
        storage = services["result_storage"]
        if not isinstance(storage, ResultStorage):
            raise TypeError("result_storage 必须实现 ResultStorage。")
        self._storage = storage

    def build_result(self, context: PluginResultContext) -> dict[str,object]:
        storage = self._require_storage()
        event_time = context.model.simulation.end_time
        for lot in context.lots:
            storage.append_extension_data(
                plugin_name="final_state_snapshot",
                record_type="lot",
                event_time=event_time,
                payload=self._lot_row(lot),
            )
        for tool in context.tools.values():
            storage.append_extension_data(
                plugin_name="final_state_snapshot",
                record_type="tool",
                event_time=event_time,
                payload={
                    "id": tool.tool_id,
                    "name": tool.name,
                    "process": tool.type,
                },
            )
        return {}

    def _require_storage(self) -> ResultStorage:
        if self._storage is None:
            raise RuntimeError("FinalStateSnapshotPlugin 尚未获得 result_storage。")
        return self._storage

    @staticmethod
    def _lot_row(lot:LotState) -> dict[str, object]:
        return {
            "id": lot.id,
            "product_id": lot.product_id,
            "generation_time": lot.generation_time,
            "release_time": lot.release_time,
            "due_time": lot.due_time,
            "end_time": lot.end_time,
            "completed": lot.completed,
            "step": lot.operation_index,
        }
    
