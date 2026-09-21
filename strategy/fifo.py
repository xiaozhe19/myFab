"""新事件引擎的 FIFO 基准策略。"""

from __future__ import annotations

from fab.model.entities import FabModel, LotState
from fab.strategy import FabStrategy, StrategyDecision, StrategyState


class FIFOStrategy(FabStrategy):
    """订单池按 FIFO 投料；每台空闲设备按 FIFO 选择可加工 lot。"""

    name = "FIFO"
    def initialize(self, _: FabModel) -> None:
        """FIFO 不需要预计算工厂参数。"""

    def decide(self, state: StrategyState) -> StrategyDecision:
        dispatches: dict[str, LotState] = {}
        explanations: dict[str, object] | None = (
            {} if state.record_diagnostics else None
        )
        reserved_lot_ids: set[str] = set()
        # 引擎直接提供按稳定顺序排列的设备及其合法候选 Lot。
        for tool, eligible_candidates in state.dispatchable_candidates:
            candidates = [
                lot
                for lot in eligible_candidates
                if lot.id not in reserved_lot_ids
            ]
            if not candidates:
                continue
            selected = min(candidates, key=self._fifo_key)
            dispatches[tool.tool_id] = selected
            reserved_lot_ids.add(selected.id)
            if explanations is not None:
                explanations[tool.tool_id] = {
                    "rule": "按 ready、实际投料、输入顺序、lot ID 排序。",
                    "selected_key": list(self._fifo_key(selected)),
                }

        return StrategyDecision(
            dispatches=dispatches,
            release_lot=state.release_opportunity and state.waiting_lot_count > 0,
            diagnostics=(
                {
                    "strategy": {"name": self.name, "rule": "FIFO"},
                    "release": {"rule": "每个投料检查点放行订单池队首 lot。"},
                    "dispatches": explanations,
                }
                if state.record_diagnostics
                else {}
            ),
        )

    @staticmethod
    def _fifo_key(lot: LotState) -> tuple[float, float, int, str]:
        return (lot.ready_time, lot.release_time, lot.input_order, lot.id)
