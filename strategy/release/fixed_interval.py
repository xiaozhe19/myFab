"""固定节拍投料：每隔 ``interval`` 分钟放行订单池队首的一个 lot。"""

from __future__ import annotations

from fab.model.entities import FabModel
from fab.strategy import ReleaseHandle, StrategyState


class FixedIntervalRelease:
    triggers = frozenset({"release_wakeup"})

    def __init__(self, interval: float | None = None) -> None:
        # None：使用模型的 simulation.release_interval。
        self.interval = interval
        self.name = "固定节拍投料：每个节拍放行队首 lot"

    def initialize(self, model: FabModel, handle: ReleaseHandle) -> None:
        if self.interval is None:
            self.interval = model.simulation.release_interval
        if self.interval <= 0:
            raise ValueError("投料节拍必须为正数。")
        handle.schedule_wakeup(model.simulation.start_time)

    def on_trigger(
        self, trigger: str, state: StrategyState, handle: ReleaseHandle
    ) -> None:
        if state.waiting_lots:
            handle.release(state.waiting_lots[0].id)
        handle.schedule_wakeup(state.current_time + self.interval)
