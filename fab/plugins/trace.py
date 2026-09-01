"""把仿真事件转换为可持久化记录的插件。"""

from __future__ import annotations

from fab.model.entities import LotState, ToolState
from fab.plugins.base import FabPlugin
from fab.plugins.storage import LotEventRecord, OperationRecord, ResultStorage
from fab.strategy import StrategyDecision


class _StorageConsumer(FabPlugin):
    def __init__(self) -> None:
        self._storage: ResultStorage | None = None

    def required_services(self) -> tuple[str, ...]:
        return ("result_storage",)

    def bind_services(self, services: dict[str, object]) -> None:
        storage = services["result_storage"]
        if not isinstance(storage, ResultStorage):
            raise TypeError("result_storage 必须实现 ResultStorage。")
        self._storage = storage

    def _require_storage(self) -> ResultStorage:
        if self._storage is None:
            raise RuntimeError("插件尚未获得 result_storage。")
        return self._storage


class TracePlugin(_StorageConsumer):
    """将 lot 与工序事件流式写入存储服务。"""

    def on_lot_released(self, current_time: float, lot: LotState) -> None:
        self._require_storage().append_lot_event(
            LotEventRecord(lot.id, current_time, "released")
        )

    def on_operation_completed(
        self,
        lot: LotState,
        tool: ToolState,
        start: float,
        end: float,
        active_duration: float,
    ) -> None:
        process = lot.current_process
        if process is None:
            raise RuntimeError("完成工序时 lot 不应缺少当前 process。")
        storage = self._require_storage()
        storage.append_operation(
            OperationRecord(
                lot.id,
                lot.product_id,
                tool.tool_id,
                process,
                lot.operation_index,
                start,
                end,
                active_duration,
            )
        )
        
    def on_lot_completed(self, current_time: float, lot: LotState) -> None:
        self._require_storage().append_lot_event(
            LotEventRecord(lot.id, current_time, "completed")
        )


class DecisionLogPlugin(_StorageConsumer):
    """将策略诊断以插件扩展数据的形式流式写入。"""

    def on_decision_made(
        self,
        current_time: float,
        decision_kind: str,
        decision: StrategyDecision,
    ) -> None:
        self._require_storage().append_extension_data(
            plugin_name="decision_log",
            record_type="decision_made",
            event_time=current_time,
            payload={
                "kind": decision_kind,
                "diagnostics": dict(decision.diagnostics),
            },
        )


class CqtViolationPlugin(_StorageConsumer):
    """将已确认的 CQT violation 流式写入。"""

    def on_cqt_violation(
        self,
        current_time: float,
        lot_id: str,
        target_step: int,
    ) -> None:
        self._require_storage().append_extension_data(
            plugin_name="cqt_violation",
            record_type="violation",
            event_time=current_time,
            payload={"lot_id": lot_id, "target_step": target_step},
        )
