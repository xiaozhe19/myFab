from __future__ import annotations

from fab.model import FabModel
from fab.strategy import StrategyState,StrategyDecision

class EDD:
    name = "EDD"
    run_id = "edd"
    def __init__(self):
        pass

    def initialize(self, model:FabModel)-> None:
        self.model = model
        
    def decide(self, state:StrategyState) -> StrategyDecision:
        dispatches: dict[str, object] = {}
        explanations: dict[str, object] | None = (
            {} if state.record_diagnostics else None
        )
        reserved_lot_ids : set[str] = set()

        #派工，遍历引擎中所有候选的lot并排序
        for tool, eligible_candidates in state.dispatchable_candidates:
            candidates = [
                lot for lot in eligible_candidates
                if lot.id not in reserved_lot_ids
            ]
            if not candidates:
                continue

            selected  = min(candidates, key=self._edd_key)
            dispatches[tool.tool_id] = selected
            reserved_lot_ids.add(selected.id)
            if explanations is not None:
                explanations[tool.tool_id] = {
                    "rule":"按due_time 最早优先。",
                    "selected_due_time": selected.due_time
                }

        #投料
        release_lot = state.release_opportunity and state.waiting_lot_count >0

        return StrategyDecision(
            dispatches=dispatches,
            release_lot=release_lot,
            diagnostics={
                "strategy":{"name":self.name,"rule":"EDD"},
                "release":{"rule":"放行第一个"},
                "dispatches": explanations
            } if state.record_diagnostics else {},
        )

    @staticmethod
    def _edd_key(lot) -> tuple:
        due = lot.due_time if lot.due_time is not None else float("inf")
        return (due, lot.release_time,lot.input_order, lot.id)

