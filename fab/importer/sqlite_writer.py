"""将原始 SMT 工作簿写入 SQLite。"""

from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import sqlite3

from fab.importer.types import RawSmtWorkbook
from fab.model.entities import SimulationSpec


SCHEMA = """
CREATE TABLE IF NOT EXISTS simulation_config (
    start_time REAL NOT NULL,
    end_time REAL NOT NULL,
    release_interval REAL NOT NULL,
    warmup_time REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS smt_sheets (
    sheet_name TEXT PRIMARY KEY,
    headers_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS smt_rows (
    sheet_name TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    values_json TEXT NOT NULL,
    PRIMARY KEY (sheet_name, row_index)
);
CREATE INDEX IF NOT EXISTS idx_smt_rows_sheet ON smt_rows (sheet_name, row_index);
"""


def write_raw_smt_workbook(
    workbook: RawSmtWorkbook,
    database_path: Path,
    simulation: SimulationSpec,
    *,
    replace: bool = False,
) -> None:
    """保存 SMT 原始表与用户提供的仿真时间配置。

    除 ``simulation_config`` 外，本函数不写入任何领域对象或推导数据。
    """

    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(SCHEMA)
        has_data = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM smt_sheets) OR EXISTS(SELECT 1 FROM simulation_config)"
        ).fetchone()[0]
        if has_data and not replace:
            raise ValueError("模型库已有 SMT 数据；如需重建，请使用 replace=True。")
        if has_data:
            connection.execute("DELETE FROM smt_rows")
            connection.execute("DELETE FROM smt_sheets")
            connection.execute("DELETE FROM simulation_config")
            for table in (
                "tool_groups", "route_steps", "lot_releases", "preventive_maintenance",
                "breakdowns", "setup_transitions", "transport_rules",
            ):
                connection.execute(f"DROP TABLE IF EXISTS {table}")

        connection.execute(
            "INSERT INTO simulation_config VALUES (?, ?, ?, ?)",
            (simulation.start_time, simulation.end_time, simulation.release_interval, simulation.warmup_time),
        )
        connection.executemany(
            "INSERT INTO smt_sheets VALUES (?, ?)",
            ((sheet.name, _to_json(sheet.headers)) for sheet in workbook.sheets),
        )
        connection.executemany(
            "INSERT INTO smt_rows VALUES (?, ?, ?)",
            (
                (sheet.name, row_index, _to_json(row))
                for sheet in workbook.sheets
                for row_index, row in enumerate(sheet.rows, start=2)
            ),
        )


def _to_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=_json_default, separators=(",", ":"))


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"无法写入 SQLite 的 SMT 单元格类型：{type(value).__name__}")
