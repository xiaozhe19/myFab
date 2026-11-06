"""Lot 的入厂、工步推进、运输与完工。"""

from __future__ import annotations

from fab.engine.events import EventKind, SimulationEvent
from fab.engine.index import dispatch as dispatch_index
from fab.model.entities import LotState, RouteStepSpec
from fab.sampling import sample_duration
from fab.snapshot_collections import SnapshotList


def admit_lot(engine, lot: LotState) -> None:
    """把放行的 lot 加入 fab，并定位到第一道实际加工工步。"""

    engine.lots.append(lot)
    engine._lots_by_id[lot.id] = lot
    engine._admitted_count += 1
    engine._lot_order[lot.id] = engine._admitted_count
    engine._plugin_manager.on_lot_released(engine.current_time, lot)
    if advance_over_unsampled_steps(engine, lot):
        dispatch_index.mark_ready(engine, lot)
    else:
        complete_lot(engine, lot)


def after_transport(engine, event: SimulationEvent) -> None:
    lot = engine._lot(str(event.payload["lot_id"]))
    lot.ready_time = engine.current_time
    dispatch_index.mark_ready(engine, lot)


def route_rework(engine, event: SimulationEvent) -> None:
    lot = engine._lot(str(event.payload["lot_id"]))
    step_id = str(event.payload["step_id"])
    target = engine._route_step_index_by_product[lot.product_id].get(step_id)
    if target is None:
        raise ValueError(f"重工目标 {step_id} 不在 Lot {lot.id} 的路线中。")
    end_index = int(event.payload.get("end_index", lot.operation_index - 1))
    if target > end_index:
        raise ValueError(f"重工目标 {step_id} 不能位于刚完成的工步之后。")
    bindings: dict[int, str | None] = {}
    ltl_targets = engine._ltl_target_steps_by_product[lot.product_id]
    for index in range(target, end_index + 1):
        tool_id = lot.processed_tool_by_step.get(index)
        # 抽检未命中的工步没有原设备；重走路线时它仍按抽检
        # 概率决定是否执行。上一轮真正加工过的工步必须重做：
        # 默认可用组内任意设备（None），rework_same_tool 时回原设备。
        if tool_id is None:
            continue
        bindings[index] = tool_id if engine._rework_same_tool else None
        step_id = lot.route[index].id
        if step_id in ltl_targets:
            # LTL 目标工步完成时已解除绑定；返工重做仍须回套刻绑定的设备。
            lot.dedicated_tools[step_id] = tool_id
    lot.rework_tool_bindings = bindings
    lot.operation_index = target
    schedule_transport_for_lot(engine, lot.id)


def advance_over_unsampled_steps(engine, lot: LotState) -> bool:
    """根据 SMT2020 processing probability 跳过未抽中的工步。

    返工段必须完整重复，因此有返工设备绑定的工步不再抽样。
    """

    while not lot.completed:
        if lot.operation_index in lot.rework_tool_bindings:
            return True
        step = lot.current_step
        if step is None:
            break
        probability = min(max(step.sampling_probability, 0.0), 1.0)
        if probability >= 1.0 or (
            probability > 0.0 and engine.lot_rng(lot.id).random() < probability
        ):
            return True
        if step.id is not None:
            lot.dedicated_tools.pop(step.id, None)
        lot.operation_index += 1
        engine._changed_lot_ids.add(lot.id)
    return False


def continue_route(engine, lot: LotState) -> None:
    """卸载后选出下一道实际需要加工的工步。"""

    if advance_over_unsampled_steps(engine, lot):
        schedule_transport_for_lot(engine, lot.id)
    else:
        complete_lot(engine, lot)


def complete_lot(engine, lot: LotState) -> None:
    """统一结算 lot 完工，避免抽检跳过路径漏掉完工通知。"""

    if lot.end_time is not None:
        return
    lot.end_time = engine.current_time
    engine._lot_rngs.pop(lot.id, None)
    engine._changed_lot_ids.add(lot.id)
    engine._plugin_manager.on_lot_completed(engine.current_time, lot)
    engine._completed_pending.append(lot)
    if engine._notify_completion:
        engine._fired_triggers.add("lot_completed")


def drop_completed_lots(engine) -> None:
    if not engine._completed_pending:
        return
    for lot in engine._completed_pending:
        del engine._lots_by_id[lot.id]
    engine.lots = SnapshotList(lot for lot in engine.lots if lot.end_time is None)
    engine._completed_pending.clear()


def record_cqt_violation(engine, event: SimulationEvent) -> None:
    lot = engine._lots_by_id.get(str(event.payload["lot_id"]))
    if lot is None:
        return
    target_index = int(event.payload["target_index"])
    # CQT 只要求在目标工步开始前；已开始加工则不构成违反。
    if lot.operation_index < target_index or (
        lot.operation_index == target_index and not lot.in_process
    ):
        if engine._plugin_manager.has_cqt_plugins:
            engine._plugin_manager.on_cqt_violation(
                engine.current_time,
                lot.id,
                target_index,
            )


def schedule_transport_for_lot(engine, lot_id: str) -> None:
    lot = engine._lot(lot_id)
    dispatch_index.remove_ready(engine, lot)
    if lot.completed:
        complete_lot(engine, lot)
        return
    lot.ready_time = float("inf")
    engine.schedule(
        engine.current_time + transport_time(engine, lot),
        EventKind.TRANSPORT_COMPLETE,
        lot_id=lot.id,
    )


def schedule_cqt_deadline(
    engine, lot: LotState, step: RouteStepSpec, *, at: float
) -> None:
    """CQT 从加工结束时刻 at 起算。"""
    if step.critical_queue_time is None or step.critical_queue_time_step is None:
        return
    target = engine._route_step_index_by_product[lot.product_id].get(
        step.critical_queue_time_step
    )
    if target is not None:
        # 卸载中停机可能长过 CQT 时限：截止时刻已过则立即到期（记为违反）。
        engine.schedule(
            max(at + step.critical_queue_time, engine.current_time),
            EventKind.CQT_EXPIRE,
            lot_id=lot.id,
            target_index=target,
        )


def transport_time(engine, lot: LotState) -> float:
    if lot.operation_index >= len(lot.route):
        return 0.0
    following = lot.route[lot.operation_index]
    previous_group = engine.model.tool_groups.get(lot.last_tool_group_id or "")
    following_group = engine.model.tool_groups.get(following.tool_group_id or "")
    if previous_group is None or following_group is None:
        return 0.0
    return sample_duration(
        engine.lot_rng(lot.id),
        engine._transport_by_location.get(
            (previous_group.location, following_group.location)
        ),
    )
