"""推式投料：lot 到达 release pool 即放行（SMT2020 Lotrelease 的语义）。"""

from __future__ import annotations

from fab.model.entities import FabModel
from fab.strategy import ReleaseHandle, StrategyState


class PushRelease:
    name = "推式投料：到达即放行"
    triggers = frozenset({"order_arrival"})

    def initialize(self, model: FabModel, handle: ReleaseHandle) -> None:
        """推式投料不需要预计算或闹钟。"""

    def on_trigger(
        self, trigger: str, state: StrategyState, handle: ReleaseHandle
    ) -> None:
        for lot in state.waiting_lots:
            handle.release(lot.id)
