"""单个设备加工流程"""

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
                engine._remove_ready(member)
                member.in_process = True
                member.processing_tool_id = tool.tool_id
                member.start_time = (
                    member.start_time
                    if member.start_time is not None
                    else engine.current_time
                )
            duration = sample_duration(
                engine.rng, step.processing_distribution, step.process_time
            )
            self.start_phase(
                tool,
                lot,
                EventKind.OPERATION_COMPLETE,
                lead_time + duration + unloading,
                process_start=process_start,
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
                    engine.rng, step.processing_distribution, step.process_time
                )
                + (n - 1) * step.cascading_interval
            )
        else:
            duration = sample_duration_sum(
                engine.rng,
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
            process_time=duration,
            step_index=lot.operation_index,
            activity_token=activity_token,
            next_start=next_start,
        )
    
