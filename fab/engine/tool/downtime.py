"""处理Tool因为PM或者随机因素的停机"""
from collections import deque

from fab.engine.events import EventKind, SimulationEvent
from fab.sampling import sample_duration

class Downtime:
    def __init__(self,engine):
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

        engine._begin_downtime(event, EventKind.PM_COMPLETE)

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
                duration=sample_duration(engine.rng, pm.repair_distribution),
                reason=f"pm:{pm.event_name}",
                pm_event=pm.event_name,
            )