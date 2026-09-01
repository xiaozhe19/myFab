from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class OperationRecord:
    lot_id: str
    product_id: str
    tool_id: str
    process: str
    step: int
    start_time: float
    end_time: float
    duration: float

@dataclass(frozen=True)
class LotEventRecord:
    lot_id: str
    event_time: float
    event_type: str
    step: int | None = None
    process: str | None = None
    tool_id: str | None = None

class ResultStorage(ABC):
    """供 trace、metrics 等插件调用的持久化接口。"""

    @abstractmethod
    def start_run(self) -> None:
        """开始一个新的运行。"""

    @abstractmethod
    def append_operation(self, record: OperationRecord) -> None:
        """追加一条已完成工序。"""

    @abstractmethod
    def append_lot_event(self, record: LotEventRecord) -> None:
        """追加一个lot的生命周期"""

    @abstractmethod
    def append_metrics(self, measurement: dict[str, float | int]) -> None:
        """追加本轮运行的汇总指标。"""

    @abstractmethod
    def append_extension_data(
        self,
        plugin_name: str,
        record_type: str,
        payload: dict[str, object],
        event_time: float | None = None,
    ) -> None:
        """追加插件自定义数据。"""
    @abstractmethod
    def finish_run(self, strategy_name: str) -> None:
        """完成本轮运行，提交并关闭存储连接。"""

