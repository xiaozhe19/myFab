"""FIFO：先到当前工具组排队的先做。SMT2020 标准规则的排序键。"""

from __future__ import annotations

from fab.model.entities import LotState
from strategy.base_rule import BaseRule


class FIFORule(BaseRule):
    name = "SMT2020_FIFO"
    rank_rule = "FIFO"

    def rank_key(self, lot: LotState) -> tuple[object, ...]:
        return (lot.ready_time, lot.release_time, lot.input_order, lot.id)
