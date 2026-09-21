"""新引擎与派工策略之间的窄接口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, TypeAlias

from fab.model.entities import FabModel, LotState, ToolState

DispatchableCandidate: TypeAlias = tuple[ToolState, tuple[LotState, ...]]


@dataclass(frozen=True)
class StrategyState:
    """策略在一个事件时刻可读取的工厂快照。"""

    model: FabModel
    current_time: float
    lots: tuple[LotState, ...]
    waiting_lots: tuple[LotState, ...]
    tool_states: dict[str, ToolState]
    waiting_lot_count: int
    release_opportunity: bool
    event_kinds: tuple[str, ...] = ()
    strategy_reasons: tuple[str, ...] = ()
    # (process, visit_number) -> 最近一次该工序访问完成的模拟时刻。
    last_operation_completion: dict[tuple[str, int], float] = field(
        default_factory=dict
    )
    # 已按 (available_time, tool_id) 排序的设备及其合法候选 Lot。
    dispatchable_candidates: tuple[DispatchableCandidate, ...] = ()
    # 决策轨迹只用于调试和回放；正式批量运行可关闭，避免每个事件构造大量 JSON。
    record_diagnostics: bool = True
    # 本轮状态更新涉及的 lot。策略可据此增量维护其内部 WIP 索引；未声明使用
    # 该字段的第三方策略仍可继续读取完整 lots 快照。
    changed_lots: tuple[LotState, ...] = ()

@dataclass
class StrategyDecision:
    """策略给出的投料和派工动作；引擎负责再次校验后执行。"""

    dispatches: dict[str, LotState] = field(default_factory=dict)
    # 未指定 ID 时，True 表示按订单池 FIFO 放行一个 lot；False 表示不放行。
    release_lot: bool = False
    # 可选定向投料。给出时隐含放行，且必须引用当前订单池中的一个 lot。
    release_lot_id: str | None = None
    # 预留：策略主动组批 tool_id -> 同炉 lot 列表。仅在引擎
    # batch_from_strategy=True 时生效；缺省 None 表示引擎自主组批。
    batches: dict[str, list[LotState]] = field(default_factory=dict)
    wakeups: list[tuple[float, str]] = field(default_factory=list)
    # 策略可选地写入决策解释；引擎只保存，不参与算法或动作校验。
    diagnostics: dict[str, object] = field(default_factory=dict)


class FabStrategy(Protocol):
    """所有新策略遵守的接口。"""

    name: str

    def initialize(self, model: FabModel) -> None:
        """仿真开始前调用一次。"""

    def decide(self, state: StrategyState) -> StrategyDecision:
        """在当前事件时刻给出下一步投料和派工决策。"""
