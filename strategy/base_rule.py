"""规则策略的公共底座：SMT2020 分层派工；投料交给可替换的投料员。

规则来源：SMT2020 General Data specification 2.1 节与 AutoSched AP 文档 3.10-3.12 节。

子类只覆盖 ``rank_key(lot)``：派工排序的最后一层，见下方第 3 条。
投料方式通过 ``__init__`` 的 ``release`` 参数传入投料员（见 strategy/release/），
与排序键任意组合。其余公共机制（优先级、setup、预留、setup avoidance、组批）不在子类中改动，
开关通过 ``__init__`` 参数传入，保证不同排序键之间的比较只差排序键一项。

- 投料：默认推式（PushRelease）。lot 到达 release pool 即放行。
- 派工排序（rule_FIRST + FWLRANK）：
  1. 优先级高者优先（Super Hot 30 > Hot 20 > Regular 10）；
  2. 同优先级时 setup 时间最少；
  3. 仍相同时按子类的 ``rank_key``（SMT2020 默认为 FIFO）。
- Tool wake-up：多台设备空闲时，选 setup 时间最少的设备。
- Super Hot Lot（DISPATCHING = SuperHotLotFIRST_and_Reservation）：为下一道
  工步预留一台设备，该设备只等这个 lot 到达；setup 在 lot 到达后才做。
- Setup avoidance（DISPATCHING = Setupavoidancerule，Implant 组）：设备切换
  setup 后至少连续加工 Setups 表规定的 lot 数。规范未说明队列中已没有当前
  setup 的 lot 时怎么办，这里采用折中：设备因该规则无 lot 可做时开始计时，
  空等时间达到这次切换的 setup 时长后允许切换（等待已比换一次线更贵）。
  到期时刻登记策略唤醒，保证无其他事件时也会重新决策。该组不使用预留。
"""

from __future__ import annotations

import heapq
import re

from fab.engine.eligibility import tool_is_available
from fab.model.entities import FabModel, LotState, RouteStepSpec, ToolState
from fab.setup_rules import SetupRules
from fab.strategy import ReleasePolicy, StrategyDecision, StrategyState
from strategy.release import PushRelease

_RESERVATION_RULE = "superhotlotfirstandreservation"
_SETUP_AVOIDANCE_RULE = "setupavoidancerule"


def _normalize(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


class BaseRule:
    """公共底座。不能直接使用，子类必须实现 ``rank_key``。"""

    name = "BASE_RULE"
    # 写进诊断信息的排序键名称，子类覆盖。
    rank_rule = "?"

    def __init__(
        self,
        setup_aware_ranking: bool = False,
        release: ReleasePolicy | None = None,
    ) -> None:
        # 投料员；引擎直接与它交互，派工逻辑不读取它。
        self.release = release if release is not None else PushRelease()
        # 路线定义的（与顺序无关的）setup 在排序中如何计时：
        # False（默认，对齐 AutoSched）：按工步名义 setup 时长，不看设备当前 setup；
        # True：设备已是同一 setup 记 0，主动凑同 setup（setup 更少、排队更短）。
        # 换型矩阵定义的 setup 两种模式下都按当前 setup 计时。
        # 见 docs/audits/report_setup_ranking_20261026.md。
        self.setup_aware_ranking = setup_aware_ranking
        if setup_aware_ranking:
            self.name = f"{self.name}_SETUP_AWARE"

    # ------------------------------------------------------------ 子类接口

    def rank_key(self, lot: LotState) -> tuple[object, ...]:
        """排序的最后一层：优先级与 setup 都相同时，值小者先派。

        必须以 ``lot.input_order, lot.id`` 结尾，保证排序完全确定。
        """

        raise NotImplementedError

    def pair_key(
        self, lot: LotState, setup: float, tool_rank: int
    ) -> tuple[object, ...]:
        """有 setup 的组里 (设备, lot) 配对的完整排序键，值小者先派。

        默认是字典序 (优先级, setup, rank_key, 设备顺序)；混合策略改成把 setup
        并入打分。
        """

        return (-lot.priority, setup, *self.rank_key(lot), tool_rank)

    # ------------------------------------------------------ 子类可用的工具

    @staticmethod
    def step_work(step: RouteStepSpec, wafer_count: int) -> float:
        """一道工步的名义加工时长。按片加工乘片数；组批一炉一个时长，不分摊。

        不考虑级联流水、抽检概率与随机分布，只用于排序。
        """

        if step.processing_unit == "wafer":
            return step.process_time * wafer_count
        return step.process_time

    def remaining_work(self, lot: LotState) -> float:
        """从当前工步（含）到完工的名义加工时长之和。"""

        key = (lot.product_id, lot.wafer_count)
        table = self._remaining_work_tables.get(key)
        if table is None:
            suffix = [0.0] * (len(lot.route) + 1)
            for index in range(len(lot.route) - 1, -1, -1):
                suffix[index] = suffix[index + 1] + self.step_work(
                    lot.route[index], lot.wafer_count
                )
            table = self._remaining_work_tables[key] = tuple(suffix)
        return table[lot.operation_index]

    def initialize(self, model: FabModel) -> None:
        self.model = model
        self._setups = SetupRules(model)
        self._group_by_tool = {
            tool_id: spec.tool_group_id or "" for tool_id, spec in model.tools.items()
        }
        tools_by_group: dict[str, list[str]] = {}
        for tool_id, group_id in self._group_by_tool.items():
            tools_by_group.setdefault(group_id, []).append(tool_id)
        self._tools_by_group = {
            group_id: tuple(sorted(tool_ids))
            for group_id, tool_ids in tools_by_group.items()
        }
        self._rule_by_group = {
            group_id: _normalize(group.dispatch_rule.name if group.dispatch_rule else None)
            for group_id, group in model.tool_groups.items()
        }
        # 与 BatchRules 相同的组批条件解析，用于预判自动组批的成员。
        self._batch_criteria_by_group: dict[str, tuple[bool, bool, bool]] = {}
        for group_id, group in model.tool_groups.items():
            criterion = _normalize(group.batch_criterion)
            self._batch_criteria_by_group[group_id] = (
                "sameproduct" in criterion,
                "samepartfam" in criterion or "samepartfamily" in criterion,
                "samestepname" in criterion or "samestep" in criterion,
            )
        # 路线中存在需要 setup 的工步的工具组；其余组 setup 恒为 0，可走快速路径。
        self._setup_groups = {
            step.tool_group_id or ""
            for product in model.products.values()
            for step in product.route
            if step.required_setup
        }
        # 路线工步是模型中的不可变对象，按 id 缓存期望 setup 时长。
        self._expected_setup_cache: dict[tuple[str | None, int], float] = {}
        self._min_run_cache: dict[tuple[str, str], int] = {}
        # setup avoidance 折中：设备因最小连续量无 lot 可做的起始时刻，以及
        # 已登记的唤醒时刻（避免重复登记）。设备获得派工后清除。
        self._blocked_since: dict[str, float] = {}
        self._blocked_wakeup: dict[str, float] = {}
        self._now = 0.0
        self._wakeups: list[tuple[float, str]] = []
        # 未完成的 super hot lot 及其预留：lot_id -> (目标工步下标, 设备 ID)。
        self._super_hot: dict[str, LotState] = {}
        self._reservations: dict[str, tuple[int, str]] = {}
        # remaining_work 的后缀和表：(product_id, wafer_count) -> 各工步起的剩余时长。
        self._remaining_work_tables: dict[tuple[str, int], tuple[float, ...]] = {}
        # 上次没派出任何 lot 的组：group_id -> (输入签名, 有效截止时刻, 候选 lot ID, entries)。
        # 输入不变时结果必然相同，直接跳过；保留 entries 引用，保证签名里的 id() 不被复用。
        self._idle_groups: dict[str, tuple[tuple[object, ...], float, frozenset[str], object]] = {}
        # 本组评估中最早的 setup avoidance 到期时刻，由 _start_blocked_timer 更新。
        self._group_horizon = float("inf")

    def decide(self, state: StrategyState) -> StrategyDecision:
        explanations: dict[str, object] | None = (
            {} if state.record_diagnostics else None
        )
        self._now = state.current_time
        self._wakeups = []
        for lot in state.changed_lots:
            if lot.is_super_hot and not lot.completed:
                self._super_hot[lot.id] = lot
        reserved_tools = self._update_reservations(state)

        entries_by_group: dict[str, list[tuple[ToolState, tuple[LotState, ...]]]] = {}
        for tool, candidates in state.dispatchable_candidates:
            entries_by_group.setdefault(self._group_by_tool[tool.tool_id], []).append(
                (tool, candidates)
            )

        dispatches: dict[str, LotState] = {}
        used_lot_ids: set[str] = set()
        for group_id, entries in entries_by_group.items():
            self._dispatch_group(
                group_id,
                entries,
                reserved_tools,
                used_lot_ids,
                dispatches,
                explanations,
            )

        return StrategyDecision(
            dispatches=dispatches,
            wakeups=self._wakeups,
            diagnostics=(
                {
                    "strategy": {
                        "name": self.name,
                        "rule": f"优先级 -> 最少 setup -> {self.rank_rule}；wake-up 最少 setup",
                    },
                    "release": {"rule": self.release.name},
                    "reservations": {
                        tool_id: lot_id for tool_id, lot_id in reserved_tools.items()
                    },
                    "dispatches": explanations,
                }
                if state.record_diagnostics
                else {}
            ),
        )

    # ------------------------------------------------------------------ 派工

    def _dispatch_group(
        self,
        group_id: str,
        entries: list[tuple[ToolState, tuple[LotState, ...]]],
        reserved_tools: dict[str, str],
        used_lot_ids: set[str],
        dispatches: dict[str, LotState],
        explanations: dict[str, object] | None,
    ) -> None:
        """在一个工具组内按 (优先级, setup, rank_key, 设备顺序) 配对。

        有 setup 的组：对所有 (设备, lot) 配对按固定键排序后贪心扫描，等价于
        反复取剩余配对中的最小者；setup 项同时实现 tool wake-up 的"最少 setup
        设备"。无 setup 的组：setup 恒为 0，按设备顺序各取最优 lot 即可，
        预留设备先选。
        """

        idle = self._idle_groups.get(group_id)
        if (
            idle is not None
            and self._now < idle[1]
            and idle[0] == self._group_signature(entries, reserved_tools)
            and used_lot_ids.isdisjoint(idle[2])
        ):
            return
        dispatch_count = len(dispatches)
        self._group_horizon = float("inf")
        self._dispatch_group_pairs(
            group_id, entries, reserved_tools, used_lot_ids, dispatches, explanations
        )
        if len(dispatches) > dispatch_count:
            self._idle_groups.pop(group_id, None)
            return
        # 没派出 lot：每台设备的候选都被占用、预留或 setup avoidance 挡住。
        # 这些条件只在设备/候选/预留变化，或 setup avoidance 到期时才会改变。
        lot_ids = frozenset(lot.id for _, candidates in entries for lot in candidates)
        if used_lot_ids.isdisjoint(lot_ids):
            self._idle_groups[group_id] = (
                self._group_signature(entries, reserved_tools),
                self._group_horizon,
                lot_ids,
                entries,
            )
        else:
            self._idle_groups.pop(group_id, None)

    def _group_signature(
        self,
        entries: list[tuple[ToolState, tuple[LotState, ...]]],
        reserved_tools: dict[str, str],
    ) -> tuple[object, ...]:
        """组的派工输入：设备与候选元组（按对象身份）、setup 状态、预留、阻塞计时。"""

        return tuple(
            (
                id(tool),
                id(candidates),
                tool.current_setup,
                tool.current_setup_run_length,
                reserved_tools.get(tool.tool_id),
                self._blocked_since.get(tool.tool_id),
            )
            for tool, candidates in entries
        )

    def _dispatch_group_pairs(
        self,
        group_id: str,
        entries: list[tuple[ToolState, tuple[LotState, ...]]],
        reserved_tools: dict[str, str],
        used_lot_ids: set[str],
        dispatches: dict[str, LotState],
        explanations: dict[str, object] | None,
    ) -> None:
        group = self.model.tool_groups.get(group_id)
        is_batching = bool(group and group.batching_tool)
        if group_id in self._setup_groups:
            pairs = self._setup_pairs(group_id, entries, reserved_tools, used_lot_ids)
        else:
            pairs = self._tool_order_pairs(entries, reserved_tools)

        assigned_tools: set[str] = set()
        candidates_by_tool = {tool.tool_id: candidates for tool, candidates in entries}
        for setup, tool, lot in pairs:
            if tool.tool_id in assigned_tools:
                continue
            if lot is None:
                # 无 setup 组：此刻才为该设备选 lot，以便跳过前面设备已占用的 lot。
                lot = self._best_lot(
                    candidates_by_tool[tool.tool_id],
                    reserved_tools.get(tool.tool_id),
                    used_lot_ids,
                )
                if lot is None:
                    continue
            elif lot.id in used_lot_ids:
                continue
            dispatches[tool.tool_id] = lot
            assigned_tools.add(tool.tool_id)
            used_lot_ids.add(lot.id)
            self._blocked_since.pop(tool.tool_id, None)
            self._blocked_wakeup.pop(tool.tool_id, None)
            step = lot.route[lot.operation_index]
            if is_batching and step.processing_unit == "batch":
                # 引擎会自动把同批 lot 冻结到这台设备，它们不能再派给其他设备。
                used_lot_ids.update(
                    member.id
                    for member in self._predict_batch(
                        group_id, lot, candidates_by_tool[tool.tool_id], used_lot_ids
                    )
                )
            if explanations is not None:
                explanations[tool.tool_id] = {
                    "rule": f"优先级 -> 最少 setup -> {self.rank_rule}",
                    "selected_lot_id": lot.id,
                    "priority": lot.priority,
                    "expected_setup": setup,
                    "reserved_super_hot": reserved_tools.get(tool.tool_id) == lot.id,
                }

    def _lot_key(self, lot: LotState) -> tuple[object, ...]:
        return (-lot.priority, *self.rank_key(lot))

    def _best_lot(
        self,
        candidates: tuple[LotState, ...],
        reserved_for: str | None,
        used_lot_ids: set[str],
    ) -> LotState | None:
        best: LotState | None = None
        best_key: tuple[object, ...] | None = None
        for lot in candidates:
            if lot.id in used_lot_ids or (reserved_for is not None and lot.id != reserved_for):
                continue
            key = self._lot_key(lot)
            if best_key is None or key < best_key:
                best, best_key = lot, key
        return best

    @staticmethod
    def _tool_order_pairs(
        entries: list[tuple[ToolState, tuple[LotState, ...]]],
        reserved_tools: dict[str, str],
    ) -> list[tuple[float, ToolState, LotState | None]]:
        """无 setup 组的处理顺序：预留设备优先，其余保持引擎给出的设备顺序。"""

        ordered = sorted(
            range(len(entries)),
            key=lambda i: (entries[i][0].tool_id not in reserved_tools, i),
        )
        return [(0.0, entries[i][0], None) for i in ordered]

    def _setup_pairs(
        self,
        group_id: str,
        entries: list[tuple[ToolState, tuple[LotState, ...]]],
        reserved_tools: dict[str, str],
        used_lot_ids: set[str],
    ) -> list[tuple[float, ToolState, LotState | None]]:
        setup_avoidance = self._rule_by_group.get(group_id) == _SETUP_AVOIDANCE_RULE
        # 每台设备最多被其他 len(entries)-1 台设备抢走候选，只需保留各自最优的
        # len(entries) 个。组批组需要完整列表来预判同批成员，不做截断。
        group = self.model.tool_groups.get(group_id)
        per_tool_limit = None if group and group.batching_tool else len(entries)
        keyed: list[tuple[tuple[object, ...], float, ToolState, LotState]] = []
        for rank, (tool, candidates) in enumerate(entries):
            reserved_for = reserved_tools.get(tool.tool_id)
            current = tool.current_setup
            # 同一设备对同一工步的放行结论相同，逐设备缓存 (是否放行, 等待阈值)。
            allowed_by_step: dict[int, tuple[bool, float]] = {}
            blocked_threshold: float | None = None
            tool_pairs = []
            for lot in candidates:
                if lot.id in used_lot_ids or (
                    reserved_for is not None and lot.id != reserved_for
                ):
                    continue
                step = lot.route[lot.operation_index]
                if setup_avoidance:
                    verdict = allowed_by_step.get(id(step))
                    if verdict is None:
                        verdict = self._setup_avoidance_allows(tool, step)
                        allowed_by_step[id(step)] = verdict
                    allowed, threshold = verdict
                    if not allowed:
                        if blocked_threshold is None or threshold < blocked_threshold:
                            blocked_threshold = threshold
                        continue
                setup = self._expected_setup(current, step)
                # 预留设备优先接收它等待的 super hot lot。
                tool_rank = -1 if reserved_for == lot.id else rank
                key = self.pair_key(lot, setup, tool_rank)
                tool_pairs.append((key, setup, tool, lot))
            if not tool_pairs and blocked_threshold is not None:
                self._start_blocked_timer(tool.tool_id, blocked_threshold)
            if per_tool_limit is not None and len(tool_pairs) > per_tool_limit:
                tool_pairs = heapq.nsmallest(
                    per_tool_limit, tool_pairs, key=lambda item: item[0]
                )
            keyed.extend(tool_pairs)
        keyed.sort(key=lambda item: item[0])
        return [(setup, tool, lot) for _, setup, tool, lot in keyed]

    def _expected_setup(self, current: str | None, step: RouteStepSpec) -> float:
        if (
            not self.setup_aware_ranking
            and step.required_setup
            and step.setup_distribution is not None
        ):
            return float(step.setup_distribution.mean)
        if not step.required_setup or step.required_setup == current:
            return 0.0
        key = (current, id(step))
        cached = self._expected_setup_cache.get(key)
        if cached is None:
            cached = self._setups.expected_time(current, step)
            self._expected_setup_cache[key] = cached
        return cached

    def _setup_avoidance_allows(
        self, tool: ToolState, step: RouteStepSpec
    ) -> tuple[bool, float]:
        """返回 (是否允许切换到 step 的 setup, 未达最小连续量时需空等的时长)。"""

        current, new = tool.current_setup, step.required_setup
        if not new or not current or new == current:
            return True, 0.0
        key = (current, new)
        min_run = self._min_run_cache.get(key)
        if min_run is None:
            min_run = self._setups.min_run(current, new)
            self._min_run_cache[key] = min_run
        if tool.current_setup_run_length >= min_run:
            return True, 0.0
        # 折中：设备因该规则无 lot 可做、且空等已达这次切换的 setup 时长，允许切换。
        threshold = self._expected_setup(current, step)
        since = self._blocked_since.get(tool.tool_id)
        return since is not None and self._now - since >= threshold, threshold

    def _start_blocked_timer(self, tool_id: str, threshold: float) -> None:
        """设备只剩被最小连续量挡住的 lot：开始计时，并在阈值到期时唤醒策略。"""

        since = self._blocked_since.setdefault(tool_id, self._now)
        due = since + threshold
        self._group_horizon = min(self._group_horizon, due)
        if due > self._now and self._blocked_wakeup.get(tool_id) != due:
            self._blocked_wakeup[tool_id] = due
            self._wakeups.append((due, f"setup_avoidance:{tool_id}"))

    def _predict_batch(
        self,
        group_id: str,
        first: LotState,
        candidates: tuple[LotState, ...],
        used_lot_ids: set[str],
    ) -> list[LotState]:
        """按 Batching.gather 的规则预判同批成员（按入厂顺序装到 batch_maximum）。"""

        step = first.current_step
        if step is None:
            return []
        group = self.model.tool_groups.get(group_id)
        by_lot = "lot" in (group.batching_unit or "").lower() if group else False
        same_product, same_family, same_step = self._batch_criteria_by_group.get(
            group_id, (False, False, False)
        )
        quantity = 1 if by_lot else first.wafer_count
        cap = step.batch_maximum
        members: list[LotState] = []
        for candidate in candidates:
            if candidate is first or candidate.id in used_lot_ids:
                continue
            other = candidate.current_step
            if other is None or other.processing_unit != "batch":
                continue
            if other.tool_group_id != step.tool_group_id:
                continue
            if same_product and candidate.product_id != first.product_id:
                continue
            if same_family and self._part_family(candidate) != self._part_family(first):
                continue
            if same_step:
                if other.id != step.id:
                    continue
            elif not (same_product or same_family) and other != step:
                continue
            candidate_quantity = 1 if by_lot else candidate.wafer_count
            if cap is not None and quantity + candidate_quantity > cap:
                continue
            members.append(candidate)
            quantity += candidate_quantity
        return members

    def _part_family(self, lot: LotState) -> str:
        product = self.model.products.get(lot.product_id)
        if product is None:
            return lot.product_id
        return product.part_family or product.id

    # ------------------------------------------------------------ super hot

    def _update_reservations(self, state: StrategyState) -> dict[str, str]:
        """刷新每个 super hot lot 的下游预留，返回 tool_id -> lot_id。"""

        reserved: dict[str, str] = {}
        for lot_id, lot in tuple(self._super_hot.items()):
            if lot.completed or lot.end_time is not None:
                del self._super_hot[lot_id]
                self._reservations.pop(lot_id, None)
                continue
            index = self._reservation_step_index(lot, state)
            if index is None:
                self._reservations.pop(lot_id, None)
                continue
            step = lot.route[index]
            group_id = step.tool_group_id or ""
            if self._rule_by_group.get(group_id) != _RESERVATION_RULE:
                self._reservations.pop(lot_id, None)
                continue
            current = self._reservations.get(lot_id)
            if current is not None:
                current_index, tool_id = current
                tool = state.tool_states[tool_id]
                if current_index == index and not tool.is_down and tool_id not in reserved:
                    reserved[tool_id] = lot_id
                    continue
            tool_id = self._choose_reserved_tool(lot, step, group_id, state, reserved)
            if tool_id is None:
                self._reservations.pop(lot_id, None)
                continue
            self._reservations[lot_id] = (index, tool_id)
            reserved[tool_id] = lot_id
        return reserved

    @staticmethod
    def _reservation_step_index(lot: LotState, state: StrategyState) -> int | None:
        """super hot lot 下一次要用设备的工步下标。

        加工中预留下一道工步；运输或排队中预留当前工步。卸载阶段引擎已把
        operation_index 推进到下一步，此时设备的活动工步不再是当前工步。
        """

        index = lot.operation_index
        if lot.in_process and lot.processing_tool_id is not None:
            tool = state.tool_states[lot.processing_tool_id]
            current_step = lot.route[index] if index < len(lot.route) else None
            if current_step is not None and tool.active_step_id == current_step.id:
                index += 1
        return index if index < len(lot.route) else None

    def _choose_reserved_tool(
        self,
        lot: LotState,
        step: RouteStepSpec,
        group_id: str,
        state: StrategyState,
        reserved: dict[str, str],
    ) -> str | None:
        """LTL 绑定设备优先；否则空闲设备优先，再按最少 setup、最早可用。"""

        dedicated = lot.dedicated_tools.get(step.id) if step.id else None
        if dedicated is not None:
            tool = state.tool_states.get(dedicated)
            if tool is None or tool.is_down or dedicated in reserved:
                return None
            return dedicated
        best: tuple[object, ...] | None = None
        best_tool: str | None = None
        for tool_id in self._tools_by_group.get(group_id, ()):
            if tool_id in reserved:
                continue
            tool = state.tool_states[tool_id]
            if tool.is_down:
                continue
            idle = tool_is_available(tool, state.current_time)
            key = (
                not idle,
                self._setups.expected_time(tool.current_setup, step),
                tool.available_time,
                tool_id,
            )
            if best is None or key < best:
                best, best_tool = key, tool_id
        return best_tool
