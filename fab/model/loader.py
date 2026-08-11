"""加载教学版的单一 JSON 模型。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fab.model.entities import (
    DistributionSpec, FabModel, LotReleaseSpec, LotTypeSpec, ProductSpec,
    RouteStepSpec, SimulationSpec, SyntheticOrderSpec, ToolSpec,
)


def _by_id(items: list[object], label: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in items:
        item_id = str(getattr(item, "id"))
        if item_id in result:
            raise ValueError(f"{label} 的 ID 重复：{item_id}。")
        result[item_id] = item
    return result


def load_fab_model(path: Path) -> FabModel:
    """读取最小模型及其显式投料来源。"""

    with path.open(encoding="utf-8") as file:
        payload: dict[str, Any] = json.load(file)
    factory = payload.get("factory", {})
    if not factory.get("id"):
        raise ValueError("factory.id 为必填项。")

    tools = _by_id([
        ToolSpec(str(item["id"]), str(item["name"]), str(item["process"]),
                 float(item.get("available_from", 0)))
        for item in payload.get("tools", [])
    ], "tools")
    products = _by_id([
        ProductSpec(
            str(item["id"]), str(item["name"]),
            tuple(RouteStepSpec(str(step["process"]), float(step["process_time"]))
                  for step in item.get("route", [])),
            str(item["route_name"]) if item.get("route_name") is not None else None,
        )
        for item in payload.get("products", [])
    ], "products")
    source = payload.get("order_source", {})
    source_mode = str(source["mode"])
    if source_mode == "synthetic":
        synthetic_order: SyntheticOrderSpec | None = SyntheticOrderSpec(
            float(source["arrival_interval"]), int(source["lots_per_order"]),
            int(source.get("waiting_capacity", 0)),
            tuple((str(item["product_id"]), float(item["weight"]))
                  for item in source.get("product_mix", [])),
            float(source["due_time_offset"]) if source.get("due_time_offset") is not None else None,
        )
    elif source_mode == "external":
        synthetic_order = None
    else:
        raise ValueError("order_source.mode 必须是 synthetic 或 external。")

    lot_types = _by_id([
        LotTypeSpec(
            str(item["id"]), str(item.get("name", item["id"])),
            int(item.get("priority", 1)), bool(item.get("is_super_hot", False)),
            int(item.get("wafer_count", 25)),
        )
        for item in payload.get("lot_types", [])
    ], "lot_types")
    lot_releases = tuple(
        LotReleaseSpec(
            product_id=str(item["product_id"]), route_name=str(item["route_name"]),
            lot_type_id=str(item["lot_type_id"]), start_date=float(item["start_date"]),
            release_distribution=_distribution(item["release_distribution"]),
            lots_per_release=int(item["lots_per_release"]),
            due_date=float(item["due_date"]) if item.get("due_date") is not None else None,
        )
        for item in payload.get("lot_releases", [])
    )
    simulation = payload.get("simulation", {})
    model = FabModel(
        id=str(factory["id"]),
        time_unit=str(factory.get("time_unit", "minute")),
        tools=tools,  # type: ignore[arg-type]
        products=products,  # type: ignore[arg-type]
        synthetic_order=synthetic_order,
        simulation=SimulationSpec(
            float(simulation.get("start_time", 0)), float(simulation["end_time"]),
            float(simulation["release_interval"]), float(simulation.get("warmup_time", 0)),
        ),
        source_mode=source_mode,
        lot_types=lot_types,  # type: ignore[arg-type]
        lot_releases=lot_releases,
    )
    _validate(model)
    return model


def _validate(model: FabModel) -> None:
    if not model.tools or not model.products:
        raise ValueError("模型至少需要一台设备和一种产品。")
    if any(tool.available_from < 0 or not tool.process for tool in model.tools.values()):
        raise ValueError("设备必须有工艺类型，available_from 不能为负数。")
    for product in model.products.values():
        if not product.route or any(step.process_time <= 0 for step in product.route):
            raise ValueError(f"产品 {product.id} 的路线必须非空且加工时间为正数。")
    if model.source_mode == "synthetic":
        order = model.synthetic_order
        if order is None:
            raise ValueError("synthetic 模式缺少 order_source 配置。")
        if order.arrival_interval <= 0 or order.lots_per_order <= 0 or order.waiting_capacity < 0:
            raise ValueError("order_source 的到达间隔、订单量或容量无效。")
        if not order.product_mix or sum(weight for _, weight in order.product_mix) <= 0:
            raise ValueError("order_source.product_mix 必须包含正权重。")
        if any(product_id not in model.products or weight < 0 for product_id, weight in order.product_mix):
            raise ValueError("order_source.product_mix 引用了未知产品或负权重。")
    elif model.source_mode == "external":
        if model.synthetic_order is not None or not model.lot_releases:
            raise ValueError("external 模式只能使用非空的 lot_releases。")
    else:
        raise ValueError("未知投料模式。")
    simulation = model.simulation
    if simulation.start_time < 0 or simulation.end_time <= simulation.start_time or simulation.release_interval <= 0:
        raise ValueError("simulation 的时间范围或 release_interval 无效。")


def _distribution(value: Any) -> DistributionSpec:
    if not isinstance(value, dict):
        raise ValueError("release_distribution 必须是对象。")
    return DistributionSpec(
        kind=str(value["kind"]), mean=float(value["mean"]),
        offset=float(value.get("offset", 0)), unit=str(value.get("unit", "minute")),
    )
