"""就绪队列、空闲设备与派工候选索引。"""

from __future__ import annotations

from bisect import bisect_right
from itertools import chain
from operator import itemgetter

from fab.engine.eligibility import tool_is_available
from fab.model.entities import LotState, RouteStepSpec, ToolState
from fab.strategy import DispatchableCandidate


# 从排序条目 (available_time, tool_id, tool, candidates) 中取出 (tool, candidates)。
_TOOL_AND_CANDIDATES = itemgetter(2, 3)


def dedication_allowed_fast(lot: LotState, tool_id: str) -> bool:
    """O(1) LTL 判断：当前工步若是进行中绑定的目标则必须回绑定设备。"""
    rework_tool = lot.rework_tool_bindings.get(lot.operation_index)
    if rework_tool is not None:
        return rework_tool == tool_id
    step = lot.route[lot.operation_index]
    bound = lot.dedicated_tools.get(step.id) if lot.dedicated_tools else None
    return bound is None or bound == tool_id


def setup_allowed(
    step: RouteStepSpec, current_setup: str | None, run_length: int
) -> bool:
    """setup 状态不构成引擎硬约束。

    Setups 表的最小连续加工量由 Toolgroups.DISPATCHING 的
    setupavoidance 派工规则执行，属于策略职责（见 strategy/base_rule.py）。
    保留此函数以维持候选构建的调用点不变。
    """
    return True


def candidate_index(engine) -> tuple[DispatchableCandidate, ...]:
    """更新发生变化的工具组，并返回设备与候选 Lot 配对。"""
    if not engine._candidate_groups_dirty:
        return engine._dispatchable_candidates_cache

    for group_id in engine._candidate_groups_dirty:
        rebuild_group_candidates(engine, group_id)
    # 候选变了的组和只需重排的组，都按当前 available_time 重建排序条目。
    # 只需重排的组要攒到这里才处理：没有组变脏时沿用旧缓存，与原行为一致。
    for group_id in engine._candidate_groups_dirty | engine._order_dirty_groups:
        group_candidates = engine._eligible_lots_by_group.get(group_id)
        if group_candidates:
            engine._entries_by_group[group_id] = [
                (
                    engine.tool_states[tool_id].available_time,
                    tool_id,
                    engine.tool_states[tool_id],
                    candidates,
                )
                for tool_id, candidates in group_candidates.items()
            ]
        else:
            engine._entries_by_group.pop(group_id, None)
    # 元组先比 available_time 再比 tool_id（唯一），与原排序规则相同。
    engine._dispatchable_candidates_cache = tuple(
        map(
            _TOOL_AND_CANDIDATES,
            sorted(chain.from_iterable(engine._entries_by_group.values())),
        )
    )
    engine._candidate_groups_dirty.clear()
    engine._order_dirty_groups.clear()
    return engine._dispatchable_candidates_cache


def rebuild_group_candidates(engine, group_id: str) -> None:
    """重新计算一个工具组内空闲设备与等待 Lot 的合法匹配。"""

    bucket = engine._ready_lots_by_group.get(group_id, ())
    tool_ids = engine._idle_tools_by_group.get(group_id)
    if not bucket or not tool_ids:
        engine._eligible_lots_by_group.pop(group_id, None)
        return

    group_candidates: dict[str, tuple[LotState, ...]] = {}
    tools = [engine.tool_states[tool_id] for tool_id in tool_ids]
    free = engine._free_ready_by_group.get(group_id, ())
    dedicated = engine._dedicated_ready_by_group.get(group_id, {})

    group = engine.model.tool_groups.get(group_id)
    is_batching_group = bool(group and group.batching_tool)
    # batch 组队列里有绑定到本组设备的 lot 时，凑批数量因设备而异，只能逐台计算。
    per_tool = is_batching_group and any(dedicated.values())
    if not per_tool:
        # _setup_allowed 不检查 setup，自由候选与设备无关，整组只算一次。
        common = tuple(
            lot
            for lot, step in free
            if step.processing_unit != "batch"
            or engine._batching.minimum_reached(tools[0], lot)
        )
    for tool in tools:
        profile = (tool.current_setup, tool.current_setup_run_length)
        if per_tool:
            candidates = tuple(
                lot
                for lot, step in free
                if setup_allowed(step, *profile)
                and (
                    step.processing_unit != "batch"
                    or engine._batching.minimum_reached(tool, lot)
                )
            )
        else:
            candidates = common
        own = dedicated.get(tool.tool_id, ())
        if own:
            own_candidates = tuple(
                lot
                for lot, step in own
                if setup_allowed(step, *profile)
                and (
                    step.processing_unit != "batch"
                    or engine._batching.minimum_reached(tool, lot)
                )
            )
            candidates = merge_by_lot_order(engine, candidates, own_candidates)
        if candidates:
            group_candidates[tool.tool_id] = candidates

    if group_candidates:
        group_candidates = engine._batching.limit_candidate_tools(
            group_id, bucket, group_candidates
        )
    if group_candidates:
        engine._eligible_lots_by_group[group_id] = group_candidates
    else:
        engine._eligible_lots_by_group.pop(group_id, None)


def is_eligible(engine, tool: ToolState, lot: LotState) -> bool:
    """校验单个策略配对，不为此重新扫描所有 lot。"""

    if not tool_is_available(tool, engine.current_time):
        return False
    if lot.in_process or lot.completed or lot.ready_time > engine.current_time:
        return False
    step = lot.current_step
    if step is None or step.tool_group_id != engine._tool_group_by_id[tool.tool_id]:
        return False
    if engine._ready_group_by_lot.get(lot.id) != step.tool_group_id:
        return False
    if not dedication_allowed_fast(lot, tool.tool_id):
        return False
    return setup_allowed(
        step, tool.current_setup, tool.current_setup_run_length
    ) and engine._batching.minimum_reached(tool, lot)


def mark_ready(engine, lot: LotState) -> None:
    """把刚进入等待状态的 lot 放入其当前工具组的有序索引。"""

    if lot.in_process or lot.completed or lot.ready_time > engine.current_time:
        return
    step = lot.route[lot.operation_index]
    group_id = step.tool_group_id
    if group_id is None:
        return
    remove_ready(engine, lot)
    if step.processing_unit == "batch":
        engine._ready_batch_class_by_lot[lot.id] = engine._batch_rules.class_key(lot)

    bucket = engine._ready_lots_by_group.setdefault(group_id, [])
    order = engine._lot_order[lot.id]
    index = bisect_right(bucket, order, key=lambda item: engine._lot_order[item.id])
    bucket.insert(index, lot)
    engine._ready_group_by_lot[lot.id] = group_id
    bound = lot.rework_tool_bindings.get(lot.operation_index)
    if bound is None and lot.dedicated_tools:
        bound = lot.dedicated_tools.get(step.id)
    if bound is None:
        pile = engine._free_ready_by_group.setdefault(group_id, [])
    elif engine._tool_group_by_id.get(bound) == group_id:
        pile = engine._dedicated_ready_by_group.setdefault(group_id, {}).setdefault(
            bound, []
        )
    else:
        pile = None  # 绑定到其他组的设备：与原逻辑一致，两堆都不放
    if pile is not None:
        index = bisect_right(
            pile, order, key=lambda item: engine._lot_order[item[0].id]
        )
        pile.insert(index, (lot, step))
        engine._ready_pile_by_lot[lot.id] = bound
    engine._candidate_groups_dirty.add(group_id)
    engine._dispatch_needed = True
    engine._changed_lot_ids.add(lot.id)


def remove_ready(engine, lot: LotState) -> None:
    engine._ready_batch_class_by_lot.pop(lot.id, None)
    engine._changed_lot_ids.add(lot.id)
    group_id = engine._ready_group_by_lot.pop(lot.id, None)
    if group_id is None:
        return
    bucket = engine._ready_lots_by_group[group_id]
    for index, candidate in enumerate(bucket):
        if candidate is lot:
            del bucket[index]
            break
    if lot.id in engine._ready_pile_by_lot:
        bound = engine._ready_pile_by_lot.pop(lot.id)
        if bound is None:
            pile = engine._free_ready_by_group[group_id]
        else:
            pile = engine._dedicated_ready_by_group[group_id][bound]
        for index, (candidate, _) in enumerate(pile):
            if candidate is lot:
                del pile[index]
                break
    if not bucket:
        del engine._ready_lots_by_group[group_id]
    engine._candidate_groups_dirty.add(group_id)


def mark_idle(engine, tool: ToolState) -> None:
    group_id = engine._tool_group_by_id[tool.tool_id]
    tools = engine._idle_tools_by_group.setdefault(group_id, set())
    if tool.tool_id in tools:
        # 已经空闲：候选不变，但 available_time 可能刚被刷新，排序要更新。
        engine._order_dirty_groups.add(group_id)
        return
    tools.add(tool.tool_id)
    engine._candidate_groups_dirty.add(group_id)
    engine._dispatch_needed = True


def mark_busy(engine, tool: ToolState) -> None:
    group_id = engine._tool_group_by_id[tool.tool_id]
    tools = engine._idle_tools_by_group.get(group_id)
    if tools is None or tool.tool_id not in tools:
        return
    tools.remove(tool.tool_id)
    if not tools:
        del engine._idle_tools_by_group[group_id]
    engine._candidate_groups_dirty.add(group_id)


def merge_by_lot_order(
    engine, left: tuple[LotState, ...], right: tuple[LotState, ...]
) -> tuple[LotState, ...]:
    """合并两个按 fab 进入顺序排列的候选序列。"""

    return tuple(sorted((*left, *right), key=lambda lot: engine._lot_order[lot.id]))
