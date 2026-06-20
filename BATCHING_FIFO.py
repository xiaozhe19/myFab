from __future__ import annotations

from collections import Counter

from fab_sim_core import (
    FabStrategyBase,
    FactoryState,
    MachineState,
    WaferState,
    ready_candidates,
)


RELEASE_INTERVAL = 38.0


class BatchingFIFOStrategy(FabStrategyBase):
    """优先延续同产品、否则选择候选最多产品族的策略对象。"""

    name = "Batching FIFO"
    run_id = "batching-fifo-baseline"
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
                machine.type,
                state.current_time,
            )
            if wafer.id not in reserved_wafer_ids
        ]
        if not candidates:
            return None

        same_product = [
            wafer
            for wafer in candidates
            if wafer.product_id == machine.current_product_id
        ]
        if same_product:
            return min(
                same_product,
                key=lambda wafer: (
                    wafer.ready_time,
                    wafer.release_time,
                    wafer.input_order,
                    wafer.id,
                ),
            )

        product_counts = Counter(wafer.product_id for wafer in candidates)
        best_product = max(
            product_counts.items(),
            key=lambda item: (item[1], item[0]),
        )[0]
        product_candidates = [
            wafer for wafer in candidates if wafer.product_id == best_product
        ]
        return min(
            product_candidates,
            key=lambda wafer: (
                wafer.ready_time,
                wafer.release_time,
                wafer.input_order,
                wafer.id,
            ),
        )
