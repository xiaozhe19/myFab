"""Daily Fab statistics, using event callbacks without storing individual events."""

from __future__ import annotations

import math
from statistics import mean

from fab.plugins.base import FabPlugin
from fab.plugins.storage import ResultStorage


class DailyMetricsPlugin(FabPlugin):
    """Time-weighted WIP/Ready Queue and cycle times, all in minutes.

    Ordinary days use [start,end); midnight events belong to the next day.
    The final interval includes simulation_end so final events are not lost.
    """

    def __init__(self, day_minutes=1440.0):
        if not math.isfinite(day_minutes) or day_minutes <= 0:
            raise ValueError("day_minutes must be finite and positive")
        self.day_minutes = float(day_minutes)
        self.engine = None
        self.model = None
        self._storage = None
        self._finalized = False

    def bind_engine(self, engine):
        self.engine = engine

    def required_services(self):
        return ("result_storage",)

    def bind_services(self, services):
        storage = services["result_storage"]
        if not isinstance(storage, ResultStorage):
            raise TypeError("result_storage must implement ResultStorage")
        self._storage = storage

    def on_simulation_started(self, model):
        if self.engine is None or self._storage is None:
            raise RuntimeError("Bind the engine and result_storage before starting DailyMetricsPlugin")
        if self.engine.model is not model or model.time_unit != "minute":
            raise ValueError("DailyMetricsPlugin requires the bound model and minute units")
        if self.engine.current_time != model.simulation.start_time:
            raise ValueError("Daily collection must start at the model's start_time")
        self.model = model
        self.day_start_time = model.simulation.start_time
        self.last_time = self.day_start_time
        self.next_day_time = self.day_start_time + self.day_minutes
        self.current_wip = sum(lot.end_time is None for lot in self.engine.lots)
        self.start_wip = self.current_wip
        self.start_queue = len(self.engine._ready_group_by_lot)
        self.wip_area = 0.0
        self.queue_area = 0.0
        self.daily_released = 0
        self.daily_completed = 0
        self.fab_cycle_times = []
        self.end_to_end_cycle_times = []
        self.days_written = 0
        self._finalized = False
        self._storage.append_extension_data(
            "daily_metrics", "schema",
            {"version": 1, "day_minutes": self.day_minutes,
             "start_time": self.day_start_time, "end_time": model.simulation.end_time,
             "measurement_start_time": self.day_start_time + model.simulation.warmup_time,
             "intervals": "[start,end); final interval includes simulation_end",
             "averages": "Time-weighted by interval duration; WIP includes transport",
             "queue": "Ready Queue only; len(engine._ready_group_by_lot)",
             "fab_cycle_time": "completion_time - actual Fab release_time; completed Lots only",
             "end_to_end_cycle_time": "completion_time - generation_time; includes release-pool wait"},
            self.day_start_time,
        )

    def _advance_time(self, current_time):
        if self.model is None or self._finalized:
            raise RuntimeError("DailyMetricsPlugin is not running")
        if (not math.isfinite(current_time) or current_time < self.last_time
                or current_time > self.model.simulation.end_time):
            raise ValueError("Daily metrics time must be finite, nondecreasing and within the run")
        # Engine calls time_advanced before events/actions at current_time.
        # The queue index therefore describes the interval which just elapsed.
        queue = len(self.engine._ready_group_by_lot)
        while current_time >= self.next_day_time and self.next_day_time < self.model.simulation.end_time:
            elapsed = self.next_day_time - self.last_time
            self.wip_area += self.current_wip * elapsed
            self.queue_area += queue * elapsed
            self.last_time = self.next_day_time
            self._write_day(self.next_day_time, queue, final=False)
            self.next_day_time += self.day_minutes
        elapsed = current_time - self.last_time
        self.wip_area += self.current_wip * elapsed
        self.queue_area += queue * elapsed
        self.last_time = current_time

    def on_time_advanced(self, current_time, lots):
        self._advance_time(current_time)

    def on_lot_released(self, current_time, lot):
        self._advance_time(current_time)
        self.current_wip += 1
        self.daily_released += 1

    def on_lot_completed(self, current_time, lot):
        self._advance_time(current_time)
        fab_cycle = current_time - lot.release_time
        end_to_end_cycle = current_time - lot.generation_time
        if (not math.isfinite(fab_cycle) or not math.isfinite(end_to_end_cycle)
                or fab_cycle < 0 or end_to_end_cycle < 0 or self.current_wip < 1):
            raise ValueError("Invalid completed-Lot timestamps or WIP count")
        self.current_wip -= 1
        self.daily_completed += 1
        self.fab_cycle_times.append(fab_cycle)
        self.end_to_end_cycle_times.append(end_to_end_cycle)

    @staticmethod
    def _p95(values):
        if not values:
            return None
        ordered = sorted(values)
        position = 0.95 * (len(ordered) - 1)
        left = int(position)
        right = min(left + 1, len(ordered) - 1)
        return ordered[left] + (ordered[right] - ordered[left]) * (position - left)

    def _write_day(self, end_time, queue, *, final):
        duration = end_time - self.day_start_time
        if duration <= 0:
            return
        if self.current_wip != self.start_wip + self.daily_released - self.daily_completed:
            raise RuntimeError("Daily WIP balance failed")
        measurement_start = self.model.simulation.start_time + self.model.simulation.warmup_time
        phase = ("warmup" if end_time <= measurement_start else
                 "collection" if self.day_start_time >= measurement_start else "mixed")
        payload = {
            "day": self.days_written + 1, "start_time": self.day_start_time, "end_time": end_time,
            "duration_minutes": duration, "partial_day": not math.isclose(duration, self.day_minutes),
            "end_inclusive": final, "phase": phase,
            "average_wip": self.wip_area / duration, "average_queue": self.queue_area / duration,
            "wip_start": self.start_wip, "wip_end": self.current_wip,
            "queue_start": self.start_queue, "queue_end": queue,
            "released": self.daily_released, "completed": self.daily_completed,
            "mean_fab_cycle_time_minutes": mean(self.fab_cycle_times) if self.fab_cycle_times else None,
            "p95_fab_cycle_time_minutes": self._p95(self.fab_cycle_times),
            "mean_end_to_end_cycle_time_minutes": (
                mean(self.end_to_end_cycle_times) if self.end_to_end_cycle_times else None),
        }
        self._storage.append_extension_data("daily_metrics", "daily_summary", payload, end_time)
        self.days_written += 1
        self.day_start_time = end_time
        self.start_wip = self.current_wip
        self.start_queue = queue
        self.wip_area = 0.0
        self.queue_area = 0.0
        self.daily_released = 0
        self.daily_completed = 0
        self.fab_cycle_times.clear()
        self.end_to_end_cycle_times.clear()

    def build_result(self, context):
        if self.model is None or context.model is not self.model or not self.engine._done:
            raise RuntimeError("Daily metrics finalization requires the finished bound simulation")
        if not self._finalized:
            if self.engine.current_time != self.model.simulation.end_time:
                raise RuntimeError("Simulation ended before its configured daily-metrics horizon")
            self._advance_time(self.model.simulation.end_time)
            self._write_day(self.model.simulation.end_time, len(self.engine._ready_group_by_lot), final=True)
            self._finalized = True
        return {"daily_metrics": {"days": self.days_written, "day_minutes": self.day_minutes}}
