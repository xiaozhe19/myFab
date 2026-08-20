"""SMT2020 事件驱动引擎。

引擎只维护物理状态、事件日历和动作合法性；任何 Lot/设备选择都由策略给出。
"""

from __future__ import annotations

import heapq
import random
from bisect import bisect_right
from itertools import count
from typing import Callable

from fab.engine.eligibility import (
    tool_is_available,
)
from fab.engine.events import EVENT_PRIORITY, EventKind, SimulationEvent
from fab.metrics import MetricsCollector
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
from fab.order_source import LotSource, create_lot_source
from fab.strategy import FabStrategy, StrategyDecision, StrategyState


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
        self.model = model
        # 事件日历和 ToolState.available_time 必须使用同一精度。否则两者只差
        # 极小浮点误差时，空闲索引会认为设备可派工，而最终合法性校验会拒绝它。
        self.current_time = self._time(model.simulation.start_time)
        self.rng = random.Random(order_seed)
        self.tool_states = {
            spec.id: ToolState(
                spec.id, spec.name, spec.process, self._time(spec.available_from)
            )
            for spec in model.tools.values()
        }
        # (process, visit) 只由路线决定；预计算可避免每次完工都扫描路线前缀来
        # 计算该工艺是产品路线中的第几次访问。
        self._operation_keys_by_product = {
            product_id: self._operation_keys(product.route)
            for product_id, product in model.products.items()
        }
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
        # 当前可派工的 lot，按工具组增量维护。列表始终按进入 fab 的顺序，
        # 因而保持旧版扫描 ``self.lots`` 时的候选顺序。
        self._ready_lots_by_group: dict[str, list[LotState]] = {}
        self._ready_group_by_lot: dict[str, str] = {}
        self._lot_order: dict[str, int] = {}
        self.order_source: LotSource = create_lot_source(model, order_seed)
        self._calendar: list[SimulationEvent] = []
        self._sequence = count()
        self._tokens: dict[str, int] = {tool_id: 0 for tool_id in self.tool_states}
        self._active: dict[str, dict[str, object]] = {}
        self._paused: dict[str, dict[str, object]] = {}
        self._last_operation_completion: dict[tuple[str, int], float] = {}
        self._strategy: FabStrategy | None = None
        # 默认保留完整空闲设备快照，确保未声明优化开关的第三方策略完全兼容。
        self._include_available_tools = True
        self._metrics: MetricsCollector | None = None
        self._pending: StrategyDecision | None = None
        self._done = False
        self._result: dict[str, object] | None = None
        self.cqt_violations: list[dict[str, object]] = []
        self._decision_log: list[dict[str, object]] = []
        self._progress_callback = progress_callback
        self._last_progress = -1.0
        # 优化：增量维护"无占用/无故障/无换型"的设备 ID 集合，供 _state 快速构建
        # available_tools，避免每次决策都扫描全部 1443 台设备（原热点 tool_is_available
        # 被调用数百万次）。只在设备状态转变的关键点增删集合。
        self._idle_tools: set[str] = set()
        self._idle_tools_by_group: dict[str, set[str]] = {}
        self._available_tools_cache: tuple[ToolState, ...] | None = None
        self._tool_group_by_id = {
            tool_id: spec.tool_group_id or ""
            for tool_id, spec in self.model.tools.items()
        }
        # 优化：_matching_tools 的匹配结果只依赖静态模型（tools/tool_groups），
        # 与仿真状态无关，缓存后避免每次加工完成时重复扫描全部设备。
        self._matching_tools_cache: dict[tuple[str, str], list[ToolState]] = {}
        # 优化：wafer_count 型 PM 只在设备累计加工量达标时触发，且匹配的设备集合
        # 是静态的。预计算 (pm, 设备列表) 对，避免每次操作完成时遍历全部 PM 规则
        # 并反复调用 _matching_tools（原热点 _schedule_counter_maintenance）。
        self._counter_pm_matches: list[tuple[PreventiveMaintenanceSpec, set[str]]] = [
            (
                pm,
                # 优化：用设备 ID 集合做 O(1) 成员判断，避免每次操作后对设备列表线性查找。
                # 注意 ToolState 是可变 dataclass（不可哈希），因此按 tool_id 存集合。
                {
                    tool.tool_id
                    for tool in self._matching_tools(pm.valid_for_type, pm.type_name)
                },
            )
            for pm in self.model.preventive_maintenance
            if pm.pm_type == "wafer_count"
        ]

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
        self._strategy, self._metrics = strategy, MetricsCollector(self.model)
        # 内置策略只读取 dispatchable_tools；显式 opt-out 后无需在每轮决策复制
        # 全部空闲设备。未声明该属性的外部策略仍按旧行为获得完整快照。
        self._include_available_tools = bool(
            getattr(strategy, "requires_available_tools", True)
        )
        strategy.initialize(self.model)
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

        strategy, metrics = self._runtime()
        if self._pending is not None:
            return self._state(True)

        while self._calendar:
            self.current_time = self._calendar[0].time
            self._report_progress()
            metrics.advance_time(self.current_time, self.lots)
            event_kinds: list[str] = []
            reasons: list[str] = []
            release_opportunity = False
            dispatch_opportunity = False
            simulation_end = False

            # 同一时刻内，新事件（例如 operation -> unload）也必须在
            # SIMULATION_END 之前依照 priority 结算，不能遗留到下一轮。
            while self._calendar and self._calendar[0].time == self.current_time:
                event = heapq.heappop(self._calendar)
                event_kinds.append(event.kind.value)
                event_release, event_dispatch = self._handle_event(
                    event, metrics, reasons
                )
                release_opportunity |= event_release
                dispatch_opportunity |= event_dispatch
                simulation_end |= event.kind is EventKind.SIMULATION_END

            if simulation_end:
                self._done = True
                return None
            if release_opportunity:
                state = self._state(True, event_kinds, reasons)
                decision = strategy.decide(state)
                self._log_decision(decision, self.current_time, "release")
                self._pending = decision
                return state

            if dispatch_opportunity:
                # 只有设备或 Lot 真的可能重新进入候选集时才请求策略派工。
                decision = strategy.decide(self._state(False, event_kinds, reasons))
                self._log_decision(decision, self.current_time, "dispatch")
                self._apply_decision(decision, metrics)

        self._done = True
        return None

    def apply_release_action(self, release_lot: bool | None) -> None:
        """执行策略的 release 决定，或由 RL 覆盖该布尔动作。"""

        strategy, metrics = self._runtime()
        if self._pending is None:
            raise RuntimeError("当前没有投料决策点。")
        decision, self._pending = self._pending, None
        if release_lot is not None:
            if decision.release_lot_id is not None:
                raise ValueError(
                    "定向投料必须由策略返回 lot ID，不能用布尔 RL 动作覆盖。"
                )
            decision.release_lot = bool(release_lot)
        self._apply_decision(decision, metrics)
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
        self._mark_ready(lot)
        metrics.lot_released(lot)
        # 新 Lot 入厂后由策略再次决定派工；引擎不替它选设备或 Lot。
        follow_up = strategy.decide(self._state(False))
        self._log_decision(follow_up, self.current_time, "follow_up")
        if follow_up.release_lot or follow_up.release_lot_id is not None:
            raise ValueError("策略只能在 LOT_RELEASE 事件中请求投料。")
        self._apply_decision(follow_up, metrics)
        self._schedule_wakeups(follow_up)

    def finish_episode(self) -> dict[str, object]:
        strategy, metrics = self._runtime()
        if not self._done:
            raise RuntimeError("仿真尚未结束。")
        if self._result is None:
            self._result = metrics.result(strategy.name, self.lots, self.tool_states)
            self._result["cqt_violations"] = self.cqt_violations
            self._result["decision_log"] = self._decision_log
        return self._result

    def _handle_event(
        self, event: SimulationEvent, metrics: MetricsCollector, reasons: list[str]
    ) -> tuple[bool, bool]:
        """结算一条客观事件，返回（投料机会，派工机会）。"""

        kind = event.kind
        dispatch_opportunity = kind in {
            EventKind.UNLOAD_COMPLETE,
            EventKind.TRANSPORT_COMPLETE,
            EventKind.REPAIR_COMPLETE,
            EventKind.PM_COMPLETE,
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
                dispatch_opportunity or source_result.release_opportunity,
            )
        if kind is EventKind.LOAD_COMPLETE:
            self._after_load(event)
        elif kind is EventKind.SETUP_COMPLETE:
            self._after_setup(event)
        elif kind is EventKind.OPERATION_COMPLETE:
            self._after_operation(event, metrics)
        elif kind is EventKind.UNLOAD_COMPLETE:
            self._after_unload(event, metrics)
        elif kind is EventKind.TRANSPORT_COMPLETE:
            self._after_transport(event)
        elif kind is EventKind.BREAKDOWN:
            self._begin_downtime(event, EventKind.REPAIR_COMPLETE)
        elif kind is EventKind.REPAIR_COMPLETE:
            self._end_downtime(event)
        elif kind is EventKind.PM_DUE:
            self._begin_downtime(event, EventKind.PM_COMPLETE)
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
        return False, dispatch_opportunity

    def _log_decision(
        self, decision: StrategyDecision, current_time: float, kind: str
    ) -> None:
        """记录一次策略决策及其内部参数，供调试与回放。"""

        self._decision_log.append(
            {
                "time": current_time,
                "kind": kind,
                "diagnostics": dict(decision.diagnostics),
            }
        )

    def _apply_decision(
        self, decision: StrategyDecision, metrics: MetricsCollector
    ) -> None:
        """验证策略给出的配对，并启动该配对的物理事件链。"""

        self._pending_batches = decision.batches or None
        assigned: set[str] = set()
        for tool_id, lot in decision.dispatches.items():
            tool = self.tool_states.get(tool_id)
            if tool is None or lot.id in assigned:
                raise ValueError(f"非法派工：{lot.id} -> {tool_id}。")
            if not self._is_eligible(tool, lot):
                raise ValueError(f"非法派工：{lot.id} -> {tool_id}。")
            self._remove_ready(lot)
            lot.in_process, lot.processing_tool_id = True, tool_id
            lot.start_time = (
                lot.start_time if lot.start_time is not None else self.current_time
            )
            tool.in_process, tool.current_product_id = True, lot.product_id
            tool.active_lot_id = lot.id
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
        **payload: object,
    ) -> None:
        """占用设备并安排当前物理阶段的完成事件。batch 工步可传入整炉 lot 列表。"""

        # 优化：设备被占用（加工/换型），从空闲集合移除。
        self._mark_busy(tool)
        self._tokens[tool.tool_id] += 1
        token = self._tokens[tool.tool_id]
        finish = self._time(self.current_time + max(duration, 0.0))
        tool.available_time = finish
        tool.is_in_setup = completion is EventKind.SETUP_COMPLETE
        members = batch_lots or [lot]
        self._active[tool.tool_id] = {
            "token": token,
            "completion": completion,
            "lot_id": lot.id,
            "step": lot.operation_index,
            "finish": finish,
            "phase_start": self.current_time,
            "batch_lots": members,
            **payload,
        }
        self.schedule(
            finish,
            completion,
            tool_id=tool.tool_id,
            lot_id=lot.id,
            token=token,
            **payload,
        )

    def _after_load(self, event: SimulationEvent) -> None:
        tool, lot = self._current_activity(event)
        if tool is None or lot is None:
            return
        step = lot.current_step
        if step is None:
            return
        if step.required_setup and step.required_setup != tool.current_setup:
            self._start_phase(
                tool,
                lot,
                EventKind.SETUP_COMPLETE,
                self._setup_time(tool, step),
                setup=step.required_setup,
            )
        else:
            self._start_process(tool, lot)

    def _after_setup(self, event: SimulationEvent) -> None:
        tool, lot = self._current_activity(event)
        if tool is None or lot is None:
            return
        tool.current_setup = str(event.payload["setup"])
        tool.current_setup_run_length = 0
        self._start_process(tool, lot)

    def _start_process(self, tool: ToolState, lot: LotState) -> None:
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
                self._duration(step.processing_distribution, step.process_time),
                process_start=self.current_time,
                step_index=lot.operation_index,
                worked=0.0,
                batch_lots=members,
            )
            return
        # wafer 步：逐片加工，每片独立抽样求和；lot 步：整 lot 一次加工。
        n = lot.wafer_count if step.processing_unit == "wafer" else 1
        duration = sum(
            self._duration(step.processing_distribution, step.process_time)
            for _ in range(n)
        )
        self._start_phase(
            tool,
            lot,
            EventKind.OPERATION_COMPLETE,
            duration,
            process_start=self.current_time,
            step_index=lot.operation_index,
            worked=0.0,
        )

    def _select_batch(self, tool: ToolState, first: LotState) -> list[LotState]:
        """选择与 first 同炉的 lot 列表（含 first）。

        引擎自主组批：同工具组、同工艺、可加批的候选按到达顺序凑满
        batch_maximum 片；策略未提供分组时退化为此规则。
        """

        if not self._batch_from_strategy:
            return self._gather_batch(tool, first)
        groups = self._pending_batches or {}
        for members in groups.values():
            if first in members:
                return members
        return self._gather_batch(tool, first)

    def _gather_batch(self, tool: ToolState, first: LotState) -> list[LotState]:
        """自动收集可同批的 lot（含 first），累计 wafer 不超过 batch_maximum。"""

        step = first.current_step
        members = [first]
        wafers = first.wafer_count
        cap = step.batch_maximum
        for candidate in self._ready_lots_by_group.get(step.tool_group_id or "", ()):
            if (
                candidate is first
                or candidate.route[candidate.operation_index] is not step
                or not self._dedication_allowed_fast(candidate, tool.tool_id)
            ):
                continue
            if cap is not None and wafers + candidate.wafer_count > cap:
                continue
            members.append(candidate)
            wafers += candidate.wafer_count
        return members

    def _dedication_allowed_fast(self, lot: LotState, tool_id: str) -> bool:
        """O(1) LTL 判断：当前工步若是进行中绑定的目标则必须回绑定设备。"""
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

    def _after_operation(
        self, event: SimulationEvent, metrics: MetricsCollector
    ) -> None:
        tool, lot = self._current_activity(event)
        if tool is None or lot is None:
            return
        active = self._active[tool.tool_id]
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
            metrics.record_operation(
                member, tool, start, self.current_time, active_duration=worked
            )
            member.operation_history.append(
                {
                    "step": member.operation_index,
                    "process": step.process,
                    "tool_id": tool.tool_id,
                    "start": start,
                    "end": self.current_time,
                    "active_duration": worked,
                }
            )
            operation_key = self._operation_keys_by_product[member.product_id][
                member.operation_index
            ]
            self._last_operation_completion[operation_key] = self.current_time
            if step.lot_to_lens_dedication_step:
                # 绑定工步：把 lot 绑定到当前设备，供 LTL 目标工步（step id）回本机。
                member.dedicated_tools[step.lot_to_lens_dedication_step] = tool.tool_id
            elif step.id in member.dedicated_tools:
                # LTL 目标工步完成，解除该绑定（不影响其他进行中的绑定）。
                del member.dedicated_tools[step.id]
            self._schedule_cqt_deadline(member, step)
            member.operation_index += 1
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
        )

    def _after_unload(self, event: SimulationEvent, metrics: MetricsCollector) -> None:
        tool, lot = self._current_activity(event)
        if tool is None or lot is None:
            return
        active = self._active.pop(tool.tool_id, None)
        members = active.get("batch_lots", [lot]) if active else [lot]
        finished_step = event.payload.get("finished_step")
        if not isinstance(finished_step, RouteStepSpec):
            raise RuntimeError("卸载事件缺少路线工步。")
        tool.in_process = False
        tool.is_in_setup = False
        tool.active_lot_id = None
        for member in members:
            member.in_process, member.processing_tool_id = False, None
            if member.completed:
                member.end_time = self.current_time
                metrics.lot_completed(member)
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
                )
            elif self.rng.random() <= finished_step.sampling_probability:
                self.schedule(
                    self.current_time, EventKind.SAMPLING_DECISION, lot_id=member.id
                )
            else:
                self._schedule_transport_for_lot(member.id)
        # 优化：设备卸载完成变为空闲；若同时没有故障/换型，则重新进入空闲集合。
        if not tool.is_down:
            self._mark_idle(tool)

    def _after_transport(self, event: SimulationEvent) -> None:
        lot = self._lot(str(event.payload["lot_id"]))
        lot.ready_time = self.current_time
        self._mark_ready(lot)

    def _begin_downtime(self, event: SimulationEvent, completion: EventKind) -> None:
        tool = self.tool_states[str(event.payload["tool_id"])]
        if tool.is_down:
            return
        # 优化：设备进入故障/维护，从空闲集合移除。
        self._mark_busy(tool)
        active = self._active.pop(tool.tool_id, None)
        if active is not None:
            self._tokens[tool.tool_id] += 1  # 使原完成事件失效
            active["remaining"] = max(float(active["finish"]) - self.current_time, 0.0)
            if active["completion"] is EventKind.OPERATION_COMPLETE:
                active["worked"] = (
                    float(active.get("worked", 0.0))
                    + self.current_time
                    - float(active["phase_start"])
                )
            self._paused[tool.tool_id] = active
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
        tool.is_down = False
        tool.down_reason = None
        tool.available_time = self.current_time
        paused = self._paused.pop(tool.tool_id, None)
        if paused is None:
            tool.in_process = False
            # 优化：设备恢复且无暂停活动，重新进入空闲集合。
            self._mark_idle(tool)
        else:
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
        if event.kind is EventKind.REPAIR_COMPLETE and event.payload.get("breakdown"):
            name = str(event.payload["breakdown"])
            breakdown = next(
                item for item in self.model.breakdowns if item.event_name == name
            )
            self.schedule(
                self.current_time + self._duration(breakdown.time_to_failure),
                EventKind.BREAKDOWN,
                tool_id=tool.tool_id,
                duration=self._duration(breakdown.time_to_repair),
                reason=f"breakdown:{name}",
                breakdown=name,
            )
        if event.kind is EventKind.PM_COMPLETE and event.payload.get("pm_event"):
            name = str(event.payload["pm_event"])
            pm = next(
                item
                for item in self.model.preventive_maintenance
                if item.event_name == name
            )
            if pm.pm_type == "time_based":
                self.schedule(
                    self.current_time + pm.mean_time_before_pm,
                    EventKind.PM_DUE,
                    tool_id=tool.tool_id,
                    duration=self._duration(pm.repair_distribution),
                    reason=f"pm:{name}",
                    pm_event=name,
                )

    def _route_rework(self, event: SimulationEvent) -> None:
        lot = self._lot(str(event.payload["lot_id"]))
        step_id = str(event.payload["step_id"])
        target = next(
            (index for index, step in enumerate(lot.route) if step.id == step_id), None
        )
        if target is None:
            raise ValueError(f"重工目标 {step_id} 不在 Lot {lot.id} 的路线中。")
        lot.operation_index = target
        self._schedule_transport_for_lot(lot.id)

    def _record_cqt_violation(self, event: SimulationEvent) -> None:
        lot = self._lot(str(event.payload["lot_id"]))
        target_index = int(event.payload["target_index"])
        # CQT 只要求在目标工步开始前；已开始加工则不构成违反。
        if lot.operation_index < target_index or (
            lot.operation_index == target_index and not lot.in_process
        ):
            self.cqt_violations.append(
                {
                    "lot_id": lot.id,
                    "target_step": target_index,
                    "time": self.current_time,
                }
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
        target = next(
            (
                index
                for index, candidate in enumerate(lot.route)
                if candidate.id == step.critical_queue_time_step
            ),
            None,
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
                first = (
                    pm.first_one_at
                    if pm.first_one_at is not None
                    else pm.mean_time_before_pm
                )
                self.schedule(
                    self.current_time + first,
                    EventKind.PM_DUE,
                    tool_id=tool.tool_id,
                    duration=self._duration(pm.repair_distribution),
                    reason=f"pm:{pm.event_name}",
                    pm_event=pm.event_name,
                )

    def _schedule_counter_maintenance(self, tool: ToolState) -> None:
        # 优化：遍历预计算的 (wafer_count PM, 匹配设备 ID 集合)，只处理与当前设备
        # 相关的规则，避免每次操作完成时扫描全部 PM 规则。
        for pm, matched_tool_ids in self._counter_pm_matches:
            if tool.tool_id not in matched_tool_ids:
                continue
            if tool.processed_wafers >= pm.mean_time_before_pm:
                tool.processed_wafers %= int(pm.mean_time_before_pm)
                self.schedule(
                    self.current_time,
                    EventKind.PM_DUE,
                    tool_id=tool.tool_id,
                    duration=self._duration(pm.repair_distribution),
                    reason=f"pm:{pm.event_name}",
                    pm_event=pm.event_name,
                )

    def _schedule_breakdowns(self) -> None:
        for breakdown in self.model.breakdowns:
            for tool in self._matching_tools(
                breakdown.valid_for_type, breakdown.type_name
            ):
                first = (
                    breakdown.first_one_at
                    if breakdown.first_one_at is not None
                    else self._duration(breakdown.time_to_failure)
                )
                self.schedule(
                    self.current_time + first,
                    EventKind.BREAKDOWN,
                    tool_id=tool.tool_id,
                    duration=self._duration(breakdown.time_to_repair),
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
    ) -> tuple[ToolState | None, LotState | None]:
        tool = self.tool_states[str(event.payload["tool_id"])]
        active = self._active.get(tool.tool_id)
        if active is None or int(event.payload.get("token", -1)) != active["token"]:
            return None, None
        return tool, self._lot(str(event.payload["lot_id"]))

    def _state(
        self,
        release_opportunity: bool,
        event_kinds: list[str] | None = None,
        reasons: list[str] | None = None,
    ) -> StrategyState:
        # 内置策略只使用 dispatchable_tools；只有显式声明需要完整空闲快照的
        # 策略才构造 available_tools，避免每个决策复制约 1400 台设备引用。
        available_tools: tuple[ToolState, ...] = ()
        if self._include_available_tools:
            available_tools = self._available_tools_cache
            if available_tools is None:
                available_tools = tuple(
                    self.tool_states[tool_id] for tool_id in self._idle_tools
                )
                self._available_tools_cache = available_tools
        # 优化：候选表只保留有合法候选 lot 的设备，并预排序，避免策略每次决策
        # 都遍历全部可用设备（约 1400 台）再逐一过滤空候选。
        eligible_by_tool, dispatchable_tools = self._eligible_lots_by_tool()
        return StrategyState(
            self.model,
            self.current_time,
            tuple(self.lots),
            tuple(self.order_source.queue),
            self.tool_states,
            len(self.order_source.queue),
            release_opportunity,
            tuple(event_kinds or ()),
            tuple(reasons or ()),
            eligible_by_tool,
            dict(self._last_operation_completion),
            available_tools=available_tools,
            dispatchable_tools=dispatchable_tools,
        )

    def _eligible_lots_by_tool(
        self,
    ) -> tuple[dict[str, tuple[LotState, ...]], tuple[ToolState, ...]]:
        """从 ready 索引构造候选表；同 setup 状态的设备共享候选 tuple。"""

        result: dict[str, tuple[LotState, ...]] = {}
        dispatchable: list[ToolState] = []
        for group_id, bucket in self._ready_lots_by_group.items():
            tool_ids = self._idle_tools_by_group.get(group_id)
            if not tool_ids:
                continue
            tools = [self.tool_states[tool_id] for tool_id in tool_ids]
            free: list[tuple[LotState, RouteStepSpec]] = []
            dedicated: dict[str, list[tuple[LotState, RouteStepSpec]]] = {}
            for lot in bucket:
                step = lot.route[lot.operation_index]
                bound = lot.dedicated_tools.get(step.id) if lot.dedicated_tools else None
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
                    )
                    shared[profile] = candidates
                own = dedicated.get(tool.tool_id, ())
                if own:
                    own_candidates = tuple(
                        lot
                        for lot, step in own
                        if self._setup_allowed(step, *profile)
                    )
                    candidates = self._merge_by_lot_order(candidates, own_candidates)
                if candidates:
                    result[tool.tool_id] = candidates
                    dispatchable.append(tool)
        dispatchable.sort(key=lambda tool: (tool.available_time, tool.tool_id))
        return result, tuple(dispatchable)

    def _is_eligible(self, tool: ToolState, lot: LotState) -> bool:
        """校验单个策略配对，不为此重新扫描所有 lot。"""

        if not tool_is_available(tool, self.current_time):
            return False
        if lot.in_process or lot.completed or lot.ready_time > self.current_time:
            return False
        step = lot.current_step
        if step is None or step.tool_group_id != self._tool_group_by_id[tool.tool_id]:
            return False
        if not self._dedication_allowed_fast(lot, tool.tool_id):
            return False
        return self._setup_allowed(step, tool.current_setup, tool.current_setup_run_length)

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

    def _remove_ready(self, lot: LotState) -> None:
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

    def _mark_idle(self, tool: ToolState) -> None:
        if tool.tool_id not in self._idle_tools:
            self._available_tools_cache = None
        self._idle_tools.add(tool.tool_id)
        self._idle_tools_by_group.setdefault(
            self._tool_group_by_id[tool.tool_id], set()
        ).add(tool.tool_id)

    def _mark_busy(self, tool: ToolState) -> None:
        if tool.tool_id in self._idle_tools:
            self._available_tools_cache = None
        self._idle_tools.discard(tool.tool_id)
        group_id = self._tool_group_by_id[tool.tool_id]
        tools = self._idle_tools_by_group.get(group_id)
        if tools is None:
            return
        tools.discard(tool.tool_id)
        if not tools:
            del self._idle_tools_by_group[group_id]

    def _merge_by_lot_order(
        self, left: tuple[LotState, ...], right: tuple[LotState, ...]
    ) -> tuple[LotState, ...]:
        """合并两个按 fab 进入顺序排列的候选序列。"""

        return tuple(sorted((*left, *right), key=lambda lot: self._lot_order[lot.id]))

    def _runtime(self) -> tuple[FabStrategy, MetricsCollector]:
        if self._strategy is None or self._metrics is None:
            raise RuntimeError("请先调用 begin_episode(strategy)。")
        return self._strategy, self._metrics

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
        transition = next(
            (
                item
                for item in self.model.setup_transitions
                if item.current_setup == (tool.current_setup or "")
                and item.new_setup == step.required_setup
            ),
            None,
        )
        return self._duration(
            transition.setup_time if transition else step.setup_distribution
        )

    def _transport_time(self, lot: LotState) -> float:
        if lot.operation_index == 0 or lot.operation_index >= len(lot.route):
            return 0.0
        previous, following = (
            lot.route[lot.operation_index - 1],
            lot.route[lot.operation_index],
        )
        previous_group = self.model.tool_groups.get(previous.tool_group_id or "")
        following_group = self.model.tool_groups.get(following.tool_group_id or "")
        if previous_group is None or following_group is None:
            return 0.0
        return self._duration(
            self._transport_by_location.get(
                (previous_group.location, following_group.location)
            )
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

    def _duration(
        self, distribution: DistributionSpec | None, fallback: float = 0.0
    ) -> float:
        if distribution is None:
            return fallback
        kind = distribution.kind.lower()
        if kind in {"constant", "deterministic"}:
            # SMT 的 SETUP 分布可能是 deterministic（固定值），等价于 constant。
            return distribution.mean
        if kind == "uniform":
            # SMT 的 OFFSET 列可能是“±绝对偏差”或“±%偏差”两种语义，导入后
            # offset 可能大于 mean（例如 Uniform(mean=10, offset=20)）。若直接
            # uniform(mean-offset, mean+offset)，下界会为负 → 引擎会安排出
            # “过去时刻”的事件而崩溃。这里把下界钳到 0，保证抽样时长恒为非负。
            low = max(distribution.mean - distribution.offset, 0.0)
            return self.rng.uniform(low, distribution.mean + distribution.offset)
        if kind == "exponential":
            return self.rng.expovariate(1 / distribution.mean)
        raise ValueError(f"不支持的分布：{distribution.kind}。")
