from __future__ import annotations

from math import inf

from fab.model.entities import FabModel, LotState, RouteStepSpec
from fab.strategy import StrategyDecision, StrategyState


class CriticalRatio:
    """At every decision point, prefer the lot with the smallest CR."""

    name = "CR"
    run_id = "critical_ratio"
    def initialize(self, model: FabModel) -> None:
        self.model = model
        # (product_id, wafer_count) -> suffix work table. Entry i is the
        # expected work from route step i through completion.
        self._remaining_work_tables: dict[tuple[str, int], tuple[float, ...]] = {}

        for release in model.lot_releases:
            product = model.products[release.product_id]
            wafer_count = model.lot_types[release.lot_type_id].wafer_count
            self._build_remaining_work_table(
                release.product_id,
                wafer_count,
                product.route,
            )

    def _build_remaining_work_table(
        self,
        product_id: str,
        wafer_count: int,
        route: tuple[RouteStepSpec, ...],
    ) -> tuple[float, ...]:
        key = (product_id, wafer_count)
        cached = self._remaining_work_tables.get(key)
        if cached is not None:
            return cached

        suffix = [0.0] * (len(route) + 1)
        for index in range(len(route) - 1, -1, -1):
            step = route[index]
            step_work = step.process_time
            # Wafer-unit steps are processed once per wafer. A batch step has
            # one furnace duration per lot, so it is not divided by batch size.
            if step.processing_unit == "wafer":
                step_work *= wafer_count
            suffix[index] = suffix[index + 1] + step_work

        result = tuple(suffix)
        self._remaining_work_tables[key] = result
        return result

    def _remaining_work(self, lot: LotState) -> float:
        table = self._build_remaining_work_table(
            lot.product_id,
            lot.wafer_count,
            lot.route,
        )
        return table[lot.operation_index]

    @staticmethod
    def _tie_break_key(lot: LotState) -> tuple[int, float, float, int, str]:
        #同时需要考虑优先级

        return (
            -lot.priority,
            lot.ready_time,
            lot.release_time,
            lot.input_order,
            lot.id,
        )

    def decide(self, state: StrategyState) -> StrategyDecision:
        dispatches: dict[str, LotState] = {}
        explanations: dict[str, object] | None = (
            {} if state.record_diagnostics else None
        )
        reserved_lot_ids: set[str] = set()

        # A lot may be considered by multiple interchangeable tools in this
        # decision. Its CR is identical at the current timestamp, so cache it.
        cr_cache: dict[str, tuple[float, float]] = {}

        def cr_and_work(lot: LotState) -> tuple[float, float]:
            cached = cr_cache.get(lot.id)
            if cached is not None:
                return cached

            remaining_work = self._remaining_work(lot)
            cr = (
                inf
                if lot.due_time is None or remaining_work <= 0.0
                else (lot.due_time - state.current_time) / remaining_work
            )
            result = (cr, remaining_work)
            cr_cache[lot.id] = result
            return result

        def cr_key(lot: LotState) -> tuple[float, int, float, float, int, str]:
            cr, _ = cr_and_work(lot)
            return (cr, *self._tie_break_key(lot))

        for tool, eligible_candidates in state.dispatchable_candidates:
            candidates = [
                lot
                for lot in eligible_candidates
                if lot.id not in reserved_lot_ids
            ]
            if not candidates:
                continue

            selected = min(candidates, key=cr_key)
            selected_cr, remaining_work = cr_and_work(selected)
            dispatches[tool.tool_id] = selected
            reserved_lot_ids.add(selected.id)
            if explanations is not None:
                explanations[tool.tool_id] = {
                    "rule": "最小 CR 优先",
                    "selected_lot_id": selected.id,
                    "selected_cr": selected_cr,
                    "remaining_work": remaining_work,
                    "due_time": selected.due_time,
                }

        release_lot_id = None
        if state.release_opportunity and state.waiting_lots:
            release_lot_id = min(state.waiting_lots, key=cr_key).id

        return StrategyDecision(
            dispatches=dispatches,
            # An ID is necessary: release_lot=True would instead release the
            # FIFO queue head, not the CR-selected order.
            release_lot_id=release_lot_id,
            diagnostics=(
                {
                    "strategy": {"name": self.name, "rule": "CR"},
                    "release": {
                        "rule": "订单池中最小 CR 优先",
                        "selected_lot_id": release_lot_id,
                    },
                    "dispatches": explanations,
                }
                if state.record_diagnostics
                else {}
            ),
        )
