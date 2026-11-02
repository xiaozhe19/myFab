"""SRPT：剩余名义加工时长短（离完工近）的先做。"""

from __future__ import annotations

from fab.model.entities import LotState
from strategy.base_rule import BaseRule


class SRPTRule(BaseRule):
    name = "SMT2020_SRPT"
    rank_rule = "SRPT"

    def rank_key(self, lot: LotState) -> tuple[object, ...]:
        return (self.remaining_work(lot), lot.ready_time, lot.input_order, lot.id)
