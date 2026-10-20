"""组批流程。"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from fab.model.entities import LotState, ToolState


class Batching:
    def __init__(self,engine, rules):
        self.engine = engine
        self.rules = rules

        engine._pending_batches = {}
        engine._ready_batch_class_by_lot = {}

    def gather(
            self, 
            tool: ToolState, 
            first: LotState,
            candidates: Iterable[LotState],
            *,
            can_use_tool: Callable[[LotState,str],bool]
               ) -> list[LotState]:
            """自动收集可同批的 lot（含 first），累计 wafer 不超过 batch_maximum。"""
    
            step = first.current_step
            if step is None:
                raise ValueError("已完成 Lot 不能组批。")
            members = [first]
            quantity = self.rules.member_quantity(tool, first)
            cap = step.batch_maximum
            for candidate in candidates:
                if (
                    candidate is first
                    or not self.rules.same_batch_class(tool, first, candidate)
                    or not can_use_tool(candidate, tool.tool_id)
                ):
                    continue
                candidate_quantity = self.rules.member_quantity(tool, candidate)
                if cap is not None and quantity + candidate_quantity > cap:
                    continue
                members.append(candidate)
                quantity += candidate_quantity
            return members

    def gather_ready(self, tool, first):
        engine = self.engine
        step = first.current_step
        if step is None:
            raise ValueError("已完成Lot不能组批。")

        candidates = engine._ready_lots_by_group.get(
            step.tool_group_id or "", ()
        )
        return self.gather(
        tool,
        first,
        candidates,
        can_use_tool=engine._dedication_allowed_fast,
        )

    def reserve(self, tool, first):
        engine = self.engine

        members = self.gather_ready(tool,first)
        engine._pending_batches[tool.tool_id] = members
        for member in members:
            engine._remove_ready(member)
        return members

    def select(self, tool):
        return self.engine._pending_batches.pop(tool.tool_id)

    def minimum_reached(self, tool, lot) -> bool:
        engine = self.engine

        step = lot.current_step
        if step is None or step.processing_unit != "batch":
            return True

        minimum = step.batch_minimum
        if minimum is None:
            return True

        quantity = self.rules.member_quantity(tool, lot)
        if quantity >= minimum:
            return True

        cap = step.batch_maximum

        batch_class = engine._ready_batch_class_by_lot.get(lot.id)
        if batch_class is None:
            batch_class = self.rules.class_key(lot)
        for candidate in engine._ready_lots_by_group.get(step.tool_group_id or "", ()):
            if candidate is lot:
                continue
            candidate_class = engine._ready_batch_class_by_lot.get(candidate.id)
            if candidate_class is None:
                candidate_class = self.rules.class_key(candidate)
            if candidate_class != batch_class:
                continue
            if not engine._dedication_allowed_fast(candidate, tool.tool_id):
                continue
            if not self.rules.same_batch_class(tool, lot, candidate):
                continue
            if not engine._dedication_allowed_fast(candidate, tool.tool_id):
                continue

            candidate_quantity = self.rules.member_quantity(tool, candidate)

            if cap is not None and quantity + candidate_quantity > cap:
                continue

            quantity += candidate_quantity

            if quantity >= minimum:
                return True
        return False
