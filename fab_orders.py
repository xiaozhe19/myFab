from __future__ import annotations

import random
from collections import deque
from typing import Any, Callable, Protocol


class OrderLot(Protocol):
    id: str
    order_id: str | None
    product_id: str
    product_name: str
    route: list[dict[str, Any]]
    generation_time: float
    release_time: float
    ready_time: float


LotFactory = Callable[[float, dict[str, Any], str, int], OrderLot]


class FabOrderPool:
    """
    Fab 外部的订单生成器与 waiting list。

    订单按固定到达周期生成。每张订单包含若干 lot，每个 lot 按订单配置
    中的 product_mix 权重独立随机选择产品，然后进入 FIFO waiting list。
    """

    def __init__(
        self,
        order_config: dict[str, Any],
        products: dict[str, Any],
        lot_factory: LotFactory,
        random_seed: int,
    ) -> None:
        arrival = order_config.get("arrival", {})
        if arrival.get("type", "fixed_interval") != "fixed_interval":
            raise ValueError("Only fixed_interval order arrival is currently supported.")
        self.arrival_type = str(arrival.get("type", "fixed_interval"))
        self.order_interval = float(arrival.get("interval", 720))
        self.lots_per_order = int(order_config.get("lots_per_order", 1))
        self.capacity = int(
            order_config.get("waiting_list", {}).get("max_size", 0)
        )
        if self.order_interval <= 0:
            raise ValueError("release.order_interval must be greater than zero.")
        if self.lots_per_order <= 0:
            raise ValueError("release.lots_per_order must be greater than zero.")
        if self.capacity < 0:
            raise ValueError("release.max_waiting_list_size cannot be negative.")

        self.products = list(products.values())
        if not self.products:
            raise ValueError("products must contain at least one product.")

        product_mix = order_config.get("product_mix", {})
        self.product_mix = {
            str(product_id): float(weight)
            for product_id, weight in product_mix.items()
        }
        self.weights = [
            float(self.product_mix.get(product["id"], 0.0))
            for product in self.products
        ]
        if any(weight < 0 for weight in self.weights) or sum(self.weights) <= 0:
            raise ValueError(
                "Product generation weights must be non-negative with a positive sum."
            )

        self.rng = random.Random(int(random_seed))
        self.lot_factory = lot_factory
        self.queue: deque[OrderLot] = deque()
        self.next_lot_number = 1
        self.next_order_number = 1
        self.generated_count = 0

    def generate_order(self, generation_time: float) -> dict[str, Any]:
        """生成一张随机混合产品订单，并将其 lot 放入 waiting list。"""

        if self.capacity and len(self.queue) + self.lots_per_order > self.capacity:
            return {
                "accepted": False,
                "time": round(generation_time, 3),
                "reason": "waiting_list_capacity",
                "queue_size": len(self.queue),
            }

        order_id = f"O{self.next_order_number:04d}"
        self.next_order_number += 1
        lot_ids: list[str] = []
        lots: list[dict[str, Any]] = []

        for _ in range(self.lots_per_order):
            product = self.rng.choices(
                self.products,
                weights=self.weights,
                k=1,
            )[0]
            lot_number = self.next_lot_number
            self.next_lot_number += 1
            lot = self.lot_factory(
                generation_time,
                product,
                order_id,
                lot_number,
            )
            self.queue.append(lot)
            self.generated_count += 1
            lot_ids.append(lot.id)
            lots.append(
                {
                    "id": lot.id,
                    "product_id": lot.product_id,
                    "product_name": lot.product_name,
                }
            )

        return {
            "accepted": True,
            "order_id": order_id,
            "time": round(generation_time, 3),
            "quantity": self.lots_per_order,
            "wafer_ids": lot_ids,
            "lots": lots,
            "queue_size": len(self.queue),
        }

    def release_head(self, release_time: float) -> OrderLot | None:
        if not self.queue:
            return None
        lot = self.queue.popleft()
        lot.release_time = round(release_time, 3)
        lot.ready_time = lot.release_time
        return lot

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "position": position,
                "id": lot.id,
                "order_id": lot.order_id,
                "product_id": lot.product_id,
                "product_name": lot.product_name,
                "generation_time": lot.generation_time,
            }
            for position, lot in enumerate(self.queue, start=1)
        ]
