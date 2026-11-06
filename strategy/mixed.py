"""分设备组的加权排名混合规则（Fabotron 阶段 1.0）。

没有权重的组用原版 FIFO_SA（字典序：优先级 -> setup-aware 最少 setup -> FIFO）。
有权重的组按加权排名打分，分数小的先派：

1. 对 FIFO、EDD、CR、SPT、SRPT 五个排序键，各取 lot 的原始值，在本组当前
   队列（本次决策中该组所有设备的候选 lot 并集）里排名，
   r = (名次 - 1) / (n - 1)，并列取平均名次；n = 1 时 r = 0。
2. SETUP 维：该 (设备, lot) 配对需要换型记 1，否则记 0（setup-aware 计法）。
   它本身已在 [0, 1]，不再排名。
3. score = Σ w_k · r_k。完整键为 (优先级, score, ready_time, input_order, id,
   设备顺序)；setup 不再单独占字典序的一层。

组批组：沿用公共底座，分数最小的 lot 作为批首，同批成员由组批规则补齐。

权重带时间表：``schedule`` 是按开始时刻排列的若干段，每段给出
{组: {规则: 权重}}；派工时按当前时刻取所在段。权重会归一化到和为 1。
"""

from __future__ import annotations

from bisect import bisect_right
from math import inf

from fab.model.entities import FabModel, LotState, ToolState
from fab.strategy import ReleasePolicy
from strategy.base_rule import BaseRule

RULES = ("FIFO", "EDD", "CR", "SPT", "SRPT", "SETUP")
_DAY = 1440.0


def _ranks(values: list[float]) -> list[float]:
    """把原始值转成 [0, 1] 的归一化名次，并列取平均名次。"""

    n = len(values)
    if n <= 1:
        return [0.0] * n
    order = sorted(range(n), key=values.__getitem__)
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        r = (i + j) / 2 / (n - 1)
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    return ranks


class MixedRule(BaseRule):
    name = "MIXED"
    rank_rule = "FIFO（无权重组）/ 加权排名（有权重组）"

    def __init__(
        self,
        schedule: list[dict[str, object]] | None = None,
        release: ReleasePolicy | None = None,
    ) -> None:
        """``schedule``：[{"start_day": 0, "weights": {组: {规则: 权重}}}, ...]。"""

        super().__init__(setup_aware_ranking=True, release=release)
        self.name = "MIXED"
        # 当前正在派工的组的权重与 lot 分数；只在 _dispatch_group_pairs 内有效。
        self._weights: tuple[float, ...] | None = None
        self._scores: dict[str, float] = {}
        self.set_schedule(schedule)

    def set_schedule(self, schedule: list[dict[str, object]] | None) -> None:
        """替换权重时间表。可在仿真中途调用（如预热后复制出的引擎各换一张表）。"""

        segments = []
        for segment in schedule or ():
            weights: dict[str, tuple[float, ...]] = {}
            for group_id, by_rule in dict(segment.get("weights", {})).items():
                unknown = set(by_rule) - set(RULES)
                if unknown:
                    raise ValueError(f"未知规则 {sorted(unknown)}，可用 {RULES}")
                total = sum(float(v) for v in by_rule.values())
                if total <= 0:
                    raise ValueError(f"组 {group_id} 的权重之和必须为正")
                weights[group_id] = tuple(
                    float(by_rule.get(rule, 0.0)) / total for rule in RULES
                )
            segments.append((float(segment.get("start_day", 0.0)) * _DAY, weights))
        segments.sort(key=lambda item: item[0])
        self._segment_starts = [start for start, _ in segments]
        self._segment_weights = [weights for _, weights in segments]
        if hasattr(self, "model"):
            self._check_groups()

    def initialize(self, model: FabModel) -> None:
        super().initialize(model)
        self._check_groups()

    def _check_groups(self) -> None:
        for weights in self._segment_weights:
            missing = set(weights) - set(self.model.tool_groups)
            if missing:
                raise ValueError(f"时间表里的组不在模型中：{sorted(missing)}")

    def weights_at(self, time: float) -> dict[str, tuple[float, ...]]:
        index = bisect_right(self._segment_starts, time) - 1
        return self._segment_weights[index] if index >= 0 else {}

    # ------------------------------------------------------------------ 打分

    def _dispatch_group_pairs(
        self,
        group_id: str,
        entries: list[tuple[ToolState, tuple[LotState, ...]]],
        reserved_tools: dict[str, str],
        used_lot_ids: set[str],
        dispatches: dict[str, LotState],
        explanations: dict[str, object] | None,
    ) -> None:
        self._weights = self.weights_at(self._now).get(group_id)
        if self._weights is not None:
            self._scores = self._score_queue(entries)
        try:
            super()._dispatch_group_pairs(
                group_id, entries, reserved_tools, used_lot_ids, dispatches, explanations
            )
        finally:
            self._weights = None
            self._scores = {}

    def _score_queue(
        self, entries: list[tuple[ToolState, tuple[LotState, ...]]]
    ) -> dict[str, float]:
        lots: dict[str, LotState] = {}
        for _, candidates in entries:
            for lot in candidates:
                lots.setdefault(lot.id, lot)
        queue = list(lots.values())
        weights = self._weights
        assert weights is not None
        scores = [0.0] * len(queue)
        now = self._now
        for k, rule in enumerate(RULES[:-1]):
            w = weights[k]
            if w == 0.0:
                continue
            if rule == "FIFO":
                values = [lot.ready_time for lot in queue]
            elif rule == "EDD":
                values = [inf if lot.due_time is None else lot.due_time for lot in queue]
            elif rule == "CR":
                values = []
                for lot in queue:
                    remaining = self.remaining_work(lot)
                    values.append(
                        inf
                        if lot.due_time is None or remaining <= 0.0
                        else (lot.due_time - now) / remaining
                    )
            elif rule == "SPT":
                values = [
                    self.step_work(lot.route[lot.operation_index], lot.wafer_count)
                    for lot in queue
                ]
            else:  # SRPT
                values = [self.remaining_work(lot) for lot in queue]
            for i, r in enumerate(_ranks(values)):
                scores[i] += w * r
        return {lot.id: score for lot, score in zip(queue, scores)}

    def rank_key(self, lot: LotState) -> tuple[object, ...]:
        if self._weights is None:
            return (lot.ready_time, lot.release_time, lot.input_order, lot.id)
        return (self._scores[lot.id], lot.ready_time, lot.input_order, lot.id)

    def pair_key(
        self, lot: LotState, setup: float, tool_rank: int
    ) -> tuple[object, ...]:
        weights = self._weights
        if weights is None:
            return super().pair_key(lot, setup, tool_rank)
        score = self._scores[lot.id] + (weights[-1] if setup > 0.0 else 0.0)
        return (-lot.priority, score, lot.ready_time, lot.input_order, lot.id, tool_rank)
