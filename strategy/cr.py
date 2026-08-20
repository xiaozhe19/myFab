from __future__ import annotations

from fab.model.entities import FabModel
from fab.strategy import StrategyDecision, StrategyState\


class CriticalRatio:
    name = "Critical Ratio"
    run_id = "critical_ratio"

    def __init__(self):
        pass

    def initialize(self, model:FabModel) -> None:
        self.model = model

    def _cal_cr(self,state):
        #更新所有lot的cr
        

    def decide(self, state:StrategyState) -> StrategyDecision:
        dispatches : dict[str,object] = {}
        explanations: dict[str, object] = {}
        reserved_lot_ids : set[str] = set()

        return StrategyDecision(
            dispatches = dispatches,
            release_lot = release_lot,
            diagnostics={
                "strategy": {"name":self.name,"rule":"EDD"},
                "release": {"rule":"放行第一个"},
                "dispatches": explanations
            },
        )

