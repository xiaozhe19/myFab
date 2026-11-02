"""新引擎与派工策略之间的窄接口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, TypeAlias

from fab.model.entities import FabModel, LotState, ToolState

DispatchableCandidate: TypeAlias = tuple[ToolState, tuple[LotState, ...]]


class CompletionSnapshot:
    """Shared version of the engine's completion table, detached before writes."""

    def __init__(self, data: dict[tuple[str, int], float]) -> None:
        self.data = data


@dataclass(frozen=True)
class StrategyState:
    """策略在一个事件时刻可读取的工厂快照。"""

    model: FabModel
    current_time: float
    lots: tuple[LotState, ...]
    waiting_lots: tuple[LotState, ...]
    tool_states: dict[str, ToolState]
    waiting_lot_count: int
    event_kinds: tuple[str, ...] = ()
    strategy_reasons: tuple[str, ...] = ()
    # (process, visit_number) -> 最近一次该工序访问完成的模拟时刻。
    last_operation_completion: dict[tuple[str, int], float] = field(default_factory=dict)
    # 已按 (available_time, tool_id) 排序的设备及其合法候选 Lot。
    dispatchable_candidates: tuple[DispatchableCandidate, ...] = ()
    # 决策轨迹只用于调试和回放；正式批量运行可关闭，避免每个事件构造大量 JSON。
    record_diagnostics: bool = True
    # 本轮状态更新涉及的 lot。策略可据此增量维护其内部 WIP 索引；未声明使用
    # 该字段的第三方策略仍可继续读取完整 lots 快照。
    changed_lots: tuple[LotState, ...] = ()

    def _set_completion_dict(self, value) -> None:
        object.__setattr__(self, '_completion_value', value)

    def _completion_dict(self) -> dict[tuple[str, int], float]:
        value = self._completion_value
        if isinstance(value, CompletionSnapshot):
            value = dict(value.data)
            object.__setattr__(self, '_completion_value', value)
        return value


# Keep the public dataclass field, constructor, comparison and serialization;
# defer materializing its private copy until the field is actually accessed.
StrategyState.last_operation_completion = property(
    StrategyState._completion_dict, StrategyState._set_completion_dict
)

@dataclass
class StrategyDecision:
    """策略给出的派工动作；引擎负责再次校验后执行。投料由 ReleasePolicy 负责。"""

    dispatches: dict[str, LotState] = field(default_factory=dict)
    wakeups: list[tuple[float, str]] = field(default_factory=list)
    # 策略可选地写入决策解释；引擎只保存，不参与算法或动作校验。
    diagnostics: dict[str, object] = field(default_factory=dict)


# 投料员可以订阅的触发；引擎在结算点按此顺序通知。
RELEASE_TRIGGERS = ("order_arrival", "lot_completed", "release_wakeup")


class ReleaseHandle(Protocol):
    """引擎交给投料员的"下单电话"。"""

    def release(self, lot_id: str) -> None:
        """登记放行订单池中的一个 lot；在本结算点统一执行。"""

    def schedule_wakeup(self, time: float) -> None:
        """在 ``time`` 时刻（不早于当前时刻）触发一次 release_wakeup。"""


class ReleasePolicy(Protocol):
    """投料员：声明关心的触发，被通知时自己决定是否下单。

    引擎在事件处理中只记录触发，在同一时刻的事件全部结算、派工决策做完后，
    才用同一份 state 调用 ``on_trigger``，保证投料员看到的状态与派工一致。
    """

    name: str
    # RELEASE_TRIGGERS 的子集。release_wakeup 只由投料员自己订的闹钟产生。
    triggers: frozenset[str]

    def initialize(self, model: FabModel, handle: ReleaseHandle) -> None:
        """仿真开始前调用一次，可在此订第一个闹钟。"""

    def on_trigger(
        self, trigger: str, state: StrategyState, handle: ReleaseHandle
    ) -> None:
        """收到一种已订阅的触发。"""


class FabStrategy(Protocol):
    """所有新策略遵守的接口：派工由 decide 给出，投料交给 release。"""

    name: str
    release: ReleasePolicy

    def initialize(self, model: FabModel) -> None:
        """仿真开始前调用一次。"""

    def decide(self, state: StrategyState) -> StrategyDecision:
        """在当前事件时刻给出派工决策。"""
