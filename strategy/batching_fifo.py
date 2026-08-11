from __future__ import annotations

from collections import Counter

<<<<<<< ours
from fab.core import (
    FabStrategyBase,
    FactoryState,
    MachineState,
    WaferState,
    ready_candidates,
)
=======
from fab.engine.eligibility import eligible_lots
from fab.model.entities import FabModel, LotState
from fab.strategy import FabStrategy, StrategyDecision, StrategyState
>>>>>>> theirs


RELEASE_INTERVAL = 38.0


<<<<<<< ours
class BatchingFIFOStrategy(FabStrategyBase):
=======
class BatchingFIFOStrategy(FabStrategy):
>>>>>>> theirs
    """优先延续同产品、否则选择候选最多产品族的策略对象。"""

    name = "Batching FIFO"
    run_id = "batching-fifo-baseline"
    release_interval = RELEASE_INTERVAL

<<<<<<< ours
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
=======
    def initialize(self, model: FabModel) -> None:
        """Batching FIFO 不需要预计算工厂参数。"""

    def decide(self, state: StrategyState) -> StrategyDecision:
        dispatches: dict[str, LotState] = {}
        explanations: dict[str, object] = {}
        reserved_lot_ids: set[str] = set()
        for machine in sorted(
            state.tool_states.values(),
            key=lambda item: (item.available_time, item.tool_id),
        ):
            candidates = [
                lot
                for lot in eligible_lots(
                    state.model, machine, list(state.lots), state.current_time
                )
                if lot.id not in reserved_lot_ids
            ]
            if not candidates:
                continue
            product_counts = Counter(lot.product_id for lot in candidates)
            same_product = [
                lot for lot in candidates
                if lot.product_id == machine.current_product_id
            ]
            if same_product:
                selected = min(same_product, key=self._fifo_key)
            else:
                best_product = max(
                    product_counts.items(), key=lambda item: (item[1], item[0])
                )[0]
                selected = min(
                    [lot for lot in candidates if lot.product_id == best_product],
                    key=self._fifo_key,
                )
            dispatches[machine.tool_id] = selected
            explanations[machine.tool_id] = {
                "rule": "优先延续设备当前产品；否则选择候选数最多的产品，再按 FIFO。",
                "selected_product": selected.product_id,
                "candidate_product_counts": dict(product_counts),
            }
            reserved_lot_ids.add(selected.id)
        return StrategyDecision(
            dispatches=dispatches,
            release_lot=state.release_opportunity and state.waiting_lot_count > 0,
            diagnostics={
                "strategy": {"name": self.name, "rule": "Batching FIFO"},
                "release": {"rule": "每个投料检查点释放 waiting list 队首 lot。"},
                "dispatches": explanations,
            },
        )

    @staticmethod
    def _fifo_key(lot: LotState) -> tuple[float, float, int, str]:
        return (lot.ready_time, lot.release_time, lot.input_order, lot.id)
>>>>>>> theirs
