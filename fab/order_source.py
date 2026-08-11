"""投料候选 lot 的来源。

来源只负责把 lot 放入 release pool；是否实际进入 fab 始终由策略决定。
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass
from typing import Protocol

from fab.engine.events import EventKind
from fab.model.entities import DistributionSpec, FabModel, LotReleaseSpec, LotState


@dataclass(frozen=True)
class SourceEvent:
    """投料源请求引擎放入日历的一条事件。"""

    time: float
    kind: EventKind
    payload: dict[str, object]


@dataclass(frozen=True)
class SourceEventResult:
    """投料源处理事件后的新日历项和是否需要策略投料决策。"""

    next_events: tuple[SourceEvent, ...] = ()
    release_opportunity: bool = False


class LotSource(Protocol):
    """自动与外部投料源共享的窄接口。"""

    queue: deque[LotState]

    def initial_events(self, start_time: float, end_time: float, release_interval: float) -> tuple[SourceEvent, ...]:
        """返回本次仿真开始时应放入事件日历的供给事件。"""

    def handle_event(self, time: float, kind: EventKind, payload: dict[str, object], end_time: float,
                     release_interval: float) -> SourceEventResult | None:
        """处理属于该来源的事件；无关事件返回 ``None``。"""

    def release(self, time: float, lot_id: str | None = None) -> LotState | None:
        """将策略准入的候选 lot 从 release pool 移入 fab。"""


class SyntheticOrderSource:
    """旧版的自动订单生成器。

    订单按 ``arrival_interval`` 随机生成，策略仍只在 ``release_interval`` 的
    节拍点决定是否从队首放行一个 lot。
    """

    def __init__(self, model: FabModel, random_seed: int) -> None:
        if model.synthetic_order is None:
            raise ValueError("synthetic 投料模式必须提供 synthetic_order。")
        self.model = model
        self.spec = model.synthetic_order
        self.rng = random.Random(random_seed)
        self.queue: deque[LotState] = deque()
        self.orders_generated = 0
        self.generated_count = 0

    def initial_events(self, start_time: float, _: float, __: float) -> tuple[SourceEvent, ...]:
        return (
            SourceEvent(start_time, EventKind.ORDER_ARRIVAL, {}),
            SourceEvent(start_time, EventKind.LOT_RELEASE, {}),
        )

    def handle_event(self, time: float, kind: EventKind, _: dict[str, object], end_time: float,
                     release_interval: float) -> SourceEventResult | None:
        if kind is EventKind.ORDER_ARRIVAL:
            self.generate_order(time)
            next_time = time + self.spec.arrival_interval
            events = ((SourceEvent(next_time, EventKind.ORDER_ARRIVAL, {}),)
                      if next_time < end_time else ())
            return SourceEventResult(events)
        if kind is EventKind.LOT_RELEASE:
            next_time = time + release_interval
            events = ((SourceEvent(next_time, EventKind.LOT_RELEASE, {}),)
                      if next_time < end_time else ())
            return SourceEventResult(events, release_opportunity=True)
        return None

    def generate_order(self, time: float) -> None:
        if self.spec.waiting_capacity and len(self.queue) + self.spec.lots_per_order > self.spec.waiting_capacity:
            return
        self.orders_generated += 1
        product_ids, weights = zip(*self.spec.product_mix)
        for _ in range(self.spec.lots_per_order):
            self.generated_count += 1
            product_id = self.rng.choices(product_ids, weights=weights, k=1)[0]
            self.queue.append(LotState(
                id=f"L{self.generated_count:04d}", product_id=product_id,
                order_id=f"O{self.orders_generated:04d}", generation_time=time,
                release_time=float("inf"), due_time=(time + self.spec.due_time_offset
                                                      if self.spec.due_time_offset else None),
                priority=1, route=self.model.products[product_id].route,
                input_order=self.generated_count, planned_release_time=time,
                ready_time=float("inf"),
            ))

    def release(self, time: float, lot_id: str | None = None) -> LotState | None:
        if not self.queue:
            return None
        if lot_id is None:
            lot = self.queue.popleft()
        else:
            lot = next((candidate for candidate in self.queue if candidate.id == lot_id), None)
            if lot is None:
                return None
            self.queue.remove(lot)
        lot.release_time = lot.ready_time = time
        return lot


class ExternalLotReleaseSource:
    """由已导入的 SMT ``LotReleaseSpec`` 生成候选 lot。

    每个计划在 ``start_date`` 首次到达，后续到达间隔由其
    ``release_distribution`` 采样。到达只进入 release pool，不绕过 DBR。
    """

    def __init__(self, model: FabModel, random_seed: int) -> None:
        if not model.lot_releases:
            raise ValueError("external 投料模式必须提供至少一条 lot_releases 规则。")
        self.model = model
        self.rng = random.Random(random_seed)
        self.queue: deque[LotState] = deque()
        self._release_counts = [0] * len(model.lot_releases)
        self._generated_count = 0
        self._validate_plans()

    def initial_events(self, start_time: float, _: float, __: float) -> tuple[SourceEvent, ...]:
        arrivals = tuple(
            SourceEvent(max(start_time, spec.start_date), EventKind.LOT_AVAILABLE, {"plan_index": index})
            for index, spec in enumerate(self.model.lot_releases)
        )
        # 保留统一的 DBR 决策节拍：即使没有新的外部计划到达，也能准入池中已有 lot。
        return arrivals + (SourceEvent(start_time, EventKind.LOT_RELEASE, {}),)

    def handle_event(self, time: float, kind: EventKind, payload: dict[str, object], end_time: float,
                     release_interval: float) -> SourceEventResult | None:
        if kind is EventKind.LOT_AVAILABLE:
            index = int(payload["plan_index"])
            spec = self.model.lot_releases[index]
            self._generate_release(index, spec, time)
            next_time = time + self._duration(spec.release_distribution)
            events = ((SourceEvent(next_time, EventKind.LOT_AVAILABLE, {"plan_index": index}),)
                      if next_time < end_time else ())
            return SourceEventResult(events, release_opportunity=True)
        if kind is EventKind.LOT_RELEASE:
            next_time = time + release_interval
            events = ((SourceEvent(next_time, EventKind.LOT_RELEASE, {}),)
                      if next_time < end_time else ())
            return SourceEventResult(events, release_opportunity=True)
        return None

    def _generate_release(self, index: int, spec: LotReleaseSpec, time: float) -> None:
        self._release_counts[index] += 1
        lot_type = self.model.lot_types[spec.lot_type_id]
        for _ in range(spec.lots_per_release):
            self._generated_count += 1
            self.queue.append(LotState(
                id=f"E{self._generated_count:06d}", product_id=spec.product_id,
                order_id=f"R{index + 1:03d}-{self._release_counts[index]:05d}",
                generation_time=time, release_time=float("inf"), due_time=spec.due_date,
                priority=lot_type.priority, route=self.model.products[spec.product_id].route,
                input_order=self._generated_count, planned_release_time=time,
                ready_time=float("inf"), lot_type_id=lot_type.id, wafer_count=lot_type.wafer_count,
                is_super_hot=lot_type.is_super_hot,
            ))

    def release(self, time: float, lot_id: str | None = None) -> LotState | None:
        if not self.queue:
            return None
        if lot_id is None:
            lot = self.queue.popleft()
        else:
            lot = next((candidate for candidate in self.queue if candidate.id == lot_id), None)
            if lot is None:
                return None
            self.queue.remove(lot)
        lot.release_time = lot.ready_time = time
        return lot

    def _validate_plans(self) -> None:
        for spec in self.model.lot_releases:
            product = self.model.products.get(spec.product_id)
            if product is None:
                raise ValueError(f"Lotrelease 引用了未知产品：{spec.product_id}。")
            if product.route_name is not None and product.route_name != spec.route_name:
                raise ValueError(f"Lotrelease 的路线 {spec.route_name} 与产品 {product.id} 不一致。")
            if spec.lot_type_id not in self.model.lot_types:
                raise ValueError(f"Lotrelease 引用了未知 lot type：{spec.lot_type_id}。")
            if spec.lots_per_release <= 0:
                raise ValueError("Lotrelease 的 lots_per_release 必须为正数。")
            distribution = spec.release_distribution
            kind = distribution.kind.lower()
            if kind not in {"constant", "uniform", "exponential"}:
                raise ValueError(f"不支持的投料间隔分布：{distribution.kind}。")
            if distribution.mean <= 0 or (kind == "uniform" and distribution.mean <= distribution.offset):
                raise ValueError("Lotrelease 的 release_distribution 必须产生正间隔。")

    def _duration(self, distribution: DistributionSpec) -> float:
        kind = distribution.kind.lower()
        if kind == "constant":
            return distribution.mean
        if kind == "uniform":
            return self.rng.uniform(distribution.mean - distribution.offset, distribution.mean + distribution.offset)
        if kind == "exponential":
            return self.rng.expovariate(1 / distribution.mean)
        raise ValueError(f"不支持的投料间隔分布：{distribution.kind}。")


def create_lot_source(model: FabModel, random_seed: int) -> LotSource:
    """根据显式 source_mode 创建唯一投料来源。"""

    if model.source_mode == "synthetic":
        return SyntheticOrderSource(model, random_seed)
    if model.source_mode == "external":
        return ExternalLotReleaseSource(model, random_seed)
    raise ValueError(f"未知投料模式：{model.source_mode}；仅支持 synthetic 或 external。")
