"""组批流程。"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from fab.engine.index import dispatch as dispatch_index
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
        can_use_tool=dispatch_index.dedication_allowed_fast,
        )

    def reserve(self, tool, first):
        engine = self.engine

        members = self.gather_ready(tool,first)
        engine._pending_batches[tool.tool_id] = members
        for member in members:
            dispatch_index.remove_ready(engine, member)
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
            if not dispatch_index.dedication_allowed_fast(candidate, tool.tool_id):
                continue
            if not self.rules.same_batch_class(tool, lot, candidate):
                continue
            if not dispatch_index.dedication_allowed_fast(candidate, tool.tool_id):
                continue

            candidate_quantity = self.rules.member_quantity(tool, candidate)

            if cap is not None and quantity + candidate_quantity > cap:
                continue

            quantity += candidate_quantity

            if quantity >= minimum:
                return True
        return False

    def limit_candidate_tools(
            self,
            group_id: str,
            bucket: tuple[LotState, ...] | list[LotState],
            candidates_by_tool: dict[str, tuple[LotState, ...]],
        ) -> dict[str, tuple[LotState, ...]]:
            """策略派工之前 限制有多少台炉子能看到同一批等待中的原料
            """
            engine = self.engine

            group = engine.model.tool_groups.get(group_id)
            if (
                group is None
                or not group.batching_tool
                or not bucket
                or any(lot.current_step.processing_unit != "batch" for lot in bucket)
            ):
                return candidates_by_tool
    
            representative_tool = engine.tool_states[next(iter(candidates_by_tool))]
            lots_by_class: dict[tuple[object, ...], list[LotState]] = {}
            for lot in bucket:
                lots_by_class.setdefault(self.rules.class_key(lot), []).append(lot)
    
            slots: list[tuple[object, ...]] = []
            classes = sorted(
                lots_by_class.items(),
                key=lambda item: min(engine._lot_order[lot.id] for lot in item[1]),
            )
            for class_key, class_lots in classes:
                step = class_lots[0].current_step
                if step is None:
                    continue
                quantity = sum(
                    self.rules.member_quantity(representative_tool, lot)
                    for lot in class_lots
                )
                minimum = step.batch_minimum or 1
                maximum = step.batch_maximum or quantity
                while quantity >= minimum:
                    slots.append(class_key)
                    quantity -= min(maximum, quantity)
    
            remaining_tools = sorted(candidates_by_tool)
            result: dict[str, tuple[LotState, ...]] = {}
            for class_key in slots:
                for tool_id in tuple(remaining_tools):
                    tool = engine.tool_states[tool_id]
                    matching = tuple(
                        lot
                        for lot in candidates_by_tool[tool_id]
                        if self.rules.class_key(lot) == class_key
                    )
                    if matching:
                        result[tool_id] = matching
                        remaining_tools.remove(tool_id)
                        break
                if not remaining_tools:
                    break
            return result
     
