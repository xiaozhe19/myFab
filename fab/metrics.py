"""只收集学习调度所需的结果指标。"""

from __future__ import annotations

from fab.model.entities import FabModel, LotState, ToolState


class MetricsCollector:
    def __init__(self, model: FabModel) -> None:
        self.model = model
        self.operations: list[dict[str, object]] = []
        self._wip_area = 0.0
        self._last_time = model.simulation.start_time
        self._wip_lot_ids: set[str] = set()

    def lot_released(self, lot: LotState) -> None:
        """记录一个已经实际进入 Fab 的 Lot。"""

        self._wip_lot_ids.add(lot.id)

    def lot_completed(self, lot: LotState) -> None:
        """记录一个已经完成路线的 Lot。"""

        self._wip_lot_ids.discard(lot.id)

    def advance_time(self, time: float, lots: list[LotState]) -> None:
        start = max(
            self._last_time,
            self.model.simulation.start_time + self.model.simulation.warmup_time,
        )
        end = min(time, self.model.simulation.end_time)
        if end > start:
            wip = len(self._wip_lot_ids)
            self._wip_area += wip * (end - start)
        self._last_time = time

    def record_operation(
        self,
        lot: LotState,
        tool: ToolState,
        start: float,
        end: float,
        active_duration: float | None = None,
    ) -> None:
        duration = end - start if active_duration is None else active_duration
        self.operations.append(
            {
                "lot_id": lot.id,
                "product_id": lot.product_id,
                "tool_id": tool.tool_id,
                "process": lot.current_process,
                "step": lot.operation_index,
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(duration, 3),
            }
        )

    def result(
        self,
        strategy: str,
        lots: list[LotState],
        tools: dict[str, ToolState],
        release_pool_lots_at_end: int,
    ) -> dict[str, object]:
        self.advance_time(self.model.simulation.end_time, lots)
        start = self.model.simulation.start_time + self.model.simulation.warmup_time
        horizon = self.model.simulation.end_time - start
        completed = [
            lot for lot in lots if lot.end_time is not None and lot.end_time >= start
        ]
        end_to_end_cycle_times = [
            lot.end_time - lot.generation_time
            for lot in completed
            if lot.end_time is not None
        ]
        completed_with_due_date = [lot for lot in completed if lot.due_time is not None]
        on_time_count = sum(
            lot.end_time <= lot.due_time
            for lot in completed_with_due_date
            if lot.end_time is not None and lot.due_time is not None
        )
        return {
            "strategy": strategy,
            "factory_id": self.model.id,
            "time_unit": self.model.time_unit,
            "operations": self.operations,
            "lots": [self._lot_row(lot) for lot in lots],
            "machines": [
                {"id": tool.tool_id, "name": tool.name, "process": tool.type}
                for tool in tools.values()
            ],
            "measurement": {
                "throughput": len(completed),
                "average_fab_wip": (
                    round(self._wip_area / horizon, 3) if horizon else 0.0
                ),
                "p95_end_to_end_cycle_time": (
                    round(self._percentile(end_to_end_cycle_times, 0.95), 3)
                    if end_to_end_cycle_times
                    else 0.0
                ),
                "on_time_rate": (
                    round(on_time_count / len(completed_with_due_date), 6)
                    if completed_with_due_date
                    else 0.0
                ),
                "release_pool_lots_at_end": release_pool_lots_at_end,
            },
        }

    @staticmethod
    def _lot_row(lot: LotState) -> dict[str, object]:
        return {
            "id": lot.id,
            "product_id": lot.product_id,
            "generation_time": lot.generation_time,
            "release_time": lot.release_time,
            "due_time": lot.due_time,
            "end_time": lot.end_time,
            "completed": lot.completed,
            "step": lot.operation_index,
            "operation_history": lot.operation_history,
        }

    @staticmethod
    def _percentile(values: list[float], ratio: float) -> float:
        if not values:
            return 0.0
        values = sorted(values)
        index = (len(values) - 1) * ratio
        lower = int(index)
        upper = min(lower + 1, len(values) - 1)
        return values[lower] + (values[upper] - values[lower]) * (index - lower)
