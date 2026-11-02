"""SMT2020 事件驱动引擎。

引擎只维护物理状态、事件日历和动作合法性；任何 Lot/设备选择都由策略给出。
"""

from __future__ import annotations

import heapq
import random
import re
import weakref
from bisect import bisect_right
from collections import deque
from itertools import chain, count
from operator import itemgetter
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
from fab.snapshot_collections import SnapshotDeque, SnapshotList
from fab.setup_rules import SetupRules
from fab.strategy import (
    RELEASE_TRIGGERS,
    DispatchableCandidate,
    CompletionSnapshot,
    FabStrategy,
    ReleasePolicy,
    StrategyDecision,
    StrategyState,
)
from fab.engine.tool.batch import Batching
from fab.engine.tool.batch_rules import BatchRules
from fab.engine.tool.downtime import Downtime
from fab.engine.tool.process import Process

# 从排序条目 (available_time, tool_id, tool, candidates) 中取出 (tool, candidates)。
_TOOL_AND_CANDIDATES = itemgetter(2, 3)


class _ReleaseHandle:
    """交给投料员的"下单电话"：只登记，由引擎在结算点统一执行。"""

    def __init__(self, engine: FabEngine) -> None:
        self._engine = engine

    def release(self, lot_id: str) -> None:
        self._engine._release_orders.append(lot_id)

    def schedule_wakeup(self, time: float) -> None:
        engine = self._engine
        if time < engine.current_time:
            raise ValueError("投料闹钟不能早于当前时间。")
        if time < engine.model.simulation.end_time:
            engine.schedule(time, EventKind.RELEASE_WAKEUP)


class FabEngine:
    """执行客观事件；策略决定 dispatch，投料员决定 release。"""

    _TIME_DIGITS = 6

    def __init__(
        self,
        model: FabModel,
        order_seed: int,
        progress_callback: Callable[[float, float], None] | None = None,
        *,
        plugin_manager: PluginManager | None = None,
        rework_same_tool: bool = False,
    ) -> None:
        if model.time_unit != "minute":
            raise ValueError("FabEngine 只接受以 minute 为规范时间单位的 FabModel。")
        # SMT2020 只规定返工回到 STEP FOR REWORK；True 时额外要求返工段回原设备，
        # 仅用于与旧行为对比。
        self._rework_same_tool = rework_same_tool
        self._process = Process(self)
        
        # Setups 表查表（含 CURRENT SETUP 为空的通配行）。最小连续加工量属于
        # Toolgroups.DISPATCHING 的 setupavoidance 派工规则，由策略负责。
        self._setup_rules = SetupRules(model)
        # 返工退回 LTL 目标工步时需要恢复套刻绑定。
        self._ltl_target_steps_by_product: dict[str, frozenset[str]] = {
            product_id: frozenset(
                step.lot_to_lens_dedication_step
                for step in product.route
                if step.lot_to_lens_dedication_step
            )
            for product_id, product in model.products.items()
        }
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
        self.lots: list[LotState] = SnapshotList()
        self._lots_by_id: dict[str, LotState] = {}
        self._changed_lot_ids: set[str] = set()
        # 当前可派工的 lot，按工具组增量维护。列表始终按进入 fab 的顺序，
        # 因而保持旧版扫描 ``self.lots`` 时的候选顺序。
        self._ready_lots_by_group: dict[str, list[LotState]] = {}
        self._ready_group_by_lot: dict[str, str] = {}
        # 按工具组维护的就绪 lot 分类，随 _mark_ready/_remove_ready 增量更新，
        # 避免每次重建候选时重新遍历整个队列。
        self._free_ready_by_group: dict[str, list[tuple[LotState, RouteStepSpec]]] = {}
        self._dedicated_ready_by_group: dict[
            str, dict[str, list[tuple[LotState, RouteStepSpec]]]
        ] = {}
        # lot 放在哪一堆：None 表示自由堆，否则为绑定的设备 id。
        self._ready_pile_by_lot: dict[str, str | None] = {}
        self._lot_order: dict[str, int] = {}
        self._admitted_count = 0
        self.order_source: LotSource = create_lot_source(model, order_seed)
        self._calendar: list[SimulationEvent] = []
        self._sequence = count()
        self._tokens: dict[str, int] = {tool_id: 0 for tool_id in self.tool_states}
        self._active: dict[str, dict[int, dict[str, object]]] = {}
        self._paused: dict[str, list[dict[str, object]]] = {}
        self._scheduled_tool_available: set[tuple[str, float]] = set()
        # 同一设备可能在维修期间再次收到 PM/故障事件。不能丢弃这些事件，
        # 而是按到达顺序逐项停机，全部完成后再恢复被中断的加工。
        self._pending_downtimes: dict[str, deque[tuple[SimulationEvent, EventKind]]] = (
            {}
        )
        self._pending_pms: dict[str, deque[SimulationEvent]] = {}
        self._last_operation_completion: dict[tuple[str, int], float] = {}
        self._completion_snapshot_ref: weakref.ReferenceType[CompletionSnapshot] | None = None
        self._strategy: FabStrategy | None = None
        self._release_policy: ReleasePolicy | None = None
        self._release_handle = _ReleaseHandle(self)
        # 投料员订阅了哪些触发；热路径里只检查布尔值。
        self._notify_arrival = False
        self._notify_completion = False
        # 事件处理中只记录触发，结算点再通知投料员（构造 state 有副作用）。
        self._fired_triggers: set[str] = set()
        self._release_orders: list[str] = []
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
        self._tool_group_spec_by_tool_id = {
            tool_id: self.model.tool_groups.get(spec.tool_group_id or "")
            for tool_id, spec in self.model.tools.items()
        }
        tool_ids_by_group: dict[str, list[str]] = {}
        for tool_id, group_id in self._tool_group_by_id.items():
            tool_ids_by_group.setdefault(group_id, []).append(tool_id)
        self._tool_ids_by_group = {
            group_id: tuple(tool_ids)
            for group_id, tool_ids in tool_ids_by_group.items()
        }

        self._batch_rules = BatchRules(model)
        self._batching = Batching(self,self._batch_rules)
        # 候选表只会受某一工具组中的 ready lot 或 idle tool 变化影响。保留上一次
        # 结果，并在状态变化时仅重建对应工具组，避免每个运输/卸载事件都扫描全厂。
        self._eligible_lots_by_group: dict[str, dict[str, tuple[LotState, ...]]] = {}
        self._dispatchable_candidates_cache: tuple[DispatchableCandidate, ...] = ()
        self._candidate_groups_dirty: set[str] = set()
        # 每组的排序条目 (available_time, tool_id, tool, candidates)，只在该组变化时重建。
        self._entries_by_group: dict[str, list[tuple]] = {}
        # 候选内容没变、但空闲设备的 available_time 可能变了的组：只需重排，不必重新筛选。
        self._order_dirty_groups: set[str] = set()
        self._downtime = Downtime(self)
        # 优化：Downtime.matching_tools 的匹配结果只依赖静态模型（tools/tool_groups），
        # 与仿真状态无关，缓存后避免每次加工完成时重复扫描全部设备。
        self._matching_tools_cache: dict[tuple[str, str], list[ToolState]] = {}
        self._breakdowns_by_name: dict[str, BreakdownSpec] = {}
        self._completed_pending: list[LotState] = []
        for breakdown in self.model.breakdowns:
            self._breakdowns_by_name.setdefault(breakdown.event_name, breakdown)
        self._pms_by_name: dict[str, PreventiveMaintenanceSpec] = {}
        for pm in self.model.preventive_maintenance:
            self._pms_by_name.setdefault(pm.event_name, pm)
        # wafer-count PM 的适用范围是静态的。初始化时反向建立 tool -> PM 规则
        # 索引，使每次工序完成后只检查当前设备自己的规则。
        counter_pms_by_tool: dict[str, list[PreventiveMaintenanceSpec]] = {}
        # processed_wafers 保留为永不归零的累计量。每条 PM 规则独立保存
        # 下一个触发阈值，避免短周期 PM 把长周期 PM 的计数清掉。
        self._next_counter_pm: dict[tuple[str, str], float] = {}
        for pm in self.model.preventive_maintenance:
            if pm.pm_type != "wafer_count":
                continue
            tools = self._downtime.matching_tools(pm.valid_for_type, pm.type_name)
            for tool, first in zip(tools, self._downtime.first_pm_offsets(pm, tools)):
                counter_pms_by_tool.setdefault(tool.tool_id, []).append(pm)
                self._next_counter_pm[(tool.tool_id, pm.event_name)] = first
        self._counter_pms_by_tool: dict[str, tuple[PreventiveMaintenanceSpec, ...]] = {
            tool_id: tuple(rules) for tool_id, rules in counter_pms_by_tool.items()
        }
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
        policy = getattr(strategy, "release", None)
        if policy is None:
            raise TypeError(f"策略 {strategy.name} 没有提供投料员 release。")
        unknown = set(policy.triggers) - set(RELEASE_TRIGGERS)
        if unknown:
            raise ValueError(f"投料员订阅了未知触发：{sorted(unknown)}。")
        self._release_policy = policy
        self._notify_arrival = "order_arrival" in policy.triggers
        self._notify_completion = "lot_completed" in policy.triggers
        strategy.initialize(self.model)
        self._plugin_manager.on_simulation_started(self.model)
        # 优化：初始空闲设备集合 = 初始可用（未占用、未故障、已到可用时刻）的设备。
        for tool in self.tool_states.values():
            if tool_is_available(tool, self.current_time):
                self._mark_idle(tool)
        for event in self.order_source.initial_events(
            self.current_time,
            self.model.simulation.end_time,
        ):
            self.schedule(event.time, event.kind, **event.payload)
        policy.initialize(self.model, self._release_handle)
        self.schedule(self.model.simulation.end_time, EventKind.SIMULATION_END)
        self._downtime.schedule_calendar_maintenance()
        self._downtime.schedule_breakdowns()

    def run(self, strategy: FabStrategy) -> dict[str, object]:
        self.begin_episode(strategy)
        self.advance()
        return self.finish_episode()

    def advance(self) -> None:
        """推进事件直到仿真结束。"""

        while self._calendar:
            self.current_time = self._calendar[0].time
            self._report_progress()
            if self._plugin_manager.has_time_advanced_plugins:
                self._plugin_manager.on_time_advanced(self.current_time, self.lots)
            event_kinds: list[str] = []
            reasons: list[str] = []
            forced_decision = False
            simulation_end = False

            # 同一时刻内，新事件（例如 operation -> unload）也必须在
            # SIMULATION_END 之前依照 priority 结算，不能遗留到下一轮。
            while self._calendar and self._calendar[0].time == self.current_time:
                event = heapq.heappop(self._calendar)
                event_kinds.append(event.kind.value)
                forced_decision |= self._handle_event(event, reasons)
                simulation_end |= event.kind is EventKind.SIMULATION_END

            if simulation_end:
                self._done = True
                return
            if self._fired_triggers:
                # 投料结算的同一份状态也包含当前派工候选，因此同时消费此前
                # 累积的派工通知；是否真的派工仍完全由策略决定。
                self._dispatch_needed = False
                self._settle_release(self._state(event_kinds, reasons))
                continue

            if self._dispatch_needed or forced_decision:
                # ready/idle 状态变化只说明候选可能增加。先更新候选索引；若确实
                # 没有合法动作则无需调用策略。显式 wakeup/batch 事件必须调用。
                dispatchable_candidates = self._candidate_index()
                self._dispatch_needed = False
                if not forced_decision and not dispatchable_candidates:
                    continue
                decision = self._decide(
                    self._state(
                        event_kinds,
                        reasons,
                        dispatchable_candidates=dispatchable_candidates,
                    ),
                    "dispatch",
                )
                self._apply_decision(decision)
                # 派工决策同样可以登记唤醒（如周期刷新、等待阈值到期）。
                self._schedule_wakeups(decision)

        self._done = True

    def _settle_release(self, state: StrategyState) -> None:
        """投料结算点：派工与投料基于同一份 state，先派工、再执行投料单。"""

        decision = self._decide(state, "release")
        policy = self._release_policy
        for trigger in RELEASE_TRIGGERS:
            if trigger in self._fired_triggers:
                policy.on_trigger(trigger, state, self._release_handle)
        self._fired_triggers.clear()
        self._apply_decision(decision)
        self._schedule_wakeups(decision)

        orders, self._release_orders = self._release_orders, []
        for lot_id in orders:
            lot = self.order_source.release(self.current_time, lot_id)
            if lot is None:
                raise ValueError(f"投料单引用了不在订单池中的 lot：{lot_id}。")
            self._admit_lot(lot)
        if not orders:
            return
        # 新 Lot 入厂后由策略再次决定派工；引擎不替它选设备或 Lot。
        self._dispatch_needed = False
        follow_up = self._decide(self._state(), "follow_up")
        self._apply_decision(follow_up)
        self._schedule_wakeups(follow_up)

    def _admit_lot(self, lot: LotState) -> None:
        """把放行的 lot 加入 fab，并定位到第一道实际加工工步。"""

        self.lots.append(lot)
        self._lots_by_id[lot.id] = lot
        self._admitted_count += 1
        self._lot_order[lot.id] = self._admitted_count
        self._plugin_manager.on_lot_released(self.current_time, lot)
        if self._advance_over_unsampled_steps(lot):
            self._mark_ready(lot)
        else:
            self._complete_lot(lot)

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
            self._drop_completed_lots()
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

    def _handle_event(self, event: SimulationEvent, reasons: list[str]) -> bool:
        """结算一条客观事件，返回是否强制唤醒策略。"""

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
        )
        if source_result is not None:
            for next_event in source_result.next_events:
                self.schedule(next_event.time, next_event.kind, **next_event.payload)
            if self._notify_arrival:
                self._fired_triggers.add("order_arrival")
            return forced_decision
        # 装载/卸载是固定时长，不再单独产生事件：装载并入换型或加工阶段，
        # 卸载并入加工阶段，OPERATION_COMPLETE 在卸载结束时刻触发。
        if kind is EventKind.SETUP_COMPLETE:
            self._process.after_setup(event)
        elif kind is EventKind.OPERATION_COMPLETE:
            self._after_operation(event)
        elif kind is EventKind.TRANSPORT_COMPLETE:
            self._after_transport(event)
        elif kind is EventKind.TOOL_AVAILABLE:
            tool_id = str(event.payload["tool_id"])
            self._scheduled_tool_available.discard((tool_id, event.time))
            tool = self.tool_states[tool_id]
            self._refresh_tool_capacity(tool)
        elif kind is EventKind.BREAKDOWN:
            self._downtime.begin_downtime(event, EventKind.REPAIR_COMPLETE)
        elif kind is EventKind.REPAIR_COMPLETE:
            self._downtime.end_downtime(event)
        elif kind is EventKind.PM_DUE:
            self._downtime.handle_pm_due(event)
        elif kind is EventKind.PM_COMPLETE:
            self._downtime.end_downtime(event)
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
        elif kind is EventKind.RELEASE_WAKEUP:
            self._fired_triggers.add("release_wakeup")
        return forced_decision

    def _apply_decision(self, decision: StrategyDecision) -> None:
        """验证策略给出的配对，并启动该配对的物理事件链。"""

        assigned: set[str] = set()
        for tool_id, lot in decision.dispatches.items():
            tool = self.tool_states.get(tool_id)
            if tool is None or lot.id in assigned:
                raise ValueError(f"非法派工：{lot.id} -> {tool_id}。")
            if not self._is_eligible(tool, lot):
                raise ValueError(f"非法派工：{lot.id} -> {tool_id}。")
            step = lot.current_step
            if step is not None and step.processing_unit == "batch":
                self._batching.reserve(tool, lot)
            self._remove_ready(lot)
            lot.in_process, lot.processing_tool_id = True, tool_id
            lot.start_time = (
                lot.start_time if lot.start_time is not None else self.current_time
            )
            tool.current_product_id = lot.product_id
            loading = self._loading_time(tool)
            if step is not None and step.required_setup and step.required_setup != tool.current_setup:
                # 装载与换型连续占用设备，合并为一个换型阶段。
                self._process.start_phase(
                    tool,
                    lot,
                    EventKind.SETUP_COMPLETE,
                    loading + self._setup_time(tool, step),
                    setup=step.required_setup,
                )
            else:
                self._process.start_process(tool, lot, lead_time=loading)
            assigned.add(lot.id)

    
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
        """setup 状态不构成引擎硬约束。

        Setups 表的最小连续加工量由 Toolgroups.DISPATCHING 的
        setupavoidance 派工规则执行，属于策略职责（见 strategy/base_rule.py）。
        保留此函数以维持候选构建的调用点不变。
        """
        return True

    def _after_operation(self, event: SimulationEvent) -> None:
        """加工+卸载阶段结束：先按加工结束时刻记账，再执行卸载后的路由。"""
        tool, lot, active = self._current_activity(event)
        if tool is None or lot is None or active is None:
            return
        members = active.get("batch_lots", [lot])
        step = lot.current_step
        if step is None:
            return
        start = float(event.payload.get("process_start", self.current_time))
        # 事件在卸载结束时触发，加工结束时刻 = 当前时刻 - 固定卸载时长。
        # 若停机恰好发生在卸载那段时间内，该时刻会顺延停机时长（极少见）。
        process_end = self._time(self.current_time - self._unloading_time(tool))
        # 停机只暂停、不丢失剩余时长，所以实际加工时长就是采样的加工时长。
        worked = float(event.payload.get("process_time", process_end - start))
        for member in members:
            if self._plugin_manager.has_operation_plugins:
                self._plugin_manager.on_operation_completed(
                    member,
                    tool,
                    start,
                    process_end,
                    worked,
                )
            operation_key = self._operation_keys_by_product[member.product_id][
                member.operation_index
            ]
            # Outstanding states retain their old version without forcing every
            # state to copy a table that most strategies never read.
            if self._completion_snapshot_ref is not None:
                self._detach_completion_snapshot()
            self._last_operation_completion[operation_key] = process_end
            member.last_tool_group_id = step.tool_group_id
            member.processed_tool_by_step[member.operation_index] = tool.tool_id
            member.rework_tool_bindings.pop(member.operation_index, None)
            if step.lot_to_lens_dedication_step:
                # 绑定工步：把 lot 绑定到当前设备，供 LTL 目标工步（step id）回本机。
                member.dedicated_tools[step.lot_to_lens_dedication_step] = tool.tool_id
            elif step.id in member.dedicated_tools:
                # LTL 目标工步完成，解除该绑定（不影响其他进行中的绑定）。
                del member.dedicated_tools[step.id]
            self._schedule_cqt_deadline(member, step, at=process_end)
            member.operation_index += 1
            self._changed_lot_ids.add(member.id)
            tool.processed_wafers += member.wafer_count
        tool.current_setup_run_length += 1
        self._downtime.schedule_counter_maintenance(tool)

        # 卸载完成：释放槽位，lot 进入下一道工步。
        token = int(active["token"])
        tool_activities = self._active.get(tool.tool_id)
        if tool_activities is not None:
            tool_activities.pop(token, None)
            if not tool_activities:
                self._active.pop(tool.tool_id, None)
        for member in members:
            member.in_process, member.processing_tool_id = False, None
            if member.completed:
                self._complete_lot(member)
                continue
            if self.rng.random() < step.rework_probability and step.rework_step:
                self.schedule(
                    self.current_time,
                    EventKind.REWORK_ROUTED,
                    lot_id=member.id,
                    step_id=step.rework_step,
                    end_index=member.operation_index - 1,
                )
            else:
                self._continue_route(member)
        self._refresh_tool_capacity(tool)
        self._downtime.start_pending_pm_if_idle(tool)

    def _after_transport(self, event: SimulationEvent) -> None:
        lot = self._lot(str(event.payload["lot_id"]))
        lot.ready_time = self.current_time
        self._mark_ready(lot)
        
    def _route_rework(self, event: SimulationEvent) -> None:
        lot = self._lot(str(event.payload["lot_id"]))
        step_id = str(event.payload["step_id"])
        target = self._route_step_index_by_product[lot.product_id].get(step_id)
        if target is None:
            raise ValueError(f"重工目标 {step_id} 不在 Lot {lot.id} 的路线中。")
        end_index = int(event.payload.get("end_index", lot.operation_index - 1))
        if target > end_index:
            raise ValueError(f"重工目标 {step_id} 不能位于刚完成的工步之后。")
        bindings: dict[int, str | None] = {}
        ltl_targets = self._ltl_target_steps_by_product[lot.product_id]
        for index in range(target, end_index + 1):
            tool_id = lot.processed_tool_by_step.get(index)
            # 抽检未命中的工步没有原设备；重走路线时它仍按抽检
            # 概率决定是否执行。上一轮真正加工过的工步必须重做：
            # 默认可用组内任意设备（None），rework_same_tool 时回原设备。
            if tool_id is None:
                continue
            bindings[index] = tool_id if self._rework_same_tool else None
            step_id = lot.route[index].id
            if step_id in ltl_targets:
                # LTL 目标工步完成时已解除绑定；返工重做仍须回套刻绑定的设备。
                lot.dedicated_tools[step_id] = tool_id
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
        self._completed_pending.append(lot)
        if self._notify_completion:
            self._fired_triggers.add("lot_completed")

    def _drop_completed_lots(self) -> None:
        if not self._completed_pending:
            return
        for lot in self._completed_pending:
            del self._lots_by_id[lot.id]
        self.lots = SnapshotList(lot for lot in self.lots if lot.end_time is None)
        self._completed_pending.clear()

    def _record_cqt_violation(self, event: SimulationEvent) -> None:
        lot = self._lots_by_id.get(str(event.payload["lot_id"]))
        if lot is None:
            return
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
            self._complete_lot(lot)
            return
        lot.ready_time = float("inf")
        self.schedule(
            self.current_time + self._transport_time(lot),
            EventKind.TRANSPORT_COMPLETE,
            lot_id=lot.id,
        )

    def _schedule_cqt_deadline(
        self, lot: LotState, step: RouteStepSpec, *, at: float
    ) -> None:
        """CQT 从加工结束时刻 at 起算。"""
        if step.critical_queue_time is None or step.critical_queue_time_step is None:
            return
        target = self._route_step_index_by_product[lot.product_id].get(
            step.critical_queue_time_step
        )
        if target is not None:
            self.schedule(
                at + step.critical_queue_time,
                EventKind.CQT_EXPIRE,
                lot_id=lot.id,
                target_index=target,
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
        try:
            active = self._active[tool_id][token]
        except KeyError:
            # 停机或卸载后，旧完成事件可能已没有对应活动。
            return None, None, None
        if active is None:
            return None, None, None
        return tool, self._lot(str(event.payload["lot_id"])), active

    def _tool_has_activities(self, tool_id: str) -> bool:
        return self.tool_states[tool_id].active_count > 0

    def _refresh_tool_capacity(self, tool: ToolState) -> None:
        """按设备容量和槽位可用时间刷新空闲索引。"""

        activities = self._active.get(tool.tool_id)
        active_count = len(activities) if activities else 0
        tool.active_count = active_count
        tool.in_process = active_count > 0
        if not active_count:
            tool.is_in_setup = False
            tool.active_lot_id = None
            tool.active_step_id = None
            if tool.is_down:
                self._mark_busy(tool)
            else:
                tool.available_time = self.current_time
                self._mark_idle(tool)
            return

        if tool.is_down:
            tool.is_in_setup = any(
                active.get("completion") is EventKind.SETUP_COMPLETE
                for active in activities.values()
            )
            tool.active_lot_id = str(next(reversed(activities.values()))["lot_id"])
            self._mark_busy(tool)
            return

        # 单活动直接读字段；多活动只遍历一次，不复制列表或重复扫描。
        iterator = iter(activities.values())
        active = next(iterator)
        completion = active.get("completion")
        # 装载已并入换型/加工阶段，加工前阶段只剩换型。
        is_in_setup = completion is EventKind.SETUP_COMPLETE
        next_slot_time = float(active.get("next_start", active["finish"]))
        if active_count > 1:
            for active in iterator:
                if active.get("completion") is EventKind.SETUP_COMPLETE:
                    is_in_setup = True
                slot_time = float(active.get("next_start", active["finish"]))
                if slot_time < next_slot_time:
                    next_slot_time = slot_time
        tool.is_in_setup = is_in_setup
        tool.active_lot_id = str(active["lot_id"])

        # 换型或槽位已满时，不需要检查级联槽位开放事件。
        if is_in_setup or active_count >= tool.capacity:
            tool.available_time = next_slot_time
            self._mark_busy(tool)
            return

        available_at = max(self.current_time, next_slot_time)
        tool.available_time = available_at
        if available_at <= self.current_time:
            self._mark_idle(tool)
            return

        group = self._tool_group(tool)
        if group and group.cascading_tool and tool.capacity > 1:
            key = (tool.tool_id, available_at)
            if key not in self._scheduled_tool_available:
                self.schedule(
                    available_at,
                    EventKind.TOOL_AVAILABLE,
                    tool_id=tool.tool_id,
                )
                self._scheduled_tool_available.add(key)
        self._mark_busy(tool)

    def _detach_completion_snapshot(self) -> None:
        if self._completion_snapshot_ref() is not None:
            self._last_operation_completion = self._last_operation_completion.copy()
        self._completion_snapshot_ref = None

    def _state(
        self,
        event_kinds: list[str] | None = None,
        reasons: list[str] | None = None,
        dispatchable_candidates: tuple[DispatchableCandidate, ...] | None = None,
    ) -> StrategyState:
        # 优化：候选表只保留有合法候选 lot 的设备，并预排序，避免策略每次决策
        # 都遍历全部可用设备（约 1400 台）再逐一过滤空候选。
        if dispatchable_candidates is None:
            dispatchable_candidates = self._candidate_index()
        # set 的遍历顺序随 PYTHONHASHSEED 变化；按入厂顺序输出保证可复现。
        changed_lots_list = [self._lots_by_id[lot_id] for lot_id in self._changed_lot_ids
                             if lot_id in self._lots_by_id]
        if len(changed_lots_list) > 1:
            changed_lots_list.sort(key=lambda lot: self._lot_order[lot.id])
        changed_lots = tuple(changed_lots_list)
        self._changed_lot_ids.clear()
        self._drop_completed_lots()  # 删除已完工的 lot，避免策略每次决策都遍历全部 lot。
        completion_snapshot = (self._completion_snapshot_ref()
                               if self._completion_snapshot_ref is not None else None)
        if completion_snapshot is None:
            completion_snapshot = CompletionSnapshot(self._last_operation_completion)
            self._completion_snapshot_ref = weakref.ref(completion_snapshot)
        return StrategyState(
            model=self.model,
            current_time=self.current_time,
            lots=(
                self.lots.snapshot()
                if isinstance(self.lots, SnapshotList) else tuple(self.lots)
            ),
            waiting_lots=(
                self.order_source.queue.snapshot()
                if isinstance(self.order_source.queue, SnapshotDeque)
                else tuple(self.order_source.queue)
            ),
            tool_states=self.tool_states,
            waiting_lot_count=len(self.order_source.queue),
            event_kinds=tuple(event_kinds or ()),
            strategy_reasons=tuple(reasons or ()),
            last_operation_completion=completion_snapshot,
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
        # 候选变了的组和只需重排的组，都按当前 available_time 重建排序条目。
        # 只需重排的组要攒到这里才处理：没有组变脏时沿用旧缓存，与原行为一致。
        for group_id in self._candidate_groups_dirty | self._order_dirty_groups:
            group_candidates = self._eligible_lots_by_group.get(group_id)
            if group_candidates:
                self._entries_by_group[group_id] = [
                    (
                        self.tool_states[tool_id].available_time,
                        tool_id,
                        self.tool_states[tool_id],
                        candidates,
                    )
                    for tool_id, candidates in group_candidates.items()
                ]
            else:
                self._entries_by_group.pop(group_id, None)
        # 元组先比 available_time 再比 tool_id（唯一），与原排序规则相同。
        self._dispatchable_candidates_cache = tuple(
            map(
                _TOOL_AND_CANDIDATES,
                sorted(chain.from_iterable(self._entries_by_group.values())),
            )
        )
        self._candidate_groups_dirty.clear()
        self._order_dirty_groups.clear()
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
        free = self._free_ready_by_group.get(group_id, ())
        dedicated = self._dedicated_ready_by_group.get(group_id, {})

        group = self.model.tool_groups.get(group_id)
        is_batching_group = bool(group and group.batching_tool)
        # batch 组队列里有绑定到本组设备的 lot 时，凑批数量因设备而异，只能逐台计算。
        per_tool = is_batching_group and any(dedicated.values())
        if not per_tool:
            # _setup_allowed 不检查 setup，自由候选与设备无关，整组只算一次。
            common = tuple(
                lot
                for lot, step in free
                if step.processing_unit != "batch"
                or self._batching.minimum_reached(tools[0], lot)
            )
        for tool in tools:
            profile = (tool.current_setup, tool.current_setup_run_length)
            if per_tool:
                candidates = tuple(
                    lot
                    for lot, step in free
                    if self._setup_allowed(step, *profile)
                    and (
                        step.processing_unit != "batch"
                        or self._batching.minimum_reached(tool, lot)
                    )
                )
            else:
                candidates = common
            own = dedicated.get(tool.tool_id, ())
            if own:
                own_candidates = tuple(
                    lot
                    for lot, step in own
                    if self._setup_allowed(step, *profile)
                    and (
                        step.processing_unit != "batch"
                        or self._batching.minimum_reached(tool, lot)
                    )
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
            lots_by_class.setdefault(self._batch_rules.class_key(lot), []).append(lot)

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
                self._batch_rules.member_quantity(representative_tool, lot)
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
                    if self._batch_rules.class_key(lot) == class_key
                )
                if matching:
                    result[tool_id] = matching
                    remaining_tools.remove(tool_id)
                    break
            if not remaining_tools:
                break
        return result

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
        return self._setup_allowed(
            step, tool.current_setup, tool.current_setup_run_length
        ) and self._batching.minimum_reached(tool, lot)

    def _mark_ready(self, lot: LotState) -> None:
        """把刚进入等待状态的 lot 放入其当前工具组的有序索引。"""

        if lot.in_process or lot.completed or lot.ready_time > self.current_time:
            return
        step = lot.route[lot.operation_index]
        group_id = step.tool_group_id
        if group_id is None:
            return
        self._remove_ready(lot)
        if step.processing_unit == "batch":
            self._ready_batch_class_by_lot[lot.id] = self._batch_rules.class_key(lot)

        bucket = self._ready_lots_by_group.setdefault(group_id, [])
        order = self._lot_order[lot.id]
        index = bisect_right(bucket, order, key=lambda item: self._lot_order[item.id])
        bucket.insert(index, lot)
        self._ready_group_by_lot[lot.id] = group_id
        bound = lot.rework_tool_bindings.get(lot.operation_index)
        if bound is None and lot.dedicated_tools:
            bound = lot.dedicated_tools.get(step.id)
        if bound is None:
            pile = self._free_ready_by_group.setdefault(group_id, [])
        elif self._tool_group_by_id.get(bound) == group_id:
            pile = self._dedicated_ready_by_group.setdefault(group_id, {}).setdefault(
                bound, []
            )
        else:
            pile = None  # 绑定到其他组的设备：与原逻辑一致，两堆都不放
        if pile is not None:
            index = bisect_right(
                pile, order, key=lambda item: self._lot_order[item[0].id]
            )
            pile.insert(index, (lot, step))
            self._ready_pile_by_lot[lot.id] = bound
        self._candidate_groups_dirty.add(group_id)
        self._dispatch_needed = True
        self._changed_lot_ids.add(lot.id)

    def _remove_ready(self, lot: LotState) -> None:
        self._ready_batch_class_by_lot.pop(lot.id, None)
        self._changed_lot_ids.add(lot.id)
        group_id = self._ready_group_by_lot.pop(lot.id, None)
        if group_id is None:
            return
        bucket = self._ready_lots_by_group[group_id]
        for index, candidate in enumerate(bucket):
            if candidate is lot:
                del bucket[index]
                break
        if lot.id in self._ready_pile_by_lot:
            bound = self._ready_pile_by_lot.pop(lot.id)
            if bound is None:
                pile = self._free_ready_by_group[group_id]
            else:
                pile = self._dedicated_ready_by_group[group_id][bound]
            for index, (candidate, _) in enumerate(pile):
                if candidate is lot:
                    del pile[index]
                    break
        if not bucket:
            del self._ready_lots_by_group[group_id]
        self._candidate_groups_dirty.add(group_id)

    def _mark_idle(self, tool: ToolState) -> None:
        group_id = self._tool_group_by_id[tool.tool_id]
        tools = self._idle_tools_by_group.setdefault(group_id, set())
        if tool.tool_id in tools:
            # 已经空闲：候选不变，但 available_time 可能刚被刷新，排序要更新。
            self._order_dirty_groups.add(group_id)
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
        return self._tool_group_spec_by_tool_id[tool.tool_id]

    def _loading_time(self, tool: ToolState) -> float:
        group = self._tool_group(tool)
        return group.loading_time if group else 0.0

    def _unloading_time(self, tool: ToolState) -> float:
        group = self._tool_group(tool)
        return group.unloading_time if group else 0.0

    def _setup_time(self, tool: ToolState, step: RouteStepSpec) -> float:
        return sample_duration(
            self.rng, self._setup_rules.distribution(tool.current_setup, step)
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

