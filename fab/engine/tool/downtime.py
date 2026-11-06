"""处理Tool因为PM或者随机因素的停机"""
from collections import deque

from fab.engine.index import dispatch as dispatch_index
from fab.engine.events import EventKind, SimulationEvent
from fab.sampling import sample_duration
from fab.model.entities import ToolState,PreventiveMaintenanceSpec

# 活动里以绝对时刻登记、停机恢复时需要顺延的字段。
_SHIFTED_ON_RESUME = ("process_start", "process_end")


class Downtime:
    def __init__(self,engine) -> None:
        self.engine = engine

    def handle_pm_due(self, event:SimulationEvent) -> None:
        engine = self.engine

        tool = engine.tool_states[str(event.payload["tool_id"])]
        self.schedule_next_calendar_pm(event)
        if tool.is_down or engine._tool_has_activities(tool.tool_id):
            #pms是一个字典 保存每台设备好没有执行的事件队列
            #如果已经停机了 或者有任务 那么就等执行完毕之后停机
            engine._pending_pms.setdefault(tool.tool_id, deque()).append(event)
            return

        self.begin_downtime(event, EventKind.PM_COMPLETE)

    def schedule_next_calendar_pm(self, event: SimulationEvent) -> None:
        """time-based PM 按日历循环：MTBPM 是相邻两次到期的间隔，已包含 PM 时长，
        设备忙导致的推迟也不会顺延后续 PM。"""
        engine = self.engine
        pm = engine._pms_by_name.get(str(event.payload.get("pm_event")))
        if pm is None or pm.pm_type != "time_based":
            return
        next_time = engine.current_time + pm.mean_time_before_pm
        if next_time < engine.model.simulation.end_time:
            engine.schedule(
                next_time,
                EventKind.PM_DUE,
                tool_id=event.payload["tool_id"],
                duration=sample_duration(
                    engine.tool_rng(str(event.payload["tool_id"]), f"pm:{pm.event_name}"),
                    pm.repair_distribution,
                ),
                reason=f"pm:{pm.event_name}",
                pm_event=pm.event_name,
            )

    def start_pending_pm_if_idle(self, tool: ToolState) -> bool:
        """在Tool空闲时推进PM"""
        if tool.is_down or self.engine._tool_has_activities(tool.tool_id):
            return False
        pending = self.engine._pending_pms.get(tool.tool_id)
        if not pending:
            return False
        event = pending.popleft()
        if not pending:
            del self.engine._pending_pms[tool.tool_id]
        self.begin_downtime(event, EventKind.PM_COMPLETE)
        return True
    
    def begin_downtime(self, event: SimulationEvent, completion: EventKind) -> None:
        engine = self.engine
        
        tool = engine.tool_states[str(event.payload["tool_id"])]
        if tool.is_down:
            engine._pending_downtimes.setdefault(tool.tool_id, deque()).append(
                (event, completion)
            )
            return
        
        paused: list[dict[str, object]] = []
        # 只取出当前设备的活动；移除后，旧完成事件将无法再匹配到活动。
        activities = engine._active.pop(tool.tool_id, {})
        for active in activities.values():
            active["remaining"] = max(
                float(active["finish"]) - engine.current_time,
                0.0,
            )
            # cascading 第二槽位的开放时刻随停机一起顺延。
            active["next_start_remaining"] = max(
                float(active.get("next_start", active["finish"])) - engine.current_time,
                0.0,
            )
            active["paused_at"] = engine.current_time
            paused.append(active)
        if paused:
            # 一次停机使该设备所有旧 token 失效。
            engine._tokens[tool.tool_id] += 1
            engine._paused[tool.tool_id] = paused
        tool.active_count = 0
        tool.in_process = False
        tool.active_lot_id = None
        tool.active_step_id = None
        dispatch_index.mark_busy(engine, tool)

        self.start_downtime(tool, event, completion)

    def start_downtime(
        self,
        tool: ToolState,
        event: SimulationEvent,
        completion: EventKind,
    ) -> None:
        """启动一项停机；调用方负责保存正在进行的加工。"""
        engine = self.engine
        tool.is_down = True
        tool.is_in_setup = False
        tool.down_reason = str(event.payload.get("reason", event.kind.value))
        duration = float(event.payload.get("duration", 0.0))
        tool.available_time = engine._time(engine.current_time + duration)
        engine.schedule(
            tool.available_time,
            completion,
            tool_id=tool.tool_id,
            breakdown=event.payload.get("breakdown"),
            pm_event=event.payload.get("pm_event"),
        )

    def end_downtime(self, event: SimulationEvent) -> None:
            engine = self.engine

            tool = engine.tool_states[str(event.payload["tool_id"])]
            if event.kind is EventKind.REPAIR_COMPLETE and event.payload.get("breakdown"):
                name = str(event.payload["breakdown"])
                breakdown = engine._breakdowns_by_name[name]
                rng = engine.tool_rng(tool.tool_id, f"bd:{name}")
                engine.schedule(
                    engine.current_time
                    + sample_duration(rng, breakdown.time_to_failure),
                    EventKind.BREAKDOWN,
                    tool_id=tool.tool_id,
                    duration=sample_duration(rng, breakdown.time_to_repair),
                    reason=f"breakdown:{name}",
                    breakdown=name,
                )
    
            # 当前停机结束后，优先执行维修期间积压的停机。设备只有在队列清空后
            # 才恢复加工，避免 PM/故障重叠时其中一个事件被静默丢弃。
            #TODO：目前的语义是如果在这次停机中还会发生停机 那么就加长这次停机的时间，但是现实是这样吗？
            #TODO：用AutoSched AP 构建的也有这个问题 Fast Simulations of Large-Scale Highly Congested Systems

            pending = engine._pending_downtimes.get(tool.tool_id)
            if pending:
                next_event, next_completion = pending.popleft()
                if not pending:
                    del engine._pending_downtimes[tool.tool_id]
                self.start_downtime(tool, next_event, next_completion)
                return
    
            tool.is_down = False
            tool.down_reason = None
            tool.available_time = engine.current_time
            if self.start_pending_pm_if_idle(tool):
                return
            paused_activities = engine._paused.pop(tool.tool_id, [])
            if not paused_activities:
                engine._refresh_tool_capacity(tool)
                return
    
            for paused in paused_activities:
                # 停机前尚未到达的加工开始/结束时刻顺延停机时长；已过去的保持不变。
                paused_at = float(paused["paused_at"])
                shift = engine.current_time - paused_at
                for key in _SHIFTED_ON_RESUME:
                    value = paused.get(key)
                    if value is not None and float(value) > paused_at:
                        paused[key] = engine._time(float(value) + shift)
                lot = engine._lot(str(paused["lot_id"]))
                completion = paused["completion"]
                if not isinstance(completion, EventKind):
                    raise RuntimeError("暂停活动缺少完成事件类型。")
                payload = {
                    key: value
                    for key, value in paused.items()
                    if key
                    not in {
                        "token",
                        "completion",
                        "lot_id",
                        "step",
                        "finish",
                        "phase_start",
                        "remaining",
                        "next_start",
                        "next_start_remaining",
                        "paused_at",
                        "batch_lots",
                    }
                }
                engine._process.start_phase(
                    tool,
                    lot,
                    completion,
                    float(paused["remaining"]),
                    batch_lots=paused.get("batch_lots"),
                    next_start=engine.current_time
                    + float(paused.get("next_start_remaining", paused["remaining"])),
                    **payload,
                )

    def matching_tools(self, valid_for_type: str, type_name: str) -> list[ToolState]:
        """找到维护/故障规则适用的设别
            valid_for_type: 'tool', 'toolgroup', 'area'
            type_name:具体的tool/toolgroup/area
        """
        engine = self.engine
        key = (valid_for_type, type_name)
        cached = engine._matching_tools_cache.get(key)
        if cached is not None:
            return cached
        kind = valid_for_type.lower()
        if kind == "tool":
            result = [
                tool for tool in engine.tool_states.values() if tool.tool_id == type_name
            ]
        elif kind == "toolgroup":
            result = [
                tool
                for tool in engine.tool_states.values()
                if engine.model.tools[tool.tool_id].tool_group_id == type_name
            ]
        elif kind == "area":
            result = [
                tool
                for tool in engine.tool_states.values()
                if (group := engine._tool_group(tool)) is not None
                and group.area == type_name
            ]
        else:
            raise ValueError(f"未知维护/故障适用范围：{valid_for_type}。")
        # 优化：结果只依赖静态模型，缓存复用。
        engine._matching_tools_cache[key] = result
        return result

    def first_pm_offsets(
        self, pm: PreventiveMaintenanceSpec, tools: list[ToolState]
    ) -> list[float]:
        """计算第一次PM什么时候触发
            pm: 一条PM配置
            tools: 配置适用的设备列表
        """
        engine = self.engine
        fallback = (
            pm.first_one_at if pm.first_one_at is not None else pm.mean_time_before_pm
        )
        distribution = pm.first_one_at_distribution
        if distribution is not None and distribution.kind.lower() not in {
            "constant",
            "deterministic",
        }:
            return [
                sample_duration(
                    engine.tool_rng(tool.tool_id, f"pm:{pm.event_name}"),
                    distribution,
                    fallback,
                )
                for tool in tools
            ]
        foa = distribution.mean if distribution is not None else fallback
        rank = {
            tool.tool_id: k
            for k, tool in enumerate(sorted(tools, key=lambda t: t.tool_id), 1)
        }
        return [rank[tool.tool_id] * foa / len(tools) for tool in tools]

            
    def schedule_calendar_maintenance(self) -> None:
        """安排时间型PM
        """
        engine = self.engine
        for pm in engine.model.preventive_maintenance:
            if pm.pm_type != "time_based":
                continue
            tools = self.matching_tools(pm.valid_for_type, pm.type_name)
            for tool, first in zip(tools, self.first_pm_offsets(pm, tools)):
                engine.schedule(
                    engine.current_time + first,
                    EventKind.PM_DUE,
                    tool_id=tool.tool_id,
                    duration=sample_duration(
                        engine.tool_rng(tool.tool_id, f"pm:{pm.event_name}"),
                        pm.repair_distribution,
                    ),
                    reason=f"pm:{pm.event_name}",
                    pm_event=pm.event_name,
                )

    def schedule_counter_maintenance(self, tool: ToolState) -> None:
        """为当前设备检查并安排已达到计数阈值的 PM。"""
        engine = self.engine
        for pm in engine._counter_pms_by_tool.get(tool.tool_id, ()):
            key = (tool.tool_id, pm.event_name)
            threshold = engine._next_counter_pm[key]
            while tool.processed_wafers >= threshold:
                engine.schedule(
                    engine.current_time,
                    EventKind.PM_DUE,
                    tool_id=tool.tool_id,
                    duration=sample_duration(
                        engine.tool_rng(tool.tool_id, f"pm:{pm.event_name}"),
                        pm.repair_distribution,
                    ),
                    reason=f"pm:{pm.event_name}",
                    pm_event=pm.event_name,
                )
                threshold += pm.mean_time_before_pm
            engine._next_counter_pm[key] = threshold

    def schedule_breakdowns(self) -> None:
        """安排各故障规则的首次停机事件。"""
        engine = self.engine
        for breakdown in engine.model.breakdowns:
            for tool in self.matching_tools(
                breakdown.valid_for_type, breakdown.type_name
            ):
                rng = engine.tool_rng(tool.tool_id, f"bd:{breakdown.event_name}")
                if breakdown.first_one_at_distribution is not None:
                    first = sample_duration(rng, breakdown.first_one_at_distribution)
                elif breakdown.first_one_at is not None:
                    first = breakdown.first_one_at
                else:
                    first = sample_duration(rng, breakdown.time_to_failure)
                engine.schedule(
                    engine.current_time + first,
                    EventKind.BREAKDOWN,
                    tool_id=tool.tool_id,
                    duration=sample_duration(rng, breakdown.time_to_repair),
                    reason=f"breakdown:{breakdown.event_name}",
                    breakdown=breakdown.event_name,
                )
    
