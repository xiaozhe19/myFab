"""新引擎与派工策略之间的窄接口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Protocol

from fab.model.entities import FabModel, LotState, ToolState


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
    # 引擎按硬约束计算的候选集合；策略只负责从中选择，不负责重新扫描全部 Lot。
    eligible_lots_by_tool: dict[str, tuple[LotState, ...]] = field(default_factory=dict)
    # (process, visit_number) -> 最近一次该工序访问完成的模拟时刻。
    last_operation_completion: dict[tuple[str, int], float] = field(
        default_factory=dict
    )
    available_tools: tuple[ToolState, ...] = ()
    # 优化：引擎预计算"当前至少有一个合法候选 lot"的设备，已按
    # (available_time, tool_id) 排序。策略派工时应优先遍历此列表，避免每次
    # 决策对全部可用设备（约 1400 台）做无意义遍历与排序。
    dispatchable_tools: tuple[ToolState, ...] = ()

    @property
    def wafers(self) -> tuple[LotState, ...]:
        """DBR/RL 使用的真实 lot 集合。"""
        return self.lots

    @property
    def machines(self) -> tuple[ToolState, ...]:
        return tuple(self.tool_states.values())

    @property
    def waiting_list(self) -> SimpleNamespace:
        return SimpleNamespace(queue=self.waiting_lots)

    @property
    def products(self) -> dict[str, dict[str, object]]:
        return {
            product.id: {
                "id": product.id,
                "name": product.name,
                "route": product.route,
            }
            for product in self.model.products.values()
        }


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


@dataclass(frozen=True)
class StrategyWakeup:
    """策略请求未来再次决策的时间点。"""

    time: float
    reason: str


class FabStrategy(Protocol):
    """所有新策略遵守的接口。"""

    name: str

    def initialize(self, model: FabModel) -> None:
        """仿真开始前调用一次。"""

    def decide(self, state: StrategyState) -> StrategyDecision:
        """在当前事件时刻给出下一步投料和派工决策。"""
