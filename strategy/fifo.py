from __future__ import annotations

from fab.core import (
    FabStrategyBase,
    FactoryState,
    MachineState,
    WaferState,
    ready_candidates,
)


RELEASE_INTERVAL = 38.0


class FIFOStrategy(FabStrategyBase):
    """FIFO 策略对象；Engine 只接收这个对象，不再接收回调函数。"""

    name = "FIFO"
    run_id = "fifo-baseline"
    release_interval = RELEASE_INTERVAL

    def select_job(
        self,
        state: FactoryState,
        machine: MachineState,
        reserved_wafer_ids: set[str],
    ) -> WaferState | None:
        candidates = [
            wafer
            for wafer in ready_candidates(
                state.wafers,
                machine,
                state.current_time,
            )
            if wafer.id not in reserved_wafer_ids
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda wafer: (
                wafer.ready_time,
                wafer.release_time,
                wafer.input_order,
                wafer.id,
            ),
        )
