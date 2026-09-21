"""SMT2020 事件驱动引擎。

引擎只维护物理状态、事件日历和动作合法性；任何 Lot/设备选择都由策略给出。
"""

from __future__ import annotations

import heapq
import random
import re
from bisect import bisect_right
from collections import deque
from itertools import count
from typing import Callable

from fab.engine.eligibility import (
    tool_is_available,
)
from fab.engine.events import EVENT_PRIORITY, EventKind, SimulationEvent
from fab.model.entities import (
    BreakdownSpec,
    DistributionSpec,
    FabModel,
    LotState,
    PreventiveMaintenanceSpec,
    RouteStepSpec,
    ToolGroupSpec,
    ToolState,
)
from fab.plugins import PluginManager, PluginResultContext
from fab.order_source import LotSource, create_lot_source
from fab.sampling import sample_duration, sample_duration_sum
from fab.strategy import (
    DispatchableCandidate,
    FabStrategy,
    StrategyDecision,
    StrategyState,
)


class FabEngine:
    """执行客观事件；策略决定 release 与 dispatch。"""

    _TIME_DIGITS = 6

    def __init__(
        self,
        model: FabModel,
        order_seed: int,
        progress_callback: Callable[[float, float], None] | None = None,
        *,
        batch_from_strategy: bool = False,
        plugin_manager: PluginManager | None = None,
    ) -> None:
        if model.time_unit != "minute":
            raise ValueError("FabEngine 只接受以 minute 为规范时间单位的 FabModel。")
        self._batch_from_strategy = batch_from_strategy
        # 策略主动组批的分组（StrategyDecision.batches）；None 表示引擎自主组批。
        self._pending_batches: dict[str, list[LotState]] | None = None
        # 优化：setup 最小连续加工量映射 (current_setup, new_setup) -> minimal_run_length，
        # 避免 setup_change_allowed 每次遍历全部 setup_transitions。
        self._setup_min_run: dict[tuple[str, str], int] = {
            (t.current_setup, t.new_setup): t.minimal_run_length
            for t in model.setup_transitions
        }
        self._setup_transition_by_pair = {}
        for transition in model.setup_transitions:
            self._setup_transition_by_pair.setdefault(
                (transition.current_setup, transition.new_setup), transition
            )
        self.model = model
        # 事件日历和 ToolState.available_time 必须使用同一精度。否则两者只差
        # 极小浮点误差时，空闲索引会认为设备可派工，而最终合法性校验会拒绝它。
        self.current_time = self._time(model.simulation.start_time)
        self.rng = random.Random(order_seed)
        self.tool_states: dict[str, ToolState] = {}
        for spec in model.tools.values():
            group = model.tool_groups.get(spec.tool_group_id or "")
            capacity = (
                max(1, group.cascading_capacity)
                if group is not None and group.cascading_tool
                else 1
            )
            self.tool_states[spec.id] = ToolState(
                spec.id,
                spec.name,
                spec.process,
                self._time(spec.available_from),
                capacity=capacity,
            )
        # (process, visit) 只由路线决定；预计算可避免每次完工都扫描路线前缀来
        # 计算该工艺是产品路线中的第几次访问。
        self._operation_keys_by_product = {
            product_id: self._operation_keys(product.route)
            for product_id, product in model.products.items()
        }
        self._route_step_index_by_product: dict[str, dict[str, int]] = {}
        for product_id, product in model.products.items():
            indexes: dict[str, int] = {}
            for index, step in enumerate(product.route):
                if step.id is not None:
                    indexes.setdefault(step.id, index)
            self._route_step_index_by_product[product_id] = indexes
        self._transport_by_location: dict[
            tuple[str | None, str | None], DistributionSpec
        ] = {}
        for rule in model.transport_rules:
            # 旧实现用 next() 取第一条匹配规则；setdefault 保留这一语义，同时把
            # 频繁的运输规则线性查找改为按地点对的 O(1) 查表。
            self._transport_by_location.setdefault(
                (rule.from_location, rule.to_location), rule.transport_time
            )
        self.lots: list[LotState] = []
        self._lots_by_id: dict[str, LotState] = {}
        self._changed_lot_ids: set[str] = set()
        # 当前可派工的 lot，按工具组增量维护。列表始终按进入 fab 的顺序，
        # 因而保持旧版扫描 ``self.lots`` 时的候选顺序。
        self._ready_lots_by_group: dict[str, list[LotState]] = {}
        self._ready_group_by_lot: dict[str, str] = {}
        self._lot_order: dict[str, int] = {}
        self.order_source: LotSource = create_lot_source(model, order_seed)
        self._calendar: list[SimulationEvent] = []
        self._sequence = count()
        self._tokens: dict[str, int] = {tool_id: 0 for tool_id in self.tool_states}
        self._active: dict[tuple[str, int], dict[str, object]] = {}
        self._paused: dict[str, list[dict[str, object]]] = {}
        self._scheduled_tool_available: set[tuple[str, float]] = set()
        # 同一设备可能在维修期间再次收到 PM/故障事件。不能丢弃这些事件，
        # 而是按到达顺序逐项停机，全部完成后再恢复被中断的加工。
        self._pending_downtimes: dict[
            str, deque[tuple[SimulationEvent, EventKind]]
        ] = {}
        self._pending_pms: dict[str, deque[SimulationEvent]] = {}
        self._last_operation_completion: dict[tuple[str, int], float] = {}
        self._strategy: FabStrategy | None = None
        self._pending: StrategyDecision | None = None
        # ready lot 或 idle tool 的出现意味着合法派工动作可能增加。这个标记只
        # 表示需要询问策略，不代表引擎替策略选择或执行任何派工动作。
        self._dispatch_needed = False
        self._done = False
        self._result: dict[str, object] | None = None
        self._progress_callback = progress_callback
        self._last_progress = -1.0
        # 增量维护空闲设备 ID；派工候选索引据此只重建发生变化的工具组。
        self._idle_tools_by_group: dict[str, set[str]] = {}
        self._tool_group_by_id = {
            tool_id: spec.tool_group_id or ""
            for tool_id, spec in self.model.tools.items()
        }
        tool_ids_by_group: dict[str, list[str]] = {}
        for tool_id, group_id in self._tool_group_by_id.items():
            tool_ids_by_group.setdefault(group_id, []).append(tool_id)
        self._tool_ids_by_group = {
            group_id: tuple(tool_ids)
            for group_id, tool_ids in tool_ids_by_group.items()
        }
        # 候选表只会受某一工具组中的 ready lot 或 idle tool 变化影响。保留上一次
        # 结果，并在状态变化时仅重建对应工具组，避免每个运输/卸载事件都扫描全厂。
        self._eligible_lots_by_group: dict[str, dict[str, tuple[LotState, ...]]] = {}
        self._dispatchable_candidates_cache: tuple[DispatchableCandidate, ...] = ()
        self._candidate_groups_dirty: set[str] = set()
        # 优化：_matching_tools 的匹配结果只依赖静态模型（tools/tool_groups），
        # 与仿真状态无关，缓存后避免每次加工完成时重复扫描全部设备。
        self._matching_tools_cache: dict[tuple[str, str], list[ToolState]] = {}
        self._breakdowns_by_name: dict[str, BreakdownSpec] = {}
        for breakdown in self.model.breakdowns:
            self._breakdowns_by_name.setdefault(breakdown.event_name, breakdown)
        self._pms_by_name: dict[str, PreventiveMaintenanceSpec] = {}
        for pm in self.model.preventive_maintenance:
            self._pms_by_name.setdefault(pm.event_name, pm)
        # wafer-count PM 的适用范围是静态的。初始化时反向建立 tool -> PM 规则
        # 索引，使每次工序完成后只检查当前设备自己的规则。
        counter_pms_by_tool: dict[str, list[PreventiveMaintenanceSpec]] = {}
        for pm in self.model.preventive_maintenance:
            if pm.pm_type != "wafer_count":
                continue
            for tool in self._matching_tools(pm.valid_for_type, pm.type_name):
                counter_pms_by_tool.setdefault(tool.tool_id, []).append(pm)
        self._counter_pms_by_tool: dict[str, tuple[PreventiveMaintenanceSpec, ...]] = {
            tool_id: tuple(rules) for tool_id, rules in counter_pms_by_tool.items()
        }
        # processed_wafers 保留为永不归零的累计量。每条 PM 规则独立保存
        # 下一个触发阈值，避免短周期 PM 把长周期 PM 的计数清掉。
        self._next_counter_pm: dict[tuple[str, str], float] = {}
        for tool_id, rules in self._counter_pms_by_tool.items():
            for pm in rules:
                first = sample_duration(
                    self.rng,
                    pm.first_one_at_distribution,
                    pm.first_one_at
                    if pm.first_one_at is not None
                    else pm.mean_time_before_pm,
                )
                self._next_counter_pm[(tool_id, pm.event_name)] = first
        self._plugin_manager = (
            plugin_manager if plugin_manager is not None else PluginManager()
        )

    def schedule(self, time: float, kind: EventKind, **payload: object) -> None:
        """安排未来事件；日历排序由 SimulationEvent 定义。"""

        time = self._time(time)
        if time < self.current_time:
            raise ValueError("不能安排过去事件。")
        heapq.heappush(
            self._calendar,
            SimulationEvent(
                time,
                EVENT_PRIORITY[kind],
                next(self._sequence),
                kind,
                payload,
            ),
        )

    @classmethod
    def _time(cls, value: float) -> float:
        """规范化事件与设备可用时刻，消除浮点尾差导致的非法派工。"""

        return round(value, cls._TIME_DIGITS)

    @staticmethod
    def _operation_keys(
        route: tuple[RouteStepSpec, ...],
    ) -> tuple[tuple[str, int], ...]:
        visits: dict[str, int] = {}
        keys: list[tuple[str, int]] = []
        for step in route:
            visits[step.process] = visits.get(step.process, 0) + 1
            keys.append((step.process, visits[step.process]))
        return tuple(keys)

    def begin_episode(self, strategy: FabStrategy) -> None:
        if self._strategy is not None:
            raise RuntimeError("一个引擎实例只能运行一次。")
        self._strategy = strategy
        strategy.initialize(self.model)
        self._plugin_manager.on_simulation_started(self.model)
        # 优化：初始空闲设备集合 = 初始可用（未占用、未故障、已到可用时刻）的设备。
        for tool in self.tool_states.values():
            if tool_is_available(tool, self.current_time):
                self._mark_idle(tool)
        for event in self.order_source.initial_events(
            self.current_time,
            self.model.simulation.end_time,
            self.model.simulation.release_interval,
        ):
            self.schedule(event.time, event.kind, **event.payload)
        self.schedule(self.model.simulation.end_time, EventKind.SIMULATION_END)
        self._schedule_calendar_maintenance()
        self._schedule_breakdowns()

    def run(self, strategy: FabStrategy) -> dict[str, object]:
        self.begin_episode(strategy)
        while self.advance_until_release() is not None:
            self.apply_release_action(None)
        return self.finish_episode()

    def advance_until_release(self) -> StrategyState | None:
        """推进客观事件，直到下一次需要外部 release 动作的时刻。"""

        if self._pending is not None:
            return self._state(True)

        while self._calendar:
            self.current_time = self._calendar[0].time
            self._report_progress()
            if self._plugin_manager.has_time_advanced_plugins:
                self._plugin_manager.on_time_advanced(self.current_time, self.lots)
            event_kinds: list[str] = []
            reasons: list[str] = []
            release_opportunity = False
            forced_decision = False
            simulation_end = False

            # 同一时刻内，新事件（例如 operation -> unload）也必须在
            # SIMULATION_END 之前依照 priority 结算，不能遗留到下一轮。
            while self._calendar and self._calendar[0].time == self.current_time:
                event = heapq.heappop(self._calendar)
                event_kinds.append(event.kind.value)
                event_release, event_forced = self._handle_event(event, reasons)
                release_opportunity |= event_release
                forced_decision |= event_forced
                simulation_end |= event.kind is EventKind.SIMULATION_END

            if simulation_end:
                self._done = True
                return None
            if release_opportunity:
                # 投料决策使用的同一份状态也包含当前派工候选，因此同时消费此前
                # 累积的派工通知；是否真的派工仍完全由策略决定。
                self._dispatch_needed = False
                state = self._state(True, event_kinds, reasons)
                self._pending = self._decide(state, "release")
                return state

            if self._dispatch_needed or forced_decision:
                # ready/idle 状态变化只说明候选可能增加。先更新候选索引；若确实
                # 没有合法动作则无需调用策略。显式 wakeup/batch 事件必须调用。
                dispatchable_candidates = self._candidate_index()
                self._dispatch_needed = False
                if not forced_decision and not dispatchable_candidates:
                    continue
                decision = self._decide(
                    self._state(
                        False,
                        event_kinds,
                        reasons,
                        dispatchable_candidates=dispatchable_candidates,
                    ),
                    "dispatch",
                )
                self._apply_decision(decision)

        self._done = True
        return None

    def apply_release_action(self, release_lot: bool | None) -> None:
        """执行策略的 release 决定，或由 RL 覆盖该布尔动作。"""

        if self._pending is None:
            raise RuntimeError("当前没有投料决策点。")
        decision, self._pending = self._pending, None
        if release_lot is not None:
            if decision.release_lot_id is not None:
                raise ValueError(
                    "定向投料必须由策略返回 lot ID，不能用布尔 RL 动作覆盖。"
                )
            decision.release_lot = bool(release_lot)
        self._apply_decision(decision)
        self._schedule_wakeups(decision)

        if not decision.release_lot and decision.release_lot_id is None:
            return
        lot = self.order_source.release(self.current_time, decision.release_lot_id)
        if lot is None:
            if decision.release_lot_id is not None:
                raise ValueError(
                    f"定向投料引用了不在订单池中的 lot：{decision.release_lot_id}。"
                )
            return
        self.lots.append(lot)
        self._lots_by_id[lot.id] = lot
        self._lot_order[lot.id] = len(self.lots)
        self._plugin_manager.on_lot_released(self.current_time, lot)
        if self._advance_over_unsampled_steps(lot):
            self._mark_ready(lot)
        else:
            self._complete_lot(lot)
        # 新 Lot 入厂后由策略再次决定派工；引擎不替它选设备或 Lot。
        self._dispatch_needed = False
        follow_up = self._decide(self._state(False), "follow_up")
        if follow_up.release_lot or follow_up.release_lot_id is not None:
            raise ValueError("策略只能在 LOT_RELEASE 事件中请求投料。")
        self._apply_decision(follow_up)
        self._schedule_wakeups(follow_up)

    def _decide(self, state: StrategyState, kind: str) -> StrategyDecision:
        """请求一次策略决策，并统一通知决策观察插件。"""

        decision = self._runtime().decide(state)
        if self._plugin_manager.has_decision_plugins:
            self._plugin_manager.on_decision_made(self.current_time, kind, decision)
        return decision

    def finish_episode(self) -> dict[str, object]:
        strategy = self._runtime()
        if not self._done:
            raise RuntimeError("仿真尚未结束。")
        if self._result is None:
            context = PluginResultContext(
                strategy_name=strategy.name,
                model=self.model,
                lots=self.lots,
                tools=self.tool_states,
                release_pool_lots_at_end=len(self.order_source.queue),
            )
            plugin_result = self._plugin_manager.build_result(context)
            self._result = {
                "strategy": strategy.name,
                "factory_id": self.model.id,
                "time_unit": self.model.time_unit,
            }
            duplicated_keys = self._result.keys() & plugin_result.keys()
            if duplicated_keys:
                duplicated = ", ".join(sorted(duplicated_keys))
                raise ValueError(f"插件不能覆盖 Engine 核心结果字段：{duplicated}。")
            self._result.update(plugin_result)
            self._plugin_manager.on_simulation_finished(context)
        return self._result

    def _handle_event(
        self, event: SimulationEvent, reasons: list[str]
    ) -> tuple[bool, bool]:
        """结算一条客观事件，返回（投料机会，强制策略唤醒）。"""

        kind = event.kind
        forced_decision = kind in {
            EventKind.STRATEGY_WAKEUP,
            EventKind.BATCH_READY,
        }
        source_result = self.order_source.handle_event(
            self.current_time,
            kind,
            event.payload,
            self.model.simulation.end_time,
            self.model.simulation.release_interval,
        )
        if source_result is not None:
            for next_event in source_result.next_events:
                self.schedule(next_event.time, next_event.kind, **next_event.payload)
            return (
                source_result.release_opportunity,
                forced_decision,
            )
        if kind is EventKind.LOAD_COMPLETE:
            self._after_load(event)
        elif kind is EventKind.SETUP_COMPLETE:
            self._after_setup(event)
        elif kind is EventKind.OPERATION_COMPLETE:
            self._after_operation(event)
        elif kind is EventKind.UNLOAD_COMPLETE:
            self._after_unload(event)
        elif kind is EventKind.TRANSPORT_COMPLETE:
            self._after_transport(event)
        elif kind is EventKind.TOOL_AVAILABLE:
            tool_id = str(event.payload["tool_id"])
            self._scheduled_tool_available.discard((tool_id, event.time))
            tool = self.tool_states[tool_id]
            self._refresh_tool_capacity(tool)
        elif kind is EventKind.BREAKDOWN:
            self._begin_downtime(event, EventKind.REPAIR_COMPLETE)
        elif kind is EventKind.REPAIR_COMPLETE:
            self._end_downtime(event)
        elif kind is EventKind.PM_DUE:
            self._handle_pm_due(event)
        elif kind is EventKind.PM_COMPLETE:
            self._end_downtime(event)
        elif kind is EventKind.REWORK_ROUTED:
            self._route_rework(event)
        elif kind is EventKind.SAMPLING_DECISION:
            self._schedule_transport_for_lot(str(event.payload["lot_id"]))
        elif kind is EventKind.CQT_EXPIRE:
            self._record_cqt_violation(event)
        elif kind is EventKind.STRATEGY_WAKEUP:
            reasons.append(str(event.payload.get("reason", "strategy_wakeup")))
        elif kind is EventKind.BATCH_READY:
            # 批量组成由策略返回的动作决定；引擎只提供此唤醒事件。
            reasons.append("batch_ready")
        return False, forced_decision

    def _apply_decision(self, decision: StrategyDecision) -> None:
        """验证策略给出的配对，并启动该配对的物理事件链。"""

        if decision.batches:
            if self._pending_batches is None:
                self._pending_batches = {}
            self._pending_batches.update(decision.batches)
        assigned: set[str] = set()
        for tool_id, lot in decision.dispatches.items():
            tool = self.tool_states.get(tool_id)
            if tool is None or lot.id in assigned:
                raise ValueError(f"非法派工：{lot.id} -> {tool_id}。")
            if not self._is_eligible(tool, lot):
                raise ValueError(f"非法派工：{lot.id} -> {tool_id}。")
            step = lot.current_step
            if step is not None and step.processing_unit == "batch":
                self._reserve_batch(tool, lot)
            self._remove_ready(lot)
            lot.in_process, lot.processing_tool_id = True, tool_id
            lot.start_time = (
                lot.start_time if lot.start_time is not None else self.current_time
            )
            tool.current_product_id = lot.product_id
            self._start_phase(
                tool, lot, EventKind.LOAD_COMPLETE, self._loading_time(tool)
            )
            assigned.add(lot.id)

    def _start_phase(
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
        """占用设备并安排当前物理阶段的完成事件。batch 工步可传入整炉 lot 列表。"""

        finish = self._time(self.current_time + max(duration, 0.0))
        if activity_token is None:
            self._tokens[tool.tool_id] += 1
            token = self._tokens[tool.tool_id]
            active: dict[str, object] = {}
        else:
            token = activity_token
            active = self._active.get((tool.tool_id, token), {})
            if not active:
                raise RuntimeError(
                    f"设备 {tool.tool_id} 的加工活动 token {token} 已失效。"
                )

        members = batch_lots or [lot]
        active.update({
            "token": token,
            "completion": completion,
            "lot_id": lot.id,
            "step": lot.operation_index,
            "finish": finish,
            "phase_start": self.current_time,
            "batch_lots": members,
            "next_start": self._time(
                next_start if next_start is not None else finish
            ),
            **payload,
        })
        self._active[(tool.tool_id, token)] = active
        tool.active_lot_id = lot.id
        current_step = lot.current_step
        tool.active_step_id = current_step.id if current_step else None
        self._refresh_tool_capacity(tool)
        self.schedule(
            finish,
            completion,
            tool_id=tool.tool_id,
            lot_id=lot.id,
            token=token,
            **payload,
        )

    def _after_load(self, event: SimulationEvent) -> None:
        tool, lot, active = self._current_activity(event)
        if tool is None or lot is None or active is None:
            return
        step = lot.current_step
        if step is None:
            return
        token = int(active["token"])
        if step.required_setup and step.required_setup != tool.current_setup:
            self._start_phase(
                tool,
                lot,
                EventKind.SETUP_COMPLETE,
                self._setup_time(tool, step),
                setup=step.required_setup,
                activity_token=token,
            )
        else:
            self._start_process(
                tool,
                lot,
                activity_token=token,
            )

    def _after_setup(self, event: SimulationEvent) -> None:
        tool, lot, active = self._current_activity(event)
        if tool is None or lot is None or active is None:
            return
        tool.current_setup = str(event.payload["setup"])
        tool.current_setup_run_length = 0
        self._start_process(
            tool,
            lot,
            activity_token=int(active["token"]),
        )

    def _start_process(
        self,
        tool: ToolState,
        lot: LotState,
        *,
        activity_token: int | None = None,
        next_start: float | None = None,
    ) -> None:
        step = lot.current_step
        if step is None:
            raise ValueError("已完成 Lot 不能开始加工。")
        if step.processing_unit == "batch":
            members = self._select_batch(tool, lot)
            for member in members:
                self._remove_ready(member)
                member.in_process = True
                member.processing_tool_id = tool.tool_id
                member.start_time = (
                    member.start_time
                    if member.start_time is not None
                    else self.current_time
                )
            self._start_phase(
                tool,
                lot,
                EventKind.OPERATION_COMPLETE,
                sample_duration(
                    self.rng, step.processing_distribution, step.process_time
                ),
                process_start=self.current_time,
                step_index=lot.operation_index,
                worked=0.0,
                batch_lots=members,
                activity_token=activity_token,
                next_start=next_start,
            )
            return
        # wafer 步：普通设备逐片求和；级联设备只有第一片走完整
        # 加工时间，后续 wafer 按 cascading interval 流水进入。
        n = lot.wafer_count if step.processing_unit == "wafer" else 1
        group = self._tool_group(tool)
        if (
            n > 1
            and group is not None
            and group.cascading_tool
            and step.cascading_interval is not None
        ):
            duration = sample_duration(
                self.rng, step.processing_distribution, step.process_time
            ) + (n - 1) * step.cascading_interval
        else:
            duration = sample_duration_sum(
                self.rng,
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
            next_start = self.current_time + (n - 1) * step.cascading_interval
        self._start_phase(
            tool,
            lot,
            EventKind.OPERATION_COMPLETE,
            duration,
            process_start=self.current_time,
            step_index=lot.operation_index,
            worked=0.0,
            activity_token=activity_token,
            next_start=next_start,
        )

    def _select_batch(self, tool: ToolState, first: LotState) -> list[LotState]:
        """选择与 first 同炉的 lot 列表（含 first）。

        引擎自主组批：同工具组、同工艺、可加批的候选按到达顺序凑满
        batch_maximum 片；策略未提供分组时退化为此规则。
        """

        selected = (self._pending_batches or {}).pop(tool.tool_id, None)
        if selected is not None:
            members = selected
        elif self._batch_from_strategy:
            raise ValueError(
                f"策略模式必须为批处理设备 {tool.tool_id} 提供 batches。"
            )
        else:
            members = self._gather_batch(tool, first)
        self._validate_batch(tool, first, members)
        return members

    def _reserve_batch(self, tool: ToolState, first: LotState) -> list[LotState]:
        """在 loading 前冻结批次成员，避免后续派工抢走同批 lot。"""

        if self._pending_batches is None:
            self._pending_batches = {}
        selected = self._pending_batches.pop(tool.tool_id, None)
        if selected is None:
            if self._batch_from_strategy:
                raise ValueError(
                    f"策略模式必须为批处理设备 {tool.tool_id} 提供 batches。"
                )
            selected = self._gather_batch(tool, first)
        self._validate_batch(tool, first, selected)
        self._pending_batches[tool.tool_id] = selected
        for member in selected:
            self._remove_ready(member)
        return selected

    def _gather_batch(self, tool: ToolState, first: LotState) -> list[LotState]:
        """自动收集可同批的 lot（含 first），累计 wafer 不超过 batch_maximum。"""

        step = first.current_step
        if step is None:
            raise ValueError("已完成 Lot 不能组批。")
        members = [first]
        quantity = self._batch_member_quantity(tool, first)
        cap = step.batch_maximum
        for candidate in self._ready_lots_by_group.get(step.tool_group_id or "", ()):
            if (
                candidate is first
                or not self._same_batch_class(tool, first, candidate)
                or not self._dedication_allowed_fast(candidate, tool.tool_id)
            ):
                continue
            candidate_quantity = self._batch_member_quantity(tool, candidate)
            if cap is not None and quantity + candidate_quantity > cap:
                continue
            members.append(candidate)
            quantity += candidate_quantity
        return members

    def _same_batch_class(
        self, tool: ToolState, first: LotState, candidate: LotState
    ) -> bool:
        """判断两个 lot 是否满足工具组的组批条件。"""

        first_step = first.current_step
        candidate_step = candidate.current_step
        if first_step is None or candidate_step is None:
            return False
        if candidate_step.processing_unit != "batch":
            return False
        if candidate_step.tool_group_id != first_step.tool_group_id:
            return False
        group = self._tool_group(tool)
        criteria = self._batch_criteria(group)
        if (
            criteria["same_product"]
            and candidate.product_id != first.product_id
        ):
            return False
        if (
            criteria["same_part_family"]
            and self._part_family(candidate.product_id)
            != self._part_family(first.product_id)
        ):
            return False
        if criteria["same_step"]:
            return candidate_step.id == first_step.id
        if not any(criteria.values()):
            # 未声明 criterion 时也不允许把两道不同工艺混在一炉。
            return candidate_step == first_step
        return True

    def _batch_member_quantity(self, tool: ToolState, lot: LotState) -> int:
        """按 Toolgroups.BATCHING UNIT 把单个 lot 换算为组批数量。"""

        group = self._tool_group(tool)
        unit = (group.batching_unit or "wafer").strip().lower() if group else "wafer"
        if "wafer" in unit:
            return lot.wafer_count
        if "lot" in unit:
            return 1
        raise ValueError(f"不支持的组批单位：{group.batching_unit if group else unit}。")

    def _validate_batch(
        self, tool: ToolState, first: LotState, members: list[LotState]
    ) -> None:
        """验证策略/引擎组成的批次满足 SMT2020 物理约束。"""

        if not members or first not in members:
            raise ValueError(f"设备 {tool.tool_id} 的批次必须包含首个 Lot {first.id}。")
        if len({member.id for member in members}) != len(members):
            raise ValueError(f"设备 {tool.tool_id} 的批次中存在重复 Lot。")
        for member in members:
            if member is first:
                continue
            if (
                member.in_process
                or member.completed
                or member.ready_time > self.current_time
                or not self._same_batch_class(tool, first, member)
                or not self._dedication_allowed_fast(member, tool.tool_id)
            ):
                raise ValueError(
                    f"设备 {tool.tool_id} 的批次包含非法 Lot {member.id}。"
                )
        step = first.current_step
        if step is None:
            raise ValueError("已完成 Lot 不能组批。")
        quantity = sum(self._batch_member_quantity(tool, member) for member in members)
        if step.batch_minimum is not None and quantity < step.batch_minimum:
            raise ValueError(
                f"设备 {tool.tool_id} 的批次数量 {quantity} "
                f"低于最小批量 {step.batch_minimum}。"
            )
        if step.batch_maximum is not None and quantity > step.batch_maximum:
            raise ValueError(
                f"设备 {tool.tool_id} 的批次数量 {quantity} "
                f"超过最大批量 {step.batch_maximum}。"
            )

    def _batch_minimum_reached(self, tool: ToolState, lot: LotState) -> bool:
        step = lot.current_step
        if step is None or step.processing_unit != "batch":
            return True
        if step.batch_minimum is None:
            return True
        members = self._gather_batch(tool, lot)
        quantity = sum(self._batch_member_quantity(tool, member) for member in members)
        return quantity >= step.batch_minimum

    def _batch_class_key(self, tool: ToolState, lot: LotState) -> tuple[object, ...]:
        """返回一炉内必须相同的静态属性。"""

        step = lot.current_step
        if step is None:
            return (None,)
        group = self._tool_group(tool)
        criteria = self._batch_criteria(group)
        key: list[object] = [step.tool_group_id]
        if criteria["same_product"]:
            key.append(lot.product_id)
        if criteria["same_part_family"]:
            key.append(self._part_family(lot.product_id))
        if criteria["same_step"]:
            key.append(step.id)
        elif not any(criteria.values()):
            key.append(step)
        return tuple(key)

    @staticmethod
    def _batch_criteria(group: ToolGroupSpec | None) -> dict[str, bool]:
        criterion = (group.batch_criterion or "").lower() if group else ""
        normalized = re.sub(r"[^a-z0-9]+", "", criterion)
        return {
            "same_product": "sameproduct" in normalized,
            "same_part_family": (
                "samepartfam" in normalized
                or "samepartfamily" in normalized
            ),
            "same_step": (
                "samestepname" in normalized
                or "samestep" in normalized
            ),
        }

    def _part_family(self, product_id: str) -> str:
        product = self.model.products.get(product_id)
        if product is None:
            return product_id
        return product.part_family or product.id

    def _dedication_allowed_fast(self, lot: LotState, tool_id: str) -> bool:
        """O(1) LTL 判断：当前工步若是进行中绑定的目标则必须回绑定设备。"""
        rework_tool = lot.rework_tool_bindings.get(lot.operation_index)
        if rework_tool is not None:
            return rework_tool == tool_id
        step = lot.route[lot.operation_index]
        bound = lot.dedicated_tools.get(step.id) if lot.dedicated_tools else None
        return bound is None or bound == tool_id

    def _setup_allowed(
        self, step: RouteStepSpec, current_setup: str | None, run_length: int
    ) -> bool:
        """只依赖设备 setup 状态的 O(1) 资格判断。"""
        required = step.required_setup
        if required is None or required == current_setup:
            return True
        if not current_setup:
            return True
        return run_length >= self._setup_min_run.get((current_setup, required), 0)

    def _after_operation(self, event: SimulationEvent) -> None:
        tool, lot, active = self._current_activity(event)
        if tool is None or lot is None or active is None:
            return
        members = active.get("batch_lots", [lot])
        step = lot.current_step
        if step is None:
            return
        start = float(event.payload.get("process_start", self.current_time))
        worked = (
            float(event.payload.get("worked", 0.0))
            + self.current_time
            - float(active["phase_start"])
        )
        for member in members:
            if self._plugin_manager.has_operation_plugins:
                self._plugin_manager.on_operation_completed(
                    member,
                    tool,
                    start,
                    self.current_time,
                    worked,
                )
            operation_key = self._operation_keys_by_product[member.product_id][
                member.operation_index
            ]
            self._last_operation_completion[operation_key] = self.current_time
            member.last_tool_group_id = step.tool_group_id
            member.processed_tool_by_step[member.operation_index] = tool.tool_id
            member.rework_tool_bindings.pop(member.operation_index, None)
            if step.lot_to_lens_dedication_step:
                # 绑定工步：把 lot 绑定到当前设备，供 LTL 目标工步（step id）回本机。
                member.dedicated_tools[step.lot_to_lens_dedication_step] = tool.tool_id
            elif step.id in member.dedicated_tools:
                # LTL 目标工步完成，解除该绑定（不影响其他进行中的绑定）。
                del member.dedicated_tools[step.id]
            self._schedule_cqt_deadline(member, step)
            member.operation_index += 1
            self._changed_lot_ids.add(member.id)
            tool.processed_wafers += member.wafer_count
        tool.current_setup_run_length += 1
        self._schedule_counter_maintenance(tool)
        self._start_phase(
            tool,
            lot,
            EventKind.UNLOAD_COMPLETE,
            self._unloading_time(tool),
            batch_lots=members,
            finished_step=step,
            activity_token=int(active["token"]),
            next_start=float(active["next_start"]),
        )

    def _after_unload(self, event: SimulationEvent) -> None:
        tool, lot, active = self._current_activity(event)
        if tool is None or lot is None or active is None:
            return
        self._active.pop((tool.tool_id, int(active["token"])), None)
        members = active.get("batch_lots", [lot])
        finished_step = event.payload.get("finished_step")
        if not isinstance(finished_step, RouteStepSpec):
            raise RuntimeError("卸载事件缺少路线工步。")
        for member in members:
            member.in_process, member.processing_tool_id = False, None
            if member.completed:
                self._complete_lot(member)
                continue
            if (
                self.rng.random() < finished_step.rework_probability
                and finished_step.rework_step
            ):
                self.schedule(
                    self.current_time,
                    EventKind.REWORK_ROUTED,
                    lot_id=member.id,
                    step_id=finished_step.rework_step,
                    end_index=member.operation_index - 1,
                )
            else:
                self._continue_route(member)
        self._refresh_tool_capacity(tool)
        self._start_pending_pm_if_idle(tool)

    def _after_transport(self, event: SimulationEvent) -> None:
        lot = self._lot(str(event.payload["lot_id"]))
        lot.ready_time = self.current_time
        self._mark_ready(lot)

    def _begin_downtime(self, event: SimulationEvent, completion: EventKind) -> None:
        tool = self.tool_states[str(event.payload["tool_id"])]
        if tool.is_down:
            self._pending_downtimes.setdefault(tool.tool_id, deque()).append(
                (event, completion)
            )
            return
        paused: list[dict[str, object]] = []
        for key, active in tuple(self._active.items()):
            if key[0] != tool.tool_id:
                continue
            self._active.pop(key, None)
            active["remaining"] = max(
                float(active["finish"]) - self.current_time,
                0.0,
            )
            if active["completion"] is EventKind.OPERATION_COMPLETE:
                active["worked"] = (
                    float(active.get("worked", 0.0))
                    + self.current_time
                    - float(active["phase_start"])
                )
            paused.append(active)
        if paused:
            # 一次停机使该设备所有旧 token 失效。
            self._tokens[tool.tool_id] += 1
            self._paused[tool.tool_id] = paused
        tool.active_count = 0
        tool.in_process = False
        tool.active_lot_id = None
        tool.active_step_id = None
        self._mark_busy(tool)

        self._start_downtime(tool, event, completion)

    def _handle_pm_due(self, event: SimulationEvent) -> None:
        """PM 到期后只登记；设备空闲时才真正开始维护。"""

        tool = self.tool_states[str(event.payload["tool_id"])]
        if tool.is_down or self._tool_has_activities(tool.tool_id):
            self._pending_pms.setdefault(tool.tool_id, deque()).append(event)
            return
        self._begin_downtime(event, EventKind.PM_COMPLETE)

    def _start_pending_pm_if_idle(self, tool: ToolState) -> bool:
        if tool.is_down or self._tool_has_activities(tool.tool_id):
            return False
        pending = self._pending_pms.get(tool.tool_id)
        if not pending:
            return False
        event = pending.popleft()
        if not pending:
            del self._pending_pms[tool.tool_id]
        self._begin_downtime(event, EventKind.PM_COMPLETE)
        return True

    def _start_downtime(
        self,
        tool: ToolState,
        event: SimulationEvent,
        completion: EventKind,
    ) -> None:
        """启动一项停机；调用方负责保存正在进行的加工。"""

        tool.is_down = True
        tool.is_in_setup = False
        tool.down_reason = str(event.payload.get("reason", event.kind.value))
        duration = float(event.payload.get("duration", 0.0))
        tool.available_time = self._time(self.current_time + duration)
        self.schedule(
            tool.available_time,
            completion,
            tool_id=tool.tool_id,
            breakdown=event.payload.get("breakdown"),
            pm_event=event.payload.get("pm_event"),
        )

    def _end_downtime(self, event: SimulationEvent) -> None:
        tool = self.tool_states[str(event.payload["tool_id"])]
        if event.kind is EventKind.REPAIR_COMPLETE and event.payload.get("breakdown"):
            name = str(event.payload["breakdown"])
            breakdown = self._breakdowns_by_name[name]
            self.schedule(
                self.current_time
                + sample_duration(self.rng, breakdown.time_to_failure),
                EventKind.BREAKDOWN,
                tool_id=tool.tool_id,
                duration=sample_duration(self.rng, breakdown.time_to_repair),
                reason=f"breakdown:{name}",
                breakdown=name,
            )
        if event.kind is EventKind.PM_COMPLETE and event.payload.get("pm_event"):
            name = str(event.payload["pm_event"])
            pm = self._pms_by_name[name]
            if pm.pm_type == "time_based":
                self.schedule(
                    self.current_time + pm.mean_time_before_pm,
                    EventKind.PM_DUE,
                    tool_id=tool.tool_id,
                    duration=sample_duration(self.rng, pm.repair_distribution),
                    reason=f"pm:{name}",
                    pm_event=name,
                )

        # 当前停机结束后，优先执行维修期间积压的停机。设备只有在队列清空后
        # 才恢复加工，避免 PM/故障重叠时其中一个事件被静默丢弃。
        pending = self._pending_downtimes.get(tool.tool_id)
        if pending:
            next_event, next_completion = pending.popleft()
            if not pending:
                del self._pending_downtimes[tool.tool_id]
            self._start_downtime(tool, next_event, next_completion)
            return

        tool.is_down = False
        tool.down_reason = None
        tool.available_time = self.current_time
        if self._start_pending_pm_if_idle(tool):
            return
        paused_activities = self._paused.pop(tool.tool_id, [])
        if not paused_activities:
            self._refresh_tool_capacity(tool)
            return

        for paused in paused_activities:
            lot = self._lot(str(paused["lot_id"]))
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
                    "batch_lots",
                }
            }
            self._start_phase(
                tool,
                lot,
                completion,
                float(paused["remaining"]),
                batch_lots=paused.get("batch_lots"),
                **payload,
            )

    def _route_rework(self, event: SimulationEvent) -> None:
        lot = self._lot(str(event.payload["lot_id"]))
        step_id = str(event.payload["step_id"])
        target = self._route_step_index_by_product[lot.product_id].get(step_id)
        if target is None:
            raise ValueError(f"重工目标 {step_id} 不在 Lot {lot.id} 的路线中。")
        end_index = int(event.payload.get("end_index", lot.operation_index - 1))
        if target > end_index:
            raise ValueError(
                f"重工目标 {step_id} 不能位于刚完成的工步之后。"
            )
        bindings: dict[int, str] = {}
        for index in range(target, end_index + 1):
            tool_id = lot.processed_tool_by_step.get(index)
            # 抽检未命中的工步没有原设备；重走路线时它仍按抽检
            # 概率决定是否执行。只对上一轮真正加工过的工步绑定原设备。
            if tool_id is not None:
                bindings[index] = tool_id
        lot.rework_tool_bindings = bindings
        lot.operation_index = target
        self._schedule_transport_for_lot(lot.id)

    def _advance_over_unsampled_steps(self, lot: LotState) -> bool:
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
                probability > 0.0 and self.rng.random() < probability
            ):
                return True
            if step.id is not None:
                lot.dedicated_tools.pop(step.id, None)
            lot.operation_index += 1
            self._changed_lot_ids.add(lot.id)
        return False

    def _continue_route(self, lot: LotState) -> None:
        """卸载后选出下一道实际需要加工的工步。"""

        if self._advance_over_unsampled_steps(lot):
            self._schedule_transport_for_lot(lot.id)
        else:
            self._complete_lot(lot)

    def _complete_lot(self, lot: LotState) -> None:
        """统一结算 lot 完工，避免抽检跳过路径漏掉完工通知。"""

        if lot.end_time is not None:
            return
        lot.end_time = self.current_time
        self._changed_lot_ids.add(lot.id)
        self._plugin_manager.on_lot_completed(self.current_time, lot)

    def _record_cqt_violation(self, event: SimulationEvent) -> None:
        lot = self._lot(str(event.payload["lot_id"]))
        target_index = int(event.payload["target_index"])
        # CQT 只要求在目标工步开始前；已开始加工则不构成违反。
        if lot.operation_index < target_index or (
            lot.operation_index == target_index and not lot.in_process
        ):
            if self._plugin_manager.has_cqt_plugins:
                self._plugin_manager.on_cqt_violation(
                    self.current_time,
                    lot.id,
                    target_index,
                )

    def _schedule_transport_for_lot(self, lot_id: str) -> None:
        lot = self._lot(lot_id)
        self._remove_ready(lot)
        if lot.completed:
            lot.end_time = self.current_time
            return
        lot.ready_time = float("inf")
        self.schedule(
            self.current_time + self._transport_time(lot),
            EventKind.TRANSPORT_COMPLETE,
            lot_id=lot.id,
        )

    def _schedule_cqt_deadline(self, lot: LotState, step: RouteStepSpec) -> None:
        if step.critical_queue_time is None or step.critical_queue_time_step is None:
            return
        target = self._route_step_index_by_product[lot.product_id].get(
            step.critical_queue_time_step
        )
        if target is not None:
            self.schedule(
                self.current_time + step.critical_queue_time,
                EventKind.CQT_EXPIRE,
                lot_id=lot.id,
                target_index=target,
            )

    def _schedule_calendar_maintenance(self) -> None:
        for pm in self.model.preventive_maintenance:
            if pm.pm_type != "time_based":
                continue
            for tool in self._matching_tools(pm.valid_for_type, pm.type_name):
                first = sample_duration(
                    self.rng,
                    pm.first_one_at_distribution,
                    pm.first_one_at
                    if pm.first_one_at is not None
                    else pm.mean_time_before_pm,
                )
                self.schedule(
                    self.current_time + first,
                    EventKind.PM_DUE,
                    tool_id=tool.tool_id,
                    duration=sample_duration(self.rng, pm.repair_distribution),
                    reason=f"pm:{pm.event_name}",
                    pm_event=pm.event_name,
                )

    def _schedule_counter_maintenance(self, tool: ToolState) -> None:
        """为当前设备检查并安排已达到计数阈值的 PM。"""

        for pm in self._counter_pms_by_tool.get(tool.tool_id, ()):
            key = (tool.tool_id, pm.event_name)
            threshold = self._next_counter_pm[key]
            while tool.processed_wafers >= threshold:
                self.schedule(
                    self.current_time,
                    EventKind.PM_DUE,
                    tool_id=tool.tool_id,
                    duration=sample_duration(self.rng, pm.repair_distribution),
                    reason=f"pm:{pm.event_name}",
                    pm_event=pm.event_name,
                )
                threshold += pm.mean_time_before_pm
            self._next_counter_pm[key] = threshold

    def _schedule_breakdowns(self) -> None:
        for breakdown in self.model.breakdowns:
            for tool in self._matching_tools(
                breakdown.valid_for_type, breakdown.type_name
            ):
                if breakdown.first_one_at_distribution is not None:
                    first = sample_duration(
                        self.rng, breakdown.first_one_at_distribution
                    )
                elif breakdown.first_one_at is not None:
                    first = breakdown.first_one_at
                else:
                    first = sample_duration(self.rng, breakdown.time_to_failure)
                self.schedule(
                    self.current_time + first,
                    EventKind.BREAKDOWN,
                    tool_id=tool.tool_id,
                    duration=sample_duration(self.rng, breakdown.time_to_repair),
                    reason=f"breakdown:{breakdown.event_name}",
                    breakdown=breakdown.event_name,
                )

    def _schedule_wakeups(self, decision: StrategyDecision) -> None:
        for time, reason in decision.wakeups:
            if time <= self.current_time:
                raise ValueError("策略唤醒时间必须晚于当前时间。")
            if time < self.model.simulation.end_time:
                self.schedule(time, EventKind.STRATEGY_WAKEUP, reason=reason)

    def _current_activity(
        self, event: SimulationEvent
    ) -> tuple[ToolState | None, LotState | None, dict[str, object] | None]:
        tool_id = str(event.payload["tool_id"])
        token = int(event.payload.get("token", -1))
        tool = self.tool_states[tool_id]
        active = self._active.get((tool_id, token))
        if active is None:
            return None, None, None
        return tool, self._lot(str(event.payload["lot_id"])), active

    def _tool_has_activities(self, tool_id: str) -> bool:
        return any(key[0] == tool_id for key in self._active)

    def _refresh_tool_capacity(self, tool: ToolState) -> None:
        """按设备容量和槽位可用时间刷新空闲索引。"""

        activities = [
            active
            for (tool_id, _), active in self._active.items()
            if tool_id == tool.tool_id
        ]
        tool.active_count = len(activities)
        tool.in_process = bool(activities)
        tool.is_in_setup = any(
            active.get("completion") is EventKind.SETUP_COMPLETE
            for active in activities
        )
        if activities:
            tool.active_lot_id = str(activities[-1]["lot_id"])
        else:
            tool.active_lot_id = None
            tool.active_step_id = None

        group = self._tool_group(tool)
        cascading = bool(group and group.cascading_tool and tool.capacity > 1)
        if tool.is_down:
            self._mark_busy(tool)
            return
        if not activities:
            tool.available_time = self.current_time
            self._mark_idle(tool)
            return

        next_slot_time = min(
            float(active.get("next_start", active["finish"]))
            for active in activities
        )
        pre_process_phase = any(
            active.get("completion")
            in {EventKind.LOAD_COMPLETE, EventKind.SETUP_COMPLETE}
            for active in activities
        )
        if tool.is_in_setup or pre_process_phase:
            tool.available_time = next_slot_time
            self._mark_busy(tool)
            return
        if tool.active_count < tool.capacity:
            tool.available_time = max(self.current_time, next_slot_time)
        else:
            tool.available_time = next_slot_time

        if cascading and tool.active_count < tool.capacity:
            available_at = max(self.current_time, next_slot_time)
            if available_at > self.current_time:
                key = (tool.tool_id, available_at)
                if key not in self._scheduled_tool_available:
                    self.schedule(
                        available_at,
                        EventKind.TOOL_AVAILABLE,
                        tool_id=tool.tool_id,
                    )
                    self._scheduled_tool_available.add(key)

        if tool.active_count < tool.capacity and tool.available_time <= self.current_time:
            self._mark_idle(tool)
        else:
            self._mark_busy(tool)

    def _state(
        self,
        release_opportunity: bool,
        event_kinds: list[str] | None = None,
        reasons: list[str] | None = None,
        dispatchable_candidates: tuple[DispatchableCandidate, ...] | None = None,
    ) -> StrategyState:
        # 优化：候选表只保留有合法候选 lot 的设备，并预排序，避免策略每次决策
        # 都遍历全部可用设备（约 1400 台）再逐一过滤空候选。
        if dispatchable_candidates is None:
            dispatchable_candidates = self._candidate_index()
        changed_lots = tuple(
            self._lots_by_id[lot_id]
            for lot_id in self._changed_lot_ids
            if lot_id in self._lots_by_id
        )
        self._changed_lot_ids.clear()
        return StrategyState(
            model=self.model,
            current_time=self.current_time,
            lots=tuple(self.lots),
            waiting_lots=tuple(self.order_source.queue),
            tool_states=self.tool_states,
            waiting_lot_count=len(self.order_source.queue),
            release_opportunity=release_opportunity,
            event_kinds=tuple(event_kinds or ()),
            strategy_reasons=tuple(reasons or ()),
            last_operation_completion=dict(self._last_operation_completion),
            dispatchable_candidates=dispatchable_candidates,
            record_diagnostics=self._plugin_manager.has_decision_plugins,
            changed_lots=changed_lots,
        )

    def _candidate_index(
        self,
    ) -> tuple[DispatchableCandidate, ...]:
        """更新发生变化的工具组，并返回设备与候选 Lot 配对。"""
        if not self._candidate_groups_dirty:
            return self._dispatchable_candidates_cache

        for group_id in self._candidate_groups_dirty:
            self._rebuild_group_candidates(group_id)

        self._rebuild_dispatchable_candidates()
        self._candidate_groups_dirty.clear()
        return self._dispatchable_candidates_cache

    def _rebuild_group_candidates(self, group_id: str) -> None:
        """重新计算一个工具组内空闲设备与等待 Lot 的合法匹配。"""

        bucket = self._ready_lots_by_group.get(group_id, ())
        tool_ids = self._idle_tools_by_group.get(group_id)
        if not bucket or not tool_ids:
            self._eligible_lots_by_group.pop(group_id, None)
            return

        group_candidates: dict[str, tuple[LotState, ...]] = {}
        tools = [self.tool_states[tool_id] for tool_id in tool_ids]
        free: list[tuple[LotState, RouteStepSpec]] = []
        dedicated: dict[str, list[tuple[LotState, RouteStepSpec]]] = {}
        for lot in bucket:
            step = lot.route[lot.operation_index]
            bound = lot.rework_tool_bindings.get(lot.operation_index)
            if bound is None and lot.dedicated_tools:
                bound = lot.dedicated_tools.get(step.id)
            if bound is None:
                free.append((lot, step))
            elif self._tool_group_by_id.get(bound) == group_id:
                dedicated.setdefault(bound, []).append((lot, step))

        shared: dict[tuple[str | None, int], tuple[LotState, ...]] = {}
        for tool in tools:
            profile = (tool.current_setup, tool.current_setup_run_length)
            candidates = shared.get(profile)
            if candidates is None:
                candidates = tuple(
                    lot
                    for lot, step in free
                    if self._setup_allowed(step, *profile)
                    and self._batch_minimum_reached(tool, lot)
                )
                shared[profile] = candidates
            own = dedicated.get(tool.tool_id, ())
            if own:
                own_candidates = tuple(
                    lot
                    for lot, step in own
                    if self._setup_allowed(step, *profile)
                    and self._batch_minimum_reached(tool, lot)
                )
                candidates = self._merge_by_lot_order(candidates, own_candidates)
            if candidates:
                group_candidates[tool.tool_id] = candidates

        if group_candidates:
            group_candidates = self._limit_batch_candidate_tools(
                group_id, bucket, group_candidates
            )
        if group_candidates:
            self._eligible_lots_by_group[group_id] = group_candidates
        else:
            self._eligible_lots_by_group.pop(group_id, None)

    def _limit_batch_candidate_tools(
        self,
        group_id: str,
        bucket: tuple[LotState, ...] | list[LotState],
        candidates_by_tool: dict[str, tuple[LotState, ...]],
    ) -> dict[str, tuple[LotState, ...]]:
        """不向策略暴露超过当前 WIP 能支撑的开炉数量。

        引擎仍不选 lot，只把同一组批类别的合法 lot 放在同一个物理
        开炉槽中。每炉按现有默认行为尽量装到 batch_maximum。
        """

        group = self.model.tool_groups.get(group_id)
        if (
            group is None
            or not group.batching_tool
            or not bucket
            or any(lot.current_step.processing_unit != "batch" for lot in bucket)
        ):
            return candidates_by_tool

        representative_tool = self.tool_states[next(iter(candidates_by_tool))]
        lots_by_class: dict[tuple[object, ...], list[LotState]] = {}
        for lot in bucket:
            lots_by_class.setdefault(
                self._batch_class_key(representative_tool, lot), []
            ).append(lot)

        slots: list[tuple[object, ...]] = []
        classes = sorted(
            lots_by_class.items(),
            key=lambda item: min(self._lot_order[lot.id] for lot in item[1]),
        )
        for class_key, class_lots in classes:
            step = class_lots[0].current_step
            if step is None:
                continue
            quantity = sum(
                self._batch_member_quantity(representative_tool, lot)
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
                tool = self.tool_states[tool_id]
                matching = tuple(
                    lot
                    for lot in candidates_by_tool[tool_id]
                    if self._batch_class_key(tool, lot) == class_key
                )
                if matching:
                    result[tool_id] = matching
                    remaining_tools.remove(tool_id)
                    break
            if not remaining_tools:
                break
        return result

    def _rebuild_dispatchable_candidates(self) -> None:
        """按稳定顺序重建设备及其候选 Lot 的配对列表。"""

        dispatchable_candidates = sorted(
            (
                (self.tool_states[tool_id], candidates)
                for group_candidates in self._eligible_lots_by_group.values()
                for tool_id, candidates in group_candidates.items()
            ),
            key=lambda item: (item[0].available_time, item[0].tool_id),
        )
        self._dispatchable_candidates_cache = tuple(dispatchable_candidates)

    def _is_eligible(self, tool: ToolState, lot: LotState) -> bool:
        """校验单个策略配对，不为此重新扫描所有 lot。"""

        if not tool_is_available(tool, self.current_time):
            return False
        if lot.in_process or lot.completed or lot.ready_time > self.current_time:
            return False
        step = lot.current_step
        if step is None or step.tool_group_id != self._tool_group_by_id[tool.tool_id]:
            return False
        if self._ready_group_by_lot.get(lot.id) != step.tool_group_id:
            return False
        if not self._dedication_allowed_fast(lot, tool.tool_id):
            return False
        return (
            self._setup_allowed(step, tool.current_setup, tool.current_setup_run_length)
            and self._batch_minimum_reached(tool, lot)
        )

    def _mark_ready(self, lot: LotState) -> None:
        """把刚进入等待状态的 lot 放入其当前工具组的有序索引。"""

        if lot.in_process or lot.completed or lot.ready_time > self.current_time:
            return
        step = lot.route[lot.operation_index]
        group_id = step.tool_group_id
        if group_id is None:
            return
        self._remove_ready(lot)
        bucket = self._ready_lots_by_group.setdefault(group_id, [])
        order = self._lot_order[lot.id]
        index = bisect_right(bucket, order, key=lambda item: self._lot_order[item.id])
        bucket.insert(index, lot)
        self._ready_group_by_lot[lot.id] = group_id
        self._candidate_groups_dirty.add(group_id)
        self._dispatch_needed = True
        self._changed_lot_ids.add(lot.id)

    def _remove_ready(self, lot: LotState) -> None:
        self._changed_lot_ids.add(lot.id)
        group_id = self._ready_group_by_lot.pop(lot.id, None)
        if group_id is None:
            return
        bucket = self._ready_lots_by_group[group_id]
        for index, candidate in enumerate(bucket):
            if candidate is lot:
                del bucket[index]
                break
        if not bucket:
            del self._ready_lots_by_group[group_id]
        self._candidate_groups_dirty.add(group_id)

    def _mark_idle(self, tool: ToolState) -> None:
        group_id = self._tool_group_by_id[tool.tool_id]
        tools = self._idle_tools_by_group.setdefault(group_id, set())
        if tool.tool_id in tools:
            return
        tools.add(tool.tool_id)
        self._candidate_groups_dirty.add(group_id)
        self._dispatch_needed = True

    def _mark_busy(self, tool: ToolState) -> None:
        group_id = self._tool_group_by_id[tool.tool_id]
        tools = self._idle_tools_by_group.get(group_id)
        if tools is None or tool.tool_id not in tools:
            return
        tools.remove(tool.tool_id)
        if not tools:
            del self._idle_tools_by_group[group_id]
        self._candidate_groups_dirty.add(group_id)

    def _merge_by_lot_order(
        self, left: tuple[LotState, ...], right: tuple[LotState, ...]
    ) -> tuple[LotState, ...]:
        """合并两个按 fab 进入顺序排列的候选序列。"""

        return tuple(sorted((*left, *right), key=lambda lot: self._lot_order[lot.id]))

    def _runtime(self) -> FabStrategy:
        if self._strategy is None:
            raise RuntimeError("请先调用 begin_episode(strategy)。")
        return self._strategy

    def _report_progress(self) -> None:
        if self._progress_callback is None:
            return
        start = self.model.simulation.start_time
        duration = self.model.simulation.end_time - start
        elapsed_time = min(max(self.current_time - start, 0.0), duration)
        progress = elapsed_time / duration
        if progress >= 1.0 or progress - self._last_progress >= 0.01:
            self._progress_callback(elapsed_time, duration)
            self._last_progress = progress

    def _lot(self, lot_id: str) -> LotState:
        return self._lots_by_id[lot_id]

    def _tool_group(self, tool: ToolState) -> ToolGroupSpec | None:
        spec = self.model.tools[tool.tool_id]
        return self.model.tool_groups.get(spec.tool_group_id or "")

    def _loading_time(self, tool: ToolState) -> float:
        group = self._tool_group(tool)
        return group.loading_time if group else 0.0

    def _unloading_time(self, tool: ToolState) -> float:
        group = self._tool_group(tool)
        return group.unloading_time if group else 0.0

    def _setup_time(self, tool: ToolState, step: RouteStepSpec) -> float:
        transition = self._setup_transition_by_pair.get(
            (tool.current_setup or "", step.required_setup)
        )
        return sample_duration(
            self.rng,
            transition.setup_time if transition else step.setup_distribution,
        )

    def _transport_time(self, lot: LotState) -> float:
        if lot.operation_index >= len(lot.route):
            return 0.0
        following = lot.route[lot.operation_index]
        previous_group = self.model.tool_groups.get(lot.last_tool_group_id or "")
        following_group = self.model.tool_groups.get(following.tool_group_id or "")
        if previous_group is None or following_group is None:
            return 0.0
        return sample_duration(
            self.rng,
            self._transport_by_location.get(
                (previous_group.location, following_group.location)
            ),
        )

    def _matching_tools(self, valid_for_type: str, type_name: str) -> list[ToolState]:
        key = (valid_for_type, type_name)
        cached = self._matching_tools_cache.get(key)
        if cached is not None:
            return cached
        kind = valid_for_type.lower()
        if kind == "tool":
            result = [
                tool for tool in self.tool_states.values() if tool.tool_id == type_name
            ]
        elif kind == "toolgroup":
            result = [
                tool
                for tool in self.tool_states.values()
                if self.model.tools[tool.tool_id].tool_group_id == type_name
            ]
        elif kind == "area":
            result = [
                tool
                for tool in self.tool_states.values()
                if (group := self._tool_group(tool)) is not None
                and group.area == type_name
            ]
        else:
            raise ValueError(f"未知维护/故障适用范围：{valid_for_type}。")
        # 优化：结果只依赖静态模型，缓存复用。
        self._matching_tools_cache[key] = result
        return result
