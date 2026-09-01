"""
    Fab插件的基础接口
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping,Sequence

from fab.model.entities import FabModel,LotState,ToolState
from fab.strategy import StrategyDecision

@dataclass(frozen=True)
class PluginResultContext:
    strategy_name: str
    model: FabModel
    lots: Sequence[LotState]
    tools: Mapping[str, ToolState]
    release_pool_lots_at_end: int

class FabPlugin:
    def on_simulation_started(self, model:FabModel) -> None:
        """仿真开始调用一次"""

    def on_time_advanced(
            self,
            current_time: float,
            lots: Sequence[LotState]
    ) -> None:
        """仿真时钟前进时调用"""

    def on_lot_released(
            self,
            current_time:float,
            lot:LotState,
    )-> None:
        """Lot 被许可进入Fab后调用"""

    def on_operation_completed(
            self,
            lot:LotState,
            tool:ToolState,
            start:float,
            end:float,
            active_duration:float,
    ) -> None:
        """一道实际加工工序完成后调用"""
    def on_lot_completed(
        self,
        current_time: float,
        lot: LotState,
    ) -> None:
        """Lot 完成完整路线后调用。"""

    def on_decision_made(
        self,
        current_time: float,
        decision_kind: str,
        decision: StrategyDecision,
    ) -> None:
        """策略完成一次 release 或 dispatch 决策后调用。"""

    def on_cqt_violation(
        self,
        current_time: float,
        lot_id: str,
        target_step: int,
    ) -> None:
        """确认发生 CQT violation 时调用。"""

    def build_result(
        self,
        context: PluginResultContext,
    ) -> dict[str, object]:
        """返回插件希望加入最终 result 的字段。"""

        return {}

    def provided_services(self) -> dict[str, object]:
        """声明本插件可供其他插件使用的服务。"""
        return {}

    def required_services(self) -> tuple[str, ...]:
        """声明本插件需要的服务名。"""
        return ()

    def bind_services(self, services: dict[str, object]) -> None:
        """PluginManager 收集完服务后注入给本插件。"""

    def on_simulation_finished(
        self,
        context: PluginResultContext,
    ) -> None:
        """所有插件完成 build_result 后调用。"""
