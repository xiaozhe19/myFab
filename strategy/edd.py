"""EDD：交期早的先做。没有交期的 lot 排最后。"""

from __future__ import annotations

from math import inf

from fab.model.entities import LotState
from strategy.base_rule import BaseRule


class EDDRule(BaseRule):
    name = "SMT2020_EDD"
    rank_rule = "EDD"

    def rank_key(self, lot: LotState) -> tuple[object, ...]:
        due = inf if lot.due_time is None else lot.due_time
        return (due, lot.ready_time, lot.input_order, lot.id)
