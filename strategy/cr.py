"""CR：(交期 - 现在) / 剩余名义加工时长，小者先做；已延误的为负，最先做。

没有交期或已无剩余加工的 lot 排最后。
"""

from __future__ import annotations

from math import inf

from fab.model.entities import LotState
from strategy.base_rule import BaseRule


class CRRule(BaseRule):
    name = "SMT2020_CR"
    rank_rule = "CR"

    def rank_key(self, lot: LotState) -> tuple[object, ...]:
        remaining = self.remaining_work(lot)
        if lot.due_time is None or remaining <= 0.0:
            cr = inf
        else:
            cr = (lot.due_time - self._now) / remaining
        return (cr, lot.ready_time, lot.input_order, lot.id)
