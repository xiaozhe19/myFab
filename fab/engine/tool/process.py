"""单个设备加工流程"""

from fab.engine.index import dispatch as dispatch_index
from fab.engine.lot import route as lot_route
from fab.engine.events import EventKind, SimulationEvent
from fab.model.entities import LotState, ToolState
from fab.sampling import sample_duration, sample_duration_sum

class Process:
    def __init__(self,engine):
        self.engine = engine

    def after_setup(self, event: SimulationEvent) -> None:
        """换型"""
        engine = self.engine

        tool, lot, active = engine._current_activity(event)
        if tool is None or lot is None or active is None:
            return
        tool.current_setup = str(event.payload["setup"])
        tool.current_setup_run_length = 0
        self.start_process(
            tool,
            lot,
            activity_token=int(active["token"]),
        )

    def start_phase(
            self,
            tool: ToolState,
            lot: LotState,
            completion: EventKind,
            duration: float,
            *,
            batch_lots: list[LotState] | None = None,
            activity_token: int | None = None,
            next_start: float | None = None,
            **payload: object,
        ) -> None:
            engine = self.engine
            
            """ 登记和切换活动状态"""
    
            finish = engine._time(engine.current_time + max(duration, 0.0))
            if activity_token is None:
                engine._tokens[tool.tool_id] += 1
                token = engine._tokens[tool.tool_id]
                active: dict[str, object] = {} #新的activity
            else:
                token = activity_token
                active = engine._active.get(tool.tool_id, {}).get(token, {})
                if not active:
                    raise RuntimeError(
                        f"设备 {tool.tool_id} 的加工活动 token {token} 已失效。"
                    )
    
            members = batch_lots or [lot]
            active.update(
                {
                    "token": token,
                    "completion": completion,
                    "lot_id": lot.id,
                    "step": lot.operation_index,
                    "finish": finish,
                    "phase_start": engine.current_time,
                    "batch_lots": members,
                    "next_start": engine._time(
                        next_start if next_start is not None else finish
                    ),
                    **payload,
                }
            )
            engine._active.setdefault(tool.tool_id, {})[token] = active
            
            tool.active_lot_id = lot.id
            current_step = lot.current_step
            tool.active_step_id = current_step.id if current_step else None
            engine._refresh_tool_capacity(tool)
            engine.schedule(
                finish,
                completion,
                tool_id=tool.tool_id,
                lot_id=lot.id,
                token=token,
                **payload,
            )

    def start_process(
        self,
        tool: ToolState,
        lot: LotState,
        *,
        activity_token: int | None = None,
        next_start: float | None = None,
        lead_time: float = 0.0,
    ) -> None:
        """启动"加工+卸载"阶段。lead_time 是加工前仍需占用的时间（装载）。

        阶段在卸载结束时触发 OPERATION_COMPLETE；槽位开放时刻 next_start
        仍按加工开始/结束计算，与卸载无关。
        """
        engine = self.engine

        step = lot.current_step
        if step is None:
            raise ValueError("已完成 Lot 不能开始加工。")
        process_start = engine.current_time + lead_time
        unloading = engine._unloading_time(tool)
        if step.processing_unit == "batch":
            members = engine._batching.select(tool)
            for member in members:
                dispatch_index.remove_ready(engine, member)
                member.in_process = True
                member.processing_tool_id = tool.tool_id
                member.start_time = (
                    member.start_time
                    if member.start_time is not None
                    else engine.current_time
                )
            # 一炉抽一次，走设备的流：组批成员随规则变化，不能用某个 lot 的流。
            duration = sample_duration(
                engine.tool_rng(tool.tool_id, "batch"),
                step.processing_distribution,
                step.process_time,
            )
            self.start_phase(
                tool,
                lot,
                EventKind.OPERATION_COMPLETE,
                lead_time + duration + unloading,
                process_start=process_start,
                process_end=process_start + duration,
                process_time=duration,
                step_index=lot.operation_index,
                batch_lots=members,
                activity_token=activity_token,
                next_start=(
                    next_start if next_start is not None else process_start + duration
                ),
            )
            return
        # wafer 步：普通设备逐片求和；级联设备只有第一片走完整
        # 加工时间，后续 wafer 按 cascading interval 流水进入。
        n = lot.wafer_count if step.processing_unit == "wafer" else 1
        group = engine._tool_group(tool)
        if (
            n > 1
            and group is not None
            and group.cascading_tool
            and step.cascading_interval is not None
        ):
            duration = (
                sample_duration(
                    engine.lot_rng(lot.id),
                    step.processing_distribution,
                    step.process_time,
                )
                + (n - 1) * step.cascading_interval
            )
        else:
            duration = sample_duration_sum(
                engine.lot_rng(lot.id),
                step.processing_distribution,
                step.process_time,
                n,
            )
        if (
            n > 1
            and group is not None
            and group.cascading_tool
            and step.cascading_interval is not None
        ):
            next_start = process_start + (n - 1) * step.cascading_interval
        elif (
            step.processing_unit == "lot"
            and group is not None
            and group.cascading_tool
            and step.cascading_interval is not None
        ):
            # 按 lot 级联：相邻两个 lot 的完工间隔为 cascading interval，
            # 因此下一个 lot 在本 lot 开工一个 interval 后即可占用第二槽位。
            next_start = process_start + step.cascading_interval
        elif next_start is None:
            # 非级联：槽位在加工结束时开放，与拆分卸载事件时的语义一致。
            next_start = process_start + duration
        self.start_phase(
            tool,
            lot,
            EventKind.OPERATION_COMPLETE,
            lead_time + duration + unloading,
            process_start=process_start,
            process_end=process_start + duration,
            process_time=duration,
            step_index=lot.operation_index,
            activity_token=activity_token,
            next_start=next_start,
        )


    def after_operation(self, event: SimulationEvent) -> None:
        """加工+卸载阶段结束：先按加工结束时刻记账，再执行卸载后的路由。"""
        engine = self.engine
        tool, lot, active = engine._current_activity(event)
        if tool is None or lot is None or active is None:
            return
        members = active.get("batch_lots", [lot])
        step = lot.current_step
        if step is None:
            return
        start = float(event.payload.get("process_start", engine.current_time))
        # process_start/process_end 在开工时登记；停机恢复时只顺延尚未到达的那个
        # （见 Downtime.end_downtime），所以卸载中停机不会推迟加工结束时刻。
        process_end = engine._time(
            float(
                event.payload.get(
                    "process_end",
                    engine.current_time - engine._unloading_time(tool),
                )
            )
        )
        # 停机只暂停、不丢失剩余时长，所以实际加工时长就是采样的加工时长。
        worked = float(event.payload.get("process_time", process_end - start))
        for member in members:
            if engine._plugin_manager.has_operation_plugins:
                engine._plugin_manager.on_operation_completed(
                    member,
                    tool,
                    start,
                    process_end,
                    worked,
                )
            operation_key = engine._operation_keys_by_product[member.product_id][
                member.operation_index
            ]
            # Outstanding states retain their old version without forcing every
            # state to copy a table that most strategies never read.
            if engine._completion_snapshot_ref is not None:
                engine._detach_completion_snapshot()
            engine._last_operation_completion[operation_key] = process_end
            member.last_tool_group_id = step.tool_group_id
            member.processed_tool_by_step[member.operation_index] = tool.tool_id
            member.rework_tool_bindings.pop(member.operation_index, None)
            if step.lot_to_lens_dedication_step:
                # 绑定工步：把 lot 绑定到当前设备，供 LTL 目标工步（step id）回本机。
                member.dedicated_tools[step.lot_to_lens_dedication_step] = tool.tool_id
            elif step.id in member.dedicated_tools:
                # LTL 目标工步完成，解除该绑定（不影响其他进行中的绑定）。
                del member.dedicated_tools[step.id]
            lot_route.schedule_cqt_deadline(engine, member, step, at=process_end)
            member.operation_index += 1
            engine._changed_lot_ids.add(member.id)
            tool.processed_wafers += member.wafer_count
        tool.current_setup_run_length += 1
        engine._downtime.schedule_counter_maintenance(tool)

        # 卸载完成：释放槽位，lot 进入下一道工步。
        token = int(active["token"])
        tool_activities = engine._active.get(tool.tool_id)
        if tool_activities is not None:
            tool_activities.pop(token, None)
            if not tool_activities:
                engine._active.pop(tool.tool_id, None)
        for member in members:
            member.in_process, member.processing_tool_id = False, None
            if member.completed:
                lot_route.complete_lot(engine, member)
                continue
            if (
                engine.lot_rng(member.id).random() < step.rework_probability
                and step.rework_step
            ):
                engine.schedule(
                    engine.current_time,
                    EventKind.REWORK_ROUTED,
                    lot_id=member.id,
                    step_id=step.rework_step,
                    end_index=member.operation_index - 1,
                )
            else:
                lot_route.continue_route(engine, member)
        engine._refresh_tool_capacity(tool)
        engine._downtime.start_pending_pm_if_idle(tool)
    
