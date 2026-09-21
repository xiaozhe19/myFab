"""把 SQLite 中保存的原始 SMT2020 表构造成内存 ``FabModel``。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fab.importer.types import RawSheetData, RawSmtWorkbook
from fab.model.entities import (
    BreakdownSpec,
    DispatchRuleSpec,
    DistributionSpec,
    FabModel,
    LotReleaseSpec,
    LotTypeSpec,
    PreventiveMaintenanceSpec,
    ProductSpec,
    RouteStepSpec,
    SetupTransitionSpec,
    SimulationSpec,
    ToolGroupSpec,
    ToolSpec,
    TransportRuleSpec,
)


_TIME_TO_MINUTES = {
    "min": 1.0, "minute": 1.0, "minutes": 1.0,
    "hr": 60.0, "hour": 60.0, "hours": 60.0,
    "day": 1440.0, "days": 1440.0,
}


def build_fab_model(
    workbook: RawSmtWorkbook,
    simulation: SimulationSpec,
    *,
    model_id: str,
) -> FabModel:
    """从原始 SMT 表派生一次运行所需的领域对象。

    本函数的所有产物仅存在内存中，不回写 SQLite。
    """

    sheets = {sheet.name: _records(sheet) for sheet in workbook.sheets}
    groups = _build_tool_groups(_sheet(sheets, "Toolgroups"))
    routes = _build_routes(sheets)
    release_rows = _sheet(sheets, "Lotrelease")
    origin = min(_datetime(row["START DATE"]) for row in release_rows)
    releases = _build_lot_releases(release_rows, origin)
    products = _derive_products(release_rows, routes)
    lot_types = _derive_lot_types(release_rows)
    tools = _expand_tools(groups)

    return FabModel(
        id=model_id,
        time_unit="minute",
        tools=tools,
        products=products,
        synthetic_order=None,
        simulation=simulation,
        source_mode="external",
        tool_groups=groups,
        lot_types=lot_types,
        lot_releases=releases,
        preventive_maintenance=_build_pm(sheets.get("PM", [])),
        breakdowns=_build_breakdowns(sheets.get("Breakdown", [])),
        setup_transitions=_build_setups(sheets.get("Setups", [])),
        transport_rules=_build_transport(sheets.get("Transport", [])),
    )


def _build_tool_groups(rows: list[dict[str, Any]]) -> dict[str, ToolGroupSpec]:
    groups: dict[str, ToolGroupSpec] = {}
    for row in rows:
        group_id = _required(row, "TOOLGROUP")
        area = _required(row, "AREA")
        dispatch_name = _text(row.get("DISPATCHING"))
        rankings = tuple(
            value for value in (_text(row.get("Ranking 1")), _text(row.get("Ranking 2")), _text(row.get("Ranking 3")))
            if value
        )
        dispatch = (
            DispatchRuleSpec(dispatch_name, rankings, _text(row.get("TOOL WAKE UP Ranking")))
            if dispatch_name else None
        )
        groups[group_id] = ToolGroupSpec(
            id=group_id,
            area=area,
            name=group_id,
            number_of_tools=_integer(row["NUMBER OF TOOLS"]),
            process=area,
            cascading_tool=_yes_no(row.get("CASCADINGTOOL")),
            batching_tool=_yes_no(row.get("BACTHINGTOOL")),
            batch_criterion=_text(row.get("BATCHCRITERION")),
            batching_unit=_text(row.get("BATCHING UNIT")),
            loading_time=_duration(row.get("LOADINGTIME"), row.get("LT UNITS")),
            unloading_time=_duration(row.get("UNLOADINGTIME"), row.get("ULT UNITS")),
            location=_text(row.get("TOOLGROUPLOCATION")),
            dispatch_rule=dispatch,
        )
    return groups


def _build_routes(sheets: dict[str, list[dict[str, Any]]]) -> dict[str, tuple[RouteStepSpec, ...]]:
    routes: dict[str, tuple[RouteStepSpec, ...]] = {}
    for sheet_name, rows in sheets.items():
        if not sheet_name.startswith("Route_Product_"):
            continue
        steps: list[tuple[int, RouteStepSpec]] = []
        for row in rows:
            step_number = _integer(row["STEP"])
            process = _required(row, "AREA")
            process_time = _distribution(
                row["PROCESSINGTIME DISTRIBUTION"], row["MEAN"], row.get("OFFSET"), row["PT UNITS"],
            )
            setup = _optional_distribution(
                row.get("SETUP DISTRIBUTION"), row.get("SETUP TIME"), row.get("OFFSET.1"), row.get("ST UNITS"),
            )
            steps.append((step_number, RouteStepSpec(
                id=str(step_number),
                process=process,
                process_time=process_time.mean,
                description=_text(row.get("STEP DESCRIPTION")),
                area=process,
                tool_group_id=_required(row, "TOOLGROUP"),
                processing_unit=_required(row, "PROCESSING UNIT").lower(),
                processing_distribution=process_time,
                cascading_interval=_optional_duration(row.get("CASCADING INTERVAL"), row.get("C Units")),
                cascading_interval_unit="minute" if row.get("CASCADING INTERVAL") is not None else None,
                batch_minimum=_optional_integer(row.get("BATCH MINIMUM")),
                batch_maximum=_optional_integer(row.get("BATCH MAXIMUM")),
                required_setup=_text(row.get("SETUP")),
                setup_when=_text(row.get("WHEN")) or "if_needed",
                setup_distribution=setup,
                lot_to_lens_dedication_step=_text(row.get("STEP FOR LTL DEDICATION")),
                rework_probability=_percentage(row.get("REWORK PROBABILITY in %"), default=0.0),
                rework_unit=_text(row.get("R UNIT")),
                rework_step=_text(row.get("STEP FOR REWORK")),
                sampling_probability=_percentage(row.get("PROCESSING PROBABILITY in % (Sampling)"), default=1.0),
                critical_queue_time_step=_text(row.get("STEP FOR CRITICAL QUEUE TIME")),
                critical_queue_time=_optional_duration(row.get("CQT"), row.get("CQTUNITS")),
                critical_queue_time_unit="minute" if row.get("CQT") is not None else None,
            )))
        if not steps:
            continue
        route_name = _required(rows[0], "ROUTE")
        routes[route_name] = tuple(step for _, step in sorted(steps, key=lambda item: item[0]))
    return routes


def _derive_products(
    release_rows: list[dict[str, Any]], routes: dict[str, tuple[RouteStepSpec, ...]],
) -> dict[str, ProductSpec]:
    products: dict[str, ProductSpec] = {}
    for row in release_rows:
        product_id = _required(row, "PRODUCT NAME")
        route_name = _required(row, "ROUTE NAME")
        if route_name not in routes:
            raise ValueError(f"产品 {product_id} 引用了不存在的路线 {route_name}。")
        part_family = (
            _text(row.get("PARTFAM"))
            or _text(row.get("PART FAMILY"))
            or _text(row.get("PART FAMILY NAME"))
            or product_id
        )
        products.setdefault(
            product_id,
            ProductSpec(
                product_id,
                product_id,
                routes[route_name],
                route_name,
                part_family,
            ),
        )
    return products


def _derive_lot_types(rows: list[dict[str, Any]]) -> dict[str, LotTypeSpec]:
    result: dict[str, LotTypeSpec] = {}
    for row in rows:
        lot_type_id = _required(row, "LOT NAME/TYPE")
        result.setdefault(lot_type_id, LotTypeSpec(
            lot_type_id,
            lot_type_id,
            priority=_integer(row["PRIORITY"]),
            is_super_hot=_yes_no(row.get("SUPERHOTLOT")),
            wafer_count=_integer(row["WAFERS PER LOT"]),
        ))
    return result


def _build_lot_releases(rows: list[dict[str, Any]], origin: datetime) -> tuple[LotReleaseSpec, ...]:
    return tuple(
        LotReleaseSpec(
            product_id=_required(row, "PRODUCT NAME"),
            route_name=_required(row, "ROUTE NAME"),
            lot_type_id=_required(row, "LOT NAME/TYPE"),
            start_date=_minutes_since(_datetime(row["START DATE"]), origin),
            release_distribution=_distribution(
                row["RELEASE DISTRIBUTION"], row["RELEASE INTERVAL"], 0, row["R UNITS"],
            ),
            lots_per_release=_integer(row["LOTS PER RELEASE"]),
            due_date=(
                _minutes_since(_datetime(row["DUE DATE"]), origin)
                if row.get("DUE DATE") is not None else None
            ),
        )
        for row in rows
    )


def _expand_tools(groups: dict[str, ToolGroupSpec]) -> dict[str, ToolSpec]:
    return {
        f"{group.id}#{number:03d}": ToolSpec(
            id=f"{group.id}#{number:03d}",
            name=f"{group.id}#{number:03d}",
            process=group.process,
            tool_group_id=group.id,
            location=group.location,
        )
        for group in groups.values()
        for number in range(1, group.number_of_tools + 1)
    }


def _build_pm(rows: list[dict[str, Any]]) -> tuple[PreventiveMaintenanceSpec, ...]:
    result = []
    for row in rows:
        pm_type = _required(row, "PM TYPE").lower().replace(" ", "_")
        time_based = "time" in pm_type
        unit = _required(row, "MTBPM UNITS")
        result.append(PreventiveMaintenanceSpec(
            event_name=_required(row, "PM EVENT NAME"),
            valid_for_type=_required(row, "PM EVENT VALID FOR TYPE").lower(),
            type_name=_required(row, "TYPE NAME"),
            pm_type="time_based" if time_based else "wafer_count",
            mean_time_before_pm=_duration(row["MTBeforePM"], unit) if time_based else _number(row["MTBeforePM"]),
            mtbpm_unit="minute" if time_based else unit.lower(),
            repair_distribution=_distribution(row["TTR DISTRIBUTION"], row["MEAN"], row.get("OFFSET"), row["TTR UNITS"]),
            first_one_at_distribution=_optional_distribution(
                row.get("FIRST ONE AT DISTRIBUTION"), row.get("FOA"), 0, row.get("FOA UNITS"),
                normalize_time=time_based,
            ),
            first_one_at=(
                _duration(row["FOA"], row["FOA UNITS"]) if time_based and row.get("FOA") is not None
                else _optional_number(row.get("FOA"))
            ),
            first_one_at_unit="minute" if time_based and row.get("FOA") is not None else unit.lower(),
        ))
    return tuple(result)


def _build_breakdowns(rows: list[dict[str, Any]]) -> tuple[BreakdownSpec, ...]:
    return tuple(
        BreakdownSpec(
            event_name=_required(row, "DOWN EVENT NAME"),
            valid_for_type=_required(row, "DOWN EVENT VALID FOR TYPE").lower(),
            type_name=_required(row, "TYPE NAME"),
            down_type=_required(row, "DOWN TYPE").lower(),
            time_to_failure=_distribution(row["TTF DISTRIBUTION"], row["MTTF"], 0, row["MTTF UNITS"]),
            time_to_repair=_distribution(row["TTR DISTRIBUTION"], row["MTTR"], 0, row["MTTR UNITS"]),
            first_one_at_distribution=_optional_distribution(
                row.get("FIRST ONE AT DISTRIBUTION"), row.get("FOA"), 0, row.get("FOA UNITS"),
            ),
            first_one_at=_optional_duration(row.get("FOA"), row.get("FOA UNITS")),
            first_one_at_unit="minute" if row.get("FOA") is not None else None,
        )
        for row in rows
    )


def _build_setups(rows: list[dict[str, Any]]) -> tuple[SetupTransitionSpec, ...]:
    return tuple(
        SetupTransitionSpec(
            setup_group_name=_text(row.get("SETUP GROUP NAME")) or "",
            current_setup=_text(row.get("CURRENT SETUP")) or "",
            new_setup=_required(row, "NEW SETUP"),
            setup_time=_distribution("deterministic", row["SETUP TIME"], 0, row["ST UNITS"]),
            minimal_run_length=_optional_integer(row.get("MINMAL NUMBER OF RUNS")) or 0,
        )
        for row in rows
    )


def _build_transport(rows: list[dict[str, Any]]) -> tuple[TransportRuleSpec, ...]:
    return tuple(
        TransportRuleSpec(
            from_location=_required(row, "FROM LOCATION"),
            to_location=_required(row, "TO LOCATION"),
            transport_time=_distribution(
                row["TRANSPORTTIME DISTRIBUTION"], row["MEAN"], row.get("OFFSET"), row["TT UNITS"],
            ),
        )
        for row in rows
    )


def _sheet(sheets: dict[str, list[dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    if name not in sheets:
        raise ValueError(f"SMT 工作簿缺少 {name} 表。")
    return sheets[name]


def _records(sheet: RawSheetData) -> list[dict[str, Any]]:
    headers: list[str] = []
    counts: dict[str, int] = {}
    for header in sheet.headers:
        name = header.strip()
        counts[name] = counts.get(name, 0) + 1
        headers.append(name if counts[name] == 1 else f"{name}.{counts[name] - 1}")
    return [dict(zip(headers, row)) for row in sheet.rows]


def _distribution(
    kind: object,
    mean: object,
    offset: object,
    unit: object,
    *,
    normalize_time: bool = True,
) -> DistributionSpec:
    if normalize_time:
        return DistributionSpec(_required_value(kind), _duration(mean, unit), _optional_duration(offset, unit) or 0.0)
    return DistributionSpec(
        _required_value(kind),
        _number(mean),
        _optional_number(offset) or 0.0,
        _required_value(unit).lower(),
    )


def _optional_distribution(
    kind: object,
    mean: object,
    offset: object,
    unit: object,
    *,
    normalize_time: bool = True,
) -> DistributionSpec | None:
    if kind is None or mean is None:
        return None
    return _distribution(kind, mean, offset, unit, normalize_time=normalize_time)


def _duration(value: object, unit: object) -> float:
    return _number(value) * _TIME_TO_MINUTES[_required_value(unit).lower()]


def _optional_duration(value: object, unit: object) -> float | None:
    return None if value is None else _duration(value, unit)


def _minutes_since(value: datetime, origin: datetime) -> float:
    return (value - origin).total_seconds() / 60.0


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise ValueError(f"预期日期时间，实际为 {value!r}。")


def _required(row: dict[str, Any], name: str) -> str:
    return _required_value(row.get(name))


def _required_value(value: object) -> str:
    text = _text(value)
    if text is None:
        raise ValueError("SMT 必填单元格为空。")
    return text


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: object) -> float:
    if value is None:
        raise ValueError("SMT 必填数值为空。")
    return float(value)


def _optional_number(value: object) -> float | None:
    return None if value is None else _number(value)


def _integer(value: object) -> int:
    return int(_number(value))


def _optional_integer(value: object) -> int | None:
    return None if value is None else _integer(value)


def _yes_no(value: object) -> bool:
    return _text(value) in {"Yes", "YES", "yes", "Y", "y", "1", "True", "true"}


def _percentage(value: object, *, default: float) -> float:
    if value is None:
        return default
    return _number(value) / 100.0
