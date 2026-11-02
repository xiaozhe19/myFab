"""SPT：当前工步名义加工时长短的先做。"""

from __future__ import annotations

from fab.model.entities import LotState
from strategy.base_rule import BaseRule


class SPTRule(BaseRule):
    name = "SMT2020_SPT"
    rank_rule = "SPT"

    def rank_key(self, lot: LotState) -> tuple[object, ...]:
        work = self.step_work(lot.route[lot.operation_index], lot.wafer_count)
        return (work, lot.ready_time, lot.input_order, lot.id)
